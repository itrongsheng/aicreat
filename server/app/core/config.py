"""环境变量配置（pydantic-settings）。

字段名 = 环境变量名小写；完整清单、默认值与说明见 docs/05-deployment.md §2.2。
本机开发读取当前目录（``server/``）下的 ``.env``；docker-compose 通过 ``env_file`` 注入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# server/ 目录（app/core/config.py → parents[2]）
SERVER_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ============ 数据库 / Redis ============
    database_url: str = "mysql+pymysql://aicreat:password@127.0.0.1:3306/aicreat"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 仅 compose 使用；声明这些字段使同源的 server/.env 不触发 extra 校验错误
    mysql_root_password: str = "root-change-me"
    mysql_database: str = "aicreat"
    mysql_user: str = "aicreat"
    mysql_password: str = "password"
    gunicorn_workers: int = 2
    nginx_http_port: int = 80

    # ============ 安全与运行模式 ============
    admin_jwt_secret: str = "please-change-me-admin"
    admin_jwt_expire_seconds: int = 7200
    admin_login_max_failures: int = 5
    dev_mode: bool = True
    log_level: str = "INFO"
    app_timezone: str = "Asia/Shanghai"
    allowed_origins: str = "http://127.0.0.1:5174,http://localhost:5174"
    public_base_url: str = "http://127.0.0.1:8100"

    # ============ 存储 ============
    storage_mode: str = "local"
    storage_provider: str = "s3"
    local_storage_dir: str = "storage"
    oss_endpoint: str = ""
    oss_region: str = ""
    oss_bucket: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""
    oss_public_base_url: str = "http://127.0.0.1:8100/media"
    max_image_size_mb: int = 10
    max_video_size_mb: int = 200

    # ============ zhiqiapi 网关 ============
    zhiqi_base_url: str = "https://zhiqiapi.com/v1"
    zhiqi_api_key: str = ""
    zhiqi_user_agent: str = "aicreat/0.1"
    zhiqi_timeout_connect_seconds: int = 10
    zhiqi_timeout_text_seconds: int = 180
    zhiqi_timeout_submit_seconds: int = 60
    zhiqi_timeout_poll_seconds: int = 30
    zhiqi_timeout_download_seconds: int = 300
    zhiqi_max_retries: int = 3
    zhiqi_retry_base_seconds: float = 1.0
    zhiqi_retry_max_seconds: float = 30
    zhiqi_breaker_failure_threshold: int = 5
    zhiqi_breaker_window_seconds: int = 300
    zhiqi_breaker_open_seconds: int = 120
    zhiqi_text_default_model: str = ""
    zhiqi_text_default_protocol: str = "openai_chat"
    zhiqi_image_default_model: str = ""
    zhiqi_video_default_model: str = ""
    zhiqi_geo_default_model: str = ""
    zhiqi_seo_default_model: str = ""
    zhiqi_image_poll_budget_seconds: int = 600
    zhiqi_video_poll_budget_seconds: int = 1200
    zhiqi_image_sync_fallback: bool = True
    zhiqi_usage_reconcile_interval_seconds: int = 300
    zhiqi_models_sync_interval_seconds: int = 3600
    zhiqi_health_probe_interval_seconds: int = 600
    zhiqi_quota_per_unit: int = 500000
    zhiqi_usd_cny_rate: float = 7.2
    zhiqi_group_ratio: float = 1.0

    # ============ 生成频控 / 并发 / 配额 ============
    generate_rate_limit: str = "60/hour"
    media_rate_limit: str = "20/hour"
    ai_max_concurrency_text: int = 4
    ai_max_concurrency_image: int = 2
    ai_max_concurrency_video: int = 1
    ai_daily_quota_limit: int = 0
    ai_project_monthly_quota_limit: int = 0

    # ============ worker 进程 ============
    worker_poll_interval_seconds: float = 2
    worker_stale_task_minutes: int = 10
    monitor_poll_interval_seconds: float = 5

    # ============ 链接删除检测 ============
    monitor_user_agent: str = "aicreatLinkMonitor/1.0 (+https://example.com/contact)"
    monitor_fetch_timeout_seconds: int = 15
    monitor_max_response_bytes: int = 2097152
    monitor_max_redirects: int = 3
    monitor_concurrency: int = 4
    monitor_per_domain_interval_seconds: int = 2
    monitor_allow_http: bool = True

    # ============ SEO 收录检测提供器凭据 ============
    seo_baidu_ai_search_api_key: str = ""
    seo_bing_webmaster_api_key: str = ""
    seo_bing_site_url: str = ""
    seo_gsc_credentials_file: str = ""
    seo_gsc_site_url: str = ""

    # ============ 告警通道 ============
    alert_webhook_url: str = ""
    alert_webhook_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "no-reply@example.com"

    # ============ 初始超级管理员 ============
    seed_admin_username: str = "admin"
    seed_admin_password: str = "admin123"

    # ------------------------------------------------------------------ 校验

    @field_validator("*", mode="before")
    @classmethod
    def _empty_to_default(cls, value: Any, info: ValidationInfo) -> Any:
        """非字符串字段（数值 / 布尔）留空时回退默认值，避免 ``SMTP_PORT=`` 之类的空值启动失败。"""
        if isinstance(value, str):
            value = value.strip()
            if value == "" and info.field_name:
                field = cls.model_fields[info.field_name]
                if field.annotation is not str:
                    return field.default
        return value

    @field_validator("storage_mode", "storage_provider", "zhiqi_text_default_protocol", mode="after")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("log_level", mode="after")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @field_validator("storage_mode", mode="after")
    @classmethod
    def _check_storage_mode(cls, value: str) -> str:
        if value not in {"local", "oss"}:
            raise ValueError("STORAGE_MODE 只能为 local 或 oss")
        return value

    # ------------------------------------------------------------------ 派生属性

    @property
    def cors_origins(self) -> list[str]:
        """``ALLOWED_ORIGINS`` 按逗号拆分（去空白、去尾部斜杠、去重保序）。"""
        result: list[str] = []
        for item in self.allowed_origins.split(","):
            origin = item.strip().rstrip("/")
            if origin and origin not in result:
                result.append(origin)
        return result

    @property
    def use_local_storage(self) -> bool:
        """``STORAGE_MODE=local`` 或 ``OSS_ENDPOINT`` 为空时使用本地存储。"""
        return self.storage_mode == "local" or not self.oss_endpoint.strip()

    @property
    def zhiqi_mock_mode(self) -> bool:
        """``ZHIQI_API_KEY`` 为空 → Mock 模式。"""
        return not self.zhiqi_api_key.strip()

    @property
    def zhiqi_origin(self) -> str:
        """``ZHIQI_BASE_URL`` 去掉尾部 ``/v1``（``/api/*`` 管理接口与 Anthropic 协议使用）。"""
        base = self.zhiqi_base_url.strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base.rstrip("/")

    @property
    def media_public_base(self) -> str:
        """素材公网基址：本地模式 ``PUBLIC_BASE_URL + /media``，oss 模式 ``OSS_PUBLIC_BASE_URL``。"""
        if self.use_local_storage:
            return self.public_base_url.strip().rstrip("/") + "/media"
        return self.oss_public_base_url.strip().rstrip("/")

    @property
    def local_storage_path(self) -> Path:
        """``LOCAL_STORAGE_DIR`` 的绝对路径（相对路径按 ``server/`` 目录解析）。"""
        path = Path(self.local_storage_dir).expanduser()
        if not path.is_absolute():
            path = SERVER_DIR / path
        return path

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def jwt_secret_is_weak(self) -> bool:
        """密钥为默认值或短于 32 字节（HS256 推荐下限）。"""
        secret = self.admin_jwt_secret
        return secret == "please-change-me-admin" or len(secret.encode("utf-8")) < 32


settings = Settings()
