"""文本 diff 工具：用 difflib 生成统一格式 diff，供 LLM 参考。"""

from __future__ import annotations

import difflib


def unified_diff(
    old: str,
    new: str,
    *,
    fromfile: str = "old",
    tofile: str = "new",
    n: int = 3,
) -> str:
    """生成 unified diff 文本；空字符串表示无差异。"""
    if old == new:
        return ""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=tofile, n=n)
    return "".join(diff)


def has_changes(old: str, new: str) -> bool:
    return old != new
