"""长程状态单测：跨上下文重置的记忆可靠性。

核心断言不是「能存能读」，而是「重启后 agent 真的不会重复失败方向」。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cforge.longrun.state import (
    MAX_DIRECTIONS,
    MAX_LESSONS,
    STATE_DIR,
    STATE_FILE,
    Direction,
    TargetState,
    direction_signature,
    normalize_direction,
    stagnation_ladder,
    workspace_fingerprint,
)


class TestDirectionDedup:
    def test_same_text_same_signature(self):
        assert direction_signature("refactor auth module") == \
               direction_signature("refactor auth module")

    def test_case_and_punctuation_insensitive(self):
        assert direction_signature("Refactor Auth Module!") == \
               direction_signature("refactor auth module")

    def test_word_order_insensitive(self):
        """词序不同但语义相同的方向，应判为同一个。"""
        assert direction_signature("refactor auth module") == \
               direction_signature("auth module refactor")

    def test_different_directions_differ(self):
        assert direction_signature("refactor auth module") != \
               direction_signature("rewrite the parser")

    def test_stop_words_removed(self):
        n = normalize_direction("the agent should refactor the code")
        assert "agent" not in n and "should" not in n and "refactor" in n


class TestRecordDirection:
    def test_record_new_direction(self):
        s = TargetState()
        s.turn = 3
        d = s.record_direction("try approach A", "failed", "timeout")
        assert d.outcome == "failed"
        assert d.turn == 3
        assert len(s.directions) == 1

    def test_repeat_direction_updates_not_duplicates(self):
        s = TargetState()
        s.record_direction("try approach A", "failed", "timeout")
        s.turn = 5
        s.record_direction("try approach A", "failed", "still timeout")
        assert len(s.directions) == 1, "同方向重复应更新而不是新增"
        assert s.directions[0].turn == 5
        assert "still" in s.directions[0].failure_reason

    def test_direction_cap_enforced(self):
        s = TargetState()
        for i in range(MAX_DIRECTIONS + 6):
            s.record_direction(f"approach number {i}")
        assert len(s.directions) == MAX_DIRECTIONS

    def test_failed_directions_filter(self):
        s = TargetState()
        s.record_direction("good one", "ok")
        s.record_direction("bad one", "failed", "why")
        assert [d.text for d in s.failed_directions()] == ["bad one"]

    def test_seen_direction_returns_record(self):
        s = TargetState()
        s.record_direction("try X", "failed", "reason")
        found = s.seen_direction("try X")
        assert found is not None and found.failure_reason == "reason"


class TestLessons:
    def test_add_and_dedupe(self):
        s = TargetState()
        s.add_lesson("don't edit tests")
        s.add_lesson("don't edit tests")
        assert len(s.lessons) == 1

    def test_cap_enforced(self):
        s = TargetState()
        for i in range(MAX_LESSONS + 5):
            s.add_lesson(f"lesson {i}")
        assert len(s.lessons) == MAX_LESSONS

    def test_empty_ignored(self):
        s = TargetState()
        s.add_lesson("   ")
        assert s.lessons == []


class TestStagnation:
    def test_same_fingerprint_detected(self):
        s = TargetState()
        assert s.push_fingerprint("abc") is False
        assert s.push_fingerprint("abc") is True
        assert s.stagnation_count == 1

    def test_changed_fingerprint_resets(self):
        s = TargetState()
        s.push_fingerprint("abc")
        s.push_fingerprint("abc")
        assert s.stagnation_count == 1
        s.push_fingerprint("def")
        assert s.stagnation_count == 0

    def test_ladder_escalates(self):
        assert stagnation_ladder(0).level == 0
        assert stagnation_ladder(1).level == 1
        assert stagnation_ladder(2).level == 2
        assert stagnation_ladder(3).level == 3
        assert stagnation_ladder(3).should_restart is True
        assert stagnation_ladder(2).should_restart is False

    def test_ladder_gives_different_instruction_per_level(self):
        texts = {stagnation_ladder(i).inject for i in (1, 2, 3)}
        assert len(texts) == 3, "每级停滞应给不同指令"


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        s = TargetState(target="build parser", success_criteria=["tests pass"])
        s.turn = 7
        s.record_direction("approach A", "failed", "crashed")
        s.add_lesson("check imports first")
        s.save(tmp_path)

        loaded = TargetState.load(tmp_path)
        assert loaded.target == "build parser"
        assert loaded.turn == 7
        assert loaded.lessons == ["check imports first"]
        assert len(loaded.directions) == 1
        assert loaded.directions[0].failure_reason == "crashed"

    def test_load_missing_returns_default(self, tmp_path):
        s = TargetState.load(tmp_path)
        assert s.target == "" and s.directions == []

    def test_load_corrupt_returns_default(self, tmp_path):
        """坏文件不能让整个长跑崩掉 —— 退回空状态继续。"""
        p = tmp_path / STATE_DIR / STATE_FILE
        p.parent.mkdir(parents=True)
        p.write_text("{not valid json", encoding="utf-8")
        s = TargetState.load(tmp_path)
        assert s.target == ""

    def test_save_is_atomic(self, tmp_path):
        """用 os.replace，不应留下 .tmp 残留。"""
        s = TargetState(target="x")
        s.save(tmp_path)
        d = tmp_path / STATE_DIR
        assert (d / STATE_FILE).is_file()
        assert not list(d.glob("*.tmp"))

    def test_saved_json_is_readable(self, tmp_path):
        s = TargetState(target="目标", lessons=["教训"])
        path = s.save(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["target"] == "目标"
        assert "教训" in raw["lessons"]


class TestInjectBlock:
    def test_includes_failed_directions(self):
        s = TargetState(target="t")
        s.record_direction("bad approach", "failed", "timed out")
        block = s.inject_block()
        assert "已失败的方向" in block
        assert "bad approach" in block
        assert "timed out" in block

    def test_excludes_successful_from_failed_section(self):
        s = TargetState(target="t")
        s.record_direction("good approach", "ok")
        block = s.inject_block()
        assert "已失败的方向" not in block
        assert "已验证有效的做法" in block

    def test_includes_lessons(self):
        s = TargetState(target="t")
        s.add_lesson("never edit the test file")
        assert "never edit the test file" in s.inject_block()

    def test_warns_on_stagnation(self):
        s = TargetState(target="t")
        s.stagnation_count = 2
        block = s.inject_block()
        assert "无实质变化" in block
        assert "必须换" in block

    def test_empty_state_gives_readable_block(self):
        s = TargetState()
        block = s.inject_block()
        assert isinstance(block, str)

    def test_criteria_listed(self):
        s = TargetState(target="t", success_criteria=["a", "b"])
        block = s.inject_block()
        assert "- a" in block and "- b" in block


class TestWorkspaceFingerprint:
    def test_empty_dir_stable(self, tmp_path):
        assert workspace_fingerprint(tmp_path) == workspace_fingerprint(tmp_path)

    def test_content_change_changes_fingerprint(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1", encoding="utf-8")
        before = workspace_fingerprint(tmp_path)
        f.write_text("x = 2", encoding="utf-8")
        assert workspace_fingerprint(tmp_path) != before

    def test_irrelevant_files_ignored(self, tmp_path):
        """缓存/构建目录的变动不应算作「有进展」。"""
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        before = workspace_fingerprint(tmp_path)
        for bad in ("__pycache__", ".venv", "node_modules"):
            d = tmp_path / bad
            d.mkdir()
            (d / "noise.py").write_text("noise", encoding="utf-8")
        assert workspace_fingerprint(tmp_path) == before

    def test_non_source_files_ignored(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        before = workspace_fingerprint(tmp_path)
        (tmp_path / "image.bin").write_bytes(b"\x00\x01")
        assert workspace_fingerprint(tmp_path) == before

    def test_rename_changes_fingerprint(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
        before = workspace_fingerprint(tmp_path)
        (tmp_path / "a.py").rename(tmp_path / "b.py")
        assert workspace_fingerprint(tmp_path) != before
