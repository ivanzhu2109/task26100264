# configsnap：配置快照与环境对等分析库

Python + SQLite 实现，由上层直接调用。配置内容与其版本关系全部持久化到
SQLite，支持初始快照、任意快照派生（分支）、任意两快照对等比较。

## 用法

```python
from configsnap import SnapshotStore, ChangeSet

with SnapshotStore("snap.db") as store:
    base = store.create_initial({("app", "host"): "a", ("app", "port"): "80"})
    staging = store.derive(base, ChangeSet(
        writes={("app", "port"): "8080"},
        deletes=frozenset({("app", "host")}),
    ))
    report = store.compare(base, staging)
    report.added / report.removed / report.changed
    report.parity_ratio
    report.warning
```

- 快照创建后不可修改，可从任意旧快照反复派生分支；关闭重开后全部
  历史快照仍可派生与比较。
- 比较结果只由最终内容决定：删除后同值重建、改值后改回、不同分支
  殊途同归，都不会产生虚假差异。
- 预警用整数运算判定（same*20 < union*19），不受显示舍入影响；
  两边皆空时对等率为 100%。

## 存储设计

每个快照只持久化相对父快照的增量（entries，删除记为 NULL），并沿
派生链每 500 层落一次全量检查点（snap_items），物化成本与链长无关。
比较不物化全量内容：两快照的差异只可能出现在各自到最近公共祖先的
路径上被触碰的键；会话首次比较时将增量小表装入内存，每对比较仅在
内存中走父链并解析被触碰键的最终值，未触碰键由 item_count 直接计数。

## 测试与规模验收

```bash
python3 -m pytest tests/ -q        # 14 个功能与等价性测试
python3 bench/scale_bench.py       # 规模验收（可重复运行）
```

规模验收（10 万初始项 + 1 万层派生链 + 3 条分支，共 10301 个快照，
100 对比较、每对最终差异不超过 20 项）实测（本机，Apple Silicon）：

| 指标 | 限制 | 实测 |
| --- | --- | --- |
| 持久化体积 | <= 512 MiB | 73.2 MiB |
| 进程峰值内存 | <= 256 MiB | 165.5 MiB |
| 重开库后 100 对比较总耗时 | <= 5 s | 1.32 s |
| 创建全部快照耗时 | — | 3.07 s |
