"""管理员密码哈希与 JWT（docs/07-admin-rbac.md §7.1）。

JWT 为 HS256，密钥 ``ADMIN_JWT_SECRET``，claims 固定 ``sub`` / ``aud="admin"`` / ``ver`` / ``iat`` / ``exp``。
"""

import time
import warnings

import bcrypt
import jwt

from app.core.config import settings

try:  # PyJWT ≥ 2.10：短密钥逐次告警；改为启动时由 settings.jwt_secret_is_weak 统一提示
    from jwt.warnings import InsecureKeyLengthWarning

    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
except ImportError:  # pragma: no cover
    pass

AUD = "admin"
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"aicreat-dummy", bcrypt.gensalt()).decode("utf-8")   # 账号不存在时登录也对其 checkpw 一次，耗时与真实校验一致


def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:72]          # bcrypt 上限 72 字节


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bool(hashed) and bcrypt.checkpw(_to_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_token(subject: int, token_version: int) -> str:
    now = int(time.time())
    payload = {"sub": str(subject), "aud": AUD, "ver": token_version, "iat": now, "exp": now + settings.admin_jwt_expire_seconds}
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.admin_jwt_secret, algorithms=["HS256"], audience=AUD)
    except jwt.PyJWTError:
        return None
