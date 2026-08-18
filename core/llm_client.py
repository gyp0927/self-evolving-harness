#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/llm_client.py —— LLM 封装 + 向量嵌入（优化版）
新增：embed() 用 Ollama bge-m3 做本地向量化（免费、快）
"""
import hashlib
import json
import time
import urllib.request

from langchain_openai import ChatOpenAI
from core.config import (LLM_CONFIG, CACHE_DIR, CACHE_MAX_AGE_HOURS,
                         OLLAMA_URL, EMBED_MODEL)


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


# 全局单例
llm_client = LLMClient()
