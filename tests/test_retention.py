import datetime
from app.clock import utcnow

from app.retention import VersionInfo, versions_to_prune


def mk(id_, days_ago):
    return VersionInfo(id=id_, created_at=utcnow() - datetime.timedelta(days=days_ago))


def test_no_policy_keeps_everything():
    versions = [mk(1, 0), mk(2, 1), mk(3, 2)]
    assert versions_to_prune(versions, retention_count=0, retention_days=0) == []


def test_count_based_pruning_keeps_newest_n():
    versions = [mk(1, 0), mk(2, 1), mk(3, 2), mk(4, 3)]
    pruned = versions_to_prune(versions, retention_count=2, retention_days=0)
    assert {v.id for v in pruned} == {3, 4}


def test_days_based_pruning():
    versions = [mk(1, 0), mk(2, 5), mk(3, 10)]
    pruned = versions_to_prune(versions, retention_count=0, retention_days=7)
    assert {v.id for v in pruned} == {3}


def test_newest_version_never_pruned_even_if_old():
    versions = [mk(1, 100)]
    pruned = versions_to_prune(versions, retention_count=1, retention_days=1)
    assert pruned == []


def test_empty_list():
    assert versions_to_prune([], retention_count=5, retention_days=5) == []


def test_combined_policy_is_union_of_violations():
    # keep 1 by count, but retention_days=3 should still protect a 2-day-old one
    versions = [mk(1, 0), mk(2, 2), mk(3, 20)]
    pruned = versions_to_prune(versions, retention_count=2, retention_days=3)
    assert {v.id for v in pruned} == {3}


def _pv(id_, when, chain):
    from app.retention import PolicyVersion
    return PolicyVersion(id=id_, created_at=when, chain_key=chain)


def test_keep_newest_chains_completely():
    from app.retention import versions_to_keep
    base = datetime.datetime(2026, 1, 1)
    # chain A: 1-8 (full + 7 incrementals), chain B: 9-16, chain C: 17-19
    vs = [_pv(i, base + datetime.timedelta(days=i), "A" if i <= 8 else "B" if i <= 16 else "C") for i in range(1, 20)]
    assert versions_to_keep(vs, retention_count=3, retention_chains=2) == set(range(9, 20))
    assert versions_to_keep(vs, retention_chains=1) == {17, 18, 19}


def test_generations_daily_weekly_monthly():
    from app.retention import versions_to_keep
    # one backup every 6 hours for 120 days, each self-contained (restic)
    start = datetime.datetime(2026, 1, 1)
    vs = [_pv(i, start + datetime.timedelta(hours=6 * i), f"s{i}") for i in range(480)]
    keep = versions_to_keep(vs, retention_count=4, keep_daily=7, keep_weekly=4, keep_monthly=3)
    by_id = {v.id: v.created_at for v in vs}
    kept = sorted(by_id[i] for i in keep)
    newest = max(by_id.values())
    assert {i for i in range(476, 480)} <= keep  # the 4 newest
    days = {d.date() for d in kept if d >= newest - datetime.timedelta(days=7)}
    assert len(days) == 7  # one per day for the last 7 days
    months = {(d.year, d.month) for d in kept}
    assert len(months) == 3  # oldest kept month = 3rd newest month
    assert len(keep) <= 4 + 7 + 4 + 3


def test_extended_policy_without_new_settings_matches_the_old_one():
    from app.retention import versions_to_keep
    vs = [mk(i, i + 0.5) for i in range(10)]
    pvs = [_pv(v.id, v.created_at, f"s{v.id}") for v in vs]
    for count, days in [(0, 0), (3, 0), (0, 4), (2, 5)]:
        old_pruned = {v.id for v in versions_to_prune(vs, count, days)}
        assert versions_to_keep(pvs, count, days) == {v.id for v in vs} - old_pruned
