#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/evolution_log.py —— 结构自进化审计日志（新增）
架构师每次决策（提案/验证/影子/门控/切换/回滚）都落 JSONL；
图快照是回滚的数据源，切换前必须备份。
"""
import json
import time

from core.config import EVOLUTION_LOG_PATH, GRAPH_SPEC_BACKUP_PATH


def log_event(kind: str, payload: dict):
    """追加一条进化事件（可审计）"""
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **payload}
    with open(EVOLUTION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_events() -> list:
    """读取全部进化事件（回滚检测 / 审计用）"""
    events = []
    try:
        for line in EVOLUTION_LOG_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    except FileNotFoundError:
        pass
    return events


def save_snapshot(spec: dict, label: str):
    """切换前备份图快照（回滚源）"""
    with open(GRAPH_SPEC_BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump({"label": label, "spec": spec}, f, ensure_ascii=False, indent=2)


def load_snapshot():
    """读取最近一次备份（回滚用），不存在返回 None"""
    try:
        return json.loads(GRAPH_SPEC_BACKUP_PATH.read_text(encoding="utf-8"))["spec"]
    except Exception:
        return None
