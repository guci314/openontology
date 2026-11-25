"""WO-35 Tests: Modular EditFunction Behavioral Contracts, PO-5 Realizability & Whole-Model Z3 Proofs."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel

from open_ontology import Actor, InvariantError, ObjectNotFoundError, ObjectType, OntologyRuntime, OntologySpec
from open_ontology.console_server import ConsoleService, DemoActorProfile, start_console_server
from open_ontology.induction import (
    ActionBlueprint,
    CustomerOverlaySpec,
    FieldSpec,
    InvariantRuleSpec,
    compile_blueprint,
    enforce_edit_function_contracts,
    export_spec_blueprint,
)
from open_ontology.induction.prover import prove_blueprint


def _make_wallet_spec(
    *,
    buggy_mode: str = "none",
    weak_ensures: bool = False,
    vacuous_ensures: bool = False,
    uncontracted: bool = False,
) -> OntologySpec:
    spec = OntologySpec()
    spec.role("finance", "Finance operator")
    spec.role("admin", "Administrator")

    @spec.object_type
    class Account(ObjectType):
        __primary_key__: ClassVar[str] = "account_id"
        __non_negative_fields__: ClassVar[tuple[str, ...]] = ("balance",)

        account_id: str
        owner: str = "Alice"
        balance: int = 0
        frozen: bool = False

    @spec.object_type
    class AuditLog(ObjectType):
        __primary_key__: ClassVar[str] = "log_id"

        log_id: str
        note: str = ""

    class AdjustParams(BaseModel):
        account_id: str
        delta: int

    if vacuous_ensures:
        ens: tuple[dict, ...] = (
            {
                "name": "impossible_bounds",
                "code": "IMPOSSIBLE",
                "target_type": "Account",
                "id_param": "account_id",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "gt", "left": {"ref": "after.balance"}, "right": {"lit": 100}},
                        {"op": "lt", "left": {"ref": "after.balance"}, "right": {"lit": 50}},
                    ],
                },
            },
        )
    elif weak_ensures:
        ens = (
            {
                "name": "balance_shifted",
                "code": "WEAK_CONTRACT",
                "target_type": "Account",
                "id_param": "account_id",
                "must_hold": {
                    "op": "le",
                    "left": {"ref": "after.balance"},
                    "right": {"ref": "before.balance"},
                },
            },
        )
    elif uncontracted:
        ens = ()
    else:
        ens = (
            {
                "name": "balance_after_ok",
                "code": "CONTRACT_ENSURES_VIOLATION",
                "target_type": "Account",
                "id_param": "account_id",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "ge", "left": {"ref": "after.balance"}, "right": {"lit": 0}},
                        {"op": "eq", "left": {"ref": "before.frozen"}, "right": {"lit": False}},
                    ],
                },
            },
        )

    contract_kwargs = (
        {}
        if uncontracted
        else {
            "modifies": ("Account.balance",),
            "ensures": ens,
        }
    )

    @spec.edit_function(
        "adjust_account_balance",
        AdjustParams,
        touched_object_types=("Account",),
        **contract_kwargs,
    )
    def adjust_account_balance(ctx: OntologyRuntime, params: AdjustParams) -> None:
        acc = ctx.require(Account, params.account_id)
        if buggy_mode == "undeclared_field":
            ctx.modify(acc, balance=acc.balance + params.delta, owner="Hijacked")
            return
        if buggy_mode == "undeclared_create":
            ctx.create(AuditLog(log_id="LOG-SNEAK", note="hidden"))
            ctx.modify(acc, balance=acc.balance + params.delta)
            return
        if buggy_mode == "undeclared_delete":
            log_row = ctx.require(AuditLog, "LOG-1")
            ctx.delete(log_row)
            ctx.modify(acc, balance=acc.balance + params.delta)
            return
        if buggy_mode == "violate_ensures":
            ctx.modify(acc, balance=acc.balance + params.delta)
            return
        if acc.frozen or acc.balance + params.delta < 0:
            raise InvariantError("cannot apply invalid delta", code="INVALID_DELTA")
        ctx.modify(acc, balance=acc.balance + params.delta)

    spec.declare_action(
        "adjust_account_balance",
        edit_function="adjust_account_balance",
        allowed_roles=("finance", "admin"),
    )
    enforce_edit_function_contracts(spec)
    return spec


# ---------------------------------------------------------------------------
# 1. Runtime Contract Enforcement (Frame + Ensures + Atomic Rollback, Py3.13 & Py3.11)
# ---------------------------------------------------------------------------
def test_runtime_frame_violation_undeclared_modify_create_delete_and_rollback() -> None:
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    Account_name = "Account"

    # 1a. Undeclared field modification -> CONTRACT_FRAME_VIOLATION + atomic rollback
    spec_mod = _make_wallet_spec(buggy_mode="undeclared_field")
    rt_mod = OntologyRuntime(spec_mod, db_path=":memory:")
    try:
        Acc = spec_mod.get_object_type(Account_name)
        rt_mod.put_object(Acc(account_id="A-1", owner="Alice", balance=100))
        with pytest.raises(InvariantError) as exc:
            rt_mod.submit_action("R-1", "adjust_account_balance", {"account_id": "A-1", "delta": 25}, admin)
        assert exc.value.code == "CONTRACT_FRAME_VIOLATION"
        row = rt_mod.get_object(Acc, "A-1", admin)
        assert row.owner == "Alice"
        assert row.balance == 100
    finally:
        rt_mod.close()

    # 1b. Undeclared ObjectType create -> CONTRACT_FRAME_VIOLATION + atomic rollback
    spec_cre = _make_wallet_spec(buggy_mode="undeclared_create")
    rt_cre = OntologyRuntime(spec_cre, db_path=":memory:")
    try:
        Acc = spec_cre.get_object_type("Account")
        Log = spec_cre.get_object_type("AuditLog")
        rt_cre.put_object(Acc(account_id="A-1", balance=100))
        with pytest.raises(InvariantError) as exc:
            rt_cre.submit_action("R-2", "adjust_account_balance", {"account_id": "A-1", "delta": 25}, admin)
        assert exc.value.code == "CONTRACT_FRAME_VIOLATION"
        assert rt_cre.get_object(Acc, "A-1", admin).balance == 100
        with pytest.raises(ObjectNotFoundError):
            rt_cre.get_object(Log, "LOG-SNEAK", admin)
    finally:
        rt_cre.close()

    # 1c. Undeclared ObjectType delete -> CONTRACT_FRAME_VIOLATION + atomic rollback
    spec_del = _make_wallet_spec(buggy_mode="undeclared_delete")
    rt_del = OntologyRuntime(spec_del, db_path=":memory:")
    try:
        Acc = spec_del.get_object_type("Account")
        Log = spec_del.get_object_type("AuditLog")
        rt_del.put_object(Acc(account_id="A-1", balance=100))
        rt_del.put_object(Log(log_id="LOG-1", note="keep"))
        with pytest.raises(InvariantError) as exc:
            rt_del.submit_action("R-3", "adjust_account_balance", {"account_id": "A-1", "delta": 25}, admin)
        assert exc.value.code == "CONTRACT_FRAME_VIOLATION"
        assert rt_del.get_object(Acc, "A-1", admin).balance == 100
        assert rt_del.get_object(Log, "LOG-1", admin) is not None
    finally:
        rt_del.close()


def test_runtime_ensures_violation_and_atomic_rollback() -> None:
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    spec = _make_wallet_spec(buggy_mode="violate_ensures")
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        Acc = spec.get_object_type("Account")
        rt.put_object(Acc(account_id="A-FRZ", balance=100, frozen=True))
        with pytest.raises(InvariantError) as exc:
            rt.submit_action("R-ENS", "adjust_account_balance", {"account_id": "A-FRZ", "delta": 10}, admin)
        assert exc.value.code == "CONTRACT_ENSURES_VIOLATION"
        # Atomic rollback ensures zero dirty writes in DB
        assert rt.get_object(Acc, "A-FRZ", admin).balance == 100
    finally:
        rt.close()


def test_subtype_frame_inheritance_and_blueprint_roundtrip() -> None:
    from examples.medauto_export_extension import build_medauto_spec

    spec = build_medauto_spec()
    bp = export_spec_blueprint(spec, model_id="medauto_roundtrip")
    act_bp = next(a for a in bp.actions if a.name == "batch_recall_defective_lot")
    assert "WorkOrder.x_quarantined" in act_bp.modifies
    assert "WorkCard" in act_bp.creates
    assert len(act_bp.ensures) == 4

    recompiled = compile_blueprint(bp, base_specs=(spec,), edit_functions=spec.edit_functions)
    re_act = recompiled.get_action("batch_recall_defective_lot")
    assert set(re_act.modifies) == set(act_bp.modifies)
    assert set(re_act.creates) == set(act_bp.creates)
    assert len(re_act.ensures) == len(act_bp.ensures) == 4


# ---------------------------------------------------------------------------
# 2. Z3 Modular Verification: PO-5 Realizability + PO-4b Weak / Conflict Detection
# ---------------------------------------------------------------------------
def test_z3_vacuous_contract_rejected_with_minimal_unsat_core() -> None:
    pytest.importorskip("z3")
    spec = _make_wallet_spec(vacuous_ensures=True)
    rep = prove_blueprint(spec)
    assert rep.ok is False
    assert rep.vacuous_contract_count == 1
    assert rep.contract_proved_count == 0
    assert len(rep.invariant_preservation) == 1
    item = rep.invariant_preservation[0]
    assert item.action_name == "adjust_account_balance"
    assert item.status == "VACUOUS_CONTRACT"
    assert item.proof_kind == "contract_assume_guarantee"
    assert any("impossible_bounds" in c for c in item.unsat_core)

    # Also verify /api/blueprint/prove surfaces ISSUES_FOUND cleanly on vacuous contracts
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        svc = ConsoleService(
            rt,
            default_actor=admin,
            demo_actors=[
                DemoActorProfile(
                    actor_id="act-admin",
                    display_name="Admin",
                    role="admin",
                    token="tok",
                )
            ],
            pack_hint="none",
        )
        proved = svc.prove_current_blueprint(admin)
        assert proved["verdict"] == "ISSUES_FOUND"
        assert proved["summary"]["vacuous_contract_count"] == 1
    finally:
        rt.close()


def test_z3_weak_contract_caught_with_negative_counterexample() -> None:
    pytest.importorskip("z3")
    spec = _make_wallet_spec(weak_ensures=True)
    rep = prove_blueprint(spec)
    assert rep.ok is False
    assert rep.vacuous_contract_count == 0
    assert rep.invariant_violation_count == 1
    item = rep.invariant_preservation[0]
    assert item.action_name == "adjust_account_balance"
    assert item.status == "VIOLATED"
    assert item.proof_kind == "contract_assume_guarantee"
    assert "Account.balance_non_negative" in item.violated_invariants
    assert len(item.counterexamples) >= 1
    after_vals = next(iter(item.counterexamples[0].after.values()))
    assert after_vals["balance"] < 0


def test_z3_cross_overlay_doal_invariant_conflict_caught_on_edit_function() -> None:
    pytest.importorskip("z3")
    from examples.std_enterprise_world_model import build_enterprise_standard_spec

    base_spec = build_enterprise_standard_spec()
    # Add a stricter customer DOAL invariant conflicting with reparent_department's ensures/frame:
    # e.g., every active Department MUST have a non-null parent_department, whereas reparent_department
    # modifies parent_department allowing None.
    conflicting_overlay = CustomerOverlaySpec(
        overlay_id="strict_parent_overlay",
        add_invariants=[
            InvariantRuleSpec(
                name="dept_always_requires_parent",
                code="MISSING_PARENT_DEPT",
                target_type="Department",
                when={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "active"}},
                must_hold={
                    "op": "ne",
                    "left": {"ref": "obj.parent_department"},
                    "right": {"lit": None},
                },
            )
        ],
    )
    rep = prove_blueprint(base_spec, overlay=conflicting_overlay, check_redundancy=False)
    by_act = {p.action_name: p for p in rep.invariant_preservation}
    assert by_act["reparent_department"].status == "VIOLATED"
    assert "dept_always_requires_parent" in by_act["reparent_department"].violated_invariants
    assert rep.ok is False


def test_z3_uncontracted_edit_function_never_faked_as_proved() -> None:
    pytest.importorskip("z3")
    spec = _make_wallet_spec(uncontracted=True)
    rep = prove_blueprint(spec)
    assert rep.uncontracted_edit_function_count == 1
    assert rep.contract_proved_count == 0
    assert rep.invariant_preservation[0].status == "SKIPPED_UNCONTRACTED"
    assert rep.invariant_preservation[0].proof_kind == "skipped"


def test_whole_model_100_percent_z3_verification_enterprise_and_medauto() -> None:
    pytest.importorskip("z3")
    from examples.enterprise_console_app import build_console_service
    from examples.medauto_export_extension import build_medauto_spec

    ent_svc = build_console_service(mode="standalone", demo_actor_switch=False)
    try:
        rep_ent = prove_blueprint(ent_svc.runtime.spec, check_redundancy=True)
        assert rep_ent.ok is True
        assert len(rep_ent.invariant_preservation) == 33
        assert rep_ent.exact_wp_proved_count == 24
        assert rep_ent.contract_proved_count == 9
        assert rep_ent.vacuous_contract_count == 0
        assert rep_ent.uncontracted_edit_function_count == 0
    finally:
        ent_svc.runtime.close()

    rep_med = prove_blueprint(build_medauto_spec(), check_redundancy=True)
    assert rep_med.ok is True
    assert len(rep_med.invariant_preservation) == 43
    assert rep_med.exact_wp_proved_count == 29
    assert rep_med.contract_proved_count == 14
    assert rep_med.vacuous_contract_count == 0
    assert rep_med.uncontracted_edit_function_count == 0


# ---------------------------------------------------------------------------
# 3. Playwright /blueprint UI Verification (Contract Chips + 43/43 Verdict, Py3.13)
# ---------------------------------------------------------------------------
def test_playwright_blueprint_displays_contracts_and_43_of_43_breakdown() -> None:
    pytest.importorskip("z3")
    playwright_sync = pytest.importorskip("playwright.sync_api")
    from examples.medauto_export_extension import build_medauto_console

    svc = build_medauto_console(enable_llm_assistant=False)
    srv = start_console_server(svc, host="127.0.0.1", port=0)
    try:
        with playwright_sync.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            page = context.new_page()
            page.goto(f"{srv.base_url}/", wait_until="networkidle")
            page.get_by_test_id("nav-blueprint").click()
            page.get_by_test_id("blueprint-page").wait_for(state="visible", timeout=15000)

            # EditFunction contract chips are visible on /blueprint cards
            ef_card = page.get_by_test_id("ef-contract-allocate_container_shipping_cost")
            ef_card.scroll_into_view_if_needed()
            ef_card.wait_for(state="visible", timeout=10000)
            ef_text = ef_card.inner_text()
            assert "modifies: ExportSalesOrder.x_allocated_freight_cents" in ef_text
            assert "ensures × 1" in ef_text

            # Trigger Z3 verification and inspect honest whole-model breakdown
            page.get_by_test_id("run-z3-prove-btn").click()
            verdict = page.get_by_test_id("z3-verdict-clean")
            verdict.wait_for(state="visible", timeout=30000)
            summary_text = page.get_by_test_id("z3-hoare-summary").inner_text()
            assert "全模型 Hoare 保真：43 / 43" in summary_text
            assert "Rule 精确 wp 29 + EditFn 契约证明 14" in summary_text
            assert "0 空真契约 · 运行期强验守约" in summary_text
            context.close()
            browser.close()
    finally:
        srv.stop()
        svc.runtime.close()
