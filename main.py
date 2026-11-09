#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py —— 自进化 Harness：完整结构自进化入口
====================================
三轴进化，每次运行自动推进：
  1. 知识轴：任务 → 向量检索技能 → LLM 决策（可调用技能工具）→ 真实执行 → 反思 → 技能 upsert
  2. 权重轴：每轮结束按历史相关性自适应奖励权重（memory/reward_weights.json）
  3. 结构轴：每满 EPOCH_ROUNDS 轮触发架构师检查点 —— LLM 提案改图拓扑，
     验证器 + 影子评估（天气回放）+ 门控 + 回滚四层护栏

用法：
  python main.py            # 真实模式（需 LLM_API_KEY + Ollama bge-m3）
  python main.py --mock-llm # 演示模式（无 key 无 Ollama，确定性伪 LLM）
"""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, ".")

# 关键：--mock-llm 必须在导入 core.* 之前置位。
# core.config 在导入时就把 MOCK_LLM 求值成常量，core.llm_client 也在此刻构造单例；
# 放到 main() 里再设环境变量已经晚了（后者还会在无 key 机器上直接抛 OpenAIError）。
if "--mock-llm" in sys.argv:
    os.environ["LLM_MOCK"] = "1"

from core.state import new_state
from core.skill_manager import SkillManager
from core.weights import load_weights, adapt_weights
from core.memory import iter_trajectory, visualize
from core.graph_builder import GraphRuntime, DEFAULT_SPEC, save_spec
from core import executor
from core.architect import run_epoch_checkpoint, show_architecture_status
from core.tracer import (section, trace_replay, show_reflection,
                         show_skill_created, result_line, info)
from core.config import EPOCH_ROUNDS, GRAPH_SPEC_PATH
import mock_api

# 任务套件（固定顺序 —— 影子评估与生产共用同一套件，保证公平）
ROUND_TASKS = [
    "调用可能超时的API并保证成功",
    "调用可能超时的API并保证成功",
    "输出一行问候语",
]

skill_mgr = SkillManager()
executor.set_skill_mgr(skill_mgr)

round_events = {}  # round_id → 该轮 mock 请求结果序列（天气回放数据源）


def run_task(task: str, runtime: GraphRuntime, round_id: int):
    section(f"📋 任务: {task}  (轮次 {round_id}, 图 {runtime.graph_version})")
    st = new_state(task, round_id=round_id, graph_version=runtime.graph_version)
    final = runtime.app.invoke(st)

    # 过程回放：轨迹全文
    trace_replay(final["trace"])

    # 反思/技能生成展示
    if final.get("reflection"):
        show_reflection(final["reflection"])
    if final.get("new_skill"):
        show_skill_created(final["new_skill"], final.get("skill_path", ""),
                           updated=final.get("skill_updated", False))

    # 奖励明细
    section("🎁 奖励评估")
    tag = "降级" if final.get("_degraded") else ("成功" if final["success"] else "失败")
    result_line(f"任务{tag} | 重试 {final.get('_retries', 0)} 次 | 耗时 {final.get('_elapsed', 0)}s"
                + (f" | 工具 {final.get('_tool_used', '')}" if final.get("_tool_used") else ""),
                final["success"])
    for r in final["rewards"]:
        print(f"  {r['step']:<14} raw={r['raw']:+.2f} → weighted={r['weighted']:+.2f} ({r['reason']})")
    print(f"  🏆 总奖励: {final['total_reward']:.2f}")
    return final


def main():
    parser = argparse.ArgumentParser(description="结构自进化 Agent Harness")
    parser.add_argument("--mock-llm", action="store_true",
                        help="演示模式：无 key 无 Ollama，用确定性伪 LLM")
    parser.add_argument("--weather", default="",
                        help="真实会话天气脚本（tools/export_dsh.py 产出的 dsh_weather.json）")
    args = parser.parse_args()
    # LLM_MOCK 已在文件顶部（core.* 导入之前）置位，这里不再设置

    weather_script = []
    if args.weather:
        weather_script = json.loads(Path(args.weather).read_text(encoding="utf-8"))
        if not isinstance(weather_script, list) or not weather_script:
            sys.exit(f"天气脚本为空或格式不对: {args.weather}")

    mock_api.start_server()
    print("🤖 自进化 Harness —— 完整结构自进化版")
    mode = "🎭 演示模式（伪 LLM，无外部依赖）" if args.mock_llm else "🔴 真实模式（DeepSeek + Ollama）"
    print(mode)
    if weather_script:
        print(f"🌧️ 天气回放: {args.weather} ({len(weather_script)} 条真实会话序列)")
    else:
        print("📡 mock API 已启动: http://127.0.0.1:8891 (突发性超时: 过载期持续, 自然恢复)")
    print("🧬 进化三轴: 知识(技能upsert) | 权重(奖励自适应) | 结构(架构师改图)")

    load_weights()
    runtime = GraphRuntime()
    # 从未有 spec 文件时落一份默认 spec（后续架构师基于它迭代）
    if not GRAPH_SPEC_PATH.exists():
        save_spec(DEFAULT_SPEC)

    # 轮次从历史轨迹恢复（旧 schema 行无 round_id，用记录数保证单调累计；
    # 满 EPOCH_ROUNDS 轮触发架构师）
    start_round = len(iter_trajectory()) + 1
    rounds_run = list(range(start_round, start_round + len(ROUND_TASKS)))
    show_architecture_status(runtime)

    # ============ 本轮任务 ============
    for idx, round_id in enumerate(rounds_run):
        if weather_script:
            entry = weather_script[idx % len(weather_script)]
            events = entry["events"] if isinstance(entry, dict) else entry
            mock_api.set_weather(events)
        run_task(task=ROUND_TASKS[round_id - start_round], runtime=runtime, round_id=round_id)
        # 天气增量快照（影子回放源）：只存本轮新增的请求事件，
        # 否则全量快照会让前几轮事件在拼接时重复出现
        prev_total = sum(len(v) for v in round_events.values())
        round_events[round_id] = list(mock_api.request_events[prev_total:])

    # ============ 权重轴：奖励权重自适应 ============
    records = iter_trajectory()
    new_weights = adapt_weights(records)
    section("⚖️ 权重自适应")
    info("当前权重", {k: round(v, 2) for k, v in new_weights.items()})

    # ============ 结构轴：架构师检查点（本轮包含满 EPOCH_ROUNDS 轮则触发）============
    if any(r % EPOCH_ROUNDS == 0 for r in rounds_run):
        print("\n🧹 Curator 技能整理:")
        skill_mgr.curator_run()
        run_epoch_checkpoint(runtime, ROUND_TASKS, round_events)
        show_architecture_status(runtime)

    print("\n📈 可视化:")
    visualize()

    mock_api.set_weather(None)
    mock_api.stop_server()


if __name__ == "__main__":
    main()
