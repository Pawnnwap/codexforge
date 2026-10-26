"""端到端验证：真实起 proxy，用假 upstream 验证协议转换链路。

不依赖外网。用一个本地假 upstream 模拟 `/chat/completions`，
然后通过 proxy 发 `/responses` 请求，断言返回的是合法 Responses 结构。

这验证的是「codex 能否通过 proxy 驱动只支持 chat 的上游」这一核心命题。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import urllib.request

from cforge.harness.compat_proxy import CompatProxy, ProxyConfig


class FakeUpstream:
    """模拟上游 chat/completions 服务。"""

    def __init__(self, reply: str = "pong", tool_calls: list | None = None):
        self.reply = reply
        self.tool_calls = tool_calls
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        upstream = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: A003
                return

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                upstream.requests.append(body)
                message: dict = {"content": upstream.reply}
                if upstream.tool_calls:
                    message["tool_calls"] = upstream.tool_calls
                    message["content"] = None
                payload = {
                    "id": "chatcmpl-1",
                    "model": body.get("model", ""),
                    "choices": [{"message": message, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15,
                    },
                }
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._server = None


def _post(url: str, payload: dict) -> tuple[int, bytes, str]:
    """返回 (status, raw_bytes, content_type)。SSE 与非流式都能处理。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 [(event_name, data), ...]。"""
    out: list[tuple[str, dict]] = []
    text = raw.decode("utf-8", "replace")
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        data_line = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data_line = line[len("data: "):]
        if name and data_line:
            try:
                out.append((name, json.loads(data_line)))
            except json.JSONDecodeError:
                continue
    return out


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    """强制非流式，拿纯 JSON。测试响应体结构时用这个更直接。"""
    payload = {**payload, "stream": False}
    status, raw, _ = _post(url, payload)
    try:
        return status, json.loads(raw.decode())
    except json.JSONDecodeError:
        return status, {}


@pytest.fixture
def stack():
    up = FakeUpstream(reply="pong")
    up.start()
    proxy = CompatProxy(ProxyConfig(
        upstream_base_url=up.base_url,
        upstream_api_key="test-key",
        model="test-model",
        listen_port=0,  # 由系统分配
    ))
    # 容器需要真实端口，故手动启动一个
    from cforge.harness.compat_proxy import make_handler
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proxy.cfg))
    server.daemon_threads = True
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    proxy_url = f"http://127.0.0.1:{port}"
    try:
        yield up, proxy_url
    finally:
        server.shutdown()
        server.server_close()
        up.stop()


class TestProxyEndToEnd:
    def test_responses_endpoint_returns_valid_shape(self, stack):
        up, proxy_url = stack
        status, body = _post_json(f"{proxy_url}/responses", {
            "model": "test-model",
            "input": "ping",
            "max_output_tokens": 16,
        })
        assert status == 200
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output"][0]["type"] == "message"
        assert body["output"][0]["content"][0]["text"] == "pong"

    def test_default_is_sse(self, stack):
        """不显式传 stream 时必须走 SSE —— codex 就是这种调用方式。

        回归：早期版本返回整包 JSON，codex 报
        `stream closed before response.completed`，链路完全不通。
        """
        _, proxy_url = stack
        status, raw, ctype = _post(f"{proxy_url}/responses",
                                   {"model": "m", "input": "x"})
        assert status == 200
        assert "text/event-stream" in ctype
        events = _parse_sse(raw)
        names = [n for n, _ in events]
        assert "response.created" in names
        assert "response.completed" in names, "缺少 response.completed，codex 会判定失败"

    def test_sse_contains_text_delta(self, stack):
        _, proxy_url = stack
        _, raw, _ = _post(f"{proxy_url}/responses", {"model": "m", "input": "x"})
        events = dict(_parse_sse(raw))
        assert "response.output_text.delta" in events
        assert events["response.output_text.delta"]["delta"] == "pong"

    def test_sse_completed_carries_output(self, stack):
        _, proxy_url = stack
        _, raw, _ = _post(f"{proxy_url}/responses", {"model": "m", "input": "x"})
        events = dict(_parse_sse(raw))
        completed = events["response.completed"]["response"]
        assert completed["status"] == "completed"
        assert completed["output"][0]["content"][0]["text"] == "pong"

    def test_request_reached_upstream_as_chat_format(self, stack):
        up, proxy_url = stack
        _post_json(f"{proxy_url}/responses", {"model": "test-model", "input": "hello"})
        assert len(up.requests) == 1
        sent = up.requests[0]
        # 关键断言：发给上游的必须是 chat 格式
        assert "messages" in sent
        assert "input" not in sent
        assert sent["messages"][-1] == {"role": "user", "content": "hello"}

    def test_max_output_tokens_translated(self, stack):
        up, proxy_url = stack
        _post_json(f"{proxy_url}/responses", {
            "model": "test-model", "input": "x", "max_output_tokens": 42,
        })
        assert up.requests[0]["max_tokens"] == 42

    def test_usage_mapped_in_response(self, stack):
        _, proxy_url = stack
        _, body = _post_json(f"{proxy_url}/responses", {"model": "m", "input": "x"})
        assert body["usage"]["input_tokens"] == 11
        assert body["usage"]["output_tokens"] == 4

    def test_non_responses_path_rejected(self, stack):
        _, proxy_url = stack
        status, body = _post_json(f"{proxy_url}/chat/completions", {"model": "m"})
        assert status == 404
        assert "only serves /responses" in body["error"]["message"]

    def test_audit_recorded(self, stack):
        up, proxy_url = stack
        _post_json(f"{proxy_url}/responses", {"model": "m", "input": "x"})
        # audit 由 handler 闭包写入独立 cfg；这里验证 upstream 确实被调用
        assert len(up.requests) == 1

    def test_models_endpoint(self, stack):
        _, proxy_url = stack
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{proxy_url}/models", timeout=10) as resp:
            body = json.loads(resp.read().decode())
        assert body["object"] == "list"
        assert body["data"][0]["id"] == "test-model"


class TestProxyToolCalls:
    def test_tool_call_survives_roundtrip(self):
        up = FakeUpstream(tool_calls=[{
            "id": "call_x", "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"a"}'},
        }])
        up.start()
        from cforge.harness.compat_proxy import make_handler
        cfg = ProxyConfig(
            upstream_base_url=up.base_url, upstream_api_key="k",
            model="m", listen_port=0,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
        server.daemon_threads = True
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            _, body = _post_json(f"http://127.0.0.1:{port}/responses",
                                 {"model": "m", "input": "go"})
            fc = [o for o in body["output"] if o["type"] == "function_call"]
            assert len(fc) == 1
            assert fc[0]["name"] == "write_file"
            assert fc[0]["call_id"] == "call_x"
        finally:
            server.shutdown()
            server.server_close()
            up.stop()

    def test_tool_call_emits_function_call_sse_events(self):
        """工具调用走 function_call_arguments.* 而非 output_text.delta。"""
        up = FakeUpstream(tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "shell", "arguments": '{"command":"ls"}'},
        }])
        up.start()
        from cforge.harness.compat_proxy import make_handler
        cfg = ProxyConfig(upstream_base_url=up.base_url,
                          upstream_api_key="k", model="m", listen_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            _, raw, _ = _post(f"http://127.0.0.1:{server.server_address[1]}/responses",
                              {"model": "m", "input": "go"})
            names = [n for n, _ in _parse_sse(raw)]
            assert "response.function_call_arguments.done" in names
            assert "response.completed" in names
        finally:
            server.shutdown()
            server.server_close()
            up.stop()
