#!/usr/bin/env python
"""aicreat Mock 模式端到端冒烟脚本（docs/06「自动冒烟脚本」、docs/11 §16.4、docs/12 §14）。

流程：健康检查 → 登录 → 建冒烟项目 → 关键词 → 标题 → 内容（生成：版本 1、SEO 要素与 FAQ 非空 → 重写小节：版本 2 → 审核）→
图片 → 回填（公网 URL，``published_at`` 置于 31 天前以触发 ``scheduled`` 收录轮次）→ SEO / GEO 检测（``scheduled`` 轮次后仍全部未命中时手动复查，最多 3 次）→ 删除检测 /
告警分支（回填返回 404 的公网 URL：基线 ``suspected_deleted`` → 手动检测 ``deleted`` → ``link_deleted`` 告警确认、解决）→
重算今日 ``daily_stats`` → 总览断言（``seo_index_rate`` / ``geo_cite_rate`` 非 null 且 > 0、``ai_calls > 0``、``cost_cny >= 0``）。

前置条件：API、``app.worker``、``app.monitor_worker`` 三个进程都在运行（compose 下即 ``server`` / ``worker`` /
``monitor-worker`` 三个容器），后端为 Mock 模式（``ZHIQI_API_KEY`` 为空），``monitor_worker`` 能访问公网。

只依赖标准库与 httpx（后端依赖已包含）。任何一步失败即以非零状态退出，并打印最后一次响应与相关 ``request_id``。

用法::

    python scripts/integration_smoke.py [--base-url http://127.0.0.1:8100] [--username admin] [--password ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import httpx
except ImportError:  # pragma: no cover - 运行环境缺依赖时给出明确提示
    sys.stderr.write("缺少依赖 httpx：请在 server/ 的虚拟环境中运行（pip install -e .）\n")
    raise SystemExit(2) from None

API_PREFIX = "/api/v1"
DEFAULT_BASE_URL = "http://127.0.0.1:8100"
DEFAULT_PUBLIC_URL = "https://example.com/"
DEFAULT_DELETION_URL = "https://httpbin.org/status/404"
SEED_DEFAULT_USERNAME = "admin"
SEED_DEFAULT_PASSWORD = "admin123"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"  # server/.env（本机开发）
RUN_MARKER_PARAM = "aicreat_smoke"

# 超时（秒）：Mock 下典型耗时见 docs/06「预期耗时（Mock）」，这里留足余量
BATCH_TIMEOUT = 180
CONTENT_TIMEOUT = 300
IMAGE_TIMEOUT = 240
LINK_CHECK_TIMEOUT = 150
SCHEDULED_ROUND_TIMEOUT = 480  # index_check.scan_interval_seconds=300 + 8 个引擎执行
MANUAL_INDEX_TIMEOUT = 180
RECHECK_MAX = 3
DELETED_RULES = {"http_404", "http_410", "http_451"}

T = TypeVar("T")


# =====================================================================
# 输出
# =====================================================================

_STARTED = time.monotonic()


def _elapsed() -> str:
    seconds = int(time.monotonic() - _STARTED)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def log(message: str) -> None:
    print(f"[{_elapsed()}]   {message}", flush=True)


def step(index: int, total: int, title: str) -> None:
    print(f"\n[{_elapsed()}] ==> 第 {index}/{total} 步：{title}", flush=True)


def _short(value: Any, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字符，已截断）"


class SmokeError(Exception):
    """冒烟断言失败；``context`` 为附加诊断信息（相关 request_id、任务摘要等）。"""

    def __init__(self, message: str, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context = dict(context or {})


# =====================================================================
# 凭据缺省值
# =====================================================================


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 ``.env`` 解析：``KEY=value``，支持行尾 `` # 注释`` 与单 / 双引号（与 docs/05 §2.1 书写规则一致）。"""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        values[key] = value
    return values


def default_credentials() -> tuple[str, str]:
    """``SEED_ADMIN_USERNAME`` / ``SEED_ADMIN_PASSWORD``：环境变量优先，其次 ``server/.env``，最后取 seed 缺省值。"""
    file_values = _parse_env_file(ENV_FILE)
    username = os.environ.get("SEED_ADMIN_USERNAME") or file_values.get("SEED_ADMIN_USERNAME") or SEED_DEFAULT_USERNAME
    password = os.environ.get("SEED_ADMIN_PASSWORD") or file_values.get("SEED_ADMIN_PASSWORD") or SEED_DEFAULT_PASSWORD
    return username, password


# =====================================================================
# HTTP 客户端
# =====================================================================


