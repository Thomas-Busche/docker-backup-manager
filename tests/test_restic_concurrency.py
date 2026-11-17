"""restic: a backup and the background cleanup of deleted backups on the
same repository must not break each other, and an early restic exit must
report restic's own error instead of a bare "Broken pipe"."""
import json
import os
import threading
import time

import pytest

from app import restic_engine

pytestmark = pytest.mark.skipif(not restic_engine.is_available(), reason="restic not installed")


def _chunks(n=400, size=256 * 1024):
    for _ in range(n):
        yield os.urandom(size)


def test_early_restic_exit_reports_the_reason(tmp_path):
    repo = str(tmp_path / "repo")
    restic_engine.init_repo_if_needed(repo, {}, "right-password")
    logs = []
    with pytest.raises(RuntimeError) as exc:
        restic_engine.backup_volume_from_stream(_chunks(), repo, {}, "wrong-password", "c/vol", on_log=logs.append)
    msg = str(exc.value)
    assert "Broken pipe" not in msg
    assert "vorzeitig beendet" in msg and "password" in msg.lower()


def test_purge_keeps_a_repo_that_got_a_new_snapshot(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    target = ("local_path", json.dumps({"path": str(nas)}), 1)
    repo, env, _ = restic_engine.repo_url_and_env("local_path", {"path": str(nas)}, "app")
    monkeypatch.setattr(restic_engine, "get_password", lambda: "pw")
    restic_engine.init_repo_if_needed(repo, env, "pw")
    restic_engine.backup_volume_from_stream(_chunks(4), repo, env, "pw", "app/vol")
    restic_engine.purge_restic_repo("app", target)
    assert restic_engine.snapshot_count(repo, env, "pw") == 1  # new snapshot -> repo kept
    sid = json.loads(restic_engine.subprocess.run(
        ["restic", "-r", repo, "snapshots", "--json"], capture_output=True,
        env={**os.environ, "RESTIC_PASSWORD": "pw"}).stdout)[0]["id"]
    restic_engine.forget_snapshot(repo, env, "pw", sid)
    restic_engine.purge_restic_repo("app", target)
    assert restic_engine.snapshot_count(repo, env, "pw") is None  # empty -> folder gone


def test_cleanup_waits_for_a_running_backup_on_the_same_repo(tmp_path):
    repo = str(tmp_path / "repo")
    restic_engine.init_repo_if_needed(repo, {}, "pw")
    order = []
    lock = restic_engine.repo_lock(repo)
    lock.acquire()  # what backup_container holds from init to its last snapshot

    def cleanup():
        with restic_engine.repo_lock(repo):
            order.append("cleanup")

    t = threading.Thread(target=cleanup)
    t.start()
    time.sleep(0.3)
    order.append("backup done")
    lock.release()
    t.join(5)
    assert order == ["backup done", "cleanup"]
