"""后台权限码注册表（纯数据，取值以 docs/07-admin-rbac.md §4、§5 为准）。

``admin_rbac_service.ensure_rbac_seed`` 据此补齐 ``admin_permissions`` 行与系统用户组，
``api.deps.require_permission`` 据 ``PERMISSION_CODES`` 在导入时校验权限码。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

PermissionType = Literal["menu", "action"]


@dataclass(frozen=True)
class PermissionSpec:
    code: str
    module: str
    name: str
    type: PermissionType
    parent_code: str | None
    sort: int


def _resource(module: str, resource: str, name: str, actions: list[tuple[str, str]], sort: int) -> list[PermissionSpec]:
    """生成一个资源的 ``menu`` 型 ``module.resource.view`` 与若干 ``action`` 型权限（``sort`` 依次 +1）。"""
    view_code = f"{module}.{resource}.view"
    specs = [PermissionSpec(view_code, module, name, "menu", None, sort)]
    for index, (action, label) in enumerate(actions, start=1):
        specs.append(PermissionSpec(f"{module}.{resource}.{action}", module, label, "action", view_code, sort + index))
    return specs


PERMISSIONS: list[PermissionSpec] = [
    PermissionSpec("dashboard.view", "dashboard", "查看控制台", "menu", None, 100),
    *_resource("content", "projects", "项目管理", [("create", "新增项目"), ("update", "编辑项目"), ("status", "归档或恢复项目"), ("delete", "删除项目")], 200),
    *_resource("content", "prompt_templates", "Prompt 模板", [("create", "新增模板"), ("update", "编辑模板"), ("publish", "发布或归档模板"), ("delete", "删除模板")], 210),
    *_resource("content", "keywords", "关键词", [("generate", "生成关键词"), ("create", "新增关键词"), ("import", "导入关键词"), ("update", "编辑关键词"), ("status", "采用或弃用关键词"), ("delete", "删除关键词")], 220),
    *_resource("content", "titles", "标题", [("generate", "生成标题"), ("create", "新增标题"), ("update", "编辑或打分标题"), ("status", "采用或弃用标题"), ("delete", "删除标题")], 230),
    *_resource("content", "contents", "内容", [("generate", "生成或重写内容"), ("create", "新增内容"), ("update", "编辑内容"), ("review", "审核内容"), ("status", "归档或恢复内容"), ("export", "导出内容"), ("delete", "删除内容")], 240),
    *_resource("content", "batches", "生成批次", [("cancel", "取消批次"), ("retry", "重试批次")], 250),
    *_resource("media", "assets", "素材库", [("retry", "重试或转存素材"), ("delete", "删除素材")], 300),
    *_resource("media", "images", "图片生成", [("generate", "生成图片")], 310),
    *_resource("media", "videos", "视频生成", [("generate", "生成视频")], 320),
    *_resource("ai", "models", "模型目录", [("sync", "同步模型目录")], 400),
    *_resource("ai", "routes", "能力路由", [("create", "新增项目路由"), ("update", "编辑路由"), ("delete", "删除项目路由"), ("test", "健康探测"), ("reset_breaker", "重置熔断")], 410),
    *_resource("ai", "tasks", "AI 任务", [("retry", "重试任务"), ("cancel", "取消任务")], 420),
    *_resource("ai", "usage", "用量对账", [("reconcile", "执行对账")], 430),
    *_resource("publish", "platforms", "发布平台", [("create", "新增平台"), ("update", "编辑平台规则"), ("delete", "删除平台"), ("test", "测试平台规则")], 500),
    *_resource("publish", "links", "回填链接", [("create", "回填链接"), ("update", "编辑链接"), ("check", "手动检测"), ("mark", "人工标记收录"), ("delete", "删除链接")], 510),
    *_resource("monitoring", "link_checks", "删除检测", [("run", "批量触发删除检测")], 600),
    *_resource("monitoring", "index_checks", "收录检测", [("run", "批量触发收录检测")], 610),
    *_resource("monitoring", "alerts", "告警中心", [("handle", "处理告警")], 620),
    *_resource("stats", "reports", "报表", [("export", "导出报表"), ("recompute", "重算统计")], 700),
    *_resource("system", "settings", "系统配置", [("update", "修改系统配置")], 800),
    *_resource("system", "upload", "素材上传", [("create", "上传参考素材")], 810),
    *_resource("security", "admins", "用户管理", [("create", "新增用户"), ("update", "编辑用户"), ("status", "启用或禁用用户"), ("reset_password", "重置用户密码")], 900),
    *_resource("security", "groups", "用户组权限", [("create", "新增用户组"), ("update", "编辑用户组"), ("delete", "删除用户组"), ("assign", "分配用户组权限")], 910),
    PermissionSpec("security.audit.view", "security", "操作日志", "menu", None, 920),
]

PERMISSION_CODES = {item.code for item in PERMISSIONS}

PERMISSIONS_BY_CODE: dict[str, PermissionSpec] = {item.code: item for item in PERMISSIONS}

PERMISSION_DEPENDENCIES: dict[str, set[str]] = {
    "content.keywords.generate": {"content.batches.view"},
    "content.titles.generate": {"content.batches.view"},
    "content.contents.generate": {"content.batches.view"},
    "media.images.generate": {"media.assets.view"},
    "media.videos.generate": {"media.assets.view"},
}

SYSTEM_GROUPS = [
    {"code": "super_admin", "name": "超级管理员", "name_en": "Super Administrator", "description": "拥有全部后台权限", "is_system": 1, "data_scope": "all"},
    {"code": "operator", "name": "运营人员", "name_en": "Operator", "description": "生成、编辑、回填与监控处理", "is_system": 1, "data_scope": "own"},
    {"code": "reviewer", "name": "审核人员", "name_en": "Reviewer", "description": "审核内容并查看生产数据", "is_system": 1, "data_scope": "all"},
    {"code": "read_only", "name": "只读", "name_en": "Read Only", "description": "仅查看并导出报表与非敏感数据", "is_system": 1, "data_scope": "all"},
]

SYSTEM_GROUP_CODES = {group["code"] for group in SYSTEM_GROUPS}

SUPER_ADMIN_GROUP_CODE = "super_admin"

OPERATOR_EXCLUDED = {"content.contents.review", "content.projects.delete", "content.prompt_templates.publish", "content.prompt_templates.delete"}

DEFAULT_GROUP_PERMISSIONS = {
    # super_admin：PERMISSION_CODES 全集（seed 时直接写入全部，不在本字典中）
    "operator": {
        code for code in PERMISSION_CODES
        if code in {"dashboard.view", "system.upload.view", "system.upload.create"}
        or (code.startswith("content.") and code not in OPERATOR_EXCLUDED)
        or code.startswith("media.")
        or code.startswith("publish.links.")
        or code == "publish.platforms.view"
        or code.startswith("monitoring.")
        or (code.startswith("ai.") and (code.endswith(".view") or code in {"ai.tasks.retry", "ai.tasks.cancel"}))
        or code in {"stats.reports.view", "stats.reports.export"}
    },
    "reviewer": {
        code for code in PERMISSION_CODES
        if code == "dashboard.view"
        or (code.startswith("content.") and code.endswith(".view"))
        or code in {"content.contents.review", "content.contents.update", "content.contents.export"}
        or (code.startswith("media.") and code.endswith(".view"))
        or (code.startswith("publish.") and code.endswith(".view"))
        or (code.startswith("monitoring.") and code.endswith(".view"))
        or code == "stats.reports.view"
    },
    "read_only": {
        code for code in PERMISSION_CODES
        if (code.endswith(".view") and not code.startswith("security.") and code != "system.settings.view")
        or code == "stats.reports.export"
    },
}

# 模块中文名（权限树与操作日志「模块」列；admin_rbac_service.MODULE_NAMES 引用此常量）
MODULE_NAMES: dict[str, str] = {
    "dashboard": "控制台",
    "content": "内容生产",
    "media": "媒体",
    "ai": "AI 网关",
    "publish": "发布",
    "monitoring": "监控",
    "stats": "报表",
    "system": "系统",
    "security": "权限安全",
}


def expand_permission_codes(codes: Iterable[str]) -> set[str]:
    """按 §4.3 补齐：每个 ``action`` 码补上 ``parent_code``（同资源 view），再补 ``PERMISSION_DEPENDENCIES``。

    只处理合法码（未知码由调用方先行校验并返回 400）。
    """
    result = {code for code in codes if code in PERMISSION_CODES}
    changed = True
    while changed:
        changed = False
        for code in list(result):
            extra: set[str] = set()
            parent = PERMISSIONS_BY_CODE[code].parent_code
            if parent:
                extra.add(parent)
            extra |= PERMISSION_DEPENDENCIES.get(code, set())
            new = extra - result
            if new:
                result |= new
                changed = True
    return result


def permission_list() -> list[dict[str, Any]]:
    """平铺权限列表 ``[{code, module, name, type, parent_code, sort}]``，按 ``sort`` 排序。"""
    return [
        {"code": p.code, "module": p.module, "name": p.name, "type": p.type, "parent_code": p.parent_code, "sort": p.sort}
        for p in sorted(PERMISSIONS, key=lambda item: (item.sort, item.code))
    ]


def permission_tree() -> list[dict[str, Any]]:
    """module → menu → action 的权限树（``GET /admin/admin-permissions/tree``）；每个码恰好出现一次。"""
    modules: dict[str, dict[str, Any]] = {}
    menus: dict[str, dict[str, Any]] = {}
    ordered = sorted(PERMISSIONS, key=lambda item: (item.sort, item.code))
    for spec in ordered:
        if spec.type != "menu":
            continue
        module = modules.setdefault(spec.module, {"module": spec.module, "name": MODULE_NAMES.get(spec.module, spec.module), "items": []})
        node = {"code": spec.code, "name": spec.name, "type": spec.type, "children": []}
        module["items"].append(node)
        menus[spec.code] = node
    for spec in ordered:
        if spec.type == "action" and spec.parent_code in menus:
            menus[spec.parent_code]["children"].append({"code": spec.code, "name": spec.name, "type": spec.type})
    return list(modules.values())
