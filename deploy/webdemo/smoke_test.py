"""体验站本地冒烟测试：同进程起服务 + 打接口，避免后台进程被回收。

覆盖：
- /healthz
- GET / 返回 HTML
- POST /api/analyze：正常代码 / 语法错误代码 / 空代码
- POST /api/guard：示例破坏必须判"拒绝"（这是语义守护的核心断言）
- GET /demo.mp4 的 Range 请求（视频拖动播放依赖）
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r"E:/pyprojects/repos_public/web160")
sys.path.insert(0, str(ROOT))

from server import Handler  # noqa: E402

PORT = 8731
BASE = f"http://127.0.0.1:{PORT}"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, obj: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with op.open(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.6)

    fails = []

    def check(name, cond, extra=""):
        print(("  OK   " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))
        if not cond:
            fails.append(name)

    try:
        # 1 healthz
        with op.open(BASE + "/api/health", timeout=10) as r:
            check("healthz", json.loads(r.read().decode()).get("ok") is True)

        # 2 首页
        with op.open(BASE + "/", timeout=10) as r:
            html = r.read().decode("utf-8", "replace")
            check("首页 HTML", "CodexForge" in html and len(html) > 3000,
                  f"{len(html)} 字节")

        # 3 分析：长函数
        with op.open(BASE + "/api/samples", timeout=10) as r:
            s = json.loads(r.read().decode())
        samples = s["samples"]
        r1 = post("/api/analyze", {"filename": "s.py",
                                   "code": samples["长函数"]["code"]})
        kinds = [x["kind"] for x in r1.get("smells", [])]
        check("分析-长函数", "long_function" in kinds,
              f"score={r1.get('score')} smells={len(kinds)}")

        r2 = post("/api/analyze", {"filename": "b.py",
                                   "code": samples["裸 except + 未使用 import"]["code"]})
        k2 = [x["kind"] for x in r2.get("smells", [])]
        check("分析-裸except/未用import",
              any("except" in k for k in k2) and any("import" in k for k in k2),
              f"score={r2.get('score')} smells={k2}")

        # 4 语法错误不应 500，而是转成 high smell
        r3 = post("/api/analyze", {"filename": "x.py", "code": "def f(:\n  pass\n"})
        check("分析-语法错误返回 high",
              any(x["kind"] == "syntax_error" for x in r3.get("smells", [])),
              f"score={r3.get('score')}")

        # 5 空代码 400
        try:
            post("/api/analyze", {"filename": "e.py", "code": ""})
            check("空代码应 400", False)
        except urllib.error.HTTPError as e:
            check("空代码应 400", e.code == 400)

        # 6 语义守护：示例必须判拒绝
        gc = s["guard_case"]
        g = post("/api/guard", {"filename": "cli.py",
                                "before": gc["before"], "after": gc["after"]})
        check("守护-拒绝行为破坏", g.get("guard_ok") is False,
              f"verdict={g.get('verdict')} reason={str(g.get('guard_reason'))[:46]}")

        # 7 语义守护：等价改写应通过
        same = gc["before"]
        g2 = post("/api/guard", {"filename": "cli.py", "before": same, "after": same})
        check("守护-相同代码通过", g2.get("guard_ok") is True,
              f"verdict={g2.get('verdict')}")

        # 8 视频 Range
        req = urllib.request.Request(BASE + "/demo.mp4")
        req.add_header("Range", "bytes=0-1023")
        with op.open(req, timeout=20) as r:
            chunk = r.read()
            check("视频 Range 206", r.status == 206 and len(chunk) == 1024,
                  f"status={r.status} bytes={len(chunk)}")

        # 9 healthz 应暴露模型与额度信息
        with op.open(BASE + "/api/health", timeout=10) as r:
            h = json.loads(r.read().decode())
        check("api/health 含模型/额度",
              "intern_key_configured" in h and "quota" in h,
              f"key={h.get('intern_key_configured')} quota={h.get('quota')}")

        # 10 key 文件绝不能通过 HTTP 读到（安全红线）
        leaked = False
        for probe in ("/intern_key.txt", "/static/../intern_key.txt",
                      "/server.py", "/static/../server.py"):
            try:
                with op.open(BASE + probe, timeout=10) as r:
                    body = r.read().decode("utf-8", "replace")
                    if "sk-" in body or "ThreadingHTTPServer" in body:
                        leaked = True
            except urllib.error.HTTPError:
                pass
        check("key/源码不可下载", not leaked)

        # 11 书生建议（快速档，真实调用）
        s2 = post("/api/suggest", {
            "filename": "s.py", "depth": "fast",
            "code": "def f(x):\n    try:\n        return 1 / x\n    except:\n        return 0\n"
        })
        ok_model = s2.get("source") == "intern" and bool(s2.get("suggestion"))
        check("书生建议-快速档", ok_model or s2.get("degraded") is True,
              f"source={s2.get('source')} model={s2.get('model')} "
              f"{s2.get('elapsed','?')}s → {str(s2.get('suggestion'))[:34]}")
        if not ok_model:
            print("       （降级原因：{0}）".format(s2.get("reason")))

        # 12 相同代码应命中缓存，不重复花钱
        s3 = post("/api/suggest", {
            "filename": "s.py", "depth": "fast",
            "code": "def f(x):\n    try:\n        return 1 / x\n    except:\n        return 0\n"
        })
        check("相同代码走缓存", s3.get("cached") is True,
              f"cached={s3.get('cached')}")

    finally:
        srv.shutdown()

    print()
    if fails:
        print(f"[X] {len(fails)} 项失败：{fails}")
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
