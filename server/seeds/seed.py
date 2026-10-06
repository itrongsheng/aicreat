"""幂等 upsert 初始化数据（docs/README 实施顺序第 1 步、docs/06「初始化数据」、docs/07 §3.6、docs/13 §14）。

在 ``server/`` 目录执行 ``python seeds/seed.py``（依赖 ``pip install -e .`` 使 ``app`` 可导入）；重复执行不报错、不产生重复数据：

- 默认超级管理员 ``SEED_ADMIN_USERNAME`` / ``SEED_ADMIN_PASSWORD``（组 ``super_admin``、``created_by=NULL``、``token_version=1``）；
  账号已存在时**不覆盖**密码、不改组、不改状态；
- 示例项目（``owner_id`` = 默认超管）；已存在时不改负责人；
- 系统 Prompt 模板（第 4 步）、默认发布平台（第 8 步）由后续阶段在 ``SEED_STEPS`` 中追加各自的 ``seed_*`` 函数。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 以脚本运行时 sys.path[0] 是 seeds/；补上 server/ 以便未 pip install -e 时也能导入 app
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.admin_permissions import SUPER_ADMIN_GROUP_CODE  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import Admin, AdminGroup, Project  # noqa: E402
from app.services.admin_rbac_service import ensure_rbac_seed  # noqa: E402

logger = logging.getLogger("seeds")

SEED_ADMIN_DISPLAY_NAME = "超级管理员"

EXAMPLE_PROJECT: dict[str, Any] = {
    "name": "示例项目",
    "slug": "example",
    "industry": "企业服务",
    "audience": "希望用 AI 批量产出 SEO / GEO 内容的运营与内容团队",
    "brand_name": "aicreat",
    "brand_info": "aicreat 是 AI 内容生成与效果监控平台：关键词 → 标题 → 文章生成，发布后回填链接并监控删除与收录。语气专业、客观，避免夸大宣传。",
    "description": "seed 创建的示例项目，可直接用于体验关键词、标题、内容生成与链接监控流程",
    "language": "zh-CN",
    "default_style": "news",
    "default_format": "markdown",
}


def _super_admin_group(db: Session) -> AdminGroup:
    group = db.scalar(select(AdminGroup).where(AdminGroup.code == SUPER_ADMIN_GROUP_CODE))
    if group is None:                                  # 迁移 0002 未执行或被删：先补齐权限码与系统组
        ensure_rbac_seed(db)
        group = db.scalar(select(AdminGroup).where(AdminGroup.code == SUPER_ADMIN_GROUP_CODE))
    if group is None:  # pragma: no cover
        raise RuntimeError("super_admin 用户组不存在")
    return group


def seed_admin(db: Session) -> Admin:
    """默认超级管理员：不存在则创建；已存在时不覆盖密码、不改组、不改状态。"""
    username = settings.seed_admin_username.strip()
    if not username:
        raise RuntimeError("SEED_ADMIN_USERNAME 不能为空")
    admin = db.scalar(select(Admin).where(func.lower(Admin.username) == username.lower()))
    if admin is not None:
        logger.info("超级管理员 %s 已存在，跳过", admin.username)
        return admin
    group = _super_admin_group(db)
    admin = Admin(
        username=username,
        password_hash=hash_password(settings.seed_admin_password),
        display_name=SEED_ADMIN_DISPLAY_NAME,
        group_id=group.id,
        is_active=True,
        token_version=1,
        created_by=None,
    )
    db.add(admin)
    db.commit()
    logger.info("已创建超级管理员 %s", username)
    return admin


def seed_example_project(db: Session, owner: Admin) -> Project:
    """示例项目（``owner_id`` = 默认超管）；按 slug 识别，已存在时不改负责人与内容。"""
    project = db.scalar(select(Project).where(Project.slug == EXAMPLE_PROJECT["slug"]).order_by(Project.id).limit(1))
    if project is not None:
        logger.info("示例项目已存在（id=%s），跳过", project.id)
        return project
    project = Project(**EXAMPLE_PROJECT, status="active", owner_id=owner.id, created_by=owner.id)
    db.add(project)
    db.commit()
    logger.info("已创建示例项目 id=%s", project.id)
    return project


def _seed_admin_and_project(db: Session, context: dict[str, Any]) -> None:
    context["admin"] = seed_admin(db)
    context["project"] = seed_example_project(db, context["admin"])


# 依次执行的 seed 步骤 (名称, 函数(db, context))；后续阶段追加系统 Prompt 模板与默认发布平台
SEED_STEPS: list[tuple[str, Callable[[Session, dict[str, Any]], None]]] = [
    ("admin_and_example_project", _seed_admin_and_project),
]


def run_seed(db: Session) -> dict[str, Any]:
    """按 ``SEED_STEPS`` 顺序执行全部 seed（幂等），返回上下文（``admin``、``project`` …）。"""
    context: dict[str, Any] = {}
    for name, step in SEED_STEPS:
        logger.info("seed: %s", name)
        step(db, context)
    return context


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with SessionLocal() as db:
        try:
            run_seed(db)
        except Exception:
            db.rollback()
            logger.exception("seed 失败")
            return 1
    logger.info("seed 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
