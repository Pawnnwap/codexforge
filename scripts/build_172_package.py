"""组装 Track 172 提交包（与 160 完全隔离）。

## 官方规格（getTrack 172 的 submit_way，逐字核对）

外层 zip 命名：`单位简称-作品名称-队长姓名-队长手机号-日期.zip`
限制：单文件 ≤10MB、整包 ≤50MB（upconfig: upsize=50, upisize=10）。
内含两项材料：
  1. 代码托管平台访问链接 —— 写在 word/txt 里；仓库需含完整源码/配置/README
  2. 演示视频（≤5 分钟、MP4、≥1080P）—— 过大才用网盘链接

本作品视频压缩后 5.79MB，直接打进包内即可，不依赖网盘。

用法：
    python scripts/build_172_package.py --out E:/pyprojects/repos_public/xz_submit_172 --date 20261029
"""
from __future__ import annotations

import argparse
import shutil
import time
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
VIDEO_CANDIDATES = [
    SRC.parent / "remotion-videos" / "output" / "codexforge-demo-10mb.mp4",
    SRC.parent / "remotion-videos" / "output" / "codexforge-demo.mp4",
]

REPO = "https://github.com/Pawnnwap/codexforge"
DEMO = "https://codexforge-quality-gate.app.workbuddy.host/?sharecode=C4KEM3JJIR"

NOTE = """CodexForge —— 一人公司的无人值守代码质量守门与持续重构
兴智杯第三届全国人工智能创新应用大赛 · 赛道 172（面向代码生成的生产效率创新方案赛）

一、代码托管平台访问链接
    {repo}

仓库包含完整项目源代码、配置文件及 README 文档（说明项目结构、运行环境与启动方式）。

快速复现：
    git clone {repo}.git
    cd codexforge
    python -m cforge.cli doctor                              # 环境体检（无需安装任何第三方包）
    python -m pytest tests/ -q                               # 390 个测试
    python -m cforge.cli quality --path ./cforge --verbose    # 代码整洁度评估

运行时零第三方依赖（仅 Python 3.11+ 标准库）。

二、演示视频
    文件：CodexForge-演示视频.mp4（包内直接提供，无需网盘）
    规格：MP4 / H.264 / 1920x1080（1080P）/ 30fps / 4 分 40 秒

内容对应官方四项要求：
    1. 需求分析与任务拆解过程演示 —— 守门触发闸门与动作化指令生成段落
    2. AI Coding 工具链使用过程录屏 —— doctor / quality / run_case 终端实录
    3. 代码生成与人工审查优化过程 —— smokeshow 案例的接受与回滚判定
    4. 最终运行效果展示 —— 真实仓库质量分 95.2 → 97.3（+2.1）

三、在线体验（辅助材料，可不看）
    {demo}
    可在线粘贴 Python 代码看整洁度评估与语义守护判定，并调用书生模型给出重构动作。

四、说明
    官方须知第 5 条「以末次提交的有效作品为准」，如需更新将重新提交同一作品。
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--date", default="20261029")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    out = Path(a.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not a.force:
        print(f"[X] 目标非空：{out}（加 --force）", file=sys.stderr)
        return 2

    video = next((p for p in VIDEO_CANDIDATES if p.is_file()), None)
    if video is None:
        print("[X] 找不到演示视频", file=sys.stderr)
        return 2

    stage = out / "pkg"
    if stage.exists():
        # 不用 rmtree：批量删除会被本机安全钩子拦下（实测 state lock timeout），
        # 且删掉就失去上一次提交物的可比对副本。改为改名归档。
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stage.rename(out / f"pkg-{stamp}")
    stage.mkdir(parents=True)

    shutil.copy2(video, stage / "CodexForge-演示视频.mp4")
    (stage / "作品提交说明.txt").write_text(
        NOTE.format(repo=REPO, demo=DEMO), encoding="utf-8")

    name = f"个人-CodexForge-黄太哲-19872081696-{a.date}.zip"
    outer = out / name
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(stage.iterdir()):
            z.write(f, f.name)

    total = outer.stat().st_size / 1024 / 1024
    vid = (stage / "CodexForge-演示视频.mp4").stat().st_size / 1024 / 1024
    with zipfile.ZipFile(outer) as z:
        ok = z.testzip() is None
        names = z.namelist()
    print(f"[OK] {outer.name}")
    print(f"     整包 {total:.2f} MB（限 50）｜视频 {vid:.2f} MB（限 10）｜完整性 {ok}")
    print(f"     条目：{names}")
    return 0 if (total <= 50 and vid <= 10 and ok) else 1


if __name__ == "__main__":
    sys.exit(main())
