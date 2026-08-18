#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/executor.py —— 真实任务执行 + 反思（结构自进化版）

核心结构：
1. LLM 决策两种模式：调用技能工具（确定性执行其 action）或自定义策略
2. 执行循环 _act() 被 execute_task 与 tool_dispatch 节点共享（探测/重试/熔断/降级）
3. API 可注入（_oracle）：影子评估用天气回放 oracle，生产用真实请求
4. route_execution：条件策略——有匹配技能工具 → 走 tool_dispatch 节点（档3 图进化的接点）
"""
import json
import time
import urllib.request

from core.state import AgentState, record_step, summarize_trace
from core.llm_client import llm_client
from core.config import (MOCK_API_URL, MOCK_TIMEOUT, PRE_CHECK_TIMEOUT,
                         CIRCUIT_THRESHOLD)

_skill_mgr = None  # 由 main 注入（set_skill_mgr），tool_dispatch/route_execution 依赖


def set_skill_mgr(mgr):
    global _skill_mgr
    _skill_mgr = mgr


def get_skill_mgr():
    return _skill_mgr


STRATEGY_PROMPT = """你是一个 API 调用策略专家。任务：调用一个可能超时的 API 并保证成功。

可参考的技能（可能为空）:
{loaded_skills}

可用技能工具（可调用，自带已验证的 action 配置）:
{tools}

输出 JSON 策略（不要代码块，只要 JSON），两种模式任选其一:
A. 调用技能工具: {{"mode": "tool", "tool": "<工具名>"}}
   —— 工具会按它的 action 配置确定性执行，你只需选对工具
   （仅在"可用技能工具"段列出工具时使用模式A；显示"(无可用工具)"时禁止模式A）
B. 自定义策略: {{"max_retries": 3, "base_delay": 0.5, "backoff": "exponential",
   "pre_check": true, "circuit_breaker": true, "fallback": "cached"}}

字段说明（模式B）:
- max_retries: 1-5，base_delay: 0.2-2.0 秒，backoff: "exponential"(指数退避,推荐) 或 "fixed"
- pre_check: true/false —— 是否先做一次短超时探测。探测失败说明服务可能正不可用，
  直接降级兜底，避免在死服务上浪费正式请求的重试
- circuit_breaker: true/false —— 连续失败达到阈值后熔断，停止无谓重试，直接降级
- fallback: "cached"/"default"/null —— 兜底方式。重试耗尽、熔断或探测失败后，
  返回可用替代结果（缓存/默认值），确保"保证有结果"而不是无限重试

规则：
- 有匹配的技能工具时优先用模式A（技能是已验证的经验，比你现场想更可靠）
- 如果技能里有相关经验，遵循技能建议（尤其是 pre_check / circuit_breaker / fallback）
- 目标不是"永远重试成功"，而是"保证有结果"
"""

REFLECTION_PROMPT = """你是Agent进化反思器。分析执行轨迹,提炼可复用经验。

任务:{task}
已有技能:{loaded_skills}
轨迹(摘要):{trace}
执行结果: 成功={success}, 总耗时={elapsed}s, 重试={retries}次

