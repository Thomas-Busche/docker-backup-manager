"""End-to-end test of incremental chains against a REAL Docker daemon.

Opt-in (needs Docker + the alpine helper image):  DBM_E2E_DOCKER=1 pytest tests/test_e2e_incremental_docker.py

Backs up a container with a named volume, a directory bind mount and a
single-file bind mount; changes data between runs (modify, add, delete,
symlinks, a large file); builds full -> incrementals -> synthetic full ->
incremental on top; then restores EVERY point in time (whole container,
single volume, single bind mount) and compares a fingerprint (path, type,
mode, SHA-256 of every file, symlink targets) with the state recorded at
backup time. Also: encrypted chain, restore on a "new host" with chain parts
fetched from a storage target, old chain deleted after the synthetic full.
"""
import base64
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DBM_E2E_DOCKER") != "1",
                                reason="set DBM_E2E_DOCKER=1 to run against a real Docker daemon")

FINGERPRINT = (
    "cd /data && find . | sort | while read -r f; do "
    "if [ -L \"$f\" ]; then echo \"L $f -> $(readlink \"$f\")\"; "
    "elif [ -f \"$f\" ]; then echo \"F $f $(stat -c %a \"$f\") $(sha256sum < \"$f\" | cut -d' ' -f1)\"; "
    "else echo \"D $f $(stat -c %a \"$f\")\"; fi; done"
)


def _run(client, mounts: dict, cmd: str) -> str:
    out = client.containers.run("alpine:3.20", ["sh", "-c", cmd], volumes=mounts, remove=True)
    return out.decode()


def _fp_volume(client, vol: str) -> str:
    return _run(client, {vol: {"bind": "/data", "mode": "ro"}}, FINGERPRINT)


def _fp_dir(client, path: str) -> str:
    return _run(client, {path: {"bind": "/data", "mode": "ro"}}, FINGERPRINT)


@pytest.fixture()
def env(tmp_path):
    from app.docker_client import get_client
    client = get_client()
    tag = uuid.uuid4().hex[:6]
    name, vol = f"e2e-app-{tag}", f"e2e-app-{tag}_data"
    bind_dir = tmp_path / "binddata"
    bind_dir.mkdir()
    cfg = tmp_path / "config.yml"
    cfg.write_text("version: 1\n")
    client.volumes.create(vol)
    c = client.containers.run("alpine:3.20", ["sleep", "3600"], name=name, detach=True,
                              volumes={vol: {"bind": "/vol", "mode": "rw"},
                                       str(bind_dir): {"bind": "/binds", "mode": "rw"},
                                       str(cfg): {"bind": "/etc/app/config.yml", "mode": "rw"}})
    created = {"containers": [name], "volumes": [vol]}
    yield client, c, name, vol, bind_dir, cfg, created
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


def _sh(c, cmd: str) -> None:
    code, out = c.exec_run(["sh", "-c", cmd])
    assert code == 0, out


STEPS = [
    # initial data
    "mkdir -p /vol/docs /vol/db && echo hello > /vol/docs/a.txt && echo keep > /vol/docs/keep.txt "
    "&& head -c 3000000 /dev/urandom > /vol/db/big.bin && ln -s docs/a.txt /vol/link "
    "&& chmod 600 /vol/docs/keep.txt && echo b1 > /binds/one.txt",
    # modify, add, delete
    "echo changed >> /vol/docs/a.txt && echo new > /vol/docs/new.txt && rm /vol/link "
    "&& ln -s docs/new.txt /vol/link2 && echo b2 > /binds/two.txt && echo 'version: 2' > /etc/app/config.yml",
    # delete a directory, touch big file content
    "rm -r /vol/docs && mkdir /vol/other && echo x > /vol/other/x.txt "
    "&& head -c 100 /dev/urandom >> /vol/db/big.bin && rm /binds/one.txt",
    # after synthetic full
    "echo again > /vol/other/x.txt && mkdir -p /vol/docs && echo back > /vol/docs/a.txt",
]


def _backup_series(client, c, name, vol, bind_dir, cfg, root, synthetic_every=2):
    from app import backup_engine, chain
    points = []
    for i, step in enumerate(STEPS):
        _sh(c, step)
        result = backup_engine.backup_container(name, root, backup_mode="incremental",
                                                synthetic_full_every=synthetic_every)
        assert result.ok, result.error
        fp = (_fp_volume(client, vol), _fp_dir(client, str(bind_dir)), cfg.read_text())
        points.append((result, fp))
        if result.synthetic is not None:
            points.append((result.synthetic, fp))  # synthetic full = same state as its incremental
    return points


