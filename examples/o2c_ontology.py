"""标准 O2C（Order-to-Cash）纯业务本体模型（PIM · M2 OWL 规范）。

只使用业务语言：客户、销售订单、交货单、应收发票、信用额度、运营治理。
遵循 M2 元模型契约：
* 声明式 ``RoleDef`` 角色词表（``spec.role``）与调用方 ``Actor`` 分离；
* OWL 字段级 ``ObjectProperty[Target]`` 自动同步三元组与 ``TARGET_NOT_FOUND`` 校验；
* ``AutoID`` 系统主键推导与 ``__aggregate_root__`` 聚合根标记；
* 列级脱敏 ``__masked_fields__`` 与后置全局不变量 ``@spec.invariant``。
不包含任何外部系统表名、传输协议或客户端代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from open_ontology import (
    Actor,
    AutoID,
    InvariantError,
    ObjectNotFoundError,
    ObjectProperty,
    OntologyRuntime,
    OntologySpec,
)
from pydantic import BaseModel

try:
    from examples.business_ontology import (
        BalanceLedger,
        EventVoucher,
        MasterEntity,
        ProcessDocument,
        spec as business_spec,
    )
except ImportError:
    from pathlib import Path
    import sys

    _REPO_ROOT = str(Path(__file__).resolve().parents[1])
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from examples.business_ontology import (
        BalanceLedger,
        EventVoucher,
        MasterEntity,
        ProcessDocument,
        spec as business_spec,
    )

RiskTier = Literal["NORMAL", "WATCHLIST", "BLOCKED"]
OrderStatus = Literal[
    "draft", "confirmed", "shipped", "invoiced", "paid", "cancelled"
]
DeliveryStatus = Literal["pending", "shipped"]
InvoiceStatus = Literal["posted", "paid"]
VoucherKind = Literal["invoice_post", "payment_settlement"]


# ---------------------------------------------------------------------------
# Spec & Role vocabulary (extends abstract business_ontology M1 spec)
# ---------------------------------------------------------------------------
spec = OntologySpec(extends=(business_spec,))

for _role_name, _role_desc in (
    ("sales_rep", "销售代表：负责草稿订单确认与客户履约跟进"),
    ("buyer", "采购员：负责代客下单确认与采购协同"),
    ("warehouse", "库管员：负责拣货校验与物理出库确认"),
    ("finance", "财务人员：负责已出库订单开票过账与收款核销"),
    ("finance_director", "财务总监：负责超授信大单签批、风控冻结与冲正撤回"),
    ("ops_manager", "运营主管：负责跨部门履约协调与风控备注维护"),
    ("ai_agent", "智能助手：在治理护栏内执行订单、出库、开票与收款预演及提交"),
    ("system", "系统角色：供内部只读诊断与不变量视图使用"),
):
    spec.role(_role_name, _role_desc)


# ---------------------------------------------------------------------------
# Object types (all 4 M1 business archetypes + OWL ObjectProperty + AutoID)
# ---------------------------------------------------------------------------
class Customer(MasterEntity):
    """客户与信用主数据（继承 MasterEntity · 混合主权 + 风控备注列级脱敏）。"""

    __primary_key__ = "customer_id"
    __title__ = "name"
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("credit_limit_cents",)
    __owned__ = {"risk_tier": "NORMAL", "credit_hold_note": None}
    __masked_fields__ = {
        "credit_hold_note": (
            "finance_director",
            "finance",
            "ops_manager",
            "sales_rep",
            "system",
        )
    }
    __mask_defaults__ = {"credit_hold_note": "***"}

    customer_id: str
    name: str
    credit_limit_cents: int
    risk_tier: RiskTier = "NORMAL"
    credit_hold_note: str | None = None


class ReceivableBalance(BalanceLedger):
    """客户应收余额存量台账（继承 BalanceLedger · 热行 OCC 解耦 + 账证守恒）。"""

    __primary_key__ = "ledger_id"
    __title__ = "ledger_id"
    __owned__ = False
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("ar_balance_cents",)
    __voucher_delta_rules__: ClassVar[dict[str, tuple[str, str, str]]] = {
        "ar_balance_cents": ("ReceivableVoucher", "ledger", "delta_cents"),
    }

    ledger_id: AutoID
    customer: ObjectProperty[Customer]
    ar_balance_cents: int = 0


class SalesOrder(ProcessDocument):
    """销售订单（继承 ProcessDocument · 混合主权）。"""

    __primary_key__ = "order_id"
    __title__ = "order_id"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {
        "draft": ("confirmed", "cancelled"),
        "confirmed": ("shipped", "cancelled"),
        "shipped": ("invoiced",),
        "invoiced": ("paid",),
    }
    __owned__ = {"account_rep": None, "fulfillment_note": None}

    order_id: str
    customer: ObjectProperty[Customer]
    product_summary: str
    quantity: int
    unit_price_cents: int
    total_cents: int
    status: OrderStatus = "draft"
    account_rep: str | None = None
    fulfillment_note: str | None = None


class Delivery(ProcessDocument):
    """仓库交货单（继承 ProcessDocument · AutoID 主键）。"""

    __primary_key__ = "delivery_id"
    __title__ = "delivery_id"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("pending",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {
        "pending": ("shipped",),
    }
    __owned__ = {"carrier_note": None}

    delivery_id: AutoID
    order: ObjectProperty[SalesOrder]
    status: DeliveryStatus = "pending"
    carrier_note: str | None = None


class Invoice(ProcessDocument):
    """应收账款发票（继承 ProcessDocument · 外部源权威 · AutoID 主键）。"""

    __primary_key__ = "invoice_id"
    __title__ = "invoice_id"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("posted",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {
        "posted": ("paid",),
    }
    __owned__ = False

    invoice_id: AutoID
    order: ObjectProperty[SalesOrder]
    customer: ObjectProperty[Customer]
    amount_cents: int
    status: InvoiceStatus = "posted"


class ReceivableVoucher(EventVoucher):
    """应收账务凭证流水（继承 EventVoucher · 只追加不可篡改）。"""

    __primary_key__ = "voucher_id"
    __title__ = "voucher_id"
    __owned__ = False

    voucher_id: AutoID
    ledger: ObjectProperty[ReceivableBalance]
    customer: ObjectProperty[Customer]
    order: ObjectProperty[SalesOrder]
    entry_kind: VoucherKind
    delta_cents: int


for _cls in (
    Customer,
    ReceivableBalance,
    SalesOrder,
    Delivery,
    Invoice,
    ReceivableVoucher,
):
    spec.object_type(_cls)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class DiagnoseParams(BaseModel):
    include_details: bool = True


class CreditExposureParams(BaseModel):
    order: ObjectProperty[SalesOrder]


class CreateOrderParams(BaseModel):
    order_id: str
    customer: ObjectProperty[Customer] = "O2C-CUST-001"
    product_summary: str = "O2C Widget"
    quantity: int
    unit_price_cents: int


class ConfirmOrderParams(BaseModel):
    order: ObjectProperty[SalesOrder]
    # Filled by the action handler after reading customer/order state; the
    # approval rule runs after the handler in the same simulation.
    credit_requires_approval: bool = False


class ShipOrderParams(BaseModel):
    order: ObjectProperty[SalesOrder]


class InvoiceOrderParams(BaseModel):
    order: ObjectProperty[SalesOrder]


class RecordPaymentParams(BaseModel):
    order: ObjectProperty[SalesOrder]
    payment_cents: int


class AnnotateO2CParams(BaseModel):
    customer: ObjectProperty[Customer] | None = None
    risk_tier: RiskTier | None = None
    credit_hold_note: str | None = None
    order: ObjectProperty[SalesOrder] | None = None
    account_rep: str | None = None
    fulfillment_note: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ar_ledger_id(customer_id: str) -> str:
    return f"AR-{customer_id}"


def get_customer_ar_balance(
    source: Any, customer_id: str, actor: Actor | None = None
) -> int:
    ledger_key = ar_ledger_id(customer_id)
    if hasattr(source, "get"):
        ledger = source.get(ReceivableBalance, ledger_key)
        return ledger.ar_balance_cents if ledger is not None else 0
    try:
        ledger = source.get_object(
            ReceivableBalance,
            ledger_key,
            actor or Actor(actor_id="__ar__", role="system", clearance_tags={"*"}),
        )
        return ledger.ar_balance_cents
    except ObjectNotFoundError:
        return 0


def _credit_over_limit(
    customer: Customer, ar_balance_cents: int, order: SalesOrder
) -> int:
    return max(
        0,
        ar_balance_cents
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


def _find_order_delivery(ctx: Any, order_id: str) -> Delivery | None:
    direct = ctx.get(Delivery, f"DEL-{order_id}")
    if direct is not None and direct.order == order_id:
        return direct
    matches = [d for d in ctx.query(Delivery) if d.order == order_id]
    return matches[0] if matches else None


def _find_order_invoice(ctx: Any, order_id: str) -> Invoice | None:
    direct = ctx.get(Invoice, f"INV-{order_id}")
    if direct is not None and direct.order == order_id:
        return direct
    matches = [inv for inv in ctx.query(Invoice) if inv.order == order_id]
    return matches[0] if matches else None


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
    deliveries_by_order: dict[str, list[Delivery]] = {}
    for delivery in ctx.query(Delivery):
        deliveries_by_order.setdefault(delivery.order, []).append(delivery)
    invoices_by_order: dict[str, list[Invoice]] = {}
    for invoice in ctx.query(Invoice):
        invoices_by_order.setdefault(invoice.order, []).append(invoice)

    for order in orders:
        customer = ctx.get(Customer, order.customer)
        if customer is None:
            continue
        ar_balance_cents = get_customer_ar_balance(ctx, customer.customer_id)

        if order.status == "draft":
            over = _credit_over_limit(customer, ar_balance_cents, order)
            if over > 0:
                credit_exceeded.append(
                    {
                        "order_id": order.order_id,
                        "customer_id": customer.customer_id,
                        "total_cents": order.total_cents,
                        "ar_balance_cents": ar_balance_cents,
                        "credit_limit_cents": customer.credit_limit_cents,
                        "over_limit_cents": over,
                    }
                )

        deliveries = deliveries_by_order.get(order.order_id, [])
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

        invoices = invoices_by_order.get(order.order_id, [])
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
                        "order_id": invoice.order,
                        "customer_id": invoice.customer,
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
    order = ctx.require(SalesOrder, params.order)
    customer = ctx.require(Customer, order.customer)
    ar_balance_cents = get_customer_ar_balance(ctx, customer.customer_id)
    over_limit = _credit_over_limit(customer, ar_balance_cents, order)
    return {
        "order_id": order.order_id,
        "customer_id": customer.customer_id,
        "ar_balance_cents": ar_balance_cents,
        "order_total_cents": order.total_cents,
        "credit_limit_cents": customer.credit_limit_cents,
        "over_limit_cents": over_limit,
        "requires_finance_director": over_limit > 0,
    }


# ---------------------------------------------------------------------------
# Governed actions (OWL ObjectProperty auto-links triples)
# ---------------------------------------------------------------------------
@spec.action(
    "create_sales_order",
    CreateOrderParams,
    allowed_roles=("sales_rep", "buyer", "ai_agent", "finance_director", "ops_manager"),
    affects_source=True,
    description="为客户新建草稿销售订单（报价单）；信用冻结客户严禁开单。",
)
def create_sales_order(ctx: Any, params: CreateOrderParams) -> None:
    order_id = params.order_id.strip()
    if not order_id:
        raise InvariantError("order_id must not be empty", code="INVALID_ORDER_ID")
    if ctx.get(SalesOrder, order_id) is not None:
        raise InvariantError(
            f"order {order_id} already exists",
            code="ORDER_ALREADY_EXISTS",
        )
    customer = ctx.require(Customer, params.customer)
    if customer.risk_tier == "BLOCKED":
        raise InvariantError(
            f"customer {customer.customer_id} is credit-blocked",
            code="CUSTOMER_CREDIT_BLOCKED",
        )
    if params.quantity <= 0:
        raise InvariantError(
            "order quantity must be positive",
            code="INVALID_ORDER_QUANTITY",
        )
    if params.unit_price_cents <= 0:
        raise InvariantError(
            "unit_price_cents must be positive",
            code="INVALID_UNIT_PRICE",
        )
    product_summary = params.product_summary.strip() or "O2C Widget"
    ctx.create(
        SalesOrder(
            order_id=order_id,
            customer=customer.customer_id,
            product_summary=product_summary,
            quantity=params.quantity,
            unit_price_cents=params.unit_price_cents,
            total_cents=params.quantity * params.unit_price_cents,
            status="draft",
        )
    )


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
    order = ctx.require(SalesOrder, params.order)
    if order.status != "draft":
        raise InvariantError(
            f"order {order.order_id} is {order.status}, not draft",
            code="ORDER_NOT_DRAFT",
        )
    customer = ctx.require(Customer, order.customer)
    if customer.risk_tier == "BLOCKED":
        raise InvariantError(
            f"customer {customer.customer_id} is credit-blocked",
            code="CUSTOMER_CREDIT_BLOCKED",
        )

    ar_balance_cents = get_customer_ar_balance(ctx, customer.customer_id)
    params.credit_requires_approval = (
        _credit_over_limit(customer, ar_balance_cents, order) > 0
    )

    ctx.modify(order, status="confirmed")
    ctx.create(
        Delivery(
            delivery_id=f"DEL-{order.order_id}",
            order=order.order_id,
            status="pending",
        )
    )


@spec.action(
    "ship_order_delivery",
    ShipOrderParams,
    allowed_roles=("warehouse", "sales_rep", "ai_agent", "ops_manager"),
    affects_source=True,
    description="仓库发货：交货单与销售订单同步转为已发货。",
)
def ship_order_delivery(ctx: Any, params: ShipOrderParams) -> None:
    order = ctx.require(SalesOrder, params.order)
    if order.status != "confirmed":
        raise InvariantError(
            f"order {order.order_id} is {order.status}, not confirmed",
            code="ORDER_NOT_CONFIRMED",
        )
    delivery = _find_order_delivery(ctx, order.order_id)
    if delivery is None:
        raise InvariantError(
            f"order {order.order_id} has no delivery",
            code="DELIVERY_NOT_FOUND",
        )
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
    order = ctx.require(SalesOrder, params.order)
    if order.status != "shipped":
        raise InvariantError(
            "cannot invoice an unshipped order",
            code="CANNOT_INVOICE_UNSHIPPED_ORDER",
        )
    ledger = ctx.require(ReceivableBalance, ar_ledger_id(order.customer))

    ctx.create(
        Invoice(
            invoice_id=f"INV-{order.order_id}",
            order=order.order_id,
            customer=order.customer,
            amount_cents=order.total_cents,
            status="posted",
        )
    )
    ctx.modify(order, status="invoiced")
    ctx.modify(
        ledger,
        ar_balance_cents=ledger.ar_balance_cents + order.total_cents,
    )
    ctx.create(
        ReceivableVoucher(
            voucher_id=f"ARV-INV-{order.order_id}",
            ledger=ledger.ledger_id,
            customer=order.customer,
            order=order.order_id,
            entry_kind="invoice_post",
            delta_cents=order.total_cents,
        )
    )


@spec.action(
    "record_customer_payment",
    RecordPaymentParams,
    allowed_roles=("finance", "finance_director", "ai_agent", "ops_manager"),
    affects_source=True,
    description="登记客户收款并核销发票；金额必须与发票精确一致。",
)
def record_customer_payment(ctx: Any, params: RecordPaymentParams) -> None:
    order = ctx.require(SalesOrder, params.order)
    invoice = _find_order_invoice(ctx, order.order_id)
    if invoice is None:
        raise InvariantError(
            f"order {order.order_id} has no invoice",
            code="INVOICE_NOT_FOUND",
        )
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

    ledger = ctx.require(ReceivableBalance, ar_ledger_id(invoice.customer))
    ctx.modify(invoice, status="paid")
    ctx.modify(order, status="paid")
    ctx.modify(
        ledger,
        ar_balance_cents=ledger.ar_balance_cents - params.payment_cents,
    )
    ctx.create(
        ReceivableVoucher(
            voucher_id=f"ARV-PAY-{order.order_id}",
            ledger=ledger.ledger_id,
            customer=invoice.customer,
            order=order.order_id,
            entry_kind="payment_settlement",
            delta_cents=-params.payment_cents,
        )
    )


@spec.action(
    "annotate_o2c_governance",
    AnnotateO2CParams,
    allowed_roles=("sales_rep", "buyer", "ai_agent", "finance_director", "ops_manager"),
    affects_source=False,
    description="只修改客户风险层级/信用备注与订单责任人/履约备注（领域自有治理）。",
)
def annotate_o2c_governance(ctx: Any, params: AnnotateO2CParams) -> None:
    updated = False

    if params.customer is not None:
        customer = ctx.require(Customer, params.customer)
        customer_updates: dict[str, Any] = {}
        if params.risk_tier is not None:
            customer_updates["risk_tier"] = params.risk_tier
        if params.credit_hold_note is not None:
            customer_updates["credit_hold_note"] = params.credit_hold_note
        if customer_updates:
            ctx.modify(customer, **customer_updates)
            updated = True

    if params.order is not None:
        order = ctx.require(SalesOrder, params.order)
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
# Declarative post-state invariants (enforced across every action)
# ---------------------------------------------------------------------------
@spec.invariant(
    "invoice_requires_shipped_order",
    code="CANNOT_INVOICE_UNSHIPPED_ORDER",
    description="任何应收账款发票关联的销售订单必须已完成出库（已发货、已开票或已收款）。",
)
def invoice_requires_shipped_order(view: Any) -> bool:
    for inv in view.query(Invoice):
        order = view.get(SalesOrder, inv.order)
        if order is None or order.status not in ("shipped", "invoiced", "paid"):
            return False
    return True


@spec.invariant(
    "delivery_requires_confirmed_order",
    code="DELIVERY_WITHOUT_CONFIRMED_ORDER",
    description="任何仓库交货单关联的销售订单不得处于草稿或已取消状态。",
)
def delivery_requires_confirmed_order(view: Any) -> bool:
    for delivery in view.query(Delivery):
        order = view.get(SalesOrder, delivery.order)
        if order is None or order.status in ("draft", "cancelled"):
            return False
    return True


@spec.invariant(
    "customer_ar_non_negative",
    code="NEGATIVE_AR_BALANCE",
    description="客户授信总额与应收余额台账均不得为负数。",
)
def customer_ar_non_negative(view: Any) -> bool:
    for customer in view.query(Customer):
        if customer.credit_limit_cents < 0:
            return False
    for ledger in view.query(ReceivableBalance):
        if ledger.ar_balance_cents < 0:
            return False
    return True


# ---------------------------------------------------------------------------
# Display dictionary (pure business Chinese; zero kernel leakage)
# ---------------------------------------------------------------------------
spec.update_display(
    {
        "ontologies": {
            "o2c_order_to_cash": "订单到收款业财治理",
        },
        "categories": {
            "Order-to-Cash": "业财风控",
        },
        "objects": {
            "Customer": "客户授信档案",
            "ReceivableBalance": "客户应收余额台账",
            "SalesOrder": "销售订单",
            "Delivery": "仓库交货单",
            "Invoice": "应收账款发票",
            "ReceivableVoucher": "应收账务凭证",
        },
        "object_properties": {
            "ReceivableBalance.customer": "台账所属客户",
            "SalesOrder.customer": "订单所属客户",
            "Delivery.order": "交货关联订单",
            "Invoice.order": "发票关联订单",
            "Invoice.customer": "发票开票客户",
            "ReceivableVoucher.ledger": "记账关联台账",
            "ReceivableVoucher.customer": "凭证所属客户",
            "ReceivableVoucher.order": "凭证关联订单",
        },
        "actions": {
            "create_sales_order": "新建销售订单",
            "confirm_sales_order": "确认销售订单",
            "ship_order_delivery": "仓库确认出库",
            "issue_and_post_invoice": "开具并过账发票",
            "record_customer_payment": "登记收款核销",
            "annotate_o2c_governance": "登记风控特批备注",
        },
        "functions": {
            "diagnose_o2c_bottlenecks": "订单到收款经营体检",
            "evaluate_credit_exposure": "评估订单授信敞口",
        },
        "fields": {
            "customer_id": "客户编号",
            "name": "客户名称",
            "order_id": "订单编号",
            "status": "单据状态",
            "amount_cents": "发票金额（分）",
            "ledger_id": "应收台账编号",
            "voucher_id": "账务凭证号",
            "ledger": "关联应收台账",
            "entry_kind": "凭证记账类型",
            "delta_cents": "应收变动金额（分）",
            "customer": "所属客户",
            "order": "关联销售订单",
            "credit_limit_cents": "授信总额（分）",
            "ar_balance_cents": "当前应收占用（分）",
            "risk_tier": "客户风险等级",
            "credit_hold_note": "授信风控备注",
            "product_summary": "商品摘要",
            "quantity": "订购数量",
            "unit_price_cents": "商品单价（分）",
            "total_cents": "订单总金额（分）",
            "account_rep": "客户负责人",
            "fulfillment_note": "履约特批备注",
            "delivery_id": "交货单号",
            "carrier_note": "物流承运备注",
            "invoice_id": "应收发票号",
            "payment_cents": "收款金额（分）",
            "credit_requires_approval": "触发超授信审批",
            "include_details": "包含明细列表",
        },
        "statuses": {
            "draft": "草稿",
            "confirmed": "已确认",
            "shipped": "已发货",
            "invoiced": "已开票",
            "paid": "已收款",
            "cancelled": "已取消",
            "pending": "待发货",
            "posted": "已过账",
            "invoice_post": "开票记账增加",
            "payment_settlement": "收款核销冲减",
            "NORMAL": "正常",
            "WATCHLIST": "重点关注",
            "BLOCKED": "信用冻结",
        },
        "roles": {
            "sales_rep": "销售代表",
            "buyer": "采购员",
            "warehouse": "库管员",
            "finance": "财务人员",
            "finance_director": "财务总监",
            "ops_manager": "运营主管",
            "ai_agent": "智能销售助理",
            "system": "系统审计",
        },
    }
)


# ---------------------------------------------------------------------------
# Convenience demo builder & actor presets (M0 tests / demo helpers)
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
    )
    runtime.put_object(customer)
    runtime.put_object(
        ReceivableBalance(
            ledger_id=ar_ledger_id(customer.customer_id),
            customer=customer.customer_id,
            ar_balance_cents=0,
        )
    )
    for order in (
        SalesOrder(
            order_id="O2C-SO-001",
            customer=customer.customer_id,
            product_summary="O2C Widget",
            quantity=2,
            unit_price_cents=1_000_000,
            total_cents=2_000_000,
            status="draft",
        ),
        SalesOrder(
            order_id="O2C-SO-002",
            customer=customer.customer_id,
            product_summary="O2C Widget",
            quantity=12,
            unit_price_cents=1_000_000,
            total_cents=12_000_000,
            status="draft",
        ),
    ):
        runtime.put_object(order)
    return O2CDemo(runtime=runtime)
