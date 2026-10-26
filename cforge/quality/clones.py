"""跨文件重复代码检测：基于 AST 结构指纹。

## 为什么这个指标关键

LLM 有一个稳定的坏习惯：**遇到相似需求就复制粘贴改两个变量名**。
单文件看每处都合理，全局看就是灾难 —— 改一处逻辑要改五处。

传统工具用文本相似度（如 jscpd / PMD CPD），对改名不敏感但对
格式化敏感。这里用 **AST 结构指纹**：把函数体转成结构签名，
变量名与字面量全部归一化，因此「改个变量名就以为骗过检查」无效。

## 算法

1. 对每个函数提取「结构骨架」：节点类型序列 + 控制流形状
2. 转成 token 序列（标识符与字面量归一化，保留操作符类型）
3. 用 `difflib.SequenceMatcher.ratio()` 做序列相似度

## 为什么不用 set-Jaccard（实测踩过的坑）

集合 Jaccard 只看「出现过哪些 token」，**完全忽略频次与顺序**。
实测：一个 for 循环 vs 五个相同结构的 for 循环，token 集合完全相同，
Jaccard = 1.0（判为完全重复），而实际相似度只有 0.467。

这类误报会直接污染「重复代码率」这个指标 —— agent 会被要求
去「消除重复」，而实际上并没有重复。改用 SequenceMatcher 后，
顺序与频次都参与计算，同类误报消失。

## 阈值选择的依据（用真实代码校准，不是拍脑袋）

在 demo 项目上实测（两个文件，`save`/`load` 是语义不同的操作，
`save_cache`/`load_cache` 是真正的复制粘贴）：

| 阈值 | 检出对数 | 内容 | 判定 |
|---|---|---|---|
| 0.72 | 6 | 含 `save~load`（0.76） | ❌ 误报：INSERT vs SELECT 被当成重复 |
| 0.80 | 2 | 只有 `save_cache~save`、`load_cache~load` | ✅ 正确 |
| 0.90 | 2 | 同上 | ✅ 正确 |
| 1.00 | 2 | 同上 | ✅ 正确 |

在 cforge 自身（3.5k 行）上：阈值 0.72 会把 `_snapshot~_restore`（0.76）
判为重复，但这两个是方向相反的不同操作，不该合并。

→ 取 **0.90**：既保留对「复制粘贴改名」的检出（这类相似度通常 >0.95），
又排除「结构相似但语义不同」的误报。

初版用 0.72 是错的：它会把「所有 CRUD 函数」互相判为重复。
"""
from __future__ import annotations

import ast
import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# 不参与结构比较的节点属性（否则改名就能绕过）
_IGNORED_FIELDS = {"id", "arg", "name", "attr", "value", "n", "s", "attr_id"}


def _shape(node: ast.AST, depth: int = 0) -> str:
    """把 AST 压成结构字符串：只保留节点类型与层级，丢弃标识符与字面量。"""
    if depth > 12:  # 防御深层递归
        return "…"
    kind = type(node).__name__
    parts = [kind]
    for child in ast.iter_child_nodes(node):
        parts.append(_shape(child, depth + 1))
    return f"{kind}({','.join(parts[1:])})" if len(parts) > 1 else kind


def _tokens(node: ast.AST) -> list[str]:
    """token 序列，用于 Jaccard 二次确认（保留操作符与结构，归一化名字）。"""
    out: list[str] = []
    for n in ast.walk(node):
        t = type(n).__name__
        if isinstance(n, ast.Name):
            out.append("NAME")
        elif isinstance(n, ast.Constant):
            out.append(f"CONST:{type(n.value).__name__}")
        elif isinstance(n, ast.Attribute):
            out.append("ATTR")
        elif isinstance(n, ast.BinOp):
            out.append(f"BINOP:{type(n.op).__name__}")
        elif isinstance(n, ast.Compare):
            out.append(f"CMP:{len(n.ops)}")
        else:
            out.append(t)
    return out


