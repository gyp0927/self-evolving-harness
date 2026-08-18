#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/tracer.py —— 过程可视化（新增）
让"自进化"每一步都看得见：
- 技能检索过程（每个技能的相似度分数）
- LLM 策略生成的原始输出
- 轨迹逐步回放（thought/action/result 全文）
- 反思与技能生成的完整内容
"""
import sys
from contextlib import contextmanager

# ANSI 颜色（Windows 终端也支持）
C = {
    "cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m",
    "red": "\033[91m", "blue": "\033[94m", "magenta": "\033[95m",
    "dim": "\033[90m", "bold": "\033[1m", "end": "\033[0m",
}

ENABLED = True


def set_enabled(flag: bool):
    """全局静默开关（影子评估期间关闭展示，避免刷屏）"""
    global ENABLED
    ENABLED = flag


@contextmanager
def silent():
    set_enabled(False)
    try:
        yield
    finally:
        set_enabled(True)


def _c(color: str, text: str) -> str:
    if not ENABLED or not sys.stdout.isatty():
        return text
    return f"{C.get(color, '')}{text}{C['end']}"


def section(title: str, char: str = "═"):
    if not ENABLED:
        return
    print()
    print(_c("cyan", char * 62))
    print(_c("bold", f"  {title}"))
    print(_c("cyan", char * 62))


def step(msg: str, color: str = "dim"):
    if not ENABLED:
        return
    print(_c(color, f"  ▸ {msg}"))


def info(label: str, value: str, color: str = "yellow"):
    if not ENABLED:
        return
    print(f"  {_c('bold', label)}: {_c(color, value)}")


def result_line(msg: str, ok: bool):
    if not ENABLED:
        return
    mark = "✅" if ok else "❌"
    color = "green" if ok else "red"
    print(_c(color, f"  {mark} {msg}"))


def trace_replay(trace: list):
    """逐步回放执行轨迹（全文）"""
    if not ENABLED:
        return
    section("📜 执行轨迹回放")
    icons = {"plan": "🧠", "tool_call": "🔧", "wait": "⏳", "finish": "🏁"}
    for i, t in enumerate(trace, 1):
        icon = icons.get(t["step"], "•")
        st = t["step"]
        th = t.get("thought", "")
        ac = t.get("action", "")
        rs = t.get("result", "")
        print(f"  {_c('bold', f'{icon} 步骤{i} [{st}]')}")
        print(f"    {_c('cyan', '想法')}: {th}")
        print(f"    {_c('blue', '动作')}: {ac}")
        print(f"    {_c('green', '结果')}: {rs}")
        print()


def show_retrieval(scores: list, threshold: float):
    """展示技能检索过程：每个候选技能的相似度"""
    if not ENABLED:
        return
    section("🔍 技能检索（bge-m3 向量相似度）")
    if not scores:
        print(_c("yellow", "  ⚠️ 技能库为空 —— 这是第一次运行，还没有可复用的经验"))
        return
    for name, score in sorted(scores, key=lambda x: x[1], reverse=True):
        hit = "✅ 命中" if score >= threshold else "  未达阈值"
        color = "green" if score >= threshold else "dim"
        print(f"  {_c(color, f'{name:<40} 相似度={score:.3f}  {hit}')}")
    print()


def show_strategy(raw: str, parsed: dict):
    """展示 LLM 生成的重试策略"""
    if not ENABLED:
        return
    section("🧠 LLM 重试策略生成")
    print(_c("dim", f"  LLM 原始输出: {raw.strip()[:200]}"))
    print()
    print(f"  {_c('bold', '解析结果')}:")
    for k, v in parsed.items():
        print(f"    {k}: {_c('yellow', v)}")
    print()


def show_execution(attempts: list, success: bool, elapsed: float,
                   degraded: bool = False, fallback: str = None, note: str = None):
    """展示真实执行过程：每次请求的结果（支持前置探测/降级兜底）"""
    if not ENABLED:
        return
    section("⚡ 真实执行（mock API: 突发性超时）")
    for i, a in enumerate(attempts, 1):
        if a.get("probe"):
            print(f"  🔍 前置探测: {_c('red', '⏱️ 超时')}")
            continue
        if a["ok"]:
            print(f"  {_c('green', f'✅ 第{i}次请求成功')}  (耗时 {a['t']:.2f}s)")
        else:
            delay = a.get("delay")
            d = f" → 退避等待 {delay:.1f}s" if delay else ""
            print(f"  {_c('red', f'❌ 第{i}次请求超时')}{d}")
    if note:
        print(f"  {_c('yellow', f'  ⚠️ {note}')}")
    if degraded:
        print(f"  {_c('yellow', f'  🟡 降级兜底: fallback={fallback}')}")
    final = "任务成功(降级) 🟡" if degraded else ("任务成功 ✅" if success else "任务失败 ❌")
    color = "yellow" if degraded else ("green" if success else "red")
    print(f"  {_c('bold', '最终结果')}: {_c(color, final)}  |  总耗时 {_c('yellow', f'{elapsed}s')}")
    print()


def show_reflection(text: str):
    """展示完整反思"""
    if not ENABLED:
        return
    section("🧠 反思（进化时刻）")
    for line in text.strip().splitlines():
        if line.strip():
            print(f"  {line.strip()}")
    print()


def show_skill_created(content: str, path: str, updated: bool = False):
    """展示沉淀的技能（新建或更新）"""
    if not ENABLED:
        return
    section("📦 技能沉淀")
    action = "🔄 更新已有技能（知识收敛）" if updated else "🆕 新建技能"
    print(f"  {_c('bold', action)}")
    print(f"  {_c('bold', '保存位置')}: {path}")
    print(f"  {_c('bold', '技能内容')}:")
    for line in content.strip().splitlines():
        print(f"  {_c('dim', line)}")
    print()


def show_tool_dispatch(tool: dict):
    """展示技能工具调度（档1：技能作为可调用插件被执行）"""
    if not ENABLED:
        return
    section("🔧 技能工具调度")
    print(f"  {_c('bold', '调用技能')}: {_c('yellow', tool['name'])}")
    print(f"  {_c('bold', 'action')}: {tool['action']}")
    print()
