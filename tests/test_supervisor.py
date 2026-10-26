"""Goal Guard 单测：证据门控的核心纪律。

断言的重点不是「能不能判对」，而是「会不会谎报通过」。
赛道评分里「真实性」是硬要求，所以宁可判 unverified 也不能假通过。
"""
from __future__ import annotations

import pytest

from cforge.harness.supervisor import (
    DeterministicProvider,
    Evidence,
    Phase,
    Protocol,
    SupervisorLoop,
    Target,
)


@pytest.fixture
def provider() -> DeterministicProvider:
    return DeterministicProvider()


class TestDeterministicProvider:
    def test_file_exists_true(self, tmp_path, provider):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        ev = provider.collect(
            Target("t", "d", "file_exists", {"path": "a.txt"}), tmp_path,
        )
        assert ev.satisfied

    def test_file_exists_false(self, tmp_path, provider):
        ev = provider.collect(
            Target("t", "d", "file_exists", {"path": "nope.txt"}), tmp_path,
        )
        assert not ev.satisfied

    def test_file_contains(self, tmp_path, provider):
        (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
        ok = provider.collect(
            Target("t", "d", "file_contains",
                   {"path": "a.txt", "needle": "world"}), tmp_path,
        )
        assert ok.satisfied
        miss = provider.collect(
            Target("t", "d", "file_contains",
                   {"path": "a.txt", "needle": "absent"}), tmp_path,
        )
        assert not miss.satisfied

    def test_file_contains_on_missing_file_is_unsatisfied(self, tmp_path, provider):
        ev = provider.collect(
            Target("t", "d", "file_contains",
                   {"path": "ghost.txt", "needle": "x"}), tmp_path,
        )
        assert not ev.satisfied

    def test_cmd_exit_zero(self, tmp_path, provider):
        ok = provider.collect(
            Target("t", "d", "cmd_exit_zero", {"cmd": "exit 0"}), tmp_path,
        )
        assert ok.satisfied
        bad = provider.collect(
            Target("t", "d", "cmd_exit_zero", {"cmd": "exit 3"}), tmp_path,
        )
        assert not bad.satisfied

    def test_unknown_check_is_unsatisfied_not_error(self, tmp_path, provider):
        """未知 check 必须判失败，绝不能默认通过。"""
        ev = provider.collect(Target("t", "d", "no_such_check"), tmp_path)
        assert not ev.satisfied
        assert "未知" in ev.detail


class TestSupervisorLoop:
    def test_all_met_yields_done(self, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("t1", "file a", "file_exists", {"path": "a.txt"}),
            ]),
            workspace=tmp_path,
        )
        v = loop.evaluate(claimed_done=True)
        assert v.done
        assert not v.unverified
        assert v.targets_met == ["t1"]

    def test_claim_done_with_unmet_is_blocked(self, tmp_path):
        """核心纪律：声称完成但证据不足 → 拒绝 DONE。"""
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("t1", "missing", "file_exists", {"path": "nope.txt"}),
            ]),
            workspace=tmp_path,
        )
        v = loop.evaluate(claimed_done=True)
        assert not v.done
        assert v.phase is Phase.EXECUTE
        assert "t1" in v.next_action

    def test_repeated_blocked_stops_eventually_unverified(self, tmp_path):
        """超过上限后接受完成，但必须标记 unverified —— 不许谎报已验证。

        语义：`blocked_stops` 在每次「声称完成但证据不足」时递增，
        当 `blocked_stops > max_blocked_stops` 时才放行并标记 unverified。
        max=1 时：第 1 次 blocked=1（不放行），第 2 次 blocked=2（2>1 放行）。
        """
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("t1", "missing", "file_exists", {"path": "nope.txt"}),
            ]),
            workspace=tmp_path,
            max_blocked_stops=1,
        )
        v1 = loop.evaluate(claimed_done=True)
        assert not v1.done
        assert not v1.unverified
        v2 = loop.evaluate(claimed_done=True)
        assert v2.done
        assert v2.unverified is True

    def test_higher_cap_allows_more_attempts(self, tmp_path):
        """max=3 时应允许 3 次被驳回，第 4 次才放行。"""
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("t1", "missing", "file_exists", {"path": "nope.txt"}),
            ]),
            workspace=tmp_path,
            max_blocked_stops=3,
        )
        for _ in range(3):
            assert not loop.evaluate(claimed_done=True).done
        v = loop.evaluate(claimed_done=True)
        assert v.done and v.unverified

    def test_no_targets_claiming_done_is_unverified(self, tmp_path):
        """没有目标却声称完成 → 无可验证证据。"""
        loop = SupervisorLoop(protocol=Protocol(targets=[]), workspace=tmp_path)
        v = loop.evaluate(claimed_done=True)
        assert v.done
        assert v.unverified

    def test_prepare_phase_before_claim(self, tmp_path):
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("t1", "x", "file_exists", {"path": "nope"}),
            ]),
            workspace=tmp_path,
        )
        v = loop.evaluate(claimed_done=False)
        assert not v.done
        assert v.phase is Phase.EXECUTE

    def test_audit_records_every_round(self, tmp_path):
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("t1", "x", "file_exists", {"path": "nope"}),
            ]),
            workspace=tmp_path,
        )
        loop.evaluate(claimed_done=False)
        loop.evaluate(claimed_done=True)
        assert len(loop.audit) == 2
        assert loop.audit[0]["turn"] == 1
        assert loop.audit[1]["claimed_done"] is True
        # 审计必须带证据明细，否则无法事后复核
        assert "evidence" in loop.audit[1]

    def test_audit_tail_respects_n(self, tmp_path):
        loop = SupervisorLoop(protocol=Protocol(targets=[]), workspace=tmp_path)
        for _ in range(7):
            loop.evaluate(claimed_done=False)
        assert len(loop.audit_tail(3)) == 3

    def test_summary_line_reports_unverified(self, tmp_path):
        loop = SupervisorLoop(protocol=Protocol(targets=[]), workspace=tmp_path)
        v = loop.evaluate(claimed_done=True)
        assert "UNVERIFIED" in v.summary_line()

    def test_partial_met_reports_both_lists(self, tmp_path):
        (tmp_path / "yes.txt").write_text("x", encoding="utf-8")
        loop = SupervisorLoop(
            protocol=Protocol(targets=[
                Target("ok", "exists", "file_exists", {"path": "yes.txt"}),
                Target("bad", "missing", "file_exists", {"path": "no.txt"}),
            ]),
            workspace=tmp_path,
        )
        v = loop.evaluate(claimed_done=True)
        assert v.targets_met == ["ok"]
        assert v.targets_unmet == ["bad"]
        assert not v.done


class TestProtocolMarkdown:
    def test_contains_three_sections(self):
        p = Protocol(
            input_desc="ws", targets=[Target("t", "do thing", "file_exists", {"path": "a"})],
            restrictions=["no network"],
        )
        md = p.to_markdown()
        for section in ("## INPUT", "## TARGET", "## RESTRICTIONS"):
            assert section in md
        assert "no network" in md

    def test_empty_protocol_still_valid(self):
        md = Protocol().to_markdown()
        assert "## INPUT" in md
