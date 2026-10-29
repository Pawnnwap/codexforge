"""AGENTS.md 仓库契约的读取、裁剪与注入断言。

重点验证三件事：
1. 上溯查找能找到（工程里 AGENTS.md 常常不在被改文件的同一层）；
2. 裁剪只保留相关条目（防止把 20KB 约定整份塞进 prompt，把任务淹没）；
3. 注入到 build_fix_prompt 时位置在「约束」之后，且没约定时不占字符。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from cforge.repo_contract import (
    find_contract,
    load_contract,
    render_for_prompt,
    select_rules,
)

SAMPLE = """# 项目规矩

这是一段和改代码无关的介绍文字，应当被裁掉。

## 代码质量

- 所有新增函数必须有 docstring
- 禁止使用裸 except
- 测试必须能在无网络环境下通过

## 发布流程

- 打 tag 前需要更新 CHANGELOG
"""


def test_find_at_root() -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")
    assert find_contract(tmp) == (tmp / "AGENTS.md").resolve()


def test_find_upward_from_subdir() -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")
    sub = tmp / "pkg" / "deep"
    sub.mkdir(parents=True)
    assert find_contract(sub) == (tmp / "AGENTS.md").resolve()


def test_find_stops_at_git_root() -> None:
    """有 .git 就不再往上找，避免误取家目录的约定。"""
    tmp = Path(tempfile.mkdtemp())
    outer = tmp / "outer"
    inner = outer / "repo"
    inner.mkdir(parents=True)
    (outer / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")
    (inner / ".git").mkdir()
    assert find_contract(inner) is None


def test_missing_returns_none() -> None:
    assert find_contract(Path(tempfile.mkdtemp())) is None


def test_select_keeps_relevant_and_drops_noise() -> None:
    body, truncated, rules = select_rules(SAMPLE)
    assert "docstring" in body
    assert "裸 except" in body
    assert "测试必须" in body
    # 无关段落被裁掉
    assert "和改代码无关" not in body
    assert truncated is False
    assert len(rules) >= 3


def test_select_marks_truncation() -> None:
    big = "# 规矩\n\n" + "\n".join(f"- 必须遵守第 {i} 条质量规范" for i in range(200))
    body, truncated, _ = select_rules(big, max_chars=300)
    assert truncated is True
    assert len(body) <= 300 + 40          # 允许截断提示的少量溢出
    assert "已截断" in body


def test_load_contract_shape() -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")
    c = load_contract(tmp)
    assert c is not None
    assert c.raw_chars == len(SAMPLE)
    assert c.used_chars == len(c.text)
    assert c.truncated is False
    assert c.as_dict()["kept_rules"] >= 3


def test_load_empty_returns_none() -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "AGENTS.md").write_text("   \n\n", encoding="utf-8")
    assert load_contract(tmp) is None


def test_render_none_is_empty() -> None:
    assert render_for_prompt(None) == ""


def test_render_mentions_filename() -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")
    out = render_for_prompt(load_contract(tmp))
    assert "AGENTS.md" in out
    assert "仓库约定" in out


def test_build_fix_prompt_injects_contract() -> None:
    """有约定时注入；且注入位置在「约束」之后（问题清单在前）。"""
    from cforge.quality.metrics import evaluate
    from cforge.watch.fixer import build_fix_prompt

    tmp = Path(tempfile.mkdtemp())
    (tmp / "a.py").write_text(
        "import os\n\n\ndef f(x):\n    try:\n        return 1 / x\n    except:\n        return 0\n",
        encoding="utf-8")
    (tmp / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")

    prompt = build_fix_prompt(evaluate(tmp, include_clones=False), tmp)
    assert "仓库约定" in prompt
    assert "禁止使用裸 except" in prompt
    assert prompt.index("要改的地方") < prompt.index("仓库约定")


def test_build_fix_prompt_no_contract_no_overhead() -> None:
    from cforge.quality.metrics import evaluate
    from cforge.watch.fixer import build_fix_prompt

    tmp = Path(tempfile.mkdtemp())
    (tmp / "a.py").write_text(
        "import os\n\n\ndef f(x):\n    try:\n        return 1 / x\n    except:\n        return 0\n",
        encoding="utf-8")
    prompt = build_fix_prompt(evaluate(tmp, include_clones=False), tmp)
    assert prompt
    assert "仓库约定" not in prompt
