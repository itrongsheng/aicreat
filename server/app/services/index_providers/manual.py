"""人工标记提供器 ``manual``（docs/11 §7.2、§7.4；docs/03 B.22 人工标记行固定写法）。

``POST /admin/links/{id}/mark-index`` 经 ``index_check_service.mark_index`` 调用 ``ManualProvider.result`` 生成结果：
``provider=manual``、``match_mode=manual``、``confidence=1``、``duration_ms=0``、``query_text`` / ``model`` / ``request_id`` /
``ai_task_id`` 为 NULL、``evidence_url`` = 入参、``evidence_json={"note", "source": "manual"}``。

``status`` 只接受确定结论（SEO ``indexed`` / ``not_indexed``，GEO ``cited`` / ``not_cited``），由调用方先行校验。配置为
``manual`` 的引擎不参与自动排程（只能人工标记），``check`` 仅作协议完整性实现：返回 ``unknown`` + ``manual_only``。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import PublishLink
from app.services.index_providers.base import MSG_MANUAL_ONLY, CheckContext, CheckResult

PROVIDER = "manual"


class ManualProvider:
    code = PROVIDER

    def available(self) -> tuple[bool, str | None]:
        return True, None

    def check(self, db: Session, link: PublishLink, engine: str, query_by: list[str], *, ctx: CheckContext) -> CheckResult:
        del db, link, query_by, ctx
        return CheckResult(status="unknown", provider=PROVIDER, evidence={"source": PROVIDER, "engine": engine},
                           error_message=MSG_MANUAL_ONLY)

    @staticmethod
    def result(status: str, *, note: str | None = None, evidence_url: str | None = None) -> CheckResult:
        return CheckResult(
            status=status,
            provider=PROVIDER,
            match_mode="manual",
            query_text=None,
            evidence_url=evidence_url,
            evidence={"note": note, "source": PROVIDER},
            confidence=Decimal(1),
            duration_ms=0,
        )


__all__ = ["PROVIDER", "ManualProvider"]
