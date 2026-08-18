"""图验证器：护栏规则全覆盖（结构自进化的安全边界）"""
from copy import deepcopy

from core.graph_builder import (DEFAULT_SPEC, NODES, CONDITIONAL_POLICIES,
                                validate_spec, build_graph)


def base():
    return deepcopy(DEFAULT_SPEC)


def test_default_spec_is_valid():
    ok, errs = validate_spec(base())
    assert ok, errs


def test_self_loop_rejected():
    s = base()
    s["edges"].append({"source": "r_load", "target": "r_load"})
    ok, _ = validate_spec(s)
    assert not ok


def test_duplicate_edge_rejected():
    s = base()
    s["edges"].append({"source": "load_skills", "target": "r_load"})
    ok, _ = validate_spec(s)
    assert not ok


def test_cycle_rejected():
    s = base()
    s["edges"] = [e for e in s["edges"] if not (e["source"] == "r_eff" and e.get("policy"))]
    s["edges"].append({"source": "r_exec", "target": "r_eff"})
    s["edges"].append({"source": "r_eff", "target": "r_exec"})
    ok, errs = validate_spec(s)
    assert not ok
    assert any("环" in e for e in errs)


def test_unreachable_node_rejected():
    s = base()
    s["edges"] = [e for e in s["edges"] if e["source"] != "load_skills"]
    ok, errs = validate_spec(s)
    assert not ok
    assert any("不可达" in e for e in errs)


def test_node_without_exit_rejected():
    s = base()
    s["edges"] = [e for e in s["edges"] if e["source"] != "save_memory"]
    ok, errs = validate_spec(s)
    assert not ok
    assert any("无法到达 END" in e for e in errs)


def test_dangling_conditional_target_rejected():
    s = base()
    s["edges"] = [e for e in s["edges"] if not (e["source"] == "r_eff" and e.get("policy"))]
    s["edges"].append({"source": "r_eff", "target": "END",
                       "policy": "should_deep_reflect",
                       "targets": {"reflect": "不存在的节点", "save_memory": "save_memory"}})
    ok, _ = validate_spec(s)
    assert not ok


def test_protected_node_missing_rejected():
    s = base()
    s["nodes"].remove("execute")
    ok, _ = validate_spec(s)
    assert not ok


def test_unregistered_node_rejected():
    s = base()
    s["nodes"].append("hacker_node")
    ok, errs = validate_spec(s)
    assert not ok
    assert any("不在注册表" in e for e in errs)


def test_dual_exit_rejected():
    """同一节点不可同时有 plain 出边和 conditional 出边"""
    s = base()
    s["edges"].append({"source": "r_exec", "target": "r_eff",
                       "policy": "should_deep_reflect",
                       "targets": {"reflect": "reflect", "save_memory": "save_memory"}})
    ok, _ = validate_spec(s)
    assert not ok


def test_size_limit_rejected():
    s = base()
    s["nodes"] = s["nodes"] + [f"n{i}" for i in range(10)]
    ok, _ = validate_spec(s)
    assert not ok


def test_missing_policy_label_rejected():
    """条件边 targets 必须覆盖策略可能返回的全部标签（运行时 KeyError 防护）"""
    s = base()
    s["edges"] = [e for e in s["edges"] if not (e["source"] == "r_eff" and e.get("policy"))]
    s["edges"].append({"source": "r_eff", "target": "END",
                       "policy": "should_deep_reflect",
                       "targets": {"reflect": "reflect"}})
    ok, _ = validate_spec(s)
    assert not ok


def test_unknown_policy_rejected():
    s = base()
    s["edges"] = [e for e in s["edges"] if not (e["source"] == "r_eff" and e.get("policy"))]
    s["edges"].append({"source": "r_eff", "target": "END",
                       "policy": "hacker_policy",
                       "targets": {"reflect": "reflect", "save_memory": "save_memory"}})
    ok, _ = validate_spec(s)
    assert not ok


def test_architect_typical_proposal_valid():
    """架构师典型进化：tool_dispatch 接入（含出边）必须合法且可编译"""
    s = base()
    s["nodes"].append("tool_dispatch")
    s["edges"].append({"source": "r_load", "target": "tool_dispatch",
                       "policy": "route_execution",
                       "targets": {"execute": "execute", "tool_dispatch": "tool_dispatch"}})
    s["edges"].append({"source": "tool_dispatch", "target": "r_exec"})
    s["edges"] = [e for e in s["edges"]
                  if not (e["source"] == "r_load" and not e.get("policy"))]
    ok, errs = validate_spec(s)
    assert ok, errs
    build_graph(s)


def test_reflect_branch_shutdown_valid():
    """反思分支整体下线：4 删 + 1 改（架构师完整提案）"""
    s = base()
    for n in ("reflect", "r_reflect", "create_skill", "r_skill"):
        s["nodes"].remove(n)
        s["edges"] = [e for e in s["edges"] if e["source"] != n and e["target"] != n]
    for e in s["edges"]:
        if e.get("source") == "r_eff" and e.get("policy"):
            e["targets"] = {"reflect": "save_memory", "save_memory": "save_memory"}
    ok, errs = validate_spec(s)
    assert ok, errs


def test_registry_integrity():
    assert set(CONDITIONAL_POLICIES) == {"should_deep_reflect", "route_execution"}
    assert "tool_dispatch" in NODES
