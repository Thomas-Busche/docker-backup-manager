"""Regressions found by an end-to-end backup on one Docker host and restore on
a second, empty one (catalog import from a shared storage target)."""
import json
from pathlib import Path
from unittest.mock import MagicMock

from app import backup_engine, restore_engine, storage_sync
from app.config import BACKUPS_DIR


# ---------- landscape members reach the storage target ----------

def _local_target(db, tmp_path, name):
    from app.models import StorageTarget
    t = StorageTarget(name=name, type="local_path", enabled=True,
                      config_json=json.dumps({"path": str(tmp_path / name)}))
    db.add(t)
    db.commit()
    return t.id, tmp_path / name


def _version(rel: str, files: dict) -> Path:
    d = BACKUPS_DIR / rel
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (d / name).write_text(content)
    return d


def test_landscape_sync_uploads_member_backups_not_just_the_link_folder(tmp_path):
    from app.database import SessionLocal, init_db

    init_db()
    with SessionLocal() as db:
        tid, root = _local_target(db, tmp_path, "members-nas")
    land = _version("_landscapes/shop/20260101T000000Z", {"meta.json": "{}", "web.json": "{}"})
    web = _version("web/20260101T000000Z", {"meta.json": "{}", "image.tar": "img"})
    db_ = _version("db/20260101T000000Z", {"meta.json": "{}", "image.tar": "img"})

    results, member_synced = storage_sync.sync_backup_with_members(land, [str(web), str(db_)], [tid])

    assert [r["ok"] for r in results] == [True]
    assert (root / "_landscapes/shop/20260101T000000Z/meta.json").exists()
    assert (root / "web/20260101T000000Z/image.tar").exists()
    assert (root / "db/20260101T000000Z/image.tar").exists()
    assert member_synced == {str(web): [tid], str(db_): [tid]}


def test_metadata_is_copied_to_stream_target_even_if_not_selected(tmp_path):
    from app.database import SessionLocal, init_db
    from app.models import StorageTarget

    init_db()
    with SessionLocal() as db:
        stream_id, stream_root = _local_target(db, tmp_path, "stream-nas")
        st = db.get(StorageTarget, stream_id)
        stream_target = (st.type, st.config_json, st.id)
    version = _version("app/20260101T000000Z", {"meta.json": "{}", "container.json": "{}"})

    results, _ = storage_sync.sync_backup_with_members(version, [], [], stream_target=stream_target)

    assert results == []  # nothing selected ...
    assert (stream_root / "app/20260101T000000Z/meta.json").exists()  # ... but the catalog marker is there


# ---------- single-file bind mounts ----------

def test_bind_source_is_file_asks_docker_when_path_is_not_visible_locally():
    client = MagicMock()
    client.containers.run.return_value = b"file\n"
    assert backup_engine._bind_source_is_file(client, "/srv/not/in/this/container.yml") is True
    client.containers.run.return_value = b"dir\n"
    assert backup_engine._bind_source_is_file(client, "/srv/not/in/this/dir") is False


def test_bind_source_is_file_uses_local_view_when_available(tmp_path):
    client = MagicMock()
    f = tmp_path / "c.conf"
    f.write_text("x")
    assert backup_engine._bind_source_is_file(client, str(f)) is True
    assert backup_engine._bind_source_is_file(client, str(tmp_path)) is False
    client.containers.run.assert_not_called()


def test_bind_source_is_file_defaults_to_directory_if_docker_fails():
    client = MagicMock()
    client.containers.run.side_effect = RuntimeError("daemon gone")
    assert backup_engine._bind_source_is_file(client, "/nope") is False


# ---------- networks and aliases ----------

def test_recreate_network_keeps_labels_flags_and_subnet():
    client = MagicMock()
    restore_engine._recreate_network(client, "shop_default", {
        "Driver": "bridge", "Internal": True, "Attachable": False,
        "Labels": {"com.docker.compose.network": "default", "com.docker.compose.project": "shop"},
        "IPAM": {"Driver": "default", "Config": [{"Subnet": "172.30.0.0/16", "Gateway": "172.30.0.1"}]},
    })
    args, kwargs = client.networks.create.call_args
    assert args == ("shop_default",)
    assert kwargs["labels"]["com.docker.compose.project"] == "shop"
    assert kwargs["internal"] is True
    assert "attachable" not in kwargs
    assert kwargs["ipam"]["Config"][0]["Subnet"] == "172.30.0.0/16"


