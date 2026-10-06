"""RBAC 后端测试（docs/07-admin-rbac.md §11）：权限注册表、require_permission、令牌失效事件、登录频控、
安全规则、权限保存与补齐、审计中间件（含路由遍历）、启动 seed、用户组数据范围，以及应用骨架（health / site-info /
settings / media / 请求 ID / 分页 / seed 脚本）。"""

from __future__ import annotations

import json
import time
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core import admin_permissions as ap
from app.core.config import settings
from app.core.ratelimit import admin_login_key
from app.core.redis import redis_client
from app.models import Admin, AdminGroup, AdminGroupPermission, AdminOperationLog, AdminPermission, Project
from app.services import admin_rbac_service as rbac
from conftest import ADMIN_API, DEFAULT_PASSWORD, User, UserFactory, bearer, login, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")


# =====================================================================
# 夹具与助手
# =====================================================================


@pytest.fixture
def sec_admin(users: UserFactory) -> User:
    """非超管的安全管理员（全部 security.* 权限，all 范围），用于验证「最后一个超管」规则对他人同样生效。"""
    codes = [c for c in ap.PERMISSION_CODES if c.startswith("security.")]
    group = users.custom_group("安全管理员", codes, data_scope="all")
    return users.create(group, username="secadmin")


def _err(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == status, body
    return body


def _logs(db: Session, **filters: Any) -> list[AdminOperationLog]:
    stmt = select(AdminOperationLog).order_by(AdminOperationLog.id)
    for key, value in filters.items():
        stmt = stmt.where(getattr(AdminOperationLog, key) == value)
    db.expire_all()
    return list(db.scalars(stmt).all())


def _group_codes(db: Session, code: str) -> set[str]:
    db.expire_all()
    group = db.scalar(select(AdminGroup).where(AdminGroup.code == code))
    return set(rbac.group_permission_codes(db, group))


def _permission_dependencies(route: Any) -> list[str]:
    """路由依赖树中的 require_permission 权限码。"""
    found: list[str] = []

    def walk(dependant: Any) -> None:
        for dep in dependant.dependencies:
            code = getattr(dep.call, "permission_code", None)
            if code:
                found.append(code)
            walk(dep)

    walk(route.dependant)
    return found


# =====================================================================
# 1. 权限注册表一致性
# =====================================================================


def test_permission_registry_consistency() -> None:
    codes = [p.code for p in ap.PERMISSIONS]
    assert len(codes) == len(set(codes)) == 90
    assert ap.PERMISSION_CODES == set(codes)
    menus = {p.code for p in ap.PERMISSIONS if p.type == "menu"}
    for spec in ap.PERMISSIONS:
        assert spec.module in rbac.MODULE_NAMES
        if spec.type == "action":
            assert spec.parent_code in menus
            assert spec.parent_code.rsplit(".", 1)[0] == spec.code.rsplit(".", 1)[0]
        else:
            assert spec.code.endswith(".view") and spec.parent_code is None
    assert set(ap.DEFAULT_GROUP_PERMISSIONS) <= ap.SYSTEM_GROUP_CODES
    assert "super_admin" not in ap.DEFAULT_GROUP_PERMISSIONS
    for values in ap.DEFAULT_GROUP_PERMISSIONS.values():
        assert values <= ap.PERMISSION_CODES
        assert rbac.expand_codes(values) == values         # 默认组已包含全部依赖（§4.3）
    for key, values in ap.PERMISSION_DEPENDENCIES.items():
        assert key in ap.PERMISSION_CODES and values <= ap.PERMISSION_CODES
    tree_codes: list[str] = []
    for module in rbac.permission_tree():
        assert module["name"] == rbac.MODULE_NAMES[module["module"]]
        for menu in module["items"]:
            tree_codes.append(menu["code"])
            tree_codes.extend(child["code"] for child in menu["children"])
    assert sorted(tree_codes) == sorted(codes)
    flat = rbac.permission_list()
    assert [item["sort"] for item in flat] == sorted(item["sort"] for item in flat)
    assert set(flat[0]) == {"code", "module", "name", "type", "parent_code", "sort"}


def test_system_groups_seeded_with_defaults(db: Session) -> None:
    groups = {g.code: g for g in db.scalars(select(AdminGroup)).all()}
    assert {code: g.data_scope for code, g in groups.items()} == {
        "super_admin": "all", "operator": "own", "reviewer": "all", "read_only": "all",
    }
    assert all(g.is_system and g.is_active for g in groups.values())
    assert db.scalar(select(func.count(AdminPermission.id))) == 90
    assert _group_codes(db, "super_admin") == ap.PERMISSION_CODES
    for code in ("operator", "reviewer", "read_only"):
        assert _group_codes(db, code) == ap.DEFAULT_GROUP_PERMISSIONS[code]


def test_permission_endpoints(client: TestClient, super_admin: User, operator: User) -> None:
    flat = ok_data(client.get(f"{ADMIN_API}/admin-permissions", headers=super_admin.headers))
    assert len(flat) == 90
    tree = ok_data(client.get(f"{ADMIN_API}/admin-permissions/tree", headers=super_admin.headers))
    content = next(m for m in tree if m["module"] == "content")
    keywords = next(i for i in content["items"] if i["code"] == "content.keywords.view")
    assert keywords["type"] == "menu"
    assert {"code": "content.keywords.generate", "name": "生成关键词", "type": "action"} in keywords["children"]
    _err(client.get(f"{ADMIN_API}/admin-permissions/tree", headers=operator.headers), 403)


# =====================================================================
# 2. require_permission
# =====================================================================


def test_require_permission_rejects_unknown_code() -> None:
    with pytest.raises(ValueError, match="未知权限码"):
        require_permission("content.keywords.publish")


@pytest.mark.parametrize(
    ("role", "method", "path", "status"),
    [
        ("operator", "GET", "/admins", 403),
        ("operator", "GET", "/admin-groups", 403),
        ("operator", "GET", "/settings", 403),
        ("operator", "GET", "/settings/runtime", 200),
        ("operator", "GET", "/projects/owner-options", 200),
        ("operator", "GET", "/admin-operation-logs", 403),
        ("reviewer", "GET", "/projects/owner-options", 200),
        ("reviewer", "GET", "/admin-permissions", 403),
        ("reviewer", "GET", "/settings/generation_config", 403),
        ("read_only", "GET", "/projects/owner-options", 200),
        ("read_only", "GET", "/settings", 403),
        ("read_only", "GET", "/admins", 403),
        ("custom_user", "GET", "/projects/owner-options", 403),
        ("custom_user", "GET", "/settings/runtime", 200),
        ("super_admin", "GET", "/admins", 200),
        ("super_admin", "GET", "/admin-operation-logs", 200),
        ("super_admin", "GET", "/settings", 200),
    ],
)
def test_role_access_matrix(request: pytest.FixtureRequest, client: TestClient, role: str, method: str, path: str, status: int) -> None:
    user: User = request.getfixturevalue(role)
    response = client.request(method, f"{ADMIN_API}{path}", headers=user.headers)
    assert response.status_code == status, response.text
    if status == 403:
        body = response.json()
        assert body["message"] == "无权执行此操作"
        assert body["data"]["permission"] in ap.PERMISSION_CODES


def test_has_permission_for_documented_samples(db: Session, operator: User, reviewer: User, read_only: User) -> None:
    """07 §11 的抽样（业务接口随后续阶段挂载，这里先在权限层验证同一判定）。"""
    assert not rbac.has_permission(db, reviewer.admin, "content.keywords.generate")      # POST /admin/keywords/generate → 403
    assert rbac.has_permission(db, reviewer.admin, "content.contents.review")            # POST /admin/contents/{id}/approve → 200
    assert rbac.has_permission(db, operator.admin, "content.keywords.generate")
    assert not rbac.has_permission(db, operator.admin, "content.contents.review")
    assert not rbac.has_permission(db, operator.admin, "publish.platforms.create")
    assert rbac.has_permission(db, read_only.admin, "stats.reports.export")
    assert not rbac.has_permission(db, read_only.admin, "content.contents.export")
    assert not rbac.has_permission(db, read_only.admin, "system.settings.view")
    assert rbac.has_permission(db, read_only.admin, "system.upload.view")
    assert not rbac.has_permission(db, read_only.admin, "system.upload.create")
    group = db.get(AdminGroup, operator.admin.group_id)
    group.is_active = False
    db.commit()
    assert rbac.permission_codes(db, operator.admin) == set()                            # 停用组无任何权限


def test_all_write_endpoints_forbidden_without_permission(app: FastAPI, client: TestClient, read_only: User, db: Session) -> None:
    """直接调用接口无法绕过前端按钮隐藏：只读组对每个带权限码的写接口都得到 403，且不写入任何数据。"""
    from app.main import iter_api_routes

    before = db.scalar(select(func.count(AdminOperationLog.id)))
    checked = 0
    for path, methods, route in iter_api_routes(app):
        codes = _permission_dependencies(route)
        if not path.startswith(f"{ADMIN_API}/") or not codes:
            continue
        if codes[0].endswith(".view"):
            continue                      # 无副作用的 POST（如模板 preview）只要求 view 权限，只读组本就拥有
        for method in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            url = path.replace("{admin_id}", "1").replace("{group_id}", "1").replace("{key}", "generation_config")
            response = client.request(method, url, headers=read_only.headers, json={})
            body = _err(response, 403)
            assert body["data"] == {"permission": codes[0]}
            checked += 1
    assert checked >= 10
    assert db.scalar(select(func.count(AdminOperationLog.id))) == before


def test_permission_change_takes_effect_without_relogin(client: TestClient, super_admin: User, users: UserFactory) -> None:
    group = users.custom_group("临时组", ["dashboard.view"])
    member = users.create(group)
    _err(client.get(f"{ADMIN_API}/admins", headers=member.headers), 403)
    ok_data(client.put(f"{ADMIN_API}/admin-groups/{group.id}/permissions", headers=super_admin.headers,
                       json={"permission_codes": ["security.admins.view"]}))
    page = ok_data(client.get(f"{ADMIN_API}/admins", headers=member.headers))       # 同一令牌，立即生效
    assert page["total"] >= 2
    me = ok_data(client.get(f"{ADMIN_API}/auth/me", headers=member.headers))
    assert me["permissions"] == ["security.admins.view"]


# =====================================================================
# 3. 令牌
# =====================================================================


def test_token_missing_tampered_expired(client: TestClient, super_admin: User) -> None:
    url = f"{ADMIN_API}/auth/me"
    assert _err(client.get(url), 401)["message"] == "未登录"
    _err(client.get(url, headers={"Authorization": super_admin.token}), 401)            # 缺少 Bearer 前缀
    _err(client.get(url, headers=bearer(super_admin.token[:-2] + "xx")), 401)
    now = int(time.time())
    expired = jwt.encode({"sub": str(super_admin.id), "aud": "admin", "ver": 1, "iat": now - 100, "exp": now - 10},
                         settings.admin_jwt_secret, algorithm="HS256")
    _err(client.get(url, headers=bearer(expired)), 401)
    other_secret = jwt.encode({"sub": str(super_admin.id), "aud": "admin", "ver": 1, "iat": now, "exp": now + 60},
                              "another-secret-0123456789abcdef-0123456789", algorithm="HS256")
    _err(client.get(url, headers=bearer(other_secret)), 401)
    wrong_ver = jwt.encode({"sub": str(super_admin.id), "aud": "admin", "ver": 99, "iat": now, "exp": now + 60},
                           settings.admin_jwt_secret, algorithm="HS256")
    assert _err(client.get(url, headers=bearer(wrong_ver)), 401)["message"] == "登录状态已失效，请重新登录"
    ok_data(client.get(url, headers=super_admin.headers))


def test_logout_invalidates_token(client: TestClient, operator: User) -> None:
    ok_data(client.get(f"{ADMIN_API}/auth/me", headers=operator.headers))
    assert ok_data(client.post(f"{ADMIN_API}/auth/logout", headers=operator.headers)) is None
    _err(client.get(f"{ADMIN_API}/auth/me", headers=operator.headers), 401)


def test_change_password_flow(client: TestClient, db: Session, operator: User) -> None:
    url = f"{ADMIN_API}/auth/change-password"
    body = _err(client.post(url, headers=operator.headers, json={"old_password": "Wrong1234", "new_password": "NewPass2026"}), 400)
    assert body["message"] == "原密码错误"
    assert body["data"] == [{"loc": ["body", "old_password"], "msg": "原密码错误", "type": "wrong_password"}]
    body = _err(client.post(url, headers=operator.headers, json={"old_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD}), 400)
    assert body["data"][0]["loc"] == ["body", "new_password"] and body["data"][0]["type"] == "same_as_old"
    assert "input" not in body["data"][0]
    body = _err(client.post(url, headers=operator.headers, json={"old_password": DEFAULT_PASSWORD, "new_password": "short1"}), 400)
    assert body["data"][0]["loc"] == ["body", "new_password"] and body["data"][0]["type"] == "value_error"
    assert body["data"][0]["msg"] == "密码至少 8 位且须同时包含字母与数字"
    assert "input" not in body["data"][0]

    response = client.post(url, headers=operator.headers, json={"old_password": DEFAULT_PASSWORD, "new_password": "NewPass2026"})
    assert response.json() == {"code": 0, "message": "密码已修改，请重新登录", "data": None}
    _err(client.get(f"{ADMIN_API}/auth/me", headers=operator.headers), 401)        # 其它会话同时失效
    _err(login(client, operator.username, DEFAULT_PASSWORD), 401)
    ok_data(login(client, operator.username, "NewPass2026"))
    log = _logs(db, action="reset_password", admin_id=operator.id)[-1]
    assert log.permission_code == "auth.change_password" and log.target_type == "admin" and log.target_id == str(operator.id)
    assert "NewPass2026" not in log.summary and DEFAULT_PASSWORD not in log.summary


def test_token_version_events(client: TestClient, db: Session, super_admin: User, users: UserFactory) -> None:
    h = super_admin.headers
    target = users.create("operator", username="victim01")
    me_url = f"{ADMIN_API}/auth/me"

    # 仅修改显示名：不递增
    ok_data(client.put(f"{ADMIN_API}/admins/{target.id}", headers=h, json={"display_name": "新名字"}))
    ok_data(client.get(me_url, headers=target.headers))

    # 修改用户组权限：不递增
    operator_group = users.group("operator")
    codes = sorted(ap.DEFAULT_GROUP_PERMISSIONS["operator"] - {"stats.reports.export"})
    ok_data(client.put(f"{ADMIN_API}/admin-groups/{operator_group.id}/permissions", headers=h, json={"permission_codes": codes}))
    ok_data(client.get(me_url, headers=target.headers))

    # 重置他人密码：递增
    ok_data(client.post(f"{ADMIN_API}/admins/{target.id}/reset-password", headers=h, json={"password": "Reset2026x"}))
    _err(client.get(me_url, headers=target.headers), 401)
    token = users.token(target.admin)

    # 禁用：旧令牌 401；再次启用后旧令牌仍 401
    ok_data(client.patch(f"{ADMIN_API}/admins/{target.id}/status", headers=h, json={"is_active": False}))
    _err(client.get(me_url, headers=bearer(token)), 401)
    ok_data(client.patch(f"{ADMIN_API}/admins/{target.id}/status", headers=h, json={"is_active": True}))
    _err(client.get(me_url, headers=bearer(token)), 401)
    token = users.token(target.admin)
    ok_data(client.get(me_url, headers=bearer(token)))

    # 换组：递增
    reviewer_group = users.group("reviewer")
    item = ok_data(client.put(f"{ADMIN_API}/admins/{target.id}", headers=h, json={"group_id": reviewer_group.id}))
    assert item["group"]["code"] == "reviewer" and item["data_scope"] == "all"
    _err(client.get(me_url, headers=bearer(token)), 401)
    db.expire_all()
    assert db.get(Admin, target.id).token_version == 5


def test_rejected_group_change_keeps_token(client: TestClient, db: Session, super_admin: User, sec_admin: User, users: UserFactory) -> None:
    operator_group = users.group("operator")
    body = _err(client.put(f"{ADMIN_API}/admins/{super_admin.id}", headers=sec_admin.headers, json={"group_id": operator_group.id}), 403)
    assert body["data"] is None and body["message"] == "不能把最后一个有效的超级管理员移出超级管理员组"
    db.expire_all()
    admin = db.get(Admin, super_admin.id)
    assert admin.token_version == 1 and admin.group_id == users.group("super_admin").id
    ok_data(client.get(f"{ADMIN_API}/auth/me", headers=super_admin.headers))


def test_disabled_group_member_rejected(client: TestClient, db: Session, users: UserFactory) -> None:
    group = users.custom_group("将停用组", ["dashboard.view"])
    member = users.create(group)
    ok_data(client.get(f"{ADMIN_API}/auth/me", headers=member.headers))
    group.is_active = False
    db.commit()
    assert _err(client.get(f"{ADMIN_API}/auth/me", headers=member.headers), 401)["message"] == "管理员用户组已停用"


# =====================================================================
# 4. 登录
# =====================================================================


def test_login_success_returns_profile_and_audit(client: TestClient, db: Session, super_admin: User) -> None:
    response = login(client, "ADMIN", super_admin.password)                    # 大小写不敏感
    data = ok_data(response)
    assert data["expires_in"] == settings.admin_jwt_expire_seconds
    profile = data["admin"]
    assert profile["username"] == "admin" and profile["display_name"] == "超级管理员"
    assert profile["group"] == {"id": super_admin.admin.group_id, "code": "super_admin", "name": "超级管理员"}
    assert profile["data_scope"] == "all"
    assert len(profile["permissions"]) == 90 and profile["permissions"][0] == "dashboard.view"
    me = ok_data(client.get(f"{ADMIN_API}/auth/me", headers=bearer(data["token"])))
    assert me["last_login_at"].endswith("Z") and me["zhiqi_mode"] == "mock"
    assert {k: me[k] for k in profile} == profile
    log = _logs(db, action="login")[-1]
    assert (log.admin_id, log.permission_code, log.target_type, log.target_id) == (super_admin.id, "auth.login", "admin", str(super_admin.id))
    assert log.request_id == response.headers["X-Request-Id"]
    assert super_admin.password not in log.summary


def test_login_failures_same_message_and_lockout(client: TestClient, operator: User) -> None:
    wrong = _err(login(client, operator.username, "Wrong12345"), 401)
    missing = _err(login(client, "nobody-here", "Wrong12345"), 401)
    assert wrong["message"] == missing["message"] == "用户名或密码错误"
    assert wrong["data"] is None and missing["data"] is None

    for _ in range(4):
        _err(login(client, f"  {operator.username.upper()} ", "Wrong12345"), 401)   # 同一计数键（strip + lower）
    assert int(redis_client.get(admin_login_key(operator.username))) == 5
    body = _err(login(client, operator.username, "Wrong12345"), 429)
    assert body["message"] == "登录失败次数过多，请稍后再试"
    assert 0 < body["data"]["retry_after"] <= 900
    assert _err(login(client, operator.username, DEFAULT_PASSWORD), 429)["data"]["retry_after"] > 0   # 锁定期间正确密码同样 429
    assert int(redis_client.get(admin_login_key(operator.username))) == 5                            # 锁定期间不 INCR


def test_login_success_clears_failures(client: TestClient, operator: User) -> None:
    for _ in range(3):
        _err(login(client, operator.username, "Wrong12345"), 401)
    assert redis_client.ttl(admin_login_key(operator.username)) > 0
    ok_data(login(client, operator.username, DEFAULT_PASSWORD))
    assert redis_client.exists(admin_login_key(operator.username)) == 0


def test_login_inactive_account_or_group(client: TestClient, db: Session, users: UserFactory) -> None:
    disabled = users.create("operator", is_active=False)
    body = _err(login(client, disabled.username, DEFAULT_PASSWORD), 403)
    assert body == {"code": 403, "message": "账号已禁用", "data": None}
    _err(login(client, disabled.username, "Wrong12345"), 401)                      # 密码错误先于状态判定

    group = users.custom_group("停用中的组", ["dashboard.view"])
    member = users.create(group)
    group.is_active = False
    db.commit()
    assert _err(login(client, member.username, DEFAULT_PASSWORD), 403)["message"] == "用户组已停用"
    assert _logs(db, action="login") == []                                        # 403 不写审计


# =====================================================================
# 5. 用户管理与安全规则
# =====================================================================


def test_admin_crud_and_listing(client: TestClient, db: Session, super_admin: User, operator: User, reviewer: User) -> None:
    h = super_admin.headers
    operator_group = db.scalar(select(AdminGroup).where(AdminGroup.code == "operator"))
    created = ok_data(client.post(f"{ADMIN_API}/admins", headers=h, json={
        "username": "operator02", "display_name": "运营小李", "password": "Op3rator2026", "group_id": operator_group.id,
    }))
    assert created["is_active"] is True and created["created_by"] == super_admin.id
    assert created["group"]["code"] == "operator" and created["data_scope"] == "own"
    assert created["last_login_at"] is None and created["created_at"].endswith("Z")
    assert "password" not in json.dumps(created) and "password_hash" not in created

    detail = ok_data(client.get(f"{ADMIN_API}/admins/{created['id']}", headers=h))
    assert set(detail["permissions"]) == ap.DEFAULT_GROUP_PERMISSIONS["operator"]

    page = ok_data(client.get(f"{ADMIN_API}/admins", headers=h))
    assert page["total"] == 4 and page["page"] == 1 and page["page_size"] == 20
    assert [i["id"] for i in page["items"]] == sorted((i["id"] for i in page["items"]), reverse=True)
    assert {i["username"]: i["data_scope"] for i in page["items"]} == {
        "admin": "all", "operator01": "own", "reviewer01": "all", "operator02": "own",
    }
    assert ok_data(client.get(f"{ADMIN_API}/admins", headers=h, params={"keyword": "小李"}))["total"] == 1
    assert ok_data(client.get(f"{ADMIN_API}/admins", headers=h, params={"keyword": "OPERATOR"}))["total"] == 2
    assert ok_data(client.get(f"{ADMIN_API}/admins", headers=h, params={"group_id": operator_group.id}))["total"] == 2
    assert ok_data(client.get(f"{ADMIN_API}/admins", headers=h, params={"is_active": False}))["total"] == 0
    assert ok_data(client.get(f"{ADMIN_API}/admins", headers=h, params={"page_size": 2, "page": 2}))["items"][0]["username"] == "operator01"
    _err(client.get(f"{ADMIN_API}/admins/9999", headers=h), 404)
    _err(client.get(f"{ADMIN_API}/admins/0", headers=h), 400)

    # 新用户可登录
    ok_data(login(client, "operator02", "Op3rator2026"))
    # 清空显示名
    item = ok_data(client.put(f"{ADMIN_API}/admins/{created['id']}", headers=h, json={"display_name": None}))
    assert item["display_name"] is None
    _err(client.put(f"{ADMIN_API}/admins/{created['id']}", headers=h, json={}), 400)


def test_duplicate_username_and_group_name(client: TestClient, db: Session, super_admin: User, operator: User) -> None:
    h = super_admin.headers
    group_id = operator.admin.group_id
    body = _err(client.post(f"{ADMIN_API}/admins", headers=h, json={"username": "Operator01", "password": "Abcdef123", "group_id": group_id}), 409)
    assert body["data"] == {"existing_id": operator.id}

    first = ok_data(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "SEO 专员"}))
    body = _err(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "SEO 专员"}), 409)
    assert body["data"] == {"existing_id": first["id"]}
    second = ok_data(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "媒体组"}))
    body = _err(client.put(f"{ADMIN_API}/admin-groups/{second['id']}", headers=h, json={"name": "运营人员"}), 409)
    assert body["data"] == {"existing_id": users_group_id(db, "operator")}


