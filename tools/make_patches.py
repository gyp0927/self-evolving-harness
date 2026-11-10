#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_patches.py —— 把真实失败簇变成可审阅的补丁包（skill / plugin）

输入：build/*/dsh_turns.jsonl（tools/export_dsh.py 产物；可选 dsh_failures.json）
输出：build/*/patches/<id>/patch.json + payload/*，以及 INDEX.md

每个补丁都必须能被追溯、能回滚、能验收：
  evidence —— 来自哪几轮、哪个失败签名、多少次
  apply    —— 装到哪里
  rollback —— 怎么撤
  verify   —— 用什么指标、基线多少、目标多少
  status   —— candidate / approved / rejected（生成时一律 candidate）

用法：
  python tools/make_patches.py                       # 用默认导出目录
  python tools/make_patches.py --turns build/dsh_weather/dsh_turns.jsonl --out build/dsh_weather/patches
  python tools/make_patches.py --self-check          # 合成数据自检，不写真实目录
"""
import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path

from cluster_failures import cluster

KIND_ORDER = ("skill", "plugin")

# 提示词不属于本项目的补丁面：开源 agent 的 system prompt 已经写得很好，不去改它。
# 只动两处：技能（知识/恢复步骤）与插件（执行前的硬拦截）。

# 每类高频错误的“正确做法”；没命中的走 default
TOOL_RULES = {
    "ops_bash": (
        "ops_bash 复杂命令",
        "多行命令、内联脚本（node -e / python -c）、嵌套引号或 JSON 参数，"
        "先写成 .ps1/.sh/.mjs 文件再执行，不要塞进一行参数；"
        "失败先看 exitCode 与 stderr，再决定重试还是换命令。",
    ),
    "run_code": (
        "run_code 契约错误",
        "调用 mnemon 或其它工具前，先确认当前 View 真的提供该 action、"
        "Memory Space 在本轮 pinned 范围内；报 result.X is required 时按 schema 补齐必填字段，"
        "不要只改一个字段就重试。",
    ),
    "edit": (
        "edit 定位失败",
        "改之前先 read 目标区域；old_string 必须与文件逐字一致（注意 CRLF 与缩进）；"
        "找不到就重新读文件再定位，不要凭记忆重试。",
    ),
    "web_fetch": (
        "web_fetch 失败",
        "先确认 URL 可达；重试一次仍失败就换来源或改用搜索，不要反复抓同一地址。",
    ),
    "dshm_search": (
        "dshm_search 安装状态失败",
        "市场读不到安装状态时，检查 profile 的 package.json 依赖 spec："
        "本地目录要用 file: 前缀，link: 在 Windows 路径下会被判为 unknown。",
    ),
    "write": (
        "write 失败",
        "写入前确认目录存在、文件未被占用；路径用绝对路径。",
    ),
}


def _rule(cluster_key: str):
    tool = cluster_key.split(":", 1)[0].strip()
    return TOOL_RULES.get(tool, (
        "通用失败",
        "读错误原文，先分清是参数/契约错还是环境/网络错；"
        "契约错改参数，环境错重试一次，仍失败换路径。",
    ))


def _clean_task(text: str) -> str:
    """去掉运行时注入（PUA 遥测、失败观察等），只留人写的任务描述。"""
    for marker in ("PUA_RUNTIME_V1", "新增终端失败观察", "[PUA ", "MNEMON RUNTIME MEMORY SNAPSHOT"):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
    return " ".join(text.split())[:180]


def _samples(clusters, limit=5):
    out = []
    for c in clusters:
        for s in c["samples"]:
            if len(out) >= limit:
                return out
            out.append({"session": s["session"], "turn": s["turn"], "task": _clean_task(s["task"])})
    return out


def _evidence(clusters, rows) -> dict:
    total_turns = len(rows)
    errors = sum(c["errors"] for c in clusters)
    turns = sum(c["turns"] for c in clusters)
    total_calls = sum(len(r.get("tool_calls", [])) for r in rows)
    return {
        "total_turns": total_turns,
        "failure_turns": turns,
        "tool_calls": total_calls,
        "tool_errors": errors,
        "failure_rate": round(turns / total_turns, 4) if total_turns else 0.0,
        "tool_error_rate": round(errors / total_calls, 4) if total_calls else 0.0,
        "clusters": [
            {"signature": c["cluster"], "turns": c["turns"], "errors": c["errors"]}
            for c in clusters
        ],
        "samples": _samples(clusters),
    }


def _verify(evidence: dict) -> dict:
    base = evidence["tool_error_rate"]
    return {
        "metric": "tool_error_rate",
        "baseline": base,
        "target": round(max(base * 0.7, 0.0), 4),
        "cases": evidence["failure_turns"],
        "method": "用同一批 dsh_turns 回放对比工具错误率；未低于 target 不合入",
    }


def _skill_md(clusters, evidence) -> str:
    lines = [
        "---",
        "name: dsh-tool-recovery",
        "description: 工具调用失败后的恢复流程。适用于重复失败、ops_bash 复杂命令、"
        "run_code 契约错误、edit 定位失败、web_fetch 失败。重试前先换方法。",
        "---",
        "",
        "# 工具失败恢复（从真实会话聚类生成）",
        "",
        f"证据：{evidence['failure_turns']} 个失败轮 / {evidence['tool_errors']} 次工具错误。",
        "核心原则：**失败两次就换方法，不要原样重试第三次。**",
        "",
        "## 高频错误与做法",
        "",
    ]
    seen = set()
    for c in clusters:
        title, advice = _rule(c["cluster"])
        if title in seen:
            continue
        seen.add(title)
        lines.append(f"### {title}（{c['turns']} 轮 / {c['errors']} 次）")
        lines.append("")
        lines.append(f"- 签名：`{c['cluster'][:90]}`")
        lines.append(f"- 做法：{advice}")
        lines.append("")
    lines += [
        "## 决策顺序",
        "",
        "1. 读错误原文，确认是「参数/契约错」还是「环境/网络错」",
        "2. 契约错 → 改参数或换工具；环境错 → 重试一次，仍失败换路径",
        "3. 同一调用完全相同地失败 2 次 → 停下来换方案（改参数不算换方案）",
        "",
    ]
    return "\n".join(lines)


GUARD_JS = r'''/**
 * dsh-guard —— 重复失败拦截（补丁生成器产出的候选插件）
 *
 * 同一「工具名 + 参数」连续失败 2 次后，下一次相同调用在 tools/pre-execute 阶段
 * 直接拒绝，并附最近错误与换方法提示。成功一次清零；10 分钟窗口外不计。
 */
