"""SEO 收录 / GEO 引用检测提供器（docs/11 §7、§8；docs/02 ``services/index_providers/``）。

- ``get_seo_provider(code)``：``zhiqi_web_search`` / ``baidu_ai_search`` / ``bing_webmaster`` / ``google_search_console`` /
  ``manual``；未知 code 抛 ``KeyError``；
- ``get_geo_engine(code)``：当前固定返回 ``geo_engine.ZhiqiModelEngine``（``provider="zhiqi_model"``），``code`` 为
  ``geo_engines.engines[].code``（引擎差异全部在配置中：``model`` / ``protocol`` / ``extra`` / ``parse`` / ``timeout_seconds``）。

提供器只负责「调用 + 解析 + 返回 ``CheckResult``」，不写库；``index_checks`` 插入与 ``publish_links`` 回写由
``index_check_service`` 完成（§9）。
"""

from __future__ import annotations

from app.services.index_providers.baidu_ai_search import BaiduAiSearchProvider
from app.services.index_providers.base import CheckContext, CheckResult, GeoEngine, SeoProvider, match_citations
from app.services.index_providers.bing_webmaster import BingWebmasterProvider
from app.services.index_providers.geo_engine import ZhiqiModelEngine
from app.services.index_providers.google_search_console import GoogleSearchConsoleProvider
from app.services.index_providers.manual import ManualProvider
from app.services.index_providers.zhiqi_web_search import ZhiqiWebSearchProvider

SEO_PROVIDER_CODES: tuple[str, ...] = (
    "zhiqi_web_search", "baidu_ai_search", "bing_webmaster", "google_search_console", "manual",
)
GEO_PROVIDER = "zhiqi_model"

_SEO_FACTORIES = {
    "zhiqi_web_search": ZhiqiWebSearchProvider,
    "baidu_ai_search": BaiduAiSearchProvider,
    "bing_webmaster": BingWebmasterProvider,
    "google_search_console": GoogleSearchConsoleProvider,
    "manual": ManualProvider,
}


def get_seo_provider(code: str) -> SeoProvider:
    """按 ``seo_provider`` 枚举值返回提供器实例；未知 code 抛 ``KeyError``。"""
    factory = _SEO_FACTORIES[code]
    return factory()


def get_geo_engine(code: str) -> GeoEngine:
    """GEO 引擎实现：当前所有引擎都经 zhiqiapi 联网模型提问（``ZhiqiModelEngine``）。"""
    del code
    return ZhiqiModelEngine()


__all__ = [
    "GEO_PROVIDER",
    "SEO_PROVIDER_CODES",
    "CheckContext",
    "CheckResult",
    "GeoEngine",
    "SeoProvider",
    "get_geo_engine",
    "get_seo_provider",
    "match_citations",
]
