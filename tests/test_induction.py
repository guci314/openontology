"""验收测试：神经符号归纳（LLM 溯因提议 + 符号沙箱证伪结晶 · Ontology Builder & Evolver）。

覆盖：
1. L1 零 eval/exec 声明式编译器：ObjectType/ObjectProperty/AutoID/FSM/算术与跨对象谓词、危险表达式拦截；
2. FrictionCollector 防噪与聚类：单点 DEVICE_STATE_MISMATCH 不越线、重复 REVERTED 达标升级；
3. SymbolicCrucible 五道确定性关卡（正反断言 + 生产库 state_fingerprint 零污染保证）：
   - 关 1：静态安全与 M2 契约校验；
   - 关 2：存量 M0 无损拟合与 FieldMigrationSpec 迁移校验；
   - 关 3：Builder 正负样本判别力（防空壳）与 Evolver 目标事故消除；
   - 关 4：osdk.run_eval_suite 防灾难性遗忘回归锁；
   - 关 5：奥卡姆剃刀防实例 ID 过拟合（OVERFITTING_INSTANCE_ID）与复杂度上限；
4. 元自举治理本体（InductionGovernanceService）权责分离：
   - Builder/Evolver 无法伪造证伪报告（PermissionDeniedError）；
   - Builder/Evolver 无法自我批复结晶（PermissionDeniedError）；
   - 未经 Crucible 放行的提案不可申请结晶（UNVERIFIED_PROPOSAL）；
   - Steward 审批后原子落盘不可变 CrystallizedModelVersion 与 GoldenEvalCase，并支持回退；
5. 端到端三幕剧 `examples/o2c_induction_demo.py` 全链路验收与 M2 架构红线无退化。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.o2c_induction_demo import (
    AI_AGENT,
    FINANCE_DIRECTOR,
    _base_o2c_blueprint,
    build_initial_o2c_m0_objects,
    run_o2c_induction_demo,
)
from open_ontology import (
    Actor,
    DeviceStateMismatchError,
    InvariantError,
    ObjectNotFoundError,
    OntologyRuntime,
    PermissionDeniedError,
    SourceBridge,
)
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    BUILDER_ACTOR,
    CRUCIBLE_ACTOR,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EVOLVER_ACTOR,
    EvolutionPatch,
    FieldMigrationSpec,
    FieldSpec,
    FrictionClusterSummary,
    FrictionCollector,
    IncidentCounterexample,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    OntologyEvolverAgent,
    ProposedEvalCase,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    apply_evolution_patch,
    blueprint_fingerprint,
    compile_blueprint,
    eval_predicate,
    migrate_m0_snapshot,
    validate_predicate_ast,
)
from open_ontology.model_views import describe_tbox


# ---------------------------------------------------------------------------
# 1. L1 零 exec 声明式编译与安全白名单
# ---------------------------------------------------------------------------
def test_l1_zero_exec_compiler_supports_object_properties_fsm_and_arithmetic() -> None:
    bp = DeclarativeBlueprint.model_validate(
        _base_o2c_blueprint(include_unshipped_invoice_guard=True)
    )
    spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    # TBox 投影必须完整暴露 AutoID 与 ObjectProperty 元数据
    tbox = describe_tbox(spec)
    obj_map = {ot["name"]: ot for ot in tbox["object_types"]}
    assert "Customer" in obj_map and "SalesOrder" in obj_map and "Invoice" in obj_map
    inv_fields = {f["name"]: f for f in obj_map["Invoice"]["fields"]}
    assert inv_fields["invoice_id"]["auto_generated"] is True
    assert inv_fields["order"]["property_kind"] == "object"
    assert inv_fields["order"]["range"] == "SalesOrder"

    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        for row in build_initial_o2c_m0_objects():
            cls = spec.get_object_type(row["object_type"])
            rt.put_object(cls(**row["data"]))

        # 非法状态跳变（draft -> shipped 绕过 confirmed）必须被 FSM 跃迁不变量拦截
        with pytest.raises(InvariantError) as exc_fsm:
            rt.submit_action("BAD-FSM", "ship_sales_order", {"order_id": "SO-SMALL-01"}, AI_AGENT)
        assert exc_fsm.value.code == "ILLEGAL_DOCUMENT_TRANSITION"

        # 未发货开票必须被跨 ObjectProperty 不变量拦截
        with pytest.raises(InvariantError) as exc_inv:
            rt.submit_action(
                "BAD-INV",
                "issue_invoice",
                {"invoice_id": "INV-1", "order_id": "SO-SMALL-01", "amount": 20000},
                AI_AGENT,
            )
        assert exc_inv.value.code == "CANNOT_INVOICE_UNSHIPPED_ORDER"

        # 合法链路：confirm -> ship -> issue_invoice
        rt.submit_action(
            "OK-1",
            "confirm_sales_order",
            {"order_id": "SO-SMALL-01", "order_amount": 20000},
            AI_AGENT,
        )
        rt.submit_action("OK-2", "ship_sales_order", {"order_id": "SO-SMALL-01"}, AI_AGENT)
        rt.submit_action(
            "OK-3",
            "issue_invoice",
            {"invoice_id": "INV-OK", "order_id": "SO-SMALL-01", "amount": 20000},
            AI_AGENT,
        )
        assert rt.count_objects("Invoice") == 1
    finally:
        rt.close()


def test_predicate_ast_rejects_unsafe_ops_and_dunder_refs() -> None:
    with pytest.raises(InvariantError) as exc_op:
        validate_predicate_ast({"op": "eval", "code": "__import__('os').system('id')"})
    assert exc_op.value.code == "UNSAFE_EXPRESSION"

    with pytest.raises(InvariantError) as exc_dunder:
        validate_predicate_ast(
            {"op": "eq", "left": {"ref": "obj.__class__"}, "right": {"lit": "x"}}
        )
    assert exc_dunder.value.code == "INVALID_IDENTIFIER"

    with pytest.raises(InvariantError) as exc_root:
        validate_predicate_ast(
            {"op": "eq", "left": {"ref": "globals.secret"}, "right": {"lit": "x"}}
        )
    assert exc_root.value.code == "INVALID_PREDICATE_AST"


def test_migrate_m0_snapshot_supports_add_default_rename_and_cast() -> None:
    raw = [
        {"object_type": "SalesOrder", "object_id": "SO-1", "data": {"order_id": "SO-1", "amt": "500"}},
    ]
    migrations = [
        FieldMigrationSpec(object_type="SalesOrder", op="rename_field", field="amt", new_field="amount"),
        FieldMigrationSpec(object_type="SalesOrder", op="cast_type", field="amount", target_type="int"),
        FieldMigrationSpec(object_type="SalesOrder", op="add_default", field="note", default="migrated"),
    ]
    out = migrate_m0_snapshot(raw, migrations)
    assert out[0]["data"] == {"order_id": "SO-1", "amount": 500, "note": "migrated"}


# ---------------------------------------------------------------------------
# 2. SymbolicCrucible 五道关卡独立正反测试 + 生产库零污染
# ---------------------------------------------------------------------------
def test_crucible_gate_1_blocks_broken_relation_target() -> None:
    bad_bp = DeclarativeBlueprint(
        model_id="broken",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Order",
                primary_key="order_id",
                fields=[FieldSpec(name="order_id", primary_key=True, required=True)],
                relations=[RelationSpec(field="customer", target_type="GhostCustomer")],
            )
        ],
    )
    report = SymbolicCrucible().evaluate(bad_bp, CrucibleContext(mode="genesis"))
    assert report.passed is False
    assert report.gates[0].gate == "gate_1_static_safety"
    assert report.gates[0].passed is False


def test_crucible_gate_2_blocks_incompatible_rule_over_legacy_m0_objects() -> None:
    base = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    # 荒谬的新不变量：要求所有存量订单金额 <= 50000，直接把存量 15 万合法订单判死！
    patch = EvolutionPatch(
        base_version="1.0.0",
        target_version="1.1.0",
        add_invariants=[
            InvariantRuleSpec(
                name="absurd_small_cap",
                code="ORDER_TOO_LARGE",
                target_type="SalesOrder",
                must_hold={"op": "le", "left": {"ref": "obj.amount"}, "right": {"lit": 50000}},
            )
        ],
    )
    candidate = apply_evolution_patch(base, patch)
    report = SymbolicCrucible().evaluate(
        candidate,
        CrucibleContext(
            mode="evolution",
            m0_objects=build_initial_o2c_m0_objects(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        ),
        patch=patch,
    )
    assert report.passed is False
    g2 = next(g for g in report.gates if g.gate == "gate_2_m0_grounding")
    assert g2.passed is False
    assert any("ORDER_TOO_LARGE" in ce for ce in g2.counterexamples)


def test_crucible_gate_3_catches_vacuous_genesis_blueprint() -> None:
    vacuous_bp = DeclarativeBlueprint.model_validate(
        _base_o2c_blueprint(include_unshipped_invoice_guard=False)
    )
    ctx = CrucibleContext(
        mode="genesis",
        m0_objects=build_initial_o2c_m0_objects(),
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        negative_events=[
            ReplayEvent(
                name="neg_invoice_unshipped",
                action="issue_invoice",
                params={"invoice_id": "INV-X", "order_id": "SO-SMALL-01", "amount": 20000},
                actor=AI_AGENT,
                expected_valid=False,
                expected_error_code="CANNOT_INVOICE_UNSHIPPED_ORDER",
            )
        ],
    )
    report = SymbolicCrucible().evaluate(vacuous_bp, ctx)
    assert report.passed is False
    assert [g.gate for g in report.failed_gates()] == ["gate_3_counterexample_elimination"]


def test_crucible_gate_3_evolution_enforces_positive_events_against_shortcut_rules() -> None:
    """In evolution mode, Gate 3 must reject shortcut patches that block legitimate positive_events."""
    base = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    confirm_act = next(a for a in base.actions if a.name == "confirm_sales_order")

    # Shortcut / reward-hacking patch: blocks ALL ai_agent confirmations regardless of amount!
    shortcut_act = confirm_act.model_copy(
        update={
            "preconditions": list(confirm_act.preconditions)
            + [
                InvariantRuleSpec(
                    name="ban_all_ai_confirm",
                    code="BAN_ALL_AI_CONFIRM",
                    target_type="SalesOrder",
                    id_param="order_id",
                    description="shortcut blocking every ai_agent order confirmation",
                    must_hold={
                        "op": "ne",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": "ai_agent"},
                    },
                )
            ]
        }
    )
    shortcut_patch = EvolutionPatch(
        base_version="1.0.0",
        target_version="1.1.0",
        update_actions=[shortcut_act],
    )
    ctx = CrucibleContext(
        mode="evolution",
        m0_objects=build_initial_o2c_m0_objects(),
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        incident_counterexamples=[
            IncidentCounterexample(
                name="cx_big_order_ai",
                action="confirm_sales_order",
                params={"order_id": "SO-BIG-01", "order_amount": 150000},
                actor=AI_AGENT,
                expect_valid=False,
            )
        ],
        positive_events=[
            ReplayEvent(
                name="pos_small_order_ai",
                action="confirm_sales_order",
                params={"order_id": "SO-SMALL-01", "order_amount": 20000},
                actor=AI_AGENT,
                expected_valid=True,
            ),
            ReplayEvent(
                name="pos_big_order_finance",
                action="confirm_sales_order",
                params={"order_id": "SO-BIG-01", "order_amount": 150000},
                actor=FINANCE_DIRECTOR,
                expected_valid=True,
            ),
        ],
        default_actor=AI_AGENT,
    )
    bad_report = SymbolicCrucible().evaluate(
        apply_evolution_patch(base, shortcut_patch), ctx, patch=shortcut_patch
    )
    assert bad_report.passed is False
    g3_bad = next(g for g in bad_report.gates if g.gate == "gate_3_counterexample_elimination")
    assert g3_bad.passed is False
    assert g3_bad.metrics["incidents_eliminated"] == 1
    assert g3_bad.metrics["positive_passed"] == 1
    assert g3_bad.metrics["positive_total"] == 2
    assert any("pos_small_order_ai" in ce for ce in g3_bad.counterexamples)

    # Proper semantic patch: only block ai_agent when order amount exceeds customer credit_limit.
    proper_act = confirm_act.model_copy(
        update={
            "preconditions": list(confirm_act.preconditions)
            + [
                InvariantRuleSpec(
                    name="credit_limit_exceeded_for_ai",
                    code="CREDIT_LIMIT_EXCEEDED_FOR_AI",
                    target_type="SalesOrder",
                    id_param="order_id",
                    description="ai_agent cannot confirm orders exceeding customer credit limit",
                    must_hold={
                        "op": "or",
                        "args": [
                            {"op": "ne", "left": {"ref": "actor.role"}, "right": {"lit": "ai_agent"}},
                            {
                                "op": "le",
                                "left": {"ref": "obj.amount"},
                                "right": {"ref": "obj.customer.credit_limit"},
                            },
                        ],
                    },
                )
            ]
        }
    )
    proper_patch = EvolutionPatch(
        base_version="1.0.0",
        target_version="1.1.0",
        update_actions=[proper_act],
    )
    good_report = SymbolicCrucible().evaluate(
        apply_evolution_patch(base, proper_patch), ctx, patch=proper_patch
    )
    assert good_report.passed is True
    g3_good = next(g for g in good_report.gates if g.gate == "gate_3_counterexample_elimination")
    assert g3_good.metrics["incidents_eliminated"] == 1
    assert g3_good.metrics["positive_passed"] == 2


def test_crucible_gate_4_prevents_catastrophic_forgetting_and_preserves_production() -> None:
    base = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    spec = compile_blueprint(base, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    prod = OntologyRuntime(spec, db_path=":memory:")
    try:
        for row in build_initial_o2c_m0_objects():
            prod.put_object(spec.get_object_type(row["object_type"])(**row["data"]))
        fp_before = prod.state_fingerprint()

        # 把合法订单确认一刀切拦死 -> 回归破坏黄金小单用例
        patch = EvolutionPatch(
            base_version="1.0.0",
            target_version="1.1.0",
            add_invariants=[
                InvariantRuleSpec(
                    name="block_all_confirmed",
                    code="REGRESS_TEST",
                    target_type="SalesOrder",
                    must_hold={"op": "ne", "left": {"ref": "obj.status"}, "right": {"lit": "confirmed"}},
                )
            ],
        )
        candidate = apply_evolution_patch(base, patch)
        ctx = CrucibleContext(
            mode="evolution",
            production_runtime=prod,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            golden_eval_cases=[
                {
                    "name": "small_order_no_approval",
                    "actor": {"actor_id": "agent:o2c-ops", "role": "ai_agent", "clearance_tags": ["*"]},
                    "action": "confirm_sales_order",
                    "params": {"order_id": "SO-SMALL-01", "order_amount": 20000},
                    "expect": {"valid": True},
                }
            ],
            default_actor=AI_AGENT,
        )
        report = SymbolicCrucible().evaluate(candidate, ctx, patch=patch)
        assert report.passed is False
        assert report.production_untouched is True
        assert prod.state_fingerprint() == fp_before
        assert [g.gate for g in report.failed_gates()] == ["gate_4_golden_regression"]
    finally:
        prod.close()


def test_crucible_gate_5_catches_instance_id_overfitting_and_rule_bloat() -> None:
    base = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    confirm_act = next(a for a in base.actions if a.name == "confirm_sales_order")
    overfit_act = confirm_act.model_copy(
        update={
            "preconditions": list(confirm_act.preconditions)
            + [
                InvariantRuleSpec(
                    name="overfit_single_order",
                    code="OVERFIT_ORDER",
                    target_type="SalesOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "ne",
                        "left": {"ref": "param.order_id"},
                        "right": {"lit": "SO-BIG-01"},
                    },
                )
            ]
        }
    )
    overfit_patch = EvolutionPatch(
        base_version="1.0.0",
        target_version="1.1.0",
        update_actions=[overfit_act],
    )
    candidate = apply_evolution_patch(base, overfit_patch)
    report = SymbolicCrucible().evaluate(
        candidate,
        CrucibleContext(
            mode="evolution",
            m0_objects=build_initial_o2c_m0_objects(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        ),
        patch=overfit_patch,
    )
    assert report.passed is False
    g5 = next(g for g in report.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert g5.passed is False
    assert any("OVERFITTING_INSTANCE_ID" in ce for ce in g5.counterexamples)


# ---------------------------------------------------------------------------
# 3. FrictionCollector 防噪门禁测试
# ---------------------------------------------------------------------------
def test_friction_collector_filters_single_noise_and_escalates_systemic_reverts() -> None:
    bp = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        for row in build_initial_o2c_m0_objects():
            rt.put_object(spec.get_object_type(row["object_type"])(**row["data"]))

        # 单次偶发 DEVICE_STATE_MISMATCH
        rt.bind_source_bridge(
            SourceBridge(
                actuate=lambda _mv: {"ok": True},
                observe=lambda: {
                    "SalesOrder": [
                        {
                            "order_id": "SO-SMALL-01",
                            "customer": "CUST-APPLE",
                            "amount": 20000,
                            "status": "draft",
                        }
                    ]
                },
            ),
            world_mode="coupled",
        )
        with pytest.raises(DeviceStateMismatchError):
            rt.submit_action(
                "MISMATCH-1",
                "confirm_sales_order",
                {"order_id": "SO-SMALL-01", "order_amount": 20000},
                AI_AGENT,
            )
        rt.bind_source_bridge(None, world_mode="standalone")

        # 2 次系统性回滚
        rt.submit_action("R1", "confirm_sales_order", {"order_id": "SO-BIG-01", "order_amount": 150000}, AI_AGENT)
        rt.revert_action("R1", "REV1", FINANCE_DIRECTOR)
        rt.submit_action("R2", "confirm_sales_order", {"order_id": "SO-BIG-02", "order_amount": 180000}, AI_AGENT)
        rt.revert_action("R2", "REV2", FINANCE_DIRECTOR)

        clusters = FrictionCollector(min_support=2).collect(
            rt,
            revert_reasons={"R1": "over_limit", "R2": "over_limit"},
        )
        by_kind = {c.signal_kind: c for c in clusters}
        assert by_kind["DEVICE_STATE_MISMATCH"].support_count == 1
        assert by_kind["DEVICE_STATE_MISMATCH"].escalated is False
        assert by_kind["REVERTED_INCIDENT"].support_count == 2
        assert by_kind["REVERTED_INCIDENT"].escalated is True

        # 未达阈值的噪声簇若强行送进 Evolver，必须被拒绝
        with InductionGovernanceService() as gov:
            evolver = OntologyEvolverAgent(
                backend=CallableAbductionBackend(lambda *_: {}),
                governance=gov,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            with pytest.raises(InvariantError) as exc_noise:
                evolver.run_evolution_cycle(
                    model_id="erp_o2c",
                    cluster=by_kind["DEVICE_STATE_MISMATCH"],
                    crucible_context=CrucibleContext(
                        mode="evolution",
                        seed_ontology=BUSINESS_SEED_ONTOLOGY,
                    ),
                    base_blueprint=bp,
                )
            assert exc_noise.value.code == "BELOW_ESCALATION_THRESHOLD"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 4. 元自举治理本体：权责分离（RBAC）、防未验证结晶、Steward 审批与回退
# ---------------------------------------------------------------------------
def test_governance_enforces_separation_of_powers_and_steward_rollback() -> None:
    bp_v1 = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    bp_v2 = apply_evolution_patch(
        bp_v1,
        EvolutionPatch(
            base_version="1.0.0",
            target_version="1.1.0",
            rationale="upgrade to 1.1.0",
        ),
    )

    with InductionGovernanceService() as gov:
        # 1) Builder 提议 v1.0.0
        gov.runtime.submit_action(
            "P1",
            "propose_genesis_model",
            {
                "proposal_id": "PROP-1",
                "model_id": "erp_o2c",
                "target_version": "1.0.0",
                "blueprint_json": bp_v1.model_dump_json(),
            },
            BUILDER_ACTOR,
        )

        # 2) Builder 严禁伪造证伪报告（越权被 RBAC 拦截）
        with pytest.raises(PermissionDeniedError):
            gov.runtime.submit_action(
                "CHEAT-VERIFY",
                "record_falsification_report",
                {
                    "report_id": "RPT-FAKE",
                    "proposal_id": "PROP-1",
                    "passed": True,
                    "report_json": "{}",
                },
                BUILDER_ACTOR,
            )

        # 3) 未经 Crucible 验证的提案严禁申请结晶
        with pytest.raises(InvariantError) as exc_unverified:
            gov.request_crystallization("PROP-1", BUILDER_ACTOR, run_id="CHEAT-CRYSTAL")
        assert exc_unverified.value.code == "UNVERIFIED_PROPOSAL"

        # 4) Crucible 签发通过报告后，Builder 申请结晶 -> 同步进入 AWAITING_APPROVAL（零 M2 PENDING_APPROVAL）
        gov.runtime.submit_action(
            "REAL-VERIFY-1",
            "record_falsification_report",
            {
                "report_id": "RPT-1",
                "proposal_id": "PROP-1",
                "passed": True,
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        req_res = gov.request_crystallization("PROP-1", BUILDER_ACTOR, run_id="CRYSTAL-1")
        assert req_res["status"] == "COMMITTED"
        assert [p.proposal_id for p in gov.list_pending_crystallizations("erp_o2c")] == ["PROP-1"]
        assert all(r["status"] in {"COMMITTED", "REJECTED", "REVERTED"} for r in gov.runtime.list_action_runs())
        assert gov.get_active_blueprint("erp_o2c") is None  # 未批复前生效谱系零改动

        # 5) Builder / Evolver 严禁自我批复结晶
        with pytest.raises(PermissionDeniedError):
            gov.approve_crystallization("CRYSTAL-1", BUILDER_ACTOR)
        with pytest.raises(PermissionDeniedError):
            gov.approve_crystallization("CRYSTAL-1", EVOLVER_ACTOR)

        # 6) 只有 Steward 能批复结晶
        committed = gov.approve_crystallization("CRYSTAL-1", STEWARD_ACTOR)
        assert committed["status"] == "COMMITTED"
        assert committed["approved_by"] == STEWARD_ACTOR.actor_id
        assert gov.get_active_blueprint("erp_o2c").version == "1.0.0"

        # 7) 结晶 v1.1.0 后测试 Steward 回退至 v1.0.0
        gov.runtime.submit_action(
            "P2",
            "propose_evolution_patch",
            {
                "proposal_id": "PROP-2",
                "model_id": "erp_o2c",
                "base_version": "1.0.0",
                "target_version": "1.1.0",
                "blueprint_json": bp_v2.model_dump_json(),
                "patch_json": "{}",
            },
            EVOLVER_ACTOR,
        )
        gov.runtime.submit_action(
            "REAL-VERIFY-2",
            "record_falsification_report",
            {
                "report_id": "RPT-2",
                "proposal_id": "PROP-2",
                "passed": True,
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        gov.request_crystallization("PROP-2", EVOLVER_ACTOR, run_id="CRYSTAL-2")
        gov.approve_crystallization("CRYSTAL-2", STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.1.0"
        assert all(r["status"] in {"COMMITTED", "REJECTED", "REVERTED"} for r in gov.runtime.list_action_runs())

        # Steward 一键回退至 1.0.0（同时不可变版本历史与报告仍完整保留）
        gov.rollback_crystallization("PROP-2", steward=STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.0.0"
        assert len(gov.list_reports()) == 2


# ---------------------------------------------------------------------------
# 5. 端到端三幕剧验收（Builder 0->1 + 噪声过滤 + Evolver 1->N 三轮 CEGIS）
# ---------------------------------------------------------------------------
def test_o2c_induction_end_to_end_demo_offline() -> None:
    summary = run_o2c_induction_demo(offline=True)

    assert summary["backend_mode"] == "offline-deterministic"
    # 第一幕：Builder 第 1 轮漏写不变量被关 3 拦截，第 2 轮修正后通过
    assert summary["genesis_rounds"] == [
        {"round": 1, "passed": False, "failed_gates": ["gate_3_counterexample_elimination"]},
        {"round": 2, "passed": True, "failed_gates": []},
    ]
    # 第二幕：1 条单点噪声被过滤，1 个系统性回滚簇（support=2）被升级
    assert summary["noise_filtered_clusters"] == 1
    assert summary["escalated_support_count"] == 2
    # 第三幕：Evolver 第 1 轮过拟合被关 5 精准抓出 -> 第 2 轮遗忘被关 4 精准抓出 -> 第 3 轮全绿结晶
    assert summary["evolution_rounds"] == [
        {"round": 1, "passed": False, "failed_gates": ["gate_5_parsimony_anti_overfitting"]},
        {"round": 2, "passed": False, "failed_gates": ["gate_4_golden_regression"]},
        {"round": 3, "passed": True, "failed_gates": []},
    ]
    assert summary["production_untouched"] is True
    assert summary["final_active_version"] == "1.1.0"
    assert summary["golden_eval_case_count"] == 3
    assert summary["golden_eval_case_names"] == [
        "small_order_auto_confirm",
        "cannot_invoice_unshipped_order",
        "big_order_requires_finance_approval",
    ]


# ---------------------------------------------------------------------------
# 6. P0 / P1 / P2 评审返工专项回归测试
# ---------------------------------------------------------------------------
def test_p0_1_cegis_recovers_from_llm_schema_error_and_prompts_include_schema() -> None:
    from open_ontology.induction import BUILDER_SYSTEM_PROMPT, EVOLVER_SYSTEM_PROMPT

    assert "object_types" in BUILDER_SYSTEM_PROMPT
    assert "entity_mappings" in BUILDER_SYSTEM_PROMPT
    assert "base_version" in EVOLVER_SYSTEM_PROMPT

    seen_feedbacks: list[str] = []

    def _flaky_llm(_sys: str, user: str, round_index: int) -> dict[str, Any]:
        payload = json.loads(user)
        seen_feedbacks.append(str(payload.get("previous_crucible_feedback") or ""))
        if round_index == 1:
            # 模拟真实 LLM 第一轮吐出非法额外键（extra="forbid" 会抛 ValidationError）
            return {"blueprint": {"model_id": "erp_o2c", "bogus_hallucinated_key": 999}}
        return {
            "rationale": "第 2 轮根据 ValidationError 修正为合法蓝图",
            "blueprint": _base_o2c_blueprint(include_unshipped_invoice_guard=True),
        }

    with InductionGovernanceService() as gov:
        builder = OntologyBuilderAgent(
            backend=CallableAbductionBackend(_flaky_llm),
            governance=gov,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        res = builder.run_genesis_cycle(
            model_id="erp_o2c",
            crucible_context=CrucibleContext(
                mode="genesis",
                m0_objects=build_initial_o2c_m0_objects(),
                default_actor=AI_AGENT,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            ),
            max_rounds=3,
        )
        assert res.verified is True
        assert len(res.rounds) == 2
        assert res.rounds[0].passed is False
        assert res.rounds[0].failed_gates[0] == "gate_1_static_safety"
        assert "bogus_hallucinated_key" in seen_feedbacks[1]
        assert res.rounds[1].passed is True


def test_p0_2_gate_2_verifies_production_ontology_overlay_and_detects_ownership_loss() -> None:
    from examples.odoo_builder_o2c_demo import (
        SALES_AGENT,
        _synthesize_o2c_blueprint,
        _synthesize_odoo_adapter_mapping,
        build_offline_odoo_mirror_profile,
    )
    from open_ontology.induction import AdapterMappingSpec, project_external_phenomenon

    bp = DeclarativeBlueprint.model_validate(_synthesize_o2c_blueprint(include_unshipped_guard=True))
    spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    prod = OntologyRuntime(spec, db_path=":memory:")
    try:
        profile = build_offline_odoo_mirror_profile()
        mapping = AdapterMappingSpec.model_validate(_synthesize_odoo_adapter_mapping(clean_filters=True))
        projected, _ = project_external_phenomenon(profile, mapping)
        for row in projected:
            prod.put_object(spec.get_object_type(row["object_type"])(**row["data"]))

        # 在生产库写入一条 ontology_overlay 自有治理补丁
        prod.submit_action(
            "OV-1",
            "annotate_customer_risk",
            {"customer_id": "CUST-77", "risk_tier": "WATCHLIST", "sales_followup_note": "VIP 重点跟进"},
            SALES_AGENT,
        )

        # 合法候选模型保留 __owned__ -> 关 2 应校验 1 条 overlay 并通过
        ok_report = SymbolicCrucible().evaluate(
            bp,
            CrucibleContext(
                mode="evolution",
                production_runtime=prod,
                default_actor=SALES_AGENT,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            ),
        )
        g2_ok = next(g for g in ok_report.gates if g.gate == "gate_2_m0_grounding")
        assert g2_ok.passed is True
        assert g2_ok.metrics["m0_overlays_checked"] == 1

        # 破坏性候选模型误去掉了 Customer.risk_tier 的 owned=True -> 关 2 必须捕获 OVERLAY_OWNERSHIP_LOST
        broken_bp = bp.model_copy(deep=True)
        for ot in broken_bp.object_types:
            if ot.name == "Customer":
                for f in ot.fields:
                    if f.name == "risk_tier":
                        f.owned = False
        bad_report = SymbolicCrucible().evaluate(
            broken_bp,
            CrucibleContext(
                mode="evolution",
                production_runtime=prod,
                default_actor=SALES_AGENT,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            ),
        )
        g2_bad = next(g for g in bad_report.gates if g.gate == "gate_2_m0_grounding")
        assert g2_bad.passed is False
        assert any("OVERLAY_OWNERSHIP_LOST" in ce for ce in g2_bad.counterexamples)
    finally:
        prod.close()


def test_p0_3_adapter_mapping_persistence_optimistic_lock_and_multistep_rollback() -> None:
    from open_ontology.induction import (
        AdapterMappingSpec,
        EntityMappingSpec,
        FieldTransformSpec,
        blueprint_fingerprint,
    )

    bp_v1 = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    bp_v1_1 = apply_evolution_patch(
        bp_v1, EvolutionPatch(base_version="1.0.0", target_version="1.1.0", rationale="v1.1")
    )
    bp_v1_1_alt = apply_evolution_patch(
        bp_v1, EvolutionPatch(base_version="1.0.0", target_version="1.1.1", rationale="concurrent alt")
    )
    bp_v1_2 = apply_evolution_patch(
        bp_v1_1, EvolutionPatch(base_version="1.1.0", target_version="1.2.0", rationale="v1.2")
    )
    sample_mapping = AdapterMappingSpec(
        mapping_id="map-v1",
        entity_mappings=[
            EntityMappingSpec(
                source_table="t_cust",
                target_object_type="Customer",
                id_transform=FieldTransformSpec(target_field="customer_id", source_field="id", kind="id_prefix", prefix="C-"),
            )
        ],
    )

    with InductionGovernanceService() as gov:
        # 1) 结晶 v1.0.0 并保留 adapter_mapping_json
        gov.runtime.submit_action(
            "P1",
            "propose_genesis_model",
            {
                "proposal_id": "PROP-V1",
                "model_id": "erp_o2c",
                "target_version": "1.0.0",
                "blueprint_json": bp_v1.model_dump_json(),
                "adapter_mapping_json": sample_mapping.model_dump_json(),
                "proposed_eval_cases_json": json.dumps([{"name": "case_v1"}]),
            },
            BUILDER_ACTOR,
        )
        gov.runtime.submit_action(
            "R1",
            "record_falsification_report",
            {
                "report_id": "RPT-V1",
                "proposal_id": "PROP-V1",
                "passed": True,
                "blueprint_hash": blueprint_fingerprint(bp_v1),
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        gov.request_crystallization("PROP-V1", BUILDER_ACTOR, run_id="C1")
        gov.approve_crystallization("C1", STEWARD_ACTOR)
        saved_map = gov.get_active_adapter_mapping("erp_o2c")
        assert saved_map is not None and saved_map.mapping_id == "map-v1"

        # 2) 两个并发演化提案都基于 v1.0.0 提交：先批 PROP-V11 成功，后批 PROP-V11-ALT 必须被乐观锁拦截
        for pid, ver, bp, cases in (
            ("PROP-V11", "1.1.0", bp_v1_1, [{"name": "case_v1_1"}]),
            ("PROP-V11-ALT", "1.1.1", bp_v1_1_alt, [{"name": "case_v1_1_alt"}]),
        ):
            gov.runtime.submit_action(
                f"P-{pid}",
                "propose_evolution_patch",
                {
                    "proposal_id": pid,
                    "model_id": "erp_o2c",
                    "base_version": "1.0.0",
                    "target_version": ver,
                    "blueprint_json": bp.model_dump_json(),
                    "patch_json": "{}",
                    "proposed_eval_cases_json": json.dumps(cases),
                },
                EVOLVER_ACTOR,
            )
            gov.runtime.submit_action(
                f"R-{pid}",
                "record_falsification_report",
                {
                    "report_id": f"RPT-{pid}",
                    "proposal_id": pid,
                    "passed": True,
                    "blueprint_hash": blueprint_fingerprint(bp),
                    "report_json": '{"passed": true}',
                },
                CRUCIBLE_ACTOR,
            )
        # 两个并发提案均在 v1.0.0 时申批；先批 PROP-V11 成功后，后批 PROP-V11-ALT 必须在 t1 重算时被乐观锁拦截（STALE_BASE_VERSION）
        gov.request_crystallization("PROP-V11", EVOLVER_ACTOR, run_id="C-V11")
        gov.request_crystallization("PROP-V11-ALT", EVOLVER_ACTOR, run_id="C-V11-ALT")
        gov.approve_crystallization("C-V11", STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.1.0"
        assert gov.get_active_adapter_mapping("erp_o2c").mapping_id == "map-v1"

        # 基于过期 base_version="1.0.0" 的第二个并发提案在 t1 批准与重新申批均必须被拒绝（STALE_BASE_VERSION）
        with pytest.raises(InvariantError) as exc_stale:
            gov.approve_crystallization("C-V11-ALT", STEWARD_ACTOR)
        assert exc_stale.value.code == "STALE_BASE_VERSION"

        # 3) 继续升级到 v1.2.0，再连续两步回滚 v1.2.0 -> v1.1.0 -> v1.0.0，且自动停用回滚版本的黄金用例
        gov.runtime.submit_action(
            "P-V12",
            "propose_evolution_patch",
            {
                "proposal_id": "PROP-V12",
                "model_id": "erp_o2c",
                "base_version": "1.1.0",
                "target_version": "1.2.0",
                "blueprint_json": bp_v1_2.model_dump_json(),
                "patch_json": "{}",
                "proposed_eval_cases_json": json.dumps([{"name": "case_v1_2"}]),
            },
            EVOLVER_ACTOR,
        )
        gov.runtime.submit_action(
            "R-V12",
            "record_falsification_report",
            {
                "report_id": "RPT-V12",
                "proposal_id": "PROP-V12",
                "passed": True,
                "blueprint_hash": blueprint_fingerprint(bp_v1_2),
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        gov.request_crystallization("PROP-V12", EVOLVER_ACTOR, run_id="C-V12")
        gov.approve_crystallization("C-V12", STEWARD_ACTOR)
        assert [c["name"] for c in gov.get_golden_eval_cases("erp_o2c")] == ["case_v1", "case_v1_1", "case_v1_2"]

        gov.rollback_crystallization("PROP-V12", steward=STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.1.0"
        assert [c["name"] for c in gov.get_golden_eval_cases("erp_o2c")] == ["case_v1", "case_v1_1"]

        gov.rollback_crystallization("PROP-V11", steward=STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.0.0"
        assert [c["name"] for c in gov.get_golden_eval_cases("erp_o2c")] == ["case_v1"]


def test_p1_fixes_precondition_no_full_scan_toposort_and_arithmetic_guard() -> None:
    from open_ontology.induction import (
        AdapterMappingSpec,
        EntityMappingSpec,
        ExternalEntityProfile,
        ExternalPhenomenonProfile,
        FieldTransformSpec,
        RowFilterSpec,
        project_external_phenomenon,
    )

    # P1-1: 传入不存在的 order_id 必须抛 ObjectNotFoundError，而非全表扫描报无关单据错误
    bp = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        for row in build_initial_o2c_m0_objects():
            rt.put_object(spec.get_object_type(row["object_type"])(**row["data"]))
        with pytest.raises(ObjectNotFoundError):
            rt.submit_action(
                "MISSING-ORD",
                "confirm_sales_order",
                {"order_id": "SO-DOES-NOT-EXIST", "order_amount": 999},
                AI_AGENT,
            )
    finally:
        rt.close()

    # P1-2: 倒序声明 Invoice 在 SalesOrder 之前，拓扑排序仍正确解析 lookup_join；全过滤成 0 行则报 EMPTY_ENTITY_PROJECTION
    profile = ExternalPhenomenonProfile(
        entities=[
            ExternalEntityProfile(table_name="orders", sample_rows=[{"id": 1, "code": "S001"}]),
            ExternalEntityProfile(table_name="invoices", sample_rows=[{"id": 9, "origin": "S001"}]),
        ]
    )
    reversed_mapping = AdapterMappingSpec(
        entity_mappings=[
            EntityMappingSpec(
                source_table="invoices",
                target_object_type="Invoice",
                id_transform=FieldTransformSpec(target_field="invoice_id", source_field="id", kind="id_prefix", prefix="I-"),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="order",
                        source_field="origin",
                        kind="lookup_join",
                        lookup_table="orders",
                        lookup_key_field="code",
                    )
                ],
            ),
            EntityMappingSpec(
                source_table="orders",
                target_object_type="SalesOrder",
                id_transform=FieldTransformSpec(target_field="order_id", source_field="id", kind="id_prefix", prefix="O-"),
            ),
        ]
    )
    proj, errs = project_external_phenomenon(profile, reversed_mapping)
    assert errs == []
    inv_row = next(r for r in proj if r["object_type"] == "Invoice")
    assert inv_row["data"]["order"] == "O-1"

    vacuous_mapping = AdapterMappingSpec(
        entity_mappings=[
            EntityMappingSpec(
                source_table="orders",
                target_object_type="SalesOrder",
                id_transform=FieldTransformSpec(target_field="order_id", source_field="id", kind="id_prefix", prefix="O-"),
            ),
            EntityMappingSpec(
                source_table="invoices",
                target_object_type="Invoice",
                row_filters=[RowFilterSpec(field="origin", op="eq", value="IMPOSSIBLE")],
                id_transform=FieldTransformSpec(target_field="invoice_id", source_field="id", kind="id_prefix", prefix="I-"),
            ),
        ]
    )
    _, vac_errs = project_external_phenomenon(profile, vacuous_mapping)
    assert any("EMPTY_ENTITY_PROJECTION" in e for e in vac_errs)

    # P1-3: 字符串乘整数必须被拦截（防内存放大 DoS）
    with pytest.raises(InvariantError) as exc_arith:
        eval_predicate(
            {
                "op": "eq",
                "left": {"op": "mul", "left": {"lit": "attack"}, "right": {"lit": 10_000_000}},
                "right": {"lit": 0},
            },
            {},
        )
    assert exc_arith.value.code == "INVALID_ARITHMETIC_OPERAND"

    # P1-4: 关 5 捕获非 *_id 的具体客户名/业务单号硬编码过拟合
    code_overfit = apply_evolution_patch(
        bp,
        EvolutionPatch(
            base_version="1.0.0",
            target_version="1.1.0",
            add_invariants=[
                InvariantRuleSpec(
                    name="name_overfit",
                    code="OVERFIT",
                    target_type="Customer",
                    must_hold={"op": "ne", "left": {"ref": "obj.name"}, "right": {"lit": "Apple Retail"}},
                )
            ],
        ),
    )
    g5_report = SymbolicCrucible().evaluate(
        code_overfit,
        CrucibleContext(
            mode="evolution",
            m0_objects=build_initial_o2c_m0_objects(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        ),
    )
    g5 = next(g for g in g5_report.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert g5.passed is False
    assert any("OVERFITTING_INSTANCE_CODE" in ce for ce in g5.counterexamples)


def test_antigravity_cli_abduction_backend_isolation_reuse_retry_and_single_turn_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify AntigravityCLIAbductionBackend enforces isolated cwd, session reuse, retry, and single-turn guard."""
    import subprocess
    from open_ontology.induction import AntigravityCLIAbductionBackend

    fake_cli = tmp_path / "fake_agy"
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")

    calls: list[dict[str, Any]] = []
    responses = [
        # Round 1: transient EOF on attempt 1, then success on attempt 2
        subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error: Post https://x: EOF"
        ),
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "conversation_id": "conv-cegis-1",
                    "status": "SUCCESS",
                    "num_turns": 1,
                    "duration_seconds": 4.2,
                    "usage": {"input_tokens": 10000, "output_tokens": 80},
                    "response": '{"ok": true, "round": 1}',
                }
            ),
            stderr="",
        ),
        # Round 2: reuses conv-cegis-1
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "conversation_id": "conv-cegis-1",
                    "status": "SUCCESS",
                    "num_turns": 1,
                    "duration_seconds": 3.1,
                    "usage": {"input_tokens": 11000, "output_tokens": 90, "cache_read_tokens": 9000},
                    "response": '```json\n{"ok": true, "round": 2}\n```',
                }
            ),
            stderr="",
        ),
        # Fresh Round 1 of a new cycle: must NOT reuse conv-cegis-1
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "conversation_id": "conv-cegis-2",
                    "status": "SUCCESS",
                    "num_turns": 1,
                    "response": '{"ok": true, "cycle": 2}',
                }
            ),
            stderr="",
        ),
        # Multi-turn tool use attempt: must be rejected by enforce_single_turn
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "conversation_id": "conv-cheat",
                    "status": "SUCCESS",
                    "num_turns": 3,
                    "response": '{"ok": true}',
                }
            ),
            stderr="",
        ),
    ]

    def fake_run(cmd: list[str], *, text: bool, capture_output: bool, cwd: str, timeout: int):
        calls.append({"cmd": list(cmd), "cwd": cwd, "entries": list(Path(cwd).iterdir())})
        return responses.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("open_ontology.induction.backends.time.sleep", lambda _s: None)

    be = AntigravityCLIAbductionBackend(
        model="gemini-4-neon-medium",
        cli_path=str(fake_cli),
        reuse_conversation=True,
        isolate_cwd=True,
        enforce_single_turn=True,
        max_retries=3,
    )

    # Round 1 succeeds after transient EOF retry; runs in an empty isolated temp dir
    out1 = be.propose_json(system_prompt="SYS", user_prompt="U1", round_index=1)
    assert out1 == {"ok": True, "round": 1}
    assert len(calls) == 2
    assert calls[1]["entries"] == []
    assert calls[1]["cwd"] != str(Path.cwd())
    assert "--disable-slash-commands" in calls[1]["cmd"]
    assert "--conversation" not in calls[1]["cmd"]
    assert be.last_conversation_id == "conv-cegis-1"
    assert be.last_usage == {"input_tokens": 10000, "output_tokens": 80}

    # Round 2 reuses conv-cegis-1
    out2 = be.propose_json(system_prompt="SYS", user_prompt="U2", round_index=2)
    assert out2 == {"ok": True, "round": 2}
    assert "--conversation" in calls[2]["cmd"]
    assert calls[2]["cmd"][calls[2]["cmd"].index("--conversation") + 1] == "conv-cegis-1"

    # New cycle round_index=1 resets conversation_id
    out3 = be.propose_json(system_prompt="SYS", user_prompt="U1-new", round_index=1)
    assert out3 == {"ok": True, "cycle": 2}
    assert "--conversation" not in calls[3]["cmd"]
    assert be.last_conversation_id == "conv-cegis-2"

    # Multi-turn (num_turns=3) must be rejected to prevent silent tool smuggling
    with pytest.raises(RuntimeError, match="num_turns=3"):
        be.propose_json(system_prompt="SYS", user_prompt="U-cheat", round_index=1)


