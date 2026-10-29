"""修复分级（repair tier）的行为断言。

分级是「自动改还是只建议」的唯一依据，写错会导致自动修复越权，
所以这里把分类、阻断、以及 actionable_text 的输出格式都钉死。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from cforge.quality.metrics import Smell, evaluate
from cforge.quality.repair import (
    BLOCKING,
    TIERS,
    is_auto_fixable,
    is_blocking,
    repair_tier,
    summarize,
)


def test_auto_tier_is_exactly_unused_import() -> None:
    """auto 档必须只有能证明安全的修复，多一个都不行。

    这条断言是护栏：以后想往 auto 里加类型，必须先改这里，
    等于强制过一道「是否真的可证安全」的评审。
    """
    auto = {k for k, v in TIERS.items() if v == "auto"}
    assert auto == {"unused_import"}


def test_tier_classification() -> None:
    assert repair_tier("unused_import") == "auto"
    assert repair_tier("bare_except") == "suggest"
    assert repair_tier("long_function") == "suggest"
    assert repair_tier("todo_marker") == "advisory"
    assert repair_tier("read_error") == "advisory"


def test_unknown_kind_falls_back_to_suggest() -> None:
    """未知类型不能默认成 auto —— 宁可只建议，也不能自动改。"""
    assert repair_tier("some_new_smell") == "suggest"


def test_blocking_kinds() -> None:
    assert is_blocking("syntax_error")
    assert is_blocking("read_error")
    assert not is_blocking("unused_import")
    assert BLOCKING == {"syntax_error", "read_error"}


def test_smell_properties() -> None:
    s = Smell("unused_import", "a.py", 3, "`os` 未被使用", "medium")
    assert s.repair_tier == "auto"
    assert s.blocking is False
    assert s.as_dict()["repair_tier"] == "auto"

    bad = Smell("syntax_error", "b.py", 1, "invalid syntax", "high")
    assert bad.blocking is True


def test_summarize_counts() -> None:
    got = summarize(["unused_import", "unused_import", "bare_except", "todo_marker"])
    assert got == {"auto": 2, "suggest": 1, "advisory": 1}


def test_actionable_text_has_tiers_and_self_check() -> None:
    """补上的 ACI 闭环：必须给出自检命令与目标分数。"""
    code = "import os\nimport sys\n\n\ndef f(x):\n    try:\n        return 1 / x\n    except:\n        return 0\n"
    tmp = Path(tempfile.mkdtemp())
    (tmp / "a.py").write_text(code, encoding="utf-8")
    report = evaluate(tmp, include_clones=False)

    text = report.actionable_text(workspace=tmp)
    assert "可自动" in text and "需判断" in text
    assert "[可自动]" in text            # unused_import 行带标记
    assert "改完自检" in text
    assert str(tmp) in text
    # 目标分数 = 当前 + 0.5，向上取整显示
    assert f"{round(report.score + 0.5):.0f}" in text


def test_actionable_text_empty_when_clean() -> None:
    report = evaluate(Path(tempfile.mkdtemp()), include_clones=False)
    assert report.actionable_text() == ""


def test_auto_fixable_helper() -> None:
    assert is_auto_fixable("unused_import") is True
    assert is_auto_fixable("bare_except") is False


def test_scripted_fixer_skips_when_blocking() -> None:
    """有语法错误时不自动修复：语法不成立时任何重写都可能扩大破坏。"""
    from cforge.quality.metrics import QualityReport
    from cforge.watch.fixer import ScriptedFixAgent

    tmp = Path(tempfile.mkdtemp())
    f = tmp / "bad.py"
    f.write_text("import os\n\ndef broken(:\n    pass\n", encoding="utf-8")
    before = f.read_text(encoding="utf-8")

    report = QualityReport()
    report.smells = [
        Smell("syntax_error", "bad.py", 3, "invalid syntax", "high"),
        Smell("unused_import", "bad.py", 1, "`os` 未被使用", "medium"),
    ]
    agent = ScriptedFixAgent()
    assert agent.fix(report, tmp, 1) is False
    assert f.read_text(encoding="utf-8") == before   # 一个字节都没动


def test_scripted_fixer_only_touches_auto_tier() -> None:
    """只有 auto 档的问题会被真改；suggest/advisory 保持原样。"""
    from cforge.quality.metrics import QualityReport
    from cforge.watch.fixer import ScriptedFixAgent

    tmp = Path(tempfile.mkdtemp())
    f = tmp / "a.py"
    f.write_text("import os\nimport sys\n\n\ndef f():\n    return sys.argv\n",
                 encoding="utf-8")
    report = QualityReport()
    report.smells = [
        Smell("unused_import", "a.py", 1, "`os` 未被使用", "medium"),
        Smell("todo_marker", "a.py", 4, "TODO 标记", "low"),
    ]
    agent = ScriptedFixAgent()
    assert agent.fix(report, tmp, 1) is True
    after = f.read_text(encoding="utf-8")
    assert "import os" not in after          # auto 档已修
    assert "import sys" in after             # 仍被使用，保留
