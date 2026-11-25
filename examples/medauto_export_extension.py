"""Customer Secondary-Development Showcase: MedAuto Export Manufacturing Extension Pack (WO-34).

Precision Medical & Automotive Export Components Manufacturer ("华美精密医疗与汽车部件出口有限公司")
built strictly monotonically on top of the untouched 6-domain standard Enterprise World Model
(``build_enterprise_standard_spec()``):

- Standard baseline: **31 actions** (**22 DOAL Rule** + **9 EditFunction**).
- MedAuto secondary development: **20 extension items** (**15 DOAL [75.0%]** + **5 EditFunction [25.0%]**):
  * **8 DOAL Structural / Inheritance / DL / Invariant Extensions**:
    1. ``ExportSalesOrder`` (extends ``O2cOrder``, narrowed FSM + 10 owned ``x_*`` fields)
    2. ``DomesticSampleOrder`` (extends ``O2cOrder``, narrowed FSM + 2 owned ``x_*`` fields)
    3. ``ReworkWorkOrder`` (extends ``WorkOrder``, narrowed FSM + 2 ``x_*`` fields + ``parent_work_order``)
    4. ``QualityInspection`` (``ProcessDocument``, pending -> passed | failed)
    5. ``RegulatoryCert`` (``ProcessDocument``, valid -> expired)
    6. ``CustomsDeclaration`` (``ProcessDocument``, draft -> cleared -> ExportSalesOrder)
    7. ``DealerRebateSettlement`` (``EventVoucher``, append-only rebate settlement)
    8. DL ``Disjoint(ExportSalesOrder, DomesticSampleOrder)``, monotonic ``x_*`` fields on ``CrmAccount`` /
       ``WorkOrder`` / ``O2cDelivery``, 3 declarative invariants, and 2 cross-object action preconditions
  * **7 Native DOAL Rule Actions**:
    1. ``submit_first_article_inspection``
    2. ``verify_export_letter_of_credit``
    3. ``declare_customs_clearance``
    4. ``flag_customer_credit_hold``
    5. ``classify_export_risk_tier``
    6. ``open_rework_work_order`` (``op="create"`` reading ``source_obj.work_order.*``)
    7. ``approve_sample_free_shipment``
  * **5 Custom Turing-Complete EditFunctions**:
    8. ``apply_export_tax_rebate_claim``
    9. ``batch_recall_defective_lot``
    10. ``explode_multilevel_substitute_bom``
    11. ``settle_annual_tiered_dealer_rebate``
    12. ``allocate_container_shipping_cost``

- Effective total: **43 actions** (**29 DOAL Rule [67.4%]** + **14 EditFunction [32.6%]**),
  mounted exclusively on the **Pure L1 Domain-Agnostic Shell (``pack_hint="none"``)**.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from open_ontology import Actor, InvariantError, OntologyRuntime, OntologySpec
from open_ontology.console_server import (
    ConsoleService,
    DemoActorProfile,
    WorkQueueBinding,
)
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    RelationSpec,
    apply_overlay_to_spec,
    enforce_edit_function_contracts,
    export_spec_blueprint,
)
from open_ontology.spec import EditFunctionDef

try:
    from .business_ontology import BUSINESS_SEED_ONTOLOGY, WorkCard
    from .std_core_ontology import spec as std_core_spec
    from .std_crm_ontology import spec as std_crm_spec
    from .std_enterprise_world_model import build_enterprise_standard_spec
    from .std_h2r_ontology import spec as std_h2r_spec
    from .std_o2c_ontology import spec as std_o2c_spec
    from .std_p2m_ontology import spec as std_p2m_spec
    from .std_p2p_ontology import spec as std_p2p_spec
    from .std_r2r_ontology import spec as std_r2r_spec
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import BUSINESS_SEED_ONTOLOGY, WorkCard
    from examples.std_core_ontology import spec as std_core_spec
    from examples.std_crm_ontology import spec as std_crm_spec
    from examples.std_enterprise_world_model import build_enterprise_standard_spec
    from examples.std_h2r_ontology import spec as std_h2r_spec
    from examples.std_o2c_ontology import spec as std_o2c_spec
    from examples.std_p2m_ontology import spec as std_p2m_spec
    from examples.std_p2p_ontology import spec as std_p2p_spec
    from examples.std_r2r_ontology import spec as std_r2r_spec

__all__ = [
    "MEDAUTO_DOAL_OVERLAY",
    "MEDAUTO_CUSTOM_EDIT_FUNCTIONS",
    "MEDAUTO_WORK_QUEUE",
    "MEDAUTO_SANDBOX_PRESETS",
    "MEDAUTO_QUICK_PROMPTS",
    "build_medauto_spec",
    "build_medauto_blueprint",
    "seed_medauto_world",
    "build_medauto_console",
]

# ---------------------------------------------------------------------------
# 1. Customer Overlay (8 DOAL Structural/DL/Invariant + 7 DOAL Rule Actions)
# ---------------------------------------------------------------------------
MEDAUTO_DOAL_OVERLAY = CustomerOverlaySpec(
    overlay_id="medauto_export_mfg_overlay",
    base_model_id="std_enterprise_world_model",
    target_version="1.0.0",
    description="华美精密医疗与汽车部件出口制造二开扩展包：15 项 DOAL 声明式扩展与 5 个客户定制 EditFunction（只增不破）。",
    derive_object_types=[
        ObjectTypeBlueprint(
            name="ExportSalesOrder",
            extends="O2cOrder",
            primary_key="order_id",
            title_field="order_number",
            owned=False,
            status_field="status",
            initial_statuses=["draft"],
            transitions={
                "draft": ["confirmed", "cancelled"],
                "confirmed": ["delivered", "cancelled"],
                "delivered": [],
                "invoiced": [],
                "cancelled": [],
            },
            fields=[
                FieldSpec(name="x_customs_no", field_type="str", default="", owned=True),
                FieldSpec(name="x_incoterms", field_type="str", default="FOB", owned=True),
                FieldSpec(name="x_destination_country", field_type="str", default="DE", owned=True),
                FieldSpec(name="x_lc_verified", field_type="bool", default=False, owned=True),
                FieldSpec(
                    name="x_discount_pct",
                    field_type="int",
                    default=0,
                    non_negative=True,
                    owned=True,
                ),
                FieldSpec(name="x_risk_tier", field_type="str", default="LOW", owned=True),
                FieldSpec(name="x_container_no", field_type="str", default="", owned=True),
                FieldSpec(
                    name="x_gross_weight_kg",
                    field_type="int",
                    default=100,
                    non_negative=True,
                    owned=True,
                ),
                FieldSpec(
                    name="x_allocated_freight_cents",
                    field_type="int",
                    default=0,
                    non_negative=True,
                    owned=True,
                ),
                FieldSpec(name="x_lot_code", field_type="str", default="", owned=True),
            ],
            display_label="出口贸易销售订单",
        ),
        ObjectTypeBlueprint(
            name="DomesticSampleOrder",
            extends="O2cOrder",
            primary_key="order_id",
            title_field="order_number",
            owned=True,
            status_field="status",
            initial_statuses=["draft"],
            transitions={
                "draft": ["confirmed", "cancelled"],
                "confirmed": ["delivered", "cancelled"],
                "delivered": [],
                "invoiced": [],
                "cancelled": [],
            },
            fields=[
                FieldSpec(
                    name="x_sample_qty",
                    field_type="int",
                    default=1,
                    non_negative=True,
                    owned=True,
                ),
                FieldSpec(name="x_sample_approved", field_type="bool", default=False, owned=True),
            ],
            display_label="国内试制样品单",
        ),
        ObjectTypeBlueprint(
            name="ReworkWorkOrder",
            extends="WorkOrder",
            primary_key="work_order_id",
            title_field="work_order_id",
            owned=True,
            status_field="status",
            initial_statuses=["draft"],
            transitions={
                "draft": ["released"],
                "released": ["in_progress"],
                "in_progress": ["completed"],
                "completed": [],
                "cancelled": [],
            },
            fields=[
                FieldSpec(
                    name="x_rework_qty",
                    field_type="int",
                    default=1,
                    non_negative=True,
                    owned=True,
                ),
                FieldSpec(name="x_defect_reason", field_type="str", default="", owned=True),
            ],
            relations=[
                RelationSpec(
                    field="parent_work_order",
                    target_type="WorkOrder",
                    required=True,
                    min_cardinality=1,
                    owned=True,
                ),
            ],
            display_label="不良返工工单",
        ),
    ],
    add_object_types=[
        ObjectTypeBlueprint(
            name="QualityInspection",
            archetype="ProcessDocument",
            primary_key="inspection_id",
            title_field="inspection_code",
            owned=True,
            status_field="status",
            initial_statuses=["pending"],
            transitions={
                "pending": ["passed", "failed"],
                "passed": [],
                "failed": [],
            },
            fields=[
                FieldSpec(name="inspection_id", field_type="str", primary_key=True, required=True, owned=True),
                FieldSpec(name="inspection_code", field_type="str", required=True, owned=True),
                FieldSpec(name="lot_code", field_type="str", default="", owned=True),
                FieldSpec(name="defect_count", field_type="int", default=0, non_negative=True, owned=True),
                FieldSpec(name="inspector_note", field_type="str", default="", owned=True),
                FieldSpec(name="rework_opened", field_type="bool", default=False, owned=True),
                FieldSpec(name="status", field_type="str", default="pending", owned=True),
            ],
            relations=[
                RelationSpec(
                    field="work_order",
                    target_type="WorkOrder",
                    required=True,
                    min_cardinality=1,
                    owned=True,
                ),
                RelationSpec(
                    field="order",
                    target_type="O2cOrder",
                    required=False,
                    owned=True,
                ),
            ],
            display_label="首件质检单",
        ),
        ObjectTypeBlueprint(
            name="RegulatoryCert",
            archetype="ProcessDocument",
            primary_key="cert_id",
            title_field="cert_no",
            owned=True,
            status_field="status",
            initial_statuses=["valid", "expired"],
            transitions={
                "valid": ["expired"],
                "expired": [],
            },
            fields=[
                FieldSpec(name="cert_id", field_type="str", primary_key=True, required=True, owned=True),
                FieldSpec(name="cert_no", field_type="str", required=True, owned=True),
                FieldSpec(name="cert_kind", field_type="str", default="IATF_16949", owned=True),
                FieldSpec(name="status", field_type="str", default="valid", owned=True),
            ],
            relations=[
                RelationSpec(
                    field="account",
                    target_type="CrmAccount",
                    required=True,
                    min_cardinality=1,
                    owned=True,
                ),
            ],
            display_label="医疗/车规合规资质",
        ),
        ObjectTypeBlueprint(
            name="CustomsDeclaration",
            archetype="ProcessDocument",
            primary_key="declaration_id",
            title_field="customs_no",
            owned=True,
            status_field="status",
            initial_statuses=["draft"],
            transitions={
                "draft": ["cleared"],
                "cleared": [],
            },
            fields=[
                FieldSpec(name="declaration_id", field_type="str", primary_key=True, required=True, owned=True),
                FieldSpec(name="customs_no", field_type="str", required=True, owned=True),
                FieldSpec(name="customs_fee_cents", field_type="int", default=0, non_negative=True, owned=True),
                FieldSpec(name="rebate_amount_cents", field_type="int", default=0, non_negative=True, owned=True),
                FieldSpec(name="rebate_claimed", field_type="bool", default=False, owned=True),
                FieldSpec(name="status", field_type="str", default="draft", owned=True),
            ],
            relations=[
                RelationSpec(
                    field="export_order",
                    target_type="ExportSalesOrder",
                    required=True,
                    min_cardinality=1,
                    owned=True,
                ),
            ],
            display_label="出口报关单",
        ),
        ObjectTypeBlueprint(
            name="DealerRebateSettlement",
            archetype="EventVoucher",
            primary_key="settlement_id",
            title_field="settlement_code",
            aggregate_root=False,
            append_only=True,
            owned=False,
            fields=[
                FieldSpec(name="settlement_id", field_type="str", primary_key=True, required=True, owned=False),
                FieldSpec(name="settlement_code", field_type="str", required=True, owned=False),
                FieldSpec(name="annual_sales_cents", field_type="int", default=0, non_negative=True, owned=False),
                FieldSpec(name="rebate_cents", field_type="int", default=0, non_negative=True, owned=False),
                FieldSpec(name="offset_invoice_count", field_type="int", default=0, non_negative=True, owned=False),
            ],
            relations=[
                RelationSpec(
                    field="account",
                    target_type="CrmAccount",
                    required=True,
                    min_cardinality=1,
                    owned=False,
                ),
            ],
            display_label="经销商年度阶梯返利凭证",
        ),
    ],
    add_disjoint_types=[("ExportSalesOrder", "DomesticSampleOrder")],
    add_fields={
        "CrmAccount": [
            FieldSpec(
                name="x_credit_status",
                field_type="str",
                default="NORMAL",
                owned=True,
                description="出口客户信用管控状态（NORMAL / HOLD）",
            ),
        ],
        "WorkOrder": [
            FieldSpec(name="x_lot_code", field_type="str", default="", owned=True),
            FieldSpec(name="x_quarantined", field_type="bool", default=False, owned=True),
        ],
        "O2cDelivery": [
            FieldSpec(name="x_lot_code", field_type="str", default="", owned=True),
            FieldSpec(name="x_quarantined", field_type="bool", default=False, owned=True),
        ],
    },
    add_invariants=[
        InvariantRuleSpec(
            name="export_discount_cap",
            code="EXPORT_DISCOUNT_CAP_EXCEEDED",
            target_type="ExportSalesOrder",
            must_hold={
                "op": "le",
                "left": {"ref": "obj.x_discount_pct"},
                "right": {"lit": 12},
            },
            description="出口销售订单允许商业折扣比例不得超过 12%。",
        ),
        InvariantRuleSpec(
            name="rework_qty_within_planned",
            code="REWORK_EXCEEDS_PLANNED_QTY",
            target_type="ReworkWorkOrder",
            must_hold={
                "op": "le",
                "left": {"ref": "obj.x_rework_qty"},
                "right": {"ref": "obj.planned_qty"},
            },
            description="返工工单返修数量不得超过母工单计划产量。",
        ),
        InvariantRuleSpec(
            name="export_high_value_requires_lc",
            code="EXPORT_HIGH_VALUE_REQUIRES_LC",
            target_type="ExportSalesOrder",
            when={
                "op": "and",
                "args": [
                    {"op": "ne", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    {"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 500000.0}},
                ],
            },
            must_hold={
                "op": "eq",
                "left": {"ref": "obj.x_lc_verified"},
                "right": {"lit": True},
            },
            description="金额超过 500,000 的非草稿出口订单必须完成信用证（L/C）核验。",
        ),
    ],
    add_action_preconditions={
        "complete_o2c_delivery": [
            InvariantRuleSpec(
                name="delivery_requires_passed_fai",
                code="DELIVERY_MISSING_PASSED_FAI",
                target_type="O2cDelivery",
                id_param="delivery_id",
                must_hold={
                    "op": "exists",
                    "object_type": "QualityInspection",
                    "where": {
                        "op": "and",
                        "args": [
                            {"op": "eq", "left": {"ref": "target.order"}, "right": {"ref": "obj.order"}},
                            {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "passed"}},
                        ],
                    },
                },
                description="红线拦截：完成出库发货前必须关联一条已通过（passed）的首件质检单。",
            ),
        ],
        "confirm_o2c_order": [
            InvariantRuleSpec(
                name="cert_not_expired_blocks_confirm",
                code="CUSTOMER_REGULATORY_CERT_EXPIRED",
                target_type="O2cOrder",
                id_param="order_id",
                must_hold={
                    "op": "not_exists",
                    "object_type": "RegulatoryCert",
                    "where": {
                        "op": "and",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"ref": "target.account.party"},
                                "right": {"ref": "obj.customer.party"},
                            },
                            {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "expired"}},
                        ],
                    },
                },
                description="红线拦截：若客户名下存在已过期医疗/车规合规证书，严禁确认销售订单。",
            ),
        ],
    },
    add_actions=[
        # 1. submit_first_article_inspection
        ActionBlueprint(
            name="submit_first_article_inspection",
            description="提交首件质检结论：0 缺陷自动判定 passed，存在缺陷判定 failed。",
            allowed_roles=["ops", "plant_manager", "steward", "ai_agent", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="inspection_id", field_type="str", required=True),
                FieldSpec(name="defect_count", field_type="int", required=True),
                FieldSpec(name="inspector_note", field_type="str", default=""),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="fai_must_be_pending",
                    code="INSPECTION_NOT_PENDING",
                    target_type="QualityInspection",
                    id_param="inspection_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "pending"}},
                    description="仅待检状态（pending）首件质检单允许录入结论。",
                ),
                InvariantRuleSpec(
                    name="fai_defect_count_non_negative",
                    code="NEGATIVE_DEFECT_COUNT",
                    target_type="QualityInspection",
                    id_param="inspection_id",
                    must_hold={"op": "ge", "left": {"ref": "param.defect_count"}, "right": {"lit": 0}},
                    description="缺陷数不得为负。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="QualityInspection",
                    id_param="inspection_id",
                    when={"op": "eq", "left": {"ref": "param.defect_count"}, "right": {"lit": 0}},
                    set_fields={
                        "defect_count": {"ref": "param.defect_count"},
                        "inspector_note": {"ref": "param.inspector_note"},
                        "status": {"lit": "passed"},
                    },
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="QualityInspection",
                    id_param="inspection_id",
                    when={"op": "gt", "left": {"ref": "param.defect_count"}, "right": {"lit": 0}},
                    set_fields={
                        "defect_count": {"ref": "param.defect_count"},
                        "inspector_note": {"ref": "param.inspector_note"},
                        "status": {"lit": "failed"},
                    },
                ),
            ],
            display_label="录入首件质检结论",
        ),
        # 2. verify_export_letter_of_credit
        ActionBlueprint(
            name="verify_export_letter_of_credit",
            description="核验高额出口订单信用证（L/C）开立与真实性。",
            allowed_roles=["finance_director", "finance", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="order_id", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="lc_order_not_cancelled",
                    code="ORDER_CANCELLED",
                    target_type="ExportSalesOrder",
                    id_param="order_id",
                    must_hold={"op": "ne", "left": {"ref": "obj.status"}, "right": {"lit": "cancelled"}},
                    description="已取消出口订单不可核验信用证。",
                ),
                InvariantRuleSpec(
                    name="lc_not_yet_verified",
                    code="LC_ALREADY_VERIFIED",
                    target_type="ExportSalesOrder",
                    id_param="order_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.x_lc_verified"}, "right": {"lit": False}},
                    description="信用证已核验通过，请勿重复核验。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="ExportSalesOrder",
                    id_param="order_id",
                    set_fields={"x_lc_verified": {"lit": True}},
                ),
            ],
            display_label="核验出口信用证",
        ),
        # 3. declare_customs_clearance
        ActionBlueprint(
            name="declare_customs_clearance",
            description="完成出口报关清关放行，并同步回填关联出口订单的报关单号。",
            allowed_roles=["ops", "warehouse", "finance", "steward", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="declaration_id", field_type="str", required=True),
                FieldSpec(name="customs_fee_cents", field_type="int", default=0),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="customs_must_be_draft",
                    code="CUSTOMS_NOT_DRAFT",
                    target_type="CustomsDeclaration",
                    id_param="declaration_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    description="仅草稿报关单允许申报清关。",
                ),
                InvariantRuleSpec(
                    name="customs_export_order_delivered",
                    code="EXPORT_ORDER_NOT_DELIVERED",
                    target_type="CustomsDeclaration",
                    id_param="declaration_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.export_order.status"}, "right": {"lit": "delivered"}},
                    description="关联出口订单必须已完成发货出库（delivered）方可清关。",
                ),
                InvariantRuleSpec(
                    name="customs_fee_non_negative",
                    code="NEGATIVE_CUSTOMS_FEE",
                    target_type="CustomsDeclaration",
                    id_param="declaration_id",
                    must_hold={"op": "ge", "left": {"ref": "param.customs_fee_cents"}, "right": {"lit": 0}},
                    description="报关规费金额不得为负。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="CustomsDeclaration",
                    id_param="declaration_id",
                    set_fields={
                        "customs_fee_cents": {"ref": "param.customs_fee_cents"},
                        "status": {"lit": "cleared"},
                    },
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="ExportSalesOrder",
                    id_param="order_id",
                    target_ref="obj.export_order",
                    set_fields={
                        "x_customs_no": {"ref": "source_obj.customs_no"},
                    },
                ),
            ],
            display_label="申报出口报关放行",
        ),
        # 4. flag_customer_credit_hold
        ActionBlueprint(
            name="flag_customer_credit_hold",
            description="当客户存在已过账未结清应收发票时，将其信用状态锁定为 HOLD。",
            allowed_roles=["finance", "finance_director", "sales_manager", "steward", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="account_id", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="credit_account_not_already_hold",
                    code="ALREADY_ON_CREDIT_HOLD",
                    target_type="CrmAccount",
                    id_param="account_id",
                    must_hold={"op": "ne", "left": {"ref": "obj.x_credit_status"}, "right": {"lit": "HOLD"}},
                    description="客户已处于信用冻结状态。",
                ),
                InvariantRuleSpec(
                    name="credit_has_unpaid_posted_invoice",
                    code="NO_UNPAID_INVOICE",
                    target_type="CrmAccount",
                    id_param="account_id",
                    must_hold={
                        "op": "exists",
                        "object_type": "O2cInvoice",
                        "where": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "posted"}},
                                {"op": "ne", "left": {"ref": "target.payment_state"}, "right": {"lit": "paid"}},
                                {
                                    "op": "eq",
                                    "left": {"ref": "target.order.customer.party"},
                                    "right": {"ref": "obj.party"},
                                },
                            ],
                        },
                    },
                    description="仅在关联企业存在未结清已过账应收发票时触发信用冻结。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="CrmAccount",
                    id_param="account_id",
                    set_fields={"x_credit_status": {"lit": "HOLD"}},
                ),
            ],
            display_label="锁定客户逾期信用冻结",
        ),
        # 5. classify_export_risk_tier
        ActionBlueprint(
            name="classify_export_risk_tier",
            description="按出口订单金额三段自动定级风险等级（HIGH / MEDIUM / LOW）。",
            allowed_roles=["sales_manager", "finance_director", "ops", "steward", "ai_agent", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="order_id", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="risk_order_active",
                    code="ORDER_CANCELLED",
                    target_type="ExportSalesOrder",
                    id_param="order_id",
                    must_hold={"op": "ne", "left": {"ref": "obj.status"}, "right": {"lit": "cancelled"}},
                    description="已取消出口订单不参与风险定级。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="ExportSalesOrder",
                    id_param="order_id",
                    when={"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 500000.0}},
                    set_fields={"x_risk_tier": {"lit": "HIGH"}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="ExportSalesOrder",
                    id_param="order_id",
                    when={
                        "op": "and",
                        "args": [
                            {"op": "ge", "left": {"ref": "obj.amount_total"}, "right": {"lit": 200000.0}},
                            {"op": "le", "left": {"ref": "obj.amount_total"}, "right": {"lit": 500000.0}},
                        ],
                    },
                    set_fields={"x_risk_tier": {"lit": "MEDIUM"}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="ExportSalesOrder",
                    id_param="order_id",
                    when={"op": "lt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 200000.0}},
                    set_fields={"x_risk_tier": {"lit": "LOW"}},
                ),
            ],
            display_label="出口订单风险三档定级",
        ),
        # 6. open_rework_work_order (uses op="create" reading source_obj.work_order.*)
        ActionBlueprint(
            name="open_rework_work_order",
            description="根据未通过的首件质检单自动派生返工工单，并标记原质检单已开立返工。",
            allowed_roles=["plant_manager", "production_planner", "ops", "steward", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="inspection_id", field_type="str", required=True),
                FieldSpec(name="rework_work_order_id", field_type="str", required=True),
                FieldSpec(name="rework_qty", field_type="int", required=True),
                FieldSpec(name="defect_reason", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="rework_requires_failed_fai",
                    code="INSPECTION_NOT_FAILED",
                    target_type="QualityInspection",
                    id_param="inspection_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "failed"}},
                    description="仅不合格（failed）首件质检单可开立返工工单。",
                ),
                InvariantRuleSpec(
                    name="rework_not_already_opened",
                    code="REWORK_ALREADY_OPENED",
                    target_type="QualityInspection",
                    id_param="inspection_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.rework_opened"}, "right": {"lit": False}},
                    description="该质检单已派生返工工单，禁止重复开立。",
                ),
                InvariantRuleSpec(
                    name="rework_qty_positive",
                    code="NON_POSITIVE_REWORK_QTY",
                    target_type="QualityInspection",
                    id_param="inspection_id",
                    must_hold={"op": "gt", "left": {"ref": "param.rework_qty"}, "right": {"lit": 0}},
                    description="返工数量必须严格大于 0。",
                ),
                InvariantRuleSpec(
                    name="rework_qty_within_parent_planned",
                    code="REWORK_EXCEEDS_PLANNED_QTY",
                    target_type="QualityInspection",
                    id_param="inspection_id",
                    must_hold={
                        "op": "le",
                        "left": {"ref": "param.rework_qty"},
                        "right": {"ref": "obj.work_order.planned_qty"},
                    },
                    description="返工数量不得超过原生产工单计划产量。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="create",
                    object_type="ReworkWorkOrder",
                    id_param="rework_work_order_id",
                    set_fields={
                        "work_order_id": {"ref": "param.rework_work_order_id"},
                        "accounting_entity": {"ref": "source_obj.work_order.accounting_entity"},
                        "bom": {"ref": "source_obj.work_order.bom"},
                        "output_resource": {"ref": "source_obj.work_order.output_resource"},
                        "planned_qty": {"ref": "source_obj.work_order.planned_qty"},
                        "issued_qty": {"lit": 0.0},
                        "issued_cost_cents": {"lit": 0},
                        "completed_good_qty": {"lit": 0.0},
                        "completed_scrap_qty": {"lit": 0.0},
                        "status": {"lit": "draft"},
                        "x_lot_code": {"ref": "source_obj.lot_code"},
                        "x_quarantined": {"lit": False},
                        "x_rework_qty": {"ref": "param.rework_qty"},
                        "x_defect_reason": {"ref": "param.defect_reason"},
                        "parent_work_order": {"ref": "source_obj.work_order"},
                    },
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="QualityInspection",
                    id_param="inspection_id",
                    set_fields={"rework_opened": {"lit": True}},
                ),
            ],
            display_label="不合格质检开立返工工单",
        ),
        # 7. approve_sample_free_shipment
        ActionBlueprint(
            name="approve_sample_free_shipment",
            description="特批数量 <= 5 件的国内试制免费样品单直接确认。",
            allowed_roles=["sales_manager", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="order_id", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="sample_order_must_be_draft",
                    code="SAMPLE_ORDER_NOT_DRAFT",
                    target_type="DomesticSampleOrder",
                    id_param="order_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    description="仅草稿国内样品单可执行特批确认。",
                ),
                InvariantRuleSpec(
                    name="sample_qty_within_free_limit",
                    code="SAMPLE_QTY_EXCEEDS_FREE_LIMIT",
                    target_type="DomesticSampleOrder",
                    id_param="order_id",
                    must_hold={"op": "le", "left": {"ref": "obj.x_sample_qty"}, "right": {"lit": 5}},
                    description="免费试制样品单数量不得超过 5 件上限。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="DomesticSampleOrder",
                    id_param="order_id",
                    set_fields={
                        "x_sample_approved": {"lit": True},
                        "status": {"lit": "confirmed"},
                    },
                ),
            ],
            display_label="特批免费样品直发确认",
        ),
    ],
)

# ---------------------------------------------------------------------------
# 2. Five Custom Turing-Complete EditFunctions
# ---------------------------------------------------------------------------


class ApplyExportTaxRebateParams(BaseModel):
    declaration_id: str
    rebate_rate: float = Field(default=0.13, gt=0.0, le=0.25)
    fx_rate: float = Field(default=7.20, gt=0.0)


def _build_read_set_from_batch(ctx: Any) -> list[dict[str, Any]]:
    read_set: list[dict[str, Any]] = []
    for key, ver in sorted(getattr(ctx, "read_versions", {}).items()):
        ot_name, _, oid = str(key).partition(":")
        if ot_name and oid:
            read_set.append(
                {
                    "object_type": ot_name,
                    "object_id": oid,
                    "expected_version": int(ver),
                }
            )
    return read_set


def _plan_export_tax_rebate_claim(ctx: Any, params: ApplyExportTaxRebateParams) -> dict[str, Any]:
    """Pure read-only M2.5 planner (zero write handle): compute statutory export tax rebate proposal."""
    DeclCls = ctx.spec.get_object_type("CustomsDeclaration")
    ExpOrderCls = ctx.spec.get_object_type("ExportSalesOrder")
    decl = ctx.require(DeclCls, params.declaration_id)
    if decl.status != "cleared":
        raise InvariantError(
            f"CustomsDeclaration:{decl.declaration_id} status={decl.status!r} must be 'cleared' before rebate claim",
            code="CUSTOMS_NOT_CLEARED",
        )
    if decl.rebate_claimed:
        raise InvariantError(
            f"CustomsDeclaration:{decl.declaration_id} export tax rebate already claimed",
            code="REBATE_ALREADY_CLAIMED",
        )
    exp_order = ctx.require(ExpOrderCls, decl.export_order)
    rebate_cents = int(
        round(float(exp_order.amount_total) * 100.0 * float(params.rebate_rate) * float(params.fx_rate))
    )
    if rebate_cents <= 0:
        raise InvariantError("computed rebate_amount_cents must be > 0", code="NON_POSITIVE_REBATE")
    return {
        "contract_name": "apply_export_tax_rebate_claim",
        "params": params.model_dump(mode="json"),
        "read_set": _build_read_set_from_batch(ctx),
        "ops": [
            {
                "op": "modify",
                "object_type": "CustomsDeclaration",
                "object_id": str(decl.declaration_id),
                "set_fields": {
                    "rebate_amount_cents": rebate_cents,
                    "rebate_claimed": True,
                },
            }
        ],
        "brain_source": "pure_function:plan_export_tax_rebate_claim",
    }


def _apply_export_tax_rebate_claim(ctx: Any, params: ApplyExportTaxRebateParams) -> None:
    from open_ontology.contract_gate import WriteOp, apply_ops_to_batch

    plan = _plan_export_tax_rebate_claim(ctx, params)
    apply_ops_to_batch(ctx, [WriteOp.model_validate(o) for o in plan["ops"]])


class BatchRecallDefectiveLotParams(BaseModel):
    lot_code: str
    reason: str = "质量异常批次紧急召回隔离"


def _batch_recall_defective_lot(ctx: Any, params: BatchRecallDefectiveLotParams) -> None:
    clean_lot = params.lot_code.strip()
    if not clean_lot:
        raise InvariantError("lot_code must not be empty", code="EMPTY_LOT_CODE")

    WoCls = ctx.spec.get_object_type("WorkOrder")
    DelCls = ctx.spec.get_object_type("O2cDelivery")
    ExpCls = ctx.spec.get_object_type("ExportSalesOrder")
    WcCls = ctx.spec.get_object_type("WorkCard")

    targets: list[tuple[str, Any]] = []
    seen_targets: set[tuple[str, str]] = set()
    for base_cls in (WoCls, DelCls):
        for sub_cls in ctx.spec.subtypes_of(base_cls):
            for row in ctx.query(sub_cls):
                if getattr(row, "x_lot_code", "") != clean_lot:
                    continue
                if getattr(row, "x_quarantined", False):
                    continue
                key = (sub_cls.__name__, row.primary_key_value())
                if key not in seen_targets:
                    seen_targets.add(key)
                    targets.append((sub_cls.__name__, row))

    # Also track any ExportSalesOrder lot tagging if needed (does not set x_quarantined unless field exists)
    _ = ExpCls

    if not targets:
        raise InvariantError(
            f"no unquarantined WorkOrder/O2cDelivery found for lot_code={clean_lot!r}",
            code="LOT_NOT_FOUND_OR_ALREADY_QUARANTINED",
        )

    existing_card_ids = {c.card_id for c in ctx.query(WcCls)}
    existing_active_recall_sigs = {
        (c.card_kind, c.target_object_type, c.target_object_id, c.requested_action)
        for c in ctx.query(WcCls)
        if c.status in ("OPEN", "IN_PROGRESS")
    }
    for idx, (otype, row) in enumerate(sorted(targets, key=lambda pair: (pair[0], pair[1].primary_key_value())), start=1):
        ctx.modify(row, x_quarantined=True)
        oid = row.primary_key_value()
        recall_sig = ("HUMAN_TASK", otype, oid, "batch_recall_defective_lot")
        if recall_sig in existing_active_recall_sigs:
            raise InvariantError(
                f"duplicate active recall WorkCard for {recall_sig}",
                code="DUPLICATE_ACTIVE_WORK_CARD",
            )
        existing_active_recall_sigs.add(recall_sig)
        cid = f"WC-RECALL-{clean_lot}-{idx:02d}"
        while cid in existing_card_ids:
            cid = f"{cid}-X"
        existing_card_ids.add(cid)
        ctx.create(
            WcCls(
                card_id=cid,
                card_kind="HUMAN_TASK",
                target_object_type=otype,
                target_object_id=oid,
                requested_action="batch_recall_defective_lot",
                requested_params_json=json.dumps({"lot_code": clean_lot, "reason": params.reason}, ensure_ascii=False),
                requester_actor_id=ctx.actor.actor_id,
                assignee_role="plant_manager",
                assignee_dept="品质工程与制造管理部",
                status="OPEN",
                external_ref=f"[{clean_lot}] {otype}:{oid} 缺陷批次锁定隔离 — {params.reason}",
            )
        )


class ExplodeMultilevelSubstituteBomParams(BaseModel):
    parent_work_order_id: str
    available_stock_json: str = "{}"
    child_wo_prefix: str = "SUB-WO"


def _plan_multilevel_substitute_bom(
    ctx: Any, params: ExplodeMultilevelSubstituteBomParams
) -> dict[str, Any]:
    """Pure read-only M2.5 planner (zero write handle): recursively explode multi-level substitute BOM."""
    WoCls = ctx.spec.get_object_type("WorkOrder")
    BomCls = ctx.spec.get_object_type("BomSpec")

    parent_wo = ctx.require(WoCls, params.parent_work_order_id)
    try:
        raw_stock = json.loads(params.available_stock_json or "{}")
    except Exception as exc:
        raise InvariantError(f"invalid available_stock_json: {exc}", code="INVALID_STOCK_JSON") from exc
    if not isinstance(raw_stock, dict):
        raise InvariantError("available_stock_json must encode a JSON object", code="INVALID_STOCK_JSON")
    stock: dict[str, float] = {str(k): max(0.0, float(v)) for k, v in raw_stock.items()}

    boms_by_output: dict[str, list[Any]] = {}
    for bom in ctx.query(BomCls):
        if bom.status == "active":
            boms_by_output.setdefault(bom.output_resource, []).append(bom)
    for res_id in boms_by_output:
        boms_by_output[res_id].sort(key=lambda b: (str(b.version), str(b.bom_id)))

    root_bom = ctx.require(BomCls, parent_wo.bom)
    existing_wo_ids: set[str] = set()
    for sub_cls in ctx.spec.subtypes_of(WoCls):
        for wo_row in ctx.query(sub_cls):
            existing_wo_ids.add(wo_row.work_order_id)

    def _choose_bom(resource_id: str, make_qty: float) -> Any | None:
        cands = boms_by_output.get(resource_id, ())
        if not cands:
            return None
        for cand in cands:
            needed = make_qty * float(cand.input_per_unit)
            if cand.input_resource in boms_by_output or stock.get(cand.input_resource, 0.0) >= needed - 1e-9:
                return cand
        return cands[0]

    queue: list[tuple[str, float, tuple[str, ...]]] = [
        (
            root_bom.input_resource,
            float(parent_wo.planned_qty) * float(root_bom.input_per_unit),
            (parent_wo.output_resource, root_bom.input_resource),
        )
    ]
    ops: list[dict[str, Any]] = []
    created_seq = 0
    max_children = 24
    while queue:
        res_id, req_qty, path = queue.pop(0)
        if req_qty <= 1e-9:
            continue
        on_hand = stock.get(res_id, 0.0)
        consume = min(on_hand, req_qty)
        if consume > 0.0:
            stock[res_id] = round(on_hand - consume, 6)
        shortage = round(req_qty - consume, 6)
        if shortage <= 1e-9:
            continue
        chosen = _choose_bom(res_id, shortage)
        if chosen is None:
            continue
        created_seq += 1
        if created_seq > max_children:
            raise InvariantError(
                "BOM explosion exceeded safety limit (possible cycle or excessive depth)",
                code="BOM_EXPLOSION_LIMIT",
            )
        wo_id = f"{params.child_wo_prefix}-{created_seq:02d}"
        while wo_id in existing_wo_ids:
            created_seq += 1
            wo_id = f"{params.child_wo_prefix}-{created_seq:02d}"
        existing_wo_ids.add(wo_id)
        ops.append(
            {
                "op": "create",
                "object_type": "WorkOrder",
                "object_id": wo_id,
                "set_fields": {
                    "work_order_id": wo_id,
                    "accounting_entity": parent_wo.accounting_entity,
                    "bom": chosen.bom_id,
                    "commitment": parent_wo.commitment,
                    "output_resource": chosen.output_resource,
                    "planned_qty": shortage,
                    "status": "draft",
                    "x_lot_code": getattr(parent_wo, "x_lot_code", ""),
                },
            }
        )
        next_res = chosen.input_resource
        next_qty = round(shortage * float(chosen.input_per_unit), 6)
        if next_res in path:
            raise InvariantError(f"BOM cycle detected on {next_res!r}", code="BOM_CYCLE_DETECTED")
        if next_res in boms_by_output and next_qty > 1e-9:
            queue.append((next_res, next_qty, (*path, next_res)))

    if created_seq == 0:
        raise InvariantError(
            f"no shortage sub-BOM explosion required for WorkOrder:{parent_wo.work_order_id}",
            code="NO_BOM_SHORTAGE",
        )

    return {
        "contract_name": "explode_multilevel_substitute_bom",
        "params": params.model_dump(mode="json"),
        "read_set": _build_read_set_from_batch(ctx),
        "ops": ops,
        "brain_source": "pure_function:plan_multilevel_substitute_bom",
    }


def _explode_multilevel_substitute_bom(ctx: Any, params: ExplodeMultilevelSubstituteBomParams) -> None:
    from open_ontology.contract_gate import WriteOp, apply_ops_to_batch

    plan = _plan_multilevel_substitute_bom(ctx, params)
    apply_ops_to_batch(ctx, [WriteOp.model_validate(o) for o in plan["ops"]])


class SettleAnnualTieredDealerRebateParams(BaseModel):
    settlement_id: str
    settlement_code: str
    account_id: str


def _compute_progressive_rebate_cents(annual_sales_cents: int) -> int:
    # Tiers (in fen / cents):
    #   [0, 200_000 RMB]:          2%
    #   (200_000, 500_000 RMB]:    4%
    #   (500_000, 1_000_000 RMB]:  6%
    #   > 1_000_000 RMB:           8%
    tiers = (
        (20_000_000, 2),
        (50_000_000, 4),
        (100_000_000, 6),
    )
    rebate_num = 0  # denominated in hundredths of a cent before rounding
    prev_limit = 0
    for limit, pct in tiers:
        if annual_sales_cents <= prev_limit:
            break
        span = min(annual_sales_cents, limit) - prev_limit
        rebate_num += span * pct
        prev_limit = limit
    if annual_sales_cents > 100_000_000:
        rebate_num += (annual_sales_cents - 100_000_000) * 8
    return (rebate_num + 50) // 100


def _settle_annual_tiered_dealer_rebate(ctx: Any, params: SettleAnnualTieredDealerRebateParams) -> None:
    AccCls = ctx.spec.get_object_type("CrmAccount")
    OrderCls = ctx.spec.get_object_type("O2cOrder")
    CustCls = ctx.spec.get_object_type("O2cCustomerProfile")
    InvCls = ctx.spec.get_object_type("O2cInvoice")
    SettleCls = ctx.spec.get_object_type("DealerRebateSettlement")

    if ctx.get(SettleCls, params.settlement_id) is not None:
        raise InvariantError(
            f"DealerRebateSettlement:{params.settlement_id} already exists",
            code="DUPLICATE_SETTLEMENT",
        )
    account = ctx.require(AccCls, params.account_id)
    party_id = account.party

    profile_ids = {p.profile_id for p in ctx.query(CustCls) if p.party == party_id}
    matching_order_ids: set[str] = set()
    annual_sales_cents = 0
    for sub_cls in ctx.spec.subtypes_of(OrderCls):
        for order in ctx.query(sub_cls):
            if order.customer in profile_ids:
                matching_order_ids.add(order.order_id)
                if order.status not in ("draft", "cancelled"):
                    annual_sales_cents += int(round(float(order.amount_total) * 100.0))

    if annual_sales_cents <= 0:
        raise InvariantError(
            f"CrmAccount:{account.account_id} has zero confirmed annual sales for rebate settlement",
            code="NO_ELIGIBLE_ANNUAL_SALES",
        )

    rebate_cents = _compute_progressive_rebate_cents(annual_sales_cents)
    remaining_cents = rebate_cents
    offset_count = 0

    open_invoices = sorted(
        (
            inv
            for inv in ctx.query(InvCls)
            if inv.order in matching_order_ids and inv.status == "posted" and float(inv.amount_residual) > 1e-9
        ),
        key=lambda inv: str(inv.invoice_id),
    )
    for inv in open_invoices:
        if remaining_cents <= 0:
            break
        res_cents = int(round(float(inv.amount_residual) * 100.0))
        apply_cents = min(res_cents, remaining_cents)
        if apply_cents <= 0:
            continue
        new_res_cents = res_cents - apply_cents
        remaining_cents -= apply_cents
        offset_count += 1
        ctx.modify(
            inv,
            amount_residual=round(new_res_cents / 100.0, 2),
            payment_state="paid" if new_res_cents == 0 else "partial",
        )

    ctx.create(
        SettleCls(
            settlement_id=params.settlement_id,
            settlement_code=params.settlement_code,
            account=account.account_id,
            annual_sales_cents=annual_sales_cents,
            rebate_cents=rebate_cents,
            offset_invoice_count=offset_count,
        )
    )


class AllocateContainerShippingCostParams(BaseModel):
    container_no: str
    total_freight_cents: int = Field(..., gt=0)


def _plan_container_shipping_cost(
    ctx: Any, params: AllocateContainerShippingCostParams
) -> dict[str, Any]:
    """Pure read-only M2.5 planner (zero write handle): allocate container freight by gross weight."""
    cntr = params.container_no.strip()
    if not cntr:
        raise InvariantError("container_no must not be empty", code="EMPTY_CONTAINER_NO")

    ExpOrderCls = ctx.spec.get_object_type("ExportSalesOrder")
    orders = sorted(
        (
            o
            for o in ctx.query(ExpOrderCls)
            if o.x_container_no == cntr and o.status != "cancelled"
        ),
        key=lambda o: str(o.order_id),
    )
    if not orders:
        raise InvariantError(
            f"no active ExportSalesOrder assigned to container {cntr!r}",
            code="EMPTY_CONTAINER_ORDERS",
        )
    total_weight = sum(int(o.x_gross_weight_kg) for o in orders)
    if total_weight <= 0:
        raise InvariantError(
            f"container {cntr!r} total gross weight must be > 0",
            code="ZERO_CONTAINER_WEIGHT",
        )

    ops: list[dict[str, Any]] = []
    allocated_so_far = 0
    last_idx = len(orders) - 1
    for idx, order in enumerate(orders):
        if idx < last_idx:
            share = (int(params.total_freight_cents) * int(order.x_gross_weight_kg)) // total_weight
            allocated_so_far += share
        else:
            share = int(params.total_freight_cents) - allocated_so_far
        ops.append(
            {
                "op": "modify",
                "object_type": "ExportSalesOrder",
                "object_id": str(order.order_id),
                "set_fields": {"x_allocated_freight_cents": share},
            }
        )

    return {
        "contract_name": "allocate_container_shipping_cost",
        "params": params.model_dump(mode="json"),
        "read_set": _build_read_set_from_batch(ctx),
        "ops": ops,
        "brain_source": "pure_function:plan_container_shipping_cost",
    }


def _allocate_container_shipping_cost(ctx: Any, params: AllocateContainerShippingCostParams) -> None:
    from open_ontology.contract_gate import WriteOp, apply_ops_to_batch

    plan = _plan_container_shipping_cost(ctx, params)
    apply_ops_to_batch(ctx, [WriteOp.model_validate(o) for o in plan["ops"]])


MEDAUTO_CUSTOM_EDIT_FUNCTIONS: tuple[EditFunctionDef, ...] = (
    EditFunctionDef(
        name="apply_export_tax_rebate_claim",
        params=ApplyExportTaxRebateParams,
        handler=_apply_export_tax_rebate_claim,
        description="基于清关后出口报关单的离岸货值 × 退税率 × 汇率执行非线性浮点精度出口退税核定。",
        touched_object_types=("CustomsDeclaration", "ExportSalesOrder"),
        modifies=("CustomsDeclaration.rebate_amount_cents", "CustomsDeclaration.rebate_claimed"),
        trust_mode="attested_provider",
        attested_providers=("plan_export_tax_rebate_claim",),
        require_read_set_cas=True,
        planner_function="plan_export_tax_rebate_claim",
        ensures=(
            {
                "name": "rebate_claim_post",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "CustomsDeclaration",
                "id_param": "declaration_id",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "eq", "left": {"ref": "before.status"}, "right": {"lit": "cleared"}},
                        {"op": "eq", "left": {"ref": "before.rebate_claimed"}, "right": {"lit": False}},
                        {"op": "eq", "left": {"ref": "after.rebate_claimed"}, "right": {"lit": True}},
                        {"op": "gt", "left": {"ref": "after.rebate_amount_cents"}, "right": {"lit": 0}},
                    ],
                },
                "description": "出口退税核定仅针对未申报清关报关单，且核定后退税额正值并置位已申报。",
            },
        ),
    ),
    EditFunctionDef(
        name="batch_recall_defective_lot",
        params=BatchRecallDefectiveLotParams,
        handler=_batch_recall_defective_lot,
        description="按缺陷批次号跨生产工单、返工工单与发货单原子锁定隔离，并批量开立车间追踪协同工单。",
        touched_object_types=("WorkOrder", "ReworkWorkOrder", "O2cDelivery", "WorkCard"),
        modifies=("WorkOrder.x_quarantined", "O2cDelivery.x_quarantined"),
        creates=("WorkCard",),
        ensures=(
            {
                "name": "batch_recall_card_open",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "WorkCard",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "eq", "left": {"ref": "after.status"}, "right": {"lit": "OPEN"}},
                        {"op": "eq", "left": {"ref": "after.resolved_by_actor_id"}, "right": {"lit": ""}},
                        {"op": "eq", "left": {"ref": "after.requester_actor_id"}, "right": {"ref": "actor.actor_id"}},
                    ],
                },
                "description": "批次召回生成的协同追踪工单初态必须为 OPEN 且记录发起人。",
            },
            {
                "name": "batch_recall_card_unique",
                "code": "DUPLICATE_ACTIVE_WORK_CARD",
                "target_type": "WorkCard",
                "must_hold": {
                    "op": "not_exists",
                    "object_type": "WorkCard",
                    "where": {
                        "op": "and",
                        "args": [
                            {"op": "ne", "left": {"ref": "target.card_id"}, "right": {"ref": "after.card_id"}},
                            {"op": "eq", "left": {"ref": "target.card_kind"}, "right": {"ref": "after.card_kind"}},
                            {
                                "op": "eq",
                                "left": {"ref": "target.target_object_type"},
                                "right": {"ref": "after.target_object_type"},
                            },
                            {
                                "op": "eq",
                                "left": {"ref": "target.target_object_id"},
                                "right": {"ref": "after.target_object_id"},
                            },
                            {
                                "op": "eq",
                                "left": {"ref": "target.requested_action"},
                                "right": {"ref": "after.requested_action"},
                            },
                            {
                                "op": "in",
                                "left": {"ref": "target.status"},
                                "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                            },
                        ],
                    },
                },
                "description": "批次召回协同工单不得与已有在途工单重复意图。",
            },
        ),
    ),
    EditFunctionDef(
        name="explode_multilevel_substitute_bom",
        params=ExplodeMultilevelSubstituteBomParams,
        handler=_explode_multilevel_substitute_bom,
        description="多级 BOM 递归展算与主料短缺自动切换替代料配方（BFS 扣减可用库存并原子创建多级子工单）。",
        touched_object_types=("WorkOrder", "BomSpec", "TradeResource"),
        creates=("WorkOrder",),
        trust_mode="untrusted_verifiable",
        require_read_set_cas=True,
        planner_function="plan_multilevel_substitute_bom",
        ensures=(
            {
                "name": "explode_bom_child_wo_post",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "WorkOrder",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "eq", "left": {"ref": "after.status"}, "right": {"lit": "draft"}},
                        {"op": "gt", "left": {"ref": "after.planned_qty"}, "right": {"lit": 0.0}},
                        {"op": "eq", "left": {"ref": "after.issued_qty"}, "right": {"lit": 0.0}},
                        {"op": "eq", "left": {"ref": "after.issued_cost_cents"}, "right": {"lit": 0}},
                        {"op": "eq", "left": {"ref": "after.completed_good_qty"}, "right": {"lit": 0.0}},
                        {"op": "eq", "left": {"ref": "after.completed_scrap_qty"}, "right": {"lit": 0.0}},
                    ],
                },
                "description": "多级 BOM 展算产生的子工单初态必须为 draft 且计划数量正值、已领料与产出均为零。",
            },
        ),
    ),
    EditFunctionDef(
        name="settle_annual_tiered_dealer_rebate",
        params=SettleAnnualTieredDealerRebateParams,
        handler=_settle_annual_tiered_dealer_rebate,
        description="按四档超额累进算法核算年度经销商返利，按 FIFO 冲抵未清应收发票并固化不可变返利凭证。",
        touched_object_types=(
            "DealerRebateSettlement",
            "CrmAccount",
            "O2cOrder",
            "ExportSalesOrder",
            "O2cInvoice",
        ),
        modifies=("O2cInvoice.amount_residual", "O2cInvoice.payment_state"),
        creates=("DealerRebateSettlement",),
        ensures=(
            {
                "name": "settle_rebate_invoice_post",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "O2cInvoice",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "ge", "left": {"ref": "after.amount_residual"}, "right": {"lit": 0.0}},
                        {
                            "op": "le",
                            "left": {"ref": "after.amount_residual"},
                            "right": {"ref": "before.amount_residual"},
                        },
                    ],
                },
                "description": "返利冲抵后受影响应收发票未核销余额非负且单调非增。",
            },
            {
                "name": "settle_rebate_voucher_post",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "DealerRebateSettlement",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "gt", "left": {"ref": "after.annual_sales_cents"}, "right": {"lit": 0}},
                        {"op": "ge", "left": {"ref": "after.rebate_cents"}, "right": {"lit": 0}},
                        {"op": "ge", "left": {"ref": "after.offset_invoice_count"}, "right": {"lit": 0}},
                    ],
                },
                "description": "新建返利结算凭证销售额正值、返利金额与冲抵笔数非负。",
            },
        ),
    ),
    EditFunctionDef(
        name="allocate_container_shipping_cost",
        params=AllocateContainerShippingCostParams,
        handler=_allocate_container_shipping_cost,
        description="按集装箱毛重比例跨多张拼箱出口订单精确分摊海运运费（尾差归口，分厘守恒）。",
        touched_object_types=("ExportSalesOrder",),
        modifies=("ExportSalesOrder.x_allocated_freight_cents",),
        trust_mode="untrusted_verifiable",
        require_read_set_cas=True,
        planner_function="plan_container_shipping_cost",
        ensures=(
            {
                "name": "allocate_freight_conservation_post",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "ExportSalesOrder",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {
                            "op": "ge",
                            "left": {"ref": "after.x_allocated_freight_cents"},
                            "right": {"lit": 0},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "op": "sum",
                                "object_type": "ExportSalesOrder",
                                "field": "x_allocated_freight_cents",
                                "where": {
                                    "op": "and",
                                    "args": [
                                        {
                                            "op": "eq",
                                            "left": {"ref": "target.x_container_no"},
                                            "right": {"ref": "param.container_no"},
                                        },
                                        {
                                            "op": "ne",
                                            "left": {"ref": "target.status"},
                                            "right": {"lit": "cancelled"},
                                        },
                                    ],
                                },
                                "default": 0,
                            },
                            "right": {"ref": "param.total_freight_cents"},
                        },
                    ],
                },
                "description": "拼箱出口海运费分摊后每单非负且整柜分摊总额严格守恒等于总运费。",
            },
        ),
    ),
)

_MEDAUTO_EF_ACTION_META: dict[str, dict[str, Any]] = {
    "apply_export_tax_rebate_claim": {
        "allowed_roles": ("finance", "finance_director", "admin"),
        "affects_source": False,
        "display_label": "核定出口退税申报",
    },
    "batch_recall_defective_lot": {
        "allowed_roles": ("plant_manager", "ops", "steward", "admin"),
        "affects_source": False,
        "display_label": "缺陷批次全局召回隔离",
    },
    "explode_multilevel_substitute_bom": {
        "allowed_roles": ("production_planner", "plant_manager", "ops", "admin"),
        "affects_source": True,
        "display_label": "多级替代料 BOM 展算投产",
    },
    "settle_annual_tiered_dealer_rebate": {
        "allowed_roles": ("finance_director", "chief_accountant", "finance", "admin"),
        "affects_source": True,
        "display_label": "结算年度阶梯返利并冲账",
    },
    "allocate_container_shipping_cost": {
        "allowed_roles": ("ops", "warehouse", "finance", "admin"),
        "affects_source": False,
        "display_label": "拼箱出口运费按重精确分摊",
    },
}

# ---------------------------------------------------------------------------
# 3. Display Dictionary (Pure L1 Console Localized Business Labels)
# ---------------------------------------------------------------------------
_MEDAUTO_DISPLAY_PATCH: dict[str, dict[str, str]] = {
    "ontologies": {
        "std_enterprise_world_model": "华美精密医疗与汽车部件出口制造二开世界模型（15 DOAL + 5 EditFunction）",
    },
    "objects": {
        "ExportSalesOrder": "出口贸易销售订单",
        "DomesticSampleOrder": "国内试制样品单",
        "ReworkWorkOrder": "不良返工工单",
        "QualityInspection": "首件质检单",
        "RegulatoryCert": "医疗/车规合规资质",
        "CustomsDeclaration": "出口报关单",
        "DealerRebateSettlement": "经销商年度阶梯返利凭证",
    },
    "fields": {
        "x_customs_no": "海关报关单号",
        "x_incoterms": "国际贸易术语 (Incoterms)",
        "x_destination_country": "目的国别代码",
        "x_lc_verified": "信用证 (L/C) 已核验",
        "x_discount_pct": "商业折扣 (%)",
        "x_risk_tier": "风控评级",
        "x_container_no": "拼箱集装箱号",
        "x_gross_weight_kg": "毛重 (kg)",
        "x_allocated_freight_cents": "已分摊海运费 (分)",
        "x_lot_code": "生产追溯批次号",
        "x_quarantined": "批次质量隔离锁定",
        "x_sample_qty": "免费样品数量",
        "x_sample_approved": "样品直发已特批",
        "x_rework_qty": "返修计划数量",
        "x_defect_reason": "不合格缺陷原因",
        "parent_work_order": "母生产工单",
        "inspection_id": "质检单编码",
        "inspection_code": "质检单号",
        "lot_code": "追溯批次号",
        "defect_count": "检出缺陷数",
        "inspector_note": "质检员备注",
        "rework_opened": "已触发返工",
        "work_order": "关联生产工单",
        "order": "关联销售订单",
        "cert_id": "合规证书编码",
        "cert_no": "证书编号",
        "cert_kind": "认证体系标准",
        "account": "关联客户账户",
        "declaration_id": "报关单编码",
        "customs_no": "海关报关单号",
        "customs_fee_cents": "报关规费 (分)",
        "rebate_amount_cents": "核定退税额 (分)",
        "rebate_claimed": "已申报退税",
        "export_order": "关联出口订单",
        "settlement_id": "返利结算编码",
        "settlement_code": "返利结算单号",
        "annual_sales_cents": "年度合格销售额 (分)",
        "rebate_cents": "阶梯返利总额 (分)",
        "offset_invoice_count": "FIFO 冲抵发票笔数",
        "x_credit_status": "客户信用控制状态",
        "rework_work_order_id": "返工工单编码",
        "rework_qty": "返工数量",
        "defect_reason": "缺陷说明",
        "rebate_rate": "出口退税率",
        "fx_rate": "结算汇率",
        "reason": "召回隔离原因",
        "parent_work_order_id": "母工单编码",
        "available_stock_json": "可用库存快照 JSON",
        "child_wo_prefix": "子工单编号前缀",
        "container_no": "集装箱号",
        "total_freight_cents": "整箱海运总运费 (分)",
    },
    "actions": {
        "submit_first_article_inspection": "录入首件质检结论",
        "verify_export_letter_of_credit": "核验出口信用证",
        "declare_customs_clearance": "申报出口报关放行",
        "flag_customer_credit_hold": "锁定客户逾期信用冻结",
        "classify_export_risk_tier": "出口订单风险三档定级",
        "open_rework_work_order": "不合格质检开立返工工单",
        "approve_sample_free_shipment": "特批免费样品直发确认",
        "apply_export_tax_rebate_claim": "核定出口退税申报",
        "batch_recall_defective_lot": "缺陷批次全局召回隔离",
        "explode_multilevel_substitute_bom": "多级替代料 BOM 展算投产",
        "settle_annual_tiered_dealer_rebate": "结算年度阶梯返利并冲账",
        "allocate_container_shipping_cost": "拼箱出口运费按重精确分摊",
    },
    "functions": {
        "plan_container_shipping_cost": "M2.5 纯算大脑 · 拼箱运费分摊提案",
        "plan_multilevel_substitute_bom": "M2.5 纯算大脑 · 多级替代料 BOM 展算提案",
        "plan_export_tax_rebate_claim": "M2.5 权威纯算大脑 · 法定出口退税核定提案",
    },
    "statuses": {
        "pending": "待处理/待检",
        "passed": "首件检验合格",
        "failed": "检验不合格",
        "valid": "证书有效",
        "expired": "证书已过期",
        "draft": "草稿",
        "confirmed": "已确认",
        "delivered": "已发货",
        "cleared": "海关已放行",
        "released": "已下达",
        "in_progress": "进行中",
        "completed": "已完成",
        "cancelled": "已取消",
        "posted": "已过账",
        "paid": "已结清",
        "partial": "部分结清",
        "not_paid": "待核销",
        "NORMAL": "正常授信",
        "HOLD": "信用冻结",
        "HIGH": "高风险",
        "MEDIUM": "中风险",
        "LOW": "低风险",
    },
    "values": {
        "NORMAL": "正常授信",
        "HOLD": "信用冻结",
        "HIGH": "高风险",
        "MEDIUM": "中风险",
        "LOW": "低风险",
        "IATF_16949": "IATF 16949 车规认证",
        "MDR_CE_0123": "欧盟 MDR CE 医疗器械认证",
        "FOB": "FOB 离岸价",
        "CIF": "CIF 到岸价",
    },
    "roles": {
        "finance_director": "财务总监 (CFO)",
        "plant_manager": "制造厂长",
        "production_planner": "主生产计划员",
        "warehouse": "关务/仓储主管",
        "sales_manager": "出口销售总监",
        "sales_rep": "大客户销售经理",
        "finance": "国际结算与税务会计",
        "chief_accountant": "总账财务主管",
        "ops": "质量与运营协同主管",
        "steward": "合规治理专员",
        "ai_agent": "AI 智能执行体",
        "admin": "系统实施管理员",
    },
    "errors": {
        "EXPORT_DISCOUNT_CAP_EXCEEDED": "红线拦截：出口订单商业折扣严禁超过 12%。",
        "REWORK_EXCEEDS_PLANNED_QTY": "红线拦截：返工工单返修数量不得超过母工单计划产量。",
        "EXPORT_HIGH_VALUE_REQUIRES_LC": "红线拦截：金额超 ¥500,000 的出口订单未核验信用证前严禁确认放行。",
        "DELIVERY_MISSING_PASSED_FAI": "红线拦截：该订单尚未关联已通过（passed）的首件质检单，严禁发货出库。",
        "CUSTOMER_REGULATORY_CERT_EXPIRED": "红线拦截：客户名下存在已过期的医疗/车规合规资质证书，严禁确认新订单。",
    },
    "invariants": {
        "export_discount_cap": "出口销售订单商业折扣比例严禁超过 12%。",
        "rework_qty_within_planned": "返工工单返修数量不得超过母工单计划产量。",
        "export_high_value_requires_lc": "金额大于 500,000 的非草稿出口销售订单必须完成信用证（L/C）核验。",
        "delivery_requires_passed_fai": "完成发货确认前，订单必须存在一条已通过（passed）的首件质检记录。",
        "cert_not_expired_blocks_confirm": "确认销售订单前，关联客户往来主体下不得存在已过期的合规资质证书。",
    },
}


def build_medauto_spec() -> OntologySpec:
    """Assemble the monotonic MedAuto secondary-development OntologySpec (43 actions: 29 Rule + 14 EditFn)."""
    base_spec = build_enterprise_standard_spec()
    derived = apply_overlay_to_spec(
        base_spec,
        MEDAUTO_DOAL_OVERLAY,
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        custom_edit_functions=MEDAUTO_CUSTOM_EDIT_FUNCTIONS,
    )
    for ef in MEDAUTO_CUSTOM_EDIT_FUNCTIONS:
        meta = _MEDAUTO_EF_ACTION_META[ef.name]
        derived.declare_action(
            ef.name,
            edit_function=ef.name,
            allowed_roles=meta["allowed_roles"],
            affects_source=bool(meta["affects_source"]),
            description=ef.description,
            touched_object_types=ef.touched_object_types,
        )

    # Register M2.5 strictly read-only Pure Planners (zero write handle; emit WriteBatchProposal)
    derived.function(
        "plan_container_shipping_cost",
        params=AllocateContainerShippingCostParams,
        description="M2.5 纯算大脑（零写句柄）：按集装箱毛重比例试算拼箱海运费分摊提案（WriteBatchProposal + Read-Set CAS）。",
    )(_plan_container_shipping_cost)
    derived.function(
        "plan_multilevel_substitute_bom",
        params=ExplodeMultilevelSubstituteBomParams,
        description="M2.5 纯算大脑（零写句柄）：递归展算多级替代料 BOM 并输出子工单创建提案（WriteBatchProposal + Read-Set CAS）。",
    )(_plan_multilevel_substitute_bom)
    derived.function(
        "plan_export_tax_rebate_claim",
        params=ApplyExportTaxRebateParams,
        description="M2.5 权威纯算大脑（Class B · 零写句柄）：按法定退税率与汇率试算报关单退税提案供契约门禁签验。",
    )(_plan_export_tax_rebate_claim)

    for src in (
        std_core_spec,
        std_crm_spec,
        std_o2c_spec,
        std_p2p_spec,
        std_p2m_spec,
        std_r2r_spec,
        std_h2r_spec,
    ):
        if src.display:
            derived.update_display(src.display)
    derived.update_display(_MEDAUTO_DISPLAY_PATCH)
    enforce_edit_function_contracts(derived)
    return derived


def build_medauto_blueprint() -> DeclarativeBlueprint:
    """Export the full MedAuto derived OntologySpec as a DeclarativeBlueprint for Z3/L1 inspection."""
    return export_spec_blueprint(
        build_medauto_spec(),
        model_id="medauto_export_mfg_world",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="华美精密医疗与汽车部件出口制造二开包蓝图（31 标准底座 + 15 DOAL + 5 EditFunction = 43 动作）。",
    )


# ---------------------------------------------------------------------------
# 4. Seed World & Presets
# ---------------------------------------------------------------------------
MEDAUTO_WORK_QUEUE = WorkQueueBinding(
    object_type="WorkCard",
    id_field="card_id",
    kind_field="card_kind",
    status_field="status",
    active_statuses=("OPEN", "IN_PROGRESS", "SLA_BREACHED"),
    target_type_field="target_object_type",
    target_id_field="target_object_id",
    intent_action_field="requested_action",
    intent_params_field="requested_params_json",
    requester_field="requester_actor_id",
    assignee_role_field="assignee_role",
    accept_op="complete_work_card",
    decline_op="reject_work_card",
    claim_op="claim_work_card",
    id_param="card_id",
    note_param="resolution_note",
    settled_run_param="settled_run_id",
)

MEDAUTO_QUICK_PROMPTS: list[str] = [
    "当前有哪些出口订单、首件质检单与待核验信用证？",
    "批次 LOT-2026-09 涉及多少张生产工单和发货单？",
    "在沙盘中试算集装箱 CNTR-DE-01 按毛重分摊海运运费",
    "查看不良首件质检单 QI-003 派生返工工单",
]

MEDAUTO_SANDBOX_PRESETS: list[dict[str, Any]] = [
    {
        "preset_id": "medauto_doal_fai_and_risk",
        "title": "DOAL 场景：首件质检放行与高额出口单风险定级",
        "subtitle": "对 QI-001 录入 0 缺陷通过首件质检，并对 ¥680,000 出口大单 EXP-SO-001 执行三档风险定级（HIGH）。",
        "badge": "DOAL 声明式推演",
        "steps": [
            {
                "action": "submit_first_article_inspection",
                "params": {
                    "inspection_id": "QI-001",
                    "defect_count": 0,
                    "inspector_note": "三坐标首件全尺寸合格，准予放行发货",
                },
            },
            {
                "action": "classify_export_risk_tier",
                "params": {"order_id": "EXP-SO-001"},
            },
        ],
    },
    {
        "preset_id": "medauto_doal_rework_wo",
        "title": "DOAL 场景：不合格首件自动派生返工工单",
        "subtitle": "从已失败质检单 QI-003（3 件毛刺缺陷）自动复制母工单配方创建返工工单 RWO-MED-001。",
        "badge": "DOAL 声明式派工",
        "steps": [
            {
                "action": "open_rework_work_order",
                "params": {
                    "inspection_id": "QI-003",
                    "rework_work_order_id": "RWO-MED-001",
                    "rework_qty": 10,
                    "defect_reason": "钛合金导管内壁激光毛刺超差，需二次精密抛光返修",
                },
            },
        ],
    },
    {
        "preset_id": "medauto_ef_batch_recall",
        "title": "EditFunction 场景：LOT-2026-09 缺陷批次全局召回隔离",
        "subtitle": "跨所有工单与发货单全表扫描 LOT-2026-09 并原子锁定隔离、生成厂长追踪协同工单。",
        "badge": "图灵完备 · 批量闭环",
        "steps": [
            {
                "action": "batch_recall_defective_lot",
                "params": {
                    "lot_code": "LOT-2026-09",
                    "reason": "德国客户端抽检发现同批次微裂纹风险，全链路紧急隔离复检",
                },
            },
        ],
    },
    {
        "preset_id": "medauto_ef_container_freight",
        "title": "EditFunction 场景：CNTR-DE-01 拼箱海运费按毛重分摊",
        "subtitle": "按 1200kg : 800kg 毛重比向 EXP-SO-001 与 EXP-SO-002 精确分摊 ¥1,000.03 整箱海运费（尾差分毫不差）。",
        "badge": "图灵完备 · 运费分摊",
        "steps": [
            {
                "action": "allocate_container_shipping_cost",
                "params": {
                    "container_no": "CNTR-DE-01",
                    "total_freight_cents": 100003,
                },
            },
        ],
    },
]


def seed_medauto_world(rt: OntologyRuntime, admin: Actor) -> dict[str, Any]:
    """Populate realistic MedAuto export manufacturing & precision quality scenarios."""
    spec = rt.spec
    EntCls = spec.get_object_type("AccountingEntity")
    PartyCls = spec.get_object_type("BusinessParty")
    ResCls = spec.get_object_type("TradeResource")
    AccCls = spec.get_object_type("CrmAccount")
    CustCls = spec.get_object_type("O2cCustomerProfile")
    CertCls = spec.get_object_type("RegulatoryCert")
    BomCls = spec.get_object_type("BomSpec")
    WoCls = spec.get_object_type("WorkOrder")
    SoCls = spec.get_object_type("O2cOrder")
    ExpCls = spec.get_object_type("ExportSalesOrder")
    SampleCls = spec.get_object_type("DomesticSampleOrder")
    DelCls = spec.get_object_type("O2cDelivery")
    InvCls = spec.get_object_type("O2cInvoice")
    QiCls = spec.get_object_type("QualityInspection")
    DeclCls = spec.get_object_type("CustomsDeclaration")

    rt.put_object(
        EntCls(
            entity_id="ACC-ENT-MEDAUTO",
            name="华美精密医疗与汽车部件出口有限公司",
            base_currency="CNY",
        ),
        actor=admin,
    )

    for pid, lname, tid, cc in (
        ("PARTY-DE-MEDTECH", "慕尼黑精密医疗器械集团 (Munich MedTech GmbH)", "DE294810221", "DE"),
        ("PARTY-JP-AUTO", "名古屋精工电装株式会社 (Nagoya AutoParts KK)", "JP7010401055", "JP"),
        ("PARTY-CN-HOSP", "上海瑞康临床医疗中心", "91310115MA09RUIKANG", "CN"),
    ):
        rt.put_object(
            PartyCls(party_id=pid, legal_name=lname, tax_id=tid, country_code=cc),
            actor=admin,
        )

    rt.put_object(
        AccCls(
            account_id="ACC-DE-01",
            name="慕尼黑医疗出口大客户",
            party="PARTY-DE-MEDTECH",
            industry="骨科微创植入物与精密导管",
            x_credit_status="NORMAL",
        ),
        actor=admin,
    )
    rt.put_object(
        AccCls(
            account_id="ACC-JP-02",
            name="名古屋车载传感器客户",
            party="PARTY-JP-AUTO",
            industry="新能源汽车激光雷达壳体",
            x_credit_status="NORMAL",
        ),
        actor=admin,
    )
    rt.put_object(
        AccCls(
            account_id="ACC-CN-03",
            name="上海瑞康临床试制客户",
            party="PARTY-CN-HOSP",
            industry="三甲临床微创耗材试制",
            x_credit_status="NORMAL",
        ),
        actor=admin,
    )

    rt.put_object(
        CustCls(
            profile_id="CUST-DE-01",
            party="PARTY-DE-MEDTECH",
            accounting_entity="ACC-ENT-MEDAUTO",
            credit_limit=3_000_000.0,
            payment_terms="LC_60D",
        ),
        actor=admin,
    )
    rt.put_object(
        CustCls(
            profile_id="CUST-JP-02",
            party="PARTY-JP-AUTO",
            accounting_entity="ACC-ENT-MEDAUTO",
            credit_limit=1_500_000.0,
            payment_terms="NET30",
        ),
        actor=admin,
    )
    rt.put_object(
        CustCls(
            profile_id="CUST-CN-03",
            party="PARTY-CN-HOSP",
            accounting_entity="ACC-ENT-MEDAUTO",
            credit_limit=500_000.0,
            payment_terms="PREPAID",
        ),
        actor=admin,
    )

    # Regulatory compliance certificates (valid DE + expired JP red-line)
    rt.put_object(
        CertCls(
            cert_id="CERT-VALID-01",
            cert_no="IATF-16949-DE-2026-001",
            cert_kind="IATF_16949",
            account="ACC-DE-01",
            status="valid",
        ),
        actor=admin,
    )
    rt.put_object(
        CertCls(
            cert_id="CERT-EXP-02",
            cert_no="MDR-CE-0123-EXP-2025",
            cert_kind="MDR_CE_0123",
            account="ACC-JP-02",
            status="expired",
        ),
        actor=admin,
    )

    # Trade resources + multi-level BOM with substitute fallback
    for rid, code, name, unit, summary in (
        ("RES-TI-IMPLANT", "MED-IMP-900", "钛合金骨科微创植入导管组件", "SET", "EU MDR Class IIb 出口成品"),
        ("RES-CNC-CORE", "SEMI-CNC-420", "五轴精密 CNC 导管芯轴半成品", "PCS", "公差 ±0.005mm"),
        ("RES-TI-BAR-DE", "TI-6AL4V-DE", "进口德标医用钛棒（主料）", "KG", "主选供应商牌号"),
        ("RES-TI-BAR-CN", "TI-6AL4V-CN", "国产航空级医用钛棒（替代料）", "KG", "主料短缺自动切换替代料"),
        ("RES-TI-SPONGE", "TI-RAW-SPG", "海绵钛坯料", "KG", "底层冶金原料"),
    ):
        rt.put_object(
            ResCls(resource_id=rid, code=code, name=name, unit=unit, spec_summary=summary),
            actor=admin,
        )

    for bid, out_r, in_r, per_u, ver in (
        ("BOM-IMPLANT-L1", "RES-TI-IMPLANT", "RES-CNC-CORE", 1.0, "1.0"),
        ("BOM-CORE-PRI", "RES-CNC-CORE", "RES-TI-BAR-DE", 1.5, "1.0"),
        ("BOM-CORE-SUB", "RES-CNC-CORE", "RES-TI-BAR-CN", 1.5, "2.0-SUB"),
        ("BOM-BAR-CN", "RES-TI-BAR-CN", "RES-TI-SPONGE", 2.0, "1.0"),
    ):
        rt.put_object(
            BomCls(
                bom_id=bid,
                output_resource=out_r,
                input_resource=in_r,
                input_per_unit=per_u,
                max_scrap_ratio=0.05,
                version=ver,
                status="active",
            ),
            actor=admin,
        )

    # WorkOrders (2 sharing LOT-2026-09 + 1 draft for multi-level BOM explosion)
    rt.put_object(
        WoCls(
            work_order_id="WO-MED-001",
            accounting_entity="ACC-ENT-MEDAUTO",
            bom="BOM-IMPLANT-L1",
            output_resource="RES-TI-IMPLANT",
            planned_qty=50.0,
            x_lot_code="LOT-2026-09",
            status="draft",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-WO-MED-001-REL", "release_work_order", {"work_order_id": "WO-MED-001"}, admin)
    rt.submit_action(
        "SEED-WO-MED-001-ISS",
        "issue_material_to_work_order",
        {
            "consumption_id": "CONS-MED-001",
            "work_order_id": "WO-MED-001",
            "input_resource": "RES-CNC-CORE",
            "qty": 50.0,
            "unit_cost_cents": 25_000,
        },
        admin,
    )

    rt.put_object(
        WoCls(
            work_order_id="WO-MED-002",
            accounting_entity="ACC-ENT-MEDAUTO",
            bom="BOM-CORE-PRI",
            output_resource="RES-CNC-CORE",
            planned_qty=30.0,
            x_lot_code="LOT-2026-09",
            status="draft",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-WO-MED-002-REL", "release_work_order", {"work_order_id": "WO-MED-002"}, admin)

    rt.put_object(
        WoCls(
            work_order_id="WO-MED-003",
            accounting_entity="ACC-ENT-MEDAUTO",
            bom="BOM-IMPLANT-L1",
            output_resource="RES-TI-IMPLANT",
            planned_qty=20.0,
            x_lot_code="LOT-2026-10",
            status="draft",
        ),
        actor=admin,
    )

    # Concrete O2cOrders for FAI / Delivery / Rebate / Expired Cert tests
    rt.put_object(
        SoCls(
            order_id="SO-STD-001",
            order_number="SO-MED-2026-001（德国骨科导管主单·待首检发货）",
            customer="CUST-DE-01",
            accounting_entity="ACC-ENT-MEDAUTO",
            amount_total=420_000.0,
            status="draft",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-SO01-CONF", "confirm_o2c_order", {"order_id": "SO-STD-001"}, admin)

    rt.put_object(
        SoCls(
            order_id="SO-STD-002",
            order_number="SO-MED-2026-002（慕尼黑车规部件框架·已发货开票）",
            customer="CUST-DE-01",
            accounting_entity="ACC-ENT-MEDAUTO",
            amount_total=860_000.0,
            status="draft",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-SO02-CONF", "confirm_o2c_order", {"order_id": "SO-STD-002"}, admin)

    rt.put_object(
        SoCls(
            order_id="SO-JP-BLOCKED-01",
            order_number="SO-JP-2026-099（名古屋车载传感器·证书过期阻断样本）",
            customer="CUST-JP-02",
            accounting_entity="ACC-ENT-MEDAUTO",
            amount_total=150_000.0,
            status="draft",
        ),
        actor=admin,
    )

    # QualityInspections: QI-001 (pending), QI-002 (transitioned passed), QI-003 (transitioned failed)
    rt.put_object(
        QiCls(
            inspection_id="QI-001",
            inspection_code="FAI-2026-001",
            work_order="WO-MED-001",
            order="SO-STD-001",
            lot_code="LOT-2026-09",
            status="pending",
        ),
        actor=admin,
    )
    rt.put_object(
        QiCls(
            inspection_id="QI-002",
            inspection_code="FAI-2026-002",
            work_order="WO-MED-002",
            order="SO-STD-002",
            lot_code="LOT-2026-09",
            status="pending",
        ),
        actor=admin,
    )
    rt.submit_action(
        "SEED-QI02-PASS",
        "submit_first_article_inspection",
        {"inspection_id": "QI-002", "defect_count": 0, "inspector_note": "首件尺寸全检合格"},
        admin,
    )
    rt.put_object(
        QiCls(
            inspection_id="QI-003",
            inspection_code="FAI-2026-003",
            work_order="WO-MED-001",
            lot_code="LOT-2026-09",
            status="pending",
        ),
        actor=admin,
    )
    rt.submit_action(
        "SEED-QI03-FAIL",
        "submit_first_article_inspection",
        {"inspection_id": "QI-003", "defect_count": 3, "inspector_note": "内腔毛刺超差 3 处"},
        admin,
    )

    # Deliveries (LOT-2026-09) & Posted Unpaid Invoices (CUST-DE-01)
    rt.put_object(
        DelCls(
            delivery_id="DLV-MED-001",
            order="SO-STD-001",
            x_lot_code="LOT-2026-09",
            status="pending",
        ),
        actor=admin,
    )
    rt.put_object(
        DelCls(
            delivery_id="DLV-MED-002",
            order="SO-STD-002",
            x_lot_code="LOT-2026-09",
            status="pending",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-DLV02-COMP", "complete_o2c_delivery", {"delivery_id": "DLV-MED-002"}, admin)

    rt.put_object(
        InvCls(
            invoice_id="INV-DE-001",
            order="SO-STD-001",
            amount_total=420_000.0,
            amount_residual=18_000.0,
            payment_state="partial",
            status="draft",
        ),
        actor=admin,
    )
    # Seed posted invoice directly after confirming delivery-backed SO-STD-002; INV-DE-001 requires SO-STD-001 delivered if posted via post_o2c_invoice,
    # so put posted state cleanly or post INV-DE-002 after delivery and put INV-DE-001 posted directly:
    rt.put_object(
        InvCls(
            invoice_id="INV-DE-001",
            order="SO-STD-001",
            amount_total=420_000.0,
            amount_residual=18_000.0,
            payment_state="partial",
            status="posted",
        ),
        actor=admin,
    )
    rt.put_object(
        InvCls(
            invoice_id="INV-DE-002",
            order="SO-STD-002",
            amount_total=300_000.0,
            amount_residual=80_000.0,
            payment_state="not_paid",
            status="draft",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-INV02-POST", "post_o2c_invoice", {"invoice_id": "INV-DE-002"}, admin)

    # Subtyped ExportSalesOrders sharing container CNTR-DE-01 + DomesticSampleOrder
    rt.put_object(
        ExpCls(
            order_id="EXP-SO-001",
            order_number="EXP-DE-2026-001（德国骨科器械大单·未验L/C）",
            customer="CUST-DE-01",
            accounting_entity="ACC-ENT-MEDAUTO",
            amount_total=680_000.0,
            status="draft",
            x_incoterms="CIF",
            x_destination_country="DE",
            x_lc_verified=False,
            x_discount_pct=8,
            x_risk_tier="LOW",
            x_container_no="CNTR-DE-01",
            x_gross_weight_kg=1200,
            x_lot_code="LOT-2026-09",
        ),
        actor=admin,
    )
    rt.put_object(
        ExpCls(
            order_id="EXP-SO-002",
            order_number="EXP-DE-2026-002（德国微创组件·已出运待报关）",
            customer="CUST-DE-01",
            accounting_entity="ACC-ENT-MEDAUTO",
            amount_total=320_000.0,
            status="delivered",
            x_incoterms="FOB",
            x_destination_country="DE",
            x_lc_verified=True,
            x_discount_pct=5,
            x_risk_tier="MEDIUM",
            x_container_no="CNTR-DE-01",
            x_gross_weight_kg=800,
            x_lot_code="LOT-2026-09",
        ),
        actor=admin,
    )
    rt.put_object(
        SampleCls(
            order_id="SMP-SO-001",
            order_number="SMP-CN-2026-001（上海瑞康临床免费试制样品）",
            customer="CUST-CN-03",
            accounting_entity="ACC-ENT-MEDAUTO",
            amount_total=1_200.0,
            status="draft",
            x_sample_qty=3,
            x_sample_approved=False,
        ),
        actor=admin,
    )

    rt.put_object(
        DeclCls(
            declaration_id="CD-001",
            customs_no="CUS-2026-8801",
            export_order="EXP-SO-002",
            customs_fee_cents=0,
            rebate_amount_cents=0,
            rebate_claimed=False,
            status="draft",
        ),
        actor=admin,
    )

    profiles = [
        DemoActorProfile(
            actor_id="act-admin",
            display_name="实施架构师",
            role="admin",
            title="二开系统实施总监",
            badge="全量实体与 Z3 形式化蓝图权限（Pure L1 验证视图）",
            token="tok-medauto-admin",
            clearance_tags=("*",),
        ),
        DemoActorProfile(
            actor_id="act-cfo",
            display_name="林总 (CFO)",
            role="finance_director",
            title="出口贸易财务总监",
            badge="负责信用证核验、出口退税核定与经销商年度阶梯返利",
            token="tok-medauto-cfo",
            clearance_tags=("*",),
        ),
        DemoActorProfile(
            actor_id="act-pm",
            display_name="高厂长",
            role="plant_manager",
            title="精密制造质量厂长",
            badge="负责首件质检裁决、不良返工派单与缺陷批次全局召回隔离",
            token="tok-medauto-pm",
            clearance_tags=("*",),
        ),
        DemoActorProfile(
            actor_id="act-ops",
            display_name="唐主管",
            role="ops",
            title="关务物流协同主管",
            badge="负责出口清关申报、拼箱海运费分摊与履约发运协同",
            token="tok-medauto-ops",
            clearance_tags=("*",),
        ),
    ]
    return {"profiles": profiles}


def build_medauto_console(
    *,
    db_path: str | Path = ":memory:",
    demo_actor_switch: bool = True,
    enable_llm_assistant: bool | None = None,
) -> ConsoleService:
    """Mount the MedAuto 15-DOAL / 5-EditFunction secondary-dev pack on Pure L1 Shell (pack_hint='none')."""
    spec = build_medauto_spec()
    rt = OntologyRuntime(spec, db_path=str(db_path), world_mode="standalone")
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    seeded = seed_medauto_world(rt, admin)
    profiles: list[DemoActorProfile] = list(seeded["profiles"])

    return ConsoleService(
        rt,
        default_actor=admin,
        demo_actors=profiles,
        demo_actor_switch=demo_actor_switch,
        app_title="华美精密出口制造二开孪生台（Pure L1 Shell）",
        app_subtitle="15 项 DOAL 声明式扩展 + 5 个客户 EditFunction · 零领域硬编码 L1 壳实时托管",
        pack_hint="none",
        work_queue_binding=MEDAUTO_WORK_QUEUE,
        sandbox_presets=MEDAUTO_SANDBOX_PRESETS,
        quick_prompts=MEDAUTO_QUICK_PROMPTS,
        assistant_welcome="您好 {display_name}！我是运行在纯净 L1 通用本体壳（Zero-Pack）上的二开助教助手，可为您穿透对象继承图、运行 Z3 体检与平行沙盘预演。",
        value_unit_label="¥",
        enable_llm_assistant=enable_llm_assistant,
    )
