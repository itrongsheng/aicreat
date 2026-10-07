# CLAUDE.md

本文件为 Claude Code 在本仓库工作时的指引。内容提炼自 `README.md` 与 `docs/00~12`；细节以对应「权威文档」为准，本文件不重复定义取值。

## 项目概况

aicreat 是面向运营 / 内容团队的 **AI 内容生成与效果监控平台**（B 端内部工具，无 C 端用户）：关键词生成 → 标题生成 → 内容生成（大纲 / 正文 / SEO 要素）→ 图片 / 视频生成 → 运营手工发布后回填链接 → 链接删除检测 + SEO / GEO 收录检测 → 告警 + 控制台报表。所有 AI 能力统一经 **zhiqiapi**（`https://zhiqiapi.com/v1`）接入；`ZHIQI_API_KEY` 留空即全链路 Mock，无密钥可跑通。

**当前状态**：仓库目前只有设计文档（`README.md` + `docs/`），尚无代码。实现时严格按 [docs/README.md](docs/README.md)「当前规划顺序」1~10 推进，每一步完成后都要能在 Mock 模式下独立验证（各阶段最小验收信号见该节表格）。

技术栈与工程约定**完全沿用本地 `navigation` 工程**：

- 后端：Python 3.11 + FastAPI + Pydantic v2 / pydantic-settings + SQLAlchemy 2.x / Alembic + MySQL 8（utf8mb4_unicode_ci）+ Redis 7 + gunicorn/uvicorn + pyjwt/bcrypt + httpx + boto3。**不引入** Celery / RQ / Pillow / ffmpeg。
- 异步：两个轮询式 worker 进程 `python -m app.worker`（AI 任务、媒体轮询与转存、用量对账、模型同步、健康探测、过期回收）与 `python -m app.monitor_worker`（删除检测、收录检测、每日统计聚合、告警评估）。
- 前端：唯一前端 `apps/admin`，Vue 3 + Vite + TypeScript + Element Plus + Pinia + Vue Router + vue-i18n（zh-CN / en-US）+ axios + echarts。
- 共享包 `packages/shared`（`@aicreat/shared`：types / enums / constants）；pnpm@9 workspace（`apps/*`、`packages/*`）。

## 文档地图（先读文档再写代码）

全套文档共用一份命名基准：表名、字段、状态枚举、接口路径、权限码、环境变量、配置键、Redis 键在所有文档中**逐字一致**，每项只在一篇权威文档中完整定义。

| 要查的内容 | 权威文档 |
| --- | --- |
| 产品定位、角色、模块能力、业务流程、**状态枚举取值集合** | `docs/00-overview.md` |
| 服务拓扑、数据流时序、后端分层、**Redis 键 / 队列 / 锁**、**worker 任务总表** | `docs/01-architecture.md` |
| 完整目录树、模块职责、**命名约定**、新增资源 / 周期任务触点清单 | `docs/02-project-structure.md` |
| **24 张表**字段 / 索引、`settings` 配置键清单、**一致性与事务规则** | `docs/03-data-model.md` |
| 响应结构、分页、**业务码表**、全部 `/api/v1/admin/*` 接口与权限码 | `docs/04-api-spec.md` |
| **完整 `.env` 清单**、docker-compose、Nginx、发布、迁移回滚、备份 | `docs/05-deployment.md` |
| 启动步骤、Mock 验证、接入真实 zhiqiapi、常用命令、**排错表** | `docs/06-getting-started.md` |
| RBAC、**90 个权限码全表**、系统用户组默认权限、审计 | `docs/07-admin-rbac.md` |
| zhiqiapi 适配层、能力路由 / 候选链、重试 / 熔断、错误分类、对账、`ai_tasks` 状态机 | `docs/08-zhiqiapi-integration.md` |
| Prompt 模板、生成批次、关键词 / 标题 / 内容状态机、频控与配额 | `docs/09-generation-pipeline.md` |
| 图片 / 视频任务生命周期、轮询与转存、素材与文章关联 | `docs/10-media-generation.md` |
| 回填、平台规则、删除检测判定、SEO / GEO 收录检测、告警规则、SSRF | `docs/11-link-backfill-and-monitoring.md` |
| 指标公式、`daily_stats` 口径、报表接口与页面 | `docs/12-dashboard-reports.md` |

功能文档 07~12 末尾均有「测试范围」「验收标准」「实施顺序」，实现某模块时以此为完成标准。

## 常用命令

根 `package.json` 的 `dev:*` / `smoke:api` 脚本写死 `.venv\Scripts\python.exe`，仅 Windows 可直接用；macOS / Linux 在 `server/` 下激活 venv 后运行等价命令。

