"""修复动作分级：把「报出来的问题」分成「能自动改的」与「只能建议的」。

## 为什么需要这一层（来自开源实践）

`codex-edit-gates` 的 `py_edit_gate` 用四层递进：安全 autofix → auto-import →
可证安全的 import 提升 → 纯检查。核心思想是**只有能被证明不改变行为的修复才自动执行**，
其余一律只报告。

我们此前只有「全自动（scripted fixer）」与「全不自动（只报）」两档，
中间是空的：像 `bare_except` 这种「该改但改成什么要判断」的问题，
要么被粗糙地自动改（风险），要么完全不碰（失去价值）。

## 分级判据

- `auto`      删除未使用 import。AST 已能确认该名字从未被引用，删除不改变任何可观测行为。
- `suggest`   需要判断的问题：裸 except 换成哪个异常、长函数怎么拆、重复分支是否条件写错。
              给动作，但不代替人/模型决定。
- `advisory`  只是记录：TODO 标记、读文件失败。这类不该产生自动改动。

`syntax_error` 单列 `blocking`：它不产生修复动作，但会阻断后续所有自动修复
（语法不成立时任何重写都可能扩大破坏）。
"""
from __future__ import annotations

# 每个 kind 的分级。新增 smell 类型时必须在这里登记，否则默认按 suggest 处理。
TIERS: dict[str, str] = {
    # 可证安全：删掉一个 AST 确认未被引用的 import
    "unused_import": "auto",
    # 需要判断
    "bare_except": "suggest",
    "dead_assignment": "suggest",
    "deep_nesting": "suggest",
    "duplicate_branch": "suggest",
    "long_function": "suggest",
    "long_line": "suggest",
    "syntax_error": "suggest",
    # 仅记录
    "todo_marker": "advisory",
    "read_error": "advisory",
}

DEFAULT_TIER = "suggest"

# 阻断型：存在时不做任何自动修复
BLOCKING = {"syntax_error", "read_error"}

TIER_LABEL = {
    "auto": "可自动",
    "suggest": "需判断",
    "advisory": "仅记录",
}


def repair_tier(kind: str) -> str:
    """返回修复分级：auto / suggest / advisory。"""
    return TIERS.get(kind, DEFAULT_TIER)


def is_blocking(kind: str) -> bool:
    """该问题是否阻断自动修复。"""
    return kind in BLOCKING


def is_auto_fixable(kind: str) -> bool:
    return repair_tier(kind) == "auto"


def summarize(kinds: list[str]) -> dict[str, int]:
    """按分级计数，用于报告尾部给出一句话结论。"""
    out = {"auto": 0, "suggest": 0, "advisory": 0}
    for k in kinds:
        out[repair_tier(k)] += 1
    return out
