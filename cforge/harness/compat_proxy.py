"""协议适配代理：让只认 `responses` 的 codex 驱动只提供 `chat/completions` 的上游。

## 这个模块存在的唯一理由（实测证据）

codex 0.163.0：
```
Error loading config.toml: `wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"` in your provider config.
```

书生 / OpenRouter：只保证 `/chat/completions`（书生在鉴权前统一 401，
无法用错误码区分路由是否存在，因此不能假设它支持 responses）。

→ 结论：必须有本代理，否则这条技术路线根本不成立。

## 转换职责

请求方向（Responses → Chat）：
- `input`（字符串或 messages 数组）→ `messages`
- `instructions` → system message
- `tools`（Responses 的函数格式）→ Chat 的 functions 格式
- `max_output_tokens` → `max_tokens`

响应方向（Chat → Responses）：
- `choices[0].message.content` → `output[].content[].text`
- `tool_calls` → `output[]` 里的 function_call 项
- `usage` 字段名映射

## 故意不做的事

- **不做 SSE 流式**：骨架阶段先用整包响应。codex 对非流式响应是可用的
  （它自己处理聚合），省掉一大块状态机复杂度。流式留 TODO。
- **不做「400 学习 profile」**：那是 super-opencode 的做法，需要缓存与大量
  试探请求。骨架阶段用静态映射 + 显式配置更可控。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Responses API 请求体 → Chat Completions 请求体。"""

    # 角色映射：codex 的 Responses 协议用 developer 角色承载系统提示，
    # 但书生（chat.intern-ai.org.cn）上游实测拒绝 developer（400 -20019
    # 「不允许的消息角色」）。developer 语义等同 system，映射过去。
    _ROLE_MAP = {
        "developer": "system",
    }
    messages: list[dict[str, Any]] = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            # 已经是 message 形态
            if "role" in item:
                role = _ROLE_MAP.get(item["role"], item["role"])
                content = item.get("content")
                if isinstance(content, list):
                    # Responses 的 content 数组 → 纯文本拼接
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") in ("input_text", "output_text", "text"):
                                parts.append(str(c.get("text", "")))
                    content = "\n".join(p for p in parts if p)
                messages.append({"role": role, "content": content or ""})
            # function_call_output（工具结果回填）
            elif item.get("type") == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": str(item.get("output", "")),
                })
            elif item.get("type") == "function_call":
                # 上一轮模型的工具调用，回填到 assistant 消息里
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments") or "{}",
                        },
                    }],
                })

    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
    }
    if body.get("max_output_tokens"):
        out["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]

    tools = body.get("tools")
    if isinstance(tools, list):
        converted = []
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "function":
                # Responses 格式已经是扁平的 {type, name, parameters}
                if "function" in t:
                    converted.append(t)  # 已是 chat 格式
                else:
                    converted.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters") or {"type": "object"},
                        },
                    })
        if converted:
            out["tools"] = converted
            if body.get("tool_choice"):
                out["tool_choice"] = body["tool_choice"]
    return out


def chat_to_responses(chat: dict[str, Any], model: str = "") -> dict[str, Any]:
    """Chat Completions 响应 → Responses API 响应体。

    codex 只读 `output` 数组与 `usage`；缺字段会直接报协议错误，
    所以每个字段都必须显式给出（哪怕为空列表）。
    """
    choices = chat.get("choices") or []
    message = (choices[0].get("message") if choices else {}) or {}
    output: list[dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, str) and text.strip():
        output.append({
            "type": "message",
            "id": "msg_0",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })

    for idx, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        output.append({
            "type": "function_call",
            "id": f"fc_{idx}",
            "call_id": call.get("id") or f"call_{idx}",
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments") or "{}",
            "status": "completed",
        })

    if not output:
        # 空响应也必须给一个合法的 message，否则 codex 判定协议违约
        output.append({
            "type": "message",
            "id": "msg_0",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        })

    raw_usage = chat.get("usage") or {}
    usage = {
        "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
        "output_tokens": int(raw_usage.get("completion_tokens") or 0),
        "total_tokens": int(raw_usage.get("total_tokens") or 0),
    }

    finish = (choices[0].get("finish_reason") if choices else None) or "stop"
    return {
        "id": chat.get("id") or "resp_0",
        "object": "response",
        "created_at": chat.get("created") or 0,
        "model": chat.get("model") or model,
        "status": "completed",
        "output": output,
        "usage": usage,
        # codex 会读这两个字段判断是否还有后续
        "parallel_tool_calls": True,
        "incomplete_details": None if finish != "length" else {"reason": "max_output_tokens"},
    }


