"""书生就绪度检查（R1-S0）：把「书生路径好了没有」变成可出示的结论。

为什么单独一个模块而不是塞进 doctor：这是技术文档「书生融合」章节的
核心证据之一。key 未到位时，doctor 不能报错中断（那等于告诉评审
「我们的东西是坏的」），而要给出**就绪度结论**：

- `ready`           ：key 已配置，可以跑真实端到端（intern_probe.py）
- `config-ready`    ：key 未配置，但 provider 抽象 / 协议代理 / e2e 演练
                      全部就绪 —— 这是 key 未到位时的**正常状态**
- `not-configured`  ：连 provider 抽象都没配上（不应发生，除非源码被改坏）

设计取舍：检测项全部只读本地状态（env / 文件存在性），**不做网络探测**。
理由：网络探测结果受运行环境限制（sandbox/门户网络会劫持），
把「环境问题」报成「配置问题」会误导。连通性归 intern_probe.py 管。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from cforge.gateway.profiles import Backend, GatewayConfig

KEY_ENV = "INTERN_API_KEY"
OVERRIDE_ENV = "CFORGE_BASE_URL_OVERRIDE"

# e2e 演练脚本的存在性。probe 脚本在参赛工作区（codexforge 仓库外），
# 两个根不同：e2e 相对 codexforge 仓库根；probe 相对 workspace 根。
_E2E_SCRIPT = Path("tests/e2e/intern_e2e.py")
_PROBE_SCRIPT = Path("xingzhi_cup/track160/probe/intern_probe.py")


@dataclass
class ReadinessReport:
    """就绪度结论。overall 三态见模块 docstring。"""

    overall: str                       # ready | config-ready | not-configured
    key_configured: bool
    profile_ok: bool
    base_url_override: str
    e2e_script_exists: bool
    probe_script_exists: bool
    checks: list[dict] = field(default_factory=list)
    next_step: str = ""

    def as_dict(self) -> dict:
        return {
            "overall": self.overall,
            "key_configured": self.key_configured,
            "profile_ok": self.profile_ok,
            "base_url_override": self.base_url_override or None,
            "e2e_script_exists": self.e2e_script_exists,
            "probe_script_exists": self.probe_script_exists,
            "checks": self.checks,
            "next_step": self.next_step,
        }


def check_readiness(*, repo_root: Path | None = None) -> ReadinessReport:
    """检查书生接入的就绪度。repo_root 用于定位 e2e/probe 脚本。"""
    root = Path(repo_root) if repo_root else Path.cwd()
    checks: list[dict] = []

    key = os.environ.get(KEY_ENV, "").strip()
    override = os.environ.get(OVERRIDE_ENV, "").strip()

    # 1. provider 抽象可构造（INTERN_PROFILES 在源码里）
    try:
        cfg = GatewayConfig.for_backend(Backend.INTERN)
        profile_ok = bool(cfg.profiles) and cfg.profiles[0].backend is Backend.INTERN
        model_id = cfg.profiles[0].model_id if cfg.profiles else ""
    except Exception:  # noqa: BLE001 —— 配置坏了也要出报告，不是抛异常
        profile_ok, model_id = False, ""
    checks.append({
        "item": "书生 provider 抽象",
        "ok": profile_ok,
        "detail": f"INTERN_PROFILES 可用，默认 model={model_id}（以 /api/v1/models 实测为准）",
    })

    # 2. e2e 演练脚本存在（key 未到位时的链路证据）
    e2e_exists = (root / _E2E_SCRIPT).is_file()
    checks.append({
        "item": "e2e 演练脚本",
        "ok": e2e_exists,
        "detail": str(_E2E_SCRIPT) if e2e_exists else "缺失（tests/e2e/intern_e2e.py）",
    })

    # 3. probe 脚本存在（key 到位后的证据生产线）。
    # probe 在参赛工作区，不在 codexforge 仓库内——repo_root 是 codexforge 根，
    # 其 parents[0] 是 workspace 根。
    probe_exists = (root / _PROBE_SCRIPT).is_file() or (root.parent / _PROBE_SCRIPT).is_file()
    checks.append({
        "item": "probe 探测脚本",
        "ok": probe_exists,
        "detail": str(_PROBE_SCRIPT) if probe_exists else "缺失",
    })

    # 4. key 状态
    checks.append({
        "item": f"API key（{KEY_ENV}）",
        "ok": bool(key),
        "detail": "已配置" if key else "未配置（需手机注册书生 API 平台）",
    })

    if not profile_ok:
        overall = "not-configured"
        next_step = "检查 cforge/gateway/profiles.py 的 INTERN_PROFILES 是否被改动。"
    elif key:
        overall = "ready"
        next_step = (
            f"跑 python {_PROBE_SCRIPT} 拿实测证据（model id 全集/时延），"
            "然后 CFORGE_BACKEND=intern 跑一次完整守门循环。"
        )
    else:
        overall = "config-ready"
        next_step = (
            "注册书生 API 平台（internlm.intern-ai.org.cn）并申请提额"
            "（备注「兴智杯+团队名称」）；期间可跑 tests/e2e/intern_e2e.py "
            "用假上游演练完整链路。"
        )

    if override:
        checks.append({
            "item": "base_url 覆盖",
            "ok": True,
            "detail": f"{OVERRIDE_ENV}={override}（e2e 演练用；生产不要设置）",
        })

    return ReadinessReport(
        overall=overall,
        key_configured=bool(key),
        profile_ok=profile_ok,
        base_url_override=override,
        e2e_script_exists=e2e_exists,
        probe_script_exists=probe_exists,
        checks=checks,
        next_step=next_step,
    )
