# aicreat 后端镜像：server / worker / monitor-worker 三个进程共用（docs/05 §3.3）
# - server：缺省 CMD（gunicorn + uvicorn workers，容器内 8000；compose 以 -w ${GUNICORN_WORKERS} 覆盖）
# - worker：python -m app.worker
# - monitor-worker：python -m app.monitor_worker
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# curl 供 compose healthcheck；ca-certificates 供 httpx / boto3 访问 HTTPS。不安装 ffmpeg。
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 先拷贝源码再安装：pip install -e . 需要 app/ 包存在（只拷 pyproject.toml 会安装出空包）；源码变更后重建镜像即可
COPY . .
RUN pip install -e .

EXPOSE 8000
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "-b", "0.0.0.0:8000", "-w", "2", "--timeout", "120"]
