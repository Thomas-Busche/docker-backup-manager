"""
Complete, self-contained export of a backup point - and importing it again.

Whatever the backup looks like in the catalog (classic full, incremental
chain member, synthetic full, streamed to a target, restic snapshot, group
backup), the export holds the complete state of that point in time as an
ordinary full backup: container.json, networks.json, meta.json, image.tar,
volumes/*.tar.gz, binds/*. That file can sit on an external disk and be
imported on any other Docker Backup Manager, where it shows up as a normal
backup and restores like one - no storage target or restic repo needed.

File format (an uncompressed .tar; the archives inside are gzipped already):

    dbm-export.json                 manifest (format version, type, members)
    <container>/<ts>/...            one ordinary full backup per container
    _landscapes/<group>/<ts>/...    group backups: the group folder

Encryption: if DBM_ENCRYPTION_KEY is set, the exported backup folders are
encrypted file by file like every other backup (importing needs the same key);
without a key the export is plaintext.
"""
from __future__ import annotations

import datetime
import gzip
import json
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional

from app import chain, encryption, restic_engine, storage_sync
from app.config import BACKUPS_DIR

logger = logging.getLogger("dbm.export")

FORMAT = "dbm-export"
FORMAT_VERSION = 1
MANIFEST = "dbm-export.json"
EXPORTS_DIR_NAME = ".exports"
EXPORT_TTL_SECONDS = 24 * 3600

# meta.json keys that only make sense in the source catalog
_SOURCE_ONLY_META = ("chain", "checksums", "chain_stats", "streamed_target_id", "backup_engine",
                     "restic_repo_url", "restic_snapshot_ids", "restic_data_bytes", "restic_added_bytes",
                     "restic_password", "materialized_from", "synthesized_from")

Log = Optional[Callable[[str], None]]


def exports_dir() -> Path:
    d = BACKUPS_DIR / ".tmp" / EXPORTS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_old_exports(now: Optional[float] = None) -> None:
    import time
    now = now or time.time()
    for f in exports_dir().glob("*.tar"):
        try:
            if now - f.stat().st_mtime > EXPORT_TTL_SECONDS:
                f.unlink()
        except OSError:
            pass


def _staging() -> tempfile.TemporaryDirectory:
    root = BACKUPS_DIR / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root, prefix="dbm-export-")


