"""界面语言判定：``zh-CN`` / ``en-US``，非法值回退 ``zh-CN``。

后端不做文案本地化（接口 ``message`` 固定中文），语言只用于选取按 ``locale`` 存储的配置行（``system_info``）。
"""

from __future__ import annotations

from typing import get_args

from app.models import Locale

SUPPORTED_LOCALES: tuple[str, ...] = get_args(Locale)
DEFAULT_LOCALE: Locale = "zh-CN"


def _match(value: str) -> Locale | None:
    tag = value.strip().replace("_", "-").lower()
    if not tag:
        return None
    for locale in SUPPORTED_LOCALES:
        if tag == locale.lower():
            return locale  # type: ignore[return-value]
    primary = tag.split("-", 1)[0]
    if primary == "zh":
        return "zh-CN"
    if primary == "en":
        return "en-US"
    return None


def normalize_locale(value: str | None, default: Locale = DEFAULT_LOCALE) -> Locale:
    """把 ``lang`` 参数或 ``Accept-Language`` 头归一为 ``zh-CN`` / ``en-US``。

    - 大小写与 ``_`` / ``-`` 不敏感，``zh`` / ``zh-TW`` / ``zh-Hans`` → ``zh-CN``，``en`` / ``en-GB`` → ``en-US``；
    - ``Accept-Language`` 形式（``fr-FR,en-US;q=0.8``）按 q 值从高到低取第一个受支持的语言；
    - 空值与非法值回退 ``default``（``zh-CN``）。
    """
    if not value or not isinstance(value, str):
        return default
    candidates: list[tuple[float, int, str]] = []
    for index, part in enumerate(value.split(",")[:20]):
        pieces = part.strip().split(";")
        tag = pieces[0].strip()
        q = 1.0
        for param in pieces[1:]:
            name, _, raw = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(raw.strip())
                except ValueError:
                    q = 0.0
        if tag and q > 0:
            candidates.append((-q, index, tag))
    for _q, _i, tag in sorted(candidates):
        matched = _match(tag)
        if matched:
            return matched
    return default


def pick_locale(lang: str | None = None, accept_language: str | None = None) -> Locale:
    """``get_locale`` 的判定顺序：``lang`` 参数 > ``Accept-Language`` > ``zh-CN``。"""
    if lang and _match(lang.split(",")[0].split(";")[0]):
        return normalize_locale(lang)
    return normalize_locale(accept_language)
