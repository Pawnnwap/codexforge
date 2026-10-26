"""codex 后端单测：argv 构造与事件流解析。

argv 是纯函数，可以完全离线断言。这很重要 ——
因为 codex 的真实调用要钱要时间，但命令行对错必须能立刻验证。
"""
from __future__ import annotations

import sys

import pytest

from cforge.harness import codex_backend as cb


@pytest.fixture
def spec(tmp_path):
    return cb.CodexSpec(
        prompt="do the thing",
        workspace=tmp_path,
        model="test-model",
        provider_base_url="http://127.0.0.1:8765",
        context_window=262_144,
    )


class TestConfigFlags:
    def test_wire_api_is_always_responses(self, spec):
        """0.163.0 只接受 responses；这个断言防止有人改错成 chat。"""
        flags = cb.build_config_flags(spec)
        joined = " ".join(flags)
        assert "wire_api=responses" in joined
        assert "wire_api=chat" not in joined

    def test_model_and_provider_present(self, spec):
        joined = " ".join(cb.build_config_flags(spec))
        assert "model=test-model" in joined
        assert "model_provider=cforge" in joined

    def test_base_url_present(self, spec):
        joined = " ".join(cb.build_config_flags(spec))
        assert "model_providers.cforge.base_url=http://127.0.0.1:8765" in joined

    def test_api_key_never_on_command_line(self, spec):
        """api key 只能走 env_key，绝不能出现在命令行（会进进程列表）。"""
        joined = " ".join(cb.build_config_flags(spec))
        assert "env_key=CFORGE_UPSTREAM_KEY" in joined
        assert "sk-" not in joined

    def test_context_window_passed(self, spec):
        joined = " ".join(cb.build_config_flags(spec))
        assert "model_context_window=262144" in joined

    def test_reasoning_effort_optional(self, spec):
        assert "model_reasoning_effort" not in " ".join(cb.build_config_flags(spec))
        spec.reasoning_effort = "high"
        assert "model_reasoning_effort=high" in " ".join(cb.build_config_flags(spec))

    def test_extra_config_appended(self, spec):
        spec.extra_config = {"features.multi_agent": "false"}
        assert "features.multi_agent=false" in " ".join(cb.build_config_flags(spec))


class TestArgv:
    def test_starts_with_exec(self, spec):
        assert cb.build_argv(spec, exe="codex")[:2] == ["codex", "exec"]

    def test_uses_workspace(self, spec, tmp_path):
        argv = cb.build_argv(spec, exe="codex")
        assert "-C" in argv
        assert str(tmp_path) in argv

    def test_sandbox_mode(self, spec):
        argv = cb.build_argv(spec, exe="codex")
        assert "-s" in argv
        idx = argv.index("-s")
        # 默认 danger-full-access：exec 模式下 workspace-write 会被模型
        # 理解为只读，导致空转（实测，见 SANDBOX_NOTES）
        assert argv[idx + 1] == "danger-full-access"

    def test_sandbox_can_be_overridden(self, spec):
        spec.sandbox = "read-only"
        argv = cb.build_argv(spec, exe="codex")
        assert argv[argv.index("-s") + 1] == "read-only"

    def test_json_flag_present(self, spec):
        assert "--json" in cb.build_argv(spec, exe="codex")

    def test_prompt_is_last(self, spec):
        argv = cb.build_argv(spec, exe="codex")
        assert argv[-1] == "do the thing"

    def test_hook_trust_bypass_defaults_off(self, spec):
        """默认不加 —— 它会往 stdout 打两条 error 级噪声，且信任应由交互式会话完成。"""
        assert spec.bypass_hook_trust is False
        assert "--dangerously-bypass-hook-trust" not in cb.build_argv(spec, exe="codex")

    def test_hook_trust_bypass_can_be_enabled(self, spec):
        spec.bypass_hook_trust = True
        assert "--dangerously-bypass-hook-trust" in cb.build_argv(spec, exe="codex")

    def test_isolate_auth_defaults_on(self, spec):
        """默认隔离 auth，避免 ChatGPT token 刷新重试拖垮 turn。"""
        assert spec.isolate_auth is True

    def test_isolated_home_has_no_auth_json(self):
        home = cb.make_isolated_codex_home()
        assert home.is_dir()
        assert (home / "config.toml").is_file()
        assert not (home / "auth.json").exists()

    def test_git_check_skip_toggle(self, spec):
        assert "--skip-git-repo-check" in cb.build_argv(spec, exe="codex")
        spec.skip_git_check = False
        assert "--skip-git-repo-check" not in cb.build_argv(spec, exe="codex")


