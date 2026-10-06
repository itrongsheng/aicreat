# 05 部署

> 本文是 aicreat 的部署与运维基准：运行组件、**完整 `.env` 清单（全套文档中环境变量的权威定义）**、docker-compose 结构、Nginx 路由、公网素材 URL 要求、构建产物、发布流程、迁移与回滚、备份与监控指标。
> 服务拓扑、时序图、Redis 键表与 worker 任务表见 [01-architecture](./01-architecture.md)；目录结构见 [02-project-structure](./02-project-structure.md)；本机开发与 Mock 模式验证步骤见 [06-getting-started](./06-getting-started.md)；各配置键（`settings` 表）的结构分别见 [08-zhiqiapi-integration](./08-zhiqiapi-integration.md)（`ai_routing_config`）、[09-generation-pipeline](./09-generation-pipeline.md)（`generation_config`）、[10-media-generation](./10-media-generation.md)（`media_config`）、[11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md)（`monitoring_config` / `geo_engines` / `seo_providers` / `alert_config`）、[12-dashboard-reports](./12-dashboard-reports.md)（`stats_config`）。

## 1. 运行组件

### 1.1 组件表

| 组件 | 镜像 / 入口 | 职责 | 依赖与出网 | 端口 |
| --- | --- | --- | --- | --- |
| MySQL 8 | `mysql:8`（`utf8mb4` / `utf8mb4_unicode_ci`，InnoDB） | 24 张业务表（[03-data-model](./03-data-model.md)）；任务状态、检测记录、统计的**权威存储** | — | 3306：容器间走服务名 `mysql`；宿主机只绑定 `127.0.0.1:3306`（供本机开发 `docker compose up -d mysql redis` 直连，[06-getting-started](./06-getting-started.md)），不对公网开放（§3.2） |
| Redis 7 | `redis:7`（AOF 持久化，`maxmemory-policy noeviction`） | 缓存 `cache:*`、4 条任务队列（`queue:ai_tasks` / `queue:link_checks` / `queue:index_checks` / `queue:stats_recompute`）、锁 `lock:*`、熔断 `ai:breaker:*`、健康快照 `ai:health:*`、全局暂停 `ai:paused:*`、心跳 `worker:heartbeat:*`、频控/配额/日上限计数、实时统计 `stats:rt:*`（键表见 [01-architecture](./01-architecture.md)） | — | 6379：容器间走服务名 `redis`；宿主机只绑定 `127.0.0.1:6379`（同上） |
| server | `build: ./server`；`gunicorn -k uvicorn.workers.UvicornWorker app.main:app -b 0.0.0.0:8000 -w ${GUNICORN_WORKERS}` | FastAPI：`/api/v1/admin/*`、`GET /api/v1/health`、`GET /media/{key}`；启动时在 `lock:bootstrap` 内执行 `ensure_rbac_seed` / `ensure_default_settings` / `ensure_default_routes`，并把 `app/core/zhiqi/mock_assets/` 复制到 `LOCAL_STORAGE_DIR/mock/` | MySQL、Redis；出网到 zhiqiapi（同步接口）、对象存储（上传）、任意公网（`POST /admin/platforms/{id}/test`） | 8000（容器内；本机开发 8100） |
| worker | 同一镜像；`python -m app.worker` | AI 任务进程：消费 `queue:ai_tasks`、轮询图片/视频任务、转存素材、用量对账、模型目录同步、健康探测、僵死回收、素材清理（任务表见 [01-architecture](./01-architecture.md)） | MySQL、Redis；**出网到 zhiqiapi、对象存储与任意公网 HTTPS**（上游 `data[].url` 托管在第三方 CDN，必须能下载）；**不需要 ffmpeg** | 无 |
| monitor-worker | 同一镜像；`python -m app.monitor_worker` | 监控进程：链接删除检测、SEO/GEO 收录检测、`daily_stats` 聚合、告警评估 | MySQL、Redis；**出网到任意公网 HTTP/HTTPS 的 80/443**（抓取回填链接）、zhiqiapi（SEO/GEO 检测）、可选的百度 AI 搜索 / Bing Webmaster / Google Search Console 接口 | 无 |
| admin | `apps/admin/dist`（`pnpm build:admin`，`base: /admin/`） | 唯一前端：Vue 3 管理后台静态资源，由 nginx 托管 | — | — |
| nginx | `nginx:alpine` | 反向代理 `/api/`、`/media/`；托管 `/admin/`；`/` → `302 /admin/`；上传体积限制 | server | `${NGINX_HTTP_PORT}`（默认 80） |
| 对象存储（可选） | S3 兼容（阿里 OSS / 七牛 / MinIO；客户端 `boto3`） | `STORAGE_MODE=oss` 时存放生成与上传的素材，经 `OSS_PUBLIC_BASE_URL`（CDN）对外 | — | — |
| zhiqiapi（外部） | `https://zhiqiapi.com/v1` | 全部文本 / 图片 / 视频 / GEO / SEO 能力、模型目录、价格、用量日志（契约见 [08-zhiqiapi-integration](./08-zhiqiapi-integration.md)） | — | 443 |

与 navigation 的差异：没有用户端 `web` 与 Nuxt 进程；worker 镜像**不装 ffmpeg**（首版不转码、不解析视频宽高/时长，图片宽高由 `storage.probe_image_size` 解析文件头）；多一个 `monitor-worker` 进程；多一条「素材 URL 必须公网可达」的硬约束（§5）。

### 1.2 部署拓扑与网络出入口

```mermaid
flowchart LR
    Browser["运营浏览器"] -->|"80/443"| Nginx["nginx"]
    Nginx -->|"/api/ /media/"| Server["server:8000"]
    Nginx -->|"/admin/"| Dist[("apps/admin/dist")]
    Server --> MySQL[("mysql:3306")]
    Server --> Redis[("redis:6379")]
    Worker["worker"] --> MySQL
    Worker --> Redis
    Monitor["monitor-worker"] --> MySQL
    Monitor --> Redis
    Server & Worker & Monitor -->|"HTTPS"| Zhiqi["zhiqiapi.com/v1"]
    Worker -->|"转存下载"| CDN["上游 CDN 临时 URL"]
    Server & Worker -->|"S3 API 或本地卷"| Store[("对象存储 / media_data")]
    Monitor -->|"抓取 80/443"| Sites["外部发布平台页面"]
    Zhiqi -.->|"拉取参考素材"| Public["PUBLIC_BASE_URL/media 或 OSS_PUBLIC_BASE_URL"]
```

| 组件 | 入站 | 出站（防火墙必须放行） |
| --- | --- | --- |
| nginx | 公网 `80`（HTTPS 在前置负载均衡或 nginx 终止，§4.3） | `server:8000` |
| server | nginx | `mysql:3306`、`redis:6379`；`ZHIQI_BASE_URL`（`POST /admin/ai/models/sync`、`POST /admin/ai/routes/{id}/test`、`POST /admin/ai/health/probe`、`POST /admin/ai/usage/reconcile`、`POST /admin/media/assets/{id}/retry`（复查旧上游任务）为 API 进程内的同步调用，其余生成类接口只入队不出网）；`OSS_ENDPOINT`（`POST /admin/uploads/*`）；任意公网（`POST /admin/platforms/{id}/test` 实时抓取） |
| worker | 无 | `mysql`、`redis`；`ZHIQI_BASE_URL`；`OSS_ENDPOINT`；**任意公网 HTTPS**（转存经 `safe_fetch.stream_public_bytes` 逐跳校验公网地址、不带 Authorization） |
| monitor-worker | 无 | `mysql`、`redis`；**任意公网 HTTP/HTTPS 的 80/443**（删除检测只允许这两个端口与公网 IP）；`ZHIQI_BASE_URL`；按需 `qianfan.baidubce.com`、Bing Webmaster、Google API |
| 公网（zhiqiapi、浏览器） | `PUBLIC_BASE_URL` 下的 `/media/{key}` 或 `OSS_PUBLIC_BASE_URL`（图生图 / 图生视频的参考素材必须能被 zhiqiapi 拉取，§5） | — |

出网若需代理：给 server / worker / monitor-worker 三个容器设置标准的 `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`（httpx 与 boto3 均遵守），`NO_PROXY` 必须包含 `mysql,redis,127.0.0.1`。

### 1.3 副本与资源

| 组件 | 副本（首版默认值） | 说明 |
| --- | --- | --- |
| server | 1 个容器，`GUNICORN_WORKERS=2`（每 CPU 核 1~2） | 接口以「校验 + 入队」为主；长同步接口（路由测试 ≤ 30s/模型、`POST /admin/stats/recompute` 跨度 ≤ 7 天时在进程内逐日执行）以线程池方式运行，gunicorn `--timeout 120`（UvicornWorker 下该值只用于 arbiter 心跳、不限制单次请求时长；单次请求的上限由 nginx `proxy_read_timeout 300s` 决定，§4.2） |
| worker | 1 起步，可 `docker compose up -d --scale worker=N` | 每副本线程池 = `AI_MAX_CONCURRENCY_TEXT + AI_MAX_CONCURRENCY_IMAGE + AI_MAX_CONCURRENCY_VIDEO + 2`（默认 9）；并发信号量为进程内实现，**总并发 = 副本数 × 上限**；互斥依赖 DB `claim`（`UPDATE … WHERE status='queued'`）与 `lock:ai_task:{id}`；心跳键每副本一个（`worker:heartbeat:worker:{hostname}:{pid}`） |
| monitor-worker | 1 起步 | 线程池 = `link_check.global_concurrency + index_check.concurrency + 2`（默认 8）；多副本时 `domain:last_fetch:{domain}` 同域名间隔仍全局生效，但全局抓取并发按副本倍增，注意对外抓取礼貌性与 `link_check.daily_limit` |
| mysql | 1 | `max_connections >= 200`；`innodb_buffer_pool_size` 为内存 50%~70%；增长最快的表为 `ai_tasks`、`link_checks`、`index_checks`、`ai_usage_logs`、`daily_stats` |
| redis | 1 | 内存 ≥ 512 MB；`maxmemory-policy noeviction`（队列、锁、熔断状态不可被淘汰；计数类键均自带 TTL） |
| 磁盘 | — | `media_data` 卷：单个视频转存上限 `media_config.video.max_download_mb`（默认 500 MB）、日上限 `media_config.daily_limits.videos=20`，按 `视频数 × 平均体积 × 保留天数` 预估；`mysql_data` 按 `ai_tasks` 每行约 5~30 KB（含脱敏请求体）估算 |

## 2. 环境变量（`.env`）

### 2.1 文件位置与读取规则

