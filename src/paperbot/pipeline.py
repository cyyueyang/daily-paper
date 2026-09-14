"""编排层：每日流程 = FR-1 抓取 → FR-2 逐篇生成速读卡片 → 汇总数据。

被 CLI（fetch/push）与 scheduler（每日定时）共用；推送本身在 feishu_bot。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from . import arxiv_client, llm
from .cardfmt import summary_markdown
from .config import Settings, get_settings
from .db import SessionLocal
from .models import STATUS_SUMMARIZE_FAILED, Paper
from .queue_service import (
    mark_summarize_failed,
    papers_awaiting_relevance,
    papers_awaiting_summary,
    record_card,
    record_relevance,
)

logger = logging.getLogger(__name__)


@dataclass
class DailyReport:
    date: str = ""
    fetched: arxiv_client.FetchResult | None = None
    summarized_ok: int = 0
    summarized_failed: int = 0
    today_papers: list[Paper] = field(default_factory=list)  # 今日入队且已出卡片
    summary_text: str = ""
    pushed: bool = False


def refine_relevance() -> tuple[int, int]:
    """二级语义过滤：关键词命中的论文再过一道 DeepSeek 相关性判定（FR-1 增强）。

    剔除「把 LLM 当工具解决行业问题」的应用型论文，只留核心方向技术研究。
    返回 (保留数, 淘汰数)。判定失败 fail-open：不过滤、不标记，明天重试。
    """
    settings = get_settings()
    if not settings.deepseek_api_key:
        return 0, 0
    papers = papers_awaiting_relevance()
    kept = dropped = 0
    for i, paper in enumerate(papers):
        try:
            direction, tokens = llm.judge_relevance(paper.title, paper.abstract)
            record_relevance(paper.id, direction, tokens)
            if direction:
                kept += 1
            else:
                dropped += 1
                logger.info("语义淘汰 %s：%s", paper.arxiv_id, paper.title[:60])
        except llm.LLMError as exc:
            logger.warning("相关性判定失败 %s：%s（fail-open 保留，明天重试）", paper.arxiv_id, exc)
        if i + 1 < len(papers):
            time.sleep(llm.CALL_INTERVAL_S)
    if papers:
        logger.info("语义过滤完成：判定 %d，保留 %d，淘汰 %d", len(papers), kept, dropped)
    return kept, dropped


def summarize_pending() -> tuple[int, int]:
    """为所有待生成论文串行生成速读卡片（每篇间隔 >= 0.5s，不设篇数上限）。

    返回 (成功数, 失败数)。API key 未配置时整批跳过（保持 pending，
    之后补配 key 重跑 fetch 即可恢复，不误标 summarize_failed）。
    """
    settings = get_settings()
    if not settings.deepseek_api_key:
        logger.warning("DEEPSEEK_API_KEY 未配置，跳过卡片生成（论文保持 pending）")
        return 0, 0

    papers = papers_awaiting_summary()
    ok = failed = 0
    for i, paper in enumerate(papers):
        try:
            result = llm.generate_card(paper.title, paper.abstract, direction=paper.direction)
            record_card(paper.id, result.text, result.tokens)
            ok += 1
            logger.info("卡片生成 %d/%d %s（%d tokens）", i + 1, len(papers), paper.arxiv_id, result.tokens)
        except llm.LLMError as exc:
            mark_summarize_failed(paper.id)
            failed += 1
            logger.error("卡片生成失败 %s：%s（标记 summarize_failed，继续下一篇）", paper.arxiv_id, exc)
        if i + 1 < len(papers):
            time.sleep(llm.CALL_INTERVAL_S)
    return ok, failed


def today_hit_papers() -> list[Paper]:
    """今日入队（created today）、命中关键词、已出卡片的论文（汇总消息用）。"""
    today_start = dt.datetime.combine(dt.date.today(), dt.time.min)
    with SessionLocal() as s:
        papers = list(
            s.scalars(
                select(Paper)
                .where(
                    Paper.created_at >= today_start,
                    Paper.filtered_out == 0,
                    Paper.card_text.isnot(None),
                    Paper.status != STATUS_SUMMARIZE_FAILED,
                )
                .order_by(Paper.published, Paper.id)
            ).all()
        )
        s.expunge_all()
        return papers


def today_failed_count() -> int:
    today_start = dt.datetime.combine(dt.date.today(), dt.time.min)
    with SessionLocal() as s:
        return len(
            s.scalars(
                select(Paper.id).where(
                    Paper.created_at >= today_start,
                    Paper.filtered_out == 0,
                    Paper.status == STATUS_SUMMARIZE_FAILED,
                )
            ).all()
        )


def run_daily(settings: Settings) -> DailyReport:
    """每日任务主体：抓取 → 总结 → 组装汇总文本（不推送，推送由调用方决定）。"""
    report = DailyReport(date=dt.date.today().isoformat())
    report.fetched = arxiv_client.run_fetch(settings)
    refine_relevance()  # 关键词召回 → 语义判定，通过的才生成卡片
    report.summarized_ok, report.summarized_failed = summarize_pending()
    report.today_papers = today_hit_papers()
    report.summary_text = summary_markdown(
        report.date, report.today_papers, today_failed_count()
    )
    return report
