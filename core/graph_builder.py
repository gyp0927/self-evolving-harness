#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/graph_builder.py —— 图即数据 + 结构自进化核心（档3，新增）

把 StateGraph 的拓扑从硬编码代码变成可序列化的数据（memory/graph_spec.json）：
- NODES：节点函数注册表（LLM 生成的 spec 只能引用注册表内的节点——安全边界）
- CONDITIONAL_POLICIES：条件路由策略注册表（spec 只能引用注册表内的策略）
- validate_spec：十条护栏规则，任何拓扑修改必须整体通过
- GraphRuntime：持有 spec + 编译后的 app，架构师切换/回滚时重新编译并替换引用
"""
import json
from copy import deepcopy
from langgraph.graph import StateGraph, END

from core.state import AgentState
from core.config import (GRAPH_SPEC_PATH, GRAPH_MAX_NODES, GRAPH_MAX_EDGES,
                         PROTECTED_NODES)
from core import executor
from core.rewarders import (reward_loader, reward_execute, reward_efficiency,
                            reward_reflect, reward_skill)
from core.memory import save_memory


# ============ 节点注册表（安全边界：spec 只能引用这里的名字）============
def node_load(state):
    mgr = executor.get_skill_mgr()
    state["loaded_skills"] = mgr.load_relevant(state["task"]) if mgr else "(无相关技能)"
    return state


def node_create_skill(state):
    mgr = executor.get_skill_mgr()
    if mgr is None:
        return state
    # 影子评估：persist=False，走完计算但不落盘、不碰元数据（奖励照常打分）
    persist = not state.get("_shadow", False)
    content, path, updated = mgr.create(state["reflection"], state["task"], persist=persist)
    state["new_skill"] = content
    state["skill_path"] = path
    state["skill_updated"] = updated
    return state


def should_deep_reflect(state) -> str:
    """条件策略：失败或重试过多 → 反思沉淀；否则直接存记忆"""
    if not state.get("success"):
        return "reflect"
    if state.get("_retries", 0) > 2:
        return "reflect"
    return "save_memory"


NODES = {
    "load_skills": node_load,
    "r_load": reward_loader,
    "execute": executor.execute_task,
    "tool_dispatch": executor.tool_dispatch,
    "r_exec": reward_execute,
    "r_eff": reward_efficiency,
    "reflect": executor.reflect,
    "r_reflect": reward_reflect,
    "create_skill": node_create_skill,
    "r_skill": reward_skill,
    "save_memory": save_memory,
}

# 条件策略注册表：name → (函数, 可能返回的标签集)。
# 验证器强制条件边的 targets 键必须覆盖这些标签——否则运行时 KeyError。
CONDITIONAL_POLICIES = {
    "should_deep_reflect": (should_deep_reflect, ("reflect", "save_memory")),
    "route_execution": (executor.route_execution, ("execute", "tool_dispatch")),
}


# ============ 默认图（当前拓扑的数据表示；spec 缺失/损坏时的回退）============
DEFAULT_SPEC = {
    "version": 1,
    "version_label": "default",
    "entry": "load_skills",
    "nodes": ["load_skills", "r_load", "execute", "r_exec", "r_eff",
              "reflect", "r_reflect", "create_skill", "r_skill", "save_memory"],
    "edges": [
        {"source": "load_skills", "target": "r_load"},
        {"source": "r_load", "target": "execute"},
        {"source": "execute", "target": "r_exec"},
        {"source": "r_exec", "target": "r_eff"},
        {"source": "r_eff", "target": "END", "policy": "should_deep_reflect",
         "targets": {"reflect": "reflect", "save_memory": "save_memory"}},
        {"source": "reflect", "target": "r_reflect"},
        {"source": "r_reflect", "target": "create_skill"},
        {"source": "create_skill", "target": "r_skill"},
        {"source": "r_skill", "target": "save_memory"},
        {"source": "save_memory", "target": "END"},
    ],
}


# ============ 图验证器（十条护栏，全部通过才 OK）============
def validate_spec(spec: dict):
    """校验图 spec。返回 (ok, errors)。任何修改必须整体通过才能生效。"""
    errors = []
    if not isinstance(spec, dict):
        return False, ["spec 非 dict"]
    for k in ("entry", "nodes", "edges"):
        if k not in spec:
            errors.append(f"缺字段 {k}")
    if errors:
        return False, errors

    nodes, entry, edges = spec["nodes"], spec["entry"], spec["edges"]
    node_set = set(nodes)

    # 1. entry 必须存在于节点集
    if entry not in node_set:
        errors.append(f"entry {entry} 不在节点集")
    # 2. 节点必须全部在注册表内（安全边界），且无重名
    for n in nodes:
        if n not in NODES:
            errors.append(f"节点 {n} 不在注册表")
    if len(node_set) != len(nodes):
        errors.append("节点有重名")
    # 3. 保护节点不可缺失
    missing = PROTECTED_NODES - node_set
    if missing:
        errors.append(f"保护节点缺失: {sorted(missing)}")
    # 4. 无自环、无重复边
    seen = set()
    for e in edges:
        if not isinstance(e, dict) or "source" not in e or "target" not in e:
            errors.append(f"非法边: {e}")
            continue
        if e["source"] == e["target"]:
            errors.append(f"自环: {e['source']}")
        key = (e["source"], e["target"])
        if key in seen:
            errors.append(f"重复边: {key}")
        seen.add(key)
    # 5. 同节点不可既有 plain 出边又有 conditional 出边
    plain_src = {e["source"] for e in edges if not e.get("policy")}
    cond_src = {e["source"] for e in edges if e.get("policy")}
    conflict = plain_src & cond_src
    if conflict:
        errors.append(f"节点同时有 plain/conditional 出边: {sorted(conflict)}")
    # 6. 无环（Kahn 拓扑排序）
    def _targets_of(e):
        """边的实际路由目标：条件边取 targets 映射的值，普通边取 target"""
        if e.get("policy"):
            return [v for v in (e.get("targets") or {}).values() if v != "END"]
        return [e["target"]] if e["target"] != "END" else []

    adj = {n: [] for n in nodes}
    indeg = {n: 0 for n in nodes}
    for e in edges:
        for t in _targets_of(e):
            if t not in node_set:
                continue  # 悬空目标交给规则 9 报错
            # 边引用的 source 可能已被删（规则 2/3 会报错，这里容错不崩）
            adj.setdefault(e["source"], []).append(t)
            indeg[t] += 1
    queue = [n for n in nodes if indeg[n] == 0]
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if len(order) != len(nodes):
        errors.append(f"存在环，无法拓扑排序（Kahn 剩余 {len(nodes) - len(order)} 节点）")
    # 7. 全部节点从 entry 可达（BFS，条件边按 targets 扩展）
    reachable = set()
    stack = [entry]
    while stack:
        n = stack.pop()
        if n in reachable:
            continue
        reachable.add(n)
        for e in edges:
            if e["source"] == n:
                stack.extend(_targets_of(e))
    unreachable = node_set - reachable
    if unreachable:
        errors.append(f"从 entry 不可达: {sorted(unreachable)}")
    # 8. 全部节点可到达 END（逆 BFS，条件边按 targets 反向连接）
    rev = {n: [] for n in nodes}
    for e in edges:
        for t in _targets_of(e):
            if t in rev:
                rev[t].append(e["source"])  # 悬空目标跳过（规则 9 会报错）
        if not e.get("policy") and e["target"] == "END":
            rev.setdefault("END", []).append(e["source"])
    can_end = set()
    stack = ["END"]
    while stack:
        n = stack.pop()
        if n in can_end:
            continue
        can_end.add(n)
        for m in rev.get(n, []):
            stack.append(m)
    dead = node_set - can_end
    if dead:
        errors.append(f"无法到达 END: {sorted(dead)}")
    # 9. conditional 边的 policy 必须在策略注册表，targets 必须在节点集
    for e in edges:
        if not e.get("policy"):
            continue
        entry = CONDITIONAL_POLICIES.get(e["policy"])
        if entry is None:
            errors.append(f"条件策略 {e['policy']} 不在注册表")
            continue
        targets = e.get("targets") or {}
        # targets 键必须覆盖策略可能返回的所有标签（运行时 KeyError 防护）
        missing_keys = set(entry[1]) - set(targets.keys())
        if missing_keys:
            errors.append(f"条件边 {e['source']} 的 targets 缺标签 {sorted(missing_keys)}")
        for k, v in targets.items():
            if v != "END" and v not in node_set:
                errors.append(f"条件目标 {v} 不在节点集（边 {e['source']}）")
    # 10. 规模上限
    if len(nodes) > GRAPH_MAX_NODES:
        errors.append(f"节点数 {len(nodes)} 超上限 {GRAPH_MAX_NODES}")
    if len(edges) > GRAPH_MAX_EDGES:
        errors.append(f"边数 {len(edges)} 超上限 {GRAPH_MAX_EDGES}")

    return (not errors), errors


# ============ 加载 / 构建 ============
def load_spec(path=None) -> dict:
    """读 graph_spec.json；缺失/损坏/校验失败 → 默认图（三层 fallback）"""
    path = path or GRAPH_SPEC_PATH
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        ok, errors = validate_spec(spec)
        if ok:
            return spec
        print(f"⚠️ graph_spec 校验失败({errors})，回退默认图")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ graph_spec 读取失败({e})，回退默认图")
    return deepcopy(DEFAULT_SPEC)


def save_spec(spec: dict, path=None):
    path = path or GRAPH_SPEC_PATH
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")


def build_graph(spec: dict = None):
    """从 spec 构建编译好的 LangGraph app"""
    if spec is None:
        spec = load_spec()
    g = StateGraph(AgentState)
    for name in spec["nodes"]:
        g.add_node(name, NODES[name])
    g.set_entry_point(spec["entry"])
    for e in spec["edges"]:
        t = END if e["target"] == "END" else e["target"]
        if e.get("policy"):
            policy_fn = CONDITIONAL_POLICIES[e["policy"]][0]
            mapping = {k: (END if v == "END" else v) for k, v in e["targets"].items()}
            g.add_conditional_edges(e["source"], policy_fn, mapping)
        else:
            g.add_edge(e["source"], t)
    return g.compile()


class GraphRuntime:
    """图运行时持有者：spec + 编译好的 app。
    架构师切换/回滚 = recompile/restore，引用自动更新（避免旧 app 单例问题）"""

    def __init__(self, spec: dict = None):
        if spec is None:
            spec = load_spec()
        self.spec = spec
        self.graph_version = spec.get("version_label", "default")
        self.app = build_graph(spec)

    def recompile(self, spec: dict):
        ok, errors = validate_spec(spec)
        if not ok:
            raise ValueError(f"spec 非法: {errors}")
        self.spec = spec
        self.graph_version = spec.get("version_label", "default")
        self.app = build_graph(spec)

    def snapshot(self) -> dict:
        return deepcopy(self.spec)

    def restore(self, spec: dict):
        self.recompile(spec)