| 项 | 规则 |
| --- | --- |
| 模板 | 仓库根 `.env.example`（`server/.env.example` 为其副本，两者同源，改一处须同步另一处） |
| docker-compose | 读取**仓库根** `.env`：既作 `env_file: .env` 注入三个应用容器，也作 compose 变量插值（`${MYSQL_USER}`、`${GUNICORN_WORKERS}`、`${NGINX_HTTP_PORT}` 等） |
| 本机开发 | `server/.env`（pydantic-settings 读取，字段名 = 变量名小写），见 [06-getting-started](./06-getting-started.md) |
| compose 覆盖 | `DATABASE_URL`、`REDIS_URL`、`LOCAL_STORAGE_DIR` 在 `docker-compose.yml` 的 `environment:` 中以容器内地址覆盖（§3），`.env` 里保持本机开发值即可 |
| 仅 compose | `MYSQL_ROOT_PASSWORD` / `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD` / `GUNICORN_WORKERS` / `NGINX_HTTP_PORT` 只被 compose 使用，后端 `Settings` 不读取 |
| seed 语义 | 标注「seed」的变量**只在配置键首次写入时生效**：`settings_service.ensure_default_settings(db)` 在 server 与两个 worker 启动时对每个配置键「键不存在则插入 `DEFAULT_SETTINGS[key]` 深合并环境变量派生值」，已存在的键不再改写；之后运行期一律以数据库配置为准（配置键在后台「系统配置」页各 Tab 维护，`ai_routing_config` 即「系统配置 → AI 路由 Tab」）。`capability_routes` 全局行由 `ensure_default_routes` 同样处理（§2.3），之后在后台「AI 网关 → 能力路由」页维护候选链 |
| 密钥 | `ADMIN_JWT_SECRET`、`MYSQL_*`、`OSS_ACCESS_KEY` / `OSS_SECRET_KEY`、`ZHIQI_API_KEY`、`SEO_*`、`ALERT_WEBHOOK_SECRET`、`SMTP_PASSWORD` 永不入库、不入日志、不入响应；后台只显示 `configured: true/false` |
| 书写规则 | `KEY=value`，行尾 `  # 注释` 允许；**含空格或 `#` 的值必须用双引号包住**（如 `MONITOR_USER_AGENT`），docker compose、pydantic-settings（python-dotenv）与 bash 三种解析器才会得到同一个值；**值中不要出现 `$`**（compose 与 python-dotenv 都会把未加单引号的 `$VAR` / `${VAR}` 当作变量展开）——随机生成 `ADMIN_JWT_SECRET` / 密码时只用 `[A-Za-z0-9-_.]` 字符集（如 `openssl rand -hex 32`），确需 `$` 时整个值用单引号包住；`MYSQL_PASSWORD` 会被拼进 `DATABASE_URL`，因此只允许 URL 安全字符 `[A-Za-z0-9-_.~]`，否则连接串需按 RFC 3986 百分号编码 |

### 2.2 完整清单

```ini
# ============ 数据库 / Redis（compose 覆盖为容器内地址；本机开发填 127.0.0.1）============
DATABASE_URL=mysql+pymysql://aicreat:password@127.0.0.1:3306/aicreat
REDIS_URL=redis://127.0.0.1:6379/0
# 仅 compose：mysql 服务初始化；MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE 必须与 DATABASE_URL 一致
MYSQL_ROOT_PASSWORD=root-change-me
MYSQL_DATABASE=aicreat
MYSQL_USER=aicreat
MYSQL_PASSWORD=password
# 仅 compose：server 容器 gunicorn worker 进程数、nginx 对外端口
GUNICORN_WORKERS=2
NGINX_HTTP_PORT=80

# ============ 安全与运行模式 ============
ADMIN_JWT_SECRET=please-change-me-admin        # 管理员 JWT（HS256，aud=admin）密钥；生产必改，改后全部管理员需重新登录
ADMIN_JWT_EXPIRE_SECONDS=7200                  # 令牌有效期
ADMIN_LOGIN_MAX_FAILURES=5                     # 15 分钟内登录失败上限（rate:admin_login:{username}）
DEV_MODE=true                                  # 生产设 false：严格 CORS、拒绝 http 参考 URL、错误响应不带堆栈
LOG_LEVEL=INFO
APP_TIMEZONE=Asia/Shanghai                     # 展示 / 切日时区；seed stats_config.timezone
ALLOWED_ORIGINS=http://127.0.0.1:5174,http://localhost:5174   # CORS 白名单（逗号分隔）；生产填后台公网 origin
# PUBLIC_BASE_URL：对外可访问基址（无尾部斜杠）；本地模式素材 URL = PUBLIC_BASE_URL + /media/{key}
# 真实模式必须是浏览器与 zhiqiapi 都能访问的公网地址（§5）；禁止 http://nginx、127.0.0.1（仅 Mock 模式可用）
PUBLIC_BASE_URL=http://127.0.0.1:8100

# ============ 存储 ============
STORAGE_MODE=local                             # local / oss
STORAGE_PROVIDER=s3                            # 仅 s3（S3 兼容：阿里 OSS / 七牛 / MinIO 均走 S3 API，boto3）
LOCAL_STORAGE_DIR=storage                      # 本地存储目录（相对 server/）；compose 覆盖为 storage → 共享卷 media_data:/app/storage
OSS_ENDPOINT=                                  # S3 兼容 endpoint；为空则强制 local
OSS_REGION=
OSS_BUCKET=
OSS_ACCESS_KEY=
OSS_SECRET_KEY=
OSS_PUBLIC_BASE_URL=http://127.0.0.1:8100/media   # oss 模式对象公网 / CDN 基址：素材 URL = OSS_PUBLIC_BASE_URL + /{key}
MAX_IMAGE_SIZE_MB=10                           # POST /admin/uploads/image 上限
MAX_VIDEO_SIZE_MB=200                          # POST /admin/uploads/video 上限（Nginx client_max_body_size 须 ≥ 此值，§4.2）

# ============ zhiqiapi 网关 ============
ZHIQI_BASE_URL=https://zhiqiapi.com/v1         # Anthropic 协议自动去掉尾部 /v1；/api/* 管理接口自动取 origin
ZHIQI_API_KEY=                                 # 为空 → Mock 模式（无密钥可跑通全流程）
ZHIQI_USER_AGENT=aicreat/0.1
ZHIQI_TIMEOUT_CONNECT_SECONDS=10               # 连接超时
ZHIQI_TIMEOUT_TEXT_SECONDS=180                 # 文本调用读超时；seed ai_routing_config.timeouts.text_seconds
ZHIQI_TIMEOUT_SUBMIT_SECONDS=60                # 图片 / 视频提交读超时；seed timeouts.submit_seconds
ZHIQI_TIMEOUT_POLL_SECONDS=30                  # 轮询读超时；seed timeouts.poll_seconds
ZHIQI_TIMEOUT_DOWNLOAD_SECONDS=300             # 媒体下载超时
ZHIQI_MAX_RETRIES=3                            # 客户端 HTTP 幂等重试次数；seed ai_routing_config.retry.max_attempts
ZHIQI_RETRY_BASE_SECONDS=1.0                   # 退避基数；seed retry.base_seconds
ZHIQI_RETRY_MAX_SECONDS=30                     # 退避上限；seed retry.max_seconds
ZHIQI_BREAKER_FAILURE_THRESHOLD=5              # 窗口内失败次数触发熔断；seed breaker.failure_threshold
ZHIQI_BREAKER_WINDOW_SECONDS=300               # 失败统计窗口；seed breaker.window_seconds
ZHIQI_BREAKER_OPEN_SECONDS=120                 # 熔断打开时长；seed breaker.open_seconds
# 能力路由主模型初值（seed capability_routes 全局行）；Mock 模式忽略并固定 mock-text / mock-image / mock-video
ZHIQI_TEXT_DEFAULT_MODEL=                      # keyword / title / content / rewrite；真实模式为空 → 路由 is_enabled=0 + 启动告警日志
ZHIQI_TEXT_DEFAULT_PROTOCOL=openai_chat        # openai_chat / openai_responses / anthropic_messages
ZHIQI_IMAGE_DEFAULT_MODEL=                     # image 路由主模型
ZHIQI_VIDEO_DEFAULT_MODEL=                     # video 路由主模型
ZHIQI_GEO_DEFAULT_MODEL=                       # geo_check 路由（联网模型；仅健康探测与默认 params，GEO 引擎模型在 settings.geo_engines）
ZHIQI_SEO_DEFAULT_MODEL=                       # seo_check 路由（联网模型）
ZHIQI_IMAGE_POLL_BUDGET_SECONDS=600            # 图片轮询预算；seed media_config.image.poll_budget_seconds
ZHIQI_VIDEO_POLL_BUDGET_SECONDS=1200           # 视频轮询预算；seed media_config.video.poll_budget_seconds
ZHIQI_IMAGE_SYNC_FALLBACK=true                 # 异步路由不存在 / 模型未路由时回退同步；seed media_config.image.sync_fallback
ZHIQI_USAGE_RECONCILE_INTERVAL_SECONDS=300     # 用量对账间隔；seed ai_routing_config.usage.reconcile_interval_seconds（0 关闭）
ZHIQI_MODELS_SYNC_INTERVAL_SECONDS=3600        # 模型目录同步间隔；seed catalog.sync_interval_seconds
ZHIQI_HEALTH_PROBE_INTERVAL_SECONDS=600        # 健康探测间隔；seed health.probe_interval_seconds（0 关闭自动探测）
ZHIQI_QUOTA_PER_UNIT=500000                    # 500000 额度 = 1 USD；seed ai_routing_config.pricing.quota_per_unit
ZHIQI_USD_CNY_RATE=7.2                         # 汇率；seed pricing.usd_cny_rate
ZHIQI_GROUP_RATIO=1.0                          # 本 Key 分组倍率；seed pricing.group_ratio（对账后以日志 group_ratio 为准）

# ============ 生成频控 / 并发 / 配额 ============
GENERATE_RATE_LIMIT=60/hour                    # 每管理员文本生成频控；seed generation_config.rate_limits.generate_per_admin
MEDIA_RATE_LIMIT=20/hour                       # 每管理员媒体生成频控；seed generation_config.rate_limits.media_per_admin
AI_MAX_CONCURRENCY_TEXT=4                      # worker 文本并发（进程内，每副本独立）
AI_MAX_CONCURRENCY_IMAGE=2                     # 图片提交并发
AI_MAX_CONCURRENCY_VIDEO=1                     # 视频提交并发
AI_DAILY_QUOTA_LIMIT=0                         # 全局日额度上限（估算值，0 不限）；seed generation_config.quota.daily_limit
AI_PROJECT_MONTHLY_QUOTA_LIMIT=0               # 项目月额度上限；seed generation_config.quota.project_monthly_limit

# ============ worker 进程 ============
WORKER_POLL_INTERVAL_SECONDS=2                 # app.worker 主循环休眠
WORKER_STALE_TASK_MINUTES=10                   # 心跳超时回收阈值（recover_stale_tasks）
MONITOR_POLL_INTERVAL_SECONDS=5                # app.monitor_worker 主循环休眠

# ============ 链接删除检测（seed monitoring_config.link_check.*）============
MONITOR_USER_AGENT="aicreatLinkMonitor/1.0 (+https://example.com/contact)"   # 固定 UA，换成真实联系方式；含空格必须加双引号
MONITOR_FETCH_TIMEOUT_SECONDS=15               # 抓取超时
MONITOR_MAX_RESPONSE_BYTES=2097152             # 响应体上限 2 MB
MONITOR_MAX_REDIRECTS=3                        # 最大重定向跳数
MONITOR_CONCURRENCY=4                          # 删除检测并发（进程内信号量）
MONITOR_PER_DOMAIN_INTERVAL_SECONDS=2          # 同域名最小间隔
MONITOR_ALLOW_HTTP=true                        # 允许 http 目标；生产必须 false（§2.3）

# ============ SEO 收录检测提供器凭据（留空 → 该提供器返回 unknown + auth_failed / credential_missing）============
SEO_BAIDU_AI_SEARCH_API_KEY=                   # baidu_ai_search（百度 AI 搜索官方 API）
SEO_BING_WEBMASTER_API_KEY=                    # bing_webmaster
SEO_BING_SITE_URL=                             # Bing 已验证站点（只对该域名下的链接生效）
SEO_GSC_CREDENTIALS_FILE=                      # google_search_console 服务账号 JSON 路径（容器内路径，只读挂载）
SEO_GSC_SITE_URL=                              # GSC 已验证资源

# ============ 告警通道（预留；alert_config.channels.* 打开后生效）============
ALERT_WEBHOOK_URL=                             # 告警 webhook（POST JSON）
ALERT_WEBHOOK_SECRET=                          # 签名头 X-Aicreat-Signature: sha256=<HMAC-SHA256(body)>
SMTP_HOST=
SMTP_PORT=465
SMTP_USER=
SMTP_PASSWORD=
MAIL_FROM=no-reply@example.com

# ============ 初始超级管理员（seeds/seed.py 首次写入；启动日志提示修改密码）============
SEED_ADMIN_USERNAME=admin
SEED_ADMIN_PASSWORD=admin123
```

