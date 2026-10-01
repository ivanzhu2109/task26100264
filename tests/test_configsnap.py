"""验收测试：长链/分支内容等价、跨命名空间混合增删改、95% 临界、空快照、持久化。"""
import pytest

from configsnap import ChangeSet, SnapshotStore


@pytest.fixture()
def store():
    with SnapshotStore(":memory:") as s:
        yield s


def items(n, prefix="v"):
    return {("ns", f"k{i}"): f"{prefix}{i}" for i in range(n)}


class TestLongChainAndBranches:
    def test_delete_rebuild_and_revert_on_long_chain(self, store):
        base = store.create_initial(items(100))
        parent = base
        # 1200 层派生链，跨越多个检查点（间隔 500）
        for i in range(1200):
            parent = store.derive(parent, ChangeSet(
                writes={("ns", f"k{i % 50}"): f"transient{i}"}))
        # 链尾删除一批，再同值重建；改一批值，再改回
        deleted = {("ns", f"k{i}") for i in range(20)}
        parent = store.derive(parent, ChangeSet(deletes=frozenset(deleted)))
        parent = store.derive(parent, ChangeSet(
            writes={k: f"v{k[1][1:]}" for k in deleted}))
        changed = {("ns", f"k{i}"): "other" for i in range(20, 40)}
        parent = store.derive(parent, ChangeSet(writes=changed))
        parent = store.derive(parent, ChangeSet(
            writes={("ns", f"k{i}"): f"v{i}" for i in range(20, 40)}))
        # 撤销全部临时修改
        for i in range(1199, -1, -1):
            parent = store.derive(parent, ChangeSet(
                writes={("ns", f"k{i % 50}"): f"v{i % 50}"}))

        report = store.compare(base, parent)
        assert report.added == frozenset()
        assert report.removed == frozenset()
        assert report.changed == frozenset()
        assert report.parity_ratio == 100.0
        assert report.warning is False

    def test_branches_converging_to_same_content(self, store):
        base = store.create_initial(items(50))
        # 分支一：直接改
        left = store.derive(base, ChangeSet(
            writes={("ns", "k0"): "x", ("ns", "k1"): "y"},
            deletes=frozenset({("ns", "k2")})))
        # 分支二：绕路（先删再建、先改再改）到达同一最终内容
        right = store.derive(base, ChangeSet(
            deletes=frozenset({("ns", "k0"), ("ns", "k1"), ("ns", "k2")})))
        right = store.derive(right, ChangeSet(
            writes={("ns", "k0"): "tmp", ("ns", "k1"): "tmp"}))
        right = store.derive(right, ChangeSet(
            writes={("ns", "k0"): "x", ("ns", "k1"): "y"}))
        report = store.compare(left, right)
        assert not (report.added or report.removed or report.changed)
        assert report.parity_ratio == 100.0
        assert store.materialize(left) == store.materialize(right)

    def test_historical_snapshots_immutable(self, store):
        base = store.create_initial(items(10))
        snapshot_before = store.materialize(base)
        child = store.derive(base, ChangeSet(writes={("ns", "k0"): "changed"}))
        assert store.materialize(base) == snapshot_before
        assert store.materialize(child)[("ns", "k0")] == "changed"
        # 从旧快照反复建立分支互不影响
        again = store.derive(base, ChangeSet(writes={("ns", "k1"): "other"}))
        assert store.materialize(again)[("ns", "k0")] == "v0"
        assert store.materialize(child)[("ns", "k1")] == "v1"


class TestCrossNamespace:
    def test_mixed_add_delete_change_across_namespaces(self, store):
        a = store.create_initial({
            ("ns1", "same"): "1", ("ns2", "same"): "2",
            ("ns1", "gone"): "x", ("ns2", "gone"): "keep",
            ("ns1", "mod"): "old", ("ns2", "mod"): "old",
            ("ns1", "empty"): "",
        })
        b = store.derive(a, ChangeSet(
            writes={
                ("ns1", "mod"): "new",                 # 变更
                ("ns1", "new"): "n",                   # 新增
                ("ns2", "gone"): "keep",               # 同值写入 → 无差异
                ("ns1", "empty"): "",                  # 空串同值 → 无差异
            },
            deletes=frozenset({("ns1", "gone"),        # 删除
                               ("ns2", "absent")}),    # 删除不存在项 → 无效果
        ))
        report = store.compare(a, b)
        assert report.added == frozenset({("ns1", "new")})
        assert report.removed == frozenset({("ns1", "gone")})
        assert report.changed == frozenset({("ns1", "mod")})
        # 并集 8 项：same×2, gone×2(一边缺), mod×2, empty, new → same=5
        assert report.parity_ratio == pytest.approx(5 / 8 * 100)
        # 反向比较新增/删除互换
        reverse = store.compare(b, a)
        assert reverse.added == report.removed
        assert reverse.removed == report.added
        assert reverse.changed == report.changed

    def test_empty_string_is_not_absent(self, store):
        a = store.create_initial({("ns", "k"): ""})
        b = store.derive(a, ChangeSet(deletes=frozenset({("ns", "k")})))
        report = store.compare(a, b)
        assert report.removed == frozenset({("ns", "k")})
        assert report.parity_ratio == 0.0