class TestParseStream:
    def test_valid_json_line(self):
        events = list(cb.parse_stream(['{"type": "agent_message", "message": "hi"}']))
        assert len(events) == 1
        assert events[0].kind == "agent_message"
        assert events[0].text() == "hi"
        assert events[0].is_message

    def test_non_json_passes_through_as_raw(self):
        """codex 会往 stdout 混入告警，不能让解析器崩。"""
        events = list(cb.parse_stream(["WARNING: something odd"]))
        assert events[0].kind == "raw"
        assert "WARNING" in events[0].payload["line"]

    def test_blank_lines_skipped(self):
        assert list(cb.parse_stream(["", "  ", ""])) == []

    def test_tool_event_detected(self):
        events = list(cb.parse_stream(['{"type": "exec_command_begin"}']))
        assert events[0].is_tool

    def test_mixed_stream(self):
        lines = [
            '{"type": "agent_message", "message": "thinking"}',
            "garbage line",
            '{"type": "exec_command_end", "output": "ok"}',
        ]
        events = list(cb.parse_stream(lines))
        assert len(events) == 3
        assert events[0].is_message
        assert events[1].kind == "raw"
        assert events[2].is_tool

    def test_json_array_line_is_raw(self):
        """顶层不是 dict 的 JSON 也不能崩。"""
        events = list(cb.parse_stream(["[1,2,3]"]))
        assert events[0].kind == "raw"

    def test_text_falls_back_across_keys(self):
        for key in ("message", "text", "content", "output"):
            ev = cb.parse_stream([f'{{"type":"x","{key}":"v"}}']).__next__()
            assert ev.text() == "v"


class TestRealEventStructure:
    """用实测抓取的 codex 0.163.0 真实事件结构做回归。

    初版解析器只读顶层 type，导致 messages 永远为空 —— 这是个真实 bug，
    这些用例就是它的护栏。
    """

    REAL_STREAM = [
        '{"type":"thread.started","thread_id":"01a164a1-8af4"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"error",'
        '"message":"Model metadata for `m` not found."}}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}',
        '{"type":"turn.completed","usage":{"input_tokens":11162,"output_tokens":2}}',
    ]

    def test_agent_message_nested_in_item_is_detected(self):
        events = list(cb.parse_stream(self.REAL_STREAM))
        msgs = [e for e in events if e.is_message]
        assert len(msgs) == 1
        assert msgs[0].text() == "OK"

    def test_item_type_exposed(self):
        events = list(cb.parse_stream(self.REAL_STREAM))
        msg = next(e for e in events if e.is_message)
        assert msg.item_type == "agent_message"

    def test_error_item_flagged(self):
        events = list(cb.parse_stream(self.REAL_STREAM))
        errs = [e for e in events if e.is_error]
        assert len(errs) == 1
        assert "metadata" in errs[0].text().lower()

    def test_usage_extracted_from_turn_completed(self):
        events = list(cb.parse_stream(self.REAL_STREAM))
        done = next(e for e in events if e.kind == "turn.completed")
        assert done.usage()["input_tokens"] == 11162
        assert done.usage()["output_tokens"] == 2

    def test_turn_completed_not_a_message(self):
        events = list(cb.parse_stream(self.REAL_STREAM))
        done = next(e for e in events if e.kind == "turn.completed")
        assert not done.is_message
        assert done.usage()

    def test_turn_failed_flagged_as_failure(self):
        events = list(cb.parse_stream([
            '{"type":"turn.failed","error":{"message":"exceeded retry limit"}}',
        ]))
        assert events[0].is_failure

    def test_thread_started_is_neither(self):
        events = list(cb.parse_stream(self.REAL_STREAM))
        ts = events[0]
        assert not ts.is_message and not ts.is_error and not ts.is_tool

    def test_tool_item_types_detected(self):
        for itype in ("command_execution", "file_change", "mcp_tool_call"):
            ev = next(cb.parse_stream([
                f'{{"type":"item.completed","item":{{"type":"{itype}"}}}}',
            ]))
            assert ev.is_tool, f"{itype} 应被识别为工具事件"


