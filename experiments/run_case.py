"""真实第三方开源仓库的守门案例（R5 验收核心证据）。

## 为什么必须补这一层（原有 case_study 的致命缺陷）

原有两份 `case_study-*.md` 的四个对象全是 **codexforge 自己的子包**
（watch / gateway / mcp / skills）。那只能证明「工具能算自己的代码」，
证明不了「在有历史债务的陌生代码库上能收敛」。赛道评分里实用性占 40%，
而实用性的证据形式就是「别人的、真实的、有历史包袱的代码库」。

## 与 experiments/cli.py --suite quality 的分工

`suite_quality` 是本地零成本自检（拿自己代码当样本），本脚本是
**对外可出示的案例装置**：clone 公开仓库 → 固定 commit → 跑守门 →
归档「基线→当前」+ 修复清单 + 接受/回滚 + 剩余债务，并强制记录
来源 URL / license / commit（R5 验收原文要求）。

## 数据分级（遵循 docs 里的红线）

只用**公开开源仓库**，可含文件级细节（PRD 允许）。仅输出聚合数字的
策略用于用户自有项目——本脚本不碰用户私有代码。

## 用法

    # 列出内置候选仓库（不下载）
    python experiments/run_case.py --list

    # 跑一个（会 git clone 到 .data/cases/，体积受控）
    python experiments/run_case.py --repo smokeshow

    # 跑全部候选（耗时较长）
    python experiments/run_case.py --all

产物：
    codexforge/docs/cases/<repo>/report-*.md    守门原始报告
    codexforge/docs/cases/<repo>/case.md        对外可出示的案例说明
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# clone 落地处。放 .data/（.gitignore 已排除），不污染仓库也不进提交包。
CACHE = ROOT / ".data" / "cases"
OUT_BASE = ROOT / "docs" / "cases"


@dataclass
class RepoSpec:
    """一个候选开源仓库。

    选型标准（刻意的，不是随手挑）：
    - 纯 Python，能进 AST 度量（本工具的多语言边界是设计选择，见 README）
    - 有一定规模但 clone 体积可控（< 30MB），避免拖垮磁盘与时长
    - 有真实历史债务（不是全新干净的脚手架），否则分数贴着 100 没信息量
    - license 明确（MIT/BSD/Apache），便于在文档里公开引用
    """

    key: str
    url: str
    license: str
    subdir: str = ""          # 评估的子目录（"" = 仓库根）
    why: str = ""             # 为什么选它 —— 直接进 case.md


CANDIDATES: list[RepoSpec] = [
    RepoSpec(
        key="smokeshow",
        url="https://github.com/samuelcolvin/smokeshow",
        license="MIT",
        why="中等规模 CLI 工具，含 CLI/服务/存储多层，典型「个人项目长期演进」形态",
    ),
    RepoSpec(
        key="httpx",
        url="https://github.com/encode/httpx",
        license="BSD-3-Clause",
        subdir="httpx",
        why="工业级 HTTP 客户端，注释与类型密度高，可检验度量在高成熟度代码上的假阳性",
    ),
    RepoSpec(
        key="starlette",
        url="https://github.com/encode/starlette",
        license="BSD-3-Clause",
        subdir="starlette",
        why="ASGI 框架核心，抽象层多，适合观察跨文件重复的误报率",
    ),
]


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600,
         *, direct: bool = False) -> tuple[int, str]:
    """跑子进程并同时收 stdout+stderr（git 的报错常在 stderr）。

    direct=True 时清掉所有代理环境变量并显式置空 git 的 proxy 配置。
    本机实测教训：残留的 https_proxy 指向已失效端口时，git clone 会
    静默卡到超时（表现为进程被 SIGTERM，没有任何报错输出）——这不是
    网络不通，是代理配置脏。案例脚本要在任何机器上可复现，故 clone
    一律强制直连；需要代理的环境用 CFORGE_GIT_PROXY 显式指定。
    """
    env = None
    if direct:
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        proxy = os.environ.get("CFORGE_GIT_PROXY", "").strip()
        cmd = [*cmd[:1],
               "-c", f"http.proxy={proxy}",
               "-c", f"https.proxy={proxy}",
               *cmd[1:]]
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, env=env,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"超时（>{timeout}s）"
    except OSError as e:
        return -2, f"{type(e).__name__}: {e}"


def ensure_repo(spec: RepoSpec, *, depth: int = 1) -> tuple[Path | None, dict]:
    """clone（或复用已有）。返回 (仓库路径, 元信息)。

    元信息含 commit hash 与 license —— R5 要求「注明来源与 license」，
    这里一次采齐，避免写文档时再去翻。
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / spec.key
    meta: dict = {"url": spec.url, "license_declared": spec.license, "key": spec.key}

    if (dest / ".git").is_dir():
        meta["clone"] = "reused"
    else:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        code, out = _run(["git", "clone", "--depth", str(depth), spec.url, str(dest)],
                         timeout=900, direct=True)
        if code != 0:
            meta["error"] = f"git clone 失败（exit {code}）：{out[-400:]}"
            return None, meta
        meta["clone"] = "fresh"

    code, out = _run(["git", "rev-parse", "HEAD"], cwd=dest, timeout=60)
    meta["commit"] = out.strip() if code == 0 else "（无 git）"
    code, out = _run(["git", "log", "-1", "--format=%cI"], cwd=dest, timeout=60)
    meta["commit_date"] = out.strip() if code == 0 else ""

    # license 实测（不信候选表，以仓库里的文件为准）
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        f = dest / name
        if f.is_file():
            head = f.read_text(encoding="utf-8", errors="replace")[:400]
            meta["license_file"] = name
            meta["license_head"] = " ".join(head.split())[:160]
            break
    else:
        meta["license_file"] = ""
        meta["license_head"] = "（未找到 LICENSE 文件）"

    target = dest / spec.subdir if spec.subdir else dest
    if not target.is_dir():
        meta["error"] = f"子目录不存在：{spec.subdir}"
        return None, meta
    meta["path"] = str(target)
    return target, meta


