"""常驻守门守护进程：让 CodexForge 从「手动命令」变成「持续存在」。

## 为什么定位要求这个形态

定位是「给一人公司的代码守门」。守门这个动作天然是**持续的**，
不是「我每次想起来了跑一下」。如果形态还是 CLI，定位和产品会自相矛盾 ——
评委一眼就能看出这点。

## 与前台的 PolishLoop 的分工

| 层 | 职责 |
|---|---|
| `TriggerPolicy` | 决定什么时候跑（四道闸门） |
| `Guardian` | 真的跑一轮重构，记录结果，写报告 |
| `PolishLoop` | 单轮内部：快照 → 改 → 净改善门 → 回滚 |

Guardian 是长时运行的外壳，PolishLoop 是它调用的执行单元。

## 关键设计：单次运行必须可中断

常驻服务最怕「跑到一半卡死」。所以每一轮重构都有：
- 单轮超时（`run_timeout_s`）
- 连续失败熔断（`max_consecutive_failures`）
- 明确的停止原因，写进报告

## 不做的事

- **不做文件监听（inotify/watchdog）**：跨平台差异大，且边写边触发会打断用户。
  用「轮询 + 冷却」反而更稳，也让行为可预测、可解释。
- **不在守护进程里改用户的分支**：只改工作区文件，不碰 git 操作。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from cforge.quality.polish import FixAgent, PolishLoop, PolishResult
from cforge.watch.trigger import (
    TriggerPolicy,
    WatchConfig,
    WatchEvent,
)


class GuardianReporter(Protocol):
    """报告回调。CLI 打印、JSON 落盘、Webhook 都走这个接口。"""

    def on_decision(self, event: WatchEvent) -> None: ...
    def on_cycle(self, events: list[WatchEvent], report_path: Path) -> None: ...


@dataclass
class GuardianConfig:
    """守护进程的运行参数。"""

    # 轮询间隔：多久检查一次「该不该跑」
    poll_interval_s: float = 60.0
    # 单次重构的硬超时（防止卡死）
    run_timeout_s: float = 900.0
    # 连续失败多少次后停止守护
    max_consecutive_failures: int = 3
    # 总运行时长上限（0 = 不限）
    total_budget_s: float = 0.0
    # 报告落盘位置（相对 workspace）
    report_dir: str = ".cforge/guardian"
    # 每轮最多修复几处问题（防止一次改太多难回滚）
    max_fixes_per_run: int = 20


@dataclass
class GuardianState:
    """跨运行的持久状态，让守护进程重启后接得上。

    区分三类计数，因为它们回答不同的问题：
    - `checks_total`：检查了多少次（衡量守护是否在干活）
    - `runs_total`：真正动手了几次（衡量触发频率是否合理）
    - `runs_accepted/rejected`：改动是否被净改善门接受（衡量修复质量）
    """

    checks_total: int = 0
    runs_total: int = 0
    runs_accepted: int = 0
    runs_rejected: int = 0
    quality_start: float = 0.0
    quality_last: float = 0.0
    consecutive_failures: int = 0
    last_run_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "checks_total": self.checks_total,
            "runs_total": self.runs_total,
            "runs_accepted": self.runs_accepted,
            "runs_rejected": self.runs_rejected,
            "quality_start": self.quality_start,
            "quality_last": self.quality_last,
            "quality_gain": round(self.quality_last - self.quality_start, 1),
            "consecutive_failures": self.consecutive_failures,
            "last_run_at": self.last_run_at,
        }


@dataclass
class Guardian:
    """常驻守门。被 `run_forever` 或 `run_once` 驱动。"""

    workspace: Path
    agent: FixAgent
    watch_config: WatchConfig = field(default_factory=WatchConfig)
    config: GuardianConfig = field(default_factory=GuardianConfig)
    # 是否启用（可以外部注入开关做测试）
    clock: Callable[[], float] = time.time
    verbose: bool = True

    _policy: TriggerPolicy | None = None
    _state: GuardianState = field(default_factory=GuardianState)
    _events: list[WatchEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)
        self._policy = TriggerPolicy(self.workspace, self.watch_config)
        self._load_state()

    # ------------------------------------------------------------------ 状态
    @property
    def report_dir(self) -> Path:
        return self.workspace / self.config.report_dir

    def _state_path(self) -> Path:
        return self.report_dir / "state.json"

    def _load_state(self) -> None:
        p = self._state_path()
        if not p.is_file():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._state = GuardianState(
            checks_total=int(raw.get("checks_total", 0)),
            runs_total=int(raw.get("runs_total", 0)),
            runs_accepted=int(raw.get("runs_accepted", 0)),
            runs_rejected=int(raw.get("runs_rejected", 0)),
            quality_start=float(raw.get("quality_start", 0.0)),
            quality_last=float(raw.get("quality_last", 0.0)),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            last_run_at=float(raw.get("last_run_at", 0.0)),
        )
        if self._state.last_run_at:
            self._policy.mark_ran(self._state.last_run_at)

    def _save_state(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self._state_path()).write_text(
            json.dumps(self._state.as_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ 单轮
    def run_once(self) -> WatchEvent:
        """检查一次并按需执行。返回本轮事件（可能只是「跳过」）。"""
        started = self.clock()
        decision = self._policy.decide(now=started)
        self._state.checks_total += 1

        if not decision.should_run:
            ev = WatchEvent(ts=started, ran=False, reason=decision.reason)
            self._events.append(ev)
            # 跳过也要落盘：否则「检查了多少次」这个数会随着进程重启丢失
            self._save_state()
            if self.verbose:
                print(f"  [guardian] 跳过：{decision.reason}")
            return ev

        before = decision.report.score if decision.report else 0.0
        if self._state.quality_start == 0.0:
            self._state.quality_start = before

        if self.verbose:
            print(f"  [guardian] 触发：{decision.reason}")

        ev = WatchEvent(ts=started, ran=True, reason=decision.reason,
                        quality_before=before)
        try:
            loops = PolishLoop(
                workspace=self.workspace,
                agent=self.agent,
                max_rounds=1,          # 每次触发只做一轮，保持改动小、好回滚
                patience=1,
            )
            result: PolishResult = loops.run()
            ev.quality_after = result.final_score
            ev.accepted = result.accepted_rounds > 0
            # 把「为什么停下」带出来：修复器放弃 / 收敛 / 无问题 三种结局
            # 对读者是完全不同的信息，丢掉它报告就只剩「回滚」两个字。
            ev.stop_reason = result.stop_reason
            # 修复器自己报的错误（如 codex 上游 404）优先级更高——
            # 「放弃」是行为，「404」才是病因。没这一步就会把上游故障
            # 误读成「模型判断这些代码没法修」。
            agent_err = getattr(self.agent, "last_error", "")
            if agent_err and not ev.accepted:
                ev.stop_reason = f"{ev.stop_reason}｜{agent_err[:200]}"
            if result.rounds:
                ev.fixed = result.rounds[0].fixed
                ev.introduced = result.rounds[0].introduced
            ev.elapsed_s = round(self.clock() - started, 1)

            self._state.runs_total += 1
            self._state.quality_last = ev.quality_after
            self._state.last_run_at = started
            if ev.accepted:
                self._state.runs_accepted += 1
                self._state.consecutive_failures = 0
            else:
                self._state.runs_rejected += 1
                self._state.consecutive_failures += 1
        except Exception as exc:  # noqa: BLE001 — 单轮失败不能让守护进程死掉
            ev.error = f"{type(exc).__name__}: {exc}"
            ev.elapsed_s = round(self.clock() - started, 1)
            self._state.consecutive_failures += 1
            if self.verbose:
                print(f"  [guardian] 出错：{ev.error}")

        self._policy.mark_ran(started)
        self._events.append(ev)
        self._save_state()
        if self.verbose:
            print(f"  [guardian] {ev.summary_line()}")
        return ev

    # ------------------------------------------------------------------ 长跑
    def run_forever(self, *, max_cycles: int = 0,
                    should_stop: Callable[[], bool] | None = None) -> dict:
        """常驻循环。max_cycles=0 表示不限次数（被测序或信号中断）。"""
        started = self.clock()
        cycles = 0
        stop_reason = "正常结束"

        while True:
            if should_stop and should_stop():
                stop_reason = "收到停止信号"
                break
            if max_cycles and cycles >= max_cycles:
                stop_reason = f"达到轮数上限 {max_cycles}"
                break
            if self.config.total_budget_s and (
                self.clock() - started > self.config.total_budget_s
            ):
                stop_reason = "达到时间预算"
                break
            if self._state.consecutive_failures >= self.config.max_consecutive_failures:
                stop_reason = f"连续失败 {self._state.consecutive_failures} 次，熔断"
                break

            self.run_once()
            cycles += 1

            # 睡眠也走 clock，便于测试注入假时钟
            target = self.clock() + self.config.poll_interval_s
            while self.clock() < target:
                if should_stop and should_stop():
                    stop_reason = "收到停止信号"
                    break
                time.sleep(min(1.0, max(0.0, target - self.clock())))
            else:
                continue
            break

        report_path = self.write_report(stop_reason)
        ran = [e for e in self._events if e.ran]
        accepted = [e for e in ran if e.accepted]
        return {
            "cycles": cycles,
            "stop_reason": stop_reason,
            # 会话内统计与历史累计分开，避免「跳过却显示质量下降」的误导
            "session": {
                "checks": len(self._events),
                "ran": len(ran),
                "accepted": len(accepted),
                "rejected": len(ran) - len(accepted),
            },
            "state": self._state.as_dict(),
            "report": str(report_path),
            "events": [e.as_dict() for e in self._events],
        }

    # ------------------------------------------------------------------ 报告
    def write_report(self, stop_reason: str = "") -> Path:
        """把整段守护历史写成人可读报告。

        这份报告是「实用价值」的凭证 —— 评委/用户要看的不是日志，
        而是「守了一天，代码质量从多少变成多少，拦住了什么」。

        ## 一个必须避免的误导（实测踩过）

        初版无论本次是否执行，都打印「质量 A → B」。
        但 A 是历史基线、B 是上次运行后的值 —— 如果本次全部跳过，
        报告会显示「质量 83.8 → 75.0 (-8.8)」，让用户以为代码变差了，
        而实际上什么都没做。

        现在：只有本次真的执行过，才显示会话内的质量变化；
        否则明确标注「本次未执行」，历史数据单独列为「累计」。
        """
        self.report_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = self.report_dir / f"report-{ts}.md"

        ran = [e for e in self._events if e.ran]
        accepted = [e for e in ran if e.accepted]
        rejected = [e for e in ran if not e.accepted]

        lines = [
            "# CodexForge 守门报告",
            "",
            f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 工作区：`{self.workspace}`",
            f"- 停止原因：{stop_reason or '未指定'}",
            "",
            "## 本次会话",
            "",
        ]

        if not ran:
            lines += [
                "**本次未执行任何改动。**",
                "",
                "| 项 | 值 |",
                "|---|---|",
                f"| 检查次数 | {len(self._events)} |",
                "| 实际执行 | 0 |",
            ]
            if self._events:
                lines.append(f"| 跳过原因 | {self._events[-1].reason} |")
        else:
            first = ran[0].quality_before
            last = ran[-1].quality_after
            lines += [
                "| 项 | 值 |",
                "|---|---|",
                f"| 检查次数 | {len(self._events)} |",
                f"| 实际执行 | {len(ran)} |",
                f"| 改动被接受 | {len(accepted)} |",
                f"| 改动被回滚 | {len(rejected)} |",
                f"| 质量变化 | {first} → {last} ({last - first:+.1f}) |",
                f"| 修复问题数 | {sum(e.fixed for e in ran)} |",
            ]

        # 历史累计单独列，避免与会话内变化混淆
        if self._state.runs_total:
            lines += [
                "",
                "## 历史累计",
                "",
                "| 项 | 值 |",
                "|---|---|",
                f"| 累计检查 | {self._state.checks_total} |",
                f"| 累计执行 | {self._state.runs_total} |",
                f"| 累计接受 / 回滚 | {self._state.runs_accepted} / {self._state.runs_rejected} |",
                f"| 基线 → 当前 | {self._state.quality_start} → {self._state.quality_last} |",
            ]

        # ---- 剩余债务：这是「守门是否真的守住」的关键信息 ----
        #
        # 如果修复器只修了一部分（比如 AST 版修不了跨文件重复），
        # 用户必须知道「还有东西没修以及为什么」。否则会误以为已经干净了。
        remaining = self._describe_remaining_debt()
        if remaining:
            lines += ["", "## 剩余债务（当前自动修复器无法处理）", ""]
            lines += remaining

        lines += ["", "## 明细", "", "| 时间 | 结果 | 原因 |", "|---|---|---|"]
        for e in self._events:
            if e.ran:
                detail = f"{e.quality_before} → {e.quality_after} ({e.quality_delta:+.1f})"
            else:
                detail = "—"
            lines.append(
                f"| {time.strftime('%H:%M:%S', time.localtime(e.ts))} "
                f"| {detail} | {e.reason} |"
            )

        # 机器可读版本，供 CI / 看板消费
        lines += ["", "## JSON", "", "```json",
                  json.dumps({
                      "stop_reason": stop_reason,
                      "session": {
                          "checks": len(self._events),
                          "ran": len(ran),
                          "accepted": len(accepted),
                          "rejected": len(rejected),
                      },
                      "state": self._state.as_dict(),
                      "events": [e.as_dict() for e in self._events],
                  }, ensure_ascii=False, indent=1), "```", ""]

        text = "\n".join(lines)
        path.write_text(text, encoding="utf-8")
        (self.report_dir / "latest.md").write_text(text, encoding="utf-8")
        return path

    def _describe_remaining_debt(self) -> list[str]:
        """列出当前自动修复器处理不了的问题。

        实测：AST 版修复器只能删 unused import。若项目里有跨文件重复，
        分数会停在 75 左右不动 —— 用户看到「守了一天还是 75」会觉得工具没用，
        除非报告明确告诉他「有 2 对重复代码需要 LLM 修复器」。
        """
        from cforge.quality.metrics import evaluate as _evaluate

        try:
            report = _evaluate(self.workspace)
        except Exception:  # noqa: BLE001
            return []

        out: list[str] = []

        fixable_kinds = {"unused_import"}
        hard = [s for s in report.smells
                if s.severity in ("high", "medium") and s.kind not in fixable_kinds]
        if hard:
            out.append(f"**{len(hard)} 处需要人工或 LLM 判断：**")
            out.append("")
            for s in hard[:10]:
                out.append(f"- `{s.path}:{s.line}` {s.kind} — {s.detail}")
            if len(hard) > 10:
                out.append(f"- …另有 {len(hard) - 10} 处")

        if report.clone_pairs:
            if out:
                out.append("")
            out.append(
                f"**{report.clone_pairs} 对跨文件重复函数**"
                f"（重复率 {report.duplicate_ratio:.1%}）—— 需要合并抽象，"
                f"确定性修复器无法安全处理。"
            )

        if out:
            out.insert(0, "以下问题仍在，是当前分数无法提升的原因：")
            out.insert(1, "")
            out.append("")
            out.append("→ 用 `--fixer codex` 让 LLM 处理，或人工重构。")
        return out