class Api:
    """``{base_url}/api/v1`` 客户端：校验 HTTP 状态与业务码 ``code == 0``，记录最后一次响应供失败时打印。"""

    def __init__(self, base_url: str, *, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        host = (urlsplit(self.base_url).hostname or "").lower()
        loopback = host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")
        # 本机地址不走 HTTP(S)_PROXY（开发机常配置了全局代理）
        self.client = httpx.Client(base_url=self.base_url + API_PREFIX, timeout=timeout, trust_env=not loopback)
        self.token: str | None = None
        self.last: dict[str, Any] | None = None

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        expect_status: Iterable[int] = (200,),
        auth: bool = True,
        envelope: bool = False,
    ) -> Any:
        headers = {"Accept": "application/json", "X-Request-Id": uuid.uuid4().hex}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.client.request(method, path, json=json_body, params=params, headers=headers)
        except httpx.HTTPError as exc:
            self.last = {"method": method, "url": f"{self.base_url}{API_PREFIX}{path}", "status": None,
                         "request_id": headers["X-Request-Id"], "body": f"{type(exc).__name__}: {exc}"}
            raise SmokeError(f"请求失败：{method} {path}（{type(exc).__name__}: {exc}）；请确认 --base-url 与 API 进程") from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        self.last = {
            "method": method,
            "url": str(response.request.url),
            "status": response.status_code,
            "request_id": response.headers.get("X-Request-Id") or headers["X-Request-Id"],
            "body": payload,
        }
        expected = tuple(expect_status)
        if response.status_code not in expected:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise SmokeError(f"{method} {path} 返回 HTTP {response.status_code}（期望 {'/'.join(map(str, expected))}）"
                             + (f"：{message}" if message else ""))
        if not isinstance(payload, dict) or "code" not in payload:
            raise SmokeError(f"{method} {path} 响应不是统一外壳 {{code, message, data}}")
        if envelope:
            return payload
        if payload.get("code") != 0:
            raise SmokeError(f"{method} {path} 业务码 {payload.get('code')}：{payload.get('message')}")
        return payload.get("data")

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json_body=body, **kwargs)


# =====================================================================
# 工具
# =====================================================================


def wait_for(
    description: str,
    probe: Callable[[], T | None],
    *,
    timeout: float,
    interval: float = 3.0,
    progress: Callable[[], str] | None = None,
    progress_every: float = 30.0,
) -> T:
    """轮询 ``probe`` 直到返回非 ``None``；超时抛 ``SmokeError``。``probe`` 可直接抛 ``SmokeError`` 表示提前失败。"""
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    next_report = started + progress_every
    while True:
        result = probe()
        if result is not None:
            return result
        now = time.monotonic()
        if now >= deadline:
            raise SmokeError(f"等待超时（{int(timeout)}s）：{description}")
        if now >= next_report:
            extra = f"；{progress()}" if progress else ""
            log(f"仍在等待 {description}（已 {int(now - started)}s）{extra}")
            next_report = now + progress_every
        time.sleep(min(interval, max(0.5, deadline - now)))


def with_run_marker(url: str, run_id: str) -> str:
    """在 URL query 末尾追加 ``aicreat_smoke=<run_id>``：链接按归一化 URL 全局唯一，重复运行冒烟时避免 409 重复回填。"""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    pairs.append((RUN_MARKER_PARAM, run_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(pairs), ""))


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def items_of(page: Any) -> list[dict[str, Any]]:
    if isinstance(page, dict) and isinstance(page.get("items"), list):
        return [item for item in page["items"] if isinstance(item, dict)]
    if isinstance(page, list):
        return [item for item in page if isinstance(item, dict)]
    return []


# =====================================================================
# 冒烟流程
# =====================================================================


