"""FR-7 定时任务：APScheduler 内嵌主进程，与飞书长连接分线程运行。

- 每日 DAILY_PUSH_TIME（本地时间）执行：抓取 → 总结 → 推送汇总
- 崩溃恢复：状态全在 SQLite；用 last_digest_date 判重，
  重启后若已过推送时间且今日未推 → 立即补跑（不重复推送）
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline
from .config import Settings
from .models import STATE_LAST_DIGEST_DATE
from .queue_service import get_state, set_state

logger = logging.getLogger(__name__)

JOB_ID = "daily_push"


def daily_push_job(bot, settings: Settings) -> None:
    """每日任务主体；last_digest_date 判重保证当日汇总不重复推送。"""
    today = dt.date.today().isoformat()
    if get_state(STATE_LAST_DIGEST_DATE) == today:
        logger.info("今日（%s）汇总已推送过，跳过", today)
        return
    try:
        report = pipeline.run_daily(settings)
    except Exception:  # noqa: BLE001 — 任务失败不炸调度器，明日再试
        logger.exception("每日任务执行失败")
        return
    if bot.send_summary(report):
        set_state(STATE_LAST_DIGEST_DATE, today)
        logger.info(
            "今日汇总已推送：新增命中 %d 篇，总结失败 %d 篇",
            len(report.today_papers), report.summarized_failed,
        )
    else:
        # 推送失败不记录日期 → 重启/补跑时会重试推送
        logger.error("今日汇总推送失败（重试已耗尽），等待下次补推")


def start_scheduler(bot, settings: Settings) -> BackgroundScheduler:
    hour, minute = settings.push_hour_minute
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        daily_push_job,
        CronTrigger(hour=hour, minute=minute),
        args=[bot, settings],
        id=JOB_ID,
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("定时任务已注册：每日 %02d:%02d 推送", hour, minute)

    # 启动补偿：进程在推送时间之后启动且今日未推 → 立即补跑一次
    now = dt.datetime.now()
    today = dt.date.today().isoformat()
    if (now.hour, now.minute) >= (hour, minute) and get_state(STATE_LAST_DIGEST_DATE) != today:
        logger.info("检测到今日汇总未推送（崩溃恢复/首次启动），立即补跑")
        threading.Thread(
            target=daily_push_job, args=[bot, settings], name="daily-push-catchup", daemon=True
        ).start()
    return scheduler
