# CodexForge

> **定位：给同时维护多个项目的独立开发者 / 一人公司，做无人值守的代码质量守门与持续重构。**
> 两个核心能力：**自主反馈**（不靠模型自评，靠确定性证据）与**代码整洁度**（可量化、可收敛）。
> Execution backend: **codex only**。
>
> 状态：核心机制 + 常驻守门形态已实现并验证（v0.3.0）。

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

## 为什么是这个定位

「给程序员用」不是定位，是品类 —— 所有 coding agent 都如此。
真正能立住的是「**没有人做 code review 的人，如何防止代码腐化**」。

一人公司的硬约束是：没有 review、没有技术债专项、没有 CI 门禁。
质量完全取决于自律，而赶 deadline 时自律必然让位。等到想重构时已经不敢动了。

通用 coding agent 的形态是「你叫它才动」，但这个痛点的本质是「需要主动持续」。
**这不是模型能力问题，是产品形态问题** —— 所以 CodexForge 做成了常驻守门。

详细推导见 [`docs/定位-一人公司代码守门.md`](docs/定位-一人公司代码守门.md)。

### 两个机制在这个场景里是刚需，不是加分项

| 机制 | 通用场景 | 一人公司守门场景 |
|---|---|---|
| 长程自主反馈 | 「更好的实现」 | 重构天生多轮，且没人在旁看着，**必须**自己判断改好没有 |
| 代码整洁度 | 附加功能 | **就是业务指标本身** |

---

## 快速开始

```bash
# 环境体检
python -m cforge.cli doctor

# 架构总览
python -m cforge.cli layout

# 整洁度评估（零成本，不调 LLM）
python -m cforge.cli quality --path ./src --verbose

# 常驻守门（推荐：先用零成本修复器验证链路）
python -m cforge.cli guard --workspace ./myproject --fixer scripted --max-cycles 1

# 常驻守门（真 LLM 修复，需 API key）
export OPENROUTER_API_KEY=sk-or-...
python -m cforge.cli guard --workspace ./myproject --fixer codex \
  --cooldown 600 --debt-score 80

# 单次长程任务
python -m cforge.cli longrun \
  --workspace ./demo \
  --prompt "创建 calc 包：add.py / sub.py / __init__.py + 测试" \
  --expect-file calc/__init__.py
```

---

## 守门怎么工作

四道闸门决定「何时动手」—— 常驻服务的成败全在这里：

| 闸门 | 默认值 | 作用 |
|---|---|---|
| 冷却 | 600s | 防止连续保存触发多次重构 |
| 变更 | 只看源码后缀 | 改 README 不触发代码重构 |
| 债务 | 分数 < 80 或 high > 2 | 90 分的代码不需要「重构」 |
| 冲突 | 变更 > 25 文件则跳过 | 用户正在大改，插进去等于制造冲突 |

**每个判定都给出人能看懂的理由** —— 黑盒守护进程没人敢用。

修复后过**净改善门**：分数下降 / 改善 < 0.5 / 新增 high 级问题 → 全部回滚。

### 实测效果

```
$ python -m cforge.cli guard --workspace ./demo-guard --debt-score 99 --cooldown 0

修复器：scripted（AST 确定性，零 token）
  [guardian] 触发：整洁度 89.2 低于 99.0
  [guardian] ✓ 89.2 → 100.0 (+10.8)，修复 4 处，新增 0 处

质量：89.2 → 100.0 (+10.8)
```

业务逻辑完好无损，只精确删掉 4 个未使用 import。第二次运行自动跳过（债务已达阈值）。

### 守门报告

每次运行产出 `{workspace}/.cforge/guardian/report-*.md`，含汇总表 + 完整 JSON。
这是可对外证明的东西 —— 一人公司最缺的就是「我的代码库在过去 N 周质量提升了 X」。

---

## 三个核心机制的实现

### 零、修复分级 + 仓库契约 + 自检闭环（v0.4 新增）

这三项来自 2026-10 对头部开源项目的实测调研（见 `../docs/oss-agent-survey-20261029.md`）。

**1. 修复分级（`cforge/quality/repair.py`）**

此前只有「全自动」与「全不自动」两档，中间是空的。现在每个 smell 都带分级：

