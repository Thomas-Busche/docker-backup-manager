"""
Incremental backup chains with synthetic full backups.

How it works (one chain per container):

  1. Initial full backup: every volume / bind mount is archived completely
     (volumes/<vol>.tar.gz, a normal tar.gz that plain `tar xzf` can restore),
     plus a manifest (volumes/<vol>.manifest.json.gz) listing every entry with
     its metadata and the SHA-256 of every regular file.
  2. Incremental backups: the volume is read again, but only regular files
     whose metadata (size, mtime, mode, owner, type, link target) changed
     compared with the parent's manifest are written to the archive. The
     manifest still lists the COMPLETE file set at that point in time and, per
     file, which backup ("src") holds its content. Deleted files are simply
     no longer listed. The image is only saved again if its ID changed.
  3. Synthetic full: after N incrementals the app combines the latest state of
     the chain - entirely from the backup data already in the repository,
     without touching the source container - into a new self-contained full
     backup, verifies it file by file against the manifest and starts a new
     chain on top of it.
  4. Restore of any point in time: the requested state is synthesized from the
     chain (every file is taken from the backup that holds it, every file's
     hash is checked on the way) into ordinary full archives, which then go
     through the normal restore path.

Every backup also stores SHA-256 checksums of its own files (meta "checksums")
so a chain can be checked for bit rot / missing parts without restoring it.

Backup "keys" are paths relative to the backup root: "<container>/<timestamp>".
They are portable: a chain restored on another host with a different
BACKUPS_DIR still resolves, because the root is derived from the backup's own
location (<root>/<container>/<timestamp>).
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import gzip
import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from app import encryption

logger = logging.getLogger("dbm.chain")

CHAIN_FORMAT = 1
MANIFEST_SUFFIX = ".manifest.json.gz"
ARCHIVE_SUFFIX = ".tar.gz"

LEVEL_FULL = "full"
LEVEL_INCREMENTAL = "incremental"
LEVEL_SYNTHETIC = "synthetic_full"
LEVELS = (LEVEL_FULL, LEVEL_INCREMENTAL, LEVEL_SYNTHETIC)

# Metadata fields that decide whether a regular file counts as changed.
_CHANGE_KEYS = ("t", "s", "m", "mo", "u", "g", "l")

FetchMissing = Optional[Callable[[str, Path], None]]
LogCallback = Optional[Callable[[str], None]]
ShouldCancel = Callable[[], bool]


class ChainError(RuntimeError):
    """A chain is incomplete or damaged (missing part, wrong checksum, ...)."""


def _never_cancel() -> bool:
    return False


# ---------------------------------------------------------------------------
# Helpers: reading files that may be encrypted at rest
# ---------------------------------------------------------------------------

def _staging_root() -> Path:
    from app.config import BACKUPS_DIR
    root = BACKUPS_DIR / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def plain_file(backup_dir: Path, rel: str) -> Iterator[Path]:
    """Yields a plaintext path for <backup_dir>/<rel>, decrypting `<rel>.enc`
    into a temp file first if the backup is encrypted at rest."""
    plain = Path(backup_dir) / rel
    if plain.exists():
        yield plain
        return
    enc = Path(backup_dir) / (rel + encryption.ENCRYPTED_SUFFIX)
    if not enc.exists():
        raise FileNotFoundError(str(plain))
    if not encryption.is_enabled():
        raise ChainError(
            f"{enc.name} ist verschlüsselt, aber DBM_ENCRYPTION_KEY ist nicht gesetzt - "
            "denselben Schlüssel wie beim Erstellen der Sicherung setzen."
        )
    fd, tmp_name = tempfile.mkstemp(dir=str(_staging_root()), prefix="dbm-chain-")
    import os
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        encryption.decrypt_file(enc, tmp)
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def file_exists(backup_dir: Path, rel: str) -> bool:
    p = Path(backup_dir) / rel
    return p.exists() or Path(str(p) + encryption.ENCRYPTED_SUFFIX).exists()


def read_json(backup_dir: Path, rel: str) -> dict:
    with plain_file(backup_dir, rel) as p:
        return json.loads(p.read_text())


def read_meta(backup_dir: Path) -> dict:
    try:
        return read_json(backup_dir, "meta.json")
    except FileNotFoundError:
        return {}


def read_manifest(backup_dir: Path, rel: str) -> dict:
    with plain_file(backup_dir, rel) as p:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(manifest, f, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_chain_meta(meta: dict) -> bool:
    return isinstance(meta.get("chain"), dict) and meta.get("backup_level") in LEVELS


def backup_key(backup_dir: Path) -> str:
    backup_dir = Path(backup_dir)
    return f"{backup_dir.parent.name}/{backup_dir.name}"


def root_of(backup_dir: Path) -> Path:
    return Path(backup_dir).parent.parent


def mount_entries(meta: dict) -> list[dict]:
    """Every archived mount of a chain backup: [{"kind", "name", "archive",
    "manifest"}] - "name" is the volume name or the bind's destination."""
    out = []
    for vol in meta.get("volumes", []) or []:
        base = _safe(vol)
        out.append({"kind": "volume", "name": vol,
                    "archive": f"volumes/{base}{ARCHIVE_SUFFIX}",
                    "manifest": f"volumes/{base}{MANIFEST_SUFFIX}"})
    for bind in meta.get("bind_mounts", []) or []:
        base = _safe(bind["destination"])
        out.append({"kind": "bind", "name": bind["destination"],
                    "archive": f"binds/{bind.get('filename') or base + ARCHIVE_SUFFIX}",
                    "manifest": f"binds/{base}{MANIFEST_SUFFIX}"})
    return out


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)