export const name = 'dsh-guard';

const FAIL_THRESHOLD = 2;
const WINDOW_MS = 10 * 60 * 1000;
const MAX_CALLS = 2000;
const MAX_FAILURES = 500;
const ARG_CLIP = 600;

const calls = new Map();
const failures = new Map();

function argText(raw) {
  if (raw == null) return '';
  if (typeof raw === 'string') return raw;
  try { return JSON.stringify(raw); } catch { return String(raw); }
}

function signature(tool, raw) {
  const text = argText(raw).replace(/\s+/g, ' ').trim();
  return `${tool}\u0000${text.length > ARG_CLIP ? text.slice(0, ARG_CLIP) : text}`;
}

function firstLine(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  const line = text.split('\n')[0].trim();
  return line.length > 160 ? `${line.slice(0, 160)}…` : line;
}

function errorText(data) {
  const message = data?.message ?? {};
  const blocks = Array.isArray(message.content) ? message.content : [];
  for (const block of blocks) {
    const line = firstLine(block?.text);
    if (line) return line;
  }
  return firstLine(message.text) || firstLine(data?.error) || '未知错误';
}

export function apply(ctx) {
  ctx.on('session/event', (_session, event) => {
    if (!event || typeof event.type !== 'string') return;

    if (event.type === 'tool/call') {
      const data = event.data ?? {};
      const callId = data.callId ?? data.id;
      if (!callId) return;
      if (calls.size >= MAX_CALLS) calls.delete(calls.keys().next().value);
      calls.set(callId, signature(String(data.name ?? ''), data.arguments));
      return;
    }

    if (event.type !== 'tool/result') return;
    const data = event.data ?? {};
    const message = data.message ?? {};
    const callId = data.toolCallId ?? message.toolCallId ?? data.callId;
    if (!callId) return;
    const sig = calls.get(callId);
    if (!sig) return;
    calls.delete(callId);

    if (!(Boolean(message.isError) || Boolean(data.isError))) {
      failures.delete(sig);
      return;
    }

    const now = Date.now();
    const prev = failures.get(sig);
    failures.set(sig, {
      count: (prev && now - prev.last <= WINDOW_MS ? prev.count : 0) + 1,
      last: now,
      error: errorText(data),
    });
    if (failures.size > MAX_FAILURES) {
      for (const [key, entry] of failures) if (now - entry.last > WINDOW_MS) failures.delete(key);
    }
  });

  ctx.on('tools/pre-execute', async (exec, next) => {
    try {
      const tool = String(exec?.name ?? '');
      const raw = exec?.arguments ?? exec?.args ?? exec?.input;
      if (!tool || raw == null) return next();
      const key = signature(tool, raw);
      const entry = failures.get(key);
      if (!entry || entry.count < FAIL_THRESHOLD) return next();
      if (Date.now() - entry.last > WINDOW_MS) { failures.delete(key); return next(); }
      return {
        kind: 'deny',
        reason:
          `dsh-guard：这个调用（${tool} + 相同参数）已经连续失败 ${entry.count} 次。` +
          `最近错误：${entry.error}。不要原样重试第三次——换参数、换工具或先读文档。`,
      };
    } catch {
      return next();
    }
  });
}
'''

GUARD_PKG = json.dumps({
    "name": "dsh-guard",
    "version": "0.1.0",
    "description": "重复失败拦截：同一调用连续失败两次后，第三次拒绝并提示换方法。",
    "type": "module",
    "main": "lib/index.js",
    "exports": {".": "./lib/index.js", "./package.json": "./package.json"},
    "license": "MIT",
    "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
}, ensure_ascii=False, indent=2) + "\n"

GUARD_PATCH = "- insert:\n    - id: dsh-guard\n      name: dsh-guard\n"


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_patches(rows, out_dir: Path):
    clusters = cluster(rows)
    evidence = _evidence(clusters, rows)
    verify = _verify(evidence)
    out_dir.mkdir(parents=True, exist_ok=True)

    patches = []

    # 1) 技能补丁
    p1 = out_dir / "p001-skill-dsh-tool-recovery"
    _write(p1 / "payload" / "SKILL.md", _skill_md(clusters, evidence))
    patches.append({
        "id": "p001",
        "kind": "skill",
        "title": "工具失败恢复技能（dsh-tool-recovery）",
        "target": "dsh:desktop",
        "risk": "low",
        "status": "candidate",
        "evidence": evidence,
        "payload": {"files": [{"path": "SKILL.md", "role": "skill"}]},
        "apply": "复制到 ~/.agents/skills/dsh-tool-recovery/SKILL.md（或 .claude/skills 镜像目录）",
        "rollback": "删除该技能目录即可，不影响其它配置",
        "verify": verify,
        "_dir": p1,
    })

    # 2) 插件补丁
    p2 = out_dir / "p002-plugin-repeat-guard"
    _write(p2 / "payload" / "lib" / "index.js", GUARD_JS)
    _write(p2 / "payload" / "package.json", GUARD_PKG)
    _write(p2 / "payload" / "cordis.patch.yml", GUARD_PATCH)
    patches.append({
        "id": "p002",
        "kind": "plugin",
        "title": "dsh-guard：重复失败调用拦截",
        "target": "dsh:desktop",
        "risk": "medium",
        "status": "candidate",
        "evidence": evidence,
        "payload": {"files": [
            {"path": "lib/index.js", "role": "plugin-source"},
            {"path": "package.json", "role": "plugin-manifest"},
            {"path": "cordis.patch.yml", "role": "bundle-patch"},
        ]},
        "apply": "装到 desktop profile（node_modules junction + dependencies + bundles），重启生效",
        "rollback": "删 junction 并从 package.json 去掉依赖与 bundle 条目",
        "verify": verify,
        "_dir": p2,
    })

    for p in patches:
        d = p.pop("_dir")
        meta = {k: p[k] for k in ("id", "kind", "title", "target", "risk", "status",
                                  "evidence", "payload", "apply", "rollback", "verify")}
        _write(d / "patch.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    index = ["# 补丁清单", "",
             f"来源：{evidence['total_turns']} 轮 / {evidence['tool_calls']} 次工具调用，"
             f"失败 {evidence['failure_turns']} 轮、{evidence['tool_errors']} 次工具错误"
             f"（工具错误率 {evidence['tool_error_rate']:.1%}）", "",
             "| 编号 | 类型 | 标题 | 风险 | 状态 |", "| --- | --- | --- | --- | --- |"]
    for p in patches:
        index.append(f"| {p['id']} | {p['kind']} | {p['title']} | {p['risk']} | {p['status']} |")
    index += ["", "验收口径：同一批会话回放，工具错误率需低于基线 70%。", ""]
    (out_dir / "INDEX.md").write_text("\n".join(index), encoding="utf-8")
    return patches, evidence


def _self_check():
    rows = [
        {"session": "s1", "turn": 1, "task": "跑一个复杂命令", "success": False,
         "end_reason": "completed", "duration_ms": 10,
         "tool_calls": [{"name": "ops_bash", "ok": False, "error": "Command failed: node -e"}]},
        {"session": "s1", "turn": 2, "task": "改文件", "success": False,
         "end_reason": "completed", "duration_ms": 10,
         "tool_calls": [{"name": "edit", "ok": False, "error": "old_string was not found"}]},
        {"session": "s1", "turn": 3, "task": "正常", "success": True,
         "end_reason": "completed", "duration_ms": 10, "tool_calls": []},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        patches, evidence = build_patches(rows, Path(tmp))
        assert [p["kind"] for p in patches] == list(KIND_ORDER)
        assert evidence["failure_turns"] == 2
        for p in patches:
            dirs = list(Path(tmp).glob(f"{p['id']}-*"))
            assert len(dirs) == 1, p["id"]
            meta = json.loads((dirs[0] / "patch.json").read_text("utf-8"))
            for key in ("id", "kind", "evidence", "payload", "apply", "rollback", "verify", "status"):
                assert key in meta, key
        assert (Path(tmp) / "p002-plugin-repeat-guard" / "payload" / "lib" / "index.js").exists()
    print("self-check ok: 2 类补丁 + 必需字段齐全")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", default="build/dsh_weather/dsh_turns.jsonl")
    ap.add_argument("--out", default="")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        _self_check()
        return

    turns_path = Path(args.turns)
    rows = [json.loads(l) for l in turns_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    out_dir = Path(args.out) if args.out else turns_path.parent / "patches"
    patches, evidence = build_patches(rows, out_dir)
    print(f"轮次 {evidence['total_turns']}，失败轮 {evidence['failure_turns']}，"
          f"工具错误 {evidence['tool_errors']}，失败率 {evidence['failure_rate']:.1%}")
    for p in patches:
        print(f"  {p['id']}  {p['kind']:<6} {p['title']}")
    print(f"\n写出: {out_dir}")


if __name__ == "__main__":
    main()
