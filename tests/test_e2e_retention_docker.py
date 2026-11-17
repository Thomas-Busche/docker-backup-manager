"""Retention "keep N chains" over real scheduler runs against a REAL Docker daemon.

Opt-in:  DBM_E2E_DOCKER=1 pytest tests/test_e2e_retention_docker.py
"""
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DBM_E2E_DOCKER") != "1",
                                reason="set DBM_E2E_DOCKER=1 to run against a real Docker daemon")

FP = ("cd /data && find . -type f | sort | while read -r f; do echo \"$f $(sha256sum < \"$f\" | cut -d' ' -f1)\"; done")


def test_keep_two_chains_over_seven_runs_and_every_kept_point_restores():
    from app import chain, restore_engine, scheduler
    from app.database import SessionLocal, init_db
    from app.docker_client import get_client
    from app.models import BackupRecord, Schedule
    init_db()
    client = get_client()
    tag = uuid.uuid4().hex[:6]
    name, vol = f"ret-{tag}", f"ret-{tag}_data"
    client.volumes.create(vol)
    c = client.containers.run("alpine:3.20", ["sleep", "3600"], name=name, detach=True,
                              volumes={vol: {"bind": "/vol", "mode": "rw"}})
    cleanup = [vol]
    try:
        with SessionLocal() as db:
            s = Schedule(name=f"ret {tag}", target_type="container", target_ref=name, cron_expression="0 3 * * *",
                         retention_count=1, retention_days=0, retention_chains=2, storage_target_ids="[]",
                         backup_mode="incremental", synthetic_full_every=2, enabled=True)
            db.add(s)
            db.commit()
            sid = s.id
        states = {}
        for i in range(7):
            c.exec_run(["sh", "-c", f"echo run{i} > /vol/f{i} && echo {i} >> /vol/log"])
            scheduler.run_schedule(sid)
            fp = client.containers.run("alpine:3.20", ["sh", "-c", FP], volumes={vol: {"bind": "/data", "mode": "ro"}},
                                       remove=True).decode()
            with SessionLocal() as db:
                for r in db.query(BackupRecord).filter(BackupRecord.name == name):
                    states.setdefault(r.path, fp)
        with SessionLocal() as db:
            left = sorted(db.query(BackupRecord).filter(BackupRecord.name == name), key=lambda r: r.created_at)
            levels = [r.backup_level for r in left]
            chains = {r.chain_id for r in left}
            paths = [r.path for r in left]
        # 2 chains: synthetic full + its incrementals, newest synthetic full
        assert len(chains) == 2, levels
        assert levels[0] == "synthetic_full" and levels[-1] == "synthetic_full", levels
        assert all(Path(p).exists() for p in paths)
        for p in paths:
            assert chain.verify(Path(p))["ok"]
            target = f"ret-check-{uuid.uuid4().hex[:6]}"
            cleanup.append(target)
            client.volumes.create(target)
            restore_engine.restore_mount(Path(p), "volume", vol, target=target, replace=True)
            got = client.containers.run("alpine:3.20", ["sh", "-c", FP], volumes={target: {"bind": "/data", "mode": "ro"}},
                                        remove=True).decode()
            assert got == states[p], p
    finally:
        c.remove(force=True)
        for v in cleanup:
            try:
                client.volumes.get(v).remove(force=True)
            except Exception:
                pass
