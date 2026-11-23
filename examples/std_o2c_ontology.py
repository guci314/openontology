"""标准 O2C（Order-to-Cash）纯业务领域本体（Std O2C Ontology · Pure M1）。

基于 ``std_core_ontology`` 最小核心扩展从订单到回款的标准骨干（只保留领域核心不变量，
不硬编码具体企业的个性化授信阈值或大单特批门槛）：
* 实体：``O2cCustomerProfile``、``O2cOrder``、``O2cDelivery``、``O2cInvoice``；
* 动作：``confirm_o2c_order``、``complete_o2c_delivery``、``post_o2c_invoice``、``record_o2c_payment``。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from open_ontology.induction.dsl import DeclarativeBlueprint, export_spec_blueprint
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )
except ImportError:
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )

__all__ = [
    "spec",
    "AccountingEntity",
    "BusinessParty",
    "WorkCard",
    "O2cCustomerProfile",
    "O2cOrder",
    "O2cDelivery",
    "O2cInvoice",
    "ConfirmO2cOrderParams",
    "CompleteO2cDeliveryParams",
    "PostO2cInvoiceParams",
    "RecordO2cPaymentParams",
    "build_std_o2c_blueprint",
]

_O2C_ORDER_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("confirmed", "cancelled"),
    "confirmed": ("delivered", "invoiced", "cancelled"),
    "delivered": ("invoiced",),
    "invoiced": (),
    "cancelled": (),
}

_O2C_DELIVERY_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}

_O2C_INVOICE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("posted", "cancelled"),
    "posted": (),
    "cancelled": (),
}

spec = OntologySpec(extends=(std_core_spec,))


@spec.object_type
class O2cCustomerProfile(MasterEntity):
    """标准 O2C 客户信用与账期档案（连接 ``BusinessParty`` 与 ``AccountingEntity``）。"""

    __primary_key__: ClassVar[str] = "profile_id"
    __title__: ClassVar[str] = "profile_id"
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("credit_limit",)

    profile_id: str
    party: ObjectProperty[BusinessParty]
    accounting_entity: ObjectProperty[AccountingEntity]
    credit_limit: float = Field(default=0.0, ge=0.0)
    payment_terms: str = "NET30"
    status: str = "active"


@spec.object_type
class O2cOrder(ProcessDocument):
    """标准 O2C 销售订单单据。"""

    __primary_key__: ClassVar[str] = "order_id"
    __title__: ClassVar[str] = "order_number"
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _O2C_ORDER_TRANSITIONS
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("amount_total",)

    order_id: str
    order_number: str
    customer: ObjectProperty[O2cCustomerProfile]
    accounting_entity: ObjectProperty[AccountingEntity]
    amount_total: float = Field(default=0.0, ge=0.0)
    status: str = "draft"


@spec.object_type
class O2cDelivery(ProcessDocument):
    """标准 O2C 发货单据。"""

    __primary_key__: ClassVar[str] = "delivery_id"
    __title__: ClassVar[str] = "delivery_id"
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("pending",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _O2C_DELIVERY_TRANSITIONS

    delivery_id: str
    order: ObjectProperty[O2cOrder]
    status: str = "pending"


@spec.object_type
class O2cInvoice(ProcessDocument):
    """标准 O2C 应收发票单据（维护未清应收余额 ``amount_residual`` 与核销状态）。"""

    __primary_key__: ClassVar[str] = "invoice_id"
    __title__: ClassVar[str] = "invoice_id"
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _O2C_INVOICE_TRANSITIONS
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("amount_total", "amount_residual")

    invoice_id: str
    order: ObjectProperty[O2cOrder]
    amount_total: float = Field(default=0.0, ge=0.0)
    amount_residual: float = Field(default=0.0, ge=0.0)
    payment_state: str = "not_paid"
    status: str = "draft"


class ConfirmO2cOrderParams(BaseModel):
    order_id: str


class CompleteO2cDeliveryParams(BaseModel):
    delivery_id: str


class PostO2cInvoiceParams(BaseModel):
    invoice_id: str


class RecordO2cPaymentParams(BaseModel):
    invoice_id: str
    payment_amount: float = Field(..., ge=0.0)


@spec.edit_function(
    "confirm_o2c_order",
    ConfirmO2cOrderParams,
    description="校验草稿状态与正金额后确认标准销售订单。",
    touched_object_types=("O2cOrder",),
)
def confirm_o2c_order(ctx: Any, params: ConfirmO2cOrderParams) -> None:
    order = ctx.require(O2cOrder, params.order_id)
    if order.status != "draft":
        raise InvariantError(
            f"O2cOrder:{order.order_id} status {order.status!r} must be 'draft' to confirm",
            code="ORDER_NOT_DRAFT",
        )
    if order.amount_total <= 0:
        raise InvariantError(
            f"O2cOrder:{order.order_id} amount_total={order.amount_total} must be > 0",
            code="NON_POSITIVE_ORDER_AMOUNT",
        )
    ctx.modify(order, status="confirmed")

spec.declare_action(
    "confirm_o2c_order",
    edit_function="confirm_o2c_order",
    allowed_roles=("sales_rep", "sales_manager", "ops", "finance_director", "steward", "ai_agent", "admin"),
    description="校验草稿状态与正金额后确认标准销售订单。",
)


@spec.edit_function(
    "complete_o2c_delivery",
    CompleteO2cDeliveryParams,
    description="完成待发货履约单并同步推进关联销售订单为已发货（delivered）。",
    touched_object_types=("O2cDelivery", "O2cOrder"),
)
def complete_o2c_delivery(ctx: Any, params: CompleteO2cDeliveryParams) -> None:
    delivery = ctx.require(O2cDelivery, params.delivery_id)
    if delivery.status != "pending":
        raise InvariantError(
            f"O2cDelivery:{delivery.delivery_id} status {delivery.status!r} must be 'pending'",
            code="DELIVERY_NOT_PENDING",
        )
    order = ctx.require(O2cOrder, delivery.order)
    if order.status != "confirmed":
        raise InvariantError(
            f"O2cOrder:{order.order_id} status {order.status!r} must be 'confirmed' before delivery",
            code="ORDER_NOT_CONFIRMED",
        )
    ctx.modify(delivery, status="completed")
    ctx.modify(order, status="delivered")

spec.declare_action(
    "complete_o2c_delivery",
    edit_function="complete_o2c_delivery",
    allowed_roles=("warehouse", "ops", "finance_director", "steward", "ai_agent", "admin"),
    description="完成待发货履约单并同步推进关联销售订单为已发货（delivered）。",
)


@spec.edit_function(
    "post_o2c_invoice",
    PostO2cInvoiceParams,
    description="将草稿应收发票正式过账并将关联订单推进为已开票（invoiced）。",
    touched_object_types=("O2cInvoice", "O2cOrder"),
)
def post_o2c_invoice(ctx: Any, params: PostO2cInvoiceParams) -> None:
    invoice = ctx.require(O2cInvoice, params.invoice_id)
    if invoice.status != "draft":
        raise InvariantError(
            f"O2cInvoice:{invoice.invoice_id} status {invoice.status!r} must be 'draft' to post",
            code="INVOICE_NOT_DRAFT",
        )
    order = ctx.require(O2cOrder, invoice.order)
    if order.status not in ("confirmed", "delivered", "invoiced"):
        raise InvariantError(
            f"O2cOrder:{order.order_id} status {order.status!r} is not billable",
            code="ORDER_NOT_BILLABLE",
        )
    ctx.modify(invoice, status="posted")
    ctx.modify(order, status="invoiced")

spec.declare_action(
    "post_o2c_invoice",
    edit_function="post_o2c_invoice",
    allowed_roles=("finance", "finance_director", "steward", "ai_agent", "admin"),
    description="将草稿应收发票正式过账并将关联订单推进为已开票（invoiced）。",
)


@spec.edit_function(
    "record_o2c_payment",
    RecordO2cPaymentParams,
    description="登记已过账发票的回款核销（支持分期部分核销与全额结清）。",
    touched_object_types=("O2cInvoice",),
)
def record_o2c_payment(ctx: Any, params: RecordO2cPaymentParams) -> None:
    invoice = ctx.require(O2cInvoice, params.invoice_id)
    if invoice.status != "posted":
        raise InvariantError(
            f"O2cInvoice:{invoice.invoice_id} status {invoice.status!r} must be 'posted'",
            code="INVOICE_NOT_POSTED",
        )
    if params.payment_amount <= 0:
        raise InvariantError(
            f"payment_amount={params.payment_amount} must be > 0",
            code="INVALID_PAYMENT_AMOUNT",
        )
    if params.payment_amount > invoice.amount_residual:
        raise InvariantError(
            f"payment_amount={params.payment_amount} exceeds amount_residual={invoice.amount_residual}",
            code="PAYMENT_EXCEEDS_RESIDUAL",
        )
    new_residual = max(0.0, round(invoice.amount_residual - params.payment_amount, 6))
    new_payment_state = "paid" if new_residual <= 1e-9 else "partial"
    ctx.modify(invoice, amount_residual=new_residual, payment_state=new_payment_state)

spec.declare_action(
    "record_o2c_payment",
    edit_function="record_o2c_payment",
    allowed_roles=("finance", "finance_director", "steward", "ai_agent", "admin"),
    description="登记已过账发票的回款核销（支持分期部分核销与全额结清）。",
)


spec.update_display(
    {
        "ontologies": {
            "std_o2c_ontology": "标准 O2C 领域本体（客户授信·销售订单·发货·应收发票）",
        },
        "objects": {
            "O2cCustomerProfile": "O2C客户信用档案",
            "O2cOrder": "O2C销售订单",
            "O2cDelivery": "O2C发货单",
            "O2cInvoice": "O2C应收发票",
        },
        "actions": {
            "confirm_o2c_order": "确认销售订单",
            "complete_o2c_delivery": "完成发货确认",
            "post_o2c_invoice": "应收发票过账",
            "record_o2c_payment": "登记收款核销",
        },
        "fields": {
            "profile_id": "主档ID",
            "credit_limit": "信用额度",
            "payment_terms": "付款条款",
            "order_id": "订单编码",
            "order_number": "订单编号",
            "amount_total": "单据总额",
            "delivery_id": "发货单编码",
            "invoice_id": "发票编码",
            "amount_residual": "待结余款",
            "payment_state": "结算状态",
        },
    }
)


def build_std_o2c_blueprint() -> DeclarativeBlueprint:
    """将 ``std_o2c_ontology`` 唯一源码本体投影为标准 O2C 领域声明式蓝图。"""
    return export_spec_blueprint(
        spec,
        model_id="std_o2c_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        imports=("std_core_ontology",),
        description="标准 O2C 领域本体蓝图：在核心底座上组合客户信用档案、销售订单、发货单与应收发票。",
    )
