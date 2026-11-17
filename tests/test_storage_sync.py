import json
from pathlib import Path

import pytest

from app.config import BACKUPS_DIR
from app.storage_sync import (
    _relative_key, check_target_connection, delete_from_target, download_from_target,
    download_full_backup_from_target, list_backups_on_target, stream_upload_to_target, sync_local_path,
)


def test_sync_local_path_copies_tree(tmp_path: Path):
    backup_dir = BACKUPS_DIR / "app1" / "20240101T000000Z"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "meta.json").write_text('{"ok": true}')

    dest_root = tmp_path / "remote"
    sync_local_path(backup_dir, {"path": str(dest_root)})

    copied = dest_root / "app1" / "20240101T000000Z" / "meta.json"
    assert copied.exists()
    assert json.loads(copied.read_text()) == {"ok": True}


def test_sync_local_path_overwrites_existing_files_in_version(tmp_path: Path):
    backup_dir = BACKUPS_DIR / "app2" / "v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "f.txt").write_text("new")

    dest_root = tmp_path / "remote2"
    old_dest = dest_root / "app2" / "v1"
    old_dest.mkdir(parents=True)
    (old_dest / "f.txt").write_text("old")

    sync_local_path(backup_dir, {"path": str(dest_root)})

    assert (old_dest / "f.txt").read_text() == "new"


def test_sync_local_path_keeps_files_streamed_there_earlier(tmp_path: Path):
    # A streamed backup writes image.tar straight to the target; the local
    # version folder only holds the metadata. Syncing the metadata afterwards
    # must not wipe the image (it used to: restore then failed with
    # "No such file or directory: .../image.tar").
    backup_dir = BACKUPS_DIR / "app3" / "20240101T000000Z"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "meta.json").write_text("{}")

    dest_root = tmp_path / "remote3"
    remote_version = dest_root / "app3" / "20240101T000000Z"
    stream_upload_to_target("local_path", json.dumps({"path": str(dest_root)}),
                            "app3/20240101T000000Z/image.tar", iter([b"image"]))

    sync_local_path(backup_dir, {"path": str(dest_root)})

    assert (remote_version / "image.tar").read_bytes() == b"image"
    assert (remote_version / "meta.json").exists()


def test_check_target_connection_local_path_write_check(tmp_path: Path):
    target_dir = tmp_path / "share"
    check_target_connection("local_path", json.dumps({"path": str(target_dir)}))
    assert target_dir.exists()
    assert not (target_dir / ".dbm_write_test").exists()


def test_delete_from_target_local_path_removes_synced_copy(tmp_path: Path):
    backup_dir = BACKUPS_DIR / "app3" / "v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "f.txt").write_text("data")

    dest_root = tmp_path / "remote3"
    sync_local_path(backup_dir, {"path": str(dest_root)})
    relative_key = _relative_key(backup_dir)
    assert (dest_root / relative_key).exists()

    delete_from_target("local_path", json.dumps({"path": str(dest_root)}), relative_key)
    assert not (dest_root / relative_key).exists()


def test_delete_from_target_local_path_missing_copy_is_a_noop(tmp_path: Path):
    dest_root = tmp_path / "remote4"
    delete_from_target("local_path", json.dumps({"path": str(dest_root)}), "never-synced/v1")  # must not raise


def test_stream_upload_to_target_local_path_writes_chunks(tmp_path: Path):
    dest_root = tmp_path / "remote5"
    stream_upload_to_target(
        "local_path", json.dumps({"path": str(dest_root)}), "app/v1/volumes/data.tar.gz",
        iter([b"hello ", b"world"]),
    )
    assert (dest_root / "app/v1/volumes/data.tar.gz").read_bytes() == b"hello world"


def test_stream_upload_to_target_rejects_unsupported_type():
    with pytest.raises(ValueError):
        stream_upload_to_target("google_drive", "{}", "app/v1/volumes/data.tar.gz", iter([b"x"]))


