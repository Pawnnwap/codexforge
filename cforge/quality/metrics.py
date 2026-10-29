"""代码整洁度度量：纯 AST 的确定性指标。

## 为什么自己写而不用 ruff/pylint

赛道评分看的是「可复现性」。第三方 linter 的版本漂移会让同一份代码
在不同机器上给出不同分数。而整洁度如果作为验收门（gate），必须是
**确定性且可逐项解释**的 —— 所以这里只用 `ast`，规则自定、版本无关。

外部 linter 仍然保留（`checks/gates.py` 调 codex-edit-gates），
但那属于「建议层」；本模块属于「判定层」。

## 指标设计原则

每个指标都满足三条：
1. **可计算**：给定源码必得同一数值，无随机、无环境依赖
2. **可归因**：能定位到具体文件+行号，agent 才知道改哪里
3. **有阈值**：超过阈值才算问题，避免「完美主义瘫痪」

## 刻意不做的指标

- 圈复杂度：对 LLM 生成的代码区分度低（它天然爱写长函数），且阈值难定
- 命名风格：主观，且与整洁度的相关性弱
- 注释率：**高注释率不等于干净**，反而常是代码不自解释的信号
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Smell:
    """一处整洁度问题。必须能定位到文件+行，否则 agent 无从下手。"""

    kind: str
    path: str
    line: int
    detail: str
    # 严重度：high 直接判不合格，medium 累计计分，low 仅提示
    severity: str = "medium"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "path": self.path, "line": self.line,
            "detail": self.detail, "severity": self.severity,
            "repair_tier": self.repair_tier,
        }

    @property
    def repair_tier(self) -> str:
        """修复分级：auto（可证安全）/ suggest（需判断）/ advisory（仅记录）。"""
        from cforge.quality.repair import repair_tier

        return repair_tier(self.kind)

    @property
    def blocking(self) -> bool:
        from cforge.quality.repair import is_blocking

        return is_blocking(self.kind)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.path}:{self.line} {self.kind} — {self.detail}"


def detect_encoding_problem(raw: bytes) -> tuple[str, str]:
    """检测编码问题。返回 (问题类型, 说明)，无问题返回 ("", "")。

    ## 为什么这条必须单独检查（实测踩过）

    agent 在 Windows 上写文件时可能产出 **UTF-16 编码**（带 `\\xff\\xfe` BOM）。
    内容逻辑完全正确，但 Python 解释器按 UTF-8 读会直接报 SyntaxError：

        `utf-16 content` → `SyntaxError: invalid syntax`

    若只报「语法错」，agent 会去改逻辑，而根本没找对病因。
    实测真实案例：4 个文件全部 UTF-16，内容完全正确，却被判 4 个语法错。
    """
    if raw.startswith(b"\xff\xfe"):
        return "encoding_utf16", "文件是 UTF-16 LE 编码（带 BOM），Python 无法直接解析"
    if raw.startswith(b"\xfe\xff"):
        return "encoding_utf16", "文件是 UTF-16 BE 编码（带 BOM），Python 无法直接解析"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "encoding_bom", "文件带 UTF-8 BOM，部分工具会报错"
    # 大量 NUL 字节也是 UTF-16 的强信号（ASCII 字符高字节为 0）
    if len(raw) > 20 and raw.count(b"\x00") > len(raw) * 0.2:
        return "encoding_utf16", "文件含大量 NUL 字节，疑似 UTF-16 编码但缺 BOM"
    return "", ""


@dataclass
class FileMetrics:
    """单文件的原始度量。"""

    path: str
    sloc: int = 0              # 有效代码行（去空行、去纯注释）
    total_lines: int = 0
    n_functions: int = 0
    n_classes: int = 0
    max_func_len: int = 0
    max_func_name: str = ""
    max_nesting: int = 0
    n_imports: int = 0
    n_unused_imports: int = 0
    n_todo: int = 0
    n_bare_except: int = 0
    n_long_lines: int = 0
    n_dead_assign: int = 0
    n_dup_branches: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class QualityReport:
    """一次整洁度评估的完整结果。"""

    files: list[FileMetrics] = field(default_factory=list)
    smells: list[Smell] = field(default_factory=list)
    # 0-100，越高越干净
    score: float = 100.0
    # 跨文件重复率（0-1）。单独存放，因为它不是「单点 smell」而是全局指标。
    # 实测教训：早期版本没把重复率纳入评分，导致「100 分但两个函数一模一样」，
    # 而这恰恰是技术债里最该修的一类。
    duplicate_ratio: float = 0.0
    clone_pairs: int = 0

    @property
    def total_sloc(self) -> int:
        return sum(f.sloc for f in self.files)

    def by_severity(self, sev: str) -> list[Smell]:
        return [s for s in self.smells if s.severity == sev]

    def actionable_text(self, limit: int = 12, *, workspace: Path | None = None,
                        target_delta: float = 0.5) -> str:
        """给 agent 的可执行反馈。按严重度排序，限制条数避免刷屏。

        ## 补上 ACI 闭环（来自 SWE-agent 的教训）

        SWE-agent 把「给智能体的接口」当独立设计对象：只报问题不够，
        还要告诉它**改完怎么自检**，否则模型不知道自己的改动算不算成功，
        容易反复微调或虚假宣布完成。

        所以末尾固定给出自检命令与目标分数：分数是确定性的，
        模型可以自己跑一次判断有没有达到目标，而不是靠猜。

        ## 每条标注修复分级

        `[可自动]` 的项有确定性修复器能处理；`[需判断]` 必须由人/模型决定；
        `[仅记录]` 不该产生改动。这样 agent 不会把 TODO 标记也当成待修问题。
        """
        if not self.smells and self.clone_pairs == 0:
            return ""
        from cforge.quality.repair import TIER_LABEL, summarize

        order = {"high": 0, "medium": 1, "low": 2}
        ranked = sorted(self.smells, key=lambda s: (order.get(s.severity, 9), s.path, s.line))
        tally = summarize([s.kind for s in self.smells])
        head = (f"整洁度 {self.score:.0f}/100，发现 {len(self.smells)} 处问题"
                f"（可自动 {tally['auto']}｜需判断 {tally['suggest']}"
                f"｜仅记录 {tally['advisory']}）：")
        lines = [head]
        for s in ranked[:limit]:
            lines.append(f"  [{TIER_LABEL.get(s.repair_tier, s.repair_tier)}] {s}")
        if self.clone_pairs:
            lines.append(
                f"  另有 {self.clone_pairs} 对跨文件重复函数"
                f"（重复率 {self.duplicate_ratio:.1%}）"
            )
        if len(ranked) > limit:
            lines.append(f"  …另有 {len(ranked) - limit} 处")

        target = min(100.0, round(self.score + target_delta, 1))
        where = str(workspace) if workspace is not None else "<工作区>"
        lines.append(f"改完自检：python -m cforge.cli quality --path {where}"
                     f"（目标 ≥ {target:.0f}）")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 单文件分析
# --------------------------------------------------------------------------- #
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b", re.IGNORECASE)


def _count_sloc(source: str) -> tuple[int, int]:
    """返回 (有效行数, 总行数)。有效行 = 非空且非纯注释。"""
    lines = source.splitlines()
    total = len(lines)
    sloc = 0
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            sloc += 1
    return sloc, total


class _NestingVisitor(ast.NodeVisitor):
    """统计最大嵌套深度（只算控制流，不算函数/类定义本身）。"""

    CONTROL = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)

    def __init__(self) -> None:
        self.max_depth = 0
        self._depth = 0

    def _enter(self, node: ast.AST) -> None:
        self._depth += 1
        self.max_depth = max(self.max_depth, self._depth)
        self.generic_visit(node)
        self._depth -= 1

    def visit_If(self, node): self._enter(node)          # noqa: N802
    def visit_For(self, node): self._enter(node)          # noqa: N802
    def visit_While(self, node): self._enter(node)        # noqa: N802
    def visit_With(self, node): self._enter(node)         # noqa: N802
    def visit_Try(self, node): self._enter(node)          # noqa: N802
    def visit_AsyncFor(self, node): self._enter(node)     # noqa: N802
    def visit_AsyncWith(self, node): self._enter(node)    # noqa: N802


# 结构性 import：不绑定运行时名字，永远不该被报为「未使用」
#   __future__.*      —— 编译期行为开关
#   typing.TYPE_CHECKING 块内的 import 由 _used_names 的字符串扫描兜住
_STRUCTURAL_MODULES = ("__future__",)


class _ImportCollector(ast.NodeVisitor):
    """收集 import 的绑定名 -> [(模块名, 行号), ...]，以及结构性命中。

    ## 为什么用 list 而不是 dict（实测踩过）

    初版用 `dict[str, tuple[str, int]]`，重复 import 同一模块时后者覆盖前者：

        import os      <- 行 1，被覆盖
        import os      <- 行 2，被覆盖
        ...
        import os      <- 行 12，只报这一行

    结果 12 个问题只报 1 个，密度算出来 97.3 分（实际该更低），
    而且 agent 只看到一处，改完还剩 11 处。必须按行全量记录。
    """

    def __init__(self) -> None:
        self.bindings: dict[str, list[tuple[str, int]]] = {}
        self.structural: set[str] = set()

    def _add(self, name: str, module: str, lineno: int) -> None:
        self.bindings.setdefault(name, []).append((module, lineno))
        if module.split(".")[0] in _STRUCTURAL_MODULES:
            self.structural.add(name)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._add(name, alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self._add(name, f"{module}.{alias.name}", node.lineno)


def _used_names(tree: ast.AST) -> set[str]:
    """所有被读取的标识符。用于判断 import 是否真的被用到。"""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            # a.b 里的 a 会由 Name 覆盖；这里补上 attribute 链的根
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    # 字符串里有引用的（如 __all__、类型注解字符串）也算用过
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                used.add(tok)
    return used


def _find_bare_except(tree: ast.AST) -> list[int]:
    """裸 except（不带异常类型）会吞掉 KeyboardInterrupt 等，是真实缺陷。"""
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]


def _find_dead_assign(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    """函数内赋值后从未读取的局部变量。

    只报「赋值了但整个函数体内再没出现该名字」的情况，
    避免误伤 `_ = f()` 这类惯例写法与循环变量。
    """
    assigned: dict[str, int] = {}
    read: set[str] = set()

    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id != "_":
                    assigned.setdefault(target.id, node.lineno)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                if node.target.id != "_":
                    assigned.setdefault(node.target.id, node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read.add(node.id)

    return [line for name, line in assigned.items() if name not in read]


def _find_dup_branches(tree: ast.AST) -> list[int]:
    """if/elif 分支体完全相同 —— 说明条件写错或可合并。"""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        bodies = [node.body]
        cur = node.orelse
        while len(cur) == 1 and isinstance(cur[0], ast.If):
            bodies.append(cur[0].body)
            cur = cur[0].orelse
        if cur:
            bodies.append(cur)
        dumps = [ast.dump(ast.Module(body=b, type_ignores=[])) for b in bodies]
        if len(dumps) != len(set(dumps)) and len(dumps) > 1:
            hits.append(node.lineno)
    return hits


def analyze_source(source: str, path: str = "<memory>", *,
                   max_func_len: int = 60, max_nesting: int = 4,
                   max_line_len: int = 120) -> tuple[FileMetrics, list[Smell]]:
    """分析一段源码。语法错误不抛异常，转成 high 级 smell。"""
    metrics = FileMetrics(path=str(path))
    smells: list[Smell] = []
    metrics.sloc, metrics.total_lines = _count_sloc(source)

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        smells.append(Smell("syntax_error", str(path), exc.lineno or 0,
                            f"{exc.msg}", "high"))
        return metrics, smells

    # --- 函数与类 ---
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            metrics.n_functions += 1
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            length = end - node.lineno + 1
            if length > metrics.max_func_len:
                metrics.max_func_len = length
                metrics.max_func_name = node.name
            if length > max_func_len:
                smells.append(Smell(
                    "long_function", str(path), node.lineno,
                    f"{node.name}() 有 {length} 行（阈值 {max_func_len}）",
                    "high" if length > max_func_len * 2 else "medium",
                ))
        elif isinstance(node, ast.ClassDef):
            metrics.n_classes += 1

    # --- 嵌套深度 ---
    nesting = _NestingVisitor()
    nesting.visit(tree)
    metrics.max_nesting = nesting.max_depth
    if nesting.max_depth > max_nesting:
        smells.append(Smell(
            "deep_nesting", str(path), 0,
            f"最大嵌套 {nesting.max_depth} 层（阈值 {max_nesting}）", "medium",
        ))

    # --- import ---
    collector = _ImportCollector()
    collector.visit(tree)
    metrics.n_imports = sum(len(v) for v in collector.bindings.values())
    used = _used_names(tree)
    for name, occurrences in collector.bindings.items():
        if name in collector.structural:
            continue  # __future__ 之类不绑定运行时名字
        if name in used:
            continue
        # 每处重复 import 都单独报 —— 否则 agent 只改一处，剩下 11 处还在
        for mod, line in occurrences:
            metrics.n_unused_imports += 1
            smells.append(Smell(
                "unused_import", str(path), line,
                f"`{name}`（来自 {mod}）未被使用", "medium",
            ))

    # --- except ---
    for line in _find_bare_except(tree):
        metrics.n_bare_except += 1
        smells.append(Smell(
            "bare_except", str(path), line,
            "裸 except 会吞掉 KeyboardInterrupt/SystemExit", "high",
        ))

    # --- 死赋值 ---
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in _find_dead_assign(node):
                metrics.n_dead_assign += 1
                smells.append(Smell(
                    "dead_assignment", str(path), line,
                    f"{node.name}() 内有赋值后未使用的变量", "low",
                ))

    # --- 重复分支 ---
    for line in _find_dup_branches(tree):
        metrics.n_dup_branches += 1
        smells.append(Smell(
            "duplicate_branch", str(path), line,
            "if/elif 分支体完全相同，条件可能写错或可合并", "medium",
        ))

    # --- TODO ---
    for i, line_text in enumerate(source.splitlines(), 1):
        if _TODO_RE.search(line_text):
            metrics.n_todo += 1
            smells.append(Smell("todo_marker", str(path), i,
                                f"遗留标记：{line_text.strip()[:80]}", "low"))

    # --- 超长行 ---
    for i, line_text in enumerate(source.splitlines(), 1):
        if len(line_text) > max_line_len:
            metrics.n_long_lines += 1
            smells.append(Smell(
                "long_line", str(path), i,
                f"第 {i} 行 {len(line_text)} 字符（阈值 {max_line_len}）", "low",
            ))

    return metrics, smells


def analyze_file(path: Path, **kwargs) -> tuple[FileMetrics, list[Smell]]:
    """读文件并分析。先做编码检测，再交给 analyze_source。

    顺序很关键：先判编码再判语法，否则 UTF-16 文件会被误报成
    「语法错」，agent 会去改逻辑而不是改编码（实测踩过）。
    """
    try:
        raw = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return FileMetrics(path=str(path)), [
            Smell("read_error", str(path), 0, str(exc), "high"),
        ]

    enc_kind, enc_detail = detect_encoding_problem(raw)
    if enc_kind:
        metrics = FileMetrics(path=str(path))
        # 尽量解出内容以便统计行数（utf-16 可解）
        try:
            text = raw.decode("utf-16", errors="replace")
            metrics.sloc, metrics.total_lines = _count_sloc(text)
        except Exception:  # noqa: BLE001
            pass
        return metrics, [Smell(enc_kind, str(path), 0, enc_detail, "high")]

    source = raw.decode("utf-8", errors="replace")
    return analyze_source(source, str(path), **kwargs)


# --------------------------------------------------------------------------- #
# 目录级评估
# --------------------------------------------------------------------------- #
# 评估时必须排除的目录。
# `.cforge` 与 `.opencode` 尤其关键 —— 它们装着快照与状态文件，
# 若被算进整洁度评估，agent 自己产生的备份会污染分数，
# 而且快照目录会随轮次增长，导致分数单调恶化（实测踩过）。
DEFAULT_EXCLUDE = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "build", "dist", ".tox", ".workbuddy",
    ".archive", ".checkpoints", "runs", ".cforge", ".opencode",
}


def iter_python_files(root: Path, *, exclude: set[str] | None = None,
                      max_files: int = 400) -> Iterator[Path]:
    exclude = exclude or DEFAULT_EXCLUDE
    count = 0
    for path in sorted(root.rglob("*.py")):
        if count >= max_files:
            return
        parts = set(path.relative_to(root).parts[:-1])
        if parts & exclude:
            continue
        if path.name.startswith("."):
            continue
        count += 1
        yield path


# 扣分权重。high 权重最高，因为它代表真实缺陷而非风格问题。
PENALTY = {"high": 6.0, "medium": 1.5, "low": 0.4}


def evaluate(root: Path, *, exclude: set[str] | None = None,
             max_files: int = 400, include_clones: bool = True,
             **kwargs) -> QualityReport:
    """对目录做整洁度评估，返回 0-100 分与逐条 smell。

    ## 评分模型（三次迭代后的结果）

    三类问题分开计分，因为它们的严重度和规模关系不同：

    1. **语法错误** —— 按「文件失败率」算。一个文件全错和一百个文件里
       错一个，严重度天差地别。全项目语法错 = 扣 40 分。
    2. **high 级问题**（裸 except）—— 独立扣分，每个 8 分，上限 30。
       不能与 medium 混算：它吞掉 KeyboardInterrupt，是真实缺陷。
    3. **一般 smell** —— 分两档，因为「密度」对大小文件不公平：
       - 小文件（<40 行）按**绝对条数**算，每个加权问题约扣 1.8 分
       - 大文件按**密度**算，封顶 12（每百行 12 个加权问题即最差）

    ## 踩过的坑（都反映在上面）

    - 初版纯按 `penalty / (sloc/100)`：1 行代码 + 1 个语法错 = 10 分，
      agent 第一轮必被质量门拦死，还没开始就被回退
    - 第二版按密度 + 封顶：「30 个 medium」与「1 个 high」同分 58，
      丢失了严重度区分
    - 第三版（当前）：分 Three-tier，各档独立，实测区分度符合直觉
    """
    report = QualityReport()
    for path in iter_python_files(root, exclude=exclude, max_files=max_files):
        metrics, smells = analyze_file(path, **kwargs)
        try:
            metrics.path = str(path.relative_to(root)).replace("\\", "/")
            for s in smells:
                s.path = metrics.path
        except ValueError:
            pass
        report.files.append(metrics)
        report.smells.extend(smells)

    n_files = max(len(report.files), 1)
    sloc = max(report.total_sloc, 1)

    raw_penalty = 0.0
    syntax_fail = 0
    high_count = 0
    for s in report.smells:
        if s.kind == "syntax_error":
            syntax_fail += 1
            continue
        if s.severity == "high":
            high_count += 1
        raw_penalty += PENALTY.get(s.severity, 1.0)

    # 1) 语法错误：按文件失败率
    syntax_penalty = (syntax_fail / n_files) * 40.0

    # 2) high 级：独立扣分
    high_penalty = min(high_count * 8.0, 30.0)

    # 3) 一般 smell：小文件按条数，大文件按密度
    if sloc < 40:
        smell_penalty = raw_penalty * 1.8
    else:
        density = min(raw_penalty / sloc * 100.0, 12.0)
        smell_penalty = density * 7.0

    score = 100.0 - smell_penalty - syntax_penalty - high_penalty

    # 4) 跨文件重复：按重复率扣分
    #
    # 为什么不把每个 clone 当成一个 smell：克隆是「一对」关系，
    # 同一段代码参与多个 clone 时会被重复计数，条数不可加。
    # 用「重复率」这个全局比例更稳，也更容易解释。
    #
    # 阈值依据：重复率 10% 以内属于可接受（少量样板代码），
    # 超过 30% 说明项目里有明显的复制粘贴习惯。
    if include_clones:
        from cforge.quality.clones import scan as _scan_clones

        clone_report = _scan_clones(root, exclude=exclude)
        report.clone_pairs = len(clone_report.pairs)
        raw_ratio = clone_report.duplicate_ratio
        report.duplicate_ratio = raw_ratio

        # 样本量置信度：函数太少时比例不可靠。
        # 实测教训：2 个函数里有 1 对重复 → 比例 100%，但这只是一个观察，
        # 不足以断言「这个项目满盘重复」。用 sqrt(n/6) 做平滑，
        # n=6 以上完全采信，n=2 时只算 58% 的权重。
        n_units = len(clone_report.units)
        confidence = min(1.0, (n_units / 6.0) ** 0.5) if n_units > 0 else 0.0

        if raw_ratio > 0.10:
            excess = (raw_ratio - 0.10) * 100.0
            score -= min(excess * 1.2 * confidence, 25.0)

    report.score = round(max(0.0, min(100.0, score)), 1)
    return report


def diff_reports(before: QualityReport, after: QualityReport) -> dict:
    """对比两次评估，用于证明「改进是否真的让代码更干净」。"""
    def key(s: Smell) -> tuple[str, int, str]:
        return (s.path, s.line, s.kind)

    b = {key(s): s for s in before.smells}
    a = {key(s): s for s in after.smells}
    fixed = [b[k] for k in b if k not in a]
    introduced = [a[k] for k in a if k not in b]
    return {
        "score_before": before.score,
        "score_after": after.score,
        "score_delta": round(after.score - before.score, 1),
        "fixed": len(fixed),
        "introduced": len(introduced),
        "sloc_before": before.total_sloc,
        "sloc_after": after.total_sloc,
        "fixed_detail": [str(s) for s in fixed[:10]],
        "introduced_detail": [str(s) for s in introduced[:10]],
    }
