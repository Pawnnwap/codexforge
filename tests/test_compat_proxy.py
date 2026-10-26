"""协议转换单测：Responses ↔ Chat。

这是整个架构最容易出错、也最难人工检查的部分。
转换错了 codex 会直接报协议违约，且错误信息难以定位，
所以每条映射都单独断言。
"""
from __future__ import annotations

import json

import pytest

from cforge.harness.compat_proxy import chat_to_responses, responses_to_chat


class TestResponsesToChat:
    def test_string_input_becomes_user_message(self):
        out = responses_to_chat({"model": "m", "input": "hello"})
        assert out["messages"] == [{"role": "user", "content": "hello"}]
        assert out["model"] == "m"

    def test_instructions_become_system_message(self):
        out = responses_to_chat({
            "model": "m", "instructions": "be terse", "input": "hi",
        })
        assert out["messages"][0] == {"role": "system", "content": "be terse"}
        assert out["messages"][1] == {"role": "user", "content": "hi"}

    def test_input_array_with_roles(self):
        out = responses_to_chat({
            "model": "m",
            "input": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ],
        })
        assert [m["role"] for m in out["messages"]] == ["user", "assistant"]

    def test_content_parts_are_flattened(self):
        out = responses_to_chat({
            "model": "m",
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "line1"},
                    {"type": "input_text", "text": "line2"},
                ],
            }],
        })
        assert out["messages"][0]["content"] == "line1\nline2"

    def test_function_call_output_becomes_tool_message(self):
        out = responses_to_chat({
            "model": "m",
            "input": [
                {"type": "function_call_output", "call_id": "c1", "output": "42"},
            ],
        })
        assert out["messages"][0] == {
            "role": "tool", "tool_call_id": "c1", "content": "42",
        }

    def test_function_call_becomes_assistant_tool_calls(self):
        out = responses_to_chat({
            "model": "m",
            "input": [{
                "type": "function_call", "call_id": "c9",
                "name": "read_file", "arguments": '{"path":"a.txt"}',
            }],
        })
        msg = out["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["tool_calls"][0]["id"] == "c9"
        assert msg["tool_calls"][0]["function"]["name"] == "read_file"

    def test_max_output_tokens_maps_to_max_tokens(self):
        out = responses_to_chat({"model": "m", "input": "x", "max_output_tokens": 99})
        assert out["max_tokens"] == 99

    def test_flat_function_tool_is_wrapped(self):
        out = responses_to_chat({
            "model": "m", "input": "x",
            "tools": [{
                "type": "function", "name": "shell",
                "description": "run", "parameters": {"type": "object"},
            }],
        })
        tool = out["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "shell"

    def test_already_chat_shaped_tool_passes_through(self):
        chat_tool = {
            "type": "function",
            "function": {"name": "f", "description": "", "parameters": {}},
        }
        out = responses_to_chat({"model": "m", "input": "x", "tools": [chat_tool]})
        assert out["tools"][0] == chat_tool

    def test_empty_input_still_produces_valid_shape(self):
        out = responses_to_chat({"model": "m"})
        assert isinstance(out["messages"], list)


class TestChatToResponses:
    def test_plain_text_becomes_message_output(self):
        chat = {
            "id": "gen-1", "model": "m",
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
        out = chat_to_responses(chat)
        assert out["object"] == "response"
        assert out["status"] == "completed"
        assert out["output"][0]["type"] == "message"
        assert out["output"][0]["content"][0]["text"] == "hello"

    def test_usage_field_names_are_mapped(self):
        chat = {
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        }
        out = chat_to_responses(chat)
        assert out["usage"] == {
            "input_tokens": 7, "output_tokens": 2, "total_tokens": 9,
        }

    def test_tool_calls_become_function_call_items(self):
        chat = {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": "write_file", "arguments": '{"p":"a"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        out = chat_to_responses(chat)
        kinds = [o["type"] for o in out["output"]]
        assert "function_call" in kinds
        fc = next(o for o in out["output"] if o["type"] == "function_call")
        assert fc["name"] == "write_file"
        assert fc["call_id"] == "call_1"

    def test_empty_response_still_valid(self):
        """空响应必须给合法 message，否则 codex 判协议违约。"""
        out = chat_to_responses({"choices": [{"message": {}}]})
        assert out["output"][0]["type"] == "message"
        assert out["output"][0]["content"][0]["text"] == ""

    def test_truncated_finish_reports_incomplete(self):
        chat = {
            "choices": [{"message": {"content": "cut"}, "finish_reason": "length"}],
        }
        out = chat_to_responses(chat)
        assert out["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_normal_finish_has_null_incomplete(self):
        chat = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
        assert chat_to_responses(chat)["incomplete_details"] is None

    def test_all_required_fields_present(self):
        """codex 缺字段会直接报协议错误，逐字段断言。"""
        out = chat_to_responses({"choices": [{"message": {"content": "x"}}]})
        for key in ("id", "object", "created_at", "model", "status",
                    "output", "usage", "parallel_tool_calls", "incomplete_details"):
            assert key in out, f"缺少必填字段 {key}"


class TestRoundTrip:
    def test_json_serializable(self):
        chat = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        out = chat_to_responses(chat)
        # 必须能序列化 —— 代理要靠它写回 HTTP 响应
        assert json.loads(json.dumps(out)) == out
