"""Step 3: Scoped OSDK Views + O2C x P2P Incremental Value-Stream Grafting onto ONE Unified ERP Core.

Demonstrates all Section 4.3 architectural principles on ``BUSINESS_SEED_ONTOLOGY``:
1. **Single Unified Decision Domain + AccountingEntity Anchor**:
   Crystallize ``unified_erp_core`` v1.0.0 (Order-to-Cash) via 5-gate ``SymbolicCrucible``,
   then incrementally graft Procure-to-Pay (v2.0.0) onto the SAME model via ``EvolutionPatch``
   (``add_object_types``, ``add_relations``, ``add_actions``, ``add_views``) — zero dual-spec split.
2. **Party + Role Anti-God-Object Pattern**:
   Shared counterparty ``BusinessParty`` ("Acme Corporation" acting as both Customer & Supplier)
   stays strictly <= 6 core identity fields, while commercial credit lives in
   ``CustomerCommercialProfile`` and procurement terms live in ``SupplierProcurementProfile``.
3. **BalanceLedger + EventVoucher Conservation**:
   ``WarehouseStockBalance`` declares ``voucher_delta_rules`` binding ``on_hand_qty`` to
   immutable signed ``InventoryMovementVoucher.qty_delta`` (+15 inbound receipt / -10 shipment).
4. **Counterfactual Branch Simulation Across Value Streams (``fork()``)**:
   Confirmed ``CustomerSalesOrder`` (qty=10) blocks on ``INSUFFICIENT_WAREHOUSE_STOCK`` (on_hand=2).
   Inside ``rt.fork()``, a 3-step sequence (``approve_purchase_order`` -> ``post_inbound_goods_receipt`` +15
   -> ``fulfill_sales_shipment`` -10) yields ending stock ``7.0`` with parent runtime untouched at ``2.0``.
5. **Scoped OSDK Views + Complexity Bound (< 60%) + Runtime Isolation**:
   ``o2c_sales_ops`` and ``p2p_buyer_ops`` both stay < 60% of full v2.0.0 schema tokens while
   ``AIPAgentExecutor(..., enforce_view_scope=True)`` rejects out-of-view reads/actions.
6. **Live Odoo 19 Verification**:
   When ``offline=False``, clones ``erp_north`` via ``EphemeralOdooSandbox``, projects real draft
   ``purchase.order`` records, and settles ``approve_purchase_order`` -> ``button_confirm``
   (``draft`` -> ``purchase``) with homomorphic readback and zero leak.
"""

from __future__ import annotations

from typing import Any

from open_ontology import (
    Actor,
    InvariantError,
    OntologyRuntime,
    OntologySpec,
    PermissionDeniedError,
    SourceBridge,
)
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EvolutionPatch,
    FieldSpec,
    FrictionClusterSummary,
    FrictionEvidence,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    OntologyEvolverAgent,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    ScopedViewBlueprint,
    SymbolicCrucible,
    compile_blueprint,
    measure_agent_complexity,
)
from open_ontology.osdk import AIPAgentExecutor

from .business_ontology import (
    WorkCard,
    build_business_seed_ontology,
    register_work_card_primitives,
    settle_work_card,
)
from .odoo_dual_erp_demo import DB_NORTH
from .odoo_fde_autonomous_pipeline import AI_OPS_ACTOR, STEWARD_OPS_ACTOR
from .odoo_fde_prober import EphemeralOdooSandbox

O2C_OPS_ACTOR = Actor(actor_id="agent:o2c-sales", role="o2c_agent", clearance_tags=frozenset({"*"}))
P2P_OPS_ACTOR = Actor(actor_id="agent:p2p-buyer", role="p2p_agent", clearance_tags=frozenset({"*"}))
WAREHOUSE_OPS_ACTOR = Actor(actor_id="agent:warehouse", role="warehouse_agent", clearance_tags=frozenset({"*"}))


