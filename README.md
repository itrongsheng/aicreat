# aicreat

aicreat 是面向运营 / 内容团队的 **AI 内容生成与效果监控平台**（B 端工具，带用户系统：每个用户只能看到自己的数据，总后台看全部）。在一个管理后台内完成「**关键词生成 → 标题生成 → 内容生成（大纲 / 正文 / SEO 要素）→ 图片 / 视频生成 → 运营手工发布后回填链接 → 链接删除检测 + SEO / GEO 收录检测 → 告警 + 控制台报表**」的闭环；全部文本、图片、视频 AI 能力统一经 **zhiqiapi（志奇引擎，`https://zhiqiapi.com/v1`）** 接入，`ZHIQI_API_KEY` 留空时全链路走本地 Mock，无密钥即可跑通。

后端 **Python 3.11 + FastAPI + SQLAlchemy 2.x / Alembic + MySQL 8 + Redis 7**，两个轮询式 worker 进程（`app.worker` 负责 AI 任务，`app.monitor_worker` 负责监控与聚合），唯一前端为 **Vue 3 + Vite + Element Plus** 管理后台，pnpm monorepo。技术栈与工程约定完全沿用本地 `navigation` 工程。完整设计文档见 [docs/README.md](docs/README.md)。

## 功能概览

所有业务接口都在 `/api/v1/admin/*` 之下（管理员 JWT + 权限码 + 数据范围），下表接口列省略 `/api/v1` 前缀，权限模块列的 `module.resource.*` 表示该资源下的全部权限码（如 `content.keywords.*` = `content.keywords.view/generate/create/import/update/status/delete`）；接口全表见 [docs/04-api-spec.md](docs/04-api-spec.md)，权限码全表见 [docs/07-admin-rbac.md](docs/07-admin-rbac.md)。

