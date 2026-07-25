"""通用工具函数。"""

from __future__ import annotations

import re


def split_chunks(text: str, max_chars: int) -> list[str]:
    """按行切分，使每块不超过 max_chars 字符。"""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > max_chars and buf:
            chunks.append("".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n")


def strip_code_fence(text: str) -> str:
    """去掉模型偶发输出的 ``` 代码块包裹。"""
    t = text.strip()
    if t.startswith("```"):
        t = _CODE_FENCE_RE.sub("", t, count=1)
        if t.endswith("```"):
            t = t[:-3]
    return t.strip("\n")


_INVALID_FS = re.compile(r'[<>:"|?*]')


def safe_filename(title: str) -> str:
    name = title.replace("/", "_").replace("\\", "_")
    return _INVALID_FS.sub("_", name)


def title_to_path(title: str) -> tuple[str, str]:
    """把含命名空间前缀的标题拆成 (ns_dir, basename)。

    "Template:Infobox" -> ("Template", "Infobox")
    "Category:实体"     -> ("Category", "实体")
    "A-120"            -> ("", "A-120")  # 主条目无前缀
    """
    if ":" in title:
        ns, _, base = title.partition(":")
        # MediaWiki 的合法 ns 名不会含有空格、问号等
        if ns and ns.strip() and "/" not in ns:
            return safe_filename(ns), safe_filename(base)
    return "", safe_filename(title)
