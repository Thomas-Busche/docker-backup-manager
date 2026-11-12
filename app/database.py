import logging
import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DB_PATH

logger = logging.getLogger("dbm.database")

# WAL lets the many small log writes from background threads run alongside UI reads.
# WAL does not work on network file systems (NFS/SMB) - set DBM_DB_WAL=0 if /data lives on one.
_USE_WAL = os.environ.get("DBM_DB_WAL", "1").strip().lower() not in ("0", "false", "no", "off")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    try:
        if _USE_WAL:
            cur.execute("PRAGMA journal_mode=WAL")
            # NORMAL is safe in WAL mode (no corruption, at most the last commit is lost on power loss)
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
    finally:
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _add_missing_columns():
    # Lightweight migration: create_all() only creates missing *tables*, so columns added
    # to existing models in later versions need to be added by hand for already-deployed DBs.
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        if "users" in tables:
            existing = {col["name"] for col in inspector.get_columns("users")}
            if "failed_attempts" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0"))
            if "locked_until" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN locked_until DATETIME"))
            if "is_admin" not in existing:
                # Promote existing users to admin so no one loses access on upgrade
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 1"))
                conn.execute(text("UPDATE users SET is_admin = 1"))

        if "schedules" in tables:
            existing = {col["name"] for col in inspector.get_columns("schedules")}
            if "storage_target_ids" not in existing:
                conn.execute(text("ALTER TABLE schedules ADD COLUMN storage_target_ids TEXT NOT NULL DEFAULT '[]'"))
            if "project_filter" not in existing:
                conn.execute(text("ALTER TABLE schedules ADD COLUMN project_filter TEXT"))
            if "stream_volumes_target_id" not in existing:
                conn.execute(text("ALTER TABLE schedules ADD COLUMN stream_volumes_target_id INTEGER"))
            if "name_contains" not in existing:
                conn.execute(text("ALTER TABLE schedules ADD COLUMN name_contains TEXT"))
            if "stop_containers" not in existing:
                conn.execute(text("ALTER TABLE schedules ADD COLUMN stop_containers BOOLEAN NOT NULL DEFAULT 0"))
            if "exclude_names" not in existing:
                conn.execute(text("ALTER TABLE schedules ADD COLUMN exclude_names TEXT"))

        if "backup_records" in tables:
            existing = {col["name"] for col in inspector.get_columns("backup_records")}
            if "synced_target_ids" not in existing:
                conn.execute(text("ALTER TABLE backup_records ADD COLUMN synced_target_ids TEXT"))
            if "streamed_target_id" not in existing:
                conn.execute(text("ALTER TABLE backup_records ADD COLUMN streamed_target_id INTEGER"))


def _pragma(conn, name: str):
    return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def _ensure_incremental_auto_vacuum():
    """Switches the DB to auto_vacuum=INCREMENTAL so pages freed by deletes can be handed
    back to the file system. For an existing DB the mode only takes effect after a full
    VACUUM, which is done once here (on a fresh DB it is a no-op)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if _pragma(conn, "auto_vacuum") == 2:  # 2 = INCREMENTAL
            return
        conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
        conn.exec_driver_sql("VACUUM")
    # Pooled connections opened before the VACUUM still report the old mode - drop them
    engine.dispose()
    logger.info("Datenbank auf automatische Platzfreigabe umgestellt (einmalig)")


def db_file_size() -> int:
    """Size of the SQLite DB on disk incl. WAL/SHM side files, in bytes."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(f"{DB_PATH}{suffix}")
        except OSError:
            pass
    return total


def compact_db(full: bool = False) -> dict:
    """Gives free pages back to the file system and empties the WAL file.
    full=True runs a complete VACUUM (rewrites the file, also defragments) -
    otherwise only an incremental vacuum, which is cheap enough to run every night."""
    before = db_file_size()
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Raw DB-API cursor: incremental_vacuum frees pages step by step, so the
            # statement has to be stepped to the end (fetchall), which SQLAlchemy's
            # result wrapper refuses for statements without result columns.
            cur = conn.connection.dbapi_connection.cursor()
            try:
                if full:
                    cur.execute("VACUUM")
                else:
                    cur.execute("PRAGMA incremental_vacuum")
                    cur.fetchall()
                if _USE_WAL:
                    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    cur.fetchall()
            finally:
                cur.close()
    except Exception:  # noqa: BLE001
        logger.exception("Datenbank-Komprimierung fehlgeschlagen")
    after = db_file_size()
    return {"size_before": before, "size_after": after}


def init_db():
    from app import models  # noqa: F401  (ensure models are registered)
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    try:
        _ensure_incremental_auto_vacuum()
    except Exception:  # noqa: BLE001
        logger.exception("Umstellung auf auto_vacuum fehlgeschlagen - Datenbank wird trotzdem verwendet")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