`Settings` 的派生属性（代码内使用，不是环境变量）：`cors_origins`（`ALLOWED_ORIGINS` 拆分）、`use_local_storage`（`STORAGE_MODE=local` 或 `OSS_ENDPOINT` 为空）、`zhiqi_mock_mode`（`not zhiqi_api_key`）、`zhiqi_origin`（`ZHIQI_BASE_URL` 去掉尾部 `/v1`）、`media_public_base`（本地模式 `PUBLIC_BASE_URL + /media`，oss 模式 `OSS_PUBLIC_BASE_URL`）。

### 2.3 生产环境必改项

| 变量 | 生产要求 | 不改的后果 |
| --- | --- | --- |
| `ADMIN_JWT_SECRET` | ≥ 32 字节随机串 | 任何人可伪造管理员令牌 |
| `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD`（与 `DATABASE_URL` 一致） | 随机密码，字符集 `[A-Za-z0-9-_.~]`（§2.1 书写规则） | 数据库弱口令；含 `$`/`@`/`:` 等字符时 compose 插值或 `DATABASE_URL` 解析出错 |
| `DEV_MODE` | `false` | CORS 放宽、允许 http 参考 URL、错误响应带堆栈 |
| `ALLOWED_ORIGINS` | 后台公网 origin（如 `https://aicreat.example.com`） | 跨域请求被拒或过宽 |
| `PUBLIC_BASE_URL` | 公网可解析且可访问的 **https** 域名（§5；`DEV_MODE=false` 时参考 URL 必须为 https，`http://<公网IP>` 只能配合 `DEV_MODE=true` 在预发临时使用） | 真实模式图生图 / 图生视频 4222 或上游拉取失败；`GET /api/v1/health` 持续 `warnings[]` |
| `STORAGE_MODE` + `OSS_*` | 生产固定 `oss`（桶允许匿名读 + CDN，§5.1）；`local` 仅限本机开发与预发 | 本地卷无冗余，素材随容器宿主机丢失 |
| `ZHIQI_API_KEY` | 真实 Key | 停留在 Mock 模式（假文 / 占位图） |
| `ZHIQI_TEXT_DEFAULT_MODEL` / `ZHIQI_IMAGE_DEFAULT_MODEL` / `ZHIQI_VIDEO_DEFAULT_MODEL` / `ZHIQI_GEO_DEFAULT_MODEL` / `ZHIQI_SEO_DEFAULT_MODEL` | 首次启动前填好（模型 ID 以 zhiqiapi 模型目录为准） | 对应全局路由 `is_enabled=0`，生成接口返回 5031，需在后台「AI 网关 → 能力路由」页为该路由选择主模型后手工启用 |
| `MONITOR_ALLOW_HTTP` | `false` | 允许向 http 目标发起抓取 |
| `MONITOR_USER_AGENT` | 含真实联系方式 | 被目标站点视为匿名爬虫 |
| `SEED_ADMIN_PASSWORD` | 首次登录后立即改密（或 seed 前改为强密码）；改密后运行冒烟脚本需用 `--password` 传入新密码（§3.4） | `admin/admin123` 弱口令 |

### 2.4 seed 变量与配置键的对应

下列变量只在配置键首次写入时参与初值（`settings_service.ENV_SEED_PATHS`，`ensure_default_settings` 据此深合并）；上线后修改它们**不会**改变数据库里已有的配置，应在后台修改：配置键在「系统配置」页对应 Tab 修改（`ai_routing_config.*` 在「系统配置 → AI 路由 Tab」），表末行对应的 `capability_routes` 全局行（主模型、备选链与协议）在「AI 网关 → 能力路由」页修改。需要强制重 seed 时：停止三个应用容器 → ``DELETE FROM settings WHERE `key`='<配置键>' AND locale='*'`` → 启动（会丢失该键上所有后台改动；`settings` 主键为 `(key, locale)`，`key` 是 MySQL 保留字必须加反引号）。

| 变量 | 配置键路径 | 变量 | 配置键路径 |
| --- | --- | --- | --- |
| `APP_TIMEZONE` | `stats_config.timezone` | `GENERATE_RATE_LIMIT` | `generation_config.rate_limits.generate_per_admin` |
| `MEDIA_RATE_LIMIT` | `generation_config.rate_limits.media_per_admin` | `AI_DAILY_QUOTA_LIMIT` | `generation_config.quota.daily_limit` |
| `AI_PROJECT_MONTHLY_QUOTA_LIMIT` | `generation_config.quota.project_monthly_limit` | `ZHIQI_TIMEOUT_TEXT_SECONDS` | `ai_routing_config.timeouts.text_seconds` |
| `ZHIQI_TIMEOUT_SUBMIT_SECONDS` | `ai_routing_config.timeouts.submit_seconds` | `ZHIQI_TIMEOUT_POLL_SECONDS` | `ai_routing_config.timeouts.poll_seconds` |
| `ZHIQI_MAX_RETRIES` | `ai_routing_config.retry.max_attempts` | `ZHIQI_RETRY_BASE_SECONDS` | `ai_routing_config.retry.base_seconds` |
| `ZHIQI_RETRY_MAX_SECONDS` | `ai_routing_config.retry.max_seconds` | `ZHIQI_BREAKER_FAILURE_THRESHOLD` | `ai_routing_config.breaker.failure_threshold` |
| `ZHIQI_BREAKER_WINDOW_SECONDS` | `ai_routing_config.breaker.window_seconds` | `ZHIQI_BREAKER_OPEN_SECONDS` | `ai_routing_config.breaker.open_seconds` |
| `ZHIQI_IMAGE_POLL_BUDGET_SECONDS` | `media_config.image.poll_budget_seconds` | `ZHIQI_VIDEO_POLL_BUDGET_SECONDS` | `media_config.video.poll_budget_seconds` |
| `ZHIQI_IMAGE_SYNC_FALLBACK` | `media_config.image.sync_fallback` | `ZHIQI_USAGE_RECONCILE_INTERVAL_SECONDS` | `ai_routing_config.usage.reconcile_interval_seconds` |
| `ZHIQI_MODELS_SYNC_INTERVAL_SECONDS` | `ai_routing_config.catalog.sync_interval_seconds` | `ZHIQI_HEALTH_PROBE_INTERVAL_SECONDS` | `ai_routing_config.health.probe_interval_seconds` |
| `ZHIQI_QUOTA_PER_UNIT` | `ai_routing_config.pricing.quota_per_unit` | `ZHIQI_USD_CNY_RATE` | `ai_routing_config.pricing.usd_cny_rate` |
| `ZHIQI_GROUP_RATIO` | `ai_routing_config.pricing.group_ratio` | `MONITOR_USER_AGENT` | `monitoring_config.link_check.user_agent` |
| `MONITOR_FETCH_TIMEOUT_SECONDS` | `monitoring_config.link_check.timeout_seconds` | `MONITOR_MAX_RESPONSE_BYTES` | `monitoring_config.link_check.max_response_bytes` |
| `MONITOR_MAX_REDIRECTS` | `monitoring_config.link_check.max_redirects` | `MONITOR_CONCURRENCY` | `monitoring_config.link_check.global_concurrency` |
| `MONITOR_PER_DOMAIN_INTERVAL_SECONDS` | `monitoring_config.link_check.per_domain_interval_seconds` | `MONITOR_ALLOW_HTTP` | `monitoring_config.link_check.allow_http` |
| `ZHIQI_API_KEY` 为空（Mock） | `geo_engines.engines[].enabled = true` | `ZHIQI_*_DEFAULT_MODEL` / `ZHIQI_TEXT_DEFAULT_PROTOCOL` | `capability_routes` 全局行（`project_id=0`，不入 `settings`） |

运行期每轮由 worker 从数据库读取的周期参数（`usage.reconcile_interval_seconds`、`catalog.sync_interval_seconds`、`health.probe_interval_seconds`）与按调用注入的重试 / 熔断参数，在「系统配置 → AI 路由 Tab」修改后下一轮 / 下一次调用生效，无需重启。

### 2.5 环境矩阵

| 环境 | `.env` 位置 | 模式 | 关键取值 |
| --- | --- | --- | --- |
| 本机开发 | `server/.env` | Mock（`ZHIQI_API_KEY` 空） | `DATABASE_URL`/`REDIS_URL` 指向 `127.0.0.1`；`PUBLIC_BASE_URL=http://127.0.0.1:8100`；API 8100、admin 5174（Vite 代理 `/api`、`/media` → `http://127.0.0.1:8100`） |
| 预发（staging） | 仓库根 `.env`（compose） | 先 Mock（跑通 `scripts/integration_smoke.py`，命令与凭据参数见 §3.4），再切真实 Key 重复最小流程（§11 第 12 项） | `DEV_MODE=false`；`PUBLIC_BASE_URL` 为预发公网地址；额度上限设小额防误用：首次启动前通过 `AI_DAILY_QUOTA_LIMIT` seed，已初始化的环境在后台修改 `generation_config.quota.daily_limit`（§2.4） |
| 生产 | 仓库根 `.env`（compose） | 真实 Key | §2.3 全部必改项；`DEV_MODE=false` + `PUBLIC_BASE_URL=https://…`；`STORAGE_MODE=oss`；`MONITOR_ALLOW_HTTP=false`；`LOG_LEVEL=INFO` |

## 3. docker-compose

### 3.1 固定约定

