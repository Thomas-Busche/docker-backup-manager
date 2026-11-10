"""Persistent event log for the log overview page - unlike job_tracker (in-memory,
lost on restart), these rows survive app restarts so past activity stays visible."""
import datetime
from app.clock import utcnow
import logging
import os

from app.database import SessionLocal
from app.models import LogEntry

logger = logging.getLogger("dbm.event_log")

# Maps the trailing part of a dbm.* logger name to the category stored in DB.
# None = skip (no DB entry for this logger).
_LOGGER_CATEGORY = {
    "remote_cleanup": "cleanup",
    "restic_engine":  "restic",
    "backup_engine":  "backup",
    "scheduler":      "scheduler",
    "space":          "space",
    "event_log":      None,   # skip — avoid recursion
}

_LEVEL_MAP = {
    logging.WARNING:  "warning",
    logging.ERROR:    "error",
    logging.CRITICAL: "error",
}


class DBLogHandler(logging.Handler):
    """Writes dbm.* Python log records into LogEntry so they show up in the
    web UI alongside the high-level schedule/backup events."""

    def emit(self, record: logging.LogRecord) -> None:
        # Derive category from the last segment of the logger name
        parts = record.name.split(".")
        raw = parts[-1] if len(parts) > 1 else record.name
        category = _LOGGER_CATEGORY.get(raw, raw)
        if category is None:
            return
        level = _LEVEL_MAP.get(record.levelno, "info")
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        log_event(category, message, level)

# Default: keep log entries for 90 days; 0 = keep forever
DEFAULT_LOG_RETENTION_DAYS = int(os.environ.get("DBM_LOG_RETENTION_DAYS", "90"))


def log_event(category: str, message: str, level: str = "info") -> None:
    db = SessionLocal()
    try:
        db.add(LogEntry(category=category, message=message, level=level))
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write log entry: [%s] %s", category, message)
    finally:
        db.close()


def purge_old_entries(retention_days: int) -> int:
    """Delete log entries older than retention_days. Returns number of deleted rows."""
    if retention_days <= 0:
        return 0
    cutoff = utcnow() - datetime.timedelta(days=retention_days)
    db = SessionLocal()
    try:
        deleted = db.query(LogEntry).filter(LogEntry.created_at < cutoff).delete()
        db.commit()
        return deleted
    except Exception:  # noqa: BLE001
        logger.exception("Failed to purge old log entries")
        return 0
    finally:
        db.close()


def list_entries(limit: int = 200, since_id: int = 0) -> list[LogEntry]:
    db = SessionLocal()
    try:
        q = db.query(LogEntry)
        if since_id:
            q = q.filter(LogEntry.id > since_id)
        return q.order_by(LogEntry.created_at.desc()).limit(limit).all()
    finally:
        db.close()


def count_entries() -> int:
    db = SessionLocal()
    try:
        return db.query(LogEntry).count()
    finally:
        db.close()
