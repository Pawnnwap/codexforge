"""常驻守门单测：四道闸门与守护循环。

重点是「不该跑的时候真的不跑」—— 一个爱乱动的守护进程没人敢用。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cforge.watch.fixer import ScriptedFixAgent, build_fix_prompt
from cforge.watch.guardian import Guardian, GuardianConfig, GuardianState
from cforge.watch.trigger import (
    ChangeSet,
    TriggerPolicy,
    WatchConfig,
    scan_changes,
)
from cforge.quality.metrics import evaluate


ARROW = chr(45) + chr(62)   # "->"，避开 shell 转义
NL = chr(10)


def write(ws: Path, name: str, content: str, *, mtime: float | None = None) -> Path:
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class FakeClock:
    """可推进的假时钟，让测试无需真的 sleep。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestScanChanges:
    def test_detects_new_source_file(self, tmp_path):
        old = time.time() - 1000
        cs = scan_changes(tmp_path, since=old)
        assert cs.is_empty

        write(tmp_path, "a.py", "x = 1" + NL)
        cs = scan_changes(tmp_path, since=old)
        assert "a.py" in cs.files

    def test_ignores_docs(self, tmp_path):
        """改 README 不该触发代码重构。"""
        old = time.time() - 1000
        write(tmp_path, "README.md", "# hi" + NL)
        cs = scan_changes(tmp_path, since=old)
        assert cs.is_empty

    def test_ignores_cache_dirs(self, tmp_path):
        old = time.time() - 1000
        write(tmp_path, "__pycache__/x.py", "x = 1" + NL)
        write(tmp_path, "node_modules/y.js", "1")
        cs = scan_changes(tmp_path, since=old)
        assert cs.is_empty

    def test_respects_since(self, tmp_path):
        write(tmp_path, "a.py", "x = 1" + NL, mtime=time.time() - 5000)
        cs = scan_changes(tmp_path, since=time.time() - 100)
        assert cs.is_empty

    def test_massive_change_flagged(self, tmp_path):
        old = time.time() - 1000
        for i in range(30):
            write(tmp_path, f"f{i}.py", "x = 1" + NL)
        cs = scan_changes(tmp_path, since=old)
        assert cs.is_massive

    def test_massive_threshold_from_config_is_honoured(self, tmp_path):
        """`WatchConfig.massive_threshold` 必须真正生效。

        回归背景：该字段长期存在于配置里但**从未被读取**（闸门里写死 25）。
        表现为「改了配置没反应」——这类缺陷最难发现，因为不报错。
        实测影响：案例脚本想放开这个闸门跑真实仓库（clone 出来的文件
        mtime 全是新的，36 个文件必被判 massive），改配置完全无效。
        """
        old = time.time() - 1000
        for i in range(30):
            write(tmp_path, f"f{i}.py", "x = 1" + NL)
        cs = scan_changes(tmp_path, since=old)
        assert cs.is_massive_with(25) is True
        assert cs.is_massive_with(50) is False
        # 通过 TriggerPolicy 走完整判定，确认闸门用的是配置值
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0, massive_threshold=50))
        decision = policy.decide()
        assert decision.gates.get("conflict") == "通过", decision.gates


