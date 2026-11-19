# 销售 AI Agent 业务需求说明书：基于 Odoo 19 的 O2C（订单到收款）本体

> **发起方**：销售运营部 & 财务风控部  
> **面向对象**：销售 AI Agent（`sales_agent`）、仓库履约协同与财务总监审批（`finance_director`）  
> **外部现状系统**：公司现行 Odoo 19 ERP（`erp_north`）

---

## 1. 业务背景与痛点

目前公司销售报价、出库发货和应收开票全部跑在 Odoo 19 里，但我们**绝对不敢把 Odoo 的原始表（`res.partner`、`sale.order`、`stock.picking`、`account.move`）或原生 RPC 直接开放给销售 AI Agent**：

1. **概念混杂**：`res.partner` 里既有客户又有内部联系人/非客户；`stock.picking` 里混着与销售无关的内部调拨单；`account.move` 一张表里塞了凭证、供应商账单、红字退款单（`out_refund`）和客户应收发票（`out_invoice`）。
2. **缺乏业务护栏**：如果销售 Agent 产生幻觉，给没发货的订单提前开出发票、私自放行超过 10 万元的高风险大单，或者把收款金额写错/重复核销，财务底账会立刻失真。
3. **缺少自有治理沉淀**：销售 Agent 每天需要给客户打风控标签（`risk_tier`）和写跟进备注（`sales_followup_note`），这些信息不该污染老 ERP 字段，且 Odoo 定时同步刷新时绝不能把它们冲掉。

我们需要 **Ontology Builder（本体建造师）** 只读勘察现行 Odoo 19，结合本需求文档，为销售 Agent 建造一套干净、强治理、零 Odoo 技术脏词的 **O2C 业务本体（`o2c_ontology`）** 以及对应的 **Odoo → O2C 映射契约**。

---

## 2. 期望的核心业务对象（纯业务语义）

| 业务对象 | 归属原型 | 核心语义与清洗要求 |
|---|---|---|
| **客户（`Customer`）** | `MasterEntity`（聚合根） | 仅纳管真实客户；金额统一折算为整数分（`credit_limit_cents` ≥ 0，并对非财务/管理员角色脱敏）；挂载本体自有字段 `risk_tier`（`NORMAL / WATCHLIST / BLOCKED`）与 `sales_followup_note`。 |
| **销售订单（`SalesOrder`）** | `ProcessDocument` | 强引用所属 `Customer`；状态机严格遵循 `draft → confirmed → cancelled`（Odoo 中 `draft`/`sent` 归一为 `draft`，`sale` 归一为 `confirmed`）；金额 `amount_cents` ≥ 0。 |
| **出库交货单（`Delivery`）** | `ProcessDocument` | 仅纳管挂靠在销售订单下的出库单（过滤无销售归属的内部调拨）；强引用 `SalesOrder`；状态机严格遵循 `ready → done / cancelled`。 |
| **应收发票（`Invoice`）** | `ProcessDocument` | 仅纳管客户正向应收发票（过滤 `entry`/`in_invoice`/`out_refund` 等非客户正向发票）；强引用 `SalesOrder` 与 `Customer`；状态机遵循 `posted → paid`；记录总额 `amount_cents` 与待收余额 `residual_cents`（均 ≥ 0）。 |

---

## 3. 销售 Agent 受控动作与合规铁律

1. **确认销售订单（`confirm_sales_order`）**
   * 允许角色：`sales_agent`、`finance_director`、`admin`。
   * 强校验：入参订单金额必须与系统账面一致（`ORDER_AMOUNT_MISMATCH`）；草稿态订单才可确认。
   * **大额审批铁律**：凡订单金额 **超过 ¥100,000（即 `> 10_000_000` 分）** 且调用方不是 `finance_director` 时，**严禁直接落库，必须自动挂起为 `PENDING_APPROVAL` 等待财务总监审批**。
2. **完成交货出库（`ship_order_delivery`）**
   * 允许角色：`warehouse_ops`、`sales_agent`、`admin`。
   * **前置铁律**：关联销售订单必须已处于 `confirmed` 状态，未确认草稿单严禁出库（`ORDER_NOT_CONFIRMED`）。
3. **开具应收发票（`issue_sales_invoice`）**
   * 允许角色：`sales_agent`、`finance_director`、`admin`。
   * **核心财务红线**：对应销售订单**必须存在至少一张状态为 `done` 的出库交货单**；未发货订单严禁提前开票（全局不变量 `CANNOT_INVOICE_UNSHIPPED_ORDER`）。
4. **登记收款核销（`record_invoice_payment`）**
   * 允许角色：`finance_director`、`sales_agent`、`admin`。
   * 强校验：已结清（`paid`）发票严禁重复收款（`INVOICE_ALREADY_PAID`）；实收金额必须精确等于当前待收余额 `residual_cents`（`PAYMENT_AMOUNT_MISMATCH`）；核销后待收余额归零且状态转为 `paid`。
5. **标注客户风控与销售跟进（`annotate_customer_risk`）**
   * 允许角色：`sales_agent`、`finance_director`、`admin`。
   * 主权边界：`affects_source=False`，只修改 `Customer` 的本体自有字段 `risk_tier` 与 `sales_followup_note`（落盘 `ontology_overlay`，外部 Odoo 全量同步刷新时 100% 存活）。

---

## 4. 验收标准

* **零侵扰勘察**：构建过程只能只读探测 Odoo 19，不得修改 Odoo 中任何一条业务数据。
* **真实数据全拟合**：从现行 Odoo 19 抽取的真实客户、销售订单、交货单与客户发票，经映射投入 O2C 本体后必须 100% 通过类型校验、参照完整性与所有业务不变量（且自动过滤红字退款单、内部调拨单等脏数据）。
* **反例全拦截 & 可用工具导出**：在真实 Odoo 采样订单上验证“未发货开票被红牌拦截”、“12 万大单被挂起审批”、“正常小单可顺畅推进”、“风控备注落入自有层”，并导出标准 OSDK Tool Schema 供销售 Agent 直接调用。
