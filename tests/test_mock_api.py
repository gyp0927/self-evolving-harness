"""mock API：突发性超时模型 + 天气回放 + mock-llm 演示模式"""
import time

import mock_api
from core.llm_client import MockLLM


def test_burst_time_based_recovery():
    """过载期按时间恢复：恢复点未到持续超时，到达后恢复"""
    mock_api._burst = 1
    mock_api._recovery_at = time.time() + 0.5  # 0.5s 后才恢复
    assert mock_api._should_timeout() is True
    assert mock_api._should_timeout() is True
    mock_api._recovery_at = time.time() - 0.1  # 已过恢复点
    assert mock_api._should_timeout() is False
    mock_api._burst = 0


def test_burst_random_entry():
    """正常态（无过载）时以 TIMEOUT_RATE 概率进入过载——非恒真/恒假"""
    mock_api._burst = 0
    mock_api._recovery_at = time.time() - 1
    hits = 0
    for _ in range(100):
        if mock_api._should_timeout():
            hits += 1
            # 重置回正常态（模拟"等过载过去"再探测），只统计进入过载的概率
            mock_api._burst = 0
            mock_api._recovery_at = time.time() - 1
    mock_api._burst = 0
    assert 0 < hits < 100


def test_mock_llm_deterministic():
    """伪 LLM：按 prompt 关键词返回确定性回复"""
    m = MockLLM()
    assert "pre_check" in m.call("你是一个 API 调用策略专家")
    assert "失败教训" in m.call("你是Agent进化反思器")
    assert '"ops": []' in m.call("你是Agent架构师")
    assert "resilient-api-call" in m.call("根据反思生成标准 SKILL.md")


def test_mock_llm_embed_similarity():
    """伪向量：相似文本 → 较高相似度；无关文本 → 较低"""
    m = MockLLM()
    a = m.embed("调用可能超时的API并保证成功")
    b = m.embed("调用可能超时的API并保证成功，通过前置检查")
    c = m.embed("输出一行问候语")
    sim_ab = sum(x * y for x, y in zip(a, b))
    sim_ac = sum(x * y for x, y in zip(a, c))
    assert sim_ab > sim_ac
