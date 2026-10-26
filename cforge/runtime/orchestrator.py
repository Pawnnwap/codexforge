"""编排层：把 gateway / codex backend / gates / supervisor 串成一条可跑的通路。

## 一个完整 run 的阶段

```
bootstrap  → 解析配置、定位 codex、检查门禁、探测上游 wire_api
proxy      → 若上游不支持 responses，起 compat proxy 并改指向它
plan       → 生成 protocol 提示（骨架：直接注入）
execute    → 调 codex exec，流式收集事件
judge      → 确定性证据裁决（Goal Guard）
audit      → 落盘审计链
```

## 骨架诚实声明

`execute` 阶段当前**只跑一轮**。多轮「判官驳回 → 重新执行」的控制流
已在 `SupervisorLoop` 里实现并单测，但编排层的循环接线留到下一步——
先把单轮通路跑通、把每一步的产物落盘，再叠重试语义。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cforge.checks.gates import GateSet, GateVerdict
from cforge.gateway.client import LLMGateway
from cforge.gateway.profiles import Backend, GatewayConfig
from cforge.harness import codex_backend as cb
from cforge.harness.compat_proxy import CompatProxy, ProxyConfig
from cforge.harness.supervisor import (
    DeterministicProvider,
    Phase,
    Protocol,
    SupervisorLoop,
    Verdict,
)


def _base_url_for(backend: str) -> str:
    """按后端取 base_url。

    实测定论：书生必须用 internlm.intern-ai.org.cn；
    api.intern-ai.org.cn 返回 403 RBAC，是内部网关，不要用。

    `CFORGE_BASE_URL_OVERRIDE` 优先（e2e 假上游演练用，生产不设置）——
    这样 key 到位前就能演练完整链路，做到「key 一到 1 小时出证据」。
    """
    override = os.environ.get("CFORGE_BASE_URL_OVERRIDE", "").strip()
    if override:
        return override.rstrip("/")
    if str(backend).lower() == "intern":
        return "https://internlm.intern-ai.org.cn/api/v1"
    return "https://openrouter.ai/api/v1"


def _resolve_api_key(backend: str) -> str:
    env = "INTERN_API_KEY" if str(backend).lower() == "intern" else "OPENROUTER_API_KEY"
    return os.environ.get(env, "").strip()


@dataclass
class RunConfig:
    """一次运行的完整配置。"""

    workspace: Path
    prompt: str
    protocol: Protocol = field(default_factory=Protocol)
    backend: Backend = Backend.OPENROUTER
    model_override: str = ""
    context_window: int = 262_144
    sandbox: str = "workspace-write"
    timeout_s: int = 900
    # 门禁脚本目录；空则跳过门禁
    gate_scripts_dir: Path | None = None
    # 审计与产物落盘目录
    run_dir: Path = field(default_factory=lambda: Path("runs"))
    # 是否在 codex 里真正执行（False = dry-run，只打印命令）
    execute_codex: bool = True
    # 是否强制起 proxy（即使上游声称支持 responses）
    force_proxy: bool = False
    # 0 = 自动选空闲端口。Windows 上固定端口常撞 WinError 10013。
    proxy_port: int = 0


@dataclass
class RunReport:
    """一次运行的产物摘要。"""

    ok: bool
    phase: str
    gateway_probe: dict[str, Any] = field(default_factory=dict)
    codex_argv: list[str] = field(default_factory=list)
    codex_exit: int = 0
    n_events: int = 0
    messages: list[str] = field(default_factory=list)
    tool_events: list[str] = field(default_factory=list)
    # 从 turn.completed 汇总的 token 用量
    codex_usage: dict[str, int] = field(default_factory=dict)
    gate_verdicts: list[dict[str, Any]] = field(default_factory=list)
    verdict: Verdict | None = None
    proxy_url: str = ""
    elapsed_s: float = 0.0
    error: str = ""
    run_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "phase": self.phase,
            "gateway_probe": self.gateway_probe,
            "codex_argv": self.codex_argv,
            "codex_exit": self.codex_exit,
            "n_events": self.n_events,
            "messages": self.messages,
            "tool_events": self.tool_events,
            "codex_usage": self.codex_usage,
            "gate_verdicts": self.gate_verdicts,
            "verdict": None if self.verdict is None else {
                "phase": self.verdict.phase.value,
                "targets_met": self.verdict.targets_met,
                "targets_unmet": self.verdict.targets_unmet,
                "done": self.verdict.done,
                "unverified": self.verdict.unverified,
                "next_action": self.verdict.next_action,
                "turn": self.verdict.turn,
            },
            "proxy_url": self.proxy_url,
            "elapsed_s": self.elapsed_s,
            "error": self.error,
            "run_dir": self.run_dir,
        }


class Orchestrator:
    """把各层串起来。持有网关实例以复用用量统计。"""

    def __init__(self, cfg: RunConfig, gateway: LLMGateway | None = None):
        self.cfg = cfg
        self.gateway = gateway or LLMGateway(GatewayConfig.from_env())

    # ------------------------------------------------------------------ 工具
    def _resolve_profile(self):
        """挑一个 provider profile：优先 backend 匹配，其次第一个有 key 的。"""
        profiles = self.gateway.config.profiles
        if not profiles:
            return None
        matching = [p for p in profiles if p.backend is self.cfg.backend]
        pool = matching or profiles
        for p in pool:
            if self.cfg.model_override:
                from cforge.gateway.profiles import ModelProfile
                return ModelProfile(
                    backend=p.backend,
                    model_id=self.cfg.model_override,
                    base_url=p.base_url,
                    api_key_env=p.api_key_env,
                    max_context=p.max_context,
                    supports_tools=p.supports_tools,
                    supports_vision=p.supports_vision,
                    note="run override",
                )
            return p
        return None

    def _write_audit(self, report: RunReport, extra: dict[str, Any] | None = None) -> None:
        run_dir = Path(report.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {"report": report.to_dict(), "extra": extra or {},
                   "written_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        (run_dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
        )

    # ------------------------------------------------------------------ 主流程
    def run(self) -> RunReport:
        started = time.perf_counter()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_dir = Path(self.cfg.run_dir) / f"{stamp}-{os.getpid()}"
        report = RunReport(ok=False, phase="bootstrap", run_dir=str(run_dir))
        run_dir.mkdir(parents=True, exist_ok=True)

        profile = self._resolve_profile()
        if profile is None:
            report.error = "没有可用的 provider profile"
            report.phase = "failed"
            self._write_audit(report)
            return report

        api_key = profile.api_key()
        if not api_key:
            report.error = f"环境变量 {profile.api_key_env} 未设置（backend={profile.backend.value}）"
            report.phase = "failed"
            self._write_audit(report)
            return report

        # ---- 1) 探测上游 wire_api ----
        report.phase = "probe"
        probe = cb.probe_wire_api(profile.base_url, api_key, profile.model_id)
        report.gateway_probe = {
            "backend": profile.backend.value,
            "model": profile.model_id,
            "base_url": profile.base_url,
            **probe,
        }

        # ---- 2) 决定是否需要 compat proxy ----
        proxy: CompatProxy | None = None
        target_base_url = profile.base_url
        needs_proxy = self.cfg.force_proxy or not probe.get("supports_responses")
        if needs_proxy:
            report.phase = "proxy"
            proxy = CompatProxy(ProxyConfig(
                upstream_base_url=profile.base_url,
                upstream_api_key=api_key,
                model=profile.model_id,
                listen_port=self.cfg.proxy_port,
            ))
            try:
                target_base_url = proxy.start()
                report.proxy_url = target_base_url
            except Exception as exc:  # noqa: BLE001
                report.error = f"proxy 启动失败：{type(exc).__name__}: {exc}"
                report.phase = "failed"
                self._write_audit(report)
                return report

        try:
            # ---- 3) 组装 codex 调用 ----
            report.phase = "execute"
            spec = cb.CodexSpec(
                prompt=self.cfg.prompt,
                workspace=self.cfg.workspace,
                model=profile.model_id,
                provider_base_url=target_base_url,
                context_window=self.cfg.context_window,
                sandbox=self.cfg.sandbox,
                timeout_s=self.cfg.timeout_s,
            )
            report.codex_argv = cb.build_argv(spec)
            # 写入 argv 便于复现（去掉可能的密钥，实际不含）
            (run_dir / "codex_argv.json").write_text(
                json.dumps(report.codex_argv, ensure_ascii=False, indent=1), encoding="utf-8",
            )

            if self.cfg.execute_codex:
                env_extra = {spec.provider_env_key: api_key}
                stderr_log = run_dir / "codex_stderr.log"
                exit_code, events = cb.run_codex(
                    spec, env_extra=env_extra, stderr_path=stderr_log,
                )
                report.codex_exit = exit_code
                report.n_events = len(events)
                usage_totals: dict[str, int] = {}
                for ev in events:
                    if ev.is_message:
                        txt = ev.text()
                        if txt:
                            report.messages.append(txt[:2000])
                    if ev.is_tool:
                        report.tool_events.append(ev.item_type or ev.kind)
                    if ev.is_error:
                        msg = ev.text()
                        # "Model metadata not found" 是无害提示，不算失败
                        if msg and "metadata" not in msg.lower():
                            report.error = msg[:400]
                    if ev.is_failure and ev.kind == "turn.failed":
                        err = ev.payload.get("error") or {}
                        report.error = str(err.get("message", ""))[:400]
                    if ev.kind == "turn.completed":
                        for k, v in ev.usage().items():
                            if isinstance(v, int):
                                usage_totals[k] = usage_totals.get(k, 0) + v
                report.codex_usage = usage_totals
                # stderr 里若出现重试耗尽，补一条可读错误
                if stderr_log.is_file():
                    tail = stderr_log.read_text(encoding="utf-8", errors="replace")
                    if "exceeded retry limit" in tail:
                        report.error = report.error or "codex 上游重试耗尽（429 限流）"
                    (run_dir / "codex_stderr_head.txt").write_text(
                        tail[:4000], encoding="utf-8",
                    )
            else:
                report.n_events = 0

            # ---- 4) 门禁（对工作区里可能被改动的文件跑一遍）----
            report.phase = "gates"
            if self.cfg.gate_scripts_dir:
                gs = GateSet(scripts_dir=Path(self.cfg.gate_scripts_dir))
                for py_file in sorted(self.cfg.workspace.rglob("*.py"))[:5]:
                    for name in ("py_edit_gate",):
                        if name not in gs.available():
                            continue
                        verdict: GateVerdict = gs.run(
                            name, file_path=str(py_file), cwd=str(self.cfg.workspace),
                        )
                        report.gate_verdicts.append({
                            "gate": verdict.gate,
                            "file": str(py_file.relative_to(self.cfg.workspace)),
                            "ran": verdict.ran,
                            "clean": verdict.clean,
                            "advisory": verdict.advisory[:500],
                            "error": verdict.error,
                        })

            # ---- 5) 判官（确定性证据）----
            report.phase = "judge"
            loop = SupervisorLoop(
                protocol=self.cfg.protocol,
                workspace=self.cfg.workspace,
                provider=DeterministicProvider(),
            )
            verdict = loop.evaluate(claimed_done=True)
            report.verdict = verdict
            report.phase = verdict.phase.value
            report.ok = report.codex_exit == 0
            (run_dir / "supervisor_audit.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in loop.audit) + "\n",
                encoding="utf-8",
            )
        finally:
            if proxy is not None:
                proxy.stop()

        report.elapsed_s = round(time.perf_counter() - started, 2)
        self._write_audit(report, {"proxy_audit": proxy.cfg.audit if proxy else []})
        return report

    # ------------------------------------------------------------------ 便捷
    def write_protocol(self) -> Path:
        """把 protocol 落盘到工作区，供 codex 读取。"""
        path = self.cfg.workspace / "protocol.md"
        path.write_text(self.cfg.protocol.to_markdown(), encoding="utf-8")
        return path