| 模块 | 能力 | 主要接口 | 后台页面（`apps/admin/src/views/`） | 权限模块 |
| --- | --- | --- | --- | --- |
| 项目 / 专题 | 内容生产的组织单位：行业、受众、品牌信息（`brand_info` 注入 Prompt 变量）、生成语言（默认 `zh-CN`）、默认风格 / 格式 / 模板、常用平台；项目级默认模型以 `capability_routes` 项目覆盖行表达；归档、删除校验 | `/admin/projects`、`PUT /admin/projects/{id}/routes`、`GET /admin/projects/{id}/overview` | `projects/Index.vue`、`projects/Detail.vue` | `content.projects.*` |
| Prompt 模板 | 14 种 `prompt_kind`（`keyword`/`title`/`outline`/`content`/`section`/`rewrite`/`expand`/`shorten`/`restyle`/`seo_meta`/`faq`/`image_prompt`/`geo_query`/`seo_query`）；变量、输出格式、预览渲染、版本；`draft → published → archived`，已发布版本不可编辑、编辑即复制为新版本；系统模板 `sys_*` 由 seed 写入 | `/admin/prompt-templates` | `prompt-templates/Index.vue`、`prompt-templates/Editor.vue` | `content.prompt_templates.*` |
| 关键词生成 | 种子词 / 行业 / 竞品 / 受众 → LLM 批量生成候选词（搜索意图 `keyword_intent`、类型 `keyword_type`、难度 / 热度可选）；JSON 与 CSV 导入；去重（`UNIQUE(project_id, normalized_keyword)`）；状态 `candidate / adopted / discarded`；导出 CSV | `POST /admin/keywords/generate`、`/admin/keywords/import`、`/admin/keywords/import-file`、`/admin/keywords/{id}/adopt`·`discard`·`restore`、`/admin/keywords/batch-status`、`GET /admin/keywords/export` | `keywords/Index.vue` | `content.keywords.*` |
| 标题生成 | 对未弃用（`candidate` / `adopted`）关键词生成 N 个候选标题（采用标题要求其关键词已 `adopted`），7 种风格 `content_style`（`news`/`tutorial`/`review`/`qa`/`recommend`/`listicle`/`story`）；人工编辑（记 `original_title`）、打分 `manual_score`、采用 / 弃用 / 恢复 | `POST /admin/titles/generate`、`/admin/titles/{id}/score`、`/admin/titles/{id}/adopt`·`discard`·`restore`、`/admin/titles/batch-status` | `titles/Index.vue` | `content.titles.*` |
| 内容生成 | 标题 + 关键词 + 大纲 + Prompt 模板 → Markdown / HTML 文章：大纲先行、分段生成、重写 / 扩写 / 缩写 / 改风格（全文或单节）、SEO 要素（`seo_title`/`seo_description`/`seo_keywords`/FAQ/`summary`）、不可变版本历史与回滚、审核流、素材绑定（封面 / 插图）、导出 `md`/`html`/`json`；状态 `draft → generating → ready → reviewing → approved → published → archived`（审核驳回为 `rejected`，重新生成 / 人工编辑后回到 `ready` 再提审；全部 8 个取值与流转见 [docs/00-overview.md](docs/00-overview.md)） | `POST /admin/contents/generate`、`/admin/contents/{id}/generate-outline`·`generate-body`·`rewrite`·`generate-seo`、`GET /admin/contents/{id}/task`、`/admin/contents/{id}/submit-review`·`approve`·`reject`·`archive`·`unarchive`、`/admin/contents/{id}/versions`、`/admin/contents/{id}/assets/{asset_id}/attach`·`detach`、`GET /admin/contents/{id}/export` | `contents/Index.vue`、`contents/Editor.vue` | `content.contents.*` |
| 生成批次 | 关键词 / 标题 / 内容生成的用户级任务：子任务（根任务）进度、取消未开始子任务、重试失败子任务；`queued → running → succeeded / partial / failed`，可 `cancelled` | `/admin/generation-batches`、`/admin/generation-batches/{id}/cancel`·`retry` | `generation-batches/Index.vue` | `content.batches.*` |
| 图片生成 | 文生图、图生图（参考图为公网 URL，≤ 9 张）；分辨率 `1080p`/`2k`/`4k`，比例 `1:1`/`4:3`/`3:4`/`16:9`/`9:16`；多图逐张并行；可由文章自动生成配图提示词（`from_content_prompt`）；异步提交 → 轮询 → 转存本地 / 对象存储 → `ready` | `POST /admin/media/images/generate`、`GET /admin/media/assets/{id}/task` | `media/ImageGenerate.vue` | `media.images.*` |
| 视频生成 | 文生视频、图生视频（`input_reference`、首尾帧 `first_frame_image_url`/`last_frame_image_url`、参考图 / 视频 / 音频 URL）；`480p`/`720p`/`1080p`/`4k`、时长、可选 `generate_audio`；长任务（轮询预算默认 1200s）→ 下载保存 | `POST /admin/media/videos/generate` | `media/VideoGenerate.vue` | `media.videos.*` |
| 素材库与上传 | 图片 / 视频素材列表（按项目 / 内容 / 类型 / 状态筛选）、预览、删除、失败重试、重新转存；参考图 / 参考视频上传（返回 URL 并标注 `public` 是否公网可达） | `/admin/media/assets`、`/admin/media/assets/{id}/retry`·`transfer`、`POST /admin/uploads/image`·`video` | `media/Assets.vue` | `media.assets.*`、`system.upload.*` |
| AI 网关（zhiqiapi） | 模型目录与价格快照同步；能力 → 主模型 + 备选链路由（全局 / 项目覆盖）；文本三协议 `openai_chat`/`openai_responses`/`anthropic_messages`；指数退避重试、按模型熔断、超时预算、全局暂停；一键健康探测；AI 任务（根任务 + 尝试行、`request_id`、错误分类、tokens / 额度 / 成本 / 耗时）；`/api/log/token` 用量对账 | `/admin/ai/models`（`/sync`、`/options`）、`/admin/ai/routes`（`/{id}/test`、`/{id}/reset-breaker`）、`/admin/ai/health`、`POST /admin/ai/health/probe`、`/admin/ai/tasks`（`/{id}/retry`、`/{id}/cancel`、`/export`）、`/admin/ai/usage/logs`·`reconcile`·`summary` | `ai/Models.vue`、`ai/Routes.vue`、`ai/Tasks.vue`、`ai/Usage.vue` | `ai.*` |
| 发布平台 | 平台字典（seed `zhihu`/`wechat_mp`/`xiaohongshu`/`csdn`/`toutiao`/`baijiahao`/`website`/`other`）、URL 识别正则、「已删除」特征文案、跳转到首页 / 登录页规则、抓取配置；规则在线测试、URL 识别 | `/admin/platforms`、`POST /admin/platforms/{id}/test`、`POST /admin/platforms/detect` | `platforms/Index.vue` | `publish.platforms.*` |
| 回填链接 | 文章由运营**手工**发布到外部平台后回填 URL（一文多链、多平台；平台自动识别；规范化 URL + `url_hash` 唯一）；发布账号、发布时间、回填人；批量回填；手动删除检测 / 收录检测、人工标记收录、重置基线、暂停 / 恢复监控、检测历史与证据、导出 | `/admin/links`、`/admin/links/batch`、`/admin/links/{id}/check`·`index-check`·`rebaseline`·`mark-index`·`pause`·`resume`、`/admin/links/{id}/checks`·`index-checks`、`GET /admin/links/export` | `links/Index.vue`、`links/Detail.vue` | `publish.links.*` |
| 删除检测 | SSRF 安全抓取器（只允许 http/https、公网 IP、≤ 3 跳、响应体 ≤ 2 MB、固定 UA、不带 Cookie）→ HTTP 404/410/451、跳转首页 / 登录页、平台特征文案、标题 / 正文 SimHash 指纹变化（海明距离 ≥ 20）逐条判定；`pending → alive → changed / suspected_deleted → deleted / unknown`；新链接前 7 天每日 1 次、之后每周 1 次、异常后加密，删除 / 恢复产生告警 | `/admin/monitoring/link-checks`、`POST /admin/monitoring/link-checks/run` | `monitoring/LinkChecks.vue` | `monitoring.link_checks.*` |
| SEO / GEO 收录检测 | SEO 按引擎 `baidu`/`bing`/`google` 经提供器（`zhiqi_web_search`/`baidu_ai_search`/`bing_webmaster`/`google_search_console`/`manual`）判定 `indexed / not_indexed / unknown`；GEO 按引擎 `baidu_ai`/`doubao`/`kimi`/`deepseek`/`perplexity`/`chatgpt` 经联网模型提问并解析回答中的引用，默认只按引用 URL / 域名判定 `cited / not_cited / unknown`（标题近似匹配须在 `geo_engines` 中为该引擎显式设 `parse.match_mode=title`）；每次记录证据、提供器、模型与 `request_id`；发布后第 1/3/7/14/30 天检测，之后每月一次，已收录的引擎每 90 天复核一次；**不抓取搜索引擎结果页 HTML** | `/admin/monitoring/index-checks`、`POST /admin/monitoring/index-checks/run` | `monitoring/IndexChecks.vue` | `monitoring.index_checks.*` |
| 告警中心 | 11 种 `alert_type`：`link_deleted`/`link_restored`/`link_changed`/`index_overdue`/`ai_task_failures`/`ai_breaker_open`/`ai_quota_exceeded`/`ai_auth_failed`/`ai_upstream_unavailable`/`media_task_failed`/`worker_stale`；按 `dedupe_key` 去重；`open → acknowledged → resolved`，可 `ignored`；站内通道固定，webhook / 邮件预留；顶栏铃铛 60s 轮询未处理数 | `/admin/alerts`、`/admin/alerts/summary`、`/admin/alerts/{id}/acknowledge`·`resolve`·`ignore`、`/admin/alerts/batch-resolve` | `alerts/Index.vue`、`components/AlertBadge.vue` | `monitoring.alerts.*` |
| 控制台与报表 | 总览 KPI（关键词 / 标题 / 文章数、回填链接数、存活率、SEO 收录率、GEO 引用率、AI 调用次数 / 成功率 / 平均耗时、额度消耗与估算费用、图片 / 视频生成数、未处理告警）与环比；日 / 周 / 月趋势；按项目 / 平台 / 人员 / 模型 / 能力 / 引擎分解；榜单（收录最快文章、被删最多平台、消耗最高模型 / 项目、失败最多模型）；CSV 导出；`daily_stats` 预聚合 + Redis 实时计数兜底 + 重算 | `/admin/stats/overview`、`/admin/stats/trends`、`/admin/stats/breakdown`、`/admin/stats/rankings`、`GET /admin/stats/export`、`POST /admin/stats/recompute` | `Dashboard.vue`、`stats/Reports.vue` | `dashboard.view`、`stats.reports.*` |
| 系统配置 | `settings(key, locale, value)` JSON 配置键：`generation_config`/`media_config`/`monitoring_config`/`geo_engines`/`seo_providers`/`alert_config`/`ai_routing_config`/`stats_config`/`system_info`；密钥只在环境变量，后台仅显示 `configured` | `/admin/settings`、`GET /admin/settings/runtime` | `settings/Index.vue` | `system.settings.*` |
| 用户与权限 | 用户（后台账号）、用户组、90 个权限码（`menu`（`*.view`）与 `action` 两级）、系统用户组、操作日志（审计中间件自动记录写接口） | `/admin/auth/*`、`/admin/admins`、`/admin/admin-groups`、`/admin/admin-permissions`、`/admin/admin-operation-logs` | `admins/Index.vue`（用户管理）、`admin-groups/Index.vue`、`admin-operation-logs/Index.vue` | `security.*` |
| 数据隔离（用户系统） | 用户组的数据范围 `data_scope`：`own` 的普通用户只能看到、操作自己负责的项目（`projects.owner_id`）及其下全部数据、告警与统计，`all` 的总后台看全部；总后台可为用户开设 / 转移项目、在顶栏切换到任一用户视角（`owner_id`）、按用户分解报表；范围外对象一律 404，回填他人已回填的 URL 返回 `owned_by_other` | 受范围约束的列表、导出、统计接口与 `GET /admin/alerts/summary`、`GET /admin/monitoring/overview`、`GET /admin/ai/usage/summary` 的可选 `owner_id` 参数（[13](docs/13-user-data-scope.md) §6.2，操作日志除外）、`GET /admin/projects/owner-options`、`GET /admin/stats/breakdown?dimension=owner` | 顶栏 `components/OwnerSelect.vue`、`admin-groups/Index.vue`（数据范围）、`projects/Index.vue`（负责人） | 无新增权限码（由用户组 `data_scope` 决定） |

各模块的完整设计：用户系统与数据隔离 [docs/13-user-data-scope.md](docs/13-user-data-scope.md)；数据表与一致性规则 [docs/03-data-model.md](docs/03-data-model.md)；关键词 / 标题 / 内容生成与 Prompt 模板 [docs/09-generation-pipeline.md](docs/09-generation-pipeline.md)；图片 / 视频生成 [docs/10-media-generation.md](docs/10-media-generation.md)；回填链接、删除检测、收录检测与告警 [docs/11-link-backfill-and-monitoring.md](docs/11-link-backfill-and-monitoring.md)；控制台与报表 [docs/12-dashboard-reports.md](docs/12-dashboard-reports.md)；zhiqiapi 适配层与 AI 网关 [docs/08-zhiqiapi-integration.md](docs/08-zhiqiapi-integration.md)。

