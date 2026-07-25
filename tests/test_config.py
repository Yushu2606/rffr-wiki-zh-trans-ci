"""config 加载测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from wiki_translate.config import load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    # 把所有 prefix 相关的环境变量清掉，避免外部污染
    prefixes = (
        "WIKI_",
        "TRANSLATOR_",
        "OUTPUT_",
        "PUBLISH_",
        "STRATEGY_",
        "FANDOM_BOT_",
    )
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


def test_load_defaults_without_env_file(tmp_path: Path):
    cfg = load_config(tmp_path / "no.env")
    assert cfg.wiki.api_url == ""
    assert cfg.wiki.target_lang == "zh"
    assert cfg.translator.model == "gpt-4o-mini"
    assert cfg.publish.enabled is False
    assert cfg.strategy.use_source_diff is True


def test_load_from_env_file(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "WIKI_API_URL=https://x.fandom.com/api.php\n"
        "WIKI_TARGET_LANG=ja\n"
        "WIKI_NAMESPACES=0,10,14\n"
        'PUBLISH_TITLE_MAP={"Home":"首页"}\n'
        "PUBLISH_ENABLED=true\n"
        "STRATEGY_USE_SOURCE_DIFF=false\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.wiki.api_url == "https://x.fandom.com/api.php"
    assert cfg.wiki.target_lang == "ja"
    assert cfg.wiki.namespaces == [0, 10, 14]
    assert cfg.publish.title_map == {"Home": "首页"}
    assert cfg.publish.enabled is True
    assert cfg.strategy.use_source_diff is False


def test_real_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / ".env"
    p.write_text("WIKI_TARGET_LANG=ja\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_TARGET_LANG", "fr")
    cfg = load_config(p)
    assert cfg.wiki.target_lang == "fr"
