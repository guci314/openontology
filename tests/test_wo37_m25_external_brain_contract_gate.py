"""WO-37: M2.5 External Turing-Complete Brain & Contract-Gated Commit ABI Tests.

Covers:
1. Gate 6 purity (pure runtime imports never load ``open_ontology.contract_gate``, ``dsl``, ``prover``, or ``z3``).
2. Class A (``untrusted_verifiable``): external zero-write-handle brain + NP-verifiable conservation ``ensures``
   (``sum == total_freight_cents``) + Frame violation + Read-Set CAS anti-TOCTOU + idempotency + time-travel revert.
3. Class A recursive BOM pure planner (``plan_multilevel_substitute_bom`` -> ``explode_multilevel_substitute_bom``).
4. Class B (``attested_provider``): statutory formula protection (``apply_export_tax_rebate_claim``) —
   blocks raw unattested proposals, blocks tampered proposals via zero-trust pure-planner cross-check,
   supports external HMAC attestation keys, and commits valid attested proposals.
5. Console REST API (``/api/contracts/plan``, ``/api/sandboxes/{sid}/propose``, ``/api/blueprint``) and
   AI assistant dynamic ``submit_sandbox_contract_proposal`` tool isolation.
6. Full Z3 PO-1~PO-7 proof on MedAuto including the upgraded conservation ``ensures``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from examples.enterprise_console_app import build_starship_console
from examples.medauto_export_extension import build_medauto_spec, seed_medauto_world
from open_ontology.console_server import ConsoleService, create_console_app
from open_ontology.contract_gate import (
    WriteBatchProposal,
    apply_contract_proposal,
    build_read_snapshot,
    plan_contract_proposal,
    sign_proposal_attestation,
)
from open_ontology.runtime import OntologyRuntime
from open_ontology.spec import Actor, ConcurrencyConflictError, InvariantError

REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    import z3 as _z3_check  # type: ignore[import-not-found]
    HAS_Z3 = _z3_check is not None
except ImportError:
    HAS_Z3 = False

requires_z3 = pytest.mark.skipif(not HAS_Z3, reason="z3-solver optional dependency not installed")


def _open_seeded_medauto(tmp_path: Path) -> tuple[OntologyRuntime, Actor]:
    spec = build_medauto_spec()
    rt = OntologyRuntime(spec, db_path=tmp_path / "medauto_wo37.db")
    admin = Actor(actor_id="u_admin", role="admin")
    seed_medauto_world(rt, admin)
    return rt, admin


def test_gate6_pure_runtime_does_not_load_contract_gate() -> None:
    code = (
        "import sys, open_ontology, open_ontology.spec, open_ontology.runtime\n"
        "leaked = [m for m in ('open_ontology.contract_gate', 'open_ontology.induction.dsl', 'z3') if m in sys.modules]\n"
        "assert not leaked, f'Gate 6 violated: {leaked}'\n"
        "print('GATE6_WO37_CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "GATE6_WO37_CLEAN" in proc.stdout


def test_class_a_untrusted_verifiable_freight_conservation_cas_frame_and_revert(
    tmp_path: Path,
) -> None:
    rt, admin = _open_seeded_medauto(tmp_path)
    try:
        # 1) External zero-write-handle brain reads versioned snapshot
        snap = build_read_snapshot(rt, admin, object_types=["ExportSalesOrder"])
        orders = snap["objects"]["ExportSalesOrder"]
        cntr_orders = [
            o for o in orders if o["properties"]["x_container_no"] == "CNTR-DE-01"
        ]
        assert len(cntr_orders) == 2

        # 2) Pure read-only planner emits proposal with ZERO writes to DB
        before_fp = rt.state_fingerprint()
        planned = plan_contract_proposal(
            rt,
            "allocate_container_shipping_cost",
            {"container_no": "CNTR-DE-01", "total_freight_cents": 120000},
            admin,
        )
        assert rt.state_fingerprint() == before_fp
        assert planned.attestation is None  # Class A requires no attestation
        assert len(planned.ops) == 2
        assert len(planned.read_set) == 2
        assert sum(o.set_fields["x_allocated_freight_cents"] for o in planned.ops) == 120000

        # 3) Untrusted external brain submits a custom balanced split (70000 + 50000 = 120000) -> COMMITTED!
        ext_prop = WriteBatchProposal(
            proposal_id="PROP-EXT-BALANCED-01",
            contract_name="allocate_container_shipping_cost",
            params={"container_no": "CNTR-DE-01", "total_freight_cents": 120000},
            read_set=[
                {
                    "object_type": "ExportSalesOrder",
                    "object_id": o["object_id"],
                    "expected_version": o["version"],
                }
                for o in cntr_orders
            ],
            ops=[
                {
                    "op": "modify",
                    "object_type": "ExportSalesOrder",
                    "object_id": cntr_orders[0]["object_id"],
                    "set_fields": {"x_allocated_freight_cents": 70000},
                },
                {
                    "op": "modify",
                    "object_type": "ExportSalesOrder",
                    "object_id": cntr_orders[1]["object_id"],
                    "set_fields": {"x_allocated_freight_cents": 50000},
                },
            ],
            brain_source="external_or_tools_solver",
        )
        run = apply_contract_proposal(rt, ext_prop, admin)
        assert run["status"] == "COMMITTED"
        # Idempotency check: same proposal_id returns exact existing run
        run_dup = apply_contract_proposal(rt, ext_prop, admin)
        assert run_dup["run_id"] == run["run_id"]

        # 4) Conservation violation (cheating brain drops 1 cent: 70000 + 49999 != 120000) -> REJECTED + 0 dirty writes
        bad_sum = plan_contract_proposal(
            rt,
            "allocate_container_shipping_cost",
            {"container_no": "CNTR-DE-01", "total_freight_cents": 120000},
            admin,
            proposal_id="PROP-CHEAT-SUM-01",
        )
        bad_sum.ops[1].set_fields["x_allocated_freight_cents"] -= 1
        with pytest.raises(InvariantError) as exc_sum:
            apply_contract_proposal(rt, bad_sum, admin)
        assert exc_sum.value.code == "CONTRACT_ENSURES_VIOLATION"
        assert rt.get_action_run("PROP-CHEAT-SUM-01")["status"] == "REJECTED"

        # 5) Frame violation (sneaky brain tries to also flip x_lc_verified=True) -> CONTRACT_FRAME_VIOLATION
        bad_frame = plan_contract_proposal(
            rt,
            "allocate_container_shipping_cost",
            {"container_no": "CNTR-DE-01", "total_freight_cents": 120000},
            admin,
            proposal_id="PROP-CHEAT-FRAME-01",
        )
        bad_frame.ops[0].set_fields["x_lc_verified"] = True
        with pytest.raises(InvariantError) as exc_frame:
            apply_contract_proposal(rt, bad_frame, admin)
        assert exc_frame.value.code == "CONTRACT_FRAME_VIOLATION"

        # 6) Read-Set CAS anti-TOCTOU: stale version -> ConcurrencyConflictError
        stale_cas = plan_contract_proposal(
            rt,
            "allocate_container_shipping_cost",
            {"container_no": "CNTR-DE-01", "total_freight_cents": 120000},
            admin,
            proposal_id="PROP-STALE-CAS-01",
        )
        stale_cas.read_set[0].expected_version = 1  # already bumped to 2 by step 3!
        with pytest.raises(ConcurrencyConflictError):
            apply_contract_proposal(rt, stale_cas, admin)

        # Simulate mode returns clean SimulationResult with CONCURRENCY_CONFLICT
        sim_stale = apply_contract_proposal(rt, stale_cas, admin, simulate=True)
        assert sim_stale.valid is False
        assert sim_stale.error_code == "CONCURRENCY_CONFLICT"

        # 7) Missing read_set pin -> MISSING_READ_SET_VERSION
        missing_rs = plan_contract_proposal(
            rt,
            "allocate_container_shipping_cost",
            {"container_no": "CNTR-DE-01", "total_freight_cents": 120000},
            admin,
            proposal_id="PROP-MISSING-RS-01",
        )
        missing_rs.read_set = []
        with pytest.raises(InvariantError) as exc_rs:
            apply_contract_proposal(rt, missing_rs, admin)
        assert exc_rs.value.code == "MISSING_READ_SET_VERSION"

        # 8) Empty ops rejected -> EMPTY_PROPOSAL_OPS
        empty_ops = planned.model_copy(update={"proposal_id": "PROP-EMPTY-01", "ops": []})
        with pytest.raises(InvariantError) as exc_empty:
            apply_contract_proposal(rt, empty_ops, admin)
        assert exc_empty.value.code == "EMPTY_PROPOSAL_OPS"

        # 9) Time-travel revert restores exact pre-proposal state!
        rev = rt.revert_action("PROP-EXT-BALANCED-01", "REV-EXT-01", admin)
        assert rev["status"] == "COMMITTED"
        o0 = rt.get_object("ExportSalesOrder", cntr_orders[0]["object_id"], admin)
        assert o0.x_allocated_freight_cents == 0
    finally:
        rt.close()


def test_class_a_recursive_bom_pure_planner_and_gate(tmp_path: Path) -> None:
    rt, admin = _open_seeded_medauto(tmp_path)
    try:
        bom_prop = plan_contract_proposal(
            rt,
            "explode_multilevel_substitute_bom",
            {
                "parent_work_order_id": "WO-MED-001",
                "available_stock_json": "{}",
                "child_wo_prefix": "M25-SUB",
            },
            admin,
            proposal_id="PROP-BOM-01",
        )
        assert len(bom_prop.ops) >= 2
        assert all(op.op == "create" and op.object_type == "WorkOrder" for op in bom_prop.ops)

        # Reject forged child WorkOrder violating ensures (non-draft initial status)
        bad_bom = bom_prop.model_copy(deep=True)
        bad_bom.proposal_id = "PROP-BOM-BAD-01"
        bad_bom.ops[0].set_fields["status"] = "in_progress"
        with pytest.raises(InvariantError) as exc_bom:
            apply_contract_proposal(rt, bad_bom, admin)
        assert exc_bom.value.code == "CONTRACT_ENSURES_VIOLATION"

        # Valid pure-planner proposal commits atomically
        res = apply_contract_proposal(rt, bom_prop, admin)
        assert res["status"] == "COMMITTED"
        created_ids = [op.object_id for op in bom_prop.ops]
        for wid in created_ids:
            assert rt.get_object("WorkOrder", str(wid), admin).status == "draft"
    finally:
        rt.close()


def test_class_b_attested_provider_blocks_raw_and_tampered_rebate_proposals(
    tmp_path: Path,
) -> None:
    rt, admin = _open_seeded_medauto(tmp_path)
    try:
        # Advance CD-001 to 'cleared' via native DOAL Rule action first
        rt.submit_action(
            "RUN-CLEAR-CD001",
            "declare_customs_clearance",
            {"declaration_id": "CD-001"},
            admin,
        )
        loaded_cd = rt.load_object("CustomsDeclaration", "CD-001")
        assert loaded_cd is not None
        cd_ver = loaded_cd.version

        # 1) Unattested raw proposal (hallucinating LLM trying to grant 999,999,999 cents) -> BLOCKED!
        raw_cheat = WriteBatchProposal(
            proposal_id="PROP-REBATE-RAW-01",
            contract_name="apply_export_tax_rebate_claim",
            params={"declaration_id": "CD-001", "rebate_rate": 0.13, "fx_rate": 7.2},
            read_set=[
                {
                    "object_type": "CustomsDeclaration",
                    "object_id": "CD-001",
                    "expected_version": cd_ver,
                }
            ],
            ops=[
                {
                    "op": "modify",
                    "object_type": "CustomsDeclaration",
                    "object_id": "CD-001",
                    "set_fields": {"rebate_amount_cents": 999999999, "rebate_claimed": True},
                }
            ],
        )
        with pytest.raises(InvariantError) as exc_raw:
            apply_contract_proposal(rt, raw_cheat, admin)
        assert exc_raw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        # 2) Spoofed provider_id + recomputed SHA256 with tampered amount -> BLOCKED by zero-trust planner cross-check!
        spoofed = raw_cheat.model_copy(update={"proposal_id": "PROP-REBATE-SPOOF-01"})
        spoofed = spoofed.model_copy(
            update={"attestation": sign_proposal_attestation(spoofed, "plan_export_tax_rebate_claim")}
        )
        with pytest.raises(InvariantError) as exc_spoof:
            apply_contract_proposal(rt, spoofed, admin)
        assert exc_spoof.value.code == "ATTESTATION_MISMATCH"

        # 3) External HMAC attestation verification (rejects wrong key, accepts valid key)
        genuine = plan_contract_proposal(
            rt,
            "apply_export_tax_rebate_claim",
            {"declaration_id": "CD-001", "rebate_rate": 0.13, "fx_rate": 7.2},
            admin,
            proposal_id="PROP-REBATE-HMAC-01",
            secret_key="audit-key-v1",
        )
        assert genuine.attestation is not None
        assert genuine.attestation.provider_id == "plan_export_tax_rebate_claim"
        with pytest.raises(InvariantError) as exc_hmac:
            apply_contract_proposal(
                rt,
                genuine.model_copy(update={"proposal_id": "PROP-REBATE-BADHMAC-01"}),
                admin,
                attestation_keys={"plan_export_tax_rebate_claim": "wrong-key"},
            )
        assert exc_hmac.value.code == "ATTESTATION_MISMATCH"

        # 4) Genuine attested proposal commits cleanly via zero-trust cross-verification!
        genuine_clean = plan_contract_proposal(
            rt,
            "apply_export_tax_rebate_claim",
            {"declaration_id": "CD-001", "rebate_rate": 0.13, "fx_rate": 7.2},
            admin,
            proposal_id="PROP-REBATE-OK-01",
        )
        res = apply_contract_proposal(rt, genuine_clean, admin)
        assert res["status"] == "COMMITTED"
        decl_after = rt.get_object("CustomsDeclaration", "CD-001", admin)
        assert decl_after.rebate_claimed is True
        assert decl_after.rebate_amount_cents == genuine_clean.ops[0].set_fields["rebate_amount_cents"]
    finally:
        rt.close()


def test_console_rest_api_blueprint_metadata_and_sandbox_proposal_isolation(
    tmp_path: Path,
) -> None:
    rt, admin = _open_seeded_medauto(tmp_path)
    try:
        svc = ConsoleService(rt, demo_actor_switch=True, default_actor=admin, pack_hint="none")
        client = TestClient(create_console_app(svc))

        # 1) /api/blueprint exposes M2.5 trust_mode, require_read_set_cas, planner_function
        bp_resp = client.get("/api/blueprint")
        assert bp_resp.status_code == 200
        acts = {a["name"]: a for a in bp_resp.json()["actions"]}
        assert acts["allocate_container_shipping_cost"]["contract"]["trust_mode"] == "untrusted_verifiable"
        assert acts["allocate_container_shipping_cost"]["contract"]["planner_function"] == "plan_container_shipping_cost"
        assert acts["allocate_container_shipping_cost"]["contract"]["require_read_set_cas"] is True
        assert acts["apply_export_tax_rebate_claim"]["contract"]["trust_mode"] == "attested_provider"
        assert acts["apply_export_tax_rebate_claim"]["contract"]["attested_providers"] == [
            "plan_export_tax_rebate_claim"
        ]

        # 2) /api/contracts/plan + /api/sandboxes/{sid}/propose keeps primary world 0 dirty writes!
        primary_fp = rt.state_fingerprint()
        sbx = client.post("/api/sandboxes").json()
        sid = sbx["sandbox_id"]

        plan_resp = client.post(
            "/api/contracts/plan",
            json={
                "contract_name": "allocate_container_shipping_cost",
                "params": {"container_no": "CNTR-DE-01", "total_freight_cents": 100000},
                "sandbox_id": sid,
            },
        )
        assert plan_resp.status_code == 200
        plan_body = plan_resp.json()
        assert plan_body["ok"] is True
        assert plan_body["trust_mode"] == "untrusted_verifiable"

        prop_resp = client.post(
            f"/api/sandboxes/{sid}/propose",
            json={"proposal": plan_body["proposal"]},
        )
        assert prop_resp.status_code == 200
        assert prop_resp.json()["ok"] is True

        # Sandbox diff shows 2 updated ExportSalesOrder records; primary world untouched!
        diff_resp = client.get(f"/api/sandboxes/{sid}/diff").json()
        assert diff_resp["total_changes"] == 2
        assert rt.state_fingerprint() == primary_fp

        # 3) AI Assistant tool gating: Starship (0 planners) has exact 5 tools; MedAuto has 6th sandbox proposal tool!
        st_res = build_starship_console()
        st_svc = st_res if isinstance(st_res, ConsoleService) else st_res[0]
        assert len(st_svc._build_m2_llm_tools(include_contract_proposals=False)) == 5
        med_tools = [t["function"]["name"] for t in svc._build_m2_llm_tools(include_contract_proposals=True)]
        assert "submit_sandbox_contract_proposal" in med_tools
        assert "submit_action" not in med_tools

        # Direct tool execution runs strictly inside sandbox
        holder: dict = {"sid": None}
        trace: list = []
        discovered: list = []
        tool_out = svc._execute_m2_assistant_tool(
            "submit_sandbox_contract_proposal",
            {
                "contract_name": "allocate_container_shipping_cost",
                "params": {"container_no": "CNTR-DE-01", "total_freight_cents": 100000},
                "read_set": plan_body["proposal"]["read_set"],
                "ops": plan_body["proposal"]["ops"],
            },
            admin,
            holder,
            trace,
            discovered,
        )
        assert tool_out["ok"] is True
        assert tool_out["diff_summary"]["total_changes"] == 2
        assert rt.state_fingerprint() == primary_fp
    finally:
        rt.close()


@requires_z3
def test_medauto_with_conservation_ensures_proves_100_percent_clean_in_z3() -> None:
    from open_ontology.induction.prover import prove_blueprint

    rep = prove_blueprint(build_medauto_spec(), check_redundancy=True)
    assert rep.ok is True
    assert rep.exact_wp_proved_count == 29
    assert rep.contract_proved_count == 14
    assert rep.vacuous_contract_count == 0
    assert rep.when_branch_issue_count == 0
    assert rep.workflow_issue_count == 0
