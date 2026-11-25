"""WO-36: DOAL PO-6 (`when` branch exhaustiveness/disjointness), PO-7 (workflow FSM liveness + Z3 multi-step
action terminal reachability), M1 Expert contract visibility fixes, and DOAL v1.2 Gate 6 / Gate 8
specification conformance test suite.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal, get_args

import pytest

from open_ontology.console_server import ConsoleService
from open_ontology.induction import (
    ALLOWED_EXPR_OPS,
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    RelationSpec,
    WhenBranchProof,
    WorkflowLivenessProof,
    apply_overlay_to_spec,
    compile_blueprint,
    export_spec_blueprint,
    prove_blueprint,
    prove_when_branches,
    prove_workflow_liveness,
    validate_predicate_ast,
)
from open_ontology.induction.dsl import (
    AGGREGATE_OPS,
    ARITHMETIC_OPS,
    COMPARISON_OPS,
    LOGICAL_OPS,
    QUANTIFIER_OPS,
)
from open_ontology.induction.prover import ProverUnavailableError
from open_ontology.runtime import OntologyRuntime
from open_ontology.spec import Actor, InvariantError, OntologySpec

REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    import z3 as _z3_check  # type: ignore[import-not-found]
    HAS_Z3 = _z3_check is not None
except ImportError:
    HAS_Z3 = False

requires_z3 = pytest.mark.skipif(not HAS_Z3, reason="z3-solver optional dependency not installed")


# ===========================================================================
# Helpers for synthetic red/green blueprints
# ===========================================================================
def _make_ticket_bp(
    *,
    mutations: list[ActionMutationSpec],
    preconditions: list[InvariantRuleSpec] | None = None,
    transitions: dict[str, list[str]] | None = None,
    initial_statuses: list[str] | None = None,
) -> DeclarativeBlueprint:
    return DeclarativeBlueprint(
        model_id="wo36_test_model",
        version="1.0.0",
        roles=["operator"],
        object_types=[
            ObjectTypeBlueprint(
                name="Order",
                primary_key="order_id",
                fields=[
                    FieldSpec(name="order_id", field_type="str", required=True, primary_key=True),
                    FieldSpec(name="status", field_type="str", default="draft", required=True),
                    FieldSpec(name="amount", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="tier", field_type="str", default="LOW"),
                    FieldSpec(name="locked", field_type="bool", default=False),
                ],
                initial_statuses=initial_statuses if initial_statuses is not None else ["draft"],
                transitions=transitions
                if transitions is not None
                else {
                    "draft": ["confirmed"],
                    "confirmed": ["closed"],
                    "closed": [],
                },
            )
        ],
        actions=[
            ActionBlueprint(
                name="classify_order",
                allowed_roles=["operator"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                preconditions=preconditions or [],
                mutations=mutations,
            )
        ],
    )


# ===========================================================================
# 1. PO-6: Guarded `when` Branch Exhaustiveness, Reachability & Disjointness
# ===========================================================================
@requires_z3
class TestPO6WhenBranches:
    def test_exhaustive_partition_proved_clean(self) -> None:
        bp = _make_ticket_bp(
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 500}},
                    set_fields={"tier": {"lit": "HIGH"}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "le", "left": {"ref": "obj.amount"}, "right": {"lit": 500}},
                    set_fields={"tier": {"lit": "LOW"}},
                ),
            ]
        )
        proofs = prove_when_branches(bp)
        assert len(proofs) == 1
        assert isinstance(proofs[0], WhenBranchProof)
        assert proofs[0].status == "EXHAUSTIVE"
        assert proofs[0].branch_count == 2
        assert proofs[0].dead_branch_indices == []
        assert proofs[0].overlapping_pairs == []
        assert proofs[0].uncovered_witness is None

    def test_incomplete_when_branches_caught_with_counterexample(self) -> None:
        # Missing middle range [200, 500]
        bp = _make_ticket_bp(
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 500}},
                    set_fields={"tier": {"lit": "HIGH"}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "lt", "left": {"ref": "obj.amount"}, "right": {"lit": 200}},
                    set_fields={"tier": {"lit": "LOW"}},
                ),
            ]
        )
        proofs = prove_when_branches(bp)
        assert len(proofs) == 1
        assert proofs[0].status == "INCOMPLETE_WHEN_BRANCHES"
        assert proofs[0].uncovered_witness is not None
        obj_state = next(iter(proofs[0].uncovered_witness["objects"].values()))
        assert 200 <= obj_state["amount"] <= 500

        # Full report must fail `ok`
        rep = prove_blueprint(bp)
        assert rep.ok is False
        assert rep.when_branch_issue_count == 1

    def test_dead_branch_caught_under_precondition(self) -> None:
        bp = _make_ticket_bp(
            preconditions=[
                InvariantRuleSpec(
                    name="amount_at_least_100",
                    code="AMOUNT_TOO_SMALL",
                    target_type="Order",
                    id_param="order_id",
                    must_hold={"op": "ge", "left": {"ref": "obj.amount"}, "right": {"lit": 100}},
                )
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "ge", "left": {"ref": "obj.amount"}, "right": {"lit": 100}},
                    set_fields={"tier": {"lit": "HIGH"}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "lt", "left": {"ref": "obj.amount"}, "right": {"lit": 50}},
                    set_fields={"tier": {"lit": "LOW"}},
                ),
            ],
        )
        proofs = prove_when_branches(bp)
        assert proofs[0].status == "DEAD_BRANCH"
        assert proofs[0].dead_branch_indices == [1]
        assert prove_blueprint(bp).ok is False

    def test_overlapping_branches_caught_with_witness(self) -> None:
        # Both >= 200 and <= 500 fire for amount in [200, 500] and both write `tier`
        bp = _make_ticket_bp(
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "ge", "left": {"ref": "obj.amount"}, "right": {"lit": 200}},
                    set_fields={"tier": {"lit": "HIGH"}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "le", "left": {"ref": "obj.amount"}, "right": {"lit": 500}},
                    set_fields={"tier": {"lit": "LOW"}},
                ),
            ]
        )
        proofs = prove_when_branches(bp)
        assert proofs[0].status == "OVERLAPPING_WHEN_BRANCHES"
        assert proofs[0].overlapping_pairs == [(0, 1)]
        assert proofs[0].overlap_witness is not None
        obj_state = next(iter(proofs[0].overlap_witness["objects"].values()))
        assert 200 <= obj_state["amount"] <= 500
        assert prove_blueprint(bp).ok is False

    def test_single_conditional_side_effect_alongside_unconditional_mutation_passes(self) -> None:
        bp = _make_ticket_bp(
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    set_fields={"locked": {"lit": True}},
                ),
                ActionMutationSpec(
                    op="modify",
                    object_type="Order",
                    id_param="order_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 1000}},
                    set_fields={"tier": {"lit": "HIGH"}},
                ),
            ]
        )
        proofs = prove_when_branches(bp)
        assert len(proofs) == 1
        assert proofs[0].status == "CONDITIONAL_SIDE_EFFECT"


# ===========================================================================
# 2. PO-7: Workflow FSM Structural Liveness & Z3 Multi-Step Terminal Reachability
# ===========================================================================
@requires_z3
class TestPO7WorkflowLiveness:
    def test_trap_cycle_caught(self) -> None:
        bp = _make_ticket_bp(
            mutations=[],
            transitions={
                "draft": ["review", "closed"],
                "review": ["rework"],
                "rework": ["review"],  # inescapable livelock cycle
                "closed": [],
            },
            initial_statuses=["draft"],
        )
        wfs = prove_workflow_liveness(bp)
        assert len(wfs) == 1
        assert isinstance(wfs[0], WorkflowLivenessProof)
        assert wfs[0].status == "TRAP_STATE"
        assert set(wfs[0].trap_states) == {"review", "rework"}
        assert prove_blueprint(bp).ok is False

    def test_unreachable_orphan_state_caught(self) -> None:
        bp = _make_ticket_bp(
            mutations=[],
            transitions={
                "draft": ["closed"],
                "closed": [],
                "ghost_state": ["closed"],  # unreachable from draft
            },
            initial_statuses=["draft"],
        )
        wfs = prove_workflow_liveness(bp)
        assert wfs[0].status == "UNREACHABLE_STATE"
        assert wfs[0].unreachable_states == ["ghost_state"]
        assert prove_blueprint(bp).ok is False

    def test_no_terminal_state_caught(self) -> None:
        bp = _make_ticket_bp(
            mutations=[],
            transitions={
                "draft": ["confirmed"],
                "confirmed": ["draft"],
            },
            initial_statuses=["draft"],
        )
        wfs = prove_workflow_liveness(bp)
        assert wfs[0].status == "NO_TERMINAL_STATE"
        assert prove_blueprint(bp).ok is False

    def test_cross_step_data_deadlock_caught_by_z3_multistep_chaining(self) -> None:
        """Each action is individually PO-3 reachable, AND FSM graph is live,
        YET step1 sets locked=True while step2 requires locked=False -> ACTION_CHAIN_DEADLOCKED!
        """
        bp = DeclarativeBlueprint(
            model_id="wo36_chain_deadlock",
            version="1.0.0",
            roles=["operator"],
            object_types=[
                ObjectTypeBlueprint(
                    name="Order",
                    primary_key="order_id",
                    fields=[
                        FieldSpec(name="order_id", field_type="str", required=True, primary_key=True),
                        FieldSpec(name="status", field_type="str", default="draft", required=True),
                        FieldSpec(name="locked", field_type="bool", default=False),
                    ],
                    initial_statuses=["draft"],
                    transitions={
                        "draft": ["confirmed"],
                        "confirmed": ["closed"],
                        "closed": [],
                    },
                )
            ],
            actions=[
                ActionBlueprint(
                    name="confirm_order",
                    allowed_roles=["operator"],
                    params=[FieldSpec(name="order_id", field_type="str", required=True)],
                    preconditions=[
                        InvariantRuleSpec(
                            name="must_be_draft",
                            code="NOT_DRAFT",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                        )
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Order",
                            id_param="order_id",
                            set_fields={
                                "status": {"lit": "confirmed"},
                                "locked": {"lit": True},  # Poisons step 2!
                            },
                        )
                    ],
                ),
                ActionBlueprint(
                    name="close_order",
                    allowed_roles=["operator"],
                    params=[FieldSpec(name="order_id", field_type="str", required=True)],
                    preconditions=[
                        InvariantRuleSpec(
                            name="must_be_confirmed",
                            code="NOT_CONFIRMED",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "confirmed"}},
                        ),
                        InvariantRuleSpec(
                            name="must_not_be_locked",
                            code="ORDER_LOCKED",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.locked"}, "right": {"lit": False}},
                        ),
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Order",
                            id_param="order_id",
                            set_fields={"status": {"lit": "closed"}},
                        )
                    ],
                ),
            ],
        )
        rep = prove_blueprint(bp)
        # Notice: BOTH individual actions are reachable in isolation (PO-2 passes)!
        assert all(r.status == "REACHABLE" for r in rep.reachability)
        # YET PO-7 Z3 multi-step composition catches the cross-step data deadlock!
        assert rep.ok is False
        assert rep.workflow_issue_count == 1
        assert rep.workflow_liveness_proofs[0].status == "ACTION_CHAIN_DEADLOCKED"
        assert rep.workflow_liveness_proofs[0].action_terminal_status == "ACTION_CHAIN_DEADLOCKED"

    def test_prep_action_unblocks_multistep_workflow_in_z3(self) -> None:
        """Adding an unlock prep action restores a 3-step witness trace: confirm -> unlock -> close!"""
        bp = DeclarativeBlueprint(
            model_id="wo36_chain_unblocked",
            version="1.0.0",
            roles=["operator"],
            object_types=[
                ObjectTypeBlueprint(
                    name="Order",
                    primary_key="order_id",
                    fields=[
                        FieldSpec(name="order_id", field_type="str", required=True, primary_key=True),
                        FieldSpec(name="status", field_type="str", default="draft", required=True),
                        FieldSpec(name="locked", field_type="bool", default=False),
                    ],
                    initial_statuses=["draft"],
                    transitions={
                        "draft": ["confirmed"],
                        "confirmed": ["closed"],
                        "closed": [],
                    },
                )
            ],
            actions=[
                ActionBlueprint(
                    name="confirm_order",
                    allowed_roles=["operator"],
                    params=[FieldSpec(name="order_id", field_type="str", required=True)],
                    preconditions=[
                        InvariantRuleSpec(
                            name="must_be_draft",
                            code="NOT_DRAFT",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                        )
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Order",
                            id_param="order_id",
                            set_fields={"status": {"lit": "confirmed"}, "locked": {"lit": True}},
                        )
                    ],
                ),
                ActionBlueprint(
                    name="unlock_order",
                    allowed_roles=["operator"],
                    params=[FieldSpec(name="order_id", field_type="str", required=True)],
                    preconditions=[
                        InvariantRuleSpec(
                            name="must_be_locked",
                            code="NOT_LOCKED",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.locked"}, "right": {"lit": True}},
                        )
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Order",
                            id_param="order_id",
                            set_fields={"locked": {"lit": False}},
                        )
                    ],
                ),
                ActionBlueprint(
                    name="close_order",
                    allowed_roles=["operator"],
                    params=[FieldSpec(name="order_id", field_type="str", required=True)],
                    preconditions=[
                        InvariantRuleSpec(
                            name="must_be_confirmed",
                            code="NOT_CONFIRMED",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "confirmed"}},
                        ),
                        InvariantRuleSpec(
                            name="must_not_be_locked",
                            code="ORDER_LOCKED",
                            target_type="Order",
                            id_param="order_id",
                            must_hold={"op": "eq", "left": {"ref": "obj.locked"}, "right": {"lit": False}},
                        ),
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Order",
                            id_param="order_id",
                            set_fields={"status": {"lit": "closed"}},
                        )
                    ],
                ),
            ],
        )
        rep = prove_blueprint(bp)
        assert rep.ok is True
        wf = rep.workflow_liveness_proofs[0]
        assert wf.status == "LIVE"
        assert wf.action_terminal_status == "ACTION_TERMINAL_REACHABLE"
        assert wf.terminal_witness_trace == ["confirm_order", "unlock_order", "close_order"]
        assert wf.reached_terminal_status == "closed"


# ===========================================================================
# 3. Full Real Models Sanity (Enterprise / MedAuto / Starship) + M1 Expert Fixes
# ===========================================================================
@requires_z3
class TestRealModelsAndM1ExpertFixes:
    def test_all_three_live_models_pass_po1_through_po7(self) -> None:
        from examples.enterprise_console_app import build_starship_console
        from examples.medauto_export_extension import build_medauto_spec
        from examples.std_enterprise_world_model import build_enterprise_standard_spec

        # 1) Enterprise Standard World Model
        ent_rep = prove_blueprint(build_enterprise_standard_spec(), check_redundancy=True)
        assert ent_rep.ok is True
        assert ent_rep.when_branch_checked_count >= 1
        assert ent_rep.when_branch_issue_count == 0
        assert ent_rep.workflow_checked_count == 19
        assert ent_rep.workflow_issue_count == 0
        assert ent_rep.workflow_action_verified_count >= 12

        # 2) MedAuto Export Manufacturing Showcase (36 classes / 43 actions / 25 FSMs)
        med_rep = prove_blueprint(build_medauto_spec(), check_redundancy=True)
        assert med_rep.ok is True
        assert med_rep.exact_wp_proved_count == 29
        assert med_rep.contract_proved_count == 14
        assert med_rep.vacuous_contract_count == 0
        assert med_rep.when_branch_checked_count == 3
        assert med_rep.when_branch_issue_count == 0
        when_by_name = {w.action_name: w for w in med_rep.when_branch_proofs}
        assert when_by_name["submit_first_article_inspection"].status == "EXHAUSTIVE"
        assert when_by_name["classify_export_risk_tier"].status == "EXHAUSTIVE"
        assert when_by_name["resolve_p2p_dispute"].status == "CONDITIONAL_SIDE_EFFECT"

        assert med_rep.workflow_checked_count == 25
        assert med_rep.workflow_live_count == 25
        assert med_rep.workflow_issue_count == 0
        wf_by_name = {w.object_type: w for w in med_rep.workflow_liveness_proofs}
        # Subtype pruned inherited states are tolerated & transparently recorded
        assert wf_by_name["ExportSalesOrder"].status == "LIVE"
        assert wf_by_name["ExportSalesOrder"].pruned_inherited_states == ["invoiced"]
        assert wf_by_name["ExportSalesOrder"].terminal_witness_trace == [
            "confirm_o2c_order",
            "complete_o2c_delivery",
        ]
        # Mixed Rule -> EditFunction Contract -> Rule 3-step witness!
        assert wf_by_name["ReworkWorkOrder"].status == "LIVE"
        assert wf_by_name["ReworkWorkOrder"].pruned_inherited_states == ["cancelled"]
        assert wf_by_name["ReworkWorkOrder"].terminal_witness_trace == [
            "release_work_order",
            "issue_material_to_work_order",
            "close_work_order",
        ]
        assert wf_by_name["ReworkWorkOrder"].reached_terminal_status == "completed"

        # 3) Starship Pure L1
        st_res = build_starship_console()
        st_spec = st_res.runtime.spec if hasattr(st_res, "runtime") else st_res[1].spec
        st_rep = prove_blueprint(st_spec, check_redundancy=True)
        assert st_rep.ok is True
        assert st_rep.workflow_checked_count == 1
        assert st_rep.workflow_issue_count == 0

    def test_m1_expert_inspect_exposes_editfn_contracts_and_prove_filters_contract_proved(
        self, tmp_path: Path
    ) -> None:
        from examples.medauto_export_extension import build_medauto_spec

        spec = build_medauto_spec()
        rt = OntologyRuntime(spec, db_path=tmp_path / "medauto.db")
        try:
            svc = ConsoleService(rt, pack_hint="none")
            actor = Actor(actor_id="u_admin", role="admin")
            draft = svc._create_m1_draft(actor)
            draft_id = draft.draft_id

            # 1) inspect_m1_blueprint MUST expose EditFunction contracts
            trace: list[dict] = []
            mut_flag = [False]
            inspected = svc._execute_m1_expert_tool(
                "inspect_m1_blueprint", {"include_draft": True}, actor, draft_id, trace, mut_flag
            )
            assert inspected["ok"] is True
            rebate_act = next(
                a for a in inspected["actions"] if a["name"] == "apply_export_tax_rebate_claim"
            )
            assert rebate_act["implementation_kind"] == "edit_function"
            assert "contract" in rebate_act
            assert "CustomsDeclaration.rebate_amount_cents" in rebate_act["contract"]["modifies"]
            assert len(rebate_act["contract"]["ensures"]) == 1

            # 2) prove_draft_mathematics MUST NOT leak passing CONTRACT_PROVED items as failures
            proved = svc._execute_m1_expert_tool(
                "prove_draft_mathematics", {}, actor, draft_id, trace, mut_flag
            )
            assert proved["ok"] is True
            assert proved["verdict"] == "PROVEN_CLEAN"
            assert proved["invariant_proofs"] == []
            assert proved["when_branch_proofs"] == []
            assert proved["workflow_liveness_proofs"] == []
            assert proved["summary"]["when_branch_checked_count"] == 3
            assert proved["summary"]["workflow_checked_count"] == 25
            assert proved["summary"]["workflow_action_verified_count"] >= 17
        finally:
            rt.close()


# ===========================================================================
# 4. DOAL v1.2 Gate 6 / Gate 8 / Spec Conformance Suite
# ===========================================================================
class TestDOALv12ConformanceGates:
    def test_gate6_pure_runtime_imports_never_load_dsl_prover_or_z3(self) -> None:
        code = (
            "import sys, open_ontology, open_ontology.spec, open_ontology.runtime\n"
            "forbidden = [m for m in ('open_ontology.induction.dsl', 'open_ontology.induction.prover', 'z3') if m in sys.modules]\n"
            "assert not forbidden, f'Gate 6 violated: loaded {forbidden}'\n"
            "print('GATE6_CLEAN')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "GATE6_CLEAN" in proc.stdout

    def test_gate8_operator_lock_and_forbidden_turing_primitives(self) -> None:
        # Exact 23-operator lock matching docs/DOAL_SPEC.md Section 2.3
        expected_logical = frozenset({"and", "or", "not"})
        expected_comparison = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in", "not_in"})
        expected_quantifier = frozenset({"exists", "not_exists", "count_eq", "count_ge", "count_le"})
        expected_arithmetic = frozenset({"add", "sub", "mul", "div"})
        expected_aggregate = frozenset({"sum", "min", "max"})
        expected_all = (
            expected_logical
            | expected_comparison
            | expected_quantifier
            | expected_arithmetic
            | expected_aggregate
        )

        assert LOGICAL_OPS == expected_logical
        assert COMPARISON_OPS == expected_comparison
        assert QUANTIFIER_OPS == expected_quantifier
        assert ARITHMETIC_OPS == expected_arithmetic
        assert AGGREGATE_OPS == expected_aggregate
        assert ALLOWED_EXPR_OPS == expected_all
        assert len(ALLOWED_EXPR_OPS) == 23

        # Conditional value `if_else` is accepted inside predicate ASTs
        validate_predicate_ast(
            {
                "op": "eq",
                "left": {
                    "op": "if_else",
                    "cond": {"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 0}},
                    "then": {"lit": "POS"},
                    "else": {"lit": "ZERO"},
                },
                "right": {"lit": "POS"},
            },
            allowed_roots={"obj"},
        )

        # S2 Anti-xtUML permanent blacklist MUST be rejected everywhere
        forbidden_ops = (
            "while",
            "loop",
            "for",
            "for_each",
            "foreach",
            "modify_where",
            "delete_where",
            "recurse",
            "exec",
            "eval",
            "call_python",
            "lambda",
        )
        for bad_op in forbidden_ops:
            assert bad_op not in ALLOWED_EXPR_OPS
            with pytest.raises((InvariantError, ValueError)):
                validate_predicate_ast(
                    {"op": bad_op, "left": {"lit": 1}, "right": {"lit": 2}},
                    allowed_roots={"obj"},
                )
            with pytest.raises(Exception):
                ActionMutationSpec(op=bad_op, object_type="Order", id_param="order_id")  # type: ignore[arg-type]

        # ActionMutationSpec.op strictly locked to create / modify / delete
        op_ann = ActionMutationSpec.model_fields["op"].annotation
        assert set(get_args(op_ann)) == {"create", "modify", "delete"}

        # OntologySpec.action permanently retired
        assert not hasattr(OntologySpec, "action")

        # S1 mutual exclusion: edit_function + non-empty mutations rejected
        with pytest.raises(Exception):
            ActionBlueprint(
                name="bad_hybrid",
                allowed_roles=["admin"],
                edit_function="some_ef",
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Order",
                        id_param="order_id",
                        set_fields={"status": {"lit": "closed"}},
                    )
                ],
            )

        # extra="forbid" enforced across all core DOAL Pydantic models
        for model_cls in (
            DeclarativeBlueprint,
            ObjectTypeBlueprint,
            FieldSpec,
            RelationSpec,
            InvariantRuleSpec,
            ActionMutationSpec,
            ActionBlueprint,
            CustomerOverlaySpec,
            WhenBranchProof,
            WorkflowLivenessProof,
        ):
            assert model_cls.model_config.get("extra") == "forbid", f"{model_cls.__name__} missing extra=forbid"

    def test_zero_exec_eval_compile_in_dsl_and_prover_ast(self) -> None:
        banned_calls = {"exec", "eval", "compile"}
        for rel_path in ("open_ontology/induction/dsl.py", "open_ontology/induction/prover.py"):
            src = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=rel_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in banned_calls, (
                        f"Forbidden dynamic code call `{node.func.id}()` found in {rel_path}:{node.lineno}"
                    )

    def test_graceful_degradation_when_z3_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import open_ontology.induction.prover as prover_mod

        bp = _make_ticket_bp(mutations=[])
        # Compilation works 100% cleanly without Z3
        spec = compile_blueprint(bp)
        assert "Order" in spec.object_types

        # Prover raises ProverUnavailableError cleanly when z3 is absent
        monkeypatch.setitem(sys.modules, "z3", None)
        with pytest.raises(ProverUnavailableError) as exc_info:
            prover_mod.prove_blueprint(bp)
        assert exc_info.value.code == "PROVER_UNAVAILABLE"

    def test_docs_doal_spec_section_8_2_json_example_validates_and_proves_live(self) -> None:
        from examples.std_enterprise_world_model import build_enterprise_standard_spec

        spec_doc = REPO_ROOT / "docs" / "DOAL_SPEC.md"
        assert spec_doc.exists(), "docs/DOAL_SPEC.md must exist in repository"
        text = spec_doc.read_text(encoding="utf-8")
        json_blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
        assert len(json_blocks) >= 2, "Expected Section 8.2 Overlay JSON and Section 9.2 WriteBatchProposal JSON"

        # Section 8.2 Overlay JSON MUST validate, apply onto std enterprise spec, and Z3 prove cleanly!
        overlay_data = json.loads(json_blocks[0])
        overlay = CustomerOverlaySpec.model_validate(overlay_data)
        merged_spec = apply_overlay_to_spec(build_enterprise_standard_spec(), overlay)
        assert "ExportSalesOrder" in merged_spec.object_types
        if HAS_Z3:
            report = prove_blueprint(merged_spec, check_redundancy=True)
            assert report.ok is True
            assert report.when_branch_issue_count == 0
            assert report.workflow_issue_count == 0
            classify_wp = next(
                w for w in report.when_branch_proofs if w.action_name == "classify_export_order_risk"
            )
            assert classify_wp.status == "EXHAUSTIVE"

        # Section 9.2 M2.5 WriteBatchProposal example MUST parse as valid JSON with Read-Set CAS keys
        proposal_data = json.loads(json_blocks[1])
        assert proposal_data["contract_name"] == "allocate_container_shipping_cost"
        assert len(proposal_data["read_set"]) >= 1
        assert all("expected_version" in r for r in proposal_data["read_set"])
        assert all(op["op"] in ("create", "modify", "delete") for op in proposal_data["ops"])
