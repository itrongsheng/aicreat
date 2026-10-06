"""ORM 模型与 Alembic 迁移：24 张表、约束/索引命名、真实外键、models.py 与 0001 一致、0002 seed、离线 MySQL SQL。"""

from __future__ import annotations

import os

# 须在导入 app.core 之前设置（conftest 已设置时不覆盖）
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_mock_engine, delete, func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.core import admin_permissions as ap
from app.core.database import Base, make_engine

SERVER_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = SERVER_DIR / "alembic.ini"

# docs/03「表总览」B.1 ~ B.24
EXPECTED_TABLES = [
    "admins", "admin_groups", "admin_permissions", "admin_group_permissions", "admin_operation_logs", "settings",
    "projects", "prompt_templates", "generation_batches", "keywords", "titles", "contents", "content_versions",
    "media_assets", "ai_tasks", "ai_models", "capability_routes", "ai_usage_logs", "publish_platforms",
    "publish_links", "link_checks", "index_checks", "alerts", "daily_stats",
]

# 唯一约束（docs/03 各表「索引」）
EXPECTED_UNIQUES: dict[str, dict[str, list[str]]] = {
    "admins": {"uq_admins_username": ["username"]},
    "admin_groups": {"uq_admin_groups_code": ["code"], "uq_admin_groups_name": ["name"]},
    "admin_permissions": {"uq_admin_permissions_code": ["code"]},
    "projects": {"uq_projects_owner_id_name": ["owner_id", "name"], "uq_projects_owner_id_slug": ["owner_id", "slug"]},
    "prompt_templates": {"uq_prompt_templates_code_version": ["code", "version"]},
    "keywords": {"uq_keywords_project_id_normalized_keyword": ["project_id", "normalized_keyword"]},
    "content_versions": {"uq_content_versions_content_id_version_no": ["content_id", "version_no"]},
    "ai_models": {"uq_ai_models_model_id": ["model_id"]},
    "capability_routes": {"uq_capability_routes_capability_project_id": ["capability", "project_id"]},
    "ai_usage_logs": {"uq_ai_usage_logs_entry_hash": ["entry_hash"]},
    "publish_platforms": {"uq_publish_platforms_code": ["code"]},
    "publish_links": {"uq_publish_links_url_hash": ["url_hash"]},
    "daily_stats": {
        "uq_daily_stats_stat_date_project_id_dimension_dimension_key": ["stat_date", "project_id", "dimension", "dimension_key"],
    },
}

# 普通索引（列序）
EXPECTED_INDEXES: dict[str, list[list[str]]] = {
    "admins": [["group_id"], ["is_active"]],
    "admin_groups": [],
    "admin_permissions": [["module", "sort"]],
    "admin_group_permissions": [["permission_id"]],
    "admin_operation_logs": [["admin_id", "created_at"], ["permission_code"], ["target_type", "target_id"], ["created_at"]],
    "settings": [],
    "projects": [["status"]],
    "prompt_templates": [["kind", "status", "project_id"], ["project_id"]],
    "generation_batches": [["project_id", "kind", "status"], ["status", "created_at"], ["created_by", "created_at"]],
    "keywords": [["project_id", "status", "score"], ["batch_id"], ["ai_task_id"]],
    "titles": [["keyword_id", "status"], ["project_id", "status", "created_at"], ["batch_id"], ["ai_task_id"]],
    "contents": [
        ["project_id", "status", "updated_at"], ["title_id"], ["keyword_id"], ["status", "created_at"], ["batch_id"], ["created_by"],
    ],
    "content_versions": [["ai_task_id"]],
    "media_assets": [
        ["project_id", "kind", "status", "created_at"], ["content_id", "sort"], ["ai_task_id"], ["status", "updated_at"],
        ["status", "next_transfer_at"], ["upstream_task_id"], ["kind", "ready_at"], ["kind", "failed_at"], ["created_by", "created_at"],
    ],
    "ai_tasks": [
        ["status", "next_poll_at"], ["status", "heartbeat_at"], ["root_task_id"], ["request_id"], ["upstream_task_id"], ["batch_id"],
        ["target_type", "target_id", "operation", "status"], ["project_id", "capability", "created_at"],
        ["capability", "model", "created_at"], ["created_at"],
    ],
    "ai_models": [["is_available", "sort_order"], ["vendor_id"]],
    "capability_routes": [],
    "ai_usage_logs": [["request_id", "log_type"], ["upstream_task_id"], ["ai_task_id"], ["model_name", "pulled_at"], ["matched_at"]],
    "publish_platforms": [["is_active", "sort"]],
    "publish_links": [
        ["content_id"], ["project_id", "platform_id", "published_at"], ["platform_id", "alive_status"],
        ["is_monitoring", "next_check_at"], ["is_monitoring", "next_index_check_at"], ["alive_status"], ["published_at"],
    ],
    "link_checks": [["link_id", "checked_at"], ["checked_at"], ["result_status", "checked_at"]],
    "index_checks": [["link_id", "kind", "engine", "checked_at"], ["checked_at"], ["kind", "result_status", "checked_at"], ["ai_task_id"]],
    "alerts": [
        ["status", "severity", "last_triggered_at"], ["dedupe_key", "status"], ["project_id", "status"],
        ["alert_type", "created_at"], ["target_type", "target_key"],
    ],
    "daily_stats": [["stat_date", "dimension"], ["project_id", "stat_date"]],
}

