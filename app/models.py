from app.clock import utcnow

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)


class BackupRecord(Base):
    __tablename__ = "backup_records"

    id = Column(Integer, primary_key=True)
    backup_type = Column(String(16), nullable=False)  # "container" | "landscape"
    name = Column(String(255), nullable=False)  # container name or landscape label
    path = Column(String(1024), nullable=False)  # absolute path to this version's folder
    status = Column(String(16), nullable=False, default="ok")  # ok | failed
    error = Column(Text, nullable=True)
    size_bytes = Column(Integer, default=0)
    containers_json = Column(Text, nullable=True)  # for landscape: JSON list of member container names
    source_image = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    synced_target_ids = Column(Text, nullable=True)  # JSON list of StorageTarget ids this version was uploaded to
    streamed_target_id = Column(Integer, nullable=True)  # set if volumes were streamed directly to this StorageTarget instead of being written locally


class StorageTarget(Base):
    __tablename__ = "storage_targets"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    type = Column(String(16), nullable=False)  # "local_path" | "s3" | "rclone"
    config_json = Column(Text, nullable=False, default="{}")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    last_sync_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String(16), nullable=True)
    last_sync_error = Column(Text, nullable=True)


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    target_type = Column(String(16), nullable=False)  # "container" | "landscape"
    target_ref = Column(String(255), nullable=True)  # container name, empty for landscape
    project_filter = Column(String(255), nullable=True)  # landscape only: limit to one Compose project (e.g. multi-container apps like Immich/Nextcloud); empty = entire landscape
    name_contains = Column(String(255), nullable=True)  # landscape only: alternative to project_filter for apps without a Compose project label (e.g. Nextcloud AIO) - matches container names by substring
    exclude_names = Column(String, nullable=True)  # landscape only: comma-separated container name substrings to skip (e.g. "borgbackup,aio-watchtower")
    cron_expression = Column(String(64), nullable=False)  # standard 5-field cron
    retention_count = Column(Integer, default=7)
    retention_days = Column(Integer, default=0)
    storage_target_ids = Column(Text, nullable=False, default="[]")  # JSON list of StorageTarget ids to sync to
    stream_volumes_target_id = Column(Integer, nullable=True)  # if set, volumes are streamed directly here instead of being written locally first (bypasses at-rest encryption for volume data)
    stop_containers = Column(Boolean, nullable=False, default=False)  # opt-in: stop the container(s) before archiving volumes/binds and restart afterwards, for an application-consistent (rather than crash-consistent) backup
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(16), nullable=True)


class LogEntry(Base):
    __tablename__ = "log_entries"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    level = Column(String(16), nullable=False, default="info")  # info | error
    category = Column(String(16), nullable=False)  # backup | restore | schedule
    message = Column(Text, nullable=False)


class AppSetting(Base):
    """Einfache Schlüssel/Wert-Einstellungen (JSON), z. B. die NetPulse-Kopplung."""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
