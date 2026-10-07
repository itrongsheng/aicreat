# 02 项目结构

本工程采用 **pnpm monorepo**：后端 FastAPI 与两个轮询式 worker 进程放在 `server/`，唯一前端（管理后台）为 `apps/admin`，前后端共享的 TypeScript 类型 / 枚举 / 常量放在 `packages/shared`。本文档是 **目录树、模块职责与命名约定的权威说明**；其它文档引用目录与文件名时以本文为准。

- 服务拓扑、数据流、Redis 键表、worker 任务总表见 [01-architecture](./01-architecture.md)。
- 表结构见 [03-data-model](./03-data-model.md)，接口见 [04-api-spec](./04-api-spec.md)，权限码见 [07-admin-rbac](./07-admin-rbac.md)。
- 环境变量、docker-compose 与 Nginx 的完整说明见 [05-deployment](./05-deployment.md)；本地启动见 [06-getting-started](./06-getting-started.md)。

## 1. 顶层目录树

```text
aicreat/
├── docs/                      # 设计文档（README + 00~13，即本目录）
├── server/                    # FastAPI 后端 + app.worker + app.monitor_worker
├── apps/
│   └── admin/                 # 唯一前端：Vue 3 管理后台（base /admin/，端口 5174）
├── packages/
│   └── shared/                # @aicreat/shared：前后端共享的 TS 类型 / 枚举 / 常量
├── nginx/
│   └── nginx.conf             # /api/ 与 /media/ → server:8000；/admin/ → admin dist；/ → 302 /admin/
├── scripts/
│   ├── dev-setup.ps1          # Windows 首次一键初始化（.env、MySQL / Redis、venv、迁移与 seed、前端依赖），最后调用 dev-restart.ps1
│   ├── dev-restart.ps1        # 重启 API / worker / monitor / admin，端口占用自动换端口
│   └── db-backup.sh           # mysqldump 到 backups/，保留 14 天
├── docker-compose.yml         # mysql / redis / server / worker / monitor-worker / nginx
├── pnpm-workspace.yaml        # 声明 apps/*、packages/*
├── pnpm-lock.yaml             # 前端依赖锁文件（入库：首次 pnpm install 生成后提交；生产 / 镜像构建用 pnpm install --frozen-lockfile）
├── package.json               # 根脚本（dev:* / build* / smoke:api，见 §1.2）
├── .env.example               # 仓库根 .env 模板（compose 读取根 .env；server/.env 仅本机开发）
├── .gitignore
└── README.md                  # 工程说明（引用文档时用 docs/ 前缀）
```

### 1.1 顶层目录职责

| 目录 / 文件 | 职责 |
| --- | --- |
| `docs/` | 全部设计与使用文档；`docs/README.md` 为索引 |
| `server/` | 后端服务（`/api/v1`、`/media`）与两个 worker 进程，共用一个 Python 包 `app` 与一个 Docker 镜像 |
| `apps/admin/` | 面向运营 / 审核 / 管理员的后台，所有业务操作均在此完成（无 C 端） |
| `packages/shared/` | 被 `apps/admin` 引用的契约类型（`Project`、`Content`、`PublishLink`…）、状态枚举（`as const`）与常量（API 前缀、分页默认值、上传限制等） |
| `nginx/` | 生产反代与后台静态资源托管配置 |
| `scripts/` | 本机开发重启脚本与数据库备份脚本（一次性 / 运维脚本都放这里，不放进 `server/app`） |
| `docker-compose.yml` | 一键拉起依赖与全部服务；`server` / `worker` / `monitor-worker` 共用 `build: ./server` 镜像、不同 `command` |
| `pnpm-workspace.yaml` | 工作区定义：`apps/*`、`packages/*`（pnpm@9） |
| `pnpm-lock.yaml` | 前端依赖锁文件，提交入库（首次在仓库根执行 `pnpm install` 生成后提交）；本地开发用 `pnpm install`，生产 / 镜像构建用 `pnpm install --frozen-lockfile`（锁文件与 `package.json` 不一致时直接失败、不改写锁文件） |
| `.env.example` | 根 `.env` 与 `server/.env` 的同源模板；变量清单见 [05-deployment](./05-deployment.md) |
| `.gitignore` | 忽略 `.env`、`server/.env`、`server/.venv/`、`server/storage/`、`backups/`、`secrets/`（仓库根，存放 GSC 服务账号 JSON，见 [05-deployment](./05-deployment.md) §3.2、§9.5）、`node_modules/`、`apps/admin/dist/`、`packages/shared/dist/`、`__pycache__/`、`.pytest_cache/` |
| `README.md` | 仓库根说明（工程简介、功能概览、目录结构摘要、快速开始、默认账号 `admin/admin123`、外部依赖 Mock 表）；引用文档时用 `docs/` 前缀 |

### 1.2 根 `package.json` 脚本

| 脚本 | 命令 |
| --- | --- |
| `dev:server` | `cd server && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8100` |
| `dev:worker` | `cd server && .venv\Scripts\python.exe -m app.worker` |
| `dev:monitor` | `cd server && .venv\Scripts\python.exe -m app.monitor_worker` |
| `dev:admin` | `pnpm --filter admin dev` |
| `dev:setup` | `powershell -ExecutionPolicy Bypass -File scripts/dev-setup.ps1` |
| `dev:restart` | `powershell -ExecutionPolicy Bypass -File scripts/dev-restart.ps1` |
| `build:admin` | `pnpm --filter admin build` |
| `build:shared` | `pnpm --filter @aicreat/shared build` |
| `build` | `pnpm -r build` |
| `smoke:api` | `cd server && .venv\Scripts\python.exe scripts\integration_smoke.py` |