def users_group_id(db: Session, code: str) -> int:
    return db.scalar(select(AdminGroup.id).where(AdminGroup.code == code))


@pytest.mark.parametrize("password", ["abcdefgh", "12345678", "abc1234", "a1" + "x" * 71, "密码密码密码密码1a" * 3])
def test_weak_passwords_rejected_without_echo(client: TestClient, super_admin: User, operator: User, password: str) -> None:
    body = _err(client.post(f"{ADMIN_API}/admins", headers=super_admin.headers,
                            json={"username": "weakpass", "password": password, "group_id": operator.admin.group_id}), 400)
    assert body["data"] == [{"loc": ["body", "password"], "msg": "密码至少 8 位且须同时包含字母与数字", "type": "value_error"}]
    assert password not in json.dumps(body, ensure_ascii=False)
    body = _err(client.post(f"{ADMIN_API}/admins/{operator.id}/reset-password", headers=super_admin.headers, json={"password": password}), 400)
    assert body["data"][0]["loc"] == ["body", "password"] and "input" not in body["data"][0]


def test_group_unavailable_on_assign(client: TestClient, db: Session, super_admin: User, users: UserFactory, operator: User) -> None:
    inactive = users.custom_group("已停用组", ["dashboard.view"], is_active=False)
    for payload in ({"username": "newbie01", "password": "Abcdef123", "group_id": inactive.id},
                    {"username": "newbie02", "password": "Abcdef123", "group_id": 9999}):
        body = _err(client.post(f"{ADMIN_API}/admins", headers=super_admin.headers, json=payload), 400)
        assert body["data"] == [{"loc": ["body", "group_id"], "msg": "用户组不存在或已停用", "type": "group_unavailable", "input": payload["group_id"]}]
    body = _err(client.put(f"{ADMIN_API}/admins/{operator.id}", headers=super_admin.headers, json={"group_id": inactive.id}), 400)
    assert body["data"][0]["type"] == "group_unavailable"
    db.expire_all()
    assert db.get(Admin, operator.id).token_version == 1


