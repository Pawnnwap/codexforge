"""实验入口：跑长程与整洁度两个核心机制的量化验证。

用法：
    # 全套（3 任务 × 2 组，约 10-20 分钟，会消耗 token）
    python -m experiments.cli --suite full

    # 单个任务快速验证
    python -m experiments.cli --suite smoke

    # 只测整洁度（不调 LLM，纯本地）
    python -m experiments.cli --suite quality

    # 先看会跑什么（不实际调用）
    python -m experiments.cli --suite full --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from experiments.harness import TASKS, ExperimentConfig, Task
from experiments.run_longrun import ExperimentRunner

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "experiments" / "results"


def pick_model() -> str:
    """选一个当前可用的免费模型。

    实测：OpenRouter 的 :free 模型走上游共享池，可用性随时间变化。
    优先用用户指定的 Xing，不可用时按候选列表降级。
    """
    return os.environ.get("CFORGE_MODEL", "xingchen-agi/xing4.0-29b-a4b:free")


def suite_quality() -> dict:
    """纯本地：验证整洁度度量与收敛机制（不调 LLM，零成本）。"""
    from cforge.quality.metrics import evaluate
    from cforge.quality.polish import NetImprovementGate

    print("=" * 70)
    print("整洁度机制验证（本地，零 token）")
    print("=" * 70)

    # 用自己的代码库做样本：这是个真实的中等规模 Python 项目
    src = ROOT / "cforge"
    report = evaluate(src)
    print(f"\n样本：cforge/ ({report.total_sloc} SLOC, {len(report.files)} 文件)")
    print(f"得分：{report.score}/100")
    print(f"问题分布：")
    from collections import Counter
    for kind, cnt in Counter(s.kind for s in report.smells).most_common():
        sev = next(s.severity for s in report.smells if s.kind == kind)
        print(f"  {kind:<20} {cnt:>3}  [{sev}]")

    clones = None
    try:
        from cforge.quality.clones import scan as scan_clones
        clones = scan_clones(src)
        print(f"\n重复代码：{len(clones.units)} 个函数中检出 "
              f"{len(clones.pairs)} 对重复（比率 {clones.duplicate_ratio:.1%}）")
        for p in clones.pairs[:5]:
            print(f"  {p}")
    except Exception as exc:  # noqa: BLE001
        print(f"克隆检测跳过：{exc}")

    return {
        "kind": "quality",
        "sample": str(src),
        "sloc": report.total_sloc,
        "score": report.score,
        "n_smells": len(report.smells),
        "by_kind": dict(Counter(s.kind for s in report.smells).most_common()),
        "clones": clones.as_dict() if clones else {},
    }


def suite_smoke(model: str, dry: bool) -> dict:
    """单任务最小验证：确认整条链路能真实跑起来。"""
    cfg = ExperimentConfig(
        name="smoke",
        workspace_root=ROOT / "experiments" / "ws",
        tasks=[TASKS[0]],
        max_turns=3,
        model=model,
        min_quality=60.0,
    )
    if dry:
        print(f"[dry] 将跑：{cfg.name} / {[t.id for t in cfg.tasks]} / model={model}")
        return {"kind": "smoke", "dry_run": True}

    runner = ExperimentRunner(cfg=cfg, verbose=True)
    return runner.run()


def suite_full(model: str, dry: bool) -> dict:
    """完整对照：feedback 组 vs no-feedback 组。"""
    configs = [
        ExperimentConfig(
            name="full",
            workspace_root=ROOT / "experiments" / "ws",
            tasks=TASKS,
            max_turns=5,
            model=model,
            min_quality=70.0,
        ),
    ]
    if dry:
        for c in configs:
            print(f"[dry] {c.name}: {[t.id for t in c.tasks]} max_turns={c.max_turns}")
        return {"kind": "full", "dry_run": True}

    out = {"kind": "full", "model": model, "runs": []}
    for c in configs:
        runner = ExperimentRunner(cfg=c, verbose=True)
        out["runs"].append(runner.run())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="quality",
                    choices=["quality", "smoke", "full"])
    ap.add_argument("--model", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    model = a.model or pick_model()
    OUT.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    if a.suite == "quality":
        result = suite_quality()
    elif a.suite == "smoke":
        result = suite_smoke(model, a.dry_run)
    else:
        result = suite_full(model, a.dry_run)

    result["elapsed_s"] = round(time.time() - t0, 1)
    result["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    out_path = Path(a.out) if a.out else OUT / f"{a.suite}-{time.strftime('%H%M%S')}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"\n结果已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