class TestHardTimeout:
    """回归：codex 内部重试可以无限跑，必须有外层硬超时。

    实测：撞上被策略拒绝的命令时，codex 进入「重试→被拒→再重试」循环，
    13 分钟无任何文件变动。没有硬超时的话整个长跑就废了。
    """

    def test_timeout_kills_and_reports(self, tmp_path, monkeypatch):
        # 用 python -c 模拟一个永不返回的进程
        spec = cb.CodexSpec(
            prompt="x", workspace=tmp_path, model="m",
            provider_base_url="http://127.0.0.1:9",
            timeout_s=2, isolate_auth=False,
        )
        monkeypatch.setattr(cb, "build_argv", lambda s, exe=None: [
            sys.executable, "-c", "import time; time.sleep(60)",
        ])
        import time as _t
        t0 = _t.time()
        code, events = cb.run_codex(spec, env_extra={})
        elapsed = _t.time() - t0
        assert code == -1
        assert any(e.kind == "timeout" for e in events)
        assert elapsed < 30, f"硬超时未生效，耗时 {elapsed:.1f}s"

    def test_timeout_event_carries_limit(self, tmp_path, monkeypatch):
        spec = cb.CodexSpec(
            prompt="x", workspace=tmp_path, model="m",
            provider_base_url="http://127.0.0.1:9",
            timeout_s=1, isolate_auth=False,
        )
        monkeypatch.setattr(cb, "build_argv", lambda s, exe=None: [
            sys.executable, "-c", "import time; time.sleep(30)",
        ])
        _, events = cb.run_codex(spec, env_extra={})
        timeout_ev = next(e for e in events if e.kind == "timeout")
        assert timeout_ev.payload["timeout_s"] == 1


class TestFindCodex:
    def test_returns_path_or_none(self):
        got = cb.find_codex()
        assert got is None or isinstance(got, str)

    def test_version_string_shape(self):
        ver = cb.codex_version()
        assert ver == "" or "codex" in ver.lower()


class TestProbeWireApi:
    def test_missing_credentials_short_circuits(self):
        out = cb.probe_wire_api("", "", "m")
        assert out["supports_responses"] is False
        assert "跳过" in out["detail"]

    def test_network_failure_is_reported(self):
        # 指向一个必然不可达的地址，确认不抛异常
        out = cb.probe_wire_api("http://127.0.0.1:9", "k", "m", timeout=2)
        assert out["supports_responses"] is False
        assert out["detail"]


class TestCmdWrapperPromptFlatten:
    """Windows `.cmd` 包装器会截断 argv 里的换行 —— 必须压平成单行。

    ## 这是「prompt 明明传了但模型说没收到任务」的真正原因

    实测链路：891 字符 / 30 行的修复 prompt 交给 `codex.CMD` 后，
    目标进程只收到第一行 `============...`，于是模型回复
    「your message may have been just a separator」，一个文件都没改。

    为什么之前查不出来：
      - 退出码 0、上游 200、turn 正常完成，看起来一切正常
      - `build_argv` 单测断言「argv 里有完整 prompt」——**确实有**，
        丢失发生在 spawn 之后的 cmd.exe 解析阶段，纯函数测试覆盖不到
      - 报告只显示「改善不足」或「修复器放弃」，不指向真因
    对照实验：单行指令 17 秒就能改文件，差别只在换行。

    这组测试锁住「.cmd 路径必须无换行、信息不丢」。
    """

    def _spec(self, tmp_path, prompt: str):
        return cb.CodexSpec(prompt=prompt, workspace=tmp_path, model="m")

    def test_cmd_wrapper_flattens_newlines(self, tmp_path):
        prompt = "LINE1" + chr(10) + "LINE2" + chr(10) + "LINE3"
        spec = self._spec(tmp_path, prompt)
        argv = cb.build_argv(spec, exe="C:/npm/codex.CMD")
        assert chr(10) not in argv[-1], "经 .cmd 调用时不得含换行（会被截断）"
        assert chr(13) not in argv[-1]

    def test_cmd_wrapper_keeps_all_content(self, tmp_path):
        """压平不能丢信息 —— 三段文字都要还在。"""
        prompt = "AAA" + chr(10) + "BBB" + chr(10) + "CCC"
        spec = self._spec(tmp_path, prompt)
        flat = cb.build_argv(spec, exe="codex.cmd")[-1]
        for token in ("AAA", "BBB", "CCC"):
            assert token in flat, f"压平后丢了 {token}"

    def test_native_exe_keeps_newlines(self, tmp_path):
        """原生 exe 不需要压平 —— 别为了 Windows 破坏其它平台的可读性。"""
        prompt = "LINE1" + chr(10) + "LINE2"
        spec = self._spec(tmp_path, prompt)
        argv = cb.build_argv(spec, exe="codex")
        assert argv[-1] == prompt

    def test_paragraph_breaks_become_markers(self, tmp_path):
        """空行（段落）要变成显式标记，避免压平后糊成一片。"""
        prompt = "HEAD" + chr(10) + chr(10) + "BODY"
        spec = self._spec(tmp_path, prompt)
        flat = cb.build_argv(spec, exe="codex.cmd")[-1]
        assert " ¶ ¶ " in flat or "¶" in flat

    def test_bat_also_flattened(self, tmp_path):
        prompt = "A" + chr(10) + "B"
        spec = self._spec(tmp_path, prompt)
        assert chr(10) not in cb.build_argv(spec, exe="run.bat")[-1]

    def test_find_codex_prefers_existing(self):
        """find_codex 必须能定位到 .cmd（Windows npm 安装形态）。"""
        exe = cb.find_codex()
        assert exe is None or exe.lower().endswith((".exe", ".cmd", ".bat", "codex"))


