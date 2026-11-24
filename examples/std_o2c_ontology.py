"""标准 O2C（Order-to-Cash）纯业务领域本体（Std O2C Ontology · Pure M1）。

基于 ``std_core_ontology`` 最小核心扩展从订单到回款的标准骨干（只保留领域核心不变量，
不硬编码具体企业的个性化授信阈值或大单特批门槛）：
* 实体：``O2cCustomerProfile``、``O2cOrder``、``O2cDelivery``、``O2cInvoice``；
* 动作：``confirm_o2c_order``、``complete_o2c_delivery``、``post_o2c_invoice``、``record_o2c_payment``。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from open_ontology.induction.dsl import (
    ActionBlueprint,
    ActionMutationSpec,
    DeclarativeBlueprint,
    InvariantRuleSpec,
    export_spec_blueprint,
    register_rule_actions,
)
from open_ontology.spec import ObjectProperty, OntologySpec

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


_O2C_PAYMENT_RESIDUAL_DIFF = {
    "op": "sub",
    "left": {"ref": "obj.amount_residual"},
    "right": {"ref": "param.payment_amount"},
}
_O2C_PAYMENT_SETTLED = {
    "op": "le",
    "left": _O2C_PAYMENT_RESIDUAL_DIFF,
    "right": {"lit": 1e-9},
}

register_rule_actions(
    spec,
    [
        (
            ActionBlueprint(
                name="confirm_o2c_order",
                description="校验草稿状态与正金额后确认标准销售订单。",
                allowed_roles=[
                    "sales_rep",
                    "sales_manager",
                    "ops",
                    "finance_director",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="o2c_order_must_be_draft",
                        code="ORDER_NOT_DRAFT",
                        target_type="O2cOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        description="仅草稿状态（draft）销售订单可执行确认。",
                    ),
                    InvariantRuleSpec(
                        name="o2c_order_positive_amount",
                        code="NON_POSITIVE_ORDER_AMOUNT",
                        target_type="O2cOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "gt",
                            "left": {"ref": "obj.amount_total"},
                            "right": {"lit": 0.0},
                        },
                        description="销售订单总额必须严格大于 0。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="O2cOrder",
                        id_param="order_id",
                        set_fields={"status": {"lit": "confirmed"}},
                    ),
                ],
            ),
            ConfirmO2cOrderParams,
        ),
        (
            ActionBlueprint(
                name="complete_o2c_delivery",
                description="完成待发货履约单并同步推进关联销售订单为已发货（delivered）。",
                allowed_roles=[
                    "warehouse",
                    "ops",
                    "finance_director",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="o2c_delivery_must_be_pending",
                        code="DELIVERY_NOT_PENDING",
                        target_type="O2cDelivery",
                        id_param="delivery_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "pending"},
                        },
                        description="仅待发货（pending）履约单可执行完成确认。",
                    ),
                    InvariantRuleSpec(
                        name="o2c_delivery_order_confirmed",
                        code="ORDER_NOT_CONFIRMED",
                        target_type="O2cDelivery",
                        id_param="delivery_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.order.status"},
                            "right": {"lit": "confirmed"},
                        },
                        description="关联销售订单必须已确认（confirmed）方可执行发货确认。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="O2cDelivery",
                        id_param="delivery_id",
                        set_fields={"status": {"lit": "completed"}},
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="O2cOrder",
                        target_ref="obj.order",
                        set_fields={"status": {"lit": "delivered"}},
                    ),
                ],
            ),
            CompleteO2cDeliveryParams,
        ),
        (
            ActionBlueprint(
                name="post_o2c_invoice",
                description="将草稿应收发票正式过账并将关联订单推进为已开票（invoiced）。",
                allowed_roles=["finance", "finance_director", "steward", "ai_agent", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="o2c_invoice_must_be_draft",
                        code="INVOICE_NOT_DRAFT",
                        target_type="O2cInvoice",
                        id_param="invoice_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        description="仅草稿状态（draft）应收发票可执行正式过账。",
                    ),
                    InvariantRuleSpec(
                        name="o2c_invoice_order_billable",
                        code="ORDER_NOT_BILLABLE",
                        target_type="O2cInvoice",
                        id_param="invoice_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.order.status"},
                            "right": {"lit": ["confirmed", "delivered", "invoiced"]},
                        },
                        description="关联销售订单必须已确认/已发货/已开票方可过账应收发票。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="O2cInvoice",
                        id_param="invoice_id",
                        set_fields={"status": {"lit": "posted"}},
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="O2cOrder",
                        target_ref="obj.order",
                        set_fields={"status": {"lit": "invoiced"}},
                    ),
                ],
            ),
            PostO2cInvoiceParams,
        ),
        (
            ActionBlueprint(
                name="record_o2c_payment",
                description="登记已过账发票的回款核销（支持分期部分核销与全额结清）。",
                allowed_roles=["finance", "finance_director", "steward", "ai_agent", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="o2c_payment_invoice_posted",
                        code="INVOICE_NOT_POSTED",
                        target_type="O2cInvoice",
                        id_param="invoice_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "posted"},
                        },
                        description="仅已过账（posted）应收发票可登记回款核销。",
                    ),
                    InvariantRuleSpec(
                        name="o2c_payment_positive_amount",
                        code="INVALID_PAYMENT_AMOUNT",
                        target_type="O2cInvoice",
                        id_param="invoice_id",
                        must_hold={
                            "op": "gt",
                            "left": {"ref": "param.payment_amount"},
                            "right": {"lit": 0.0},
                        },
                        description="回款核销金额必须严格大于 0。",
                    ),
                    InvariantRuleSpec(
                        name="o2c_payment_within_residual",
                        code="PAYMENT_EXCEEDS_RESIDUAL",
                        target_type="O2cInvoice",
                        id_param="invoice_id",
                        must_hold={
                            "op": "le",
                            "left": {"ref": "param.payment_amount"},
                            "right": {"ref": "obj.amount_residual"},
                        },
                        description="回款核销金额不得超过发票待结余款。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="O2cInvoice",
                        id_param="invoice_id",
                        set_fields={
                            "amount_residual": {
                                "op": "if_else",
                                "cond": _O2C_PAYMENT_SETTLED,
                                "then": {"lit": 0.0},
                                "else": _O2C_PAYMENT_RESIDUAL_DIFF,
                            },
                            "payment_state": {
                                "op": "if_else",
                                "cond": _O2C_PAYMENT_SETTLED,
                                "then": {"lit": "paid"},
                                "else": {"lit": "partial"},
                            },
                        },
                    ),
                ],
            ),
            RecordO2cPaymentParams,
        ),
    ],
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
