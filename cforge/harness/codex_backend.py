"""Codex 执行后端：把 codex CLI 当作唯一 harness 调起来。

## 为什么需要这一层

codex 0.163.0 实测只接受 `wire_api = "responses"`（`chat` 已被明确移除，
报错原文：`wire_api = "chat" is no longer supported`）。而书生与 OpenRouter
都只保证 `/chat/completions`。两者之间必须有协议适配，否则 codex 无法驱动。

本模块负责三件事：
1. 生成 codex 的 `-c` 覆盖参数（provider / model / sandbox / context window）
2. 检测「上游是否原生支持 /responses」，不支持时提示启用 compat proxy
3. 以 JSONL 事件流方式解析 codex 输出，产出结构化 step

## 设计取舍

- **不修改用户全局 config.toml**：全部走 `-c` 覆盖与独立 `CODEX_HOME`，
  避免污染用户的日常 codex 环境（本机 config.toml 已有 fleet provider）。
- **`--dangerously-bypass-hook-trust`**：门禁 hook 需要信任才能跑，
  非交互场景无法人工确认，源码已审阅的前提下用它（有风险提示注释）。
- **`--json` 事件流**：这是唯一可稳定解析的输出形态；纯文本模式无法区分
  「模型说话」和「工具执行」。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# 与 codex-edit-gates 的 README 对齐：该套门禁在 codex-cli 0.163.0 上验证过
VERIFIED_CODEX_VERSION = "0.163.0"

# --------------------------------------------------------------------------- #
# sandbox 策略的实测结论（这条会直接决定 agent 能不能干活）
# --------------------------------------------------------------------------- #
# 现象：`-s workspace-write` 配 `codex exec` 时，模型收到 17K 字符的 system
# prompt 后，会回复「sandbox mode is read-only and only permits reading files」
# 并拒绝写文件 —— 上游返回 200，turn 正常完成，但**什么都没做**。
#
# 这不是模型的问题，也不是 proxy 的问题：codex 在非交互 exec 模式下的
# approval mode 是 `never`，它对模型描述 sandbox 的措辞让模型推断出「不可写」。
#
# 实测：把 sandbox 换成 `danger-full-access` 后，同一个模型立刻正常创建文件。
#
# 因此本框架默认用 danger-full-access。安全边界由外层的
# workspace 目录隔离 + 编辑门禁 + Goal Guard 证据校验来保证，
# 而不是依赖 codex 自己的 sandbox（它在 exec 模式下描述不可靠）。
SANDBOX_NOTES = (
    "exec 模式下 workspace-write 会被模型理解为只读，导致空转。"
    "默认改用 danger-full-access，安全由外层目录隔离承担。"
)


@dataclass
class CodexSpec:
    """一次 codex 调用的完整描述。"""

    prompt: str
    workspace: Path
    model: str
    provider_id: str = "cforge"
    provider_base_url: str = ""
    provider_env_key: str = "CFORGE_UPSTREAM_KEY"
    context_window: int = 262_144
    # sandbox 策略。默认用危险模式，理由见下方 SANDBOX_NOTES。
    # 可选值：read-only | workspace-write | danger-full-access
    sandbox: str = "danger-full-access"
    reasoning_effort: str = ""
    timeout_s: int = 900
    extra_config: dict[str, str] = field(default_factory=dict)
    bypass_hook_trust: bool = False
    skip_git_check: bool = True
    ephemeral: bool = True
    # 孤立 auth：codex 默认会尝试刷新 ChatGPT 登录 token，在受限网络下
    # 每 1~2 秒刷一次 403，几十次后拖垮整个 turn（实测 61s 全部消耗在这里）。
    # 用临时 CODEX_HOME 且不复制 auth.json，彻底避开这条路径。
    isolate_auth: bool = True


@dataclass
class CodexEvent:
    """从 codex JSONL 流里解析出的一条事件。

    codex 0.163.0 的真实结构（实测抓取，不是猜的）：
        {"type":"thread.started","thread_id":"..."}
        {"type":"item.completed","item":{"id":"item_0","type":"error","message":"..."}}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}
        {"type":"turn.completed","usage":{"input_tokens":11162,...}}

    → 关键：真正的语义在 `item` 子对象里，顶层 type 只是 "item.completed"。
    只读顶层会拿到空消息，这是初版的一个真实 bug。
    """

    kind: str
    payload: dict[str, Any]

    @property
    def item(self) -> dict[str, Any]:
        """取嵌套的 item（若有）。"""
        inner = self.payload.get("item")
        return inner if isinstance(inner, dict) else {}

    @property
    def item_type(self) -> str:
        return str(self.item.get("type") or "")

    @property
    def is_message(self) -> bool:
        if self.item_type == "agent_message":
            return True
        return self.kind in ("agent_message", "assistant_message", "message")

    @property
    def is_tool(self) -> bool:
        it = self.item_type
        if it in ("command_execution", "file_change", "mcp_tool_call"):
            return True
        return "tool" in self.kind or self.kind in (
            "exec_command_begin", "exec_command_end", "patch_apply_begin",
        )

    @property
    def is_error(self) -> bool:
        return self.item_type == "error" or self.kind == "error"

    @property
    def is_failure(self) -> bool:
        return self.kind in ("turn.failed", "error", "timeout")

    def text(self) -> str:
        """按优先级取文本：item.text > 顶层常见键。"""
        for source in (self.item, self.payload):
            for key in ("text", "message", "content", "output", "summary"):
                val = source.get(key)
                if isinstance(val, str) and val.strip():
                    return val
        return ""

    def usage(self) -> dict[str, int]:
        """turn.completed 里带 usage，供成本核算。"""
        raw = self.payload.get("usage")
        return raw if isinstance(raw, dict) else {}


def find_codex() -> str | None:
    """定位 codex 可执行文件（Windows 上常在 npm 全局目录）。"""
    exe = shutil.which("codex")
    if exe:
        return exe
    if sys.platform == "win32":
        candidate = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
        if candidate.is_file():
            return str(candidate)
    return None


def codex_version(exe: str | None = None) -> str:
    exe = exe or find_codex()
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=30,
        )
        return (out.stdout or out.stderr or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def build_config_flags(spec: CodexSpec) -> list[str]:
    """把 spec 转成一串 `-c key=value`。

    注意 codex 的 `-c` 值会先按 TOML 解析，失败才当字面字符串。
    所以 URL / 模型名这类含特殊字符的值不加引号也能正确传递。
    """
    pid = spec.provider_id
    flags: list[str] = [
        "-c", f"model={spec.model}",
        "-c", f"model_provider={pid}",
        "-c", f"model_context_window={spec.context_window}",
        "-c", f"model_providers.{pid}.name={pid}",
        "-c", f"model_providers.{pid}.base_url={spec.provider_base_url}",
        # 0.163.0 只支持 responses；这一行是硬约束，不是选择
        "-c", f"model_providers.{pid}.wire_api=responses",
        "-c", f"model_providers.{pid}.env_key={spec.provider_env_key}",
    ]
    if spec.reasoning_effort:
        flags += ["-c", f"model_reasoning_effort={spec.reasoning_effort}"]
    for key, val in spec.extra_config.items():
        flags += ["-c", f"{key}={val}"]
    return flags


def make_isolated_codex_home() -> Path:
    """建一个最小 CODEX_HOME，不含 auth.json。

    这样 codex 不会去找 ChatGPT 的登录态，也就不会触发 token 刷新重试。
    只放一个空 config.toml，其余全靠 `-c` 覆盖。
    """
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="cforge-codex-home-"))
    (home / "config.toml").write_text(
        "# CodexForge isolated home — 不配置任何 provider，全部走 -c 覆盖\n",
        encoding="utf-8",
    )
    return home


def find_codex() -> str | None:
    """定位 codex 可执行文件。

    Windows 上 `npm install -g` 装出来的是 **`codex.CMD`**（批处理包装器），
    不是原生 exe。这个事实带来一个严重后果，见 `_flatten_prompt`。
    """
    import shutil

    for name in ("codex", "codex.cmd", "codex.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _uses_cmd_wrapper(exe: str) -> bool:
    """该可执行文件是否是 Windows 的 .cmd/.bat 批处理包装器。

    .cmd 经由 cmd.exe 执行，**参数里的换行会被截断**（实测：`A\\nB\\nC`
    传给 .cmd 后目标进程只收到 `A`）。这是 WSH/cmd 的参数解析行为，
    与 codex 本身无关。
    """
    return exe.lower().endswith((".cmd", ".bat"))


def _flatten_prompt(prompt: str, exe: str) -> str:
    """把多行 prompt 压成单行（仅在经 .cmd 调用时）。

    ## 这是「prompt 明明传了但模型说没收到任务」的真正原因

    实测链路：`CodexFixAgent` 生成 891 字符、30 行的修复 prompt →
    `subprocess.run([codex.CMD, ..., "--json", prompt])` → **CMD 只把第一行
    交给了 node** → codex 收到的 prompt 是 `============...`（一行等号）→
    模型回复「your message may have been just a separator」。

    对照实验（`experiments/diag_codex_action.py`）用**单行**中文指令就
    17 秒成功改文件 —— 差别就在换行，不在模型能力、不在代理、不在权限。

    这个 bug 极难发现，因为：
      - 退出码 0、上游 200、turn 正常完成
      - `build_argv` 单测断言的是「argv 里有完整 prompt」——确实有，
        丢失发生在 spawn 之后的 cmd.exe 解析阶段，单测覆盖不到
      - 报告只会显示「改善不足」或「修复器放弃」

    解法：把换行替换为 `\\n` 字面量以外的东西。这里用 ` ¶ `（段落符），
    它不含 shell 元字符、模型也能理解分段。同时压缩连续空白，
    避免单行过长无结构。
    """
    if not _uses_cmd_wrapper(exe):
        return prompt
    # 保留段落感：把换行变成显式分隔符，而不是简单空格
    flat = prompt.replace("\r\n", "\n").replace("\r", "\n")
    flat = flat.replace("\n\n", " ¶ ¶ ")
    flat = flat.replace("\n", " ¶ ")
    # 折叠多余空格
    while "  " in flat:
        flat = flat.replace("  ", " ")
    return flat.strip()


def build_argv(spec: CodexSpec, exe: str | None = None) -> list[str]:
    """组装完整命令行。纯函数，便于单测断言。

    ⚠️ prompt 的形态取决于可执行文件类型：经 `.cmd` 包装时必须压成单行，
    否则换行会被 cmd.exe 截断（详见 `_flatten_prompt` 的实测记录）。
    """
    exe = exe or find_codex() or "codex"
    argv = [exe, "exec"]
    argv += build_config_flags(spec)
    argv += ["-C", str(spec.workspace)]
    argv += ["-s", spec.sandbox]
    if spec.skip_git_check:
        argv.append("--skip-git-repo-check")
    if spec.ephemeral:
        argv.append("--ephemeral")
    if spec.bypass_hook_trust:
        # 门禁 hook 必须被信任才会执行；非交互场景无人工确认入口。
        # 默认关闭 —— 实测它每次调用都会往 stdout 打两条 error 级噪声，
        # 且 hook 信任本来就该由交互式会话一次性完成。
        argv.append("--dangerously-bypass-hook-trust")
    argv += ["--json", _flatten_prompt(spec.prompt, exe)]
    return argv


def parse_stream(lines: Iterator[str]) -> Iterator[CodexEvent]:
    """解析 codex `--json` 输出的 JSONL。

    宽容策略：解析不了的行不抛异常，作为 raw 事件透传。
    理由：codex 偶尔会在 stdout 混入非 JSON 告警（如 Windows 的
    `failed to read PATH aliases`），不能因此中断整个运行。
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            yield CodexEvent(kind="raw", payload={"line": line})
            continue
        if not isinstance(data, dict):
            yield CodexEvent(kind="raw", payload={"line": line})
            continue
        kind = str(data.get("type") or data.get("event") or "unknown")
        yield CodexEvent(kind=kind, payload=data)


