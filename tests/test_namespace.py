"""命名空间相关辅助函数测试。"""
from __future__ import annotations

from wiki_translate.translator import _namespace_hint
from wiki_translate.utils import title_to_path


def test_title_to_path_main():
    assert title_to_path("A-120") == ("", "A-120")


def test_title_to_path_template():
    ns, base = title_to_path("Template:Infobox")
    assert ns == "Template"
    assert base == "Infobox"


def test_title_to_path_category():
    ns, base = title_to_path("Category:Entities")
    assert ns == "Category"
    assert base == "Entities"


def test_title_to_path_with_invalid_chars():
    ns, base = title_to_path('Template:Bad<name>?')
    assert ns == "Template"
    assert "<" not in base and ">" not in base and "?" not in base


def test_namespace_hint_template():
    hint = _namespace_hint("Template:Infobox")
    assert "模板" in hint or "Template" in hint
    assert "{{{" in hint  # 提到 {{{...}}} 占位符


def test_namespace_hint_category():
    hint = _namespace_hint("Category:Entities")
    assert "分类" in hint


def test_namespace_hint_file():
    hint = _namespace_hint("File:Foo.png")
    assert "文件" in hint or "File" in hint


def test_namespace_hint_main_empty():
    assert _namespace_hint("A-120") == ""
