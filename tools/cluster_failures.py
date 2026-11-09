#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cluster_failures.py —— 从导出的 DSH 轮次里聚出真实失败模式

输入：build/*/dsh_turns.jsonl（tools/export_dsh.py 产物）
输出：build/*/dsh_failures.json + 终端表格

聚类口径：一轮失败 = success=false 或 tool_errors>0；
按"失败工具组合"分组（如 ops_bash、run_code+ops_bash），
没有工具错误的失败按 end_reason 单独一组。
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


def _sig(name: str, error: str) -> str:
    """把错误文本压成可聚类的签名：去 id/路径噪声，保留前 70 字。"""
    text = (error or "").replace("Error: ", "", 1)
    text = re.sub(r"[0-9a-f]{16,}", "<id>", text)
    text = re.sub(r"[A-Za-z]:\\[^\s:]+", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f"{name}: {text[:70]}" if text else f"{name}: (无错误文本)"


def cluster(rows):
    clusters = defaultdict(lambda: {"turns": [], "errors": 0, "samples": []})
    for r in rows:
        failing = [c for c in r["tool_calls"] if not c["ok"]]
        if not failing and r["success"]:
            continue
        if failing:
            key = _sig(failing[0]["name"], failing[0].get("error", ""))
        else:
            key = f"(无工具错误:{r['end_reason']})"
        c = clusters[key]
        c["turns"].append(r["turn"])
        c["errors"] += len(failing)
        if len(c["samples"]) < 3:
            c["samples"].append({
                "session": r["session"],
                "turn": r["turn"],
                "task": r["task"],
                "errors": [{"tool": x["name"], "error": x.get("error", "")} for x in failing],
                "duration_ms": r["duration_ms"],
            })

    out = []
    for key, c in clusters.items():
        out.append({
            "cluster": key,
            "turns": len(c["turns"]),
            "errors": c["errors"],
            "turn_ids": c["turns"],
            "samples": c["samples"],
        })
    out.sort(key=lambda x: (-x["errors"], -x["turns"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", default="build/dsh_weather/dsh_turns.jsonl")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    turns_path = Path(args.turns)
    rows = [json.loads(l) for l in turns_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    clusters = cluster(rows)

    total = sum(c["turns"] for c in clusters)
    print(f"失败轮: {total} / {len(rows)}  聚类数: {len(clusters)}\n")
    print(f"{'聚类':<28}{'轮数':>4}{'错误数':>6}  样例任务")
    print("-" * 82)
    for c in clusters:
        sample = c["samples"][0]["task"][:28] if c["samples"] else ""
        print(f"{c['cluster']:<28}{c['turns']:>4}{c['errors']:>6}  {sample}")

    out = Path(args.out) if args.out else turns_path.parent / "dsh_failures.json"
    out.write_text(json.dumps(clusters, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出: {out}")


if __name__ == "__main__":
    main()
