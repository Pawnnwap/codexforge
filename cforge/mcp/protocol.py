"""MCP 协议层：JSON-RPC 2.0 over stdio 的成帧、解析与错误码。

成帧容忍度（实测出来的，不是规范抄的）：
不同 MCP 客户端有两种成帧习惯——
  a) `Content-Length: N\\r\\n\\r\\n{json}`（LSP 风格，部分客户端用）
  b) 裸 JSON + 换行（stdio server 最常见）
两种都要能读，否则「协议正确但客户端连不上」，极难排查。

错误码用规范标准值（PARSE_ERROR / INVALID_REQUEST / METHOD_NOT_FOUND /
INVALID_PARAMS / INTERNAL_ERROR）。工具内异常**不抛给传输层**——
转成 `isError: true` 的工具结果。这是 MCP 客户端的行为要求；
super-opencode 时代的教训是「工具内 raise，客户端只收到笼统的
Error executing tool」，原因全丢。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TextIO

# JSON-RPC 2.0 标准错误码（规范原文 -32700/-32600/-32601/-32602/-32603）
#
# 修正记录：此处原为 -32000（即 -32000 是 "Server error" 区间的起始值，
# 属**保留区间**的起点而非规范指定的 internal error 码）。规范明写
# "Internal error: -32603"。这个错不影响本机自测（我们自己发自己收），
# 但会把协议层不一致暴露给真实第三方客户端——由 mcp_client_demo.py
# 的外部视角核对才查出来，自测永远发现不了。故修正为标准值。
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# 本 server 实现的 MCP 协议版本（规范 2025-06-18 的 stdio 子集）
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "codexforge-quality", "version": "0.3.0"}


@dataclass
class JsonRpcRequest:
    """一条解析后的请求。`id` 为 None 表示 notification（不应答）。"""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: Any = None

    @property
    def is_notification(self) -> bool:
        return self.id is None


@dataclass
class JsonRpcError:
    code: int
    message: str
    data: Any = None

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class JsonRpcResponse:
    id: Any
    result: Any = None
    error: JsonRpcError | None = None

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            out["error"] = self.error.as_dict()
        else:
            out["result"] = self.result
        return out


def read_message(stream: TextIO) -> JsonRpcRequest | None:
    """从流里读一条消息。

    返回 None = 流结束（EOF）。解析失败抛 ValueError（由调用方转
    PARSE_ERROR 响应——notification 解析失败时不应答，规范要求）。
    """
    line = stream.readline()
    if not line:
        return None

    # 形态 a：Content-Length 头（可能有多行头）
    if line.lower().startswith("content-length:"):
        headers: dict[str, str] = {}
        while line and line.strip():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            line = stream.readline()
        try:
            n = int(headers.get("content-length", "0"))
        except ValueError:
            raise ValueError("bad Content-Length header") from None
        if n <= 0:
            raise ValueError("Content-Length must be positive")
        body = stream.read(n)
        if not body:
            return None
        return _parse(body)

    # 形态 b：裸 JSON 行
    stripped = line.strip()
    if not stripped:
        # 空行：可能是心跳，递归读下一条（深度由调用方的循环控制）
        return read_message(stream)
    return _parse(stripped)


def _parse(body: str) -> JsonRpcRequest:
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise ValueError("request must be a JSON object")
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError("missing method")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return JsonRpcRequest(method=method, params=params, id=raw.get("id"))


def write_message(stream: TextIO, response: JsonRpcResponse) -> None:
    """写一条响应。stdin/stdout 都是文本流，flush 是必须的——
    不 flush 客户端会认为 server 挂死（实测排查过类似问题）。"""
    stream.write(json.dumps(response.as_dict(), ensure_ascii=False) + "\n")
    stream.flush()
