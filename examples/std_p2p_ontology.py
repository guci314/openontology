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

from open_ontology.induction.dsl import DeclarativeBlueprint, export_spec_blueprint
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


@spec.edit_function(
    "confirm_p2p_order",
    ConfirmP2pOrderParams,
    touched_object_types=("P2pPurchaseOrder", "P2pSupplierProfile"),
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
    touched_object_types=("P2pGoodsReceipt", "P2pPurchaseOrder"),
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
    touched_object_types=("P2pVendorBill", "P2pPurchaseOrder"),
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
    touched_object_types=("P2pPurchaseOrder", "P2pGoodsReceipt", "WorkCard"),
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
    touched_object_types=("P2pVendorBill", "P2pPurchaseOrder"),
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
    touched_object_types=("P2pVendorBill",),
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