def test_strong_typed_proposed_eval_cases_self_check_and_multi_cycle_gate4_lock() -> None:
    """Verify strong-typed ProposedEvalCase validation, Gate 4 self-check, and multi-cycle regression lock."""
    bp_v1 = DeclarativeBlueprint.model_validate(_base_o2c_blueprint(include_unshipped_invoice_guard=True))
    confirm_act = next(a for a in bp_v1.actions if a.name == "confirm_sales_order")
    guarded_confirm = confirm_act.model_copy(
        update={
            "preconditions": list(confirm_act.preconditions)
            + [
                InvariantRuleSpec(
                    name="credit_cap",
                    code="CREDIT_LIMIT_EXCEEDED",
                    target_type="SalesOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "or",
                        "args": [
                            {"op": "ne", "left": {"ref": "actor.role"}, "right": {"lit": "ai_agent"}},
                            {"op": "le", "left": {"ref": "obj.amount"}, "right": {"ref": "obj.customer.credit_limit"}},
                        ],
                    },
                )
            ]
        }
    )
    cluster = FrictionClusterSummary(
        cluster_id="CLU-EVAL-1",
        signal_kind="REVERTED",
        action_type="confirm_sales_order",
        signature="confirm_over_credit",
        support_count=2,
        run_ids=("RUN-1", "RUN-2"),
        escalated=True,
        evidences=(),
    )

    with InductionGovernanceService(db_path=":memory:") as gov:
        # Genesis v1.0.0
        gov.runtime.submit_action(
            "P-GEN",
            "propose_genesis_model",
            {
                "proposal_id": "PROP-GEN",
                "model_id": "erp_o2c",
                "target_version": "1.0.0",
                "blueprint_json": bp_v1.model_dump_json(),
            },
            BUILDER_ACTOR,
        )
        gov.runtime.submit_action(
            "R-GEN",
            "record_falsification_report",
            {
                "report_id": "RPT-GEN",
                "proposal_id": "PROP-GEN",
                "passed": True,
                "blueprint_hash": blueprint_fingerprint(bp_v1),
                "report_json": '{"passed": true}',
            },
            CRUCIBLE_ACTOR,
        )
        gov.request_crystallization("PROP-GEN", BUILDER_ACTOR, run_id="C-GEN")
        gov.approve_crystallization("C-GEN", STEWARD_ACTOR)

        self_contained_setup = [
            {
                "object_type": "Customer",
                "data": {"customer_id": "EVAL-CUST-1", "name": "Eval Customer", "credit_limit": 50000},
            },
            {
                "object_type": "SalesOrder",
                "data": {"order_id": "EVAL-ORD-1", "customer": "EVAL-CUST-1", "amount": 90000, "status": "draft"},
            },
        ]

        # Subcase A & B: Round 1 emits ad-hoc given/when/then -> Gate 1 parse failure;
        # Round 2 emits canonical ProposedEvalCase that contradicts candidate -> Gate 4 self-check rejects;
        # Round 3 fixes expectation to expect valid=False, error_code="CREDIT_LIMIT_EXCEEDED" -> PASS.
        def _three_round_evolver(_s: str, _u: str, round_index: int) -> dict[str, Any]:
            if round_index == 1:
                return {
                    "patch": {
                        "base_version": "1.0.0",
                        "target_version": "1.1.0",
                        "update_actions": [guarded_confirm.model_dump(mode="json")],
                        "proposed_eval_cases": [
                            {"given": {"order": "EVAL-ORD-1"}, "when": "confirm", "then": "reject"}
                        ],
                    }
                }
            if round_index == 2:
                return {
                    "patch": EvolutionPatch(
                        base_version="1.0.0",
                        target_version="1.1.0",
                        update_actions=[guarded_confirm],
                        proposed_eval_cases=[
                            ProposedEvalCase.model_validate(
                                {
                                    "name": "contradictory_case",
                                    "action": "confirm_sales_order",
                                    "actor": {"actor_id": "eval:ai", "role": "ai_agent"},
                                    "params": {"order_id": "EVAL-ORD-1", "order_amount": 90000},
                                    "setup_objects": self_contained_setup,
                                    "expect": {"valid": True},  # contradicts credit_cap guard!
                                }
                            )
                        ],
                    ).model_dump(mode="json")
                }
            return {
                "patch": EvolutionPatch(
                    base_version="1.0.0",
                    target_version="1.1.0",
                    update_actions=[guarded_confirm],
                    proposed_eval_cases=[
                        ProposedEvalCase.model_validate(
                            {
                                "name": "golden_block_eval_over_credit",
                                "action": "confirm_sales_order",
                                "actor": {"actor_id": "eval:ai", "role": "ai_agent"},
                                "params": {"order_id": "EVAL-ORD-1", "order_amount": 90000},
                                "setup_objects": self_contained_setup,
                                "expect": {"valid": False, "error_code": "CREDIT_LIMIT_EXCEEDED"},
                            }
                        )
                    ],
                ).model_dump(mode="json")
            }

        evolver = OntologyEvolverAgent(
            backend=CallableAbductionBackend(_three_round_evolver),
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        res = evolver.run_evolution_cycle(
            model_id="erp_o2c",
            cluster=cluster,
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=build_initial_o2c_m0_objects(),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
                incident_counterexamples=[
                    IncidentCounterexample(
                        name="cx_big",
                        action="confirm_sales_order",
                        params={"order_id": "SO-BIG-01", "order_amount": 150000},
                        actor=AI_AGENT,
                        expect_valid=False,
                    )
                ],
                default_actor=AI_AGENT,
            ),
            max_rounds=4,
        )
        assert res.verified is True
        assert len(res.rounds) == 3
        assert res.rounds[0].failed_gates[0] == "gate_1_static_safety"
        assert "proposed_eval_cases" in res.rounds[0].feedback
        assert res.rounds[1].failed_gates == ("gate_4_golden_regression",)
        assert "proposed_eval_case 'contradictory_case' failed self-check on candidate" in res.rounds[1].feedback

        gov.approve_crystallization(res.pending_approval_run_id, STEWARD_ACTOR)  # type: ignore[arg-type]
        stored_golden = gov.get_golden_eval_cases("erp_o2c")
        assert [c["name"] for c in stored_golden] == ["golden_block_eval_over_credit"]

        # Subcase C: In a subsequent v1.1.0 -> v1.2.0 cycle with empty m0_objects=[],
        # the stored GoldenEvalCase with setup_objects blocks a regressing v1.2.0 patch in Gate 4!
        regressing_patch = EvolutionPatch(
            base_version="1.1.0",
            target_version="1.2.0",
            update_actions=[confirm_act],  # strips credit_cap!
        )
        v1_1_bp = gov.get_active_blueprint("erp_o2c")
        assert v1_1_bp is not None
        reg_res = OntologyEvolverAgent(
            backend=CallableAbductionBackend(lambda *_: {"patch": regressing_patch.model_dump(mode="json")}),
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        ).run_evolution_cycle(
            model_id="erp_o2c",
            cluster=cluster,
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=[],
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
                default_actor=AI_AGENT,
            ),
            max_rounds=1,
        )
        assert reg_res.verified is False
        assert "gate_4_golden_regression" in reg_res.rounds[0].failed_gates
        assert "golden case 'golden_block_eval_over_credit' regressed" in reg_res.rounds[0].feedback


