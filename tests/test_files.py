"""分块上传与系统消息识别测试。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wiki_translate.config import PublishConfig
from wiki_translate.fandom import FandomPublisher, UploadError
from wiki_translate.files import (
    _IDEMPOTENT_UPLOAD_CODES,
    _normalize_filename,
    _prepare_upload_file,
    _sniff_mime,
    rewrite_file_links,
)
from wiki_translate.system import _is_code_message


def test_is_code_message_css_js():
    assert _is_code_message("Common.css") is True
    assert _is_code_message("Common.js") is True
    assert _is_code_message("Foo.json") is True
    assert _is_code_message("Sidebar") is False
    assert _is_code_message("Pagetitle-view-mainpage") is False


def test_normalize_filename_png_actually_webp():
    name, renamed = _normalize_filename("Foo.png", "image/webp")
    assert renamed is True
    assert name == "Foo.webp"


def test_normalize_filename_already_correct():
    name, renamed = _normalize_filename("Foo.webp", "image/webp")
    assert renamed is False
    assert name == "Foo.webp"


def test_normalize_filename_unknown_mime_keeps_name():
    name, renamed = _normalize_filename("strange.bin", "application/x-foo")
    assert renamed is False
    assert name == "strange.bin"


def test_normalize_filename_jpeg_extension_normalized():
    name, renamed = _normalize_filename("Foo.JPEG", "image/jpeg")
    # JPEG 期望规范成小写 .jpg
    assert renamed is True
    assert name == "Foo.jpg"


def test_normalize_filename_no_extension():
    name, renamed = _normalize_filename("noext", "image/png")
    assert renamed is True
    assert name == "noext.png"


def test_rewrite_file_links_no_rename_map_returns_text_unchanged():
    text = "[[File:Foo.jpg|thumb]]"
    assert rewrite_file_links(text, {}) == text


def test_rewrite_file_links_updates_wikilink():
    text = "[[File:Foo.jpg|thumb|caption]]"
    out = rewrite_file_links(text, {"Foo.jpg": "Foo.png"})
    assert out == "[[File:Foo.png|thumb|caption]]"


def test_rewrite_file_links_updates_gallery_and_infobox_bare_name():
    text = "<gallery>\nFoo.jpg|cap\n</gallery>\n| image = Foo.jpg\n"
    out = rewrite_file_links(text, {"Foo.jpg": "Foo.png"})
    assert out == "<gallery>\nFoo.png|cap\n</gallery>\n| image = Foo.png\n"


def test_rewrite_file_links_space_underscore_equivalent():
    # 空格/下划线在 MediaWiki 文件名里等价：两种写法都要命中，替换成统一的新文件名
    text = "[[File:Bar_Baz.png]] and [[File:Bar Baz.png]]"
    out = rewrite_file_links(text, {"Bar_Baz.png": "Bar_Baz.webp"})
    assert out == "[[File:Bar_Baz.webp]] and [[File:Bar_Baz.webp]]"


def test_rewrite_file_links_does_not_match_longer_filename():
    text = "[[File:Foo.jpg.bak]]"
    out = rewrite_file_links(text, {"Foo.jpg": "Foo.png"})
    assert out == text


def test_rewrite_file_links_skips_noop_rename():
    text = "[[File:Foo.jpg]]"
    assert rewrite_file_links(text, {"Foo.jpg": "Foo.jpg"}) == text


def test_prepare_upload_file_renames_when_policy_allows():
    name, mime, renamed, reason = _prepare_upload_file(
        "Foo.png",
        b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 ",
        "image/png",
        on_mime_mismatch="rename",
    )

    assert name == "Foo.webp"
    assert mime == "image/webp"
    assert renamed is True
    assert reason == ""


def test_prepare_upload_file_returns_reason_when_policy_skips():
    name, mime, renamed, reason = _prepare_upload_file(
        "Foo.png",
        b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 ",
        "image/png",
        on_mime_mismatch="skip",
    )

    assert name == "Foo.webp"
    assert mime == "image/webp"
    assert renamed is True
    assert "MIME" in reason


def test_sniff_mime_png():
    assert _sniff_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"


def test_sniff_mime_webp():
    blob = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 "
    assert _sniff_mime(blob) == "image/webp"


def test_sniff_mime_jpeg():
    assert _sniff_mime(b"\xff\xd8\xffrest") == "image/jpeg"


def test_sniff_mime_unknown():
    assert _sniff_mime(b"random bytes here without magic") is None


def test_sniff_mime_empty():
    assert _sniff_mime(b"") is None


def _ftyp_blob(brand: bytes) -> bytes:
    # 4 字节 size + 'ftyp' + 4 字节 major_brand + 占位
    return b"\x00\x00\x00\x20" + b"ftyp" + brand + b"\x00\x00\x00\x00" + b"rest"


def test_sniff_mime_mp4_brand():
    assert _sniff_mime(_ftyp_blob(b"isom")) == "video/mp4"
    assert _sniff_mime(_ftyp_blob(b"mp42")) == "video/mp4"


def test_sniff_mime_quicktime_brand():
    # QuickTime major brand 'qt  '
    assert _sniff_mime(_ftyp_blob(b"qt  ")) == "video/quicktime"


def test_sniff_mime_m4a_brand():
    assert _sniff_mime(_ftyp_blob(b"M4A ")) == "audio/mp4"


def test_sniff_mime_m4v_brand():
    assert _sniff_mime(_ftyp_blob(b"M4V ")) == "video/x-m4v"


def test_normalize_filename_mp4_actually_mov():
    name, renamed = _normalize_filename("Foo.mp4", "video/quicktime")
    assert renamed is True
    assert name == "Foo.mov"


def test_sniff_mime_wav_is_audio():
    blob = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"fmt "
    assert _sniff_mime(blob) == "audio/wav"


@pytest.fixture
def fake_publisher(monkeypatch: pytest.MonkeyPatch):
    pub_cfg = PublishConfig(api_url="https://x.example.com/api.php")
    p = FandomPublisher(pub_cfg, "TestUA/1.0")
    p._csrf = "test-token+\\"  # noqa: SLF001
    return p


def test_upload_chunked_iterates_chunks(fake_publisher: FandomPublisher):
    p = fake_publisher
    blob = b"X" * (10 * 1024)  # 10 KB

    posts: list[dict] = []

    def fake_post(url, data=None, files=None, **kw):  # noqa: ANN001
        posts.append({"data": dict(data or {}), "has_chunk": "chunk" in (files or {})})
        offset = int(data.get("offset", "0"))
        size = int(data.get("filesize", "0"))
        # 第一次 stash 阶段
        if data.get("stash") == "1":
            new_offset = offset + len(files["chunk"][1])
            result = "Continue" if new_offset < size else "Success"
            resp = MagicMock()
            resp.json.return_value = {
                "upload": {"result": result, "filekey": "fake-filekey-1"}
            }
            resp.raise_for_status = MagicMock()
            return resp
        # commit 阶段
        resp = MagicMock()
        resp.json.return_value = {
            "upload": {"result": "Success", "filename": data.get("filename")}
        }
        resp.raise_for_status = MagicMock()
        return resp

    p.client.post = fake_post  # type: ignore[assignment]

    info = p.upload_chunked("foo.bin", blob, chunk_size=4 * 1024)
    assert info["result"] == "Success"
    # 10KB / 4KB = 3 个 stash 块 + 1 个 commit
    assert len(posts) == 4
    assert posts[0]["data"].get("stash") == "1"
    # 最后一次 commit 不带 stash，但带 filekey
    assert posts[-1]["data"].get("stash") is None
    assert posts[-1]["data"].get("filekey") == "fake-filekey-1"


def test_upload_chunked_empty_blob(fake_publisher: FandomPublisher):
    with pytest.raises(RuntimeError, match="blob 为空"):
        fake_publisher.upload_chunked("x", b"")


def test_upload_raises_typed_error(fake_publisher: FandomPublisher):
    p = fake_publisher

    def fake_post(url, data=None, files=None, **kw):  # noqa: ANN001
        resp = MagicMock()
        resp.json.return_value = {
            "error": {
                "code": "fileexists-no-change",
                "info": "The upload is an exact duplicate of File:Foo.png",
            }
        }
        resp.raise_for_status = MagicMock()
        return resp

    p.client.post = fake_post  # type: ignore[assignment]

    with pytest.raises(UploadError) as exc:
        p.upload("Foo.png", b"data")
    assert exc.value.code == "fileexists-no-change"
    assert exc.value.code in _IDEMPOTENT_UPLOAD_CODES


def test_upload_chunked_commit_typed_error(fake_publisher: FandomPublisher):
    p = fake_publisher
    blob = b"X" * 1024

    def fake_post(url, data=None, files=None, **kw):  # noqa: ANN001
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if data.get("stash") == "1":
            resp.json.return_value = {
                "upload": {"result": "Success", "filekey": "fake"}
            }
        else:
            resp.json.return_value = {
                "error": {
                    "code": "fileexists-no-change",
                    "info": "exact duplicate",
                }
            }
        return resp

    p.client.post = fake_post  # type: ignore[assignment]

    with pytest.raises(UploadError) as exc:
        p.upload_chunked("Foo.png", blob, chunk_size=4096)
    assert exc.value.code == "fileexists-no-change"
