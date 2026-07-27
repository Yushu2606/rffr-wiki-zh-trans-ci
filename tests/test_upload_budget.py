"""upload 软时间预算：用尽时保存进度并正常退出，而不是被 CI 硬杀丢掉进度。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki_translate import upload as up
from wiki_translate.config import (
    AppConfig,
    OutputConfig,
    PublishConfig,
    StrategyConfig,
    TranslatorConfig,
    WikiConfig,
)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    manifest = tmp_path / "manifest.json"
    items = [{"kind": "page", "title": f"P{i}", "action": "translate"} for i in range(5)]
    manifest.write_text(
        json.dumps({"schema": 1, "generated_at": 0, "items": items}), encoding="utf-8"
    )
    out = tmp_path / "output" / "zh"
    out.mkdir(parents=True)
    for i in range(5):
        (out / f"P{i}.wiki").write_text(f"content {i}", encoding="utf-8")
    return AppConfig(
        wiki=WikiConfig(api_url="http://example.invalid"),
        translator=TranslatorConfig(api_key="unused"),
        output=OutputConfig(dir=str(tmp_path / "output"), state_file=str(tmp_path / "state.json")),
        publish=PublishConfig(
            enabled=True, api_url="http://example.invalid/api.php", sleep_between=0
        ),
        strategy=StrategyConfig(
            manifest_file=str(manifest), fetch_target_before_publish=False
        ),
    )


def _stub_publisher(monkeypatch, pushed: list[str]) -> None:
    class FakePublisher:
        def __init__(self, *a, **k):
            pass

        def login(self):
            pass

        def logout(self):
            pass

        def close(self):
            pass

        def map_title(self, t):
            return t

        def fetch_page(self, t):
            return None

        def edit(self, title, content):
            pushed.append(title)
            return {"newrevid": len(pushed)}

    monkeypatch.setattr(up, "FandomPublisher", FakePublisher)


def test_no_budget_processes_everything(monkeypatch, cfg: AppConfig):
    pushed: list[str] = []
    _stub_publisher(monkeypatch, pushed)
    assert up.run_upload(cfg) == 0
    assert len(pushed) == 5


def test_budget_stops_early_and_keeps_progress(monkeypatch, cfg: AppConfig):
    """预算在第 3 项后耗尽：前 2 项必须已推送且写进 state。"""
    cfg.publish.time_budget_seconds = 10
    pushed: list[str] = []
    _stub_publisher(monkeypatch, pushed)

    ticks = iter([0, 1, 2, 999, 999, 999, 999])
    monkeypatch.setattr(up.time, "monotonic", lambda: next(ticks))

    rc = up.run_upload(cfg)
    assert rc == 0, "预算用尽是计划内收工，不能报失败——否则 CI 不会提交进度"
    assert len(pushed) == 2, f"应只推送 2 项，实际 {pushed}"

    state = json.loads(Path(cfg.output.state_file).read_text(encoding="utf-8"))
    done = [t for t, p in state.get("pages", {}).items() if p.get("last_published")]
    assert sorted(done) == ["P0", "P1"], "已完成的部分必须落盘，下次运行才不会重做"


def test_budget_exhausted_before_first_item_pushes_nothing(monkeypatch, cfg: AppConfig):
    cfg.publish.time_budget_seconds = 10
    pushed: list[str] = []
    _stub_publisher(monkeypatch, pushed)
    # 第一次调用用于计算 deadline(=0+10)，之后时钟已越过它
    ticks = iter([0] + [999] * 10)
    monkeypatch.setattr(up.time, "monotonic", lambda: next(ticks))
    assert up.run_upload(cfg) == 0
    assert pushed == []


def test_one_item_exception_does_not_kill_the_run(monkeypatch, cfg: AppConfig):
    """单项抛异常必须只算该项失败，其余继续。

    回归用例：fetch_page 重试耗尽会抛异常，早期版本没有逐项兜底，整个 upload 进程
    直接崩溃；CI 里 Commit 步骤随之被跳过，已推送内容的 state 提交不回去，下次重推。
    """
    pushed: list[str] = []
    _stub_publisher(monkeypatch, pushed)
    real = up._upload_page

    def flaky(cfg_, publisher, state, item, **kw):
        if item["title"] == "P2":
            raise RuntimeError("模拟 fetch_page 重试耗尽")
        return real(cfg_, publisher, state, item, **kw)

    monkeypatch.setattr(up, "_upload_page", flaky)
    rc = up.run_upload(cfg)

    assert rc == 1, "有失败项应返回 1"
    assert pushed == ["P0", "P1", "P3", "P4"], "P2 之后的条目必须继续处理"
    state = json.loads(Path(cfg.output.state_file).read_text(encoding="utf-8"))
    done = sorted(t for t, p in state.get("pages", {}).items() if p.get("last_published"))
    assert done == ["P0", "P1", "P3", "P4"], "成功项的进度必须落盘"


def test_remaining_reported_not_silently_dropped(monkeypatch, cfg: AppConfig, caplog):
    """只传了一半却不报出来，会被误读成全部完成。"""
    cfg.publish.time_budget_seconds = 10
    _stub_publisher(monkeypatch, [])
    ticks = iter([0, 1, 999, 999, 999, 999])
    monkeypatch.setattr(up.time, "monotonic", lambda: next(ticks))
    with caplog.at_level("WARNING"):
        up.run_upload(cfg)
    assert any("时间预算用尽" in r.message for r in caplog.records)
    assert any("pending:" in r.getMessage() for r in caplog.records)
