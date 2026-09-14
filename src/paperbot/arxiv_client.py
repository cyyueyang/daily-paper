"""FR-1 arXiv 抓取 + 关键词过滤 + 去重。

- 官方 Atom API（export.arxiv.org），feedparser 解析，不用第三方爬虫库
- 按 submittedDate 降序分页（start + max_results），把日期窗口内的结果全部抓完，不设条数上限
- 请求间隔 >= 3 秒；超时/非 200/429 重试 2 次（间隔 5s）
- 去重：arxiv_id（不含版本号）为唯一键，跨分类只入库一次，v2+ 不重复推送
"""

from __future__ import annotations

import datetime as dt
import html as _html
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
ARXIV_LIST_URL = "https://arxiv.org/list/{cat}/new"
ARXIV_ABS_URL = "https://arxiv.org/abs/{arxiv_id}"
PAGE_SIZE = 100
REQUEST_INTERVAL_S = 3.0  # arXiv rate limit
LIST_REQUEST_INTERVAL_S = 2.0  # 降级通道逐篇抓 abs 页的间隔
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
        retry_after: str | None = None
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.content
            retry_after = resp.headers.get("Retry-After")  # arXiv 429 常带此头
            logger.warning(
                "arxiv page start=%d 返回 %d（第 %d 次）", start, resp.status_code, attempt + 1
            )
        except httpx.HTTPError as exc:
            logger.warning("arxiv page start=%d 请求异常 %s（第 %d 次）", start, exc, attempt + 1)
        if attempt < RETRY_TIMES:
            # 默认 5s（spec），服务器给了 Retry-After 则从其值
            time.sleep(float(retry_after) if retry_after else RETRY_INTERVAL_S)
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


# ---------- 降级通道：arXiv 主站列表页（官方 API 不可用时启用） ----------

