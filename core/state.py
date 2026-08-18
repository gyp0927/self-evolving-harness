#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/state.py —— 状态管理（结构自进化版）
新增：_shadow（影子评估）、round_id/graph_version（日志溯源）、_oracle（天气回放注入）
"""
from typing import TypedDict, Annotated, List, Dict
from core.config import get_weight


class AgentState(TypedDict):
    task: str
    trace: Annotated[List[Dict], "trace"]
    result: str
    success: bool
    reflection: str
    new_skill: str
    loaded_skills: str
    rewards: Annotated[List[Dict], "rewards"]
    total_reward: float
    _elapsed: float
    _retries: int
    _strategy: Dict          # LLM 生成的完整策略（含 pre_check/circuit_breaker/fallback）
    _degraded: bool          # 是否降级兜底返回（"保证有结果"而非"重试成功"）
    _shadow: bool            # 影子评估模式（只算不写：日志/技能/元数据全部隔离）
    _tool_used: str          # 本次任务调用的技能工具名（未用为空）
    _oracle: object          # API 可注入接口（影子轮 = 天气回放 oracle）
    round_id: int            # 运行轮次（日志溯源）
    graph_version: str       # 图版本（"default" 或 "g<epoch>-<id>"）
    skill_path: str          # 本次技能沉淀的文件路径
    skill_updated: bool      # 技能是更新已有(True)还是新建(False)


def new_state(task: str, *, shadow: bool = False, round_id: int = 0,
              graph_version: str = "default") -> AgentState:
    return {
        "task": task, "trace": [], "result": "", "success": True,
        "reflection": "", "new_skill": "", "loaded_skills": "",
        "rewards": [], "total_reward": 0.0, "_elapsed": 0.0, "_retries": 0,
        "_strategy": {}, "_degraded": False, "_shadow": shadow,
        "_tool_used": "", "_oracle": None,
        "round_id": round_id, "graph_version": graph_version,
        "skill_path": "", "skill_updated": False,
    }


def record_step(state: AgentState, step: str, thought: str, action: str, result: str):
    """追加一步轨迹"""
    state["trace"] = state.get("trace", []) + [{
        "step": step, "thought": thought, "action": action, "result": result,
    }]
    return state


def add_reward(state: AgentState, step: str, reward: float, reason: str):
    """记录一步奖励(带运行时权重)"""
    weighted = reward * get_weight(step)
    state["rewards"] = state.get("rewards", []) + [{
        "step": step, "raw": reward, "weighted": weighted, "reason": reason,
    }]
    state["total_reward"] = state.get("total_reward", 0.0) + weighted
    return state


def recompute_total(record: dict, weights: dict) -> float:
    """用指定权重从 raw 重算总奖励（权重变更后历史 total_reward 不可比，必须重算）"""
    total = 0.0
    for rw in record.get("rewards", []):
        total += rw.get("raw", 0.0) * weights.get(rw["step"], 1.0)
    return total


def summarize_trace(state: AgentState, max_len: int = 50) -> str:
    """轨迹摘要:只留关键信息(省 token)"""
    lines = []
    for t in state["trace"]:
        lines.append(f"[{t['step']}] {t['thought'][:max_len]} → {t['result'][:max_len]}")
    return "\n".join(lines)
