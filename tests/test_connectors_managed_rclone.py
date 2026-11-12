"""Tests for the rclone-based targets that are configured in the web UI
(SFTP, WebDAV, FTP/FTPS, Azure Blob, Dropbox, further clouds) and for the
S3 provider/checksum options.

No real servers or accounts are used: rclone is replaced by a stand-in
executable that records every call together with the generated rclone.conf,
so the exact config rclone would see can be checked. The option names follow
the rclone backend docs (rclone.org/sftp, /webdav, /ftp, /azureblob,
/dropbox, /pcloud, /box, /mega, /koofr, /storj).
"""
import json
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    script = bin_dir / "rclone"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["obscure"]:
    plain = sys.stdin.read() if args[1:] == ["-"] else args[1]
    print("OBS(" + plain + ")")
    sys.exit(0)
conf_text, known_hosts = None, None
if "--config" in args:
    conf = args[args.index("--config") + 1]
    if os.path.exists(conf):
        conf_text = open(conf).read()
        mode = oct(os.stat(conf).st_mode & 0o777)
        kh = os.path.join(os.path.dirname(conf), "known_hosts")
        if os.path.exists(kh):
            known_hosts = open(kh).read()
data = b""
if args[:1] == ["rcat"]:
    data = sys.stdin.buffer.read()
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "conf": conf_text, "known_hosts": known_hosts,
                        "conf_mode": mode if conf_text is not None else None,
                        "stdin": data.decode(errors="replace")[:20], "stdin_len": len(data)}}) + "\\n")
if os.environ.get("FAKE_RCLONE_FAIL"):
    sys.stderr.write(os.environ["FAKE_RCLONE_FAIL"] + "\\n")
    sys.exit(1)
if args[:1] == ["lsjson"]:
    print(os.environ.get("FAKE_LSJSON", "[]"))
