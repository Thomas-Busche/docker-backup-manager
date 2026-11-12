"""Datenbankwachstum: Log-Aufbewahrung, Komprimierung, gekürzte Einträge."""
import datetime
import logging

from fastapi.testclient import TestClient

from app import event_log
from app.clock import utcnow
from app.database import SessionLocal, compact_db, engine, init_db
from app.models import LogEntry


def _clear_logs():
    db = SessionLocal()
    try:
        db.query(LogEntry).delete()
        db.commit()
    finally:
        db.close()


def test_init_db_switches_to_incremental_auto_vacuum_and_wal():
    init_db()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA auto_vacuum").scalar() == 2
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"


def test_settings_are_persisted_and_survive_module_defaults():
    event_log.save_settings(30, 1234)
    assert event_log.get_settings() == {"retention_days": 30, "max_entries": 1234}
    event_log.save_settings(90, 50000)


def test_purge_excess_entries_keeps_newest():
    _clear_logs()
    for i in range(10):
        event_log.log_event("backup", f"msg-{i}")
    deleted = event_log.purge_excess_entries(3)
    assert deleted == 7
    remaining = [e.message for e in event_log.list_entries(limit=100)]
    assert sorted(remaining) == ["msg-7", "msg-8", "msg-9"]
    assert event_log.purge_excess_entries(0) == 0


def test_run_rotation_applies_both_limits_and_shrinks_file():
    _clear_logs()
    event_log.save_settings(7, 100)
    db = SessionLocal()
    try:
        old = utcnow() - datetime.timedelta(days=30)
        db.add_all([LogEntry(category="backup", message="x" * 3000, created_at=old) for _ in range(500)])
        db.commit()
    finally:
        db.close()
    for i in range(150):
        event_log.log_event("backup", f"new-{i}")
    compact_db(full=True)

    result = event_log.run_rotation(full_vacuum=True)
    assert result["deleted"] == 500 + 50
    assert event_log.count_entries() == 100
    assert result["size_after"] < result["size_before"]
    event_log.save_settings(90, 50000)


def test_long_messages_are_truncated():
    _clear_logs()
    event_log.log_event("backup", "y" * 10000)
    msg = event_log.list_entries(limit=1)[0].message
    assert len(msg) < 4100 and msg.endswith("(gekürzt)")


def test_db_handler_stores_only_exception_line_not_traceback():
    _clear_logs()
    handler = event_log.DBLogHandler()
    log = logging.getLogger("dbm.backup_engine")
    log.addHandler(handler)
    try:
        try:
            raise ValueError("kaputt")
        except ValueError:
            log.exception("Backup fehlgeschlagen")
    finally:
        log.removeHandler(handler)
    msg = event_log.list_entries(limit=1)[0].message
    assert msg == "Backup fehlgeschlagen - ValueError: kaputt"
    assert "Traceback" not in msg


def test_space_info_is_not_written_to_db_log():
    _clear_logs()
    handler = event_log.DBLogHandler()
    handler.setLevel(logging.INFO)
    log = logging.getLogger("dbm.space")
    log.addHandler(handler)
    try:
        from app import storage_sync
        storage_sync._space_logger.debug("rclone about x: total=1")
    finally:
        log.removeHandler(handler)
    assert event_log.count_entries() == 0


def test_netpulse_inventory_warning_is_logged_once(monkeypatch):
    from app import netpulse

    calls = []
    monkeypatch.setattr(netpulse, "_inventory_last_problem", None)
    monkeypatch.setattr(netpulse, "inventory", lambda: [])
    monkeypatch.setattr(netpulse.logger, "warning", lambda *a, **k: calls.append(("w", a)))
    monkeypatch.setattr(netpulse.logger, "info", lambda *a, **k: calls.append(("i", a)))

    def fail(*_a, **_k):
        raise netpulse.NetPulseError("nicht erreichbar")

    cfg = {"enabled": True, "url": "http://np", "token": "t"}
    monkeypatch.setattr(netpulse, "_request", fail)
    for _ in range(5):
        netpulse.push_inventory(cfg)
    assert [c[0] for c in calls] == ["w"]

    monkeypatch.setattr(netpulse, "_request", lambda *a, **k: {"subjects": []})
    netpulse.push_inventory(cfg)
    netpulse.push_inventory(cfg)
    assert [c[0] for c in calls] == ["w", "i"]


def test_logs_settings_api_persists_and_reports_db_size():
    from app.main import app
    from app.reset_password import reset_password

    reset_password("dbsize-user", "supersecret1")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "dbsize-user", "password": "supersecret1"})
        resp = client.put("/api/logs/settings", json={"retention_days": 14, "max_entries": 2000})
        assert resp.status_code == 200
        got = client.get("/api/logs/settings").json()
        assert got["retention_days"] == 14 and got["max_entries"] == 2000
        assert got["db_size_bytes"] > 0
        # max_entries is optional for older clients
        assert client.put("/api/logs/settings", json={"retention_days": 20}).json()["max_entries"] == 2000
        assert client.put("/api/logs/settings", json={"retention_days": 1, "max_entries": -1}).status_code == 400
        purge = client.post("/api/logs/purge").json()
        assert {"deleted", "size_before", "size_after"} <= purge.keys()
    event_log.save_settings(90, 50000)


def test_nightly_incremental_vacuum_gives_space_back():
    _clear_logs()
    init_db()
    db = SessionLocal()
    try:
        db.add_all([LogEntry(category="backup", message="z" * 3000) for _ in range(1500)])
        db.commit()
    finally:
        db.close()
    compact_db(full=False)
    _clear_logs()
    result = compact_db(full=False)
    # ~4.5 MB of log text was deleted - the file must actually get smaller, not just keep free pages
    assert result["size_before"] - result["size_after"] > 3_000_000