# ---------------------------------------------------------------------------
# Tar plumbing
# ---------------------------------------------------------------------------

class _ChunkReader:
    """File-like read() over an iterator of byte chunks (the helper
    container's tar stream)."""

    def __init__(self, chunks: Iterable[bytes]):
        self._it = iter(chunks)
        self._buf = bytearray()
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        while not self._eof and (size < 0 or len(self._buf) < size):
            try:
                self._buf.extend(next(self._it))
            except StopIteration:
                self._eof = True
        if size < 0:
            data = bytes(self._buf)
            self._buf.clear()
        else:
            data = bytes(self._buf[:size])
            del self._buf[:size]
        return data


class _HashingReader:
    def __init__(self, fileobj, should_cancel: ShouldCancel = _never_cancel):
        self._f = fileobj
        self._h = hashlib.sha256()
        self.count = 0
        self._should_cancel = should_cancel
        self._since_check = 0

    def read(self, size: int = -1) -> bytes:
        data = self._f.read(size)
        self._h.update(data)
        self.count += len(data)
        self._since_check += len(data)
        if self._since_check >= 8 * 1024 * 1024:
            self._since_check = 0
            if self._should_cancel():
                from app.backup_engine import BackupCancelled
                raise BackupCancelled("Abgebrochen")
        return data

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def _entry_from_tarinfo(ti: tarfile.TarInfo) -> dict:
    t = ti.type.decode("latin-1") if isinstance(ti.type, bytes) else str(ti.type)
    if ti.isreg():
        t = "0"  # normalize AREGTYPE/CONTTYPE/sparse to a plain regular file
    e = {"n": ti.name, "t": t, "s": ti.size if ti.isreg() else 0, "m": int(ti.mtime),
         "mo": ti.mode, "u": ti.uid, "g": ti.gid}
    if ti.uname:
        e["un"] = ti.uname
    if ti.gname:
        e["gn"] = ti.gname
    if ti.linkname:
        e["l"] = ti.linkname
    if ti.ischr() or ti.isblk():
        e["dm"], e["dn"] = ti.devmajor, ti.devminor
    return e


