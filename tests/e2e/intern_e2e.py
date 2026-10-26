"""书生链路 e2e 演练：codex → compat_proxy → 假书生上游。

**这个脚本证明什么**（诚实声明）：
- 证明：`CFORGE_BACKEND=intern` + `CFORGE_BASE_URL_OVERRIDE` 下，
  codex CLI 能通过协议适配代理完成一次真实 turn；上游收到了带
  INTERN_API_KEY 的请求；profiles 的 override 机制工作。
- 不证明：真实书生模型的输出质量。那需要真 key，走
  `xingzhi_cup/track160/probe/intern_probe.py`（R1-S1）。

跑法（独立脚本，非 pytest 收集——它驱动真实 codex 子进程，慢）：
    cd codexforge
    python tests/e2e/intern_e2e.py

退出码：0 = 全断言通过；1 = 有断言失败（明细打印到 stdout）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cforge.harness import codex_backend as cb  # noqa: E402
from cforge.harness.codex_backend import CodexSpec, run_codex  # noqa: E402
from cforge.harness.compat_proxy import CompatProxy, ProxyConfig  # noqa: E402
from tests.e2e.intern_stub import InternStub  # noqa: E402

FAKE_KEY = "sk-intern-fake-for-e2e"
PROMPT = "只回复 CODEXFORGE-INTERN-OK 这一串字符，不要做任何其他事。"


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    exe = cb.find_codex()
    print("=" * 66)
    print("书生链路 e2e 演练（假上游）")
    print("=" * 66)
    if not exe:
        print("  [SKIP] 未找到 codex CLI，无法演练（安装：npm i -g @openai/codex）")
        print("\n结论：SKIP（环境缺 codex，不是链路失败）")
        return 0

    with tempfile.TemporaryDirectory(prefix="cforge-intern-e2e-") as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()

        with InternStub(reply_text="CODEXFORGE-INTERN-OK") as stub:
            # 1. profiles 的 override 机制
            os.environ["CFORGE_BASE_URL_OVERRIDE"] = stub.base_url
            os.environ["CFORGE_BACKEND"] = "intern"
            os.environ["INTERN_API_KEY"] = FAKE_KEY
            try:
                from cforge.gateway.profiles import GatewayConfig

                cfg = GatewayConfig.from_env()
                check("profiles.from_env 读取 base_url override",
                      cfg.base_url_override == stub.base_url,
                      f"override={cfg.base_url_override}")
                check("effective_base_url 生效",
                      cfg.effective_base_url(cfg.profiles[0]) == stub.base_url)

                # 2. compat_proxy 指向假上游
                proxy = CompatProxy(ProxyConfig(
                    upstream_base_url=stub.base_url,
                    upstream_api_key=FAKE_KEY,
                    model="intern-s2-preview",
                    listen_port=0,
                ))
                url = proxy.start()
                check("compat_proxy 启动", url.startswith("http://127.0.0.1:"), url)

                # 3. 真实 codex turn
                spec = CodexSpec(
                    prompt=PROMPT,
                    workspace=ws,
                    model="intern-s2-preview",
                    provider_base_url=url,
                    provider_env_key="CFORGE_UPSTREAM_KEY",
                    timeout_s=180,
                )
                env_extra = {"CFORGE_UPSTREAM_KEY": FAKE_KEY}
                t0 = time.monotonic()
                exit_code, events = run_codex(spec, env_extra=env_extra)
                elapsed = time.monotonic() - t0

                check("codex turn 退出码为 0", exit_code == 0, f"exit={exit_code}")
                check("codex 产出事件", len(events) > 0, f"{len(events)} events")
                messages = [e for e in events if e.is_message]
                joined = " ".join(e.text() for e in messages)
                check("模型回复内容到达", "CODEXFORGE-INTERN-OK" in joined,
                      f"messages={joined[:60]!r}")

                # 4. 假上游收到了请求（链路真的通了）
                chat_reqs = [r for r in stub.requests
                             if r.path.endswith("/chat/completions")]
                check("上游收到 chat/completions 请求", len(chat_reqs) >= 1,
                      f"{len(chat_reqs)} 次")
                if chat_reqs:
                    auth = chat_reqs[0].authorization
                    check("请求携带书生 key", f"Bearer {FAKE_KEY}" in auth,
                          auth[:24] + "…")

                # 5. 代理审计（cfg.audit——ProxyConfig 上的列表）
                check("proxy 审计记录转发", len(proxy.cfg.audit) >= 1,
                      f"{len(proxy.cfg.audit)} 条")

                print(f"\n  耗时 {elapsed:.1f}s；请求路径分布："
                      f"{ {r.path: 1 for r in stub.requests} }")
                proxy.stop()
            finally:
                os.environ.pop("CFORGE_BASE_URL_OVERRIDE", None)
                os.environ.pop("CFORGE_BACKEND", None)
                os.environ.pop("INTERN_API_KEY", None)

    failed = [n for n, ok, _ in results if not ok]
    print()
    print("=" * 66)
    if failed:
        print(f"❌ {len(failed)}/{len(results)} 断言失败：{failed}")
        return 1
    print(f"✅ {len(results)}/{len(results)} 断言全部通过")
    print("结论：书生接入路径已演练通。真 key 到位后跑 intern_probe.py 即出端到端证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