| 分级 | 含义 | 当前包含 |
|---|---|---|
| `auto` | 可证安全，允许自动改 | 仅 `unused_import` |
| `suggest` | 需要判断，只给动作 | 裸 except、长函数、深嵌套、重复分支等 |
| `advisory` | 仅记录，不产生改动 | `todo_marker`、`read_error` |

护栏：`tests/test_repair_tiers.py` 断言 `auto` **恰好只有** `unused_import`，
以后想加类型必须先改这条断言 —— 等于强制过一道「是否真的可证安全」的评审。
存在 `syntax_error` 时不执行任何自动重写。

**2. 仓库契约（`AGENTS.md`，`cforge/repo_contract.py`）**

调研发现 14 个头部仓库有 8 个根目录放了 `AGENTS.md`（ruff 20KB、opencode 16KB、
crush 10KB、OpenHands 10KB），其中 ruff / semgrep 并不是 Agent 产品 ——
它们是**被 Agent 开发的仓库**。这说明给 Agent 写仓库级契约已成事实标准。

我们的做法是「读它，但不整份注入」：

```bash
python -m cforge.cli contract --path . --raw
```

只挑与改动相关的条目行，硬性截断 1500 字符，并标注是否截断。
原因是实测教训：prompt 从 934 字符涨到 25K 字符时，模型改为先探索仓库、
随后忘记任务。**注入约定不能把任务本身挤掉。**

**3. 自检闭环（`QualityReport.actionable_text()`）**

SWE-agent 的经验是把「给智能体的接口」当独立设计对象：只报问题不够，
还要告诉它改完怎么验证。所以反馈末尾固定给出：

```
改完自检：python -m cforge.cli quality --path <ws>（目标 ≥ 54）
```

分数是确定性的，模型自己跑一次就能判断是否达标，不必靠猜或虚假宣布完成。

### 一、长程自主反馈

三路信号，**都不用 LLM 判断**：

| 信号 | 判定 |
|---|---|
| 目标达成 | 文件存在 **且可解析** / 测试退出码 |
| 代码整洁度 | AST 指标 → 0-100 分 |
| 工作区进展 | 内容哈希，排除缓存 |

跨轮记忆存 `.opencode/target_state.json`，重启注入：

```markdown
## 已失败的方向（不要重复）
- 用正则解析 CSV → 失败：引号内逗号处理不了

## 教训
- 第 3 轮声称完成但目标未达成：test_calc.py 不存在
```

停滞阶梯：1 次提醒 → 2 次强制换策略 → 3 次重启会话。

**反直觉设计**：prompt 只给失败与待办，**不给「已完成 N 项」**——
模型看到进度会倾向收尾，这在长程任务里是致命的。

### 二、代码整洁度

只用 `ast`，零第三方。作为验收门必须确定性 —— 第三方 linter 版本漂移会让
同一份代码在不同机器上给出不同分数。

八类问题：`syntax_error` / `encoding_utf16` / `encoding_bom` / `bare_except`（high）
/ `long_function` / `deep_nesting` / `unused_import` / `duplicate_branch`（medium）
/ `dead_assignment` / `todo_marker` / `long_line`（low）。

**三段式评分**（三次迭代才对）：
- 语法错误按文件失败率 ×40
- high 级独立扣分（每个 8，上限 30）
- 一般 smell：小文件（<40 行）按条数、大文件按密度（封顶 12）

**跨文件重复检测**用 AST 指纹 + `SequenceMatcher`。不能用集合 Jaccard ——
它忽略频次，实测「1 个循环」vs「5 个相同结构的循环」会判 1.0（实际 0.467）。

---

## 环境变量

| 变量 | 用途 |
|---|---|
| `CFORGE_BACKEND` | `openrouter`（开发）或 `intern`（答辩，书生） |
| `OPENROUTER_API_KEY` | 开发期替代模型 |
| `INTERN_API_KEY` | 书生（赛道硬性要求） |
| `CFORGE_MODEL` | 覆盖默认模型 |

**换模型只改环境变量，不动代码。**

---

## 已验证的事实

| 项 | 结果 |
|---|---|
| codex 只支持 `wire_api=responses` | 确认（`chat` 已被移除） |
| SSE 代理让 codex 跑通完整 turn | ✅ exit=0 |
| 模型真的创建文件（非只说话） | ✅ |
| 多轮反馈让 agent 补齐缺失文件 | ✅ 第 1 轮 goal=False → 第 2 轮 goal=True |
| 编码错误被精确诊断 | ✅ 报「UTF-16 编码」而非「语法错」 |
| 守门完整链路（检测→修复→验证） | ✅ 89.2 → 100.0 |
| 幂等性（修好后不反复动） | ✅ 第二次运行跳过 |
| 单元测试 | **292 passed** |

