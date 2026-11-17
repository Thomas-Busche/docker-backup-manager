"""Export/import without Docker: format, encryption, rejecting bad files."""
import base64
import io
import json
import os
import tarfile
import uuid

import pytest


def _fake_full(name):
    from app.config import BACKUPS_DIR
    d = BACKUPS_DIR / name / "20260101T000000Z"
    (d / "volumes").mkdir(parents=True)
    (d / "container.json").write_text(json.dumps({"Name": "/" + name, "Mounts": []}))
    (d / "image.tar").write_bytes(os.urandom(5000))
    (d / "volumes" / "data.tar.gz").write_bytes(os.urandom(3000))
    (d / "meta.json").write_text(json.dumps({"container_name": name, "volumes": ["data"],
                                             "created_at": "2026-01-01T00:00:00Z"}))
    return d


def test_encrypted_export_roundtrip(tmp_path, monkeypatch):
    import shutil
    from app import encryption, export_import
    from app.database import SessionLocal, init_db
    monkeypatch.setenv("DBM_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    init_db()
    name = f"enc-{uuid.uuid4().hex[:6]}"
    src = _fake_full(name)
    originals = {p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob("*") if p.is_file()}
    out = tmp_path / "x.tar"
    manifest = export_import.build_export([{"record_path": str(src)}], out)
    assert manifest["encrypted"] and manifest["members"][0]["key"] == f"{name}/20260101T000000Z"
    with tarfile.open(out) as tar:
        assert all(n.endswith(".enc") for n in tar.getnames() if "/" in n and "." in n.rsplit("/", 1)[1])
    shutil.rmtree(src.parent)
    with SessionLocal() as db:
        created = export_import.import_export(out, db)
        assert created[0].backup_level == "full" and created[0].name == name
    with encryption.decrypt_directory_to_temp(src) as plain:
        from pathlib import Path
        for rel, data in originals.items():
            if rel != "meta.json":
                assert (Path(plain) / rel).read_bytes() == data
        meta = json.loads((Path(plain) / "meta.json").read_text())
        assert meta["backup_level"] == "full" and "exported_from" in meta


def test_import_rejects_path_traversal_and_foreign_files(tmp_path):
    from app import export_import
    from app.database import SessionLocal, init_db
    init_db()
    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as tar:
        manifest = json.dumps({"format": "dbm-export", "version": 1, "type": "container",
                               "members": [{"name": "x", "key": "x/1"}]}).encode()
        info = tarfile.TarInfo("dbm-export.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        evil = tarfile.TarInfo("x/1/../../../../etc/evil")
        evil.size = 1
        tar.addfile(evil, io.BytesIO(b"x"))
    with SessionLocal() as db, pytest.raises(RuntimeError):
        export_import.import_export(bad, db)
    other = tmp_path / "other.tar"
    with tarfile.open(other, "w") as tar:
        tar.add(__file__, arcname="readme.txt")
    with pytest.raises(RuntimeError, match="Keine Exportdatei"):
        export_import.read_manifest(other)
