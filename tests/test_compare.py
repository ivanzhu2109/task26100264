"""Diff classification must reflect final content only, never history."""

from config_snapshots import SnapshotStore


def _build_long_chain(store, length=2000):
    """Chain that deletes, recreates, changes and reverts items."""
    root = store.create_snapshot({
        ("ns1", "keep"): "stable",
        ("ns1", "flip"): "original",
        ("ns1", "gone"): "temp",
        ("ns2", "keep"): "other-ns",
    })
    chain = [root]
    for i in range(length):
        parent = chain[-1]
        if i == 0:
            chain.append(store.derive_snapshot(
                parent, writes={("ns1", "flip"): "changed"}, deletes=[("ns1", "gone")]))
        elif i == 1:
            # Recreate deleted item with the same value, revert the change.
            chain.append(store.derive_snapshot(
                parent,
                writes={("ns1", "flip"): "original", ("ns1", "gone"): "temp"}))
        else:
            # Churn on a scratch key that always ends up reverted.
            chain.append(store.derive_snapshot(
                parent, writes={("ns1", "scratch"): f"x{i}"}))
            chain.append(store.derive_snapshot(
                chain[-1], deletes=[("ns1", "scratch")]))
    return chain


def test_long_chain_history_produces_no_false_diff(tmp_path):
    with SnapshotStore(str(tmp_path / "db.sqlite")) as store:
        chain = _build_long_chain(store)
        head = chain[2]  # content identical to root after revert+recreate
        tail = chain[-1]
        result = store.compare(chain[0], head)
        assert not result.added and not result.removed and not result.changed
        assert result.parity == 100.0 and not result.warning
        result = store.compare(chain[0], tail)
        assert not result.added and not result.removed and not result.changed
        assert result.parity == 100.0 and not result.warning


def test_same_content_via_different_branches(store):
    root = store.create_snapshot({("ns", "a"): "1", ("ns", "b"): "2"})
    # Branch 1: change then revert.
    b1 = store.derive_snapshot(root, writes={("ns", "a"): "9"})
    b1 = store.derive_snapshot(b1, writes={("ns", "a"): "1"})
    # Branch 2: delete then recreate with same value, plus revert another.
    b2 = store.derive_snapshot(root, deletes=[("ns", "b")])
    b2 = store.derive_snapshot(b2, writes={("ns", "b"): "2", ("ns", "c"): "z"})
    b2 = store.derive_snapshot(b2, deletes=[("ns", "c")])
    result = store.compare(b1, b2)
    assert not result.added and not result.removed and not result.changed
    assert result.same_count == 2 and result.union_count == 2
    assert result.parity == 100.0 and not result.warning


def test_cross_namespace_mixed_operations(store):
    a = store.create_snapshot({
        ("ns1", "same-key"): "keep",
        ("ns2", "same-key"): "keep",
        ("ns1", "mod"): "old",
        ("ns2", "mod"): "old",
        ("ns1", "del"): "x",
        ("ns2", "del"): "x",
    })
    b = store.derive_snapshot(
        a,
        writes={
            ("ns1", "mod"): "new",          # changed only in ns1
            ("ns1", "new-key"): "born",     # added only in ns1
            ("ns2", "del"): "x",            # rewritten with same value: no-op
        },
        deletes=[("ns1", "del")],           # deleted only in ns1
    )
    result = store.compare(a, b)
    assert result.added == {("ns1", "new-key")}
    assert result.removed == {("ns1", "del")}
    assert result.changed == {("ns1", "mod")}
    # Untouched twins in ns2 plus ns1/same-key must be completely unaffected.
    assert result.same_count == 4
    assert result.union_count == 7
    assert abs(result.parity - 4 / 7 * 100) < 1e-9


def test_empty_string_is_not_absence(store):
    a = store.create_snapshot({("ns", "k"): ""})
    b = store.derive_snapshot(a, deletes=[("ns", "k")])
    result = store.compare(a, b)
    assert result.removed == {("ns", "k")}
    assert result.same_count == 0 and result.union_count == 1
    c = store.derive_snapshot(b, writes={("ns", "k"): ""})
    assert store.compare(a, c).parity == 100.0


def test_compare_direction_matters(store):
    a = store.create_snapshot({("ns", "k"): "1"})
    b = store.derive_snapshot(a, writes={("ns", "k2"): "2"})
    forward = store.compare(a, b)
    backward = store.compare(b, a)
    assert forward.added == {("ns", "k2")} and not forward.removed
    assert backward.removed == {("ns", "k2")} and not backward.added


def test_compare_across_distinct_roots(store):
    a = store.create_snapshot({("ns", "x"): "1", ("ns", "y"): "2"})
    b = store.create_snapshot({("ns", "y"): "2", ("ns", "z"): "3"})
    b2 = store.derive_snapshot(b, writes={("ns", "y"): "2!"})
    result = store.compare(a, b2)
    assert result.added == {("ns", "z")}
    assert result.removed == {("ns", "x")}
    assert result.changed == {("ns", "y")}
    assert result.same_count == 0
    assert result.union_count == 3


def test_deep_branch_vs_chain_tip(store):
    root = store.create_snapshot({("ns", "base"): "0"})
    node = root
    for i in range(500):
        node = store.derive_snapshot(node, writes={("ns", f"hot{i % 5}"): str(i)})
    tip = node
    # Branch from the middle of the chain converging to the tip's content.
    middle = root
    for i in range(250):
        middle = store.derive_snapshot(middle, writes={("ns", f"hot{i % 5}"): str(i)})
    branch = middle
    for i in range(250, 500):
        branch = store.derive_snapshot(branch, writes={("ns", f"hot{i % 5}"): str(i)})
    result = store.compare(tip, branch)
    assert not result.added and not result.removed and not result.changed
    assert result.parity == 100.0
