"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .fetch import run_fetch
from .pipeline import run_merge_state, run_process
from .upload import run_upload


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="wiki-translate",
        description="Wiki AI 翻译流水线",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="环境变量配置文件路径，默认 .env（不存在时只读真实环境变量）",
    )
    parser.add_argument(
        "--mode",
        choices=["fetch", "process", "upload", "merge-state", "all"],
        default="all",
        help=(
            "fetch=拉取源 wiki 全部内容并生成 manifest；"
            "process=读 manifest 做 LLM 翻译/代码复制（可分片并行）；"
            "upload=统一推送页面/文件/系统消息到目标 wiki；"
            "merge-state=合并各并行 job 的 state delta；"
            "all=本地顺序执行 fetch+process+upload"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 state 强制重新处理/推送",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行：不调用 LLM / 不修改 wiki / 不写文件 / 不更新 state",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别，默认 INFO",
    )
    args = parser.parse_args()

    _setup_logging(args.log_level)
    cfg = load_config(args.env)

    if args.mode == "merge-state":
        return run_merge_state(cfg, force=args.force, dry_run=args.dry_run)

    rc = 0
    if args.mode in ("fetch", "all"):
        rc = run_fetch(cfg, force=args.force, dry_run=args.dry_run)
        if rc != 0 and args.mode == "all":
            logging.warning("fetch 阶段存在失败，仍继续尝试 process")
    if args.mode in ("process", "all"):
        rc2 = run_process(cfg, force=args.force, dry_run=args.dry_run)
        rc = rc or rc2
    if args.mode in ("upload", "all"):
        rc3 = run_upload(cfg, force=args.force, dry_run=args.dry_run)
        rc = rc or rc3
    return rc


if __name__ == "__main__":
    sys.exit(main())
