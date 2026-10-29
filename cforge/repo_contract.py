"""读取仓库级 Agent 契约（`AGENTS.md`），只取与「改代码」相关的那部分。

## 为什么要支持它

2026-10 的实测调研：14 个头部开源仓库里 **8 个**根目录有 `AGENTS.md`
（ruff 20KB、opencode 16KB、crush 10KB、OpenHands 10KB），
其中 `ruff`、`semgrep` 并不是 Agent 产品 —— 它们是**被 Agent 开发的仓库**，
所以给 Agent 写了仓库级规矩。这已经成了事实标准。

我们此前靠 `skills/` 在运行时拼装指令，属于自定义形态。
读 `AGENTS.md` 等于直接复用生态惯例：别人仓库里已经写好的约定，我们不必要求用户重写一遍。

## 为什么不能整份注入

`py_edit_gate` 与我们的 `build_fix_prompt` 都踩过同一个坑：
上下文里塞满无关描述后，模型会把注意力放到「了解全局」而不是「改这一处」。
实测 prompt 从 934 字符涨到 25K 字符时，模型改为先探索仓库、然后忘记任务。

所以这里做两件事：
1. **只挑与改动相关的行**（含质量/测试/格式/禁止类关键词的条目行或标题行）；
2. **硬性截断**（默认 1500 字符），并在注入时明确标注被截断。

## 边界

只认 `AGENTS.md` / `agents.md`，不解析 `CLAUDE.md`、`.cursor/rules` 等其它格式 ——
那些属于别的工具生态，硬塞进来会让"仓库约定"的来源变得不可解释。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FILENAMES = ("AGENTS.md", "agents.md")
MAX_CHARS_DEFAULT = 1500
MAX_UPWARD_LEVELS = 3

# 与「怎么改代码」相关的关键词。命中任一即视为相关条目。
_KEYWORDS = (
    "quality", "lint", "linting", "format", "fmt", "style", "test", "tests", "pytest",
    "type", "typing", "import", "commit", "gate", "guard", "review", "docstring",
    "must", "never", "always", "do not", "don't", "avoid", "required", "prefer",
    "质量", "整洁", "规范", "测试", "格式", "禁止", "不要", "必须", "优先", "避免",
    "提交", "审查", "注释", "类型", "导入",
)

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


@dataclass
class RepoContract:
    """一份仓库约定及其裁剪结果。"""

    path: Path
    text: str                      # 实际注入的正文
    raw_chars: int                 # 原文件总字符
    truncated: bool = False
    kept_rules: list[str] = field(default_factory=list)

    @property
    def used_chars(self) -> int:
        return len(self.text)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path), "raw_chars": self.raw_chars,
            "used_chars": self.used_chars, "truncated": self.truncated,
            "kept_rules": len(self.kept_rules),
        }


def find_contract(workspace: Path) -> Path | None:
    """在 workspace 及其上溯目录找 AGENTS.md（上溯到 .git 或 3 层为止）。"""
    cur = Path(workspace).resolve()
    for _ in range(MAX_UPWARD_LEVELS + 1):
        for name in FILENAMES:
            cand = cur / name
            if cand.is_file():
                return cand
        if (cur / ".git").exists():
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _looks_relevant(line: str) -> bool:
    low = line.lower()
    return any(k in low for k in _KEYWORDS)


def select_rules(text: str, max_chars: int = MAX_CHARS_DEFAULT) -> tuple[str, bool, list[str]]:
    """挑选相关行，返回 (正文, 是否截断, 命中的条目行)。

    保留原有顺序与标题层级（标题后面的条目属于哪个小节，读者要能看出来）。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    picked: list[str] = []
    rules: list[str] = []
    last_heading = ""

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        h = _HEADING_RE.match(line)
        if h:
            last_heading = line.strip()
            continue
        b = _BULLET_RE.match(line)
        body = b.group(1).strip() if b else line.strip()
        if not _looks_relevant(body):
            # 非条目但强相关的普通句子也收，避免漏掉「All code must be typed」这类写法
            if not _looks_relevant(line):
                continue
        entry = f"- {body}" if not b else f"- {body}"
        # 同一小节下重复内容去重
        if entry in picked:
            continue
        if last_heading:
            entry = f"{entry}（{last_heading.lstrip('# ').strip()}）"
            last_heading = ""
        picked.append(entry)
        rules.append(body)

    if not picked:
        # 没有命中关键词：退化为保留前若干行，至少让约定可见
        picked = [ln.strip() for ln in lines if ln.strip()][:12]
        rules = list(picked)

    out = "\n".join(picked)
    truncated = False
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n…（已截断，完整内容见 AGENTS.md）"
        truncated = True
    return out, truncated, rules


def load_contract(workspace: Path, max_chars: int = MAX_CHARS_DEFAULT) -> RepoContract | None:
    """读取并裁剪仓库约定。找不到返回 None（不是错误）。"""
    path = find_contract(workspace)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body, truncated, rules = select_rules(raw, max_chars=max_chars)
    if not body.strip():
        return None
    return RepoContract(path=path, text=body, raw_chars=len(raw),
                        truncated=truncated, kept_rules=rules)


def render_for_prompt(contract: RepoContract | None) -> str:
    """渲染成可注入 prompt 的片段。空约定返回空串（不占 token）。"""
    if contract is None:
        return ""
    note = "（已按相关性裁剪）" if contract.truncated else ""
    return (f"【仓库约定 {contract.path.name}{note}】\n"
            f"{contract.text}\n"
            f"以上是本仓库的既有规矩，改动必须遵守；与目标冲突时以更高一级的约束为准。")
