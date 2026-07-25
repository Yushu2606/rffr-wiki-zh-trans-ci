"""段落级 diff 与局部翻译规划。

切段规则：
- 用一个或多个连续空行作为段落分隔符；
- 同时把"段落分隔符本身"也作为 token 保留，便于无损 stitch 回去。

对齐：
- 用 difflib.SequenceMatcher 在 (old_source_segments, new_source_segments) 上做 opcodes 对齐；
- 若 old_source_segments 与 old_translation_segments 段数 **完全一致**，则可一一对应；
  否则回退（plan_translation 返回 needs_full_retranslate=True）。

输出 plan：每个新源段落 → 三种状态
- "keep"     : 拷贝旧译文段（未变）
- "translate": 调 LLM 翻译此段（新增/修改）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# 一个或多个连续空白行（含可能的回车）
_PARA_SPLIT = re.compile(r"(\n[ \t]*\n+)")


def split_segments(text: str) -> list[str]:
    """把 text 切成段落 + 段落间空白序列交替的列表。

    例如 "a\n\nb\nc\n\n\nd" -> ["a", "\n\n", "b\nc", "\n\n\n", "d"]
    join("".join) 还原原文。
    """
    if not text:
        return []
    parts = _PARA_SPLIT.split(text)
    return [p for p in parts if p != ""]


def _is_separator(seg: str) -> bool:
    """是否是空白分隔段（split 出来的偶数索引）。"""
    return seg.strip() == "" and "\n" in seg


@dataclass(slots=True)
class SegmentPlan:
    kind: str  # "keep" | "translate"
    new_text: str
    old_translation: str | None = None  # kind == keep 时复用


@dataclass(slots=True)
class TranslationPlan:
    plans: list[SegmentPlan]
    needs_full_retranslate: bool = False
    reason: str = ""

    @property
    def translate_count(self) -> int:
        return sum(1 for p in self.plans if p.kind == "translate")

    @property
    def keep_count(self) -> int:
        return sum(1 for p in self.plans if p.kind == "keep")


def plan_translation(
    *,
    old_source: str,
    new_source: str,
    old_translation: str,
) -> TranslationPlan:
    """生成 diff 级翻译计划。

    若旧源/旧译段数不匹配，置 needs_full_retranslate=True；调用方据此回退到整页重译。
    """
    old_src_segs = split_segments(old_source)
    new_src_segs = split_segments(new_source)
    old_trans_segs = split_segments(old_translation)

    # 段落分隔符在前述切分中以 "\n\n..." 形式留在偶数索引位置；
    # 但实际段数对齐只看真正的段落（非分隔符）
    if len(old_src_segs) != len(old_trans_segs):
        return TranslationPlan(
            plans=[],
            needs_full_retranslate=True,
            reason=(
                f"old_source 段数 {len(old_src_segs)} != old_translation 段数 {len(old_trans_segs)}"
            ),
        )

    # 在新旧源段序列上做 opcode 对齐，索引对应到 split_segments 的下标
    matcher = SequenceMatcher(a=old_src_segs, b=new_src_segs, autojunk=False)
    plans: list[SegmentPlan] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                old_idx = i1 + k
                new_idx = j1 + k
                seg_new = new_src_segs[new_idx]
                seg_old_trans = old_trans_segs[old_idx]
                if _is_separator(seg_new):
                    plans.append(
                        SegmentPlan(
                            kind="keep",
                            new_text=seg_new,
                            old_translation=seg_new,
                        )
                    )
                else:
                    plans.append(
                        SegmentPlan(
                            kind="keep",
                            new_text=seg_new,
                            old_translation=seg_old_trans,
                        )
                    )
        else:
            # replace / insert / delete: 取新序列段直接翻译
            for new_idx in range(j1, j2):
                seg_new = new_src_segs[new_idx]
                if _is_separator(seg_new):
                    plans.append(
                        SegmentPlan(
                            kind="keep",
                            new_text=seg_new,
                            old_translation=seg_new,
                        )
                    )
                else:
                    plans.append(SegmentPlan(kind="translate", new_text=seg_new))

    return TranslationPlan(plans=plans)


def stitch(plan: TranslationPlan, translated_segments: list[str]) -> str:
    """把翻译完的段落拼回完整 wikitext。

    translated_segments 长度需等于 plan 中 kind=='translate' 的数量，按出现顺序对应。
    """
    out: list[str] = []
    it = iter(translated_segments)
    for p in plan.plans:
        if p.kind == "keep":
            out.append(p.old_translation if p.old_translation is not None else p.new_text)
        else:
            out.append(next(it))
    return "".join(out)
