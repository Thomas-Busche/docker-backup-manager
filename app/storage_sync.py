"""
Uploads/replicates a finished backup to configured off-site storage targets.

Supported target types:
  - local_path: any filesystem path already reachable from inside the
    container (e.g. an NFS share, or an SMB share mounted at the host/OS
    level and bind-mounted in). No credentials are entered in the app here -
    authentication happened when the share was mounted outside the app.
  - smb: a *real* in-app SMB/CIFS connection (server, share, username,
    password) using a pure-Python SMB2/3 client (`smbprotocol`) - no host
    mount, no privileged container needed. This is the option to use when you
    want to type a username/password directly into the app. Share names can
    be listed rather than typed via list_smb_shares() (uses the lighter
    `pysmb` library, which - unlike smbprotocol - exposes share enumeration).
  - s3: any S3-compatible object storage (AWS S3, MinIO, Wasabi, Backblaze B2,
    Ceph RGW, ...) via boto3, using access key/secret + endpoint URL.
  - rclone: shells out to the bundled `rclone` binary for every other backend
    rclone supports out of the box - most notably SFTP, WebDAV, B2, etc. The
    admin configures the remote once (rclone.conf, mounted as a secret or
    built via `rclone config`) and just references its name + a destination
    path here.
  - sftp / webdav / ftp / azure_blob / dropbox / rclone_cloud: also run on
    the bundled rclone, but are configured entirely in the web UI - a temp
    rclone.conf is generated per operation (app/rclone_backends.py) and the
    regular rclone code paths below are reused.
  - google_drive / onedrive: OAuth-based targets set up entirely from the web
    UI ("Mit Google/Microsoft anmelden") - no rclone.conf, no terminal. See
    app/oauth_storage.py for the actual OAuth + upload implementation; this
    module just dispatches to it.
"""
from __future__ import annotations

import datetime
from app.clock import utcnow
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Optional

from app import rclone_backends
from app.config import BACKUPS_DIR

logger = logging.getLogger("dbm.storage_sync")

# smbclient.open_file() falls back to Python io's default buffer (8KB) unless
# told otherwise - each write()/read() then becomes its own synchronous SMB2
# request/response round trip, so throughput on even a fast LAN collapses to
# a fraction of the link's real bandwidth (observed: ~14MB/s on gigabit).
# A much larger buffer means far fewer, far larger requests.
_SMB_IO_BUFFER_SIZE = 1024 * 1024

RCLONE_CONFIG_PATH = "/data/rclone.conf"

# Upper bound for one rclone transfer (whole backup copy / single volume
# download). Large backups over a slow uplink easily need more than an hour -
# the old 1h limit killed perfectly healthy multi-GB uploads.
TRANSFER_TIMEOUT_SECONDS = int(os.environ.get("DBM_TRANSFER_TIMEOUT", str(12 * 3600)))

# Flags for every rclone call that moves data: retry transient errors on the
# whole operation and on individual low-level requests.
_RCLONE_TRANSFER_FLAGS = ["--retries", "3", "--low-level-retries", "10"]


def _rclone_path(config: dict, relative: str = "") -> str:
    """Builds "remote:path" for an rclone target. With an empty remote_path
    the result must stay relative ("remote:key", not "remote:/key") - for SFTP
    and local remotes a leading slash means the filesystem root instead of the
    remote's home/base directory. Matches how restic_engine builds repo URLs."""
    remote = config["remote"]
    parts = [p.strip("/") for p in (config.get("remote_path", ""), relative.replace("\\", "/")) if p and p.strip("/")]
    return f"{remote}:" + "/".join(parts)


