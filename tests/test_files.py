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
    resolve_upload_collisions,
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


def test_rewrite_file_links_rename_key_with_spaces():
    """改名表的 key 带空格时也要能命中——state.json 里的文件名就是空格形式。

    回归用例：早先实现先 re.escape 整个名字再替换分隔符，而 re.escape 会把空格
    转义成 "\\ "，替换后变成 "\\[ _]"（匹配字面量方括号），含空格的文件名全部静默失配。
    """
    text = "<gallery>\nFile:Section A Tight Hallway.png|cap\n</gallery>"
    out = rewrite_file_links(text, {"Section A Tight Hallway.png": "Section_A_Tight_Hallway.webp"})
    assert out == "<gallery>\nFile:Section_A_Tight_Hallway.webp|cap\n</gallery>"


def test_rewrite_file_links_spaced_key_matches_underscored_text():
    """key 用空格、正文用下划线（MediaWiki 两种写法等价）也必须命中。"""
    text = "[[File:A-120_Recreated.png|thumb]]"
    out = rewrite_file_links(text, {"A-120 Recreated.png": "A-120_Recreated.webp"})
    assert out == "[[File:A-120_Recreated.webp|thumb]]"


def test_rewrite_file_links_does_not_match_longer_filename():
    text = "[[File:Foo.jpg.bak]]"
    out = rewrite_file_links(text, {"Foo.jpg": "Foo.png"})
    assert out == text


def test_rewrite_file_links_skips_noop_rename():
    text = "[[File:Foo.jpg]]"
    assert rewrite_file_links(text, {"Foo.jpg": "Foo.jpg"}) == text


def _file_item(name: str, upload_name: str, renamed: bool = True) -> dict:
    return {
        "kind": "file",
        "title": f"File:{name}",
        "name": name,
        "upload_name": upload_name,
        "renamed": renamed,
    }


def test_resolve_collisions_two_renamed_to_same_target():
    """D140.gif 与 D140.png 实际都是 webp 时，不能双双变成 D140.webp。"""
    items = [
        _file_item("D140.gif", "D140.webp"),
        _file_item("D140.png", "D140.webp"),
    ]
    changes = resolve_upload_collisions(items, {})
    names = sorted(it["upload_name"] for it in items)
    assert len(set(names)) == 2, "两个文件必须拿到不同的上传名"
    assert names == ["D140.webp", "D140_png.webp"]
    assert changes == [("File:D140.png", "D140.webp", "D140_png.webp")]


def test_resolve_collisions_unrenamed_file_keeps_its_own_name():
    """未改名的文件对自己的原名有优先权，改名的一方让路。"""
    items = [
        _file_item("A-258.webp", "A-258.webp", renamed=False),
        _file_item("A-258.gif", "A-258.webp"),
    ]
    resolve_upload_collisions(items, {})
    by_name = {it["name"]: it["upload_name"] for it in items}
    assert by_name["A-258.webp"] == "A-258.webp"
    assert by_name["A-258.gif"] == "A-258_gif.webp"


def test_resolve_collisions_respects_previously_uploaded_names():
    """历史上别的文件已占用该名字时也要避开——那个文件这次没变化、不在本批里。"""
    items = [_file_item("Sha200.gif", "Sha200.webp")]
    state = {"File:Sha200.webp": {"uploaded_as": "Sha200.webp", "uploaded": True}}
    resolve_upload_collisions(items, state)
    assert items[0]["upload_name"] == "Sha200_gif.webp"


def test_resolve_collisions_ignores_own_previous_name():
    """文件重传时应能沿用自己上次的名字，不该被自己挡住。"""
    items = [_file_item("D140.gif", "D140.webp")]
    state = {"File:D140.gif": {"uploaded_as": "D140.webp", "uploaded": True}}
    resolve_upload_collisions(items, state)
    assert items[0]["upload_name"] == "D140.webp"


def test_resolve_collisions_is_order_independent():
    """结果不能依赖源 wiki 的分页顺序，否则重跑会改名重传。"""
    a = [_file_item("D140.gif", "D140.webp"), _file_item("D140.png", "D140.webp")]
    b = [_file_item("D140.png", "D140.webp"), _file_item("D140.gif", "D140.webp")]
    resolve_upload_collisions(a, {})
    resolve_upload_collisions(b, {})
    assert {i["name"]: i["upload_name"] for i in a} == {
        i["name"]: i["upload_name"] for i in b
    }


def test_resolve_collisions_no_conflict_leaves_names_untouched():
    items = [_file_item("Foo.png", "Foo.webp"), _file_item("Bar.png", "Bar.webp")]
    assert resolve_upload_collisions(items, {}) == []
    assert [it["upload_name"] for it in items] == ["Foo.webp", "Bar.webp"]


def test_resolve_collisions_latest_upload_keeps_the_plain_name():
    """撞车留下的烂摊子：两个文件都记着同一个 uploaded_as，最后传的那个才在 wiki 上。

    必须让它保住原名，否则修复反而会把当前正确的页面引用指到别的图上。
    """
    items = [
        _file_item("E10i.jpg", "E10i.webp"),
        _file_item("E10i.png", "E10i.webp"),
    ]
    state = {
        "File:E10i.jpg": {"uploaded_as": "E10i.webp", "uploaded_at": 100},
        "File:E10i.png": {"uploaded_as": "E10i.webp", "uploaded_at": 200},  # 更晚，在 wiki 上
    }
    resolve_upload_collisions(items, state)
    by_name = {it["name"]: it["upload_name"] for it in items}
    assert by_name["E10i.png"] == "E10i.webp", "当前 wiki 上的那个应保住原名"
    assert by_name["E10i.jpg"] == "E10i_jpg.webp"


def test_resolve_collisions_three_way_gets_distinct_names():
    items = [
        _file_item("X.gif", "X.webp"),
        _file_item("X.png", "X.webp"),
        _file_item("X.jpg", "X.webp"),
    ]
    resolve_upload_collisions(items, {})
    names = [it["upload_name"] for it in items]
    assert len(set(names)) == 3


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
