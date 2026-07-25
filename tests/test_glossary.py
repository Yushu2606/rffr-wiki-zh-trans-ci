"""术语表测试。"""
from __future__ import annotations

from pathlib import Path

from wiki_translate.glossary import (
    GlossaryEntry,
    load_glossary,
    render_for_prompt,
    select_relevant,
)


def test_load_glossary_basic(tmp_path: Path):
    p = tmp_path / "g.csv"
    p.write_text(
        "en,zh,note\n"
        "# 这是注释行\n"
        "Seek,Seek,实体名\n"
        "Crucifix,十字架,\n"
        ",,\n"  # 空 en 应被忽略
        "  Door  ,  Door  ,\n",
        encoding="utf-8",
    )
    entries = load_glossary(p)
    ens = [e.en for e in entries]
    assert "Seek" in ens
    assert "Crucifix" in ens
    assert "Door" in ens
    assert all(e.en for e in entries)


def test_load_glossary_missing_file():
    assert load_glossary("/path/that/should/not/exist.csv") == []


def test_select_relevant_word_boundary():
    entries = [
        GlossaryEntry(en="Seek", zh="Seek"),
        GlossaryEntry(en="Door", zh="Door"),
        GlossaryEntry(en="Crucifix", zh="十字架"),
    ]
    text = "The player must use a [[Crucifix]] when Seek appears."
    selected = select_relevant(text, entries)
    ens = [e.en for e in selected]
    assert "Seek" in ens
    assert "Crucifix" in ens
    assert "Door" not in ens


def test_select_relevant_case_insensitive():
    entries = [GlossaryEntry(en="Rush", zh="Rush")]
    assert select_relevant("rush is coming", entries)
    assert select_relevant("RUSH is coming", entries)


def test_select_relevant_avoids_substring_match():
    entries = [GlossaryEntry(en="Door", zh="Door")]
    # Doorway 不应触发 Door
    assert select_relevant("Doorway is here", entries) == []


def test_render_for_prompt_empty_zh_marks_keep():
    entries = [
        GlossaryEntry(en="Seek", zh="", note=""),
        GlossaryEntry(en="Crucifix", zh="十字架", note="道具"),
    ]
    out = render_for_prompt(entries)
    assert "保持原样" in out
    assert "十字架" in out
    assert "Seek" in out
    assert "Crucifix" in out


def test_render_for_prompt_empty_returns_blank():
    assert render_for_prompt([]) == ""
