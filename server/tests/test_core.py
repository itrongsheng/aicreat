"""core 基础设施单元测试：安全、权限注册表、URL、指纹、存储探测、异常外壳、Redis 助手 / 锁 / 频控。"""

from __future__ import annotations

import os

# 测试固定使用 Redis db 15 与 SQLite；须在导入 app.core 之前设置（conftest 已设置时不覆盖）
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import struct
import time
import zlib
from pathlib import Path

import pytest
import redis
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.core import admin_permissions as ap
from app.core import fingerprint, storage, urls
from app.core.config import Settings, settings
from app.core.exceptions import (
    CODE_CAPABILITY_UNAVAILABLE,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    CODE_TEMPLATE_VARIABLE_MISSING,
    BusinessError,
    field_error,
    invalid_params,
    register_exception_handlers,
)
from app.core.response import fail, ok, paginated
from app.core.security import AUD, DUMMY_PASSWORD_HASH, create_token, decode_token, hash_password, verify_password

# ---------------------------------------------------------------- Redis 夹具


def _redis_db15_available() -> bool:
    from app.core.redis import redis_client

    if int(redis_client.connection_pool.connection_kwargs.get("db", 0)) != 15:
        return False
    try:
        return bool(redis_client.ping())
    except redis.RedisError:
        return False


@pytest.fixture()
def rds():
    if not _redis_db15_available():
        pytest.skip("需要 Redis db 15（REDIS_URL=redis://127.0.0.1:6379/15）")
    from app.core.redis import redis_client

    redis_client.flushdb()
    yield redis_client
    redis_client.flushdb()


# ---------------------------------------------------------------- config


