# 06 本地启动

本文档说明如何在开发机上从零跑起 aicreat 的四个本地进程（API、`app.worker`、`app.monitor_worker`、管理后台），在**没有 `ZHIQI_API_KEY`** 的 Mock 模式下走通「建项目 → 关键词 → 标题 → 内容 → 图片 → 视频 → 回填链接 → 手动触发检测 → 删除检测与告警 → 报表」全流程，再给出接入真实 zhiqiapi 的步骤、常用命令、默认账号与排错表。

- 环境变量全表与 docker-compose / Nginx 部署：[05-deployment](./05-deployment.md)
- 目录树与模块职责：[02-project-structure](./02-project-structure.md)
- zhiqiapi 适配层、Mock 实现、能力路由与健康探测：[08-zhiqiapi-integration](./08-zhiqiapi-integration.md)
- 各步骤涉及的接口定义：[04-api-spec](./04-api-spec.md)；表与字段：[03-data-model](./03-data-model.md)
- 各步骤背后的业务规则：权限码与系统用户组 [07-admin-rbac](./07-admin-rbac.md)；关键词/标题/内容状态机 [09-generation-pipeline](./09-generation-pipeline.md)；图片/视频任务生命周期 [10-media-generation](./10-media-generation.md)；回填、删除检测、收录检测与告警 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md)；报表指标公式 [12-dashboard-reports](./12-dashboard-reports.md)

## 环境要求

| 工具 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.11+ | 后端 API 与两个 worker 共用一个 venv；依赖见 `server/pyproject.toml`（httpx、SQLAlchemy 2.x、Alembic、boto3…），**不需要** ffmpeg / Pillow |
| Node.js | 20 LTS（最低 18） | Vite 5 + Vue 3 管理后台 |
| pnpm | 9.x（根 `package.json` 的 `packageManager: pnpm@9`） | `corepack enable && corepack prepare pnpm@9.0.0 --activate`，或 `npm i -g pnpm@9` |
| MySQL | 8.x | 数据库 `aicreat`，字符集 `utf8mb4`、排序规则 `utf8mb4_unicode_ci` |
| Redis | 7.x | 缓存、任务队列（`queue:*`）、锁（`lock:*`）、频控、worker 心跳、Mock 任务状态 |
| Docker（可选） | Docker Engine 24+ / Docker Desktop 4.x（需 `docker compose` v2 子命令） | 只用来启动 `mysql`、`redis` 两个容器；本机已装服务时可不用 |
| 终端 | — | Windows：PowerShell 5.1+；macOS/Linux：bash/zsh |

本地开发端口固定为 API `8100`（容器内 `8000`）、管理后台 `5174`（`base: /admin/`）。根 `package.json` 的 `dev:*` 脚本固定写死 Windows 路径 `server\.venv\Scripts\python.exe`（见「常用命令」）；macOS/Linux 直接运行脚本对应的命令即可。

### 本地进程拓扑

```mermaid
flowchart LR
    B["浏览器"] -->|"http://localhost:5174/admin/"| V["Vite dev server<br/>apps/admin · 5174"]
    V -->|"代理 /api、/media"| A["uvicorn app.main:app<br/>server · 8100"]
    A --> M[("MySQL 8<br/>aicreat")]
    A --> R[("Redis 7")]
    W["python -m app.worker"] --> M
    W --> R
    MW["python -m app.monitor_worker"] --> M
    MW --> R
    W -.->|"ZHIQI_API_KEY 为空 → MockZhiqiClient"| Z["zhiqiapi<br/>https://zhiqiapi.com/v1"]
    MW -.->|"SEO/GEO 检测经 ai_gateway_service"| Z
    A -.->|"管理动作：模型同步 / 一键测试 / 立即对账 / 素材 retry 复查"| Z
    MW -->|"safe_fetch 真实抓取（不经 zhiqiapi）"| P["外部发布平台页面"]
    A -->|"GET /media/..."| S["server/storage/<br/>LOCAL_STORAGE_DIR"]
    W -->|"转存生成结果"| S
```

四个进程之间只通过 MySQL（任务权威状态）与 Redis（队列、锁、心跳）协作，没有进程间直接调用。生成类接口（关键词/标题/内容/图片/视频）在 API 内只创建根任务并 `RPUSH queue:ai_tasks`，不调用上游；实际的生成调用发生在 `app.worker`，SEO/GEO 收录检测调用发生在 `app.monitor_worker`。API 进程直接调用 zhiqiapi 的只有四类同步管理动作：`POST /admin/ai/models/sync`（模型目录）、`POST /admin/ai/routes/{id}/test` 与 `POST /admin/ai/health/probe`（健康探测）、`POST /admin/ai/usage/reconcile`（用量对账）、`POST /admin/media/assets/{id}/retry`（复查旧上游任务）。无论哪个进程发起，`ZHIQI_API_KEY` 为空时都走同一个 `MockZhiqiClient`。

## 一、准备依赖服务

顺序固定为**先复制环境变量文件，再启动 MySQL / Redis**：`docker compose` 的变量插值（mysql 服务的 `${MYSQL_ROOT_PASSWORD}` / `${MYSQL_DATABASE}` / `${MYSQL_USER}` / `${MYSQL_PASSWORD}` 均无缺省值）与三个应用服务的 `env_file: .env` 都读取仓库根 `.env`（[05-deployment](./05-deployment.md) §2.1、§3.2）。没有根 `.env` 就执行 `docker compose up -d mysql redis`，要么 compose 因找不到 `env_file` 直接报错，要么 mysql 拿到空的 root 密码：`mysql:8` 入口脚本拒绝初始化并反复重启，healthcheck 永远不会 healthy，库与账号 `aicreat` 也不会创建。

### 1. 环境变量

模板有两份且内容同源：仓库根 `.env.example`，以及它的副本 `server/.env.example`（改一处须同步另一处，见 [05-deployment](./05-deployment.md)）。各复制为一份实际配置：

| 文件 | 来源模板 | 读取方 | 说明 |
| --- | --- | --- | --- |
| 根 `.env` | `.env.example` | `docker compose`：变量插值（`${MYSQL_*}` 等）与 `server` / `worker` / `monitor-worker` 的 `env_file: .env` | 用 compose 启动 mysql / redis 前必须先建根 `.env`（compose 的变量插值与 `env_file` 都读它）；完全使用本机 MySQL / Redis 时才可不建。compose 在 `environment:` 中把 `DATABASE_URL` / `REDIS_URL` / `LOCAL_STORAGE_DIR` 覆盖为容器内地址，因此根 `.env` 里保持本机开发值即可 |
| `server/.env` | `server/.env.example` | 本机运行的 API / `app.worker` / `app.monitor_worker`（pydantic-settings 按**当前目录**解析，所以后端命令一律在 `server/` 下执行） | 本机开发只改这一份 |

Windows（PowerShell）：

```powershell
Copy-Item .env.example .env
Copy-Item server\.env.example server\.env
```

macOS / Linux：

```bash
cp .env.example .env
cp server/.env.example server/.env
```

Mock 模式本地开发只需确认下面这些键（其余保持模板默认值，完整清单见 [05-deployment](./05-deployment.md)）：

```ini
DATABASE_URL=mysql+pymysql://aicreat:password@127.0.0.1:3306/aicreat
REDIS_URL=redis://127.0.0.1:6379/0
ADMIN_JWT_SECRET=please-change-me-admin
DEV_MODE=true                                   # 放宽 CORS、允许 http 参考 URL、错误带堆栈
ALLOWED_ORIGINS=http://127.0.0.1:5174,http://localhost:5174
PUBLIC_BASE_URL=http://127.0.0.1:8100           # /media 公网 URL 前缀；Mock 模式允许 127.0.0.1
STORAGE_MODE=local
LOCAL_STORAGE_DIR=storage                       # 相对 server/，即 server/storage/
ZHIQI_BASE_URL=https://zhiqiapi.com/v1
ZHIQI_API_KEY=                                  # 留空 = Mock 模式
SEED_ADMIN_USERNAME=admin
SEED_ADMIN_PASSWORD=admin123
```

`ZHIQI_API_KEY` 为空时 `Settings.zhiqi_mock_mode=True`，`app.core.zhiqi.get_client()` 返回 `MockZhiqiClient`。`OSS_ENDPOINT` 为空时强制本地存储：文件落 `server/storage/`，经 `GET /media/{key}` 暴露，`media_assets.url` 前缀 = `PUBLIC_BASE_URL + /media/`。

### 2. MySQL 与 Redis

用 Docker 启动（推荐；前提是已按上一节复制出根 `.env`）：

```bash
docker compose up -d mysql redis
docker compose ps          # 等 mysql 的 healthcheck（mysqladmin ping）变为 healthy，首次约 30~100 秒
```

`docker-compose.yml` 读取仓库根 `.env` 中的 `MYSQL_ROOT_PASSWORD` / `MYSQL_DATABASE=aicreat` / `MYSQL_USER=aicreat` / `MYSQL_PASSWORD=password` 初始化数据库与业务账号，并把 `3306`、`6379` 映射到本机回环地址，与 `server/.env` 里的 `DATABASE_URL` / `REDIS_URL` 默认值一致。启动前可执行 `docker compose config` 确认变量插值没有空值告警（[05-deployment](./05-deployment.md) §11 第 2 项）。`MYSQL_*` 只在 `mysql_data` 卷首次初始化时生效，之后再改不会改变已建的库与账号（处理见「九、排错」）。

使用本机已装服务时手工建库建账号（字符集必须是 `utf8mb4`）：

```sql
CREATE DATABASE aicreat CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'aicreat'@'%' IDENTIFIED BY 'password';
GRANT ALL PRIVILEGES ON aicreat.* TO 'aicreat'@'%';
FLUSH PRIVILEGES;
```

## 二、启动后端（API）

Windows（PowerShell）：

```powershell
cd server
python -m venv .venv
.venv\Scripts\Activate.ps1            # 被执行策略拦截时先执行：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[dev]"               # dev 额外依赖只有 pytest；不跑测试可 pip install -e .

alembic upgrade head                  # 0001_initial（24 张表）+ 0002_seed_permissions（90 个权限码、4 个系统用户组）
python seeds/seed.py                  # 在 server/ 下执行；幂等 upsert：超管 admin/admin123、示例项目、系统 Prompt 模板、默认发布平台

uvicorn app.main:app --reload --host 127.0.0.1 --port 8100
```

macOS / Linux：

```bash
cd server
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head
python seeds/seed.py

uvicorn app.main:app --reload --host 127.0.0.1 --port 8100
```

各步骤写入的数据：

| 步骤 | 写入内容 | 位置 |
| --- | --- | --- |
| `alembic upgrade head`（0001） | 全部 24 张表；`settings` 表的 `key` 列是 MySQL 8 保留字，迁移与运维 SQL 一律写作 `` `key` `` | `server/migrations/versions/0001_initial.py` |
| `alembic upgrade head`（0002） | `admin_permissions`（90 个权限码）与 `admin_groups` 的 `super_admin` / `operator` / `reviewer` / `read_only` | `server/migrations/versions/0002_seed_permissions.py` |
| `python seeds/seed.py` | 超级管理员（`SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`，组 `super_admin`）、1 个示例项目、系统 Prompt 模板 `sys_keyword` / `sys_title` / `sys_outline` / `sys_content` / `sys_section` / `sys_rewrite` / `sys_expand` / `sys_shorten` / `sys_restyle` / `sys_seo_meta` / `sys_faq` / `sys_image_prompt` / `sys_geo_query` / `sys_seo_query`（`language=zh-CN`、`status=published`、`is_system=1`）、默认平台 `zhihu` / `wechat_mp` / `xiaohongshu` / `csdn` / `toutiao` / `baijiahao` / `website` / `other` | `server/seeds/seed.py` |
| API 启动 | 在 `lock:bootstrap` 内依次执行 `ensure_rbac_seed` → `ensure_default_settings`（9 个配置键：`generation_config` / `media_config` / `monitoring_config` / `geo_engines` / `seo_providers` / `alert_config` / `ai_routing_config` / `stats_config` / `system_info`，键不存在才插入默认 JSON 并深合并环境变量派生值）→ `ensure_default_routes`（8 条全局 `capability_routes`，Mock 模式主模型固定为 `mock-text` / `mock-image` / `mock-video`）；再把 `app/core/zhiqi/mock_assets/` 下的 `placeholder.png` 与 `placeholder.mp4` 复制到 `LOCAL_STORAGE_DIR/mock/`，供 `/media/mock/*` 访问 | `server/app/main.py` |

