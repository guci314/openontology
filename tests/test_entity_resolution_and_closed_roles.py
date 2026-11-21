"""WO-10 acceptance & red tests: closed-role blueprint governance, role ceilings & 4-tier entity resolution."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY, WorkCard, settle_work_card
from examples.std_core_ontology import STD_ROLES, build_std_core_blueprint
from examples.std_crm_ontology import build_std_crm_blueprint, spec as std_crm_spec
from examples.std_o2c_ontology import build_std_o2c_blueprint, spec as std_o2c_spec
from open_ontology import Actor, InvariantError, OntologyRuntime, PermissionDeniedError, UnknownRoleError
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    AdapterMappingSpec,
    CrosswalkTable,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    EntityMappingSpec,
    EntityResolutionDecision,
    EntityResolutionSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldSpec,
    FieldTransformSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    ScopedViewBlueprint,
    SourceEntityBatch,
    SourceRef,
    apply_overlay,
    apply_resolution_decisions,
    build_resolution_task_intent,
    compile_blueprint,
    compose_blueprints,
    inject_crosswalk_into_mapping,
    merge_projected_phenomena,
    project_external_phenomenon,
    resolve_entities,
)


def _composed_enterprise_bp() -> DeclarativeBlueprint:
    return compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
        ],
        model_id="std_enterprise_unified",
        version="1.0.0",
        seed_ontology_id="business_ontology",
    )


# ---------------------------------------------------------------------------
# Part 1: Closed-Role Whitelist Red Tests & Monotonic add_roles Governance
# ---------------------------------------------------------------------------
def test_1_composed_standard_blueprints_merge_std_roles() -> None:
    core_bp = build_std_core_blueprint()
    composed = _composed_enterprise_bp()
    expected = [role_name for role_name, _ in STD_ROLES]
    assert core_bp.roles == expected
    assert composed.roles == expected
    spec = compile_blueprint(composed, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    assert set(spec.roles.keys()) == set(expected)


@pytest.mark.parametrize(
    ("surface", "overlay_payload"),
    [
        (
            "restrict_allowed_roles",
            {"restrict_allowed_roles": {"win_crm_deal": ["sales_rep", "finace_director"]}},
        ),
        (
            "update_revert_roles",
            {"update_revert_roles": {"complete_work_card": ["steward", "finace_director"]}},
        ),
        (
            "add_views",
            {
                "add_views": [
                    ScopedViewBlueprint(
                        name="bad_role_view",
                        roles=["finace_director"],
                        object_types=["CrmDeal"],
                        actions=["win_crm_deal"],
                    )
                ]
            },
        ),
        (
            "add_actions_allowed_roles",
            {
                "add_actions": [
                    ActionBlueprint(
                        name="tag_deal_priority",
                        allowed_roles=["finace_director"],
                        params=[FieldSpec(name="deal_id", field_type="str", required=True)],
                        mutations=[
                            ActionMutationSpec(
                                op="modify",
                                object_type="CrmDeal",
                                id_param="deal_id",
                                set_fields={"loss_reason": {"lit": "TAGGED"}},
                            )
                        ],
                    )
                ]
            },
        ),
        (
            "add_actions_revert_roles",
            {
                "add_actions": [
                    ActionBlueprint(
                        name="tag_deal_priority",
                        allowed_roles=["steward"],
                        revert_roles=["finace_director"],
                        params=[FieldSpec(name="deal_id", field_type="str", required=True)],
                    )
                ]
            },
        ),
        (
            "add_fields_masked_roles",
            {
                "add_fields": {
                    "O2cOrder": [
                        FieldSpec(
                            name="margin_rate",
                            field_type="float",
                            default=0.0,
                            owned=True,
                            masked_roles=("finace_director",),
                        )
                    ]
                }
            },
        ),
        (
            "precondition_actor_role_in",
            {
                "add_action_preconditions": {
                    "confirm_o2c_order": [
                        InvariantRuleSpec(
                            name="bad_role_gate_in",
                            code="APPROVAL_REQUIRED_DIRECTOR",
                            target_type="O2cOrder",
                            id_param="order_id",
                            must_hold={
                                "op": "in",
                                "left": {"ref": "actor.role"},
                                "right": {"lit": ["steward", "finace_director"]},
                            },
                        )
                    ]
                }
            },
        ),
        (
            "precondition_actor_role_eq",
            {
                "add_action_preconditions": {
                    "win_crm_deal": [
                        InvariantRuleSpec(
                            name="bad_role_gate_eq",
                            code="APPROVAL_REQUIRED_DIRECTOR",
                            target_type="CrmDeal",
                            id_param="deal_id",
                            when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 1000}},
                            must_hold={
                                "op": "eq",
                                "left": {"ref": "actor.role"},
                                "right": {"lit": "finace_director"},
                            },
                        )
                    ]
                }
            },
        ),
        (
            "precondition_actor_role_ne_reversed",
            {
                "add_action_preconditions": {
                    "win_crm_deal": [
                        InvariantRuleSpec(
                            name="bad_role_gate_ne",
                            code="ROLE_FORBIDDEN",
                            target_type="CrmDeal",
                            id_param="deal_id",
                            must_hold={
                                "op": "ne",
                                "left": {"lit": "finace_director"},
                                "right": {"ref": "actor.role"},
                            },
                        )
                    ]
                }
            },
        ),
        (
            "precondition_actor_role_not_in",
            {
                "add_action_preconditions": {
                    "confirm_o2c_order": [
                        InvariantRuleSpec(
                            name="bad_role_gate_not_in",
                            code="ROLE_BLOCKED",
                            target_type="O2cOrder",
                            id_param="order_id",
                            must_hold={
                                "op": "not_in",
                                "left": {"ref": "actor.role"},
                                "right": {"lit": ["finace_director"]},
                            },
                        )
                    ]
                }
            },
        ),
        (
            "add_invariants_actor_role_typo",
            {
                "add_invariants": [
                    InvariantRuleSpec(
                        name="bad_inv_role",
                        code="BAD_INV_ROLE",
                        target_type="CrmDeal",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "actor.role"},
                            "right": {"lit": "finace_director"},
                        },
                    )
                ]
            },
        ),
    ],
)
def test_2_closed_role_rejects_typos_across_all_overlay_surfaces(
    surface: str,
    overlay_payload: dict[str, Any],
) -> None:
    composed = _composed_enterprise_bp()
    with pytest.raises(InvariantError) as exc_info:
        apply_overlay(composed, overlay_payload)
    assert exc_info.value.code == "UNKNOWN_ROLE", (
        f"{surface} raised {exc_info.value.code!r} instead of 'UNKNOWN_ROLE'"
    )


def test_3_compile_blueprint_also_enforces_closed_roles_directly() -> None:
    bp_bad_expr = DeclarativeBlueprint(
        model_id="closed_role_compile_check",
        version="1.0.0",
        roles=["admin", "steward", "finance_director"],
        object_types=[
            ObjectTypeBlueprint(
                name="BudgetRequest",
                primary_key="req_id",
                fields=[
                    FieldSpec(name="req_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="approved", field_type="bool", default=False),
                ],
            )
        ],
        actions=[
            ActionBlueprint(
                name="approve_budget",
                allowed_roles=["steward", "finance_director"],
                params=[FieldSpec(name="req_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="director_only_typo",
                        code="APPROVAL_REQUIRED",
                        target_type="BudgetRequest",
                        id_param="req_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "actor.role"},
                            "right": {"lit": "finace_director"},
                        },
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="BudgetRequest",
                        id_param="req_id",
                        set_fields={"approved": {"lit": True}},
                    )
                ],
            )
        ],
    )
    with pytest.raises(InvariantError) as exc_expr:
        compile_blueprint(bp_bad_expr)
    assert exc_expr.value.code == "UNKNOWN_ROLE"

    bp_bad_action_role = copy.deepcopy(bp_bad_expr)
    bp_bad_action_role.actions[0].preconditions = []
    bp_bad_action_role.actions[0].allowed_roles = ["finace_director"]
    with pytest.raises(UnknownRoleError):
        compile_blueprint(bp_bad_action_role)


def test_4_add_roles_unlocks_custom_extensions_but_forbids_expanding_standard_actions() -> None:
    composed = _composed_enterprise_bp()

    custom_overlay = CustomerOverlaySpec(
        overlay_id="regional_cfo_overlay",
        base_model_id=composed.model_id,
        add_roles=["regional_cfo", "audit_observer"],
        add_fields={
            "CrmDeal": [
                FieldSpec(
                    name="secret_discount_floor",
                    field_type="float",
                    default=0.1,
                    owned=True,
                    masked_roles=("audit_observer",),
                )
            ]
        },
        add_actions=[
            ActionBlueprint(
                name="flag_deal_for_cfo_review",
                allowed_roles=["regional_cfo"],
                params=[
                    FieldSpec(name="deal_id", field_type="str", required=True),
                    FieldSpec(name="note", field_type="str", default="CFO_FLAG"),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="CrmDeal",
                        id_param="deal_id",
                        set_fields={"loss_reason": {"ref": "param.note"}},
                    )
                ],
            )
        ],
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="ultra_high_order_requires_regional_cfo",
                    code="APPROVAL_REQUIRED_REGIONAL_CFO",
                    target_type="O2cOrder",
                    id_param="order_id",
                    when={"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 500_000.0}},
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["regional_cfo", "finance_director", "admin"]},
                    },
                )
            ]
        },
        add_views=[
            ScopedViewBlueprint(
                name="audit_observer_view",
                roles=["audit_observer", "regional_cfo"],
                object_types=["CrmDeal", "O2cOrder"],
                actions=["flag_deal_for_cfo_review"],
            )
        ],
    )
    extended = apply_overlay(composed, custom_overlay)
    assert "regional_cfo" in extended.roles
    assert "audit_observer" in extended.roles
    compiled = compile_blueprint(extended, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    assert "regional_cfo" in compiled.roles
    assert "audit_observer" in compiled.roles

    # Even with add_roles=["regional_cfo"], expanding a STANDARD action's allowed_roles is STILL rejected
    expand_std_overlay = CustomerOverlaySpec(
        overlay_id="illegal_std_role_expansion",
        base_model_id=composed.model_id,
        add_roles=["regional_cfo"],
        restrict_allowed_roles={
            "confirm_o2c_order": ["sales_rep", "regional_cfo"],
        },
    )
    with pytest.raises(InvariantError) as exc_expand:
        apply_overlay(composed, expand_std_overlay)
    assert exc_expand.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"


def test_5_open_role_legacy_blueprint_without_roles_stays_compatible() -> None:
    open_bp = DeclarativeBlueprint(
        model_id="legacy_open_roles_bp",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="SampleDoc",
                primary_key="doc_id",
                fields=[FieldSpec(name="doc_id", field_type="str", primary_key=True, required=True)],
            )
        ],
        actions=[
            ActionBlueprint(
                name="touch_doc",
                allowed_roles=["any_custom_role_1"],
                params=[FieldSpec(name="doc_id", field_type="str", required=True)],
            )
        ],
    )
    overlaid = apply_overlay(
        open_bp,
        CustomerOverlaySpec(
            restrict_allowed_roles={"touch_doc": ["any_custom_role_1"]},
            add_action_preconditions={
                "touch_doc": [
                    InvariantRuleSpec(
                        name="open_role_gate",
                        code="NEED_CUSTOM_ROLE",
                        target_type="SampleDoc",
                        id_param="doc_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "actor.role"},
                            "right": {"lit": "any_custom_role_1"},
                        },
                    )
                ]
            },
        ),
    )
    spec = compile_blueprint(overlaid)
    assert spec.roles == {}
    rt = OntologyRuntime(spec, ":memory:")
    try:
        doc_cls = spec.get_object_type("SampleDoc")
        rt.put_object(doc_cls(doc_id="D-1"))
        res = rt.submit_action(
            "RUN-OPEN-1",
            "touch_doc",
            {"doc_id": "D-1"},
            Actor(actor_id="u1", role="any_custom_role_1"),
        )
        assert res["status"] == "COMMITTED"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# Part 2: Standard Domain Role Ceiling Upgrade (finance_director)
# ---------------------------------------------------------------------------
def test_6_standard_crm_and_o2c_finance_director_escalation_and_narrowing() -> None:
    assert "finance_director" in std_o2c_spec.get_action("confirm_o2c_order").allowed_roles
    assert "finance_director" in std_o2c_spec.get_action("complete_o2c_delivery").allowed_roles
    assert "finance_director" in std_crm_spec.get_action("win_crm_deal").allowed_roles

    composed = _composed_enterprise_bp()
    for action_name in ("confirm_o2c_order", "complete_o2c_delivery", "win_crm_deal"):
        act = next(a for a in composed.actions if a.name == action_name)
        assert "finance_director" in act.allowed_roles

    # Overlay narrows confirm_o2c_order for >50k orders and complete_o2c_delivery/win_crm_deal to finance_director
    overlay = CustomerOverlaySpec(
        overlay_id="finance_director_escalation",
        base_model_id=composed.model_id,
        restrict_allowed_roles={
            "confirm_o2c_order": ["sales_rep", "finance_director", "steward", "admin"],
            "complete_o2c_delivery": ["warehouse", "finance_director", "admin"],
            "win_crm_deal": ["sales_rep", "finance_director", "admin"],
        },
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="large_order_finance_director_gate",
                    code="APPROVAL_REQUIRED_FINANCE_DIRECTOR",
                    target_type="O2cOrder",
                    id_param="order_id",
                    when={"op": "gt", "left": {"ref": "obj.amount_total"}, "right": {"lit": 50_000.0}},
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["finance_director", "admin"]},
                    },
                )
            ],
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="large_deal_win_finance_director_gate",
                    code="APPROVAL_REQUIRED_FINANCE_DIRECTOR_DEAL",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 50_000.0}},
                    must_hold={
                        "op": "eq",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": "finance_director"},
                    },
                )
            ],
        },
    )
    eff_bp = apply_overlay(composed, overlay)
    spec = compile_blueprint(eff_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, ":memory:")
    try:
        admin = Actor(actor_id="adm-1", role="admin", clearance_tags={"*"})
        sales = Actor(actor_id="sales-1", role="sales_rep")
        warehouse = Actor(actor_id="wh-1", role="warehouse")
        fin_dir = Actor(actor_id="fd-1", role="finance_director")

        rt.put_object(spec.get_object_type("AccountingEntity")(entity_id="ENT-1", name="HQ"), actor=admin)
        rt.put_object(spec.get_object_type("BusinessParty")(party_id="PTY-1", legal_name="MegaCorp"), actor=admin)
        rt.put_object(
            spec.get_object_type("O2cCustomerProfile")(
                profile_id="PROF-1",
                party="PTY-1",
                accounting_entity="ENT-1",
                credit_limit=500_000.0,
            ),
            actor=admin,
        )
        rt.put_object(
            spec.get_object_type("O2cOrder")(
                order_id="ORD-BIG",
                order_number="SO-BIG-1",
                customer="PROF-1",
                accounting_entity="ENT-1",
                amount_total=80_000.0,
                status="draft",
            ),
            actor=admin,
        )
        rt.put_object(spec.get_object_type("O2cDelivery")(delivery_id="DEL-BIG", order="ORD-BIG", status="pending"), actor=admin)
        rt.put_object(spec.get_object_type("CrmAccount")(account_id="ACC-1", name="MegaCorp CRM", party="PTY-1"), actor=admin)
        rt.put_object(
            spec.get_object_type("CrmDeal")(
                deal_id="DEAL-BIG",
                title="Mega Deal",
                account="ACC-1",
                amount=80_000.0,
                stage="NEGOTIATION",
            ),
            actor=admin,
        )

        # sales_rep cannot approve >50k order directly; opens WorkCard -> finance_director settles in t1
        with pytest.raises(InvariantError) as exc_ord:
            rt.submit_action("RUN-ORD-SALES", "confirm_o2c_order", {"order_id": "ORD-BIG"}, sales)
        assert exc_ord.value.code == "APPROVAL_REQUIRED_FINANCE_DIRECTOR"

        rt.submit_action(
            "RUN-WC-OPEN-ORD",
            "open_work_card",
            {
                "card_id": "WC-ORD-FD",
                "card_kind": "APPROVAL",
                "target_object_type": "O2cOrder",
                "target_object_id": "ORD-BIG",
                "requested_action": "confirm_o2c_order",
                "requested_params_json": json.dumps({"order_id": "ORD-BIG"}),
                "assignee_role": "finance_director",
            },
            sales,
        )
        res_ord = settle_work_card(rt, "WC-ORD-FD", fin_dir)
        assert res_ord["status"] == "COMMITTED"
        assert rt.get_object("O2cOrder", "ORD-BIG", fin_dir).status == "confirmed"
        assert rt.get_object(WorkCard, "WC-ORD-FD", fin_dir).status == "COMPLETED"

        # warehouse can deliver OR finance_director can complete_o2c_delivery directly when escalated
        res_deliv = rt.submit_action("RUN-DELIV-FD", "complete_o2c_delivery", {"delivery_id": "DEL-BIG"}, fin_dir)
        assert res_deliv["status"] == "COMMITTED"
        assert rt.get_object("O2cOrder", "ORD-BIG", warehouse).status == "delivered"

        # win_crm_deal >50k requires finance_director; sales_rep rejected, finance_director commits
        with pytest.raises(InvariantError) as exc_deal:
            rt.submit_action("RUN-WIN-SALES", "win_crm_deal", {"deal_id": "DEAL-BIG"}, sales)
        assert exc_deal.value.code == "APPROVAL_REQUIRED_FINANCE_DIRECTOR_DEAL"
        # ops was narrowed out via restrict_allowed_roles
        with pytest.raises(PermissionDeniedError):
            rt.submit_action("RUN-WIN-STEWARD", "win_crm_deal", {"deal_id": "DEAL-BIG"}, Actor(actor_id="st1", role="steward"))
        res_win = rt.submit_action("RUN-WIN-FD", "win_crm_deal", {"deal_id": "DEAL-BIG"}, fin_dir)
        assert res_win["status"] == "COMMITTED"
        assert rt.get_object("CrmDeal", "DEAL-BIG", fin_dir).stage == "CLOSED_WON"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# Part 3: Pure M2 4-Tier Entity Resolution Funnel + Crosswalk End-to-End
# ---------------------------------------------------------------------------
def test_7_l1_hard_key_merge_across_different_pks_and_names_with_survivorship() -> None:
    spec = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code"],
        survivorship={
            "legal_name": ["crm_db", "erp_db"],
            "tax_id": ["erp_db", "crm_db"],
            "country_code": ["erp_db", "crm_db"],
            "tier_tag": ["crm_db"],
        },
    )
    batches = [
        SourceEntityBatch(
            source_id="crm_db",
            source_table="accounts",
            pk_field="id",
            field_map={"legal_name": "name", "tax_id": "vat", "country_code": "country", "tier_tag": "tier"},
            records=[
                {"id": "uuid-acme-1", "name": "Acme China Manufacturing", "vat": "us1234-5673", "country": "", "tier": "STRATEGIC"}
            ],
        ),
        SourceEntityBatch(
            source_id="erp_db",
            source_table="res_partner",
            pk_field="partner_pk",
            field_map={"legal_name": "title", "tax_id": "reg_code", "country_code": "iso2"},
            records=[
                {"partner_pk": 77, "title": "ACME CORP (GLOBAL)", "reg_code": "US 12345673", "iso2": "CN"}
            ],
        ),
    ]
    report = resolve_entities(spec, batches)
    assert report.ambiguous_candidates == []
    assert report.blocked_pairs == []
    assert report.stats["canonical_entities"] == 1
    assert report.stats["l1_hard_key_entities"] == 1

    entry = report.crosswalk.entries[0]
    assert entry.match_tier == "L1_HARD_KEY"
    assert entry.confidence == 1.0
    assert len(entry.members) == 2
    assert entry.golden_record["legal_name"] == "Acme China Manufacturing"
    assert entry.golden_record["tax_id"] == "US 12345673"
    assert entry.golden_record["country_code"] == "CN"
    assert entry.golden_record["tier_tag"] == "STRATEGIC"
    assert report.crosswalk.resolve_canonical_id("crm_db", "uuid-acme-1") == entry.canonical_id
    assert report.crosswalk.resolve_canonical_id("erp_db", "77") == entry.canonical_id
    assert report.crosswalk.alias_map_for("crm_db") == {"uuid-acme-1": entry.canonical_id}
    assert report.crosswalk.alias_map_for("erp_db") == {"77": entry.canonical_id}


def test_8_conflicting_hard_key_blocks_identical_names_and_abc_transitive_merge() -> None:
    spec = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code"],
        strip_tokens=["有限公司", "Co., Ltd.", "Ltd."],
        auto_merge_threshold=0.90,
        review_threshold=0.55,
    )

    # Part A: Same-name sibling subsidiaries with distinct tax_ids MUST hard-block and NEVER become L4 candidate
    sub_report = resolve_entities(
        spec,
        [
            SourceEntityBatch(
                source_id="src_north",
                source_table="partners",
                pk_field="pk",
                records=[{"pk": "N1", "legal_name": "星辰精密制造有限公司", "tax_id": "91110000111111111A", "country_code": "CN"}],
            ),
            SourceEntityBatch(
                source_id="src_south",
                source_table="partners",
                pk_field="pk",
                records=[{"pk": "S1", "legal_name": "星辰精密制造", "tax_id": "91440300222222222B", "country_code": "CN"}],
            ),
        ],
    )
    assert sub_report.stats["canonical_entities"] == 2
    assert sub_report.ambiguous_candidates == []
    assert len(sub_report.blocked_pairs) == 1
    assert sub_report.blocked_pairs[0].reason_code == "CONFLICTING_HARD_KEY"

    # Part B: A-B-C transitive conflict red test
    # A (tax=AAA) ~ B (missing tax, 100% same quasi + neighbor) AND B ~ C (tax=CCC), but A conflicts with C!
    # Transitive clustering invariant MUST guarantee A and C NEVER end up in the same cluster.
    abc_report = resolve_entities(
        spec,
        [
            SourceEntityBatch(
                source_id="sys_a",
                source_table="parties",
                pk_field="id",
                records=[{"id": "A", "legal_name": "Vertex Precision Robotics Co., Ltd.", "tax_id": "TAX-AAA-001", "country_code": "CN"}],
            ),
            SourceEntityBatch(
                source_id="sys_b",
                source_table="parties",
                pk_field="id",
                records=[{"id": "B", "legal_name": "Vertex Precision Robotics", "tax_id": "", "country_code": "CN"}],
            ),
            SourceEntityBatch(
                source_id="sys_c",
                source_table="parties",
                pk_field="id",
                records=[{"id": "C", "legal_name": "Vertex Precision Robotics Ltd.", "tax_id": "TAX-CCC-999", "country_code": "CN"}],
            ),
        ],
    )
    cid_a = abc_report.crosswalk.resolve_canonical_id("sys_a", "A")
    cid_b = abc_report.crosswalk.resolve_canonical_id("sys_b", "B")
    cid_c = abc_report.crosswalk.resolve_canonical_id("sys_c", "C")
    assert cid_a is not None and cid_b is not None and cid_c is not None
    assert cid_a != cid_c, "A and C have conflicting hard keys and must NEVER transitively merge!"
    # B merges into one of them (without bridging A and C), leaving 2 clusters, 1 blocked pair, and 0 L4 conflicts
    assert len(abc_report.crosswalk.entries) == 2
    assert any(bp.reason_code == "CONFLICTING_HARD_KEY" for bp in abc_report.blocked_pairs)
    assert abc_report.ambiguous_candidates == []
    for entry in abc_report.crosswalk.entries:
        member_keys = {m.key for m in entry.members}
        assert not ({"sys_a:A", "sys_c:C"} <= member_keys)


def test_9_l2_caller_strip_tokens_and_l3_neighbor_weighted_auto_merge() -> None:
    spec_with_tokens = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code", "city"],
        strip_tokens=["股份有限公司", "有限责任公司", "有限公司", "Inc.", "Co., Ltd."],
        auto_merge_threshold=0.90,
        review_threshold=0.55,
    )
    batches = [
        SourceEntityBatch(
            source_id="crm",
            source_table="accounts",
            pk_field="id",
            records=[{"id": "CRM-HX", "legal_name": "华星光电材料有限公司", "tax_id": "", "country_code": "CN", "city": "Shenzhen"}],
        ),
        SourceEntityBatch(
            source_id="erp",
            source_table="partners",
            pk_field="id",
            records=[{"id": "ERP-HX", "legal_name": "华星光电材料股份有限公司", "tax_id": "", "country_code": "CN", "city": "Shenzhen"}],
        ),
    ]
    rep = resolve_entities(spec_with_tokens, batches)
    assert rep.stats["canonical_entities"] == 1
    assert rep.crosswalk.entries[0].match_tier == "L2_L3_SCORE"
    assert rep.crosswalk.entries[0].confidence == pytest.approx(1.0)
    assert rep.ambiguous_candidates == []

    # Without caller-injected strip_tokens (M2 never hardcodes industry suffixes), score falls below auto-merge
    spec_no_tokens = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code", "city"],
        strip_tokens=[],
        auto_merge_threshold=0.95,
        review_threshold=0.55,
    )
    rep_raw = resolve_entities(spec_no_tokens, batches)
    assert rep_raw.stats["canonical_entities"] == 2
    assert len(rep_raw.ambiguous_candidates) == 1


def test_10_l4_gray_zone_work_card_steward_confirm_and_reject_persistence() -> None:
    spec = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code", "city"],
        auto_merge_threshold=0.90,
        review_threshold=0.55,
        quasi_key_weight=0.70,
        neighbor_weight=0.30,
    )
    batches = [
        SourceEntityBatch(
            source_id="crm",
            source_table="accounts",
            pk_field="id",
            records=[
                {"id": "C-EAST", "legal_name": "Eastlake Robotics Labs", "country_code": "CN", "city": "Hangzhou"},
                {"id": "C-BEACON", "legal_name": "Beacon Medical Supply", "country_code": "US", "city": "Boston"},
            ],
        ),
        SourceEntityBatch(
            source_id="erp",
            source_table="partners",
            pk_field="id",
            records=[
                {"id": "E-EAST", "legal_name": "Eastlake Robotics", "country_code": "CN", "city": "Hangzhou"},
                {"id": "E-BEACON", "legal_name": "Beacon Medical", "country_code": "US", "city": "Boston"},
            ],
        ),
    ]

    rep1 = resolve_entities(spec, batches)
    # Gray zone MUST NOT auto-merge silently
    assert rep1.stats["canonical_entities"] == 4
    assert len(rep1.ambiguous_candidates) == 2
    for cand in rep1.ambiguous_candidates:
        assert cand.match_tier == "L4_AMBIGUOUS"
        assert 0.55 <= cand.score < 0.90

    # Mount M1 WorkCard(card_kind="HUMAN_TASK") via domain-agnostic build_resolution_task_intent
    core_spec = compile_blueprint(build_std_core_blueprint(), seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(core_spec, ":memory:")
    try:
        ops = Actor(actor_id="ops-1", role="ops")
        steward = Actor(actor_id="steward-1", role="steward")

        cand_by_pair = {
            frozenset({c.left.raw_pk, c.right.raw_pk}): c for c in rep1.ambiguous_candidates
        }
        cand_east = cand_by_pair[frozenset({"C-EAST", "E-EAST"})]
        cand_beacon = cand_by_pair[frozenset({"C-BEACON", "E-BEACON"})]

        decisions: list[EntityResolutionDecision] = []
        for idx, (cand, action_mode) in enumerate(
            [(cand_east, "approve"), (cand_beacon, "reject")],
            start=1,
        ):
            intent = build_resolution_task_intent(cand)
            assert intent["task_kind"] == "HUMAN_TASK"
            card_id = f"WC-ER-{idx}"
            rt.submit_action(
                f"RUN-OPEN-{idx}",
                "open_work_card",
                {
                    "card_id": card_id,
                    "card_kind": intent["task_kind"],
                    "target_object_type": intent["target_object_type"],
                    "target_object_id": intent["target_object_id"],
                    "requested_action": intent["requested_operation"],
                    "requested_params_json": intent["params_json"],
                    "assignee_role": intent["suggested_assignee_role"],
                },
                ops,
            )
            rt.submit_action(f"RUN-CLAIM-{idx}", "claim_work_card", {"card_id": card_id}, steward)
            if action_mode == "approve":
                rt.submit_action(
                    f"RUN-COMP-{idx}",
                    "complete_work_card",
                    {"card_id": card_id, "resolution_note": "Confirmed same legal entity"},
                    steward,
                )
                assert rt.get_object(WorkCard, card_id, steward).status == "COMPLETED"
                decisions.append(
                    EntityResolutionDecision(
                        candidate_id=cand.candidate_id,
                        left=cand.left,
                        right=cand.right,
                        verdict="CONFIRM_MERGE",
                        decided_by=steward.actor_id,
                        rationale="Verified domain registration match",
                        preferred_canonical_id="PTY-EASTLAKE",
                    )
                )
            else:
                rt.submit_action(
                    f"RUN-REJ-{idx}",
                    "reject_work_card",
                    {"card_id": card_id, "resolution_note": "Distinct independent distributors"},
                    steward,
                )
                assert rt.get_object(WorkCard, card_id, steward).status == "REJECTED"
                decisions.append(
                    EntityResolutionDecision(
                        candidate_id=cand.candidate_id,
                        left=cand.left,
                        right=cand.right,
                        verdict="REJECT_SEPARATE",
                        decided_by=steward.actor_id,
                        rationale="Confirmed distinct legal entities",
                    )
                )
    finally:
        rt.close()

    cw_updated = apply_resolution_decisions(rep1.crosswalk, decisions)
    assert len(cw_updated.confirmed_links) == 1
    assert len(cw_updated.negative_links) == 1

    # Re-run entity resolution: Eastlake merges at L0_CONFIRMED ("PTY-EASTLAKE"); Beacon stays split & blocked cleanly
    rep2 = resolve_entities(spec, batches, existing_crosswalk=cw_updated)
    assert rep2.ambiguous_candidates == []
    assert rep2.stats["canonical_entities"] == 3
    assert rep2.stats["l0_confirmed_entities"] == 1
    assert rep2.crosswalk.resolve_canonical_id("crm", "C-EAST") == "PTY-EASTLAKE"
    assert rep2.crosswalk.resolve_canonical_id("erp", "E-EAST") == "PTY-EASTLAKE"
    east_entry = next(e for e in rep2.crosswalk.entries if e.canonical_id == "PTY-EASTLAKE")
    assert east_entry.match_tier == "L0_CONFIRMED"
    assert east_entry.confidence == 1.0

    cid_c_beacon = rep2.crosswalk.resolve_canonical_id("crm", "C-BEACON")
    cid_e_beacon = rep2.crosswalk.resolve_canonical_id("erp", "E-BEACON")
    assert cid_c_beacon is not None and cid_e_beacon is not None and cid_c_beacon != cid_e_beacon
    assert any(bp.reason_code == "L0_NEGATIVE_LINK" for bp in rep2.blocked_pairs)

    # Round 3 re-run still yields 0 repeat ambiguous prompts
    rep3 = resolve_entities(spec, batches, existing_crosswalk=rep2.crosswalk)
    assert rep3.ambiguous_candidates == []
    assert rep3.stats["canonical_entities"] == 3


def test_11_e2e_inject_crosswalk_and_merge_projected_phenomena_triggers_cross_system_guardrail() -> None:
    crm_profile = ExternalPhenomenonProfile(
        source_kind="sqlite_crm",
        entities=[
            ExternalEntityProfile(
                table_name="accounts",
                row_count=1,
                fields=[ExternalFieldProfile(name="id"), ExternalFieldProfile(name="name")],
                sample_rows=[
                    {
                        "id": "crm-uuid-9001",
                        "name": "Huayu Precision Manufacturing Co., Ltd.",
                        "tax": "91310000MA1FL0012X",
                        "country": "CN",
                    }
                ],
            ),
            ExternalEntityProfile(
                table_name="tickets",
                row_count=1,
                fields=[ExternalFieldProfile(name="tid"), ExternalFieldProfile(name="acc_id")],
                sample_rows=[
                    {
                        "tid": "TK-9001",
                        "acc_id": "crm-uuid-9001",
                        "subj": "Line calibration alarm",
                        "prio": "HIGH",
                        "st": "OPEN",
                    }
                ],
            ),
        ],
    )
    erp_profile = ExternalPhenomenonProfile(
        source_kind="erp_jsonrpc",
        entities=[
            ExternalEntityProfile(
                table_name="res_company",
                row_count=1,
                fields=[ExternalFieldProfile(name="cid")],
                sample_rows=[{"cid": "CMP-CN", "cname": "China Ops Entity", "ccy": "CNY"}],
            ),
            ExternalEntityProfile(
                table_name="res_partner",
                row_count=1,
                fields=[ExternalFieldProfile(name="pid"), ExternalFieldProfile(name="vat")],
                sample_rows=[
                    {
                        "pid": "501",
                        "title": "华宇精密制造（苏州）",
                        "vat": "9131-0000-MA1FL0012X",
                        "iso": "CN",
                        "credit_limit": 200_000.0,
                        "company_id": "CMP-CN",
                    }
                ],
            ),
            ExternalEntityProfile(
                table_name="sale_order",
                row_count=1,
                fields=[ExternalFieldProfile(name="oid"), ExternalFieldProfile(name="pid")],
                sample_rows=[
                    {
                        "oid": "SO-9001",
                        "doc_no": "SO-2026-9001",
                        "pid": "501",
                        "company_id": "CMP-CN",
                        "total": 45_000.0,
                        "st": "draft",
                    }
                ],
            ),
        ],
    )

    er_spec = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code"],
        survivorship={
            "legal_name": ["src_crm", "src_erp"],
            "tax_id": ["src_erp", "src_crm"],
            "country_code": ["src_erp", "src_crm"],
        },
    )
    er_report = resolve_entities(
        er_spec,
        [
            SourceEntityBatch(
                source_id="src_crm",
                source_table="accounts",
                pk_field="id",
                field_map={"legal_name": "name", "tax_id": "tax", "country_code": "country"},
                records=crm_profile.entities[0].sample_rows,
            ),
            SourceEntityBatch(
                source_id="src_erp",
                source_table="res_partner",
                pk_field="pid",
                field_map={"legal_name": "title", "tax_id": "vat", "country_code": "iso"},
                records=erp_profile.entities[1].sample_rows,
            ),
        ],
    )
    assert er_report.stats["canonical_entities"] == 1
    assert er_report.crosswalk.entries[0].match_tier == "L1_HARD_KEY"
    shared_party_id = er_report.crosswalk.entries[0].canonical_id

    base_crm_mapping = AdapterMappingSpec(
        mapping_id="crm_map",
        source_kind="sqlite_crm",
        entity_mappings=[
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="id", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="tax_id", source_field="tax", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct", default="CN"),
                ],
            ),
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="CrmAccount",
                id_transform=FieldTransformSpec(target_field="account_id", source_field="id", kind="id_prefix", prefix="ACC-"),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="party", source_field="id", kind="direct"),
                ],
            ),
            EntityMappingSpec(
                source_table="tickets",
                target_object_type="CrmTicket",
                id_transform=FieldTransformSpec(target_field="ticket_id", source_field="tid", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="subject", source_field="subj", kind="direct", default=""),
                    FieldTransformSpec(target_field="account", source_field="acc_id", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="priority", source_field="prio", kind="direct", default="MEDIUM"),
                    FieldTransformSpec(target_field="status", source_field="st", kind="direct", default="OPEN"),
                ],
            ),
        ],
    )
    base_erp_mapping = AdapterMappingSpec(
        mapping_id="erp_map",
        source_kind="erp_jsonrpc",
        entity_mappings=[
            EntityMappingSpec(
                source_table="res_company",
                target_object_type="AccountingEntity",
                id_transform=FieldTransformSpec(target_field="entity_id", source_field="cid", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="cname", kind="direct", default=""),
                    FieldTransformSpec(target_field="base_currency", source_field="ccy", kind="direct", default="CNY"),
                ],
            ),
            EntityMappingSpec(
                source_table="res_partner",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="pid", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="title", kind="direct", default=""),
                    FieldTransformSpec(target_field="tax_id", source_field="vat", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="iso", kind="direct", default="CN"),
                ],
            ),
            EntityMappingSpec(
                source_table="res_partner",
                target_object_type="O2cCustomerProfile",
                id_transform=FieldTransformSpec(target_field="profile_id", source_field="pid", kind="id_prefix", prefix="PROF-"),
                field_transforms=[
                    FieldTransformSpec(target_field="party", source_field="pid", kind="direct"),
                    FieldTransformSpec(target_field="accounting_entity", source_field="company_id", kind="direct"),
                    FieldTransformSpec(target_field="credit_limit", source_field="credit_limit", kind="direct", default=0.0),
                ],
            ),
            EntityMappingSpec(
                source_table="sale_order",
                target_object_type="O2cOrder",
                id_transform=FieldTransformSpec(target_field="order_id", source_field="oid", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="order_number", source_field="doc_no", kind="direct", default=""),
                    FieldTransformSpec(target_field="customer", source_field="pid", kind="foreign_key_id", prefix="PROF-"),
                    FieldTransformSpec(target_field="accounting_entity", source_field="company_id", kind="direct"),
                    FieldTransformSpec(target_field="amount_total", source_field="total", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="status", source_field="st", kind="direct", default="draft"),
                ],
            ),
        ],
    )

    # Immute-inject crosswalk aliases into both heterogeneous mappings
    crm_mapping = inject_crosswalk_into_mapping(
        base_crm_mapping,
        source_id="src_crm",
        crosswalk=er_report.crosswalk,
        foreign_key_fields=[("CrmAccount", "party")],
    )
    erp_mapping = inject_crosswalk_into_mapping(
        base_erp_mapping,
        source_id="src_erp",
        crosswalk=er_report.crosswalk,
        foreign_key_fields=[("O2cCustomerProfile", "party")],
    )
    assert base_crm_mapping.entity_mappings[0].id_transform.kind == "direct"

    crm_m0, crm_errs = project_external_phenomenon(crm_profile, crm_mapping)
    erp_m0, erp_errs = project_external_phenomenon(erp_profile, erp_mapping)
    assert crm_errs == [] and erp_errs == []

    merged_m0 = merge_projected_phenomena([crm_m0, erp_m0])
    parties = [item for item in merged_m0 if item["object_type"] == "BusinessParty"]
    assert len(parties) == 1
    assert parties[0]["object_id"] == shared_party_id
    assert parties[0]["data"]["legal_name"] == "Huayu Precision Manufacturing Co., Ltd."
    assert parties[0]["data"]["tax_id"] == "91310000MA1FL0012X"

    composed = _composed_enterprise_bp()
    cross_sys_bp = apply_overlay(
        composed,
        CustomerOverlaySpec(
            overlay_id="cross_system_party_guard",
            base_model_id=composed.model_id,
            add_action_preconditions={
                "confirm_o2c_order": [
                    InvariantRuleSpec(
                        name="block_o2c_order_on_open_critical_crm_ticket",
                        code="OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER",
                        target_type="O2cOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "not_exists",
                            "object_type": "CrmTicket",
                            "where": {
                                "op": "and",
                                "args": [
                                    {
                                        "op": "eq",
                                        "left": {"ref": "target.account.party"},
                                        "right": {"ref": "obj.customer.party"},
                                    },
                                    {"op": "in", "left": {"ref": "target.priority"}, "right": {"lit": ["HIGH", "URGENT"]}},
                                    {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                                ],
                            },
                        },
                    )
                ]
            },
        ),
    )
    spec = compile_blueprint(cross_sys_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, ":memory:")
    try:
        admin = Actor(actor_id="adm-1", role="admin", clearance_tags={"*"})
        for entry in merged_m0:
            cls = spec.get_object_type(entry["object_type"])
            rt.put_object(cls(**entry["data"]), actor=admin)

        sales = Actor(actor_id="sales-1", role="sales_rep")
        support = Actor(actor_id="sup-1", role="support_agent")

        with pytest.raises(InvariantError) as exc_blk:
            rt.submit_action("RUN-ORD-BLK", "confirm_o2c_order", {"order_id": "SO-9001"}, sales)
        assert exc_blk.value.code == "OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER"

        rt.submit_action(
            "RUN-TK-RES",
            "resolve_crm_ticket",
            {"ticket_id": "TK-9001", "resolution_summary": "Calibrated sensor"},
            support,
        )
        res_ok = rt.submit_action("RUN-ORD-OK", "confirm_o2c_order", {"order_id": "SO-9001"}, sales)
        assert res_ok["status"] == "COMMITTED"
        assert rt.get_object("O2cOrder", "SO-9001", sales).status == "confirmed"
    finally:
        rt.close()