def test_settings_defaults_and_derived(monkeypatch):
    for name in ("ZHIQI_API_KEY", "OSS_ENDPOINT", "STORAGE_MODE", "ZHIQI_BASE_URL", "ALLOWED_ORIGINS", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.admin_jwt_expire_seconds == 7200
    assert s.admin_login_max_failures == 5
    assert s.generate_rate_limit == "60/hour"
    assert s.zhiqi_quota_per_unit == 500000
    assert s.monitor_max_response_bytes == 2097152
    assert s.zhiqi_mock_mode is True
    assert s.use_local_storage is True
    assert s.zhiqi_origin == "https://zhiqiapi.com"
    assert s.media_public_base == "http://127.0.0.1:8100/media"
    assert s.cors_origins == ["http://127.0.0.1:5174", "http://localhost:5174"]


def test_settings_oss_mode(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "oss")
    monkeypatch.setenv("OSS_ENDPOINT", "https://oss.example.com")
    monkeypatch.setenv("OSS_PUBLIC_BASE_URL", "https://cdn.example.com/")
    monkeypatch.setenv("ZHIQI_API_KEY", "sk-test")
    monkeypatch.setenv("SMTP_PORT", "")
    s = Settings(_env_file=None)
    assert s.use_local_storage is False
    assert s.media_public_base == "https://cdn.example.com"
    assert s.zhiqi_mock_mode is False
    assert s.smtp_port == 465
    monkeypatch.setenv("OSS_ENDPOINT", "")
    assert Settings(_env_file=None).use_local_storage is True


def test_env_example_parses_and_matches_root():
    server_example = Path(__file__).resolve().parents[1] / ".env.example"
    root_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert server_example.read_text(encoding="utf-8") == root_example.read_text(encoding="utf-8")
    s = Settings(_env_file=str(server_example))
    assert s.monitor_user_agent == "aicreatLinkMonitor/1.0 (+https://example.com/contact)"
    assert s.seed_admin_username == "admin"
    keys = {line.split("=", 1)[0] for line in server_example.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#") and "=" in line}
    assert {k.lower() for k in keys} == set(Settings.model_fields)


# ---------------------------------------------------------------- security


def test_password_hash_and_verify():
    hashed = hash_password("admin123")
    assert hashed != "admin123"
    assert verify_password("admin123", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("admin123", "")
    assert not verify_password("admin123", "not-a-bcrypt-hash")
    assert not verify_password("x", DUMMY_PASSWORD_HASH)
    long_pw = "密" * 40  # 120 字节，bcrypt 只取前 72 字节
    assert verify_password(long_pw, hash_password(long_pw))


def test_token_roundtrip_and_claims():
    token = create_token(42, 3)
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == "42" and payload["ver"] == 3 and payload["aud"] == AUD == "admin"
    assert payload["exp"] - payload["iat"] == settings.admin_jwt_expire_seconds
    assert decode_token(token + "x") is None
    assert decode_token("garbage") is None


def test_token_wrong_audience_and_expired():
    import jwt

    now = int(time.time())
    wrong_aud = jwt.encode({"sub": "1", "aud": "user", "ver": 0, "iat": now, "exp": now + 60}, settings.admin_jwt_secret, algorithm="HS256")
    assert decode_token(wrong_aud) is None
    expired = jwt.encode({"sub": "1", "aud": "admin", "ver": 0, "iat": now - 100, "exp": now - 10}, settings.admin_jwt_secret, algorithm="HS256")
    assert decode_token(expired) is None
    other_key = jwt.encode({"sub": "1", "aud": "admin", "ver": 0, "iat": now, "exp": now + 60}, "another-secret-key-of-32-bytes!!!", algorithm="HS256")
    assert decode_token(other_key) is None


# ---------------------------------------------------------------- admin_permissions


def test_permission_registry_invariants():
    codes = [p.code for p in ap.PERMISSIONS]
    assert len(codes) == 90
    assert len(set(codes)) == 90
    assert ap.PERMISSION_CODES == set(codes)
    menus = {p.code for p in ap.PERMISSIONS if p.type == "menu"}
    for p in ap.PERMISSIONS:
        assert p.type in ("menu", "action")
        assert p.module == p.code.split(".", 1)[0]
        if p.type == "menu":
            assert p.parent_code is None and p.code.endswith(".view")
        else:
            assert p.parent_code in menus
            assert p.parent_code.rsplit(".", 1)[0] == p.code.rsplit(".", 1)[0]
    for key, deps in ap.PERMISSION_DEPENDENCIES.items():
        assert key in ap.PERMISSION_CODES and deps <= ap.PERMISSION_CODES
    assert set(ap.MODULE_NAMES) == {p.module for p in ap.PERMISSIONS}


def test_resource_sort_and_parent():
    specs = ap._resource("content", "batches", "生成批次", [("cancel", "取消批次"), ("retry", "重试批次")], 250)
    assert [(s.code, s.type, s.parent_code, s.sort) for s in specs] == [
        ("content.batches.view", "menu", None, 250),
        ("content.batches.cancel", "action", "content.batches.view", 251),
        ("content.batches.retry", "action", "content.batches.view", 252),
    ]


def test_system_groups_and_defaults():
    assert [g["code"] for g in ap.SYSTEM_GROUPS] == ["super_admin", "operator", "reviewer", "read_only"]
    assert {g["code"]: g["data_scope"] for g in ap.SYSTEM_GROUPS} == {"super_admin": "all", "operator": "own", "reviewer": "all", "read_only": "all"}
    assert all(g["is_system"] == 1 for g in ap.SYSTEM_GROUPS)
    assert set(ap.DEFAULT_GROUP_PERMISSIONS) <= ap.SYSTEM_GROUP_CODES
    assert "super_admin" not in ap.DEFAULT_GROUP_PERMISSIONS
    for perms in ap.DEFAULT_GROUP_PERMISSIONS.values():
        assert perms <= ap.PERMISSION_CODES
        # 默认权限已满足 view 与跨资源依赖补齐（07 §4.3）
        assert ap.expand_permission_codes(perms) == perms

    operator = ap.DEFAULT_GROUP_PERMISSIONS["operator"]
    assert not (operator & ap.OPERATOR_EXCLUDED)
    assert {"content.keywords.generate", "media.images.generate", "publish.links.create", "monitoring.alerts.handle",
            "ai.tasks.retry", "ai.tasks.cancel", "ai.models.view", "stats.reports.export", "system.upload.create"} <= operator
    assert not operator & {"publish.platforms.create", "ai.models.sync", "ai.routes.update", "ai.usage.reconcile",
                           "stats.reports.recompute", "system.settings.view", "security.admins.view"}

    reviewer = ap.DEFAULT_GROUP_PERMISSIONS["reviewer"]
    assert {"content.contents.review", "content.contents.update", "content.contents.export", "publish.links.view"} <= reviewer
    assert not any(c.startswith("ai.") for c in reviewer)
    assert "content.keywords.generate" not in reviewer and "stats.reports.export" not in reviewer

    read_only = ap.DEFAULT_GROUP_PERMISSIONS["read_only"]
    assert "system.upload.view" in read_only and "system.upload.create" not in read_only
    assert "stats.reports.export" in read_only and "content.contents.export" not in read_only
    assert not any(c.startswith("security.") for c in read_only) and "system.settings.view" not in read_only
    assert all(c.endswith(".view") or c == "stats.reports.export" for c in read_only)


def test_expand_permission_codes():
    expanded = ap.expand_permission_codes(["dashboard.view", "content.keywords.generate", "media.images.generate", "bogus.code"])
    assert expanded == {
        "dashboard.view", "content.keywords.generate", "content.keywords.view", "content.batches.view",
        "media.images.generate", "media.images.view", "media.assets.view",
    }


def test_permission_tree_contains_each_code_once():
    tree = ap.permission_tree()
    seen: list[str] = []
    for module in tree:
        assert module["name"] == ap.MODULE_NAMES[module["module"]]
        for menu in module["items"]:
            seen.append(menu["code"])
            seen.extend(child["code"] for child in menu["children"])
    assert sorted(seen) == sorted(ap.PERMISSION_CODES)
    flat = ap.permission_list()
    assert len(flat) == 90 and [p["sort"] for p in flat] == sorted(p["sort"] for p in flat)


# ---------------------------------------------------------------- urls


def test_normalize_url_examples():
    assert urls.normalize_url("https://www.zhihu.com/question/1/answer/2?utm_source=wechat&spm=a#top") == "https://www.zhihu.com/question/1/answer/2"
    assert (
        urls.normalize_url("https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc&chksm=xyz&scene=126")
        == "https://mp.weixin.qq.com/s?__biz=MzA&idx=1&mid=1&sn=abc"
    )


def test_normalize_url_rules():
    assert urls.normalize_url("HTTPS://Example.COM:443//a//b/?b=2&a=1&UTM_Medium=x") == "https://example.com/a/b?a=1&b=2"
    assert urls.normalize_url("http://example.com:80") == "http://example.com/"
    assert urls.normalize_url("https://example.com/") == "https://example.com/"
    assert urls.normalize_url("https://example.com/%E4%B8%AD%e6%96%87/") == "https://example.com/%E4%B8%AD%E6%96%87"
    assert urls.normalize_url("https://example.com/中文") == "https://example.com/%E4%B8%AD%E6%96%87"
    assert urls.normalize_url("https://例子.测试/a") == "https://xn--fsqu00a.xn--0zwm56d/a"
    assert urls.normalize_url("https://example.com/a?x=&b=1") == "https://example.com/a?b=1&x="
    assert urls.normalize_url("https://example.com/a?xsec_token=abc") == "https://example.com/a"


@pytest.mark.parametrize(
    "bad",
    ["ftp://example.com/a", "javascript:alert(1)", "file:///etc/passwd", "data:text/html,hi", "https://user:pw@example.com/",
     "https://example.com:8080/", "https:///nohost", "", "   ", "https://exa mple.com/", "//example.com/a"],
)
def test_normalize_url_rejects(bad):
    with pytest.raises(urls.InvalidURLError):
        urls.normalize_url(bad)


def test_canonicalize_allow_http_flag():
    assert urls.canonicalize_public_url("http://Example.com//x#frag") == "http://example.com/x#frag"
    with pytest.raises(urls.InvalidURLError):
        urls.canonicalize_public_url("http://example.com/", allow_http=False)
    assert urls.canonicalize_public_url("https://example.com:443/a") == "https://example.com:443/a"


def test_url_hash_and_domain_and_patterns():
    h = urls.url_hash("https://example.com/a")
    assert len(h) == 64 and h == urls.url_hash("https://example.com/a") and h != urls.url_hash("https://example.com/b")
    assert urls.extract_domain("https://WWW.Zhihu.com/question/1") == "zhihu.com"
    assert urls.extract_domain("https://zhuanlan.zhihu.com/p/1") == "zhuanlan.zhihu.com"
    assert urls.extract_domain("not a url") == ""
    patterns = ["^https?://(www\\.|zhuanlan\\.)?zhihu\\.com/", "(bad"]
    assert urls.match_url_patterns("https://ZHUANLAN.zhihu.com/p/1", patterns)
    assert not urls.match_url_patterns("https://example.com/", patterns)
    assert not urls.match_url_patterns("https://example.com/", [])


# ---------------------------------------------------------------- fingerprint


def test_normalize_title():
    assert fingerprint.normalize_title(None) == ""
    assert fingerprint.normalize_title("") == ""
    assert fingerprint.normalize_title("如何选择 AI 写作工具？ - 知乎") == "如何选择ai写作工具"
    assert fingerprint.normalize_title("如何选择 AI 写作工具｜CSDN博客") == "如何选择ai写作工具"
    assert fingerprint.normalize_title("Hello World | Example Site") == "helloworld"
    assert fingerprint.normalize_title("SEO 指南 _ 百家号") == "seo指南"
    # 剩余不足 4 字符时不剥离后缀
    assert fingerprint.normalize_title("AI - 知乎") == "ai知乎"
    # NFKC：全角字母数字与全角连字符
    assert fingerprint.normalize_title("ＡＢＣ１２３ 测试 － 网站") == "abc123测试"


def test_simhash_signed_and_distance():
    text = "人工智能内容生成平台帮助运营团队批量生成关键词、标题与文章，并监控发布链接的收录情况。" * 3
    a = fingerprint.simhash64(text)
    assert -(1 << 63) <= a < (1 << 63)
    assert a == fingerprint.simhash64(text)
    assert fingerprint.hamming_distance(a, a) == 0
    b = fingerprint.simhash64(text.replace("收录", "删除") + " extra english words here")
    far = fingerprint.simhash64("The quick brown fox jumps over the lazy dog near the riverbank every morning.")
    assert fingerprint.hamming_distance(a, b) < fingerprint.hamming_distance(a, far)
    assert fingerprint.simhash64("") == 0
    assert fingerprint.hamming_distance(-1, 0) == 64
    assert fingerprint.hamming_distance(fingerprint.to_signed64(1 << 63), 0) == 1
    assert fingerprint.to_unsigned64(fingerprint.to_signed64((1 << 64) - 1)) == (1 << 64) - 1
    assert fingerprint.text_simhash("短文本") is None
    assert fingerprint.text_simhash(text) == a


def test_simhash_features():
    feats = fingerprint.simhash_features("中文测试 Hello hello 42")
    assert feats["中文测"] == 1 and feats["文测试"] == 1 and feats["hello"] == 2 and feats["42"] == 1


def test_extract_main_text():
    body = "正文段落内容。" * 40
    html = f"""<html><head><title> 页面标题 - 站点 </title><meta property="og:title" content="OG 标题"></head>
    <body><header>站点头部</header><nav>导航菜单</nav><script>var x = 1;</script>
    <article><h1>文章标题</h1><p>{body}</p></article>
    <footer>版权所有</footer><form><input name="q">搜索</form></body></html>"""
    title, text = fingerprint.extract_main_text(html)
    assert title == "OG 标题"
    assert text.startswith("文章标题 正文段落内容")
    for noise in ("站点头部", "导航菜单", "var x", "版权所有", "搜索"):
        assert noise not in text

    title2, text2 = fingerprint.extract_main_text("<html><head><title>只有&amp;标题</title></head><body><main>短</main><div>其它 正文</div></body></html>")
    assert title2 == "只有&标题"
    assert text2 == "短 其它 正文"
    assert fingerprint.extract_main_text("") == (None, "")
    _, long_text = fingerprint.extract_main_text("<p>" + "字" * 50000 + "</p>")
    assert len(long_text) == 40000


# ---------------------------------------------------------------- storage


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", zlib.crc32(chunk)) + b"\x00" * 16


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + b"\xff\xff" + sof + b"\xff\xda\x00\x08" + b"\x00" * 6 + b"\xff\xd9"


def _webp_vp8(width: int, height: int) -> bytes:
    frame = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 10
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(frame)) + b"WEBP" + b"VP8 " + struct.pack("<I", len(frame)) + frame


def _webp_vp8l(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 8
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = b"\x10\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload + b"\x00" * 8


def test_probe_image_size():
    assert storage.probe_image_size(_png(1024, 768)) == (1024, 768)
    assert storage.probe_image_size(_jpeg(640, 480)) == (640, 480)
    assert storage.probe_image_size(_webp_vp8(300, 200)) == (300, 200)
    assert storage.probe_image_size(_webp_vp8l(4000, 3000)) == (4000, 3000)
    assert storage.probe_image_size(_webp_vp8x(16383, 9000)) == (16383, 9000)
    assert storage.probe_image_size(b"GIF89a" + struct.pack("<HH", 32, 16) + b"\x00" * 10) == (32, 16)
    assert storage.probe_image_size(b"") is None
    assert storage.probe_image_size(b"not an image at all") is None
    assert storage.probe_image_size(_png(10, 10)[:20]) is None
    assert storage.probe_image_size(b"\xff\xd8\xff\xe0\x00") is None


def test_sniff_media_type():
    assert storage.sniff_media_type(_png(1, 1)) == "image/png"
    assert storage.sniff_media_type(_jpeg(1, 1)) == "image/jpeg"
    assert storage.sniff_media_type(_webp_vp8(1, 1)) == "image/webp"
    assert storage.sniff_media_type(b"GIF87a\x01\x00\x01\x00") == "image/gif"
    assert storage.sniff_media_type(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00") == "video/mp4"
    assert storage.sniff_media_type(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00") == "video/quicktime"
    assert storage.sniff_media_type(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") is None
    assert storage.sniff_media_type(b"%PDF-1.7") is None
    assert storage.sniff_media_type(b"") is None
    assert storage.extension_for("image/jpeg") == "jpg" and storage.extension_for("video/quicktime") == "mov"


def test_storage_keys_and_public_url():
    key = storage.build_storage_key("images", "png")
    assert key.startswith("media/images/") and key.endswith(".png") and len(key.split("/")) == 5
    for bad in ("../etc/passwd", "/abs/path", "a/../../b", "a\\b", "", "a//b", "c:/x"):
        with pytest.raises(storage.InvalidStorageKey):
            storage.validate_storage_key(bad)
    assert storage.public_url_for("media/images/2026/10/x.png") == f"{settings.media_public_base}/media/images/2026/10/x.png"


def test_local_storage_roundtrip(tmp_path):
    backend = storage.LocalStorage(tmp_path / "store")
    key = "media/images/2026/10/a.png"
    assert backend.save(key, _png(2, 3), "image/png") == key
    assert backend.exists(key) and backend.size(key) == len(_png(2, 3))
    assert storage.probe_image_size(backend.read(key)) == (2, 3)
    src = tmp_path / "upload.part"
    src.write_bytes(b"video-bytes")
    backend.save("media/videos/2026/10/b.mp4", src)
    assert not src.exists() and backend.read("media/videos/2026/10/b.mp4") == b"video-bytes"
    import io

    backend.save("media/uploads/2026/10/c.gif", io.BytesIO(b"GIF89a"))
    with backend.open("media/uploads/2026/10/c.gif") as fh:
        assert fh.read() == b"GIF89a"
    assert backend.delete(key) is True and backend.delete(key) is False
    with pytest.raises(FileNotFoundError):
        backend.open(key)
    with pytest.raises(storage.InvalidStorageKey):
        backend.path_for("../outside.txt")


def test_get_storage_selects_local_when_no_endpoint():
    storage.reset_storage()
    backend = storage.get_storage()
    assert isinstance(backend, storage.LocalStorage)
    assert storage.get_storage() is backend


def test_s3_storage_with_fake_client():
    class FakeS3:
        def __init__(self):
            self.objects: dict[str, tuple[bytes, str]] = {}

        def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
            self.objects[Key] = (Body, ContentType)

        def head_object(self, Bucket, Key):  # noqa: N803
            if Key not in self.objects:
                err = Exception("not found")
                err.response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}
                raise err
            return {"ContentLength": len(self.objects[Key][0])}

        def get_object(self, Bucket, Key):  # noqa: N803
            import io

            self.head_object(Bucket, Key)
            return {"Body": io.BytesIO(self.objects[Key][0])}

        def delete_object(self, Bucket, Key):  # noqa: N803
            self.objects.pop(Key, None)

    fake = FakeS3()
    backend = storage.S3Storage(bucket="b", client=fake)
    backend.save("media/images/2026/10/x.png", b"\x89PNG")
    assert fake.objects["media/images/2026/10/x.png"][1] == "image/png"
    assert backend.exists("media/images/2026/10/x.png") and backend.size("media/images/2026/10/x.png") == 4
    assert backend.read("media/images/2026/10/x.png") == b"\x89PNG"
    assert backend.delete("media/images/2026/10/x.png") and not backend.delete("media/images/2026/10/x.png")
    with pytest.raises(FileNotFoundError):
        backend.open("media/images/2026/10/x.png")


# ---------------------------------------------------------------- response / exceptions


def test_response_helpers():
    assert ok({"a": 1}) == {"code": 0, "message": "ok", "data": {"a": 1}}
    assert ok() == {"code": 0, "message": "ok", "data": None}
    assert fail(404, "资源不存在") == {"code": 404, "message": "资源不存在", "data": None}
    assert fail("资源不存在", 404) == {"code": 404, "message": "资源不存在", "data": None}
    assert paginated([1, 2], 10, 1, 20) == {"code": 0, "message": "ok", "data": {"items": [1, 2], "total": 10, "page": 1, "page_size": 20}}


def test_business_error_defaults():
    assert BusinessError("x").http_status == 400 and BusinessError("x").code == 400
    assert BusinessError("x", code=CODE_NOT_FOUND).http_status == 404
    assert BusinessError("x", code=CODE_TEMPLATE_VARIABLE_MISSING).http_status == 422
    assert BusinessError("x", code=CODE_CAPABILITY_UNAVAILABLE).http_status == 503
    assert BusinessError("x", code=401, http_status=401).http_status == 401


class _Body(BaseModel):
    count: int = Field(ge=1, le=50)
    name: str = Field(max_length=10)
    password: str = Field(min_length=8)


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/items/{item_id}")
    def get_item(item_id: int, page: int = Query(1, ge=1)):
        if item_id == 404:
            raise BusinessError("对象不存在", code=404, http_status=404)
        if item_id == 409:
            raise BusinessError("冲突", code=409, http_status=409, data={"existing_id": 3})
        return ok({"id": item_id, "page": page})

    @app.post("/items")
    def create_item(body: _Body):
        return ok(body.model_dump(exclude={"password"}))

    @app.post("/check")
    def check():
        raise invalid_params(field_error(["body", "url"], "URL 只允许 http 或 https 协议", "value_error", "ftp://x" + "y" * 300))

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    return app


def test_exception_envelopes(monkeypatch):
    client = TestClient(_app(), raise_server_exceptions=False)

    assert client.get("/items/1").json() == {"code": 0, "message": "ok", "data": {"id": 1, "page": 1}}

    r = client.get("/items/404")
    assert r.status_code == 404 and r.json() == {"code": 404, "message": "对象不存在", "data": None}

    r = client.get("/items/409")
    assert r.status_code == 409 and r.json()["data"] == {"existing_id": 3}

    r = client.get("/items/abc")
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == 400 and body["message"] == "参数错误"
    assert body["data"][0]["loc"] == ["path", "item_id"] and body["data"][0]["input"] == "abc"
    assert set(body["data"][0]) == {"loc", "msg", "type", "input"}

    r = client.get("/items/1?page=0")
    assert r.json()["data"][0]["msg"] == "不能小于 1"

    r = client.post("/items", json={"count": 80, "name": "x" * 300, "password": "short"})
    assert r.status_code == 400
    errors = {tuple(e["loc"]): e for e in r.json()["data"]}
    assert errors[("body", "count")] == {"loc": ["body", "count"], "msg": "不能大于 50", "type": "less_than_equal", "input": 80}
    assert errors[("body", "name")]["type"] == "string_too_long" and len(errors[("body", "name")]["input"]) == 200
    assert errors[("body", "password")]["type"] == "string_too_short" and "input" not in errors[("body", "password")]

    r = client.post("/items", json={})
    missing = r.json()["data"]
    assert all(e["type"] == "missing" and e.get("input") is None for e in missing)
    assert "input" not in next(e for e in missing if e["loc"] == ["body", "password"])

    r = client.post("/items", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == 400

    r = client.post("/check")
    item = r.json()["data"][0]
    assert r.status_code == 400 and item["loc"] == ["body", "url"] and len(item["input"]) == 200

    r = client.get("/nope")
    assert r.status_code == 404 and r.json() == {"code": 404, "message": "资源不存在", "data": None}

    r = client.delete("/items/1")
    assert r.status_code == 405 and r.json()["code"] == 405

    monkeypatch.setattr(settings, "dev_mode", True)
    r = client.get("/boom")
    assert r.status_code == 500 and r.json()["code"] == 500 and "kaboom" in r.json()["data"]["traceback"]
    monkeypatch.setattr(settings, "dev_mode", False)
    r = client.get("/boom")
    assert r.status_code == 500 and r.json() == {"code": 500, "message": "服务器内部错误", "data": None}


# ---------------------------------------------------------------- redis / locks / ratelimit


def test_cache_helpers(rds):
    from app.core.redis import cache_delete, cache_delete_prefix, cache_get_json, cache_set_json

    assert cache_get_json("cache:missing") is None
    assert cache_set_json("cache:settings:a:*", {"x": 1, "中文": [1, 2]}, ttl=60)
    assert cache_get_json("cache:settings:a:*") == {"x": 1, "中文": [1, 2]}
    assert 0 < rds.ttl("cache:settings:a:*") <= 60
    for i in range(1200):
        rds.set(f"cache:routes:text:{i}", "1")
    rds.set("cache:platforms:all", "[]")
    assert cache_delete_prefix("cache:routes:") == 1200
    assert rds.exists("cache:platforms:all") == 1
    assert cache_delete("cache:platforms:all", "cache:none") == 1
    rds.set("cache:bad", "{not json")
    assert cache_get_json("cache:bad") is None and not rds.exists("cache:bad")


def test_cache_helpers_degrade_when_redis_down(monkeypatch):
    from app.core import redis as core_redis

    broken = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, decode_responses=True)
    monkeypatch.setattr(core_redis, "redis_client", broken)
    assert core_redis.cache_get_json("k") is None
    assert core_redis.cache_set_json("k", {"a": 1}, 10) is False
    assert core_redis.cache_delete("k") == 0
    assert core_redis.cache_delete_prefix("cache:") == 0
    assert core_redis.redis_ping() is False


def test_locks(rds):
    from app.core.locks import LockTimeout, acquire_lock, extend_lock, release_lock, with_lock

    token = acquire_lock("lock:test", 30)
    assert token and acquire_lock("lock:test", 30) is None
    assert not release_lock("lock:test", "other-token")
    assert extend_lock("lock:test", 100, token) and rds.ttl("lock:test") > 30
    assert not extend_lock("lock:test", 100, "other-token")
    assert release_lock("lock:test", token) and not rds.exists("lock:test")

    with with_lock("lock:bootstrap", ttl=60) as tok:
        assert rds.get("lock:bootstrap") == tok
        with pytest.raises(LockTimeout):
            with with_lock("lock:bootstrap", ttl=60, wait_seconds=0.3):
                pass
    assert not rds.exists("lock:bootstrap")


def test_rate_limit_sliding_window(rds):
    from app.core.ratelimit import check_rate_limit, parse_rate

    assert parse_rate("60/hour") == (60, 3600)
    assert parse_rate("20/hour") == (20, 3600)
    assert parse_rate("30/ Minute") == (30, 60)
    assert parse_rate("10/5m") == (10, 300)
    with pytest.raises(ValueError):
        parse_rate("abc")
    for i in range(3):
        assert check_rate_limit("rate:generate:1", "3/hour") == i + 1
    with pytest.raises(BusinessError) as info:
        check_rate_limit("rate:generate:1", 3, 3600)
    assert info.value.code == CODE_RATE_LIMITED and info.value.http_status == 429
    assert info.value.message == "请求过于频繁"
    assert 3590 <= info.value.data["retry_after"] <= 3600
    assert 0 < rds.ttl("rate:generate:1") <= 3600
    assert check_rate_limit("rate:generate:2", 0, 60) == 0  # 0 表示不限


def test_admin_login_lockout(rds):
    from app.core import ratelimit

    name = "  Admin "
    assert ratelimit.admin_login_key(name) == "rate:admin_login:admin"
    ratelimit.check_admin_login_allowed(name)
    for i in range(settings.admin_login_max_failures):
        assert ratelimit.record_admin_login_failure(name) == i + 1
    assert 0 < rds.ttl("rate:admin_login:admin") <= 900
    with pytest.raises(BusinessError) as info:
        ratelimit.check_admin_login_allowed("ADMIN")
    assert info.value.http_status == 429 and info.value.message == "登录失败次数过多，请稍后再试"
    assert 0 < info.value.data["retry_after"] <= 900
    ratelimit.clear_admin_login_failures(name)
    ratelimit.check_admin_login_allowed(name)
    assert ratelimit.admin_login_retry_after(name) == 0


def test_enforce_interval(rds):
    from app.core.ratelimit import enforce_interval

    assert enforce_interval("domain:last_fetch:example.com", 0.3) < 0.05
    assert rds.exists("domain:last_fetch:example.com")
    waited = enforce_interval("domain:last_fetch:example.com", 0.3)
    assert 0.15 <= waited <= 1.0
    assert enforce_interval("domain:last_fetch:other.com", 0.3) < 0.05
    assert enforce_interval("domain:last_fetch:example.com", 0) == 0.0
