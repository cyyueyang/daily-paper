"""ORM 模型（spec 5.1 数据模型）。

论文状态机：
    pending → delivered → read
                 ↓          ↑
              skipped ──────┘   （skipped 回复「详细」仍可深读）
    summarize_failed（终态，不进入推送队列）
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 论文状态
STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"
STATUS_SKIPPED = "skipped"
STATUS_SUMMARIZE_FAILED = "summarize_failed"

# state 表 KV 键
STATE_CURRENT_PAPER_ID = "current_paper_id"
STATE_LAST_DIGEST_DATE = "last_digest_date"
STATE_TOTAL_TOKENS = "total_tokens"


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # 不含版本号
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组字符串
    abstract: Mapped[str] = mapped_column(Text)
    primary_category: Mapped[str | None] = mapped_column(String, nullable=True)
    categories: Mapped[str | None] = mapped_column(String, nullable=True)  # 逗号分隔
    published: Mapped[dt.date] = mapped_column(Date)  # arXiv 提交日期（UTC）
    pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    abs_url: Mapped[str | None] = mapped_column(String, nullable=True)
    matched_keyword: Mapped[str | None] = mapped_column(String, nullable=True)  # 命中的第一个关键词
    filtered_out: Mapped[int] = mapped_column(Integer, default=0)  # 1=未命中关键词或语义过滤淘汰
    relevance_checked: Mapped[int] = mapped_column(Integer, default=0)  # 1=已过二级语义判定
    direction: Mapped[str | None] = mapped_column(String, nullable=True)  # 语义闸门判定的方向
    status: Mapped[str] = mapped_column(String, default=STATUS_PENDING, index=True)
    card_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 速读卡片缓存
    detail_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 深度解读缓存
    starred: Mapped[int] = mapped_column(Integer, default=0)
    token_usage: Mapped[int] = mapped_column(Integer, default=0)  # 该篇累计消耗 token
    # 本地时间戳（单用户本地工具，与定时任务的本地时区一致）
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.now, onupdate=dt.datetime.now
    )


class State(Base):
    """单行 KV 表：current_paper_id / last_digest_date / total_tokens。"""

    __tablename__ = "state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(String, nullable=True)
