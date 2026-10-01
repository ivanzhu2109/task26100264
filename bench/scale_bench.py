"""规模验收：10 万初始项 + 1 万层派生链 + 分支。

测量：持久化体积（<=512 MiB）、峰值内存（<=256 MiB）、
重新打开库后 100 对比较总耗时（<=5 s）。可重复运行：
    python3 bench/scale_bench.py [db_path]

构造说明：每次派生写 5 个热键（共 10 个，值随批次变化）+ 1 个冷键，
下一批撤销该冷键。任意两个快照最终差异 <= 12 项，但历史上被修改
过的项数不受此限制（1 万余个不同冷键 + 热键反复改写）。
"""
import os
import resource
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from configsnap import ChangeSet, SnapshotStore

INITIAL_ITEMS = 100_000
CHAIN_LEN = 10_000
BRANCHES = 3
BRANCH_LEN = 100
PAIRS = 100
HOT_KEYS = [("ns.hot", f"h{i}") for i in range(10)]

RSS_UNIT = 1 if sys.platform == "darwin" else 1024  # macOS 字节，Linux 千字节


def peak_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * RSS_UNIT / 2**20


def batch(seq, tag):
    writes = {HOT_KEYS[(seq + j) % 10]: f"{tag}{seq}" for j in range(5)}
    writes[("ns.cold", f"c{seq}")] = f"{tag}{seq}"          # 新冷键
    deletes = frozenset({("ns.cold", f"c{seq - 1}")}) if seq > 0 else frozenset()
    return ChangeSet(writes=writes, deletes=deletes)


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "bench/scale.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    t0 = time.perf_counter()
    with SnapshotStore(db_path) as store:
        base = store.create_initial(
            {("ns.init", f"k{i:06d}"): f"v{i:06d}" for i in range(INITIAL_ITEMS)})
        chain = [base]
        parent = base
        for i in range(1, CHAIN_LEN + 1):
            parent = store.derive(parent, batch(i, "w"))
            chain.append(parent)
        branch_nodes = []
        for b in range(BRANCHES):
            parent = chain[(b + 1) * CHAIN_LEN // (BRANCHES + 1)]
            for j in range(BRANCH_LEN):
                parent = store.derive(parent, batch(CHAIN_LEN + b * BRANCH_LEN + j + 1, f"b{b}w"))
                if j == BRANCH_LEN // 2:
                    branch_nodes.append(parent)
            branch_nodes.append(parent)
    create_secs = time.perf_counter() - t0

    size_mib = os.path.getsize(db_path) / 2**20
    create_peak_mib = peak_mib()

    # 200 个互不重复快照组成 100 对：链上均匀取样 + 分支梢，首尾配对
    step = CHAIN_LEN // (PAIRS * 2 - BRANCHES * 2)
    pool = [chain[min(i * step, CHAIN_LEN)] for i in range(PAIRS * 2 - BRANCHES * 2)]
    pool += branch_nodes
    assert len(set(pool)) == PAIRS * 2
    pairs = [(pool[k], pool[-1 - k]) for k in range(PAIRS)]

    t1 = time.perf_counter()
    with SnapshotStore(db_path) as store:
        reports = [store.compare(a, b) for a, b in pairs]
    compare_secs = time.perf_counter() - t1
    compare_peak_mib = peak_mib()

    diffs = [len(r.added) + len(r.removed) + len(r.changed) for r in reports]
    assert max(diffs) <= 20, f"最终差异超限: {max(diffs)}"

    print(f"快照总数:            {1 + CHAIN_LEN + BRANCHES * BRANCH_LEN}")
    print(f"创建耗时:            {create_secs:.2f} s")
    print(f"持久化体积:          {size_mib:.1f} MiB (限制 512 MiB)")
    print(f"进程峰值内存:        {compare_peak_mib:.1f} MiB (限制 256 MiB)")
    print(f"100 对比较总耗时:    {compare_secs:.2f} s (限制 5 s)")
    print(f"每对最终差异:        最大 {max(diffs)} 项")
    ok = size_mib <= 512 and compare_peak_mib <= 256 and compare_secs <= 5
    print("验收结果:", "通过" if ok else "未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
