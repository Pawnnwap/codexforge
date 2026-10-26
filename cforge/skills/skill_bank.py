"""技能库：catalog 注入 + 按需加载正文 + 路径穿越防护。

三个实测驱动的设计（都有证据，不是惯例）：

1. **正文永不进初始 prompt**。codex 0.163.0 会把 `$HOME/.agents/skills`
   下所有技能注入系统提示词（抓包实测第 2 条 developer 消息 7,367 字符，
   含微信视频号下载、竞赛答辩等无关内容），把 554 字符的真实任务淹没在
   25K 里。小模型表现为「读不到指令」。我们的对策：catalog 只含
   name + description，正文必须显式 `load(name)` 才读。

2. **路径穿越双保险**。super-opencode 时代只用 `_NAME_RE` 正则校验；
   这里再加 `resolve()` 后校验仍在 skills_dir 内——正则拦名字，
   resolve 拦符号链接/.. 逃逸。两者缺一不可。

3. **加载上限 + LRU**。单轮 `max_loads_per_turn`（默认 1）防「加载链」
   耗尽上下文；resident set 用 LRU 淘汰（默认 3）。参数来自
   super-opencode 的实测经验，不是拍的。

frontmatter 解析是手写的 YAML 子集（key: value 行）——不引 PyYAML，
因为技能文件格式自控，自控格式手写解析是零依赖架构下的小代价。
"""
from __future__ import annotations

import re
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

# 技能名：小写字母/数字/连字符，防路径穿越的第一道闸
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# frontmatter：--- 包裹的 key: value 行（无嵌套、无锚点，自控子集）
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

BUILTIN_DIR = Path(__file__).parent / "builtin"


@dataclass(frozen=True)
class SkillMeta:
    """catalog 条目。进 prompt 的只有这些字段。"""

    name: str
    description: str
    path: Path

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description}


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆 frontmatter 与正文。没有 frontmatter 时返回 ({}, 全文)。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


class SkillBank:
    """按需加载的技能库。

    默认指向内置目录；也可指向用户安装的目录（install_builtin 的目标）。
    """

    def __init__(self, skills_dir: Path | None = None, *,
                 max_resident: int = 3, max_loads_per_turn: int = 1):
        # 两侧都 resolve，保证 is_relative_to 在同一基准下比较
        # （未 resolve 的一方可能带 `..` 或大小写差异，导致误判）
        self.skills_dir = (Path(skills_dir) if skills_dir else BUILTIN_DIR).resolve()
        self.max_resident = max_resident
        self.max_loads_per_turn = max_loads_per_turn
        # OrderedDict 而非 dict：LRU 需要 move_to_end（dict 没有这个方法，
        # 首轮测试即暴露——这正是「先写测试」的价值）
        self._resident: OrderedDict[str, str] = OrderedDict()
        self._loads_this_turn = 0

    # ------------------------------------------------------------------ catalog
    def catalog(self) -> list[SkillMeta]:
        """扫目录，只读 frontmatter 的 name/description。正文不读。"""
        if not self.skills_dir.is_dir():
            return []
        out: list[SkillMeta] = []
        for p in sorted(self.skills_dir.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = _parse_frontmatter(text)
            name = meta.get("name") or p.stem
            desc = meta.get("description") or "（无描述）"
            out.append(SkillMeta(name=name, description=desc, path=p))
        return out

    def render_catalog_prompt(self) -> str:
        """渲染成可注入 prompt 的「可用技能」块。

        只有这个块允许进初始 prompt；正文永远不行。
        """
        catalog = self.catalog()
        if not catalog:
            return ""
        lines = ["## 可用技能（按需加载，勿预读正文）"]
        for meta in catalog:
            lines.append(f"- {meta.name}: {meta.description}")
        lines.append("用法：需要某个技能的完整流程时，输出 LOAD_SKILL: <name> 请求加载。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 加载
    def load(self, name: str) -> str:
        """按需读正文。三道检查：名字格式 / 单轮上限 / resolve 目录校验。"""
        if not _NAME_RE.match(name):
            raise ValueError(f"非法技能名：{name!r}（须匹配 {_NAME_RE.pattern}）")
        if self._loads_this_turn >= self.max_loads_per_turn:
            raise ValueError(
                f"本轮已加载 {self._loads_this_turn} 个技能（上限 "
                f"{self.max_loads_per_turn}）。先完成当前技能，或开启新一轮。"
            )
        if name in self._resident:
            self._resident.move_to_end(name)  # LRU 触达
            return self._resident[name]

        path = (self.skills_dir / f"{name}.md").resolve()
        # 第二道闸：解析后的真实路径必须仍在技能目录内
        # （拦符号链接与 .. 逃逸——正则只拦名字，拦不住这两种）
        #
        # ⚠️ 必须用 Path.is_relative_to 而不是 `str.startswith(dir + "/")`。
        # 后者在 Windows 上恒为 False：Windows 的 `str(Path)` 用反斜杠，
        # 拼上硬编码的 "/" 得 `...\skills/`，与解析结果的 `...\skills\x.md`
        # 永不匹配。原实现靠 `path.parent != skills_dir` 兜底才"看起来能用"，
        # 但那导致「正常文件也走兜底」——逻辑上闸门形同虚设，
        # 且在被测文件恰好在子目录时行为不确定。
        # 这个缺陷是靠「克隆后首跑偶发失败」暴露的，不是逻辑审查发现的。
        if not path.is_relative_to(self.skills_dir):
            raise ValueError(f"技能路径越界：{path}")
        if not path.is_file():
            raise KeyError(f"技能不存在：{name}")

        text = path.read_text(encoding="utf-8")
        _, body = _parse_frontmatter(text)
        self._resident[name] = body
        while len(self._resident) > self.max_resident:
            self._resident.popitem(last=False)  # 淘汰最久未用
        self._loads_this_turn += 1
        return body

    def begin_turn(self) -> None:
        """新一轮对话开始，重置单轮加载计数。"""
        self._loads_this_turn = 0

    # ------------------------------------------------------------------ 安装
    def install_builtin(self, dest: Path) -> list[Path]:
        """把内置技能复制到 dest。返回复制的文件列表。"""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for p in sorted(BUILTIN_DIR.glob("*.md")):
            target = dest / p.name
            shutil.copyfile(p, target)
            copied.append(target)
        return copied
