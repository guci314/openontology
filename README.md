# OpenOntology · 业务本体、治理运行时与可替换工具

受 Palantir Ontology + AIP 启发的轻量业务本体运行时。核心依赖 Python 标准库 `sqlite3` + `pydantic`；测试使用 `pytest`，浏览器验收另需 Playwright。外部集成通过 Python 适配器及 Quarkus / Apache Camel 工程完成。

> **核心原则：业务纯洁性是依赖边界，不是词汇禁令。**
> 主干为 **Agent → Ontology → Tool**。业务蓝图表达对象、关系、规则与合法动作，不依赖 ERP、传输协议、宿主会话或 Aspect。运行时与外围执行层负责实现这些业务定义。
>
> **透明 Aspect 目标架构**：工具调用连线上可织入审计、溯源和执行保障；业务无需主动上报事件或携带技术字段。溯源能力保留并分层，不能为了纯洁性全部删除。

**架构入口：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。** [完整目标设计](docs/plans/2026-11-12-transparent-aspect-architecture.md)与[第一阶段计划](docs/plans/2026-11-12-aspect-execution-foundation.md)分别说明约束和实施边界。

> **实施状态**：TK-ONTO-020 正在 `feat/aspect-foundation` 隔离分支实现内部能力绑定与透明可选观察。尚未验收、未合并 main，未接入真实工具路径；完整持久化溯源、可靠执行账本及控制型 Aspect 属于后续阶段。现有词汇黑名单测试本轮不修改。

## 快速开始

```bash
cd /Users/guci/work/open-ontology
pip install -r requirements.txt

pytest -v                              # 包含内核、治理、集成与界面验收，条数以本轮实际运行输出为准
python3 -m examples.cold_chain         # 冷链示例
python3 -m examples.odoo_dual_erp_demo # 真实双 Odoo 19 ERP 并购订单全链路
python3 -m examples.steel_mdm_demo     # 钢厂主数据两大死结破局演示
python3 -m examples.odoo_o2c_adapter   # 真实 Odoo 19 标准 O2C 全流程

# 真实 Quarkus/Camel 工程（离线编译）
cd camel-sync
PATH="/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home/bin:/opt/homebrew/opt/maven/bin:$PATH" \
  mvn -o package -DskipTests
# 产物：target/quarkus-app/quarkus-run.jar
```

Camel E2E 测试会自动离线构建 `quarkus-run.jar`，然后每条测试真实拉起：

```text
Python Ontology HTTP Server  +  Java quarkus-run.jar  +  erp_north / erp_south
```

## 结构

| 层 | 文件 | 职责 |
|---|---|---|
| L2 存储 | `open_ontology/schema.sql` | 5 张纯业务核心表 + 物理写门禁触发器 |
| L3 声明 | `open_ontology/spec.py` | `Actor` / `ObjectType` / `LinkType` / 函数 / 动作注册器 |
| L4 沙箱 | `open_ontology/sandbox.py` | `EditBatch` 内存推演、`ObjectDiff` 与逆向补丁 |
| L4 运行时 | `open_ontology/runtime.py` | put_object / load_snapshot / simulate / submit / approve / revert |
| L4 查询 | `open_ontology/query.py` | `ObjectSet` 集合代数、`pivot`、`aggregate` |
| L5 OSDK | `open_ontology/osdk.py` | LLM Tool JSON Schema 导出 + AIP 确定性评测器 |
| HTTP 传输边界 | `open_ontology/http_server.py` | 标准库 `ThreadingHTTPServer` 暴露 health / snapshot / action-runs / objects；不是业务蓝图 |
| 指挥台展示 | `open_ontology/workshop.py`、`workshop_ui.py` | 本体蓝图与运行实例双视图、中文展示与拓扑大图 |
| 内部执行边界（施工中） | `open_ontology/execution/` | 实例级能力绑定与透明可选观察；尚未接入实际治理调用路径 |
| 纯业务模型 | `examples/odoo_orders_ontology.py` | 双源订单并购场景的 PIM |
| Odoo PSM | `examples/odoo_dual_erp_demo.py` | 真实 Odoo JSON-RPC 建库、同步、回写桥接 |
| 钢厂 MDM | `examples/steel_mdm_demo.py` | 散码寄生归并、源头建码护栏、高维批次桶、三视角对账 |
| O2C PIM | `examples/o2c_ontology.py` | Customer / SalesOrder / Delivery / Invoice + 2 诊断函数 + 5 治理动作 |
| O2C PSM | `examples/odoo_o2c_adapter.py` | 真实 Odoo 19 确认/出库/开票过账/收款核销适配器 |
| Camel 总线 | `camel-sync/` | Quarkus 3.15.1 + Camel 4.8.0：REST + Split + Choice CBR + Timer + DeadLetter |