def _gzip_file(src: Path, dest: Path) -> None:
    with open(src, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def _normalize_meta(meta: dict, source_key: str) -> dict:
    meta = {k: v for k, v in meta.items() if k not in _SOURCE_ONLY_META}
    meta["backup_level"] = "full"
    meta["exported_from"] = source_key
    return meta


def _export_streamed(src: Path, dest: Path, meta: dict, stream_target, rel_key: str, _log) -> None:
    """Folder whose data lives on the streaming target (restic repo or plain stream)."""
    for rel in ("container.json", "networks.json"):
        if (src / rel).exists():
            shutil.copy2(src / rel, dest / rel)
    if not stream_target:
        raise RuntimeError("Die Daten dieser Sicherung liegen auf einem Speicherziel, das nicht mehr verfügbar ist")
    target_type, config_json, _tid = stream_target
    (dest / "volumes").mkdir(exist_ok=True)
    binds = meta.get("bind_mounts") or []
    if binds:
        (dest / "binds").mkdir(exist_ok=True)
    if meta.get("backup_engine") == "restic":
        repo_url, env, conf = restic_engine.repo_url_and_env(
            target_type, json.loads(config_json or "{}"), meta.get("container_name") or src.parent.name)
        password = meta.get("restic_password") or restic_engine.get_password()
        snaps = meta.get("restic_snapshot_ids") or {}
        try:
            with tempfile.TemporaryDirectory(dir=dest) as tmp:
                for vol in meta.get("volumes", []):
                    _log(f"Restic: Volume {vol}")
                    raw = Path(tmp) / "data.tar"
                    restic_engine.dump_snapshot_to_file(repo_url, env, password, snaps[vol], raw)
                    _gzip_file(raw, dest / "volumes" / f"{_sanitize(vol)}.tar.gz")
                    raw.unlink()
                for bind in binds:
                    key = f"bind_{_sanitize(bind['destination'])}"
                    _log(f"Restic: Bind-Mount {bind['destination']}")
                    raw = Path(tmp) / "data.tar"
                    restic_engine.dump_snapshot_to_file(repo_url, env, password, snaps[key], raw)
                    _gzip_file(raw, dest / "binds" / bind["filename"])
                    raw.unlink()
                if snaps.get("__image__"):
                    _log("Restic: Image")
                    restic_engine.dump_snapshot_to_file(repo_url, env, password, snaps["__image__"],
                                                        dest / "image.tar")
        finally:
            if conf:
                restic_engine.rclone_backends.discard_conf(conf)
    else:
        for vol in meta.get("volumes", []):
            name = f"{_sanitize(vol)}.tar.gz"
            _log(f"Lade Volume {vol}")
            storage_sync.download_from_target(target_type, config_json, f"{rel_key}/volumes/{name}",
                                              dest / "volumes" / name)
        for bind in binds:
            storage_sync.download_from_target(target_type, config_json, f"{rel_key}/binds/{bind['filename']}",
                                              dest / "binds" / bind["filename"])
    if not (dest / "image.tar").exists():
        if (src / "image.tar").exists():
            shutil.copy2(src / "image.tar", dest / "image.tar")
        else:
            _log("Lade Image")
            storage_sync.download_from_target(target_type, config_json, f"{rel_key}/image.tar", dest / "image.tar")


def _sanitize(name: str) -> str:
    from app.backup_engine import sanitize_name
    return sanitize_name(name)


def export_container_dir(backup_dir: Path, dest: Path, stream_target=None, fetch_missing=None,
                         on_log: Log = None) -> dict:
    """Writes the complete state of one container backup into dest as an
    ordinary plaintext full backup. Returns its meta.json."""
    _log = on_log or (lambda m: None)
    backup_dir = Path(backup_dir)
    rel_key = storage_sync._relative_key(backup_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if not backup_dir.exists():
        if not stream_target:
            raise RuntimeError("Sicherung ist lokal nicht vorhanden und es gibt kein Speicherziel zum Nachladen")
        _log("Lade Sicherung vom Speicherziel")
        storage_sync.download_full_backup_from_target(stream_target[0], stream_target[1], rel_key, backup_dir)
    if encryption.is_backup_encrypted(backup_dir) and not encryption.is_enabled():
        raise RuntimeError("Sicherung ist verschlüsselt, auf diesem System ist aber kein DBM_ENCRYPTION_KEY gesetzt")

    meta = chain.read_meta(backup_dir)
    if chain.is_chain_meta(meta):
        _log("Stelle den Zeitpunkt aus der Sicherungskette zusammen")
        chain.materialize(backup_dir, dest, fetch_missing=fetch_missing, on_log=on_log)
    elif encryption.is_backup_encrypted(backup_dir):
        with encryption.decrypt_directory_to_temp(backup_dir) as plain:
            _copy_or_fetch(Path(plain), dest, meta, stream_target, rel_key, _log)
    else:
        _copy_or_fetch(backup_dir, dest, meta, stream_target, rel_key, _log)

    for leftover in list(dest.rglob("*.manifest.json.gz")):
        leftover.unlink()
    meta = _normalize_meta(json.loads((dest / "meta.json").read_text()) if (dest / "meta.json").exists() else meta,
                           rel_key)
    (dest / "meta.json").write_text(json.dumps(meta, indent=2))
    missing = [p for p in ("container.json", "image.tar") if not (dest / p).exists()]
    if missing:
        raise RuntimeError(f"Export unvollständig, es fehlt: {', '.join(missing)}")
    return meta


def _copy_or_fetch(src: Path, dest: Path, meta: dict, stream_target, rel_key: str, _log) -> None:
    if meta.get("streamed_target_id") is not None:
        _export_streamed(src, dest, meta, stream_target, rel_key, _log)
        (dest / "meta.json").write_text(json.dumps(meta))
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True)


def build_export(items: list[dict], out_file: Path, landscape: Optional[dict] = None,
                 on_log: Log = None, on_progress: Optional[Callable[[int, str, int], None]] = None) -> dict:
    """items: [{"record_path", "stream_target", "fetch_missing"}] - one per
    container backup. landscape: {"path", "name"} for a group backup.
    Writes the export tar to out_file and returns its manifest."""
    _log = on_log or (lambda m: None)
    total = len(items) + 2
    with _staging() as tmp:
        root = Path(tmp)
        members = []
        for i, item in enumerate(items, 1):
            src = Path(item["record_path"])
            key = storage_sync._relative_key(src)
            if on_progress:
                on_progress(i, f"Exportiere {src.parent.name}", total)
            _log(f"Exportiere {key}")
            meta = export_container_dir(src, root / key, item.get("stream_target"), item.get("fetch_missing"),
                                        on_log=on_log)
            members.append({"name": meta.get("container_name") or src.parent.name, "key": key,
                            "created_at": meta.get("created_at")})
        manifest = {"format": FORMAT, "version": FORMAT_VERSION,
                    "type": "landscape" if landscape else "container",
                    "exported_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "encrypted": encryption.is_enabled(), "members": members}
        if landscape:
            lkey = storage_sync._relative_key(Path(landscape["path"]))
            ldir = root / lkey
            ldir.mkdir(parents=True, exist_ok=True)
            lsrc = Path(landscape["path"])
            lmeta = {}
            if lsrc.exists():
                if encryption.is_backup_encrypted(lsrc):
                    with encryption.decrypt_directory_to_temp(lsrc) as plain:
                        lmeta = _read_json(Path(plain) / "meta.json")
                else:
                    lmeta = _read_json(lsrc / "meta.json")
            lmeta = _normalize_meta(lmeta, lkey)
            lmeta.setdefault("landscape_name", landscape["name"])
            (ldir / "meta.json").write_text(json.dumps(lmeta, indent=2))
            for m in members:  # member links: rewritten to local paths on import
                (ldir / f"{m['name']}.json").write_text(json.dumps(
                    {"container_name": m["name"], "backup_key": m["key"], "backup_level": "full"}))
            manifest["landscape"] = {"name": landscape["name"], "key": lkey,
                                     "created_at": lmeta.get("created_at")}
        if encryption.is_enabled():
            if on_progress:
                on_progress(total - 1, "Verschlüssele Export", total)
            for m in members:
                encryption.encrypt_directory_in_place(root / m["key"])
            if landscape:
                encryption.encrypt_directory_in_place(root / manifest["landscape"]["key"])
        (root / MANIFEST).write_text(json.dumps(manifest, indent=2))
        if on_progress:
            on_progress(total, "Schreibe Exportdatei", total)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        partial = out_file.with_suffix(".partial")
        with tarfile.open(partial, "w") as tar:
            tar.add(root / MANIFEST, arcname=MANIFEST)
            for m in members:
                tar.add(root / m["key"], arcname=m["key"])
            if landscape:
                tar.add(root / manifest["landscape"]["key"], arcname=manifest["landscape"]["key"])
        partial.rename(out_file)
    _log(f"Export fertig: {out_file.stat().st_size} Bytes")
    return manifest


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _safe_members(tar: tarfile.TarFile, allowed_roots: set[str]):
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"Ungültiger Pfad in der Exportdatei: {name}")
        if not (m.isfile() or m.isdir()):
            raise RuntimeError(f"Ungültiger Eintrag in der Exportdatei: {name}")
        if name != MANIFEST and not any(name == r or name.startswith(r + "/") for r in allowed_roots):
            raise RuntimeError(f"Unerwarteter Eintrag in der Exportdatei: {name}")
        yield m


def read_manifest(export_file: Path) -> dict:
    with tarfile.open(export_file, "r") as tar:
        try:
            f = tar.extractfile(MANIFEST)
        except KeyError:
            f = None
        if f is None:
            raise RuntimeError("Keine Exportdatei des Docker Backup Managers (dbm-export.json fehlt)")
        manifest = json.loads(f.read())
    if manifest.get("format") != FORMAT or int(manifest.get("version", 0)) > FORMAT_VERSION:
        raise RuntimeError("Unbekanntes Exportformat")
    if manifest.get("encrypted") and not encryption.is_enabled():
        raise RuntimeError("Die Exportdatei ist verschlüsselt - auf diesem System denselben DBM_ENCRYPTION_KEY "
                           "setzen wie auf dem Quellsystem")
    return manifest


def _check_key(key: str) -> str:
    parts = Path(key).parts
    if not key or key.startswith("/") or ".." in parts or len(parts) not in (2, 3):
        raise RuntimeError(f"Ungültiger Sicherungspfad in der Exportdatei: {key}")
    return key


class ImportCancelled(Exception):
    pass


class _ProgressReader:
    """Wraps the export file for tarfile and reports every byte read, so the
    progress bar moves smoothly even through a single multi-GB image.tar.
    tarfile only reads headers while listing members and skips the data with
    seek(), so the read bytes add up to roughly the file size."""

    def __init__(self, f, on_bytes: Optional[Callable[[int], None]], should_cancel: Optional[Callable[[], bool]]):
        self._f = f
        self._on_bytes = on_bytes
        self._should_cancel = should_cancel

    def read(self, size=-1):
        if self._should_cancel and self._should_cancel():
            raise ImportCancelled("Vom Nutzer abgebrochen")
        data = self._f.read(size)
        if data and self._on_bytes:
            self._on_bytes(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._f, name)


def import_export(export_file: Path, db, on_log: Log = None,
                  on_step: Optional[Callable[[int, str, int], None]] = None,
                  on_bytes: Optional[Callable[[int], None]] = None,
                  should_cancel: Optional[Callable[[], bool]] = None) -> list:
    """Extracts an export into BACKUPS_DIR and creates the catalog records.
    Returns the created BackupRecords (group record last).

    Progress: on_step(1, "Entpacken", file_size) starts the extraction (then
    on_bytes(n) for every n bytes read from the file), on_step(2, ..., 0)
    starts taking the folders over into the catalog. should_cancel() is
    polled while extracting; raising ImportCancelled leaves nothing behind."""
    from app.backup_engine import dir_size_bytes
    from app.models import BackupRecord
    _log = on_log or (lambda m: None)
    _step = on_step or (lambda *a: None)
    manifest = read_manifest(export_file)
    keys = [_check_key(m["key"]) for m in manifest.get("members", [])]
    lkey = _check_key(manifest["landscape"]["key"]) if manifest.get("landscape") else None
    roots = set(keys) | ({lkey} if lkey else set())
    for k in roots:
        if (BACKUPS_DIR / k).exists():
            raise RuntimeError(f"Diese Sicherung ist hier schon vorhanden: {k}")
    _step(1, "Entpacken", export_file.stat().st_size)
    with _staging() as tmp, open(export_file, "rb") as raw, \
            tarfile.open(fileobj=_ProgressReader(raw, on_bytes, should_cancel), mode="r") as tar:
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(tmp, members=_safe_members(tar, roots), **kwargs)
        for k in roots:
            if not (Path(tmp) / k).is_dir():
                raise RuntimeError(f"Exportdatei unvollständig: {k} fehlt")
        _step(2, "In den Katalog übernehmen", 0)
        for k in roots:
            dest = BACKUPS_DIR / k
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(Path(tmp) / k), str(dest))
            _log(f"Importiert: {k}")

    created = []
    records = {}
    for m, k in zip(manifest["members"], keys):
        path = BACKUPS_DIR / k
        r = BackupRecord(backup_type="container", name=m["name"], path=str(path), status="ok",
                         size_bytes=dir_size_bytes(path), containers_json=json.dumps([m["name"]]),
                         backup_level="full", created_at=_parse(m.get("created_at")))
        db.add(r)
        records[k] = r
        created.append(r)
    if lkey:
        ldir = BACKUPS_DIR / lkey
        _rewrite_member_links(ldir, {k: str(BACKUPS_DIR / k) for k in keys})
        land = manifest["landscape"]
        lr = BackupRecord(backup_type="landscape", name=land["name"], path=str(ldir), status="ok",
                          size_bytes=dir_size_bytes(ldir),
                          containers_json=json.dumps([m["name"] for m in manifest["members"]]),
                          created_at=_parse(land.get("created_at")))
        db.add(lr)
        created.append(lr)
    db.commit()
    return created


def _rewrite_member_links(ldir: Path, path_by_key: dict) -> None:
    """Member links of the group point to the imported folders on this host."""
    for f in list(ldir.glob("*.json")) + list(ldir.glob("*.json.enc")):
        if f.name in ("meta.json", "meta.json.enc"):
            continue
        if f.suffix == ".enc":
            plain = f.with_suffix("")
            encryption.decrypt_file(f, plain)
            data = _read_json(plain)
            f.unlink()
        else:
            plain = f
            data = _read_json(f)
        key = data.pop("backup_key", None)
        if key in path_by_key:
            data["backup_path"] = path_by_key[key]
        data.pop("synthetic_path", None)
        plain.write_text(json.dumps(data))
        if encryption.is_enabled() and f.suffix == ".enc":
            encryption.encrypt_file(plain)


def _parse(value: Optional[str]) -> datetime.datetime:
    if value:
        try:
            return datetime.datetime.fromisoformat(value.rstrip("Z"))
        except ValueError:
            pass
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
