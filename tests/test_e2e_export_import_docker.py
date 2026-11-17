"""End-to-end: "Komplett herunterladen" + import against a REAL Docker daemon.

Opt-in:  DBM_E2E_DOCKER=1 pytest tests/test_e2e_export_import_docker.py

A backup point is exported to one file, then everything else is wiped (local
folders, catalog, storage target) - like a new host that only has that file.
After the import the backup restores bit-identically. Covered: a point in the
middle of an incremental chain, and a group backup streamed to restic.
"""
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.environ.get("DBM_E2E_DOCKER") != "1",
                                reason="set DBM_E2E_DOCKER=1 to run against a real Docker daemon")

FP = ("cd /data && find . | sort | while read -r f; do if [ -L \"$f\" ]; then echo \"L $f $(readlink \"$f\")\"; "
      "elif [ -f \"$f\" ]; then echo \"F $f $(stat -c %a \"$f\") $(sha256sum < \"$f\" | cut -d' ' -f1)\"; "
      "else echo \"D $f\"; fi; done")


def _fp(client, src):
    return client.containers.run("alpine:3.20", ["sh", "-c", FP], volumes={src: {"bind": "/data", "mode": "ro"}},
                                 remove=True).decode()


@pytest.fixture()
def env(tmp_path):
    from app.database import init_db
    from app.docker_client import get_client
    from app.reset_password import reset_password
    init_db()
    reset_password("export-user", "supersecret1")
    client = get_client()
    tag = uuid.uuid4().hex[:6]
    proj = f"exp{tag}"
    name, vol = f"{proj}-app", f"{proj}_data"
    bind_dir = tmp_path / "bind"
    bind_dir.mkdir()
    client.volumes.create(vol)
    c = client.containers.run("alpine:3.20", ["sleep", "3600"], name=name, detach=True,
                              labels={"com.docker.compose.project": proj},
                              volumes={vol: {"bind": "/vol", "mode": "rw"},
                                       str(bind_dir): {"bind": "/binds", "mode": "rw"}})
    created = {"containers": [name], "volumes": [vol]}
    yield client, c, proj, name, vol, bind_dir, created
    for cn in created["containers"]:
        try:
            client.containers.get(cn).remove(force=True)
        except Exception:
            pass
    for vn in created["volumes"]:
        try:
            client.volumes.get(vn).remove(force=True)
        except Exception:
            pass


def _sh(c, cmd):
    code, out = c.exec_run(["sh", "-c", cmd])
    assert code == 0, out


def _login(tc):
    tc.post("/api/auth/login", json={"username": "export-user", "password": "supersecret1"})


def _wait(job_id):
    from app import job_tracker
    for _ in range(600):
        job = job_tracker.get_job(job_id)
        if job.status not in ("running", "cancelling"):
            return job
        time.sleep(0.2)
    raise AssertionError("job hangs")


def _export(tc, record_id, tmp_path):
    resp = tc.post(f"/api/backups/{record_id}/export")
    assert resp.status_code == 200, resp.text
    job = _wait(resp.json()["job_id"])
    assert job.status == "success", job.error
    data = tc.get(job.download_url)
    assert data.status_code == 200
    out = tmp_path / f"export-{record_id}.tar"
    out.write_bytes(data.content)
    return out


def _wipe(db, names):
    from app.config import BACKUPS_DIR
    from app.models import BackupRecord
    for r in db.query(BackupRecord).filter(BackupRecord.name.in_(names)).all():
        db.delete(r)
    db.commit()
    for n in names:
        shutil.rmtree(BACKUPS_DIR / n, ignore_errors=True)
        from app.backup_engine import sanitize_name
        shutil.rmtree(BACKUPS_DIR / "_landscapes" / sanitize_name(n), ignore_errors=True)


def _import(tc, file):
    resp = tc.post("/api/backups/import", content=file.read_bytes(),
                   headers={"Content-Type": "application/octet-stream"})
    assert resp.status_code == 200, resp.text
    job = _wait(resp.json()["job_id"])
    assert job.status == "success", job.error
    return job.result_backup_id


