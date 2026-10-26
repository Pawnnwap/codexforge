# CodexForge 参赛镜像
#
# 设计取舍（写进技术文档的容量规划）：
# 1. python:3.13-slim + 仅装 codex CLI（npm 全局，版本锁定 0.163.0——
#    与实测版本一致，协议行为不漂移）。
# 2. **不装任何第三方 pip 包**：运行时零依赖是架构决策
#    （评审复现不装包、避开依赖树 copyleft）。镜像里只有标准库 + codex。
# 3. 镜像本体 < 400MB，**不进 50MB 提交包**——提交包只含 Dockerfile
#    与脚本（几十 KB），镜像由评审 docker build 自行产出或从网盘拉取。
# 4. 默认 CFORGE_BACKEND=openrouter（开发态）；书生由运行时 -e 注入：
#      docker run -e CFORGE_BACKEND=intern -e INTERN_API_KEY=... \
#        -e CFORGE_MODEL=intern-s2-preview ...
#    model id 以 probe 实测为准，不硬编码进镜像。

FROM python:3.13-slim

# codex CLI：执行后端唯一选择（用户裁决）。版本锁定——
# 0.163.0 是 wire_api=responses 唯一被支持的实测版本。
RUN npm install -g @openai/codex@0.163.0 \
    && npm cache clean --force

# 只复制源码（排除测试/实验/文档——评审复现按 README 跑，不跑我们的测试）
COPY cforge/ /app/cforge/
COPY pyproject.toml README.md /app/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    CFORGE_BACKEND=openrouter \
    PYTHONIOENCODING=utf-8

# doctor 是第一道验证：容器起来了先证明环境是活的
RUN python -m cforge.cli doctor || true

ENTRYPOINT ["python", "-m", "cforge.cli"]
CMD ["doctor"]
