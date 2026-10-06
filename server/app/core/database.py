"""数据库引擎、会话工厂与 ORM 基类。

生产为 MySQL 8（``mysql+pymysql``，utf8mb4，会话时区 UTC）；测试会话允许 SQLite（docs/03「测试会话」）。
ORM 不使用 MySQL 专有列类型，service 中的写入保持方言无关。
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings

# 约束 / 索引命名约定：未显式命名的约束在 MySQL 与 SQLite 下得到稳定名称（Alembic autogenerate 依赖）
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str | None = None, **overrides: Any) -> Engine:
    """按连接串创建引擎：MySQL 连接池参数 / SQLite 线程与内存库适配。"""
    url = url or settings.database_url
    parsed = make_url(url)
    kwargs: dict[str, Any] = {}

    if parsed.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        database = parsed.database or ""
        if database in ("", ":memory:") or "mode=memory" in str(parsed.query):
            # 内存库：所有连接共享同一个底层连接，否则每个会话都看到空库
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(pool_pre_ping=True, pool_recycle=3600, pool_size=10, max_overflow=20, pool_timeout=30)
        if parsed.get_backend_name() == "mysql":
            connect_args: dict[str, Any] = {}
            if "charset" not in parsed.query:
                connect_args["charset"] = "utf8mb4"
            if parsed.get_driver_name() == "pymysql":
                # 库内 DATETIME 一律存 UTC；会话时区固定 UTC，使 NOW()/CURRENT_TIMESTAMP 与应用写入一致
                connect_args["init_command"] = "SET time_zone = '+00:00'"
            kwargs["connect_args"] = connect_args

    kwargs.update(overrides)
    engine = create_engine(url, **kwargs)
    if parsed.get_backend_name() == "sqlite":
        event.listen(engine, "connect", _sqlite_on_connect)
    return engine


engine: Engine = make_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个会话，结束时关闭（未提交的事务随 close 回滚）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """worker / 脚本用：块内正常结束提交，异常回滚，最后关闭。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
