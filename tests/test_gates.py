"""门禁桥接单测：对着真实的 codex-edit-gates 脚本跑。

这里不 mock —— 门禁是本项目的关键增量，必须验证它真的能跑。
若脚本目录不存在，相关用例 skip 而不是 fail，保证 CI 可在无门禁环境下跑。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from cforge.checks.gates import (
    GATE_SPECS,
    GateSet,
    hooks_json_fragment,
    install_gate_scripts,
)

# 门禁脚本可能存在的几个位置（按优先级）
CANDIDATE_DIRS = [
    Path("E:/pyprojects/competition_hunter/.workbuddy/tmp/codex-edit-gates/codex-edit-gates-main"),
    Path("E:/pyprojects/competition_hunter/codexforge/vendor/codex-edit-gates"),
]


def _find_gates_dir() -> Path | None:
    for d in CANDIDATE_DIRS:
        if (d / "py_edit_gate.py").is_file():
            return d
    return None


GATES_DIR = _find_gates_dir()
needs_gates = pytest.mark.skipif(GATES_DIR is None, reason="门禁脚本目录不存在")


class TestGateSpecs:
    def test_eight_gates_defined(self):
        assert len(GATE_SPECS) == 8

    def test_each_has_required_fields(self):
        for spec in GATE_SPECS:
            for key in ("name", "script", "matcher", "timeout", "lang", "fixes"):
                assert key in spec, f"{spec.get('name')} 缺少 {key}"

    def test_write_scan_uses_exec_matcher(self):
        """write_scan 监听的是 shell 写入，matcher 与其他门禁不同。"""
        ws = next(s for s in GATE_SPECS if s["name"] == "write_scan_gate")
        assert "exec_command" in ws["matcher"]
        others = [s for s in GATE_SPECS if s["name"] != "write_scan_gate"]
        for s in others:
            assert "exec_command" not in s["matcher"]

    def test_timeouts_are_sane(self):
        for spec in GATE_SPECS:
            assert 5 <= spec["timeout"] <= 120


@needs_gates
class TestGateSetReal:
    def test_all_scripts_present(self):
        gs = GateSet(scripts_dir=GATES_DIR)
        assert len(gs.available()) == 8, f"缺失：{gs.missing()}"

    def test_py_gate_detects_syntax_error(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
        gs = GateSet(scripts_dir=GATES_DIR)
        v = gs.run("py_edit_gate", file_path=str(bad), cwd=str(tmp_path))
        assert v.ran
        assert not v.clean
        assert "COMPILE" in v.advisory.upper() or "SYNTAX" in v.advisory.upper()

    def test_py_gate_silent_on_clean_file(self, tmp_path):
        good = tmp_path / "good.py"
        good.write_text('"""Doc."""\n\n\ndef f() -> int:\n    return 1\n', encoding="utf-8")
        gs = GateSet(scripts_dir=GATES_DIR)
        v = gs.run("py_edit_gate", file_path=str(good), cwd=str(tmp_path))
        assert v.ran
        assert v.exit_code == 0
        assert v.clean, f"干净文件不应有告警，实际：{v.advisory!r}"

    def test_gate_never_raises_on_unknown(self, tmp_path):
        gs = GateSet(scripts_dir=GATES_DIR)
        v = gs.run("no_such_gate", file_path=str(tmp_path / "x.py"), cwd=str(tmp_path))
        assert v.ran is False
        assert "未知" in v.error

    def test_gate_handles_missing_script(self, tmp_path):
        empty = tmp_path / "empty_gates"
        empty.mkdir()
        gs = GateSet(scripts_dir=empty)
        v = gs.run("py_edit_gate", file_path=str(tmp_path / "x.py"), cwd=str(tmp_path))
        assert v.ran is False
        assert "不存在" in v.error

    def test_kill_switch_respected(self, tmp_path):
        good = tmp_path / "g.py"
        good.write_text("x = 1\n", encoding="utf-8")
        gs = GateSet(scripts_dir=GATES_DIR, kill_switches={"PY_EDIT_GATE": "0"})
        v = gs.run("py_edit_gate", file_path=str(good), cwd=str(tmp_path))
        assert v.ran is False
        assert "kill switch" in v.error

    def test_run_all_returns_all_installed(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        gs = GateSet(scripts_dir=GATES_DIR)
        verdicts = gs.run_all(file_path=str(f), cwd=str(tmp_path))
        assert len(verdicts) == 8
        assert all(v.ran for v in verdicts)

    def test_disabled_marker_respected(self, tmp_path):
        """`.edit-gate-disable` 标记应让门禁降级为 advisory-only。"""
        (tmp_path / ".edit-gate-disable").write_text("", encoding="utf-8")
        f = tmp_path / "b.py"
        f.write_text("def broken(:\n", encoding="utf-8")
        gs = GateSet(scripts_dir=GATES_DIR)
        v = gs.run("py_edit_gate", file_path=str(f), cwd=str(tmp_path))
        # 标记存在时门禁仍应运行成功，但强烈检查被抑制
        assert v.ran


@needs_gates
class TestInstall:
    def test_install_copies_all_scripts(self, tmp_path):
        target = tmp_path / "scripts"
        copied = install_gate_scripts(GATES_DIR, target)
        assert len(copied) == 8
        for spec in GATE_SPECS:
            assert (target / spec["script"]).is_file()

    def test_install_is_idempotent(self, tmp_path):
        target = tmp_path / "scripts"
        install_gate_scripts(GATES_DIR, target)
        again = install_gate_scripts(GATES_DIR, target)
        assert again == []  # 内容一致时不再复制

    def test_install_updates_changed_script(self, tmp_path):
        target = tmp_path / "scripts"
        install_gate_scripts(GATES_DIR, target)
        (target / "py_edit_gate.py").write_text("# tampered", encoding="utf-8")
        copied = install_gate_scripts(GATES_DIR, target)
        assert "py_edit_gate.py" in copied


class TestHooksFragment:
    def test_generates_posttooluse_with_eight_blocks(self):
        frag = hooks_json_fragment("C:/py/python.exe", Path("C:/scripts"))
        blocks = frag["hooks"]["PostToolUse"]
        assert len(blocks) == 8

    def test_paths_use_forward_slashes(self):
        """Windows 反斜杠在 JSON 里要转义，统一用正斜杠避免踩坑。"""
        frag = hooks_json_fragment("C:\\py\\python.exe", Path("C:/my/scripts"))
        cmd = frag["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "\\" not in cmd

    def test_each_block_has_matcher_and_command(self):
        frag = hooks_json_fragment("py", Path("s"))
        for block in frag["hooks"]["PostToolUse"]:
            assert block["matcher"]
            h = block["hooks"][0]
            assert h["type"] == "command"
            assert h["command"]
            assert isinstance(h["timeout"], int)

    def test_serializable(self):
        frag = hooks_json_fragment("py", Path("s"))
        assert json.loads(json.dumps(frag)) == frag
