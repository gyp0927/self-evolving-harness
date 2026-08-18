"""影子评估：零污染、静默、天气回放公平性"""
import io
import contextlib
from copy import deepcopy
from pathlib import Path
from unittest import mock

from core.architect import run_shadow_eval
from core.graph_builder import DEFAULT_SPEC


def _snapshot(root: Path):
    out = {}
    for d in ["skills", "memory", "rl_logs"]:
        p = root / d
        out[d] = {}
        for f in p.glob("*"):
            if "shadow" in f.name:
                continue  # 影子文件是预期写入
            out[d][f.name] = f.stat().st_size
    return out


def test_shadow_zero_pollution(tmp_path, monkeypatch):
    """影子评估不触碰生产数据（skills/memory/rl_logs）"""
    for d in ["skills", "memory", "rl_logs", "cache"]:
        (tmp_path / d).mkdir()
    from core import config
    monkeypatch.setattr(config, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "rl_logs")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "GRAPH_SPEC_PATH", tmp_path / "graph_spec.json")
    # 模拟已存在的生产数据
    (tmp_path / "rl_logs/rl_trajectory.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "memory/experiences.jsonl").write_text("", encoding="utf-8")

    spec = deepcopy(DEFAULT_SPEC)
    events = [True, True, False, True, True, False]
    strategy = ('{"max_retries": 3, "pre_check": true, "fallback": "cached"}')

    before = _snapshot(tmp_path)
    with mock.patch("core.executor.llm_client.call", return_value=strategy):
        results = run_shadow_eval(spec, ["调用可能超时的API并保证成功",
                                         "输出一行问候语"], events, rounds=2)
    after = _snapshot(tmp_path)
    assert before == after, f"生产数据被污染: {before} != {after}"
    assert len(results) == 4


def test_shadow_silent(tmp_path, monkeypatch):
    """影子评估期间展示输出被静默（不刷屏）"""
    for d in ["skills", "memory", "rl_logs", "cache"]:
        (tmp_path / d).mkdir()
    from core import config
    monkeypatch.setattr(config, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "rl_logs")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "GRAPH_SPEC_PATH", tmp_path / "graph_spec.json")

    spec = deepcopy(DEFAULT_SPEC)
    strategy = '{"max_retries": 3, "pre_check": true, "fallback": "cached"}'
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
         mock.patch("core.executor.llm_client.call", return_value=strategy):
        run_shadow_eval(spec, ["调用可能超时的API并保证成功"], [True, True], rounds=1)
    out = buf.getvalue()
    assert "LLM 重试策略生成" not in out
    assert "⚡ 真实执行" not in out


def test_replay_oracle_shared_cursor(tmp_path):
    """天气回放必须共享游标：多任务顺序消费事件序列，而非各自从头重放"""
    import mock_api
    oracle = mock_api.make_replay_oracle([True, False, True])
    assert oracle(1.0) is True    # 第 1 次：True
    assert oracle(1.0) is False   # 第 2 次：False
    assert oracle(1.0) is True    # 第 3 次：True
    assert oracle(1.0) is True    # 序列耗尽：默认 True


def test_replay_oracle_does_not_touch_burst():
    import mock_api
    before_burst = mock_api._burst
    oracle = mock_api.make_replay_oracle([True, False])
    oracle(1.0)
    oracle(1.0)
    assert mock_api._burst == before_burst  # 回放不触碰真实服务状态
