"""整洁度收敛循环：把「代码干净」从一次性检查变成可收敛的过程。

## 核心思路

单次评估只能说「现在脏」，不能保证「改完更干净」。真正的风险是：
**agent 为了修一个问题而引入两个新问题**，净效果为负。

所以引入两条纪律：

1. **净改善门（net-improvement gate）**：只有当 `score_after > score_before`
   且没有新增 high 级问题时，改动才被接受。否则回滚。
2. **收敛判定（convergence）**：连续 N 轮改善量 < ε 即停止，避免
   在 88 分上无限打磨而浪费时间预算。

## 为什么不用「达到目标分就停」

简单的 `score >= 90` 有两个问题：
- 95 分的代码里，那 5 分往往是主观风格，强行改会伤害可读性
- 有时间预算约束时，「性价比」比「绝对分数」重要

因此用「改善速率」而非「绝对分数」作为停止条件。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from cforge.quality.clones import CloneReport, scan as scan_clones
from cforge.quality.metrics import QualityReport, diff_reports, evaluate

# 快照时排除的目录
_COPY_EXCLUDE = {
    "__pycache__", ".venv", "venv", "node_modules", ".git", ".pytest_cache",
    ".cforge", ".opencode", "runs", ".archive",
}


@dataclass
class PolishRound:
    """一轮整洁度改进的记录。"""

    round_no: int
    score_before: float
    score_after: float
    fixed: int
    introduced: int
    accepted: bool
    reason: str
    elapsed_s: float = 0.0

    @property
    def delta(self) -> float:
        return round(self.score_after - self.score_before, 1)

    def as_dict(self) -> dict:
        return {
            "round": self.round_no,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "delta": self.delta,
            "fixed": self.fixed,
            "introduced": self.introduced,
            "accepted": self.accepted,
            "reason": self.reason,
            "elapsed_s": self.elapsed_s,
        }


@dataclass
class PolishResult:
    """整个收敛过程的产物。"""

    rounds: list[PolishRound] = field(default_factory=list)
    initial_score: float = 0.0
    final_score: float = 0.0
    stop_reason: str = ""
    snapshot_dir: str = ""

    @property
    def total_delta(self) -> float:
        return round(self.final_score - self.initial_score, 1)

    @property
    def accepted_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.accepted)

    def as_dict(self) -> dict:
        return {
            "initial_score": self.initial_score,
            "final_score": self.final_score,
            "total_delta": self.total_delta,
            "rounds": len(self.rounds),
            "accepted_rounds": self.accepted_rounds,
            "stop_reason": self.stop_reason,
            "history": [r.as_dict() for r in self.rounds],
        }


class FixAgent(Protocol):
    """修复器接口。给定问题清单，尝试改代码。

    返回 True 表示「已尝试修改」（是否真的改好由评估器判定），
    返回 False 表示放弃（如问题需要人工判断）。
    """

    def fix(self, report: QualityReport, workspace: Path, round_no: int) -> bool: ...


class NetImprovementGate:
    """净改善门：改动必须让分数上升且不引入 high 级问题。"""

    def __init__(self, min_delta: float = 0.5, allow_new_high: bool = False,
                 behavior_guard: bool = True):
        self.min_delta = min_delta
        self.allow_new_high = allow_new_high
        self.behavior_guard = behavior_guard

    def judge(self, before: QualityReport, after: QualityReport) -> tuple[bool, str]:
        delta = after.score - before.score
        new_high = [
            s for s in after.smells
            if s.severity == "high"
            and not any(
                b.kind == s.kind and b.path == s.path and b.line == s.line
                for b in before.smells
            )
        ]
        if new_high and not self.allow_new_high:
            return False, f"引入 {len(new_high)} 个新 high 级问题，回滚"
        if delta < self.min_delta:
            if delta < 0:
                return False, f"分数下降 {-delta:.1f}，回滚"
            return False, f"改善 {delta:.1f} 未达阈值 {self.min_delta}，回滚"
        return True, f"改善 {delta:+.1f}，接受"

    def judge_sources(self, before_files: dict[str, str],
                      after_files: dict[str, str]) -> tuple[bool, str]:
        """语义守护：分数门看不出「分数没变但行为变了」的破坏。

        ## 为什么必须有这一层（真实事故）

        smokeshow 上实测 codex 的一次「修复」：
        ```
        -import sys
        -        print(e, file=sys.stderr)
        +        print(e)
        ```
        它把错误输出从 stderr 改到 stdout —— 这是**可观测的行为破坏**
        （脚本用户 ripgrep stderr 会失效、管道语义改变），但整洁度分数
        一分没变（分数只看 smell 计数），净改善门因此判「改善不足，回滚」
        而不是「破坏行为，回滚」。归因错了，用户下次还会再踩。

        本方法做 AST 级对比，只查**确定性可判**的破坏：
        1. 函数/类被删（API 消失）
        2. 函数参数签名变化（调用方会炸）
        3. `print(..., file=X)` 的 file 目标被去掉（输出流改变）
        4. `except` 子句被删（异常不再被捕获）

        刻意不做：变量重命名、代码格式化、控制流等价改写 —— 这些无法
        靠 AST 粗比对可靠判定，硬判会误杀正常重构。宁可漏报不可误报。
        """
        if not self.behavior_guard:
            return True, "语义守护已关闭"

        import ast as _ast

        problems: list[str] = []

        def _outline(src: str) -> dict | None:
            """提取「API 面 + 语义敏感点」的摘要。解析失败返回 None。"""
            try:
                tree = _ast.parse(src)
            except SyntaxError:
                return None
            funcs: dict[str, str] = {}
            classes: set[str] = set()
            prints_with_file = 0
            prints_total = 0
            excepts = 0
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef):
                    classes.add(node.name)
                elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    # 签名 = 参数名序列 + 是否有 *args/**kwargs
                    a = node.args
                    sig = ",".join([x.arg for x in a.args])
                    sig += "|" + ",".join(x.arg for x in a.kwonlyargs)
                    if a.vararg:
                        sig += "|*" + a.vararg.arg
                    if a.kwarg:
                        sig += "|**" + a.kwarg.arg
                    funcs[node.name] = sig
                elif isinstance(node, _ast.Call):
                    f = node.func
                    if isinstance(f, _ast.Name) and f.id == "print":
                        prints_total += 1
                        if any(k.arg == "file" for k in node.keywords):
                            prints_with_file += 1
                elif isinstance(node, _ast.ExceptHandler):
                    excepts += 1
            return {"funcs": funcs, "classes": classes,
                    "prints_file": prints_with_file, "prints": prints_total,
                    "excepts": excepts}

        for path, before_src in before_files.items():
            before_out = _outline(before_src)
            after_src = after_files.get(path)
            if before_out is None:
                continue
            if after_src is None:
                problems.append(f"{path} 被删除")
                continue
            after_out = _outline(after_src)
            if after_out is None:
                problems.append(f"{path} 改动后无法解析（语法错）")
                continue
            # 1) API 消失
            gone = set(before_out["funcs"]) - set(after_out["funcs"])
            if gone:
                problems.append(f"{path} 删除了函数：{', '.join(sorted(gone)[:3])}")
            gone_cls = before_out["classes"] - after_out["classes"]
            if gone_cls:
                problems.append(f"{path} 删除了类：{', '.join(sorted(gone_cls)[:3])}")
            # 2) 签名变化
            for name, sig in before_out["funcs"].items():
                new_sig = after_out["funcs"].get(name)
                if new_sig is not None and new_sig != sig:
                    problems.append(f"{path}:{name} 参数签名变化 {sig!r} → {new_sig!r}")
            # 3) print 的 file 目标被去掉（输出流改变）
            #
            # 判据不能加 `prints == prints` 这个前提：实测的破坏恰恰是
            # 连带删掉了 `import sys`，print 调用数不变而 file= 消失。
            # 但反过来，「print 数量减少」也可能是把 print 改成 logger，
            # 属正常重构，不该误杀。故只在「print 总数没减少」时追究 file= 的丢失。
            lost_file = before_out["prints_file"] - after_out["prints_file"]
            if lost_file > 0 and after_out["prints"] >= before_out["prints"]:
                problems.append(
                    f"{path} 有 {lost_file} 处 print 去掉了 file= 参数"
                    "（输出流从 stderr 变成 stdout）")
            # 4) except 被删
            if after_out["excepts"] < before_out["excepts"]:
                problems.append(
                    f"{path} 少了 {before_out['excepts'] - after_out['excepts']} 个 except 子句")

        if problems:
            return False, "破坏行为，回滚：" + "；".join(problems[:3])
        return True, "语义守护通过"


def _read_sources(workspace: Path) -> dict[str, str]:
    """读工作区全部 .py 源码，供语义守护做前后对比。

    键用相对路径（正斜杠归一），保证跨平台一致、且不受工作区绝对路径变化影响。
    读不了的单个文件跳过而非抛错 —— 一个坏文件不该让整轮判定失败。
    """
    from cforge.quality.metrics import iter_python_files

    out: dict[str, str] = {}
    try:
        files = iter_python_files(Path(workspace), max_files=400)
    except Exception:  # noqa: BLE001
        return out
    for p in files:
        try:
            rel = str(p.relative_to(workspace)).replace("\\", "/")
        except ValueError:
            rel = str(p)
        try:
            out[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def _snapshot(workspace: Path, dest: Path) -> int:
    """把当前 .py 文件快照到 dest。返回文件数。用于回滚。

    必须排除 dest 本身 —— 若快照目录建在工作区内部，第二次快照会把
    第一次的快照再复制进去，指数膨胀；_restore 同样会把旧快照写回，
    形成自引用。实测这个 bug 会让测试卡死（文件数指数增长）。
    """
    import shutil

    ws = Path(workspace).resolve()
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for py in ws.rglob("*.py"):
        resolved = py.resolve()
        # 排除快照目录自身
        if dest == resolved or dest in resolved.parents:
            continue
        rel = resolved.relative_to(ws)
        parts = set(rel.parts[:-1])
        if parts & _COPY_EXCLUDE:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(py, target)
        n += 1
    return n


def _restore(snapshot: Path, workspace: Path) -> int:
    """从快照恢复。返回（恢复 + 删除）的文件数。

    ## 必须同时删除「快照里没有、工作区里新增的 .py」

    原实现只恢复快照中存在的文件，刻意不删新增文件，注释理由是
    「宁可能回滚不彻底，也不要误删 agent 的产物」。

    **这个保守策略实测有害**（httpx 上的真实事故）：
    codex 为了改 `_transports/asgi.py`，先写了一个**辅助脚本**
    `_transports/patch_asgi.py`（用 Python 做文本替换）。守门判定回滚后，
    只有 `asgi.py` 被还原，`patch_asgi.py` 被留下 ——

      - 基线 80.5 → 回滚后实际 **71.9（-8.6）**
      - 原因：新增文件带 BOM（`\\ufeff`）且 `import pathlib` 后全是顶层语句，
        被判语法/整洁度问题
      - 更糟的是**报告显示「判定回滚，80.5 → 80.5」**：读者会以为工作区干净，
        实际已被污染。这是「报告与现实不一致」，比分数难看严重得多。

    正确语义：**「回滚」的定义就是回到修改前的状态**，包括不留下任何新文件。
    agent 的「产物」在判定回滚时本来就该被丢弃 —— 否则这个门形同虚设。

    安全边界：只删 `*.py`，且只删快照里没有的；非 .py 文件（可能是用户数据）
    不动，避免误伤。
    """
    import shutil

    snap = Path(snapshot).resolve()
    ws = Path(workspace).resolve()
    n = 0

    # 1) 还愿：快照里有的，覆盖回工作区
    snap_files: set[Path] = set()
    for py in snap.rglob("*.py"):
        rel = py.resolve().relative_to(snap)
        snap_files.add(rel)
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(py, target)
        n += 1

    # 2) 清理：工作区里新增的 .py（快照里没有的）
    for py in ws.rglob("*.py"):
        resolved = py.resolve()
        if snap == resolved or snap in resolved.parents:
            continue  # 别把快照目录自己算进来
        try:
            rel = resolved.relative_to(ws)
        except ValueError:
            continue
        parts = set(rel.parts[:-1])
        if parts & _COPY_EXCLUDE:
            continue
        if rel not in snap_files:
            try:
                resolved.unlink()
                n += 1
            except OSError:
                pass
    return n


@dataclass
class PolishLoop:
    """整洁度收敛循环。"""

    workspace: Path
    agent: FixAgent
    max_rounds: int = 5
    # 连续多少轮改善不足即判定收敛
    patience: int = 2
    gate: NetImprovementGate = field(default_factory=NetImprovementGate)
    snapshot_root: Path | None = None
    # 复制到最终结果的逐轮明细
    verbose: bool = False

    def run(self) -> PolishResult:
        workspace = Path(self.workspace)
        snap_root = self.snapshot_root or (workspace / ".cforge" / "polish_snapshots")

        before = evaluate(workspace)
        result = PolishResult(initial_score=before.score, final_score=before.score)
        stagnant = 0

        if not before.smells:
            result.stop_reason = "无整洁度问题，无需收敛"
            return result

        for i in range(1, self.max_rounds + 1):
            t0 = time.perf_counter()
            snap = snap_root / f"round{i}_before"
            _snapshot(workspace, snap)
            before_src = _read_sources(workspace)

            changed = self.agent.fix(before, workspace, i)
            if not changed:
                result.stop_reason = "修复器放弃（问题需人工判断）"
                break

            after = evaluate(workspace)
            # 先过语义守护（分数门看不出「分数没变但行为变了」），
            # 再过净改善门。顺序有意：行为破坏是最严重的一类，
            # 它必须优先被归因，否则报告只会说「改善不足」。
            guard_ok, guard_reason = self.gate.judge_sources(
                before_src, _read_sources(workspace))
            if not guard_ok:
                accepted, reason = False, guard_reason
            else:
                accepted, reason = self.gate.judge(before, after)
            elapsed = round(time.perf_counter() - t0, 2)

            if not accepted:
                _restore(snap, workspace)
                after = evaluate(workspace)  # 回滚后重新评估，确保状态一致

            delta_info = diff_reports(before, after)
            result.rounds.append(PolishRound(
                round_no=i,
                score_before=before.score,
                score_after=after.score,
                fixed=delta_info["fixed"],
                introduced=delta_info["introduced"],
                accepted=accepted,
                reason=reason,
                elapsed_s=elapsed,
            ))

            if accepted:
                before = after
                result.final_score = after.score
                if self.verbose:
                    print(f"  round {i}: {reason}")
            else:
                if self.verbose:
                    print(f"  round {i}: {reason}（已回滚）")

            # 收敛判定：改善不足累计到 patience 即停
            if abs(after.score - result.rounds[-1].score_before) < self.gate.min_delta:
                stagnant += 1
                if stagnant >= self.patience:
                    result.stop_reason = f"连续 {stagnant} 轮改善不足，判定收敛"
                    break
            else:
                stagnant = 0

            if not before.smells:
                result.stop_reason = "已无问题"
                break
        else:
            result.stop_reason = f"达到最大轮数 {self.max_rounds}"

        if not result.stop_reason:
            result.stop_reason = "结束"
        result.snapshot_dir = str(snap_root)
        return result


class NoOpAgent:
    """占位修复器。用于测试收敛逻辑本身。"""

    def __init__(self, outcomes: list[bool] | None = None):
        self.outcomes = outcomes or []
        self.calls = 0

    def fix(self, report: QualityReport, workspace: Path, round_no: int) -> bool:
        self.calls += 1
        if self.outcomes:
            return self.outcomes[min(round_no - 1, len(self.outcomes) - 1)]
        return False


def quality_gate(report: QualityReport, *, min_score: float = 85.0,
                 max_high: int = 0) -> tuple[bool, str]:
    """验收门：整洁度是否达标。供 Goal Guard 作为 target 判定使用。"""
    highs = report.by_severity("high")
    if len(highs) > max_high:
        return False, f"{len(highs)} 个 high 级问题（上限 {max_high}）"
    if report.score < min_score:
        return False, f"得分 {report.score} 低于目标 {min_score}"
    return True, f"得分 {report.score}，high 级 {len(highs)} 个"
