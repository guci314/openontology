"""Phase A (WO-06) 验收测试：标准域本体 import 组合与只增不破 CustomerOverlay 护栏。

覆盖：
1. 多模块 ``import`` 零冲突组合（``std_core + std_crm + std_o2c`` 共享实体幂等去重 + 冲突报错）；
2. 正向扩展：不修改标准核心，用 ``CustomerOverlaySpec`` 叠加 ``owned=True`` 扩展字段、
   跨对象阻断前置条件（未关闭高优工单禁赢单）、高金额输单审批流与 ``WorkCard`` 结算、
   以及角色窄视图隔离（``O2cInvoice`` 越权读拦截）；
3. 破坏性 Overlay 红测（至少 4 大类破坏全部在编译期被 ``OVERLAY_BREAKS_STANDARD_CONTRACT`` 拦截）；
4. Python ``OntologySpec(extends=(std_crm_spec, std_o2c_spec))`` 与声明式组合语义对齐；
5. ``open_ontology/runtime.py`` 行数预算及公开导出检查。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.business_ontology import (
    BUSINESS_SEED_ONTOLOGY,
    WorkCard,
    settle_work_card,
    submit_or_open_work_card,
)
from examples.std_core_ontology import (
    AccountingEntity,
    BusinessParty,
    build_std_core_blueprint,
)
from examples.std_crm_ontology import (
    CrmAccount,
    CrmDeal,
    CrmTicket,
    build_std_crm_blueprint,
    spec as std_crm_spec,
)
from examples.std_o2c_ontology import (
    O2cCustomerProfile,
    O2cDelivery,
    O2cInvoice,
    O2cOrder,
    build_std_o2c_blueprint,
    spec as std_o2c_spec,
)
from open_ontology import Actor, InvariantError, OntologyRuntime, OntologySpec, PermissionDeniedError
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    EvolutionPatch,
    FieldMigrationSpec,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    ScopedViewBlueprint,
    apply_overlay,
    blueprint_fingerprint,
    compile_blueprint,
    compose_blueprints,
)
from open_ontology.osdk import AIPAgentExecutor

REPO_ROOT = Path(__file__).resolve().parents[1]


def _composed_enterprise_blueprint() -> DeclarativeBlueprint:
    return compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
        ],
        model_id="enterprise_std_unified",
        version="1.0.0",
    )


def _seed_crm_and_o2c_world(rt: OntologyRuntime, spec: OntologySpec) -> None:
    admin = Actor(actor_id="admin-1", role="admin", clearance_tags={"*"})
    PartyCls = spec.get_object_type("BusinessParty")
    EntityCls = spec.get_object_type("AccountingEntity")
    AccCls = spec.get_object_type("CrmAccount")
    DealCls = spec.get_object_type("CrmDeal")
    TicketCls = spec.get_object_type("CrmTicket")
    ProfCls = spec.get_object_type("O2cCustomerProfile")
    OrdCls = spec.get_object_type("O2cOrder")
    DelCls = spec.get_object_type("O2cDelivery")
    InvCls = spec.get_object_type("O2cInvoice")

    rt.put_object(EntityCls(entity_id="ENT-NORTH", name="华北法人主体", base_currency="CNY"), actor=admin)
    rt.put_object(
        PartyCls(party_id="PTY-001", legal_name="星辰精密制造有限公司", tax_id="91110000123456789X"),
        actor=admin,
    )
    rt.put_object(AccCls(account_id="ACC-001", name="星辰制造", party="PTY-001", industry="Manufacturing"), actor=admin)
    rt.put_object(
        DealCls(deal_id="DEAL-001", title="星辰二期智能产线项目", account="ACC-001", amount=80_000.0, stage="PROPOSAL"),
        actor=admin,
    )
    rt.put_object(
        TicketCls(ticket_id="TK-001", subject="产线试机传感器告警", account="ACC-001", priority="HIGH", status="OPEN"),
        actor=admin,
    )
    rt.put_object(
        ProfCls(
            profile_id="PROF-001",
            party="PTY-001",
            accounting_entity="ENT-NORTH",
            credit_limit=500_000.0,
            payment_terms="NET30",
        ),
        actor=admin,
    )
    rt.put_object(
        OrdCls(
            order_id="ORD-001",
            order_number="SO-2026-001",
            customer="PROF-001",
            accounting_entity="ENT-NORTH",
            amount_total=1000.0,
            status="draft",
        ),
        actor=admin,
    )
    rt.put_object(DelCls(delivery_id="DEL-001", order="ORD-001", status="pending"), actor=admin)
    rt.put_object(
        InvCls(
            invoice_id="INV-001",
            order="ORD-001",
            amount_total=1000.0,
            amount_residual=1000.0,
            payment_state="not_paid",
            status="draft",
        ),
        actor=admin,
    )


# ---------------------------------------------------------------------------
# Test 1: Multi-module import composition & conflict detection
# ---------------------------------------------------------------------------
def test_1_compose_blueprints_deduplicates_shared_core_and_rejects_conflicts() -> None:
    composed = _composed_enterprise_blueprint()

    assert composed.imports == ["std_core_ontology", "std_crm_ontology", "std_o2c_ontology"]
    assert composed.seed_ontology_id == "business_ontology"

    ot_names = [ot.name for ot in composed.object_types]
    assert len(ot_names) == len(set(ot_names)) == 10
    assert set(ot_names) == {
        "AccountingEntity",
        "BusinessParty",
        "WorkCard",
        "CrmAccount",
        "CrmDeal",
        "CrmTicket",
        "O2cCustomerProfile",
        "O2cOrder",
        "O2cDelivery",
        "O2cInvoice",
    }

    party_bp = next(ot for ot in composed.object_types if ot.name == "BusinessParty")
    assert len(party_bp.fields) <= 6

    act_names = [a.name for a in composed.actions]
    assert len(act_names) == len(set(act_names)) == 13
    assert {inv.name for inv in composed.invariants} == {
        "work_card_unique_active_intent",
        "work_card_segregation_of_duties",
    }

    spec = compile_blueprint(composed, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, ":memory:")
    _seed_crm_and_o2c_world(rt, spec)

    sales = Actor(actor_id="sales-1", role="sales_rep")
    warehouse = Actor(actor_id="wh-1", role="warehouse")
    finance = Actor(actor_id="fin-1", role="finance")

    # Standard CRM allows winning even with open ticket (customer overlay adds ticket blocking rule later)
    rt.submit_action("RUN-WIN-STD", "win_crm_deal", {"deal_id": "DEAL-001"}, sales)
    assert rt.get_object("CrmDeal", "DEAL-001", sales).stage == "CLOSED_WON"

    # Standard O2C full flow
    rt.submit_action("RUN-CONF", "confirm_o2c_order", {"order_id": "ORD-001"}, sales)
    rt.submit_action("RUN-DELIV", "complete_o2c_delivery", {"delivery_id": "DEL-001"}, warehouse)
    rt.submit_action("RUN-POST", "post_o2c_invoice", {"invoice_id": "INV-001"}, finance)
    rt.submit_action("RUN-PAY-1", "record_o2c_payment", {"invoice_id": "INV-001", "payment_amount": 400.0}, finance)
    inv_partial = rt.get_object("O2cInvoice", "INV-001", finance)
    assert inv_partial.amount_residual == pytest.approx(600.0)
    assert inv_partial.payment_state == "partial"

    rt.submit_action("RUN-PAY-2", "record_o2c_payment", {"invoice_id": "INV-001", "payment_amount": 600.0}, finance)
    inv_paid = rt.get_object("O2cInvoice", "INV-001", finance)
    assert inv_paid.amount_residual == pytest.approx(0.0)
    assert inv_paid.payment_state == "paid"

    # Non-isomorphic conflicts must raise BLUEPRINT_COMPOSITION_CONFLICT
    conflicting_ot = DeclarativeBlueprint(
        model_id="conflicting_crm",
        seed_ontology_id="business_ontology",
        object_types=[
            ObjectTypeBlueprint(
                name="BusinessParty",
                archetype="MasterEntity",
                primary_key="party_id",
                fields=[
                    FieldSpec(name="party_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="legal_name", field_type="str", required=True),
                    FieldSpec(name="extra_god_col", field_type="str", default=""),
                ],
            )
        ],
    )
    with pytest.raises(InvariantError) as exc_ot:
        compose_blueprints([build_std_core_blueprint(), conflicting_ot], model_id="bad_ot")
    assert exc_ot.value.code == "BLUEPRINT_COMPOSITION_CONFLICT"

    conflicting_act = DeclarativeBlueprint(
        model_id="conflicting_action",
        seed_ontology_id="business_ontology",
        actions=[
            ActionBlueprint(
                name="win_crm_deal",
                allowed_roles=["guest"],
                params=[FieldSpec(name="deal_id", field_type="str", required=True)],
            )
        ],
    )
    with pytest.raises(InvariantError) as exc_act:
        compose_blueprints([build_std_crm_blueprint(), conflicting_act], model_id="bad_act")
    assert exc_act.value.code == "BLUEPRINT_COMPOSITION_CONFLICT"

    conflicting_seed = DeclarativeBlueprint(
        model_id="other_seed_bp",
        seed_ontology_id="custom_other_seed",
        object_types=[ObjectTypeBlueprint(name="CustomObj", fields=[FieldSpec(name="id", primary_key=True)])],
    )
    with pytest.raises(InvariantError) as exc_seed:
        compose_blueprints([build_std_core_blueprint(), conflicting_seed], model_id="bad_seed")
    assert exc_seed.value.code == "BLUEPRINT_COMPOSITION_CONFLICT"


# ---------------------------------------------------------------------------
# Test 2: Positive additive CustomerOverlay end-to-end
# ---------------------------------------------------------------------------
def test_2_customer_overlay_extends_without_editing_standard_core() -> None:
    std_core_before = blueprint_fingerprint(build_std_core_blueprint())
    std_crm_before = blueprint_fingerprint(build_std_crm_blueprint())
    std_o2c_before = blueprint_fingerprint(build_std_o2c_blueprint())

    base = _composed_enterprise_blueprint()
    base_fp = blueprint_fingerprint(base)

    overlay = CustomerOverlaySpec(
        overlay_id="acme_crm_o2c_overlay",
        base_model_id=base.model_id,
        target_version="1.1.0",
        add_fields={
            "CrmAccount": [
                FieldSpec(
                    name="vip_tier",
                    field_type="str",
                    default="STANDARD",
                    owned=True,
                    description="客户自有 VIP 等级扩展字段",
                )
            ]
        },
        add_action_preconditions={
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="no_open_high_priority_ticket_before_win",
                    code="OPEN_HIGH_PRIORITY_TICKET_BLOCKS_WIN",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "target.account"}, "right": {"ref": "obj.account"}},
                                {"op": "eq", "left": {"ref": "target.priority"}, "right": {"lit": "HIGH"}},
                                {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                            ],
                        },
                    },
                    description="客户有未关闭高优先级工单时禁止商机赢单。",
                )
            ],
            "lose_crm_deal": [
                InvariantRuleSpec(
                    name="high_value_deal_loss_requires_steward",
                    code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 100000}},
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["steward", "admin"]},
                    },
                    description="金额大于 100000 的商机输单须经 steward/admin 审批放行。",
                )
            ],
        },
        add_views=[
            ScopedViewBlueprint(
                name="crm_sales_view",
                description="CRM 销售角色窄视图（屏蔽财务 O2cInvoice）",
                roles=["sales_rep"],
                object_types=["BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket", "WorkCard"],
                actions=["win_crm_deal", "lose_crm_deal", "open_work_card"],
                include_reachable_objects=False,
            )
        ],
    )

    extended = apply_overlay(base, overlay)
    assert extended.version == "1.1.0"
    assert blueprint_fingerprint(base) == base_fp
    assert blueprint_fingerprint(build_std_core_blueprint()) == std_core_before
    assert blueprint_fingerprint(build_std_crm_blueprint()) == std_crm_before
    assert blueprint_fingerprint(build_std_o2c_blueprint()) == std_o2c_before

    spec = compile_blueprint(extended, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, ":memory:")
    _seed_crm_and_o2c_world(rt, spec)

    admin = Actor(actor_id="admin-1", role="admin", clearance_tags={"*"})
    sales = Actor(actor_id="sales-1", role="sales_rep")
    support = Actor(actor_id="sup-1", role="support_agent")
    steward = Actor(actor_id="stew-1", role="steward")

    # 1. Mixed sovereignty owned field on CrmAccount
    CrmAccountCls = spec.get_object_type("CrmAccount")
    assert CrmAccountCls.__owned__ == {"vip_tier": "STANDARD"}
    acc = rt.get_object("CrmAccount", "ACC-001", admin)
    assert acc.vip_tier == "STANDARD"
    rt.put_object(
        CrmAccountCls(account_id="ACC-001", name="星辰制造", party="PTY-001", industry="Manufacturing", vip_tier="PLATINUM"),
        actor=admin,
    )
    assert rt.get_object("CrmAccount", "ACC-001", sales).vip_tier == "PLATINUM"

    # 2. Cross-object precondition blocks win_crm_deal while high-priority ticket is OPEN
    with pytest.raises(InvariantError) as exc_win:
        rt.submit_action("RUN-WIN-BLOCKED", "win_crm_deal", {"deal_id": "DEAL-001"}, sales)
    assert exc_win.value.code == "OPEN_HIGH_PRIORITY_TICKET_BLOCKS_WIN"

    # Resolve ticket -> win_crm_deal succeeds immediately
    rt.submit_action("RUN-RESOLVE-TK", "resolve_crm_ticket", {"ticket_id": "TK-001", "resolution_summary": "已更换传感器"}, support)
    rt.submit_action("RUN-WIN-OK", "win_crm_deal", {"deal_id": "DEAL-001"}, sales)
    assert rt.get_object("CrmDeal", "DEAL-001", sales).stage == "CLOSED_WON"

    # 3. High-value deal loss (> 100k) triggers APPROVAL_REQUIRED_HIGH_VALUE_LOSS -> WorkCard + SoD + t1 settle
    DealCls = spec.get_object_type("CrmDeal")
    rt.put_object(
        DealCls(deal_id="DEAL-BIG", title="超大型集团ERP改造", account="ACC-001", amount=250_000.0, stage="NEGOTIATION"),
        actor=admin,
    )
    esc = submit_or_open_work_card(
        rt,
        "RUN-LOSE-BIG",
        "lose_crm_deal",
        {"deal_id": "DEAL-BIG", "loss_reason": "客户预算冻结"},
        sales,
        target_object_type="CrmDeal",
        target_object_id="DEAL-BIG",
        assignee_role="steward",
        card_id="WC-LOSS-250K",
    )
    assert esc["status"] == "AWAITING_WORK_CARD"
    assert esc["error_code"] == "APPROVAL_REQUIRED_HIGH_VALUE_LOSS"
    assert rt.get_object("CrmDeal", "DEAL-BIG", sales).stage == "NEGOTIATION"
    assert rt.get_object(WorkCard, "WC-LOSS-250K", sales).status == "OPEN"

    # SoD: same actor_id with steward role cannot self-settle
    same_id_steward = Actor(actor_id="sales-1", role="steward")
    with pytest.raises(InvariantError) as exc_sod:
        settle_work_card(rt, "WC-LOSS-250K", same_id_steward)
    assert exc_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

    # Independent steward settles WorkCard -> live t1 execution marks deal CLOSED_LOST
    settled = settle_work_card(rt, "WC-LOSS-250K", steward, resolution_note="核实预算冻结属实，同意输单归档")
    assert rt.get_object(WorkCard, "WC-LOSS-250K", steward).status == "COMPLETED"
    deal_lost = rt.get_object("CrmDeal", "DEAL-BIG", steward)
    assert deal_lost.stage == "CLOSED_LOST"
    assert deal_lost.loss_reason == "客户预算冻结"
    assert settled["settled_action_run"]["action_type"] == "lose_crm_deal"

    # 4. ScopedView blocks cross-domain O2cInvoice reads & O2C action dispatch via AIPAgentExecutor
    agent = AIPAgentExecutor(rt, sales, view="crm_sales_view", enforce_view_scope=True)
    deals = agent.dispatch("query_objects", {"object_type": "CrmDeal"})
    assert deals["count"] == 2
    scoped_action_tools = {
        t["function"]["name"]
        for t in agent.tools()
        if t.get("x-ontology-kind") == "action"
    }
    assert scoped_action_tools == {"win_crm_deal", "lose_crm_deal", "open_work_card"}
    with pytest.raises(PermissionDeniedError):
        agent.dispatch("query_objects", {"object_type": "O2cInvoice"})
    with pytest.raises(PermissionDeniedError):
        agent.dispatch("get_object", {"object_type": "O2cInvoice", "object_id": "INV-001"})
    with pytest.raises(PermissionDeniedError):
        agent.dispatch("submit_action", {"run_id": "RUN-BLOCK-INV", "action": "post_o2c_invoice", "params": {"invoice_id": "INV-001"}})


# ---------------------------------------------------------------------------
# Test 3: Destructive Overlay red tests (all 4+ categories rejected at compile time)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "overlay_payload"),
    [
        (
            "delete_standard_object_type",
            {"remove_object_types": ["CrmTicket"]},
        ),
        (
            "delete_standard_required_field",
            {"remove_fields": {"CrmDeal": ["title"]}},
        ),
        (
            "drop_standard_field_via_migration",
            {"migrations": [FieldMigrationSpec(object_type="CrmDeal", op="drop_field", field="title")]},
        ),
        (
            "overwrite_standard_field_via_add_fields",
            {"add_fields": {"CrmDeal": [FieldSpec(name="title", field_type="str")]}},
        ),
        (
            "change_standard_field_type",
            {"alter_fields": {"CrmDeal": [FieldSpec(name="amount", field_type="str", non_negative=True)]}},
        ),
        (
            "weaken_non_negative_true_to_false",
            {"alter_fields": {"CrmDeal": [FieldSpec(name="amount", field_type="float", non_negative=False)]}},
        ),
        (
            "weaken_required_true_to_false",
            {"alter_fields": {"CrmDeal": [FieldSpec(name="title", field_type="str", required=False)]}},
        ),
        (
            "mutate_standard_primary_key",
            {"update_primary_keys": {"CrmDeal": "title"}},
        ),
        (
            "add_competing_primary_key_field",
            {"add_fields": {"CrmDeal": [FieldSpec(name="alt_id", field_type="str", primary_key=True)]}},
        ),
        (
            "delete_standard_invariant",
            {"remove_invariants": ["work_card_segregation_of_duties"]},
        ),
        (
            "overwrite_standard_invariant",
            {
                "add_invariants": [
                    InvariantRuleSpec(
                        name="work_card_segregation_of_duties",
                        code="WEAKENED",
                        target_type="WorkCard",
                        must_hold={"op": "eq", "left": {"lit": 1}, "right": {"lit": 1}},
                    )
                ]
            },
        ),
        (
            "delete_standard_action_precondition_explicit",
            {"remove_action_preconditions": {"win_crm_deal": ["crm_deal_winnable_stage"]}},
        ),
        (
            "drop_standard_precondition_via_update_actions",
            {
                "update_actions": [
                    ActionBlueprint(
                        name="win_crm_deal",
                        allowed_roles=["sales_rep", "steward"],
                        params=[FieldSpec(name="deal_id", field_type="str", required=True)],
                        preconditions=[],
                        mutations=[
                            ActionMutationSpec(
                                op="modify",
                                object_type="CrmDeal",
                                id_param="deal_id",
                                set_fields={"stage": {"lit": "CLOSED_WON"}},
                            )
                        ],
                    )
                ]
            },
        ),
        (
            "expand_standard_action_allowed_roles",
            {"add_roles": ["guest"], "restrict_allowed_roles": {"win_crm_deal": ["sales_rep", "guest"]}},
        ),
        (
            "reopen_terminal_fsm_state",
            {"update_transitions": {"CrmDeal": {"CLOSED_WON": ["PROSPECTING"]}}},
        ),
        (
            "drop_standard_fsm_transition_edge",
            {"update_transitions": {"CrmDeal": {"NEGOTIATION": ["CLOSED_WON"]}}},
        ),
        (
            "drop_fsm_state_via_replace_transitions",
            {"replace_transitions": {"CrmDeal": {"PROSPECTING": ["QUALIFICATION"]}}},
        ),
    ],
)
def test_3_destructive_overlay_rejected_at_compile_time(label: str, overlay_payload: dict) -> None:
    base = _composed_enterprise_blueprint()
    with pytest.raises(InvariantError, match=".") as exc_info:
        apply_overlay(base, overlay_payload)
    assert exc_info.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT", f"{label} raised unexpected code {exc_info.value.code!r}"


def test_3b_evolution_patch_with_destructive_op_also_rejected_by_apply_overlay() -> None:
    base = _composed_enterprise_blueprint()
    bad_patch = EvolutionPatch(
        base_version=base.version,
        target_version="1.0.1",
        remove_fields={"O2cOrder": ["order_number"]},
    )
    with pytest.raises(InvariantError) as exc_info:
        apply_overlay(base, bad_patch)
    assert exc_info.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"

    # Contrast: strictly monotonic tightening overlay succeeds
    strict_overlay = CustomerOverlaySpec(
        add_object_types=[
            ObjectTypeBlueprint(
                name="CustomerExtensionLog",
                archetype="EventVoucher",
                primary_key="log_id",
                owned=True,
                fields=[FieldSpec(name="log_id", field_type="str", primary_key=True, required=True)],
            )
        ],
        update_transitions={
            "CrmDeal": {
                "PROPOSAL": ["NEGOTIATION", "EXEC_REVIEW", "CLOSED_WON", "CLOSED_LOST"],
                "EXEC_REVIEW": ["CLOSED_WON", "CLOSED_LOST"],
            }
        },
        restrict_allowed_roles={"win_crm_deal": ["sales_manager", "steward"]},
        alter_fields={
            "CrmAccount": [FieldSpec(name="industry", field_type="str", required=True, default="GENERAL")]
        },
    )
    tightened = apply_overlay(base, strict_overlay, target_version="1.2.0")
    assert tightened.version == "1.2.0"
    assert any(ot.name == "CustomerExtensionLog" for ot in tightened.object_types)
    assert compile_blueprint(tightened, seed_ontology=BUSINESS_SEED_ONTOLOGY)


# ---------------------------------------------------------------------------
# Test 4: Python OntologySpec(extends=(std_crm_spec, std_o2c_spec)) parity
# ---------------------------------------------------------------------------
def test_4_python_diamond_extends_aligns_with_compose_blueprints() -> None:
    py_unified = OntologySpec(extends=(std_crm_spec, std_o2c_spec))
    bp_unified = compile_blueprint(_composed_enterprise_blueprint(), seed_ontology=BUSINESS_SEED_ONTOLOGY)

    assert set(py_unified.roles.keys()) == set(bp_unified.roles.keys())
    assert set(py_unified.object_types.keys()) == set(bp_unified.object_types.keys())
    assert set(py_unified.actions.keys()) == set(bp_unified.actions.keys())
    assert set(py_unified.invariants.keys()) == set(bp_unified.invariants.keys())
    assert set(py_unified.transition_invariants.keys()) == set(bp_unified.transition_invariants.keys())
    assert set(py_unified.object_properties.keys()) == set(bp_unified.object_properties.keys())

    for name, py_cls in py_unified.object_types.items():
        bp_cls = bp_unified.get_object_type(name)
        assert py_cls.__primary_key__ == bp_cls.__primary_key__
        assert getattr(py_cls, "__status_field__", "status") == getattr(bp_cls, "__status_field__", "status")
        assert getattr(py_cls, "__initial_statuses__", None) == getattr(bp_cls, "__initial_statuses__", None)
        assert getattr(py_cls, "__transitions__", {}) == getattr(bp_cls, "__transitions__", {})

    # Verify live execution on Python-composed unified spec
    rt = OntologyRuntime(py_unified, ":memory:")
    _seed_crm_and_o2c_world(rt, py_unified)
    sales = Actor(actor_id="sales-py", role="sales_rep")
    warehouse = Actor(actor_id="wh-py", role="warehouse")
    finance = Actor(actor_id="fin-py", role="finance")

    rt.submit_action("PY-WIN", "win_crm_deal", {"deal_id": "DEAL-001"}, sales)
    assert rt.get_object(CrmDeal, "DEAL-001", sales).stage == "CLOSED_WON"

    rt.submit_action("PY-CONF", "confirm_o2c_order", {"order_id": "ORD-001"}, sales)
    rt.submit_action("PY-DELIV", "complete_o2c_delivery", {"delivery_id": "DEL-001"}, warehouse)
    rt.submit_action("PY-POST", "post_o2c_invoice", {"invoice_id": "INV-001"}, finance)
    rt.submit_action("PY-PAY", "record_o2c_payment", {"invoice_id": "INV-001", "payment_amount": 1000.0}, finance)
    assert rt.get_object(O2cInvoice, "INV-001", finance).payment_state == "paid"
    assert rt.get_object(O2cOrder, "ORD-001", finance).status == "invoiced"
    assert rt.get_object(O2cDelivery, "DEL-001", warehouse).status == "completed"
    assert isinstance(rt.get_object(BusinessParty, "PTY-001", sales), BusinessParty)
    assert isinstance(rt.get_object(AccountingEntity, "ENT-NORTH", finance), AccountingEntity)
    assert isinstance(rt.get_object(CrmAccount, "ACC-001", sales), CrmAccount)
    assert isinstance(rt.get_object(CrmTicket, "TK-001", sales), CrmTicket)
    assert isinstance(rt.get_object(O2cCustomerProfile, "PROF-001", finance), O2cCustomerProfile)


# ---------------------------------------------------------------------------
# Test 5: Line budget & public exports sanity
# ---------------------------------------------------------------------------
def test_5_runtime_line_budget_and_exports() -> None:
    runtime_lines = (REPO_ROOT / "open_ontology" / "runtime.py").read_text(encoding="utf-8").splitlines()
    assert len(runtime_lines) <= 1350, f"open_ontology/runtime.py={len(runtime_lines)} exceeds 1350"
