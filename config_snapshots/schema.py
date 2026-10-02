"""SQLite schema definitions for the snapshot store."""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER REFERENCES snapshots (id),
        depth INTEGER NOT NULL,
        item_count INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS root_items (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots (id),
        ns TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, ns, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deltas (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots (id),
        ns TEXT NOT NULL,
        key TEXT NOT NULL,
        op INTEGER NOT NULL,
        value TEXT,
        PRIMARY KEY (snapshot_id, ns, key)
    )
    """,
)

OP_DELETE = 0
OP_WRITE = 1


def ensure_schema(conn) -> None:
    """Create tables if they do not exist yet."""
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
