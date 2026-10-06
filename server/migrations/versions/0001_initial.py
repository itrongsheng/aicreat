"""initial: 全部 24 张表、索引、唯一约束与 6 处真实外键（docs/03-data-model.md）

Revision ID: 0001
Revises:
Create Date: 2026-10-06 00:00:00+00:00

与 app/models.py 逐列一致（tests/test_models.py 校验）：
- 表顺序按 docs/03 B.1~B.24，被真实外键引用的 admin_groups / admin_permissions 提前；
- 索引命名 ix_<表>_<列…> / uq_<表>_<列…>，真实外键 fk_<表>_<列>（docs/03「数据库约定 · 命名」）；
- alerts.dedupe_key 在 MySQL 下为前缀索引 dedupe_key(191)；
- updated_at 在 MySQL 下为 DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP，其它方言 DEFAULT CURRENT_TIMESTAMP。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 创建顺序（downgrade 逆序删除）
TABLES: tuple[str, ...] = (
    "admin_groups",
    "admin_permissions",
    "admins",
    "admin_group_permissions",
    "admin_operation_logs",
    "settings",
    "projects",
    "prompt_templates",
    "generation_batches",
    "keywords",
    "titles",
    "contents",
    "content_versions",
    "media_assets",
    "ai_tasks",
    "ai_models",
    "capability_routes",
    "ai_usage_logs",
    "publish_platforms",
    "publish_links",
    "link_checks",
    "index_checks",
    "alerts",
    "daily_stats",
)


def _updated_at_default() -> sa.TextClause:
    if op.get_context().dialect.name == "mysql":
        return sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    return sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    updated_at_default = _updated_at_default()

    # admin_groups
    op.create_table('admin_groups',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('name_en', sa.String(length=80), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('is_system', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('data_scope', sa.String(length=16), server_default='own', nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_groups')),
        sa.UniqueConstraint('code', name='uq_admin_groups_code'),
        sa.UniqueConstraint('name', name='uq_admin_groups_name'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )

    # admin_permissions
    op.create_table('admin_permissions',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('module', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('parent_code', sa.String(length=100), nullable=True),
        sa.Column('sort', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_permissions')),
        sa.UniqueConstraint('code', name='uq_admin_permissions_code'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_admin_permissions_module_sort', 'admin_permissions', ['module', 'sort'], unique=False)

    # admins
    op.create_table('admins',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(length=50), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=50), nullable=True),
        sa.Column('group_id', sa.BigInteger(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('token_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['admin_groups.id'], name='fk_admins_group_id', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_admins')),
        sa.UniqueConstraint('username', name='uq_admins_username'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_admins_group_id', 'admins', ['group_id'], unique=False)
    op.create_index('ix_admins_is_active', 'admins', ['is_active'], unique=False)

    # admin_group_permissions
    op.create_table('admin_group_permissions',
        sa.Column('group_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('permission_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['admin_groups.id'], name='fk_admin_group_permissions_group_id', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['permission_id'], ['admin_permissions.id'], name='fk_admin_group_permissions_permission_id', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('group_id', 'permission_id', name=op.f('pk_admin_group_permissions')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_admin_group_permissions_permission_id', 'admin_group_permissions', ['permission_id'], unique=False)

    # admin_operation_logs
    op.create_table('admin_operation_logs',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('admin_id', sa.BigInteger(), nullable=False),
        sa.Column('permission_code', sa.String(length=100), nullable=False),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('target_type', sa.String(length=50), nullable=False),
        sa.Column('target_id', sa.String(length=64), nullable=True),
        sa.Column('summary', sa.String(length=255), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=False),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_operation_logs')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_admin_operation_logs_admin_id_created_at', 'admin_operation_logs', ['admin_id', 'created_at'], unique=False)
    op.create_index('ix_admin_operation_logs_permission_code', 'admin_operation_logs', ['permission_code'], unique=False)
    op.create_index('ix_admin_operation_logs_target_type_target_id', 'admin_operation_logs', ['target_type', 'target_id'], unique=False)
    op.create_index('ix_admin_operation_logs_created_at', 'admin_operation_logs', ['created_at'], unique=False)

    # settings
    op.create_table('settings',
        sa.Column('key', sa.String(length=80), autoincrement=False, nullable=False),
        sa.Column('locale', sa.String(length=10), autoincrement=False, nullable=False),
        sa.Column('value', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('key', 'locale', name=op.f('pk_settings')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )

    # projects
    op.create_table('projects',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('slug', sa.String(length=80), nullable=False),
        sa.Column('industry', sa.String(length=80), nullable=True),
        sa.Column('audience', sa.String(length=255), nullable=True),
        sa.Column('brand_name', sa.String(length=100), nullable=True),
        sa.Column('brand_info', sa.Text(), nullable=True),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('language', sa.String(length=10), server_default='zh-CN', nullable=False),
        sa.Column('default_style', sa.String(length=32), server_default='news', nullable=False),
        sa.Column('default_format', sa.String(length=10), server_default='markdown', nullable=False),
        sa.Column('default_templates_json', sa.Text(), nullable=True),
        sa.Column('default_platform_ids_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
        sa.Column('owner_id', sa.BigInteger(), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
        sa.UniqueConstraint('owner_id', 'name', name='uq_projects_owner_id_name'),
        sa.UniqueConstraint('owner_id', 'slug', name='uq_projects_owner_id_slug'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_projects_status', 'projects', ['status'], unique=False)

    # prompt_templates
    op.create_table('prompt_templates',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=80), nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('capability', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('language', sa.String(length=10), server_default='zh-CN', nullable=False),
        sa.Column('project_id', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('system_prompt', sa.Text(), nullable=True),
        sa.Column('user_prompt', sa.Text(), nullable=False),
        sa.Column('variables_json', sa.Text(), nullable=True),
        sa.Column('output_format', sa.String(length=16), server_default='json', nullable=False),
        sa.Column('output_schema_json', sa.Text(), nullable=True),
        sa.Column('model_params_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
        sa.Column('is_system', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('updated_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_prompt_templates')),
        sa.UniqueConstraint('code', 'version', name='uq_prompt_templates_code_version'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_prompt_templates_kind_status_project_id', 'prompt_templates', ['kind', 'status', 'project_id'], unique=False)
    op.create_index('ix_prompt_templates_project_id', 'prompt_templates', ['project_id'], unique=False)

    # generation_batches
    op.create_table('generation_batches',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='queued', nullable=False),
        sa.Column('input_json', sa.Text(), nullable=True),
        sa.Column('template_id', sa.BigInteger(), nullable=False),
        sa.Column('template_version', sa.Integer(), nullable=False),
        sa.Column('requested_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('produced_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('task_total', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('task_done', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('task_failed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('error_summary', sa.String(length=1000), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_generation_batches')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_generation_batches_project_id_kind_status', 'generation_batches', ['project_id', 'kind', 'status'], unique=False)
    op.create_index('ix_generation_batches_status_created_at', 'generation_batches', ['status', 'created_at'], unique=False)
    op.create_index('ix_generation_batches_created_by_created_at', 'generation_batches', ['created_by', 'created_at'], unique=False)

    # keywords
    op.create_table('keywords',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=False),
        sa.Column('keyword', sa.String(length=120), nullable=False),
        sa.Column('normalized_keyword', sa.String(length=160), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('intent', sa.String(length=16), server_default='unknown', nullable=False),
        sa.Column('keyword_type', sa.String(length=16), server_default='core', nullable=False),
        sa.Column('difficulty', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=True),
        sa.Column('heat', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=True),
        sa.Column('score', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('seed', sa.String(length=120), nullable=True),
        sa.Column('source', sa.String(length=16), server_default='generated', nullable=False),
        sa.Column('batch_id', sa.BigInteger(), nullable=True),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('tags_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='candidate', nullable=False),
        sa.Column('adopted_by', sa.BigInteger(), nullable=True),
        sa.Column('adopted_at', sa.DateTime(), nullable=True),
        sa.Column('title_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('content_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_keywords')),
        sa.UniqueConstraint('project_id', 'normalized_keyword', name='uq_keywords_project_id_normalized_keyword'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_keywords_project_id_status_score', 'keywords', ['project_id', 'status', 'score'], unique=False)
    op.create_index('ix_keywords_batch_id', 'keywords', ['batch_id'], unique=False)
    op.create_index('ix_keywords_ai_task_id', 'keywords', ['ai_task_id'], unique=False)

    # titles
    op.create_table('titles',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=False),
        sa.Column('keyword_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('original_title', sa.String(length=200), nullable=True),
        sa.Column('style', sa.String(length=32), nullable=False),
        sa.Column('ai_score', sa.Numeric(precision=4, scale=2), nullable=True),
        sa.Column('manual_score', sa.Numeric(precision=4, scale=2), nullable=True),
        sa.Column('is_edited', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('source', sa.String(length=16), server_default='generated', nullable=False),
        sa.Column('batch_id', sa.BigInteger(), nullable=True),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='candidate', nullable=False),
        sa.Column('adopted_by', sa.BigInteger(), nullable=True),
        sa.Column('adopted_at', sa.DateTime(), nullable=True),
        sa.Column('content_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_titles')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_titles_keyword_id_status', 'titles', ['keyword_id', 'status'], unique=False)
    op.create_index('ix_titles_project_id_status_created_at', 'titles', ['project_id', 'status', 'created_at'], unique=False)
    op.create_index('ix_titles_batch_id', 'titles', ['batch_id'], unique=False)
    op.create_index('ix_titles_ai_task_id', 'titles', ['ai_task_id'], unique=False)

    # contents
    op.create_table('contents',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=False),
        sa.Column('title_id', sa.BigInteger(), nullable=True),
        sa.Column('keyword_id', sa.BigInteger(), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('format', sa.String(length=10), server_default='markdown', nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('style', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
        sa.Column('prev_status', sa.String(length=16), nullable=True),
        sa.Column('review_result', sa.String(length=10), nullable=True),
        sa.Column('current_version_id', sa.BigInteger(), nullable=True),
        sa.Column('version_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('outline_json', sa.Text(), nullable=True),
        sa.Column('body', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
        sa.Column('summary', sa.String(length=500), nullable=True),
        sa.Column('seo_title', sa.String(length=200), nullable=True),
        sa.Column('seo_description', sa.String(length=500), nullable=True),
        sa.Column('seo_keywords_json', sa.Text(), nullable=True),
        sa.Column('faq_json', sa.Text(), nullable=True),
        sa.Column('word_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cover_asset_id', sa.BigInteger(), nullable=True),
        sa.Column('template_id', sa.BigInteger(), nullable=True),
        sa.Column('generation_params_json', sa.Text(), nullable=True),
        sa.Column('batch_id', sa.BigInteger(), nullable=True),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('quality_score', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=True),
        sa.Column('risk_flags_json', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.BigInteger(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_note', sa.String(length=500), nullable=True),
        sa.Column('link_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('first_published_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('updated_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_contents')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_contents_project_id_status_updated_at', 'contents', ['project_id', 'status', 'updated_at'], unique=False)
    op.create_index('ix_contents_title_id', 'contents', ['title_id'], unique=False)
    op.create_index('ix_contents_keyword_id', 'contents', ['keyword_id'], unique=False)
    op.create_index('ix_contents_status_created_at', 'contents', ['status', 'created_at'], unique=False)
    op.create_index('ix_contents_batch_id', 'contents', ['batch_id'], unique=False)
    op.create_index('ix_contents_created_by', 'contents', ['created_by'], unique=False)

    # content_versions
    op.create_table('content_versions',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('content_id', sa.BigInteger(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
        sa.Column('content_hash', sa.CHAR(length=64), nullable=False),
        sa.Column('outline_json', sa.Text(), nullable=True),
        sa.Column('summary', sa.String(length=500), nullable=True),
        sa.Column('seo_title', sa.String(length=200), nullable=True),
        sa.Column('seo_description', sa.String(length=500), nullable=True),
        sa.Column('seo_keywords_json', sa.Text(), nullable=True),
        sa.Column('faq_json', sa.Text(), nullable=True),
        sa.Column('word_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('template_id', sa.BigInteger(), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('change_summary', sa.String(length=300), nullable=True),
        sa.Column('restored_from_version_id', sa.BigInteger(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.ForeignKeyConstraint(['content_id'], ['contents.id'], name='fk_content_versions_content_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_content_versions')),
        sa.UniqueConstraint('content_id', 'version_no', name='uq_content_versions_content_id_version_no'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_content_versions_ai_task_id', 'content_versions', ['ai_task_id'], unique=False)

    # media_assets
    op.create_table('media_assets',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=True),
        sa.Column('content_id', sa.BigInteger(), nullable=True),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('usage_type', sa.String(length=16), server_default='standalone', nullable=False),
        sa.Column('source', sa.String(length=16), server_default='generated', nullable=False),
        sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('negative_prompt', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('params_json', sa.Text(), nullable=True),
        sa.Column('reference_asset_ids_json', sa.Text(), nullable=True),
        sa.Column('upstream_task_id', sa.String(length=80), nullable=True),
        sa.Column('upstream_url', sa.String(length=1000), nullable=True),
        sa.Column('storage_key', sa.String(length=255), nullable=True),
        sa.Column('url', sa.String(length=1000), nullable=True),
        sa.Column('thumbnail_key', sa.String(length=255), nullable=True),
        sa.Column('thumbnail_url', sa.String(length=1000), nullable=True),
        sa.Column('mime_type', sa.String(length=80), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('file_hash', sa.CHAR(length=64), nullable=True),
        sa.Column('progress', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('transfer_attempts', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('next_transfer_at', sa.DateTime(), nullable=True),
        sa.Column('ready_at', sa.DateTime(), nullable=True),
        sa.Column('failed_at', sa.DateTime(), nullable=True),
        sa.Column('sort', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_media_assets')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_media_assets_project_id_kind_status_created_at', 'media_assets', ['project_id', 'kind', 'status', 'created_at'], unique=False)
    op.create_index('ix_media_assets_content_id_sort', 'media_assets', ['content_id', 'sort'], unique=False)
    op.create_index('ix_media_assets_ai_task_id', 'media_assets', ['ai_task_id'], unique=False)
    op.create_index('ix_media_assets_status_updated_at', 'media_assets', ['status', 'updated_at'], unique=False)
    op.create_index('ix_media_assets_status_next_transfer_at', 'media_assets', ['status', 'next_transfer_at'], unique=False)
    op.create_index('ix_media_assets_upstream_task_id', 'media_assets', ['upstream_task_id'], unique=False)
    op.create_index('ix_media_assets_kind_ready_at', 'media_assets', ['kind', 'ready_at'], unique=False)
    op.create_index('ix_media_assets_kind_failed_at', 'media_assets', ['kind', 'failed_at'], unique=False)
    op.create_index('ix_media_assets_created_by_created_at', 'media_assets', ['created_by', 'created_at'], unique=False)

    # ai_tasks
    op.create_table('ai_tasks',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=True),
        sa.Column('capability', sa.String(length=20), nullable=False),
        sa.Column('operation', sa.String(length=32), nullable=False),
        sa.Column('input_json', sa.Text(), nullable=True),
        sa.Column('protocol', sa.String(length=24), nullable=True),
        sa.Column('trigger_type', sa.String(length=16), server_default='user', nullable=False),
        sa.Column('target_type', sa.String(length=32), nullable=True),
        sa.Column('target_id', sa.BigInteger(), nullable=True),
        sa.Column('batch_id', sa.BigInteger(), nullable=True),
        sa.Column('root_task_id', sa.BigInteger(), nullable=True),
        sa.Column('parent_task_id', sa.BigInteger(), nullable=True),
        sa.Column('route_id', sa.BigInteger(), nullable=True),
        sa.Column('candidate_index', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('attempt', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('1'), nullable=False),
        sa.Column('segment_index', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=False),
        sa.Column('template_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='queued', nullable=False),
        sa.Column('pause_count', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('upstream_task_id', sa.String(length=80), nullable=True),
        sa.Column('request_payload_json', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
        sa.Column('response_meta_json', sa.Text(), nullable=True),
        sa.Column('output_excerpt', sa.String(length=2000), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cache_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('quota_reserved', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('quota_estimated', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('quota_actual', sa.BigInteger(), nullable=True),
        sa.Column('cost_cny', sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column('reconciled_at', sa.DateTime(), nullable=True),
        sa.Column('usage_log_type', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=True),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('upstream_latency_ms', sa.Integer(), nullable=True),
        sa.Column('progress', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('poll_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('next_poll_at', sa.DateTime(), nullable=True),
        sa.Column('deadline_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=64), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_tasks')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_ai_tasks_status_next_poll_at', 'ai_tasks', ['status', 'next_poll_at'], unique=False)
    op.create_index('ix_ai_tasks_status_heartbeat_at', 'ai_tasks', ['status', 'heartbeat_at'], unique=False)
    op.create_index('ix_ai_tasks_root_task_id', 'ai_tasks', ['root_task_id'], unique=False)
    op.create_index('ix_ai_tasks_request_id', 'ai_tasks', ['request_id'], unique=False)
    op.create_index('ix_ai_tasks_upstream_task_id', 'ai_tasks', ['upstream_task_id'], unique=False)
    op.create_index('ix_ai_tasks_batch_id', 'ai_tasks', ['batch_id'], unique=False)
    op.create_index('ix_ai_tasks_target_type_target_id_operation_status', 'ai_tasks', ['target_type', 'target_id', 'operation', 'status'], unique=False)
    op.create_index('ix_ai_tasks_project_id_capability_created_at', 'ai_tasks', ['project_id', 'capability', 'created_at'], unique=False)
    op.create_index('ix_ai_tasks_capability_model_created_at', 'ai_tasks', ['capability', 'model', 'created_at'], unique=False)
    op.create_index('ix_ai_tasks_created_at', 'ai_tasks', ['created_at'], unique=False)

    # ai_models
    op.create_table('ai_models',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('model_id', sa.String(length=120), nullable=False),
        sa.Column('owned_by', sa.String(length=80), nullable=True),
        sa.Column('vendor_id', sa.Integer(), nullable=True),
        sa.Column('vendor_name', sa.String(length=80), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('tags_json', sa.Text(), nullable=True),
        sa.Column('icon', sa.String(length=500), nullable=True),
        sa.Column('cover_url', sa.String(length=500), nullable=True),
        sa.Column('supported_endpoint_types_json', sa.Text(), nullable=True),
        sa.Column('modalities_json', sa.Text(), nullable=True),
        sa.Column('quota_type', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('model_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('model_price', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('completion_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('cache_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('create_cache_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('enable_groups_json', sa.Text(), nullable=True),
        sa.Column('billing_mode', sa.String(length=32), nullable=True),
        sa.Column('billing_expr', sa.String(length=255), nullable=True),
        sa.Column('model_price_type', sa.String(length=32), nullable=True),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('is_available', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_health_status', sa.String(length=16), server_default='unknown', nullable=False),
        sa.Column('last_health_at', sa.DateTime(), nullable=True),
        sa.Column('last_health_latency_ms', sa.Integer(), nullable=True),
        sa.Column('raw_pricing_json', sa.Text(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_models')),
        sa.UniqueConstraint('model_id', name='uq_ai_models_model_id'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_ai_models_is_available_sort_order', 'ai_models', ['is_available', 'sort_order'], unique=False)
    op.create_index('ix_ai_models_vendor_id', 'ai_models', ['vendor_id'], unique=False)

    # capability_routes
    op.create_table('capability_routes',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('capability', sa.String(length=20), nullable=False),
        sa.Column('project_id', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('protocol', sa.String(length=24), nullable=False),
        sa.Column('primary_model', sa.String(length=120), nullable=False),
        sa.Column('fallback_models_json', sa.Text(), nullable=True),
        sa.Column('params_json', sa.Text(), nullable=True),
        sa.Column('timeout_seconds', sa.Integer(), nullable=True),
        sa.Column('max_attempts', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('3'), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_capability_routes')),
        sa.UniqueConstraint('capability', 'project_id', name='uq_capability_routes_capability_project_id'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )

    # ai_usage_logs
    op.create_table('ai_usage_logs',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('entry_hash', sa.CHAR(length=64), nullable=False),
        sa.Column('upstream_log_id', sa.BigInteger(), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('log_type', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=False),
        sa.Column('model_name', sa.String(length=120), nullable=True),
        sa.Column('group_name', sa.String(length=50), nullable=True),
        sa.Column('quota', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cache_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('group_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('model_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('completion_ratio', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('request_path', sa.String(length=255), nullable=True),
        sa.Column('upstream_task_id', sa.String(length=80), nullable=True),
        sa.Column('upstream_created_at', sa.DateTime(), nullable=True),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('matched_at', sa.DateTime(), nullable=True),
        sa.Column('pulled_at', sa.DateTime(), nullable=False),
        sa.Column('raw_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_usage_logs')),
        sa.UniqueConstraint('entry_hash', name='uq_ai_usage_logs_entry_hash'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_ai_usage_logs_request_id_log_type', 'ai_usage_logs', ['request_id', 'log_type'], unique=False)
    op.create_index('ix_ai_usage_logs_upstream_task_id', 'ai_usage_logs', ['upstream_task_id'], unique=False)
    op.create_index('ix_ai_usage_logs_ai_task_id', 'ai_usage_logs', ['ai_task_id'], unique=False)
    op.create_index('ix_ai_usage_logs_model_name_pulled_at', 'ai_usage_logs', ['model_name', 'pulled_at'], unique=False)
    op.create_index('ix_ai_usage_logs_matched_at', 'ai_usage_logs', ['matched_at'], unique=False)

    # publish_platforms
    op.create_table('publish_platforms',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('name_en', sa.String(length=80), nullable=False),
        sa.Column('icon', sa.String(length=500), nullable=True),
        sa.Column('home_url', sa.String(length=255), nullable=True),
        sa.Column('url_patterns_json', sa.Text(), nullable=True),
        sa.Column('deleted_markers_json', sa.Text(), nullable=True),
        sa.Column('redirect_markers_json', sa.Text(), nullable=True),
        sa.Column('fetch_config_json', sa.Text(), nullable=True),
        sa.Column('is_system', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('sort', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_publish_platforms')),
        sa.UniqueConstraint('code', name='uq_publish_platforms_code'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_publish_platforms_is_active_sort', 'publish_platforms', ['is_active', 'sort'], unique=False)

    # publish_links
    op.create_table('publish_links',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=False),
        sa.Column('content_id', sa.BigInteger(), nullable=False),
        sa.Column('platform_id', sa.BigInteger(), nullable=False),
        sa.Column('url', sa.String(length=1000), nullable=False),
        sa.Column('normalized_url', sa.String(length=1000), nullable=False),
        sa.Column('url_hash', sa.CHAR(length=64), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('publish_account', sa.String(length=100), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=False),
        sa.Column('backfilled_by', sa.BigInteger(), nullable=False),
        sa.Column('title_snapshot', sa.String(length=300), nullable=False),
        sa.Column('alive_status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('alive_changed_at', sa.DateTime(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('next_check_at', sa.DateTime(), nullable=True),
        sa.Column('check_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('consecutive_unknown', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('consecutive_suspected', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('last_http_status', sa.Integer(), nullable=True),
        sa.Column('baseline_title', sa.String(length=300), nullable=True),
        sa.Column('baseline_simhash', sa.BigInteger(), nullable=True),
        sa.Column('baseline_excerpt', sa.String(length=1000), nullable=True),
        sa.Column('baseline_captured_at', sa.DateTime(), nullable=True),
        sa.Column('seo_status_json', sa.Text(), nullable=True),
        sa.Column('geo_status_json', sa.Text(), nullable=True),
        sa.Column('seo_indexed_any', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('geo_cited_any', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('first_indexed_at', sa.DateTime(), nullable=True),
        sa.Column('first_cited_at', sa.DateTime(), nullable=True),
        sa.Column('last_index_checked_at', sa.DateTime(), nullable=True),
        sa.Column('next_index_check_at', sa.DateTime(), nullable=True),
        sa.Column('index_check_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('index_checks_done', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('is_monitoring', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_publish_links')),
        sa.UniqueConstraint('url_hash', name='uq_publish_links_url_hash'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_publish_links_content_id', 'publish_links', ['content_id'], unique=False)
    op.create_index('ix_publish_links_project_id_platform_id_published_at', 'publish_links', ['project_id', 'platform_id', 'published_at'], unique=False)
    op.create_index('ix_publish_links_platform_id_alive_status', 'publish_links', ['platform_id', 'alive_status'], unique=False)
    op.create_index('ix_publish_links_is_monitoring_next_check_at', 'publish_links', ['is_monitoring', 'next_check_at'], unique=False)
    op.create_index('ix_publish_links_is_monitoring_next_index_check_at', 'publish_links', ['is_monitoring', 'next_index_check_at'], unique=False)
    op.create_index('ix_publish_links_alive_status', 'publish_links', ['alive_status'], unique=False)
    op.create_index('ix_publish_links_published_at', 'publish_links', ['published_at'], unique=False)

    # link_checks
    op.create_table('link_checks',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('link_id', sa.BigInteger(), nullable=False),
        sa.Column('check_type', sa.String(length=16), nullable=False),
        sa.Column('result_status', sa.String(length=20), nullable=False),
        sa.Column('previous_status', sa.String(length=20), nullable=False),
        sa.Column('applied_status', sa.String(length=20), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('final_url', sa.String(length=1000), nullable=True),
        sa.Column('redirect_count', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), server_default=sa.text('0'), nullable=False),
        sa.Column('matched_rule', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=True),
        sa.Column('simhash', sa.BigInteger(), nullable=True),
        sa.Column('hamming_distance', sa.SmallInteger().with_variant(mysql.TINYINT(), 'mysql'), nullable=True),
        sa.Column('response_bytes', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('evidence_json', sa.Text(), nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('triggered_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.ForeignKeyConstraint(['link_id'], ['publish_links.id'], name='fk_link_checks_link_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_link_checks')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_link_checks_link_id_checked_at', 'link_checks', ['link_id', 'checked_at'], unique=False)
    op.create_index('ix_link_checks_checked_at', 'link_checks', ['checked_at'], unique=False)
    op.create_index('ix_link_checks_result_status_checked_at', 'link_checks', ['result_status', 'checked_at'], unique=False)

    # index_checks
    op.create_table('index_checks',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('link_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('engine', sa.String(length=32), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('check_type', sa.String(length=16), nullable=False),
        sa.Column('result_status', sa.String(length=16), nullable=False),
        sa.Column('previous_status', sa.String(length=16), nullable=False),
        sa.Column('match_mode', sa.String(length=16), nullable=False),
        sa.Column('query_text', sa.String(length=500), nullable=True),
        sa.Column('ai_task_id', sa.BigInteger(), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('evidence_title', sa.String(length=300), nullable=True),
        sa.Column('evidence_snippet', sa.String(length=1000), nullable=True),
        sa.Column('evidence_url', sa.String(length=1000), nullable=True),
        sa.Column('evidence_json', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column('duration_ms', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('triggered_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.ForeignKeyConstraint(['link_id'], ['publish_links.id'], name='fk_index_checks_link_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_index_checks')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_index_checks_link_id_kind_engine_checked_at', 'index_checks', ['link_id', 'kind', 'engine', 'checked_at'], unique=False)
    op.create_index('ix_index_checks_checked_at', 'index_checks', ['checked_at'], unique=False)
    op.create_index('ix_index_checks_kind_result_status_checked_at', 'index_checks', ['kind', 'result_status', 'checked_at'], unique=False)
    op.create_index('ix_index_checks_ai_task_id', 'index_checks', ['ai_task_id'], unique=False)

    # alerts
    op.create_table('alerts',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('alert_type', sa.String(length=40), nullable=False),
        sa.Column('severity', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='open', nullable=False),
        sa.Column('project_id', sa.BigInteger(), nullable=True),
        sa.Column('target_type', sa.String(length=32), nullable=True),
        sa.Column('target_id', sa.BigInteger(), nullable=True),
        sa.Column('target_key', sa.String(length=160), server_default='', nullable=False),
        sa.Column('dedupe_key', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('message', sa.String(length=1000), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('first_triggered_at', sa.DateTime(), nullable=False),
        sa.Column('last_triggered_at', sa.DateTime(), nullable=False),
        sa.Column('trigger_count', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('acknowledged_by', sa.BigInteger(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_by', sa.BigInteger(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resolution_note', sa.String(length=500), nullable=True),
        sa.Column('notified_channels_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_alerts')),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_alerts_status_severity_last_triggered_at', 'alerts', ['status', 'severity', 'last_triggered_at'], unique=False)
    op.create_index('ix_alerts_dedupe_key_status', 'alerts', ['dedupe_key', 'status'], unique=False, mysql_length={'dedupe_key': 191})
    op.create_index('ix_alerts_project_id_status', 'alerts', ['project_id', 'status'], unique=False)
    op.create_index('ix_alerts_alert_type_created_at', 'alerts', ['alert_type', 'created_at'], unique=False)
    op.create_index('ix_alerts_target_type_target_key', 'alerts', ['target_type', 'target_key'], unique=False)

    # daily_stats
    op.create_table('daily_stats',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('stat_date', sa.Date(), nullable=False),
        sa.Column('project_id', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('dimension', sa.String(length=16), nullable=False),
        sa.Column('dimension_key', sa.String(length=120), server_default='', nullable=False),
        sa.Column('keywords_created', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('keywords_adopted', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('titles_created', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('titles_adopted', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('contents_created', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('contents_approved', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('contents_published', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_backfilled', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_checked', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_deleted', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_changed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_restored', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_alive_snapshot', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('links_total_snapshot', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('seo_checks', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('seo_newly_indexed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('seo_indexed_snapshot', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('geo_checks', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('geo_newly_cited', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('geo_cited_snapshot', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('index_hours_sum', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('index_hours_links', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('ai_calls', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('ai_succeeded', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('ai_failed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('ai_duration_ms_sum', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('tasks_succeeded', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('tasks_failed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('task_duration_ms_sum', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('prompt_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('completion_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('quota_estimated', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('quota_actual', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('quota_reconciled_calls', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('cost_cny', sa.Numeric(precision=14, scale=6), server_default=sa.text('0'), nullable=False),
        sa.Column('images_generated', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('videos_generated', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('media_failed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('alerts_opened', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('alerts_resolved', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('extra_json', sa.Text(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=updated_at_default, nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_daily_stats')),
        sa.UniqueConstraint('stat_date', 'project_id', 'dimension', 'dimension_key', name='uq_daily_stats_stat_date_project_id_dimension_dimension_key'),
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
        mysql_engine='InnoDB'
    )
    op.create_index('ix_daily_stats_stat_date_dimension', 'daily_stats', ['stat_date', 'dimension'], unique=False)
    op.create_index('ix_daily_stats_project_id_stat_date', 'daily_stats', ['project_id', 'stat_date'], unique=False)


def downgrade() -> None:
    # 删表即删除其索引与外键；逆序保证先删引用方（admins / admin_group_permissions / content_versions / link_checks / index_checks）
    for table in reversed(TABLES):
        op.drop_table(table)