### 角色

无 C 端用户，账号由总后台在「系统 → 用户管理」创建（无自助注册）。每个用户经所属用户组获得**功能权限**（权限码）与**数据范围**（`data_scope`）：`own` 只能看到本人负责的项目及其下数据，`all` 即总后台、看全部用户的数据（[docs/13-user-data-scope.md](docs/13-user-data-scope.md)）。四个系统用户组（`admin_groups.is_system=1`，不可删除 / 停用），默认权限的完整规则见 [docs/07-admin-rbac.md](docs/07-admin-rbac.md)：

| 用户组 code | 名称 | 默认数据范围 | 默认权限（摘要） |
| --- | --- | --- | --- |
| `super_admin` | 超级管理员 | `all`（固定，总后台） | 全部 90 个权限码 |
| `operator` | 运营人员 | `own`（只看本人项目） | `dashboard.view`、`system.upload.*`、全部 `content.*`（不含 `content.contents.review`、`content.projects.delete`、`content.prompt_templates.publish`、`content.prompt_templates.delete`）、全部 `media.*`、全部 `publish.links.*`、`publish.platforms.view`、全部 `monitoring.*`、`ai.*.view` + `ai.tasks.retry`/`ai.tasks.cancel`、`stats.reports.view`/`stats.reports.export` |
| `reviewer` | 审核人员 | `all` | `dashboard.view`、`content.*.view` + `content.contents.review`/`update`/`export`、`media.*.view`、`publish.*.view`、`monitoring.*.view`、`stats.reports.view` |
| `read_only` | 只读 | `all` | 全部 `*.view`（不含 `security.*` 与 `system.settings.view`）+ `stats.reports.export` |

### 端到端流程

```mermaid
flowchart LR
    P["项目 / 专题<br/>projects"] --> K["关键词生成<br/>keywords: candidate → adopted"]
    K --> T["标题生成<br/>titles: candidate → adopted"]
    T --> C["内容生成<br/>contents: 大纲 → 正文 → SEO 要素<br/>版本 content_versions"]
    M["图片 / 视频生成<br/>media_assets（异步任务 + 转存）"] -.->|"配图 / 封面"| C
    C --> R{"审核<br/>reviewing"}
    R -->|"approve"| A["approved"]
    R -->|"reject"| RJ["rejected（重新生成 / 编辑后再提审）"]
    RJ --> C
    A -->|"运营手工发布到知乎 / 公众号 / 小红书 …"| B["回填链接<br/>publish_links → published"]
    B --> LC["删除检测<br/>link_checks（monitor_worker）"]
    B --> IC["SEO / GEO 收录检测<br/>index_checks（monitor_worker）"]
    LC --> AL["告警中心<br/>alerts"]
    IC --> AL
    LC --> S["daily_stats 预聚合<br/>控制台 / 报表"]
    IC --> S
    C --> S
    M --> S
    G["AI 网关 ai_gateway_service<br/>capability_routes / ai_tasks / 熔断 / 对账"] -->|"zhiqiapi"| K
    G -->|"zhiqiapi"| T
    G -->|"zhiqiapi"| C
    G -->|"zhiqiapi"| M
    G -->|"zhiqiapi（联网模型）"| IC
```

**首版非目标**：站内直接发布 / 托管文章页面、自动登录外部平台代发（只做手工发布后回填）、C 端用户社区、会员 / 支付、多语言内容（界面保留 zh-CN / en-US 切换，生成内容语言由 `projects.language` 决定，默认中文）。详见 [docs/00-overview.md](docs/00-overview.md)。

## 技术栈与运行组件

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11 + FastAPI + Pydantic v2 + pydantic-settings；SQLAlchemy 2.x + Alembic；MySQL 8（utf8mb4_unicode_ci，InnoDB）；Redis 7（缓存 / 队列 / 频控 / 锁）；gunicorn + uvicorn workers；pyjwt + bcrypt；httpx（zhiqiapi 调用）；boto3（S3 兼容对象存储）；google-auth（仅 `google_search_console` 提供器取令牌）。不引入 Celery / RQ / Pillow / ffmpeg（图片宽高由 `storage.probe_image_size` 解析文件头得到，视频宽高 / 时长首版留空） |
| 异步进程 | navigation 式轮询 worker：`python -m app.worker`（消费 `queue:ai_tasks`、媒体轮询与转存、用量对账、模型同步、健康探测、过期回收）、`python -m app.monitor_worker`（删除检测、收录检测、每日统计聚合、告警评估）；Redis List 作队列，MySQL 任务表为权威状态，心跳 / 过期回收 |
| 管理后台 | `apps/admin`：Vue 3 + Vite + TypeScript + Element Plus + Pinia + Vue Router + vue-i18n（zh-CN / en-US）+ axios + echarts；`base: /admin/`，端口 5174，Vite 代理 `/api`、`/media` → `http://127.0.0.1:8100` |
| 共享包 / 包管理 | `packages/shared`（`@aicreat/shared`：TS 类型、状态枚举、常量）；pnpm@9 workspace（`apps/*`、`packages/*`）；后端 venv + pip（`server/pyproject.toml`，包名 `aicreat-server`） |
| 存储 / 反代 | `STORAGE_MODE=local` 时文件落 `server/storage/` 经 `/media/{key}` 暴露，`oss` 时走 S3 兼容对象存储（`OSS_*`）；Nginx（`/api/`、`/media/` → server:8000，`/admin/` → admin dist，`/` → 302 `/admin/`）+ docker-compose |

统一约定：响应 `{code, message, data}`（`code=0` 成功）；分页 `page`/`page_size`（默认 20，最大 100）返回 `{items, total, page, page_size}`；业务异常 `BusinessError(message, code, http_status, data)`；管理员 JWT（HS256，`aud="admin"`，`token_version` 失效机制），`require_permission("module.resource.action")` 依赖；时间 ISO 8601 UTC；所有表含 `created_at`/`updated_at`。详见 [docs/01-architecture.md](docs/01-architecture.md)、[docs/04-api-spec.md](docs/04-api-spec.md)。

| 进程 | 启动方式 | 端口 / 说明 |
| --- | --- | --- |
| `mysql` / `redis` | `docker compose up -d mysql redis`（或本机服务） | 3306 / 6379；库 `aicreat`、账号 `aicreat`/`password`（`MYSQL_*`） |
| `server` | 开发 `uvicorn app.main:app --reload --port 8100`；容器 gunicorn | 开发 8100，容器内 8000；`GET /api/v1/health` 公开，`/docs` 为 OpenAPI |
| `worker` | `python -m app.worker` | 无端口；心跳 `worker:heartbeat:worker:{hostname}:{pid}` |
| `monitor-worker` | `python -m app.monitor_worker` | 无端口；心跳 `worker:heartbeat:monitor_worker:{hostname}:{pid}` |
| `admin` | `pnpm dev:admin`；生产为 `apps/admin/dist` 静态文件 | 开发 `http://localhost:5174/admin/`；生产经 nginx `/admin/`（`NGINX_HTTP_PORT`，默认 80） |

