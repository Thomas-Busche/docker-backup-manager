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


def test_leftover_lock_of_a_previous_repo_is_removed(tmp_path):
    """Old repo deleted only partly (its lock survived), new repo created in
    the same folder -> "unable to create lock in backend: ciphertext
    verification failed" on every backup, until the foreign lock is gone."""
    import shutil
    import subprocess
    repo = tmp_path / "repo"
    restic_engine.init_repo_if_needed(str(repo), {}, "pw")
    old_locks = tmp_path / "old_locks"
    p = subprocess.Popen(["restic", "-r", str(repo), "backup", "--stdin"], stdin=subprocess.PIPE,
                         env={**os.environ, "RESTIC_PASSWORD": "pw"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait for a completely written lock: restic creates the file first and
    # fills it afterwards, so killing it right after the file shows up can
    # leave an empty lock that later runs ignore (flaky on slow CI runners).
    lock = None
    for _ in range(300):
        files = [f for f in (repo / "locks").iterdir() if f.stat().st_size > 0]
        if files:
            lock = files[0]
            break
        time.sleep(0.05)
    time.sleep(0.3)
    p.kill()
    p.wait()
    assert lock is not None, "restic hat keine Sperrdatei angelegt"
    shutil.copytree(repo / "locks", old_locks)
    shutil.rmtree(repo)
    restic_engine.init_repo_if_needed(str(repo), {}, "pw")  # fresh repo, new master key
    for f in old_locks.iterdir():
        shutil.copy(f, repo / "locks" / f.name)
    with pytest.raises(RuntimeError, match="ciphertext verification failed"):
        restic_engine.backup_volume_from_stream(_chunks(4), str(repo), {}, "pw", "c/vol")
    logs = []
    restic_engine.init_repo_if_needed(str(repo), {}, "pw", on_log=logs.append)  # what the next backup does first
    assert any("Sperrdateien" in m for m in logs)
    sid, _n = restic_engine.backup_volume_from_stream(_chunks(4), str(repo), {}, "pw", "c/vol")
    assert sid


def test_leftovers_of_a_partly_deleted_repo_are_cleared_before_init(tmp_path):
    repo = tmp_path / "repo"
    restic_engine.init_repo_if_needed(str(repo), {}, "pw")
    restic_engine.backup_volume_from_stream(_chunks(4), str(repo), {}, "pw", "c/vol")
    (repo / "config").unlink()  # purge cut off: config gone, snapshots/data/keys still there
    restic_engine.init_repo_if_needed(str(repo), {}, "pw")
    sid, _n = restic_engine.backup_volume_from_stream(_chunks(4), str(repo), {}, "pw", "c/vol")
    assert sid and restic_engine.snapshot_count(str(repo), {}, "pw") == 1


def test_missing_repo_is_detected_on_old_and_new_restic():
    from app.restic_engine import _repo_missing
    old = ("Fatal: unable to open config file: Stat: stat /r/config: no such file or directory\n"
           "Is there a repository at the following location?\n/r")
    assert _repo_missing(1, old)
    assert _repo_missing(10, "")
    assert not _repo_missing(1, "Fatal: unable to open repository: connection refused")
    assert not _repo_missing(12, "Fatal: wrong password or no key found")
