#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/config.py —— 全局配置（优化版）
新增：Ollama 向量检索配置、mock API 配置
"""
import os
import re
from pathlib import Path

# 自动读取 Hermes 的 .env（DEEPSEEK_API_KEY 等）。
# 路径来自环境变量 HERMES_HOME，未设置时回退到系统用户目录下的默认位置。
# 注意：不硬编码任何用户名路径（GitHub 安全）。
_HERMES_DEFAULT = Path.home() / "AppData/Local/hermes"
_HERMES_ENV = Path(os.getenv("HERMES_HOME", str(_HERMES_DEFAULT))) / ".env"
if _HERMES_ENV.exists():
    for _line in _HERMES_ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ============ 路径 ============
BASE_DIR = Path(__file__).resolve().parent.parent   # harness_v4/
SKILLS_DIR = BASE_DIR / "skills"
MEMORY_DIR = BASE_DIR / "memory"
LOG_DIR = BASE_DIR / "rl_logs"
CACHE_DIR = BASE_DIR / "cache"

for d in [SKILLS_DIR, MEMORY_DIR, LOG_DIR, CACHE_DIR]:
    d.mkdir(exist_ok=True)

# ============ LLM 配置 ============
LLM_CONFIG = {
    "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    "api_key": os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", ""),
    "model": os.getenv("LLM_MODEL", "deepseek-chat"),
}

# ============ 向量检索配置（Ollama bge-m3，本地免费）============
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
EMBED_DIM = 1024          # bge-m3 输出维度
LOAD_THRESHOLD = 0.40     # 余弦相似度阈值（bge-m3 语义空间）
LOAD_TOP_K = 2

# ============ 奖励权重（默认值；运行时值由 weights.py 加载到 memory/reward_weights.json）============
REWARD_WEIGHTS_DEFAULTS = {
    "load_skills": 1.0,
    "execute": 3.0,
    "efficiency": 1.5,
    "reflect": 1.0,
    "create_skill": 1.0,
}

_runtime_weights = None  # 由 weights.load_weights() 设置；None → 用默认值


def get_weight(step: str) -> float:
    """运行时读取奖励权重（属性访问而非 import 绑定，权重自适应才能生效）"""
    if _runtime_weights is not None:
        return _runtime_weights.get(step, REWARD_WEIGHTS_DEFAULTS.get(step, 1.0))
    return REWARD_WEIGHTS_DEFAULTS.get(step, 1.0)


def set_runtime_weights(weights: dict):
    global _runtime_weights
    _runtime_weights = dict(weights) if weights else None

# ============ 效率奖励配置 ============
EFF_TOOL_PENALTY = 0.5    # 每次工具调用扣分
EFF_STEP_BONUS = 1.0      # 步骤少加分
EFF_REUSE_BONUS = 1.0     # 复用技能加分
EFF_STEP_MIN = 3
EFF_STEP_MAX = 6

# ============ Curator 配置 ============
STALE_DAYS = 30
ARCHIVE_DAYS = 90
UMBRELLA_MIN = 3

# ============ 缓存 ============
CACHE_MAX_AGE_HOURS = 24

# ============ Mock API（真实任务环境）============
MOCK_API_URL = "http://127.0.0.1:8891/data"
MOCK_TIMEOUT = 1.0        # 客户端正式请求超时：1秒（mock 有 3 秒慢响应→必超时）
PRE_CHECK_TIMEOUT = 0.3   # 前置探测超时：短超时试探服务是否可用
CIRCUIT_THRESHOLD = 2     # 熔断阈值：连续失败次数达到即熔断，停止重试
UPDATE_THRESHOLD = 0.70   # 技能 upsert 阈值：新反思与已有技能相似度达到即更新而非新建
TOOL_MATCH_THRESHOLD = 0.55  # 技能工具匹配阈值：task 嵌入与技能 description 相似度达到即调用该工具

# ============ 结构自进化（档3 图进化 / 档2 权重自适应）============
GRAPH_SPEC_PATH = MEMORY_DIR / "graph_spec.json"
GRAPH_SPEC_BACKUP_PATH = MEMORY_DIR / "graph_spec.backup.json"
WEIGHTS_PATH = MEMORY_DIR / "reward_weights.json"
EVOLUTION_LOG_PATH = MEMORY_DIR / "evolution_log.jsonl"
SHADOW_TRAJ_PATH = LOG_DIR / "shadow_trajectory.jsonl"
SHADOW_EXP_PATH = MEMORY_DIR / "shadow_experiences.jsonl"

PROTECTED_NODES = {"load_skills", "execute", "save_memory"}  # 架构师不可删除的核心节点
GRAPH_MAX_NODES = 12        # 图节点数上限
GRAPH_MAX_EDGES = 16        # 边（含条件边）总数上限
EPOCH_ROUNDS = 3           # 每 N 轮触发一次架构师检查点（与每次运行 3 任务的节奏对齐）
SHADOW_ROUNDS = 3           # 影子评估轮数
GATE_THRESHOLD = 1.0        # 切换门控：影子重算总分均值须 ≥ 生产 + 此值
ROLLBACK_WINDOW = 3         # 回滚观察窗口（轮）
ROLLBACK_THRESHOLD = 2.0    # 回滚触发：切换后均值低于基线此值
ROLLBACK_COOLDOWN_SECONDS = 600  # 回滚后冷却期（秒），期间不提案防振荡

WEIGHT_MIN = 0.2            # 权重下限
WEIGHT_MAX = 4.0            # 权重上限
WEIGHT_STEP = 0.1           # 单次调整比例（±10%）
WEIGHT_MIN_SAMPLES = 8      # 自适应最小样本数（窗口 5 轮 × 3 任务 = 15 条，留余量）
WEIGHT_MIN_GROUP = 2        # 每组（成功/失败）最小样本数
WEIGHT_DEADBAND = 0.25      # 死区：|Δ| 低于此值不动权重
WEIGHT_STEPS = ("load_skills", "execute", "efficiency")  # 只对恒现步骤自适应
WEIGHT_WINDOW_ROUNDS = 5    # 自适应统计窗口（轮）
