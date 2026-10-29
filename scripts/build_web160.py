"""构建 160 在线体验站的部署目录（可重建快照）。

与 172 公开仓库同一套原则：**codexforge 是唯一真源，部署目录是它的快照**。
手工改部署目录会丢（重建即覆盖），所有定制必须写在 server.py / index.html 里。

产物结构：
    web160/
      server.py          ← deploy/webdemo/server.py
      cforge/            ← 真源包（排除 .cforge / __pycache__）
      static/
        index.html
        demo.mp4         ← 5.8MB 压缩版演示视频（1080P/4:40）

用法：
    python scripts/build_web160.py --out E:/pyprojects/repos_public/web160
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
VIDEO_CANDIDATES = [
    SRC.parent / "remotion-videos" / "output" / "codexforge-demo-10mb.mp4",
    SRC.parent / "remotion-videos" / "output" / "codexforge-demo.mp4",
]
EXCLUDE_ANY = {"__pycache__", ".cforge", ".pytest_cache", ".mypy_cache"}


def copy_pkg(dst: Path) -> int:
    n = 0
    for f in (SRC / "cforge").rglob("*"):
        if f.is_dir() or any(p in EXCLUDE_ANY for p in f.parts):
            continue
        rel = f.relative_to(SRC)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--key", default="",
                    help="书生 API key；缺省读环境变量 INTERN_API_KEY")
    a = ap.parse_args(argv)

    out = Path(a.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not a.force and not a.dry_run:
        print(f"[X] 目标非空：{out}（加 --force 覆盖）", file=sys.stderr)
        return 2

    video = next((p for p in VIDEO_CANDIDATES if p.is_file()), None)
    if video is None:
        print("[X] 找不到演示视频，先跑 remotion 或确认输出目录", file=sys.stderr)
        return 2

    if a.dry_run:
        n = sum(1 for f in (SRC / "cforge").rglob("*")
                if f.is_file() and not any(p in EXCLUDE_ANY for p in f.parts))
        print(f"[dry] cforge {n} 个文件；视频 {video.name} "
              f"{video.stat().st_size/1024/1024:.1f}MB → {out}")
        return 0

    if out.exists():
        shutil.rmtree(out)
    (out / "static").mkdir(parents=True, exist_ok=True)

    n = copy_pkg(out)
    shutil.copy2(SRC / "deploy" / "webdemo" / "server.py", out / "server.py")
    shutil.copy2(SRC / "deploy" / "webdemo" / "static" / "index.html",
                 out / "static" / "index.html")
    shutil.copy2(video, out / "static" / "demo.mp4")

    # 书生 key：放在部署目录根部（不在 static/ 下，静态处理器只服务 static/，
    # 因此不会被下载）。优先命令行参数，其次环境变量。
    key = (a.key or __import__("os").environ.get("INTERN_API_KEY", "")).strip()
    if key:
        (out / "intern_key.txt").write_text(key, encoding="utf-8")
        print(f"     书生 key 已写入 intern_key.txt（{key[:7]}…{key[-4:]}）")
    else:
        print("[!] 未提供书生 key：线上「书生建议」会走规则兜底", file=sys.stderr)

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[OK] {out}")
    print(f"     cforge {n} 个文件 / 视频 {video.name} "
          f"{video.stat().st_size/1024/1024:.1f}MB / 合计 {total/1024/1024:.1f}MB")
    print(f"     启动：cd {out} && PORT=8000 python server.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
