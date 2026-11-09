"""门控防噪：bootstrap 胜率 + 天气回放耗尽策略"""
import pytest

from core.architect import _bootstrap_win_prob, _gate


def _shadow(vals):
    return [{"success": True, "total_reward": v} for v in vals]


def test_gate_accepts_clear_win():
    """明显提升：全部 12 分 vs 生产 4 分 → 通过"""
    shadow = _shadow([12.0] * 6)
    prod = {"success_rate": 1.0, "avg_total": 4.0, "n": 6}
    ok, msg = _gate(shadow, prod, [4.0] * 6)
    assert ok, msg
    assert _bootstrap_win_prob(shadow, [4.0] * 6) == 1.0


def test_gate_rejects_noisy_mean():
    """点估计够阈值但胜率不足：均值 5.5 vs 5.5 → 拒绝"""
    shadow = _shadow([10.0, 1.0, 10.0, 1.0, 10.0, 1.0])
    prod = {"success_rate": 1.0, "avg_total": 4.0, "n": 6}
    ok, msg = _gate(shadow, prod, [5.5] * 6)
    assert not ok
    assert "胜率" in msg


def test_gate_keeps_success_rate_floor():
    """成功率下降即使分数更高也拒绝"""
    shadow = [{"success": False, "total_reward": 20.0},
              {"success": True, "total_reward": 20.0}]
    prod = {"success_rate": 1.0, "avg_total": 4.0, "n": 2}
    ok, msg = _gate(shadow, prod, [4.0, 4.0])
    assert not ok
    assert "成功率" in msg


def test_replay_oracle_sticky_on_exhaustion():
    """序列耗尽后保持最后一个天气，而不是白送成功"""
    import mock_api
    oracle = mock_api.make_replay_oracle([True, False])
    assert oracle(0.01) is True
    assert oracle(0.01) is False
    assert oracle(0.01) is False
    assert oracle(0.01) is False
