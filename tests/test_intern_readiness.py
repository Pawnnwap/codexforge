"""书生就绪度单测：三态结论（ready / config-ready / not-configured）。

关键断言：**缺 key 时是 config-ready 而不是错误**——
这是「书生融合」叙事的降级策略在代码层的落点。
"""
from __future__ import annotations

import pytest

from cforge.gateway.intern_readiness import check_readiness


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("INTERN_API_KEY", raising=False)
    monkeypatch.delenv("CFORGE_BASE_URL_OVERRIDE", raising=False)


def _repo_root() -> object:
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


class TestNoKey:
    def test_config_ready_without_key(self, monkeypatch):
        """key 未到位 = config-ready（既定状态，不是错误）。"""
        monkeypatch.delenv("INTERN_API_KEY", raising=False)
        report = check_readiness(repo_root=_repo_root())
        assert report.overall == "config-ready"
        assert report.key_configured is False
        assert report.profile_ok is True

    def test_next_step_mentions_registration(self, monkeypatch):
        report = check_readiness(repo_root=_repo_root())
        assert "internlm.intern-ai.org.cn" in report.next_step
        assert "兴智杯" in report.next_step

    def test_e2e_script_detected(self):
        report = check_readiness(repo_root=_repo_root())
        assert report.e2e_script_exists is True


class TestWithKey:
    def test_ready_with_key(self, monkeypatch):
        monkeypatch.setenv("INTERN_API_KEY", "sk-test")
        report = check_readiness(repo_root=_repo_root())
        assert report.overall == "ready"
        assert report.key_configured is True
        assert "intern_probe" in report.next_step

    def test_override_recorded_not_hidden(self, monkeypatch):
        monkeypatch.setenv("INTERN_API_KEY", "sk-test")
        monkeypatch.setenv("CFORGE_BASE_URL_OVERRIDE", "http://127.0.0.1:9/api/v1")
        report = check_readiness(repo_root=_repo_root())
        assert report.base_url_override.endswith("/api/v1")
        assert any(c["item"] == "base_url 覆盖" for c in report.checks)


class TestAsDict:
    def test_as_dict_serializable(self, monkeypatch):
        import json

        report = check_readiness(repo_root=_repo_root())
        payload = json.dumps(report.as_dict(), ensure_ascii=False)
        assert "config-ready" in payload