def _tarinfo_from_entry(e: dict) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(e["n"])
    ti.type = e["t"].encode("latin-1")
    ti.size = e.get("s", 0) if e["t"] == "0" else 0
    ti.mtime = e.get("m", 0)
    ti.mode = e.get("mo", 0o644)
    ti.uid = e.get("u", 0)
    ti.gid = e.get("g", 0)
    ti.uname = e.get("un", "")
    ti.gname = e.get("gn", "")
    ti.linkname = e.get("l", "")
    ti.devmajor = e.get("dm", 0)
    ti.devminor = e.get("dn", 0)
    return ti


def _unchanged(prev: Optional[dict], cur: dict) -> bool:
    if not prev or prev.get("t") != "0" or not prev.get("h") or not prev.get("src"):
        return False
    return all(prev.get(k) == cur.get(k) for k in _CHANGE_KEYS)


def _is_regular(e: dict) -> bool:
    return e.get("t") == "0"


# ---------------------------------------------------------------------------
# Capture: full or incremental archive of one mount
# ---------------------------------------------------------------------------

def capture(tar_chunks: Iterable[bytes], out_archive: Path, self_key: str,
            parent_manifest: Optional[dict] = None, gzip_level: int = 6,
            should_cancel: ShouldCancel = _never_cancel) -> dict:
    """Reads an uncompressed tar stream of a volume and writes out_archive.

    parent_manifest=None -> full: every entry goes into the archive.
    Otherwise -> incremental: only changed/new regular files are archived.

    Returns the new manifest (complete file list at this point in time)."""
    parent_by_name = {}
    if parent_manifest:
        parent_by_name = {e["n"]: e for e in parent_manifest.get("entries", [])}
    full = parent_manifest is None
    entries: list[dict] = []
    stats = {"files": 0, "changed_files": 0, "changed_bytes": 0, "unchanged_files": 0,
             "total_bytes": 0, "deleted": 0}

    out_archive.parent.mkdir(parents=True, exist_ok=True)
    reader = _ChunkReader(tar_chunks)
    with open(out_archive, "wb") as raw, \
            gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=gzip_level, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w|", format=tarfile.GNU_FORMAT) as dst, \
            tarfile.open(fileobj=reader, mode="r|") as src:
        for ti in src:
            e = _entry_from_tarinfo(ti)
            if ti.isreg():
                stats["files"] += 1
                stats["total_bytes"] += ti.size
                prev = parent_by_name.get(e["n"])
                if not full and _unchanged(prev, e):
                    e["h"], e["src"] = prev["h"], prev["src"]
                    stats["unchanged_files"] += 1
                    entries.append(e)
                    continue  # stream mode skips the member's data on its own
                fobj = src.extractfile(ti)
                hr = _HashingReader(fobj, should_cancel)
                out_ti = _tarinfo_from_entry(e)
                dst.addfile(out_ti, hr)
                if hr.count != ti.size:
                    raise ChainError(f"Datei {ti.name}: {hr.count} statt {ti.size} Bytes gelesen")
                e["h"], e["src"] = hr.hexdigest(), self_key
                stats["changed_files"] += 1
                stats["changed_bytes"] += ti.size
            elif full:
                dst.addfile(_tarinfo_from_entry(e))
            entries.append(e)
    if parent_manifest:
        current = {e["n"] for e in entries}
        stats["deleted"] = sum(1 for n in parent_by_name if n not in current)
    return {"format": CHAIN_FORMAT, "entries": entries, "stats": stats}


# ---------------------------------------------------------------------------
# Synthesis: rebuild the complete state of one mount from the chain
# ---------------------------------------------------------------------------

