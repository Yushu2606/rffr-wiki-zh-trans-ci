"""文件同步：从源 wiki 下载文件并上传到目标 wiki。

MediaWiki 文件操作约定：
- 源端：action=query&prop=imageinfo&iiprop=url|sha1|size 拿原始 URL；直接 GET 下载
- 目标端：action=upload，需要 csrf token + multipart 上传文件 binary
- 重名策略：默认 ignorewarnings=1（覆盖同名/小变更也允许）
"""

from __future__ import annotations

import logging
import re
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


def _filename_pattern(name: str) -> re.Pattern[str]:
    """匹配 wikitext 里对某文件名的引用：空格/下划线等价，首字母大小写不敏感（MediaWiki 标题规则）。

    前后不接单词字符/"."/"/"，避免命中更长文件名的一部分（例如 "Foo.jpg" 不应
    命中 "Foo.jpg.bak" 或 "notFoo.jpg" 里的子串）。
    """
    escaped = re.escape(name)
    escaped = re.sub(r"[ _]", "[ _]", escaped)
    if escaped and escaped[0].isalpha():
        escaped = f"[{escaped[0].lower()}{escaped[0].upper()}]{escaped[1:]}"
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
    for old_name, new_name in rename_map.items():
        if old_name == new_name:
            continue
        text = _filename_pattern(old_name).sub(new_name.replace("\\", "\\\\"), text)
    return text


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
