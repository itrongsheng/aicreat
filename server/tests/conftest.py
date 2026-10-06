"""pytest 公共夹具（docs/02 §2.10）：SQLite 测试会话、Redis db 15、应用与 TestClient、各用户组的用户与令牌。

- 每个测试一份全新的内存 SQLite（``StaticPool``），``SessionLocal`` 重新绑定到它，因此依赖覆盖的 ``get_db``、
  审计中间件与 lifespan 启动引导访问的是同一个库；建表后执行 ``ensure_rbac_seed`` 与 ``ensure_default_settings``。
- Redis 固定 db 15，每个测试前后 ``FLUSHDB``。
- bcrypt 在测试中降到最低成本（rounds=4），只为提速。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 须在导入 app.core 之前设置（已显式设置时不覆盖）
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ADMIN_JWT_SECRET", "aicreat-test-only-secret-0123456789abcdef")

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:  # 使 seeds 包可导入
    sys.path.insert(0, str(SERVER_DIR))

import warnings  # noqa: E402

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")

import bcrypt  # noqa: E402

_original_gensalt = bcrypt.gensalt


def _fast_gensalt(rounds: int = 4, prefix: bytes = b"2b") -> bytes:
    return _original_gensalt(rounds=4, prefix=prefix)


bcrypt.gensalt = _fast_gensalt  # type: ignore[assignment]

import itertools  # noqa: E402
from collections.abc import Iterable, Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core import database  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, get_db, make_engine  # noqa: E402
from app.core.redis import redis_client  # noqa: E402
from app.core.security import create_token, hash_password  # noqa: E402
from app.core.storage import reset_storage  # noqa: E402
from app.models import Admin, AdminGroup  # noqa: E402
from app.services import admin_rbac_service, settings_service  # noqa: E402

DEFAULT_PASSWORD = "Passw0rd2026"
API = "/api/v1"
ADMIN_API = f"{API}/admin"


def redis_is_db15() -> bool:
    try:
        return int(redis_client.connection_pool.connection_kwargs.get("db", 0)) == 15 and bool(redis_client.ping())
    except Exception:  # noqa: BLE001
        return False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("filterwarnings", "ignore:Using `httpx` with `starlette.testclient` is deprecated")
    config.addinivalue_line("filterwarnings", "ignore::jwt.warnings.InsecureKeyLengthWarning")


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    """每个测试前后清空 Redis db 15（只在确实连到 db 15 时执行）。"""
    on15 = redis_is_db15()
    if on15:
        redis_client.flushdb()
    yield
    if on15:
        redis_client.flushdb()


@pytest.fixture(autouse=True)
def _zhiqi_mock_fast(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """测试中 MockZhiqiClient 不 sleep 模拟延迟（latency_ms 照常非 0），并在前后重置 ``get_client()`` 单例。"""
    from app.core.zhiqi import mock as zhiqi_mock
    from app.core.zhiqi.client import reset_client

    monkeypatch.setattr(zhiqi_mock, "SIMULATE_LATENCY", False)
    reset_client()
    yield
    reset_client()


@pytest.fixture
def redis_required() -> None:
    if not redis_is_db15():
        pytest.skip("需要 Redis db 15（REDIS_URL=redis://127.0.0.1:6379/15）")


# =====================================================================
# 数据库
# =====================================================================


@pytest.fixture
def engine() -> Iterator[Engine]:
    """全新的内存 SQLite；``SessionLocal`` 在测试期间绑定到它。"""
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    previous = database.SessionLocal.kw.get("bind")
    database.SessionLocal.configure(bind=eng)
    try:
        yield eng
    finally:
        database.SessionLocal.configure(bind=previous)
        eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """测试用会话：已执行 ``ensure_rbac_seed`` 与 ``ensure_default_settings``。"""
    session = database.SessionLocal()
    admin_rbac_service.ensure_rbac_seed(session)
    settings_service.ensure_default_settings(session)
    try:
        yield session
    finally:
        session.close()


# =====================================================================
# 应用
# =====================================================================


@pytest.fixture
def app(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    from app.main import create_app

    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path / "storage"))
    reset_storage()
    application = create_app()

    def _get_test_db() -> Iterator[Session]:
        session = database.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = _get_test_db
    try:
        yield application
    finally:
        application.dependency_overrides.clear()
        reset_storage()


@pytest.fixture
def client(app: FastAPI, db: Session) -> Iterator[TestClient]:
    """TestClient（以上下文管理器运行，触发 lifespan 启动引导）。"""
    with TestClient(app) as test_client:
        yield test_client


# =====================================================================
# 用户与令牌
# =====================================================================


@dataclass
class User:
    admin: Admin
    password: str
    token: str

    @property
    def id(self) -> int:
        return self.admin.id

    @property
    def username(self) -> str:
        return self.admin.username

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class UserFactory:
    """在指定用户组下创建用户并签发令牌（``create_token``，与登录签发的令牌等价）。"""

    _seq = itertools.count(1)

    def __init__(self, db: Session) -> None:
        self.db = db

    def group(self, code: str) -> AdminGroup:
        group = self.db.scalar(select(AdminGroup).where(AdminGroup.code == code))
        assert group is not None, f"用户组不存在: {code}"
        return group

    def custom_group(self, name: str, codes: Iterable[str] = (), *, data_scope: str = "own", is_active: bool = True) -> AdminGroup:
        from app.schemas.admin_rbac import GroupCreate

        item, _ = admin_rbac_service.create_group(
            self.db, SimpleNamespace(id=None), GroupCreate(name=name, data_scope=data_scope, is_active=is_active)  # type: ignore[arg-type]
        )
        if codes:
            admin_rbac_service.save_group_permissions(self.db, item["id"], list(codes))
        return self.db.get(AdminGroup, item["id"])  # type: ignore[return-value]

    def create(
        self,
        group: str | AdminGroup = "operator",
        *,
        username: str | None = None,
        password: str = DEFAULT_PASSWORD,
        display_name: str | None = None,
        is_active: bool = True,
    ) -> User:
        grp = group if isinstance(group, AdminGroup) else self.group(group)
        admin = Admin(
            username=username or f"user{next(self._seq):04d}",
            password_hash=hash_password(password),
            display_name=display_name,
            group_id=grp.id,
            is_active=is_active,
            token_version=1,
        )
        self.db.add(admin)
        self.db.commit()
        return User(admin=admin, password=password, token=create_token(admin.id, admin.token_version))

    def token(self, admin: Admin) -> str:
        self.db.refresh(admin)
        return create_token(admin.id, admin.token_version)


@pytest.fixture
def users(db: Session) -> UserFactory:
    return UserFactory(db)


@pytest.fixture
def super_admin(db: Session) -> User:
    """seed 创建的默认超管（``SEED_ADMIN_USERNAME`` / ``SEED_ADMIN_PASSWORD``）。"""
    from seeds.seed import seed_admin

    admin = seed_admin(db)
    return User(admin=admin, password=settings.seed_admin_password, token=create_token(admin.id, admin.token_version))


@pytest.fixture
def operator(users: UserFactory) -> User:
    return users.create("operator", username="operator01", display_name="运营小王")


@pytest.fixture
def reviewer(users: UserFactory) -> User:
    return users.create("reviewer", username="reviewer01", display_name="审核小张")


@pytest.fixture
def read_only(users: UserFactory) -> User:
    return users.create("read_only", username="readonly01", display_name="只读小李")


@pytest.fixture
def custom_user(users: UserFactory) -> User:
    """自定义组「媒体设计」（own）：``dashboard.view`` + 图片生成（自动补齐依赖）。"""
    group = users.custom_group("媒体设计", ["dashboard.view", "media.images.generate"])
    return users.create(group, username="designer01")


# =====================================================================
# 断言助手
# =====================================================================


def ok_data(response: Any) -> Any:
    """断言 HTTP 200 且 ``code=0``，返回 ``data``。"""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def login(client: TestClient, username: str, password: str) -> Any:
    return client.post(f"{ADMIN_API}/auth/login", json={"username": username, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
