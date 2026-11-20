"""Step 2.2: Heterogeneous BottleCRM (SQLite) x Odoo 19 Dual-Mount onto ONE Business Ontology.

This example proves M1 / Adapter / M0 decoupling across two completely heterogeneous
enterprise backends under a single ``OntologyRuntime``:

1. One pure business ``DeclarativeBlueprint`` grounded on ``BUSINESS_SEED_ONTOLOGY``
   (zero SQLite/Odoo table names, column names, or RPC method names inside M1):
   - ``CustomerAccount`` (``MasterEntity``)
   - ``SalesOpportunity`` (``ProcessDocument``)
   - ``SupportCase`` (``ProcessDocument``)
   - ``FulfillmentOrder`` (``ProcessDocument``)
2. Adapter-layer Entity Resolution (``HeterogeneousDualMountBridge``) mapping:
   - BottleCRM ``accounts`` ('Acme China Manufacturing', 'Northwind Trading', ...)
   - Odoo ``res.partner`` ('Acme Corporation', 'Northwind Manufacturing')
   onto canonical ``CustomerAccount`` IDs (``CUST-ACME``, ``CUST-NORTHWIND``, ...),
   while mounting both an ephemeral copy of ``vendor_crm/bottlecrm.sqlite3`` and
   an ephemeral clone of Odoo 19 ``erp_north`` (or ``OfflineOdooPhysicalWorld``)
   onto ONE ``OntologyRuntime(spec, world_mode="coupled", source_bridge=...)``.
3. Cross-system closed-loop verification across 4 scenarios:
   - Scenario 1 (Hard CRM block): Open high-priority BottleCRM case synchronously blocks
     Odoo order confirmation (``OPEN_CRITICAL_CRM_CASE_BLOCKS_ERP_ORDER``, zero Odoo write).
   - Scenario 2 (Cross-system unlock): Resolving the CRM case in temp BottleCRM SQLite
     unlocks within-credit Odoo confirmation (``COMMITTED``, Odoo ``sale.order.state='sale'``).
   - Scenario 3 (Cumulative exposure Approval Gate + M1 WorkCard settlement): Over-credit
     order confirmation by ``ai_agent`` is synchronously blocked with
     ``APPROVAL_REQUIRED_CUMULATIVE_CREDIT_EXCEEDED`` (Odoo remains ``draft``);
     agent opens M1 ``WorkCard`` and Steward ``settle_work_card`` settles physically into
     Odoo at ``t1`` (``COMMITTED``, ``state='sale'``, ``WorkCard.status='COMPLETED'``).
   - Scenario 4 (Drift detection): Out-of-band external mismatch triggers
     ``DEVICE_STATE_MISMATCH`` with zero memory corruption.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from open_ontology import (
    DEVICE_STATE_MISMATCH,
    Actor,
    DeviceStateMismatchError,
    InvariantError,
    OntologyRuntime,
    OntologySpec,
    SourceBridge,
)
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CrucibleContext,
    DeclarativeBlueprint,
    FieldSpec,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    CallableAbductionBackend,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
)

from .business_ontology import (
    WorkCard,
    build_business_seed_ontology,
    register_work_card_primitives,
    settle_work_card,
)
from .odoo_dual_erp_demo import DB_NORTH
from .odoo_fde_autonomous_pipeline import AI_OPS_ACTOR, STEWARD_OPS_ACTOR
from .odoo_fde_prober import EphemeralOdooSandbox
from .odoo_utility_benchmark import OfflineOdooPhysicalWorld, PhysicalOdooHarness

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_BOTTLECRM_DB = REPO_ROOT / "vendor_crm" / "bottlecrm.sqlite3"
ACME_CREDIT_LIMIT = 100_000.0


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. Pure Business M1 Blueprint (Zero Vendor / ORM Identifiers)
# ---------------------------------------------------------------------------
def build_heterogeneous_dual_mount_blueprint() -> DeclarativeBlueprint:
    """Build ONE pure business M1 ontology unifying Commercial Accounts, Cases, Opportunities, and Orders."""
    return DeclarativeBlueprint(
        model_id="heterogeneous_crm_erp_o2c",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description=(
            "Pure business Order-to-Cash & Customer-Service ontology unifying enterprise account governance "
            "across commercial pipeline, service case escalation, and order fulfillment."
        ),
        object_types=[
            ObjectTypeBlueprint(
                name="CustomerAccount",
                archetype="MasterEntity",
                primary_key="account_id",
                title_field="name",
                fields=[
                    FieldSpec(name="account_id", field_type="str", required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(name="industry", field_type="str", default=""),
                    FieldSpec(name="credit_limit", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="active"),
                ],
                transitions={"active": ["suspended"], "suspended": ["active"]},
            ),
            ObjectTypeBlueprint(
                name="SalesOpportunity",
                archetype="ProcessDocument",
                primary_key="opportunity_id",
                title_field="name",
                status_field="stage",
                fields=[
                    FieldSpec(name="opportunity_id", field_type="str", required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(name="amount", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="stage", field_type="str", default="PROSPECTING"),
                ],
                relations=[
                    RelationSpec(field="customer", target_type="CustomerAccount", required=True),
                ],
                transitions={
                    "PROSPECTING": ["QUALIFICATION", "CLOSED_LOST"],
                    "QUALIFICATION": ["PROPOSAL", "NEGOTIATION", "CLOSED_LOST"],
                    "PROPOSAL": ["NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"],
                    "NEGOTIATION": ["CLOSED_WON", "CLOSED_LOST"],
                    "CLOSED_WON": [],
                    "CLOSED_LOST": [],
                },
            ),
            ObjectTypeBlueprint(
                name="SupportCase",
                archetype="ProcessDocument",
                primary_key="case_id",
                title_field="name",
                status_field="status",
                fields=[
                    FieldSpec(name="case_id", field_type="str", required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(name="priority", field_type="str", default="Medium"),
                    FieldSpec(name="status", field_type="str", default="New"),
                ],
                relations=[
                    RelationSpec(field="customer", target_type="CustomerAccount", required=True),
                ],
                transitions={
                    "New": ["In Progress", "Assigned", "Closed"],
                    "In Progress": ["Assigned", "Closed"],
                    "Assigned": ["Closed"],
                    "Closed": [],
                },
            ),
            ObjectTypeBlueprint(
                name="FulfillmentOrder",
                archetype="ProcessDocument",
                primary_key="order_id",
                title_field="order_number",
                status_field="status",
                fields=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="order_number", field_type="str", required=True),
                    FieldSpec(name="amount_total", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(field="customer", target_type="CustomerAccount", required=True),
                    RelationSpec(field="opportunity", target_type="SalesOpportunity", required=False),
                ],
                transitions={
                    "draft": ["confirmed", "cancelled"],
                    "confirmed": ["cancelled"],
                    "cancelled": [],
                },
            ),
        ],
        actions=[
            ActionBlueprint(
                name="resolve_support_case",
                description="Close a customer support escalation once verified resolved.",
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                affects_source=True,
                params=[FieldSpec(name="case_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="case_must_be_open",
                        code="CASE_ALREADY_CLOSED",
                        target_type="SupportCase",
                        id_param="case_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["New", "In Progress", "Assigned"]},
                        },
                        description="Only non-closed support cases can be transitioned to Closed.",
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SupportCase",
                        id_param="case_id",
                        set_fields={"status": {"lit": "Closed"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="confirm_fulfillment_order",
                description="Confirm a draft fulfillment order after verifying service-case clearance and customer credit exposure.",
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                affects_source=True,
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="order_must_be_draft",
                        code="INVALID_ORDER_STATUS_FOR_CONFIRMATION",
                        target_type="FulfillmentOrder",
                        id_param="order_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                        description="Only draft fulfillment orders may be confirmed.",
                    ),
                    InvariantRuleSpec(
                        name="block_when_critical_support_case_open",
                        code="OPEN_CRITICAL_CRM_CASE_BLOCKS_ERP_ORDER",
                        target_type="FulfillmentOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "not",
                            "arg": {
                                "op": "exists",
                                "object_type": "SupportCase",
                                "where": {
                                    "op": "and",
                                    "args": [
                                        {"op": "eq", "left": {"ref": "target.customer"}, "right": {"ref": "obj.customer"}},
                                        {
                                            "op": "in",
                                            "left": {"ref": "target.priority"},
                                            "right": {"lit": ["High", "Urgent", "Critical"]},
                                        },
                                        {"op": "ne", "left": {"ref": "target.status"}, "right": {"lit": "Closed"}},
                                    ],
                                },
                            },
                        },
                        description="Fulfillment confirmation is synchronously blocked while an unclosed high-severity customer case exists.",
                    ),
                    InvariantRuleSpec(
                        name="require_steward_approval_over_cumulative_credit_limit",
                        code="APPROVAL_REQUIRED_CUMULATIVE_CREDIT_EXCEEDED",
                        target_type="FulfillmentOrder",
                        id_param="order_id",
                        when={
                            "op": "and",
                            "args": [
                                {"op": "gt", "left": {"ref": "obj.customer.credit_limit"}, "right": {"lit": 0}},
                                {
                                    "op": "gt",
                                    "left": {
                                        "op": "sum",
                                        "object_type": "FulfillmentOrder",
                                        "field": {
                                            "op": "if_else",
                                            "cond": {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "confirmed"}},
                                            "then": {"ref": "target.amount_total"},
                                            "else": {"lit": 0.0},
                                        },
                                        "where": {"op": "eq", "left": {"ref": "target.customer"}, "right": {"ref": "obj.customer"}},
                                    },
                                    "right": {
                                        "op": "sub",
                                        "left": {"ref": "obj.customer.credit_limit"},
                                        "right": {"ref": "obj.amount_total"},
                                    },
                                },
                            ],
                        },
                        must_hold={"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["steward", "admin"]}},
                        description="Over-credit cumulative customer exposure requires Steward/Admin approval via WorkCard before confirmation.",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="FulfillmentOrder",
                        id_param="order_id",
                        set_fields={"status": {"lit": "confirmed"}},
                    )
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 2. Adapter-Layer Entity Resolution + Dual-Mount SourceBridge
# ---------------------------------------------------------------------------
_ODOO_TO_BIZ_STATUS = {
    "draft": "draft",
    "sent": "draft",
    "sale": "confirmed",
    "done": "confirmed",
    "cancel": "cancelled",
}

_CRM_ACCOUNT_TO_CANONICAL = {
    "39e36940179f47c7be22805d72db1a99": "CUST-ACME",
    "9865ac3c44d1493da0a88913d2458f52": "CUST-NORTHWIND",
    "3fe3cc2bed70479ea9ed418d2667e06d": "CUST-SOUTHSTAR",
}


class HeterogeneousDualMountBridge:
    """Mounts temp BottleCRM SQLite + Odoo sandbox/offline world under ONE OntologyRuntime."""

    def __init__(
        self,
        *,
        crm_sqlite_path: Path,
        odoo_client: Any,
        partner_id_to_canonical: dict[int, str],
        case_id_map: dict[str, str],
        opp_by_account: dict[str, str],
        order_opp_map: dict[int, str] | None = None,
    ) -> None:
        self.crm_sqlite_path = crm_sqlite_path
        self.odoo_client = odoo_client
        self.partner_id_to_canonical = dict(partner_id_to_canonical)
        self.case_id_map = dict(case_id_map)  # canonical_case_id -> sqlite row id
        self.opp_by_account = dict(opp_by_account)  # canonical_account_id -> canonical_opp_id
        self.order_opp_map = dict(order_opp_map or {})
        self.sqlite_writes = 0
        self.odoo_writes = 0
        self._skip_odoo_call = False

    def _read_case_row_sqlite(self, raw_case_id: str) -> dict[str, Any]:
        conn = sqlite3.connect(self.crm_sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT id, name, status, priority, account_id FROM "case" WHERE id = ?',
                (raw_case_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"BottleCRM case {raw_case_id!r} not found")
            return dict(row)
        finally:
            conn.close()

    def _read_odoo_order_row(self, ext_order_id: int) -> dict[str, Any]:
        rows = self.odoo_client.search_read(
            "sale.order",
            [("id", "=", int(ext_order_id))],
            ["id", "name", "state", "partner_id", "amount_total"],
            limit=1,
        )
        if not rows:
            raise RuntimeError(f"Odoo sale.order {ext_order_id} not found")
        return dict(rows[0])

    def project_case(self, row: dict[str, Any], canonical_id: str) -> dict[str, Any]:
        cust_id = _CRM_ACCOUNT_TO_CANONICAL.get(str(row["account_id"] or ""), f"CUST-{row['account_id']}")
        return {
            "case_id": canonical_id,
            "name": str(row["name"]),
            "customer": cust_id,
            "priority": str(row["priority"] or "Medium"),
            "status": str(row["status"] or "New"),
        }

    def project_order(self, row: dict[str, Any]) -> dict[str, Any]:
        oid = int(row["id"])
        raw_partner = row.get("partner_id")
        pid = int(raw_partner[0] if isinstance(raw_partner, (list, tuple)) else raw_partner)
        cust_id = self.partner_id_to_canonical[pid]
        return {
            "order_id": f"ORD-{oid}",
            "order_number": str(row["name"]),
            "customer": cust_id,
            "opportunity": self.order_opp_map.get(oid) or self.opp_by_account.get(cust_id),
            "amount_total": float(row.get("amount_total") or 0.0),
            "status": _ODOO_TO_BIZ_STATUS.get(str(row.get("state") or "draft"), "draft"),
        }

    def read_odoo_raw_state(self, canonical_order_id: str) -> str:
        ext_id = int(canonical_order_id.removeprefix("ORD-"))
        return str(self._read_odoo_order_row(ext_id)["state"])

    def read_sqlite_raw_case_status(self, canonical_case_id: str) -> str:
        raw_id = self.case_id_map[canonical_case_id]
        return str(self._read_case_row_sqlite(raw_id)["status"])

    def make_source_bridge(self) -> SourceBridge:
        def _actuate(move: dict[str, Any]) -> Any:
            action_name = str(move["action_type"])
            params = dict(move.get("params") or {})
            if action_name == "resolve_support_case":
                canonical_cid = str(params["case_id"])
                raw_cid = self.case_id_map[canonical_cid]
                conn = sqlite3.connect(self.crm_sqlite_path)
                try:
                    cur = conn.execute(
                        'UPDATE "case" SET status = ?, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                        ("Closed", raw_cid),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Expected 1 SQLite case row updated for {raw_cid!r}, got {cur.rowcount}")
                    conn.commit()
                    self.sqlite_writes += 1
                finally:
                    conn.close()
                return True
            if action_name == "confirm_fulfillment_order":
                if self._skip_odoo_call:
                    return None
                ext_id = int(str(params["order_id"]).removeprefix("ORD-"))
                res = self.odoo_client.call_method("sale.order", "action_confirm", [ext_id])
                self.odoo_writes += 1
                return res
            raise ValueError(f"unsupported dual-mount action {action_name!r}")

        def _observe(move: dict[str, Any]) -> dict[str, Any]:
            action_name = str(move["action_type"])
            params = dict(move.get("params") or {})
            if action_name == "resolve_support_case":
                canonical_cid = str(params["case_id"])
                raw_cid = self.case_id_map[canonical_cid]
                row = self._read_case_row_sqlite(raw_cid)
                return {"SupportCase": [self.project_case(row, canonical_cid)]}
            if action_name == "confirm_fulfillment_order":
                ext_id = int(str(params["order_id"]).removeprefix("ORD-"))
                row = self._read_odoo_order_row(ext_id)
                return {"FulfillmentOrder": [self.project_order(row)]}
            raise ValueError(f"unsupported dual-mount observe action {action_name!r}")

        return SourceBridge(actuate=_actuate, observe=_observe)


# ---------------------------------------------------------------------------
# 3. End-to-End Heterogeneous Dual-Mount Execution
# ---------------------------------------------------------------------------
def _prepare_temp_bottlecrm_sqlite(tmp_dir: Path) -> Path:
    dst = tmp_dir / "bottlecrm_ephemeral.sqlite3"
    shutil.copy2(SOURCE_BOTTLECRM_DB, dst)
    # Verify Acme has an active high/urgent open case in temp SQLite; if not, ensure it inside the copy
    conn = sqlite3.connect(dst)
    conn.row_factory = sqlite3.Row
    try:
        acme_row = conn.execute(
            'SELECT id, status, priority FROM "case" WHERE account_id = ?',
            ("39e36940179f47c7be22805d72db1a99",),
        ).fetchone()
        if acme_row is None:
            raise RuntimeError("Expected seeded Acme case in bottlecrm.sqlite3")
        if acme_row["status"] == "Closed":
            conn.execute(
                'UPDATE "case" SET status = ?, priority = ? WHERE id = ?',
                ("New", "High", acme_row["id"]),
            )
            conn.commit()
    finally:
        conn.close()
    return dst


def _resolve_dual_partners_and_orders(
    *,
    harness: PhysicalOdooHarness,
    sandbox_client: Any,
    offline: bool,
) -> tuple[dict[int, str], dict[int, float], dict[int, str]]:
    """Resolve Acme / Northwind partner IDs and set uniform credit limits in ephemeral Odoo."""
    partner_id_to_canonical: dict[int, str] = {}
    credit_limit_by_partner: dict[int, float] = {}
    names_by_canonical: dict[str, tuple[str, str]] = {
        "CUST-ACME": ("Acme Corporation", "ACME-DUAL"),
        "CUST-NORTHWIND": ("Northwind Manufacturing", "NORTHWIND-DUAL"),
    }
    partner_names: dict[int, str] = {}
    for canonical, (pname, pref) in names_by_canonical.items():
        if offline:
            pid = harness.create_partner(pref, pname, credit_limit=ACME_CREDIT_LIMIT)
        else:
            matches = sandbox_client.search_read(
                "res.partner",
                [("name", "=", pname)],
                ["id", "name", "credit_limit"],
                limit=5,
            )
            if matches:
                pid = int(matches[0]["id"])
                sandbox_client._client.write("res.partner", [pid], {"credit_limit": ACME_CREDIT_LIMIT})
            else:
                pid = harness.create_partner(pref, pname, credit_limit=ACME_CREDIT_LIMIT)
        partner_id_to_canonical[pid] = canonical
        credit_limit_by_partner[pid] = ACME_CREDIT_LIMIT
        partner_names[pid] = pname
    return partner_id_to_canonical, credit_limit_by_partner, partner_names


def _crystallize_dual_mount_blueprint(spec_bp: DeclarativeBlueprint) -> OntologySpec:
    seed = build_business_seed_ontology()
    with InductionGovernanceService(db_path=":memory:") as gov:
        backend = CallableAbductionBackend(
            lambda _s, _u, _r: {
                "rationale": "Heterogeneous BottleCRM x Odoo 19 dual-mount business ontology",
                "blueprint": spec_bp.model_dump(mode="json"),
                "proposed_eval_cases": [],
            }
        )
        builder = OntologyBuilderAgent(
            backend=backend,
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=seed,
        )
        m0_synthetic = [
            {
                "object_type": "CustomerAccount",
                "data": {"account_id": "CUST-SYN-1", "name": "Synthetic Co", "credit_limit": 1000.0, "status": "active"},
            },
            {
                "object_type": "SalesOpportunity",
                "data": {"opportunity_id": "OPP-SYN-1", "name": "Syn Deal", "customer": "CUST-SYN-1", "amount": 500.0, "stage": "PROPOSAL"},
            },
            {
                "object_type": "SupportCase",
                "data": {"case_id": "CASE-SYN-OPEN", "name": "Blocked Ticket", "customer": "CUST-SYN-1", "priority": "High", "status": "New"},
            },
            {
                "object_type": "CustomerAccount",
                "data": {"account_id": "CUST-SYN-2", "name": "Clear Co", "credit_limit": 1000.0, "status": "active"},
            },
            {
                "object_type": "SupportCase",
                "data": {"case_id": "CASE-SYN-CLOSED", "name": "Resolved Ticket", "customer": "CUST-SYN-2", "priority": "High", "status": "Closed"},
            },
            {
                "object_type": "FulfillmentOrder",
                "data": {"order_id": "ORD-SYN-BLOCKED", "order_number": "S-BLK", "customer": "CUST-SYN-1", "amount_total": 400.0, "status": "draft"},
            },
            {
                "object_type": "FulfillmentOrder",
                "data": {"order_id": "ORD-SYN-OK", "order_number": "S-OK", "customer": "CUST-SYN-2", "amount_total": 400.0, "status": "draft"},
            },
            {
                "object_type": "FulfillmentOrder",
                "data": {"order_id": "ORD-SYN-OVER", "order_number": "S-OVR", "customer": "CUST-SYN-2", "amount_total": 1500.0, "status": "draft"},
            },
        ]
        ctx = CrucibleContext(
            mode="genesis",
            m0_objects=m0_synthetic,
            positive_events=[
                ReplayEvent(
                    name="POS-RESOLVE-CASE",
                    action="resolve_support_case",
                    params={"case_id": "CASE-SYN-OPEN"},
                    actor=AI_OPS_ACTOR,
                    expected_valid=True,
                ),
                ReplayEvent(
                    name="POS-CONFIRM-CLEAR-WITHIN",
                    action="confirm_fulfillment_order",
                    params={"order_id": "ORD-SYN-OK"},
                    actor=AI_OPS_ACTOR,
                    expected_valid=True,
                    expected_requires_approval=False,
                ),
                ReplayEvent(
                    name="POS-CONFIRM-OVER-STEWARD",
                    action="confirm_fulfillment_order",
                    params={"order_id": "ORD-SYN-OVER"},
                    actor=STEWARD_OPS_ACTOR,
                    expected_valid=True,
                ),
            ],
            negative_events=[
                ReplayEvent(
                    name="NEG-CONFIRM-OVER-REQUIRES-APPROVAL",
                    action="confirm_fulfillment_order",
                    params={"order_id": "ORD-SYN-OVER"},
                    actor=AI_OPS_ACTOR,
                    expected_valid=False,
                    expected_error_code="APPROVAL_REQUIRED_CUMULATIVE_CREDIT_EXCEEDED",
                ),
                ReplayEvent(
                    name="NEG-OPEN-CRITICAL-CASE-BLOCKS",
                    action="confirm_fulfillment_order",
                    params={"order_id": "ORD-SYN-BLOCKED"},
                    actor=AI_OPS_ACTOR,
                    expected_valid=False,
                    expected_error_code="OPEN_CRITICAL_CRM_CASE_BLOCKS_ERP_ORDER",
                ),
                ReplayEvent(
                    name="NEG-DOUBLE-CLOSE-CASE",
                    action="resolve_support_case",
                    params={"case_id": "CASE-SYN-CLOSED"},
                    actor=AI_OPS_ACTOR,
                    expected_valid=False,
                    expected_error_code="CASE_ALREADY_CLOSED",
                ),
            ],
            default_actor=AI_OPS_ACTOR,
            seed_ontology=seed,
        )
        cycle = builder.run_genesis_cycle(
            model_id="heterogeneous_crm_erp_o2c",
            phenomenon_brief={"scope": "dual_mount_commercial_crm_erp"},
            crucible_context=ctx,
            seed_ontology=seed,
            max_rounds=2,
        )
        assert cycle.verified and cycle.pending_approval_run_id is not None, (
            f"Dual-mount blueprint failed 5-gate Crucible:\n{cycle.final_report.diagnostic_feedback()}"
        )
        gov.approve_crystallization(cycle.pending_approval_run_id, STEWARD_ACTOR)
        active_bp = gov.get_active_blueprint("heterogeneous_crm_erp_o2c")
        assert active_bp is not None
        compiled_spec = compile_blueprint(active_bp, seed_ontology=seed)
        return register_work_card_primitives(compiled_spec)


def run_heterogeneous_dual_mount_demo(
    *,
    offline: bool = True,
    source_db: str = DB_NORTH,
) -> dict[str, Any]:
    """Mount ephemeral BottleCRM SQLite + ephemeral Odoo world onto ONE OntologyRuntime."""
    bp = build_heterogeneous_dual_mount_blueprint()
    compiled_spec = _crystallize_dual_mount_blueprint(bp)

    with tempfile.TemporaryDirectory(prefix="dual_mount_") as tmp_dir_str:
        crm_copy = _prepare_temp_bottlecrm_sqlite(Path(tmp_dir_str))
        if offline:
            world = OfflineOdooPhysicalWorld()
            harness = PhysicalOdooHarness(offline_world=world)
            return _execute_dual_mount_flow(
                compiled_spec=compiled_spec,
                crm_sqlite_path=crm_copy,
                harness=harness,
                odoo_client=world,
                sandbox_client=None,
                offline=True,
                sandbox_label="offline://dual_mount_mirror",
            )

        with EphemeralOdooSandbox(source_db=source_db) as sandbox_client:
            sandbox_client._ensure_login()
            harness = PhysicalOdooHarness(live_client=sandbox_client._client)
            return _execute_dual_mount_flow(
                compiled_spec=compiled_spec,
                crm_sqlite_path=crm_copy,
                harness=harness,
                odoo_client=sandbox_client,
                sandbox_client=sandbox_client,
                offline=False,
                sandbox_label=sandbox_client.db,
            )


def _execute_dual_mount_flow(
    *,
    compiled_spec: OntologySpec,
    crm_sqlite_path: Path,
    harness: PhysicalOdooHarness,
    odoo_client: Any,
    sandbox_client: Any | None,
    offline: bool,
    sandbox_label: str,
) -> dict[str, Any]:
    partner_id_to_canonical, credit_limit_by_partner, _ = _resolve_dual_partners_and_orders(
        harness=harness,
        sandbox_client=sandbox_client,
        offline=offline,
    )
    acme_pid = next(pid for pid, can in partner_id_to_canonical.items() if can == "CUST-ACME")

    # Seed 3 draft FulfillmentOrders in ephemeral Odoo clone/mirror for Acme:
    # 1) within credit limit (20,000 <= 100,000) -> blocked by CRM case, then unlocked!
    # 2) over cumulative credit limit (+90,000 -> 110,200 > 100,000) -> APPROVAL_REQUIRED -> WorkCard -> Steward settles!
    # 3) extra draft within limit for Scenario 4 external drift detection
    f_within = harness.create_draft_order(
        partner_id=acme_pid, qty=2, unit_price=10_000.0, client_ref="DUAL-ACME-WITHIN"
    )
    f_over = harness.create_draft_order(
        partner_id=acme_pid, qty=9, unit_price=10_000.0, client_ref="DUAL-ACME-OVER"
    )
    f_drift = harness.create_draft_order(
        partner_id=acme_pid, qty=1, unit_price=10_000.0, client_ref="DUAL-ACME-DRIFT"
    )
    if offline:
        # Enforce uniform 10,000 * qty math in offline mirror just like live Odoo untaxed line
        odoo_client.tables["sale.order"][f_within.order_id]["amount_total"] = 20_000.0
        odoo_client.tables["sale.order"][f_over.order_id]["amount_total"] = 90_000.0
        odoo_client.tables["sale.order"][f_drift.order_id]["amount_total"] = 10_000.0

    # Read BottleCRM accounts, opportunity, and case from temp SQLite copy
    conn = sqlite3.connect(crm_sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        crm_accounts = [dict(r) for r in conn.execute('SELECT id, name, industry FROM "accounts"').fetchall()]
        crm_opps = [dict(r) for r in conn.execute('SELECT id, name, stage, amount, account_id FROM "opportunity"').fetchall()]
        crm_cases = [dict(r) for r in conn.execute('SELECT id, name, status, priority, account_id FROM "case"').fetchall()]
    finally:
        conn.close()

    case_id_map: dict[str, str] = {}
    acme_case_canonical = ""
    for idx, c_row in enumerate(crm_cases, start=1):
        can_cid = (
            "CASE-ACME-001"
            if c_row["account_id"] == "39e36940179f47c7be22805d72db1a99"
            else f"CASE-{idx:03d}"
        )
        if can_cid == "CASE-ACME-001":
            acme_case_canonical = can_cid
        case_id_map[can_cid] = str(c_row["id"])
    assert acme_case_canonical, "expected BottleCRM open case linked to Acme"

    opp_by_account: dict[str, str] = {}
    for idx, o_row in enumerate(crm_opps, start=1):
        can_acc = _CRM_ACCOUNT_TO_CANONICAL.get(str(o_row["account_id"] or ""), "")
        if can_acc:
            opp_by_account[can_acc] = f"OPP-{idx:03d}"

    order_opp_map = {
        f_within.order_id: opp_by_account.get("CUST-ACME", ""),
        f_over.order_id: opp_by_account.get("CUST-ACME", ""),
    }

    bridge = HeterogeneousDualMountBridge(
        crm_sqlite_path=crm_sqlite_path,
        odoo_client=odoo_client,
        partner_id_to_canonical=partner_id_to_canonical,
        case_id_map=case_id_map,
        opp_by_account=opp_by_account,
        order_opp_map={k: v for k, v in order_opp_map.items() if v},
    )

    rt = OntologyRuntime(
        compiled_spec,
        db_path=":memory:",
        world_mode="coupled",
        source_bridge=bridge.make_source_bridge(),
    )
    try:
        cust_cls = compiled_spec.get_object_type("CustomerAccount")
        opp_cls = compiled_spec.get_object_type("SalesOpportunity")
        case_cls = compiled_spec.get_object_type("SupportCase")
        order_cls = compiled_spec.get_object_type("FulfillmentOrder")

        # Load unified CustomerAccount (resolved from BottleCRM + Odoo res.partner)
        resolved_credit_by_canonical = {
            partner_id_to_canonical[pid]: lim for pid, lim in credit_limit_by_partner.items()
        }
        seen_accounts: set[str] = set()
        for acc_row in crm_accounts:
            can_aid = _CRM_ACCOUNT_TO_CANONICAL.get(str(acc_row["id"]), f"CUST-{acc_row['id']}")
            seen_accounts.add(can_aid)
            rt.put_object(
                cust_cls(
                    account_id=can_aid,
                    name=str(acc_row["name"]),
                    industry=str(acc_row.get("industry") or ""),
                    credit_limit=float(resolved_credit_by_canonical.get(can_aid, 0.0)),
                    status="active",
                )
            )

        # Load BottleCRM SalesOpportunity rows
        for idx, o_row in enumerate(crm_opps, start=1):
            can_aid = _CRM_ACCOUNT_TO_CANONICAL.get(str(o_row["account_id"] or ""), f"CUST-{o_row['account_id']}")
            if can_aid not in seen_accounts:
                continue
            rt.put_object(
                opp_cls(
                    opportunity_id=f"OPP-{idx:03d}",
                    name=str(o_row["name"]),
                    customer=can_aid,
                    amount=float(o_row.get("amount") or 0.0),
                    stage=str(o_row.get("stage") or "PROSPECTING"),
                )
            )

        # Load BottleCRM SupportCase rows
        reverse_case = {raw_id: can_cid for can_cid, raw_id in case_id_map.items()}
        for c_row in crm_cases:
            can_cid = reverse_case[str(c_row["id"])]
            rt.put_object(case_cls(**bridge.project_case(c_row, can_cid)))

        # Load Odoo sale.order rows belonging to resolved customer accounts (includes historical confirmed + 3 draft orders)
        resolved_pids = set(partner_id_to_canonical.keys())
        odoo_orders = odoo_client.search_read(
            "sale.order",
            [],
            ["id", "name", "state", "partner_id", "amount_total"],
            limit=200,
        )
        for row in odoo_orders:
            raw_partner = row.get("partner_id")
            pid = raw_partner[0] if isinstance(raw_partner, (list, tuple)) and raw_partner else raw_partner
            if isinstance(pid, int) and pid in resolved_pids:
                rt.put_object(order_cls(**bridge.project_order(row)))

        within_oid = f"ORD-{f_within.order_id}"
        over_oid = f"ORD-{f_over.order_id}"
        drift_oid = f"ORD-{f_drift.order_id}"

        # Scenario 1: Open BottleCRM High-Priority SupportCase synchronously blocks Odoo confirm_fulfillment_order
        try:
            r1 = rt.submit_action("DUAL-S1-BLOCK", "confirm_fulfillment_order", {"order_id": within_oid}, AI_OPS_ACTOR)
            s1_status = r1["status"]
            s1_err = r1.get("error_code")
        except InvariantError as exc:
            s1_status = "REJECTED"
            s1_err = exc.code
        s1_odoo_state = bridge.read_odoo_raw_state(within_oid)
        assert s1_status == "REJECTED" and s1_err == "OPEN_CRITICAL_CRM_CASE_BLOCKS_ERP_ORDER"
        assert s1_odoo_state == "draft" and bridge.odoo_writes == 0

        # Scenario 2: Close BottleCRM SupportCase (settles SQLite) -> retry order confirmation (settles Odoo)
        r2_case = rt.submit_action(
            "DUAL-S2-CLOSE-CRM", "resolve_support_case", {"case_id": acme_case_canonical}, AI_OPS_ACTOR
        )
        assert r2_case["status"] == "COMMITTED"
        assert bridge.read_sqlite_raw_case_status(acme_case_canonical) == "Closed"

        r2_order = rt.submit_action(
            "DUAL-S2-CONFIRM-ERP", "confirm_fulfillment_order", {"order_id": within_oid}, AI_OPS_ACTOR
        )
        assert r2_order["status"] == "COMMITTED"
        s2_odoo_state = bridge.read_odoo_raw_state(within_oid)
        assert s2_odoo_state == "sale"

        # Scenario 3: Over-credit cumulative order -> APPROVAL_REQUIRED_* -> M1 WorkCard -> Steward settles
        s3_err_code = ""
        try:
            rt.submit_action(
                "DUAL-S3-OVER-CREDIT", "confirm_fulfillment_order", {"order_id": over_oid}, AI_OPS_ACTOR
            )
            raise AssertionError("expected over-credit confirmation by ai_agent to be blocked")
        except InvariantError as exc:
            s3_err_code = exc.code or ""
        assert s3_err_code == "APPROVAL_REQUIRED_CUMULATIVE_CREDIT_EXCEEDED"
        assert bridge.read_odoo_raw_state(over_oid) == "draft"
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []

        r3_wc_open = rt.submit_action(
            "DUAL-S3-WC-OPEN",
            "open_work_card",
            {
                "card_id": "WC-DUAL-S3",
                "card_kind": "APPROVAL",
                "target_object_type": "FulfillmentOrder",
                "target_object_id": over_oid,
                "requested_action": "confirm_fulfillment_order",
                "requested_params_json": json.dumps({"order_id": over_oid}),
                "assignee_role": "steward",
            },
            AI_OPS_ACTOR,
        )
        assert r3_wc_open["status"] == "COMMITTED"
        assert bridge.read_odoo_raw_state(over_oid) == "draft"

        r3_settled = settle_work_card(
            rt,
            "WC-DUAL-S3",
            STEWARD_OPS_ACTOR,
            resolution_note="steward approved cumulative exposure",
        )
        assert r3_settled["status"] == "COMMITTED"
        assert r3_settled["settled_action_run"]["status"] == "COMMITTED"
        assert bridge.read_odoo_raw_state(over_oid) == "sale"
        wc_s3 = rt.get_object(WorkCard, "WC-DUAL-S3", STEWARD_OPS_ACTOR)
        assert wc_s3.status == "COMPLETED"
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []

        # Scenario 4: Out-of-band external mismatch triggers DEVICE_STATE_MISMATCH without memory corruption
        fp_before = rt.state_fingerprint()
        bridge._skip_odoo_call = True
        drift_error_code = ""
        try:
            rt.submit_action("DUAL-S4-DRIFT", "confirm_fulfillment_order", {"order_id": drift_oid}, STEWARD_OPS_ACTOR)
        except DeviceStateMismatchError as exc:
            drift_error_code = exc.code
        finally:
            bridge._skip_odoo_call = False
        fp_after = rt.state_fingerprint()
        assert drift_error_code == DEVICE_STATE_MISMATCH
        assert fp_after["objects"] == fp_before["objects"]
        assert fp_after["versions"] == fp_before["versions"]
        assert bridge.read_odoo_raw_state(drift_oid) == "draft"

        return {
            "mode": "offline-dual-mount" if offline else "live-bottlecrm-x-odoo19",
            "sandbox_label": sandbox_label,
            "resolved_customers": len(seen_accounts),
            "sqlite_writes": bridge.sqlite_writes,
            "odoo_writes": bridge.odoo_writes,
            "scenario_1_crm_block": {"status": s1_status, "error_code": s1_err, "odoo_state": s1_odoo_state},
            "scenario_2_crm_unlock": {
                "crm_status": bridge.read_sqlite_raw_case_status(acme_case_canonical),
                "order_status": r2_order["status"],
                "odoo_state": s2_odoo_state,
            },
            "scenario_3_credit_approval": {
                "blocked_status": "REJECTED",
                "error_code": s3_err_code,
                "work_card_status": wc_s3.status,
                "approved_status": r3_settled["settled_action_run"]["status"],
                "odoo_state": bridge.read_odoo_raw_state(over_oid),
                "pending_approval_runs": len(rt.list_action_runs(status="PENDING_APPROVAL")),
            },
            "scenario_4_drift_guard": {
                "error_code": drift_error_code,
                "memory_uncorrupted": fp_after["objects"] == fp_before["objects"],
                "odoo_state": bridge.read_odoo_raw_state(drift_oid),
            },
        }
    finally:
        rt.close()