---

## 最大的两个踩坑

### 1. 「文件存在」是最弱的验收标准

实测：agent 创建了 4 个文件，逻辑完全正确，但全部是 **UTF-16 编码**。
旧的 `file_exists` 判「达成 ✅」，旧整洁度报 4 个「语法错」——**双重失效**。

修复后诊断：`calc/add.py 无法解析：UTF-16 编码（含 BOM），需转为 UTF-8`

**反馈的价值不在于「有问题」，而在于「指向真病因」。**

### 2. codex 的 sandbox 描述不可靠

`codex exec -s workspace-write` 时模型收到 17K 系统提示词后回复
「sandbox is read-only」拒绝写文件 —— 上游 200、turn 正常完成、但什么都没做。
改 `danger-full-access` 后立刻正常。

---

## 其他踩坑

| 现象 | 根因 | 修复 |
|---|---|---|
| `stream disconnected before completion` | codex 流式消费，代理返回整包 JSON | 实现完整 SSE 事件序列 |
| codex 卡 61.8 秒 | 默认 CODEX_HOME 刷 ChatGPT token，403 重试 | 隔离 CODEX_HOME → 4.4 秒 |
| `messages` 永远为空 | 消息在 `item.completed` 的 `item.text` | `CodexEvent.item/item_type` |
| codex 内部重试无限跑 | 撞策略拒绝时循环 13 分钟 | 看门狗线程 + `taskkill /F /T` |
| 端口 8765 报 WinError 10013 | Windows 端口保留区 | `listen_port=0` |
| 重复 import 只报 1 处 | 用 dict 收集，同模块覆盖 | 改 list 按行全量记录 |
| 快照目录自我复制 | `_snapshot` 把自己的输出扫进去 | 排除 dest 本身 |
| 不同方向被判重复 | 归一化正则要求 3 字符，数字被丢弃 | 保留所有 token |

---

## 与赛道 160 的对应

| 评分维度 | 权重 | 本作品 |
|---|---|---|
| 完整性 · 可复现性 | 30% 内 | 零依赖 + 339 测试 + 每条结论有实测 + Dockerfile（deploy/） |
| 完整性 · 文档规范 | 30% 内 | 每模块 docstring 记录设计取舍与踩坑 |
| **技术难度 · 书生融合** | **30% 内第一要素** | 三层就绪：provider 抽象 ✅ + 假上游 e2e 9/9 断言 ✅ + intern_probe.py（key 到位即出证据）；**key 未到位是既定事实** |
| 技术难度 · 前沿技术 | 30% 内 | 长周期记忆 ✅ 上下文工程 ✅ **MCP ✅**（4 工具，协议 2025-06-18 子集）**Skills ✅**（4 技能，按需加载） |
| **实用性 · 场景适配度** | **40% 内** | ✅ 一人公司守门（痛点/形态/可量化价值） |
| 实用性 · 真实案例 | 40% 内 | ✅ 案例流水线（`scripts/case_study.py`，真实仓库前后对比落盘） |

---

## 书生融合：三层就绪（诚实口径）

> key 未到位（需手机注册书生 API 平台 + 提额审核）。本节按证据等级分层写，
> 每一层都可以独立核验——不写「已接入」这种没有证据的话。

| 层 | 内容 | 证据 | 状态 |
|---|---|---|---|
| **S0 协议层** | Responses↔Chat 适配代理 | `tests/test_proxy_e2e.py` 8 用例 | ✅ 全绿 |
| **S0 链路层** | codex → proxy → 书生同构上游 | `tests/e2e/intern_e2e.py` 9 断言（假上游 + 真实 codex turn，11.1s） | ✅ 全过 |
| **S0 就绪度** | `cforge doctor --backend intern`（规划中）输出 ready / config-ready 结论 | `cforge/gateway/intern_readiness.py` | ✅ |
| **S1 端到端** | 真实 key 的一次完整探测 + 守门循环 | `xingzhi_cup/track160/probe/intern_probe.py` 落盘 `result.json` | ⏳ 待 key |
| **S2 降级** | 若 key 未及时到位：文档明写「待 key 验证」+ 复现命令 | 本节即 S2 形态 | ✅ |

