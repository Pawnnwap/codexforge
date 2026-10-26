"""把真实 codex 接进长程循环：实验的执行引擎。

## 关键设计：每轮 prompt 如何组织

这决定了「自主反馈」是否真的有效。每轮 prompt 包含五块：

1. 任务原文（不变）
2. 上一轮的**具体失败事实**（不是「再试一次」这种空话）
3. 未达成的验收项（确定性，非模型自评）
4. 整洁度问题的文件+行号清单（可定位）
5. 停滞警告（若触发）

## 一个反直觉的取舍

**不给模型看「成功了多少次」**。只给失败与待办。
理由：模型看到「已经做了很多」会倾向于收尾，而不是继续深挖 ——
这在长程任务里是致命倾向。

## 成本控制

每轮都真实调用 codex，成本按 token 计。所以：
- 单轮 max_turns 默认 6，不追求跑满
- 一旦达标立即停
- 停滞 2 级就停，不浪费额度
"""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from cforge.harness import codex_backend as cb
from cforge.harness.compat_proxy import CompatProxy, ProxyConfig
from cforge.longrun.loop import LongRunConfig, LongRunLoop, LongRunResult
from cforge.longrun.state import TargetState
from cforge.quality.metrics import evaluate
from experiments.harness import Task


@dataclass
class CodexTurnRunner:
    """用真实 codex 跑一轮。"""

    workspace: Path
    task: Task
    model: str
    base_url: str
    api_key: str
    timeout_s: int = 420
    # 上一轮的反馈，用于构造本轮 prompt
    _last_feedback: str = ""
    calls: int = 0
    total_tokens: dict = field(default_factory=dict)

    def _build_prompt(self, state: TargetState) -> str:
        parts = [self.task.prompt, ""]

        # 未达成项：来自外部目标检查，不是模型自评
        ok, unmet = self.task.verify(self.workspace)
        if unmet and not ok:
            parts.append("## 当前未达成（必须解决）")
            for u in unmet if isinstance(unmet, list) else [unmet]:
                parts.append(f"- {u}")
            parts.append("")

        # 上一轮的失败方向
        failed = state.failed_directions()
        if failed:
            parts.append("## 已失败的做法（不要重复）")
            for d in failed[-4:]:
                parts.append(f"- {d.text[:80]} → {d.failure_reason[:100]}")
            parts.append("")

        # 整洁度问题：可定位到文件+行
        qr = evaluate(self.workspace)
        highs = qr.by_severity("high")
        if highs:
            parts.append("## 代码整洁度问题（先修这些）")
            for s in highs[:6]:
                parts.append(f"- {s.path}:{s.line} {s.kind} — {s.detail}")
            parts.append("")

        # 停滞警告
        if state.stagnation_count > 0:
            parts.append(
                f"⚠ 连续 {state.stagnation_count} 轮工作区无实质变化。"
                "必须换一个完全不同的实现策略。"
            )
            parts.append("")

        parts.append("完成后确保所有验收项都满足。直接动手改文件，不要只描述方案。")
        return "\n".join(parts)

    def run_turn(self, prompt: str, state: TargetState) -> tuple[str, bool]:
        self.calls += 1
        full_prompt = self._build_prompt(state)

        spec = cb.CodexSpec(
            prompt=full_prompt,
            workspace=self.workspace,
            model=self.model,
            provider_base_url=self.base_url,
            context_window=262_144,
            timeout_s=self.timeout_s,
            sandbox="danger-full-access",
            isolate_auth=True,
        )
        stderr_path = Path(tempfile.gettempdir()) / f"cforge-exp-{os.getpid()}.log"
        try:
            exit_code, events = cb.run_codex(
                spec,
                env_extra={spec.provider_env_key: self.api_key},
                stderr_path=stderr_path,
            )
        except Exception as exc:  # noqa: BLE001
            return f"codex 调用异常：{exc}", False

        messages: list[str] = []
        for ev in events:
            if ev.is_message:
                t = ev.text()
                if t:
                    messages.append(t)
            if ev.kind == "turn.completed":
                for k, v in ev.usage().items():
                    if isinstance(v, int):
                        self.total_tokens[k] = self.total_tokens.get(k, 0) + v

        summary = " | ".join(messages)[:500] if messages else f"exit={exit_code}"
        failed = any(
            ev.kind in ("turn.failed", "error") and "metadata" not in ev.text().lower()
            for ev in events
        )
        if failed and not messages:
            # 把失败记为「方向失败」，供下一轮避让
            state.record_direction(
                full_prompt[:120], "failed",
                f"codex 返回失败，exit={exit_code}",
            )
        return summary, False