def test_admin_security_rules(client: TestClient, db: Session, super_admin: User, sec_admin: User, users: UserFactory) -> None:
    h = super_admin.headers
    # 禁用自己
    body = _err(client.patch(f"{ADMIN_API}/admins/{super_admin.id}/status", headers=h, json={"is_active": False}), 403)
    assert body == {"code": 403, "message": "不能禁用自己", "data": None}
    # 修改自己的用户组
    body = _err(client.put(f"{ADMIN_API}/admins/{super_admin.id}", headers=h, json={"group_id": users_group_id(db, "operator")}), 403)
    assert body["message"] == "不能修改自己的用户组"
    # 禁用最后一个有效超管（由他人操作）
    body = _err(client.patch(f"{ADMIN_API}/admins/{super_admin.id}/status", headers=sec_admin.headers, json={"is_active": False}), 403)
    assert body["message"] == "不能禁用最后一个有效的超级管理员"
    # 已禁用的超管不计入「有效超管」
    users.create("super_admin", username="superoff", is_active=False)
    _err(client.patch(f"{ADMIN_API}/admins/{super_admin.id}/status", headers=sec_admin.headers, json={"is_active": False}), 403)
    # 新增一个超管后原超管可被禁用
    second = users.create("super_admin", username="super02")
    ok_data(client.patch(f"{ADMIN_API}/admins/{super_admin.id}/status", headers=second.headers, json={"is_active": False}))
    # 此时 super02 成为最后一个有效超管
    _err(client.put(f"{ADMIN_API}/admins/{second.id}", headers=sec_admin.headers, json={"group_id": users_group_id(db, "reviewer")}), 403)


