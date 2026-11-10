#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_patch.py —— 补丁验收：把“同一批失败任务重跑、比较错误率”变成可执行流程

三段式：
  plan     从真实导出里挑出失败用例，写 cases.json + verify_plan.json（基线/目标/口径）
  run      执行外部 runner（隔离 profile 里的重跑脚本），产出 results.json
  compare  比较重跑结果与基线，判定 pass/fail，写 verify_result.json，并可选更新补丁档案

runner 契约（由你提供，任何语言都行）：
  python my_runner.py --cases cases.json --out results.json
  cases.json   : [{"id": "...", "task": "...", "tool": "ops_bash"}]
  results.json : {"cases": [{"id": "...", "ok": true}]}

用法：
  python tools/verify_patch.py plan --patch build/.../p001-skill-* --turns build/dsh_weather/dsh_turns.jsonl
  python tools/verify_patch.py run --patch-dir ... --runner "python my_runner.py --cases {cases} --out {out}"
  python tools/verify_patch.py compare --patch-dir ... --before results_before.json --after results_after.json
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def _patch_meta(patch_dir: Path) -> dict:
    meta_path = patch_dir / "patch.json"
    if not meta_path.exists():
        raise SystemExit(f"找不到 {meta_path}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _fail_cases(turns_path: Path) -> list:
    cases = []
    for line in turns_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        bad = [c for c in row.get("tool_calls", []) if not c.get("ok")]
        if not bad and row.get("success"):
            continue
        cases.append({
            "id": f"{row['session']}:{row['turn']}",
            "task": row.get("task", ""),
            "tool": bad[0]["name"] if bad else "",
            "error": bad[0].get("error", "") if bad else "",
        })
    return cases


def cmd_plan(args):
    patch_dir = Path(args.patch)
    meta = _patch_meta(patch_dir)
    cases = _fail_cases(Path(args.turns))
    out = Path(args.out) if args.out else patch_dir
    (out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verify = meta["verify"]
    plan = {
        "patch_id": meta["id"],
        "metric": verify["metric"],
        "baseline": verify["baseline"],
        "target": verify["target"],
        "cases": len(cases),
        "runner_contract": "runner --cases {cases} --out {out}；输出 {\"cases\":[{\"id\",\"ok\"}]}",
        "steps": [
            "把补丁装进隔离 profile / 临时会话（不要动生产 profile）",
            "用 runner 重跑 cases.json，得到 results_after.json",
            "再跑一次未打补丁的同一批用例，得到 results_before.json",
            "compare 两份结果，未达到 target 就不合入",
        ],
    }
    (out / "verify_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"用例 {len(cases)} 条 → {out / 'cases.json'}")
    print(f"基线 {verify['baseline']:.2%} → 目标 ≤{verify['target']:.2%}")


def _rate(results_path: Path) -> tuple:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    ok = sum(1 for c in cases if c.get("ok"))
    return (1 - ok / len(cases) if cases else 0.0), len(cases)


def cmd_run(args):
    patch_dir = Path(args.patch_dir)
    cases = patch_dir / "cases.json"
    if not cases.exists():
        raise SystemExit("先跑 plan 生成 cases.json")
    out = Path(args.out) if args.out else patch_dir / "results_after.json"
    cmd = args.runner.replace("{cases}", str(cases)).replace("{out}", str(out))
    print(f"执行: {cmd}")
    proc = subprocess.run(cmd, shell=True)
    if proc.returncode != 0:
        raise SystemExit(f"runner 失败，退出码 {proc.returncode}")
    if not out.exists():
        raise SystemExit(f"runner 没有产出 {out}")
    rate, n = _rate(out)
    print(f"结果 {n} 条，错误率 {rate:.2%} → {out}")


def cmd_compare(args):
    patch_dir = Path(args.patch_dir)
    meta = _patch_meta(patch_dir)
    target = meta["verify"]["target"]
    before_rate, before_n = _rate(Path(args.before))
    after_rate, after_n = _rate(Path(args.after))
    passed = after_rate <= target and after_n >= meta["verify"]["cases"] * 0.5
    result = {
        "patch_id": meta["id"],
        "metric": meta["verify"]["metric"],
        "target": target,
        "before": {"error_rate": round(before_rate, 4), "cases": before_n},
        "after": {"error_rate": round(after_rate, 4), "cases": after_n},
        "passed": passed,
        "reason": "错误率达标" if passed else
                  ("重跑用例不足" if after_n < meta["verify"]["cases"] * 0.5 else "错误率未低于目标"),
    }
    out = patch_dir / "verify_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{result['reason']}：{before_rate:.2%} → {after_rate:.2%}（目标 ≤{target:.2%}）")
    print(f"写出: {out}")

    if args.registry:
        sys.path.insert(0, str(Path(__file__).parent))
        from patch_registry import load, record, promote, save
        reg_path = Path(args.registry)
        data = load(reg_path)
        pid = meta["id"]
        if pid in data["patches"]:
            record(data, pid, {"tool_error_rate": round(after_rate, 4), "passed": passed,
                               "cases": after_n}, note=result["reason"])
            if passed and data["patches"][pid]["status"] == "candidate":
                promote(data, pid, "trial", note="离线验收通过")
            save(reg_path, data)
            print(f"档案已更新: {pid}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--patch", required=True)
    p_plan.add_argument("--turns", default="build/dsh_weather/dsh_turns.jsonl")
    p_plan.add_argument("--out", default="")

    p_run = sub.add_parser("run")
    p_run.add_argument("--patch-dir", required=True)
    p_run.add_argument("--runner", required=True)
    p_run.add_argument("--out", default="")

    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("--patch-dir", required=True)
    p_cmp.add_argument("--before", required=True)
    p_cmp.add_argument("--after", required=True)
    p_cmp.add_argument("--registry", default="")

    args = ap.parse_args()
    if args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "compare":
        cmd_compare(args)


if __name__ == "__main__":
    main()
