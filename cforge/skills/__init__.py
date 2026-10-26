"""skills 包：Agent Skills 技能化封装 + 按需加载。

设计来源（super-opencode 时代的实测遗产）：
技能正文永不进初始 prompt——只有 catalog（name + 一句话描述）进。
判官/调用方显式请求才加载正文。这直接命中两个真实问题：
1. 上下文污染：codex 会把 $HOME/.agents/skills 下所有技能注入系统提示词
   （实测 7,367 字符，含微信视频号下载等无关内容），任务描述被淹没；
2. 安全：`_NAME_RE` 校验 + resolve() 目录校验双保险，结构性阻断路径穿越。

本实现是双模式：既能作为 harness 的技能目录，也能被 CLI/MCP 查询。
"""
from cforge.skills.skill_bank import SkillBank, SkillMeta

__all__ = ["SkillBank", "SkillMeta"]
