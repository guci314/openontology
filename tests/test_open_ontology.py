"""Acceptance suite for the OpenOntology kernel (TK-ONTO-002 · v3).

Covers the v2 core mechanisms:

1. physical write gate (objects + links + overlay)
2. append-only action runs
3. content-hash state placement idempotency
4. RBAC (action roles / approver roles)
5. row level security (get / query / linked traversal)
6. sandbox read-your-own-writes
7. link overlay + diff compilation
8. diff inversion
9. simulation is zero-write
10. atomic submit + optimistic locking + business audit
11. approval intercept + concurrency conflict
12. time-machine revert (update + create cases)

And the v3 Operational Ontology additions:

13. ``__owned__`` field-level sovereignty + ``ontology_overlay`` survival
14. ``load_snapshot`` reindex and orphaned-overlay refusal
15. authority line checks (MIXED / UNDECLARED / MISDECLARED)
16. refused attempts persisted as ``status='REJECTED'`` audit rows
17. ``ObjectSet`` union/intersect/subtract + pivot + aggregate

Plus: LLM tool export, AIP executor, deterministic evals, registry,
and a dedicated zero-IT-concept purity assertion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import BaseModel

from examples import cold_chain as cc
from open_ontology import (
    AIPAgentExecutor,
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    InvariantError,
    ObjectDiff,
    ObjectNotFoundError,
    ObjectSet,
    OntologyRuntime,
    OntologySpec,
    ObjectType,
    PermissionDeniedError,
    ReindexConflictError,
    export_llm_tools,
    run_eval_suite,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def demo():
    instance = cc.build_demo()
    yield instance
    instance.runtime.close()


def low_cost(**overrides):
    params = {
        "shipment_id": "SHP-001",
        "replacement_warehouse_id": "WH-002",
        "replacement_truck_id": "TRK-002",
        "extra_cost": 800,
        "reason": "truck breakdown",
    }
    params.update(overrides)
    return params


def high_cost(**overrides):
    return low_cost(extra_cost=2400, **overrides)


def shipment_version(runtime):
    return runtime.list_objects("Shipment")[0]["version"]


def carriers(runtime, actor=cc.OPS_MANAGER):
    shipment = runtime.get_object(cc.Shipment, "SHP-001", actor)
    return sorted(
        t.truck_id for t in runtime.get_linked_to(shipment, cc.CARRIES, cc.Truck, actor)
    )


# ---------------------------------------------------------------------------
# 1. Physical write gate
# ---------------------------------------------------------------------------
def test_write_gate_blocks_direct_object_and_link_writes(demo):
    conn = demo.runtime.connection

    assert conn.execute(
        "SELECT is_open FROM ontology_write_gate WHERE id = 1"
    ).fetchone()["is_open"] == 0

    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute(
            """
            INSERT INTO ontology_objects
                (object_type, object_id, title, properties_json,
                 content_hash, updated_at)
            VALUES ('Truck', 'TRK-X', 'x', '{}', 'h', 'now')
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("UPDATE ontology_objects SET title = 'hacked'")

    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("DELETE FROM ontology_objects")

    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute(
            """
            INSERT INTO ontology_links
                (link_type, source_type, source_id, target_type, target_id)
            VALUES ('carries', 'Truck', 'TRK-001', 'Shipment', 'SHP-001')
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("DELETE FROM ontology_links")

    # gate is still closed and the runtime still works
    assert conn.execute(
        "SELECT is_open FROM ontology_write_gate WHERE id = 1"
    ).fetchone()["is_open"] == 0
    result = demo.runtime.ingest_object(
        cc.Truck(
            truck_id="TRK-777", plate="京A-7777", region="north", eta_hours=1.0
        )
    )
    assert result["status"] == "CREATED"
    assert conn.execute(
        "SELECT is_open FROM ontology_write_gate WHERE id = 1"
    ).fetchone()["is_open"] == 0


def test_action_runs_are_append_only(demo):
    demo.runtime.submit_action("RUN-A", "reroute_shipment", low_cost(), cc.DISPATCHER)
    conn = demo.runtime.connection

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ontology_action_runs WHERE run_id = 'RUN-A'")

    # status transitions are allowed, deletion never is
    assert demo.runtime.get_action_run("RUN-A")["status"] == "COMMITTED"


# ---------------------------------------------------------------------------
# 2. State placement: content hash idempotency + versioning
# ---------------------------------------------------------------------------
def test_put_object_hash_is_idempotent_and_skips_version_bump(demo):
    runtime = demo.runtime
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    row = runtime.list_objects("Shipment")[0]
    assert row["version"] == 1

    expected_hash = hashlib.sha256(
        json.dumps(
            shipment.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert row["content_hash"] == expected_hash

    # ingest_object is a compatibility alias of put_object
    skipped = runtime.ingest_object(shipment)
    assert skipped["status"] == "SKIPPED"
    assert skipped["version"] == 1
    assert shipment_version(runtime) == 1

    # no external-system parameter is accepted by either entry point
    with pytest.raises(TypeError):
        runtime.ingest_object(shipment, lineage={})
    with pytest.raises(TypeError):
        runtime.put_object(shipment, lineage={})

    changed = shipment.model_copy(update={"status": "delayed"})
    updated = runtime.put_object(changed)
    assert updated["status"] == "UPDATED"
    assert updated["version"] == 2
    assert updated["content_hash"] != row["content_hash"]
    assert runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER).status == "delayed"


# ---------------------------------------------------------------------------
# 3. RBAC
# ---------------------------------------------------------------------------
def test_rbac_rejects_wrong_roles(demo):
    runtime = demo.runtime
    auditor = cc.AUDITOR_NORTH

    with pytest.raises(PermissionDeniedError):
        runtime.simulate_action("reroute_shipment", low_cost(), auditor)
    with pytest.raises(PermissionDeniedError):
        runtime.submit_action("RUN-RBAC", "reroute_shipment", low_cost(), auditor)

    runtime.submit_action("RUN-P", "reroute_shipment", high_cost(), cc.AI_AGENT)
    with pytest.raises(PermissionDeniedError):
        runtime.approve_action("RUN-P", cc.DISPATCHER)
    with pytest.raises(PermissionDeniedError):
        runtime.revert_action("RUN-P", "RUN-P-REV", auditor)

    assert runtime.get_action_run("RUN-P")["status"] == "PENDING_APPROVAL"


# ---------------------------------------------------------------------------
# 4. Row level security
# ---------------------------------------------------------------------------
def test_row_level_security_filters_get_query_and_links(demo):
    runtime = demo.runtime
    south = Actor(actor_id="south-1", role="auditor", clearance_tags={"region:south"})
    north = Actor(actor_id="north-1", role="auditor", clearance_tags={"region:north"})

    with pytest.raises(PermissionDeniedError):
        runtime.get_object(cc.Shipment, "SHP-001", south)

    assert runtime.query_objects(cc.Shipment, south) == []
    assert len(runtime.query_objects(cc.Shipment, north)) == 1

    warehouses = runtime.query_objects(cc.Warehouse, south)
    assert [w.warehouse_id for w in warehouses] == ["WH-003"]

    # a south truck linked to a north shipment is invisible to a north actor
    south_truck = cc.Truck(
        truck_id="TRK-009",
        plate="沪A-9009",
        region="south",
        cold_chain_certified=True,
        status="available",
        eta_hours=1.0,
    )
    runtime.ingest_object(south_truck)
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    runtime.link_objects(south_truck, cc.CARRIES, shipment)

    north_links = runtime.get_linked_to(shipment, cc.CARRIES, cc.Truck, north)
    assert [t.truck_id for t in north_links] == ["TRK-001"]

    wildcard = Actor(actor_id="root", role="admin", clearance_tags={"*"})
    all_links = runtime.get_linked_to(shipment, cc.CARRIES, cc.Truck, wildcard)
    assert [t.truck_id for t in all_links] == ["TRK-001", "TRK-009"]

    assert len(
        runtime.query_objects(cc.Warehouse, north, filters={"region": "north"})
    ) == 2
    assert runtime.query_objects(cc.Warehouse, north, filters={"region": "south"}) == []


# ---------------------------------------------------------------------------
# 5. Simulation is zero-write + approval decision
# ---------------------------------------------------------------------------
def test_simulation_is_zero_write_and_requires_approval(demo):
    runtime = demo.runtime
    before_count = runtime.count_objects()
    before_version = shipment_version(runtime)

    low = runtime.simulate_action("reroute_shipment", low_cost(), cc.DISPATCHER)
    assert low.valid is True
    assert low.requires_approval is False
    assert low.error is None
    assert not low.diff.is_empty()
    assert low.read_versions["Shipment:SHP-001"] == 1

    ai = runtime.simulate_action("reroute_shipment", high_cost(), cc.AI_AGENT)
    assert ai.valid is True
    assert ai.requires_approval is True

    # dispatcher spending 2400 is *not* subject to the ai_agent rule
    dispatcher_high = runtime.simulate_action(
        "reroute_shipment", high_cost(), cc.DISPATCHER
    )
    assert dispatcher_high.requires_approval is False

    assert runtime.count_objects() == before_count
    assert shipment_version(runtime) == before_version
    assert runtime.list_action_runs() == []


# ---------------------------------------------------------------------------
# 6. Diff inversion
# ---------------------------------------------------------------------------
def test_diff_inversion_swaps_before_after_and_links(demo):
    runtime = demo.runtime
    sim = runtime.simulate_action("reroute_shipment", high_cost(), cc.AI_AGENT)
    diff = sim.diff

    shipment_change = next(
        c for c in diff.object_changes if c.object_id == "SHP-001"
    )
    assert shipment_change.kind == "update"
    assert shipment_change.before["status"] == "in_transit"
    assert shipment_change.after["status"] == "rerouted"

    inverse = diff.inverted()
    inverse_shipment = next(
        c for c in inverse.object_changes if c.object_id == "SHP-001"
    )
    assert inverse_shipment.before == shipment_change.after
    assert inverse_shipment.after == shipment_change.before
    assert inverse_shipment.before_version is None  # re-read current at apply time

    kinds = {
        (c.link_type, c.source_id, c.target_id): c.kind
        for c in inverse.link_changes
    }
    assert kinds[("carries", "TRK-001", "SHP-001")] == "create"
    assert kinds[("carries", "TRK-002", "SHP-001")] == "delete"

    # JSON roundtrip keeps the patch lossless
    assert ObjectDiff.from_dict(diff.to_dict()).to_dict() == diff.to_dict()


# ---------------------------------------------------------------------------
# 7. Atomic submit
# ---------------------------------------------------------------------------
def test_submit_is_atomic_and_audited(demo):
    runtime = demo.runtime
    run = runtime.submit_action(
        "RUN-0001", "reroute_shipment", low_cost(), cc.DISPATCHER
    )
    assert run["status"] == "COMMITTED"
    assert run["approved_by"] is None
    assert run["actor_role"] == "dispatcher"
    assert run["read_versions"]["Shipment:SHP-001"] == 1

    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.status == "rerouted"
    assert shipment.assigned_truck_id == "TRK-002"
    assert shipment.current_warehouse_id == "WH-002"
    assert shipment_version(runtime) == 2

    warehouses = runtime.get_linked(
        shipment, cc.SHIPS_FROM, cc.Warehouse, cc.OPS_MANAGER
    )
    assert [w.warehouse_id for w in warehouses] == ["WH-002"]
    assert carriers(runtime) == ["TRK-002"]

    warehouse = runtime.get_object(cc.Warehouse, "WH-002", cc.OPS_MANAGER)
    assert warehouse.batch_stock["BATCH-001"] == 180
    assert runtime.get_object(cc.Truck, "TRK-001", cc.OPS_MANAGER).status == "unavailable"
    assert runtime.get_object(cc.Truck, "TRK-002", cc.OPS_MANAGER).status == "assigned"

    # the only observable output is the pure-business action ledger
    runs = runtime.list_action_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "RUN-0001"
    assert runs[0]["status"] == "COMMITTED"
    assert runs[0]["object_diff"]["object_changes"]
    assert runs[0]["object_diff"]["link_changes"]


def test_submit_is_idempotent(demo):
    runtime = demo.runtime
    first = runtime.submit_action(
        "RUN-IDEM", "reroute_shipment", low_cost(), cc.DISPATCHER
    )
    second = runtime.submit_action(
        "RUN-IDEM", "reroute_shipment", high_cost(), cc.AI_AGENT
    )

    assert second == first
    assert shipment_version(runtime) == 2
    assert len(runtime.list_action_runs()) == 1
    assert carriers(runtime) == ["TRK-002"]


# ---------------------------------------------------------------------------
# 8. Approval intercept + concurrency conflict
# ---------------------------------------------------------------------------
def test_approval_flow_pending_then_manager_commits(demo):
    runtime = demo.runtime

    pending = runtime.submit_action(
        "RUN-APPR", "reroute_shipment", high_cost(), cc.AI_AGENT
    )
    assert pending["status"] == "PENDING_APPROVAL"
    assert pending["read_versions"]["Shipment:SHP-001"] == 1
    assert shipment_version(runtime) == 1
    assert carriers(runtime) == ["TRK-001"]
    assert len(runtime.list_action_runs()) == 1
    assert (
        runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER).status
        == "in_transit"
    )

    approved = runtime.approve_action("RUN-APPR", cc.OPS_MANAGER)
    assert approved["status"] == "COMMITTED"
    assert approved["approved_by"] == "manager-01"
    assert shipment_version(runtime) == 2
    assert carriers(runtime) == ["TRK-002"]

    audited = runtime.get_action_run("RUN-APPR")
    assert audited["approved_by"] == "manager-01"
    assert audited["object_diff"]["object_changes"]

    with pytest.raises(GovernanceViolationError):
        runtime.approve_action("RUN-APPR", cc.OPS_MANAGER)


def test_approval_detects_concurrency_conflict(demo):
    runtime = demo.runtime
    runtime.submit_action("RUN-CONF", "reroute_shipment", high_cost(), cc.AI_AGENT)

    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    runtime.ingest_object(shipment.model_copy(update={"status": "delayed"}))
    assert shipment_version(runtime) == 2

    with pytest.raises(ConcurrencyConflictError):
        runtime.approve_action("RUN-CONF", cc.OPS_MANAGER)

    run = runtime.get_action_run("RUN-CONF")
    assert run["status"] == "PENDING_APPROVAL"
    current = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert current.status == "delayed"
    assert shipment_version(runtime) == 2


# ---------------------------------------------------------------------------
# 9. Time-machine revert
# ---------------------------------------------------------------------------
def test_revert_restores_graph_and_records_business_audit(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "RUN-REV", "reroute_shipment", low_cost(), cc.DISPATCHER
    )
    assert shipment_version(runtime) == 2

    revert = runtime.revert_action("RUN-REV", "RUN-REV-BACK", cc.OPS_MANAGER)
    assert revert["status"] == "COMMITTED"
    assert revert["action_type"] == "reroute_shipment"

    original = runtime.get_action_run("RUN-REV")
    assert original["status"] == "REVERTED"
    assert original["reverted_by_run_id"] == "RUN-REV-BACK"

    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.status == "in_transit"
    assert shipment.assigned_truck_id == "TRK-001"
    assert shipment.current_warehouse_id == "WH-001"
    assert shipment_version(runtime) == 3
    assert carriers(runtime) == ["TRK-001"]
    assert runtime.get_object(cc.Warehouse, "WH-002", cc.OPS_MANAGER).batch_stock[
        "BATCH-001"
    ] == 300
    assert runtime.get_object(cc.Truck, "TRK-001", cc.OPS_MANAGER).status == "assigned"
    assert runtime.get_object(cc.Truck, "TRK-002", cc.OPS_MANAGER).status == "available"

    runs = {row["run_id"]: row for row in runtime.list_action_runs()}
    assert runs["RUN-REV"]["status"] == "REVERTED"
    assert runs["RUN-REV-BACK"]["status"] == "COMMITTED"
    assert runs["RUN-REV-BACK"]["object_diff"]["link_changes"]

    with pytest.raises(GovernanceViolationError):
        runtime.revert_action("RUN-REV", "RUN-REV-AGAIN", cc.OPS_MANAGER)


# ---------------------------------------------------------------------------
# 10. Business invariants
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("params", "expected_code", "needle"),
    [
        (low_cost(replacement_truck_id="TRK-003"), "TRUCK_NOT_COLD_CHAIN", "cold-chain"),
        (low_cost(replacement_truck_id="TRK-004"), "ETA_TOO_LONG", "ETA"),
        (
            low_cost(replacement_warehouse_id="WH-003"),
            "WAREHOUSE_NOT_COLD_CHAIN",
            "cold-chain",
        ),
        (low_cost(extra_cost=-1), "INVALID_COST", "extra_cost"),
        (low_cost(replacement_truck_id="TRK-001"), "SAME_TRUCK", "differ"),
    ],
)
def test_invariants_return_invalid_simulation(demo, params, expected_code, needle):
    # wildcard actor: WH-003 is region:south and must be visible to test its
    # cold-chain certification invariant (north dispatcher would hit RLS first)
    sim = demo.runtime.simulate_action("reroute_shipment", params, cc.AI_AGENT)
    assert sim.valid is False
    assert sim.error_code == expected_code
    assert needle in (sim.error or "")
    assert sim.diff.is_empty()
    assert shipment_version(demo.runtime) == 1


def test_insufficient_stock_and_submit_invariant_aborts_cleanly(demo):
    runtime = demo.runtime
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    runtime.ingest_object(shipment.model_copy(update={"quantity": 400}))

    sim = runtime.simulate_action(
        "reroute_shipment", low_cost(), cc.DISPATCHER
    )
    assert sim.valid is False
    assert sim.error_code == "INSUFFICIENT_STOCK"
    assert "need 400" in sim.error

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "RUN-BAD", "reroute_shipment", low_cost(), cc.DISPATCHER
        )
    assert excinfo.value.code == "INSUFFICIENT_STOCK"
    rejected = runtime.get_action_run("RUN-BAD")
    assert rejected is not None
    assert rejected["status"] == "REJECTED"
    assert rejected["error_code"] == "INSUFFICIENT_STOCK"
    assert "need 400" in rejected["error_message"]
    assert rejected["object_diff"] == {"object_changes": [], "link_changes": []}
    assert runtime.count_objects("Shipment") == 1
    assert shipment_version(runtime) == 2  # only the ingest bumped it


# ---------------------------------------------------------------------------
# 11. LLM tool export + AIP executor + evals
# ---------------------------------------------------------------------------
def test_export_llm_tools_schema(demo):
    tools = export_llm_tools(cc.spec)
    by_name = {tool["function"]["name"]: tool for tool in tools}

    assert set(by_name) == {
        "recommend_reroute",
        "reroute_shipment",
        "annotate_shipment",
    }

    function_tool = by_name["recommend_reroute"]
    assert function_tool["type"] == "function"
    assert function_tool["x-ontology-kind"] == "function"
    assert "shipment_id" in function_tool["function"]["parameters"]["properties"]

    annotate_tool = by_name["annotate_shipment"]
    assert annotate_tool["x-ontology-kind"] == "action"
    assert "note" in annotate_tool["function"]["parameters"]["properties"]

    action_tool = by_name["reroute_shipment"]
    assert action_tool["x-ontology-kind"] == "action"
    assert action_tool["x-allowed-roles"] == [
        "dispatcher",
        "ai_agent",
        "ops_manager",
    ]
    assert action_tool["x-approver-roles"] == ["ops_manager"]
    assert set(action_tool) == {
        "type",
        "function",
        "x-ontology-kind",
        "x-allowed-roles",
        "x-approver-roles",
    }
    action_schema = action_tool["function"]["parameters"]
    assert action_schema["type"] == "object"
    assert {"replacement_warehouse_id", "extra_cost"} <= set(
        action_schema["properties"]
    )
    assert "replacement_truck_id" in action_schema["required"]


def test_aip_executor_dispatch(demo):
    runtime = demo.runtime
    executor = AIPAgentExecutor(runtime, cc.AI_AGENT)

    queried = executor.dispatch("query_objects", {"object_type": "Shipment"})
    assert queried["count"] == 1
    assert queried["objects"][0]["shipment_id"] == "SHP-001"

    got = executor.dispatch(
        "get_object", {"object_type": "Truck", "object_id": "TRK-002"}
    )
    assert got["object"]["status"] == "available"

    recommendation = executor.dispatch(
        "call_function",
        {"name": "recommend_reroute", "params": {"shipment_id": "SHP-001"}},
    )
    assert recommendation["ok"] is True
    assert recommendation["result"]["candidates"]

    simulated = executor.dispatch(
        "simulate_action",
        {"action": "reroute_shipment", "params": high_cost()},
    )
    assert simulated["valid"] is True
    assert simulated["requires_approval"] is True

    submitted = executor.dispatch(
        "submit_action",
        {"run_id": "RUN-AIP", "action": "reroute_shipment", "params": low_cost()},
    )
    assert submitted["run"]["status"] == "COMMITTED"

    runs = executor.dispatch("list_action_runs", {})
    assert [run["run_id"] for run in runs["runs"]] == ["RUN-AIP"]

    # OpenAI tool-call shape, with JSON-string arguments
    tool_call = executor.dispatch_tool_call(
        {
            "function": {
                "name": "get_object",
                "arguments": json.dumps(
                    {"object_type": "Shipment", "object_id": "SHP-001"}
                ),
            }
        }
    )
    assert tool_call["object"]["status"] == "rerouted"

    with pytest.raises(ValueError):
        executor.dispatch("drop_database", {})


def test_run_eval_suite_passes_and_detects_wrong_expectation(demo):
    runtime = demo.runtime
    before = runtime.count_objects()

    cases = [
        {
            "name": "ok",
            "action": "reroute_shipment",
            "params": low_cost(),
            "expect": {
                "valid": True,
                "requires_approval": False,
                "object_changes": [
                    {
                        "object_type": "Shipment",
                        "object_id": "SHP-001",
                        "kind": "update",
                        "after": {"status": "rerouted"},
                    }
                ],
                "link_changes": [
                    {
                        "link_type": "carries",
                        "source_id": "TRK-002",
                        "target_id": "SHP-001",
                        "kind": "create",
                    }
                ],
            },
        },
        {
            "name": "ai-approval",
            "actor": cc.AI_AGENT,
            "action": "reroute_shipment",
            "params": high_cost(),
            "expect": {"valid": True, "requires_approval": True},
        },
        {
            "name": "eta",
            "action": "reroute_shipment",
            "params": low_cost(replacement_truck_id="TRK-004"),
            "expect": {"valid": False, "error_contains": "ETA"},
        },
    ]
    report = run_eval_suite(runtime, cc.DISPATCHER, cases)
    assert report.total == 3
    assert report.passed == 3
    assert report.failed == 0
    assert report.all_passed is True

    bad_cases = [
        {
            "name": "wrong-expectation",
            "action": "reroute_shipment",
            "params": low_cost(),
            "expect": {"valid": False},
        }
    ]
    bad_report = run_eval_suite(runtime, cc.DISPATCHER, bad_cases)
    assert bad_report.failed == 1
    assert any("valid" in failure for failure in bad_report.results[0].failures)

    assert runtime.count_objects() == before
    assert runtime.list_action_runs() == []


# ---------------------------------------------------------------------------
# 12. Registry persistence
# ---------------------------------------------------------------------------
def test_spec_registry_is_persisted(demo):
    runtime = demo.runtime
    rows = runtime.connection.execute(
        "SELECT kind, COUNT(*) AS n FROM ontology_registry GROUP BY kind"
    ).fetchall()
    counts = {row["kind"]: row["n"] for row in rows}
    assert counts == {
        "object_type": 4,
        "link_type": 3,
        "function": 1,
        "action": 2,
    }

    action_row = runtime.connection.execute(
        "SELECT spec_json FROM ontology_registry WHERE kind = 'action' AND name = ?",
        ("reroute_shipment",),
    ).fetchone()
    spec_json = json.loads(action_row["spec_json"])
    assert spec_json["approver_roles"] == ["ops_manager"]
    assert spec_json["affects_source"] is True
    assert "properties" in spec_json["params_schema"]

    annotate_row = runtime.connection.execute(
        "SELECT spec_json FROM ontology_registry WHERE kind = 'action' AND name = ?",
        ("annotate_shipment",),
    ).fetchone()
    annotate_spec = json.loads(annotate_row["spec_json"])
    assert annotate_spec["affects_source"] is False


# ---------------------------------------------------------------------------
# 13. Sandbox: read-your-own-writes + link overlay (mini spec)
# ---------------------------------------------------------------------------
class Note(ObjectType):
    __primary_key__ = "note_id"
    __title__ = "text"
    __security_tag_fields__ = ("region",)

    note_id: str
    text: str = ""
    region: str = "north"


class Box(ObjectType):
    __primary_key__ = "box_id"
    __title__ = "label"
    __security_tag_fields__ = ("region",)

    box_id: str
    label: str
    region: str = "north"


class NoteParams(BaseModel):
    note_id: str


class MakeNoteParams(BaseModel):
    box_id: str


mini_spec = OntologySpec()
mini_spec.object_type(Note)
mini_spec.object_type(Box)
mini_spec.link_type("in_box", "Note", "Box")
EDITED = Actor(actor_id="editor-01", role="editor", clearance_tags={"*"})
OBSERVED: list[tuple] = []


@mini_spec.action("touch_note", NoteParams, allowed_roles=("editor",))
def touch_note(ctx, params: NoteParams) -> None:
    note = ctx.require(Note, params.note_id)
    OBSERVED.append(("initial", note.text))

    ctx.modify(note, text=note.text + "-a")
    again = ctx.require(Note, params.note_id)
    OBSERVED.append(("ryow", again.text))

    boxes_before = [b.box_id for b in ctx.query_linked(again, "in_box", Box)]
    old_box = ctx.require(Box, boxes_before[0])
    new_box = ctx.require(Box, "B2")
    ctx.unlink(again, "in_box", old_box)
    ctx.link(again, "in_box", new_box)
    boxes_after = [b.box_id for b in ctx.query_linked(again, "in_box", Box)]
    OBSERVED.append(("links", boxes_before, boxes_after))

    ctx.modify(again, text=again.text + "-b")


@mini_spec.action("make_note", MakeNoteParams, allowed_roles=("editor",))
def make_note(ctx, params: MakeNoteParams) -> None:
    note = Note(note_id="N2", text="created")
    ctx.create(note)
    box = ctx.require(Box, params.box_id)
    ctx.link(note, "in_box", box)


@pytest.fixture()
def mini():
    runtime = OntologyRuntime(mini_spec)
    runtime.ingest_object(Note(note_id="N1", text="hello"))
    runtime.ingest_object(Box(box_id="B1", label="one"))
    runtime.ingest_object(Box(box_id="B2", label="two"))
    note = runtime.get_object(Note, "N1", EDITED)
    box = runtime.get_object(Box, "B1", EDITED)
    runtime.link_objects(note, "in_box", box)
    yield runtime
    runtime.close()


def test_edit_batch_read_your_own_writes_and_link_overlay(mini):
    OBSERVED.clear()

    sim = mini.simulate_action("touch_note", {"note_id": "N1"}, EDITED)
    assert sim.valid is True

    assert OBSERVED[0] == ("initial", "hello")
    assert OBSERVED[1] == ("ryow", "hello-a")
    assert OBSERVED[2] == ("links", ["B1"], ["B2"])

    note_change = next(c for c in sim.diff.object_changes if c.object_id == "N1")
    assert note_change.kind == "update"
    assert note_change.after["text"] == "hello-a-b"
    link_kinds = {
        (c.source_id, c.target_id): c.kind for c in sim.diff.link_changes
    }
    assert link_kinds == {("N1", "B1"): "delete", ("N1", "B2"): "create"}
    assert mini.count_objects("Note") == 1

    mini.submit_action("RUN-TOUCH", "touch_note", {"note_id": "N1"}, EDITED)
    note = mini.get_object(Note, "N1", EDITED)
    assert note.text == "hello-a-b"
    assert [
        b.box_id for b in mini.get_linked(note, "in_box", Box, EDITED)
    ] == ["B2"]


def test_revert_of_create_removes_object_and_links(mini):
    mini.submit_action("RUN-MAKE", "make_note", {"box_id": "B1"}, EDITED)
    created = mini.get_object(Note, "N2", EDITED)
    assert created.text == "created"
    assert mini.link_exists("in_box", "Note", "N2", "Box", "B1")

    mini.revert_action("RUN-MAKE", "RUN-MAKE-BACK", EDITED)

    with pytest.raises(ObjectNotFoundError):
        mini.get_object(Note, "N2", EDITED)
    assert not mini.link_exists("in_box", "Note", "N2", "Box", "B1")
    assert mini.count_objects("Note") == 1
    assert mini.get_action_run("RUN-MAKE")["status"] == "REVERTED"


# ---------------------------------------------------------------------------
# 15. v3 field-level ownership, authority boundary and refusal audit
# ---------------------------------------------------------------------------
class Device(ObjectType):
    __primary_key__ = "device_id"
    __title__ = "name"
    __owned__ = {"note": None}

    device_id: str
    name: str
    status: str = "ok"
    note: str | None = None


class CaseFile(ObjectType):
    __primary_key__ = "case_id"
    __owned__ = True

    case_id: str
    title: str


class DeviceParams(BaseModel):
    device_id: str
    status: str | None = None
    note: str | None = None


class NoteParams(BaseModel):
    device_id: str
    note: str | None = None


class SplitParams(BaseModel):
    left_id: str = "D1"
    right_id: str = "D2"


authority_spec = OntologySpec()
authority_spec.object_type(Device)
authority_spec.object_type(CaseFile)
OPS = Actor(actor_id="ops-1", role="ops", clearance_tags={"*"})


@authority_spec.action(
    "touch_both", DeviceParams, allowed_roles=("ops",), affects_source=True
)
def touch_both(ctx, params: DeviceParams) -> None:
    device = ctx.require(Device, params.device_id)
    ctx.modify(device, status=params.status or "changed", note=params.note or "note")


@authority_spec.action(
    "write_source_only", DeviceParams, allowed_roles=("ops",), affects_source=False
)
def write_source_only(ctx, params: DeviceParams) -> None:
    device = ctx.require(Device, params.device_id)
    ctx.modify(device, status="changed")


@authority_spec.action(
    "write_owned_only", NoteParams, allowed_roles=("ops",), affects_source=True
)
def write_owned_only(ctx, params: NoteParams) -> None:
    device = ctx.require(Device, params.device_id)
    ctx.modify(device, note=params.note or "note")


@authority_spec.action(
    "valid_owned", NoteParams, allowed_roles=("ops",), affects_source=False
)
def valid_owned(ctx, params: NoteParams) -> None:
    device = ctx.require(Device, params.device_id)
    ctx.modify(device, note=params.note or "note")


@authority_spec.action("valid_source", DeviceParams, allowed_roles=("ops",))
def valid_source(ctx, params: DeviceParams) -> None:
    device = ctx.require(Device, params.device_id)
    ctx.modify(device, status=params.status or "changed")


@authority_spec.action(
    "split_authority", SplitParams, allowed_roles=("ops",), affects_source=True
)
def split_authority(ctx, params: SplitParams) -> None:
    left = ctx.require(Device, params.left_id)
    right = ctx.require(Device, params.right_id)
    ctx.modify(left, status="changed")
    ctx.modify(right, note="note")


@pytest.fixture()
def authority_runtime():
    runtime = OntologyRuntime(authority_spec)
    runtime.put_object(Device(device_id="D1", name="pump", status="ok"))
    runtime.put_object(Device(device_id="D2", name="valve", status="ok"))
    runtime.put_object(CaseFile(case_id="C1", title="owned case"))
    yield runtime
    runtime.close()


def _device_state(runtime, device_id="D1"):
    device = runtime.get_object(Device, device_id, OPS)
    return (device.status, device.note)


def test_annotate_only_owned_field_creates_overlay(demo):
    runtime = demo.runtime
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    source_before = shipment.model_dump(mode="json")
    source_before.pop("dispatcher_note")

    sim = runtime.simulate_action(
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "priority"},
        cc.DISPATCHER,
    )
    assert sim.valid is True
    assert sim.diff.object_changes[0].after["dispatcher_note"] == "priority"

    run = runtime.submit_action(
        "RUN-NOTE",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "priority"},
        cc.DISPATCHER,
    )
    assert run["status"] == "COMMITTED"

    overlay = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Shipment' AND object_id = 'SHP-001'"
    ).fetchone()
    assert json.loads(overlay["patch_json"]) == {"dispatcher_note": "priority"}

    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.dispatcher_note == "priority"
    # all source-backed fields remain untouched
    after = shipment.model_dump(mode="json")
    after.pop("dispatcher_note")
    assert after == source_before


def test_reset_owned_field_to_default_removes_overlay(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "RUN-NOTE-1",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "temporary"},
        cc.DISPATCHER,
    )
    assert runtime.connection.execute(
        "SELECT 1 FROM ontology_overlay WHERE object_type = 'Shipment'"
    ).fetchone()

    runtime.submit_action(
        "RUN-NOTE-2",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": None},
        cc.DISPATCHER,
    )
    assert runtime.connection.execute(
        "SELECT 1 FROM ontology_overlay WHERE object_type = 'Shipment'"
    ).fetchone() is None
    assert (
        runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER).dispatcher_note
        is None
    )


def test_revert_of_owned_annotation_restores_default(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "RUN-NOTE",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "temporary"},
        cc.DISPATCHER,
    )
    assert runtime.connection.execute(
        "SELECT 1 FROM ontology_overlay WHERE object_type = 'Shipment'"
    ).fetchone()

    runtime.revert_action("RUN-NOTE", "RUN-NOTE-BACK", cc.OPS_MANAGER)
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.dispatcher_note is None
    assert shipment.status == "in_transit"
    assert runtime.connection.execute(
        "SELECT 1 FROM ontology_overlay WHERE object_type = 'Shipment'"
    ).fetchone() is None
    assert runtime.get_action_run("RUN-NOTE")["status"] == "REVERTED"


def test_overlay_is_behind_write_gate(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "RUN-NOTE",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "x"},
        cc.DISPATCHER,
    )
    conn = runtime.connection
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("UPDATE ontology_overlay SET patch_json = '{}'")
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("DELETE FROM ontology_overlay")
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute(
            """
            INSERT INTO ontology_overlay
                (object_type, object_id, patch_json, updated_at)
            VALUES ('Shipment', 'SHP-001', '{}', 'now')
            """
        )


def test_snapshot_reapplies_overlay_and_preserves_version(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "RUN-NOTE",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "must survive"},
        cc.DISPATCHER,
    )
    version_before = shipment_version(runtime)
    objects, links = cc._base_snapshot(runtime)

    result = runtime.load_snapshot(objects, links)
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.dispatcher_note == "must survive"
    assert shipment_version(runtime) == version_before
    assert result["overlays_reapplied"] == 1

    # a genuine source change still bumps the version, while the overlay stays
    objects["Shipment"][0]["status"] = "delayed"
    runtime.load_snapshot(objects, links)
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.status == "delayed"
    assert shipment.dispatcher_note == "must survive"
    assert shipment_version(runtime) == version_before + 1


def test_snapshot_rejects_owned_fields_unknown_fields_and_owned_types(demo, authority_runtime):
    runtime = demo.runtime
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    payload = shipment.model_dump(mode="json")

    with pytest.raises(GovernanceViolationError, match="ontology-owned"):
        runtime.load_snapshot({"Shipment": [payload]})

    clean = {key: value for key, value in payload.items() if key != "dispatcher_note"}
    clean["unknown_field"] = 1
    with pytest.raises(GovernanceViolationError):
        runtime.load_snapshot({"Shipment": [clean]})

    with pytest.raises(GovernanceViolationError, match="fully ontology-owned"):
        authority_runtime.load_snapshot(
            {"CaseFile": [{"case_id": "C2", "title": "not allowed"}]}
        )
    assert authority_runtime.get_object(CaseFile, "C1", OPS).title == "owned case"


def test_orphaned_overlay_reindex_is_refused_and_rolled_back(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "RUN-NOTE",
        "annotate_shipment",
        {"shipment_id": "SHP-001", "note": "orphan guard"},
        cc.DISPATCHER,
    )
    version_before = shipment_version(runtime)

    with pytest.raises(ReindexConflictError):
        runtime.load_snapshot({"Shipment": []})

    # whole transaction rolled back: base object, overlay and version intact
    shipment = runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)
    assert shipment.dispatcher_note == "orphan guard"
    assert shipment_version(runtime) == version_before
    overlay = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Shipment' AND object_id = 'SHP-001'"
    ).fetchone()
    assert json.loads(overlay["patch_json"]) == {"dispatcher_note": "orphan guard"}


@pytest.mark.parametrize(
    ("action", "params", "expected_code"),
    [
        (
            "touch_both",
            {"device_id": "D1", "status": "changed", "note": "x"},
            "MIXED_AUTHORITY",
        ),
        (
            "write_source_only",
            {"device_id": "D1"},
            "UNDECLARED_SOURCE_WRITE",
        ),
        (
            "write_owned_only",
            {"device_id": "D1", "note": "x"},
            "MISDECLARED_SOURCE_WRITE",
        ),
    ],
)
def test_authority_violations_are_blocked_and_audited(
    authority_runtime, action, params, expected_code
):
    runtime = authority_runtime
    before = runtime.list_objects("Device")

    sim = runtime.simulate_action(action, params, OPS)
    assert sim.valid is False
    assert sim.error_code == expected_code

    run_id = f"REJ-{action}"
    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(run_id, action, params, OPS)
    assert excinfo.value.code == expected_code

    rejected = runtime.get_action_run(run_id)
    assert rejected["status"] == "REJECTED"
    assert rejected["error_code"] == expected_code
    assert rejected["error_message"]
    assert rejected["object_diff"] == {"object_changes": [], "link_changes": []}
    assert runtime.list_objects("Device") == before
    assert runtime.connection.execute(
        "SELECT COUNT(*) AS n FROM ontology_overlay"
    ).fetchone()["n"] == 0


def test_cross_object_mixed_authority_is_rejected(authority_runtime):
    runtime = authority_runtime
    sim = runtime.simulate_action("split_authority", {}, OPS)
    assert sim.valid is False
    assert sim.error_code == "MIXED_AUTHORITY"
    assert _device_state(runtime) == ("ok", None)


def test_valid_owned_action_creates_overlay(authority_runtime):
    runtime = authority_runtime
    sim = runtime.simulate_action(
        "valid_owned", {"device_id": "D1", "note": "hello"}, OPS
    )
    assert sim.valid is True
    change = sim.diff.object_changes[0]
    assert change.after["note"] == "hello"
    assert change.after["status"] == "ok"

    runtime.submit_action(
        "RUN-OWNED", "valid_owned", {"device_id": "D1", "note": "hello"}, OPS
    )
    assert _device_state(runtime) == ("ok", "hello")
    overlay = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Device' AND object_id = 'D1'"
    ).fetchone()
    assert json.loads(overlay["patch_json"]) == {"note": "hello"}


def test_valid_source_action_does_not_touch_overlay(authority_runtime):
    runtime = authority_runtime
    sim = runtime.simulate_action("valid_source", {"device_id": "D1"}, OPS)
    assert sim.valid is True
    runtime.submit_action("RUN-SRC", "valid_source", {"device_id": "D1"}, OPS)
    assert _device_state(runtime) == ("changed", None)
    assert runtime.connection.execute(
        "SELECT COUNT(*) AS n FROM ontology_overlay"
    ).fetchone()["n"] == 0


def test_rejected_run_cannot_be_approved_or_reverted(authority_runtime):
    runtime = authority_runtime
    with pytest.raises(InvariantError):
        runtime.submit_action(
            "RUN-REJ",
            "write_source_only",
            {"device_id": "D1"},
            OPS,
        )
    with pytest.raises(GovernanceViolationError):
        runtime.approve_action("RUN-REJ", OPS)
    with pytest.raises(GovernanceViolationError):
        runtime.revert_action("RUN-REJ", "RUN-REJ-BACK", OPS)


# ---------------------------------------------------------------------------
# 16. v3 ObjectSet algebra, pivot and aggregation
# ---------------------------------------------------------------------------
def test_object_set_union_intersect_subtract(demo):
    runtime = demo.runtime
    all_trucks = runtime.search_set(cc.Truck, cc.OPS_MANAGER)
    available = runtime.search_set(cc.Truck, cc.OPS_MANAGER, {"status": "available"})
    assigned = runtime.search_set(cc.Truck, cc.OPS_MANAGER, {"status": "assigned"})

    assert set(all_trucks.ids) == {"TRK-001", "TRK-002", "TRK-003", "TRK-004"}
    assert set(available.ids) == {"TRK-002", "TRK-003", "TRK-004"}
    assert runtime.union(available, assigned).ids == [
        "TRK-002",
        "TRK-003",
        "TRK-004",
        "TRK-001",
    ]
    assert runtime.union(available, available).ids == available.ids
    assert runtime.intersect(all_trucks, assigned).ids == ["TRK-001"]
    assert set(runtime.subtract(all_trucks, assigned).ids) == {
        "TRK-002",
        "TRK-003",
        "TRK-004",
    }

    shipments = runtime.search_set(cc.Shipment, cc.OPS_MANAGER)
    with pytest.raises(ValueError):
        runtime.union(all_trucks, shipments)


def test_pivot_forward_reverse_and_rls(demo):
    runtime = demo.runtime
    shipments = runtime.search_set(cc.Shipment, cc.OPS_MANAGER)

    reverse = runtime.pivot(shipments, cc.CARRIES, cc.OPS_MANAGER)
    assert reverse.object_type == "Truck"
    assert reverse.ids == ["TRK-001"]

    forward = runtime.pivot(shipments, cc.SHIPS_FROM, cc.OPS_MANAGER, "forward")
    assert forward.object_type == "Warehouse"
    assert forward.ids == ["WH-001"]

    south = Actor(actor_id="south", role="auditor", clearance_tags={"region:south"})
    forged = ObjectSet(
        "Shipment",
        [runtime.get_object(cc.Shipment, "SHP-001", cc.OPS_MANAGER)],
    )
    assert runtime.pivot(forged, cc.CARRIES, south).ids == []

    with pytest.raises(ValueError):
        runtime.pivot(shipments, cc.CARRIES, cc.OPS_MANAGER, "sideways")
    with pytest.raises(ValueError):
        runtime.pivot(shipments, cc.CARRIES, cc.OPS_MANAGER, "forward")


def test_aggregate_and_filter_aggregation(demo):
    runtime = demo.runtime
    shipments = runtime.search_set(cc.Shipment, cc.OPS_MANAGER)

    agg = runtime.aggregate(shipments, group_by="status", sum_field="quantity")
    assert len(agg.rows) == 1
    row = agg.rows[0]
    assert row.key == "in_transit"
    assert row.object_ids == ["SHP-001"]
    assert row.metrics == {"count": 1, "sum": 120}

    total = runtime.aggregate(shipments, sum_field="quantity")
    assert total.rows[0].key is None
    assert total.rows[0].metrics == {"count": 1, "sum": 120}

    filtered = runtime.filter_aggregation(agg, lambda item: item.key == "in_transit")
    assert filtered.object_set.ids == ["SHP-001"]
    assert filtered.rows == agg.rows

    empty = runtime.filter_aggregation(agg, lambda item: item.key == "delivered")
    assert empty.rows == []
    assert empty.object_set.ids == []

    with pytest.raises(ValueError):
        runtime.aggregate(shipments, group_by="unknown")
    with pytest.raises(TypeError):
        runtime.aggregate(shipments, sum_field="product")


def test_aip_executor_pivot_and_aggregate(demo):
    runtime = demo.runtime
    executor = AIPAgentExecutor(runtime, cc.OPS_MANAGER)

    pivoted = executor.dispatch(
        "pivot",
        {"object_type": "Shipment", "link": "carries"},
    )
    assert pivoted["object_type"] == "Truck"
    assert pivoted["ids"] == ["TRK-001"]
    assert pivoted["objects"][0]["truck_id"] == "TRK-001"

    by_ids = executor.dispatch(
        "pivot",
        {
            "object_type": "Shipment",
            "ids": ["SHP-001"],
            "link": "ships_from",
            "direction": "forward",
        },
    )
    assert by_ids["ids"] == ["WH-001"]

    aggregated = executor.dispatch(
        "aggregate",
        {
            "object_type": "Shipment",
            "group_by": "status",
            "sum_field": "quantity",
        },
    )
    assert aggregated["total"] == 1
    assert aggregated["rows"][0]["metrics"] == {"count": 1, "sum": 120}


# ---------------------------------------------------------------------------
# 17. Zero IT-concept purity (v3 hard rule)
# ---------------------------------------------------------------------------
BANNED_IT_TERMS = (
    "outbox",
    "writeback",
    "target_system",
    "retry_count",
    "last_error",
    "lineage",
)


def test_pure_business_ontology_has_no_it_outbox_or_lineage_concepts(demo):
    runtime = demo.runtime
    conn = runtime.connection

    # 1. SQLite schema: exactly the five pure ontology tables + write gate
    tables = {
        row["name"].lower()
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == {
        "ontology_write_gate",
        "ontology_registry",
        "ontology_objects",
        "ontology_links",
        "ontology_action_runs",
        "ontology_overlay",
    }
    for term in BANNED_IT_TERMS:
        assert not any(term in table for table in tables), term

    # 2. ontology_objects has no external-system blood-line column
    object_columns = {
        row["name"].lower()
        for row in conn.execute("PRAGMA table_info(ontology_objects)")
    }
    assert object_columns == {
        "object_type",
        "object_id",
        "title",
        "properties_json",
        "version",
        "security_tags_json",
        "content_hash",
        "updated_at",
    }
    assert "lineage" not in object_columns
    assert "lineage_json" not in object_columns

    run_columns = {
        row["name"].lower()
        for row in conn.execute("PRAGMA table_info(ontology_action_runs)")
    }
    assert run_columns == {
        "run_id",
        "action_type",
        "actor_id",
        "actor_role",
        "params_json",
        "status",
        "read_versions_json",
        "object_diff_json",
        "approved_by",
        "reverted_by_run_id",
        "executed_at",
        "error_code",
        "error_message",
    }
    for term in BANNED_IT_TERMS:
        assert not any(term in column for column in object_columns | run_columns), term

    overlay_columns = {
        row["name"].lower()
        for row in conn.execute("PRAGMA table_info(ontology_overlay)")
    }
    assert overlay_columns == {
        "object_type",
        "object_id",
        "patch_json",
        "updated_at",
    }

    # 3. ActionDef stays a pure business declaration
    action_fields = {
        name.lower()
        for name in type(runtime.spec.get_action("reroute_shipment")).model_fields
    }
    assert "writeback_target" not in action_fields
    assert "lineage" not in action_fields
    assert action_fields == {
        "name",
        "params",
        "handler",
        "allowed_roles",
        "approver_roles",
        "approval_rule",
        "affects_source",
        "post_approval",
        "description",
    }
    with pytest.raises(TypeError):
        cc.spec.action("forbidden_action", cc.RerouteParams, writeback_target="x")

    # 4. Exported LLM tool schemas carry zero IT concepts
    tools_blob = json.dumps(
        export_llm_tools(cc.spec), ensure_ascii=False
    ).lower()
    for term in BANNED_IT_TERMS:
        assert term not in tools_blob, term

    # 5. Kernel + pure business domain text is clean (comments too).
    #    The external Odoo adapter is deliberately excluded: v4 keeps every
    #    JSON-RPC/HTTP concern inside examples/odoo_dual_erp_demo.py.
    root = Path(__file__).resolve().parents[1]
    sources = [
        *root.glob("open_ontology/*.py"),
        root / "open_ontology" / "schema.sql",
        root / "examples" / "cold_chain.py",
        root / "examples" / "odoo_orders_ontology.py",
    ]
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        for term in BANNED_IT_TERMS:
            assert term not in text, f"{term!r} leaked into {path.name}"
