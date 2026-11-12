"""Persistent event log for the log overview page - unlike job_tracker (in-memory,
lost on restart), these rows survive app restarts so past activity stays visible."""
import datetime
from app.clock import utcnow
import json
import logging
import os

from app.database import SessionLocal
from app.models import AppSetting, LogEntry

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
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        # Full tracebacks only go to the console (stdout handler) - in the DB they would
        # cost several KB per entry. Keep just the final "ExceptionType: message" line.
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            message = f"{message} - {type(exc).__name__}: {exc}"
        log_event(category, message, level)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# Defaults used until something is saved in the UI (then app_settings wins).
# Keep log entries for 90 days / at most 50,000 entries; 0 = no limit.
DEFAULT_LOG_RETENTION_DAYS = _env_int("DBM_LOG_RETENTION_DAYS", 90)
DEFAULT_LOG_MAX_ENTRIES = _env_int("DBM_LOG_MAX_ENTRIES", 50000)
# Longer messages (e.g. raw rclone/restic output) are cut off.
MAX_MESSAGE_LENGTH = 4000

SETTINGS_KEY = "log_settings"


def get_settings() -> dict:
    """Log retention settings, persisted in app_settings so they survive restarts/updates."""
    out = {"retention_days": DEFAULT_LOG_RETENTION_DAYS, "max_entries": DEFAULT_LOG_MAX_ENTRIES}
    db = SessionLocal()
    try:
        row = db.get(AppSetting, SETTINGS_KEY)
        stored = json.loads(row.value) if row and row.value else {}
        for key in out:
            if isinstance(stored.get(key), int) and stored[key] >= 0:
                out[key] = stored[key]
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    return out


def save_settings(retention_days: int, max_entries: int) -> dict:
    clean = {"retention_days": max(0, int(retention_days)), "max_entries": max(0, int(max_entries))}
    db = SessionLocal()
    try:
        row = db.get(AppSetting, SETTINGS_KEY)
        if row is None:
            row = AppSetting(key=SETTINGS_KEY)
            db.add(row)
        row.value = json.dumps(clean)
        db.commit()
    finally:
        db.close()
    return clean


def log_event(category: str, message: str, level: str = "info") -> None:
    message = str(message)
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[:MAX_MESSAGE_LENGTH] + " … (gekürzt)"
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


def purge_excess_entries(max_entries: int) -> int:
    """Keep only the newest max_entries log entries. Returns number of deleted rows."""
    if max_entries <= 0:
        return 0
    db = SessionLocal()
    try:
        boundary = (db.query(LogEntry.id).order_by(LogEntry.id.desc())
                    .offset(max_entries - 1).limit(1).scalar())
        if boundary is None:
            return 0
        deleted = db.query(LogEntry).filter(LogEntry.id < boundary).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception:  # noqa: BLE001
        logger.exception("Failed to trim log entries")
        return 0
    finally:
        db.close()


def run_rotation(full_vacuum: bool = False) -> dict:
    """Apply both retention limits, then give the freed space back to the file system."""
    from app.database import compact_db

    settings = get_settings()
    deleted = purge_old_entries(settings["retention_days"])
    deleted += purge_excess_entries(settings["max_entries"])
    sizes = compact_db(full=full_vacuum)
    return {"deleted": deleted, **settings, **sizes}


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
