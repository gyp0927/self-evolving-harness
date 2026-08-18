"""架构师操作应用：原子性、拒绝路径、修复建议"""
from copy import deepcopy

from core.architect import _apply_ops, _suggest_fix
from core.graph_builder import DEFAULT_SPEC


def base():
    return deepcopy(DEFAULT_SPEC)


def test_full_proposal_applied():
    """tool_dispatch 接入（含出边）整包应用成功"""
    ops = [
        {"op": "add_node", "name": "tool_dispatch"},
        {"op": "add_edge", "source": "r_load", "target": "tool_dispatch",
         "policy": "route_execution",
         "targets": {"execute": "execute", "tool_dispatch": "tool_dispatch"}},
        {"op": "remove_edge", "source": "r_load", "target": "execute"},
        {"op": "add_edge", "source": "tool_dispatch", "target": "r_exec"},
    ]
    s = _apply_ops(base(), ops)
    assert s is not None and "tool_dispatch" in s["nodes"]


def test_unknown_op_rejected():
    assert _apply_ops(base(), [{"op": "delete_everything"}]) is None


def test_remove_protected_node_rejected():
    assert _apply_ops(base(), [{"op": "remove_node", "name": "execute"}]) is None


def test_remove_orphans_atomic_rejected():
    """删 r_eff（条件源）→ 反射链孤岛 → 整体拒绝（原子性）"""
    assert _apply_ops(base(), [{"op": "remove_node", "name": "r_eff"}]) is None


def test_set_policy_alone_rejected():
    """set_policy 单独改路由 → 反射链失联 → 拒绝（护栏正确）"""
    ops = [{"op": "set_policy", "source": "r_eff",
            "policy": "should_deep_reflect",
            "targets": {"reflect": "save_memory", "save_memory": "save_memory"}}]
    assert _apply_ops(base(), ops) is None


def test_reflect_branch_shutdown_valid():
    """反思分支整体下线（4 删 + 1 改）→ 合法"""
    ops = [
        {"op": "remove_node", "name": "reflect"},
        {"op": "remove_node", "name": "r_reflect"},
        {"op": "remove_node", "name": "create_skill"},
        {"op": "remove_node", "name": "r_skill"},
        {"op": "set_policy", "source": "r_eff",
         "policy": "should_deep_reflect",
         "targets": {"reflect": "save_memory", "save_memory": "save_memory"}},
    ]
    s = _apply_ops(base(), ops)
    assert s is not None
    assert s["nodes"] == ["load_skills", "r_load", "execute", "r_exec",
                          "r_eff", "save_memory"]


def test_noop_and_empty_rejected():
    assert _apply_ops(base(), []) is None
    assert _apply_ops(base(), [{"op": "noop"}]) is None


def test_duplicate_node_rejected():
    assert _apply_ops(base(), [{"op": "add_node", "name": "execute"}]) is None


def test_set_policy_missing_edge_rejected():
    ops = [{"op": "set_policy", "source": "不存在的节点",
            "policy": "should_deep_reflect",
            "targets": {"reflect": "reflect", "save_memory": "save_memory"}}]
    assert _apply_ops(base(), ops) is None


def test_suggest_fix_generates_guidance():
    fixes = _suggest_fix(["从 entry 不可达: ['r_skill']", "无法到达 END: ['reflect']"])
    assert "移除这些节点" in fixes or "补" in fixes
    assert _suggest_fix(["完全无关的错误"]) == ""
