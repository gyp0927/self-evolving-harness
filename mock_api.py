#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_api.py —— 真实任务环境：一个会"突发性超时"的本地 API 服务
超时是突发性的（模拟真实故障特征）：正常时 25% 概率超时；
一旦失败，服务进入"过载期"，后续请求持续高概率失败；成功则恢复。
—— 这使"前置探测/熔断/降级"策略有真实价值，而非独立随机的赌运气。
"""
import time
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

TIMEOUT_RATE = 0.25     # 正常时触发超时的概率
BURST_MIN_SEC = 0.5     # 过载期持续的最短秒数
BURST_MAX_SEC = 2.5     # 过载期持续的最长秒数（之后服务按时间自然恢复）
TIMEOUT_SEC = 3.0       # 超时延迟秒数
REQUEST_LOG = []        # 记录每次请求耗时与结果
request_events = []     # 天气回放：生产窗口内每次请求的 ok/超时 结果序列（影子评估按序重放）

_burst = 0              # 是否处于过载期
_recovery_at = 0.0      # 恢复时间戳（时间驱动：即使没有请求，过载也会自动过去）


def _should_timeout() -> bool:
    """突发性超时决策：过载期按时间自动恢复（真实服务特性——即使无人探测也会恢复）。
    这让"探测失败→降级→稍后重试"成为可行策略，而不是永久锁死在降级。"""
    global _burst, _recovery_at
    if _burst == 0:
        if random.random() < TIMEOUT_RATE:
            _burst = 1
            _recovery_at = time.time() + random.uniform(BURST_MIN_SEC, BURST_MAX_SEC)
            return True
        return False
    if time.time() >= _recovery_at:
        _burst = 0
        return False
    return True


def make_replay_oracle(events: list):
    """天气回放 oracle：按序号重放生产窗口记录的请求结果序列。

    影子评估用它替代真实请求——影子图与生产图面对完全相同的"服务天气"，
    且不触碰真实服务（burst 状态零污染）。
    耗时模拟与真实路径一致：失败 = 客户端等满自己的 timeout 才放弃，成功 = 0.05s（奖励公平）。
    """
    seq = list(events)
    idx = [0]

    def oracle(timeout: float) -> bool:
        import time as _t
        if idx[0] < len(seq):
            ok = seq[idx[0]]
            idx[0] += 1
        else:
            ok = True  # 序列耗尽：默认成功（影子轮与生产轮请求数相同，正常不会到这里）
        _t.sleep(timeout if not ok else 0.05)
        return ok

    return oracle


class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        start = time.time()
        if _should_timeout():
            time.sleep(TIMEOUT_SEC)          # 模拟慢响应（调用方会超时）
            result = "timeout"
        else:
            time.sleep(0.05)
            result = "ok"
        elapsed = round(time.time() - start, 2)
        REQUEST_LOG.append({"elapsed": elapsed, "result": result})
        request_events.append(result == "ok")  # 天气回放：记录本次请求结果
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(result.encode())
        except ConnectionError:
            pass  # 客户端超时已断开，写响应失败是预期行为，静默忽略

    def log_message(self, *args):
        pass  # 静默


_server = None
_thread = None


def start_server(port: int = 8891):
    """启动 mock API（幂等）"""
    global _server, _thread
    if _server:
        return port
    _server = HTTPServer(("127.0.0.1", port), MockHandler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    return port


def stop_server():
    global _server
    if _server:
        _server.shutdown()
        _server = None


def reset_log():
    REQUEST_LOG.clear()
