#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/skill_manager.py —— Skill 全生命周期（结构自进化版）
- 向量检索：bge-m3 嵌入 + 余弦相似度（替代 LLM 逐个打分，快 10 倍）
- 注入精简：检索结果按"描述+关键步骤+关键教训"压缩，1500 字符内装下核心经验
- 反思 → 生成 SKILL.md；已存在高度相似技能时【更新】而非新建（upsert，知识收敛）
- 工具注册（档1）：技能 frontmatter 带 action 字段 → 注册为可调用工具，
  技能库的增删 = Agent 动作集的增删；无 action 的技能仍是参考文本（兼容）
- persist 开关：影子评估（persist=False）走完计算但不落盘、不碰元数据
- 缓存按文件 mtime 失效（技能更新/归档后向量与注册表自动刷新）
- Curator：雨伞合并 + 过期归档
"""
import json
import time
import math
import re
import yaml
from pathlib import Path

from core.config import (SKILLS_DIR, MEMORY_DIR, LOAD_THRESHOLD, LOAD_TOP_K,
                         STALE_DAYS, ARCHIVE_DAYS, UMBRELLA_MIN, UPDATE_THRESHOLD,
                         TOOL_MATCH_THRESHOLD, MAX_UPDATE_BLOCKS)
from core.llm_client import llm_client

SKILL_GEN_PROMPT = """根据反思生成标准 SKILL.md(YAML frontmatter + Markdown)。

反思:
{reflection}

任务:
{task}

格式(不要代码块):
---
name: 技能名(小写连字符)
description: 一句话说明
type: agent-created
created_at: {date}
state: active
---

## Task
解决什么问题

## Procedure
1. 第一步
2. 第二步

