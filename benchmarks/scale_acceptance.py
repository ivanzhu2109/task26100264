"""Scale acceptance benchmark for the config snapshot library.

Generates the acceptance-scale dataset (100k initial items, a 10,000-layer
derivation chain plus multiple branches, ~10k derived snapshots), then
verifies and reports:

* persisted database size (must be <= 512 MiB)
* peak process memory during creation and comparison (must be <= 256 MiB)
* wall time from reopening the database to finishing 100 pair reports
  (must be <= 5 s), where each pair's final diff is at most 20 items

Run with:  python3 benchmarks/scale_acceptance.py [db_path]
The script is self-contained and can be re-run; it rebuilds the database
from scratch every time unless --reuse is given and the db exists.
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config_snapshots import SnapshotStore  # noqa: E402

INITIAL_ITEMS = 100_000
CHAIN_LENGTH = 10_000          # layers of the main derivation chain
BRANCH_POINTS = list(range(200, CHAIN_LENGTH + 1, 200))  # 50 branches
HOT_POOL = [("ns00", f"key{i:06d}") for i in range(10)]
PAIR_DIFF_LIMIT = 20
COMPARE_PAIRS = 100

MIB = 1024 * 1024
SIZE_LIMIT = 512 * MIB
MEMORY_LIMIT = 256 * MIB
COMPARE_TIME_LIMIT = 5.0


def root_value(item):
    i = int(item[1][3:])
    return f"val{i:06d}"


def build_dataset(db_path: str) -> dict:
    """Create the full acceptance-scale dataset; return build statistics."""
    items = {
        (f"ns{i % 100:02d}", f"key{i:06d}"): f"val{i:06d}"
        for i in range(INITIAL_ITEMS)
    }
    stats = {}
    tracemalloc.start()
    t0 = time.perf_counter()
    with SnapshotStore(db_path) as store:
        root = store.create_snapshot(items)
        node = root
        chain = {0: root}
        for step in range(1, CHAIN_LENGTH + 1):
            if step % 2 == 1:
                writes = {item: f"s{step:05d}" for item in HOT_POOL}
            else:
                writes = {item: root_value(item) for item in HOT_POOL}
            node = store.derive_snapshot(node, writes=writes)
            chain[step] = node
        branch_tips = {}
        for depth in BRANCH_POINTS:
            base = chain[depth]
            marker = f"b{depth:05d}"
            first = store.derive_snapshot(
                base, writes={item: marker for item in HOT_POOL})
            tip = store.derive_snapshot(
                first,
                writes={item: root_value(item) for item in HOT_POOL[:5]})
            branch_tips[depth] = tip
    creation_seconds = time.perf_counter() - t0
    _, creation_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stats.update(
        root=root,
        chain=chain,
        branch_tips=branch_tips,
        creation_seconds=creation_seconds,
        creation_peak_bytes=creation_peak,
        snapshot_count=1 + CHAIN_LENGTH + 2 * len(BRANCH_POINTS),
    )
    return stats


def make_pairs(stats) -> list[tuple[int, int]]:
    """100 pairs over 200 distinct snapshots, each final diff <= 20 items."""
    chain = stats["chain"]
    branch_tips = stats["branch_tips"]
    pairs = []
    # 50 pairs spanning (nearly) the whole chain; even depths share the
    # root's content, so the final diff is 0 despite 10k layers of history.
    for k in range(1, 51):
        pairs.append((chain[2 * k], chain[CHAIN_LENGTH - 2 * k]))
    # 50 pairs of (chain node, tip of the branch off that node): diff is 5.
    for depth in BRANCH_POINTS:
        pairs.append((chain[depth], branch_tips[depth]))
    assert len(pairs) == COMPARE_PAIRS
    flat = [sid for pair in pairs for sid in pair]
    assert len(set(flat)) == 200, "pairs must use 200 distinct snapshots"
    return pairs


def run_comparisons(db_path: str, pairs) -> dict:
    """Reopen the database and produce all 100 reports; verify constraints."""
    tracemalloc.start()
    t0 = time.perf_counter()
    with SnapshotStore(db_path) as store:
        reports = [store.compare(a, b) for a, b in pairs]
    compare_seconds = time.perf_counter() - t0
    _, compare_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    for report in reports:
        diff = len(report.added) + len(report.removed) + len(report.changed)
        assert diff <= PAIR_DIFF_LIMIT, f"pair diff {diff} exceeds limit"
    # The first 50 pairs span the whole chain with identical final content.
    for report in reports[:50]:
        assert report.parity == 100.0 and not report.warning, \
            "identical content across the chain must show no drift"
    return {
        "compare_seconds": compare_seconds,
        "compare_peak_bytes": compare_peak,
        "reports": reports,
    }


def db_size(db_path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return total


def peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS reports bytes.
    return rss if sys.platform == "darwin" else rss * 1024


def recover_stats(db_path: str) -> dict:
    """Rebuild the pair layout from an existing database (for --reuse)."""
    stats = {"chain": {}, "branch_tips": {}}
    with SnapshotStore(db_path) as store:
        store._ensure_loaded()
        node = max(
            sid for sid, (_p, depth, _c) in store._meta.items()
            if depth == CHAIN_LENGTH
        )
        chain = {}
        while node is not None:
            parent, depth, _count = store._meta[node]
            chain[depth] = node
            node = parent
        stats["chain"] = chain
        children = {}
        for sid, (parent, _depth, _c) in store._meta.items():
            if parent is not None:
                children.setdefault(parent, []).append(sid)
        for depth in BRANCH_POINTS:
            base = chain[depth]
            first = min(sid for sid in children.get(base, [])
                        if sid != chain.get(depth + 1))
            stats["branch_tips"][depth] = children[first][0]
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_dir = os.path.join(os.path.dirname(__file__), "scale_data")
    parser.add_argument("db_path", nargs="?",
                        default=os.path.join(default_dir, "scale_acceptance.db"))
    parser.add_argument("--reuse", action="store_true",
                        help="reuse an existing database instead of rebuilding")
    args = parser.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.db_path)), exist_ok=True)

    if not (args.reuse and os.path.exists(args.db_path)):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(args.db_path + suffix)
            except OSError:
                pass
        stats = build_dataset(args.db_path)
    else:
        stats = recover_stats(args.db_path)

    ok = True

    def check(label, value, limit, fmt="{:,.0f}"):
        nonlocal ok
        passed = value <= limit
        ok = ok and passed
        print(f"  {label:<34} {fmt.format(value):>16}  "
              f"(limit {fmt.format(limit)})  {'OK' if passed else 'FAIL'}")

    print("== scale acceptance ==")
    if "snapshot_count" in stats:
        print(f"snapshots persisted : {stats['snapshot_count']:,} "
              f"(1 initial + {CHAIN_LENGTH:,} chain + "
              f"{2 * len(BRANCH_POINTS)} branch)")
        print(f"creation wall time  : {stats['creation_seconds']:.2f} s")
        check("creation traced peak (bytes)",
              stats["creation_peak_bytes"], MEMORY_LIMIT)

    size = db_size(args.db_path)
    check("persisted size (bytes)", size, SIZE_LIMIT)

    pairs = make_pairs(stats)
    result = run_comparisons(args.db_path, pairs)
    check("compare traced peak (bytes)",
          result["compare_peak_bytes"], MEMORY_LIMIT)
    check("reopen+100 reports (seconds)",
          result["compare_seconds"], COMPARE_TIME_LIMIT, "{:.3f}")
    check("process peak RSS (bytes)", peak_rss_bytes(), MEMORY_LIMIT)

    diffs = [len(r.added) + len(r.removed) + len(r.changed)
             for r in result["reports"]]
    print(f"pair diffs          : min={min(diffs)} max={max(diffs)} "
          f"(limit {PAIR_DIFF_LIMIT})")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
