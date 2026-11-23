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

from open_ontology.induction.dsl import DeclarativeBlueprint, compose_blueprints
from open_ontology.spec import (
    InvariantError,
    ObjectProperty,
    OntologySpec,
)

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        AccountingEntity,
        BusinessParty,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
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


@spec.edit_function(
    "confirm_p2p_order",
    ConfirmP2pOrderParams,
    description="校验供应商激活与正金额后确认标准采购订单。",
)
def confirm_p2p_order(ctx: Any, params: ConfirmP2pOrderParams) -> None:
    po = ctx.require(P2pPurchaseOrder, params.po_id)
    if po.status != "draft":
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} status is {po.status!r}, expected 'draft'",
            code="PO_NOT_DRAFT",
        )
    supplier = ctx.require(P2pSupplierProfile, po.supplier)
    if supplier.status != "active":
        raise InvariantError(
            f"P2pSupplierProfile:{supplier.profile_id} status is {supplier.status!r}, expected 'active'",
            code="SUPPLIER_INACTIVE",
        )
    if po.amount_total <= 0 or po.ordered_qty <= 0:
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} ordered_qty/amount_total must be > 0",
            code="NON_POSITIVE_PO_AMOUNT",
        )
    ctx.modify(po, status="confirmed")

spec.declare_action(
    "confirm_p2p_order",
    edit_function="confirm_p2p_order",
    allowed_roles=("buyer", "procurement_manager", "ops", "finance_director", "steward", "ai_agent", "admin"),
    description="校验供应商激活与正金额后确认标准采购订单。",
)


@spec.edit_function(
    "record_p2p_receipt",
    RecordP2pReceiptParams,
    description="登记采购收货实收数量与质检结论；短装/短磅或质检不合格即联动 PO 进入 disputed。",
)
def record_p2p_receipt(ctx: Any, params: RecordP2pReceiptParams) -> None:
    receipt = ctx.require(P2pGoodsReceipt, params.receipt_id)
    if receipt.status != "pending":
        raise InvariantError(
            f"P2pGoodsReceipt:{receipt.receipt_id} status is {receipt.status!r}, expected 'pending'",
            code="RECEIPT_NOT_PENDING",
        )
    po = ctx.require(P2pPurchaseOrder, receipt.purchase_order)
    if po.status not in ("confirmed", "disputed"):
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} status is {po.status!r}, expected confirmed/disputed",
            code="PO_NOT_CONFIRMED",
        )
    if params.received_qty < 0:
        raise InvariantError("received_qty must be >= 0", code="NEGATIVE_RECEIVED_QTY")
    if params.inspection_status not in ("passed", "failed"):
        raise InvariantError(
            f"inspection_status {params.inspection_status!r} must be 'passed' or 'failed'",
            code="INVALID_INSPECTION_STATUS",
        )
    is_disputed = params.received_qty < receipt.expected_qty or params.inspection_status == "failed"
    ctx.modify(
        receipt,
        received_qty=params.received_qty,
        inspection_status=params.inspection_status,
        status="disputed" if is_disputed else "completed",
    )
    ctx.modify(po, status="disputed" if is_disputed else "received")

spec.declare_action(
    "record_p2p_receipt",
    edit_function="record_p2p_receipt",
    allowed_roles=("warehouse", "buyer", "procurement_manager", "ops", "finance_director", "steward", "ai_agent", "admin"),
    description="登记采购收货实收数量与质检结论；短装/短磅或质检不合格即联动 PO 进入 disputed。",
)


@spec.edit_function(
    "post_p2p_bill",
    PostP2pBillParams,
    description="正式过账供应商应付账单；若关联采购单处于 disputed 则以 PO_DISPUTED_BLOCKED 硬拦截。",
)
def post_p2p_bill(ctx: Any, params: PostP2pBillParams) -> None:
    bill = ctx.require(P2pVendorBill, params.bill_id)
    po = ctx.require(P2pPurchaseOrder, bill.purchase_order)
    if po.status == "disputed":
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} is 'disputed'; resolve dispute before posting {bill.bill_id}",
            code="PO_DISPUTED_BLOCKED",
        )
    if bill.status != "draft":
        raise InvariantError(
            f"P2pVendorBill:{bill.bill_id} status is {bill.status!r}, expected 'draft'",
            code="BILL_NOT_DRAFT",
        )
    if po.status != "received":
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} status is {po.status!r}, expected 'received'",
            code="PO_NOT_RECEIVED",
        )
    if bill.amount_total > po.amount_total + 1e-9:
        raise InvariantError(
            f"P2pVendorBill:{bill.bill_id} amount {bill.amount_total} exceeds settled PO amount {po.amount_total}",
            code="BILL_EXCEEDS_PO_AMOUNT",
        )
    ctx.modify(bill, status="posted")
    ctx.modify(po, status="billed")

