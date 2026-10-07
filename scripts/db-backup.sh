#!/usr/bin/env bash
# mysqldump 到 backups/，保留 14 天（docs/05-deployment.md §9.1）。
# 宿主机 cron：30 3 * * * cd /opt/aicreat && bash scripts/db-backup.sh
# 凭据取 mysql 容器自身的环境变量（compose 已注入 MYSQL_ROOT_PASSWORD / MYSQL_DATABASE），
# 宿主机不 source .env（.env 含带空格 / 括号的值，直接 source 会报语法错误）。
# 恢复：gunzip -c backups/aicreat-<stamp>.sql.gz | docker compose exec -T mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
set -euo pipefail
cd "$(dirname "$0")/.."
# 只用于备份文件名；grep 未命中（或缺少 .env）时不因 set -e / pipefail 中断，回退 aicreat
db=$({ grep -E '^MYSQL_DATABASE=' .env 2>/dev/null || true; } | head -n1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^"//; s/"$//')
db=${db:-aicreat}
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
out="backups/${db}-${stamp}.sql.gz"
# 先写临时文件，mysqldump / gzip 任一失败即删除，避免留下截断的备份被当作有效文件
trap 'rm -f "$out.partial"' EXIT
docker compose exec -T mysql sh -c \
  'exec mysqldump --single-transaction --quick --routines --triggers -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  | gzip > "$out.partial"
mv "$out.partial" "$out"
find backups -name "${db}-*.sql.gz" -mtime +14 -delete
echo "backup written: $out"
