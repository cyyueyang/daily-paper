"""FR-3 本地队列与状态机。

状态全在 DB，无内存态 → 进程重启后从 SQLite 完整恢复。
「下一条」用 UPDATE ... WHERE status='pending' 原子认领，
用户连发多条「下一条」不会重复发同一篇。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update

from .db import SessionLocal
from .models import (
    STATE_CURRENT_PAPER_ID,
    STATE_TOTAL_TOKENS,
    STATUS_DELIVERED,
    STATUS_PENDING,
    STATUS_READ,
    STATUS_SKIPPED,
    STATUS_SUMMARIZE_FAILED,
    Paper,
    State,
)

logger = logging.getLogger(__name__)


# ---------- state 表 KV ----------

def get_state(key: str) -> str | None:
    with SessionLocal() as s:
        row = s.get(State, key)
        return row.value if row else None


def set_state(key: str, value: str) -> None:
    with SessionLocal.begin() as s:
        row = s.get(State, key)
        if row is None:
            s.add(State(key=key, value=value))
        else:
            row.value = value


def _set_state_in(session, key: str, value: str) -> None:
    row = session.get(State, key)
    if row is None:
        session.add(State(key=key, value=value))
    else:
        row.value = value


def add_tokens(session, paper: Paper | None, tokens: int) -> None:
    """每次 LLM 调用记录 usage.total_tokens 到 papers 行与 state 累计值。"""
    if tokens <= 0:
        return
    if paper is not None:
        paper.token_usage = (paper.token_usage or 0) + tokens
    total = int(get_state_in(session, STATE_TOTAL_TOKENS) or 0)
    _set_state_in(session, STATE_TOTAL_TOKENS, str(total + tokens))


def get_state_in(session, key: str) -> str | None:
    row = session.get(State, key)
    return row.value if row else None


# ---------- 队列查询 ----------

def _queue_filter():
    """进入推送队列的条件：命中关键词 + 未失败 + 已生成卡片。"""
    return (
        Paper.filtered_out == 0,
        Paper.status != STATUS_SUMMARIZE_FAILED,
        Paper.card_text.isnot(None),
    )


def peek_next() -> Paper | None:
    """查看下一篇 pending（不改状态；CLI 调试用）。"""
    with SessionLocal() as s:
        return s.scalars(
            select(Paper)
            .where(*_queue_filter(), Paper.status == STATUS_PENDING)
            .order_by(Paper.published, Paper.id)
            .limit(1)
        ).first()


def next_paper() -> Paper | None:
    """「下一条」：原子认领最早 pending → delivered，并置为 current。无则返回 None。"""
    with SessionLocal.begin() as s:
        candidate_id = s.scalars(
            select(Paper.id)
            .where(*_queue_filter(), Paper.status == STATUS_PENDING)
            .order_by(Paper.published, Paper.id)
            .limit(1)
        ).first()
        if candidate_id is None:
            return None
        # 事务内条件更新：并发「下一条」只有一个能认领成功
        res = s.execute(
            update(Paper)
            .where(Paper.id == candidate_id, Paper.status == STATUS_PENDING)
            .values(status=STATUS_DELIVERED)
        )
        if res.rowcount != 1:
            return None
        _set_state_in(s, STATE_CURRENT_PAPER_ID, str(candidate_id))
        return s.get(Paper, candidate_id)


def get_current() -> Paper | None:
    with SessionLocal() as s:
        raw = get_state_in(s, STATE_CURRENT_PAPER_ID)
        if not raw:
            return None
        return s.get(Paper, int(raw))


def set_current(session, paper_id: int) -> None:
    _set_state_in(session, STATE_CURRENT_PAPER_ID, str(paper_id))


def set_current_by_id(paper_id: int) -> None:
    with SessionLocal.begin() as s:
        _set_state_in(s, STATE_CURRENT_PAPER_ID, str(paper_id))


def requeue(paper_id: int) -> None:
    """飞书发送失败时回滚：delivered → pending，下次「下一条」重发同一篇。"""
    with SessionLocal.begin() as s:
        s.execute(
            update(Paper)
            .where(Paper.id == paper_id, Paper.status == STATUS_DELIVERED)
            .values(status=STATUS_PENDING)
        )


def skip_paper(paper_id: int) -> None:
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is not None and paper.status in (STATUS_DELIVERED, STATUS_PENDING):
            paper.status = STATUS_SKIPPED


def mark_read(paper_id: int) -> None:
    """深度解读完成：delivered/skipped → read。"""
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is not None and paper.status in (STATUS_DELIVERED, STATUS_SKIPPED, STATUS_PENDING):
            paper.status = STATUS_READ


def star_paper(paper_id: int) -> Paper | None:
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is not None:
            paper.starred = 1
        return paper


def pending_count() -> int:
    with SessionLocal() as s:
        return s.scalar(
            select(func.count(Paper.id)).where(*_queue_filter(), Paper.status == STATUS_PENDING)
        ) or 0


# ---------- 速读卡片生成记录 ----------

def papers_awaiting_relevance() -> list[Paper]:
    """关键词命中但还没过二级语义判定的论文。"""
    with SessionLocal() as s:
        papers = list(
            s.scalars(
                select(Paper)
                .where(Paper.filtered_out == 0, Paper.relevance_checked == 0)
                .order_by(Paper.published, Paper.id)
            ).all()
        )
        s.expunge_all()
        return papers


def record_relevance(paper_id: int, direction: str | None, tokens: int) -> None:
    """记录语义判定结果；direction=None 表示淘汰（保留数据但不进队列）。"""
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is None:
            return
        paper.relevance_checked = 1
        paper.direction = direction
        if direction is None:
            paper.filtered_out = 1
        add_tokens(s, paper, tokens)


def papers_awaiting_summary(limit: int | None = None) -> list[Paper]:
    """命中关键词但还没卡片的 pending 论文（补生成入口；API key 后补也能恢复）。"""
    with SessionLocal() as s:
        q = (
            select(Paper)
            .where(
                Paper.filtered_out == 0,
                Paper.status == STATUS_PENDING,
                Paper.card_text.is_(None),
            )
            .order_by(Paper.published, Paper.id)
        )
        if limit:
            q = q.limit(limit)
        papers = list(s.scalars(q).all())
        s.expunge_all()
        return papers


def record_card(paper_id: int, card_text: str, tokens: int) -> None:
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is None:
            return
        paper.card_text = card_text
        add_tokens(s, paper, tokens)


def record_detail(paper_id: int, detail_text: str, tokens: int) -> None:
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is None:
            return
        paper.detail_text = detail_text
        add_tokens(s, paper, tokens)


def mark_summarize_failed(paper_id: int) -> None:
    with SessionLocal.begin() as s:
        paper = s.get(Paper, paper_id)
        if paper is not None:
            paper.status = STATUS_SUMMARIZE_FAILED


# ---------- 列表 / 统计 ----------

# 状态 emoji（FR-5）；delivered（正在读）spec 未给，取 📖（SPEC-GAP）
STATUS_EMOJI = {
    STATUS_PENDING: "⏳",
    STATUS_DELIVERED: "📖",
    STATUS_READ: "✅",
    STATUS_SKIPPED: "⏭️",
    STATUS_SUMMARIZE_FAILED: "⚠️",
}


def list_queue() -> list[Paper]:
    """「列表」：今日入队的全部论文（含各状态）+ 历史遗留未读完的 backlog。

    SPEC-GAP: spec 只说「今日队列概览」，未定义跨日未读完的展示；
    这里把仍为 pending/delivered 的历史论文一并列出，避免 backlog 不可见。
    """
    today = dt.datetime.combine(dt.date.today(), dt.time.min)
    with SessionLocal() as s:
        papers = list(
            s.scalars(
                select(Paper)
                .where(
                    Paper.filtered_out == 0,
                    Paper.status != STATUS_SUMMARIZE_FAILED,
                    Paper.card_text.isnot(None),
                    (
                        (Paper.created_at >= today)
                        | (Paper.status.in_([STATUS_PENDING, STATUS_DELIVERED]))
                    ),
                )
                .order_by(Paper.published, Paper.id)
            ).all()
        )
        s.expunge_all()
        return papers


@dataclass
class Stats:
    total_fetched: int
    total_hit: int
    read: int
    starred: int
    pending: int
    total_tokens: int


def get_stats() -> Stats:
    with SessionLocal() as s:
        total = s.scalar(select(func.count(Paper.id))) or 0
        hit = s.scalar(select(func.count(Paper.id)).where(Paper.filtered_out == 0)) or 0
        read = s.scalar(
            select(func.count(Paper.id)).where(Paper.status == STATUS_READ)
        ) or 0
        starred = s.scalar(select(func.count(Paper.id)).where(Paper.starred == 1)) or 0
        pending = s.scalar(
            select(func.count(Paper.id)).where(*_queue_filter(), Paper.status == STATUS_PENDING)
        ) or 0
        tokens = int(get_state_in(s, STATE_TOTAL_TOKENS) or 0)
    return Stats(total, hit, read, starred, pending, tokens)


def parse_authors(paper: Paper) -> list[str]:
    try:
        return list(json.loads(paper.authors or "[]"))
    except json.JSONDecodeError:
        return []