if args[:1] == ["about"]:
    print(json.dumps({{"total": 1000, "used": 400, "free": 600}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def _conf_dict(conf_text):
    lines = conf_text.strip().splitlines()
    assert lines[0] == "[dbm_remote]"
    return dict(line.split(" = ", 1) for line in lines[1:])


def _tmp_conf_dirs():
    from app.config import BASE_DIR
    tmp = BASE_DIR / ".tmp"
    return sorted(p.name for p in tmp.glob("rclone_tmp_*")) if tmp.exists() else []


def _make_backup(name, ts, files):
    from app.config import BACKUPS_DIR
    d = BACKUPS_DIR / name / ts
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


SFTP = {"host": "u1.your-storagebox.de", "port": "23", "username": "u1", "password": "geheim", "path": "/backups/"}


# ---------------------------------------------------------------- config building

def test_sftp_conf_with_password_key_and_host_key(fake_rclone):
    from app import rclone_backends

    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----\n"
    cfg = {**SFTP, "private_key": key, "key_passphrase": "pp", "host_key": "ssh-ed25519 AAAAC3Nza"}
    opts, files, base = rclone_backends.build_remote("sftp", cfg)
    assert opts["type"] == "sftp" and opts["host"] == "u1.your-storagebox.de" and opts["port"] == "23"
    assert opts["user"] == "u1" and opts["pass"] == "OBS(geheim)"
    # rclone wants the PEM on one line with literal \n separators
    assert opts["key_pem"] == "-----BEGIN OPENSSH PRIVATE KEY-----\\nAAAA\\nBBBB\\n-----END OPENSSH PRIVATE KEY-----"
    assert opts["key_file_pass"] == "OBS(pp)"
    # non-standard port -> "[host]:port" form, as in OpenSSH known_hosts
    assert files["known_hosts"] == "[u1.your-storagebox.de]:23 ssh-ed25519 AAAAC3Nza\n"
    assert base == "backups"


def test_sftp_accepts_full_known_hosts_line_and_requires_a_credential(fake_rclone):
    from app import rclone_backends

    cfg = {**SFTP, "port": "22", "host_key": "myhost ssh-rsa AAAAB3 comment"}
    _, files, _ = rclone_backends.build_remote("sftp", {**cfg, "password": ""} | {"private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"})
    assert files["known_hosts"] == "myhost ssh-rsa AAAAB3\n"
    with pytest.raises(rclone_backends.ConfigError, match="Passwort oder privater"):
        rclone_backends.build_remote("sftp", {**SFTP, "password": ""})
    with pytest.raises(rclone_backends.ConfigError, match="Host-Key"):
        rclone_backends.build_remote("sftp", {**SFTP, "host_key": "garbage"})
    with pytest.raises(rclone_backends.ConfigError, match="Port"):
        rclone_backends.build_remote("sftp", {**SFTP, "port": "99999"})


def test_newlines_in_values_cannot_inject_rclone_options(fake_rclone):
    from app import rclone_backends

    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("sftp", {**SFTP, "host": "evil\n[other]\ntype = local"})
    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("webdav", {"url": "https://x\nvendor = other", "username": "u"})


@pytest.mark.parametrize("tls,skip,expected,absent", [
    ("explicit", False, {"explicit_tls": "true"}, ["tls", "no_check_certificate"]),
    ("implicit", True, {"tls": "true", "no_check_certificate": "true"}, ["explicit_tls"]),
    ("none", True, {}, ["tls", "explicit_tls", "no_check_certificate"]),
])
def test_ftp_tls_modes(fake_rclone, tls, skip, expected, absent):
    from app import rclone_backends

    opts, _, base = rclone_backends.build_remote("ftp", {"host": "fritz.box", "username": "u", "password": "p",
                                                          "tls": tls, "skip_cert_check": skip, "path": "USB/dbm"})
    assert opts["type"] == "ftp" and opts["port"] == "21" and opts["pass"] == "OBS(p)"
    for k, v in expected.items():
        assert opts[k] == v
    for k in absent:
        assert k not in opts
    assert base == "USB/dbm"


def test_webdav_vendor_and_url_validation(fake_rclone):
    from app import rclone_backends

    opts, _, base = rclone_backends.build_remote("webdav", {
        "url": "https://cloud.example.de/remote.php/dav/files/me", "vendor": "nextcloud",
        "username": "me", "password": "app-pw", "path": "dbm"})
    assert opts == {"type": "webdav", "url": "https://cloud.example.de/remote.php/dav/files/me",
                    "vendor": "nextcloud", "user": "me", "pass": "OBS(app-pw)"}
    assert base == "dbm"
    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("webdav", {"url": "ftp://x", "username": "u"})
    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("webdav", {"url": "https://x", "vendor": "bogus"})


def test_azure_key_vs_sas_and_container_in_path():
    from app import rclone_backends

    opts, _, base = rclone_backends.build_remote("azure_blob", {
        "account": "acc", "key": "k==", "container": "backups", "prefix": "/dbm/", "access_tier": "cool"})
    assert opts == {"type": "azureblob", "account": "acc", "key": "k==", "access_tier": "Cool"}
    assert base == "backups/dbm"
    opts, _, base = rclone_backends.build_remote("azure_blob", {"sas_url": "https://a.blob.core.windows.net/c?sv=1",
                                                                 "container": "c"})
    assert opts == {"type": "azureblob", "sas_url": "https://a.blob.core.windows.net/c?sv=1"}
    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("azure_blob", {"account": "a", "key": "k", "container": "c", "access_tier": "archive"})


def test_dropbox_conf_forces_token_refresh():
    from app import rclone_backends

    opts, _, base = rclone_backends.build_remote("dropbox", {
        "app_key": "ak", "app_secret": "as", "refresh_token": "rt", "folder_path": "/Apps/dbm/"})
    assert opts["client_id"] == "ak" and opts["client_secret"] == "as"
    token = json.loads(opts["token"])
    assert token["refresh_token"] == "rt" and token["expiry"].startswith("2000-")
    assert base == "Apps/dbm"
    with pytest.raises(rclone_backends.ConfigError, match="nicht verbunden"):
        rclone_backends.build_remote("dropbox", {"app_key": "ak", "app_secret": "as"})


def test_cloud_providers(fake_rclone):
    from app import rclone_backends

    tok = '{"access_token": "a", "token_type": "bearer",\n "refresh_token": "r"}'
    opts, _, _ = rclone_backends.build_remote("rclone_cloud", {"provider": "pcloud", "token": tok.replace("\n", "")})
    assert opts["type"] == "pcloud" and opts["hostname"] == "eapi.pcloud.com"
    assert json.loads(opts["token"])["refresh_token"] == "r" and "\n" not in opts["token"]
    assert rclone_backends.build_remote("rclone_cloud", {"provider": "box", "token": "{}"})[0]["type"] == "box"
    assert rclone_backends.build_remote("rclone_cloud", {"provider": "mega", "username": "m@x", "password": "p"})[0] == \
        {"type": "mega", "user": "m@x", "pass": "OBS(p)"}
    assert rclone_backends.build_remote("rclone_cloud", {"provider": "koofr", "username": "k@x", "password": "p"})[0] == \
        {"type": "koofr", "provider": "koofr", "user": "k@x", "password": "OBS(p)"}
    assert rclone_backends.build_remote("rclone_cloud", {"provider": "storj", "access_grant": "g"})[0] == \
        {"type": "storj", "provider": "existing", "access_grant": "g"}
    with pytest.raises(rclone_backends.ConfigError, match="JSON"):
        rclone_backends.build_remote("rclone_cloud", {"provider": "box", "token": "not json"})
    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("rclone_cloud", {"provider": "unknown"})


def test_custom_backend_options_are_validated_and_passwords_obscured(fake_rclone):
    from app import rclone_backends

    opts, _, _ = rclone_backends.build_remote("rclone_cloud", {
        "provider": "custom", "backend": "seafile",
        "options": "# comment\nurl = https://sea.example\nuser = me\npass = secret\n"})
    assert opts == {"type": "seafile", "url": "https://sea.example", "user": "me", "pass": "OBS(secret)"}
    for bad in ("type = local", "no-equals-sign", "Bad Key = x"):
        with pytest.raises(rclone_backends.ConfigError):
            rclone_backends.build_remote("rclone_cloud", {"provider": "custom", "backend": "b2", "options": bad})
    with pytest.raises(rclone_backends.ConfigError):
        rclone_backends.build_remote("rclone_cloud", {"provider": "custom", "backend": "b2 ; rm", "options": ""})


# ---------------------------------------------------------------- operations

def test_sync_uses_generated_conf_and_removes_it_afterwards(fake_rclone):
    from app import storage_sync

    before = _tmp_conf_dirs()
    name = f"sftp-{uuid.uuid4().hex[:6]}"
    backup = _make_backup(name, "20260101T000000Z", {"meta.json": b"{}"})
    storage_sync.sync_to_target(backup, "sftp", json.dumps({**SFTP, "host_key": "ssh-ed25519 AAAA"}))

    call = [c for c in fake_rclone() if c["args"][0] == "copy"][0]
    assert call["args"][2] == f"dbm_remote:backups/{name}/20260101T000000Z"
    assert "--retries" in call["args"]
    conf = _conf_dict(call["conf"])
    assert conf["type"] == "sftp" and conf["pass"] == "OBS(geheim)"
    assert conf["known_hosts_file"].endswith("/known_hosts")
    assert call["known_hosts"].startswith("[u1.your-storagebox.de]:23 ssh-ed25519")
    assert call["conf_mode"] == "0o600"
    assert _tmp_conf_dirs() == before  # temp conf + known_hosts gone again


def test_failed_sync_still_removes_conf_and_reports_rclone_error(fake_rclone, monkeypatch):
    from app import storage_sync

    before = _tmp_conf_dirs()
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "ssh: handshake failed: knownhosts: key mismatch")
    backup = _make_backup(f"f-{uuid.uuid4().hex[:6]}", "20260101T000000Z", {"meta.json": b"{}"})
    with pytest.raises(RuntimeError, match="key mismatch"):
        storage_sync.sync_to_target(backup, "sftp", json.dumps(SFTP))
    assert _tmp_conf_dirs() == before


@pytest.mark.parametrize("target_type,config,prefix", [
    ("webdav", {"url": "https://nc/remote.php/dav/files/u", "vendor": "nextcloud", "username": "u",
                "password": "p", "path": "dbm"}, "dbm_remote:dbm/"),
    ("ftp", {"host": "fritz.box", "username": "u", "password": "p", "path": ""}, "dbm_remote:"),
    ("azure_blob", {"account": "a", "key": "k", "container": "c"}, "dbm_remote:c/"),
    ("dropbox", {"app_key": "ak", "app_secret": "as", "refresh_token": "rt", "folder_path": "dbm"}, "dbm_remote:dbm/"),
    ("rclone_cloud", {"provider": "mega", "username": "m", "password": "p", "path": "x"}, "dbm_remote:x/"),
])
def test_stream_download_delete_roundtrip_per_type(fake_rclone, tmp_path, target_type, config, prefix):
    from app import storage_sync

    cfg = json.dumps(config)
    storage_sync.stream_upload_to_target(target_type, cfg, "app/ts/volumes/v.tar.gz", iter([b"a" * 70_000] * 10))
    storage_sync.download_from_target(target_type, cfg, "app/ts/volumes/v.tar.gz", tmp_path / "out" / "v.tar.gz")
    storage_sync.delete_from_target(target_type, cfg, "app/ts")
    calls = fake_rclone()
    rcat = next(c for c in calls if c["args"][0] == "rcat")
    assert rcat["args"][1] == prefix + "app/ts/volumes/v.tar.gz" and rcat["stdin_len"] == 700_000
    assert next(c for c in calls if c["args"][0] == "copyto")["args"][1] == prefix + "app/ts/volumes/v.tar.gz"
    assert next(c for c in calls if c["args"][0] == "purge")["args"][1] == prefix + "app/ts"
    assert all(c["conf"] and c["conf"].startswith("[dbm_remote]") for c in calls)


def test_failed_stream_removes_partial_file(fake_rclone):
    from app import storage_sync

    def failing():
        yield b"x" * 1000
        raise RuntimeError("container died")

    with pytest.raises(RuntimeError, match="container died"):
        storage_sync.stream_upload_to_target("sftp", json.dumps(SFTP), "a/b/v.tar.gz", failing())
    assert any(c["args"][:2] == ["deletefile", "dbm_remote:backups/a/b/v.tar.gz"] for c in fake_rclone())


def test_connection_check_writes_and_removes_probe(fake_rclone):
    from app import storage_sync

    storage_sync.check_target_connection("webdav", json.dumps({"url": "https://nas:5006", "username": "u",
                                                                "password": "p", "path": "dbm"}))
    calls = fake_rclone()
    assert calls[0]["args"][:2] == ["rcat", "dbm_remote:dbm/.dbm_write_test"] and calls[0]["stdin"] == "ok"
    assert calls[1]["args"][:2] == ["deletefile", "dbm_remote:dbm/.dbm_write_test"]


def test_connection_check_reports_bad_config_before_calling_rclone(fake_rclone):
    from app import storage_sync

    with pytest.raises(ValueError, match="Host fehlt"):
        storage_sync.check_target_connection("sftp", json.dumps({"username": "u", "password": "p"}))
    assert [c for c in fake_rclone() if c["args"][0] != "obscure"] == []


def test_catalog_import_and_space_info(fake_rclone, monkeypatch):
    from app import storage_sync

    monkeypatch.setenv("FAKE_LSJSON", json.dumps([
        {"Path": "myapp/20260101T030000Z/meta.json", "Size": 2},
        {"Path": "myapp/20260101T030000Z/image.tar", "Size": 100},
    ]))
    found = storage_sync.list_backups_on_target("sftp", json.dumps(SFTP))
    assert [(f["name"], f["size_bytes"]) for f in found] == [("myapp", 102)]
    lsjson = next(c for c in fake_rclone() if c["args"][0] == "lsjson")
    assert lsjson["args"][1] == "dbm_remote:backups"
    assert storage_sync.get_target_space_info("sftp", json.dumps(SFTP)) == \
        {"total_bytes": 1000, "used_bytes": 400, "free_bytes": 600}


def test_restic_repo_for_managed_target(fake_rclone):
    from app import rclone_backends, restic_engine

    repo, env, conf = restic_engine.repo_url_and_env("sftp", SFTP, "my app")
    try:
        assert repo == "rclone:dbm_remote:backups/my_app/restic_repo"
        assert env["RCLONE_CONFIG"] == conf and Path(conf).exists()
        assert _conf_dict(Path(conf).read_text())["type"] == "sftp"
    finally:
        rclone_backends.discard_conf(conf)
    assert not Path(conf).parent.exists()


def test_restic_s3_custom_endpoint_passes_region():
    from app import restic_engine

    repo, env, _ = restic_engine.repo_url_and_env("s3", {
        "endpoint_url": "https://fsn1.your-objectstorage.com", "region": "fsn1", "bucket": "b",
        "access_key": "a", "secret_key": "s"}, "app")
    assert repo == "s3:https://fsn1.your-objectstorage.com/b/app/restic_repo"
    assert env["AWS_DEFAULT_REGION"] == "fsn1"


# ---------------------------------------------------------------- S3 options

def test_s3_client_only_sends_required_checksums_and_honours_addressing_style():
    from app import storage_sync

    client = storage_sync._s3_client({"access_key": "a", "secret_key": "b", "endpoint_url": "http://10.0.0.5:9000",
                                      "addressing_style": "path"})
    cfg = client.meta.config
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert cfg.s3["addressing_style"] == "path"


def test_s3_storage_class_is_applied_and_validated(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from app import storage_sync

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bucket-sc")
        cfg = {"access_key": "test", "secret_key": "test", "bucket": "bucket-sc", "region": "us-east-1",
               "storage_class": "standard_ia"}
        backup = _make_backup(f"sc-{uuid.uuid4().hex[:6]}", "20260101T000000Z", {"meta.json": b"{}"})
        storage_sync.sync_to_target(backup, "s3", json.dumps(cfg))
        storage_sync.stream_upload_to_target("s3", json.dumps(cfg), "x/v.tar", iter([b"abc"]))
        objs = client.list_objects_v2(Bucket="bucket-sc")["Contents"]
        assert {o["StorageClass"] for o in objs} == {"STANDARD_IA"}
        with pytest.raises(ValueError, match="DEEP_ARCHIVE"):
            storage_sync.sync_to_target(backup, "s3", json.dumps({**cfg, "storage_class": "DEEP_ARCHIVE"}))


# ---------------------------------------------------------------- API

def _login(client, user):
    from app.reset_password import reset_password
    reset_password(user, "supersecret1")
    client.post("/api/auth/login", json={"username": user, "password": "supersecret1"})


def test_api_redacts_new_secrets_and_keeps_them_on_edit(monkeypatch):
    from app.database import SessionLocal
    from app.main import app
    from app.models import StorageTarget
    from app.routers import settings as settings_router

    with TestClient(app) as client:
        _login(client, "managed-targets-admin")
        r = client.post("/api/settings/storage-targets", json={"name": "box", "type": "sftp", "config": SFTP})
        assert r.status_code == 200
        tid = r.json()["id"]
        listed = next(t for t in client.get("/api/settings/storage-targets").json()["targets"] if t["id"] == tid)
        assert listed["config"]["password"] == "***" and listed["config"]["host"] == SFTP["host"]

        # The browser sends "***" back unchanged -> stored password must survive.
        r = client.put(f"/api/settings/storage-targets/{tid}",
                       json={"name": "box2", "type": "sftp", "config": {**listed["config"], "path": "neu"}})
        assert r.status_code == 200
        db = SessionLocal()
        try:
            stored = json.loads(db.get(StorageTarget, tid).config_json)
        finally:
            db.close()
        assert stored["password"] == "geheim" and stored["path"] == "neu"

        # "Verbindung testen" while editing gets the stored secret, not "***".
        seen = {}
        monkeypatch.setattr(settings_router.storage_sync, "check_target_connection",
                            lambda t, c: seen.update(json.loads(c)))
        r = client.post("/api/settings/storage-targets/test",
                        json={"type": "sftp", "config": listed["config"], "target_id": tid})
        assert r.status_code == 200 and seen["password"] == "geheim"

        r = client.post("/api/settings/storage-targets", json={"name": "x", "type": "nope", "config": {}})
        assert r.status_code == 400


def test_api_smb_edit_no_longer_saves_redacted_password():
    """Regression: editing an SMB/S3 target used to overwrite the password with "***"."""
    from app.database import SessionLocal
    from app.main import app
    from app.models import StorageTarget

    with TestClient(app) as client:
        _login(client, "smb-edit-admin")
        cfg = {"server": "nas", "share": "b", "username": "u", "password": "pw"}
        tid = client.post("/api/settings/storage-targets", json={"name": "n", "type": "smb", "config": cfg}).json()["id"]
        client.put(f"/api/settings/storage-targets/{tid}",
                   json={"name": "n", "type": "smb", "config": {**cfg, "password": "***"}})
        db = SessionLocal()
        try:
            assert json.loads(db.get(StorageTarget, tid).config_json)["password"] == "pw"
        finally:
            db.close()


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_dropbox_sign_in_flow(monkeypatch):
    from app import rclone_backends
    from app.database import SessionLocal
    from app.main import app
    from app.models import StorageTarget

    posted = []

    def fake_post(url, data=None, headers=None, timeout=None):
        posted.append((url, data))
        if url == rclone_backends.DROPBOX_TOKEN_URL:
            if data["code"] == "bad":
                return _Resp(400, {"error": "invalid_grant", "error_description": "code doesn't exist"})
            return _Resp(200, {"access_token": "at", "refresh_token": "rt-123", "token_type": "bearer"})
        return _Resp(200, {"email": "me@example.com"})

    monkeypatch.setattr(rclone_backends.requests, "post", fake_post)
    with TestClient(app) as client:
        _login(client, "dropbox-admin")
        url = client.post("/api/settings/dropbox/auth-url", json={"app_key": "ak"}).json()["url"]
        assert "client_id=ak" in url and "token_access_type=offline" in url and "redirect_uri" not in url

        r = client.post("/api/settings/dropbox/exchange", json={"app_key": "ak", "app_secret": "as", "code": "bad"})
        assert r.status_code == 400 and "code doesn't exist" in r.json()["detail"]

        r = client.post("/api/settings/dropbox/exchange", json={"app_key": "ak", "app_secret": "as", "code": "c0de"})
        assert r.status_code == 200
        body = r.json()
        assert body["account"] == "me@example.com" and "rt-123" not in json.dumps(body)
        token_call = posted[-2]
        assert token_call[1] == {"code": "c0de", "grant_type": "authorization_code",
                                 "client_id": "ak", "client_secret": "as"}

        r = client.post("/api/settings/storage-targets", json={"name": "dbx", "type": "dropbox", "config": {
            "app_key": "ak", "app_secret": "as", "folder_path": "dbm", "pending_id": body["pending_id"]}})
        assert r.status_code == 200
        tid = r.json()["id"]
        listed = next(t for t in client.get("/api/settings/storage-targets").json()["targets"] if t["id"] == tid)
        assert listed["config"]["connected"] is True and "refresh_token" not in listed["config"]
        assert listed["config"]["app_secret"] == "***"

        # A pending sign-in is single-use.
        r = client.post("/api/settings/storage-targets", json={"name": "dbx2", "type": "dropbox", "config": {
            "app_key": "ak", "app_secret": "as", "pending_id": body["pending_id"]}})
        assert r.status_code == 400

        # Editing the folder keeps token and secret.
        client.put(f"/api/settings/storage-targets/{tid}", json={"name": "dbx", "type": "dropbox",
                                                                "config": {**listed["config"], "folder_path": "neu"}})
        db = SessionLocal()
        try:
            stored = json.loads(db.get(StorageTarget, tid).config_json)
        finally:
            db.close()
        assert stored["refresh_token"] == "rt-123" and stored["app_secret"] == "as" and stored["folder_path"] == "neu"
        assert "connected" not in stored


def test_dropbox_pending_is_bound_to_app_key(monkeypatch):
    from app import rclone_backends

    monkeypatch.setattr(rclone_backends.requests, "post",
                        lambda url, **kw: _Resp(200, {"access_token": "a", "refresh_token": "r", "email": ""}))
    pending = rclone_backends.dropbox_exchange_code("ak", "as", "code")
    with pytest.raises(rclone_backends.ConfigError, match="App-Key"):
        rclone_backends.dropbox_pop_pending(pending["pending_id"], "other-key")