def _s3_client(config: dict):
    """boto3 S3 client with explicit timeouts and adaptive retries, so a flaky
    connection to S3/MinIO/Wasabi/B2 doesn't fail a backup on the first hiccup
    and a dead endpoint doesn't hang forever."""
    import boto3  # imported lazily so boto3 is only required if S3 targets are used
    from botocore.config import Config

    session = boto3.session.Session(
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        region_name=config.get("region") or None,
    )
    addressing = config.get("addressing_style") or "auto"
    if addressing not in ("auto", "path", "virtual"):
        addressing = "auto"
    return session.client(
        "s3",
        endpoint_url=config.get("endpoint_url") or None,
        config=Config(connect_timeout=30, read_timeout=300,
                      retries={"max_attempts": 10, "mode": "adaptive"},
                      # MinIO and other self-hosted servers reached by IP need
                      # path-style URLs (http://host:9000/bucket/key).
                      s3={"addressing_style": addressing},
                      # Since boto3 1.36 every upload carries a CRC checksum
                      # header/trailer by default. Several S3-compatible
                      # services (older MinIO/Ceph, some hosted providers)
                      # reject that - only send checksums the API requires.
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


_S3_STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER_IR")


def _s3_extra_args(config: dict) -> Optional[dict]:
    """Optional storage class for uploads (AWS). Classes needing a restore
    request before download (GLACIER, DEEP_ARCHIVE) are deliberately not
    allowed - a restore would fail until AWS thawed the objects."""
    storage_class = (config.get("storage_class") or "").strip().upper()
    if not storage_class or storage_class == "STANDARD":
        return None
    if storage_class not in _S3_STORAGE_CLASSES:
        raise ValueError(f"Nicht unterstützte S3-Speicherklasse: {storage_class}")
    return {"StorageClass": storage_class}


def _s3_dir_prefix(config: dict, relative_key: str) -> str:
    """Key prefix for everything *inside* a backup directory. The trailing
    slash matters: without it, the prefix for ".../v1" also matches ".../v10"
    and deleting or downloading one version would touch the other."""
    return "/".join(filter(None, [config.get("prefix", "").strip("/"), relative_key.strip("/")])) + "/"


def _relative_key(backup_path: Path) -> str:
    try:
        return str(Path(backup_path).resolve().relative_to(BACKUPS_DIR.resolve()))
    except ValueError:
        return Path(backup_path).name


def sync_local_path(backup_path: Path, config: dict) -> None:
    dest_root = Path(config["path"])
    dest = dest_root / _relative_key(backup_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(backup_path, dest)


def sync_s3(backup_path: Path, config: dict) -> None:
    s3 = _s3_client(config)
    bucket = config["bucket"]
    prefix = config.get("prefix", "").strip("/")
    key_root = _relative_key(backup_path)
    extra_args = _s3_extra_args(config)

    for file_path in Path(backup_path).rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(backup_path)
        key = "/".join(filter(None, [prefix, key_root, str(rel).replace("\\", "/")]))
        s3.upload_file(str(file_path), bucket, key, ExtraArgs=extra_args)


def _smb_remote_root(config: dict, key_root: str) -> str:
    """Pure helper (no network) so the path-building logic is unit-testable."""
    server = config["server"]
    share = config["share"]
    base = config.get("base_path", "").strip("/\\").replace("/", "\\")
    root = f"\\\\{server}\\{share}"
    if base:
        root += "\\" + base
    if key_root and key_root != ".":
        root += "\\" + key_root.replace("/", "\\")
    return root


def _smb_register_session(config: dict):
    import smbclient

    username = config["username"]
    domain = config.get("domain", "").strip()
    if domain:
        username = f"{domain}\\{username}"
    smbclient.register_session(
        config["server"],
        username=username,
        password=config["password"],
        port=int(config.get("port") or 445),
    )


# Share types the SMB protocol itself reports; used to keep the actual data
# shares in the "list available shares" helper below and hide plumbing.
_HIDDEN_SHARE_TYPES = {"IPC", "PRINTER"}


def _filter_browsable_shares(raw_shares: list) -> list[str]:
    """Pure helper (no network) so the filtering logic is unit-testable.
    Takes pysmb SharedDevice-like objects (must have .name and .isSpecial /
    .type) and returns plain share names a user would actually want to pick,
    i.e. not admin/print/IPC shares such as ADMIN$, C$, IPC$, print$."""
    names = []
    for share in raw_shares:
        if getattr(share, "isSpecial", False):
            continue
        if share.name.upper().endswith("$"):
            continue
        names.append(share.name)
    return names


def list_smb_shares(config: dict) -> list[str]:
    """Connects to the given SMB server and returns the names of shares a
    normal user could back up to, so the UI can offer a picker instead of
    requiring the exact share name to be typed in."""
    from smb.SMBConnection import SMBConnection  # imported lazily, only needed for this lookup

    username = config["username"]
    domain = config.get("domain", "").strip()
    server = config["server"]
    port = int(config.get("port") or 445)

    conn = SMBConnection(username, config["password"], "docker-backup-manager", server,
                          domain=domain, use_ntlm_v2=True, is_direct_tcp=True)
    try:
        if not conn.connect(server, port, timeout=15):
            raise RuntimeError("SMB authentication failed")
        return _filter_browsable_shares(conn.listShares())
    finally:
        conn.close()


def sync_smb(backup_path: Path, config: dict) -> None:
    import smbclient  # imported lazily so smbprotocol is only required if SMB targets are used

    _smb_register_session(config)
    key_root = _relative_key(backup_path)
    remote_root = _smb_remote_root(config, key_root)
    smbclient.makedirs(remote_root, exist_ok=True)

    for file_path in Path(backup_path).rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(backup_path)
        remote_path = remote_root + "\\" + rel.as_posix().replace("/", "\\")
        remote_dir = remote_path.rsplit("\\", 1)[0]
        smbclient.makedirs(remote_dir, exist_ok=True)
        with open(file_path, "rb") as src, smbclient.open_file(remote_path, mode="wb", buffering=_SMB_IO_BUFFER_SIZE) as dst:
            shutil.copyfileobj(src, dst, length=_SMB_IO_BUFFER_SIZE)


def sync_rclone(backup_path: Path, config: dict, conf_path: Optional[str] = None) -> None:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    dest = _rclone_path(config, _relative_key(backup_path))
    cmd = ["rclone", "copy", str(backup_path), dest, "--config", conf_path, *_RCLONE_TRANSFER_FLAGS]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TRANSFER_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone failed: {proc.stderr.strip() or proc.stdout.strip()}")


def sync_google_drive(backup_path: Path, config: dict) -> None:
    from app import oauth_storage
    oauth_storage.sync_google_drive(backup_path, config)


def sync_onedrive(backup_path: Path, config: dict) -> None:
    from app import oauth_storage
    oauth_storage.sync_onedrive(backup_path, config)


SYNC_HANDLERS = {
    "local_path": sync_local_path,
    "smb": sync_smb,
    "s3": sync_s3,
    "rclone": sync_rclone,
    "google_drive": sync_google_drive,
    "onedrive": sync_onedrive,
}


def sync_to_target(backup_path: Path, target_type: str, config_json: str) -> None:
    handler = SYNC_HANDLERS.get(target_type)
    if not handler:
        raise ValueError(f"Unknown storage target type: {target_type}")
    config = json.loads(config_json or "{}")
    handler(Path(backup_path), config)


def _delete_local_path(config: dict, relative_key: str) -> None:
    dest = Path(config["path"]) / relative_key
    if dest.exists():
        shutil.rmtree(dest)
    # Walk up and remove empty parent directories
    base = Path(config["path"])
    parent = dest.parent
    while parent != base and parent.exists():
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
            else:
                break
        except OSError:
            break


def _delete_s3(config: dict, relative_key: str) -> None:
    s3 = _s3_client(config)
    bucket = config["bucket"]
    prefix = _s3_dir_prefix(config, relative_key)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            errors = resp.get("Errors") or []
            if errors:
                first = errors[0]
                raise RuntimeError(
                    f"S3: {len(errors)} Objekt(e) konnten nicht gelöscht werden "
                    f"({first.get('Key')}: {first.get('Code')} {first.get('Message')})"
                )


def _delete_smb(config: dict, relative_key: str) -> None:
    import smbclient

    _smb_register_session(config)
    remote_root = _smb_remote_root(config, relative_key)
    if not smbclient.path.exists(remote_root):
        return
    for dirpath, _dirnames, filenames in smbclient.walk(remote_root, topdown=False):
        for filename in filenames:
            smbclient.remove(f"{dirpath}\\{filename}")
        smbclient.rmdir(dirpath)
    # Walk up and remove empty parent directories
    parts = relative_key.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, 0, -1):
        parent_key = "/".join(parts[:i])
        parent_root = _smb_remote_root(config, parent_key)
        try:
            if smbclient.path.exists(parent_root) and not smbclient.listdir(parent_root):
                smbclient.rmdir(parent_root)
            else:
                break
        except Exception:
            break


def _delete_rclone(config: dict, relative_key: str, conf_path: Optional[str] = None) -> None:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    if not relative_key.strip("/"):
        raise ValueError("rclone: leerer Pfad - Löschen des Zielordners verweigert")
    dest = _rclone_path(config, relative_key)
    proc = subprocess.run(
        ["rclone", "purge", dest, "--config", conf_path],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0 and "directory not found" not in (proc.stderr or "").lower():
        raise RuntimeError(f"rclone purge failed: {proc.stderr.strip() or proc.stdout.strip()}")
    # Walk up and remove empty parent directories (rclone rmdir is a no-op on non-empty)
    parts = relative_key.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, 0, -1):
        parent_key = "/".join(parts[:i])
        parent_dest = _rclone_path(config, parent_key)
        result = subprocess.run(
            ["rclone", "rmdir", parent_dest, "--config", conf_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            break


def _delete_google_drive(config: dict, relative_key: str) -> None:
    from app import oauth_storage
    oauth_storage.delete_google_drive(config, relative_key)


def _delete_onedrive(config: dict, relative_key: str) -> None:
    from app import oauth_storage
    oauth_storage.delete_onedrive(config, relative_key)


DELETE_HANDLERS = {
    "local_path": _delete_local_path,
    "smb": _delete_smb,
    "s3": _delete_s3,
    "rclone": _delete_rclone,
    "google_drive": _delete_google_drive,
    "onedrive": _delete_onedrive,
}


def delete_from_target(target_type: str, config_json: str, relative_key: str) -> None:
    """Removes a single backup version's uploaded copy from a storage target.
    Best-effort by convention of the caller - a missing remote copy (already
    deleted, sync never completed, ...) should not be treated as fatal by
    handlers where that's distinguishable, but network/auth errors still raise."""
    handler = DELETE_HANDLERS.get(target_type)
    if not handler:
        raise ValueError(f"Unknown storage target type: {target_type}")
    config = json.loads(config_json or "{}")
    handler(config, relative_key)


def try_rmdir_on_target(target_type: str, config_json: str, relative_key: str) -> None:
    """Attempt to remove an empty directory on a storage target. Silent on all errors.
    Used after purge_restic_repo to clean up the now-empty container-level folder."""
    config = json.loads(config_json or "{}")
    try:
        if target_type == "rclone":
            dest = _rclone_path(config, relative_key)
            subprocess.run(
                ["rclone", "rmdir", dest, "--config", RCLONE_CONFIG_PATH],
                capture_output=True, text=True, timeout=30,
            )
        elif target_type == "smb":
            import smbclient
            _smb_register_session(config)
            root = _smb_remote_root(config, relative_key)
            if smbclient.path.exists(root) and not smbclient.listdir(root):
                smbclient.rmdir(root)
        elif target_type == "local_path":
            dest = Path(config["path"]) / relative_key
            if dest.exists() and not any(dest.iterdir()):
                dest.rmdir()
        elif rclone_backends.is_managed(target_type):
            _managed_try_rmdir(target_type, config, relative_key)
    except Exception:
        pass


class _ChunkIteratorReader:
    """Minimal file-like adapter so a plain iterator of bytes chunks (as
    produced by backup_engine.iter_volume_tar_chunks) can be handed to APIs
    that expect a .read(size) method, e.g. boto3's upload_fileobj."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buffer = b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                break
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


def _stream_upload_local_path(config: dict, relative_path: str, chunks) -> None:
    dest = Path(config["path"]) / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in chunks:
            f.write(chunk)


def _stream_upload_s3(config: dict, relative_path: str, chunks) -> None:
    s3 = _s3_client(config)
    key = "/".join(filter(None, [config.get("prefix", "").strip("/"), relative_path]))
    s3.upload_fileobj(_ChunkIteratorReader(chunks), config["bucket"], key, ExtraArgs=_s3_extra_args(config))


def _stream_upload_smb(config: dict, relative_path: str, chunks) -> None:
    import smbclient

    _smb_register_session(config)
    remote_root = _smb_remote_root(config, "")
    remote_path = remote_root + "\\" + relative_path.replace("/", "\\")
    remote_dir = remote_path.rsplit("\\", 1)[0]
    smbclient.makedirs(remote_dir, exist_ok=True)
    with smbclient.open_file(remote_path, mode="wb", buffering=_SMB_IO_BUFFER_SIZE) as dst:
        for chunk in chunks:
            dst.write(chunk)


def _stream_upload_rclone(config: dict, relative_path: str, chunks, conf_path: Optional[str] = None) -> None:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    dest = _rclone_path(config, relative_path)
    proc = subprocess.Popen(
        ["rclone", "rcat", dest, "--config", conf_path, "--low-level-retries", "10"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    # Drain stderr concurrently: if rclone logs more than a pipe buffer's worth
    # while we're still writing stdin, both sides would block forever.
    stderr_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    try:
        try:
            for chunk in chunks:
                proc.stdin.write(chunk)
            proc.stdin.close()
        except BrokenPipeError:
            # rclone exited early (bad remote, auth error, ...) - report its
            # own error message below instead of a bare "Broken pipe".
            pass
        code = proc.wait(timeout=TRANSFER_TIMEOUT_SECONDS)
        reader.join(timeout=10)
        if code != 0:
            stderr = b"".join(stderr_chunks).decode(errors="replace").strip()
            raise RuntimeError(f"rclone rcat failed: {stderr or f'exit code {code}'}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            proc.stdin.close()
        except OSError:  # already closed, or rclone gone with unread data
            pass


STREAM_UPLOAD_HANDLERS = {
    "local_path": _stream_upload_local_path,
    "smb": _stream_upload_smb,
    "s3": _stream_upload_s3,
    "rclone": _stream_upload_rclone,
}


def _delete_partial_local_path(config: dict, relative_path: str) -> None:
    dest = Path(config["path"]) / relative_path
    dest.unlink(missing_ok=True)


def _delete_partial_smb(config: dict, relative_path: str) -> None:
    import smbclient

    _smb_register_session(config)
    remote_root = _smb_remote_root(config, "")
    remote_path = remote_root + "\\" + relative_path.replace("/", "\\")
    if smbclient.path.exists(remote_path):
        smbclient.remove(remote_path)


def _delete_partial_s3(config: dict, relative_path: str) -> None:
    s3 = _s3_client(config)
    key = "/".join(filter(None, [config.get("prefix", "").strip("/"), relative_path]))
    s3.delete_object(Bucket=config["bucket"], Key=key)


def _delete_partial_rclone(config: dict, relative_path: str, conf_path: Optional[str] = None) -> None:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    dest = _rclone_path(config, relative_path)
    subprocess.run(
        ["rclone", "deletefile", dest, "--config", conf_path],
        capture_output=True, text=True, timeout=300,
    )


STREAM_UPLOAD_CLEANUP_HANDLERS = {
    "local_path": _delete_partial_local_path,
    "smb": _delete_partial_smb,
    "s3": _delete_partial_s3,
    "rclone": _delete_partial_rclone,
}


def stream_upload_to_target(target_type: str, config_json: str, relative_path: str, chunks) -> None:
    """Uploads a stream of bytes chunks straight to a storage target, without
    ever writing it to local disk first. Not supported for google_drive /
    onedrive (their upload session APIs need a file size or seekable content
    up front) - use a regular synced target for those instead."""
    handler = STREAM_UPLOAD_HANDLERS.get(target_type)
    if not handler:
        raise ValueError(
            f"Direktes Streaming wird für Zieltyp '{target_type}' nicht unterstützt "
            "(nicht für Google Drive und OneDrive)."
        )
    config = json.loads(config_json or "{}")
    try:
        handler(config, relative_path, chunks)
    except BaseException:
        # A cancelled/failed volume archive (e.g. BackupCancelled, a network
        # blip) still leaves however many bytes were already written sitting
        # on the target - for a multi-GB volume that's a large orphaned file
        # that never gets counted or cleaned up by retention. Best-effort
        # remove it so failed attempts don't silently eat disk space forever.
        cleanup = STREAM_UPLOAD_CLEANUP_HANDLERS.get(target_type)
        if cleanup:
            try:
                cleanup(config, relative_path)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to clean up partial streamed upload for %s", relative_path)
        raise


def resolve_stream_target(db, target_id: Optional[int]):
    """Looks up a StorageTarget for direct-volume-streaming, returning the
    (type, config_json, id) tuple backup_engine's stream_target expects, or
    None if unset/not found/disabled."""
    if target_id is None:
        return None
    from app.models import StorageTarget
    target = db.query(StorageTarget).filter(
        StorageTarget.id == target_id, StorageTarget.enabled == True,  # noqa: E712
    ).first()
    if not target:
        return None
    return (target.type, target.config_json, target.id)


def _download_local_path(config: dict, relative_path: str, dest_path: Path) -> None:
    shutil.copy2(Path(config["path"]) / relative_path, dest_path)


def _download_s3(config: dict, relative_path: str, dest_path: Path) -> None:
    s3 = _s3_client(config)
    key = "/".join(filter(None, [config.get("prefix", "").strip("/"), relative_path]))
    s3.download_file(config["bucket"], key, str(dest_path))


def _download_smb(config: dict, relative_path: str, dest_path: Path) -> None:
    import smbclient

    _smb_register_session(config)
    remote_root = _smb_remote_root(config, "")
    remote_path = remote_root + "\\" + relative_path.replace("/", "\\")
    with smbclient.open_file(remote_path, mode="rb", buffering=_SMB_IO_BUFFER_SIZE) as src, open(dest_path, "wb") as dst:
        shutil.copyfileobj(src, dst, length=_SMB_IO_BUFFER_SIZE)


def _download_rclone(config: dict, relative_path: str, dest_path: Path, conf_path: Optional[str] = None) -> None:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    src = _rclone_path(config, relative_path)
    proc = subprocess.run(
        ["rclone", "copyto", src, str(dest_path), "--config", conf_path, *_RCLONE_TRANSFER_FLAGS],
        capture_output=True, text=True, timeout=TRANSFER_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copyto failed: {proc.stderr.strip() or proc.stdout.strip()}")


DOWNLOAD_HANDLERS = {
    "local_path": _download_local_path,
    "smb": _download_smb,
    "s3": _download_s3,
    "rclone": _download_rclone,
}


def download_from_target(target_type: str, config_json: str, relative_path: str, dest_path: Path) -> None:
    """Downloads a single file previously written via stream_upload_to_target
    to a local path - used to restore a volume that was streamed directly to
    a target rather than kept locally."""
    handler = DOWNLOAD_HANDLERS.get(target_type)
    if not handler:
        raise ValueError(f"Download wird für Zieltyp '{target_type}' nicht unterstützt.")
    config = json.loads(config_json or "{}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    handler(config, relative_path, dest_path)


def sync_to_all_targets(backup_path: Path, on_progress=None) -> list[dict]:
    """Runs after a successful ad-hoc (manually triggered) backup: syncs to
    every enabled storage target. Scheduled backups instead use
    sync_to_selected_targets() so each schedule can pick specific targets."""
    return _sync_to_targets(backup_path, target_ids=None, on_progress=on_progress)


def sync_to_selected_targets(backup_path: Path, target_ids: list[int], on_progress=None) -> list[dict]:
    """Runs after a successful scheduled backup: syncs only to the explicitly
    selected (and still enabled) storage targets. An empty list means no
    off-site sync for this schedule."""
    return _sync_to_targets(backup_path, target_ids=target_ids, on_progress=on_progress)


def _sync_to_targets(backup_path: Path, target_ids: Optional[list[int]], on_progress=None) -> list[dict]:
    """Never raises - a failed off-site copy must not make the (already
    successful) local backup look failed. Targets are independent of each
    other, so they are uploaded to in parallel (DBM_SYNC_PARALLELISM); the DB
    is only touched from this thread."""
    from concurrent.futures import ThreadPoolExecutor

    from app.config import SYNC_PARALLELISM
    from app.database import SessionLocal
    from app.models import StorageTarget

    db = SessionLocal()
    results = []
    try:
        query = db.query(StorageTarget).filter(StorageTarget.enabled == True)  # noqa: E712
        if target_ids is not None:
            if not target_ids:
                return []
            query = query.filter(StorageTarget.id.in_(target_ids))
        targets = query.all()
        if not targets:
            return []
        jobs = [(t.id, t.name, t.type, t.config_json) for t in targets]
        by_id = {t.id: t for t in targets}
        done = [0]

        def run(job):
            target_id, name, target_type, config_json = job
            try:
                sync_to_target(backup_path, target_type, config_json)
                return target_id, None
            except Exception as exc:  # noqa: BLE001
                logger.exception("Storage sync failed for target %s", name)
                return target_id, str(exc) or exc.__class__.__name__

        if on_progress:
            on_progress(f"Uploading to {', '.join(j[1] for j in jobs)}", 0, len(jobs))
        with ThreadPoolExecutor(max_workers=min(SYNC_PARALLELISM, len(jobs)),
                                thread_name_prefix="storage-sync") as pool:
            outcomes = {}
            for target_id, error in pool.map(run, jobs):
                outcomes[target_id] = error
                done[0] += 1
                if on_progress:
                    on_progress(f"Uploaded to {by_id[target_id].name}", done[0], len(jobs))

        for target_id, name, _type, _cfg in jobs:
            target = by_id[target_id]
            error = outcomes[target_id]
            target.last_sync_status = "ok" if error is None else "failed"
            target.last_sync_error = error
            target.last_sync_at = utcnow()
            entry = {"target": name, "target_id": target_id, "ok": error is None}
            if error is not None:
                entry["error"] = error
            results.append(entry)
        db.commit()
    finally:
        db.close()
    return results


def check_target_connection(target_type: str, config_json: str) -> None:
    """Best-effort connectivity check, raises on failure."""
    config = json.loads(config_json or "{}")
    if target_type == "local_path":
        p = Path(config["path"])
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".dbm_write_test"
        probe.write_text("ok")
        probe.unlink()
    elif target_type == "s3":
        s3 = _s3_client(config)
        s3.head_bucket(Bucket=config["bucket"])
    elif target_type == "smb":
        import smbclient
        _smb_register_session(config)
        root = _smb_remote_root(config, "")
        smbclient.makedirs(root, exist_ok=True)
        smbclient.listdir(root)
    elif target_type == "rclone":
        remote = config["remote"]
        proc = subprocess.run(
            ["rclone", "lsd", f"{remote}:", "--config", RCLONE_CONFIG_PATH],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    elif target_type == "google_drive":
        from app import oauth_storage
        oauth_storage.check_google_drive_connection(config)
    elif target_type == "onedrive":
        from app import oauth_storage
        oauth_storage.check_onedrive_connection(config)
    elif rclone_backends.is_managed(target_type):
        _check_managed_connection(target_type, config)
    else:
        raise ValueError(f"Unknown storage target type: {target_type}")


import logging as _logging
_space_logger = _logging.getLogger("dbm.space")


def _rclone_about(rclone_target: str, rclone_conf_path: str) -> dict:
    """Runs 'rclone about <rclone_target>' and returns {total_bytes, used_bytes, free_bytes}.
    rclone_target must be a full rclone path like 'myremote:' or 'myremote:share/path'.
    Any value may be None; never raises."""
    try:
        proc = subprocess.run(
            ["rclone", "about", rclone_target, "--json", "--config", rclone_conf_path],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            _space_logger.warning("rclone about %s: exit %d — %s", rclone_target, proc.returncode,
                                  (proc.stderr or proc.stdout or "").strip()[:300])
            return {"total_bytes": None, "used_bytes": None, "free_bytes": None}
        data = json.loads(proc.stdout)
        total = data.get("total")
        used = data.get("used")
        free = data.get("free") or (total - used if total and used else None)
        # debug only: the dashboard asks for this on every page load - INFO would put a row
        # into the persistent log each time and make the database grow for no benefit
        _space_logger.debug("rclone about %s: total=%s used=%s free=%s", rclone_target, total, used, free)
        return {"total_bytes": total, "used_bytes": used, "free_bytes": free}
    except Exception as exc:
        _space_logger.warning("rclone about %s exception: %s", rclone_target, exc)
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None}


def get_target_space_info(target_type: str, config_json: str) -> dict:
    """Returns disk usage for a storage target.
    Result: {total_bytes, used_bytes, free_bytes} — any value may be None if unsupported.
    Never raises; errors result in all-None."""
    config = json.loads(config_json or "{}")
    try:
        if target_type == "local_path":
            usage = shutil.disk_usage(config.get("path", "/"))
            return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
        elif target_type == "smb":
            # rclone's SMB backend requires the share name as the first path
            # component: "dbm_smb:share_name" — not just "dbm_smb:"
            from app import restic_engine
            smb_conf_path = restic_engine._write_smb_rclone_conf(config)
            try:
                share = config["share"]
                # rclone about only accepts the SMB share name (first path component).
                # If the user stored a sub-path like "Backup_Clients\Lagerverwaltung",
                # strip everything after the first separator.
                top_share = share.replace("\\", "/").split("/")[0]
                return _rclone_about(f"dbm_smb:{top_share}", smb_conf_path)
            finally:
                Path(smb_conf_path).unlink(missing_ok=True)
        elif target_type == "rclone":
            remote = config.get("remote", "")
            if not remote:
                return {"total_bytes": None, "used_bytes": None, "free_bytes": None}
            return _rclone_about(f"{remote}:", RCLONE_CONFIG_PATH)
        elif rclone_backends.is_managed(target_type):
            return _managed_space_info(target_type, config)
    except Exception:
        pass
    return {"total_bytes": None, "used_bytes": None, "free_bytes": None}


# ---------- Catalog import: discover + pull back backups that already exist
# on a target (e.g. a fresh install pointed at an old NAS/S3 bucket/rclone
# remote after a full host loss) ----------

def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def _parse_backup_relative_dir(rel: PurePosixPath) -> Optional[dict]:
    """A backup version directory is always either <name>/<timestamp> (single
    container) or _landscapes/<name>/<timestamp> (landscape/project). Anything
    else found under a target isn't a backup this app made."""
    parts = rel.parts
    if len(parts) == 3 and parts[0] == "_landscapes":
        name, ts = parts[1], parts[2]
        backup_type = "landscape"
    elif len(parts) == 2:
        name, ts = parts
        backup_type = "container"
    else:
        return None
    try:
        created_at = datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return {"relative_key": "/".join(parts), "name": name, "backup_type": backup_type, "created_at": created_at}


_META_FILENAMES = ("meta.json", "meta.json.enc")


def _list_backups_local_path(config: dict) -> list[dict]:
    root = Path(config["path"])
    if not root.exists():
        return []
    entries = []
    seen = set()
    for meta_filename in _META_FILENAMES:
        for meta_path in root.rglob(meta_filename):
            rel = meta_path.parent.relative_to(root)
            if rel in seen:
                continue
            seen.add(rel)
            parsed = _parse_backup_relative_dir(PurePosixPath(rel.as_posix()))
            if parsed:
                parsed["size_bytes"] = _dir_size(meta_path.parent)
                entries.append(parsed)
    return entries


def _list_backups_s3(config: dict) -> list[dict]:
    s3 = _s3_client(config)
    bucket = config["bucket"]
    prefix = config.get("prefix", "").strip("/")
    list_prefix = prefix + "/" if prefix else ""

    sizes_by_key: dict[str, int] = {}
    has_meta: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(list_prefix):] if list_prefix else obj["Key"]
            parts = PurePosixPath(rel).parts
            if parts and parts[0] == "_landscapes" and len(parts) >= 4:
                backup_rel = "/".join(parts[:3])
            elif len(parts) >= 2:
                backup_rel = "/".join(parts[:2])
            else:
                continue
            sizes_by_key[backup_rel] = sizes_by_key.get(backup_rel, 0) + obj["Size"]
            if parts[-1] in _META_FILENAMES:
                has_meta.add(backup_rel)

    entries = []
    for backup_rel in has_meta:
        parsed = _parse_backup_relative_dir(PurePosixPath(backup_rel))
        if parsed:
            parsed["size_bytes"] = sizes_by_key.get(backup_rel, 0)
            entries.append(parsed)
    return entries


def _list_backups_smb(config: dict) -> list[dict]:
    # Use rclone lsjson instead of smbclient.walk() — the smbclient approach
    # blocks indefinitely on large shares and has no usable timeout.
    from app import restic_engine as _re

    smb_conf_path = _re._write_smb_rclone_conf(config)
    try:
        share = config["share"]
        base = config.get("base_path", "").strip("/\\").replace("\\", "/")
        top_share = share.replace("\\", "/").split("/")[0]
        sub = "/".join(p for p in [share.replace("\\", "/").split("/", 1)[1] if "/" in share.replace("\\", "/") else "", base] if p)
        src = f"dbm_smb:{top_share}/{sub}".rstrip("/") if sub else f"dbm_smb:{top_share}"
        proc = subprocess.run(
            ["rclone", "lsjson", src, "-R", "--files-only", "--config", smb_conf_path],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        try:
            Path(smb_conf_path).unlink(missing_ok=True)
        except Exception:
            pass

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "directory not found" in err.lower() or "object not found" in err.lower():
            return []
        raise RuntimeError(f"rclone lsjson (SMB) fehlgeschlagen: {err}")

    sizes_by_key: dict[str, int] = {}
    has_meta: set[str] = set()
    containers_by_key: dict[str, list[str]] = {}
    for item in json.loads(proc.stdout or "[]"):
        parts = PurePosixPath(item["Path"]).parts
        if parts and parts[0] == "_landscapes" and len(parts) >= 4:
            backup_rel = "/".join(parts[:3])
            # .json files directly inside the landscape dir (depth 4) are member-container markers
            if len(parts) == 4 and parts[-1].endswith(".json") and parts[-1] not in _META_FILENAMES:
                containers_by_key.setdefault(backup_rel, []).append(parts[-1][:-len(".json")])
        elif len(parts) >= 2:
            backup_rel = "/".join(parts[:2])
        else:
            continue
        sizes_by_key[backup_rel] = sizes_by_key.get(backup_rel, 0) + item.get("Size", 0)
        if parts[-1] in _META_FILENAMES:
            has_meta.add(backup_rel)

    entries = []
    for backup_rel in has_meta:
        parsed = _parse_backup_relative_dir(PurePosixPath(backup_rel))
        if parsed:
            parsed["size_bytes"] = sizes_by_key.get(backup_rel, 0)
            parsed["containers"] = containers_by_key.get(backup_rel, [])
            entries.append(parsed)
    return entries


def _list_backups_rclone(config: dict, conf_path: Optional[str] = None) -> list[dict]:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    src = _rclone_path(config)
    proc = subprocess.run(
        ["rclone", "lsjson", src, "-R", "--files-only", "--config", conf_path],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        if "directory not found" in (proc.stderr or "").lower():
            return []
        raise RuntimeError(f"rclone lsjson failed: {proc.stderr.strip() or proc.stdout.strip()}")

    sizes_by_key: dict[str, int] = {}
    has_meta: set[str] = set()
    containers_by_key: dict[str, list[str]] = {}
    for item in json.loads(proc.stdout or "[]"):
        parts = PurePosixPath(item["Path"]).parts
        if parts and parts[0] == "_landscapes" and len(parts) >= 4:
            backup_rel = "/".join(parts[:3])
            if len(parts) == 4 and parts[-1].endswith(".json") and parts[-1] not in _META_FILENAMES:
                containers_by_key.setdefault(backup_rel, []).append(parts[-1][:-len(".json")])
        elif len(parts) >= 2:
            backup_rel = "/".join(parts[:2])
        else:
            continue
        sizes_by_key[backup_rel] = sizes_by_key.get(backup_rel, 0) + item.get("Size", 0)
        if parts[-1] in _META_FILENAMES:
            has_meta.add(backup_rel)

    entries = []
    for backup_rel in has_meta:
        parsed = _parse_backup_relative_dir(PurePosixPath(backup_rel))
        if parsed:
            parsed["size_bytes"] = sizes_by_key.get(backup_rel, 0)
            parsed["containers"] = containers_by_key.get(backup_rel, [])
            entries.append(parsed)
    return entries


LIST_BACKUPS_HANDLERS = {
    "local_path": _list_backups_local_path,
    "smb": _list_backups_smb,
    "s3": _list_backups_s3,
    "rclone": _list_backups_rclone,
}


def list_backups_on_target(target_type: str, config_json: str) -> list[dict]:
    """Scans a storage target for existing backup versions (recognized by a
    meta.json/meta.json.enc marker file), so a fresh install can rebuild its
    local catalog from a target that already holds real backups - e.g. after
    a full host loss where only the offsite copy survived. Not supported for
    google_drive/onedrive (no cheap recursive listing API for either without
    a lot more code); use one of the other target types for this workflow."""
    handler = LIST_BACKUPS_HANDLERS.get(target_type)
    if not handler:
        raise ValueError(
            f"Katalog-Import wird für Zieltyp '{target_type}' nicht unterstützt "
            "(nicht für Google Drive und OneDrive)."
        )
    config = json.loads(config_json or "{}")
    return handler(config)


def _download_full_local_path(config: dict, relative_key: str, dest_dir: Path) -> None:
    src = Path(config["path"]) / relative_key
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(src, dest_dir)


def _download_full_s3(config: dict, relative_key: str, dest_dir: Path) -> None:
    s3 = _s3_client(config)
    bucket = config["bucket"]
    prefix = _s3_dir_prefix(config, relative_key)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):].lstrip("/")
            if not rel:
                continue
            dest_file = dest_dir / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(dest_file))


def _download_full_smb(config: dict, relative_key: str, dest_dir: Path) -> None:
    # rclone-based implementation — replaces the old smbclient.walk() version
    # which blocked indefinitely on certain SMB servers (same issue as _list_backups_smb).
    from app import restic_engine as _re

    smb_conf_path = _re._write_smb_rclone_conf(config)
    try:
        share = config["share"]
        base = config.get("base_path", "").strip("/\\").replace("\\", "/")
        top_share = share.replace("\\", "/").split("/")[0]
        sub_parts = [p for p in [
            share.replace("\\", "/").split("/", 1)[1] if "/" in share.replace("\\", "/") else "",
            base,
            relative_key.replace("\\", "/"),
        ] if p]
        sub = "/".join(sub_parts)
        src = f"dbm_smb:{top_share}/{sub}" if sub else f"dbm_smb:{top_share}"
        proc = subprocess.run(
            ["rclone", "copy", src, str(dest_dir), "--config", smb_conf_path, *_RCLONE_TRANSFER_FLAGS],
            capture_output=True, text=True, timeout=TRANSFER_TIMEOUT_SECONDS,
        )
    finally:
        try:
            Path(smb_conf_path).unlink(missing_ok=True)
        except Exception:
            pass
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copy (SMB) fehlgeschlagen: {(proc.stderr or proc.stdout or '').strip()}")


def _download_full_rclone(config: dict, relative_key: str, dest_dir: Path, conf_path: Optional[str] = None) -> None:
    conf_path = conf_path or RCLONE_CONFIG_PATH
    src = _rclone_path(config, relative_key)
    proc = subprocess.run(
        ["rclone", "copy", src, str(dest_dir), "--config", conf_path, *_RCLONE_TRANSFER_FLAGS],
        capture_output=True, text=True, timeout=TRANSFER_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copy failed: {proc.stderr.strip() or proc.stdout.strip()}")


DOWNLOAD_FULL_HANDLERS = {
    "local_path": _download_full_local_path,
    "smb": _download_full_smb,
    "s3": _download_full_s3,
    "rclone": _download_full_rclone,
}


def download_full_backup_from_target(target_type: str, config_json: str, relative_key: str, dest_dir: Path) -> None:
    """Downloads every file under relative_key on a target into dest_dir,
    reconstructing the backup's normal local directory layout - used to
    materialize a backup version that a fresh install only knows about via
    list_backups_on_target(), before it can be restored."""
    handler = DOWNLOAD_FULL_HANDLERS.get(target_type)
    if not handler:
        raise ValueError(f"Download wird für Zieltyp '{target_type}' nicht unterstützt.")
    config = json.loads(config_json or "{}")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    handler(config, relative_key, dest_dir)


# ---------- rclone-based targets configured in the web UI (SFTP, WebDAV, FTP,
# Azure Blob, Dropbox, further clouds). Each operation materializes a temp
# rclone.conf from the stored settings and then runs the exact same code path
# as a hand-configured "rclone" target. See app/rclone_backends.py. ----------

def _managed(target_type: str, fn):
    """Wraps an rclone handler (config first, conf_path keyword) so it runs
    against a freshly generated rclone.conf for a UI-configured target."""
    def handler(config: dict, *args):
        with rclone_backends.materialized(target_type, config) as (conf_path, rclone_config):
            return fn(rclone_config, *args, conf_path=conf_path)
    return handler


def _managed_sync(target_type: str):
    def handler(backup_path: Path, config: dict) -> None:
        with rclone_backends.materialized(target_type, config) as (conf_path, rclone_config):
            sync_rclone(backup_path, rclone_config, conf_path=conf_path)
    return handler


for _t in rclone_backends.TARGET_TYPES:
    SYNC_HANDLERS[_t] = _managed_sync(_t)
    DELETE_HANDLERS[_t] = _managed(_t, _delete_rclone)
    STREAM_UPLOAD_HANDLERS[_t] = _managed(_t, _stream_upload_rclone)
    STREAM_UPLOAD_CLEANUP_HANDLERS[_t] = _managed(_t, _delete_partial_rclone)
    DOWNLOAD_HANDLERS[_t] = _managed(_t, _download_rclone)
    LIST_BACKUPS_HANDLERS[_t] = _managed(_t, _list_backups_rclone)
    DOWNLOAD_FULL_HANDLERS[_t] = _managed(_t, _download_full_rclone)
del _t


def _check_managed_connection(target_type: str, config: dict) -> None:
    """Writes and deletes a small probe file - proves login, path and write
    permission in one go (a plain listing would pass on a read-only login)."""
    with rclone_backends.materialized(target_type, config) as (conf_path, rclone_config):
        probe = _rclone_path(rclone_config, ".dbm_write_test")
        proc = subprocess.run(
            ["rclone", "rcat", probe, "--config", conf_path, "--low-level-retries", "2", "--retries", "1"],
            input="ok", capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"rclone exit {proc.returncode}")
        subprocess.run(["rclone", "deletefile", probe, "--config", conf_path],
                       capture_output=True, text=True, timeout=60)


def _managed_space_info(target_type: str, config: dict) -> dict:
    with rclone_backends.materialized(target_type, config) as (conf_path, rclone_config):
        return _rclone_about(f"{rclone_config['remote']}:", conf_path)


def _managed_try_rmdir(target_type: str, config: dict, relative_key: str) -> None:
    with rclone_backends.materialized(target_type, config) as (conf_path, rclone_config):
        subprocess.run(["rclone", "rmdir", _rclone_path(rclone_config, relative_key), "--config", conf_path],
                       capture_output=True, text=True, timeout=30)
