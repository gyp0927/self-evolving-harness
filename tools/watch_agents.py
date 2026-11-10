#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_agents.py —— 实时监督本机 agent 的工具失败

扫描两类来源：
  claude : ~/.claude/projects/**/*.jsonl   （追加写，增量 tail）
  dsh    : ~/.dsh/sessions/**/session.v4.jsonl.zstd （整文件解压，按行号增量）

做三件事：
  1. 把新出现的失败事件归一化成 {ts, source, session, tool, error}
  2. 按「工具 + 归一化错误」聚签名
  3. 同一签名 24h 内 >= 3 次且 1h 内没报过 → 写 build/alerts.jsonl 并打印

用法：
  python tools/watch_agents.py --once                 # 扫一遍就退出（适合计划任务）
  python tools/watch_agents.py --interval 15          # 常驻，每 15 秒扫一次
  python tools/watch_agents.py --once --sources claude
"""
import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = Path.home() / ".claude" / "projects"
DSH_DIR = Path.home() / ".dsh" / "sessions"
STATE = ROOT / "build" / "watch_state.json"
ALERTS = ROOT / "build" / "alerts.jsonl"

ALERT_WINDOW_S = 24 * 3600
ALERT_THRESHOLD = 3
COOLDOWN_S = 3600


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_error(text: str, limit: int = 90) -> str:
    """压掉 id/路径/数字，保留可聚类的骨架"""
    s = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", s, flags=re.I)
    s = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<path>", s)
    s = re.sub(r"/(?:[\w.-]+/)+[\w.-]+", "<path>", s)
    s = re.sub(r"\b\d+\b", "<n>", s)
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def signature(tool: str, error: str) -> str:
    return f"{tool} :: {normalize_error(error)}"


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"claude": {}, "dsh": {}, "tools": {}}


def save_state(state: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ---------------- claude ----------------

def _claude_tool_map(line: dict, tools: dict):
    msg = line.get("message") or {}
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tid, name = block.get("id"), block.get("name")
            if tid and name:
                tools[tid] = name
                if len(tools) > 800:
                    tools.pop(next(iter(tools)))


def scan_claude(state: dict, out: list):
    tools = state.setdefault("tools", {})
    files = state.setdefault("claude", {})
    for path in CLAUDE_DIR.rglob("*.jsonl"):
        key = str(path)
        st = files.get(key) or {"offset": 0, "buf": ""}
        try:
            size = path.stat().st_size
            if size <= st["offset"]:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(st["offset"])
                chunk = f.read()
            st["offset"] = size
            lines = (st.get("buf", "") + chunk).split("\n")
            st["buf"] = lines.pop() if lines else ""
            files[key] = st
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    line = json.loads(raw)
                except Exception:
                    continue
                _claude_tool_map(line, tools)
                if line.get("type") != "user":
                    continue
                content = (line.get("message") or {}).get("content") or []
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    if not block.get("is_error"):
                        continue
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                    tid = block.get("tool_use_id")
                    out.append({
                        "ts": _parse_ts(line.get("timestamp")),
                        "source": "claude",
                        "session": line.get("sessionId") or path.stem,
                        "tool": tools.get(tid, "unknown"),
                        "error": str(body or ""),
                    })
        except (OSError, PermissionError):
            continue


def _parse_ts(value) -> float:
    if not value:
        return _now()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return _now()


# ---------------- dsh ----------------

def _dsh_events(path: Path):
    import zstandard
    if path.suffix == ".zstd":
        with open(path, "rb") as f:
            raw = zstandard.ZstdDecompressor().stream_reader(f).read()
    else:
        raw = path.read_bytes()
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.strip():
            try:
                yield json.loads(line)
            except Exception:
                continue


def scan_dsh(state: dict, out: list):
    files = state.setdefault("dsh", {})
    for path in DSH_DIR.rglob("session.v4.jsonl.zstd"):
        key = str(path)
        st = files.get(key) or {"lines": 0}
        try:
            mtime = path.stat().st_mtime
            if st.get("mtime") == mtime:
                continue
            events = list(_dsh_events(path))
            st["mtime"] = mtime
            tools = {}
            for ev in events:
                if ev.get("type") == "tool/call":
                    d = ev.get("data") or {}
                    if d.get("callId") and d.get("name"):
                        tools[d["callId"]] = d["name"]
            for ev in events[st.get("lines", 0):]:
                if ev.get("type") != "tool/result":
                    continue
                d = ev.get("data") or {}
                msg = d.get("message") or {}
                if not msg.get("isError"):
                    continue
                content = msg.get("content") or []
                text = content[0].get("text", "") if content and isinstance(content[0], dict) else str(content)
                meta = d.get("meta") or {}
                if not text and isinstance(meta.get("opsShell"), dict):
                    text = f"exit {meta['opsShell'].get('exitCode')}"
                cid = d.get("toolCallId") or msg.get("toolCallId")
                out.append({
                    "ts": (ev.get("time") or 0) / 1000 or _now(),
                    "source": "dsh",
                    "session": path.parent.name,
                    "tool": tools.get(cid, "unknown"),
                    "error": text,
                })
            st["lines"] = len(events)
            files[key] = st
        except Exception:
            continue


# ---------------- 聚合与告警 ----------------

def emit_alerts(failures: list, state: dict):
    seen = state.setdefault("seen", {})
    now = _now()
    alerted = state.setdefault("alerted", {})
    ALERTS.parent.mkdir(parents=True, exist_ok=True)
    hits = 0
    for f in failures:
        sig = signature(f["tool"], f["error"])
        seen[sig] = [t for t in seen.get(sig, []) if now - t < ALERT_WINDOW_S] + [f["ts"]]
        if len(seen[sig]) < ALERT_THRESHOLD:
            continue
        if now - alerted.get(sig, 0) < COOLDOWN_S:
            continue
        alerted[sig] = now
        rec = {**f, "signature": sig, "count_24h": len(seen[sig]), "at": _iso(now)}
        with open(ALERTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"⚠️  {f['tool']}  x{len(seen[sig])}/24h  {normalize_error(f['error'])}")
        hits += 1
    return hits


def sweep(sources: list) -> list:
    state = load_state()
    out = []
    if "claude" in sources:
        scan_claude(state, out)
    if "dsh" in sources:
        scan_dsh(state, out)
    hits = emit_alerts(out, state)
    save_state(state)
    return out, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--sources", default="claude,dsh")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    while True:
        out, hits = sweep(sources)
        if args.json:
            Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[{datetime.now():%H:%M:%S}] 新失败 {len(out)} 条，告警 {hits} 条")
        if args.once:
            break
        time.sleep(max(3, args.interval))


if __name__ == "__main__":
    main()