class TestCmdWrapperPromptFlatten:
    """Windows `.cmd` 包装器会截断 argv 里的换行 —— 必须压平成单行。

    ## 这是「prompt 明明传了但模型说没收到任务」的真正原因

    实测链路：891 字符 / 30 行的修复 prompt 交给 `codex.CMD` 后，
    目标进程只收到第一行 `============...`，于是模型回复
    「your message may have been just a separator」，一个文件都没改。

    为什么之前查不出来：
      - 退出码 0、上游 200、turn 正常完成，看起来一切正常
      - `build_argv` 单测断言「argv 里有完整 prompt」——**确实有**，
        丢失发生在 spawn 之后的 cmd.exe 解析阶段，纯函数测试覆盖不到
      - 报告只显示「改善不足」或「修复器放弃」，不指向真因
    对照实验：单行指令 17 秒就能改文件，差别只在换行。

    这组测试锁住「.cmd 路径必须无换行、信息不丢」。
    """

    def _spec(self, tmp_path, prompt: str):
        return cb.CodexSpec(prompt=prompt, workspace=tmp_path, model="m")

    def test_cmd_wrapper_flattens_newlines(self, tmp_path):
        prompt = "LINE1" + chr(10) + "LINE2" + chr(10) + "LINE3"
        spec = self._spec(tmp_path, prompt)
        argv = cb.build_argv(spec, exe="C:/npm/codex.CMD")
        assert chr(10) not in argv[-1], "经 .cmd 调用时不得含换行（会被截断）"
        assert chr(13) not in argv[-1]

    def test_cmd_wrapper_keeps_all_content(self, tmp_path):
        """压平不能丢信息 —— 三段文字都要还在。"""
        prompt = "AAA" + chr(10) + "BBB" + chr(10) + "CCC"
        spec = self._spec(tmp_path, prompt)
        flat = cb.build_argv(spec, exe="codex.cmd")[-1]
        for token in ("AAA", "BBB", "CCC"):
            assert token in flat, f"压平后丢了 {token}"

    def test_native_exe_keeps_newlines(self, tmp_path):
        """原生 exe 不需要压平 —— 别为了 Windows 破坏其它平台的可读性。"""
        prompt = "LINE1" + chr(10) + "LINE2"
        spec = self._spec(tmp_path, prompt)
        argv = cb.build_argv(spec, exe="codex")
        assert argv[-1] == prompt

    def test_paragraph_breaks_become_markers(self, tmp_path):
        """空行（段落）要变成显式标记，避免压平后糊成一片。"""
        prompt = "HEAD" + chr(10) + chr(10) + "BODY"
        spec = self._spec(tmp_path, prompt)
        flat = cb.build_argv(spec, exe="codex.cmd")[-1]
        assert " ¶ ¶ " in flat or "¶" in flat

    def test_bat_also_flattened(self, tmp_path):
        prompt = "A" + chr(10) + "B"
        spec = self._spec(tmp_path, prompt)
        assert chr(10) not in cb.build_argv(spec, exe="run.bat")[-1]

    def test_find_codex_prefers_existing(self):
        """find_codex 必须能定位到 .cmd（Windows npm 安装形态）。"""
        exe = cb.find_codex()
        assert exe is None or exe.lower().endswith((".exe", ".cmd", ".bat", "codex"))