spec.declare_action(
    "post_p2p_bill",
    edit_function="post_p2p_bill",
    allowed_roles=("finance", "finance_director", "procurement_manager", "steward", "ai_agent", "admin"),
    description="正式过账供应商应付账单；若关联采购单处于 disputed 则以 PO_DISPUTED_BLOCKED 硬拦截。",
)


@spec.edit_function(
    "resolve_p2p_dispute",
    ResolveP2pDisputeParams,
    description="凭已完成审批的 WorkCard 裁决采购争议，按核定结算金额解封 PO 与收货单。",
)
def resolve_p2p_dispute(ctx: Any, params: ResolveP2pDisputeParams) -> None:
    po = ctx.require(P2pPurchaseOrder, params.po_id)
    receipt = ctx.require(P2pGoodsReceipt, params.receipt_id)
    card = ctx.require(WorkCard, params.work_card_id)
    if po.status != "disputed":
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} must be 'disputed' to resolve",
            code="PO_NOT_DISPUTED",
        )
    if receipt.purchase_order != po.po_id:
        raise InvariantError(
            f"P2pGoodsReceipt:{receipt.receipt_id} does not belong to {po.po_id}",
            code="RECEIPT_PO_MISMATCH",
        )
    if receipt.status not in ("disputed", "completed"):
        raise InvariantError(
            f"P2pGoodsReceipt:{receipt.receipt_id} status {receipt.status!r} cannot resolve dispute",
            code="RECEIPT_NOT_DISPUTED",
        )
    if card.status != "COMPLETED":
        raise InvariantError(
            f"WorkCard:{card.card_id} must be COMPLETED before resolving PO dispute",
            code="WORK_CARD_NOT_COMPLETED",
        )
    if card.target_object_type != "P2pPurchaseOrder" or card.target_object_id != po.po_id:
        raise InvariantError(
            f"WorkCard:{card.card_id} does not target P2pPurchaseOrder:{po.po_id}",
            code="WORK_CARD_TARGET_MISMATCH",
        )
    if params.settled_amount < 0:
        raise InvariantError("settled_amount must be >= 0", code="NEGATIVE_SETTLED_AMOUNT")
    ctx.modify(po, amount_total=params.settled_amount, status="received")
    if receipt.status != "completed":
        ctx.modify(receipt, status="completed")

spec.declare_action(
    "resolve_p2p_dispute",
    edit_function="resolve_p2p_dispute",
    allowed_roles=("procurement_manager", "finance_director", "steward", "admin"),
    description="凭已完成审批的 WorkCard 裁决采购争议，按核定结算金额解封 PO 与收货单。",
)


@spec.edit_function(
    "reconcile_p2p_bill",
    ReconcileP2pBillParams,
    description="将草稿应付账单金额与残值同步至已核定采购单金额。",
)
def reconcile_p2p_bill(ctx: Any, params: ReconcileP2pBillParams) -> None:
    bill = ctx.require(P2pVendorBill, params.bill_id)
    po = ctx.require(P2pPurchaseOrder, bill.purchase_order)
    if bill.status != "draft":
        raise InvariantError(
            f"P2pVendorBill:{bill.bill_id} must be 'draft' to reconcile",
            code="BILL_NOT_DRAFT",
        )
    if po.status != "received":
        raise InvariantError(
            f"P2pPurchaseOrder:{po.po_id} must be 'received' before reconciling bill",
            code="PO_NOT_RECEIVED",
        )
    ctx.modify(
        bill,
        amount_total=po.amount_total,
        amount_residual=po.amount_total,
    )