```mermaid
flowchart LR
    Browser["浏览器（运营 / 审核 / 只读）"] -->|"/admin/"| Admin["admin（Vite 5174 / Nginx 静态）"]
    Browser -->|"/api/v1/admin/*、/media/*"| Nginx["nginx :80"]
    Nginx --> Server["server（FastAPI，容器 8000 / 开发 8100）"]
    Admin -.->|"开发态：Vite 代理 /api、/media → 127.0.0.1:8100"| Server
    Server --> MySQL[("MySQL 8")]
    Server --> Redis[("Redis 7：队列 / 锁 / 缓存 / 熔断")]
    Server --> Storage[("storage/ 或 S3 兼容对象存储")]
    Worker["worker<br/>python -m app.worker"] --> MySQL
    Worker --> Redis
    Worker --> Storage
    Worker -->|"Bearer ZHIQI_API_KEY"| Zhiqi["zhiqiapi<br/>https://zhiqiapi.com/v1"]
    Server -->|"模型目录同步 / 一键探测 / 手动对账"| Zhiqi
    Monitor["monitor-worker<br/>python -m app.monitor_worker"] --> MySQL
    Monitor --> Redis
    Monitor -->|"safe_fetch（SSRF 安全抓取）"| Pages["外部发布页面"]
    Monitor -->|"seo_check / geo_check"| Zhiqi
```

完整拓扑、时序图、Redis 键表与 worker 任务总表见 [docs/01-architecture.md](docs/01-architecture.md)。

## 目录结构

```text
aicreat/
├── docs/                                  设计文档：README（索引）+ 00~13
├── server/                                FastAPI 后端 + 两个 worker（包名 aicreat-server）
│   ├── app/
│   │   ├── main.py                        create_app()：路由、CORS、异常处理、审计中间件、/media 静态、启动 ensure_*
│   │   ├── worker.py                      python -m app.worker：AI 任务进程
│   │   ├── monitor_worker.py              python -m app.monitor_worker：监控进程
│   │   ├── models.py                      全部 24 张表的 SQLAlchemy 模型
│   │   ├── api/                           deps.py、health.py、admin/*.py（一资源一路由文件，均挂在 /api/v1/admin）
│   │   ├── core/                          config / database / redis / locks / security / storage / ratelimit / response /
│   │   │   │                              exceptions / admin_permissions / safe_fetch / fingerprint / urls
│   │   │   └── zhiqi/                     zhiqiapi 适配层：types / errors / client / text / images / videos / catalog /
│   │   │                                  usage / health / breaker / mock + mock_assets/（placeholder.png、placeholder.mp4）
│   │   ├── schemas/                       Pydantic v2 请求 / 响应模型（一资源一文件）
│   │   ├── services/                      业务编排与事务边界（*_service.py）、index_providers/（SEO 提供器与 GEO 引擎）
│   │   └── tasks/                         worker 周期任务：run_ai_tasks、poll_media_tasks、transfer_media、reconcile_usage、
│   │                                      sync_models、health_probe、recover_stale_tasks、cleanup_media（app.worker）；
│   │                                      schedule_link_checks、run_link_checks、schedule_index_checks、run_index_checks、
│   │                                      aggregate_daily_stats、evaluate_alerts（app.monitor_worker）
│   ├── migrations/versions/               0001_initial.py（24 张表）、0002_seed_permissions.py（权限码与系统用户组）
│   ├── seeds/seed.py                      幂等 seed：超管 admin/admin123、示例项目、系统 Prompt 模板、默认平台
│   ├── scripts/integration_smoke.py       Mock 模式端到端冒烟
│   ├── tests/                             pytest：admin_rbac / data_scope / zhiqi_adapter / generation / media / links / monitoring / stats
│   ├── storage/                           本地 Mock 存储（gitignore），经 /media 暴露
│   └── alembic.ini · pyproject.toml · Dockerfile · .env.example
├── apps/admin/                            唯一前端：Vue 3 管理后台（base /admin/，端口 5174）
│   ├── vite.config.ts                     代理 /api、/media → http://127.0.0.1:8100
│   └── src/
│       ├── router/ · store/ · i18n/ · api/ · layouts/ · directives/ · composables/ · utils/ · components/
│       └── views/                         Login、Forbidden、Dashboard、projects、prompt-templates、keywords、titles、contents、
│                                          generation-batches、media、ai、platforms、links、monitoring、alerts、stats、settings、
│                                          admins、admin-groups、admin-operation-logs
├── packages/shared/                       @aicreat/shared：types.ts / enums.ts / constants.ts
├── nginx/nginx.conf                       /api/ → server:8000；/media/ → server:8000；/admin/ → admin dist；/ → 302 /admin/
├── scripts/                               dev-restart.ps1（重启四个开发进程）、db-backup.sh（mysqldump，保留 14 天）
├── docker-compose.yml                     mysql / redis / server / worker / monitor-worker / nginx
├── pnpm-lock.yaml                         pnpm 锁文件（首次 pnpm install 生成后提交入库；镜像 / 生产构建用 pnpm install --frozen-lockfile）
├── pnpm-workspace.yaml · package.json · .env.example · .gitignore
└── README.md
```

完整目录树（每个文件的职责）、模块职责表与命名约定见 [docs/02-project-structure.md](docs/02-project-structure.md)。

## 快速开始

### 环境要求

| 工具 | 版本 |
| --- | --- |
| Python | 3.11+ |
| Node.js | 18+ |
| pnpm | 9.x |
| MySQL | 8.x（utf8mb4） |
| Redis | 7.x |
| Docker / Docker Compose | 可选（用于启动 MySQL / Redis 或整体部署） |

### 1. 依赖服务

```bash
cp .env.example .env                  # 仓库根 .env：docker-compose 读取（MYSQL_* 初始化库与账号、NGINX_HTTP_PORT），必须先于 compose 命令复制
cp server/.env.example server/.env    # 本机开发时后端读取（与根 .env.example 同源，默认值已对准本机 MySQL / Redis）
docker compose up -d mysql redis
docker compose ps                     # 等 mysql 的 healthcheck（mysqladmin ping）变为 healthy 再继续，首次约 30~100 秒
```

顺序不能颠倒：compose 中 `mysql` 服务的 `MYSQL_ROOT_PASSWORD` / `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD` 全部取自根 `.env` 插值且无缺省值，`server` / `worker` / `monitor-worker` 还声明了 `env_file: .env`；先起容器会因缺少 `.env` 报错，或让 mysql 以空 root 密码初始化失败并反复重启，库与账号 `aicreat` 不会被创建，下一步 `alembic upgrade head` 随之连接失败（见 [docs/05-deployment.md](docs/05-deployment.md) §2.1 / §3.2）。

默认 `DATABASE_URL=mysql+pymysql://aicreat:password@127.0.0.1:3306/aicreat`、`REDIS_URL=redis://127.0.0.1:6379/0`，与 compose 的 `MYSQL_DATABASE=aicreat`、`MYSQL_USER=aicreat`、`MYSQL_PASSWORD=password` 一致，本地开发无需修改。**保持 `ZHIQI_API_KEY` 为空即为 Mock 模式**。完整 `.env` 清单见 [docs/05-deployment.md](docs/05-deployment.md)。

### 2. 后端（API）

```bash
cd server
python -m venv .venv
.venv\Scripts\activate                # Windows；macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"               # dev 额外依赖只有 pytest；不跑测试可 pip install -e .
alembic upgrade head                  # 0001_initial（24 张表）+ 0002_seed_permissions（权限码、系统用户组）
python seeds/seed.py                  # 在 server/ 下执行（依赖上一步 pip install -e 使 app 可导入）；幂等 upsert：超管 admin/admin123、示例项目、系统 Prompt 模板（zh-CN）、默认发布平台
uvicorn app.main:app --reload --host 127.0.0.1 --port 8100
```

