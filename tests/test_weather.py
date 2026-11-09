"""真实会话天气回放：脚本驱动 mock API 的成败序列"""
import mock_api


def setup_function():
    mock_api.set_weather(None)


def test_weather_script_replays_in_order():
    mock_api.set_weather([True, False, True])
    assert mock_api._should_timeout() is False
    assert mock_api._should_timeout() is True
    assert mock_api._should_timeout() is False


def test_weather_sticks_to_last_event():
    mock_api.set_weather([False, True])
    assert mock_api._should_timeout() is True     # 第一条：失败
    assert mock_api._should_timeout() is False    # 第二条：成功
    # 序列耗尽后粘住最后一个天气（True → 不超时）
    assert mock_api._should_timeout() is False


def test_clear_weather_restores_burst_model(monkeypatch):
    mock_api.set_weather([False])
    monkeypatch.setattr(mock_api.random, "random", lambda: 0.99)  # 正常期不超时
    mock_api.set_weather(None)
    assert mock_api._should_timeout() is False