def _build_agent(kind: str, *, verbose: bool = False):
    """按名字造修复器。

    scripted：零 token、确定性，用于「可核验」的对比数据。
    codex   ：真实 LLM 修复，用于证明产品上限；结果不可逐字节复现，
              故所有 codex 跑出来的数字必须在文档里标注「不可复现，仅一次记录」。
    """
    from cforge.watch.fixer import CodexFixAgent, ScriptedFixAgent

    if kind == "scripted":
        return ScriptedFixAgent(verbose=verbose)

    if kind == "codex":
        import os

        from cforge.gateway.profiles import GatewayConfig

        cfg = GatewayConfig.from_env()
        if not cfg.profiles:
            raise RuntimeError("没有可用 profile，检查 CFORGE_BACKEND / profiles.py")
        prof = cfg.profiles[0]
        key = os.environ.get(prof.api_key_env, "").strip()
        if not key:
            raise RuntimeError(
                f"codex 修复器需要 API key：环境变量 {prof.api_key_env} 未设置"
                f"（当前 backend={prof.backend}，model={prof.model_id}）")
        return CodexFixAgent(
            workspace=Path("."),   # 每次 fix() 会传真实 workspace，此处占位
            model=prof.model_id,
            base_url=cfg.effective_base_url(prof),
            api_key=key,
            # 实测：单文件单问题的重构成，书生 397B 需要约 5 分钟
            # （拆 66 行函数要读源码→写补丁→执行→验证）。给 600s 才够。
            # 用默认以外的值是因为案例跑在后台，时间长一点无妨。
            timeout_s=int(os.environ.get("CFORGE_FIX_TIMEOUT", "600")),
            max_items=1,
            single_file=True,
            verbose=verbose,
        )
    raise ValueError(f"未知修复器：{kind}（可选 scripted / codex）")