`seeds/seed.py` 与三个 `ensure_*` 全部幂等，重复执行不会产生重复数据；已存在的配置键与路由不会被改写（配置键在后台「系统配置」修改，全局路由在「AI 网关 → 能力路由」修改）。seed 一律在 `server/` 目录以 `python seeds/seed.py` 运行（容器内同样是 `python seeds/seed.py`，见「七、常用命令」）：以脚本方式运行时，Python 把脚本所在的 `server/seeds/` 而不是 `server/` 放进导入路径，`app` 包能被导入依赖上面的 `pip install -e`（可编辑安装；镜像构建时同样执行 `pip install -e .`，见 [05-deployment](./05-deployment.md) §3.3），未安装就运行会报 `ModuleNotFoundError: No module named 'app'`。

验证后端：

```http
GET http://127.0.0.1:8100/api/v1/health
```

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "status": "degraded",
    "db": true,
    "redis": true,
    "zhiqi_mode": "mock",
    "workers": {
      "worker": { "alive": false, "replicas": 0, "heartbeat_at": null },
      "monitor_worker": { "alive": false, "replicas": 0, "heartbeat_at": null }
    },
    "warnings": [],
    "version": "0.1.0"
  }
}
```

此时两个 worker 尚未启动，所以 `status=degraded`（只有 db / redis 不可用才返回 HTTP 503）。交互式接口文档在 `http://127.0.0.1:8100/docs`；所有业务接口都在 `/api/v1/admin/*` 之下并需要管理员 JWT，公开接口只有 `GET /api/v1/health`、`POST /api/v1/admin/auth/login`、`GET /api/v1/admin/auth/site-info` 与 `GET /media/{key}`。

## 三、启动 worker 与 monitor_worker

再开两个终端，同一 venv、同样在 `server/` 目录下：

Windows（PowerShell，无需激活 venv）：

```powershell
.venv\Scripts\python.exe -m app.worker
.venv\Scripts\python.exe -m app.monitor_worker
```

macOS / Linux（已 `source .venv/bin/activate`）：

```bash
python -m app.worker
python -m app.monitor_worker
```

| 进程 | 主循环休眠 | 职责（详见 [01-architecture](./01-architecture.md) worker 任务总表） | 不启动的后果 |
| --- | --- | --- | --- |
| `app.worker` | `WORKER_POLL_INTERVAL_SECONDS=2` | 消费 `queue:ai_tasks`（关键词/标题/内容文本生成、图片/视频提交）、轮询媒体任务、转存结果到本地存储、用量对账（每 300s）、模型目录同步（启动时立即一次，之后每 3600s）、健康探测（每 600s）、僵死任务回收（每 60s）、素材清理（每日 03:00） | 生成任务永远停在 `queued`；「模型目录」为空；素材停在 `submitted` / `generating` |
| `app.monitor_worker` | `MONITOR_POLL_INTERVAL_SECONDS=5` | 扫描到期链接入 `queue:link_checks`（每 60s）/ `queue:index_checks`（每 300s）并消费、`daily_stats` 聚合（启动时 `catch_up(days=3)`，每日 `00:30`，今日每 600s 增量）、告警规则评估（每 300s） | 回填链接永远 `pending`；没有收录检测结果；报表无 `daily_stats`（「今日」仅靠 `stats:rt:{date}:{project_id}` 兜底） |

两个进程启动时与 API 相同，在 `lock:bootstrap` 内依次执行 `ensure_rbac_seed` → `ensure_default_settings` → `ensure_default_routes`（均幂等），所以 worker 先于 API 启动时同样会补齐权限码与系统用户组、写入 9 个 `settings` 配置键（含 Mock 下的 `geo_engines`）与 8 条全局路由。之后每轮写心跳键 `worker:heartbeat:{name}:{hostname}:{pid}`（`name` ∈ `worker` / `monitor_worker`，TTL 900s）。并发上限由 `AI_MAX_CONCURRENCY_TEXT=4` / `AI_MAX_CONCURRENCY_IMAGE=2` / `AI_MAX_CONCURRENCY_VIDEO=1`（worker）与 `monitoring_config.link_check.global_concurrency=4` / `index_check.concurrency=2`（monitor_worker）决定，均为进程内信号量。

启动后再查一次 `GET /api/v1/health`：`workers.worker.alive` 与 `workers.monitor_worker.alive` 应为 `true`（90 秒内有心跳），`status` 变为 `ok`。worker 启动日志里应能看到 `sync_models` 写入 `mock-text` / `mock-image` / `mock-video` 三个模型。

## 四、启动前端（管理后台）

在仓库根目录安装依赖（pnpm workspace 一次装好 `apps/admin` 与 `packages/shared`），先构建共享包再启动：

```bash
pnpm install
pnpm build:shared              # = pnpm --filter @aicreat/shared build，生成 packages/shared/dist/
pnpm --filter admin dev        # 等价 pnpm dev:admin
```

`packages/shared/package.json` 的 `main` / `types` 指向 `dist/`，而 `packages/shared/dist/` 被 `.gitignore` 忽略，新克隆的仓库里没有它：跳过 `pnpm build:shared` 时 Vite 报 `Failed to resolve entry for package "@aicreat/shared"`，后台页面无法加载（`StatusTag` 等组件读取的状态枚举、`API_PREFIX` 等常量都在该包里）。修改 `packages/shared` 后要重新执行 `pnpm build:shared`。

打开 `http://localhost:5174/admin/`，用 `admin / admin123` 登录。`apps/admin/vite.config.ts` 固定 `base: /admin/`、端口 `5174`，并把 `/api` 与 `/media` 代理到 `http://127.0.0.1:8100`，所以浏览器端不会遇到跨域问题（直接请求 8100 时由 `ALLOWED_ORIGINS` 放行）。该配置没有设置 `server.host`，Vite 5 默认监听 `localhost`；在 Node 18/20 上 `localhost` 按系统解析顺序绑定，Windows / macOS 通常只绑定 IPv6 `::1`，此时访问 `http://127.0.0.1:5174` 会被拒绝连接，因此请用 `localhost` 访问。

登录后 `store/auth.ts` 调 `GET /api/v1/admin/auth/me` 取回权限码与 `zhiqi_mode`（`mock` / `live`），顶栏显示当前模式；侧边菜单按 `*.view` 权限码过滤，超管可见全部 7 组菜单（控制台、内容生产、媒体、AI 网关、发布与监控、报表、系统）。顶栏还有全局项目选择器（`store/project.ts`，持久化）、告警铃铛（仅有 `monitoring.alerts.view` 时渲染）、语言切换（zh-CN / en-US）与明暗主题。

### 一键启动 / 重启（Windows）

首次运行 `pnpm dev:restart` 前，先完成第一、二节（根 `.env` 与 `server/.env`、MySQL / Redis、venv、迁移与 seed），并在仓库根执行过 `pnpm install` 与 `pnpm build:shared`：脚本只负责启动进程，不构建共享包，缺少 `packages/shared/dist/` 时它启动的 Vite 同样无法加载后台。

```powershell
pnpm build:shared         # 首次运行前，或修改 packages/shared 之后
pnpm dev:restart          # = powershell -ExecutionPolicy Bypass -File scripts/dev-restart.ps1
```

`scripts/dev-restart.ps1` 与 navigation 同构：先结束本脚本上一次启动的 API / worker / monitor / admin 进程（不结束其它程序）；若 8100 / 5174 仍被其它进程占用，再从候选端口中选空闲端口（API 候选 `8100 / 8101 / 8110 / 8111 / 8120`，后台候选 `5174 / 5176 / 5177 / 5178 / 5179`）并打印实际地址。端口确定后同步改写 `server/.env` 中的 `PUBLIC_BASE_URL` / `OSS_PUBLIC_BASE_URL` / `ALLOWED_ORIGINS` 与 `apps/admin/vite.config.ts` 的代理目标，然后用 `server\.venv\Scripts\python.exe` 以隐藏窗口（`-WindowStyle Hidden`）启动 uvicorn、`app.worker`、`app.monitor_worker` 与 Vite（`--strictPort`），最后请求 `/api/v1/health` 与 `/admin/` 做健康检查并打印结果。隐藏窗口的进程**不落日志文件**：需要看日志时不要用本脚本，改为在四个前台终端分别运行「常用命令」表中的等价命令。端口被换掉后请以脚本输出的地址访问后台；运行冒烟脚本时也要用 `--base-url` 传入输出的 API 地址（见下文「自动冒烟脚本」）。

## 五、Mock 模式验证（无 `ZHIQI_API_KEY` 跑通全流程）

### Mock 行为一览

Mock 只替换 zhiqiapi 这一层（`app/core/zhiqi/mock.py`），数据库、队列、worker、状态机、对账、报表全部走真实代码路径，因此 Mock 下验证通过的流程在接入真实 Key 后不需要改任何业务逻辑。

| 能力 / 调用 | Mock 行为 | 验证时能看到什么 |
| --- | --- | --- |
| 文本（`keyword` / `title` / `content` / `rewrite`） | `mock_chat` 按模板特征返回：关键词 JSON 数组、N 个标题、JSON 大纲、含 H2 与 FAQ 的 Markdown 正文、`seo_meta` / `faq` JSON、`image_prompt` 固定英文提示词 | `ai_tasks` 尝试行 `model=mock-text`、`request_id=mock-<uuid>`、tokens 为估算值 |
| 图片（`image`） | `POST /v1/images/generations/async` 返回 `202 {id:"task_mock_…",status:"queued"}`；状态机存 `mock:task:{task_id}`，轮询 3 次后 `succeeded`，`data[0].url = PUBLIC_BASE_URL + /media/mock/placeholder.png` | 资产 `pending → submitted → generating → downloading → ready`，`url` 指向转存后的 `server/storage/media/images/…` |
| 视频（`video`） | `POST /v1/videos` 返回 `{id:"vidtask_mock_…",status:"queued"}`，轮询 5 次后 `succeeded`，URL 为 `/media/mock/placeholder.mp4` | 同上，`kind=video` |
| 转存 | `MockZhiqiClient.stream_download` 对 `/media/mock/` 路径直接复制 `app/core/zhiqi/mock_assets/` 下的文件，不发 HTTP | 不依赖 `PUBLIC_BASE_URL` 是否可达 |
| 模型目录 / 价格 | `mock_models()` 返回 `mock-text`（`openai` / `openai-response` / `anthropic`）、`mock-image`（`image-generation` / `image-generation-async` / `image-edit`）、`mock-video`（`openai-video`）；`mock_pricing()` 全部 `model_ratio=1`、`completion_ratio=1`、`quota_type=0` | `ai_models` 三行；额度 = prompt + completion tokens |
| 用量日志 | 每次调用向 `mock:usage_logs` `LPUSH` 一条 `type=2` 伪日志；`mock_token_logs()` 组装成 `/api/log/token` 响应 | 对账流程与真实模式完全相同，`quota_actual` 被回填 |
| SEO 收录检测 | 提供器 `zhiqi_web_search`（默认 `baidu`、`bing` 启用，`google` 关闭）经 `mock_chat` 返回 70% `indexed` / 30% `not_indexed`，证据 `evidence.source="mock"` | `index_checks` 行 `provider=zhiqi_web_search`、`model=mock-text` |
| GEO 引用检测 | `ensure_default_settings` 在 Mock 下把 `geo_engines` 的 6 个引擎（`baidu_ai` / `doubao` / `kimi` / `deepseek` / `perplexity` / `chatgpt`）seed 为 `enabled=true`（`model` 允许为空：引擎 `model` 为空时 `model_override=None`，走 `geo_check` 路由的 `mock-text`），70% `cited` / 30% `not_cited` | `index_checks` 行 `provider=zhiqi_model`、`model=mock-text` |
| 非 zhiqi 提供器（`baidu_ai_search` / `bing_webmaster` / `google_search_console`） | 返回 `unknown` + `error_category=auth_failed`、`error_message=credential_missing` | 默认配置未启用，不会出现 |
| 链接删除检测 | **不 Mock**：`safe_fetch.fetch_page` 真实抓取回填 URL（SSRF 规则：仅 http/https、公网 IP、≤ 3 跳、≤ 2 MB） | 需要能访问公网；无外网时结果 `unknown`（`network_error`） |
| 参考 URL 校验 | Mock 模式放行非公网 URL（真实模式返回 4222） | 可用本机 `/media/...` 地址做图生图 |

### 验证步骤

以下每一步都给出后台页面操作与等价接口；接口示例省略 `http://127.0.0.1:8100/api/v1` 前缀与 `Authorization: Bearer <token>` 头，响应省略 `code` / `message` 外层。时间均为 ISO 8601 UTC。