三条红线：不伪造调用记录；不写无证据的「已接入」；不硬编码未核实的
model id（赛题正文与 match_encourage 两处表述不一致，以 `/api/v1/models`
实测为准）。

---

## 未完成项

1. **书生真实 key**（赛道硬性要求；路径 100% 就绪，`CFORGE_BACKEND=intern` 即切）
2. 多语言 codex 修复实证（`experiments/run_multilang.py` 就绪；无 key 时按
   BLOCKED 降级，不伪造成功记录）
3. 多语言确定性修复器（当前 AST 版只覆盖 Python 的 unused_import）
5. SSE 分块转发（当前一次性吐出全部事件）



---

## 为什么必须做协议适配（这条决定项目成立与否）

实测 codex 0.163.0：

```
$ codex exec -c 'model_providers.t.wire_api=chat' ...
Error loading config.toml: `wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"` in your provider config.
```

**codex 只认 `responses`**。而书生和 OpenRouter 都只保证 `/chat/completions`。

→ 所以 `harness/compat_proxy.py` 不是「优化项」，是**前置依赖**。没有它，codex 根本驱动不了书生。

验证方法（不依赖外网，用假 upstream）：

```bash
python -m pytest tests/test_proxy_e2e.py -v
```

8 个用例断言：`/responses` 请求 → 转成 `messages` → 打到上游 `/chat/completions` → 把响应转回合法 Responses 结构。

---

## 架构分层

```
┌─────────────────────────────────────────────────────────┐
│  runtime/orchestrator.py                                │
│  bootstrap → probe → proxy → execute → gates → judge    │
└───────┬─────────────┬──────────────┬──────────────┬─────┘
        │             │              │              │
   gateway/      harness/       checks/        harness/
   profiles.py   codex_backend  gates.py       supervisor.py
   client.py     compat_proxy   （8 语言门禁）  （Goal Guard）
        │             │
   书生 │ OpenRouter   codex CLI 0.163.0
```

| 模块 | 职责 | 状态 |
|---|---|---|
| `gateway/profiles.py` | provider 抽象：书生 / OpenRouter 同接口 + base_url 覆盖 | 实现 |
| `gateway/client.py` | OpenAI 兼容网关：降级链 + 结构化错误分类 | 实现 |
| `gateway/intern_readiness.py` | 书生就绪度三态结论（ready / config-ready / not-configured） | 实现 |
| `harness/codex_backend.py` | codex CLI 驱动：argv 构造 + JSONL 解析 | 实现 |
| `harness/compat_proxy.py` | Responses ↔ Chat 协议适配代理（SSE） | 实现 |
| `harness/supervisor.py` | Goal Guard：证据门控裁决 | 实现 |
| `checks/gates.py` | 8 语言编辑门禁桥接 | 实现 |
| `mcp/` | **MCP server**（stdio JSON-RPC，4 工具，标准库实现） | 实现 |
| `skills/` | **Agent Skills**（catalog + 按需加载 + 4 内置技能） | 实现 |
| `quality/` | 整洁度度量 + 跨文件克隆检测（评分真源） | 实现 |
| `watch/` | 四道闸门常驻守门 + 双修复器 | 实现 |
| `longrun/` | 长程自主反馈（三路确定性信号） | 实现 |
| `runtime/orchestrator.py` | 编排全链路 | 实现 |
| `deploy/` | Dockerfile + 体验动线 demo.sh | 实现 |
| `telemetry/` | 审计链与用量统计 | 占位 |

**运行时零第三方依赖**（仅标准库）。刻意的选择：评审复现不需要联网装包，也避开依赖树里的 copyleft 问题。MCP server 与技能库同样标准库实现——这是把零依赖贯彻到底，不是偷懒（设计取舍见各模块 docstring）。

---

## 快速开始

### 1. 环境体检

```bash
python -m cforge.cli doctor
```

检查 codex 是否存在、版本、Python、API key。

### 2. 配置

```bash
cp .env.example .env
```

| 环境变量 | 用途 |
|---|---|
| `CFORGE_BACKEND` | `openrouter`（开发）或 `intern`（答辩） |
| `OPENROUTER_API_KEY` | 开发期替代模型 |
| `INTERN_API_KEY` | 书生（赛道强制） |
| `CFORGE_MODEL` | 覆盖默认模型 |
| `CFORGE_PROXY` | 需要走代理时设置；默认强制直连 |

**换模型只改环境变量，不动代码。** 这是「暂用 Xing 29B 代替书生」的实现方式。

### 3. 探测上游

```bash
python -m cforge.cli probe --backend openrouter
```

输出 wire_api 支持情况、对话连通性、用量统计、逐模型成功率。

### 4. 安装门禁

```bash
python -m cforge.cli gates --install \
  --source <codex-edit-gates 目录> \
  --install-to .cforge/gates