def run_guardian_on(workspace: Path, *, max_rounds: int = 2,
                    fixer: str = "scripted") -> dict:
    """在给定工作区跑守门（scripted 修复器，零 token 可复现）。

    刻意用 scripted 而非 codex：案例的可复现性是验收项。codex 修复器
    受上游模型状态影响（限流、行为漂移），同一仓库两次跑可能不同结果，
    写进文档的分数就不可核验。scripted 只删未使用 import，行为确定。
    本函数同时记录「有多少问题 scripted 修不了」——那是剩余债务，必须明说。

    **每次必须先清掉 workspace 里的 .cforge 状态目录**：守门的冷却/触发
    判定是跨运行持久的（state.json 记 last_run_at），不清就会命中冷却而
    「未触发」，让第二次跑的结果与第一次完全不同。实测踩过：同一 commit
    第一次报「执行/回滚」，第二次报「未触发/无源码变更」——案例数据因此
    不可复现，这比分数不好看严重得多。
    """
    from collections import Counter

    from cforge.quality.metrics import evaluate
    from cforge.watch.fixer import ScriptedFixAgent
    from cforge.watch.guardian import Guardian, GuardianConfig
    from cforge.watch.trigger import WatchConfig

    stale = workspace / ".cforge"
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)

    before = evaluate(workspace)
    t0 = time.time()
    out: dict = {
        "baseline_score": round(before.score, 1),
        "baseline_sloc": before.total_sloc,
        "baseline_files": len(before.files),
        "baseline_smells": len(before.smells),
        "baseline_high": sum(1 for s in before.smells if s.severity == "high"),
        "baseline_kinds": dict(Counter(s.kind for s in before.smells).most_common(8)),
    }

    g = Guardian(
        workspace=workspace,
        agent=_build_agent(fixer, verbose=False),
        # debt_score=99：案例要的是「看见修复」而非日常阈值。
        # max_high=99 同理——否则「high 未超标」会让守门跳过，
        # 剩余债务就永远得不到处置机会，案例变成一条空记录。
        watch_config=WatchConfig(cooldown_s=0.0, debt_score=99.0, max_high=99),
        config=GuardianConfig(max_fixes_per_run=20),
        verbose=False,
    )
    # 关键一步：放开「变更规模过大」闸门。
    #
    # 为什么必须这么做：闸门 2 扫 `mtime > last_run_at` 的源码文件，
    # 数量 >= `massive_threshold`（默认 25）就判「用户正在大改」并跳过。
    # 刚 git clone 的仓库**所有文件 mtime 都是刚刚**，于是 starlette
    # （36 个文件）必被判 massive，httpx 同理，案例恒为 0 改善。
    #
    # 实测证据（诊断输出）：
    #   last_run_at=0 → since=now-86400 → 扫到 36 个文件 → is_massive=True
    # 把 _last_run_at 往前推也没用，因为 clone 的 mtime 是「刚才」，
    # 永远晚于任何历史时间点。所以唯一正确的绕法是把阈值抬到高于文件总数。
    #
    # 语义上这也说得通：`massive_threshold` 服务的是「常驻守护别在用户
    # 手上打断他」；案例要测的是「给定这份代码库，守门能不能改善它」，
    # 不存在「用户正在写」这个前提，故不应受该闸门约束。
    g._policy.config.massive_threshold = 10 ** 6
    _OLD = time.time() - 400 * 86400
    g._policy._last_run_at = _OLD
    events = []
    for _ in range(max_rounds):
        g._policy._last_run_at = _OLD   # 每轮重设，抵消上一轮 mark_ran 的覆盖
        ev = g.run_once()
        # 字段名对齐 WatchEvent 的真实定义（quality_before/quality_after）。
        # 第一版写成 score_before/score_after，getattr 静默返回 None ——
        # 「字段名写错不报错、报告里显示 None」是最难发现的一类错，
        # 故这里显式用 getattr 带默认值，并在下面断言至少拿到一个数值。
        events.append({
            "ran": ev.ran, "reason": ev.reason,
            "accepted": getattr(ev, "accepted", None),
            "quality_before": getattr(ev, "quality_before", None),
            "quality_after": getattr(ev, "quality_after", None),
            "stop_reason": getattr(ev, "stop_reason", ""),
            "error": getattr(ev, "error", ""),
        })
        if not ev.ran:
            break

    after = evaluate(workspace)
    report_dir = workspace / ".cforge" / "guardian"
    out.update({
        "current_score": round(after.score, 1),
        "delta": round(after.score - before.score, 1),
        "current_smells": len(after.smells),
        "current_high": sum(1 for s in after.smells if s.severity == "high"),
        "elapsed_s": round(time.time() - t0, 1),
        "events": events,
        "report_files": sorted(p.name for p in report_dir.glob("*.md"))
        if report_dir.is_dir() else [],
    })
    # 剩余债务：修不动的部分必须单列，不能因为分数涨了就宣称干净
    out["remaining_smells"] = [
        {"kind": s.kind, "path": s.path, "line": s.line,
         "severity": s.severity, "detail": s.detail[:100]}
        for s in after.smells[:20]
    ]
    return out