def test_group_security_rules(client: TestClient, db: Session, super_admin: User, users: UserFactory) -> None:
    h = super_admin.headers
    ids = {code: users_group_id(db, code) for code in ap.SYSTEM_GROUP_CODES}
    for group_id in ids.values():
        assert _err(client.delete(f"{ADMIN_API}/admin-groups/{group_id}", headers=h), 403)["message"] == "系统用户组不可删除"
        body = _err(client.put(f"{ADMIN_API}/admin-groups/{group_id}", headers=h, json={"is_active": False}), 403)
        assert body["message"] == "系统用户组不可停用" and body["data"] is None
    body = _err(client.put(f"{ADMIN_API}/admin-groups/{ids['super_admin']}/permissions", headers=h,
                           json={"permission_codes": ["dashboard.view"]}), 403)
    assert body["message"] == "超级管理员组的权限固定为全部权限，不可修改"
    body = _err(client.put(f"{ADMIN_API}/admin-groups/{ids['reviewer']}/permissions", headers=h, json={"permission_codes": []}), 403)
    assert body["message"] == "系统用户组的权限不能为空"
    assert _group_codes(db, "reviewer") == ap.DEFAULT_GROUP_PERMISSIONS["reviewer"]

    custom = users.custom_group("运营二组", ["dashboard.view"])
    member = users.create(custom)
    assert _err(client.put(f"{ADMIN_API}/admin-groups/{custom.id}", headers=h, json={"is_active": False}), 403)["message"] == \
        "用户组内仍有启用中的用户，不能停用"
    assert _err(client.delete(f"{ADMIN_API}/admin-groups/{custom.id}", headers=h), 403)["message"] == "用户组内仍有用户，不能删除"
    ok_data(client.patch(f"{ADMIN_API}/admins/{member.id}/status", headers=h, json={"is_active": False}))
    ok_data(client.put(f"{ADMIN_API}/admin-groups/{custom.id}", headers=h, json={"is_active": False}))   # 只剩已禁用成员可停用
    _err(client.delete(f"{ADMIN_API}/admin-groups/{custom.id}", headers=h), 403)                         # 含已禁用成员仍不可删除

    empty_id = users.custom_group("空组").id
    assert ok_data(client.put(f"{ADMIN_API}/admin-groups/{empty_id}/permissions", headers=h, json={"permission_codes": []}))["permission_codes"] == []
    assert ok_data(client.delete(f"{ADMIN_API}/admin-groups/{empty_id}", headers=h)) is None
    db.expunge_all()
    assert db.get(AdminGroup, empty_id) is None
    _err(client.get(f"{ADMIN_API}/admin-groups/{empty_id}", headers=h), 404)


