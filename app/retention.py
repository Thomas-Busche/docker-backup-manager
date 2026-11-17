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


@dataclass
class PolicyVersion:
    """A version as the extended policy sees it. chain_start: this version
    begins a new chain (full / synthetic full / self-contained restic
    snapshot); chain_key groups the versions of one chain."""
    id: int
    created_at: datetime.datetime
    chain_key: str


def versions_to_keep(versions: list[PolicyVersion], retention_count: int = 0, retention_days: int = 0,
                     retention_chains: int = 0, keep_daily: int = 0, keep_weekly: int = 0,
                     keep_monthly: int = 0) -> set[int]:
    """Extended retention ("Generationenprinzip"):

    * retention_chains > 0: keep the newest N chains completely (replaces the
      version count) - e.g. full + 7 incrementals, 3 chains kept.
    * otherwise retention_count > 0: keep the newest N versions (0 = all).
    * retention_days > 0: of those, only versions younger than N days.
    * keep_daily / keep_weekly / keep_monthly: additionally keep the newest
      version of each of the last N days / ISO weeks / months that have a
      backup - independent of the limits above (long-term history).
    The newest version is always kept. What a kept version needs for its
    restore (chain base) is protected by the caller."""
    if not versions:
        return set()
    ordered = sorted(versions, key=lambda v: v.created_at, reverse=True)
    if retention_chains and retention_chains > 0:
        chains: list[str] = []
        for v in ordered:
            if v.chain_key not in chains:
                chains.append(v.chain_key)
        wanted = set(chains[:retention_chains])
        base = {v.id for v in ordered if v.chain_key in wanted}
    elif retention_count and retention_count > 0:
        base = {v.id for v in ordered[:retention_count]}
    else:
        base = {v.id for v in ordered}
    if retention_days and retention_days > 0:
        cutoff = utcnow() - datetime.timedelta(days=retention_days)
        base = {v.id for v in ordered if v.id in base and v.created_at >= cutoff}

    def _bucketed(n: int, key) -> set[int]:
        out, seen = set(), []
        for v in ordered:
            k = key(v.created_at)
            if k in seen:
                continue
            if len(seen) >= n:
                break
            seen.append(k)
            out.add(v.id)
        return out

    keep = set(base) | {ordered[0].id}
    if keep_daily and keep_daily > 0:
        keep |= _bucketed(keep_daily, lambda d: d.date())
    if keep_weekly and keep_weekly > 0:
        keep |= _bucketed(keep_weekly, lambda d: tuple(d.isocalendar()[:2]))
    if keep_monthly and keep_monthly > 0:
        keep |= _bucketed(keep_monthly, lambda d: (d.year, d.month))
    return keep