## 五张纯业务表

`ontology_registry`、`ontology_objects`、`ontology_links`、`ontology_action_runs`、`ontology_overlay`，外加物理门禁表 `ontology_write_gate`。

## 核心机制

1. **物理写门禁**：对象、关系、overlay 的 INSERT/UPDATE/DELETE 由 SQLite `BEFORE` 触发器直接 `RAISE(ABORT, 'GOVERNANCE_VIOLATION: ...')`。只有 `OntologyRuntime` 在同一个事务里临时开门，异常时连同开门一起回滚。
2. **Append-only 业务审计**：`ontology_action_runs` 上禁止 DELETE，历史动作不可篡改。
3. **状态置入**：`put_object(obj)` 按 `properties_json` 的 SHA256 内容指纹判断，内容未变时静默跳过且不递增 `version`。`ingest_object(obj)` 是兼容别名。
4. **字段级主权（`__owned__`）**：
   - `__owned__ = True`：整个类型由 Ontology 原生拥有，外部快照不得提供该类型。
   - `__owned__ = {"assignee": None, "ops_note": None}`：类型整体 source-backed，但字典中的字段归 Ontology 自有。
   - 动作修改自有字段时，同时更新对象行与 `ontology_overlay`；改回默认值会自动清理 overlay。
5. **快照重索引 `load_snapshot`**：单事务内替换 source-backed 底表，再按 overlay 重新覆盖自有字段；若快照会让某个 overlay 失去基础对象，抛 `ReindexConflictError` 并完整回滚（防孤儿保护）。
6. **主权边界强校验**：每个 Action 声明 `affects_source`。编辑计划同时触碰 source 与 ontology 两侧 → `MIXED_AUTHORITY`；触碰 source 却声明 `False` → `UNDECLARED_SOURCE_WRITE`；只碰 ontology 却声明 `True` → `MISDECLARED_SOURCE_WRITE`。
7. **RLS 行级安全**：`Actor.can_access(tags)` 强制 `region:north` 等安全标签；查询按可见性过滤，直接读取越权对象抛 `PermissionDeniedError`。
8. **Action 永不直接写库**：Action 只操作 `EditBatch`，读得到自己刚暂存的修改（read-your-own-writes），结束后编译成可序列化、可逆转的 `ObjectDiff`。
9. **模拟零写入**：`simulate_action` 返回 `valid / diff / requires_approval / read_versions / error`，数据库版本与行数完全不变。
10. **原子提交 + 乐观锁**：`submit_action` 幂等（同一 `run_id` 只执行一次），校验 `read_versions` 后在单事务内改对象、改关系、写业务审计记录。
11. **被拒企图审计**：业务不变量或主权边界失败时，对象/关系零改动，但必须持久化一条 `status='REJECTED'` 的审计记录（含 `error_code` / `error_message`），随后抛出 `InvariantError`。
12. **治理审批 + 审批后置钩子**：`approval_rule` 命中时只落 `PENDING_APPROVAL` + diff，对象不动；`approve_action` 校验审批角色，并再次比对 `read_versions`，等待期间任何被读对象版本变化都会抛 `ConcurrencyConflictError`。可选的 `post_approval` 钩子用于审批通过后原子创建关联的 Ontology 自有对象（如新物理规格），其 diff 与主动作 diff 合并存储，可被时光机整体撤回。
13. **时光机回滚**：`revert_action` 取原 diff 的 `inverted()` 逆向恢复对象与关系，原运行标记 `REVERTED`，新增一条回滚运行记录。
14. **ObjectSet 集合代数**：`search_set` / `union` / `intersect` / `subtract` / `pivot`（正向或反向群跳转，两端 RLS 过滤）/ `aggregate`（分组计数与求和）/ `filter_aggregation`。
15. **OSDK / AIP**：`export_llm_tools` 导出 OpenAI Tool JSON Schema；`AIPAgentExecutor` 统一分派 `query_objects / call_function / simulate_action / submit_action / pivot / aggregate`；`run_eval_suite` 对模型决策产生的 `ObjectDiff` 做确定性评分。

## HTTP 传输边界

`open_ontology/http_server.py` 使用标准库 `ThreadingHTTPServer`：

```text
GET  /api/health                    -> {"ok": true}
POST /api/snapshot                  -> runtime.load_snapshot(objects, links)
GET  /api/action-runs?status=...    -> {"runs": [...]}
GET  /api/objects/{object_type}     -> [objects...]
```