_LISTING_DATE_RE = re.compile(r"Showing new listings for ([^<]+)</h3>")
_LIST_ID_RE = re.compile(r'href\s*=\s*"/abs/([0-9.]+)"')
_LIST_TITLE_RE = re.compile(r"list-title[^>]*>.*?</span>\s*(.*?)</div>", re.DOTALL)
_LIST_AUTHORS_RE = re.compile(r"list-authors[^>]*>(.*?)</div>", re.DOTALL)
_LIST_SUBJECTS_RE = re.compile(r"list-subjects[^>]*>(.*?)</div>", re.DOTALL)
_PRIMARY_SUBJECT_RE = re.compile(r"primary-subject[^>]*>.*?\(([^)]+)\)", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ABS_ABSTRACT_RE = re.compile(r'class="abstract mathjax">.*?</span>(.*?)</blockquote>', re.DOTALL)
_ABS_DATELINE_RE = re.compile(r"Submitted on ([0-9]{1,2} [A-Z][a-z]{2} [0-9]{4})")


def _strip_tags(fragment: str) -> str:
    return " ".join(_html.unescape(_TAG_RE.sub("", fragment)).split())


def _parse_listing_page(html_text: str) -> tuple[dt.date | None, list[dict]]:
    """解析 /list/{cat}/new：只取 New + Cross submissions，跳过 Replacement（v2+ 更新不推）。"""
    m = _LISTING_DATE_RE.search(html_text)
    listing_date = None
    if m:
        try:
            listing_date = dt.datetime.strptime(m.group(1).strip(), "%A, %d %B %Y").date()
        except ValueError:
            logger.warning("列表页日期解析失败：%s", m.group(1))

    # 截掉 Replacement 段
    cut = re.search(r"<h3[^>]*>\s*Replacement submissions", html_text)
    if cut:
        html_text = html_text[: cut.start()]

    entries: list[dict] = []
    for block in re.split(r"<dt>", html_text)[1:]:
        id_m = _LIST_ID_RE.search(block)
        title_m = _LIST_TITLE_RE.search(block)
        if not (id_m and title_m):
            continue
        authors_m = _LIST_AUTHORS_RE.search(block)
        subjects_m = _LIST_SUBJECTS_RE.search(block)
        subjects_html = subjects_m.group(1) if subjects_m else ""
        primary_m = _PRIMARY_SUBJECT_RE.search(subjects_html)
        entries.append({
            "arxiv_id": strip_version(id_m.group(1)),
            "title": _strip_tags(title_m.group(1)),
            "authors": re.findall(r"<a[^>]*>([^<]+)</a>", authors_m.group(1)) if authors_m else [],
            "primary_category": primary_m.group(1) if primary_m else None,
            "categories": re.findall(r"\(([a-zA-Z][a-zA-Z.\-]+)\)", subjects_html),
        })
    return listing_date, entries


def _parse_abs_page(html_text: str) -> tuple[str | None, dt.date | None]:
    """abs 页解析：(摘要, v1 提交日期)。失败返回 (None, None)。"""
    abs_m = _ABS_ABSTRACT_RE.search(html_text)
    abstract = _strip_tags(abs_m.group(1)) if abs_m else None
    date_m = _ABS_DATELINE_RE.search(html_text)
    submitted = None
    if date_m:
        try:
            submitted = dt.datetime.strptime(date_m.group(1), "%d %b %Y").date()
        except ValueError:
            pass
    return abstract, submitted


def fetch_entries_listing(settings: Settings, days: int) -> tuple[list[ArxivEntry], list[str]]:
    """降级通道：逐分类抓 /list/{cat}/new，再逐篇抓 abs 页补摘要。

    SPEC-GAP（仅降级通道生效时）：
    - published 取「列表页公告日期」而非提交日期（列表页就是按公告日聚合的，
      且公告日才是用户视角的「今日新论文」；dateline 提交日仅用于 1 年新鲜度兜底）
    - 摘要需逐篇抓 abs 页（间隔 2s），篇数多时全程 10-20 分钟，属预期
    - abs 页抓取失败的篇目当日丢弃并记日志（次日列表更新后不会再出现，属可接受损耗）
    """
    errors: list[str] = []
    merged: dict[str, dict] = {}
    listing_date: dt.date | None = None
    with httpx.Client(
        timeout=httpx.Timeout(30.0, read=90.0),
        proxy=settings.http_proxy,
        headers={"User-Agent": "paperbot/0.1 (daily arxiv digest; fallback listing mode)"},
        follow_redirects=True,
    ) as client:
        for cat in settings.categories:
            raw = None
            for attempt in range(RETRY_TIMES + 1):
                try:
                    resp = client.get(ARXIV_LIST_URL.format(cat=cat))
                    if resp.status_code == 200:
                        raw = resp.text
                        break
                    logger.warning("列表页 %s 返回 %d（第 %d 次）", cat, resp.status_code, attempt + 1)
                except httpx.HTTPError as exc:
                    logger.warning("列表页 %s 请求异常 %s（第 %d 次）", cat, exc, attempt + 1)
                if attempt < RETRY_TIMES:
                    time.sleep(RETRY_INTERVAL_S)
            if raw is None:
                errors.append(f"列表页 {cat} 抓取失败")
                continue
            page_date, entries = _parse_listing_page(raw)
            listing_date = listing_date or page_date
            for e in entries:
                if e["arxiv_id"] in merged:  # 跨分类去重：合并 categories
                    merged[e["arxiv_id"]]["categories"] = sorted(
                        set(merged[e["arxiv_id"]]["categories"]) | set(e["categories"])
                    )
                else:
                    merged[e["arxiv_id"]] = e
            logger.info("列表页 %s：%d 条（累计去重后 %d 条）", cat, len(entries), len(merged))
            time.sleep(REQUEST_INTERVAL_S)

        if listing_date is None:
            listing_date = dt.datetime.now(dt.timezone.utc).date()
        age_floor = listing_date - dt.timedelta(days=settings.arxiv_max_age_days)

        results: list[ArxivEntry] = []
        total = len(merged)
        for idx, e in enumerate(merged.values(), 1):
            abstract = None
            for attempt in range(RETRY_TIMES + 1):
                try:
                    resp = client.get(ARXIV_ABS_URL.format(arxiv_id=e["arxiv_id"]))
                    if resp.status_code == 200:
                        abstract, submitted = _parse_abs_page(resp.text)
                        if submitted and submitted < age_floor:
                            abstract = "__SKIP__"  # 提交日超 1 年新鲜度兜底
                        break
                    logger.warning("abs 页 %s 返回 %d（第 %d 次）", e["arxiv_id"], resp.status_code, attempt + 1)
                except httpx.HTTPError as exc:
                    logger.warning("abs 页 %s 请求异常 %s（第 %d 次）", e["arxiv_id"], exc, attempt + 1)
                if attempt < RETRY_TIMES:
                    time.sleep(RETRY_INTERVAL_S)
            if abstract == "__SKIP__":
                continue
            if not abstract:
                errors.append(f"abs 页 {e['arxiv_id']} 无摘要，跳过")
                continue
            results.append(ArxivEntry(
                arxiv_id=e["arxiv_id"],
                title=e["title"],
                authors=e["authors"],
                abstract=abstract,
                primary_category=e["primary_category"],
                categories=e["categories"],
                published=listing_date,
                pdf_url=f"https://arxiv.org/pdf/{e['arxiv_id']}",
                abs_url=f"https://arxiv.org/abs/{e['arxiv_id']}",
            ))
            if idx % 25 == 0:
                logger.info("abs 页进度 %d/%d", idx, total)
            if idx < total:
                time.sleep(LIST_REQUEST_INTERVAL_S)
    logger.info("降级通道完成：%d/%d 篇成功获取摘要", len(results), total)
    return results, errors


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
    if not entries and errors:
        # 官方 API 整体失败（如出口 IP 被 429）→ 降级到主站列表页通道
        logger.warning("官方 API 不可用（%s），切换主站列表页降级通道", errors)
        entries, errors = fetch_entries_listing(settings, days)
    result = store_entries(entries, settings)
    result.window_days = days
    result.errors = errors
    logger.info(
        "抓取完成：窗口 %d 天，获取 %d，新命中 %d，过滤 %d，去重跳过 %d，错误 %d",
        result.window_days, result.fetched, result.new_hit,
        result.new_filtered, result.skipped_existing, len(result.errors),
    )
    return result
