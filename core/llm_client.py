#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/llm_client.py —— LLM 封装 + 向量嵌入
- 真实模式：OpenAI 兼容端点（DeepSeek 等）+ Ollama bge-m3 嵌入
- Mock 模式（LLM_MOCK=1 或 --mock-llm）：无 key 无 Ollama 也能演示完整流程，
  用确定性伪回复 + 字符 n-gram 伪向量（相似文本 → 相似向量，检索流程可演示）
"""
import hashlib
import json
import math
import time
import urllib.request

from langchain_openai import ChatOpenAI
from core.config import (LLM_CONFIG, CACHE_DIR, CACHE_MAX_AGE_HOURS,
                         OLLAMA_URL, EMBED_MODEL, EMBED_DIM, MOCK_LLM)


class MockLLM:
    """无密钥演示模式：按 prompt 关键词返回确定性回复（零成本、零外部依赖）"""

    STRATEGY = ('{"max_retries": 3, "base_delay": 0.5, "backoff": "exponential", '
                '"pre_check": true, "circuit_breaker": true, "fallback": "cached"}')
    REFLECTION = ("1. 【成功经验】\n"
                  "- 按策略执行并如实记录每次请求结果\n"
                  "2. 【失败教训】\n"
                  "- 探测失败说明服务不可用，应走降级兜底而非浪费重试\n"
                  "- 连续失败达到阈值应立即熔断\n"
                  "3. 【可复用流程】\n"
                  "- 前置探测 → 正式请求 → 退避重试 → 熔断 → 降级兜底\n"
                  "4. 【技能建议】(skill名称)\n"
                  "- resilient-api-call")
    SKILL = ("---\n"
             "name: resilient-api-call\n"
             "description: 调用可能超时的API并保证成功，前置探测+熔断+降级兜底\n"
             "type: agent-created\n"
             "created_at: 2026-01-01\n"
             "state: active\n"
             "action:\n"
             "  max_retries: 3\n"
             "  base_delay: 0.5\n"
             "  backoff: exponential\n"
             "  pre_check: true\n"
             "  circuit_breaker: true\n"
             "  fallback: cached\n"
             "---\n\n"
             "## Task\n\n演示模式生成的技能：调用可能超时的API并保证有结果\n\n"
             "## Procedure\n\n1. 前置探测（短超时）\n2. 正式请求\n3. 退避重试\n4. 熔断\n5. 降级兜底\n\n"
             "## Lessons\n\n- 保证有结果比保证成功更重要\n- 熔断避免对已过载服务加压\n")

    def call(self, prompt: str, use_cache: bool = True) -> str:
        if "架构师" in prompt:
            return '{"ops": []}'  # 演示模式不做图进化（noop）
        if "反思器" in prompt:
            return self.REFLECTION
        if "策略专家" in prompt:
            return self.STRATEGY
        if "SKILL.md" in prompt:
            return self.SKILL
        return "好的。"

    def embed(self, text: str) -> list:
        """字符 2-gram 伪向量：相似文本 → 相似向量（bge-m3 的廉价近似，仅演示用）"""
        vec = [0.0] * EMBED_DIM
        s = text.replace(" ", "").lower()
        for i in range(max(0, len(s) - 1)):
            h = int(hashlib.md5(s[i:i + 2].encode()).hexdigest()[:8], 16)
            vec[h % EMBED_DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


class LLMClient:
    def __init__(self):
        self.main_llm = ChatOpenAI(
            base_url=LLM_CONFIG["base_url"], api_key=LLM_CONFIG["api_key"],
            model=LLM_CONFIG["model"], temperature=0.3)

    def call(self, prompt: str, use_cache: bool = True) -> str:
        """统一调用主模型 + 可选缓存"""
        if use_cache:
            cached = self._read_cache(prompt)
            if cached is not None:
                return cached
        result = self.main_llm.invoke(prompt).content
        if use_cache:
            self._write_cache(prompt, result)
        return result

    # ---------- 向量嵌入（Ollama bge-m3，本地免费）----------
    def embed(self, text: str) -> list:
        """文本 → 1024 维向量（Ollama /api/embed）。
        Ollama 不可用时给出可操作的修复提示（而不是裸连接错误堆栈）。"""
        body = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise RuntimeError(
                f"无法连接 Ollama ({OLLAMA_URL}): {e}\n"
                f"请先启动 Ollama（ollama serve 或打开 Ollama 应用），"
                f"并确保已拉取模型: ollama pull {EMBED_MODEL}"
            ) from e
        return data["embeddings"][0]

    # ---------- 缓存 ----------
    def _cache_key(self, prompt: str) -> str:
        return hashlib.md5(prompt.encode()).hexdigest()

    def _read_cache(self, prompt: str):
        path = CACHE_DIR / f"{self._cache_key(prompt)}.txt"
        if path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600
            if age_h < CACHE_MAX_AGE_HOURS:
                return path.read_text(encoding="utf-8")
        return None

    def _write_cache(self, prompt: str, result: str):
        path = CACHE_DIR / f"{self._cache_key(prompt)}.txt"
        path.write_text(result, encoding="utf-8")


class _LazyLLMClient:
    """惰性单例：导入时不构造任何真实客户端。

    这样：
    - 没有 API key 的机器也能 import 本项目（测试/文档生成不再在导入期抛 OpenAIError）；
    - --mock-llm 只要在首次调用前置位 LLM_MOCK 就生效；
    - 真正的凭证错误推迟到第一次调用，报错信息仍指向使用处。
    """

    def __init__(self):
        self._impl = None

    def _get(self):
        if self._impl is None:
            self._impl = MockLLM() if MOCK_LLM else LLMClient()
        return self._impl

    def call(self, prompt: str, use_cache: bool = True) -> str:
        return self._get().call(prompt, use_cache=use_cache)

    def embed(self, text: str) -> list:
        return self._get().embed(text)


# 全局单例（惰性：首次调用时才按配置选择真实/演示模式）
llm_client = _LazyLLMClient()
