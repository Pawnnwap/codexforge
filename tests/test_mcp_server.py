"""MCP 模块单测：协议成帧/解析/错误码、工具 handler、server 分发。

全部离线：stdio 用 StringIO 假流，工具 handler 用临时目录。
真实链路（codex → proxy → 上游）由 tests/e2e/intern_e2e.py 负责。
"""
from __future__ import annotations

import io
import json

import pytest

from cforge.mcp.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcRequest,
    read_message,
    write_message,
)
from cforge.mcp.server import MCPServer
from cforge.mcp.tools import TOOL_DEFS, call_tool, tool_schemas


# --------------------------------------------------------------------------- #
# 协议层
# --------------------------------------------------------------------------- #
class TestProtocol:
    def test_read_bare_json_line(self):
        stream = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        req = read_message(stream)
        assert req is not None
        assert req.method == "initialize"
        assert req.id == 1

    def test_read_content_length_framing(self):
        """LSP 风格成帧（部分客户端用）也必须能读——容忍度是实测出来的。"""
        body = '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
        stream = io.StringIO(f"Content-Length: {len(body)}\r\n\r\n{body}")
        req = read_message(stream)
        assert req is not None
        assert req.method == "tools/list"

    def test_read_eof_returns_none(self):
        assert read_message(io.StringIO("")) is None

    def test_parse_error_on_garbage(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            read_message(io.StringIO("not json\n"))

    def test_notification_has_no_id(self):
        stream = io.StringIO('{"jsonrpc":"2.0","method":"initialized"}\n')
        req = read_message(stream)
        assert req is not None
        assert req.is_notification

    def test_write_message_flushes_json(self):
        out = io.StringIO()
        resp_io = out
        from cforge.mcp.protocol import JsonRpcResponse

        write_message(resp_io, JsonRpcResponse(id=1, result={"ok": True}))
        payload = json.loads(out.getvalue())
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == 1
        assert payload["result"] == {"ok": True}


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #
class TestTools:
    def test_tool_count_at_least_three(self):
        """PRD R2 验收线：≥3 个工具。"""
        assert len(TOOL_DEFS) >= 3

    def test_schemas_have_required_fields(self):
        for schema in tool_schemas():
            assert "name" in schema and "description" in schema
            assert "inputSchema" in schema
            assert "required" in schema["inputSchema"]

    def test_unknown_tool_returns_is_error(self):
        """未知工具转 isError 结果，不抛异常（不断链）。"""
        result = call_tool("no_such_tool", {})
        assert result["isError"] is True
        assert "未知工具" in result["content"][0]["text"]

    def test_missing_required_param_returns_is_error(self):
        result = call_tool("quality_score", {})
        assert result["isError"] is True
        assert "缺少必需参数" in result["content"][0]["text"]

    def test_quality_score_on_real_workspace(self, tmp_path):
        """评分真源唯一：handler 必须给出与 quality.evaluate 一致的分数。"""
        from cforge.quality.metrics import evaluate

        src = tmp_path / "m.py"
        src.write_text("import os\n\n\ndef f():\n    return 1\n", encoding="utf-8")
        expected = evaluate(tmp_path).score
        result = call_tool("quality_score", {"path": str(tmp_path)})
        assert result.get("isError") is not True
        assert result["structuredContent"]["score"] == round(expected, 2)

    def test_quality_score_nonexistent_dir_is_error(self):
        result = call_tool("quality_score", {"path": "/no/such/dir/xyz"})
        assert result["isError"] is True

    def test_gate_status_gives_reason(self, tmp_path):
        """判定必须带人看理由——黑盒守护进程没人敢用。"""
        result = call_tool("gate_status", {"workspace": str(tmp_path)})
        payload = result["structuredContent"]
        assert "reason" in payload
        assert "gates" in payload
        assert "explain" in payload

    def test_guardian_report_no_report_state(self, tmp_path):
        """没跑过守门时返回明确状态 + 补救命令，不是错误。"""
        result = call_tool("guardian_report", {"workspace": str(tmp_path)})
        payload = result["structuredContent"]
        assert payload["status"] == "no-report"
        assert "cforge.cli guard" in payload["hint"]


# --------------------------------------------------------------------------- #
# server 分发
# --------------------------------------------------------------------------- #
def _drive(server: MCPServer, lines: list[str]) -> list[dict]:
    """喂一串请求，收回所有响应。"""
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    server._stdin = stdin
    server._stdout = stdout
    server.serve()
    return [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]


class TestServer:
    def test_initialize_then_tools_list(self):
        srv = MCPServer()
        responses = _drive(srv, [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}),
        ])
        assert responses[0]["result"]["protocolVersion"]
        assert len(responses[0]["result"]["serverInfo"]["name"]) > 0
        assert len(responses[1]["result"]["tools"]) >= 3
        assert responses[2]["result"] == {}

    def test_tools_list_before_initialize_rejected(self):
        srv = MCPServer()
        responses = _drive(srv, [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}),
        ])
        assert responses[0]["error"]["code"] == INVALID_PARAMS or "error" in responses[0]

    def test_unknown_method_returns_method_not_found(self):
        srv = MCPServer()
        responses = _drive(srv, [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "bogus/method"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}),
        ])
        assert responses[1]["error"]["code"] == METHOD_NOT_FOUND

    def test_tools_call_end_to_end(self, tmp_path):
        (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        srv = MCPServer()
        responses = _drive(srv, [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "quality_score",
                                   "arguments": {"path": str(tmp_path)}}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}),
        ])
        result = responses[1]["result"]
        assert result.get("isError") is not True
        payload = json.loads(result["content"][0]["text"])
        assert 0 <= payload["score"] <= 100

    def test_notification_gets_no_response(self):
        srv = MCPServer()
        responses = _drive(srv, [
            json.dumps({"jsonrpc": "2.0", "method": "initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}),
        ])
        # 只有 initialize 与 shutdown 有响应；initialized 是 notification
        assert len(responses) == 2

    def test_parse_error_does_not_kill_server(self):
        """坏 JSON 后 server 必须还能继续服务（解析错误不应断链）。"""
        srv = MCPServer()
        stdin = io.StringIO("garbage\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}) + "\n")
        stdout = io.StringIO()
        srv._stdin = stdin
        srv._stdout = stdout
        srv.serve()
        lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
        assert lines[0]["error"]["code"] == PARSE_ERROR
        assert lines[1]["result"]["protocolVersion"]

    def test_ping_supported_before_initialize(self):
        """ping 是 MCP 规范必备方法，且握手前就要能用。

        客户端常以 ping 探测进程是否活着，那时还没 initialize。
        早期实现漏了 ping，返回 -32601；本机自测（自己发自己收）
        发现不了，是外部视角的 mcp_client_demo.py 核对出来的。
        """
        srv = MCPServer()
        responses = _drive(srv, [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}),
        ])
        assert responses[0]["result"] == {}
        assert "error" not in responses[0]

    def test_internal_error_code_is_standard(self):
        """内部错误必须是 -32603（规范值），不是保留区间起点 -32000。"""
        from cforge.mcp.protocol import INTERNAL_ERROR

        assert INTERNAL_ERROR == -32603