# 6 处真实外键：(表, 列) → (被引用表, 名称, ON DELETE)
EXPECTED_FKS = {
    ("admins", "group_id"): ("admin_groups", "fk_admins_group_id", "RESTRICT"),
    ("admin_group_permissions", "group_id"): ("admin_groups", "fk_admin_group_permissions_group_id", "RESTRICT"),
    ("admin_group_permissions", "permission_id"): ("admin_permissions", "fk_admin_group_permissions_permission_id", "RESTRICT"),
    ("content_versions", "content_id"): ("contents", "fk_content_versions_content_id", "CASCADE"),
    ("link_checks", "link_id"): ("publish_links", "fk_link_checks_link_id", "CASCADE"),
    ("index_checks", "link_id"): ("publish_links", "fk_index_checks_link_id", "CASCADE"),
}

COMPOSITE_PKS = {"settings": ["key", "locale"], "admin_group_permissions": ["group_id", "permission_id"]}


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def orm_engine() -> Iterator[Engine]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def db(orm_engine: Engine) -> Iterator[Session]:
    with Session(orm_engine, autoflush=False, expire_on_commit=False) as session:
        yield session


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture()
def migrated_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(url), "head")
    return url


# ---------------------------------------------------------------- 元数据结构


def test_metadata_has_exactly_the_24_tables() -> None:
    assert sorted(Base.metadata.tables) == sorted(EXPECTED_TABLES)
    assert len(m.ALL_MODELS) == 24
    assert {model.__tablename__ for model in m.ALL_MODELS} == set(EXPECTED_TABLES)


def test_create_all_on_sqlite_memory(orm_engine: Engine) -> None:
    insp = inspect(orm_engine)
    assert sorted(insp.get_table_names()) == sorted(EXPECTED_TABLES)
    for table in EXPECTED_TABLES:
        columns = {c["name"] for c in insp.get_columns(table)}
        assert {"created_at", "updated_at"} <= columns, table
        uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints(table)}
        assert uniques == EXPECTED_UNIQUES.get(table, {}), table
        pk = insp.get_pk_constraint(table)["constrained_columns"]
        assert pk == COMPOSITE_PKS.get(table, ["id"]), table


def test_indexes_follow_docs_and_naming_convention() -> None:
    for table_name, expected in EXPECTED_INDEXES.items():
        table = Base.metadata.tables[table_name]
        actual = []
        for index in table.indexes:
            cols = [c.name for c in index.columns]
            assert index.name == f"ix_{table_name}_{'_'.join(cols)}"
            assert not index.unique
            actual.append(cols)
        assert sorted(actual) == sorted(expected), table_name
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint":
                assert constraint.name == f"uq_{table_name}_{'_'.join(c.name for c in constraint.columns)}"


def test_exactly_six_real_foreign_keys() -> None:
    found = {}
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            found[(table.name, fk.parent.name)] = (fk.column.table.name, fk.constraint.name, fk.ondelete)
    assert found == EXPECTED_FKS


def test_user_system_columns() -> None:
    groups = Base.metadata.tables["admin_groups"]
    assert groups.c.data_scope.type.length == 16 and not groups.c.data_scope.nullable
    projects = Base.metadata.tables["projects"]
    assert not projects.c.owner_id.nullable
    assert "ix_projects_owner_id" not in {ix.name for ix in projects.indexes}
    assert "ix_media_assets_created_by_created_at" in {ix.name for ix in Base.metadata.tables["media_assets"].indexes}
    assert Base.metadata.tables["settings"].c.key.name == "key"


