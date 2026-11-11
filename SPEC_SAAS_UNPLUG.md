# OpenOntology 规格说明书（TK-ONTO-009 · 「用 Ontology 去掉 SaaS」从寄生多云 SaaS 到脱壳独立运行实证 Demo）

## 1. 背景与核心命题

现代企业通常被多套云端 SaaS（如 **Shopify 管订单 + Stripe 管支付退款 + Zendesk 管客服工单**，外加 **Zapier** 做胶水）割裂并绑架高昂订阅费。
本工单构建一个可运行的对比实证 Demo（`examples/saas_unplug_demo.py` 与 `tests/test_saas_unplug.py`），直接证明：
1. **上半场（寄生统合多套 SaaS）**：`OpenOntology` 盖在 Shopify、Stripe、Zendesk 三套异构 SaaS 之上，用 `overlay` 对齐客户身份、用受控 `Action` 取代脆弱的 Zapier 面条，守住跨系统的业务铁律（如「退货退款必须同时满足：订单 $\le 30$ 天、无欺诈高危标签、且 $\ge \$100$ 必须挂起等店长审批」）；
2. **下半场（去掉 SaaS · 脱壳原生运行！）**：当企业决定砍掉昂贵的外部 SaaS（如停用 Zendesk 工单 SaaS 与外部 OMS），只需将模型主权切换为 **`__owned__ = True`（100% Ontology 原生持有）**，拔掉所有外部 SaaS 同步插头，`OpenOntology` 自己直接作为唯一业务系统独立完成「建客户 $\to$ 下单 $\to$ 提客诉工单 $\to$ AI 风控分诊 $\to$ 退货退款审批与记账 $\to$ 时光机撤回」全生命周期——**而且上层 AI Agent 调用的 Tool、业务 Action 逻辑与 Evals 测试集连一行代码都不用改！**

---

## 2. 新增文件结构

```text
/Users/guci/work/open-ontology/
├── examples/
│   └── saas_unplug_demo.py     # 「从寄生 3 个 SaaS 到彻底去掉 SaaS」完整对比演示脚本
└── tests/
    └── test_saas_unplug.py     # 针对去 SaaS 原生运行与契约零改动演进的 pytest 测试套件 (≥10 条)
```

---

## 3. 领域模型与双模式演进设计（`examples/saas_unplug_demo.py`）

### 3.1 四类核心业务对象（支持从 `parasitic` 寄生模式一键切换为 `standalone` 无 SaaS 原生模式）

提供一个工厂函数 `build_saas_ontology_spec(standalone: bool = False) -> OntologySpec`：
- 当 `standalone=False`（阶段 1–2：寄生在 Shopify/Stripe/Zendesk 上）：
  - `Customer`、`ShopOrder`、`SupportTicket` 的基础字段来自外部 SaaS 快照（`__owned__ = {"fraud_score": 0, "ai_triage_note": None, "vip_tier": "STANDARD"}` 存储在 `ontology_overlay`）；
- 当 `standalone=True`（阶段 4：**彻底去掉 SaaS，纯 Ontology 独立运行**）：
  - `Customer`、`ShopOrder`、`SupportTicket`、`RefundRecord` 全部设为 **`__owned__ = True`**，不依赖任何外部 SaaS 快照，所有对象直接由 Ontology `Action` 原生创建与流转！

具体对象字段：
1. **`Customer`**：`customer_id: str`, `email: str`, `name: str`, `lifetime_value_cents: int = 0`, `fraud_score: int = 0`（0~100）, `vip_tier: str = "STANDARD"`
2. **`ShopOrder`**：`order_id: str`, `customer_id: str`, `item_sku: str`, `amount_cents: int`, `age_days: int`, `status: str = "paid"`（`"paid"` | `"shipped"` | `"refunded"`）, `ops_memo: str | None = None`
3. **`SupportTicket`**：`ticket_id: str`, `order_id: str`, `customer_id: str`, `issue_type: str`（`"damaged"` | `"late"` | `"remorse"`）, `requested_refund_cents: int`, `status: str = "open"`（`"open"` | `"triaged"` | `"resolved"` | `"rejected"`）, `ai_triage_note: str | None = None`
4. **`RefundRecord`（`__owned__ = True`）**：`refund_id: str`, `ticket_id: str`, `order_id: str`, `customer_id: str`, `amount_cents: int`, `approved_by: str`

