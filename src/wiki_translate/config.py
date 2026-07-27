"""配置加载：通过 python-dotenv 读取 .env，再用 pydantic 风格的 dataclass 暴露强类型字段。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _str(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return v if v is not None else default


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _int(key: str, default: int = 0) -> int:
    v = os.environ.get(key, "")
    try:
        return int(v) if v != "" else default
    except ValueError:
        return default


def _float(key: str, default: float = 0.0) -> float:
    v = os.environ.get(key, "")
    try:
        return float(v) if v != "" else default
    except ValueError:
        return default


def _list(key: str, default: list[str] | None = None, sep: str = ",") -> list[str]:
    v = os.environ.get(key, "")
    if v == "":
        return list(default or [])
    return [x.strip() for x in v.split(sep) if x.strip()]


def _int_list(key: str, default: list[int] | None = None, sep: str = ",") -> list[int]:
    out: list[int] = []
    for x in _list(key, [], sep):
        try:
            out.append(int(x))
        except ValueError:
            pass
    return out if out else list(default or [])


def _json(key: str, default: Any) -> Any:
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return default


def _read_text(path: str, fallback: str = "") -> str:
    if not path:
        return fallback
    p = Path(path)
    if not p.exists():
        return fallback
    return p.read_text(encoding="utf-8")


@dataclass(slots=True)
class WikiConfig:
    api_url: str
    source_lang: str = "en"
    target_lang: str = "zh"
    namespaces: list[int] = field(default_factory=lambda: [0])
    category: str = ""
    page_list: list[str] = field(default_factory=list)
    all_pages: bool = False
    filter_redirects: str = "nonredirects"
    max_pages: int = 0
    sleep_between: float = 0.0
    user_agent: str = "WikiTranslateBot/1.0 (github actions)"


@dataclass(slots=True)
class TranslatorConfig:
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 4096
    chunk_chars: int = 3500
    retry: int = 3
    retry_delay: int = 5
    system_prompt: str = ""
    api_key: str = ""
    glossary_file: str = ""
    glossary_max_entries: int = 50
    concurrency: int = 1


@dataclass(slots=True)
class OutputConfig:
    dir: str = "output"
    state_file: str = "state.json"
    commit_message: str = "chore(i18n): auto-translate wiki pages"


@dataclass(slots=True)
class PublishConfig:
    enabled: bool = False
    api_url: str = ""
    summary: str = "Auto translation by github actions"
    bot_flag: bool = True
    minor: bool = False
    sleep_between: float = 1.0
    retry: int = 3
    retry_delay: int = 5
    title_prefix: str = ""
    title_suffix: str = ""
    title_map: dict[str, str] = field(default_factory=dict)
    username: str = ""
    password: str = ""
    on_target_conflict: str = "skip"
    chunk_threshold_mb: float = 5.0
    chunk_size_mb: float = 4.0
    on_mime_mismatch: str = "rename"
    time_budget_seconds: int = 0


@dataclass(slots=True)
class StrategyConfig:
    use_source_diff: bool = True
    use_repo_reference: bool = True
    fetch_target_before_publish: bool = True
    cache_source_dir: str = "cache/source"
    diff_translation: bool = False
    incoming_dir: str = "cache/incoming"
    manifest_file: str = "manifest.json"


@dataclass(slots=True)
class AppConfig:
    wiki: WikiConfig
    translator: TranslatorConfig
    output: OutputConfig
    publish: PublishConfig
    strategy: StrategyConfig


def load_config(env_file: str | os.PathLike[str] = ".env") -> AppConfig:
    """读取 .env 并构造强类型配置对象。真实环境变量优先于 .env。"""
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path, override=False)

    system_prompt = _str("TRANSLATOR_SYSTEM_PROMPT", "")
    if not system_prompt:
        system_prompt = _read_text(
            _str("TRANSLATOR_SYSTEM_PROMPT_FILE", "prompts/system.txt"),
            fallback=(
                "You are a professional Wiki translator. "
                "Translate to the target language while preserving all MediaWiki markup."
            ),
        )

    return AppConfig(
        wiki=WikiConfig(
            api_url=_str("WIKI_API_URL"),
            source_lang=_str("WIKI_SOURCE_LANG", "en"),
            target_lang=_str("WIKI_TARGET_LANG", "zh"),
            namespaces=_int_list("WIKI_NAMESPACES", [0]),
            category=_str("WIKI_CATEGORY", ""),
            page_list=_list("WIKI_PAGE_LIST", []),
            all_pages=_bool("WIKI_ALL_PAGES", False),
            filter_redirects=_str("WIKI_FILTER_REDIRECTS", "nonredirects"),
            max_pages=_int("WIKI_MAX_PAGES", 0),
            sleep_between=_float("WIKI_SLEEP_BETWEEN", 0.0),
            user_agent=_str("WIKI_USER_AGENT", "WikiTranslateBot/1.0 (github actions)"),
        ),
        translator=TranslatorConfig(
            base_url=_str("OPENAI_BASE_URL", "")
            or _str("TRANSLATOR_BASE_URL", "https://api.openai.com/v1"),
            model=_str("OPENAI_MODEL", "") or _str("TRANSLATOR_MODEL", "gpt-4o-mini"),
            temperature=_float("TRANSLATOR_TEMPERATURE", 0.2),
            max_tokens=_int("TRANSLATOR_MAX_TOKENS", 4096),
            chunk_chars=_int("TRANSLATOR_CHUNK_CHARS", 3500),
            retry=_int("TRANSLATOR_RETRY", 3),
            retry_delay=_int("TRANSLATOR_RETRY_DELAY", 5),
            system_prompt=system_prompt,
            api_key=_str("OPENAI_API_KEY", ""),
            glossary_file=_str("TRANSLATOR_GLOSSARY_FILE", "prompts/glossary.csv"),
            glossary_max_entries=_int("TRANSLATOR_GLOSSARY_MAX_ENTRIES", 50),
            concurrency=_int("TRANSLATOR_CONCURRENCY", 1),
        ),
        output=OutputConfig(
            dir=_str("OUTPUT_DIR", "output"),
            state_file=_str("OUTPUT_STATE_FILE", "state.json"),
            commit_message=_str(
                "OUTPUT_COMMIT_MESSAGE",
                "chore(i18n): auto-translate wiki pages",
            ),
        ),
        publish=PublishConfig(
            enabled=_bool("PUBLISH_ENABLED", False),
            api_url=_str("PUBLISH_API_URL", ""),
            summary=_str("PUBLISH_SUMMARY", "Auto translation by github actions"),
            bot_flag=_bool("PUBLISH_BOT_FLAG", True),
            minor=_bool("PUBLISH_MINOR", False),
            sleep_between=_float("PUBLISH_SLEEP_BETWEEN", 1.0),
            retry=_int("PUBLISH_RETRY", 3),
            retry_delay=_int("PUBLISH_RETRY_DELAY", 5),
            title_prefix=_str("PUBLISH_TITLE_PREFIX", ""),
            title_suffix=_str("PUBLISH_TITLE_SUFFIX", ""),
            title_map=_json("PUBLISH_TITLE_MAP", {}) or {},
            username=_str("FANDOM_BOT_USER", ""),
            password=_str("FANDOM_BOT_PASSWORD", ""),
            on_target_conflict=_str("PUBLISH_ON_TARGET_CONFLICT", "skip"),
            chunk_threshold_mb=_float("PUBLISH_CHUNK_THRESHOLD_MB", 5.0),
            chunk_size_mb=_float("PUBLISH_CHUNK_SIZE_MB", 4.0),
            on_mime_mismatch=_str("PUBLISH_ON_MIME_MISMATCH", "rename"),
            time_budget_seconds=_int("PUBLISH_TIME_BUDGET_SECONDS", 0),
        ),
        strategy=StrategyConfig(
            use_source_diff=_bool("STRATEGY_USE_SOURCE_DIFF", True),
            use_repo_reference=_bool("STRATEGY_USE_REPO_REFERENCE", True),
            fetch_target_before_publish=_bool("STRATEGY_FETCH_TARGET_BEFORE_PUBLISH", True),
            cache_source_dir=_str("STRATEGY_CACHE_SOURCE_DIR", "cache/source"),
            diff_translation=_bool("STRATEGY_DIFF_TRANSLATION", False),
            incoming_dir=_str("STRATEGY_INCOMING_DIR", "cache/incoming"),
            manifest_file=_str("STRATEGY_MANIFEST_FILE", "manifest.json"),
        ),
    )