```mermaid
sequenceDiagram
    participant U as 运营（后台）
    participant API as server:8100
    participant Q as Redis 队列
    participant W as app.worker
    participant MW as app.monitor_worker
    participant Z as MockZhiqiClient
    participant DB as MySQL
    U->>API: POST /admin/keywords/generate
    API->>DB: generation_batches(queued) + ai_tasks 根任务(queued)
    API->>Q: RPUSH queue:ai_tasks
    W->>Q: LPOP
    W->>Z: chat/completions（mock_chat）
    W->>DB: keywords 20 条 / 批次 succeeded
    U->>API: POST /admin/titles/generate、POST /admin/contents/generate（同上）
    U->>API: POST /admin/media/images/generate
    W->>Z: POST /v1/images/generations/async → task_mock_…
    W->>Z: GET 轮询 ×3 → succeeded
    W->>DB: 转存 placeholder.png → media_assets ready
    U->>API: POST /admin/links（回填）
    API->>Q: RPUSH queue:link_checks（baseline）
    MW->>MW: safe_fetch 抓取 → alive（真实 HTTP）
    U->>API: POST /admin/links/1/index-check
    API->>Q: RPUSH queue:index_checks
    MW->>Z: SEO/GEO 提问（mock_chat，70% 命中）
    MW->>DB: index_checks + seo_status_json / geo_status_json
    U->>API: POST /admin/links（回填 404 URL）、POST /admin/links/2/check
    MW->>MW: safe_fetch 抓取 404 → suspected_deleted → deleted（真实 HTTP）
    MW->>DB: link_checks + alerts（link_deleted，warning）
    U->>API: 告警确认 / 解决（acknowledge、resolve）
    U->>API: GET /admin/stats/overview
```

**第 1 步：登录并确认 Mock 模式**

页面：`/admin/` 登录页，输入 `admin / admin123`。

```http
POST /admin/auth/login
Content-Type: application/json
```

```json
{ "username": "admin", "password": "admin123" }
```

响应 `data.token` 即后续 Bearer；`GET /admin/auth/me` 返回 `"zhiqi_mode": "mock"` 与全部 90 个权限码。连续 5 次密码错误会被 `rate:admin_login:{username}` 锁定 15 分钟。

**第 2 步：确认 AI 网关就绪**

页面：「AI 网关 → 模型目录」应已有 `mock-text` / `mock-image` / `mock-video`（worker 启动时同步）；为空则点「同步」。「AI 网关 → 能力路由」应有 8 条全局路由（`keyword` / `title` / `content` / `rewrite` / `image` / `video` / `geo_check` / `seo_check`，`project_id=0`，`is_enabled=1`），主模型分别为 `mock-text` / `mock-image` / `mock-video`。对任意一条点「一键测试」：

```http
POST /admin/ai/models/sync
POST /admin/ai/routes/1/test
GET  /admin/ai/health
```

`test` 返回 `[{"model":"mock-text","status":"healthy","latency_ms":3,"request_id":"mock-…","error_category":null}]`；`/ai/health` 的 `paused.quota_exceeded` / `paused.auth_failed` 应为 `false`，`workers[]` 列出两个 worker 副本且 `alive=true`。

**第 3 步：建项目**

页面：「内容生产 → 项目」。`seeds/seed.py` 已创建一个示例项目可直接使用；新建时：

```http
POST /admin/projects
```

```json
{
  "name": "示例-智能家居",
  "slug": "smart-home",
  "industry": "智能家居",
  "audience": "一二线城市 25~40 岁家庭用户",
  "brand_name": "aicreat",
  "brand_info": "语气专业克制，不夸大功效，不出现竞品名",
  "language": "zh-CN",
  "default_style": "news",
  "default_format": "markdown"
}
```

在顶栏项目选择器选中该项目，后续关键词/标题/内容/链接页面都按此 `project_id` 过滤。

**第 4 步：生成关键词**

页面：「关键词」→ 右上「生成」抽屉，填种子词与数量。

```http
POST /admin/keywords/generate
```

```json
{ "project_id": 1, "seeds": ["智能门锁", "全屋智能"], "count": 20, "audience": "首次装修的年轻家庭" }
```

响应 `{"batch_id": 1}`。页面用 `usePolling`（3s）轮询批次：

```http
GET /admin/generation-batches/1
```

```json
{ "id": 1, "kind": "keyword", "status": "succeeded", "requested_count": 20, "produced_count": 20, "task_total": 1, "task_done": 1, "task_failed": 0, "error_summary": null }
```

Mock 下几秒内 `queued → running → succeeded`；关键词列表出现 20 条 `status=candidate`，每条带 `intent` / `keyword_type` / `reason`。勾选若干条「采用」：

```http
POST /admin/keywords/batch-status
```

```json
{ "ids": [3, 7, 11], "action": "adopt" }
```

重复种子词再次生成时，重复项按 `UNIQUE(project_id, normalized_keyword)` 跳过并记入批次 `error_summary`（`duplicates=n`），属预期。

**第 5 步：生成标题**

页面：「标题」→ 选关键词 → 「生成」。

```http
POST /admin/titles/generate
```

```json
{ "project_id": 1, "keyword_ids": [3, 7], "count": 5, "style": "tutorial" }
```

每个关键词一个根任务（`operation=title_generate`），批次 `kind=title`。生成后可编辑（`PUT /admin/titles/{id}`，记 `original_title`、`is_edited=1`）、打分（`POST /admin/titles/{id}/score` `{"manual_score": 8.5}`），然后对一条标题「采用」（`POST /admin/titles/{id}/adopt`）。采用标题要求其关键词已 `adopted`，否则返回 409。

**第 6 步：生成内容**

页面：「内容」→ 「从标题生成」。

```http
POST /admin/contents/generate
```

```json
{ "project_id": 1, "title_ids": [12], "outline_first": true, "target_word_count": 1500, "include_faq": true, "include_seo_meta": true, "format": "markdown" }
```

响应 `{"batch_id": 3, "content_ids": [1]}`；内容以 `status=generating`（`prev_status=draft`）创建，根任务 `operation=content_generate` 依次执行大纲 → 正文（分段，每段一行尝试行）→ SEO 要素。编辑器页（`contents/Editor.vue`）轮询：

```http
GET /admin/contents/1/task
```

```json
{ "task_id": 9, "operation": "content_generate", "status": "succeeded", "progress": 100, "error_category": null, "error_message": null, "model_override": null, "finished_at": "2026-10-06T08:00:12Z" }
```

`model_override` 取根任务 `input_json.model`（本例未传 `model`，为 `null`；非空即表示使用了请求级覆盖模型、失败时不切换备选）。完成后内容 `generating → ready`，`GET /admin/contents/1` 应有 `outline`、`body`（含 H2 与 FAQ）、`summary`、`seo_title` / `seo_description` / `seo_keywords`、`faq`、`word_count`，版本抽屉显示 `version_no=1`、`source=generate`、`model=mock-text`。接着验证重写（第 13 步的 `ai_calls=30` 包含这 1 次重写，请勿跳过）：

```http
POST /admin/contents/1/rewrite
```

```json
{ "mode": "expand", "scope": "full", "instruction": "补充选购注意事项" }
```

内容回到 `generating`，完成后新增版本 `source=expand` 并回到 `ready`；在版本抽屉用 `VersionDiff` 对比两版，或 `POST /admin/contents/1/versions/{version_id}/restore` 回滚。

**第 7 步：审核**

默认 `generation_config.review_required=true`：

```http
POST /admin/contents/1/submit-review      # ready → reviewing
POST /admin/contents/1/approve            # reviewing → approved（权限 content.contents.review，超管具备）
```

要验证审核分权，可新建一个 `reviewer` 组账号登录后执行 `approve`；`operator` 组账号调用会得到 403。

**第 8 步：生成图片（封面）**

页面：「媒体 → 图片生成」或内容编辑器的素材面板。

```http
POST /admin/media/images/generate
```

```json
{ "project_id": 1, "content_id": 1, "usage_type": "cover", "count": 1, "resolution": "1080p", "aspect_ratio": "16:9", "from_content_prompt": true }
```

`from_content_prompt=true` 时不必填 `prompt`：worker 先同步执行一个内嵌根任务 `operation=image_prompt`（模板 `sys_image_prompt`，Mock 返回固定英文提示词）写入 `media_assets.prompt`，再提交图片。响应 `{"asset_ids": [1], "task_ids": [10]}`；轮询：

```http
GET /admin/media/assets/1/task
GET /admin/media/assets/1
```

状态依次 `pending → submitted → generating`（`progress` 递增）`→ downloading → ready`，Mock 下约 30~40 秒（按 `media_config.image.poll_intervals_seconds=[5, 10, 15, 30]` 的前三档轮询，第 3 次轮询返回 `succeeded`，累计 30 秒，再加本地复制）。`ready` 后 `url` 形如 `http://127.0.0.1:8100/media/media/images/2026/10/<hash>.png`（`storage_key=media/images/2026/10/<hash>.png`），浏览器可直接打开；`width/height` 由 `storage.probe_image_size` 从 PNG 头解析。本例带 `content_id=1` 且 `usage_type=cover`，不需要再调 `attach`：资产创建即写 `media_assets.content_id=1` / `usage_type=cover`，转存成功进入 `ready` 时由 `media_service.on_asset_ready` 自动把 `contents.cover_asset_id` 指向它（只对 `kind=image` 生效；内容原有封面时，旧封面资产改为 `inline`、仍保持绑定）。核对：`GET /admin/media/assets/1` 的 `references.cover_of=1`（以本素材为封面的内容）、`references.bound_content_id=1`，`GET /admin/contents/1` 的 `cover_asset_id=1`，编辑器封面区显示该图。`attach`（`POST /admin/contents/{id}/assets/{asset_id}/attach` `{"usage_type": "cover", "sort": 0}`，即素材面板「设为封面」）只用于把**另一张已有的** `ready` 图片（例如 `standalone` 生成或上传的素材）设为封面，本步无需执行。

图生图：先 `POST /admin/uploads/image`（multipart `file`）得到 `{asset_id, url, public:false}`，把 `url` 放进 `reference_image_urls`；Mock 模式放行非公网 URL，`public=false` 只在真实模式下有意义。

**第 9 步：生成视频**

页面：「媒体 → 视频生成」，用途选 `standalone`，不选关联内容。

```http
POST /admin/media/videos/generate
```

```json
{ "project_id": 1, "usage_type": "standalone", "prompt": "智能门锁开锁过程的 5 秒产品展示镜头", "duration": 5, "resolution": "720p", "aspect_ratio": "16:9", "generate_audio": false }
```

`standalone` 的 `content_id` 必须为空（带上返回 400）；要把视频插入文章正文，改用 `"usage_type": "inline"` 并带 `"content_id": 1`（视频不能作封面，传 `cover` 返回 400）。两种写法都只产生 1 次视频提交尝试行，不影响第 13 步的统计。Mock 需 5 次轮询才 `succeeded`（`media_config.video.poll_intervals_seconds=[15, 30, 60]`，之后固定 60s，即提交后第 15 / 45 / 105 / 165 / 225 秒各轮询一次），约 4 分钟后资产 `ready`，`url` 指向转存后的 `placeholder.mp4`；页面的长任务进度条（`TaskProgress.vue`）期间显示 `progress`。视频日上限 `media_config.daily_limits.videos=20`（`limit:videos:{date}`），超限返回 4291。

**第 10 步：回填发布链接**

内容 `approved` 后才能回填。页面：内容编辑器「链接面板」或「发布与监控 → 回填链接 → 回填」。

```http
POST /admin/links
```

```json
{ "content_id": 1, "url": "https://example.com/", "publish_account": "aicreat 官方", "published_at": "2026-09-01T02:00:00Z", "note": "本地冒烟" }
```

说明：

