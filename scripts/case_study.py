"""真实案例数据流水线（R5）：守门前后的可对比证据。

产出就是技术文档「实测效果」章节的表格素材：
- case_study.md  人看：每个仓库一段（基线→当前分数、修复明细、剩余债务）
- case_study.json 机器读：聚合数字，供看板/引用

数据分级（Q5 授权问题的设计回应）：
- 自有项目默认 `--aggregate-only`：只输出分数与计数，不贴源码片段
- 公开开源仓库可全量输出（须注明来源与 license）

被否方案：临时目录造假项目跑分。拒绝理由——那是「设计意图」不是
「实测证据」，实用性 40% 要的是真实代码库上的前后对比。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 允许从仓库根直接跑：python scripts/case_study.py ...
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass
class CaseResult:
    """一个仓库的守门案例。"""

    repo: str
    origin: str                        # "self-owned" | "public: <license>"
    before_score: float
    after_score: float | None          # 修复器没跑成功时为 None（如实记录）
    n_smells_before: int
    n_smells_after: int | None
    high_before: int
    high_after: int | None
    clone_pairs_before: int
    clone_pairs_after: int | None
    accepted: bool = False
    rolled_back: bool = False
    report_path: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "origin": self.origin,
            "before_score": self.before_score,
            "after_score": self.after_score,
            "delta": None if self.after_score is None else round(
                self.after_score - self.before_score, 2),
            "n_smells_before": self.n_smells_before,
            "n_smells_after": self.n_smells_after,
            "high_before": self.high_before,
            "high_after": self.high_after,
            "clone_pairs_before": self.clone_pairs_before,
            "clone_pairs_after": self.clone_pairs_after,
            "accepted": self.accepted,
            "rolled_back": self.rolled_back,
            "report_path": self.report_path,
            "notes": self.notes,
        }


def _snapshot(path: Path) -> dict:
    from cforge.quality.clones import scan as scan_clones
    from cforge.quality.metrics import evaluate

    report = evaluate(path)
    clones = scan_clones(path)
    return {
        "score": report.score,
        "n_smells": len(report.smells),
        "high": len(report.by_severity("high")),
        "clone_pairs": len(clones.pairs),
    }


def run_case(repo: Path, origin: str, *, max_cycles: int = 1) -> CaseResult:
    """对一个真实仓库跑「基线 → 守门 → 复评」。

    用 scripted 修复器（AST 确定性，零 token）——案例数据的意义在
    「无人值守守门真的能提升真实代码库的质量」，LLM 修复会引入
    模型随机性，让数据不可复现。重复代码类债务如实记为剩余债务。
    """
    from cforge.watch import Guardian, GuardianConfig, ScriptedFixAgent, WatchConfig

    before = _snapshot(repo)

    guardian = Guardian(
        workspace=repo,
        agent=ScriptedFixAgent(),
        watch_config=WatchConfig(cooldown_s=0.0, debt_score=99.0),
        config=GuardianConfig(),
        verbose=False,
    )
    out = guardian.run_forever(max_cycles=max_cycles)
    report_path = out.get("report", "")
    ran = [e for e in out["events"] if e["ran"]]

    after = _snapshot(repo)
    accepted = any(e.get("accepted") for e in ran)
    # 净改善门拒绝 = 回滚；此时 after 应与 before 相同（如实呈现）
    rolled_back = bool(ran) and not accepted

    return CaseResult(
        repo=str(repo),
        origin=origin,
        before_score=before["score"],
        after_score=after["score"],
        n_smells_before=before["n_smells"],
        n_smells_after=after["n_smells"],
        high_before=before["high"],
        high_after=after["high"],
        clone_pairs_before=before["clone_pairs"],
        clone_pairs_after=after["clone_pairs"],
        accepted=accepted,
        rolled_back=rolled_back,
        report_path=str(report_path),
    )


def render_md(results: list[CaseResult], *, aggregate_only: bool) -> str:
    lines = [
        "# CodexForge 守门案例：真实代码库前后对比",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 修复器：scripted（AST 确定性，零 token）——保证数据可复现",
        "- 净改善门：分数下降 / 改善<0.5 / 新增 high → 全部回滚",
        "",
        "| 仓库 | 来源 | 基线分 | 守门后 | 变化 | high 前→后 | 重复对 前→后 | 结果 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        delta = "—" if r.after_score is None else f"{r.after_score - r.before_score:+.1f}"
        verdict = "接受" if r.accepted else ("回滚（净改善门拦截）" if r.rolled_back else "未触发")
        lines.append(
            f"| `{Path(r.repo).name}` | {r.origin} | {r.before_score:.1f} "
            f"| {'—' if r.after_score is None else f'{r.after_score:.1f}'} | {delta} "
            f"| {r.high_before}→{'—' if r.high_after is None else r.high_after} "
            f"| {r.clone_pairs_before}→{'—' if r.clone_pairs_after is None else r.clone_pairs_after} "
            f"| {verdict} |"
        )
    lines += [
        "",
        "## 读数说明",
        "",
        "- 分数只对比本次守门会话内真实跑过的前后值（跨会话历史基线"
        "对比会造成「跳过却显示质量下降」的误导，实测踩过）。",
        "- 重复对是全局指标，scripted 修复器处理不了——如实保留为剩余债务，",
        "  处理路径见技能 `clone-refactor`（codex 修复器或人工）。",
    ]
    if aggregate_only:
        lines += [
            "",
            "> 隐私分级：本次数据为聚合数字（分数/计数），不包含源码片段。",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="守门案例数据流水线（真实代码库前后对比）")
    ap.add_argument("repos", nargs="+", help="仓库/目录列表（≥2 个才有说服力）")
    ap.add_argument("--origin", default="", help="逗号分隔的来源标注，与 repos 对齐"
                    "（如 self-owned,public:MIT）")
    ap.add_argument("--out", default="", help="输出目录，默认 experiments/results/")
    ap.add_argument("--max-cycles", type=int, default=1)
    args = ap.parse_args(argv)

    origins = [s.strip() for s in args.origin.split(",")] if args.origin else []
    results: list[CaseResult] = []
    for i, raw in enumerate(args.repos):
        repo = Path(raw).resolve()
        if not repo.is_dir():
            print(f"[SKIP] 目录不存在：{repo}")
            continue
        origin = origins[i] if i < len(origins) else "self-owned"
        print(f"[case] {repo.name}（{origin}）…", flush=True)
        r = run_case(repo, origin, max_cycles=args.max_cycles)
        results.append(r)
        delta = "—" if r.after_score is None else f"{r.after_score - r.before_score:+.1f}"
        print(f"       {r.before_score:.1f} → {delta}（{r.repo}）")

    if not results:
        print("没有可用的案例。")
        return 2

    out_dir = Path(args.out) if args.out else Path("experiments/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    aggregate_only = all("public" not in r.origin for r in results)

    md_path = out_dir / f"case_study-{ts}.md"
    json_path = out_dir / f"case_study-{ts}.json"
    md_path.write_text(render_md(results, aggregate_only=aggregate_only), encoding="utf-8")
    json_path.write_text(json.dumps(
        [r.as_dict() for r in results], ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n已落盘：\n  {md_path}\n  {json_path}")
    print("技术文档「实测效果」章节直接引用本文件的表格。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
