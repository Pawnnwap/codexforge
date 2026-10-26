# 部署与体验（Track 160 提交物「应用体验链接」的落地页）

## 体验动线（= 演示视频脚本骨架）

```bash
# 本地（零 token，不调 LLM）
bash deploy/scripts/demo.sh /tmp/cforge-demo
```

四步：`doctor`（环境体检）→ `quality`（整洁度评分）→ `guard`（守门一轮，
scripted 修复器精确删除未使用 import）→ 再 `quality`（看前后对比）。
预期输出：约 89 → 100 分，业务逻辑完好。

## 本地 Docker 构建

```bash
docker build -f deploy/Dockerfile -t codexforge:0.3.0 ..
docker run --rm codexforge:0.3.0 doctor
docker run --rm -v /tmp/cforge-demo:/ws codexforge:0.3.0 \
    guard --workspace /ws --fixer scripted --debt-score 99 --cooldown 0 --max-cycles 1
```

镜像内含 codex CLI 0.163.0（npm 全局，版本锁定）+ 标准库运行时。
**不含任何第三方 pip 包**——零依赖是架构决策，评审复现不装包。

## 公网部署（应用体验链接）

体验链接要求公网可访问（官方待确认项 Q10，大概率不接受内网）。
Vultr VM（已有）一条命令：

```bash
# 在 VM 上
docker build -t codexforge . # 或用 scp 传镜像 tar
docker run -d --name cforge-demo -p 8080:8080 \
    -e CFORGE_BACKEND=openrouter -e OPENROUTER_API_KEY=sk-or-... \
    codexforge guard --workspace /demo --fixer codex --poll 300
```

> **容量规划（提交包相关）**：镜像本体 < 400MB，**不进 50MB 提交包**。
> 提交包只含 Dockerfile + deploy/ 脚本（几十 KB）；需要镜像的评审
> 按 README 自行 build，或从网盘拉取（链接写进「模型下载.txt」同级说明）。

## 书生模式（key 到位后）

```bash
docker run --rm \
    -e CFORGE_BACKEND=intern \
    -e INTERN_API_KEY=sk-... \
    -e CFORGE_MODEL=intern-s2-preview \
    codexforge:0.3.0 guard --workspace /ws --fixer codex
```

model id 以 `intern_probe.py` 实测为准（赛题正文与 match_encourage
两处表述不一致，不硬编码）。

## 待办（验收状态）

- [ ] 本机无 Docker → 本仓库只做 Dockerfile 语法校验 + demo.sh dry-run
- [ ] 真构建 + 公网部署在 Vultr VM 执行后回填本文件（附 URL 与截图）
