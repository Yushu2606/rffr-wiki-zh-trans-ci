"""utils 工具函数测试。"""
from __future__ import annotations

from wiki_translate.utils import safe_filename, split_chunks, strip_code_fence


def test_split_chunks_short():
    assert split_chunks("hello", 100) == ["hello"]


def test_split_chunks_by_lines():
    text = "line1\nline2\nline3\nline4\n"
    chunks = split_chunks(text, max_chars=12)
    assert "".join(chunks) == text
    assert all(len(c) <= 12 or c.count("\n") <= 1 for c in chunks)
    assert len(chunks) >= 2


def test_split_chunks_oversize_single_line():
    # 单行超过阈值时仍作为单块返回，不会丢内容
    big = "x" * 50 + "\n"
    chunks = split_chunks(big, max_chars=10)
    assert "".join(chunks) == big


def test_strip_code_fence_with_lang():
    raw = "```wiki\n[[Foo]]\n```"
    assert strip_code_fence(raw) == "[[Foo]]"


def test_strip_code_fence_plain():
    raw = "```\nhello\n```"
    assert strip_code_fence(raw) == "hello"


def test_strip_code_fence_no_fence():
    assert strip_code_fence("hello") == "hello"


def test_safe_filename():
    assert safe_filename("A-258 (IR)") == "A-258 (IR)"
    assert safe_filename("Some/Path") == "Some_Path"
    assert safe_filename('bad<name>?').startswith("bad")
    assert "<" not in safe_filename('bad<name>?')
    assert ">" not in safe_filename('bad<name>?')