python -m cforge.cli install-hooks --scripts-dir .cforge/gates
```

第二条会写入 `~/.codex/hooks.json`（自动备份，只增不改）。

### 5. 试跑

```bash
python -m cforge.cli run \
  --workspace ./demo \
  --prompt "创建 hello.py 并打印 hello" \
  --expect-file hello.py
```

---

## 已验证的事实（不是设计意图）

| 项 | 结果 | 验证方式 |
|---|---|---|
| codex 只支持 `wire_api=responses` | 确认 | 实测 `-c` 覆盖报错 |
| proxy 能转 `/responses` → `/chat/completions` | 通过 | `test_proxy_e2e.py` 8 用例 |
| codex 能通过 proxy 跑通完整 turn | **通过** | 真实调 OpenRouter，`messages=['CODEXFORGE OK']`，16.9s |
| 8 个门禁脚本能跑 | 通过 | 真实执行，语法错误准确报出 |
| Goal Guard 拒绝伪完成 | 通过 | `test_supervisor.py` 18 用例 |
| 隔离 CODEX_HOME 消除 auth 重试 | **确认** | 61.8s → 4.4s |
| 测试总数 | **122 passed** | `pytest tests/` |

---

## 踩过的坑（都写进代码注释了）

| 现象 | 根因 | 修复 |
|---|---|---|
| codex 卡 60 秒后失败 | 默认 CODEX_HOME 会刷 ChatGPT token，受限网络下每次都 403 并重试 | `make_isolated_codex_home()`，不复制 `auth.json` |
| `messages` 永远为空 | codex 的真实消息在 `item.completed` 的 `item.text` 里，初版只读顶层 `type` | `CodexEvent.item` / `item_type` / `is_message` |
| 端口 8765 启动报 WinError 10013 | Windows 端口保留区 | `listen_port=0` 由系统分配 |
| 所有请求卡在 SSL 握手 | 残留的 `https_proxy` 环境变量指向已失效端口 | `ProxyHandler({})` 强制直连 |
| 免费模型频繁 429 | OpenRouter `:free` 走**上游共享池**（`limit_source: upstream_provider_shared_pool`） | 降级链 + `rate_limited_shared_pool` 专门分类 |

---

## 与赛道 160 的对应关系

| 评分维度 | 权重 | 本项目提供什么 |
|---|---|---|
| 完整性 · 可复现性 | 30% 内 | 零依赖 + 一条命令体检 + 122 个测试 + 审计链落盘 |
| 完整性 · 文档规范 | 30% 内 | 本文件 + 每模块 docstring 写明设计取舍 |
| **技术难度 · 书生融合** | **30% 内第一要素** | provider 抽象已就位；**待办：真实 key 接入 + 多模态验证** |
| 技术难度 · 前沿技术 | 30% 内 | MCP（待接）、Agent Skills（待接）、长记忆（Goal Guard 审计） |
| 实用性 | 40% | **仍是最大缺口**，需要垂直场景 |

---

## 明确的未完成项

架构骨架阶段刻意不做的事：

1. **多轮监督循环接线**：`SupervisorLoop` 已实现并通过测试，但 `Orchestrator` 只跑一轮，未做「判官驳回 → 重新执行」
2. **SSE 流式**：proxy 目前整包响应，未做 `stream=true` 的事件流
3. **书生的真实接入**：profile 已配好，但缺 key，未做端到端验证
4. **MCP / Agent Skills**：未接
5. **垂直场景**：完全没做（这是 40% 实用性权重的根基）

---

## 许可

本作品原创部分：Apache-2.0。
`codex-edit-gates` 门禁脚本以**独立子进程**方式调用，不包含在本仓库源码内，按其自身许可使用（详见 `../xingzhi_cup/shared/THIRD_PARTY_LICENSES.md`）。
