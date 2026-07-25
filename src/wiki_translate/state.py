"""state.json v2 持久化与 schema 迁移。

schema v2:
{
  "schema": 2,
  "pages": {
    "<source_title>": {
      "source":      {"revid": int, "hash": str, "fetched_at": int},
      "target":      {"title": str, "revid": int, "hash": str, "fetched_at": int},
      "translation": {"hash": str, "file": str, "translated_at": int},
      "last_published": {
          "target_revid": int,
          "target_hash": str,
          "translation_hash": str,
          "published_at": int
      }
    }
  }
}
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _empty() -> dict[str, Any]:
    return {"schema": 2, "pages": {}}


def _migrate_v1_to_v2(legacy: dict[str, Any]) -> dict[str, Any]:
    """旧 schema：每个 title 直接挂 revid/translated/file/published_revid。"""
    pages: dict[str, Any] = {}
    for title, info in legacy.items():
        if not isinstance(info, dict):
            continue
        revid = info.get("revid", 0)
        page: dict[str, Any] = {
            "source": {"revid": revid, "hash": "", "fetched_at": 0},
        }
        if info.get("translated"):
            page["translation"] = {
                "hash": "",
                "file": info.get("file", ""),
                "translated_at": info.get("updated_at", 0),
            }
        if info.get("published_revid"):
            page["last_published"] = {
                "target_revid": info.get("published_newrevid", 0),
                "target_hash": "",
                "translation_hash": "",
                "published_at": info.get("published_at", 0),
            }
            page["target"] = {
                "title": info.get("published_target", title),
                "revid": info.get("published_newrevid", 0),
                "hash": "",
                "fetched_at": 0,
            }
        pages[title] = page
    return {"schema": 2, "pages": pages}


def load_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    if data.get("schema") == 2 and "pages" in data:
        return data
    return _migrate_v1_to_v2(data)


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_page(state: dict[str, Any], title: str) -> dict[str, Any]:
    return state.setdefault("pages", {}).setdefault(title, {})


def all_titles(state: dict[str, Any]) -> list[str]:
    return list(state.get("pages", {}).keys())


_SECTION_KEYS: tuple[str, ...] = ("pages", "system_messages", "files")


def merge_states(base: dict[str, Any], *deltas: dict[str, Any]) -> dict[str, Any]:
    """把多个分片产出的 state 合并到 base 上（后者覆盖前者）。

    每个 section（pages / system_messages / files）按其条目 key 浅合并：
    同一 title 若在多个 delta 中出现，靠后的 delta 整体覆盖该条目。
    分片之间各自负责不相交的 title，因此覆盖只会发生在 base 与 delta 之间。
    """
    merged: dict[str, Any] = {"schema": 2}
    for section in _SECTION_KEYS:
        section_acc: dict[str, Any] = dict(base.get(section, {}) or {})
        for delta in deltas:
            for key, val in (delta.get(section, {}) or {}).items():
                section_acc[key] = val
        if section_acc or section == "pages":
            merged[section] = section_acc
    return merged


def extract_delta(
    state: dict[str, Any],
    *,
    pages: set[str] | None = None,
    system_messages: set[str] | None = None,
    files: set[str] | None = None,
) -> dict[str, Any]:
    """从完整 state 中抽取指定 key 子集，生成可独立落盘的 delta state。

    None 表示该 section 不导出；空集合表示导出 0 条但保留该 section。
    """
    delta: dict[str, Any] = {"schema": 2}
    selectors: dict[str, set[str] | None] = {
        "pages": pages,
        "system_messages": system_messages,
        "files": files,
    }
    for section, keys in selectors.items():
        if keys is None:
            continue
        src = state.get(section, {}) or {}
        delta[section] = {k: src[k] for k in keys if k in src}
    delta.setdefault("pages", {})
    return delta


def load_states(paths: list[str | Path]) -> list[dict[str, Any]]:
    """批量加载多个 state 文件，忽略不存在的路径。"""
    out: list[dict[str, Any]] = []
    for p in paths:
        if Path(p).exists():
            out.append(load_state(p))
    return out


def save_progress(
    state: dict[str, Any],
    full_path: str | Path,
    *,
    delta_path: str | Path | None = None,
    pages: set[str] | None = None,
    system_messages: set[str] | None = None,
    files: set[str] | None = None,
) -> None:
    """统一的进度落盘：

    - 普通模式（delta_path 为空）：把完整 state 写到 full_path。
    - 分片模式（delta_path 非空）：只把本分片改动过的条目抽成 delta 写到 delta_path，
      避免多个分片并发覆盖同一个 state.json。
    """
    if delta_path:
        save_state(
            delta_path,
            extract_delta(
                state,
                pages=pages,
                system_messages=system_messages,
                files=files,
            ),
        )
    else:
        save_state(full_path, state)
