"""数据类型定义：配置项标识、变更集、快照信息、比较报告。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

ConfigItem = Tuple[str, str]  # (namespace, key)


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: int
    parent_id: Optional[int]
    item_count: int


@dataclass(frozen=True)
class ChangeSet:
    """派生快照的一批修改：writes 覆盖/新增，deletes 删除。同键不得同时出现。"""
    writes: Dict[ConfigItem, str] = field(default_factory=dict)
    deletes: FrozenSet[ConfigItem] = frozenset()


@dataclass(frozen=True)
class DiffReport:
    """以 A 为基准看 B 的比较结果。"""
    added: FrozenSet[ConfigItem]    # 仅 B 存在
    removed: FrozenSet[ConfigItem]  # 仅 A 存在
    changed: FrozenSet[ConfigItem]  # 两边都有但值不同
    parity_ratio: float             # 0.0 ~ 100.0
    warning: bool                   # parity < 95%（精确判断，不受舍入影响）
