"""网关单测：错误分类、降级链、无 key 短路。

不联网。降级链用假 upstream 验证结构正确性。
真实连通性由 `cforge probe` 负责，那个会实际花钱。
"""
from __future__ import annotations

import json

import pytest

from cforge.gateway import client as gc
from cforge.gateway.client import LLMGateway, Usage
from cforge.gateway.profiles import Backend, GatewayConfig, ModelProfile


def _profile(model: str, env: str, backend: Backend = Backend.OPENROUTER) -> ModelProfile:
    return ModelProfile(
        backend=backend, model_id=model, base_url="http://127.0.0.1:9/v1",
        api_key_env=env, max_context=1000,
    )


class TestClassify:
    def test_ok(self):
        assert gc._classify(200, "{}")[0] == "ok"

    def test_bad_key_recognizes_a0211(self):
        """书生特有的错误码，必须单独识别，否则会误报成普通 401。"""
        kind, detail = gc._classify(401, '{"msgCode":"A0211","msg":"user token expired"}')
        assert kind == "bad_key"
        assert "A0211" in detail

    def test_plain_401_is_unauthorized(self):
        assert gc._classify(401, "nope")[0] == "unauthorized"

    def test_shared_pool_429_is_distinguished(self):
        """OpenRouter 免费池的限流必须与普通 429 区分 —— 两者应对策略不同。"""
        body = json.dumps({"error": {"metadata": {
            "limit_source": "upstream_provider_shared_pool"}}})
        kind, _ = gc._classify(429, body)
        assert kind == "rate_limited_shared_pool"

    def test_plain_429(self):
        assert gc._classify(429, "{}")[0] == "rate_limited"

    def test_network_error(self):
        assert gc._classify(-1, "boom")[0] == "network_error"

    def test_5xx(self):
        assert gc._classify(503, "")[0] == "upstream_error"

    def test_402_quota(self):
        assert gc._classify(402, "")[0] == "insufficient_quota"


class TestUsage:
    def test_add_accumulates(self):
        a = Usage(1, 2, 3)
        a.add(Usage(10, 20, 30))
        assert (a.prompt_tokens, a.completion_tokens, a.total_tokens) == (11, 22, 33)


class TestGatewayNoKey:
    def test_missing_key_reports_per_profile_and_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("CFORGE_TEST_KEY_A", raising=False)
        monkeypatch.delenv("CFORGE_TEST_KEY_B", raising=False)
        cfg = GatewayConfig(
            profiles=[_profile("m1", "CFORGE_TEST_KEY_A"), _profile("m2", "CFORGE_TEST_KEY_B")],
            retries_per_model=0,
        )
        gw = LLMGateway(cfg)
        res = gw.chat([{"role": "user", "content": "hi"}])
        assert res.ok is False
        assert res.error_kind == "missing_api_key"
        # 两个 profile 都应该被记录到 attempts
        kinds = [a["kind"] for a in res.attempts]
        assert kinds.count("missing_api_key") == 2

    def test_empty_profiles_returns_structured_error(self):
        gw = LLMGateway(GatewayConfig(profiles=[]))
        res = gw.chat([{"role": "user", "content": "hi"}])
        assert res.ok is False
        assert res.error_kind == "no_profile"


class TestGatewayNetworkFail:
    def test_unreachable_upstream_is_classified(self, monkeypatch):
        monkeypatch.setenv("CFORGE_TEST_KEY", "k")
        cfg = GatewayConfig(
            profiles=[_profile("m1", "CFORGE_TEST_KEY")],
            retries_per_model=0,
            request_timeout_s=2,
            backoff_base_s=0,
            backoff_cap_s=0,
        )
        gw = LLMGateway(cfg)
        res = gw.chat([{"role": "user", "content": "hi"}])
        assert res.ok is False
        # 可能是 network_error，也可能是 switch_on_status 之外的其它分类
        assert res.error_kind in ("network_error", "upstream_error", "http_-1")


class TestGatewayParse:
    def test_valid_chat_response_parsed(self):
        body = json.dumps({
            "model": "m",
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })
        gw = LLMGateway(GatewayConfig(profiles=[]))
        parsed = gw._parse(body)
        assert parsed is not None
        assert parsed.content == "hello"
        assert parsed.usage.total_tokens == 7

    def test_malformed_returns_none(self):
        gw = LLMGateway(GatewayConfig(profiles=[]))
        assert gw._parse("not json") is None

    def test_missing_usage_defaults_to_zero(self):
        gw = LLMGateway(GatewayConfig(profiles=[]))
        parsed = gw._parse(json.dumps({"choices": [{"message": {"content": "x"}}]}))
        assert parsed is not None
        assert parsed.usage.total_tokens == 0


class TestGatewayConfigFromEnv:
    def test_default_is_openrouter(self, monkeypatch):
        monkeypatch.delenv("CFORGE_BACKEND", raising=False)
        cfg = GatewayConfig.from_env()
        assert cfg.profiles[0].backend is Backend.OPENROUTER

    def test_intern_switch(self, monkeypatch):
        monkeypatch.setenv("CFORGE_BACKEND", "intern")
        cfg = GatewayConfig.from_env()
        assert cfg.profiles[0].backend is Backend.INTERN
        # 实测 2026-10-25：书生官方调用入口是 chat.intern-ai.org.cn/api/v1
        # （internlm.intern-ai.org.cn/api/v1 返回 401 C1114 token scope forbidden）
        assert "chat.intern-ai.org.cn" in cfg.profiles[0].base_url
        assert "internlm.intern-ai.org.cn" not in cfg.profiles[0].base_url

    def test_unknown_backend_falls_back(self, monkeypatch):
        monkeypatch.setenv("CFORGE_BACKEND", "nonsense")
        cfg = GatewayConfig.from_env()
        assert cfg.profiles[0].backend is Backend.OPENROUTER

    def test_model_override(self, monkeypatch):
        monkeypatch.setenv("CFORGE_BACKEND", "openrouter")
        monkeypatch.setenv("CFORGE_MODEL", "custom/model")
        cfg = GatewayConfig.from_env()
        assert cfg.profiles[0].model_id == "custom/model"

    def test_intern_uses_correct_base_url(self, monkeypatch):
        """实测 2026-10-25：chat.intern-ai.org.cn/api/v1 是唯一可用的书生调用入口；
        api.intern-ai.org.cn 是内部网关（403 RBAC），不能用；
        internlm.intern-ai.org.cn/api/v1 是门户（401 C1114 token scope forbidden），不能用。"""
        monkeypatch.setenv("CFORGE_BACKEND", "intern")
        cfg = GatewayConfig.from_env()
        for p in cfg.profiles:
            assert "chat.intern-ai.org.cn" in p.base_url
            assert "api.intern-ai.org.cn" not in p.base_url
            assert "internlm.intern-ai.org.cn" not in p.base_url


class TestProfilesIntegrity:
    def test_all_profiles_have_key_env(self):
        for cfg in (GatewayConfig.for_backend(Backend.INTERN),
                    GatewayConfig.for_backend(Backend.OPENROUTER)):
            for p in cfg.profiles:
                assert p.api_key_env

    def test_openrouter_first_choice_is_xing(self):
        cfg = GatewayConfig.for_backend(Backend.OPENROUTER)
        assert "xing" in cfg.profiles[0].model_id.lower()

    def test_fallback_chain_has_multiple(self):
        cfg = GatewayConfig.for_backend(Backend.OPENROUTER)
        assert len(cfg.profiles) >= 2