def run_codex(
    spec: CodexSpec,
    *,
    env_extra: dict[str, str] | None = None,
    on_event: Any = None,
    stderr_path: Path | None = None,
) -> tuple[int, list[CodexEvent]]:
    """同步跑一次 codex，收集全部事件。

    ## 硬超时是必需的（实测踩过）

    codex 内部有自己的重试逻辑。当它反复撞上被策略拒绝的命令时，会进入
    「重试 → 被拒 → 再重试」的循环，**可以无限跑下去**。实测一次卡了 13 分钟
    无任何文件变动，而 stderr 里只有被拒的命令。

    因此这里用 `proc.wait(timeout=...)` 加独立看门狗线程：
    - 超时后 kill 整个进程树（Windows 上 codex 会派生子进程）
    - 返回 `timeout` 事件，让上层记入停滞
    """
    argv = build_argv(spec)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("NO_COLOR", "1")

    # 隔离 auth：不复制 auth.json，避免 ChatGPT token 刷新重试
    if spec.isolate_auth:
        home = make_isolated_codex_home()
        env["CODEX_HOME"] = str(home)
        env.pop("OPENAI_API_KEY", None)

    stderr_target = stderr_path
    if stderr_target is None:
        stderr_target = Path(os.environ.get("TEMP", ".")) / "cforge-codex-stderr.log"
    stderr_target.parent.mkdir(parents=True, exist_ok=True)
    stderr_fh = open(stderr_target, "w", encoding="utf-8", errors="replace")

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=stderr_fh,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )
    events: list[CodexEvent] = []
    timed_out = {"hit": False}

    def _watchdog() -> None:
        """独立线程做硬超时 —— 主线程可能阻塞在 stdout 读取上。"""
        import threading
        done = threading.Event()

        def wait_and_kill() -> None:
            if done.wait(timeout=spec.timeout_s):
                return
            timed_out["hit"] = True
            _kill_tree(proc)

        t = threading.Thread(target=wait_and_kill, daemon=True)
        t.start()
        return done

    done_flag = _watchdog()
    try:
        assert proc.stdout is not None
        for ev in parse_stream(proc.stdout):
            events.append(ev)
            if on_event:
                on_event(ev)
        proc.wait(timeout=max(5, spec.timeout_s))
    except subprocess.TimeoutExpired:
        timed_out["hit"] = True
        _kill_tree(proc)
    finally:
        done_flag.set()
        if proc.poll() is None:
            _kill_tree(proc)
        stderr_fh.close()

    if timed_out["hit"]:
        events.append(CodexEvent("timeout", {"timeout_s": spec.timeout_s}))
        return -1, events
    return proc.returncode or 0, events


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉整个进程树。

    codex 会派生 shell 子进程，只 kill 父进程会留孤儿占着管道。
    Windows 用 `taskkill /F /T`，POSIX 用进程组。
    """
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def probe_wire_api(base_url: str, api_key: str, model: str, timeout: int = 25) -> dict:
    """探测上游是否原生支持 Responses API。

    为什么要探：codex 只能走 responses。若上游只支持 chat/completions，
    就必须先起 compat proxy，否则 codex 会在启动阶段直接失败。
    """
    import urllib.error
    import urllib.request

    result = {"base_url": base_url, "supports_responses": False, "detail": ""}
    if not base_url or not api_key:
        result["detail"] = "缺少 base_url 或 api_key，跳过探测"
        return result
    payload = json.dumps({"model": model, "input": "ping", "max_output_tokens": 8}).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # 强制直连：本机代理端口每会话变化，残留的失效代理会让请求卡在握手阶段
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            result["supports_responses"] = resp.status == 200
            result["detail"] = body[:300]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        result["detail"] = f"HTTP {exc.code}: {body[:280]}"
        # 401 说明路由存在但鉴权失败 → 端点存在，视为支持
        result["supports_responses"] = exc.code in (400, 401, 422)
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"{type(exc).__name__}: {exc}"
    return result
