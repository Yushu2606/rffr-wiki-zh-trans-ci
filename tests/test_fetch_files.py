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

# 真实的 webp 魔数，会被 _sniff_mime 认出来
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40


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


def test_unchanged_and_uploaded_is_skipped_entirely(monkeypatch, cfg):
    state = {
        "files": {
            "File:D140.gif": {"sha1": "aaa", "uploaded": True, "uploaded_as": "D140.webp"}
        }
    }
    items = _run(monkeypatch, cfg, src_items=_src(sha1="aaa"), blob=WEBP, state=state)
    assert items == []
