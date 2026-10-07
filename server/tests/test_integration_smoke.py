"""``scripts/integration_smoke.py`` 内容链路（docs/09 §14.2）的进程内回归：用 ``TestClient`` 替代 httpx、每次请求前同步排空
``queue:ai_tasks``（代替 worker），依次执行脚本的「登录 → 建项目 → 关键词 → 标题 → 内容」步骤，断言脚本按 §14.2 校验
版本 1 / SEO 要素 / FAQ，并实际执行「重写小节（版本 2）→ 提审 → 审核通过」。

完整冒烟（图片、回填、收录 / 删除检测、报表）需要 API / worker / monitor_worker 三个进程，见 docs/06「自动冒烟脚本」。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redis import redis_client
from app.models import AiTask, ContentVersion
from app.services import ai_catalog_service, ai_task_service
from app.services import ai_gateway_service as gw
from tests.conftest import User

pytestmark = pytest.mark.usefixtures("redis_required")

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "integration_smoke.py"


def load_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aicreat_integration_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def drain_queue(db: Session) -> None:
    """模拟 worker：``LPOP queue:ai_tasks`` → ``claim`` → ``process_one``，直到队列为空（处理器链式入队的后续根任务一并执行）。"""
    while True:
        raw = redis_client.lpop(gw.QUEUE_AI_TASKS)
        if raw is None:
            break
        if ai_task_service.claim(db, int(raw), "w:smoke"):
            ai_task_service.process_one(int(raw), worker_id="w:smoke")
    db.expire_all()


class InProcessApi:
    """与脚本 ``Api`` 同接口（``request`` / ``get`` / ``post`` / ``token`` / ``last``），请求经 ``TestClient`` 发往进程内应用；
    每次请求前排空任务队列，使脚本的轮询等待在首轮探测即可看到终态。"""

    def __init__(self, smoke: ModuleType, client: TestClient, db: Session) -> None:
        self.smoke = smoke
        self.client = client
        self.db = db
        self.token: str | None = None
        self.last: dict[str, Any] | None = None
        self.calls: list[tuple[str, str]] = []

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
        drain_queue(self.db)
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        response = self.client.request(method, self.smoke.API_PREFIX + path, json=json_body, params=params, headers=headers)
        payload = response.json()
        self.calls.append((method, path))
        self.last = {"method": method, "url": path, "status": response.status_code, "body": payload}
        if response.status_code not in tuple(expect_status):
            raise self.smoke.SmokeError(f"{method} {path} 返回 HTTP {response.status_code}：{payload}")
        if envelope:
            return payload
        if payload.get("code") != 0:
            raise self.smoke.SmokeError(f"{method} {path} 业务码 {payload.get('code')}：{payload.get('message')}")
        return payload.get("data")

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json_body=body, **kwargs)


def test_smoke_content_chain_asserts_version_seo_faq_and_rewrites_section(
    client: TestClient, db: Session, super_admin: User, system_templates: dict[str, Any],
) -> None:
    ai_catalog_service.sync_models(db)
    smoke = load_smoke()
    api = InProcessApi(smoke, client, db)
    args = SimpleNamespace(username=super_admin.username, password=super_admin.password)
    runner = smoke.Smoke(api, args)
    for action in (runner.step_login, runner.step_project, runner.step_keywords, runner.step_titles, runner.step_content):
        action()

    content_id = runner.content_id
    rewrite_calls = [c for c in api.calls if c == ("POST", f"/admin/contents/{content_id}/rewrite")]
    assert len(rewrite_calls) == 1
    versions = db.scalars(select(ContentVersion).where(ContentVersion.content_id == content_id).order_by(ContentVersion.version_no)).all()
    assert [(v.version_no, v.source) for v in versions] == [(1, "generate"), (2, "expand")]
    rewrite_root = db.scalar(select(AiTask).where(
        AiTask.operation == "content_rewrite", AiTask.target_id == content_id, AiTask.root_task_id.is_(None),
    ))
    assert rewrite_root is not None and rewrite_root.status == "succeeded"
    assert smoke.json.loads(rewrite_root.input_json)["scope"] == "section"
    assert api.calls[-1] == ("POST", f"/admin/contents/{content_id}/approve")
    detail = api.get(f"/admin/contents/{content_id}")
    assert (detail["status"], detail["version_count"], detail["current_version"]["source"]) == ("approved", 2, "expand")


def test_smoke_content_assertions_reject_missing_seo_and_faq(client: TestClient, db: Session, super_admin: User) -> None:
    """``_assert_generated`` 对版本 / SEO 要素 / FAQ 缺失直接失败（docs/09 §14.2「版本 1 存在，SEO 要素与 FAQ 非空」）。"""
    smoke = load_smoke()
    runner = smoke.Smoke(InProcessApi(smoke, client, db), SimpleNamespace())
    good = {
        "version_count": 1, "current_version": {"version_no": 1, "source": "generate"}, "summary": "s", "seo_title": "t",
        "seo_description": "d", "seo_keywords": ["k"], "faq": [{"q": "q", "a": "a"}],
    }
    runner._assert_generated(good)  # noqa: SLF001
    for broken in ({"faq": []}, {"seo_keywords": None}, {"seo_title": ""}, {"version_count": 0, "current_version": None}):
        with pytest.raises(smoke.SmokeError):
            runner._assert_generated({**good, **broken})  # noqa: SLF001
