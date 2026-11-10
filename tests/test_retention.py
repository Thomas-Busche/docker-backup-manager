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
