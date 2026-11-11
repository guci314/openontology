# OpenOntology 规格说明书（TK-ONTO-010 · 下载真实开源 CRM (BottleCRM/django-crm + Odoo CRM) 并实证「吸干 SaaS 后拔掉插头独立运行」）

## 1. 背景与目标

拒绝使用假 JSON Mock 外部 SaaS！本工单要求在本机真实下载并配置开源 CRM **BottleCRM（PyPI 包 `django-crm==1.44.0`，2.7MB wheel，已在 PyPI 验证秒下）** 并在本机真实 Odoo 19 中启用开源 **`crm`（商机/线索）模块**，构成**「真实开源 CRM + 真实开源 ERP」**双 SaaS 环境。

随后用代码完整实证 **「寄居蟹式吞并并去掉 SaaS」** 的全过程：
1. **阶段 A（连接并吸干真实开源 SaaS）**：从真实运行的开源 CRM 与 ERP 中抽取客户（Account/Partner）、销售商机（Opportunity/Lead）、客服工单（Case/Ticket）与销售订单（Order）进入 `OpenOntology`，叠加 `overlay` 治理属性并执行跨系统受控 Action；
2. **阶段 B（拔掉 SaaS 插头 · 去掉 SaaS 独立运行！）**：将对象主权一键切换为 `__owned__ = True`（`unplug_saas()`），**物理切断与外部 CRM / ERP 的一切连接**，验证所有已吸纳的客户、商机、订单、工单与 `overlay` 状态在 `OpenOntology` 内部完好保留，且上层 AI Agent、业务 Action（新建商机、赢单转订单、工单退货退款审批、时光机一键撤回）与 AIP Evals **一行代码不改即可在零外部 SaaS 下 100% 独立运行**！

---

## 2. 本机真实开源 CRM 下载与环境要求

1. **下载开源 CRM `django-crm` (BottleCRM 1.44.0)**：
   - PyPI Wheel 直链（仅 2.7MB）：可通过 `pip download django-crm==1.44.0 -d /Users/guci/work/open-ontology/vendor_crm --no-deps` 下载并解压到 `/Users/guci/work/open-ontology/vendor_crm/bottlecrm_src`（含完整的 `accounts`, `leads`, `opportunity`, `cases`, `contacts`, `invoices` 开源 CRM 模型与 SQLite 建表结构），并在本机真实初始化 BottleCRM 的 SQLite 数据库 `/Users/guci/work/open-ontology/vendor_crm/bottlecrm.sqlite3`！
2. **同时在本机真实 Odoo 19 (`erp_north`) 中安装/启用开源 `crm` 模块（或结合 BottleCRM 真实库 + Odoo 真实库）**：
   - BottleCRM（提供真实的开源 CRM 表 `account`, `lead`, `opportunity`, `case`）+ Odoo 19（提供真实的开源 ERP 订单表 `sale_order`）组成**两个真实的异构开源 SaaS 系统**！

---

## 3. 新增文件结构

```text
/Users/guci/work/open-ontology/
├── vendor_crm/                     # 真实下载的开源 BottleCRM (django-crm 1.44.0) 源码与真实 SQLite 库
├── examples/
│   └── real_crm_unplug_demo.py     # 「真实 BottleCRM + 真实 Odoo 19 → 吸干数据 → 拔掉插头去 SaaS 独立运行」演示脚本
└── tests/
    └── test_real_crm_unplug.py     # 针对真实开源 CRM 下载、吸纳、拔插头独立运行的 pytest 验收测试 (≥10 条)
```

---

## 4. 核心业务流程与验收标准（`examples/real_crm_unplug_demo.py` & `tests/test_real_crm_unplug.py`）

1. **真实开源 CRM 验证**：
   - 验证 `/Users/guci/work/open-ontology/vendor_crm/` 下真实下载并解压了开源 `django-crm` (BottleCRM) 源码包，且其真实数据库表（`crm_account`, `crm_opportunity`, `crm_case`）与本机真实 Odoo 19（`erp_north` 的 `sale.order`）中写入了真实业务记录。
2. **阶段 A（寄生双开源 SaaS 态）**：
   - 从真实 BottleCRM 数据库读取客户、商机、客诉工单（`crm_case`），从真实 Odoo 19 JSON-RPC 读取销售订单（`sale.order`），通过 `load_snapshot` 装入 `OpenOntology`；
   - 执行 `ai_triage_crm_case`（写入 Ontology 自有属性 `fraud_score`, `vip_tier`, `ai_resolution_plan` 到 `ontology_overlay`）；
   - 验证外部 BottleCRM 与 Odoo 重新全量 `load_snapshot` 时，`overlay` 属性 100% 存活。
3. **阶段 B（一键拔掉 SaaS 插头 `unplug_all_saas()` —— 去掉 SaaS 独立运行态）**：
   - 调用 `unplug_all_saas()`：将 `OpenOntology` 中的 `CustomerAccount`, `SalesOpportunity`, `SupportCase`, `SalesOrder`, `RefundSettlement` 全部晋升为 `__owned__ = True`，并将外部 BottleCRM 和 Odoo 连接器彻底置为 `Disconnected / Disabled`（若有任何代码再尝试调用外部 SaaS 则立即抛异常，以此证明 100% 脱离外部 SaaS！）；
   - 验证拔掉插头后，此前从 BottleCRM 和 Odoo 吸过来的全部数据 + `overlay` 补丁 100% 原样保留在 `OpenOntology` 中；
   - 在完全脱离外部 SaaS 的状态下，直接用同一套上层 Action 跑通：
     - ① `win_opportunity_and_create_order`：将 CRM 商机赢单并原生生成销售订单；
     - ② `open_customer_case`：原生创建客诉工单；
     - ③ `resolve_case_with_refund`：验证超 30 天退货期被 `RETURN_WINDOW_EXPIRED` 拦截并记入 `REJECTED`、高欺诈分被 `HIGH_FRAUD_RISK_BLOCKED` 拦截、小额单 AI 自动退款、大额单（$\ge \$100$）挂起 `PENDING_APPROVAL` 由经理 `approve_action` 放行；
     - ④ `revert_action`：时光机一键撤回退款，客户 LTV、订单状态、工单状态全部恢复如初！
4. 运行 `python3 -m examples.real_crm_unplug_demo` 与 `pytest -v tests/test_real_crm_unplug.py` 100% 通过。
