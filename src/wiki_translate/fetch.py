"""fetch 阶段：集中完成所有"源 wiki 读"操作。

把页面（主条目/模板/分类/Lua 模块/CSS/JS）、文件（ns6）、系统消息（ns8 customised）
一次性拉取到本地缓存，并和仓库 state.json 比对决定哪些条目需要处理，写入 manifest.json。

本阶段**只读源 wiki**，不调用 LLM、不写目标 wiki。产出：
- cache/incoming/<...>.wiki   —— 新抓到的页面/系统消息源码（process 用作待译输入）
- cache/files/<name>          —— 下载的文件二进制（upload 用）
- manifest.json               —— 待处理/待上传条目清单
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from .config import AppConfig
from .fandom import FandomClient
from .files import (  # noqa: SLF001
    _download,
    _file_cache_path,
    _list_source_files,
    _prepare_upload_file,
    convert_animated_webp_to_gif,
    resolve_upload_collisions,
)
from .manifest import classify_title, save_manifest
from .pipeline import collect_titles
from .state import load_state, sha256, sha256_bytes
from .system import _is_code_message, _list_modified_messages  # noqa: SLF001
from .utils import safe_filename, title_to_path

log = logging.getLogger(__name__)


def _incoming_page_path(cfg: AppConfig, title: str) -> Path:
    base = Path(cfg.strategy.incoming_dir)
    ns_dir, name = title_to_path(title)
    if ns_dir:
        return base / ns_dir / (name + ".wiki")
    return base / (name + ".wiki")


def _incoming_system_path(cfg: AppConfig, name: str) -> Path:
    return Path(cfg.strategy.incoming_dir) / "MediaWiki" / (safe_filename(name) + ".wiki")


# __FETCH_PAGES__
def _fetch_pages(
    cfg: AppConfig,
    client: FandomClient,
    state: dict,
    *,
    force: bool,
    dry_run: bool,
) -> list[dict]:
    """拉取所有页面（含模板/分类/Lua/CSS/JS，由 WIKI_NAMESPACES 决定），返回 page 清单项。"""
    titles = collect_titles(client, cfg.wiki)
    log.info("源 wiki 共枚举 %d 个页面", len(titles))
    pages = state.get("pages", {})
    items: list[dict] = []
    changed = skipped = missing = 0

    for idx, title in enumerate(titles, 1):
        res = client.fetch_page(title)
        if not res:
            missing += 1
            continue
        source, revid = res
        new_hash = sha256(source)
        page = pages.get(title, {})
        src_state = page.get("source", {})
        trans_state = page.get("translation", {})
        unchanged = src_state.get("hash") == new_hash and trans_state.get("hash")
        if not force and unchanged:
            skipped += 1
            continue

        action = classify_title(title)
        incoming = _incoming_page_path(cfg, title)
        if not dry_run:
            incoming.parent.mkdir(parents=True, exist_ok=True)
            incoming.write_text(source, encoding="utf-8")
        items.append(
            {
                "kind": "page",
                "title": title,
                "action": action,
                "revid": revid,
                "source_hash": new_hash,
                "incoming_cache": str(incoming.as_posix()),
            }
        )
        changed += 1
        if idx % 50 == 0:
            log.info("  ...已处理 %d/%d", idx, len(titles))

    log.info(
        "页面：待处理 %d / 跳过 %d / 缺失 %d",
        changed,
        skipped,
        missing,
    )
    return items


def _fetch_files(
    cfg: AppConfig,
    client: FandomClient,
    state: dict,
    *,
    force: bool,
    dry_run: bool,
) -> list[dict]:
    """列出并下载 ns6 文件，返回 file 清单项。"""
    files_state = state.get("files", {})
    max_files = int(cfg.wiki.max_pages or 0)
    src_items = _list_source_files(client, limit=max_files)
    log.info("源 wiki 共 %d 个文件", len(src_items))
    items: list[dict] = []
    changed = skipped = invalid = 0

    download_client: httpx.Client | None = None
    if not dry_run:
        download_client = httpx.Client(
            headers={"User-Agent": cfg.wiki.user_agent},
            follow_redirects=True,
        )
    try:
        for item in src_items:
            title = item["title"]
            sha1 = item["sha1"]
            prev = files_state.get(title) or {}
            if not force and prev.get("sha1") == sha1 and prev.get("uploaded"):
                skipped += 1
                continue
            cpath = _file_cache_path(cfg, item["name"])
            if not dry_run and download_client is not None:
                try:
                    blob = _download(download_client, item["url"])
                except Exception as e:  # noqa: BLE001
                    log.error("  下载失败 %s: %s", title, e)
                    continue
                blob, webp_converted = convert_animated_webp_to_gif(blob)
                if webp_converted:
                    log.info("  动态 webp 已转码为 GIF：%s", title)
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_bytes(blob)
                blob_hash = sha256_bytes(blob)
                if prev.get("blob_sha256") and prev["blob_sha256"] != blob_hash:
                    # 源端 sha1 是元数据里的原文件哈希，和实际下载到的字节不是一回事
                    log.warning(
                        "  下载字节与上次不同（源 sha1 %s）：%s",
                        "未变" if prev.get("sha1") == sha1 else "已变",
                        title,
                    )
                upload_name, real_mime, renamed, reason = _prepare_upload_file(
                    item["name"],
                    blob,
                    item.get("mime", ""),
                    on_mime_mismatch=cfg.publish.on_mime_mismatch,
                )
                if reason:
                    invalid += 1
                    log.warning("  文件预校验跳过 %s: %s", title, reason)
                    continue
                # 源端内容没变就必须沿用既有上传名。否则 CDN 换个转码格式就会改出一个
                # 新文件名，wiki 上多一份副本、旧名字变成没人维护的孤儿，正文里的链接
                # 也跟着漂——而源文件其实压根没动过。
                #
                # 但刚发生的 webp->gif 转码是例外：它是确定性的、我们主动做的修正
                # （把之前因为缩略图不支持动画 webp 而卡在 .webp 的文件迁回 .gif），
                # 不是这条保护要防的那种 CDN 随机摇摆，必须放行，否则这些文件会永远
                # 卡在坏掉的旧名字上。
                prior = prev.get("uploaded_as")
                same_source = prev.get("sha1") == sha1
                if not webp_converted and prior and same_source and upload_name != prior:
                    log.warning(
                        "  源端未变但本次嗅探出不同后缀（%s），沿用既有上传名：%s -> %s",
                        real_mime,
                        upload_name,
                        prior,
                    )
                    upload_name = prior
                    renamed = prior != item["name"]
            else:
                upload_name = item["name"]
                real_mime = item.get("mime", "")
                renamed = False
                blob_hash = ""
            items.append(
                {
                    "kind": "file",
                    "title": title,
                    "name": item["name"],
                    "upload_name": upload_name,
                    "url": item["url"],
                    "sha1": sha1,
                    "size": item.get("size", 0),
                    "mime": item.get("mime", ""),
                    "real_mime": real_mime,
                    "renamed": renamed,
                    "blob_sha256": blob_hash,
                    "cache_path": str(cpath.as_posix()),
                }
            )
            changed += 1
    finally:
        if download_client is not None:
            download_client.close()

    # MIME 校正后可能两个文件撞到同一个上传名，必须在写 manifest 前消歧，
    # 否则后上传的会静默覆盖先上传的（目标 wiki 上直接丢一张图）。
    for title, want, fixed in resolve_upload_collisions(items, files_state):
        log.warning("  上传名冲突 %s：%s -> %s", title, want, fixed)

    log.info(
        "文件：待上传 %d / 跳过 %d / 预校验跳过 %d",
        changed,
        skipped,
        invalid,
    )
    return items


def _fetch_system(
    cfg: AppConfig,
    client: FandomClient,
    state: dict,
    *,
    force: bool,
    dry_run: bool,
) -> list[dict]:
    """拉取 customised 系统消息，返回 system 清单项。"""
    sys_state = state.get("system_messages", {})
    msgs = _list_modified_messages(client)
    log.info("源 wiki 共 %d 条 customised 系统消息", len(msgs))
    items: list[dict] = []
    changed = skipped = 0

    for m in msgs:
        name = m["name"]
        content = m["content"]
        if not content:
            continue
        new_hash = sha256(content)
        prev = sys_state.get(name) or {}
        unchanged = prev.get("source_hash") == new_hash and prev.get("translation_hash")
        if not force and unchanged:
            skipped += 1
            continue
        action = "copy" if _is_code_message(name) else "translate"
        incoming = _incoming_system_path(cfg, name)
        if not dry_run:
            incoming.parent.mkdir(parents=True, exist_ok=True)
            incoming.write_text(content, encoding="utf-8")
        items.append(
            {
                "kind": "system",
                "name": name,
                "action": action,
                "source_hash": new_hash,
                "incoming_cache": str(incoming.as_posix()),
            }
        )
        changed += 1

    log.info("系统消息：待处理 %d / 跳过 %d", changed, skipped)
    return items


def run_fetch(cfg: AppConfig, *, force: bool = False, dry_run: bool = False) -> int:
    """fetch 阶段入口：拉取源 wiki 全部内容并生成 manifest.json。"""
    state = load_state(cfg.output.state_file)
    if dry_run:
        log.info("[fetch dry-run] 只枚举与比对，不下载、不写缓存、不写 manifest")

    with FandomClient(cfg.wiki.api_url, cfg.wiki.user_agent) as client:
        items: list[dict] = []
        items += _fetch_pages(cfg, client, state, force=force, dry_run=dry_run)
        items += _fetch_files(cfg, client, state, force=force, dry_run=dry_run)
        items += _fetch_system(cfg, client, state, force=force, dry_run=dry_run)

    log.info(
        "[fetch done] 清单共 %d 项：page=%d / file=%d / system=%d",
        len(items),
        sum(1 for i in items if i["kind"] == "page"),
        sum(1 for i in items if i["kind"] == "file"),
        sum(1 for i in items if i["kind"] == "system"),
    )

    if dry_run:
        return 0

    manifest = {
        "schema": 1,
        "generated_at": int(time.time()),
        "items": items,
    }
    save_manifest(cfg.strategy.manifest_file, manifest)
    log.info("已写出 %s", cfg.strategy.manifest_file)
    return 0