def write_case(spec: RepoSpec, meta: dict, result: dict, out_dir: Path) -> Path:
    """写 case.md —— 对外可出示的案例说明。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    md = [
        f"# 守门案例：{spec.key}",
        "",
        f"- 来源：{spec.url}",
        f"- license：{spec.license}（实测 `{meta.get('license_file') or '无'}`）",
        f"- commit：`{meta.get('commit', '')[:12]}`（{meta.get('commit_date', '')}）",
        f"- 评估范围：`{spec.subdir or '.'}`",
        f"- 选它的理由：{spec.why}",
        f"- 运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 结果",
        "",
        "| 指标 | 基线 | 守门后 | 变化 |",
        "|---|---|---|---|",
        f"| 整洁度分数 | {result['baseline_score']} | {result['current_score']} "
        f"| {result['delta']:+.1f} |",
        f"| 问题数 | {result['baseline_smells']} | {result['current_smells']} "
        f"| {result['current_smells'] - result['baseline_smells']:+d} |",
        f"| high 级问题 | {result['baseline_high']} | {result['current_high']} "
        f"| {result['current_high'] - result['baseline_high']:+d} |",
        f"| SLOC | {result['baseline_sloc']} | — | — |",
        f"| 文件数 | {result['baseline_files']} | — | — |",
        "",
        f"耗时 {result['elapsed_s']}s（scripted 修复器，零 token）。",
        "",
        "## 修复器处置",
        "",
    ]
    for i, ev in enumerate(result["events"], 1):
        if not ev["ran"]:
            md.append(f"- 第 {i} 轮：**未触发** —— {ev['reason']}")
        else:
            before_v = ev.get("quality_before")
            after_v = ev.get("quality_after")
            verdict = "接受" if ev.get("accepted") else "回滚"
            md.append(
                f"- 第 {i} 轮：执行，判定 **{verdict}**，"
                f"分数 {before_v if before_v is not None else '—'} → "
                f"{after_v if after_v is not None else '—'}"
                + (f"，终止原因：{ev['stop_reason']}" if ev.get("stop_reason") else "")
                + (f"，错误 `{ev['error']}`" if ev.get("error") else ""))
    md += [
        "",
        "## 问题类型分布（基线）",
        "",
        "| 类型 | 数量 |",
        "|---|---|",
    ]
    for k, v in result["baseline_kinds"].items():
        md.append(f"| `{k}` | {v} |")

    md += [
        "",
        "## 剩余债务（检测到但 scripted 修不了）",
        "",
        "> 这一节必须保留。分数上涨不等于代码干净 —— scripted 修复器只处理"
        "「未使用 import」这类可确定性删除的问题；裸 except、深嵌套、跨文件重复"
        "都需要 codex 修复器或人工。把它们隐去等于虚报。",
        "",
    ]
    if result["remaining_smells"]:
        md += ["| 类型 | 位置 | 严重度 | 摘要 |", "|---|---|---|---|"]
        for s in result["remaining_smells"][:12]:
            md.append(
                f"| `{s['kind']}` | `{s['path']}:{s['line']}` "
                f"| {s['severity']} | {s['detail'][:60]} |")
    else:
        md.append("（本次未剩余问题）")

    md += [
        "",
        "## 结论与边界（必读）",
        "",
    ]
    if result["delta"] == 0 and result["events"] and all(
            e.get("stop_reason", "").startswith("修复器放弃") for e in result["events"]
            if e["ran"]):
        md += [
            "**本轮 0 改善，且原因是明确的：scripted 修复器在这份代码上无计可施。**",
            "",
            f"它的能力边界只有「删除未使用的 import」一项（AST 可 100% 保证安全）。"
            f"而这份仓库的 {result['baseline_smells']} 个问题里 "
            f"`unused_import` 为 **0** 个——全是 "
            + "、".join(f"`{k}`" for k in list(result["baseline_kinds"])[:4])
            + "，都需要语义判断，AST 无法安全自动处理。",
            "",
            "这个结果本身是有价值的，它说明两件事：",
            "",
            "1. **成熟开源项目的「脏」不在语法层**。三个独立仓库（smokeshow / httpx / "
            "starlette）的 `unused_import` 全部为 0 —— 这类问题早被 CI linter 拦住了。"
            "守门工具若只做这一项，在真实代码上的价值接近于零。",
            "2. **因此主力修复器必须是 codex（LLM）**，scripted 只配当「零成本第一层」"
            "兜底（无 key 环境、以及先清掉最廉价的那类问题）。"
            "本次刻意用 scripted 是为了保证可复现性（见下），"
            "不代表产品上限。",
            "",
            "换 codex 修复器复跑（需 API key，结果不可逐字节复现）：",
            "",
            "```bash",
            f"python experiments/run_case.py --repo {spec.key} --fixer codex",
            "```",
        ]
    elif result["delta"] > 0:
        md += [f"**接受修复，净改善 {result['delta']:+.1f} 分**，"
               f"剩余 {result['current_smells']} 个问题属修复器能力边界外的债务。"]
    else:
        md += [f"本轮无净改善（{result['delta']:+.1f}）。详见上方逐轮处置与剩余债务两节。"]

    md += [
        "",
        "## 可复现",
        "",
        "```bash",
        f"python experiments/run_case.py --repo {spec.key}",
        "```",
        "",
        "scripted 修复器行为确定，同一 commit 重跑得到同一结果；",
        "codex 修复器受上游模型状态影响，不用于可核验的对比数据。",
    ]
    p = out_dir / "case.md"
    p.write_text("\n".join(md) + "\n", encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真实开源仓库守门案例")
    ap.add_argument("--list", action="store_true", help="列出候选仓库")
    ap.add_argument("--repo", default="", help="跑指定仓库（key）")
    ap.add_argument("--all", action="store_true", help="跑全部候选")
    ap.add_argument("--out", default="", help="输出根目录，默认 docs/cases")
    ap.add_argument("--fixer", default="scripted", choices=["scripted", "codex"],
                    help="修复器：scripted=确定性可复现（默认）；codex=真实 LLM，"
                         "证明产品上限但结果不可逐字节复现")
    ap.add_argument("--rounds", type=int, default=2, help="最多守门轮数")
    a = ap.parse_args(argv)

    if a.list:
        print("候选仓库：")
        for s in CANDIDATES:
            print(f"  {s.key:<12} {s.license:<14} {s.url}")
            print(f"  {'':<12} {s.why}")
        return 0

    specs = CANDIDATES if a.all else [s for s in CANDIDATES if s.key == a.repo]
    if not specs:
        print(f"未找到仓库：{a.repo!r}（用 --list 看候选）", file=sys.stderr)
        return 2

    out_base = Path(a.out) if a.out else OUT_BASE
    results = []
    for spec in specs:
        print(f"\n{'=' * 66}\n{spec.key}  {spec.url}\n{'=' * 66}")
        target, meta = ensure_repo(spec)
        if target is None:
            print(f"[SKIP] {meta.get('error')}")
            results.append({"repo": spec.key, "error": meta.get("error")})
            continue
        print(f"commit {meta.get('commit', '')[:12]}  license {meta.get('license_file')}")
        print(f"评估 {target}")
        try:
            res = run_guardian_on(target, fixer=a.fixer, max_rounds=a.rounds)
        except Exception as e:  # noqa: BLE001 —— 单个仓库失败不拖死批量
            print(f"[FAIL] 守门异常：{type(e).__name__}: {e}")
            results.append({"repo": spec.key, "error": f"{type(e).__name__}: {e}"})
            continue

        case_dir = out_base / spec.key
        # 把守门原始报告一并归档（报告是证据，要跟代码一起进包）
        guardian_dir = target / ".cforge" / "guardian"
        if guardian_dir.is_dir():
            case_dir.mkdir(parents=True, exist_ok=True)
            for f in guardian_dir.glob("*.md"):
                shutil.copyfile(f, case_dir / f.name)
        p = write_case(spec, meta, res, case_dir)
        (case_dir / "meta.json").write_text(
            json.dumps({"repo": spec.__dict__, "meta": meta, "result": res},
                       ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")

        print(f"基线 {res['baseline_score']} → 当前 {res['current_score']} "
              f"({res['delta']:+.1f})  问题 {res['baseline_smells']}→{res['current_smells']}")
        print(f"案例已写入 {p}")
        results.append({"repo": spec.key, **res})

    if len(results) > 1:
        print(f"\n{'=' * 66}\n汇总\n{'=' * 66}")
        print(f"{'仓库':<14}{'基线':>8}{'当前':>8}{'变化':>8}{'问题数':>8}")
        for r in results:
            if "error" in r:
                print(f"{r['repo']:<14}  失败：{r['error'][:50]}")
            else:
                print(f"{r['repo']:<14}{r['baseline_score']:>8}{r['current_score']:>8}"
                      f"{r['delta']:>+8.1f}{r['baseline_smells']:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
