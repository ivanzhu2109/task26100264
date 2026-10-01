"""SnapshotStore：对外 API。

- create_initial：建立初始快照（完整配置）。
- derive：从任意已有快照派生（批量写入/删除）。
- materialize：返回快照最终内容字典。
- compare：以 A 为基准比较 B，返回 DiffReport。

物化通过一条递归 CTE 完成：从目标快照沿父链回溯到最近的检查点
（或根），将检查点全量内容与链上增量按「越靠近叶子越新」合并，
删除（value 为 NULL）的项被过滤。

比较不物化全量内容：两快照的差异只可能出现在各自到最近公共祖先
（LCA）的路径上被触碰的键。会话首次比较时把 snapshots/entries/
checkpoints 三张增量小表装入内存，之后每对比较只在内存中走父链、
解析被触碰键的最终值；未触碰键两侧一致，直接由 item_count 计数。
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Tuple

from .models import ChangeSet, ConfigItem, DiffReport, SnapshotInfo
from .schema import CHECKPOINT_INTERVAL, init_schema

# 合并时取离叶子最近（存储 depth 最大）的记录；检查点内容最旧，取极小深度。
_CP_DEPTH = -(1 << 40)

_CHAIN_CTE = """
WITH RECURSIVE chain(id, parent_id, depth, is_cp) AS (
    SELECT s.id, s.parent_id, s.depth,
           EXISTS(SELECT 1 FROM checkpoints cp WHERE cp.snapshot_id = s.id)
    FROM snapshots s WHERE s.id = ?
    UNION ALL
    SELECT p.id, p.parent_id, p.depth,
           EXISTS(SELECT 1 FROM checkpoints cp WHERE cp.snapshot_id = p.id)
    FROM chain c JOIN snapshots p ON p.id = c.parent_id
    WHERE c.is_cp = 0
)
"""

_MERGE_SELECT = f"""
SELECT namespace, key, value FROM (
    SELECT namespace, key, value, depth,
           MAX(depth) OVER (PARTITION BY namespace, key) AS md
    FROM (
        SELECT e.namespace, e.key, e.value, c.depth
        FROM chain c JOIN entries e ON e.snapshot_id = c.id
        UNION ALL
        SELECT i.namespace, i.key, i.value, {_CP_DEPTH}
        FROM chain c JOIN snap_items i ON i.snapshot_id = c.id
        WHERE c.is_cp = 1
    )
)
WHERE depth = md AND value IS NOT NULL
"""

_MATERIALIZE_SQL = _CHAIN_CTE + _MERGE_SELECT

_ABSENT = object()  # 键不存在的哨兵

_IN_CHUNK = 5000  # snap_items 点查的分块大小（受 SQLite 变量数限制）


def _validate_item(item: ConfigItem) -> None:
    if (not isinstance(item, tuple) or len(item) != 2
            or not isinstance(item[0], str) or not isinstance(item[1], str)):
        raise TypeError(f"配置项标识必须是 (namespace, key) 字符串二元组: {item!r}")


class SnapshotStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        init_schema(self._conn)
        # 单条内容缓存：串行调用下，链式派生时命中父快照，避免重复物化。
        self._cache_id: Optional[int] = None
        self._cache_content: Optional[Dict[ConfigItem, str]] = None
        # 比较会话缓存：首次比较时加载，写入后失效。
        self._snaps: Optional[Dict[int, Tuple[Optional[int], int, int]]] = None
        self._entries: Optional[Dict[int, List[Tuple[str, str, Optional[str]]]]] = None
        self._checkpoints: Optional[set] = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部工具 ----

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SnapshotStore 已关闭")
        return self._conn

    def _snapshot_exists(self, snapshot_id: int) -> bool:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        return row is not None

    def _content_of(self, snapshot_id: int) -> Dict[ConfigItem, str]:
        if snapshot_id != self._cache_id:
            content = self._load_content(snapshot_id)
            self._cache_id = snapshot_id
            self._cache_content = content
        return self._cache_content

    def _load_content(self, snapshot_id: int) -> Dict[ConfigItem, str]:
        conn = self._require_conn()
        if not self._snapshot_exists(snapshot_id):
            raise KeyError(f"快照不存在: {snapshot_id}")
        rows = conn.execute(_MATERIALIZE_SQL, (snapshot_id,)).fetchall()
        return {(ns, key): value for ns, key, value in rows}

    def _insert_snapshot(self, parent_id: Optional[int], depth: int,
                         item_count: int) -> int:
        conn = self._require_conn()
        cur = conn.execute(
            "INSERT INTO snapshots(parent_id, depth, item_count) VALUES (?,?,?)",
            (parent_id, depth, item_count),
        )
        return cur.lastrowid

    def _maybe_checkpoint(self, snapshot_id: int, depth: int,
                          content: Dict[ConfigItem, str]) -> None:
        if depth == 0 or depth % CHECKPOINT_INTERVAL != 0:
            return
        conn = self._require_conn()
        conn.execute("INSERT INTO checkpoints(snapshot_id) VALUES (?)",
                     (snapshot_id,))
        conn.executemany(
            "INSERT INTO snap_items(snapshot_id, namespace, key, value)"
            " VALUES (?,?,?,?)",
            ((snapshot_id, ns, key, value)
             for (ns, key), value in content.items()),
        )

    @staticmethod
    def _validate_writes(writes: Dict[ConfigItem, str]) -> None:
        for item, value in writes.items():
            _validate_item(item)
            if not isinstance(value, str):
                raise TypeError(f"配置值必须是字符串: {item!r} -> {value!r}")

    # ---- 对外 API ----

    def create_initial(self, items: Dict[ConfigItem, str]) -> int:
        conn = self._require_conn()
        self._validate_writes(items)
        content = dict(items)
        snapshot_id = self._insert_snapshot(None, 0, len(content))
        conn.executemany(
            "INSERT INTO entries(snapshot_id, namespace, key, value)"
            " VALUES (?,?,?,?)",
            ((snapshot_id, ns, key, value)
             for (ns, key), value in content.items()),
        )
        conn.commit()
        self._cache_id = snapshot_id
        self._cache_content = content
        self._invalidate_session_cache()
        return snapshot_id

    def derive(self, parent_id: int, changes: ChangeSet) -> int:
        conn = self._require_conn()
        writes = dict(changes.writes)
        deletes = frozenset(changes.deletes)
        overlap = set(writes) & deletes
        if overlap:
            raise ValueError(f"同一批内写入与删除冲突: {sorted(overlap)!r}")
        self._validate_writes(writes)
        for item in deletes:
            _validate_item(item)

        content = self._content_of(parent_id)  # KeyError 若父快照不存在
        for item in deletes:
            content.pop(item, None)            # 删除不存在的项无效果
        for item, value in writes.items():
            content[item] = value

        parent_depth = conn.execute(
            "SELECT depth FROM snapshots WHERE id = ?", (parent_id,)
        ).fetchone()[0]
        depth = parent_depth + 1
        snapshot_id = self._insert_snapshot(parent_id, depth, len(content))
        conn.executemany(
            "INSERT INTO entries(snapshot_id, namespace, key, value)"
            " VALUES (?,?,?,?)",
            [(snapshot_id, ns, key, value) for (ns, key), value in writes.items()]
            + [(snapshot_id, ns, key, None) for ns, key in deletes],
        )
        self._maybe_checkpoint(snapshot_id, depth, content)
        conn.commit()
        self._cache_id = snapshot_id  # 缓存就地代表新快照
        self._invalidate_session_cache()
        return snapshot_id

    def materialize(self, snapshot_id: int) -> Dict[ConfigItem, str]:
        return dict(self._content_of(snapshot_id))

    # ---- 比较（LCA + 增量解析） ----

    def _invalidate_session_cache(self) -> None:
        self._snaps = self._entries = self._checkpoints = None

    def _ensure_session_cache(self) -> None:
        if self._snaps is not None:
            return
        conn = self._require_conn()
        self._snaps = {
            sid: (pid, depth, count)
            for sid, pid, depth, count in conn.execute(
                "SELECT id, parent_id, depth, item_count FROM snapshots")
        }
        entries: Dict[int, List[Tuple[str, str, Optional[str]]]] = {}
        for sid, ns, key, value in conn.execute(
                "SELECT snapshot_id, namespace, key, value FROM entries"):
            entries.setdefault(sid, []).append((ns, key, value))
        self._entries = entries
        self._checkpoints = {
            row[0] for row in conn.execute("SELECT snapshot_id FROM checkpoints")
        }

    def _paths_to_lca(self, a_id: int, b_id: int):
        """返回 (pathA, pathB, lca)；路径为叶→LCA 顺序，不含 LCA。"""
        snaps = self._snaps
        path_a, path_b = [], []
        x, y = a_id, b_id
        depth_x = snaps[x][1]
        depth_y = snaps[y][1]
        while depth_x > depth_y:
            path_a.append(x)
            x = snaps[x][0]
            depth_x -= 1
        while depth_y > depth_x:
            path_b.append(y)
            y = snaps[y][0]
            depth_y -= 1
        while x != y:
            path_a.append(x)
            path_b.append(y)
            x = snaps[x][0]
            y = snaps[y][0]
        return path_a, path_b, x

    def _apply_path(self, path: List[int]) -> Dict[ConfigItem, Optional[str]]:
        """沿路径（叶→根）从根向叶应用增量，返回 {键: 值或None(删除)}。"""
        delta: Dict[ConfigItem, Optional[str]] = {}
        for sid in reversed(path):
            for ns, key, value in self._entries.get(sid, ()):
                delta[(ns, key)] = value
        return delta

    def _lca_base_values(self, lca: int, needed: set) -> Dict[ConfigItem, object]:
        """LCA 处各键的最终值：检查点之前的链上增量 + snap_items 点查。"""
        chain = [lca]
        while chain[-1] not in self._checkpoints:
            parent = self._snaps[chain[-1]][0]
            if parent is None:
                break
            chain.append(parent)
        base: Dict[ConfigItem, object] = {}
        for sid in reversed(chain):
            for ns, key, value in self._entries.get(sid, ()):
                base[(ns, key)] = _ABSENT if value is None else value
        checkpoint = chain[-1] if chain[-1] in self._checkpoints else None
        missing = [k for k in needed
                   if k not in base and checkpoint is not None]
        if missing:
            conn = self._require_conn()
            for start in range(0, len(missing), _IN_CHUNK):
                chunk = missing[start:start + _IN_CHUNK]
                placeholders = ",".join("(?,?)" for _ in chunk)
                params = [checkpoint]
                for ns, key in chunk:
                    params += [ns, key]
                rows = conn.execute(
                    "SELECT namespace, key, value FROM snap_items"
                    " WHERE snapshot_id = ?"
                    f" AND (namespace, key) IN ({placeholders})",
                    params,
                )
                for ns, key, value in rows:
                    base[(ns, key)] = value
        return base

    def compare(self, a_id: int, b_id: int) -> DiffReport:
        self._require_conn()
        self._ensure_session_cache()
        snaps = self._snaps
        for sid in (a_id, b_id):
            if sid not in snaps:
                raise KeyError(f"快照不存在: {sid}")

        path_a, path_b, lca = self._paths_to_lca(a_id, b_id)
        delta_a = self._apply_path(path_a)
        delta_b = self._apply_path(path_b)
        touched = set(delta_a) | set(delta_b)

        # 只在单侧被触碰的键，另一侧取 LCA 处的继承值。
        need_lca = {k for k in touched if k not in delta_a or k not in delta_b}
        base = self._lca_base_values(lca, need_lca) if need_lca else {}

        added, removed, changed = set(), set(), set()
        for item in touched:
            val_a = delta_a[item] if item in delta_a else base.get(item, _ABSENT)
            val_b = delta_b[item] if item in delta_b else base.get(item, _ABSENT)
            if val_a is None:
                val_a = _ABSENT
            if val_b is None:
                val_b = _ABSENT
            if val_a is _ABSENT and val_b is _ABSENT:
                continue  # 同侧先写后删、另一侧本就没有 → 无差异
            if val_a is _ABSENT:
                added.add(item)
            elif val_b is _ABSENT:
                removed.add(item)
            elif val_a != val_b:
                changed.add(item)

        count_a = snaps[a_id][2]
        count_b = snaps[b_id][2]
        both = count_a - len(removed)
        same = both - len(changed)
        union = count_a + count_b - both
        if union == 0:
            ratio = 100.0
            warning = False
        else:
            ratio = same * 100.0 / union
            warning = same * 20 < union * 19  # 精确判断 < 95%，不受舍入影响
        return DiffReport(
            added=frozenset(added),
            removed=frozenset(removed),
            changed=frozenset(changed),
            parity_ratio=ratio,
            warning=warning,
        )

    def info(self, snapshot_id: int) -> SnapshotInfo:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT id, parent_id, item_count FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"快照不存在: {snapshot_id}")
        return SnapshotInfo(snapshot_id=row[0], parent_id=row[1],
                            item_count=row[2])
