"""Comparison of two snapshots based on their final content."""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import OP_WRITE

ItemId = tuple[str, str]  # (namespace, key)

_ABSENT = object()


@dataclass(frozen=True)
class Comparison:
    """Result of comparing snapshot A (baseline) with snapshot B."""

    added: frozenset[ItemId] = field(default_factory=frozenset)
    removed: frozenset[ItemId] = field(default_factory=frozenset)
    changed: frozenset[ItemId] = field(default_factory=frozenset)
    same_count: int = 0
    union_count: int = 0

    @property
    def parity(self) -> float:
        """Percentage of identical items over the union; 100.0 when both empty."""
        if self.union_count == 0:
            return 100.0
        return self.same_count / self.union_count * 100.0

    @property
    def warning(self) -> bool:
        """True when parity is strictly below 95% (exact integer comparison)."""
        return 20 * self.same_count < 19 * self.union_count


def compare_snapshots(store, a_id: int, b_id: int) -> Comparison:
    """Compare two snapshots; only final content matters, never history."""
    store._ensure_loaded()
    meta = store._meta
    if a_id not in meta:
        raise KeyError(f"unknown snapshot id: {a_id}")
    if b_id not in meta:
        raise KeyError(f"unknown snapshot id: {b_id}")

    ancestors_a: dict[int, int] = {}
    node = a_id
    while node is not None:
        parent, depth, _ = meta[node]
        ancestors_a[node] = depth
        node = parent

    lca_id = b_id
    while lca_id is not None and lca_id not in ancestors_a:
        lca_id = meta[lca_id][0]

    if lca_id is None:
        return _compare_distinct_roots(store, a_id, b_id)

    lca_depth = ancestors_a[lca_id]
    eff_a = _effective_ops(store, a_id, lca_depth)
    eff_b = _effective_ops(store, b_id, lca_depth)
    eff_lca = _effective_ops(store, lca_id, -1)

    root_id = lca_id
    while meta[root_id][0] is not None:
        root_id = meta[root_id][0]
    root_content = store._root_content(root_id)
    root_count = meta[root_id][2]

    def lca_value(item: ItemId):
        op_value = eff_lca.get(item)
        if op_value is not None:
            return op_value[1] if op_value[0] == OP_WRITE else None
        return root_content.get(item)

    lca_count = root_count
    for item, (op, _value) in eff_lca.items():
        in_root = item in root_content
        if op == OP_WRITE and not in_root:
            lca_count += 1
        elif op != OP_WRITE and in_root:
            lca_count -= 1

    touched = eff_a.keys() | eff_b.keys()
    # Items untouched below the LCA are identical on both sides; their
    # count is the LCA content size minus touched items present in the LCA.
    base = lca_count - sum(
        1 for item in touched if lca_value(item) is not None
    )

    added: set[ItemId] = set()
    removed: set[ItemId] = set()
    changed: set[ItemId] = set()
    same = base
    union = base
    for item in touched:
        value_a = _side_value(eff_a.get(item), item, lca_value)
        value_b = _side_value(eff_b.get(item), item, lca_value)
        if value_a is None and value_b is None:
            continue
        union += 1
        if value_a is None:
            added.add(item)
        elif value_b is None:
            removed.add(item)
        elif value_a == value_b:
            same += 1
        else:
            changed.add(item)

    return Comparison(
        added=frozenset(added),
        removed=frozenset(removed),
        changed=frozenset(changed),
        same_count=same,
        union_count=union,
    )


def _side_value(op_value, item, lca_value):
    """Final value of an item on one side; None means absent."""
    if op_value is None:
        return lca_value(item)
    return op_value[1] if op_value[0] == OP_WRITE else None


def _effective_ops(store, start_id: int, stop_depth: int) -> dict[ItemId, tuple[int, str]]:
    """Net effect of deltas below/at start down to (excl.) stop_depth.

    Walking from the leaf towards the root, the first occurrence of an
    item is the last applied operation, i.e. its effective state.
    """
    meta = store._meta
    deltas = store._deltas
    effective: dict[ItemId, tuple[int, str]] = {}
    node = start_id
    while node is not None and meta[node][1] > stop_depth:
        for ns, key, op, value in deltas[node]:
            effective.setdefault((ns, key), (op, value))
        node = meta[node][0]
    return effective


def _materialize(store, snapshot_id: int) -> dict[ItemId, str]:
    """Full final content of a snapshot (used for distinct-root compares)."""
    meta = store._meta
    path = []
    node = snapshot_id
    while node is not None:
        path.append(node)
        node = meta[node][0]
    content = dict(store._root_content(path[-1]))
    for node in reversed(path[:-1]):
        for ns, key, op, value in store._deltas[node]:
            if op == OP_WRITE:
                content[(ns, key)] = value
            else:
                content.pop((ns, key), None)
    return content


def _compare_distinct_roots(store, a_id: int, b_id: int) -> Comparison:
    content_a = _materialize(store, a_id)
    content_b = _materialize(store, b_id)
    added = frozenset(content_b.keys() - content_a.keys())
    removed = frozenset(content_a.keys() - content_b.keys())
    changed = frozenset(
        item for item in content_a.keys() & content_b.keys()
        if content_a[item] != content_b[item]
    )
    same = sum(
        1 for item in content_a.keys() & content_b.keys()
        if content_a[item] == content_b[item]
    )
    return Comparison(
        added=added,
        removed=removed,
        changed=changed,
        same_count=same,
        union_count=len(content_a.keys() | content_b.keys()),
    )
