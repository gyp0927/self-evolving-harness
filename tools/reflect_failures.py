#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reflect_failures.py —— 把真实失败聚类反射成候选技能

默认用确定性模板（无外部依赖）；加 --llm 时调用项目配置的 LLM 做真反思。
产物写到 build/dsh_skills/*.md，可直接拷进 DSH 技能目录做人工评审。
"""
import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

PROCEDURES = {
    "run_code": (
        "1. 先确认要调用的工具/action 在当前 View 里真的被 offer（错误形如 "
        "`Source Action is not offered`）\n"
        "2. mnemon 相关调用必须落在 pinned Memory Space 内（错误形如 "
        "`outside pinned Source view`）\n"
        "3. 失败后先读错误原文，按契约改参数，不要原样重试\n"
        "4. 复杂 shell 写成脚本文件执行，避免引号/转义"
    ),
    "ops_bash": (
        "1. 复杂命令写成 .ps1/.sh 文件再执行，不要塞进一行 JSON 参数\n"
        "2. PowerShell 里 JSON 用单引号包裹或 here-string，避免 `$` 展开\n"
        "3. 失败先看 exitCode 与 stderr，再决定重试还是换命令"
    ),
    "edit": (
        "1. 改文件前先 read 当前内容，不要凭记忆构造 old_string\n"
        "2. old_string 必须与文件逐字一致；跨平台注意 CRLF\n"
        "3. 失败后重新读取目标区域再改"
    ),
    "web_fetch": (
        "1. 先确认 URL 可达（可先 web_search 找准确地址）\n"
        "2. 失败重试一次；仍失败就换来源，不要反复抓同一个地址"
    ),
    "dshm_search": (
        "1. 市场查询依赖 profile 安装状态完整；出现 `读取 web profile 安装状态失败` 时\n"
        "   先检查 package.json 的依赖 spec（link: 只认 POSIX 绝对路径，Windows 用 file:）\n"
        "2. 修好后无需重启，重新查询即可"
    ),
    "dshm_uninstall": (
        "1. bundle 类插件不能用官方管理器卸载（报 `not-removable`）\n"
        "2. 手工从 profile 的 bundles 与 dependencies 移除，再把目录移出 node_modules\n"
        "3. 重启后生效，先备份 package.json"
    ),
}

TEMPLATE = """---
name: dsh-{slug}
description: {desc}
type: agent-created
state: candidate
source: dsh-failure
created_at: {today}
evidence: {turns} 轮 / {errors} 次失败
---

## 现象

```
{error}
```

## 触发条件

{conditions}

## 做法

{procedure}

## 验证

下次遇到同类调用，先按上面做法执行；仍失败就把错误原文追加到本技能，
由 curator 按命中率决定保留、升级或淘汰。
"""


def slugify(cluster: str) -> str:
    tool = cluster.split(":", 1)[0].strip().replace("__", "-")
    h = hashlib.md5(cluster.encode()).hexdigest()[:6]
    return f"{tool}-{h}"


def conditions(cluster: dict) -> str:
    lines = []
    for s in cluster["samples"][:3]:
        lines.append(f"- {s['task'][:60]}")
    return "\n".join(lines) or "- （无样例）"


def procedure_for(cluster_name: str) -> str:
    tool = cluster_name.split(":", 1)[0].strip()
    return PROCEDURES.get(tool, "1. 复现失败\n2. 读错误原文定位契约/环境问题\n3. 修正后重试一次，仍失败则换方案")


def build_skill(cluster: dict, today: str) -> str:
    name = cluster["cluster"]
    tool = name.split(":", 1)[0].strip()
    desc = f"{tool} 失败模式：{name.split(':', 1)[1].strip()[:60] if ':' in name else '未知'}"
    return TEMPLATE.format(
        slug=slugify(name),
        desc=desc.replace("\n", " "),
        today=today,
        turns=cluster["turns"],
        errors=cluster["errors"],
        error=cluster["samples"][0]["errors"][0]["error"][:300] if cluster["samples"] and cluster["samples"][0]["errors"] else name,
        conditions=conditions(cluster),
        procedure=procedure_for(name),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--failures", default="build/dsh_weather/dsh_failures.json")
    ap.add_argument("--out", default="build/dsh_skills")
    ap.add_argument("--min-turns", type=int, default=1)
    args = ap.parse_args()

    clusters = json.loads(Path(args.failures).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    made = []
    for c in clusters:
        if c["cluster"].startswith("(无工具错误") or c["errors"] == 0:
            continue
        if c["turns"] < args.min_turns:
            continue
        text = build_skill(c, date.today().isoformat())
        path = out / f"{slugify(c['cluster'])}.md"
        path.write_text(text, encoding="utf-8")
        made.append((path.name, c["cluster"][:56], c["turns"], c["errors"]))

    print(f"生成候选技能 {len(made)} 个 -> {out}\n")
    for name, cluster, turns, errors in made:
        print(f"  {name:<18} {turns:>2} 轮 / {errors:>2} 次   {cluster}")
    print("\n这些是 candidate，不会自动进入 agent；评审后再移进 DSH 技能目录。")


if __name__ == "__main__":
    main()