def test_recreate_network_falls_back_to_auto_subnet_on_overlap():
    client = MagicMock()
    client.networks.create.side_effect = [RuntimeError("Pool overlaps with other one"), MagicMock()]
    restore_engine._recreate_network(client, "n", {"Driver": "bridge",
                                                   "IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}})
    assert client.networks.create.call_count == 2
    assert "ipam" not in client.networks.create.call_args.kwargs


def _container_json(aliases, ipv4=None):
    return {
        "Id": "5a3c3ae8ca87ffffffff",
        "Name": "/shop-db-1",
        "HostConfig": {"NetworkMode": "shop_default"},
        "NetworkSettings": {"Networks": {"shop_default": {
            "Aliases": aliases, "IPAMConfig": {"IPv4Address": ipv4} if ipv4 else None,
        }}},
    }


def test_attach_networks_restores_compose_service_alias_on_primary_network():
    client, container = MagicMock(), MagicMock()
    net = client.networks.get.return_value
    restore_engine._attach_networks(client, container, _container_json(["db", "5a3c3ae8ca87"]),
                                    {"shop_default": {}}, None)
    net.disconnect.assert_called_once_with(container)
    net.connect.assert_called_once_with(container, aliases=["db"], ipv4_address=None)


def test_attach_networks_retries_without_static_ip_if_taken():
    client, container = MagicMock(), MagicMock()
    net = client.networks.get.return_value
    net.connect.side_effect = [RuntimeError("Address already in use"), None]
    restore_engine._attach_networks(client, container, _container_json(["db"], "172.30.0.5"),
                                    {"shop_default": {}}, None)
    assert net.connect.call_args_list[-1].kwargs == {"aliases": ["db"]}


def test_renamed_copy_does_not_steal_aliases_on_a_shared_network():
    # A parallel "staging_" restore next to the running original must not add
    # a second "db" alias - Docker's DNS would round-robin between the two.
    client, container = MagicMock(), MagicMock()
    net = client.networks.get.return_value
    restore_engine._attach_networks(client, container, _container_json(["db"]),
                                    {"shop_default": {}}, "staging_shop-db-1", created_networks=set())
    net.connect.assert_not_called()
    net.disconnect.assert_not_called()


def test_renamed_copy_gets_aliases_on_a_network_it_created():
    client, container = MagicMock(), MagicMock()
    net = client.networks.get.return_value
    restore_engine._attach_networks(client, container, _container_json(["db"]),
                                    {"shop_default": {}}, "staging_shop-db-1",
                                    created_networks={"shop_default"})
    net.connect.assert_called_once_with(container, aliases=["db"], ipv4_address=None)


# ---------- volumes ----------

def test_create_volume_keeps_compose_labels():
    client = MagicMock()
    restore_engine._create_volume(client, "shop_data", {"Driver": "local",
                                                        "Labels": {"com.docker.compose.volume": "data"}})
    client.volumes.create.assert_called_once_with(name="shop_data",
                                                  labels={"com.docker.compose.volume": "data"})


def test_create_volume_falls_back_to_plain_volume_if_driver_missing():
    client = MagicMock()
    client.volumes.create.side_effect = [RuntimeError("plugin not found"), MagicMock()]
    restore_engine._create_volume(client, "v", {"Driver": "rexray"})
    assert client.volumes.create.call_args_list[-1].kwargs == {"name": "v"}


def test_volume_info_ignores_non_dict_attrs():
    client = MagicMock()  # MagicMock attrs must not end up in meta.json
    assert backup_engine._volume_info(client, "v") == {}
    client.volumes.get.return_value.attrs = {"Driver": "local", "Labels": {"a": "b"}, "Options": None}
    assert backup_engine._volume_info(client, "v") == {"Driver": "local", "Labels": {"a": "b"}}


# ---------- clear message when a host port is taken ----------

def test_restore_explains_a_port_that_is_already_taken(tmp_path, monkeypatch):
    import pytest

    backup_dir = tmp_path / "backups" / "web" / "v1"
    backup_dir.mkdir(parents=True)
    (backup_dir / "container.json").write_text(json.dumps({"Name": "/web", "Config": {}, "HostConfig": {}}))
    (backup_dir / "image.tar").write_bytes(b"img")
    (backup_dir / "meta.json").write_text(json.dumps({"volumes": [], "bind_mounts": []}))

    client = MagicMock()
    loaded = MagicMock()
    loaded.tags = ["nginx:latest"]
    client.images.load.return_value = [loaded]
    client.networks.list.return_value = []
    client.containers.create.return_value.start.side_effect = RuntimeError(
        "Bind for 0.0.0.0:8088 failed: port is already allocated")
    monkeypatch.setattr(restore_engine, "get_client", lambda: client)

    with pytest.raises(RuntimeError, match="Port ist auf diesem System schon belegt"):
        restore_engine.restore_container(backup_dir, new_name="staging_web")


# ---------- restic repo after DBM_ENCRYPTION_KEY was set ----------

def test_restic_repo_keeps_working_after_password_change(tmp_path, monkeypatch):
    # Streamed backups used the auto-generated restic password; once the user
    # sets DBM_ENCRYPTION_KEY the password changes. The existing repo must be
    # reopened (old password adds the new one as a key) instead of "init"
    # failing and the backup silently falling back to an unencrypted stream.
    import shutil
    import subprocess

    import pytest

    from app import restic_engine

    if not shutil.which("restic"):
        pytest.skip("restic not installed")
    monkeypatch.setattr(restic_engine, "BASE_DIR", tmp_path)
    (tmp_path / ".restic_password").write_text("auto-generated-old")
    monkeypatch.delenv("DBM_RESTIC_PASSWORD", raising=False)
    repo = str(tmp_path / "repo")
    restic_engine.init_repo_if_needed(repo, {}, "auto-generated-old")

    restic_engine.init_repo_if_needed(repo, {}, "new-encryption-key")

    ok = subprocess.run(["restic", "-r", repo, "cat", "config", "--no-lock"], capture_output=True,
                        env={**restic_engine._full_env({}, "new-encryption-key")})
    assert ok.returncode == 0
    old = subprocess.run(["restic", "-r", repo, "cat", "config", "--no-lock"], capture_output=True,
                         env={**restic_engine._full_env({}, "auto-generated-old")})
    assert old.returncode == 0  # older backups (password in their meta.json) still restore


def test_restic_repo_with_unknown_password_gives_a_clear_error(tmp_path, monkeypatch):
    import shutil

    import pytest

    from app import restic_engine

    if not shutil.which("restic"):
        pytest.skip("restic not installed")
    monkeypatch.setattr(restic_engine, "BASE_DIR", tmp_path)
    monkeypatch.delenv("DBM_RESTIC_PASSWORD", raising=False)
    repo = str(tmp_path / "repo")
    restic_engine.init_repo_if_needed(repo, {}, "lost-password")
    with pytest.raises(RuntimeError, match="aktuellen Passwort nicht öffnen"):
        restic_engine.init_repo_if_needed(repo, {}, "other")


# ---------- encrypted backups on another host ----------

def test_encrypted_landscape_member_files_are_read(tmp_path, monkeypatch):
    import base64
    import os

    from app import encryption
    from app.routers.backups import _read_member_jsons

    monkeypatch.setenv("DBM_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    land = tmp_path / "land"
    land.mkdir()
    (land / "meta.json").write_text("{}")
    (land / "web.json").write_text(json.dumps({"container_name": "web", "backup_path": "/data/backups/web/v1"}))
    encryption.encrypt_directory_in_place(land)
    assert not list(land.glob("*.json"))

    assert _read_member_jsons(land) == [{"container_name": "web", "backup_path": "/data/backups/web/v1"}]


def test_restoring_encrypted_backup_without_key_says_what_to_do(tmp_path, monkeypatch):
    import pytest

    d = tmp_path / "web" / "v1"
    d.mkdir(parents=True)
    (d / "meta.json.enc").write_bytes(b"x" * 64)
    monkeypatch.delenv("DBM_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DBM_ENCRYPTION_KEY"):
        restore_engine.restore_container(d)