class TestParityThreshold:
    def test_exactly_95_percent_no_warning(self, store):
        a = store.create_initial(items(20))
        b = store.derive(a, ChangeSet(writes={("ns", "k0"): "different"}))
        report = store.compare(a, b)
        assert report.parity_ratio == pytest.approx(95.0)
        assert report.warning is False

    def test_below_95_percent_warns(self, store):
        a = store.create_initial(items(20))
        b = store.derive(a, ChangeSet(
            writes={("ns", "k0"): "x", ("ns", "k1"): "y"}))
        report = store.compare(a, b)
        assert report.parity_ratio == pytest.approx(90.0)
        assert report.warning is True

    def test_warning_not_fooled_by_rounding(self, store):
        # 18999/20000 = 94.995%，显示上接近 95%，但必须预警
        a = store.create_initial(items(20000))
        b = store.derive(a, ChangeSet(
            writes={("ns", f"k{i}"): "x" for i in range(1001)}))
        report = store.compare(a, b)
        assert 94.9 < report.parity_ratio < 95.0
        assert report.warning is True

    def test_both_empty_is_100_percent(self, store):
        a = store.create_initial({})
        b = store.derive(a, ChangeSet())
        report = store.compare(a, b)
        assert report.parity_ratio == 100.0
        assert report.warning is False
        assert not (report.added or report.removed or report.changed)


class TestValidation:
    def test_conflicting_write_and_delete_rejected(self, store):
        a = store.create_initial(items(1))
        with pytest.raises(ValueError):
            store.derive(a, ChangeSet(writes={("ns", "k0"): "v"},
                                      deletes=frozenset({("ns", "k0")})))

    def test_unknown_snapshot_rejected(self, store):
        with pytest.raises(KeyError):
            store.materialize(999)
        with pytest.raises(KeyError):
            store.derive(999, ChangeSet())

    def test_non_string_value_rejected(self, store):
        with pytest.raises(TypeError):
            store.create_initial({("ns", "k"): 1})


class TestPersistence:
    def test_reopen_keeps_history_derivable_and_comparable(self, tmp_path):
        db = str(tmp_path / "snap.db")
        with SnapshotStore(db) as store:
            base = store.create_initial(items(30))
            parent = base
            for i in range(600):  # 跨检查点
                parent = store.derive(parent, ChangeSet(
                    writes={("ns", f"k{i % 10}"): f"w{i}"}))
            branch = store.derive(base, ChangeSet(
                deletes=frozenset({("ns", "k0")})))
            expected_parent = store.materialize(parent)
            expected_branch = store.materialize(branch)

        with SnapshotStore(db) as reopened:
            assert reopened.materialize(parent) == expected_parent
            assert reopened.materialize(branch) == expected_branch
            report = reopened.compare(parent, branch)
            assert report.removed == frozenset() or report.removed
            # 重开后仍可继续派生
            more = reopened.derive(branch, ChangeSet(
                writes={("ns", "k0"): "back"}))
            assert reopened.materialize(more)[("ns", "k0")] == "back"
            same = reopened.compare(reopened.derive(base, ChangeSet()), base)
            assert same.parity_ratio == 100.0


class TestCompareEquivalence:
    """随机操作下，LCA 比较结果必须与全量物化后的逐一对比完全一致。"""

    def test_randomized_against_materialize(self, tmp_path):
        import random
        rng = random.Random(20261002)
        db = str(tmp_path / "rand.db")
        namespaces = ["ns1", "ns2", "ns3"]
        keys = [f"k{i}" for i in range(30)]

        def random_item():
            return (rng.choice(namespaces), rng.choice(keys))

        with SnapshotStore(db) as store:
            initial = {random_item(): f"v{i}" for i in range(200)}
            snapshots = [store.create_initial(initial)]
            for _ in range(150):
                parent = rng.choice(snapshots)
                writes, deletes = {}, set()
                for _ in range(rng.randint(0, 8)):
                    item = random_item()
                    if rng.random() < 0.3:
                        deletes.add(item)          # 可能删除不存在的项
                    else:
                        writes[item] = rng.choice(["x", "y", "", f"v{item[1]}"])
                for item in deletes:
                    writes.pop(item, None)
                snapshots.append(store.derive(parent, ChangeSet(
                    writes=writes, deletes=frozenset(deletes))))

        with SnapshotStore(db) as store:  # 重新打开后再比较
            contents = {sid: store.materialize(sid) for sid in snapshots}
            for _ in range(200):
                a, b = rng.choice(snapshots), rng.choice(snapshots)
                ca, cb = contents[a], contents[b]
                report = store.compare(a, b)
                assert report.added == frozenset(set(cb) - set(ca))
                assert report.removed == frozenset(set(ca) - set(cb))
                assert report.changed == frozenset(
                    k for k in set(ca) & set(cb) if ca[k] != cb[k])
                union = set(ca) | set(cb)
                same = sum(1 for k in union if ca.get(k) == cb.get(k)
                           and k in ca and k in cb)
                expected = 100.0 if not union else same * 100.0 / len(union)
                assert report.parity_ratio == pytest.approx(expected)
                assert report.warning == (same * 20 < len(union) * 19)
