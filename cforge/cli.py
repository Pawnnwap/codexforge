"""CodexForge CLI：架构自检、上游探测、门禁安装、试跑。

设计原则：每个子命令都能独立证明架构的某一块是活的。
这对应赛道「完整性 30% · 成果可复现性」——评审可以逐条命令验证。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cforge import __version__
from cforge.checks.gates import GATE_SPECS, GateSet, hooks_json_fragment, install_gate_scripts
from cforge.gateway.client import LLMGateway
from cforge.gateway.profiles import Backend, GatewayConfig
from cforge.harness import codex_backend as cb


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    """环境体检：这是复现的第一步，必须先能跑通。"""
    checks: list[dict] = []

    exe = cb.find_codex()
    ver = cb.codex_version(exe)
    checks.append({
        "item": "codex 可执行文件",
        "ok": bool(exe),
        "detail": exe or "未找到（npm i -g @openai/codex）",
    })
    checks.append({
        "item": "codex 版本",
        "ok": bool(ver),
        "detail": ver or "无法获取",
        "note": f"门禁在 {cb.VERIFIED_CODEX_VERSION} 上验证过；版本不同需重跑 tests/",
    })

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append({
        "item": "Python",
        "ok": sys.version_info >= (3, 11),
        "detail": py_ver,
    })

    # provider key
    cfg = GatewayConfig.from_env()
    prof = cfg.profiles[0] if cfg.profiles else None
    if prof:
        has_key = bool(prof.api_key())
        checks.append({
            "item": f"API key ({prof.api_key_env})",
            "ok": has_key,
            "detail": "已设置" if has_key else "未设置",
            "backend": prof.backend.value,
            "model": prof.model_id,
        })

    # 书生就绪度（R1-S0）：缺 key 时给就绪度结论而非报错中断——
    # 「key 未到位」是既定状态，doctor 的职责是讲清「还差什么」，不是判死刑。
    # 设计文档 T02 的形态要求（doctor 追加就绪度行），由独立模块 intern_readiness 承载。
    if args.backend == "intern":
        from cforge.gateway.intern_readiness import check_readiness

        rr = check_readiness(repo_root=Path(__file__).resolve().parents[1])
        for c in rr.checks:
            checks.append({"item": f"intern · {c['item']}", "ok": c["ok"],
                           "detail": c["detail"]})
        checks.append({
            "item": "intern · 就绪度结论",
            "ok": rr.overall != "not-configured",
            "detail": f"{rr.overall} → {rr.next_step}",
        })

    ok_all = all(c["ok"] for c in checks)
    print("=" * 66)
    print(f"CodexForge {__version__} · 环境体检")
    print("=" * 66)
    for c in checks:
        mark = "OK  " if c["ok"] else "FAIL"
        print(f"  [{mark}] {c['item']:<28} {c['detail']}")
        if c.get("note"):
            print(f"         └ {c['note']}")
    print()
    print("结论：" + ("环境就绪" if ok_all else "存在阻塞项，见上"))
    if args.json:
        _print(checks)
    return 0 if ok_all else 1


def cmd_probe(args: argparse.Namespace) -> int:
    """探测上游：wire_api 支持情况 + 一次最小对话。"""
    backend = Backend(args.backend)
    cfg = GatewayConfig.for_backend(backend)
    if args.model and cfg.profiles:
        from cforge.gateway.profiles import ModelProfile
        p = cfg.profiles[0]
        cfg.profiles[0] = ModelProfile(
            backend=p.backend, model_id=args.model, base_url=p.base_url,
            api_key_env=p.api_key_env, max_context=p.max_context,
            supports_tools=p.supports_tools, supports_vision=p.supports_vision,
            note="cli override",
        )
    prof = cfg.profiles[0]
    key = prof.api_key()

    out: dict = {
        "backend": backend.value,
        "model": prof.model_id,
        "base_url": prof.base_url,
        "api_key_set": bool(key),
    }
    if not key:
        out["error"] = f"环境变量 {prof.api_key_env} 未设置"
        _print(out)
        return 2

    out["wire_api"] = cb.probe_wire_api(prof.base_url, key, prof.model_id)

    gw = LLMGateway(cfg)
    out["chat"] = gw.probe()
    out["usage_total"] = {
        "prompt": gw.total_usage.prompt_tokens,
        "completion": gw.total_usage.completion_tokens,
        "total": gw.total_usage.total_tokens,
    }
    out["per_model_stats"] = gw.stats
    _print(out)

    if not out["chat"].get("ok"):
        print("\n提示：`rate_limited_shared_pool` 表示免费模型的上游共享池被限流，"
              "不是配置错误。等几十秒重试，或换 profiles 里的降级模型。")
        return 1
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    """门禁管理：安装 / 检查 / 单文件试跑。"""
    src = Path(args.source) if args.source else None
    target = Path(args.install_to) if args.install_to else None

    if args.install:
        if not src or not target:
            print("--install 需要同时给 --source 与 --install-to")
            return 2
        copied = install_gate_scripts(src, target)
        print(f"已复制 {len(copied)} 个脚本 -> {target}")
        for name in copied:
            print(f"  + {name}")
        frag = hooks_json_fragment(sys.executable, target)
        frag_path = target / "hooks.fragment.json"
        frag_path.write_text(json.dumps(frag, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已生成 hooks 片段：{frag_path}")
        print("合并进 ~/.codex/hooks.json 的 hooks.PostToolUse 数组即可。")
        print("注意：新增 hook 后需在交互式会话里接受信任，否则不会执行。")
        return 0

    if not target:
        print("门禁检查需要 --install-to <脚本目录>")
        return 2
    gs = GateSet(scripts_dir=target)
    print("=" * 66)
    print(f"门禁目录：{target}")
    print("=" * 66)
    avail, missing = gs.available(), gs.missing()
    print(f"  已安装 {len(avail)}/{len(GATE_SPECS)}")
    for spec in GATE_SPECS:
        mark = "OK  " if spec["name"] in avail else "MISS"
        print(f"  [{mark}] {spec['name']:<18} {spec['lang']:<12} 修={spec['fixes']}")

    if args.test_file:
        print(f"\n对 {args.test_file} 试跑 py_edit_gate：")
        v = gs.run("py_edit_gate", file_path=args.test_file, cwd=str(Path(args.test_file).parent))
        _print({
            "ran": v.ran, "clean": v.clean, "exit": v.exit_code,
            "advisory": v.advisory, "error": v.error,
        })
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    """把门禁写入 ~/.codex/hooks.json，自动备份。

    只增不改：保留已有 hook，把我们的 8 个门禁追加到 PostToolUse。
    """
    codex_home = Path(args.codex_home or (Path.home() / ".codex"))
    hooks_path = codex_home / "hooks.json"
    scripts_dir = Path(args.scripts_dir)

    if not scripts_dir.is_dir():
        print(f"门禁脚本目录不存在：{scripts_dir}")
        return 2

    existing: dict = {}
    if hooks_path.is_file():
        try:
            existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{hooks_path} 不是合法 JSON，中止以免破坏配置")
            return 1

    backup = codex_home / "hooks.json.cforge-backup"
    if hooks_path.is_file() and not backup.is_file():
        backup.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已备份原配置 -> {backup}")

    frag = hooks_json_fragment(sys.executable, scripts_dir)
    hooks = existing.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])

    # 去重：同 command 的不重复追加
    existing_cmds = set()
    for block in post:
        for h in block.get("hooks", []):
            existing_cmds.add(h.get("command", ""))

    added = 0
    for block in frag["hooks"]["PostToolUse"]:
        cmd = block["hooks"][0]["command"]
        if cmd in existing_cmds:
            continue
        post.append(block)
        added += 1

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已追加 {added} 个门禁 -> {hooks_path}")
    print("下一步：开一个交互式 codex 会话接受信任提示，否则 hook 不执行。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """试跑：走完整编排链。"""
    from cforge.harness.supervisor import Protocol, Target
    from cforge.runtime.orchestrator import Orchestrator, RunConfig

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"工作区不存在：{ws}")
        return 2

    protocol = Protocol(
        input_desc=args.input_desc or f"workspace={ws}",
        targets=[
            Target(id="t1", description=f"文件存在：{args.expect_file}",
                   check="file_exists", params={"path": args.expect_file}),
        ],
        restrictions=["不要修改工作区之外的文件"],
    )

    cfg = RunConfig(
        workspace=ws,
        prompt=args.prompt,
        protocol=protocol,
        backend=Backend(args.backend),
        model_override=args.model or "",
        execute_codex=not args.dry_run,
        gate_scripts_dir=Path(args.scripts_dir) if args.scripts_dir else None,
        run_dir=Path(args.run_dir),
        force_proxy=args.force_proxy,
        proxy_port=args.proxy_port,
        timeout_s=args.timeout,
    )
    orch = Orchestrator(cfg)
    if args.write_protocol:
        p = orch.write_protocol()
        print(f"protocol 已写入 {p}")
    report = orch.run()
    _print(report.to_dict())
    print(f"\n产物目录：{report.run_dir}")
    return 0 if report.ok else 1


def cmd_quality(args: argparse.Namespace) -> int:
    """整洁度评估：默认扫当前项目，可指定路径。"""
    from cforge.quality.metrics import evaluate
    from cforge.quality.clones import scan as scan_clones

    target = Path(args.path or ".").resolve()
    if not target.is_dir():
        print(f"目录不存在：{target}")
        return 2

    report = evaluate(target)
    print("=" * 68)
    print(f"整洁度评估 · {target}")
    print("=" * 68)
    print(f"  得分      {report.score}/100")
    print(f"  文件数    {len(report.files)}")
    print(f"  有效行数  {report.total_sloc}")
    print(f"  问题数    {len(report.smells)}")

    from collections import Counter
    if report.smells:
        print("\n  问题分布（按修复分级）：")
        from cforge.quality.repair import TIER_LABEL

        for kind, cnt in Counter(s.kind for s in report.smells).most_common():
            sev = next(s.severity for s in report.smells if s.kind == kind)
            tier = next(s.repair_tier for s in report.smells if s.kind == kind)
            print(f"    {kind:<20} {cnt:>3}  [{sev}]  {TIER_LABEL.get(tier, tier)}")

    if not args.no_clones:
        clones = scan_clones(target)
        print(f"\n  重复代码：{len(clones.units)} 个函数中检出 "
              f"{len(clones.pairs)} 对（{clones.duplicate_ratio:.1%}）")
        for p in clones.pairs[:8]:
            print(f"    {p}")

    if args.verbose and report.smells:
        print("\n  明细（按严重度）：")
        print(report.actionable_text(limit=30))

    return 0 if report.score >= args.min_score else 1


def cmd_contract(args: argparse.Namespace) -> int:
    """查看仓库约定（AGENTS.md）会被怎么注入修复 prompt。"""
    from cforge.repo_contract import find_contract, load_contract, render_for_prompt

    ws = Path(args.path or ".").resolve()
    if not ws.is_dir():
        print(f"目录不存在：{ws}")
        return 2

    found = find_contract(ws)
    if found is None:
        print(f"未找到 AGENTS.md（向上查了 3 层）：{ws}")
        print("提示：这不是错误。没有约定时修复 prompt 不会多占任何 token。")
        return 0

    contract = load_contract(ws, max_chars=args.max_chars)
    if contract is None:
        print(f"找到 {found} 但内容为空或不可读")
        return 1

    print("=" * 68)
    print(f"仓库约定 · {contract.path}")
    print("=" * 68)
    print(render_for_prompt(contract))
    if args.raw:
        print()
        print(f"  原文件字符数  {contract.raw_chars}")
        print(f"  实际注入字符  {contract.used_chars}")
        print(f"  是否截断      {'是' if contract.truncated else '否'}")
        print(f"  命中条目数    {len(contract.kept_rules)}")
    return 0


def cmd_longrun(args: argparse.Namespace) -> int:
    """跑一次长程任务：真实 codex + 自主反馈 + 整洁度门。"""
    from cforge.harness.compat_proxy import CompatProxy, ProxyConfig
    from cforge.longrun.loop import LongRunConfig, LongRunLoop
    from cforge.quality.metrics import evaluate
    from cforge.runtime.orchestrator import _resolve_api_key, _base_url_for

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"工作区不存在：{ws}")
        return 2

    api_key = _resolve_api_key(args.backend)
    if not api_key:
        env = "INTERN_API_KEY" if args.backend == "intern" else "OPENROUTER_API_KEY"
        print(f"缺少 {env}")
        return 2

    model = args.model or GatewayConfig.for_backend(args.backend).profiles[0].model_id
    proxy = CompatProxy(ProxyConfig(
        upstream_base_url=_base_url_for(args.backend),
        upstream_api_key=api_key,
        model=model,
        listen_port=0,
    ))
    url = proxy.start()
    print(f"proxy: {url}  model: {model}")
    try:
        from experiments.run_longrun import CodexTurnRunner
        from experiments.harness import Task
        task = Task(
            id="cli", name="cli",
            prompt=args.prompt,
            check="file_exists",
            params={"path": args.expect_file},
        )
        runner = CodexTurnRunner(
            workspace=ws, task=task, model=model,
            base_url=url, api_key=api_key, timeout_s=args.timeout,
        )
        loop = LongRunLoop(
            workspace=ws,
            runner=runner,
            goal_checker=lambda w: task.verify(w),
            config=LongRunConfig(
                max_turns=args.max_turns,
                min_quality=args.min_quality,
                patience=2,
            ),
            target=args.prompt,
            verbose=True,
        )
        result = loop.run()
        _print(result.as_dict())
        print(f"\n最终整洁度：{result.final_quality}（{result.quality_delta:+.1f}）")
        return 0 if result.goal_met else 1
    finally:
        proxy.stop()


def cmd_guard(args: argparse.Namespace) -> int:
    """常驻守门：持续监控工作区，按需做整洁度修复。

    这是面向一人公司的产品形态 —— 不是「我想起来才跑一次」，
    而是「挂在项目里自己看着」。
    """
    from cforge.watch import (
        CodexFixAgent,
        Guardian,
        GuardianConfig,
        ScriptedFixAgent,
        WatchConfig,
    )
    from cforge.runtime.orchestrator import _base_url_for, _resolve_api_key

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"工作区不存在：{ws}")
        return 2

    watch_cfg = WatchConfig(
        cooldown_s=args.cooldown,
        debt_score=args.debt_score,
        max_high=args.max_high,
        massive_threshold=args.massive_threshold,
    )
    gcfg = GuardianConfig(
        poll_interval_s=args.poll,
        max_consecutive_failures=args.max_failures,
        total_budget_s=args.budget,
    )

    # 选修复器：--fixer scripted 用 AST 确定性修复（零成本），
    # codex 则真的调 LLM。
    if args.fixer == "scripted":
        agent = ScriptedFixAgent()
        print("修复器：scripted（AST 确定性，零 token）")
    else:
        api_key = _resolve_api_key(args.backend)
        if not api_key:
            env = "INTERN_API_KEY" if args.backend == "intern" else "OPENROUTER_API_KEY"
            print(f"缺少 {env}；如只想验证链路，可加 --fixer scripted")
            return 2
        model = args.model or GatewayConfig.for_backend(args.backend).profiles[0].model_id

        # codex 只认 responses，上游只有 chat → 必须起 proxy
        from cforge.harness.compat_proxy import CompatProxy, ProxyConfig
        proxy = CompatProxy(ProxyConfig(
            upstream_base_url=_base_url_for(args.backend),
            upstream_api_key=api_key,
            model=model,
            listen_port=0,
        ))
        url = proxy.start()
        print(f"修复器：codex（{model}），proxy={url}")
        agent = CodexFixAgent(
            workspace=ws, model=model, base_url=url, api_key=api_key
        )

    try:
        guardian = Guardian(
            workspace=ws,
            agent=agent,
            watch_config=watch_cfg,
            config=gcfg,
            verbose=True,
        )
        print("=" * 68)
        print(f"CodexForge 守门 · {ws}")
        print(f"  冷却 {watch_cfg.cooldown_s:.0f}s | 债务阈值 {watch_cfg.debt_score} "
              f"| high 上限 {watch_cfg.max_high}")
        print(f"  轮询 {gcfg.poll_interval_s:.0f}s | 最多 {args.max_cycles or '不限'} 轮")
        print("=" * 68)

        out = guardian.run_forever(max_cycles=args.max_cycles)
        print()
        print(f"停止原因：{out['stop_reason']}")

        session = out.get("session", {})
        events = out["events"]
        ran = [e for e in events if e["ran"]]
        print(f"本次检查 {len(events)} 次，实际执行 {len(ran)} 次")

        # 只有本次真的动过手，才谈「质量变化」——
        # 否则拿历史基线对比会造成「跳过却显示质量下降」的误导（实测踩过）
        if ran:
            first = ran[0]["quality_before"]
            last = ran[-1]["quality_after"]
            print(f"本次质量：{first} → {last} ({last - first:+.1f})")
        else:
            print("本次未执行改动：" + (events[-1]["reason"] if events else "无事件"))

        st = out["state"]
        if st.get("runs_total"):
            print(f"历史累计：检查 {st['checks_total']} 次，执行 {st['runs_total']} 次，"
                  f"接受 {st['runs_accepted']} / 回滚 {st['runs_rejected']}")
            print(f"基线 → 当前：{st['quality_start']} → {st['quality_last']}")

        print(f"报告：{out['report']}")
        return 0
    finally:
        if args.fixer != "scripted":
            proxy.stop()


def cmd_mcp(args: argparse.Namespace) -> int:
    """MCP server：把质量评估能力以 MCP 协议暴露（stdio）。

    设计取舍：自研标准库 JSON-RPC 而非官方 mcp SDK——运行时零第三方依赖
    是架构决策（评审复现不装包、避开依赖树 copyleft）。协议版本 2025-06-18。
    """
    if args.tools:
        from cforge.mcp.tools import tool_schemas

        _print(tool_schemas())
        return 0
    from cforge.mcp.server import MCPServer

    audit = Path(args.audit) if args.audit else None
    return MCPServer(audit_path=audit).serve()


def cmd_skills(args: argparse.Namespace) -> int:
    """Agent Skills：技能 catalog / 正文按需加载 / 安装。

    技能正文永不进初始 prompt——这是「codex 把本机 20+ 技能注入系统提示词」
    实测污染的对策（见 skills/builtin/context-hygiene.md）。
    """
    from cforge.skills.skill_bank import SkillBank

    bank = SkillBank(Path(args.dir) if args.dir else None)
    if args.install:
        dest = Path(args.install)
        copied = bank.install_builtin(dest)
        print(f"已安装 {len(copied)} 个内置技能 -> {dest}")
        for p in copied:
            print(f"  + {p.name}")
        print("\n把该目录配给 harness 的技能目录即可（codex 用 --skills 或配置项，"
              "以所用 harness 文档为准）。")
        return 0
    catalog = bank.catalog()
    print("=" * 66)
    print(f"技能 catalog（{len(catalog)} 个；正文按需加载，不随 catalog 进 prompt）")
    print("=" * 66)
    for meta in catalog:
        print(f"  {meta.name:<20} {meta.description}")
    if args.load:
        try:
            text = bank.load(args.load)
        except KeyError:
            print(f"\n技能不存在：{args.load}（用 python -m cforge.cli skills 查看可用清单）")
            return 2
        except ValueError as e:
            print(f"\n加载失败：{str(e).strip(chr(39))}")
            return 2
        print(f"\n----- {args.load} 正文 -----\n{text}")
    return 0


def cmd_layout(args: argparse.Namespace) -> int:
    """打印架构分层与模块状态。架构自证用的命令。"""
    rows = [
        ("gateway/profiles.py", "provider 抽象：书生 / OpenRouter 同接口", "实现"),
        ("gateway/client.py", "OpenAI 兼容网关：降级链 + 结构化错误", "实现"),
        ("harness/codex_backend.py", "codex CLI 驱动：argv 构造 + JSONL 解析", "实现"),
        ("harness/compat_proxy.py", "Responses↔Chat 协议适配（SSE 流式）", "实现"),
        ("harness/supervisor.py", "Goal Guard：证据门控裁决", "实现"),
        ("checks/gates.py", "8 语言编辑门禁桥接", "实现"),
        ("quality/metrics.py", "整洁度度量：8 指标 + 0-100 分", "**核心**"),
        ("quality/clones.py", "跨文件重复检测（AST 指纹）", "**核心**"),
        ("quality/polish.py", "整洁度收敛：净改善门 + 回滚", "**核心**"),
        ("longrun/state.py", "跨上下文记忆：方向/教训/停滞", "**核心**"),
        ("longrun/loop.py", "长程自主反馈循环（三路信号）", "**核心**"),
        ("runtime/orchestrator.py", "编排：bootstrap→proxy→execute→judge", "实现"),
        ("telemetry/", "审计链与用量统计", "占位"),
    ]
    print("=" * 78)
    print(f"CodexForge {__version__} · 架构分层")
    print("=" * 78)
    print(f"  {'模块':<30} {'职责':<34} 状态")
    print("  " + "-" * 74)
    for mod, duty, state in rows:
        print(f"  {mod:<30} {duty:<34} {state}")
    print()
    print("定位：general coding agent，主打超长程任务的自主反馈与代码整洁度")
    print("执行后端：仅 codex（0.163.0，wire_api 只支持 responses）")
    print("运行时依赖：零第三方库（仅标准库）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cforge", description="CodexForge — Codex-only supervised agent harness",
    )
    ap.add_argument("--version", action="version", version=f"CodexForge {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="环境体检")
    p.add_argument("--json", action="store_true")
    p.add_argument("--backend", default="", choices=["", "openrouter", "intern"],
                   help="附加上下文检查：intern = 书生就绪度三态结论")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("mcp", help="MCP server（stdio）：暴露质量评估工具")
    p.add_argument("--tools", action="store_true", help="只打印工具清单后退出")
    p.add_argument("--audit", default="", help="审计日志路径（可选）")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("skills", help="Agent Skills：catalog / 按需加载 / 安装")
    p.add_argument("--dir", default="", help="技能目录，默认内置")
    p.add_argument("--load", default="", help="加载指定技能正文")
    p.add_argument("--install", default="", help="把内置技能安装到指定目录")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("contract", help="查看本仓库的 AGENTS.md 约定（会被注入修复 prompt）")
    p.add_argument("--path", default=".", help="工作区路径")
    p.add_argument("--max-chars", type=int, default=1500, help="注入内容上限")
    p.add_argument("--raw", action="store_true", help="额外打印原文件字符数等统计")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("layout", help="打印架构分层")
    p.set_defaults(func=cmd_layout)

    p = sub.add_parser("probe", help="探测上游 wire_api 与对话连通性")
    p.add_argument("--backend", default="openrouter", choices=["openrouter", "intern"])
    p.add_argument("--model", default="")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("gates", help="门禁安装/检查/试跑")
    p.add_argument("--source", default="")
    p.add_argument("--install-to", default="")
    p.add_argument("--install", action="store_true")
    p.add_argument("--test-file", default="")
    p.set_defaults(func=cmd_gates)

    p = sub.add_parser("install-hooks", help="把门禁写入 ~/.codex/hooks.json")
    p.add_argument("--scripts-dir", required=True)
    p.add_argument("--codex-home", default="")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("run", help="试跑完整编排链")
    p.add_argument("--workspace", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--backend", default="openrouter", choices=["openrouter", "intern"])
    p.add_argument("--model", default="")
    p.add_argument("--expect-file", default="README.md")
    p.add_argument("--input-desc", default="")
    p.add_argument("--scripts-dir", default="")
    p.add_argument("--run-dir", default="runs")
    p.add_argument("--proxy-port", type=int, default=0,
                   help="代理监听端口；0 = 自动分配（推荐，避免 Windows 10013）")
    p.add_argument("--force-proxy", action="store_true")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write-protocol", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("quality", help="整洁度评估（AST 指标 + 重复检测）")
    p.add_argument("--path", default=".", help="要评估的目录，默认当前目录")
    p.add_argument("--min-score", type=float, default=80.0,
                   help="低于此分返回退出码 1")
    p.add_argument("--no-clones", action="store_true", help="跳过重复代码检测")
    p.add_argument("--verbose", action="store_true", help="打印问题明细")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("longrun", help="跑长程任务（真实 codex + 自主反馈）")
    p.add_argument("--workspace", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--expect-file", default="", help="达成条件：该文件存在")
    p.add_argument("--backend", default="openrouter", choices=["openrouter", "intern"])
    p.add_argument("--model", default="")
    p.add_argument("--max-turns", type=int, default=5)
    p.add_argument("--min-quality", type=float, default=70.0)
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_longrun)

    p = sub.add_parser("guard", help="常驻守门：持续监控并按需修复整洁度")
    p.add_argument("--workspace", required=True)
    p.add_argument("--fixer", default="scripted", choices=["scripted", "codex"],
                   help="scripted=AST 确定性修复（零 token）；codex=调 LLM")
    p.add_argument("--backend", default="openrouter",
                   choices=["openrouter", "intern"])
    p.add_argument("--model", default="")
    p.add_argument("--cooldown", type=float, default=600.0,
                   help="两次运行最小间隔（秒）")
    p.add_argument("--debt-score", type=float, default=80.0,
                   help="整洁度低于此分才动手")
    p.add_argument("--max-high", type=int, default=2,
                   help="high 级问题超过此数才动手")
    p.add_argument("--massive-threshold", type=int, default=25,
                   help="变更文件数达此值视为用户在大改，跳过")
    p.add_argument("--poll", type=float, default=60.0, help="轮询间隔（秒）")
    p.add_argument("--max-cycles", type=int, default=1, help="最多检查几轮")
    p.add_argument("--max-failures", type=int, default=3, help="连续失败熔断阈值")
    p.add_argument("--budget", type=float, default=0.0, help="总时间预算（秒），0=不限")
    p.set_defaults(func=cmd_guard)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
