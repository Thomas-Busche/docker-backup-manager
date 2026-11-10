"""S3 and rclone connector tests.

S3 runs against moto's in-memory S3 implementation (real boto3 calls, no
account needed). rclone is exercised through a stand-in executable so the
exact command lines and error handling can be checked without real remotes.
"""
import json
import os
import stat
import sys
import uuid

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
def s3_config(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="backups")
        yield {"access_key": "test", "secret_key": "test", "bucket": "backups",
               "region": "us-east-1", "prefix": "dbm"}


def _make_backup(name, ts, files):
    from app.config import BACKUPS_DIR

    d = BACKUPS_DIR / name / ts
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def _keys(config):
    client = boto3.client("s3", region_name="us-east-1")
    return sorted(o["Key"] for o in client.list_objects_v2(Bucket=config["bucket"]).get("Contents", []))


def test_s3_sync_check_and_list(s3_config):
    from app import storage_sync

    name = f"s3app-{uuid.uuid4().hex[:6]}"
    backup = _make_backup(name, "20250101T000000Z", {"meta.json": b"{}", "volumes/v.tar.gz": b"data"})
    storage_sync.check_target_connection("s3", json.dumps(s3_config))
    storage_sync.sync_to_target(backup, "s3", json.dumps(s3_config))

    assert _keys(s3_config) == [f"dbm/{name}/20250101T000000Z/meta.json",
                                f"dbm/{name}/20250101T000000Z/volumes/v.tar.gz"]
    found = storage_sync.list_backups_on_target("s3", json.dumps(s3_config))
    assert [(f["name"], f["backup_type"]) for f in found] == [(name, "container")]


def test_s3_delete_of_one_version_never_touches_a_version_sharing_its_prefix(s3_config):
    """Regression: deleting ".../v1" used prefix ".../v1" which also matched ".../v10"."""
    from app import storage_sync

    name = f"s3pfx-{uuid.uuid4().hex[:6]}"
    v1 = _make_backup(name, "v1", {"meta.json": b"1"})
    v10 = _make_backup(name, "v10", {"meta.json": b"10"})
    for b in (v1, v10):
        storage_sync.sync_to_target(b, "s3", json.dumps(s3_config))

    storage_sync.delete_from_target("s3", json.dumps(s3_config), f"{name}/v1")
    assert _keys(s3_config) == [f"dbm/{name}/v10/meta.json"]


def test_s3_full_download_only_fetches_the_requested_version(s3_config, tmp_path):
    from app import storage_sync

    name = f"s3dl-{uuid.uuid4().hex[:6]}"
    for ts in ("20250101T000000Z", "20250101T000000Z1"):
        storage_sync.sync_to_target(_make_backup(name, ts, {"meta.json": ts.encode()}), "s3", json.dumps(s3_config))

    dest = tmp_path / "restore"
    storage_sync.download_full_backup_from_target("s3", json.dumps(s3_config), f"{name}/20250101T000000Z", dest)
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == ["meta.json"]
    assert (dest / "meta.json").read_bytes() == b"20250101T000000Z"


def test_s3_stream_upload_download_and_partial_cleanup(s3_config, tmp_path):
    from app import storage_sync

    payload = [os.urandom(1024 * 1024) for _ in range(9)]  # > 8 MiB -> multipart upload
    storage_sync.stream_upload_to_target("s3", json.dumps(s3_config), "app/ts/volumes/v.tar.gz", iter(payload))
    out = tmp_path / "v.tar.gz"
    storage_sync.download_from_target("s3", json.dumps(s3_config), "app/ts/volumes/v.tar.gz", out)
    assert out.read_bytes() == b"".join(payload)

    def failing():
        yield b"x" * 1024
        raise RuntimeError("container died")

    with pytest.raises(RuntimeError):
        storage_sync.stream_upload_to_target("s3", json.dumps(s3_config), "app/ts/volumes/w.tar.gz", failing())
    assert "dbm/app/ts/volumes/w.tar.gz" not in _keys(s3_config)


