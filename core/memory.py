#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/memory.py —— 记忆存储 + RL 日志 + 可视化（结构自进化版）
- 新 schema：round_id/task_id/graph_version/shadow 字段（统计溯源、窗口过滤）
- shadow 分支：影子评估写独立文件（shadow_trajectory/shadow_experiences），零污染生产
- window_stats：按窗口聚合步骤奖励统计（权重自适应与架构师的共同输入）
"""
import json
import time
import numpy as np
from core.state import AgentState, recompute_total
from core.config import (MEMORY_DIR, LOG_DIR, SHADOW_TRAJ_PATH, SHADOW_EXP_PATH)


def _record_of(state: AgentState) -> dict:
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "round_id": state.get("round_id", 0),
        "task_id": state.get("task"),
        "graph_version": state.get("graph_version", "default"),
        "shadow": state.get("_shadow", False),
        "task": state["task"],
        "success": state["success"],
        "degraded": state.get("_degraded", False),
        "total_reward": state["total_reward"],
        "elapsed": state.get("_elapsed", 0),
        "retries": state.get("_retries", 0),
        "tool_used": state.get("_tool_used", ""),
        "rewards": state["rewards"],
        "reflection": state.get("reflection", "")[:200],
    }


def save_memory(state: AgentState) -> AgentState:
    """经验 + RL 轨迹双写（shadow → 独立文件，不污染生产数据）"""
    record = _record_of(state)
    if state.get("_shadow", False):
        with open(SHADOW_TRAJ_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        with open(SHADOW_EXP_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return state
    with open(MEMORY_DIR / "experiences.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    with open(LOG_DIR / "rl_trajectory.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return state


def iter_trajectory(path=None, *, shadow=False):
    """读取轨迹（生产或 shadow 文件），返回记录 list"""
    path = path or (SHADOW_TRAJ_PATH if shadow else LOG_DIR / "rl_trajectory.jsonl")
    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    except FileNotFoundError:
        pass
    return records


def window_records(records: list, window_rounds: int, task_ids=None) -> list:
    """取最近 window_rounds 轮、非 shadow、指定任务（默认全部）的记录"""
    recent = [r for r in records if r.get("round_id") is not None
              and not r.get("shadow", False)]
    if not recent:
        return []
    if task_ids is not None:
        recent = [r for r in recent if r.get("task_id") in task_ids]
    max_round = max(r["round_id"] for r in recent)
    floor = max_round - window_rounds + 1
    return [r for r in recent if r["round_id"] >= floor]


def window_stats(records: list, weights: dict) -> dict:
    """聚合统计（架构师输入 + 门控基线）：
    成功率、降级率、按当前权重重算的总分均值、每步骤 raw 按成功/失败分组的均值"""
    if not records:
        return {"n": 0, "success_rate": 0.0, "degraded_rate": 0.0,
                "avg_total": 0.0, "steps": {}}
    totals = [recompute_total(r, weights) for r in records]
    succ = [r for r in records if r["success"]]
    deg = [r for r in records if r.get("degraded", False)]
    steps = {}
    for r in records:
        for rw in r.get("rewards", []):
            s = rw["step"]
            steps.setdefault(s, {"succ": [], "fail": []})
            (steps[s]["succ"] if r["success"] else steps[s]["fail"]).append(rw["raw"])
    return {
        "n": len(records),
        "success_rate": len(succ) / len(records),
        "degraded_rate": len(deg) / len(records),
        "avg_total": sum(totals) / len(totals),
        "steps": {s: {k: (round(sum(v) / len(v), 3) if v else None)
                      for k, v in g.items()}
                  for s, g in steps.items()},
    }


def visualize():
    """进化历史可视化：奖励曲线 + 各步骤奖励统计"""
    log_path = LOG_DIR / "rl_trajectory.jsonl"
    if not log_path.exists():
        print("暂无日志")
        return
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))

    print(f"\n📈 进化历史可视化({len(records)} 轮):")
    rewards = [r["total_reward"] for r in records]
    print(f"  总奖励曲线: {[f'{x:.1f}' for x in rewards]}")
    print(f"  成功率: {sum(1 for r in records if r['success'])}/{len(records)}")
    if len(rewards) > 1:
        trend = "上升 📈" if rewards[-1] > rewards[0] else "下降/平稳 📉"
        print(f"  趋势: {trend} ({rewards[0]:.1f} → {rewards[-1]:.1f})")

    step_stats = {}
    for r in records:
        for rw in r.get("rewards", []):
            step = rw["step"]
            step_stats.setdefault(step, []).append(rw.get("weighted", rw.get("raw", 0)))
    print("  各步骤平均奖励:")
    for step, vals in step_stats.items():
        print(f"    {step:<14} avg={np.mean(vals):.2f}  (n={len(vals)})")