def synthesize(manifest: dict, open_archive: Callable[[str], contextlib.AbstractContextManager],
               out_fileobj, should_cancel: ShouldCancel = _never_cancel,
               on_log: LogCallback = None) -> dict:
    """Writes an uncompressed tar of the full state described by `manifest` to
    out_fileobj. open_archive(src_key) must be a context manager yielding the
    local plaintext .tar.gz path of the archive held by backup `src_key`.

    Every regular file's content is hashed on the way and compared with the
    manifest - a damaged or incomplete chain raises ChainError instead of
    silently producing a wrong restore.

    Order: directories, regular files (grouped by the archive holding them),
    symlinks/special files, hard links last (their target must exist)."""
    entries = manifest.get("entries", [])
    by_src: dict[str, dict[str, dict]] = {}
    for e in entries:
        if _is_regular(e):
            if not e.get("src"):
                raise ChainError(f"Manifest ohne Quelle für {e['n']}")
            by_src.setdefault(e["src"], {})[e["n"]] = e
    stats = {"files": 0, "bytes": 0, "sources": len(by_src)}

    with tarfile.open(fileobj=out_fileobj, mode="w|", format=tarfile.GNU_FORMAT) as dst:
        for e in entries:
            if e["t"] == "5":
                dst.addfile(_tarinfo_from_entry(e))
        # Sources in chain order (oldest first) - keys end with a sortable timestamp.
        for src_key in sorted(by_src, key=lambda k: k.split("/")[-1]):
            wanted = by_src[src_key]
            remaining = set(wanted)
            if on_log:
                on_log(f"Übernehme {len(wanted)} Datei(en) aus {src_key}")
            with open_archive(src_key) as archive_path:
                with tarfile.open(archive_path, mode="r|gz") as src:
                    for ti in src:
                        if ti.name not in remaining or not ti.isreg():
                            continue
                        e = wanted[ti.name]
                        if ti.size != e["s"]:
                            raise ChainError(f"{ti.name} in {src_key}: Größe {ti.size} statt {e['s']}")
                        hr = _HashingReader(src.extractfile(ti), should_cancel)
                        dst.addfile(_tarinfo_from_entry(e), hr)
                        if hr.hexdigest() != e["h"]:
                            raise ChainError(f"Prüfsumme falsch: {ti.name} in {src_key}")
                        remaining.discard(ti.name)
                        stats["files"] += 1
                        stats["bytes"] += ti.size
            if remaining:
                sample = ", ".join(sorted(remaining)[:3])
                raise ChainError(f"{len(remaining)} Datei(en) fehlen in {src_key} (z. B. {sample})")
        for e in entries:
            if e["t"] not in ("0", "5", "1"):
                dst.addfile(_tarinfo_from_entry(e))
        for e in entries:
            if e["t"] == "1":
                dst.addfile(_tarinfo_from_entry(e))
    return stats


class _NullSink:
    def write(self, b) -> int:
        return len(b)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Resolving chain members (local, or fetched from a storage target)
# ---------------------------------------------------------------------------

class ChainResolver:
    """Finds the backup directories a chain needs, fetching missing ones via
    fetch_missing(key, dest_dir) (e.g. download from a storage target)."""

    def __init__(self, root: Path, fetch_missing: FetchMissing = None, on_log: LogCallback = None):
        self.root = Path(root)
        self.fetch_missing = fetch_missing
        self.on_log = on_log

    def dir(self, key: str) -> Path:
        d = self.root / key
        if (d / "meta.json").exists() or (d / ("meta.json" + encryption.ENCRYPTED_SUFFIX)).exists():
            return d
        if self.fetch_missing is None:
            raise ChainError(f"Teil der Sicherungskette fehlt: {key}")
        if self.on_log:
            self.on_log(f"Lade fehlenden Kettenteil {key} vom Speicherziel")
        try:
            self.fetch_missing(key, d)
        except Exception as exc:  # noqa: BLE001
            raise ChainError(f"Kettenteil {key} fehlt lokal und konnte nicht geladen werden: {exc}") from exc
        if not file_exists(d, "meta.json"):
            raise ChainError(f"Kettenteil {key} ist unvollständig (meta.json fehlt)")
        return d

    @contextlib.contextmanager
    def archive_for(self, mount: dict, key: str):
        with plain_file(self.dir(key), mount["archive"]) as p:
            yield p


