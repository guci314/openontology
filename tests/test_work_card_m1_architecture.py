"""Phase 1 Acceptance Tests: Unified M1 WorkCard Architecture & Pure Sync Governance.

Covers all 4 Phase 1 verification groups:
1. Phantom-read defense red test (live t1 re-computation catches newly inserted SupportCase
   even when target order & customer versions did not change; WorkCard stays non-COMPLETED);
2. Segregation of Duties (SoD) red test (both Python M1 and compiled L1 declarative blueprints
   forbid self-approval/self-rejection by the requester);
3. Human & Department generalized Tool async lifecycle (OPEN -> IN_PROGRESS / SLA_BREACHED ->
   COMPLETED, duplicate active card prevention, cross-object WorkCard blocking cancel_order);
4. Pure synchronous InductionGovernanceService verification (zero post_approval / approval_rule /
   PENDING_APPROVAL in governance.py source or ontology_action_runs, full SoD + reject +
   concurrent STALE_BASE_VERSION + rollback).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from examples.business_ontology import (
    BUSINESS_SEED_ONTOLOGY,
    MasterEntity,
    ProcessDocument,
    WorkCard,
    build_no_active_work_card_precondition,
    build_work_card_action_blueprints,
    build_work_card_invariant_blueprints,
    build_work_card_object_blueprint,
    register_work_card_primitives,
    settle_work_card,
    spec as business_spec,
)
from open_ontology import (
    Actor,
    EditBatch,
    InvariantError,
    ObjectProperty,
    OntologyRuntime,
    OntologySpec,
    PermissionDeniedError,
    SourceBridge,
)
from open_ontology.induction import (
    BUILDER_ACTOR,
    CRUCIBLE_ACTOR,
    DeclarativeBlueprint,
    EVOLVER_ACTOR,
    InductionGovernanceService,
    STEWARD_ACTOR,
    blueprint_fingerprint,
    compile_blueprint,
)
from open_ontology.induction.dsl import ActionBlueprint, FieldSpec, InvariantRuleSpec, ObjectTypeBlueprint

REPO_ROOT = Path(__file__).resolve().parents[1]

AI_OPS = Actor(actor_id="agent:o2c-ai", role="ai_agent", clearance_tags={"*"})
STEWARD_1 = Actor(actor_id="human:steward-1", role="steward", clearance_tags={"*"})
STEWARD_2 = Actor(actor_id="human:steward-2", role="steward", clearance_tags={"*"})
WAREHOUSE_1 = Actor(actor_id="dept:warehouse-1", role="warehouse", clearance_tags={"*"})
OPS_1 = Actor(actor_id="human:ops-1", role="ops", clearance_tags={"*"})


def _build_o2c_work_card_spec(*, coupled_source: bool = False) -> tuple[OntologySpec, type[Any], type[Any], type[Any]]:
    s = OntologySpec(extends=(business_spec,))
    for r in ("ai_agent", "steward", "warehouse", "ops", "admin"):
        s.role(r)

    class Customer(MasterEntity):
        __primary_key__: ClassVar[str] = "customer_id"
        __title__: ClassVar[str] = "name"
        __owned__: ClassVar[bool] = not coupled_source

        customer_id: str
        name: str
        credit_limit: float = 100000.0

    class SupportCase(ProcessDocument):
        __primary_key__: ClassVar[str] = "case_id"
        __title__: ClassVar[str] = "case_id"
        __owned__: ClassVar[bool] = True
        __status_field__: ClassVar[str] = "status"
        __initial_statuses__: ClassVar[tuple[str, ...]] = ("OPEN",)
        __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {
            "OPEN": ("RESOLVED",),
            "RESOLVED": (),
        }

        case_id: str
        customer_id: str
        priority: str = "MEDIUM"
        status: str = "OPEN"

    class SalesOrder(ProcessDocument):
        __primary_key__: ClassVar[str] = "order_id"
        __title__: ClassVar[str] = "order_id"
        __owned__: ClassVar[bool] = not coupled_source
        __status_field__: ClassVar[str] = "status"
        __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
        __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {
            "draft": ("confirmed", "cancelled"),
            "confirmed": (),
            "cancelled": (),
        }

        order_id: str
        customer: ObjectProperty[Customer]
        amount: float = 0.0
        status: str = "draft"

    s.object_type(Customer)
    s.object_type(SupportCase)
    s.object_type(SalesOrder)

    class _ConfirmOrderParams(BaseModel):
        order_id: str

    class _CancelOrderParams(BaseModel):
        order_id: str

    class _OpenCaseParams(BaseModel):
        case_id: str
        customer_id: str
        priority: str = "URGENT"

    class _ResolveCaseParams(BaseModel):
        case_id: str

    @s.invariant(
        "confirmed_order_no_open_critical_case",
        code="OPEN_CRITICAL_CASE",
        description="Confirmed order cannot coexist with an unresolved HIGH/URGENT SupportCase on the customer.",
    )
    def _no_confirmed_order_with_critical_case(view: Any) -> bool:
        cases_by_cust: dict[str, list[SupportCase]] = {}
        for c in view.query(SupportCase):
            if c.status != "RESOLVED" and c.priority in ("HIGH", "URGENT"):
                cases_by_cust.setdefault(c.customer_id, []).append(c)
        for o in view.query(SalesOrder):
            if o.status == "confirmed" and cases_by_cust.get(str(o.customer)):
                open_ids = [c.case_id for c in cases_by_cust[str(o.customer)]]
                raise InvariantError(
                    f"SalesOrder:{o.order_id} blocked by open critical SupportCase(s): {open_ids}",
                    code="OPEN_CRITICAL_CASE",
                )
        return True

    @s.action(
        "confirm_order",
        _ConfirmOrderParams,
        allowed_roles=("ai_agent", "steward", "admin"),
        affects_source=coupled_source,
    )
    def _confirm_order(ctx: EditBatch, params: _ConfirmOrderParams) -> None:
        order = ctx.require(SalesOrder, params.order_id)
        cust = ctx.require(Customer, str(order.customer))
        if order.amount > cust.credit_limit and ctx.actor.role not in ("steward", "admin"):
            raise InvariantError(
                f"order {order.order_id} amount {order.amount} exceeds credit_limit {cust.credit_limit}",
                code="CREDIT_LIMIT_EXCEEDED",
            )
        ctx.modify(order, status="confirmed")

    @s.action(
        "cancel_order",
        _CancelOrderParams,
        allowed_roles=("ai_agent", "steward", "admin"),
        affects_source=False,
    )
    def _cancel_order(ctx: EditBatch, params: _CancelOrderParams) -> None:
        order = ctx.require(SalesOrder, params.order_id)
        active_cards = [
            wc.card_id
            for wc in ctx.query(WorkCard)
            if wc.target_object_type == "SalesOrder"
            and wc.target_object_id == order.order_id
            and wc.status in ("OPEN", "IN_PROGRESS")
        ]
        if active_cards:
            raise InvariantError(
                f"SalesOrder:{order.order_id} has active WorkCard(s): {active_cards}",
                code="ACTIVE_WORK_CARD_BLOCKS_ACTION",
            )
        ctx.modify(order, status="cancelled")

    @s.action(
        "open_support_case",
        _OpenCaseParams,
        allowed_roles=("ai_agent", "ops", "steward", "admin"),
        affects_source=False,
    )
    def _open_case(ctx: EditBatch, params: _OpenCaseParams) -> None:
        ctx.require(Customer, params.customer_id)
        ctx.create(
            SupportCase(
                case_id=params.case_id,
                customer_id=params.customer_id,
                priority=params.priority,
                status="OPEN",
            )
        )

    @s.action(
        "resolve_support_case",
        _ResolveCaseParams,
        allowed_roles=("ops", "steward", "admin"),
        affects_source=False,
    )
    def _resolve_case(ctx: EditBatch, params: _ResolveCaseParams) -> None:
        case = ctx.require(SupportCase, params.case_id)
        ctx.modify(case, status="RESOLVED")

    register_work_card_primitives(s)
    return s, Customer, SupportCase, SalesOrder


def _object_version(rt: OntologyRuntime, object_type: str, object_id: str) -> int:
    row = rt._conn.execute(
        "SELECT version FROM ontology_objects WHERE object_type = ? AND object_id = ?",
        (object_type, object_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# 1. Phantom-Read Defense Red Test (t1 Live Re-computation)
# ---------------------------------------------------------------------------
def test_phantom_read_defense_live_t1_recomputation() -> None:
    """INSERT of a new urgent SupportCase between t0 and t1 must block APPROVAL completion at t1."""
    spec, Customer, SupportCase, SalesOrder = _build_o2c_work_card_spec()
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        rt.put_object(Customer(customer_id="C-01", name="Acme Corp", credit_limit=100000.0))
        rt.put_object(SalesOrder(order_id="SO-01", customer="C-01", amount=150000.0, status="draft"))

        # t0: AI tries confirm_order -> blocked by credit limit; opens APPROVAL WorkCard (raw intent only)
        with pytest.raises(InvariantError) as exc_t0:
            rt.submit_action("RUN-CFM-T0", "confirm_order", {"order_id": "SO-01"}, AI_OPS)
        assert exc_t0.value.code == "CREDIT_LIMIT_EXCEEDED"

        open_res = rt.submit_action(
            "RUN-WC-OPEN",
            "open_work_card",
            {
                "card_id": "WC-APPR-01",
                "card_kind": "APPROVAL",
                "target_object_type": "SalesOrder",
                "target_object_id": "SO-01",
                "requested_action": "confirm_order",
                "requested_params_json": '{"order_id": "SO-01"}',
                "assignee_role": "steward",
                "external_ref": "feishu:om_appr_001",
            },
            AI_OPS,
        )
        assert open_res["status"] == "COMMITTED"
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []

        so_ver_t0 = _object_version(rt, "SalesOrder", "SO-01")
        cust_ver_t0 = _object_version(rt, "Customer", "C-01")

        # Between t0 and t1: INSERT a brand-new URGENT SupportCase (neither SalesOrder nor Customer version changes!)
        rt.submit_action(
            "RUN-CASE-PHANTOM",
            "open_support_case",
            {"case_id": "CASE-URGENT-1", "customer_id": "C-01", "priority": "URGENT"},
            OPS_1,
        )
        assert _object_version(rt, "SalesOrder", "SO-01") == so_ver_t0
        assert _object_version(rt, "Customer", "C-01") == cust_ver_t0

        # t1: Steward approves & completes WorkCard -> live t1 re-execution catches OPEN_CRITICAL_CASE!
        with pytest.raises(InvariantError) as exc_t1:
            rt.submit_action(
                "RUN-WC-APPROVE-FAIL",
                "complete_work_card",
                {"card_id": "WC-APPR-01", "resolution_note": "approve over-limit order"},
                STEWARD_1,
            )
        assert exc_t1.value.code == "OPEN_CRITICAL_CASE"

        # Zero dirty write & WorkCard MUST remain OPEN (never marked COMPLETED!)
        assert rt.get_object(SalesOrder, "SO-01", STEWARD_1).status == "draft"
        card_mid = rt.get_object(WorkCard, "WC-APPR-01", STEWARD_1)
        assert card_mid.status == "OPEN"
        assert card_mid.resolved_by_actor_id == ""
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []

        # Resolve SupportCase -> Steward approves again -> succeeds cleanly with atomic OCC
        rt.submit_action("RUN-CASE-RESOLVE", "resolve_support_case", {"case_id": "CASE-URGENT-1"}, OPS_1)
        assert rt.get_object(SupportCase, "CASE-URGENT-1", STEWARD_1).status == "RESOLVED"

        ok_res = rt.submit_action(
            "RUN-WC-APPROVE-OK",
            "complete_work_card",
            {"card_id": "WC-APPR-01", "resolution_note": "approved after critical case resolved"},
            STEWARD_1,
        )
        assert ok_res["status"] == "COMMITTED"
        assert rt.get_object(SalesOrder, "SO-01", STEWARD_1).status == "confirmed"
        card_done = rt.get_object(WorkCard, "WC-APPR-01", STEWARD_1)
        assert card_done.status == "COMPLETED"
        assert card_done.resolved_by_actor_id == STEWARD_1.actor_id
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []
    finally:
        rt.close()


def test_coupled_source_work_card_phantom_read_zero_external_actuation() -> None:
    """Coupled SourceBridge APPROVAL WorkCard: t1 failure must have actuate_calls == 0."""
    spec, Customer, SupportCase, SalesOrder = _build_o2c_work_card_spec(coupled_source=True)
    ext_orders: dict[str, dict[str, Any]] = {
        "SO-01": {"order_id": "SO-01", "customer": "C-01", "amount": 150000.0, "status": "draft"}
    }
    actuate_calls = 0

    def _actuate(_mv: dict[str, Any]) -> dict[str, Any]:
        nonlocal actuate_calls
        actuate_calls += 1
        ext_orders["SO-01"]["status"] = "confirmed"
        return {"ok": True}

    def _observe() -> dict[str, Any]:
        return {
            "Customer": [{"customer_id": "C-01", "name": "Acme Corp", "credit_limit": 100000.0}],
            "SalesOrder": list(ext_orders.values()),
        }

    rt = OntologyRuntime(
        spec,
        db_path=":memory:",
        world_mode="coupled",
        source_bridge=SourceBridge(actuate=_actuate, observe=_observe),
    )
    try:
        rt.load_snapshot(
            {
                "Customer": [{"customer_id": "C-01", "name": "Acme Corp", "credit_limit": 100000.0}],
                "SalesOrder": [{"order_id": "SO-01", "customer": "C-01", "amount": 150000.0, "status": "draft"}],
            }
        )

        # t0: credit limit blocks AI agent; AI opens APPROVAL WorkCard
        with pytest.raises(InvariantError) as exc_t0:
            rt.submit_action("SRC-CFM-T0", "confirm_order", {"order_id": "SO-01"}, AI_OPS)
        assert exc_t0.value.code == "CREDIT_LIMIT_EXCEEDED"
        assert actuate_calls == 0

        rt.submit_action(
            "SRC-WC-OPEN",
            "open_work_card",
            {
                "card_id": "WC-SRC-01",
                "card_kind": "APPROVAL",
                "target_object_type": "SalesOrder",
                "target_object_id": "SO-01",
                "requested_action": "confirm_order",
                "requested_params_json": '{"order_id": "SO-01"}',
                "assignee_role": "steward",
            },
            AI_OPS,
        )

        # Phantom critical SupportCase arrives between t0 and t1
        rt.submit_action(
            "SRC-CASE-OPEN",
            "open_support_case",
            {"case_id": "CASE-SRC-1", "customer_id": "C-01", "priority": "URGENT"},
            OPS_1,
        )

        # Calling raw complete_work_card without settle_work_card is also fail-closed
        with pytest.raises(InvariantError) as raw_exc:
            rt.submit_action(
                "SRC-WC-RAW-BYPASS",
                "complete_work_card",
                {"card_id": "WC-SRC-01"},
                STEWARD_1,
            )
        assert raw_exc.value.code == "SOURCE_APPROVAL_REQUIRES_SETTLED_RUN"
        assert actuate_calls == 0

        # t1 via settle_work_card: fails with OPEN_CRITICAL_CASE and actuate_calls == 0!
        with pytest.raises(InvariantError) as exc_t1:
            settle_work_card(
                rt,
                "WC-SRC-01",
                STEWARD_1,
                resolution_note="attempt approval with open urgent case",
            )
        assert exc_t1.value.code == "OPEN_CRITICAL_CASE"
        assert actuate_calls == 0
        assert rt.get_object(SalesOrder, "SO-01", STEWARD_1).status == "draft"
        assert rt.get_object(WorkCard, "WC-SRC-01", STEWARD_1).status == "OPEN"

        # Resolve SupportCase -> settle_work_card succeeds cleanly with SourceBridge actuation
        rt.submit_action("SRC-CASE-RESOLVE", "resolve_support_case", {"case_id": "CASE-SRC-1"}, OPS_1)
        assert rt.get_object(SupportCase, "CASE-SRC-1", STEWARD_1).status == "RESOLVED"

        ok = settle_work_card(
            rt,
            "WC-SRC-01",
            STEWARD_1,
            resolution_note="approved after case closed",
            external_ref="feishu:ok_001",
        )
        assert ok["status"] == "COMMITTED"
        assert ok["settled_action_run"]["status"] == "COMMITTED"
        assert actuate_calls == 1
        assert rt.get_object(SalesOrder, "SO-01", STEWARD_1).status == "confirmed"
        card_done = rt.get_object(WorkCard, "WC-SRC-01", STEWARD_1)
        assert card_done.status == "COMPLETED"
        assert card_done.resolved_by_actor_id == STEWARD_1.actor_id
        assert card_done.external_ref == "feishu:ok_001"
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. Segregation of Duties (SoD) Red Test (Python M1 + Declarative L1 Parity)
# ---------------------------------------------------------------------------
def test_sod_blocks_self_approval_and_declarative_blueprint_parity() -> None:
    """Requester cannot self-approve or self-reject even when holding steward/admin role."""
    spec, Customer, _SupportCase, SalesOrder = _build_o2c_work_card_spec()
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        rt.put_object(Customer(customer_id="C-SOD", name="SoD Corp", credit_limit=50000.0))
        rt.put_object(SalesOrder(order_id="SO-SOD", customer="C-SOD", amount=80000.0, status="draft"))

        # Steward 1 opens an approval card on behalf of a transaction
        rt.submit_action(
            "SOD-OPEN-1",
            "open_work_card",
            {
                "card_id": "WC-SOD-1",
                "card_kind": "APPROVAL",
                "target_object_type": "SalesOrder",
                "target_object_id": "SO-SOD",
                "requested_action": "confirm_order",
                "requested_params_json": '{"order_id": "SO-SOD"}',
                "assignee_role": "steward",
            },
            STEWARD_1,
        )

        # Self-completion and self-rejection MUST fail with SOD_SELF_APPROVAL_FORBIDDEN
        with pytest.raises(InvariantError) as exc_self_ok:
            rt.submit_action("SOD-SELF-OK", "complete_work_card", {"card_id": "WC-SOD-1"}, STEWARD_1)
        assert exc_self_ok.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        with pytest.raises(InvariantError) as exc_self_rej:
            rt.submit_action("SOD-SELF-REJ", "reject_work_card", {"card_id": "WC-SOD-1"}, STEWARD_1)
        assert exc_self_rej.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        assert rt.get_object(WorkCard, "WC-SOD-1", STEWARD_1).status == "OPEN"

        # Another role cannot bypass assignee_role
        with pytest.raises(PermissionDeniedError):
            rt.submit_action("SOD-WRONG-ROLE", "complete_work_card", {"card_id": "WC-SOD-1"}, AI_OPS)
        with pytest.raises(InvariantError) as exc_assignee:
            rt.submit_action("SOD-WRONG-ASSIGNEE", "complete_work_card", {"card_id": "WC-SOD-1"}, WAREHOUSE_1)
        assert exc_assignee.value.code == "WORK_CARD_ASSIGNEE_MISMATCH"

        # Distinct Steward 2 completes cleanly
        done = rt.submit_action(
            "SOD-OTHER-OK",
            "complete_work_card",
            {"card_id": "WC-SOD-1", "resolution_note": "independent steward sign-off"},
            STEWARD_2,
        )
        assert done["status"] == "COMMITTED"
        assert rt.get_object(SalesOrder, "SO-SOD", STEWARD_2).status == "confirmed"
        assert rt.get_object(WorkCard, "WC-SOD-1", STEWARD_2).resolved_by_actor_id == STEWARD_2.actor_id
    finally:
        rt.close()

    # Parity check: Declarative blueprint helpers compiled via SeedOntology enforce same SoD, dedup & blocking
    dec_bp = DeclarativeBlueprint(
        model_id="declarative_work_card_parity",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        object_types=[
            ObjectTypeBlueprint(
                name="Ticket",
                archetype="ProcessDocument",
                primary_key="ticket_id",
                owned=True,
                status_field="status",
                initial_statuses=["open"],
                transitions={"open": ["cancelled"], "cancelled": []},
                fields=[
                    FieldSpec(name="ticket_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="status", field_type="str", default="open"),
                ],
            ),
            ObjectTypeBlueprint.model_validate(build_work_card_object_blueprint()),
        ],
        invariants=[InvariantRuleSpec.model_validate(x) for x in build_work_card_invariant_blueprints()],
        actions=[
            ActionBlueprint(
                name="cancel_ticket",
                allowed_roles=["ai_agent", "steward"],
                affects_source=False,
                params=[FieldSpec(name="ticket_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec.model_validate(
                        build_no_active_work_card_precondition(target_object_type="Ticket", id_param="ticket_id")
                    )
                ],
                mutations=[
                    {
                        "op": "modify",
                        "object_type": "Ticket",
                        "id_param": "ticket_id",
                        "set_fields": {"status": {"lit": "cancelled"}},
                    }
                ],
            ),
            *[ActionBlueprint.model_validate(a) for a in build_work_card_action_blueprints()],
        ],
    )
    dec_spec = compile_blueprint(dec_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    dec_rt = OntologyRuntime(dec_spec, db_path=":memory:")
    try:
        TicketCls = dec_spec.get_object_type("Ticket")
        WcCls = dec_spec.get_object_type("WorkCard")
        dec_rt.put_object(TicketCls(ticket_id="TK-1", status="open"))
        dec_rt.submit_action(
            "DEC-OPEN-1",
            "open_work_card",
            {
                "card_id": "WC-DEC-1",
                "card_kind": "HUMAN_TASK",
                "target_object_type": "Ticket",
                "target_object_id": "TK-1",
                "requested_action": "verify_docs",
                "assignee_role": "steward",
            },
            STEWARD_1,
        )
        # Duplicate active declarative WorkCard rejected
        with pytest.raises(InvariantError) as dec_dup:
            dec_rt.submit_action(
                "DEC-OPEN-DUP",
                "open_work_card",
                {
                    "card_id": "WC-DEC-2",
                    "card_kind": "HUMAN_TASK",
                    "target_object_type": "Ticket",
                    "target_object_id": "TK-1",
                    "requested_action": "verify_docs",
                    "assignee_role": "steward",
                },
                AI_OPS,
            )
        assert dec_dup.value.code == "DUPLICATE_ACTIVE_WORK_CARD"

        # Active WorkCard blocks cancel_ticket via build_no_active_work_card_precondition
        with pytest.raises(InvariantError) as dec_blk:
            dec_rt.submit_action("DEC-CANCEL-BLK", "cancel_ticket", {"ticket_id": "TK-1"}, AI_OPS)
        assert dec_blk.value.code == "ACTIVE_WORK_CARD_BLOCKS_ACTION"

        # Self-approval blocked in declarative blueprint
        with pytest.raises(InvariantError) as dec_sod:
            dec_rt.submit_action("DEC-SELF", "complete_work_card", {"card_id": "WC-DEC-1"}, STEWARD_1)
        assert dec_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        dec_rt.submit_action(
            "DEC-OK",
            "complete_work_card",
            {"card_id": "WC-DEC-1", "resolution_note": "verified"},
            STEWARD_2,
        )
        assert dec_rt.get_object(WcCls, "WC-DEC-1", STEWARD_2).status == "COMPLETED"
        dec_rt.submit_action("DEC-CANCEL-OK", "cancel_ticket", {"ticket_id": "TK-1"}, AI_OPS)
        assert dec_rt.get_object(TicketCls, "TK-1", AI_OPS).status == "cancelled"
    finally:
        dec_rt.close()


# ---------------------------------------------------------------------------
# 3. Human + Dept Generalized Tool Lifecycle & Cross-Object Invariants
# ---------------------------------------------------------------------------
def test_human_and_dept_work_card_lifecycle_and_cross_object_invariants() -> None:
    """Verify DEPT_WORK + HUMAN_TASK async lifecycle, SLA breach recovery, dedup, and cross-object blocking."""
    spec, Customer, _SupportCase, SalesOrder = _build_o2c_work_card_spec()
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        rt.put_object(Customer(customer_id="C-DEPT", name="North Co", credit_limit=200000.0))
        rt.put_object(SalesOrder(order_id="SO-200", customer="C-DEPT", amount=30000.0, status="draft"))

        # Dispatch DEPT_WORK to warehouse & HUMAN_TASK to ops
        rt.submit_action(
            "WC-DEPT-OPEN",
            "open_work_card",
            {
                "card_id": "WC-DEPT-1",
                "card_kind": "DEPT_WORK",
                "target_object_type": "SalesOrder",
                "target_object_id": "SO-200",
                "requested_action": "warehouse_pick_and_pack",
                "assignee_role": "warehouse",
                "assignee_dept": "logistics",
                "sla_due_at": "2026-11-21T12:00:00Z",
                "external_ref": "feishu:om_dept_01",
            },
            AI_OPS,
        )
        # Duplicate DEPT_WORK on same target+action blocked
        with pytest.raises(InvariantError) as exc_dup:
            rt.submit_action(
                "WC-DEPT-DUP",
                "open_work_card",
                {
                    "card_id": "WC-DEPT-DUP-1",
                    "card_kind": "DEPT_WORK",
                    "target_object_type": "SalesOrder",
                    "target_object_id": "SO-200",
                    "requested_action": "warehouse_pick_and_pack",
                    "assignee_role": "warehouse",
                },
                AI_OPS,
            )
        assert exc_dup.value.code == "DUPLICATE_ACTIVE_WORK_CARD"

        # Separate HUMAN_TASK on the same order succeeds
        rt.submit_action(
            "WC-HUMAN-OPEN",
            "open_work_card",
            {
                "card_id": "WC-HUMAN-1",
                "card_kind": "HUMAN_TASK",
                "target_object_type": "SalesOrder",
                "target_object_id": "SO-200",
                "requested_action": "customs_clearance_check",
                "assignee_role": "ops",
                "external_ref": "ticket:OPS-09",
            },
            AI_OPS,
        )

        # Dept claims card -> OPEN -> IN_PROGRESS and updates external_ref
        rt.submit_action(
            "WC-DEPT-CLAIM",
            "claim_work_card",
            {"card_id": "WC-DEPT-1", "external_ref": "wms:PICK-778"},
            WAREHOUSE_1,
        )
        dept_card = rt.get_object(WorkCard, "WC-DEPT-1", WAREHOUSE_1)
        assert dept_card.status == "IN_PROGRESS"
        assert dept_card.claimed_by_actor_id == WAREHOUSE_1.actor_id
        assert dept_card.external_ref == "wms:PICK-778"

        # M1 cross-object invariant blocks cancelling SO-200 while DEPT_WORK/HUMAN_TASK is OPEN/IN_PROGRESS
        with pytest.raises(InvariantError) as exc_cancel:
            rt.submit_action("SO-CANCEL-BLOCKED", "cancel_order", {"order_id": "SO-200"}, AI_OPS)
        assert exc_cancel.value.code == "ACTIVE_WORK_CARD_BLOCKS_ACTION"

        # Complete DEPT_WORK
        rt.submit_action(
            "WC-DEPT-DONE",
            "complete_work_card",
            {"card_id": "WC-DEPT-1", "resolution_note": "3 pallets packed"},
            WAREHOUSE_1,
        )
        assert rt.get_object(WorkCard, "WC-DEPT-1", WAREHOUSE_1).status == "COMPLETED"

        # HUMAN_TASK breaches SLA, gets claimed & recovered -> COMPLETED
        rt.submit_action(
            "WC-HUMAN-SLA",
            "mark_work_card_sla_breached",
            {"card_id": "WC-HUMAN-1", "resolution_note": "exceeded 2h SLA"},
            AI_OPS,
        )
        assert rt.get_object(WorkCard, "WC-HUMAN-1", OPS_1).status == "SLA_BREACHED"
        rt.submit_action("WC-HUMAN-CLAIM", "claim_work_card", {"card_id": "WC-HUMAN-1"}, OPS_1)
        assert rt.get_object(WorkCard, "WC-HUMAN-1", OPS_1).status == "IN_PROGRESS"
        rt.submit_action(
            "WC-HUMAN-DONE",
            "complete_work_card",
            {"card_id": "WC-HUMAN-1", "resolution_note": "docs cleared"},
            OPS_1,
        )
        assert rt.get_object(WorkCard, "WC-HUMAN-1", OPS_1).status == "COMPLETED"

        # Now no OPEN/IN_PROGRESS WorkCards remain on SO-200 -> cancel_order commits
        res_cancel = rt.submit_action("SO-CANCEL-OK", "cancel_order", {"order_id": "SO-200"}, AI_OPS)
        assert res_cancel["status"] == "COMMITTED"
        assert rt.get_object(SalesOrder, "SO-200", AI_OPS).status == "cancelled"

        # Opening & cancelling a fresh card after prior completion succeeds
        rt.submit_action(
            "WC-DEPT-REOPEN",
            "open_work_card",
            {
                "card_id": "WC-DEPT-2",
                "card_kind": "DEPT_WORK",
                "target_object_type": "SalesOrder",
                "target_object_id": "SO-200",
                "requested_action": "warehouse_pick_and_pack",
                "assignee_role": "warehouse",
            },
            AI_OPS,
        )
        rt.submit_action("WC-DEPT-CANCEL", "cancel_work_card", {"card_id": "WC-DEPT-2"}, AI_OPS)
        assert rt.get_object(WorkCard, "WC-DEPT-2", AI_OPS).status == "CANCELLED"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 4. Meta-Governance Pure Synchronous Verification
# ---------------------------------------------------------------------------
def test_governance_pure_sync_zero_pending_approval_and_source_clean() -> None:
    """governance.py must have zero post_approval/PENDING_APPROVAL/approval_rule and zero pending runs."""
    gov_src = (REPO_ROOT / "open_ontology" / "induction" / "governance.py").read_text(encoding="utf-8")
    assert "post_approval" not in gov_src
    assert "PENDING_APPROVAL" not in gov_src
    assert "approval_rule" not in gov_src

    bp_v1 = DeclarativeBlueprint(
        model_id="sync_gov_test",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Item",
                primary_key="item_id",
                fields=[FieldSpec(name="item_id", field_type="str", primary_key=True, required=True)],
            )
        ],
    )
    bp_v11_a = bp_v1.model_copy(update={"version": "1.1.0"})
    bp_v11_b = bp_v1.model_copy(update={"version": "1.1.1"})

    with InductionGovernanceService(db_path=":memory:") as gov:
        # 1) Propose + Verify Genesis v1.0.0
        gov.runtime.submit_action(
            "PROP-G",
            "propose_genesis_model",
            {
                "proposal_id": "PG-1",
                "model_id": "sync_gov_test",
                "target_version": "1.0.0",
                "blueprint_json": bp_v1.model_dump_json(),
            },
            BUILDER_ACTOR,
        )
        gov.runtime.submit_action(
            "RPT-G",
            "record_falsification_report",
            {
                "report_id": "RG-1",
                "proposal_id": "PG-1",
                "passed": True,
                "blueprint_hash": blueprint_fingerprint(bp_v1),
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        gov.request_crystallization("PG-1", BUILDER_ACTOR, run_id="REQ-PG-1")
        assert [p.proposal_id for p in gov.list_pending_crystallizations("sync_gov_test")] == ["PG-1"]

        with pytest.raises(PermissionDeniedError):
            gov.approve_crystallization("PG-1", BUILDER_ACTOR)
        with pytest.raises(PermissionDeniedError):
            gov.reject_crystallization("PG-1", EVOLVER_ACTOR)

        app = gov.approve_crystallization("PG-1", STEWARD_ACTOR)
        assert app["status"] == "COMMITTED"
        assert gov.get_active_blueprint("sync_gov_test").version == "1.0.0"

        # 2) SoD on governance + reject path
        gov.runtime.submit_action(
            "PROP-E-REJ",
            "propose_evolution_patch",
            {
                "proposal_id": "PE-REJ",
                "model_id": "sync_gov_test",
                "base_version": "1.0.0",
                "target_version": "1.0.9",
                "blueprint_json": bp_v1.model_copy(update={"version": "1.0.9"}).model_dump_json(),
                "patch_json": "{}",
            },
            EVOLVER_ACTOR,
        )
        gov.runtime.submit_action(
            "RPT-E-REJ",
            "record_falsification_report",
            {
                "report_id": "RE-REJ",
                "proposal_id": "PE-REJ",
                "passed": True,
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        gov.request_crystallization("PE-REJ", STEWARD_2)
        with pytest.raises(InvariantError) as exc_gov_sod:
            gov.approve_crystallization("PE-REJ", STEWARD_2)
        assert exc_gov_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        rej = gov.reject_crystallization("PE-REJ", STEWARD_ACTOR, reason="policy hold")
        assert rej["status"] == "COMMITTED"
        pe_rej_obj = next(p for p in gov.list_proposals("sync_gov_test") if p.proposal_id == "PE-REJ")
        assert pe_rej_obj.status == "REJECTED"
        assert pe_rej_obj.rejection_reason == "policy hold"

        # 3) Concurrent evolution proposals -> approve first -> second blocked at t1 by STALE_BASE_VERSION -> rollback
        for pid, bp in (("PE-A", bp_v11_a), ("PE-B", bp_v11_b)):
            gov.runtime.submit_action(
                f"P-{pid}",
                "propose_evolution_patch",
                {
                    "proposal_id": pid,
                    "model_id": "sync_gov_test",
                    "base_version": "1.0.0",
                    "target_version": bp.version,
                    "blueprint_json": bp.model_dump_json(),
                    "patch_json": "{}",
                },
                EVOLVER_ACTOR,
            )
            gov.runtime.submit_action(
                f"R-{pid}",
                "record_falsification_report",
                {
                    "report_id": f"RPT-{pid}",
                    "proposal_id": pid,
                    "passed": True,
                    "blueprint_hash": blueprint_fingerprint(bp),
                    "report_json": '{"passed": true}',
                },
                CRUCIBLE_ACTOR,
            )
            gov.request_crystallization(pid, EVOLVER_ACTOR)

        gov.approve_crystallization("PE-A", STEWARD_ACTOR)
        assert gov.get_active_blueprint("sync_gov_test").version == "1.1.0"

        with pytest.raises(InvariantError) as exc_stale:
            gov.approve_crystallization("PE-B", STEWARD_ACTOR)
        assert exc_stale.value.code == "STALE_BASE_VERSION"

        gov.rollback_crystallization("PE-A", steward=STEWARD_ACTOR)
        assert gov.get_active_blueprint("sync_gov_test").version == "1.0.0"

        assert gov.runtime.list_action_runs(status="PENDING_APPROVAL") == []
