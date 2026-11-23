"""WO-28 Quantitative Benchmark: ``design prove`` (Z3 SMT) vs Schema-Aware Random Fuzzing.

Builds 12 paired multi-department enterprise scenarios (6 real defect classes C1-C6
and 6 conflict-free clean controls N1-N6) plus a 4-tier rule scale ladder
(10 -> 25 -> 50 -> 100 multi-department rules across Sales, Finance, Procurement,
Production, and Risk/Compliance) atop ``std_enterprise_world_model``.

Outputs structured benchmark metrics to ``/tmp/wo28_prover_benchmark_report.json``
and prints reproducible Markdown comparison tables.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any, Mapping, Sequence

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from open_ontology import Actor, OntologyRuntime
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    apply_overlay,
    compile_blueprint,
    eval_predicate,
    prove_blueprint,
    prove_upgrade_compatibility,
)

try:
    from .business_ontology import BUSINESS_SEED_ONTOLOGY
    from .std_enterprise_world_model import build_enterprise_standard_blueprint
except ImportError:  # pragma: no cover
    from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
    from examples.std_enterprise_world_model import build_enterprise_standard_blueprint

REPORT_OUTPUT_PATH = Path("/tmp/wo28_prover_benchmark_report.json")


def _lit(val: Any) -> dict[str, Any]:
    return {"lit": val}


def _ref(path: str) -> dict[str, Any]:
    return {"ref": path}


def _op(name: str, left: Any, right: Any) -> dict[str, Any]:
    return {"op": name, "left": left, "right": right}


def _and(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "and", "args": list(args)}


def _not(arg: dict[str, Any]) -> dict[str, Any]:
    return {"op": "not", "arg": arg}


def _extend_enterprise_governance_fields(bp: DeclarativeBlueprint) -> DeclarativeBlueprint:
    """Attach realistic enterprise governance attributes via additive overlay."""
    gov_overlay = CustomerOverlaySpec(
        overlay_id="enterprise_governance_attrs",
        description="Additive governance fields for multi-department SLA, tier, channel, and tax policies.",
        add_roles=["tax_controller", "compliance_officer"],
        add_fields={
            "O2cOrder": [
                FieldSpec(name="vip_tier", field_type="str", default="STANDARD"),
                FieldSpec(name="sales_channel", field_type="str", default="DIRECT"),
                FieldSpec(name="region", field_type="str", default="CN_EAST"),
                FieldSpec(name="payment_terms", field_type="str", default="NET30"),
                FieldSpec(name="discount_rate", field_type="float", default=0.0, non_negative=True),
                FieldSpec(name="tax_clearance_code", field_type="str", default=""),
                FieldSpec(name="risk_band", field_type="str", default="LOW"),
                FieldSpec(name="margin_rate", field_type="float", default=0.25, non_negative=True),
                FieldSpec(name="contract_category", field_type="str", default="GOODS"),
            ],
            "O2cInvoice": [
                FieldSpec(name="vip_tier", field_type="str", default="STANDARD"),
                FieldSpec(name="region", field_type="str", default="CN_EAST"),
                FieldSpec(name="tax_verified", field_type="bool", default=True),
            ],
            "P2pPurchaseOrder": [
                FieldSpec(name="sourcing_mode", field_type="str", default="CONTRACT"),
                FieldSpec(name="region", field_type="str", default="CN_EAST"),
                FieldSpec(name="risk_band", field_type="str", default="LOW"),
                FieldSpec(name="budget_code", field_type="str", default="OPEX"),
            ],
            "P2pVendorBill": [
                FieldSpec(name="payment_channel", field_type="str", default="WIRE"),
                FieldSpec(name="region", field_type="str", default="CN_EAST"),
            ],
            "WorkOrder": [
                FieldSpec(name="priority_tier", field_type="str", default="NORMAL"),
                FieldSpec(name="plant_zone", field_type="str", default="ZONE_A"),
            ],
        },
        restrict_allowed_roles={},
        add_action_preconditions={},
    )
    out = apply_overlay(bp, gov_overlay)
    # Grant newly declared enterprise roles on actions where overlays may tighten/intersect
    for act in out.actions:
        if act.name in ("confirm_o2c_order", "post_o2c_invoice", "confirm_p2p_order"):
            for r in ("tax_controller", "compliance_officer"):
                if r not in act.allowed_roles:
                    act.allowed_roles.append(r)
    return out


def _base_enterprise_bp() -> DeclarativeBlueprint:
    return _extend_enterprise_governance_fields(build_enterprise_standard_blueprint())


# ---------------------------------------------------------------------------
# 1. Six Paired Defect Scenarios (C1-C6) + Six Clean Controls (N1-N6)
# ---------------------------------------------------------------------------
def build_paired_scenarios() -> list[dict[str, Any]]:
    """Build 6 real enterprise defect scenarios paired with 6 conflict-free clean controls."""
    base = _base_enterprise_bp()
    scenarios: list[dict[str, Any]] = []

    # C1 / N1: Global deadlock vs clean threshold split
    c1_ov = CustomerOverlaySpec(
        overlay_id="c1_global_deadlock",
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="fin_min_wholesale_floor",
                    code="FIN_BELOW_WHOLESALE_FLOOR",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold=_op("ge", _ref("obj.amount_total"), _lit(50000.0)),
                ),
                InvariantRuleSpec(
                    name="risk_non_director_ceiling",
                    code="RISK_CEILING_EXCEEDED",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold=_op("le", _ref("obj.amount_total"), _lit(20000.0)),
                ),
            ]
        },
    )
    n1_ov = CustomerOverlaySpec(
        overlay_id="n1_clean_threshold",
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="fin_min_wholesale_floor",
                    code="FIN_BELOW_WHOLESALE_FLOOR",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold=_op("ge", _ref("obj.amount_total"), _lit(1000.0)),
                ),
                InvariantRuleSpec(
                    name="risk_sales_rep_ceiling",
                    code="RISK_CEILING_EXCEEDED",
                    target_type="O2cOrder",
                    id_param="order_id",
                    when=_op("gt", _ref("obj.amount_total"), _lit(20000.0)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["sales_manager", "finance_director", "admin"])),
                ),
            ]
        },
    )
    scenarios.append(
        {
            "id": "C1",
            "category": "TOTAL_DEADLOCK",
            "target_action": "confirm_o2c_order",
            "expect_defect": True,
            "blueprint": apply_overlay(base, c1_ov),
            "expected_core": {"fin_min_wholesale_floor", "risk_non_director_ceiling"},
        }
    )
    scenarios.append(
        {
            "id": "N1",
            "category": "CLEAN_CONTROL_C1",
            "target_action": "confirm_o2c_order",
            "expect_defect": False,
            "blueprint": apply_overlay(base, n1_ov),
        }
    )

    # C2 / N2: Narrow 3-department conditional deadlock vs clean 3-department control
    def _make_3dept_overlay(*, finance_roles: list[str], ov_id: str) -> CustomerOverlaySpec:
        return CustomerOverlaySpec(
            overlay_id=ov_id,
            add_action_preconditions={
                "confirm_o2c_order": [
                    InvariantRuleSpec(
                        name="sales_strategic_high_discount_owner",
                        code="SALES_DISCOUNT_APPROVAL",
                        target_type="O2cOrder",
                        id_param="order_id",
                        when=_and(
                            _op("eq", _ref("obj.vip_tier"), _lit("STRATEGIC")),
                            _op("gt", _ref("obj.discount_rate"), _lit(0.15)),
                        ),
                        must_hold=_op("in", _ref("actor.role"), _lit(["sales_manager", "ops"])),
                    ),
                    InvariantRuleSpec(
                        name="finance_strategic_mega_order_owner",
                        code="FINANCE_MEGA_APPROVAL",
                        target_type="O2cOrder",
                        id_param="order_id",
                        when=_and(
                            _op("eq", _ref("obj.vip_tier"), _lit("STRATEGIC")),
                            _op("gt", _ref("obj.amount_total"), _lit(200000.0)),
                        ),
                        must_hold=_op("in", _ref("actor.role"), _lit(finance_roles)),
                    ),
                    InvariantRuleSpec(
                        name="tax_high_value_discount_clearance",
                        code="TAX_CLEARANCE_REQUIRED",
                        target_type="O2cOrder",
                        id_param="order_id",
                        when=_and(
                            _op("gt", _ref("obj.amount_total"), _lit(200000.0)),
                            _op("gt", _ref("obj.discount_rate"), _lit(0.15)),
                        ),
                        must_hold=_op("ne", _ref("obj.tax_clearance_code"), _lit("")),
                    ),
                ]
            },
        )

    scenarios.append(
        {
            "id": "C2",
            "category": "NARROW_SLICE_3DEPT_CONDITIONAL_DEADLOCK",
            "target_action": "confirm_o2c_order",
            "expect_defect": True,
            "blueprint": apply_overlay(
                base,
                _make_3dept_overlay(finance_roles=["finance_director"], ov_id="c2_3dept_deadlock"),
            ),
            "expected_core": {"sales_strategic_high_discount_owner", "finance_strategic_mega_order_owner"},
        }
    )
    scenarios.append(
        {
            "id": "N2",
            "category": "CLEAN_CONTROL_C2",
            "target_action": "confirm_o2c_order",
            "expect_defect": False,
            "blueprint": apply_overlay(
                base,
                _make_3dept_overlay(
                    finance_roles=["finance_director", "sales_manager"], ov_id="n2_3dept_clean"
                ),
            ),
        }
    )

    # C3 / N3: Standard library v1 -> v2 upgrade deadlock regression vs compatible control
    cust_expedite_ov = CustomerOverlaySpec(
        overlay_id="cust_expedite_dropship",
        add_action_preconditions={
            "post_p2p_bill": [
                InvariantRuleSpec(
                    name="cust_dropship_bill_before_receipt",
                    code="DROPSHIP_REQUIRES_CONFIRMED_PO",
                    target_type="P2pVendorBill",
                    id_param="bill_id",
                    when=_op("eq", _ref("obj.payment_channel"), _lit("DROPSHIP_EXPRESS")),
                    must_hold=_op("eq", _ref("obj.purchase_order.status"), _lit("confirmed")),
                )
            ]
        },
    )
    v1_bp = copy.deepcopy(base)
    v2_tight = apply_overlay(
        base,
        CustomerOverlaySpec(
            overlay_id="std_v2_mandatory_receipt",
            target_version="2.0.0",
            add_action_preconditions={
                "post_p2p_bill": [
                    InvariantRuleSpec(
                        name="v2_std_receipt_required_before_bill",
                        code="RECEIPT_REQUIRED_BEFORE_BILL",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold=_op("eq", _ref("obj.purchase_order.status"), _lit("received")),
                    )
                ]
            },
        ),
    )
    v2_compat = apply_overlay(
        base,
        CustomerOverlaySpec(
            overlay_id="std_v2_non_disputed_only",
            target_version="2.0.0",
            add_action_preconditions={
                "post_p2p_bill": [
                    InvariantRuleSpec(
                        name="v2_std_no_disputed_bill",
                        code="PO_DISPUTED_BLOCKED",
                        target_type="P2pVendorBill",
                        id_param="bill_id",
                        must_hold=_op("ne", _ref("obj.purchase_order.status"), _lit("disputed")),
                    )
                ]
            },
        ),
    )
    scenarios.append(
        {
            "id": "C3",
            "category": "UPGRADE_REGRESSION_V1_TO_V2",
            "target_action": "post_p2p_bill",
            "expect_defect": True,
            "mode": "upgrade",
            "base_v1": v1_bp,
            "base_v2": v2_tight,
            "overlay": cust_expedite_ov,
            "blueprint": apply_overlay(v2_tight, cust_expedite_ov),
        }
    )
    scenarios.append(
        {
            "id": "N3",
            "category": "CLEAN_CONTROL_C3",
            "target_action": "post_p2p_bill",
            "expect_defect": False,
            "mode": "upgrade",
            "base_v1": v1_bp,
            "base_v2": v2_compat,
            "overlay": cust_expedite_ov,
            "blueprint": apply_overlay(v2_compat, cust_expedite_ov),
        }
    )

    # C4 / N4: Buggy AI Rule Action (negative residual + broken double-entry balance) vs verified fix
    def _make_rule_action_overlay(*, buggy: bool, ov_id: str) -> CustomerOverlaySpec:
        credit_pre = [
            InvariantRuleSpec(
                name="invoice_must_be_posted",
                code="INVOICE_NOT_POSTED",
                target_type="O2cInvoice",
                id_param="invoice_id",
                must_hold=_op("eq", _ref("obj.status"), _lit("posted")),
            ),
            InvariantRuleSpec(
                name="credit_amount_positive",
                code="NON_POSITIVE_CREDIT",
                target_type="O2cInvoice",
                id_param="invoice_id",
                must_hold=_op("gt", _ref("param.credit_amount"), _lit(0.0)),
            ),
        ]
        if not buggy:
            credit_pre.append(
                InvariantRuleSpec(
                    name="credit_cannot_exceed_residual",
                    code="CREDIT_EXCEEDS_RESIDUAL",
                    target_type="O2cInvoice",
                    id_param="invoice_id",
                    must_hold=_op("le", _ref("param.credit_amount"), _ref("obj.amount_residual")),
                )
            )

        gl_muts = [
            ActionMutationSpec(
                op="modify",
                object_type="GlAccount",
                id_param="debit_account_code",
                set_fields={
                    "debit_total_cents": _op("add", _ref("obj.debit_total_cents"), _ref("param.amount_cents")),
                },
            )
        ]
        if not buggy:
            gl_muts.append(
                ActionMutationSpec(
                    op="modify",
                    object_type="GlAccount",
                    id_param="credit_account_code",
                    set_fields={
                        "credit_total_cents": _op("add", _ref("obj.credit_total_cents"), _ref("param.amount_cents")),
                    },
                )
            )

        return CustomerOverlaySpec(
            overlay_id=ov_id,
            add_invariants=[
                InvariantRuleSpec(
                    name="r2r_global_double_entry_balance",
                    code="TRIAL_BALANCE_UNBALANCED",
                    target_type="GlAccount",
                    must_hold=_op(
                        "eq",
                        {"op": "sum", "object_type": "GlAccount", "field": "debit_total_cents"},
                        {"op": "sum", "object_type": "GlAccount", "field": "credit_total_cents"},
                    ),
                )
            ],
            add_actions=[
                ActionBlueprint(
                    name="apply_o2c_loyalty_credit_note",
                    allowed_roles=["finance", "finance_director", "admin"],
                    affects_source=True,
                    params=[
                        FieldSpec(name="invoice_id", field_type="str", required=True),
                        FieldSpec(name="credit_amount", field_type="float", required=True, non_negative=True),
                    ],
                    preconditions=credit_pre,
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="O2cInvoice",
                            id_param="invoice_id",
                            set_fields={
                                "amount_residual": _op(
                                    "sub", _ref("obj.amount_residual"), _ref("param.credit_amount")
                                )
                            },
                        )
                    ],
                ),
                ActionBlueprint(
                    name="post_manual_gl_adjustment_rule",
                    allowed_roles=["chief_accountant", "finance_director", "admin"],
                    affects_source=True,
                    params=[
                        FieldSpec(name="debit_account_code", field_type="str", required=True),
                        FieldSpec(name="credit_account_code", field_type="str", required=True),
                        FieldSpec(name="amount_cents", field_type="int", required=True, non_negative=True),
                    ],
                    preconditions=[
                        InvariantRuleSpec(
                            name="gl_adj_positive_cents",
                            code="NON_POSITIVE_AMOUNT",
                            target_type="GlAccount",
                            id_param="debit_account_code",
                            must_hold=_op("gt", _ref("param.amount_cents"), _lit(0)),
                        )
                    ],
                    mutations=gl_muts,
                ),
            ],
        )

    scenarios.append(
        {
            "id": "C4",
            "category": "INVARIANT_VIOLATION_RULE_ACTION",
            "target_action": "apply_o2c_loyalty_credit_note",
            "expect_defect": True,
            "mode": "invariant",
            "blueprint": apply_overlay(base, _make_rule_action_overlay(buggy=True, ov_id="c4_buggy_rules")),
        }
    )
    scenarios.append(
        {
            "id": "N4",
            "category": "CLEAN_CONTROL_C4",
            "target_action": "apply_o2c_loyalty_credit_note",
            "expect_defect": False,
            "mode": "invariant",
            "blueprint": apply_overlay(base, _make_rule_action_overlay(buggy=False, ov_id="n4_safe_rules")),
        }
    )

    # C5 / N5: Redundant/subsumed rule vs strictly tighter control
    c5_ov = CustomerOverlaySpec(
        overlay_id="c5_redundant_rule",
        add_action_preconditions={
            "confirm_p2p_order": [
                InvariantRuleSpec(
                    name="corp_po_over_100k_mgr",
                    code="PO_MGR_APPROVAL_REQUIRED",
                    target_type="P2pPurchaseOrder",
                    id_param="po_id",
                    when=_op("gt", _ref("obj.amount_total"), _lit(100000.0)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["procurement_manager", "finance_director"])),
                ),
                InvariantRuleSpec(
                    name="ai_subsumed_po_over_250k_mgr",
                    code="SUBSUMED_PO_APPROVAL",
                    target_type="P2pPurchaseOrder",
                    id_param="po_id",
                    when=_op("gt", _ref("obj.amount_total"), _lit(250000.0)),
                    must_hold=_op(
                        "in",
                        _ref("actor.role"),
                        _lit(["procurement_manager", "finance_director", "admin"]),
                    ),
                ),
            ]
        },
    )
    n5_ov = CustomerOverlaySpec(
        overlay_id="n5_strict_non_redundant",
        add_action_preconditions={
            "confirm_p2p_order": [
                InvariantRuleSpec(
                    name="corp_po_over_100k_mgr",
                    code="PO_MGR_APPROVAL_REQUIRED",
                    target_type="P2pPurchaseOrder",
                    id_param="po_id",
                    when=_op("gt", _ref("obj.amount_total"), _lit(100000.0)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["procurement_manager", "finance_director"])),
                ),
                InvariantRuleSpec(
                    name="director_required_over_250k",
                    code="PO_DIRECTOR_APPROVAL_REQUIRED",
                    target_type="P2pPurchaseOrder",
                    id_param="po_id",
                    when=_op("gt", _ref("obj.amount_total"), _lit(250000.0)),
                    must_hold=_op("eq", _ref("actor.role"), _lit("finance_director")),
                ),
            ]
        },
    )
    scenarios.append(
        {
            "id": "C5",
            "category": "REDUNDANT_RULE",
            "target_action": "confirm_p2p_order",
            "expect_defect": True,
            "mode": "redundancy",
            "blueprint": apply_overlay(base, c5_ov),
        }
    )
    scenarios.append(
        {
            "id": "N5",
            "category": "CLEAN_CONTROL_C5",
            "target_action": "confirm_p2p_order",
            "expect_defect": False,
            "mode": "redundancy",
            "blueprint": apply_overlay(base, n5_ov),
        }
    )

    # C6 / N6: Cross-table BMC quantifier contradiction vs clean quantifier control
    c6_ov = CustomerOverlaySpec(
        overlay_id="c6_cross_table_bmc_conflict",
        add_action_preconditions={
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="block_win_if_any_unclosed_ticket",
                    code="UNCLOSED_TICKET_BLOCKS_WIN",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when=_op("gt", _ref("obj.amount"), _lit(100000.0)),
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": _and(
                            _op("eq", _ref("target.account"), _ref("obj.account")),
                            _op("ne", _ref("target.status"), _lit("CLOSED")),
                        ),
                    },
                ),
                InvariantRuleSpec(
                    name="require_active_open_high_sla_ticket",
                    code="ACTIVE_SLA_TICKET_REQUIRED",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when=_op("gt", _ref("obj.amount"), _lit(100000.0)),
                    must_hold={
                        "op": "exists",
                        "object_type": "CrmTicket",
                        "where": _and(
                            _op("eq", _ref("target.account"), _ref("obj.account")),
                            _op("eq", _ref("target.priority"), _lit("HIGH")),
                            _op("eq", _ref("target.status"), _lit("OPEN")),
                        ),
                    },
                ),
            ]
        },
    )
    n6_ov = CustomerOverlaySpec(
        overlay_id="n6_cross_table_bmc_clean",
        add_action_preconditions={
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="block_win_if_any_unclosed_ticket",
                    code="UNCLOSED_TICKET_BLOCKS_WIN",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when=_op("gt", _ref("obj.amount"), _lit(100000.0)),
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": _and(
                            _op("eq", _ref("target.account"), _ref("obj.account")),
                            _op("ne", _ref("target.status"), _lit("CLOSED")),
                        ),
                    },
                ),
                InvariantRuleSpec(
                    name="require_closed_high_sla_audit_ticket",
                    code="CLOSED_SLA_AUDIT_REQUIRED",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when=_op("gt", _ref("obj.amount"), _lit(100000.0)),
                    must_hold={
                        "op": "exists",
                        "object_type": "CrmTicket",
                        "where": _and(
                            _op("eq", _ref("target.account"), _ref("obj.account")),
                            _op("eq", _ref("target.priority"), _lit("HIGH")),
                            _op("eq", _ref("target.status"), _lit("CLOSED")),
                        ),
                    },
                ),
            ]
        },
    )
    scenarios.append(
        {
            "id": "C6",
            "category": "CROSS_TABLE_BMC_CONFLICT",
            "target_action": "win_crm_deal",
            "expect_defect": True,
            "blueprint": apply_overlay(base, c6_ov),
            "expected_core": {
                "block_win_if_any_unclosed_ticket",
                "require_active_open_high_sla_ticket",
            },
        }
    )
    scenarios.append(
        {
            "id": "N6",
            "category": "CLEAN_CONTROL_C6",
            "target_action": "win_crm_deal",
            "expect_defect": False,
            "blueprint": apply_overlay(base, n6_ov),
        }
    )

    return scenarios


# ---------------------------------------------------------------------------
# 2. Multi-Department Scale Ladder (10 -> 25 -> 50 -> 100 rules)
# ---------------------------------------------------------------------------
def build_scale_ladder_blueprint(
    total_rules: int,
    *,
    inject_conflict: bool,
    variant_seed: int,
) -> tuple[DeclarativeBlueprint, str | None]:
    """Generate a deterministic N-rule blueprint spanning 5 departments."""
    base = _base_enterprise_bp()
    regions = ["CN_EAST", "CN_NORTH", "EU", "US", "APAC"]
    channels = ["DIRECT", "PARTNER", "ECOMMERCE", "TENDER"]
    tiers = ["STANDARD", "SILVER", "GOLD", "STRATEGIC"]
    risk_bands = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    zones = ["ZONE_A", "ZONE_B", "ZONE_C", "ZONE_D"]
    categories = ["GOODS", "SAAS", "SERVICES", "HARDWARE"]

    pre_map: dict[str, list[InvariantRuleSpec]] = {}

    def _add_pre(act_name: str, rule: InvariantRuleSpec) -> None:
        pre_map.setdefault(act_name, []).append(rule)

    reserve_conflict = 3 if inject_conflict else 0
    bg_count = max(1, total_rules - reserve_conflict)

    # 5 department rule templates distributed across core enterprise actions
    for idx in range(bg_count):
        dept = idx % 5
        r_tag = regions[idx % len(regions)]
        c_tag = channels[(idx // 2) % len(channels)]
        t_tag = tiers[(idx // 3) % len(tiers)]
        thresh = float(10000 + ((idx * 7500) % 180000))

        if dept == 0:
            # Sales department on confirm_o2c_order
            _add_pre(
                "confirm_o2c_order",
                InvariantRuleSpec(
                    name=f"dept_sales_rule_{idx}",
                    code=f"SALES_POLICY_{idx}",
                    target_type="O2cOrder",
                    id_param="order_id",
                    when=_and(
                        _op("eq", _ref("obj.region"), _lit(r_tag)),
                        _op("gt", _ref("obj.amount_total"), _lit(thresh)),
                    ),
                    must_hold=_op(
                        "in",
                        _ref("actor.role"),
                        _lit(["sales_manager", "finance_director", "admin"]),
                    ),
                ),
            )
        elif dept == 1:
            # Finance / Credit department on confirm_o2c_order or post_o2c_invoice
            if idx % 2 == 0:
                _add_pre(
                    "confirm_o2c_order",
                    InvariantRuleSpec(
                        name=f"dept_finance_rule_{idx}",
                        code=f"FINANCE_POLICY_{idx}",
                        target_type="O2cOrder",
                        id_param="order_id",
                        when=_and(
                            _op("eq", _ref("obj.sales_channel"), _lit(c_tag)),
                            _op("gt", _ref("obj.discount_rate"), _lit(0.05 + (idx % 3) * 0.03)),
                        ),
                        must_hold=_op(
                            "in",
                            _ref("actor.role"),
                            _lit(["sales_manager", "finance_director", "steward", "admin"]),
                        ),
                    ),
                )
            else:
                _add_pre(
                    "post_o2c_invoice",
                    InvariantRuleSpec(
                        name=f"dept_finance_inv_rule_{idx}",
                        code=f"FINANCE_INV_POLICY_{idx}",
                        target_type="O2cInvoice",
                        id_param="invoice_id",
                        when=_op("eq", _ref("obj.region"), _lit(r_tag)),
                        must_hold=_op("eq", _ref("obj.tax_verified"), _lit(True)),
                    ),
                )
        elif dept == 2:
            # Procurement department on confirm_p2p_order
            _add_pre(
                "confirm_p2p_order",
                InvariantRuleSpec(
                    name=f"dept_procurement_rule_{idx}",
                    code=f"PROC_POLICY_{idx}",
                    target_type="P2pPurchaseOrder",
                    id_param="po_id",
                    when=_and(
                        _op("eq", _ref("obj.region"), _lit(r_tag)),
                        _op("gt", _ref("obj.amount_total"), _lit(thresh)),
                    ),
                    must_hold=_op(
                        "in",
                        _ref("actor.role"),
                        _lit(["procurement_manager", "finance_director", "admin"]),
                    ),
                ),
            )
        elif dept == 3:
            # Production / Ops department on release_work_order
            z_tag = zones[idx % len(zones)]
            _add_pre(
                "release_work_order",
                InvariantRuleSpec(
                    name=f"dept_production_rule_{idx}",
                    code=f"PROD_POLICY_{idx}",
                    target_type="WorkOrder",
                    id_param="work_order_id",
                    when=_and(
                        _op("eq", _ref("obj.plant_zone"), _lit(z_tag)),
                        _op("gt", _ref("obj.planned_qty"), _lit(float(50 + idx * 5))),
                    ),
                    must_hold=_op(
                        "in",
                        _ref("actor.role"),
                        _lit(["plant_manager", "ops", "admin"]),
                    ),
                ),
            )
        else:
            # Risk / Compliance department across O2C & P2P
            rb = risk_bands[idx % len(risk_bands)]
            cat = categories[idx % len(categories)]
            if idx % 2 == 0:
                _add_pre(
                    "confirm_o2c_order",
                    InvariantRuleSpec(
                        name=f"dept_risk_rule_{idx}",
                        code=f"RISK_POLICY_{idx}",
                        target_type="O2cOrder",
                        id_param="order_id",
                        when=_and(
                            _op("eq", _ref("obj.vip_tier"), _lit(t_tag)),
                            _op("eq", _ref("obj.contract_category"), _lit(cat)),
                            _op("eq", _ref("obj.risk_band"), _lit(rb)),
                        ),
                        must_hold=_op("ge", _ref("obj.margin_rate"), _lit(0.05)),
                    ),
                )
            else:
                _add_pre(
                    "confirm_p2p_order",
                    InvariantRuleSpec(
                        name=f"dept_compliance_po_rule_{idx}",
                        code=f"COMPLIANCE_PO_{idx}",
                        target_type="P2pPurchaseOrder",
                        id_param="po_id",
                        when=_op("eq", _ref("obj.risk_band"), _lit(rb)),
                        must_hold=_op("in", _ref("actor.role"), _lit(["procurement_manager", "steward", "admin"])),
                    ),
                )

    conflict_action: str | None = None
    if inject_conflict:
        # Plant a realistic narrow 6-dimension / 3-department pairwise-compatible triangle deadlock
        # Each pair has a valid shared role, but the 3-way conjunction is empty (~0.015% density).
        conflict_action = "confirm_o2c_order"
        rng = random.Random(20261123 + total_rules * 31 + variant_seed)
        c_tier = "STRATEGIC"
        c_reg = rng.choice(regions)
        c_chan = rng.choice(channels)
        c_risk = "HIGH"
        c_amt = float(rng.choice([390000.0, 410000.0, 425000.0]))
        c_disc = float(rng.choice([0.24, 0.25, 0.26]))

        _add_pre(
            "confirm_o2c_order",
            InvariantRuleSpec(
                name=f"planted_sales_narrow_guard_v{variant_seed}",
                code="PLANTED_SALES_NARROW_LOCK",
                target_type="O2cOrder",
                id_param="order_id",
                when=_and(
                    _op("eq", _ref("obj.vip_tier"), _lit(c_tier)),
                    _op("eq", _ref("obj.region"), _lit(c_reg)),
                    _op("gt", _ref("obj.discount_rate"), _lit(c_disc)),
                ),
                must_hold=_op("in", _ref("actor.role"), _lit(["sales_manager", "ops", "admin"])),
            ),
        )
        _add_pre(
            "confirm_o2c_order",
            InvariantRuleSpec(
                name=f"planted_finance_narrow_guard_v{variant_seed}",
                code="PLANTED_FINANCE_NARROW_LOCK",
                target_type="O2cOrder",
                id_param="order_id",
                when=_and(
                    _op("eq", _ref("obj.sales_channel"), _lit(c_chan)),
                    _op("eq", _ref("obj.risk_band"), _lit(c_risk)),
                    _op("gt", _ref("obj.amount_total"), _lit(c_amt)),
                ),
                must_hold=_op("in", _ref("actor.role"), _lit(["finance_director", "sales_manager", "steward"])),
            ),
        )
        _add_pre(
            "confirm_o2c_order",
            InvariantRuleSpec(
                name=f"planted_tax_narrow_guard_v{variant_seed}",
                code="PLANTED_TAX_NARROW_LOCK",
                target_type="O2cOrder",
                id_param="order_id",
                when=_and(
                    _op("eq", _ref("obj.vip_tier"), _lit(c_tier)),
                    _op("eq", _ref("obj.risk_band"), _lit(c_risk)),
                    _op("gt", _ref("obj.amount_total"), _lit(c_amt)),
                    _op("gt", _ref("obj.discount_rate"), _lit(c_disc)),
                ),
                must_hold=_op("in", _ref("actor.role"), _lit(["finance_director", "tax_controller", "admin"])),
            ),
        )

    ov = CustomerOverlaySpec(
        overlay_id=f"scale_ladder_{total_rules}_{'conflict' if inject_conflict else 'clean'}_{variant_seed}",
        add_action_preconditions=pre_map,
    )
    return apply_overlay(base, ov), conflict_action


# ---------------------------------------------------------------------------
# 3. Baseline Competitor: Schema-Aware Random Property Fuzzing (1,000 samples)
# ---------------------------------------------------------------------------
class _FuzzView:
    """Minimal InvariantView / EditBatch shim supporting eval_predicate for fuzzing."""

    def __init__(self, spec: Any, rows_by_type: Mapping[str, list[Any]]) -> None:
        self.spec = spec
        self._rows_by_type = rows_by_type

    def query(self, cls: type[Any]) -> list[Any]:
        return list(self._rows_by_type.get(cls.__name__, ()))

    def get(self, cls: type[Any], pk: str) -> Any:
        for obj in self._rows_by_type.get(cls.__name__, ()):
            if obj.primary_key_value() == pk:
                return obj
        return None


def _random_field_val(
    fspec: FieldSpec,
    rng: random.Random,
    *,
    status_choices: Sequence[str] = (),
    prefer_compliant: bool = True,
) -> Any:
    if fspec.name == "status" and status_choices:
        return rng.choice(list(status_choices))
    if fspec.field_type == "bool":
        return True if (prefer_compliant and fspec.name == "tax_verified") else rng.choice([True, False])
    if fspec.field_type == "int":
        lo = 0 if fspec.non_negative else -50000
        return rng.randint(lo, 600000)
    if fspec.field_type == "float":
        if fspec.name == "margin_rate" and prefer_compliant:
            return round(rng.uniform(0.08, 0.45), 4)
        if "rate" in fspec.name:
            return round(rng.uniform(0.0, 0.35), 4)
        if fspec.name in ("amount_total", "amount", "planned_qty") and prefer_compliant:
            return round(rng.uniform(1500.0, 500000.0), 2)
        lo = 0.0 if fspec.non_negative else -1000.0
        return round(rng.uniform(lo, 500000.0), 2)

    enum_pools: dict[str, list[str]] = {
        "vip_tier": ["STANDARD", "SILVER", "GOLD", "STRATEGIC"],
        "sales_channel": ["DIRECT", "PARTNER", "ECOMMERCE", "TENDER"],
        "region": ["CN_EAST", "CN_NORTH", "EU", "US", "APAC"],
        "payment_terms": ["NET30", "NET60", "PREPAID"],
        "risk_band": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
        "contract_category": ["GOODS", "SAAS", "SERVICES", "HARDWARE"],
        "priority_tier": ["NORMAL", "URGENT", "EXPEDITED"],
        "plant_zone": ["ZONE_A", "ZONE_B", "ZONE_C", "ZONE_D"],
        "priority": ["LOW", "MEDIUM", "HIGH"],
        "payment_channel": ["WIRE", "ACH", "DROPSHIP_EXPRESS"],
    }
    if fspec.name == "tax_clearance_code":
        return "TAX-OK-01" if prefer_compliant else rng.choice(["", "TAX-OK-01"])
    if fspec.name in enum_pools:
        return rng.choice(enum_pools[fspec.name])
    if fspec.default and isinstance(fspec.default, str):
        return fspec.default if prefer_compliant else rng.choice([fspec.default, "OTHER"])
    return f"VAL-{rng.randint(1, 5)}"


def fuzz_blueprint(
    bp: DeclarativeBlueprint,
    *,
    target_actions: Sequence[str],
    samples_per_action: int = 1000,
    seed: int = 42,
    check_invariants: bool = False,
) -> dict[str, Any]:
    """Run strong valid-domain random property fuzzing across target actions.

    For each sampled object configuration (with valid business-evidence fields and
    valid FSM states), searches across all allowed roles and repairable evidence/linked
    states so legitimate single-rule validation guards do not inflate FPR.
    """
    t0 = time.perf_counter()
    rng = random.Random(seed)
    spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY, base_specs=bp._base_specs)
    ot_by_name = {ot.name: ot for ot in bp.object_types}
    act_by_name = {a.name: a for a in bp.actions}

    detected_deadlock_actions: set[str] = set()
    detected_invariant_actions: set[str] = set()
    slice_hits = 0

    class _ParamObj:
        def __init__(self, d: Mapping[str, Any]) -> None:
            for k, v in d.items():
                setattr(self, k, v)

    for act_name in target_actions:
        act = act_by_name.get(act_name)
        if act is None:
            continue

        # 1) Reachability fuzzing across N random valid-domain states (skip when testing invariant preservation)
        if act.preconditions and not check_invariants:
            subj_type = act.preconditions[0].target_type
            subj_ot = ot_by_name[subj_type]
            subj_cls = spec.get_object_type(subj_type)
            allowed_roles = list(act.allowed_roles) or ["admin"]
            states = (
                list(subj_ot.transitions.keys()) or list(subj_ot.initial_statuses or ("active",))
            )

            any_state_passed = False
            saw_unpassable_slice = False

            # Candidate repairable linked statuses & quantifier rows so clean models aren't falsely flagged
            linked_ots = [ot_by_name[rel.target_type] for rel in subj_ot.relations if rel.target_type in ot_by_name]
            quant_types: list[str] = []
            for pre in act.preconditions:
                for qexpr in (pre.when, pre.must_hold):
                    if isinstance(qexpr, dict) and qexpr.get("object_type") in ot_by_name:
                        qt = str(qexpr["object_type"])
                        if qt not in quant_types:
                            quant_types.append(qt)

            for s_idx in range(samples_per_action):
                data: dict[str, Any] = {}
                for fspec in subj_ot.fields:
                    data[fspec.name] = _random_field_val(
                        fspec, rng, status_choices=states, prefer_compliant=True
                    )
                data[subj_ot.primary_key] = f"FUZZ-{s_idx}"
                for rel in subj_ot.relations:
                    if rel.multiple:
                        data[rel.field] = ()
                    elif rel.required:
                        data[rel.field] = f"REL-{s_idx}"
                    else:
                        data[rel.field] = None
                obj = subj_cls(**data)

                p_vals = {
                    p.name: (
                        f"FUZZ-{s_idx}"
                        if p.name in (subj_ot.primary_key, act.preconditions[0].id_param)
                        else _random_field_val(p, rng, prefer_compliant=True)
                    )
                    for p in act.params
                }
                p_obj = _ParamObj(p_vals)

                # Determine small search set over linked FSM states / quantifier evidence patterns
                rel_variants: list[dict[str, list[Any]]] = [{}]
                for l_ot in linked_ots:
                    l_cls = spec.get_object_type(l_ot.name)
                    l_states = list(l_ot.transitions.keys()) or ["active"]
                    next_variants: list[dict[str, list[Any]]] = []
                    for st in l_states:
                        r_data = {
                            rf.name: _random_field_val(rf, rng, prefer_compliant=True)
                            for rf in l_ot.fields
                        }
                        r_data[l_ot.primary_key] = f"REL-{s_idx}"
                        r_data[l_ot.status_field] = st
                        for rrel in l_ot.relations:
                            r_data[rrel.field] = () if rrel.multiple else (f"SUB-{s_idx}" if rrel.required else None)
                        inst = l_cls(**r_data)
                        for base_v in rel_variants:
                            nv = dict(base_v)
                            nv[l_ot.name] = [inst]
                            next_variants.append(nv)
                    rel_variants = next_variants[:8]

                if quant_types:
                    qt = quant_types[0]
                    q_ot = ot_by_name[qt]
                    q_cls = spec.get_object_type(qt)
                    q_states = list(q_ot.transitions.keys()) or ["OPEN", "CLOSED"]
                    q_prios = ["HIGH", "MEDIUM"] if any(f.name == "priority" for f in q_ot.fields) else ["DEFAULT"]
                    q_variants: list[dict[str, list[Any]]] = []
                    for base_v in rel_variants:
                        # Try clean single row across target states/priorities
                        for qst in q_states[:4]:
                            for qpr in q_prios:
                                qd = {
                                    qf.name: _random_field_val(qf, rng, prefer_compliant=True)
                                    for qf in q_ot.fields
                                }
                                qd[q_ot.primary_key] = f"Q-{qt}-{s_idx}-{qst}-{qpr}"
                                qd[q_ot.status_field] = qst
                                if any(f.name == "priority" for f in q_ot.fields):
                                    qd["priority"] = qpr
                                for qr in q_ot.relations:
                                    qd[qr.field] = () if qr.multiple else data.get("account", f"REL-{s_idx}")
                                nv = dict(base_v)
                                nv[qt] = [q_cls(**qd)]
                                q_variants.append(nv)
                    rel_variants = q_variants

                state_ok = False
                for r_map in rel_variants:
                    fview = _FuzzView(spec, {subj_type: [obj], **r_map})
                    for role in allowed_roles:
                        ctx = {
                            "obj": obj,
                            "param": p_obj,
                            "actor": Actor(actor_id=f"fuzz:{role}", role=role, clearance_tags={"*"}),
                        }
                        ok_all = True
                        for pre in act.preconditions:
                            if pre.when is not None and not eval_predicate(pre.when, ctx, fview):
                                continue
                            if not eval_predicate(pre.must_hold, ctx, fview):
                                ok_all = False
                                break
                        if ok_all:
                            state_ok = True
                            break
                    if state_ok:
                        break

                if state_ok:
                    any_state_passed = True
                else:
                    saw_unpassable_slice = True
                    slice_hits += 1

            if (not any_state_passed) or saw_unpassable_slice:
                detected_deadlock_actions.add(act_name)

        # 2) Rule-action runtime execution fuzzing for C4 invariant checks
        if check_invariants and act.edit_function is None and act.mutations:
            rt = OntologyRuntime(spec, db_path=":memory:", world_mode="standalone")
            try:
                ot_names = {m.object_type for m in act.mutations}
                if "apply_o2c_loyalty_credit_note" == act.name:
                    # Seed valid posted invoice + parent order hierarchy and fuzz credit amounts
                    party_cls = spec.get_object_type("BusinessParty")
                    acct_cls = spec.get_object_type("AccountingEntity")
                    cust_cls = spec.get_object_type("O2cCustomerProfile")
                    ord_cls = spec.get_object_type("O2cOrder")
                    inv_cls = spec.get_object_type("O2cInvoice")
                    rt.put_object(party_cls(party_id="BP-1", legal_name="Acme"))
                    rt.put_object(acct_cls(entity_id="AE-1", name="HQ"))
                    rt.put_object(cust_cls(profile_id="C-1", party="BP-1", accounting_entity="AE-1"))
                    rt.put_object(
                        ord_cls(
                            order_id="SO-1",
                            order_number="SO-1",
                            customer="C-1",
                            accounting_entity="AE-1",
                            amount_total=500.0,
                            status="invoiced",
                        )
                    )
                    rt.put_object(
                        inv_cls(
                            invoice_id="INV-1",
                            order="SO-1",
                            amount_total=500.0,
                            amount_residual=100.0,
                            status="posted",
                        )
                    )
                    admin = Actor(actor_id="fuzz:admin", role="admin", clearance_tags={"*"})
                    for i in range(samples_per_action):
                        amt = round(rng.uniform(0.01, 200.0), 2)
                        res = rt.simulate_action(
                            act.name, {"invoice_id": "INV-1", "credit_amount": amt}, admin
                        )
                        if not res.valid and res.error_code in (
                            "NEGATIVE_VALUE",
                            "NEGATIVE_BALANCE",
                            "TRIAL_BALANCE_UNBALANCED",
                        ):
                            detected_invariant_actions.add(act.name)
                            break
                elif ot_names:
                    pass
            finally:
                rt.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return {
        "detected_defect": bool(detected_deadlock_actions or detected_invariant_actions),
        "detected_deadlock_actions": sorted(detected_deadlock_actions),
        "detected_invariant_actions": sorted(detected_invariant_actions),
        "blocked_state_hits": slice_hits,
        "elapsed_ms": elapsed_ms,
    }


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(ordered[int(k)], 3)
    return round(ordered[f] * (c - k) + ordered[c] * (k - f), 3)


def run_benchmark(
    *,
    fuzz_samples: int = 1000,
    output_path: Path = REPORT_OUTPUT_PATH,
) -> dict[str, Any]:
    """Execute full paired + scale-ladder benchmark and return structured report."""
    paired = build_paired_scenarios()
    paired_results: list[dict[str, Any]] = []

    z3_tp = z3_fp = z3_tn = z3_fn = 0
    fz_tp = fz_fp = fz_tn = fz_fn = 0
    fragment_histogram: dict[str, int] = {}

    for sc in paired:
        mode = sc.get("mode", "reachability")
        expect_defect = bool(sc["expect_defect"])
        target_act = str(sc["target_action"])

        if mode == "upgrade":
            up_rep = prove_upgrade_compatibility(sc["base_v1"], sc["base_v2"], sc["overlay"])
            z3_detected = not up_rep.compatible
            z3_ms = up_rep.elapsed_ms
            detail = (
                f"regressions={[r.regression_kind for r in up_rep.regressions]} "
                f"core={[r.unsat_core for r in up_rep.regressions]}"
            )
            full_rep = prove_blueprint(sc["blueprint"], upgrade_to=sc["base_v2"], overlay=sc["overlay"])
            for k, v in full_rep.logic_fragment_counts.items():
                fragment_histogram[k] = fragment_histogram.get(k, 0) + v
        else:
            rep = prove_blueprint(sc["blueprint"])
            for k, v in rep.logic_fragment_counts.items():
                fragment_histogram[k] = fragment_histogram.get(k, 0) + v
            z3_ms = rep.elapsed_ms
            if mode == "redundancy":
                z3_detected = rep.redundant_rule_count > 0
                detail = f"redundant={[(r.action_name, r.rule_name, r.entailed_by) for r in rep.redundant_rules]}"
            elif mode == "invariant":
                z3_detected = rep.invariant_violation_count > 0
                detail = f"violated={[p.violated_invariants for p in rep.invariant_preservation if p.status == 'VIOLATED']}"
            else:
                z3_detected = (rep.total_deadlock_count + rep.conditional_deadlock_count) > 0
                cores = [
                    (r.action_name, r.status, r.unsat_core)
                    for r in rep.reachability
                    if r.status != "REACHABLE"
                ]
                detail = f"deadlocks={cores}"

        if expect_defect and z3_detected:
            z3_tp += 1
        elif expect_defect and not z3_detected:
            z3_fn += 1
        elif (not expect_defect) and z3_detected:
            z3_fp += 1
        else:
            z3_tn += 1

        # Fuzzing comparison on reachability & invariant scenarios (fuzzing cannot detect redundancy or upgrade diffs alone)
        if mode in ("redundancy", "upgrade"):
            # For C3 upgrade fuzzing, run fuzzer on effective v2 blueprint; for C5 redundancy, fuzzing has N/A capability (False)
            if mode == "upgrade":
                fz_res = fuzz_blueprint(sc["blueprint"], target_actions=[target_act], samples_per_action=fuzz_samples, seed=101)
                fz_detected = fz_res["detected_defect"]
                fz_ms = fz_res["elapsed_ms"]
            else:
                fz_detected = False
                fz_ms = 0.0
        else:
            fz_res = fuzz_blueprint(
                sc["blueprint"],
                target_actions=[target_act],
                samples_per_action=fuzz_samples,
                seed=202,
                check_invariants=(mode == "invariant"),
            )
            fz_detected = fz_res["detected_defect"]
            fz_ms = fz_res["elapsed_ms"]

        if expect_defect and fz_detected:
            fz_tp += 1
        elif expect_defect and not fz_detected:
            fz_fn += 1
        elif (not expect_defect) and fz_detected:
            fz_fp += 1
        else:
            fz_tn += 1

        paired_results.append(
            {
                "id": sc["id"],
                "category": sc["category"],
                "expect_defect": expect_defect,
                "z3_detected": z3_detected,
                "z3_ms": z3_ms,
                "fuzz_detected": fz_detected,
                "fuzz_ms": fz_ms,
                "detail": detail,
            }
        )

    # 2. Scale ladder (10 -> 25 -> 50 -> 100 rules across 5 departments)
    ladder_tiers = [10, 25, 50, 100]
    ladder_summary: list[dict[str, Any]] = []
    all_narrow_z3_hits = 0
    all_narrow_fz_hits = 0
    all_narrow_total = 0
    reduction_ratios: list[float] = []

    for rule_count in ladder_tiers:
        tier_z3_ms: list[float] = []
        tier_fz_ms: list[float] = []
        tier_z3_hits = 0
        tier_fz_hits = 0
        tier_clean_fp_z3 = 0
        tier_clean_fp_fz = 0
        tier_cores: list[int] = []

        # 3 seeded conflict runs + 2 clean controls per tier
        for var_i in range(3):
            bp_c, c_act = build_scale_ladder_blueprint(rule_count, inject_conflict=True, variant_seed=var_i)
            rep_c = prove_blueprint(bp_c)
            tier_z3_ms.append(rep_c.elapsed_ms)
            for k, v in rep_c.logic_fragment_counts.items():
                fragment_histogram[k] = fragment_histogram.get(k, 0) + v
            found_c = (rep_c.total_deadlock_count + rep_c.conditional_deadlock_count) > 0
            all_narrow_total += 1
            if found_c:
                tier_z3_hits += 1
                all_narrow_z3_hits += 1
                z3_tp += 1
                for rr in rep_c.reachability:
                    if rr.unsat_core:
                        tier_cores.append(len(rr.unsat_core))
                        reduction_ratios.append(max(0.0, 1.0 - (len(rr.unsat_core) / float(rule_count))))
            else:
                z3_fn += 1

            fz_c = fuzz_blueprint(
                bp_c,
                target_actions=[c_act or "confirm_o2c_order"],
                samples_per_action=fuzz_samples,
                seed=500 + rule_count + var_i,
            )
            tier_fz_ms.append(fz_c["elapsed_ms"])
            if fz_c["detected_defect"]:
                tier_fz_hits += 1
                all_narrow_fz_hits += 1
                fz_tp += 1
            else:
                fz_fn += 1

        for var_j in range(2):
            bp_n, _ = build_scale_ladder_blueprint(rule_count, inject_conflict=False, variant_seed=var_j)
            rep_n = prove_blueprint(bp_n)
            tier_z3_ms.append(rep_n.elapsed_ms)
            for k, v in rep_n.logic_fragment_counts.items():
                fragment_histogram[k] = fragment_histogram.get(k, 0) + v
            if (rep_n.total_deadlock_count + rep_n.conditional_deadlock_count + rep_n.invariant_violation_count) > 0:
                tier_clean_fp_z3 += 1
                z3_fp += 1
            else:
                z3_tn += 1

            fz_n = fuzz_blueprint(
                bp_n,
                target_actions=["confirm_o2c_order", "confirm_p2p_order"],
                samples_per_action=fuzz_samples,
                seed=800 + rule_count + var_j,
            )
            tier_fz_ms.append(fz_n["elapsed_ms"])
            if fz_n["detected_defect"]:
                tier_clean_fp_fz += 1
                fz_fp += 1
            else:
                fz_tn += 1

        avg_core_size = round(statistics.mean(tier_cores), 2) if tier_cores else 0.0
        red_pct = round((1.0 - (avg_core_size / rule_count)) * 100.0, 1) if avg_core_size else 0.0
        ladder_summary.append(
            {
                "rule_count": rule_count,
                "conflict_runs": 3,
                "clean_runs": 2,
                "z3_recall_pct": round(tier_z3_hits / 3.0 * 100.0, 1),
                "z3_fpr_pct": round(tier_clean_fp_z3 / 2.0 * 100.0, 1),
                "fuzz_recall_pct": round(tier_fz_hits / 3.0 * 100.0, 1),
                "fuzz_fpr_pct": round(tier_clean_fp_fz / 2.0 * 100.0, 1),
                "avg_unsat_core_rules": avg_core_size,
                "root_cause_reduction_pct": red_pct,
                "z3_p50_ms": _percentile(tier_z3_ms, 50),
                "z3_p95_ms": _percentile(tier_z3_ms, 95),
                "z3_max_ms": _percentile(tier_z3_ms, 100),
                "fuzz_p50_ms": _percentile(tier_fz_ms, 50),
            }
        )

    def _metrics(tp: int, fp: int, tn: int, fn: int) -> dict[str, Any]:
        prec = (tp / (tp + fp) * 100.0) if (tp + fp) > 0 else 100.0
        rec = (tp / (tp + fn) * 100.0) if (tp + fn) > 0 else 100.0
        fpr = (fp / (fp + tn) * 100.0) if (fp + tn) > 0 else 0.0
        return {
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "precision_pct": round(prec, 2),
            "recall_pct": round(rec, 2),
            "fpr_pct": round(fpr, 2),
        }

    total_frags = sum(fragment_histogram.values()) or 1
    frag_pcts = {
        k: {"count": v, "pct": round(v / total_frags * 100.0, 2)}
        for k, v in sorted(fragment_histogram.items())
    }

    report = {
        "benchmark": "wo28_enterprise_rule_prover_benchmark",
        "fuzz_samples_per_action": fuzz_samples,
        "paired_scenarios": paired_results,
        "scale_ladder": ladder_summary,
        "overall_metrics": {
            "z3_smt": _metrics(z3_tp, z3_fp, z3_tn, z3_fn),
            "random_fuzzing": _metrics(fz_tp, fz_fp, fz_tn, fz_fn),
            "narrow_slice_3dept_recall": {
                "z3_hits": all_narrow_z3_hits + (1 if paired_results[2]["z3_detected"] else 0),
                "fuzz_hits": all_narrow_fz_hits + (1 if paired_results[2]["fuzz_detected"] else 0),
                "total_cases": all_narrow_total + 1,
                "z3_pct": round(
                    (all_narrow_z3_hits + (1 if paired_results[2]["z3_detected"] else 0))
                    / (all_narrow_total + 1)
                    * 100.0,
                    1,
                ),
                "fuzz_pct": round(
                    (all_narrow_fz_hits + (1 if paired_results[2]["fuzz_detected"] else 0))
                    / (all_narrow_total + 1)
                    * 100.0,
                    1,
                ),
            },
            "avg_root_cause_reduction_pct": (
                round(statistics.mean(reduction_ratios) * 100.0, 2) if reduction_ratios else 0.0
            ),
        },
        "logic_fragment_distribution": frag_pcts,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _print_markdown_report(report: Mapping[str, Any]) -> None:
    print("# WO-28 Enterprise Rule Conflict Benchmark Report\n")
    print("## 1. 12 Paired Scenarios (C1-C6 Defects vs N1-N6 Clean Controls)\n")
    print("| ID | Scenario | Defect? | Z3 (`design prove`) | Z3 (ms) | Fuzz (`1k/act`) | Fuzz (ms) |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for row in report["paired_scenarios"]:
        print(
            f"| {row['id']} | {row['category']} | {row['expect_defect']} | "
            f"{'DETECTED' if row['z3_detected'] else 'CLEAN'} | {row['z3_ms']} | "
            f"{'DETECTED' if row['fuzz_detected'] else 'CLEAN'} | {row['fuzz_ms']} |"
        )

    print("\n## 2. Multi-Dept Scale Ladder (10 -> 25 -> 50 -> 100 Rules)\n")
    print("| Rules | Z3 Recall | Z3 FPR | Fuzz Recall | Fuzz FPR | Avg `unsat_core` | Root-Cause Reduction | Z3 p50/p95/max (ms) |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for tier in report["scale_ladder"]:
        print(
            f"| {tier['rule_count']} | {tier['z3_recall_pct']}% | {tier['z3_fpr_pct']}% | "
            f"{tier['fuzz_recall_pct']}% | {tier['fuzz_fpr_pct']}% | "
            f"{tier['avg_unsat_core_rules']} | {tier['root_cause_reduction_pct']}% | "
            f"{tier['z3_p50_ms']} / {tier['z3_p95_ms']} / {tier['z3_max_ms']} |"
        )

    om = report["overall_metrics"]
    print("\n## 3. Overall Metrics Summary\n")
    print("| Engine | Precision | Recall | FPR | Narrow 3-Dept Recall | Avg Root-Cause Reduction |")
    print("|---|---:|---:|---:|---:|---:|")
    z3m = om["z3_smt"]
    fzm = om["random_fuzzing"]
    ns = om["narrow_slice_3dept_recall"]
    print(
        f"| `design prove` (Z3 SMT) | {z3m['precision_pct']}% | {z3m['recall_pct']}% | "
        f"{z3m['fpr_pct']}% | {ns['z3_pct']}% ({ns['z3_hits']}/{ns['total_cases']}) | "
        f"{om['avg_root_cause_reduction_pct']}% |"
    )
    print(
        f"| Random Fuzzing (1,000/act) | {fzm['precision_pct']}% | {fzm['recall_pct']}% | "
        f"{fzm['fpr_pct']}% | {ns['fuzz_pct']}% ({ns['fuzz_hits']}/{ns['total_cases']}) | N/A |"
    )

    print("\n## 4. Honest Logic Fragment Distribution\n")
    print("| Logic Fragment | Proof Checks | Share |")
    print("|---|---:|---:|")
    for frag, info in report["logic_fragment_distribution"].items():
        print(f"| `{frag}` | {info['count']} | {info['pct']}% |")


if __name__ == "__main__":
    rep = run_benchmark()
    _print_markdown_report(rep)
