"""Persistent snapshot store backed by SQLite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from . import schema
from .compare import Comparison, ItemId


class SnapshotStore:
    """Creates, derives and compares immutable config snapshots."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        schema.ensure_schema(self._conn)
        # Lazily populated in-memory analysis cache.
        self._meta: dict[int, tuple[int | None, int, int | None]] | None = None
        self._deltas: dict[int, tuple[tuple[str, str, int, str | None], ...]] | None = None
        self._root_items: dict[int, dict[ItemId, str]] = {}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_item(item: ItemId, value: str) -> None:
        ns, key = item
        if not isinstance(ns, str) or not isinstance(key, str):
            raise TypeError("item identifiers must be (str, str) tuples")
        if not isinstance(value, str):
            raise TypeError("config values must be strings")

    def create_snapshot(self, items: Mapping[ItemId, str]) -> int:
        """Create an initial snapshot from a complete config mapping."""
        entries = [((ns, key), value) for (ns, key), value in items.items()]
        for item, value in entries:
            self._validate_item(item, value)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO snapshots (parent_id, depth, item_count) VALUES (NULL, 0, ?)",
                (len(entries),),
            )
            snapshot_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO root_items (snapshot_id, ns, key, value) VALUES (?, ?, ?, ?)",
                [(snapshot_id, ns, key, value) for (ns, key), value in entries],
            )
        if self._meta is not None:
            self._meta[snapshot_id] = (None, 0, len(entries))
            self._deltas[snapshot_id] = ()
        return snapshot_id

    def derive_snapshot(
        self,
        parent_id: int,
        writes: Mapping[ItemId, str] | None = None,
        deletes: Iterable[ItemId] = (),
    ) -> int:
        """Derive a new snapshot from an existing one via writes/deletes."""
        writes = dict(writes or {})
        deletes = list(deletes)
        for item, value in writes.items():
            self._validate_item(item, value)
        delete_set = set(deletes)
        if len(delete_set) != len(deletes):
            raise ValueError("duplicate item in deletes")
        overlap = delete_set & set(writes)
        if overlap:
            raise ValueError(f"item appears in both writes and deletes: {sorted(overlap)!r}")
        parent = self._get_meta(parent_id)
        rows = [
            (ns, key, schema.OP_WRITE, value) for (ns, key), value in writes.items()
        ] + [(ns, key, schema.OP_DELETE, None) for ns, key in delete_set]
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO snapshots (parent_id, depth, item_count) VALUES (?, ?, NULL)",
                (parent_id, parent[1] + 1),
            )
            snapshot_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO deltas (snapshot_id, ns, key, op, value) VALUES (?, ?, ?, ?, ?)",
                [(snapshot_id, ns, key, op, value) for ns, key, op, value in rows],
            )
        if self._meta is not None:
            self._meta[snapshot_id] = (parent_id, parent[1] + 1, None)
            self._deltas[snapshot_id] = tuple(rows)
        return snapshot_id

    def compare(self, a_id: int, b_id: int) -> Comparison:
        """Compare snapshot A (baseline) against snapshot B."""
        from .compare import compare_snapshots

        return compare_snapshots(self, a_id, b_id)

    # ------------------------------------------------------------------
    # internal data access used by the comparison engine
    # ------------------------------------------------------------------
    def _get_meta(self, snapshot_id: int) -> tuple[int | None, int, int | None]:
        self._ensure_loaded()
        try:
            return self._meta[snapshot_id]
        except KeyError:
            raise KeyError(f"unknown snapshot id: {snapshot_id}") from None

    def _ensure_loaded(self) -> None:
        if self._meta is not None:
            return
        self._meta = {}
        self._deltas = {}
        for row in self._conn.execute(
            "SELECT id, parent_id, depth, item_count FROM snapshots"
        ):
            self._meta[row[0]] = (row[1], row[2], row[3])
        grouped: dict[int, list] = {snapshot_id: [] for snapshot_id in self._meta}
        for ns, key, op, value, snapshot_id in self._conn.execute(
            "SELECT ns, key, op, value, snapshot_id FROM deltas"
        ):
            grouped[snapshot_id].append((ns, key, op, value))
        self._deltas = {
            snapshot_id: tuple(rows) for snapshot_id, rows in grouped.items()
        }

    def _root_content(self, root_id: int) -> dict[ItemId, str]:
        cached = self._root_items.get(root_id)
        if cached is None:
            cached = {
                (ns, key): value
                for ns, key, value in self._conn.execute(
                    "SELECT ns, key, value FROM root_items WHERE snapshot_id = ?",
                    (root_id,),
                )
            }
            self._root_items[root_id] = cached
        return cached
