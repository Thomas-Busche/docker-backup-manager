import json
import time

from fastapi.testclient import TestClient


def _wait_until_gone(path, timeout=5.0):
    # Remote cleanup runs in a background thread so the UI answers immediately -
    # give it a moment to finish instead of asserting right after the request.
    deadline = time.monotonic() + timeout
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not path.exists()


def test_deleting_a_backup_also_removes_it_from_synced_storage_targets(tmp_path):
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord, StorageTarget
    from app.reset_password import reset_password
    from app.storage_sync import _relative_key, sync_local_path

    reset_password("delete-backup-user", "supersecret1")

    backup_dir = BACKUPS_DIR / "app-del" / "v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "f.txt").write_text("data")

    remote_root = tmp_path / "remote"
    sync_local_path(backup_dir, {"path": str(remote_root)})
    relative_key = _relative_key(backup_dir)
    remote_copy = remote_root / relative_key
    assert remote_copy.exists()

    db = SessionLocal()
    try:
        target = StorageTarget(name="local-test-target", type="local_path",
                                config_json=json.dumps({"path": str(remote_root)}), enabled=True)
        db.add(target)
        db.commit()
        db.refresh(target)

        record = BackupRecord(
            backup_type="container", name="app-del", path=str(backup_dir), status="ok",
            synced_target_ids=json.dumps([target.id]),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        record_id = record.id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "delete-backup-user", "password": "supersecret1"})
        resp = client.delete(f"/api/backups/{record_id}")
        assert resp.status_code == 200
        assert "warning" not in resp.json()

    assert _wait_until_gone(remote_copy)
    assert not backup_dir.exists()


def test_deleting_a_backup_also_removes_streamed_volume_data(tmp_path):
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord, StorageTarget
    from app.reset_password import reset_password
    from app.storage_sync import stream_upload_to_target

    reset_password("delete-streamed-user", "supersecret1")

    # Local backup only has the small stuff (meta.json/image.tar); the volume
    # itself was streamed straight to the target and never touched local disk.
    backup_dir = BACKUPS_DIR / "app-streamed" / "v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "meta.json").write_text("{}")

    remote_root = tmp_path / "remote"
    stream_upload_to_target(
        "local_path", json.dumps({"path": str(remote_root)}), "app-streamed/v1/volumes/data.tar.gz",
        iter([b"volume-bytes"]),
    )
    remote_volume_copy = remote_root / "app-streamed" / "v1" / "volumes" / "data.tar.gz"
    assert remote_volume_copy.exists()

    db = SessionLocal()
    try:
        target = StorageTarget(name="stream-target", type="local_path",
                                config_json=json.dumps({"path": str(remote_root)}), enabled=True)
        db.add(target)
        db.commit()
        db.refresh(target)

        record = BackupRecord(
            backup_type="container", name="app-streamed", path=str(backup_dir), status="ok",
            streamed_target_id=target.id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        record_id = record.id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "delete-streamed-user", "password": "supersecret1"})
        resp = client.delete(f"/api/backups/{record_id}")
        assert resp.status_code == 200
        assert "warning" not in resp.json()

    assert _wait_until_gone(remote_volume_copy)
    assert not backup_dir.exists()