- 删除检测是**真实抓取**，请填一个开发机能访问的公网 URL；`https://example.com/` 稳定返回 200 且可通过 SSRF 校验，是最省事的冒烟目标（返回 404 的删除与告警分支在第 12 步用第二条链接验证）。平台识别可单独验证：`POST /admin/platforms/detect` `{"url":"https://zhuanlan.zhihu.com/p/123456789"}` → `{"platform_id": 1, "code": "zhihu"}`；未命中任何 `url_patterns_json` 的 URL 归入 `website`。
- `published_at` 故意填 **≥ 31 天以前**（示例 `2026-09-01` 距 2026-10-06 为 35 天）：收录检测排程以 `published_at` 为基准，`schedule_days=[1,3,7,14,30]` 全部已过期时首个到期引擎的 `due = now`，晚回填直接进入已过期的轮次并立即到期（`next_index_check_at=now`，不补跑过期轮次），这样 `monitor_worker` 的下一次扫描（≤ `index_check.scan_interval_seconds=300`）就会执行一轮 `scheduled` 检测并把 `index_checks_done` 加 1——报表 `seo_index_rate` / `geo_cite_rate` 的分母只统计 `index_checks_done >= 1` 的链接，手动检测不计入。填当前时间也能跑通其它步骤，只是这两个比率在第 1 天的排程执行前保持 `null`。`published_at` 早于内容 `created_at` 是允许的（用于登记历史文章或补录）：回填校验只要求它不晚于当前时间 + 5 分钟、不早于当前时间 − 3650 天（防止年份笔误），省略时取当前时间（[11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §4.1），所以给当天生成的内容填 35 天前的时间可以正常回填。
- 响应 `{"link": {...,"alive_status":"pending","platform_id":7}, "queued": true}`；同事务内内容 `approved → published`、`contents.link_count=1`、`first_published_at` 重算；基线检测经 `link_service.enqueue_check(link, "baseline", admin_id)` 入 `queue:link_checks`。同一 URL 再次回填返回 409 并带已存在链接 ID。

几秒后 `GET /admin/links/1`：`alive_status=alive`、`baseline_title="Example Domain"`、`baseline_simhash` 非空、`last_http_status=200`、`next_check_at` = 7 天后（`published_at` 距今 > 7 天按 `regular_interval_days=7`；≤ 7 天的新链接按 `initial_interval_hours=24` 每日一次）。无外网时结果为 `unknown`（`matched_rule=network_error`），未达 `unknown_confirm_count=3` 前 `alive_status` 保持 `pending`，不影响后续步骤。

**第 11 步：手动触发检测**

页面：链接列表行操作「立即检测」「收录检测」，或链接详情页。

```http
POST /admin/links/1/check                   # 删除检测，LPUSH 插队，check_type=manual
POST /admin/links/1/index-check             # SEO/GEO 收录检测
```

```json
{ "kinds": ["seo", "geo"] }
```

两者都返回 `{"queued": true}`；已在队列时返回 `{"queued": false, "reason": "already_queued"}`（去重标记 `queued:link_check:{link_id}` / `queued:index_check:{link_id}:{kind}`，消费完成即 `DEL`，最长 3600s 自过期），收录检测超出 `index_check.daily_limit=2000` 时返回 `{"queued": false, "reason": "daily_limit"}`，每管理员手动收录检测频控固定 `30/hour`（`rate:index_check_manual:{admin_id}`）。第 10 步 `published_at` 填在 31 天前时，`scheduled` 轮次会在回填后 ≤ 300s 自动入队并执行：若手动触发恰好落在该轮次尚在队列中的窗口，收录检测返回 `already_queued`，等约 1 分钟后再点一次即可。`monitor_worker` 消费后：

- `GET /admin/links/1/checks` 出现两条 `link_checks`：`check_type=baseline`（`result_status=alive`、`matched_rule=ok`）与 `check_type=manual`（手动检测不改 `next_check_at`，除非 `applied_status` 发生变化）。
- `GET /admin/links/1/index-checks`：每轮 8 条 `index_checks`——SEO `baidu` / `bing`（`provider=zhiqi_web_search`）与 GEO 6 个引擎（`provider=zhiqi_model`），`model=mock-text`、`request_id=mock-…`、`evidence.source="mock"`（列 `evidence_json`）。`published_at` 填在 31 天前时共 16 条（`check_type=scheduled` 8 条 + `manual` 8 条），填当前时间时只有 `manual` 8 条。
- `GET /admin/links/1` 的 `seo_status` / `geo_status` 按引擎给出 `{"status":"indexed","checked_at":"…","first_indexed_at":"…","check_count":1}`（`check_count` 只计 `scheduled`，手动不计）；任一引擎命中即 `seo_indexed_any=1` / `geo_cited_any=1` 并写 `first_indexed_at` / `first_cited_at`。Mock 单引擎命中率固定 70%：SEO 两个引擎同轮全部未命中的概率为 0.3² = 9%，GEO 六个引擎全部未命中为 0.3⁶ ≈ 0.07%。若 `seo_indexed_any` 仍为 0，再手动触发一次即可（手动检测同样回写 `seo_status_json` / `seo_indexed_any`，且 `not_indexed → indexed` 允许往返，收录状态机见 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md)），不影响后续步骤。
- 每个引擎调用对应一个同步执行的根任务 + 尝试行（`capability=seo_check` / `geo_check`、`operation=seo_check` / `geo_check`、`target_type=publish_link`、`target_id=1`；`scheduled` 轮次 `trigger_type=worker`，手动触发 `trigger_type=user` 且 `created_by` = 触发人），在「AI 网关 → AI 任务」可见。这类根任务不可 `retry` / `cancel`：`POST /admin/ai/tasks/{id}/retry` 返回 409 并在 `data.hint` 提示改用 `POST /admin/links/{link_id}/index-check`。

批量触发走「发布与监控 → 删除检测 / 收录检测」页的「批量触发」：`POST /admin/monitoring/link-checks/run` / `POST /admin/monitoring/index-checks/run`（`{"kinds":["seo","geo"],"only_due":false,"link_ids":[1]}`），返回 `{enqueued, skipped}`。`GET /admin/monitoring/overview` 显示两类队列的 `due / queued / today / last_run_at` 与 `monitor_worker` 副本心跳。

**第 12 步：删除检测与告警**

第 10 步的 `https://example.com/` 稳定返回 200，只能走到 `alive`。删除检测的状态流转（`suspected_deleted → deleted`）与告警（`link_deleted` 进入告警中心、确认、解决）用第二条链接验证：对同一内容再回填一个稳定返回 404 的公网 URL。页面操作同第 10 步，内容已是 `published` 时仍可继续回填。

```http
POST /admin/links
```

```json
{ "content_id": 1, "url": "https://httpbin.org/status/404", "publish_account": "aicreat 官方", "note": "删除检测冒烟" }
```

- 响应 `{"link": {...,"id":2,"alive_status":"pending","platform_id":7}, "queued": true}`：`httpbin.org` 不命中任何平台的 `url_patterns_json`，同样归入 `website`；内容保持 `published`，`contents.link_count=2`。
- `published_at` 省略即取当前时间：收录检测的首轮排程在 1 天后（`schedule_days` 第 1 天），链接进入 `deleted` 后 `next_index_check_at` 置 `NULL`（此时手动 `index-check` 也返回 409），所以这一步不产生 `seo_check` / `geo_check` 调用，第 13 步的 `ai_calls` 仍为 30。
- 任何稳定返回 404 / 410 / 451 的公网 URL 都可以替代；httpbin 偶发 5xx 或开发机无外网时结果为 `unknown`（`matched_rule=network_error`），连续 3 次之前不改变链接状态（基线时停在 `pending`），稍后再手动检测即可。

几秒后基线检测完成，`GET /admin/links/2`：`alive_status=suspected_deleted`、`last_http_status=404`、`consecutive_suspected=1`、`next_check_at` 约为 6 小时后（`abnormal_backoff_hours[0]`）；`GET /admin/links/2/checks` 的基线记录为 `check_type=baseline`、`result_status=suspected_deleted`、`matched_rule=http_404`。基线抓到 404 只记疑似删除、等待确认，此时还没有告警（判定规则、确认阈值与状态机见 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §6.3~§6.5）。在链接列表对该行点「立即检测」：

```http
POST /admin/links/2/check
```

非基线检测遇到 404 直接判定 `deleted`：`GET /admin/links/2/checks` 新增一条 `check_type=manual`、`result_status=deleted`、`previous_status=suspected_deleted`、`applied_status=deleted`、`matched_rule=http_404` 的记录；链接 `alive_status=deleted`、`alive_changed_at` 更新、`consecutive_suspected` 清零。状态发生了变化，所以 `next_check_at` 按删除复检规则重算为 7 天后（`deleted_recheck_days=7`，复检到 `alive_changed_at` 后 30 天为止）。告警在同一事务写入，「发布与监控 → 告警中心」可见：

```http
GET /admin/alerts?alert_type=link_deleted&status=open
GET /admin/alerts/summary
```

- 列表出现 1 条告警：`alert_type=link_deleted`、`severity=warning`、`status=open`、`project_id=1`、`target_type=publish_link`、`target_id=2`（`dedupe_key=link_deleted:publish_link:2`），`payload.matched_rule=http_404`。
- `summary` 的 `open.warning` 比回填前 +1（全新环境为 1）、`today_opened` +1；顶栏铃铛（`AlertBadge.vue` 每 60s 轮询 `GET /admin/alerts/summary`）在下一次轮询后计数 +1。

在告警中心对该告警依次点「确认」「解决」（权限 `monitoring.alerts.handle`，超管具备）：

```http
POST /admin/alerts/{alert_id}/acknowledge     # open → acknowledged
POST /admin/alerts/{alert_id}/resolve         # acknowledged → resolved
```

```json
{ "note": "冒烟验证：目标 URL 固定返回 404" }
```

