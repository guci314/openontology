# Official jshERP (华夏 ERP / 管伊佳 ERP) Local ARM64/AMD64 Stack

Lightweight, repository-clean Docker setup building official [`jishenghua/jshERP`](https://github.com/jishenghua/jshERP) from upstream source inside a multi-stage Docker build (zero vendor Java/SQL bloat committed to Git).

## Port Mapping (Localhost-Only, Conflict-Free)

| Container      | Image / Build        | Host Bind                | Notes |
|----------------|----------------------|--------------------------|-------|
| `jsherp-mysql` | `mysql:8.0`          | `127.0.0.1:13306->3306`  | Schema `jsh_erp`, root pass `123456` |
| `jsherp-redis` | `redis:7-alpine`     | `127.0.0.1:16379->6379`  | Password `1234abcd` |
| `jsherp-boot`  | `jsherp-boot:local`  | `127.0.0.1:19999->9999`  | Context path `/jshERP-boot` |

## Quick Start

```bash
docker compose -f docker/jsherp/docker-compose.yml up -d --build
```

Check health:

```bash
docker compose -f docker/jsherp/docker-compose.yml ps
curl -s http://127.0.0.1:19999/jshERP-boot/platformConfig/getPlatform/name
```

## Export Official `jsh_erp.sql`

```bash
docker run --rm --entrypoint cat jsherp-boot:local /opt/jshERP/jsh_erp.sql > /tmp/jsh_erp.sql
```

## Default Accounts & Headless API Login

Upstream `jsh_erp.sql` seeds default tenant login `jsh` (password `123456`) and admin `admin` (`123456`). By default, `entrypoint.sh` sets `checkcode_flag='0'` on initial schema load so `/jshERP-boot/user/login` accepts standard MD5 password hex (`e10adc3949ba59abbe56e057f20f883e` for `123456`) and issues `X-Access-Token` without an interactive image captcha.

## Reset Database Volume

```bash
docker compose -f docker/jsherp/docker-compose.yml down -v
```