def test_mysql_ddl_rendering() -> None:
    statements: list[str] = []
    engine = create_mock_engine("mysql+pymysql://", lambda sql, *a, **kw: statements.append(str(sql.compile(dialect=engine.dialect))))
    Base.metadata.create_all(engine, checkfirst=False)
    ddl = "\n".join(statements)
    assert ddl.count("CREATE TABLE") == 24
    assert ddl.count("DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP") == 24
    assert "ON alerts (dedupe_key(191), status)" in ddl
    assert "`key` VARCHAR(80) NOT NULL" in ddl
    assert "value MEDIUMTEXT NOT NULL" in ddl
    assert "body MEDIUMTEXT" in ddl and "request_payload_json MEDIUMTEXT" in ddl
    assert "difficulty TINYINT" in ddl and "is_active BOOL NOT NULL DEFAULT 1" in ddl
    assert "data_scope VARCHAR(16) NOT NULL DEFAULT 'own'" in ddl
    assert "cost_cny NUMERIC(14, 6)" in ddl
    assert "ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci" in ddl
    assert "ENUM" not in ddl
    assert ddl.count("FOREIGN KEY") == 6


def test_literal_enums() -> None:
    assert m.enum_values(m.DataScopeValue) == ("all", "own")
    assert m.enum_values(m.ContentStatus) == (
        "draft", "generating", "ready", "reviewing", "approved", "rejected", "published", "archived",
    )
    assert len(m.enum_values(m.ErrorCategory)) == 15
    assert len(m.enum_values(m.AiTaskOperation)) == 13
    assert len(m.enum_values(m.AlertType)) == 11
    assert m.enum_values(m.AdminGroupCode) == tuple(group["code"] for group in ap.SYSTEM_GROUPS)
    for group in ap.SYSTEM_GROUPS:
        assert group["data_scope"] in m.enum_values(m.DataScopeValue)


# ---------------------------------------------------------------- ORM 行为


def _group(db: Session, code: str = "operator") -> m.AdminGroup:
    group = m.AdminGroup(code=code, name=f"组{code}", name_en=code)
    db.add(group)
    db.flush()
    return group


def test_defaults_and_timestamps(db: Session) -> None:
    group = _group(db)
    assert group.data_scope == "own" and group.is_active is True and group.is_system is False
    admin = m.Admin(username="u1", password_hash="x", group_id=group.id)
    db.add(admin)
    db.commit()
    assert admin.id > 0 and admin.token_version == 1 and admin.is_active is True
    assert isinstance(admin.created_at, datetime) and admin.created_at.tzinfo is None
    # 数据库默认值（绕过 ORM 的 Core 插入）同样生效
    db.execute(m.AdminGroup.__table__.insert().values(code="raw", name="原始", name_en="raw"))
    raw = db.execute(select(m.AdminGroup).where(m.AdminGroup.code == "raw")).scalar_one()
    assert raw.data_scope == "own" and raw.is_active is True and raw.created_at is not None and raw.updated_at is not None
    # onupdate：ORM 与 Core update() 都刷新 updated_at
    old = datetime(2020, 1, 1)
    db.execute(update(m.Admin).where(m.Admin.id == admin.id).values(updated_at=old))
    db.expire_all()
    assert db.get(m.Admin, admin.id).updated_at == old
    db.execute(update(m.Admin).where(m.Admin.id == admin.id).values(display_name="新"))
    db.expire_all()
    assert db.get(m.Admin, admin.id).updated_at > old


