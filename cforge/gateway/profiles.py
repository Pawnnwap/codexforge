"""Provider 配置：书生（生产）与 OpenRouter（开发测试）走同一套抽象。

设计约束（来自实测定论，不是假设）：
1. OpenRouter 的 `:free` 模型走**上游共享池**，实测持续返回 429
   （`limit_source: upstream_provider_shared_pool`，provider=Phala）。
   → 因此 provider 层必须内建重试/退避/降级链，不能假设单次调用必成。
2. 书生 API 与 OpenRouter 都是 OpenAI 兼容的 `/chat/completions`，
   差异只在 base_url / api_key / model id。
   → 用 Profile 描述，不写 if-else 分支。

替换模型只改配置，不动代码——这是「暂用 Xing 29B 代替书生」的实现方式。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class Backend(str, Enum):
    """LLM 服务商标识。"""

    INTERN = "intern"        # 上海人工智能实验室书生（赛道强制）
    OPENROUTER = "openrouter"  # 开发期替代品


@dataclass(frozen=True)
class ModelProfile:
    """一次可用的模型接入点。"""

    backend: Backend
    model_id: str
    base_url: str
    api_key_env: str
    max_context: int
    supports_tools: bool = True
    supports_vision: bool = False
    note: str = ""

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()


# --------------------------------------------------------------------------- #
# 书生（生产目标）
# --------------------------------------------------------------------------- #
# base_url 实测结论（2026-10-25，key 实测）：
#   chat.intern-ai.org.cn/api/v1      -> 200 正确的参赛调用入口（官方文档指定）
#   internlm.intern-ai.org.cn/api/v1  -> 401 C1114 "API token scope forbidden"（令牌无此域权限）
#   api.intern-ai.org.cn/v1           -> 403 RBAC，内部网关，不要用
# 模型清单实测（/models 返回 11 个，probe/result.json 落盘）：
#   intern-s2-preview-397b (Intern-S2-Preview-397B)  <- 赛题 describe 点名的型号
#   intern-s2-preview      (Intern-S2-Preview-35B)
#   intern-s2 / intern-latest (Intern-S2 最新)
#   intern-s1-pro / intern-s1 / intern-s1-mini / internvl3.5-* (多模态)
# tools/function-calling 实测：intern-s2-preview-397b 返回规范 tool_calls（finish=tool_calls）。
INTERN_PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile(
        backend=Backend.INTERN,
        model_id="intern-s2-preview-397b",
        base_url="https://chat.intern-ai.org.cn/api/v1",
        api_key_env="INTERN_API_KEY",
        max_context=262_144,
        supports_vision=False,
        note="赛题 describe 点名 Intern-S2-397B；/models 实测 ref=Intern-S2-Preview-397B；"
             "tools 实测通过；旗舰，推理质量优先时用它",
    ),
    ModelProfile(
        backend=Backend.INTERN,
        model_id="intern-s2-preview",
        base_url="https://chat.intern-ai.org.cn/api/v1",
        api_key_env="INTERN_API_KEY",
        max_context=262_144,
        supports_vision=False,
        note="Intern-S2-Preview-35B；降级备选 1（延迟更低）",
    ),
    ModelProfile(
        backend=Backend.INTERN,
        model_id="intern-s2",
        base_url="https://chat.intern-ai.org.cn/api/v1",
        api_key_env="INTERN_API_KEY",
        max_context=262_144,
        supports_vision=False,
        note="Intern-S2 最新稳定版；降级备选 2",
    ),
    ModelProfile(
        backend=Backend.INTERN,
        model_id="intern-s1-pro",
        base_url="https://chat.intern-ai.org.cn/api/v1",
        api_key_env="INTERN_API_KEY",
        max_context=262_144,
        supports_vision=True,
        note="S1 Pro，多模态科学数据解析可用；降级备选 3",
    ),
)

# --------------------------------------------------------------------------- #
# OpenRouter（开发期替代）
# --------------------------------------------------------------------------- #
# 实测：xingchen-agi/xing4.0-29b-a4b:free，ctx=262144，价格 0/0。
# 但上游 Phala 共享池持续 429，所以降级链里排了多个备用。
OPENROUTER_PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile(
        backend=Backend.OPENROUTER,
        model_id="xingchen-agi/xing4.0-29b-a4b:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        max_context=262_144,
        note="用户指定的替代模型（Xing 4.0 29B A4B，免费）",
    ),
    ModelProfile(
        backend=Backend.OPENROUTER,
        model_id="google/gemma-4-31b-it:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        max_context=262_144,
        note="降级备选 1",
    ),
    ModelProfile(
        backend=Backend.OPENROUTER,
        model_id="nvidia/nemotron-3-super-120b-a12b:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        max_context=262_144,
        note="降级备选 2",
    ),
)


@dataclass
class GatewayConfig:
    """网关运行参数。

    退避策略针对「上游共享池 429」这一实测量身定做：
    这类限流通常几十秒到几分钟，指数退避 + 换模型比死等有效。
    """

    profiles: list[ModelProfile] = field(default_factory=list)
    # 单次请求超时（秒）
    request_timeout_s: int = 120
    # 单次请求的重试次数（同一模型）
    retries_per_model: int = 2
    # 退避基数（秒），实际等待 = base * 2**attempt，上限 backoff_cap_s
    backoff_base_s: float = 3.0
    backoff_cap_s: float = 30.0
    # 遇到这些 HTTP 状态码时换下一个模型而不是继续重试本模型
    switch_on_status: tuple[int, ...] = (402, 403, 404, 429, 502, 503)
    # base_url 覆盖（默认 None = 用 profile 自带的官方地址）。
    # 存在的理由（设计取舍）：书生 key 到位前，tests/e2e 用假上游演练
    # 完整链路，做到「key 一到 1 小时内出端到端证据」而不是现场调试。
    # 环境变量 CFORGE_BASE_URL_OVERRIDE 可设置；生产不留。
    base_url_override: str | None = None

    def effective_base_url(self, profile: ModelProfile) -> str:
        """该 profile 实际应使用的 base_url。"""
        return (self.base_url_override or profile.base_url).rstrip("/")

    @classmethod
    def for_backend(cls, backend: Backend | str) -> GatewayConfig:
        """按后端取默认配置。env 变量 `CFORGE_BACKEND` 可覆盖。"""
        if isinstance(backend, str):
            backend = Backend(backend)
        if backend is Backend.INTERN:
            return cls(profiles=list(INTERN_PROFILES))
        return cls(profiles=list(OPENROUTER_PROFILES))

    @classmethod
    def from_env(cls) -> GatewayConfig:
        """开发期默认 OpenRouter，答辩/生产切 INTERN，只改一个环境变量。"""
        raw = os.environ.get("CFORGE_BACKEND", "openrouter").strip().lower()
        try:
            backend = Backend(raw)
        except ValueError:
            backend = Backend.OPENROUTER
        cfg = cls.for_backend(backend)
        # 允许用环境变量覆盖模型，便于快速试验
        override = os.environ.get("CFORGE_MODEL", "").strip()
        if override and cfg.profiles:
            first = cfg.profiles[0]
            cfg.profiles[0] = ModelProfile(
                backend=first.backend,
                model_id=override,
                base_url=first.base_url,
                api_key_env=first.api_key_env,
                max_context=first.max_context,
                supports_tools=first.supports_tools,
                supports_vision=first.supports_vision,
                note=f"env override (原 {first.model_id})",
            )
        # base_url 覆盖（e2e 假上游演练用；生产环境不设置）
        url_override = os.environ.get("CFORGE_BASE_URL_OVERRIDE", "").strip()
        if url_override:
            cfg.base_url_override = url_override
        return cfg