| 项 | 约定 |
| --- | --- |
| 服务 | `mysql` / `redis` / `server` / `worker` / `monitor-worker` / `nginx`（无 `web`） |
| 镜像 | `server` / `worker` / `monitor-worker` 共用 `build: ./server` 同一镜像，只是 `command` 不同（gunicorn / `python -m app.worker` / `python -m app.monitor_worker`）；三者都声明 `image: aicreat-server`，`docker compose build server` 构建一次后 `up -d` 不再为 worker / monitor-worker 重复构建（缺少 `image:` 时 compose 会按服务名各构建一份 `aicreat-worker` / `aicreat-monitor-worker`） |
| 端口 | 只有 `nginx` 对外（`${NGINX_HTTP_PORT}:80`）；`mysql` / `redis` 绑定宿主机回环地址 `127.0.0.1:3306` / `127.0.0.1:6379`（供本机开发 `docker compose up -d mysql redis` 直连，[06-getting-started](./06-getting-started.md)），不绑定 `0.0.0.0`；`server` / `worker` / `monitor-worker` 不映射端口 |
| 环境 | 三个应用容器 `env_file: .env`（仓库根），并以相同的 `environment:` 覆盖 `DATABASE_URL=mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@mysql:3306/${MYSQL_DATABASE}`、`REDIS_URL=redis://redis:6379/0`、`LOCAL_STORAGE_DIR=storage` |
| 卷 | `mysql_data:/var/lib/mysql`、`redis_data:/data`（AOF）、`media_data:/app/storage`（三个应用容器共享；本地模式的素材与 `mock/` 占位文件在此） |
| 健康检查 | mysql：`mysqladmin ping -h 127.0.0.1 -p$MYSQL_ROOT_PASSWORD`（interval 10s、retries 10）；redis：`redis-cli ping`；server：`curl -f http://127.0.0.1:8000/api/v1/health`（db / redis 不可用时接口返回 503 即不健康，worker 不活跃只是 `degraded` 仍为 200） |
| 依赖 | `server` / `worker` / `monitor-worker` 均 `depends_on: {mysql: service_healthy, redis: service_healthy}` 且 `restart: unless-stopped`；`nginx` `depends_on: [server]` |
| nginx 挂载 | `./nginx/nginx.conf`（只读）与宿主机 `pnpm build:admin` 产物 `./apps/admin/dist`（只读） |
| 发布命令 | `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"` → `docker compose up -d`（`run` 同样等待 mysql / redis 健康） |
| worker 存活 | 不做容器级 healthcheck；以 `GET /api/v1/health` 的 `workers.*.alive`（任一副本心跳 < 90s）与 `worker_stale` 告警判断 |

### 3.2 `docker-compose.yml`

```yaml
x-app-env: &app-env                    # §2.1「compose 覆盖」行：三个应用容器相同
  DATABASE_URL: mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@mysql:3306/${MYSQL_DATABASE}
  REDIS_URL: redis://redis:6379/0
  LOCAL_STORAGE_DIR: storage

x-app-base: &app-base
  build: ./server
  image: aicreat-server                # 三个应用服务同名镜像：只构建一次，worker / monitor-worker 直接复用
  env_file: .env
  environment: *app-env
  restart: unless-stopped
  depends_on:
    mysql: { condition: service_healthy }
    redis: { condition: service_healthy }
  volumes:
    - media_data:/app/storage
  logging:
    driver: json-file
    options: { max-size: "50m", max-file: "5" }

services:
  mysql:
    image: mysql:8
    command: --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: ${MYSQL_DATABASE}
      MYSQL_USER: ${MYSQL_USER}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
    ports:
      - "127.0.0.1:3306:3306"          # 只绑回环地址：本机开发直连；生产主机外部不可达
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -p$$MYSQL_ROOT_PASSWORD"]   # $$ 交给容器内 shell 展开
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  redis:
    image: redis:7
    command: redis-server --appendonly yes --maxmemory-policy noeviction
    ports:
      - "127.0.0.1:6379:6379"          # 同上，只绑回环地址
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  server:
    <<: *app-base
    command: gunicorn -k uvicorn.workers.UvicornWorker app.main:app -b 0.0.0.0:8000 -w ${GUNICORN_WORKERS:-2} --timeout 120
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8000/api/v1/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s

  worker:
    <<: *app-base
    command: python -m app.worker        # AI 任务 / 轮询 / 转存 / 对账 / 模型同步 / 健康探测 / 回收 / 清理
    stop_grace_period: 90s               # 给在飞的提交 / 转存留出收尾时间；超时未完成的任务由 recover_stale_tasks 回收

  monitor-worker:
    <<: *app-base
    command: python -m app.monitor_worker   # 删除检测 / 收录检测 / daily_stats / 告警评估
    stop_grace_period: 60s

  nginx:
    image: nginx:alpine
    ports:
      - "${NGINX_HTTP_PORT:-80}:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./apps/admin/dist:/usr/share/nginx/admin:ro
    depends_on:
      - server
    restart: unless-stopped

volumes:
  mysql_data:
  redis_data:
  media_data:
```

说明：

- `worker` 与 `monitor-worker` 不对外暴露端口，也不装 ffmpeg；它们必须能访问对象存储 endpoint 与公网（§1.2），否则表现为素材停留在 `downloading`（`transfer_failed`）或链接检测全部 `unknown(network_error)`。
- `media_data` 挂到三个应用容器是为了让三者配置完全一致；只有 `server`（上传、`/media` 直出、复制 Mock 占位文件）与 `worker`（转存、清理）实际读写该卷。
- 多副本：`docker compose up -d --scale worker=2`；每副本有独立的线程池、信号量与心跳键，任务互斥由 DB `claim` 与 `lock:*` 保证。
- 停机：`docker compose stop worker` 发送 SIGTERM 并等待 `stop_grace_period`；仍为 `running` 的根任务在心跳超过 `WORKER_STALE_TASK_MINUTES` 后由 `recover_stale_tasks` 按能力回收（文本未收到响应头的自动重试，但所属批次已 `cancelled` 时只置 `failed(timeout)`、不自动重试；媒体已受理的改为 `polling` 继续轮询；可能已计费的置 `failed(timeout)` 不自动重试），队列元素丢失由同一任务按 DB 补扫，因此 worker 可随时重启而不丢任务。
- `SEO_GSC_CREDENTIALS_FILE` 指向的服务账号 JSON 以只读方式挂进 `monitor-worker`（`- ./secrets/gsc.json:/run/secrets/gsc.json:ro`，变量填容器内路径 `/run/secrets/gsc.json`；仓库根 `secrets/` 目录加入 `.gitignore`，它不在 `build: ./server` 的构建上下文内，不会进镜像）。注意 YAML 的 `<<: *app-base` 不合并列表：在 `monitor-worker` 下自行写 `volumes:` 时必须把 `media_data:/app/storage` 一并列出，否则该容器丢失共享卷。
- `mysql` / `redis` 的 `ports:` 只绑定 `127.0.0.1`，目的只有一个：本机开发用 `docker compose up -d mysql redis` 起依赖后，`server/.env` 的 `DATABASE_URL` / `REDIS_URL` 默认值（`127.0.0.1:3306` / `127.0.0.1:6379`）可以直连；生产主机上该映射对外不可达，可保留。宿主机 3306 / 6379 已被占用时改为 `127.0.0.1:13306:3306` 这类映射，并同步修改 `server/.env`（容器内连接串不受影响）。

### 3.3 `server/Dockerfile`

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# curl 供 compose healthcheck；ca-certificates 供 httpx / boto3 访问 HTTPS。不安装 ffmpeg。
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 先拷贝源码再安装：pip install -e . 需要 app/ 包存在（只拷 pyproject.toml 会安装出空包）；源码变更后重建镜像即可
COPY . .
RUN pip install -e .

EXPOSE 8000
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "-b", "0.0.0.0:8000", "-w", "2"]
```

`server/.dockerignore` 至少排除 `.venv/`、`storage/`、`.env`、`__pycache__/`、`*.log`，避免把本机素材与密钥打进镜像。镜像内包含 `migrations/`、`seeds/`、`scripts/`（含 `integration_smoke.py`）与 `app/core/zhiqi/mock_assets/`（`placeholder.png` / `placeholder.mp4`），三个进程共用。

### 3.4 常用命令

| 命令 | 说明 |
| --- | --- |
| `docker compose build server` | 构建应用镜像 `aicreat-server`（worker / monitor-worker 声明同一 `image:`，直接复用，不重复构建） |
| `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"` | 迁移 + 幂等 seed（发布前固定步骤） |
| `docker compose up -d` | 启动 / 滚动更新全部服务 |
| `docker compose up -d --scale worker=2` | 扩容 worker |
| `docker compose ps` / `docker compose logs -f --tail=200 worker` | 状态与日志 |
| `docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000` | Mock 模式端到端冒烟（预发）：在 server 容器内运行，容器内 gunicorn 监听 8000，必须显式传 `--base-url`；凭据参数与覆盖范围见表后说明，断言细节见 [06-getting-started](./06-getting-started.md) |
| `docker compose exec redis redis-cli LLEN queue:ai_tasks` | 查看队列积压 |
| `docker compose exec mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'` | 进入数据库（单引号：变量在 mysql 容器内展开，宿主机无需加载 `.env`） |

冒烟脚本 `server/scripts/integration_smoke.py` 的参数：`--base-url`（API 根地址，只写 scheme + 主机 + 端口，不带 `/api/v1`；缺省 `http://127.0.0.1:8100` 是本机开发端口，server 容器内 8100 上没有服务）、`--username` / `--password`（缺省取环境变量 `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`，容器内即根 `.env` 经 `env_file` 注入的值）。超管改密后（§7.2 第 3 步），`.env` 里的 `SEED_ADMIN_PASSWORD` 不再能登录（`seeds/seed.py` 不覆盖已存在账号的密码），须用参数传入当前密码：

```bash
docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000 --password '<改密后的密码>'
# 超管用户名与 SEED_ADMIN_USERNAME 不同时，再追加 --username <用户名>
```

脚本按「登录 → 关键词 → 标题 → 内容 → 图片 → 回填 → 检测 → 报表」顺序执行，断言 SEO / GEO 检测结果非 `unknown`、报表 `seo_index_rate` / `geo_cite_rate` 非 null 且 > 0。其中的删除检测 / 告警分支会回填一条返回 404 的公网地址，断言出现 `link_deleted`（`warning`）告警，并由脚本确认、解决该告警；预发若已启用 webhook 通道（§10.4），该告警会按通道规则照常投递。`link_restored` 恢复分支不在脚本内，由后端测试覆盖（[11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md) §16.2）。前置条件：`server` / `worker` / `monitor-worker` 三个容器都在运行，且 `monitor-worker` 可访问公网（§11 第 4 项）。

## 4. Nginx 路由约定

### 4.1 路由表