def test_extract_json_object_handles_trailing_extra_data_and_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify JSON raw_decode ignores trailing commentary and HTTP helper enforces wall-clock timeout."""
    import urllib.request
    from open_ontology.induction.backends import (
        _extract_json_object,
        _read_http_body_with_deadline,
    )

    assert _extract_json_object('{"ok": true, "n": 1}\nextra trailing text {"ignored": 2}') == {
        "ok": True,
        "n": 1,
    }
    assert _extract_json_object('```json\n{"a": [1, 2]}\n``` trailing') == {"a": [1, 2]}

    class _SlowResp:
        def __init__(self) -> None:
            self._calls = 0

        def __enter__(self) -> "_SlowResp":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def read(self, _n: int = -1) -> bytes:
            self._calls += 1
            return b'{"chunk": 1'

    ticks = iter([0.0, 0.2, 2.5])
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _SlowResp())
    monkeypatch.setattr("open_ontology.induction.backends.time.monotonic", lambda: next(ticks))

    dummy_req = urllib.request.Request("https://example.invalid", method="POST")
    with pytest.raises(TimeoutError, match="hard wall-clock deadline"):
        _read_http_body_with_deadline(dummy_req, timeout_sec=1.0)


def test_l2_architect_agent_evolves_m1_if_else_and_extra_params() -> None:
    """Verify if_else value AST, apply_adapter_mapping_patch, and OntologyL2ArchitectAgent with Gate 2 hook."""
    from open_ontology.induction import (
        ActionExecutionBinding,
        AdapterMappingSpec,
        EntityMappingSpec,
        FieldTransformSpec,
        L2EvolutionTicket,
        OntologyL2ArchitectAgent,
        apply_adapter_mapping_patch,
    )

    # 1. Verify 'if_else' AST validation + runtime execution
    pay_cond = {
        "op": "le",
        "left": {"op": "sub", "left": {"ref": "obj.amount_residual"}, "right": {"ref": "param.amount"}},
        "right": {"lit": 0.0},
    }
    if_else_ast = {
        "op": "if_else",
        "cond": pay_cond,
        "then": {"lit": "paid"},
        "else": {"lit": "partial"},
    }
    assert validate_predicate_ast(pay_cond) == 4

    base_bp = DeclarativeBlueprint(
        model_id="l2_unit_o2c",
        version="1.0.0",
        description="Minimal L1 baseline lacking payment_state/amount_residual and wizard protocols.",
        object_types=[
            ObjectTypeBlueprint(
                name="AccountMove",
                primary_key="record_id",
                fields=[
                    FieldSpec(name="record_id", field_type="str", primary_key=True),
                    FieldSpec(name="state", field_type="str", default="draft"),
                    FieldSpec(name="amount_total", field_type="float", default=0.0),
                ],
                status_field="state",
                initial_statuses=["draft", "posted"],
                transitions={"draft": ["posted"], "posted": []},
            )
        ],
        actions=[
            ActionBlueprint(
                name="account_move__action_post",
                allowed_roles=["ai_agent", "steward"],
                params=[FieldSpec(name="record_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="must_be_draft",
                        code="INVALID_STATE",
                        target_type="AccountMove",
                        id_param="record_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.state"}, "right": {"lit": "draft"}},
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="AccountMove",
                        id_param="record_id",
                        set_fields={"state": {"lit": "posted"}},
                    )
                ],
            )
        ],
    )
    base_mapping = AdapterMappingSpec(
        mapping_id="l2_unit_o2c_map_v1",
        source_kind="discovered_erp",
        entity_mappings=[
            EntityMappingSpec(
                source_table="account.move",
                target_object_type="AccountMove",
                id_transform=FieldTransformSpec(
                    target_field="record_id", source_field="id", kind="id_prefix", prefix="INV-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="state", source_field="state", kind="direct", default="draft"),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct", default=0.0),
                ],
            )
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="account_move__action_post",
                external_table="account.move",
                external_method="action_post",
                target_object_type="AccountMove",
                id_param="record_id",
                id_prefix="INV-",
                expected_state_field="state",
                expected_state_after="posted",
            )
        ],
    )

    m0_objs = [
        {"object_type": "AccountMove", "data": {"record_id": "INV-1", "state": "draft", "amount_total": 100.0}},
        {"object_type": "AccountMove", "data": {"record_id": "INV-2", "state": "posted", "amount_total": 100.0}},
    ]

    with InductionGovernanceService(db_path=":memory:") as gov:
        # Crystallize v1.0.0 first via Builder
        def _gen_backend(_s: str, _u: str, _r: int) -> dict[str, Any]:
            return {"blueprint": base_bp.model_dump(mode="json"), "adapter_mapping": base_mapping.model_dump(mode="json")}

        builder = OntologyBuilderAgent(backend=CallableAbductionBackend(_gen_backend), governance=gov)
        gen = builder.run_genesis_cycle(
            model_id="l2_unit_o2c",
            phenomenon_brief={"tables": ["account.move"]},
            crucible_context=CrucibleContext(
                mode="genesis",
                m0_objects=m0_objs,
                positive_events=[
                    ReplayEvent(
                        name="POS-POST",
                        action="account_move__action_post",
                        params={"record_id": "INV-1"},
                        actor=AI_AGENT,
                        expected_valid=True,
                    )
                ],
                incident_counterexamples=[
                    IncidentCounterexample(
                        name="CX-ALREADY-POSTED",
                        action="account_move__action_post",
                        params={"record_id": "INV-2"},
                        actor=AI_AGENT,
                        expect_valid=False,
                        expect_error_code="INVALID_STATE",
                    )
                ],
                default_actor=AI_AGENT,
            ),
            max_rounds=1,
        )
        assert gen.verified and gen.pending_approval_run_id is not None
        gov.approve_crystallization(gen.pending_approval_run_id, STEWARD_ACTOR)

        # Round 1 proposes partial patch without protocol_steps (caught by l2_validation_hook); Round 2 fixes it
        def _l2_backend(_s: str, _u: str, round_idx: int) -> dict[str, Any]:
            missing_steps = round_idx == 1
            patch = EvolutionPatch(
                patch_id="PATCH-L2-UNIT",
                base_version="1.0.0",
                target_version="2.0.0",
                rationale="Add partial payment tracking + transient wizard protocol binding.",
                add_fields={
                    "AccountMove": [
                        FieldSpec(name="amount_residual", field_type="float", default=100.0),
                        FieldSpec(name="payment_state", field_type="str", default="not_paid"),
                    ]
                },
                add_actions=[
                    ActionBlueprint(
                        name="register_invoice_payment",
                        allowed_roles=["ai_agent", "steward"],
                        params=[
                            FieldSpec(name="record_id", field_type="str", required=True),
                            FieldSpec(name="amount", field_type="float", required=True),
                        ],
                        preconditions=[
                            InvariantRuleSpec(
                                name="posted_only",
                                code="INVOICE_NOT_POSTED",
                                target_type="AccountMove",
                                id_param="record_id",
                                must_hold={"op": "eq", "left": {"ref": "obj.state"}, "right": {"lit": "posted"}},
                            ),
                            InvariantRuleSpec(
                                name="positive_within_residual",
                                code="INVALID_PAYMENT_AMOUNT",
                                target_type="AccountMove",
                                id_param="record_id",
                                must_hold={
                                    "op": "and",
                                    "args": [
                                        {"op": "gt", "left": {"ref": "param.amount"}, "right": {"lit": 0.0}},
                                        {"op": "le", "left": {"ref": "param.amount"}, "right": {"ref": "obj.amount_residual"}},
                                    ],
                                },
                            ),
                        ],
                        mutations=[
                            ActionMutationSpec(
                                op="modify",
                                object_type="AccountMove",
                                id_param="record_id",
                                set_fields={
                                    "amount_residual": {"op": "sub", "left": {"ref": "obj.amount_residual"}, "right": {"ref": "param.amount"}},
                                    "payment_state": if_else_ast,
                                },
                            )
                        ],
                    )
                ],
                migrations=[
                    FieldMigrationSpec(object_type="AccountMove", field="amount_residual", op="add_default", default=100.0),
                    FieldMigrationSpec(object_type="AccountMove", field="payment_state", op="add_default", default="not_paid"),
                ],
                update_entity_mappings=[
                    EntityMappingSpec(
                        source_table="account.move",
                        target_object_type="AccountMove",
                        id_transform=FieldTransformSpec(target_field="record_id", source_field="id", kind="id_prefix", prefix="INV-"),
                        field_transforms=[
                            FieldTransformSpec(target_field="state", source_field="state", kind="direct", default="draft"),
                            FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct", default=0.0),
                            FieldTransformSpec(target_field="amount_residual", source_field="amount_residual", kind="direct", default=0.0),
                            FieldTransformSpec(target_field="payment_state", source_field="payment_state", kind="direct", default="not_paid"),
                        ],
                    )
                ],
                add_action_bindings=[
                    ActionExecutionBinding(
                        action_name="register_invoice_payment",
                        external_table="account.move",
                        external_method="action_create_payments",
                        target_object_type="AccountMove",
                        id_param="record_id",
                        id_prefix="INV-",
                        extra_params=[] if missing_steps else ["amount"],
                    )
                ],
                proposed_eval_cases=[
                    ProposedEvalCase(
                        name="EVAL-L2-PARTIAL",
                        description="50% payment yields partial; remaining 50% yields paid.",
                        actor={"actor_id": "eval:ai_agent", "role": "ai_agent"},
                        action="register_invoice_payment",
                        params={"record_id": "INV-EVAL", "amount": 40.0},
                        setup_objects=[
                            {
                                "object_type": "AccountMove",
                                "data": {
                                    "record_id": "INV-EVAL",
                                    "state": "posted",
                                    "amount_total": 100.0,
                                    "amount_residual": 100.0,
                                    "payment_state": "not_paid",
                                },
                            }
                        ],
                        expect={"valid": True},
                    )
                ],
            )
            return {"patch": patch.model_dump(mode="json")}

        def _l2_hook(_bp: DeclarativeBlueprint, mp: AdapterMappingSpec | None) -> list[str]:
            if mp is None:
                return ["missing adapter_mapping"]
            pb = mp.get_action_binding("register_invoice_payment")
            if pb is None or "amount" not in pb.extra_params:
                return ["register_invoice_payment binding missing required extra_params=['amount']"]
            return []

        architect = OntologyL2ArchitectAgent(backend=CallableAbductionBackend(_l2_backend), governance=gov)
        res = architect.run_l2_evolution_cycle(
            model_id="l2_unit_o2c",
            l2_tickets=[
                L2EvolutionTicket(
                    ticket_id="TKT-T4-1",
                    probe_id="T4-1",
                    bucket="T4_escape",
                    capability_gap="partial_payment_settlement",
                    title="Partial customer payment",
                    why_l1_insufficient="Requires parameterized payment registration.",
                    external_evidence={},
                    red_test_spec={},
                )
            ],
            external_phenomenon_context={
                "table": "account.move",
                "observed_fields": ["amount_residual", "payment_state"],
            },
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=m0_objs,
                positive_events=[
                    ReplayEvent(name="POS-HIST", action="account_move__action_post", params={"record_id": "INV-1"}, actor=AI_AGENT, expected_valid=True),
                    ReplayEvent(name="POS-PART", action="register_invoice_payment", params={"record_id": "INV-2", "amount": 40.0}, actor=AI_AGENT, expected_valid=True),
                ],
                incident_counterexamples=[
                    IncidentCounterexample(
                        name="CX-OVERPAY",
                        action="register_invoice_payment",
                        params={"record_id": "INV-2", "amount": 200.0},
                        actor=AI_AGENT,
                        expect_valid=False,
                        expect_error_code="INVALID_PAYMENT_AMOUNT",
                    )
                ],
                default_actor=AI_AGENT,
            ),
            max_rounds=3,
            l2_validation_hook=_l2_hook,
        )
        assert len(res.rounds) == 2
        assert res.rounds[0].passed is False
        assert "gate_2_m0_grounding" in res.rounds[0].failed_gates
        assert "extra_params" in res.rounds[0].feedback
        assert res.verified is True and res.pending_approval_run_id is not None

        gov.approve_crystallization(res.pending_approval_run_id, STEWARD_ACTOR)
        active_bp = gov.get_active_blueprint("l2_unit_o2c")
        active_mp = gov.get_active_adapter_mapping("l2_unit_o2c")
        assert active_bp is not None and active_bp.version == "2.0.0"
        assert active_mp is not None
        pay_binding = active_mp.get_action_binding("register_invoice_payment")
        assert pay_binding is not None and pay_binding.extra_params == ["amount"]

        # Runtime check of consecutive 40 + 60 partial->paid if_else evaluation
        rt = OntologyRuntime(compile_blueprint(active_bp), db_path=":memory:")
        try:
            cls = rt.spec.get_object_type("AccountMove")
            rt.put_object(cls(record_id="INV-RUN", state="posted", amount_total=100.0, amount_residual=100.0, payment_state="not_paid"))
            rt.submit_action("PAY-1", "register_invoice_payment", {"record_id": "INV-RUN", "amount": 40.0}, AI_AGENT)
            obj1 = rt.get_object(cls, "INV-RUN", AI_AGENT)
            assert obj1.amount_residual == pytest.approx(60.0)
            assert obj1.payment_state == "partial"
            rt.submit_action("PAY-2", "register_invoice_payment", {"record_id": "INV-RUN", "amount": 60.0}, AI_AGENT)
            obj2 = rt.get_object(cls, "INV-RUN", AI_AGENT)
            assert obj2.amount_residual == pytest.approx(0.0)
            assert obj2.payment_state == "paid"
        finally:
            rt.close()

        # Sanity check direct helper apply_adapter_mapping_patch
        assert res.patch is not None
        patched_mp = apply_adapter_mapping_patch(base_mapping, res.patch)
        assert patched_mp is not None and patched_mp.get_action_binding("register_invoice_payment") is not None


def test_induction_kernel_zero_psm_or_protocol_smuggling() -> None:
    """Enforce pure M2 architecture: zero Odoo/vendor/PSM protocol tokens inside open_ontology/induction/."""
    import re

    repo_root = Path(__file__).resolve().parents[1]
    ind_dir = repo_root / "open_ontology" / "induction"
    forbidden = (
        "odoo",
        "sale.order",
        "stock.picking",
        "account.move",
        "res.partner",
        "purchase.order",
        "bottlecrm",
        "many2one",
        "m2m_ext_id",
        "record_m2o",
        "externalprotocolstep",
        "prepare_child_quantities",
        "call_and_follow_transient",
        "create_and_call_transient",
        "post_created_document",
        "context_active_table",
        "outbox",
        "writeback",
        "target_system",
        "retry_count",
        "last_error",
        "lineage",
    )
    for py_path in sorted(ind_dir.glob("*.py")):
        low = py_path.read_text(encoding="utf-8").lower()
        for tok in forbidden:
            assert tok not in low, f"PSM / vendor / banned IT token {tok!r} leaked into {py_path.name}"

    schema_text = (repo_root / "open_ontology" / "schema.sql").read_text(encoding="utf-8")
    schema_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)", schema_text))
    assert schema_tables == {
        "ontology_write_gate",
        "ontology_registry",
        "ontology_objects",
        "ontology_links",
        "ontology_action_runs",
        "ontology_overlay",
    }
    runtime_lines = len(
        (repo_root / "open_ontology" / "runtime.py").read_text(encoding="utf-8").splitlines()
    )
    assert runtime_lines <= 1350, f"runtime.py exceeded 1350 lines ({runtime_lines})"


def test_stage1_p0_1_precondition_relation_traversal_and_context_validation(tmp_path: Path) -> None:
    bad_global_bp = DeclarativeBlueprint(
        model_id="stage1-bad-global",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Ticket",
                primary_key="ticket_id",
                fields=[
                    FieldSpec(name="ticket_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="owner_id", field_type="str", required=True),
                ],
            )
        ],
        invariants=[
            InvariantRuleSpec(
                name="bad_actor_ref",
                code="BAD_REF",
                target_type="Ticket",
                must_hold={"op": "eq", "left": {"ref": "obj.owner_id"}, "right": {"ref": "actor.actor_id"}},
            )
        ],
    )
    with pytest.raises(InvariantError) as exc_global:
        compile_blueprint(bad_global_bp)
    assert exc_global.value.code == "UNAVAILABLE_REF_IN_CONTEXT"

    bad_create_ref_bp = DeclarativeBlueprint(
        model_id="stage1-bad-create-ref",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Ticket",
                primary_key="ticket_id",
                fields=[
                    FieldSpec(name="ticket_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="amount", field_type="float", required=True),
                ],
            )
        ],
        actions=[
            ActionBlueprint(
                name="create_ticket",
                allowed_roles=["sales", "admin"],
                params=[
                    FieldSpec(name="ticket_id", field_type="str", required=True),
                    FieldSpec(name="amount", field_type="float", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="Ticket",
                        id_param="ticket_id",
                        set_fields={
                            "ticket_id": {"ref": "param.ticket_id"},
                            "amount": {"ref": "obj.amount"},
                        },
                    )
                ],
            )
        ],
    )
    with pytest.raises(InvariantError) as exc_create_ref:
        compile_blueprint(bad_create_ref_bp)
    assert exc_create_ref.value.code == "UNAVAILABLE_REF_IN_CONTEXT"

    gated_bp = DeclarativeBlueprint(
        model_id="stage1-precondition-relation-view",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Customer",
                primary_key="customer_id",
                fields=[
                    FieldSpec(name="customer_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="credit_limit", field_type="float", required=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="SalesOrder",
                primary_key="order_id",
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="amount", field_type="float", required=True),
                    FieldSpec(name="state", field_type="str", required=True),
                ],
                relations=[
                    RelationSpec(field="customer", target_type="Customer", required=True),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="seed_customer",
                allowed_roles=["sales", "manager", "admin"],
                params=[
                    FieldSpec(name="customer_id", field_type="str", required=True),
                    FieldSpec(name="credit_limit", field_type="float", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="Customer",
                        id_param="customer_id",
                        set_fields={
                            "customer_id": {"ref": "param.customer_id"},
                            "credit_limit": {"ref": "param.credit_limit"},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="bump_credit",
                allowed_roles=["manager", "admin"],
                params=[
                    FieldSpec(name="customer_id", field_type="str", required=True),
                    FieldSpec(name="credit_limit", field_type="float", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Customer",
                        id_param="customer_id",
                        set_fields={"credit_limit": {"ref": "param.credit_limit"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="seed_order",
                allowed_roles=["sales", "manager", "admin"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="customer", field_type="str", required=True),
                    FieldSpec(name="amount", field_type="float", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="SalesOrder",
                        id_param="order_id",
                        set_fields={
                            "order_id": {"ref": "param.order_id"},
                            "customer": {"ref": "param.customer"},
                            "amount": {"ref": "param.amount"},
                            "state": {"lit": "draft"},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="confirm_order",
                allowed_roles=["sales", "manager", "admin"],
                revert_roles=["manager", "admin"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="credit_limit_gate",
                        code="APPROVAL_REQUIRED_CREDIT_LIMIT",
                        target_type="SalesOrder",
                        id_param="order_id",
                        when={
                            "op": "gt",
                            "left": {"ref": "obj.amount"},
                            "right": {"ref": "obj.customer.credit_limit"},
                        },
                        must_hold={
                            "op": "in",
                            "left": {"ref": "actor.role"},
                            "right": {"lit": ["manager", "admin"]},
                        },
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SalesOrder",
                        id_param="order_id",
                        set_fields={"state": {"lit": "confirmed"}},
                    )
                ],
            ),
        ],
    )
    spec = compile_blueprint(gated_bp)
    sales = Actor(actor_id="sales-1", role="sales", clearance_tags={"*"})
    mgr = Actor(actor_id="mgr-1", role="manager", clearance_tags={"*"})

    rt = OntologyRuntime(spec, db_path=tmp_path / "approval.db")
    try:
        rt.submit_action("RUN-C1", "seed_customer", {"customer_id": "C1", "credit_limit": 1000.0}, sales)
        rt.submit_action(
            "RUN-SO1",
            "seed_order",
            {"order_id": "SO-1", "customer": "C1", "amount": 1500.0},
            sales,
        )
        rt.submit_action(
            "RUN-SO2",
            "seed_order",
            {"order_id": "SO-2", "customer": "C1", "amount": 500.0},
            sales,
        )

        # Within customer credit_limit -> executes cleanly
        low_res = rt.submit_action("RUN-CFM-2", "confirm_order", {"order_id": "SO-2"}, sales)
        assert low_res["status"] == "COMMITTED"

        # Exceeds obj.customer.credit_limit -> synchronous precondition blocks unprivileged sales actor
        sim = rt.simulate_action("confirm_order", {"order_id": "SO-1"}, sales)
        assert sim.valid is False
        assert sim.error_code == "APPROVAL_REQUIRED_CREDIT_LIMIT"

        with pytest.raises(InvariantError) as exc_over:
            rt.submit_action("RUN-CFM-1", "confirm_order", {"order_id": "SO-1"}, sales)
        assert exc_over.value.code == "APPROVAL_REQUIRED_CREDIT_LIMIT"

        # Manager bumps credit_limit -> now within limit, sales can confirm directly at t1
        rt.submit_action("RUN-BUMP", "bump_credit", {"customer_id": "C1", "credit_limit": 2000.0}, mgr)
        ok_after_bump = rt.submit_action("RUN-CFM-1B", "confirm_order", {"order_id": "SO-1"}, sales)
        assert ok_after_bump["status"] == "COMMITTED"

        # Order exceeding updated limit -> manager commits directly
        rt.submit_action("RUN-SO3", "seed_order", {"order_id": "SO-3", "customer": "C1", "amount": 2500.0}, sales)
        approved3 = rt.submit_action("RUN-CFM-3", "confirm_order", {"order_id": "SO-3"}, mgr)
        assert approved3["status"] == "COMMITTED"
    finally:
        rt.close()


def test_stage1_p0_2_zero_m1_hardcoding_and_custom_seed_ontology(tmp_path: Path) -> None:
    from typing import ClassVar
    from open_ontology import ObjectType, OntologySpec
    from open_ontology.induction import seed_from_spec

    m1_archetype_without_seed = DeclarativeBlueprint(
        model_id="stage1-no-m1-default",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Account",
                archetype="MasterEntity",
                primary_key="account_id",
                fields=[FieldSpec(name="account_id", field_type="str", primary_key=True, required=True)],
            )
        ],
    )
    with pytest.raises(InvariantError) as exc_arch:
        compile_blueprint(m1_archetype_without_seed)
    assert exc_arch.value.code == "UNKNOWN_SEED_ARCHETYPE"

    class FlightTelemetry(ObjectType):
        __abstract__: ClassVar[bool] = True
        __non_negative_fields__: ClassVar[tuple[str, ...]] = ()

    class MissionDirective(ObjectType):
        __abstract__: ClassVar[bool] = True
        __status_field__: ClassVar[str] = "status"
        __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {}

    aero_spec = OntologySpec()
    aero_spec.object_type(FlightTelemetry)
    aero_spec.object_type(MissionDirective)

    @aero_spec.invariant("aero_non_negative_fuel", code="NEGATIVE_FUEL")
    def _inv_fuel(view: Any) -> bool:
        for cls in view.spec.object_types.values():
            for fname in tuple(getattr(cls, "__non_negative_fields__", ()) or ()):
                for obj in view.query(cls):
                    val = getattr(obj, fname, None)
                    if isinstance(val, (int, float)) and not isinstance(val, bool) and val < 0:
                        raise InvariantError(f"{cls.__name__}.{fname} < 0", code="NEGATIVE_FUEL")
        return True

    @aero_spec.transition_invariant("aero_directive_fsm", code="ILLEGAL_DIRECTIVE_STEP")
    def _inv_fsm(diff: Any, view: Any) -> bool:
        for ch in diff.object_changes:
            cls = view.spec.get_object_type(ch.object_type)
            if not issubclass(cls, MissionDirective):
                continue
            if ch.kind == "update":
                old_s = (ch.before or {}).get("status")
                new_s = (ch.after or {}).get("status")
                allowed = (getattr(cls, "__transitions__", {}) or {}).get(old_s, ())
                if old_s != new_s and new_s not in allowed:
                    raise InvariantError(f"{old_s} -> {new_s} illegal", code="ILLEGAL_DIRECTIVE_STEP")
        return True

    aero_seed = seed_from_spec(
        seed_id="aerospace_seed",
        domain_name="aerospace",
        base_spec=aero_spec,
        provides_non_negative=True,
        provides_fsm=True,
    )

    aero_bp = DeclarativeBlueprint(
        model_id="stage1-aero",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="DroneFuel",
                archetype="FlightTelemetry",
                primary_key="telemetry_id",
                fields=[
                    FieldSpec(name="telemetry_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="fuel_kg", field_type="float", required=True, non_negative=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="LaunchTask",
                archetype="MissionDirective",
                primary_key="directive_id",
                fields=[
                    FieldSpec(name="directive_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="status", field_type="str", required=True),
                ],
                status_field="status",
                initial_statuses=["queued"],
                transitions={"queued": ["active"]},
            ),
        ],
        actions=[
            ActionBlueprint(
                name="create_fuel",
                allowed_roles=["ops"],
                params=[
                    FieldSpec(name="telemetry_id", field_type="str", required=True),
                    FieldSpec(name="fuel_kg", field_type="float", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="DroneFuel",
                        id_param="telemetry_id",
                        set_fields={
                            "telemetry_id": {"ref": "param.telemetry_id"},
                            "fuel_kg": {"ref": "param.fuel_kg"},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="create_task",
                allowed_roles=["ops"],
                params=[
                    FieldSpec(name="directive_id", field_type="str", required=True),
                    FieldSpec(name="status", field_type="str", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="LaunchTask",
                        id_param="directive_id",
                        set_fields={
                            "directive_id": {"ref": "param.directive_id"},
                            "status": {"ref": "param.status"},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="step_task",
                allowed_roles=["ops"],
                params=[
                    FieldSpec(name="directive_id", field_type="str", required=True),
                    FieldSpec(name="status", field_type="str", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="LaunchTask",
                        id_param="directive_id",
                        set_fields={"status": {"ref": "param.status"}},
                    )
                ],
            ),
        ],
    )
    compiled = compile_blueprint(aero_bp, seed_ontology=aero_seed)
    assert "declarative_non_negative_fields" not in compiled.invariants
    assert "declarative_fsm_transitions" not in compiled.transition_invariants

    op = Actor(actor_id="flight-op", role="ops", clearance_tags={"*"})
    rt = OntologyRuntime(compiled, db_path=tmp_path / "aero.db")
    try:
        with pytest.raises(InvariantError) as exc_fuel:
            rt.submit_action("RUN-F1", "create_fuel", {"telemetry_id": "D1", "fuel_kg": -5.0}, op)
        assert exc_fuel.value.code == "NEGATIVE_FUEL"

        rt.submit_action("RUN-M1", "create_task", {"directive_id": "M1", "status": "queued"}, op)
        with pytest.raises(InvariantError) as exc_fsm:
            rt.submit_action("RUN-M2", "step_task", {"directive_id": "M1", "status": "done"}, op)
        assert exc_fsm.value.code == "ILLEGAL_DIRECTIVE_STEP"
    finally:
        rt.close()


def test_stage1_p1_1_dsl_arithmetic_aggregates_target_ref_and_evolution(tmp_path: Path) -> None:
    from open_ontology.induction import (
        AdapterMappingSpec,
        EntityMappingSpec,
        FieldTransformSpec,
        apply_adapter_mapping_patch,
    )

    assert eval_predicate(
        {"op": "eq", "left": {"op": "div", "left": {"lit": 600}, "right": {"lit": 3}}, "right": {"lit": 200}},
        {},
    )
    with pytest.raises(InvariantError) as exc_div:
        eval_predicate({"op": "gt", "left": {"op": "div", "left": {"lit": 10}, "right": {"lit": 0}}, "right": {"lit": 0}}, {})
    assert exc_div.value.code == "DIVISION_BY_ZERO"

    bp = DeclarativeBlueprint(
        model_id="stage1-p1-1",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Customer",
                primary_key="customer_id",
                fields=[
                    FieldSpec(name="customer_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="credit_limit", field_type="float", required=True),
                    FieldSpec(name="exposure", field_type="float", required=True),
                    FieldSpec(name="legacy_note", field_type="str", required=False),
                ],
            ),
            ObjectTypeBlueprint(
                name="SalesOrder",
                primary_key="order_id",
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="amount", field_type="float", required=True),
                ],
                relations=[
                    RelationSpec(field="customer", target_type="Customer", required=True),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="seed_customer",
                allowed_roles=["sales"],
                params=[
                    FieldSpec(name="customer_id", field_type="str", required=True),
                    FieldSpec(name="credit_limit", field_type="float", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="Customer",
                        id_param="customer_id",
                        set_fields={
                            "customer_id": {"ref": "param.customer_id"},
                            "credit_limit": {"ref": "param.credit_limit"},
                            "exposure": {"lit": 0.0},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="book_order",
                allowed_roles=["sales"],
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="customer", field_type="str", required=True),
                    FieldSpec(name="amount", field_type="float", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="customer_sum_cap",
                        code="SUM_CAP_EXCEEDED",
                        target_type="Customer",
                        id_param="customer",
                        must_hold={
                            "op": "le",
                            "left": {
                                "op": "add",
                                "left": {
                                    "op": "sum",
                                    "object_type": "SalesOrder",
                                    "field": "amount",
                                    "where": {
                                        "op": "eq",
                                        "left": {"ref": "target.customer"},
                                        "right": {"ref": "param.customer"},
                                    },
                                },
                                "right": {"ref": "param.amount"},
                            },
                            "right": {"lit": 1000.0},
                        },
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="SalesOrder",
                        id_param="order_id",
                        set_fields={
                            "order_id": {"ref": "param.order_id"},
                            "customer": {"ref": "param.customer"},
                            "amount": {"ref": "param.amount"},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="post_order_exposure",
                allowed_roles=["sales"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SalesOrder",
                        id_param="order_id",
                        set_fields={"amount": {"ref": "obj.amount"}},
                    ),
                    ActionMutationSpec(
                        op="modify",
                        object_type="Customer",
                        target_ref="obj.customer",
                        set_fields={
                            "exposure": {
                                "op": "add",
                                "left": {"ref": "obj.exposure"},
                                "right": {"ref": "source_obj.amount"},
                            }
                        },
                    ),
                ],
            ),
        ],
        invariants=[
            InvariantRuleSpec(
                name="max_order_cap",
                code="MAX_ORDER_EXCEEDED",
                target_type="SalesOrder",
                must_hold={
                    "op": "le",
                    "left": {"op": "max", "object_type": "SalesOrder", "field": "amount"},
                    "right": {"lit": 900.0},
                },
            ),
            InvariantRuleSpec(
                name="min_order_floor",
                code="MIN_ORDER_FLOOR",
                target_type="SalesOrder",
                must_hold={
                    "op": "ge",
                    "left": {"op": "min", "object_type": "SalesOrder", "field": "amount"},
                    "right": {"lit": 10.0},
                },
            ),
        ],
    )

    spec = compile_blueprint(bp)
    actor = Actor(actor_id="sales-1", role="sales", clearance_tags={"*"})
    rt = OntologyRuntime(spec, db_path=tmp_path / "p1_1.db")
    try:
        rt.submit_action("RUN-C1", "seed_customer", {"customer_id": "C1", "credit_limit": 2000.0}, actor)
        rt.submit_action("RUN-O1", "book_order", {"order_id": "O1", "customer": "C1", "amount": 600.0}, actor)
        rt.submit_action("RUN-EXP1", "post_order_exposure", {"order_id": "O1"}, actor)
        cust = rt.get_object(spec.get_object_type("Customer"), "C1", actor)
        assert cust.exposure == pytest.approx(600.0)
        # sum(600) + 500 > 1000 precondition fails
        with pytest.raises(InvariantError) as exc_sum:
            rt.submit_action("RUN-O2", "book_order", {"order_id": "O2", "customer": "C1", "amount": 500.0}, actor)
        assert exc_sum.value.code == "SUM_CAP_EXCEEDED"
    finally:
        rt.close()

    # Invalid target_ref checks
    bad_target_ref_bp = bp.model_copy(
        update={
            "actions": [
                ActionBlueprint(
                    name="bad_target",
                    allowed_roles=["sales"],
                    params=[FieldSpec(name="order_id", field_type="str", required=True)],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Customer",
                            target_ref="customer",
                            set_fields={"exposure": {"lit": 0.0}},
                        )
                    ],
                )
            ]
        }
    )
    with pytest.raises(InvariantError) as exc_tref:
        compile_blueprint(bad_target_ref_bp)
    assert exc_tref.value.code in {"INVALID_MUTATION_TARGET_REF", "MISSING_TARGET_REF"}

    # EvolutionPatch: CANNOT_REMOVE_PRIMARY_KEY, FIELD_IN_USE, clean remove_fields + add_relations
    with pytest.raises(InvariantError) as exc_pk:
        apply_evolution_patch(
            bp,
            EvolutionPatch(base_version="1.0.0", target_version="1.1.0", remove_fields={"Customer": ["customer_id"]}),
        )
    assert exc_pk.value.code == "CANNOT_REMOVE_PRIMARY_KEY"

    with pytest.raises(InvariantError) as exc_inuse:
        apply_evolution_patch(
            bp,
            EvolutionPatch(base_version="1.0.0", target_version="1.1.0", remove_fields={"Customer": ["exposure"]}),
        )
    assert exc_inuse.value.code == "FIELD_IN_USE"

    clean_patch = EvolutionPatch(
        base_version="1.0.0",
        target_version="1.1.0",
        add_relations={
            "SalesOrder": [RelationSpec(field="alt_customer", target_type="Customer", required=False)]
        },
        remove_fields={"Customer": ["legacy_note"]},
    )
    evolved = apply_evolution_patch(bp, clean_patch)
    so_bp = next(ot for ot in evolved.object_types if ot.name == "SalesOrder")
    cust_bp = next(ot for ot in evolved.object_types if ot.name == "Customer")
    assert any(r.field == "alt_customer" for r in so_bp.relations)
    assert all(f.name != "legacy_note" for f in cust_bp.fields)
    assert any(m.op == "drop_field" and m.field == "legacy_note" for m in evolved.migrations)

    mapping = AdapterMappingSpec(
        mapping_id="mock-map",
        source_kind="mock",
        entity_mappings=[
            EntityMappingSpec(
                source_table="res.partner",
                target_object_type="Customer",
                id_transform=FieldTransformSpec(target_field="customer_id", source_field="id", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="credit_limit", source_field="credit_limit", kind="direct"),
                    FieldTransformSpec(target_field="exposure", source_field="exposure", kind="direct"),
                    FieldTransformSpec(target_field="legacy_note", source_field="comment", kind="direct"),
                ],
            )
        ],
    )
    patched_mapping = apply_adapter_mapping_patch(mapping, clean_patch)
    assert patched_mapping is not None
    assert all(tf.target_field != "legacy_note" for tf in patched_mapping.entity_mappings[0].field_transforms)


def test_stage1_p1_2_gate5_overfitting_and_categorical_codes() -> None:
    crucible = SymbolicCrucible()
    m0_objs = [
        {
            "object_type": "Customer",
            "object_id": "1042",
            "data": {"customer_id": 1042, "tier_code": "B2B-VIP", "region_code": "US-EAST", "cert_code": "ISO-9001"},
        },
        {
            "object_type": "Customer",
            "object_id": "1043",
            "data": {"customer_id": 1043, "tier_code": "B2B-VIP", "region_code": "US-EAST", "cert_code": "ISO-9001"},
        },
        {
            "object_type": "Customer",
            "object_id": "1044",
            "data": {"customer_id": 1044, "tier_code": "B2B-STD", "region_code": "EU-WEST", "cert_code": "ISO-9001"},
        },
    ]
    base_bp = DeclarativeBlueprint(
        model_id="stage1-gate5",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Customer",
                primary_key="customer_id",
                fields=[
                    FieldSpec(name="customer_id", field_type="int", primary_key=True, required=True),
                    FieldSpec(name="tier_code", field_type="str", required=True),
                    FieldSpec(name="region_code", field_type="str", required=True),
                    FieldSpec(name="cert_code", field_type="str", required=True),
                ],
            )
        ],
    )

    uuid_bp = base_bp.model_copy(
        update={
            "invariants": [
                InvariantRuleSpec(
                    name="uuid_hardcode",
                    code="ERR",
                    target_type="Customer",
                    must_hold={"op": "ne", "left": {"ref": "obj.tier_code"}, "right": {"lit": "550e8400-e29b-41d4-a716-446655440000"}},
                )
            ]
        }
    )
    uuid_res = crucible.evaluate(uuid_bp, CrucibleContext(mode="evolution", m0_objects=m0_objs))
    g5_uuid = next(g for g in uuid_res.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert g5_uuid.passed is False
    assert any("OVERFITTING_INSTANCE_ID" in ce for ce in g5_uuid.counterexamples)

    pk_bp = base_bp.model_copy(
        update={
            "invariants": [
                InvariantRuleSpec(
                    name="pk_hardcode",
                    code="ERR",
                    target_type="Customer",
                    must_hold={"op": "ne", "left": {"ref": "obj.customer_id"}, "right": {"lit": 1042}},
                )
            ]
        }
    )
    pk_res = crucible.evaluate(pk_bp, CrucibleContext(mode="evolution", m0_objects=m0_objs))
    g5_pk = next(g for g in pk_res.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert g5_pk.passed is False
    assert any("OVERFITTING_INSTANCE_ID" in ce for ce in g5_pk.counterexamples)

    cat_bp = base_bp.model_copy(
        update={
            "invariants": [
                InvariantRuleSpec(
                    name="valid_codes",
                    code="ERR",
                    target_type="Customer",
                    must_hold={
                        "op": "and",
                        "args": [
                            {"op": "in", "left": {"ref": "obj.tier_code"}, "right": {"lit": ["B2B-VIP", "B2B-STD"]}},
                            {"op": "in", "left": {"ref": "obj.region_code"}, "right": {"lit": ["US-EAST", "EU-WEST"]}},
                            {"op": "eq", "left": {"ref": "obj.cert_code"}, "right": {"lit": "ISO-9001"}},
                        ],
                    },
                )
            ]
        }
    )
    cat_res = crucible.evaluate(cat_bp, CrucibleContext(mode="evolution", m0_objects=m0_objs))
    g5_cat = next(g for g in cat_res.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert not any("OVERFITTING_INSTANCE_ID" in ce for ce in g5_cat.counterexamples)
    assert g5_cat.passed is True


def test_stage1_p1_3_phenomenon_false_handling_and_p1_4_crucible_encapsulation(tmp_path: Path) -> None:
    from open_ontology.induction import (
        AdapterMappingSpec,
        EntityMappingSpec,
        ExternalEntityProfile,
        ExternalFieldProfile,
        ExternalPhenomenonProfile,
        FieldTransformSpec,
        project_external_phenomenon,
        project_single_record,
    )

    entity_map = EntityMappingSpec(
        source_table="lead",
        target_object_type="Lead",
        id_transform=FieldTransformSpec(target_field="id", source_field="id", kind="direct"),
        field_transforms=[
            FieldTransformSpec(target_field="is_active", source_field="active", kind="direct", default=None),
            FieldTransformSpec(
                target_field="fallback_flag",
                source_field="active",
                kind="coalesce",
                fallback_fields=["backup_flag"],
            ),
            FieldTransformSpec(
                target_field="status_label",
                source_field="active",
                kind="enum_map",
                enum_map={"False": "inactive", "True": "active"},
                default="unknown",
            ),
        ],
    )
    raw = {"id": "L1", "active": False, "backup_flag": True}

    kept = project_single_record(entity_map, raw)
    assert kept["is_active"] is False
    assert kept["fallback_flag"] is False
    assert kept["status_label"] == "inactive"

    nullified = project_single_record(entity_map, raw, treat_false_as_null=True)
    assert nullified["is_active"] is None
    assert nullified["fallback_flag"] is True
    assert nullified["status_label"] == "unknown"

    phenom = ExternalPhenomenonProfile(
        source_kind="sqlite",
        source_locator="sqlite://test",
        entities=[
            ExternalEntityProfile(
                table_name="lead",
                row_count=1,
                fields=[
                    ExternalFieldProfile(name="id", raw_type="string", label="id"),
                    ExternalFieldProfile(name="active", raw_type="integer", label="active"),
                    ExternalFieldProfile(name="backup_flag", raw_type="integer", label="backup_flag"),
                ],
                sample_rows=[raw],
            )
        ],
    )
    proj_default, err_default = project_external_phenomenon(
        phenom,
        AdapterMappingSpec(mapping_id="m-sql", source_kind="sqlite", entity_mappings=[entity_map]),
    )
    assert err_default == []
    assert proj_default[0]["data"]["is_active"] is False

    proj_odoo, err_odoo = project_external_phenomenon(
        phenom,
        AdapterMappingSpec(
            mapping_id="m-odoo",
            source_kind="odoo",
            treat_false_as_null=True,
            entity_mappings=[entity_map],
        ),
    )
    assert err_odoo == []
    assert proj_odoo[0]["data"]["is_active"] is None

    agents_src = (Path(__file__).resolve().parents[1] / "open_ontology" / "induction" / "agents.py").read_text(
        encoding="utf-8"
    )
    assert "._actor" not in agents_src
    assert "crucible._actor" not in agents_src

    gov = InductionGovernanceService(tmp_path / "gov_encap.db")
    try:
        cluster = FrictionClusterSummary(
            cluster_id="cl-stage1",
            signal_kind="ROLLBACK",
            action_type="confirm_order",
            signature="CREDIT_EXCEEDED",
            support_count=3,
            escalated=True,
            run_ids=("R1", "R2", "R3"),
            evidences=(),
        )
        run = SymbolicCrucible().record_friction_cluster(gov, model_id="stage1-model", cluster=cluster)
        assert run["actor_id"] == CRUCIBLE_ACTOR.actor_id
    finally:
        gov.close()


def test_incident_precondition_blocks_unguarded_and_passes_role_conditional_gate() -> None:
    """IncidentCounterexample(expect_valid=False) blocks unguarded commits while passing APPROVAL_REQUIRED_* preconditions."""
    base_fields = [
        FieldSpec(name="deal_id", field_type="str", required=True),
        FieldSpec(name="amount", field_type="float", default=0.0, non_negative=True),
        FieldSpec(name="status", field_type="str", default="open"),
    ]
    m0 = [
        {"object_type": "Deal", "data": {"deal_id": "D-OK", "amount": 50.0, "status": "open"}},
        {"object_type": "Deal", "data": {"deal_id": "D-BIG", "amount": 5000.0, "status": "open"}},
    ]
    inc = IncidentCounterexample(
        name="INC-BIG-DEAL",
        action="close_deal",
        params={"deal_id": "D-BIG"},
        actor=AI_AGENT,
        expect_valid=False,
    )
    pos = [ReplayEvent(name="POS-OK", action="close_deal", params={"deal_id": "D-OK"}, actor=AI_AGENT, expected_valid=True)]

    # 1. Unguarded blueprint fails Gate 3
    unguarded = DeclarativeBlueprint(
        model_id="deal_guard_test",
        version="1.0.0",
        object_types=[ObjectTypeBlueprint(name="Deal", primary_key="deal_id", fields=base_fields)],
        actions=[
            ActionBlueprint(
                name="close_deal",
                allowed_roles=["ai_agent", "steward"],
                params=[FieldSpec(name="deal_id", field_type="str", required=True)],
                mutations=[ActionMutationSpec(op="modify", object_type="Deal", id_param="deal_id", set_fields={"status": {"lit": "closed"}})],
            )
        ],
    )
    r_unguarded = SymbolicCrucible().evaluate(
        unguarded,
        CrucibleContext(mode="evolution", m0_objects=m0, positive_events=pos, incident_counterexamples=[inc], default_actor=AI_AGENT),
    )
    assert not r_unguarded.passed

    # 2. Hard precondition rejection passes Gate 3
    hard_block = DeclarativeBlueprint(
        model_id="deal_guard_test",
        version="1.1.0",
        object_types=[ObjectTypeBlueprint(name="Deal", primary_key="deal_id", fields=base_fields)],
        actions=[
            ActionBlueprint(
                name="close_deal",
                allowed_roles=["ai_agent", "steward"],
                params=[FieldSpec(name="deal_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="limit",
                        code="BIG_DEAL_BLOCKED",
                        target_type="Deal",
                        id_param="deal_id",
                        must_hold={"op": "le", "left": {"ref": "obj.amount"}, "right": {"lit": 1000.0}},
                    )
                ],
                mutations=[ActionMutationSpec(op="modify", object_type="Deal", id_param="deal_id", set_fields={"status": {"lit": "closed"}})],
            )
        ],
    )
    r_hard = SymbolicCrucible().evaluate(
        hard_block,
        CrucibleContext(mode="evolution", m0_objects=m0, positive_events=pos, incident_counterexamples=[inc], default_actor=AI_AGENT),
    )
    assert r_hard.passed

    # 3. Role-conditional APPROVAL_REQUIRED_* synchronous precondition also passes Gate 3
    role_gate = DeclarativeBlueprint(
        model_id="deal_guard_test",
        version="1.2.0",
        object_types=[ObjectTypeBlueprint(name="Deal", primary_key="deal_id", fields=base_fields)],
        actions=[
            ActionBlueprint(
                name="close_deal",
                allowed_roles=["ai_agent", "steward"],
                params=[FieldSpec(name="deal_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="limit_requires_steward",
                        code="APPROVAL_REQUIRED_BIG_DEAL",
                        target_type="Deal",
                        id_param="deal_id",
                        when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 1000.0}},
                        must_hold={"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["steward"]}},
                    )
                ],
                mutations=[ActionMutationSpec(op="modify", object_type="Deal", id_param="deal_id", set_fields={"status": {"lit": "closed"}})],
            )
        ],
    )
    r_appr = SymbolicCrucible().evaluate(
        role_gate,
        CrucibleContext(mode="evolution", m0_objects=m0, positive_events=pos, incident_counterexamples=[inc], default_actor=AI_AGENT),
    )
    assert r_appr.passed



def test_crucible_closed_role_name_column_does_not_false_positive_and_preserves_overfitting_check() -> None:
    """Pure M2 closed-role *_name columns avoid Gate 5 false positives without weakening instance checks."""
    base_roles = ["pilot", "commander", "engineer"]
    object_types = [
        ObjectTypeBlueprint(
            name="CrewSlot",
            primary_key="slot_id",
            fields=[
                FieldSpec(name="slot_id", field_type="str", primary_key=True, required=True),
                FieldSpec(name="slot_role_name", field_type="str", required=True),
            ],
        ),
        ObjectTypeBlueprint(
            name="Vendor",
            primary_key="vendor_id",
            fields=[
                FieldSpec(name="vendor_id", field_type="str", primary_key=True, required=True),
                FieldSpec(name="vendor_name", field_type="str", required=True),
            ],
        ),
    ]
    m0 = [
        {"object_type": "CrewSlot", "object_id": "CS-1", "data": {"slot_id": "CS-1", "slot_role_name": "pilot"}},
        {"object_type": "CrewSlot", "object_id": "CS-2", "data": {"slot_id": "CS-2", "slot_role_name": "commander"}},
        {"object_type": "CrewSlot", "object_id": "CS-3", "data": {"slot_id": "CS-3", "slot_role_name": "engineer"}},
        {"object_type": "Vendor", "object_id": "V-1", "data": {"vendor_id": "V-1", "vendor_name": "Starlight Corp"}},
        {"object_type": "Vendor", "object_id": "V-2", "data": {"vendor_id": "V-2", "vendor_name": "pilot"}},
    ]

    # 1. Closed-role whitelist invariant passes even though "pilot" also appears in Vendor.vendor_name
    clean_bp = DeclarativeBlueprint(
        model_id="pure_m2_closed_role_test",
        version="1.0.0",
        roles=base_roles,
        object_types=object_types,
        invariants=[
            InvariantRuleSpec(
                name="crew_slot_closed_role",
                code="UNKNOWN_ROLE",
                target_type="CrewSlot",
                must_hold={
                    "op": "in",
                    "left": {"ref": "obj.slot_role_name"},
                    "right": {"lit": list(base_roles)},
                },
            )
        ],
    )
    rep_clean = SymbolicCrucible().evaluate(
        clean_bp,
        CrucibleContext(mode="genesis", m0_objects=m0),
    )
    assert rep_clean.passed, rep_clean.diagnostic_feedback()
    g5_clean = next(g for g in rep_clean.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert g5_clean.passed is True
    assert g5_clean.counterexamples == ()

    # 2. Mixed entity-name / role-name column still blocks both ordinary and role-colliding literals
    for bad_literal in ("Starlight Corp", "pilot"):
        overfit_bp = DeclarativeBlueprint(
            model_id="pure_m2_overfit_vendor_test",
            version="1.0.0",
            roles=base_roles,
            object_types=object_types,
            invariants=[
                InvariantRuleSpec(
                    name="overfit_vendor_literal",
                    code="OVERFIT_VENDOR",
                    target_type="Vendor",
                    must_hold={
                        "op": "eq",
                        "left": {"ref": "obj.vendor_name"},
                        "right": {"lit": bad_literal},
                    },
                )
            ],
        )
        rep_bad = SymbolicCrucible().evaluate(
            overfit_bp,
            CrucibleContext(mode="genesis", m0_objects=m0),
        )
        assert not rep_bad.passed
        g5_bad = next(g for g in rep_bad.gates if g.gate == "gate_5_parsimony_anti_overfitting")
        assert g5_bad.passed is False
        assert any("OVERFITTING_INSTANCE_CODE" in ce and bad_literal in ce for ce in g5_bad.counterexamples), g5_bad.counterexamples


def test_crucible_std_h2r_blueprint_with_live_m0_objects_passes_gate_5() -> None:
    """Real std_h2r blueprint grounded on live H2R M0 rows passes SymbolicCrucible directly."""
    from examples.std_h2r_ontology import build_std_h2r_blueprint

    bp = build_std_h2r_blueprint()
    m0 = [
        {
            "object_type": "AccountingEntity",
            "object_id": "ENT-1",
            "data": {"entity_id": "ENT-1", "name": "Titan Holdings", "base_currency": "CNY", "status": "active"},
        },
        {
            "object_type": "Department",
            "object_id": "DEPT-1",
            "data": {"department_id": "DEPT-1", "name": "HQ", "scope_tag": "hq", "status": "active", "accounting_entity": "ENT-1", "parent_department": None},
        },
        {
            "object_type": "Department",
            "object_id": "DEPT-2",
            "data": {"department_id": "DEPT-2", "name": "ops", "scope_tag": "ops", "status": "active", "accounting_entity": "ENT-1", "parent_department": "DEPT-1"},
        },
        {
            "object_type": "JobPosition",
            "object_id": "POS-1",
            "data": {"position_id": "POS-1", "title": "Finance Director", "role_name": "finance_director", "approval_ceiling_cents": 50000000, "status": "active", "department": "DEPT-1"},
        },
        {
            "object_type": "JobPosition",
            "object_id": "POS-2",
            "data": {"position_id": "POS-2", "title": "Sales Manager", "role_name": "sales_manager", "approval_ceiling_cents": 20000000, "status": "active", "department": "DEPT-2"},
        },
        {
            "object_type": "JobPosition",
            "object_id": "POS-3",
            "data": {"position_id": "POS-3", "title": "Ops Specialist", "role_name": "ops", "approval_ceiling_cents": 5000000, "status": "active", "department": "DEPT-2"},
        },
        {
            "object_type": "Employee",
            "object_id": "EMP-1",
            "data": {"employee_id": "EMP-1", "full_name": "Alice", "actor_id": "emp_a", "status": "active", "department": "DEPT-2", "position": "POS-3"},
        },
    ]
    report = SymbolicCrucible().evaluate(
        bp,
        CrucibleContext(
            mode="genesis",
            m0_objects=m0,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        ),
    )
    assert report.passed, report.diagnostic_feedback()
    g2 = next(g for g in report.gates if g.gate == "gate_2_m0_grounding")
    g5 = next(g for g in report.gates if g.gate == "gate_5_parsimony_anti_overfitting")
    assert g2.passed is True
    assert g5.passed is True
    assert g5.counterexamples == ()
