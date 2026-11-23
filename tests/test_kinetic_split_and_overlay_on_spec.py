"""WO-27A Verification Suite: Kinetic Trio Split, @spec.action Retirement & apply_overlay_to_spec."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
import pytest

from open_ontology import (
    ActionDef,
    Actor,
    EditFunctionDef,
    EditFunctionNotFoundError,
    InvariantError,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    PermissionDeniedError,
)
from open_ontology.console_server import ConsoleService, create_console_app
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    apply_overlay_to_spec,
    compile_blueprint,
)
from open_ontology.model_views import describe_behavior
from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.std_h2r_ontology import (
    AuthorityMandate,
    Department,
    Employee,
    JobPosition,
    spec as std_h2r_spec,
)
from examples.std_core_ontology import AccountingEntity
from examples.std_r2r_ontology import (
    FiscalPeriod,
    GlAccount,
    JournalEntry,
    spec as std_r2r_spec,
)


# ---------------------------------------------------------------------------
# 1. Three-way Kinetic Orthogonality + Complete @spec.action Retirement
# ---------------------------------------------------------------------------
def test_1_kinetic_trio_orthogonality_and_spec_action_retired() -> None:
    class Account(ObjectType):
        __primary_key__ = "acc_id"
        acc_id: str
        balance: int = 0

    class CreditParams(BaseModel):
        acc_id: str
        delta: int

    assert not hasattr(OntologySpec, "action")
    spec = OntologySpec()
    spec.role("ops")
    spec.object_type(Account)

    with pytest.raises(AttributeError):
        _ = spec.action

    # @spec.edit_function rejects governance shell kwargs
    with pytest.raises(TypeError):
        spec.edit_function("bad_ef", CreditParams, allowed_roles=("ops",))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        spec.edit_function("bad_ef2", CreditParams, affects_source=False)  # type: ignore[call-arg]

    # EditFunctionDef schema itself rejects governance shell fields
    with pytest.raises(ValidationError):
        EditFunctionDef(
            name="credit",
            params=CreditParams,
            handler=lambda ctx, p: None,
            allowed_roles=("ops",),  # type: ignore[call-arg]
        )

    @spec.edit_function(
        "credit_account",
        CreditParams,
        description="Increase account balance atomically",
        touched_object_types=("Account",),
    )
    def credit_account(ctx, params: CreditParams) -> None:
        acc = ctx.require(Account, params.acc_id)
        ctx.modify(acc, balance=acc.balance + params.delta)

    declared = spec.declare_action(
        "credit_account_ops",
        edit_function="credit_account",
        allowed_roles=("ops",),
    )
    assert isinstance(declared, ActionDef)
    assert declared.implementation_kind == "edit_function"
    assert declared.edit_function == "credit_account"
    assert declared.params is CreditParams
    assert declared.touched_object_types == ("Account",)
    assert declared.description == "Increase account balance atomically"

    with pytest.raises(EditFunctionNotFoundError):
        spec.declare_action("broken_action", edit_function="does_not_exist", allowed_roles=("ops",))

    # compile_blueprint rule actions vs ActionBlueprint(edit_function=...)
    bp = DeclarativeBlueprint(
        model_id="trio_bp",
        object_types=[
            ObjectTypeBlueprint(
                name="Account",
                primary_key="acc_id",
                fields=[
                    FieldSpec(name="acc_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="balance", field_type="int", default=0),
                ],
            )
        ],
        actions=[
            ActionBlueprint(
                name="reset_balance_rule",
                params=[FieldSpec(name="acc_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="must_be_non_negative",
                        code="ALREADY_NEGATIVE",
                        target_type="Account",
                        id_param="acc_id",
                        must_hold={"op": "ge", "left": {"ref": "obj.balance"}, "right": {"lit": 0}},
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Account",
                        id_param="acc_id",
                        set_fields={"balance": {"lit": 0}},
                    )
                ],
            ),
            ActionBlueprint(
                name="credit_via_ef",
                edit_function="credit_account",
                allowed_roles=["admin"],
            ),
        ],
    )
    compiled = compile_blueprint(bp, edit_functions={"credit_account": spec.get_edit_function("credit_account")})
    rule_act = compiled.get_action("reset_balance_rule")
    assert rule_act.implementation_kind == "rule"
    assert rule_act.edit_function is None
    assert len(rule_act.submission_criteria) == 1

    ef_act = compiled.get_action("credit_via_ef")
    assert ef_act.implementation_kind == "edit_function"
    assert ef_act.edit_function == "credit_account"

    with pytest.raises(ValueError, match="cannot specify both"):
        ActionBlueprint(
            name="hybrid_illegal",
            edit_function="credit_account",
            mutations=[ActionMutationSpec(op="modify", object_type="Account", id_param="acc_id", set_fields={"balance": 1})],
        )


# ---------------------------------------------------------------------------
# 2. Native std_h2r_spec + apply_overlay_to_spec: Preserves Turing Loops & Adds Constraints + RLS
# ---------------------------------------------------------------------------
def test_2_native_std_h2r_spec_with_apply_overlay_to_spec() -> None:
    overlay = CustomerOverlaySpec(
        add_fields={
            "Employee": [
                FieldSpec(name="badge_level", field_type="str", default="L1", owned=True),
            ]
        },
        add_action_preconditions={
            "grant_authority_mandate": [
                InvariantRuleSpec(
                    name="high_ceiling_requires_l3_badge",
                    code="GRANTEE_BADGE_TOO_LOW",
                    target_type="Employee",
                    id_param="grantee_id",
                    when={
                        "op": "gt",
                        "left": {"ref": "param.ceiling_cents"},
                        "right": {"lit": 500_000},
                    },
                    must_hold={
                        "op": "in",
                        "left": {"ref": "obj.badge_level"},
                        "right": {"lit": ["L3", "L4"]},
                    },
                    description="授权额度高于 ¥5,000 时，被授权人职级标签必须为 L3 或 L4。",
                )
            ]
        },
    )

    overlaid = apply_overlay_to_spec(
        std_h2r_spec,
        overlay,
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        rls_object_types=("Department",),
    )

    # Zero mutation on original std_h2r_spec and its base ObjectType classes
    assert "badge_level" not in Employee.model_fields
    assert "org" not in Department.model_fields
    assert std_h2r_spec.object_types["Employee"] is Employee
    assert std_h2r_spec.object_types["Department"] is Department
    assert "badge_level" in overlaid.object_types["Employee"].model_fields
    assert "org" in overlaid.object_types["Department"].model_fields

    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    hr_dir = Actor(actor_id="act-hr", role="hr_director", clearance_tags={"*"})
    north_actor = Actor(actor_id="act-north", role="hr_admin", clearance_tags={"org:plant_north"})
    south_actor = Actor(actor_id="act-south", role="hr_admin", clearance_tags={"org:plant_south"})

    EmpCls = overlaid.get_object_type("Employee")
    DeptCls = overlaid.get_object_type("Department")

    rt = OntologyRuntime(overlaid)
    try:
        rt.put_object(AccountingEntity(entity_id="ENT-1", name="Group HQ"))
        rt.put_object(DeptCls(department_id="D-ROOT", accounting_entity="ENT-1", name="HQ", org="plant_north"))
        rt.put_object(DeptCls(department_id="D-A", accounting_entity="ENT-1", name="Line A", parent_department="D-ROOT", org="plant_north"))
        rt.put_object(DeptCls(department_id="D-B", accounting_entity="ENT-1", name="Line B", parent_department="D-A", org="plant_north"))
        rt.put_object(DeptCls(department_id="D-C", accounting_entity="ENT-1", name="Line C", parent_department="D-B", org="plant_north"))
        rt.put_object(DeptCls(department_id="D-S1", accounting_entity="ENT-1", name="South Ops", org="plant_south"))

        # RLS on overlaid Department
        assert [d.department_id for d in rt.query_objects(DeptCls, south_actor)] == ["D-S1"]
        north_ids = {d.department_id for d in rt.query_objects(DeptCls, north_actor)}
        assert north_ids == {"D-ROOT", "D-A", "D-B", "D-C"}

        rt.put_object(
            JobPosition(
                position_id="POS-DIR",
                department="D-ROOT",
                title="Director",
                role_name="hr_director",
                approval_ceiling_cents=2_000_000,
            )
        )
        rt.put_object(
            JobPosition(
                position_id="POS-JR",
                department="D-A",
                title="Specialist",
                role_name="hr_admin",
                approval_ceiling_cents=200_000,
            )
        )
        rt.put_object(
            EmpCls(
                employee_id="E-GRANTOR",
                department="D-ROOT",
                position="POS-DIR",
                full_name="Director",
                actor_id="act-hr",
                status="active",
                badge_level="L4",
            )
        )
        rt.put_object(
            EmpCls(
                employee_id="E-LOW",
                department="D-A",
                position="POS-JR",
                full_name="Junior Staff",
                actor_id="act-jr",
                status="active",
                badge_level="L1",
            )
        )
        rt.put_object(
            EmpCls(
                employee_id="E-SENIOR",
                department="D-B",
                position="POS-JR",
                full_name="Senior Lead",
                actor_id="act-sr",
                status="active",
                badge_level="L3",
            )
        )

        # Overlay precondition blocks high ceiling grant to L1 badge
        sim_bad_badge = rt.simulate_action(
            "grant_authority_mandate",
            {
                "mandate_id": "MND-FAIL",
                "grantor_id": "E-GRANTOR",
                "grantee_id": "E-LOW",
                "role_name": "hr_director",
                "ceiling_cents": 800_000,
            },
            hr_dir,
        )
        assert sim_bad_badge.valid is False
        assert sim_bad_badge.error_code == "GRANTEE_BADGE_TOO_LOW"

        # L3 grantee succeeds cleanly & persists active AuthorityMandate
        rt.submit_action(
            "RUN-GRANT-1",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-OK-1",
                "grantor_id": "E-GRANTOR",
                "grantee_id": "E-SENIOR",
                "role_name": "hr_director",
                "ceiling_cents": 800_000,
            },
            hr_dir,
        )
        rt.submit_action(
            "RUN-GRANT-2",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-OK-2",
                "grantor_id": "E-GRANTOR",
                "grantee_id": "E-SENIOR",
                "role_name": "hr_admin",
                "ceiling_cents": 100_000,
            },
            hr_dir,
        )

        # Native Turing-complete while-loop DAG cycle detection STILL intact after overlay!
        sim_cycle = rt.simulate_action(
            "reparent_department",
            {"department_id": "D-ROOT", "new_parent_id": "D-C"},
            admin,
        )
        assert sim_cycle.valid is False
        assert sim_cycle.error_code == "DEPARTMENT_CYCLE_DETECTED"

        # Native Turing-complete for-loop batch revocation in offboard_employee STILL works atomically!
        rt.submit_action(
            "RUN-OFFBOARD-SR",
            "offboard_employee",
            {"employee_id": "E-SENIOR", "reason": "resigned"},
            hr_dir,
        )
        assert rt.get_object(EmpCls, "E-SENIOR", admin).status == "offboarded"
        assert rt.get_object(EmpCls, "E-SENIOR", admin).badge_level == "L3"
        assert rt.get_object(AuthorityMandate, "MND-OK-1", admin).status == "revoked"
        assert rt.get_object(AuthorityMandate, "MND-OK-2", admin).status == "revoked"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. Native std_r2r_spec + apply_overlay_to_spec: Preserves Native @spec.function
# ---------------------------------------------------------------------------
def test_3_native_std_r2r_spec_with_apply_overlay_to_spec_and_functions() -> None:
    overlay = CustomerOverlaySpec(
        add_fields={
            "JournalEntry": [
                FieldSpec(name="audit_ticket", field_type="str", default="", owned=True),
            ]
        },
        add_action_preconditions={
            "post_journal_entry": [
                InvariantRuleSpec(
                    name="high_amount_requires_audit_ticket",
                    code="AUDIT_TICKET_REQUIRED",
                    target_type="JournalEntry",
                    id_param="entry_id",
                    when={
                        "op": "ge",
                        "left": {"ref": "obj.amount_cents"},
                        "right": {"lit": 1_000_000},
                    },
                    must_hold={
                        "op": "ne",
                        "left": {"ref": "obj.audit_ticket"},
                        "right": {"lit": ""},
                    },
                )
            ]
        },
    )

    overlaid = apply_overlay_to_spec(std_r2r_spec, overlay, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    assert "audit_ticket" not in JournalEntry.model_fields
    assert "compute_trial_balance" in overlaid.functions

    JeCls = overlaid.get_object_type("JournalEntry")
    cfo = Actor(actor_id="act-cfo", role="finance_director", clearance_tags={"*"})
    rt = OntologyRuntime(overlaid)
    try:
        rt.put_object(AccountingEntity(entity_id="ENT-R2R", name="Finance HQ"))
        rt.put_object(
            FiscalPeriod(
                period_id="2026-11",
                accounting_entity="ENT-R2R",
                start_date="2026-11-01",
                end_date="2026-11-30",
                status="open",
            )
        )
        rt.put_object(GlAccount(account_code="1001", accounting_entity="ENT-R2R", name="Cash", category="ASSET", normal_side="DEBIT"))
        rt.put_object(GlAccount(account_code="6001", accounting_entity="ENT-R2R", name="Revenue", category="REVENUE", normal_side="CREDIT"))
        rt.put_object(
            JeCls(
                entry_id="JE-NO-TICKET",
                accounting_entity="ENT-R2R",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="6001",
                amount_cents=1_500_000,
                audit_ticket="",
            )
        )
        rt.put_object(
            JeCls(
                entry_id="JE-WITH-TICKET",
                accounting_entity="ENT-R2R",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="6001",
                amount_cents=1_500_000,
                audit_ticket="AUD-2026-99",
            )
        )

        sim_fail = rt.simulate_action("post_journal_entry", {"entry_id": "JE-NO-TICKET"}, cfo)
        assert sim_fail.valid is False
        assert sim_fail.error_code == "AUDIT_TICKET_REQUIRED"

        rt.submit_action("RUN-JE-OK", "post_journal_entry", {"entry_id": "JE-WITH-TICKET"}, cfo)
        tb = rt.call_function("compute_trial_balance", {"accounting_entity": "ENT-R2R"}, cfo)
        assert tb["is_balanced"] is True
        assert tb["accounting_equation_holds"] is True
        assert tb["total_debit_cents"] == 1_500_000
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 4. Monotonic Non-Breaking Contract Red Tests for apply_overlay_to_spec
# ---------------------------------------------------------------------------
def test_4_apply_overlay_to_spec_monotonic_contract_red_tests() -> None:
    bad_cases = [
        # (a) Overwrite standard field / alter standard field weaker
        CustomerOverlaySpec(add_fields={"Employee": [FieldSpec(name="full_name", field_type="str")]}),
        CustomerOverlaySpec(alter_fields={"Employee": [FieldSpec(name="full_name", field_type="str", required=False)]}),
        # (b) Drop FSM edge / reopen terminal state
        CustomerOverlaySpec(update_transitions={"Employee": {"onboarding": []}}),
        CustomerOverlaySpec(update_transitions={"Employee": {"offboarded": ["active"]}}),
        # (c) Overwrite standard invariant
        CustomerOverlaySpec(
            add_invariants=[
                InvariantRuleSpec(
                    name="h2r_department_dag_acyclic",
                    code="FAKE",
                    target_type="Department",
                    must_hold={"op": "eq", "left": {"lit": 1}, "right": {"lit": 1}},
                )
            ]
        ),
        # (d) Redefine standard action via add_actions / update_actions
        CustomerOverlaySpec(add_actions=[ActionBlueprint(name="offboard_employee")]),
        CustomerOverlaySpec(update_actions=[ActionBlueprint(name="offboard_employee", allowed_roles=["admin"])]),
        # (e) Expand or empty allowed_roles
        CustomerOverlaySpec(restrict_allowed_roles={"offboard_employee": ["hr_admin", "shopfloor"]}),
        CustomerOverlaySpec(restrict_allowed_roles={"offboard_employee": []}),
        # (f) Forbidden destructive directives
        CustomerOverlaySpec(remove_fields={"Employee": ["actor_id"]}),
        CustomerOverlaySpec(remove_invariants=["h2r_department_dag_acyclic"]),
    ]
    for ov in bad_cases:
        with pytest.raises(InvariantError) as exc_info:
            apply_overlay_to_spec(std_h2r_spec, ov, seed_ontology=BUSINESS_SEED_ONTOLOGY)
        assert exc_info.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"


# ---------------------------------------------------------------------------
# 5. Shared EditFunctionDef Reused by Two Distinct Governed Actions
# ---------------------------------------------------------------------------
def test_5_single_edit_function_reused_by_multiple_governed_actions() -> None:
    class Wallet(ObjectType):
        __primary_key__ = "wallet_id"
        __owned__ = {"tier_note": ""}
        wallet_id: str
        balance: int = 100
        tier_note: str = ""

    class AdjustParams(BaseModel):
        wallet_id: str
        delta: int
        note: str = ""

    base = OntologySpec()
    base.role("ops")
    base.role("director")
    base.object_type(Wallet)

    @base.edit_function(
        "adjust_wallet_kernel",
        AdjustParams,
        touched_object_types=("Wallet",),
        description="Shared atomic wallet update kernel",
    )
    def adjust_wallet_kernel(ctx, params: AdjustParams) -> None:
        w = ctx.require(Wallet, params.wallet_id)
        ctx.modify(w, balance=w.balance + params.delta, tier_note=params.note or w.tier_note)

    base.declare_action(
        "fast_credit_adjust",
        edit_function="adjust_wallet_kernel",
        allowed_roles=("ops",),
        affects_source=False,
    )
    base.declare_action(
        "vip_credit_adjust",
        edit_function="adjust_wallet_kernel",
        allowed_roles=("director",),
        affects_source=True,
    )

    # Overlay restricts ONLY fast_credit_adjust with a ceiling check; vip_credit_adjust unaffected
    ov = CustomerOverlaySpec(
        add_action_preconditions={
            "fast_credit_adjust": [
                InvariantRuleSpec(
                    name="fast_adjust_ceiling",
                    code="FAST_ADJUST_LIMIT_EXCEEDED",
                    target_type="Wallet",
                    id_param="wallet_id",
                    must_hold={"op": "le", "left": {"ref": "param.delta"}, "right": {"lit": 50}},
                )
            ]
        }
    )
    derived = apply_overlay_to_spec(base, ov)

    assert derived.get_action("fast_credit_adjust").edit_function == "adjust_wallet_kernel"
    assert derived.get_action("vip_credit_adjust").edit_function == "adjust_wallet_kernel"
    assert len(base.get_action("fast_credit_adjust").submission_criteria) == 0
    assert len(derived.get_action("fast_credit_adjust").submission_criteria) == 1
    assert len(derived.get_action("vip_credit_adjust").submission_criteria) == 0

    ops = Actor(actor_id="ops-1", role="ops", clearance_tags={"*"})
    director = Actor(actor_id="dir-1", role="director", clearance_tags={"*"})
    rt = OntologyRuntime(derived)
    try:
        rt.put_object(Wallet(wallet_id="W-1", balance=100))
        sim_over = rt.simulate_action("fast_credit_adjust", {"wallet_id": "W-1", "delta": 200}, ops)
        assert sim_over.valid is False
        assert sim_over.error_code == "FAST_ADJUST_LIMIT_EXCEEDED"

        # Director cannot call ops action (RBAC raises PermissionDeniedError)
        with pytest.raises(PermissionDeniedError):
            rt.simulate_action("fast_credit_adjust", {"wallet_id": "W-1", "delta": 10}, director)

        # Director CAN run vip_credit_adjust with delta=200 via the exact same EditFunctionDef!
        rt.submit_action("RUN-VIP-200", "vip_credit_adjust", {"wallet_id": "W-1", "delta": 200}, director)
        assert rt.get_object(Wallet, "W-1", director).balance == 300
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 6. model_views.describe_behavior + console_server Over Overlaid Spec
# ---------------------------------------------------------------------------
def test_6_describe_behavior_and_console_server_compatibility() -> None:
    overlaid = apply_overlay_to_spec(
        std_r2r_spec,
        CustomerOverlaySpec(
            add_fields={"JournalEntry": [FieldSpec(name="ext_ref", field_type="str", default="")]},
            add_actions=[
                ActionBlueprint(
                    name="tag_journal_rule",
                    allowed_roles=["finance_director", "admin"],
                    params=[
                        FieldSpec(name="entry_id", field_type="str", required=True),
                        FieldSpec(name="ext_ref", field_type="str", required=True),
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="JournalEntry",
                            id_param="entry_id",
                            set_fields={"ext_ref": {"ref": "param.ext_ref"}},
                        )
                    ],
                )
            ],
        ),
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
    )

    behavior = describe_behavior(overlaid)
    by_name = {a["name"]: a for a in behavior["actions"]}
    assert by_name["post_journal_entry"]["implementation_kind"] == "edit_function"
    assert by_name["post_journal_entry"]["edit_function"] == "post_journal_entry"
    assert by_name["tag_journal_rule"]["implementation_kind"] == "rule"
    assert by_name["tag_journal_rule"]["edit_function"] is None

    rt = OntologyRuntime(overlaid)
    cfo = Actor(actor_id="act-cfo", role="finance_director", clearance_tags={"*"})
    svc = ConsoleService(rt, default_actor=cfo)
    try:
        client = TestClient(create_console_app(svc))
        meta = client.get("/api/meta").json()
        assert meta["ok"] is True
        tbox = client.get("/api/tbox").json()
        assert tbox["ok"] is True
        act_map = {a["name"]: a for a in tbox["actions"]}
        assert act_map["post_journal_entry"]["implementation_kind"] == "edit_function"
        assert act_map["tag_journal_rule"]["implementation_kind"] == "rule"
        sbx = client.post("/api/sandboxes").json()
        assert sbx["ok"] is True
    finally:
        svc.close()
        rt.close()
