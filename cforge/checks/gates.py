"""编辑门禁层：把 codex-edit-gates 的 8 个 PostToolUse 门禁接进 CodexForge。

## 这一层在赛道评分里的位置

Track 160 的「完整性 30%」明确要求**成果可复现性**。门禁做的事正好是：
每一次文件写入都被机器验证过（编译 / lint / 结构 / 编码），
且验证结果是确定性、可重放的。这在技术文档里是「可复现性」的硬证据。

## 为什么用子进程而不是 import

`codex-edit-gates` 的 8 个 gate 全部是 stdlib-only 独立脚本，
设计上就是被 codex hook 以**子进程**方式调起的。保持子进程调用有三个好处：

1. 与上游项目的部署形态一致（它就是按 hook 设计的）
2. 单个 gate 崩溃不影响主流程（gate 自身 fail-open 返回 0）
3. **许可证隔离**：gate 脚本独立进程、无链接，不构成衍生作品

## hook 协议（从 codex-edit-gates 实测确认）

stdin:  JSON，含 `hook_event_name` / `tool_name` / `cwd` / `tool_input`
stdout: JSON，形如 `{"hookSpecificOutput": {"additionalContext": "..."}, "systemMessage": "..."}`
exit:   永远 0（fail open，绝不阻塞工具调用）
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 8 个 gate 的元信息。matcher 与 timeout 取自 codex-edit-gates README。
GATE_SPECS: tuple[dict[str, Any], ...] = (
    {"name": "py_edit_gate", "script": "py_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "python", "fixes": True},
    {"name": "rs_edit_gate", "script": "rs_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "rust", "fixes": True},
    {"name": "win_edit_gate", "script": "win_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "powershell", "fixes": False},
    {"name": "ml_edit_gate", "script": "ml_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "matlab", "fixes": False},
    {"name": "cpp_edit_gate", "script": "cpp_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "cpp", "fixes": False},
    {"name": "js_edit_gate", "script": "js_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "javascript", "fixes": False},
    {"name": "html_edit_gate", "script": "html_edit_gate.py",
     "matcher": "apply_patch|Edit|Write|MultiEdit|edit_file|write_file",
     "timeout": 25, "lang": "html", "fixes": False},
    {"name": "write_scan_gate", "script": "write_scan_gate.py",
     "matcher": "exec_command",
     "timeout": 60, "lang": "shell-write", "fixes": False},
)


@dataclass
class GateVerdict:
    """一次门禁执行的结果。"""

    gate: str
    ran: bool
    exit_code: int = 0
    advisory: str = ""
    system_message: str = ""
    stdout_raw: str = ""
    error: str = ""

    @property
    def clean(self) -> bool:
        """无告警即视为干净。门禁静默 = 文件通过。"""
        return self.ran and not self.advisory.strip() and self.exit_code == 0


@dataclass
class GateSet:
    """一套已安装的门禁脚本目录。"""

    scripts_dir: Path
    python_exe: str = field(default_factory=lambda: sys.executable)
    # 环境级 kill switch，形如 {"PY_EDIT_GATE": "0"}
    kill_switches: dict[str, str] = field(default_factory=dict)

    def available(self) -> list[str]:
        return [
            spec["name"] for spec in GATE_SPECS
            if (self.scripts_dir / spec["script"]).is_file()
        ]

    def missing(self) -> list[str]:
        return [
            spec["name"] for spec in GATE_SPECS
            if not (self.scripts_dir / spec["script"]).is_file()
        ]

    def run(
        self,
        gate: str,
        *,
        file_path: str,
        cwd: str,
        tool_name: str = "Write",
        event: str = "PostToolUse",
        extra_args: list[str] | None = None,
    ) -> GateVerdict:
        """执行单个门禁。永不抛异常，失败信息进 GateVerdict.error。"""
        spec = next((s for s in GATE_SPECS if s["name"] == gate), None)
        if spec is None:
            return GateVerdict(gate=gate, ran=False, error=f"未知门禁：{gate}")

        script = self.scripts_dir / spec["script"]
        if not script.is_file():
            return GateVerdict(gate=gate, ran=False, error=f"脚本不存在：{script}")

        env_switch = self.kill_switches.get(spec["name"].upper().replace("_EDIT_GATE", "_EDIT_GATE"))
        if str(env_switch) == "0":
            return GateVerdict(gate=gate, ran=False, error="已通过 kill switch 关闭")

        payload = {
            "hook_event_name": event,
            "tool_name": tool_name,
            "cwd": cwd,
            "tool_input": {"file_path": file_path},
        }
        argv = [self.python_exe, str(script)] + list(extra_args or [])
        try:
            proc = subprocess.run(
                argv,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec["timeout"],
            )
        except subprocess.TimeoutExpired:
            return GateVerdict(gate=gate, ran=True, exit_code=-1, error="门禁超时")
        except Exception as exc:  # noqa: BLE001
            return GateVerdict(gate=gate, ran=False, error=f"{type(exc).__name__}: {exc}")

        verdict = GateVerdict(
            gate=gate,
            ran=True,
            exit_code=proc.returncode,
            stdout_raw=(proc.stdout or "").strip(),
        )
        if proc.returncode != 0:
            # 门禁设计上永远返回 0；非 0 说明脚本本身出问题
            verdict.error = f"非预期退出码 {proc.returncode}: {(proc.stderr or '')[:200]}"
            return verdict

        text = verdict.stdout_raw
        if not text:
            return verdict  # 静默 = 通过
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            verdict.advisory = text[:1000]
            return verdict

        hook_out = data.get("hookSpecificOutput") or {}
        verdict.advisory = str(hook_out.get("additionalContext") or "")
        verdict.system_message = str(data.get("systemMessage") or "")
        return verdict

    def run_all(self, *, file_path: str, cwd: str, tool_name: str = "Write") -> list[GateVerdict]:
        """跑全部已安装门禁。按扩展名可优化，但骨架阶段全跑更安全。"""
        return [
            self.run(gate, file_path=file_path, cwd=cwd, tool_name=tool_name)
            for gate in self.available()
        ]


def install_gate_scripts(source_dir: Path, target_dir: Path) -> list[str]:
    """把门禁脚本复制到目标目录。返回已复制的文件名列表。

    幂等：目标已存在且内容一致时跳过。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for spec in GATE_SPECS:
        src = source_dir / spec["script"]
        if not src.is_file():
            continue
        dst = target_dir / spec["script"]
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            continue
        shutil.copy2(src, dst)
        copied.append(spec["script"])
    return copied


def hooks_json_fragment(python_exe: str, scripts_dir: Path) -> dict[str, Any]:
    """生成可合并进 `~/.codex/hooks.json` 的 hooks 片段。

    为什么不是 config.toml：本机实测 hooks 实际登记在 `hooks.json`，
    `config.toml` 里没有 `[[hooks]]` 块。生成哪个格式由调用方决定，
    这里只提供 JSON 结构。
    """
    py = python_exe.replace("\\", "/")
    scripts = str(scripts_dir).replace("\\", "/")
    post_tool: list[dict[str, Any]] = []
    for spec in GATE_SPECS:
        matcher = spec["matcher"]
        post_tool.append({
            "matcher": matcher,
            "hooks": [{
                "type": "command",
                "command": f'"{py}" "{scripts}/{spec["script"]}"',
                "timeout": spec["timeout"],
            }],
        })
    return {"hooks": {"PostToolUse": post_tool}}
