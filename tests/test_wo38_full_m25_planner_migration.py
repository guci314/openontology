"""WO-38: Full 14/14 M2.5 Pure Planner & Contract Gate Migration Verification Suite.

Verifies that 100% of Enterprise Standard (9/9) and MedAuto Extension (14/14)
``EditFunction`` actions are decoupled into zero-write-handle ``read_only=True``
``@spec.function`` Pure Planners + DOAL Contract Gates (Read-Set CAS + Class A
``untrusted_verifiable`` / Class B ``attested_provider``), with end-to-end red/green
contract-gate tests across every domain (H2R, R2R, P2M, WorkCard, MedAuto) and
full Z3 PO-1 ~ PO-7 formal verification.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from examples.enterprise_console_app import build_console_service
from examples.medauto_export_extension import build_medauto_spec, seed_medauto_world
from examples.std_enterprise_world_model import build_enterprise_standard_spec
from open_ontology.console_server import ConsoleService, create_console_app
from open_ontology.contract_gate import (
    WriteBatchProposal,
    apply_contract_proposal,
    plan_contract_proposal,
    sign_proposal_attestation,
)
from open_ontology.runtime import OntologyRuntime
from open_ontology.spec import Actor, ConcurrencyConflictError, InvariantError

try:
    import z3 as _z3_check  # type: ignore[import-not-found]

    HAS_Z3 = _z3_check is not None
except ImportError:
    HAS_Z3 = False

requires_z3 = pytest.mark.skipif(not HAS_Z3, reason="z3-solver optional dependency not installed")


EXPECTED_ENTERPRISE_CLASS_A = {
    "open_work_card",
    "post_journal_entry",
    "reparent_department",
    "offboard_employee",
}
EXPECTED_ENTERPRISE_CLASS_B = {
    "complete_work_card",
    "issue_material_to_work_order",
    "record_production_output",
    "reverse_journal_entry",
    "close_fiscal_period",
}
EXPECTED_MEDAUTO_CLASS_A = EXPECTED_ENTERPRISE_CLASS_A | {
    "allocate_container_shipping_cost",
    "explode_multilevel_substitute_bom",
    "batch_recall_defective_lot",
}
EXPECTED_MEDAUTO_CLASS_B = EXPECTED_ENTERPRISE_CLASS_B | {
    "apply_export_tax_rebate_claim",
    "settle_annual_tiered_dealer_rebate",
}


def _open_enterprise_rt(tmp_path: Path, name: str) -> tuple[OntologyRuntime, Actor]:
    spec = build_enterprise_standard_spec()
    rt = OntologyRuntime(spec, db_path=tmp_path / f"{name}.db", world_mode="standalone")
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    return rt, admin


def test_wo38_100_percent_m25_planner_coverage_and_blueprint_metadata() -> None:
    ent_spec = build_enterprise_standard_spec()
    ent_efs = [a for a in ent_spec.actions.values() if a.edit_function]
    assert len(ent_efs) == 9
    assert {a.name for a in ent_efs if a.trust_mode == "untrusted_verifiable"} == EXPECTED_ENTERPRISE_CLASS_A
    assert {a.name for a in ent_efs if a.trust_mode == "attested_provider"} == EXPECTED_ENTERPRISE_CLASS_B
    for act in ent_efs:
        assert act.require_read_set_cas is True
        assert act.planner_function and act.planner_function in ent_spec.functions
        assert ent_spec.functions[act.planner_function].read_only is True
        if act.trust_mode == "attested_provider":
            assert act.attested_providers == (act.planner_function,)

    med_spec = build_medauto_spec()
    med_efs = [a for a in med_spec.actions.values() if a.edit_function]
    assert len(med_efs) == 14
    assert {a.name for a in med_efs if a.trust_mode == "untrusted_verifiable"} == EXPECTED_MEDAUTO_CLASS_A
    assert {a.name for a in med_efs if a.trust_mode == "attested_provider"} == EXPECTED_MEDAUTO_CLASS_B
    for act in med_efs:
        assert act.require_read_set_cas is True
        assert act.planner_function and act.planner_function in med_spec.functions
        assert med_spec.functions[act.planner_function].read_only is True
        if act.trust_mode == "attested_provider":
            assert act.attested_providers == (act.planner_function,)

    # Verify /api/blueprint exposes all 9 Enterprise + 14 MedAuto M2.5 badges & localized planner labels
    ent_svc = build_console_service(mode="standalone", enable_llm_assistant=False)
    try:
        ent_bp = TestClient(create_console_app(ent_svc)).get("/api/blueprint").json()
        ent_acts = {a["name"]: a for a in ent_bp["actions"] if a["implementation_kind"] == "edit_function"}
        assert len(ent_acts) == 9
        for name, a in ent_acts.items():
            assert a["contract"]["planner_function"], f"{name} missing planner_function in /api/blueprint"
            assert a["contract"]["require_read_set_cas"] is True
            assert a["contract"]["trust_mode"] in ("untrusted_verifiable", "attested_provider")
        ent_fns = {f["name"]: f for f in ent_bp["functions"]}
        for a in ent_acts.values():
            pf = a["contract"]["planner_function"]
            assert pf in ent_fns
            assert "M2.5" in ent_fns[pf]["display_label"]
    finally:
        ent_svc.close()
        ent_svc.runtime.close()

    med_svc = build_console_service(mode="medauto", enable_llm_assistant=False)
    try:
        med_bp = TestClient(create_console_app(med_svc)).get("/api/blueprint").json()
        med_acts = {a["name"]: a for a in med_bp["actions"] if a["implementation_kind"] == "edit_function"}
        assert len(med_acts) == 14
        med_fns = {f["name"]: f for f in med_bp["functions"]}
        for a in med_acts.values():
            pf = a["contract"]["planner_function"]
            assert pf in med_fns
            assert "M2.5" in med_fns[pf]["display_label"]
    finally:
        med_svc.close()
        med_svc.runtime.close()


def test_wo38_h2r_class_a_reparent_and_offboard_contract_gates(tmp_path: Path) -> None:
    rt, admin = _open_enterprise_rt(tmp_path, "h2r_wo38")
    try:
        spec = rt.spec
        Ent = spec.get_object_type("AccountingEntity")
        Dept = spec.get_object_type("Department")
        Pos = spec.get_object_type("JobPosition")
        Emp = spec.get_object_type("Employee")
        Mnd = spec.get_object_type("AuthorityMandate")

        rt.put_object(Ent(entity_id="ENT-A", name="Entity A", base_currency="CNY"), actor=admin)
        rt.put_object(Ent(entity_id="ENT-B", name="Entity B", base_currency="CNY"), actor=admin)
        rt.put_object(Dept(department_id="D-HQ", accounting_entity="ENT-A", name="HQ"), actor=admin)
        rt.put_object(
            Dept(department_id="D-ENG", accounting_entity="ENT-A", name="Eng", parent_department="D-HQ"),
            actor=admin,
        )
        rt.put_object(
            Dept(department_id="D-OPS", accounting_entity="ENT-A", name="Ops", parent_department="D-HQ"),
            actor=admin,
        )
        rt.put_object(Dept(department_id="D-OTHER", accounting_entity="ENT-B", name="Other"), actor=admin)

        # 1) reparent_department (Class A): untrusted brain proposing cycle (D-HQ -> D-ENG) -> BLOCKED!
        hq_ver = rt.load_object("Department", "D-HQ").version
        cycle_prop = WriteBatchProposal(
            proposal_id="PROP-H2R-CYCLE",
            contract_name="reparent_department",
            params={"department_id": "D-HQ", "new_parent_id": "D-ENG"},
            read_set=[{"object_type": "Department", "object_id": "D-HQ", "expected_version": hq_ver}],
            ops=[
                {
                    "op": "modify",
                    "object_type": "Department",
                    "object_id": "D-HQ",
                    "set_fields": {"parent_department": "D-ENG"},
                }
            ],
            brain_source="untrusted_llm",
        )
        with pytest.raises(InvariantError) as exc_cyc:
            apply_contract_proposal(rt, cycle_prop, admin)
        assert exc_cyc.value.code == "DEPARTMENT_CYCLE_DETECTED"

        # 2) Untrusted brain proposing cross-entity parent (D-ENG -> D-OTHER) -> BLOCKED by ensures!
        eng_ver = rt.load_object("Department", "D-ENG").version
        xent_prop = WriteBatchProposal(
            proposal_id="PROP-H2R-XENT",
            contract_name="reparent_department",
            params={"department_id": "D-ENG", "new_parent_id": "D-OTHER"},
            read_set=[{"object_type": "Department", "object_id": "D-ENG", "expected_version": eng_ver}],
            ops=[
                {
                    "op": "modify",
                    "object_type": "Department",
                    "object_id": "D-ENG",
                    "set_fields": {"parent_department": "D-OTHER"},
                }
            ],
            brain_source="untrusted_llm",
        )
        with pytest.raises(InvariantError) as exc_xent:
            apply_contract_proposal(rt, xent_prop, admin)
        assert exc_xent.value.code == "CONTRACT_ENSURES_VIOLATION"

        # 3) Valid reparent via pure planner (zero DB writes) + atomic commit + Read-Set CAS guard
        fp0 = rt.state_fingerprint()
        valid_rep = plan_contract_proposal(
            rt,
            "reparent_department",
            {"department_id": "D-ENG", "new_parent_id": "D-OPS"},
            admin,
            proposal_id="PROP-H2R-REPARENT-OK",
        )
        assert rt.state_fingerprint() == fp0
        assert valid_rep.attestation is None
        res_rep = apply_contract_proposal(rt, valid_rep, admin)
        assert res_rep["status"] == "COMMITTED"
        assert rt.get_object("Department", "D-ENG", admin).parent_department == "D-OPS"

        stale_rep = valid_rep.model_copy(update={"proposal_id": "PROP-H2R-STALE"})
        with pytest.raises(ConcurrencyConflictError):
            apply_contract_proposal(rt, stale_rep, admin)

        # 4) offboard_employee (Class A): seed 3 employees + 2 active mandates (1 owned by E1, 1 unrelated E2->E3)
        rt.put_object(
            Pos(position_id="P-MGR", department="D-HQ", title="Mgr", role_name="finance_director", approval_ceiling_cents=100000),
            actor=admin,
        )
        for eid in ("E-1", "E-2", "E-3"):
            rt.put_object(
                Emp(employee_id=eid, department="D-HQ", position="P-MGR", full_name=eid, actor_id=f"u-{eid}", status="active"),
                actor=admin,
            )
        rt.put_object(
            Mnd(mandate_id="M-E1", grantor="E-2", grantee="E-1", role_name="finance", ceiling_cents=50000, status="active"),
            actor=admin,
        )
        rt.put_object(
            Mnd(mandate_id="M-UNREL", grantor="E-2", grantee="E-3", role_name="finance", ceiling_cents=50000, status="active"),
            actor=admin,
        )

        e1_ver = rt.load_object("Employee", "E-1").version
        m1_ver = rt.load_object("AuthorityMandate", "M-E1").version
        mu_ver = rt.load_object("AuthorityMandate", "M-UNREL").version

        # 4a) Incomplete offboard proposal (forgets to revoke M-E1) -> OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN
        incomplete_off = WriteBatchProposal(
            proposal_id="PROP-OFF-INCOMPLETE",
            contract_name="offboard_employee",
            params={"employee_id": "E-1"},
            read_set=[{"object_type": "Employee", "object_id": "E-1", "expected_version": e1_ver}],
            ops=[
                {
                    "op": "modify",
                    "object_type": "Employee",
                    "object_id": "E-1",
                    "set_fields": {"status": "offboarded"},
                }
            ],
        )
        with pytest.raises(InvariantError) as exc_inc:
            apply_contract_proposal(rt, incomplete_off, admin)
        assert exc_inc.value.code == "OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN"

        # 4b) Sneaky proposal that ALSO revokes unrelated M-UNREL -> CONTRACT_ENSURES_VIOLATION
        sneaky_off = WriteBatchProposal(
            proposal_id="PROP-OFF-SNEAKY",
            contract_name="offboard_employee",
            params={"employee_id": "E-1"},
            read_set=[
                {"object_type": "Employee", "object_id": "E-1", "expected_version": e1_ver},
                {"object_type": "AuthorityMandate", "object_id": "M-E1", "expected_version": m1_ver},
                {"object_type": "AuthorityMandate", "object_id": "M-UNREL", "expected_version": mu_ver},
            ],
            ops=[
                {"op": "modify", "object_type": "AuthorityMandate", "object_id": "M-E1", "set_fields": {"status": "revoked"}},
                {"op": "modify", "object_type": "AuthorityMandate", "object_id": "M-UNREL", "set_fields": {"status": "revoked"}},
                {"op": "modify", "object_type": "Employee", "object_id": "E-1", "set_fields": {"status": "offboarded"}},
            ],
        )
        with pytest.raises(InvariantError) as exc_snk:
            apply_contract_proposal(rt, sneaky_off, admin)
        assert exc_snk.value.code == "CONTRACT_ENSURES_VIOLATION"

        # 4c) Genuine pure planner proposal revokes M-E1 + offboards E-1 atomically while leaving M-UNREL active
        ok_off = plan_contract_proposal(
            rt, "offboard_employee", {"employee_id": "E-1"}, admin, proposal_id="PROP-OFF-OK"
        )
        res_off = apply_contract_proposal(rt, ok_off, admin)
        assert res_off["status"] == "COMMITTED"
        assert rt.get_object("Employee", "E-1", admin).status == "offboarded"
        assert rt.get_object("AuthorityMandate", "M-E1", admin).status == "revoked"
        assert rt.get_object("AuthorityMandate", "M-UNREL", admin).status == "active"
    finally:
        rt.close()


def test_wo38_r2r_class_a_post_and_class_b_reverse_close_contract_gates(tmp_path: Path) -> None:
    rt, admin = _open_enterprise_rt(tmp_path, "r2r_wo38")
    try:
        spec = rt.spec
        Ent = spec.get_object_type("AccountingEntity")
        Fp = spec.get_object_type("FiscalPeriod")
        Acc = spec.get_object_type("GlAccount")
        Je = spec.get_object_type("JournalEntry")

        rt.put_object(Ent(entity_id="ENT-1", name="Group", base_currency="CNY"), actor=admin)
        rt.put_object(
            Fp(
                period_id="FP-2026-11",
                accounting_entity="ENT-1",
                start_date="2026-11-01",
                end_date="2026-11-30",
                status="open",
            ),
            actor=admin,
        )
        rt.put_object(
            Acc(
                account_code="1001",
                accounting_entity="ENT-1",
                name="Cash",
                category="ASSET",
                normal_side="DEBIT",
            ),
            actor=admin,
        )
        rt.put_object(
            Acc(
                account_code="4001",
                accounting_entity="ENT-1",
                name="Revenue",
                category="REVENUE",
                normal_side="CREDIT",
            ),
            actor=admin,
        )
        rt.put_object(
            Je(
                entry_id="JE-100",
                accounting_entity="ENT-1",
                fiscal_period="FP-2026-11",
                debit_account="1001",
                credit_account="4001",
                amount_cents=50_000,
                memo="Sales receipt",
                status="draft",
            ),
            actor=admin,
        )

        # 1) post_journal_entry (Class A): unbalanced credit delta rejected!
        bad_post = plan_contract_proposal(
            rt, "post_journal_entry", {"entry_id": "JE-100"}, admin, proposal_id="PROP-JE-UNBAL"
        )
        assert bad_post.attestation is None
        cr_op = next(op for op in bad_post.ops if op.object_type == "GlAccount" and op.object_id == "4001")
        cr_op.set_fields["credit_total_cents"] = 49_999  # drop 1 cent -> violates trial balance!
        with pytest.raises(InvariantError) as exc_unbal:
            apply_contract_proposal(rt, bad_post, admin)
        assert exc_unbal.value.code in ("TRIAL_BALANCE_IMBALANCE", "CONTRACT_ENSURES_VIOLATION")

        # Valid post_journal_entry commits cleanly
        ok_post = plan_contract_proposal(
            rt, "post_journal_entry", {"entry_id": "JE-100"}, admin, proposal_id="PROP-JE-OK"
        )
        assert apply_contract_proposal(rt, ok_post, admin)["status"] == "COMMITTED"
        assert rt.get_object("JournalEntry", "JE-100", admin).status == "posted"
        assert rt.get_object("GlAccount", "1001", admin).debit_total_cents == 50_000
        assert rt.get_object("GlAccount", "4001", admin).credit_total_cents == 50_000

        # 2) reverse_journal_entry (Class B): raw unattested blocked, tampered blocked, deterministic `-REV` commits!
        gen_rev = plan_contract_proposal(
            rt,
            "reverse_journal_entry",
            {"entry_id": "JE-100"},  # omit reversal_entry_id to prove deterministic JE-100-REV!
            admin,
            proposal_id="PROP-REV-OK",
        )
        assert gen_rev.attestation is not None
        assert gen_rev.attestation.provider_id == "plan_reverse_journal_entry"

        raw_rev = gen_rev.model_copy(update={"proposal_id": "PROP-REV-RAW", "attestation": None})
        with pytest.raises(InvariantError) as exc_raw:
            apply_contract_proposal(rt, raw_rev, admin)
        assert exc_raw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        tamp_rev = gen_rev.model_copy(deep=True)
        tamp_rev.proposal_id = "PROP-REV-TAMP"
        acc_op = next(op for op in tamp_rev.ops if op.object_type == "GlAccount")
        for k in list(acc_op.set_fields):
            acc_op.set_fields[k] = 12_345
        tamp_rev = tamp_rev.model_copy(
            update={"attestation": sign_proposal_attestation(tamp_rev, "plan_reverse_journal_entry")}
        )
        with pytest.raises(InvariantError) as exc_tamp:
            apply_contract_proposal(rt, tamp_rev, admin)
        assert exc_tamp.value.code == "ATTESTATION_MISMATCH"

        assert apply_contract_proposal(rt, gen_rev, admin)["status"] == "COMMITTED"
        assert rt.get_object("JournalEntry", "JE-100", admin).status == "reversed"
        assert rt.get_object("JournalEntry", "JE-100-REV", admin).status == "posted"
        assert rt.get_object("GlAccount", "1001", admin).credit_total_cents == 50_000
        assert rt.get_object("GlAccount", "4001", admin).debit_total_cents == 50_000

        # 3) close_fiscal_period (Class B): advance open -> closing, raw blocked, attested commits!
        rt.submit_action("RUN-FP-CLOSING", "start_fiscal_period_closing", {"period_id": "FP-2026-11"}, admin)
        gen_close = plan_contract_proposal(
            rt, "close_fiscal_period", {"period_id": "FP-2026-11"}, admin, proposal_id="PROP-CLOSE-OK"
        )
        raw_close = gen_close.model_copy(update={"proposal_id": "PROP-CLOSE-RAW", "attestation": None})
        with pytest.raises(InvariantError) as exc_cl_raw:
            apply_contract_proposal(rt, raw_close, admin)
        assert exc_cl_raw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        assert apply_contract_proposal(rt, gen_close, admin)["status"] == "COMMITTED"
        assert rt.get_object("FiscalPeriod", "FP-2026-11", admin).status == "closed"
    finally:
        rt.close()


def test_wo38_p2m_class_b_issue_material_and_record_output_contract_gates(tmp_path: Path) -> None:
    rt, admin = _open_enterprise_rt(tmp_path, "p2m_wo38")
    try:
        spec = rt.spec
        Ent = spec.get_object_type("AccountingEntity")
        Res = spec.get_object_type("TradeResource")
        Bom = spec.get_object_type("BomSpec")
        Wo = spec.get_object_type("WorkOrder")

        rt.put_object(Ent(entity_id="ENT-1", name="Plant", base_currency="CNY"), actor=admin)
        rt.put_object(Res(resource_id="R-RAW", code="RAW", name="Raw", unit="KG"), actor=admin)
        rt.put_object(Res(resource_id="R-FG", code="FG", name="FG", unit="EA"), actor=admin)
        rt.put_object(
            Bom(bom_id="BOM-1", output_resource="R-FG", input_resource="R-RAW", input_per_unit=2.0, max_scrap_ratio=0.1),
            actor=admin,
        )
        rt.put_object(
            Wo(
                work_order_id="WO-1",
                accounting_entity="ENT-1",
                bom="BOM-1",
                output_resource="R-FG",
                planned_qty=10.0,
                status="draft",
            ),
            actor=admin,
        )
        rt.submit_action("RUN-REL-WO1", "release_work_order", {"work_order_id": "WO-1"}, admin)

        # 1) issue_material_to_work_order (Class B)
        gen_iss = plan_contract_proposal(
            rt,
            "issue_material_to_work_order",
            {
                "consumption_id": "CONS-1",
                "work_order_id": "WO-1",
                "input_resource": "R-RAW",
                "qty": 20.0,
                "unit_cost_cents": 500,
            },
            admin,
            proposal_id="PROP-ISS-OK",
        )
        raw_iss = gen_iss.model_copy(update={"proposal_id": "PROP-ISS-RAW", "attestation": None})
        with pytest.raises(InvariantError) as exc_iss_raw:
            apply_contract_proposal(rt, raw_iss, admin)
        assert exc_iss_raw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        tamp_iss = gen_iss.model_copy(deep=True)
        tamp_iss.proposal_id = "PROP-ISS-TAMP"
        tamp_iss.ops[1].set_fields["issued_cost_cents"] = 1  # forged cost
        tamp_iss = tamp_iss.model_copy(
            update={"attestation": sign_proposal_attestation(tamp_iss, "plan_issue_material_to_work_order")}
        )
        with pytest.raises(InvariantError) as exc_iss_tamp:
            apply_contract_proposal(rt, tamp_iss, admin)
        assert exc_iss_tamp.value.code == "ATTESTATION_MISMATCH"

        assert apply_contract_proposal(rt, gen_iss, admin)["status"] == "COMMITTED"
        wo_mid = rt.get_object("WorkOrder", "WO-1", admin)
        assert wo_mid.status == "in_progress"
        assert wo_mid.issued_cost_cents == 10_000

        # 2) record_production_output (Class B)
        gen_out = plan_contract_proposal(
            rt,
            "record_production_output",
            {
                "output_id": "OUT-1",
                "work_order_id": "WO-1",
                "output_resource": "R-FG",
                "good_qty": 10.0,
                "scrap_qty": 0.0,
                "absorbed_cost_cents": 10_000,
            },
            admin,
            proposal_id="PROP-OUT-OK",
        )
        raw_out = gen_out.model_copy(update={"proposal_id": "PROP-OUT-RAW", "attestation": None})
        with pytest.raises(InvariantError) as exc_out_raw:
            apply_contract_proposal(rt, raw_out, admin)
        assert exc_out_raw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        assert apply_contract_proposal(rt, gen_out, admin)["status"] == "COMMITTED"
        wo_done = rt.get_object("WorkOrder", "WO-1", admin)
        assert wo_done.completed_good_qty == 10.0
        assert rt.get_object("ProductionOutput", "OUT-1", admin).absorbed_cost_cents == 10_000
    finally:
        rt.close()


def test_wo38_work_card_class_a_open_and_class_b_complete_honest_boundary(tmp_path: Path) -> None:
    rt, admin = _open_enterprise_rt(tmp_path, "wc_wo38")
    try:
        requester = Actor(actor_id="u-req", role="ops", clearance_tags={"*"})
        steward = Actor(actor_id="u-stew", role="steward", clearance_tags={"*"})

        # 1) open_work_card (Class A): valid commit + duplicate blocked + forged status blocked
        open_prop = plan_contract_proposal(
            rt,
            "open_work_card",
            {
                "card_id": "WC-M25-01",
                "card_kind": "HUMAN_TASK",
                "target_object_type": "Department",
                "target_object_id": "D-1",
                "requested_action": "manual_review",
                "assignee_role": "steward",
            },
            requester,
            proposal_id="PROP-WC-OPEN-01",
        )
        assert open_prop.attestation is None
        assert apply_contract_proposal(rt, open_prop, requester)["status"] == "COMMITTED"

        # Forged WorkCard skipping OPEN -> COMPLETED rejected by ensures
        bad_open = open_prop.model_copy(deep=True)
        bad_open.proposal_id = "PROP-WC-OPEN-FORGED"
        bad_open.ops[0].object_id = "WC-M25-02"
        bad_open.ops[0].set_fields["card_id"] = "WC-M25-02"
        bad_open.ops[0].set_fields["status"] = "COMPLETED"
        with pytest.raises(InvariantError) as exc_forged:
            apply_contract_proposal(rt, bad_open, requester)
        assert exc_forged.value.code == "CONTRACT_ENSURES_VIOLATION"

        # Duplicate active WorkCard intent rejected
        dup_open = open_prop.model_copy(deep=True)
        dup_open.proposal_id = "PROP-WC-OPEN-DUP"
        dup_open.ops[0].object_id = "WC-M25-DUP"
        dup_open.ops[0].set_fields["card_id"] = "WC-M25-DUP"
        with pytest.raises(InvariantError) as exc_dup:
            apply_contract_proposal(rt, dup_open, requester)
        assert exc_dup.value.code in ("DUPLICATE_ACTIVE_WORK_CARD", "CONTRACT_ENSURES_VIOLATION")

        # 2) complete_work_card (Class B): raw blocked, SoD self-completion blocked, attested steward commits!
        with pytest.raises(InvariantError) as exc_sod:
            plan_contract_proposal(
                rt, "complete_work_card", {"card_id": "WC-M25-01", "resolution_note": "self"}, requester
            )
        assert exc_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        gen_comp = plan_contract_proposal(
            rt,
            "complete_work_card",
            {"card_id": "WC-M25-01", "resolution_note": "reviewed"},
            steward,
            proposal_id="PROP-WC-COMP-01",
        )
        raw_comp = gen_comp.model_copy(update={"proposal_id": "PROP-WC-COMP-RAW", "attestation": None})
        with pytest.raises(InvariantError) as exc_comp_raw:
            apply_contract_proposal(rt, raw_comp, steward)
        assert exc_comp_raw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        assert apply_contract_proposal(rt, gen_comp, steward)["status"] == "COMMITTED"
        done_card = rt.get_object("WorkCard", "WC-M25-01", steward)
        assert done_card.status == "COMPLETED"
        assert done_card.resolved_by_actor_id == "u-stew"

        # 3) Honest M2.5 boundary:
        # 3a) Source-backed APPROVAL card requires settled_run_id
        rt.submit_action(
            "RUN-WC-APPROVAL-OPEN",
            "open_work_card",
            {
                "card_id": "WC-APPR-SRC",
                "card_kind": "APPROVAL",
                "target_object_type": "Department",
                "target_object_id": "D-1",
                "requested_action": "reparent_department",
                "requested_params_json": '{"department_id": "D-1", "new_parent_id": ""}',
                "assignee_role": "steward",
            },
            requester,
        )
        with pytest.raises(InvariantError) as exc_src:
            plan_contract_proposal(
                rt, "complete_work_card", {"card_id": "WC-APPR-SRC"}, steward
            )
        assert exc_src.value.code == "SOURCE_APPROVAL_REQUIRES_SETTLED_RUN"

        # Source-backed settled APPROVAL card DOES work cleanly through the pure planner
        settled_prop = plan_contract_proposal(
            rt,
            "complete_work_card",
            {"card_id": "WC-APPR-SRC", "settled_run_id": "RUN-EXT-01"},
            steward,
            proposal_id="PROP-WC-APPR-SETTLED",
        )
        assert apply_contract_proposal(rt, settled_prop, steward)["status"] == "COMMITTED"
        assert rt.get_object("WorkCard", "WC-APPR-SRC", steward).status == "COMPLETED"

        # 3b) Non-source delegated APPROVAL card honestly flags DELEGATED_APPROVAL_REQUIRES_DIRECT_DISPATCH
        rt.submit_action(
            "RUN-WC-APPROVAL-DEL",
            "open_work_card",
            {
                "card_id": "WC-APPR-DEL",
                "card_kind": "APPROVAL",
                "target_object_type": "WorkCard",
                "target_object_id": "WC-M25-01",
                "requested_action": "mark_work_card_sla_breached",
                "requested_params_json": '{"card_id": "WC-M25-01"}',
                "assignee_role": "steward",
            },
            requester,
        )
        with pytest.raises(InvariantError) as exc_del:
            plan_contract_proposal(
                rt, "complete_work_card", {"card_id": "WC-APPR-DEL"}, steward
            )
        assert exc_del.value.code == "DELEGATED_APPROVAL_REQUIRES_DIRECT_DISPATCH"
    finally:
        rt.close()


def test_wo38_medauto_remaining_batch_recall_class_a_and_tiered_rebate_class_b(
    tmp_path: Path,
) -> None:
    spec = build_medauto_spec()
    rt = OntologyRuntime(spec, db_path=tmp_path / "medauto_wo38.db", world_mode="standalone")
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    try:
        seed_medauto_world(rt, admin)

        # 1) batch_recall_defective_lot (Class A):
        # 1a) Wrong-lot quarantine attempt -> CONTRACT_ENSURES_VIOLATION
        wo3_ver = rt.load_object("WorkOrder", "WO-MED-003").version  # LOT-2026-10 (NOT LOT-2026-09!)
        wrong_lot = WriteBatchProposal(
            proposal_id="PROP-RECALL-WRONGLOT",
            contract_name="batch_recall_defective_lot",
            params={"lot_code": "LOT-2026-09"},
            read_set=[{"object_type": "WorkOrder", "object_id": "WO-MED-003", "expected_version": wo3_ver}],
            ops=[
                {
                    "op": "modify",
                    "object_type": "WorkOrder",
                    "object_id": "WO-MED-003",
                    "set_fields": {"x_quarantined": True},
                }
            ],
        )
        with pytest.raises(InvariantError) as exc_wl:
            apply_contract_proposal(rt, wrong_lot, admin)
        assert exc_wl.value.code == "CONTRACT_ENSURES_VIOLATION"

        # 1b) Partial recall (only quarantining 1 of the LOT-2026-09 objects) -> CONTRACT_ENSURES_VIOLATION
        full_recall = plan_contract_proposal(
            rt,
            "batch_recall_defective_lot",
            {"lot_code": "LOT-2026-09", "reason": "M2.5 批次缺陷隔离"},
            admin,
            proposal_id="PROP-RECALL-OK",
        )
        assert full_recall.attestation is None
        assert len(full_recall.ops) >= 4  # >=2 targets * (modify + create WorkCard)

        partial_recall = full_recall.model_copy(
            update={"proposal_id": "PROP-RECALL-PARTIAL", "ops": full_recall.ops[:2]}
        )
        with pytest.raises(InvariantError) as exc_part:
            apply_contract_proposal(rt, partial_recall, admin)
        assert exc_part.value.code == "CONTRACT_ENSURES_VIOLATION"

        # 1c) Genuine complete recall proposal commits atomically!
        assert apply_contract_proposal(rt, full_recall, admin)["status"] == "COMMITTED"
        assert rt.get_object("WorkOrder", "WO-MED-001", admin).x_quarantined is True
        assert rt.get_object("WorkOrder", "WO-MED-002", admin).x_quarantined is True
        assert rt.get_object("WorkOrder", "WO-MED-003", admin).x_quarantined is False

        # 2) settle_annual_tiered_dealer_rebate (Class B):
        gen_reb = plan_contract_proposal(
            rt,
            "settle_annual_tiered_dealer_rebate",
            {
                "settlement_id": "SET-M25-01",
                "settlement_code": "REB-2026-DE-M25",
                "account_id": "ACC-DE-01",
            },
            admin,
            proposal_id="PROP-TIERED-OK",
        )
        assert gen_reb.attestation is not None
        assert gen_reb.attestation.provider_id == "plan_annual_tiered_dealer_rebate"

        # 2a) Raw unattested forged rebate -> UNTRUSTED_PROPOSAL_PROVIDER
        raw_reb = gen_reb.model_copy(update={"proposal_id": "PROP-TIERED-RAW", "attestation": None})
        with pytest.raises(InvariantError) as exc_traw:
            apply_contract_proposal(rt, raw_reb, admin)
        assert exc_traw.value.code == "UNTRUSTED_PROPOSAL_PROVIDER"

        # 2b) Spoofed attestation with inflated rebate_cents -> ATTESTATION_MISMATCH
        tamp_reb = gen_reb.model_copy(deep=True)
        tamp_reb.proposal_id = "PROP-TIERED-TAMP"
        tamp_reb.ops[-1].set_fields["rebate_cents"] = 999_999_999
        tamp_reb = tamp_reb.model_copy(
            update={"attestation": sign_proposal_attestation(tamp_reb, "plan_annual_tiered_dealer_rebate")}
        )
        with pytest.raises(InvariantError) as exc_ttamp:
            apply_contract_proposal(rt, tamp_reb, admin)
        assert exc_ttamp.value.code == "ATTESTATION_MISMATCH"

        # 2c) Genuine attested proposal commits atomically
        assert apply_contract_proposal(rt, gen_reb, admin)["status"] == "COMMITTED"
        voucher = rt.get_object("DealerRebateSettlement", "SET-M25-01", admin)
        assert voucher.rebate_cents > 0
        assert voucher.annual_sales_cents > 0
    finally:
        rt.close()


@requires_z3
def test_wo38_z3_po1_to_po7_clean_for_enterprise_and_medauto() -> None:
    from open_ontology.induction.prover import prove_blueprint

    ent_rep = prove_blueprint(build_enterprise_standard_spec(), check_redundancy=True)
    assert ent_rep.ok is True
    assert ent_rep.exact_wp_proved_count == 22
    assert ent_rep.contract_proved_count == 9
    assert ent_rep.vacuous_contract_count == 0
    assert ent_rep.when_branch_issue_count == 0
    assert ent_rep.workflow_issue_count == 0

    med_rep = prove_blueprint(build_medauto_spec(), check_redundancy=True)
    assert med_rep.ok is True
    assert med_rep.exact_wp_proved_count == 29
    assert med_rep.contract_proved_count == 14
    assert med_rep.vacuous_contract_count == 0
    assert med_rep.when_branch_issue_count == 0
    assert med_rep.workflow_issue_count == 0
