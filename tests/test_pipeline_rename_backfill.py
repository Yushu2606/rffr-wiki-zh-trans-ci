"""跨批次改名回填：build_rename_map 必须读 state 的完整历史台账，不能只看当次
manifest。这是"受损文件链接从 15 涨到 110"那次回归的根因，端到端验证修复。
"""
from __future__ import annotations

import json
from pathlib import Path

from wiki_translate.config import (
    AppConfig,
    OutputConfig,
    PublishConfig,
    StrategyConfig,
    TranslatorConfig,
    WikiConfig,
)
from wiki_translate.manifest import save_manifest
from wiki_translate.pipeline import run_process


def _cfg(tmp_path: Path, manifest_path: Path) -> AppConfig:
    # 全部路径必须显式钉在 tmp_path 下——StrategyConfig.cache_source_dir 默认是相对路径
    # "cache/source"，pytest 从仓库根目录跑，漏配一个就会真的写坏仓库里的缓存文件
    # （这个坑亲身踩过一次：A-120/D-140 的 cache/source 被测试内容覆盖，靠 git checkout 救回）。
    return AppConfig(
        wiki=WikiConfig(api_url="http://example.invalid"),
        translator=TranslatorConfig(api_key="unused"),
        output=OutputConfig(dir=str(tmp_path / "output"), state_file=str(tmp_path / "state.json")),
        publish=PublishConfig(),
        strategy=StrategyConfig(
            manifest_file=str(manifest_path),
            incoming_dir=str(tmp_path / "incoming"),
            cache_source_dir=str(tmp_path / "cache" / "source"),
        ),
    )


def test_backfill_uses_rename_from_unrelated_earlier_batch(tmp_path: Path):
    """回归复现：文件改名发生在"更早的批次"，跟本次要处理的页面毫无关系
    （本次 manifest 里压根没有任何 file 条目），页面正文里的旧文件名引用
    仍然必须被回填——旧实现（rename_map 只读当次 manifest）在这里会失败。
    """
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    src = incoming / "D-140.wiki"
    src.write_text(
        "{{Infobox|image=D-140AnimRecreation.gif}}\n[[File:D-140AnimRecreation.gif]]",
        encoding="utf-8",
    )

    manifest_path = tmp_path / "manifest.json"
    save_manifest(
        manifest_path,
        {
            "schema": 1,
            "generated_at": 0,
            "items": [
                {
                    "kind": "page",
                    "title": "D-140",
                    "action": "copy",  # 走原样复制分支，不需要真的调 LLM
                    "revid": 1,
                    "source_hash": "",
                    "incoming_cache": str(src.as_posix()),
                }
                # 注意：这里没有任何 kind=file 条目——本次运行根本没碰过这个文件，
                # 改名是很久以前另一批次做的，只活在 state.json 里。
            ],
        },
    )

    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "pages": {},
                "files": {
                    "File:D-140AnimRecreation.gif": {
                        "sha1": "aaa",
                        "uploaded": True,
                        "uploaded_as": "D-140AnimRecreation.webp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = _cfg(tmp_path, manifest_path)
    rc = run_process(cfg, dry_run=False)
    assert rc == 0

    out_path = tmp_path / "output" / "zh" / "D-140.wiki"
    out_text = out_path.read_text(encoding="utf-8")
    assert "D-140AnimRecreation.webp" in out_text
    assert "D-140AnimRecreation.gif" not in out_text


def test_no_backfill_when_state_has_no_rename_history(tmp_path: Path):
    """对照组：state 里没有任何改名记录时，正文原样不动（不产生虚假替换）。"""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    src = incoming / "A-120.wiki"
    src.write_text("[[File:Untouched.png]]", encoding="utf-8")

    manifest_path = tmp_path / "manifest.json"
    save_manifest(
        manifest_path,
        {
            "schema": 1,
            "generated_at": 0,
            "items": [
                {
                    "kind": "page",
                    "title": "A-120",
                    "action": "copy",
                    "revid": 1,
                    "source_hash": "",
                    "incoming_cache": str(src.as_posix()),
                }
            ],
        },
    )
    (tmp_path / "state.json").write_text(
        json.dumps({"schema": 2, "pages": {}, "files": {}}), encoding="utf-8"
    )

    cfg = _cfg(tmp_path, manifest_path)
    assert run_process(cfg, dry_run=False) == 0

    out_text = (tmp_path / "output" / "zh" / "A-120.wiki").read_text(encoding="utf-8")
    assert out_text == "[[File:Untouched.png]]"
