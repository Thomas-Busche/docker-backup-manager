import functools
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

APP_VERSION = "1.5.5"
GITHUB_REPO = "Sirbuschi2003/docker-backup-manager"

import pytz

logger = logging.getLogger(__name__)

BASE_DIR = Path(os.environ.get("DBM_BASE_DIR", "/data")).resolve()
BACKUPS_DIR = Path(os.environ.get("DBM_BACKUPS_DIR", str(BASE_DIR / "backups"))).resolve()
DB_PATH = Path(os.environ.get("DBM_DB_PATH", str(BASE_DIR / "dbm.sqlite3"))).resolve()

BASE_DIR.mkdir(parents=True, exist_ok=True)


def _load_or_create_secret_key() -> str:
    env_key = os.environ.get("DBM_SECRET_KEY")
    if env_key:
        return env_key
    # No key configured (common for a first-time/non-technical install): generate one
    # and persist it next to the database so sessions survive restarts, instead of
    # falling back to a hardcoded value that would be identical across every install.
    key_path = BASE_DIR / ".secret_key"
    if key_path.exists():
        return key_path.read_text().strip()
    key = secrets.token_hex(32)
    key_path.write_text(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _load_or_create_secret_key()
SESSION_COOKIE_NAME = "dbm_session"
SESSION_MAX_AGE = int(os.environ.get("DBM_SESSION_MAX_AGE", str(60 * 60 * 24 * 7)))
SESSION_HTTPS_ONLY = os.environ.get("DBM_SESSION_HTTPS_ONLY", "false").lower() in ("1", "true", "yes")

LOGIN_MAX_ATTEMPTS = int(os.environ.get("DBM_LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("DBM_LOGIN_LOCKOUT_SECONDS", str(5 * 60)))

DOCKER_HELPER_IMAGE = os.environ.get("DBM_HELPER_IMAGE", "alpine:3.20")

# Timezone schedules are evaluated in (IANA name, e.g. "Europe/Berlin"). Defaults
# to UTC because that's the only thing guaranteed to be correct out of the box -
# the container has no way to know the operator's local timezone on its own, and
# getting this wrong silently shifts every scheduled backup by the UTC offset.
# An invalid name must not crash the scheduler at startup (it's built from this
# value at import time) - fall back to UTC and keep the bad value around so the
# UI can point out the typo instead of the whole app failing to boot.
_tz_env = os.environ.get("DBM_TZ", "UTC")
try:
    pytz.timezone(_tz_env)
    TZ_NAME = _tz_env
    TZ_ERROR = None
except pytz.UnknownTimeZoneError:
    TZ_NAME = "UTC"
    TZ_ERROR = f"DBM_TZ=\"{_tz_env}\" ist keine gültige Zeitzone (IANA-Name, z. B. \"Europe/Berlin\") - UTC wird verwendet."

# OAuth-based storage targets (Google Drive / OneDrive). PUBLIC_URL is the
# address the browser uses to reach this app (e.g. "http://192.168.1.10:8420")
# - needed to build the OAuth redirect URI, since the container can't know
# its own externally-reachable address on its own.
PUBLIC_URL = os.environ.get("DBM_PUBLIC_URL", "").rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("DBM_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("DBM_GOOGLE_CLIENT_SECRET", "")
MS_CLIENT_ID = os.environ.get("DBM_MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("DBM_MS_CLIENT_SECRET", "")
MS_TENANT = os.environ.get("DBM_MS_TENANT", "common")

DEFAULT_RETENTION_COUNT = int(os.environ.get("DBM_DEFAULT_RETENTION_COUNT", "7"))
DEFAULT_RETENTION_DAYS = int(os.environ.get("DBM_DEFAULT_RETENTION_DAYS", "0"))  # 0 = disabled

BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def _detect_host_base_dir() -> Optional[str]:
    """Return the host-side path that corresponds to BASE_DIR inside this container.

    When DBM runs inside Docker and spawns helper containers via the Docker socket,
    volume-mount paths must be HOST paths (as seen by the Docker daemon), not the
    container-internal paths.  This function resolves that mapping via:

    1. Explicit env var DBM_HOST_DATA_DIR  (fastest, for advanced users)
    2. Auto-detection: inspect our own container via the Docker socket and find the
       bind-mount whose Destination matches BASE_DIR.
    3. Fall back to None  →  caller uses the container path as-is (works when the
       host and container paths coincidentally match, or when not running in Docker).
    """
    explicit = os.environ.get("DBM_HOST_DATA_DIR", "").strip()
    if explicit:
        return explicit

    hostname = os.environ.get("HOSTNAME", "").strip()
    if not hostname:
        return None

    try:
        from app.docker_client import get_client  # local import to avoid circular
        client = get_client()
        self_container = client.containers.get(hostname)
        for mount in self_container.attrs.get("Mounts", []):
            dest = mount.get("Destination", "")
            if not dest:
                continue
            if Path(dest).resolve() == BASE_DIR:
                src = mount.get("Source", "")
                if src:
                    logger.debug("Host-Pfad für %s erkannt: %s", BASE_DIR, src)
                    return src
    except Exception as exc:
        logger.debug("Host-Pfad-Erkennung fehlgeschlagen: %s", exc)

    return None


def container_path_to_host(path: Path) -> str:
    """Translate an in-container path to its host-visible equivalent.

    Required when passing paths as Docker volume sources to containers spawned via
    the Docker socket — the daemon interprets them as HOST paths, not container paths.
    """
    host_base = _detect_host_base_dir()
    if host_base:
        try:
            rel = path.resolve().relative_to(BASE_DIR)
            return str(Path(host_base) / rel)
        except ValueError:
            pass
    return str(path)
