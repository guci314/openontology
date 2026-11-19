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
import tempfile
from pathlib import Path
from typing import Any

import pytest

from examples.o2c_induction_demo import (
    AI_AGENT,
    FINANCE_DIRECTOR,
    _base_o2c_blueprint,
    build_initial_o2c_m0_objects,
    run_o2c_induction_demo,
)
from open_ontology import (
    Actor,
    InvariantError,
    LatentSpace,
    ObjectNotFoundError,
    OntologyRuntime,
    PermissionDeniedError,
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
    spec = compile_blueprint(bp)

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
        assert exc_fsm.value.code == "ILLEGAL_STATE_TRANSITION"

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
        CrucibleContext(mode="evolution", m0_objects=build_initial_o2c_m0_objects()),
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
    spec = compile_blueprint(base)
    prod = OntologyRuntime(spec, db_path=":memory:")
    try:
        for row in build_initial_o2c_m0_objects():
            prod.put_object(spec.get_object_type(row["object_type"])(**row["data"]))
        fp_before = prod.state_fingerprint()

        # 把所有 ai_agent 确认都拦成审批 -> 回归破坏黄金小单用例
        patch = EvolutionPatch(
            base_version="1.0.0",
            target_version="1.1.0",
            update_approval_rules={
                "confirm_sales_order": {
                    "op": "eq",
                    "left": {"ref": "actor.role"},
                    "right": {"lit": "ai_agent"},
                }
            },
        )
        candidate = apply_evolution_patch(base, patch)
        ctx = CrucibleContext(
            mode="evolution",
            production_runtime=prod,
            golden_eval_cases=[
                {
                    "name": "small_order_no_approval",
                    "actor": {"actor_id": "agent:o2c-ops", "role": "ai_agent", "clearance_tags": ["*"]},
                    "action": "confirm_sales_order",
                    "params": {"order_id": "SO-SMALL-01", "order_amount": 20000},
                    "expect": {"valid": True, "requires_approval": False},
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
    overfit_patch = EvolutionPatch(
        base_version="1.0.0",
        target_version="1.1.0",
        update_approval_rules={
            "confirm_sales_order": {
                "op": "eq",
                "left": {"ref": "param.order_id"},
                "right": {"lit": "SO-BIG-01"},
            }
        },
    )
    candidate = apply_evolution_patch(base, overfit_patch)
    report = SymbolicCrucible().evaluate(
        candidate,
        CrucibleContext(mode="evolution", m0_objects=build_initial_o2c_m0_objects()),
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
    spec = compile_blueprint(bp)
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        for row in build_initial_o2c_m0_objects():
            rt.put_object(spec.get_object_type(row["object_type"])(**row["data"]))

        # 单次偶发 DEVICE_STATE_MISMATCH
        with tempfile.TemporaryDirectory() as tmpdir:
            with LatentSpace(rt, Path(tmpdir) / "latent.sqlite3") as latent:
                latent.stage_move(
                    "MISMATCH-1",
                    "confirm_sales_order",
                    {"order_id": "SO-SMALL-01", "order_amount": 20000},
                    AI_AGENT,
                )
                latent.settle_move(
                    "MISMATCH-1",
                    readback_objects={
                        "SalesOrder": [
                            {
                                "order_id": "SO-SMALL-01",
                                "customer": "CUST-APPLE",
                                "amount": 20000,
                                "status": "draft",
                            }
                        ]
                    },
                )

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
            )
            with pytest.raises(InvariantError) as exc_noise:
                evolver.run_evolution_cycle(
                    model_id="erp_o2c",
                    cluster=by_kind["DEVICE_STATE_MISMATCH"],
                    crucible_context=CrucibleContext(mode="evolution"),
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
            gov.runtime.submit_action(
                "CHEAT-CRYSTAL",
                "crystallize_model",
                {"proposal_id": "PROP-1"},
                BUILDER_ACTOR,
            )
        assert exc_unverified.value.code == "UNVERIFIED_PROPOSAL"

        # 4) Crucible 签发通过报告后，Builder 申请结晶 -> 强制挂起 PENDING_APPROVAL
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
        pending = gov.runtime.submit_action(
            "CRYSTAL-1",
            "crystallize_model",
            {"proposal_id": "PROP-1"},
            BUILDER_ACTOR,
        )
        assert pending["status"] == "PENDING_APPROVAL"
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
        gov.runtime.submit_action("CRYSTAL-2", "crystallize_model", {"proposal_id": "PROP-2"}, EVOLVER_ACTOR)
        gov.approve_crystallization("CRYSTAL-2", STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.1.0"

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
        )
        res = builder.run_genesis_cycle(
            model_id="erp_o2c",
            crucible_context=CrucibleContext(
                mode="genesis",
                m0_objects=build_initial_o2c_m0_objects(),
                default_actor=AI_AGENT,
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
    spec = compile_blueprint(bp)
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
            CrucibleContext(mode="evolution", production_runtime=prod, default_actor=SALES_AGENT),
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
            CrucibleContext(mode="evolution", production_runtime=prod, default_actor=SALES_AGENT),
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
        gov.runtime.submit_action("C1", "crystallize_model", {"proposal_id": "PROP-V1"}, BUILDER_ACTOR)
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
        gov.runtime.submit_action("C-V11", "crystallize_model", {"proposal_id": "PROP-V11"}, EVOLVER_ACTOR)
        gov.approve_crystallization("C-V11", STEWARD_ACTOR)
        assert gov.get_active_blueprint("erp_o2c").version == "1.1.0"
        assert gov.get_active_adapter_mapping("erp_o2c").mapping_id == "map-v1"

        # 基于过期 base_version="1.0.0" 的第二个并发提案必须被拒绝（STALE_BASE_VERSION）
        with pytest.raises(InvariantError) as exc_stale:
            gov.runtime.submit_action("C-V11-ALT", "crystallize_model", {"proposal_id": "PROP-V11-ALT"}, EVOLVER_ACTOR)
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
        gov.runtime.submit_action("C-V12", "crystallize_model", {"proposal_id": "PROP-V12"}, EVOLVER_ACTOR)
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
    spec = compile_blueprint(bp)
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
        CrucibleContext(mode="evolution", m0_objects=build_initial_o2c_m0_objects()),
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
    monkeypatch.setattr("open_ontology.induction.agents.time.sleep", lambda _s: None)

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
        gov.runtime.submit_action("C-GEN", "crystallize_model", {"proposal_id": "PROP-GEN"}, BUILDER_ACTOR)
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
        )
        res = evolver.run_evolution_cycle(
            model_id="erp_o2c",
            cluster=cluster,
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=build_initial_o2c_m0_objects(),
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
        ).run_evolution_cycle(
            model_id="erp_o2c",
            cluster=cluster,
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=[],
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
    from open_ontology.induction.agents import (
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
    monkeypatch.setattr("open_ontology.induction.agents.time.monotonic", lambda: next(ticks))

    dummy_req = urllib.request.Request("https://example.invalid", method="POST")
    with pytest.raises(TimeoutError, match="hard wall-clock deadline"):
        _read_http_body_with_deadline(dummy_req, timeout_sec=1.0)
