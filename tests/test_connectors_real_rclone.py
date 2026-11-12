"""End-to-end tests of the UI-configured rclone targets against a REAL rclone
binary and real local SFTP/WebDAV/FTP servers (started with `rclone serve`).

They prove that the generated rclone.conf (option names, obscured passwords,
paths) is accepted by rclone itself and that upload, streaming, download,
catalog import and delete work over the actual protocols.

Skipped when no rclone binary is installed (the CI workflow installs one).
"""
import json
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

RCLONE = shutil.which("rclone")
pytestmark = pytest.mark.skipif(
    not RCLONE or os.environ.get("DBM_SKIP_REAL_RCLONE") == "1",
    reason="real rclone binary not installed",
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port, proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"rclone serve exited: {proc.stderr.read().decode(errors='replace')}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("rclone serve did not start")


@pytest.fixture(params=["sftp", "webdav", "ftp"])
def served_target(request, tmp_path):
    protocol = request.param
    root = tmp_path / "server-root"
    root.mkdir()
    port = _free_port()
    empty_conf = tmp_path / "empty.conf"
    empty_conf.write_text("")
    env = {**os.environ, "RCLONE_CONFIG": str(empty_conf), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    cmd = [RCLONE, "serve", protocol, str(root), "--addr", f"127.0.0.1:{port}", "--user", "dbm", "--pass", "s3cr3t pw"]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_port(port, proc)
        if protocol == "sftp":
            config = {"host": "127.0.0.1", "port": str(port), "username": "dbm", "password": "s3cr3t pw",
                      "path": "offsite"}
        elif protocol == "webdav":
            config = {"url": f"http://127.0.0.1:{port}", "vendor": "other", "username": "dbm",
                      "password": "s3cr3t pw", "path": "offsite"}
        else:
            config = {"host": "127.0.0.1", "port": str(port), "username": "dbm", "password": "s3cr3t pw",
                      "tls": "none", "path": "offsite"}
        yield protocol, config, root
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _make_backup(name, ts, files):
    from app.config import BACKUPS_DIR
    d = BACKUPS_DIR / name / ts
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def test_full_roundtrip_over_real_protocol(served_target, tmp_path):
    from app import storage_sync

    target_type, config, root = served_target
    cfg = json.dumps(config)
    name = f"real-{uuid.uuid4().hex[:6]}"
    ts = "20260101T030000Z"

    storage_sync.check_target_connection(target_type, cfg)
    assert not (root / "offsite" / ".dbm_write_test").exists()

    backup = _make_backup(name, ts, {"meta.json": b'{"x": 1}', "container.json": b"{}"})
    storage_sync.sync_to_target(backup, target_type, cfg)
    assert (root / "offsite" / name / ts / "meta.json").read_bytes() == b'{"x": 1}'

    payload = [os.urandom(256 * 1024) for _ in range(8)]
    storage_sync.stream_upload_to_target(target_type, cfg, f"{name}/{ts}/volumes/v.tar.gz", iter(payload))
    assert (root / "offsite" / name / ts / "volumes" / "v.tar.gz").read_bytes() == b"".join(payload)

    out = tmp_path / "dl" / "v.tar.gz"
    storage_sync.download_from_target(target_type, cfg, f"{name}/{ts}/volumes/v.tar.gz", out)
    assert out.read_bytes() == b"".join(payload)

    found = storage_sync.list_backups_on_target(target_type, cfg)
    assert [(f["name"], f["relative_key"]) for f in found] == [(name, f"{name}/{ts}")]

    full = tmp_path / "full"
    storage_sync.download_full_backup_from_target(target_type, cfg, f"{name}/{ts}", full)
    assert sorted(p.relative_to(full).as_posix() for p in full.rglob("*") if p.is_file()) == \
        ["container.json", "meta.json", "volumes/v.tar.gz"]

    storage_sync.delete_from_target(target_type, cfg, f"{name}/{ts}")
    assert not (root / "offsite" / name).exists()


def test_wrong_password_is_reported(served_target):
    from app import storage_sync

    target_type, config, _ = served_target
    with pytest.raises(RuntimeError):
        storage_sync.check_target_connection(target_type, json.dumps({**config, "password": "wrong"}))


@pytest.mark.skipif(not shutil.which("restic"), reason="restic not installed")
def test_restic_repo_over_managed_sftp(served_target, monkeypatch):
    from app import rclone_backends, restic_engine

    target_type, config, root = served_target
    if target_type != "sftp":
        pytest.skip("one protocol is enough for restic")
    repo, env, conf = restic_engine.repo_url_and_env(target_type, config, "app")
    try:
        password = "restic-test-pw"
        restic_engine.init_repo_if_needed(repo, env, password)
        assert (root / "offsite" / "app" / "restic_repo" / "config").exists()
    finally:
        rclone_backends.discard_conf(conf)