```bash
# 依赖服务（必须先复制根 .env 再起 compose，否则 mysql 初始化失败）
cp .env.example .env && cp server/.env.example server/.env
docker compose up -d mysql redis          # 等 mysql healthy 再继续

# 后端（均在 server/ 目录、venv 已激活）
pip install -e ".[dev]"
alembic upgrade head                      # 0001_initial（24 张表）+ 0002_seed_permissions
python seeds/seed.py                      # 幂等：超管 admin/admin123、示例项目、系统模板、默认平台
uvicorn app.main:app --reload --host 127.0.0.1 --port 8100
python -m app.worker
python -m app.monitor_worker
alembic revision --autogenerate -m "msg"  # 由 app/models.py 生成迁移
pytest                                    # 全部后端测试
pytest tests/test_zhiqi_adapter.py -k mock

# 前端（仓库根）
pnpm install
pnpm build:shared                         # 新克隆或修改 packages/shared 后必须先执行
pnpm dev:admin                            # http://localhost:5174/admin/
pnpm build:admin

# Mock 端到端冒烟（需 API + 两个 worker 运行，且 monitor-worker 能访问公网）
pnpm smoke:api                            # = python scripts/integration_smoke.py [--base-url] [--username] [--password]
```

- 健康检查：`curl http://127.0.0.1:8100/api/v1/health`（两个 worker 未启动时 `status=degraded` 属预期）；OpenAPI：`http://127.0.0.1:8100/docs`。
- 默认账号 `admin / admin123`（`super_admin`）。
- 端口：API 开发 8100 / 容器 8000；admin 5174（`base: /admin/`，Vite 代理 `/api`、`/media` → `127.0.0.1:8100`）。

## 架构硬性规则

### 分层与依赖方向（只允许自上而下）

- `api`（路由层）→ `schemas` / `services` / `core`：只做入参校验、`require_permission`、调用 service、返回 schema；**不直接写 SQL / Redis**。
- `tasks`（worker 周期任务）→ `services` / `core`：薄壳，只做领取 / 调度 / 持锁（`lock:*`），业务规则在 service。
- `services` → `models` / `core` / `core.zhiqi`：事务边界、状态机、缓存读写与失效、告警触发、冗余计数都在这里；跨表一致性规则**同一事务**完成（清单见 03「一致性与事务规则」）；JSON 列由 service 编解码。
- `core` 无业务语义，不导入 `services` / `models`。
- `core/zhiqi` **不读数据库**；路由解析、候选链、额度落库与业务回写全部在 `services/ai_gateway_service.py`。`ai_gateway_service` 是**唯一**允许发起生成类计费请求的 service。
- `main.py` 只做装配；`worker.py` / `monitor_worker.py` 只做主循环，主循环线程**禁止任何网络 I/O**，单轮阻塞 ≤ 2s，耗时工作进线程池（并发上限用进程内 `threading.BoundedSemaphore`）。

### 异步与状态

- 所有耗时操作（文本生成、媒体提交 / 轮询 / 转存、检测、聚合）由 worker 执行，API 只写任务表并入队。
- **MySQL 任务表是权威状态**，Redis List 只是触发信号，丢失由 `recover_stale_tasks` 按 DB 补扫。
- 启动引导：`main.py` 与两个 worker 在 `lock:bootstrap` 内执行 `ensure_rbac_seed` / `ensure_default_settings` / `ensure_default_routes`，全部幂等（`INSERT … ON DUPLICATE KEY UPDATE id=id` 或逐条捕获 `IntegrityError`）。

### 配置三层

环境变量（`core/config.py`，**密钥只在这里**）→ `settings` 表配置键（`settings_service.get_config`，后台可改，Redis 缓存 60s）→ 请求级参数。运行期以数据库为准，环境变量只做首次 seed。密钥（`ZHIQI_API_KEY`、`OSS_SECRET_KEY`、`SEO_*_API_KEY`…）永不入库、不入日志、不入响应，后台只显示 `configured: true/false`。

### 安全

- 外部 URL 抓取一律走 `core/safe_fetch.py`（只允许 http/https、80/443、公网 IP 且逐跳校验、≤ 3 跳、≤ 2 MB、固定 UA、不带 Cookie、不执行 JS）。
- SEO / GEO 收录检测**不抓取搜索引擎结果页 HTML**，只经提供器 / 联网模型。
- zhiqiapi 返回的临时 URL **必须转存**后才写入 `media_assets.url`。

## 编码约定

