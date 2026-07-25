"""术语表加载与匹配。

格式：CSV (utf-8)，header 行必须为 `en,zh,note`：
    en,zh,note
    Seek,Seek,实体名保持英文
    Crucifix,十字架,
    # 以 # 开头的行是注释

匹配规则：
- 使用大小写不敏感的整词匹配（必要时单边模糊匹配中文）
- 仅返回在文本中真实出现的条目，避免向 prompt 塞过多无关术语
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class GlossaryEntry:
    en: str
    zh: str
    note: str = ""


def load_glossary(path: str | Path) -> list[GlossaryEntry]:
    p = Path(path)
    if not p.exists():
        return []
    entries: list[GlossaryEntry] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(
            line for line in f if line.strip() and not line.lstrip().startswith("#")
        )
        for row in reader:
            en = (row.get("en") or "").strip()
            if not en:
                continue
            entries.append(
                GlossaryEntry(
                    en=en,
                    zh=(row.get("zh") or "").strip(),
                    note=(row.get("note") or "").strip(),
                )
            )
    return entries


def select_relevant(text: str, entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    """只保留 text 中真实出现的条目（按 en 字段大小写不敏感整词匹配）。"""
    if not entries or not text:
        return []
    selected: list[GlossaryEntry] = []
    lowered = text.lower()
    for e in entries:
        needle = e.en.lower()
        # 整词匹配；MediaWiki 标记里的 [[X]]、{{X}} 也能命中
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])"
        if re.search(pattern, lowered):
            selected.append(e)
    return selected


def render_for_prompt(entries: list[GlossaryEntry]) -> str:
    """渲染成 Markdown 表格供 prompt 使用，空译文显式标注「保持原样」。"""
    if not entries:
        return ""
    lines = ["| en | zh | 备注 |", "|---|---|---|"]
    for e in entries:
        zh = e.zh if e.zh else "保持原样（英文）"
        note = e.note or ""
        lines.append(f"| {e.en} | {zh} | {note} |")
    return "\n".join(lines)