确认后该告警在 `summary` 中从 `open.warning` 移到 `acknowledged.warning`；解决后两处都不再计入、`today_resolved` +1，告警记录的 `resolved_by` 为当前管理员、`resolution_note` 为填写的备注。人工解决只结束告警，不改变链接状态：链接仍为 `deleted` 并按上面的排程复检。只有链接恢复（`deleted → alive` / `changed`）才会产生 `link_restored`（`info`，创建即 `resolved`）并自动解决 `link_deleted`（`resolved_by=null`、`resolution_note="auto"`）；公网 404 地址无法人为恢复，这一分支由后端测试覆盖（[11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §16）。第 13 步的链接与告警核对值包含这条链接；不需要时 `DELETE /admin/links/2` 会级联删除它的检测记录，并同事务解决该链接未结束的告警。

**第 13 步：查看报表**

页面：「控制台 → 总览」（`Dashboard.vue`，只调 `GET /admin/stats/overview`）与「报表」（`stats/Reports.vue`）。响应结构以 [12-dashboard-reports](./12-dashboard-reports.md) §9.1 为准，分 `meta` / `kpis` / `compare` / `breakdowns` / `series` 五块。下例为完成前 12 步、对账已完成、今日 `daily_stats` 行在第 12 步之后重新聚合过的取值；tokens、额度、成本与耗时是 Mock 估算的示例值，各引擎命中随机：

```http
GET /admin/stats/overview?project_id=1&range=30d
```

```json
{
  "meta": {
    "range": "30d", "start_date": "2026-09-07", "end_date": "2026-10-06", "timezone": "Asia/Shanghai",
    "project_id": 1, "today_source": "daily_stats", "snapshot_date": "2026-10-06",
    "computed_at": "2026-10-06T08:40:00Z", "cached": false, "warnings": []
  },
  "kpis": {
    "keywords_total": 20, "keywords_created": 20, "keywords_adopted": 3, "keyword_adopt_rate": 0.15,
    "titles_total": 10, "titles_created": 10, "titles_adopted": 1,
    "contents_total": 1, "contents_created": 1, "contents_approved": 1, "contents_published": 0,
    "links_total": 2, "links_backfilled": 2, "links_alive": 1, "links_deleted": 1,
    "link_alive_rate": 0.5, "link_deleted_rate": 0.5, "links_checked": 4,
    "seo_index_rate": 1.0, "geo_cite_rate": 1.0, "seo_newly_indexed": 1, "geo_newly_cited": 1,
    "time_to_index_hours_avg": null, "time_to_index_hours_p50": null,
    "ai_calls": 30, "ai_success_rate": 1.0, "ai_avg_duration_ms": 12, "task_success_rate": 1.0,
    "prompt_tokens": 5532, "completion_tokens": 4344, "tokens_total": 9876,
    "quota_estimated": 9876, "quota_actual": 9876, "quota_reconciled_rate": 1.0,
    "cost_cny": 0.142214, "cost_cny_per_content": 0.097920,
    "images_generated": 1, "videos_generated": 1, "media_failed": 0, "media_success_rate": 1.0,
    "alerts_opened": 1, "alerts_resolved": 1
  },
  "compare": {
    "ai_calls": { "previous": 0, "delta": 30, "delta_rate": null },
    "cost_cny": { "previous": 0, "delta": 0.142214, "delta_rate": null },
    "links_backfilled": { "previous": 0, "delta": 2, "delta_rate": null }
  },
  "breakdowns": {
    "keywords_by_status": { "candidate": 17, "adopted": 3, "discarded": 0 },
    "titles_by_status": { "candidate": 9, "adopted": 1, "discarded": 0 },
    "contents_by_status": { "draft": 0, "generating": 0, "ready": 0, "reviewing": 0, "approved": 0, "rejected": 0, "published": 1, "archived": 0 },
    "links_by_status": { "pending": 0, "alive": 1, "changed": 0, "suspected_deleted": 0, "deleted": 1, "unknown": 0 },
    "alerts_open": { "info": 0, "warning": 0, "critical": 0 },
    "cost_by_capability": [
      { "capability": "content", "ai_calls": 8, "ai_success_rate": 1.0, "tokens_total": 5600, "quota_estimated": 5600, "quota_actual": 5600, "cost_cny": 0.080640, "share": 0.5670, "task_success_rate": 1.0, "task_avg_duration_ms": 910 },
      { "capability": "geo_check", "ai_calls": 12, "ai_success_rate": 1.0, "tokens_total": 1700, "quota_estimated": 1700, "quota_actual": 1700, "cost_cny": 0.024480, "share": 0.1721, "task_success_rate": 1.0, "task_avg_duration_ms": 35 },
      { "capability": "rewrite", "ai_calls": 1, "ai_success_rate": 1.0, "tokens_total": 1200, "quota_estimated": 1200, "quota_actual": 1200, "cost_cny": 0.017280, "share": 0.1215, "task_success_rate": 1.0, "task_avg_duration_ms": 700 },
      { "capability": "seo_check", "ai_calls": 4, "ai_success_rate": 1.0, "tokens_total": 560, "quota_estimated": 560, "quota_actual": 560, "cost_cny": 0.008064, "share": 0.0567, "task_success_rate": 1.0, "task_avg_duration_ms": 32 },
      { "capability": "title", "ai_calls": 2, "ai_success_rate": 1.0, "tokens_total": 456, "quota_estimated": 456, "quota_actual": 456, "cost_cny": 0.006566, "share": 0.0462, "task_success_rate": 1.0, "task_avg_duration_ms": 580 },
      { "capability": "keyword", "ai_calls": 1, "ai_success_rate": 1.0, "tokens_total": 360, "quota_estimated": 360, "quota_actual": 360, "cost_cny": 0.005184, "share": 0.0365, "task_success_rate": 1.0, "task_avg_duration_ms": 640 },
      { "capability": "image", "ai_calls": 1, "ai_success_rate": 1.0, "tokens_total": 0, "quota_estimated": 0, "quota_actual": 0, "cost_cny": 0.000000, "share": 0.0, "task_success_rate": 1.0, "task_avg_duration_ms": 31200 },
      { "capability": "video", "ai_calls": 1, "ai_success_rate": 1.0, "tokens_total": 0, "quota_estimated": 0, "quota_actual": 0, "cost_cny": 0.000000, "share": 0.0, "task_success_rate": 1.0, "task_avg_duration_ms": 226400 }
    ],
    "cost_by_model": [
      { "model": "mock-text", "ai_calls": 28, "ai_success_rate": 1.0, "tokens_total": 9876, "quota_estimated": 9876, "quota_actual": 9876, "cost_cny": 0.142214, "share": 1.0 },
      { "model": "mock-image", "ai_calls": 1, "ai_success_rate": 1.0, "tokens_total": 0, "quota_estimated": 0, "quota_actual": 0, "cost_cny": 0.000000, "share": 0.0 },
      { "model": "mock-video", "ai_calls": 1, "ai_success_rate": 1.0, "tokens_total": 0, "quota_estimated": 0, "quota_actual": 0, "cost_cny": 0.000000, "share": 0.0 }
    ],
    "seo_index_rate_by_engine": {
      "baidu": { "rate": 0.5, "hit": 1, "total": 2 }, "bing": { "rate": 0.0, "hit": 0, "total": 2 }, "google": { "rate": null, "hit": null, "total": 2 }
    },
    "geo_cite_rate_by_engine": {
      "baidu_ai": { "rate": 0.5, "hit": 1, "total": 2 }, "doubao": { "rate": 0.5, "hit": 1, "total": 2 }, "kimi": { "rate": 0.0, "hit": 0, "total": 2 },
      "deepseek": { "rate": 0.5, "hit": 1, "total": 2 }, "perplexity": { "rate": 0.5, "hit": 1, "total": 2 }, "chatgpt": { "rate": 0.0, "hit": 0, "total": 2 }
    }
  },
  "series": {
    "dates": [
      "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16",
      "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26",
      "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"
    ],
    "ai_calls":          [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 30],
    "cost_cny":          [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.142214],
    "links_backfilled":  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2],
    "seo_newly_indexed": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
  }
}
```

核对要点（字段路径相对响应 `data`）：

- **AI 调用**：`kpis.ai_calls` 按**尝试行**计数（`status ∈ succeeded/failed` 且 `trigger_type != health_probe`），本流程为关键词 1 + 标题 2 + 内容 7（大纲 1 + 正文 4 段 + `seo_meta` 1 + `faq` 1）+ 重写 1 + 图片提示词 1 + 图片提交 1 + 视频提交 1 + 收录检测 16（`scheduled` 8 + `manual` 8）= 30；正文段数等于 Mock 大纲的 H2 数，实际值以「AI 任务」页 `row_kind=attempt` 的行数为准。第 2 步的一键测试与 worker 每 600s 的自动探测（均为 `trigger_type=health_probe`）不计入；第 12 步的删除检测是真实抓取、不经 zhiqiapi，也不计入。`breakdowns.cost_by_capability` 按能力拆分这 30 行：图片提示词按 `capability=content` 归入 `content`（7 + 1 = 8），GEO 6 引擎 × 2 轮 = `geo_check` 12，SEO 2 引擎 × 2 轮 = `seo_check` 4。`breakdowns.cost_by_model` 按模型拆分同样的 30 行：`mock-text` 28（关键词、标题、内容、重写、图片提示词与收录检测都走 `mock-text`）、`mock-image` 1、`mock-video` 1。`kpis.task_success_rate` 与 `cost_by_capability` 各行的 `task_success_rate` / `task_avg_duration_ms` 按**根任务**统计（`root_task_id IS NULL` 且 `trigger_type != health_probe`，一个业务单元计一次，分段生成的多次尝试不重复计数）：本流程 24 个根任务（关键词 1、标题 2、内容 1 + 图片提示词 1、重写 1、图片 1、视频 1、收录检测 16，每个引擎的每次检测各一个同步根任务）全部 `succeeded`，因此均为 1.0。`image` / `video` 行的 `task_avg_duration_ms` 为根任务提交 → 上游完成的全程（含轮询、不含转存），约等于第 8 / 9 步的轮询累计 30 秒 / 225 秒加调度延迟，也是总览「媒体成功率」卡片副值中的平均任务耗时；文本各行为 Mock 估算的示例值。
- **额度与成本**：Mock 价格 `model_ratio=1`、`completion_ratio=1`，所以 `kpis.quota_estimated = kpis.tokens_total`，`cost_by_capability` / `cost_by_model` 每行的 `quota_estimated` 也等于该行 `tokens_total`（图片 / 视频提交的尝试行 tokens 为 0，`image` / `video` 与 `mock-image` / `mock-video` 行额度与费用均为 0），`kpis.cost_cny = quota / 500000 × 7.2`（`ai_routing_config.pricing`），`kpis.cost_cny_per_content` = `content` 与 `rewrite` 两行费用之和 / `kpis.contents_created`。`kpis.quota_actual`（及各行 `quota_actual`）与 `kpis.quota_reconciled_rate` 要等对账（worker 每 300s 自动执行，或第 14 步手动触发）回填并重算当日后才有值；Mock 伪日志的 `quota` 即本地估算值，所以对账后 `quota_actual` 与 `quota_estimated` 相等。
- **内容**：当前状态看 `breakdowns.contents_by_status.published=1`（`kpis.contents_total=1`）。`kpis.contents_published=0` 属预期：它按 `contents.first_published_at` 归属统计日，该值等于最早链接的 `published_at`，即第 10 步的 2026-09-01，落在 30d 窗口（2026-09-07 起）之外。
- **链接**：`kpis.links_total=2`、`kpis.links_alive=1`、`kpis.links_deleted=1`（总览中均为当前值，`links_alive` / `links_deleted` 按 `alive_status` 计数）、`kpis.link_alive_rate=0.5` 与 `kpis.link_deleted_rate=0.5`（分母为非 `pending` 的 2 条链接），`breakdowns.links_by_status` 中 `alive=1`、`deleted=1`；流量类 `kpis.links_backfilled=2`、`kpis.links_checked=4`（两条链接各一次基线 + 一次手动检测）。
- **收录**：`kpis.seo_index_rate` / `kpis.geo_cite_rate` 的分母只含 `index_checks_done >= 1` 的链接（SEO 另要求 `published_at` 距今 ≥ 1 天），第 12 步的 404 链接不计入，比率仍由第一条链接决定；完成一轮 `scheduled` 收录检测前为 `null`（见第 10 步）。`breakdowns.seo_index_rate_by_engine` / `geo_cite_rate_by_engine` 是另一种口径：分母 `total` 为日终链接总数（含 404 链接），未启用的 `google` 返回 `null`，Mock 下各引擎 `hit` 随机（两种口径的区别见 [12-dashboard-reports](./12-dashboard-reports.md) §3.3）。`kpis.time_to_index_hours_avg` / `time_to_index_hours_p50` 为 `null` 属预期：第 10 步的链接 `published_at=2026-09-01T02:00:00Z`、今日才回填，补录延迟（`publish_links.created_at − published_at`）约 35 天，超过 72 小时（`stats_service.MAX_BACKFILL_DELAY_HOURS`）即视为历史补录——回填前是否、何时已被收录无从观测，`first_indexed_at` 只是本系统首次检测到命中的时间，所以该链接不计入这两项、`daily_stats.index_hours_sum` / `index_hours_links` 与榜单 `fastest_indexed`（报表趋势的 `time_to_index_hours_avg` = Σ `index_hours_sum` / Σ `index_hours_links`，分母为 0 时为 `null`），但仍计入收录率与 `kpis.seo_newly_indexed=1` 等其它指标（`seo_newly_indexed` 只计新收录数量、不作耗时分母；口径见 [12-dashboard-reports](./12-dashboard-reports.md) §3.2）。第 12 步的 404 链接从未收录，本流程没有可计入的样本：总览「SEO 收录率」卡片的副值「平均收录耗时」显示 `--`，报表页「明细榜」的 `fastest_indexed`（`/admin/stats/rankings`）返回空数组。要验证收录耗时，回填时让 `published_at` 落在回填前 72 小时以内（如省略即取当前时间），代价是两个比率要等第 1 天的排程执行后才有值（见第 10 步）。
- **告警**：`kpis.alerts_opened=1`、`kpis.alerts_resolved=1` 即第 12 步的 `link_deleted`（分别按 `first_triggered_at` / `resolved_at` 归属今日）；该告警已解决，`breakdowns.alerts_open` 三档均为 0（解决前为 `warning=1`）。
- **序列与今日数据**：`series.dates` 为 2026-09-07 ~ 2026-10-06 共 30 个点（点数 = range 天数，最少 7 个），四条序列缺行填 0，本流程只有最后一天非 0；上一周期没有数据，`compare` 的 `delta_rate` 均为 `null`。当前值类指标（`*_total`、`*_by_status`、`links_alive` / `links_deleted`、各比率、`alerts_open`）实时查询业务表；流量类指标与 `series` 的「今日」优先取今日 `daily_stats` 行（`meta.today_source=daily_stats`，`aggregate_today()` 每 600s 刷新，最多滞后 10 分钟，且不与 Redis 叠加），该行尚不存在时才取 `stats:rt:{date}:{project_id}`（`today_source=realtime`，不含 `quota_actual` 等对账列）。刚完成的操作若还没计入，等下一次 `aggregate_today()` 或按下方重算今日即可；总览另有 `stats_config.overview_cache_seconds=60` 秒缓存（命中时 `meta.cached=true`）。

要立即重算（`end_date` 可以是今日）：

```http
POST /admin/stats/recompute
```

```json
{ "start_date": "2026-10-01", "end_date": "2026-10-06" }
```

跨度 ≤ 7 天在 API 进程内逐日执行并返回 `{days, rows_upserted, skipped[], duration_ms}`；> 7 天写 `queue:stats_recompute` 由 `monitor_worker` 异步执行（返回 202）。报表页的趋势 / 分解 / 榜单分别对应 `GET /admin/stats/trends`、`/admin/stats/breakdown`、`/admin/stats/rankings`，「导出 CSV」= `GET /admin/stats/export?report=trends|breakdown|rankings`。

**第 14 步：用量对账与任务明细**

「AI 网关 → 用量对账」点「立即对账」：

```http
POST /admin/ai/usage/reconcile
```

```json
{ "pulled": 31, "new": 31, "matched": 31, "unmatched": 0, "window_overflow": false, "request_ids": ["mock-…"] }
```

Mock 下 `mock_token_logs()` 返回 `mock:usage_logs` 中的伪日志（每次文本/图片/视频调用各 `LPUSH` 一条 `type=2`，探测调用同样计入），按 `request_id` 匹配尝试行并回填 `quota_actual` / `reconciled_at` / `cost_cny`。核对规则是 `pulled >= 31` 且 `unmatched = 0`：比第 13 步 `ai_calls=30` 多出的部分全部是 `trigger_type=health_probe` 的探测尝试行——第 2 步一键测试 1 行，加上 worker 每 `health.probe_interval_seconds=600` 秒自动探测时每个 `(capability, model)` 组合各 1 行（Mock 下为 `keyword` / `title` / `content` / `rewrite` / `geo_check` / `seo_check` × `mock-text` 共 6 行；`image` / `video` 路由默认 `probe_media=false`，只核对目录、不发请求、不产生伪日志）。探测行参与对账但不计入 `ai_calls`；示例中的 31 对应「一键测试 1 次、自动探测尚未执行」的情形，流程耗时超过 10 分钟时 `pulled` 会随自动探测轮次增加。worker 每 `usage.reconcile_interval_seconds=300` 秒自动执行同样的对账，手动按钮只是提前触发（两者互斥于 `lock:worker:reconcile`）：若周期对账已先执行，本次返回的 `new` / `matched` 为 0（条目按 `entry_hash` 幂等、不重复入库），属正常。最终以 `GET /admin/ai/usage/summary?group_by=model` 的 `reconciled_rate=1.0` 为准（分子分母同样排除 `health_probe`）。「AI 任务」页切换 `row_kind=attempt` 可逐条查看 `request_id`、协议、tokens、额度、成本与错误分类；「操作日志」页记录了以上全部写操作（`admin_operation_logs`）。

**第 15 步：数据隔离验证（用户系统）**

验证「普通用户只能看到自己的数据、总后台看全部」（规则见 [13-user-data-scope](./13-user-data-scope.md)）。用 `admin` 在「系统 → 用户管理」新建两个 `operator` 组用户（数据范围默认「仅本人」）：

```http
POST /admin/admins
```

```json
{ "username": "user_a", "display_name": "用户A", "password": "UserA2026x", "group_id": 2, "is_active": true }
```

同样创建 `user_b`（`group_id` 取 `GET /admin/admin-groups` 中 `code=operator` 的组 ID）。然后：

1. 分别以 `user_a`、`user_b` 登录：`GET /admin/auth/me` 返回 `"data_scope": "own"`；顶栏没有用户视角切换器、显示「我的数据」；「项目」列表为空——前面步骤的示例项目负责人是 `admin`，对二人不可见。
2. `user_a` 创建项目「A 的项目」并生成一批关键词（同第 3 步）；`user_b` 创建同名项目「A 的项目」也能成功（项目名按负责人唯一）。
3. `user_b` 访问 `user_a` 的项目与关键词：`GET /admin/projects/{A 的项目 ID}`、`GET /admin/keywords/{A 的关键词 ID}` 均返回 `{ "code": 404, "message": "对象不存在", "data": null }`；`GET /admin/keywords?project_id={A 的项目 ID}` 返回空列表。
4. `user_b` 回填第 10 步已回填过的 URL（需先有自己的 `approved` 内容）：返回 `{ "code": 409, "message": "该链接已由其他用户回填", "data": { "existing_id": null, "reason": "owned_by_other" } }`。
5. 两人的「控制台」只统计各自项目：`GET /admin/stats/overview?range=7d` 的 `meta.scope="owner"`，`user_a` 的 `kpis.keywords_created` 等于其生成的数量，不含示例项目的数据。
6. 用 `admin` 登录（`data_scope=all`）：顶栏出现用户视角切换器（`GET /admin/projects/owner-options` 列出 `admin` / `user_a` / `user_b`）；选「用户A」后所有列表与控制台只显示 `user_a` 的数据（请求自动附加 `owner_id`），与 `user_a` 本人所见一致；报表「分解」Tab 选维度「用户」（`GET /admin/stats/breakdown?dimension=owner&metric=keywords_created&start=…&end=…`）按用户列出关键词数。
7. `admin` 把「A 的项目」的负责人改为 `user_b`（`PUT /admin/projects/{id}` `{"owner_id": <user_b 的 ID>}`）：因 `user_b` 已有同名项目返回 409；先把 `user_b` 的同名项目改名再转移即成功，之后 `user_a` 访问该项目返回 404，`user_b` 可见，项目下的关键词与统计随之转移。

### 预期耗时（Mock）

| 步骤 | 典型耗时 | 决定因素 |
| --- | --- | --- |
| 关键词 / 标题 / 内容生成 | 2~10 秒 | worker 主循环 2s + `mock_chat` 即时返回；内容分段数越多越久 |
| 图片 | 30~40 秒 | 轮询间隔 `[5, 10, 15, 30]` 的前三档（第 3 次轮询 `succeeded`，累计 30 秒）+ 本地复制 |
| 视频 | 约 4 分钟 | 轮询间隔 `[15, 30, 60]`、之后固定 60s（第 5 次轮询 `succeeded`，累计 225 秒）+ 本地复制 |
| 基线删除检测 | 5~20 秒 | monitor_worker 5s 轮询 + 真实抓取（`MONITOR_FETCH_TIMEOUT_SECONDS=15`） |
| 删除检测与告警（第 12 步） | 10~40 秒 | 基线与手动检测各一次真实抓取；`link_deleted` 告警与 `deleted` 同事务写入，铃铛最长 60s 后刷新 |
| 手动收录检测 | 5~30 秒 | 8 个引擎串行调用 Mock |
| 首轮 `scheduled` 收录检测 | ≤ 5 分钟 | `index_check.scan_interval_seconds=300` |
| 报表「今日」落库 | ≤ 10 分钟 | `stats_config.intraday_refresh_seconds=600` |

### 自动冒烟脚本

```bash
pnpm smoke:api        # Windows：= cd server && .venv\Scripts\python.exe scripts\integration_smoke.py
                      # macOS/Linux：cd server && python scripts/integration_smoke.py
```

脚本接受三个命令行参数：

| 参数 | 缺省值 | 说明 |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8100` | API 根地址，只写 scheme + 主机 + 端口，不带 `/api/v1`；API 不在 8100 时（`dev-restart` 换过端口、在 compose 的 server 容器内运行）必须显式传入 |
| `--username` | 环境变量 `SEED_ADMIN_USERNAME` | 登录账号；本机读 `server/.env`，容器内读根 `.env` 经 `env_file` 注入的同名环境变量 |
| `--password` | 环境变量 `SEED_ADMIN_PASSWORD` | 登录密码，来源同上；超管改过密码后（预发按 [05-deployment](./05-deployment.md) §7.2 在登录后立即改密，再跑冒烟）缺省值已无法登录，必须用该参数传入新密码——重放 `seeds/seed.py` 不会把已存在账号的密码改回 `SEED_ADMIN_PASSWORD` |

常见调用：

```bash
# 本机（在 server/ 下执行）：dev-restart 把 API 换到其它端口后，传入脚本输出的 API 地址
.venv\Scripts\python.exe scripts\integration_smoke.py --base-url http://127.0.0.1:8101    # Windows
python scripts/integration_smoke.py --base-url http://127.0.0.1:8101                     # macOS/Linux（已激活 venv）

# compose 环境（预发）：在 server 容器内运行；容器内 gunicorn 监听 8000，8100 上没有服务
docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000

# 超管已改密：用参数传入当前账号与新密码（本机运行同样追加 --username / --password）
docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000 --username admin --password '<新密码>'
```

`server/scripts/integration_smoke.py` 在 Mock 模式下顺序执行「登录 → 关键词 → 标题 → 内容 → 图片 → 回填 → 检测 → 报表」：检测步骤断言 SEO / GEO 结果非 `unknown`，并按第 12 步再回填一条返回 404 的公网 URL，断言基线 `suspected_deleted`、手动检测后 `deleted`、出现 `link_deleted` 告警且可确认、解决（恢复分支 `link_restored` 无法用公网 404 地址触发，不在脚本内，由后端测试覆盖，见第 12 步）；报表步骤断言 `seo_index_rate` / `geo_cite_rate` 非 `null` 且 > 0（Mock 单引擎命中率 70%：脚本在 `scheduled` 轮次完成后若 `seo_indexed_any` / `geo_cited_any` 仍为 0，再调用 `POST /admin/links/{id}/index-check` 复查，最多 3 次，仍全部未命中才判失败）。前置条件：API、`app.worker`、`app.monitor_worker` 三个进程都在运行（compose 下即 `server` / `worker` / `monitor-worker` 三个容器），且 `monitor_worker` 能访问公网；登录凭据有效（`--username` / `--password` 缺省取 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`，超管改密后须传入新密码，见上表）；回填步骤使用公网 URL 并把 `published_at` 置于 31 天前以触发 `scheduled` 轮次，因此整段最长约 8 分钟（含 3 次复查；删除检测分支只多两次抓取；视频不在脚本内，按第 9 步手工验证）。脚本任何一步失败即非零退出并打印最后一次响应与相关 `request_id`。

## 六、接入真实 zhiqiapi

### 1. 填写 Key 与默认模型

编辑 `server/.env`（容器部署改根 `.env`）：

```ini
ZHIQI_BASE_URL=https://zhiqiapi.com/v1
ZHIQI_API_KEY=sk-********                       # 只存在于环境变量，永不入库、不入日志、不入响应
ZHIQI_TEXT_DEFAULT_MODEL=<文本模型 ID>           # keyword / title / content / rewrite 四条路由的主模型初值
ZHIQI_TEXT_DEFAULT_PROTOCOL=openai_chat         # openai_chat / openai_responses / anthropic_messages
ZHIQI_IMAGE_DEFAULT_MODEL=<图片模型 ID>
ZHIQI_VIDEO_DEFAULT_MODEL=<视频模型 ID>
ZHIQI_GEO_DEFAULT_MODEL=<联网文本模型 ID>        # 仅用于 geo_check 路由的健康探测与默认 params
ZHIQI_SEO_DEFAULT_MODEL=<联网文本模型 ID>
PUBLIC_BASE_URL=https://aicreat.example.com     # 必须是浏览器与 zhiqiapi 都能访问的公网地址
```

模型 ID 以 `GET /v1/models` 返回的目录为准（zhiqiapi 官方文档），本文档不列具体型号。`ZHIQI_*_DEFAULT_MODEL` 只在 `ensure_default_routes` 建路由或把 Mock 路由（`primary_model` 以 `mock-` 开头）替换为真实模型时生效：

- 从 Mock 切换到真实模式时，启动过程会把 8 条全局路由中以 `mock-` 开头的主模型替换为对应环境变量；环境变量为空的路由被置为 `is_enabled=0` 并打启动告警日志，该能力的生成接口返回 5031，直到在「AI 网关 → 能力路由」填好模型并启用。
- 已经是真实模型的路由不会被环境变量覆盖，后续调整一律走后台。
- `PUBLIC_BASE_URL` 在真实模式下禁止填 `127.0.0.1` / `localhost` / compose 内部服务名：上传的参考图、参考视频以及图生图 / 图生视频的所有参考 URL 都要由 zhiqiapi 从公网拉取，否则接口返回 4222（`CODE_PUBLIC_URL_REQUIRED`）、上传接口返回 `public:false`；主机解析为非公网地址时 `GET /api/v1/health` 的 `warnings[]` 与启动日志会提示。对象存储方案（`STORAGE_MODE=oss` + `OSS_*`）见 [05-deployment](./05-deployment.md)。

改完后**重启 API、`app.worker`、`app.monitor_worker` 三个进程**（环境变量只在启动时读取），`GET /admin/auth/me` 与 `GET /api/v1/health` 的 `zhiqi_mode` 应变为 `live`。

### 2. 同步模型目录

worker 启动时会立即同步一次，之后每 `ai_routing_config.catalog.sync_interval_seconds=3600` 秒一次；也可在「AI 网关 → 模型目录」点「同步」：

```http
POST /admin/ai/models/sync
```

```json
{ "total": 180, "added": 180, "updated": 0, "unavailable": 3, "synced_at": "2026-10-06T09:00:00Z", "request_ids": { "models": "…", "pricing": "…" } }
```

`sync_models` 拉取 `GET /v1/models`（`id` / `owned_by` / `supported_endpoint_types`）与 `GET /api/pricing_new`（价格与分组），upsert `ai_models` 并由 `supported_endpoint_types` 推导 `modalities`（`text` / `image` / `video`）；未再出现的模型（包括 Mock 期间的 `mock-*`）置 `is_available=0`，7 天后默认从列表隐藏（`?include_hidden=1` 可见）。列表的「价格快照」列显示 `quota_type`（0 按量 / 1 按次）、`model_ratio`、`completion_ratio`，这是本地额度估算 `quota = (prompt_tokens + completion_tokens × completion_ratio) × model_ratio × group_ratio` 的依据。

### 3. 配置能力路由

「AI 网关 → 能力路由」（`ai/Routes.vue`）逐条编辑 8 条全局路由，模型下拉（`ModelSelect.vue`）按模态从 `GET /admin/ai/models/options?modality=text|image|video` 取可用模型：

| capability | protocol | params（默认调用参数） | 选型规则 |
| --- | --- | --- | --- |
| `keyword` / `title` / `content` / `rewrite` | `openai_chat` / `openai_responses` / `anthropic_messages` 三选一 | `{"temperature":0.7,"max_tokens":2048}`（`content` 路由示例用 4096） | 主模型 + ≥ 1 个备选（`fallback_models`）。候选须存在于 `ai_models` 且 `modalities` 含 `text`，否则 400（`data` 为校验错误列表）；`is_available=0` 的模型允许保存（上游模型可能临时下架又恢复），响应以 `warnings[]` 提示，运行期 `resolve_route` 跳过不可用候选；模型目录中该模型的 `supported_endpoint_types` 不含所选协议时，`protocol_for` 按 `openai → openai_chat`、`openai-response → openai_responses`、`anthropic → anthropic_messages` 顺序改用首个可用协议 |
| `image` | `image_async` | `{"resolution":"1080p","aspect_ratio":"16:9"}` | 优先选 `supported_endpoint_types` 含 `image-generation-async` 的模型。只要模型有任一图片端点，请求一律先走异步 `POST /v1/images/generations/async`（图生图的参考图随 `reference_image_urls` 一并提交），目录未列 `image-generation-async` 也不预选同步，模型目录缺少该模型条目时按 `preferred`（`image_async`）处理；仅当异步提交返回 `route_missing` / `model_unrouted` 且 `media_config.image.sync_fallback=true` 时，才在同一候选模型内回退同步（无参考图走 `image_sync`；有参考图且目录含 `image-edit` 或目录缺失时走 `image_edit`，否则走 `image_sync`） |
| `video` | `video` | `{"resolution":"720p","duration":5,"aspect_ratio":"16:9","generate_audio":false}` | 选含 `openai-video` 的模型 |
| `geo_check` / `seo_check` | 文本协议 | 同文本 | 选具备联网检索能力的模型；`geo_check` 路由只作健康探测对象与默认 `params` 来源，GEO 各引擎实际使用 `geo_engines.engines[].model`（下一步）；`seo_check` 路由是 `seo_providers.engines.<engine>.model` 为空时的候选链 |

```http
PUT /admin/ai/routes/3
```

```json
{ "protocol": "openai_chat", "primary_model": "<文本模型 ID>", "fallback_models": ["<备选模型 ID>"], "params": { "temperature": 0.7, "max_tokens": 4096 }, "timeout_seconds": 180, "max_attempts": 3, "is_enabled": 1, "note": "内容生成主路由" }
```

保存后清 `cache:routes:*`，下一次调用即生效。重试 / 熔断 / 回退 / 超时等全局参数在「系统配置 → AI 路由 Tab」（`PUT /admin/settings/ai_routing_config`）维护，修改后 worker 下一轮循环生效。项目级覆盖在项目详情页保存（`PUT /admin/projects/{id}/routes`，只写 `protocol` / `primary_model` / `fallback_models` / `params`）。

同时在「系统配置」页完成两项与检测相关的配置：

- **GEO 引擎**（`PUT /admin/settings/geo_engines`）：为每个要启用的引擎填入联网模型 ID 与协议（`chatgpt` 默认走 `openai_responses` 并经 `ai_routing_config.passthrough` 白名单透传 `extra.tools:[{"type":"web_search"}]`；该字段名与各引擎对应的联网模型 ID 以 zhiqiapi 官方文档和模型目录为准，模型无法表达的托管工具会被上游移除而不会伪造）。真实模式下 `model` 为空的引擎不可启用（接口返回 400）；Mock 期间被 seed 为 `enabled=true` 且 `model` 为空的引擎，切换到真实模式后请先填模型或改为 `enabled=false`，否则该引擎每次检测都记 `unknown`（`error_category=model_unrouted`，不发起 HTTP、`request_id=NULL`）。
- **SEO 提供器**（`PUT /admin/settings/seo_providers`）：默认 `baidu` / `bing` 走 `zhiqi_web_search`（可按引擎指定 `model` 作为 `model_override`，为空走 `seo_check` 路由候选链）；启用 `baidu_ai_search` / `bing_webmaster` / `google_search_console` 前先在 `.env` 配置 `SEO_BAIDU_AI_SEARCH_API_KEY` / `SEO_BING_WEBMASTER_API_KEY` + `SEO_BING_SITE_URL` / `SEO_GSC_CREDENTIALS_FILE` + `SEO_GSC_SITE_URL`，凭据为空时保存返回 400；后两者只对自有已验证站点的链接生效。

### 4. 健康探测

每条路由的「一键测试」对主模型与每个备选模型各发一次最小文本请求（`health.probe_text_prompt="ping"`、`health.probe_max_tokens=8`、单次超时固定 30s、不重试；每次探测记一行 `ai_tasks(trigger_type=health_probe, operation=route_probe)`）：

```http
POST /admin/ai/routes/3/test
```

```json
[
  { "model": "<文本模型 ID>", "status": "healthy", "latency_ms": 820, "request_id": "…", "error_category": null },
  { "model": "<备选模型 ID>", "status": "down", "latency_ms": 0, "request_id": "…", "error_category": "model_unrouted" }
]
```

`status` ∈ `healthy`（成功且延迟 < `health.degraded_latency_ms=15000`）/ `degraded` / `down`；`image` / `video` 路由默认只检查模型是否在目录中，传 `{"probe_media": true}` 才真实提交（会计费）。单模型探测用 `POST /admin/ai/health/probe` `{"capability":"content","model":"…","protocol":"openai_chat"}`。worker 每 `health.probe_interval_seconds=600` 秒自动探测全部启用路由的主/备模型与 GEO/SEO 引擎模型，结果写 `ai:health:{capability}:{model}` 与 `ai_models.last_health_*`；同一模型连续 2 次失败触发 `ai_upstream_unavailable` 告警并打开熔断，恢复后自动解决。

`GET /admin/ai/health` 汇总当前状态：

```json
{
  "zhiqi_mode": "live",
  "base_url": "https://zhiqiapi.com/v1",
  "paused": { "quota_exceeded": false, "auth_failed": false },
  "models": [ { "capability": "content", "model": "…", "status": "healthy", "latency_ms": 820, "checked_at": "…", "breaker_state": "closed", "breaker_reason": null, "is_available": true } ],
  "workers": [ { "name": "worker", "hostname": "dev-pc", "pid": 1234, "heartbeat_at": "…", "alive": true } ]
}
```

### 5. 真实生成一次并对账

重复第五节第 4 步生成一批关键词：「AI 任务」页的尝试行应出现真实 `request_id`（上游响应头 `x-oneapi-request-id`）、真实 tokens 与 `quota_estimated`；≤ 5 分钟后（或 `POST /admin/ai/usage/reconcile`）对账任务从 `GET /api/log/token` 拉取最近 1000 条日志按 `request_id` 匹配，回填 `quota_actual` / `cost_cny` / `reconciled_at`。上游日志只保留最近 1000 条且无分页：当一个对账周期内的调用数接近 1000（`pulled=1000` 且返回 `window_overflow=true`，worker 会记 warning 并把下一次间隔临时降到 `usage.min_interval_seconds=60`）时，把 `ai_routing_config.usage.reconcile_interval_seconds` 调小（最小 60），否则溢出窗口的调用永久无法对账（`quota_reconciled_rate < 100%`）。

### 接入检查清单

| # | 检查项 | 判定 |
| --- | --- | --- |
| 1 | `.env` 已填 `ZHIQI_API_KEY` 与 `ZHIQI_*_DEFAULT_MODEL`，三个进程已重启 | `GET /admin/auth/me` → `zhiqi_mode=live` |
| 2 | 模型目录已同步 | `GET /admin/ai/models` 的 `total > 0`，目标模型 `is_available=1`、`modalities` 正确 |
| 3 | 8 条全局路由 `is_enabled=1`，主/备模型均可用 | `GET /admin/ai/routes` 无 `is_enabled=0`，主/备模型 `is_available` 均为 `true`（保存时允许不可用模型，只以 `warnings[]` 提示），`breaker_state=closed` |
| 4 | 一键测试通过 | 文本路由主模型 `healthy`；`image` / `video` 至少模型在目录中 |
| 5 | GEO 引擎 / SEO 提供器已配置 | 启用的引擎 `model` 非空；`PUT /admin/settings/*` 无 400 |
| 6 | 真实生成 + 对账 | 尝试行 `request_id` 非 `mock-` 前缀，`quota_actual` 非空 |
| 7 | 媒体公网地址 | `GET /api/v1/health` 的 `warnings[]` 为空；上传接口返回 `public:true` |
| 8 | 无全局暂停 / 熔断 | `GET /admin/ai/health` 的 `paused` 全为 `false`，无 `open` 熔断 |

## 七、常用命令

根 `package.json` 脚本（`dev:server` / `dev:worker` / `dev:monitor` / `smoke:api` 固定使用 `server\.venv\Scripts\python.exe`，仅 Windows 可直接用；macOS/Linux 在 `server/` 下激活 venv 后把 `.venv\Scripts\python.exe` 换成 `python` 执行同样的参数）：

| 脚本 | 命令 | 说明 |
| --- | --- | --- |
| `pnpm dev:server` | `cd server && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8100` | 启动 API |
| `pnpm dev:worker` | `cd server && .venv\Scripts\python.exe -m app.worker` | 启动 AI 任务进程 |
| `pnpm dev:monitor` | `cd server && .venv\Scripts\python.exe -m app.monitor_worker` | 启动监控进程 |
| `pnpm dev:admin` | `pnpm --filter admin dev` | 启动管理后台（5174，访问 `http://localhost:5174/admin/`；首次运行前先 `pnpm build:shared`） |
| `pnpm dev:restart` | `powershell -ExecutionPolicy Bypass -File scripts/dev-restart.ps1` | Windows 一键重启：先结束本脚本上次启动的四个进程；8100 / 5174 被其它程序占用时自动换端口并打印实际地址（首次运行前先 `pnpm build:shared`） |
| `pnpm build:shared` / `pnpm build:admin` / `pnpm build` | `pnpm --filter @aicreat/shared build` / `pnpm --filter admin build` / `pnpm -r build` | 构建共享包（新克隆仓库或修改 `packages/shared` 后必须先执行）/ 后台 `apps/admin/dist` / 全部 |
| `pnpm smoke:api` | `cd server && .venv\Scripts\python.exe scripts\integration_smoke.py` | Mock 端到端冒烟（需 API、`app.worker`、`app.monitor_worker` 三进程运行；连接缺省地址 `http://127.0.0.1:8100`，以 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` 登录；API 在其它端口或超管已改密时直接运行脚本并传 `--base-url` / `--password`） |

后端（在 `server/` 目录、venv 已激活）：

| 命令 | 说明 |
| --- | --- |
| `alembic upgrade head` / `alembic downgrade -1` | 应用 / 回退迁移 |
| `alembic revision --autogenerate -m "msg"` | 由 `app/models.py` 生成迁移脚本 |
| `python seeds/seed.py` | 重放 seed（幂等） |
| `pytest` | 全部测试（`tests/conftest.py` 提供 SQLite/MySQL 会话、Mock 客户端与管理员 token 夹具） |
| `pytest tests/test_zhiqi_adapter.py -k mock` | 只跑适配层 Mock 相关用例 |
| `python scripts/integration_smoke.py [--base-url <API 根地址>] [--username <账号>] [--password <密码>]` | 冒烟脚本（需三进程运行；`--base-url` 缺省 `http://127.0.0.1:8100`，`--username` / `--password` 缺省取 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`） |

容器与运维：

| 命令 | 说明 |
| --- | --- |
| `docker compose up -d mysql redis` | 只起依赖服务（本机跑应用）；执行前必须已有根 `.env` |
| `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"` → `docker compose up -d` | 全容器运行（`server` / `worker` / `monitor-worker` / `nginx`），详见 [05-deployment](./05-deployment.md) |
| `docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000` | compose 环境（预发）的 Mock 冒烟：在 server 容器内运行，容器内 API 监听 8000；超管已改密时追加 `--password <新密码>`（缺省读容器环境变量 `SEED_ADMIN_PASSWORD`） |
| `docker compose logs -f server worker monitor-worker` | 跟踪容器日志 |
| `bash scripts/db-backup.sh` | `mysqldump` 到 `backups/`，保留 14 天 |

排障时常用的 Redis / HTTP 查询：

```bash
redis-cli LLEN queue:ai_tasks                       # 排队中的根任务数
redis-cli LLEN queue:link_checks                    # 排队中的删除检测
redis-cli LLEN queue:index_checks                   # 排队中的收录检测
redis-cli --scan --pattern 'worker:heartbeat:*'     # 存活的 worker 副本
redis-cli --scan --pattern 'ai:paused:*'            # 全局暂停键（quota_exceeded / auth_failed）
redis-cli --scan --pattern 'ai:breaker:*'           # 熔断器状态
redis-cli DEL rate:admin_login:admin                # 解除登录锁定
curl -s http://127.0.0.1:8100/api/v1/health
curl -s -X POST http://127.0.0.1:8100/api/v1/admin/auth/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}'
```

PowerShell 下 `curl` 是 `Invoke-WebRequest` 的别名，用 `Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8100/api/v1/admin/auth/login -ContentType 'application/json' -Body '{"username":"admin","password":"admin123"}'` 替代。

## 八、默认账号

| 账号 | 密码 | 用户组 | 来源 |
| --- | --- | --- | --- |
| `admin` | `admin123` | `super_admin`（全部 90 个权限码） | `seeds/seed.py` 按 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` 创建；启动日志会提示修改密码 |

- 首次登录后在顶栏「修改密码」（`POST /admin/auth/change-password`，≥ 8 位且含字母与数字）；成功后 `token_version += 1`，旧令牌失效需重新登录。改密后 `SEED_ADMIN_PASSWORD` 不再能登录（重放 `seeds/seed.py` 不会覆盖已存在账号的密码），运行冒烟脚本须用 `--password` 传入新密码（见「自动冒烟脚本」）。
- 其余三个系统用户组 `operator`（运营，数据范围「仅本人」）、`reviewer`（审核，「全部数据」）、`read_only`（只读，「全部数据」）只有组和默认权限，没有预置账号；在「系统 → 用户管理」新建（`POST /admin/admins`）并选择用户组即可验证菜单过滤、403 与数据隔离（第五节第 15 步）。`admin` 的数据范围固定为「全部数据」（总后台）。`operator` 没有 `content.contents.review`，`read_only` 只有各 `*.view`（不含 `system.settings.view` 与 `security.*`）加 `stats.reports.export`。
- 安全规则：不能禁用自己或最后一个有效超管；系统组不可删除、停用或清空权限；管理员不物理删除。

## 九、排错

| 症状 | 可能原因 | 处理 |
| --- | --- | --- |
| `alembic upgrade head` 连接失败 | mysql 容器尚未 healthy；未建根 `.env` 就启动了 compose（`MYSQL_*` 插值为空，mysql 初始化失败、反复重启）；`server/.env` 的 `DATABASE_URL` 与根 `.env` 的账号密码 / 库名或端口映射不一致 | `docker compose ps` 确认 healthy（不健康时看 `docker compose logs mysql`）；核对 `server/.env` 的 `DATABASE_URL`（用户、密码、库名、主机端口）与根 `.env` 的 `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` 及 compose 端口映射 `127.0.0.1:3306` 一致（`MYSQL_*` 只给 compose 用，后端只读 `DATABASE_URL`）；`mysql_data` 卷首次初始化后再改 `MYSQL_*` 不会生效，需要重建卷（本地可 `docker compose down -v`，会一并清空 `redis_data` 与 `media_data`）或手工修改账号 |
| 迁移报 SQL 语法错误（`settings` 表） | `key` 是 MySQL 8 保留字 | 迁移与手写 SQL 中列名写作 `` `key` `` |
| `python -m app.worker` 报 `No module named app`，或启动后连接 `127.0.0.1:3306` 失败但 `server/.env` 已改 | 未在 `server/` 目录下执行（模块路径与 `.env` 都按当前目录解析） | `cd server` 后再运行；`dev:*` 脚本已内置 `cd server` |
| API 启动后卡住数十秒 | `lock:bootstrap` 被上一个异常退出的进程持有 | 获取失败最多等待 30s 后直接重读（锁 TTL 60s 自动过期），无需干预；持续卡住则确认 Redis 可达 |
| 浏览器访问 `/admin/` 空白或 `/api` 404 | 未走 Vite 代理 / 代理目标端口与 API 实际端口不一致（`dev-restart` 换过端口） | 以 `dev-restart` 输出的地址访问；核对 `apps/admin/vite.config.ts` |
| Vite 报 `Failed to resolve entry for package "@aicreat/shared"`，后台无法加载 | 未构建共享包：`packages/shared/package.json` 的 `main` / `types` 指向 `dist/`，而 `packages/shared/dist/` 被 `.gitignore` 忽略 | 仓库根执行 `pnpm build:shared` 后重启 Vite（`pnpm dev:admin` 或 `pnpm dev:restart`）；修改 `packages/shared` 后同样要重新构建 |
| 访问 `http://127.0.0.1:5174/admin/` 被拒绝连接，`localhost` 却正常 | `vite.config.ts` 未设 `server.host`，Vite 默认只监听 `localhost`，Node 18/20 上可能只绑定 IPv6 `::1` | 用 `http://localhost:5174/admin/` 访问 |
| 浏览器报 CORS | 直连 8100 且来源不在 `ALLOWED_ORIGINS` | 加入 `ALLOWED_ORIGINS` 后重启 API，或改走 5174 代理 |
| 登录提示账号已锁定 | 15 分钟内失败 ≥ 5 次（`ADMIN_LOGIN_MAX_FAILURES=5`，计数键 `rate:admin_login:{username}` TTL 900s） | 等 15 分钟自动解锁，或 `redis-cli DEL rate:admin_login:<username>` |
| 登录成功后所有请求 401 | 重启时改了 `ADMIN_JWT_SECRET`；该管理员 `token_version` 已递增；管理员被禁用或所属用户组被停用 | 前两种重新登录即可；后两种重新登录会返回 403（「账号已禁用」/「用户组已停用」），需超级管理员在「管理员」或「用户组权限」页恢复 |
| 生成接口返回 429 | 每管理员频控 `generation_config.rate_limits.generate_per_admin=60/hour`（`rate:generate:{admin_id}`）或媒体 `media_per_admin=20/hour`（`rate:media:{admin_id}`）；手动收录检测固定 `30/hour`（`rate:index_check_manual:{admin_id}`） | 等待 `data.retry_after` 秒，或在「系统配置 → 生成 Tab」调高（手动收录检测频控不可配） |
| 生成接口返回 5031 | 路由 `is_enabled=0`（真实模式未设默认模型）/ 候选全部 `is_available=0` / 存在 `ai:paused:*`（`data.paused_reason`） | 到「AI 网关 → 能力路由」填模型并启用；检查「AI 网关 → 模型目录」；暂停键见下行 |
| `ai:paused:quota_exceeded` / `auth_failed` 存在，告警中心出现 `ai_quota_exceeded` / `ai_auth_failed` | 上游返回 402 / 401（额度不足或 Key 失效） | 充值或换 Key 后重启，再点任一路由「重置熔断」（`POST /admin/ai/routes/{id}/reset-breaker` 会删除暂停键）；不处理则暂停键在 `ai_routing_config.pause_seconds=600` 秒后自动过期、下一次失败再续写；暂停期间回滚为 `queued` 的任务（`pause_count += 1`，≥ 3 次改为 `failed`）由 `recover_stale_tasks` 自动补扫入队 |
| 生成任务 `failed` 且 `error_category=content_blocked` | 上游判定内容违规，不重试不回退；生成接口异步执行、不返回 5021，任务 `error_message` 也不附 hint（`data.hint=prompt_blocked` 只出现在同步调用上游的 HTTP 接口返回的 5021 中） | 修改提示词 / 种子词 |
| 返回 4291 | `data.scope=daily` / `project_monthly`：估算额度达到 `generation_config.quota.daily_limit` / `project_monthly_limit`（初值来自 `AI_DAILY_QUOTA_LIMIT` / `AI_PROJECT_MONTHLY_QUOTA_LIMIT`，0 不限；计数键 `quota:daily:{date}` / `quota:project:{project_id}:{yyyy-mm}`）；`scope=daily_images` / `daily_videos`：媒体日上限 `media_config.daily_limits`（`limit:images:{date}` / `limit:videos:{date}`） | 调高对应配置（修改后下一次请求即生效），或次日再试 |
| 任务一直 `queued` | `app.worker` 未运行；`ai:paused:*` 存在；并发信号量满 | 看 `/api/v1/health` 的 `workers.worker.alive` 与 `redis-cli LLEN queue:ai_tasks`；队列元素丢失时 `recover_stale_tasks` 每 60s 补扫超过 10 分钟的 `queued` 任务 |
| 批次长期 `running` | 子任务心跳超时（`WORKER_STALE_TASK_MINUTES=10`） | 等待 `recover_stale_tasks`（每 60s）回收：已拿到 `request_id` 的任务置 `failed(timeout)`、未发出请求的自动重试；全部根任务终态后批次按 [09-generation-pipeline](./09-generation-pipeline.md) §9.3 收敛（全部成功 → `succeeded`，部分失败或取消 → `partial`，全部失败或取消 → `failed`），`heartbeat_at` 超 30 分钟且无活动根任务的批次由回收兜底置 `partial` |
| 图片 / 视频停在 `generating` 很久 | 正常：Mock 固定 3 / 5 次轮询；真实模式上游可能长时间停在 99% | 超过 `poll_budget_seconds`（600 / 1200）置 `expired`，在素材库「重试」（`POST /admin/media/assets/{id}/retry` 会先复查旧上游任务，不重复计费） |
| 资产 `failed(transfer_failed)` | 上游临时 URL 过期、`Content-Type` / 魔数校验不通过、超过 50 MB / 500 MB | 「重新转存」（`POST /admin/media/assets/{id}/transfer`）；过期则「重试」重新提交 |
| 媒体接口返回 4222 | 真实模式下参考 URL 非公网（`PUBLIC_BASE_URL` 为 `127.0.0.1` 或内网） | 配置公网 `PUBLIC_BASE_URL` 或对象存储；Mock 模式不校验 |
| 回填返回 400 | URL 非 http/https、带用户名密码、非 80/443 端口、`javascript:` / `file:`；`published_at` 晚于当前时间 + 5 分钟或早于当前时间 − 3650 天（早于内容 `created_at` 是允许的） | 修正 URL 或 `published_at`；改 URL 需删后重填 |
| 链接一直 `pending` | `app.monitor_worker` 未运行；开发机无外网导致连续 `unknown`；URL 解析到内网 IP 被 SSRF 拒绝（`matched_rule=ssrf_blocked`） | 查 `GET /admin/links/{id}/checks` 的 `matched_rule` / `error_message`；`unknown` 连续 3 次后才写入 `unknown` 状态 |
| 收录检测结果全是 `unknown` | 引擎未启用；非 zhiqi 提供器凭据为空（`credential_missing`）；熔断打开（`breaker_open`）；存在 `ai:paused:*` | 看 `index_checks.error_category` / `error_message`；真实模式确认 `geo_engines` 每个启用引擎的 `model` 非空 |
| `seo_index_rate` / `geo_cite_rate` 为 `null` | 没有链接完成过 `scheduled` 轮次（`index_checks_done=0`，手动检测不计） | 把 `published_at` 填为 31 天前或等待排程（第 1 天） |
| 报表 / 总览没有历史数据 | `daily_stats` 未聚合（monitor_worker 未运行或刚启动） | `POST /admin/stats/recompute`（≤ 31 天）；「今日」由 `stats:rt` 兜底 |
| 模型目录为空 / 同步失败 | worker 未运行；真实模式 Key 无效或上游 5xx | `POST /admin/ai/models/sync` 看返回的 `request_ids` 与 `error_category`；查「AI 网关 → 健康」 |
| 真实模式 `/api/v1/health` 有 `warnings` | `PUBLIC_BASE_URL` 主机非公网 | 改为公网域名 / IP 并重启 |
| 从真实模式切回 Mock 后生成返回 5031 | 路由主模型仍是真实模型 ID，Mock 同步后被置 `is_available=0` | 在「AI 网关 → 能力路由」把主模型改回 `mock-text` / `mock-image` / `mock-video`（全局路由不能经接口删除）；或执行 SQL `DELETE FROM capability_routes WHERE project_id = 0` 后重启任一进程，让 `ensure_default_routes` 重新 seed |
| 告警中心出现 `worker_stale` | 某 worker 副本退出或主循环阻塞超过 5 分钟 | 重启对应进程；心跳恢复后告警自动解决 |
| `pnpm install` 报 pnpm 版本不匹配 | 本机 pnpm 非 9.x | `corepack prepare pnpm@9.0.0 --activate` |
| macOS/Linux 运行 `pnpm dev:server` 失败 | 脚本写死 `.venv\Scripts\python.exe` | 直接运行「常用命令」表中的等价命令 |
| Windows 激活 venv 报执行策略错误 | PowerShell 默认禁止运行脚本 | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`，或改用 `.venv\Scripts\python.exe` 直接运行 |
| 端口被占用 | `dev-restart` 上次启动的进程未退出，或 8100 / 5174 被其它程序占用 | `pnpm dev:restart` 会结束本脚本上次启动的进程；端口仍被其它程序占用时自动换端口，按输出地址访问；也可手动结束占用端口的进程 |