该文件做业务 JSON 与 HTTP 的传输绑定，不负责供应商 ERP 调用。业务定义不依赖它；HTTP 本身属于平台适配，不能因承载业务内容就称为“零技术概念”。

## 钢厂主数据两大死结破局

`examples/steel_mdm_demo.py` 用纯业务本体解开钢铁行业 MDM 两个死结：

**死结一：采购随合同建散码**

- 老 ERP 的 4 个 `ContractSKU` 不合并、不改码。
- `map_legacy_sku_to_spec` 只写 `canonical_spec_id` / `mapped_by` 到 `ontology_overlay` 并建立 `sku_maps_to_spec` 边。
- `load_snapshot` 重刷 `ContractSKU` / `Manufacturer` 底表后，归并关系 100% 存活；`MaterialSpec` / `MaterialLot` 完全自有，不受影响。
- 增量建码 `register_contract_sku` 硬护栏：不绑现有规格、也不申请新规格 → `UNMAPPED_PHYSICAL_SPEC` + REJECTED 审计；灰分/硫分超阈值挂高档规格 → `SPEC_QUALITY_MISMATCH`；新物理品种 → `PENDING_APPROVAL`，`mdm_steward` 批准后才创建新 `MaterialSpec` 和新散码。

**死结二：采购按规格、生产按厂家**

- `receive_material_lot` 建立 `MaterialLot` 高维批次桶，同时挂 `lot_has_spec` / `lot_produced_by` / `lot_billed_as` 三条正交边。
- MES 只按厂家报工：`consume_by_brand_fifo(brand_id, tons)` 自动按 `lot_id` FIFO 扣减该厂家的多个规格批次。
- `reconcile_multi_view_ledger` 同时输出三本账：
  - 成本科按厂家品牌（`BRAND-1` 150 吨 / 280,000 元）
  - 采购 & ERP 报产按物理规格（`SPEC-A` 100 吨 / 200,000 元 + `SPEC-B` 50 吨 / 80,000 元）
  - 财务税务按原始发票散码（`ERP-SKU-9001` 100 吨 + `ERP-SKU-9003` 50 吨）
  - 三本账总吨数与总成本 100% 恒等，`balanced=True`。
- `ObjectSet + pivot + aggregate` 可从品牌批次直接跳转到规格/散码并汇总消耗吨数。
- `revert_action` 可整体撤回投料，库存恢复如初。

## 标准 O2C 订单到收款全流程

`examples/o2c_ontology.py`（PIM）只描述业务：

- `Customer`（混合主权）：信用额度、应收余额 + `risk_tier` / `credit_hold_note` 自有治理字段。
- `SalesOrder`（混合主权）：草稿 → 确认 → 已发货 → 已开票 → 已收款；`account_rep` / `fulfillment_note` 归 Ontology 自有。
- `Delivery`（混合主权）：待发货 / 已发货；`carrier_note` 自有。
- `Invoice`（source-backed）：已过账 / 已收款。
- 关系：`customer_sales_orders`、`order_deliveries`、`order_invoices`。
- 只读函数：`diagnose_o2c_bottlenecks`（信用超限草稿 / 未发货确认单 / 漏开票 / 未收款发票）与 `evaluate_credit_exposure`。
- 治理动作：`confirm_sales_order`、`ship_order_delivery`、`issue_and_post_invoice`、`record_customer_payment`、`annotate_o2c_governance`。
- 硬规则：未发货严禁开票（`CANNOT_INVOICE_UNSHIPPED_ORDER` + REJECTED 审计）；超信用额度且非 `finance_director` 自动 `PENDING_APPROVAL`；付款金额必须与发票精确一致。

`examples/odoo_o2c_adapter.py`（PSM）把业务动作翻译成真实 Odoo 19：

```text
confirm_sales_order    -> sale.order.action_confirm
ship_order_delivery    -> stock.picking.action_assign
                           stock.move.quantity + picked
                           stock.picking.button_validate
issue_and_post_invoice -> account.move(move_type=out_invoice, sale_line_ids)
                           account.move.action_post
record_customer_payment-> account.payment.register create
                           account.payment.register.action_create_payments
```

每一步写回后立即从真实 `erp_north` 回查：

```text
sale.order.state == 'sale'
stock.picking.state == 'done'
account.move.state == 'posted'
account.move.amount_residual == 0 / payment_state == 'paid'
```

