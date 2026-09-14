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
    # 抓取失败（如 arXiv 429）≠ 真的没论文：不推送、不记日期，等巡检重试
    if report.fetched and report.fetched.errors:
        logger.error("arXiv 抓取失败（%s），本次不推送，等待 30 分钟巡检重试", report.fetched.errors)
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
        # Mac 睡眠会让 cron 错过触发时间：宽限 6 小时，唤醒后补跑
        misfire_grace_time=6 * 3600,
        coalesce=True,
        replace_existing=True,
    )
    # 双保险：每 30 分钟检查一次「今日是否已推」，未推且已过推送时间则补跑。
    # 防御任何调度器在睡眠/唤醒下的意外不触发；last_digest_date 判重保证不重复推送。
    scheduler.add_job(
        catchup_check,
        CronTrigger(minute="*/30"),
        args=[bot, settings],
        id="daily_push_catchup",
        misfire_grace_time=1800,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("定时任务已注册：每日 %02d:%02d 推送（+30 分钟补跑巡检）", hour, minute)

    # 启动补偿：进程在推送时间之后启动且今日未推 → 立即补跑一次
    catchup_check(bot, settings, async_run=True)
    return scheduler


def catchup_check(bot, settings: Settings, *, async_run: bool = False) -> None:
    now = dt.datetime.now()
    today = dt.date.today().isoformat()
    if (now.hour, now.minute) < settings.push_hour_minute:
        return
    if get_state(STATE_LAST_DIGEST_DATE) == today:
        return
    logger.info("检测到今日汇总未推送（崩溃恢复/睡眠错过/首次启动），补跑")
    if async_run:
        threading.Thread(
            target=daily_push_job, args=[bot, settings], name="daily-push-catchup", daemon=True
        ).start()
    else:
        daily_push_job(bot, settings)
