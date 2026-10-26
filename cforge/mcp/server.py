"""MCP server 主循环：initialize → tools/list → tools/call → shutdown。

单线程顺序处理——stdio 传输本就是串行语义，并发只会引入竞态。
每请求记一行审计到 stderr（MCP 客户端不读 stderr，不会污染协议流）。

退出条件：stdin EOF 或收到 `shutdown` 请求。
不挂死——CI 里挂着的 server 会拖垮整条流水线（实测教训）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from cforge.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_INFO,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    read_message,
    write_message,
)
from cforge.mcp.tools import call_tool, tool_schemas


class MCPServer:
    """一个最小但完整的 MCP stdio server。"""

    def __init__(self, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
                 audit_path: Path | None = None):
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._audit_path = audit_path
        self._initialized = False
        self._running = False

    # ------------------------------------------------------------------ 审计
    def _audit(self, line: str) -> None:
        print(f"[mcp] {line}", file=sys.stderr)
        if self._audit_path:
            try:
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self._audit_path.open("a", encoding="utf-8") as f:
                    import time

                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
            except OSError:
                pass  # 审计失败不阻塞协议

    # ------------------------------------------------------------------ 分发
    def _dispatch(self, req: JsonRpcRequest) -> JsonRpcResponse | None:
        if req.is_notification:
            # notification：处理但不应答（如 initialized）
            if req.method == "initialized":
                self._audit("initialized")
            return None

        if req.method == "initialize":
            self._initialized = True
            self._audit(f"initialize from client")
            return JsonRpcResponse(id=req.id, result={
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
            })

        if req.method == "shutdown":
            self._running = False
            self._audit("shutdown")
            return JsonRpcResponse(id=req.id, result={})

        # ping 是 MCP 规范的**必备**方法（客户端保活与健康检查用）。
        # 它在 initialize 之前也必须可用——客户端常以 ping 探测进程是否活着，
        # 此时还没握手。所以本分支刻意不检查 self._initialized。
        if req.method == "ping":
            return JsonRpcResponse(id=req.id, result={})

        if req.method == "tools/list":
            if not self._initialized:
                return JsonRpcResponse(id=req.id, error=JsonRpcError(
                    INVALID_REQUEST, "server not initialized"))
            return JsonRpcResponse(id=req.id, result={"tools": tool_schemas()})

        if req.method == "tools/call":
            if not self._initialized:
                return JsonRpcResponse(id=req.id, error=JsonRpcError(
                    INVALID_REQUEST, "server not initialized"))
            name = req.params.get("name", "")
            args = req.params.get("arguments") or {}
            if not isinstance(name, str) or not name:
                return JsonRpcResponse(id=req.id, error=JsonRpcError(
                    INVALID_PARAMS, "tools/call requires string 'name'"))
            self._audit(f"tools/call {name}")
            return JsonRpcResponse(id=req.id, result=call_tool(name, args))

        return JsonRpcResponse(id=req.id, error=JsonRpcError(
            METHOD_NOT_FOUND, f"unknown method: {req.method}"))

    # ------------------------------------------------------------------ 主循环
    def serve(self) -> int:
        """跑到 EOF 或 shutdown。返回退出码（0 = 正常）。"""
        self._running = True
        self._audit(f"server start pid-protocol={PROTOCOL_VERSION}")
        while self._running:
            try:
                req = read_message(self._stdin)
            except ValueError as e:
                # 解析错误：能答就答（有 id 才行），然后继续
                self._audit(f"parse error: {e}")
                write_message(self._stdout, JsonRpcResponse(
                    id=None, error=JsonRpcError(PARSE_ERROR, str(e))))
                continue
            if req is None:
                break  # EOF
            try:
                resp = self._dispatch(req)
            except Exception as e:  # noqa: BLE001 —— 分发层兜底，绝不让 server 崩
                self._audit(f"internal error: {type(e).__name__}: {e}")
                resp = JsonRpcResponse(
                    id=req.id, error=JsonRpcError(INTERNAL_ERROR, f"{type(e).__name__}: {e}"))
            if resp is not None:
                write_message(self._stdout, resp)
        self._audit("server stop")
        return 0


def serve_forever(*, audit_path: Path | None = None) -> int:
    return MCPServer(audit_path=audit_path).serve()