`sync_from_odoo` 会重刷真实 Odoo 的客户、订单、交货单、发票状态，`Customer.risk_tier`、`SalesOrder.account_rep` 等 `overlay` 自有治理属性 100% 存活。

## 真 Apache Camel 集成总线

`camel-sync/` 是真实的 Quarkus 3.15.1 + Camel 4.8.0 工程：

```text
POST /camel/ingest    -> direct:ingest
    OdooJsonBean 从 erp_north + erp_south 抽取客户/订单
    -> 归一化为业务快照 -> POST /api/snapshot

POST /camel/writeback -> direct:writeback
    -> bean 拉取 /api/action-runs?status=COMMITTED
    -> split(body())
    -> choice()
         when body[region] == 'north' -> direct:writeback-north
         when body[region] == 'south' -> direct:writeback-south
    -> 真实 Odoo action_cancel / action_draft + action_confirm
    -> 回查 sale.order.state 验证

GET /camel/stats      -> ingest_count / north_writebacks / south_writebacks
                         / processed_run_ids / last_error
```

- 上行快照与下行回写均通过 Java `HttpClient`，显式 `HTTP_1_1`。
- CDI Bean 使用 `@ApplicationScoped @Named("odooSync")`，路由通过 `.to("bean:odooSync?method=...")` 调用。
- `onException` + `direct:writeback-unknown` 提供错误响应与死信通道。
- `timer:autoSync` 路由可通过 `camel.sync.auto.enabled=true` 开启。
- 幂等：同一 `run_id` 只回写一次，`processed_run_ids` 可在 `/camel/stats` 查看。

## 测试覆盖

下列分项为早期核心与集成用例说明，不代表当前全部测试数量。透明 Aspect 施工前，在隔离工作树独立实跑的基线为 **234 passed、零 skip**；新增功能的测试结果必须另行验证。

- `tests/test_open_ontology.py`：43 条纯内核测试（写门禁、append-only、内容指纹幂等、RBAC、RLS、沙箱 RYOW、diff 逆转、零写入模拟、原子提交、幂等提交、审批挂起、并发冲突、时光机回滚、业务不变量、Tool Schema、AIP 分派、评测器、注册表持久化、字段级主权/overlay/快照防孤儿/主权边界/被拒审计/ObjectSet 代数，以及零 IT 概念污染断言）。
- `tests/test_odoo_dual_erp.py`：10 条真实双 Odoo 集成测试（跨 ERP 查询与集合代数、RLS、已发货取消被拒、待发货取消真实回写、撤回真实恢复、AI 大额审批真实回写、全量重索引 overlay/OrderNote 存活、空原因拒绝、重复取消拒绝、回写幂等、PIM/PSM 分离）。
- `tests/test_camel_odoo_e2e.py`：6 条真实 Camel E2E 测试。每条测试都会离线构建并启动 `quarkus-run.jar`、启动 `OpenOntology` HTTP Server，并连接真实 `erp_north` / `erp_south`：
  1. 上行聚合抽取
  2. 北区取消 + 时光机撤回真实回写
  3. 南区大额 AI 审批后 CBR 回写
  4. 全量重刷保留 overlay 与 `OrderNote`
  5. 同一 `run_id` 幂等回写保护
  6. 已发货订单拒绝后无回写
- `tests/test_steel_mdm.py`：14 条钢厂 MDM 测试（存量寄生归并重刷存活、无归属野码拦截、以次充好拦截、新物理品种审批、新规格撤回、已有规格秒级建码、未治理散码禁止入库、高维批次三边挂载、按厂家 FIFO 跨规格消耗、库存不足拒绝、三视角金额恒等、投料撤回、规格推荐、ObjectSet/pivot/aggregate 多对多还原）。
- `tests/test_odoo_o2c.py`：14 条真实 Odoo 19 O2C 测试（PIM/PSM 分离、真实草稿报价单、订单确认、出库校验、未发货开票拒绝、全链路 paid + residual=0、信用敞口、AI 大额审批、overlay 重刷存活、付款金额不一致、重复收款、黑名单客户、分阶段经营体检、重复确认）。

## 运行环境

- Python 3.13、`pydantic`、`pytest`
- Java 17：`/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home`
- Maven：`/opt/homebrew/opt/maven`
- 真实 Odoo 19：`http://localhost:8069`，数据库 `erp_north` / `erp_south`
- PostgreSQL 容器：`odoo-demo-db`（宿主机 5433）

Odoo 主密码优先读取 `ODOO_MASTER_PASSWORD`，其次 `odoo-demo-master-2026`，最后兼容实际容器默认的 `admin`。
