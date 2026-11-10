import json
import threading
import time


def test_archive_command_keeps_builtin_gzip_by_default():
    from app.backup_engine import _archive_shell_command

    assert _archive_shell_command(True) == \
        "if [ -d /data ]; then tar czf - -C /data .; else tar czf - /data; fi"
    assert "gzip" not in _archive_shell_command(False, 1)  # restic gets plain tar regardless


def test_archive_command_with_custom_gzip_level_keeps_tar_failures_visible():
    from app.backup_engine import _archive_shell_command

    cmd = _archive_shell_command(True, 1)
    assert cmd.startswith("set -o pipefail;")
    assert cmd.endswith("| gzip -1")


def test_sync_runs_targets_in_parallel_and_records_each_result(monkeypatch, tmp_path):
    from app import storage_sync
    from app.database import SessionLocal, init_db
    from app.models import StorageTarget

    init_db()
    with SessionLocal() as db:
        ids = []
        for name in ("par-a", "par-b", "par-fail"):
            t = StorageTarget(name=name, type="local_path", enabled=True,
                              config_json=json.dumps({"path": str(tmp_path / name)}))
            db.add(t)
            db.commit()
            ids.append(t.id)

    running, peak, lock = [0], [0], threading.Lock()

    def fake_sync(backup_path, target_type, config_json):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        if "par-fail" in config_json:
            raise RuntimeError("NAS offline")

    monkeypatch.setattr(storage_sync, "sync_to_target", fake_sync)
    started = time.monotonic()
    results = storage_sync.sync_to_selected_targets(tmp_path, ids)
    assert time.monotonic() - started < 0.5  # not 3 x 0.2s back to back
    assert peak[0] >= 2
    assert {r["target"]: r["ok"] for r in results} == {"par-a": True, "par-b": True, "par-fail": False}

    with SessionLocal() as db:
        failed = db.get(StorageTarget, ids[2])
        assert failed.last_sync_status == "failed" and failed.last_sync_error == "NAS offline"
        assert db.get(StorageTarget, ids[0]).last_sync_status == "ok"