| 对象 | 规则 |
| --- | --- |
| 表 / 字段 / 枚举值 / 配置键 / Redis 键段 | `snake_case`；枚举用 `VARCHAR` 小写字符串，不用 MySQL ENUM |
| JSON 列 | 列名 `*_json`，API 字段名去掉后缀（`fallback_models_json` ↔ `fallback_models`） |
| URL | 资源 `kebab-case` 复数；对象级动作 `POST /{resource}/{id}/{action}`，集合级 `POST /{resource}/{action}` |
| 路由注册 | 静态子路径（`summary` / `export` / `generate` / `import` / `batch-*` / `sync` / `options` / `detect` / `overview` …）**必须先于**同方法 `/{id}` 注册，否则 422 |
| 权限码 | `module.resource.action`；每资源一个 `menu` 型 `*.view` + 若干 `action` 型 |
| 响应 / 异常 | `{code, message, data}`（`code=0` 成功），经 `core.response.ok / fail / paginated`；业务异常 `BusinessError(message, code, http_status, data)`，业务码只用 04 业务码表中的值 |
| 分页 | `page`（从 1）/ `page_size`（默认 20，最大 100）→ `{items, total, page, page_size}`；CSV 导出 `GET …/export?format=csv`（UTF-8 BOM，≤ 50,000 行） |
| 时间 | 库内 UTC `DATETIME`，API ISO 8601 UTC；所有表含 `created_at` / `updated_at` |
| 主键 / 数值 | `BIGINT AUTO_INCREMENT`（`settings(key, locale)` 等复合主键例外）；布尔 `TINYINT(1)`；额度 `BIGINT`；金额 `DECIMAL(14,6)` |
| 删除 | 不做通用软删除，用状态（`archived` / `discarded` / `deleted`）表达；物理删除仅限草稿类对象 |
| Python | 模块 `snake_case.py`；路由文件 = 资源复数，service = `<resource>_service.py`，task = 动词短语；Pydantic 请求 `XxxBody` / 响应 `XxxOut`，dataclass 结果 `XxxResult` |
| Vue / TS | 页面目录 `kebab-case/Index.vue`（`Detail.vue` / `Editor.vue`）；组件 `PascalCase.vue` 多词；`api/`、`store/` 文件 camelCase；组合式函数 `useXxx.ts`；TS 枚举大写 + `as const` |
| 测试 | `server/tests/test_<domain>.py` |

- **跨端契约只定义一次**：`packages/shared/src/types.ts` / `enums.ts` 是前端唯一类型来源，后端 `app/models.py` 顶部的 `Literal` 常量必须与之逐字一致，改一处必须同步另一处。
- 界面文案走 `apps/admin/src/i18n/locales/zh-CN.ts` 与 `en-US.ts`，两种语言同时补齐。
- 写配置 / 路由 / 平台后清对应缓存（`cache:settings:*`、`cache:routes:*`、`cache:platforms:all`）。
- **Mock 优先**：新增任何外部依赖必须同时提供无密钥可运行的本地实现，并补进 `docs/06-getting-started.md` 的 Mock 验证步骤。
- zhiqiapi 只按 08 中已核实的契约实现，其余上游细节标注「以 zhiqiapi 官方文档为准」，不臆测字段。

## 新增资源 / 周期任务

按 `docs/02-project-structure.md` §6.4 / §6.5 的触点清单逐项完成，要点：

- **资源**：`models.py` + Alembic 迁移 → `schemas/<resource>.py` → `services/<resource>_service.py` → `api/admin/<resource>.py` 并在 `api/__init__.py` 挂载 → `core/admin_permissions.py` 登记权限 → `main.py` 的 `AUDIT_TARGET_TYPES` → `packages/shared` 类型与枚举 → `apps/admin` 的 api / 页面 / 路由 `meta.permission` / 菜单 / i18n → `tests/test_<domain>.py` → 更新 docs。
- **周期任务**：`tasks/<动词短语>.py` 独立入口函数（自持 `lock:*`，网络 I/O 只在函数内）→ 在对应 worker 主循环加 `timers.run_due(...)` / `timers.run_daily(...)` → 业务写进 service → 测试锁互斥、幂等重入、异常不中断主循环 → 同步 01 的 worker 任务总表与 Redis 键表。

## 修改文档时

- 每篇以 `# NN 标题` 开头；文档间用相对链接（`./03-data-model.md`），根 `README.md` 用 `docs/` 前缀。
- 权威定义只出现一次：非权威文档只写一句引用（≤ 5 行摘要表），名称逐字一致；改动某项取值时先改权威文档，再全局搜索引用处同步。
- mermaid：拓扑 `flowchart LR`、时序 `sequenceDiagram`、状态机 `stateDiagram-v2`、ER `erDiagram`；接口示例用 `http` + `json` 代码块。
- 代码与文档不一致时，以文档为准实现；确需改设计时先改文档再改代码。

## 常见坑

- `settings` 表列名 `key` 是 MySQL 8 保留字，迁移与手写 SQL 中写作 `` `key` ``。
- `@aicreat/shared` 的 `main` / `types` 指向被 gitignore 的 `dist/`：不先 `pnpm build:shared`，Vite 解析失败、`build:admin` 失败。
- `python -m app.worker` 等命令必须在 `server/` 目录执行（模块路径与 `.env` 按当前目录解析）。
- 删除检测**没有 Mock**，始终真实抓取；回填 `127.0.0.1` / 内网地址会得到 `unknown`（`matched_rule=ssrf_blocked`）。
- 报表 `seo_index_rate` / `geo_cite_rate` 分母只含完成过 ≥ 1 轮 `scheduled` 收录检测的链接，手动检测不计；刚回填的链接会使其为 `null`。
- 从 Mock 切到真实模式后，Mock 期间 seed 的 `geo_engines` 需在「系统配置 → GEO 引擎」逐个填写模型或停用。
- 更多症状与处理见 `docs/06-getting-started.md`「九、排错」。
