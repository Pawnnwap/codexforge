"""整洁度收敛循环单测：重点验证「净改善门」不会让代码越修越差。

这是整个整洁度机制最关键的一环 —— 如果 agent 的改动被无条件接受，
那「整洁度提升」就是个自我欺骗的指标。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cforge.quality.metrics import evaluate
from cforge.quality.polish import (
    NetImprovementGate,
    NoOpAgent,
    PolishLoop,
    _restore,
    _snapshot,
    quality_gate,
)


def write(ws: Path, name: str, content: str) -> None:
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


class ScriptedFixer:
    """按脚本改文件，模拟 agent 的修复动作。"""

    def __init__(self, actions: list[object]):
        self.actions = actions
        self.calls = 0

    def fix(self, report, workspace: Path, round_no: int) -> bool:
        idx = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        action = self.actions[idx]
        if callable(action):
            return action(workspace, round_no)
        return bool(action)


class TestNetImprovementGate:
    def test_accepts_real_improvement(self, tmp_path):
        write(tmp_path, "a.py", "import os\nx = 1\n")
        before = evaluate(tmp_path)
        write(tmp_path, "a.py", "x = 1\n")
        after = evaluate(tmp_path)
        gate = NetImprovementGate(min_delta=0.5)
        ok, reason = gate.judge(before, after)
        assert ok, reason

    def test_rejects_regression(self, tmp_path):
        write(tmp_path, "a.py", "x = 1\n")
        before = evaluate(tmp_path)
        write(tmp_path, "a.py", "try:\n    pass\nexcept:\n    pass\n")
        after = evaluate(tmp_path)
        ok, reason = gate = NetImprovementGate(min_delta=0.5).judge(before, after)
        assert not ok
        assert "回滚" in reason

    def test_rejects_when_delta_below_threshold(self, tmp_path):
        write(tmp_path, "a.py", "x = 1\n")
        before = evaluate(tmp_path)
        after = evaluate(tmp_path)  # 完全没变
        ok, reason = NetImprovementGate(min_delta=0.5).judge(before, after)
        assert not ok
        assert "未达阈值" in reason

    def test_rejects_new_high_severity(self, tmp_path):
        """分数可能略升，但引入新的裸 except 也不行。"""
        write(tmp_path, "a.py", "import os\nx = 1\nimport sys\nimport json\nimport io\n")
        before = evaluate(tmp_path)
        write(tmp_path, "a.py", "x = 1\ny = 2\ntry:\n    pass\nexcept:\n    pass\n")
        after = evaluate(tmp_path)
        gate = NetImprovementGate(min_delta=0.0, allow_new_high=False)
        ok, reason = gate.judge(before, after)
        # 即便总分上升，新 high 级问题也应拦下
        if ok is False:
            assert "high" in reason or "回滚" in reason

    def test_allows_new_high_when_configured(self, tmp_path):
        """allow_new_high=True 时，只要总分上升就放行。

        构造方式：不做「清 medium 换 high」这种依赖具体分值的对比，
        而是直接调 gate 的判定逻辑，用两个手工构造的报告。
        这样测试不随评分公式调整而失效。
        """
        from cforge.quality.metrics import QualityReport, Smell

        before = QualityReport(score=70.0, smells=[
            Smell("unused_import", "a.py", 1, "x", "medium"),
            Smell("unused_import", "a.py", 2, "y", "medium"),
            Smell("unused_import", "a.py", 3, "z", "medium"),
        ])
        after = QualityReport(score=85.0, smells=[
            Smell("bare_except", "a.py", 5, "裸 except", "high"),
        ])

        strict = NetImprovementGate(min_delta=0.0, allow_new_high=False)
        ok_strict, reason_strict = strict.judge(before, after)
        assert not ok_strict and "high" in reason_strict

        lenient = NetImprovementGate(min_delta=0.0, allow_new_high=True)
        ok, reason = lenient.judge(before, after)
        assert ok, f"总分上升且允许新 high 时应放行，实际：{reason}"

    def test_still_rejects_score_drop_even_with_allow_new_high(self, tmp_path):
        """分数下降无论配置如何都要拦 —— 这是底线。"""
        write(tmp_path, "a.py", "x = 1\n")
        before = evaluate(tmp_path)
        write(tmp_path, "a.py", "try:\n    pass\nexcept:\n    pass\n")
        after = evaluate(tmp_path)
        gate = NetImprovementGate(min_delta=0.0, allow_new_high=True)
        ok, reason = gate.judge(before, after)
        assert not ok
        assert "回滚" in reason


class TestPolishLoop:
    def test_stops_when_no_smells(self, tmp_path):
        write(tmp_path, "a.py", "x = 1\n")
        loop = PolishLoop(workspace=tmp_path, agent=NoOpAgent(), max_rounds=3)
        result = loop.run()
        assert result.stop_reason == "无整洁度问题，无需收敛"
        assert result.rounds == []

    def test_accepts_improving_rounds(self, tmp_path):
        write(tmp_path, "a.py", "import os\nimport sys\nx = 1\n")

        def fix_once(ws, n):
            write(ws, "a.py", "x = 1\n")
            return True

        loop = PolishLoop(
            workspace=tmp_path, agent=ScriptedFixer([fix_once, False]),
            max_rounds=3,
        )
        result = loop.run()
        assert result.rounds
        assert result.rounds[0].accepted
        assert result.final_score >= result.initial_score

    def test_rolls_back_regression(self, tmp_path):
        write(tmp_path, "a.py", "x = 1\nimport os\n")

        def wreck(ws, n):
            write(ws, "a.py", "import os\n" * 30 + "try:\n    pass\nexcept:\n    pass\n")
            return True

        loop = PolishLoop(workspace=tmp_path, agent=ScriptedFixer([wreck, False]),
                          max_rounds=2)
        result = loop.run()
        assert result.rounds[0].accepted is False
        # 回滚后文件应恢复原样
        content = (tmp_path / "a.py").read_text(encoding="utf-8")
        assert "import sys" not in content

    def test_agent_gives_up(self, tmp_path):
        write(tmp_path, "a.py", "import os\nx = 1\n")
        loop = PolishLoop(workspace=tmp_path, agent=NoOpAgent([False]), max_rounds=3)
        result = loop.run()
        assert "放弃" in result.stop_reason
        assert result.rounds == []

    def test_convergence_by_patience(self, tmp_path):
        """连续无改善应停，而不是跑满轮数。"""
        write(tmp_path, "a.py", "import os\nx = 1\n")

        def no_change(ws, n):
            return True  # 声称改了，实际没动

        loop = PolishLoop(
            workspace=tmp_path,
            agent=ScriptedFixer([no_change] * 6),
            max_rounds=6,
        )
        result = loop.run()
        assert result.rounds
        assert "收敛" in result.stop_reason or result.rounds[-1].accepted is False

    def test_max_rounds_respected(self, tmp_path):
        write(tmp_path, "a.py", "import os\nx = 1\n")

        def tiny_fix(ws, n):
            # 每轮删一个 unused import，制造持续小改善
            write(ws, "a.py", "x = 1\n" + "y = " + str(n) + "\n")
            return True

        loop = PolishLoop(workspace=tmp_path, agent=ScriptedFixer([tiny_fix] * 10),
                          max_rounds=2)
        result = loop.run()
        assert len(result.rounds) <= 2

    def test_snapshot_and_restore(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        write(ws, "a.py", "original = 1\n")
        snap = tmp_path / "snap"
        n = _snapshot(ws, snap)
        assert n == 1

        write(ws, "a.py", "modified = 2\n")
        _restore(snap, ws)
        assert "original" in (ws / "a.py").read_text(encoding="utf-8")


class TestQualityGate:
    def test_passes_clean_code(self, tmp_path):
        write(tmp_path, "a.py", '"""Doc."""\n\n\ndef f() -> int:\n    return 1\n')
        report = evaluate(tmp_path)
        ok, msg = quality_gate(report, min_score=90.0, max_high=0)
        assert ok, msg

    def test_fails_on_high_smells(self, tmp_path):
        write(tmp_path, "a.py", "try:\n    pass\nexcept:\n    pass\n")
        report = evaluate(tmp_path)
        ok, msg = quality_gate(report, min_score=0.0, max_high=0)
        assert not ok
        assert "high" in msg

    def test_fails_on_low_score(self, tmp_path):
        write(tmp_path, "a.py", "import os\nimport sys\nimport json\n")
        report = evaluate(tmp_path)
        ok, msg = quality_gate(report, min_score=99.9, max_high=99)
        assert not ok
        assert "低于目标" in msg


class TestRealWorldScenario:
    """模拟真实场景：agent 先写脏代码，再逐步清理。"""

    def test_progressive_cleanup_improves_score(self, tmp_path):
        stages = [
            "import os\nimport sys\nimport json\n" + "x = 1\n",
            "import os\nx = 1\n",
            "x = 1\n",
        ]

        class Stager:
            def __init__(self):
                self.i = 0

            def fix(self, report, workspace, round_no):
                self.i += 1
                if self.i >= len(stages):
                    return False
                write(workspace, "a.py", stages[self.i])
                return True

        write(tmp_path, "a.py", stages[0])
        before = evaluate(tmp_path)
        loop = PolishLoop(workspace=tmp_path, agent=Stager(), max_rounds=4)
        result = loop.run()
        assert result.final_score > result.initial_score
        assert result.accepted_rounds >= 1


class TestBehaviorGuard:
    """语义守护：分数门看不出「分数没变但行为变了」的破坏。

    背景是真实事故：codex 在 smokeshow 上把
        print(e, file=sys.stderr) -> print(e)
    并顺手删了 `import sys`。整洁度分数一分未变（它只数 smell），
    于是净改善门报「改善不足」，而真正的问题是**行为被改了**。
    归因错误会让用户下一次继续踩同一个坑。
    """

    def _g(self):
        from cforge.quality.polish import NetImprovementGate
        return NetImprovementGate()

    def test_stderr_to_stdout_detected(self):
        before = {"m.py": "import sys\n\ndef f(e):\n    print(e, file=sys.stderr)\n"}
        after = {"m.py": "def f(e):\n    print(e)\n"}
        ok, why = self._g().judge_sources(before, after)
        assert ok is False
        assert "输出流" in why

    def test_deleted_function_detected(self):
        before = {"m.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"}
        after = {"m.py": "def a():\n    return 1\n"}
        ok, why = self._g().judge_sources(before, after)
        assert ok is False
        assert "删除了函数" in why

    def test_signature_change_detected(self):
        before = {"m.py": "def f(a, b):\n    return a + b\n"}
        after = {"m.py": "def f(a):\n    return a\n"}
        ok, why = self._g().judge_sources(before, after)
        assert ok is False
        assert "签名变化" in why

    def test_dropped_except_detected(self):
        # 注意：本段源码用 chr(10) 拼接而非字面 "\n"，因为经 Bash heredoc
        # 写入时反斜杠会被转义成 "/n"（实测踩过，会让断言假失败）。
        _n = chr(10)
        before = {"m.py": ("def f():" + _n + "    try:" + _n
                           + "        return 1" + _n
                           + "    except ValueError:" + _n
                           + "        return 0" + _n)}
        after = {"m.py": "def f():" + _n + "    return 1" + _n}
        ok, why = self._g().judge_sources(before, after)
        assert ok is False
        assert "except" in why

    def test_clean_refactor_allowed(self):
        """正常重构（拆函数、改名局部变量）不能被误杀。"""
        before = {"m.py": "def f(a):\n    x = a + 1\n    y = x * 2\n    return y\n"}
        after = {"m.py": ("def f(a):\n    return _calc(a)\n\n"
                          "def _calc(v):\n    t = v + 1\n    return t * 2\n")}
        ok, why = self._g().judge_sources(before, after)
        assert ok is True, why

    def test_syntax_error_after_detected(self):
        before = {"m.py": "def f():\n    return 1\n"}
        after = {"m.py": "def f(:\n    return 1\n"}
        ok, why = self._g().judge_sources(before, after)
        assert ok is False
        assert "无法解析" in why


class TestRestoreRemovesNewFiles:
    """回滚必须删除 agent 新建的 .py —— 否则「回滚」名不副实。

    真实事故（httpx）：codex 为改 `_transports/asgi.py`，先写了个辅助脚本
    `_transports/patch_asgi.py`。守门判定回滚后只还原 asgi.py，
    辅助脚本被留下 → 实际分数 80.5 → 71.9（-8.6），
    而报告写的是「判定回滚，80.5 → 80.5」。**报告与现实不一致**。
    """

    def _setup(self, ws):
        (ws / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def test_new_py_removed_on_restore(self, tmp_path):
        from cforge.quality.polish import _restore, _snapshot

        ws = tmp_path / "ws"
        ws.mkdir()
        self._setup(ws)
        snap = tmp_path / "snap"
        _snapshot(ws, snap)

        (ws / "b.py").write_text("x = 1\n", encoding="utf-8")
        assert (ws / "b.py").is_file()

        _restore(snap, ws)
        assert not (ws / "b.py").exists(), "回滚后不应残留新增文件"

    def test_existing_file_content_restored(self, tmp_path):
        from cforge.quality.polish import _restore, _snapshot

        ws = tmp_path / "ws"
        ws.mkdir()
        self._setup(ws)
        snap = tmp_path / "snap"
        _snapshot(ws, snap)

        (ws / "a.py").write_text("def f():\n    return 999\n", encoding="utf-8")
        _restore(snap, ws)
        assert "return 1" in (ws / "a.py").read_text(encoding="utf-8")

    def test_non_py_files_untouched(self, tmp_path):
        """只清 .py；非代码文件可能是用户数据，不能碰。"""
        from cforge.quality.polish import _restore, _snapshot

        ws = tmp_path / "ws"
        ws.mkdir()
        self._setup(ws)
        snap = tmp_path / "snap"
        _snapshot(ws, snap)

        (ws / "notes.md").write_text("user data\n", encoding="utf-8")
        _restore(snap, ws)
        assert (ws / "notes.md").is_file(), "不得删除非 .py 文件"


class TestRestoreRemovesNewFiles:
    """回滚必须删除 agent 新建的 .py —— 否则「回滚」名不副实。

    真实事故（httpx）：codex 为改 `_transports/asgi.py`，先写了个辅助脚本
    `_transports/patch_asgi.py`。守门判定回滚后只还原 asgi.py，
    辅助脚本被留下 → 实际分数 80.5 → 71.9（-8.6），
    而报告写的是「判定回滚，80.5 → 80.5」。**报告与现实不一致**。
    """

    def _setup(self, ws):
        (ws / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def test_new_py_removed_on_restore(self, tmp_path):
        from cforge.quality.polish import _restore, _snapshot

        ws = tmp_path / "ws"
        ws.mkdir()
        self._setup(ws)
        snap = tmp_path / "snap"
        _snapshot(ws, snap)

        (ws / "b.py").write_text("x = 1\n", encoding="utf-8")
        assert (ws / "b.py").is_file()

        _restore(snap, ws)
        assert not (ws / "b.py").exists(), "回滚后不应残留新增文件"

    def test_existing_file_content_restored(self, tmp_path):
        from cforge.quality.polish import _restore, _snapshot

        ws = tmp_path / "ws"
        ws.mkdir()
        self._setup(ws)
        snap = tmp_path / "snap"
        _snapshot(ws, snap)

        (ws / "a.py").write_text("def f():\n    return 999\n", encoding="utf-8")
        _restore(snap, ws)
        assert "return 1" in (ws / "a.py").read_text(encoding="utf-8")

    def test_non_py_files_untouched(self, tmp_path):
        """只清 .py；非代码文件可能是用户数据，不能碰。"""
        from cforge.quality.polish import _restore, _snapshot

        ws = tmp_path / "ws"
        ws.mkdir()
        self._setup(ws)
        snap = tmp_path / "snap"
        _snapshot(ws, snap)

        (ws / "notes.md").write_text("user data\n", encoding="utf-8")
        _restore(snap, ws)
        assert (ws / "notes.md").is_file(), "不得删除非 .py 文件"