@dataclass
class ExperimentRunner:
    """跑一组实验：任务集 × 配置。"""

    cfg: object          # ExperimentConfig
    verbose: bool = True

    def run(self) -> dict:
        from experiments.harness import summarize_result

        cfg = self.cfg
        results = []
        for task in cfg.tasks:
            ws = Path(cfg.workspace_root) / cfg.name / task.id
            if ws.exists():
                import shutil
                shutil.rmtree(ws, ignore_errors=True)
            ws.mkdir(parents=True, exist_ok=True)
            (ws / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")

            if self.verbose:
                print(f"\n[{cfg.name}] {task.id} — {task.name}", flush=True)

            # 起 proxy：codex 只认 responses，OpenRouter 只有 chat
            proxy = CompatProxy(ProxyConfig(
                upstream_base_url=self._base_url(cfg.backend),
                upstream_api_key=self._api_key(cfg.backend),
                model=cfg.model,
                listen_port=0,
            ))
            url = proxy.start()
            try:
                runner = CodexTurnRunner(
                    workspace=ws,
                    task=task,
                    model=cfg.model,
                    base_url=url,
                    api_key=self._api_key(cfg.backend),
                )
                loop = LongRunLoop(
                    workspace=ws,
                    runner=runner,
                    goal_checker=lambda w, t=task: t.verify(w),
                    config=LongRunConfig(
                        max_turns=cfg.max_turns,
                        min_quality=cfg.min_quality if cfg.min_quality is not None else 0.0,
                        patience=2,
                    ),
                    target=task.prompt,
                    verbose=self.verbose,
                )
                result: LongRunResult = loop.run()
                summary = summarize_result(result.as_dict())
                summary.update({
                    "task": task.id,
                    "task_name": task.name,
                    "codex_calls": runner.calls,
                    "tokens": runner.total_tokens,
                    "why_multi_turn": task.why_multi_turn,
                })
                results.append(summary)
                if self.verbose:
                    print(f"  → {summary['stop_reason']} turns={summary['turns']} "
                          f"goal={summary['goal_met']} q={summary['quality_final']}", flush=True)
            finally:
                proxy.stop()

        return {
            "name": cfg.name,
            "model": cfg.model,
            "backend": cfg.backend,
            "tasks": results,
            "aggregate": self._aggregate(results),
        }

    def _aggregate(self, results: list[dict]) -> dict:
        if not results:
            return {}
        n = len(results)
        return {
            "n_tasks": n,
            "goal_rate": round(sum(1 for r in results if r["goal_met"]) / n, 3),
            "avg_turns": round(sum(r["turns"] for r in results) / n, 2),
            "avg_quality_final": round(sum(r["quality_final"] for r in results) / n, 1),
            "avg_quality_delta": round(sum(r["quality_delta"] for r in results) / n, 1),
            "total_stagnation": sum(r["stagnation_hits"] for r in results),
            "total_tokens": sum(
                r["tokens"].get("total_tokens", 0) for r in results
            ),
        }

    @staticmethod
    def _base_url(backend: str) -> str:
        if backend == "intern":
            return "https://internlm.intern-ai.org.cn/api/v1"
        return "https://openrouter.ai/api/v1"

    @staticmethod
    def _api_key(backend: str) -> str:
        env = "INTERN_API_KEY" if backend == "intern" else "OPENROUTER_API_KEY"
        return os.environ.get(env, "").strip()
