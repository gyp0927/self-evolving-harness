#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/weights.py —— 奖励权重自适应（档2，新增）

原理：RL 的信号质量 = 奖励与结果的因果相关性。
- 统计每个恒现步骤的 raw 在 success/fail 组的均值差 Δ
- |Δ| 大且方向正确 → 该评估有效 → 权重上调；Δ≈0 → 该评估是噪声 → 权重下调
- 只用 raw（不随权重变化），相关性估计与权重当前值无关
- 只对恒现步骤自适应（load_skills/execute/efficiency）：
  reflect/create_skill 的样本几乎全来自失败任务，对其调权重系统性失真，
  它们的去留交给图拓扑进化（职责分工）
"""
import json
import time

from core import config
from core.config import (WEIGHTS_PATH, REWARD_WEIGHTS_DEFAULTS, WEIGHT_MIN,
                         WEIGHT_MAX, WEIGHT_STEP, WEIGHT_MIN_SAMPLES,
                         WEIGHT_MIN_GROUP, WEIGHT_DEADBAND, WEIGHT_STEPS,
                         WEIGHT_WINDOW_ROUNDS)
from core.memory import window_records
from core.state import recompute_total


def load_weights() -> dict:
    """加载运行时权重（缺失/损坏 → 默认值）。同时注入 config（get_weight 生效）"""
    try:
        data = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
        weights = data["weights"]
        # 合并默认值：新 step 有默认，已自适应 step 保持
        weights = {**REWARD_WEIGHTS_DEFAULTS, **weights}
    except Exception:
        weights = dict(REWARD_WEIGHTS_DEFAULTS)
    config.set_runtime_weights(weights)
    return weights


def save_weights(weights: dict):
    WEIGHTS_PATH.write_text(json.dumps({
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weights": weights,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _window_records(records: list, window_rounds: int) -> list:
    """取最近 window_rounds 轮、非 shadow 的记录（复用 memory.window_records），
    只保留窗口内出现过失败样本的判别性任务——永远成功的任务
    （如问候语，降级兜底也算成功）没有任何 RL 判别信号，混入会稀释相关性。"""
    window = window_records(records, window_rounds)
    by_task = {}
    for r in window:
        by_task.setdefault(r.get("task_id"), []).append(r)
    return [r for rows in by_task.values()
            if any(not x["success"] for x in rows) for r in rows]


def adapt_weights(records: list) -> dict:
    """基于轨迹记录自适应权重。返回调整后的权重 dict（并写入文件、注入 config）"""
    weights = load_weights()
    window = _window_records(records, WEIGHT_WINDOW_ROUNDS)
    if not window:
        return weights

    changes = []
    for step in WEIGHT_STEPS:
        rows = []
        for r in window:
            for rw in r.get("rewards", []):
                if rw["step"] == step:
                    rows.append((r["success"], rw["raw"]))
        if len(rows) < WEIGHT_MIN_SAMPLES:
            continue
        succ = [raw for ok, raw in rows if ok]
        fail = [raw for ok, raw in rows if not ok]
        if len(succ) < WEIGHT_MIN_GROUP or len(fail) < WEIGHT_MIN_GROUP:
            continue
        delta = (sum(succ) / len(succ)) - (sum(fail) / len(fail))
        if abs(delta) < WEIGHT_DEADBAND:
            continue
        # 正 Δ：高 raw → 成功，评估有效 → 上调；负 Δ：评估与结果矛盾 → 下调
        old = weights.get(step, REWARD_WEIGHTS_DEFAULTS.get(step, 1.0))
        new = max(WEIGHT_MIN, min(WEIGHT_MAX, old * (1 + WEIGHT_STEP * (1 if delta > 0 else -1))))
        if abs(new - old) > 1e-9:
            changes.append((step, old, new, round(delta, 3)))
            weights[step] = new

    save_weights(weights)
    config.set_runtime_weights(weights)
    return weights
