"""Catalog side of incremental chains: dependencies protect chain members
from retention and manual deletion, synthetic fulls get their own records,
merged incrementals can be dropped right away."""
import datetime
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


def _rec(db, name, ts, level, depends=(), chain_id="c1"):
    from app.config import BACKUPS_DIR
    from app.models import BackupRecord
    d = BACKUPS_DIR / name / ts
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text("{}")
    r = BackupRecord(backup_type="container", name=name, path=str(d), status="ok", backup_level=level,
                     chain_id=chain_id, depends_json=json.dumps([str(BACKUPS_DIR / name / t) for t in depends]),
                     created_at=datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ"))
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def test_retention_keeps_the_chain_base_a_kept_incremental_needs(monkeypatch):
    from app import scheduler
    from app.database import SessionLocal, init_db
    from app.models import BackupRecord, Schedule

    init_db()
    name = f"ret-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(scheduler, "_cleanup_remote_for_record", lambda r, db: None, raising=False)
    import app.routers.backups as backups_router
    monkeypatch.setattr(backups_router, "_cleanup_remote_for_record", lambda r, db: None)
    with SessionLocal() as db:
        # old chain: full + 2 incrementals; new chain: synthetic full + 1 incremental
        f1 = _rec(db, name, "20260101T000000Z", "full")
        i1 = _rec(db, name, "20260102T000000Z", "incremental", ["20260101T000000Z"])
        i2 = _rec(db, name, "20260103T000000Z", "incremental", ["20260101T000000Z", "20260102T000000Z"])
        s1 = _rec(db, name, "20260104T000000Z", "synthetic_full", chain_id="c2")
        i3 = _rec(db, name, "20260105T000000Z", "incremental", ["20260104T000000Z"], chain_id="c2")
        ids = {k: r.id for k, r in dict(f1=f1, i1=i1, i2=i2, s1=s1, i3=i3).items()}
        paths = {k: r.path for k, r in dict(f1=f1, i1=i1, i2=i2, s1=s1, i3=i3).items()}
        sched = Schedule(name=name, target_type="container", target_ref=name, cron_expression="0 3 * * *",
                         retention_count=4, retention_days=0, storage_target_ids="[]")
        db.add(sched)
        db.commit()
        # keep 4: i1 is out of retention, but i2 still needs it -> nothing deleted
        scheduler._apply_retention(db, sched)
        assert {r.id for r in db.query(BackupRecord).filter(BackupRecord.name == name)} == set(ids.values())
        # keep 1: only i3 - it needs the synthetic full; the old chain goes completely
        sched.retention_count = 1
        db.commit()
        scheduler._apply_retention(db, sched)
        left = {r.id for r in db.query(BackupRecord).filter(BackupRecord.name == name)}
    assert left == {ids["s1"], ids["i3"]}
    assert not Path(paths["f1"]).exists() and Path(paths["s1"]).exists()


def test_deleting_a_chain_base_is_refused_unless_the_dependents_go_too(monkeypatch):
    from app.database import SessionLocal, init_db
    from app.main import app
    from app.models import BackupRecord
    from app.reset_password import reset_password

    init_db()
    reset_password("chain-user", "supersecret1")
    name = f"del-{uuid.uuid4().hex[:6]}"
    with SessionLocal() as db:
        full = _rec(db, name, "20260101T000000Z", "full")
        i1 = _rec(db, name, "20260102T000000Z", "incremental", ["20260101T000000Z"])
        other = _rec(db, name, "20260103T000000Z", "full", chain_id="c2")
        full, i1, other = (type("R", (), {"id": r.id}) for r in (full, i1, other))
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "chain-user", "password": "supersecret1"})
        resp = client.delete(f"/api/backups/{full.id}")
        assert resp.status_code == 409
        assert [d["id"] for d in resp.json()["detail"]["dependents"]] == [i1.id]

        listing = client.get("/api/backups").json()["groups"][name]
        by_id = {v["id"]: v for v in listing}
        assert by_id[full.id]["dependents_count"] == 1
        assert by_id[i1.id]["depends_on_ids"] == [full.id]
        assert by_id[i1.id]["backup_level"] == "incremental"

        # bulk delete of only the full skips it; full + incremental together works
        assert client.post("/api/backups/bulk-delete", json={"ids": [full.id]}).json()["skipped_chain_bases"] == [full.id]
        assert client.delete(f"/api/backups/{full.id}?with_dependents=true").status_code == 200
    with SessionLocal() as db:
        left = {r.id for r in db.query(BackupRecord).filter(BackupRecord.name == name)}
    assert left == {other.id}


