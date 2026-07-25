"""主流程：translate / publish / all。

新架构对应流程图：
  - 拉取源 wiki
  - 拉取目标 wiki（推送阶段才需要）
  - 读取仓库内当前译文 + 上次源缓存
  - 三路 hash 比对决定动作：
      * 源未变 → 跳过翻译
      * 源变了 → 用 source_diff + 仓库现有译文当作 LLM 上下文，整页重译
      * 推送时目标 wiki hash 与 last_published.target_hash 不一致 → 冲突
        策略 on_target_conflict: skip / overwrite
  - 推送至目标 wiki
  - state.json + output/ + cache/source/ 同步落盘，由 GH Actions commit 回仓库
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from pathlib import Path

from .config import AppConfig, WikiConfig
from .diff import unified_diff
from .fandom import FandomClient
from .files import rewrite_file_links
from .segmenter import plan_translation, stitch
from .state import (
    get_page,
    load_state,
    load_states,
    merge_states,
    save_progress,
    save_state,
    sha256,
)
from .translator import Translator
from .utils import split_chunks, title_to_path

log = logging.getLogger(__name__)


def _output_path(cfg: AppConfig, source_title: str) -> Path:
    out_dir = Path(cfg.output.dir) / cfg.wiki.target_lang
    ns_dir, base = title_to_path(source_title)
    if ns_dir:
        return out_dir / ns_dir / (base + ".wiki")
    return out_dir / (base + ".wiki")


def _cache_path(cfg: AppConfig, source_title: str) -> Path:
    cache_dir = Path(cfg.strategy.cache_source_dir)
    ns_dir, base = title_to_path(source_title)
    if ns_dir:
        return cache_dir / ns_dir / (base + ".wiki")
    return cache_dir / (base + ".wiki")


def _read_or_empty(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def collect_titles(client: FandomClient, wiki: WikiConfig) -> list[str]:
    titles: list[str] = list(wiki.page_list)
    if wiki.category:
        ns = wiki.namespaces[0] if wiki.namespaces else 0
        titles.extend(client.list_category(wiki.category, ns=ns, limit=wiki.max_pages or 50))
    if wiki.all_pages:
        log.info(
            "拉取全部页面，命名空间=%s, 上限=%s",
            wiki.namespaces,
            wiki.max_pages or "不限",
        )
        titles.extend(
            client.list_all_pages(
                wiki.namespaces,
                limit=wiki.max_pages,
                filter_redirects=wiki.filter_redirects,
            )
        )
    seen: set[str] = set()
    uniq: list[str] = []
    for t in titles:
        norm = t.replace("_", " ")
        if norm not in seen:
            seen.add(norm)
            uniq.append(norm)
    return uniq


def _translate_page(
    cfg: AppConfig,
    translator: Translator | None,
    title: str,
    new_source: str,
    page_state: dict,
    *,
    dry_run: bool,
) -> tuple[str, dict]:
    """对单页执行翻译，返回 (translated_text, info_for_logging)。

    info 包含 chunks/old_source_hash/has_diff/has_reference 等便于 dry-run 报告。
    """
    cache_old_source = _read_or_empty(_cache_path(cfg, title))
    old_source = cache_old_source or None
    source_diff = (
        unified_diff(old_source, new_source, fromfile="old", tofile="new")
        if (cfg.strategy.use_source_diff and old_source)
        else None
    )

    reference: str | None = None
    if cfg.strategy.use_repo_reference:
        reference = _read_or_empty(_output_path(cfg, title)) or None

    chunks = split_chunks(new_source, cfg.translator.chunk_chars)
    info = {
        "chunks": len(chunks),
        "has_old_source": bool(old_source),
        "has_source_diff": bool(source_diff),
        "has_reference": bool(reference),
        "source_chars": len(new_source),
        "mode": "full",
    }

    # diff 级局部翻译：要求同时有旧源 + 旧译文 + 开关启用
    use_diff_translation = cfg.strategy.diff_translation and bool(old_source) and bool(reference)
    plan = None
    if use_diff_translation:
        plan = plan_translation(
            old_source=old_source or "",
            new_source=new_source,
            old_translation=reference or "",
        )
        if plan.needs_full_retranslate:
            log.info("  diff 级翻译回退到整页：%s", plan.reason)
            plan = None
        else:
            info["mode"] = "diff"
            info["segments_total"] = len(plan.plans)
            info["segments_translate"] = plan.translate_count
            info["segments_keep"] = plan.keep_count

    if dry_run:
        return "", info

    assert translator is not None

    if plan is not None:
        log.info(
            "  diff 级翻译：复用 %d 段，重译 %d 段（共 %d 段）",
            plan.keep_count,
            plan.translate_count,
            len(plan.plans),
        )
        translated_parts: list[str] = []
        for idx, p in enumerate(plan.plans, 1):
            if p.kind != "translate":
                continue
            log.info("    -> [diff] 翻译段 %d (%d chars)", idx, len(p.new_text))
            translated_parts.append(
                translator.translate_segment(
                    source_lang=cfg.wiki.source_lang,
                    target_lang=cfg.wiki.target_lang,
                    segment_text=p.new_text,
                    full_new_source=new_source,
                    full_reference_translation=reference,
                    title=title,
                )
            )
        return stitch(plan, translated_parts), info

    parts: list[str] = []
    # 第一块带完整上下文（diff + 参考译文）。
    # 后续块只传当前块本身，避免重复发送 diff 把 token 量打爆。
    for ci, chunk in enumerate(chunks, 1):
        log.info("  -> 翻译分块 %d/%d (%d chars)", ci, len(chunks), len(chunk))
        if ci == 1:
            translated = translator.translate(
                chunk,
                cfg.wiki.source_lang,
                cfg.wiki.target_lang,
                old_source=old_source,
                source_diff=source_diff,
                reference_translation=reference,
                title=title,
            )
        else:
            translated = translator.translate(
                chunk,
                cfg.wiki.source_lang,
                cfg.wiki.target_lang,
                title=title,
            )
        parts.append(translated)
    return "\n".join(parts), info


def _shard_titles(titles: list[str], index: int, total: int) -> list[str]:
    """把标题列表按 round-robin 切成 total 份，取第 index 份（0-based）。

    round-robin（i % total == index）比连续切片更均衡：相邻页面体量相近时，
    每个分片都能拿到长短交错的页面，避免某个分片全是大页面而拖慢整体。
    """
    if total <= 1:
        return titles
    if not (0 <= index < total):
        raise ValueError(f"SHARD_INDEX={index} 超出范围 [0,{total})")
    return [t for i, t in enumerate(titles) if i % total == index]


def _shard_env() -> tuple[int, int]:
    """读取 SHARD_INDEX / SHARD_TOTAL，缺省为单分片 (0, 1)。"""
    total = int(os.environ.get("SHARD_TOTAL", "1") or 1)
    index = int(os.environ.get("SHARD_INDEX", "0") or 0)
    if total < 1:
        total = 1
    return index, total


def _delta_path() -> str | None:
    """分片模式下用于写 delta 的 state 文件路径；普通模式返回 None。

    由 STATE_DELTA_FILE 指定，例如 state.delta.translate-0.json。
    """
    return os.environ.get("STATE_DELTA_FILE") or None


def _process_page_item(
    cfg: AppConfig,
    translator: Translator | None,
    state: dict,
    item: dict,
    *,
    rename_map: dict[str, str],
    dry_run: bool,
) -> bool:
    """处理单个 page 清单项：翻译或原样复制，写 output 并推进 cache 基线。

    返回是否成功。"""
    from .manifest import classify_title  # 局部导入避免与 fetch 形成顶层循环

    title = item["title"]
    incoming = Path(item["incoming_cache"])
    if not incoming.exists():
        log.error("  缺少 incoming 缓存：%s", incoming)
        return False
    new_source = incoming.read_text(encoding="utf-8")
    action = item.get("action") or classify_title(title)

    page = get_page(state, title)
    if action == "copy":
        info = {"mode": "copy", "source_chars": len(new_source)}
        translated = new_source
        if dry_run:
            log.info("  [pending copy] %s 字符=%d", title, len(new_source))
            return True
    else:
        translated, info = _translate_page(
            cfg, translator, title, new_source, page, dry_run=dry_run
        )
        if dry_run:
            log.info(
                "  [pending] %s 字符=%d 模式=%s",
                title,
                info.get("source_chars", len(new_source)),
                info.get("mode"),
            )
            return True

    translated = rewrite_file_links(translated, rename_map)

    out_path = _output_path(cfg, title)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(translated, encoding="utf-8")

    # 推进 diff 基线：incoming → cache/source（保持与真实源 wiki 一致，不做改名回填）
    cache_path = _cache_path(cfg, title)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(new_source, encoding="utf-8")

    page["source"] = {
        "revid": item.get("revid", 0),
        "hash": item.get("source_hash") or sha256(new_source),
        "fetched_at": int(time.time()),
    }
    page["translation"] = {
        "hash": sha256(translated),
        "file": str(out_path.as_posix()),
        "translated_at": int(time.time()),
    }
    return True


def _process_system_item(
    cfg: AppConfig,
    translator: Translator | None,
    state: dict,
    item: dict,
    *,
    rename_map: dict[str, str],
    dry_run: bool,
) -> bool:
    from .system import _output_path as _system_output_path

    name = item["name"]
    incoming = Path(item["incoming_cache"])
    if not incoming.exists():
        log.error("  缺少 incoming 缓存：%s", incoming)
        return False
    content = incoming.read_text(encoding="utf-8")
    action = item.get("action", "translate")
    is_code = action == "copy"

    if dry_run:
        log.info(
            "  [pending sys] MediaWiki:%s %s",
            name,
            "原样复制" if is_code else "调 LLM 翻译",
        )
        return True

    if is_code:
        translated = content
    else:
        assert translator is not None
        translated = translator.translate(
            content,
            cfg.wiki.source_lang,
            cfg.wiki.target_lang,
            title=f"MediaWiki:{name}",
        )

    translated = rewrite_file_links(translated, rename_map)

    out_path = _system_output_path(cfg, name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(translated, encoding="utf-8")

    sys_state = state.setdefault("system_messages", {})
    sys_state[name] = {
        "source_hash": item.get("source_hash") or sha256(content),
        "translation_hash": sha256(translated),
        "is_code": is_code,
        "file": str(out_path.as_posix()),
        "updated_at": int(time.time()),
    }
    return True


def run_process(cfg: AppConfig, *, force: bool = False, dry_run: bool = False) -> int:
    """process 阶段：读 manifest，只做非 wiki-API 工作（LLM 翻译 / 代码复制）。

    支持 SHARD_INDEX/SHARD_TOTAL 跨 job 分片；同时支持 TRANSLATOR_CONCURRENCY 控制的
    进程内线程池并发——翻译是网络 I/O 瓶颈，多个 LLM 请求同时飞能显著提速。
    文件（kind=file）无需处理，保留给 upload 阶段。
    """
    from .manifest import build_rename_map, items_of, load_manifest, shard_items

    manifest = load_manifest(cfg.strategy.manifest_file)
    rename_map = build_rename_map(manifest)
    if rename_map:
        log.info("本次有 %d 个文件因 MIME 校正被改名，将回填页面正文引用", len(rename_map))
    work_items = [it for it in items_of(manifest) if it.get("kind") in ("page", "system")]
    if not work_items:
        log.info("manifest 无需处理的 page/system 条目")
        return 0

    shard_index, shard_total = _shard_env()
    if shard_total > 1:
        total_count = len(work_items)
        work_items = shard_items(work_items, shard_index, shard_total)
        log.info(
            "分片 %d/%d：本分片处理 %d/%d 项",
            shard_index,
            shard_total,
            len(work_items),
            total_count,
        )

    delta_path = _delta_path()
    changed_pages: set[str] = set()
    changed_messages: set[str] = set()

    state = load_state(cfg.output.state_file)
    translator: Translator | None = None
    needs_llm = any(it.get("action") == "translate" for it in work_items)
    if not dry_run and needs_llm:
        translator = Translator(cfg.translator)

    workers = max(1, cfg.translator.concurrency)
    log.info("共 %d 项待处理（并发数=%d）", len(work_items), workers)
    ok = failed = done = 0
    failures: list[str] = []
    lock = threading.Lock()

    def _handle(item: dict) -> tuple[str, bool, tuple[str, str] | None]:
        """跑单个条目，返回 (label, 是否成功, (kind, ident) 或 None)。不碰共享计数器。"""
        kind = item["kind"]
        label = item.get("title") or item.get("name") or "?"
        try:
            if kind == "page":
                success = _process_page_item(
                    cfg, translator, state, item, rename_map=rename_map, dry_run=dry_run
                )
                changed_key = ("page", item["title"]) if success and not dry_run else None
            else:
                success = _process_system_item(
                    cfg, translator, state, item, rename_map=rename_map, dry_run=dry_run
                )
                changed_key = ("system", item["name"]) if success and not dry_run else None
        except Exception as e:  # noqa: BLE001
            log.error("  [error] %s: %s", label, e)
            return label, False, None
        return label, success, changed_key

    def _record(label: str, success: bool, changed_key: tuple[str, str] | None) -> None:
        """汇总结果、推进 state / 落盘：并发下唯一会碰共享状态的地方，靠 lock 串行化。"""
        nonlocal ok, failed, done
        with lock:
            done += 1
            if not success:
                failed += 1
                failures.append(label)
                log.info("[%d/%d] %s 失败", done, len(work_items), label)
                return
            ok += 1
            if changed_key:
                kind_, ident = changed_key
                (changed_pages if kind_ == "page" else changed_messages).add(ident)
            if not dry_run:
                save_progress(
                    state,
                    cfg.output.state_file,
                    delta_path=delta_path,
                    pages=changed_pages,
                    system_messages=changed_messages,
                )
            log.info("[%d/%d] %s", done, len(work_items), label)

    if workers <= 1:
        for item in work_items:
            _record(*_handle(item))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_handle, item) for item in work_items]
            for future in concurrent.futures.as_completed(futures):
                _record(*future.result())

    log.info("[process done] 成功 %d / 失败 %d", ok, failed)
    for f in failures:
        log.error("  failed: %s", f)
    return 0 if not failures else 1


def run_merge_state(cfg: AppConfig, *, force: bool = False, dry_run: bool = False) -> int:
    """合并各并行 job 产出的 state delta 到主 state.json。

    并行结构下，translate 各分片、files、system 都只写各自的 delta（由
    STATE_DELTA_DIR 指定的目录），互不覆盖。本步骤在汇总 job 中执行：
      - 以仓库现有 state.json 为 base
      - 按文件名排序依次合并所有 *.json delta（pages/system_messages/files 各 section）
      - 写回 state.json，供后续 publish 与 commit 使用
    """
    delta_dir = Path(os.environ.get("STATE_DELTA_DIR", "state-deltas"))
    if not delta_dir.exists():
        log.info("delta 目录 %s 不存在，无需合并", delta_dir)
        return 0

    delta_files = sorted(delta_dir.rglob("*.json"))
    if not delta_files:
        log.info("delta 目录 %s 下没有 *.json，无需合并", delta_dir)
        return 0

    log.info("合并 %d 个 state delta：%s", len(delta_files), [p.name for p in delta_files])
    base = load_state(cfg.output.state_file)
    deltas = load_states(list(delta_files))
    merged = merge_states(base, *deltas)

    n_pages = len(merged.get("pages", {}))
    n_sys = len(merged.get("system_messages", {}))
    n_files = len(merged.get("files", {}))
    log.info("合并结果：pages=%d / system_messages=%d / files=%d", n_pages, n_sys, n_files)

    if dry_run:
        log.info("[merge-state dry-run] 不写回 state.json")
        return 0

    save_state(cfg.output.state_file, merged)
    log.info("已写回 %s", cfg.output.state_file)
    return 0
