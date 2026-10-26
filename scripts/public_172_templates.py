"""172 公开仓库的定制内容模板。

## 为什么放在这里而不是直接改产物

`build_public_172.py` 每次会清空目标目录重建。任何手工改动都会丢失 ——
实测第一次重建就把手写的 LICENSE / NOTICE / README 头部全弄没了。

所以**所有 172 专属内容必须是可重建的模板**，放在构建脚本旁边。
真源（codexforge）保持 160 的形态不变，172 的差异只体现在这些模板里。
"""
from __future__ import annotations

# README 的 172 版本头部（插在 H1 之后，覆盖原「为什么是这个定位」之前）
README_HEADER = """
> 本仓库为**兴智杯 Track 172「面向代码生成的生产效率创新方案赛」**参赛作品的公开代码仓库。

---

## 这个仓库解决什么问题

AI 写代码已经够快了。**慢的是「敢不敢让它改」。**

一个人维护多个项目时，没有 review、没有技术债专项、没有 CI 门禁。
于是 AI 生成的改动只能全凭肉眼扫一遍 —— 而肉眼扫不出「把 `print(e, file=sys.stderr)`
改成 `print(e)`」这种**分数没变但行为变了**的问题。

CodexForge 是一个守在改动之后的**确定性质量门**：每次改动都算一遍代码事实
（AST 整洁度、函数签名、异常处理、输出流），**变了就回滚，说不清就不改**。

### 三个不妥协的设计

| 设计 | 为什么 |
|---|---|
| **反馈信号来自代码事实，不来自 LLM 自评** | 文件在不在、AST 有几个 smell、签名有没有变 —— 可复现、可审计、可断言。判官类方案（Agent-as-a-Judge）的致命弱点是自评不可靠 |
| **每个判定都给出人可读理由** | 「跑 / 不跑」「接受 / 回滚」都能解释，否则没人敢让黑盒守护进程常驻自己仓库 |
| **净改善门 + 语义守护 + 全部可回滚** | 只有分数上升**且**行为未被破坏才接受；否则精确还原到改动前（含删掉 agent 新建的文件） |

### 实测（真实第三方开源仓库）

| 仓库 | 基线 | 守门后 | 处置 |
|---|---|---|---|
| smokeshow (MIT) | 95.2 | **97.3 (+2.1)** | 接受：拆开 66 行 `upload()`，抽 `_upload_and_notify()`，公开接口与注释保留，语义守护通过 |
| httpx (BSD-3) | 80.5 | 80.5 | 回滚：目标文件 600s 内未完成，工作区还原干净 |
| starlette (BSD-3) | 87.8 | 87.8 | 回滚：同上 |

> 「1 接受 / 2 回滚」本身就是产品价值的证明：被回滚的两次，工作区**回到与修改前完全一致**。
> 净改善门不是装饰，是真的在阻止低质量改动落地。

---

## 项目结构

```
cforge/
  quality/      整洁度度量（AST）、净改善门、语义守护、克隆检测
  watch/        常驻守门：四道触发闸门 + 修复器（scripted / codex）
  harness/      codex 执行后端 + 协议兼容代理 + 监督器
  mcp/          MCP server（4 个只读工具，标准库实现）
  skills/       Agent Skills（按需加载 + 路径越界防护）
  longrun/      长周期记忆（方向 / 教训 / 停滞）
tests/          363 个测试，零第三方运行时依赖
experiments/    真实仓库守门案例脚本（run_case.py）与诊断实验
docs/cases/     三个真实仓库的守门前后报告
```

---

"""

GITIGNORE = """\
# Python
__pycache__/
*.py[cod]
build/
dist/
*.egg-info/

# 虚拟环境
.venv/
venv/
env/

# 测试与覆盖率
.pytest_cache/
.coverage
htmlcov/

# CodexForge 运行产物
# .cforge 出现在被测工作区里，是守门状态（快照/报告），不是源码
.cforge/
runs/

# 本地克隆的待测仓库（体积大，且是他人代码）
.data/

# 会话产物
.workbuddy/

# 本地环境与凭据 —— 绝不提交
.env
.env.*
*.key
*_creds.json
*_token.txt

# 编辑器
.idea/
.vscode/
*.swp
.DS_Store
Thumbs.db
"""

GITATTRIBUTES = """\
* text=auto eol=lf
*.py text eol=lf
*.md text eol=lf
*.toml text eol=lf
*.sh text eol=lf
*.yml text eol=lf
*.yaml text eol=lf
*.json text eol=lf

# Windows 专用脚本保留 CRLF，否则 cmd/PowerShell 会解析异常
*.ps1 text eol=crlf
*.cmd text eol=crlf
*.bat text eol=crlf

# 二进制
*.png binary
*.jpg binary
*.zip binary
"""

NOTICE = """\
CodexForge
Copyright 2026 黄太哲 (VladHong)

本作品以 Apache License 2.0 授权（见 LICENSE 文件）。

---

本作品在运行时不包含任何第三方源代码副本，仅通过子进程与 HTTP API
调用下列组件。此处声明用于满足 Apache-2.0 第 4(d) 条的归属要求，
以及兴智杯参赛须知第 4 条「如涉及其他开源项目，需符合相关项目许可证合规要求」。

## 以子进程方式调用的可执行程序

  codex CLI 0.163.0
  https://github.com/openai/codex
  License: Apache-2.0
  用法：本作品通过子进程启动 codex 执行代码修复任务。
        未包含其源代码副本，属聚合（mere aggregation），不构成衍生作品。

## 以 HTTP API 方式调用的模型服务

  Intern-S2 / Intern-S1 Pro / InternVL 系列
  提供方：上海人工智能实验室（书生 API 平台）
  用法：通过官方 API 调用，不分发任何模型权重。

  OpenRouter 及其上游模型（仅开发期替代方案）
  用法：HTTP API 调用。

## 运行时 Python 依赖

  无。本作品运行时零第三方依赖，仅使用 Python 3.11+ 标准库。

## 开发与测试依赖

  pytest >= 8.0.0（MIT License），仅用于运行测试。
"""
