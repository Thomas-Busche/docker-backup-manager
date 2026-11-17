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


def test_import_prefers_the_target_that_holds_the_data_whatever_the_order(tmp_path):
    # A streamed backup: the streaming target ("cloud") has image + data, the
    # other selected target ("nas") only a metadata copy. Importing nas first
    # used to bind the record to nas for good, and the restore then failed.
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord, StorageTarget
    from app.reset_password import reset_password

    reset_password("catalog-order-user", "supersecret1")
    rel = ("streamy", "20260103T030000Z")
    nas_root, cloud_root = tmp_path / "nas", tmp_path / "cloud"
    for root, with_data in ((nas_root, False), (cloud_root, True)):
        d = root.joinpath(*rel)
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{}")
        if with_data:
            (d / "image.tar").write_bytes(b"x" * 5000)

    db = SessionLocal()
    try:
        ids = []
        for name, root in (("nas-o", nas_root), ("cloud-o", cloud_root)):
            t = StorageTarget(name=name, type="local_path", config_json=json.dumps({"path": str(root)}), enabled=True)
            db.add(t)
            db.commit()
            ids.append(t.id)
    finally:
        db.close()
    nas_id, cloud_id = ids

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "catalog-order-user", "password": "supersecret1"})
        assert client.post(f"/api/settings/storage-targets/{nas_id}/import-catalog").json()["imported"] == 1
        assert client.post(f"/api/settings/storage-targets/{cloud_id}/import-catalog").json()["updated"] == 1
        # And importing the small copy again doesn't flip it back.
        assert client.post(f"/api/settings/storage-targets/{nas_id}/import-catalog").json()["skipped"] == 1

    db = SessionLocal()
    try:
        rec = db.query(BackupRecord).filter(BackupRecord.name == "streamy").one()
        assert rec.streamed_target_id == cloud_id
    finally:
        db.close()