输出:
1. 【成功经验】
2. 【失败教训】
3. 【可复用流程】
4. 【技能建议】(skill名称)
纯文本,不要代码块:
"""


def _real_api(timeout: float) -> bool:
    """真实请求 mock API，返回是否成功（超时/异常 → False）"""
    try:
        urllib.request.urlopen(MOCK_API_URL, timeout=timeout)
        return True
    except Exception:
        return False


def _call_api(state: AgentState, timeout: float) -> bool:
    """API 调用入口：影子评估用天气回放 oracle，生产用真实请求"""
    oracle = state.get("_oracle")
    if oracle is not None:
        return oracle(timeout)
    return _real_api(timeout)


def _as_bool(v) -> bool:
    """防御性布尔解析：LLM 可能输出字符串 'true'/'false'"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _act(state: AgentState, strat: dict, api) -> AgentState:
    """共享执行循环：前置探测 → 重试（含熔断）→ 降级兜底。
    strat = 策略配置（自定义或技能 action），api = call(timeout) -> bool"""
    from core.tracer import show_execution
    max_retries = int(strat.get("max_retries", 3))
    base_delay = float(strat.get("base_delay", 0.5))
    exponential = strat.get("backoff") == "exponential"
    fallback = strat.get("fallback")
    pre_check = _as_bool(strat.get("pre_check", False))
    circuit = _as_bool(strat.get("circuit_breaker", False))

    start = time.time()
    retries = 0
    success = False
    degraded = False
    attempts = []

    # 前置探测（可选）：短超时试探。探测失败 = 服务正不可用 → 直接降级，不浪费重试
    if pre_check:
        probe_ok = api(PRE_CHECK_TIMEOUT)
        if probe_ok:
            state = record_step(state, "tool_call", "前置探测", "HEAD /data", "✅探测成功")
        else:
            state = record_step(state, "tool_call", "前置探测", "HEAD /data", "⏱️探测超时→服务不可用")
            show_execution([{"ok": False, "probe": True}], success=False, elapsed=0.3,
                           note="前置探测失败 → 服务可能不可用，跳过正式重试")
            elapsed = round(time.time() - start, 2)
            if fallback:
                degraded, success = True, True
                result = f"任务完成(降级): 探测失败→{fallback}兜底, 耗时{elapsed}s"
            else:
                result = f"任务失败: 探测失败且无降级兜底, 耗时{elapsed}s"
            state["result"] = result
            state["success"] = success
            state["_degraded"] = degraded
            state["_elapsed"] = elapsed
            state["_retries"] = 0
            state = record_step(state, "finish", "任务结束", "汇总", result)
            return state

    # 正式请求重试循环（含熔断：连续失败达到阈值 → 停止重试直接降级）
    fail_streak = 0
    for attempt in range(max_retries + 1):
        ok = api(MOCK_TIMEOUT)
        if ok:
            success = True
            attempts.append({"ok": True, "t": 0.05})
            state = record_step(state, "tool_call", f"第{attempt+1}次请求", "GET /data", "✅成功")
            break
        retries += 1
        fail_streak += 1
        attempts.append({"ok": False, "t": MOCK_TIMEOUT})
        state = record_step(state, "tool_call", f"第{attempt+1}次超时", "GET /data", "⏱️超时")
        if circuit and fail_streak >= CIRCUIT_THRESHOLD:
            state = record_step(state, "plan", "熔断", "熔断器打开", f"连续{fail_streak}次失败")
            break
        if attempt < max_retries:
            delay = base_delay * (2 ** attempt) if exponential else base_delay
            attempts[-1]["delay"] = delay
            state = record_step(state, "wait", "退避等待", f"sleep {delay:.1f}s", "等待")
            time.sleep(delay)

    # 重试耗尽或熔断后：降级兜底（"保证有结果"）
    if not success and fallback:
        degraded, success = True, True
        state = record_step(state, "tool_call", "降级", f"fallback={fallback}", "✅返回兜底结果")

    elapsed = round(time.time() - start, 2)
    show_execution(attempts, success, elapsed, degraded=degraded, fallback=fallback)
    state["result"] = (f"任务完成(降级): 重试{retries}次后{fallback}兜底, 耗时{elapsed}s" if degraded
                       else f"任务完成: success={success}, 重试{retries}次, 耗时{elapsed}s")
    state["success"] = success
    state["_degraded"] = degraded
    state["_elapsed"] = elapsed
    state["_retries"] = retries
    state = record_step(state, "finish", "任务结束", "汇总", state["result"])
    return state


def _tools_block() -> str:
    """技能工具清单（注入 prompt）：无工具时为空"""
    if _skill_mgr is None:
        return "(无可用工具)"
    tools = _skill_mgr.registry()
    if not tools:
        return "(无可用工具)"
    return "\n".join(f"- {t['name']}: {t['description']} (action={t['action']})"
                     for t in tools)