后端依赖通过 `server/pyproject.toml` + venv（`server/.venv/`，不入库）安装：在 `server/` 内执行 `pip install -e ".[dev]"`（可编辑安装，`app` 包因此可被 `python seeds/seed.py` 等脚本导入；不跑测试可只 `pip install -e .`）。前端依赖在仓库根由 pnpm workspace 统一安装：本地开发 `pnpm install`，生产 / 镜像构建 `pnpm install --frozen-lockfile`（锁文件 `pnpm-lock.yaml` 入库，见 §1.1）。`smoke:api` 不带参数运行：连接 `--base-url` 的缺省地址 `http://127.0.0.1:8100`，并以 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` 登录；API 在其它端口或超管已改密时，直接运行脚本并传 `--base-url` / `--username` / `--password`（见 §2.10）。

### 1.3 运行进程与端口

| 进程 | 入口 | 开发端口 / 容器端口 | 说明 |
| --- | --- | --- | --- |
| `server` | `app.main:app`（开发 uvicorn，生产 gunicorn + uvicorn workers） | 8100 / 8000 | `/api/v1/*` 与 `/media/{key}` |
| `worker` | `python -m app.worker` | — | AI 任务队列、媒体轮询与转存、用量对账、模型同步、健康探测、过期回收 |
| `monitor-worker` | `python -m app.monitor_worker` | — | 链接删除检测、SEO/GEO 收录检测、每日统计聚合、告警评估 |
| `admin` | Vite dev server（`pnpm --filter admin dev`） | 5174 | `base: /admin/`，代理 `/api`、`/media` → `http://127.0.0.1:8100` |
| `nginx` / `mysql` / `redis` | docker-compose | 80（`NGINX_HTTP_PORT`）/ 3306 / 6379 | 生产编排，见 [05-deployment](./05-deployment.md) |

## 2. 后端 `server/`

### 2.1 目录树（逐文件注释）

```text
server/
├── app/
│   ├── __init__.py
│   ├── main.py                        # create_app()：挂载 /api/v1 与 /media、CORS、异常处理器、审计中间件（AUDIT_TARGET_TYPES）、
│   │                                  #   启动钩子（lock:bootstrap 内 ensure_rbac_seed / ensure_default_settings / ensure_default_routes，复制 Mock 占位文件）
│   ├── worker.py                      # AI 任务进程入口：python -m app.worker（消费 queue:ai_tasks、轮询媒体任务、转存、对账、模型同步、健康探测、过期回收）
│   ├── monitor_worker.py              # 监控进程入口：python -m app.monitor_worker（删除检测、SEO/GEO 收录检测、每日统计聚合、告警评估）
│   ├── models.py                      # 全部 SQLAlchemy ORM 模型（24 张表）+ 顶部以 Literal 声明的状态枚举常量
│   ├── api/
│   │   ├── __init__.py                # api_router：汇总 /health 与 admin 子路由，挂载到 /api/v1（静态子路径先于 /{id} 注册）
│   │   ├── deps.py                    # get_db、get_locale、get_current_admin、require_permission、get_data_scope、get_pagination
│   │   ├── health.py                  # GET /health（db / redis / zhiqi_mode / worker 心跳，公开）
│   │   └── admin/                     # 全部业务接口（管理员 JWT + 权限码）
│   │       ├── __init__.py
│   │       ├── auth.py                # 登录 / me / 登出 / 改密 / site-info（login 与 site-info 公开）
│   │       ├── admins.py              # 管理员 CRUD、状态、重置密码
│   │       ├── admin_groups.py        # 用户组 CRUD、权限覆盖保存
│   │       ├── admin_permissions.py   # 权限树
│   │       ├── operation_logs.py      # 操作日志查询
│   │       ├── settings.py            # settings 读写（按 key 校验 JSON 结构）、runtime 非敏感子集（已登录即可读）
│   │       ├── projects.py            # 项目 CRUD、负责人（创建 / 转移、GET /owner-options）、归档、项目概览、项目级路由覆盖保存（PUT /{id}/routes）
│   │       ├── prompt_templates.py    # Prompt 模板 CRUD、版本、发布、预览渲染
│   │       ├── keywords.py            # 关键词列表 / 生成 / 导入（JSON 与 CSV 文件两个接口）/ 采用 / 弃用 / 导出
│   │       ├── titles.py              # 标题列表 / 生成 / 打分 / 采用 / 弃用
│   │       ├── contents.py            # 内容 CRUD、大纲 / 正文 / 重写 / SEO 要素生成（全部异步入队）、进行中任务查询（GET /{id}/task）、审核、版本（含删除历史版本）、素材绑定、导出
│   │       ├── generation_batches.py  # 生成批次查询 / 取消 / 重试
│   │       ├── media.py               # 图片 / 视频生成提交、素材列表、重试 / 转存 / 删除
│   │       ├── uploads.py             # 参考图 / 参考视频上传（返回公网 URL）
│   │       ├── ai_models.py           # 模型目录查询、/options 不分页选项列表、同步（挂载 /ai/models）
│   │       ├── ai_routes.py           # 能力路由 CRUD、健康探测、熔断重置、健康快照（挂载 /ai：/routes/…、/health、/health/probe）
│   │       ├── ai_tasks.py            # AI 任务查询 / 重试 / 取消 / 导出（挂载 /ai/tasks）
│   │       ├── ai_usage.py            # 用量日志、对账触发、用量汇总（挂载 /ai/usage）
│   │       ├── platforms.py           # 发布平台与删除特征规则 CRUD、规则测试、URL 识别
│   │       ├── links.py               # 回填链接 CRUD、手动检测、收录标记、重置基线、暂停 / 恢复、历史、导出
│   │       ├── monitoring.py          # 检测记录查询、监控概览、手动批量触发（link-checks/run、index-checks/run）
│   │       ├── alerts.py              # 告警中心：列表 / 确认 / 解决 / 忽略 / 汇总
│   │       └── stats.py               # 总览 / 趋势 / 分解 / 榜单 / 导出 / 重算
│   ├── core/                          # 基础设施（无业务规则；不得导入 services / models）
│   │   ├── __init__.py
│   │   ├── config.py                  # Settings(BaseSettings)：全部环境变量与派生属性
│   │   ├── database.py                # engine、SessionLocal、Base、get_db
│   │   ├── redis.py                   # redis_client、cache_get_json / cache_set_json / cache_delete / cache_delete_prefix
│   │   ├── locks.py                   # acquire_lock / release_lock / extend_lock、with_lock(key, ttl, wait_seconds=0) 上下文（LockTimeout）
│   │   ├── security.py                # hash_password / verify_password / create_token / decode_token（aud=admin）
│   │   ├── storage.py                 # StorageBackend / LocalStorage / S3Storage（boto3）、get_storage、public_url_for、probe_image_size（只解析 PNG IHDR / JPEG SOF / WebP VP8 头，无第三方依赖）、sniff_media_type（按魔数识别 PNG/JPEG/WebP/GIF/MP4）
│   │   ├── ratelimit.py               # check_rate_limit / enforce_interval（Redis 滑动窗口、同域名最小间隔）；并发信号量为进程内 BoundedSemaphore（worker 内），不走 Redis
│   │   ├── response.py                # ok / fail / paginated / Page
│   │   ├── exceptions.py              # BusinessError、业务码常量（CODE_*）、register_exception_handlers
│   │   ├── admin_permissions.py       # PERMISSIONS / PERMISSION_CODES / _resource / PERMISSION_DEPENDENCIES / SYSTEM_GROUPS / OPERATOR_EXCLUDED / DEFAULT_GROUP_PERMISSIONS
│   │   ├── safe_fetch.py              # SSRF 安全抓取：normalize_public_url / assert_public_url / fetch_public_bytes / fetch_page / robots_allowed / stream_public_bytes（媒体转存用，返回 DownloadResult：follow_redirects=False、逐跳 assert_public_url、≤3 跳、不带 Authorization）
│   │   ├── fingerprint.py             # normalize_title（NFKC → 去掉站点后缀（最后一个 " - " / " | " / " _ " / "｜" 之后的部分，仅当剩余 ≥ 4 字符）→ 去标点与空白 → 小写；None → ""）/ simhash64（返回有符号 64 位整数，补码映射）/ hamming_distance（先转回无符号再异或）/ extract_main_text
│   │   ├── urls.py                    # normalize_url / url_hash / extract_domain / match_url_patterns
│   │   └── zhiqi/                     # zhiqiapi 适配层：只做传输与契约，不读数据库
│   │       ├── __init__.py            # get_client()、导出 Capability / Protocol / ErrorCategory
│   │       ├── types.py               # 能力 / 协议 / 错误枚举、Timeouts、RetryPolicy、请求与结果 dataclass（含下载结果 DownloadResult）
│   │       ├── errors.py              # ZhiqiError、ZhiqiBreakerOpen、classify_error、is_retryable、is_fallbackable、classify_task_failure
│   │       ├── client.py              # ZhiqiClient（httpx、Bearer、x-oneapi-request-id、按调用注入 RetryPolicy 的幂等重试；stream_download 返回 DownloadResult、只接受相对路径，绝对 URL 下载改走 safe_fetch.stream_public_bytes）
│   │       ├── text.py                # chat / responses / messages 三协议文本调用、build_payload、parse_result、extract_citations、extract_json
│   │       ├── images.py              # submit_async / get_generation / generate_sync / edit_sync / build_async_payload / validate_request
│   │       ├── videos.py              # submit_video / get_video / download_content（返回 DownloadResult）/ build_payload / validate_request
│   │       ├── catalog.py             # list_models / list_pricing / derive_modalities / protocol_for
│   │       ├── usage.py               # fetch_token_logs / estimate_quota / estimate_tokens / quota_to_cny
│   │       ├── health.py              # probe / status_from
│   │       ├── breaker.py             # CircuitBreaker（状态存 Redis ai:breaker:*；由 ai_gateway_service.get_breaker 按当前 ai_routing_config.breaker 构造）
│   │       ├── mock.py                # MockZhiqiClient 与 mock_* 占位实现（ZHIQI_API_KEY 为空时启用；stream_download 对 /media/mock/ 路径直接复制本地文件，不发 HTTP）
│   │       └── mock_assets/           # placeholder.png、placeholder.mp4
│   ├── schemas/                       # Pydantic v2 请求 / 响应模型（一资源一文件）
│   │   ├── __init__.py
│   │   ├── common.py                  # PageParams、IdList、StatusBody、ExportParams
│   │   ├── auth.py                    # LoginBody、ChangePasswordBody、MeOut
│   │   ├── admin_rbac.py              # AdminCreate / AdminUpdate / AdminStatusBody、GroupCreate / GroupUpdate、PermissionCodesBody
│   │   ├── settings.py                # SettingItem、各配置键的校验模型（GenerationConfig、MediaConfig、MonitoringConfig…）
│   │   ├── project.py
│   │   ├── prompt_template.py
│   │   ├── keyword.py                 # KeywordGenerateBody、KeywordImportBody、KeywordOut
│   │   ├── title.py
│   │   ├── content.py                 # ContentGenerateBody、RewriteBody、ReviewBody、VersionOut
│   │   ├── generation_batch.py
│   │   ├── media.py                   # ImageGenerateBody、VideoGenerateBody、AssetOut
│   │   ├── ai.py                      # ModelOut、RouteUpsert、ProbeBody、TaskOut、UsageLogOut
│   │   ├── platform.py
│   │   ├── link.py                    # LinkBackfillBody、LinkBatchBody、MarkIndexBody
│   │   ├── monitoring.py
│   │   ├── alert.py
│   │   └── stats.py
│   ├── services/                      # 业务编排、事务边界、缓存读写
│   │   ├── __init__.py
│   │   ├── i18n.py                    # normalize_locale
│   │   ├── admin_rbac_service.py      # ensure_rbac_seed / has_permission / 管理员与用户组 CRUD / write_audit / list_operation_logs
│   │   ├── data_scope_service.py      # DataScope / SYSTEM_SCOPE、按项目负责人的范围谓词、get_visible / require_project、owner_options（见 13）
│   │   ├── settings_service.py        # get_value / set_value / get_config(key) / ensure_default_settings、DEFAULT_SETTINGS、ENV_SEED_PATHS
│   │   ├── project_service.py         # 项目 CRUD、负责人规则（创建 / 转移）、归档、删除校验、save_project_routes
│   │   ├── prompt_template_service.py # CRUD、版本、render(template, variables)、resolve_template(kind, project_id, language)
│   │   ├── keyword_service.py         # 归一化、去重、导入、状态流转
│   │   ├── title_service.py           # 标题 CRUD、打分、状态流转、title_dedup_key（标题去重键：NFKC → 去标点与空白 → 小写，不剥离站点后缀，见 09 §7.3；与 fingerprint.normalize_title 不同，后者先剥离站点后缀）
│   │   ├── content_service.py         # 内容状态机（transition）、版本写入、质量规则、导出
│   │   ├── generation_service.py      # 创建 generation_batches 与 ai_tasks、批次收敛（on_task_finished）、取消 / 重试
│   │   ├── ai_gateway_service.py      # 能力路由解析、候选链、熔断、额度预占 / 结算（check_quota / settle_quota）、ai_tasks 尝试行记录、全局暂停（ai:paused:*）、complete_text / submit_image / submit_video / poll_task、ensure_default_routes
│   │   ├── ai_task_service.py         # claim / dispatch：按根任务 operation + input_json 执行并把结果写回业务对象
│   │   ├── ai_catalog_service.py      # 模型目录与价格同步到 ai_models、catalog_entry（读 cache:ai:models:catalog，键缺失时由 ai_models 回填）
│   │   ├── ai_usage_service.py        # 拉取 /api/log/token、写 ai_usage_logs、按 request_id / upstream_task_id 回填 quota_actual 与 cost_cny
│   │   ├── media_service.py           # 素材 CRUD、生成请求参数映射、提交 / 轮询 / 转存状态机
│   │   ├── platform_service.py        # 平台 CRUD、URL 识别、规则测试
│   │   ├── link_service.py            # 回填（URL 安全校验）、归一化、调度时间计算、enqueue_check、rebaseline、暂停 / 恢复、人工标记收录
│   │   ├── link_check_service.py      # 单链接删除检测：抓取 → 判定 → 状态流转 → 告警
│   │   ├── index_check_service.py     # SEO/GEO 收录检测编排：enabled_engines(kind)、提供器 / 引擎调用（经 ai_gateway_service.complete_text 同步记根任务 + 尝试行）→ 记录 → 回写链接
│   │   ├── index_providers/           # 收录检测提供器与 GEO 引擎实现
│   │   │   ├── __init__.py            # get_seo_provider(code) / get_geo_engine(code)
│   │   │   ├── base.py                # SeoProvider / GeoEngine Protocol、CheckResult dataclass
│   │   │   ├── zhiqi_web_search.py    # 默认提供器：zhiqiapi 联网模型按 URL / 标题检索
│   │   │   ├── baidu_ai_search.py     # 百度 AI 搜索官方 API（需 SEO_BAIDU_AI_SEARCH_API_KEY）
│   │   │   ├── bing_webmaster.py      # Bing Webmaster API（仅自有已验证站点）
│   │   │   ├── google_search_console.py # GSC URL Inspection（仅自有已验证站点）
│   │   │   ├── manual.py              # 人工标记
│   │   │   └── geo_engine.py          # 通用 GEO 引擎：模型 + 协议 + 提示模板 + 解析规则；实现 GeoEngine 协议的 precheck（调用前预检，见 11 §8.2）与 check
│   │   ├── alert_service.py           # raise_alert（dedupe）/ resolve / evaluate 规则 / 通道投递（站内，预留 webhook / 邮件）
│   │   └── stats_service.py           # overview / trends / breakdown / rankings / export / aggregate_daily / 实时计数
│   └── tasks/                         # worker 周期任务（[worker] = app.worker，[monitor] = app.monitor_worker）
│       ├── __init__.py
│       ├── run_ai_tasks.py            # [worker] 消费 queue:ai_tasks
│       ├── poll_media_tasks.py        # [worker] 轮询 polling 状态的媒体任务
│       ├── transfer_media.py          # [worker] 下载上游临时 URL 并转存到本地 / 对象存储
│       ├── reconcile_usage.py         # [worker] 用量对账
│       ├── sync_models.py             # [worker] 模型目录与价格同步
│       ├── health_probe.py            # [worker] 路由健康探测
│       ├── recover_stale_tasks.py     # [worker] 心跳超时 / 轮询超期回收、批次收敛
│       ├── cleanup_media.py           # [worker] 清理失败 / 孤儿素材文件
│       ├── schedule_link_checks.py    # [monitor] 扫描到期链接入队
│       ├── run_link_checks.py         # [monitor] 消费 queue:link_checks
│       ├── schedule_index_checks.py   # [monitor] 扫描到期收录检测入队
│       ├── run_index_checks.py        # [monitor] 消费 queue:index_checks
│       ├── aggregate_daily_stats.py   # [monitor] daily_stats 重算（含消费 queue:stats_recompute 的异步重算请求）
│       └── evaluate_alerts.py         # [monitor] 周期性告警规则评估
├── migrations/                        # Alembic 迁移
│   ├── env.py
│   └── versions/
│       ├── 0001_initial.py            # 全部 24 张表
│       └── 0002_seed_permissions.py   # 只写权限码与系统用户组（默认 settings / 路由 / 超管 / 平台 / 模板不在迁移里）
├── seeds/
│   ├── __init__.py
│   └── seed.py                        # 幂等 upsert：默认超级管理员 admin/admin123、示例项目、系统 Prompt 模板（zh-CN）、默认平台
├── scripts/
│   └── integration_smoke.py           # Mock 模式端到端冒烟：登录 → 关键词 → 标题 → 内容 → 图片 → 回填 → 检测（含回填 404 公网 URL 的删除检测 / 告警分支）→ 报表；参数 --base-url / --username / --password
├── tests/                             # pytest
│   ├── conftest.py                    # SQLite / MySQL 测试会话、Mock 客户端、管理员 token 夹具
│   ├── test_admin_rbac.py
│   ├── test_data_scope.py             # 用户数据隔离：列表 / 详情 404 / 引用校验 / owned_by_other / 负责人转移 / 统计范围 / 路由覆盖
│   ├── test_zhiqi_adapter.py          # 三协议请求体、错误分类、重试、熔断、Mock
│   ├── test_generation.py             # 关键词 / 标题 / 内容生成与状态机
│   ├── test_media.py                  # 图片 / 视频任务生命周期与转存
│   ├── test_links.py                  # 回填、归一化、调度时间
│   ├── test_monitoring.py             # 删除判定、收录检测、告警
│   └── test_stats.py                  # daily_stats 聚合与指标公式
├── storage/                           # 本地 Mock 存储（gitignore；compose 内为共享卷 media_data:/app/storage）
├── alembic.ini
├── pyproject.toml                     # 包名 aicreat-server：依赖与工具配置
├── Dockerfile                         # server / worker / monitor-worker 共用镜像
└── .env.example                       # 根 .env.example 的副本（本机开发复制为 server/.env）
```

### 2.2 分层与依赖方向

```mermaid
flowchart LR
    subgraph PROC["进程"]
        MAIN["main.py 创建 app"]
        WK["worker.py"]
        MW["monitor_worker.py"]
    end
    subgraph LAYERS["分层"]
        API["api/ 路由层 + deps.py"]
        SCH["schemas/ 入参校验、出参序列化"]
        SVC["services/ 业务编排、事务、缓存"]
        TSK["tasks/ 周期任务"]
        CORE["core/ 基础设施"]
        ZQ["core/zhiqi 适配层"]
        ORM["models.py"]
    end
    MAIN --> API
    WK --> TSK
    MW --> TSK
    API --> SCH
    API --> SVC
    API --> CORE
    TSK --> SVC
    TSK --> CORE
    SVC --> ORM
    SVC --> CORE
    SVC --> ZQ
    ZQ --> CORE
    CORE --> MYSQL[(MySQL)]
    CORE --> REDIS[(Redis)]
    ZQ --> UP[(zhiqiapi)]
```

依赖只允许自上而下：`api` → `schemas` / `services` / `core`（路由只用 `core.response`、`core.exceptions`，`deps.py` 用 `core.security` / `core.database`）；`tasks` → `services` / `core`（只用 `core.redis` 的队列操作与 `core.locks`）；`services` → `models` / `core` / `core.zhiqi`；`core` 只依赖 `core` 内部与第三方库。`core/zhiqi` **不读数据库**，路由解析、候选链、额度落库与业务回写全部在 `services/ai_gateway_service.py`。`worker.py` / `monitor_worker.py` 只做主循环（领取、提交线程池、心跳），业务都在 `tasks/*` 与 `services/*`。

### 2.3 后端模块职责

| 模块 | 职责 |
| --- | --- |
| `app/main.py` | `create_app()`：`include_router(api_router, prefix="/api/v1")`、`/media/{key}` 文件路由（`local` 直接读 `LOCAL_STORAGE_DIR`，`oss` 回源对象存储）、CORS（`ALLOWED_ORIGINS`）、`register_exception_handlers`、请求 ID 中间件（每个请求生成 `request_id` 并写响应头 `X-Request-Id`，审计日志 `admin_operation_logs.request_id` 记同值）、审计中间件（写接口成功后按 `AUDIT_TARGET_TYPES` 写 `admin_operation_logs`；`request.state.audit_written=True` 的接口跳过）、启动钩子（`lock:bootstrap` 内 `ensure_rbac_seed` + `ensure_default_settings` + `ensure_default_routes`，把 `core/zhiqi/mock_assets/` 复制到 `LOCAL_STORAGE_DIR/mock/`） |
| `app/worker.py` | `python -m app.worker` 主循环：启动时在 `lock:bootstrap` 内执行 ensure_*，再 `named_submit("recover", recover_stale_tasks.recover)`；线程池 `TrackedThreadPool(max_workers = AI_MAX_CONCURRENCY_TEXT + AI_MAX_CONCURRENCY_IMAGE + AI_MAX_CONCURRENCY_VIDEO + 2)`，信号量 `sems = {"text", "image", "video"}`（各取对应 `AI_MAX_CONCURRENCY_*`）；每轮心跳 `worker:heartbeat:worker:{hostname}:{pid}`（TTL 900s），`run_ai_tasks.drain` + `poll_media_tasks.poll_due`，周期提交对账 / 模型同步 / 健康探测 / 过期回收 / 转存重试 / 每日清理到进程内线程池；主循环线程不做任何网络 I/O，单轮阻塞 ≤ 2s，有任务提交时不休眠、否则 `sleep(WORKER_POLL_INTERVAL_SECONDS)`。主循环辅助类 `TrackedThreadPool`（带 `in_flight` 计数与 `named_submit` 的 `ThreadPoolExecutor` 子类）、`PeriodicTimers`（`run_due` / `run_daily`）与 `heartbeat(name)` 定义在本文件顶部，`monitor_worker.py` 从 `app.worker` 导入复用（两进程同构，骨架见 [01-architecture](./01-architecture.md)） |
| `app/monitor_worker.py` | `python -m app.monitor_worker` 主循环：启动时在 `lock:bootstrap` 内执行 ensure_*，再 `named_submit("catch_up", aggregate_daily_stats.catch_up)`（补算最近 3 天）；线程池 `TrackedThreadPool(link_check.global_concurrency + index_check.concurrency + 2)`，信号量 `sems = {"link_check", "index_check"}`；每轮心跳 `worker:heartbeat:monitor_worker:{hostname}:{pid}`，`schedule_*` 入队、`run_link_checks.drain` / `run_index_checks.drain`、`drain_recompute`、每日聚合与告警评估；休眠 `MONITOR_POLL_INTERVAL_SECONDS` |
| `app/models.py` | 24 张表的 ORM 定义与 `Literal` 枚举常量（见 [03-data-model](./03-data-model.md)） |
| `app/api/__init__.py` | 路由汇总与前缀挂载（见 §2.4） |
| `app/api/deps.py` | `get_db`（会话）、`get_locale`（`lang` 参数 / `Accept-Language`，经 `services.i18n.normalize_locale`）、`get_current_admin`（解析 `aud=admin` JWT，校验 `ver` = `admins.token_version`、管理员 `is_active=1` 且所属用户组 `is_active=1`，任一不满足返回 401，用户组停用时为「管理员用户组已停用」；见 [07-admin-rbac](./07-admin-rbac.md) §7.2）、`require_permission(code)`（校验权限，不满足返回 403「无权执行此操作」、`data={"permission": code}`；通过后写 `request.state.permission_code`）、`get_data_scope`（读所属用户组 `data_scope` 与查询参数 `owner_id`，返回 `DataScope`，见 [13-user-data-scope](./13-user-data-scope.md) §9.1）、`get_pagination`（`page` / `page_size`，默认 20、最大 100） |
| `app/api/health.py` | `GET /api/v1/health`（公开）：返回 `status` / `db` / `redis` / `zhiqi_mode` / `workers`（`SCAN worker:heartbeat:*` 汇总）/ `warnings[]` / `version`；db / redis 不可用返回 503，worker 不活跃仅 `degraded` |
| `app/api/admin/*` | 全部业务接口，每个文件一个资源；只做入参校验、权限声明与调用 service，不写业务规则 |
| `app/core` | 配置、数据库、Redis、锁、安全、存储、频控、响应、异常、权限定义、SSRF 安全抓取、指纹、URL 工具 |
| `app/core/zhiqi` | zhiqiapi 传输与契约层（见 [08-zhiqiapi-integration](./08-zhiqiapi-integration.md)） |
| `app/schemas` | 入参校验与出参序列化；API 字段名 = 列名去掉 `_json` 后缀 |
| `app/services` | 业务编排、状态机、事务边界、缓存读写、Redis 计数、告警触发 |
| `app/services/index_providers` | SEO 收录检测提供器与 GEO 引擎实现（见 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md)） |
| `app/tasks` | 两个 worker 的周期任务函数（见 §2.8；调度频率、锁与幂等规则见 [01-architecture](./01-architecture.md)） |
| `migrations` | 表结构版本管理（`0001_initial`、`0002_seed_permissions`） |
| `seeds/seed.py` | 幂等初始化：超管、示例项目、系统 Prompt 模板、默认平台 |
| `scripts/integration_smoke.py` | Mock 模式端到端冒烟（根脚本 `smoke:api`） |
| `tests` | pytest 单元 / 集成测试 |
| `storage/` | `STORAGE_MODE=local` 时的文件目录，经 `/media/{key}` 暴露 |

### 2.4 路由文件与挂载前缀

所有业务接口都在 `/api/v1/admin/*` 之下；`app/api/__init__.py` 以 `admin.include_router(x.router, prefix="/<kebab>")` 挂载，再把 `admin` 挂到 `api_router`，`main.py` 以 `prefix="/api/v1"` 挂载 `api_router`。接口明细见 [04-api-spec](./04-api-spec.md)。

| 路由文件 | 挂载前缀（`/api/v1` 之后） | 权限模块 | 说明 |
| --- | --- | --- | --- |
| `api/health.py` | `/health` | 公开 | 直接挂在 `api_router` |
| `admin/auth.py` | `/admin/auth` | — | `POST /login`、`GET /site-info` 公开；`me` / `logout` / `change-password` 已登录 |
| `admin/admins.py` | `/admin/admins` | `security.admins.*` | |
| `admin/admin_groups.py` | `/admin/admin-groups` | `security.groups.*` | `PUT /{id}/permissions` 沿用 navigation 形式 |
| `admin/admin_permissions.py` | `/admin/admin-permissions` | `security.groups.view` | 权限树 `GET /tree` |
| `admin/operation_logs.py` | `/admin/admin-operation-logs` | `security.audit.view` | |
| `admin/settings.py` | `/admin/settings` | `system.settings.*` | `GET /runtime` 已登录即可读 |
| `admin/projects.py` | `/admin/projects` | `content.projects.*` | |
| `admin/prompt_templates.py` | `/admin/prompt-templates` | `content.prompt_templates.*` | |
| `admin/keywords.py` | `/admin/keywords` | `content.keywords.*` | |
| `admin/titles.py` | `/admin/titles` | `content.titles.*` | |
| `admin/contents.py` | `/admin/contents` | `content.contents.*` | 版本子资源 `/{id}/versions/{version_id}` |
| `admin/generation_batches.py` | `/admin/generation-batches` | `content.batches.*` | |
| `admin/media.py` | `/admin/media` | `media.assets.*` / `media.images.*` / `media.videos.*` | |
| `admin/uploads.py` | `/admin/uploads` | `system.upload.*` | |
| `admin/ai_models.py` | `/admin/ai/models` | `ai.models.*` | |
| `admin/ai_routes.py` | `/admin/ai` | `ai.routes.*` | 内部路径 `/routes`、`/routes/{id}`、`/routes/{id}/test`、`/routes/{id}/reset-breaker`、`/health`、`/health/probe` |
| `admin/ai_tasks.py` | `/admin/ai/tasks` | `ai.tasks.*` | |
| `admin/ai_usage.py` | `/admin/ai/usage` | `ai.usage.*` | |
| `admin/platforms.py` | `/admin/platforms` | `publish.platforms.*` | |
| `admin/links.py` | `/admin/links` | `publish.links.*` | |
| `admin/monitoring.py` | `/admin/monitoring` | `monitoring.link_checks.*` / `monitoring.index_checks.*` | |
| `admin/alerts.py` | `/admin/alerts` | `monitoring.alerts.*` | |
| `admin/stats.py` | `/admin/stats` | `stats.reports.*` / `dashboard.view` | |

`app/api/__init__.py` 的挂载骨架（与 navigation 同形）：

```python
"""汇总所有路由，挂载到 /api/v1。"""
from fastapi import APIRouter

from app.api import health
from app.api.admin import (
    auth as admin_auth, admins, admin_groups, admin_permissions, operation_logs, settings,
    projects, prompt_templates, keywords, titles, contents, generation_batches,
    media, uploads, ai_models, ai_routes, ai_tasks, ai_usage,
    platforms, links, monitoring, alerts, stats,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])                 # GET /api/v1/health

admin = APIRouter(prefix="/admin")
admin.include_router(admin_auth.router, prefix="/auth", tags=["admin-auth"])
admin.include_router(admins.router, prefix="/admins", tags=["admin-rbac"])
admin.include_router(admin_groups.router, prefix="/admin-groups", tags=["admin-rbac"])
admin.include_router(admin_permissions.router, prefix="/admin-permissions", tags=["admin-rbac"])
admin.include_router(operation_logs.router, prefix="/admin-operation-logs", tags=["admin-rbac"])
admin.include_router(settings.router, prefix="/settings", tags=["system"])
admin.include_router(projects.router, prefix="/projects", tags=["content"])
admin.include_router(prompt_templates.router, prefix="/prompt-templates", tags=["content"])
admin.include_router(keywords.router, prefix="/keywords", tags=["content"])
admin.include_router(titles.router, prefix="/titles", tags=["content"])
admin.include_router(contents.router, prefix="/contents", tags=["content"])
admin.include_router(generation_batches.router, prefix="/generation-batches", tags=["content"])
admin.include_router(media.router, prefix="/media", tags=["media"])
admin.include_router(uploads.router, prefix="/uploads", tags=["media"])
admin.include_router(ai_models.router, prefix="/ai/models", tags=["ai"])
admin.include_router(ai_routes.router, prefix="/ai", tags=["ai"])         # /ai/routes/…、/ai/health
admin.include_router(ai_tasks.router, prefix="/ai/tasks", tags=["ai"])
admin.include_router(ai_usage.router, prefix="/ai/usage", tags=["ai"])
admin.include_router(platforms.router, prefix="/platforms", tags=["publish"])
admin.include_router(links.router, prefix="/links", tags=["publish"])
admin.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])
admin.include_router(alerts.router, prefix="/alerts", tags=["monitoring"])
admin.include_router(stats.router, prefix="/stats", tags=["stats"])
api_router.include_router(admin)
```

路由文件内部的固定写法：

- 每个处理函数以 `Depends(require_permission("module.resource.action"))` 声明单一权限码；只需登录的接口用 `Depends(get_current_admin)`。
- **静态子路径必须在同方法的 `/{id}` 路由之前注册**（`summary` / `export` / `generate` / `import` / `import-file` / `batch` / `batch-*` / `sync` / `options` / `owner-options` / `health` / `probe` / `logs` / `reconcile` / `detect` / `overview` / `runtime` / `tree` / `site-info` / `link-checks` / `index-checks`），否则 Starlette 会把它们匹配成 `{id}`（`{id}` 匹配 `[^/]+`）并返回 422。
- 对象级动作 `POST /{resource}/{id}/{action}`，集合级动作 `POST /{resource}/{action}`，动作名 kebab-case；RBAC 保留 `PATCH /admins/{id}/status` 与 `PUT /admin-groups/{id}/permissions` 两个非 POST 动作。
- 返回值一律经 `core.response.ok / paginated`；错误抛 `BusinessError`，不在路由里拼响应。

### 2.5 `core/` 基础设施模块

| 文件 | 导出 | 主要使用方 |
| --- | --- | --- |
| `config.py` | `Settings(BaseSettings)` 与单例 `settings`；字段名 = 环境变量小写；派生属性 `cors_origins`、`use_local_storage`、`zhiqi_mock_mode`、`zhiqi_origin`、`media_public_base`。变量按前缀分组：`DATABASE_URL` / `REDIS_URL`、`ADMIN_JWT_*` / `ADMIN_LOGIN_MAX_FAILURES`、`DEV_MODE` / `LOG_LEVEL` / `APP_TIMEZONE` / `ALLOWED_ORIGINS` / `PUBLIC_BASE_URL`、`STORAGE_*` / `LOCAL_STORAGE_DIR` / `OSS_*` / `MAX_*_SIZE_MB`、`ZHIQI_*`、`GENERATE_RATE_LIMIT` / `MEDIA_RATE_LIMIT` / `AI_*`、`WORKER_*` / `MONITOR_*`、`SEO_*`、`ALERT_*` / `SMTP_*` / `MAIL_FROM`、`SEED_ADMIN_*`、`MYSQL_*` / `GUNICORN_WORKERS` / `NGINX_HTTP_PORT`（仅 docker-compose 使用；`Settings` 同样声明这些字段，使同源的 `server/.env` 不触发 pydantic-settings 的 extra 校验错误）（完整清单与默认值见 [05-deployment](./05-deployment.md)） | 全部模块 |
| `database.py` | `engine`、`SessionLocal`、`Base`、`get_db` | `api/deps.py`、worker、services |
| `redis.py` | `redis_client`（`decode_responses=True`）、`cache_get_json` / `cache_set_json` / `cache_delete` / `cache_delete_prefix` | services、tasks、ratelimit、locks、breaker |
| `locks.py` | `acquire_lock(key, ttl)` / `release_lock` / `extend_lock(key, ttl)`、`with_lock(key, ttl, wait_seconds=0)`（超时抛 `LockTimeout`） | `main.py` 启动、tasks、services |
| `security.py` | `hash_password` / `verify_password` / `create_token` / `decode_token`（HS256，`aud="admin"`，claims `sub` / `ver` / `iat` / `exp`） | `admin_rbac_service`、`api/deps.py` |
| `storage.py` | `StorageBackend` / `LocalStorage` / `S3Storage`（boto3，`endpoint_url=OSS_ENDPOINT`、`region_name=OSS_REGION`、AK/SK 为 `OSS_ACCESS_KEY` / `OSS_SECRET_KEY`）、`get_storage()`（`STORAGE_MODE=local` 或 `OSS_ENDPOINT` 为空 → `LocalStorage`）、`public_url_for(key)`（`PUBLIC_BASE_URL + /media/{key}` 或 `OSS_PUBLIC_BASE_URL + /{key}`）、`probe_image_size(data) -> tuple[int, int] \| None`（无法解析返回 `None`；只解析 PNG IHDR / JPEG SOF / WebP VP8 头，无第三方依赖）、`sniff_media_type(head) -> str \| None`（按魔数识别 PNG/JPEG/WebP/GIF/MP4，返回 MIME，未识别返回 `None`） | `media_service`、`transfer_media`、`uploads.py`、`main.py` 的 `/media` |
| `ratelimit.py` | `check_rate_limit`（Redis 滑动窗口，键 `rate:*`，超限抛 `BusinessError("请求过于频繁", code=CODE_RATE_LIMITED, http_status=429, data={"retry_after": 秒})`）、`enforce_interval`（`domain:last_fetch:{domain}`，同域名最小间隔）；并发限流**不在此处**：worker 的能力 / 检测并发为进程内 `threading.BoundedSemaphore`（多副本时每副本独立） | 生成 / 媒体 / 手动检测接口、`link_check_service` |
| `response.py` | `ok(data)` / `fail(code, message, data)` / `paginated(items, total, page, page_size)` / `Page` | 全部路由 |
| `exceptions.py` | `BusinessError(message, code=400, http_status=400, data=None)`、`CODE_*` 常量（业务码表见 [04-api-spec](./04-api-spec.md)）、`register_exception_handlers(app)` | 全部 |
| `admin_permissions.py` | `PermissionSpec`、`_resource`、`PERMISSIONS`、`PERMISSION_CODES`、`PERMISSION_DEPENDENCIES`、`SYSTEM_GROUPS`、`OPERATOR_EXCLUDED`、`DEFAULT_GROUP_PERMISSIONS`（纯数据，取值见 [07-admin-rbac](./07-admin-rbac.md)） | `admin_rbac_service.ensure_rbac_seed`、`require_permission` |
| `safe_fetch.py` | `normalize_public_url`（回填预校验：scheme 只允许 http/https、禁止 userinfo、端口只允许 80/443/缺省、拒绝 `javascript:` / `file:`；失败由调用方返回 400）/ `assert_public_url`（DNS 解析后校验公网 IP，重定向逐跳校验）/ `fetch_public_bytes` / `fetch_page`（删除检测抓取：≤ 3 跳、响应体上限、只解析 `text/html`、固定 UA、不执行 JS、不带 Cookie / Authorization）/ `robots_allowed`（`robots:{domain}` 缓存）/ `stream_public_bytes(url, dest, *, max_bytes, allowed_types, max_redirects=3, timeout) -> DownloadResult`（媒体转存：`follow_redirects=False`、每跳先 `assert_public_url` 再请求、不带 Authorization 只带 User-Agent；`timeout` 为 httpx 读超时（两次数据块之间的最大等待，不是总时长），`transfer_media` 传 `timeout=ZHIQI_TIMEOUT_DOWNLOAD_SECONDS`（同 [08-zhiqiapi-integration](./08-zhiqiapi-integration.md) §6.6 媒体下载）；返回类型定义在 `core/zhiqi/types.py`，见 §2.6）；对应 navigation `core/seo_fetch.py` 的同名函数并扩展 | `link_check_service`、`link_service`、`transfer_media`、`images.validate_request` / `videos.validate_request` |
| `fingerprint.py` | `normalize_title(title) -> str`（NFKC → 去掉站点后缀（最后一个 `" - "` / `" \| "` / `" _ "` / `"｜"` 之后的部分，仅当剩余 ≥ 4 字符）→ 去标点与空白 → 小写；`None` → `""`；见 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §6.2）/ `simhash64(text) -> int`（返回**有符号** 64 位整数，补码映射，可直接存 `BIGINT`）/ `hamming_distance(a, b) -> int`（先转回无符号再异或计数）/ `extract_main_text(html: str) -> tuple[str \| None, str]`（返回 `(title, text)`：`title` 取 `og:title`，其次 `<title>`；`text` 为正文纯文本，优先 `<article>` / `<main>`，合并空白、截断 40000 字符；供指纹与 `deleted_markers` 匹配，以 11 §6.2 为准） | `link_check_service`、`safe_fetch.fetch_page` |
| `urls.py` | `normalize_url` / `url_hash` / `extract_domain` / `match_url_patterns` | `link_service`、`platform_service` |

### 2.6 `core/zhiqi/` 适配层

契约、函数签名、协议映射与错误分类的权威说明在 [08-zhiqiapi-integration](./08-zhiqiapi-integration.md)；本表只给出文件分工。

| 文件 | 导出 | 对应上游端点 |
| --- | --- | --- |
| `__init__.py` | `get_client()`、`Capability` / `Protocol` / `ErrorCategory` 再导出 | — |
| `types.py` | `Capability`、`TEXT_CAPABILITIES`、`MODALITY_OF`、`Protocol`、`ErrorCategory`、`TaskStatus`、`Timeouts`、`RetryPolicy`、`TextRequest` / `TextResult` / `Citation`、`ImageRequest` / `ImageSubmitResult` / `ImageTaskStatus`、`VideoRequest` / `VideoSubmitResult` / `VideoTaskStatus`、`ModelInfo` / `PricingEntry` / `PricingCatalog`、`UsageLogEntry`、`HealthResult`、`DownloadResult`（下载结果，含 `source`、`request_id`、`http_status`、`request_ids[]`；`client.stream_download`、`videos.download_content` 与 `safe_fetch.stream_public_bytes` 共用的返回值，由调用方 `transfer_media` 写入根任务 `response_meta_json.download`；定义在本文件而非 `client.py`，因为 `client.py` 依赖 `safe_fetch`，放在 `client.py` 会与 `safe_fetch` 循环导入） | — |
| `errors.py` | `ZhiqiError`、`ZhiqiBreakerOpen`、`classify_error`、`is_retryable`、`is_fallbackable`、`classify_task_failure` | — |
| `client.py` | `ZhiqiClient`（只持有 `base_url` / `api_key` / `timeouts` / `user_agent`，重试策略按调用以 `retry: RetryPolicy` 注入）、`ZhiqiResponse`、`get_client`（lru_cache 单例；`ZHIQI_API_KEY` 为空返回 `MockZhiqiClient`）、`reset_client`；只有相对路径（`/v1/*`、`/api/*`）请求带 `Authorization: Bearer`，`absolute=True` 的请求只带 User-Agent；每次响应读取 `x-oneapi-request-id`；`stream_download` 返回 `DownloadResult`（`request_id` 取自响应头；失败抛 `ZhiqiError(TRANSFER_FAILED)` 时同样携带），只接受相对路径（`/v1/videos/{id}/content`，或 origin 等于 `ZHIQI_BASE_URL` origin 的 URL），第三方 CDN 的绝对 URL 一律改走 `safe_fetch.stream_public_bytes` | 任意 `/v1/*`、`/api/*` |
| `text.py` | `TextProvider`（Protocol）、`complete`、`complete_openai_chat` / `complete_openai_responses` / `complete_anthropic_messages`、`build_payload`、`parse_result`、`extract_citations`、`extract_json` | `POST /v1/chat/completions`、`POST /v1/responses`、`POST /v1/messages` |
| `images.py` | `submit_async`、`get_generation`、`generate_sync`、`edit_sync`、`build_async_payload`、`validate_request` | `POST /v1/images/generations/async`、`GET /v1/images/generations/{task_id}`、`POST /v1/images/generations`、`POST /v1/images/edits` |
| `videos.py` | `submit_video`、`get_video`、`download_content`（经 `client.stream_download`，返回 `DownloadResult`）、`build_payload`、`validate_request` | `POST /v1/videos`、`GET /v1/videos/{id}`、`GET /v1/videos/{id}/content` |
| `catalog.py` | `list_models`、`list_pricing`、`derive_modalities`、`protocol_for` | `GET /v1/models`、`GET /api/pricing_new` |
| `usage.py` | `fetch_token_logs`、`estimate_quota`、`estimate_tokens`、`quota_to_cny` | `GET /api/log/token` |
| `health.py` | `probe`、`status_from` | 文本能力：按模型协议发最小文本调用（`POST /v1/chat/completions` / `POST /v1/responses` / `POST /v1/messages`）；图片 / 视频默认不发 HTTP，按调用方查得的 `ai_models.is_available`（`model_available` 参数）判定，`probe_media=True` 时才提交 1 张 `1080p`、`1:1` 图片或最小视频任务（不等待完成） |
| `breaker.py` | `CircuitBreaker`（状态 Hash `ai:breaker:{capability}:{model}`、失败窗口 List `ai:breaker:failures:{capability}:{model}`；`key`（静态方法，返回 `ai:breaker:{capability}:{model}`）/ `state` / `reason` / `allow` / `record_success` / `record_failure` / `force_open` / `reset` / `snapshot`）；不持有配置，由 `ai_gateway_service.get_breaker(config)` 每次按当前 `ai_routing_config.breaker` 构造 | — |
| `mock.py` | `MockZhiqiClient`（`is_mock=True`；`request` 按 path 分发到下列函数，`request_id = "mock-" + uuid4 hex`，每次调用向 `mock:usage_logs` 写一条伪用量日志；`stream_download` 对 `/media/mock/` 路径直接从 `mock_assets/` 复制文件，不发 HTTP，返回 `request_id` 以 `mock-` 开头的 `DownloadResult`）、`mock_chat` / `mock_responses` / `mock_messages`（均接收 `(payload, metadata)`：`request` 把请求体 `kw["json"]` 与 `kw.get("metadata") or {}`（即 `TextRequest.metadata`，本地透传、不发送）一并传入；`seo_query` / `geo_query` 命中分支的目标 URL 只取 `metadata.target_url`、不从提示词提取，缺失时一律按未收录分支返回，见 [08-zhiqiapi-integration](./08-zhiqiapi-integration.md) §13.2）/ `mock_image_async` / `mock_image_status` / `mock_video_submit` / `mock_video_status`（任务状态机存 `mock:task:{task_id}`）/ `mock_models` / `mock_pricing` / `mock_token_logs` | 无网络；`ZHIQI_API_KEY` 为空时 `get_client()` 返回它 |
| `mock_assets/` | `placeholder.png`、`placeholder.mp4`（启动时复制到 `LOCAL_STORAGE_DIR/mock/`） | — |

### 2.7 `services/` 业务层

| 文件 | 职责 | 主要调用方 |
| --- | --- | --- |
| `i18n.py` | `normalize_locale`（`zh-CN` / `en-US`，非法值回退 `zh-CN`） | `api/deps.get_locale` |
| `admin_rbac_service.py` | `ensure_rbac_seed`、`has_permission`、管理员 / 用户组 CRUD（自动补齐同资源 `view` 与 `PERMISSION_DEPENDENCIES`）、`write_audit`、`list_operation_logs` | auth / admins / admin_groups / admin_permissions / operation_logs 路由、审计中间件 |
| `data_scope_service.py` | `DataScope` / `SYSTEM_SCOPE`、`visible_project_ids` 与各表范围谓词（`scope_by_project` / `scope_media` / `scope_templates` / `scope_routes` / `scope_link_children` / `scope_usage_logs` / `scope_operation_logs`）、`get_visible`（不可见即 404）、`require_project`、`is_visible`、`owner_options`（[13-user-data-scope](./13-user-data-scope.md) §9.2） | `api/deps.get_data_scope`、所有读写受数据范围约束表的 service |
| `settings_service.py` | `get_value` / `set_value` / `get_config(key)`（Redis 缓存 + 默认值深合并）、`ensure_default_settings`（`DEFAULT_SETTINGS[key]` 深合并 `ENV_SEED_PATHS` 派生值，键不存在才插入） | settings 路由、全部读取配置的 service / task |
| `project_service.py` | 项目 CRUD、负责人规则（创建缺省本人、总后台转移；创建 / 删除 / 转移提交后清 `cache:stats:*`）、归档 / 恢复、删除校验、`save_project_routes` | projects 路由 |
| `prompt_template_service.py` | 模板 CRUD、版本、发布、`render(template, variables)`、`resolve_template(kind, project_id, language)` | prompt_templates 路由、`ai_task_service` |
| `keyword_service.py` / `title_service.py` | 归一化、去重、导入、打分、`adopt` / `discard` / `restore` 状态流转；`title_service.title_dedup_key(title)` 为标题去重键（NFKC → 去标点与空白 → 小写，**不**剥离站点后缀，见 [09-generation-pipeline](./09-generation-pipeline.md) §7.3），生成写入去重与 `duplicate_title` 质量标记共用；与 `fingerprint.normalize_title`（先剥离站点后缀，用于链接检测比对页面标题）不同，二者不互相复用 | keywords / titles 路由、`ai_task_service` |
| `content_service.py` | 内容状态机 `transition(content, action, actor)`（非法流转抛 409；进入 `generating` / `archived` 写 `prev_status`，离开时清空）、版本写入（版本化字段变更同事务写 `content_versions`，`content_hash` 与当前版本相同不建版本，达 `rewrite.max_versions` 时先自动裁剪）、质量规则（`quality_score` / `risk_flags_json`）、素材绑定、导出 | contents 路由、`ai_task_service` |
| `generation_service.py` | 创建 `generation_batches` 与根任务并 `RPUSH queue:ai_tasks`、`on_task_finished(batch_id)` 收敛、取消 / 重试 | keywords / titles / contents / generation_batches 路由、`run_ai_tasks` |
| `ai_gateway_service.py` | `ResolvedRoute`（dataclass）、`resolve_route`、`check_paused`、`check_quota` / `settle_quota`、`complete_text` / `submit_image` / `submit_video` / `poll_task`、`record_failure`、`finalize_root`、`estimate_for`、`get_breaker` / `get_retry_policy`、`ensure_default_routes` | `ai_task_service`、`index_providers`、ai_routes 路由、生成类路由（`check_paused` / `check_quota` / `model?` 覆盖校验）、worker 任务 |
| `ai_task_service.py` | `claim(db, task_id)`、`dispatch`：按 `operation` + `input_json` 执行根任务并写回关键词 / 标题 / 内容 / 素材 | `run_ai_tasks.process_one` |
| `ai_catalog_service.py` | `sync_models` 落库 `ai_models`、维护 `cache:ai:models:*`；`catalog_entry(db, model_id) -> ModelInfo \| None`（读 `cache:ai:models:catalog`，键缺失时从 `ai_models` 重建并回填，供 `catalog.protocol_for` 选协议；`core/zhiqi` 不读库，回填只在本服务内完成） | `sync_models` 任务、ai_models 路由、`ai_gateway_service`（逐候选选协议） |
| `ai_usage_service.py` | `reconcile`：拉取用量日志、按 `entry_hash` 幂等 upsert `ai_usage_logs`、按 `request_id` → `upstream_task_id` 的顺序匹配尝试行、回填 `quota_actual` / `reconciled_at` / `usage_log_type` / `cost_cny` 并重算根任务合计列、写 `ai:usage:last_pull` | `reconcile_usage` 任务、ai_usage 路由 |
| `media_service.py` | 素材 CRUD、`ImageGenerateBody` / `VideoGenerateBody` → `ImageRequest` / `VideoRequest` 映射、提交 / 轮询 / 转存状态机、删除文件 | media / uploads 路由、`poll_media_tasks`、`transfer_media`、`cleanup_media` |
| `platform_service.py` | 平台与特征规则 CRUD、`detect`（URL → 平台）、规则测试、`cache:platforms:all` | platforms 路由、`link_service` |
| `link_service.py` | `backfill`（`safe_fetch.normalize_public_url` 预校验 → `urls.normalize_url` / `url_hash` / `extract_domain` → 平台缺省识别 → 同事务插入 `publish_links`、更新 `contents.link_count`、`approved → published`、重算 `contents.first_published_at`（`url_hash` 冲突返回 409：已有链接对调用者可见时带 `existing_id`，不可见时 `data={"existing_id":null,"reason":"owned_by_other"}`，见 [13-user-data-scope](./13-user-data-scope.md) §8）→ 入队基线检测；DNS / 内网 IP 校验留在基线检测）、`enqueue_check(link, check_type, triggered_by) -> bool`（`SET NX queued:link_check:{link_id}` → `RPUSH` / manual `LPUSH` → 非 manual 推后 `next_check_at` 1 小时）、`compute_next_check_at(link, result)` / `compute_next_index_check_at(link)`（调度时间计算）、`rebaseline`、暂停 / 恢复、`mark_index`（人工标记收录） | links / monitoring 路由、`schedule_link_checks`、`schedule_index_checks` |
| `link_check_service.py` | `check_link(db, link, check_type, triggered_by)`：`safe_fetch.fetch_page` 抓取 → `judge(fetch_result, link, platform, *, check_type) -> tuple[str, str, dict]`（返回 `(result_status, matched_rule, evidence)`，签名以 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §6.3 为准）规则判定 → 确认阈值得 `applied_status` → 同一事务 `link_checks` 插入 + `publish_links` 状态流转 → `alert_service.raise_alert` / `resolve_alert` → `stats:rt` 计数；平台规则测试接口复用 `judge`（不写库） | `run_link_checks`、platforms 路由（`POST /{id}/test`） |
| `index_check_service.py` | `run(db, link, kinds, engines, check_type, triggered_by)`、`enabled_engines(kind)`（`seo` 读 `seo_providers.engines.*.enabled`，`geo` 读 `geo_engines.engines[].enabled`，无 Mock 特例）：逐引擎调用提供器 / GEO 引擎（经 `ai_gateway_service.complete_text(model_override=引擎 model, protocol_override=引擎 protocol)` 同步记根任务 + 尝试行），每引擎独立事务 `SELECT … FOR UPDATE` 重读 JSON 列 → 写 `index_checks` → 回写 `seo_status_json` / `geo_status_json` / `*_any` / `first_*_at` → 全部引擎完成后重算 `next_index_check_at` | `run_index_checks` |
| `index_providers/*` | `SeoProvider` / `GeoEngine` 协议与五个提供器、通用 GEO 引擎；`GeoEngine` 协议含 `precheck`（调用前预检，见 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §8.2：`parse.match_mode=title` 且命中 `title_in_prompt` 时返回 `status=unknown` 的 `CheckResult`，`index_check_service` 据此不创建根任务、不调用上游；否则返回 `None`）与 `check`；`get_seo_provider(code)` / `get_geo_engine(code)` 工厂 | `index_check_service` |
| `alert_service.py` | `raise_alert(db, alert_type, *, target_type, target_id=None, target_key="", project_id=None, title, message, payload)`（`dedupe_key = {alert_type}:{target_type}:{target_key}` 幂等：已有 `open` / `acknowledged` 行则 `trigger_count += 1`；`alert:cooldown:{dedupe_key}` 内不重复投递通道）、`resolve_alert(db, alert_type, target_type, target_key, note="auto")`、`acknowledge` / `resolve` / `ignore` / `batch_resolve`（人工处理）、规则评估（供 `evaluate_alerts`）、通道投递（`in_app` 固定；webhook / 邮件预留，`enabled=false`） | 各 service、`evaluate_alerts`、alerts 路由 |
| `stats_service.py` | `overview` / `trends` / `breakdown` / `rankings` / `export`、`aggregate_daily`、`stats:rt` 实时计数 | stats 路由、`aggregate_daily_stats`、各 service 计数点 |

### 2.8 `tasks/` 周期任务

每个文件暴露一个可独立调用的入口函数，由对应 worker 主循环按周期提交到进程内线程池；调度频率、锁键、幂等与回收规则见 [01-architecture](./01-architecture.md)，业务语义分别见 08 / 10 / 11 / 12。

| 文件 | 进程 | 入口函数 | 消费的队列 / 触发 |
| --- | --- | --- | --- |
| `run_ai_tasks.py` | worker | `drain(pool, sems, limit=10) -> int`、`process_one(task_id) -> bool` | `queue:ai_tasks` |
| `poll_media_tasks.py` | worker | `poll_due(pool, limit=20) -> int` | `ai_tasks.status='polling'` 且 `next_poll_at` 到期 |
| `transfer_media.py` | worker | `transfer_asset(asset_id) -> bool`、`retry_due(pool, limit=10) -> int` | 轮询成功后即刻；`downloading` 重试每 60s |
| `reconcile_usage.py` | worker | `reconcile() -> dict` | `ai_routing_config.usage.reconcile_interval_seconds` |
| `sync_models.py` | worker | `sync_models() -> dict` | `ai_routing_config.catalog.sync_interval_seconds`，启动时一次 |
| `health_probe.py` | worker | `probe_routes(pool) -> list[HealthResult]` | `ai_routing_config.health.probe_interval_seconds` |
| `recover_stale_tasks.py` | worker | `recover() -> dict` | 每 60s，启动时一次 |
| `cleanup_media.py` | worker | `cleanup() -> dict` | 每日 03:00（`stats_config.timezone`） |
| `schedule_link_checks.py` | monitor | `enqueue_due(limit=200) -> int` | `monitoring_config.link_check.scan_interval_seconds` → 生产 `queue:link_checks` |
| `run_link_checks.py` | monitor | `drain(pool, limit=20) -> int`、`process_one(payload) -> bool` | `queue:link_checks` |
| `schedule_index_checks.py` | monitor | `enqueue_due(limit=100) -> int` | `monitoring_config.index_check.scan_interval_seconds` → 生产 `queue:index_checks` |
| `run_index_checks.py` | monitor | `drain(pool, limit=10) -> int`、`process_one(payload) -> bool` | `queue:index_checks` |
| `aggregate_daily_stats.py` | monitor | `aggregate(stat_date) -> int`、`aggregate_today() -> int`、`catch_up(days=3) -> int`、`drain_recompute() -> int` | 每日 `stats_config.daily_at`、当日每 `intraday_refresh_seconds`、启动补算、`queue:stats_recompute` |
| `evaluate_alerts.py` | monitor | `evaluate() -> dict` | 每 300s |

### 2.9 `schemas/` 约定

- 一资源一文件，文件名为路由文件名的单数形式（`keywords.py` ↔ `schemas/keyword.py`），不可数资源名保持原样（`settings.py` / `media.py` / `monitoring.py` / `stats.py`）；登录相关为 `auth.py`，管理员 / 用户组 / 权限三个 RBAC 路由合并为 `admin_rbac.py`，AI 四个路由合并为 `ai.py`。
- 命名：请求体 `XxxBody` / `XxxCreate` / `XxxUpdate`，响应 `XxxOut`，列表项直接复用 `XxxOut`；公共分页参数 `common.PageParams`，批量 ID `common.IdList`，状态变更 `common.StatusBody`，CSV 导出 `common.ExportParams`（`format=csv` + 该列表的筛选参数）。
- `settings.py` 为每个配置键定义同名校验模型：`generation_config` → `GenerationConfig`、`media_config` → `MediaConfig`、`monitoring_config` → `MonitoringConfig`、`geo_engines` → `GeoEngines`、`seo_providers` → `SeoProviders`、`alert_config` → `AlertConfig`、`ai_routing_config` → `AiRoutingConfig`、`stats_config` → `StatsConfig`、`system_info` → `SystemInfo`；`PUT /admin/settings/{key}` 与批量 `PUT /admin/settings` 按 key 选择模型校验（各键结构的权威文档见 [03-data-model](./03-data-model.md) 的 settings 键清单）。
- JSON 列在 schema 中以去掉 `_json` 后缀的字段名出现（`default_templates`、`fallback_models`、`params`、`evidence`…），由 service 层编解码。

### 2.10 迁移、seed、脚本与测试

| 位置 | 内容 | 说明 |
| --- | --- | --- |
| `migrations/versions/0001_initial.py` | 全部 24 张表、索引与唯一约束 | 表定义见 [03-data-model](./03-data-model.md) |
| `migrations/versions/0002_seed_permissions.py` | 权限码（`PERMISSION_CODES`）与系统用户组（`super_admin` / `operator` / `reviewer` / `read_only`，含默认 `data_scope`） | 只写权限与组；新增权限码后由启动时 `ensure_rbac_seed` 补齐 |
| 启动钩子（`main.py`、两个 worker） | `lock:bootstrap` 内 `ensure_rbac_seed` → `ensure_default_settings` → `ensure_default_routes` | 全部幂等（`INSERT … ON DUPLICATE KEY UPDATE id=id` 或逐条 `IntegrityError` 回滚） |
| `seeds/seed.py` | 超管 `admin/admin123`（`SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`）、示例项目（负责人为该超管）、系统 Prompt 模板（`sys_*`，`zh-CN`）、默认发布平台 | 幂等 upsert；本机在 `server/` 目录执行 `python seeds/seed.py`（依赖 `pip install -e .` 使 `app` 可导入，见 §1.2）；docker 内同样是 `python seeds/seed.py`，发布流程为 `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"`（见 §5） |
| `scripts/integration_smoke.py` | Mock 模式端到端冒烟：登录 → 关键词 → 标题 → 内容 → 图片 → 回填 → 检测 → 报表。参数 `--base-url`（API 根地址，不带 `/api/v1`，缺省 `http://127.0.0.1:8100`）、`--username` / `--password`（缺省取 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`；预发改密后用参数传入新密码）；本机经根脚本 `smoke:api` 运行，compose 环境在容器内运行 `docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000` | 检测步骤断言 SEO / GEO 结果非 `unknown`，并另回填一条返回 404 的公网 URL，断言基线 `suspected_deleted`、手动检测后 `deleted`、产生 `link_deleted` 告警且可确认、解决；报表步骤断言 `seo_index_rate` / `geo_cite_rate` 非 null 且 > 0；恢复分支（`link_restored`）不在脚本内，由后端集成测试覆盖（[11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §16.2 删除 / 恢复闭环）；用法见 [06-getting-started](./06-getting-started.md) |
| `tests/conftest.py` | 测试会话（SQLite 或 `DATABASE_URL` 指向的 MySQL）、`reset_client()` 后注入 `MockZhiqiClient`、管理员 token 夹具 | 测试文件按领域命名 `test_<domain>.py` |

### 2.11 后端依赖（`pyproject.toml`）

`fastapi>=0.110`、`uvicorn[standard]>=0.29`、`gunicorn>=21.2`、`sqlalchemy>=2.0`、`alembic>=1.13`、`pymysql>=1.1`、`redis>=5.0`、`pydantic>=2.6`、`pydantic-settings>=2.2`、`python-multipart>=0.0.9`、`bcrypt>=4.0`、`pyjwt>=2.8`、`httpx>=0.27`、`tzdata>=2024.1`、`boto3>=1.34`（`S3Storage`）、`google-auth>=2.29`（仅 `google_search_console` 提供器）；dev：`pytest>=8.0`。**不引入** Celery / RQ / Pillow / ffmpeg：队列用 Redis List + MySQL 任务表，图片宽高由 `storage.probe_image_size` 解析文件头得到，视频 `width` / `height` / `duration_seconds` 首版留空。

## 3. 管理后台 `apps/admin/`

### 3.1 目录树

```text
apps/admin/
├── index.html
├── vite.config.ts                 # base /admin/，端口 5174，代理 /api 与 /media → http://127.0.0.1:8100
├── package.json                   # 包名 admin；依赖 @aicreat/shared（workspace:*）、vue、vue-router、pinia、vue-i18n、element-plus、axios、echarts、markdown-it
├── tsconfig.json
├── public/                        # favicon、占位图
└── src/
    ├── main.ts                    # 创建 app：Pinia、Router、i18n、Element Plus、v-permission 指令
    ├── App.vue
    ├── env.d.ts
    ├── router/
    │   └── index.ts               # 路由表 + 登录守卫 + meta.permission 权限守卫 + 403
    ├── store/
    │   ├── auth.ts                # token、admin（含 data_scope）、permissions、isAllScope（刷新时调 /auth/me）
    │   ├── theme.ts               # 明暗主题（localStorage）
    │   ├── project.ts             # 当前项目（全局项目选择器）与总后台用户视角 ownerId，均持久化
    │   └── alerts.ts              # 未处理告警数（仅有 monitoring.alerts.view 时启动 60s 轮询）
    ├── i18n/
    │   ├── index.ts               # vue-i18n 实例、语言检测与切换（持久化）
    │   └── locales/
    │       ├── zh-CN.ts
    │       └── en-US.ts
    ├── api/
    │   ├── client.ts              # axios 实例：Bearer、lang、ownerId>0 时附加 owner_id（13 §12.2）、401 清登录态并跳转登录、403 按 data.permission 区分提示（07 §9.6）
    │   ├── auth.ts                # login / me / logout / changePassword / siteInfo（公开）
    │   ├── admins.ts              # 管理员 CRUD、状态、重置密码；操作日志查询
    │   ├── groups.ts              # 用户组 CRUD、权限保存；权限树
    │   ├── settings.ts            # list / get / save / runtime
    │   ├── projects.ts            # CRUD / ownerOptions（GET /owner-options）/ overview / saveRoutes（PUT /projects/{id}/routes）
    │   ├── promptTemplates.ts
    │   ├── keywords.ts
    │   ├── titles.ts
    │   ├── contents.ts            # CRUD / 生成 / getTask（GET /contents/{id}/task 轮询）/ 版本（含 deleteVersion）
    │   ├── batches.ts
    │   ├── media.ts
    │   ├── uploads.ts
    │   ├── ai.ts                  # models（含 listModelOptions）/ routes / tasks（含 exportTasks）/ usage / health
    │   ├── platforms.ts
    │   ├── links.ts               # 回填 / 检测 / 标记 / 历史 / exportLinks
    │   ├── monitoring.ts
    │   ├── alerts.ts
    │   └── stats.ts               # overview（含 series）/ trends / breakdown / rankings / export / recompute（超时 300s）
    ├── layouts/
    │   └── Layout.vue             # 侧边菜单（显式菜单配置，每项绑定一个 *.view 权限码并按其过滤）+ 顶栏（OwnerSelect（仅总后台）、项目选择器、AlertBadge（仅有 monitoring.alerts.view 时渲染）、语言、主题、范围标签）
    ├── directives/
    │   └── permission.ts          # v-permission
    ├── composables/
    │   ├── usePermission.ts       # has(code) / hasAny(codes) / isAllScope
    │   ├── usePolling.ts          # 任务 / 批次状态轮询（页面可见时 3s）
    │   └── useProject.ts          # 当前项目 id 与切换
    ├── utils/
    │   ├── format.ts              # 时间（UTC → 本地）、额度、金额、时长
    │   ├── markdown.ts            # Markdown 渲染（markdown-it）与 HTML 清理
    │   └── download.ts            # CSV / MD / HTML 下载
    ├── views/
    │   ├── Login.vue
    │   ├── Forbidden.vue
    │   ├── Dashboard.vue          # 总览 KPI + 迷你趋势（只调用 GET /stats/overview，迷你趋势取其 series，权限 dashboard.view）；告警摘要块读 store/alerts，仅 usePermission().has('monitoring.alerts.view') 时渲染
    │   ├── projects/
    │   │   ├── Index.vue          # 项目列表与编辑弹窗
    │   │   └── Detail.vue         # 项目概览、默认模板 / 模型（capability_routes 项目覆盖）
    │   ├── prompt-templates/
    │   │   ├── Index.vue          # 模板列表（按 kind / 状态）
    │   │   └── Editor.vue         # system / user prompt、变量、输出格式、预览渲染、版本
    │   ├── keywords/
    │   │   └── Index.vue          # 关键词表格、生成抽屉、导入、批量采用 / 弃用、导出
    │   ├── titles/
    │   │   └── Index.vue          # 按关键词生成标题、编辑、打分、采用
    │   ├── contents/
    │   │   ├── Index.vue          # 内容列表（状态 Tab：草稿 / 生成中 / 待审 / 已审 / 已发布）
    │   │   └── Editor.vue         # 左编辑右预览、大纲面板、重写工具条、SEO 要素、版本抽屉、素材面板、链接面板
    │   ├── generation-batches/
    │   │   └── Index.vue          # 生成批次与子任务进度
    │   ├── media/
    │   │   ├── Assets.vue         # 素材库（图片 / 视频，筛选、预览、删除、重试转存）
    │   │   ├── ImageGenerate.vue  # 文生图 / 图生图表单 + 任务进度
    │   │   └── VideoGenerate.vue  # 文生视频 / 图生视频表单 + 长任务进度
    │   ├── ai/
    │   │   ├── Models.vue         # 模型目录、价格快照、同步按钮
    │   │   ├── Routes.vue         # 菜单「AI 网关 → 能力路由」：能力 → 主模型 / 备选链（候选链）、健康探测（一键测试）、熔断状态与重置
    │   │   ├── Tasks.vue          # AI 任务列表（根任务 / 尝试行切换 row_kind；列：request_id、错误分类、tokens、额度、成本、耗时）、根任务详情展开 attempts[]、重试 / 取消、导出 CSV
    │   │   └── Usage.vue          # 用量对账日志与汇总
    │   ├── platforms/
    │   │   └── Index.vue          # 平台与删除特征规则维护、规则测试
    │   ├── links/
    │   │   ├── Index.vue          # 回填链接列表、回填弹窗、批量回填、手动检测
    │   │   └── Detail.vue         # 链接详情：时间线（删除检测记录、SEO/GEO 收录检测记录、该链接的告警）与证据
    │   ├── monitoring/
    │   │   ├── LinkChecks.vue     # 删除检测记录
    │   │   └── IndexChecks.vue    # 收录检测记录
    │   ├── alerts/
    │   │   └── Index.vue          # 告警中心
    │   ├── stats/
    │   │   └── Reports.vue        # 趋势、分解、榜单、导出 CSV
    │   ├── settings/
    │   │   └── Index.vue          # 菜单「系统配置」，Tab：生成 / 媒体 / 监控 / GEO 引擎 / SEO 提供器 / 告警 / AI 路由（ai_routing_config 全局路由参数；候选链在「AI 网关 → 能力路由」）/ 统计 / 系统
    │   ├── admins/
    │   │   └── Index.vue
    │   ├── admin-groups/
    │   │   └── Index.vue
    │   └── admin-operation-logs/
    │       └── Index.vue
    └── components/
        ├── LangSwitch.vue
        ├── ThemeSwitch.vue
        ├── ToolbarSelect.vue
        ├── ProjectSelect.vue
        ├── OwnerSelect.vue        # 顶栏用户视角切换器（仅数据范围 all 的总后台显示）
        ├── StatusTag.vue          # 统一状态 → 颜色 / 文案映射（读 shared 枚举）
        ├── KpiCard.vue
        ├── TrendChart.vue         # echarts 折线 / 柱状封装
        ├── MarkdownEditor.vue
        ├── MarkdownPreview.vue
        ├── VersionDiff.vue
        ├── PromptVariablesForm.vue
        ├── ModelSelect.vue        # 从 GET /ai/models/options?modality= 选择（不分页，按模态过滤）
        ├── ImageUpload.vue
        ├── AssetCard.vue
        ├── AssetPicker.vue
        ├── TaskProgress.vue       # ai_tasks / media 进度条与错误分类
        ├── LinkBackfillDialog.vue
        ├── EvidenceDrawer.vue     # 检测证据 JSON 展示
        ├── AlertBadge.vue
        └── JsonEditor.vue
```

### 3.2 管理后台模块职责

| 模块 | 职责 |
| --- | --- |
| `router` | 路由表（§3.3）、登录守卫（无 token → `/login?redirect=`）、权限守卫（`meta.permission` 不满足 → `/403`）、`createWebHistory("/admin/")` |
| `store/auth` | `token`（localStorage）、`admin`（含 `data_scope`）、`permissions`；刷新页面时调 `GET /admin/auth/me` 重建；401 时清空 |
| `store/theme` | 明暗主题，持久化到 localStorage |
| `store/project` | 当前项目 id（顶栏 `ProjectSelect`）与总后台的用户视角 `ownerId`（顶栏 `OwnerSelect`），均持久化；各业务页列表默认带 `project_id` 筛选，`ownerId > 0` 时由 `client.ts` 为 GET 请求及两个检测 `run` 接口附加 `owner_id` |
| `store/alerts` | `open_count` 未处理告警数；仅 `usePermission().has('monitoring.alerts.view')` 为真时每 60s 调 `GET /admin/alerts/summary` |
| `i18n` | vue-i18n 实例与 `zh-CN` / `en-US` 界面词条；请求带 `lang` 参数 |
| `api` | 统一封装后端调用（§3.4）；`client.ts` 注入 `Authorization: Bearer`、`lang`，拆解 `{code, message, data}`，`code != 0` 时抛错并提示 |
| `layouts/Layout.vue` | 侧边菜单显式配置（每项绑定一个 `*.view` 权限码并按其过滤）、面包屑、顶栏（用户视角切换器 `OwnerSelect`（仅总后台）、项目选择器、`AlertBadge`、`LangSwitch`、`ThemeSwitch`、当前用户菜单与范围标签「总后台」/「我的数据」）、用户视角提示条 |
| `directives/permission.ts` | `v-permission="'content.keywords.generate'"`：无权限时移除元素 |
| `composables` | `usePermission`（`has` / `hasAny` / `isAllScope`）、`usePolling`（任务 / 批次轮询，页面不可见时暂停）、`useProject` |
| `utils` | 时间 / 额度 / 金额 / 时长格式化，Markdown 渲染与清理，CSV / MD / HTML 文件下载 |
| `views` | 页面级组件，按资源分目录 `kebab-case/Index.vue`；同资源的详情 / 编辑页为 `Detail.vue` / `Editor.vue` |
| `components` | 跨页面复用的 UI 组件（§3.6） |

### 3.3 路由与页面清单

路由路径按页面目录名派生（`views/<dir>/Index.vue` → `/<dir>`，`Detail.vue` / `Editor.vue` → `/<dir>/:id`，新建用 `/<dir>/new`；具名文件 → `/<dir>/<文件名 kebab-case>`，如 `media/Assets.vue` → `/media/assets`、`monitoring/LinkChecks.vue` → `/monitoring/link-checks`），嵌套在 `/`（`Layout.vue`，`meta.requiresAuth`）之下；每条路由的 `meta.permission` 与 `Layout.vue` 菜单项使用同一个 `*.view` 权限码（权限码全表见 [07-admin-rbac](./07-admin-rbac.md)）。

| 分组 | 路由 | 页面 | `meta.permission` | 主要接口 |
| --- | --- | --- | --- | --- |
| — | `/login` | `Login.vue` | 公开 | `POST /admin/auth/login`、`GET /admin/auth/site-info` |
| — | `/403` | `Forbidden.vue` | 已登录 | — |
| 控制台 | `/dashboard`（`/` 重定向至此） | `Dashboard.vue` | `dashboard.view` | `GET /admin/stats/overview` |
| 内容生产 | `/projects`、`/projects/:id` | `projects/Index.vue`、`projects/Detail.vue` | `content.projects.view` | `/admin/projects`、`PUT /admin/projects/{id}/routes` |
| 内容生产 | `/prompt-templates`、`/prompt-templates/new`、`/prompt-templates/:id` | `prompt-templates/Index.vue`、`prompt-templates/Editor.vue` | `content.prompt_templates.view` | `/admin/prompt-templates` |
| 内容生产 | `/keywords` | `keywords/Index.vue` | `content.keywords.view` | `/admin/keywords`、`GET /admin/generation-batches/{id}` |
| 内容生产 | `/titles` | `titles/Index.vue` | `content.titles.view` | `/admin/titles`、`GET /admin/generation-batches/{id}` |
| 内容生产 | `/contents`、`/contents/new`、`/contents/:id` | `contents/Index.vue`、`contents/Editor.vue` | `content.contents.view` | `/admin/contents`、`GET /admin/contents/{id}/task` |
| 内容生产 | `/generation-batches` | `generation-batches/Index.vue` | `content.batches.view` | `/admin/generation-batches` |
| 媒体 | `/media/assets` | `media/Assets.vue` | `media.assets.view` | `/admin/media/assets`、`POST /admin/media/assets/{id}/retry` / `transfer` |
| 媒体 | `/media/images` | `media/ImageGenerate.vue` | `media.images.view` | `POST /admin/media/images/generate`、`POST /admin/uploads/image`、`GET /admin/media/assets/{id}/task` |
| 媒体 | `/media/videos` | `media/VideoGenerate.vue` | `media.videos.view` | `POST /admin/media/videos/generate`、`POST /admin/uploads/image` / `video`、`GET /admin/media/assets/{id}/task` |
| AI 网关 | `/ai/models` | `ai/Models.vue` | `ai.models.view` | `/admin/ai/models` |
| AI 网关 | `/ai/routes` | `ai/Routes.vue` | `ai.routes.view` | `/admin/ai/routes`、`/admin/ai/health` |
| AI 网关 | `/ai/tasks` | `ai/Tasks.vue` | `ai.tasks.view` | `/admin/ai/tasks` |
| AI 网关 | `/ai/usage` | `ai/Usage.vue` | `ai.usage.view` | `/admin/ai/usage` |
| 发布与监控 | `/platforms` | `platforms/Index.vue` | `publish.platforms.view` | `/admin/platforms` |
| 发布与监控 | `/links`、`/links/:id` | `links/Index.vue`、`links/Detail.vue` | `publish.links.view` | `/admin/links`、`GET /admin/links/{id}/checks`、`GET /admin/links/{id}/index-checks`、`GET /admin/alerts?target_type=publish_link&target_id={id}`（详情时间线的告警数据源，仅有 `monitoring.alerts.view` 时请求） |
| 发布与监控 | `/monitoring/link-checks` | `monitoring/LinkChecks.vue` | `monitoring.link_checks.view` | `/admin/monitoring` |
| 发布与监控 | `/monitoring/index-checks` | `monitoring/IndexChecks.vue` | `monitoring.index_checks.view` | `/admin/monitoring` |
| 发布与监控 | `/alerts` | `alerts/Index.vue` | `monitoring.alerts.view` | `/admin/alerts` |
| 报表 | `/stats/reports` | `stats/Reports.vue` | `stats.reports.view` | `/admin/stats/trends` / `breakdown` / `rankings` / `export` |
| 系统 | `/settings` | `settings/Index.vue` | `system.settings.view` | `/admin/settings` |
| 系统 | `/admins` | `admins/Index.vue` | `security.admins.view` | `/admin/admins` |
| 系统 | `/admin-groups` | `admin-groups/Index.vue` | `security.groups.view` | `/admin/admin-groups`、`GET /admin/admin-permissions/tree` |
| 系统 | `/admin-operation-logs` | `admin-operation-logs/Index.vue` | `security.audit.view` | `/admin/admin-operation-logs` |

`router/index.ts` 骨架（与 navigation 同形）：

```ts
const routes: RouteRecordRaw[] = [
  { path: "/login", name: "login", component: () => import("@/views/Login.vue") },
  {
    path: "/",
    component: () => import("@/layouts/Layout.vue"),
    meta: { requiresAuth: true },
    children: [
      { path: "", redirect: "/dashboard" },
      { path: "dashboard", component: () => import("@/views/Dashboard.vue"), meta: { permission: "dashboard.view" } },
      { path: "projects", component: () => import("@/views/projects/Index.vue"), meta: { permission: "content.projects.view" } },
      { path: "projects/:id", component: () => import("@/views/projects/Detail.vue"), meta: { permission: "content.projects.view" } },
      // … 其余条目按 §3.3 表逐行声明
      { path: "403", component: () => import("@/views/Forbidden.vue") },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/dashboard" },
];

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (to.meta.requiresAuth && !auth.token) return { path: "/login", query: { redirect: to.fullPath } };
  if (auth.token && !auth.admin) await auth.fetchMe();            // 刷新后重建 admin 与 permissions
  const code = to.meta.permission as string | undefined;
  if (code && !usePermission().has(code)) return { path: "/403", query: { from: to.fullPath } };
  return true;
});
```

### 3.4 `api/` 封装与后端前缀对照

| 文件 | 后端前缀 | 函数（节选） |
| --- | --- | --- |
| `client.ts` | `/api/v1` | `request<T>()`：注入 `Authorization: Bearer <token>` 与 `lang`，`store/project.ownerId > 0` 时为 GET 请求及 `POST /admin/monitoring/link-checks/run`、`POST /admin/monitoring/index-checks/run` 附加 `owner_id`（请求已显式携带时不覆盖，其它写请求不附加，[13-user-data-scope](./13-user-data-scope.md) §12.2），解包 `{code, message, data}`；`code != 0` 抛 `ApiError(code, message, data)`；401 → 清登录态并跳转 `/login`（登录请求本身的 401 交由登录页展示）；403 → `data.permission` 存在时提示「无权执行此操作」并刷新权限，`data` 为 null（安全规则拒绝）时直接展示后端 `message`、不刷新权限（[07-admin-rbac](./07-admin-rbac.md) §9.6）；文件下载走 `responseType: "blob"`；`recompute` 等长请求可单独传 `timeout` |
| `auth.ts` | `/admin/auth` | `login`、`me`、`logout`、`changePassword`、`siteInfo` |
| `admins.ts` | `/admin/admins`、`/admin/admin-operation-logs` | `listAdmins`、`getAdmin`、`createAdmin`、`updateAdmin`、`setAdminStatus`、`resetPassword`、`listOperationLogs` |
| `groups.ts` | `/admin/admin-groups`、`/admin/admin-permissions` | `listGroups`、`getGroup`、`createGroup`、`updateGroup`、`deleteGroup`、`saveGroupPermissions`、`listPermissions`、`permissionTree` |
| `settings.ts` | `/admin/settings` | `listSettings`、`getSetting`（`GET /{key}?locale=`）、`saveSetting`（`PUT /{key}`）、`saveSettings`（批量 `PUT /admin/settings`）、`runtime`（`GET /runtime`，已登录即可） |
| `projects.ts` | `/admin/projects` | CRUD、`ownerOptions`（`GET /owner-options`）、`archive` / `unarchive`、`overview`、`saveRoutes` |
| `promptTemplates.ts` | `/admin/prompt-templates` | CRUD、`versions`、`publish`、`archive`、`duplicate`、`preview` |
| `keywords.ts` | `/admin/keywords` | CRUD、`generate`、`importKeywords`、`importFile`、`adopt` / `discard` / `restore`、`batchStatus`、`exportKeywords` |
| `titles.ts` | `/admin/titles` | CRUD、`generate`、`score`、`adopt` / `discard` / `restore`、`batchStatus` |
| `contents.ts` | `/admin/contents` | CRUD、`generate`、`generateOutline`、`generateBody`、`rewrite`、`generateSeo`、`getTask`、`submitReview` / `approve` / `reject`、`archive` / `unarchive`、`versions`、`getVersion`、`restoreVersion`、`deleteVersion`、`listAssets`、`attach` / `detach`、`listLinks`、`exportContent` |
| `batches.ts` | `/admin/generation-batches` | `list`、`get`、`cancel`、`retry` |
| `media.ts` | `/admin/media` | `generateImage`、`generateVideo`、`listAssets`、`getAsset`、`getAssetTask`、`retryAsset`、`transferAsset`、`deleteAsset` |
| `uploads.ts` | `/admin/uploads` | `uploadImage`、`uploadVideo`（multipart，返回公网 URL） |
| `ai.ts` | `/admin/ai/*` | `listModels`、`listModelOptions`（`GET /ai/models/options?modality=`）、`getModel`、`syncModels`、`listRoutes`、`getRoute`、`createRoute`、`updateRoute`、`deleteRoute`、`testRoute`、`resetBreaker`、`health`、`probe`、`listTasks`、`getTask`、`retryTask`、`cancelTask`、`exportTasks`、`usageLogs`、`reconcile`、`usageSummary` |
| `platforms.ts` | `/admin/platforms` | CRUD、`testRule`、`detect` |
| `links.ts` | `/admin/links` | `list`、`get`、`backfill`、`batchBackfill`、`update`、`remove`、`check`、`indexCheck`、`markIndex`、`rebaseline`、`pause` / `resume`、`listChecks`（`GET /{id}/checks`）、`listIndexChecks`（`GET /{id}/index-checks`）、`exportLinks` |
| `monitoring.ts` | `/admin/monitoring` | `overview`、`listLinkChecks`、`getLinkCheck`、`listIndexChecks`、`getIndexCheck`、`runLinkChecks`、`runIndexChecks` |
| `alerts.ts` | `/admin/alerts` | `list`（筛选含 `target_type` / `target_id`，供链接详情时间线）、`get`、`summary`、`acknowledge`、`resolve`、`ignore`、`batchResolve` |
| `stats.ts` | `/admin/stats` | `overview`、`trends`、`breakdown`、`rankings`、`exportStats`、`recompute`（`timeout: 300000`） |

函数名为 camelCase 动词 + 资源；与后端动作一一对应（`adopt` ↔ `POST /{id}/adopt`）。接口路径、参数与响应以 [04-api-spec](./04-api-spec.md) 为准，`api/*.ts` 不得自行扩展后端不存在的路径。

### 3.5 状态、组合式函数与工具

| 文件 | 导出 | 说明 |
| --- | --- | --- |
| `store/auth.ts` | `useAuthStore`：`token`、`admin`、`permissions`、`isAllScope`、`login()`、`fetchMe()`、`logout()` | `permissions` 为字符串数组（权限码），`super_admin` 组成员同样以显式权限码判断；`isAllScope` = `admin.data_scope === "all"`（总后台） |
| `store/theme.ts` | `useThemeStore`：`mode`、`toggle()` | `dark` 时给 `html` 加 `dark` class（Element Plus 暗色变量） |
| `store/project.ts` | `useProjectStore`：`currentId`、`projects`、`setCurrent()`、`load()`、`ownerId`、`owners`、`setOwner()`、`loadOwners()`、`resetScope()` | 项目列表来自 `GET /admin/projects?status=active`（带 `owner_id`）；`owners` 来自 `GET /admin/projects/owner-options`；切换用户时 `currentId` 重置为 0；登录 / 退出时由 `store/auth.ts` 调用 `resetScope()` 清空 `ownerId`、`currentId`、`owners` 并移除 localStorage `aicreat.owner_id`（13 §12.2） |
| `store/alerts.ts` | `useAlertsStore`：`openCount`、`start()`、`stop()` | `Layout.vue` 挂载时按权限启动 |
| `composables/usePermission.ts` | `has(code)`、`hasAny(codes)`、`isAllScope` | 读 `useAuthStore().permissions` / `isAllScope` |
| `composables/usePolling.ts` | `usePolling(fn, { interval: 3000 })` → `start` / `stop` / `running` | `document.visibilityState !== "visible"` 时暂停；任务到终态时调用方 `stop()` |
| `composables/useProject.ts` | `projectId`（computed）、`requireProject()` | 生成类页面无当前项目时提示先选择项目 |
| `utils/format.ts` | `formatDateTime`（UTC ISO → 本地）、`formatQuota`、`formatCny`、`formatDuration` | 金额两位小数；额度千分位 |
| `utils/markdown.ts` | `renderMarkdown(md)`、`sanitizeHtml(html)` | markdown-it + 白名单清理 |
| `utils/download.ts` | `downloadBlob(blob, filename)`、`downloadText(text, filename, mime)` | CSV（后端已带 BOM）、MD、HTML |

### 3.6 组件

| 组件 | 用途 | 使用页面 |
| --- | --- | --- |
| `LangSwitch.vue` / `ThemeSwitch.vue` | 语言 / 主题切换 | `Layout.vue`、`Login.vue` |
| `ToolbarSelect.vue` | 列表页筛选下拉（状态 / 平台 / 能力等枚举） | 各列表页 |
| `ProjectSelect.vue` | 项目选择器（顶栏全局 + 表单内；只列可见项目，总后台选了用户时只列该用户的项目） | `Layout.vue`、生成表单 |
| `OwnerSelect.vue` | 用户视角切换器（`GET /admin/projects/owner-options`，首项「全部用户」；仅 `isAllScope && has("content.projects.view")` 时渲染） | `Layout.vue` 顶栏、`projects/Index.vue` 负责人字段 |
| `StatusTag.vue` | 状态枚举 → `el-tag` 颜色 / i18n 文案（读 `@aicreat/shared` 枚举） | 所有带状态列的表格 |
| `KpiCard.vue` / `TrendChart.vue` | KPI 卡片、echarts 折线 / 柱状封装 | `Dashboard.vue`、`stats/Reports.vue`、`ai/Usage.vue` |
| `MarkdownEditor.vue` / `MarkdownPreview.vue` | 正文编辑与预览 | `contents/Editor.vue`、`prompt-templates/Editor.vue` |
| `VersionDiff.vue` | 两个内容版本的差异对比 | `contents/Editor.vue` 版本抽屉 |
| `PromptVariablesForm.vue` | 按模板变量定义渲染表单 | `prompt-templates/Editor.vue` 预览、生成抽屉 |
| `ModelSelect.vue` | `GET /admin/ai/models/options?modality=` 模型下拉（`text` / `image` / `video`） | 生成表单的 `model?` 覆盖、`ai/Routes.vue`、`projects/Detail.vue`、`settings/Index.vue` GEO / SEO Tab |
| `ImageUpload.vue` | 参考图 / 参考视频上传（`POST /admin/uploads/image` / `video`，返回公网 URL 与 `public` 标志） | `media/ImageGenerate.vue`、`media/VideoGenerate.vue` |
| `AssetCard.vue` / `AssetPicker.vue` | 素材卡片、从素材库选择图片绑定到内容 | `media/Assets.vue`、`contents/Editor.vue` 素材面板 |
| `TaskProgress.vue` | `ai_tasks` / 素材进度条、状态与错误分类 | 生成页、批次页、`ai/Tasks.vue` |
| `LinkBackfillDialog.vue` | 回填链接弹窗（平台自动识别、多链接） | `links/Index.vue`、`contents/Editor.vue` 链接面板 |
| `EvidenceDrawer.vue` | 检测证据 JSON（`evidence`）展示 | `links/Detail.vue`、`monitoring/*` |
| `AlertBadge.vue` | 顶栏告警铃铛（读 `store/alerts`） | `Layout.vue`（仅有 `monitoring.alerts.view` 时渲染） |
| `JsonEditor.vue` | 配置 JSON 编辑（校验后提交 `PUT /admin/settings/{key}`） | `settings/Index.vue`、`ai/Routes.vue` 的 `params` |

### 3.7 `vite.config.ts` 与 i18n

```ts
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/admin/",
  plugins: [vue()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5174,
    proxy: {
      "/api": { target: "http://127.0.0.1:8100", changeOrigin: true },
      "/media": { target: "http://127.0.0.1:8100", changeOrigin: true },
    },
  },
});
```

- 构建产物 `apps/admin/dist/` 由 Nginx 以 `/admin/` 托管；`@aicreat/shared` 通过 workspace 依赖引入，`pnpm build` 先构建 shared 再构建 admin。
- 词条文件 `i18n/locales/zh-CN.ts` / `en-US.ts` 按命名空间组织：`common.*`（按钮 / 提示）、`menu.*`（菜单项，键与路由目录名一致，如 `menu.promptTemplates`）、`status.<enum>.<value>`（枚举文案，供 `StatusTag` 使用，如 `status.content_status.reviewing`）、`<page>.*`（页面私有词条）。新增可见文案必须同时补两种语言。
- 界面语言只影响后台文案；生成内容语言由 `projects.language` 决定（默认 `zh-CN`）。

## 4. 共享包 `packages/shared/`

```text
packages/shared/                       # @aicreat/shared
├── package.json                       # name @aicreat/shared，main/types 指向 dist/，scripts.build = tsc -p tsconfig.json
├── tsconfig.json
└── src/
    ├── index.ts                       # export * from "./types" / "./enums" / "./constants"
    ├── types.ts                       # 契约接口（以 API 字段名声明，JSON 列去掉 _json 后缀）
    ├── enums.ts                       # 全部状态枚举（as const）
    └── constants.ts                   # API_PREFIX、分页默认值、语言列表、上传限制、媒体参数枚举、能力列表、业务码
```

| 文件 | 内容 |
| --- | --- |
| `types.ts` | `AdminGroupRef`、`AdminProfile`、`AdminItem`、`AdminGroupItem`、`AdminPermissionItem`、`AdminOperationLogItem`（管理员、用户组、权限与操作日志，定义见 [07-admin-rbac](./07-admin-rbac.md) §9.7；`AdminProfile` / `AdminItem` / `AdminGroupItem` 含 `data_scope`（`AdminGroupRef` 不含））、`OwnerOption`（负责人候选，[13-user-data-scope](./13-user-data-scope.md) §12.4）、`Setting`、`Project`、`PromptTemplate`、`GenerationBatch`、`Keyword`、`Title`、`Content`、`ContentVersion`、`MediaAsset`、`AiTask`、`AiTaskSummary`、`BatchTaskSummary`、`AiModel`、`CapabilityRoute`、`AiUsageLog`、`Platform`、`PublishLink`、`LinkCheck`、`IndexCheck`、`Alert`、`DailyStats`、`StatsOverview`（统计总览响应 `meta` / `kpis` / `compare` / `breakdowns` / `series`；`kpis` 含任务级 `task_success_rate`，`breakdowns` 含 `cost_by_capability[]` 与 `cost_by_model[]`，见 [04-api-spec](./04-api-spec.md) §7.14、[12-dashboard-reports](./12-dashboard-reports.md) §9.1）、`TrendPoint`、`BreakdownRow`、`RankingRow`（趋势 / 分解 / 榜单响应的行结构，见 12 §9.2～§9.4），以及通用 `ApiResponse<T>`、`Page<T>`；字段与 [04-api-spec](./04-api-spec.md) 的响应一致（如 `PublishLink.seo_status` / `geo_status` 为解码后的对象）。接口专属的响应字段同样在此声明：文本生成接口（关键词 / 标题 / 内容系列，04 §6.9、§6.10、§6.11）与图片 / 视频生成接口（04 §6.13）响应的可选 `quota_warning?: {scope, limit, used, percent}`（`scope` ∈ `daily` / `project_monthly`；单个对象，一次请求最多一个，04 §5.2）；素材详情 `GET /admin/media/assets/{id}` 的 `references?: {cover_of, bound_content_id, referenced_by_asset_ids: {id, status}[], count}`（仅详情返回、列表不计算，04 §6.13）；能力路由写接口 `POST /admin/ai/routes`、`PUT /admin/ai/routes/{id}` 的响应为路由对象另附 `warnings: {loc, msg, type, input}[]`（每个 `is_available=0` 的主 / 备模型一项，`type=model_unavailable`，结构同 400 校验错误项，无警告为 `[]`，04 §6.15）；根任务摘要 `BatchTaskSummary`（`GET /admin/generation-batches/{id}` 的 `tasks[]` 项）与 `AiTaskSummary`（`GET /admin/contents/{id}/task`、`GET /admin/media/assets/{id}/task` 的返回，内容详情 `pending_tasks[]` 与素材详情 `task` 同结构）都含 `model_override: string \| null`（取根任务 `input_json.model`，文本与媒体相同，媒体不使用 `params.model`；前端以其非空识别使用了请求级覆盖模型） |
| `enums.ts` | 蓝本为 [00-overview](./00-overview.md) 的状态枚举总表：`KEYWORD_STATUS`、`TITLE_STATUS`、`CONTENT_STATUS`、`BATCH_STATUS`、`AI_TASK_STATUS`、`MEDIA_STATUS`、`LINK_ALIVE_STATUS`、`ERROR_CATEGORY`、`ALERT_TYPE` / `ALERT_STATUS` / `ALERT_SEVERITY`、`PROTOCOL`、`HEALTH_STATUS` / `BREAKER_STATE`、`SEO_INDEX_STATUS` / `GEO_CITE_STATUS`、`PROMPT_KIND` / `PROMPT_STATUS`、`CONTENT_STYLE`、`VERSION_SOURCE`、`REWRITE_MODE` / `REWRITE_SCOPE`、`KEYWORD_INTENT` / `KEYWORD_TYPE` / `KEYWORD_SOURCE` / `TITLE_SOURCE`、`MEDIA_KIND` / `MEDIA_USAGE_TYPE` / `MEDIA_SOURCE`、`MODALITY`、`AI_TASK_OPERATION` / `AI_TASK_TRIGGER_TYPE` / `AI_TASK_TARGET_TYPE`、`ALERT_TARGET_TYPE`、`CHECK_TYPE`（`baseline` / `scheduled` / `manual` / `retry`；`index_checks` 只用其中 `scheduled` / `manual`）、`LINK_CHECK_RULE`、`INDEX_KIND` / `SEO_ENGINE` / `GEO_ENGINE`、`INDEX_MATCH_MODE`、`SEO_PROVIDER` / `GEO_PROVIDER`、`PROJECT_STATUS`、`STATS_DIMENSION` / `STATS_GRANULARITY`、`ADMIN_GROUP_CODE`、`DATA_SCOPE`、`OPERATION_ACTION`。常量名 = 枚举名大写，同时导出联合类型（`KeywordStatus` 等） |
| `constants.ts` | `API_PREFIX = "/api/v1"`、`DEFAULT_PAGE_SIZE = 20`、`MAX_PAGE_SIZE = 100`、`SUPPORTED_LOCALES = ["zh-CN", "en-US"]`、`UPLOAD_LIMITS = { image_mb: 10, video_mb: 200 }`（前端预检默认值，与 `MAX_IMAGE_SIZE_MB` / `MAX_VIDEO_SIZE_MB` 默认一致，以后端为准）、`IMAGE_RESOLUTIONS`、`ASPECT_RATIOS`、`VIDEO_RESOLUTIONS`、`CAPABILITIES`、`BUSINESS_CODES` |

`enums.ts` 与 `constants.ts` 的写法：

```ts
// enums.ts
export const KEYWORD_STATUS = ["candidate", "adopted", "discarded"] as const;
export type KeywordStatus = (typeof KEYWORD_STATUS)[number];

export const CONTENT_STATUS = ["draft", "generating", "ready", "reviewing", "approved", "rejected", "published", "archived"] as const;
export type ContentStatus = (typeof CONTENT_STATUS)[number];

// constants.ts
export const API_PREFIX = "/api/v1";
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;
export const SUPPORTED_LOCALES = ["zh-CN", "en-US"] as const;
export const IMAGE_RESOLUTIONS = ["1080p", "2k", "4k"] as const;
export const ASPECT_RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16"] as const;
export const VIDEO_RESOLUTIONS = ["480p", "720p", "1080p", "4k"] as const;
export const CAPABILITIES = ["keyword", "title", "content", "rewrite", "image", "video", "geo_check", "seo_check"] as const;
export const BUSINESS_CODES = {
  BAD_REQUEST: 400, UNAUTHORIZED: 401, FORBIDDEN: 403, NOT_FOUND: 404, CONFLICT: 409, RATE_LIMITED: 429,
  TEMPLATE_VARIABLE_MISSING: 4221, PUBLIC_URL_REQUIRED: 4222, QUOTA_LIMIT_REACHED: 4291,
  UPSTREAM_ERROR: 5021, CAPABILITY_UNAVAILABLE: 5031,
} as const;
```

职责：被 `apps/admin` 引用，保证前端与后端契约的类型一致；后端 `app/models.py` 顶部以 `Literal` 声明同名取值集合，两边取值必须逐字一致（新增枚举值时两处同时修改）。

## 5. 反代、编排与脚本

| 位置 | 内容 | 权威文档 |
| --- | --- | --- |
| `nginx/nginx.conf` | `/api/` → `server:8000`；`/media/` → `server:8000`；`/admin/` → `apps/admin/dist`（history 回退到 `/admin/index.html`）；`/` → `302 /admin/` | [05-deployment](./05-deployment.md) |
| `docker-compose.yml` | 服务 `mysql` / `redis` / `server` / `worker` / `monitor-worker` / `nginx`；`env_file: .env`（仓库根）；三个 Python 服务共用 `build: ./server`、不同 `command`（gunicorn / `python -m app.worker` / `python -m app.monitor_worker`），相同的 `environment:` 覆盖 `DATABASE_URL=mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@mysql:3306/${MYSQL_DATABASE}`、`REDIS_URL=redis://redis:6379/0`、`LOCAL_STORAGE_DIR=storage`，共享卷 `media_data:/app/storage`；mysql / redis healthcheck，三个 Python 服务 `depends_on` 二者 `service_healthy` 且 `restart: unless-stopped`；server healthcheck `curl -f http://127.0.0.1:8000/api/v1/health`；nginx 挂载 `./nginx/nginx.conf` 与宿主机构建产物 `./apps/admin/dist`（`pnpm install --frozen-lockfile && pnpm build:shared && pnpm build:admin`），`depends_on: server`；发布流程固定为 `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"` → `docker compose up -d` | [05-deployment](./05-deployment.md) |
| `.env.example` | 根 `.env` 模板；`server/.env.example` 为其副本；本机开发把副本复制为 `server/.env` | [05-deployment](./05-deployment.md)、[06-getting-started](./06-getting-started.md) |
| `scripts/dev-setup.ps1` | Windows 首次一键初始化：检查 Python 3.11+ / Node.js 18+ / pnpm / Docker；复制根 `.env` 与 `server/.env`（已存在不动）；`docker compose up -d mysql redis` 并等待 healthy（`-SkipDocker` 用本机服务）；创建 `server/.venv` 并 `pip install -e ".[dev]"`；`alembic upgrade head` 与 `seeds/seed.py`；`pnpm install` 与 `pnpm build:shared`；提示 `ZHIQI_API_KEY` 是否已填；最后调用 `dev-restart.ps1`（`-NoStart` 跳过）。全部步骤幂等 | [06-getting-started](./06-getting-started.md) |
| `scripts/dev-restart.ps1` | 结束本脚本先前启动的 API / worker / monitor / admin 进程后重启 `dev:server` / `dev:worker` / `dev:monitor` / `dev:admin`；8100 / 5174 仍被其它进程占用时自动换端口并打印实际端口 | [06-getting-started](./06-getting-started.md) |
| `scripts/db-backup.sh` | `mysqldump` 到 `backups/`，保留 14 天 | [05-deployment](./05-deployment.md) |
| `server/Dockerfile` | Python 3.11 基础镜像，复制源码后 `pip install -e .`（`app` 可导入，容器内 `python seeds/seed.py` 依赖于此），默认 `command` 为 gunicorn；worker 容器以 `python -m app.worker` / `python -m app.monitor_worker` 覆盖 | [05-deployment](./05-deployment.md) |

## 6. 命名与组织约定

### 6.1 命名规则

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 表名 / 字段名 / 枚举值 / 配置键 / Redis 键段 | `snake_case`，枚举值小写字符串（不用 MySQL ENUM） | `publish_links.alive_status = "suspected_deleted"` |
| JSON 列 | 列名以 `_json` 结尾；API 字段名去掉后缀 | `fallback_models_json` ↔ `fallback_models` |
| URL 资源 | `kebab-case` 复数；对象级动作 `POST /{resource}/{id}/{action}`，集合级动作 `POST /{resource}/{action}` | `/admin/prompt-templates`、`POST /admin/links/{id}/index-check`、`POST /admin/keywords/batch-status` |
| 权限码 | `module.resource.action`；每个资源一个 `menu` 型 `*.view` 与若干 `action` 型 | `content.keywords.generate`、`monitoring.alerts.handle` |
| 环境变量 | 大写 `SNAKE_CASE`，按子系统前缀分组 | `ZHIQI_API_KEY`、`MONITOR_CONCURRENCY`、`SEO_GSC_SITE_URL` |
| Redis 键 | 冒号分段，首段为类别 | `queue:ai_tasks`、`lock:monitor:link_check:{link_id}`、`cache:stats:overview:{scope_key}:{project_id}:{range}` |
| Python 模块 | `snake_case.py`；路由文件 = 资源复数；service = `<resource>_service.py`；task = 动词短语 | `generation_batches.py`、`link_check_service.py`、`schedule_link_checks.py` |
| Python 类 / 函数 | 类 `PascalCase`、函数 `snake_case`；Pydantic 请求 `XxxBody` / 响应 `XxxOut`；dataclass 结果 `XxxResult` | `LinkBackfillBody`、`ImageSubmitResult` |
| Vue 页面 | 目录 `kebab-case`，文件 `Index.vue` / `Detail.vue` / `Editor.vue`；单页面资源用具名文件 | `views/prompt-templates/Editor.vue`、`views/media/Assets.vue` |
| Vue 组件 | `PascalCase.vue`，多词 | `LinkBackfillDialog.vue` |
| 前端 TS 文件 | `api/`、`store/` 用 camelCase 文件名；组合式函数 `useXxx.ts` | `api/promptTemplates.ts`、`composables/usePolling.ts` |
| TS 枚举常量 | 枚举名大写 + `as const`，联合类型 PascalCase | `LINK_ALIVE_STATUS` / `LinkAliveStatus` |
| 时间 | 数据库 `DATETIME` 存 UTC；API 输入输出 ISO 8601 UTC（`2026-10-06T08:00:00Z`）；前端展示时转本地 | `created_at`、`next_check_at` |
| 测试 | `server/tests/test_<domain>.py` | `test_monitoring.py` |

### 6.2 一资源一组文件

后端按「资源」拆分路由、schema 与 service，前端按「页面 views + 组件 components + 接口 api + 状态 store」四段式组织，同一资源涉及的文件固定如下：

| 资源 | 路由文件 | schemas | service | 前端 api | 页面 |
| --- | --- | --- | --- | --- | --- |
| 项目 | `projects.py` | `project.py` | `project_service.py` | `projects.ts` | `projects/Index.vue`、`projects/Detail.vue` |
| Prompt 模板 | `prompt_templates.py` | `prompt_template.py` | `prompt_template_service.py` | `promptTemplates.ts` | `prompt-templates/Index.vue`、`Editor.vue` |
| 关键词 | `keywords.py` | `keyword.py` | `keyword_service.py` + `generation_service.py` | `keywords.ts` | `keywords/Index.vue` |
| 标题 | `titles.py` | `title.py` | `title_service.py` + `generation_service.py` | `titles.ts` | `titles/Index.vue` |
| 内容 | `contents.py` | `content.py` | `content_service.py` + `generation_service.py` | `contents.ts` | `contents/Index.vue`、`Editor.vue` |
| 生成批次 | `generation_batches.py` | `generation_batch.py` | `generation_service.py` | `batches.ts` | `generation-batches/Index.vue` |
| 媒体 / 上传 | `media.py`、`uploads.py` | `media.py` | `media_service.py` | `media.ts`、`uploads.ts` | `media/Assets.vue`、`ImageGenerate.vue`、`VideoGenerate.vue` |
| AI 网关 | `ai_models.py`、`ai_routes.py`、`ai_tasks.py`、`ai_usage.py` | `ai.py` | `ai_catalog_service.py`、`ai_gateway_service.py`、`ai_task_service.py`、`ai_usage_service.py` | `ai.ts` | `ai/Models.vue`、`Routes.vue`、`Tasks.vue`、`Usage.vue` |
| 发布平台 | `platforms.py` | `platform.py` | `platform_service.py` | `platforms.ts` | `platforms/Index.vue` |
| 回填链接 | `links.py` | `link.py` | `link_service.py`、`link_check_service.py`、`index_check_service.py` | `links.ts` | `links/Index.vue`、`links/Detail.vue` |
| 监控记录 | `monitoring.py` | `monitoring.py` | `link_check_service.py`、`index_check_service.py` | `monitoring.ts` | `monitoring/LinkChecks.vue`、`IndexChecks.vue` |
| 告警 | `alerts.py` | `alert.py` | `alert_service.py` | `alerts.ts` | `alerts/Index.vue` |
| 报表 | `stats.py` | `stats.py` | `stats_service.py` | `stats.ts` | `Dashboard.vue`、`stats/Reports.vue` |
| 系统配置 | `settings.py` | `settings.py` | `settings_service.py` | `settings.ts` | `settings/Index.vue` |
| 鉴权与 RBAC | `auth.py`、`admins.py`、`admin_groups.py`、`admin_permissions.py`、`operation_logs.py` | `auth.py`、`admin_rbac.py` | `admin_rbac_service.py` | `auth.ts`、`admins.ts`、`groups.ts` | `Login.vue`、`admins/Index.vue`、`admin-groups/Index.vue`、`admin-operation-logs/Index.vue` |

### 6.3 组织规则

- **接口分组**：没有 public / user / community 分组，全部业务接口在 `/api/v1/admin/*`（管理员 JWT + 权限码），公开的只有 `POST /admin/auth/login`、`GET /admin/auth/site-info`、`GET /api/v1/health` 与 `/media/{key}`。
- **分层边界**：路由层只做入参校验、权限声明与调用 service；状态机、事务、缓存失效、Redis 计数、告警触发都在 service；`core` 不含业务规则、不导入 `services` / `models`；`core/zhiqi` 不读数据库。
- **异步任务**：所有耗时操作（文本生成、媒体提交 / 轮询 / 转存、检测、聚合）都由 worker 执行，API 只写任务表并入队；MySQL 任务表为权威状态，Redis List 只是触发信号（丢失由回收任务按 DB 补扫）。
- **配置分三层**：环境变量（`core/config.py`，密钥只在这里）→ `settings` 表配置键（`settings_service.get_config`，后台可改，Redis 缓存 60s）→ 请求级参数（`model?` 覆盖等）；运行期以数据库配置为准，环境变量只做 seed。
- **缓存失效**：写配置、路由、平台后分别 `cache_delete_prefix("cache:settings:")`、`cache_delete_prefix("cache:routes:")`、`cache_delete("cache:platforms:all")`；`sync_models` 后 `cache_delete_prefix("cache:ai:models:")` 并重建 `cache:ai:models:catalog`（读取统一经 `ai_catalog_service.catalog_entry`，键缺失时从 `ai_models` 回填）；重算统计后清 `cache:stats:*`；项目创建、删除、转移负责人提交后同样 `cache_delete_prefix("cache:stats:")`（可见项目集变化，[13-user-data-scope](./13-user-data-scope.md) §10.4）。
- **跨端契约只定义一次**：`packages/shared/src/types.ts` 与 `enums.ts` 是前端唯一的类型来源；后端 `models.py` 的 `Literal` 常量与之逐字一致。
- **国际化**：界面文案走 `i18n/locales` 词条；`settings.system_info` 按 `locale` 各存一行；其它配置键 `locale='*'`。
- **Mock 优先**：所有外部依赖（zhiqiapi、对象存储、SEO 第三方提供器、告警通道）都必须有无密钥可运行的本地实现，新增外部依赖时同时提供 Mock 并写入 [06-getting-started](./06-getting-started.md) 的 Mock 验证步骤。

### 6.4 新增一个资源的触点清单

| 步骤 | 文件 | 内容 |
| --- | --- | --- |
| 1 | `server/app/models.py`、`server/migrations/versions/000N_<name>.py` | ORM 模型 + Alembic 迁移（含索引、`created_at` / `updated_at`） |
| 2 | `server/app/schemas/<resource>.py` | `XxxBody` / `XxxOut`，JSON 列去 `_json` 后缀 |
| 3 | `server/app/services/<resource>_service.py` | 业务逻辑、事务边界、缓存与计数 |
| 4 | `server/app/api/admin/<resource>.py`、`server/app/api/__init__.py` | 路由（静态子路径先于 `/{id}`）、`admin.include_router(..., prefix="/<kebab>")`；资源属于业务数据时路由声明 `get_data_scope`、service 以 `scope` 为必填参数，并把前缀加入 `SCOPED_ROUTE_PREFIXES`（[13-user-data-scope](./13-user-data-scope.md) §9.3），表须能经 `project_id` 归属到项目负责人 |
| 5 | `server/app/core/admin_permissions.py` | `_resource("module", "resource", "名称", [(action, 说明)…], sort)`；启动时 `ensure_rbac_seed` 自动补齐权限行；默认组规则按 `DEFAULT_GROUP_PERMISSIONS` 自动覆盖 |
| 6 | `server/app/main.py` | `AUDIT_TARGET_TYPES` 增加「路由前缀 → target_type」 |
| 7 | `packages/shared/src/types.ts`、`enums.ts` | 契约接口与枚举（同步 `models.py` 的 `Literal`） |
| 8 | `apps/admin/src/api/<resource>.ts` | 接口封装 |
| 9 | `apps/admin/src/views/<resource>/Index.vue`、`router/index.ts`、`layouts/Layout.vue` | 页面、路由（`meta.permission`）、菜单项（`*.view`） |
| 10 | `apps/admin/src/i18n/locales/zh-CN.ts`、`en-US.ts` | 菜单与页面词条（两种语言同时补齐） |
| 11 | `server/tests/test_<domain>.py` | 服务与接口测试；涉及 worker 的补 `tasks` 用例 |
| 12 | `docs/` | 更新本文档目录树、[03-data-model](./03-data-model.md)、[04-api-spec](./04-api-spec.md)、[07-admin-rbac](./07-admin-rbac.md)；资源属于业务数据时同步 [13-user-data-scope](./13-user-data-scope.md) §4.2 归属矩阵、§6.3 接口组表与 §9.3 的受约束路由文件及 `SCOPED_ROUTE_PREFIXES` 列表（04 中的同名前缀列表一并更新） |

### 6.5 新增一个 worker 周期任务的触点清单

| 步骤 | 文件 | 内容 |
| --- | --- | --- |
| 1 | `server/app/tasks/<动词短语>.py` | 一个可独立调用的入口函数（返回 `int` 或 `dict` 计数）；函数内部获取自己的单例锁 `lock:*`（`with_lock(key, ttl)`，`finally` 释放），任何网络 I/O 与长事务只在该函数内发生（它在线程池线程中执行） |
| 2 | `server/app/worker.py` 或 `server/app/monitor_worker.py` | 主循环加一行 `timers.run_due("<name>", <间隔秒或配置键取值>, fn)`；每日任务用 `timers.run_daily("<name>", "HH:MM", fn, tz=stats_config["timezone"])`；间隔若需后台可改，则放进对应 `settings` 配置键并由主循环每轮 `settings_service.get_config` 读取（`0` 表示关闭） |
| 3 | `server/app/services/<resource>_service.py` | 任务文件只做领取 / 调度 / 分派，业务规则、状态机与事务写在 service |
| 4 | `server/tests/test_<domain>.py` | 直接调用入口函数的用例：锁互斥、幂等重入、异常分支不中断主循环 |
| 5 | `docs/` | 本文档 §2.1 目录树与 §2.8 任务表各加一行；[01-architecture](./01-architecture.md) 的 worker 任务总表与 Redis 键表（锁键、TTL）同步更新 |