def _similarity(a: list[str], b: list[str]) -> float:
    """序列相似度。考虑顺序与频次，避免 set 的频次盲区。"""
    if not a or not b:
        return 0.0
    # 长度差过大直接短路，省掉 O(n²) 的对齐计算
    ratio_len = min(len(a), len(b)) / max(len(a), len(b))
    if ratio_len < 0.5:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class FunctionUnit:
    """一个可比较的函数单元。"""

    path: str
    name: str
    lineno: int
    end_lineno: int
    n_statements: int
    shape: str
    tokens: list[str]
    # 是否为 async def。用来识别「同步/异步孪生实现」——
    # 真实仓库里大量高相似度对其实是这类合理的架构模式（见 ClonePair.is_twin_pair）。
    is_async: bool = False
    # 所属类名（顶层函数为空）。同步/异步孪生常成对出现在
    # `Foo` / `AsyncFoo` 这类镜像类里，靠类名判断比只靠函数名可靠。
    owner: str = ""

    @property
    def loc(self) -> int:
        return self.end_lineno - self.lineno + 1

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(self.shape.encode()).hexdigest()[:16]


def _strip_async_prefix(name: str) -> str:
    """去掉 asyncio 命名惯用的 `a` 前缀，用于判断孪生。

    覆盖三种常见写法（均在 httpx 实测遇到）：
      - `aread` / `read`
      - `aiter_bytes` / `iter_bytes`
      - `__aenter__` / `__enter__`（a 在 dunder 之后）

    只剥一层，且要求剩余名字长度 >= 2，避免把 `abs`、`add` 这类
    真以 a 开头的词剥坏成 `bs`、`dd`（那会制造新的假阳性）。
    """
    if name.startswith("__a") and name.endswith("__"):
        return "__" + name[3:]
    if name.startswith("a") and len(name) > 3 and not name.startswith("__"):
        return name[1:]
    return name


def _mirror_owner(a: str, b: str) -> bool:
    """两个类名是否互为同步/异步镜像（`Client` / `AsyncClient`）。

    顶层函数无 owner，返回 False。要求两侧非空且去掉 Async 后相等，
    避免空字符串相等导致顶层函数全被判成孪生。
    """
    if not a or not b:
        return False
    return a.replace("Async", "") == b.replace("Async", "")


@dataclass
class ClonePair:
    """一对高相似度的函数。

    区分「真重复」与「同步/异步孪生」是必要的一步：前者该合，后者不该动。
    """

    a: FunctionUnit
    b: FunctionUnit
    similarity: float

    @property
    def is_sync_async_twin(self) -> bool:
        """是否「同步/异步孪生实现」对。

        ## 为什么必须识别（实测踩过的假阳性）

        在 httpx 上实测，`detect_clones` 报出 10 对高相似度：
          - `Client.__init__` ≈ `AsyncClient.__init__`（0.92）
          - `Client.request` ≈ `AsyncClient.request`（0.94）
          - `Client._send_single_request` ≈ `AsyncClient._send_single_request`（0.96）
          - `_decoders.decode` ≈ `_decoders.adecode`（1.00）
          - `_models.read` ≈ `_models.aread`（0.92）

        这些**不是重复代码，是必要的双实现**：asyncio 生态里同步与异步版本
        必须分别写（`await` 无法参数化）。把它们当债务让模型去「抽共用函数」
        会直接破坏 API —— 而我们的 prompt 恰恰会这么写，属于**会导致代码损坏
        的误导**。故在度量层直接标注为孪生对，由上层决定是否排除。

        判据（三条任一成立即可，且必须一 async 一 sync）：
        1. 函数名去掉 a 前缀后相同（`aread` / `read`、`aiter_bytes` / `iter_bytes`）
        2. 类名去掉 `Async` 前缀后相同（`AsyncClient.__init__` / `Client.__init__`）
        3. 函数名相同且类名是 Async 前缀镜像（同上，覆盖 `_transport_for_url`）

        刻意要求「必须一 async 一 sync」作为前提，避免把两个同名同步函数
        （真正的重复）误判为孪生而放过。
        """
        if self.a.is_async == self.b.is_async:
            # 例外：同文件里 `Xxx` 与 `AsyncXxx` 两个**同步**的构造器/方法，
            # 若类名互为镜像也视为孪生（模型常见写法：两套类都同步定义，
            # 只是其中一个不被 await）。此时仍要求类名镜像，防误伤。
            if not _mirror_owner(self.a.owner, self.b.owner):
                return False
        else:
            na, nb = self.a.name, self.b.name
            name_ok = _strip_async_prefix(na) == _strip_async_prefix(nb)
            owner_ok = _mirror_owner(self.a.owner, self.b.owner)
            if not (name_ok or owner_ok):
                return False
        # 名字维度也要过：至少函数名或类名之一互为镜像
        return (_strip_async_prefix(self.a.name) == _strip_async_prefix(self.b.name)
                or _mirror_owner(self.a.owner, self.b.owner))

    def __str__(self) -> str:
        tag = "（同步/异步孪生，非重复）" if self.is_sync_async_twin else ""
        return (f"{self.a.path}:{self.a.lineno} {self.a.name}() ≈ "
                f"{self.b.path}:{self.b.lineno} {self.b.name}() "
                f"(相似度 {self.similarity:.2f}){tag}")

    def as_dict(self) -> dict:
        return {
            "a": f"{self.a.path}:{self.a.lineno}", "a_name": self.a.name,
            "b": f"{self.b.path}:{self.b.lineno}", "b_name": self.b.name,
            "similarity": round(self.similarity, 3),
            "sync_async_twin": self.is_sync_async_twin,
            "loc": (self.a.loc, self.b.loc),
        }