def execute_task(state: AgentState) -> AgentState:
    """真实任务：LLM 决策（调用技能工具或自定义策略）→ 执行循环"""
    from core.tracer import show_strategy
    task = state["task"]
    loaded = state.get("loaded_skills", "")

    # 1. LLM 根据技能 + 工具清单生成决策（技能被真正消费）
    strategy_prompt = STRATEGY_PROMPT.format(
        loaded_skills=loaded[:1500], tools=_tools_block())
    state = record_step(state, "plan", "生成重试策略", "LLM决策", strategy_prompt[:50])
    raw = llm_client.call(strategy_prompt)
    try:
        strat = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception:
        strat = {}
    show_strategy(raw, strat)

    # 2. 模式A：调用技能工具（确定性执行其 action）
    if strat.get("mode") == "tool" and _skill_mgr is not None:
        # 名称归一化：LLM 可能把技能名带 .md 后缀（loaded_skills 注入格式导致）
        tool_name = str(strat.get("tool", "")).removesuffix(".md")
        tool = next((t for t in _skill_mgr.registry() if t["name"] == tool_name), None)
        if tool is not None:
            state["_tool_used"] = tool_name
            state["_strategy"] = dict(tool["action"])
            state = record_step(state, "plan", "调用技能工具", tool_name,
                                f"action={tool['action']}")
            return _act(state, tool["action"], lambda t: _call_api(state, t))
        # 工具匹配失败：明确记录 + 让 LLM 重新生成自定义策略（不静默吞掉）
        available = [t["name"] for t in _skill_mgr.registry()]
        state = record_step(state, "plan", "工具调用失败", str(strat.get("tool")),
                            f"可用工具: {available or '无'}")
        retry_prompt = (strategy_prompt +
                        f"\n注意: 你调用的工具 '{str(strat.get('tool'))}' 不存在"
                        f"（可用: {available or '无'}）。请改为输出模式B自定义策略 JSON。")
        raw = llm_client.call(retry_prompt)
        try:
            strat = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        except Exception:
            strat = {}

    # 3. 模式B：自定义策略
    strat = {
        "max_retries": int(strat.get("max_retries", 3)),
        "base_delay": float(strat.get("base_delay", 0.5)),
        "backoff": strat.get("backoff", "exponential"),
        "pre_check": _as_bool(strat.get("pre_check", False)),
        "circuit_breaker": _as_bool(strat.get("circuit_breaker", False)),
        "fallback": strat.get("fallback") or None,
    }
    state["_strategy"] = strat
    state = record_step(state, "plan", "策略生成", "解析JSON",
                        f"max_retries={strat['max_retries']} backoff={strat['backoff']} "
                        f"pre_check={strat['pre_check']} circuit={strat['circuit_breaker']} "
                        f"fallback={strat['fallback']}")
    return _act(state, strat, lambda t: _call_api(state, t))


def tool_dispatch(state: AgentState) -> AgentState:
    """技能工具分发器节点（档1+档3）：匹配技能 → 确定性执行其 action。
    无匹配技能时置失败（由下游条件路由兜底，不会卡死图）"""
    from core.tracer import show_tool_dispatch
    tool = None
    if _skill_mgr is not None:
        tool = _skill_mgr.match_tool(state["task"])
    if tool is None:
        state["result"] = "tool_dispatch: 无匹配技能工具"
        state["success"] = False
        state["_elapsed"] = 0.0
        state["_retries"] = 0
        return state
    state["_tool_used"] = tool["name"]
    state["_strategy"] = dict(tool["action"])
    state = record_step(state, "plan", "工具调度", f"调用技能 {tool['name']}",
                        f"action={tool['action']}")
    show_tool_dispatch(tool)
    return _act(state, tool["action"], lambda t: _call_api(state, t))


def route_execution(state: AgentState) -> str:
    """条件策略（注册进 CONDITIONAL_POLICIES）：有匹配技能工具 → tool_dispatch，否则 execute"""
    if _skill_mgr is not None and _skill_mgr.match_tool(state["task"]):
        return "tool_dispatch"
    return "execute"


def reflect(state: AgentState) -> AgentState:
    """反思器：用主模型，只喂摘要轨迹（省 token）"""
    trace_summary = summarize_trace(state)
    prompt = REFLECTION_PROMPT.format(
        task=state["task"],
        loaded_skills=state.get("loaded_skills", "")[:200],
        trace=trace_summary,
        success=state.get("success"),
        elapsed=state.get("_elapsed", 0),
        retries=state.get("_retries", 0),
    )
    state["reflection"] = llm_client.call(prompt)
    return state
