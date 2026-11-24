"""Standard P2P (Procure-to-Pay) Domain Ontology (pure M1, zero O2C/CRM/vendor coupling).

Extends ``std_core_ontology`` with canonical procurement master/process types and
governed actions:
- ``P2pSupplierProfile``
- ``P2pPurchaseOrder``
- ``P2pGoodsReceipt``
- ``P2pVendorBill``

Hard invariants enforced synchronously at M1:
1. ``record_p2p_receipt`` deterministically moves both receipt and PO into ``disputed``
   when ``received_qty < expected_qty`` or ``inspection_status == "failed"``.
2. ``post_p2p_bill`` blocks with ``InvariantError(..., code="PO_DISPUTED_BLOCKED")`` whenever
   the parent ``P2pPurchaseOrder`` is ``disputed``, and blocks bills exceeding PO amount.
3. ``resolve_p2p_dispute`` enforces the synchronous M1 ``WorkCard`` pattern: an authorized
   resolver must complete a ``WorkCard`` targeting the disputed PO before resetting
   ``po.amount_total = settled_amount``, ``po.status = "received"``, and
   ``receipt.status = "completed"``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from open_ontology.induction.dsl import (
    ActionBlueprint,
    ActionMutationSpec,
    DeclarativeBlueprint,
    InvariantRuleSpec,
    export_spec_blueprint,
    register_rule_actions,
)
from open_ontology.spec import (
    ObjectProperty,
    OntologySpec,
)

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )

__all__ = [
    "spec",
    "P2pSupplierProfile",
    "P2pPurchaseOrder",
    "P2pGoodsReceipt",
    "P2pVendorBill",
    "ConfirmP2pOrderParams",
    "RecordP2pReceiptParams",
    "PostP2pBillParams",
    "ResolveP2pDisputeParams",
    "ReconcileP2pBillParams",
    "RecordP2pPaymentParams",
    "build_std_p2p_blueprint",
]

_P2P_PO_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("confirmed", "cancelled"),
    "confirmed": ("received", "disputed", "cancelled"),
    "received": ("billed", "disputed", "cancelled"),
    "disputed": ("received", "cancelled"),
    "billed": (),
    "cancelled": (),
}

_P2P_RECEIPT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("completed", "disputed", "cancelled"),
    "disputed": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}

_P2P_BILL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("posted", "cancelled"),
    "posted": (),
    "cancelled": (),
}

spec = OntologySpec(extends=(std_core_spec,))


@spec.object_type
class P2pSupplierProfile(MasterEntity):
    """P2P 供应商采购主数据（归属于共同往来伙伴 BusinessParty 与核算主体）。"""

    __primary_key__: ClassVar[str] = "profile_id"
    __title__: ClassVar[str] = "profile_id"

    profile_id: str
    party: ObjectProperty[BusinessParty]
    accounting_entity: ObjectProperty[AccountingEntity]
    payment_terms: str = "NET30"
    status: str = "active"


@spec.object_type
class P2pPurchaseOrder(ProcessDocument):
    """P2P 标准采购订单（含争议挂起与核定金额对账状态机）。"""

    __primary_key__: ClassVar[str] = "po_id"
    __title__: ClassVar[str] = "po_number"
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "ordered_qty",
        "unit_price",
        "amount_total",
    )
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _P2P_PO_TRANSITIONS

    po_id: str
    po_number: str
    supplier: ObjectProperty[P2pSupplierProfile]
    accounting_entity: ObjectProperty[AccountingEntity]
    ordered_qty: float = 0.0
    unit_price: float = 0.0
    amount_total: float = 0.0
    status: str = "draft"


@spec.object_type
class P2pGoodsReceipt(ProcessDocument):
    """P2P 采购收货质检单（实收不足或质检失败即转 disputed 拦截全额入账）。"""

    __primary_key__: ClassVar[str] = "receipt_id"
    __title__: ClassVar[str] = "receipt_id"
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("expected_qty", "received_qty")
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("pending",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _P2P_RECEIPT_TRANSITIONS

    receipt_id: str
    purchase_order: ObjectProperty[P2pPurchaseOrder]
    expected_qty: float = 0.0
    received_qty: float = 0.0
    inspection_status: str = "pending"
    status: str = "pending"


@spec.object_type
class P2pVendorBill(ProcessDocument):
    """P2P 供应商应付账单（受三单匹配与争议阻断守卫保护）。"""

    __primary_key__: ClassVar[str] = "bill_id"
    __title__: ClassVar[str] = "bill_id"
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("amount_total", "amount_residual")
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _P2P_BILL_TRANSITIONS

    bill_id: str
    purchase_order: ObjectProperty[P2pPurchaseOrder]
    amount_total: float = 0.0
    amount_residual: float = 0.0
    payment_state: str = "not_paid"
    status: str = "draft"


class ConfirmP2pOrderParams(BaseModel):
    po_id: str


class RecordP2pReceiptParams(BaseModel):
    receipt_id: str
    received_qty: float
    inspection_status: str = "passed"


class PostP2pBillParams(BaseModel):
    bill_id: str


class ResolveP2pDisputeParams(BaseModel):
    po_id: str
    receipt_id: str
    work_card_id: str
    settled_amount: float


class ReconcileP2pBillParams(BaseModel):
    bill_id: str


class RecordP2pPaymentParams(BaseModel):
    bill_id: str
    payment_amount: float


_P2P_RECEIPT_IS_DISPUTED = {
    "op": "or",
    "args": [
        {
            "op": "lt",
            "left": {"ref": "param.received_qty"},
            "right": {"ref": "obj.expected_qty"},
        },
        {
            "op": "eq",
            "left": {"ref": "param.inspection_status"},
            "right": {"lit": "failed"},
        },
    ],
}
_P2P_PO_DISPUTED_FROM_RECEIPT = {
    "op": "or",
    "args": [
        {
            "op": "lt",
            "left": {"ref": "param.received_qty"},
            "right": {"ref": "source_obj.expected_qty"},
        },
        {
            "op": "eq",
            "left": {"ref": "param.inspection_status"},
            "right": {"lit": "failed"},
        },
    ],
}
_P2P_PAYMENT_RESIDUAL_DIFF = {
    "op": "sub",
    "left": {"ref": "obj.amount_residual"},
    "right": {"ref": "param.payment_amount"},
}
_P2P_PAYMENT_SETTLED = {
    "op": "le",
    "left": _P2P_PAYMENT_RESIDUAL_DIFF,
    "right": {"lit": 1e-9},
}

register_rule_actions(
    spec,
    [
        (
            ActionBlueprint(
                name="confirm_p2p_order",
                description="校验供应商激活与正金额后确认标准采购订单。",
                allowed_roles=[
                    "buyer",
                    "procurement_manager",
                    "ops",
                    "finance_director",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="p2p_po_must_be_draft",
                        code="PO_NOT_DRAFT",
                        target_type="P2pPurchaseOrder",
                        id_param="po_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        description="仅草稿状态（draft）采购订单可执行确认。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_po_supplier_active",
                        code="SUPPLIER_INACTIVE",
                        target_type="P2pPurchaseOrder",
                        id_param="po_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.supplier.status"},
                            "right": {"lit": "active"},
                        },
                        description="采购订单关联供应商档案必须处于 active 状态。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_po_positive_totals",
                        code="NON_POSITIVE_PO_AMOUNT",
                        target_type="P2pPurchaseOrder",
                        id_param="po_id",
                        must_hold={
                            "op": "and",
                            "args": [
                                {
                                    "op": "gt",
                                    "left": {"ref": "obj.amount_total"},
                                    "right": {"lit": 0.0},
                                },
                                {
                                    "op": "gt",
                                    "left": {"ref": "obj.ordered_qty"},
                                    "right": {"lit": 0.0},
                                },
                            ],
                        },
                        description="采购订单数量与金额必须严格大于 0。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pPurchaseOrder",
                        id_param="po_id",
                        set_fields={"status": {"lit": "confirmed"}},
                    ),
                ],
            ),
            ConfirmP2pOrderParams,
        ),
        (
            ActionBlueprint(
                name="record_p2p_receipt",
                description="登记采购收货实收数量与质检结论；短装/短磅或质检不合格即联动 PO 进入 disputed。",
                allowed_roles=[
                    "warehouse",
                    "buyer",
                    "procurement_manager",
                    "ops",
                    "finance_director",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="p2p_receipt_must_be_pending",
                        code="RECEIPT_NOT_PENDING",
                        target_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "pending"},
                        },
                        description="仅待收货（pending）采购收货单可登记收货与质检结论。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_receipt_po_confirmable",
                        code="PO_NOT_CONFIRMED",
                        target_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": ["confirmed", "disputed"]},
                        },
                        description="关联采购订单必须处于 confirmed 或 disputed 状态方可登记收货。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_receipt_non_negative_qty",
                        code="NEGATIVE_RECEIVED_QTY",
                        target_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        must_hold={
                            "op": "ge",
                            "left": {"ref": "param.received_qty"},
                            "right": {"lit": 0.0},
                        },
                        description="实收数量 received_qty 不得为负。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_receipt_valid_inspection",
                        code="INVALID_INSPECTION_STATUS",
                        target_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "param.inspection_status"},
                            "right": {"lit": ["passed", "failed"]},
                        },
                        description="质检结论 inspection_status 必须为 passed 或 failed。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        set_fields={
                            "received_qty": {"ref": "param.received_qty"},
                            "inspection_status": {"ref": "param.inspection_status"},
                            "status": {
                                "op": "if_else",
                                "cond": _P2P_RECEIPT_IS_DISPUTED,
                                "then": {"lit": "disputed"},
                                "else": {"lit": "completed"},
                            },
                        },
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pPurchaseOrder",
                        target_ref="obj.purchase_order",
                        set_fields={
                            "status": {
                                "op": "if_else",
                                "cond": _P2P_PO_DISPUTED_FROM_RECEIPT,
                                "then": {"lit": "disputed"},
                                "else": {"lit": "received"},
                            },
                        },
                    ),
                ],
            ),
            RecordP2pReceiptParams,
        ),
        (
            ActionBlueprint(
                name="post_p2p_bill",
                description="正式过账供应商应付账单；若关联采购单处于 disputed 则以 PO_DISPUTED_BLOCKED 硬拦截。",
                allowed_roles=[
                    "finance",
                    "finance_director",
                    "procurement_manager",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="p2p_bill_po_not_disputed",
                        code="PO_DISPUTED_BLOCKED",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "ne",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "disputed"},
                        },
                        description="关联采购订单处于争议挂起（disputed）时严禁过账应付账单。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_bill_must_be_draft",
                        code="BILL_NOT_DRAFT",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        description="仅草稿状态（draft）应付账单可执行过账。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_bill_po_must_be_received",
                        code="PO_NOT_RECEIVED",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        when={
                            "op": "ne",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "disputed"},
                        },
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "received"},
                        },
                        description="非争议采购订单必须完成收货（received）方允许应付过账。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_bill_within_po_amount",
                        code="BILL_EXCEEDS_PO_AMOUNT",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "le",
                            "left": {"ref": "obj.amount_total"},
                            "right": {
                                "op": "add",
                                "left": {"ref": "obj.purchase_order.amount_total"},
                                "right": {"lit": 1e-9},
                            },
                        },
                        description="应付账单总额不得超过核定采购订单金额。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pVendorBill",
                        id_param="bill_id",
                        set_fields={"status": {"lit": "posted"}},
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pPurchaseOrder",
                        target_ref="obj.purchase_order",
                        set_fields={"status": {"lit": "billed"}},
                    ),
                ],
            ),
            PostP2pBillParams,
        ),
        (
            ActionBlueprint(
                name="resolve_p2p_dispute",
                description="凭已完成审批的 WorkCard 裁决采购争议，按核定结算金额解封 PO 与收货单。",
                allowed_roles=["procurement_manager", "finance_director", "steward", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="p2p_dispute_po_disputed",
                        code="PO_NOT_DISPUTED",
                        target_type="P2pPurchaseOrder",
                        id_param="po_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "disputed"},
                        },
                        description="仅 disputed 状态采购订单可裁决争议。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_dispute_receipt_matches_po",
                        code="RECEIPT_PO_MISMATCH",
                        target_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.purchase_order"},
                            "right": {"ref": "param.po_id"},
                        },
                        description="收货单必须关联当前待裁决采购订单。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_dispute_receipt_status",
                        code="RECEIPT_NOT_DISPUTED",
                        target_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["disputed", "completed"]},
                        },
                        description="收货单状态必须为 disputed 或 completed。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_dispute_card_completed",
                        code="WORK_CARD_NOT_COMPLETED",
                        target_type="WorkCard",
                        id_param="work_card_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "COMPLETED"},
                        },
                        description="裁决采购争议前关联审批工单卡必须已批结（COMPLETED）。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_dispute_card_target_match",
                        code="WORK_CARD_TARGET_MISMATCH",
                        target_type="WorkCard",
                        id_param="work_card_id",
                        must_hold={
                            "op": "and",
                            "args": [
                                {
                                    "op": "eq",
                                    "left": {"ref": "obj.target_object_type"},
                                    "right": {"lit": "P2pPurchaseOrder"},
                                },
                                {
                                    "op": "eq",
                                    "left": {"ref": "obj.target_object_id"},
                                    "right": {"ref": "param.po_id"},
                                },
                            ],
                        },
                        description="审批工单卡必须指向目标 P2pPurchaseOrder。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_dispute_non_negative_settlement",
                        code="NEGATIVE_SETTLED_AMOUNT",
                        target_type="P2pPurchaseOrder",
                        id_param="po_id",
                        must_hold={
                            "op": "ge",
                            "left": {"ref": "param.settled_amount"},
                            "right": {"lit": 0.0},
                        },
                        description="核定结算金额 settled_amount 不得为负。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pPurchaseOrder",
                        id_param="po_id",
                        set_fields={
                            "amount_total": {"ref": "param.settled_amount"},
                            "status": {"lit": "received"},
                        },
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pGoodsReceipt",
                        id_param="receipt_id",
                        when={
                            "op": "ne",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "completed"},
                        },
                        set_fields={"status": {"lit": "completed"}},
                    ),
                ],
            ),
            ResolveP2pDisputeParams,
        ),
        (
            ActionBlueprint(
                name="reconcile_p2p_bill",
                description="将草稿应付账单金额与残值同步至已核定采购单金额。",
                allowed_roles=[
                    "buyer",
                    "procurement_manager",
                    "finance",
                    "finance_director",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="p2p_reconcile_bill_draft",
                        code="BILL_NOT_DRAFT",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "draft"},
                        },
                        description="仅草稿状态（draft）应付账单可执行对账重算。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_reconcile_po_received",
                        code="PO_NOT_RECEIVED",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "received"},
                        },
                        description="对账前关联采购订单必须处于 received 状态。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pVendorBill",
                        id_param="bill_id",
                        set_fields={
                            "amount_total": {"ref": "obj.purchase_order.amount_total"},
                            "amount_residual": {"ref": "obj.purchase_order.amount_total"},
                        },
                    ),
                ],
            ),
            ReconcileP2pBillParams,
        ),
        (
            ActionBlueprint(
                name="record_p2p_payment",
                description="登记已过账应付账单的付款核销，校验付款额位于 (0, amount_residual]。",
                allowed_roles=["finance", "finance_director", "steward", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="p2p_payment_bill_posted",
                        code="BILL_NOT_POSTED",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "posted"},
                        },
                        description="仅已过账（posted）应付账单可登记付款核销。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_payment_positive_amount",
                        code="INVALID_PAYMENT_AMOUNT",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "gt",
                            "left": {"ref": "param.payment_amount"},
                            "right": {"lit": 0.0},
                        },
                        description="付款金额 payment_amount 必须严格大于 0。",
                    ),
                    InvariantRuleSpec(
                        name="p2p_payment_within_residual",
                        code="PAYMENT_EXCEEDS_RESIDUAL",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "le",
                            "left": {"ref": "param.payment_amount"},
                            "right": {
                                "op": "add",
                                "left": {"ref": "obj.amount_residual"},
                                "right": {"lit": 1e-9},
                            },
                        },
                        description="付款金额不得超过应付账单待结余款。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="P2pVendorBill",
                        id_param="bill_id",
                        set_fields={
                            "amount_residual": {
                                "op": "if_else",
                                "cond": _P2P_PAYMENT_SETTLED,
                                "then": {"lit": 0.0},
                                "else": _P2P_PAYMENT_RESIDUAL_DIFF,
                            },
                            "payment_state": {
                                "op": "if_else",
                                "cond": _P2P_PAYMENT_SETTLED,
                                "then": {"lit": "paid"},
                                "else": {"lit": "partial"},
                            },
                        },
                    ),
                ],
            ),
            RecordP2pPaymentParams,
        ),
    ],
)


spec.update_display(
    {
        "ontologies": {
            "std_p2p_ontology": "标准 P2P 领域本体（供应商主数据·采购订单·收货质检·应付账单）",
        },
        "objects": {
            "P2pSupplierProfile": "P2P供应商采购档案",
            "P2pPurchaseOrder": "P2P采购订单",
            "P2pGoodsReceipt": "P2P采购收货单",
            "P2pVendorBill": "P2P应付账单",
        },
        "fields": {
            "profile_id": "主档ID",
            "party": "往来伙伴",
            "accounting_entity": "核算主体",
            "payment_terms": "付款条款",
            "po_id": "采购订单编码",
            "po_number": "采购单号",
            "supplier": "供应商档案",
            "ordered_qty": "订购数量",
            "unit_price": "单价",
            "amount_total": "单据总额",
            "receipt_id": "收货单编码",
            "purchase_order": "关联采购单",
            "expected_qty": "应收数量",
            "received_qty": "实收数量",
            "inspection_status": "质检结论",
            "bill_id": "应付单编码",
            "amount_residual": "待结余款",
            "payment_state": "结算状态",
        },
        "actions": {
            "confirm_p2p_order": "确认采购订单",
            "record_p2p_receipt": "采购收货与质检",
            "post_p2p_bill": "应付账单过账",
            "resolve_p2p_dispute": "凭工单裁决采购争议",
            "reconcile_p2p_bill": "对账重算草稿应付单",
            "record_p2p_payment": "登记采购付款核销",
        },
    }
)


def build_std_p2p_blueprint() -> DeclarativeBlueprint:
    """将 ``std_p2p_ontology`` 唯一源码本体投影为标准 P2P 领域声明式蓝图。"""
    return export_spec_blueprint(
        spec,
        model_id="std_p2p_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        imports=("std_core_ontology",),
        description="基于 std_core 扩展的通用标准 P2P（采购到付款）核心领域本体。",
    )
