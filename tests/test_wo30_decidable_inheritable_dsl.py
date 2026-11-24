"""WO-30 Verification Suite: Decidable Inheritable Ontology & Action DSL.

Covers:
1. Monotonic ``extends`` behavioral subtyping, CWA-DL structural axioms, and
   lossless round-trip across ``compose_blueprints``, ``apply_overlay``,
   ``compile_blueprint``, ``apply_overlay_to_spec``, and ``export_spec_blueprint``.
2. Strict negative LSP & cycle/DL guards (field removal, PK change, field
   weakening, widened FSM, relaxed cardinality, cycles, disjoint ancestors).
3. Optional Z3 CWA-DL proofs: dead/unsatisfiable class detection + minimal
   UNSAT core, DL disjointness conflicts, cardinality bound + dead-target
   propagation conflicts, LSP violations, and satisfiable class witnesses.
4. Guarded finite-branch mutations (``ActionMutationSpec.when``) runtime execution
   and exact Hoare weakest-precondition preservation/counterexample proofs.
5. Console ``/api/blueprint`` & ``/api/blueprint/prove``, ``design prove`` CLI,
   and Py3.11 zero-optional-dependency ``PROVER_UNAVAILABLE`` fallback.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel
import pytest

from open_ontology import (
    Actor,
    InvariantError,
    InvariantView,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
)
from open_ontology.console_server import ConsoleService, create_console_app
from open_ontology.design import main as design_cli_main, run_design_prove
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    ClassSatisfiabilityProof,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    RelationSpec,
    apply_overlay,
    apply_overlay_to_spec,
    compile_blueprint,
    compose_blueprints,
    export_spec_blueprint,
    prove_blueprint,
    prove_class_satisfiability,
    prove_invariant_preservation,
)
from examples.enterprise_console_app import build_console_service, build_starship_console


def _has_z3() -> bool:
    return importlib.util.find_spec("z3") is not None


def _lit(v: Any) -> dict[str, Any]:
    return {"lit": v}


def _ref(p: str) -> dict[str, Any]:
    return {"ref": p}


def _op(op_name: str, left: Any, right: Any) -> dict[str, Any]:
    return {"op": op_name, "left": left, "right": right}


def _and(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": "and", "args": list(args)}


def _not(arg: dict[str, Any]) -> dict[str, Any]:
    return {"op": "not", "arg": arg}


def _make_inheritable_base_and_subtype_bp(
    *,
    sub_discount_floor: int | None = None,
    sub_discount_cap: int | None = 10,
    sub_fsm_transitions: dict[str, list[str]] | None = None,
    disjoint_types: list[tuple[str, str]] = (),
    rel_min: int = 2,
    rel_max: int | None = 3,
    include_extra_benign_parent_inv: bool = True,
) -> DeclarativeBlueprint:
    base_invs = [
        InvariantRuleSpec(
            name="discount_cap",
            code="DISCOUNT_TOO_HIGH",
            target_type="BaseOrder",
            must_hold=_op("le", _ref("obj.discount"), _lit(20)),
        )
    ]
    if include_extra_benign_parent_inv:
        base_invs.append(
            InvariantRuleSpec(
                name="amount_positive_or_zero",
                code="NEG_AMOUNT",
                target_type="BaseOrder",
                must_hold=_op("ge", _ref("obj.amount"), _lit(0)),
            )
        )
    if sub_discount_cap is not None:
        base_invs.append(
            InvariantRuleSpec(
                name="sub_discount_cap",
                code="VIP_CAP",
                target_type="VipOrder",
                must_hold=_op("le", _ref("obj.discount"), _lit(sub_discount_cap)),
            )
        )
    if sub_discount_floor is not None:
        base_invs.append(
            InvariantRuleSpec(
                name="discount_floor",
                code="VIP_FLOOR",
                target_type="VipOrder",
                must_hold=_op("ge", _ref("obj.discount"), _lit(sub_discount_floor)),
            )
        )

    return DeclarativeBlueprint(
        model_id="inheritable_demo",
        roles=["ops", "admin"],
        disjoint_types=list(disjoint_types),
        object_types=[
            ObjectTypeBlueprint(
                name="ChannelTag",
                primary_key="tag_id",
                fields=[
                    FieldSpec(name="tag_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="label", field_type="str", required=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="BaseOrder",
                primary_key="order_id",
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="amount", field_type="int", default=100, non_negative=True),
                    FieldSpec(name="discount", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(
                        field="tags",
                        target_type="ChannelTag",
                        multiple=True,
                        min_cardinality=1,
                        max_cardinality=5,
                    )
                ],
                status_field="status",
                initial_statuses=["draft", "queued"],
                transitions={
                    "draft": ["confirmed", "cancelled"],
                    "queued": ["confirmed"],
                    "confirmed": [],
                    "cancelled": [],
                },
            ),
            ObjectTypeBlueprint(
                name="VipOrder",
                extends="BaseOrder",
                fields=[
                    FieldSpec(name="vip_tier", field_type="str", default="GOLD"),
                ],
                relations=[
                    RelationSpec(
                        field="tags",
                        target_type="ChannelTag",
                        multiple=True,
                        min_cardinality=rel_min,
                        max_cardinality=rel_max,
                    )
                ],
                initial_statuses=["draft"],
                transitions=sub_fsm_transitions
                if sub_fsm_transitions is not None
                else {
                    "draft": ["confirmed"],
                    "confirmed": [],
                },
            ),
        ],
        invariants=base_invs,
        actions=[
            ActionBlueprint(
                name="create_vip_order",
                allowed_roles=["ops", "admin"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="VipOrder",
                        id_param="order_id",
                        set_fields={
                            "discount": _lit(5),
                            "tags": _lit(["T1", "T2"]),
                            "status": _lit("draft"),
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="set_vip_discount",
                allowed_roles=["ops", "admin"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="discount", field_type="int", required=True, non_negative=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="bounded_discount",
                        code="BAD_DISCOUNT",
                        target_type="VipOrder",
                        id_param="order_id",
                        must_hold=_op("le", _ref("param.discount"), _lit(10)),
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="VipOrder",
                        id_param="order_id",
                        set_fields={"discount": _ref("param.discount")},
                    )
                ],
            ),
            ActionBlueprint(
                name="set_vip_status",
                allowed_roles=["ops", "admin"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="next_status", field_type="str", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="valid_confirm",
                        code="INVALID_STATUS_STEP",
                        target_type="VipOrder",
                        id_param="order_id",
                        must_hold=_and(
                            _op("eq", _ref("obj.status"), _lit("draft")),
                            _op("eq", _ref("param.next_status"), _lit("confirmed")),
                        ),
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="VipOrder",
                        id_param="order_id",
                        set_fields={"status": _ref("param.next_status")},
                    )
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 1. Monotonic `extends` Subtyping & Runtime Semantics (Py3.11 & Py3.13)
# ---------------------------------------------------------------------------
def test_1_declarative_extends_inheritance_compose_compile_and_roundtrip() -> None:
    # Out-of-order declaration still resolves cleanly via topological sort
    bp_part_base = DeclarativeBlueprint(
        model_id="part_base",
        roles=["ops"],
        object_types=[
            ObjectTypeBlueprint(
                name="ChannelTag",
                primary_key="tag_id",
                fields=[FieldSpec(name="tag_id", field_type="str", primary_key=True, required=True)],
            ),
            ObjectTypeBlueprint(
                name="BaseOrder",
                primary_key="order_id",
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="discount", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(
                        field="tags",
                        target_type="ChannelTag",
                        multiple=True,
                        min_cardinality=1,
                        max_cardinality=5,
                    )
                ],
                initial_statuses=["draft", "queued"],
                transitions={"draft": ["confirmed", "cancelled"], "queued": ["confirmed"], "confirmed": [], "cancelled": []},
            ),
        ],
        invariants=[
            InvariantRuleSpec(
                name="discount_cap",
                code="DISCOUNT_TOO_HIGH",
                target_type="BaseOrder",
                must_hold=_op("le", _ref("obj.discount"), _lit(20)),
            )
        ],
    )
    bp_part_sub = DeclarativeBlueprint(
        model_id="part_sub",
        roles=["admin"],
        disjoint_types=[("VipOrder", "ChannelTag")],
        object_types=[
            ObjectTypeBlueprint(
                name="VipOrder",
                extends="BaseOrder",
                fields=[FieldSpec(name="vip_tier", field_type="str", default="GOLD")],
                relations=[
                    RelationSpec(
                        field="tags",
                        target_type="ChannelTag",
                        multiple=True,
                        min_cardinality=2,
                        max_cardinality=3,
                    )
                ],
                initial_statuses=["draft"],
                transitions={"draft": ["confirmed"], "confirmed": []},
            )
        ],
        actions=[
            *_make_inheritable_base_and_subtype_bp().actions,
            ActionBlueprint(
                name="force_vip_status",
                allowed_roles=["ops"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="next_status", field_type="str", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="VipOrder",
                        id_param="order_id",
                        set_fields={"status": _ref("param.next_status")},
                    )
                ],
            ),
            ActionBlueprint(
                name="try_create_under_card",
                allowed_roles=["ops"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="VipOrder",
                        id_param="order_id",
                        set_fields={
                            "discount": _lit(5),
                            "tags": _lit(["T1"]),
                            "status": _lit("draft"),
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="bypass_cap_on_sub",
                allowed_roles=["ops"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="VipOrder",
                        id_param="order_id",
                        set_fields={"discount": _lit(25)},
                    )
                ],
            ),
        ],
    )
    composed = compose_blueprints([bp_part_sub, bp_part_base], model_id="composed_inherit")
    assert ("VipOrder", "ChannelTag") in composed.disjoint_types

    spec = compile_blueprint(composed)
    base_cls = spec.get_object_type("BaseOrder")
    vip_cls = spec.get_object_type("VipOrder")
    tag_cls = spec.get_object_type("ChannelTag")
    assert issubclass(vip_cls, base_cls)
    assert vip_cls.primary_key_name() == "order_id"

    vip_inst = vip_cls(order_id="VO-1", discount=10, tags=("T1", "T2"))
    assert isinstance(vip_inst, base_cls)

    rt = OntologyRuntime(spec)
    actor = Actor(actor_id="u-ops", role="ops", clearance_tags={"*"})
    try:
        for t in (tag_cls(tag_id="T1"), tag_cls(tag_id="T2"), tag_cls(tag_id="T3")):
            rt.put_object(t, actor=actor)
        rt.put_object(vip_cls(order_id="VO-OK", discount=10, tags=("T1", "T2"), status="draft"), actor=actor)

        # Narrowed FSM: draft -> confirmed allowed, draft -> cancelled rejected on VipOrder
        with pytest.raises(InvariantError) as exc_fsm:
            rt.submit_action(
                "r-bad-fsm",
                "force_vip_status",
                {"order_id": "VO-OK", "next_status": "cancelled"},
                actor,
            )
        assert exc_fsm.value.code == "ILLEGAL_STATE_TRANSITION"

        # Runtime cardinality enforcement on VipOrder: min=2 violated when 1 tag passed
        with pytest.raises(InvariantError) as exc_card:
            rt.submit_action("r-bad-card", "try_create_under_card", {"order_id": "VO-BAD-CARD"}, actor)
        assert exc_card.value.code == "CARDINALITY_VIOLATION"

        # Parent declarative invariant discount_cap inherited by VipOrder at runtime
        with pytest.raises(InvariantError) as exc_inh:
            rt.submit_action("r-bad-cap", "bypass_cap_on_sub", {"order_id": "VO-OK"}, actor)
        assert exc_inh.value.code == "DISCOUNT_TOO_HIGH"

        # Allowed transition commits cleanly
        ok_res = rt.submit_action(
            "r-ok-fsm",
            "force_vip_status",
            {"order_id": "VO-OK", "next_status": "confirmed"},
            actor,
        )
        assert ok_res["status"] == "COMMITTED"

        # Lossless export_spec_blueprint round-trip
        exported = export_spec_blueprint(spec, model_id="exported_inherit")
        exp_by_name = {o.name: o for o in exported.object_types}
        assert exp_by_name["VipOrder"].extends == "BaseOrder"
        exp_rel = next(r for r in exp_by_name["VipOrder"].relations if r.field == "tags")
        assert (exp_rel.min_cardinality, exp_rel.max_cardinality) == (2, 3)
        assert ("VipOrder", "ChannelTag") in exported.disjoint_types
    finally:
        rt.close()


def test_2_apply_overlay_to_spec_derive_object_types_and_python_invariant_inheritance() -> None:
    class AccountPartner(ObjectType):
        __primary_key__ = "partner_id"
        partner_id: str
        name: str

    class ParentAccount(ObjectType):
        __primary_key__ = "acc_id"
        __status_field__ = "status"
        __initial_statuses__ = ("open", "hold")
        __transitions__ = {"open": ("hold", "closed"), "hold": ("open", "closed"), "closed": ()}
        acc_id: str
        credit_limit: int = 1000
        status: str = "open"

    class UpdateLimitParams(BaseModel):
        acc_id: str
        limit: int

    base_spec = OntologySpec()
    base_spec.role("admin")
    base_spec.object_type(AccountPartner)
    base_spec.object_type(ParentAccount)

    @base_spec.invariant("parent_py_ceiling", code="PY_CEIL")
    def _check_parent_ceiling(view: InvariantView) -> bool:
        for row in view.query(ParentAccount):
            if row.credit_limit > 10000:
                raise InvariantError("credit_limit exceeds 10000", code="PY_CEIL")
        return True

    @base_spec.edit_function("update_acc_limit", UpdateLimitParams)
    def _upd_limit(ctx, params: UpdateLimitParams) -> None:
        acc = ctx.require(ParentAccount, params.acc_id)
        ctx.modify(acc, credit_limit=params.limit)

    base_spec.declare_action("update_acc_limit", edit_function="update_acc_limit", allowed_roles=("admin",))

    overlay = CustomerOverlaySpec(
        overlay_id="regional_ext",
        derive_object_types=[
            ObjectTypeBlueprint(
                name="RegionalAccount",
                extends="ParentAccount",
                fields=[FieldSpec(name="region_code", field_type="str", default="CN")],
                initial_statuses=["open"],
                transitions={"open": ["closed"], "closed": []},
            )
        ],
        add_disjoint_types=[("RegionalAccount", "AccountPartner")],
        add_actions=[
            ActionBlueprint(
                name="set_regional_limit",
                allowed_roles=["admin"],
                params=[
                    FieldSpec(name="acc_id", field_type="str", required=True),
                    FieldSpec(name="limit", field_type="int", required=True, non_negative=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="RegionalAccount",
                        id_param="acc_id",
                        set_fields={"credit_limit": _ref("param.limit")},
                    )
                ],
            )
        ],
    )

    derived_spec = apply_overlay_to_spec(base_spec, overlay)
    reg_cls = derived_spec.get_object_type("RegionalAccount")
    assert issubclass(reg_cls, ParentAccount)
    reg_inst = reg_cls(acc_id="RA-1", credit_limit=5000, region_code="EAST")
    assert isinstance(reg_inst, ParentAccount)

    rt = OntologyRuntime(derived_spec)
    admin = Actor(actor_id="u-adm", role="admin", clearance_tags={"*"})
    try:
        rt.put_object(reg_inst, actor=admin)
        # Inherited Python @spec.invariant catches violation on subtype!
        with pytest.raises(InvariantError) as exc_ceil:
            rt.submit_action("r-bad", "set_regional_limit", {"acc_id": "RA-1", "limit": 20000}, admin)
        assert exc_ceil.value.code == "PY_CEIL"

        ok_res = rt.submit_action("r-ok", "set_regional_limit", {"acc_id": "RA-1", "limit": 9000}, admin)
        assert ok_res["status"] == "COMMITTED"
    finally:
        rt.close()


def test_3_negative_lsp_violations_cycles_and_dl_disjoint_guards() -> None:
    base_bp = _make_inheritable_base_and_subtype_bp()

    def _with_sub(**sub_kwargs: Any) -> DeclarativeBlueprint:
        bp = DeclarativeBlueprint(
            model_id="neg_lsp",
            roles=["ops", "admin"],
            object_types=[
                base_bp.object_types[0],
                base_bp.object_types[1],
                ObjectTypeBlueprint(name="BadSub", extends="BaseOrder", **sub_kwargs),
            ],
        )
        return bp

    # 3a. exclude_fields forbidden
    with pytest.raises(InvariantError) as e_excl:
        compile_blueprint(_with_sub(exclude_fields=["discount"]))
    assert e_excl.value.code == "LSP_VIOLATION"

    # 3b. PK change forbidden
    with pytest.raises(InvariantError) as e_pk:
        compile_blueprint(_with_sub(primary_key="sub_id"))
    assert e_pk.value.code == "LSP_VIOLATION"

    # 3c. Field type / non_negative / required weakening forbidden
    with pytest.raises(InvariantError) as e_ftype:
        compile_blueprint(_with_sub(fields=[FieldSpec(name="discount", field_type="float")]))
    assert e_ftype.value.code == "LSP_VIOLATION"

    with pytest.raises(InvariantError) as e_fnn:
        compile_blueprint(
            _with_sub(fields=[FieldSpec(name="discount", field_type="int", non_negative=False)])
        )
    assert e_fnn.value.code == "LSP_VIOLATION"

    # 3d. Widened FSM forbidden (new state or disallowed edge)
    with pytest.raises(InvariantError) as e_fsm:
        compile_blueprint(
            _with_sub(transitions={"draft": ["confirmed", "hacked"], "confirmed": [], "hacked": []})
        )
    assert e_fsm.value.code == "LSP_VIOLATION"

    # 3e. Relaxed relation cardinality forbidden
    with pytest.raises(InvariantError) as e_rel_min:
        compile_blueprint(
            _with_sub(
                relations=[
                    RelationSpec(
                        field="tags", target_type="ChannelTag", multiple=True, min_cardinality=0, max_cardinality=5
                    )
                ]
            )
        )
    assert e_rel_min.value.code == "LSP_VIOLATION"

    with pytest.raises(InvariantError) as e_rel_max:
        compile_blueprint(
            _with_sub(
                relations=[
                    RelationSpec(
                        field="tags", target_type="ChannelTag", multiple=True, min_cardinality=1, max_cardinality=9
                    )
                ]
            )
        )
    assert e_rel_max.value.code == "LSP_VIOLATION"

    # 3f. Inheritance cycles (self & mutual)
    with pytest.raises(InvariantError) as e_cyc:
        compile_blueprint(
            DeclarativeBlueprint(
                model_id="cyc",
                object_types=[
                    ObjectTypeBlueprint(name="NodeA", extends="NodeB"),
                    ObjectTypeBlueprint(name="NodeB", extends="NodeA"),
                ],
            )
        )
    assert e_cyc.value.code == "INHERITANCE_CYCLE"

    # 3g. DL disjoint violation
    with pytest.raises(InvariantError) as e_disj:
        compile_blueprint(
            DeclarativeBlueprint(
                model_id="disj",
                disjoint_types=[("BaseOrder", "BadSub")],
                object_types=[
                    base_bp.object_types[0],
                    base_bp.object_types[1],
                    ObjectTypeBlueprint(name="BadSub", extends="BaseOrder"),
                ],
            )
        )
    assert e_disj.value.code == "DL_DISJOINT_VIOLATION"

    # 3h. Contradictory local cardinality
    with pytest.raises(InvariantError) as e_card:
        compile_blueprint(
            DeclarativeBlueprint(
                model_id="bad_card",
                object_types=[
                    ObjectTypeBlueprint(name="TargetT"),
                    ObjectTypeBlueprint(
                        name="SourceT",
                        relations=[
                            RelationSpec(
                                field="refs",
                                target_type="TargetT",
                                multiple=True,
                                min_cardinality=3,
                                max_cardinality=1,
                            )
                        ],
                    ),
                ],
            )
        )
    assert e_card.value.code == "DL_CARDINALITY_CONFLICT"

    # 3i. derive_object_types without extends & remove_fields via overlay
    with pytest.raises(InvariantError) as e_no_ext:
        apply_overlay(base_bp, CustomerOverlaySpec(derive_object_types=[ObjectTypeBlueprint(name="Orphan")]))
    assert e_no_ext.value.code == "LSP_VIOLATION"

    with pytest.raises(InvariantError) as e_rm:
        apply_overlay(base_bp, CustomerOverlaySpec(remove_fields={"BaseOrder": ["discount"]}))
    assert e_rm.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"


# ---------------------------------------------------------------------------
# 2. Z3 CWA-DL Formal Class, Disjointness, Cardinality & LSP Proofs (Py3.13)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _has_z3(), reason="z3-solver not installed")
def test_4_z3_dead_class_minimal_unsat_core_and_satisfiable_witness() -> None:
    # Dead class: parent discount <= 20, subtype discount >= 30 (+ benign amount >= 0)
    dead_bp = _make_inheritable_base_and_subtype_bp(
        sub_discount_floor=30,
        sub_discount_cap=None,
        include_extra_benign_parent_inv=True,
    )
    rep_dead = prove_blueprint(dead_bp)
    assert rep_dead.ok is False
    assert rep_dead.dead_class_count == 1
    assert rep_dead.dl_conflict_count == 0
    assert rep_dead.lsp_violation_count == 0

    by_cls = {cp.object_type: cp for cp in rep_dead.class_proofs}
    assert by_cls["BaseOrder"].status == "SATISFIABLE"
    assert by_cls["BaseOrder"].witness is not None

    vip_cp = by_cls["VipOrder"]
    assert isinstance(vip_cp, ClassSatisfiabilityProof)
    assert vip_cp.extends == "BaseOrder"
    assert vip_cp.status == "UNSATISFIABLE_CLASS"
    # Minimal 2-rule core: benign 'amount_positive_or_zero' excluded!
    assert set(vip_cp.unsat_core) == {"discount_cap", "discount_floor"}
    assert "amount_positive_or_zero" not in vip_cp.unsat_core

    # Valid monotonic subtype: parent discount <= 20, subtype 0 <= discount <= 10 -> SATISFIABLE
    good_bp = _make_inheritable_base_and_subtype_bp(
        sub_discount_floor=0,
        sub_discount_cap=10,
    )
    rep_good = prove_blueprint(good_bp)
    assert rep_good.ok is True
    assert rep_good.dead_class_count == 0
    good_by_cls = {cp.object_type: cp for cp in rep_good.class_proofs}
    assert good_by_cls["VipOrder"].status == "SATISFIABLE"
    w = good_by_cls["VipOrder"].witness
    assert w is not None and 0 <= w["discount"] <= 10


@pytest.mark.skipif(not _has_z3(), reason="z3-solver not installed")
def test_5_z3_dl_disjointness_cardinality_propagation_and_lsp_proofs() -> None:
    # 5a. DL Disjointness conflict on raw blueprint
    disj_bp = _make_inheritable_base_and_subtype_bp(
        disjoint_types=[("BaseOrder", "VipOrder")],
        sub_discount_floor=0,
    )
    rep_disj = prove_blueprint(disj_bp)
    assert rep_disj.ok is False
    assert rep_disj.dl_conflict_count >= 1
    disj_by_cls = {c.object_type: c for c in rep_disj.class_proofs}
    assert disj_by_cls["VipOrder"].status == "DL_AXIOM_CONFLICT"

    # 5b. Direct min > max cardinality conflict + required relation targeting dead class
    card_prop_bp = DeclarativeBlueprint(
        model_id="card_prop",
        object_types=[
            ObjectTypeBlueprint(
                name="DeadTarget",
                fields=[FieldSpec(name="id", field_type="str", primary_key=True, required=True), FieldSpec(name="v", field_type="int")],
            ),
            ObjectTypeBlueprint(
                name="HealthyDependent",
                fields=[FieldSpec(name="id", field_type="str", primary_key=True, required=True)],
                relations=[
                    RelationSpec(
                        field="dead_ref",
                        target_type="DeadTarget",
                        multiple=False,
                        required=True,
                        min_cardinality=1,
                        max_cardinality=1,
                    )
                ],
            ),
            ObjectTypeBlueprint(
                name="ContradictBounds",
                fields=[FieldSpec(name="id", field_type="str", primary_key=True, required=True)],
                relations=[
                    RelationSpec(
                        field="multi",
                        target_type="DeadTarget",
                        multiple=True,
                        min_cardinality=3,
                        max_cardinality=1,
                    )
                ],
            ),
        ],
        invariants=[
            InvariantRuleSpec(
                name="dead_gt_10",
                code="C1",
                target_type="DeadTarget",
                must_hold=_op("gt", _ref("obj.v"), _lit(10)),
            ),
            InvariantRuleSpec(
                name="dead_lt_5",
                code="C2",
                target_type="DeadTarget",
                must_hold=_op("lt", _ref("obj.v"), _lit(5)),
            ),
        ],
    )
    rep_card = prove_blueprint(card_prop_bp)
    assert rep_card.ok is False
    c_map = {c.object_type: c for c in rep_card.class_proofs}
    assert c_map["DeadTarget"].status == "UNSATISFIABLE_CLASS"
    assert c_map["ContradictBounds"].status == "DL_AXIOM_CONFLICT"
    assert c_map["HealthyDependent"].status == "DL_AXIOM_CONFLICT"

    # 5c. LSP Violation detected in prover (widened FSM transition)
    lsp_bp = _make_inheritable_base_and_subtype_bp(
        sub_fsm_transitions={"draft": ["confirmed", "bypassed"], "confirmed": [], "bypassed": []},
        sub_discount_floor=0,
    )
    rep_lsp = prove_blueprint(lsp_bp)
    assert rep_lsp.ok is False
    assert rep_lsp.lsp_violation_count == 1
    assert {c.object_type: c for c in rep_lsp.class_proofs}["VipOrder"].status == "LSP_VIOLATION"

    # 5d. Overlay introducing dead class into base blueprint
    base_only = _make_inheritable_base_and_subtype_bp(sub_discount_floor=0)
    bad_ov = CustomerOverlaySpec(
        add_invariants=[
            InvariantRuleSpec(
                name="impossible_ov_floor",
                code="IMP",
                target_type="VipOrder",
                must_hold=_op("ge", _ref("obj.discount"), _lit(25)),
            )
        ]
    )
    ov_proofs = prove_class_satisfiability(base_only, overlay=bad_ov)
    assert {c.object_type: c for c in ov_proofs}["VipOrder"].status == "UNSATISFIABLE_CLASS"


# ---------------------------------------------------------------------------
# 3. Guarded Finite-Branch (`ActionMutationSpec.when`) Runtime + Hoare `wp`
# ---------------------------------------------------------------------------
def _make_guarded_branch_bp(*, buggy_express_branch: bool = False) -> DeclarativeBlueprint:
    deduct_expr = (
        _op("sub", _op("sub", _ref("obj.balance"), _ref("param.amt")), _lit(10))
        if buggy_express_branch
        else _op("sub", _ref("obj.balance"), _ref("param.amt"))
    )
    return DeclarativeBlueprint(
        model_id="guarded_branch_demo",
        roles=["ops"],
        object_types=[
            ObjectTypeBlueprint(
                name="Wallet",
                primary_key="wallet_id",
                fields=[
                    FieldSpec(name="wallet_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="balance", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="points", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="active"),
                ],
                initial_statuses=["active"],
                transitions={"active": ["active", "hold"], "hold": ["active"]},
            ),
            ObjectTypeBlueprint(
                name="BonusTicket",
                primary_key="ticket_id",
                fields=[
                    FieldSpec(name="ticket_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="wallet_id", field_type="str", required=True),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="guarded_charge",
                allowed_roles=["ops"],
                params=[
                    FieldSpec(name="wallet_id", field_type="str", required=True),
                    FieldSpec(name="amt", field_type="int", required=True, non_negative=True),
                    FieldSpec(name="express", field_type="bool", default=False),
                    FieldSpec(name="ticket_id", field_type="str", default="TK-1"),
                ],
                mutations=[
                    # Branch 1: express=True & balance >= amt -> deduct + create ticket
                    ActionMutationSpec(
                        op="modify",
                        object_type="Wallet",
                        id_param="wallet_id",
                        when=_and(
                            _op("eq", _ref("param.express"), _lit(True)),
                            _op("ge", _ref("obj.balance"), _ref("param.amt")),
                        ),
                        set_fields={
                            "balance": deduct_expr,
                            "points": _op("add", _ref("obj.points"), _lit(5)),
                        },
                    ),
                    ActionMutationSpec(
                        op="create",
                        object_type="BonusTicket",
                        id_param="ticket_id",
                        when=_op("eq", _ref("param.express"), _lit(True)),
                        set_fields={"wallet_id": _ref("param.wallet_id")},
                    ),
                    # Branch 2: complementary express=False -> only modify if balance >= amt, else hold
                    ActionMutationSpec(
                        op="modify",
                        object_type="Wallet",
                        id_param="wallet_id",
                        when=_and(
                            _not(_op("eq", _ref("param.express"), _lit(True))),
                            _op("lt", _ref("obj.balance"), _ref("param.amt")),
                        ),
                        set_fields={"status": _lit("hold")},
                    ),
                ],
            )
        ],
    )


def test_6_guarded_finite_branch_runtime_execution_no_z3() -> None:
    bp = _make_guarded_branch_bp(buggy_express_branch=False)
    spec = compile_blueprint(bp)
    wallet_cls = spec.get_object_type("Wallet")
    ticket_cls = spec.get_object_type("BonusTicket")
    rt = OntologyRuntime(spec)
    actor = Actor(actor_id="u-ops", role="ops", clearance_tags={"*"})
    try:
        rt.put_object(wallet_cls(wallet_id="W-1", balance=100, points=0, status="active"), actor=actor)

        # Non-express with amt > balance -> takes Branch 2 (status="hold"), skips Branch 1 & BonusTicket create
        r_hold = rt.submit_action(
            "r-hold",
            "guarded_charge",
            {"wallet_id": "W-1", "amt": 150, "express": False, "ticket_id": "TK-NO"},
            actor,
        )
        assert r_hold["status"] == "COMMITTED"
        w1 = rt.get_object(wallet_cls, "W-1", actor)
        assert w1.status == "hold"
        assert w1.balance == 100
        assert rt.query_objects(ticket_cls, actor) == []

        # Express with amt <= balance -> takes Branch 1 & creates BonusTicket
        r_exp = rt.submit_action(
            "r-exp",
            "guarded_charge",
            {"wallet_id": "W-1", "amt": 40, "express": True, "ticket_id": "TK-YES"},
            actor,
        )
        assert r_exp["status"] == "COMMITTED"
        w1_after = rt.get_object(wallet_cls, "W-1", actor)
        assert w1_after.balance == 60
        assert w1_after.points == 5
        tickets = rt.query_objects(ticket_cls, actor)
        assert len(tickets) == 1 and tickets[0].ticket_id == "TK-YES"
    finally:
        rt.close()


@pytest.mark.skipif(not _has_z3(), reason="z3-solver not installed")
def test_7_guarded_finite_branch_z3_hoare_positive_negative_and_inherited_invariant() -> None:
    # 7a. Sound guarded branching preserves non-negativity & FSM on ALL branches in EXACT_QF_LIA
    good_pres = prove_invariant_preservation(_make_guarded_branch_bp(buggy_express_branch=False))
    assert len(good_pres) == 1
    assert good_pres[0].status == "PROVED"
    assert good_pres[0].logic_fragment == "EXACT_QF_LIA"
    assert good_pres[0].violated_invariants == []

    # 7b. Buggy express branch violates non_negative on ONLY express=True branch
    bad_pres = prove_invariant_preservation(_make_guarded_branch_bp(buggy_express_branch=True))
    assert bad_pres[0].status == "VIOLATED"
    assert "Wallet.balance_non_negative" in bad_pres[0].violated_invariants
    ce = bad_pres[0].counterexamples[0]
    assert ce.params["express"] is True

    # 7c. Subtype action mutating VipOrder violates inherited Parent invariant (discount_cap)
    sub_hoare_bp = _make_inheritable_base_and_subtype_bp(sub_discount_floor=0, sub_discount_cap=None)
    sub_hoare_bp.actions = [
        ActionBlueprint(
            name="unguarded_vip_bump",
            allowed_roles=["ops"],
            params=[
                FieldSpec(name="order_id", field_type="str", required=True),
                FieldSpec(name="delta", field_type="int", required=True, non_negative=True),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="VipOrder",
                    id_param="order_id",
                    set_fields={"discount": _op("add", _ref("obj.discount"), _ref("param.delta"))},
                )
            ],
        )
    ]
    sub_res = prove_invariant_preservation(sub_hoare_bp)
    assert sub_res[0].status == "VIOLATED"
    assert "discount_cap" in sub_res[0].violated_invariants


# ---------------------------------------------------------------------------
# 4. Console API, `design prove` CLI & Py3.11 Fallback
# ---------------------------------------------------------------------------
def test_8_console_blueprint_prove_cli_and_py311_fallback(tmp_path: Path) -> None:
    # 8a. Enterprise & Starship /api/blueprint + /api/blueprint/prove include class_proofs & disjoint_types
    for svc in (
        build_console_service(mode="standalone", demo_actor_switch=True),
        build_starship_console(demo_actor_switch=True),
    ):
        try:
            client = TestClient(create_console_app(svc))
            bp_json = client.get("/api/blueprint").json()
            assert "disjoint_types" in bp_json
            assert all("extends" in o for o in bp_json["object_types"])

            prove_json = client.post("/api/blueprint/prove", json={}).json()
            assert prove_json["ok"] is True
            if _has_z3():
                assert prove_json["prover_available"] is True
                assert prove_json["verdict"] == "PROVEN_CLEAN"
                assert prove_json["summary"]["dead_class_count"] == 0
                assert prove_json["summary"]["dl_conflict_count"] == 0
                assert prove_json["summary"]["lsp_violation_count"] == 0
                assert prove_json["summary"]["satisfiable_classes"] == prove_json["summary"]["total_classes"]
                assert len(prove_json["class_proofs"]) == prove_json["summary"]["total_classes"]
            else:
                assert prove_json["prover_available"] is False
                assert prove_json["reason"] == "PROVER_UNAVAILABLE"
        finally:
            svc.close()

    # 8b. Live console wrapping a compiled inherited spec (+ optional dead subtype detection on Py3.13)
    good_spec = compile_blueprint(_make_inheritable_base_and_subtype_bp(sub_discount_floor=0, sub_discount_cap=10))
    good_svc = ConsoleService(OntologyRuntime(good_spec), demo_actor_switch=True)
    try:
        gc = TestClient(create_console_app(good_svc))
        gbp = gc.get("/api/blueprint").json()
        vip_meta = next(o for o in gbp["object_types"] if o["name"] == "VipOrder")
        assert vip_meta["extends"] == "BaseOrder"
        tag_link = next(lk for lk in gbp["links"] if lk["source"] == "VipOrder" and lk["field"] == "tags")
        assert tag_link["min_cardinality"] == 2
        assert tag_link["max_cardinality"] == 3
    finally:
        good_svc.close()

    dead_bp_path = tmp_path / "dead_sub.json"
    dead_bp_path.write_text(
        json.dumps(
            _make_inheritable_base_and_subtype_bp(sub_discount_floor=30, sub_discount_cap=None).model_dump(
                mode="json"
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    if _has_z3():
        dead_spec = compile_blueprint(_make_inheritable_base_and_subtype_bp(sub_discount_floor=30, sub_discount_cap=None))
        dead_svc = ConsoleService(OntologyRuntime(dead_spec), demo_actor_switch=True)
        try:
            dc = TestClient(create_console_app(dead_svc))
            d_prove = dc.post("/api/blueprint/prove", json={}).json()
            assert d_prove["verdict"] == "ISSUES_FOUND"
            assert d_prove["summary"]["dead_class_count"] == 1
            bad_cls = next(c for c in d_prove["class_proofs"] if c["object_type"] == "VipOrder")
            assert bad_cls["status"] == "UNSATISFIABLE_CLASS"
            assert set(bad_cls["unsat_core"]) == {"discount_cap", "discount_floor"}
        finally:
            dead_svc.close()

        cli_res = run_design_prove(blueprints=[str(dead_bp_path)])
        assert cli_res["exit_code"] == 2
        assert cli_res["verdict"] == "FALSIFIED"
        assert "UNSATISFIABLE_CLASS" in cli_res["error_codes"]
    else:
        rc = design_cli_main(["prove", "--blueprint", str(dead_bp_path), "--json"])
        assert rc == 1
