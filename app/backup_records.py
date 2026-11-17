"""
Turning a BackupResult into catalog records, uploading it, and the chain
bookkeeping around it (dependencies, synthetic fulls, merged incrementals).

Shared by manual container backups, manual group backups and the scheduler
so all three treat incremental chains the same way.
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Callable, Optional

from app import storage_sync
from app.models import BackupRecord

logger = logging.getLogger("dbm.backup_records")


def _parse_iso(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def chain_columns(result) -> dict:
    """BackupRecord columns describing a result's place in a chain."""
    root = Path(result.path).parent.parent
    return {
        "backup_level": getattr(result, "level", None),
        "chain_id": getattr(result, "chain_id", None),
        "depends_json": json.dumps([str(root / k) for k in (getattr(result, "depends_on", None) or [])]),
        "data_as_of": _parse_iso(getattr(result, "data_as_of", None)),
        "added_bytes": getattr(result, "added_bytes", None),
    }


def _container_record(result, backup_type: str = "container") -> BackupRecord:
    return BackupRecord(
        backup_type=backup_type, name=result.name, path=str(result.path),
        status="ok" if result.ok else "failed", error=result.error,
        size_bytes=result.size_bytes, containers_json=json.dumps(result.containers or [result.name]),
        streamed_target_id=result.streamed_target_id,
        **(chain_columns(result) if result.ok else {}),
    )


def effective_target_ids(results: list, target_ids: Optional[list[int]]) -> Optional[list[int]]:
    """Adds the streaming target an incremental chain fell back from (see
    BackupResult.fallback_sync_target_id) to the upload targets."""
    if target_ids is None:
        return None
    ids = list(target_ids)
    for r in results:
        fid = getattr(r, "fallback_sync_target_id", None)
        if fid is not None and fid not in ids:
            ids.append(fid)
    return ids


def persist_result(db, result, backup_type: str, target_ids: Optional[list[int]], stream_target=None,
                   on_progress=None, keep_merged_incrementals: bool = True,
                   delete_record: Optional[Callable] = None) -> BackupRecord:
    """Creates the record(s) for a finished backup run, uploads everything to
    the storage targets and handles synthetic fulls. Returns the main record."""
    members = list(result.member_results or [])
    all_results = [result] + members
    ids = effective_target_ids(all_results, target_ids)
    fell_back = any(getattr(r, "fallback_sync_target_id", None) is not None for r in all_results)
    if fell_back:
        stream_target = None

    if backup_type == "landscape":
        record = BackupRecord(
            backup_type="landscape", name=result.name, path=str(result.path),
            status="ok" if result.ok else "failed", error=result.error, size_bytes=result.size_bytes,
            containers_json=json.dumps(result.containers),
            streamed_target_id=None if fell_back else result.streamed_target_id,
        )
    else:
        record = _container_record(result)
    db.add(record)
    db.commit()
    db.refresh(record)

    member_records = []
    for member in members:
        mr = _container_record(member)
        db.add(mr)
        member_records.append(mr)
    db.commit()

    # Synthetic fulls created during this run (one per container chain)
    synthetic_pairs = []
    for r in ([result] if backup_type != "landscape" else []) + members:
        if getattr(r, "synthetic", None) is not None and r.synthetic.ok:
            sr = _container_record(r.synthetic)
            db.add(sr)
            synthetic_pairs.append((sr, r.merged_keys))
    db.commit()

    if result.ok or (backup_type == "landscape" and member_records):
        if backup_type == "landscape":
            ok_members = [m for m in member_records if m.status == "ok" and Path(m.path).exists()]
            if result.ok:
                sync_results, member_synced = storage_sync.sync_backup_with_members(
                    result.path, [m.path for m in ok_members], ids,
                    stream_target=stream_target, on_progress=on_progress)
                record.synced_target_ids = json.dumps([r["target_id"] for r in sync_results if r["ok"]])
                for m in ok_members:
                    m.synced_target_ids = json.dumps(member_synced.get(m.path, []))
        elif result.ok:
            sync_results, _ = storage_sync.sync_backup_with_members(
                result.path, [], ids, stream_target=stream_target, on_progress=on_progress)
            record.synced_target_ids = json.dumps([r["target_id"] for r in sync_results if r["ok"]])
        for sr, _merged in synthetic_pairs:
            sync_results, _ = storage_sync.sync_backup_with_members(sr.path, [], ids, on_progress=on_progress)
            sr.synced_target_ids = json.dumps([r["target_id"] for r in sync_results if r["ok"]])
        db.commit()

    # Group backups reference their members' incrementals as restore points of
    # the whole group - those stay until retention drops the group backup.
    if not keep_merged_incrementals and delete_record is not None and backup_type != "landscape":
        for sr, merged in synthetic_pairs:
            prune_merged_incrementals(db, sr, merged, delete_record)
    return record


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def depends_paths(record: BackupRecord) -> list[str]:
    try:
        return list(json.loads(record.depends_json or "[]"))
    except ValueError:
        return []


def dependents(db, record: BackupRecord, exclude_ids: Optional[set] = None) -> list[BackupRecord]:
    """Records that need `record` for their restore."""
    q = db.query(BackupRecord).filter(BackupRecord.depends_json.like(f'%{json.dumps(record.path)}%'))
    out = [r for r in q.all() if record.path in depends_paths(r) and r.id != record.id]
    if exclude_ids:
        out = [r for r in out if r.id not in exclude_ids]
    return out


def required_paths(records: list[BackupRecord]) -> set[str]:
    out: set[str] = set()
    for r in records:
        out.update(depends_paths(r))
    return out


def prune_merged_incrementals(db, synthetic_record: BackupRecord, merged_keys: list[str],
                              delete_record: Callable) -> int:
    """After a verified synthetic full: deletes the incrementals it merged,
    unless some other record still needs one of them. The chain's base full
    stays (it's an independent restore point, subject to normal retention)."""
    root = Path(synthetic_record.path).parent.parent
    paths = {str(root / k) for k in merged_keys}
    candidates = db.query(BackupRecord).filter(BackupRecord.path.in_(paths)).all()
    deleted = 0
    # Newest first, so each one is no longer needed by a later one when its turn comes
    for r in sorted(candidates, key=lambda r: r.path, reverse=True):
        if r.backup_level != "incremental":
            continue
        if dependents(db, r):  # already-deleted newer merged ones are gone from the DB
            continue
        try:
            delete_record(r, db)
            db.commit()
            deleted += 1
        except Exception:  # noqa: BLE001
            logger.exception("Zusammengeführtes Inkrement %s konnte nicht gelöscht werden", r.path)
            db.rollback()
    return deleted
