"""端到端演示：LLM 溯因提议 + 符号沙箱证伪结晶（Ontology Builder & Ontology Evolver）。

三幕完整呈现 OMG MOF ``M0 -> M1`` 神经符号归纳闭环：

* **第一幕 · Ontology Builder（本体建造师 · 0→1 测绘创世）**：
  观察 O2C 原始现象与对抗负样本；第 1 轮漏写“未发货禁开票”不变量被
  ``SymbolicCrucible`` 关 3 打回；第 2 轮补齐跨 ``ObjectProperty`` 规则后五关全绿，
  经 ``Ontology Steward`` 审批结晶为 **M1 v1.0.0** 并锁入黄金回归集。
* **第二幕 · M0 生产世界摩擦与噪声过滤**：
  在 v1.0.0 生产世界运行中，1 次偶发 ``DEVICE_STATE_MISMATCH`` 因未达支持度阈值被
  ``FrictionCollector`` 自动过滤；2 笔超 10 万大单被 ``ai_agent`` 误确认后遭财务总监
  ``revert_action`` 冲正，聚类达到阈值触发进化警报。
* **第三幕 · Ontology Evolver（本体进化师 · 1→N 法医修宪）**：
  - 第 1 轮企图硬编码订单号特判（过拟合）→ 被 **关 5（奥卡姆剃刀）** 抓现行打回；
  - 第 2 轮一刀切拦截所有 AI 确认（灾难性遗忘）→ 被 **关 4（run_eval_suite 黄金回归锁）** 打回；
  - 第 3 轮提炼出通用大额审批法则 + 存量 M0 字段迁移 + 新增黄金用例 → 五关全绿、生产库零污染，
    经 ``Steward`` 审批结晶为 **M1 v1.1.0**！

运行方式::

    python3 -m examples.o2c_induction_demo --offline
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from open_ontology import Actor, DeviceStateMismatchError, OntologyRuntime, SourceBridge
from open_ontology.induction import (
    AbductionBackend,
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
    FrictionCollector,
    GeminiAbductionBackend,
    IncidentCounterexample,
    InductionCycleResult,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    OntologyEvolverAgent,
    OpenAICompatibleAbductionBackend,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
)
from .business_ontology import BUSINESS_SEED_ONTOLOGY

AI_AGENT = Actor(actor_id="agent:o2c-ops", role="ai_agent", clearance_tags=frozenset({"*"}))
FINANCE_DIRECTOR = Actor(
    actor_id="human:finance-dir", role="finance_director", clearance_tags=frozenset({"*"})
)


# ---------------------------------------------------------------------------
# 初始外部现象（M0 观测样本）与离线确定性溯因剧本
# ---------------------------------------------------------------------------
def build_initial_o2c_m0_objects() -> list[dict[str, Any]]:
    return [
        {
            "object_type": "Customer",
            "object_id": "CUST-APPLE",
            "data": {
                "customer_id": "CUST-APPLE",
                "name": "Apple Retail",
                "credit_limit": 100000,
            },
        },
        {
            "object_type": "SalesOrder",
            "object_id": "SO-SMALL-01",
            "data": {
                "order_id": "SO-SMALL-01",
                "customer": "CUST-APPLE",
                "amount": 20000,
                "status": "draft",
            },
        },
        {
            "object_type": "SalesOrder",
            "object_id": "SO-SHIPPED-01",
            "data": {
                "order_id": "SO-SHIPPED-01",
                "customer": "CUST-APPLE",
                "amount": 30000,
                "status": "shipped",
            },
        },
        {
            "object_type": "SalesOrder",
            "object_id": "SO-BIG-01",
            "data": {
                "order_id": "SO-BIG-01",
                "customer": "CUST-APPLE",
                "amount": 150000,
                "status": "draft",
            },
        },
        {
            "object_type": "SalesOrder",
            "object_id": "SO-BIG-02",
            "data": {
                "order_id": "SO-BIG-02",
                "customer": "CUST-APPLE",
                "amount": 180000,
                "status": "draft",
            },
        },
    ]


def _confirm_sales_order_action_bp(*extra_preconditions: InvariantRuleSpec) -> ActionBlueprint:
    return ActionBlueprint(
        name="confirm_sales_order",
        description="确认草稿销售订单",
        allowed_roles=["ai_agent", "finance_director", "admin"],
        approver_roles=["finance_director", "admin"],
        affects_source=True,
        display_label="确认销售订单",
        params=[
            FieldSpec(name="order_id", field_type="str", required=True),
            FieldSpec(name="order_amount", field_type="int", required=True),
        ],
        preconditions=[
            InvariantRuleSpec(
                name="order_amount_matches_record",
                code="ORDER_AMOUNT_MISMATCH",
                target_type="SalesOrder",
                must_hold={
                    "op": "eq",
                    "left": {"ref": "obj.amount"},
                    "right": {"ref": "param.order_amount"},
                },
                description="动作入参金额必须与订单账面金额一致。",
            ),
            *extra_preconditions,
        ],
        mutations=[
            ActionMutationSpec(
                op="modify",
                object_type="SalesOrder",
                id_param="order_id",
                set_fields={"status": {"lit": "confirmed"}},
            )
        ],
    )


def _base_o2c_blueprint(*, include_unshipped_invoice_guard: bool) -> dict[str, Any]:
    invariants: list[dict[str, Any]] = []
    if include_unshipped_invoice_guard:
        invariants.append(
            InvariantRuleSpec(
                name="no_invoice_without_shipment",
                code="CANNOT_INVOICE_UNSHIPPED_ORDER",
                target_type="Invoice",
                must_hold={
                    "op": "in",
                    "left": {"ref": "obj.order.status"},
                    "right": {"lit": ["shipped", "invoiced"]},
                },
                description="未发货订单严禁开具应收发票（跨 ObjectProperty 校验关联订单状态）。",
            ).model_dump(mode="json")
        )

    bp = DeclarativeBlueprint(
        model_id="erp_o2c",
        version="1.0.0",
        description="由 Ontology Builder 测绘提炼的标准 O2C 订单到收款本体",
        object_types=[
            ObjectTypeBlueprint(
                name="Customer",
                archetype="MasterEntity",
                primary_key="customer_id",
                title_field="name",
                display_label="客户主数据",
                fields=[
                    FieldSpec(name="customer_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(name="credit_limit", field_type="int", default=0, non_negative=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="SalesOrder",
                archetype="ProcessDocument",
                primary_key="order_id",
                title_field="order_id",
                display_label="销售订单",
                initial_statuses=["draft"],
                transitions={
                    "draft": ["confirmed"],
                    "confirmed": ["shipped"],
                    "shipped": ["invoiced"],
                },
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="amount", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(
                        field="customer",
                        target_type="Customer",
                        required=True,
                        description="下单客户",
                    )
                ],
            ),
            ObjectTypeBlueprint(
                name="Invoice",
                archetype="ProcessDocument",
                primary_key="invoice_id",
                title_field="invoice_id",
                display_label="应收发票",
                initial_statuses=["posted"],
                transitions={"posted": ["paid"]},
                fields=[
                    FieldSpec(
                        name="invoice_id",
                        field_type="str",
                        primary_key=True,
                        auto_id=True,
                        required=True,
                    ),
                    FieldSpec(name="amount", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="posted"),
                ],
                relations=[
                    RelationSpec(
                        field="order",
                        target_type="SalesOrder",
                        required=True,
                        description="对应销售订单",
                    )
                ],
            ),
        ],
        actions=[
            _confirm_sales_order_action_bp(),
            ActionBlueprint(
                name="ship_sales_order",
                description="确认订单出库发货",
                allowed_roles=["ai_agent", "admin"],
                affects_source=True,
                display_label="订单出库发货",
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SalesOrder",
                        id_param="order_id",
                        set_fields={"status": {"lit": "shipped"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="issue_invoice",
                description="针对已发货订单开具应收发票",
                allowed_roles=["ai_agent", "finance_director", "admin"],
                affects_source=True,
                display_label="开具应收发票",
                params=[
                    FieldSpec(name="invoice_id", field_type="str", required=True),
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="amount", field_type="int", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="Invoice",
                        id_param="invoice_id",
                        set_fields={
                            "invoice_id": {"ref": "param.invoice_id"},
                            "order": {"ref": "param.order_id"},
                            "amount": {"ref": "param.amount"},
                            "status": {"lit": "posted"},
                        },
                    )
                ],
            ),
        ],
        invariants=[InvariantRuleSpec.model_validate(i) for i in invariants],
    )
    return bp.model_dump(mode="json")


def make_offline_builder_backend() -> CallableAbductionBackend:
    """离线 Builder 剧本：第 1 轮漏写不变量被关 3 抓出，第 2 轮补齐后五关全绿。"""

    def _propose(_sys: str, _user: str, round_index: int) -> dict[str, Any]:
        golden_v1 = [
            {
                "name": "small_order_auto_confirm",
                "actor": {"actor_id": "agent:o2c-ops", "role": "ai_agent", "clearance_tags": ["*"]},
                "action": "confirm_sales_order",
                "params": {"order_id": "SO-SMALL-01", "order_amount": 20000},
                "expect": {
                    "valid": True,
                    "object_changes": [
                        {
                            "object_type": "SalesOrder",
                            "object_id": "SO-SMALL-01",
                            "kind": "update",
                            "after": {"status": "confirmed"},
                        }
                    ],
                },
            },
            {
                "name": "cannot_invoice_unshipped_order",
                "actor": {"actor_id": "agent:o2c-ops", "role": "ai_agent", "clearance_tags": ["*"]},
                "action": "issue_invoice",
                "params": {"invoice_id": "INV-BAD-01", "order_id": "SO-SMALL-01", "amount": 20000},
                "expect": {
                    "valid": False,
                    "error_contains": "CANNOT_INVOICE_UNSHIPPED_ORDER",
                },
            },
        ]
        if round_index == 1:
            return {
                "rationale": "初版测绘：先对齐 Customer / SalesOrder / Invoice 原型与状态机。",
                "blueprint": _base_o2c_blueprint(include_unshipped_invoice_guard=False),
                "proposed_eval_cases": golden_v1[:1],
            }
        return {
            "rationale": "根据关 3 反例补充 no_invoice_without_shipment 跨对象不变量。",
            "blueprint": _base_o2c_blueprint(include_unshipped_invoice_guard=True),
            "proposed_eval_cases": golden_v1,
        }

    return CallableAbductionBackend(_propose)


def make_offline_evolver_backend() -> CallableAbductionBackend:
    """离线 Evolver 剧本：
    第 1 轮硬编码订单号过拟合 -> 被关 5 打回；
    第 2 轮一刀切拦截所有 AI 确认 -> 被关 4 黄金回归锁打回；
    第 3 轮产出通用金额审批法则 + M0 字段迁移 + 黄金用例 -> 五关全绿。
    """

    def _propose(_sys: str, _user: str, round_index: int) -> dict[str, Any]:
        if round_index == 1:
            # 典型作弊：对涉事订单号硬编码特判（能骗过关 1~4，唯独被关 5 奥卡姆剃刀抓现行！）
            return {
                "patch": EvolutionPatch(
                    base_version="1.0.0",
                    target_version="1.1.0",
                    rationale="偷懒特判：只要求非总监处理涉事订单 SO-BIG-01 / SO-BIG-02 时拦截审批。",
                    update_actions=[
                        _confirm_sales_order_action_bp(
                            InvariantRuleSpec(
                                name="overfit_big_order_ids",
                                code="APPROVAL_REQUIRED_ORDER_AMOUNT_EXCEEDED",
                                target_type="SalesOrder",
                                id_param="order_id",
                                when={
                                    "op": "in",
                                    "left": {"ref": "param.order_id"},
                                    "right": {"lit": ["SO-BIG-01", "SO-BIG-02"]},
                                },
                                must_hold={
                                    "op": "in",
                                    "left": {"ref": "actor.role"},
                                    "right": {"lit": ["finance_director", "admin"]},
                                },
                                description="仅针对具体订单号硬编码签批拦截。",
                            )
                        )
                    ],
                ).model_dump(mode="json")
            }

        if round_index == 2:
            # 矫枉过正：把所有 ai_agent 确认订单全拦成审批 -> 破坏小单自动确认黄金用例（关 4 打回！）
            return {
                "patch": EvolutionPatch(
                    base_version="1.0.0",
                    target_version="1.1.0",
                    rationale="过度收紧：所有非财务总监提交的确认单全部强审批拦截。",
                    update_actions=[
                        _confirm_sales_order_action_bp(
                            InvariantRuleSpec(
                                name="block_all_ai_orders",
                                code="APPROVAL_REQUIRED_ORDER_AMOUNT_EXCEEDED",
                                target_type="SalesOrder",
                                id_param="order_id",
                                must_hold={
                                    "op": "in",
                                    "left": {"ref": "actor.role"},
                                    "right": {"lit": ["finance_director", "admin"]},
                                },
                                description="无条件拦截非财务总监确认任意销售订单。",
                            )
                        )
                    ],
                ).model_dump(mode="json")
            }

        # 第 3 轮：通用金额法则 + M0 存量无损迁移 + 新黄金回归用例
        return {
            "patch": EvolutionPatch(
                base_version="1.0.0",
                target_version="1.1.0",
                rationale="通用法医修宪：非财务总监确认超 10 万大单以 APPROVAL_REQUIRED_ORDER_AMOUNT_EXCEEDED 前置拦截（转 M1 审批流），并为存量订单补充 credit_review_note 治理字段。",
                add_fields={
                    "SalesOrder": [
                        FieldSpec(
                            name="credit_review_note",
                            field_type="str",
                            default="",
                            owned=True,
                            description="超额信用审查备注",
                        )
                    ]
                },
                migrations=[
                    FieldMigrationSpec(
                        object_type="SalesOrder",
                        op="add_default",
                        field="credit_review_note",
                        default="",
                    )
                ],
                update_actions=[
                    _confirm_sales_order_action_bp(
                        InvariantRuleSpec(
                            name="big_order_requires_finance_director",
                            code="APPROVAL_REQUIRED_ORDER_AMOUNT_EXCEEDED",
                            target_type="SalesOrder",
                            id_param="order_id",
                            when={
                                "op": "gt",
                                "left": {"ref": "param.order_amount"},
                                "right": {"lit": 100000},
                            },
                            must_hold={
                                "op": "in",
                                "left": {"ref": "actor.role"},
                                "right": {"lit": ["finance_director", "admin"]},
                            },
                            description="超过 100,000 的订单确认需财务总监或管理员签批。",
                        )
                    )
                ],
                proposed_eval_cases=[
                    {
                        "name": "big_order_requires_finance_approval",
                        "actor": {
                            "actor_id": "agent:o2c-ops",
                            "role": "ai_agent",
                            "clearance_tags": ["*"],
                        },
                        "action": "confirm_sales_order",
                        "params": {"order_id": "SO-BIG-01", "order_amount": 150000},
                        "expect": {
                            "valid": False,
                            "error_code": "APPROVAL_REQUIRED_ORDER_AMOUNT_EXCEEDED",
                        },
                    }
                ],
            ).model_dump(mode="json")
        }

    return CallableAbductionBackend(_propose)


def _select_backends(offline: bool) -> tuple[AbductionBackend, AbductionBackend, str]:
    if not offline:
        if os.environ.get("GEMINI_API_KEY"):
            b = GeminiAbductionBackend()
            return b, b, "gemini"
        if os.environ.get("OPENAI_API_KEY"):
            b = OpenAICompatibleAbductionBackend()
            return b, b, "openai-compatible"
    return make_offline_builder_backend(), make_offline_evolver_backend(), "offline-deterministic"


# ---------------------------------------------------------------------------
# 完整三幕闭环执行器
# ---------------------------------------------------------------------------
def run_o2c_induction_demo(*, offline: bool = True) -> dict[str, Any]:
    builder_backend, evolver_backend, backend_mode = _select_backends(offline=offline)
    m0_seed = build_initial_o2c_m0_objects()

    with InductionGovernanceService(db_path=":memory:") as governance:
        crucible = SymbolicCrucible()

        # ===================================================================
        # 第一幕：Ontology Builder（0 -> 1 测绘创世 + 关 3 打回修正 + 结晶 v1.0.0）
        # ===================================================================
        genesis_ctx = CrucibleContext(
            mode="genesis",
            m0_objects=m0_seed,
            positive_events=[
                ReplayEvent(
                    name="pos_confirm_small_order",
                    action="confirm_sales_order",
                    params={"order_id": "SO-SMALL-01", "order_amount": 20000},
                    actor=AI_AGENT,
                    expected_valid=True,
                ),
                ReplayEvent(
                    name="pos_invoice_shipped_order",
                    action="issue_invoice",
                    params={"invoice_id": "INV-OK-01", "order_id": "SO-SHIPPED-01", "amount": 30000},
                    actor=AI_AGENT,
                    expected_valid=True,
                ),
            ],
            negative_events=[
                ReplayEvent(
                    name="neg_invoice_before_ship",
                    action="issue_invoice",
                    params={"invoice_id": "INV-ILLEGAL-01", "order_id": "SO-SMALL-01", "amount": 20000},
                    actor=AI_AGENT,
                    expected_valid=False,
                    expected_error_code="CANNOT_INVOICE_UNSHIPPED_ORDER",
                ),
            ],
            default_actor=AI_AGENT,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )

        builder = OntologyBuilderAgent(
            backend=builder_backend,
            governance=governance,
            crucible=crucible,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        genesis_res: InductionCycleResult = builder.run_genesis_cycle(
            model_id="erp_o2c",
            phenomenon_brief={
                "source_tables": ["customers", "sales_orders", "invoices"],
                "sop_rules": [
                    "Draft sales orders transition draft -> confirmed -> shipped -> invoiced.",
                    "Unshipped sales orders must NEVER be invoiced.",
                ],
                "sample_objects": m0_seed[:3],
            },
            crucible_context=genesis_ctx,
            max_rounds=3,
        )
        assert genesis_res.verified and genesis_res.pending_approval_run_id is not None
        governance.approve_crystallization(genesis_res.pending_approval_run_id, STEWARD_ACTOR)
        v1_blueprint = governance.get_active_blueprint("erp_o2c")
        assert v1_blueprint is not None and v1_blueprint.version == "1.0.0"

        # ===================================================================
        # 第二幕：基于结晶 v1.0.0 运行生产 M0 世界 -> 产生偶发噪声 + 系统性大单回滚摩擦
        # ===================================================================
        v1_spec = compile_blueprint(v1_blueprint, seed_ontology=BUSINESS_SEED_ONTOLOGY)
        prod_rt = OntologyRuntime(v1_spec, db_path=":memory:")
        try:
            for item in m0_seed:
                cls = v1_spec.get_object_type(item["object_type"])
                prod_rt.put_object(cls(**item["data"]))

            # 2a) 偶发单点噪声：1 次外部回读 DEVICE_STATE_MISMATCH（不应唤醒 Evolver）
            prod_rt.bind_source_bridge(
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
            try:
                prod_rt.submit_action(
                    "NOISE-MISMATCH-1",
                    "confirm_sales_order",
                    {"order_id": "SO-SMALL-01", "order_amount": 20000},
                    AI_AGENT,
                )
            except DeviceStateMismatchError:
                pass
            prod_rt.bind_source_bridge(None, world_mode="standalone")

            # 2b) 系统性规则漏洞：ai_agent 连续确认 2 笔超 10 万大单，财务总监事后紧急冲正
            prod_rt.submit_action(
                "RUN-BIG-1",
                "confirm_sales_order",
                {"order_id": "SO-BIG-01", "order_amount": 150000},
                AI_AGENT,
            )
            prod_rt.revert_action("RUN-BIG-1", "REV-BIG-1", FINANCE_DIRECTOR)

            prod_rt.submit_action(
                "RUN-BIG-2",
                "confirm_sales_order",
                {"order_id": "SO-BIG-02", "order_amount": 180000},
                AI_AGENT,
            )
            prod_rt.revert_action("RUN-BIG-2", "REV-BIG-2", FINANCE_DIRECTOR)

            collector = FrictionCollector(min_support=2)
            clusters = collector.collect(
                prod_rt,
                revert_reasons={
                    "RUN-BIG-1": "exceeds_100k_credit_approval_threshold",
                    "RUN-BIG-2": "exceeds_100k_credit_approval_threshold",
                },
            )
            noise_clusters = [c for c in clusters if not c.escalated]
            escalated_clusters = [c for c in clusters if c.escalated]
            assert len(noise_clusters) == 1
            assert len(escalated_clusters) == 1
            target_cluster = escalated_clusters[0]

            # ===================================================================
            # 第三幕：Ontology Evolver（1 -> N 法医修宪：关 5 防过拟合 -> 关 4 防遗忘 -> 结晶 v1.1.0）
            # ===================================================================
            prod_fp_before = prod_rt.state_fingerprint()
            evo_ctx = CrucibleContext(
                mode="evolution",
                production_runtime=prod_rt,
                incident_counterexamples=[
                    IncidentCounterexample(
                        name="inc_big_order_1_must_require_approval",
                        action="confirm_sales_order",
                        params={"order_id": "SO-BIG-01", "order_amount": 150000},
                        actor=AI_AGENT,
                        expect_blocked_or_approval=True,
                    ),
                    IncidentCounterexample(
                        name="inc_big_order_2_must_require_approval",
                        action="confirm_sales_order",
                        params={"order_id": "SO-BIG-02", "order_amount": 180000},
                        actor=AI_AGENT,
                        expect_blocked_or_approval=True,
                    ),
                    IncidentCounterexample(
                        name="inc_director_can_confirm_directly",
                        action="confirm_sales_order",
                        params={"order_id": "SO-BIG-01", "order_amount": 150000},
                        actor=FINANCE_DIRECTOR,
                        expect_valid=True,
                    ),
                ],
                default_actor=AI_AGENT,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )

            evolver = OntologyEvolverAgent(
                backend=evolver_backend,
                governance=governance,
                crucible=crucible,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            evo_res: InductionCycleResult = evolver.run_evolution_cycle(
                model_id="erp_o2c",
                cluster=target_cluster,
                crucible_context=evo_ctx,
                max_rounds=4,
            )
            prod_fp_after = prod_rt.state_fingerprint()
            assert prod_fp_before == prod_fp_after
            assert evo_res.verified and evo_res.pending_approval_run_id is not None

            governance.approve_crystallization(evo_res.pending_approval_run_id, STEWARD_ACTOR)
            v1_1_blueprint = governance.get_active_blueprint("erp_o2c")
            assert v1_1_blueprint is not None and v1_1_blueprint.version == "1.1.0"
            golden_cases = governance.get_golden_eval_cases("erp_o2c")

            return {
                "backend_mode": backend_mode,
                "genesis_rounds": [
                    {
                        "round": r.round_index,
                        "passed": r.passed,
                        "failed_gates": list(r.failed_gates),
                    }
                    for r in genesis_res.rounds
                ],
                "noise_filtered_clusters": len(noise_clusters),
                "escalated_cluster_id": target_cluster.cluster_id,
                "escalated_support_count": target_cluster.support_count,
                "evolution_rounds": [
                    {
                        "round": r.round_index,
                        "passed": r.passed,
                        "failed_gates": list(r.failed_gates),
                    }
                    for r in evo_res.rounds
                ],
                "production_untouched": prod_fp_before == prod_fp_after,
                "final_active_version": v1_1_blueprint.version,
                "golden_eval_case_count": len(golden_cases),
                "golden_eval_case_names": [c["name"] for c in golden_cases],
            }
        finally:
            prod_rt.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="OpenOntology Neuro-Symbolic Induction Demo")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force deterministic offline abduction backends (no API key needed)",
    )
    args = parser.parse_args(argv)
    summary = run_o2c_induction_demo(offline=args.offline)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
