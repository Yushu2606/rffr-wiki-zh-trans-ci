"""清单（manifest）：fetch 阶段产出，描述本次需要处理/上传的所有条目。

三阶段流水线（fetch → process → upload）通过 manifest.json 解耦：
- fetch：拉取源 wiki 全部页面/文件/系统消息，写入 cache/，并把"需要处理的条目"
  （源已变化或强制）登记进 manifest。
- process：并行（按分片）读 manifest，只做非 wiki-API 工作（LLM 翻译 / 代码原样复制），
  产出写入 output/，不触达任何 wiki API。
- upload：单 job 顺序读 manifest，把 output/ 与 cache/files 统一推送到目标 wiki，
  集中控制频率，规避限流。

manifest item 形态（按 kind 区分）：
  page:   {kind, title, ns, action, revid, source_hash, incoming_cache}
  system: {kind, name, action, source_hash, incoming_cache}
  file:   {kind, title, name, url, sha1, size, mime, cache_path}

action 取值：
  translate —— 走 LLM 翻译
  copy      —— 原样复制（Lua 模块 / CSS / JS / JSON 等代码）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = 1


def empty_manifest() -> dict[str, Any]:
    return {"schema": SCHEMA, "generated_at": 0, "items": []}


def load_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return empty_manifest()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty_manifest()
    if not isinstance(data, dict) or "items" not in data:
        return empty_manifest()
    return data


def save_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def items_of(manifest: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    items = manifest.get("items", []) or []
    if kind is None:
        return list(items)
    return [it for it in items if it.get("kind") == kind]


def shard_items(items: list[dict[str, Any]], index: int, total: int) -> list[dict[str, Any]]:
    """round-robin 把条目切成 total 份取第 index 份（0-based）。

    与页面分片同理：轮转切分让每个分片长短条目交错，避免单分片过载。
    """
    if total <= 1:
        return items
    if not (0 <= index < total):
        raise ValueError(f"shard index={index} 超出范围 [0,{total})")
    return [it for i, it in enumerate(items) if i % total == index]


def build_rename_map(manifest: dict[str, Any]) -> dict[str, str]:
    """从 kind=file 条目里提取因 MIME 校正而被改名的文件。

    fetch 阶段发现文件真实 MIME 与后缀不符时会记录 upload_name（见 files.py
    _normalize_filename），但页面正文里的 [[File:...]] 引用还是旧后缀。返回
    {旧文件名: 新文件名}，供 process 阶段回填页面正文，避免上传后链接失效。
    """
    rename_map: dict[str, str] = {}
    for item in items_of(manifest, "file"):
        if not item.get("renamed"):
            continue
        old_name = item.get("name") or ""
        new_name = item.get("upload_name") or ""
        if not (old_name and new_name):
            continue
        # 空格与下划线在 MediaWiki 文件名里等价，只差分隔符不算改名——否则会把正文
        # 改成语义完全相同的另一种写法，凭空产生 diff 并触发无意义的重新推送。
        if old_name.replace("_", " ") == new_name.replace("_", " "):
            continue
        rename_map[old_name] = new_name
    return rename_map


def classify_title(title: str) -> str:
    """根据标题判断该页面应翻译还是原样复制。

    - Module:（Lua 模块，ns828）→ copy
    - 以 .css / .js / .json 结尾（站点脚本/样式）→ copy
    - 其余（含 Template: 模板、Category: 分类、主条目）→ translate
    """
    prefix = title.split(":", 1)[0].lower() if ":" in title else ""
    if prefix == "module":
        return "copy"
    if title.lower().endswith((".css", ".js", ".json")):
        return "copy"
    return "translate"