## Lessons
- 经验
"""


def cosine_sim(a: list, b: list) -> float:
    """余弦相似度（bge-m3 已归一化，点积即可）"""
    return sum(x * y for x, y in zip(a, b)) / max(
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)), 1e-9)


class SkillManager:
    def __init__(self):
        self.meta_path = MEMORY_DIR / "skill_meta.json"
        self.meta = self._load_meta()
        self._emb_cache = {}   # 技能名 → (mtime, 向量)，技能文件更新后自动失效
        self._desc_cache = {}  # 工具名 → (description, 向量)，description 变更自动重嵌

    def _load_meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        return {}

    def _save_meta(self):
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _embed_cached(self, f: Path) -> list:
        """按文件 mtime 缓存的嵌入：技能更新/归档后自动重新嵌入（修复陈旧向量 bug）"""
        mtime = f.stat().st_mtime
        entry = self._emb_cache.get(f.stem)
        if entry is not None and entry[0] == mtime:
            return entry[1]
        vec = llm_client.embed(f.read_text(encoding="utf-8")[:1500])
        self._emb_cache[f.stem] = (mtime, vec)
        return vec

    # ---------- 向量检索 ----------
    def load_relevant(self, task: str) -> str:
        """bge-m3 嵌入任务 → 与每个技能算余弦相似度 → 返回 Top K（精简版）"""
        from core.tracer import show_retrieval
        task_vec = llm_client.embed(task)
        scored = []
        scores_all = []
        for f in SKILLS_DIR.glob("*.md"):
            content = f.read_text(encoding="utf-8")
            score = cosine_sim(task_vec, self._embed_cached(f))
            scores_all.append((f.stem, score))
            if score >= LOAD_THRESHOLD:
                scored.append((score, f.name, content))
        show_retrieval(scores_all, LOAD_THRESHOLD)
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return "(无相关技能)"
        # 精简注入：同一字符预算内装下更多核心经验（description + 关键步骤 + 关键教训）
        return "\n\n".join(
            self._summarize_skill(s, name, content)
            for s, name, content in scored[:LOAD_TOP_K]
        )

    def _summarize_skill(self, score: float, name: str, content: str) -> str:
        """技能精简版：description + 前3条Procedure + 前3条Lessons"""
        desc = ""
        if content.count("---") >= 2:
            try:
                desc = yaml.safe_load(content.split("---")[1]).get("description", "")
            except Exception:
                pass
        lines = [l.strip() for l in content.splitlines()]
        proc = [l for l in lines if re.match(r"^\d+\.\s", l)][:3]
        lessons = [l[1:].strip() for l in lines if l.startswith("-")][:3]
        return (f"--- {name} (相关度{score:.3f}) ---\n"
                f"描述: {desc}\n"
                f"Procedure: {' | '.join(proc)}\n"
                f"Lessons: {' | '.join(lessons)}")

    # ---------- 工具注册（档1：技能 = 可调用工具）----------
    ACTION_WHITELIST = {"pre_check", "circuit_breaker", "fallback",
                        "max_retries", "base_delay", "backoff"}

    def _parse_action(self, front: dict) -> dict:
        """解析 frontmatter 的 action 字段：白名单 + 类型/数值约束，防 LLM 写坏配置"""
        action = front.get("action")
        if not action or not isinstance(action, dict):
            return None
        out = {}
        for k, v in action.items():
            if k not in self.ACTION_WHITELIST:
                continue  # 未知字段忽略
            if k == "pre_check" or k == "circuit_breaker":
                out[k] = bool(v) if isinstance(v, bool) else str(v).lower() == "true"
            elif k == "max_retries":
                try:
                    out[k] = max(1, min(5, int(v)))
                except (TypeError, ValueError):
                    pass
            elif k == "base_delay":
                try:
                    out[k] = max(0.2, min(2.0, float(v)))
                except (TypeError, ValueError):
                    pass
            elif k == "backoff":
                out[k] = v if v in ("exponential", "fixed") else None
            elif k == "fallback":
                out[k] = v if v in ("cached", "default") else None
        return out or None

    def registry(self) -> list:
        """动态扫描技能库 → 工具清单 [{name, description, action}]。
        技能库增删/更新 → 动作集随之变化（结构自进化的动作维度）。
        无 action 字段的技能不注册（仍是参考文本）。"""
        tools = []
        for f in SKILLS_DIR.glob("*.md"):
            content = f.read_text(encoding="utf-8")
            try:
                front = yaml.safe_load(content.split("---")[1]) or {}
            except Exception:
                continue
            action = self._parse_action(front)
            if action is None:
                continue
            tools.append({
                "name": f.stem,
                "description": front.get("description", f.stem),
                "action": action,
            })
        return tools

    def match_tool(self, task: str):
        """任务 → 最匹配的技能工具（description 嵌入相似度 ≥ TOOL_MATCH_THRESHOLD），无则 None。
        用 description 而非全文：语义更聚焦，避免跨领域任务（如问候语）误匹配 API 技能。
        description 变更时自动重嵌（按内容缓存）。"""
        tools = self.registry()
        if not tools:
            return None
        task_vec = llm_client.embed(task)
        best, best_score = None, 0.0
        for t in tools:
            desc = t["description"] or t["name"]
            entry = self._desc_cache.get(t["name"])
            if entry is None or entry[0] != desc:
                vec = llm_client.embed(desc)
                self._desc_cache[t["name"]] = (desc, vec)
            else:
                vec = entry[1]
            s = cosine_sim(task_vec, vec)
            if s > best_score:
                best, best_score = t, s
        if best_score >= TOOL_MATCH_THRESHOLD:
            return best
        return None

    # ---------- 生成（upsert：相似技能存在则更新，否则新建）----------
    def create(self, reflection: str, task: str, *, persist: bool = True):
        """反思 → 技能。返回 (内容, 路径, 是否更新已有技能)。
        persist=False（影子评估）：走完计算但不写文件、不碰 skill_meta，
        供 reward_skill 照常打分且零污染。"""
        best = self._find_most_similar(task)
        if best and best[1] >= UPDATE_THRESHOLD:
            return self._update_skill(best[0], reflection, task, persist=persist)
        return self._create_new(reflection, task, persist=persist)

    def _skill_description(self, f: Path) -> str:
        """取技能 frontmatter 的 description（与 match_tool 同一口径）"""
        try:
            front = yaml.safe_load(f.read_text(encoding="utf-8").split("---")[1]) or {}
            return front.get("description") or f.stem
        except Exception:
            return f.stem

    def _find_most_similar(self, task: str):
        """找与任务最相似的已有技能，返回 (名称, 相似度) 或 None。

        口径与 match_tool 一致：用 description 嵌入，不用技能全文。
        全文含 Procedure/Lessons，会把"同领域但描述不同"的技能拉偏，
        也让 upsert 阈值（0.70）与工具匹配阈值（0.55）含义不可比。
        """
        task_vec = llm_client.embed(task)
        best = None
        for f in SKILLS_DIR.glob("*.md"):
            desc = self._skill_description(f)
            entry = self._desc_cache.get(f.stem)
            if entry is None or entry[0] != desc:
                self._desc_cache[f.stem] = (desc, llm_client.embed(desc))
            s = cosine_sim(task_vec, self._desc_cache[f.stem][1])
            if best is None or s > best[1]:
                best = (f.stem, s)
        return best

    def _create_new(self, reflection: str, task: str, *, persist: bool = True):
        """反思 → 新建 SKILL.md（persist=False 只计算不落盘）"""
        prompt = SKILL_GEN_PROMPT.format(
            reflection=reflection[:800], task=task,
            date=time.strftime("%Y-%m-%d"))
        content = llm_client.call(prompt)
        try:
            name = yaml.safe_load(content.split("---")[1]).get(
                "name", f"skill_{int(time.time())}")
        except Exception:
            name = f"skill_{int(time.time())}"
        path = SKILLS_DIR / f"{name}.md"
        if not persist:
            return content, "", False
        path.write_text(content, encoding="utf-8")
        # 更新元数据
        now = time.time()
        if name in self.meta:
            self.meta[name]["uses"] = self.meta[name].get("uses", 0) + 1
        else:
            self.meta[name] = {"created": now, "last_used": now, "uses": 1}
        self._save_meta()
        return content, str(path), False

    def _update_skill(self, name: str, reflection: str, task: str, *, persist: bool = True):
        """更新已有技能：追加本轮反思提炼的新教训（去重），不新建文件"""
        path = SKILLS_DIR / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        parts = text.split("---")
        front = parts[1] if len(parts) >= 2 else ""
        body = "---".join(parts[2:]) if len(parts) >= 3 else text
        now = time.strftime("%Y-%m-%d")
        # 提取新教训（与已有教训行前20字重复的跳过；清洗缩进/多余破折号）
        def _clean_lesson(line: str) -> str:
            t = line.strip()
            while t.startswith("-"):
                t = t[1:].strip()
            return t
        existing = {_clean_lesson(l)[:20] for l in body.splitlines()
                    if l.strip().startswith("-")}
        new_lessons = [_clean_lesson(l) for l in reflection.splitlines()
                       if l.strip().startswith("-")
                       and _clean_lesson(l)[:20] not in existing]
        if not new_lessons:
            # 全部与已有教训重复 → 无新知识，仅登记使用，不动文档、不递增版本
            if persist:
                self._bump_meta(name)
            return text, str(path) if persist else "", True
        # frontmatter：更新 version / updated_at
        version = 2
        m = re.search(r"^version:\s*(\d+)", front, re.M)
        if m:
            version = int(m.group(1)) + 1
        if "version:" in front:
            front = re.sub(r"^version:\s*\S+", f"version: {version}", front, flags=re.M)
        else:
            front = front.rstrip() + f"\nversion: {version}\n"
        if "updated_at:" in front:
            front = re.sub(r"^updated_at:\s*\S+", f"updated_at: {now}", front, flags=re.M)
        else:
            front = front.rstrip() + f"\nupdated_at: {now}\n"
        update_block = (f"\n## Update v{version} ({now})\n\n任务: {task}\n\n"
                        + "\n".join(f"- {l}" for l in new_lessons) + "\n")
        new_text = f"---{front}---{body.rstrip()}\n{update_block}"
        # 只保留最近 N 段 Update：重复追加会让技能无限膨胀、稀释检索嵌入；
        # 老教训已沉淀在正文，不需要全部留在文档里。
        if new_text.count("\n## Update v") > MAX_UPDATE_BLOCKS:
            head, *blocks = new_text.split("\n## Update v")
            new_text = head + "".join("\n## Update v" + b
                                      for b in blocks[-MAX_UPDATE_BLOCKS:])
        if persist:
            path.write_text(new_text, encoding="utf-8")
            self._bump_meta(name)
        return new_text, str(path) if persist else "", True

    def _bump_meta(self, name: str):
        """登记技能被使用（uses+1, last_used=now）"""
        now = time.time()
        if name in self.meta:
            self.meta[name]["uses"] = self.meta[name].get("uses", 0) + 1
            self.meta[name]["last_used"] = now
        else:
            self.meta[name] = {"created": now, "last_used": now, "uses": 1}
        self._save_meta()

    def mark_used(self, skill_names: list):
        """标记技能被使用"""
        now = time.time()
        for n in skill_names:
            if n in self.meta:
                self.meta[n]["last_used"] = now
                self.meta[n]["uses"] = self.meta[n].get("uses", 0) + 1
        self._save_meta()

    # ---------- Curator ----------
    def curator_run(self):
        """技能整理：过期归档 + 雨伞合并"""
        print("\n🧹 Curator 运行中...")
        now = time.time()
        archive_dir = SKILLS_DIR / ".archive"
        archive_dir.mkdir(exist_ok=True)

        for name, info in list(self.meta.items()):
            days_unused = (now - info.get("last_used", now)) / 86400
            f = SKILLS_DIR / f"{name}.md"
            if days_unused > ARCHIVE_DAYS and f.exists():
                f.rename(archive_dir / f.name)
                print(f"  📦 归档(>{ARCHIVE_DAYS}天未用): {name}")
            elif days_unused > STALE_DAYS:
                print(f"  ⚠️ stale(>{STALE_DAYS}天未用): {name}")

        groups = {}
        for f in SKILLS_DIR.glob("*.md"):
            prefix = f.stem.split("-")[0]
            groups.setdefault(prefix, []).append(f)
        for prefix, files in groups.items():
            if len(files) < UMBRELLA_MIN:
                continue
            umbrella = SKILLS_DIR / f"{prefix}.md"
            others = [f for f in files if f != umbrella]
            if not others:
                continue
            # 首次合并时必须“开伞”：原实现只在 umbrella 已存在时合并，
            # 而 umbrella 永远不会自己出现 → 雨伞合并是死代码。
            if umbrella.exists():
                base = umbrella.read_text(encoding="utf-8")
            else:
                base = (f"---\nname: {prefix}\n"
                        f"description: {prefix} 系列技能合集（Curator 自动合并）\n"
                        f"type: curator\nstate: active\n---\n\n")
            parts = [f"## {f.stem}\n{f.read_text(encoding='utf-8')}" for f in others]
            umbrella.write_text(base.rstrip() + "\n\n" + "\n\n".join(parts) + "\n",
                                encoding="utf-8")
            for f in others:
                f.rename(archive_dir / f.name)
            print(f"  🌂 雨伞合并: {len(others)} 个 {prefix}-* → {prefix}.md")
        print("  ✅ Curator 完成")
