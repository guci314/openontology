# camel-sync

真实 Quarkus 3.15.1 + Apache Camel 4.8.0 集成总线，连接两个真实 Odoo 19 ERP
（`erp_north` / `erp_south`）与纯业务 OpenOntology HTTP 服务。

## 离线编译

```bash
cd /Users/guci/work/open-ontology/camel-sync
PATH="/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin:/opt/homebrew/opt/maven/bin:$PATH" \
  mvn -o package -DskipTests
```

产物：

```text
target/quarkus-app/quarkus-run.jar
```

## 启动

```bash
java -Dquarkus.http.port=18080 \
     -Dontology.url=http://127.0.0.1:18000 \
     -Dodoo.url=http://localhost:8069/jsonrpc \
     -jar target/quarkus-app/quarkus-run.jar
```

也可用环境变量覆盖：

```text
QUARKUS_HTTP_PORT=18080
ONTOLOGY_URL=http://127.0.0.1:18000
ODOO_URL=http://localhost:8069/jsonrpc
ODOO_DB_NORTH=erp_north
ODOO_DB_SOUTH=erp_south
```

## 路由

```text
POST /camel/ingest
  -> direct:ingest
  -> 从 erp_north / erp_south 抽取 res.partner + sale.order
  -> POST OpenOntology /api/snapshot

POST /camel/writeback
  -> direct:writeback
  -> 拉取 OpenOntology /api/action-runs?status=COMMITTED
  -> split(body())
  -> choice()
       body[region] == 'north' -> direct:writeback-north
       body[region] == 'south' -> direct:writeback-south
  -> 真实 Odoo action_cancel / action_draft + action_confirm
  -> 回查 sale.order.state

GET /camel/stats
  -> ingest_count / north_writebacks / south_writebacks
     / processed_run_ids / last_error
```

`timer:autoSync` 可通过 `camel.sync.auto.enabled=true` 开启，间隔由
`camel.sync.auto.period` 控制。

## 手工验证

```bash
curl -s -X POST http://127.0.0.1:18080/camel/ingest
curl -s http://127.0.0.1:18080/camel/stats
curl -s -X POST http://127.0.0.1:18080/camel/writeback
```

## 测试

```bash
cd /Users/guci/work/open-ontology
python3 -m pytest tests/test_camel_odoo_e2e.py -v
```

测试会自动离线编译本工程，并为每条用例启动独立 JVM、独立 Python HTTP
服务和真实双 Odoo 数据库。
