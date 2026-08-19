"""fetch 阶段的文件同步：上传名稳定性与下载字节漂移检测。"""
from __future__ import annotations

import pytest

from wiki_translate import fetch as ft
from wiki_translate.config import (
    AppConfig,
    OutputConfig,
    PublishConfig,
    StrategyConfig,
    TranslatorConfig,
    WikiConfig,
)
from wiki_translate.state import sha256_bytes

# 真实的 webp 魔数，会被 _sniff_mime 认出来（非真实可解码图像，仅用于魔数级测试）
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40


def _real_animated_webp() -> bytes:
    """真正可被 Pillow 解码的动图 webp，用于测试 fetch.py 里的转码集成。"""
    from io import BytesIO

    from PIL import Image

    frames = [Image.new("RGBA", (4, 4), c) for c in [(255, 0, 0, 255), (0, 255, 0, 255)]]
    buf = BytesIO()
    frames[0].save(
        buf, format="WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    return buf.getvalue()


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        wiki=WikiConfig(api_url="http://example.invalid"),
        translator=TranslatorConfig(api_key="unused"),
        output=OutputConfig(dir=str(tmp_path / "output"), state_file=str(tmp_path / "state.json")),
        publish=PublishConfig(on_mime_mismatch="rename"),
        strategy=StrategyConfig(cache_source_dir=str(tmp_path / "cache" / "source")),
    )


def _run(monkeypatch, cfg, *, src_items, blob, state, force=False):
    monkeypatch.setattr(ft, "_list_source_files", lambda client, limit=0: src_items)
    monkeypatch.setattr(ft, "_download", lambda client, url: blob)
    return ft._fetch_files(cfg, object(), state, force=force, dry_run=False)


def _src(name="D140.gif", sha1="aaa"):
    return [
        {
            "title": f"File:{name}",
            "name": name,
            "url": "http://example.invalid/x",
            "sha1": sha1,
            "size": 10,
            "mime": "image/gif",
        }
    ]


def test_fresh_file_gets_mime_corrected_name(monkeypatch, cfg):
    """没有历史记录时，按实际嗅探到的 MIME 改名。"""
    items = _run(monkeypatch, cfg, src_items=_src(), blob=WEBP, state={})
    assert items[0]["upload_name"] == "D140.webp"
    assert items[0]["renamed"] is True


def test_records_downloaded_blob_hash(monkeypatch, cfg):
    items = _run(monkeypatch, cfg, src_items=_src(), blob=WEBP, state={})
    assert items[0]["blob_sha256"] == sha256_bytes(WEBP)


def test_force_rerun_keeps_prior_name_when_source_unchanged(monkeypatch, cfg):
    """--force 重跑时，源端 sha1 未变就必须沿用既有上传名。

    回归用例：Fandom CDN 对同一个 .gif 有时返回原始 gif、有时返回 webp 转码版，
    源 sha1 却一直不变。若跟着嗅探结果改名，wiki 上会多出一份副本、旧名变孤儿，
    正文链接也跟着漂——而源文件压根没动过。
    """
    state = {
        "files": {
            "File:D140.gif": {
                "sha1": "aaa",
                "uploaded": True,
                "uploaded_as": "D140.webp",
                "blob_sha256": sha256_bytes(WEBP),
            }
        }
    }
    # 这次下回来是原始 gif（嗅探结果与上次不同），但源 sha1 没变
    items = _run(
        monkeypatch, cfg, src_items=_src(sha1="aaa"), blob=GIF, state=state, force=True
    )
    assert items[0]["upload_name"] == "D140.webp", "应沿用既有名字，不能跟着嗅探漂"


def test_source_changed_allows_new_name(monkeypatch, cfg):
    """源端真的变了就该重新判定，不受旧名字束缚。"""
    state = {
        "files": {
            "File:D140.gif": {
                "sha1": "old",
                "uploaded": True,
                "uploaded_as": "D140.webp",
            }
        }
    }
    items = _run(monkeypatch, cfg, src_items=_src(sha1="new"), blob=GIF, state=state)
    assert items[0]["upload_name"] == "D140.gif"
    assert items[0]["renamed"] is False


def test_blob_drift_is_logged(monkeypatch, cfg, caplog):
    """上次上传失败（uploaded 未置位）导致重下时，字节漂移要留痕。"""
    state = {
        "files": {
            "File:D140.gif": {
                "sha1": "aaa",
                "uploaded_as": "D140.webp",
                "blob_sha256": sha256_bytes(WEBP),
            }
        }
    }
    with caplog.at_level("WARNING"):
        _run(monkeypatch, cfg, src_items=_src(sha1="aaa"), blob=GIF, state=state)
    assert any("下载字节与上次不同" in r.getMessage() for r in caplog.records)


def test_force_rerun_migrates_stuck_animated_webp_to_gif(monkeypatch, cfg):
    """--force 重跑时，之前卡在坏掉的 .webp 名字上的动图必须能迁回 .gif。

    回归用例：Fandom 缩略图服务不支持动画 webp（实测 1528 个里 242 个白方块）。
    这些文件的 state 记录着 sha1 未变 + uploaded_as 是旧的 .webp 名字——如果稳定性
    保护（见上面 test_force_rerun_keeps_prior_name_when_source_unchanged）不分青红
    皂白一律"沿用旧名"，这些文件就永远修不好。webp->gif 转码是确定性修正，必须
    绕开这条保护。
    """
    animated = _real_animated_webp()
    state = {
        "files": {
            "File:D140.gif": {
                "sha1": "aaa",
                "uploaded": True,
                "uploaded_as": "D140.webp",  # 历史上因为动图 webp 卡住的坏名字
            }
        }
    }
    items = _run(
        monkeypatch, cfg, src_items=_src(sha1="aaa"), blob=animated, state=state, force=True
    )
    assert items[0]["upload_name"] == "D140.gif", "动图必须迁回 .gif，不能被稳定性保护卡住"


def test_force_rerun_still_locks_static_webp_name(monkeypatch, cfg):
    """非动图场景下，稳定性保护必须照常生效——这个改动不能松绑无关场景。"""
    state = {
        "files": {
            "File:D140.gif": {
                "sha1": "aaa",
                "uploaded": True,
                "uploaded_as": "D140.webp",
                "blob_sha256": sha256_bytes(WEBP),
            }
        }
    }
    items = _run(
        monkeypatch, cfg, src_items=_src(sha1="aaa"), blob=GIF, state=state, force=True
    )
    assert items[0]["upload_name"] == "D140.webp", "非转码场景应保持原有稳定性行为"


def test_unchanged_and_uploaded_is_skipped_entirely(monkeypatch, cfg):
    state = {
        "files": {
            "File:D140.gif": {"sha1": "aaa", "uploaded": True, "uploaded_as": "D140.webp"}
        }
    }
    items = _run(monkeypatch, cfg, src_items=_src(sha1="aaa"), blob=WEBP, state=state)
    assert items == []
