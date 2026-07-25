"""state 合并 / delta 抽取 / 分片相关测试。"""
from __future__ import annotations

from pathlib import Path

from wiki_translate.pipeline import _shard_titles
from wiki_translate.state import (
    extract_delta,
    load_states,
    merge_states,
    save_progress,
    save_state,
)


def test_extract_delta_pages_only():
    state = {
        "schema": 2,
        "pages": {"A": {"x": 1}, "B": {"x": 2}, "C": {"x": 3}},
        "files": {"f": {"y": 1}},
    }
    delta = extract_delta(state, pages={"A", "C"})
    assert delta["pages"] == {"A": {"x": 1}, "C": {"x": 3}}
    assert "files" not in delta


def test_extract_delta_missing_keys_ignored():
    state = {"schema": 2, "pages": {"A": {"x": 1}}}
    delta = extract_delta(state, pages={"A", "ZZZ"})
    assert delta["pages"] == {"A": {"x": 1}}


def test_extract_delta_files_and_system():
    state = {
        "schema": 2,
        "pages": {},
        "files": {"f1": {"sha1": "a"}, "f2": {"sha1": "b"}},
        "system_messages": {"m1": {"h": "x"}},
    }
    delta = extract_delta(state, files={"f1"}, system_messages={"m1"})
    assert delta["files"] == {"f1": {"sha1": "a"}}
    assert delta["system_messages"] == {"m1": {"h": "x"}}
    assert delta["pages"] == {}


def test_merge_states_disjoint_pages():
    base = {"schema": 2, "pages": {"A": {"v": 0}}}
    d1 = {"schema": 2, "pages": {"B": {"v": 1}}}
    d2 = {"schema": 2, "pages": {"C": {"v": 2}}}
    merged = merge_states(base, d1, d2)
    assert merged["pages"] == {"A": {"v": 0}, "B": {"v": 1}, "C": {"v": 2}}


def test_merge_states_delta_overrides_base():
    base = {"schema": 2, "pages": {"A": {"v": 0}}}
    d1 = {"schema": 2, "pages": {"A": {"v": 99}}}
    merged = merge_states(base, d1)
    assert merged["pages"]["A"] == {"v": 99}


def test_merge_states_later_delta_wins():
    base = {"schema": 2, "pages": {}}
    d1 = {"schema": 2, "pages": {"A": {"v": 1}}}
    d2 = {"schema": 2, "pages": {"A": {"v": 2}}}
    merged = merge_states(base, d1, d2)
    assert merged["pages"]["A"] == {"v": 2}


def test_merge_states_separate_sections():
    base = {"schema": 2, "pages": {"P": {"v": 0}}}
    d_files = {"schema": 2, "files": {"f": {"sha1": "a"}}}
    d_sys = {"schema": 2, "system_messages": {"m": {"h": "x"}}}
    merged = merge_states(base, d_files, d_sys)
    assert merged["pages"] == {"P": {"v": 0}}
    assert merged["files"] == {"f": {"sha1": "a"}}
    assert merged["system_messages"] == {"m": {"h": "x"}}


def test_save_progress_delta_mode(tmp_path: Path):
    state = {"schema": 2, "pages": {"A": {"v": 1}, "B": {"v": 2}}}
    full = tmp_path / "state.json"
    delta = tmp_path / "delta.json"
    save_progress(state, full, delta_path=delta, pages={"A"})
    assert not full.exists()
    loaded = load_states([delta])
    assert loaded[0]["pages"] == {"A": {"v": 1}}


def test_save_progress_full_mode(tmp_path: Path):
    state = {"schema": 2, "pages": {"A": {"v": 1}}}
    full = tmp_path / "state.json"
    save_progress(state, full)
    assert full.exists()


def test_save_state_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "deep" / "state.json"
    save_state(target, {"schema": 2, "pages": {}})
    assert target.exists()


def test_load_states_skips_missing(tmp_path: Path):
    p1 = tmp_path / "a.json"
    save_state(p1, {"schema": 2, "pages": {"A": {}}})
    out = load_states([p1, tmp_path / "missing.json"])
    assert len(out) == 1


def test_shard_titles_roundrobin():
    titles = [f"T{i}" for i in range(10)]
    s0 = _shard_titles(titles, 0, 3)
    s1 = _shard_titles(titles, 1, 3)
    s2 = _shard_titles(titles, 2, 3)
    assert s0 == ["T0", "T3", "T6", "T9"]
    assert s1 == ["T1", "T4", "T7"]
    assert s2 == ["T2", "T5", "T8"]
    assert sorted(s0 + s1 + s2) == sorted(titles)


def test_shard_titles_single_shard_returns_all():
    titles = ["A", "B", "C"]
    assert _shard_titles(titles, 0, 1) == titles


def test_merge_then_extract_roundtrip(tmp_path: Path):
    base = {"schema": 2, "pages": {"A": {"v": 0}}}
    full_state = {
        "schema": 2,
        "pages": {"A": {"v": 0}, "B": {"v": 1}, "C": {"v": 2}},
    }
    d0 = extract_delta(full_state, pages={"B"})
    d1 = extract_delta(full_state, pages={"C"})
    f0 = tmp_path / "d0.json"
    f1 = tmp_path / "d1.json"
    save_state(f0, d0)
    save_state(f1, d1)
    merged = merge_states(base, *load_states([f0, f1]))
    assert merged["pages"] == full_state["pages"]
