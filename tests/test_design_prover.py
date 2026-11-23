"""WO-28 Tests: Optional Z3 Formal Verifier (``design prove``) & Multi-Dept Conflict Detection."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any

import pytest

from open_ontology.design import main as design_cli_main, run_design_prove
from open_ontology.induction import (
    PROVER_UNAVAILABLE,
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    ProverUnavailableError,
    RelationSpec,
    apply_overlay,
    prove_action_reachability,
    prove_blueprint,
    prove_invariant_preservation,
    prove_redundant_rules,
    prove_upgrade_compatibility,
)
from open_ontology.induction.prover import _StringCodec, _Z3Compiler

REPO_ROOT = Path(__file__).resolve().parents[1]


def _lit(v: Any) -> dict[str, Any]:
    return {"lit": v}


def _ref(p: str) -> dict[str, Any]:
    return {"ref": p}


def _op(op_name: str, left: Any, right: Any) -> dict[str, Any]:
    return {"op": op_name, "left": left, "right": right}


def _and(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "and", "args": list(args)}


# ---------------------------------------------------------------------------
# 1 & 2. Always-Run Packaging, Lazy Import & Missing-Z3 Tests (No z3 required)
# ---------------------------------------------------------------------------
def test_1_optional_packaging_and_lazy_import_isolation() -> None:
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.is_file()
    parsed = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = parsed["project"]
    deps = [str(d).lower() for d in project.get("dependencies", [])]
    assert all("z3" not in d for d in deps), f"z3 must never be in core dependencies: {deps}"

    opt = project.get("optional-dependencies", {})
    assert "prove" in opt
    assert any("z3-solver" in str(d) for d in opt["prove"])

    req_txt = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "z3" not in req_txt

    # Clean subprocess: import open_ontology loads neither z3 nor prover
    probe_root = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, open_ontology; "
                "assert 'z3' not in sys.modules; "
                "assert 'open_ontology.induction.prover' not in sys.modules; "
                "import open_ontology.induction; "
                "assert 'open_ontology.induction.prover' in sys.modules; "
                "assert 'z3' not in sys.modules"
            ),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe_root.returncode == 0, probe_root.stderr or probe_root.stdout


def test_2_missing_z3_friendly_error_and_cli_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "z3", None)

    minimal_bp = DeclarativeBlueprint(
        model_id="minimal_lazy_probe",
        object_types=[
            ObjectTypeBlueprint(
                name="Widget",
                primary_key="widget_id",
                fields=[
                    FieldSpec(name="widget_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="qty", field_type="int", default=0, non_negative=True),
                ],
            )
        ],
        actions=[
            ActionBlueprint(
                name="adjust_widget",
                allowed_roles=["admin"],
                params=[FieldSpec(name="widget_id", field_type="str", required=True)],
            )
        ],
    )

    with pytest.raises(ProverUnavailableError) as exc_info:
        prove_blueprint(minimal_bp)
    assert exc_info.value.code == PROVER_UNAVAILABLE
    assert "z3-solver" in str(exc_info.value)

    rc = design_cli_main(
        ["prove", "--blueprint", "examples.std_o2c_ontology:spec", "--json"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert PROVER_UNAVAILABLE in payload["error_codes"]


# ---------------------------------------------------------------------------
# 3-10. Z3-Backed Formal Verification Tests (Skipped Gracefully if z3 Absent)
# ---------------------------------------------------------------------------
def _make_commerce_sample_bp() -> DeclarativeBlueprint:
    return DeclarativeBlueprint(
        model_id="sample_commerce",
        roles=["rep", "mgr", "director", "admin"],
        object_types=[
            ObjectTypeBlueprint(
                name="ClientAccount",
                primary_key="acc_id",
                fields=[
                    FieldSpec(name="acc_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="tier", field_type="str", default="STD"),
                    FieldSpec(name="credit_cap", field_type="float", default=10000.0, non_negative=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="SalesContract",
                primary_key="contract_id",
                status_field="status",
                initial_statuses=["draft"],
                transitions={"draft": ["active", "void"], "active": ["closed"], "void": [], "closed": []},
                fields=[
                    FieldSpec(name="contract_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="total_cents", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="rebate_ratio", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="region", field_type="str", default="NORTH"),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(field="account", target_type="ClientAccount", required=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="ReviewTicket",
                primary_key="ticket_id",
                status_field="status",
                initial_statuses=["OPEN"],
                transitions={"OPEN": ["CLOSED"], "CLOSED": []},
                fields=[
                    FieldSpec(name="ticket_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="severity", field_type="int", default=1, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="OPEN"),
                ],
                relations=[
                    RelationSpec(field="account", target_type="ClientAccount", required=True),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="activate_contract_int",
                allowed_roles=["rep", "mgr", "director", "admin"],
                params=[
                    FieldSpec(name="contract_id", field_type="str", required=True),
                    FieldSpec(name="bonus_cents", field_type="int", default=0, non_negative=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="must_be_draft",
                        code="NOT_DRAFT",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_op("eq", _ref("obj.status"), _lit("draft")),
                    ),
                    InvariantRuleSpec(
                        name="positive_int_total",
                        code="NON_POS",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_op("gt", _ref("obj.total_cents"), _lit(0)),
                    ),
                ],
            ),
            ActionBlueprint(
                name="activate_contract_real_div",
                allowed_roles=["rep", "mgr", "director", "admin"],
                params=[
                    FieldSpec(name="contract_id", field_type="str", required=True),
                    FieldSpec(name="units", field_type="int", required=True, non_negative=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="const_div_cap",
                        code="UNIT_CAP",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_op(
                            "le",
                            _op("div", _ref("obj.rebate_ratio"), _lit(2)),
                            _lit(0.25),
                        ),
                    ),
                ],
            ),
            ActionBlueprint(
                name="activate_contract_nonlinear",
                allowed_roles=["rep", "mgr", "director", "admin"],
                params=[
                    FieldSpec(name="contract_id", field_type="str", required=True),
                    FieldSpec(name="multiplier", field_type="float", required=True, non_negative=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="sym_mul_limit",
                        code="NL_LIMIT",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_op(
                            "le",
                            _op("mul", _ref("obj.rebate_ratio"), _ref("param.multiplier")),
                            _lit(0.5),
                        ),
                    ),
                ],
            ),
            ActionBlueprint(
                name="activate_contract_multihop_and_bmc",
                allowed_roles=["mgr", "director", "admin"],
                params=[FieldSpec(name="contract_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="tier_credit_check",
                        code="TIER_CAP",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_and(
                            _op("ne", _ref("obj.account.tier"), _lit("BLACKLISTED")),
                            _op(
                                "le",
                                {
                                    "op": "if_else",
                                    "cond": _op("eq", _ref("obj.account.tier"), _lit("VIP")),
                                    "then": _ref("obj.rebate_ratio"),
                                    "else": _lit(0.05),
                                },
                                _lit(0.3),
                            ),
                            {
                                "op": "le",
                                "left": {
                                    "op": "sum",
                                    "object_type": "ReviewTicket",
                                    "field": "severity",
                                    "where": _op("eq", _ref("target.account"), _ref("obj.account")),
                                },
                                "right": _lit(10),
                            },
                            {
                                "op": "not_exists",
                                "object_type": "ReviewTicket",
                                "where": _and(
                                    _op("eq", _ref("target.account"), _ref("obj.account")),
                                    _op("eq", _ref("target.status"), _lit("OPEN")),
                                    _op("ge", _ref("target.severity"), _lit(5)),
                                ),
                            },
                        ),
                    )
                ],
            ),
        ],
    )


def test_3_ast_compiler_fragments_strings_multihop_nonlinear_and_bmc() -> None:
    z3 = pytest.importorskip("z3")
    bp = _make_commerce_sample_bp()
    reach = prove_action_reachability(bp, bmc_bound=4)
    by_name = {r.action_name: r for r in reach}

    assert by_name["activate_contract_int"].status == "REACHABLE"
    assert by_name["activate_contract_int"].logic_fragment == "EXACT_QF_LIA"

    assert by_name["activate_contract_real_div"].status == "REACHABLE"
    assert by_name["activate_contract_real_div"].logic_fragment == "EXACT_QF_LRA"

    assert by_name["activate_contract_nonlinear"].status == "REACHABLE"
    assert by_name["activate_contract_nonlinear"].logic_fragment == "QF_NIA_HEURISTIC"

    assert by_name["activate_contract_multihop_and_bmc"].status == "REACHABLE"
    assert by_name["activate_contract_multihop_and_bmc"].logic_fragment == "BMC_BOUNDED(k=4)"

    # Finite string domain sentinel encoding allows unseen string values via __oo_other__
    codec = _StringCodec(bp)
    comp = _Z3Compiler(z3, bp, codec, bmc_bound=4)
    c1 = comp.make_scalar_var("s1", "str")
    c2 = comp.make_scalar_var("s2", "str")
    s = z3.Solver()
    for bg in comp.background:
        s.add(bg)
    s.add(c1.expr != c2.expr)
    for lit_str, lit_code in codec.str_to_id.items():
        s.add(c1.expr != lit_code)
    assert s.check() == z3.sat
    mdl = s.model()
    assert codec.decode(mdl.eval(c1.expr, model_completion=True).as_long()).startswith("__oo_other__")

    # Test min / max / count_eq / count_ge / count_le
    count_bp = DeclarativeBlueprint(
        model_id="bmc_quant_ops",
        roles=["admin"],
        object_types=bp.object_types,
        actions=[
            ActionBlueprint(
                name="check_counts_and_extrema",
                allowed_roles=["admin"],
                params=[FieldSpec(name="acc_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="between_1_and_2_tickets",
                        code="CNT_ERR",
                        target_type="ClientAccount",
                        id_param="acc_id",
                        must_hold=_and(
                            {"op": "count_ge", "object_type": "ReviewTicket", "value": 1},
                            {"op": "count_le", "object_type": "ReviewTicket", "value": 2},
                            _op(
                                "ge",
                                {"op": "min", "object_type": "ReviewTicket", "field": "severity"},
                                _lit(2),
                            ),
                            _op(
                                "le",
                                {"op": "max", "object_type": "ReviewTicket", "field": "severity"},
                                _lit(9),
                            ),
                        ),
                    )
                ],
            )
        ],
    )
    c_res = prove_action_reachability(count_bp, bmc_bound=4)
    assert c_res[0].status == "REACHABLE"
    assert c_res[0].logic_fragment == "BMC_BOUNDED(k=4)"


def test_4_reachability_total_and_conditional_deadlock_minimal_unsat_core() -> None:
    pytest.importorskip("z3")
    base = _make_commerce_sample_bp()

    # Global deadlock amidst noise rules -> core pinpoints exact 2 conflicting rules
    noise = [
        InvariantRuleSpec(
            name=f"benign_noise_{i}",
            code=f"BENIGN_{i}",
            target_type="SalesContract",
            id_param="contract_id",
            must_hold=_op("le", _ref("obj.total_cents"), _lit(10_000_000 + i)),
        )
        for i in range(12)
    ]
    total_ov = CustomerOverlaySpec(
        overlay_id="total_lock",
        add_action_preconditions={
            "activate_contract_int": [
                *noise,
                InvariantRuleSpec(
                    name="floor_rule",
                    code="FLOOR",
                    target_type="SalesContract",
                    id_param="contract_id",
                    must_hold=_op("ge", _ref("obj.total_cents"), _lit(50000)),
                ),
                InvariantRuleSpec(
                    name="cap_rule",
                    code="CAP",
                    target_type="SalesContract",
                    id_param="contract_id",
                    must_hold=_op("le", _ref("obj.total_cents"), _lit(20000)),
                ),
            ]
        },
    )
    r_tot = prove_action_reachability(base, overlay=total_ov, action_names=["activate_contract_int"])
    assert r_tot[0].status == "TOTAL_DEADLOCK"
    assert set(r_tot[0].unsat_core) == {"floor_rule", "cap_rule"}

    # 3-way conditional deadlock amidst 25 noise rules -> exact 3-rule unsat_core
    cond_noise = [
        InvariantRuleSpec(
            name=f"cond_noise_{i}",
            code=f"CN_{i}",
            target_type="SalesContract",
            id_param="contract_id",
            when=_op("eq", _ref("obj.region"), _lit(f"REG_{i}")),
            must_hold=_op("in", _ref("actor.role"), _lit(["rep", "mgr", "director", "admin"])),
        )
        for i in range(22)
    ]
    cond_ov = CustomerOverlaySpec(
        overlay_id="cond_3dept_lock",
        add_action_preconditions={
            "activate_contract_int": [
                *cond_noise,
                InvariantRuleSpec(
                    name="dept_a_sales_rule",
                    code="DA",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_op("gt", _ref("obj.total_cents"), _lit(200000)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["rep", "admin"])),
                ),
                InvariantRuleSpec(
                    name="dept_b_fin_rule",
                    code="DB",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_op("gt", _ref("obj.rebate_ratio"), _lit(0.15)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["mgr", "rep"])),
                ),
                InvariantRuleSpec(
                    name="dept_c_risk_rule",
                    code="DC",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_and(
                        _op("eq", _ref("obj.region"), _lit("EU")),
                        _op("gt", _ref("obj.total_cents"), _lit(200000)),
                    ),
                    must_hold=_op("in", _ref("actor.role"), _lit(["mgr", "admin"])),
                ),
            ]
        },
    )
    r_cond = prove_action_reachability(base, overlay=cond_ov, action_names=["activate_contract_int"])
    assert r_cond[0].status == "CONDITIONAL_DEADLOCK"
    assert len(r_cond[0].deadlocked_slices) >= 1
    assert set(r_cond[0].unsat_core) == {
        "dept_a_sales_rule",
        "dept_b_fin_rule",
        "dept_c_risk_rule",
    }

    # Custom slices check
    r_slice = prove_action_reachability(
        base,
        action_names=["activate_contract_int"],
        slices={
            "activate_contract_int": {
                "zero_cents_draft": _and(
                    _op("eq", _ref("obj.status"), _lit("draft")),
                    _op("eq", _ref("obj.total_cents"), _lit(0)),
                )
            }
        },
    )
    assert r_slice[0].status == "CONDITIONAL_DEADLOCK"
    assert r_slice[0].deadlocked_slices[0].slice_name == "zero_cents_draft"
    assert r_slice[0].deadlocked_slices[0].unsat_core == ["positive_int_total"]


def test_5_clean_enterprise_world_and_dual_erp_overlays_zero_false_positives() -> None:
    pytest.importorskip("z3")
    from examples.std_enterprise_world_model import build_enterprise_standard_blueprint

    base_bp = build_enterprise_standard_blueprint()
    odoo_ov = CustomerOverlaySpec.model_validate(
        json.loads((REPO_ROOT / "examples" / "adapters" / "odoo19" / "overlay.json").read_text(encoding="utf-8"))
    )
    jsh_ov = CustomerOverlaySpec.model_validate(
        json.loads((REPO_ROOT / "examples" / "adapters" / "jsherp" / "overlay.json").read_text(encoding="utf-8"))
    )

    rep_base = prove_blueprint(base_bp)
    assert rep_base.ok is True
    assert rep_base.total_deadlock_count == 0
    assert rep_base.conditional_deadlock_count == 0
    assert rep_base.invariant_violation_count == 0
    assert rep_base.redundant_rule_count == 0

    for single_ov in (odoo_ov, jsh_ov):
        rep_single = prove_blueprint(base_bp, overlay=single_ov)
        assert rep_single.ok is True
        assert rep_single.total_deadlock_count == 0
        assert rep_single.conditional_deadlock_count == 0
        assert rep_single.invariant_violation_count == 0

    rep_dual = prove_blueprint(base_bp, overlay=[odoo_ov, jsh_ov])
    assert rep_dual.ok is True
    assert rep_dual.total_deadlock_count == 0
    assert rep_dual.conditional_deadlock_count == 0
    assert rep_dual.invariant_violation_count == 0
    # Dual ERP overlays impose equivalent delivery/receipt checks -> cross-overlay redundancy flagged
    assert rep_dual.redundant_rule_count >= 2


def test_6_redundant_rules_entailment_tautology_vacuous_and_no_deadlock_conflation() -> None:
    pytest.importorskip("z3")
    base = _make_commerce_sample_bp()
    ov = CustomerOverlaySpec(
        overlay_id="redundancy_suite",
        add_action_preconditions={
            "activate_contract_int": [
                InvariantRuleSpec(
                    name="strict_mgr_over_10k",
                    code="STRICT",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_op("gt", _ref("obj.total_cents"), _lit(10000)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["mgr", "director"])),
                ),
                InvariantRuleSpec(
                    name="redundant_over_50k",
                    code="RED",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_op("gt", _ref("obj.total_cents"), _lit(50000)),
                    must_hold=_op("in", _ref("actor.role"), _lit(["mgr", "director", "admin"])),
                ),
                InvariantRuleSpec(
                    name="tautological_pos_bonus",
                    code="TAUT",
                    target_type="SalesContract",
                    id_param="contract_id",
                    must_hold=_op("ge", _ref("param.bonus_cents"), _lit(0)),
                ),
                InvariantRuleSpec(
                    name="vacuous_bad_fsm_state",
                    code="VAC",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_op("eq", _ref("obj.status"), _lit("nonexistent_fsm_state")),
                    must_hold=_op("eq", _ref("actor.role"), _lit("director")),
                ),
            ]
        },
    )
    findings = prove_redundant_rules(base, overlay=ov, action_names=["activate_contract_int"])
    by_rule = {f.rule_name: f for f in findings}
    assert by_rule["redundant_over_50k"].reason == "IMPLIED_BY_OTHER_RULES"
    assert by_rule["redundant_over_50k"].entailed_by == ["strict_mgr_over_10k"]
    assert by_rule["tautological_pos_bonus"].reason == "INTRINSIC_TAUTOLOGY"
    assert by_rule["vacuous_bad_fsm_state"].reason == "VACUOUS_WHEN_GUARD"


def test_7_invariant_preservation_hoare_counterexamples_and_double_entry() -> None:
    pytest.importorskip("z3")

    def _make_ledger_bp(*, safe_debit: bool, credit_both_sides: bool) -> DeclarativeBlueprint:
        sub_pres = [
            InvariantRuleSpec(
                name="pos_delta",
                code="NON_POS",
                target_type="LedgerAccount",
                id_param="debit_acc",
                must_hold=_op("gt", _ref("param.delta_cents"), _lit(0)),
            )
        ]
        if safe_debit:
            sub_pres.append(
                InvariantRuleSpec(
                    name="sufficient_balance",
                    code="INSUFF_BAL",
                    target_type="LedgerAccount",
                    id_param="debit_acc",
                    must_hold=_op("ge", _ref("obj.balance_cents"), _ref("param.delta_cents")),
                )
            )
        muts = [
            ActionMutationSpec(
                op="modify",
                object_type="LedgerAccount",
                id_param="debit_acc",
                set_fields={
                    "balance_cents": _op("sub", _ref("obj.balance_cents"), _ref("param.delta_cents")),
                    "debit_cents": _op("add", _ref("obj.debit_cents"), _ref("param.delta_cents")),
                },
            )
        ]
        if credit_both_sides:
            muts.append(
                ActionMutationSpec(
                    op="modify",
                    object_type="LedgerAccount",
                    id_param="credit_acc",
                    set_fields={
                        "credit_cents": _op("add", _ref("obj.credit_cents"), _ref("param.delta_cents")),
                    },
                )
            )
        return DeclarativeBlueprint(
            model_id="ledger_hoare",
            roles=["accountant", "admin"],
            object_types=[
                ObjectTypeBlueprint(
                    name="LedgerAccount",
                    primary_key="acc_code",
                    fields=[
                        FieldSpec(name="acc_code", field_type="str", primary_key=True, required=True),
                        FieldSpec(name="balance_cents", field_type="int", default=0, non_negative=True),
                        FieldSpec(name="debit_cents", field_type="int", default=0, non_negative=True),
                        FieldSpec(name="credit_cents", field_type="int", default=0, non_negative=True),
                    ],
                )
            ],
            invariants=[
                InvariantRuleSpec(
                    name="double_entry_total_balance",
                    code="UNBALANCED",
                    target_type="LedgerAccount",
                    must_hold=_op(
                        "eq",
                        {"op": "sum", "object_type": "LedgerAccount", "field": "debit_cents"},
                        {"op": "sum", "object_type": "LedgerAccount", "field": "credit_cents"},
                    ),
                )
            ],
            actions=[
                ActionBlueprint(
                    name="transfer_entry",
                    allowed_roles=["accountant", "admin"],
                    params=[
                        FieldSpec(name="debit_acc", field_type="str", required=True),
                        FieldSpec(name="credit_acc", field_type="str", required=True),
                        FieldSpec(name="delta_cents", field_type="int", required=True, non_negative=True),
                    ],
                    preconditions=sub_pres,
                    mutations=muts,
                )
            ],
        )

    bad_rep = prove_invariant_preservation(_make_ledger_bp(safe_debit=False, credit_both_sides=False))
    assert bad_rep[0].status == "VIOLATED"
    assert "LedgerAccount.balance_cents_non_negative" in bad_rep[0].violated_invariants
    assert "double_entry_total_balance" in bad_rep[0].violated_invariants
    ce_bal = next(c for c in bad_rep[0].counterexamples if c.violated_invariant == "LedgerAccount.balance_cents_non_negative")
    assert ce_bal.violated_code == "NEGATIVE_VALUE"
    assert ce_bal.params["delta_cents"] > ce_bal.before["LedgerAccount:debit_acc"]["balance_cents"]

    good_rep = prove_invariant_preservation(_make_ledger_bp(safe_debit=True, credit_both_sides=True))
    assert good_rep[0].status == "PROVED"
    assert good_rep[0].violated_invariants == []


def test_8_upgrade_compatibility_v1_to_v2() -> None:
    pytest.importorskip("z3")
    base_v1 = _make_commerce_sample_bp()
    cust_ov = CustomerOverlaySpec(
        overlay_id="fast_track_vip",
        add_action_preconditions={
            "activate_contract_int": [
                InvariantRuleSpec(
                    name="vip_auto_fast_track",
                    code="VIP_FAST_TRACK",
                    target_type="SalesContract",
                    id_param="contract_id",
                    when=_op("eq", _ref("obj.region"), _lit("US_EXPRESS")),
                    must_hold=_op("le", _ref("obj.total_cents"), _lit(5000)),
                )
            ]
        },
    )
    base_v2_breaking = apply_overlay(
        base_v1,
        CustomerOverlaySpec(
            overlay_id="v2_high_floor",
            target_version="2.0.0",
            add_action_preconditions={
                "activate_contract_int": [
                    InvariantRuleSpec(
                        name="v2_std_min_amount",
                        code="V2_MIN",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_op("ge", _ref("obj.total_cents"), _lit(10000)),
                    )
                ]
            },
        ),
    )
    base_v2_compat = apply_overlay(
        base_v1,
        CustomerOverlaySpec(
            overlay_id="v2_small_floor",
            target_version="2.0.0",
            add_action_preconditions={
                "activate_contract_int": [
                    InvariantRuleSpec(
                        name="v2_std_min_amount",
                        code="V2_MIN",
                        target_type="SalesContract",
                        id_param="contract_id",
                        must_hold=_op("ge", _ref("obj.total_cents"), _lit(100)),
                    )
                ]
            },
        ),
    )

    rep_bad = prove_upgrade_compatibility(base_v1, base_v2_breaking, cust_ov)
    assert rep_bad.compatible is False
    assert any(r.regression_kind == "NEW_CONDITIONAL_DEADLOCK" for r in rep_bad.regressions)

    rep_ok = prove_upgrade_compatibility(base_v1, base_v2_compat, cust_ov)
    assert rep_ok.compatible is True
    assert rep_ok.regressions == []


def test_9_design_prove_cli_multi_overlay_exit_codes_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("z3")

    # 9a. Clean multi-overlay enterprise CLI run -> exit 0
    rc_ok = design_cli_main(
        [
            "prove",
            "--blueprint",
            "examples.std_enterprise_world_model:build_enterprise_standard_blueprint",
            "--overlay",
            str(REPO_ROOT / "examples" / "adapters" / "odoo19" / "overlay.json"),
            "--overlay",
            str(REPO_ROOT / "examples" / "adapters" / "jsherp" / "overlay.json"),
            "--json",
        ]
    )
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["ok"] is True
    assert out_ok["exit_code"] == 0
    assert out_ok["report"]["total_deadlock_count"] == 0

    # 9b. Conflicting multi-department overlays -> exit 2 + human-readable unsat_core
    ov_a = tmp_path / "sales_ov.json"
    ov_b = tmp_path / "finance_ov.json"
    ov_a.write_text(
        json.dumps(
            {
                "overlay_id": "sales_ov",
                "add_action_preconditions": {
                    "confirm_o2c_order": [
                        {
                            "name": "cli_sales_guard",
                            "code": "CLI_SALES",
                            "target_type": "O2cOrder",
                            "id_param": "order_id",
                            "when": {"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 50000}},
                            "must_hold": {"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["sales_manager"]}},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    ov_b.write_text(
        json.dumps(
            {
                "overlay_id": "finance_ov",
                "add_action_preconditions": {
                    "confirm_o2c_order": [
                        {
                            "name": "cli_finance_guard",
                            "code": "CLI_FIN",
                            "target_type": "O2cOrder",
                            "id_param": "order_id",
                            "when": {"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 50000}},
                            "must_hold": {"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["finance_director"]}},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    rc_fail = design_cli_main(
        [
            "prove",
            "--blueprint",
            "examples.std_o2c_ontology:spec",
            "--overlay",
            str(ov_a),
            "--overlay",
            str(ov_b),
        ]
    )
    assert rc_fail == 2
    human_out = capsys.readouterr().out
    assert "CONDITIONAL_DEADLOCK" in human_out
    assert "cli_sales_guard" in human_out
    assert "cli_finance_guard" in human_out

    # 9c. Missing file -> exit 1
    res_err = run_design_prove(blueprints=["/nonexistent_wo28_bp.json"])
    assert res_err["ok"] is False
    assert res_err["exit_code"] == 1


def test_10_wo28_benchmark_core_assertions(tmp_path: Path) -> None:
    pytest.importorskip("z3")
    from examples.wo28_enterprise_rule_prover_benchmark import run_benchmark

    bench_path = tmp_path / "wo28_bench.json"
    rep = run_benchmark(fuzz_samples=200, output_path=bench_path)
    assert bench_path.is_file()

    z3_m = rep["overall_metrics"]["z3_smt"]
    assert z3_m["precision_pct"] == 100.0
    assert z3_m["recall_pct"] == 100.0
    assert z3_m["fpr_pct"] == 0.0

    ladder_100 = next(t for t in rep["scale_ladder"] if t["rule_count"] == 100)
    assert ladder_100["z3_recall_pct"] == 100.0
    assert ladder_100["z3_fpr_pct"] == 0.0
    assert ladder_100["root_cause_reduction_pct"] >= 95.0

    frags = rep["logic_fragment_distribution"]
    assert "EXACT_QF_LIA" in frags
    assert "EXACT_QF_LRA" in frags
    assert any(k.startswith("BMC_BOUNDED") for k in frags)
