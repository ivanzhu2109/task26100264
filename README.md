# config-snapshots

配置快照与环境对等分析库：Python + SQLite，由上层直接调用。
配置内容与其版本（派生）关系全部持久化在 SQLite 中。

## 功能

- 建立初始快照（完整配置）、从任意已有快照派生新快照（批量写入/删除）、
  比较任意两个快照。
- 配置项以 `(命名空间, 键)` 标识，值为字符串，按内容精确比较。
- 快照创建后不可修改，可从任意旧快照反复建立分支；关闭并重开库后，
  全部历史快照仍可派生与比较。
- 比较以 A 为基准看 B：`added`（仅 B 有）、`removed`（仅 A 有）、
  `changed`（两边都有但值不同），均为完整的配置项标识集合。
- 对等率 = 两边都存在且值相同的项数 / 两边标识的并集大小 × 100%；
  两边皆空为 100%。低于 95% 预警（恰好 95% 不预警），
  预警判断使用整数精确比较（`20*same < 19*union`），不受显示舍入影响。
- 比较结果只由最终内容决定：删除后同值重建、改值后改回、
  不同分支收敛到相同内容，都不会产生虚假差异。

## API

```python
from config_snapshots import SnapshotStore

with SnapshotStore("snapshots.db") as store:
    root = store.create_snapshot({("ns", "key"): "value", ...})
    child = store.derive_snapshot(
        root,
        writes={("ns", "key"): "new-value"},   # 替换或新增
        deletes=[("ns", "old-key")],           # 使配置不存在
    )
    report = store.compare(root, child)
    report.added / report.removed / report.changed   # frozenset[(ns, key)]
    report.same_count / report.union_count
    report.parity    # 0..100 的浮点展示值
    report.warning   # 对等率严格低于 95% 时为 True
```

约束：同一批派生操作中每个配置项最多出现一次（写入与删除不得重叠，
否则抛 `ValueError`）；值必须是字符串；删除不存在的项是无操作。

## 存储设计

- `snapshots(id, parent_id, depth, item_count)`：派生树，根快照记录项数。
- `root_items(snapshot_id, ns, key, value)`：初始快照的完整内容。
- `deltas(snapshot_id, ns, key, op, value)`：每个派生快照的增量
  （写入/删除），历史快照天然不可变，持久化体积与修改量成正比。
- 比较时在派生树上找到两快照的最近公共祖先（LCA）：
  LCA 之下两条路径上被触及的项才是差异候选，其余项两边必然一致，
  因此比较耗时与“两路径上的修改量”相关，与快照总项数无关；
  快照图与增量在首次比较时一次性载入内存缓存，批量比较无需重复读库。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：长链与分支上的删除后重建、改值后改回（最终内容相同必须无差异、
对等率 100%）；跨命名空间同名键的混合增删改（差异分类、项集合、
对等率与最终内容完全一致）；95% 预警临界值（含 94.95% 这类
显示舍入陷阱）；空快照；不可变性与重开库后的派生/比较。

## 规模验收

```bash
python3 benchmarks/scale_acceptance.py           # 重建数据并验收
python3 benchmarks/scale_acceptance.py --reuse   # 复用已有库重复验收
```

数据规模：初始快照 10 万项；10,000 层主派生链 + 50 个分支共 10,100 个
派生快照；每个派生快照涉及 10 项；命名空间/键/值均不超过 32 字节。
100 对比较由 200 个互不重复的快照组成（50 对横跨整条派生链、
50 对来自不同分支），每对最终差异不超过 20 项。

本机实测（Python 3.14.3 / SQLite 3.50.4，macOS）：

| 指标 | 实测 | 上限 |
| --- | --- | --- |
| 创建 10,101 个快照耗时 | 1.11 s | — |
| 持久化体积 | 12.2 MiB | 512 MiB |
| 创建期峰值内存（tracemalloc） | 21.4 MiB | 256 MiB |
| 比较期峰值内存（tracemalloc） | 49.7 MiB | 256 MiB |
| 进程峰值 RSS | 157 MiB | 256 MiB |
| 重开库 + 100 对完整报告 | 2.0–3.1 s | 5 s |
