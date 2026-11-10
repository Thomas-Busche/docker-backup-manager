import json

from fastapi.testclient import TestClient


def test_import_catalog_creates_records_and_skips_duplicates_on_rerun(tmp_path):
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord, StorageTarget
    from app.reset_password import reset_password

    reset_password("catalog-import-user", "supersecret1")

    # Simulate a target that already holds two backup versions of "myapp".
    target_root = tmp_path / "nas"
    v1 = target_root / "myapp" / "20260101T030000Z"
    v2 = target_root / "myapp" / "20260102T030000Z"
    for d in (v1, v2):
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}")
        (d / "image.tar").write_bytes(b"x" * 50)

    db = SessionLocal()
    try:
        target = StorageTarget(name="nas", type="local_path",
                                config_json=json.dumps({"path": str(target_root)}), enabled=True)
        db.add(target)
        db.commit()
        db.refresh(target)
        target_id = target.id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "catalog-import-user", "password": "supersecret1"})

        resp = client.post(f"/api/settings/storage-targets/{target_id}/import-catalog")
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] == 2
        assert data["imported"] == 2
        assert data["skipped"] == 0

        # Running it again must not create duplicates.
        resp2 = client.post(f"/api/settings/storage-targets/{target_id}/import-catalog")
        assert resp2.json() == {"found": 2, "imported": 0, "updated": 0, "skipped": 2}

    db = SessionLocal()
    try:
        records = db.query(BackupRecord).filter(BackupRecord.name == "myapp").all()
        assert len(records) == 2
        for r in records:
            assert r.streamed_target_id == target_id
        # Cataloging is metadata-only - nothing gets downloaded until an actual restore.
        assert not (BACKUPS_DIR / "myapp").exists()
    finally:
        db.close()


def test_import_catalog_unknown_target_404():
    from app.main import app
    from app.reset_password import reset_password

    reset_password("catalog-import-user-2", "supersecret1")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "catalog-import-user-2", "password": "supersecret1"})
        resp = client.post("/api/settings/storage-targets/999999/import-catalog")
        assert resp.status_code == 404