def test_persist_result_records_synthetic_full_and_prunes_merged_incrementals(monkeypatch):
    from app import backup_records, storage_sync
    from app.backup_engine import BackupResult
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal, init_db
    from app.models import BackupRecord

    init_db()
    monkeypatch.setattr(storage_sync, "sync_backup_with_members", lambda *a, **k: ([], {}))
    name = f"syn-{uuid.uuid4().hex[:6]}"
    deleted = []

    def fake_delete(r, db):
        deleted.append(r.path)
        db.delete(r)

    with SessionLocal() as db:
        full = _rec(db, name, "20260101T000000Z", "full")
        i1 = _rec(db, name, "20260102T000000Z", "incremental", ["20260101T000000Z"])
        full_path, i1_path = full.path, i1.path
        latest_dir = BACKUPS_DIR / name / "20260103T000000Z"
        latest_dir.mkdir(parents=True)
        synth_dir = BACKUPS_DIR / name / "20260103T000001Z"
        synth_dir.mkdir(parents=True)
        synth = BackupResult(ok=True, name=name, path=synth_dir, level="synthetic_full", chain_id="c9",
                             data_as_of="2026-01-03T00:00:00Z")
        result = BackupResult(ok=True, name=name, path=latest_dir, containers=[name], level="incremental",
                              chain_id="c1", depends_on=[f"{name}/20260101T000000Z", f"{name}/20260102T000000Z"],
                              synthetic=synth,
                              merged_keys=[f"{name}/20260101T000000Z", f"{name}/20260102T000000Z",
                                           f"{name}/20260103T000000Z"])
        record = backup_records.persist_result(db, result, "container", [], keep_merged_incrementals=False,
                                               delete_record=fake_delete)
        db.commit()
        rows = {r.path: r for r in db.query(BackupRecord).filter(BackupRecord.name == name)}
        assert record.backup_level == "incremental"
        assert rows[str(synth_dir)].backup_level == "synthetic_full"
        assert rows[str(synth_dir)].data_as_of == datetime.datetime(2026, 1, 3)
    # both incrementals merged into the synthetic full are gone, the base full stays
    assert set(deleted) == {str(latest_dir), i1_path}
    assert full_path in rows


def test_incremental_falls_back_from_plain_streaming_to_upload(monkeypatch):
    from app import backup_records
    from app.backup_engine import BackupResult
    r = BackupResult(ok=True, name="x", path=Path("/x/y/z"), fallback_sync_target_id=5)
    assert backup_records.effective_target_ids([r], [1, 2]) == [1, 2, 5]
    assert backup_records.effective_target_ids([r], [5]) == [5]
    assert backup_records.effective_target_ids([r], None) is None


def test_schedule_api_stores_chain_settings():
    from app.database import init_db
    from app.main import app
    from app.reset_password import reset_password

    init_db()
    reset_password("chain-sched-user", "supersecret1")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "chain-sched-user", "password": "supersecret1"})
        body = {"name": f"inc-{uuid.uuid4().hex[:6]}", "target_type": "container", "target_ref": "web",
                "cron_expression": "0 3 * * *", "backup_mode": "incremental", "synthetic_full_every": 5,
                "keep_merged_incrementals": False, "enabled": False}
        assert client.post("/api/schedules", json=body).status_code == 200
        s = next(x for x in client.get("/api/schedules").json()["schedules"] if x["name"] == body["name"])
        assert (s["backup_mode"], s["synthetic_full_every"], s["keep_merged_incrementals"]) == ("incremental", 5, False)
        bad = dict(body, name=body["name"] + "b", backup_mode="differential")
        assert client.post("/api/schedules", json=bad).status_code == 400


def test_verify_a_group_backup_checks_every_member(monkeypatch):
    import time
    from app import chain, job_tracker
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal, init_db
    from app.main import app
    from app.models import BackupRecord
    from app.reset_password import reset_password

    init_db()
    reset_password("chain-verify-user", "supersecret1")
    tag = uuid.uuid4().hex[:6]
    checked = []

    def fake_verify(path, fetch_missing=None, on_log=None, **kw):
        checked.append(Path(path).parent.name)
        ok = not Path(path).parent.name.startswith("bad")
        return {"ok": ok, "errors": [] if ok else ["Prüfsumme falsch"], "checked_files": 3,
                "checked_archives": 1, "dependencies": []}

    monkeypatch.setattr(chain, "verify", fake_verify)
    with SessionLocal() as db:
        lsc_dir = BACKUPS_DIR / f"grp-{tag}" / "20260101T000000Z"
        lsc_dir.mkdir(parents=True)
        lsc = BackupRecord(backup_type="landscape", name=f"grp-{tag}", path=str(lsc_dir), status="ok",
                           containers_json=json.dumps([f"web-{tag}", f"bad-{tag}"]),
                           created_at=datetime.datetime(2026, 1, 1))
        db.add(lsc)
        db.commit()
        _rec(db, f"web-{tag}", "20260101T000010Z", "full")
        _rec(db, f"bad-{tag}", "20260101T000020Z", "full")
        lsc_id = lsc.id
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "chain-verify-user", "password": "supersecret1"})
        resp = client.post(f"/api/backups/{lsc_id}/verify")
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]
        for _ in range(100):
            job = job_tracker.get_job(job_id)
            if job.status not in ("running", "pending"):
                break
            time.sleep(0.05)
    assert sorted(checked) == sorted([f"web-{tag}", f"bad-{tag}"])
    assert job.status == "failed"
    logs = " ".join(entry["msg"] for entry in job_tracker.get_logs(job_id))
    assert f"bad-{tag}: FEHLER" in logs and f"web-{tag}: in Ordnung" in logs and "2 Container" in logs