- `main.py` 启动时在 `lock:bootstrap` 内执行 `ensure_rbac_seed` / `ensure_default_settings` / `ensure_default_routes`（均幂等）：默认 settings 配置键按「不存在则插入」写入（初值由环境变量派生），8 条全局能力路由在 Mock 模式下固定为 `mock-text` / `mock-image` / `mock-video`；同时把 `app/core/zhiqi/mock_assets/` 的占位文件复制到 `storage/mock/`。
- 验证：`curl http://127.0.0.1:8100/api/v1/health` 返回 `{"status":"degraded","db":true,"redis":true,"zhiqi_mode":"mock","workers":{"worker":{"alive":false,"replicas":0,"heartbeat_at":null},"monitor_worker":{...}},"warnings":[],"version":"..."}` —— 两个 worker 尚未启动时 `status` 为 `degraded` 属预期，启动后变为 `ok`；db / redis 不可用才返回 503。
- OpenAPI 文档：`http://127.0.0.1:8100/docs`。

### 3. worker 与 monitor-worker

另开两个终端（均在 `server/` 目录、已激活 venv）：

```bash
python -m app.worker            # 终端 2：AI 任务进程
python -m app.monitor_worker    # 终端 3：监控进程
```

| 进程 | 消费队列 | 周期任务（默认间隔，运行期以数据库配置为准） | 心跳键 |
| --- | --- | --- | --- |
| `app.worker`（主循环休眠 `WORKER_POLL_INTERVAL_SECONDS=2`） | `queue:ai_tasks`（文本生成与图片 / 视频提交的根任务） | `poll_media_tasks`（按 `next_poll_at`）、`transfer_media`（成功即转存，失败重试扫描 60s）、`reconcile_usage`（300s）、`sync_models`（3600s，启动即跑一次）、`health_probe`（600s）、`recover_stale_tasks`（60s）、`cleanup_media`（每日 03:00） | `worker:heartbeat:worker:{hostname}:{pid}` |
| `app.monitor_worker`（主循环休眠 `MONITOR_POLL_INTERVAL_SECONDS=5`） | `queue:link_checks`、`queue:index_checks`、`queue:stats_recompute` | `schedule_link_checks`（60s）、`schedule_index_checks`（300s）、`aggregate_daily_stats`（每日 00:30 聚合昨天，今日每 600s 增量，启动补算 3 天）、`evaluate_alerts`（300s） | `worker:heartbeat:monitor_worker:{hostname}:{pid}` |

两个进程都是「主循环只领取 / 调度 + 线程池执行」的轮询模式，并发上限为进程内信号量：worker 按模态取 `AI_MAX_CONCURRENCY_TEXT` / `AI_MAX_CONCURRENCY_IMAGE` / `AI_MAX_CONCURRENCY_VIDEO`，monitor-worker 取 `monitoring_config.link_check.global_concurrency`（seed 自 `MONITOR_CONCURRENCY=4`）与 `monitoring_config.index_check.concurrency`（默认 2）；多副本部署时每副本独立。任务总表见 [docs/01-architecture.md](docs/01-architecture.md)。

### 4. 前端（管理后台）

```bash
pnpm install        # 仓库根执行，workspace 一次装好 apps/admin 与 packages/shared；仓库尚无 pnpm-lock.yaml 时由首次执行生成，须提交入库
pnpm build:shared   # = pnpm --filter @aicreat/shared build → packages/shared/dist/，新克隆仓库必须先构建
pnpm dev:admin      # = pnpm --filter admin dev → http://localhost:5174/admin/
```

`@aicreat/shared` 的 `main` / `types` 指向 `dist/`，而 `packages/shared/dist/` 被 `.gitignore` 忽略：不先执行 `pnpm build:shared`，开发态 Vite 解析不到 `@aicreat/shared`，`pnpm build:admin` 也会构建失败；修改 `packages/shared/src/` 后同样要重新执行一次。

Vite 把 `/api`、`/media` 代理到 `http://127.0.0.1:8100`；登录页先调用公开接口 `GET /api/v1/admin/auth/site-info` 渲染站点名（`system_info`，默认「aicreat 内容生成平台」），登录后 `GET /api/v1/admin/auth/me` 返回权限码与 `zhiqi_mode`（`mock` / `live`），侧边菜单按 `*.view` 权限码过滤。

### 5. 验证 Mock 全流程（无密钥）

脚本方式：`pnpm smoke:api`（= `server/scripts/integration_smoke.py`，需 API、`app.worker`、`app.monitor_worker` 三个进程都在运行，且 monitor-worker 能访问公网）。脚本顺序执行登录 → 关键词 → 标题 → 内容 → 图片 → 回填 → 检测 → 报表：检测步骤断言 SEO / GEO 检测结果非 `unknown`，并另回填一条稳定返回 404 的公网 URL（如 `https://httpbin.org/status/404`），断言基线检测为 `suspected_deleted`、手动检测后为 `deleted`、产生 `link_deleted` 告警且可确认、解决（恢复分支 `link_restored` 无法用公网 404 地址触发，由后端测试覆盖）；报表步骤断言 `seo_index_rate`/`geo_cite_rate` 非 null 且 > 0。回填步骤使用公网 URL 并把 `published_at` 置于 31 天前以触发一轮 `scheduled` 收录检测，整段最长约 8 分钟（含 `scheduled` 轮次后最多 3 次复查：Mock 单引擎命中率 70%，轮次完成后 `seo_indexed_any` / `geo_cited_any` 仍为 0 时调用 `POST /admin/links/{id}/index-check` 复查，3 次仍未命中才判失败；删除检测分支只多两次抓取）；视频不在脚本内，按下文第 4 步手工验证。

脚本参数：`--base-url` 为 API 根地址（只写 scheme + 主机 + 端口，不带 `/api/v1`），缺省 `http://127.0.0.1:8100`；`--username` / `--password` 缺省取 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`（本机读 `server/.env`，容器内读根 `.env` 经 `env_file` 注入的同名变量），超管改过密码后须用 `--password` 传入新密码（seed 不会覆盖已存在账号的密码）。需要传参时在 `server/` 下直接运行脚本，例如 `dev:restart` 把 API 换到其它端口后执行 `python scripts/integration_smoke.py --base-url http://127.0.0.1:8101`（Windows 用 `.venv\Scripts\python.exe` 代替 `python`）；compose 环境（预发）在 server 容器内运行：`docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000`（容器内 gunicorn 监听 8000，8100 上没有服务）。

手工方式：

