from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import event_log
from app.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def list_logs(limit: int = 200, since_id: int = 0, user: User = Depends(get_current_user)):
    entries = event_log.list_entries(limit=min(limit, 2000), since_id=since_id)
    return {
        "entries": [
            {
                "id": e.id,
                "created_at": e.created_at.isoformat() + "Z",
                "level": e.level,
                "category": e.category,
                "message": e.message,
            } for e in entries
        ],
        "total": event_log.count_entries(),
    }


@router.get("/settings")
def get_log_settings(user: User = Depends(get_current_user)):
    from app.database import db_file_size
    return {**event_log.get_settings(), "db_size_bytes": db_file_size(), "total": event_log.count_entries()}


class LogSettingsPayload(BaseModel):
    retention_days: int
    max_entries: Optional[int] = None


@router.put("/settings")
def update_log_settings(payload: LogSettingsPayload, user: User = Depends(get_current_user)):
    if payload.retention_days < 0:
        raise HTTPException(400, "retention_days muss 0 (unbegrenzt) oder größer sein")
    if payload.max_entries is not None and payload.max_entries < 0:
        raise HTTPException(400, "max_entries muss 0 (unbegrenzt) oder größer sein")
    max_entries = payload.max_entries if payload.max_entries is not None else event_log.get_settings()["max_entries"]
    saved = event_log.save_settings(payload.retention_days, max_entries)
    return {"ok": True, **saved}


@router.post("/purge")
def purge_logs(user: User = Depends(get_current_user)):
    """Apply the retention limits now and compact the database file (full VACUUM)."""
    return event_log.run_rotation(full_vacuum=True)
