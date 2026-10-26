"""LLM 网关：OpenAI 兼容的单入口，带降级链与结构化错误。

只依赖标准库，理由有两条（都可写进参赛文档）：
1. 评审复现时不需要联网装包；
2. 神库依赖越少，`pip install` 失败的风险越低。

核心行为：
- 按 `GatewayConfig.profiles` 顺序尝试；遇到 `switch_on_status` 里的状态码直接换下一个模型
- 同一模型内做指数退避重试
- 所有失败都返回结构化 `GatewayError`，绝不抛裸异常给上层 agent 循环
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


UA = "CodexForge/0.1 (+https://github.com/Pawnnwap)"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class ChatResult:
    ok: bool
    content: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    # 失败信息
    error_code: int | None = None
    error_kind: str = ""
    error_detail: str = ""
    # 诊断：这次调用实际走了几个 profile、重试几次
    attempts: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0


def _post_json(url: str, api_key: str, payload: dict, timeout_s: int) -> tuple[int, str]:
    """发 POST，返回 (status, body)。HTTP 错误也当作正常返回，便于分类。

    网络层加固（本机实测教训）：
    - 默认 urllib 会读 `http_proxy`/`https_proxy` 环境变量。本机代理端口
      每会话变化，残留的失效代理会让所有请求卡到 `handshake timed out`。
    - 因此显式装空 ProxyHandler：直连。若将来需要走代理，由调用方
      显式设置 `CFORGE_PROXY`，而不是依赖环境残留。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": UA,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    proxy = os.environ.get("CFORGE_PROXY", "").strip()
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 强制直连
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — 网络层错误统一归类
        return -1, f"{type(exc).__name__}: {exc}"


def _classify(status: int, body: str) -> tuple[str, str]:
    """把失败归类成稳定字符串，供退避策略与上层判断。"""
    if status == 200:
        return "ok", ""
    if status == -1:
        return "network_error", body[:300]
    if status == 401:
        if "A0211" in body or "token expired" in body:
            return "bad_key", "书生 token 无效/过期（A0211）"
        return "unauthorized", body[:300]
    if status == 402:
        return "insufficient_quota", body[:300]
    if status == 404:
        return "model_not_found", body[:300]
    if status == 429:
        # OpenRouter 免费池的关键诊断字段
        if "upstream_provider_shared_pool" in body:
            return "rate_limited_shared_pool", body[:400]
        return "rate_limited", body[:400]
    if status >= 500:
        return "upstream_error", body[:300]
    return f"http_{status}", body[:300]


class LLMGateway:
    """OpenAI 兼容网关。线程内同步调用，异步由上层用 to_thread 包装。"""

    def __init__(self, config: GatewayConfig):
        self.config = config
        # 累计用量，供「成本/预算」类评分点引用
        self.total_usage = Usage()
        # 记录每个模型的历史成功率，便于之后做智能路由（当前只记录）
        self.stats: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ 内部
    def _bump(self, model: str, key: str) -> None:
        slot = self.stats.setdefault(model, {"ok": 0, "fail": 0})
        slot[key] += 1

    def _sleep(self, attempt: int) -> None:
        delay = min(self.config.backoff_base_s * (2**attempt), self.config.backoff_cap_s)
        time.sleep(delay)

    # ------------------------------------------------------------------ 公开
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        """按降级链调用。永不抛异常，失败信息在 ChatResult 里。"""
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        last_error: tuple[int | None, str, str] = (None, "", "no profile configured")

        if not self.config.profiles:
            return ChatResult(
                ok=False,
                error_kind="no_profile",
                error_detail="GatewayConfig.profiles 为空",
                elapsed_s=0.0,
            )

        for profile in self.config.profiles:
            key = profile.api_key()
            if not key:
                attempts.append({
                    "model": profile.model_id,
                    "kind": "missing_api_key",
                    "detail": f"环境变量 {profile.api_key_env} 未设置",
                })
                last_error = (None, "missing_api_key", f"{profile.api_key_env} 未设置")
                continue

            for attempt in range(self.config.retries_per_model + 1):
                payload: dict[str, Any] = {
                    "model": profile.model_id,
                    "messages": messages,
                    "temperature": temperature,
                }
                if max_tokens is not None:
                    payload["max_tokens"] = max_tokens
                if tools:
                    payload["tools"] = tools
                    if tool_choice:
                        payload["tool_choice"] = tool_choice
                if response_format:
                    payload["response_format"] = response_format

                status, body = _post_json(
                    f"{profile.base_url.rstrip('/')}/chat/completions",
                    key,
                    payload,
                    self.config.request_timeout_s,
                )
                kind, detail = _classify(status, body)

                if kind == "ok":
                    parsed = self._parse(body)
                    if parsed is None:
                        attempts.append({
                            "model": profile.model_id,
                            "kind": "malformed_response",
                            "detail": body[:200],
                        })
                        last_error = (status, "malformed_response", body[:300])
                        self._bump(profile.model_id, "fail")
                        break  # 换模型
                    self._bump(profile.model_id, "ok")
                    self.total_usage.add(parsed.usage)
                    parsed.attempts = attempts
                    parsed.elapsed_s = round(time.perf_counter() - started, 2)
                    return parsed

                attempts.append({
                    "model": profile.model_id,
                    "attempt": attempt,
                    "status": status,
                    "kind": kind,
                    "detail": detail[:200],
                })
                self._bump(profile.model_id, "fail")
                last_error = (status, kind, detail)

                if status in self.config.switch_on_status:
                    break  # 该模型不可用，换下一个，不浪费时间重试
                if attempt < self.config.retries_per_model:
                    self._sleep(attempt)

        return ChatResult(
            ok=False,
            error_code=last_error[0],
            error_kind=last_error[1],
            error_detail=last_error[2],
            attempts=attempts,
            elapsed_s=round(time.perf_counter() - started, 2),
        )

    def _parse(self, body: str) -> ChatResult | None:
        try:
            data = json.loads(body)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            raw_usage = data.get("usage") or {}
            usage = Usage(
                prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                total_tokens=int(raw_usage.get("total_tokens") or 0),
            )
            return ChatResult(
                ok=True,
                content=message.get("content") or "",
                model=data.get("model") or "",
                usage=usage,
            )
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 探测
    def probe(self) -> dict[str, Any]:
        """轻量连通性探测，供 UI/CLI 显示。不做重试，快速失败。

        实现注意：不能临时替换 `self.config`（之前那样写会让 `chat()` 内部
        读到被替换的值，且 `_sleep` 的 0 退避会掩盖真实行为）。
        改为构造一个独立网关，探测完即丢弃。
        """
        probe_cfg = GatewayConfig(
            profiles=self.config.profiles,
            request_timeout_s=min(self.config.request_timeout_s, 40),
            retries_per_model=0,
            backoff_base_s=0.0,
            backoff_cap_s=0.0,
            switch_on_status=self.config.switch_on_status,
        )
        side = LLMGateway(probe_cfg)
        result = side.chat([{"role": "user", "content": "reply with: OK"}], max_tokens=8)
        return {
            "ok": result.ok,
            "model": result.model,
            "error_kind": result.error_kind,
            "error_detail": result.error_detail,
            "attempts": result.attempts,
            "elapsed_s": result.elapsed_s,
        }
