"""一次性诊断：codex + 书生 397B 能否执行单一明确指令（写文件）。

目的：把「模型不肯动手」与「我们的 prompt 太长/结构问题」分开。
若这条极简指令能改文件，说明链路是通的，问题是 prompt 设计；
若也改不了，说明是 harness/模型层面的硬限制，需要换方案。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cforge.harness import codex_backend as cb
from cforge.harness.compat_proxy import CompatProxy, ProxyConfig

KEY = os.environ.get("INTERN_API_KEY", "")
WS = Path(".data/cases/smokeshow").resolve()
TARGET = WS / "cli" / "smokeshow" / "main.py"
MARKER = "cforge-touch-marker"


def main() -> int:
    if not KEY:
        print("缺 INTERN_API_KEY", file=sys.stderr)
        return 2
    audit: list[dict] = []
    cfg = ProxyConfig(
        upstream_base_url="https://chat.intern-ai.org.cn/api/v1",
        upstream_api_key=KEY, model="intern-s2-preview-397b", audit=audit,
    )
    proxy = CompatProxy(cfg)
    url = proxy.start()
    before = TARGET.read_text(encoding="utf-8")
    try:
        spec = cb.CodexSpec(
            prompt=(f"在文件 `cli/smokeshow/main.py` 最末尾追加一行注释 `# {MARKER}`。"
                    "现在就执行，不要读其它文件，不要探索目录。"),
            workspace=WS, model="intern-s2-preview-397b",
            provider_base_url=url, context_window=262_144, timeout_s=150,
            sandbox="danger-full-access", isolate_auth=True,
        )
        code, events = cb.run_codex(
            spec, env_extra={spec.provider_env_key: KEY})
        print(f"exit={code} events={len(events)}")
        for e in events:
            txt = str(e.payload)[:200].replace("\n", " ")
            print(f"  [{e.kind}] {txt}")
    finally:
        proxy.stop()

    after = TARGET.read_text(encoding="utf-8")
    print(f"\n文件变化：{len(before)} -> {len(after)} 字符")
    print("追加成功：", MARKER in after and after != before)
    print("\n=== 代理审计 ===")
    for a in audit[:3]:
        print(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