### 3.2 三条关系边（`LinkType`）

- `customer_orders`: `Customer -> ShopOrder`（`one-to-many`）
- `order_tickets`: `ShopOrder -> SupportTicket`（`one-to-many`）
- `ticket_refund`: `SupportTicket -> RefundRecord`（`one-to-one`, `owned=True`）

### 3.3 上层完全冻结的业务函数与受控动作（同一套函数同时跑在「有 SaaS」与「无 SaaS」模式上！）

1. **`create_customer_and_order`（原生建客下单动作）**：
   - 在无 SaaS 原生模式下，直接通过 Action 创建 `Customer` 与 `ShopOrder` 并建立 `customer_orders` 链接，累加客户 `lifetime_value_cents`。
2. **`open_support_ticket`（原生建客诉工单动作 —— 替代 Zendesk）**：
   - 校验关联 `ShopOrder` 存在，创建 `SupportTicket(status="open")` 并建立 `order_tickets` 链接。
3. **`ai_triage_ticket`（AI 智能分诊与反欺诈评分动作）**：
   - 更新客户的 `fraud_score`、`vip_tier` 以及工单的 `status="triaged"`、`ai_triage_note`。
4. **`process_return_and_refund`（跨订单+工单+资金的核心治理动作 —— 替代 Stripe+Shopify+Zendesk+Zapier 联动）**：
   - **三条硬护栏（Preconditions）**：
     - ① **超期拦截**：若 `order.age_days > 30` $\to$ 抛出 `InvariantError(..., code="RETURN_WINDOW_EXPIRED")`（记入 `REJECTED`）；
     - ② **反欺诈拦截**：若 `customer.fraud_score >= 80` $\to$ 抛出 `InvariantError(..., code="HIGH_FRAUD_RISK_BLOCKED")`（记入 `REJECTED`）；
     - ③ **防重复退款与超额退款**：若 `order.status == "refunded"` 或 `requested_refund_cents > order.amount_cents` $\to$ 抛出 `InvariantError(..., code="INVALID_REFUND_STATE")`。
   - **分级治理审批规则（`approval_rule`）**：
     - 若 `requested_refund_cents >= 100_00`（$\ge \$100$）且 `actor.role == "ai_agent"` $\to$ 自动挂起为 `PENDING_APPROVAL`，必须由 `store_manager` 审批！
   - **原子状态变更（`EditBatch`）**：
     - 将 `ShopOrder.status` 置为 `"refunded"`；
     - 将 `SupportTicket.status` 置为 `"resolved"`；
     - 扣减 `Customer.lifetime_value_cents`；
     - 创建 `RefundRecord` 并建立 `ticket_refund` 链接。
5. **`workspace_overview`（`@spec.function` 只读全景看板）**：
   - 使用 `ObjectSet + pivot + aggregate` 汇总当前客户数、订单总额、待处理工单数、已退款总额与被拦截的欺诈/超期请求数。

---

## 4. 演示脚本与测试验收要求

1. 运行 `python3 -m examples.saas_unplug_demo` 打印清晰的 **两阶段脱壳对比实证**：
   - **第一阶段（寄生在 Shopify + Stripe + Zendesk 三套 SaaS 上）**：通过 `load_snapshot` 从三套 SaaS 导入数据，演示 `overlay` 赋权、跨 SaaS 退款护栏拦截与重刷存活；
   - **第二阶段（拔掉全部 SaaS 插头：`standalone=True` 纯 Ontology 独立运行）**：零外部数据源，从零通过 Ontology Action 完成「建客户 $\to$ 下单 $\to$ 提工单 $\to$ AI 分诊 $\to$ 拦截超 30 天退货 (`RETURN_WINDOW_EXPIRED`) $\to$ 拦截高欺诈分羊毛党 (`HIGH_FRAUD_RISK_BLOCKED`) $\to$ 小额单 AI 自动秒退 $\to$ 大额单 ($\ge \$100$) 店长审批放行 $\to$ 时光机一键撤回退款 (`revert_action`)」；
   - **契约冻结证明**：用 `run_eval_suite` 在「有 SaaS」和「无 SaaS」两个 Runtime 上跑**完全相同的 AIP Evals 测试集**，输出 `contract_identical = True`！
2. 在 `tests/test_saas_unplug.py` 编写至少 10 条单元测试，运行 `pytest -v tests/test_saas_unplug.py` 100% 通过。
