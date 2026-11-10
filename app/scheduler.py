from __future__ import annotations

import datetime
from app.clock import utcnow
import json
import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import backup_engine, event_log, job_tracker, netpulse, restic_engine, storage_sync
from app.config import BACKUPS_DIR, TZ_NAME
from app.database import SessionLocal
from app.models import BackupRecord, Schedule
from app.retention import VersionInfo, versions_to_prune

logger = logging.getLogger("dbm.scheduler")

# misfire_grace_time=1: missed fire times older than 1 s are skipped instead of
# immediately executed. Without this, restarting the app causes every schedule
# whose window fell inside the downtime to fire right away — which is almost
# always wrong for backup jobs that are meant to run at specific quiet times.
scheduler = BackgroundScheduler(
    timezone=TZ_NAME,
    job_defaults={"misfire_grace_time": 1, "coalesce": True},
)


def _job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


def run_schedule(schedule_id: int):
    db = SessionLocal()
    try:
        sched = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not sched or not sched.enabled:
            return

        job = job_tracker.create_job("backup", sched.name, total_steps=1)
        event_log.log_event("schedule", f'Zeitplan "{sched.name}" gestartet')

        def progress(step, name, total=None):
            job_tracker.update_progress(job.id, step, name, total)

        def on_bytes(n):
            job_tracker.update_bytes(job.id, n)

        def on_upload_bytes(n):
            job_tracker.update_upload_bytes(job.id, n)

        def on_log(msg):
            job_tracker.add_log(job.id, msg)

        try:
            stream_target = storage_sync.resolve_stream_target(db, sched.stream_volumes_target_id)
            should_cancel = lambda: job_tracker.is_cancel_requested(job.id)  # noqa: E731
            if sched.target_type == "container":
                result = backup_engine.backup_container(sched.target_ref, BACKUPS_DIR, on_progress=progress,
                                                          stream_target=stream_target, should_cancel=should_cancel,
                                                          stop_container=sched.stop_containers, on_bytes=on_bytes,
                                                          on_upload_bytes=on_upload_bytes, on_log=on_log)
            else:
                result = backup_engine.backup_landscape(BACKUPS_DIR, project_filter=sched.project_filter,
                                                          name_contains=sched.name_contains,
                                                          exclude_names=sched.exclude_names,
                                                          label=sched.name, on_progress=progress,
                                                          stream_target=stream_target, should_cancel=should_cancel,
                                                          stop_containers=sched.stop_containers, on_bytes=on_bytes,
                                                          on_upload_bytes=on_upload_bytes, on_log=on_log)

            record = BackupRecord(
                backup_type=sched.target_type,
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

            # Each member container backup of a landscape run lives in its own
            # directory with real data (image, volumes) - without its own
            # record it'd be invisible in the UI, never deletable, and never
            # subject to retention, even though it still takes up disk space.
            for member in result.member_results:
                db.add(BackupRecord(
                    backup_type="container", name=member.name, path=str(member.path),
                    status="ok" if member.ok else "failed", error=member.error,
                    size_bytes=member.size_bytes, containers_json=json.dumps([member.name]),
                    streamed_target_id=member.streamed_target_id,
                ))
            db.commit()

            if result.ok:
                def upload_progress(label, idx, total):
                    progress(1, label, 1)

                target_ids = json.loads(sched.storage_target_ids or "[]")
                sync_results = storage_sync.sync_to_selected_targets(result.path, target_ids, on_progress=upload_progress)

                # When volumes are streamed via restic to a stream_target, the local
                # backup_dir only holds meta.json / container.json / networks.json — these
                # are never part of the restic repo, so the stream_target ends up without
                # a meta.json marker. Without that marker the catalog-import scan finds
                # nothing on that target. Fix: also sync the metadata dir(s) to the
                # stream_target so it always has a meta.json for catalog discovery.
                if stream_target:
                    st_type, st_config_json, st_id = stream_target
                    # When stream_target is not also a regular storage target, the
                    # result dir was never uploaded there — do it now for catalog discovery.
                    if st_id not in target_ids:
                        try:
                            storage_sync.sync_to_target(result.path, st_type, st_config_json)
                        except Exception:
                            logger.exception("Meta-sync result dir to stream target failed for %s", result.path)
                    # Member dirs must ALWAYS go to the stream target — sync_to_selected_targets
                    # only uploads result.path (the landscape dir), never the individual member
                    # backup dirs. Without their meta.json the catalog scan finds 0 containers.
                    for m in result.member_results:
                        if m.ok and m.path and m.path.exists():
                            try:
                                storage_sync.sync_to_target(m.path, st_type, st_config_json)
                            except Exception:
                                logger.exception("Meta-sync member dir to stream target failed for %s", m.path)

                record.synced_target_ids = json.dumps([r["target_id"] for r in sync_results if r["ok"]])
                db.commit()

            if result.cancelled:
                job_tracker.cancel_job(job.id)
                event_log.log_event("schedule", f'Zeitplan "{sched.name}" abgebrochen', level="error")
            else:
                job_tracker.finish_job(job.id, result.ok, result.error, record.id)
                if result.ok:
                    event_log.log_event("schedule", f'Zeitplan "{sched.name}" erfolgreich abgeschlossen')
                else:
                    event_log.log_event("schedule", f'Zeitplan "{sched.name}" fehlgeschlagen: {result.error}',
                                         level="error")

            _apply_retention(db, sched)

            sched.last_run_at = utcnow()
            sched.last_status = "cancelled" if result.cancelled else ("ok" if result.ok else "failed")
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduled backup failed")
            job_tracker.finish_job(job.id, False, str(exc))
            event_log.log_event("schedule", f'Zeitplan "{sched.name}" fehlgeschlagen: {exc}', level="error")
            sched.last_run_at = utcnow()
            sched.last_status = "failed"
            db.commit()
    finally:
        db.close()


def _delete_record_for_retention(r: BackupRecord, db) -> None:
    """Löscht einen einzelnen Backup-Record inklusive lokaler Dateien und Remote-Daten.
    Wird von _apply_retention aufgerufen — importiert lazy um zirkuläre Imports zu vermeiden."""
    from app.models import StorageTarget
    from app.routers.backups import _cleanup_remote_for_record

    _cleanup_remote_for_record(r, db)
    backup_engine.delete_backup(r.path)
    db.delete(r)


def _apply_retention(db, sched: Schedule):
    records = db.query(BackupRecord).filter(BackupRecord.name == sched.name).all()
    if sched.target_type == "container" and sched.target_ref:
        records = db.query(BackupRecord).filter(BackupRecord.name == sched.target_ref).all()

    versions = [VersionInfo(id=r.id, created_at=r.created_at) for r in records]
    prune_ids = {v.id for v in versions_to_prune(versions, sched.retention_count, sched.retention_days)}
    if not prune_ids:
        return
    for r in records:
        if r.id not in prune_ids:
            continue
        try:
            # Landscape-Records: zuerst Member-Container-Records löschen
            if r.backup_type == "landscape" and Path(r.path).exists():
                for jf in sorted(Path(r.path).glob("*.json")):
                    try:
                        data = json.loads(jf.read_text())
                        member_name = data.get("container_name", "")
                        member_path = data.get("backup_path", "")
                        if not member_path:
                            continue
                        member = (
                            db.query(BackupRecord)
                            .filter(BackupRecord.path == member_path, BackupRecord.name == member_name)
                            .first()
                        )
                        if not member:
                            continue
                        _delete_record_for_retention(member, db)
                    except Exception:  # noqa: BLE001
                        logger.exception("Retention: Fehler beim Löschen von Landscape-Mitglied %s", jf.stem)

            _delete_record_for_retention(r, db)
            # Commit per record: if a later deletion in this batch fails, already
            # processed ones must not be rolled back or re-attempted next run,
            # since their local files are already gone from disk at this point.
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Retention: failed to prune backup %s (id=%s)", r.path, r.id)
            db.rollback()


def add_or_update_job(sched: Schedule):
    scheduler.add_job(
        run_schedule,
        trigger=CronTrigger.from_crontab(sched.cron_expression, timezone=TZ_NAME),
        args=[sched.id],
        id=_job_id(sched.id),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1,
    )


def remove_job(schedule_id: int):
    try:
        scheduler.remove_job(_job_id(schedule_id))
    except Exception:  # noqa: BLE001
        pass


def load_all_schedules():
    db = SessionLocal()
    try:
        for sched in db.query(Schedule).filter(Schedule.enabled == True).all():  # noqa: E712
            try:
                add_or_update_job(sched)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to schedule job %s", sched.id)
    finally:
        db.close()


def _purge_old_logs():
    deleted = event_log.purge_old_entries(event_log.DEFAULT_LOG_RETENTION_DAYS)
    if deleted:
        logger.info("Log-Rotation: %d alte Einträge gelöscht", deleted)


def start():
    if not scheduler.running:
        scheduler.start()
    load_all_schedules()
    # Daily log rotation at 03:30
    scheduler.add_job(
        _purge_old_logs, CronTrigger(hour=3, minute=30, timezone=TZ_NAME),
        id="log-rotation", replace_existing=True,
    )
    # NetPulse-Kopplung: Containerliste stündlich und kurz nach dem Start übertragen (macht nichts, wenn aus)
    scheduler.add_job(
        netpulse.push_inventory, CronTrigger(minute=17, timezone=TZ_NAME),
        id="netpulse-inventory", replace_existing=True,
    )
    scheduler.add_job(
        netpulse.push_inventory, "date",
        run_date=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30),
        id="netpulse-inventory-startup", replace_existing=True,
    )


def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
