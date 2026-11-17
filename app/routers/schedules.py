import json
from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import scheduler as scheduler_module
from app.auth import get_current_user
from app.database import get_db
from app.models import Schedule, User

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class SchedulePayload(BaseModel):
    name: str
    target_type: str  # "container" | "landscape"
    target_ref: Optional[str] = None
    project_filter: Optional[str] = None  # landscape only: limit to one Compose project
    name_contains: Optional[str] = None  # landscape only: alternative to project_filter, matches container names by substring
    exclude_names: Optional[str] = None  # landscape only: comma-separated substrings of container names to skip
    cron_expression: str
    retention_count: int = 7
    retention_days: int = 0
    retention_chains: int = 0  # keep the newest N chains completely (replaces retention_count)
    keep_daily: int = 0  # plus the newest backup of each of the last N days ...
    keep_weekly: int = 0  # ... ISO weeks ...
    keep_monthly: int = 0  # ... and months
    storage_target_ids: list[int] = []
    stream_volumes_target_id: Optional[int] = None  # if set, volumes stream directly here instead of being written locally
    backup_mode: str = "full"  # "full" | "incremental" (initial full, then incrementals + synthetic full)
    synthetic_full_every: int = 7  # incremental: combine into a synthetic full after this many incrementals (0 = never)
    keep_merged_incrementals: bool = True  # after a synthetic full: keep merged incrementals (retention decides) or delete them
    stop_containers: bool = False  # opt-in: stop container(s) before archiving, restart afterwards, for an application-consistent backup
    enabled: bool = True


def _validate_chain(payload: "SchedulePayload"):
    if payload.backup_mode not in ("full", "incremental"):
        raise HTTPException(400, "backup_mode muss 'full' oder 'incremental' sein")
    if payload.synthetic_full_every < 0 or payload.synthetic_full_every > 365:
        raise HTTPException(400, "Synthetic Full: 0 bis 365 Inkremente")
    for field in ("retention_count", "retention_days", "retention_chains", "keep_daily", "keep_weekly", "keep_monthly"):
        if not 0 <= getattr(payload, field) <= 100000:
            raise HTTPException(400, f"{field}: Wert muss 0 oder größer sein")


def _validate_cron(expr: str):
    try:
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid cron expression: {exc}")


@router.get("")
def list_schedules(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Schedule).order_by(Schedule.created_at.desc()).all()
    return {"schedules": [
        {
            "id": s.id, "name": s.name, "target_type": s.target_type, "target_ref": s.target_ref,
            "project_filter": s.project_filter, "name_contains": s.name_contains, "exclude_names": s.exclude_names,
            "cron_expression": s.cron_expression, "retention_count": s.retention_count,
            "retention_days": s.retention_days,
            "retention_chains": s.retention_chains or 0, "keep_daily": s.keep_daily or 0,
            "keep_weekly": s.keep_weekly or 0, "keep_monthly": s.keep_monthly or 0,
            "storage_target_ids": json.loads(s.storage_target_ids or "[]"),
            "stream_volumes_target_id": s.stream_volumes_target_id,
            "stop_containers": s.stop_containers, "enabled": s.enabled,
            "backup_mode": s.backup_mode or "full", "synthetic_full_every": s.synthetic_full_every,
            "keep_merged_incrementals": s.keep_merged_incrementals,
            "last_run_at": s.last_run_at.isoformat() + "Z" if s.last_run_at else None,
            "last_status": s.last_status,
        } for s in rows
    ]}


@router.post("")
def create_schedule(payload: SchedulePayload, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _validate_cron(payload.cron_expression)
    _validate_chain(payload)
    if payload.target_type == "container" and not payload.target_ref:
        raise HTTPException(400, "target_ref (container name) is required for target_type=container")

    data = payload.model_dump()
    target_ids = data.pop("storage_target_ids")
    sched = Schedule(storage_target_ids=json.dumps(target_ids), **data)
    db.add(sched)
    db.commit()
    db.refresh(sched)
    if sched.enabled:
        scheduler_module.add_or_update_job(sched)
    return {"id": sched.id}


@router.put("/{schedule_id}")
def update_schedule(schedule_id: int, payload: SchedulePayload, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    sched = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not sched:
        raise HTTPException(404, "Schedule not found")
    _validate_cron(payload.cron_expression)
    _validate_chain(payload)

    data = payload.model_dump()
    target_ids = data.pop("storage_target_ids")
    for k, v in data.items():
        setattr(sched, k, v)
    sched.storage_target_ids = json.dumps(target_ids)
    db.commit()

    scheduler_module.remove_job(schedule_id)
    if sched.enabled:
        scheduler_module.add_or_update_job(sched)
    return {"ok": True}


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sched = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not sched:
        raise HTTPException(404, "Schedule not found")
    scheduler_module.remove_job(schedule_id)
    db.delete(sched)
    db.commit()
    return {"ok": True}


@router.post("/{schedule_id}/run-now")
def run_schedule_now(schedule_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sched = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not sched:
        raise HTTPException(404, "Schedule not found")
    import threading
    threading.Thread(target=scheduler_module.run_schedule, args=(schedule_id,), daemon=True).start()
    return {"ok": True}
