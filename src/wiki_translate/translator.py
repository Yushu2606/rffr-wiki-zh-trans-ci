"""LLM 翻译：使用官方 openai SDK，兼容所有 OpenAI 协议端点。"""

from __future__ import annotations

import logging
import re

from openai import APIError, APIStatusError, OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import TranslatorConfig
from .glossary import (
    GlossaryEntry,
    load_glossary,
    render_for_prompt,
    select_relevant,
)
from .utils import strip_code_fence

log = logging.getLogger(__name__)


class TranslationTruncated(RuntimeError):
    """译文因 max_tokens 被截断。当作失败处理，避免半截译文覆盖已有译文。"""


# 批量分段翻译的段落分隔标记。"<<<" 不是合法 wikitext，正文里几乎不可能出现；
# 纯 ASCII，模型复现稳定。解析时只认独占一行的完整标记。
_SEG_MARKER_FMT = "<<<SEG:{n}>>>"
_SEG_MARKER_RE = re.compile(r"^[ \t]*<<<SEG:(\d+)>>>[ \t]*$", re.MULTILINE)


def _parse_segment_batch(raw: str, expected: int) -> list[str] | None:
    """按标记拆出批量译文；模型跑偏时返回 None 交由调用方回退。

    只接受"恰好 1..expected 全部出现且不重复"的结果——宁可退回逐段翻译多花点钱，
    也不能把错位或缺失的译文拼进页面，那种损坏很难被发现。
    """
    matches = list(_SEG_MARKER_RE.finditer(raw))
    if len(matches) != expected:
        return None
    if [int(m.group(1)) for m in matches] != list(range(1, expected + 1)):
        return None
    out: list[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out.append(strip_code_fence(raw[m.end() : end]))
    return out


def _namespace_hint(title: str) -> str:
    """根据标题前缀返回针对模板/分类/文件的额外指导。"""
    low = title.split(":", 1)[0].lower() if ":" in title else ""
    if low == "template":
        return (
            "## 命名空间提示：当前页面是 Template:\n"
            "- 此为 MediaWiki 模板源码。**不要翻译** {{{...}}} 形式的参数占位符、"
            "<noinclude>/<includeonly>/<onlyinclude> 标签内的开关变量、"
            "{{#switch:}} 等解析器函数的关键字。\n"
            "- 模板默认参数的展示文本（如 default = ...）可以翻译。\n"
            "- 模板内的硬编码英文文案（信息框标签、按钮文字）应翻译。\n"
            "- 类别声明 [[Category:Foo]] 的 Foo 保持原样。"
        )
    if low == "category":
        return (
            "## 命名空间提示：当前页面是 Category:\n"
            "- 分类页通常很短。直接翻译描述文本，**保留** [[Category:Parent]] / "
            "{{Category|...}} 等结构原样。"
        )
    if low == "file":
        return (
            "## 命名空间提示：当前页面是 File: 页面描述\n"
            "- 这是文件元数据页（描述、版权、来源），不影响文件本身二进制。\n"
            "- 直接翻译描述文字，{{Information|description=...}} 等模板的参数值可翻译，"
            "参数名保留原样。"
        )
    return ""


def _build_user_prompt(
    *,
    source_lang: str,
    target_lang: str,
    new_source: str,
    source_diff: str | None = None,
    reference_translation: str | None = None,
    target_local: str | None = None,
    target_diff: str | None = None,
    glossary_text: str | None = None,
    namespace_hint: str | None = None,
) -> str:
    parts: list[str] = [
        f"源语言: {source_lang}",
        f"目标语言: {target_lang}",
    ]
    if namespace_hint:
        parts.append(namespace_hint)
    if glossary_text:
        parts.append("## 术语表（必须严格遵守；en 列在原文中出现时译为 zh 列）\n" + glossary_text)
    if reference_translation:
        parts.append(
            "## 仓库现有译文（参考；保留其术语与风格，无需复制结构）\n"
            f"```\n{reference_translation}\n```"
        )
    if source_diff:
        # 只发 diff，不发旧源码全文：diff 自带上下文行、已包含变化处的旧内容，而未变部分
        # 与下面的新源码逐字相同。实测旧源码占 prompt 约三分之一，却不提供任何新信息。
        parts.append("## 源文 diff（unified format）\n" f"```diff\n{source_diff}\n```")
    if target_local and target_diff:
        parts.append(
            "## 目标 wiki 当前内容（注意：包含人工修订，需尽量保留）\n"
            f"```\n{target_local}\n```\n"
            "## 目标 diff（仓库上次推送 → 目标 wiki 当前；正数为人工修订）\n"
            f"```diff\n{target_diff}\n```"
        )
    parts.append(f"## 待翻译的源 wiki 源码（请输出完整译文）\n```\n{new_source}\n```")
    parts.append("仅输出译文 wikitext，不要解释、不要 Markdown 代码块。")
    return "\n\n".join(parts)


class Translator:
    """对 OpenAI ChatCompletion 的薄封装，带重试与代码块剥离。"""

    def __init__(self, cfg: TranslatorConfig) -> None:
        if not cfg.api_key:
            raise RuntimeError("环境变量 OPENAI_API_KEY 未设置")
        self.cfg = cfg
        self.client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
        self.glossary: list[GlossaryEntry] = (
            load_glossary(cfg.glossary_file) if cfg.glossary_file else []
        )
        if self.glossary:
            log.info("术语表加载 %d 条目（%s）", len(self.glossary), cfg.glossary_file)

        self._call_with_retry = retry(
            reraise=True,
            stop=stop_after_attempt(max(1, cfg.retry)),
            wait=wait_exponential(multiplier=cfg.retry_delay or 1, min=cfg.retry_delay, max=60),
            retry=retry_if_exception_type((APIError, APIStatusError, TimeoutError)),
        )(self._call)

    def _call(self, user_prompt: str) -> str:
        completion = self.client.chat.completions.create(
            model=self.cfg.model,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            messages=[
                {"role": "system", "content": self.cfg.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        choice = completion.choices[0]
        # finish_reason=length 表示输出撞上 max_tokens 被截断。译文会从中间断掉，
        # 而且没有任何显式错误——直接写进 output 再推上 wiki 就是残页。必须当成失败，
        # 让调用方记为该页失败并保留旧译文，而不是用半截译文覆盖。
        if getattr(choice, "finish_reason", None) == "length":
            raise TranslationTruncated(
                f"译文被 max_tokens({self.cfg.max_tokens}) 截断；"
                f"请调大 TRANSLATOR_MAX_TOKENS 或调小 TRANSLATOR_CHUNK_CHARS"
                f"（当前 {self.cfg.chunk_chars}）"
            )
        content = choice.message.content or ""
        return strip_code_fence(content)

    def _glossary_for(self, text: str) -> str | None:
        if not self.glossary:
            return None
        relevant = select_relevant(text, self.glossary)
        if not relevant:
            return None
        cap = max(1, self.cfg.glossary_max_entries)
        if len(relevant) > cap:
            relevant = relevant[:cap]
        return render_for_prompt(relevant)

    def translate(
        self,
        new_source: str,
        source_lang: str,
        target_lang: str,
        *,
        source_diff: str | None = None,
        reference_translation: str | None = None,
        target_local: str | None = None,
        target_diff: str | None = None,
        title: str = "",
    ) -> str:
        prompt = _build_user_prompt(
            source_lang=source_lang,
            target_lang=target_lang,
            new_source=new_source,
            source_diff=source_diff,
            reference_translation=reference_translation,
            target_local=target_local,
            target_diff=target_diff,
            glossary_text=self._glossary_for(new_source),
            namespace_hint=_namespace_hint(title) or None,
        )
        return self._call_with_retry(prompt)

    def translate_segments(
        self,
        *,
        source_lang: str,
        target_lang: str,
        segments: list[str],
        full_new_source: str,
        full_reference_translation: str | None = None,
        title: str = "",
    ) -> list[str]:
        """批量翻译同一页里的多个段落，返回与 segments 等长、顺序一致的译文列表。

        整页源码与参考译文是保持术语风格一致所必需的上下文，但它们比待译段落大得多；
        逐段调用会把这份上下文重发 N 遍，段数一多反而比整页重译更贵。这里按字符预算
        分批，每批只发一次上下文，让模型一次返回该批全部段落。

        模型跑偏（返回段数对不上）时自动退回逐段翻译，保证结果正确性不依赖格式遵循。
        """
        if not segments:
            return []
        out: list[str] = []
        for batch in self._batch_segments(segments):
            out.extend(
                self._translate_batch(
                    source_lang=source_lang,
                    target_lang=target_lang,
                    segments=batch,
                    full_new_source=full_new_source,
                    full_reference_translation=full_reference_translation,
                    title=title,
                )
            )
        return out

    def _batch_segments(self, segments: list[str]) -> list[list[str]]:
        """按字符预算分批。单段超预算时独占一批——段落是不可再分的最小翻译单位。"""
        budget = max(1, self.cfg.chunk_chars)
        batches: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for seg in segments:
            if cur and size + len(seg) > budget:
                batches.append(cur)
                cur, size = [], 0
            cur.append(seg)
            size += len(seg)
        if cur:
            batches.append(cur)
        return batches

    def _translate_batch(
        self,
        *,
        source_lang: str,
        target_lang: str,
        segments: list[str],
        full_new_source: str,
        full_reference_translation: str | None,
        title: str,
    ) -> list[str]:
        if len(segments) == 1:
            return [
                self.translate_segment(
                    source_lang=source_lang,
                    target_lang=target_lang,
                    segment_text=segments[0],
                    full_new_source=full_new_source,
                    full_reference_translation=full_reference_translation,
                    title=title,
                )
            ]

        sections: list[str] = [
            f"源语言: {source_lang}",
            f"目标语言: {target_lang}",
            "## 任务\n"
            f"下面是同一个页面里 {len(segments)} 个需要翻译的段落。"
            "整页源码与参考译文仅作上下文，用于保持术语与风格一致。\n\n"
            "**输出格式（必须严格遵守）**：依次输出每个段落，每段前独占一行写 "
            f"`{_SEG_MARKER_FMT.format(n='编号')}`，其后紧跟该段译文 wikitext。"
            "不要解释、不要重复原文、不要输出未列出的段落。",
        ]
        ns_hint = _namespace_hint(title)
        if ns_hint:
            sections.append(ns_hint)
        glossary_text = self._glossary_for("\n".join(segments) + "\n" + full_new_source)
        if glossary_text:
            sections.append("## 术语表（必须严格遵守）\n" + glossary_text)
        if full_reference_translation:
            sections.append(
                "## 整页参考译文（保持术语/风格一致；仅供参考）\n"
                "```\n" + full_reference_translation + "\n```"
            )
        sections.append("## 整页源 wiki 源码（仅供上下文）\n```\n" + full_new_source + "\n```")
        sections.append(
            "## 待翻译的段落\n"
            + "\n".join(
                f"{_SEG_MARKER_FMT.format(n=i)}\n{seg}" for i, seg in enumerate(segments, 1)
            )
        )
        try:
            raw = self._call_with_retry("\n\n".join(sections))
        except TranslationTruncated:
            # 批量输出比单段大得多，更容易撞上 max_tokens。拆开逐段翻译能显著缩小
            # 单次输出，通常就能过；真的连单段都放不下时再由上层记为失败。
            log.warning("  批量输出被 max_tokens 截断，改为逐段翻译")
            parsed = None
        else:
            parsed = _parse_segment_batch(raw, len(segments))
            if parsed is not None:
                return parsed
            log.warning(
                "  批量分段译文解析失败（期望 %d 段），退回逐段翻译",
                len(segments),
            )
        return [
            self.translate_segment(
                source_lang=source_lang,
                target_lang=target_lang,
                segment_text=seg,
                full_new_source=full_new_source,
                full_reference_translation=full_reference_translation,
                title=title,
            )
            for seg in segments
        ]

    def translate_segment(
        self,
        *,
        source_lang: str,
        target_lang: str,
        segment_text: str,
        full_new_source: str,
        full_reference_translation: str | None = None,
        title: str = "",
    ) -> str:
        """翻译单个段落，并把整页源/译文作为只读上下文，让模型保持术语与风格连贯。

        批量路径（translate_segments）解析失败时的回退，也用于单段批次。
        """
        sections: list[str] = [
            f"源语言: {source_lang}",
            f"目标语言: {target_lang}",
            "## 任务\n下面是单个段落的局部翻译任务。"
            "整页源/译文段落仅作为上下文供你保持风格一致，"
            "**只输出该段的译文 wikitext，不要附带其它段落**。",
        ]
        ns_hint = _namespace_hint(title)
        if ns_hint:
            sections.append(ns_hint)
        glossary_text = self._glossary_for(segment_text + "\n" + full_new_source)
        if glossary_text:
            sections.append("## 术语表（必须严格遵守）\n" + glossary_text)
        if full_reference_translation:
            sections.append(
                "## 整页参考译文（保持术语/风格一致；仅供参考）\n"
                "```\n" + full_reference_translation + "\n```"
            )
        sections.append("## 整页源 wiki 源码（仅供上下文）\n```\n" + full_new_source + "\n```")
        sections.append("## 待翻译的段落（**只输出此段译文**）\n```\n" + segment_text + "\n```")
        prompt = "\n\n".join(sections)
        return self._call_with_retry(prompt)

    def merge_translation(
        self,
        *,
        source_lang: str,
        target_lang: str,
        new_source: str,
        repo_translation: str,
        target_current: str,
        target_diff: str,
        source_diff: str | None = None,
    ) -> str:
        """三方合并：把"仓库译文 + 目标 wiki 当前内容（含人工修订）+ 源更新"融合成一份新译文。

        这条路径用于推送阶段检测到目标 wiki 被人手动改过时，避免直接覆盖。

        注意：当前无任何调用方——upload 阶段遇到冲突时按 on_target_conflict 处理，
        merge 会降级为覆盖，不再调 LLM。
        """
        instruction = (
            "## 任务\n"
            "目标 wiki 在我们上次推送之后被人手动修改过。请基于以下三方信息合并出一份新的译文：\n"
            "1. 当前最新源 wiki 源码（必须完整翻译）\n"
            "2. 仓库里上次推送的译文（保留其结构与术语）\n"
            "3. 目标 wiki 当前内容（包含他人的人工修订；这些修订要尽量原样保留进新译文）\n"
            "如三方有冲突：源更新优先于结构、人工修订优先于机翻措辞。\n"
            "只输出最终合并后的 wikitext，不要解释。"
        )
        sections: list[str] = [
            f"源语言: {source_lang}",
            f"目标语言: {target_lang}",
            instruction,
        ]
        glossary_text = self._glossary_for(
            "\n".join([new_source, repo_translation, target_current])
        )
        if glossary_text:
            sections.append("## 术语表（必须严格遵守）\n" + glossary_text)
        sections.extend(
            [
                "## 仓库上次推送的译文（参考）\n```\n" + repo_translation + "\n```",
                "## 目标 wiki 当前内容（含人工修订；尽量保留）\n```\n" + target_current + "\n```",
                "## 目标 diff（仓库上次推送 → 目标当前；体现人工修订）\n"
                "```diff\n" + target_diff + "\n```",
            ]
        )
        if source_diff:
            sections.append("## 源文 diff\n```diff\n" + source_diff + "\n```")
        sections.append(
            "## 待翻译/合并的最新源 wiki 源码（请输出完整合并版译文）\n```\n" + new_source + "\n```"
        )
        prompt = "\n\n".join(sections)
        return self._call_with_retry(prompt)
