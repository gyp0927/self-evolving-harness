#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/architect.py —— 架构师：图拓扑进化检查点（档3，新增）

流程（每 EPOCH_ROUNDS 轮触发一次）：
  回滚检测 → LLM 提案 → 操作应用+图验证器 → 影子评估（天气回放）→ 门控 → 切换/拒绝
安全护栏四层：操作白名单、图验证器、影子评估、门控+回滚。
全部决策写 evolution_log.jsonl（可审计）。
"""
import json
import random
import time
from copy import deepcopy

from core.config import (SHADOW_ROUNDS, GATE_THRESHOLD, GATE_CONFIDENCE,
                         ROLLBACK_WINDOW, ROLLBACK_THRESHOLD,
                         ROLLBACK_COOLDOWN_SECONDS, PROTECTED_NODES)
from core.graph_builder import validate_spec, build_graph, save_spec, CONDITIONAL_POLICIES
from core.memory import window_records, window_stats
from core.weights import recompute_total, load_weights
from core.evolution_log import log_event, read_events, save_snapshot, load_snapshot
from core.llm_client import llm_client
from core import executor

ARCHITECT_PROMPT = """你是Agent架构师。审视进化历史，提出图拓扑修改提案（结构自进化）。

当前图 spec (JSON):
{spec}

轨迹统计（最近窗口，按当前权重重算）:
{stats}

可用技能工具:
{tools}

进化建议（基于当前图现状）:
{hint}

可选操作（一次最多3个，输出 JSON 数组）:
- add_edge     {{"op": "add_edge", "source": "...", "target": "...", "policy": "<策略名>", "targets": {{标签: 节点}}}}
               （policy 缺省 = 普通边；带 policy = 条件边，必须同时给 targets）
- remove_edge  {{"op": "remove_edge", "source": "...", "target": "..."}}
- add_node     {{"op": "add_node", "name": "tool_dispatch"}}   （只能加已注册的节点，且不可重复）
- remove_node  {{"op": "remove_node", "name": "..."}}
- change_entry {{"op": "change_entry", "entry": "..."}}
- set_policy   {{"op": "set_policy", "source": "...", "policy": "...", "targets": {{标签: 节点}}}}
- noop

规则:
- 保护节点不可删除: {protected}
- 可用条件策略: {policies}
- 新增节点必须同时提供出边（否则无法到达 END）
- 目标：提升总奖励或成功率（特别是降低降级率）；没有把握的修改不要提（noop 也是有效提案）
- 输出: {{"ops": [...]}}，只要 JSON，不要代码块

