"""FR-6 深度解读：PDF 下载 → pypdf 解析 → 截断 → DeepSeek 解读 → 入库缓存。

- PDF 存 data/pdfs/{arxiv_id}.pdf，已存在则复用
- 提取失败或文本 < 2000 字符 → 回退仅用 abstract，解读开头注明 ⚠️
- 全文 > 100k 字符截断保留前 60k + 后 20k（保住方法/实验与结论）
- detail_text 缓存命中时不重复调用 API
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from pypdf import PdfReader

from .config import get_settings
from .db import SessionLocal
from .llm import generate_detail
from .models import Paper
from .queue_service import mark_read, record_detail

logger = logging.getLogger(__name__)

MIN_FULLTEXT_CHARS = 2000
FULLTEXT_LIMIT = 100_000
FULLTEXT_HEAD = 60_000
FULLTEXT_TAIL = 20_000
FALLBACK_NOTICE = "⚠️ 全文获取失败，本解读基于摘要"


@dataclass
class DetailResult:
    text: str
    from_cache: bool
    used_fulltext: bool


def _pdf_path(arxiv_id: str):
    return get_settings().pdf_dir / f"{arxiv_id}.pdf"


def download_pdf(paper: Paper):
    """下载 PDF（已存在则复用）；失败返回 None。"""
    settings = get_settings()
    settings.ensure_dirs()
    path = _pdf_path(paper.arxiv_id)
    if path.exists() and path.stat().st_size > 0:
        return path
    if not paper.pdf_url:
        return None
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, read=120.0),
            proxy=settings.http_proxy,
            follow_redirects=True,
            headers={"User-Agent": "paperbot/0.1"},
        ) as client, client.stream("GET", paper.pdf_url) as resp:
            if resp.status_code != 200:
                logger.warning("PDF 下载返回 %d：%s", resp.status_code, paper.pdf_url)
                return None
            with open(path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    f.write(chunk)
        return path
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("PDF 下载失败 %s：%s", paper.arxiv_id, exc)
        path.unlink(missing_ok=True)
        return None


def extract_text(path) -> str:
    """pypdf 提取全文；失败返回空串。"""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # pypdf 异常类型繁多，统一兜底
        logger.warning("PDF 解析失败 %s：%s", path, exc)
        return ""


def truncate_fulltext(text: str) -> str:
    if len(text) <= FULLTEXT_LIMIT:
        return text
    return text[:FULLTEXT_HEAD] + "\n\n……（中间内容截断）……\n\n" + text[-FULLTEXT_TAIL:]


def get_or_create_detail(paper_id: int) -> DetailResult:
    """缓存优先；未缓存则走 PDF → LLM 全链路，结果入库。"""
    with SessionLocal() as s:
        paper = s.get(Paper, paper_id)
        if paper is None:
            raise KeyError(f"paper id={paper_id} 不存在")
        if paper.detail_text:
            return DetailResult(paper.detail_text, from_cache=True, used_fulltext=True)
        s.expunge(paper)

    full_text = ""
    pdf_path = download_pdf(paper)
    if pdf_path is not None:
        full_text = extract_text(pdf_path)

    if len(full_text) >= MIN_FULLTEXT_CHARS:
        content = truncate_fulltext(full_text)
        used_fulltext = True
    else:
        if full_text:
            logger.info("PDF 文本仅 %d 字符，回退摘要：%s", len(full_text), paper.arxiv_id)
        content = f"摘要：{paper.abstract}"
        used_fulltext = False

    result = generate_detail(paper.title, content, is_fulltext=used_fulltext)
    text = result.text if used_fulltext else f"{FALLBACK_NOTICE}\n\n{result.text}"
    record_detail(paper.id, text, result.tokens)
    mark_read(paper.id)  # 状态机：delivered/skipped → read
    logger.info(
        "深度解读完成 %s：%s，%d tokens", paper.arxiv_id,
        "全文" if used_fulltext else "摘要回退", result.tokens,
    )
    return DetailResult(text, from_cache=False, used_fulltext=used_fulltext)
