#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_dsh.py —— 把 DeepSeek Harness 的会话转录导出成「每轮任务」数据集

输入：
  ~/.dsh/sessions/<项目>/<session>/session.v4.jsonl.zstd

输出（默认 build/）：
  dsh_turns.jsonl   —— 每行一轮：任务、工具调用、token、耗时、是否成功
  dsh_events.json   —— 可直接喂给进化层作「天气回放」：{轮次: [每次尝试的 ok 序列]}
  dsh_summary.json  —— 汇总：工具分布、错误率、token 总量、重试次数

用法：
  python tools/export_dsh.py --limit 20
  python tools/export_dsh.py --sessions "C:/Users/18059/.dsh/sessions" --out build
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None


def iter_events(path: Path):
    """逐行读事件（支持 .zstd 与纯文本 .jsonl）"""
    if path.suffix == ".zstd":
        if zstandard is None:
            raise RuntimeError("需要 zstandard：pip install zstandard")
        with open(path, "rb") as f:
            raw = zstandard.ZstdDecompressor().stream_reader(f).read()
    else:
        raw = path.read_bytes()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _text_of(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts).strip()


CUT_MARKERS = (
    "Current runtime context.",
    "MNEMON RUNTIME MEMORY SNAPSHOT",
    "[MNEMON]",
    "The Team Lead and all teammates",
)


def _task_text(text: str, limit: int = 240) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.S)
    cut = min((text.find(m) for m in CUT_MARKERS if m in text), default=-1)
    if cut > 0:
        text = text[:cut]
    text = re.sub(r"<[^>]{1,80}>", " ", text)   # 去掉注入标签（acp/available_skills 等）
    text = " ".join(text.split())
    return text[:limit]


def build_turns(path: Path, max_turns: int | None = None):
    """把单个会话切成一轮一轮"""
    meta = {}
    turns = {}
    current = None
    pending_calls = {}   # callId -> {"name", "time", "args"}

    for ev in iter_events(path):
        etype = ev.get("type")
        data = ev.get("data") or {}
        t = ev.get("time", 0)

        if etype == "session":
            meta = {
                "session": ev.get("id"),
                "cwd": ev.get("cwd"),
                "origin": ev.get("origin"),
                "preset": ev.get("agentPreset"),
                "parent": ev.get("parentSession"),
            }
            continue

        if etype == "turn/start":
            tid = data.get("turn")
            current = {
                "session": meta.get("session", path.parent.name),
                "cwd": meta.get("cwd"),
                "turn": tid,
                "started_at": t,
                "user_text": "",
                "steps": 0,
                "tool_calls": [],
                "attempts": [],      # 按时间：工具调用 / LLM 重试的 ok 序列
                "tokens": {"input": 0, "output": 0,
                           "cache_read": 0, "cache_write": 0, "total": 0},
                "llm_retries": 0,
                "end_reason": None,
                "ended_at": None,
            }
            turns[tid] = current
            continue

        if current is None:
            continue

        if etype == "user/message":
            txt = _text_of(data.get("content"))
            if txt:
                current["user_text"] = (current["user_text"] + "\n" + txt).strip()

        elif etype == "assistant/message":
            usage = data.get("usage") or {}
            cur = current["tokens"]
            cur["input"] += int(usage.get("inputTokens") or 0)
            cur["output"] += int(usage.get("outputTokens") or 0)
            cur["cache_read"] += int(usage.get("cacheReadTokens") or 0)
            cur["cache_write"] += int(usage.get("cacheWriteTokens") or 0)
            cur["total"] += int(usage.get("totalTokens") or 0)
            current["steps"] += 1

        elif etype == "llm/retry":
            current["llm_retries"] += 1
            current["attempts"].append((t, False, "llm_retry"))

        elif etype == "tool/call":
            cid = data.get("callId")
            pending_calls[cid] = {
                "name": data.get("name") or "unknown",
                "time": t,
                "call_id": cid,
            }

        elif etype == "tool/result":
            msg = data.get("message") or {}
            cid = data.get("toolCallId") or msg.get("toolCallId")
            call = pending_calls.pop(cid, {"name": "unknown", "time": t})
            if call.get("name") == "unknown" and cid:
                # 有些版本 call/result 不同步到达，回退用 callId 反查
                call = pending_calls.pop(cid, call)
            is_error = bool(msg.get("isError"))
            meta_tool = data.get("meta") or {}
            exit_code = (meta_tool.get("opsShell") or {}).get("exitCode")
            ok = (not is_error) and exit_code in (0, None)
            current["tool_calls"].append({
                "name": call.get("name", "unknown"),
                "ok": ok,
                "duration_ms": max(0, t - call.get("time", t)),
                "exit_code": exit_code,
            })
            current["attempts"].append((call.get("time", t), ok, call.get("name", "unknown")))

        elif etype == "turn/end":
            current["ended_at"] = t
            current["end_reason"] = (data.get("reason") or {}).get("kind")

    out = []
    for tid in sorted(turns):
        tr = turns[tid]
        if not tr["user_text"]:
            continue
        attempts = sorted(tr["attempts"], key=lambda x: x[0])
        calls = tr["tool_calls"]
        duration = (tr["ended_at"] or tr["started_at"]) - tr["started_at"]
        out.append({
            "session": tr["session"],
            "cwd": tr["cwd"],
            "turn": tr["turn"],
            "task": _task_text(tr["user_text"]),
            "success": tr["end_reason"] == "completed",
            "end_reason": tr["end_reason"],
            "duration_ms": duration,
            "steps": tr["steps"],
            "tool_calls": calls,
            "tool_errors": sum(1 for c in calls if not c["ok"]),
            "llm_retries": tr["llm_retries"],
            "tokens": tr["tokens"],
            "attempts": [{"ok": ok, "kind": kind} for _, ok, kind in attempts],
        })
        if max_turns and len(out) >= max_turns:
            break
    return meta, out


