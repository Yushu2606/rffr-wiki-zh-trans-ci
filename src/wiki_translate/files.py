"""文件同步：从源 wiki 下载文件并上传到目标 wiki。

MediaWiki 文件操作约定：
- 源端：action=query&prop=imageinfo&iiprop=url|sha1|size 拿原始 URL；直接 GET 下载
- 目标端：action=upload，需要 csrf token + multipart 上传文件 binary
- 重名策略：默认 ignorewarnings=1（覆盖同名/小变更也允许）
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import AppConfig
from .fandom import FandomClient
from .utils import safe_filename

log = logging.getLogger(__name__)


_HTTP_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
)


# 这些 MediaWiki 错误码代表"目标已经处于期望状态"，无需当作失败：
# - fileexists-no-change: 已存在且 sha1 完全相同
# - fileexists                : 同名已存在但内容相同（部分 wiki 这样回）
# - was-deleted               : 文件名上一个版本被删；提交后实际已成功上传新版（少见）
_IDEMPOTENT_UPLOAD_CODES: frozenset[str] = frozenset(
    {
        "fileexists-no-change",
    }
)


# MIME → 标准后缀（小写，含点）。仅覆盖 Fandom 上常见类型；未列出的类型保持原后缀。
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "application/ogg": ".ogg",
    "application/pdf": ".pdf",
    "application/json": ".json",
}


def _prepare_upload_file(
    filename: str,
    blob: bytes,
    server_mime: str,
    *,
    on_mime_mismatch: str,
) -> tuple[str, str, bool, str]:
    """Resolve the upload filename and return a skip reason when policy rejects it."""
    real_mime = _sniff_mime(blob) or server_mime
    upload_name, renamed = _normalize_filename(filename, real_mime)
    if renamed and on_mime_mismatch in ("skip", "strict"):
        reason = f"MIME 与后缀不一致 ({real_mime})，策略={on_mime_mismatch}"
        return upload_name, real_mime, renamed, reason
    return upload_name, real_mime, renamed, ""


def _sniff_isobmff(blob: bytes) -> str | None:
    """解析 ISO Base Media 容器（mp4 / mov / m4v / m4a）的 ftyp major brand。

    结构：4 字节 size + 'ftyp' + 4 字节 major_brand + ...
    QuickTime/MOV 的 major brand 是 'qt  '，其余按 brand 归到对应 MIME。
    """
    if len(blob) < 12 or blob[4:8] != b"ftyp":
        return None
    major = blob[8:12]
    brand = major.decode("ascii", errors="replace").strip().lower()
    if brand in ("qt",):
        return "video/quicktime"
    if brand.startswith("m4a"):
        return "audio/mp4"
    if brand.startswith("m4v"):
        return "video/x-m4v"
    # isom / iso2 / mp41 / mp42 / avc1 / dash / 其它 → 视为标准 mp4
    return "video/mp4"


def _sniff_mime(blob: bytes) -> str | None:
    """根据 magic number 探测真实 MIME，仅覆盖 Fandom 上最常见的几种格式。"""
    if not blob:
        return None
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return "audio/wav"
    if blob[:4] == b"RIFF" and blob[8:12] == b"AVI ":
        return "video/x-msvideo"
    if blob[:4] == b"OggS":
        return "application/ogg"
    if blob[:3] == b"ID3" or blob[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if blob[:4] == b"fLaC":
        return "audio/flac"
    if blob[:4] == b"\x1aE\xdf\xa3":
        return "video/x-matroska"
    if blob[4:8] == b"ftyp":
        return _sniff_isobmff(blob)
    return None


def _normalize_filename(filename: str, mime: str) -> tuple[str, bool]:
    """根据真实 MIME 把 filename 修正为正确后缀。

    返回 (新文件名, 是否被改过)；MIME 未在表中或后缀已正确则原样返回。
    """
    expected = _MIME_TO_EXT.get((mime or "").lower())
    if not expected:
        return filename, False
    # split 取最后一个 "."
    if "." in filename:
        base, _dot, _ext = filename.rpartition(".")
        cur_ext = "." + _ext.lower()
    else:
        base = filename
        cur_ext = ""
    if cur_ext == expected:
        return filename, False
    return base + expected, True


@lru_cache(maxsize=4096)
def _filename_pattern(name: str) -> re.Pattern[str]:
    """匹配 wikitext 里对某文件名的引用：空格/下划线等价，首字母大小写不敏感（MediaWiki 标题规则）。

    前后不接单词字符/"."/"/"，避免命中更长文件名的一部分（例如 "Foo.jpg" 不应
    命中 "Foo.jpg.bak" 或 "notFoo.jpg" 里的子串）。

    带缓存：改名表可能上千条，而每个页面都要整表过一遍，重复编译正则会成为瓶颈。
    """
    # 先按分隔符切开再逐段转义，最后用 [ _] 连接。不能先整体 re.escape 再替换分隔符：
    # re.escape 会把空格转义成 "\ "，替换其中的空格会得到 "\[ _]"（匹配字面量方括号），
    # 导致所有含空格的文件名静默失配。
    parts = [re.escape(p) for p in re.split(r"[ _]", name)]
    if parts and parts[0] and parts[0][0].isalpha():
        first = parts[0]
        parts[0] = f"[{first[0].lower()}{first[0].upper()}]{first[1:]}"
    escaped = "[ _]".join(parts)
    return re.compile(rf"(?<![\w./]){escaped}(?![\w.])")


def rewrite_file_links(text: str, rename_map: dict[str, str]) -> str:
    """把正文里对已改名文件的引用替换成实际上传的文件名。

    rename_map 来自 manifest.build_rename_map：{下载时的旧文件名: 上传时因 MIME
    校正而使用的新文件名}。覆盖 [[File:...]] / [[Image:...]] / 中文别名、
    <gallery> 条目、infobox image= 参数等一切裸文件名写法——正则按整个文件名
    做替换，不区分具体语法结构。
    """
    if not rename_map:
        return text
    # 预筛：改名表可能上千条，而单页通常只引用几个文件。先用廉价的子串检查排掉绝大
    # 多数候选，再跑正则。两侧都归一化成"小写 + 下划线转空格"，是正则匹配条件的放宽
    # （正则只对首字母大小写不敏感、空格下划线等价），因此不会漏掉任何真实命中。
    probe = text.lower().replace("_", " ")
    for old_name, new_name in rename_map.items():
        if old_name == new_name:
            continue
        if old_name.lower().replace("_", " ") not in probe:
            continue
        text = _filename_pattern(old_name).sub(new_name.replace("\\", "\\\\"), text)
    return text


def _disambiguate_name(desired: str, original: str, claimed: set[str]) -> str:
    """给撞车的上传名找一个不冲突的替代名。

    用原始后缀做区分（D140.gif -> D140.webp 撞车 -> D140_gif.webp），这样结果只取决于
    (目标名, 原文件名)，与处理顺序无关，重跑能得到同一个名字，不会反复改名重传。
    """
    stem, _, ext = desired.rpartition(".")
    if not stem:  # 没有后缀
        stem, ext = desired, ""
    orig_ext = original.rpartition(".")[2].lower()
    suffix = f"_{orig_ext}" if orig_ext else "_alt"

    def build(n: int) -> str:
        tail = suffix if n < 2 else f"{suffix}_{n}"
        return f"{stem}{tail}.{ext}" if ext else f"{stem}{tail}"

    n = 1
    while build(n) in claimed:
        n += 1
    return build(n)


def _claim_priority(item: dict[str, Any], files_state: dict[str, Any]) -> tuple[int, int, str]:
    """谁更有资格拿到不带后缀的目标名——越小越优先。

    1. 未改名的文件：这本来就是它自己的名字，优先级最高。
    2. 历史上已用该名字传过的：保住既成事实，避免无谓改名重传。同一个名字有多个
       历史声明时（正是撞车留下的烂摊子），上传时间最晚的那个才是当前 wiki 上的内容。
    3. 其余：按标题排序，保证与源 wiki 分页顺序无关、重跑可复现。
    """
    want = item.get("upload_name") or item["name"]
    if not item.get("renamed"):
        return (0, 0, item["title"])
    entry = files_state.get(item["title"]) or {}
    if entry.get("uploaded_as") == want:
        return (1, -int(entry.get("uploaded_at") or 0), item["title"])
    return (2, 0, item["title"])


def resolve_upload_collisions(
    items: list[dict[str, Any]], files_state: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """就地消解上传名冲突，返回 [(标题, 原本想用的名字, 改后的名字)]。

    MIME 校正只改后缀，不看目标名是否已被占用，于是 D140.gif 和 D140.png（实际都是
    webp）会双双变成 D140.webp，后上传的把先上传的覆盖掉——目标 wiki 上直接少一张图。

    占用判定同时考虑历史（state 里其它文件的 uploaded_as）与本批；未改名的文件对自己
    的原名有优先权，改名的一方让路。
    """
    batch_titles = {it["title"] for it in items}
    claimed: set[str] = {
        entry["uploaded_as"]
        for title, entry in files_state.items()
        if title not in batch_titles and entry.get("uploaded_as")
    }

    changes: list[tuple[str, str, str]] = []
    for it in sorted(items, key=lambda x: _claim_priority(x, files_state)):
        want = it.get("upload_name") or it["name"]
        if want in claimed:
            fixed = _disambiguate_name(want, it["name"], claimed)
            it["upload_name"] = fixed
            it["renamed"] = True
            changes.append((it["title"], want, fixed))
            want = fixed
        claimed.add(want)
    return changes


def _list_source_files(client: FandomClient, limit: int = 0) -> list[dict[str, Any]]:
    """列出源 wiki 上 ns=6 的所有文件，返回 [{title, url, sha1, size, mime}]。"""
    out: list[dict[str, Any]] = []
    aicontinue: str | None = None
    while True:
        if limit > 0 and len(out) >= limit:
            return out
        remaining = (limit - len(out)) if limit > 0 else 500
        params: dict[str, Any] = {
            "action": "query",
            "list": "allimages",
            "ailimit": min(500, remaining) if limit > 0 else 500,
            "aiprop": "url|sha1|size|mime",
        }
        if aicontinue:
            params["aicontinue"] = aicontinue
        data = client._get(params)  # noqa: SLF001
        for img in data.get("query", {}).get("allimages", []):
            out.append(
                {
                    "title": img.get("title") or f"File:{img.get('name', '')}",
                    "name": img.get("name") or "",
                    "url": img.get("url") or "",
                    "sha1": img.get("sha1") or "",
                    "size": img.get("size", 0),
                    "mime": img.get("mime") or "",
                }
            )
            if limit > 0 and len(out) >= limit:
                return out
        aicontinue = data.get("continue", {}).get("aicontinue")
        if not aicontinue:
            break
    return out


@_HTTP_RETRY
def _download(client: httpx.Client, url: str) -> bytes:
    resp = client.get(url, timeout=120.0)
    resp.raise_for_status()
    return resp.content


def _file_cache_path(cfg: AppConfig, name: str) -> Path:
    return Path(cfg.strategy.cache_source_dir).parent / "files" / safe_filename(name)
