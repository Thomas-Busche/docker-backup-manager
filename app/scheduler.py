from __future__ import annotations

import datetime
from app.clock import utcnow
import json
import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import backup_engine, backup_records, event_log, job_tracker, netpulse, restic_engine, storage_sync
from app.config import BACKUPS_DIR, TZ_NAME
from app.database import SessionLocal
from app.models import BackupRecord, Schedule
from app.retention import PolicyVersion, versions_to_keep

CHAIN_LEVELS = ("full", "incremental", "synthetic_full")

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
                                                          on_upload_bytes=on_upload_bytes, on_log=on_log,
                                                          **_chain_kwargs(sched))
            else:
                result = backup_engine.backup_landscape(BACKUPS_DIR, project_filter=sched.project_filter,
                                                          name_contains=sched.name_contains,
                                                          exclude_names=sched.exclude_names,
                                                          label=sched.name, on_progress=progress,
                                                          stream_target=stream_target, should_cancel=should_cancel,
                                                          stop_containers=sched.stop_containers, on_bytes=on_bytes,
                                                          on_upload_bytes=on_upload_bytes, on_log=on_log,
                                                          **_chain_kwargs(sched))

            def upload_progress(label, idx, total):
                progress(1, label, 1)

            record = backup_records.persist_result(
                db, result, sched.target_type, json.loads(sched.storage_target_ids or "[]"),
                stream_target=stream_target, on_progress=upload_progress,
                keep_merged_incrementals=_keep_merged(sched), delete_record=_delete_record_for_retention)

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

    keys = _chain_keys(db, records)
    versions = [PolicyVersion(id=r.id, created_at=r.created_at, chain_key=keys[r.id]) for r in records]
    keep = versions_to_keep(versions, sched.retention_count or 0, sched.retention_days or 0,
                            getattr(sched, "retention_chains", 0) or 0, getattr(sched, "keep_daily", 0) or 0,
                            getattr(sched, "keep_weekly", 0) or 0, getattr(sched, "keep_monthly", 0) or 0)
    prune_ids = {r.id for r in records} - keep
    prune_ids = _protect_chain_dependencies(db, records, prune_ids)
    if not prune_ids:
        return
    # Newest first: an incremental is deleted before the older chain members it needs
    for r in sorted(records, key=lambda r: r.created_at, reverse=True):
        if r.id not in prune_ids:
            continue
        try:
            # Landscape-Records: zuerst Member-Container-Records löschen
            if r.backup_type == "landscape" and Path(r.path).exists():
                from app.routers.backups import _read_member_jsons  # also reads encrypted *.json.enc
                for data in _read_member_jsons(Path(r.path)):
                    try:
                        member_name = data.get("container_name", "")
                        # The synthetic full of the same run first (it's newer), then the member itself
                        for member_path in (data.get("synthetic_path", ""), data.get("backup_path", "")):
                            if not member_path:
                                continue
                            member = (
                                db.query(BackupRecord)
                                .filter(BackupRecord.path == member_path, BackupRecord.name == member_name)
                                .first()
                            )
                            if not member:
                                continue
                            if backup_records.dependents(db, member):
                                # A kept incremental still builds on this member - it
                                # stays as a restore point of its own container.
                                logger.info("Retention: %s bleibt (Basis einer Sicherungskette)", member.path)
                                continue
                            _delete_record_for_retention(member, db)
                    except Exception:  # noqa: BLE001
                        logger.exception("Retention: Fehler beim Löschen von Landscape-Mitglied %s",
                                         data.get("container_name"))

            _delete_record_for_retention(r, db)
            # Commit per record: if a later deletion in this batch fails, already
            # processed ones must not be rolled back or re-attempted next run,
            # since their local files are already gone from disk at this point.
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Retention: failed to prune backup %s (id=%s)", r.path, r.id)
            db.rollback()


def _chain_keys(db, records: list) -> dict:
    """Which chain each version belongs to (for "keep N chains").
    Container backups: their chain id; a self-contained one (full mode,
    restic) is a chain of its own. Group backups: a new chain begins with a
    run whose members were backed up in full, or after a run that created a
    synthetic full."""
    keys: dict = {}
    current = None
    synth_before = False
    for r in sorted(records, key=lambda r: r.created_at):
        if r.backup_type != "landscape":
            keys[r.id] = r.chain_id if r.chain_id and r.backup_level in CHAIN_LEVELS else f"single-{r.id}"
            continue
        from app.routers.backups import _read_member_jsons
        members = _read_member_jsons(Path(r.path)) if Path(r.path).exists() else []
        levels = []
        for d in members:
            m = db.query(BackupRecord).filter(BackupRecord.path == d.get("backup_path", "")).first()
            levels.append((m.backup_level if m else None) or d.get("backup_level") or "full")
        starts = (current is None or synth_before or not levels
                  or any(lv not in ("incremental",) for lv in levels))
        if starts:
            current = f"l-{r.id}"
        keys[r.id] = current
        synth_before = any(d.get("synthetic_path") for d in members)
    return keys


def _chain_kwargs(sched: Schedule) -> dict:
    if (getattr(sched, "backup_mode", None) or "full") != "incremental":
        return {}
    return {
        "backup_mode": (getattr(sched, "backup_mode", None) or "full"),
        "synthetic_full_every": int(getattr(sched, "synthetic_full_every", 0) or 0),
    }


def _keep_merged(sched: Schedule) -> bool:
    value = getattr(sched, "keep_merged_incrementals", True)
    return True if value is None else bool(value)


def _protect_chain_dependencies(db, records: list, prune_ids: set) -> set:
    """Never prune a backup that a kept backup of the same or another chain
    still needs for its restore (base full / earlier incrementals). Applied
    transitively: what a protected backup needs is protected too."""
    if not prune_ids:
        return prune_ids
    by_path = {r.path: r for r in db.query(BackupRecord).all()}
    kept = [r for r in by_path.values() if r.id not in prune_ids]
    required: set = set()
    todo = list(backup_records.required_paths(kept))
    while todo:
        path = todo.pop()
        if path in required:
            continue
        required.add(path)
        dep = by_path.get(path)
        if dep is not None:
            todo.extend(backup_records.depends_paths(dep))
    protected = {r.id for r in records if r.id in prune_ids and r.path in required}
    if protected:
        logger.info("Retention: %d Sicherung(en) bleiben als Basis behaltener Inkremente", len(protected))
    return prune_ids - protected


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
    # Sunday night: full VACUUM (rewrites/defragments the file), otherwise the cheap incremental one
    full = datetime.datetime.now(datetime.timezone.utc).weekday() == 6
    result = event_log.run_rotation(full_vacuum=full)
    if result["deleted"] or result["size_after"] < result["size_before"]:
        logger.info("Log-Rotation: %d alte Einträge gelöscht, Datenbank %.1f MB → %.1f MB",
                    result["deleted"], result["size_before"] / 1e6, result["size_after"] / 1e6)


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
