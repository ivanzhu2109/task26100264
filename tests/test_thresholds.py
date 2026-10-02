"""Parity threshold: warn strictly below 95%, never affected by rounding."""

from config_snapshots import Comparison


def _comparison(same, union):
    return Comparison(same_count=same, union_count=union)


def test_exactly_95_percent_does_not_warn():
    report = _comparison(same=95, union=100)
    assert report.parity == 95.0
    assert not report.warning


def test_below_95_percent_warns():
    assert _comparison(same=94, union=100).warning
    assert _comparison(same=189, union=200).warning


def test_rounding_never_hides_warning():
    # 94.95% displays as 95.0 when rounded to one decimal, but must warn.
    report = _comparison(same=1899, union=2000)
    assert round(report.parity, 1) == 95.0
    assert report.warning
    assert _comparison(same=94999, union=100000).warning


def test_empty_snapshots_have_full_parity(store):
    empty_a = store.create_snapshot({})
    empty_b = store.derive_snapshot(empty_a)
    report = store.compare(empty_a, empty_b)
    assert report.parity == 100.0
    assert not report.warning
    assert report.same_count == 0 and report.union_count == 0


def test_empty_against_nonempty(store):
    empty = store.create_snapshot({})
    full = store.create_snapshot({("ns", "k"): "v"})
    report = store.compare(empty, full)
    assert report.added == {("ns", "k")}
    assert report.parity == 0.0
    assert report.warning


def test_threshold_integration(store):
    items = {("ns", f"k{i:04d}"): f"v{i}" for i in range(2000)}
    root = store.create_snapshot(items)
    exactly_95 = store.derive_snapshot(
        root, writes={("ns", f"k{i:04d}"): "x" for i in range(100)})
    report = store.compare(root, exactly_95)
    assert report.same_count == 1900 and report.union_count == 2000
    assert report.parity == 95.0 and not report.warning

    below_95 = store.derive_snapshot(
        root, writes={("ns", f"k{i:04d}"): "x" for i in range(101)})
    report = store.compare(root, below_95)
    assert report.same_count == 1899 and report.union_count == 2000
    assert report.warning
