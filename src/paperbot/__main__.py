"""FR-8 CLI 入口：python -m paperbot {fetch|push|next|serve|stats}"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from .cardfmt import stats_markdown
from .config import get_settings
from .db import init_db
from .models import STATE_LAST_DIGEST_DATE

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        settings.log_dir / "paperbot.log", maxBytes=1 << 20, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("lark_oapi").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def cmd_fetch() -> int:
    """手动执行一次抓取+总结（不入定时、不推送）。"""
    from . import arxiv_client, pipeline

    settings = get_settings()
    result = arxiv_client.run_fetch(settings)
    print(
        f"抓取完成：窗口 {result.window_days} 天，获取 {result.fetched} 条，"
        f"新命中 {result.new_hit}，过滤 {result.new_filtered}，去重跳过 {result.skipped_existing}"
    )
    if result.errors:
        print(f"⚠️ 部分分页失败：{result.errors}")
    ok, failed = pipeline.summarize_pending()
    print(f"速读卡片生成：成功 {ok} 篇，失败 {failed} 篇")
    return 0


def cmd_push() -> int:
    """手动推送今日汇总（按当前 DB 内容组装，不重新抓取）。"""
    import datetime as dt

    from . import pipeline
    from .cardfmt import summary_markdown
    from .feishu_bot import FeishuBot
    from .pipeline import DailyReport
    from .queue_service import set_state

    bot = FeishuBot(get_settings())
    papers = pipeline.today_hit_papers()
    failed = pipeline.today_failed_count()
    text = summary_markdown(dt.date.today().isoformat(), papers, failed)
    print(text)
    report = DailyReport(date=dt.date.today().isoformat(), today_papers=papers, summary_text=text)
    if bot.send_summary(report):
        set_state(STATE_LAST_DIGEST_DATE, dt.date.today().isoformat())
        print("✅ 已推送")
        return 0
    print("❌ 推送失败，详见日志")
    return 1


def cmd_next() -> int:
    """在终端打印下一条速读卡片（调试 DeepSeek 输出用，不消费队列）。

    SPEC-GAP: spec 未说明 CLI next 是否推进状态机；调试定位 → 只读 peek。
    """
    from .queue_service import peek_next

    paper = peek_next()
    if paper is None:
        print("📭 队列中没有待读论文（可能尚未 fetch 或全部已读）")
        return 0
    print(f"--- 下一篇：{paper.arxiv_id} ｜ {paper.published} ｜ 关键词：{paper.matched_keyword} ---")
    print(paper.card_text)
    return 0


def cmd_stats() -> int:
    from .queue_service import get_stats

    print(stats_markdown(get_stats()))
    return 0


def cmd_serve() -> int:
    """启动常驻服务：定时任务 + 飞书长连接（Ctrl+C 退出）。"""
    from .feishu_bot import run_forever
    from .scheduler import start_scheduler

    settings = get_settings()
    try:
        run_forever(settings, before_serve=lambda bot: start_scheduler(bot, settings))
    except KeyboardInterrupt:
        logger.info("收到退出信号，bye")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="paperbot", description="飞书 arXiv 论文速读机器人")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("fetch", "手动执行一次抓取+总结（不入定时）"),
        ("push", "手动推送今日汇总"),
        ("next", "在终端打印下一条速读卡片（调试用，不消费队列）"),
        ("serve", "启动常驻服务（定时任务 + 飞书长连接）"),
        ("stats", "打印统计"),
    ]:
        sub.add_parser(name, help=help_text)
    args = parser.parse_args()

    setup_logging()
    init_db()

    handlers = {
        "fetch": cmd_fetch,
        "push": cmd_push,
        "next": cmd_next,
        "serve": cmd_serve,
        "stats": cmd_stats,
    }
    try:
        return handlers[args.command]()
    except Exception as exc:  # noqa: BLE001 — CLI 统一出口，友好报错
        logger.error("命令 %s 失败：%s", args.command, exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