# ---------------------------------------------------------------------------
# 1. Phase 1: Pure O2C Genesis Blueprint (v1.0.0)
# ---------------------------------------------------------------------------
def build_o2c_v1_blueprint() -> DeclarativeBlueprint:
    """Build v1.0.0 O2C core blueprint anchored on AccountingEntity and lean BusinessParty."""
    return DeclarativeBlueprint(
        model_id="unified_erp_core",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="Unified ERP Core v1.0.0 (Order-to-Cash) grounded on AccountingEntity and Party+Role separation.",
        object_types=[
            ObjectTypeBlueprint(
                name="AccountingEntity",
                archetype="MasterEntity",
                primary_key="entity_id",
                title_field="name",
                view_tags=["finance"],
                fields=[
                    FieldSpec(name="entity_id", field_type="str", required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(name="base_currency", field_type="str", default="USD"),
                    FieldSpec(name="status", field_type="str", default="active"),
                ],
                transitions={"active": ["inactive"], "inactive": ["active"]},
            ),
            ObjectTypeBlueprint(
                name="BusinessParty",
                archetype="MasterEntity",
                primary_key="party_id",
                title_field="legal_name",
                view_tags=["o2c", "p2p"],
                fields=[
                    FieldSpec(name="party_id", field_type="str", required=True),
                    FieldSpec(name="legal_name", field_type="str", required=True),
                    FieldSpec(name="tax_id", field_type="str", default=""),
                    FieldSpec(name="country_code", field_type="str", default="CN"),
                    FieldSpec(name="status", field_type="str", default="active"),
                ],
                transitions={"active": ["suspended"], "suspended": ["active"]},
            ),
            ObjectTypeBlueprint(
                name="CustomerCommercialProfile",
                archetype="MasterEntity",
                primary_key="profile_id",
                title_field="profile_id",
                view_tags=["o2c"],
                fields=[
                    FieldSpec(name="profile_id", field_type="str", required=True),
                    FieldSpec(name="credit_limit", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="payment_terms", field_type="str", default="NET30"),
                    FieldSpec(name="status", field_type="str", default="active"),
                ],
                relations=[
                    RelationSpec(field="party", target_type="BusinessParty", required=True),
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                ],
                transitions={"active": ["hold"], "hold": ["active"]},
            ),
            ObjectTypeBlueprint(
                name="ProductCatalogItem",
                archetype="MasterEntity",
                primary_key="product_id",
                title_field="name",
                view_tags=["replenishment"],
                fields=[
                    FieldSpec(name="product_id", field_type="str", required=True),
                    FieldSpec(name="sku", field_type="str", required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(name="unit_price", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="standard_cost", field_type="float", default=0.0, non_negative=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="WarehouseStockBalance",
                archetype="BalanceLedger",
                primary_key="balance_id",
                title_field="balance_id",
                view_tags=["o2c", "replenishment"],
                fields=[
                    FieldSpec(name="balance_id", field_type="str", required=True),
                    FieldSpec(name="warehouse_code", field_type="str", required=True),
                    FieldSpec(name="on_hand_qty", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="reserved_qty", field_type="float", default=0.0, non_negative=True),
                ],
                relations=[
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                    RelationSpec(field="product", target_type="ProductCatalogItem", required=True),
                ],
                voucher_delta_rules={"on_hand_qty": ("InventoryMovementVoucher", "balance", "qty_delta")},
            ),
            ObjectTypeBlueprint(
                name="CustomerSalesOrder",
                archetype="ProcessDocument",
                primary_key="order_id",
                title_field="order_number",
                status_field="status",
                view_tags=["o2c", "replenishment"],
                fields=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="order_number", field_type="str", required=True),
                    FieldSpec(name="ordered_qty", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="amount_total", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                    RelationSpec(field="customer_profile", target_type="CustomerCommercialProfile", required=True),
                    RelationSpec(field="product", target_type="ProductCatalogItem", required=True),
                    RelationSpec(field="stock_balance", target_type="WarehouseStockBalance", required=True),
                ],
                transitions={
                    "draft": ["confirmed", "cancelled"],
                    "confirmed": ["fulfilled", "cancelled"],
                    "fulfilled": [],
                    "cancelled": [],
                },
            ),
            ObjectTypeBlueprint(
                name="InventoryMovementVoucher",
                archetype="EventVoucher",
                primary_key="voucher_id",
                title_field="voucher_id",
                view_tags=["o2c", "replenishment"],
                fields=[
                    FieldSpec(name="voucher_id", field_type="str", required=True),
                    FieldSpec(name="direction", field_type="str", required=True),
                    FieldSpec(name="qty_delta", field_type="float", required=True),
                    FieldSpec(name="reference_doc", field_type="str", default=""),
                ],
                relations=[
                    RelationSpec(field="balance", target_type="WarehouseStockBalance", required=True),
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="confirm_customer_order",
                description="Confirm a draft CustomerSalesOrder with customer credit-limit approval gating.",
                allowed_roles=["o2c_agent", "ai_agent", "steward", "admin"],
                affects_source=True,
                view_tags=["o2c"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="order_draft_for_confirm",
                        code="ORDER_NOT_DRAFT",
                        target_type="CustomerSalesOrder",
                        id_param="order_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    ),
                    InvariantRuleSpec(
                        name="order_credit_limit_requires_steward",
                        code="APPROVAL_REQUIRED_CREDIT_LIMIT_EXCEEDED",
                        target_type="CustomerSalesOrder",
                        id_param="order_id",
                        when={
                            "op": "and",
                            "args": [
                                {"op": "gt", "left": {"ref": "obj.customer_profile.credit_limit"}, "right": {"lit": 0}},
                                {
                                    "op": "gt",
                                    "left": {"ref": "obj.amount_total"},
                                    "right": {"ref": "obj.customer_profile.credit_limit"},
                                },
                            ],
                        },
                        must_hold={"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["steward", "admin"]}},
                        description="Over-credit order confirmation requires Steward/Admin approval via M1 WorkCard.",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="CustomerSalesOrder",
                        id_param="order_id",
                        set_fields={"status": {"lit": "confirmed"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="fulfill_sales_shipment",
                description="Ship confirmed sales order and record matching outbound EventVoucher + BalanceLedger deduction.",
                allowed_roles=["o2c_agent", "warehouse_agent", "ai_agent", "steward", "admin"],
                affects_source=True,
                view_tags=["o2c", "replenishment"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="voucher_id", field_type="str", required=True),
                    FieldSpec(name="balance_id", field_type="str", required=True),
                    FieldSpec(name="entity_id", field_type="str", required=True),
                    FieldSpec(name="ship_qty", field_type="float", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="shipment_order_confirmed",
                        code="ORDER_NOT_CONFIRMED",
                        target_type="CustomerSalesOrder",
                        id_param="order_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "confirmed"}},
                    ),
                    InvariantRuleSpec(
                        name="shipment_matches_order_stock_and_qty",
                        code="INSUFFICIENT_WAREHOUSE_STOCK",
                        target_type="CustomerSalesOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "param.balance_id"}, "right": {"ref": "obj.stock_balance"}},
                                {"op": "eq", "left": {"ref": "param.entity_id"}, "right": {"ref": "obj.entity"}},
                                {"op": "eq", "left": {"ref": "param.ship_qty"}, "right": {"ref": "obj.ordered_qty"}},
                                {"op": "gt", "left": {"ref": "param.ship_qty"}, "right": {"lit": 0.0}},
                                {
                                    "op": "ge",
                                    "left": {"ref": "obj.stock_balance.on_hand_qty"},
                                    "right": {"ref": "obj.ordered_qty"},
                                },
                            ],
                        },
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="WarehouseStockBalance",
                        target_ref="obj.stock_balance",
                        set_fields={
                            "on_hand_qty": {
                                "op": "sub",
                                "left": {"ref": "obj.on_hand_qty"},
                                "right": {"ref": "param.ship_qty"},
                            }
                        },
                    ),
                    ActionMutationSpec(
                        op="create",
                        object_type="InventoryMovementVoucher",
                        id_param="voucher_id",
                        set_fields={
                            "voucher_id": {"ref": "param.voucher_id"},
                            "balance": {"ref": "param.balance_id"},
                            "entity": {"ref": "param.entity_id"},
                            "direction": {"lit": "OUTBOUND"},
                            "qty_delta": {"op": "sub", "left": {"lit": 0.0}, "right": {"ref": "param.ship_qty"}},
                            "reference_doc": {"ref": "param.order_id"},
                        },
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="CustomerSalesOrder",
                        id_param="order_id",
                        set_fields={"status": {"lit": "fulfilled"}},
                    ),
                ],
            ),
        ],
        views=[
            ScopedViewBlueprint(
                name="o2c_sales_ops",
                description="Scoped O2C projection exposing customer commercial orders & inventory availability without P2P surface.",
                roles=["o2c_agent", "ai_agent", "steward", "admin"],
                view_tags=["o2c"],
                object_types=[
                    "BusinessParty",
                    "CustomerCommercialProfile",
                    "WarehouseStockBalance",
                    "CustomerSalesOrder",
                    "InventoryMovementVoucher",
                ],
                actions=["confirm_customer_order", "fulfill_sales_shipment"],
                include_reachable_objects=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 2. Phase 2: P2P Incremental Grafting Patch (v1.0.0 -> v2.0.0)
# ---------------------------------------------------------------------------
def build_p2p_grafting_patch() -> EvolutionPatch:
    """Build EvolutionPatch grafting P2P onto unified_erp_core v1.0.0 -> v2.0.0."""
    return EvolutionPatch(
        patch_id="PATCH-GRAFT-P2P-V2",
        base_version="1.0.0",
        target_version="2.0.0",
        rationale=(
            "Incrementally graft Procure-to-Pay (SupplierProcurementProfile, PurchaseOrder, "
            "InboundGoodsReceipt, SupplierBill) onto unified_erp_core reusing shared BusinessParty, "
            "AccountingEntity, and WarehouseStockBalance + EventVoucher conservation."
        ),
        add_object_types=[
            ObjectTypeBlueprint(
                name="SupplierProcurementProfile",
                archetype="MasterEntity",
                primary_key="supplier_profile_id",
                title_field="supplier_profile_id",
                view_tags=["p2p"],
                fields=[
                    FieldSpec(name="supplier_profile_id", field_type="str", required=True),
                    FieldSpec(name="lead_time_days", field_type="int", default=5, non_negative=True),
                    FieldSpec(name="approved_spend_limit", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="active"),
                ],
                relations=[
                    RelationSpec(field="party", target_type="BusinessParty", required=True),
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                ],
                transitions={"active": ["blocked"], "blocked": ["active"]},
            ),
            ObjectTypeBlueprint(
                name="PurchaseOrder",
                archetype="ProcessDocument",
                primary_key="po_id",
                title_field="po_number",
                status_field="status",
                view_tags=["p2p", "replenishment"],
                fields=[
                    FieldSpec(name="po_id", field_type="str", required=True),
                    FieldSpec(name="po_number", field_type="str", required=True),
                    FieldSpec(name="ordered_qty", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="amount_total", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                    RelationSpec(field="supplier_profile", target_type="SupplierProcurementProfile", required=True),
                    RelationSpec(field="product", target_type="ProductCatalogItem", required=True),
                    RelationSpec(field="target_balance", target_type="WarehouseStockBalance", required=True),
                ],
                transitions={
                    "draft": ["approved", "cancelled"],
                    "approved": ["received", "cancelled"],
                    "received": ["billed"],
                    "billed": [],
                    "cancelled": [],
                },
            ),
            ObjectTypeBlueprint(
                name="InboundGoodsReceipt",
                archetype="ProcessDocument",
                primary_key="receipt_id",
                title_field="receipt_id",
                status_field="status",
                view_tags=["p2p_buyer", "replenishment"],
                fields=[
                    FieldSpec(name="receipt_id", field_type="str", required=True),
                    FieldSpec(name="received_qty", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(field="purchase_order", target_type="PurchaseOrder", required=True),
                    RelationSpec(field="balance", target_type="WarehouseStockBalance", required=True),
                ],
                transitions={"draft": ["posted"], "posted": []},
            ),
            ObjectTypeBlueprint(
                name="SupplierBill",
                archetype="ProcessDocument",
                primary_key="bill_id",
                title_field="bill_id",
                status_field="status",
                view_tags=["p2p"],
                fields=[
                    FieldSpec(name="bill_id", field_type="str", required=True),
                    FieldSpec(name="amount_total", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(field="purchase_order", target_type="PurchaseOrder", required=True),
                    RelationSpec(field="entity", target_type="AccountingEntity", required=True),
                ],
                transitions={"draft": ["posted", "paid"], "posted": ["paid"], "paid": []},
            ),
        ],
        add_relations={
            "ProductCatalogItem": [
                RelationSpec(field="preferred_supplier", target_type="SupplierProcurementProfile", required=False)
            ]
        },
        add_actions=[
            ActionBlueprint(
                name="approve_purchase_order",
                description="Approve a draft PurchaseOrder subject to supplier spend-limit gating.",
                allowed_roles=["p2p_agent", "warehouse_agent", "ai_agent", "steward", "admin"],
                affects_source=True,
                view_tags=["p2p", "p2p_buyer", "replenishment"],
                params=[FieldSpec(name="po_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="po_draft_for_approve",
                        code="PO_NOT_DRAFT",
                        target_type="PurchaseOrder",
                        id_param="po_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    ),
                    InvariantRuleSpec(
                        name="po_spend_limit_requires_steward",
                        code="APPROVAL_REQUIRED_SPEND_LIMIT_EXCEEDED",
                        target_type="PurchaseOrder",
                        id_param="po_id",
                        when={
                            "op": "and",
                            "args": [
                                {
                                    "op": "gt",
                                    "left": {"ref": "obj.supplier_profile.approved_spend_limit"},
                                    "right": {"lit": 0},
                                },
                                {
                                    "op": "gt",
                                    "left": {"ref": "obj.amount_total"},
                                    "right": {"ref": "obj.supplier_profile.approved_spend_limit"},
                                },
                            ],
                        },
                        must_hold={"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["steward", "admin"]}},
                        description="Over-spend-limit PurchaseOrder requires Steward/Admin approval via M1 WorkCard.",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="PurchaseOrder",
                        id_param="po_id",
                        set_fields={"status": {"lit": "approved"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="post_inbound_goods_receipt",
                description="Post inbound warehouse receipt incrementing BalanceLedger + creating INBOUND EventVoucher.",
                allowed_roles=["p2p_agent", "warehouse_agent", "ai_agent", "steward", "admin"],
                affects_source=True,
                view_tags=["p2p", "replenishment"],
                params=[
                    FieldSpec(name="po_id", field_type="str", required=True),
                    FieldSpec(name="receipt_id", field_type="str", required=True),
                    FieldSpec(name="voucher_id", field_type="str", required=True),
                    FieldSpec(name="balance_id", field_type="str", required=True),
                    FieldSpec(name="entity_id", field_type="str", required=True),
                    FieldSpec(name="received_qty", field_type="float", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="po_approved_for_receipt",
                        code="PO_NOT_APPROVED",
                        target_type="PurchaseOrder",
                        id_param="po_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "approved"}},
                    ),
                    InvariantRuleSpec(
                        name="receipt_matches_po_balance_and_qty",
                        code="INVALID_RECEIPT_PARAMS",
                        target_type="PurchaseOrder",
                        id_param="po_id",
                        must_hold={
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "param.balance_id"}, "right": {"ref": "obj.target_balance"}},
                                {"op": "eq", "left": {"ref": "param.entity_id"}, "right": {"ref": "obj.entity"}},
                                {"op": "eq", "left": {"ref": "param.received_qty"}, "right": {"ref": "obj.ordered_qty"}},
                                {"op": "gt", "left": {"ref": "param.received_qty"}, "right": {"lit": 0.0}},
                            ],
                        },
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="InboundGoodsReceipt",
                        id_param="receipt_id",
                        set_fields={
                            "receipt_id": {"ref": "param.receipt_id"},
                            "purchase_order": {"ref": "param.po_id"},
                            "balance": {"ref": "param.balance_id"},
                            "received_qty": {"ref": "param.received_qty"},
                            "status": {"lit": "posted"},
                        },
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="WarehouseStockBalance",
                        target_ref="obj.target_balance",
                        set_fields={
                            "on_hand_qty": {
                                "op": "add",
                                "left": {"ref": "obj.on_hand_qty"},
                                "right": {"ref": "param.received_qty"},
                            }
                        },
                    ),
                    ActionMutationSpec(
                        op="create",
                        object_type="InventoryMovementVoucher",
                        id_param="voucher_id",
                        set_fields={
                            "voucher_id": {"ref": "param.voucher_id"},
                            "balance": {"ref": "param.balance_id"},
                            "entity": {"ref": "param.entity_id"},
                            "direction": {"lit": "INBOUND"},
                            "qty_delta": {"ref": "param.received_qty"},
                            "reference_doc": {"ref": "param.receipt_id"},
                        },
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="PurchaseOrder",
                        id_param="po_id",
                        set_fields={"status": {"lit": "received"}},
                    ),
                ],
            ),
            ActionBlueprint(
                name="post_supplier_bill",
                description="Post SupplierBill only after matching PurchaseOrder goods receipt is complete (3-way match).",
                allowed_roles=["p2p_agent", "ai_agent", "steward", "admin"],
                affects_source=True,
                view_tags=["p2p"],
                params=[FieldSpec(name="bill_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="bill_draft_status",
                        code="BILL_NOT_DRAFT",
                        target_type="SupplierBill",
                        id_param="bill_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    ),
                    InvariantRuleSpec(
                        name="three_way_match_requires_received_po",
                        code="PO_GOODS_NOT_RECEIVED_FOR_BILL",
                        target_type="SupplierBill",
                        id_param="bill_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.purchase_order.status"},
                            "right": {"lit": "received"},
                        },
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SupplierBill",
                        id_param="bill_id",
                        set_fields={"status": {"lit": "posted"}},
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="PurchaseOrder",
                        target_ref="obj.purchase_order",
                        set_fields={"status": {"lit": "billed"}},
                    ),
                ],
            ),
        ],
        add_views=[
            ScopedViewBlueprint(
                name="p2p_buyer_ops",
                description="Scoped P2P buyer projection covering supplier profiles, POs, receipts, and bills without O2C surface.",
                roles=["p2p_agent", "ai_agent", "steward", "admin"],
                view_tags=["p2p_buyer"],
                object_types=[
                    "BusinessParty",
                    "SupplierProcurementProfile",
                    "PurchaseOrder",
                    "InboundGoodsReceipt",
                    "SupplierBill",
                ],
                actions=["approve_purchase_order", "post_supplier_bill"],
                include_reachable_objects=False,
            ),
            ScopedViewBlueprint(
                name="warehouse_replenishment_ops",
                description="Cross-value-stream warehouse planner projection linking inbound PO receipts and outbound sales shipments.",
                roles=["warehouse_agent", "ai_agent", "steward", "admin"],
                view_tags=["replenishment"],
                object_types=[
                    "ProductCatalogItem",
                    "WarehouseStockBalance",
                    "PurchaseOrder",
                    "InboundGoodsReceipt",
                    "CustomerSalesOrder",
                    "InventoryMovementVoucher",
                ],
                actions=["approve_purchase_order", "post_inbound_goods_receipt", "fulfill_sales_shipment"],
                include_reachable_objects=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 3. Governance Crystallization + End-to-End Grafting Demo
# ---------------------------------------------------------------------------
def _base_m0_fixtures() -> list[dict[str, Any]]:
    return [
        {
            "object_type": "AccountingEntity",
            "data": {"entity_id": "ENT-NORTH", "name": "North Legal Entity", "base_currency": "USD", "status": "active"},
        },
        {
            "object_type": "BusinessParty",
            "data": {
                "party_id": "PARTY-ACME",
                "legal_name": "Acme Corporation",
                "tax_id": "US-99-ACME",
                "country_code": "US",
                "status": "active",
            },
        },
        {
            "object_type": "CustomerCommercialProfile",
            "data": {
                "profile_id": "CUST-PROF-ACME",
                "party": "PARTY-ACME",
                "entity": "ENT-NORTH",
                "credit_limit": 50_000.0,
                "payment_terms": "NET30",
                "status": "active",
            },
        },
        {
            "object_type": "ProductCatalogItem",
            "data": {
                "product_id": "PROD-WIDGET",
                "sku": "WGT-100",
                "name": "Industrial Valve Widget",
                "unit_price": 1_000.0,
                "standard_cost": 577.2,
            },
        },
        {
            "object_type": "WarehouseStockBalance",
            "data": {
                "balance_id": "BAL-MAIN-WIDGET",
                "warehouse_code": "WH-MAIN",
                "entity": "ENT-NORTH",
                "product": "PROD-WIDGET",
                "on_hand_qty": 2.0,
                "reserved_qty": 0.0,
            },
        },
        {
            "object_type": "CustomerSalesOrder",
            "data": {
                "order_id": "SO-SHORT-1",
                "order_number": "SO-2026-001",
                "entity": "ENT-NORTH",
                "customer_profile": "CUST-PROF-ACME",
                "product": "PROD-WIDGET",
                "stock_balance": "BAL-MAIN-WIDGET",
                "ordered_qty": 10.0,
                "amount_total": 10_000.0,
                "status": "confirmed",
            },
        },
    ]


def crystallize_and_graft_o2c_p2p() -> tuple[DeclarativeBlueprint, DeclarativeBlueprint, OntologySpec]:
    """Crystallize v1.0.0 O2C then graft v2.0.0 P2P via EvolutionPatch + 5-gate Crucible."""
    seed = build_business_seed_ontology()
    v1_bp = build_o2c_v1_blueprint()
    p2p_patch = build_p2p_grafting_patch()
    base_m0 = _base_m0_fixtures()

    with InductionGovernanceService(db_path=":memory:") as gov:
        # Phase 1: v1.0.0 O2C Genesis
        builder = OntologyBuilderAgent(
            backend=CallableAbductionBackend(
                lambda _s, _u, _r: {
                    "rationale": "unified_erp_core v1.0.0 O2C genesis",
                    "blueprint": v1_bp.model_dump(mode="json"),
                    "proposed_eval_cases": [],
                }
            ),
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=seed,
        )
        gen_m0 = list(base_m0) + [
            {
                "object_type": "WarehouseStockBalance",
                "data": {
                    "balance_id": "BAL-ADEQUATE",
                    "warehouse_code": "WH-OK",
                    "entity": "ENT-NORTH",
                    "product": "PROD-WIDGET",
                    "on_hand_qty": 25.0,
                    "reserved_qty": 0.0,
                },
            },
            {
                "object_type": "CustomerSalesOrder",
                "data": {
                    "order_id": "SO-DRAFT-OK",
                    "order_number": "SO-DRAFT-1",
                    "entity": "ENT-NORTH",
                    "customer_profile": "CUST-PROF-ACME",
                    "product": "PROD-WIDGET",
                    "stock_balance": "BAL-ADEQUATE",
                    "ordered_qty": 5.0,
                    "amount_total": 5_000.0,
                    "status": "draft",
                },
            },
            {
                "object_type": "CustomerSalesOrder",
                "data": {
                    "order_id": "SO-CONF-OK",
                    "order_number": "SO-CONF-1",
                    "entity": "ENT-NORTH",
                    "customer_profile": "CUST-PROF-ACME",
                    "product": "PROD-WIDGET",
                    "stock_balance": "BAL-ADEQUATE",
                    "ordered_qty": 5.0,
                    "amount_total": 5_000.0,
                    "status": "confirmed",
                },
            },
        ]
        gen_cycle = builder.run_genesis_cycle(
            model_id="unified_erp_core",
            phenomenon_brief={"stream": "order_to_cash_v1"},
            crucible_context=CrucibleContext(
                mode="genesis",
                m0_objects=gen_m0,
                positive_events=[
                    ReplayEvent(
                        name="O2C-POS-CONFIRM",
                        action="confirm_customer_order",
                        params={"order_id": "SO-DRAFT-OK"},
                        actor=O2C_OPS_ACTOR,
                        expected_valid=True,
                    ),
                    ReplayEvent(
                        name="O2C-POS-SHIP-VOUCHER",
                        action="fulfill_sales_shipment",
                        params={
                            "order_id": "SO-CONF-OK",
                            "voucher_id": "VCH-GEN-1",
                            "balance_id": "BAL-ADEQUATE",
                            "entity_id": "ENT-NORTH",
                            "ship_qty": 5.0,
                        },
                        actor=O2C_OPS_ACTOR,
                        expected_valid=True,
                    ),
                ],
                negative_events=[
                    ReplayEvent(
                        name="O2C-NEG-STOCK-SHORTAGE",
                        action="fulfill_sales_shipment",
                        params={
                            "order_id": "SO-SHORT-1",
                            "voucher_id": "VCH-GEN-FAIL",
                            "balance_id": "BAL-MAIN-WIDGET",
                            "entity_id": "ENT-NORTH",
                            "ship_qty": 10.0,
                        },
                        actor=O2C_OPS_ACTOR,
                        expected_valid=False,
                        expected_error_code="INSUFFICIENT_WAREHOUSE_STOCK",
                    ),
                ],
                default_actor=O2C_OPS_ACTOR,
                seed_ontology=seed,
            ),
            seed_ontology=seed,
            max_rounds=2,
        )
        assert gen_cycle.verified and gen_cycle.pending_approval_run_id is not None, (
            f"O2C v1.0.0 failed Crucible:\n{gen_cycle.final_report.diagnostic_feedback()}"
        )
        gov.approve_crystallization(gen_cycle.pending_approval_run_id, STEWARD_ACTOR)
        active_v1 = gov.get_active_blueprint("unified_erp_core")
        assert active_v1 is not None and active_v1.version == "1.0.0"

        # Phase 2: Incrementally graft P2P v2.0.0 onto SAME unified_erp_core model via EvolutionPatch
        evolver = OntologyEvolverAgent(
            backend=CallableAbductionBackend(
                lambda _s, _u, _r: {"patch": p2p_patch.model_dump(mode="json")}
            ),
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=seed,
        )
        evo_m0 = list(gen_m0) + [
            {
                "object_type": "SupplierProcurementProfile",
                "data": {
                    "supplier_profile_id": "SUPP-PROF-ACME",
                    "party": "PARTY-ACME",
                    "entity": "ENT-NORTH",
                    "lead_time_days": 3,
                    "approved_spend_limit": 20_000.0,
                    "status": "active",
                },
            },
            {
                "object_type": "PurchaseOrder",
                "data": {
                    "po_id": "PO-EVO-DRAFT",
                    "po_number": "PO-EVO-1",
                    "entity": "ENT-NORTH",
                    "supplier_profile": "SUPP-PROF-ACME",
                    "product": "PROD-WIDGET",
                    "target_balance": "BAL-MAIN-WIDGET",
                    "ordered_qty": 15.0,
                    "amount_total": 8_658.0,
                    "status": "draft",
                },
            },
            {
                "object_type": "PurchaseOrder",
                "data": {
                    "po_id": "PO-EVO-APPROVED",
                    "po_number": "PO-EVO-2",
                    "entity": "ENT-NORTH",
                    "supplier_profile": "SUPP-PROF-ACME",
                    "product": "PROD-WIDGET",
                    "target_balance": "BAL-MAIN-WIDGET",
                    "ordered_qty": 15.0,
                    "amount_total": 8_658.0,
                    "status": "approved",
                },
            },
            {
                "object_type": "PurchaseOrder",
                "data": {
                    "po_id": "PO-EVO-RECEIVED",
                    "po_number": "PO-EVO-3",
                    "entity": "ENT-NORTH",
                    "supplier_profile": "SUPP-PROF-ACME",
                    "product": "PROD-WIDGET",
                    "target_balance": "BAL-MAIN-WIDGET",
                    "ordered_qty": 15.0,
                    "amount_total": 8_658.0,
                    "status": "received",
                },
            },
            {
                "object_type": "SupplierBill",
                "data": {
                    "bill_id": "BILL-EVO-UNRECV",
                    "purchase_order": "PO-EVO-APPROVED",
                    "entity": "ENT-NORTH",
                    "amount_total": 8_658.0,
                    "status": "draft",
                },
            },
            {
                "object_type": "SupplierBill",
                "data": {
                    "bill_id": "BILL-EVO-RECV",
                    "purchase_order": "PO-EVO-RECEIVED",
                    "entity": "ENT-NORTH",
                    "amount_total": 8_658.0,
                    "status": "draft",
                },
            },
        ]
        cluster = FrictionClusterSummary(
            cluster_id="ERP-P2P-GRAFT-V2",
            signal_kind="MISSING_VALUE_STREAM_EXPANSION",
            action_type="fulfill_sales_shipment",
            signature="stock_shortage_requires_p2p_replenishment",
            support_count=1,
            run_ids=("GRAFT-REQ-1",),
            escalated=True,
            evidences=(
                FrictionEvidence(
                    run_id="GRAFT-REQ-1",
                    signal_kind="NL_FEATURE_REQUEST",
                    action_type="fulfill_sales_shipment",
                    actor_role="warehouse_agent",
                    signature="stock_shortage_requires_p2p_replenishment",
                    params={},
                    summary=(
                        "Extend unified_erp_core with Procure-to-Pay supplier procurement profiles, "
                        "purchase orders, inbound warehouse receipts (+qty vouchers), and 3-way matched supplier bills."
                    ),
                ),
            ),
        )
        evo_cycle = evolver.run_evolution_cycle(
            model_id="unified_erp_core",
            cluster=cluster,
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=evo_m0,
                positive_events=[
                    ReplayEvent(
                        name="GRAFT-POS-O2C-CONFIRM",
                        action="confirm_customer_order",
                        params={"order_id": "SO-DRAFT-OK"},
                        actor=O2C_OPS_ACTOR,
                        expected_valid=True,
                    ),
                    ReplayEvent(
                        name="GRAFT-POS-P2P-APPROVE",
                        action="approve_purchase_order",
                        params={"po_id": "PO-EVO-DRAFT"},
                        actor=P2P_OPS_ACTOR,
                        expected_valid=True,
                    ),
                    ReplayEvent(
                        name="GRAFT-POS-P2P-RECEIPT",
                        action="post_inbound_goods_receipt",
                        params={
                            "po_id": "PO-EVO-APPROVED",
                            "receipt_id": "GR-EVO-1",
                            "voucher_id": "VCH-IN-EVO-1",
                            "balance_id": "BAL-MAIN-WIDGET",
                            "entity_id": "ENT-NORTH",
                            "received_qty": 15.0,
                        },
                        actor=P2P_OPS_ACTOR,
                        expected_valid=True,
                    ),
                    ReplayEvent(
                        name="GRAFT-POS-P2P-BILL",
                        action="post_supplier_bill",
                        params={"bill_id": "BILL-EVO-RECV"},
                        actor=P2P_OPS_ACTOR,
                        expected_valid=True,
                    ),
                ],
                negative_events=[
                    ReplayEvent(
                        name="GRAFT-NEG-3WAY-UNRECEIVED-BILL",
                        action="post_supplier_bill",
                        params={"bill_id": "BILL-EVO-UNRECV"},
                        actor=P2P_OPS_ACTOR,
                        expected_valid=False,
                        expected_error_code="PO_GOODS_NOT_RECEIVED_FOR_BILL",
                    ),
                ],
                default_actor=AI_OPS_ACTOR,
                seed_ontology=seed,
                max_patch_rules=16,
            ),
            max_rounds=2,
        )
        assert evo_cycle.verified and evo_cycle.pending_approval_run_id is not None, (
            f"P2P v2.0.0 grafting failed Crucible:\n{evo_cycle.final_report.diagnostic_feedback()}"
        )
        gov.approve_crystallization(evo_cycle.pending_approval_run_id, STEWARD_ACTOR)
        active_v2 = gov.get_active_blueprint("unified_erp_core")
        assert active_v2 is not None and active_v2.version == "2.0.0"

    compiled_v2 = compile_blueprint(active_v2, seed_ontology=seed)
    register_work_card_primitives(
        compiled_v2,
        opener_roles=("o2c_agent", "p2p_agent", "warehouse_agent", "ai_agent", "steward", "admin"),
        resolver_roles=("steward", "admin"),
    )
    return active_v1, active_v2, compiled_v2


def run_o2c_p2p_grafting_demo(
    *,
    offline: bool = True,
    source_db: str = DB_NORTH,
) -> dict[str, Any]:
    """Run full Step 3 proof: O2C->P2P grafting, fork() replenishment, Scoped Views, and live Odoo PO settlement."""
    bp_v1, bp_v2, spec_v2 = crystallize_and_graft_o2c_p2p()

    # Check anti-God-object lean BusinessParty invariant
    party_bp = next(ot for ot in bp_v2.object_types if ot.name == "BusinessParty")
    assert len(party_bp.fields) <= 6

    # Measure full v2.0.0 vs Scoped Views complexity (< 60%)
    full_cx = measure_agent_complexity(
        spec_v2,
        regime_name="unified_v2_full",
        raw_external_entity_count=343,
        raw_external_field_count=8370,
        reference_token_budget=16000,
    )
    o2c_cx = measure_agent_complexity(
        spec_v2,
        regime_name="o2c_sales_ops",
        actor=O2C_OPS_ACTOR,
        view="o2c_sales_ops",
        raw_external_entity_count=343,
        raw_external_field_count=8370,
        reference_token_budget=16000,
    )
    p2p_cx = measure_agent_complexity(
        spec_v2,
        regime_name="p2p_buyer_ops",
        actor=P2P_OPS_ACTOR,
        view="p2p_buyer_ops",
        raw_external_entity_count=343,
        raw_external_field_count=8370,
        reference_token_budget=16000,
    )
    o2c_token_ratio = o2c_cx.osdk_tool_schema_tokens_est / max(1, full_cx.osdk_tool_schema_tokens_est)
    p2p_token_ratio = p2p_cx.osdk_tool_schema_tokens_est / max(1, full_cx.osdk_tool_schema_tokens_est)
    assert o2c_token_ratio < 0.60, f"o2c ratio {o2c_token_ratio:.3f} >= 0.60"
    assert p2p_token_ratio < 0.60, f"p2p ratio {p2p_token_ratio:.3f} >= 0.60"

    # Build runtime & load counterfactual/warehouse fixtures
    rt = OntologyRuntime(spec_v2, db_path=":memory:")
    try:
        for entry in _base_m0_fixtures():
            cls = spec_v2.get_object_type(entry["object_type"])
            rt.put_object(cls(**entry["data"]))

        sup_cls = spec_v2.get_object_type("SupplierProcurementProfile")
        po_cls = spec_v2.get_object_type("PurchaseOrder")
        bill_cls = spec_v2.get_object_type("SupplierBill")
        bal_cls = spec_v2.get_object_type("WarehouseStockBalance")
        so_cls = spec_v2.get_object_type("CustomerSalesOrder")
        vch_cls = spec_v2.get_object_type("InventoryMovementVoucher")

        rt.put_object(
            sup_cls(
                supplier_profile_id="SUPP-PROF-ACME",
                party="PARTY-ACME",
                entity="ENT-NORTH",
                lead_time_days=3,
                approved_spend_limit=20_000.0,
                status="active",
            )
        )
        rt.put_object(
            po_cls(
                po_id="PO-REPL-1",
                po_number="P00005",
                entity="ENT-NORTH",
                supplier_profile="SUPP-PROF-ACME",
                product="PROD-WIDGET",
                target_balance="BAL-MAIN-WIDGET",
                ordered_qty=15.0,
                amount_total=8_658.0,
                status="draft",
            )
        )
        rt.put_object(
            bill_cls(
                bill_id="BILL-REPL-1",
                purchase_order="PO-REPL-1",
                entity="ENT-NORTH",
                amount_total=8_658.0,
                status="draft",
            )
        )

        # Verify Scoped OSDK View runtime isolation via AIPAgentExecutor
        o2c_exec = AIPAgentExecutor(rt, O2C_OPS_ACTOR, view="o2c_sales_ops", enforce_view_scope=True)
        o2c_tbox_names = {ot["name"] for ot in o2c_exec.describe_tbox()["object_types"]}
        o2c_tool_names = {t["function"]["name"] for t in o2c_exec.tools()}
        assert "CustomerSalesOrder" in o2c_tbox_names
        assert "SupplierBill" not in o2c_tbox_names
        assert "PurchaseOrder" not in o2c_tbox_names

        cross_view_obj_blocked = False
        try:
            o2c_exec.dispatch("query_objects", {"object_type": "SupplierBill"})
        except PermissionDeniedError:
            cross_view_obj_blocked = True

        cross_view_action_blocked = False
        try:
            o2c_exec.dispatch("submit_action", {"run_id": "VIEW-BLK-1", "action": "approve_purchase_order", "params": {"po_id": "PO-REPL-1"}})
        except PermissionDeniedError:
            cross_view_action_blocked = True

        assert cross_view_obj_blocked and cross_view_action_blocked

        # Verify stock shortage blocks direct shipment on parent runtime
        shortage_code = ""
        try:
            rt.submit_action(
                "SHRT-1",
                "fulfill_sales_shipment",
                {
                    "order_id": "SO-SHORT-1",
                    "voucher_id": "VCH-FAIL-1",
                    "balance_id": "BAL-MAIN-WIDGET",
                    "entity_id": "ENT-NORTH",
                    "ship_qty": 10.0,
                },
                WAREHOUSE_OPS_ACTOR,
            )
        except InvariantError as exc:
            shortage_code = exc.code
        assert shortage_code == "INSUFFICIENT_WAREHOUSE_STOCK"

        # Fork 3-step replenishment simulation across P2P + O2C without mutating parent runtime
        fork_rt = rt.fork()
        try:
            r_po = fork_rt.submit_action("FK-1-PO", "approve_purchase_order", {"po_id": "PO-REPL-1"}, WAREHOUSE_OPS_ACTOR)
            assert r_po["status"] == "COMMITTED"

            # Check 3-way match rejects premature SupplierBill before goods receipt
            premature_bill_code = ""
            try:
                fork_rt.submit_action("FK-BILL-PRE", "post_supplier_bill", {"bill_id": "BILL-REPL-1"}, P2P_OPS_ACTOR)
            except InvariantError as exc:
                premature_bill_code = exc.code
            assert premature_bill_code == "PO_GOODS_NOT_RECEIVED_FOR_BILL"

            r_gr = fork_rt.submit_action(
                "FK-2-GR",
                "post_inbound_goods_receipt",
                {
                    "po_id": "PO-REPL-1",
                    "receipt_id": "GR-REPL-1",
                    "voucher_id": "VCH-IN-1",
                    "balance_id": "BAL-MAIN-WIDGET",
                    "entity_id": "ENT-NORTH",
                    "received_qty": 15.0,
                },
                WAREHOUSE_OPS_ACTOR,
            )
            assert r_gr["status"] == "COMMITTED"

            r_ship = fork_rt.submit_action(
                "FK-3-SHIP",
                "fulfill_sales_shipment",
                {
                    "order_id": "SO-SHORT-1",
                    "voucher_id": "VCH-OUT-1",
                    "balance_id": "BAL-MAIN-WIDGET",
                    "entity_id": "ENT-NORTH",
                    "ship_qty": 10.0,
                },
                WAREHOUSE_OPS_ACTOR,
            )
            assert r_ship["status"] == "COMMITTED"

            r_bill = fork_rt.submit_action("FK-4-BILL", "post_supplier_bill", {"bill_id": "BILL-REPL-1"}, P2P_OPS_ACTOR)
            assert r_bill["status"] == "COMMITTED"

            fork_bal = float(fork_rt.get_object(bal_cls, "BAL-MAIN-WIDGET", WAREHOUSE_OPS_ACTOR).on_hand_qty)
            fork_so_state = fork_rt.get_object(so_cls, "SO-SHORT-1", WAREHOUSE_OPS_ACTOR).status
            fork_po_state = fork_rt.get_object(po_cls, "PO-REPL-1", P2P_OPS_ACTOR).status
            vouchers = fork_rt.query_objects(vch_cls, WAREHOUSE_OPS_ACTOR)
            voucher_sum = sum(float(v.qty_delta) for v in vouchers)
            assert abs(fork_bal - 7.0) < 1e-6
            assert abs(voucher_sum - 5.0) < 1e-6  # +15 - 10 = +5 net delta added to initial 2.0
            assert fork_so_state == "fulfilled"
            assert fork_po_state == "billed"
        finally:
            fork_rt.close()

        parent_bal = float(rt.get_object(bal_cls, "BAL-MAIN-WIDGET", WAREHOUSE_OPS_ACTOR).on_hand_qty)
        assert abs(parent_bal - 2.0) < 1e-6

        # Over-spend-limit PO approval flows via M1 WorkCard + settle_work_card (zero PENDING_APPROVAL)
        over_fork = rt.fork()
        try:
            over_fork.put_object(
                po_cls(
                    po_id="PO-OVER-LIMIT-1",
                    po_number="P99001",
                    entity="ENT-NORTH",
                    supplier_profile="SUPP-PROF-ACME",
                    product="PROD-WIDGET",
                    target_balance="BAL-MAIN-WIDGET",
                    ordered_qty=50.0,
                    amount_total=35_000.0,
                    status="draft",
                )
            )
            over_err_code = ""
            try:
                over_fork.submit_action("WC-PO-BLK", "approve_purchase_order", {"po_id": "PO-OVER-LIMIT-1"}, P2P_OPS_ACTOR)
                raise AssertionError("expected over-spend-limit PO approval by p2p_agent to fail")
            except InvariantError as exc:
                over_err_code = exc.code or ""
            assert over_err_code == "APPROVAL_REQUIRED_SPEND_LIMIT_EXCEEDED"
            assert over_fork.list_action_runs(status="PENDING_APPROVAL") == []

            wc_open = over_fork.submit_action(
                "WC-PO-OPEN",
                "open_work_card",
                {
                    "card_id": "WC-P2P-OVR-1",
                    "card_kind": "APPROVAL",
                    "target_object_type": "PurchaseOrder",
                    "target_object_id": "PO-OVER-LIMIT-1",
                    "requested_action": "approve_purchase_order",
                    "requested_params_json": '{"po_id": "PO-OVER-LIMIT-1"}',
                    "assignee_role": "steward",
                },
                P2P_OPS_ACTOR,
            )
            assert wc_open["status"] == "COMMITTED"
            wc_settled = settle_work_card(
                over_fork,
                "WC-P2P-OVR-1",
                STEWARD_OPS_ACTOR,
                resolution_note="steward approved over-limit procurement",
            )
            assert wc_settled["status"] == "COMMITTED"
            assert wc_settled["settled_action_run"]["status"] == "COMMITTED"
            assert over_fork.get_object(po_cls, "PO-OVER-LIMIT-1", STEWARD_OPS_ACTOR).status == "approved"
            assert over_fork.get_object(WorkCard, "WC-P2P-OVR-1", STEWARD_OPS_ACTOR).status == "COMPLETED"
            assert over_fork.list_action_runs(status="PENDING_APPROVAL") == []
        finally:
            over_fork.close()

        live_po_result: dict[str, Any] | None = None
        if not offline:
            live_po_result = _settle_live_erp_north_po(spec_v2, source_db=source_db)

        return {
            "mode": "offline-grafting" if offline else "live-erp-north-p2p",
            "v1_version": bp_v1.version,
            "v2_version": bp_v2.version,
            "business_party_field_count": len(party_bp.fields),
            "full_v2_schema_tokens": full_cx.osdk_tool_schema_tokens_est,
            "o2c_scoped_tokens": o2c_cx.osdk_tool_schema_tokens_est,
            "p2p_scoped_tokens": p2p_cx.osdk_tool_schema_tokens_est,
            "o2c_token_ratio": round(o2c_token_ratio, 4),
            "p2p_token_ratio": round(p2p_token_ratio, 4),
            "o2c_tbox_types": sorted(o2c_tbox_names),
            "o2c_tool_count": len(o2c_tool_names),
            "cross_view_obj_blocked": cross_view_obj_blocked,
            "cross_view_action_blocked": cross_view_action_blocked,
            "fork_replenishment": {
                "parent_on_hand_qty": parent_bal,
                "fork_ending_on_hand_qty": 7.0,
                "voucher_net_delta": 5.0,
                "premature_bill_error_code": premature_bill_code,
            },
            "work_card_approval": {
                "error_code": over_err_code,
                "work_card_status": "COMPLETED",
                "settled_status": wc_settled["status"],
                "pending_approval_runs": 0,
            },
            "live_odoo_p2p_settlement": live_po_result,
        }
    finally:
        rt.close()


def _settle_live_erp_north_po(spec_v2: OntologySpec, *, source_db: str) -> dict[str, Any]:
    """Mount live cloned erp_north purchase.order onto coupled unified_erp_core runtime and confirm."""
    with EphemeralOdooSandbox(source_db=source_db) as sandbox_client:
        sandbox_client._ensure_login()
        draft_pos = sandbox_client.search_read(
            "purchase.order",
            [("state", "=", "draft")],
            ["id", "name", "partner_id", "state", "amount_total"],
            limit=5,
            order="id asc",
        )
        assert draft_pos, "expected at least one draft purchase.order in erp_north clone"
        po_row = draft_pos[0]
        ext_po_id = int(po_row["id"])
        raw_partner = po_row["partner_id"]
        partner_name = str(raw_partner[1] if isinstance(raw_partner, (list, tuple)) else "Vendor")
        live_po_id = f"PO-ODOO-{ext_po_id}"

        def _project_po(r: dict[str, Any]) -> dict[str, Any]:
            st_raw = str(r.get("state") or "draft")
            mapped_st = "approved" if st_raw in ("purchase", "done") else ("cancelled" if st_raw == "cancel" else "draft")
            return {
                "po_id": live_po_id,
                "po_number": str(r["name"]),
                "entity": "ENT-NORTH",
                "supplier_profile": "SUPP-PROF-LIVE",
                "product": "PROD-WIDGET",
                "target_balance": "BAL-MAIN-WIDGET",
                "ordered_qty": 15.0,
                "amount_total": float(r.get("amount_total") or 0.0),
                "status": mapped_st,
            }

        def _actuate(move: dict[str, Any]) -> Any:
            if str(move["action_type"]) != "approve_purchase_order":
                raise ValueError(f"unexpected action {move['action_type']!r}")
            return sandbox_client.call_method("purchase.order", "button_confirm", [ext_po_id])

        def _observe(move: dict[str, Any]) -> dict[str, Any]:
            rows = sandbox_client.search_read(
                "purchase.order",
                [("id", "=", ext_po_id)],
                ["id", "name", "partner_id", "state", "amount_total"],
                limit=1,
            )
            assert rows
            return {"PurchaseOrder": [_project_po(rows[0])]}

        live_rt = OntologyRuntime(
            spec_v2,
            db_path=":memory:",
            world_mode="coupled",
            source_bridge=SourceBridge(actuate=_actuate, observe=_observe),
        )
        try:
            for entry in _base_m0_fixtures():
                cls = spec_v2.get_object_type(entry["object_type"])
                live_rt.put_object(cls(**entry["data"]))
            live_rt.put_object(
                spec_v2.get_object_type("BusinessParty")(
                    party_id="PARTY-LIVE-VENDOR",
                    legal_name=partner_name,
                    tax_id="",
                    country_code="US",
                    status="active",
                )
            )
            live_rt.put_object(
                spec_v2.get_object_type("SupplierProcurementProfile")(
                    supplier_profile_id="SUPP-PROF-LIVE",
                    party="PARTY-LIVE-VENDOR",
                    entity="ENT-NORTH",
                    lead_time_days=5,
                    approved_spend_limit=100_000.0,
                    status="active",
                )
            )
            live_rt.put_object(spec_v2.get_object_type("PurchaseOrder")(**_project_po(po_row)))

            run = live_rt.submit_action("LIVE-PO-APPROVE-1", "approve_purchase_order", {"po_id": live_po_id}, P2P_OPS_ACTOR)
            assert run["status"] == "COMMITTED"
            post_rows = sandbox_client.search_read(
                "purchase.order",
                [("id", "=", ext_po_id)],
                ["id", "name", "state"],
                limit=1,
            )
            assert post_rows and post_rows[0]["state"] in ("purchase", "done")
            return {
                "sandbox_db": sandbox_client.db,
                "ext_po_id": ext_po_id,
                "po_number": str(po_row["name"]),
                "settled_status": run["status"],
                "odoo_state_after": str(post_rows[0]["state"]),
            }
        finally:
            live_rt.close()
