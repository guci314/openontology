"""标准 O2C（Order-to-Cash）纯业务领域本体（Std O2C Ontology · Pure M1）。

基于 ``std_core_ontology`` 最小核心扩展从订单到回款的标准骨干（只保留领域核心不变量，
不硬编码具体企业的个性化授信阈值或大单特批门槛）：
* 实体：``O2cCustomerProfile``、``O2cOrder``、``O2cDelivery``、``O2cInvoice``；
* 动作：``confirm_o2c_order``、``complete_o2c_delivery``、``post_o2c_invoice``、``record_o2c_payment``。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from open_ontology.induction.dsl import DeclarativeBlueprint, compose_blueprints
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )
except ImportError:
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        build_std_core_blueprint,
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


@spec.action(
    "confirm_o2c_order",
    ConfirmO2cOrderParams,
    allowed_roles=("sales_rep", "sales_manager", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("O2cOrder",),
    description="校验草稿状态与正金额后确认标准销售订单。",
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


@spec.action(
    "complete_o2c_delivery",
    CompleteO2cDeliveryParams,
    allowed_roles=("warehouse", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("O2cDelivery", "O2cOrder"),
    description="完成待发货履约单并同步推进关联销售订单为已发货（delivered）。",
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


@spec.action(
    "post_o2c_invoice",
    PostO2cInvoiceParams,
    allowed_roles=("finance", "finance_director", "steward", "ai_agent", "admin"),
    touched_object_types=("O2cInvoice", "O2cOrder"),
    description="将草稿应收发票正式过账并将关联订单推进为已开票（invoiced）。",
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


@spec.action(
    "record_o2c_payment",
    RecordO2cPaymentParams,
    allowed_roles=("finance", "finance_director", "steward", "ai_agent", "admin"),
    touched_object_types=("O2cInvoice",),
    description="登记已过账发票的回款核销（支持分期部分核销与全额结清）。",
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
            "profile_id": "客户档案编码",
            "credit_limit": "信用额度",
            "payment_terms": "付款条款",
            "order_id": "订单编码",
            "order_number": "订单编号",
            "amount_total": "单据总金额",
            "delivery_id": "发货单编码",
            "invoice_id": "发票编码",
            "amount_residual": "未清应收余额",
            "payment_state": "核销状态",
        },
    }
)


def build_std_o2c_blueprint() -> DeclarativeBlueprint:
    """构建标准 O2C 领域声明式蓝图（内部通过 ``compose_blueprints`` 导入核心底座）。"""
    o2c_fragment: dict[str, Any] = {
        "model_id": "_std_o2c_domain_fragment",
        "version": "1.0.0",
        "seed_ontology_id": "business_ontology",
        "object_types": [
            {
                "name": "O2cCustomerProfile",
                "archetype": "MasterEntity",
                "primary_key": "profile_id",
                "title_field": "profile_id",
                "aggregate_root": True,
                "owned": False,
                "display_label": "O2C客户信用档案",
                "fields": [
                    {"name": "profile_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "credit_limit", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "payment_terms", "field_type": "str", "default": "NET30"},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
                "relations": [
                    {"field": "party", "target_type": "BusinessParty", "required": True},
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                ],
            },
            {
                "name": "O2cOrder",
                "archetype": "ProcessDocument",
                "primary_key": "order_id",
                "title_field": "order_number",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["draft"],
                "transitions": {k: list(v) for k, v in _O2C_ORDER_TRANSITIONS.items()},
                "display_label": "O2C销售订单",
                "fields": [
                    {"name": "order_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "order_number", "field_type": "str", "required": True},
                    {"name": "amount_total", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "draft"},
                ],
                "relations": [
                    {"field": "customer", "target_type": "O2cCustomerProfile", "required": True},
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                ],
            },
            {
                "name": "O2cDelivery",
                "archetype": "ProcessDocument",
                "primary_key": "delivery_id",
                "title_field": "delivery_id",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["pending"],
                "transitions": {k: list(v) for k, v in _O2C_DELIVERY_TRANSITIONS.items()},
                "display_label": "O2C发货单",
                "fields": [
                    {"name": "delivery_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "status", "field_type": "str", "default": "pending"},
                ],
                "relations": [
                    {"field": "order", "target_type": "O2cOrder", "required": True},
                ],
            },
            {
                "name": "O2cInvoice",
                "archetype": "ProcessDocument",
                "primary_key": "invoice_id",
                "title_field": "invoice_id",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["draft"],
                "transitions": {k: list(v) for k, v in _O2C_INVOICE_TRANSITIONS.items()},
                "display_label": "O2C应收发票",
                "fields": [
                    {"name": "invoice_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "amount_total", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "amount_residual", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "payment_state", "field_type": "str", "default": "not_paid"},
                    {"name": "status", "field_type": "str", "default": "draft"},
                ],
                "relations": [
                    {"field": "order", "target_type": "O2cOrder", "required": True},
                ],
            },
        ],
        "actions": [
            {
                "name": "confirm_o2c_order",
                "display_label": "确认销售订单",
                "description": "校验草稿状态与正金额后确认标准销售订单。",
                "allowed_roles": ["sales_rep", "sales_manager", "ops", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "order_id", "field_type": "str", "required": True},
                ],
                "preconditions": [
                    {
                        "name": "o2c_order_must_be_draft",
                        "code": "ORDER_NOT_DRAFT",
                        "target_type": "O2cOrder",
                        "id_param": "order_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        "description": "O2cOrder must be in 'draft' status to confirm.",
                    },
                    {
                        "name": "o2c_order_amount_positive",
                        "code": "NON_POSITIVE_ORDER_AMOUNT",
                        "target_type": "O2cOrder",
                        "id_param": "order_id",
                        "must_hold": {
                            "op": "gt",
                            "left": {"ref": "obj.amount_total"},
                            "right": {"lit": 0},
                        },
                        "description": "O2cOrder.amount_total must be > 0 to confirm.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "O2cOrder",
                        "id_param": "order_id",
                        "set_fields": {"status": {"lit": "confirmed"}},
                    }
                ],
            },
            {
                "name": "complete_o2c_delivery",
                "display_label": "完成发货确认",
                "description": "完成待发货履约单并同步推进关联销售订单为已发货（delivered）。",
                "allowed_roles": ["warehouse", "ops", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "delivery_id", "field_type": "str", "required": True},
                ],
                "preconditions": [
                    {
                        "name": "o2c_delivery_must_be_pending",
                        "code": "DELIVERY_NOT_PENDING",
                        "target_type": "O2cDelivery",
                        "id_param": "delivery_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "pending"},
                        },
                        "description": "O2cDelivery must be 'pending' before completion.",
                    },
                    {
                        "name": "o2c_delivery_order_confirmed",
                        "code": "ORDER_NOT_CONFIRMED",
                        "target_type": "O2cDelivery",
                        "id_param": "delivery_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.order.status"},
                            "right": {"lit": "confirmed"},
                        },
                        "description": "Linked O2cOrder must be 'confirmed' before completing delivery.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "O2cDelivery",
                        "id_param": "delivery_id",
                        "set_fields": {"status": {"lit": "completed"}},
                    },
                    {
                        "op": "modify",
                        "object_type": "O2cOrder",
                        "target_ref": "obj.order",
                        "set_fields": {"status": {"lit": "delivered"}},
                    },
                ],
            },
            {
                "name": "post_o2c_invoice",
                "display_label": "应收发票过账",
                "description": "将草稿应收发票正式过账并将关联订单推进为已开票（invoiced）。",
                "allowed_roles": ["finance", "finance_director", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "invoice_id", "field_type": "str", "required": True},
                ],
                "preconditions": [
                    {
                        "name": "o2c_invoice_must_be_draft",
                        "code": "INVOICE_NOT_DRAFT",
                        "target_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        "description": "O2cInvoice must be 'draft' to post.",
                    },
                    {
                        "name": "o2c_invoice_order_billable",
                        "code": "ORDER_NOT_BILLABLE",
                        "target_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.order.status"},
                            "right": {"lit": ["confirmed", "delivered", "invoiced"]},
                        },
                        "description": "Linked O2cOrder must be confirmed, delivered, or invoiced.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "set_fields": {"status": {"lit": "posted"}},
                    },
                    {
                        "op": "modify",
                        "object_type": "O2cOrder",
                        "target_ref": "obj.order",
                        "set_fields": {"status": {"lit": "invoiced"}},
                    },
                ],
            },
            {
                "name": "record_o2c_payment",
                "display_label": "登记收款核销",
                "description": "登记已过账发票的回款核销（支持分期部分核销与全额结清）。",
                "allowed_roles": ["finance", "finance_director", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "invoice_id", "field_type": "str", "required": True},
                    {"name": "payment_amount", "field_type": "float", "required": True, "non_negative": True},
                ],
                "preconditions": [
                    {
                        "name": "o2c_payment_invoice_posted",
                        "code": "INVOICE_NOT_POSTED",
                        "target_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "posted"},
                        },
                        "description": "O2cInvoice must be 'posted' before recording payment.",
                    },
                    {
                        "name": "o2c_payment_amount_positive",
                        "code": "INVALID_PAYMENT_AMOUNT",
                        "target_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "must_hold": {
                            "op": "gt",
                            "left": {"ref": "param.payment_amount"},
                            "right": {"lit": 0},
                        },
                        "description": "payment_amount must be > 0.",
                    },
                    {
                        "name": "o2c_payment_not_exceed_residual",
                        "code": "PAYMENT_EXCEEDS_RESIDUAL",
                        "target_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "must_hold": {
                            "op": "le",
                            "left": {"ref": "param.payment_amount"},
                            "right": {"ref": "obj.amount_residual"},
                        },
                        "description": "payment_amount cannot exceed amount_residual.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "O2cInvoice",
                        "id_param": "invoice_id",
                        "set_fields": {
                            "amount_residual": {
                                "op": "sub",
                                "left": {"ref": "obj.amount_residual"},
                                "right": {"ref": "param.payment_amount"},
                            },
                            "payment_state": {
                                "op": "if_else",
                                "cond": {
                                    "op": "le",
                                    "left": {
                                        "op": "sub",
                                        "left": {"ref": "obj.amount_residual"},
                                        "right": {"ref": "param.payment_amount"},
                                    },
                                    "right": {"lit": 1e-9},
                                },
                                "then": {"lit": "paid"},
                                "else": {"lit": "partial"},
                            },
                        },
                    }
                ],
            },
        ],
        "display": {
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
        },
    }
    composed = compose_blueprints(
        [build_std_core_blueprint(), o2c_fragment],
        model_id="std_o2c_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="标准 O2C 领域本体蓝图：在核心底座上组合客户信用档案、销售订单、发货单与应收发票。",
    )
    composed.imports = ["std_core_ontology"]
    return composed
