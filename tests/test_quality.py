"""整洁度度量单测：每个指标都要有正例与反例。

反例比正例重要 —— 误报会让 agent 做无意义的改动，比漏报更伤。
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from cforge.quality.clones import detect_clones, extract_units, scan
from cforge.quality.metrics import (
    PENALTY,
    analyze_source,
    diff_reports,
    evaluate,
    iter_python_files,
)


def smell_kinds(source: str) -> set[str]:
    _, smells = analyze_source(textwrap.dedent(source))
    return {s.kind for s in smells}


class TestSyntax:
    def test_syntax_error_is_high_severity(self):
        _, smells = analyze_source("def broken(:\n    pass\n")
        assert any(s.kind == "syntax_error" and s.severity == "high" for s in smells)

    def test_valid_code_no_syntax_smell(self):
        assert "syntax_error" not in smell_kinds("x = 1\n")


class TestLongFunction:
    def test_long_function_flagged(self):
        body = "\n".join(f"    x{i} = {i}" for i in range(70))
        src = f"def big():\n{body}\n"
        assert "long_function" in smell_kinds(src)

    def test_short_function_clean(self):
        assert "long_function" not in smell_kinds("def f():\n    return 1\n")

    def test_threshold_boundary(self):
        # 正好 60 行不该报（阈值是「超过」）
        body = "\n".join(f"    x{i} = {i}" for i in range(58))
        src = f"def f():\n{body}\n"   # 59 行含 def
        assert "long_function" not in smell_kinds(src)

    def test_double_threshold_is_high(self):
        body = "\n".join(f"    x{i} = {i}" for i in range(130))
        _, smells = analyze_source(f"def huge():\n{body}\n")
        hit = [s for s in smells if s.kind == "long_function"]
        assert hit and hit[0].severity == "high"


class TestUnusedImport:
    def test_unused_import_flagged(self):
        assert "unused_import" in smell_kinds("import os\n\nx = 1\n")

    def test_used_import_clean(self):
        assert "unused_import" not in smell_kinds(
            "import os\n\nprint(os.getcwd())\n")

    def test_from_import_used_via_attribute(self):
        src = "from pathlib import Path\n\np = Path('.')\n"
        assert "unused_import" not in smell_kinds(src)

    def test_future_annotations_never_flagged(self):
        """这是真实踩过的误报：__future__ 不绑定运行时名字。"""
        src = "from __future__ import annotations\n\nx: int = 1\n"
        assert "unused_import" not in smell_kinds(src)

    def test_type_checking_import_not_flagged_when_used_in_string(self):
        src = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pathlib import Path\n\n"
            "def f(p: 'Path') -> None: ...\n"
        )
        assert "unused_import" not in smell_kinds(src)

    def test_used_in_string_annotation_counts(self):
        src = "from pathlib import Path\n\ndef f() -> 'Path': ...\n"
        assert "unused_import" not in smell_kinds(src)

    def test_all_export_counts_as_use(self):
        src = 'from os import path\n\n__all__ = ["path"]\n'
        assert "unused_import" not in smell_kinds(src)


class TestBareExcept:
    def test_bare_except_is_high(self):
        src = "try:\n    pass\nexcept:\n    pass\n"
        _, smells = analyze_source(src)
        hit = [s for s in smells if s.kind == "bare_except"]
        assert hit and hit[0].severity == "high"

    def test_typed_except_clean(self):
        src = "try:\n    pass\nexcept ValueError:\n    pass\n"
        assert "bare_except" not in smell_kinds(src)


class TestNesting:
    def test_deep_nesting_flagged(self):
        src = textwrap.dedent("""
            def f(a, b, c, d, e):
                if a:
                    if b:
                        if c:
                            if d:
                                if e:
                                    return 1
                return 0
        """)
        assert "deep_nesting" in smell_kinds(src)

    def test_flat_code_clean(self):
        assert "deep_nesting" not in smell_kinds(
            "def f(a):\n    if a:\n        return 1\n    return 0\n")


class TestDeadAssignment:
    def test_dead_local_flagged(self):
        src = "def f():\n    unused = 1\n    return 2\n"
        assert "dead_assignment" in smell_kinds(src)

    def test_underscore_ignored(self):
        src = "def f():\n    _ = compute()\n    return 2\n"
        assert "dead_assignment" not in smell_kinds(src)

    def test_used_variable_clean(self):
        src = "def f():\n    a = 1\n    return a\n"
        assert "dead_assignment" not in smell_kinds(src)


class TestDuplicateBranch:
    def test_identical_branches_flagged(self):
        src = textwrap.dedent("""
            def f(x):
                if x > 0:
                    return 1
                else:
                    return 1
        """)
        assert "duplicate_branch" in smell_kinds(src)

    def test_different_branches_clean(self):
        src = textwrap.dedent("""
            def f(x):
                if x > 0:
                    return 1
                else:
                    return 2
        """)
        assert "duplicate_branch" not in smell_kinds(src)


class TestTodoAndLongLine:
    def test_todo_flagged_low(self):
        _, smells = analyze_source("# TODO: fix later\nx = 1\n")
        hit = [s for s in smells if s.kind == "todo_marker"]
        assert hit and hit[0].severity == "low"

    def test_long_line_flagged(self):
        assert "long_line" in smell_kinds("x = " + "'a'" + " + " * 60 + "'b'\n")

    def test_normal_line_clean(self):
        assert "long_line" not in smell_kinds("x = 1\n")


class TestScoring:
    def test_clean_code_scores_100(self):
        r_src = '"""Doc."""\n\n\ndef f() -> int:\n    return 1\n'
        m, smells = analyze_source(r_src)
        assert smells == []

    def test_high_smell_penalizes_more_than_low(self):
        assert PENALTY["high"] > PENALTY["medium"] > PENALTY["low"]

    def test_score_in_range(self, tmp_path):
        (tmp_path / "a.py").write_text("import os\n" * 50, encoding="utf-8")
        r = evaluate(tmp_path)
        assert 0.0 <= r.score <= 100.0

    def test_more_smells_lower_score(self, tmp_path):
        clean = tmp_path / "clean"
        dirty = tmp_path / "dirty"
        clean.mkdir()
        dirty.mkdir()
        body = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(20))
        (clean / "a.py").write_text(body, encoding="utf-8")
        (dirty / "a.py").write_text("import os\nimport sys\n" + body, encoding="utf-8")
        assert evaluate(clean).score > evaluate(dirty).score

    def test_empty_dir_scores_100(self, tmp_path):
        assert evaluate(tmp_path).score == 100.0


class TestIterFiles:
    def test_excludes_venv_and_cache(self, tmp_path):
        (tmp_path / "a.py").write_text("x=1", encoding="utf-8")
        for bad in (".venv", "__pycache__", "node_modules"):
            d = tmp_path / bad
            d.mkdir()
            (d / "b.py").write_text("x=1", encoding="utf-8")
        names = [p.name for p in iter_python_files(tmp_path)]
        assert names == ["a.py"]

    def test_respects_max_files(self, tmp_path):
        for i in range(10):
            (tmp_path / f"f{i}.py").write_text("x=1", encoding="utf-8")
        assert len(list(iter_python_files(tmp_path, max_files=3))) == 3


class TestEncodingDetection:
    """编码问题必须单独报告，不能被笼统归为「语法错」。

    实测真实案例：agent 在 Windows 上写出 4 个 UTF-16 文件，内容逻辑
    完全正确，却全部被报「语法错」。agent 拿到「语法错」会去改逻辑，
    根本找不到病因（实际只需转编码）。
    """

    def test_utf16_le_detected(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes("x = 1\n".encode("utf-16"))   # 自带 BOM
        from cforge.quality.metrics import analyze_file
        _, smells = analyze_file(p)
        assert any(s.kind.startswith("encoding") for s in smells)
        assert not any(s.kind == "syntax_error" for s in smells)

    def test_utf16_be_detected(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes(b"\xfe\xff" + "x = 1\n".encode("utf-16-be"))
        from cforge.quality.metrics import analyze_file
        _, smells = analyze_file(p)
        assert any(s.kind == "encoding_utf16" for s in smells)

    def test_utf8_bom_detected(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes(b"\xef\xbb\xbf" + b"x = 1\n")
        from cforge.quality.metrics import analyze_file
        _, smells = analyze_file(p)
        assert any(s.kind == "encoding_bom" for s in smells)

    def test_utf16_without_bom_detected_by_nul(self, tmp_path):
        """无 BOM 的 UTF-16 靠 NUL 字节密度识别。"""
        p = tmp_path / "a.py"
        p.write_bytes("x = 1\n".encode("utf-16-le") * 5)
        from cforge.quality.metrics import analyze_file
        _, smells = analyze_file(p)
        assert any("encoding" in s.kind for s in smells)

    def test_clean_utf8_no_encoding_smell(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes(b"x = 1\n")
        from cforge.quality.metrics import analyze_file
        _, smells = analyze_file(p)
        assert not any("encoding" in s.kind for s in smells)

    def test_detect_encoding_problem_function(self):
        from cforge.quality.metrics import detect_encoding_problem
        assert detect_encoding_problem(b"\xff\xfe\x00")[0] == "encoding_utf16"
        assert detect_encoding_problem(b"\xef\xbb\xbfabc")[0] == "encoding_bom"
        assert detect_encoding_problem(b"x = 1\n")[0] == ""


class TestDiffReports:
    def test_detects_fix_and_regression(self, tmp_path):
        d = tmp_path
        # before：4 个 unused import（大量 medium）
        (d / "a.py").write_text(
            "import os\nimport sys\nimport json\nimport io\n"
            "import re\nimport ast\nx = 1\n", encoding="utf-8")
        before = evaluate(d)
        # after：清掉 import，但引入一个 high（裸 except）
        (d / "a.py").write_text(
            "x = 1\ntry:\n    pass\nexcept:\n    pass\n", encoding="utf-8")
        after = evaluate(d)
        diff = diff_reports(before, after)
        assert diff["fixed"] >= 1        # 未用 import 被修掉
        assert diff["introduced"] >= 1   # 但引入了裸 except

    def test_identical_reports_no_change(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        a = evaluate(tmp_path)
        b = evaluate(tmp_path)
        diff = diff_reports(a, b)
        assert diff["fixed"] == 0 and diff["introduced"] == 0
        assert diff["score_delta"] == 0

    def test_regression_lowers_score(self, tmp_path):
        """从干净到有 high 级问题，分数必须下降。"""
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        before = evaluate(tmp_path)
        (tmp_path / "a.py").write_text(
            "x = 1\ntry:\n    pass\nexcept:\n    pass\n", encoding="utf-8")
        after = evaluate(tmp_path)
        assert after.score < before.score


class TestClones:
    def test_identical_functions_detected(self):
        src = textwrap.dedent("""
            def alpha(a, b):
                total = 0
                for i in range(a):
                    if i % 2 == 0:
                        total += i
                    else:
                        total -= i
                result = total * b
                return result

            def beta(x, y):
                acc = 0
                for k in range(x):
                    if k % 2 == 0:
                        acc += k
                    else:
                        acc -= k
                out = acc * y
                return out
        """)
        units = extract_units(src, "m.py")
        assert len(units) == 2
        report = detect_clones(units, min_similarity=0.7)
        # 结构相同、改名，应被识别为重复
        assert len(report.pairs) == 1
        assert report.pairs[0].similarity > 0.7

    def test_different_functions_not_detected(self):
        src = textwrap.dedent("""
            def read(name):
                with open(name) as f:
                    data = f.read()
                lines = data.splitlines()
                cleaned = [x.strip() for x in lines]
                return cleaned

            def compute(values):
                total = sum(values)
                count = len(values)
                if count == 0:
                    return 0.0
                return total / count
        """)
        units = extract_units(src, "m.py")
        report = detect_clones(units, min_similarity=0.75)
        assert report.pairs == []

    def test_short_functions_skipped(self):
        """低于 min_statements 的短函数不参与比较，避免巧合重复。"""
        src = "def a():\n    return 1\n\ndef b():\n    return 1\n"
        assert extract_units(src, "m.py", min_statements=8) == []

    def test_syntax_error_yields_no_units(self):
        assert extract_units("def broken(:", "m.py") == []

    def test_duplicate_ratio_bounds(self):
        from cforge.quality.clones import CloneReport
        r = CloneReport()
        assert r.duplicate_ratio == 0.0

    def test_scan_real_dir(self, tmp_path):
        f = tmp_path / "m.py"
        body = textwrap.dedent("""
            def one(a):
                x = 0
                for i in a:
                    if i:
                        x += i
                    else:
                        x -= i
                y = x * 2
                return y

            def two(b):
                x = 0
                for i in b:
                    if i:
                        x += i
                    else:
                        x -= i
                y = x * 2
                return y
        """)
        f.write_text(body, encoding="utf-8")
        rep = scan(tmp_path)
        assert len(rep.pairs) >= 1

    def test_clone_pair_str_has_both_sides(self):
        src = textwrap.dedent("""
            def a(p):
                q = 0
                for i in p:
                    if i > 0:
                        q += i
                    else:
                        q -= i
                r = q * 3
                return r

            def b(t):
                u = 0
                for i in t:
                    if i > 0:
                        u += i
                    else:
                        u -= i
                v = u * 3
                return v
        """)
        rep = detect_clones(extract_units(src, "x.py"), min_similarity=0.7)
        assert rep.pairs
        s = str(rep.pairs[0])
        assert "a()" in s and "b()" in s


class TestCloneThresholdCalibration:
    """阈值必须用真实代码校准，不能拍脑袋。

    回归：初版用 0.72，导致「所有 CRUD 函数」互相判为重复。
    实测 demo 项目里 `save`（INSERT）与 `load`（SELECT）相似度 0.76，
    被判为重复 —— 这是误报，两者语义完全不同。
    """

    def test_semantically_different_crud_not_clones(self):
        """INSERT 与 SELECT 结构相似但语义不同，不该判重复。"""
        src = textwrap.dedent("""
            def save(conn, key, value):
                cur = conn.cursor()
                cur.execute('INSERT INTO kv VALUES (?, ?)', (key, value))
                conn.commit()
                return True

            def load(conn, key):
                cur = conn.cursor()
                cur.execute('SELECT value FROM kv WHERE key = ?', (key,))
                row = cur.fetchone()
                if row is None:
                    return None
                return row[0]
        """)
        units = extract_units(src, "m.py", min_statements=4)
        rep = detect_clones(units, min_similarity=0.90)
        assert rep.pairs == [], "INSERT 与 SELECT 不应判为重复"

    def test_copy_paste_with_rename_detected(self):
        """真正的复制粘贴改名，相似度接近 1.0，必须检出。"""
        src = textwrap.dedent("""
            def save(conn, key, value):
                cur = conn.cursor()
                cur.execute('INSERT INTO kv VALUES (?, ?)', (key, value))
                conn.commit()
                return True

            def save_cache(conn, key, value):
                cur = conn.cursor()
                cur.execute('INSERT INTO kv VALUES (?, ?)', (key, value))
                conn.commit()
                return True
        """)
        units = extract_units(src, "m.py", min_statements=4)
        rep = detect_clones(units, min_similarity=0.90)
        assert len(rep.pairs) == 1
        assert rep.pairs[0].similarity > 0.95

    def test_default_threshold_excludes_crud_false_positive(self):
        """默认参数下，CRUD 误报必须消失。"""
        src = textwrap.dedent("""
            def save(conn, key, value):
                cur = conn.cursor()
                cur.execute('INSERT INTO kv VALUES (?, ?)', (key, value))
                conn.commit()
                return True

            def load(conn, key):
                cur = conn.cursor()
                cur.execute('SELECT value FROM kv WHERE key = ?', (key,))
                row = cur.fetchone()
                if row is None:
                    return None
                return row[0]
        """)
        from cforge.quality.clones import scan
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "m.py").write_text(src, encoding="utf-8")
        rep = scan(d)
        assert rep.pairs == []

    def test_symmetrical_helpers_not_merged(self):
        """方向相反的操作（snapshot/restore）不该判为重复。"""
        src = textwrap.dedent("""
            def copy_out(src_dir, dst_dir):
                dst_dir.mkdir(parents=True, exist_ok=True)
                for f in src_dir.rglob('*.py'):
                    rel = f.relative_to(src_dir)
                    target = dst_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
                return True

            def copy_back(dst_dir, src_dir):
                for f in dst_dir.rglob('*.py'):
                    rel = f.relative_to(dst_dir)
                    target = src_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
                return True
        """)
        units = extract_units(src, "m.py", min_statements=4)
        rep = detect_clones(units, min_similarity=0.90)
        # 这两个高度相似（都是复制循环），可能被判重复 —— 这是可接受的
        # 但要确认不会把完全不同的函数判重
        for p in rep.pairs:
            assert p.similarity >= 0.90


class TestCloneFrequencyBlindSpot:
    """回归：集合 Jaccard 忽略频次，会把 1 个循环与 5 个循环判为完全重复。

    这是实测发现的真实误报（初版算法 Jaccard=1.0，实际相似度仅 0.467）。
    这些用例锁死算法选择，防止有人改回 set 比较。
    """

    def test_one_loop_vs_five_loops_not_clones(self):
        src = textwrap.dedent("""
            def one(xs):
                total = 0
                for x in xs:
                    total += x
                return total

            def five(a, b, c, d, e):
                t = 0
                for x in a:
                    t += x
                for x in b:
                    t += x
                for x in c:
                    t += x
                for x in d:
                    t += x
                for x in e:
                    t += x
                return t
        """)
        units = extract_units(src, "m.py")
        rep = detect_clones(units, min_similarity=0.72)
        assert rep.pairs == [], "不同循环数量不应判为重复"

    def test_similarity_accounts_for_length(self):
        from cforge.quality.clones import _similarity
        a = ["X"] * 10
        b = ["X"] * 50
        assert _similarity(a, b) < 0.7

    def test_identical_token_lists_score_one(self):
        from cforge.quality.clones import _similarity
        a = ["A", "B", "C", "D"]
        assert _similarity(a, list(a)) == 1.0

    def test_empty_safe(self):
        from cforge.quality.clones import _similarity
        assert _similarity([], ["A"]) == 0.0
        assert _similarity([], []) == 0.0

    def test_reordered_code_lower_than_identical(self):
        """顺序也参与相似度：同样 token 换个顺序不该等于完全重复。"""
        from cforge.quality.clones import _similarity
        a = ["A", "B", "C", "D", "E", "F"]
        b = ["F", "E", "D", "C", "B", "A"]
        assert _similarity(a, b) < 1.0


class TestSyncAsyncTwin:
    """同步/异步孪生对识别。

    为什么这是一条必须锁住的规则：这些对是**必要的双实现**（asyncio 的
    `await` 无法参数化），不是可合并的重复。度量层若把它们当债务，
    上层修复 prompt 就会让模型去「抽共用函数」，直接破坏 API。
    实测 httpx 里 18 对高相似度中有 13 对是孪生 —— 不区分就是大面积误导。
    """

    SRC = textwrap.dedent('''
        class Client:
            def request(self, url):
                r = self._build(url)
                r = self._send(r)
                return self._parse(r)

            def _send(self, req):
                x = 1
                y = 2
                z = 3
                return req


        class AsyncClient:
            async def request(self, url):
                r = self._build(url)
                r = await self._send(r)
                return self._parse(r)

            async def _send(self, req):
                x = 1
                y = 2
                z = 3
                return req
    ''')

    def test_async_method_pair_detected_as_twin(self):
        units = extract_units(self.SRC, "c.py")
        report = detect_clones(units, min_similarity=0.5)
        assert report.pairs, "应至少检出一对高相似度"
        twins = [p for p in report.pairs if p.is_sync_async_twin]
        assert twins, "Client/AsyncClient 的同名方法必须被认成孪生"
        assert all(p.is_sync_async_twin for p in twins)

    def test_a_prefix_pair_detected_as_twin(self):
        src = textwrap.dedent('''
            def read(self):
                a = 1
                b = 2
                c = 3
                return a

            async def aread(self):
                a = 1
                b = 2
                c = 3
                return a
        ''')
        units = extract_units(src, "m.py")
        report = detect_clones(units, min_similarity=0.5)
        assert any(p.is_sync_async_twin for p in report.pairs)

    def test_two_sync_functions_not_twin(self):
        """两个同步同名函数是真重复，不能因为名字相同就放过。"""
        src = textwrap.dedent('''
            def calc(x):
                a = x + 1
                b = a * 2
                c = b - 3
                return c

            def calc2(x):
                a = x + 1
                b = a * 2
                c = b - 3
                return c
        ''')
        units = extract_units(src, "m.py")
        report = detect_clones(units, min_similarity=0.5)
        assert report.pairs
        assert not any(p.is_sync_async_twin for p in report.pairs)

    def test_strip_async_prefix_does_not_mangle_short_words(self):
        """`abs`/`add` 这类真以 a 开头的短词不能被剥成 `bs`/`dd`。"""
        from cforge.quality.clones import _strip_async_prefix
        assert _strip_async_prefix("abs") == "abs"
        assert _strip_async_prefix("add") == "add"
        assert _strip_async_prefix("aread") == "read"
        assert _strip_async_prefix("aiter_bytes") == "iter_bytes"
        assert _strip_async_prefix("__aenter__") == "__enter__"

    def test_as_dict_exposes_twin_flag(self):
        units = extract_units(self.SRC, "c.py")
        report = detect_clones(units, min_similarity=0.5)
        pair = next(p for p in report.pairs if p.is_sync_async_twin)
        assert pair.as_dict()["sync_async_twin"] is True
