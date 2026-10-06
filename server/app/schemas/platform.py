"""发布平台接口的请求模型（docs/04 §6.16；docs/11 §5.2、§5.3、§5.4；docs/03 B.19）。

- ``PlatformCreate``（``POST /admin/platforms``）/ ``PlatformUpdate``（``PUT /admin/platforms/{id}``，只写出现的字段；
  ``code`` 创建后不可改，出现且与原值不同时由 service 返回 400）；
- ``url_patterns`` / ``redirect_markers``：Python ``re`` 正则，逐条 ``re.compile`` 校验，非法返回 400
  ``loc=["body","url_patterns",<序号>]``、``msg="正则无法编译：…"``、``input``=该正则；
- ``deleted_markers``：纯文本（非正则），单条 4~100 字符、≤ 50 条；
- ``fetch_config``：``{user_agent, headers, timeout_seconds, respect_robots, allow_http}``，缺失键取
  ``monitoring_config.link_check`` 同名默认值；``headers`` 键忽略大小写，拒绝 ``cookie`` / ``authorization`` /
  ``proxy-authorization``，只允许 ``Accept-Language`` / ``Referer`` / ``X-*``；``user_agent`` 非空时 ≤ 200 字符且须含 ``aicreat``。
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator

CODE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
MAX_PATTERNS = 50
MAX_MARKERS = 50
MARKER_MIN_CHARS = 4
MARKER_MAX_CHARS = 100
MAX_PATTERN_CHARS = 500
MAX_USER_AGENT_CHARS = 200
MAX_HEADER_VALUE_CHARS = 500
MAX_HEADERS = 20
USER_AGENT_REQUIRED_SUBSTRING = "aicreat"
FORBIDDEN_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})
ALLOWED_HEADERS = frozenset({"accept-language", "referer"})
# fetch_config 中平台可覆盖的键（max_response_bytes / max_redirects 只在全局 monitoring_config 中调整，docs/11 §15）
FETCH_CONFIG_KEYS = ("user_agent", "headers", "timeout_seconds", "respect_robots", "allow_http")


def check_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"正则无法编译：{exc}") from exc
    return value


def header_allowed(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered in FORBIDDEN_HEADERS:
        return False
    return lowered in ALLOWED_HEADERS or (lowered.startswith("x-") and len(lowered) > 2)


RegexPattern = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PATTERN_CHARS), AfterValidator(check_regex)
]
DeletedMarker = Annotated[str, StringConstraints(strip_whitespace=True, min_length=MARKER_MIN_CHARS, max_length=MARKER_MAX_CHARS)]
PlatformCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=CODE_PATTERN)]
PlatformName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
PlatformNameEn = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Icon = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
HomeUrl = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
UrlText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlatformFetchConfig(_Body):
    """平台抓取覆盖项；未出现（或为 ``null``）的键取 ``monitoring_config.link_check`` 同名默认值。"""

    user_agent: str | None = None
    headers: dict[str, str] | None = None
    timeout_seconds: float | None = Field(None, ge=1, le=60)
    respect_robots: bool | None = None
    allow_http: bool | None = None

    @field_validator("user_agent")
    @classmethod
    def _user_agent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return ""                                          # 空字符串表示不覆盖
        if len(value) > MAX_USER_AGENT_CHARS:
            raise ValueError(f"User-Agent 长度不能超过 {MAX_USER_AGENT_CHARS} 个字符")
        if USER_AGENT_REQUIRED_SUBSTRING not in value:
            raise ValueError("User-Agent 覆盖值必须包含 aicreat（保持可识别）")
        if any(ch in value for ch in ("\r", "\n")):
            raise ValueError("User-Agent 不能包含换行")
        return value

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > MAX_HEADERS:
            raise ValueError(f"请求头最多 {MAX_HEADERS} 个")
        cleaned: dict[str, str] = {}
        seen: set[str] = set()
        for name, raw in value.items():
            key = str(name).strip()
            lowered = key.lower()
            if lowered in FORBIDDEN_HEADERS:
                raise ValueError(f"不允许设置请求头 {key}（禁止注入 Cookie / 认证头）")
            if not header_allowed(key) or not re.fullmatch(r"[A-Za-z0-9-]+", key):
                raise ValueError(f"不允许设置请求头 {key}（只允许 Accept-Language、Referer 与 X-*）")
            if lowered in seen:
                raise ValueError(f"请求头 {key} 重复（键忽略大小写）")
            seen.add(lowered)
            text = str(raw)
            if len(text) > MAX_HEADER_VALUE_CHARS or "\r" in text or "\n" in text:
                raise ValueError(f"请求头 {key} 的值不合法（≤ {MAX_HEADER_VALUE_CHARS} 字符且不含换行）")
            cleaned[key] = text.strip()
        return cleaned

    def stored(self) -> dict[str, Any]:
        """写入 ``fetch_config_json`` 的值：只保留显式给出的非空键（空 ``user_agent`` / 空 ``headers`` 视为不覆盖）。"""
        data = self.model_dump(exclude_none=True)
        if not data.get("user_agent"):
            data.pop("user_agent", None)
        if not data.get("headers"):
            data.pop("headers", None)
        return data


class _PlatformFields(_Body):
    @field_validator("icon", "home_url", mode="before", check_fields=False)
    @classmethod
    def _blank(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PlatformCreate(_PlatformFields):
    code: PlatformCode
    name: PlatformName
    name_en: PlatformNameEn
    icon: Icon | None = None
    home_url: HomeUrl | None = None
    url_patterns: list[RegexPattern] = Field(default_factory=list, max_length=MAX_PATTERNS)
    deleted_markers: list[DeletedMarker] = Field(default_factory=list, max_length=MAX_MARKERS)
    redirect_markers: list[RegexPattern] = Field(default_factory=list, max_length=MAX_PATTERNS)
    fetch_config: PlatformFetchConfig = Field(default_factory=PlatformFetchConfig)
    is_active: bool = True
    sort: int = Field(0, ge=-100000, le=100000)


class PlatformUpdate(_PlatformFields):
    """编辑：只写出现的字段；``code`` 可原样回传（与原值不同 → 400）；``fetch_config`` 整体替换。"""

    code: PlatformCode | None = None
    name: PlatformName | None = None
    name_en: PlatformNameEn | None = None
    icon: Icon | None = None
    home_url: HomeUrl | None = None
    url_patterns: list[RegexPattern] | None = Field(None, max_length=MAX_PATTERNS)
    deleted_markers: list[DeletedMarker] | None = Field(None, max_length=MAX_MARKERS)
    redirect_markers: list[RegexPattern] | None = Field(None, max_length=MAX_PATTERNS)
    fetch_config: PlatformFetchConfig | None = None
    is_active: bool | None = None
    sort: int | None = Field(None, ge=-100000, le=100000)


class PlatformUrlBody(_Body):
    """``POST /admin/platforms/detect`` 与 ``POST /admin/platforms/{id}/test`` 的 ``{url}``。"""

    url: UrlText
