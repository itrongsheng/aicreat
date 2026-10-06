"""Alembic 环境：连接串取自 ``app.core.config.settings.database_url``（环境变量 ``DATABASE_URL``），元数据为 ``Base.metadata``。

- 在线：``alembic upgrade head``（发布流程 ``docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"``）。
- 离线：``alembic upgrade head --sql`` 只输出 SQL，不连接数据库（方言取自连接串）。
- 测试或脚本可经 ``config.attributes["connection"]`` 传入已打开的连接，或以 ``sqlalchemy.url`` 覆盖连接串。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import app.models  # noqa: F401  注册全部 24 张表到 Base.metadata
from app.core.config import settings
from app.core.database import Base, make_engine

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def _configure_kwargs(dialect_name: str) -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": False,
        # SQLite 不支持大多数 ALTER，后续迁移以 batch 模式改表
        "render_as_batch": dialect_name == "sqlite",
    }


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **_configure_kwargs(url.split(":", 1)[0].split("+", 1)[0]))
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs(connection.dialect.name))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    engine = make_engine(_database_url())
    try:
        with engine.connect() as conn:
            _run_with_connection(conn)
            conn.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