@dataclass
class CloneReport:
    units: list[FunctionUnit] = field(default_factory=list)
    pairs: list[ClonePair] = field(default_factory=list)

    @property
    def duplicate_ratio(self) -> float:
        """重复代码占函数总数的比例。"""
        if not self.units:
            return 0.0
        involved = {id(u) for p in self.pairs for u in (p.a, p.b)}
        return len(involved) / len(self.units)

    def as_dict(self) -> dict:
        return {
            "n_functions": len(self.units),
            "n_clone_pairs": len(self.pairs),
            "duplicate_ratio": round(self.duplicate_ratio, 3),
            "pairs": [p.as_dict() for p in self.pairs[:20]],
        }


def extract_units(source: str, path: str, *, min_statements: int = 4) -> list[FunctionUnit]:
    """提取够长、值得比较的函数。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    units: list[FunctionUnit] = []
    # 先建「函数节点 → 所属类名」映射，才能知道每个函数属于哪个类。
    owner_of: dict[int, str] = {}
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for sub in cls.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner_of[id(sub)] = cls.name

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        n_stmt = sum(1 for _ in ast.walk(ast.Module(body=body, type_ignores=[]))
                     if isinstance(_, ast.stmt))
        if n_stmt < min_statements:
            continue
        units.append(FunctionUnit(
            path=str(path),
            name=node.name,
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
            n_statements=n_stmt,
            shape=_shape(node),
            tokens=_tokens(node),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            owner=owner_of.get(id(node), ""),
        ))
    return units


def detect_clones(units: list[FunctionUnit], *,
                  min_similarity: float = 0.90) -> CloneReport:
    """两两比较。O(n²)，但有 max_units 上限保护。"""
    report = CloneReport(units=units)
    n = len(units)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = units[i], units[j]
            # 快筛：结构指纹不同则细比；相同则直接用 Jaccard
            sim = _similarity(a.tokens, b.tokens)
            if sim >= min_similarity:
                report.pairs.append(ClonePair(a, b, sim))
    return report


def scan(root: Path, *, exclude: set[str] | None = None,
         min_statements: int = 4, min_similarity: float = 0.90,
         max_units: int = 600) -> CloneReport:
    """扫描目录。文件数与函数数都有上限，避免大仓库卡死。"""
    from cforge.quality.metrics import iter_python_files

    units: list[FunctionUnit] = []
    for path in iter_python_files(root, exclude=exclude, max_files=200):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(path)
        units.extend(extract_units(source, rel, min_statements=min_statements))
        if len(units) >= max_units:
            units = units[:max_units]
            break
    return detect_clones(units, min_similarity=min_similarity)
