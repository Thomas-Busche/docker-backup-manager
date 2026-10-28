import json
import os
import tarfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import backup_engine, event_log, job_tracker, netpulse, restic_engine, storage_sync
from app.auth import get_current_user
from app.config import BACKUPS_DIR
from app.database import SessionLocal, get_db
from app.models import BackupRecord, StorageTarget, User
from app.restore_engine import restore_container
from app.storage_sync import _relative_key

router = APIRouter(prefix="/api/backups", tags=["backups"])


@router.get("")
def list_backups(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
        }
        if r.backup_type == "landscape":
            member_names, member_size = _landscape_member_info(r, records_by_path, records_latest_by_name)
            entry["member_names"] = member_names
            entry["member_size_bytes"] = member_size
        grouped.setdefault(r.name, []).append(entry)
    return {"groups": grouped}


def _read_member_jsons(landscape_dir: Path) -> list[dict]:
    """Return parsed member JSON dicts from a landscape dir, skipping meta.json
    and any file that lacks a backup_path (= not a real member file)."""
    result = []
    if not landscape_dir.exists():
        return result
    for jf in sorted(landscape_dir.glob("*.json")):
        if jf.name == "meta.json":
            continue
        try:
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
                                Path(smb_conf_path).unlink(missing_ok=True)
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

    for backup_id in payload.ids:
        record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
        if not record:
            continue
        members: list[BackupRecord] = []
        if record.backup_type == "landscape":
            members = _find_member_records(record, db)
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
        deleted += 1

    db.commit()

    def _bg():
        for task in all_tasks:
            for e in _run_remote_cleanup(task):
                _bg_logger.warning("Remote-Bereinigung Bulk '%s': %s", task["name"], e)

    threading.Thread(target=_bg, daemon=True, name="remote-cleanup-bulk").start()
    return {"ok": True, "deleted": deleted}


@router.delete("/{backup_id}")
def delete_backup(backup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(404, "Backup not found")

    # 1. Collect remote-cleanup tasks (all DB queries happen HERE, before deletion)
    all_tasks: list[dict] = []
    members: list[BackupRecord] = []
    if record.backup_type == "landscape":
        members = _find_member_records(record, db)
        for m in members:
            all_tasks.append(_snapshot_remote_task(m, db, exclude_id=m.id))
    all_tasks.append(_snapshot_remote_task(record, db, exclude_id=record.id))

    # 2. Delete local files + DB records immediately
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


def _run_landscape_job(job_id: str, label: Optional[str], project_filter: Optional[str],
                        storage_target_ids: Optional[list[int]],
                        stream_volumes_target_id: Optional[int] = None,
                        stop_containers: bool = False):
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
        )
        record = BackupRecord(
            backup_type="landscape",
            name=result.name,
            path=str(result.path),
            status="ok" if result.ok else "failed",
            error=result.error,
            size_bytes=result.size_bytes,
            containers_json=json.dumps(result.containers),
            streamed_target_id=result.streamed_target_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        # Each member container backup lives in its own directory with real
        # data (image, volumes) - without its own record it'd be invisible in
        # the UI, never deletable, and never subject to retention, even
        # though it still takes up real disk space.
        for member in result.member_results:
            db.add(BackupRecord(
                backup_type="container", name=member.name, path=str(member.path),
                status="ok" if member.ok else "failed", error=member.error,
                size_bytes=member.size_bytes, containers_json=json.dumps([member.name]),
                streamed_target_id=member.streamed_target_id,
            ))
        db.commit()

        if result.ok:
            def upload_progress(label_, idx, total):
                job_tracker.update_progress(job_id, 1, label_, 1)

            if storage_target_ids is None:
                sync_results = storage_sync.sync_to_all_targets(result.path, on_progress=upload_progress)
            else:
                sync_results = storage_sync.sync_to_selected_targets(result.path, storage_target_ids, on_progress=upload_progress)
            record.synced_target_ids = json.dumps([r["target_id"] for r in sync_results if r["ok"]])
            db.commit()

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
              payload.stream_volumes_target_id, payload.stop_containers),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}


class RestorePayload(BaseModel):
    new_name: Optional[str] = None
    start: bool = True
    rename_volumes: bool = True
    volume_base_dir: Optional[str] = None
    overwrite: bool = False


def _run_restore_job(job_id: str, backup_path: str, new_name: Optional[str], start: bool,
                      stream_target: Optional[tuple], rename_volumes: bool = True,
                      volume_base_dir: Optional[str] = None, overwrite: bool = False):
    label = new_name or Path(backup_path).parent.name
    event_log.log_event("restore", f"Restore von '{label}' gestartet")
    # Während der Wiederherstellung ist der Container weg/gestoppt - NetPulse soll das nicht als Ausfall melden
    np_pause = netpulse.pause([label], "Wiederherstellung")
    try:
        def progress(step, name, total=None):
            job_tracker.update_progress(job_id, step, name, total)

        restore_container(Path(backup_path), new_name=new_name, start=start, on_progress=progress,
                           stream_target=stream_target, rename_volumes=rename_volumes,
                           volume_base_dir=volume_base_dir, overwrite=overwrite)
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
              payload.rename_volumes, payload.volume_base_dir, payload.overwrite),
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
