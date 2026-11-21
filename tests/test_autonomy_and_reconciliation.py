"""Offline deterministic fault-injection tests for WO-19 Production Safety Shells:

1. Three-stage Autonomy Ramp (``open_ontology/autonomy.py``):
   - ``SHADOW`` mode (direct controller + ``OntologyMCPServer``): ABox 0 mutations,
     ``SourceBridge.actuate`` 0 calls, immutable memory & JSONL shadow ledger,
     plus ``shadow_report()`` metrics & ``/health`` exposure.
   - ``COPILOT_ESCALATE`` mode: invalid actions fail fast with zero escalation spam;
     valid actions escalate via M1 ``open_work_card`` without firing original actuation
     (and support ``ESCALATION_REQUIRED`` when unconfigured).
   - ``AUTO_UNDER_CEILING`` mode: auto-commits under ceiling (``amount_total <= 100000``),
     downgrades to ``open_work_card`` over ceiling (``> 100000``), and fails closed on
     missing ceiling metric.
2. Observe-Before-Retry Saga Reconciler (``open_ontology/reconciliation.py``):
   - Branch 1: remote commit succeeded before ``TimeoutError`` ACK loss ->
     ``actuate`` called ONCE (zero duplicate execution), ``RECONCILED_REMOTE_COMMITTED``,
     ABox atomically ``COMMITTED``.
   - Branch 2: packet dropped (``ConnectionError``, still at ``before`` state) ->
     retried with IDENTICAL deterministic SHA-256 ``idempotency_key`` -> ``RETRIED_AND_COMMITTED``.
   - Branch 3: post-timeout drift to unknown third state -> NEVER retried, raises
     ``ReconciliationAmbiguousError`` (``RECONCILIATION_STATE_AMBIGUOUS``), fires
     ``on_drift`` callback, preserves ABox with 0 dirty writes, and persists ``REJECTED``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from open_ontology import Actor, OntologyRuntime
from open_ontology.autonomy import (
    ActionAutonomyRule,
    AutonomyPolicy,
    AutonomyRampController,
)
from open_ontology.mcp_server import OntologyMCPServer
from open_ontology.reconciliation import (
    RECONCILIATION_STATE_AMBIGUOUS,
    IdempotentSourceBridge,
    ReconciliationAmbiguousError,
    ReconciliationRecord,
    SagaReconciler,
    derive_idempotency_key,
)
from open_ontology.settlement import SourceBridge

from examples.std_core_ontology import AccountingEntity, BusinessParty
from examples.std_p2p_ontology import (
    P2pPurchaseOrder,
    P2pSupplierProfile,
    spec as std_p2p_spec,
)


def _escalation_to_work_card(ctx: dict[str, Any]) -> dict[str, Any]:
    params = dict(ctx["params"])
    target_id = str(params.get("po_id") or "")
    assignee = ctx["rule"].escalation_assignee_role or "procurement_manager"
    return {
        "card_id": f"WC-{ctx['run_id']}",
        "card_kind": "APPROVAL",
        "target_object_type": "P2pPurchaseOrder",
        "target_object_id": target_id,
        "requested_action": str(ctx["action"]),
        "requested_params_json": json.dumps(params, sort_keys=True, ensure_ascii=False),
        "assignee_role": assignee,
    }


def _seed_coupled_p2p_world(
    orders: list[tuple[str, float, str]],
    *,
    actuate_fn: Any = None,
    bridge_wrapper: Any = None,
) -> tuple[OntologyRuntime, dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    remote_state: dict[str, dict[str, dict[str, Any]]] = {
        "AccountingEntity": {
            "ENT-1": {"entity_id": "ENT-1", "name": "Buyer Holdings", "base_currency": "CNY", "status": "active"}
        },
        "BusinessParty": {
            "PTY-1": {"party_id": "PTY-1", "legal_name": "North Forge Co", "tax_id": "TAX-01", "country_code": "CN", "status": "active"}
        },
        "P2pSupplierProfile": {
            "SUP-1": {
                "profile_id": "SUP-1",
                "party": "PTY-1",
                "accounting_entity": "ENT-1",
                "payment_terms": "NET30",
                "status": "active",
            }
        },
        "P2pPurchaseOrder": {},
    }
    actuate_log: list[dict[str, Any]] = []

    for po_id, total, status in orders:
        remote_state["P2pPurchaseOrder"][po_id] = {
            "po_id": po_id,
            "po_number": f"NUM-{po_id}",
            "supplier": "SUP-1",
            "accounting_entity": "ENT-1",
            "ordered_qty": 10.0,
            "unit_price": round(total / 10.0, 2),
            "amount_total": float(total),
            "status": status,
        }

    def default_actuate(move: dict[str, Any]) -> dict[str, Any]:
        actuate_log.append(copy.deepcopy(move))
        po_id = str((move.get("params") or {}).get("po_id") or "")
        if move.get("action_type") == "confirm_p2p_order" and po_id in remote_state["P2pPurchaseOrder"]:
            remote_state["P2pPurchaseOrder"][po_id]["status"] = "confirmed"
        return {"ok": True}

    def wrapped_actuate(move: dict[str, Any]) -> Any:
        if actuate_fn is not None:
            return actuate_fn(move, remote_state, actuate_log)
        return default_actuate(move)

    def observe() -> dict[str, list[dict[str, Any]]]:
        return {
            t: [copy.deepcopy(row) for row in bucket.values()]
            for t, bucket in remote_state.items()
        }

    raw_bridge = SourceBridge(actuate=wrapped_actuate, observe=observe)
    effective_bridge = bridge_wrapper(raw_bridge) if bridge_wrapper is not None else raw_bridge
    rt = OntologyRuntime(std_p2p_spec, db_path=":memory:", world_mode="coupled", source_bridge=effective_bridge)
    rt.put_object(AccountingEntity(**remote_state["AccountingEntity"]["ENT-1"]))
    rt.put_object(BusinessParty(**remote_state["BusinessParty"]["PTY-1"]))
    rt.put_object(P2pSupplierProfile(**remote_state["P2pSupplierProfile"]["SUP-1"]))
    for row in remote_state["P2pPurchaseOrder"].values():
        rt.put_object(P2pPurchaseOrder(**row))
    return rt, remote_state, actuate_log


def test_shadow_mode_direct_and_mcp_zero_mutation_and_jsonl_ledger(tmp_path: Path) -> None:
    ledger_file = tmp_path / "shadow" / "decisions.jsonl"
    rt, remote_state, actuate_log = _seed_coupled_p2p_world(
        [("PO-1", 50000.0, "draft"), ("PO-2", 60000.0, "confirmed")]
    )
    controller = AutonomyRampController(
        AutonomyPolicy(default_mode="SHADOW"),
        ledger_path=ledger_file,
    )
    actor = Actor(actor_id="agent-shadow", role="ai_agent", clearance_tags={"*"})

    try:
        dec_ok = controller.execute(rt, actor, "sh-1", "confirm_p2p_order", {"po_id": "PO-1"})
        assert dec_ok.outcome == "SHADOW_RECORDED"
        assert dec_ok.valid is True
        assert dec_ok.committed is False
        assert dec_ok.run is None

        dec_rej = controller.execute(rt, actor, "sh-2", "confirm_p2p_order", {"po_id": "PO-2"})
        assert dec_rej.outcome == "SHADOW_WOULD_REJECT"
        assert dec_rej.valid is False
        assert dec_rej.error_code == "PO_NOT_DRAFT"
        assert dec_rej.committed is False

        mcp = OntologyMCPServer(rt, actor, autonomy_controller=controller)
        health = mcp.health_payload()
        assert health["autonomy_default_mode"] == "SHADOW"

        res_mcp_ok = mcp.call_tool(
            "submit_action",
            {"run_id": "mcp-sh-1", "action": "confirm_p2p_order", "params": {"po_id": "PO-1"}},
        )
        assert res_mcp_ok["isError"] is False
        body_ok = json.loads(res_mcp_ok["content"][0]["text"])
        assert body_ok["autonomy"]["outcome"] == "SHADOW_RECORDED"
        assert body_ok["autonomy"]["committed"] is False
        assert "run" not in body_ok

        res_mcp_rej = mcp.call_tool(
            "submit_action",
            {"run_id": "mcp-sh-2", "action": "confirm_p2p_order", "params": {"po_id": "PO-2"}},
        )
        body_rej = json.loads(res_mcp_rej["content"][0]["text"])
        assert body_rej["autonomy"]["outcome"] == "SHADOW_WOULD_REJECT"
        assert body_rej["autonomy"]["error_code"] == "PO_NOT_DRAFT"

        # Zero physical calls and zero ABox / audit mutations in runtime
        assert len(actuate_log) == 0
        assert remote_state["P2pPurchaseOrder"]["PO-1"]["status"] == "draft"
        assert rt.get_object("P2pPurchaseOrder", "PO-1", actor).status == "draft"
        assert rt.list_action_runs() == []

        lines = ledger_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        assert len(controller.ledger) == 4
        report = controller.shadow_report()
        assert report["shadow_total"] == 4
        assert report["shadow_would_approve"] == 2
        assert report["shadow_would_reject"] == 2
        assert report["shadow_would_approve_rate"] == 0.5
        assert report["error_code_distribution"] == {"PO_NOT_DRAFT": 2}
    finally:
        rt.close()


def test_copilot_escalate_fail_fast_and_work_card_creation() -> None:
    rt, remote_state, actuate_log = _seed_coupled_p2p_world(
        [("PO-1", 75000.0, "draft"), ("PO-2", 60000.0, "confirmed")]
    )
    policy = AutonomyPolicy(
        default_mode="COPILOT_ESCALATE",
        default_escalation_action="open_work_card",
        default_escalation_assignee_role="procurement_manager",
    )
    ctrl_with_card = AutonomyRampController(
        policy,
        escalation_payload_builder=_escalation_to_work_card,
    )
    actor = Actor(actor_id="agent-copilot", role="ai_agent", clearance_tags={"*"})

    try:
        # 1) Invalid action fails fast BEFORE creating any WorkCard
        bad = ctrl_with_card.execute(rt, actor, "cop-bad", "confirm_p2p_order", {"po_id": "PO-2"})
        assert bad.outcome == "COPILOT_REJECTED"
        assert bad.valid is False
        assert bad.committed is False
        assert bad.error_code == "PO_NOT_DRAFT"
        assert rt.query_objects("WorkCard", actor) == []
        assert len(actuate_log) == 0

        # 2) Valid action escalates cleanly via open_work_card instead of actuating PO-1
        ok = ctrl_with_card.execute(rt, actor, "cop-ok", "confirm_p2p_order", {"po_id": "PO-1"})
        assert ok.outcome == "ESCALATED_COMMITTED"
        assert ok.valid is True
        assert ok.committed is True
        assert ok.escalation is not None
        assert ok.escalation["escalation_action"] == "open_work_card"
        assert rt.get_object("P2pPurchaseOrder", "PO-1", actor).status == "draft"
        assert remote_state["P2pPurchaseOrder"]["PO-1"]["status"] == "draft"
        assert len(actuate_log) == 0

        cards = rt.query_objects("WorkCard", actor)
        assert len(cards) == 1
        assert cards[0].status == "OPEN"
        assert cards[0].target_object_type == "P2pPurchaseOrder"
        assert cards[0].target_object_id == "PO-1"
        assert cards[0].requested_action == "confirm_p2p_order"
        assert cards[0].assignee_role == "procurement_manager"

        # 3) Unconfigured escalation_action returns ESCALATION_REQUIRED with zero writes
        ctrl_intent = AutonomyRampController(
            AutonomyPolicy(default_mode="COPILOT_ESCALATE"),
            escalation_payload_builder=_escalation_to_work_card,
        )
        intent = ctrl_intent.execute(rt, actor, "cop-intent", "confirm_p2p_order", {"po_id": "PO-1"})
        assert intent.outcome == "ESCALATION_REQUIRED"
        assert intent.committed is False
        assert len(rt.query_objects("WorkCard", actor)) == 1
    finally:
        rt.close()


def test_auto_under_ceiling_commit_vs_escalate_and_missing_metric_fail_closed() -> None:
    rt, remote_state, actuate_log = _seed_coupled_p2p_world(
        [
            ("PO-LOW", 80000.0, "draft"),
            ("PO-HIGH", 150000.0, "draft"),
            ("PO-MISS", 25000.0, "draft"),
        ]
    )
    policy = AutonomyPolicy(
        default_mode="SHADOW",
        default_escalation_action="open_work_card",
        default_escalation_assignee_role="procurement_manager",
        rules=(
            ActionAutonomyRule(
                action="confirm_p2p_order",
                roles=("ai_agent",),
                mode="AUTO_UNDER_CEILING",
                ceiling_field="amount_total",
                max_value=100000.0,
            ),
        ),
    )
    ctrl = AutonomyRampController(policy, escalation_payload_builder=_escalation_to_work_card)
    actor = Actor(actor_id="agent-auto", role="ai_agent", clearance_tags={"*"})

    try:
        # 1) Under ceiling -> auto-committed through coupled SourceBridge
        dec_low = ctrl.execute(rt, actor, "auto-low", "confirm_p2p_order", {"po_id": "PO-LOW"})
        assert dec_low.outcome == "AUTO_COMMITTED"
        assert dec_low.effective_mode == "AUTO_UNDER_CEILING"
        assert dec_low.committed is True
        assert dec_low.evaluated_value == 80000.0
        assert dec_low.run is not None and dec_low.run["status"] == "COMMITTED"
        assert rt.get_object("P2pPurchaseOrder", "PO-LOW", actor).status == "confirmed"
        assert remote_state["P2pPurchaseOrder"]["PO-LOW"]["status"] == "confirmed"
        assert len(actuate_log) == 1
        assert rt.query_objects("WorkCard", actor) == []

        # 2) Above ceiling -> automatic downgrade to COPILOT_ESCALATE opening WorkCard
        dec_high = ctrl.execute(rt, actor, "auto-high", "confirm_p2p_order", {"po_id": "PO-HIGH"})
        assert dec_high.outcome == "ESCALATED_COMMITTED"
        assert dec_high.mode == "AUTO_UNDER_CEILING"
        assert dec_high.effective_mode == "COPILOT_ESCALATE"
        assert dec_high.evaluated_value == 150000.0
        assert dec_high.escalation is not None and dec_high.escalation["reason"] == "ceiling_exceeded"
        assert rt.get_object("P2pPurchaseOrder", "PO-HIGH", actor).status == "draft"
        assert len(actuate_log) == 1

        # 3) Missing ceiling metric -> fail-closed downgrade to COPILOT_ESCALATE
        miss_policy = AutonomyPolicy(
            default_mode="AUTO_UNDER_CEILING",
            default_escalation_action="open_work_card",
            rules=(
                ActionAutonomyRule(
                    action="confirm_p2p_order",
                    mode="AUTO_UNDER_CEILING",
                    ceiling_field="nonexistent_metric",
                    max_value=100000.0,
                ),
            ),
        )
        miss_ctrl = AutonomyRampController(miss_policy, escalation_payload_builder=_escalation_to_work_card)
        dec_miss = miss_ctrl.execute(rt, actor, "auto-miss", "confirm_p2p_order", {"po_id": "PO-MISS"})
        assert dec_miss.outcome == "ESCALATED_COMMITTED"
        assert dec_miss.effective_mode == "COPILOT_ESCALATE"
        assert dec_miss.escalation is not None and dec_miss.escalation["reason"] == "missing_ceiling_metric"
        assert rt.get_object("P2pPurchaseOrder", "PO-MISS", actor).status == "draft"
        assert len(actuate_log) == 1
        assert len(rt.query_objects("WorkCard", actor)) == 2
    finally:
        rt.close()


def test_saga_reconciler_three_branches_fault_injection_in_coupled_runtime() -> None:
    k1 = derive_idempotency_key({"run_id": "r1", "action_type": "a", "params": {"b": 2, "a": 1}, "object_diff": {}})
    k2 = derive_idempotency_key({"action_type": "a", "run_id": "r1", "object_diff": {}, "params": {"a": 1, "b": 2}})
    assert k1 == k2 and len(k1) == 64

    drift_events: list[ReconciliationRecord] = []
    rt_holder: dict[str, OntologyRuntime] = {}
    actor = Actor(actor_id="agent-reconciler", role="ai_agent", clearance_tags={"*"})

    def fault_injecting_actuate(
        move: dict[str, Any],
        remote: dict[str, dict[str, dict[str, Any]]],
        log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        log.append(copy.deepcopy(move))
        po_id = str((move.get("params") or {}).get("po_id") or "")
        attempt = int(move.get("attempt") or 1)

        if po_id == "PO-1":
            # Branch 1: remote persisted BEFORE ACK timed out -> must NEVER call actuate a second time!
            remote["P2pPurchaseOrder"]["PO-1"]["status"] = "confirmed"
            raise TimeoutError("simulated timeout after remote commit")

        if po_id == "PO-2":
            # Branch 2: first packet dropped before reaching device; second attempt succeeds
            if attempt == 1:
                raise ConnectionError("simulated network drop before execution")
            remote["P2pPurchaseOrder"]["PO-2"]["status"] = "confirmed"
            return {"ok": True}

        if po_id == "PO-3":
            # Branch 3: timeout + external drift into unknown third state 'cancelled'
            remote["P2pPurchaseOrder"]["PO-3"]["status"] = "cancelled"
            raise TimeoutError("simulated timeout + third-state split brain")

        remote["P2pPurchaseOrder"][po_id]["status"] = "confirmed"
        return {"ok": True}

    reconciler_holder: list[SagaReconciler] = []

    def on_drift_open_card(rec: ReconciliationRecord) -> None:
        drift_events.append(rec)
        rt_holder["rt"].submit_action(
            f"{rec.run_id}:drift-card",
            "open_work_card",
            {
                "card_id": f"WC-DRIFT-{rec.run_id}",
                "card_kind": "HUMAN_TASK",
                "target_object_type": "P2pPurchaseOrder",
                "target_object_id": "PO-3",
                "requested_action": rec.action_type,
                "assignee_role": "procurement_manager",
                "external_ref": f"reconciliation ambiguous key={rec.idempotency_key}",
            },
            actor,
        )

    def wrap_bridge(raw: SourceBridge) -> SourceBridge:
        rec = IdempotentSourceBridge(raw, std_p2p_spec, max_retries=2, on_drift=on_drift_open_card)
        reconciler_holder.append(rec)
        return rec.as_source_bridge()

    rt, remote_state, actuate_log = _seed_coupled_p2p_world(
        [("PO-1", 40000.0, "draft"), ("PO-2", 45000.0, "draft"), ("PO-3", 50000.0, "draft")],
        actuate_fn=fault_injecting_actuate,
        bridge_wrapper=wrap_bridge,
    )
    rt_holder["rt"] = rt
    reconciler = reconciler_holder[0]

    try:
        # Branch 1: ACK lost after remote write -> 1 actuation only, atomic COMMITTED
        run1 = rt.submit_action("run-b1", "confirm_p2p_order", {"po_id": "PO-1"}, actor)
        assert run1["status"] == "COMMITTED"
        assert rt.get_object("P2pPurchaseOrder", "PO-1", actor).status == "confirmed"
        po1_calls = [m for m in actuate_log if m["params"]["po_id"] == "PO-1"]
        assert len(po1_calls) == 1
        assert reconciler.records[-1].outcome == "RECONCILED_REMOTE_COMMITTED"
        assert reconciler.records[-1].actuate_calls == 1
        assert reconciler.records[-1].observe_calls == 1

        # Branch 2: packet never reached device -> safe retry with same idempotency_key -> COMMITTED
        run2 = rt.submit_action("run-b2", "confirm_p2p_order", {"po_id": "PO-2"}, actor)
        assert run2["status"] == "COMMITTED"
        assert rt.get_object("P2pPurchaseOrder", "PO-2", actor).status == "confirmed"
        po2_calls = [m for m in actuate_log if m["params"]["po_id"] == "PO-2"]
        assert len(po2_calls) == 2
        assert po2_calls[0]["idempotency_key"] == po2_calls[1]["idempotency_key"]
        assert po2_calls[0]["attempt"] == 1 and po2_calls[1]["attempt"] == 2
        assert reconciler.records[-1].outcome == "RETRIED_AND_COMMITTED"

        # Branch 3: drifted unknown third state -> NO retry, REJECTED audit, ABox clean, on_drift WorkCard
        with pytest.raises(ReconciliationAmbiguousError) as exc_info:
            rt.submit_action("run-b3", "confirm_p2p_order", {"po_id": "PO-3"}, actor)
        assert exc_info.value.code == RECONCILIATION_STATE_AMBIGUOUS
        po3_calls = [m for m in actuate_log if m["params"]["po_id"] == "PO-3"]
        assert len(po3_calls) == 1
        assert rt.get_object("P2pPurchaseOrder", "PO-3", actor).status == "draft"
        assert remote_state["P2pPurchaseOrder"]["PO-3"]["status"] == "cancelled"

        run3 = rt.get_action_run("run-b3")
        assert run3 is not None and run3["status"] == "REJECTED"
        assert run3["error_code"] == RECONCILIATION_STATE_AMBIGUOUS
        assert len(drift_events) == 1
        assert drift_events[0].outcome == "DRIFTED_UNKNOWN_STATE"

        drift_cards = rt.query_objects("WorkCard", actor)
        assert len(drift_cards) == 1
        assert drift_cards[0].card_id == "WC-DRIFT-run-b3"
        assert drift_cards[0].status == "OPEN"
        assert len(reconciler.export_records()) == 3
    finally:
        rt.close()