def test_s3_client_has_timeouts_and_retries():
    from app import storage_sync

    client = storage_sync._s3_client({"access_key": "a", "secret_key": "b"})
    cfg = client.meta.config
    assert cfg.connect_timeout == 30 and cfg.read_timeout == 300
    assert cfg.retries["mode"] == "adaptive"


# ---------------------------------------------------------------- rclone

@pytest.mark.parametrize("config,rel,expected", [
    ({"remote": "nas"}, "app/ts", "nas:app/ts"),  # no leading slash -> stays home-relative on SFTP
    ({"remote": "nas", "remote_path": "/backups/"}, "app/ts", "nas:backups/app/ts"),
    ({"remote": "nas", "remote_path": "backups"}, "", "nas:backups"),
    ({"remote": "nas"}, "", "nas:"),
    ({"remote": "nas", "remote_path": "b"}, "app\\ts", "nas:b/app/ts"),
])
def test_rclone_path(config, rel, expected):
    from app.storage_sync import _rclone_path
    assert _rclone_path(config, rel) == expected


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    """Puts a stand-in `rclone` first on PATH that logs its argv and stdin,
    and fails when the destination contains 'broken'."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    script = bin_dir / "rclone"
    script.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
data = b""
if args and args[0] == "rcat":
    if any("broken" in a for a in args):
        sys.stderr.write("Failed to rcat: couldn't find section in config file\\n")
        sys.exit(1)
    data = sys.stdin.buffer.read()
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "stdin_len": len(data)}}) + "\\n")
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def test_rclone_sync_uses_relative_destination_and_retry_flags(fake_rclone):
    from app import storage_sync

    backup = _make_backup(f"rc-{uuid.uuid4().hex[:6]}", "20250101T000000Z", {"meta.json": b"{}"})
    storage_sync.sync_to_target(backup, "rclone", json.dumps({"remote": "sftpbox"}))
    args = fake_rclone()[0]["args"]
    assert args[0] == "copy"
    assert args[2] == f"sftpbox:{backup.parent.name}/{backup.name}"
    assert "--retries" in args and "--config" in args


def test_rclone_stream_upload_pipes_all_bytes(fake_rclone):
    from app import storage_sync

    chunks = [b"a" * 70_000] * 20  # more than a pipe buffer
    storage_sync.stream_upload_to_target("rclone", json.dumps({"remote": "r", "remote_path": "p"}),
                                         "app/ts/volumes/v.tar.gz", iter(chunks))
    call = fake_rclone()[0]
    assert call["args"][:2] == ["rcat", "r:p/app/ts/volumes/v.tar.gz"]
    assert call["stdin_len"] == 70_000 * 20


def test_rclone_stream_upload_reports_rclone_error_instead_of_broken_pipe(fake_rclone):
    from app import storage_sync

    with pytest.raises(RuntimeError, match="couldn't find section"):
        storage_sync.stream_upload_to_target("rclone", json.dumps({"remote": "broken"}),
                                             "a/b.tar.gz", iter([b"x" * 1_000_000] * 5))


def test_rclone_delete_refuses_to_purge_the_whole_target():
    from app import storage_sync

    with pytest.raises(ValueError):
        storage_sync.delete_from_target("rclone", json.dumps({"remote": "r", "remote_path": "p"}), "")


def test_restic_rclone_repo_gets_rclone_config_env():
    from app import restic_engine
    from app.storage_sync import RCLONE_CONFIG_PATH

    repo, env, conf = restic_engine.repo_url_and_env("rclone", {"remote": "gd", "remote_path": "/dbm/"}, "my app")
    assert repo == "rclone:gd:dbm/my_app/restic_repo"
    assert env["RCLONE_CONFIG"] == RCLONE_CONFIG_PATH
    assert conf is None


def test_restic_smb_repo_normalizes_windows_style_paths(monkeypatch):
    from app import restic_engine

    monkeypatch.setattr(restic_engine, "_write_smb_rclone_conf", lambda cfg: "/tmp/x.conf")
    repo, env, _ = restic_engine.repo_url_and_env(
        "smb", {"server": "nas", "share": "Backup\\Clients", "base_path": "\\docker\\", "username": "u",
                "password": "p"}, "app")
    assert repo == "rclone:dbm_smb:Backup/Clients/docker/app/restic_repo"
