"""技能库单测：catalog 不含正文、按需加载、路径穿越、LRU、加载上限。

核心断言是「**未请求时正文不进 catalog**」——这是对抗 codex 技能注入
污染的设计底线，退化成「catalog 含正文」就等于把污染搬进了自己产品。
"""
from __future__ import annotations

import pytest

from cforge.skills.skill_bank import SkillBank


@pytest.fixture()
def bank() -> SkillBank:
    return SkillBank()


class TestCatalog:
    def test_builtin_catalog_nonempty(self, bank):
        catalog = bank.catalog()
        assert len(catalog) >= 4  # 4 个内置技能
        names = {m.name for m in catalog}
        assert {"code-guardian", "debt-triage", "clone-refactor",
                "context-hygiene"} <= names

    def test_catalog_contains_only_meta(self, bank):
        """catalog 只有 name/description——正文绝不随 catalog 流出。"""
        for meta in bank.catalog():
            body = meta.path.read_text(encoding="utf-8")
            # description 是 frontmatter 的一行摘要，不是正文段落
            assert len(meta.description) < 200
            assert "## 执行步骤" not in meta.description

    def test_render_catalog_prompt_has_no_body(self, bank):
        prompt = bank.render_catalog_prompt()
        assert "可用技能" in prompt
        assert "LOAD_SKILL" in prompt          # 给了按需加载的用法
        assert "## 触发条件" not in prompt      # 正文章节不在


class TestLoad:
    def test_load_returns_body_without_frontmatter(self, bank):
        body = bank.load("code-guardian")
        assert "代码守门评估" in body
        assert not body.lstrip().startswith("---")

    def test_unknown_skill_raises_keyerror(self, bank):
        with pytest.raises(KeyError):
            bank.load("no-such-skill")

    def test_illegal_name_rejected(self, bank):
        """路径穿越第一道闸：名字格式。"""
        for bad in ("../../etc/passwd", "a/b", "UPPER", "with space", ""):
            with pytest.raises(ValueError):
                bank.load(bad)

    def test_resolve_check_blocks_symlink_escape(self, tmp_path):
        """第二道闸：符号链接逃逸——正则拦不住这种，resolve 校验拦。"""
        outside = tmp_path / "outside.md"
        outside.write_text("---\nname: evil\ndescription: x\n---\nsecret", encoding="utf-8")
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        link = skills_dir / "evil.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("平台不支持符号链接")
        bank = SkillBank(skills_dir)
        with pytest.raises(ValueError, match="越界"):
            bank.load("evil")

    def test_load_limit_per_turn(self, bank):
        """单轮加载上限防「加载链」耗尽上下文。"""
        bank.load("code-guardian")
        with pytest.raises(ValueError, match="上限"):
            bank.load("debt-triage")
        bank.begin_turn()
        bank.load("debt-triage")  # 新一轮可以再加载

    def test_lru_eviction(self, tmp_path):
        """resident set 超限时淘汰最久未用。"""
        skills_dir = tmp_path / "s"
        skills_dir.mkdir()
        for i in range(4):
            (skills_dir / f"s{i}.md").write_text(
                f"---\nname: s{i}\ndescription: d{i}\n---\nbody{i}", encoding="utf-8")
        bank = SkillBank(skills_dir, max_resident=2, max_loads_per_turn=10)
        bank.load("s0")
        bank.load("s1")
        bank.load("s0")           # 触达 s0 → s1 变最久未用
        bank.load("s2")           # 淘汰 s1
        assert "s0" in bank._resident
        assert "s2" in bank._resident
        assert "s1" not in bank._resident


class TestInstall:
    def test_install_builtin_copies_files(self, bank, tmp_path):
        dest = tmp_path / "installed"
        copied = bank.install_builtin(dest)
        assert len(copied) >= 4
        assert all(p.is_file() for p in copied)
        # 安装后的目录可直接当技能库用
        installed_bank = SkillBank(dest)
        assert len(installed_bank.catalog()) == len(copied)


class TestPathContainmentCrossPlatform:
    """技能目录边界校验必须跨平台正确。

    原实现用 `str(path).startswith(str(dir) + "/")` —— 在 Windows 上恒为 False
    （`str(Path)` 用反斜杠，与硬编码的 "/" 永不匹配），
    靠 `path.parent != dir` 兜底才「看起来能用」。
    这个缺陷是靠「克隆后首跑偶发失败」暴露的，不是逻辑审查发现的。
    """

    def test_normal_skill_inside_dir_passes(self, tmp_path):
        """正常技能必须能加载 —— 这在旧实现里靠兜底分支才通过。"""
        from cforge.skills.skill_bank import SkillBank

        d = tmp_path / "skills"
        d.mkdir()
        (d / "ok.md").write_text(
            "---\nname: ok\ndescription: fine\n---\nbody text", encoding="utf-8")
        bank = SkillBank(d)
        assert "body text" in bank.load("ok")

    def test_subdir_file_is_contained(self, tmp_path):
        """直接验证 is_relative_to 语义：子目录内的文件算「在内」。"""
        from pathlib import Path

        root = (tmp_path / "skills").resolve()
        root.mkdir()
        sub = root / "deep"
        sub.mkdir()
        f = (sub / "x.md").resolve()
        assert f.is_relative_to(root)

    def test_traversal_outside_dir_not_contained(self, tmp_path):
        from pathlib import Path

        root = (tmp_path / "skills").resolve()
        root.mkdir()
        outside = (tmp_path / "outside.md").resolve()
        outside.write_text("x", encoding="utf-8")
        assert not outside.is_relative_to(root)


class TestPathContainmentCrossPlatform:
    """技能目录边界校验必须跨平台正确。

    原实现用 `str(path).startswith(str(dir) + "/")` —— 在 Windows 上恒为 False
    （`str(Path)` 用反斜杠，与硬编码的 "/" 永不匹配），
    靠 `path.parent != dir` 兜底才「看起来能用」。
    这个缺陷是靠「克隆后首跑偶发失败」暴露的，不是逻辑审查发现的。
    """

    def test_normal_skill_inside_dir_passes(self, tmp_path):
        """正常技能必须能加载 —— 这在旧实现里靠兜底分支才通过。"""
        from cforge.skills.skill_bank import SkillBank

        d = tmp_path / "skills"
        d.mkdir()
        (d / "ok.md").write_text(
            "---\nname: ok\ndescription: fine\n---\nbody text", encoding="utf-8")
        bank = SkillBank(d)
        assert "body text" in bank.load("ok")

    def test_subdir_file_is_contained(self, tmp_path):
        """直接验证 is_relative_to 语义：子目录内的文件算「在内」。"""
        from pathlib import Path

        root = (tmp_path / "skills").resolve()
        root.mkdir()
        sub = root / "deep"
        sub.mkdir()
        f = (sub / "x.md").resolve()
        assert f.is_relative_to(root)

    def test_traversal_outside_dir_not_contained(self, tmp_path):
        from pathlib import Path

        root = (tmp_path / "skills").resolve()
        root.mkdir()
        outside = (tmp_path / "outside.md").resolve()
        outside.write_text("x", encoding="utf-8")
        assert not outside.is_relative_to(root)
