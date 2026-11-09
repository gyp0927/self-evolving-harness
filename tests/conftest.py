"""pytest 共享配置：项目根加入 sys.path + 默认演示模式。

LLM_MOCK 必须在任何 core.* 模块被导入之前设置：core.config 在导入时
就把 MOCK_LLM 求值成常量。conftest 先于测试模块加载，所以放这里生效。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_MOCK", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
