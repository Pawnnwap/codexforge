"""实验脚手架：用真实 codex + 真实 LLM 跑长程任务，量化两个核心机制的价值。

## 实验设计（这是能写进参赛文档的核心证据）

三组对照，同一任务、同一模型：

| 组 | 反馈 | 状态外置 | 整洁度门 |
|---|---|---|---|
| baseline | ✗ | ✗ | ✗ | 纯 codex，单轮
| feedback | ✓ | ✗ | ✗ | 多轮 + 目标反馈
| full | ✓ | ✓ | ✓ | 多轮 + 反馈 + 长程状态 + 整洁度

指标：
- 达成率：目标是否完成
- 轮数：用了多少轮
- 整洁度：最终分数与 delta
- 重复劳动率：重复尝试失败方向的比例（衡量"绕圈"）

## 任务集设计原则

任务必须满足：
1. 单轮做不完（否则测不出长程价值）
2. 有确定性验收（文件/测试）
3. 会诱导「先写通再写干净」的坏习惯

## 为什么不用公开 benchmark

公开 benchmark 的任务分布与本项目定位（general coding agent，
强调长程与整洁）不匹配，且无法控制「必然需要多轮」这一条件。
自建任务集反而更严谨 —— 每个任务的「最短必然轮数」可论证。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    """一个实验任务。"""

    id: str
    name: str
    prompt: str
    # 确定性验收：给定工作区返回 (是否通过, 说明)
    check: str          # file_exists | tests_pass | all_files_exist
    params: dict = field(default_factory=dict)
    # 论证「为什么必须多轮」
    why_multi_turn: str = ""

    def verify(self, workspace: Path) -> tuple[bool, list[str]]:
        """确定性验收。返回 (是否通过, 未达成项列表)。

        ## 设计原则：宁可判严，不可判松

        「文件存在」不等于「任务完成」。实测真实案例：agent 创建了 4 个文件
        且内容逻辑完全正确，但全部是 UTF-16 编码 —— `file_exists` 判通过，
        实际代码一行都跑不了。这就是典型的「伪完成」。

        因此默认对所有 .py 产物做**可解析性检查**，除非显式关闭。
        """
        if self.check == "file_exists":
            p = workspace / self.params["path"]
            if not p.is_file():
                return False, [f"{self.params['path']} 不存在"]
            if p.suffix == ".py" and self.params.get("parseable", True):
                ok, why = _check_parseable(p)
                if not ok:
                    return False, [f"{p.name} 无法解析：{why}"]
            return True, []

        if self.check == "all_files_exist":
            files = self.params["files"]
            missing = [f for f in files if not (workspace / f).is_file()]
            if missing:
                return False, [f"缺失：{m}" for m in missing]
            # 全部 .py 必须可解析
            if self.params.get("parseable", True):
                bad = []
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    ok, why = _check_parseable(workspace / f)
                    if not ok:
                        bad.append(f"{f} 无法解析：{why}")
                if bad:
                    return False, bad
            return True, []

        if self.check == "tests_pass":
            import subprocess
            try:
                r = subprocess.run(
                    self.params.get("cmd", "python -m pytest -q"),
                    shell=True, cwd=str(workspace), capture_output=True,
                    text=True, timeout=180,
                )
                if r.returncode == 0:
                    return True, []
                tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
                return False, [f"测试失败：{ln}" for ln in tail[-4:]]
            except Exception as exc:  # noqa: BLE001
                return False, [f"测试执行异常：{exc}"]

        return False, [f"未知 check: {self.check}"]


def _check_parseable(path: Path) -> tuple[bool, str]:
    """检查 Python 文件能否被 ast 解析。编码问题单独报告。"""
    import ast

    try:
        raw = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return False, f"读取失败：{exc}"

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False, "UTF-16 编码（含 BOM），需转为 UTF-8"
    if len(raw) > 20 and raw.count(b"\x00") > len(raw) * 0.2:
        return False, "疑似 UTF-16 编码（大量 NUL 字节）"

    try:
        ast.parse(raw.decode("utf-8", errors="replace"))
    except SyntaxError as exc:
        return False, f"行 {exc.lineno}: {exc.msg}"
    return True, ""


# --------------------------------------------------------------------------- #
# 任务集
# --------------------------------------------------------------------------- #
TASKS: list[Task] = [
    Task(
        id="t1-multifile",
        name="多文件小工具",
        prompt=(
            "在 workspace 里创建一个 Python 包 calc，包含三个模块："
            "add.py（加）、sub.py（减）、__init__.py（导出两者）。"
            "每个模块一个函数，带类型注解和 docstring。"
            "再写一个 test_calc.py 用 unittest 测试三个函数。"
        ),
        check="all_files_exist",
        params={"files": ["calc/__init__.py", "calc/add.py", "calc/sub.py",
                          "test_calc.py"]},
        why_multi_turn="4 个文件 + 测试，单轮容易漏文件或忘 __init__ 导出",
    ),
    Task(
        id="t2-refactor",
        name="先写散再重构",
        prompt=(
            "在 workspace 创建 report.py，里面写三个函数分别处理不同格式的"
            "数据（csv 风格字符串、dict、list），每个函数都把结果拼成文本。"
            "然后：如果发现三个函数有重复逻辑，把它们重构成一个共用函数。"
        ),
        check="file_exists",
        params={"path": "report.py"},
        why_multi_turn="要求先实现再自查重复，天然需要两轮（写 + 重构）",
    ),
    Task(
        id="t3-tests",
        name="带测试通过的实现",
        prompt=(
            "在 workspace 创建 fib.py，实现 fibonacci(n) 迭代版本。"
            "再创建 test_fib.py，测试 fib(0)=0, fib(1)=1, fib(10)=55。"
            "确保测试能通过。"
        ),
        check="tests_pass",
        params={"cmd": "python -m pytest test_fib.py -q"},
        why_multi_turn="写实现 + 写测试 + 让测试通过，常需修错一轮",
    ),
]


@dataclass
class ExperimentConfig:
    """一次实验的配置。"""

    name: str
    workspace_root: Path
    tasks: list[Task] = field(default_factory=lambda: list(TASKS))
    # 是否启用多轮反馈
    use_feedback: bool = True
    # 是否启用长程状态外置
    use_state: bool = True
    # 整洁度门的最低分（None = 不启用）
    min_quality: float | None = 75.0
    max_turns: int = 6
    model: str = ""
    backend: str = "openrouter"


def prepare_workspace(cfg: ExperimentConfig, task: Task) -> Path:
    """每个任务一个干净工作区。"""
    ws = Path(cfg.workspace_root) / cfg.name / task.id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True, exist_ok=True)
    # 让 codex 认为这里是 git 仓库，避免它反复检查
    (ws / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    return ws


def summarize_result(result_dict: dict) -> dict:
    """从 LongRunResult.as_dict() 提取关键指标。"""
    history = result_dict.get("history", [])
    quality_trend = [h["feedback"]["quality_score"] for h in history]
    return {
        "turns": result_dict.get("turns", 0),
        "goal_met": result_dict.get("goal_met", False),
        "stop_reason": result_dict.get("stop_reason", ""),
        "quality_initial": result_dict.get("initial_quality", 0),
        "quality_final": result_dict.get("final_quality", 0),
        "quality_delta": result_dict.get("quality_delta", 0),
        "quality_trend": quality_trend,
        "stagnation_hits": sum(
            1 for h in history if h["feedback"]["stagnation_level"] > 0
        ),
        "elapsed_s": result_dict.get("elapsed_s", 0),
    }
