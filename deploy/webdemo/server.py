"""CodexForge 在线体验服务（Track 160「应用体验链接」）。

## 设计取舍：为什么是「真跑分析」而不是展示页

官方要求「应用体验链接」，静态截图页没有说服力。这里把确定性分析能力
（AST 整洁度评分 + 语义守护）直接暴露成 HTTP 接口，评审粘贴一段代码
就能看到真实结果——和本地 CLI 跑出来的是同一套逻辑（直接 import cforge）。

## 只暴露确定性能力，不暴露需要 LLM 的部分

线上环境没有 codex 可执行文件、也不应该让陌生人消耗模型额度。
所以体验页限定了两个**零成本、可复现**的能力：
1. `POST /api/analyze` — 整洁度评估（纯 ast，毫秒级）
2. `POST /api/guard`   — 语义守护（比对改动前后是否破坏行为）

这两项恰好是本作品「反馈信号来自代码事实」的核心，也是最容易被验证的部分。

## 依赖

仅 Python 标准库 + 本仓库 cforge 包（其本身零第三方依赖）。
绑定 0.0.0.0:$PORT，符合托管平台的单端口要求。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cforge.quality.metrics import evaluate                      # noqa: E402
from cforge.quality.polish import NetImprovementGate             # noqa: E402

STATIC = ROOT / "static"
MAX_BODY = 512 * 1024          # 单次提交源码上限 512KB
MAX_CODE_LEN = 400_000
_STARTED_AT = time.time()

# ---------------- 书生大模型接入 ----------------
# 两档模型：快速档给交互用（实测 2.1s 出答案），深度档演示旗舰能力（约 30s）。
# 实测教训：intern-s2 的思考链与正文共享 completion 预算，
# max_tokens 给 700 会停在推理中途（finish=length），必须留足余量。
INTERN_URL = "https://chat.intern-ai.org.cn/api/v1/chat/completions"
MODELS = {
    # 快速档：实测 2.0s、finish=stop，交互体验最好
    "fast": {"model": "intern-s1-mini", "max_tokens": 700, "timeout": 30},
    # 深度档：实测 12.0s、finish=stop、1308 tokens。
    # 刻意不用 intern-s2（旗舰）：它带长思考链，实测 26s 仍未收敛，
    # 而托管平台的反向代理在 60s 会直接切断连接 —— 客户端拿不到任何响应，
    # 比「降级提示」更糟。35B 落在 12s，留足网关余量。
    "deep": {"model": "intern-s2-preview-35b", "max_tokens": 1500, "timeout": 45},
}
KEY_FILE = ROOT / "intern_key.txt"

# 限额：按 IP 限速 + 全局日配额。公网接口开着，不设限会被刷爆额度。
RATE_WINDOW = 600            # 10 分钟
RATE_LIMIT = {"fast": 6, "deep": 2}
DAILY_CAP = 400              # 全站每天最多调用次数
_rate: dict[str, list[float]] = {}
_daily = {"day": "", "count": 0}
_cache: dict[str, dict] = {}
CACHE_MAX = 200


def _intern_key() -> str:
    key = os.environ.get("INTERN_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.is_file():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def _rate_ok(ip: str, kind: str) -> tuple[bool, str]:
    """按 IP 的滑动窗口限速 + 全局日配额。"""
    now = time.time()
    today = time.strftime("%Y%m%d")
    if _daily["day"] != today:
        _daily["day"], _daily["count"] = today, 0
    if _daily["count"] >= DAILY_CAP:
        return False, f"今日全站模型调用额度（{DAILY_CAP} 次）已用完，请明天再试"

    stamps = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
    limit = RATE_LIMIT.get(kind, 4)
    if len(stamps) >= limit:
        wait = int(RATE_WINDOW - (now - stamps[0])) + 1
        return False, f"请求过于频繁：每 10 分钟最多 {limit} 次，请 {wait} 秒后再试"

    stamps.append(now)
    _rate[ip] = stamps
    _daily["count"] += 1
    return True, ""


def _call_intern(kind: str, system_prompt: str, user_prompt: str) -> dict:
    """调用书生接口。失败抛异常，由上层决定降级。"""
    cfg = MODELS[kind]
    key = _intern_key()
    if not key:
        raise RuntimeError("服务端未配置 INTERN_API_KEY")

    body = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": cfg["max_tokens"],
    }).encode("utf-8")

    req = urllib.request.Request(INTERN_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.time()
    with op.open(req, timeout=cfg["timeout"]) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))

    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    return {
        "model": cfg["model"],
        "text": _clean_answer(text),
        "finish": choice.get("finish_reason"),
        "usage": data.get("usage") or {},
        "elapsed": round(time.time() - started, 1),
    }


def _clean_answer(text: str) -> str:
    """从模型输出里取出「一条动作」。

    ## 为什么需要这层清洗（实测）

    这个接口的模型即使传 `chat_template_kwargs.enable_thinking=false`
    **仍然会输出思考过程**，而且思考链会混进 content。实测一次真实输出：
        "所以，正确的修改是将第2行的try后面加上冒号……"
    这句话是错的（代码本来就有冒号），只是推理中途的自我复述。
    直接展示给评审 = 让模型的自言自语冒充结论。

    所以提示词强制 JSON 输出（实测 2.2s 稳定返回 {"action": "..."}），
    解析失败再退到「引号结论」和「最后一行有效文本」。
    """
    if not text:
        return ""

    # 1) 优先解析 JSON（找最后一个花括号块，容忍前置的思考文本）
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", text))):
        try:
            obj = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            continue
        for key in ("action", "建议", "suggestion", "answer"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    # 2) 引号里的结论
    quoted = re.findall(r"[\"“]([^\"”\n]{6,80})[\"”]", text)
    if quoted:
        return quoted[-1].strip()

    # 3) 最后一段非空、非元话语的行
    noise = ("thinking", "analyze", "let me", "wait", "okay", "action:",
             "characters", "所以，", "因此，", "正确的修改是")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        low = ln.lower()
        if any(low.startswith(n) for n in noise):
            continue
        if len(ln) >= 6:
            return ln
    return lines[-1] if lines else ""


def _rule_based_hint(smells: list[dict]) -> str:
    """模型不可用时的确定性兜底：按 smell 类型给动作化建议。

    这不是「假装 AI 成功了」—— 前端会明确标注「规则兜底，未调用模型」。
    动作文案与 cforge/watch/fixer.py 的 `_ACTIONS` 保持一致，
    避免「线上建议」和「本地修复器」说两套话。
    """
    if not smells:
        return "未检出明显可改进项，保持现状即可。"
    kind = smells[0]["kind"]
    line = smells[0]["line"]
    table = {
        "long_function": f"把第 {line} 行的长函数按职责拆成两个，原函数名保留并转调。",
        "bare_except": f"把第 {line} 行的裸 except 改成 except Exception，并记录或重抛。",
        "unused_import": f"删除第 {line} 行未使用的 import。",
        "deep_nesting": f"用提前返回打平第 {line} 行的深层嵌套。",
        "dead_assignment": f"删除第 {line} 行赋值后从未被读取的变量。",
        "duplicate_branch": f"合并第 {line} 行重复的条件分支，或修正写错的条件。",
        "long_line": f"把第 {line} 行折到 100 字符以内，不改变语义。",
        "syntax_error": "先修复语法错误，再做其它改动。",
        "todo_marker": "TODO 标记不影响评分，按计划处理即可。",
        "read_error": "该文件无法读取，先确认路径与编码。",
    }
    return table.get(kind, f"针对第 {line} 行的 {kind} 问题做最小改动。")


SAMPLES = {    "长函数": {
        "filename": "service.py",
        "code": '''"""示例：一个 60 行以上的函数，会被判 long_function。"""


def handle_request(payload):
    result = {}
    if "user" in payload:
        user = payload["user"]
        result["name"] = user.get("name", "")
        result["email"] = user.get("email", "")
        result["age"] = user.get("age", 0)
        if result["age"] < 0:
            result["age"] = 0
        if result["age"] > 150:
            result["age"] = 150
        result["is_adult"] = result["age"] >= 18
        if result["is_adult"]:
            result["category"] = "adult"
        else:
            result["category"] = "minor"
    if "order" in payload:
        order = payload["order"]
        result["order_id"] = order.get("id", "")
        result["amount"] = order.get("amount", 0)
        result["currency"] = order.get("currency", "CNY")
        if result["amount"] < 0:
            result["amount"] = 0
        if result["amount"] > 1000000:
            result["amount"] = 1000000
        result["tax"] = round(result["amount"] * 0.06, 2)
        result["total"] = round(result["amount"] + result["tax"], 2)
    if "meta" in payload:
        meta = payload["meta"]
        result["source"] = meta.get("source", "unknown")
        result["trace"] = meta.get("trace", "")
        result["version"] = meta.get("version", "1")
        if not result["trace"]:
            result["trace"] = result["source"] + "-" + result["version"]
    if "flags" in payload:
        flags = payload["flags"]
        result["debug"] = bool(flags.get("debug", False))
        result["dry_run"] = bool(flags.get("dry_run", False))
        if result["debug"] and not result["dry_run"]:
            result["verbose"] = True
        else:
            result["verbose"] = False
    if "retry" in payload:
        retry = payload["retry"]
        result["retries"] = int(retry.get("count", 0))
        result["backoff_ms"] = int(retry.get("backoff_ms", 200))
        result["max_backoff_ms"] = int(retry.get("max_backoff_ms", 5000))
        if result["retries"] < 0:
            result["retries"] = 0
        if result["retries"] > 10:
            result["retries"] = 10
        if result["backoff_ms"] < 1:
            result["backoff_ms"] = 1
        if result["backoff_ms"] > result["max_backoff_ms"]:
            result["backoff_ms"] = result["max_backoff_ms"]
        result["estimated_wait_ms"] = result["retries"] * result["backoff_ms"]
    if "paging" in payload:
        paging = payload["paging"]
        result["page"] = int(paging.get("page", 1))
        result["page_size"] = int(paging.get("page_size", 20))
        result["offset"] = (result["page"] - 1) * result["page_size"]
        if result["page"] < 1:
            result["page"] = 1
            result["offset"] = 0
        if result["page_size"] > 200:
            result["page_size"] = 200
    if "audit" in payload:
        audit = payload["audit"]
        result["actor"] = audit.get("actor", "system")
        result["action"] = audit.get("action", "unknown")
        result["ts"] = audit.get("ts", 0)
        result["signed"] = bool(audit.get("signed", False))
        if result["signed"] and not result["actor"]:
            result["signed"] = False
    return result
''',
    },
    "裸 except + 未使用 import": {
        "filename": "legacy.py",
        "code": '''import os
import sys
import json


def read_config(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except:
        return {}


def save_config(path, data):
    try:
        with open(path, "w") as fh:
            json.dump(data, fh)
    except:
        pass
''',
    },
    "行为破坏（对照用）": {
        "filename": "cli.py",
        "code": '''import sys


def run(text):
    try:
        print(text)
    except Exception as exc:
        print(exc, file=sys.stderr)
''',
    },
}

GUARD_CASE = {
    "filename": "cli.py",
    "before": '''import sys


def run(text):
    try:
        print(text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
''',
    "after": '''def run(text):
    try:
        print(text)
    except Exception:
        print("error")
''',
    "note": "典型「清理优化」：删了 import sys、把 ValueError 放宽成 Exception、"
            "错误输出从 stderr 改成 stdout。整洁度分数可能不降反升，但行为已经变了。",
}


def _report_to_dict(report) -> dict:
    return {
        "score": report.score,
        "sloc": report.total_sloc,
        "n_files": len(report.files),
        "n_functions": sum(f.n_functions for f in report.files),
        "duplicate_ratio": round(report.duplicate_ratio, 4),
        "clone_pairs": report.clone_pairs,
        "smells": [
            {"kind": s.kind, "path": s.path, "line": s.line,
             "detail": s.detail, "severity": s.severity}
            for s in sorted(report.smells,
                            key=lambda x: ({"high": 0, "medium": 1}.get(x.severity, 2),
                                           x.path, x.line))
        ],
        "by_severity": {
            "high": len(report.by_severity("high")),
            "medium": len(report.by_severity("medium")),
            "low": len(report.by_severity("low")),
        },
    }


def analyze_code(filename: str, code: str) -> dict:
    """把源码落到临时目录再跑 evaluate —— 复用与 CLI 完全一致的评分逻辑，
    避免在体验页里重写一遍评分公式（那样线上/线下结论就可能不一致）。"""
    tmp = Path(tempfile.mkdtemp(prefix="cforge-web-"))
    try:
        safe = Path(filename or "snippet.py").name or "snippet.py"
        if not safe.endswith(".py"):
            safe += ".py"
        (tmp / safe).write_text(code, encoding="utf-8")
        report = evaluate(tmp, include_clones=False)
        d = _report_to_dict(report)
        d["filename"] = safe
        return d
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def guard_compare(filename: str, before: str, after: str) -> dict:
    """语义守护 + 分数对比。先给结论，再给证据。"""
    safe = Path(filename or "snippet.py").name or "snippet.py"
    if not safe.endswith(".py"):
        safe += ".py"

    gate = NetImprovementGate()
    ok, reason = gate.judge_sources({safe: before}, {safe: after})

    r_before = analyze_code(safe, before)
    r_after = analyze_code(safe, after)
    delta = round(r_after["score"] - r_before["score"], 1)

    if not ok:
        verdict = "拒绝（行为破坏）"
        explain = reason
    elif delta >= 0.5:
        verdict = "接受（净改善）"
        explain = f"分数 {r_before['score']} → {r_after['score']}（+{delta}），语义守护未发现破坏。"
    else:
        verdict = "回滚（无改善）"
        explain = f"分数 {r_before['score']} → {r_after['score']}（{delta:+}），未达到最小改善幅度。"

    return {
        "filename": safe,
        "guard_ok": ok,
        "guard_reason": reason,
        "verdict": verdict,
        "explain": explain,
        "delta": delta,
        "before": r_before,
        "after": r_after,
    }


def suggest(code: str, filename: str, depth: str, ip: str) -> dict:
    """先算确定性报告，再让书生模型给一条动作化建议。

    顺序刻意如此：即使模型超时/限流，也一定返回确定性的评估结果 ——
    页面永远不会只剩一个错误。
    """
    report = analyze_code(filename, code)
    smells = report.get("smells") or []

    if not smells:
        return {"report": report, "suggestion": "未检出明显可改进项，保持现状即可。",
                "source": "rule", "degraded": False, "model": None}

    # 缓存键：同一段代码 + 同一档位，10 分钟内不重复花钱
    ck = f"{depth}:{report.get('score')}:{len(smells)}:{hash(code) & 0xffffffff}"
    hit = _cache.get(ck)
    if hit and time.time() - hit["ts"] < 600:
        return {**hit["data"], "cached": True}

    ok, why = _rate_ok(ip, depth)
    if not ok:
        return {"report": report, "suggestion": _rule_based_hint(smells),
                "source": "rule", "degraded": True, "reason": why, "model": None}

    smell_txt = "\n".join(f"- {s['kind']}（第 {s['line']} 行，{s['severity']}）：{s['detail']}"
                          for s in smells[:5])
    system_prompt = (
        "你是资深 Python 代码审查员。用户给出已检出问题与源码，"
        "你只输出一个 JSON 对象，键为 action，值为一条可直接执行的重构动作："
        "中文、不超过 40 字、动词开头、指明第几行改成什么。"
        "不要输出解释、不要代码块标记、不要思考过程。"
        "示例：{\"action\": \"将第4行 except 改为 except ZeroDivisionError\"}"
    )
    user_prompt = f"已检出问题：\n{smell_txt}\n\n源码：\n```python\n{code[:4000]}\n```"

    try:
        r = _call_intern(depth, system_prompt, user_prompt)
        answer = r["text"] or _rule_based_hint(smells)
        data = {"report": report, "suggestion": answer, "source": "intern",
                "model": r["model"], "elapsed": r["elapsed"],
                "usage": r["usage"], "finish": r["finish"], "degraded": False}
        if len(_cache) < CACHE_MAX:
            _cache[ck] = {"ts": time.time(), "data": data}
        return data
    except Exception as exc:  # noqa: BLE001
        return {"report": report, "suggestion": _rule_based_hint(smells),
                "source": "rule", "degraded": True,
                "reason": f"模型调用失败（{type(exc).__name__}），已降级为规则建议",
                "model": None}


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexForgeDemo/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志，避免托管平台日志刷屏
        return

    # ---------- 基础响应 ----------
    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大（上限 512KB）")
        raw = self.rfile.read(n)
        return json.loads(raw.decode("utf-8", "replace"))

    # ---------- 静态文件（含视频 Range 支持） ----------
    def _serve_static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        size = target.stat().st_size
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                spec = rng[6:].split("-")
                start = int(spec[0]) if spec[0] else 0
                end = int(spec[1]) if len(spec) > 1 and spec[1] else size - 1
                start = max(0, start)
                end = min(size - 1, end)
                if start > end:
                    raise ValueError
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with target.open("rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except (ValueError, OSError):
                pass
        self._send(200, target.read_bytes(), ctype,
                   {"Accept-Ranges": "bytes"})

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path in ("/healthz", "/api/health"):
            # 注意：托管平台会拦截 /healthz 并返回它自己的 {"code":200,"data":"Pod alive"}，
            # 所以对外监控请用 /api/health（这个才是本服务的真实状态）。
            today = time.strftime("%Y%m%d")
            used = _daily["count"] if _daily["day"] == today else 0
            self._json(200, {
                "ok": True,
                "service": "codexforge-demo",
                "uptime_s": int(time.time() - _STARTED_AT),
                "intern_key_configured": bool(_intern_key()),
                "models": {k: v["model"] for k, v in MODELS.items()},
                "quota": {"daily_cap": DAILY_CAP, "used_today": used},
                "cache_entries": len(_cache),
            })
            return
        if path == "/api/samples":
            self._json(200, {"samples": SAMPLES, "guard_case": GUARD_CASE})
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path == "/demo.mp4":
            self._serve_static("demo.mp4")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": f"请求解析失败：{exc}"})
            return

        try:
            if path == "/api/analyze":
                code = str(body.get("code", ""))
                if not code.strip():
                    self._json(400, {"error": "代码为空"})
                    return
                if len(code) > MAX_CODE_LEN:
                    self._json(400, {"error": "代码过长（上限 400KB）"})
                    return
                self._json(200, analyze_code(str(body.get("filename", "snippet.py")), code))
                return

            if path == "/api/guard":
                before = str(body.get("before", ""))
                after = str(body.get("after", ""))
                if not before.strip() or not after.strip():
                    self._json(400, {"error": "before / after 均不能为空"})
                    return
                self._json(200, guard_compare(
                    str(body.get("filename", "snippet.py")), before, after))
                return

            if path == "/api/suggest":
                code = str(body.get("code", ""))
                depth = str(body.get("depth", "fast"))
                if depth not in MODELS:
                    depth = "fast"
                if not code.strip():
                    self._json(400, {"error": "代码为空"})
                    return
                if len(code) > 20_000:
                    self._json(400, {"error": "代码过长（上限 20KB，模型调用需要控制成本）"})
                    return
                ip = (self.headers.get("X-Forwarded-For") or
                      self.client_address[0] or "unknown").split(",")[0].strip()
                self._json(200, suggest(code, str(body.get("filename", "snippet.py")),
                                        depth, ip))
                return
        except Exception:  # noqa: BLE001
            # 把真实异常返回给前端便于自查，但不泄露路径以外的信息
            self._json(500, {"error": "分析失败", "trace": traceback.format_exc(limit=3)})
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"[codexforge-demo] serving on http://{host}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
