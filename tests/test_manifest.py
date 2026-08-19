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
    state = {
        "files": {
            "File:Foo.jpg": {"uploaded_as": "Foo.png"},
            "File:Bar.gif": {"uploaded_as": "Bar.gif"},  # 未改名，同名
        }
    }
    assert build_rename_map(state) == {"Foo.jpg": "Foo.png"}


def test_build_rename_map_empty_when_no_files():
    assert build_rename_map({}) == {}
    assert build_rename_map({"files": {}}) == {}


def test_build_rename_map_ignores_separator_only_difference():
    """空格/下划线在 MediaWiki 里等价，只差分隔符不该算改名（否则产生无意义 diff）。"""
    state = {"files": {"File:A B.webp": {"uploaded_as": "A_B.webp"}}}
    assert build_rename_map(state) == {}


def test_build_rename_map_reads_full_history_not_just_recent_entries():
    """必须读 state 里全部历史改名，不能只看"最近一批"。

    回归用例：这是"受损文件链接从 15 涨到 110"那次回归的根因——旧实现只从当次
    manifest（本批次）取数，改名发生在更早批次、或引用它的页面这次没被重新翻译，
    回填就永远轮不到。state['files'] 是跨运行持续累积的台账，不该有"批次"概念。
    """
    state = {
        "files": {
            f"File:Old{i}.gif": {"uploaded_as": f"Old{i}.webp"}
            for i in range(50)  # 模拟很久以前、分散在很多批次里发生的改名
        }
    }
    rename_map = build_rename_map(state)
    assert len(rename_map) == 50
    assert rename_map["Old0.gif"] == "Old0.webp"
    assert rename_map["Old49.gif"] == "Old49.webp"


def test_build_rename_map_strips_namespace_prefix_from_title():
    state = {"files": {"File:Foo.gif": {"uploaded_as": "Foo.webp"}}}
    assert build_rename_map(state) == {"Foo.gif": "Foo.webp"}


def test_build_rename_map_skips_entries_without_uploaded_as():
    """还没成功上传过的文件（比如失败留下的空壳记录）不该产生改名条目。"""
    state = {"files": {"File:Pending.gif": {"sha1": "aaa"}}}
    assert build_rename_map(state) == {}