最近被拒的提案及原因（避免重犯）:
{rejects}
"""


def _evolution_hint(spec: dict) -> str:
    """根据当前图现状给出进化建议（避免重复提案已实现的修改）"""
    nodes = spec["nodes"]
    if "tool_dispatch" not in nodes:
        return ("当前图还没有 tool_dispatch（技能工具分发器）节点。典型进化："
                "add_node tool_dispatch + 改 r_load 为条件路由 "
                "(policy=route_execution, targets={{'execute': 'execute', 'tool_dispatch': 'tool_dispatch'}}) "
                "+ 加出边 tool_dispatch→r_exec + 删除 r_load→execute 直边")
    hints = ["当前图已含 tool_dispatch（确定性技能分发已生效）。可考虑："]
    if "reflect" in nodes:
        hints.append(
            "- 反思分支整体下线（完整模板，直接可用）: ops = ["
            '{"op": "remove_node", "name": "reflect"}, '
            '{"op": "remove_node", "name": "r_reflect"}, '
            '{"op": "remove_node", "name": "create_skill"}, '
            '{"op": "remove_node", "name": "r_skill"}, '
            '{"op": "set_policy", "source": "r_eff", "policy": "should_deep_reflect", '
            '"targets": {"reflect": "save_memory", "save_memory": "save_memory"}}] '
            "—— 4 个节点必须一次全删，否则残留节点会失联被验证器拒绝")
    hints.append("- 调整条件路由策略（set_policy 改 targets 或换 policy）")
    hints.append("- 注意：change_entry 跳过的节点会成为不可达孤岛，验证器会拒绝；"
                 "除非同时 remove 掉被跳过的节点")
    return "\n".join(hints)


def _propose(spec, stats, tools) -> list:
    """架构师 LLM 生成修改提案（禁用缓存，避免同一提案反复提出）。
    最近被拒提案及原因喂回 prompt——架构师从自己的错误中学习"""
    recent_rejects = [e for e in read_events() if e["kind"] == "rejected"][-3:]
    rejects_text = json.dumps([
        {"ops": r.get("ops", []), "errors": r.get("errors") or r.get("reason", ""),
         "fix": r.get("fix", "")}
        for r in recent_rejects
    ], ensure_ascii=False)[:1500] or "(无)"
    prompt = ARCHITECT_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False, indent=1),
        stats=json.dumps(stats, ensure_ascii=False, indent=1)[:3000],
        tools=json.dumps(tools, ensure_ascii=False)[:1000],
        hint=_evolution_hint(spec),
        protected=", ".join(sorted(PROTECTED_NODES)),
        policies=", ".join(CONDITIONAL_POLICIES.keys()),
        rejects=rejects_text)
    raw = llm_client.call(prompt, use_cache=False)
    try:
        return json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("ops", [])
    except Exception:
        return None


def _suggest_fix(errors: list) -> str:
    """从验证错误自动生成修复建议（喂回架构师，帮它收敛到合法提案）"""
    fixes = []
    for e in errors:
        if "不可达" in e:
            fixes.append(f"{e} → 同时移除这些节点或为它们补入边")
        elif "无法到达 END" in e:
            fixes.append(f"{e} → 同时移除这些节点或为它们补出边")
        elif "缺标签" in e:
            fixes.append(f"{e} → 补齐缺失的 targets 标签")
        elif "已有" in e or "重复" in e:
            fixes.append(f"{e} → 不要在提案里重复添加已存在的内容")
        elif "保护节点" in e or "不在注册表" in e:
            fixes.append(f"{e} → 不碰保护节点，只用注册表内的名字")
    return "；".join(fixes) if fixes else ""


def _reject(op, reason: str, fix: str = ""):
    """统一的拒绝日志（所有失败路径可审计，附修复建议）"""
    payload = {"reason": reason, "ops": op if isinstance(op, list) else [op] if op else []}
    if fix:
        payload["fix"] = fix
    log_event("rejected", payload)


def _apply_ops(spec: dict, ops: list):
    """批量应用操作，原子化：任一 op 非法或结果图不过验证 → 整体拒绝（返回 None）。
    纯 noop/无实际修改 → 返回 None（没有候选图，不跑影子评估）"""
    if not ops:
        return None
    spec = deepcopy(spec)
    changed = False
    for op in ops:
        kind = op.get("op")
        if kind == "noop":
            continue
        elif kind == "add_edge":
            e = {"source": op.get("source"), "target": op.get("target")}
            if op.get("policy"):
                e["policy"] = op["policy"]
                e["targets"] = op.get("targets") or {}
            spec["edges"].append(e)
            changed = True
        elif kind == "remove_edge":
            before = len(spec["edges"])
            spec["edges"] = [e for e in spec["edges"]
                             if not (e["source"] == op.get("source")
                                     and e["target"] == op.get("target"))]
            if len(spec["edges"]) < before:
                changed = True
        elif kind == "add_node":
            name = op.get("name")
            if not name or name in spec["nodes"]:
                _reject(op, f"add_node 非法或节点已存在: {name}")
                return None
            spec["nodes"].append(name)  # 是否在注册表由验证器把关
            changed = True
        elif kind == "remove_node":
            name = op.get("name")
            if name not in spec["nodes"]:
                _reject(op, f"remove_node 节点不存在: {name}")
                return None
            if name in PROTECTED_NODES:
                _reject(op, f"remove_node 触碰保护节点: {name}")
                return None
            spec["nodes"].remove(name)
            spec["edges"] = [e for e in spec["edges"]
                             if e["source"] != name and e["target"] != name]
            changed = True
        elif kind == "change_entry":
            if op.get("entry") not in spec["nodes"]:
                _reject(op, f"change_entry 目标不在节点集: {op.get('entry')}")
                return None
            spec["entry"] = op.get("entry")
            changed = True
        elif kind == "set_policy":
            source, policy = op.get("source"), op.get("policy")
            targets = op.get("targets") or {}
            found = False
            for e in spec["edges"]:
                if e.get("source") == source and e.get("policy"):
                    e["policy"] = policy
                    e["targets"] = targets
                    found = True
                    changed = True
            if not found:
                _reject(op, f"set_policy 未找到条件边: {source}")
                return None
        else:
            _reject(op, f"未知操作: {kind}")
            return None
    if not changed:
        _reject(None, "noop（无实际修改）")
        return None
    ok, errors = validate_spec(spec)
    if not ok:
        _reject(ops, f"验证失败: {errors}", fix=_suggest_fix(errors))
        return None
    return spec


def run_shadow_eval(candidate_spec: dict, tasks: list, events: list = None,
                    rounds: int = SHADOW_ROUNDS, *, cases=None) -> list:
    """影子评估：候选图在天气回放（生产窗口真实请求序列）下跑 K 轮，只算不写。
    影子与生产面对完全相同的"服务天气"，零打扰真实服务（burst 状态不受污染）。

    公平性（cases 模式）：每个任务用**自己那一轮**的生产事件段回放。
    旧实现共享一个游标顺序消费整段序列——候选图重试次数一变，消费的事件数
    就变，后面任务见到的天气跟着错位，候选与生产就不是同一场雨。
    cases 为空时退回共享游标模式，仅用于兼容旧调用。"""
    import mock_api  # 顶层模块（仓库根目录/mock_api.py），不在 core 包内
    from core.state import new_state
    from core.tracer import silent
    app = build_graph(candidate_spec)
    results = []
    with silent():
        if cases:
            for i in range(rounds):
                for task, segment in cases:
                    st = new_state(task, shadow=True, round_id=9000 + i,
                                   graph_version="shadow-candidate")
                    st["_oracle"] = mock_api.make_replay_oracle(segment)
                    final = app.invoke(st)
                    results.append({
                        "success": final["success"],
                        "total_reward": final["total_reward"],
                    })
        else:
            oracle = mock_api.make_replay_oracle(events or [])
            for i in range(rounds):
                for task in tasks:
                    st = new_state(task, shadow=True, round_id=9000 + i,
                                   graph_version="shadow-candidate")
                    st["_oracle"] = oracle
                    final = app.invoke(st)
                    results.append({
                        "success": final["success"],
                        "total_reward": final["total_reward"],
                    })
    return results


def _bootstrap_win_prob(shadow: list, prod_rewards: list, n: int = 1000) -> float:
    """P(影子均值 > 生产均值)：bootstrap 重采样，固定种子保证可复现。
    小样本（约 9 条）下只看点估计会把噪声当提升，这里用胜率兜底。"""
    if not shadow or not prod_rewards:
        return 1.0
    rng = random.Random(0)
    s_vals = [r["total_reward"] for r in shadow]
    wins = 0
    for _ in range(n):
        s = sum(rng.choice(s_vals) for _ in range(len(s_vals))) / len(s_vals)
        p = sum(rng.choice(prod_rewards) for _ in range(len(prod_rewards))) / len(prod_rewards)
        if s > p:
            wins += 1
    return wins / n


def _gate(shadow: list, prod: dict, prod_rewards: list = None):
    """门控三条件：成功率不降、点估计 ≥ 生产 + 阈值、提升胜率 ≥ GATE_CONFIDENCE。
    第三条是小样本防噪：9 条记录的点估计可能只是运气。"""
    if not shadow:
        return False, "影子无结果"
    sr = sum(1 for r in shadow if r["success"]) / len(shadow)
    avg = sum(r["total_reward"] for r in shadow) / len(shadow)
    if sr < prod["success_rate"]:
        return False, f"影子成功率 {sr:.2f} < 生产 {prod['success_rate']:.2f}"
    if avg < prod["avg_total"] + GATE_THRESHOLD:
        return False, (f"影子总分 {avg:.2f} < 生产 {prod['avg_total']:.2f} + "
                       f"{GATE_THRESHOLD}")
    if prod_rewards:
        win = _bootstrap_win_prob(shadow, prod_rewards)
        if win < GATE_CONFIDENCE:
            return False, (f"提升胜率 {win:.2f} < {GATE_CONFIDENCE}（点估计 "
                           f"{avg:.2f} 可能来自噪声）")
    return True, (f"通过 (成功率 {sr:.2f}≥{prod['success_rate']:.2f}, "
                  f"总分 {avg:.2f}≥{prod['avg_total']+GATE_THRESHOLD:.2f})")


def _maybe_rollback(runtime, records, weights, tasks) -> tuple:
    """切换后劣化检测：最近窗口生产均值 < 切换前基线 − 阈值 → 回滚到快照"""
    switches = [e for e in read_events() if e["kind"] == "switch"]
    if not switches:
        return False, ""
    baseline = switches[-1].get("baseline")
    baseline_raw = switches[-1].get("baseline_raw")
    if baseline is None and baseline_raw is None:
        return False, ""
    window = window_records(records, ROLLBACK_WINDOW, task_ids=set(tasks))
    if not window:
        return False, ""
    avg = sum(recompute_total(r, weights) for r in window) / len(window)
    # 权重是自适应的：必须用同一套当前权重重算基线，否则基线是旧权重下的分数，
    # 与现在不可比（旧实现直接比存储的加权分，权重一漂移就误判）。
    if baseline_raw:
        base_avg = sum(recompute_total({"rewards": rws}, weights)
                       for rws in baseline_raw) / len(baseline_raw)
    else:
        base_avg = baseline
    if avg < base_avg - ROLLBACK_THRESHOLD:
        snap = load_snapshot()
        if snap is not None:
            runtime.restore(snap)
            save_spec(snap)
            log_event("rollback", {
                "reason": f"平均 {avg:.2f} < 基线 {base_avg:.2f} - {ROLLBACK_THRESHOLD}",
                "restored_version": snap.get("version_label", "default"),
            })
            return True, f"图已回滚: 平均 {avg:.2f} 低于切换前基线 {base_avg:.2f}"
    return False, ""


def run_epoch_checkpoint(runtime, tasks, round_events: dict):
    """架构师检查点主流程（每 EPOCH_ROUNDS 轮调用一次）。
    round_events: {round_id: [请求结果序列]}（生产窗口天气，供影子回放）"""
    from core.tracer import section, info, step
    from core.evolution_log import log_event
    from core.memory import iter_trajectory
    from core.config import EPOCH_ROUNDS

    section("🧠 架构师检查点（结构自进化）")
    weights = load_weights()
    records = iter_trajectory()
    canonical = set(tasks)

    # 0. 回滚检测（上次切换是否劣化）
    rolled, why = _maybe_rollback(runtime, records, weights, tasks)
    if rolled:
        info("回滚", why, "red")
        return

    # 0b. 回滚冷却期：回滚后一段时间不提案（防振荡）
    recent_evts = [e for e in read_events()
                   if e["kind"] in ("rollback",)][-1:]
    if recent_evts:
        try:
            t = time.mktime(time.strptime(recent_evts[-1]["time"], "%Y-%m-%d %H:%M:%S"))
            if time.time() - t < ROLLBACK_COOLDOWN_SECONDS:
                info("冷却", "回滚后冷却期内不提案", "dim")
                return
        except Exception:
            pass

    prod_records = window_records(records, SHADOW_ROUNDS, task_ids=canonical)
    prod = window_stats(prod_records, weights)
    info("生产窗口", f"{prod['n']} 轮 成功率={prod['success_rate']:.2f} 平均={prod['avg_total']:.2f}")

    # 1. 提案（LLM）
    mgr = executor.get_skill_mgr()
    tools = mgr.registry() if mgr else []
    ops = _propose(runtime.spec, prod, tools)
    log_event("propose", {"version": runtime.graph_version, "ops": ops or []})
    if not ops:
        info("提案", "noop（无修改把握）", "dim")
        return

    # 2. 操作应用 + 图验证器
    candidate = _apply_ops(runtime.spec, ops)
    if candidate is None:
        info("提案", "被验证器拒绝（已记日志）", "red")
        return
    step("候选图: 基于 " + runtime.graph_version + " 的拓扑修改")
    for op in ops:
        step(f"  op: {op}")

    # 3. 影子评估（按任务分段回放：每个生产轮用自己的天气段，
    #    候选图重试次数变化不再把后面任务的天气顶错位）
    cases = []
    recent_rounds = sorted(round_events.keys())[-SHADOW_ROUNDS:]
    for r in recent_rounds:
        seg = round_events.get(r) or []
        if seg:
            cases.append((tasks[(r - 1) % len(tasks)], seg))
    if not cases:
        info("影子评估", "无生产天气可回放，跳过", "yellow")
        return
    shadow = run_shadow_eval(candidate, tasks, rounds=1, cases=cases)
    log_event("shadow", {"version": "candidate", "results": shadow})

    # 4. 门控
    ok, msg = _gate(shadow, prod, [recompute_total(r, weights) for r in prod_records])
    info("门控", f"{msg}", "green" if ok else "yellow")
    if not ok:
        log_event("rejected", {"reason": msg, "ops": ops})
        return

    # 5. 切换（先备份快照，再写 spec 并重新编译）
    baseline = prod["avg_total"]
    version_label = f"g{len([e for e in read_events() if e['kind'] == 'switch']) + 1}"
    candidate["version"] = candidate.get("version", 1) + 1
    candidate["version_label"] = version_label
    save_snapshot(runtime.spec, "before-switch")
    save_spec(candidate)
    runtime.recompile(candidate)
    # 基线同时存 raw 分量：权重是自适应的，只存加权分会让回滚比较失真
    baseline_raw = [[{"step": rw.get("step", ""), "raw": rw.get("raw", 0.0)}
                     for rw in r.get("rewards", [])] for r in prod_records]
    log_event("switch", {"version": version_label, "ops": ops,
                         "baseline": baseline, "baseline_raw": baseline_raw})
    info("切换", f"图已进化为 {version_label}（基线 {baseline:.2f}）", "green")


def show_architecture_status(runtime):
    """展示当前图结构（main 每次运行结束时调用）"""
    from core.tracer import section, step
    section("🗺️ 当前图结构")
    step(f"版本: {runtime.graph_version}")
    step(f"入口: {runtime.spec['entry']}  节点: {len(runtime.spec['nodes'])}  边: {len(runtime.spec['edges'])}")
    for n in runtime.spec["nodes"]:
        outs = [e for e in runtime.spec["edges"] if e["source"] == n]
        if outs:
            desc = "; ".join(
                f"→{e['target']}" + (f"[{e['policy']}]" if e.get("policy") else "")
                for e in outs)
            step(f"  {n}: {desc}")
    step(f"保护节点: {', '.join(sorted(PROTECTED_NODES))}")
