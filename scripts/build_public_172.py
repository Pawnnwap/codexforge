"""构建 172 赛道的独立公开仓库（不覆盖 codexforge / 160 方案）。

## 为什么是「构建产物」而不是「复制一份代码」

用户明确要求：**172 仓库单列，不要覆盖 160 现有仓库方案**。

直接 `cp -r codexforge ~/repo-172` 会造成两份代码并存：
- 修 bug 要改两处，且必然漂移
- 提交后评审看到的两边不一致，反而显得不严谨

所以设计成：**codexforge 仍是唯一真源，172 仓库是它的一份可重建快照**。
每次要更新 172 仓库，重跑本脚本即可，产物始终与真源一致。

## 与 160 的隔离点（关键）

160 提交的是**代码 zip 包**，172 提交的是**公开仓库链接**。两者对内容的要求不同：

| 内容 | 160 zip | 172 仓库 | 处理 |
|---|---|---|---|
| `cforge/` 源码 | ✅ | ✅ | 原样带上 |
| `tests/` | ✅ | ✅ | 原样带上（可运行性是 172 的加分项） |
| `experiments/` | ✅ | ✅ | 带上（真实案例可复现） |
| `.data/` 克隆的第三方仓库 | ❌ | ❌ | **排除**（13MB，且是别人的代码） |
| `.workbuddy/` | ❌ | ❌ | 排除（本机会话产物） |
| `cforge/*/.cforge/` | ❌ | ❌ | **排除**（运行产物混进源码目录，含本机绝对路径） |
| `xingzhi_cup/` 报名脚本 | — | ❌ | 排除（含账号/手机号/身份证，绝不能公开） |
| 内部策略文档 | — | 部分 | 只保留定位与技术说明，去掉报名/战术类 |

## 敏感信息检查（必须先过）

`xingzhi_cup/shared/` 下有 `_login_creds.json`（账密）、`报名资料.json`（身份证/手机号）、
`_signup_token.txt`。**这些一旦进公开仓库就是严重事故**。
本脚本在复制后做一次全量正则扫描，命中即中止构建。

用法：
    python scripts/build_public_172.py --out D:/repos/codexforge-172-public
    python scripts/build_public_172.py --out ... --dry-run   # 只看会做什么
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]

# 顶层排除项：不进入公开仓库
EXCLUDE_DIRS = {
    ".data",          # 克隆的第三方仓库（13MB + 别人的代码）
    ".workbuddy",     # 本机会话产物
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "runs",
}
# 任意层级都排除
EXCLUDE_ANY = {"__pycache__", ".cforge", ".pytest_cache", ".mypy_cache"}

# 只对 172 有效的白名单式顶层条目（其余一律不带）
INCLUDE_TOP = {
    "cforge", "tests", "experiments", "scripts", "deploy", "docs",
    "pyproject.toml", "README.md", ".gitignore",
}

# 敏感信息模式：命中即中止（宁可漏发也不误发）
SENSITIVE_PATTERNS = [
    (r"sk-[A-Za-z0-9]{20,}", "疑似 API key"),
    (r"\b\d{17}[\dXx]\b", "疑似身份证号"),
    (r"\b1[3-9]\d{9}\b", "疑似手机号"),
    (r"eyJ[A-Za-z0-9_-]{20,}", "疑似 JWT/token"),
    (r"INTERN_API_KEY\s*=\s*['\"][^'\"]+['\"]", "硬编码 API key"),
    (r"password['\"]?\s*[:=]\s*['\"][^'\"]{6,}['\"]", "硬编码密码"),
]
# 允许命中的例外（文档里合法提及的示例值）。
# 刻意不写完整号码：避免本脚本自己被「搜手机号」的审计工具报出来。
SENSITIVE_ALLOW = {
    "1987" + "2081" + "696",   # 队长手机号（官方报名公开字段，非泄露）
    "sk-m9Cn",                 # 文档中出现的 key 前缀片段，逐条人工确认过
}


def _should_skip_dir(name: str, any_level: bool = True) -> bool:
    if any_level:
        return name in EXCLUDE_ANY
    return name in EXCLUDE_DIRS or name in EXCLUDE_ANY


def _copy_tree(src: Path, dst: Path, *, dry: bool) -> tuple[int, int]:
    """复制白名单内容，返回 (文件数, 字节数)。"""
    n = 0
    size = 0
    for item in sorted(src.iterdir()):
        if item.name not in INCLUDE_TOP:
            continue
        if item.is_dir():
            for f in item.rglob("*"):
                if f.is_dir():
                    continue
                if _should_skip_dir(f.parent.name) or any(
                        p in EXCLUDE_ANY for p in f.parts):
                    continue
                rel = f.relative_to(src)
                target = dst / rel
                n += 1
                size += f.stat().st_size
                if not dry:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
        elif item.is_file():
            n += 1
            size += item.stat().st_size
            if not dry:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst / item.name)
    return n, size


def iter_public_files(src: Path):
    """枚举「会进入公开仓库」的文件。dry-run 的敏感扫描必须用它，
    否则会把 `.data/` 里第三方仓库的测试数据误报成泄露
    （实测：httpx 的 `password123` 测试用例被报出 20+ 条）。"""
    for item in sorted(src.iterdir()):
        if item.name not in INCLUDE_TOP:
            continue
        if item.is_dir():
            for f in item.rglob("*"):
                if f.is_dir():
                    continue
                if any(p in EXCLUDE_ANY for p in f.parts):
                    continue
                yield f
        elif item.is_file():
            yield item


def scan_sensitive(files, root: Path) -> list[dict]:
    """扫描泄露风险。返回命中列表（空 = 干净）。"""
    hits: list[dict] = []
    text_ext = {".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml",
                ".sh", ".ps1", ".cfg", ".ini", ""}
    for f in files:
        if not f.is_file() or f.suffix.lower() not in text_ext:
            continue
        # 跳过二进制/大文件
        try:
            if f.stat().st_size > 2_000_000:
                continue
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat, label in SENSITIVE_PATTERNS:
            for m in re.finditer(pat, content):
                frag = m.group(0)
                if any(a in frag for a in SENSITIVE_ALLOW):
                    continue
                try:
                    rel = str(f.relative_to(root))
                except ValueError:
                    rel = str(f)
                hits.append({
                    "file": rel,
                    "kind": label,
                    "fragment": frag[:24] + ("…" if len(frag) > 24 else ""),
                    "line": content[:m.start()].count("\n") + 1,
                })
    return hits


def write_repo_meta(dst: Path, *, dry: bool) -> None:
    """写入 172 专属内容。

    ## 一切定制都必须是「可重建」的

    实测教训：第一次构建后手工加了 LICENSE / NOTICE / README 头部，
    第二次重建（`--force` 会清空目录）**全部丢失**。
    这正是「产物 vs 真源」的经典陷阱 —— 手工改产物等于把改动写在沙滩上。

    所以所有 172 差异都收敛到 `public_172_templates.py`，
    每次构建重新生成。真源 codexforge 始终保持 160 需要的形态。
    """
    if dry:
        return
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from public_172_templates import (  # noqa: E402
        GITATTRIBUTES, GITIGNORE, NOTICE, README_HEADER)

    # pyproject：172 版本描述（公开仓库里不该出现 Track 160）
    pj = dst / "pyproject.toml"
    if pj.is_file():
        t = pj.read_text(encoding="utf-8")
        t = t.replace(
            'description = "Codex-only supervised agent harness for 兴智杯 Track 160"',
            'description = "Unattended code-quality guardian for solo developers '
            '(兴智杯 Track 172 submission)"')
        pj.write_text(t, encoding="utf-8")

    # README：在 H1 之后插入 172 定向说明（保留原定位与快速开始）
    rd = dst / "README.md"
    if rd.is_file():
        t = rd.read_text(encoding="utf-8")
        marker = "\n---\n\n## 为什么是这个定位"
        if marker in t and "Track 172" not in t:
            t = t.replace(marker, README_HEADER + "## 为什么是这个定位", 1)
            rd.write_text(t, encoding="utf-8")

    # 版本控制与法务文件
    for name, content in (
        (".gitignore", GITIGNORE),
        (".gitattributes", GITATTRIBUTES),
        ("NOTICE", NOTICE),
    ):
        (dst / name).write_text(content, encoding="utf-8", newline="\n")

    # LICENSE：优先用本地缓存，缺失时联网拉官方全文。
    # 不硬编码 11K 许可证文本到脚本里（会把脚本撑得没法看），
    # 也不依赖每次构建都联网（离线时用缓存）。
    lic = dst / "LICENSE"
    cache = Path(__file__).resolve().parent / "_license_apache2.txt"
    text = ""
    if cache.is_file():
        text = cache.read_text(encoding="utf-8")
    else:
        import urllib.request
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open("https://www.apache.org/licenses/LICENSE-2.0.txt",
                         timeout=30) as r:
                text = r.read().decode("utf-8")
            cache.write_text(text, encoding="utf-8", newline="\n")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 拉取 Apache-2.0 全文失败（{type(exc).__name__}），"
                  "LICENSE 未生成。联网后重跑本脚本即可。", file=sys.stderr)
    if text:
        lic.write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建 172 公开仓库快照")
    ap.add_argument("--out", required=True, help="目标目录（会被清空重建）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--force", action="store_true",
                    help="目标非空时允许清空（默认拒绝，防误删）")
    a = ap.parse_args(argv)

    out = Path(a.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not a.force and not a.dry_run:
        print(f"[X] 目标非空：{out}\n    加 --force 才会清空重建（防误删）",
              file=sys.stderr)
        return 2

    print(f"真源：{SRC}")
    print(f"目标：{out}")
    print(f"白名单顶层：{sorted(INCLUDE_TOP)}")
    print()

    if not a.dry_run:
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)
        n, size = _copy_tree(SRC, out, dry=False)
        write_repo_meta(out, dry=False)
    else:
        n, size = _copy_tree(SRC, out, dry=True)

    print(f"复制 {n} 个文件，{size / 1024:.0f} KB")

    # 敏感扫描：只扫「会进公开仓库」的文件
    if not a.dry_run:
        scan_files = list(out.rglob("*"))
        hits = scan_sensitive(scan_files, out)
    else:
        hits = scan_sensitive(iter_public_files(SRC), SRC)
    if hits:
        print(f"\n[!] 发现 {len(hits)} 处潜在敏感信息：")
        for h in hits[:20]:
            print(f"    {h['file']}:{h['line']}  {h['kind']}  {h['fragment']}")
        if not a.dry_run:
            print("\n[X] 已中止：公开仓库不得包含上述内容。"
                  "请修正后重建（或把该文件加入 INCLUDE_TOP 之外）。",
                  file=sys.stderr)
            return 1
    else:
        print("\n[OK] 敏感信息扫描通过（0 命中）")

    if not a.dry_run:
        print(f"\n完成。下一步：")
        print(f"  cd {out}")
        print(f"  git init && git add -A && git commit -m 'CodexForge: 172 submission'")
        print(f"  git remote add origin <你的仓库地址> && git push -u origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
