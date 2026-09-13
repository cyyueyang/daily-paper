"""FR-1 arXiv 抓取 + 关键词过滤 + 去重。

- 官方 Atom API（export.arxiv.org），feedparser 解析，不用第三方爬虫库
- 按 submittedDate 降序分页（start + max_results），把日期窗口内的结果全部抓完，不设条数上限
- 请求间隔 >= 3 秒；超时/非 200/429 重试 2 次（间隔 5s）
- 去重：arxiv_id（不含版本号）为唯一键，跨分类只入库一次，v2+ 不重复推送
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass, field

import feedparser
import httpx
from sqlalchemy import select

from .config import Settings, get_settings
from .db import SessionLocal
from .models import Paper

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"
PAGE_SIZE = 100
REQUEST_INTERVAL_S = 3.0  # arXiv rate limit
RETRY_TIMES = 2
RETRY_INTERVAL_S = 5.0
FIRST_RUN_DAYS = 3  # 首次运行（库为空）补 3 天数据

_VERSION_RE = re.compile(r"v\d+$")


@dataclass
class ArxivEntry:
    arxiv_id: str  # 不含版本号
    title: str
    authors: list[str]
    abstract: str
    primary_category: str | None
    categories: list[str]
    published: dt.date
    pdf_url: str | None
    abs_url: str | None


@dataclass
class FetchResult:
    window_days: int = 0
    fetched: int = 0  # API 返回且落在窗口内的总条数
    new_hit: int = 0  # 新入库且命中关键词
    new_filtered: int = 0  # 新入库但未命中关键词
    skipped_existing: int = 0  # 已入库跳过（去重生效）
    errors: list[str] = field(default_factory=list)


def strip_version(arxiv_id: str) -> str:
    return _VERSION_RE.sub("", arxiv_id)


def match_keyword(title: str, abstract: str, keywords: list[str]) -> str | None:
    """大小写不敏感子串匹配，返回命中的第一个关键词。"""
    haystack = f"{title}\n{abstract}".lower()
    for kw in keywords:
        if kw.lower() in haystack:
            return kw
    return None


def _build_query_url(settings: Settings, start: int) -> str:
    cats = "+OR+".join(f"cat:{c}" for c in settings.categories)
    return (
        f"{ARXIV_API}?search_query={cats}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&start={start}&max_results={PAGE_SIZE}"
    )


def _fetch_page(client: httpx.Client, settings: Settings, start: int) -> bytes | None:
    """抓一页；重试 2 次仍失败返回 None（记日志，调用方跳过本次抓取）。"""
    url = _build_query_url(settings, start)
    for attempt in range(RETRY_TIMES + 1):
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.content
            logger.warning(
                "arxiv page start=%d 返回 %d（第 %d 次）", start, resp.status_code, attempt + 1
            )
        except httpx.HTTPError as exc:
            logger.warning("arxiv page start=%d 请求异常 %s（第 %d 次）", start, exc, attempt + 1)
        if attempt < RETRY_TIMES:
            time.sleep(RETRY_INTERVAL_S)
    logger.error("arxiv page start=%d 重试 %d 次仍失败，放弃该页", start, RETRY_TIMES)
    return None


def _parse_entries(payload: bytes) -> list[ArxivEntry]:
    feed = feedparser.parse(payload)
    entries: list[ArxivEntry] = []
    for e in feed.entries:
        raw_id = e.get("id", "").rsplit("/abs/", 1)[-1]
        if not raw_id:
            continue
        published_struct = e.get("published_parsed")
        if published_struct is None:
            continue
        published = dt.date(*published_struct[:3])  # arXiv 时间为 UTC

        pdf_url = None
        abs_url = None
        for link in e.get("links", []):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href")
            elif link.get("rel") == "alternate":
                abs_url = link.get("href")
        base_id = strip_version(raw_id)

        primary = None
        primary_cat = e.get("arxiv_primary_category")
        if isinstance(primary_cat, dict):
            primary = primary_cat.get("term")

        entries.append(
            ArxivEntry(
                arxiv_id=base_id,
                title=" ".join(e.get("title", "").split()),
                authors=[a.get("name", "") for a in e.get("authors", [])],
                abstract=" ".join(e.get("summary", "").split()),
                primary_category=primary,
                categories=[t.get("term", "") for t in e.get("tags", [])],
                published=published,
                pdf_url=pdf_url or f"https://arxiv.org/pdf/{base_id}",
                abs_url=abs_url or f"https://arxiv.org/abs/{base_id}",
            )
        )
    return entries


def fetch_window_days() -> int:
    """首次运行（papers 表为空）回退为 3 天，否则用配置值。"""
    with SessionLocal() as s:
        empty = s.scalar(select(Paper.id).limit(1)) is None
    return FIRST_RUN_DAYS if empty else max(1, get_settings().arxiv_fetch_days)


def fetch_entries(settings: Settings, days: int) -> tuple[list[ArxivEntry], list[str]]:
    """分页抓完窗口内全部条目；返回 (entries, errors)。

    窗口取 min(days, arxiv_max_age_days)：只收最近 1 年内（默认）的论文，
    防止调大 ARXIV_FETCH_DAYS 补数据时旧论文混入。
    """
    window = max(1, min(days, settings.arxiv_max_age_days))
    cutoff = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=window)
    entries: list[ArxivEntry] = []
    errors: list[str] = []
    start = 0
    proxy = settings.http_proxy
    with httpx.Client(
        timeout=httpx.Timeout(30.0, read=60.0),
        proxy=proxy,
        headers={"User-Agent": "paperbot/0.1 (daily arxiv digest; contact: local user)"},
        follow_redirects=True,
    ) as client:
        while True:
            payload = _fetch_page(client, settings, start)
            if payload is None:
                errors.append(f"start={start} 页抓取失败")
                break
            page = _parse_entries(payload)
            if not page:
                break
            in_window = [e for e in page if e.published >= cutoff]
            entries.extend(in_window)
            oldest = min(e.published for e in page)
            logger.info(
                "arxiv page start=%d：%d 条，窗口内 %d 条，最早 %s",
                start, len(page), len(in_window), oldest,
            )
            # 本页最早已早于窗口 → 之后没有更新的了；页不满 → 没有更多结果
            if oldest < cutoff or len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            time.sleep(REQUEST_INTERVAL_S)
    return entries, errors


def store_entries(entries: list[ArxivEntry], settings: Settings) -> FetchResult:
    """去重 + 关键词过滤后入库。"""
    result = FetchResult(fetched=len(entries))
    keywords = settings.keywords
    with SessionLocal.begin() as s:
        existing = set(s.scalars(select(Paper.arxiv_id)).all())
        seen_batch: set[str] = set()
        for e in entries:
            if e.arxiv_id in existing or e.arxiv_id in seen_batch:
                result.skipped_existing += 1
                continue
            seen_batch.add(e.arxiv_id)
            kw = match_keyword(e.title, e.abstract, keywords)
            paper = Paper(
                arxiv_id=e.arxiv_id,
                title=e.title,
                authors=json.dumps(e.authors, ensure_ascii=False),
                abstract=e.abstract,
                primary_category=e.primary_category,
                categories=",".join(e.categories),
                published=e.published,
                pdf_url=e.pdf_url,
                abs_url=e.abs_url,
                matched_keyword=kw,
                filtered_out=0 if kw else 1,
            )
            s.add(paper)
            if kw:
                result.new_hit += 1
            else:
                result.new_filtered += 1
    return result


def run_fetch(settings: Settings) -> FetchResult:
    """FR-1 完整流程：抓窗口 → 入库。失败不阻塞已入库队列。"""
    days = fetch_window_days()
    logger.info("开始抓取 arXiv：窗口 %d 天，分类 %s", days, settings.categories)
    entries, errors = fetch_entries(settings, days)
    result = store_entries(entries, settings)
    result.window_days = days
    result.errors = errors
    logger.info(
        "抓取完成：窗口 %d 天，获取 %d，新命中 %d，过滤 %d，去重跳过 %d，错误 %d",
        result.window_days, result.fetched, result.new_hit,
        result.new_filtered, result.skipped_existing, len(result.errors),
    )
    return result
