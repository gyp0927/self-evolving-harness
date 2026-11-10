# 🧬 Harness — Agent 补丁流水线

![Python](https://img.shields.io/badge/Python-3.12-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/tests-5%20passed-brightgreen)

> 从真实会话里提取失败，生成**技能**和**插件**两类补丁，每一条都要过回放验收。不改框架、不改源码、不改提示词。

## 为什么做这个

开源 Agent 的框架和 system prompt 已经写得很成熟，缺的不是再写一套，而是**从自己的失败里学**：同一类工具错误反复出现，没有人把它固化成技能，也没有人在执行前拦一下。

所以这个项目的定位很小：**只做补丁，不做新 Agent。**

- 框架不动——节点、循环、上下文管理都保持宿主原样
- 源码不动——不给别人的实现打 diff
- 提示词不动——人家已经调好的 system prompt 不掺和
- 只改两处：**会什么**（技能）和**被什么拦住**（插件）

## 📦 产出物：两类补丁

| 类型 | 落点 | 作用 | 例子 |
|---|---|---|---|
| **技能** | `skills/<name>/SKILL.md`（可挂到宿主技能目录） | 把失败恢复步骤固化成按需加载的知识 | "同一调用失败两次必须换方法" + 各工具的处理顺序 |
| **插件** | 宿主插件（如 DSH plugin） | 用钩子在工具执行前拦截，给出拒绝理由 | 同一「工具+参数」连续失败 2 次后，第 3 次直接 deny |

## 🔁 工作流

```
真实会话 → 导出数据集 → 失败聚类 → 生成补丁 → 档案登记 → 回放验收 → 达标才装
```

| 步骤 | 命令 | 产物 |
|---|---|---|
| 导出 | `python tools/export_dsh.py --out build/dsh_weather` | `build/dsh_weather/{dsh_turns,dsh_weather,dsh_events,dsh_summary}.json*` |
| 聚类 | `python tools/cluster_failures.py` | `build/dsh_weather/dsh_failures.json` |
| 生成 | `python tools/make_patches.py` | `build/dsh_weather/patches/p001-skill-*`、`p002-plugin-*` |
| 登记 | `python tools/patch_registry.py sync --patches build/dsh_weather/patches` | `build/patch_registry.json` |
| 验收 | `python tools/verify_patch.py plan / run / compare` | `verify_result.json` |

## 🚀 快速开始

只读本地会话，不需要 LLM key：

```bash
pip install -r requirements.txt          # 含 zstandard
python tools/export_dsh.py --out build/dsh_weather
python tools/cluster_failures.py
python tools/make_patches.py
python tools/patch_registry.py sync --patches build/dsh_weather/patches
python tools/patch_registry.py list
```

用真实数据跑出来的样子（本机 154 轮会话）：

```
轮次 154，失败轮 30，工具错误 32，失败率 19.5%
  p001  skill  工具失败恢复技能（dsh-tool-recovery）
  p002  plugin dsh-guard：重复失败调用拦截
```

## 👀 实时监督（可选）

```bash
python tools/watch_agents.py --once            # 扫一遍就退出，适合计划任务
python tools/watch_agents.py --interval 15     # 常驻轮询
```

- 来源：Claude Code（`~/.claude/projects/**/*.jsonl`，增量 tail）与 DSH（`~/.dsh/sessions/**/session.v4.jsonl.zstd`，解压后按行增量）
- 产物：新失败写 `build/alerts.jsonl`；同一「工具+错误签名」24 小时内 ≥3 次且 1 小时未报过才告警
- 状态：`build/watch_state.json`（记录每个文件的读取位点，重启不重复计数）

## 🗂️ 补丁包长什么样

```
patches/
├── INDEX.md                        # 清单：编号/类型/标题/风险/状态
├── p001-skill-tool-recovery/
│   ├── patch.json                  # 证据、装法、回滚、验收指标
│   └── payload/SKILL.md
└── p002-plugin-repeat-guard/
    ├── patch.json
    └── payload/
        ├── lib/index.js            # 插件源码
        ├── package.json
        └── cordis.patch.yml        # bundle 注册
```

`patch.json` 的字段：

| 字段 | 含义 |
|---|---|
| `id` / `kind` / `title` | 编号、类型（skill/plugin）、标题 |
| `status` | 生成时一律 `candidate`，生成不等于应用 |
| `evidence` | 来自多少轮、多少失败、哪些签名、样本 |
| `payload` | 实体文件清单 |
| `apply` / `rollback` | 怎么装、怎么撤 |
| `verify` | 指标、基线、目标 |

## 🧾 档案与验收

状态机（非法迁移直接拒绝，`adopted` 是终态）：

```
candidate ──▶ trial ──▶ adopted
   │            │
   └──▶ rejected ◀──┘        rejected 可回到 candidate
```

验收分三段，runner 由你提供（任何语言都行）：

```bash
# 1) 从失败轮生成用例 + 验收计划
python tools/verify_patch.py plan --patch build/dsh_weather/patches/p001-skill-tool-recovery

# 2) 在隔离环境重跑同一批用例（不要动生产 profile）
python tools/verify_patch.py run --patch-dir <补丁目录> \
  --runner "python my_runner.py --cases {cases} --out {out}"

# 3) 对比前后错误率；达标才升级
python tools/verify_patch.py compare --patch-dir <补丁目录> \
  --before before.json --after after.json --registry build/patch_registry.json
```

判定口径：同一批失败任务重跑，**工具错误率 ≤ 基线 70%** 才算通过；用例数不足一半直接判 fail。

## 📁 目录结构

```
self-evolving-harness/
├── tools/                       # 主线：会话 → 数据集 → 补丁 → 验收
│   ├── export_dsh.py            # 导出 DSH 会话（turns/events/weather/summary）
│   ├── cluster_failures.py      # 按错误签名聚类失败
│   ├── reflect_failures.py      # 聚类 → 候选技能
│   ├── make_patches.py          # 聚类 → 技能/插件补丁包
│   ├── patch_registry.py        # 档案状态机
│   └── verify_patch.py          # plan/run/compare 验收
├── tests/                       # 5 项测试（补丁工具）
└── build/                       # 导出与补丁产物（.gitignore）
```

## ⚖️ 边界

- **不改宿主框架、源码、system prompt**——这是刻意的边界，不是能力不足
- **生成不等于应用**：所有补丁默认 `candidate`，装不装由人或验收结果决定
- **离线指标只是门槛**：回放能过滤明显无效的补丁，不能保证线上零回归
- 本项目是**实验/教学性质**，欢迎 fork 改造

## 🔗 Related Work

- **[EvolveBank](https://github.com/C10udsea/evolvebank)** — 自进化策略银行：反思蒸馏 + 向量检索 + 结果反馈
- **[SkillFlow](https://github.com/beita6969/SkillFlow)** — 技能递归进化 + 奖励信号
- **[Voyager](https://github.com/MineDojo/Voyager)** / **[Reflexion](https://github.com/noahshinn/reflexion)** — 技能库与反思的先驱
- 领域全景：[awesome-agent-evolution](https://github.com/Shiyao-Huang/awesome-agent-evolution)

## 📄 License

MIT（参照同目录 LICENSE；如无则默认 MIT，使用前请确认）
