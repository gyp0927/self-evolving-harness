"""奖励权重自适应：相关性方向、死区、样本门槛、clamp、判别性任务过滤"""
import shutil
from pathlib import Path

import pytest

from core import config
from core import weights as W


@pytest.fixture
def iso_weights(tmp_path):
    """隔离的权重文件（不碰生产 memory/）"""
    orig = config.WEIGHTS_PATH
    config.WEIGHTS_PATH = tmp_path / "rw.json"
    W.save_weights({"load_skills": 1.0, "execute": 3.0, "efficiency": 1.5,
                    "reflect": 1.0, "create_skill": 1.0})
    yield
    config.WEIGHTS_PATH = orig
    config._runtime_weights = None


def _recs(n_succ, n_fail, task="API任务", step="execute",
          raw_succ=3.5, raw_fail=-2.0, round_id=1):
    out = []
    for i in range(n_succ):
        out.append({"round_id": round_id, "shadow": False, "success": True,
                    "task_id": task, "rewards": [{"step": step, "raw": raw_succ}]})
    for i in range(n_fail):
        out.append({"round_id": round_id, "shadow": False, "success": False,
                    "task_id": task, "rewards": [{"step": step, "raw": raw_fail}]})
    return out


def test_positive_correlation_ups_weight(iso_weights):
    """execute 成功时高 raw → 正相关 → 权重上调"""
    w = W.adapt_weights(_recs(10, 5))
    assert w["execute"] > 3.0


def test_negative_correlation_downs_weight(iso_weights):
    """efficiency 失败时反而高 raw → 与结果矛盾 → 下调"""
    recs = _recs(10, 5, step="efficiency", raw_succ=-1.0, raw_fail=1.0)
    w = W.adapt_weights(recs)
    assert w["efficiency"] < 1.5


def test_deadband_no_change(iso_weights):
    """|Δ| < 死区 → 不动"""
    recs = _recs(8, 7, raw_succ=2.1, raw_fail=2.0)
    w = W.adapt_weights(recs)
    assert w["execute"] == 3.0


def test_sample_size_gate(iso_weights):
    """样本不足 → 不动"""
    w = W.adapt_weights(_recs(3, 2))
    assert w["execute"] == 3.0


def test_clamp_bounds(iso_weights):
    """下限 clamp：0.19 负相关 → 0.2；上限 clamp：3.9 正相关 → 4.0"""
    recs = _recs(10, 5, step="efficiency", raw_succ=-1.0, raw_fail=1.0)
    W.save_weights({"efficiency": 0.19})
    assert abs(W.adapt_weights(recs)["efficiency"] - 0.2) < 1e-9
    recs_up = _recs(10, 5, step="execute")
    W.save_weights({"execute": 3.9})
    assert abs(W.adapt_weights(recs_up)["execute"] - 4.0) < 1e-9


def test_discriminative_task_filtering(iso_weights):
    """永远成功的任务（如问候语）不产生 RL 信号 → 被过滤，权重不动"""
    recs = _recs(10, 5)  # 判别性任务：有失败
    recs += [{"round_id": 1, "shadow": False, "success": True,
              "task_id": "问候语", "rewards": [{"step": "execute", "raw": 3.5}]}
             for _ in range(8)]
    w = W.adapt_weights(recs)
    assert w["execute"] > 3.0  # 仍由判别性任务驱动上调


def test_all_success_window_no_change(iso_weights):
    """窗口内全是成功任务（无判别性）→ 空窗口 → 权重不动"""
    recs = [{"round_id": 1, "shadow": False, "success": True,
             "task_id": "x", "rewards": [{"step": "execute", "raw": 1.0}]}
            for _ in range(15)]
    w = W.adapt_weights(recs)
    assert w["execute"] == 3.0


def test_recompute_total_consistent():
    rec = {"rewards": [{"step": "execute", "raw": 3.5},
                       {"step": "efficiency", "raw": 1.0}]}
    total = W.recompute_total(rec, {"execute": 3.0, "efficiency": 1.5})
    assert abs(total - (3.5 * 3.0 + 1.0 * 1.5)) < 1e-9
