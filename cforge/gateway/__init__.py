"""gateway 包：LLM 接入抽象。

对外只暴露三样东西：Backend / ModelProfile / LLMGateway。
换模型、换厂商都在这层完成，上层代码零改动。
"""

from cforge.gateway.client import ChatResult, LLMGateway, Usage
from cforge.gateway.profiles import (
    INTERN_PROFILES,
    OPENROUTER_PROFILES,
    Backend,
    GatewayConfig,
    ModelProfile,
)

__all__ = [
    "Backend",
    "ModelProfile",
    "GatewayConfig",
    "INTERN_PROFILES",
    "OPENROUTER_PROFILES",
    "LLMGateway",
    "ChatResult",
    "Usage",
]