1. 用 `admin / admin123` 登录，进入「内容生产 → 项目」确认 seed 的示例项目，顶栏项目选择器选中它。
2. 「关键词」→ 生成：填种子词与数量，提交后在「生成批次」或页面轮询看到 `succeeded`，关键词列表出现 `candidate` 词（Mock 固定返回 20 个），采用其中若干。
3. 「标题」→ 对已采用关键词生成标题并采用；「内容」→ 对已采用标题生成（大纲先行 + SEO 要素），状态 `generating → ready`，编辑器可看大纲、正文、SEO 要素与版本。
4. 「内容」→ 提交审核 → 审核通过（`approved`）；「图片生成」→ 文生图（或勾选由文章生成配图提示词），Mock 在第 3 次轮询返回 `succeeded`，按默认轮询间隔 `5 + 10 + 15` 秒约 30 秒后资产转为 `ready`（占位图 `/media/mock/placeholder.png`）；视频在第 5 次轮询返回 `succeeded`，按 `15 + 30 + 60 + 60 + 60` 秒约 4 分钟（`placeholder.mp4`）；在内容编辑器素材面板绑定为封面。
5. 「回填链接」→ 对 `approved` 内容回填一条**公网**文章 URL（平台自动识别），内容变为 `published`，链接进入 `pending` 并立即入队基线检测；monitor-worker 完成后 `alive_status` 变为 `alive`（删除检测不 Mock，真实抓取；回填 `127.0.0.1` / 内网地址时检测结果为 `unknown`（`matched_rule=ssrf_blocked`），链接状态在连续 3 次 `unknown` 之前保持 `pending`，并按 6h → 12h → 24h 退避复检）。
6. 链接详情 → 「立即收录检测」（SEO + GEO），Mock 下各引擎按 70% 概率返回 `indexed` / `cited`，证据 `source="mock"`；「告警中心」与「AI 网关 → AI 任务 / 用量对账」可看到对应记录（Mock 的伪用量日志同样走对账流程）。
7. 「控制台」总览 KPI 与「报表」趋势 / 分解 / 榜单有数据（今日由 Redis 实时计数兜底，`daily_stats` 每日 00:30 聚合）。注意 `seo_index_rate` / `geo_cite_rate` 的分母只含完成过 ≥ 1 轮 `scheduled` 收录检测的链接（`index_checks_done >= 1`；第 6 步的手动检测与人工标记不计入），刚回填的链接会使二者为 null：要在手工流程里看到非 null 值，回填时把 `published_at` 填为 31 天前（排程直接进入「每月一次」轮次、立即到期），等 `schedule_index_checks`（每 300s 扫描）入队并由 monitor-worker 完成一轮 `scheduled` 检测即可，口径见 [docs/12-dashboard-reports.md](docs/12-dashboard-reports.md)。
8. 数据隔离（用户系统）：在「系统 → 用户管理」新建两个 `operator` 用户，各自登录建项目并生成关键词——彼此看不到对方的数据（列表为空、直接访问 ID 返回 404），控制台只统计自己；`admin` 在顶栏用户视角切换器选中某用户后看到的与该用户本人一致。完整步骤见 [docs/06-getting-started.md](docs/06-getting-started.md) 第五节第 15 步。

逐步说明、接入真实 zhiqiapi 与排错见 [docs/06-getting-started.md](docs/06-getting-started.md)。

### 根目录脚本（`package.json`）

| 脚本 | 命令 |
| --- | --- |
| `dev:server` | `cd server && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8100` |
| `dev:worker` | `cd server && .venv\Scripts\python.exe -m app.worker` |
| `dev:monitor` | `cd server && .venv\Scripts\python.exe -m app.monitor_worker` |
| `dev:admin` | `pnpm --filter admin dev` |
| `dev:restart` | `powershell -ExecutionPolicy Bypass -File scripts/dev-restart.ps1`（重启 API / worker / monitor / admin，端口占用自动换端口） |
| `build:admin` | `pnpm --filter admin build` |
| `build:shared` | `pnpm --filter @aicreat/shared build` |
| `build` | `pnpm -r build` |
| `smoke:api` | `cd server && .venv\Scripts\python.exe scripts\integration_smoke.py` |

`dev:*` 脚本面向 Windows 开发机（直接调用 `.venv\Scripts\python.exe`）；macOS / Linux 在 `server/` 内激活 venv 后运行对应命令即可。后端测试：`cd server && pytest`（pytest 属于 dev 额外依赖，需已按第 2 步执行 `pip install -e ".[dev]"`）。

### 接入真实 zhiqiapi

1. 在 `server/.env`（compose 部署则为根 `.env`）填写 `ZHIQI_API_KEY`，以及各能力路由主模型初值：`ZHIQI_TEXT_DEFAULT_MODEL`（可配 `ZHIQI_TEXT_DEFAULT_PROTOCOL`，默认 `openai_chat`）、`ZHIQI_IMAGE_DEFAULT_MODEL`、`ZHIQI_VIDEO_DEFAULT_MODEL`、`ZHIQI_GEO_DEFAULT_MODEL` / `ZHIQI_SEO_DEFAULT_MODEL`（联网模型；`geo_check` 路由只作为健康探测对象与默认参数来源，各 GEO 引擎实际使用「系统配置 → GEO 引擎」中各自的 `model`，见第 5 步）。这些变量在 `ensure_default_routes` 新建全局路由，或把主模型仍以 `mock-` 开头的路由替换为真实模型时生效（见第 3 步）；已经是真实模型的路由不再被覆盖，以后台「AI 网关 → 能力路由」为准。模型 ID 以 zhiqiapi 模型目录（`GET /v1/models`）为准。
2. 把 `PUBLIC_BASE_URL` 改为**浏览器与 zhiqiapi 都能访问的公网地址**（如 `https://aicreat.example.com`）：图生图 / 图生视频的参考素材与本地转存文件都以它拼公网 URL；填 `127.0.0.1` 或 compose 内部服务名时 `GET /api/v1/health` 的 `warnings[]` 与启动日志会给出 warning，上传接口返回 `public:false`，真实模式下非公网参考 URL 被 API 以业务码 4222 拒绝。
3. 重启 `server`、`worker`、`monitor-worker`：`ensure_default_routes` 把以 `mock-` 开头的全局路由主模型替换为环境变量值（变量为空则该路由 `is_enabled=0` 并记启动告警日志）；已存在的真实模型路由不会被覆盖，之后以后台「AI 网关 → 能力路由」为准。
4. 后台「AI 网关 → 模型目录」点同步（`POST /admin/ai/models/sync`，拉取 `/v1/models` 与 `/api/pricing_new`），「AI 网关 → 能力路由」对每条路由一键测试（`POST /admin/ai/routes/{id}/test`），`GET /admin/ai/health` 查看健康快照、熔断状态与 worker 心跳。
5. 「系统配置 → GEO 引擎」为需要的引擎填写联网模型并启用（真实模式下 `model` 为空的引擎不可启用；若此前以 Mock 模式启动过，`geo_engines` 已被 seed 为 6 个引擎全部 `enabled=true` 且 `model` 为空，而 `ensure_default_settings` 对已存在的键不再改写，须在此页逐个填写模型或停用后保存）；「SEO 提供器」默认 `baidu` / `bing` 启用并走 `zhiqi_web_search`（`google` 默认停用），如需 `baidu_ai_search` / `bing_webmaster` / `google_search_console` 再配置 `SEO_*` 环境变量。
6. 用量对账（`GET /api/log/token`）由 worker 每 300s 自动执行，也可在「用量对账」页手动触发（`POST /admin/ai/usage/reconcile`）；上游只保留最近 1000 条且无分页，高峰期可能出现窗口溢出，溢出的调用只保留本地估算额度。

zhiqiapi 契约中已核实的部分（基址与鉴权、`x-oneapi-request-id`、三协议文本端点、模型 / 价格 / 用量日志接口、图片异步 / 同步 / 编辑端点、视频任务端点与状态集合、额度公式）见 [docs/08-zhiqiapi-integration.md](docs/08-zhiqiapi-integration.md)；其余上游细节（轮询响应与失败体的字段名、Responses 协议细节、`annotations`/`citations` 结构等）**以 zhiqiapi 官方文档为准**。

## 默认账号

