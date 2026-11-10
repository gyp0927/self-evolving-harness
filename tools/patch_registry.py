#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_registry.py —— 补丁档案：candidate → trial → adopted / rejected

补丁不只是生成完就完了：每个补丁的验证结果和最终去留都要有记录，
下次生成时才能优先选“已被证明有效”的做法。

状态机（非法迁移直接拒绝）：
  candidate → trial | rejected
  trial     → adopted | rejected
  rejected  → candidate        （可以重新评估）
  adopted   → （终态）

用法：
  python tools/patch_registry.py sync --patches build/dsh_weather/patches
  python tools/patch_registry.py list
  python tools/patch_registry.py promote p001 --to trial --note "装到隔离 profile"
  python tools/patch_registry.py record p001 --metrics '{"tool_error_rate": 0.041}'
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REGISTRY = "build/patch_registry.json"

STATUSES = ("candidate", "trial", "adopted", "rejected")
ALLOWED = {
    "candidate": {"trial", "rejected"},
    "trial": {"adopted", "rejected"},
    "rejected": {"candidate"},
    "adopted": set(),
}


class RegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "patches": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("patches"), dict):
        raise RegistryError(f"registry 格式不对: {path}")
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sync(data: dict, patches_dir: Path) -> list:
    """把补丁目录里新出现的 patch.json 登记进来；已存在的不覆盖状态。"""
    added = []
    for pj in sorted(patches_dir.glob("p*/patch.json")):
        meta = json.loads(pj.read_text(encoding="utf-8"))
        pid = meta["id"]
        if pid in data["patches"]:
            continue
        data["patches"][pid] = {
            "id": pid,
            "kind": meta["kind"],
            "title": meta["title"],
            "status": "candidate",
            "created_at": _now(),
            "updated_at": _now(),
            "metrics": {},
            "history": [{"at": _now(), "event": "registered", "to": "candidate"}],
        }
        added.append(pid)
    return added


def promote(data: dict, pid: str, target: str, note: str = "") -> dict:
    entry = data["patches"].get(pid)
    if entry is None:
        raise RegistryError(f"未知补丁: {pid}")
    if target not in STATUSES:
        raise RegistryError(f"未知状态: {target}")
    current = entry["status"]
    if target not in ALLOWED[current]:
        raise RegistryError(f"非法迁移: {current} → {target}")
    entry["status"] = target
    entry["updated_at"] = _now()
    entry["history"].append({"at": _now(), "event": "promote", "from": current, "to": target, "note": note})
    return entry


def record(data: dict, pid: str, metrics: dict, note: str = "") -> dict:
    entry = data["patches"].get(pid)
    if entry is None:
        raise RegistryError(f"未知补丁: {pid}")
    entry["metrics"] = {**entry.get("metrics", {}), **metrics}
    entry["updated_at"] = _now()
    entry["history"].append({"at": _now(), "event": "metrics", "value": metrics, "note": note})
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--patches", required=True)

    sub.add_parser("list")

    p_promote = sub.add_parser("promote")
    p_promote.add_argument("id")
    p_promote.add_argument("--to", required=True, choices=STATUSES)
    p_promote.add_argument("--note", default="")

    p_record = sub.add_parser("record")
    p_record.add_argument("id")
    p_record.add_argument("--metrics", required=True)
    p_record.add_argument("--note", default="")

    args = ap.parse_args()
    path = Path(args.registry)
    data = load(path)

    if args.cmd == "sync":
        added = sync(data, Path(args.patches))
        save(path, data)
        print(f"新登记 {len(added)} 个: {', '.join(added) or '无'}")
    elif args.cmd == "list":
        for pid, e in sorted(data["patches"].items()):
            print(f"{pid:<6}{e['status']:<10}{e['kind']:<8}{e['title']}")
    elif args.cmd == "promote":
        try:
            e = promote(data, args.id, args.to, args.note)
        except RegistryError as err:
            raise SystemExit(str(err))
        save(path, data)
        print(f"{e['id']} → {e['status']}")
    elif args.cmd == "record":
        try:
            metrics = json.loads(args.metrics)
        except json.JSONDecodeError as err:
            raise SystemExit(f"metrics 不是合法 JSON: {err}")
        try:
            e = record(data, args.id, metrics, args.note)
        except RegistryError as err:
            raise SystemExit(str(err))
        save(path, data)
        print(f"{e['id']} 指标已记录: {json.dumps(e['metrics'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
