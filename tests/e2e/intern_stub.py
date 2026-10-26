"""书生 API 假上游 —— 让「书生路径」在 key 到位前可完整演练。

存在理由（R1-S1 的设计）：书生 key 需要用户手机注册 + 提额审核，
有审批延迟。如果等 key 才第一次跑链路，现场调试会烧掉关键窗口。
这个 stub 把上游换成本地假服务，让 codex → compat_proxy → 上游
整条链提前演练到「只差换 URL」。

能力：
- `GET  /api/v1/models`            → 模型清单（含 intern-s2-preview）
- `POST /api/v1/chat/completions`  → 一次性文本 completion（非流式）
- 记录每个请求的路径与 Authorization 头，供断言「key 真的用上了」

端口一律 `listen_port=0` 由系统分配——Windows 固定端口常撞
WinError 10013（端口保留区），这是实测量身踩过的坑。

不带任何第三方依赖（stdlib http.server）。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class RecordedRequest:
    method: str
    path: str
    authorization: str
    body: dict = field(default_factory=dict)


class _Handler(BaseHTTPRequestHandler):
    """请求处理器。server 实例上挂着 stub 状态。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # 静音：stdio 协议流不杂

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        stub: InternStub = self.server.stub  # type: ignore[attr-defined]
        stub.record("GET", self.path, self.headers.get("Authorization", ""))
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "intern-s2-preview", "object": "model"},
                    {"id": "intern-s1-pro", "object": "model"},
                ],
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        stub: InternStub = self.server.stub  # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        stub.record("POST", self.path, self.headers.get("Authorization", ""), body)

        if self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(200, {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": body.get("model", "intern-s2-preview"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": stub.reply_text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40},
            })
            return
        self._send_json(404, {"error": "not found"})


class InternStub:
    """假上游服务。start() 后 base_url 可用。"""

    def __init__(self, reply_text: str = "CODEXFORGE-INTERN-OK"):
        self.reply_text = reply_text
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    def record(self, method: str, path: str, authorization: str,
               body: dict | None = None) -> None:
        with self._lock:
            self.requests.append(RecordedRequest(
                method=method, path=path, authorization=authorization,
                body=body or {},
            ))

    @property
    def base_url(self) -> str:
        assert self._server is not None, "stub 未启动"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/api/v1"

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.stub = self  # type: ignore[attr-defined]
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "InternStub":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
