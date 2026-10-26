"""长程循环单测：验证「自主反馈」真的会改变下一步行为。

用假 runner 模拟各种失败模式，断言循环的响应是对的：
- 目标未达成 → 继续，且注入未达成项
- 工作区不变 → 触发停滞阶梯
- 整洁度崩塌 → 停止而不是继续糟蹋代码
- 声称完成但证据不足 → 记录教训，不接受
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cforge.longrun.loop import (
    LongRunConfig,
    LongRunLoop,
    StopReason,
    build_advice,
    detect_long_run,
    Feedback,
    TurnRecord,
)
from cforge.longrun.state import TargetState


class ScriptedRunner:
    """按脚本执行：每次 run_turn 调一个动作，记录收到的 prompt。

    动作类型：
    - callable(state) -> (summary, claimed_done)：最灵活
    - Exception 实例：抛出
    - 其它：当作 summary 字符串，claimed_done=False
    """

    def __init__(self, actions: list[object]):
        self.actions = actions
        self.calls = 0
        self.prompts: list[str] = []

    def run_turn(self, prompt: str, state: TargetState) -> tuple[str, bool]:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        action = self.actions[idx]
        if callable(action):
            return action(state)
        if isinstance(action, Exception):
            raise action
        return str(action), False


class TupleRunner:
    """直接返回固定 (summary, claimed_done) 的 runner。

    单独一个类是因为 ScriptedRunner 会把非 callable 的动作当成纯字符串 ——
    初版测试用 ScriptedRunner 传元组，结果被 str() 包了一层，
    claimed_done 永远是 False，测试假绿/假红都出现过。
    """

    def __init__(self, summary: str = "done", claimed: bool = True):
        self.summary = summary
        self.claimed = claimed
        self.calls = 0
        self.prompts: list[str] = []

    def run_turn(self, prompt: str, state: TargetState) -> tuple[str, bool]:
        self.prompts.append(prompt)
        self.calls += 1
        return self.summary, self.claimed


def write(ws: Path, name: str, content: str) -> None:
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def goal_needs_file(ws: Path) -> tuple[bool, list[str]]:
    ok = (ws / "target.py").is_file()
    return ok, [] if ok else ["target.py 不存在"]


class TestGoalDrivenLoop:
    def test_stops_when_goal_met(self, tmp_path):
        def make(state):
            write(tmp_path, "target.py", "x = 1\n")
            return "created", True

        runner = ScriptedRunner([make])
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=10), target="create target.py",
        )
        result = loop.run()
        assert result.goal_met is True
        assert result.stop_reason == StopReason.GOAL_MET.value
        assert result.turns == 1

    def test_continues_while_goal_unmet(self, tmp_path):
        runner = ScriptedRunner(["noop"] * 5)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=3, patience=99), target="t",
        )
        result = loop.run()
        assert result.goal_met is False
        assert result.turns == 3
        assert result.stop_reason == StopReason.BUDGET_TURNS.value

    def test_unmet_list_appears_in_feedback(self, tmp_path):
        runner = ScriptedRunner(["noop"] * 3)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=2, patience=99), target="t",
        )
        result = loop.run()
        assert "target.py 不存在" in result.records[0].feedback.unmet

    def test_prompt_contains_state_after_first_turn(self, tmp_path):
        """第二轮起，prompt 里应带上第一轮的失败方向 —— 这是跨轮记忆的证据。"""
        def fail_with_lesson(state):
            state.record_direction("try parsing with regex", "failed", "regex too slow")
            return "tried regex", False

        runner = ScriptedRunner([fail_with_lesson, "noop", "noop"])
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=3, patience=99), target="parse it",
        )
        loop.run()
        assert len(runner.prompts) >= 2
        second = runner.prompts[1]
        assert "已失败的方向" in second or "regex" in second


class TestStagnation:
    def test_restarts_after_three_stagnant_turns(self, tmp_path):
        """工作区完全不变时，应在第 3 次检测后停止（level 3 = 重启）。"""
        runner = ScriptedRunner(["noop"] * 10)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=10, patience=99), target="t",
        )
        result = loop.run()
        assert result.stop_reason == StopReason.STAGNATION.value
        assert result.turns <= 4

    def test_progress_prevents_stagnation_stop(self, tmp_path):
        """每轮都改文件，就不该被判停滞。"""
        counter = {"n": 0}

        def make_progress(state):
            counter["n"] += 1
            write(tmp_path, "work.py", f"x = {counter['n']}\n")
            return "progress", False

        runner = ScriptedRunner([make_progress] * 20)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=5, patience=99), target="t",
        )
        result = loop.run()
        assert result.stop_reason == StopReason.BUDGET_TURNS.value

    def test_stagnation_level_recorded(self, tmp_path):
        runner = ScriptedRunner(["noop"] * 5)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=3, patience=99), target="t",
        )
        result = loop.run()
        levels = [r.feedback.stagnation_level for r in result.records]
        assert max(levels) >= 1


class TestQualityGuard:
    def test_stops_on_quality_collapse(self, tmp_path):
        """把代码写成灾难时，应停下来而不是继续产出。"""
        bad = "import os\n" * 40 + "try:\n    pass\nexcept:\n    pass\n"

        def wreck(state):
            write(tmp_path, "bad.py", bad)
            return "made a mess", False

        runner = ScriptedRunner([wreck] * 5)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=6, min_quality=99.0, patience=99),
            target="t",
        )
        result = loop.run()
        assert result.stop_reason == StopReason.QUALITY_REGRESSION.value

    def test_quality_tracked_per_turn(self, tmp_path):
        runner = ScriptedRunner(["noop"] * 3)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=2, patience=99), target="t",
        )
        result = loop.run()
        assert len(result.records) == 2
        for r in result.records:
            assert 0 <= r.feedback.quality_score <= 100


class TestClaimWithoutEvidence:
    def test_claimed_done_without_goal_records_lesson(self, tmp_path):
        runner = TupleRunner("I finished it", claimed=True)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=2, patience=99), target="t",
        )
        result = loop.run()
        state = TargetState.load(tmp_path)
        assert any("声称完成" in lesson for lesson in state.lessons)
        assert result.goal_met is False

    def test_claim_not_accepted_as_goal_met(self, tmp_path):
        runner = TupleRunner("done!", claimed=True)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=2, patience=99), target="t",
        )
        result = loop.run()
        assert not result.goal_met


class TestErrorHandling:
    def test_turn_exception_does_not_kill_loop(self, tmp_path):
        runner = ScriptedRunner([RuntimeError("boom"), "noop", "noop"])
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=3, patience=99), target="t",
        )
        result = loop.run()
        assert result.turns >= 2
        assert "boom" in result.records[0].error

    def test_state_persisted_after_each_turn(self, tmp_path):
        runner = ScriptedRunner(["noop"] * 3)
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=2, patience=99), target="my goal",
        )
        loop.run()
        state = TargetState.load(tmp_path)
        assert state.target == "my goal"
        assert state.turn >= 2


class TestAdvice:
    def test_stagnation_advice_has_priority(self):
        a = build_advice({"stagnation_level": 2, "high_smells": 5,
                          "quality_delta": -10, "unmet": ["x"], "gate_clean": True})
        assert "放弃当前方案" in a

    def test_high_smells_second_priority(self):
        a = build_advice({"stagnation_level": 0, "high_smells": 3,
                          "quality_delta": -10, "unmet": ["x"], "gate_clean": True})
        assert "high 级" in a

    def test_quality_drop_third(self):
        a = build_advice({"stagnation_level": 0, "high_smells": 0,
                          "quality_delta": -5, "unmet": ["x"], "gate_clean": True})
        assert "整洁度下降" in a

    def test_unmet_goal_mentioned(self):
        a = build_advice({"stagnation_level": 0, "high_smells": 0,
                          "quality_delta": 0, "unmet": ["missing feature"],
                          "gate_clean": True})
        assert "missing feature" in a

    def test_all_clear_gives_empty(self):
        a = build_advice({"stagnation_level": 0, "high_smells": 0,
                          "quality_delta": 0, "unmet": [], "gate_clean": True})
        assert a == ""

    def test_only_one_advice_at_a_time(self):
        """给多条等于没给 —— agent 会挑最省力的。"""
        a = build_advice({"stagnation_level": 2, "high_smells": 3,
                          "quality_delta": -5, "unmet": ["x"], "gate_clean": False})
        assert a.count("。") <= 1 or "放弃" in a


class TestDetectLongRun:
    def test_turn_count_threshold(self):
        recs = [TurnRecord(i, 0, 1, "fp", Feedback(i, False, [], 100, 0, 0, True, 0))
                for i in range(8)]
        assert detect_long_run(recs, 100000, 0) is True

    def test_few_turns_low_tokens_not_long(self):
        recs = [TurnRecord(i, 0, 1, "fp", Feedback(i, False, [], 100, 0, 0, True, 0))
                for i in range(3)]
        assert detect_long_run(recs, 100000, 1000) is False

    def test_high_token_usage_counts_as_long(self):
        recs = [TurnRecord(0, 0, 1, "fp", Feedback(0, False, [], 100, 0, 0, True, 0))]
        assert detect_long_run(recs, 1000, 4000) is True


class TestResultSerialization:
    def test_as_dict_roundtrip(self, tmp_path):
        import json
        runner = ScriptedRunner(["noop"])
        loop = LongRunLoop(
            workspace=tmp_path, runner=runner, goal_checker=goal_needs_file,
            config=LongRunConfig(max_turns=1, patience=99), target="t",
        )
        d = loop.run().as_dict()
        assert json.loads(json.dumps(d)) == d
        assert "stop_reason" in d and "history" in d