spec.declare_action(
    "reconcile_p2p_bill",
    edit_function="reconcile_p2p_bill",
    allowed_roles=("buyer", "procurement_manager", "finance", "finance_director", "steward", "ai_agent", "admin"),
    description="将草稿应付账单金额与残值同步至已核定采购单金额。",
)


@spec.edit_function(
    "record_p2p_payment",
    RecordP2pPaymentParams,
    description="登记已过账应付账单的付款核销，校验付款额位于 (0, amount_residual]。",
)
def record_p2p_payment(ctx: Any, params: RecordP2pPaymentParams) -> None:
    bill = ctx.require(P2pVendorBill, params.bill_id)
    if bill.status != "posted":
        raise InvariantError(
            f"P2pVendorBill:{bill.bill_id} must be 'posted' before recording payment",
            code="BILL_NOT_POSTED",
        )
    if params.payment_amount <= 0:
        raise InvariantError("payment_amount must be > 0", code="INVALID_PAYMENT_AMOUNT")
    if params.payment_amount > bill.amount_residual + 1e-9:
        raise InvariantError(
            f"payment_amount {params.payment_amount} exceeds amount_residual {bill.amount_residual}",
            code="PAYMENT_EXCEEDS_RESIDUAL",
        )
    remaining = max(0.0, bill.amount_residual - params.payment_amount)
    new_state = "paid" if remaining <= 1e-9 else "partial"
    ctx.modify(bill, amount_residual=remaining, payment_state=new_state)

