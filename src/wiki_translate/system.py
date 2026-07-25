"""MediaWiki 系统消息同步（ns=8）。

MediaWiki 系统消息是面向界面的硬编码字符串，每条对应一个 page name（如
`MediaWiki:Sidebar`）。源 wiki 上若编辑过任何系统消息，复制到目标 wiki 才能让
界面/侧边栏/版权声明等保持一致。

设计要点：
- 通过 action=query&list=allmessages&amcustomised=modified 仅取"已被自定义过"
  的消息；默认消息没必要同步。
- CSS/JS（Common.css/MediaWiki.css/Common.js 等）原样复制，不调 LLM 翻译。
- 其它纯文本消息 → 走和 page 翻译相同的 Translator 路径。
- 输出到 output/<lang>/MediaWiki/<MessageName>.wiki，方便 commit 与人工审阅。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import AppConfig
from .fandom import FandomClient
from .utils import safe_filename

log = logging.getLogger(__name__)


_CODE_SUFFIXES = (".css", ".js", ".json")


def _is_code_message(name: str) -> bool:
    return any(name.lower().endswith(suf) for suf in _CODE_SUFFIXES)


def _list_modified_messages(client: FandomClient) -> list[dict[str, Any]]:
    """列出所有 customised 状态的系统消息，包含名称与当前内容。"""
    out: list[dict[str, Any]] = []
    amfrom: str | None = None
    while True:
        params: dict[str, Any] = {
            "action": "query",
            "meta": "allmessages",
            "amcustomised": "modified",
            "amenableparser": 0,
            "amincludelocal": 1,
            "amlimit": "max",
        }
        if amfrom:
            params["amfrom"] = amfrom
        data = client._get(params)  # noqa: SLF001
        msgs = data.get("query", {}).get("allmessages", []) or []
        for m in msgs:
            if m.get("missing"):
                continue
            out.append(
                {
                    "name": m.get("name", ""),
                    "normalizedname": m.get("normalizedname", ""),
                    "content": m.get("content", "") or m.get("*", "") or "",
                }
            )
        # allmessages 不分页（返回 max 已经全部）；保险起见仍检测 continue
        cont = data.get("continue", {}).get("amfrom")
        if not cont or cont == amfrom:
            break
        amfrom = cont
    return out


def _output_path(cfg: AppConfig, msg_name: str) -> Path:
    out_dir = Path(cfg.output.dir) / cfg.wiki.target_lang / "MediaWiki"
    return out_dir / (safe_filename(msg_name) + ".wiki")