| 账号 | 密码 | 用户组 | 说明 |
| --- | --- | --- | --- |
| `admin` | `admin123` | `super_admin` | 由 `seeds/seed.py` 幂等创建（账号 / 密码取自 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`），启动日志提示修改密码 |

- seed 只创建这一个账号（数据范围固定 `all`，即总后台）；运营 / 审核 / 只读账号在后台「系统 → 用户管理」新建并分配到 `operator`（只看本人项目）/ `reviewer` / `read_only`（四个系统用户组及其默认数据范围由迁移 `0002_seed_permissions` 写入）；示例项目的负责人是 `admin`，`operator` 用户登录后看不到它，需自建项目或由总后台转移负责人（验证步骤见 [docs/06-getting-started.md](docs/06-getting-started.md) 第五节第 15 步）。
- 首次登录后用 `POST /api/v1/admin/auth/change-password` 改密（密码 ≥ 8 位且含字母与数字），改密 / 登出使 `token_version += 1`，旧令牌立即失效（管理员被重置密码、启用 / 禁用、更换用户组时同样递增，见 [docs/07-admin-rbac.md](docs/07-admin-rbac.md) §7.4）；令牌有效期 `ADMIN_JWT_EXPIRE_SECONDS=7200`。
- 登录 15 分钟内失败 5 次锁定（`ADMIN_LOGIN_MAX_FAILURES`，Redis `rate:admin_login:{username}`）。
- 安全规则同 navigation：不可禁用自己、不可禁用 / 移除最后一个有效超管、系统组不可删除 / 停用 / 清空权限、管理员不物理删除；生产环境必须修改 `ADMIN_JWT_SECRET`。
- seed 同时写入：示例项目、系统 Prompt 模板（`sys_keyword`、`sys_title`、`sys_outline`、`sys_content`、`sys_section`、`sys_rewrite`/`sys_expand`/`sys_shorten`/`sys_restyle`、`sys_seo_meta`、`sys_faq`、`sys_image_prompt`、`sys_geo_query`、`sys_seo_query`，`language=zh-CN`、`is_system=1`、`status=published`）与 8 个默认发布平台（`is_system=1`，删除特征文案为示例初值，以实际平台页面为准、后台可维护）。

## 外部依赖（默认 Mock，无需真实密钥）

`ZHIQI_API_KEY` 为空时 `app.core.zhiqi.get_client()` 返回 `MockZhiqiClient`（`/api/v1/health` 与 `/admin/auth/me` 的 `zhiqi_mode` 为 `mock`），其它外部依赖按下表独立判定。

| 能力 | Mock 触发条件 | Mock 行为 | 接真实实现 |
| --- | --- | --- | --- |
| zhiqiapi 文本（关键词 / 标题 / 内容 / 重写、配图提示词） | `ZHIQI_API_KEY` 为空 | `mock_chat` 按模板返回模板化假文：关键词 → 20 个 JSON 词条，标题 → N 条，大纲 → JSON，正文 / 分段 → 含 H2 与 FAQ 的 Markdown，`seo_meta` / `faq` → JSON，`image_prompt` → 固定英文提示词；`request_id = "mock-" + uuid`；全局路由主模型 `mock-text`，三协议均可用 | 填 `ZHIQI_API_KEY` + `ZHIQI_TEXT_DEFAULT_MODEL`（协议 `ZHIQI_TEXT_DEFAULT_PROTOCOL`），或在后台「AI 网关 → 能力路由」直接改主 / 备模型 |
| zhiqiapi 图片生成 | 同上 | `mock_image_async` 返回 `task_mock_…`，状态机存 Redis `mock:task:{id}`，第 3 次轮询 `succeeded`，`data[0].url = PUBLIC_BASE_URL + /media/mock/placeholder.png`；`stream_download` 对 `/media/mock/` 路径直接复制本地占位文件，不发 HTTP；路由主模型 `mock-image`；参考 URL 不要求公网 | `ZHIQI_IMAGE_DEFAULT_MODEL`；参考图须为公网 URL（`PUBLIC_BASE_URL` 公网可达或对象存储公网地址） |
| zhiqiapi 视频生成 | 同上 | `mock_video_submit` 返回 `vidtask_mock_…`，第 5 次轮询 `succeeded`，URL 为 `/media/mock/placeholder.mp4`；路由主模型 `mock-video` | `ZHIQI_VIDEO_DEFAULT_MODEL`；全部参考素材须为公网 URL；轮询预算 `ZHIQI_VIDEO_POLL_BUDGET_SECONDS=1200` |
| zhiqiapi 模型目录与价格 | 同上 | `mock_models()` 返回 `mock-text`（openai / openai-response / anthropic）、`mock-image`（image-generation / image-generation-async / image-edit）、`mock-video`（openai-video）；`mock_pricing()` 倍率均为 1、按量计费 | `sync_models` 每 3600s 或 `POST /admin/ai/models/sync` 拉取 `GET /v1/models` + `GET /api/pricing_new` 写入 `ai_models` |
| zhiqiapi 用量对账 | 同上 | 每次文本 / 图片 / 视频调用向 Redis `mock:usage_logs` 写一条伪日志（保留 1000 条），`mock_token_logs()` 以 `/api/log/token` 的响应形态返回，对账流程与真实模式完全相同 | `reconcile_usage` 每 300s 拉取 `GET /api/log/token`，按 `request_id`（未命中时按 `upstream_task_id`）回填 `quota_actual` 与 `cost_cny` |
| SEO 收录检测 | 提供器 `zhiqi_web_search` 在 Mock 模式；`baidu_ai_search` / `bing_webmaster` / `google_search_console` 在 Mock 模式或对应 `credential_env` 为空 | `zhiqi_web_search` 经 `mock_chat` 返回 `indexed`（70%）/ `not_indexed`（30%），证据 `evidence_json.source="mock"`；非 zhiqi 提供器返回 `unknown` + `error_category=auth_failed`、`error_message=credential_missing` | 真实 zhiqiapi 联网模型（`ZHIQI_SEO_DEFAULT_MODEL` 或 `seo_providers.engines.<engine>.model`）；可选 `SEO_BAIDU_AI_SEARCH_API_KEY`、`SEO_BING_WEBMASTER_API_KEY` + `SEO_BING_SITE_URL`、`SEO_GSC_CREDENTIALS_FILE` + `SEO_GSC_SITE_URL`（后两者仅限自有已验证站点） |
| GEO 引用检测 | `ZHIQI_API_KEY` 为空 | 首次写入 `geo_engines` 时 6 个引擎（`baidu_ai`/`doubao`/`kimi`/`deepseek`/`perplexity`/`chatgpt`）全部 seed 为 `enabled=true`、`model` 允许为空；提问走 `mock-text`，`cited`（70%）/ `not_cited`（30%） | 「系统配置 → GEO 引擎」为每个引擎填写 zhiqiapi 上的联网模型并启用（`model` 为空不可启用）；各引擎对应的模型 ID 以 zhiqiapi 模型目录为准 |
| 对象存储 | `STORAGE_MODE=local`，或 `OSS_ENDPOINT` 为空（强制 local） | 文件落 `server/storage/`（`LOCAL_STORAGE_DIR`），经 `GET /media/{key}` 暴露，`media_assets.url = PUBLIC_BASE_URL + /media/{key}`；compose 内为共享卷 `media_data:/app/storage` | `STORAGE_MODE=oss`（`STORAGE_PROVIDER=s3`）+ `OSS_ENDPOINT` / `OSS_REGION` / `OSS_BUCKET` / `OSS_ACCESS_KEY` / `OSS_SECRET_KEY` / `OSS_PUBLIC_BASE_URL`（S3 兼容，阿里 OSS / 七牛 / MinIO 均可，客户端 boto3） |
| 链接删除检测 | **无 Mock**，始终真实抓取 | `safe_fetch` 只允许 http/https、端口 80/443、公网 IP（DNS 解析后校验，重定向逐跳校验）、≤ 3 跳、响应体 ≤ 2 MB、固定 UA `MONITOR_USER_AGENT`、不执行 JS、不带 Cookie；`127.0.0.1` / 内网地址的检测结果为 `unknown`（`matched_rule=ssrf_blocked`，连续 3 次后链接状态才写 `unknown`）；`MONITOR_ALLOW_HTTP=true` 允许 http 目标（生产环境设为 `false`，仅允许 https） | 无需额外配置；平台删除特征文案与跳转规则在后台「发布平台」维护 |
| 告警通道 | `alert_config.channels.webhook.enabled` / `email.enabled` 默认 `false` | 只投递站内告警（`in_app` 固定），告警中心与顶栏铃铛可见 | `ALERT_WEBHOOK_URL` + `ALERT_WEBHOOK_SECRET`（签名头 `X-Aicreat-Signature: sha256=`）、`SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `MAIL_FROM`，再在「系统配置 → 告警」打开对应通道（预留） |

