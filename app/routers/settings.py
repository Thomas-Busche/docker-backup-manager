import datetime
import json
import os
import shutil
import signal
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import encryption, oauth_storage, storage_sync
from app.auth import get_admin_user, get_current_user
from app.config import APP_VERSION, BACKUPS_DIR, DEFAULT_RETENTION_COUNT, DEFAULT_RETENTION_DAYS, GITHUB_REPO, SESSION_MAX_AGE, TZ_ERROR, TZ_NAME
from app.database import get_db
from app.docker_client import is_available
from app.models import AppSetting, BackupRecord, Schedule, StorageTarget, User

router = APIRouter(prefix="/api/settings", tags=["settings"])

# ── Update check cache ──────────────────────────────────────────────────────
_update_cache: dict = {}
_update_lock = threading.Lock()


def _parse_version(tag: str) -> tuple[int, ...]:
    """Parse 'v1.4.0' or '1.4.0' into (1, 4, 0) for comparison."""
    clean = tag.lstrip("v")
    try:
        return tuple(int(x) for x in clean.split("."))
    except ValueError:
        return (0,)


def _fetch_latest_release() -> dict:
    """Query GitHub Releases API, falling back to Tags API if no releases exist."""
    headers = {"User-Agent": f"docker-backup-manager/{APP_VERSION}"}

    # Try releases first
    rel_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(rel_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        return {
            "latest_version": tag.lstrip("v"),
            "tag": tag,
            "release_url": data.get("html_url", ""),
            "published_at": data.get("published_at", ""),
        }
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    # No releases — fall back to tags
    tags_url = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
    req = urllib.request.Request(tags_url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        tags = json.loads(resp.read())
    if not tags:
        raise ValueError("Keine Releases oder Tags im Repository gefunden.")
    tag = tags[0].get("name", "")
    repo_url = f"https://github.com/{GITHUB_REPO}"
    return {
        "latest_version": tag.lstrip("v"),
        "tag": tag,
        "release_url": f"{repo_url}/releases/tag/{tag}" if tag else repo_url,
        "published_at": "",
    }


@router.get("/overview")
def overview(user: User = Depends(get_current_user)):
    import pytz

    docker_ok, docker_error = is_available()
    total_size = 0
    if BACKUPS_DIR.exists():
        total_size = sum(f.stat().st_size for f in BACKUPS_DIR.rglob("*") if f.is_file())
    server_now = datetime.datetime.now(pytz.timezone(TZ_NAME))
    return {
        "app_version": APP_VERSION,
        "backups_dir": str(BACKUPS_DIR),
        "backups_total_bytes": total_size,
        "docker_available": docker_ok,
        "docker_error": docker_error,
        "default_retention_count": DEFAULT_RETENTION_COUNT,
        "default_retention_days": DEFAULT_RETENTION_DAYS,
        "encryption_enabled": encryption.is_enabled(),
        "encryption_error": encryption.config_error(),
        "server_time": server_now.isoformat(),
        "timezone": TZ_NAME,
        "timezone_error": TZ_ERROR,
        "session_max_age_hours": round(SESSION_MAX_AGE / 3600, 1),
    }


@router.get("/update-check")
def check_for_update(user: User = Depends(get_current_user)):
    """Query GitHub Releases API and return version comparison. Cached for 1 hour."""
    with _update_lock:
        cached = _update_cache.get("result")
        fetched_at = _update_cache.get("fetched_at")
        if cached and fetched_at and (datetime.datetime.utcnow() - fetched_at).total_seconds() < 3600:
            return cached

    try:
        release = _fetch_latest_release()
        current_parts = _parse_version(APP_VERSION)
        latest_parts = _parse_version(release["latest_version"])
        result = {
            "current_version": APP_VERSION,
            "latest_version": release["latest_version"],
            "update_available": latest_parts > current_parts,
            "release_url": release["release_url"],
            "published_at": release["published_at"],
            "github_repo": GITHUB_REPO,
            "tag": release["tag"],
            "error": None,
        }
    except Exception as exc:
        result = {
            "current_version": APP_VERSION,
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "published_at": None,
            "github_repo": GITHUB_REPO,
            "error": str(exc),
        }

    with _update_lock:
        _update_cache["result"] = result
        _update_cache["fetched_at"] = datetime.datetime.utcnow()
    return result


@router.post("/apply-update")
def apply_update(user: User = Depends(get_admin_user)):
    """Download the latest GitHub release, stage it in /data/.app_update/,
    then restart the container. The entrypoint script applies the staged
    files before uvicorn starts on the next boot."""
    upd = check_for_update(user)
    if upd.get("error"):
        raise HTTPException(503, f"Update-Prüfung fehlgeschlagen: {upd['error']}")
    if not upd.get("update_available"):
        raise HTTPException(400, "Kein Update verfügbar — bereits aktuell.")

    tag = upd["tag"]  # e.g. "v1.4.1"
    tarball_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.tar.gz"

    # Download and stage new app/ files in /data/.app_update/
    # The container entrypoint copies them over /app/app/ on next start.
    update_stage = BACKUPS_DIR.parent / ".app_update"
    if update_stage.exists():
        shutil.rmtree(update_stage)
    update_stage.mkdir(parents=True)

    try:
        with urllib.request.urlopen(tarball_url, timeout=120) as resp:
            with tarfile.open(fileobj=resp, mode="r|gz") as tar:
                for member in tar:
                    # Archive root is e.g. "docker-backup-manager-1.4.1/"
                    parts = member.name.split("/", 1)
                    if len(parts) < 2:
                        continue
                    rel = parts[1]  # strip the root directory prefix
                    if not (rel.startswith("app/") or rel == "requirements.txt"):
                        continue
                    dest = (update_stage / rel).resolve()
                    if not str(dest).startswith(str(update_stage.resolve())):
                        continue  # skip path-traversal attempts
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if member.isfile():
                        with tar.extractfile(member) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
    except Exception as exc:
        shutil.rmtree(update_stage, ignore_errors=True)
        raise HTTPException(500, f"Download fehlgeschlagen: {exc}")

    # Restart after the response is delivered
    def _restart():
        import time
        time.sleep(1)
        os.kill(1, signal.SIGTERM)

    threading.Thread(target=_restart, daemon=True, name="update-restart").start()
    return {"ok": True, "version": upd["latest_version"]}


class StorageTargetPayload(BaseModel):
    name: str
    type: str  # "local_path" | "s3" | "rclone"
    config: dict
    enabled: bool = True


_REDACT_FIELDS: dict[str, list[str]] = {
    "smb": ["password"],
    "s3": ["secret_key"],
}


def _target_config_for_response(target_type: str, config: dict) -> dict:
    config = dict(config)
    for field in _REDACT_FIELDS.get(target_type, []):
        if field in config:
            config[field] = "***"
    if target_type in ("google_drive", "onedrive"):
        config = {**config, "connected": bool(config.get("refresh_token"))}
        config.pop("refresh_token", None)
    return config


@router.get("/storage-targets/{target_id}/space")
def get_target_space(target_id: int, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """Returns free/used/total bytes for a storage target. Not all types are supported."""
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise HTTPException(404, "Storage target not found")
    try:
        return storage_sync.get_target_space_info(target.type, target.config_json)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Speicherplatz-Abfrage fehlgeschlagen: {exc}")


@router.get("/storage-targets")
def list_targets(db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    rows = db.query(StorageTarget).order_by(StorageTarget.created_at.desc()).all()
    return {"targets": [
        {
            "id": t.id, "name": t.name, "type": t.type,
            "config": _target_config_for_response(t.type, json.loads(t.config_json)),
            "enabled": t.enabled, "last_sync_at": t.last_sync_at.isoformat() + "Z" if t.last_sync_at else None,
            "last_sync_status": t.last_sync_status, "last_sync_error": t.last_sync_error,
        } for t in rows
    ]}


@router.post("/storage-targets")
def create_target(payload: StorageTargetPayload, db: Session = Depends(get_db),
                   user: User = Depends(get_admin_user)):
    if payload.type not in ("local_path", "smb", "s3", "rclone", "google_drive", "onedrive"):
        raise HTTPException(400, "Invalid target type")
    target = StorageTarget(
        name=payload.name, type=payload.type, config_json=json.dumps(payload.config), enabled=payload.enabled,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return {"id": target.id}


@router.put("/storage-targets/{target_id}")
def update_target(target_id: int, payload: StorageTargetPayload, db: Session = Depends(get_db),
                   user: User = Depends(get_admin_user)):
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise HTTPException(404, "Storage target not found")
    config = payload.config
    if target.type in ("google_drive", "onedrive") and target.type == payload.type:
        # The frontend never receives the refresh_token back (see
        # _target_config_for_response), so a naive overwrite here would erase
        # it. Only folder_path (and similar non-secret fields) come from the
        # client for these types - keep the stored refresh_token/account.
        stored = json.loads(target.config_json)
        config = {**stored, **{k: v for k, v in payload.config.items() if k != "refresh_token"}}
    target.name = payload.name
    target.type = payload.type
    target.config_json = json.dumps(config)
    target.enabled = payload.enabled
    db.commit()
    return {"ok": True}


@router.delete("/storage-targets/{target_id}")
def delete_target(target_id: int, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise HTTPException(404, "Storage target not found")
    db.delete(target)
    db.commit()
    return {"ok": True}


class StorageTargetTestPayload(BaseModel):
    type: str
    config: dict


@router.post("/storage-targets/test")
def test_storage_target_config(payload: StorageTargetTestPayload, user: User = Depends(get_admin_user)):
    """Tests connection settings before a target has been saved, so mistakes
    (wrong share name, bad credentials, ...) surface immediately in the dialog."""
    try:
        storage_sync.check_target_connection(payload.type, json.dumps(payload.config))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Connection test failed: {exc}")


class SmbSharesPayload(BaseModel):
    server: str
    username: str
    password: str
    domain: str = ""
    port: str = "445"


@router.post("/smb/shares")
def list_smb_shares(payload: SmbSharesPayload, user: User = Depends(get_admin_user)):
    try:
        shares = storage_sync.list_smb_shares(payload.model_dump())
        return {"shares": shares}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Freigaben konnten nicht abgerufen werden: {exc}")


@router.post("/storage-targets/{target_id}/test")
def test_storage_target(target_id: int, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise HTTPException(404, "Storage target not found")
    try:
        storage_sync.check_target_connection(target.type, target.config_json)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Connection test failed: {exc}")


@router.post("/storage-targets/{target_id}/import-catalog")
def import_catalog_from_target(target_id: int, db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """Scans a storage target for backups that already exist there (e.g. after
    a full host loss, restoring from an offsite copy) and creates local
    catalog entries for any not already known. The actual data is only
    downloaded later, on demand, when a restore is triggered for one of them."""
    target = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
    if not target:
        raise HTTPException(404, "Storage target not found")
    try:
        found = storage_sync.list_backups_on_target(target.type, target.config_json)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Speicherziel konnte nicht durchsucht werden: {exc}")

    imported = 0
    skipped = 0
    updated = 0
    for entry in found:
        local_path = str(BACKUPS_DIR / entry["relative_key"])
        existing = db.query(BackupRecord).filter(BackupRecord.path == local_path).first()
        containers = entry.get("containers") or []
        if existing:
            # Update containers_json on landscape records that were imported before
            # this field was populated — a re-import should not leave them broken.
            if (existing.backup_type == "landscape" and not existing.containers_json
                    and containers):
                existing.containers_json = json.dumps(containers)
                updated += 1
            else:
                skipped += 1
            continue
        db.add(BackupRecord(
            backup_type=entry["backup_type"], name=entry["name"], path=local_path, status="ok",
            size_bytes=entry["size_bytes"], streamed_target_id=target.id, created_at=entry["created_at"],
            containers_json=json.dumps(containers) if containers else None,
        ))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped, "updated": updated, "found": len(found)}


# ---------- Google Drive / OneDrive OAuth ----------

@router.get("/oauth/{provider}/start", response_class=HTMLResponse)
def oauth_start(provider: str, user: User = Depends(get_admin_user)):
    if provider not in ("google", "onedrive"):
        raise HTTPException(404, "Unknown provider")
    try:
        url, state = oauth_storage.build_auth_url(provider)
    except oauth_storage.OAuthNotConfigured as exc:
        # Return a postMessage page so the opener window gets the error as a toast.
        # A plain HTTPException would show raw JSON in the popup with no feedback to the main window.
        return HTMLResponse(_callback_html(str(exc), ok=False, error=str(exc)))
    return RedirectResponse(url)


_CALLBACK_PAGE = """<!doctype html><html><body>
<p>{message}</p>
<script>
  if (window.opener) {{
    window.opener.postMessage({payload}, window.location.origin);
  }}
  window.close();
</script>
</body></html>"""


def _callback_html(message: str, ok: bool, state: str = "", error: str = "") -> str:
    import html
    # error/state can contain attacker-controlled text (query params on this public
    # redirect endpoint) - json.dumps escapes quotes, but "</script>" inside a string
    # would still close the tag early, so also escape "</" before embedding.
    payload = json.dumps({"dbmOAuth": True, "ok": ok, "state": state, "error": error}).replace("</", "<\\/")
    return _CALLBACK_PAGE.format(message=html.escape(message), payload=payload)


@router.get("/oauth/{provider}/callback", response_class=HTMLResponse)
def oauth_callback(provider: str, code: str = "", state: str = "", error: str = "",
                    user: User = Depends(get_admin_user)):
    if provider not in ("google", "onedrive"):
        raise HTTPException(404, "Unknown provider")
    if error:
        return _callback_html(f"Anmeldung fehlgeschlagen: {error}", ok=False, error=error)
    try:
        oauth_storage.handle_callback(provider, code, state)
    except Exception as exc:  # noqa: BLE001
        return _callback_html(f"Anmeldung fehlgeschlagen: {exc}", ok=False, error=str(exc))
    return _callback_html("Erfolgreich verbunden - dieses Fenster kann geschlossen werden.", ok=True, state=state)


class OAuthCompletePayload(BaseModel):
    state: str
    name: str
    folder_path: str = ""
    target_id: Optional[int] = None  # set when reconnecting an existing target, not creating a new one


@router.post("/storage-targets/oauth-complete")
def oauth_complete(payload: OAuthCompletePayload, db: Session = Depends(get_db),
                    user: User = Depends(get_admin_user)):
    try:
        pending = oauth_storage.pop_pending(payload.state)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    target_type = "google_drive" if pending["provider"] == "google" else "onedrive"
    config = {
        "refresh_token": pending["refresh_token"],
        "account": pending["account"],
        "folder_path": payload.folder_path.strip("/"),
    }

    if payload.target_id is not None:
        target = db.query(StorageTarget).filter(StorageTarget.id == payload.target_id).first()
        if not target:
            raise HTTPException(404, "Storage target not found")
        target.name = payload.name or target.name
        target.type = target_type
        target.config_json = json.dumps(config)
    else:
        target = StorageTarget(
            name=payload.name or pending["account"] or target_type, type=target_type,
            config_json=json.dumps(config), enabled=True,
        )
        db.add(target)
    db.commit()
    db.refresh(target)
    return {"id": target.id, "account": pending["account"]}


# ── OAuth app credentials (wizard) ──────────────────────────────────────────

class OAuthAppPayload(BaseModel):
    client_id: str
    client_secret: str = ""  # empty = keep existing


def _upsert_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


@router.get("/oauth-apps")
def get_oauth_apps(db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """Return configuration state for each OAuth provider (IDs only, no secrets)."""
    from app import config as cfg
    effective_url = oauth_storage._get_public_url()
    url_from_env = bool(cfg.PUBLIC_URL)
    result = {}
    for provider in ("onedrive", "google"):
        client_id, client_secret = oauth_storage._get_credentials(provider)
        from_env = (
            bool(cfg.MS_CLIENT_ID and cfg.MS_CLIENT_SECRET)
            if provider == "onedrive"
            else bool(cfg.GOOGLE_CLIENT_ID and cfg.GOOGLE_CLIENT_SECRET)
        )
        redirect = f"{effective_url}/api/settings/oauth/{provider}/callback" if effective_url else ""
        result[provider] = {
            "configured": bool(client_id and client_secret),
            "client_id": client_id,
            "from_env": from_env,
            "redirect_uri": redirect,
            "public_url_set": bool(effective_url),
            "public_url": effective_url,
            "public_url_from_env": url_from_env,
        }
    return result


class PublicUrlPayload(BaseModel):
    url: str


@router.put("/public-url")
def save_public_url(payload: PublicUrlPayload, db: Session = Depends(get_db),
                    user: User = Depends(get_admin_user)):
    url = payload.url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL muss mit http:// oder https:// beginnen.")
    _upsert_setting(db, "public_url", url)
    db.commit()
    return {"ok": True, "url": url}


@router.put("/oauth-apps/{provider}")
def save_oauth_app(provider: str, payload: OAuthAppPayload,
                   db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    if provider not in ("onedrive", "google"):
        raise HTTPException(404, "Unknown provider")
    prefix = "oauth_ms" if provider == "onedrive" else "oauth_google"
    _upsert_setting(db, f"{prefix}_client_id", payload.client_id.strip())
    if payload.client_secret:
        _upsert_setting(db, f"{prefix}_client_secret", payload.client_secret.strip())
    db.commit()
    return {"ok": True}


# ── Config export / import ──────────────────────────────────────────────────

@router.get("/export")
def export_config(db: Session = Depends(get_db), user: User = Depends(get_admin_user)):
    """Download all schedules, storage targets and users as a JSON backup."""
    targets = db.query(StorageTarget).order_by(StorageTarget.id).all()
    schedules = db.query(Schedule).order_by(Schedule.id).all()
    users = db.query(User).order_by(User.id).all()

    payload = {
        "dbm_config_version": 1,
        "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        "app_version": APP_VERSION,
        "storage_targets": [
            {
                "name": t.name, "type": t.type,
                "config": json.loads(t.config_json),
                "enabled": t.enabled,
            } for t in targets
        ],
        "schedules": [
            {
                "name": s.name, "target_type": s.target_type,
                "target_ref": s.target_ref, "project_filter": s.project_filter,
                "name_contains": s.name_contains, "exclude_names": s.exclude_names,
                "cron_expression": s.cron_expression,
                "retention_count": s.retention_count, "retention_days": s.retention_days,
                "stop_containers": s.stop_containers, "enabled": s.enabled,
                # stream/sync targets referenced by name so they survive re-import
                "stream_target_name": next(
                    (t.name for t in targets if t.id == s.stream_volumes_target_id), None
                ),
                "sync_target_names": [
                    t.name for t in targets
                    if t.id in json.loads(s.storage_target_ids or "[]")
                ],
            } for s in schedules
        ],
        "users": [
            {
                "username": u.username,
                "password_hash": u.password_hash,
                "is_admin": u.is_admin,
            } for u in users
        ],
    }
    filename = f"dbm-config-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ImportConfigPayload(BaseModel):
    data: dict
    overwrite: bool = False  # if True, existing targets/schedules with same name are replaced


@router.post("/import")
def import_config(payload: ImportConfigPayload, db: Session = Depends(get_db),
                  user: User = Depends(get_admin_user)):
    """Restore schedules and storage targets from a previously exported JSON."""
    data = payload.data
    if data.get("dbm_config_version") != 1:
        raise HTTPException(400, "Unbekanntes Exportformat — bitte eine Datei verwenden die von dieser App exportiert wurde.")

    imported = {"targets": 0, "schedules": 0, "users": 0, "skipped": 0}

    # 1. Storage targets
    name_to_target: dict[str, StorageTarget] = {}
    for td in data.get("storage_targets", []):
        existing = db.query(StorageTarget).filter(StorageTarget.name == td["name"]).first()
        if existing:
            if payload.overwrite:
                existing.type = td["type"]
                existing.config_json = json.dumps(td["config"])
                existing.enabled = td.get("enabled", True)
                name_to_target[td["name"]] = existing
                imported["targets"] += 1
            else:
                name_to_target[td["name"]] = existing
                imported["skipped"] += 1
        else:
            t = StorageTarget(
                name=td["name"], type=td["type"],
                config_json=json.dumps(td["config"]),
                enabled=td.get("enabled", True),
            )
            db.add(t)
            db.flush()
            name_to_target[td["name"]] = t
            imported["targets"] += 1

    # 2. Schedules
    for sd in data.get("schedules", []):
        existing = db.query(Schedule).filter(Schedule.name == sd["name"]).first()
        stream_id = name_to_target[sd["stream_target_name"]].id if sd.get("stream_target_name") and sd["stream_target_name"] in name_to_target else None
        sync_ids = json.dumps([name_to_target[n].id for n in sd.get("sync_target_names", []) if n in name_to_target])
        if existing:
            if payload.overwrite:
                for k, v in sd.items():
                    if k not in ("stream_target_name", "sync_target_names") and hasattr(existing, k):
                        setattr(existing, k, v)
                existing.stream_volumes_target_id = stream_id
                existing.storage_target_ids = sync_ids
                imported["schedules"] += 1
            else:
                imported["skipped"] += 1
        else:
            s = Schedule(
                name=sd["name"], target_type=sd["target_type"],
                target_ref=sd.get("target_ref"), project_filter=sd.get("project_filter"),
                name_contains=sd.get("name_contains"), exclude_names=sd.get("exclude_names"),
                cron_expression=sd["cron_expression"],
                retention_count=sd.get("retention_count", 7),
                retention_days=sd.get("retention_days", 0),
                stop_containers=sd.get("stop_containers", False),
                enabled=sd.get("enabled", True),
                stream_volumes_target_id=stream_id,
                storage_target_ids=sync_ids,
            )
            db.add(s)
            imported["schedules"] += 1

    # 3. Users (only import if not already existing)
    for ud in data.get("users", []):
        existing = db.query(User).filter(User.username == ud["username"]).first()
        if existing:
            imported["skipped"] += 1
        else:
            db.add(User(
                username=ud["username"],
                password_hash=ud["password_hash"],
                is_admin=ud.get("is_admin", False),
            ))
            imported["users"] += 1

    db.commit()

    # Reload scheduler with newly imported schedules
    from app import scheduler as scheduler_module
    scheduler_module.load_all_schedules()

    return imported