def find_sessions(root: Path):
    """按修改时间倒序返回所有 session 转录"""
    files = list(root.rglob("session.v4.jsonl.zstd")) + list(root.rglob("session.v4.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def summarize(rows):
    tools = {}
    for r in rows:
        for c in r["tool_calls"]:
            tools[c["name"]] = tools.get(c["name"], 0) + 1
    calls = sum(len(r["tool_calls"]) for r in rows)
    errors = sum(r["tool_errors"] for r in rows)
    return {
        "turns": len(rows),
        "sessions": len({r["session"] for r in rows}),
        "tool_calls": calls,
        "tool_errors": errors,
        "tool_error_rate": round(errors / calls, 3) if calls else 0.0,
        "llm_retries": sum(r["llm_retries"] for r in rows),
        "success_turns": sum(1 for r in rows if r["success"]),
        "avg_duration_ms": round(sum(r["duration_ms"] for r in rows) / len(rows)) if rows else 0,
        "tokens": {
            k: sum(r["tokens"][k] for r in rows)
            for k in ("input", "output", "cache_read", "total")
        },
        "top_tools": sorted(tools.items(), key=lambda kv: kv[1], reverse=True)[:12],
    }


def main():
    ap = argparse.ArgumentParser()
    default_root = Path(os.path.expanduser("~")) / ".dsh" / "sessions"
    ap.add_argument("--sessions", default=str(default_root))
    ap.add_argument("--out", default="build")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个会话文件")
    ap.add_argument("--max-turns", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.sessions)
    if not root.exists():
        sys.exit(f"会话目录不存在: {root}")

    files = find_sessions(root)
    if args.limit:
        files = files[: args.limit]
    if not files:
        sys.exit("没有找到 session.v4.jsonl(.zstd)")

    rows = []
    sessions = 0
    for f in files:
        try:
            _, turns = build_turns(f, args.max_turns or None)
        except Exception as exc:  # 单文件坏掉不影响整体
            print(f"[skip] {f}: {exc}", file=sys.stderr)
            continue
        if turns:
            sessions += 1
            rows.extend(turns)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    turns_path = out_dir / "dsh_turns.jsonl"
    with open(turns_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    events = {str(r["turn"]): [a["ok"] for a in r["attempts"]] for r in rows}
    (out_dir / "dsh_events.json").write_text(
        json.dumps(events, ensure_ascii=False), encoding="utf-8")

    summary = summarize(rows)
    summary["sessions_scanned"] = sessions
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    (out_dir / "dsh_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 天气脚本：每条真实 attempt 的 ok/失败序列，可直接喂给 main.py --weather
    weather = [
        {"task": r["task"], "success": r["success"],
         "duration_ms": r["duration_ms"], "events": [a["ok"] for a in r["attempts"]]}
        for r in rows if r["attempts"]
    ]
    (out_dir / "dsh_weather.json").write_text(
        json.dumps(weather, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n写出: {turns_path}  ({len(rows)} 行)")
    print(f"写出: {out_dir / 'dsh_weather.json'}  ({len(weather)} 条天气)")


if __name__ == "__main__":
    main()