def test_settings_reserved_key_column(db: Session) -> None:
    db.add(m.Setting(key="stats_config", locale="*", value='{"version":1}'))
    db.add(m.Setting(key="system_info", locale="zh-CN", value="{}"))
    db.add(m.Setting(key="system_info", locale="en-US", value="{}"))
    db.commit()
    assert db.get(m.Setting, ("stats_config", "*")).value == '{"version":1}'
    db.add(m.Setting(key="stats_config", locale="*", value="{}"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_project_uniqueness_is_per_owner(db: Session) -> None:
    def project(owner: int, name: str, slug: str) -> m.Project:
        return m.Project(name=name, slug=slug, owner_id=owner, created_by=owner)

    db.add_all([project(1, "示例", "demo"), project(2, "示例", "demo")])
    db.commit()
    assert db.scalar(select(func.count()).select_from(m.Project)) == 2
    first = db.scalars(select(m.Project).order_by(m.Project.id)).first()
    assert first.language == "zh-CN" and first.default_style == "news" and first.status == "active"
    db.add(project(1, "示例", "other"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(project(1, "其它", "demo"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_real_foreign_keys_restrict_and_cascade(db: Session) -> None:
    group = _group(db)
    db.add(m.Admin(username="u1", password_hash="x", group_id=group.id))
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(delete(m.AdminGroup).where(m.AdminGroup.id == group.id))
    db.rollback()

    content = m.Content(project_id=1, title="t", language="zh-CN", style="news", created_by=1, updated_by=1)
    db.add(content)
    db.flush()
    db.add(m.ContentVersion(content_id=content.id, version_no=1, source="manual", title="t", body="b", content_hash="0" * 64, created_by=1))
    link = m.PublishLink(
        project_id=1, content_id=content.id, platform_id=1, url="https://a.com/x", normalized_url="https://a.com/x",
        url_hash="1" * 64, domain="a.com", published_at=m.utcnow(), backfilled_by=1, title_snapshot="t",
    )
    db.add(link)
    db.flush()
    now = m.utcnow()
    db.add(m.LinkCheck(link_id=link.id, check_type="baseline", result_status="alive", previous_status="pending",
                       applied_status="alive", matched_rule="ok", duration_ms=10, checked_at=now))
    db.add(m.IndexCheck(link_id=link.id, kind="seo", engine="baidu", provider="manual", check_type="manual",
                        result_status="indexed", previous_status="unknown", match_mode="manual",
                        confidence=Decimal("1"), checked_at=now))
    db.commit()
    assert link.alive_status == "pending" and link.is_monitoring is True and link.seo_indexed_any is False

    db.execute(delete(m.Content).where(m.Content.id == content.id))
    db.execute(delete(m.PublishLink).where(m.PublishLink.id == link.id))
    db.commit()
    assert db.scalar(select(func.count()).select_from(m.ContentVersion)) == 0
    assert db.scalar(select(func.count()).select_from(m.LinkCheck)) == 0
    assert db.scalar(select(func.count()).select_from(m.IndexCheck)) == 0


def test_numeric_and_signed_bigint_roundtrip(db: Session) -> None:
    db.add(m.DailyStat(stat_date=date(2026, 10, 6), dimension="total", cost_cny=Decimal("1.234567"), computed_at=m.utcnow()))
    signed = -(1 << 63) + 5
    link = m.PublishLink(
        project_id=1, content_id=1, platform_id=1, url="https://b.com", normalized_url="https://b.com", url_hash="2" * 64,
        domain="b.com", published_at=m.utcnow() - timedelta(days=1), backfilled_by=1, title_snapshot="t", baseline_simhash=signed,
    )
    db.add(link)
    db.commit()
    db.expire_all()
    row = db.scalars(select(m.DailyStat)).one()
    assert row.project_id == 0 and row.dimension_key == "" and row.cost_cny == Decimal("1.234567") and row.ai_calls == 0
    assert db.get(m.PublishLink, link.id).baseline_simhash == signed
    assert "cost_cny" in m.DAILY_STATS_METRIC_COLUMNS and "computed_at" not in m.DAILY_STATS_METRIC_COLUMNS


# ---------------------------------------------------------------- 迁移


def test_migration_0001_matches_models(migrated_url: str) -> None:
    engine = make_engine(migrated_url)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            assert diff == []
            insp = inspect(conn)
            assert set(insp.get_table_names()) == set(EXPECTED_TABLES) | {"alembic_version"}
            for table_name in EXPECTED_TABLES:
                table = Base.metadata.tables[table_name]
                reflected = insp.get_columns(table_name)
                assert [c["name"] for c in reflected] == [c.name for c in table.columns], table_name
                for col in reflected:
                    model_col = table.c[col["name"]]
                    assert col["nullable"] == model_col.nullable, (table_name, col["name"])
                    assert (col["default"] is None) == (model_col.server_default is None), (table_name, col["name"])
                uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints(table_name)}
                assert uniques == EXPECTED_UNIQUES.get(table_name, {}), table_name
                indexes = {ix["name"]: ix["column_names"] for ix in insp.get_indexes(table_name) if not ix["unique"]}
                assert indexes == {ix.name: [c.name for c in ix.columns] for ix in table.indexes}, table_name
                fks = {(table_name, fk["constrained_columns"][0]): (fk["referred_table"], fk["name"], fk["options"].get("ondelete"))
                       for fk in insp.get_foreign_keys(table_name)}
                assert fks == {k: v for k, v in EXPECTED_FKS.items() if k[0] == table_name}, table_name
    finally:
        engine.dispose()


def test_migration_0001_source_matches_models_source() -> None:
    """不连库的文本级比对：迁移中每张表的列名序列与 models.py 一致。"""
    source = (SERVER_DIR / "migrations" / "versions" / "0001_initial.py").read_text(encoding="utf-8")
    blocks = re.findall(r"op\.create_table\('(\w+)',(.*?)\n    \)", source, flags=re.S)
    assert len(blocks) == 24
    for name, body in blocks:
        columns = re.findall(r"sa\.Column\('(\w+)'", body)
        assert columns == [c.name for c in Base.metadata.tables[name].columns], name


def test_migration_0002_seed(migrated_url: str) -> None:
    engine = make_engine(migrated_url)
    try:
        with Session(engine) as db:
            permissions = db.scalars(select(m.AdminPermission)).all()
            assert {p.code for p in permissions} == ap.PERMISSION_CODES and len(permissions) == 90
            spec = ap.PERMISSIONS_BY_CODE["content.projects.create"]
            row = next(p for p in permissions if p.code == spec.code)
            assert (row.module, row.name, row.type, row.parent_code, row.sort) == (spec.module, spec.name, spec.type, spec.parent_code, spec.sort)
            groups = {g.code: g for g in db.scalars(select(m.AdminGroup)).all()}
            assert {code: g.data_scope for code, g in groups.items()} == {
                "super_admin": "all", "operator": "own", "reviewer": "all", "read_only": "all",
            }
            assert all(g.is_system and g.is_active for g in groups.values())
            by_id = {p.id: p.code for p in permissions}
            links: dict[str, set[str]] = {code: set() for code in groups}
            id_to_code = {g.id: code for code, g in groups.items()}
            for link in db.scalars(select(m.AdminGroupPermission)).all():
                links[id_to_code[link.group_id]].add(by_id[link.permission_id])
            assert links["super_admin"] == ap.PERMISSION_CODES
            for code in ("operator", "reviewer", "read_only"):
                assert links[code] == ap.DEFAULT_GROUP_PERMISSIONS[code], code
    finally:
        engine.dispose()


def test_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'cycle.db'}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    try:
        with Session(engine) as db:
            super_admin = db.scalars(select(m.AdminGroup).where(m.AdminGroup.code == "super_admin")).one()
            db.add(m.Admin(username="admin", password_hash="x", group_id=super_admin.id))
            db.commit()
        command.downgrade(cfg, "0001")
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(m.AdminPermission)) == 0
            assert db.scalar(select(func.count()).select_from(m.AdminGroupPermission)) == 0
            # 仍有管理员归属的 super_admin 组保留，其余系统组删除
            assert db.scalars(select(m.AdminGroup.code)).all() == ["super_admin"]
        command.upgrade(cfg, "head")  # 幂等：已存在的组不冲突
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(m.AdminPermission)) == 90
            assert db.scalar(select(func.count()).select_from(m.AdminGroup)) == 4
        command.downgrade(cfg, "base")
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


def test_alembic_offline_mysql_sql() -> None:
    env = {**os.environ, "DATABASE_URL": "mysql+pymysql://u:p@localhost/x"}
    alembic_bin = Path(sys.executable).with_name("alembic")
    result = subprocess.run(
        [str(alembic_bin), "upgrade", "head", "--sql"], cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    assert "Context impl MySQLImpl" in result.stderr
    assert sql.count("CREATE TABLE") == 25  # 24 张业务表 + alembic_version
    assert sql.count("DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP") == 24
    assert "CREATE INDEX ix_alerts_dedupe_key_status ON alerts (dedupe_key(191), status)" in sql
    assert "`key` VARCHAR(80) NOT NULL" in sql
    assert sql.count("FOREIGN KEY") == 6
    assert "INSERT INTO admin_permissions" in sql and "ON DUPLICATE KEY UPDATE" in sql
    assert "INSERT INTO admin_groups" in sql and "'operator', '运营人员', 'Operator'" in sql
    assert sql.count("INSERT INTO admin_group_permissions") == 4
    assert "UPDATE alembic_version SET version_num='0002'" in sql