def test_download_from_target_local_path_roundtrip(tmp_path: Path):
    dest_root = tmp_path / "remote6"
    stream_upload_to_target(
        "local_path", json.dumps({"path": str(dest_root)}), "app/v1/volumes/data.tar.gz",
        iter([b"payload-bytes"]),
    )
    downloaded = tmp_path / "downloaded.tar.gz"
    download_from_target("local_path", json.dumps({"path": str(dest_root)}), "app/v1/volumes/data.tar.gz", downloaded)
    assert downloaded.read_bytes() == b"payload-bytes"


def test_download_from_target_rejects_unsupported_type(tmp_path: Path):
    with pytest.raises(ValueError):
        download_from_target("onedrive", "{}", "app/v1/volumes/data.tar.gz", tmp_path / "out.tar.gz")


def test_list_backups_on_target_finds_container_and_landscape_backups(tmp_path: Path):
    root = tmp_path / "target"
    container_dir = root / "myapp" / "20260101T030000Z"
    container_dir.mkdir(parents=True)
    (container_dir / "meta.json").write_text("{}")
    (container_dir / "image.tar").write_bytes(b"x" * 100)

    landscape_dir = root / "_landscapes" / "immich" / "20260102T030000Z"
    landscape_dir.mkdir(parents=True)
    (landscape_dir / "meta.json").write_text("{}")

    # A stray file that isn't a backup (no meta.json alongside it) must be ignored.
    (root / "randomfile.txt").write_text("not a backup")

    entries = list_backups_on_target("local_path", json.dumps({"path": str(root)}))
    by_name = {e["name"]: e for e in entries}

    assert by_name["myapp"]["backup_type"] == "container"
    assert by_name["myapp"]["relative_key"] == "myapp/20260101T030000Z"
    assert by_name["myapp"]["size_bytes"] >= 100
    assert by_name["immich"]["backup_type"] == "landscape"
    assert by_name["immich"]["relative_key"] == "_landscapes/immich/20260102T030000Z"


def test_list_backups_on_target_recognizes_encrypted_meta(tmp_path: Path):
    root = tmp_path / "target"
    backup_dir = root / "myapp" / "20260101T030000Z"
    backup_dir.mkdir(parents=True)
    (backup_dir / "meta.json.enc").write_bytes(b"encrypted-bytes")

    entries = list_backups_on_target("local_path", json.dumps({"path": str(root)}))
    assert len(entries) == 1
    assert entries[0]["name"] == "myapp"


def test_list_backups_on_target_missing_root_returns_empty(tmp_path: Path):
    assert list_backups_on_target("local_path", json.dumps({"path": str(tmp_path / "nope")})) == []


def test_list_backups_on_target_rejects_unsupported_type():
    with pytest.raises(ValueError):
        list_backups_on_target("onedrive", "{}")


def test_download_full_backup_from_target_local_path_recreates_tree(tmp_path: Path):
    root = tmp_path / "target"
    backup_dir = root / "myapp" / "20260101T030000Z"
    (backup_dir / "volumes").mkdir(parents=True)
    (backup_dir / "meta.json").write_text('{"a": 1}')
    (backup_dir / "volumes" / "data.tar.gz").write_bytes(b"volume-bytes")

    dest = tmp_path / "restored" / "myapp" / "20260101T030000Z"
    download_full_backup_from_target(
        "local_path", json.dumps({"path": str(root)}), "myapp/20260101T030000Z", dest,
    )

    assert (dest / "meta.json").read_text() == '{"a": 1}'
    assert (dest / "volumes" / "data.tar.gz").read_bytes() == b"volume-bytes"


def test_download_full_backup_from_target_rejects_unsupported_type(tmp_path: Path):
    with pytest.raises(ValueError):
        download_full_backup_from_target("onedrive", "{}", "myapp/v1", tmp_path / "out")
