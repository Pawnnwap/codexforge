"""守门触发判定：决定「现在该不该动手」。

## 这个模块是常驻服务的成败关键

常驻服务和 CLI 的本质区别：CLI 是「我说跑就跑」，常驻是「我自己决定何时跑」。

判断错了只有两种下场：
- **太激进** → 每次保存都触发重构，用户被打断，直接卸载
- **太保守** → 一整天不动一次，用户觉得没用

所以这里不用「文件变了就跑」这种粗暴逻辑，而是四道闸门串联。

## 四道闸门

1. **冷却闸**：距上次运行不足 `cooldown_s` 不跑。
   目的是防止「用户连续保存 20 次 → 触发 20 次」。
2. **变更闸**：只看源码类文件，且排除缓存/构建目录。
   改 README 不该触发代码重构。
3. **债务闸**：整洁度必须低于阈值，或出现新的 high 级问题。
   90 分的代码不需要「重构」。
4. **冲突闸**：工作区有未提交改动且变化剧烈时不跑。
   用户正在大规模改代码时插进去重构，等于制造冲突。

## 为什么不看 git commit

看似「每次提交后跑一次」很自然，但实测不成立：
- 一人公司常常一天提一次、甚至几天提一次
- 提交信息不可靠，无法判断这次改动是否涉及代码质量

所以用「文件系统变更 + 债务阈值」双条件，而不绑 git。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from cforge.longrun.state import workspace_fingerprint
from cforge.quality.metrics import QualityReport, evaluate

# 触发判定只看这些后缀 —— 改文档不该触发代码重构
SOURCE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".rs", ".go", ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".m", ".r", ".jl",
}

# 明确不算「源码变更」的路径片段
IGNORE_PARTS = {
    "__pycache__", ".venv", "venv", "node_modules", ".git", ".pytest_cache",
    "dist", "build", "target", "out", ".mypy_cache", ".ruff_cache",
    ".cforge", ".opencode", ".idea", ".vscode",
}


@dataclass
class ChangeSet:
    """一次扫描得到的变更概况。"""

    files: list[str] = field(default_factory=list)
    total_changed: int = 0
    fingerprint: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def is_massive(self) -> bool:
        """变更规模超过阈值 —— 用户正在大改，此时介入会制造冲突。"""
        return len(self.files) >= 25

    def is_massive_with(self, threshold: int) -> bool:
        """按调用方给的阈值判断规模。

        为什么要有这个而不是只改上面的 property：`is_massive` 把 25 写死了，
        但 `WatchConfig.massive_threshold` 这个字段一直存在却**从未被读取**
        ——配置写了不生效是最难发现的一类缺陷（改了没反应，以为是别处问题）。

        新增方法而非直接改 property：property 无参数，改签名会波及既有调用方；
        保留旧 property 让常驻守门行为逐字节不变，实验/案例走新方法。
        """
        return len(self.files) >= max(1, threshold)


def scan_changes(root: Path, since: float, *, max_files: int = 3000) -> ChangeSet:
    """扫描 since 之后被修改的源码文件。

    用 mtime 而不是内容哈希：常驻服务要频繁扫描，读全部文件内容太贵。
    内容哈希只在真正决定要跑之后才算（见 workspace_fingerprint）。
    """
    root = Path(root)
    cs = ChangeSet()
    count = 0
    for path in root.rglob("*"):
        if count >= max_files:
            break
        if not path.is_file():
            continue
        try:
            rel_parts = set(path.relative_to(root).parts[:-1])
        except ValueError:
            continue
        if rel_parts & IGNORE_PARTS:
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        count += 1
        try:
            if path.stat().st_mtime > since:
                cs.files.append(str(path.relative_to(root)).replace("\\", "/"))
                cs.total_changed += 1
        except OSError:
            continue
    return cs


@dataclass
class WatchConfig:
    """常驻守门的策略参数。

    默认值的选择依据（面向一人公司/独立开发者）：
    - `cooldown_s=600`：10 分钟。比这更频繁会打断心流；
      比这更稀疏则一天只跑几次，价值感低。
    - `debt_score=80`：低于 80 才动手。80-100 是「正常在写代码」的区间，
      不该被视为需要重构。
    - `max_high=2`：允许 2 个 high 级问题存在（如偶发裸 except），
      超过才视为债务累积。
    """

    # 两次运行的最小间隔
    cooldown_s: float = 600.0
    # 整洁度低于此分才触发重构
    debt_score: float = 80.0
    # high 级问题超过此数才触发
    max_high: int = 2
    # 变更文件数达到此值视为「用户正在大改」，本次跳过
    massive_threshold: int = 25


@dataclass
class TriggerDecision:
    """一次触发判定。

    `decide` 必须给出可解释理由 —— 用户会问「为什么现在跑」或「为什么没跑」，
    没有理由的黑盒守护进程没人敢用。
    """

    should_run: bool
    reason: str
    # 四道闸门各自的结果，便于诊断
    gates: dict[str, str] = field(default_factory=dict)
    changes: ChangeSet | None = None
    report: QualityReport | None = None

    def as_dict(self) -> dict:
        return {
            "should_run": self.should_run,
            "reason": self.reason,
            "gates": self.gates,
            "changed_files": self.changes.files[:20] if self.changes else [],
            "n_changed": self.changes.total_changed if self.changes else 0,
            "quality_score": self.report.score if self.report else None,
            "n_high": len(self.report.by_severity("high")) if self.report else None,
        }


class TriggerPolicy:
    """四道闸门串联。任何一个否决就不再往下走（省掉昂贵的评估）。"""

    def __init__(self, workspace: Path, config: WatchConfig | None = None):
        self.workspace = Path(workspace)
        self.config = config or WatchConfig()
        self._last_run_at: float = 0.0

    def mark_ran(self, when: float | None = None) -> None:
        self._last_run_at = when or time.time()

    def decide(self, *, now: float | None = None) -> TriggerDecision:
        now = now or time.time()
        cfg = self.config
        gates: dict[str, str] = {}

        # ---- 闸门 1：冷却 ----
        elapsed = now - self._last_run_at
        if self._last_run_at > 0 and elapsed < cfg.cooldown_s:
            gates["cooldown"] = f"距离上次运行 {elapsed:.0f}s < {cfg.cooldown_s:.0f}s"
            return TriggerDecision(False, "冷却中", gates)
        gates["cooldown"] = "通过"

        # ---- 闸门 2：变更 ----
        since = self._last_run_at if self._last_run_at > 0 else now - 86400
        changes = scan_changes(self.workspace, since)
        gates["changes"] = f"{changes.total_changed} 个源码文件变更"
        if changes.is_empty:
            return TriggerDecision(False, "无源码变更", gates, changes)

        if changes.is_massive_with(cfg.massive_threshold):
            gates["conflict"] = (
                f"变更 {changes.total_changed} 个文件（阈值 {cfg.massive_threshold}），"
                "用户正在大改")
            return TriggerDecision(False, "变更规模过大，避免冲突", gates, changes)
        gates["conflict"] = "通过"

        # ---- 闸门 3：债务（唯一需要昂贵评估的闸门，放在最后）----
        report = evaluate(self.workspace)
        n_high = len(report.by_severity("high"))
        low_score = report.score < cfg.debt_score
        too_many_high = n_high > cfg.max_high
        gates["debt"] = (
            f"整洁度 {report.score}（阈值 {cfg.debt_score}），high 级 {n_high}（上限 {cfg.max_high}）"
        )
        if not (low_score or too_many_high):
            return TriggerDecision(False, "债务未达阈值", gates, changes, report)

        reason_parts = []
        if low_score:
            reason_parts.append(f"整洁度 {report.score} 低于 {cfg.debt_score}")
        if too_many_high:
            reason_parts.append(f"{n_high} 个 high 级问题超过上限 {cfg.max_high}")
        return TriggerDecision(
            True, "；".join(reason_parts), gates, changes, report,
        )


@dataclass
class WatchEvent:
    """一次守门运行的记录，用于产出报告与审计。"""

    ts: float
    ran: bool
    reason: str
    quality_before: float = 0.0
    quality_after: float = 0.0
    fixed: int = 0
    introduced: int = 0
    accepted: bool = False
    elapsed_s: float = 0.0
    error: str = ""
    # 收敛循环的终止原因。为什么要单独一个字段而不复用 reason：
    # `reason` 是「为什么触发/为什么跳过」（四道闸门的判定），
    # 而这个是「跑起来之后为什么停下」——两者回答不同问题。
    # 实测动机：修复器遇到只有 todo_marker / long_function 的仓库时，
    # scripted 修不了任何一项，PolishLoop 给出「修复器放弃（问题需人工判断）」，
    # 但该结论此前没有出到 WatchEvent，报告里只剩「判定回滚，95.2 → 95.2」，
    # 读者无从知道「是修复失败」还是「这东西本来就修不了」。
    # 本项目自己的原则是「每个判定必须给出人能看懂的理由」，故补此字段。
    stop_reason: str = ""

    @property
    def quality_delta(self) -> float:
        return round(self.quality_after - self.quality_before, 1)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "ran": self.ran,
            "reason": self.reason,
            "quality_before": self.quality_before,
            "quality_after": self.quality_after,
            "quality_delta": self.quality_delta,
            "fixed": self.fixed,
            "introduced": self.introduced,
            "accepted": self.accepted,
            "elapsed_s": self.elapsed_s,
            "error": self.error[:200],
            "stop_reason": self.stop_reason[:300],
        }

    def summary_line(self) -> str:
        if not self.ran:
            return f"跳过：{self.reason}"
        mark = "✓" if self.accepted else "✗回滚"
        return (
            f"{mark} {self.quality_before} → {self.quality_after} "
            f"({self.quality_delta:+.1f})，修复 {self.fixed} 处，新增 {self.introduced} 处"
        )
