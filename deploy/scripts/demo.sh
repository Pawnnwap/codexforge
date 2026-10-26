#!/usr/bin/env bash
# CodexForge 体验动线：doctor → quality → guard → 报告。
#
# 这个脚本同时是 **≤5 分钟演示视频的脚本骨架**（Phase 4 直接用它录屏）。
# 每一步都有明确预期输出，评审照着跑就能复现。
#
# 用法：
#   bash deploy/scripts/demo.sh [工作区目录]
# 退出码：0 = 动线全程通过。

set -u
WS="${1:-/tmp/cforge-demo}"
PY="${PYTHON:-python}"

echo "============================================================"
echo "CodexForge 体验动线"
echo "工作区：$WS"
echo "============================================================"

# ---- 0. 准备一个带技术债的演示工作区（可复现的关键：不依赖评委的代码）----
if [ ! -d "$WS" ]; then
    mkdir -p "$WS/src"
    cat > "$WS/src/app.py" <<'PYEOF'
import os
import sys
import json
import re


def parse(text):
    data = json.loads(text)
    if "items" in data:
        return [i for i in data["items"]]
    return []


def render(items):
    out = []
    for i in items:
        out.append(str(i))
    return ",".join(out)
PYEOF
    echo "[setup] 已创建演示工作区 $WS（含 4 个未使用 import 的人为债务）"
fi

echo
echo "---- 第 1 步：环境体检（doctor）----"
"$PY" -m cforge.cli doctor
DOCTOR_RC=$?

echo
echo "---- 第 2 步：整洁度评分（quality，零 token，确定性）----"
"$PY" -m cforge.cli quality --path "$WS" --verbose
QUALITY_RC=$?

echo
echo "---- 第 3 步：守门一轮（guard，scripted 修复器，零 token）----"
"$PY" -m cforge.cli guard --workspace "$WS" --fixer scripted \
    --debt-score 99 --cooldown 0 --max-cycles 1
GUARD_RC=$?

echo
echo "---- 第 4 步：守门后再评分（看前后对比）----"
"$PY" -m cforge.cli quality --path "$WS"
AFTER_RC=$?

echo
echo "---- 报告位置 ----"
ls -la "$WS/.cforge/guardian/" 2>/dev/null || echo "（无报告目录）"

echo
echo "============================================================"
echo "动线结束：doctor=$DOCTOR_RC quality=$QUALITY_RC guard=$GUARD_RC after=$AFTER_RC"
echo "预期：quality 从约 89 分升到 100 分（4 个未使用 import 被精确删除，"
echo "业务逻辑 json.loads / items 判断完好无损）。"
echo "============================================================"

# doctor 允许非零（缺 codex/key 时）；后三步必须为 0 才算动线通过
if [ "$QUALITY_RC" -eq 0 ] && [ "$GUARD_RC" -eq 0 ] && [ "$AFTER_RC" -eq 0 ]; then
    exit 0
fi
exit 1
