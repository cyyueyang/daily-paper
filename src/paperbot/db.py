"""SQLAlchemy engine + session（FR-3：SQLite 单文件存储）。"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base


def _make_engine():
    settings = get_settings()
    settings.ensure_dirs()
    engine = create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False},  # WS 回调/调度器分线程访问
    )

    # WAL 允许读写并发（长连接线程读 + 定时任务线程写）
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # 轻量迁移：老库补 relevance_checked 列（create_all 不会改已有表）
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(papers)")}
        if "relevance_checked" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE papers ADD COLUMN relevance_checked INTEGER DEFAULT 0"
            )