class TestGates:
    def _dirty_project(self, ws: Path) -> None:
        body = "".join(f"import os{NL}" for _ in range(12))
        write(ws, "a.py", body + "x = 1" + NL)

    def _clean_project(self, ws: Path) -> None:
        write(ws, "a.py", '"""D."""' + NL + NL + NL
              + "def f(a: int) " + ARROW + " int:" + NL + "    return a" + NL)

    def test_cooldown_blocks(self, tmp_path):
        write(tmp_path, "a.py", "x = 1" + NL)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=600))
        policy.mark_ran()
        d = policy.decide()
        assert d.should_run is False
        assert "冷却" in d.reason

    def test_no_changes_blocks(self, tmp_path):
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0))
        d = policy.decide()
        assert d.should_run is False
        assert "无源码变更" in d.reason

    def test_stale_changes_outside_window_ignored(self, tmp_path):
        """mtime 早于扫描窗口的改动不算「新变更」。

        默认窗口是 now-86400（24 小时）；这里把文件设成 3 天前，
        应当被判为无变更。
        """
        very_old = time.time() - 3 * 86400
        body = "".join("import os" + NL for _ in range(12))
        p = write(tmp_path, "a.py", body + "x = 1" + NL, mtime=very_old)
        assert p.stat().st_mtime < time.time() - 2 * 86400

        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0, debt_score=99))
        d = policy.decide()
        assert d.should_run is False
        assert "无源码变更" in d.reason

    def test_recent_dirty_project_triggers(self, tmp_path):
        """有近期改动 + 债务超阈值 → 必须触发。"""
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0, debt_score=90))
        d = policy.decide()
        assert d.should_run is True
        assert "低于" in d.reason

    def test_clean_project_does_not_trigger(self, tmp_path):
        self._clean_project(tmp_path)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0))
        d = policy.decide()
        # 有变更但债务未达阈值
        assert d.should_run is False
        assert "债务未达阈值" in d.reason
        assert d.report is not None
        assert d.report.score >= 80

    def test_massive_change_blocks(self, tmp_path):
        for i in range(30):
            write(tmp_path, f"f{i}.py", "import os" + NL)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0))
        d = policy.decide()
        assert d.should_run is False
        assert "规模过大" in d.reason

    def test_dirty_project_triggers(self, tmp_path):
        body = "".join("import os" + NL for _ in range(15))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0, debt_score=95))
        d = policy.decide()
        assert d.should_run is True
        assert "低于" in d.reason or "high" in d.reason

    def test_decision_is_explainable(self, tmp_path):
        """每个判定都必须给出人能看懂的理由 —— 黑盒守护进程没人敢用。"""
        write(tmp_path, "a.py", "x = 1" + NL)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0))
        d = policy.decide()
        assert d.reason
        data = d.as_dict()
        assert "gates" in data and data["gates"]

    def test_gates_recorded_even_when_passing(self, tmp_path):
        body = "".join("import os" + NL for _ in range(15))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        policy = TriggerPolicy(tmp_path, WatchConfig(cooldown_s=0, debt_score=95))
        d = policy.decide()
        assert d.gates.get("cooldown") == "通过"
        assert d.gates.get("changes")
        assert d.gates.get("debt")


class TestGuardianOnce:
    def test_skip_records_event(self, tmp_path):
        g = Guardian(
            workspace=tmp_path,
            agent=ScriptedFixAgent(),
            watch_config=WatchConfig(cooldown_s=0),
            clock=FakeClock(),
            verbose=False,
        )
        ev = g.run_once()
        assert ev.ran is False
        assert ev.reason

    def test_run_and_report(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        clock = FakeClock()
        g = Guardian(
            workspace=tmp_path,
            agent=ScriptedFixAgent(),
            watch_config=WatchConfig(cooldown_s=0, debt_score=95),
            config=GuardianConfig(poll_interval_s=0),
            clock=clock,
            verbose=False,
        )
        ev = g.run_once()
        assert ev.ran is True
        assert ev.quality_after > ev.quality_before or ev.accepted is False

    def test_stop_reason_surfaces_when_fixer_gives_up(self, tmp_path):
        """修复器放弃时必须把原因带出来，否则报告只剩「回滚」二字。

        场景：仓库只有 todo_marker / long_function 这类 scripted 修不了的
        问题。PolishLoop 会给出「修复器放弃（问题需人工判断）」，此前该结论
        没有出到 WatchEvent，读者无法区分「修了但被回滚」与「根本没东西可修」。

        注意：必须先把分数压到 debt_score 之下才会触发——闸门优先级是
        「债务未达阈值」先于其它判定，这是既有设计，测试要顺着它写。
        但同时要保证**没有任何 unused_import**，否则 scripted 修复器会修好它
        并被接受，走不到「放弃」分支。故用裸 except（high）+ TODO 压分。
        """
        write(tmp_path, "a.py",
              "def f():" + NL
              + "    try:" + NL
              + "        return 1" + NL
              + "    except:" + NL          # 裸 except = high 级问题
              + "        # TODO: 以后再说" + NL
              + "        return 0" + NL
              + "def g():" + NL
              + "    # TODO: 补实现" + NL
              + "    return 2" + NL)
        g = Guardian(
            workspace=tmp_path, agent=ScriptedFixAgent(),
            watch_config=WatchConfig(cooldown_s=0, debt_score=95, max_high=99),
            config=GuardianConfig(), clock=FakeClock(), verbose=False,
        )
        ev = g.run_once()
        assert ev.ran is True
        assert ev.accepted is False
        assert ev.stop_reason  # 必须有个可读的终止原因
        assert "stop_reason" in ev.as_dict()

    def test_state_persisted(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        g = Guardian(
            workspace=tmp_path, agent=ScriptedFixAgent(),
            watch_config=WatchConfig(cooldown_s=0, debt_score=95),
            config=GuardianConfig(), clock=FakeClock(), verbose=False,
        )
        g.run_once()
        state_file = tmp_path / g.config.report_dir / "state.json"
        assert state_file.is_file()
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        assert raw["runs_total"] >= 1

    def test_checks_total_counts_skips(self, tmp_path):
        """跳过的轮次也要计入 checks_total —— 否则「守护在不在干活」看不出来。"""
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        g.run_once()
        g.run_once()
        assert g._state.checks_total == 2
        assert g._state.runs_total == 0   # 都没动手

    def test_checks_total_persisted_after_skip(self, tmp_path):
        g1 = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                      watch_config=WatchConfig(cooldown_s=0),
                      config=GuardianConfig(), clock=FakeClock(), verbose=False)
        g1.run_once()
        g2 = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                      watch_config=WatchConfig(cooldown_s=0),
                      config=GuardianConfig(), clock=FakeClock(), verbose=False)
        assert g2._state.checks_total == 1

    def test_state_survives_restart(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        cfg = GuardianConfig()
        g1 = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                      watch_config=WatchConfig(cooldown_s=0, debt_score=95),
                      config=cfg, clock=FakeClock(), verbose=False)
        g1.run_once()
        g2 = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                      watch_config=WatchConfig(cooldown_s=0, debt_score=95),
                      config=cfg, clock=FakeClock(), verbose=False)
        assert g2._state.runs_total == g1._state.runs_total

    def test_error_does_not_crash(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)

        class BoomAgent:
            def fix(self, report, workspace, round_no):
                raise RuntimeError("boom")

        g = Guardian(workspace=tmp_path, agent=BoomAgent(),
                     watch_config=WatchConfig(cooldown_s=0, debt_score=95),
                     clock=FakeClock(), verbose=False)
        ev = g.run_once()
        assert "boom" in ev.error

    def test_consecutive_failures_tracked(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)

        class BoomAgent:
            def fix(self, report, workspace, round_no):
                raise RuntimeError("boom")

        g = Guardian(workspace=tmp_path, agent=BoomAgent(),
                     watch_config=WatchConfig(cooldown_s=0, debt_score=95),
                     clock=FakeClock(), verbose=False)
        g.run_once()
        assert g._state.consecutive_failures == 1


