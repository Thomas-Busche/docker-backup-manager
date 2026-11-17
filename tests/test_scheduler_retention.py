import json


def test_apply_retention_keeps_earlier_deletions_when_a_later_one_fails(monkeypatch):
    from app import scheduler
    from app.database import SessionLocal
    from app.models import BackupRecord, Schedule

    db = SessionLocal()
    try:
        records = []
        for i in range(3):
            r = BackupRecord(backup_type="container", name="retention-app", path=f"/fake/path-{i}", status="ok")
            db.add(r)
            records.append(r)
        db.commit()
        for r in records:
            db.refresh(r)
        ids = [r.id for r in records]

        sched = Schedule(
            name="retention-app", target_type="container", target_ref="retention-app",
            cron_expression="0 3 * * *", retention_count=0, retention_days=0,
            storage_target_ids="[]",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)

        # Force-prune all 3, but make the second deletion raise - simulating a
        # permission error or similar mid-batch failure on a real filesystem.
        monkeypatch.setattr(scheduler, "versions_to_keep",
                             lambda versions, *a, **k: {v.id for v in versions if v.id not in ids})

        call_order = []

        def fake_delete_backup(path):
            call_order.append(path)
            if path == f"/fake/path-1":
                raise PermissionError("simulated failure")

        monkeypatch.setattr(scheduler.backup_engine, "delete_backup", fake_delete_backup)

        scheduler._apply_retention(db, sched)

        remaining = db.query(BackupRecord).filter(BackupRecord.id.in_(ids)).all()
        remaining_paths = {r.path for r in remaining}
        # path-0 was deleted successfully before the failure and must stay deleted
        # (not rolled back just because path-1 failed afterwards).
        assert "/fake/path-0" not in remaining_paths
        # path-1's deletion raised - its record must still be there (not silently dropped).
        assert "/fake/path-1" in remaining_paths
        # path-2 comes after the failure in iteration order and should still be processed.
        assert "/fake/path-2" not in remaining_paths
    finally:
        db.close()
