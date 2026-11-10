from unittest.mock import MagicMock

from fastapi.testclient import TestClient


def test_schedule_api_round_trips_project_filter():
    from app.main import app
    from app.reset_password import reset_password

    reset_password("project-filter-user", "supersecret1")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "project-filter-user", "password": "supersecret1"})

        create = client.post("/api/schedules", json={
            "name": "immich-backup", "target_type": "landscape", "project_filter": "immich",
            "cron_expression": "0 3 * * *",
        })
        assert create.status_code == 200
        schedule_id = create.json()["id"]

        listed = client.get("/api/schedules").json()["schedules"]
        sched = next(s for s in listed if s["id"] == schedule_id)
        assert sched["project_filter"] == "immich"
        assert sched["target_type"] == "landscape"


def test_schedule_api_round_trips_name_contains():
    from app.main import app
    from app.reset_password import reset_password

    reset_password("name-contains-user", "supersecret1")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "name-contains-user", "password": "supersecret1"})

        create = client.post("/api/schedules", json={
            "name": "aio-backup", "target_type": "landscape", "name_contains": "nextcloud-aio",
            "cron_expression": "0 3 * * *",
        })
        assert create.status_code == 200
        schedule_id = create.json()["id"]

        listed = client.get("/api/schedules").json()["schedules"]
        sched = next(s for s in listed if s["id"] == schedule_id)
        assert sched["name_contains"] == "nextcloud-aio"


def test_schedule_api_round_trips_stop_containers():
    from app.main import app
    from app.reset_password import reset_password

    reset_password("stop-containers-user", "supersecret1")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "stop-containers-user", "password": "supersecret1"})

        create = client.post("/api/schedules", json={
            "name": "db-backup", "target_type": "landscape", "stop_containers": True,
            "cron_expression": "0 3 * * *",
        })
        assert create.status_code == 200
        schedule_id = create.json()["id"]

        listed = client.get("/api/schedules").json()["schedules"]
        sched = next(s for s in listed if s["id"] == schedule_id)
        assert sched["stop_containers"] is True


def test_run_schedule_passes_stop_containers_to_backup_landscape(monkeypatch):
    from app import scheduler
    from app.backup_engine import BackupResult
    from app.database import SessionLocal
    from app.models import Schedule

    db = SessionLocal()
    try:
        sched = Schedule(
            name="db-backup", target_type="landscape", stop_containers=True,
            cron_expression="0 3 * * *", retention_count=0, retention_days=0, storage_target_ids="[]",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
        schedule_id = sched.id
    finally:
        db.close()

    captured = {}

    def fake_backup_landscape(dest_root, project_filter=None, name_contains=None, label=None, on_progress=None, stream_target=None, should_cancel=None, stop_containers=None, on_bytes=None, **kwargs):
        captured["stop_containers"] = stop_containers
        return BackupResult(ok=True, name=label, path=dest_root / "_landscapes" / "db-backup" / "v1", size_bytes=1)

    monkeypatch.setattr(scheduler.backup_engine, "backup_landscape", fake_backup_landscape)
    monkeypatch.setattr(scheduler.storage_sync, "sync_to_selected_targets", lambda *a, **k: [])

    scheduler.run_schedule(schedule_id)

    assert captured["stop_containers"] is True


def test_run_schedule_writes_log_entries_for_start_and_success(monkeypatch):
    from app import event_log, scheduler
    from app.backup_engine import BackupResult
    from app.database import SessionLocal
    from app.models import Schedule

    db = SessionLocal()
    try:
        sched = Schedule(
            name="logged-backup", target_type="landscape",
            cron_expression="0 3 * * *", retention_count=0, retention_days=0, storage_target_ids="[]",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
        schedule_id = sched.id
    finally:
        db.close()

    def fake_backup_landscape(dest_root, project_filter=None, name_contains=None, label=None, on_progress=None, stream_target=None, should_cancel=None, stop_containers=None, on_bytes=None, **kwargs):
        return BackupResult(ok=True, name=label, path=dest_root / "_landscapes" / "logged" / "v1", size_bytes=1)

    monkeypatch.setattr(scheduler.backup_engine, "backup_landscape", fake_backup_landscape)
    monkeypatch.setattr(scheduler.storage_sync, "sync_to_selected_targets", lambda *a, **k: [])

    scheduler.run_schedule(schedule_id)

    messages = [e.message for e in event_log.list_entries(limit=50)]
    assert any("logged-backup" in m and "gestartet" in m for m in messages)
    assert any("logged-backup" in m and "erfolgreich" in m for m in messages)


def test_run_schedule_passes_name_contains_to_backup_landscape(monkeypatch):
    from app import scheduler
    from app.backup_engine import BackupResult
    from app.database import SessionLocal
    from app.models import Schedule

    db = SessionLocal()
    try:
        sched = Schedule(
            name="aio-backup", target_type="landscape", name_contains="nextcloud-aio",
            cron_expression="0 3 * * *", retention_count=0, retention_days=0, storage_target_ids="[]",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
        schedule_id = sched.id
    finally:
        db.close()

    captured = {}

    def fake_backup_landscape(dest_root, project_filter=None, name_contains=None, label=None, on_progress=None, stream_target=None, should_cancel=None, stop_containers=None, on_bytes=None, **kwargs):
        captured["name_contains"] = name_contains
        return BackupResult(ok=True, name=label, path=dest_root / "_landscapes" / "aio" / "v1", size_bytes=1)

    monkeypatch.setattr(scheduler.backup_engine, "backup_landscape", fake_backup_landscape)
    monkeypatch.setattr(scheduler.storage_sync, "sync_to_selected_targets", lambda *a, **k: [])

    scheduler.run_schedule(schedule_id)

    assert captured["name_contains"] == "nextcloud-aio"


def test_run_schedule_passes_project_filter_to_backup_landscape(monkeypatch):
    from app import scheduler
    from app.backup_engine import BackupResult
    from app.database import SessionLocal
    from app.models import Schedule

    db = SessionLocal()
    try:
        sched = Schedule(
            name="nextcloud-backup", target_type="landscape", project_filter="nextcloud",
            cron_expression="0 3 * * *", retention_count=0, retention_days=0, storage_target_ids="[]",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
        schedule_id = sched.id
    finally:
        db.close()

    captured = {}

    def fake_backup_landscape(dest_root, project_filter=None, name_contains=None, label=None, on_progress=None, stream_target=None, should_cancel=None, stop_containers=None, on_bytes=None, **kwargs):
        captured["project_filter"] = project_filter
        return BackupResult(ok=True, name=label, path=dest_root / "_landscapes" / "nextcloud" / "v1", size_bytes=1)

    monkeypatch.setattr(scheduler.backup_engine, "backup_landscape", fake_backup_landscape)
    monkeypatch.setattr(scheduler.storage_sync, "sync_to_selected_targets", lambda *a, **k: [])

    scheduler.run_schedule(schedule_id)

    assert captured["project_filter"] == "nextcloud"
