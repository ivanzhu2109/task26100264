import pytest

from config_snapshots import SnapshotStore


def test_create_initial_snapshot(store):
    snap = store.create_snapshot({("ns", "k1"): "v1", ("ns", "k2"): "v2"})
    result = store.compare(snap, snap)
    assert result.same_count == 2
    assert result.union_count == 2
    assert not result.added and not result.removed and not result.changed


def test_derive_writes_and_deletes(store):
    base = store.create_snapshot({("ns", "a"): "1", ("ns", "b"): "2", ("ns", "c"): "3"})
    child = store.derive_snapshot(base, writes={("ns", "a"): "1x", ("ns", "d"): "4"},
                                  deletes=[("ns", "c")])
    result = store.compare(base, child)
    assert result.added == {("ns", "d")}
    assert result.removed == {("ns", "c")}
    assert result.changed == {("ns", "a")}


def test_delete_nonexistent_is_noop(store):
    base = store.create_snapshot({("ns", "a"): "1"})
    child = store.derive_snapshot(base, deletes=[("ns", "ghost")])
    result = store.compare(base, child)
    assert not result.added and not result.removed and not result.changed
    assert result.parity == 100.0


def test_snapshots_are_immutable_and_branchable(store):
    base = store.create_snapshot({("ns", "a"): "1"})
    branch1 = store.derive_snapshot(base, writes={("ns", "a"): "b1"})
    branch2 = store.derive_snapshot(base, writes={("ns", "a"): "b2"})
    # Deriving later branches must not affect earlier snapshots.
    assert store.compare(base, branch1).changed == {("ns", "a")}
    assert store.compare(base, branch2).changed == {("ns", "a")}
    again = store.derive_snapshot(branch1, writes={("ns", "x"): "9"})
    assert store.compare(branch1, again).added == {("ns", "x")}
    assert store.compare(base, branch1).changed == {("ns", "a")}


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "db.sqlite")
    with SnapshotStore(path) as store:
        root = store.create_snapshot({("ns", "a"): "1", ("ns", "b"): "2"})
        mid = store.derive_snapshot(root, writes={("ns", "a"): "1x"})
        leaf = store.derive_snapshot(mid, deletes=[("ns", "b")])
    with SnapshotStore(path) as reopened:
        result = reopened.compare(root, leaf)
        assert result.changed == {("ns", "a")}
        assert result.removed == {("ns", "b")}
        # Derivation still works after reopen.
        tail = reopened.derive_snapshot(leaf, writes={("ns", "c"): "3"})
        assert reopened.compare(leaf, tail).added == {("ns", "c")}


def test_batch_rejects_duplicate_items(store):
    base = store.create_snapshot({("ns", "a"): "1"})
    with pytest.raises(ValueError):
        store.derive_snapshot(base, deletes=[("ns", "a"), ("ns", "a")])
    with pytest.raises(ValueError):
        store.derive_snapshot(base, writes={("ns", "a"): "2"}, deletes=[("ns", "a")])


def test_values_must_be_strings(store):
    with pytest.raises(TypeError):
        store.create_snapshot({("ns", "a"): 1})
    base = store.create_snapshot({("ns", "a"): "1"})
    with pytest.raises(TypeError):
        store.derive_snapshot(base, writes={("ns", "a"): None})


def test_unknown_snapshot_raises(store):
    with pytest.raises(KeyError):
        store.compare(1, 2)
    with pytest.raises(KeyError):
        store.derive_snapshot(999, writes={("ns", "a"): "1"})
