import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import backup_engine, event_log, job_tracker, storage_sync
from app.auth import get_current_user
from app.config import BACKUPS_DIR
from app.database import SessionLocal, get_db
from app.docker_client import get_client, is_available
from app.models import BackupRecord, User
import json

router = APIRouter(prefix="/api/containers", tags=["containers"])


@router.get("")
def list_containers(user: User = Depends(get_current_user)):
    ok, error = is_available()
    if not ok:
        raise HTTPException(503, f"Docker not reachable: {error}")

    client = get_client()
    containers = client.containers.list(all=True)
    projects = {}
    result = []
    for c in containers:
        project = c.labels.get("com.docker.compose.project")
        item = {
            "id": c.id,
            "name": c.name,
            "image": c.image.tags[0] if c.image.tags else c.image.short_id,
            "status": c.status,
            "project": project,
        }
        result.append(item)
        if project:
            projects.setdefault(project, []).append(c.name)
    return {"containers": result, "projects": projects}


def _run_container_backup_job(job_id: str, container_name: str, storage_target_ids: Optional[list[int]],
                               stream_volumes_target_id: Optional[int] = None,
                               stop_container: bool = False):
    db = SessionLocal()
    try:
        event_log.log_event("backup", f'Backup für Container "{container_name}" gestartet')

        def progress(step, name, total=None):
            job_tracker.update_progress(job_id, step, name, total)

        def on_bytes(n):
            job_tracker.update_bytes(job_id, n)

        def on_upload_bytes(n):
            job_tracker.update_upload_bytes(job_id, n)

        def on_log(msg):
            job_tracker.add_log(job_id, msg)

        stream_target = storage_sync.resolve_stream_target(db, stream_volumes_target_id)
        result = backup_engine.backup_container(
            container_name, BACKUPS_DIR, on_progress=progress, stream_target=stream_target,
            should_cancel=lambda: job_tracker.is_cancel_requested(job_id),
            stop_container=stop_container, on_bytes=on_bytes, on_upload_bytes=on_upload_bytes,
            on_log=on_log,
        )
        record = BackupRecord(
            backup_type="container",
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

        if result.ok:
            def upload_progress(label, idx, total):
                job_tracker.update_progress(job_id, 1, label, 1)

            sync_results, _ = storage_sync.sync_backup_with_members(
                result.path, [], storage_target_ids, stream_target=stream_target, on_progress=upload_progress)
            record.synced_target_ids = json.dumps([r["target_id"] for r in sync_results if r["ok"]])
            db.commit()

        if result.cancelled:
            job_tracker.cancel_job(job_id)
            event_log.log_event("backup", f'Backup für Container "{container_name}" abgebrochen', level="error")
        else:
            job_tracker.finish_job(job_id, result.ok, result.error, record.id)
            if result.ok:
                event_log.log_event("backup", f'Backup für Container "{container_name}" erfolgreich abgeschlossen')
            else:
                event_log.log_event("backup", f'Backup für Container "{container_name}" fehlgeschlagen: {result.error}',
                                     level="error")
    except Exception as exc:  # noqa: BLE001
        job_tracker.finish_job(job_id, False, str(exc))
        event_log.log_event("backup", f'Backup für Container "{container_name}" fehlgeschlagen: {exc}', level="error")
    finally:
        db.close()


class ContainerBackupPayload(BaseModel):
    storage_target_ids: Optional[list[int]] = None
    stream_volumes_target_id: Optional[int] = None
    stop_container: bool = False


@router.post("/{name}/backup")
def backup_container_now(name: str, payload: ContainerBackupPayload = ContainerBackupPayload(),
                          user: User = Depends(get_current_user)):
    job = job_tracker.create_job("backup", name, total_steps=1)
    thread = threading.Thread(
        target=_run_container_backup_job,
        args=(job.id, name, payload.storage_target_ids, payload.stream_volumes_target_id, payload.stop_container),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}