class TestGuardianLoop:
    def test_max_cycles_respected(self, tmp_path):
        clock = FakeClock()
        g = Guardian(
            workspace=tmp_path, agent=ScriptedFixAgent(),
            watch_config=WatchConfig(cooldown_s=0),
            config=GuardianConfig(poll_interval_s=0),
            clock=clock, verbose=False,
        )
        out = g.run_forever(max_cycles=3)
        assert out["cycles"] == 3
        assert "轮数上限" in out["stop_reason"]

    def test_stop_signal(self, tmp_path):
        clock = FakeClock()
        flag = {"stop": False}

        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=clock, verbose=False)
        out = g.run_forever(max_cycles=10, should_stop=lambda: flag["stop"])
        assert out["cycles"] <= 10

    def test_circuit_breaker_on_repeated_failure(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)

        class BoomAgent:
            def fix(self, report, workspace, round_no):
                raise RuntimeError("boom")

        g = Guardian(workspace=tmp_path, agent=BoomAgent(),
                     watch_config=WatchConfig(cooldown_s=0, debt_score=95),
                     config=GuardianConfig(poll_interval_s=0,
                                           max_consecutive_failures=2),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=10)
        assert "熔断" in out["stop_reason"]

    def test_report_written(self, tmp_path):
        body = "".join("import os" + NL for _ in range(12))
        write(tmp_path, "a.py", body + "x = 1" + NL)
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0, debt_score=95),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=1)
        p = Path(out["report"])
        assert p.is_file()
        text = p.read_text(encoding="utf-8")
        assert "CodexForge 守门报告" in text
        assert "## 本次会话" in text
        assert "## 明细" in text
        latest = tmp_path / g.config.report_dir / "latest.md"
        assert latest.is_file()

    def test_report_contains_json_block(self, tmp_path):
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=1)
        text = Path(out["report"]).read_text(encoding="utf-8")
        assert "```json" in text
        assert "stop_reason" in text