spec.declare_action(
    "record_p2p_payment",
    edit_function="record_p2p_payment",
    allowed_roles=("finance", "finance_director", "steward", "admin"),
    description="登记已过账应付账单的付款核销，校验付款额位于 (0, amount_residual]。",
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
            "profile_id": "档案编码",
            "party": "往来伙伴",
            "accounting_entity": "核算主体",
            "payment_terms": "付款条款",
            "po_id": "采购订单编码",
            "po_number": "采购单号",
            "supplier": "供应商档案",
            "ordered_qty": "订购数量",
            "unit_price": "单价",
            "amount_total": "总金额",
            "receipt_id": "收货单编码",
            "purchase_order": "关联采购单",
            "expected_qty": "应收数量",
            "received_qty": "实收数量",
            "inspection_status": "质检结论",
            "bill_id": "应付单编码",
            "amount_residual": "未付余额",
            "payment_state": "付款状态",
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
    """构建与 ``std_p2p_ontology`` 完全对称的零 ``exec/eval`` 声明式领域蓝图（基于 std_core 组合）。"""
    core_bp = build_std_core_blueprint()
    p2p_payload: dict[str, Any] = {
        "model_id": "std_p2p_fragment",
        "version": "1.0.0",
        "seed_ontology_id": "business_ontology",
        "roles": list(core_bp.roles),
        "description": "标准 P2P（采购到付款）领域蓝图切片。",
        "object_types": [
            {
                "name": "P2pSupplierProfile",
                "archetype": "MasterEntity",
                "primary_key": "profile_id",
                "title_field": "profile_id",
                "owned": False,
                "display_label": "P2P供应商采购档案",
                "fields": [
                    {"name": "profile_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "payment_terms", "field_type": "str", "default": "NET30"},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
                "relations": [
                    {"field": "party", "target_type": "BusinessParty", "required": True},
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                ],
            },
            {
                "name": "P2pPurchaseOrder",
                "archetype": "ProcessDocument",
                "primary_key": "po_id",
                "title_field": "po_number",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["draft"],
                "transitions": {k: list(v) for k, v in _P2P_PO_TRANSITIONS.items()},
                "display_label": "P2P采购订单",
                "fields": [
                    {"name": "po_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "po_number", "field_type": "str", "required": True},
                    {"name": "ordered_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "unit_price", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "amount_total", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "draft"},
                ],
                "relations": [
                    {"field": "supplier", "target_type": "P2pSupplierProfile", "required": True},
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                ],
            },
            {
                "name": "P2pGoodsReceipt",
                "archetype": "ProcessDocument",
                "primary_key": "receipt_id",
                "title_field": "receipt_id",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["pending"],
                "transitions": {k: list(v) for k, v in _P2P_RECEIPT_TRANSITIONS.items()},
                "display_label": "P2P采购收货单",
                "fields": [
                    {"name": "receipt_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "expected_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "received_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "inspection_status", "field_type": "str", "default": "pending"},
                    {"name": "status", "field_type": "str", "default": "pending"},
                ],
                "relations": [
                    {"field": "purchase_order", "target_type": "P2pPurchaseOrder", "required": True},
                ],
            },
            {
                "name": "P2pVendorBill",
                "archetype": "ProcessDocument",
                "primary_key": "bill_id",
                "title_field": "bill_id",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["draft"],
                "transitions": {k: list(v) for k, v in _P2P_BILL_TRANSITIONS.items()},
                "display_label": "P2P应付账单",
                "fields": [
                    {"name": "bill_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "amount_total", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "amount_residual", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "payment_state", "field_type": "str", "default": "not_paid"},
                    {"name": "status", "field_type": "str", "default": "draft"},
                ],
                "relations": [
                    {"field": "purchase_order", "target_type": "P2pPurchaseOrder", "required": True},
                ],
            },
        ],
        "actions": [
            {
                "name": "confirm_p2p_order",
                "display_label": "确认采购订单",
                "description": "校验供应商激活与正金额后确认标准采购订单。",
                "allowed_roles": ["buyer", "procurement_manager", "ops", "finance_director", "steward", "ai_agent", "admin"],
                "params": [{"name": "po_id", "field_type": "str", "required": True}],
                "preconditions": [
                    {
                        "name": "p2p_po_must_be_draft",
                        "code": "PO_NOT_DRAFT",
                        "target_type": "P2pPurchaseOrder",
                        "id_param": "po_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                        "description": "P2pPurchaseOrder must be 'draft' to confirm.",
                    },
                    {
                        "name": "p2p_supplier_must_be_active",
                        "code": "SUPPLIER_INACTIVE",
                        "target_type": "P2pPurchaseOrder",
                        "id_param": "po_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.supplier.status"}, "right": {"lit": "active"}},
                        "description": "Supplier must be 'active'.",
                    },
                    {
                        "name": "p2p_po_amount_positive",
                        "code": "NON_POSITIVE_PO_AMOUNT",
                        "target_type": "P2pPurchaseOrder",
                        "id_param": "po_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 0}},
                                {"op": "gt", "left": {"ref": "obj.ordered_qty"}, "right": {"lit": 0}},
                            ],
                        },
                        "description": "PO ordered_qty and amount_total must be > 0.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "P2pPurchaseOrder",
                        "id_param": "po_id",
                        "set_fields": {"status": {"lit": "confirmed"}},
                    }
                ],
            },
            {
                "name": "record_p2p_receipt",
                "display_label": "采购收货与质检",
                "description": "登记采购收货实收数量与质检结论；短装或质检不合格即联动 PO 进入 disputed。",
                "allowed_roles": ["warehouse", "buyer", "procurement_manager", "ops", "finance_director", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "receipt_id", "field_type": "str", "required": True},
                    {"name": "received_qty", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "inspection_status", "field_type": "str", "default": "passed"},
                ],
                "preconditions": [
                    {
                        "name": "p2p_receipt_must_be_pending",
                        "code": "RECEIPT_NOT_PENDING",
                        "target_type": "P2pGoodsReceipt",
                        "id_param": "receipt_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "pending"}},
                        "description": "P2pGoodsReceipt must be 'pending'.",
                    },
                    {
                        "name": "p2p_receipt_po_confirmed_or_disputed",
                        "code": "PO_NOT_CONFIRMED",
                        "target_type": "P2pGoodsReceipt",
                        "id_param": "receipt_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": ["confirmed", "disputed"]},
                        },
                        "description": "Linked PO must be confirmed or disputed.",
                    },
                    {
                        "name": "p2p_receipt_inspection_valid",
                        "code": "INVALID_INSPECTION_STATUS",
                        "target_type": "P2pGoodsReceipt",
                        "id_param": "receipt_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "param.inspection_status"},
                            "right": {"lit": ["passed", "failed"]},
                        },
                        "description": "inspection_status must be passed or failed.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "P2pGoodsReceipt",
                        "id_param": "receipt_id",
                        "set_fields": {
                            "received_qty": {"ref": "param.received_qty"},
                            "inspection_status": {"ref": "param.inspection_status"},
                            "status": {
                                "op": "if_else",
                                "cond": {
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
                                },
                                "then": {"lit": "disputed"},
                                "else": {"lit": "completed"},
                            },
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "P2pPurchaseOrder",
                        "target_ref": "obj.purchase_order",
                        "set_fields": {
                            "status": {
                                "op": "if_else",
                                "cond": {
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
                                },
                                "then": {"lit": "disputed"},
                                "else": {"lit": "received"},
                            }
                        },
                    },
                ],
            },
            {
                "name": "post_p2p_bill",
                "display_label": "应付账单过账",
                "description": "正式过账供应商应付账单；若关联采购单处于 disputed 则以 PO_DISPUTED_BLOCKED 硬拦截。",
                "allowed_roles": ["finance", "finance_director", "procurement_manager", "steward", "ai_agent", "admin"],
                "params": [{"name": "bill_id", "field_type": "str", "required": True}],
                "preconditions": [
                    {
                        "name": "p2p_bill_po_not_disputed",
                        "code": "PO_DISPUTED_BLOCKED",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {
                            "op": "ne",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "disputed"},
                        },
                        "description": "Blocked while linked P2pPurchaseOrder is disputed.",
                    },
                    {
                        "name": "p2p_bill_must_be_draft",
                        "code": "BILL_NOT_DRAFT",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                        "description": "P2pVendorBill must be draft.",
                    },
                    {
                        "name": "p2p_bill_po_must_be_received",
                        "code": "PO_NOT_RECEIVED",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "received"},
                        },
                        "description": "Linked P2pPurchaseOrder must be received.",
                    },
                    {
                        "name": "p2p_bill_within_po_amount",
                        "code": "BILL_EXCEEDS_PO_AMOUNT",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {
                            "op": "le",
                            "left": {"ref": "obj.amount_total"},
                            "right": {
                                "op": "add",
                                "left": {"ref": "obj.purchase_order.amount_total"},
                                "right": {"lit": 1e-9},
                            },
                        },
                        "description": "Vendor bill amount_total must not exceed PO amount_total.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "set_fields": {"status": {"lit": "posted"}},
                    },
                    {
                        "op": "modify",
                        "object_type": "P2pPurchaseOrder",
                        "target_ref": "obj.purchase_order",
                        "set_fields": {"status": {"lit": "billed"}},
                    },
                ],
            },
            {
                "name": "resolve_p2p_dispute",
                "display_label": "凭工单裁决采购争议",
                "description": "凭已完成审批的 WorkCard 裁决采购争议，按核定结算金额解封 PO 与收货单。",
                "allowed_roles": ["procurement_manager", "finance_director", "steward", "admin"],
                "params": [
                    {"name": "po_id", "field_type": "str", "required": True},
                    {"name": "receipt_id", "field_type": "str", "required": True},
                    {"name": "work_card_id", "field_type": "str", "required": True},
                    {"name": "settled_amount", "field_type": "float", "required": True, "non_negative": True},
                ],
                "preconditions": [
                    {
                        "name": "p2p_resolve_po_disputed",
                        "code": "PO_NOT_DISPUTED",
                        "target_type": "P2pPurchaseOrder",
                        "id_param": "po_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "disputed"}},
                        "description": "P2pPurchaseOrder must be disputed.",
                    },
                    {
                        "name": "p2p_resolve_receipt_matches_po",
                        "code": "RECEIPT_PO_MISMATCH",
                        "target_type": "P2pGoodsReceipt",
                        "id_param": "receipt_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "obj.purchase_order"}, "right": {"ref": "param.po_id"}},
                                {"op": "in", "left": {"ref": "obj.status"}, "right": {"lit": ["disputed", "completed"]}},
                            ],
                        },
                        "description": "Receipt must belong to PO and be disputed/completed.",
                    },
                    {
                        "name": "p2p_resolve_work_card_completed",
                        "code": "WORK_CARD_NOT_COMPLETED",
                        "target_type": "WorkCard",
                        "id_param": "work_card_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "COMPLETED"}},
                        "description": "WorkCard must be COMPLETED.",
                    },
                    {
                        "name": "p2p_resolve_work_card_target",
                        "code": "WORK_CARD_TARGET_MISMATCH",
                        "target_type": "WorkCard",
                        "id_param": "work_card_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "obj.target_object_type"}, "right": {"lit": "P2pPurchaseOrder"}},
                                {"op": "eq", "left": {"ref": "obj.target_object_id"}, "right": {"ref": "param.po_id"}},
                            ],
                        },
                        "description": "WorkCard must target the disputed P2pPurchaseOrder.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "P2pPurchaseOrder",
                        "id_param": "po_id",
                        "set_fields": {
                            "amount_total": {"ref": "param.settled_amount"},
                            "status": {"lit": "received"},
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "P2pGoodsReceipt",
                        "id_param": "receipt_id",
                        "set_fields": {"status": {"lit": "completed"}},
                    },
                ],
            },
            {
                "name": "reconcile_p2p_bill",
                "display_label": "对账重算草稿应付单",
                "description": "将草稿应付账单金额与残值同步至已核定采购单金额。",
                "allowed_roles": ["buyer", "procurement_manager", "finance", "finance_director", "steward", "ai_agent", "admin"],
                "params": [{"name": "bill_id", "field_type": "str", "required": True}],
                "preconditions": [
                    {
                        "name": "p2p_reconcile_bill_draft",
                        "code": "BILL_NOT_DRAFT",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                        "description": "P2pVendorBill must be draft to reconcile.",
                    },
                    {
                        "name": "p2p_reconcile_po_received",
                        "code": "PO_NOT_RECEIVED",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "received"},
                        },
                        "description": "Linked PO must be received.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "set_fields": {
                            "amount_total": {"ref": "obj.purchase_order.amount_total"},
                            "amount_residual": {"ref": "obj.purchase_order.amount_total"},
                        },
                    }
                ],
            },
            {
                "name": "record_p2p_payment",
                "display_label": "登记采购付款核销",
                "description": "登记已过账应付账单的付款核销，校验付款额位于 (0, amount_residual]。",
                "allowed_roles": ["finance", "finance_director", "steward", "admin"],
                "params": [
                    {"name": "bill_id", "field_type": "str", "required": True},
                    {"name": "payment_amount", "field_type": "float", "required": True, "non_negative": True},
                ],
                "preconditions": [
                    {
                        "name": "p2p_payment_bill_posted",
                        "code": "BILL_NOT_POSTED",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "posted"}},
                        "description": "P2pVendorBill must be posted.",
                    },
                    {
                        "name": "p2p_payment_amount_positive",
                        "code": "INVALID_PAYMENT_AMOUNT",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {"op": "gt", "left": {"ref": "param.payment_amount"}, "right": {"lit": 0}},
                        "description": "payment_amount must be > 0.",
                    },
                    {
                        "name": "p2p_payment_within_residual",
                        "code": "PAYMENT_EXCEEDS_RESIDUAL",
                        "target_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "must_hold": {
                            "op": "le",
                            "left": {"ref": "param.payment_amount"},
                            "right": {
                                "op": "add",
                                "left": {"ref": "obj.amount_residual"},
                                "right": {"lit": 1e-9},
                            },
                        },
                        "description": "payment_amount cannot exceed amount_residual.",
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "P2pVendorBill",
                        "id_param": "bill_id",
                        "set_fields": {
                            "amount_residual": {
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
                                "then": {"lit": 0.0},
                                "else": {
                                    "op": "sub",
                                    "left": {"ref": "obj.amount_residual"},
                                    "right": {"ref": "param.payment_amount"},
                                },
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
                "std_p2p_ontology": "标准 P2P 领域本体（供应商主数据·采购订单·收货质检·应付账单）",
            },
            "objects": {
                "P2pSupplierProfile": "P2P供应商采购档案",
                "P2pPurchaseOrder": "P2P采购订单",
                "P2pGoodsReceipt": "P2P采购收货单",
                "P2pVendorBill": "P2P应付账单",
            },
        },
    }
    p2p_fragment = DeclarativeBlueprint.model_validate(p2p_payload)
    composed = compose_blueprints(
        [core_bp, p2p_fragment],
        model_id="std_p2p_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="基于 std_core 扩展的通用标准 P2P（采购到付款）核心领域本体。",
    )
    composed.imports = ["std_core_ontology"]
    return composed
