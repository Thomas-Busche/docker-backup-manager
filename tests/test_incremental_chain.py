"""Incremental chains + synthetic full (app/chain.py) without Docker: volume
tar streams are built in Python, the backup directories are laid out exactly
like backup_engine writes them."""
import base64
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from app import chain


# ---------- helpers ----------

def _tar_stream(files: dict, symlinks: dict = None, hardlinks: dict = None, dirs=(".", "./sub")) -> list[bytes]:
    """files: {"./a.txt": (b"content", mtime)}"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for d in dirs:
            ti = tarfile.TarInfo(d)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            tf.addfile(ti)
        for name, (data, mtime) in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = mtime
            ti.mode = 0o640
            ti.uid, ti.gid = 1000, 1000
            tf.addfile(ti, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tf.addfile(ti)
        for name, target in (hardlinks or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.LNKTYPE
            ti.linkname = target
            tf.addfile(ti)
    raw = buf.getvalue()
    return [raw[i:i + 7000] for i in range(0, len(raw), 7000)]


def _make_backup(root: Path, ts: str, files: dict, parent: Path = None, image_id="sha256:img1",
                 **extra) -> Path:
    """Writes one chain backup of container "app" with one volume "app_data"."""
    d = root / "app" / ts
    d.mkdir(parents=True)
    key = f"app/{ts}"
    (d / "container.json").write_text(json.dumps({"Name": "/app", "Config": {"Image": "app:1"}}))
    (d / "networks.json").write_text("{}")
    parent_meta = chain.read_meta(parent) if parent else {}
    if parent:
        info = chain.incremental_chain_info(chain.backup_key(parent), parent_meta, key)
        level = chain.LEVEL_INCREMENTAL
        pm = chain.read_manifest(parent, "volumes/app_data.manifest.json.gz")
    else:
        info = chain.new_chain_info(key)
        level = chain.LEVEL_FULL
        pm = None
    if parent and parent_meta.get("image_id") == image_id:
        info["image_src"] = parent_meta["chain"]["image_src"]
    else:
        (d / "image.tar").write_bytes(b"IMAGE-" + image_id.encode())
    m = chain.capture(_tar_stream(files, **extra), d / "volumes/app_data.tar.gz", key, pm)
    chain.write_manifest(d / "volumes/app_data.manifest.json.gz", m)
    chain.finalize_depends(info, [m], key)
    meta = {"backup_type": "container", "container_name": "app", "volumes": ["app_data"], "bind_mounts": [],
            "created_at": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z",
            "backup_level": level, "chain": info, "image_id": image_id, "streamed_target_id": None}
    meta["data_as_of"] = meta["created_at"]
    meta["checksums"] = chain.compute_checksums(d)
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def _restored_files(backup_dir: Path) -> dict:
    """Synthesizes the volume state and returns {name: bytes|("link", target)|"dir"}."""
    resolver = chain.ChainResolver(chain.root_of(backup_dir))
    mount = chain.mount_entries(chain.read_meta(backup_dir))[0]
    buf = io.BytesIO()
    chain.synthesize_mount(backup_dir, mount, buf, resolver)
    buf.seek(0)
    out = {}
    with tarfile.open(fileobj=buf, mode="r") as tf:
        for ti in tf:
            if ti.isreg():
                out[ti.name] = tf.extractfile(ti).read()
            elif ti.issym():
                out[ti.name] = ("sym", ti.linkname)
            elif ti.islnk():
                out[ti.name] = ("hard", ti.linkname)
            elif ti.isdir():
                out[ti.name] = "dir"
    return out


# ---------- capture ----------

def test_full_archive_is_a_normal_complete_tar_gz(tmp_path):
    d = _make_backup(tmp_path, "20260101T000000Z", {"./a.txt": (b"A1", 100), "./sub/b.bin": (b"B" * 5000, 100)},
                     symlinks={"./link": "a.txt"})
    with tarfile.open(d / "volumes/app_data.tar.gz", "r:gz") as tf:
        names = tf.getnames()
    assert {".", "./sub", "./a.txt", "./sub/b.bin", "./link"} <= set(names)
    meta = chain.read_meta(d)
    assert meta["backup_level"] == "full" and meta["chain"]["depends_on"] == []


def test_incremental_stores_only_changed_files_and_tracks_dependencies(tmp_path):
    full = _make_backup(tmp_path, "20260101T000000Z",
                        {"./a.txt": (b"A1", 100), "./b.txt": (b"B1", 100), "./c.txt": (b"C1", 100)})
    inc = _make_backup(tmp_path, "20260102T000000Z",
                       {"./a.txt": (b"A2-changed", 200), "./b.txt": (b"B1", 100), "./new.txt": (b"N", 200)},
                       parent=full)
    with tarfile.open(inc / "volumes/app_data.tar.gz", "r:gz") as tf:
        assert sorted(tf.getnames()) == ["./a.txt", "./new.txt"]
    m = chain.read_manifest(inc, "volumes/app_data.manifest.json.gz")
    assert m["stats"]["changed_files"] == 2 and m["stats"]["unchanged_files"] == 1 and m["stats"]["deleted"] == 1
    meta = chain.read_meta(inc)
    assert meta["backup_level"] == "incremental"
    assert meta["chain"]["seq"] == 1 and meta["chain"]["parent"] == "app/20260101T000000Z"
    # needs the full for b.txt and the unchanged image
    assert meta["chain"]["depends_on"] == ["app/20260101T000000Z"]
    assert meta["chain"]["image_src"] == "app/20260101T000000Z"
    assert not (inc / "image.tar").exists()


def test_every_point_in_time_restores_exactly(tmp_path):
    states = [
        {"./a.txt": (b"A1", 100), "./b.txt": (b"B1", 100), "./sub/c.txt": (b"C1", 100)},
        {"./a.txt": (b"A2", 200), "./b.txt": (b"B1", 100), "./sub/c.txt": (b"C1", 100), "./d.txt": (b"D", 200)},
        {"./a.txt": (b"A2", 200), "./sub/c.txt": (b"C3", 300), "./d.txt": (b"D", 200)},  # b deleted
        {"./a.txt": (b"A4" * 1000, 400), "./sub/c.txt": (b"C3", 300), "./d.txt": (b"D", 200),
         "./b.txt": (b"B-back", 400)},  # b comes back
    ]
    dirs = []
    for i, files in enumerate(states):
        dirs.append(_make_backup(tmp_path, f"2026010{i + 1}T000000Z", files, parent=dirs[-1] if dirs else None,
                                 symlinks={"./link": "a.txt"}, hardlinks={"./hard": "./sub/c.txt"}))
    for d, files in zip(dirs, states):
        got = _restored_files(d)
        assert {k: v for k, v in got.items() if isinstance(v, bytes)} == {k: v[0] for k, v in files.items()}
        assert got["./link"] == ("sym", "a.txt")
        assert got["./hard"] == ("hard", "./sub/c.txt")
        assert got["./sub"] == "dir"
    assert chain.read_meta(dirs[3])["chain"]["depends_on"] == [
        "app/20260101T000000Z", "app/20260102T000000Z", "app/20260103T000000Z"]


def test_new_image_is_saved_again(tmp_path):
    full = _make_backup(tmp_path, "20260101T000000Z", {"./a": (b"1", 1)})
    inc = _make_backup(tmp_path, "20260102T000000Z", {"./a": (b"1", 1)}, parent=full, image_id="sha256:img2")
    assert (inc / "image.tar").exists()
    assert chain.read_meta(inc)["chain"]["image_src"] == "app/20260102T000000Z"


# ---------- synthetic full ----------

def _chain_of(tmp_path, n):
    dirs = [_make_backup(tmp_path, "20260101T000000Z", {"./a": (b"A0", 1), "./b": (b"B0", 1)})]
    for i in range(1, n + 1):
        dirs.append(_make_backup(tmp_path, f"2026010{i + 1}T000000Z",
                                 {"./a": (f"A{i}".encode(), i + 1), "./b": (b"B0", 1), f"./f{i}": (b"x" * i, 1)},
                                 parent=dirs[-1]))
    return dirs


def test_synthetic_full_is_self_contained_and_matches_latest_state(tmp_path):
    dirs = _chain_of(tmp_path, 3)
    expected = _restored_files(dirs[-1])
    synth = chain.create_synthetic_full(dirs[-1])
    meta = chain.read_meta(synth)
    assert meta["backup_level"] == "synthetic_full"
    assert meta["chain"]["depends_on"] == []
    assert meta["chain"]["represents"] == "app/20260104T000000Z"
    # only the parts that still hold data of the latest state
    assert meta["chain"]["synthesized_from"] == ["app/20260101T000000Z", "app/20260104T000000Z"]
    assert chain.chain_members(dirs[-1]) == [chain.backup_key(d) for d in dirs]
    assert meta["data_as_of"] == chain.read_meta(dirs[-1])["created_at"]
    assert meta["chain"]["id"] != chain.read_meta(dirs[0])["chain"]["id"]
    assert (synth / "image.tar").read_bytes() == b"IMAGE-sha256:img1"
    # the whole old chain can go - the synthetic full still restores
    import shutil
    for d in dirs:
        shutil.rmtree(d)
    assert _restored_files(synth) == expected
    assert chain.verify(synth)["ok"]


def test_next_incremental_builds_on_the_synthetic_full(tmp_path):
    dirs = _chain_of(tmp_path, 2)
    synth = chain.create_synthetic_full(dirs[-1])
    parent_dir, parent_meta = chain.find_parent(tmp_path / "app")
    assert parent_dir == synth
    inc = _make_backup(tmp_path, "20991231T000000Z", {"./a": (b"A2", 3), "./b": (b"B0", 1), "./f1": (b"x", 1),
                                                       "./f2": (b"xx", 1), "./z": (b"new", 9)}, parent=synth)
    meta = chain.read_meta(inc)
    assert meta["chain"]["depends_on"] == [chain.backup_key(synth)]
    assert meta["chain"]["seq"] == 1 and meta["chain"]["id"] == chain.read_meta(synth)["chain"]["id"]


def test_synthetic_full_of_a_damaged_chain_fails_and_leaves_nothing_behind(tmp_path):
    dirs = _chain_of(tmp_path, 2)
    # Corrupt the archive that holds b (the full): replace with a tar missing it
    m = chain.capture(_tar_stream({"./a": (b"A0", 1)}), dirs[0] / "volumes/app_data.tar.gz", "app/20260101T000000Z")
    with pytest.raises(chain.ChainError):
        chain.create_synthetic_full(dirs[-1])
    assert sorted(p.name for p in (tmp_path / "app").iterdir()) == [d.name for d in dirs]


# ---------- integrity check ----------

def test_verify_detects_bit_rot_and_missing_chain_parts(tmp_path):
    dirs = _chain_of(tmp_path, 2)
    assert chain.verify(dirs[-1])["ok"]

    archive = dirs[0] / "volumes/app_data.tar.gz"  # the full still holds b
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))
    report = chain.verify(dirs[-1])
    assert not report["ok"]
    assert any("Prüfsumme" in e for e in report["errors"])

    import shutil
    shutil.rmtree(dirs[0])
    report = chain.verify(dirs[-1])
    assert not report["ok"] and any("fehlt" in e for e in report["errors"])


def test_missing_chain_part_is_fetched_from_a_storage_target(tmp_path):
    dirs = _chain_of(tmp_path, 2)
    offsite = tmp_path / "offsite"
    import shutil
    shutil.copytree(dirs[0], offsite / "app" / dirs[0].name)
    shutil.rmtree(dirs[0])

    fetched = []

    def fetch(key, dest):
        fetched.append(key)
        shutil.copytree(offsite / key, dest)

    assert not chain.verify(dirs[-1])["ok"]  # without a target the gap is reported
    report = chain.verify(dirs[-1], fetch_missing=fetch)
    assert report["ok"], report
    assert fetched == ["app/20260101T000000Z"]


# ---------- materialize (restore input) ----------

def test_materialize_lays_out_an_ordinary_full_backup(tmp_path):
    dirs = _chain_of(tmp_path, 2)
    out = tmp_path / "stage"
    chain.materialize(dirs[-1], out)
    assert (out / "image.tar").read_bytes() == b"IMAGE-sha256:img1"
    assert json.loads((out / "container.json").read_text())["Name"] == "/app"
    with tarfile.open(out / "volumes/app_data.tar.gz", "r:gz") as tf:
        files = {ti.name: tf.extractfile(ti).read() for ti in tf if ti.isreg()}
    assert files == {"./a": b"A2", "./b": b"B0", "./f2": b"xx"}


def test_materialize_single_volume_only(tmp_path):
    dirs = _chain_of(tmp_path, 1)
    out = tmp_path / "stage"
    chain.materialize(dirs[-1], out, only_mount={"kind": "volume", "name": "app_data"})
    assert (out / "volumes/app_data.tar.gz").exists() and not (out / "image.tar").exists()
    with pytest.raises(chain.ChainError):
        chain.materialize(dirs[-1], tmp_path / "x", only_mount={"kind": "volume", "name": "nope"})


# ---------- encryption at rest ----------

def test_encrypted_chain_restores_and_synthesizes(tmp_path, monkeypatch):
    from app import encryption
    monkeypatch.setenv("DBM_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    full = _make_backup(tmp_path, "20260101T000000Z", {"./a": (b"A0", 1), "./b": (b"B0", 1)})
    encryption.encrypt_directory_in_place(full)
    inc = _make_backup(tmp_path, "20260102T000000Z", {"./a": (b"A1", 2), "./b": (b"B0", 1)}, parent=full)
    encryption.encrypt_directory_in_place(inc)
    assert not (inc / "meta.json").exists()
    assert chain.verify(inc)["ok"]
    out = tmp_path / "stage"
    chain.materialize(inc, out)
    with tarfile.open(out / "volumes/app_data.tar.gz", "r:gz") as tf:
        files = {ti.name: tf.extractfile(ti).read() for ti in tf if ti.isreg()}
    assert files == {"./a": b"A1", "./b": b"B0"}
    synth = chain.create_synthetic_full(inc)
    assert (synth / "meta.json.enc").exists() and chain.verify(synth)["ok"]


# ---------- find_parent ----------

def test_find_parent_starts_a_new_chain_after_a_classic_full(tmp_path):
    dirs = _chain_of(tmp_path, 1)
    classic = tmp_path / "app" / "20270101T000000Z"
    classic.mkdir()
    (classic / "meta.json").write_text(json.dumps({"backup_type": "container", "volumes": []}))
    assert chain.find_parent(tmp_path / "app") is None
    import shutil
    shutil.rmtree(classic)
    assert chain.find_parent(tmp_path / "app")[0] == dirs[-1]
