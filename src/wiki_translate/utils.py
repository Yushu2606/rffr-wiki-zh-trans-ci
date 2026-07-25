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


# MediaWiki 标准命名空间前缀（英文规范名与常见别名，小写比较）。源 wiki 是英文站，
# 标题里的命名空间就是这些名字。必须用白名单：光看"冒号前有没有内容"无法区分
# "Template:Foo"（真命名空间）和 "Rooms: Found Footage"（主条目标题里恰好带冒号），
# 后者会被拆成不存在的 ns 目录，导致该页读写路径对不上、被当成缺失文件跳过。
_NAMESPACE_PREFIXES: frozenset[str] = frozenset(
    {
        "media",
        "special",
        "talk",
        "user",
        "user talk",
        "project",
        "project talk",
        "file",
        "file talk",
        "image",
        "image talk",
        "mediawiki",
        "mediawiki talk",
        "template",
        "template talk",
        "help",
        "help talk",
        "category",
        "category talk",
        "module",
        "module talk",
    }
)


def title_to_path(title: str) -> tuple[str, str]:
    """把含命名空间前缀的标题拆成 (ns_dir, basename)。

    "Template:Infobox"        -> ("Template", "Infobox")
    "Category:实体"            -> ("Category", "实体")
    "A-120"                   -> ("", "A-120")           # 主条目无前缀
    "Rooms: Found Footage..." -> ("", "Rooms_ Found...")  # 冒号只是标题的一部分
    """
    if ":" in title:
        ns, _, base = title.partition(":")
        # MediaWiki 里命名空间前缀的下划线与空格等价，且大小写不敏感
        if ns.strip().replace("_", " ").lower() in _NAMESPACE_PREFIXES and base.strip():
            return safe_filename(ns.strip()), safe_filename(base)
    return "", safe_filename(title)
