"""标准 O2C（Order-to-Cash）纯业务本体模型（PIM）。

只使用业务语言：客户、销售订单、交货单、应收发票、信用额度、运营治理。
不包含任何外部系统表名、传输协议或客户端代码。
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

# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class Customer(ObjectType):
    """客户与信用主数据（混合主权）。"""

    __primary_key__ = "customer_id"
    __title__ = "name"
    __owned__ = {"risk_tier": "NORMAL", "credit_hold_note": None}

    customer_id: str
    name: str
    credit_limit_cents: int
    ar_balance_cents: int = 0
    risk_tier: str = "NORMAL"  # NORMAL | WATCHLIST | BLOCKED
    credit_hold_note: str | None = None


class SalesOrder(ObjectType):
    """销售订单（混合主权）。"""

    __primary_key__ = "order_id"
    __title__ = "order_id"
    __owned__ = {"account_rep": None, "fulfillment_note": None}

    order_id: str
    customer_id: str
    product_summary: str
    quantity: int
    unit_price_cents: int
    total_cents: int
    status: str = "draft"  # draft | confirmed | shipped | invoiced | paid | cancelled
    account_rep: str | None = None
    fulfillment_note: str | None = None


class Delivery(ObjectType):
    """仓库交货单（混合主权）。"""

    __primary_key__ = "delivery_id"
    __title__ = "delivery_id"
    __owned__ = {"carrier_note": None}

    delivery_id: str
    order_id: str
    status: str = "pending"  # pending | shipped
    carrier_note: str | None = None


class Invoice(ObjectType):
    """应收账款发票（source-backed）。"""

    __primary_key__ = "invoice_id"
    __title__ = "invoice_id"
    __owned__ = False

    invoice_id: str
    order_id: str
    customer_id: str
    amount_cents: int
    status: str = "posted"  # posted | paid


# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------
CUSTOMER_SALES_ORDERS = LinkType(
    name="customer_sales_orders",
    source="Customer",
    target="SalesOrder",
    cardinality="one-to-many",
    owned=False,
)
ORDER_DELIVERIES = LinkType(
    name="order_deliveries",
    source="SalesOrder",
    target="Delivery",
    cardinality="one-to-many",
    owned=False,
)
ORDER_INVOICES = LinkType(
    name="order_invoices",
    source="SalesOrder",
    target="Invoice",
    cardinality="one-to-many",
    owned=False,
)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (Customer, SalesOrder, Delivery, Invoice):
    spec.object_type(_cls)
for _link in (CUSTOMER_SALES_ORDERS, ORDER_DELIVERIES, ORDER_INVOICES):
    spec.add_link_type(_link)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class DiagnoseParams(BaseModel):
    include_details: bool = True


class CreditExposureParams(BaseModel):
    order_id: str


class ConfirmOrderParams(BaseModel):
    order_id: str
    # Filled by the action handler after reading customer/order state; the
    # approval rule runs after the handler in the same simulation.
    credit_requires_approval: bool = False


class ShipOrderParams(BaseModel):
    order_id: str


class InvoiceOrderParams(BaseModel):
    order_id: str


class RecordPaymentParams(BaseModel):
    order_id: str
    payment_cents: int


class AnnotateO2CParams(BaseModel):
    customer_id: str | None = None
    risk_tier: str | None = None
    credit_hold_note: str | None = None
    order_id: str | None = None
    account_rep: str | None = None
    fulfillment_note: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _credit_over_limit(customer: Customer, order: SalesOrder) -> int:
    return max(
        0,
        customer.ar_balance_cents
        + order.total_cents
        - customer.credit_limit_cents,
    )


def _confirm_needs_approval(
    params: ConfirmOrderParams, actor: Actor
) -> bool:
    return (
        bool(params.credit_requires_approval)
        and actor.role != "finance_director"
    )


# ---------------------------------------------------------------------------
# Read-only functions
# ---------------------------------------------------------------------------
@spec.function(
    "diagnose_o2c_bottlenecks",
    DiagnoseParams,
    description="O2C 经营体检：信用超限草稿、未发货确认单、漏开票、未收款发票。",
)
def diagnose_o2c_bottlenecks(
    ctx: Any, params: DiagnoseParams
) -> dict[str, Any]:
    credit_exceeded: list[dict[str, Any]] = []
    unshipped_confirmed: list[dict[str, Any]] = []
    unbilled_shipped: list[dict[str, Any]] = []
    unpaid_invoices: list[dict[str, Any]] = []

    orders = sorted(ctx.query(SalesOrder), key=lambda order: order.order_id)

    for order in orders:
        customer = ctx.get(Customer, order.customer_id)
        if customer is None:
            continue

        if order.status == "draft":
            over = _credit_over_limit(customer, order)
            if over > 0:
                credit_exceeded.append(
                    {
                        "order_id": order.order_id,
                        "customer_id": customer.customer_id,
                        "total_cents": order.total_cents,
                        "ar_balance_cents": customer.ar_balance_cents,
                        "credit_limit_cents": customer.credit_limit_cents,
                        "over_limit_cents": over,
                    }
                )

        deliveries = ctx.query_linked(order, ORDER_DELIVERIES, Delivery)
        if order.status == "confirmed":
            pending = [
                delivery
                for delivery in deliveries
                if delivery.status == "pending"
            ]
            if pending:
                unshipped_confirmed.append(
                    {
                        "order_id": order.order_id,
                        "delivery_ids": sorted(
                            delivery.delivery_id for delivery in pending
                        ),
                        "total_cents": order.total_cents,
                    }
                )

        invoices = ctx.query_linked(order, ORDER_INVOICES, Invoice)
        if order.status == "shipped" and not invoices:
            unbilled_shipped.append(
                {
                    "order_id": order.order_id,
                    "total_cents": order.total_cents,
                }
            )

        for invoice in invoices:
            if invoice.status == "posted":
                unpaid_invoices.append(
                    {
                        "invoice_id": invoice.invoice_id,
                        "order_id": invoice.order_id,
                        "customer_id": invoice.customer_id,
                        "amount_cents": invoice.amount_cents,
                    }
                )

    return {
        "credit_exceeded_drafts": credit_exceeded,
        "unshipped_confirmed_orders": unshipped_confirmed,
        "unbilled_shipped_orders": unbilled_shipped,
        "unpaid_invoices": unpaid_invoices,
        "totals": {
            "credit_exceeded_cents": sum(
                row["over_limit_cents"] for row in credit_exceeded
            ),
            "unshipped_confirmed_cents": sum(
                row["total_cents"] for row in unshipped_confirmed
            ),
            "unbilled_shipped_cents": sum(
                row["total_cents"] for row in unbilled_shipped
            ),
            "unpaid_invoice_cents": sum(
                row["amount_cents"] for row in unpaid_invoices
            ),
        },
    }


@spec.function(
    "evaluate_credit_exposure",
    CreditExposureParams,
    description="计算单张订单的信用敞口、超限金额与是否需要财务总监审批。",
)
def evaluate_credit_exposure(
    ctx: Any, params: CreditExposureParams
) -> dict[str, Any]:
    order = ctx.require(SalesOrder, params.order_id)
    customer = ctx.require(Customer, order.customer_id)
    over_limit = _credit_over_limit(customer, order)
    return {
        "order_id": order.order_id,
        "customer_id": customer.customer_id,
        "ar_balance_cents": customer.ar_balance_cents,
        "order_total_cents": order.total_cents,
        "credit_limit_cents": customer.credit_limit_cents,
        "over_limit_cents": over_limit,
        "requires_finance_director": over_limit > 0,
    }


# ---------------------------------------------------------------------------
# Governed actions
# ---------------------------------------------------------------------------
@spec.action(
    "confirm_sales_order",
    ConfirmOrderParams,
    allowed_roles=("sales_rep", "buyer", "ai_agent", "finance_director", "ops_manager"),
    approver_roles=("finance_director",),
    approval_rule=_confirm_needs_approval,
    affects_source=True,
    description="确认草稿销售订单，创建待发货交货单；超信用额度需财务总监审批。",
)
def confirm_sales_order(ctx: Any, params: ConfirmOrderParams) -> None:
    order = ctx.require(SalesOrder, params.order_id)
    if order.status != "draft":
        raise InvariantError(
            f"order {order.order_id} is {order.status}, not draft",
            code="ORDER_NOT_DRAFT",
        )
    customer = ctx.require(Customer, order.customer_id)
    if customer.risk_tier == "BLOCKED":
        raise InvariantError(
            f"customer {customer.customer_id} is credit-blocked",
            code="CUSTOMER_CREDIT_BLOCKED",
        )

    params.credit_requires_approval = (
        _credit_over_limit(customer, order) > 0
    )

    ctx.modify(order, status="confirmed")
    delivery = Delivery(
        delivery_id=f"DEL-{order.order_id}",
        order_id=order.order_id,
        status="pending",
    )
    ctx.create(delivery)
    ctx.link(order, ORDER_DELIVERIES, delivery)


@spec.action(
    "ship_order_delivery",
    ShipOrderParams,
    allowed_roles=("warehouse", "sales_rep", "ai_agent", "ops_manager"),
    affects_source=True,
    description="仓库发货：交货单与销售订单同步转为已发货。",
)
def ship_order_delivery(ctx: Any, params: ShipOrderParams) -> None:
    order = ctx.require(SalesOrder, params.order_id)
    if order.status != "confirmed":
        raise InvariantError(
            f"order {order.order_id} is {order.status}, not confirmed",
            code="ORDER_NOT_CONFIRMED",
        )
    deliveries = ctx.query_linked(order, ORDER_DELIVERIES, Delivery)
    if not deliveries:
        raise InvariantError(
            f"order {order.order_id} has no delivery",
            code="DELIVERY_NOT_FOUND",
        )
    delivery = deliveries[0]
    if delivery.status != "pending":
        raise InvariantError(
            f"delivery {delivery.delivery_id} is already {delivery.status}",
            code="ALREADY_SHIPPED",
        )

    ctx.modify(delivery, status="shipped")
    ctx.modify(order, status="shipped")


@spec.action(
    "issue_and_post_invoice",
    InvoiceOrderParams,
    allowed_roles=("finance", "finance_director", "ai_agent", "ops_manager"),
    affects_source=True,
    description="对已发货订单开具并过账应收发票；未发货严禁开票。",
)
def issue_and_post_invoice(ctx: Any, params: InvoiceOrderParams) -> None:
    order = ctx.require(SalesOrder, params.order_id)
    if order.status != "shipped":
        raise InvariantError(
            "cannot invoice an unshipped order",
            code="CANNOT_INVOICE_UNSHIPPED_ORDER",
        )
    customer = ctx.require(Customer, order.customer_id)

    invoice = Invoice(
        invoice_id=f"INV-{order.order_id}",
        order_id=order.order_id,
        customer_id=customer.customer_id,
        amount_cents=order.total_cents,
        status="posted",
    )
    ctx.create(invoice)
    ctx.link(order, ORDER_INVOICES, invoice)
    ctx.modify(order, status="invoiced")
    ctx.modify(
        customer,
        ar_balance_cents=customer.ar_balance_cents + order.total_cents,
    )


@spec.action(
    "record_customer_payment",
    RecordPaymentParams,
    allowed_roles=("finance", "finance_director", "ai_agent", "ops_manager"),
    affects_source=True,
    description="登记客户收款并核销发票；金额必须与发票精确一致。",
)
def record_customer_payment(ctx: Any, params: RecordPaymentParams) -> None:
    order = ctx.require(SalesOrder, params.order_id)
    invoices = ctx.query_linked(order, ORDER_INVOICES, Invoice)
    if not invoices:
        raise InvariantError(
            f"order {order.order_id} has no invoice",
            code="INVOICE_NOT_FOUND",
        )
    invoice = invoices[0]
    if invoice.status == "paid":
        raise InvariantError(
            f"invoice {invoice.invoice_id} is already paid",
            code="INVOICE_ALREADY_PAID",
        )
    if params.payment_cents != invoice.amount_cents:
        raise InvariantError(
            f"payment {params.payment_cents} does not match invoice "
            f"{invoice.amount_cents}",
            code="PAYMENT_AMOUNT_MISMATCH",
        )

    customer = ctx.require(Customer, invoice.customer_id)
    ctx.modify(invoice, status="paid")
    ctx.modify(order, status="paid")
    ctx.modify(
        customer,
        ar_balance_cents=customer.ar_balance_cents - params.payment_cents,
    )


@spec.action(
    "annotate_o2c_governance",
    AnnotateO2CParams,
    allowed_roles=("sales_rep", "buyer", "ai_agent", "finance_director", "ops_manager"),
    affects_source=False,
    description="只修改客户风险层级/信用备注与订单责任人/履约备注（Ontology 自有）。",
)
def annotate_o2c_governance(ctx: Any, params: AnnotateO2CParams) -> None:
    updated = False

    if params.customer_id is not None:
        customer = ctx.require(Customer, params.customer_id)
        customer_updates: dict[str, Any] = {}
        if params.risk_tier is not None:
            if params.risk_tier not in ("NORMAL", "WATCHLIST", "BLOCKED"):
                raise InvariantError(
                    f"invalid risk tier {params.risk_tier!r}",
                    code="INVALID_RISK_TIER",
                )
            customer_updates["risk_tier"] = params.risk_tier
        if params.credit_hold_note is not None:
            customer_updates["credit_hold_note"] = params.credit_hold_note
        if customer_updates:
            ctx.modify(customer, **customer_updates)
            updated = True

    if params.order_id is not None:
        order = ctx.require(SalesOrder, params.order_id)
        order_updates: dict[str, Any] = {}
        if params.account_rep is not None:
            order_updates["account_rep"] = params.account_rep
        if params.fulfillment_note is not None:
            order_updates["fulfillment_note"] = params.fulfillment_note
        if order_updates:
            ctx.modify(order, **order_updates)
            updated = True

    if not updated:
        raise InvariantError(
            "no governance target or field supplied",
            code="NO_GOVERNANCE_TARGET",
        )


# ---------------------------------------------------------------------------
# Pure business seed (used by pure unit tests; the external adapter replaces it)
# ---------------------------------------------------------------------------
SALES_REP = Actor(
    actor_id="sales-rep-01",
    role="sales_rep",
    clearance_tags={"*"},
)
AI_AGENT = Actor(
    actor_id="ai-agent-01",
    role="ai_agent",
    clearance_tags={"*"},
)
FINANCE_DIRECTOR = Actor(
    actor_id="finance-director-01",
    role="finance_director",
    clearance_tags={"*"},
)
WAREHOUSE = Actor(
    actor_id="warehouse-01",
    role="warehouse",
    clearance_tags={"*"},
)


@dataclass
class O2CDemo:
    runtime: OntologyRuntime
    customer_id: str = "O2C-CUST-001"
    small_order_id: str = "O2C-SO-001"
    large_order_id: str = "O2C-SO-002"


def build_o2c_demo() -> O2CDemo:
    runtime = OntologyRuntime(spec)
    customer = Customer(
        customer_id="O2C-CUST-001",
        name="O2C Demo Customer",
        credit_limit_cents=10_000_000,
        ar_balance_cents=0,
    )
    runtime.put_object(customer)
    for order in (
        SalesOrder(
            order_id="O2C-SO-001",
            customer_id=customer.customer_id,
            product_summary="O2C Widget",
            quantity=2,
            unit_price_cents=1_000_000,
            total_cents=2_000_000,
            status="draft",
        ),
        SalesOrder(
            order_id="O2C-SO-002",
            customer_id=customer.customer_id,
            product_summary="O2C Widget",
            quantity=12,
            unit_price_cents=1_000_000,
            total_cents=12_000_000,
            status="draft",
        ),
    ):
        runtime.put_object(order)
        runtime.link_objects(customer, CUSTOMER_SALES_ORDERS, order)
    return O2CDemo(runtime=runtime)
