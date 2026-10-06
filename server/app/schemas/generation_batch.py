"""生成批次接口的响应模型（docs/04 §6.12、§7.4；docs/09 §9、§11）。

``/admin/generation-batches*`` 没有请求体（``cancel`` / ``retry`` 为空 ``POST``），本模块只描述响应结构，供 OpenAPI 文档与
``packages/shared/src/types.ts``（``GenerationBatch`` / ``BatchTaskSummary`` / ``BatchCreatedResult``）对照；实际响应由
``generation_service.batch_item`` / ``batch_detail`` / ``task_summary`` / ``batch_created_response`` 组装。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models import (
    AiTaskOperation,
    AiTaskStatus,
    AiTaskTargetType,
    BatchKind,
    BatchStatus,
    ErrorCategory,
    Protocol,
)

__all__ = ["BatchCreatedOut", "BatchTaskSummaryOut", "GenerationBatchOut", "QuotaWarningOut"]


class QuotaWarningOut(BaseModel):
    scope: str = Field(description="daily / project_monthly")
    limit: int
    used: int
    percent: int


class BatchCreatedOut(BaseModel):
    """``POST /admin/keywords/generate``、``POST /admin/titles/generate`` 的 ``data``。"""

    batch_id: int
    quota_warning: QuotaWarningOut | None = None


class BatchTaskSummaryOut(BaseModel):
    """批次详情 ``tasks[]``：根任务摘要（``model_override`` 取根任务 ``input.model``，未覆盖为 ``null``）。"""

    id: int
    operation: AiTaskOperation
    target_type: AiTaskTargetType | None
    target_id: int | None
    parent_task_id: int | None
    status: AiTaskStatus
    model: str | None
    model_override: str | None
    protocol: Protocol | None
    candidate_index: int
    attempt_count: int
    last_error_category: ErrorCategory | None
    last_error_message: str | None
    request_id: str | None
    progress: int
    started_at: str | None
    finished_at: str | None


class GenerationBatchOut(BaseModel):
    """批次对象；详情另附 ``tasks[]``。"""

    id: int
    project_id: int
    kind: BatchKind
    status: BatchStatus
    input: dict[str, Any]
    template_id: int | None
    template_version: int | None
    requested_count: int
    produced_count: int
    task_total: int
    task_done: int
    task_failed: int
    error_summary: str | None
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    created_by: int | None
    created_at: str
    updated_at: str
    tasks: list[BatchTaskSummaryOut] | None = None
