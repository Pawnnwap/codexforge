"""mcp 包：把 CodexForge 的确定性质量评估能力以 MCP 协议暴露。

为什么自研而不用官方 `mcp` Python SDK（设计取舍，写进文档）：
1. 本项目运行时零第三方依赖（评审复现不装包、避开依赖树 copyleft）；
   SDK 自带 pydantic/anyio 传递依赖，与架构决策冲突。
2. MCP 的 stdio 传输本质是「换行分隔的 JSON-RPC 2.0」，协议面很窄
   （initialize / tools-list / tools-call 三个方法），标准库完全够。
3. 被否方案：HTTP/SSE 传输。拒绝理由：本地工具用 stdio 是 MCP 的
   标准形态，且 HTTP 会引入端口管理（Windows 10013 实测教训）。

协议版本：实现 MCP 规范 2025-06-18 的 stdio 传输子集。
"""
from cforge.mcp.protocol import (
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    read_message,
    write_message,
)
from cforge.mcp.server import MCPServer
from cforge.mcp.tools import TOOL_DEFS, call_tool, tool_schemas

__all__ = [
    "JsonRpcError", "JsonRpcRequest", "JsonRpcResponse",
    "read_message", "write_message",
    "MCPServer", "TOOL_DEFS", "call_tool", "tool_schemas",
]
