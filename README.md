# 🧬 Harness — 结构自进化 Agent

![Python](https://img.shields.io/badge/Python-3.12-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/tests-46%20passed-brightgreen)
![Architecture](https://img.shields.io/badge/架构-StructureSelfEvolving-purple)

> 一个会**自己改自己**的 Agent Harness：技能自己长出来、奖励权重自己调、图的拓扑被架构师 LLM 自己改写——全程不训练模型权重，纯外部结构进化。

## 一句话定位

大多数 Agent 框架的"进化"发生在生态里（人写插件、人调参）；本项目的进化发生在**系统内部**：执行 → 真实奖励 → 反思 → 技能/权重/图三轴同步进化，每一步都有验证器与审计日志兜底。

## ✨ 核心特性

### 三轴进化（每次运行自动推进）

| 轴 | 机制 | 持久化 |
|---|---|---|
| **知识轴** | 失败 → LLM 反思 → 技能生成；与已有技能高度相似时 **upsert 更新**（版本递增、教训去重）而非新建 | `skills/*.md` |
| **技能工具化** | 技能 frontmatter 带 `action` 字段 → 注册为**可调用工具**，LLM 通过 function-call 式决策调用，技能库增删 = 动作集增删 | `skills/` + `skill_meta.json` |
| **权重轴** | 统计每个奖励步骤 raw 与任务成功的相关性（Δ），死区/样本门槛/clamp 后 ±10% 调整 | `memory/reward_weights.json` |
| **结构轴** | 每 N 轮触发**架构师检查点**：LLM 审视进化历史 → 提案改图拓扑（增删节点/改条件路由）→ 影子评估 → 门控 → 切换/回滚 | `memory/graph_spec.json` |

### 四层安全护栏（结构进化的安全网）

1. **图验证器**（10 条规则）：注册表白名单、保护节点、无环、可达性、终止性、条件标签覆盖、规模上限——LLM 生成的 spec 永远无法引用注册表外的函数
2. **影子评估**：候选图在**天气回放**（生产窗口真实请求序列）下跑 K 轮，只算不写——与生产面对完全相同环境，零污染
3. **门控**：影子成功率 ≥ 生产，且重算总分 ≥ 生产 + 阈值，才切换
4. **回滚**：切换前备份快照；后续劣化自动恢复，回滚后冷却期防振荡

### 真实任务环境

- mock API 模拟**突发性超时**（过载期按时间自然恢复——真实服务的故障特征）
- 执行循环支持：前置探测 / 指数退避重试 / **熔断器** / **降级兜底**（"保证有结果"而非"永远重试成功"）
- 全部真实执行：成功/耗时/重试/降级 = 奖励依据，无模拟打分

## 🏗️ 架构

```
                       ┌────────────────────────────────────────────┐
   任务 ──▶ load_skills │  bge-m3 向量检索 → 技能注入 + 工具注册表      │
                       └───────────────┬────────────────────────────┘
                                       ▼
                       ┌────────────────────────────────────────────┐
                       │  route_execution (g1 起为条件路由)           │
                       │  ├─ 有匹配技能工具 → tool_dispatch（确定性执行 action）│
                       │  └─ 否则 → execute（LLM 决策策略 JSON）      │
                       └───────────────┬────────────────────────────┘
                                       ▼
                       ┌────────────────────────────────────────────┐
                       │  真实执行：探测 → 重试(熔断) → 降级兜底        │
                       │  奖励评估：execute / efficiency / ...        │
                       └───────────────┬────────────────────────────┘
                                       ▼
                       ┌────────────────────────────────────────────┐
                       │  失败? ──▶ 反思 ──▶ 技能 upsert（知识轴）     │
                       │  成功? ──▶ save_memory（轨迹入库）            │
                       └────────────────────────────────────────────┘
                                       ▼
        ┌──────────────── 每 EPOCH_ROUNDS 轮 ────────────────┐
        ▼                                                    ▼
  ⚖️ 权重自适应（权重轴）                          🧠 架构师检查点（结构轴）
                                         提案 → 验证器 → 影子评估 → 门控 → 切换/回滚
                                         全部决策写入 memory/evolution_log.jsonl
```

## 🚀 快速开始

```bash
# 1. 创建环境并安装依赖（uv 或 pip 均可）
uv sync
# 或
pip install -r requirements.txt

# 2. 配置 LLM
#    方式一（推荐）：环境变量
#    set LLM_API_KEY=sk-xxx            （或 DEEPSEEK_API_KEY）
#    set LLM_BASE_URL=https://api.deepseek.com
#    方式二：本机 Hermes 的 .env（HERMES_HOME 指向其目录时自动读取）
#    set HERMES_HOME=C:\path\to\hermes

# 3. 向量检索依赖本地 Ollama + bge-m3（免费）
#    ollama pull bge-m3
#    （OLLAMA_URL 默认 http://127.0.0.1:11434）

# 4. 运行
python main.py             # 真实模式（需 LLM_API_KEY + Ollama bge-m3）
python main.py --mock-llm  # 演示模式：无 key、无 Ollama 也能跑完整流程（伪 LLM + 伪向量）

# 5. 运行测试
pip install -r requirements-dev.txt   # 或 uv pip install pytest
python -m pytest tests/ -q            # 46 项测试：验证器/权重/影子评估/操作原子性/mock 模型/门控防噪
```

> ⚠️ **密钥安全**：项目不读取、不存储任何写死在代码里的密钥。key 只通过环境变量注入，`.env*` 已被 `.gitignore` 排除。提交代码前请确保无 `.env` 文件被追踪。

## 📁 目录结构

```
self-evolving-harness/
├── main.py                  # 入口：任务套件 + 三轴进化编排
├── mock_api.py              # 真实任务环境（突发性超时模拟 + 天气回放）
├── core/
│   ├── graph_builder.py     # 图即数据：节点/策略注册表、验证器、GraphRuntime
│   ├── architect.py         # 架构师：提案→验证→影子评估→门控→切换/回滚
│   ├── executor.py          # 执行循环（探测/熔断/降级）+ 技能工具决策
│   ├── skill_manager.py     # 技能生命周期：检索/生成/upsert/工具注册/curator
│   ├── weights.py           # 奖励权重自适应
│   ├── rewarders.py         # 子评估器（真实执行结果驱动）
│   ├── memory.py            # 轨迹存储（生产/shadow 隔离）+ 统计
│   ├── state.py             # AgentState（含影子/轮次/图版本溯源）
│   ├── evolution_log.py     # 进化审计日志 + 图快照（回滚源）
│   ├── llm_client.py        # LLM 封装 + bge-m3 嵌入 + prompt 缓存
│   ├── config.py            # 配置（key 只来自环境变量）
│   └── tracer.py            # 过程可视化（影子期间静默）
├── skills/                  # 技能库（Agent 自生长，可注册为工具）
└── memory/                  # 运行数据：轨迹/权重/图spec/进化日志
```

## 🧪 真实进化记录（示例）

`memory/evolution_log.jsonl` 记录了架构师的全部决策，可审计：

```
[propose]  add_node tool_dispatch + 条件路由 + 删直边          （缺出边）
[rejected] 验证失败: 无法到达 END: ['tool_dispatch']           ← 护栏拦截
[propose]  add_node tool_dispatch + 条件路由 + 出边 + 删直边    （从拒绝中学习）
[shadow]   影子评估通过                                        ← 天气回放公平对比
[switch]   g1 已切换，基线 11.72                               ← 图结构进化为 g1
[propose]  set_policy + remove_node（精简反思链）               ← 继续探索新方向
[rejected] 验证失败 + fix 建议自动生成                         ← 修复指引喂回架构师
[switch]   g2 已切换（反思分支整体下线，基线 13.38）             ← 架构师完整提案成功
[rollback] 平均 8.55 < 基线 13.38 - 2.0 → 自动回滚             ← 劣化检测兜底生效
```

## 🖥️ 运行效果（--mock-llm 演示模式节选）

```
🧬 进化三轴: 知识(技能upsert) | 权重(奖励自适应) | 结构(架构师改图)

  🗺️ 当前图结构
  ▸ 版本: g2
  ▸ 入口: load_skills  节点: 7  边: 7
  ▸   load_skills: →r_load
  ▸   r_load: →tool_dispatch[route_execution]
  ▸   execute: →r_exec
  ▸   r_exec: →r_eff
  ▸   r_eff: →END[should_deep_reflect]
  ▸   save_memory: →END
  ▸   tool_dispatch: →r_exec
  ▸ 保护节点: execute, load_skills, save_memory

  📋 任务: 调用可能超时的API并保证成功  (轮次 43, 图 g2)
  🔍 技能检索（bge-m3 向量相似度）
  🔧 技能工具调度          ← 确定性分发：调用技能 resilient-api-call-v2
  ❌ 第1次请求超时 → 退避等待 0.5s
  ❌ 第2次请求超时 → 熔断器打开 → 降级兜底 fallback=cached

  🎁 奖励评估
  ✅ 任务降级 | 重试 2 次 | 耗时 2.56s | 工具 resilient-api-call-v2
  load_skills    raw=+0.80 → weighted=+0.80 (向量检索到相关技能)
  execute        raw=+1.50 → weighted=+6.00 (任务成功; 降级兜底-1.0; 耗时2.56s快+0.5)
  efficiency     raw=-0.50 → weighted=-0.75 (重试2次(-1.0); 步骤多-1.0; 复用技能+1.0; 遵循技能机制+0.5)
  🏆 总奖励: 6.05

  🧠 架构师检查点（结构自进化）
  回滚: 图已回滚: 平均 8.55 低于切换前基线 13.38   ← 劣化自动恢复
```

## 🛠️ 技能工具示例（技能 = 可调用插件）

技能文件 `skills/*.md` 的 frontmatter 带 `action` 字段即注册为工具，LLM 可调用、`tool_dispatch` 节点可确定性执行：

```yaml
---
name: resilient-api-call-v2
description: 调用可能超时的API并保证成功，通过前置检查、动态重试、熔断降级确保始终有可用结果
type: agent-created
created_at: 2026-08-18
state: active
version: 2
action:                    # ← 可执行配置：注册为工具
  max_retries: 3
  base_delay: 0.5
  backoff: exponential
  pre_check: true          # 前置探测：失败直接降级，不浪费重试
  circuit_breaker: true    # 熔断：连续失败 2 次停止重试
  fallback: cached         # 兜底：保证有结果
---
```

- **无 `action` 字段** → 仅作为参考知识注入 prompt（兼容旧技能）
- 技能库增删/更新 → 工具集随之变化（**动作集 = 结构的一部分**）
- 反思生成的技能若与已有技能高度相似 → **upsert 更新**（版本递增、教训去重），知识收敛不膨胀

## 📥 接入真实会话（可选）

`tools/export_dsh.py` 把 DeepSeek Harness 的会话转录（`~/.dsh/sessions/**/session.v4.jsonl.zstd`）导出成本项目可用的数据集：

```bash
python tools/export_dsh.py              # 全量，输出到 build/
python tools/export_dsh.py --limit 6    # 只取最近 6 个会话
```

产物：

- `build/dsh_turns.jsonl`：每轮任务（任务文本 / 工具调用 / token / 耗时 / 是否成功 / LLM 重试）
- `build/dsh_events.json`：`{轮次: [每次尝试的 ok 序列]}`，可直接作为天气回放输入
- `build/dsh_weather.json`：按轮次排列的天气脚本（`{task, events, success, duration_ms}`）
- `build/dsh_summary.json`：工具分布、错误率、token 总量、重试次数

用真实会话的失败序列当天气跑进化（不再用突发超时模型）：

```bash
python main.py --mock-llm --weather build/dsh_weather.json
```

依赖 `zstandard` 读取压缩转录：`pip install zstandard`。只读本地文件，不联网。

## ❓ FAQ

**Q: 没有 API key 能跑吗？**
A: 能。`python main.py --mock-llm` 用确定性伪 LLM + 伪向量跑完整流程（三轴进化、护栏、可视化全部可见），零外部依赖。

**Q: Ollama 没装 / bge-m3 没拉？**
A: 向量检索依赖本地 Ollama + bge-m3：`ollama pull bge-m3`。未启动时程序会给出可操作提示（不会裸崩）。mock 模式不需要 Ollama。

**Q: 首次运行生成了什么？**
A: `memory/` 自动创建：`graph_spec.json`（默认图）、`reward_weights.json`（默认权重）、`rl_trajectory.jsonl`（轨迹）、`evolution_log.jsonl`（进化审计）。技能库为空时第一次失败会反思生成第一个技能。

**Q: 密钥安全吗？**
A: key 只来自项目根目录 `.env`（已被 .gitignore 排除）或环境变量，代码里零硬编码。提交前可用 `git grep -lE "sk-[a-zA-Z0-9]{16,}"` 自查。

**Q: 图会被改坏吗？**
A: 四层护栏：注册表白名单（LLM 只能引用注册表内节点/策略）→ 验证器 10 条规则 → 影子评估（天气回放）→ 门控 + 自动回滚。改坏了会回滚，不会停摆。

## ⚖️ 诚实的设计边界

- **节点行为仍是代码**：LLM 进化的是拓扑/权重/工具集，不生成函数体（自我修改代码属研究级风险，刻意不做）
- **架构师的空间推理在渐进**：提案质量受 LLM 图推理能力限制，通过"拒绝原因 + 修复建议"反馈逐轮改进
- **技能库规模决定检索质量**：多领域技能积累后，跨任务误匹配会自然消失
- 本项目是**实验/教学性质**，欢迎 fork 改造

## 🔗 Related Work

本项目是以下方向的开源组合，各维度均有先驱，但**无人同时具备全部**：

- **[EvolveBank](https://github.com/C10udsea/evolvebank)** — 自进化策略银行：反思蒸馏 + 向量检索 + 结果反馈，与本项目知识轴同构
- **[SkillFlow](https://github.com/beita6969/SkillFlow)** — 技能递归进化 + 奖励信号（训练型研究）
- **[Ouroboros](https://arxiv.org/html/2608.08311)** — Self-Developing Coding Agent，核心进化带 review 门控
- **[Living-Harness](https://arxiv.org/html/2607.26598v2)** — Interactive-Agent Evolver
- **[RewardHarness](https://github.com/TIGER-AI-Lab/RewardHarness)** — 奖励框架自身自进化
- **[Voyager](https://github.com/MineDojo/Voyager)** / **[Reflexion](https://github.com/noahshinn/reflexion)** — 技能库与反思的先驱
- 领域全景：[awesome-agent-evolution](https://github.com/Shiyao-Huang/awesome-agent-evolution)

## 📄 License

MIT（参照同目录 LICENSE；如无则默认 MIT，使用前请确认）