| 路径 | 转发 / 处理 | 说明 |
| --- | --- | --- |
| `/api/` | `server:8000` | 全部接口（`/api/v1/admin/*`、`/api/v1/health`）；`proxy_read_timeout 300s` 覆盖同步长接口 |
| `/api/v1/admin/uploads/` | `server:8000`，`client_max_body_size 210m`、`proxy_request_buffering off` | 参考图 / 参考视频上传：体积 = `MAX_VIDEO_SIZE_MB` + 10m（比后端限制略大，让后端返回明确的 400 而不是 nginx 413） |
| `/media/` | `server:8000` | 本地模式素材直出（`GET /media/{key}`，公开、无鉴权，zhiqiapi 需能拉取）；`oss` 模式下 `media_assets.url` 本身就是 `OSS_PUBLIC_BASE_URL` 直链（浏览器与 zhiqiapi 都不经 nginx），`/media/{key}` 只作为 server 回源对象存储的兜底 |
| `/admin/` | `apps/admin/dist` 静态资源，`try_files … /admin/index.html` | Vite SPA（`base: /admin/`）；`assets/` 带 hash，长缓存 |
| `/` | `302 /admin/` | 没有用户端 |

其余请求体默认 `client_max_body_size 20m`：覆盖 JSON 接口与关键词 CSV 文件导入。

### 4.2 `nginx/nginx.conf`

```nginx
worker_processes auto;

events {
    worker_connections 1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;
    sendfile      on;
    keepalive_timeout 65;
    client_max_body_size 20m;              # 默认：JSON / CSV 导入足够

    # 带耗时的访问日志：$request_time / $upstream_response_time 供 §10.2「/api/ p95 延迟」与 5xx 比例统计
    log_format main '$remote_addr - [$time_local] "$request" $status $body_bytes_sent '
                    'rt=$request_time urt=$upstream_response_time "$http_user_agent"';
    access_log /var/log/nginx/access.log main;

    upstream api_server {
        server server:8000;
    }

    server {
        listen 80;
        server_name _;

        # 根路径跳转到后台
        location = / {
            return 302 /admin/;
        }

        # API 转发
        location /api/ {
            proxy_pass http://api_server;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 300s;        # 路由测试 / 对账 / ≤ 7 天的 stats recompute 等同步接口
        }

        # 参考图 / 参考视频上传：放开体积（MAX_VIDEO_SIZE_MB + 10m），关闭请求缓冲
        location /api/v1/admin/uploads/ {
            client_max_body_size 210m;
            proxy_request_buffering off;
            proxy_read_timeout 300s;        # 大视频：server 端魔数校验 / 哈希 / 落盘或上传对象存储
            proxy_pass http://api_server;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # 素材文件：本地模式直出；oss 模式下 media_assets.url 已是 OSS_PUBLIC_BASE_URL 直链，此处仅作 server 回源兜底
        location /media/ {
            proxy_pass http://api_server;
            proxy_set_header Host $host;
            add_header Cache-Control "public, max-age=2592000, immutable";   # storage_key 带日期与随机名，内容不变，可长缓存
        }

        # 管理后台（Vite SPA，base /admin/）；/admin 不带尾斜杠时补齐，否则不命中 /admin/ 前缀而 404
        location = /admin {
            return 301 /admin/;
        }
        location /admin/assets/ {
            alias /usr/share/nginx/admin/assets/;
            add_header Cache-Control "public, max-age=31536000, immutable";   # 文件名带 hash；不与 expires 并用，避免产生两个 Cache-Control 头
        }
        location /admin/ {
            alias /usr/share/nginx/admin/;
            try_files $uri $uri/ /admin/index.html;
            add_header Cache-Control "no-cache";
        }
    }
}
```

### 4.3 HTTPS 与访问控制

- 生产必须 HTTPS：在前置负载均衡 / 云 LB 终止 TLS 并透传 `X-Forwarded-Proto`，或在 `nginx.conf` 增加 `listen 443 ssl` 并把 443 端口映射写入 compose（本文模板只映射 `NGINX_HTTP_PORT`）。`PUBLIC_BASE_URL` 必须与对外 scheme / 域名完全一致（如 `https://aicreat.example.com`）。
- 后台是内部工具：生产必须对后台加访问控制，固定做法二选一——① nginx IP 白名单：在 `server {}` 级写 `allow <办公网段>; deny all;`（`allow/deny` 向所有 location 继承，`/admin/`、`/admin/assets/`、`/api/`、`/api/v1/admin/uploads/` 一并生效），再在 `location /media/` 内与新增的 `location = /api/v1/health` 内写 `allow all;` 放行（见下方片段）；② 前置公司 SSO 网关，nginx 不改。**`/media/` 与 `GET /api/v1/health` 必须保持公网可匿名访问**（zhiqiapi 拉取参考素材、负载均衡探活）。

```nginx
        # 方案①：放在 server {} 内 listen 之后；/media/ 与 /api/v1/health 单独放行
        allow 10.0.0.0/8;               # 办公网段，按实际填写
        deny all;

        location = /api/v1/health {
            allow all;
            proxy_pass http://api_server;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
        location /media/ {
            allow all;
            # 其余与 §4.2 相同
        }
```

- `GET /api/v1/health` 返回 503 表示 db / redis 不可用；外部拨测只看 HTTP 状态码，不需要令牌。
- 媒体文件在 `oss` 模式下前端与 zhiqiapi 使用的 `media_assets.url` 就是 `OSS_PUBLIC_BASE_URL` 直链（§5.1），nginx 的 `/media/` 只作为兜底。

## 5. `PUBLIC_BASE_URL` 与公网素材 URL

### 5.1 为什么必须公网可达

zhiqiapi 的图生图（`POST /v1/images/edits` 的 `image` 字段、异步生成的 `reference_image_urls`）与图生视频（`input_reference`、`first_frame_image_url` 等）只接受**公网可访问的 URL 字符串**，不收文件或 Base64。本系统上传的参考图 / 参考视频以及生成后转存的素材，其 URL 由 `storage.public_url_for(key)` 生成：

| `STORAGE_MODE` | 素材 URL | 谁来提供文件 |
| --- | --- | --- |
| `local` | `PUBLIC_BASE_URL + /media/{key}` | nginx `/media/` → server `GET /media/{key}` → `media_data` 卷 |
| `oss` | `OSS_PUBLIC_BASE_URL + /{key}` | 对象存储 / CDN（桶需允许匿名读；首版不支持签名 URL，因为 URL 会永久落库并被上游拉取） |

该 URL 在转存 / 上传成功时写入 `media_assets.url`（`thumbnail_url` 同理），**之后修改两个基址变量不会改写历史行**。

### 5.2 取值规则

| 场景 | `PUBLIC_BASE_URL` 允许值 | 说明 |
| --- | --- | --- |
| Mock 模式（`ZHIQI_API_KEY` 空） | `http://127.0.0.1:8100`、`http://localhost`、任意内网地址 | Mock 占位文件由 `MockZhiqiClient.stream_download` 直接复制本地文件，不依赖此地址可达；参考 URL 校验放行 |
| 真实模式 | 公网可解析且可访问的域名 / IP：`https://aicreat.example.com`；`http://<公网IP>:${NGINX_HTTP_PORT}` 仅在 `DEV_MODE=true`（预发临时验证）下可用，`DEV_MODE=false` 的生产环境只能是 `https`（上传素材 URL 继承该 scheme，http 会被参考 URL 校验拒绝） | 主机解析为非公网地址时启动日志 warning，`GET /api/v1/health` 的 `warnings[]` 持续提示 |
| 任何模式禁止 | `http://nginx`、`http://server:8000`（compose 服务名）、`127.0.0.1` / `localhost`（真实模式）、带尾部 `/` | 服务名只在 compose 网络内可解析；浏览器与 zhiqiapi 都无法访问 |

真实模式下，`POST /admin/uploads/image` / `video` 的响应带 `public: true|false`；`public=false` 表示 URL 非公网，图生图 / 图生视频提交会被 API 以业务码 4222（`CODE_PUBLIC_URL_REQUIRED`，`data.urls[]`）拒绝。`DEV_MODE=false` 时参考 URL 还必须是 `https`。

### 5.3 上线前验证

```bash
# 1) 从一台与部署主机无关的公网机器验证素材可匿名下载（本地模式）
curl -I https://aicreat.example.com/media/mock/placeholder.png      # 期望 200，Content-Type: image/png
# 2) oss 模式：验证 CDN 直链
curl -I https://cdn.example.com/media/images/2026/10/<key>.png
# 3) 健康接口无 PUBLIC_BASE_URL 警告
curl -s https://aicreat.example.com/api/v1/health | jq '.data.warnings'   # 期望 []
# 4) 后台上传一张参考图，响应 public 应为 true
```

### 5.4 更换域名 / 迁移存储

1. 修改 `PUBLIC_BASE_URL` / `OSS_PUBLIC_BASE_URL` 并 `docker compose up -d`（新素材立即使用新基址）。
2. 旧素材保持旧地址可访问，或按前缀批量改写历史行（先备份）：

```sql
UPDATE media_assets
   SET url = REPLACE(url, 'http://old.example.com/media/', 'https://cdn.example.com/'),
       thumbnail_url = REPLACE(thumbnail_url, 'http://old.example.com/media/', 'https://cdn.example.com/')
 WHERE url LIKE 'http://old.example.com/media/%';
```

3. `local → oss` 迁移：把 `media_data` 卷内文件按原 `storage_key`（如 `media/images/2026/10/xxx.png`）原样上传到桶，再执行上面的改写；`storage_key` 不变。

## 6. 构建产物

| 产物 | 命令 | 输出 | 说明 |
| --- | --- | --- | --- |
| 后端镜像 `aicreat-server` | `docker compose build server` | 本地镜像（标签固定 `aicreat-server:<git short sha>`，`docker tag` 后推送私有仓库便于回滚） | 入口 gunicorn；同一镜像以不同 `command` 运行 worker / monitor-worker；不含 ffmpeg；含 `curl`、`tzdata`（pip）、`mock_assets/` |
| 共享包 `@aicreat/shared` | `pnpm build:shared` | `packages/shared/dist/` | 类型 / 枚举 / 常量，供 admin 构建 |
| 管理后台 | `pnpm build:admin`（= `pnpm --filter admin build`） | `apps/admin/dist/`（`index.html` + `assets/*.[hash].js\|css`） | `base: /admin/`，请求相对路径 `/api`、`/media`，无需构建期环境变量；由 nginx 只读挂载 |
| 全部前端 | `pnpm build`（= `pnpm -r build`） | 同上 | CI 一次性构建 |
| 迁移脚本 | 随镜像 | `server/migrations/versions/0001_initial.py`、`0002_seed_permissions.py` | 由发布步骤执行 |

构建环境：Node.js 20 LTS（最低 18）、pnpm@9（`corepack enable`）、Docker 24+ / Compose v2。依赖锁定：仓库根的 `pnpm-lock.yaml` 是提交到版本库的文件（首次 `pnpm install` 生成后提交，之后依赖变更时随 `package.json` 一起提交）；发布机、CI 等生产 / 镜像构建一律用 `pnpm install --frozen-lockfile`（锁文件缺失或与 `package.json` 不一致时直接失败，构建时不会改写锁文件或升级依赖），本机开发用 `pnpm install`（[06-getting-started](./06-getting-started.md)）。`GET /api/v1/health` 的 `version` 取后端包版本（`server/pyproject.toml` 的 `aicreat-server` version），发版时同步递增。

