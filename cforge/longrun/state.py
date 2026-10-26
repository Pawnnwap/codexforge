"""长程任务状态：让 agent 在上下文重置后仍然记得「试过什么、别再试」。

## 为什么这是「超长程」的关键

LLM 的真实失效模式不是「不会做」，而是**在长任务里绕圈**：

- 第 3 轮修 A 方案，失败
- 第 11 轮（上下文已被压缩）又去试 A 方案，因为压缩时丢了这个信息
- 第 19 轮再试一次

上下文压缩是必需的技术，但它**天然会丢失失败记忆**。所以必须把
「方向 + 结果」外置到磁盘，并在每次重启时注入。

## 与超级大模型自身记忆的区别

模型侧的 memory 是不可审计的（你不知道它记住了什么、忘了什么）。
这里的状态是**结构化、可回放、可断言**的 —— 每一条都有轮次、时间、
证据引用。答辩时可以直接给人看「这个 agent 在第 7 轮试过 X，失败原因
是 Y，之后没有再试」。

## 三条防绕圈纪律

1. **方向去重**：同一个方向做第二次时，必须带上「上次为什么失败」
2. **停滞检测**：连续 N 轮工作区指纹不变 → 强制换策略
3. **教训上限**：保留最近 M 条教训，防止状态文件无限膨胀
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_DIR = ".opencode"
STATE_FILE = "target_state.json"
MAX_DIRECTIONS = 12
MAX_LESSONS = 8
# 词元正则：单字符数字/字母也要保留。
# 踩过的坑：初版用 `[a-z0-9_./-]{3,}`，导致 "approach number 0" 与
# "approach number 7" 归一化后完全相同（数字长度<3 被丢弃），
# 十几个不同的尝试方向被误判成同一个，agent 完全看不到自己试过多少条路。
TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+")
STOP_WORDS = {
    "agent", "and", "before", "candidate", "code", "current", "from",
    "into", "must", "output", "please", "should", "that", "the", "this",
    "with", "without", "task", "work", "make", "need", "want", "number",
    "approach", "step", "try",
}


def normalize_direction(text: str) -> str:
    """把「方向描述」归一化成可比较的 token 集合，用于去重。

    保留子集语义：词序无关、大小写无关、停用词剔除。
    但**不能丢数字** —— 数字往往是区分不同尝试的唯一线索。
    """
    tokens = [t.lower() for t in TOKEN_RE.findall(text or "")]
    kept = [t for t in tokens if t not in STOP_WORDS]
    return " ".join(sorted(set(kept)))


def direction_signature(text: str) -> str:
    return hashlib.sha1(normalize_direction(text).encode()).hexdigest()[:12]


@dataclass
class Direction:
    """一次「尝试方向」的记录。"""

    signature: str
    text: str
    turn: int
    outcome: str = "unknown"      # ok | failed | partial
    failure_reason: str = ""
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return asdict(self)

    def brief(self) -> str:
        mark = {"ok": "✓", "failed": "✗", "partial": "~"}.get(self.outcome, "?")
        base = f"{mark} 第{self.turn}轮：{self.text[:80]}"
        if self.outcome == "failed" and self.failure_reason:
            base += f" —— 失败原因：{self.failure_reason[:100]}"
        return base


@dataclass
class TargetState:
    """长程任务的持久状态。"""

    target: str = ""
    success_criteria: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    status: str = "running"
    directions: list[Direction] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    # 每轮的「工作区指纹」，用于检测停滞
    fingerprints: list[str] = field(default_factory=list)
    stagnation_count: int = 0
    turn: int = 0
    started_at: float = field(default_factory=time.time)

    # --- 方向去重 ---
    def seen_direction(self, text: str) -> Direction | None:
        sig = direction_signature(text)
        for d in self.directions:
            if d.signature == sig:
                return d
        return None

    def record_direction(self, text: str, outcome: str = "unknown",
                         failure_reason: str = "") -> Direction:
        sig = direction_signature(text)
        existing = self.seen_direction(text)
        if existing is not None:
            # 同方向重复：更新结果而不是新增，避免状态膨胀
            existing.outcome = outcome
            existing.failure_reason = failure_reason
            existing.turn = self.turn
            return existing
        d = Direction(signature=sig, text=text, turn=self.turn,
                      outcome=outcome, failure_reason=failure_reason)
        self.directions.append(d)
        # 保留最近 N 条
        if len(self.directions) > MAX_DIRECTIONS:
            self.directions = self.directions[-MAX_DIRECTIONS:]
        return d

    def failed_directions(self) -> list[Direction]:
        return [d for d in self.directions if d.outcome == "failed"]

    # --- 教训 ---
    def add_lesson(self, lesson: str) -> None:
        lesson = lesson.strip()
        if not lesson or lesson in self.lessons:
            return
        self.lessons.append(lesson)
        if len(self.lessons) > MAX_LESSONS:
            self.lessons = self.lessons[-MAX_LESSONS:]

    # --- 停滞 ---
    def push_fingerprint(self, fp: str) -> bool:
        """记录本轮工作区指纹。返回是否判定为停滞。"""
        self.fingerprints.append(fp)
        if len(self.fingerprints) > 10:
            self.fingerprints = self.fingerprints[-10:]
        if len(self.fingerprints) >= 2 and self.fingerprints[-1] == self.fingerprints[-2]:
            self.stagnation_count += 1
            return True
        self.stagnation_count = 0
        return False

    # --- 序列化 ---
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def save(self, workspace: Path) -> Path:
        path = Path(workspace) / STATE_DIR / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)   # 原子写入，防止崩在中间留下坏文件
        return path

    @classmethod
    def load(cls, workspace: Path) -> TargetState:
        path = Path(workspace) / STATE_DIR / STATE_FILE
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        dirs = [Direction(**d) for d in raw.get("directions", [])
                if isinstance(d, dict)]
        return cls(
            target=raw.get("target", ""),
            success_criteria=list(raw.get("success_criteria", [])),
            failure_conditions=list(raw.get("failure_conditions", [])),
            status=raw.get("status", "running"),
            directions=dirs,
            lessons=list(raw.get("lessons", [])),
            fingerprints=list(raw.get("fingerprints", [])),
            stagnation_count=int(raw.get("stagnation_count", 0)),
            turn=int(raw.get("turn", 0)),
            started_at=float(raw.get("started_at", time.time())),
        )

    # --- 注入给 agent 的摘要 ---
    def inject_block(self) -> str:
        """生成重启时要注入的上下文。这是「跨上下文重置的记忆」。"""
        lines: list[str] = []
        if self.target:
            lines.append(f"## 目标\n{self.target}")
        if self.success_criteria:
            lines.append("## 成功标准")
            lines.extend(f"- {c}" for c in self.success_criteria)

        failed = self.failed_directions()
        if failed:
            lines.append("## 已失败的方向（不要重复）")
            lines.extend(f"- {d.text[:90]} → 失败：{d.failure_reason[:100]}"
                         for d in failed[-6:])

        succeeded = [d for d in self.directions if d.outcome == "ok"]
        if succeeded:
            lines.append("## 已验证有效的做法")
            lines.extend(f"- {d.text[:90]}" for d in succeeded[-4:])

        if self.lessons:
            lines.append("## 教训")
            lines.extend(f"- {x}" for x in self.lessons)

        if self.stagnation_count > 0:
            lines.append(
                f"\n⚠ 已连续 {self.stagnation_count} 轮工作区无实质变化。"
                "必须换一个完全不同的策略，禁止重复上轮做法。"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 工作区指纹
# --------------------------------------------------------------------------- #
_FP_EXCLUDE = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "build", "dist", ".cforge", ".opencode",
    "runs", ".archive", ".checkpoints", ".workbuddy",
}


def workspace_fingerprint(workspace: Path, *, max_files: int = 800) -> str:
    """工作区内容指纹。

    只算「源码类」文件的内容哈希，不含缓存与版本控制目录。
    这样「改了一堆格式但逻辑没动」也会变指纹（真实改动），
    而「只是 time.time() 之类无关文件变化」不会误判为进展。
    """
    root = Path(workspace)
    h = hashlib.sha1()
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= max_files:
            break
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(root).parts[:-1])
        if rel_parts & _FP_EXCLUDE:
            continue
        suffix = path.suffix.lower()
        if suffix not in {".py", ".js", ".ts", ".rs", ".go", ".md", ".toml",
                          ".json", ".yaml", ".yml", ".html", ".css", ".txt"}:
            continue
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
            h.update(rel.encode())
            h.update(path.read_bytes())
        except OSError:
            continue
        count += 1
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 停滞响应阶梯
# --------------------------------------------------------------------------- #
@dataclass
class StagnationResponse:
    """停滞时应采取的动作，按严重度递进。"""

    level: int          # 0 = 正常，1 = 提醒，2 = 强制换策略，3 = 重启
    action: str
    inject: str = ""

    @property
    def should_restart(self) -> bool:
        return self.level >= 3


def stagnation_ladder(count: int) -> StagnationResponse:
    """停滞次数 → 响应。对应 super-opencode 的 nudge ladder，但简化了。"""
    if count <= 0:
        return StagnationResponse(0, "正常推进")
    if count == 1:
        return StagnationResponse(
            1, "提醒",
            "工作区在本轮没有实质变化。确认你是在推进任务而不是在分析。",
        )
    if count == 2:
        return StagnationResponse(
            2, "强制换策略",
            "连续两轮工作区无变化。必须放弃当前方案，改用不同的实现路径。",
        )
    return StagnationResponse(
        3, "重启会话",
        "连续三轮无变化，判定为绕圈。会话将重启，只保留目标与已失败方向。",
    )
