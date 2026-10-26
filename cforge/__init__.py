"""CodexForge — 以 Codex 为唯一执行后端的受监督智能体框架。

针对兴智杯 Track 160 重构：
- 执行后端从 opencode/codex 双引擎收敛为 **仅 codex**
- 新增编辑门禁层（沿用 codex-edit-gates 的 8 语言 PostToolUse 门禁）
- LLM 侧做成 provider 无关，书生与 OpenRouter 走同一套接口，仅换配置

发布状态：架构骨架（skeleton）。可运行的只有 provider 探测、命令构造、
门禁桥接与审计；监督循环保留接口契约，实现留待下一步。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