def _mount_by_name(meta: dict, kind: str, name: str) -> dict:
    for m in mount_entries(meta):
        if m["kind"] == kind and m["name"] == name:
            return m
    raise ChainError(f"{'Volume' if kind == 'volume' else 'Bind-Mount'} '{name}' ist nicht in dieser Sicherung")


def synthesize_mount(backup_dir: Path, mount: dict, out_fileobj, resolver: ChainResolver,
                     should_cancel: ShouldCancel = _never_cancel) -> dict:
    manifest = read_manifest(backup_dir, mount["manifest"])
    return synthesize(manifest, lambda key: resolver.archive_for(mount, key), out_fileobj,
                      should_cancel=should_cancel, on_log=resolver.on_log)


def write_synthesized_archive(backup_dir: Path, mount: dict, dest: Path, resolver: ChainResolver,
                              gzip_level: int = 1, should_cancel: ShouldCancel = _never_cancel) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=gzip_level, mtime=0) as gz:
        return synthesize_mount(backup_dir, mount, gz, resolver, should_cancel)


def dependencies(meta: dict) -> list[str]:
    chain = meta.get("chain") or {}
    return list(chain.get("depends_on") or [])


# ---------------------------------------------------------------------------
# Finding the parent for a new incremental
# ---------------------------------------------------------------------------

