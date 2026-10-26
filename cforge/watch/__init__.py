"""watch 包：常驻守门形态。

定位需要这个形态 —— 「给一人公司的代码守门」如果还是手动 CLI 命令，
就与定位自相矛盾。守门天然是持续的。

三个子模块：
- trigger：决定何时动手（四道闸门）
- guardian：常驻循环 + 报告
- fixer：修复器（codex 版 / AST 确定性版）
"""

from cforge.watch.fixer import CodexFixAgent, ScriptedFixAgent, build_fix_prompt
from cforge.watch.guardian import Guardian, GuardianConfig, GuardianState
from cforge.watch.trigger import (
    ChangeSet,
    TriggerDecision,
    TriggerPolicy,
    WatchConfig,
    WatchEvent,
    scan_changes,
)

__all__ = [
    "TriggerPolicy", "TriggerDecision", "WatchConfig", "WatchEvent",
    "ChangeSet", "scan_changes",
    "Guardian", "GuardianConfig", "GuardianState",
    "CodexFixAgent", "ScriptedFixAgent", "build_fix_prompt",
]
