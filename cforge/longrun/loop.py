"""长程自主反馈循环：把「单轮执行」升级成「多轮自我纠偏」。

## 与普通 agent loop 的区别

普通 loop 是「模型说完了就结束」。这里的 loop 有三个额外机制：

1. **每轮都有确定性反馈信号**（不是模型自评）
   - 目标证据：文件是否生成、测试是否通过
   - 整洁度分数：代码是否变差
   - 门禁结果：语法/结构是否合规
2. **反馈会改变下一轮的输入**：未达成项 + 已失败方向 + 停滞警告
3. **有预算与收敛判定**：不会无限跑，也不会在无改善时浪费额度

## 超长程的判定标准

一个任务算「超长程」，需满足任一：
- 轮数 ≥ 8，或
- 累计 token > 该模型上下文的 3 倍（必然经历多次压缩）

这个定义很重要，因为它决定了要不要启用状态外置。
短任务用外置状态只是额外开销。

## 反馈质量的分级

反馈不是「有/无」，而是分级：
- L0 无反馈：模型自己判断 —— 长任务必失败
- L1 单一信号：只看文件存在 —— 会产出「能跑但很脏」的代码
- L2 多信号：目标 + 整洁度 + 门禁 —— **本实现**
- L3 带历史：L2 + 跨轮记忆 + 停滞检测 —— **本实现**（长程必需）
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from cforge.longrun.state import (
    StagnationResponse,
    TargetState,
    stagnation_ladder,
    workspace_fingerprint,
)
from cforge.quality.metrics import QualityReport, evaluate


class StopReason(str, Enum):
    GOAL_MET = "goal_met"
    BUDGET_TURNS = "budget_turns"
    BUDGET_TIME = "budget_time"
    STAGNATION = "stagnation"
    AGENT_GAVE_UP = "agent_gave_up"
    QUALITY_REGRESSION = "quality_regression"
    ERROR = "error"


@dataclass
class Feedback:
    """一轮的反馈包。这是「自主反馈」的实体。"""

    turn: int
    goal_met: bool
    unmet: list[str]
    quality_score: float
    quality_delta: float
    high_smells: int
    gate_clean: bool
    stagnation_level: int
    # 给 agent 的可执行建议
    advice: str = ""

    def as_dict(self) -> dict:
        return {
            "turn": self.turn, "goal_met": self.goal_met, "unmet": self.unmet,
            "quality_score": self.quality_score,
            "quality_delta": self.quality_delta,
            "high_smells": self.high_smells,
            "gate_clean": self.gate_clean,
            "stagnation_level": self.stagnation_level,
            "advice": self.advice[:200],
        }


@dataclass
class TurnRecord:
    """一轮的完整记录。审计链的基本单元。"""

    turn: int
    started_at: float
    elapsed_s: float
    fingerprint: str
    feedback: Feedback
    agent_summary: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "turn": self.turn,
            "elapsed_s": self.elapsed_s,
            "fingerprint": self.fingerprint,
            "feedback": self.feedback.as_dict(),
            "agent_summary": self.agent_summary[:300],
            "error": self.error[:200],
        }


class TurnRunner(Protocol):
    """执行一轮的接口。返回「本轮做了什么」的简述与是否声称完成。"""

    def run_turn(self, prompt: str, state: TargetState) -> tuple[str, bool]: ...


@dataclass
class LongRunConfig:
    """长程循环的预算与阈值。"""

    max_turns: int = 40
    max_wall_s: float = 3600.0
    # 整洁度不允许低于这个分；低于即判质量回退
    min_quality: float = 60.0
    # 连续多少轮无改善即停
    patience: int = 3
    # 目标未达成但整洁度持续下降时，最多容忍几轮
    quality_regression_tolerance: int = 2
    state_dir: str = ".opencode"

    @property
    def is_long_run(self) -> bool:
        return self.max_turns >= 8


@dataclass
class LongRunResult:
    """长程运行的最终产物。"""

    stop_reason: str
    turns: int
    goal_met: bool
    initial_quality: float
    final_quality: float
    records: list[TurnRecord] = field(default_factory=list)
    state_file: str = ""
    elapsed_s: float = 0.0

    @property
    def quality_delta(self) -> float:
        return round(self.final_quality - self.initial_quality, 1)

    def as_dict(self) -> dict:
        return {
            "stop_reason": self.stop_reason,
            "turns": self.turns,
            "goal_met": self.goal_met,
            "initial_quality": self.initial_quality,
            "final_quality": self.final_quality,
            "quality_delta": self.quality_delta,
            "elapsed_s": self.elapsed_s,
            "state_file": self.state_file,
            "history": [r.as_dict() for r in self.records],
        }


def build_advice(fb_parts: dict[str, Any]) -> str:
    """把多路信号合成一句可执行的建议。

    原则：同一轮只给**一条**最重要的建议。给五条等于没给 ——
    agent 会挑最省力的那条做。
    """
    if fb_parts["stagnation_level"] >= 2:
        return "工作区连续无变化：立即放弃当前方案，换一条不同的实现路径。"
    if fb_parts["high_smells"] > 0:
        return f"有 {fb_parts['high_smells']} 个 high 级代码问题（长函数/裸 except），先修这些。"
    if fb_parts["quality_delta"] < -2:
        return f"本轮代码整洁度下降 {-fb_parts['quality_delta']:.1f} 分，先清理再加功能。"
    if fb_parts["unmet"]:
        top = fb_parts["unmet"][0]
        return f"优先完成未达成目标：{top}"
    if not fb_parts["gate_clean"]:
        return "编辑门禁报错，先修语法/结构问题。"
    return ""


def detect_long_run(turn_records: list[TurnRecord], context_limit: int,
                    total_tokens: int) -> bool:
    """判定是否属于「超长程」任务。

    两个条件任一成立即为长程：
    - 轮数 ≥ 8（必然经历多次上下文压缩）
    - 累计 token 超过上下文窗口 3 倍
    """
    if len(turn_records) >= 8:
        return True
    if context_limit > 0 and total_tokens > context_limit * 3:
        return True
    return False


@dataclass
class LongRunLoop:
    """长程自主反馈循环。"""

    workspace: Path
    runner: TurnRunner
    goal_checker: Callable[[Path], tuple[bool, list[str]]]
    config: LongRunConfig = field(default_factory=LongRunConfig)
    # 初始目标，写进状态供重启注入
    target: str = ""
    verbose: bool = False
    _state: TargetState | None = None

    @property
    def state(self) -> TargetState:
        if self._state is None:
            self._state = TargetState.load(self.workspace)
            if self.target and not self._state.target:
                self._state.target = self.target
        return self._state

    def run(self) -> LongRunResult:
        ws = Path(self.workspace)
        state = self.state
        started = time.time()

        initial_q = evaluate(ws).score
        records: list[TurnRecord] = []
        stop_reason = StopReason.BUDGET_TURNS.value
        goal_met = False
        prev_q = initial_q
        no_improve = 0
        q_regressions = 0
        prev_fp = ""

        for turn in range(1, self.config.max_turns + 1):
            if time.time() - started > self.config.max_wall_s:
                stop_reason = StopReason.BUDGET_TIME.value
                break

            state.turn = turn
            t0 = time.time()

            # --- 组装本轮 prompt：包含跨轮记忆 ---
            inject = state.inject_block()
            prompt = inject if inject.strip() else self.target

            error = ""
            summary = ""
            claimed_done = False
            try:
                summary, claimed_done = self.runner.run_turn(prompt, state)
            except Exception as exc:  # noqa: BLE001 — 单轮失败不该终止长跑
                error = f"{type(exc).__name__}: {exc}"

            # --- 收集确定性反馈 ---
            fp = workspace_fingerprint(ws)
            stagnated = state.push_fingerprint(fp)
            ladder: StagnationResponse = stagnation_ladder(state.stagnation_count)

            qr: QualityReport = evaluate(ws)
            ok, unmet = self.goal_checker(ws)
            goal_met = ok
            q_delta = round(qr.score - prev_q, 1)

            fb = Feedback(
                turn=turn,
                goal_met=ok,
                unmet=unmet,
                quality_score=qr.score,
                quality_delta=q_delta,
                high_smells=len(qr.by_severity("high")),
                gate_clean=not any(s.kind == "syntax_error" for s in qr.smells),
                stagnation_level=ladder.level,
                advice="",
            )
            fb.advice = build_advice({
                "stagnation_level": ladder.level,
                "high_smells": fb.high_smells,
                "quality_delta": q_delta,
                "unmet": unmet,
                "gate_clean": fb.gate_clean,
            })

            records.append(TurnRecord(
                turn=turn,
                started_at=t0,
                elapsed_s=round(time.time() - t0, 2),
                fingerprint=fp,
                feedback=fb,
                agent_summary=summary,
                error=error,
            ))
            state.save(ws)

            if self.verbose:
                print(f"  turn {turn:>2}: goal={ok} q={qr.score:.1f}({q_delta:+.1f}) "
                      f"stagn={ladder.level} {fb.advice[:50]}")

            # --- 记录本轮事实（必须在终止判断之前）---
            # 踩过的坑：初版把这条放在终止判断之后，导致「最后一轮声称完成
            # 但目标未达成」这个最重要的事实被 break 跳过，教训丢失。
            if claimed_done and not ok:
                state.add_lesson(f"第{turn}轮声称完成但目标未达成：{', '.join(unmet[:3])}")

            # --- 终止判定（顺序重要：先看成功，再看不该继续的） ---
            if ok:
                stop_reason = StopReason.GOAL_MET.value
                break

            if error and "fatal" in error.lower():
                stop_reason = StopReason.ERROR.value
                break

            if qr.score < self.config.min_quality:
                stop_reason = StopReason.QUALITY_REGRESSION.value
                break

            if q_delta < -2:
                q_regressions += 1
                if q_regressions > self.config.quality_regression_tolerance:
                    stop_reason = StopReason.QUALITY_REGRESSION.value
                    break
            else:
                q_regressions = 0

            if ladder.should_restart:
                stop_reason = StopReason.STAGNATION.value
                break

            # 收敛：目标与质量都无进展
            if stagnated and abs(q_delta) < 0.5:
                no_improve += 1
                if no_improve >= self.config.patience:
                    stop_reason = StopReason.STAGNATION.value
                    break
            else:
                no_improve = 0

            prev_q = qr.score
            prev_fp = fp

        final_q = evaluate(ws).score
        state.status = "done" if goal_met else "stopped"
        state_file = state.save(ws)

        return LongRunResult(
            stop_reason=stop_reason,
            turns=len(records),
            goal_met=goal_met,
            initial_quality=initial_q,
            final_quality=final_q,
            records=records,
            state_file=str(state_file),
            elapsed_s=round(time.time() - started, 2),
        )
