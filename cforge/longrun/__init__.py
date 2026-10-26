"""longrun 包：超长程任务的自主反馈与状态外置。

- state：跨上下文重置的方向/教训/停滞状态
- loop：多轮自主反馈循环（目标 + 整洁度 + 停滞三路信号）
"""

from cforge.longrun.loop import (
    Feedback,
    LongRunConfig,
    LongRunLoop,
    LongRunResult,
    StopReason,
    TurnRecord,
    build_advice,
    detect_long_run,
)
from cforge.longrun.state import (
    Direction,
    StagnationResponse,
    TargetState,
    direction_signature,
    stagnation_ladder,
    workspace_fingerprint,
)

__all__ = [
    "TargetState", "Direction", "stagnation_ladder", "StagnationResponse",
    "workspace_fingerprint", "direction_signature",
    "LongRunLoop", "LongRunConfig", "LongRunResult", "TurnRecord",
    "Feedback", "StopReason", "build_advice", "detect_long_run",
]
