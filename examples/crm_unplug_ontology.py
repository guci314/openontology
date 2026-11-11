"""纯业务「双开源 SaaS 吸干后拔插头」本体模型（PIM）。

业务对象：客户账户、销售商机、客服工单、销售订单、退款结算单。
不包含任何外部系统客户端、协议或表名。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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

# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class CustomerAccount(ObjectType):
    """客户账户（来源：外部 CRM 客户主数据）。"""

    __primary_key__ = "account_id"
    __title__ = "name"
    __owned__ = {
        "vip_tier": "STANDARD",
        "fraud_score": 0,
        "account_manager": None,
    }

    account_id: str
    name: str
    industry: str = ""
    city: str = ""
    country: str = ""
    annual_revenue_cents: int = 0
    ltv_cents: int = 0
    vip_tier: str = "STANDARD"  # STANDARD | SILVER | GOLD
    fraud_score: int = 0
    account_manager: str | None = None


class SalesOpportunity(ObjectType):
    """销售商机（来源：外部 CRM 商机）。"""

    __primary_key__ = "opportunity_id"
    __title__ = "name"
    __owned__ = {"ai_next_step": None, "win_probability": 0}

    opportunity_id: str
    name: str
    account_id: str
    stage: str = "PROSPECTING"
    amount_cents: int = 0
    currency: str = "USD"
    expected_close_date: str | None = None
    ai_next_step: str | None = None
    win_probability: int = 0


class SupportCase(ObjectType):
    """客服工单（来源：外部 CRM 客诉）。"""

    __primary_key__ = "case_id"
    __title__ = "subject"
    __owned__ = {
        "fraud_score": 0,
        "vip_tier": "STANDARD",
        "ai_resolution_plan": None,
    }

    case_id: str
    subject: str
    account_id: str
    status: str = "New"  # New | In Progress | Resolved | Closed
    priority: str = "Medium"
    case_type: str = "Problem"
    opened_at: str | None = None
    description: str = ""
    fraud_score: int = 0
    vip_tier: str = "STANDARD"
    ai_resolution_plan: str | None = None


class SalesOrder(ObjectType):
    """销售订单（来源：外部 ERP）。"""

    __primary_key__ = "order_id"
    __title__ = "order_ref"
    __owned__ = {"fulfillment_note": None, "refunded_cents": 0}

    order_id: str
    order_ref: str
    account_id: str
    status: str = "draft"  # draft | confirmed | shipped | invoiced | paid | refunded
    total_cents: int = 0
    currency: str = "USD"
    ordered_on: str | None = None
    fulfillment_note: str | None = None
    refunded_cents: int = 0


class RefundSettlement(ObjectType):
    """退款结算单（完全由 Ontology 自有）。"""

    __primary_key__ = "settlement_id"
    __title__ = "settlement_id"
    __owned__ = True

    settlement_id: str
    case_id: str
    order_id: str
    account_id: str
    amount_cents: int
    reason: str
    status: str = "posted"  # posted | reverted


# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------
ACCOUNT_OPPORTUNITIES = LinkType(
    name="account_opportunities",
    source="CustomerAccount",
    target="SalesOpportunity",
    cardinality="one-to-many",
    owned=False,
)
ACCOUNT_CASES = LinkType(
    name="account_cases",
    source="CustomerAccount",
    target="SupportCase",
    cardinality="one-to-many",
    owned=False,
)
ACCOUNT_ORDERS = LinkType(
    name="account_orders",
    source="CustomerAccount",
    target="SalesOrder",
    cardinality="one-to-many",
    owned=False,
)
CASE_ORDERS = LinkType(
    name="case_orders",
    source="SupportCase",
    target="SalesOrder",
    cardinality="many-to-one",
    owned=False,
)
CASE_REFUNDS = LinkType(
    name="case_refunds",
    source="SupportCase",
    target="RefundSettlement",
    cardinality="one-to-many",
    owned=True,
)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (
    CustomerAccount,
    SalesOpportunity,
    SupportCase,
    SalesOrder,
    RefundSettlement,
):
    spec.object_type(_cls)
for _link in (
    ACCOUNT_OPPORTUNITIES,
    ACCOUNT_CASES,
    ACCOUNT_ORDERS,
    CASE_ORDERS,
    CASE_REFUNDS,
):
    spec.add_link_type(_link)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class TriageCaseParams(BaseModel):
    case_id: str
    fraud_score: int | None = None
    vip_tier: str | None = None
    ai_resolution_plan: str | None = None


class WinOpportunityParams(BaseModel):
    opportunity_id: str
    order_ref: str | None = None
    fulfillment_note: str | None = None


class OpenCaseParams(BaseModel):
    case_id: str
    account_id: str
    subject: str
    priority: str = "Medium"
    case_type: str = "Problem"
    description: str = ""
    order_id: str | None = None


class ResolveRefundParams(BaseModel):
    case_id: str
    order_id: str
    settlement_id: str | None = None
    reason: str = "customer refund request"
    # Filled by the handler after it reads the order total; the approval rule
    # runs right after the handler in the same simulation.
    needs_approval: bool = False
    refund_cents: int = 0


class PortfolioHealthParams(BaseModel):
    include_closed: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
REFUND_WINDOW_DAYS = 30
HIGH_FRAUD_THRESHOLD = 70
LARGE_REFUND_CENTS = 10_000  # $100


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _refund_needs_approval(
    params: ResolveRefundParams, actor: Actor
) -> bool:
    """Refunds >= $100 must be approved by an ops manager."""
    return actor.role != "ops_manager" and params.needs_approval


# ---------------------------------------------------------------------------
# Read-only functions
# ---------------------------------------------------------------------------
@spec.function(
    "portfolio_health_snapshot",
    PortfolioHealthParams,
    description="跨客户/商机/工单/订单的经营健康快照（管道金额、未结工单、退款额）。",
)
def portfolio_health_snapshot(
    ctx: Any, params: PortfolioHealthParams
) -> dict[str, Any]:
    accounts = ctx.query(CustomerAccount)
    opportunities = ctx.query(SalesOpportunity)
    cases = ctx.query(SupportCase)
    orders = ctx.query(SalesOrder)
    refunds = ctx.query(RefundSettlement)

    open_opportunities = [
        opp
        for opp in opportunities
        if opp.stage not in ("CLOSED_WON", "CLOSED_LOST")
    ]
    open_cases = [
        case for case in cases if case.status not in ("Resolved", "Closed")
    ]
    active_orders = [order for order in orders if order.status != "refunded"]

    return {
        "accounts": len(accounts),
        "opportunities": {
            "total": len(opportunities),
            "open": len(open_opportunities),
            "open_pipeline_cents": sum(
                opp.amount_cents for opp in open_opportunities
            ),
        },
        "cases": {
            "total": len(cases),
            "open": len(open_cases),
            "high_fraud": sum(
                1 for case in cases if case.fraud_score >= HIGH_FRAUD_THRESHOLD
            ),
        },
        "orders": {
            "total": len(orders),
            "active": len(active_orders),
            "active_value_cents": sum(
                order.total_cents for order in active_orders
            ),
        },
        "refunds": {
            "total": len(refunds),
            "posted": sum(1 for row in refunds if row.status == "posted"),
            "posted_amount_cents": sum(
                row.amount_cents for row in refunds if row.status == "posted"
            ),
        },
        "ltv_cents": sum(account.ltv_cents for account in accounts),
    }


# ---------------------------------------------------------------------------
# Governed actions
# ---------------------------------------------------------------------------
@spec.action(
    "ai_triage_crm_case",
    TriageCaseParams,
    allowed_roles=("ai_agent", "support_agent", "ops_manager"),
    affects_source=False,
    description="AI 分诊：把欺诈分、VIP 层级与处置方案写入 Ontology 自有字段。",
)
def ai_triage_crm_case(ctx: Any, params: TriageCaseParams) -> None:
    case = ctx.require(SupportCase, params.case_id)
    updates: dict[str, Any] = {}
    if params.fraud_score is not None:
        if not 0 <= params.fraud_score <= 100:
            raise InvariantError(
                "fraud_score must be within 0..100",
                code="INVALID_FRAUD_SCORE",
            )
        updates["fraud_score"] = params.fraud_score
    if params.vip_tier is not None:
        if params.vip_tier not in ("STANDARD", "SILVER", "GOLD"):
            raise InvariantError(
                f"invalid vip_tier {params.vip_tier!r}",
                code="INVALID_VIP_TIER",
            )
        updates["vip_tier"] = params.vip_tier
    if params.ai_resolution_plan is not None:
        updates["ai_resolution_plan"] = params.ai_resolution_plan
    if not updates:
        raise InvariantError(
            "triage requires at least one field",
            code="EMPTY_TRIAGE",
        )
    ctx.modify(case, **updates)


@spec.action(
    "win_opportunity_and_create_order",
    WinOpportunityParams,
    allowed_roles=("sales_rep", "ai_agent", "ops_manager"),
    affects_source=True,
    description="商机赢单并原生生成销售订单（不再依赖任何外部 CRM/ERP）。",
)
def win_opportunity_and_create_order(
    ctx: Any, params: WinOpportunityParams
) -> None:
    opportunity = ctx.require(SalesOpportunity, params.opportunity_id)
    if opportunity.stage in ("CLOSED_WON", "CLOSED_LOST"):
        raise InvariantError(
            f"opportunity {opportunity.opportunity_id} is already "
            f"{opportunity.stage}",
            code="OPPORTUNITY_ALREADY_CLOSED",
        )
    if opportunity.amount_cents <= 0:
        raise InvariantError(
            "cannot win an opportunity with zero amount",
            code="INVALID_OPPORTUNITY_AMOUNT",
        )

    account = ctx.require(CustomerAccount, opportunity.account_id)
    order_ref = params.order_ref or f"SO-{opportunity.opportunity_id}"

    order = SalesOrder(
        order_id=order_ref,
        order_ref=order_ref,
        account_id=account.account_id,
        status="confirmed",
        total_cents=opportunity.amount_cents,
        currency=opportunity.currency,
        ordered_on=date.today().isoformat(),
        fulfillment_note=params.fulfillment_note,
    )
    ctx.create(order)
    ctx.link(account, ACCOUNT_ORDERS, order)

    ctx.modify(
        opportunity,
        stage="CLOSED_WON",
        win_probability=100,
        ai_next_step="order created natively in ontology",
    )
    ctx.modify(
        account,
        ltv_cents=account.ltv_cents + opportunity.amount_cents,
    )


@spec.action(
    "open_customer_case",
    OpenCaseParams,
    allowed_roles=("support_agent", "sales_rep", "ai_agent", "ops_manager"),
    affects_source=True,
    description="原生创建客服工单，并可挂到某张订单上。",
)
def open_customer_case(ctx: Any, params: OpenCaseParams) -> None:
    account = ctx.require(CustomerAccount, params.account_id)
    if params.priority not in ("Low", "Medium", "High", "Urgent"):
        raise InvariantError(
            f"invalid priority {params.priority!r}",
            code="INVALID_CASE_PRIORITY",
        )
    if ctx.get(SupportCase, params.case_id) is not None:
        raise InvariantError(
            f"case {params.case_id} already exists",
            code="CASE_ALREADY_EXISTS",
        )

    case = SupportCase(
        case_id=params.case_id,
        subject=params.subject,
        account_id=account.account_id,
        status="New",
        priority=params.priority,
        case_type=params.case_type,
        opened_at=datetime.now().replace(microsecond=0).isoformat(),
        description=params.description,
    )
    ctx.create(case)
    ctx.link(account, ACCOUNT_CASES, case)

    if params.order_id is not None:
        order = ctx.require(SalesOrder, params.order_id)
        ctx.link(case, CASE_ORDERS, order)


@spec.action(
    "resolve_case_with_refund",
    ResolveRefundParams,
    allowed_roles=("support_agent", "ai_agent", "ops_manager"),
    approver_roles=("ops_manager",),
    approval_rule=_refund_needs_approval,
    affects_source=True,
    description="工单退款结案：退货期/欺诈分硬护栏 + 大额人工审批。",
)
def resolve_case_with_refund(ctx: Any, params: ResolveRefundParams) -> None:
    case = ctx.require(SupportCase, params.case_id)
    order = ctx.require(SalesOrder, params.order_id)
    account = ctx.require(CustomerAccount, case.account_id)

    # --- hard guard 1: return window
    ordered_on = _parse_date(order.ordered_on)
    if ordered_on is None:
        raise InvariantError(
            f"order {order.order_id} has no valid order date",
            code="ORDER_DATE_UNKNOWN",
        )
    age_days = (date.today() - ordered_on).days
    if age_days > REFUND_WINDOW_DAYS:
        raise InvariantError(
            f"order {order.order_id} is {age_days} days old; the refund "
            f"window is {REFUND_WINDOW_DAYS} days",
            code="RETURN_WINDOW_EXPIRED",
        )

    # --- hard guard 2: fraud risk
    if case.fraud_score >= HIGH_FRAUD_THRESHOLD:
        raise InvariantError(
            f"case {case.case_id} has fraud score {case.fraud_score} "
            f">= {HIGH_FRAUD_THRESHOLD}",
            code="HIGH_FRAUD_RISK_BLOCKED",
        )

    # --- hard guard 3: cannot refund more than once / more than the order
    if order.status == "refunded" or order.refunded_cents >= order.total_cents:
        raise InvariantError(
            f"order {order.order_id} has already been refunded",
            code="ORDER_ALREADY_REFUNDED",
        )
    if case.status in ("Resolved", "Closed"):
        raise InvariantError(
            f"case {case.case_id} is already {case.status}",
            code="CASE_ALREADY_RESOLVED",
        )

    settlement_id = params.settlement_id or f"RFD-{case.case_id}"
    params.refund_cents = order.total_cents
    params.needs_approval = order.total_cents >= LARGE_REFUND_CENTS

    settlement = RefundSettlement(
        settlement_id=settlement_id,
        case_id=case.case_id,
        order_id=order.order_id,
        account_id=account.account_id,
        amount_cents=order.total_cents,
        reason=params.reason,
        status="posted",
    )
    ctx.create(settlement)
    ctx.link(case, CASE_REFUNDS, settlement)

    ctx.modify(order, status="refunded", refunded_cents=order.total_cents)
    ctx.modify(case, status="Resolved")
    ctx.modify(
        account,
        ltv_cents=max(0, account.ltv_cents - order.total_cents),
    )


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------
AI_AGENT = Actor(actor_id="ai-agent-01", role="ai_agent", clearance_tags={"*"})
SUPPORT_AGENT = Actor(
    actor_id="support-agent-01", role="support_agent", clearance_tags={"*"}
)
SALES_REP = Actor(actor_id="sales-rep-01", role="sales_rep", clearance_tags={"*"})
OPS_MANAGER = Actor(
    actor_id="ops-manager-01", role="ops_manager", clearance_tags={"*"}
)


@dataclass
class UnplugDemo:
    runtime: OntologyRuntime


def new_runtime() -> OntologyRuntime:
    return OntologyRuntime(spec)
