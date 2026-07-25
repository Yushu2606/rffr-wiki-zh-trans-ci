"""manifest 清单：分类 / 分片 / 加载保存 测试。"""
from __future__ import annotations

from pathlib import Path

from wiki_translate.manifest import (
    build_rename_map,
    classify_title,
    empty_manifest,
    items_of,
    load_manifest,
    save_manifest,
    shard_items,
)


def test_classify_module_is_copy():
    assert classify_title("Module:Foo") == "copy"
    assert classify_title("module:bar") == "copy"


def test_classify_code_suffix_is_copy():
    assert classify_title("MediaWiki:Common.css") == "copy"
    assert classify_title("MediaWiki:Common.js") == "copy"
    assert classify_title("Foo.json") == "copy"


def test_classify_template_is_translate():
    assert classify_title("Template:Infobox") == "translate"


def test_classify_plain_is_translate():
    assert classify_title("A-120") == "translate"
    assert classify_title("Category:实体") == "translate"


def test_items_of_filters_kind():
    manifest = {
        "schema": 1,
        "items": [
            {"kind": "page", "title": "A"},
            {"kind": "file", "title": "File:B"},
            {"kind": "page", "title": "C"},
        ],
    }
    pages = items_of(manifest, "page")
    assert [p["title"] for p in pages] == ["A", "C"]
    assert len(items_of(manifest)) == 3


def test_shard_items_roundrobin():
    items = [{"i": i} for i in range(10)]
    s0 = shard_items(items, 0, 3)
    s1 = shard_items(items, 1, 3)
    s2 = shard_items(items, 2, 3)
    assert [x["i"] for x in s0] == [0, 3, 6, 9]
    assert [x["i"] for x in s1] == [1, 4, 7]
    assert [x["i"] for x in s2] == [2, 5, 8]


def test_shard_items_single_returns_all():
    items = [{"i": 1}, {"i": 2}]
    assert shard_items(items, 0, 1) == items


def test_save_then_load_roundtrip(tmp_path: Path):
    p = tmp_path / "manifest.json"
    manifest = {
        "schema": 1,
        "generated_at": 100,
        "items": [{"kind": "page", "title": "A", "action": "translate"}],
    }
    save_manifest(p, manifest)
    loaded = load_manifest(p)
    assert loaded == manifest


def test_load_missing_returns_empty(tmp_path: Path):
    assert load_manifest(tmp_path / "nope.json") == empty_manifest()


def test_load_invalid_returns_empty(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    assert load_manifest(p) == empty_manifest()


def test_save_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "manifest.json"
    save_manifest(target, empty_manifest())
    assert target.exists()


def test_build_rename_map_only_includes_renamed_files():
    manifest = {
        "items": [
            {"kind": "file", "name": "Foo.jpg", "upload_name": "Foo.png", "renamed": True},
            {"kind": "file", "name": "Bar.gif", "upload_name": "Bar.gif", "renamed": False},
            {"kind": "page", "title": "A"},
        ]
    }
    assert build_rename_map(manifest) == {"Foo.jpg": "Foo.png"}


def test_build_rename_map_empty_when_no_files():
    assert build_rename_map(empty_manifest()) == {}