class TestReportHonesty:
    """报告不能误导。这组用例锁死「跳过时不谎报质量变化」。

    实测踩过：某次运行全部跳过，报告却写「质量 83.8 → 75.0 (-8.8)」，
    让用户以为代码变差了。实际上什么都没做，那个 75.0 是历史值。
    """

    def test_skipped_run_does_not_show_quality_drop(self, tmp_path):
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        # 手工注入一段「历史」状态，模拟基线不同
        g._state.quality_start = 90.0
        g._state.quality_last = 40.0
        out = g.run_forever(max_cycles=1)
        text = Path(out["report"]).read_text(encoding="utf-8")
        assert "本次未执行任何改动" in text
        # 不应出现会话内的质量变化箭头
        assert "90.0 → 40.0" not in text.replace("基线 → 当前", "")

    def test_skipped_run_reports_reason(self, tmp_path):
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=1)
        text = Path(out["report"]).read_text(encoding="utf-8")
        assert "跳过原因" in text
        assert "无源码变更" in text

    def test_session_stats_in_return_value(self, tmp_path):
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=2)
        assert "session" in out
        assert out["session"]["checks"] == 2
        assert out["session"]["ran"] == 0

    def test_remaining_debt_listed_when_unfixable(self, tmp_path):
        """AST 修复器修不了重复代码，报告必须明确告诉用户。"""
        NLc = chr(10)
        body = (
            "def save(conn, k, v):" + NLc +
            "    cur = conn.cursor()" + NLc +
            "    cur.execute('INSERT INTO kv VALUES (?, ?)', (k, v))" + NLc +
            "    conn.commit()" + NLc +
            "    return True" + NLc + NLc +
            "def save_cache(conn, k, v):" + NLc +
            "    cur = conn.cursor()" + NLc +
            "    cur.execute('INSERT INTO kv VALUES (?, ?)', (k, v))" + NLc +
            "    conn.commit()" + NLc +
            "    return True" + NLc
        )
        write(tmp_path, "a.py", body)
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0, debt_score=99),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=1)
        text = Path(out["report"]).read_text(encoding="utf-8")
        assert "剩余债务" in text
        assert "重复" in text
        assert "--fixer codex" in text

    def test_no_remaining_debt_section_when_clean(self, tmp_path):
        write(tmp_path, "a.py", '"""D."""' + NL + NL + NL
              + "def f(a: int) " + ARROW + " int:" + NL + "    return a" + NL)
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        out = g.run_forever(max_cycles=1)
        text = Path(out["report"]).read_text(encoding="utf-8")
        assert "剩余债务" not in text


class TestReportJson:
    def test_json_block_parseable(self, tmp_path):
        import re
        g = Guardian(workspace=tmp_path, agent=ScriptedFixAgent(),
                     watch_config=WatchConfig(cooldown_s=0),
                     config=GuardianConfig(poll_interval_s=0),
                     clock=FakeClock(), verbose=False)
        g._state.quality_start = 88.0
        out = g.run_forever(max_cycles=1)
        text = Path(out["report"]).read_text(encoding="utf-8")
        m = re.search(r"```json\n(.*?)\n```", text, re.S)
        assert m
        data = json.loads(m.group(1))
        assert "session" in data
        assert "state" in data
        assert "events" in data