class Smoke:
    TOTAL_STEPS = 12

    def __init__(self, api: Api, args: argparse.Namespace) -> None:
        self.api = api
        self.args = args
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]
        self.project_id = 0
        self.content_id = 0
        self.keyword_ids: list[int] = []
        self.title_id = 0
        self.link_id = 0
        self.deleted_link_id = 0

    # ------------------------------------------------------------ 公共等待

    def wait_batch(self, batch_id: int, label: str) -> dict[str, Any]:
        def probe() -> dict[str, Any] | None:
            batch = self.api.get(f"/admin/generation-batches/{batch_id}")
            status = batch.get("status")
            if status == "succeeded":
                return batch
            if status in ("partial", "failed", "cancelled"):
                tasks = batch.get("tasks") or []
                raise SmokeError(
                    f"{label}批次 #{batch_id} 结束于 {status}（期望 succeeded）",
                    {"error_summary": batch.get("error_summary"), "tasks": tasks},
                )
            return None

        batch = wait_for(f"{label}批次 #{batch_id} 完成", probe, timeout=BATCH_TIMEOUT,
                         progress=lambda: f"当前状态 {self._status_of_last()}")
        log(f"{label}批次 #{batch_id} succeeded：produced_count={batch.get('produced_count')}，"
            f"task_done={batch.get('task_done')}/{batch.get('task_total')}")
        return batch

    def _status_of_last(self) -> Any:
        body = (self.api.last or {}).get("body")
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            return body["data"].get("status")
        return None

    def link(self, link_id: int) -> dict[str, Any]:
        return self.api.get(f"/admin/links/{link_id}")

    def link_checks(self, link_id: int) -> list[dict[str, Any]]:
        return items_of(self.api.get(f"/admin/links/{link_id}/checks", params={"page_size": 100}))

    def index_checks(self, link_id: int) -> list[dict[str, Any]]:
        return items_of(self.api.get(f"/admin/links/{link_id}/index-checks", params={"page_size": 100}))

    # ------------------------------------------------------------ 步骤

    def run(self) -> None:
        steps: list[tuple[str, Callable[[], None]]] = [
            ("健康检查（API / worker / monitor_worker / Mock 模式）", self.step_health),
            ("登录", self.step_login),
            ("建冒烟项目", self.step_project),
            ("生成关键词并采用", self.step_keywords),
            ("生成标题并采用", self.step_titles),
            ("生成内容、重写小节并审核通过", self.step_content),
            ("生成封面图片", self.step_image),
            ("回填公网链接（published_at = 31 天前）", self.step_backfill),
            ("SEO / GEO 收录检测（scheduled 轮次 + 最多 3 次手动复查）", self.step_index_checks),
            ("删除检测与告警（回填 404 公网地址 → deleted → link_deleted 确认 / 解决）", self.step_deletion),
            ("重算今日 daily_stats", self.step_recompute),
            ("报表总览断言", self.step_overview),
        ]
        assert len(steps) == self.TOTAL_STEPS
        for index, (title, action) in enumerate(steps, start=1):
            step(index, self.TOTAL_STEPS, title)
            action()

    def step_health(self) -> None:
        data = self.api.get("/health", auth=False, expect_status=(200, 503), envelope=True).get("data") or {}
        log(f"status={data.get('status')} db={data.get('db')} redis={data.get('redis')} "
            f"zhiqi_mode={data.get('zhiqi_mode')} version={data.get('version')}")
        if not data.get("db") or not data.get("redis"):
            raise SmokeError("数据库或 Redis 不可用（GET /api/v1/health 返回 503）")
        if data.get("zhiqi_mode") != "mock":
            raise SmokeError("后端不是 Mock 模式（ZHIQI_API_KEY 非空）：本脚本只用于 Mock 冒烟，真实模式请按 docs/05 §11 第 12 项手工验证")
        workers = data.get("workers") or {}
        dead = [name for name in ("worker", "monitor_worker") if not (workers.get(name) or {}).get("alive")]
        if dead:
            raise SmokeError(f"worker 进程未运行：{', '.join(dead)}（需同时运行 app.worker 与 app.monitor_worker）",
                             {"workers": workers})
        for warning in data.get("warnings") or []:
            log(f"警告：{warning}")

    def step_login(self) -> None:
        data = self.api.post("/admin/auth/login", {"username": self.args.username, "password": self.args.password}, auth=False)
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise SmokeError("登录响应缺少 token")
        self.api.token = token
        me = self.api.get("/admin/auth/me")
        log(f"已登录 {me.get('username')}（data_scope={me.get('data_scope')}，zhiqi_mode={me.get('zhiqi_mode')}，"
            f"权限码 {len(me.get('permissions') or [])} 个）")
        if me.get("zhiqi_mode") != "mock":
            raise SmokeError("GET /admin/auth/me 的 zhiqi_mode 不是 mock")

    def step_project(self) -> None:
        body = {
            "name": f"冒烟-{self.run_id}",
            "slug": f"smoke-{self.run_id}",
            "industry": "智能家居",
            "audience": "一二线城市 25~40 岁家庭用户",
            "brand_name": "aicreat",
            "description": "integration_smoke.py 自动创建的冒烟项目",
        }
        project = self.api.post("/admin/projects", body)
        self.project_id = int(project["id"])
        log(f"项目 #{self.project_id} {project.get('name')}（slug={project.get('slug')}）")

    def step_keywords(self) -> None:
        data = self.api.post("/admin/keywords/generate", {
            "project_id": self.project_id, "seeds": ["智能门锁", "全屋智能"], "count": 20, "audience": "首次装修的年轻家庭",
        })
        batch_id = int(data["batch_id"])
        log(f"关键词批次 #{batch_id} 已入队")
        self.wait_batch(batch_id, "关键词")
        keywords = items_of(self.api.get("/admin/keywords", params={
            "project_id": self.project_id, "batch_id": batch_id, "status": "candidate", "page_size": 100,
        }))
        if not keywords:
            raise SmokeError("关键词批次成功但列表为空")
        ids = [int(k["id"]) for k in keywords[:3]]
        result = self.api.post("/admin/keywords/batch-status", {"ids": ids, "action": "adopt"})
        log(f"生成 {len(keywords)} 条候选关键词，采用 {ids}（updated={result.get('updated')}）")
        self.keyword_ids = ids

    def step_titles(self) -> None:
        keyword_id = self.keyword_ids[0]
        data = self.api.post("/admin/titles/generate", {
            "project_id": self.project_id, "keyword_ids": [keyword_id], "count": 5, "style": "tutorial",
        })
        batch_id = int(data["batch_id"])
        log(f"标题批次 #{batch_id} 已入队（关键词 #{keyword_id}）")
        self.wait_batch(batch_id, "标题")
        titles = items_of(self.api.get("/admin/titles", params={
            "project_id": self.project_id, "keyword_id": keyword_id, "status": "candidate", "page_size": 100,
        }))
        if not titles:
            raise SmokeError("标题批次成功但列表为空")
        title = self.api.post(f"/admin/titles/{int(titles[0]['id'])}/adopt")
        self.title_id = int(title["id"])
        log(f"生成 {len(titles)} 条候选标题，采用 #{self.title_id}「{title.get('title')}」")

    def step_content(self) -> None:
        data = self.api.post("/admin/contents/generate", {
            "project_id": self.project_id, "title_ids": [self.title_id], "outline_first": True, "target_word_count": 1500,
            "include_faq": True, "include_seo_meta": True, "format": "markdown",
        })
        batch_id = int(data["batch_id"])
        content_ids = data.get("content_ids") or []
        if not content_ids:
            raise SmokeError("内容生成响应缺少 content_ids")
        self.content_id = int(content_ids[0])
        log(f"内容 #{self.content_id} 生成中（批次 #{batch_id}）")

        def probe() -> dict[str, Any] | None:
            task = self.api.get(f"/admin/contents/{self.content_id}/task")
            status = (task or {}).get("status")
            if status in ("failed", "cancelled"):
                raise SmokeError(f"内容根任务 #{task.get('task_id')} {status}：{task.get('error_category')} {task.get('error_message')}",
                                 {"task": task})
            if status == "succeeded":
                content = self.api.get(f"/admin/contents/{self.content_id}")
                if content.get("status") == "ready":
                    return content
            return None

        content = wait_for(f"内容 #{self.content_id} 生成完成（generating → ready）", probe, timeout=CONTENT_TIMEOUT)
        log(f"内容 ready：word_count={content.get('word_count')}，seo_title={_short(content.get('seo_title'), 60)}")
        self._assert_generated(content)
        self._rewrite_section(content)
        reviewed = self.api.post(f"/admin/contents/{self.content_id}/submit-review", expect_status=(200,))
        if reviewed.get("status") == "reviewing":
            reviewed = self.api.post(f"/admin/contents/{self.content_id}/approve", {"note": "冒烟脚本自动审核"})
        if reviewed.get("status") != "approved":
            raise SmokeError(f"内容审核后状态为 {reviewed.get('status')}（期望 approved）")
        log("内容已审核通过（approved）")

    def _assert_generated(self, content: Mapping[str, Any]) -> None:
        """docs/09 §14.2：版本 1 存在（``source=generate``），SEO 要素与 FAQ 非空。"""
        current = content.get("current_version") or {}
        if content.get("version_count") != 1 or current.get("version_no") != 1 or current.get("source") != "generate":
            raise SmokeError(
                f"内容 #{self.content_id} 生成后版本异常：version_count={content.get('version_count')}，"
                f"current_version={current}（期望 version_no=1、source=generate）",
            )
        empty = [name for name in ("summary", "seo_title", "seo_description", "seo_keywords", "faq") if not content.get(name)]
        if empty:
            raise SmokeError(f"内容 #{self.content_id} 生成后以下字段为空：{', '.join(empty)}", {"content_id": self.content_id})
        log(f"版本 1（source=generate）存在；seo_keywords={len(content['seo_keywords'])} 个，faq={len(content['faq'])} 条")

    def _rewrite_section(self, content: Mapping[str, Any]) -> None:
        """docs/09 §14.2「重写小节（版本 2）」：扩写第 1 个小节，轮询任务至 ``succeeded``，断言版本 2（``source=expand``）且回到 ``ready``。"""
        if not content.get("outline"):
            raise SmokeError(f"内容 #{self.content_id} 没有大纲，无法按小节重写")
        data = self.api.post(f"/admin/contents/{self.content_id}/rewrite",
                             {"mode": "expand", "scope": "section", "section_index": 1, "instruction": "补充选购注意事项"})
        task_id = int(data["task_id"])
        log(f"重写小节 #1（expand）已入队：根任务 #{task_id}")

        def probe() -> dict[str, Any] | None:
            task = self.api.get(f"/admin/contents/{self.content_id}/task") or {}
            if task.get("task_id") != task_id:
                return None
            status = task.get("status")
            if status in ("failed", "cancelled", "expired"):
                raise SmokeError(f"重写根任务 #{task_id} {status}：{task.get('error_category')} {task.get('error_message')}",
                                 {"task": task})
            if status == "succeeded":
                rewritten = self.api.get(f"/admin/contents/{self.content_id}")
                if rewritten.get("status") == "ready":
                    return rewritten
            return None

        rewritten = wait_for(f"内容 #{self.content_id} 小节重写完成", probe, timeout=CONTENT_TIMEOUT)
        current = rewritten.get("current_version") or {}
        if rewritten.get("version_count") != 2 or current.get("version_no") != 2 or current.get("source") != "expand":
            raise SmokeError(
                f"重写后版本异常：version_count={rewritten.get('version_count')}，current_version={current}"
                "（期望 version_no=2、source=expand）",
            )
        log(f"重写完成：版本 2（source=expand），word_count={content.get('word_count')} → {rewritten.get('word_count')}")

    def step_image(self) -> None:
        data = self.api.post("/admin/media/images/generate", {
            "project_id": self.project_id, "content_id": self.content_id, "usage_type": "cover", "count": 1,
            "resolution": "1080p", "aspect_ratio": "16:9", "from_content_prompt": True,
        })
        asset_ids = data.get("asset_ids") or []
        if not asset_ids:
            raise SmokeError("图片生成响应缺少 asset_ids")
        asset_id = int(asset_ids[0])
        log(f"图片资产 #{asset_id} 已入队（任务 {data.get('task_ids')}）；Mock 约 30~40 秒")
        seen: list[str] = []

        def probe() -> dict[str, Any] | None:
            asset = self.api.get(f"/admin/media/assets/{asset_id}")
            status = asset.get("status")
            if not seen or seen[-1] != status:
                seen.append(str(status))
            if status == "ready":
                return asset
            if status in ("failed", "expired", "deleted"):
                task = self.api.get(f"/admin/media/assets/{asset_id}/task")
                raise SmokeError(f"图片资产 #{asset_id} {status}：{asset.get('error_category')} {asset.get('error_message')}",
                                 {"task": task})
            return None

        asset = wait_for(f"图片资产 #{asset_id} 转存完成（ready）", probe, timeout=IMAGE_TIMEOUT, interval=5.0,
                         progress=lambda: f"状态轨迹 {' → '.join(seen)}")
        if not asset.get("url"):
            raise SmokeError(f"图片资产 #{asset_id} ready 但 url 为空")
        log(f"图片 ready（{' → '.join(seen)}）：{asset.get('url')}（{asset.get('width')}×{asset.get('height')}）")

    def step_backfill(self) -> None:
        url = with_run_marker(self.args.public_url, self.run_id)
        published_at = iso_utc(datetime.now(timezone.utc) - timedelta(days=31))
        data = self.api.post("/admin/links", {
            "content_id": self.content_id, "url": url, "publish_account": "aicreat 冒烟",
            "published_at": published_at, "note": "integration_smoke 收录分支",
        })
        link = data.get("link") or {}
        self.link_id = int(link["id"])
        log(f"链接 #{self.link_id} 已回填：{url}（published_at={published_at}，queued={data.get('queued')}）")
        baseline = self._wait_check(self.link_id, "baseline")
        log(f"基线检测：result_status={baseline.get('result_status')} matched_rule={baseline.get('matched_rule')} "
            f"http_status={baseline.get('http_status')}")
        if baseline.get("result_status") != "alive":
            raise SmokeError(
                f"公网链接基线检测结果为 {baseline.get('result_status')}（{baseline.get('matched_rule')}），期望 alive："
                "monitor_worker 需能访问公网，或用 --public-url 指定一个可访问且稳定返回 200 的公网地址",
                {"check": baseline},
            )

    def _wait_check(self, link_id: int, check_type: str, *, after_ids: set[int] | None = None) -> dict[str, Any]:
        known = after_ids or set()

        def probe() -> dict[str, Any] | None:
            for check in self.link_checks(link_id):
                if check.get("check_type") == check_type and int(check["id"]) not in known:
                    return check
            return None

        return wait_for(f"链接 #{link_id} 的 {check_type} 删除检测记录", probe, timeout=LINK_CHECK_TIMEOUT)

    # ------------------------------------------------------------ 收录检测

    @staticmethod
    def _expected_engines(link: Mapping[str, Any], kinds: Iterable[str]) -> set[tuple[str, str]]:
        """自动检测涉及的 ``(kind, engine)``：启用且提供器不是 ``manual``（docs/11 ``schedulable_engines``）。"""
        engines = link.get("engines") or {}
        expected: set[tuple[str, str]] = set()
        for kind in kinds:
            for engine in engines.get(kind) or []:
                if engine.get("enabled") and engine.get("provider") != "manual" and engine.get("engine"):
                    expected.add((kind, str(engine["engine"])))
        return expected

    @staticmethod
    def _assert_index_results(checks: Iterable[Mapping[str, Any]]) -> None:
        bad = [c for c in checks if c.get("result_status") == "unknown"]
        if bad:
            raise SmokeError(
                f"{len(bad)} 条收录检测结果为 unknown（期望 Mock 下全部为确定结论）",
                {"unknown_checks": [{k: c.get(k) for k in ("id", "kind", "engine", "provider", "check_type", "error_category",
                                                          "error_message", "request_id", "ai_task_id")} for c in bad]},
            )
        not_mock = [c for c in checks if not isinstance(c.get("evidence"), dict) or c["evidence"].get("source") != "mock"]
        if not_mock:
            raise SmokeError(
                f"{len(not_mock)} 条收录检测的 evidence.source 不是 mock",
                {"checks": [{k: c.get(k) for k in ("id", "kind", "engine", "provider", "request_id", "evidence")} for c in not_mock]},
            )

    def step_index_checks(self) -> None:
        link = self.link(self.link_id)
        expected = self._expected_engines(link, ("seo", "geo"))
        if not any(kind == "seo" for kind, _ in expected) or not any(kind == "geo" for kind, _ in expected):
            raise SmokeError("SEO 或 GEO 没有启用的自动检测引擎（检查 seo_providers / geo_engines 配置）", {"engines": link.get("engines")})
        log(f"自动检测引擎 {len(expected)} 个：{', '.join(f'{k}:{e}' for k, e in sorted(expected))}")
        log("等待 monitor_worker 执行 scheduled 轮次（index_check.scan_interval_seconds 默认 300s）")

        def scheduled_done() -> list[dict[str, Any]] | None:
            current = self.link(self.link_id)
            if int(current.get("index_checks_done") or 0) < 1:
                return None
            checks = [c for c in self.index_checks(self.link_id) if c.get("check_type") == "scheduled"]
            covered = {(str(c.get("kind")), str(c.get("engine"))) for c in checks}
            return checks if expected <= covered else None

        scheduled = wait_for(
            "scheduled 收录检测轮次完成", scheduled_done, timeout=SCHEDULED_ROUND_TIMEOUT, interval=10.0, progress_every=60.0,
            progress=lambda: f"next_index_check_at={self.link(self.link_id).get('next_index_check_at')}",
        )
        self._assert_index_results(scheduled)
        self._log_index_summary("scheduled", scheduled)

        for attempt in range(1, RECHECK_MAX + 1):
            link = self.link(self.link_id)
            missing = [kind for kind, flag in (("seo", link.get("seo_indexed_any")), ("geo", link.get("geo_cited_any"))) if not flag]
            if not missing:
                break
            log(f"{'/'.join(missing)} 仍全部未命中，手动复查第 {attempt}/{RECHECK_MAX} 次")
            self._manual_index_check(missing, self._expected_engines(link, missing))

        link = self.link(self.link_id)
        log(f"seo_indexed_any={link.get('seo_indexed_any')} geo_cited_any={link.get('geo_cited_any')} "
            f"index_checks_done={link.get('index_checks_done')}")
        if not link.get("seo_indexed_any") or not link.get("geo_cited_any"):
            raise SmokeError(f"手动复查 {RECHECK_MAX} 次后 SEO / GEO 仍全部未命中",
                             {"seo_status": link.get("seo_status"), "geo_status": link.get("geo_status")})

    def _manual_index_check(self, kinds: list[str], expected: set[tuple[str, str]]) -> None:
        before = {int(c["id"]) for c in self.index_checks(self.link_id)}
        deadline = time.monotonic() + MANUAL_INDEX_TIMEOUT
        while True:
            result = self.api.post(f"/admin/links/{self.link_id}/index-check", {"kinds": kinds})
            if result.get("queued"):
                break
            reason = result.get("reason")
            if reason != "already_queued" or time.monotonic() >= deadline:
                raise SmokeError(f"手动收录检测未入队：{reason}")
            log("收录检测已在队列（already_queued），20 秒后重试")
            time.sleep(20)

        def probe() -> list[dict[str, Any]] | None:
            fresh = [c for c in self.index_checks(self.link_id) if int(c["id"]) not in before and c.get("check_type") == "manual"]
            covered = {(str(c.get("kind")), str(c.get("engine"))) for c in fresh}
            return fresh if expected <= covered else None

        fresh = wait_for(f"手动收录检测（{'/'.join(kinds)}）完成", probe, timeout=MANUAL_INDEX_TIMEOUT, interval=5.0)
        self._assert_index_results(fresh)
        self._log_index_summary("manual", fresh)

    @staticmethod
    def _log_index_summary(check_type: str, checks: list[dict[str, Any]]) -> None:
        parts = [f"{c.get('kind')}:{c.get('engine')}={c.get('result_status')}" for c in sorted(
            checks, key=lambda c: (str(c.get("kind")), str(c.get("engine"))))]
        log(f"{check_type} 检测 {len(checks)} 条（均非 unknown，evidence.source=mock）：{', '.join(parts)}")

    # ------------------------------------------------------------ 删除检测 / 告警

    def step_deletion(self) -> None:
        url = with_run_marker(self.args.deletion_url, self.run_id)
        data = self.api.post("/admin/links", {
            "content_id": self.content_id, "url": url, "publish_account": "aicreat 冒烟", "note": "integration_smoke 删除检测分支",
        })
        link = data.get("link") or {}
        self.deleted_link_id = link_id = int(link["id"])
        log(f"链接 #{link_id} 已回填：{url}（alive_status={link.get('alive_status')}，queued={data.get('queued')}）")

        baseline = self._wait_check(link_id, "baseline")
        log(f"基线检测：result_status={baseline.get('result_status')} matched_rule={baseline.get('matched_rule')} "
            f"http_status={baseline.get('http_status')}")
        if baseline.get("result_status") != "suspected_deleted" or baseline.get("matched_rule") not in DELETED_RULES:
            raise SmokeError(
                f"404 链接基线检测为 {baseline.get('result_status')}（{baseline.get('matched_rule')}），期望 suspected_deleted + http_404："
                "确认 monitor_worker 能访问该地址，或用 --deletion-url 换一个稳定返回 404 / 410 / 451 的公网地址",
                {"check": baseline},
            )
        current = self.link(link_id)
        if current.get("alive_status") != "suspected_deleted":
            raise SmokeError(f"基线后链接 alive_status={current.get('alive_status')}（期望 suspected_deleted）")

        result = self.api.post(f"/admin/links/{link_id}/check")
        if not result.get("queued"):
            raise SmokeError(f"手动删除检测未入队：{result.get('reason')}")
        manual = self._wait_check(link_id, "manual", after_ids={int(baseline["id"])})
        log(f"手动检测：result_status={manual.get('result_status')} previous_status={manual.get('previous_status')} "
            f"applied_status={manual.get('applied_status')} matched_rule={manual.get('matched_rule')}")
        if manual.get("applied_status") != "deleted":
            raise SmokeError(f"手动检测 applied_status={manual.get('applied_status')}（期望 deleted）", {"check": manual})
        current = self.link(link_id)
        if current.get("alive_status") != "deleted":
            raise SmokeError(f"手动检测后链接 alive_status={current.get('alive_status')}（期望 deleted）")

        alerts = items_of(self.api.get("/admin/alerts", params={
            "alert_type": "link_deleted", "target_type": "publish_link", "target_id": link_id, "page_size": 100,
        }))
        open_alerts = [a for a in alerts if a.get("status") == "open"]
        if not open_alerts:
            raise SmokeError(f"未找到链接 #{link_id} 的 open 状态 link_deleted 告警", {"alerts": alerts})
        alert = open_alerts[0]
        if alert.get("severity") != "warning":
            raise SmokeError(f"link_deleted 告警 severity={alert.get('severity')}（期望 warning）", {"alert": alert})
        alert_id = int(alert["id"])
        log(f"告警 #{alert_id}：{alert.get('alert_type')} / {alert.get('severity')} / {alert.get('status')}"
            f"（dedupe_key={alert.get('dedupe_key')}）")
        acked = self.api.post(f"/admin/alerts/{alert_id}/acknowledge")
        if acked.get("status") != "acknowledged":
            raise SmokeError(f"确认后告警状态为 {acked.get('status')}（期望 acknowledged）")
        resolved = self.api.post(f"/admin/alerts/{alert_id}/resolve", {"note": "冒烟验证：目标 URL 固定返回 404"})
        if resolved.get("status") != "resolved":
            raise SmokeError(f"解决后告警状态为 {resolved.get('status')}（期望 resolved）")
        log(f"告警 #{alert_id} 已确认并解决（resolved_by={resolved.get('resolved_by')}）")

    # ------------------------------------------------------------ 报表

    def step_recompute(self) -> None:
        probe = self.api.get("/admin/stats/overview", params={"project_id": self.project_id, "range": "7d"})
        meta = probe.get("meta") or {}
        today = meta.get("end_date")
        if not today:
            raise SmokeError("总览 meta.end_date 为空，无法确定今日（stats_config.timezone）")
        for attempt in range(1, 6):
            result = self.api.post("/admin/stats/recompute", {"start_date": today, "end_date": today})
            skipped = result.get("skipped") or []
            log(f"重算 {today}（{meta.get('timezone')}）：days={result.get('days')} rows_upserted={result.get('rows_upserted')} "
                f"skipped={skipped} duration_ms={result.get('duration_ms')}")
            if today not in skipped:
                return
            log(f"今日聚合锁被 monitor_worker 占用，5 秒后重试（{attempt}/5）")
            time.sleep(5)
        raise SmokeError(f"重算 {today} 连续 5 次被跳过（lock:monitor:daily_stats:{today} 被占用）")

    def step_overview(self) -> None:
        data = self.api.get("/admin/stats/overview", params={"project_id": self.project_id, "range": "30d"})
        meta = data.get("meta") or {}
        kpis = data.get("kpis") or {}
        log(f"meta：range={meta.get('range')} {meta.get('start_date')}~{meta.get('end_date')} today_source={meta.get('today_source')} "
            f"cached={meta.get('cached')} warnings={meta.get('warnings')}")
        shown = ("keywords_created", "titles_created", "contents_created", "links_backfilled", "links_alive", "links_deleted",
                 "seo_index_rate", "geo_cite_rate", "ai_calls", "ai_success_rate", "tokens_total", "cost_cny",
                 "images_generated", "alerts_opened", "alerts_resolved")
        log("kpis：" + ", ".join(f"{key}={kpis.get(key)}" for key in shown))
        failures: list[str] = []
        for key in ("seo_index_rate", "geo_cite_rate"):
            value = kpis.get(key)
            if not isinstance(value, (int, float)) or value <= 0:
                failures.append(f"{key}={value}（期望非 null 且 > 0）")
        ai_calls = kpis.get("ai_calls")
        if not isinstance(ai_calls, (int, float)) or ai_calls <= 0:
            failures.append(f"ai_calls={ai_calls}（期望 > 0）")
        cost = kpis.get("cost_cny")
        if not isinstance(cost, (int, float)) or cost < 0:
            failures.append(f"cost_cny={cost}（期望 >= 0）")
        if failures:
            raise SmokeError("报表断言失败：" + "；".join(failures), {"meta": meta})