## 7. 发布流程

```mermaid
flowchart LR
    Tag["拉取代码 / 打 tag"] --> Build["构建镜像 + 前端产物"]
    Build --> Backup["备份 MySQL<br/>scripts/db-backup.sh"]
    Backup --> Migrate["迁移 + seed<br/>compose run server"]
    Migrate --> Up["docker compose up -d"]
    Up --> Verify["校验 health / ai health /<br/>模型同步 / 路由测试"]
    Verify -->|"异常"| Rollback["回滚 §8.4"]
    Verify -->|"正常"| Watch["观察 30 分钟监控指标 §10"]
```

### 7.1 常规发布步骤

| 步 | 命令 / 动作 | 校验 |
| --- | --- | --- |
| 1 | `git fetch && git checkout <tag>` | — |
| 2 | `pnpm install --frozen-lockfile && pnpm build:shared && pnpm build:admin` | `apps/admin/dist/index.html` 存在；报锁文件缺失或过期时不要在发布机去掉 `--frozen-lockfile`，回开发机 `pnpm install` 后提交 `pnpm-lock.yaml`（§6） |
| 3 | `docker compose build server` | 镜像构建成功 |
| 4 | `bash scripts/db-backup.sh` | `backups/` 出现当日文件（§9.1） |
| 5 | `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"` | 输出 `alembic` 升级到的 revision；seed 为幂等 upsert |
| 6 | `docker compose up -d` | `docker compose ps` 全部 `running`，server `healthy` |
| 7 | `curl -s http://127.0.0.1:${NGINX_HTTP_PORT}/api/v1/health` | `status` 为 `ok`，`workers.worker.alive` 与 `workers.monitor_worker.alive` 为 `true`（启动后 90s 内） |
| 8 | 后台登录 → 「AI 网关 → 模型目录」页点「同步」（`POST /admin/ai/models/sync`）→ 「AI 网关 → 能力路由」页对主路由执行一键测试（`POST /admin/ai/routes/{id}/test`） | 模型目录非空、探测 `healthy` |
| 9 | 浏览器打开 `/admin/`，强制刷新 | 新版本静态资源（hash 变化）已生效 |
| 10 | 观察 §10 指标 30 分钟 | 无新增 `critical` 告警、队列无持续积压 |

### 7.2 首次部署补充

1. `cp .env.example .env`，按 §2.3 填写必改项；真实模式先在 zhiqiapi 后台确认模型 ID 并填入 `ZHIQI_*_DEFAULT_MODEL`。
2. 执行 §7.1 第 2~7 步。首次启动时 server / worker 在 `lock:bootstrap` 内写入默认 `settings`、全局 `capability_routes` 与 RBAC 种子；`seeds/seed.py` 写入超管 `admin/admin123`、示例项目、系统 Prompt 模板（zh-CN）与默认平台（`zhihu` / `wechat_mp` / `xiaohongshu` / `csdn` / `toutiao` / `baijiahao` / `website` / `other`）。
3. 登录后立即修改超管密码（之后 `.env` 里的 `SEED_ADMIN_PASSWORD` 不再能登录，冒烟脚本须用 `--password` 传入新密码，§3.4）；在「系统配置」确认 `monitoring_config`、`geo_engines`（真实模式需为每个启用引擎填写联网模型 ID）、`seo_providers`、`alert_config`。
4. 预发环境先以 Mock 模式执行冒烟脚本全流程：`docker compose exec server python scripts/integration_smoke.py --base-url http://127.0.0.1:8000 --password '<第 3 步设置的新密码>'`（参数与覆盖范围见 §3.4），再切换真实 Key 重复 §7.1 第 8 步。

### 7.3 发布期间的行为

- `docker compose up -d` 只重建镜像或配置发生变化的容器；server 重建期间 nginx 返回 502 数秒（首版单副本，接受秒级中断；需要零中断时再引入双 server 容器 + nginx 多 upstream）。
- worker 重建时在飞任务的处理见 §3.2「停机」；轮询中的媒体任务（`polling`）不受影响，新 worker 按 `next_poll_at` 继续。
- 发布后第一轮 `sync_models`（启动立即执行）会刷新 `ai_models`，如上游下架了路由中的模型会触发 `ai_breaker_open(reason=model_unavailable)` 告警，这是预期行为，按告警处理即可。

## 8. 数据库迁移与回滚

### 8.1 初始化数据的分工

| 来源 | 内容 | 幂等方式 | 执行时机 |
| --- | --- | --- | --- |
| `migrations/versions/0001_initial.py` | 全部 24 张表与索引 | Alembic 版本表 | `alembic upgrade head` |
| `migrations/versions/0002_seed_permissions.py` | 权限码（`admin_permissions`）与系统用户组 `super_admin` / `operator` / `reviewer` / `read_only`（含默认数据范围 `data_scope`，见 [13-user-data-scope](./13-user-data-scope.md)） | `INSERT … ON DUPLICATE KEY UPDATE` | 同上 |
| `ensure_rbac_seed` / `ensure_default_settings` / `ensure_default_routes` | 权限码补齐、9 个配置键默认值（`generation_config` / `media_config` / `monitoring_config` / `geo_engines` / `seo_providers` / `alert_config` / `ai_routing_config` / `stats_config` / `system_info`，含环境变量派生初值，键清单见 [03-data-model](./03-data-model.md)）、8 条全局 `capability_routes` | 「键不存在则插入」，在 `lock:bootstrap` 内 | 每次 server / worker / monitor-worker 启动 |
| `seeds/seed.py` | 超管（`SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD`）、示例项目（负责人为该超管）、系统 Prompt 模板（`sys_*`，zh-CN）、默认平台与删除特征规则 | upsert（已改密的超管不会被覆盖） | 发布步骤 |

### 8.2 命令

```bash
# 应用迁移（compose 内）
docker compose run --rm server alembic upgrade head
# 查看当前版本 / 历史
docker compose run --rm server alembic current
docker compose run --rm server alembic history --verbose
# 回滚一步
docker compose run --rm server alembic downgrade -1
# 生成新迁移（开发机，server/ 目录，先确认 models.py 已改）
alembic revision --autogenerate -m "add_xxx"
```

### 8.3 规则

- 迁移必须向后兼容，顺序为「先加列 / 加表 / 加索引 → 发布新代码 → 下一版本再删旧列」；新列给默认值或可空，JSON 列（`*_json`）新增键由 service 层以默认值兼容，不写迁移。
- 大表（`ai_tasks`、`link_checks`、`index_checks`、`ai_usage_logs`、`daily_stats`）加索引用 MySQL 8 在线 DDL（`ALGORITHM=INPLACE, LOCK=NONE`），避免长时间锁表；上线前用生产快照在预发演练并记录耗时。
- `settings` 配置键带 `version` 字段：新版本新增键或字段时靠深合并默认值兼容；需要改变语义时在 `ensure_default_settings` 中按 `version` 升级并保留管理员改动，不直接覆盖。
- `alembic autogenerate` 对 `TEXT` JSON 列与 `DECIMAL` 精度可能产生无意义的差异，提交前人工审阅迁移脚本。
- 用户系统（数据范围）的列与索引已并入 `0001_initial` / `0002_seed_permissions`；若数据库按此前的文档版本建成，按 [13-user-data-scope](./13-user-data-scope.md) §14 另写一次性迁移（`admin_groups.data_scope`、`projects.owner_id` 回填并改非空、按负责人的唯一索引、`media_assets` 新索引），上线前由总后台核对各项目负责人。该迁移不满足上条「向后兼容」规则（`projects.owner_id` 改非空、删除 `uq_projects_name` / `uq_projects_slug`），不走 §7.1 第 5 步的在线迁移：先 `bash scripts/db-backup.sh`，再 `docker compose stop server worker monitor-worker`，然后执行 `docker compose run --rm server sh -c "alembic upgrade head && python seeds/seed.py"`，完成后清除 Redis `cache:stats:*`（统计缓存键已加入 `scope_key` 段，见 13 §14 第 5 步），最后 `docker compose up -d`。回滚时不适用 §8.4「仅代码问题」（旧代码依赖全局唯一与可空的 `owner_id`），须按「迁移不可逆或数据已损坏」用备份恢复。
- Redis 没有迁移：所有键自带 TTL 或可重建。若发布涉及键格式变化，可在停止三个应用容器后执行 `FLUSHDB`，后果与恢复方式见下表。

| 被清空的键 | 后果 | 自动恢复 |
| --- | --- | --- |
| `queue:ai_tasks` | 排队中的根任务暂时不被领取 | `recover_stale_tasks` ③：`queued` 超过 10 分钟且不在队列 → 重新 `RPUSH` |
| `queue:link_checks` / `queue:index_checks` | 到期检测延迟 | 调度入队（`baseline` / `scheduled` / `retry`）时 `next_check_at` / `next_index_check_at` 已推后 1 小时，调度器下轮重新入队；`manual` 元素不触碰排程、被清空即丢失，需重新点「立即检测」或 `POST /admin/monitoring/link-checks/run` / `index-checks/run` |
| `queue:stats_recompute` | 未执行的重算请求丢失 | 需重新调用 `POST /admin/stats/recompute` |
| `quota:daily:{date}` / `limit:*:{date}` | 当日计数归零，上限判断偏松 | 次日自然恢复 |
| `quota:project:{project_id}:{yyyy-mm}` | 当月项目计数归零，项目月额度判断在当月剩余时间偏松 | 次月自然恢复 |
| `ai:breaker:*` / `ai:paused:*` / `ai:health:*` | 熔断与暂停状态清空 | 下一次失败 / 探测重建；告警记录仍在 MySQL |
| `stats:rt:*` | 今日实时兜底计数归零 | 下一次 `aggregate_today()`（≤ 600s）后总览以 `daily_stats` 为准 |
| `cache:*`、`rate:*`、`queued:*`、`lock:*` | 缓存与去重标记 | 立即重建；锁丢失只影响正在执行的任务（有 DB 双保险） |
| `alert:cooldown:*` | 通道投递冷却清空 | 同 `dedupe_key` 的告警可能在冷却期内多投递一次；无需处理 |
| `ai:usage:last_pull` | 用量页「最近拉取」摘要为空 | 下一次 `reconcile_usage`（≤ `usage.reconcile_interval_seconds`）重建 |
| `domain:last_fetch:*` / `robots:*` | 同域名间隔与 robots 缓存清空 | 下一次抓取重建；清空瞬间可能对同一域名连续抓取 2 次 |
| `mock:task:*` / `mock:usage_logs`（仅 Mock 模式） | 轮询中的 Mock 媒体任务查无此任务（404）；伪用量日志清空 | `polling` 根任务连续 3 次 404 → `failed(route_missing)`，资产 `failed`，需 `POST /admin/media/assets/{id}/retry`；对账只影响已清空的伪日志（`quota_reconciled_rate` 下降），真实模式不存在此项 |