def test_group_crud(client: TestClient, db: Session, super_admin: User, operator: User) -> None:
    h = super_admin.headers
    created = ok_data(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "seo team", "description": "  "}))
    assert created["code"].startswith("custom_") and len(created["code"]) == len("custom_") + 12
    assert created["name_en"] == "Seo Team" and created["description"] is None
    assert created["is_system"] is False and created["is_active"] is True and created["data_scope"] == "own"
    assert created["admin_count"] == 0 and created["permission_codes"] == []
    zh = ok_data(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "媒体设计", "data_scope": "all", "is_active": False}))
    assert zh["name_en"] == "Custom Group " + zh["code"][-6:] and zh["data_scope"] == "all" and zh["is_active"] is False
    _err(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "x", "data_scope": "team"}), 400)
    _err(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": ""}), 400)

    groups = ok_data(client.get(f"{ADMIN_API}/admin-groups", headers=h))
    assert [g["code"] for g in groups[:4]] == ["super_admin", "operator", "reviewer", "read_only"]
    assert [g["id"] for g in groups[4:]] == sorted(g["id"] for g in groups[4:])
    assert next(g for g in groups if g["code"] == "operator")["admin_count"] == 1
    assert [g["code"] for g in ok_data(client.get(f"{ADMIN_API}/admin-groups", headers=h, params={"is_active": False}))] == [zh["code"]]

    detail = ok_data(client.get(f"{ADMIN_API}/admin-groups/{users_group_id(db, 'super_admin')}", headers=h))
    assert detail["permission_codes"] == sorted(ap.PERMISSION_CODES)

    updated = ok_data(client.put(f"{ADMIN_API}/admin-groups/{created['id']}", headers=h,
                                 json={"name": "SEO 专员", "description": "收录与监控", "is_active": False}))
    assert (updated["name"], updated["description"], updated["is_active"]) == ("SEO 专员", "收录与监控", False)
    assert updated["code"] == created["code"] and updated["name_en"] == "Seo Team"     # code / name_en 不可修改
    _err(client.put(f"{ADMIN_API}/admin-groups/{created['id']}", headers=h, json={"code": "hacked"}), 400)


# =====================================================================
# 6. 权限保存与补齐
# =====================================================================


def test_save_permissions_expands_dependencies(client: TestClient, db: Session, super_admin: User, users: UserFactory) -> None:
    group = users.custom_group("媒体设计")
    data = ok_data(client.put(f"{ADMIN_API}/admin-groups/{group.id}/permissions", headers=super_admin.headers,
                              json={"permission_codes": ["dashboard.view", "content.keywords.generate", "media.images.generate"]}))
    assert data["permission_codes"] == [
        "content.batches.view", "content.keywords.generate", "content.keywords.view", "dashboard.view",
        "media.assets.view", "media.images.generate", "media.images.view",
    ]
    assert data["name"] == "媒体设计" and data["is_system"] is False and data["data_scope"] == "own"
    detail = ok_data(client.get(f"{ADMIN_API}/admin-groups/{group.id}", headers=super_admin.headers))
    assert detail["permission_codes"] == data["permission_codes"]


def test_save_permissions_invalid_codes_keeps_group(client: TestClient, db: Session, super_admin: User) -> None:
    operator_id = users_group_id(db, "operator")
    body = _err(client.put(f"{ADMIN_API}/admin-groups/{operator_id}/permissions", headers=super_admin.headers,
                           json={"permission_codes": ["dashboard.view", "content.keywords.view", "content.keywords.publish", "nope"]}), 400)
    assert body["data"] == [
        {"loc": ["body", "permission_codes", 2], "msg": "未知权限码", "type": "unknown_permission", "input": "content.keywords.publish"},
        {"loc": ["body", "permission_codes", 3], "msg": "未知权限码", "type": "unknown_permission", "input": "nope"},
    ]
    assert _group_codes(db, "operator") == ap.DEFAULT_GROUP_PERMISSIONS["operator"]


# =====================================================================
# 7. 审计
# =====================================================================


def test_audit_records_for_rbac_writes(client: TestClient, db: Session, super_admin: User, operator: User) -> None:
    h = {**super_admin.headers, "X-Request-Id": "req-create-0001", "X-Forwarded-For": "203.0.113.42, 10.0.0.1", "User-Agent": "pytest-ua"}
    created = ok_data(client.post(f"{ADMIN_API}/admins", headers=h, json={
        "username": "auditee", "password": "Secr3tPass99", "group_id": operator.admin.group_id,
    }))
    log = _logs(db, permission_code="security.admins.create")[-1]
    assert (log.admin_id, log.action, log.target_type, log.target_id) == (super_admin.id, "create", "admin", str(created["id"]))
    assert (log.request_id, log.ip, log.user_agent, log.summary) == ("req-create-0001", "203.0.113.0", "pytest-ua", "新增用户 auditee")

    sh = super_admin.headers
    ok_data(client.put(f"{ADMIN_API}/admins/{created['id']}", headers=sh, json={"display_name": "审计对象"}))
    ok_data(client.patch(f"{ADMIN_API}/admins/{created['id']}/status", headers=sh, json={"is_active": False}))
    ok_data(client.post(f"{ADMIN_API}/admins/{created['id']}/reset-password", headers=sh, json={"password": "Secr3tPass77"}))
    group_id = users_group_id(db, "reviewer")
    ok_data(client.put(f"{ADMIN_API}/admin-groups/{group_id}/permissions", headers=sh,
                       json={"permission_codes": sorted(ap.DEFAULT_GROUP_PERMISSIONS["reviewer"])}))
    expected = [
        ("security.admins.update", "update", "admin", str(created["id"]), "编辑用户 auditee：显示名称"),
        ("security.admins.status", "update_status", "admin", str(created["id"]), "禁用用户 auditee"),
        ("security.admins.reset_password", "reset_password", "admin", str(created["id"]), "重置用户 auditee 的密码"),
        ("security.groups.assign", "update", "admin_group", str(group_id), None),
    ]
    for code, action, target_type, target_id, summary in expected:
        log = _logs(db, permission_code=code)[-1]
        assert (log.action, log.target_type, log.target_id) == (action, target_type, target_id)
        assert len(log.request_id) == 32
        if summary:
            assert log.summary == summary
    all_logs = _logs(db)
    assert not any("Secr3tPass" in (log.summary or "") for log in all_logs)

    # 登录 / 登出
    token = ok_data(login(client, operator.username, DEFAULT_PASSWORD))["token"]
    ok_data(client.post(f"{ADMIN_API}/auth/logout", headers=bearer(token)))
    log = _logs(db, action="logout")[-1]
    assert (log.admin_id, log.permission_code, log.target_type, log.target_id) == (operator.id, "auth.logout", "admin", str(operator.id))

    # 读接口与失败的写接口不记录
    count = len(_logs(db))
    ok_data(client.get(f"{ADMIN_API}/admins", headers=sh))
    _err(client.post(f"{ADMIN_API}/admins", headers=sh, json={"username": "auditee", "password": "Abcdef123", "group_id": operator.admin.group_id}), 409)
    assert len(_logs(db)) == count


def test_audit_settings_and_group_scope_summary(client: TestClient, db: Session, super_admin: User) -> None:
    sh = super_admin.headers
    ok_data(client.put(f"{ADMIN_API}/settings/stats_config", headers=sh, json={"value": {"overview_cache_seconds": 30}}))
    log = _logs(db, permission_code="system.settings.update")[-1]
    assert (log.action, log.target_type, log.target_id, log.summary) == ("update", "setting", "stats_config", "更新 setting #stats_config")
    operator_id = users_group_id(db, "operator")
    ok_data(client.put(f"{ADMIN_API}/admin-groups/{operator_id}", headers=sh, json={"data_scope": "all"}))
    log = _logs(db, permission_code="security.groups.update")[-1]
    assert (log.action, log.target_type, log.target_id) == ("update", "admin_group", str(operator_id))
    assert log.summary == "用户组 运营人员 数据范围 own → all"


def test_audit_skipped_when_handler_marks_written(app: FastAPI, client: TestClient, db: Session, super_admin: User) -> None:
    """无副作用的 ``POST …/preview`` 由处理函数设置 ``audit_written=True`` 跳过审计；``duplicate`` 照常记 ``execute``。"""
    from app.models import PromptTemplate

    tpl = PromptTemplate(code="audit_tpl", version=1, kind="keyword", capability="keyword", name="审计模板", language="zh-CN",
                         project_id=0, user_prompt="{{seeds}}", output_format="text", status="published", created_by=super_admin.id,
                         updated_by=super_admin.id)
    db.add(tpl)
    db.commit()
    count = len(_logs(db))
    ok_data(client.post(f"{ADMIN_API}/prompt-templates/{tpl.id}/preview", headers=super_admin.headers, json={"variables": {}}))
    assert len(_logs(db)) == count
    ok_data(client.post(f"{ADMIN_API}/prompt-templates/{tpl.id}/duplicate", headers=super_admin.headers,
                        json={"code": "audit_tpl_copy", "name": "副本"}))
    log = _logs(db)[-1]
    assert (log.action, log.target_type, log.target_id) == ("execute", "prompt_template", str(tpl.id))


def test_audit_routes_traversal(app: FastAPI) -> None:
    """遍历全部 /api/v1/admin/* 写路由：resolve_action 命中显式规则（不落入兜底）且 resolve_target_type 非空。"""
    from app.main import iter_api_routes, resolve_action_with_rule, resolve_target_type

    checked = 0
    for path, methods, _route in iter_api_routes(app):
        if not path.startswith("/api/v1/admin/"):
            continue
        for method in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            action, rule = resolve_action_with_rule(method, path)
            assert rule != "fallback", (method, path)
            assert action in {"create", "update", "update_status", "delete", "execute", "login", "logout", "reset_password"}
            assert resolve_target_type(path), (method, path)
            checked += 1
    assert checked >= 13


# docs/04 接口全表中的全部写接口（含后续阶段）→ 期望的 action / target_type
DOC_WRITE_ROUTES: list[tuple[str, str, str, str]] = [
    ("DELETE", "/admin/admin-groups/{id}", "delete", "admin_group"),
    ("DELETE", "/admin/ai/routes/{id}", "delete", "capability_route"),
    ("DELETE", "/admin/contents/{id}", "delete", "content"),
    ("DELETE", "/admin/contents/{id}/versions/{version_id}", "delete", "content_version"),
    ("DELETE", "/admin/keywords/{id}", "delete", "keyword"),
    ("DELETE", "/admin/links/{id}", "delete", "publish_link"),
    ("DELETE", "/admin/media/assets/{id}", "delete", "media_asset"),
    ("DELETE", "/admin/platforms/{id}", "delete", "publish_platform"),
    ("DELETE", "/admin/projects/{id}", "delete", "project"),
    ("DELETE", "/admin/prompt-templates/{id}", "delete", "prompt_template"),
    ("DELETE", "/admin/titles/{id}", "delete", "title"),
    ("PATCH", "/admin/admins/{id}/status", "update_status", "admin"),
    ("POST", "/admin/admin-groups", "create", "admin_group"),
    ("POST", "/admin/admins", "create", "admin"),
    ("POST", "/admin/admins/{id}/reset-password", "reset_password", "admin"),
    ("POST", "/admin/ai/health/probe", "execute", "ai_model"),
    ("POST", "/admin/ai/models/sync", "execute", "ai_model"),
    ("POST", "/admin/ai/routes", "create", "capability_route"),
    ("POST", "/admin/ai/routes/{id}/reset-breaker", "execute", "capability_route"),
    ("POST", "/admin/ai/routes/{id}/test", "execute", "capability_route"),
    ("POST", "/admin/ai/tasks/{id}/cancel", "execute", "ai_task"),
    ("POST", "/admin/ai/tasks/{id}/retry", "execute", "ai_task"),
    ("POST", "/admin/ai/usage/reconcile", "execute", "ai_usage_log"),
    ("POST", "/admin/alerts/batch-resolve", "update_status", "alert"),
    ("POST", "/admin/alerts/{id}/acknowledge", "update_status", "alert"),
    ("POST", "/admin/alerts/{id}/ignore", "update_status", "alert"),
    ("POST", "/admin/alerts/{id}/resolve", "update_status", "alert"),
    ("POST", "/admin/auth/change-password", "reset_password", "admin"),
    ("POST", "/admin/auth/login", "login", "admin"),
    ("POST", "/admin/auth/logout", "logout", "admin"),
    ("POST", "/admin/contents", "create", "content"),
    ("POST", "/admin/contents/generate", "execute", "content"),
    ("POST", "/admin/contents/{id}/approve", "update_status", "content"),
    ("POST", "/admin/contents/{id}/archive", "update_status", "content"),
    ("POST", "/admin/contents/{id}/assets/{asset_id}/attach", "execute", "content"),
    ("POST", "/admin/contents/{id}/assets/{asset_id}/detach", "execute", "content"),
    ("POST", "/admin/contents/{id}/generate-body", "execute", "content"),
    ("POST", "/admin/contents/{id}/generate-outline", "execute", "content"),
    ("POST", "/admin/contents/{id}/generate-seo", "execute", "content"),
    ("POST", "/admin/contents/{id}/reject", "update_status", "content"),
    ("POST", "/admin/contents/{id}/rewrite", "execute", "content"),
    ("POST", "/admin/contents/{id}/submit-review", "update_status", "content"),
    ("POST", "/admin/contents/{id}/unarchive", "update_status", "content"),
    ("POST", "/admin/contents/{id}/versions/{version_id}/restore", "execute", "content_version"),
    ("POST", "/admin/generation-batches/{id}/cancel", "execute", "generation_batch"),
    ("POST", "/admin/generation-batches/{id}/retry", "execute", "generation_batch"),
    ("POST", "/admin/keywords", "create", "keyword"),
    ("POST", "/admin/keywords/batch-status", "update_status", "keyword"),
    ("POST", "/admin/keywords/generate", "execute", "keyword"),
    ("POST", "/admin/keywords/import", "execute", "keyword"),
    ("POST", "/admin/keywords/import-file", "execute", "keyword"),
    ("POST", "/admin/keywords/{id}/adopt", "update_status", "keyword"),
    ("POST", "/admin/keywords/{id}/discard", "update_status", "keyword"),
    ("POST", "/admin/keywords/{id}/restore", "update_status", "keyword"),
    ("POST", "/admin/links", "create", "publish_link"),
    ("POST", "/admin/links/batch", "execute", "publish_link"),
    ("POST", "/admin/links/{id}/check", "execute", "publish_link"),
    ("POST", "/admin/links/{id}/index-check", "execute", "publish_link"),
    ("POST", "/admin/links/{id}/mark-index", "execute", "publish_link"),
    ("POST", "/admin/links/{id}/pause", "update_status", "publish_link"),
    ("POST", "/admin/links/{id}/rebaseline", "execute", "publish_link"),
    ("POST", "/admin/links/{id}/resume", "update_status", "publish_link"),
    ("POST", "/admin/media/assets/{id}/retry", "execute", "media_asset"),
    ("POST", "/admin/media/assets/{id}/transfer", "execute", "media_asset"),
    ("POST", "/admin/media/images/generate", "execute", "media_asset"),
    ("POST", "/admin/media/videos/generate", "execute", "media_asset"),
    ("POST", "/admin/monitoring/index-checks/run", "execute", "publish_link"),
    ("POST", "/admin/monitoring/link-checks/run", "execute", "publish_link"),
    ("POST", "/admin/platforms", "create", "publish_platform"),
    ("POST", "/admin/platforms/detect", "execute", "publish_platform"),
    ("POST", "/admin/platforms/{id}/test", "execute", "publish_platform"),
    ("POST", "/admin/projects", "create", "project"),
    ("POST", "/admin/projects/{id}/archive", "update_status", "project"),
    ("POST", "/admin/projects/{id}/unarchive", "update_status", "project"),
    ("POST", "/admin/prompt-templates", "create", "prompt_template"),
    ("POST", "/admin/prompt-templates/{id}/archive", "update_status", "prompt_template"),
    ("POST", "/admin/prompt-templates/{id}/duplicate", "execute", "prompt_template"),
    ("POST", "/admin/prompt-templates/{id}/preview", "execute", "prompt_template"),
    ("POST", "/admin/prompt-templates/{id}/publish", "execute", "prompt_template"),
    ("POST", "/admin/stats/recompute", "execute", "daily_stats"),
    ("POST", "/admin/titles", "create", "title"),
    ("POST", "/admin/titles/batch-status", "update_status", "title"),
    ("POST", "/admin/titles/generate", "execute", "title"),
    ("POST", "/admin/titles/{id}/adopt", "update_status", "title"),
    ("POST", "/admin/titles/{id}/discard", "update_status", "title"),
    ("POST", "/admin/titles/{id}/restore", "update_status", "title"),
    ("POST", "/admin/titles/{id}/score", "execute", "title"),
    ("POST", "/admin/uploads/image", "create", "media_asset"),
    ("POST", "/admin/uploads/video", "create", "media_asset"),
    ("PUT", "/admin/admin-groups/{id}", "update", "admin_group"),
    ("PUT", "/admin/admin-groups/{id}/permissions", "update", "admin_group"),
    ("PUT", "/admin/admins/{id}", "update", "admin"),
    ("PUT", "/admin/ai/routes/{id}", "update", "capability_route"),
    ("PUT", "/admin/contents/{id}", "update", "content"),
    ("PUT", "/admin/keywords/{id}", "update", "keyword"),
    ("PUT", "/admin/links/{id}", "update", "publish_link"),
    ("PUT", "/admin/platforms/{id}", "update", "publish_platform"),
    ("PUT", "/admin/projects/{id}", "update", "project"),
    ("PUT", "/admin/projects/{id}/routes", "update", "project"),
    ("PUT", "/admin/prompt-templates/{id}", "update", "prompt_template"),
    ("PUT", "/admin/settings", "update", "setting"),
    ("PUT", "/admin/settings/{key}", "update", "setting"),
    ("PUT", "/admin/titles/{id}", "update", "title"),
]


@pytest.mark.parametrize(("method", "path", "action", "target_type"), DOC_WRITE_ROUTES)
def test_resolve_action_for_documented_routes(method: str, path: str, action: str, target_type: str) -> None:
    from app.main import resolve_action, resolve_action_with_rule, resolve_target_type

    full = "/api/v1" + path.replace("{id}", "{item_id}")
    assert resolve_action(method, full) == action
    assert resolve_action_with_rule(method, full)[1] != "fallback"
    assert resolve_target_type(full) == target_type


def test_resolve_target_id_rules() -> None:
    from app.main import default_summary, resolve_action_with_rule, resolve_target_id

    assert resolve_target_id("/api/v1/admin/contents/{content_id}/versions/{version_id}/restore", {"content_id": "3", "version_id": "9"}) == "9"
    assert resolve_target_id("/api/v1/admin/settings/{key}", {"key": "alert_config"}) == "alert_config"
    assert resolve_target_id("/api/v1/admin/ai/health/probe", {}) is None
    assert resolve_target_id("/api/v1/admin/stats/recompute", {}) is None
    assert resolve_target_id("/api/v1/admin/monitoring/link-checks/run", {}) is None
    assert resolve_target_id("/api/v1/admin/auth/change-password", {}, admin_id=5) == "5"
    assert resolve_target_id("/api/v1/admin/keywords", {}) is None
    assert resolve_action_with_rule("POST", "/api/v1/admin/keywords/{id}/frobnicate") == ("execute", "fallback")
    assert default_summary("update_status", "keyword", "12") == "变更状态 keyword #12"
    assert default_summary("create", "project", None) == "新增 project"


def test_mask_ip_and_name_en() -> None:
    assert rbac.mask_ip("203.0.113.42") == "203.0.113.0"
    assert rbac.mask_ip("203.0.113.42:5050") == "203.0.113.0"
    assert rbac.mask_ip("2001:db8:85a3:8d3:1319:8a2e:370:7348") == "2001:db8:85a3:8d3::"
    assert rbac.mask_ip("[2001:db8::1]:443") == "2001:db8:0:0::"
    assert rbac.mask_ip("::ffff:198.51.100.7") == "198.51.100.0"
    assert rbac.mask_ip("") is None and rbac.mask_ip(None) is None and rbac.mask_ip("testclient") is None
    assert rbac.generate_name_en("seo team 2", "custom_3f9a1c2b7d4e") == "Seo Team 2"
    assert rbac.generate_name_en("SEO 专员", "custom_3f9a1c2b7d4e") == "Custom Group 2b7d4e"


# =====================================================================
# 8. 启动 seed
# =====================================================================


def test_ensure_rbac_seed_idempotent(db: Session) -> None:
    def counts() -> tuple[int, int, int]:
        return (
            db.scalar(select(func.count(AdminPermission.id))),
            db.scalar(select(func.count(AdminGroup.id))),
            db.scalar(select(func.count()).select_from(AdminGroupPermission)),
        )

    before = counts()
    stats = rbac.ensure_rbac_seed(db)
    assert stats == {"permissions_inserted": 0, "permissions_updated": 0, "groups_inserted": 0, "links_inserted": 0}
    assert counts() == before


def test_ensure_rbac_seed_new_code_and_self_heal(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    operator_before = _group_codes(db, "operator")
    new_spec = ap.PermissionSpec("stats.reports.share", "stats", "分享报表", "action", "stats.reports.view", 703)
    monkeypatch.setattr(ap, "PERMISSIONS", [*ap.PERMISSIONS, new_spec])
    monkeypatch.setattr(ap, "PERMISSION_CODES", ap.PERMISSION_CODES | {new_spec.code})
    stats = rbac.ensure_rbac_seed(db)
    assert stats["permissions_inserted"] == 1 and stats["links_inserted"] == 1
    super_group = db.scalar(select(AdminGroup).where(AdminGroup.code == "super_admin"))
    linked = set(db.scalars(
        select(AdminPermission.code).join(AdminGroupPermission, AdminGroupPermission.permission_id == AdminPermission.id)
        .where(AdminGroupPermission.group_id == super_group.id)
    ).all())
    assert "stats.reports.share" in linked
    assert _group_codes(db, "operator") == operator_before

    # 已调整的系统组不被改写；被篡改的 super_admin 写回 all；被清空关联的系统组恢复默认
    groups = {g.code: g for g in db.scalars(select(AdminGroup)).all()}
    groups["super_admin"].data_scope = "own"
    groups["operator"].data_scope = "all"
    groups["reviewer"].data_scope = "own"
    db.commit()
    rbac.save_group_permissions(db, groups["read_only"].id, ["dashboard.view"])
    db.execute(AdminGroupPermission.__table__.delete().where(AdminGroupPermission.group_id == groups["reviewer"].id))
    db.commit()
    rbac.ensure_rbac_seed(db)
    db.expire_all()
    assert db.get(AdminGroup, groups["super_admin"].id).data_scope == "all"
    assert db.get(AdminGroup, groups["operator"].id).data_scope == "all"
    assert db.get(AdminGroup, groups["reviewer"].id).data_scope == "own"
    assert _group_codes(db, "read_only") == {"dashboard.view"}
    assert _group_codes(db, "reviewer") == ap.DEFAULT_GROUP_PERMISSIONS["reviewer"]


def test_ensure_rbac_seed_restores_missing_system_group(db: Session) -> None:
    group = db.scalar(select(AdminGroup).where(AdminGroup.code == "read_only"))
    db.execute(AdminGroupPermission.__table__.delete().where(AdminGroupPermission.group_id == group.id))
    db.delete(group)
    db.commit()
    stats = rbac.ensure_rbac_seed(db)
    assert stats["groups_inserted"] == 1
    restored = db.scalar(select(AdminGroup).where(AdminGroup.code == "read_only"))
    assert restored.data_scope == "all" and restored.is_system
    assert _group_codes(db, "read_only") == ap.DEFAULT_GROUP_PERMISSIONS["read_only"]


# =====================================================================
# 9. 用户组数据范围（用户组侧，docs/07 §11 末条）
# =====================================================================


def test_group_data_scope_rules(client: TestClient, db: Session, super_admin: User, operator: User) -> None:
    h = super_admin.headers
    super_id, operator_id = users_group_id(db, "super_admin"), users_group_id(db, "operator")
    body = _err(client.put(f"{ADMIN_API}/admin-groups/{super_id}", headers=h, json={"data_scope": "own"}), 403)
    assert body == {"code": 403, "message": "超级管理员组的数据范围固定为全部数据，不可修改", "data": None}
    ok_data(client.put(f"{ADMIN_API}/admin-groups/{super_id}", headers=h, json={"data_scope": "all"}))     # 不变更允许

    assert ok_data(client.get(f"{ADMIN_API}/auth/me", headers=operator.headers))["data_scope"] == "own"
    item = ok_data(client.put(f"{ADMIN_API}/admin-groups/{operator_id}", headers=h, json={"data_scope": "all"}))
    assert item["data_scope"] == "all"
    me = ok_data(client.get(f"{ADMIN_API}/auth/me", headers=operator.headers))                   # 旧令牌仍有效，立即生效
    assert me["data_scope"] == "all"
    db.expire_all()
    assert db.get(Admin, operator.id).token_version == 1
    listing = ok_data(client.get(f"{ADMIN_API}/admins", headers=h))
    assert next(i for i in listing["items"] if i["id"] == operator.id)["data_scope"] == "all"


# =====================================================================
# 10. 应用骨架：health / site-info / settings / media / 请求 ID / 分页 / seed 脚本
# =====================================================================


def test_health_reports_components(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    data = ok_data(response)
    assert data == {
        "status": "degraded", "db": True, "redis": True, "zhiqi_mode": "mock",
        "workers": {
            "worker": {"alive": False, "replicas": 0, "heartbeat_at": None},
            "monitor_worker": {"alive": False, "replicas": 0, "heartbeat_at": None},
        },
        "warnings": [], "version": "0.1.0",
    }
    assert response.headers["X-Request-Id"]

    now = datetime.now(UTC)
    redis_client.set("worker:heartbeat:worker:host-a:101", json.dumps({"hostname": "host-a", "pid": 101, "at": now.isoformat()}), ex=900)
    redis_client.set("worker:heartbeat:worker:host-b:202", json.dumps({"hostname": "host-b", "pid": 202, "at": (now - timedelta(minutes=5)).isoformat()}), ex=900)
    redis_client.set("worker:heartbeat:monitor_worker:host-a:303", json.dumps({"hostname": "host-a", "pid": 303, "at": now.timestamp()}), ex=900)
    data = ok_data(client.get("/api/v1/health"))
    assert data["status"] == "ok"
    assert data["workers"]["worker"]["alive"] is True and data["workers"]["worker"]["replicas"] == 1
    assert data["workers"]["worker"]["heartbeat_at"] == now.replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    assert data["workers"]["monitor_worker"]["alive"] is True

    from app.api.health import worker_replicas

    replicas = worker_replicas("worker")
    assert [(r["hostname"], r["pid"], r["alive"]) for r in replicas] == [("host-a", 101, True), ("host-b", 202, False)]


def test_health_returns_503_when_redis_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_lib

    def broken_ping() -> bool:
        raise redis_lib.ConnectionError("down")

    monkeypatch.setattr(redis_client, "ping", broken_ping)
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["data"]["redis"] is False and body["data"]["db"] is True and body["data"]["status"] == "degraded"


def test_health_public_base_url_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import health

    health.reset_public_base_url_cache()
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-live")
    try:
        assert health.check_public_base_url("http://127.0.0.1:8100")
        assert health.check_public_base_url("http://10.1.2.3")
        assert health.check_public_base_url("http://server:8000")
        assert health.check_public_base_url("https://8.8.8.8") == []
    finally:
        health.reset_public_base_url_cache()
    monkeypatch.setattr(settings, "zhiqi_api_key", "")
    assert health.check_public_base_url("http://127.0.0.1:8100") == []


def test_site_info_public(client: TestClient, db: Session, super_admin: User) -> None:
    data = ok_data(client.get(f"{ADMIN_API}/auth/site-info"))
    assert data == {"site_name": "aicreat 内容生成平台", "logo_url": "", "footer": "", "support_contact": ""}
    assert ok_data(client.get(f"{ADMIN_API}/auth/site-info", params={"locale": "fr-FR"}))["site_name"] == "aicreat 内容生成平台"
    ok_data(client.put(f"{ADMIN_API}/settings/system_info", headers=super_admin.headers,
                       json={"locale": "en-US", "value": {"site_name": "aicreat EN", "footer": "(c) aicreat"}}))
    data = ok_data(client.get(f"{ADMIN_API}/auth/site-info", params={"locale": "en-US"}))
    assert data["site_name"] == "aicreat EN" and data["footer"] == "(c) aicreat"
    assert ok_data(client.get(f"{ADMIN_API}/auth/site-info", params={"locale": "zh-CN"}))["site_name"] == "aicreat 内容生成平台"


def test_settings_routes(client: TestClient, super_admin: User, operator: User) -> None:
    h = super_admin.headers
    items = ok_data(client.get(f"{ADMIN_API}/settings", headers=h))
    assert len(items) == 10 and {i["key"] for i in items} >= {"generation_config", "system_info"}
    single = ok_data(client.get(f"{ADMIN_API}/settings/alert_config", headers=h))
    assert single["key"] == "alert_config" and single["locale"] == "*"
    _err(client.get(f"{ADMIN_API}/settings/unknown_key", headers=h), 404)
    saved = ok_data(client.put(f"{ADMIN_API}/settings/stats_config", headers=h, json={"value": {"overview_cache_seconds": 45}}))
    assert saved["value"]["overview_cache_seconds"] == 45
    body = _err(client.put(f"{ADMIN_API}/settings/stats_config", headers=h, json={"value": {"bogus": 1}}), 400)
    assert body["data"][0]["type"] == "extra_forbidden"
    batch = ok_data(client.put(f"{ADMIN_API}/settings", headers=h, json={"items": [
        {"key": "stats_config", "value": {"overview_cache_seconds": 50}},
    ]}))
    assert batch[0]["value"]["overview_cache_seconds"] == 50
    runtime = ok_data(client.get(f"{ADMIN_API}/settings/runtime", headers=operator.headers))
    assert runtime["zhiqi_mode"] == "mock" and "generation_config" in runtime
    _err(client.get(f"{ADMIN_API}/settings/runtime"), 401)


def test_media_file_route(client: TestClient) -> None:
    storage_dir = settings.local_storage_path
    target = storage_dir / "media" / "images" / "2026" / "10"
    target.mkdir(parents=True)
    (target / "abc.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    response = client.get("/media/media/images/2026/10/abc.png")
    assert response.status_code == 200 and response.content == b"\x89PNG\r\n\x1a\nfake"
    assert response.headers["content-type"] == "image/png"
    assert _err(client.get("/media/media/images/2026/10/missing.png"), 404)["message"] == "文件不存在"
    for bad in ("/media/media/%2e%2e/%2e%2e/secret.txt", "/media/..%2F..%2Fetc%2Fpasswd", "/media/a%5Cb.png"):
        assert client.get(bad).status_code == 400, bad


def test_request_id_passthrough_and_500(app: FastAPI, client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-Id": "abc-123"})
    assert response.headers["X-Request-Id"] == "abc-123"
    response = client.get("/api/v1/health", headers={"X-Request-Id": "bad id!"})
    assert response.headers["X-Request-Id"] != "bad id!" and len(response.headers["X-Request-Id"]) == 32

    @app.get("/api/v1/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    response = client.get("/api/v1/boom", headers={"X-Request-Id": "boom-1"})
    assert response.status_code == 500
    assert response.headers["X-Request-Id"] == "boom-1"
    body = response.json()
    assert body["code"] == 500 and body["message"] == "服务器内部错误"


def test_pagination_and_route_errors(client: TestClient, super_admin: User, users: UserFactory) -> None:
    for _ in range(3):
        users.create("operator")
    h = super_admin.headers
    page = ok_data(client.get(f"{ADMIN_API}/admins", headers=h, params={"page_size": 500}))
    assert page["page_size"] == 100
    body = _err(client.get(f"{ADMIN_API}/admins", headers=h, params={"page_size": 0}), 400)
    assert body["data"][0]["loc"] == ["query", "page_size"]
    _err(client.get(f"{ADMIN_API}/admins", headers=h, params={"page": "x"}), 400)
    _err(client.get(f"{ADMIN_API}/no-such-route", headers=h), 404)


def test_cors_headers(client: TestClient) -> None:
    response = client.options(f"{ADMIN_API}/auth/login", headers={
        "Origin": "http://127.0.0.1:5174", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"


def test_seed_script_idempotent(db: Session) -> None:
    from seeds.seed import run_seed

    context = run_seed(db)
    admin, project = context["admin"], context["project"]
    assert admin.username == settings.seed_admin_username and admin.created_by is None and admin.token_version == 1
    assert db.get(AdminGroup, admin.group_id).code == "super_admin"
    assert project.owner_id == admin.id and project.status == "active" and project.name == "示例项目"

    # 已存在时不覆盖密码、不改组、不改状态；示例项目不改负责人
    admin.password_hash = "changed-hash"
    admin.is_active = False
    other = Admin(username="owner2", password_hash="x", group_id=admin.group_id)
    db.add(other)
    db.commit()
    project.owner_id = other.id
    db.commit()
    again = run_seed(db)
    db.expire_all()
    assert again["admin"].id == admin.id
    assert db.get(Admin, admin.id).password_hash == "changed-hash" and db.get(Admin, admin.id).is_active is False
    assert db.get(Project, project.id).owner_id == other.id
    assert db.scalar(select(func.count(Admin.id)).where(Admin.username == settings.seed_admin_username)) == 1
    assert db.scalar(select(func.count(Project.id))) == 1


def test_seed_script_runs_as_cli(tmp_path: Any) -> None:
    import os
    import subprocess
    import sys

    from conftest import SERVER_DIR

    db_file = tmp_path / "seed.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "REDIS_URL": "redis://127.0.0.1:6379/15"}
    from app.core.database import Base, make_engine

    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    for _ in range(2):
        result = subprocess.run([sys.executable, "seeds/seed.py"], cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
    engine = make_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM admins").scalar() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM projects").scalar() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM admin_permissions").scalar() == 90
    engine.dispose()


def test_bootstrap_runs_under_lock(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    calls: list[str] = []
    monkeypatch.setattr(main, "run_ensure_steps", lambda: calls.append("ran"))
    main.bootstrap()
    assert calls == ["ran"] and redis_client.exists("lock:bootstrap") == 0

    redis_client.set("lock:bootstrap", "other-process", ex=60)
    monkeypatch.setattr(main, "BOOTSTRAP_LOCK_WAIT", 0.3)
    main.bootstrap()                                             # 等锁超时直接继续，不抛异常
    assert calls == ["ran"]


def test_dataclass_scope_is_frozen() -> None:
    from app.services.data_scope_service import SYSTEM_SCOPE

    with pytest.raises(FrozenInstanceError):
        SYSTEM_SCOPE.owner_id = 3  # type: ignore[misc]
    assert replace(SYSTEM_SCOPE, owner_id=3).restricted is True


# =====================================================================
# 补充：逐条规则回归（docs/07 §6.4、§7.5、§7.6、§10；docs/04 §4.1）
# =====================================================================


def test_audit_group_create_update_delete(client: TestClient, db: Session, super_admin: User) -> None:
    """07 §10 第 10 条：创建 / 编辑 / 删除用户组都有审计，target_id 为组 ID（创建时为新组 ID）。"""
    h = super_admin.headers
    created = ok_data(client.post(f"{ADMIN_API}/admin-groups", headers=h, json={"name": "审计组", "data_scope": "all"}))
    log = _logs(db, permission_code="security.groups.create")[-1]
    assert (log.action, log.target_type, log.target_id) == ("create", "admin_group", str(created["id"]))
    assert "审计组" in log.summary

    ok_data(client.put(f"{ADMIN_API}/admin-groups/{created['id']}", headers=h, json={"name": "审计二组", "description": "说明"}))
    log = _logs(db, permission_code="security.groups.update")[-1]
    assert (log.action, log.target_type, log.target_id) == ("update", "admin_group", str(created["id"]))
    assert log.summary.startswith("编辑用户组 审计组") and "审计二组" in log.summary

    assert ok_data(client.delete(f"{ADMIN_API}/admin-groups/{created['id']}", headers=h)) is None
    log = _logs(db, permission_code="security.groups.delete")[-1]
    assert (log.action, log.target_type, log.target_id, log.summary) == ("delete", "admin_group", str(created["id"]), "删除用户组 审计二组")


def test_audit_settings_batch_has_null_target(client: TestClient, db: Session, super_admin: User) -> None:
    ok_data(client.put(f"{ADMIN_API}/settings", headers=super_admin.headers,
                       json={"items": [{"key": "stats_config", "locale": "*", "value": {"rankings_limit": 20}}]}))
    log = _logs(db, permission_code="system.settings.update")[-1]
    assert (log.action, log.target_type, log.target_id, log.summary) == ("update", "setting", None, "更新 setting")


def test_audit_failure_does_not_rollback_business(client: TestClient, db: Session, super_admin: User, operator: User,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """07 §10 第 10 条：审计失败只记错误日志，不回滚业务、不影响响应。"""
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(rbac, "write_audit", boom)
    created = ok_data(client.post(f"{ADMIN_API}/admins", headers=super_admin.headers, json={
        "username": "noaudit01", "password": "Abcdef123", "group_id": operator.admin.group_id,
    }))
    db.expire_all()
    assert db.get(Admin, created["id"]) is not None
    data = ok_data(login(client, operator.username, DEFAULT_PASSWORD))              # 登录审计失败同样不影响签发令牌
    ok_data(client.get(f"{ADMIN_API}/auth/me", headers=bearer(data["token"])))
    assert ok_data(client.post(f"{ADMIN_API}/auth/logout", headers=bearer(data["token"]))) is None
    _err(client.get(f"{ADMIN_API}/auth/me", headers=bearer(data["token"])), 401)
    assert _logs(db) == []


def test_login_lockout_does_not_extend_ttl(client: TestClient, db: Session, operator: User, users: UserFactory) -> None:
    """07 §7.5：锁定期间不 INCR、不续期；不存在的用户名同样计数；密码正确但账号停用（403）不计数。"""
    key = admin_login_key(operator.username)
    for _ in range(settings.admin_login_max_failures):
        _err(login(client, operator.username, "Wrong12345"), 401)
    redis_client.expire(key, 120)                                   # 模拟锁定已过去一段时间
    body = _err(login(client, operator.username, "Wrong12345"), 429)
    assert 0 < body["data"]["retry_after"] <= 120
    assert 0 < redis_client.ttl(key) <= 120                         # 未续期
    assert int(redis_client.get(key)) == settings.admin_login_max_failures

    _err(login(client, "Ghost.User", "Wrong12345"), 401)
    ghost = admin_login_key("ghost.user")
    assert int(redis_client.get(ghost)) == 1 and 0 < redis_client.ttl(ghost) <= 900

    disabled = users.create("operator", username="disabled01", is_active=False)
    _err(login(client, disabled.username, DEFAULT_PASSWORD), 403)
    assert redis_client.exists(admin_login_key(disabled.username)) == 0


def test_token_only_accepted_in_authorization_header(client: TestClient, super_admin: User) -> None:
    """04 §4.1：令牌只放在 Authorization 头，不支持查询参数或 Cookie。"""
    _err(client.get(f"{ADMIN_API}/auth/me", params={"token": super_admin.token}), 401)
    _err(client.get(f"{ADMIN_API}/auth/me", headers={"Authorization": f"Token {super_admin.token}"}), 401)
    client.cookies.set("admin_token", super_admin.token)
    try:
        _err(client.get(f"{ADMIN_API}/auth/me"), 401)
    finally:
        client.cookies.clear()


def test_operation_logs_time_range_is_closed(client: TestClient, db: Session, super_admin: User) -> None:
    """07 §6.4：``start`` / ``end`` 为 ISO 8601 UTC 闭区间。"""
    moment = datetime(2026, 10, 6, 8, 12, 30)
    db.add(AdminOperationLog(admin_id=super_admin.id, permission_code="security.admins.status", action="update_status",
                             target_type="admin", target_id="5", summary="禁用用户 operator01", request_id="r" * 32,
                             ip="203.0.113.0", created_at=moment, updated_at=moment))
    db.commit()
    url = f"{ADMIN_API}/admin-operation-logs"
    h = super_admin.headers
    stamp = "2026-10-06T08:12:30Z"
    assert ok_data(client.get(url, headers=h, params={"start": stamp, "end": stamp}))["total"] == 1
    assert ok_data(client.get(url, headers=h, params={"end": "2026-10-06T08:12:29Z"}))["total"] == 0
    assert ok_data(client.get(url, headers=h, params={"start": "2026-10-06T08:12:31Z", "end": "2026-10-06T09:00:00Z"}))["total"] == 0
    item = ok_data(client.get(url, headers=h, params={"end": "2026-10-06T16:12:30+08:00"}))["items"][0]
    assert item["created_at"] == stamp and item["admin"] == {"id": super_admin.id, "username": "admin", "display_name": "超级管理员"}
    assert item["group_name"] == "超级管理员" and item["target_type"] == "admin" and item["target_id"] == "5"


def test_super_admin_group_scope_all_is_noop(client: TestClient, db: Session, super_admin: User) -> None:
    """``super_admin`` 组只能保持 ``all``：显式提交 ``all`` 允许（不变），其它值 403 且不写审计。"""
    group_id = users_group_id(db, "super_admin")
    item = ok_data(client.put(f"{ADMIN_API}/admin-groups/{group_id}", headers=super_admin.headers, json={"data_scope": "all"}))
    assert item["data_scope"] == "all"
    count = len(_logs(db, permission_code="security.groups.update"))
    body = _err(client.put(f"{ADMIN_API}/admin-groups/{group_id}", headers=super_admin.headers, json={"data_scope": "own"}), 403)
    assert body["data"] is None
    assert len(_logs(db, permission_code="security.groups.update")) == count
    db.expire_all()
    assert db.get(AdminGroup, group_id).data_scope == "all"
