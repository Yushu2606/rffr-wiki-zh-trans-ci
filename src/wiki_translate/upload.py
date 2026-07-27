"""upload 阶段：集中完成所有"目标 wiki 写"操作。

读取 manifest.json 与 output/，把页面、系统消息、文件统一推送到目标 wiki。
集中在单 job 顺序执行，配合 PUBLISH_SLEEP_BETWEEN 控制频率，规避 API 限流。

- 页面：走 FandomPublisher.edit，带冲突检测（PUBLISH_ON_TARGET_CONFLICT）
- 系统消息：走 edit 到 MediaWiki:<name>
- 文件：走 upload / upload_chunked，带 MIME 后缀修正

state 改动通过 STATE_DELTA_FILE 写 delta（与 process 一致，便于并行后合并）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import AppConfig
from .fandom import FandomPublisher, UploadError
from .files import (
    _IDEMPOTENT_UPLOAD_CODES,
    _prepare_upload_file,
)
from .manifest import items_of, load_manifest
from .pipeline import _output_path
from .state import get_page, load_state, save_progress, sha256
from .system import _output_path as _system_output_path

log = logging.getLogger(__name__)


# __UPLOAD_BODY__
def _upload_page(
    cfg: AppConfig,
    publisher: FandomPublisher,
    state: dict,
    item: dict,
    *,
    force: bool,
    dry_run: bool,
) -> str:
    """推送单个页面。返回结果标签：pushed / skipped / conflict / failed。"""
    pub = cfg.publish
    source_title = item["title"]
    page = get_page(state, source_title)
    out_path = _output_path(cfg, source_title)
    if not out_path.exists():
        log.warning("  缺少译文文件 %s，跳过", out_path)
        return "skipped"
    content = out_path.read_text(encoding="utf-8")
    content_hash = sha256(content)

    target_title = publisher.map_title(source_title)
    last_pub = page.get("last_published") or {}
    should_push = force or (last_pub.get("translation_hash") != content_hash)

    target_hash = ""
    target_revid = 0
    target_local: str | None = None
    if cfg.strategy.fetch_target_before_publish or last_pub:
        if dry_run:
            log.info("  [dry-run] 将检查目标 %s 冲突并推送", target_title)
            return "pushed"
        fetched = publisher.fetch_page(target_title)
        if fetched:
            target_local, target_revid = fetched
            target_hash = sha256(target_local)
        page["target"] = {
            "title": target_title,
            "revid": target_revid,
            "hash": target_hash,
            "fetched_at": int(time.time()),
        }

    conflict = (
        bool(last_pub)
        and bool(target_hash)
        and bool(last_pub.get("target_hash"))
        and target_hash != last_pub["target_hash"]
    )
    if conflict:
        log.warning("  目标 wiki 上次推送后被人改过：%s", source_title)
        if pub.on_target_conflict == "skip" and not force:
            log.warning("  策略=skip，跳过")
            return "conflict"
        # overwrite / force / merge(降级为覆盖：upload 阶段不再调 LLM) 直接覆盖
        if pub.on_target_conflict == "merge":
            log.warning("  upload 阶段不做 LLM 合并，按 overwrite 处理")

    if not should_push and target_hash and target_hash == content_hash:
        log.info("  目标已与译文一致，跳过：%s", source_title)
        return "skipped"

    if dry_run:
        log.info("  [dry-run] 将推送 %d 字符 -> %s", len(content), target_title)
        return "pushed"

    try:
        info = publisher.edit(target_title, content)
    except Exception as e:  # noqa: BLE001
        log.error("  推送失败: %s", e)
        return "failed"
    page["last_published"] = {
        "target_revid": info.get("newrevid", target_revid),
        "target_hash": content_hash,
        "translation_hash": content_hash,
        "published_at": int(time.time()),
    }
    log.info("  ok newrevid=%s", info.get("newrevid"))
    return "pushed"


def _upload_system(
    cfg: AppConfig,
    publisher: FandomPublisher,
    state: dict,
    item: dict,
    *,
    dry_run: bool,
) -> str:
    name = item["name"]
    full_title = f"MediaWiki:{name}"
    out_path = _system_output_path(cfg, name)
    if not out_path.exists():
        log.warning("  缺少译文文件 %s，跳过", out_path)
        return "skipped"
    content = out_path.read_text(encoding="utf-8")
    if dry_run:
        log.info("  [dry-run] 将推送 %s", full_title)
        return "pushed"
    try:
        info = publisher.edit(full_title, content)
    except Exception as e:  # noqa: BLE001
        log.error("  推送失败: %s", e)
        return "failed"
    sys_state = state.setdefault("system_messages", {})
    entry = sys_state.setdefault(name, {})
    entry["published_at"] = int(time.time())
    entry["published_revid"] = info.get("newrevid")
    log.info("  ok newrevid=%s", info.get("newrevid"))
    return "pushed"


def _upload_file(
    cfg: AppConfig,
    publisher: FandomPublisher,
    state: dict,
    item: dict,
    *,
    dry_run: bool,
) -> str:
    pub = cfg.publish
    title = item["title"]
    name = item["name"]
    cache_path = Path(item.get("cache_path", ""))
    if not cache_path.exists():
        log.warning("  缺少文件缓存 %s，跳过", cache_path)
        return "skipped"
    if dry_run:
        log.info("  [dry-run] 将上传 %s", name)
        return "pushed"

    blob = cache_path.read_bytes()
    upload_name = item.get("upload_name") or ""
    real_mime = item.get("real_mime") or ""
    renamed = bool(item.get("renamed"))
    if not upload_name or not real_mime:
        upload_name, real_mime, renamed, reason = _prepare_upload_file(
            name,
            blob,
            item.get("mime", ""),
            on_mime_mismatch=pub.on_mime_mismatch,
        )
        if reason:
            log.warning("  %s，跳过", reason)
            return "failed"
    if renamed:
        log.info("  MIME 修正：%s -> %s", name, upload_name)

    files_state = state.setdefault("files", {})
    entry = files_state.setdefault(title, {})
    try:
        threshold = int(pub.chunk_threshold_mb * 1024 * 1024)
        chunk_size = max(1, int(pub.chunk_size_mb * 1024 * 1024))
        if len(blob) > threshold:
            info = publisher.upload_chunked(
                upload_name, blob, chunk_size=chunk_size, comment=pub.summary
            )
        else:
            info = publisher.upload(upload_name, blob, comment=pub.summary)
    except UploadError as e:
        if e.code in _IDEMPOTENT_UPLOAD_CODES:
            log.info("  目标已存在且内容一致 (%s)", e.code)
            entry.update(
                sha1=item.get("sha1", ""),
                blob_sha256=item.get("blob_sha256", ""),
                uploaded=True,
                uploaded_at=int(time.time()),
                uploaded_as=upload_name,
            )
            return "skipped"
        log.error("  上传失败: %s", e)
        return "failed"
    except Exception as e:  # noqa: BLE001
        log.error("  上传失败: %s", e)
        return "failed"

    entry.update(
        sha1=item.get("sha1", ""),
        blob_sha256=item.get("blob_sha256", ""),
        size=item.get("size", len(blob)),
        mime=item.get("mime", ""),
        uploaded=True,
        uploaded_at=int(time.time()),
        uploaded_as=upload_name,
        upload_info={"result": info.get("result"), "filename": info.get("filename")},
    )
    log.info("  上传 ok result=%s", info.get("result"))
    return "pushed"


def _label(kind: str, item: dict) -> str:
    return f"MediaWiki:{item['name']}" if kind == "system" else item["title"]


def run_upload(cfg: AppConfig, *, force: bool = False, dry_run: bool = False) -> int:
    """upload 阶段入口：读 manifest 与 output，把全部内容统一推送目标 wiki。"""
    pub = cfg.publish
    if not pub.enabled:
        log.info("publish.enabled=false，跳过上传")
        return 0
    if not pub.api_url:
        log.error("未设置 PUBLISH_API_URL，跳过上传")
        return 1

    manifest = load_manifest(cfg.strategy.manifest_file)
    pages = items_of(manifest, "page")
    systems = items_of(manifest, "system")
    files = items_of(manifest, "file")
    if not (pages or systems or files):
        log.info("manifest 为空，无需上传")
        return 0

    delta_path = os.environ.get("STATE_DELTA_FILE") or None
    changed_pages: set[str] = set()
    changed_messages: set[str] = set()
    changed_files: set[str] = set()

    state = load_state(cfg.output.state_file)
    counters = {"pushed": 0, "skipped": 0, "conflict": 0, "failed": 0}
    failures: list[str] = []

    def _save() -> None:
        if not dry_run:
            save_progress(
                state,
                cfg.output.state_file,
                delta_path=delta_path,
                pages=changed_pages,
                system_messages=changed_messages,
                files=changed_files,
            )

    publisher = FandomPublisher(pub, cfg.wiki.user_agent)
    if not dry_run:
        publisher.login()
    # 软时间预算：CI 的 job timeout 是硬杀，被杀时本次已完成的进度还没提交回仓库，
    # 下次运行又从头再来——积压一大就永远追不上。这里在逼近上限时主动收工，
    # 让调用方有机会提交 state，把已完成的部分固化下来，剩下的下次接着做。
    deadline = time.monotonic() + pub.time_budget_seconds if pub.time_budget_seconds > 0 else None
    remaining: list[str] = []

    def _out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    try:
        total = len(pages) + len(systems) + len(files)
        log.info("待上传：page=%d / system=%d / file=%d", len(pages), len(systems), len(files))
        if deadline is not None:
            log.info("时间预算 %d 秒，用尽后会保存进度并正常退出", pub.time_budget_seconds)
        idx = 0
        stopped = False
        for kind, batch in (("page", pages), ("system", systems), ("file", files)):
            if stopped:
                remaining.extend(_label(kind, it) for it in batch)
                continue
            for pos, item in enumerate(batch):
                if _out_of_time():
                    stopped = True
                    remaining.extend(_label(kind, it) for it in batch[pos:])
                    break
                idx += 1
                label = _label(kind, item)
                log.info("[%d/%d] %s %s", idx, total, kind, label)
                # 逐项兜底：单项抛异常（例如 fetch_page 重试耗尽）不能拖垮整轮。
                # 否则前面已经推上 wiki 的进度会因为进程崩溃而提交不回仓库，下次重推。
                try:
                    if kind == "page":
                        res = _upload_page(
                            cfg, publisher, state, item, force=force, dry_run=dry_run
                        )
                        if res == "pushed" and not dry_run:
                            changed_pages.add(item["title"])
                    elif kind == "system":
                        res = _upload_system(cfg, publisher, state, item, dry_run=dry_run)
                        if res == "pushed" and not dry_run:
                            changed_messages.add(item["name"])
                    else:
                        res = _upload_file(cfg, publisher, state, item, dry_run=dry_run)
                        if res in ("pushed", "skipped") and not dry_run:
                            changed_files.add(item["title"])
                except Exception as e:  # noqa: BLE001
                    log.error("  [error] %s: %s", label, e)
                    res = "failed"
                counters[res] = counters.get(res, 0) + 1
                if res == "failed":
                    failures.append(label)
                _save()
                if pub.sleep_between > 0 and not dry_run:
                    time.sleep(pub.sleep_between)
    finally:
        if not dry_run:
            publisher.logout()
        publisher.close()

    log.info(
        "[upload done] 推送 %d / 跳过 %d / 冲突 %d / 失败 %d",
        counters["pushed"],
        counters["skipped"],
        counters["conflict"],
        counters["failed"],
    )
    if remaining:
        # 不算失败：进度已保存，下次运行会从这里接着做。但必须显式报出来，
        # 否则"只传了一半"会被误读成"全部完成"。
        log.warning(
            "时间预算用尽，本次未处理 %d 项（进度已保存，下次运行继续）：",
            len(remaining),
        )
        for label in remaining[:20]:
            log.warning("  pending: %s", label)
        if len(remaining) > 20:
            log.warning("  ...另有 %d 项", len(remaining) - 20)
    for f in failures:
        log.error("  failed: %s", f)
    return 0 if not failures else 1