### 8.4 回滚

| 场景 | 步骤 |
| --- | --- |
| 仅代码问题（最常见） | `git checkout <上一个 tag>` → `pnpm install --frozen-lockfile && pnpm build:shared && pnpm build:admin`（按旧 tag 的 `pnpm-lock.yaml` 重装依赖）→ `docker compose build server` → `docker compose up -d`（有私有镜像仓库时直接改用旧标签镜像，免重建）；数据库保持新版本 schema（向后兼容） |
| 新迁移本身有缺陷 | 先停应用容器 `docker compose stop server worker monitor-worker` → `docker compose run --rm server alembic downgrade -1`（仅当该迁移写了可逆的 `downgrade`）→ 回退代码 → `up -d` |
| 迁移不可逆或数据已损坏 | 用 §9.1 备份恢复到发布前时间点（binlog 可做到分钟级），再回退代码；期间产生的 AI 调用成本以 zhiqiapi 用量日志为准，回滚后对账会把未匹配条目保留在 `ai_usage_logs`（`ai_task_id IS NULL`） |
| 配置键改动 | 后台「系统配置」直接改回；无需回滚代码 |

回滚后同样执行 §7.1 第 7~8 步校验。

## 9. 备份与数据保留

### 9.1 MySQL

`scripts/db-backup.sh`（每日全量，保留 14 天；宿主机 cron `30 3 * * * cd /opt/aicreat && bash scripts/db-backup.sh`）：

```bash
#!/usr/bin/env bash
# mysqldump 到 backups/，保留 14 天。
# 凭据取 mysql 容器自身的环境变量（compose 已注入 MYSQL_ROOT_PASSWORD / MYSQL_DATABASE），
# 宿主机不 source .env（.env 含带空格 / 括号的值，直接 source 会报语法错误）。
set -euo pipefail
cd "$(dirname "$0")/.."
db=$(grep -E '^MYSQL_DATABASE=' .env | head -n1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^"//; s/"$//')
db=${db:-aicreat}
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
out="backups/${db}-${stamp}.sql.gz"
docker compose exec -T mysql sh -c \
  'exec mysqldump --single-transaction --quick --routines --triggers -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  | gzip > "$out"
find backups -name "${db}-*.sql.gz" -mtime +14 -delete
echo "backup written: $out"
```

- 开启 binlog（`mysql` 服务 `command` 追加 `--log-bin=mysql-bin --binlog-expire-logs-seconds=604800`，保留 7 天）以支持时间点恢复；备份文件每日同步到异地对象存储（与 `media_data` 备份同一桶，§9.2）。
- 恢复：`gunzip -c backups/aicreat-<stamp>.sql.gz | docker compose exec -T mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'`，然后 `docker compose restart server worker monitor-worker`（恢复前先 `docker compose stop server worker monitor-worker`，避免恢复期间写入）。
- 恢复后 Redis 中的队列 / 计数与数据库不一致，恢复 MySQL 后、重启应用容器前执行一次 `docker compose exec redis redis-cli FLUSHDB`（后果与自动恢复见 §8.3 表）。

### 9.2 素材与对象存储

| 模式 | 备份对象 | 要求 |
| --- | --- | --- |
| `local` | `media_data` 卷（`docker run --rm -v aicreat_media_data:/data -v $PWD/backups:/backup alpine tar czf /backup/media-$(date +%F).tgz -C /data .`；卷全名 = compose 项目名（默认为目录名 `aicreat`）+ `_media_data`，以 `docker volume ls` 为准） | 生成的图片 / 视频**无法从上游重新获取**（临时 URL 过期），卷即唯一副本，必须每日备份（与 `db-backup.sh` 同一 cron，`35 3 * * *`） |
| `oss` | 桶 | 开启版本管理与跨区域复制；生命周期规则不得早于 `media_config.retention` 与业务保留期删除对象 |

`cleanup_media`（每日 03:00，`stats_config.timezone`）只删除 `failed` / `expired` 超过 `retention.failed_days=30` 的资产缓存文件，以及超过 `retention.orphan_reference_days=7` 且未被任何任务引用的上传参考素材；`ready` 素材不会被自动删除。

### 9.3 Redis

- `--appendonly yes`（默认 `appendfsync everysec`）+ `redis_data` 卷：重启最多丢 1 秒写入，队列元素丢失由 §8.3 表中的机制补扫。
- 不做持久化也能运行，但会在每次重启时触发一轮补扫与计数归零；生产保持 AOF。

### 9.4 保留期与清理

| 数据 | 保留 / 清理机制 | 位置 |
| --- | --- | --- |
| `daily_stats` | `stats_config.retention_days=730` | monitor-worker 聚合任务按配置清理 |
| `ai_usage_logs` 未匹配条目 | `ai_routing_config.usage.unmatched_retention_days=30` | `reconcile_usage` |
| `media_assets` 失败 / 孤儿文件 | `media_config.retention.failed_days=30` / `orphan_reference_days=7` | `cleanup_media` |
| `ai_tasks` / `link_checks` / `index_checks` / `admin_operation_logs` | 首版不自动清理（报表与审计依赖）；每年 1 月由运维把 `created_at` 早于 1 年前的记录归档到历史库后再删除，删除前先对受影响日期执行 `POST /admin/stats/recompute`（≤ 31 天一批）确认 `daily_stats` 已固化；`link_checks` / `index_checks` 只归档 `alive_status=deleted` 且 `alive_changed_at` 早于 30 天前的链接的记录（物理删除的链接其记录已随外键级联删除；快照列重算依赖检测历史，§8.3） | 运维脚本 |
| 容器日志 | compose `json-file` 驱动 `max-size 50m × max-file 5` | docker |
| 备份文件 | 14 天（`db-backup.sh`） | `backups/` |

### 9.5 密钥

`.env` 与仓库根 `secrets/`（服务账号 JSON）不入版本库（`.gitignore`）；服务账号 JSON 以只读文件挂载；镜像不包含 `.env`（`server/.dockerignore`；`secrets/` 不在构建上下文内）；`ZHIQI_API_KEY` 泄露时在 zhiqiapi 后台重置并更新 `.env` → `docker compose up -d`，无需改库（密钥不入库）。

## 10. 监控指标

### 10.1 数据来源

| 来源 | 内容 | 适用 |
| --- | --- | --- |
| `GET /api/v1/health`（公开） | `{status, db, redis, zhiqi_mode, workers:{worker:{alive,replicas,heartbeat_at}, monitor_worker:{…}}, warnings[], version}`；db / redis 不可用 → 503 | 负载均衡探活、外部拨测 |
| `GET /admin/ai/health` | `zhiqi_mode`、`base_url`、`paused:{quota_exceeded,auth_failed}`、`models[]{capability,model,status,latency_ms,checked_at,breaker_state,breaker_reason,is_available}`、`workers[]` | zhiqiapi 可用性、熔断状态 |
| `GET /admin/monitoring/overview` | `link_checks:{due,queued,today,last_run_at}`、`index_checks:{…}`、`workers[]`、`daily_limits` | 检测积压与日上限 |
| `GET /admin/alerts/summary` | `open:{info,warning,critical}`、`acknowledged`、`today_opened`、`today_resolved` | 告警总量 |
| `GET /admin/stats/overview` / `GET /admin/ai/usage/summary` | 报表指标（`ai_success_rate`、`quota_reconciled_rate`、`seo_index_rate`、`link_alive_rate` …，公式见 [12-dashboard-reports](./12-dashboard-reports.md)） | 业务趋势 |
| Redis | `LLEN queue:*`、`SCAN` 匹配 `worker:heartbeat:*` / `ai:paused:*` / `ai:breaker:*`（生产禁用 `KEYS`）、`GET ai:usage:last_pull`、`GET limit:*:{date}` | 外部监控系统采集 |
| MySQL | §10.3 的 SQL | 外部监控系统采集 |
| 日志 | `docker compose logs`；每次上游调用的 `x-oneapi-request-id` 记入 `ai_tasks.request_id` 并打印到日志，排障时以此向 zhiqiapi 查询 | 排障 |

### 10.2 指标与阈值