# =====================================================================
# 入口
# =====================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    username, password = default_credentials()
    parser = argparse.ArgumentParser(
        prog="integration_smoke.py",
        description=(
            "aicreat Mock 模式端到端冒烟：登录 → 关键词 → 标题 → 内容 → 图片 → 回填 → 检测（含 404 删除检测 / 告警分支）→ "
            "重算 → 报表。前置：API、app.worker、app.monitor_worker 三进程运行，后端为 Mock 模式，monitor_worker 可访问公网。"
        ),
        epilog=(
            "示例：python scripts/integration_smoke.py --base-url http://127.0.0.1:8101；"
            "compose：docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000 "
            "--password '<新密码>'。回填的两个 URL 会追加 query 参数 aicreat_smoke=<运行 ID>，使重复运行不触发重复回填 409。"
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"API 根地址，只写 scheme + 主机 + 端口，不带 /api/v1（缺省 {DEFAULT_BASE_URL}）")
    parser.add_argument("--username", default=username,
                        help="登录账号（缺省取环境变量 SEED_ADMIN_USERNAME，其次 server/.env，当前缺省值：%(default)s）")
    parser.add_argument("--password", default=password,
                        help="登录密码（缺省取环境变量 SEED_ADMIN_PASSWORD，其次 server/.env；超管改密后必须显式传入）")
    parser.add_argument("--public-url", default=DEFAULT_PUBLIC_URL,
                        help=f"收录分支回填的公网 URL，须稳定返回 200（缺省 {DEFAULT_PUBLIC_URL}）")
    parser.add_argument("--deletion-url", default=DEFAULT_DELETION_URL,
                        help=(f"删除检测 / 告警分支回填的公网 URL，须稳定返回 404 / 410 / 451（缺省 {DEFAULT_DELETION_URL}）；"
                              "所在网络无法访问 httpbin.org 时用它换成其它可访问的公网 404 地址"))
    parser.add_argument("--timeout", type=float, default=60.0, help="单个 HTTP 请求超时秒数（缺省 %(default)s）")
    args = parser.parse_args(argv)
    for name in ("public_url", "deletion_url"):
        value = getattr(args, name)
        if urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname:
            parser.error(f"--{name.replace('_', '-')} 必须是 http(s) 公网 URL：{value}")
    if not args.username or not args.password:
        parser.error("缺少登录凭据：请传入 --username / --password 或设置 SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD")
    return args


def _print_failure(api: Api, exc: SmokeError) -> None:
    err = sys.stderr
    err.write(f"\n[{_elapsed()}] 冒烟失败：{exc}\n")
    if exc.context:
        err.write(f"相关信息：{_short(exc.context, 4000)}\n")
        request_ids = sorted(set(re.findall(r'"request_id":\s*"([^"]+)"', json.dumps(exc.context, ensure_ascii=False, default=str))))
        if request_ids:
            err.write(f"相关 request_id：{', '.join(request_ids)}\n")
    if api.last:
        last = api.last
        err.write(f"最后一次响应：{last.get('method')} {last.get('url')} → HTTP {last.get('status')}"
                  f"（X-Request-Id: {last.get('request_id')}）\n")
        err.write(f"{_short(last.get('body'), 4000)}\n")
    err.flush()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]  # Windows GBK 控制台不因个别字符崩溃
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    api = Api(args.base_url, timeout=args.timeout)
    smoke = Smoke(api, args)
    print(f"aicreat 冒烟：base_url={api.base_url} username={args.username} run_id={smoke.run_id}", flush=True)
    try:
        smoke.run()
    except SmokeError as exc:
        _print_failure(api, exc)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断\n")
        return 130
    except Exception as exc:  # noqa: BLE001 - 意外错误同样打印最后一次响应并非零退出
        _print_failure(api, SmokeError(f"{type(exc).__name__}: {exc}"))
        return 1
    finally:
        api.close()
    print(f"\n[{_elapsed()}] 冒烟通过：项目 #{smoke.project_id}，内容 #{smoke.content_id}，"
          f"链接 #{smoke.link_id}（收录分支）/ #{smoke.deleted_link_id}（删除分支）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
