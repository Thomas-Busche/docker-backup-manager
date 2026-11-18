import json
import os
import tarfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import backup_engine, backup_records, chain, encryption, event_log, job_tracker, netpulse, restic_engine, storage_sync
from app.auth import get_current_user
from app.config import BACKUPS_DIR
from app.database import SessionLocal, get_db
from app.models import BackupRecord, StorageTarget, User
from app.restore_engine import restore_container
from app.storage_sync import _relative_key

router = APIRouter(prefix="/api/backups", tags=["backups"])


@router.get("")
def list_backups(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models import StorageTarget
    records = db.query(BackupRecord).order_by(BackupRecord.created_at.desc()).all()

    # Self-heal: a successful backup whose directory no longer exists on disk
    # (e.g. deleted outside the app, or left behind by a retention pass that
    # was interrupted before committing) is just stale, misleading data -
    # drop it rather than showing a version that can never actually be
    # restored or re-deleted. Failed backups intentionally have no directory
    # (the partial data is cleaned up right away), so those are left alone.
    # Catalog-imported backups (streamed_target_id set, local path absent) are
    # intentionally remote-only — they must never be treated as stale.
    stale = [r for r in records
             if r.status == "ok" and not Path(r.path).exists() and r.streamed_target_id is None]
    if stale:
        for r in stale:
            db.delete(r)
        db.commit()
        records = [r for r in records if r not in stale]

    records_by_path = {r.path: r for r in records}
    # name → latest record per name (for size fallback when path lookup fails)
    records_latest_by_name: dict[str, BackupRecord] = {}
    for r in records:
        if r.backup_type == "container":
            existing = records_latest_by_name.get(r.name)
            if existing is None or r.created_at > existing.created_at:
                records_latest_by_name[r.name] = r

    id_by_path = {r.path: r.id for r in records}
    # Restic: every snapshot is a complete state; the oldest one per container
    # and target is the initial (full) upload, the later ones only add changes
    restic_initial_ids: set[int] = set()
    _first: dict = {}
    for r in records:
        if r.backup_type == "container" and r.backup_level == "restic" and r.status == "ok":
            key = (r.name, r.streamed_target_id)
            if key not in _first or r.created_at < _first[key].created_at:
                _first[key] = r
    restic_initial_ids = {r.id for r in _first.values()}
    dependents_count: dict[int, int] = {}
    for r in records:
        for dep in backup_records.depends_paths(r):
            if dep in id_by_path:
                dependents_count[id_by_path[dep]] = dependents_count.get(id_by_path[dep], 0) + 1

    grouped: dict[str, list] = {}
    for r in records:
        if r.status == "failed":
            continue
        entry: dict = {
            "id": r.id,
            "backup_type": r.backup_type,
            "status": r.status,
            "error": r.error,
            "size_bytes": r.size_bytes,
            "created_at": r.created_at.isoformat() + "Z",
            "containers": json.loads(r.containers_json) if r.containers_json else [],
            "synced_target_ids": json.loads(r.synced_target_ids) if r.synced_target_ids else [],
            "streamed_target_id": r.streamed_target_id,
            "backup_level": r.backup_level or "full",
            "chain_id": r.chain_id,
            "data_as_of": (r.data_as_of.isoformat() + "Z") if r.data_as_of else None,
            "depends_on_ids": [id_by_path[d] for d in backup_records.depends_paths(r) if d in id_by_path],
            "missing_dependencies": [d for d in backup_records.depends_paths(r) if d not in id_by_path],
            "dependents_count": dependents_count.get(r.id, 0),
            "added_bytes": r.added_bytes,
            "restic_initial": r.id in restic_initial_ids,
        }
        if r.backup_type == "landscape":
            member_names, member_size = _landscape_member_info(r, records_by_path, records_latest_by_name)
            entry["member_names"] = member_names
            entry["member_size_bytes"] = member_size
            entry["member_levels"], entry["member_added_bytes"] = _landscape_member_levels(r, records_by_path, restic_initial_ids)
        grouped.setdefault(r.name, []).append(entry)

    targets = db.query(StorageTarget).all()
    targets_map = {t.id: {"name": t.name, "type": t.type} for t in targets}
    return {"groups": grouped, "targets": targets_map}


def _read_member_jsons(landscape_dir: Path) -> list[dict]:
    """Return parsed member JSON dicts from a landscape dir, skipping meta.json
    and any file that lacks a backup_path (= not a real member file)."""
    result = []
    if not landscape_dir.exists():
        return result
    files = sorted(landscape_dir.glob("*.json"))
    # Encrypted landscape backups store the member files as *.json.enc - without
    # reading them a landscape restored from a storage target showed no members.
    if encryption.is_enabled():
        files += sorted(landscape_dir.glob("*.json.enc"))
    for jf in files:
        if jf.name in ("meta.json", "meta.json.enc"):
            continue
        try:
            if jf.suffix == ".enc":
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    plain = Path(tmp) / "member.json"
                    encryption.decrypt_file(jf, plain)
                    d = json.loads(plain.read_text())
            else:
                d = json.loads(jf.read_text())
            if d.get("backup_path"):
                result.append(d)
        except Exception:
            pass
    return result


def _landscape_member_info(
    record: BackupRecord,
    records_by_path: dict,
    records_latest_by_name: dict | None = None,
) -> tuple[list[str], int]:
    """Return (member_names, total_member_size_bytes) for a landscape record."""
    landscape_dir = Path(record.path)
    member_jsons = _read_member_jsons(landscape_dir)

    if member_jsons:
        names, size = [], 0
        for d in member_jsons:
            cname = d.get("container_name", "")
            bpath = d.get("backup_path", "")
            if cname:
                names.append(cname)
            mem_rec = records_by_path.get(bpath)
            # Path mismatch fallback: use latest record for this container name
            if mem_rec is None and cname and records_latest_by_name:
                mem_rec = records_latest_by_name.get(cname)
            if mem_rec:
                size += mem_rec.size_bytes or 0
        return names, size

    # Fallback for old backup format: containers_json has names, look up sizes by name
    names = json.loads(record.containers_json) if record.containers_json else []
    size = 0
    if records_latest_by_name:
        for name in names:
            mem_rec = records_latest_by_name.get(name)
            if mem_rec:
                size += mem_rec.size_bytes or 0
    return names, size


def _landscape_member_levels(record: BackupRecord, records_by_path: dict,
                             restic_initial_ids: Optional[set] = None) -> tuple[dict, Optional[int]]:
    """({level: count}, restic bytes added) of a group backup's member backups
    (full / incremental / synthetic_full / restic), so the list shows whether
    a run was incremental and what it really added to a restic repository."""
    levels: dict[str, int] = {}
    added = None
    for d in _read_member_jsons(Path(record.path)):
        mem_rec = records_by_path.get(d.get("backup_path", ""))
        level = (mem_rec.backup_level if mem_rec is not None else None) or d.get("backup_level") or "full"
        if level == "restic" and mem_rec is not None and mem_rec.id in (restic_initial_ids or ()):
            level = "restic_full"
        levels[level] = levels.get(level, 0) + 1
        if mem_rec is not None and mem_rec.added_bytes is not None:
            added = (added or 0) + mem_rec.added_bytes
    return levels, added


import logging as _logging
_bg_logger = _logging.getLogger("dbm.remote_cleanup")


def _snapshot_remote_task(record: BackupRecord, db: Session, exclude_id: int = None) -> dict:
    """Sammelt alle für die Remote-Bereinigung nötigen Daten aus einem BackupRecord.
    Liest meta.json JETZT (vor der lokalen Löschung) und gibt ein reines Dict zurück
    das kein ORM mehr benötigt — sicher für Hintergrund-Threads."""
    synced = []
    for target_id in json.loads(record.synced_target_ids or "[]"):
        t = db.query(StorageTarget).filter(StorageTarget.id == target_id).first()
        if t:
            synced.append({"type": t.type, "config_json": t.config_json, "name": t.name})

    stream = None
    is_last = False
    if record.streamed_target_id is not None and record.backup_type == "landscape":
        # A group backup's own folder (_landscapes/<name>/<ts>: meta + member
        # list) is uploaded to the streaming target too - remove it from there
        t = db.query(StorageTarget).filter(StorageTarget.id == record.streamed_target_id).first()
        if t and not any(s["name"] == t.name and s["type"] == t.type for s in synced):
            synced.append({"type": t.type, "config_json": t.config_json, "name": t.name})
    if record.streamed_target_id is not None and record.backup_type == "container":
        t = db.query(StorageTarget).filter(StorageTarget.id == record.streamed_target_id).first()
        if t:
            stream = {"type": t.type, "config_json": t.config_json, "id": record.streamed_target_id, "name": t.name}
            q = db.query(BackupRecord).filter(
                BackupRecord.name == record.name,
                BackupRecord.streamed_target_id == record.streamed_target_id,
                BackupRecord.backup_type == "container",
            )
            if exclude_id is not None:
                q = q.filter(BackupRecord.id != exclude_id)
            is_last = q.count() == 0

    # Read meta.json NOW — before the local directory is deleted — so the
    # background thread can forget restic snapshots without needing the file.
    # For encrypted backups meta.json doesn't exist — only meta.json.enc does.
    # In that case decrypt it first to get the engine/snapshot metadata.
    meta_path = Path(record.path) / "meta.json"
    meta = {}
    try:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
        else:
            enc_path = Path(record.path) / "meta.json.enc"
            if enc_path.exists():
                import tempfile
                from app import encryption
                tmp = Path(tempfile.mktemp(suffix=".json"))
                try:
                    encryption.decrypt_file(enc_path, tmp)
                    meta = json.loads(tmp.read_text())
                finally:
                    tmp.unlink(missing_ok=True)
    except Exception:
        pass

    return {
        "path": record.path,
        "name": record.name,
        "backup_type": record.backup_type,
        "synced": synced,
        "stream": stream,
        "is_last": is_last,
        "backup_engine": meta.get("backup_engine", ""),
        "restic_repo_url": meta.get("restic_repo_url", ""),
        "restic_snapshot_ids": meta.get("restic_snapshot_ids", {}),
    }


def _run_remote_cleanup(task: dict) -> list[str]:
    """Führt die Remote-Bereinigung für ein vorbereitetes Task-Dict durch.
    Kein DB-Zugriff, kein Lesen von meta.json — sicher im Hintergrund-Thread."""
    errors = []
    rec_path = Path(task["path"])
    rel_key = _relative_key(rec_path)

    _bg_logger.info(
        "Remote-Bereinigung starten: name=%s path=%s engine=%s synced=%s stream=%s is_last=%s",
        task["name"], task["path"], task["backup_engine"] or "(keins)",
        [t["name"] for t in task["synced"]],
        task["stream"]["name"] if task["stream"] else None,
        task["is_last"],
    )

    for t in task["synced"]:
        try:
            _bg_logger.info("Lösche synced-Kopie auf '%s': %s", t["name"], rel_key)
            storage_sync.delete_from_target(t["type"], t["config_json"], rel_key)
            _bg_logger.info("Synced-Kopie gelöscht: %s auf '%s'", rel_key, t["name"])
            # Leave no empty <name>/ (and _landscapes/) folders behind
            parent = rel_key.rsplit("/", 1)[0] if "/" in rel_key else ""
            while parent:
                storage_sync.try_rmdir_on_target(t["type"], t["config_json"], parent)
                parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        except Exception as exc:
            _bg_logger.warning("Fehler beim Löschen von '%s' auf '%s': %s", rel_key, t["name"], exc)
            errors.append(f"{t['name']}: {exc}")

    if task["stream"] and task["backup_type"] == "container":
        t = task["stream"]
        stream_target = (t["type"], t["config_json"], t["id"])
        try:
            if task["backup_engine"] == "restic":
                # Forget specific snapshots using pre-read IDs (meta.json is already deleted)
                repo_url = task["restic_repo_url"]
                snapshot_ids = task["restic_snapshot_ids"]
                if repo_url and snapshot_ids:
                    _bg_logger.info("Vergesse %d Restic-Snapshot(s) für '%s'", len(snapshot_ids), task["name"])
                    target_config = json.loads(t["config_json"] or "{}")
                    password = restic_engine.get_password()
                    _, env, smb_conf_path = restic_engine.repo_url_and_env(
                        t["type"], target_config, task["name"]
                    )
                    try:
                        for key, sid in snapshot_ids.items():
                            try:
                                restic_engine.forget_snapshot(repo_url, env, password, sid)
                                _bg_logger.info("Snapshot %s vergessen (%s/%s)", sid[:8], task["name"], key)
                            except Exception as exc:
                                msg = str(exc)
                                if "does not exist" in msg and "config" in msg:
                                    # Restic repo already gone — nothing to forget, skip silently
                                    _bg_logger.info(
                                        "Snapshot %s übersprungen — Restic-Repo existiert nicht mehr (%s/%s)",
                                        sid[:8], task["name"], key,
                                    )
                                else:
                                    _bg_logger.warning("forget_snapshot %s fehlgeschlagen: %s", sid[:8], exc)
                    finally:
                        if smb_conf_path:
                            try:
                                restic_engine.rclone_backends.discard_conf(smb_conf_path)
                            except Exception:
                                pass
                elif not repo_url:
                    _bg_logger.warning(
                        "Restic-Repo-URL fehlt für '%s' — Snapshots können nicht vergessen werden "
                        "(meta.json möglicherweise nicht lesbar gewesen)", task["name"]
                    )
                # Delete the per-backup timestamp directory on the stream target.
                # This holds image.tar (and nothing else for restic mode) and is
                # separate from the restic repo folder.
                _bg_logger.info("Lösche Timestamp-Verzeichnis auf Stream-Ziel '%s': %s", t["name"], rel_key)
                try:
                    storage_sync.delete_from_target(t["type"], t["config_json"], rel_key)
                    _bg_logger.info("Timestamp-Verzeichnis gelöscht: %s auf '%s'", rel_key, t["name"])
                except Exception as exc:
                    _bg_logger.warning(
                        "Timestamp-Verzeichnis '%s' auf '%s' nicht gelöscht (möglicherweise nicht vorhanden): %s",
                        rel_key, t["name"], exc,
                    )
                if task["is_last"]:
                    _bg_logger.info("Letzter Record für '%s' — lösche Restic-Repo auf '%s'", task["name"], t["name"])
                    restic_engine.purge_restic_repo(task["name"], stream_target)
                    # After both timestamp-dir and restic_repo are gone, the container-level
                    # folder should be empty — try to remove it (silent on failure)
                    container_key = rel_key.split("/")[0]
                    storage_sync.try_rmdir_on_target(t["type"], t["config_json"], container_key)
                else:
                    _bg_logger.info("Nicht letzter Record für '%s' — Restic-Repo bleibt erhalten", task["name"])
            else:
                # Old tar-stream or non-restic stream: delete the whole remote directory
                _bg_logger.info("Lösche Stream-Verzeichnis auf '%s': %s", t["name"], rel_key)
                storage_sync.delete_from_target(t["type"], t["config_json"], rel_key)
                _bg_logger.info("Stream-Verzeichnis gelöscht: %s auf '%s'", rel_key, t["name"])
        except Exception as exc:
            _bg_logger.warning("Remote-Bereinigung Stream '%s' fehlgeschlagen: %s", t["name"], exc)
            errors.append(f"{t['name']}: {exc}")

    _bg_logger.info("Remote-Bereinigung abgeschlossen für '%s' (Fehler: %d)", task["name"], len(errors))
    return errors


def _cleanup_remote_for_record(record: BackupRecord, db) -> None:
    """Bereinigt alle Remote-Daten (Sync-Ziele + Stream-Ziel/Restic) für einen
    einzelnen BackupRecord. Synchron — wird vom Scheduler-Retention-Thread aufgerufen
    der bereits im Hintergrund läuft. exclude_id=record.id stellt sicher dass dieser
    Record selbst nicht in die is_last-Prüfung einfliesst."""
    task = _snapshot_remote_task(record, db, exclude_id=record.id)
    errs = _run_remote_cleanup(task)
    for e in errs:
        _bg_logger.warning("Remote-Bereinigung Retention '%s': %s", record.name, e)


def _find_member_records(record: BackupRecord, db: Session) -> list[BackupRecord]:
    """Gibt alle Mitglieder-BackupRecords einer Landscape zurück."""
    landscape_dir = Path(record.path)
    member_jsons = _read_member_jsons(landscape_dir) if landscape_dir.exists() else []
    if member_jsons:
        members = []
        for d in member_jsons:
            m = db.query(BackupRecord).filter(
                BackupRecord.path == d.get("backup_path", ""),
                BackupRecord.name == d.get("container_name", ""),
            ).first()
            if m:
                members.append(m)
        return members
    # Fallback for old backups without per-member JSON files: use containers_json names.
    # The landscape record is committed first, then member records are committed right after
    # in the same background job — so member created_at values are slightly later but
    # within seconds. We use a 5-minute window to reliably catch them without
    # risking false matches from a different backup run.
    names = json.loads(record.containers_json) if record.containers_json else []
    result = []
    window_end = record.created_at + timedelta(minutes=5)
    for name in names:
        m = db.query(BackupRecord).filter(
            BackupRecord.name == name,
            BackupRecord.backup_type == "container",
            BackupRecord.created_at >= record.created_at,
            BackupRecord.created_at <= window_end,
        ).order_by(BackupRecord.created_at).first()
        if m:
            result.append(m)
    return result


class BulkDeletePayload(BaseModel):
    ids: list[int]


@router.post("/bulk-delete")
def delete_backups_bulk(payload: BulkDeletePayload, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    if not payload.ids:
        return {"ok": True, "deleted": 0}

    all_tasks: list[dict] = []
    deleted = 0
    skipped: list[int] = []
    deleting_ids = set(payload.ids)

    # Newest first, so incrementals go before the chain members they build on
    ordered = db.query(BackupRecord).filter(BackupRecord.id.in_(payload.ids)).order_by(
        BackupRecord.created_at.desc()).all()
    for record in ordered:
        if _chain_conflict(db, record, deleting_ids):
            skipped.append(record.id)
            continue
        members: list[BackupRecord] = []
        if record.backup_type == "landscape":
            members = [m for m in _find_member_records(record, db) if not _chain_conflict(db, m, {m.id})]
            for m in members:
                all_tasks.append(_snapshot_remote_task(m, db, exclude_id=m.id))
        all_tasks.append(_snapshot_remote_task(record, db, exclude_id=record.id))
        for m in members:
            try:
                backup_engine.delete_backup(Path(m.path))
                db.delete(m)
            except Exception as exc:
                _bg_logger.warning("Bulk-Löschung Mitglied %s: %s", m.name, exc)
        backup_engine.delete_backup(Path(record.path))
        db.delete(record)
        db.flush()
        deleted += 1

    db.commit()

    def _bg():
        for task in all_tasks:
            for e in _run_remote_cleanup(task):
                _bg_logger.warning("Remote-Bereinigung Bulk '%s': %s", task["name"], e)

    threading.Thread(target=_bg, daemon=True, name="remote-cleanup-bulk").start()
    return {"ok": True, "deleted": deleted, "skipped_chain_bases": skipped}


def _dependent_closure(db: Session, record: BackupRecord) -> list[BackupRecord]:
    """Every record that (directly or through others) needs `record`."""
    seen: dict[int, BackupRecord] = {}
    todo = [record]
    while todo:
        cur = todo.pop()
        for d in backup_records.dependents(db, cur):
            if d.id not in seen and d.id != record.id:
                seen[d.id] = d
                todo.append(d)
    return sorted(seen.values(), key=lambda r: r.created_at, reverse=True)


def _chain_conflict(db: Session, record: BackupRecord, deleting_ids: set) -> list[BackupRecord]:
    return [d for d in _dependent_closure(db, record) if d.id not in deleting_ids]


@router.delete("/{backup_id}")
def delete_backup(backup_id: int, with_dependents: bool = False, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup not found")

    # A full / earlier incremental that later incrementals build on can't go
    # alone - they'd become unrestorable. Delete the dependents with it only
    # when explicitly asked to.
    dependents = _dependent_closure(db, record)
    if dependents and not with_dependents:
        raise HTTPException(409, {
            "message": f"Diese Sicherung wird von {len(dependents)} späteren Sicherung(en) der Kette benötigt. "
                       "Zuerst diese löschen oder die Kette ab hier komplett löschen.",
            "dependents": [{"id": d.id, "name": d.name, "created_at": d.created_at.isoformat() + "Z",
                            "backup_level": d.backup_level} for d in dependents],
        })

    # 1. Collect remote-cleanup tasks (all DB queries happen HERE, before deletion)
    all_tasks: list[dict] = []
    members: list[BackupRecord] = []
    for d in dependents:
        all_tasks.append(_snapshot_remote_task(d, db, exclude_id=d.id))
    if record.backup_type == "landscape":
        members = [m for m in _find_member_records(record, db) if not _chain_conflict(db, m, {m.id})]
        for m in members:
            all_tasks.append(_snapshot_remote_task(m, db, exclude_id=m.id))
    all_tasks.append(_snapshot_remote_task(record, db, exclude_id=record.id))

    # 2. Delete local files + DB records immediately (newest dependents first)
    for d in dependents:
        backup_engine.delete_backup(Path(d.path))
        db.delete(d)
    for m in members:
        try:
            backup_engine.delete_backup(Path(m.path))
            db.delete(m)
        except Exception as exc:
            _bg_logger.warning("Lokale Löschung Mitglied %s fehlgeschlagen: %s", m.name, exc)
    backup_engine.delete_backup(Path(record.path))
    db.delete(record)
    db.commit()

    # 3. Remote cleanup in background thread — UI gets immediate response
    def _bg():
        for task in all_tasks:
            errs = _run_remote_cleanup(task)
            for e in errs:
                _bg_logger.warning("Remote-Bereinigung '%s': %s", task["name"], e)

    threading.Thread(target=_bg, daemon=True, name=f"remote-cleanup-{backup_id}").start()
    return {"ok": True}


class LandscapeBackupPayload(BaseModel):
    label: Optional[str] = None
    project_filter: Optional[str] = None
    storage_target_ids: Optional[list[int]] = None
    stream_volumes_target_id: Optional[int] = None
    stop_containers: bool = False
    backup_mode: str = "full"
    synthetic_full_every: int = 7


def _run_landscape_job(job_id: str, label: Optional[str], project_filter: Optional[str],
                        storage_target_ids: Optional[list[int]],
                        stream_volumes_target_id: Optional[int] = None,
                        stop_containers: bool = False, backup_mode: str = "full",
                        synthetic_full_every: int = 0):
    db = SessionLocal()
    try:
        landscape_label = label or "Gesamte Landschaft"
        event_log.log_event("backup", f'Landschafts-Backup "{landscape_label}" gestartet')

        def progress(step, name, total=None):
            job_tracker.update_progress(job_id, step, name, total)

        def on_bytes(n):
            job_tracker.update_bytes(job_id, n)

        def on_upload_bytes(n):
            job_tracker.update_upload_bytes(job_id, n)

        def on_log(msg):
            job_tracker.add_log(job_id, msg)

        stream_target = storage_sync.resolve_stream_target(db, stream_volumes_target_id)
        result = backup_engine.backup_landscape(
            BACKUPS_DIR, project_filter=project_filter, label=label, on_progress=progress,
            stream_target=stream_target, should_cancel=lambda: job_tracker.is_cancel_requested(job_id),
            stop_containers=stop_containers, on_bytes=on_bytes, on_upload_bytes=on_upload_bytes,
            on_log=on_log,
            **({"backup_mode": backup_mode, "synthetic_full_every": synthetic_full_every}
               if backup_mode != "full" else {}),
        )
        def upload_progress(label_, idx, total):
            job_tracker.update_progress(job_id, 1, label_, 1)

        record = backup_records.persist_result(db, result, "landscape", storage_target_ids,
                                               stream_target=stream_target, on_progress=upload_progress)

        if result.cancelled:
            job_tracker.cancel_job(job_id)
            event_log.log_event("backup", f'Landschafts-Backup "{landscape_label}" abgebrochen', level="error")
        else:
            job_tracker.finish_job(job_id, result.ok, result.error, record.id)
            if result.ok:
                event_log.log_event("backup", f'Landschafts-Backup "{landscape_label}" erfolgreich abgeschlossen')
            else:
                event_log.log_event("backup", f'Landschafts-Backup "{landscape_label}" fehlgeschlagen: {result.error}',
                                     level="error")
    except Exception as exc:  # noqa: BLE001
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("backup", f'Landschafts-Backup "{landscape_label}" fehlgeschlagen: {exc}', level="error")
    finally:
        db.close()


@router.post("/landscape")
def backup_landscape_now(payload: LandscapeBackupPayload, user: User = Depends(get_current_user)):
    job = job_tracker.create_job("backup", payload.label or "landscape", total_steps=1)
    thread = threading.Thread(
        target=_run_landscape_job,
        args=(job.id, payload.label, payload.project_filter, payload.storage_target_ids,
              payload.stream_volumes_target_id, payload.stop_containers,
              payload.backup_mode if payload.backup_mode in ("full", "incremental") else "full",
              max(0, payload.synthetic_full_every)),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}


def _record_targets(db: Session, record: BackupRecord) -> list[tuple]:
    """Every storage target that may hold this backup (streaming target first,
    then the upload targets) - missing chain parts are fetched from these."""
    out = []
    ids = []
    if record.streamed_target_id is not None:
        ids.append(record.streamed_target_id)
    ids += [i for i in json.loads(record.synced_target_ids or "[]") if i not in ids]
    for tid in ids:
        t = db.query(StorageTarget).filter(StorageTarget.id == tid, StorageTarget.enabled == True).first()  # noqa: E712
        if t:
            out.append((t.type, t.config_json, t.id))
    return out


class RestorePayload(BaseModel):
    new_name: Optional[str] = None
    start: bool = True
    rename_volumes: bool = True
    volume_base_dir: Optional[str] = None
    overwrite: bool = False


def _run_restore_job(job_id: str, backup_path: str, new_name: Optional[str], start: bool,
                      stream_target: Optional[tuple], rename_volumes: bool = True,
                      volume_base_dir: Optional[str] = None, overwrite: bool = False,
                      fallback_targets: Optional[list] = None):
    label = new_name or Path(backup_path).parent.name
    event_log.log_event("restore", f"Restore von '{label}' gestartet")
    # Während der Wiederherstellung ist der Container weg/gestoppt - NetPulse soll das nicht als Ausfall melden
    np_pause = netpulse.pause([label], "Wiederherstellung")
    try:
        def progress(step, name, total=None):
            job_tracker.update_progress(job_id, step, name, total)

        from app.restore_engine import fetcher_for_targets
        restore_container(Path(backup_path), new_name=new_name, start=start, on_progress=progress,
                           stream_target=stream_target, rename_volumes=rename_volumes,
                           volume_base_dir=volume_base_dir, overwrite=overwrite,
                           fetch_missing=fetcher_for_targets(fallback_targets or []),
                           on_log=lambda m: job_tracker.add_log(job_id, m))
        job_tracker.finish_job(job_id, True)
        event_log.log_event("restore", f"Restore von '{label}' erfolgreich abgeschlossen")
        netpulse.report(label, "ok", kind="restore")
    except Exception as exc:  # noqa: BLE001
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("restore", f"Restore von '{label}' fehlgeschlagen: {exc}", level="error")
        netpulse.report(label, "failed", str(exc), kind="restore")
    finally:
        netpulse.resume(np_pause)


@router.post("/{backup_id}/restore")
def restore_backup(backup_id: int, payload: RestorePayload, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup not found")
    if record.backup_type != "container":
        raise HTTPException(400, "Only single-container backups can be restored directly; "
                                  "restore each member container of a landscape backup individually")

    stream_target = storage_sync.resolve_stream_target(db, record.streamed_target_id)
    job = job_tracker.create_job("restore", record.name, total_steps=1)
    thread = threading.Thread(
        target=_run_restore_job,
        args=(job.id, record.path, payload.new_name, payload.start, stream_target,
              payload.rename_volumes, payload.volume_base_dir, payload.overwrite,
              _record_targets(db, record)),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}


@router.get("/{backup_id}/members")
def landscape_members(backup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """For a landscape backup, resolve each member container name to the
    BackupRecord from the SAME run (not just the most-recent one).
    The landscape directory contains per-member JSON files with the exact
    backup_path, which we use to look up the matching record."""
    import tempfile

    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record or record.backup_type != "landscape":
        raise HTTPException(404, "Landscape backup not found")

    landscape_dir = Path(record.path)
    member_jsons = _read_member_jsons(landscape_dir)

    # For catalog-imported landscapes: local dir is absent but the landscape dir
    # (with per-member JSON files) still exists on the remote target. Download it
    # to a temp dir so we can read the member JSON files directly — this is more
    # reliable than containers_json, which may be empty for older imports.
    tmp_ctx = None
    if not member_jsons and not landscape_dir.exists() and record.streamed_target_id:
        target = db.query(StorageTarget).filter(StorageTarget.id == record.streamed_target_id).first()
        if target:
            tmp_ctx = tempfile.TemporaryDirectory()
            try:
                storage_sync.download_full_backup_from_target(
                    target.type, target.config_json,
                    _relative_key(landscape_dir),
                    Path(tmp_ctx.name),
                )
                member_jsons = _read_member_jsons(Path(tmp_ctx.name))
            except Exception:
                pass  # fall back to containers_json

    result = []
    try:
        if member_jsons:
            # Per-member JSON files with exact backup_path
            for d in member_jsons:
                container_name = d.get("container_name", "")
                backup_path = d.get("backup_path", "")
                candidate = (
                    db.query(BackupRecord)
                    .filter(BackupRecord.path == backup_path, BackupRecord.name == container_name)
                    .first()
                )
                if not candidate and container_name:
                    # Path mismatch fallback (e.g. different BACKUPS_DIR on second instance).
                    # Prefer a record from the same streamed_target_id (same catalog import
                    # batch) over just picking the most-recent one by name.
                    q = db.query(BackupRecord).filter(
                        BackupRecord.name == container_name,
                        BackupRecord.backup_type == "container",
                    )
                    if record.streamed_target_id:
                        candidate = q.filter(
                            BackupRecord.streamed_target_id == record.streamed_target_id
                        ).order_by(BackupRecord.created_at.desc()).first()
                    if not candidate:
                        candidate = q.order_by(BackupRecord.created_at.desc()).first()
                if container_name:
                    result.append({
                        "container_name": container_name,
                        "backup_id": candidate.id if candidate else None,
                        "created_at": candidate.created_at.isoformat() + "Z" if candidate else None,
                        "status": candidate.status if candidate else None,
                        "size_bytes": candidate.size_bytes if candidate else None,
                        "backup_level": (candidate.backup_level or "full") if candidate else None,
                    })
        else:
            # Last-resort fallback: containers_json name list (old backup format or
            # landscapes where member JSON files could not be retrieved from remote)
            members = json.loads(record.containers_json) if record.containers_json else []
            for member_name in members:
                candidate = (
                    db.query(BackupRecord)
                    .filter(BackupRecord.name == member_name, BackupRecord.backup_type == "container")
                    .order_by(BackupRecord.created_at.desc())
                    .first()
                )
                result.append({
                    "container_name": member_name,
                    "backup_id": candidate.id if candidate else None,
                    "created_at": candidate.created_at.isoformat() + "Z" if candidate else None,
                    "status": candidate.status if candidate else None,
                    "size_bytes": candidate.size_bytes if candidate else None,
                })
    finally:
        if tmp_ctx:
            tmp_ctx.cleanup()

    return {"members": result}


@router.get("/{backup_id}/download")
def download_backup(backup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup nicht gefunden")
    path = Path(record.path)
    if not path.exists():
        raise HTTPException(404, "Backup-Verzeichnis nicht gefunden")

    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in record.name)
    archive_name = f"{safe_name}_{path.name}.tar.gz"

    def generate():
        r_fd, w_fd = os.pipe()

        def write_archive():
            try:
                with os.fdopen(w_fd, "wb") as sink:
                    with tarfile.open(fileobj=sink, mode="w|gz") as tar:
                        tar.add(str(path), arcname=path.name)
            except Exception:
                try:
                    os.close(w_fd)
                except OSError:
                    pass

        t = threading.Thread(target=write_archive, daemon=True)
        t.start()
        with os.fdopen(r_fd, "rb") as source:
            while chunk := source.read(65536):
                yield chunk
        t.join()

    return StreamingResponse(
        generate(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{archive_name}"'},
    )


# ---------- Incremental chains: contents, single volume restore, integrity check ----------

def _get_container_record(db: Session, backup_id: int) -> BackupRecord:
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup not found")
    if record.backup_type != "container":
        raise HTTPException(400, "Nur für Container-Sicherungen")
    return record


@router.get("/{backup_id}/contents")
def backup_contents(backup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Volumes and bind mounts of a backup (for the single-volume restore)."""
    from app.restore_engine import list_mounts
    record = _get_container_record(db, backup_id)
    path = Path(record.path)
    if not path.exists():
        return {"volumes": [], "binds": [], "available": False,
                "level": record.backup_level or "full"}
    try:
        data = list_mounts(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Sicherung nicht lesbar: {exc}")
    data["available"] = True
    return data


class MountRestorePayload(BaseModel):
    kind: str  # "volume" | "bind"
    name: str  # volume name | bind destination inside the container
    target: Optional[str] = None  # restore into another volume / host directory
    replace: bool = True  # empty the target first (exact point-in-time state)


def _run_mount_restore_job(job_id: str, backup_path: str, payload: dict, stream_target, targets):
    from app.restore_engine import fetcher_for_targets, restore_mount
    label = f"{payload['name']} aus {Path(backup_path).parent.name}"
    event_log.log_event("restore", f"Wiederherstellung von {label} gestartet")
    try:
        dest = restore_mount(
            Path(backup_path), payload["kind"], payload["name"], target=payload.get("target") or None,
            replace=payload.get("replace", True), stream_target=stream_target,
            fetch_missing=fetcher_for_targets(targets),
            on_progress=lambda step, name, total=None: job_tracker.update_progress(job_id, step, name, total),
            on_log=lambda m: job_tracker.add_log(job_id, m))
        job_tracker.add_log(job_id, f"Wiederhergestellt nach: {dest}")
        job_tracker.finish_job(job_id, True)
        event_log.log_event("restore", f"Wiederherstellung von {label} nach {dest} erfolgreich")
    except Exception as exc:  # noqa: BLE001
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("restore", f"Wiederherstellung von {label} fehlgeschlagen: {exc}", level="error")


@router.post("/{backup_id}/restore-mount")
def restore_single_mount(backup_id: int, payload: MountRestorePayload, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    record = _get_container_record(db, backup_id)
    if payload.kind not in ("volume", "bind"):
        raise HTTPException(400, "kind muss 'volume' oder 'bind' sein")
    stream_target = storage_sync.resolve_stream_target(db, record.streamed_target_id)
    job = job_tracker.create_job("restore", f"{record.name}: {payload.name}", total_steps=3)
    threading.Thread(target=_run_mount_restore_job,
                     args=(job.id, record.path, payload.model_dump(), stream_target, _record_targets(db, record)),
                     daemon=True).start()
    return {"job_id": job.id}


def _verify_one(job_id: str, backup_path: str, targets: list, restic_info: Optional[tuple]) -> dict:
    from app.restore_engine import fetcher_for_targets
    if restic_info:
        return _verify_restic(*restic_info)
    return chain.verify(Path(backup_path), fetch_missing=fetcher_for_targets(targets),
                        on_log=lambda m: job_tracker.add_log(job_id, m))


def _run_verify_job(job_id: str, label: str, specs: list):
    """specs: [(member_label, backup_path, targets, restic_info)] - one entry for
    a container backup, one per member for a group backup."""
    try:
        all_ok, errors, files, archives, deps = True, [], 0, 0, 0
        for i, (part_label, backup_path, targets, restic_info) in enumerate(specs, 1):
            job_tracker.update_progress(job_id, i, part_label, len(specs))
            try:
                report = _verify_one(job_id, backup_path, targets, restic_info)
            except Exception as exc:  # noqa: BLE001
                report = {"ok": False, "errors": [str(exc)]}
            prefix = f"{part_label}: " if len(specs) > 1 else ""
            for err in report["errors"][:50]:
                job_tracker.add_log(job_id, f"FEHLER {prefix}{err}")
            if len(specs) > 1:
                job_tracker.add_log(job_id, f"{part_label}: {'in Ordnung' if report['ok'] else 'FEHLER'}")
            all_ok = all_ok and report["ok"]
            errors += [prefix + e for e in report["errors"]]
            files += report.get("checked_files", 0)
            archives += report.get("checked_archives", 0)
            deps += len(report.get("dependencies") or [])
        summary = (f"Integritätsprüfung {label}: {'in Ordnung' if all_ok else 'FEHLER'} - "
                   + (f"{len(specs)} Container, " if len(specs) > 1 else "")
                   + f"{files} Dateien, {archives} Archive, {deps} Kettenteile")
        job_tracker.add_log(job_id, summary)
        job_tracker.finish_job(job_id, all_ok, None if all_ok else "; ".join(errors[:3]))
        event_log.log_event("backup", summary, level="info" if all_ok else "error")
    except Exception as exc:  # noqa: BLE001
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("backup", f"Integritätsprüfung {label} fehlgeschlagen: {exc}", level="error")


def _verify_restic(target: tuple, container_name: str, snapshot_ids: dict) -> dict:
    target_type, config_json, _tid = target
    repo_url, env, conf = restic_engine.repo_url_and_env(target_type, json.loads(config_json or "{}"),
                                                         container_name)
    try:
        restic_engine.check_repo(repo_url, env, restic_engine.get_password())
        missing = restic_engine.missing_snapshots(repo_url, env, restic_engine.get_password(),
                                                  list(snapshot_ids.values()))
    finally:
        if conf:
            restic_engine.rclone_backends.discard_conf(conf)
    errors = [f"Snapshot {sid[:8]} fehlt im Repository" for sid in missing]
    return {"ok": not errors, "errors": errors, "checked_archives": len(snapshot_ids), "checked_files": 0,
            "dependencies": []}


def _verify_spec(db: Session, record: BackupRecord) -> tuple:
    """(label, path, targets, restic_info) for one container backup; fetches a
    catalog-imported folder from its storage target first."""
    targets = _record_targets(db, record)
    restic_info = None
    path = Path(record.path)
    if not path.exists() and not targets:
        raise HTTPException(400, f"Sicherung {record.name} ist weder lokal noch auf einem Speicherziel vorhanden")
    if not path.exists():
        from app.restore_engine import fetcher_for_targets
        try:
            fetcher_for_targets(targets)(_relative_key(path), path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Sicherung {record.name} konnte nicht geladen werden: {exc}")
    meta = chain.read_meta(path)
    if meta.get("backup_engine") == "restic" and record.streamed_target_id is not None:
        stream_target = storage_sync.resolve_stream_target(db, record.streamed_target_id)
        if not stream_target:
            raise HTTPException(400, "Das Restic-Speicherziel ist nicht mehr verfügbar")
        restic_info = (stream_target, meta.get("container_name") or record.name,
                       meta.get("restic_snapshot_ids") or {})
    return record.name, record.path, targets, restic_info


@router.post("/{backup_id}/verify")
def verify_backup(backup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Integrity check: chain complete, checksums intact, every file's content
    reproducible from the chain (restic: `restic check` + snapshots present).
    For a group backup every member container is checked."""
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup not found")
    if record.backup_type == "landscape":
        members = _find_member_records(record, db)
        if not members:
            raise HTTPException(400, "Zu dieser Gruppensicherung wurden keine Container-Sicherungen gefunden")
        specs = [_verify_spec(db, m) for m in members]
    elif record.backup_type == "container":
        specs = [_verify_spec(db, record)]
    else:
        raise HTTPException(400, "Für diese Sicherung nicht verfügbar")
    label = f"{record.name} ({Path(record.path).name})"
    job = job_tracker.create_job("verify", label, total_steps=len(specs))
    threading.Thread(target=_run_verify_job, args=(job.id, label, specs), daemon=True).start()
    return {"job_id": job.id}


# ---------- Complete export / import (app/export_import.py) ----------

def _export_item(db: Session, record: BackupRecord) -> dict:
    from app.restore_engine import fetcher_for_targets
    targets = _record_targets(db, record)
    stream_target = storage_sync.resolve_stream_target(db, record.streamed_target_id) or (targets[0] if targets else None)
    return {"record_path": record.path, "stream_target": stream_target,
            "fetch_missing": fetcher_for_targets(targets)}


def _run_export_job(job_id: str, items: list, landscape: Optional[dict], label: str, filename: str):
    from app import export_import
    out = export_import.exports_dir() / f"{job_id}.tar"
    try:
        export_import.cleanup_old_exports()
        export_import.build_export(
            items, out, landscape=landscape, on_log=lambda m: job_tracker.add_log(job_id, m),
            on_progress=lambda step, name, total: job_tracker.update_progress(job_id, step, name, total))
        (export_import.exports_dir() / f"{job_id}.name").write_text(filename)
        job = job_tracker.get_job(job_id)
        if job:
            job.download_url = f"/api/backups/exports/{job_id}"
        job_tracker.finish_job(job_id, True)
        event_log.log_event("backup", f"Export {label} erstellt ({out.stat().st_size} Bytes)")
    except Exception as exc:  # noqa: BLE001
        out.unlink(missing_ok=True)
        _bg_logger.exception("Export %s fehlgeschlagen", label)
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("backup", f"Export {label} fehlgeschlagen: {exc}", level="error")


@router.post("/{backup_id}/export")
def export_backup(backup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Builds a complete, self-contained export file of this backup point
    (all data incl. image - also for chains, restic and group backups)."""
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup nicht gefunden")
    landscape = None
    if record.backup_type == "landscape":
        members = _find_member_records(record, db)
        if not members:
            raise HTTPException(400, "Zu dieser Gruppensicherung wurden keine Container-Sicherungen gefunden")
        items = [_export_item(db, m) for m in members]
        landscape = {"path": record.path, "name": record.name}
    else:
        items = [_export_item(db, record)]
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in record.name)
    filename = f"{safe}_{Path(record.path).name}.dbm-export.tar"
    label = f"{record.name} ({Path(record.path).name})"
    job = job_tracker.create_job("export", f"Export {label}", total_steps=len(items) + 2)
    threading.Thread(target=_run_export_job, args=(job.id, items, landscape, label, filename), daemon=True).start()
    return {"job_id": job.id}


@router.get("/exports/{job_id}")
def download_export(job_id: str, user: User = Depends(get_current_user)):
    from app import export_import
    if not job_id.replace("-", "").isalnum():
        raise HTTPException(400, "Ungültige Export-ID")
    f = export_import.exports_dir() / f"{job_id}.tar"
    if not f.exists():
        raise HTTPException(404, "Export nicht (mehr) vorhanden - bitte neu erstellen")
    name_file = export_import.exports_dir() / f"{job_id}.name"
    filename = name_file.read_text() if name_file.exists() else f.name
    return FileResponse(f, filename=filename, media_type="application/x-tar")


def _run_import_job(job_id: str, upload: Path):
    from app import export_import
    db = SessionLocal()
    # Step 1 ("Hochladen") already happened in the browser.
    phases = {1: "Entpacken", 2: "In den Katalog übernehmen"}

    def on_step(n: int, name: str, total_bytes: int):
        job_tracker.update_progress(job_id, n + 1, phases.get(n, name))
        job_tracker.set_byte_total(job_id, total_bytes)

    try:
        created = export_import.import_export(
            upload, db, on_log=lambda m: job_tracker.add_log(job_id, m), on_step=on_step,
            on_bytes=lambda n: job_tracker.update_bytes(job_id, n),
            should_cancel=lambda: job_tracker.is_cancel_requested(job_id))
        main = created[-1]
        job_tracker.finish_job(job_id, True, result_backup_id=main.id)
        event_log.log_event("backup", f"Sicherung {main.name} importiert ({len(created)} Einträge)")
    except export_import.ImportCancelled:
        db.rollback()
        job_tracker.cancel_job(job_id)
        event_log.log_event("backup", "Import abgebrochen")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("backup", f"Import fehlgeschlagen: {exc}", level="error")
    finally:
        db.close()
        upload.unlink(missing_ok=True)


@router.post("/import")
async def import_backup(request: Request, user: User = Depends(get_current_user)):
    """Body = an export file (raw bytes, no multipart: files of many GB are
    streamed straight to disk next to the backups)."""
    import uuid as _uuid
    from app import export_import
    upload = export_import.exports_dir() / f"upload-{_uuid.uuid4().hex}.tar"
    size = 0
    try:
        with open(upload, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
        if not size:
            raise HTTPException(400, "Leere Datei")
        try:
            export_import.read_manifest(upload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))
    except BaseException:
        upload.unlink(missing_ok=True)
        raise
    from urllib.parse import unquote
    name = unquote(request.headers.get("x-file-name", ""))[:120].strip()
    job = job_tracker.create_job("import", f"Import {name}" if name else "Import einer Sicherung", total_steps=3)
    job_tracker.update_progress(job.id, 2, "Entpacken")
    threading.Thread(target=_run_import_job, args=(job.id, upload), daemon=True).start()
    return {"job_id": job.id}
