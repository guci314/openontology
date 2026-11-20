"""多云 SaaS 寄居吸纳与脱壳纯业务本体（SaasUnplugOntology · PIM）。

纯业务声明模块，不包含 Runtime 实例化或底层演示脚本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from open_ontology.spec import (
    Actor,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)

try:
    from .business_ontology import (
        WorkCard,
        register_work_card_primitives,
        settle_work_card,
        submit_or_open_work_card,
    )
except ImportError:
    from examples.business_ontology import (
        WorkCard,
        register_work_card_primitives,
        settle_work_card,
        submit_or_open_work_card,
    )

__all__ = [
    "RETURN_WINDOW_DAYS",
    "MANAGER_APPROVAL_THRESHOLD_CENTS",
    "FRAUD_BLOCK_THRESHOLD",
    "AI_AGENT",
    "CS_AGENT",
    "STORE_MANAGER",
    "PARASITIC_OWNED_FIELDS",
    "WorkCard",
    "settle_work_card",
    "submit_or_open_work_card",
    "SaasOntologyBundle",
    "build_saas_ontology",
    "build_saas_ontology_spec",
]

RETURN_WINDOW_DAYS = 30
MANAGER_APPROVAL_THRESHOLD_CENTS = 100_00
FRAUD_BLOCK_THRESHOLD = 80

AI_AGENT = Actor(actor_id="ai-agent-01", role="ai_agent", clearance_tags={"*"})
CS_AGENT = Actor(actor_id="cs-agent-01", role="cs_agent", clearance_tags={"*"})
STORE_MANAGER = Actor(
    actor_id="store-manager-01", role="store_manager", clearance_tags={"*"}
)

PARASITIC_OWNED_FIELDS: dict[str, dict[str, Any]] = {
    "Customer": {"fraud_score": 0, "vip_tier": "STANDARD"},
    "ShopOrder": {"ops_memo": None},
    "SupportTicket": {"ai_triage_note": None},
}


@dataclass
class SaasOntologyBundle:
    """一次 spec 构建产出的纯 M1 句柄（类、边、spec，不含 Runtime）。"""

    spec: OntologySpec
    standalone: bool
    Customer: type[ObjectType]
    ShopOrder: type[ObjectType]
    SupportTicket: type[ObjectType]
    RefundRecord: type[ObjectType]
    customer_orders: ObjectProperty
    order_tickets: ObjectProperty
    ticket_refund: ObjectProperty

    def object_type(self, name: str) -> type[ObjectType]:
        return self.spec.object_types[name]


def build_saas_ontology(standalone: bool = False) -> SaasOntologyBundle:
    """构建整套 SaaS 纯业务 M1 本体。"""
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

    class Customer(ObjectType):
        """客户（寄生态来自外部客户快照，脱壳态由 Ontology 原生持有）。"""

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
        """订单（寄生态来自外部订单快照，脱壳态由 Ontology 原生持有）。"""

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
        """客诉工单（寄生态来自外部工单快照，脱壳态由 Ontology 原生持有）。"""

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

    customer_orders = ObjectProperty(
        name="customer_orders",
        domain="Customer",
        range="ShopOrder",
        cardinality="one-to-many",
        owned=standalone,
        description="客户 -> 订单",
    )
    order_tickets = ObjectProperty(
        name="order_tickets",
        domain="ShopOrder",
        range="SupportTicket",
        cardinality="one-to-many",
        owned=standalone,
        description="订单 -> 客诉工单",
    )
    ticket_refund = ObjectProperty(
        name="ticket_refund",
        domain="SupportTicket",
        range="RefundRecord",
        cardinality="one-to-one",
        owned=True,
        description="工单 -> 退款凭证",
    )
    for prop in (customer_orders, order_tickets, ticket_refund):
        spec.add_object_property(prop)

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

    class TriageStatusParams(BaseModel):
        ticket_id: str

    class RefundParams(BaseModel):
        ticket_id: str
        refund_id: str
        amount_cents: int
        approved_by: str | None = None

    class OverviewParams(BaseModel):
        pass

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
        description="原生建客诉工单：校验订单存在并建立 order_tickets 边。",
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

    @spec.action(
        "ai_triage_ticket",
        TriageParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        revert_roles=("store_manager",),
        affects_source=False,
        description="AI 智能分诊与反欺诈评分：写客户风控字段与工单分诊备注（单平面自有写）。",
    )
    def ai_triage_ticket(ctx: Any, params: TriageParams) -> None:
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

    @spec.action(
        "mark_ticket_triaged",
        TriageStatusParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        revert_roles=("store_manager",),
        affects_source=not standalone,
        description="工单分诊状态推进：单平面推进 SupportTicket.status 为 triaged。",
    )
    def mark_ticket_triaged(ctx: Any, params: TriageStatusParams) -> None:
        ticket = ctx.require(SupportTicket, params.ticket_id)
        ctx.modify(ticket, status="triaged")

    @spec.action(
        "process_return_and_refund",
        RefundParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        revert_roles=("store_manager",),
        affects_source=not standalone,
        description="跨订单 + 工单 + 资金的核心治理动作（单平面源状态变更，大额 AI 请求需店长签批）。",
    )
    def process_return_and_refund(ctx: Any, params: RefundParams) -> None:
        ticket = ctx.require(SupportTicket, params.ticket_id)
        order = ctx.require(ShopOrder, ticket.order_id)
        customer = ctx.require(Customer, ticket.customer_id)

        if order.age_days > RETURN_WINDOW_DAYS:
            raise InvariantError(
                f"order {order.order_id} is {order.age_days} days old; "
                f"return window is {RETURN_WINDOW_DAYS} days",
                code="RETURN_WINDOW_EXPIRED",
            )
        if customer.fraud_score >= FRAUD_BLOCK_THRESHOLD:
            raise InvariantError(
                f"customer {customer.customer_id} fraud_score "
                f"{customer.fraud_score} >= {FRAUD_BLOCK_THRESHOLD}",
                code="HIGH_FRAUD_RISK_BLOCKED",
            )
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
        if (
            params.amount_cents >= MANAGER_APPROVAL_THRESHOLD_CENTS
            and ctx.actor.role == "ai_agent"
        ):
            raise InvariantError(
                f"AI refund request of {params.amount_cents} cents requires store_manager approval",
                code="APPROVAL_REQUIRED_LARGE_REFUND",
            )

        ctx.modify(order, status="refunded")
        ctx.modify(ticket, status="resolved")
        ctx.modify(
            customer,
            lifetime_value_cents=customer.lifetime_value_cents - params.amount_cents,
        )

    @spec.action(
        "record_refund_voucher",
        RefundParams,
        allowed_roles=("ai_agent", "cs_agent", "store_manager"),
        revert_roles=("store_manager",),
        affects_source=False,
        description="登记全自有退款凭证 RefundRecord 并关联 ticket_refund 边（单平面自有写）。",
    )
    def record_refund_voucher(ctx: Any, params: RefundParams) -> None:
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

    @spec.function(
        "workspace_overview",
        OverviewParams,
        description=(
            "只读全景看板：汇总客户、GMV、工单状态、退款总额与被审计拦截数。"
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
                if str(code).startswith("APPROVAL_REQUIRED_"):
                    continue
                blocked[code] = blocked.get(code, 0) + 1
            overview["blocked_runs"] = dict(sorted(blocked.items()))
        return overview

    spec.update_display(
        {
            "ontologies": {
                "crm_saas_unplug": "多云 SaaS 寄居吸纳与脱壳",
            },
            "categories": {
                "Customer 360": "客户全景",
            },
            "objects": {
                "Customer": "客户",
                "ShopOrder": "店铺订单",
                "SupportTicket": "客服工单",
                "RefundRecord": "退款记录",
            },
            "object_properties": {
                "customer_orders": "客户订单",
                "order_tickets": "订单工单",
                "ticket_refund": "工单退款",
            },
            "fields": {
                "customer_id": "客户编号",
                "email": "电子邮箱",
                "lifetime_value_cents": "客户终身价值（分）",
                "fraud_score": "风险评分",
                "vip_tier": "客户等级",
                "order_id": "订单编号",
                "item_sku": "商品编码",
                "amount_cents": "金额（分）",
                "age_days": "已过天数",
                "ops_memo": "运营备注",
                "ticket_id": "工单编号",
                "issue_type": "问题类型",
                "requested_refund_cents": "申请退款金额（分）",
                "ai_triage_note": "智能分诊备注",
                "refund_id": "退款编号",
                "approved_by": "批准人",
            },
            "actions": {
                "create_customer_and_order": "创建客户与订单",
                "open_support_ticket": "创建客服工单",
                "ai_triage_ticket": "智能分诊工单",
                "mark_ticket_triaged": "更新工单分诊状态",
                "process_return_and_refund": "处理退货退款",
                "record_refund_voucher": "登记退款凭证",
            },
            "functions": {
                "workspace_overview": "工作区概览",
            },
            "roles": {
                "ai_agent": "智能助手",
                "cs_agent": "客服专员",
                "store_manager": "店铺经理",
                "sales": "销售人员",
            },
            "statuses": {
                "paid": "已支付",
                "shipped": "已发货",
                "triaged": "已分诊",
                "refunded": "已退款",
            },
        }
    )

    register_work_card_primitives(
        spec,
        opener_roles=("ai_agent", "cs_agent", "store_manager"),
        resolver_roles=("store_manager",),
    )

    return SaasOntologyBundle(
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
    return build_saas_ontology(standalone=standalone).spec