def test_incremental_chain_every_point_in_time_restores_exactly(env, tmp_path):
    from app import chain, restore_engine
    client, c, name, vol, bind_dir, cfg, created = env
    from app.config import BACKUPS_DIR
    root = BACKUPS_DIR
    points = _backup_series(client, c, name, vol, bind_dir, cfg, root)

    levels = [r.level for r, _ in points]
    assert levels == ["full", "incremental", "incremental", "synthetic_full", "incremental"]
    full_size = points[0][0].size_bytes
    inc_size = points[1][0].size_bytes
    assert inc_size < full_size / 5, (inc_size, full_size)  # no image, no unchanged big file
    synth_meta = chain.read_meta(points[3][0].path)
    assert synth_meta["chain"]["depends_on"] == []
    assert chain.read_meta(points[4][0].path)["chain"]["depends_on"] == [chain.backup_key(points[3][0].path)]

    for idx, (result, (fp_vol, fp_bind, cfg_text)) in enumerate(points):
        assert chain.verify(result.path)["ok"], chain.verify(result.path)
        # whole container, into a parallel copy (renamed volumes)
        new_name = f"{name}-r{idx}"
        created["containers"].append(new_name)
        created["volumes"].append(f"{new_name}_data")
        restore_engine.restore_container(result.path, new_name=new_name, start=False)
        assert _fp_volume(client, f"{new_name}_data") == fp_vol, f"point {idx} ({result.level})"
        # single volume into an existing, dirty volume: replace -> exactly the backed-up state
        target = f"e2e-single-{uuid.uuid4().hex[:6]}"
        created["volumes"].append(target)
        client.volumes.create(target)
        _run(client, {target: {"bind": "/data", "mode": "rw"}}, "echo junk > /data/junk.txt")
        restore_engine.restore_mount(result.path, "volume", vol, target=target, replace=True)
        assert _fp_volume(client, target) == fp_vol
        # single bind mount into a new host directory
        bind_target = tmp_path / f"bind-restore-{idx}"
        restore_engine.restore_mount(result.path, "bind", "/binds", target=str(bind_target))
        assert _fp_dir(client, str(bind_target)) == fp_bind
        # single-file bind mount
        file_target = tmp_path / f"file-restore-{idx}"
        file_target.mkdir()
        restore_engine.restore_mount(result.path, "bind", "/etc/app/config.yml", target=str(file_target))
        assert (file_target / "config.yml").read_text() == cfg_text

    # overwrite the original container with an OLD point in time -> exact old state
    restore_engine.restore_container(points[0][0].path, start=True, overwrite=True)
    assert _fp_volume(client, vol) == points[0][1][0]

    # the old chain can be deleted entirely - synthetic full + newest incremental still restore
    for result, _ in points[:3]:
        shutil.rmtree(result.path)
    for result, fp in points[3:]:
        assert chain.verify(result.path)["ok"]
        nn = f"{name}-after-{result.level}"
        created["containers"].append(nn)
        created["volumes"].append(f"{nn}_data")
        restore_engine.restore_container(result.path, new_name=nn, start=False)
        assert _fp_volume(client, f"{nn}_data") == fp[0]


def test_encrypted_chain_and_restore_on_a_new_host_from_a_storage_target(env, tmp_path, monkeypatch):
    from app import chain, encryption, restore_engine
    client, c, name, vol, bind_dir, cfg, created = env
    monkeypatch.setenv("DBM_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    from app.config import BACKUPS_DIR
    root = BACKUPS_DIR
    points = _backup_series(client, c, name, vol, bind_dir, cfg, root, synthetic_every=0)
    assert [r.level for r, _ in points] == ["full", "incremental", "incremental", "incremental"]
    assert all(encryption.is_backup_encrypted(r.path) for r, _ in points)

    # "Storage target" holding every chain member; the new host only has the newest folder
    offsite = tmp_path / "nas"
    shutil.copytree(root / name, offsite / name)
    for r, _ in points[:-1]:
        shutil.rmtree(r.path)
    target = ("local_path", json.dumps({"path": str(offsite)}), 1)
    fetch = restore_engine.fetcher_for_targets([target])
    latest, fp = points[-1]
    assert chain.verify(latest.path, fetch_missing=fetch)["ok"]
    nn = f"{name}-newhost"
    created["containers"].append(nn)
    created["volumes"].append(f"{nn}_data")
    restore_engine.restore_container(latest.path, new_name=nn, start=False, fetch_missing=fetch)
    assert _fp_volume(client, f"{nn}_data") == fp[0]
    # an older point too (its folder is fetched as well)
    older, fp_old = points[1]
    nn2 = f"{name}-newhost-old"
    created["containers"].append(nn2)
    created["volumes"].append(f"{nn2}_data")
    restore_engine.restore_container(older.path, new_name=nn2, start=False, stream_target=target)
    assert _fp_volume(client, f"{nn2}_data") == fp_old[0]


def test_incremental_mode_on_a_restic_target(env, tmp_path):
    """Streaming to a restic target is incremental on its own (dedup); in
    incremental mode the image goes into the repo too, so unchanged layers
    don't use space again. Every snapshot restores the full state."""
    from app import backup_engine, chain, restic_engine, restore_engine
    from app.config import BACKUPS_DIR
    if not restic_engine.is_available():
        pytest.skip("restic not installed")
    client, c, name, vol, bind_dir, cfg, created = env
    target = ("local_path", json.dumps({"path": str(tmp_path / "nas")}), 7)
    results = []
    for step in STEPS[:2]:
        _sh(c, step)
        r = backup_engine.backup_container(name, BACKUPS_DIR, stream_target=target, backup_mode="incremental")
        assert r.ok, r.error
        results.append((r, _fp_volume(client, vol)))
    # a run without any change adds (almost) nothing - the size shown is the protected data
    again = backup_engine.backup_container(name, BACKUPS_DIR, stream_target=target, backup_mode="incremental")
    assert again.ok, again.error
    first = results[0][0]
    assert first.added_bytes and first.added_bytes > 1_000_000  # image + data went in the first time
    assert again.size_bytes == results[-1][0].size_bytes
    assert again.added_bytes < 50_000, (again.added_bytes, first.added_bytes)
    assert chain.read_meta(again.path)["restic_added_bytes"] == again.added_bytes
    meta = chain.read_meta(results[-1][0].path)
    assert meta["backup_engine"] == "restic" and meta["backup_level"] == "restic"
    assert "__image__" in meta["restic_snapshot_ids"]
    for i, (r, fp) in enumerate(results):
        nn = f"{name}-restic-{i}"
        created["containers"].append(nn)
        created["volumes"].append(f"{nn}_data")
        restore_engine.restore_container(r.path, new_name=nn, start=False, stream_target=target)
        assert _fp_volume(client, f"{nn}_data") == fp
