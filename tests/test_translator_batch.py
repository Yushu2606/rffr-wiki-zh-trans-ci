"""批量分段翻译：分批策略、响应解析、跑偏回退。"""
from __future__ import annotations

import pytest

from wiki_translate.config import TranslatorConfig
from wiki_translate.translator import (
    TranslationTruncated,
    Translator,
    _parse_segment_batch,
)


def _make(chunk_chars: int = 100) -> Translator:
    cfg = TranslatorConfig(api_key="unused", chunk_chars=chunk_chars, glossary_file="")
    return Translator(cfg)


def test_parse_batch_happy_path():
    raw = "<<<SEG:1>>>\n第一段\n<<<SEG:2>>>\n第二段"
    assert _parse_segment_batch(raw, 2) == ["第一段", "第二段"]


def test_parse_batch_preserves_internal_blank_lines():
    raw = "<<<SEG:1>>>\n行一\n\n行二\n<<<SEG:2>>>\n第二段"
    out = _parse_segment_batch(raw, 2)
    assert out is not None
    assert out[0] == "行一\n\n行二"


def test_parse_batch_wrong_count_returns_none():
    """段数不符必须判失败——错位拼进页面的损坏极难发现。"""
    raw = "<<<SEG:1>>>\n只有一段"
    assert _parse_segment_batch(raw, 2) is None


def test_parse_batch_out_of_order_returns_none():
    raw = "<<<SEG:2>>>\nB\n<<<SEG:1>>>\nA"
    assert _parse_segment_batch(raw, 2) is None


def test_parse_batch_duplicate_index_returns_none():
    raw = "<<<SEG:1>>>\nA\n<<<SEG:1>>>\nB"
    assert _parse_segment_batch(raw, 2) is None


def test_parse_batch_no_markers_returns_none():
    assert _parse_segment_batch("模型忘了标记直接输出了译文", 2) is None


def test_parse_batch_ignores_inline_marker_lookalike():
    """只认独占一行的标记，正文里出现的相似串不该被当作分隔符。"""
    raw = "<<<SEG:1>>>\n这段提到 <<<SEG:9>>> 但不在行首独占\n<<<SEG:2>>>\nB"
    out = _parse_segment_batch(raw, 2)
    assert out is not None and len(out) == 2


def test_batch_segments_groups_within_budget():
    t = _make(chunk_chars=100)
    segs = ["a" * 40, "b" * 40, "c" * 40]
    assert [len(b) for b in t._batch_segments(segs)] == [2, 1]


def test_batch_segments_oversized_segment_gets_own_batch():
    t = _make(chunk_chars=50)
    segs = ["a" * 200, "b" * 10]
    batches = t._batch_segments(segs)
    assert batches[0] == ["a" * 200]
    assert batches[1] == ["b" * 10]


def test_batch_segments_single_batch_when_all_fit():
    t = _make(chunk_chars=1000)
    segs = ["a" * 10, "b" * 10, "c" * 10]
    assert t._batch_segments(segs) == [segs]


def test_translate_segments_empty_returns_empty():
    assert _make().translate_segments(
        source_lang="en", target_lang="zh", segments=[], full_new_source="x"
    ) == []


def test_translate_segments_batches_one_call_per_batch(monkeypatch):
    """3 段放得下一批 -> 只调 1 次 LLM（重构前是 3 次，每次重发整页上下文）。"""
    t = _make(chunk_chars=1000)
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        return "<<<SEG:1>>>\n一\n<<<SEG:2>>>\n二\n<<<SEG:3>>>\n三"

    monkeypatch.setattr(t, "_call_with_retry", fake)
    out = t.translate_segments(
        source_lang="en",
        target_lang="zh",
        segments=["A", "B", "C"],
        full_new_source="整页" * 100,
    )
    assert out == ["一", "二", "三"]
    assert len(calls) == 1
    assert calls[0].count("整页" * 100) == 1, "整页上下文每批只发一次"


def test_translate_segments_falls_back_when_model_misbehaves(monkeypatch):
    t = _make(chunk_chars=1000)
    calls: list[str] = []

    def fake(prompt: str) -> str:
        calls.append(prompt)
        # 首次批量调用返回格式错误，随后的逐段回退各自返回译文
        return "模型忘了标记" if len(calls) == 1 else f"译文{len(calls) - 1}"

    monkeypatch.setattr(t, "_call_with_retry", fake)
    out = t.translate_segments(
        source_lang="en", target_lang="zh", segments=["A", "B"], full_new_source="src"
    )
    assert out == ["译文1", "译文2"]
    assert len(calls) == 3, "1 次批量 + 2 次逐段回退"


def test_translate_segments_single_segment_skips_batch_format(monkeypatch):
    """只有一段时没必要用批量格式，直接走单段路径。"""
    t = _make()
    monkeypatch.setattr(t, "_call_with_retry", lambda p: "译文")
    out = t.translate_segments(
        source_lang="en", target_lang="zh", segments=["A"], full_new_source="src"
    )
    assert out == ["译文"]


def _fake_completion(content: str, finish_reason: str):
    class Msg:
        def __init__(self, c):
            self.content = c

    class Choice:
        def __init__(self, c, fr):
            self.message = Msg(c)
            self.finish_reason = fr

    class Completion:
        def __init__(self, c, fr):
            self.choices = [Choice(c, fr)]

    return Completion(content, finish_reason)


def test_call_raises_on_max_tokens_truncation(monkeypatch):
    """finish_reason=length 必须当失败，不能把半截译文写进 output。"""
    t = _make()
    monkeypatch.setattr(
        t.client.chat.completions,
        "create",
        lambda **kw: _fake_completion("翻到一半就断了", "length"),
    )
    with pytest.raises(TranslationTruncated) as ei:
        t._call("prompt")
    assert "max_tokens" in str(ei.value)


def test_call_accepts_normal_stop(monkeypatch):
    t = _make()
    monkeypatch.setattr(
        t.client.chat.completions,
        "create",
        lambda **kw: _fake_completion("完整译文", "stop"),
    )
    assert t._call("prompt") == "完整译文"


def test_batch_truncation_falls_back_to_per_segment(monkeypatch):
    """批量输出撞上 max_tokens 时拆成逐段重试，单次输出更小往往就能过。"""
    t = _make(chunk_chars=1000)
    calls = {"n": 0}

    def fake(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TranslationTruncated("批量输出被截断")
        return f"译文{calls['n'] - 1}"

    monkeypatch.setattr(t, "_call_with_retry", fake)
    out = t.translate_segments(
        source_lang="en", target_lang="zh", segments=["A", "B"], full_new_source="src"
    )
    assert out == ["译文1", "译文2"]
    assert calls["n"] == 3
