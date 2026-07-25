"""段落切分与 diff 级翻译规划测试。"""
from __future__ import annotations

from wiki_translate.segmenter import (
    plan_translation,
    split_segments,
    stitch,
)


def test_split_segments_roundtrip():
    text = "para 1\n\npara 2\nline2\n\n\npara 3"
    segs = split_segments(text)
    assert "".join(segs) == text
    # 段数应为 5：3 段内容 + 2 个分隔符
    assert len(segs) == 5


def test_split_segments_empty():
    assert split_segments("") == []


def test_plan_no_change_all_keep():
    src_old = "p1\n\np2\n\np3"
    src_new = "p1\n\np2\n\np3"
    trans_old = "译1\n\n译2\n\n译3"
    plan = plan_translation(
        old_source=src_old, new_source=src_new, old_translation=trans_old
    )
    assert plan.needs_full_retranslate is False
    assert plan.translate_count == 0
    assert plan.keep_count == len(plan.plans)
    out = stitch(plan, [])
    assert out == trans_old


def test_plan_one_paragraph_changed():
    src_old = "p1\n\np2\n\np3"
    src_new = "p1\n\nP2 changed\n\np3"
    trans_old = "译1\n\n译2\n\n译3"
    plan = plan_translation(
        old_source=src_old, new_source=src_new, old_translation=trans_old
    )
    assert plan.needs_full_retranslate is False
    assert plan.translate_count == 1
    # stitch：把"译2"段替换为新译
    out = stitch(plan, ["译2-新"])
    assert out == "译1\n\n译2-新\n\n译3"


def test_plan_paragraph_inserted():
    src_old = "p1\n\np2"
    src_new = "p1\n\nNEW\n\np2"
    trans_old = "译1\n\n译2"
    plan = plan_translation(
        old_source=src_old, new_source=src_new, old_translation=trans_old
    )
    assert plan.needs_full_retranslate is False
    assert plan.translate_count == 1
    out = stitch(plan, ["新译"])
    assert "新译" in out
    assert out.startswith("译1")
    assert out.endswith("译2")


def test_plan_segment_count_mismatch_falls_back():
    # 旧源 3 段，但旧译只有 2 段 → 触发回退
    src_old = "p1\n\np2\n\np3"
    src_new = "p1\n\np2\n\np3"
    trans_old = "译1\n\n译2"
    plan = plan_translation(
        old_source=src_old, new_source=src_new, old_translation=trans_old
    )
    assert plan.needs_full_retranslate is True
    assert plan.reason


def test_stitch_preserves_separators():
    src_old = "a\n\nb"
    src_new = "a\n\n\nb"  # 分隔符变长
    trans_old = "中a\n\n中b"
    plan = plan_translation(
        old_source=src_old, new_source=src_new, old_translation=trans_old
    )
    # 段数仍是 3（a, sep, b），不会触发回退
    assert plan.needs_full_retranslate is False
    out = stitch(plan, [])
    # 分隔符应该用新源里的（更长的换行）
    assert "\n\n\n" in out