def test_export_of_a_chain_point_imports_and_restores_exactly(env, tmp_path):
    from app import backup_engine, backup_records, restore_engine
    from app.config import BACKUPS_DIR
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord
    client, c, proj, name, vol, bind_dir, created = env
    steps = ["mkdir -p /vol/d && echo a > /vol/d/a && head -c 2000000 /dev/urandom > /vol/big && echo b > /binds/b",
             "echo more >> /vol/d/a && echo n > /vol/new && ln -s d/a /vol/link",
             "rm /vol/new && echo c > /binds/c"]
    ids, fps = [], []
    with SessionLocal() as db:
        for st in steps:
            _sh(c, st)
            r = backup_engine.backup_container(name, BACKUPS_DIR, backup_mode="incremental")
            assert r.ok, r.error
            rec = backup_records.persist_result(db, r, "container", [])
            ids.append(rec.id)
            fps.append((_fp(client, vol), _fp(client, str(bind_dir))))
        levels = [db.get(BackupRecord, i).backup_level for i in ids]
    assert levels == ["full", "incremental", "incremental"]
    with TestClient(app) as tc:
        _login(tc)
        export_file = _export(tc, ids[1], tmp_path)  # a point in the MIDDLE of the chain
        with SessionLocal() as db:
            _wipe(db, [name])
        new_id = _import(tc, export_file)
    with SessionLocal() as db:
        rec = db.get(BackupRecord, new_id)
        assert rec.backup_level == "full" and Path(rec.path).exists()
        path = rec.path
    nn = f"{name}-imp"
    created["containers"].append(nn)
    created["volumes"].append(f"{nn}_data".replace(f"{name}_data", vol))
    restore_engine.restore_container(Path(path), new_name=nn, start=False)
    restored_vol = vol.replace(name, nn) if vol.startswith(name) else vol
    target = tmp_path / "bind-out"
    restore_engine.restore_mount(Path(path), "bind", "/binds", target=str(target))
    assert _fp(client, str(target)) == fps[1][1]
    # volume (not prefixed with the container name -> restored under its own name; compare a fresh copy)
    single = f"exp-single-{uuid.uuid4().hex[:6]}"
    created["volumes"].append(single)
    client.volumes.create(single)
    restore_engine.restore_mount(Path(path), "volume", vol, target=single, replace=True)
    assert _fp(client, single) == fps[1][0]
    assert restored_vol


def test_export_of_a_restic_group_backup_imports_and_restores_exactly(env, tmp_path):
    from app import restore_engine, scheduler
    from app.database import SessionLocal
    from app.main import app
    from app.models import BackupRecord, Schedule, StorageTarget
    from app import restic_engine
    if not restic_engine.is_available():
        pytest.skip("restic not installed")
    client, c, proj, name, vol, bind_dir, created = env
    nas = tmp_path / "nas"
    nas.mkdir()
    with SessionLocal() as db:
        t = StorageTarget(name=f"NAS-{proj}", type="local_path", config_json=json.dumps({"path": str(nas)}))
        db.add(t)
        db.commit()
        s = Schedule(name=f"Grp {proj}", target_type="landscape", project_filter=proj, cron_expression="0 3 * * *",
                     retention_count=5, retention_days=0, storage_target_ids="[]", stream_volumes_target_id=t.id,
                     backup_mode="incremental", synthetic_full_every=7, enabled=True)
        db.add(s)
        db.commit()
        sid = s.id
    fps = []
    for st in ["echo one > /vol/x && head -c 1000000 /dev/urandom > /vol/r && echo b > /binds/b",
               "echo two >> /vol/x"]:
        _sh(c, st)
        scheduler.run_schedule(sid)
        fps.append((_fp(client, vol), _fp(client, str(bind_dir))))
    with SessionLocal() as db:
        groups = db.query(BackupRecord).filter(BackupRecord.name == f"Grp {proj}").order_by(BackupRecord.id).all()
        assert len(groups) == 2 and all(g.status == "ok" for g in groups)
        member = db.query(BackupRecord).filter(BackupRecord.name == name).order_by(BackupRecord.id).first()
        assert member.backup_level == "restic"
        first_group_id = groups[0].id
    with TestClient(app) as tc:
        _login(tc)
        export_file = _export(tc, first_group_id, tmp_path)  # the OLDER point
        with SessionLocal() as db:
            _wipe(db, [name, f"Grp {proj}"])
        shutil.rmtree(nas)  # restic repo gone too - only the file is left
        new_id = _import(tc, export_file)
        listing = tc.get("/api/backups").json()["groups"]
    grp = listing[f"Grp {proj}"]
    assert len(grp) == 1 and grp[0]["id"] == new_id and grp[0]["member_names"] == [name]
    with SessionLocal() as db:
        members = db.query(BackupRecord).filter(BackupRecord.name == name).all()
        assert len(members) == 1 and members[0].backup_level == "full"
        path = members[0].path
    single = f"exp-single-{uuid.uuid4().hex[:6]}"
    created["volumes"].append(single)
    client.volumes.create(single)
    restore_engine.restore_mount(Path(path), "volume", vol, target=single, replace=True)
    assert _fp(client, single) == fps[0][0]
    target = tmp_path / "bind-out"
    restore_engine.restore_mount(Path(path), "bind", "/binds", target=str(target))
    assert _fp(client, str(target)) == fps[0][1]
    nn = f"{name}-imp"
    created["containers"].append(nn)
    restore_engine.restore_container(Path(path), new_name=nn, start=False)
    assert client.containers.get(nn)
