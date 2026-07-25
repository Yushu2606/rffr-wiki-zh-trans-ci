"""state.json schema v2 与 v1→v2 迁移测试。"""
from __future__ import annotations

import json
from pathlib import Path

from wiki_translate.state import (
    all_titles,
    get_page,
    load_state,
    save_state,
    sha256,
)


def test_load_empty_when_missing(tmp_path: Path):
    state = load_state(tmp_path / "nope.json")
    assert state == {"schema": 2, "pages": {}}


def test_load_invalid_json_falls_back(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    assert load_state(p) == {"schema": 2, "pages": {}}


def test_save_then_load_roundtrip(tmp_path: Path):
    p = tmp_path / "s.json"
    state = {"schema": 2, "pages": {"X": {"source": {"hash": "h"}}}}
    save_state(p, state)
    assert json.loads(p.read_text(encoding="utf-8")) == state
    assert load_state(p) == state


def test_migrate_v1_to_v2(tmp_path: Path):
    p = tmp_path / "old.json"
    legacy = {
        "Foo": {
            "revid": 11,
            "translated": True,
            "file": "output/zh/Foo.wiki",
            "updated_at": 100,
            "published_revid": 22,
            "published_target": "Foo",
            "published_newrevid": 22,
            "published_at": 200,
        },
        "Bar": {"revid": 5, "translated": False},
    }
    p.write_text(json.dumps(legacy), encoding="utf-8")
    state = load_state(p)
    assert state["schema"] == 2
    foo = state["pages"]["Foo"]
    assert foo["source"]["revid"] == 11
    assert foo["translation"]["file"] == "output/zh/Foo.wiki"
    assert foo["last_published"]["target_revid"] == 22
    assert foo["target"]["title"] == "Foo"
    bar = state["pages"]["Bar"]
    assert "translation" not in bar
    assert bar["source"]["revid"] == 5


def test_get_page_creates_default():
    state = {"schema": 2, "pages": {}}
    page = get_page(state, "X")
    assert page == {}
    page["source"] = {"hash": "h"}
    assert state["pages"]["X"]["source"]["hash"] == "h"


def test_all_titles_returns_keys():
    state = {"schema": 2, "pages": {"a": {}, "b": {}}}
    assert sorted(all_titles(state)) == ["a", "b"]


def test_sha256_stable():
    assert sha256("abc") == sha256("abc")
    assert sha256("a") != sha256("b")
