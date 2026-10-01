"""SQLite 表结构与底层初始化。

存储设计：每个快照只保存相对父快照的增量（delta），并沿派生链每隔
CHECKPOINT_INTERVAL 层保存一次全量内容（检查点），使任意快照的物化
成本为「最近检查点 + 少量增量」，与链长无关。

- snapshots：快照元数据（父指针、深度、最终配置项总数）。
- entries：该快照本批写入/删除的条目；value 为 NULL 表示删除。
- checkpoints / snap_items：检查点快照的全量内容。
"""
from __future__ import annotations

import sqlite3

CHECKPOINT_INTERVAL = 500

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES snapshots(id),
    depth      INTEGER NOT NULL,
    item_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,               -- NULL 表示删除
    PRIMARY KEY (snapshot_id, namespace, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS checkpoints (
    snapshot_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS snap_items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, namespace, key)
) WITHOUT ROWID;
"""

PRAGMAS = (
    "PRAGMA synchronous = OFF",   # 不要求抗进程中途崩溃，换取批量写入速度
    "PRAGMA journal_mode = MEMORY",
    "PRAGMA temp_store = MEMORY",
)


def init_schema(conn: sqlite3.Connection) -> None:
    for pragma in PRAGMAS:
        conn.execute(pragma)
    conn.executescript(SCHEMA_SQL)