| 分组 | 指标 | 来源 | 阈值 / 动作 |
| --- | --- | --- | --- |
| 进程 | `health.status != ok`、`db`/`redis` 为 false、容器重启次数 | `/api/v1/health`、`docker compose ps` | 503 或 5 分钟内重启 ≥ 3 次 → 立即处理 |
| 进程 | worker / monitor_worker `alive=false`；`worker_stale` 告警（副本心跳落后 > 5 分钟或无存活副本） | `/api/v1/health`、`alerts` | 持续 2 分钟 → 查容器日志 / 重启 |
| 进程 | 磁盘：`media_data`、`mysql_data`、`redis_data` 使用率 | 宿主机 | > 80% 告警 |
| 接口 | nginx 5xx 比例、`/api/` p95 延迟、业务码 500 / 5021 / 5031 计数 | nginx access log、server 日志 | 5xx > 1% 或 5031 持续出现 → 看 zhiqiapi 可用性 |
| **zhiqiapi 可用性** | `models[].status` 为 `down` / `degraded` 的 `(capability, model)` 数；`breaker_state=open` 数与 `breaker_reason`（`failures` / `model_unavailable` / `probe_down` / `manual`） | `/admin/ai/health`、`ai:breaker:*` | 任一主模型 `down` 或 `open` → `ai_breaker_open` / `ai_upstream_unavailable` 告警，检查 zhiqiapi 状态、切换备选模型 |
| zhiqiapi 可用性 | `ai:paused:quota_exceeded` / `ai:paused:auth_failed` 存在（全局暂停） | `/admin/ai/health.paused` | 立即处理：充值 / 换 Key 后 `POST /admin/ai/routes/{id}/reset-breaker` 删除暂停键 |
| zhiqiapi 可用性 | `ai_success_rate`（按尝试行）、`ai_failures_by_category`、探测延迟 `latency_ms` | `/admin/stats/overview`、`/admin/ai/tasks`、`ai:health:*` | 1 小时成功率 < 90% 或延迟 ≥ `health.degraded_latency_ms`（15s）持续 → 调整路由 / 超时 |
| **任务积压** | `LLEN queue:ai_tasks`；`ai_tasks` 根任务 `queued` 数与最老 `queued` 年龄；`running` 数 | Redis、SQL ① | 队列 > 50 或最老排队 > 10 分钟 → 扩容 worker / 检查 `ai:paused:*` 与信号量上限 |
| 任务积压 | `polling` 且 `next_poll_at` 逾期 > 5 分钟的媒体根任务数；`downloading` 且 `next_transfer_at` 逾期的资产数 | SQL ② | > 0 持续 10 分钟 → worker 线程池饱和或出网故障 |
| 任务积压 | `generation_batches` `running` 超过 30 分钟数 | SQL ⑧ | > 0 → 查对应根任务 `error_category`；`heartbeat_at` 超 30 分钟且无活动根任务的批次由 `recover_stale_tasks` ④ 收敛为 `partial` |
| 任务积压 | `LLEN queue:link_checks` / `queue:index_checks`；到期未检测链接数（`next_check_at` 逾期 > 2 小时） | `/admin/monitoring/overview`、SQL ③ | 逾期 > 100 → monitor-worker 并发 / 日上限（`limit:link_checks:{date}` 达到 `link_check.daily_limit=5000`）问题 |
| **对账差异** | `quota_reconciled_rate`（近 7 天，按尝试行）；`ai:usage:last_pull.window_overflow`、`unmatched`；`now − pulled_at` | `/admin/stats/overview`、Redis | 对账率 < 80% 或 `window_overflow=true` → 调用量超过上游 1000 条窗口，调小 `usage.reconcile_interval_seconds`（最低 `min_interval_seconds=60`）；`pulled_at` 落后 > 2 × 间隔 → 对账任务异常 |
| 对账差异 | `Σ quota_actual − Σ quota_estimated` 按模型偏差率；`ai_usage_logs` 未匹配条目数（`ai_task_id IS NULL`，近 7 天） | SQL ④ | 偏差 > 20% → 价格快照过期，执行 `POST /admin/ai/models/sync`；未匹配 `type=2` 条目持续增长 → 有本系统之外的调用在使用同一 Key |
| 对账差异 | `quota:daily:{date}` 相对 `generation_config.quota.daily_limit`、各项目 `quota:project:{project_id}:{yyyy-mm}` 相对 `generation_config.quota.project_monthly_limit` 的使用率（以生效配置为准；`AI_DAILY_QUOTA_LIMIT` / `AI_PROJECT_MONTHLY_QUOTA_LIMIT` 只在配置键首次写入时 seed，§2.4）；`cost_cny` 日趋势 | Redis、`/admin/settings/generation_config`、`/admin/stats/trends` | 日额度或任一项目月额度使用率达 `generation_config.quota.warn_percent=80` → 通知负责人；上限（`daily_limit` / `project_monthly_limit`）为 0 表示不限，对应范围不适用 |
| **检测失败率** | 删除检测 `unknown` 占比（按 `matched_rule`：`network_error` / `ssrf_blocked` / `blocked_by_robots`）；`duration_ms` p95 | SQL ⑤ | 24 小时 `unknown` > 20% → 出网 / 目标平台封禁 / `MONITOR_*` 参数问题 |
| 检测失败率 | 收录检测 `unknown` 占比（按 `kind` / `engine` / `provider` 与 `error_category`） | SQL ⑥、`/admin/monitoring/index-checks` | 某引擎 `unknown` > 30% → 该引擎模型不可用 / 凭据缺失（`auth_failed` + `credential_missing`）/ 熔断 |
| 检测失败率 | `index_overdue`、`link_deleted` 告警数；`seo_index_rate`、`geo_cite_rate`、`time_to_index_hours_avg`、`link_alive_rate` | `alerts`、`/admin/stats/overview` | 业务指标，按项目周报跟踪 |
| 媒体 | `media_failed`、`media_success_rate`；`images_generated` / `videos_generated` 相对 `media_config.daily_limits` | `/admin/stats/overview` | 成功率 < 80% → 看 `media_task_failed` 告警的 `error_category`（`media_storage` / `transfer_failed` / `timeout`） |
| 统计 | `daily_stats` 昨日 `total` 行是否存在、`computed_at` 是否在 `stats_config.daily_at=00:30` 之后 | SQL ⑦ | 01:00 后仍缺失 → `POST /admin/stats/recompute` 并查 `lock:monitor:daily_stats:{date}` 与 monitor-worker 日志 |

### 10.3 采集 SQL / Redis 命令

```sql
-- ① AI 任务积压（根任务）
SELECT status, COUNT(*) AS n,
       TIMESTAMPDIFF(MINUTE, MIN(created_at), UTC_TIMESTAMP()) AS oldest_minutes
  FROM ai_tasks
 WHERE root_task_id IS NULL AND status IN ('queued', 'running', 'polling')
 GROUP BY status;

-- ② 轮询 / 转存逾期
SELECT (SELECT COUNT(*) FROM ai_tasks
         WHERE root_task_id IS NULL AND status = 'polling'
           AND next_poll_at < UTC_TIMESTAMP() - INTERVAL 5 MINUTE) AS polling_overdue,
       (SELECT COUNT(*) FROM media_assets
         WHERE status = 'downloading'
           AND next_transfer_at < UTC_TIMESTAMP() - INTERVAL 10 MINUTE) AS transfer_overdue;

-- ③ 到期未检测的链接
SELECT SUM(next_check_at < UTC_TIMESTAMP() - INTERVAL 2 HOUR)       AS link_checks_overdue,
       SUM(next_index_check_at < UTC_TIMESTAMP() - INTERVAL 2 HOUR) AS index_checks_overdue
  FROM publish_links
 WHERE is_monitoring = 1;

-- ④ 对账差异（近 7 天，按尝试行、按模型）
SELECT model, COUNT(*) AS calls,
       SUM(reconciled_at IS NOT NULL) AS reconciled,
       SUM(quota_estimated) AS quota_estimated, SUM(COALESCE(quota_actual, 0)) AS quota_actual,
       SUM(cost_cny) AS cost_cny
  FROM ai_tasks
 WHERE root_task_id IS NOT NULL AND status IN ('succeeded', 'failed')
   AND trigger_type <> 'health_probe'
   AND finished_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY
 GROUP BY model;
SELECT COUNT(*) AS unmatched_consumption
  FROM ai_usage_logs
 WHERE ai_task_id IS NULL AND log_type = 2 AND pulled_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY;

-- ⑤ 删除检测失败率（24 小时）
SELECT result_status, matched_rule, COUNT(*) AS n,
       ROUND(AVG(duration_ms)) AS avg_ms
  FROM link_checks
 WHERE checked_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY
 GROUP BY result_status, matched_rule;

-- ⑥ 收录检测失败率（24 小时）
SELECT kind, engine, provider, COUNT(*) AS checks,
       ROUND(SUM(result_status = 'unknown') / COUNT(*), 3) AS unknown_rate,
       SUM(error_category = 'auth_failed') AS auth_failed
  FROM index_checks
 WHERE checked_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY
 GROUP BY kind, engine, provider;

-- ⑦ 昨日 daily_stats 是否已固化（stat_date 按 stats_config.timezone 切日；示例按 Asia/Shanghai = +08:00）
SELECT stat_date, computed_at
  FROM daily_stats
 WHERE dimension = 'total' AND project_id = 0
   AND stat_date = DATE(DATE_SUB(CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+08:00'), INTERVAL 1 DAY));
-- 期望 1 行且 computed_at >= 当日 00:30（UTC 为前一日 16:30）

-- ⑧ 运行超过 30 分钟的生成批次
SELECT id, project_id, kind, task_total, task_done, task_failed, started_at, heartbeat_at
  FROM generation_batches
 WHERE status = 'running' AND started_at < UTC_TIMESTAMP() - INTERVAL 30 MINUTE;
```

```bash
# 队列长度
docker compose exec redis redis-cli LLEN queue:ai_tasks
docker compose exec redis redis-cli LLEN queue:link_checks
docker compose exec redis redis-cli LLEN queue:index_checks
docker compose exec redis redis-cli LLEN queue:stats_recompute
# 心跳、全局暂停、熔断、对账摘要、当日上限
docker compose exec redis redis-cli --scan --pattern 'worker:heartbeat:*'
docker compose exec redis redis-cli --scan --pattern 'ai:paused:*'
docker compose exec redis redis-cli --scan --pattern 'ai:breaker:*'
docker compose exec redis redis-cli GET ai:usage:last_pull
docker compose exec redis redis-cli GET limit:link_checks:2026-10-06
```

### 10.4 告警投递

- 站内告警（`in_app`）固定开启；生产环境打开 `alert_config.channels.webhook`（`enabled=true`、`min_severity=warning`）并配置 `ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_SECRET`：POST JSON，头 `X-Aicreat-Signature: sha256=<HMAC-SHA256(body, secret)>`，接收端据此验签后转发到 IM / 值班系统；同一 `dedupe_key` 在 `dedupe_cooldown_minutes=60` 内不重复投递。邮件通道（`email`）首版预留。
- 需要人工处置的告警：`ai_quota_exceeded`、`ai_auth_failed`（解除全局暂停）、`media_task_failed`（`POST /admin/media/assets/{id}/retry` / `transfer`）、`link_deleted`（联系平台或重新发布后回填新链接）。其余告警在条件恢复后自动解决（规则见 [11-link-backfill-and-monitoring](./11-link-backfill-and-monitoring.md)）。
- 外部监控系统（Prometheus / Zabbix 等）首版通过定时执行 §10.3 的命令采集；不提供 `/metrics` 端点。

## 11. 上线检查清单

| # | 检查项 | 通过标准 |
| --- | --- | --- |
| 1 | `.env` 必改项（§2.3）全部修改，`.env` 未提交到版本库 | 人工核对 |
| 2 | `docker compose config` 渲染正确（变量插值无空值告警） | 无 warning |
| 3 | `PUBLIC_BASE_URL` / `OSS_PUBLIC_BASE_URL` 公网验证（§5.3） | `curl -I` 200；health `warnings` 为 `[]` |
| 4 | 出网放行：三个容器可访问 `ZHIQI_BASE_URL`；worker 可下载任意公网 HTTPS；monitor-worker 可抓取 80/443 | `docker compose exec worker sh -c 'curl -s -o /dev/null -w "%{http_code}\n" "$ZHIQI_BASE_URL/models" -H "Authorization: Bearer $ZHIQI_API_KEY"'` 输出 `200`（变量在容器内展开）；`docker compose exec monitor-worker curl -s -o /dev/null -w "%{http_code}\n" https://www.zhihu.com/` 输出非 `000` |
| 5 | 迁移 + seed 成功；`admin` 已改密 | `alembic current` 为 head |
| 6 | `GET /api/v1/health` `status=ok`，两个 worker `alive=true` | — |
| 7 | 模型目录同步成功、全部启用路由 `is_enabled=1`、主模型探测 `healthy` | `/admin/ai/health` |
| 8 | GEO 引擎 / SEO 提供器配置完成（真实模式引擎 `model` 非空） | `PUT /admin/settings/geo_engines` 不返回 400 |
| 9 | 备份任务已加入 cron 并成功执行一次；恢复流程演练过 | `backups/` 有文件 |
| 10 | 告警 webhook 验签联调通过（如启用） | 收到测试告警 |
| 11 | HTTPS 生效；`/admin/` 与 `/api/` 已加访问控制（§4.3 方案①或②）；`/media/` 与 `GET /api/v1/health` 从公网匿名可达 | 办公网外 `curl -I https://<域名>/admin/` 为 403，`curl -I https://<域名>/api/v1/health` 为 200 |
| 12 | Mock 模式冒烟（预发）与真实模式最小流程（关键词 → 标题 → 内容 → 图片 → 回填 → 手动检测）各通过一次 | `scripts/integration_smoke.py` 通过（命令见 §3.4，超管已改密时传 `--password`）；后台可见 `ready` 素材与 `alive` 链接 |
