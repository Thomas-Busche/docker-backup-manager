from fastapi.testclient import TestClient


def test_list_backups_removes_ok_records_whose_directory_is_gone_but_keeps_failed_ones():
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord
    from app.reset_password import reset_password

    reset_password("self-heal-user", "supersecret1")

    real_dir = BACKUPS_DIR / "app-real" / "v1"
    real_dir.mkdir(parents=True, exist_ok=True)
    (real_dir / "f.txt").write_text("data")

    missing_dir = BACKUPS_DIR / "app-missing" / "v1"  # never created on disk

    db = SessionLocal()
    try:
        ok_present = BackupRecord(backup_type="container", name="app-real", path=str(real_dir), status="ok")
        ok_missing = BackupRecord(backup_type="container", name="app-missing", path=str(missing_dir), status="ok")
        failed_missing = BackupRecord(backup_type="container", name="app-failed", path=str(missing_dir),
                                       status="failed", error="boom")
        db.add_all([ok_present, ok_missing, failed_missing])
        db.commit()
        ok_present_id, ok_missing_id, failed_missing_id = ok_present.id, ok_missing.id, failed_missing.id
    finally:
        db.close()

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "self-heal-user", "password": "supersecret1"})
        resp = client.get("/api/backups")
        assert resp.status_code == 200
        groups = resp.json()["groups"]

        all_ids = {v["id"] for versions in groups.values() for v in versions}
        assert ok_present_id in all_ids
        assert ok_missing_id not in all_ids  # stale "ok" record with no directory - self-healed away
        # Failed records are hidden from the overview (they have nothing to restore),
        # but must not be self-healed away: the DB check below still requires them.
        assert failed_missing_id not in all_ids

    db = SessionLocal()
    try:
        assert db.query(BackupRecord).filter(BackupRecord.id == ok_missing_id).first() is None
        assert db.query(BackupRecord).filter(BackupRecord.id == failed_missing_id).first() is not None
    finally:
        db.close()