- Mock 与真实模式切换只需改 `ZHIQI_API_KEY` 并重启三个后端进程；`ai_tasks`、`ai_usage_logs`、`media_assets` 等记录结构在两种模式下完全一致，便于先在 Mock 下验收流程再接真实密钥。切换后唯一需要人工处理的是 Mock 期间 seed 的 `geo_engines`（见上文「接入真实 zhiqiapi」第 5 步）。
- 真实模式的可靠性设计（指数退避重试、按能力 / 模型熔断、候选链、健康探测、失败分类 `unsupported_parameter` / `route_missing` / `model_unrouted` / `upstream_unavailable` / `rate_limited` / `timeout` / `quota_exceeded` / `auth_failed` / `content_blocked` / `media_storage` / `transfer_failed` / `invalid_response` / `breaker_open` / `cancelled` / `unknown`、每次失败记录 `request_id`）见 [docs/08-zhiqiapi-integration.md](docs/08-zhiqiapi-integration.md)。

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ZHIQI_API_KEY` | 空 | 空 = Mock 模式；填写后走真实 zhiqiapi（`ZHIQI_BASE_URL=https://zhiqiapi.com/v1`） |
| `ZHIQI_TEXT_DEFAULT_MODEL` / `ZHIQI_IMAGE_DEFAULT_MODEL` / `ZHIQI_VIDEO_DEFAULT_MODEL` / `ZHIQI_GEO_DEFAULT_MODEL` / `ZHIQI_SEO_DEFAULT_MODEL` | 空 | 真实模式各能力全局路由（`capability_routes`，`project_id=0`）的主模型：在 `ensure_default_routes` 新建全局路由，或把主模型仍以 `mock-` 开头的路由替换为真实模型时生效（变量为空则该路由 `is_enabled=0` 并记启动告警日志）；已经是真实模型的路由不再被覆盖，以后台「AI 网关 → 能力路由」为准 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8100` | 对外可访问的服务基址，`/media` 公网 URL 前缀；真实模式必须公网可达 |
| `DATABASE_URL` / `REDIS_URL` | `mysql+pymysql://aicreat:password@127.0.0.1:3306/aicreat` / `redis://127.0.0.1:6379/0` | compose 中由 `environment:` 覆盖为容器内地址 |
| `STORAGE_MODE` | `local` | `local` / `oss`：`local` 时文件落 `server/storage/`，经 `/media/{key}` 暴露；`oss` 时走 S3 兼容对象存储（`STORAGE_PROVIDER` 默认 `s3`，仅支持 `s3`）；生产固定 `oss` |
| `OSS_ENDPOINT` / `OSS_REGION` / `OSS_BUCKET` / `OSS_ACCESS_KEY` / `OSS_SECRET_KEY` | 空 | S3 兼容 endpoint、区域、桶与 AK / SK（阿里 OSS / 七牛 / MinIO 均可）；`OSS_ENDPOINT` 为空时即使 `STORAGE_MODE=oss` 也强制 `local` |
| `OSS_PUBLIC_BASE_URL` | `http://127.0.0.1:8100/media` | oss 模式对象公网 / CDN 基址，素材 URL = `OSS_PUBLIC_BASE_URL + /{key}`；默认值只是本机占位，切到 `oss` 时必须改为公网 / CDN 基址，否则素材 URL 会落成 127.0.0.1 地址 |
| `ADMIN_JWT_SECRET` | `please-change-me-admin` | 管理员 JWT 密钥；生产必须修改（≥ 32 字节随机串），改后全部管理员需重新登录 |

全部变量（含超时 / 重试 / 熔断 / 频控 / 配额 / 监控抓取参数及其 seed 到 settings 配置键的路径）见 [docs/05-deployment.md](docs/05-deployment.md)。

## Docker 部署（简述）

```bash
cp .env.example .env            # 修改 MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / ADMIN_JWT_SECRET / DEV_MODE=false / ALLOWED_ORIGINS /
                                # PUBLIC_BASE_URL（https）/ STORAGE_MODE=oss + OSS_* / MONITOR_ALLOW_HTTP=false / MONITOR_USER_AGENT /
                                # SEED_ADMIN_PASSWORD / ZHIQI_*，完整必改项见 docs/05-deployment.md §2.3
pnpm install --frozen-lockfile && pnpm build:shared && pnpm build:admin   # 严格按已提交的 pnpm-lock.yaml 安装（锁文件缺失或与 package.json 不一致即失败）；先构建 @aicreat/shared（admin 依赖其 dist/），产物 apps/admin/dist 由 nginx 挂载
docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"
docker compose up -d            # mysql / redis / server / worker / monitor-worker / nginx
```

模板默认值面向本机开发：原样上线会带着 `DEV_MODE=true`（CORS 放宽、错误响应带堆栈、允许 http 参考 URL）与 `MONITOR_ALLOW_HTTP=true`；预发与生产都要求 `DEV_MODE=false`，生产另须 `STORAGE_MODE=oss`、`MONITOR_ALLOW_HTTP=false`（环境矩阵见 [docs/05-deployment.md](docs/05-deployment.md) §2.5）。

访问 `http://<host>:${NGINX_HTTP_PORT}/admin/`（`/` 自动 302 到 `/admin/`）；`server` / `worker` / `monitor-worker` 共用 `build: ./server` 镜像、不同 `command`，均 `depends_on` mysql / redis 健康检查并 `restart: unless-stopped`；`server` 的 healthcheck 为 `curl -f http://127.0.0.1:8000/api/v1/health`。Nginx 路由、构建产物、发布 / 迁移回滚、备份（`scripts/db-backup.sh`）与监控指标见 [docs/05-deployment.md](docs/05-deployment.md)。

预发环境先保持 `ZHIQI_API_KEY` 为空（Mock 模式）跑一遍冒烟：`docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000`（超管已改密时追加 `--password '<新密码>'`，参数说明见上文「5. 验证 Mock 全流程」），通过后再切换真实 Key（[docs/05-deployment.md](docs/05-deployment.md) §7.2）。

## 文档

设计文档索引（00~13 各篇说明、代码入口表、实施顺序 1~10）见 [docs/README.md](docs/README.md)；功能设计类文档 07~13 末尾均含「测试范围」「验收标准」「实施顺序」。

## 说明

本仓库代码按 `docs/` 文档一次性生成，未经运行调试。外部真实对接按适配器接口替换即可：zhiqiapi 在 `server/app/core/zhiqi/`（`ZhiqiClient` / `MockZhiqiClient`），对象存储在 `server/app/core/storage.py`（`LocalStorage` / `S3Storage`），SEO 提供器与 GEO 引擎在 `server/app/services/index_providers/`，告警通道在 `server/app/services/alert_service.py`。表名、字段、状态枚举、接口路径、权限码、环境变量在全套文档中保持一致，实施顺序以 [docs/README.md](docs/README.md) 为准。
