"""diff 工具测试。"""
from __future__ import annotations

from wiki_translate.diff import has_changes, unified_diff


def test_unified_diff_no_change():
    assert unified_diff("a\nb", "a\nb") == ""
    assert has_changes("a", "a") is False


def test_unified_diff_changes_present():
    diff = unified_diff("a\nb\nc\n", "a\nB\nc\n")
    assert "-b" in diff
    assert "+B" in diff
    assert has_changes("a", "b") is True


def test_unified_diff_handles_trailing_newline():
    # 末行不带换行也应能正常生成 diff（不报错）
    diff = unified_diff("hello", "hello world")
    assert diff != ""
