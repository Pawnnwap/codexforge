"""监督循环骨架：propose → validate → execute → audit。

## 从 super-opencode 继承的核心纪律

`Goal Guard` 的核心主张是：**agent 说「完成了」只是提案，不是事实。**
必须由确定性证据校验后才能接受。这条纪律在骨架里完整保留，
因为它直接对应赛道「完整性 30% · 成果可复现性」。

## 本骨架与 super-opencode 的差异

| 维度 | super-opencode | CodexForge |
|---|---|---|
| 执行后端 | opencode / codex 双引擎 | **仅 codex** |
| provider | OpenAI 兼容（含免费 tier） | **书生 / OpenRouter 可切换** |
| 门禁 | 内置 9 工具扫描器 | **外挂 8 语言 PostToolUse 门禁** |
| 判官证据 | JudgeHarness 注册表 | 保留同名概念，骨架化 |

## 骨架边界（明确不做的）

- 判官不实际调用 LLM（`JudgeStub` 只做确定性检查）
- 不做上下文压缩
- 不做自演化循环

这些留接口，实现待下一步。**先让架构跑通，再加智能。**
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class Phase(str, Enum):
    INIT = "init"
    PLAN = "plan"
    EXECUTE = "execute"
    JUDGE = "judge"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Target:
    """一个可验证的目标。必须能被确定性证据判定。"""

    id: str
    description: str
    # 判定方式：目前支持 file_exists / file_contains / cmd_exit_zero / gate_clean
    check: str
    # check 的参数，如 {"path": "src/app.py"} 或 {"cmd": "pytest -q"}
    params: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0


@dataclass
class Evidence:
    """一条确定性证据。"""

    target_id: str
    satisfied: bool
    detail: str
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "satisfied": self.satisfied,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class Verdict:
    """一轮监督的裁决。"""

    phase: Phase
    targets_met: list[str] = field(default_factory=list)
    targets_unmet: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    done: bool = False
    # 判定为 done 但证据不足时置 True，避免伪装成已验证
    unverified: bool = False
    next_action: str = ""
    turn: int = 0

    def summary_line(self) -> str:
        met = len(self.targets_met)
        total = met + len(self.targets_unmet)
        flag = " [UNVERIFIED]" if self.unverified else ""
        return f"[{self.phase.value}] {met}/{total} targets met{flag}"


@dataclass
class Protocol:
    """用户与系统之间的契约。对应 super-opencode 的 protocol.md。"""

    input_desc: str = ""
    targets: list[Target] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["## INPUT", "", self.input_desc or "(未填写)", "", "## TARGET", ""]
        for i, t in enumerate(self.targets, 1):
            lines.append(f"{i}. {t.description}")
            lines.append(f"   - 判定：`{t.check}` {t.params}")
        lines += ["", "## RESTRICTIONS", ""]
        lines += [f"- {r}" for r in self.restrictions] or ["(无)"]
        return "\n".join(lines)


class EvidenceProvider(Protocol):
    """证据提供者。骨架阶段只有确定性检查实现。"""

    def collect(self, target: Target, workspace: Path) -> Evidence: ...


@dataclass
class DeterministicProvider:
    """不依赖 LLM 的证据收集器。这是 Goal Guard 的地基。"""

    def collect(self, target: Target, workspace: Path) -> Evidence:
        check = target.check
        params = target.params

        if check == "file_exists":
            path = workspace / str(params.get("path", ""))
            ok = path.is_file()
            return Evidence(target.id, ok, f"{path} {'存在' if ok else '缺失'}", "file_exists")

        if check == "file_contains":
            path = workspace / str(params.get("path", ""))
            needle = str(params.get("needle", ""))
            if not path.is_file():
                return Evidence(target.id, False, f"{path} 不存在", "file_contains")
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                return Evidence(target.id, False, f"读取失败：{exc}", "file_contains")
            ok = needle in text
            return Evidence(target.id, ok, f"查找 {needle!r} → {'命中' if ok else '未命中'}",
                            "file_contains")

        if check == "cmd_exit_zero":
            import subprocess
            cmd = params.get("cmd")
            if not cmd:
                return Evidence(target.id, False, "cmd 参数为空", "cmd_exit_zero")
            try:
                proc = subprocess.run(
                    cmd, shell=isinstance(cmd, str), cwd=str(workspace),
                    capture_output=True, text=True, timeout=int(params.get("timeout", 120)),
                )
                ok = proc.returncode == 0
                tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
                return Evidence(target.id, ok,
                                f"exit={proc.returncode} | {' / '.join(tail)[:200]}",
                                "cmd_exit_zero")
            except subprocess.TimeoutExpired:
                return Evidence(target.id, False, "命令超时", "cmd_exit_zero")
            except Exception as exc:  # noqa: BLE001
                return Evidence(target.id, False, f"{type(exc).__name__}: {exc}", "cmd_exit_zero")

        if check == "gate_clean":
            # 由外部注入 gate 执行结果；骨架阶段用 params 里的预置结果
            ok = bool(params.get("clean", False))
            return Evidence(target.id, ok, str(params.get("detail", "")), "gate_clean")

        return Evidence(target.id, False, f"未知 check 类型：{check}", "unknown")


@dataclass
class SupervisorLoop:
    """单轮监督循环。骨架实现：跑一轮、判一次、给出 next_action。"""

    protocol: Protocol
    workspace: Path
    provider: EvidenceProvider = field(default_factory=DeterministicProvider)
    # 判官的「完成提案」最多被驳回几次
    max_blocked_stops: int = 2
    # 是否要求工作区有实际变化（防止 agent 只说话不动手）
    require_changes: bool = False
    turn: int = 0
    blocked_stops: int = 0
    audit: list[dict[str, Any]] = field(default_factory=list)

    def evaluate(self, *, claimed_done: bool = False) -> Verdict:
        """收集证据 → 裁决。

        关键纪律：`claimed_done=True` 只是提案。只要有一条目标未满足，
        就拒绝 DONE，并把未满足项写进 next_action。
        """
        self.turn += 1
        evidences: list[Evidence] = []
        met: list[str] = []
        unmet: list[str] = []

        for target in self.protocol.targets:
            ev = self.provider.collect(target, self.workspace)
            evidences.append(ev)
            (met if ev.satisfied else unmet).append(target.id)

        done = not unmet
        unverified = False

        if claimed_done and unmet:
            self.blocked_stops += 1
            if self.blocked_stops > self.max_blocked_stops:
                # super-opencode 的做法：超过上限后接受但标记 unverified，
                # 既不让循环无限跑，也不谎报「已验证通过」
                done = True
                unverified = True

        if claimed_done and done and not evidences:
            # 没有任何目标却声称完成 → 无可验证证据
            unverified = True

        verdict = Verdict(
            phase=Phase.DONE if done else Phase.EXECUTE,
            targets_met=met,
            targets_unmet=unmet,
            evidence=evidences,
            done=done,
            unverified=unverified,
            next_action=(
                "" if done
                else "继续完成未满足目标：" + ", ".join(unmet)
            ),
            turn=self.turn,
        )
        self.audit.append({
            "ts": time.time(),
            "turn": self.turn,
            "claimed_done": claimed_done,
            "done": done,
            "unverified": unverified,
            "met": met,
            "unmet": unmet,
            "evidence": [e.as_dict() for e in evidences],
        })
        return verdict

    def audit_tail(self, n: int = 5) -> list[dict[str, Any]]:
        return self.audit[-n:]

    def inject_prompt(self) -> str:
        """生成给 agent 的约束提示（含 protocol 与未满足项）。"""
        lines = [
            "## 任务契约",
            self.protocol.to_markdown(),
            "",
            "## 纪律",
            "- 声称完成不会被视为完成。完成必须由确定性证据证明。",
            "- 每次改动后，编辑门禁会自动检查文件；按提示修正。",
        ]
        if self.audit:
            last = self.audit[-1]
            if last["unmet"]:
                lines.append(f"- 上一轮未满足：{', '.join(last['unmet'])}")
        return "\n".join(lines)
