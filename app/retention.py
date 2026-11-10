"""Pure retention-policy logic, kept separate from I/O so it is trivially unit-testable."""
from __future__ import annotations

import datetime
from app.clock import utcnow
from dataclasses import dataclass


@dataclass
class VersionInfo:
    id: int
    created_at: datetime.datetime


def versions_to_prune(versions: list[VersionInfo], retention_count: int = 0,
                       retention_days: int = 0) -> list[VersionInfo]:
    """Given all versions of a backup (newest first not required), return the
    ones that should be deleted according to the retention policy.

    retention_count <= 0 disables count-based pruning.
    retention_days <= 0 disables age-based pruning.
    A version is pruned if it violates EITHER active policy, but the newest
    version is never pruned even if both policies are effectively 0/1.
    """
    if not versions:
        return []

    ordered = sorted(versions, key=lambda v: v.created_at, reverse=True)
    keep_ids = {ordered[0].id}

    if retention_count and retention_count > 0:
        for v in ordered[:retention_count]:
            keep_ids.add(v.id)
    else:
        keep_ids.update(v.id for v in ordered)

    if retention_days and retention_days > 0:
        cutoff = utcnow() - datetime.timedelta(days=retention_days)
        keep_ids = {vid for vid in keep_ids if _lookup(ordered, vid).created_at >= cutoff} | {ordered[0].id}

    return [v for v in ordered if v.id not in keep_ids]


def _lookup(versions: list[VersionInfo], vid: int) -> VersionInfo:
    for v in versions:
        if v.id == vid:
            return v
    raise KeyError(vid)