class TestScriptedFixer:
    def test_removes_unused_import(self, tmp_path):
        write(tmp_path, "a.py", "import os" + NL + "import sys" + NL + "x = 1" + NL)
        from cforge.quality.metrics import evaluate
        report = evaluate(tmp_path)
        agent = ScriptedFixAgent()
        assert agent.fix(report, tmp_path, 1) is True
        after = (tmp_path / "a.py").read_text(encoding="utf-8")
        assert "import os" not in after
        assert "x = 1" in after

    def test_keeps_used_import(self, tmp_path):
        write(tmp_path, "a.py", "import os" + NL + "print(os.getcwd())" + NL)
        report = evaluate(tmp_path)
        ScriptedFixAgent().fix(report, tmp_path, 1)
        after = (tmp_path / "a.py").read_text(encoding="utf-8")
        assert "import os" in after

    def test_preserves_utf8(self, tmp_path):
        write(tmp_path, "a.py", "import os" + NL + "x = 1" + NL)
        report = evaluate(tmp_path)
        ScriptedFixAgent().fix(report, tmp_path, 1)
        raw = (tmp_path / "a.py").read_bytes()
        assert not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"))

    def test_refuses_to_touch_utf16(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes("import os\nx = 1\n".encode("utf-16"))
        report = evaluate(tmp_path)
        assert ScriptedFixAgent().fix(report, tmp_path, 1) is False
        assert p.read_bytes().startswith(b"\xff\xfe")

    def test_returns_false_when_nothing_to_fix(self, tmp_path):
        write(tmp_path, "a.py", "x = 1" + NL)
        report = evaluate(tmp_path)
        assert ScriptedFixAgent().fix(report, tmp_path, 1) is False

    def test_removal_must_stay_parseable(self, tmp_path):
        """删完必须还能解析，否则放弃 —— 保护性检查。"""
        write(tmp_path, "a.py",
              "from os import path" + NL + "print(path.join('a','b'))" + NL)
        report = evaluate(tmp_path)
        ScriptedFixAgent().fix(report, tmp_path, 1)
        # 被用了，不该删
        assert "import path" in (tmp_path / "a.py").read_text(encoding="utf-8")


class TestFixPrompt:
    def test_prompt_lists_high_and_medium(self, tmp_path):
        body = "import os" + NL + "try:" + NL + "    pass" + NL + "except:" + NL + "    pass" + NL
        write(tmp_path, "a.py", body)
        report = evaluate(tmp_path)
        prompt = build_fix_prompt(report, tmp_path)
        assert "unused_import" in prompt or "bare_except" in prompt

    def test_prompt_has_hard_constraints(self, tmp_path):
        write(tmp_path, "import_me.py", "import os" + NL + "x = 1" + NL)
        report = evaluate(tmp_path)
        prompt = build_fix_prompt(report, tmp_path)
        assert "UTF-8" in prompt
        assert "不要删" in prompt or "不要删除" in prompt

    def test_prompt_excludes_low(self, tmp_path):
        write(tmp_path, "a.py", "# TODO: later" + NL + "x = 1" + NL)
        report = evaluate(tmp_path)
        prompt = build_fix_prompt(report, tmp_path)
        assert prompt == ""   # 只有 low 级，不值得动用 LLM

    def test_empty_report_gives_empty_prompt(self, tmp_path):
        write(tmp_path, "a.py", "x = 1" + NL)
        report = evaluate(tmp_path)
        assert build_fix_prompt(report, tmp_path) == ""

    def test_clone_only_project_still_gets_prompt(self, tmp_path):
        """回归：只有跨文件重复、没有单点 smell 时，prompt 不能为空。

        实测踩过：初版只列 report.smells，导致「只有重复代码」的项目
        拿到空 prompt，codex 修复器永远不被调用 —— 而报告却让用户
        「用 --fixer codex 处理重复」，自相矛盾。
        """
        q = chr(39)
        body = (
            "def save(conn, k, v):" + NL +
            "    cur = conn.cursor()" + NL +
            "    cur.execute(" + q + "INSERT INTO kv VALUES (?, ?)" + q + ", (k, v))" + NL +
            "    conn.commit()" + NL +
            "    return True" + NL + NL +
            "def save_cache(conn, k, v):" + NL +
            "    cur = conn.cursor()" + NL +
            "    cur.execute(" + q + "INSERT INTO kv VALUES (?, ?)" + q + ", (k, v))" + NL +
            "    conn.commit()" + NL +
            "    return True" + NL
        )
        write(tmp_path, "store.py", body)
        report = evaluate(tmp_path)
        assert report.smells == [], "构造前提：无单点 smell"
        assert report.clone_pairs >= 1, "构造前提：有重复对"

        prompt = build_fix_prompt(report, tmp_path)
        assert prompt != "", "只有重复代码时也必须生成 prompt"
        assert "跨文件重复" in prompt
        assert "save" in prompt and "save_cache" in prompt

    def test_clone_prompt_preserves_signatures(self, tmp_path):
        """合并重复时必须保留原函数名 —— 调用方可能依赖。"""
        q = chr(39)
        body = (
            "def a(x):" + NL +
            "    cur = x.cursor()" + NL +
            "    cur.execute(" + q + "A" + q + ")" + NL +
            "    x.commit()" + NL +
            "    return True" + NL + NL +
            "def b(x):" + NL +
            "    cur = x.cursor()" + NL +
            "    cur.execute(" + q + "A" + q + ")" + NL +
            "    x.commit()" + NL +
            "    return True" + NL
        )
        write(tmp_path, "m.py", body)
        report = evaluate(tmp_path)
        prompt = build_fix_prompt(report, tmp_path)
        assert "不要删除任何原有函数名" in prompt or "签名" in prompt
