"""补丁档案与验收流程：状态机、非法迁移、compare 判定。"""
import argparse
import json

import pytest

from tools import verify_patch
from tools.patch_registry import RegistryError, load, promote, record, save, sync


def _patch_dir(tmp_path, target=0.04, cases=2):
    d = tmp_path / "p001-skill-demo"
    d.mkdir()
    (d / "patch.json").write_text(json.dumps({
        "id": "p001", "kind": "skill", "title": "demo", "status": "candidate",
        "verify": {"metric": "tool_error_rate", "baseline": 0.10, "target": target, "cases": cases},
    }), encoding="utf-8")
    return d


def test_registry_sync_promote_record(tmp_path):
    reg = tmp_path / "reg.json"
    patches = tmp_path / "patches"
    (patches / "p001-skill-demo").mkdir(parents=True)
    (patches / "p001-skill-demo" / "patch.json").write_text(json.dumps({
        "id": "p001", "kind": "skill", "title": "demo"}))
    data = load(reg)
    assert sync(data, patches) == ["p001"]
    promote(data, "p001", "trial", "隔离 profile 验证")
    record(data, "p001", {"tool_error_rate": 0.03})
    save(reg, data)
    again = load(reg)
    assert again["patches"]["p001"]["status"] == "trial"
    assert again["patches"]["p001"]["metrics"]["tool_error_rate"] == 0.03
    assert len(again["patches"]["p001"]["history"]) == 3


def test_illegal_transition_rejected(tmp_path):
    reg = tmp_path / "reg.json"
    patches = tmp_path / "patches" / "p001-skill-demo"
    patches.mkdir(parents=True)
    (patches / "patch.json").write_text(json.dumps({
        "id": "p001", "kind": "skill", "title": "demo"}))
    data = load(reg)
    sync(data, tmp_path / "patches")
    with pytest.raises(RegistryError):
        promote(data, "p001", "adopted")
    promote(data, "p001", "trial")
    promote(data, "p001", "adopted")
    with pytest.raises(RegistryError):
        promote(data, "p001", "rejected")


def test_plan_writes_cases_and_baseline(tmp_path):
    patch = _patch_dir(tmp_path)
    turns = tmp_path / "turns.jsonl"
    rows = [
        {"session": "s1", "turn": 1, "task": "a", "success": False,
         "tool_calls": [{"name": "ops_bash", "ok": False, "error": "boom"}]},
        {"session": "s1", "turn": 2, "task": "b", "success": True, "tool_calls": []},
    ]
    turns.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    verify_patch.cmd_plan(argparse.Namespace(patch=str(patch), turns=str(turns), out=""))
    cases = json.loads((patch / "cases.json").read_text(encoding="utf-8"))
    assert len(cases) == 1 and cases[0]["tool"] == "ops_bash"
    plan = json.loads((patch / "verify_plan.json").read_text(encoding="utf-8"))
    assert plan["target"] == 0.04 and plan["cases"] == 1


def test_compare_pass_and_fail(tmp_path):
    patch = _patch_dir(tmp_path, target=0.04, cases=2)
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps({"cases": [{"id": "a", "ok": False}, {"id": "b", "ok": False}]}))
    after.write_text(json.dumps({"cases": [{"id": "a", "ok": True}, {"id": "b", "ok": True}]}))
    verify_patch.cmd_compare(argparse.Namespace(patch_dir=str(patch), before=str(before),
                                                after=str(after), registry=""))
    result = json.loads((patch / "verify_result.json").read_text(encoding="utf-8"))
    assert result["passed"] is True
    assert result["after"]["error_rate"] == 0.0

    after.write_text(json.dumps({"cases": [{"id": "a", "ok": False}, {"id": "b", "ok": False}]}))
    verify_patch.cmd_compare(argparse.Namespace(patch_dir=str(patch), before=str(before),
                                                after=str(after), registry=""))
    result = json.loads((patch / "verify_result.json").read_text(encoding="utf-8"))
    assert result["passed"] is False


def test_compare_fails_when_cases_missing(tmp_path):
    patch = _patch_dir(tmp_path, target=0.04, cases=4)
    one = tmp_path / "one.json"
    one.write_text(json.dumps({"cases": [{"id": "a", "ok": True}]}))
    verify_patch.cmd_compare(argparse.Namespace(patch_dir=str(patch), before=str(one),
                                                after=str(one), registry=""))
    result = json.loads((patch / "verify_result.json").read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert "用例不足" in result["reason"]
