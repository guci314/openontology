#!/usr/bin/env bash
set -euo pipefail

DB_HOST="${DB_HOST:-jsherp-mysql}"
DB_PORT="${DB_PORT:-3306}"
DB_NAME="${DB_NAME:-jsh_erp}"
DB_USER="${DB_USER:-root}"
DB_PASS="${DB_PASS:-123456}"
REDIS_HOST="${REDIS_HOST:-jsherp-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASS="${REDIS_PASS:-1234abcd}"
SERVER_PORT="${SERVER_PORT:-9999}"

echo "[jsherp-boot] Waiting for MySQL at ${DB_HOST}:${DB_PORT}..."
for i in $(seq 1 90); do
  if mysqladmin ping -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_USER}" -p"${DB_PASS}" --silent >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_USER}" -p"${DB_PASS}" -e "CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"

HAS_TABLES="$(mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_USER}" -p"${DB_PASS}" -Nse "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}' AND table_name='jsh_user';")"
if [ "${HAS_TABLES:-0}" = "0" ]; then
  echo "[jsherp-boot] Initializing schema from /opt/jshERP/jsh_erp.sql into ${DB_NAME}..."
  mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_USER}" -p"${DB_PASS}" --default-character-set=utf8mb4 "${DB_NAME}" < /opt/jshERP/jsh_erp.sql
fi

if [ "${DISABLE_LOGIN_CAPTCHA:-1}" = "1" ]; then
  mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_USER}" -p"${DB_PASS}" "${DB_NAME}" \
    -e "UPDATE jsh_platform_config SET platform_value='0' WHERE platform_key='checkcode_flag';"
fi

JDBC_URL="jdbc:mysql://${DB_HOST}:${DB_PORT}/${DB_NAME}?useUnicode=true&characterEncoding=utf8&useCursorFetch=true&defaultFetchSize=500&allowMultiQueries=true&rewriteBatchedStatements=true&useSSL=false&allowPublicKeyRetrieval=true"

echo "[jsherp-boot] Starting jshERP.jar on port ${SERVER_PORT}..."
exec java ${JAVA_OPTS} -jar /opt/jshERP/jshERP.jar \
  --server.port="${SERVER_PORT}" \
  --spring.datasource.url="${JDBC_URL}" \
  --spring.datasource.username="${DB_USER}" \
  --spring.datasource.password="${DB_PASS}" \
  --spring.redis.host="${REDIS_HOST}" \
  --spring.redis.port="${REDIS_PORT}" \
  --spring.redis.password="${REDIS_PASS}" \
  --file.path=/opt/jshERP/upload \
  --file.exportTmp=/opt/jshERP/export
