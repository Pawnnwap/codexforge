"""MCP 工具层：4 个工具的 schema 与 handler。

设计铁律——**评分真源唯一**：
所有 handler 都是 `quality/` 与 `watch/` 现有函数的薄封装，
不重新实现任何指标。这保证「MCP 看到的分数 = CLI 报告里的分数」，
不会出现两套评分漂移。工具输出 JSON 与守门报告 JSON 同构。

工具内异常一律转成 `{"isError": true, ...}` 结果返回，不断链——
MCP 客户端能继续发下一个请求，且错误原因完整可达。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ToolDef:
    """一个 MCP 工具的定义。schema 手写（标准库 json 只校验必填项）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def as_mcp_def(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# --------------------------------------------------------------------------- #
# handler：全部薄封装
# --------------------------------------------------------------------------- #
def _require_path(params: dict[str, Any]) -> Path:
    p = params.get("path") or params.get("workspace") or "."
    path = Path(p).expanduser()
    if not path.is_dir():
        raise ValueError(f"目录不存在：{path}")
    return path.resolve()


def h_quality_score(params: dict[str, Any]) -> dict[str, Any]:
    """整洁度评分。调 quality.evaluate()——唯一的评分真源。"""
    from cforge.quality.metrics import evaluate

    path = _require_path(params)
    include_clones = bool(params.get("include_clones", True))
    report = evaluate(path)
    out: dict[str, Any] = {
        "path": str(path),
        "score": round(report.score, 2),
        "n_files": len(report.files),
        "total_sloc": report.total_sloc,
        "n_smells": len(report.smells),
        "by_severity": {
            "high": len(report.by_severity("high")),
            "medium": len(report.by_severity("medium")),
            "low": len(report.by_severity("low")),
        },
    }
    if include_clones:
        from cforge.quality.clones import scan as scan_clones

        clones = scan_clones(path)
        out["duplicate_ratio"] = round(clones.duplicate_ratio, 3)
        out["n_clone_pairs"] = len(clones.pairs)
    if params.get("verbose") and report.smells:
        out["actionable"] = report.actionable_text(limit=30)
    return out


def h_clone_report(params: dict[str, Any]) -> dict[str, Any]:
    """跨文件重复检测。阈值用实测校准值（min_statements=4 + similarity=0.90）。"""
    from cforge.quality.clones import scan as scan_clones

    path = _require_path(params)
    clones = scan_clones(path)
    out = clones.as_dict()
    out["path"] = str(path)
    return out


def h_guardian_report(params: dict[str, Any]) -> dict[str, Any]:
    """读守门报告。与 watch/guardian.py 的报告 JSON 同构。

    报告结构：`{workspace}/.cforge/guardian/latest.md`（人看，JSON 嵌在
    markdown 的 json 代码块）+ `state.json`（机器读）。两者都读，
    任一缺失都是「还没跑过守门」的明确状态，不是错误。
    """
    ws = _require_path(params)
    gdir = ws / ".cforge" / "guardian"
    latest = gdir / "latest.md"
    state = gdir / "state.json"
    if not latest.is_file() and not state.is_file():
        return {
            "path": str(ws),
            "status": "no-report",
            "hint": "该工作区还没有守门报告。先跑：python -m cforge.cli guard "
                    f"--workspace {ws} --fixer scripted --max-cycles 1",
        }
    out: dict[str, Any] = {"path": str(ws), "status": "ok"}
    if state.is_file():
        try:
            out["state"] = json.loads(state.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            out["state_error"] = str(e)
    if latest.is_file():
        text = latest.read_text(encoding="utf-8")
        out["report_md"] = text
        # 抽出嵌在 markdown 里的 JSON（机器可读部分）
        if "```json" in text:
            block = text.split("```json", 1)[1].split("```", 1)[0]
            try:
                out["report_json"] = json.loads(block)
            except json.JSONDecodeError:
                out["report_json_error"] = "latest.md 内嵌 JSON 解析失败"
    return out


def h_gate_status(params: dict[str, Any]) -> dict[str, Any]:
    """四道闸门判定 + 人看理由。黑盒守护进程没人敢用，所以判定必须可解释。"""
    from cforge.watch.trigger import TriggerPolicy, WatchConfig

    ws = _require_path(params)
    cfg = WatchConfig()
    if params.get("cooldown_s") is not None:
        cfg.cooldown_s = float(params["cooldown_s"])
    if params.get("debt_score") is not None:
        cfg.debt_score = float(params["debt_score"])
    policy = TriggerPolicy(ws, cfg)
    decision = policy.decide()
    out = decision.as_dict()
    out["path"] = str(ws)
    out["explain"] = (
        "should_run=false 时 reason 即「为什么没动手」；"
        "gates 是四道闸门（冷却/变更/冲突/债务）各自的判定依据。"
    )
    return out


# --------------------------------------------------------------------------- #
# 工具注册表
# --------------------------------------------------------------------------- #
TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        name="quality_score",
        description=(
            "评估一个代码目录的整洁度（0-100）并给出问题分布。"
            "确定性 AST 度量，不调 LLM。评分与 CLI `cforge quality` 完全一致。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要评估的目录"},
                "include_clones": {"type": "boolean", "default": True,
                                   "description": "是否同时做跨文件重复检测"},
                "verbose": {"type": "boolean", "default": False,
                            "description": "是否附可执行反馈文本"},
            },
            "required": ["path"],
        },
        handler=h_quality_score,
    ),
    ToolDef(
        name="clone_report",
        description=(
            "检测跨文件重复函数（AST 指纹 + SequenceMatcher，"
            "阈值经真实代码校准：min_statements=4, similarity=0.90）。"
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "要扫描的目录"}},
            "required": ["path"],
        },
        handler=h_clone_report,
    ),
    ToolDef(
        name="guardian_report",
        description=(
            "读取指定工作区的守门报告（质量变化、接受/回滚、剩余债务）。"
            "没有报告时返回明确状态与补救命令，不报错。"
        ),
        input_schema={
            "type": "object",
            "properties": {"workspace": {"type": "string", "description": "工作区目录"}},
            "required": ["workspace"],
        },
        handler=h_guardian_report,
    ),
    ToolDef(
        name="gate_status",
        description=(
            "查询常驻守门的四道闸门当前判定：会不会跑、为什么。"
            "每个判定都带人可读的理由。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "工作区目录"},
                "cooldown_s": {"type": "number", "description": "覆盖冷却秒数"},
                "debt_score": {"type": "number", "description": "覆盖债务阈值"},
            },
            "required": ["workspace"],
        },
        handler=h_gate_status,
    ),
)


def tool_schemas() -> list[dict[str, Any]]:
    return [t.as_mcp_def() for t in TOOL_DEFS]


def call_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """按名调工具。未知工具 / 参数缺必需项 / handler 异常，
    三种失败都转成 MCP 结果对象（isError=true），不抛给传输层。"""
    tool = next((t for t in TOOL_DEFS if t.name == name), None)
    if tool is None:
        return {"isError": True, "content": [{"type": "text", "text": f"未知工具：{name}"}]}
    missing = [k for k in tool.input_schema.get("required", []) if k not in params]
    if missing:
        return {"isError": True, "content": [{"type": "text",
                "text": f"缺少必需参数：{', '.join(missing)}"}]}
    try:
        result = tool.handler(params)
    except Exception as e:  # noqa: BLE001 —— 工具内异常必须转结果不断链
        return {"isError": True, "content": [{"type": "text",
                "text": f"{type(e).__name__}: {e}"}]}
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
        "structuredContent": result,
    }
