"""TK-ONTO-009 · 「用 Ontology 去掉 SaaS」从寄生多云 SaaS 到脱壳独立运行。

同一个纯业务本体，两种主权模式：

* ``standalone=False``（Phase A · 寄生态）
  Customer / ShopOrder / SupportTicket 的基础字段来自 Shopify、Stripe、
  Zendesk 三套异构 SaaS 快照；只有 ``fraud_score`` / ``vip_tier`` /
  ``ops_memo`` / ``ai_triage_note`` 这几个字段由 Ontology 自有，
  存放在 ``ontology_overlay`` 补丁层里，源系统全量重刷也不会丢。

* ``standalone=True``（Phase B · 脱壳态）
  四个对象类型全部 ``__owned__ = True``，拔掉所有外部 SaaS 插头，
  零外部数据源，由 Ontology Action 原生跑完
  建客 → 下单 → 提工单 → AI 分诊 → 退货退款审批 → 时光机撤回 全生命周期。

**契约冻结**：上面两种模式下，Action 名、参数 schema、错误码、审批语义、
只读看板字段完全一致；上层 AI Agent 调用的 Tool 与 AIP Evals 测试集
一行代码都不用改 —— ``run_eval_suite()`` 在两种 Runtime 上跑同一套用例，
输出 ``contract_identical = True``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from open_ontology import (
    Actor,
    InvariantError,
    LinkType,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
)

__all__ = [
    "AI_AGENT",
    "CS_AGENT",
    "STORE_MANAGER",
    "SaasModel",
    "build_saas_model",
    "build_saas_ontology_spec",
    "build_saas_snapshot",
    "run_eval_suite",
    "agent_tools",
    "AIP_EVALS",
    "main",
]

# 退货窗口（天）与需要店长审批的金额门槛（分）
RETURN_WINDOW_DAYS = 30
MANAGER_APPROVAL_THRESHOLD_CENTS = 100_00
FRAUD_BLOCK_THRESHOLD = 80

# ---------------------------------------------------------------------------
# 角色（两种模式共用，纯身份，不绑定任何 SaaS 账号体系）
# ---------------------------------------------------------------------------
AI_AGENT = Actor(actor_id="ai-agent-01", role="ai_agent", clearance_tags={"*"})
CS_AGENT = Actor(actor_id="cs-agent-01", role="cs_agent", clearance_tags={"*"})
STORE_MANAGER = Actor(
    actor_id="store-manager-01", role="store_manager", clearance_tags={"*"}
)

# 寄生态下每个对象类型里「归 Ontology 自有」的字段。
#
# 刻意只收「风控评分 / VIP 分层 / 内部备注 / AI 分诊结论」这类
# *源系统没有的派生治理字段*：它们写进 ontology_overlay 补丁层，
# 无论 Shopify / Stripe / Zendesk 怎么全量重刷都杀不掉。
# 订单状态、工单状态、LTV 等仍属源系统字段（由 SaaS 快照供给）。
PARASITIC_OWNED_FIELDS: dict[str, dict[str, Any]] = {
    "Customer": {"fraud_score": 0, "vip_tier": "STANDARD"},
    "ShopOrder": {"ops_memo": None},
    "SupportTicket": {"ai_triage_note": None},
}


@dataclass
class SaasModel:
    """一次 spec 构建产出的全部可调用句柄（类、边、spec）。"""

    spec: OntologySpec
    standalone: bool
    Customer: type[ObjectType]
    ShopOrder: type[ObjectType]
    SupportTicket: type[ObjectType]
    RefundRecord: type[ObjectType]
    customer_orders: LinkType
    order_tickets: LinkType
    ticket_refund: LinkType

    # -- 便捷读取 ----------------------------------------------------------
    def new_runtime(self, db_path: str = ":memory:") -> OntologyRuntime:
        return OntologyRuntime(self.spec, db_path)

    def object_type(self, name: str) -> type[ObjectType]:
        return self.spec.object_types[name]


# ---------------------------------------------------------------------------
# Spec 工厂：业务逻辑写一次，主权声明随模式切换
# ---------------------------------------------------------------------------
def build_saas_model(standalone: bool = False) -> SaasModel:
    """构建整套 SaaS 业务本体。

    ``standalone=False`` -> 寄生态（字段级 overlay 主权）
    ``standalone=True``  -> 脱壳态（全对象 100% Ontology 自有）
    """
    spec = OntologySpec()

    customer_owned: Any = True if standalone else dict(
        PARASITIC_OWNED_FIELDS["Customer"]
    )
    order_owned: Any = True if standalone else dict(
        PARASITIC_OWNED_FIELDS["ShopOrder"]
    )
    ticket_owned: Any = True if standalone else dict(
        PARASITIC_OWNED_FIELDS["SupportTicket"]
    )

    # -- 1. 四类业务对象 ---------------------------------------------------
    class Customer(ObjectType):
        """客户（寄生态来自 Shopify/CRM 快照，脱壳态由 Ontology 原生持有）。"""

        __primary_key__ = "customer_id"
        __title__ = "name"
        __owned__ = customer_owned

        customer_id: str
        email: str
        name: str
        lifetime_value_cents: int = 0
        fraud_score: int = 0
        vip_tier: str = "STANDARD"

    class ShopOrder(ObjectType):
        """订单（寄生态来自 Shopify，脱壳态由 Ontology 原生持有）。"""

        __primary_key__ = "order_id"
        __title__ = "order_id"
        __owned__ = order_owned

        order_id: str
        customer_id: str
        item_sku: str
        amount_cents: int
        age_days: int
        status: str = "paid"
        ops_memo: str | None = None

    class SupportTicket(ObjectType):
        """客诉工单（寄生态来自 Zendesk，脱壳态由 Ontology 原生持有）。"""

        __primary_key__ = "ticket_id"
        __title__ = "ticket_id"
        __owned__ = ticket_owned

        ticket_id: str
        order_id: str
        customer_id: str
        issue_type: str
        requested_refund_cents: int
        status: str = "open"
        ai_triage_note: str | None = None

    class RefundRecord(ObjectType):
        """退款凭证 —— 无论哪种模式都 100% 由 Ontology 自有。"""

        __primary_key__ = "refund_id"
        __title__ = "refund_id"
        __owned__ = True

        refund_id: str
        ticket_id: str
        order_id: str
        customer_id: str
        amount_cents: int
        approved_by: str

    for cls in (Customer, ShopOrder, SupportTicket, RefundRecord):
        spec.object_type(cls)

    # -- 2. 三条关系边 -----------------------------------------------------
    # 脱壳态下所有边也归 Ontology 自有，否则「建对象 + 建边」会撞 MIXED_AUTHORITY
    customer_orders = LinkType(
        name="customer_orders",
        source="Customer",
        target="ShopOrder",
        cardinality="one-to-many",
        owned=standalone,
        description="客户 -> 订单",
    )
    order_tickets = LinkType(
        name="order_tickets",
        source="ShopOrder",
        target="SupportTicket",
        cardinality="one-to-many",
        owned=standalone,
        description="订单 -> 客诉工单",
    )
    ticket_refund = LinkType(
        name="ticket_refund",
        source="SupportTicket",
        target="RefundRecord",
        cardinality="one-to-one",
        owned=True,
        description="工单 -> 退款凭证",
    )
    for link in (customer_orders, order_tickets, ticket_refund):
        spec.add_link_type(link)

    # -- 3. 参数模型 -------------------------------------------------------
    class CreateCustomerOrderParams(BaseModel):
        customer_id: str
        email: str
        name: str
        order_id: str
        item_sku: str
        amount_cents: int
        age_days: int = 0

    class OpenTicketParams(BaseModel):
        ticket_id: str
        order_id: str
        issue_type: str
        requested_refund_cents: int

    class TriageParams(BaseModel):
        ticket_id: str
        fraud_score: int = 0
        vip_tier: str = "STANDARD"
        note: str | None = None

    class RefundParams(BaseModel):
        ticket_id: str
        refund_id: str
        amount_cents: int
        approved_by: str | None = None

    class OverviewParams(BaseModel):
        pass

    # -- 4. 受控动作（业务逻辑两种模式完全一致） ---------------------------
    @spec.action(
        "create_customer_and_order",
        CreateCustomerOrderParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager", "sales"),
        affects_source=not standalone,
        description="原生建客下单：创建 Customer + ShopOrder 并建立 customer_orders 边。",
    )
    def create_customer_and_order(ctx: Any, params: CreateCustomerOrderParams) -> None:
        if params.amount_cents <= 0:
            raise InvariantError(
                "order amount must be positive", code="INVALID_ORDER_AMOUNT"
            )
        if params.age_days < 0:
            raise InvariantError(
                "order age_days must not be negative", code="INVALID_ORDER_AGE"
            )
        customer = Customer(
            customer_id=params.customer_id,
            email=params.email,
            name=params.name,
            lifetime_value_cents=params.amount_cents,
        )
        order = ShopOrder(
            order_id=params.order_id,
            customer_id=params.customer_id,
            item_sku=params.item_sku,
            amount_cents=params.amount_cents,
            age_days=params.age_days,
            status="paid",
        )
        ctx.create(customer)
        ctx.create(order)
        ctx.link(customer, customer_orders, order)

    @spec.action(
        "open_support_ticket",
        OpenTicketParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        affects_source=not standalone,
        description="原生建客诉工单（替代 Zendesk）：校验订单存在并建立 order_tickets 边。",
    )
    def open_support_ticket(ctx: Any, params: OpenTicketParams) -> None:
        order = ctx.require(ShopOrder, params.order_id)
        if params.issue_type not in ("damaged", "late", "remorse"):
            raise InvariantError(
                f"unsupported issue_type {params.issue_type!r}", code="INVALID_ISSUE_TYPE"
            )
        if params.requested_refund_cents <= 0:
            raise InvariantError(
                "requested_refund_cents must be positive",
                code="INVALID_REFUND_REQUEST",
            )
        ticket = SupportTicket(
            ticket_id=params.ticket_id,
            order_id=order.order_id,
            customer_id=order.customer_id,
            issue_type=params.issue_type,
            requested_refund_cents=params.requested_refund_cents,
            status="open",
        )
        ctx.create(ticket)
        ctx.link(order, order_tickets, ticket)

    def _mark_triaged(ctx: Any, params: TriageParams) -> None:
        """工单流转状态由治理流程推进（寄生态写回 Zendesk，脱壳态写本体）。"""
        ticket = ctx.require(SupportTicket, params.ticket_id)
        ctx.modify(ticket, status="triaged")

    @spec.action(
        "ai_triage_ticket",
        TriageParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        affects_source=False,
        post_approval=_mark_triaged,
        description="AI 智能分诊与反欺诈评分：写客户风控字段与工单分诊备注。",
    )
    def ai_triage_ticket(ctx: Any, params: TriageParams) -> None:
        """AI 分诊 = 两个同主权平面的一次业务动作。

        主编辑计划只写 Ontology 自有字段（``fraud_score`` / ``vip_tier`` /
        ``ai_triage_note``，落 ``ontology_overlay``），因此本动作声明
        ``affects_source=False``；工单状态流转（``open -> triaged``）属于
        另一主权平面，交给 ``post_approval`` 钩子单独成一个 diff ——
        寄生态写回 Zendesk，脱壳态写本体，业务语义完全一致。
        """
        if not 0 <= params.fraud_score <= 100:
            raise InvariantError(
                f"fraud_score {params.fraud_score} out of range 0..100",
                code="INVALID_FRAUD_SCORE",
            )
        ticket = ctx.require(SupportTicket, params.ticket_id)
        customer = ctx.require(Customer, ticket.customer_id)
        ctx.modify(
            customer,
            fraud_score=params.fraud_score,
            vip_tier=params.vip_tier,
        )
        ctx.modify(
            ticket,
            ai_triage_note=params.note
            or (
                f"AI triage: fraud_score={params.fraud_score} "
                f"vip_tier={params.vip_tier} issue={ticket.issue_type}"
            ),
        )

    def _record_refund(ctx: Any, params: RefundParams) -> None:
        """资金凭证落账（替代 Stripe Refund 对象）。"""
        ticket = ctx.require(SupportTicket, params.ticket_id)
        refund = RefundRecord(
            refund_id=params.refund_id,
            ticket_id=ticket.ticket_id,
            order_id=ticket.order_id,
            customer_id=ticket.customer_id,
            amount_cents=params.amount_cents,
            approved_by=params.approved_by or ctx.actor.actor_id,
        )
        ctx.create(refund)
        ctx.link(ticket, ticket_refund, refund)

    def _needs_manager(params: RefundParams, actor: Actor) -> bool:
        """≥ $100 的退款由 AI 提交时必须挂起等店长审批。"""
        return (
            params.amount_cents >= MANAGER_APPROVAL_THRESHOLD_CENTS
            and actor.role == "ai_agent"
        )

    @spec.action(
        "process_return_and_refund",
        RefundParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        approver_roles=("store_manager",),
        approval_rule=_needs_manager,
        affects_source=not standalone,
        post_approval=_record_refund,
        description=(
            "跨订单 + 工单 + 资金的核心治理动作（替代 Stripe+Shopify+Zendesk+Zapier 联动）。"
        ),
    )
    def process_return_and_refund(ctx: Any, params: RefundParams) -> None:
        ticket = ctx.require(SupportTicket, params.ticket_id)
        order = ctx.require(ShopOrder, ticket.order_id)
        customer = ctx.require(Customer, ticket.customer_id)

        # ① 超期拦截
        if order.age_days > RETURN_WINDOW_DAYS:
            raise InvariantError(
                f"order {order.order_id} is {order.age_days} days old; "
                f"return window is {RETURN_WINDOW_DAYS} days",
                code="RETURN_WINDOW_EXPIRED",
            )
        # ② 反欺诈拦截
        if customer.fraud_score >= FRAUD_BLOCK_THRESHOLD:
            raise InvariantError(
                f"customer {customer.customer_id} fraud_score "
                f"{customer.fraud_score} >= {FRAUD_BLOCK_THRESHOLD}",
                code="HIGH_FRAUD_RISK_BLOCKED",
            )
        # ③ 防重复退款与超额退款
        if order.status == "refunded":
            raise InvariantError(
                f"order {order.order_id} was already refunded",
                code="INVALID_REFUND_STATE",
            )
        if params.amount_cents != ticket.requested_refund_cents:
            raise InvariantError(
                f"refund amount {params.amount_cents} does not match the "
                f"ticket request {ticket.requested_refund_cents}",
                code="REFUND_AMOUNT_MISMATCH",
            )
        if params.amount_cents > order.amount_cents:
            raise InvariantError(
                f"refund {params.amount_cents} exceeds order amount "
                f"{order.amount_cents}",
                code="INVALID_REFUND_STATE",
            )

        # 主编辑计划只动「订单/工单/客户」这三条业务主线的状态；
        # 资金凭证 RefundRecord 属于纯 Ontology 自有状态，交给
        # post_approval 钩子，避免单次 diff 同时跨源系统与本体主权。
        ctx.modify(order, status="refunded")
        ctx.modify(ticket, status="resolved")
        ctx.modify(
            customer,
            lifetime_value_cents=customer.lifetime_value_cents - params.amount_cents,
        )

    # -- 5. 只读全景看板 ---------------------------------------------------
    @spec.function(
        "workspace_overview",
        OverviewParams,
        description=(
            "只读全景看板：ObjectSet + pivot + aggregate 汇总客户、GMV、"
            "工单状态、退款总额与被审计拦截数。"
        ),
    )
    def workspace_overview(ctx: Any, params: OverviewParams) -> dict[str, Any]:
        customers = ctx.query(Customer)
        orders = ctx.query(ShopOrder)
        tickets = ctx.query(SupportTicket)
        refunds = ctx.query(RefundRecord)

        ticket_status: dict[str, int] = {}
        for ticket in tickets:
            ticket_status[ticket.status] = ticket_status.get(ticket.status, 0) + 1

        overview: dict[str, Any] = {
            "customers": len(customers),
            "orders": len(orders),
            "gmv_cents": sum(o.amount_cents for o in orders),
            "tickets": len(tickets),
            "ticket_status": dict(sorted(ticket_status.items())),
            "refund_count": len(refunds),
            "refunded_cents": sum(r.amount_cents for r in refunds),
            "ltv_cents": sum(c.lifetime_value_cents for c in customers),
        }

        loader = ctx.loader
        if hasattr(loader, "search_set"):
            # ObjectSet -> pivot -> aggregate：把「订单集」沿 customer_orders
            # 反向枢轴回客户集，并按订单状态聚合金额。
            order_set = loader.search_set(ShopOrder, ctx.actor)
            customer_set = loader.pivot(
                order_set, customer_orders, ctx.actor, direction="reverse"
            )
            order_agg = loader.aggregate(
                order_set, group_by="status", sum_field="amount_cents"
            )
            overview["order_status_amounts"] = {
                str(row.key): row.metrics["sum"] for row in order_agg.rows
            }
            overview["pivot_customers"] = sorted(customer_set.ids)
        if hasattr(loader, "list_action_runs"):
            blocked: dict[str, int] = {}
            for run in loader.list_action_runs("REJECTED"):
                code = run.get("error_code") or "UNKNOWN"
                blocked[code] = blocked.get(code, 0) + 1
            overview["blocked_runs"] = dict(sorted(blocked.items()))
        return overview

    return SaasModel(
        spec=spec,
        standalone=standalone,
        Customer=Customer,
        ShopOrder=ShopOrder,
        SupportTicket=SupportTicket,
        RefundRecord=RefundRecord,
        customer_orders=customer_orders,
        order_tickets=order_tickets,
        ticket_refund=ticket_refund,
    )


def build_saas_ontology_spec(standalone: bool = False) -> OntologySpec:
    """规格书要求的工厂函数入口。"""
    return build_saas_model(standalone=standalone).spec


# ---------------------------------------------------------------------------
# Phase A 用：三套 SaaS 的快照（Shopify 订单 / Stripe 支付 / Zendesk 工单）
# ---------------------------------------------------------------------------
def build_saas_snapshot(model: SaasModel) -> tuple[dict[str, Any], dict[str, Any]]:
    """模拟从三套异构 SaaS 拉全量快照。

    返回 ``(objects, links)``，其中 objects 只含源系统字段
    （``source_properties()`` 已剥掉 Ontology 自有字段）。
    """
    customers = [
        model.Customer(
            customer_id="CUST-1001",
            email="wang@example.com",
            name="王小明",
            lifetime_value_cents=48_000,
        ),
        model.Customer(
            customer_id="CUST-1002",
            email="li@example.com",
            name="李静",
            lifetime_value_cents=25_999,
        ),
        model.Customer(
            customer_id="CUST-1003",
            email="zhao@example.com",
            name="赵六",
            lifetime_value_cents=15_000,
        ),
        model.Customer(
            customer_id="CUST-1004",
            email="sun@example.com",
            name="孙悦",
            lifetime_value_cents=32_000,
        ),
    ]
    orders = [
        model.ShopOrder(
            order_id="ORD-2001",
            customer_id="CUST-1001",
            item_sku="SKU-COFFEE-1KG",
            amount_cents=8_999,
            age_days=12,
            status="paid",
        ),
        model.ShopOrder(
            order_id="ORD-2002",
            customer_id="CUST-1002",
            item_sku="SKU-TEA-GIFT",
            amount_cents=25_999,
            age_days=45,
            status="shipped",
        ),
        model.ShopOrder(
            order_id="ORD-2003",
            customer_id="CUST-1003",
            item_sku="SKU-BLENDER",
            amount_cents=15_000,
            age_days=8,
            status="shipped",
        ),
        model.ShopOrder(
            order_id="ORD-2004",
            customer_id="CUST-1004",
            item_sku="SKU-ESPRESSO",
            amount_cents=32_000,
            age_days=3,
            status="paid",
        ),
    ]
    tickets = [
        model.SupportTicket(
            ticket_id="TKT-3001",
            order_id="ORD-2001",
            customer_id="CUST-1001",
            issue_type="damaged",
            requested_refund_cents=8_999,
        ),
        model.SupportTicket(
            ticket_id="TKT-3002",
            order_id="ORD-2002",
            customer_id="CUST-1002",
            issue_type="late",
            requested_refund_cents=25_999,
        ),
        model.SupportTicket(
            ticket_id="TKT-3003",
            order_id="ORD-2003",
            customer_id="CUST-1003",
            issue_type="remorse",
            requested_refund_cents=15_000,
        ),
        model.SupportTicket(
            ticket_id="TKT-3004",
            order_id="ORD-2004",
            customer_id="CUST-1004",
            issue_type="damaged",
            requested_refund_cents=32_000,
        ),
    ]
    objects = {
        "Customer": [c.source_properties() for c in customers],
        "ShopOrder": [o.source_properties() for o in orders],
        "SupportTicket": [t.source_properties() for t in tickets],
    }
    links = {
        "customer_orders": [(o.customer_id, o.order_id) for o in orders],
        "order_tickets": [(t.order_id, t.ticket_id) for t in tickets],
    }
    return objects, links


# ---------------------------------------------------------------------------
# 上层 AI Agent 的 Tool 层（两种模式共用同一份，零改动）
# ---------------------------------------------------------------------------
def agent_tools(runtime: OntologyRuntime, actor: Actor = AI_AGENT) -> dict[str, Any]:
    """把 Action / Function 暴露成 AI Agent 可直接调用的 Tool 字典。"""
    return {
        "create_customer_and_order": lambda run_id, **kw: runtime.submit_action(
            run_id, "create_customer_and_order", kw, actor
        ),
        "open_support_ticket": lambda run_id, **kw: runtime.submit_action(
            run_id, "open_support_ticket", kw, actor
        ),
        "ai_triage_ticket": lambda run_id, **kw: runtime.submit_action(
            run_id, "ai_triage_ticket", kw, actor
        ),
        "process_return_and_refund": lambda run_id, **kw: runtime.submit_action(
            run_id, "process_return_and_refund", kw, actor
        ),
        "workspace_overview": lambda: runtime.call_function(
            "workspace_overview", {}, actor
        ),
    }


# ---------------------------------------------------------------------------
# AIP Evals：同一套测试集，跑在两种 Runtime 上必须给出同一个契约答案
# ---------------------------------------------------------------------------
AIP_EVALS = [
    "eval_create_and_small_refund_auto",
    "eval_expired_window_blocked",
    "eval_high_fraud_blocked",
    "eval_big_refund_requires_manager",
    "eval_double_refund_blocked",
    "eval_revert_refund",
    "eval_overview_snapshot",
]


def run_eval_suite(standalone: bool) -> dict[str, Any]:
    """在指定主权模式下从零跑一遍 AIP Evals，返回可逐字段比对的契约结果。"""
    model = build_saas_model(standalone=standalone)
    rt = model.new_runtime()
    try:
        out: dict[str, Any] = {}

        # --- eval 1: 建客下单 + 小额 AI 秒退 ------------------------------
        rt.submit_action(
            "EVAL-CREATE-1",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E1",
                "email": "e1@example.com",
                "name": "评测客户一",
                "order_id": "ORD-E1",
                "item_sku": "SKU-E1",
                "amount_cents": 4_500,
                "age_days": 5,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-1",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E1",
                "order_id": "ORD-E1",
                "issue_type": "damaged",
                "requested_refund_cents": 4_500,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TRIAGE-1",
            "ai_triage_ticket",
            {"ticket_id": "TKT-E1", "fraud_score": 10, "vip_tier": "STANDARD"},
            AI_AGENT,
        )
        small = rt.submit_action(
            "EVAL-REFUND-1",
            "process_return_and_refund",
            {"ticket_id": "TKT-E1", "refund_id": "RFD-E1", "amount_cents": 4_500},
            AI_AGENT,
        )
        out["small_refund"] = {
            "run_status": small["status"],
            "order_status": rt.get_object(
                model.ShopOrder, "ORD-E1", STORE_MANAGER
            ).status,
            "ticket_status": rt.get_object(
                model.SupportTicket, "TKT-E1", STORE_MANAGER
            ).status,
            "refund_amount": rt.get_object(
                model.RefundRecord, "RFD-E1", STORE_MANAGER
            ).amount_cents,
        }

        # --- eval 2: 超 30 天退货被拦 ------------------------------------
        rt.submit_action(
            "EVAL-CREATE-2",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E2",
                "email": "e2@example.com",
                "name": "评测客户二",
                "order_id": "ORD-E2",
                "item_sku": "SKU-E2",
                "amount_cents": 25_999,
                "age_days": 40,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-2",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E2",
                "order_id": "ORD-E2",
                "issue_type": "late",
                "requested_refund_cents": 25_999,
            },
            AI_AGENT,
        )
        try:
            rt.submit_action(
                "EVAL-REFUND-2",
                "process_return_and_refund",
                {"ticket_id": "TKT-E2", "refund_id": "RFD-E2", "amount_cents": 25_999},
                AI_AGENT,
            )
            out["expired"] = {"error": None, "run_status": None}
        except InvariantError as exc:
            out["expired"] = {
                "error": exc.code,
                "run_status": rt.get_action_run("EVAL-REFUND-2")["status"],
            }

        # --- eval 3: 高欺诈分被拦 ----------------------------------------
        rt.submit_action(
            "EVAL-CREATE-3",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E3",
                "email": "e3@example.com",
                "name": "评测客户三",
                "order_id": "ORD-E3",
                "item_sku": "SKU-E3",
                "amount_cents": 15_000,
                "age_days": 8,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-3",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E3",
                "order_id": "ORD-E3",
                "issue_type": "remorse",
                "requested_refund_cents": 15_000,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TRIAGE-3",
            "ai_triage_ticket",
            {"ticket_id": "TKT-E3", "fraud_score": 85, "vip_tier": "STANDARD"},
            AI_AGENT,
        )
        try:
            rt.submit_action(
                "EVAL-REFUND-3",
                "process_return_and_refund",
                {"ticket_id": "TKT-E3", "refund_id": "RFD-E3", "amount_cents": 15_000},
                AI_AGENT,
            )
            out["fraud"] = {"error": None, "run_status": None}
        except InvariantError as exc:
            out["fraud"] = {
                "error": exc.code,
                "run_status": rt.get_action_run("EVAL-REFUND-3")["status"],
            }

        # --- eval 4: 大额退款挂起 -> 店长审批放行 --------------------------
        rt.submit_action(
            "EVAL-CREATE-4",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E4",
                "email": "e4@example.com",
                "name": "评测客户四",
                "order_id": "ORD-E4",
                "item_sku": "SKU-E4",
                "amount_cents": 32_000,
                "age_days": 2,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-4",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E4",
                "order_id": "ORD-E4",
                "issue_type": "damaged",
                "requested_refund_cents": 32_000,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TRIAGE-4",
            "ai_triage_ticket",
            {"ticket_id": "TKT-E4", "fraud_score": 20, "vip_tier": "VIP"},
            AI_AGENT,
        )
        pending = rt.submit_action(
            "EVAL-REFUND-4",
            "process_return_and_refund",
            {"ticket_id": "TKT-E4", "refund_id": "RFD-E4", "amount_cents": 32_000},
            AI_AGENT,
        )
        order_while_pending = rt.get_object(model.ShopOrder, "ORD-E4", STORE_MANAGER)
        approved = rt.approve_action("EVAL-REFUND-4", STORE_MANAGER)
        out["big_refund"] = {
            "submit_status": pending["status"],
            "order_status_while_pending": order_while_pending.status,
            "approve_status": approved["status"],
            "approved_by": approved["approved_by"],
            "order_status_after": rt.get_object(
                model.ShopOrder, "ORD-E4", STORE_MANAGER
            ).status,
        }

        # --- eval 5: 重复退款被拦 ----------------------------------------
        try:
            rt.submit_action(
                "EVAL-REFUND-5",
                "process_return_and_refund",
                {"ticket_id": "TKT-E1", "refund_id": "RFD-E5", "amount_cents": 4_500},
                AI_AGENT,
            )
            out["double_refund"] = {"error": None, "run_status": None}
        except InvariantError as exc:
            out["double_refund"] = {
                "error": exc.code,
                "run_status": rt.get_action_run("EVAL-REFUND-5")["status"],
            }

        # --- eval 6: 时光机撤回退款 --------------------------------------
        reverted = rt.revert_action(
            "EVAL-REFUND-4", "EVAL-REFUND-4-REV", STORE_MANAGER
        )
        refund_exists = (
            rt.connection.execute(
                "SELECT 1 FROM ontology_objects "
                "WHERE object_type='RefundRecord' AND object_id='RFD-E4'"
            ).fetchone()
            is not None
        )
        out["revert"] = {
            "status": reverted["status"],
            "order_status": rt.get_object(
                model.ShopOrder, "ORD-E4", STORE_MANAGER
            ).status,
            "refund_exists": refund_exists,
            "customer_ltv": rt.get_object(
                model.Customer, "CUST-E4", STORE_MANAGER
            ).lifetime_value_cents,
        }

        # --- eval 7: 全景看板 --------------------------------------------
        out["overview"] = rt.call_function("workspace_overview", {}, STORE_MANAGER)
        return out
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 演示主程序
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 82)
    print(title)
    print("=" * 82)


def _err(exc: InvariantError) -> str:
    return f"{exc.code}: {exc}"


def phase_a_parasitic() -> dict[str, Any]:
    """Phase A：Ontology 寄生在 Shopify + Stripe + Zendesk 之上。"""
    _section("PHASE A · 寄生态：Ontology 盖在 Shopify + Stripe + Zendesk 三套 SaaS 上")

    model = build_saas_model(standalone=False)
    rt = model.new_runtime()
    try:
        objects, links = build_saas_snapshot(model)
        result = rt.load_snapshot(objects, links)
        print(
            f"  从 3 套 SaaS 导入快照: objects={result['objects']} "
            f"links={result['links']} types={result['object_types']}"
        )
        print(
            "  对象主权: "
            f"Customer.__owned__={model.Customer.owned_spec()} "
            f"ShopOrder.__owned__={model.ShopOrder.owned_spec()}"
        )
        print(f"          SupportTicket.__owned__={model.SupportTicket.owned_spec()}")
        print(f"          RefundRecord.__owned__={model.RefundRecord.owned_spec()}")

        # --- overlay 赋权 ------------------------------------------------
        print("\n  [A1] overlay 赋权：AI 分诊写入 Ontology 自有风控字段")
        rt.submit_action(
            "A-TRIAGE-3001",
            "ai_triage_ticket",
            {
                "ticket_id": "TKT-3001",
                "fraud_score": 12,
                "vip_tier": "VIP",
                "note": "AI: 老客 VIP，外箱破损，建议全额退",
            },
            AI_AGENT,
        )
        rt.submit_action(
            "A-TRIAGE-3003",
            "ai_triage_ticket",
            {
                "ticket_id": "TKT-3003",
                "fraud_score": 92,
                "vip_tier": "STANDARD",
                "note": "AI: 同设备 7 天内第 4 次退款申请，风控高危",
            },
            AI_AGENT,
        )
        rt.submit_action(
            "A-TRIAGE-3004",
            "ai_triage_ticket",
            {
                "ticket_id": "TKT-3004",
                "fraud_score": 18,
                "vip_tier": "VIP",
                "note": "AI: 高客单老客，建议店长复核后全额退",
            },
            AI_AGENT,
        )
        overlay_rows = rt.connection.execute(
            "SELECT object_type, object_id, patch_json FROM ontology_overlay "
            "ORDER BY object_id"
        ).fetchall()
        for row in overlay_rows:
            print(f"    overlay {row['object_type']}:{row['object_id']} -> {row['patch_json']}")
        print(f"    ontology_overlay 行数 = {len(overlay_rows)}")
        status_before = {
            t.ticket_id: t.status
            for t in rt.query_objects(model.SupportTicket, STORE_MANAGER)
        }
        print(f"    分诊后工单状态（Zendesk 源字段）: {status_before}")

        # --- 重刷存活 ----------------------------------------------------
        print("\n  [A2] 三套 SaaS 全量重刷（load_snapshot）后 overlay 是否存活")
        before = {
            c.customer_id: (c.fraud_score, c.vip_tier)
            for c in rt.query_objects(model.Customer, STORE_MANAGER)
        }
        objects2, links2 = build_saas_snapshot(model)
        result2 = rt.load_snapshot(objects2, links2)
        after = {
            c.customer_id: (c.fraud_score, c.vip_tier)
            for c in rt.query_objects(model.Customer, STORE_MANAGER)
        }
        print(
            f"    重刷结果: objects={result2['objects']} "
            f"overlays_reapplied={result2['overlays_reapplied']}"
        )
        print(f"    重刷前 {before}")
        print(f"    重刷后 {after}")
        print(f"    overlay preserved = {before == after}")
        status_after = {
            t.ticket_id: t.status
            for t in rt.query_objects(model.SupportTicket, STORE_MANAGER)
        }
        notes_after = {
            t.ticket_id: t.ai_triage_note
            for t in rt.query_objects(model.SupportTicket, STORE_MANAGER)
            if t.ai_triage_note
        }
        print(f"    重刷后工单状态（Zendesk 源字段，被还原）: {status_after}")
        print(f"    重刷后 AI 分诊备注（Ontology 自有字段，存活）: {sorted(notes_after)}")
        print(
            "    主权边界：源系统字段归 Zendesk，重刷即还原；"
            "风控字段归 Ontology，重刷杀不死。"
        )

        # --- 跨 SaaS 退款护栏 --------------------------------------------
        print("\n  [A3] 跨 SaaS 退货退款护栏（Shopify 订单 + Zendesk 工单 + Stripe 资金）")

        try:
            rt.submit_action(
                "A-REFUND-3002",
                "process_return_and_refund",
                {
                    "ticket_id": "TKT-3002",
                    "refund_id": "RFD-3002",
                    "amount_cents": 25_999,
                },
                AI_AGENT,
            )
            print("    ORD-2002: 未被拦截（异常）")
        except InvariantError as exc:
            run = rt.get_action_run("A-REFUND-3002")
            print(f"    ORD-2002 45 天 -> {_err(exc)}  [run={run['status']}]")

        try:
            rt.submit_action(
                "A-REFUND-3003",
                "process_return_and_refund",
                {
                    "ticket_id": "TKT-3003",
                    "refund_id": "RFD-3003",
                    "amount_cents": 15_000,
                },
                AI_AGENT,
            )
            print("    ORD-2003: 未被拦截（异常）")
        except InvariantError as exc:
            run = rt.get_action_run("A-REFUND-3003")
            print(f"    ORD-2003 fraud=92 -> {_err(exc)}  [run={run['status']}]")

        small = rt.submit_action(
            "A-REFUND-3001",
            "process_return_and_refund",
            {"ticket_id": "TKT-3001", "refund_id": "RFD-3001", "amount_cents": 8_999},
            AI_AGENT,
        )
        print(
            f"    ORD-2001 $89.99 小额 -> {small['status']} 自动秒退 "
            f"(无需审批)"
        )

        pending = rt.submit_action(
            "A-REFUND-3004",
            "process_return_and_refund",
            {"ticket_id": "TKT-3004", "refund_id": "RFD-3004", "amount_cents": 32_000},
            AI_AGENT,
        )
        print(
            f"    ORD-2004 $320.00 大额 -> {pending['status']} "
            f"(AI 提交，等店长审批)"
        )
        order_pending = rt.get_object(model.ShopOrder, "ORD-2004", STORE_MANAGER)
        print(f"      挂起期间 ORD-2004.status = {order_pending.status}（未动账）")
        approved = rt.approve_action("A-REFUND-3004", STORE_MANAGER)
        print(
            f"      店长审批 -> {approved['status']} by {approved['approved_by']}，"
            f"订单状态 = {rt.get_object(model.ShopOrder, 'ORD-2004', STORE_MANAGER).status}"
        )

        # --- 时光机撤回 --------------------------------------------------
        print("\n  [A4] 时光机撤回 A-REFUND-3004（把退款与扣减的 LTV 全部倒回）")
        reverted = rt.revert_action(
            "A-REFUND-3004", "A-REFUND-3004-REV", STORE_MANAGER
        )
        print(f"    撤回 run={reverted['run_id']} status={reverted['status']}")
        print(
            f"    ORD-2004.status = "
            f"{rt.get_object(model.ShopOrder, 'ORD-2004', STORE_MANAGER).status}，"
            f"CUST-1004.ltv = "
            f"{rt.get_object(model.Customer, 'CUST-1004', STORE_MANAGER).lifetime_value_cents}"
        )

        overview = rt.call_function("workspace_overview", {}, STORE_MANAGER)
        print("\n  [A5] 全景看板 workspace_overview")
        for key, value in overview.items():
            print(f"    {key} = {value}")
        return {"overview": overview, "overlay_rows": len(overlay_rows)}
    finally:
        rt.close()


def phase_b_standalone() -> dict[str, Any]:
    """Phase B：拔掉全部 SaaS 插头，纯 Ontology 独立运行。"""
    _section("PHASE B · 脱壳态：拔掉全部 SaaS 插头，standalone=True 纯 Ontology 原生运行")

    model = build_saas_model(standalone=True)
    rt = model.new_runtime()
    try:
        print("  零外部数据源：不导入任何 SaaS 快照，直接由 Ontology Action 造数据")
        print(
            "  对象主权: "
            f"Customer.__owned__={model.Customer.owned_spec()} "
            f"ShopOrder.__owned__={model.ShopOrder.owned_spec()} "
            f"SupportTicket.__owned__={model.SupportTicket.owned_spec()} "
            f"RefundRecord.__owned__={model.RefundRecord.owned_spec()}"
        )
        print(f"  启动时对象数 = {rt.count_objects()}（空库）")

        tools = agent_tools(rt, AI_AGENT)

        # --- 原生建客下单 -------------------------------------------------
        print("\n  [B1] 原生建客下单（替代 Shopify）")
        tools["create_customer_and_order"](
            "B-CREATE-1",
            customer_id="CUST-B01",
            email="b01@example.com",
            name="脱壳客户一",
            order_id="ORD-B01",
            item_sku="SKU-BLENDER",
            amount_cents=15_000,
            age_days=8,
        )
        tools["create_customer_and_order"](
            "B-CREATE-2",
            customer_id="CUST-B02",
            email="b02@example.com",
            name="脱壳客户二",
            order_id="ORD-B02",
            item_sku="SKU-TEA-GIFT",
            amount_cents=25_999,
            age_days=45,
        )
        tools["create_customer_and_order"](
            "B-CREATE-3",
            customer_id="CUST-B03",
            email="b03@example.com",
            name="脱壳客户三",
            order_id="ORD-B03",
            item_sku="SKU-ESPRESSO",
            amount_cents=32_000,
            age_days=2,
        )
        tools["create_customer_and_order"](
            "B-CREATE-4",
            customer_id="CUST-B04",
            email="b04@example.com",
            name="脱壳客户四",
            order_id="ORD-B04",
            item_sku="SKU-COFFEE-1KG",
            amount_cents=8_999,
            age_days=12,
        )
        print(f"    对象数 = {rt.count_objects()}（Customer 4 + ShopOrder 4）")
        print(
            f"    ontology_overlay 行数 = "
            f"{rt.connection.execute('SELECT COUNT(*) FROM ontology_overlay').fetchone()[0]}"
            "（全自有对象，无需补丁层）"
        )

        # --- 原生建工单 ---------------------------------------------------
        print("\n  [B2] 原生建客诉工单（替代 Zendesk）")
        for run_id, ticket_id, order_id, issue, amount in (
            ("B-TICKET-1", "TKT-B01", "ORD-B01", "remorse", 15_000),
            ("B-TICKET-2", "TKT-B02", "ORD-B02", "late", 25_999),
            ("B-TICKET-3", "TKT-B03", "ORD-B03", "damaged", 32_000),
            ("B-TICKET-4", "TKT-B04", "ORD-B04", "damaged", 8_999),
        ):
            tools["open_support_ticket"](
                run_id,
                ticket_id=ticket_id,
                order_id=order_id,
                issue_type=issue,
                requested_refund_cents=amount,
            )
        print(f"    对象数 = {rt.count_objects()}（+ SupportTicket 4）")

        # --- AI 分诊 ------------------------------------------------------
        print("\n  [B3] AI 风控分诊（写本体字段，无外部 SaaS 可写）")
        for run_id, ticket_id, fraud, vip in (
            ("B-TRIAGE-1", "TKT-B01", 92, "STANDARD"),
            ("B-TRIAGE-2", "TKT-B02", 15, "STANDARD"),
            ("B-TRIAGE-3", "TKT-B03", 18, "VIP"),
            ("B-TRIAGE-4", "TKT-B04", 8, "VIP"),
        ):
            tools["ai_triage_ticket"](
                run_id, ticket_id=ticket_id, fraud_score=fraud, vip_tier=vip
            )
        for ticket_id in ("TKT-B01", "TKT-B02", "TKT-B03", "TKT-B04"):
            ticket = rt.get_object(model.SupportTicket, ticket_id, STORE_MANAGER)
            print(
                f"    {ticket_id}: status={ticket.status} note={ticket.ai_triage_note}"
            )

        # --- 三条硬护栏 ---------------------------------------------------
        print("\n  [B4] 退货退款硬护栏（超期 / 高欺诈 / 重复超额）")
        try:
            tools["process_return_and_refund"](
                "B-REFUND-2",
                ticket_id="TKT-B02",
                refund_id="RFD-B02",
                amount_cents=25_999,
            )
        except InvariantError as exc:
            run = rt.get_action_run("B-REFUND-2")
            print(f"    ORD-B02 45 天 -> {_err(exc)}  [run={run['status']}]")

        try:
            tools["process_return_and_refund"](
                "B-REFUND-1",
                ticket_id="TKT-B01",
                refund_id="RFD-B01",
                amount_cents=15_000,
            )
        except InvariantError as exc:
            run = rt.get_action_run("B-REFUND-1")
            print(f"    ORD-B01 fraud=92 -> {_err(exc)}  [run={run['status']}]")

        # --- 小额秒退 / 大额审批 ------------------------------------------
        print("\n  [B5] 分级审批：小额 AI 秒退，大额店长签批")
        small = tools["process_return_and_refund"](
            "B-REFUND-4",
            ticket_id="TKT-B04",
            refund_id="RFD-B04",
            amount_cents=8_999,
        )
        print(f"    ORD-B04 $89.99 -> {small['status']} 自动秒退")

        pending = tools["process_return_and_refund"](
            "B-REFUND-3",
            ticket_id="TKT-B03",
            refund_id="RFD-B03",
            amount_cents=32_000,
        )
        print(f"    ORD-B03 $320.00 -> {pending['status']} 等待店长审批")
        order_pending = rt.get_object(model.ShopOrder, "ORD-B03", STORE_MANAGER)
        print(f"      挂起期间 ORD-B03.status = {order_pending.status}（未动账）")
        approved = rt.approve_action("B-REFUND-3", STORE_MANAGER)
        print(
            f"      店长审批 -> {approved['status']} by {approved['approved_by']}"
        )

        # --- 重复退款拦截 -------------------------------------------------
        print("\n  [B6] 防重复退款")
        try:
            tools["process_return_and_refund"](
                "B-REFUND-DUP",
                ticket_id="TKT-B04",
                refund_id="RFD-B04-DUP",
                amount_cents=8_999,
            )
        except InvariantError as exc:
            run = rt.get_action_run("B-REFUND-DUP")
            print(f"    TKT-B04 二次退款 -> {_err(exc)}  [run={run['status']}]")

        # --- 时光机撤回 ---------------------------------------------------
        print("\n  [B7] 时光机一键撤回 B-REFUND-3")
        reverted = rt.revert_action("B-REFUND-3", "B-REFUND-3-REV", STORE_MANAGER)
        print(f"    撤回 run={reverted['run_id']} status={reverted['status']}")
        print(
            f"    ORD-B03.status = "
            f"{rt.get_object(model.ShopOrder, 'ORD-B03', STORE_MANAGER).status}，"
            f"CUST-B03.ltv = "
            f"{rt.get_object(model.Customer, 'CUST-B03', STORE_MANAGER).lifetime_value_cents}"
        )

        overview = tools["workspace_overview"]()
        print("\n  [B8] 全景看板 workspace_overview")
        for key, value in overview.items():
            print(f"    {key} = {value}")
        return {"overview": overview}
    finally:
        rt.close()


def main() -> int:
    print("=" * 82)
    print("TK-ONTO-009 · 用 Ontology 去掉 SaaS：从寄生多云 SaaS 到脱壳独立运行")
    print("=" * 82)

    phase_a_parasitic()
    phase_b_standalone()

    _section("契约冻结证明 · 同一套 AIP Evals 跑在两种 Runtime 上")
    print(f"  Evals 用例 ({len(AIP_EVALS)} 条): {', '.join(AIP_EVALS)}")
    evals_parasitic = run_eval_suite(standalone=False)
    evals_standalone = run_eval_suite(standalone=True)

    for name in AIP_EVALS:
        key = name.replace("eval_", "")
        key = {
            "create_and_small_refund_auto": "small_refund",
            "expired_window_blocked": "expired",
            "high_fraud_blocked": "fraud",
            "big_refund_requires_manager": "big_refund",
            "double_refund_blocked": "double_refund",
            "revert_refund": "revert",
            "overview_snapshot": "overview",
        }[key]
        same = evals_parasitic[key] == evals_standalone[key]
        print(f"    {'✓' if same else '✗'} {name}: {'一致' if same else '不一致'}")

    print("\n  --- 寄生态 (SaaS) 结果 ---")
    for key in (
        "small_refund",
        "expired",
        "fraud",
        "big_refund",
        "double_refund",
        "revert",
    ):
        print(f"    {key} = {evals_parasitic[key]}")
    print("\n  --- 脱壳态 (standalone) 结果 ---")
    for key in (
        "small_refund",
        "expired",
        "fraud",
        "big_refund",
        "double_refund",
        "revert",
    ):
        print(f"    {key} = {evals_standalone[key]}")

    contract_identical = evals_parasitic == evals_standalone
    print()
    print("=" * 82)
    print(f"  contract_identical = {contract_identical}")
    print("  同一套 Action / Tool / Evals，拔掉 SaaS 后上层业务代码零改动。")
    print("=" * 82)
    return 0 if contract_identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