@dataclass
class ProxyConfig:
    upstream_base_url: str
    upstream_api_key: str
    model: str = ""
    listen_host: str = "127.0.0.1"
    # 0 = 由系统分配空闲端口（推荐）。指定固定端口在 Windows 上常撞
    # WinError 10013（端口被保留或权限受限），实测量身踩过。
    listen_port: int = 0
    timeout_s: int = 180
    # 上游限流时的重试策略。
    # 实测：OpenRouter 免费模型走共享池，codex 的请求带完整 system prompt
    # （~4 条消息、上万 token），比单发 curl 更容易 429。不重试等于不可用。
    retries: int = 4
    retry_backoff_s: float = 4.0
    retry_backoff_cap_s: float = 25.0
    # 遇到这些状态码才重试；4xx 里除了 429 都是请求本身的问题，重试无意义
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504, -1)
    # 记录每次转发的摘要，便于审计（不记 API key）
    audit: list[dict[str, Any]] = field(default_factory=list)


def responses_sse_events(chat: dict[str, Any], model: str = "") -> list[tuple[str, dict]]:
    """把一次 chat 响应转成一串 Responses SSE 事件。

    ## 为什么必须做这个（实测踩过的坑）

    codex 通过 `wire_api=responses` 发请求时**总是以流式方式消费**。
    若代理返回整包 JSON，codex 会报：
        `stream disconnected before completion: stream closed before response.completed`
    并在重试 5 次后失败 —— 上游明明返回了 200，链路却不通。

    ## 事件序列（严格按 OpenAI Responses API 的流式契约）

    response.created
    response.output_item.added
    response.content_part.added
    response.output_text.delta        （可多次）
    response.content_part.done
    response.output_item.done
    response.completed                （必须，codex 靠它判定成功）

    函数调用时用 response.function_call_arguments.delta / .done 代替 output_text。
    """
    resp_id = chat.get("id") or "resp_0"
    created = chat.get("created") or 0
    final = chat_to_responses(chat, model)
    base = {"id": resp_id, "object": "response", "created_at": created,
            "model": final.get("model") or model, "status": "in_progress"}

    events: list[tuple[str, dict]] = []
    events.append(("response.created", {"type": "response.created",
                                        "response": dict(base)}))
    events.append(("response.in_progress", {"type": "response.in_progress",
                                            "response": dict(base)}))

    for idx, item in enumerate(final["output"]):
        if item["type"] == "message":
            item_id = item["id"]
            events.append(("response.output_item.added", {
                "type": "response.output_item.added", "output_index": idx,
                "item": {"id": item_id, "type": "message", "role": "assistant",
                         "status": "in_progress", "content": []},
            }))
            text = item["content"][0]["text"] if item.get("content") else ""
            events.append(("response.content_part.added", {
                "type": "response.content_part.added", "item_id": item_id,
                "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }))
            if text:
                events.append(("response.output_text.delta", {
                    "type": "response.output_text.delta", "item_id": item_id,
                    "output_index": idx, "content_index": 0, "delta": text,
                }))
            events.append(("response.content_part.done", {
                "type": "response.content_part.done", "item_id": item_id,
                "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            }))
            events.append(("response.output_item.done", {
                "type": "response.output_item.done", "output_index": idx,
                "item": item,
            }))
        elif item["type"] == "function_call":
            item_id = item["id"]
            events.append(("response.output_item.added", {
                "type": "response.output_item.added", "output_index": idx,
                "item": {**item, "status": "in_progress"},
            }))
            events.append(("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "item_id": item_id, "output_index": idx,
                "delta": item.get("arguments", ""),
            }))
            events.append(("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": item_id, "output_index": idx,
                "arguments": item.get("arguments", ""),
            }))
            events.append(("response.output_item.done", {
                "type": "response.output_item.done", "output_index": idx,
                "item": item,
            }))

    done = dict(final)
    done["status"] = "completed"
    events.append(("response.completed", {"type": "response.completed",
                                          "response": done}))
    return events


def _upstream_post(cfg: ProxyConfig, path: str, payload: dict) -> tuple[int, str]:
    """向��游发请求，带限流重试。

    重试的理由很具体：codex 每次请求都带上万 token 的 system prompt，
    免费模型的上游共享池对这类大请求限流更严。实测同一模型
    单发 "say OK" 能过、codex 的真实请求 429 —— 不重试则整条链路不可用。
    """
    import time as _time

    body = json.dumps(payload).encode()
    last: tuple[int, str] = (-1, "not attempted")
    for attempt in range(cfg.retries + 1):
        req = urllib.request.Request(
            f"{cfg.upstream_base_url.rstrip('/')}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {cfg.upstream_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # 强制直连：本机代理端口不稳定，残留代理会拖死转发
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=cfg.timeout_s) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            last = (exc.code, text)
            if exc.code not in cfg.retry_on_status:
                return last
        except Exception as exc:  # noqa: BLE001
            last = (-1, f"{type(exc).__name__}: {exc}")
            if -1 not in cfg.retry_on_status:
                return last

        if attempt < cfg.retries:
            delay = min(cfg.retry_backoff_s * (2 ** attempt), cfg.retry_backoff_cap_s)
            _time.sleep(delay)
    return last


def make_handler(cfg: ProxyConfig):
    """构造请求处理器。闭包捕获 cfg，避免全局状态。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # 静音默认 stderr 访问日志；审计走 cfg.audit
            return

        def _send(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_sse(self, events: list[tuple[str, dict]]) -> None:
            """写 SSE 流。

            协议细节（错一个 codex 就不认）：
            - Content-Type: text/event-stream
            - 每个事件用 `event: <name>\\ndata: <json>\\n\\n`
            - 末尾不写 [DONE]（Responses API 用 response.completed 收尾，
              不是 Chat Completions 的 [DONE]）
            - 用 chunked 之外的方式：显式 Connection: close，避免客户端等长度
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for name, data in events:
                chunk = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                try:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return  # 客户端断开，正常退出

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {
                    "object": "list",
                    "data": [{"id": cfg.model or "proxy", "object": "model"}],
                })
                return
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                self._send(400, {"error": {"message": "invalid json"}})
                return

            path = self.path.rstrip("/")
            if not path.endswith("/responses"):
                self._send(404, {"error": {"message": f"proxy only serves /responses, got {path}"}})
                return

            if cfg.model:
                body["model"] = cfg.model
            chat_payload = responses_to_chat(body)

            status, text = _upstream_post(cfg, "/chat/completions", chat_payload)
            cfg.audit.append({
                "status": status,
                "model": chat_payload.get("model"),
                "n_messages": len(chat_payload.get("messages") or []),
                "bytes": len(text),
            })
            if status != 200:
                self._send(status if status > 0 else 502, {
                    "error": {
                        "message": f"upstream returned {status}",
                        "type": "upstream_error",
                        "detail": text[:500],
                    }
                })
                return
            try:
                chat_resp = json.loads(text)
            except json.JSONDecodeError:
                self._send(502, {"error": {"message": "upstream returned non-json"}})
                return

            # codex 以流式方式消费；请求里通常带 stream: true。
            # 即便没带，也默认走 SSE —— 非流式响应会被 codex 判为
            # "stream closed before response.completed"（实测）。
            wants_stream = body.get("stream", True) is not False
            if wants_stream:
                self._send_sse(responses_sse_events(chat_resp, cfg.model))
            else:
                self._send(200, chat_to_responses(chat_resp, cfg.model))

    return Handler


class CompatProxy:
    """可编程启停的代理服务（给测试与自动化用）。"""

    def __init__(self, cfg: ProxyConfig):
        self.cfg = cfg
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._actual_port: int = cfg.listen_port

    @property
    def url(self) -> str:
        port = self._actual_port or self.cfg.listen_port
        return f"http://{self.cfg.listen_host}:{port}"

    @property
    def port(self) -> int:
        return self._actual_port

    def start(self) -> str:
        """启动代理。端口为 0 时由系统分配，避免 Windows 端口保留导致的 10013。

        bind 放在 try 里而不是让异常穿透：编排层需要拿到清晰的失败原因
        （Windows 上 10013 通常意味着端口落在 excluded port range 里）。
        """
        handler = make_handler(self.cfg)
        self._server = ThreadingHTTPServer(
            (self.cfg.listen_host, self.cfg.listen_port), handler,
        )
        self._actual_port = self._server.server_address[1]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._server = None
        self._thread = None

    def __enter__(self) -> CompatProxy:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