def find_parent(container_dir: Path) -> Optional[tuple[Path, dict]]:
    """Newest complete chain backup of a container (any level), or None."""
    if not container_dir.exists():
        return None
    for d in sorted((p for p in container_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        try:
            meta = read_meta(d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kettenteil %s nicht lesbar, überspringe: %s", d, exc)
            continue
        if not meta:
            continue
        if not is_chain_meta(meta):
            # Newest backup is an ordinary full backup (no manifest) -> start a new chain
            return None
        try:
            missing = [m["manifest"] for m in mount_entries(meta) if not file_exists(d, m["manifest"])]
        except Exception:  # noqa: BLE001
            missing = ["?"]
        if missing:
            logger.warning("Kettenteil %s unvollständig (%s) - neue Kette", d, missing)
            return None
        return d, meta
    return None


def chain_members(latest_dir: Path) -> list[str]:
    """Keys of every backup from the chain's start up to latest_dir (oldest
    first), following the parent links - including incrementals whose data
    is no longer needed by the latest state."""
    latest_dir = Path(latest_dir)
    root = root_of(latest_dir)
    keys = []
    key: Optional[str] = backup_key(latest_dir)
    while key and key not in keys:
        keys.append(key)
        d = root / key
        if not file_exists(d, "meta.json"):
            break
        key = (read_meta(d).get("chain") or {}).get("parent")
    return list(reversed(keys))


def new_chain_info(self_key: str) -> dict:
    return {"id": uuid.uuid4().hex, "parent": None, "base": self_key, "seq": 0,
            "depends_on": [], "image_src": self_key}


def incremental_chain_info(parent_key: str, parent_meta: dict, self_key: str) -> dict:
    pc = parent_meta.get("chain") or {}
    return {"id": pc.get("id"), "parent": parent_key, "base": pc.get("base"),
            "seq": int(pc.get("seq", 0)) + 1, "depends_on": [], "image_src": self_key}


def finalize_depends(chain: dict, manifests: list[dict], self_key: str) -> None:
    deps = set()
    for m in manifests:
        for e in m.get("entries", []):
            if e.get("src") and e["src"] != self_key:
                deps.add(e["src"])
    if chain.get("image_src") and chain["image_src"] != self_key:
        deps.add(chain["image_src"])
    chain["depends_on"] = sorted(deps, key=lambda k: k.split("/")[-1])


def compute_checksums(backup_dir: Path) -> dict:
    out = {}
    for p in sorted(Path(backup_dir).rglob("*")):
        if p.is_file() and p.name != "meta.json":
            out[p.relative_to(backup_dir).as_posix()] = sha256_file(p)
    return out


# ---------------------------------------------------------------------------
# Synthetic full
# ---------------------------------------------------------------------------

def _timestamp_after(container_dir: Path) -> str:
    from app.clock import utcnow
    now = utcnow()
    while True:
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        if not (container_dir / ts).exists():
            return ts
        now += datetime.timedelta(seconds=1)


def create_synthetic_full(latest_dir: Path, on_log: LogCallback = None,
                          should_cancel: ShouldCancel = _never_cancel,
                          gzip_level: int = 6, fetch_missing: FetchMissing = None) -> Path:
    """Combines the chain ending at latest_dir into a new, self-contained full
    backup next to it - only from data in the repository, the container is
    not read again. The result is verified before it is returned; on any
    error the partial directory is removed and the chain stays as it was."""
    def _log(msg: str) -> None:
        if on_log:
            on_log(msg)

    latest_dir = Path(latest_dir)
    meta = read_meta(latest_dir)
    if not is_chain_meta(meta):
        raise ChainError("Synthetic Full nur aus einer inkrementellen Kette möglich")
    root = root_of(latest_dir)
    container_dir = latest_dir.parent
    latest_key = backup_key(latest_dir)
    new_dir = container_dir / _timestamp_after(container_dir)
    new_key = backup_key(new_dir)
    resolver = ChainResolver(root, fetch_missing, on_log)
    _log(f"Synthetic Full: kombiniere Kette bis {latest_key} im Backup-Speicher")

    new_dir.mkdir(parents=True)
    try:
        for rel in ("container.json", "networks.json"):
            if file_exists(latest_dir, rel):
                with plain_file(latest_dir, rel) as p:
                    shutil.copy2(p, new_dir / rel)
        image_src = (meta.get("chain") or {}).get("image_src") or latest_key
        with plain_file(resolver.dir(image_src), "image.tar") as p:
            _copy_or_link(p, new_dir / "image.tar")

        manifests = []
        totals = {"files": 0, "bytes": 0}
        for mount in mount_entries(meta):
            if should_cancel():
                from app.backup_engine import BackupCancelled
                raise BackupCancelled("Synthetic Full abgebrochen")
            src_manifest = read_manifest(latest_dir, mount["manifest"])
            _log(f"Synthetic Full: {mount['kind']} {mount['name']}")
            new_manifest = _synthesize_to_full(src_manifest, mount, new_dir, new_key, resolver,
                                               gzip_level, should_cancel)
            manifests.append(new_manifest)
            totals["files"] += new_manifest["stats"]["files"]
            totals["bytes"] += new_manifest["stats"]["total_bytes"]

        chain = new_chain_info(new_key)
        chain["synthesized_from"] = sorted(set(dependencies(meta)) | {latest_key},
                                           key=lambda k: k.split("/")[-1])
        chain["represents"] = latest_key
        new_meta = copy.deepcopy(meta)
        new_meta.pop("checksums", None)
        new_meta["backup_level"] = LEVEL_SYNTHETIC
        new_meta["chain"] = chain
        new_meta["data_as_of"] = meta.get("data_as_of") or meta.get("created_at")
        new_meta["created_at"] = _now_iso()
        new_meta["chain_stats"] = {"files": totals["files"], "total_bytes": totals["bytes"],
                                   "changed_files": totals["files"], "changed_bytes": totals["bytes"]}
        new_meta["checksums"] = compute_checksums(new_dir)
        (new_dir / "meta.json").write_text(json.dumps(new_meta, indent=2))

        _log("Synthetic Full: Integritätsprüfung")
        report = verify(new_dir, should_cancel=should_cancel)
        if not report["ok"]:
            raise ChainError("Integritätsprüfung des Synthetic Full fehlgeschlagen: " + "; ".join(report["errors"]))
        _log(f"Synthetic Full geprüft: {report['checked_files']} Dateien in Ordnung")
        if encryption.is_enabled():
            encryption.encrypt_directory_in_place(new_dir)
        return new_dir
    except BaseException:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise


def _synthesize_to_full(src_manifest: dict, mount: dict, new_dir: Path, new_key: str,
                        resolver: ChainResolver, gzip_level: int, should_cancel: ShouldCancel) -> dict:
    """Synthesized tar -> re-read through capture() as a full, which writes the
    standard archive and a fresh manifest whose sources all point at new_key."""
    import threading
    import os

    r_fd, w_fd = os.pipe()
    err: list[BaseException] = []

    def _produce():
        try:
            with os.fdopen(w_fd, "wb") as sink:
                synthesize(src_manifest, lambda key: resolver.archive_for(mount, key), sink,
                           should_cancel=should_cancel)
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=_produce, daemon=True)
    t.start()
    try:
        with os.fdopen(r_fd, "rb") as source:
            chunks = iter(lambda: source.read(1024 * 1024), b"")
            try:
                manifest = capture(chunks, new_dir / mount["archive"], new_key, None, gzip_level, should_cancel)
            except BaseException:
                # Drain so the producer thread doesn't block forever on a full pipe
                for _ in chunks:
                    pass
                raise
    finally:
        t.join()
    if err:
        raise err[0]
    # Same content as the source state -> hashes must be identical
    want = {e["n"]: e.get("h") for e in src_manifest.get("entries", []) if _is_regular(e)}
    got = {e["n"]: e.get("h") for e in manifest["entries"] if _is_regular(e)}
    if want != got:
        raise ChainError(f"Synthetic Full für {mount['name']} weicht vom Quellstand ab")
    write_manifest(new_dir / mount["manifest"], manifest)
    return manifest


def _copy_or_link(src: Path, dest: Path) -> None:
    import os
    try:
        if not str(src).startswith(str(_staging_root())):
            os.link(src, dest)
            return
    except OSError:
        pass
    shutil.copy2(src, dest)


def _now_iso() -> str:
    from app.clock import utcnow
    return utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Integrity check
# ---------------------------------------------------------------------------

def verify(backup_dir: Path, fetch_missing: FetchMissing = None, deep: bool = True,
           should_cancel: ShouldCancel = _never_cancel, on_log: LogCallback = None) -> dict:
    """Checks a backup and everything it depends on.

    - every chain member it needs exists (fetched from a target if allowed)
    - every file's stored SHA-256 checksum still matches (detects bit rot)
    - deep=True: the full state of every volume/bind is synthesized from the
      chain and every file's content hash is compared with the manifest

    Never raises for a damaged backup - returns {"ok": False, "errors": [...]}."""
    backup_dir = Path(backup_dir)
    errors: list[str] = []
    report = {"ok": False, "backup": backup_key(backup_dir), "level": None, "dependencies": [],
              "checked_files": 0, "checked_archives": 0, "errors": errors}
    try:
        meta = read_meta(backup_dir)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"meta.json nicht lesbar: {exc}")
        return report
    if not meta:
        errors.append("meta.json fehlt")
        return report
    report["level"] = meta.get("backup_level") or "full"
    if not is_chain_meta(meta):
        return _verify_legacy(backup_dir, meta, report)

    resolver = ChainResolver(root_of(backup_dir), fetch_missing, on_log)
    deps = dependencies(meta)
    report["dependencies"] = deps
    for key in [backup_key(backup_dir)] + deps:
        try:
            d = resolver.dir(key) if key != backup_key(backup_dir) else backup_dir
            m = meta if d == backup_dir else read_meta(d)
            for rel, digest in (m.get("checksums") or {}).items():
                if not file_exists(d, rel):
                    errors.append(f"{key}: Datei {rel} fehlt")
                    continue
                with plain_file(d, rel) as p:
                    if sha256_file(p) != digest:
                        errors.append(f"{key}: Prüfsumme von {rel} stimmt nicht")
                report["checked_archives"] += 1
        except ChainError as exc:
            errors.append(str(exc))
    if deep and not errors:
        for mount in mount_entries(meta):
            try:
                st = synthesize_mount(backup_dir, mount, _NullSink(), resolver, should_cancel)
                report["checked_files"] += st["files"]
            except ChainError as exc:
                errors.append(f"{mount['name']}: {exc}")
            except (tarfile.TarError, OSError, EOFError) as exc:
                errors.append(f"{mount['name']}: Archiv beschädigt ({exc})")
    report["ok"] = not errors
    return report


def _verify_legacy(backup_dir: Path, meta: dict, report: dict) -> dict:
    """Ordinary full backup without manifest: check every archive can be read
    to the end (gzip CRC + tar structure)."""
    errors = report["errors"]
    if meta.get("streamed_target_id") is not None:
        report["note"] = "Daten liegen auf dem Speicherziel (Streaming) - lokal nur Metadaten geprüft"
        report["ok"] = True
        return report
    for sub in ("volumes", "binds"):
        d = backup_dir / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            rel = p.relative_to(backup_dir).as_posix()
            if rel.endswith(encryption.ENCRYPTED_SUFFIX):
                rel = rel[: -len(encryption.ENCRYPTED_SUFFIX)]
            if not rel.endswith(ARCHIVE_SUFFIX):
                continue
            try:
                with plain_file(backup_dir, rel) as plain, tarfile.open(plain, "r|gz") as tf:
                    for ti in tf:
                        if ti.isreg():
                            f = tf.extractfile(ti)
                            while f.read(1024 * 1024):
                                pass
                            report["checked_files"] += 1
                report["checked_archives"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: {exc}")
    report["ok"] = not errors
    return report


# ---------------------------------------------------------------------------
# Restore: materialize a point in time as an ordinary full backup
# ---------------------------------------------------------------------------

def materialize(backup_dir: Path, dest_dir: Path, fetch_missing: FetchMissing = None,
                on_log: LogCallback = None, should_cancel: ShouldCancel = _never_cancel,
                only_mount: Optional[dict] = None) -> dict:
    """Writes the complete state of backup_dir (any chain level) into dest_dir
    in plaintext, laid out like an ordinary full backup (container.json,
    networks.json, meta.json, image.tar, volumes/*.tar.gz, binds/*.tar.gz),
    so the normal restore code can take it from there.

    only_mount: {"kind", "name"} - only that one volume/bind (single-volume
    restore); container config and image are skipped then."""
    backup_dir = Path(backup_dir)
    meta = read_meta(backup_dir)
    if not is_chain_meta(meta):
        raise ChainError("Keine inkrementelle Sicherung")
    resolver = ChainResolver(root_of(backup_dir), fetch_missing, on_log)
    dest_dir.mkdir(parents=True, exist_ok=True)
    mounts = mount_entries(meta)
    if only_mount:
        mounts = [_mount_by_name(meta, only_mount["kind"], only_mount["name"])]
    else:
        for rel in ("container.json", "networks.json"):
            if file_exists(backup_dir, rel):
                with plain_file(backup_dir, rel) as p:
                    shutil.copy2(p, dest_dir / rel)
        image_src = (meta.get("chain") or {}).get("image_src") or backup_key(backup_dir)
        if on_log:
            on_log(f"Image aus {image_src}")
        with plain_file(resolver.dir(image_src) if image_src != backup_key(backup_dir) else backup_dir,
                        "image.tar") as p:
            shutil.copy2(p, dest_dir / "image.tar")
    for mount in mounts:
        if on_log:
            on_log(f"Stelle Zeitpunkt für {mount['kind']} {mount['name']} aus der Kette zusammen")
        write_synthesized_archive(backup_dir, mount, dest_dir / mount["archive"], resolver,
                                  should_cancel=should_cancel)
    plain_meta = copy.deepcopy(meta)
    plain_meta["materialized_from"] = backup_key(backup_dir)
    (dest_dir / "meta.json").write_text(json.dumps(plain_meta, indent=2))
    return plain_meta
