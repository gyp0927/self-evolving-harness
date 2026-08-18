#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/rewarders.py —— 子评估器（优化版）
奖励全部来自【真实执行结果】：成功率、耗时、重试次数
"""
from core.state import AgentState, add_reward
from core.config import (EFF_TOOL_PENALTY, EFF_STEP_BONUS, EFF_REUSE_BONUS,
                         EFF_STEP_MIN, EFF_STEP_MAX)


def reward_loader(state: AgentState) -> AgentState:
    """加载评估：有相关技能 → 高分"""
    if "(无相关技能)" in state.get("loaded_skills", ""):
        return add_reward(state, "load_skills", 0.0, "无技能可加载")
    return add_reward(state, "load_skills", 0.8, "向量检索到相关技能")


def reward_execute(state: AgentState) -> AgentState:
    """执行评估：基于真实结果（成功 + 耗时 + 重试次数 + 是否降级）"""
    success = state.get("success")
    degraded = state.get("_degraded", False)
    elapsed = state.get("_elapsed", 99)
    retries = state.get("_retries", 0)

    if success:
        r = 2.0
        reasons = ["任务成功"]
        if degraded:
            r -= 1.0; reasons.append("降级兜底-1.0")
        elif retries == 0:
            r += 1.0; reasons.append("一次成功+1.0")
        elif retries <= 2:
            r += 0.5; reasons.append(f"重试{retries}次可控+0.5")
        else:
            reasons.append(f"重试{retries}次偏多")
        if elapsed <= 5:
            r += 0.5; reasons.append(f"耗时{elapsed}s快+0.5")
        return add_reward(state, "execute", r, "; ".join(reasons))
    return add_reward(state, "execute", -2.0, f"任务失败(重试{retries}次全超时)")


def reward_efficiency(state: AgentState) -> AgentState:
    """效率评估：真实重试次数 + 步骤数 + 复用技能 + 技能机制遵循度"""
    trace = state["trace"]
    retries = state.get("_retries", 0)
    steps = len(trace)

    r, reasons = 0.0, []
    r += -EFF_TOOL_PENALTY * retries
    reasons.append(f"重试{retries}次({-EFF_TOOL_PENALTY*retries:+.1f})")
    if steps <= EFF_STEP_MIN:
        r += EFF_STEP_BONUS; reasons.append(f"步骤少+{EFF_STEP_BONUS:.1f}")
    elif steps >= EFF_STEP_MAX:
        r += -EFF_STEP_BONUS; reasons.append(f"步骤多-{EFF_STEP_BONUS:.1f}")
    else:
        reasons.append("步骤适中")
    if "(无相关技能)" not in state.get("loaded_skills", ""):
        r += EFF_REUSE_BONUS; reasons.append(f"复用技能+{EFF_REUSE_BONUS:.1f}")
        # 学以致用度量：策略是否真正采纳了技能教的机制（探测/熔断/降级）
        strat = state.get("_strategy") or {}
        used = [k for k in ("pre_check", "circuit_breaker", "fallback") if strat.get(k)]
        if used:
            r += 0.5; reasons.append(f"遵循技能机制({'+'.join(used)})+0.5")
    return add_reward(state, "efficiency", r, "; ".join(reasons))


def reward_reflect(state: AgentState) -> AgentState:
    """反思评估：规则打分"""
    r_text = state.get("reflection", "")
    score, reasons = 0.0, []
    if "成功经验" in r_text: score += 0.4; reasons.append("有经验")
    if "失败教训" in r_text: score += 0.3; reasons.append("有教训")
    if "可复用流程" in r_text: score += 0.3; reasons.append("有流程")
    if not score: reasons = ["反思空"]
    return add_reward(state, "reflect", score, "+".join(reasons))


def reward_skill(state: AgentState) -> AgentState:
    """Skill 评估：结构完整 + 有步骤"""
    skill = state.get("new_skill", "")
    score, reasons = 0.0, []
    if "## Procedure" in skill or "## Task" in skill:
        score += 0.6; reasons.append("结构")
    if "1." in skill and "2." in skill:
        score += 0.4; reasons.append("步骤")
    if not skill:
        score, reasons = 0.0, ["无Skill"]
    return add_reward(state, "create_skill", score, "+".join(reasons))
