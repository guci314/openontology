"""验收测试：面向 Agent 的客观实用性评测器（Step 3）+ 双回路 L1/L2 演化闭环。

覆盖：
1. M2 纯洁性守卫：``open_ontology/induction/utility.py`` 零 Odoo / 零禁用 IT 词汇；
2. 反偷渡硬门禁（Anti-Smuggling Gate）：
   - 捕获把风控/业务规则偷渡进外部适配器的作弊本体（``ADAPTER_LOGIC_SMUGGLING``）；
   - 捕获空 ``object_diff`` 的 1-bit 黑盒万能按钮作弊本体（``BLACKBOX_NO_STATE_DIFF``）；
3. 四桶数学指标防刷分校验：高误杀（FPR）惩罚、失败任务不拉低 ``t1_avg_steps``、帕累托前沿与严格改进判定；
4. 离线端到端 4 体系评测 + L1 自动进化（v1.0 -> v1.1）+ L2 工单分诊；
5. 真实克隆 Odoo 19（``erp_north``）端到端评测：验证生产源库零写入、克隆库彻底销毁、
   真实 15% 税率/部分发货抽象泄漏检出、以及 L1 进化后效用严格单调提升。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError, _jsonrpc
from examples.odoo_utility_benchmark import run_odoo_utility_benchmark
from open_ontology import Actor, ObjectType, OntologySpec
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    AntiSmugglingCheckResult,
    AntiSmugglingProbeSpec,
    DeclarativeBlueprint,
    FieldSpec,
    ObjectTypeBlueprint,
    UtilityConstraints,
    UtilityProbeOutcome,
    compare_scorecards,
    compile_blueprint,
    evaluate_utility_scorecard,
    is_strict_utility_improvement,
    measure_agent_complexity,
    triage_utility_failures,
    verify_anti_smuggling_parity,
)


def test_utility_module_m2_purity() -> None:
    path = Path(__file__).resolve().parents[1] / "open_ontology" / "induction" / "utility.py"
    text = path.read_text(encoding="utf-8").lower()
    for forbidden in ("odoo", "res.partner", "sale.order", "stock.picking", "account.move"):
        assert forbidden not in text, f"Odoo PSM 词条 {forbidden!r} 不得泄漏进 M2 utility.py"
    for banned_it in ("outbox", "writeback", "target_system", "retry_count", "last_error", "lineage"):
        assert banned_it not in text, f"禁用 IT 词条 {banned_it!r} 不得泄漏进 M2 utility.py"


def test_anti_smuggling_catches_smuggled_business_rule() -> None:
    """Ontology omits credit-limit rule (offline COMMITTED) while online adapter rejects -> caught!"""
    bp = DeclarativeBlueprint(
        model_id="leaky_order_model",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="OrderDoc",
                archetype="ObjectType",
                primary_key="order_id",
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True),
                    FieldSpec(name="amount", field_type="float", default=0.0),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                status_field="status",
                transitions={"draft": ["confirmed"]},
            )
        ],
        actions=[
            ActionBlueprint(
                name="confirm_order",
                allowed_roles=["ai_agent"],
                params=[FieldSpec(name="order_id", field_type="str", required=True)],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="OrderDoc",
                        id_param="order_id",
                        set_fields={"status": {"lit": "confirmed"}},
                    )
                ],
            )
        ],
    )
    spec = compile_blueprint(bp)
    m0 = [{"object_type": "OrderDoc", "data": {"order_id": "ORD-1", "amount": 500_000.0, "status": "draft"}}]
    actor = Actor(actor_id="agent:1", role="ai_agent")

    res = verify_anti_smuggling_parity(
        spec,
        m0,
        [
            AntiSmugglingProbeSpec(
                case_id="SMUGGLE-1",
                description="Online adapter secretly blocked high-value order that ontology allowed",
                action_name="confirm_order",
                params={"order_id": "ORD-1"},
                actor=actor,
                online_status="REJECTED",
                online_error_code="ADAPTER_SECRET_LIMIT",
            )
        ],
    )
    assert res.passed is False
    assert len(res.violations) == 1
    assert res.violations[0].violation_code == "ADAPTER_LOGIC_SMUGGLING"

    sc = evaluate_utility_scorecard(
        regime_name="smuggler",
        complexity=measure_agent_complexity(spec, regime_name="smuggler"),
        anti_smuggling=res,
        probes=[],
    )
    assert sc.hard_constraints_passed is False
    assert "ANTI_SMUGGLING_GATE_FAILED" in sc.failed_hard_constraints
    routing = triage_utility_failures(sc)
    assert len(routing.l2_evolution_tickets) == 1
    assert routing.l2_evolution_tickets[0].capability_gap == "adapter_logic_smuggling"


def test_anti_smuggling_catches_blackbox_god_action() -> None:
    """A 1-bit 'execute_everything()' god action that produces empty object_diff is disqualified."""
    spec = OntologySpec()
    spec.role("ai_agent", "AI agent")

    class DummyToken(ObjectType):
        __primary_key__ = "token_id"
        token_id: str
        label: str = "ok"

    spec.object_type(DummyToken)

    class MagicParams(BaseModel):
        goal: str

    @spec.action("execute_magic_goal", MagicParams, allowed_roles=("ai_agent",))
    def _noop_handler(ctx, params: MagicParams) -> None:
        return None  # Black-box pass-through: mutates zero ontology objects!

    m0 = [{"object_type": "DummyToken", "data": {"token_id": "T-1", "label": "ok"}}]
    actor = Actor(actor_id="agent:1", role="ai_agent")
    res = verify_anti_smuggling_parity(
        spec,
        m0,
        [
            AntiSmugglingProbeSpec(
                case_id="GOD-BTN-1",
                description="Black-box god action with zero state diff",
                action_name="execute_magic_goal",
                params={"goal": "run entire o2c"},
                actor=actor,
                online_status="COMMITTED",
                expect_state_mutation=True,
            )
        ],
    )
    assert res.passed is False
    assert len(res.violations) == 1
    assert res.violations[0].violation_code == "BLACKBOX_NO_STATE_DIFF"


def test_scorecard_t1_t2_t3_t4_math_and_false_positive_penalty() -> None:
    bp = DeclarativeBlueprint(
        model_id="math_check",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="Item",
                archetype="ObjectType",
                primary_key="item_id",
                fields=[FieldSpec(name="item_id", field_type="str", primary_key=True)],
            )
        ],
    )
    spec = compile_blueprint(bp)
    comp = measure_agent_complexity(spec, regime_name="paranoid", reference_token_budget=10_000)
    ok_as = AntiSmugglingCheckResult(passed=True, checked_cases=2)

    # Paranoid system: fails T1-2 (steps=0), blocks both adversarial AND legitimate T2 controls (FPR=1.0)
    sc_paranoid = evaluate_utility_scorecard(
        regime_name="paranoid",
        complexity=comp,
        anti_smuggling=ok_as,
        probes=[
            UtilityProbeOutcome(probe_id="T1-1", bucket="T1_routine", title="ok", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, steps_used=3),
            UtilityProbeOutcome(probe_id="T1-2", bucket="T1_routine", title="fail", expected_outcome="ok", observed_outcome="fail", passed=False, external_ledger_verified=True, steps_used=0),
            UtilityProbeOutcome(probe_id="T2-1", bucket="T2_governance", title="adv", expected_outcome="block", observed_outcome="block", passed=True, external_ledger_verified=True, is_adversarial=True),
            UtilityProbeOutcome(probe_id="T2-2", bucket="T2_governance", title="legit", expected_outcome="allow", observed_outcome="wrongly_blocked", passed=False, external_ledger_verified=True, is_adversarial=False),
            UtilityProbeOutcome(probe_id="T3-1", bucket="T3_leakage", title="leak", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, homomorphic_mismatch=False),
            UtilityProbeOutcome(probe_id="T4-1", bucket="T4_escape", title="esc", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, escaped_to_raw_system=False),
        ],
        constraints=UtilityConstraints(max_escape_rate=0.5),
    )
    # Failed T1-2 (steps=0) MUST NOT drag t1_avg_steps down from 3.0 to 1.5
    assert sc_paranoid.t1_task_sr == pytest.approx(0.5)
    assert sc_paranoid.t1_avg_steps == pytest.approx(3.0)
    # Paranoid false-positive friction clearly exposed
    assert sc_paranoid.t2_guardrail_recall == pytest.approx(1.0)
    assert sc_paranoid.t2_guardrail_precision == pytest.approx(0.5)
    assert sc_paranoid.t2_false_positive_rate == pytest.approx(1.0)

    # Better system with FPR=0 and T1=1.0 dominates paranoid
    sc_clean = evaluate_utility_scorecard(
        regime_name="clean",
        complexity=comp,
        anti_smuggling=ok_as,
        probes=[
            UtilityProbeOutcome(probe_id="T1-1", bucket="T1_routine", title="ok", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, steps_used=2),
            UtilityProbeOutcome(probe_id="T1-2", bucket="T1_routine", title="ok", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, steps_used=2),
            UtilityProbeOutcome(probe_id="T2-1", bucket="T2_governance", title="adv", expected_outcome="block", observed_outcome="block", passed=True, external_ledger_verified=True, is_adversarial=True),
            UtilityProbeOutcome(probe_id="T2-2", bucket="T2_governance", title="legit", expected_outcome="allow", observed_outcome="allow", passed=True, external_ledger_verified=True, is_adversarial=False),
            UtilityProbeOutcome(probe_id="T3-1", bucket="T3_leakage", title="leak", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, homomorphic_mismatch=False),
            UtilityProbeOutcome(probe_id="T4-1", bucket="T4_escape", title="esc", expected_outcome="ok", observed_outcome="ok", passed=True, external_ledger_verified=True, escaped_to_raw_system=False),
        ],
    )
    assert is_strict_utility_improvement(sc_paranoid, sc_clean) is True
    assert is_strict_utility_improvement(sc_clean, sc_paranoid) is False
    cmp = compare_scorecards([sc_paranoid, sc_clean], baseline_name="paranoid")
    assert cmp.pareto_frontier == ("clean",)


def test_odoo_utility_benchmark_offline_end_to_end() -> None:
    out = run_odoo_utility_benchmark(offline=True)
    assert out["mode"] == "offline-physical-mirror"

    sc_raw = out["scorecards"]["raw_odoo_baseline"]
    sc_v1 = out["scorecards"]["autonomous_fde_v1.0"]
    sc_v11 = out["scorecards"]["autonomous_fde_v1.1_evolved"]
    sc_hw = out["scorecards"]["handwritten_o2c_reference"]

    # Raw baseline: high C_agent, 3 dirty writes, F1=0
    assert sc_raw["metrics"]["t2_dirty_external_writes"] == 3
    assert sc_raw["metrics"]["t2_guardrail_f1"] == 0.0
    assert sc_raw["complexity"]["osdk_tool_schema_tokens_est"] > sc_v1["complexity"]["osdk_tool_schema_tokens_est"] * 5

    # Anti-Smuggling passes on all ontology regimes
    assert sc_v1["anti_smuggling"]["passed"] is True
    assert sc_v11["anti_smuggling"]["passed"] is True
    assert sc_hw["anti_smuggling"]["passed"] is True

    # Auto FDE v1.0 honestly catches 2 dirty writes and routes them to L1 Evolver
    assert sc_v1["metrics"]["t2_dirty_external_writes"] == 2
    assert sc_v1["metrics"]["t1_avg_steps"] == 1.0
    assert out["failure_routing"]["autonomous_fde_v1.0"]["l1_repair_count"] == 1

    # L1 Evolver strictly improves v1.0 -> v1.1 with zero C_agent increase and zero dirty writes
    l1 = out["l1_evolution_closure"]
    assert l1["crucible_passed"] is True
    assert l1["strict_utility_improvement_proven"] is True
    assert l1["v1_0_dirty_writes"] == 2
    assert l1["v1_1_dirty_writes"] == 0
    assert sc_v11["metrics"]["t2_guardrail_f1"] == 1.0
    assert sc_v11["metrics"]["t2_false_positive_rate"] == 0.0
    assert sc_v11["complexity"]["osdk_tool_schema_tokens_est"] == sc_v1["complexity"]["osdk_tool_schema_tokens_est"]
    assert out["failure_routing"]["autonomous_fde_v1.1_evolved"]["l1_repair_count"] == 0

    # L2-evolved handwritten reference closes all 5 L2 tickets and passes ALL hard constraints!
    assert sc_hw["metrics"]["t1_task_sr"] == 1.0
    assert sc_hw["metrics"]["t2_guardrail_f1"] == 1.0
    assert sc_hw["metrics"]["t2_dirty_external_writes"] == 0
    assert sc_hw["metrics"]["t3_homomorphic_fidelity"] == 1.0
    assert sc_hw["metrics"]["t4_escape_rate"] == 0.0
    assert sc_hw["hard_constraints_passed"] is True
    assert sc_hw["failed_hard_constraints"] == []
    assert out["failure_routing"]["handwritten_o2c_reference"]["l2_ticket_count"] == 0

    # Both v1.0 and raw_odoo_baseline are strictly dominated and knocked off the Pareto frontier
    assert "autonomous_fde_v1.0" not in out["comparison"]["pareto_frontier"]
    assert "raw_odoo_baseline" not in out["comparison"]["pareto_frontier"]
    assert "autonomous_fde_v1.1_evolved" in out["comparison"]["pareto_frontier"]
    assert "handwritten_o2c_reference" in out["comparison"]["pareto_frontier"]


def test_l2_evolved_o2c_ontology_invariants_tax_partial_ship_partial_pay_and_refund() -> None:
    """Verify all 4 new L2 engineering capabilities in examples/o2c_ontology.py (pure :memory:)."""
    from examples import o2c_ontology as o2c
    from open_ontology import InvariantError, OntologyRuntime, PermissionDeniedError

    rt = OntologyRuntime(o2c.spec, db_path=":memory:")
    try:
        rt.put_object(o2c.Customer(customer_id="C1", name="Acme", credit_limit_cents=10_000_000))
        rt.put_object(o2c.ReceivableBalance(ledger_id=o2c.ar_ledger_id("C1"), customer="C1", ar_balance_cents=0))

        # 1. Tax arithmetic (15% VAT -> 1500 bps): 10 * 100,000 = 1,000,000 untaxed + 150,000 tax = 1,150,000
        rt.submit_action(
            "NEW-TAX",
            "create_sales_order",
            {
                "order_id": "SO-L2-1",
                "customer": "C1",
                "quantity": 10,
                "unit_price_cents": 100_000,
                "tax_rate_bps": 1500,
            },
            o2c.AI_AGENT,
        )
        ord1 = rt.get_object(o2c.SalesOrder, "SO-L2-1", o2c.FINANCE_DIRECTOR)
        assert ord1.tax_rate_bps == 1500
        assert ord1.tax_cents == 150_000
        assert ord1.total_cents == 1_150_000

        # 2. Confirm -> non-approver ai_agent MUST be denied revert_action (Fix #1)
        rt.submit_action("CONF-1", "confirm_sales_order", {"order": "SO-L2-1"}, o2c.AI_AGENT)
        with pytest.raises(PermissionDeniedError):
            rt.revert_action("CONF-1", "CONF-1-REV-AI", o2c.AI_AGENT)

        # 3. Partial shipment 4/10 -> partially_shipped; cannot invoice yet; then ship remaining 6/10 -> shipped
        rt.submit_action("SHIP-P1", "ship_order_delivery", {"order": "SO-L2-1", "ship_quantity": 4}, o2c.WAREHOUSE)
        assert rt.get_object(o2c.SalesOrder, "SO-L2-1", o2c.FINANCE_DIRECTOR).status == "partially_shipped"
        deliv = rt.get_object(o2c.Delivery, "DEL-SO-L2-1", o2c.FINANCE_DIRECTOR)
        assert deliv.status == "partial" and deliv.shipped_qty == 4 and deliv.ordered_qty == 10
        with pytest.raises(InvariantError) as inv_err:
            rt.submit_action("INV-EARLY", "issue_and_post_invoice", {"order": "SO-L2-1"}, o2c.AI_AGENT)
        assert inv_err.value.code == "CANNOT_INVOICE_UNSHIPPED_ORDER"

        rt.submit_action("SHIP-P2", "ship_order_delivery", {"order": "SO-L2-1", "ship_quantity": 6}, o2c.WAREHOUSE)
        assert rt.get_object(o2c.SalesOrder, "SO-L2-1", o2c.FINANCE_DIRECTOR).status == "shipped"
        assert rt.get_object(o2c.Delivery, "DEL-SO-L2-1", o2c.FINANCE_DIRECTOR).shipped_qty == 10

        # 4. Invoice carries 1,150,000 (incl. 150,000 tax); default payment still enforces exact match
        rt.submit_action("INV-1", "issue_and_post_invoice", {"order": "SO-L2-1"}, o2c.AI_AGENT)
        inv = rt.get_object(o2c.Invoice, "INV-SO-L2-1", o2c.FINANCE_DIRECTOR)
        assert inv.amount_cents == 1_150_000 and inv.tax_cents == 150_000
        assert o2c.get_customer_ar_balance(rt, "C1") == 1_150_000

        with pytest.raises(InvariantError) as pay_err:
            rt.submit_action("PAY-TYPO", "record_customer_payment", {"order": "SO-L2-1", "payment_cents": 500_000}, o2c.AI_AGENT)
        assert pay_err.value.code == "PAYMENT_AMOUNT_MISMATCH"

        # 5. Explicit partial installment 500,000 -> status='partial', AR=650,000
        rt.submit_action(
            "PAY-1",
            "record_customer_payment",
            {"order": "SO-L2-1", "payment_cents": 500_000, "allow_partial": True},
            o2c.AI_AGENT,
        )
        inv = rt.get_object(o2c.Invoice, "INV-SO-L2-1", o2c.FINANCE_DIRECTOR)
        assert inv.status == "partial" and inv.paid_cents == 500_000
        assert o2c.get_customer_ar_balance(rt, "C1") == 650_000

        # 6. Credit note refund requires finance_director approval -> clears remaining 650,000 AR cleanly!
        pending_ref = rt.submit_action(
            "REF-1",
            "issue_credit_note_refund",
            {"order": "SO-L2-1", "reason": "Damaged goods"},
            o2c.AI_AGENT,
        )
        assert pending_ref["status"] == "PENDING_APPROVAL"
        approved_ref = rt.approve_action("REF-1", o2c.FINANCE_DIRECTOR)
        assert approved_ref["status"] == "COMMITTED"
        inv = rt.get_object(o2c.Invoice, "INV-SO-L2-1", o2c.FINANCE_DIRECTOR)
        assert inv.status == "refunded" and inv.refunded_cents == 1_150_000
        assert rt.get_object(o2c.SalesOrder, "SO-L2-1", o2c.FINANCE_DIRECTOR).status == "refunded"
        assert o2c.get_customer_ar_balance(rt, "C1") == 0
    finally:
        rt.close()


def test_odoo_utility_benchmark_live_cloned_odoo19_zero_leak() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    dbs_before = set(_jsonrpc("db", "list", []))
    counts_before = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    out = run_odoo_utility_benchmark(source_db=DB_NORTH, offline=False)

    dbs_after = set(_jsonrpc("db", "list", []))
    counts_after = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    # Zero writes to production erp_north & ephemeral clone cleanly dropped
    assert out["mode"] == "live-cloned-odoo-19"
    assert dbs_before == dbs_after
    assert out["sandbox_db"] not in dbs_after
    assert counts_before == counts_after

    sc_raw = out["scorecards"]["raw_odoo_baseline"]
    sc_v1 = out["scorecards"]["autonomous_fde_v1.0"]
    sc_v11 = out["scorecards"]["autonomous_fde_v1.1_evolved"]
    sc_hw = out["scorecards"]["handwritten_o2c_reference"]

    # Verified compression (>90% token reduction vs raw O2C schema) & Anti-Smuggling pass
    assert sc_v11["complexity"]["osdk_tool_schema_tokens_est"] < sc_raw["complexity"]["osdk_tool_schema_tokens_est"] * 0.15
    assert sc_v1["anti_smuggling"]["passed"] is True
    assert sc_v11["anti_smuggling"]["passed"] is True
    assert sc_hw["anti_smuggling"]["passed"] is True

    # L1 Evolver strictly improves autonomous FDE ontology on real cloned Odoo 19
    l1 = out["l1_evolution_closure"]
    assert l1["strict_utility_improvement_proven"] is True
    assert l1["v1_0_dirty_writes"] == 2
    assert l1["v1_1_dirty_writes"] == 0
    assert sc_v11["metrics"]["t2_guardrail_f1"] == 1.0
    assert sc_v11["metrics"]["t3_homomorphic_fidelity"] == 0.5  # passes real 15% VAT, leaks backorder

    # L2-evolved handwritten o2c_ontology closes all 5 L2 tickets on live Odoo 19 & passes ALL hard constraints!
    assert sc_hw["metrics"]["t1_task_sr"] == 1.0
    assert sc_hw["metrics"]["t2_guardrail_f1"] == 1.0
    assert sc_hw["metrics"]["t2_dirty_external_writes"] == 0
    assert sc_hw["metrics"]["t3_homomorphic_fidelity"] == 1.0
    assert sc_hw["metrics"]["t4_escape_rate"] == 0.0
    assert sc_hw["hard_constraints_passed"] is True
    assert sc_hw["failed_hard_constraints"] == []
    t31_hw = next(p for p in sc_hw["probes"] if p["probe_id"] == "T3-1")
    assert t31_hw["passed"] is True
    assert t31_hw["external_evidence"]["predicted_cents"] == 2_300_000
    assert t31_hw["external_evidence"]["odoo_physical_cents"] == 2_300_000
    t41_hw = next(p for p in sc_hw["probes"] if p["probe_id"] == "T4-1")
    assert t41_hw["passed"] is True
    assert t41_hw["external_evidence"]["payment_state"] == "partial"
    assert t41_hw["external_evidence"]["amount_residual"] == 10_000.0
    t42_hw = next(p for p in sc_hw["probes"] if p["probe_id"] == "T4-2")
    assert t42_hw["passed"] is True
    assert t42_hw["external_evidence"]["credit_note_state"] == "posted"

    # Both v1.0 and raw_odoo_baseline are strictly dominated off the Pareto frontier
    assert "autonomous_fde_v1.0" not in out["comparison"]["pareto_frontier"]
    assert "raw_odoo_baseline" not in out["comparison"]["pareto_frontier"]
    assert "autonomous_fde_v1.1_evolved" in out["comparison"]["pareto_frontier"]
    assert "handwritten_o2c_reference" in out["comparison"]["pareto_frontier"]


def test_l2_evolved_autonomous_regime_offline_closes_t1_t3_t4_gaps() -> None:
    """Verify autonomous FDE v1.1 upgraded via L2 EvolutionPatch + protocol_steps closes T1-3, T3-2, T4-1, T4-2."""
    from examples.odoo_fde_autonomous_pipeline import (
        build_closed_phenomenon_profile,
        build_business_seed_ontology,
        synthesize_from_fde_evidence,
    )
    from examples.odoo_fde_prober import OdooActiveProber
    from examples.odoo_fde_scout import OdooFDEScout
    from examples.odoo_utility_benchmark import (
        OfflineOdooPhysicalWorld,
        PhysicalOdooHarness,
        build_reference_l1_patch_for_autonomous_v1,
        build_reference_l2_patch_for_autonomous_v11,
        evaluate_autonomous_fde_regime,
        evaluate_raw_odoo_baseline,
    )
    from open_ontology.induction import apply_adapter_mapping_patch, apply_evolution_patch

    world = OfflineOdooPhysicalWorld()
    harness = PhysicalOdooHarness(offline_world=world)
    _, _, raw_total_models, raw_total_fields = harness.measure_raw_schema_counts()
    seed = build_business_seed_ontology()

    sr = OdooFDEScout(world, source_locator="offline://l2_eval", max_models=6).scout()
    profile = build_closed_phenomenon_profile(sr, client=world)
    pr = OdooActiveProber(world, sandbox_db="offline_l2").probe(sr)
    bundle = synthesize_from_fde_evidence(
        sr, pr, profile=profile, seed_ontology=seed, model_id="auto_o2c_l2", version="1.0.0"
    )
    _, raw_baseline_tokens = evaluate_raw_odoo_baseline(harness)

    out_v1 = run_odoo_utility_benchmark(offline=True)
    l1_patch = build_reference_l1_patch_for_autonomous_v1(bundle.blueprint, bundle.adapter_mapping)
    bp_v11 = apply_evolution_patch(bundle.blueprint, l1_patch)
    l2_patch = build_reference_l2_patch_for_autonomous_v11(bp_v11, bundle.adapter_mapping, target_version="2.0.0")
    bp_v2 = apply_evolution_patch(bp_v11, l2_patch)
    mapping_v2 = apply_adapter_mapping_patch(bundle.adapter_mapping, l2_patch)
    assert mapping_v2 is not None

    sc_v2 = evaluate_autonomous_fde_regime(
        regime_name="autonomous_fde_v2.0_l2",
        blueprint=bp_v2,
        adapter_mapping=mapping_v2,
        harness=harness,
        sandbox_client=world,
        raw_baseline_tokens=raw_baseline_tokens,
        raw_total_models=raw_total_models,
        raw_total_fields=raw_total_fields,
        seed_ontology=seed,
    )
    probe_map = {p.probe_id: p for p in sc_v2.probes}
    assert sc_v2.anti_smuggling.passed is True, sc_v2.anti_smuggling.violations
    for pid in ("T1-1", "T1-2", "T1-3", "T2-1", "T2-2", "T2-3", "T2-4", "T3-1", "T3-2", "T4-1", "T4-2"):
        assert probe_map[pid].passed is True, f"{pid} failed: {probe_map[pid].observed_outcome}"
    sc_d = sc_v2.to_dict()["metrics"]
    assert sc_d["t2_dirty_external_writes"] == 0
    assert sc_d["t2_guardrail_f1"] == 1.0
    assert sc_d["t3_homomorphic_fidelity"] == 1.0
    assert sc_d["composite_utility"] > 0.60
    assert out_v1["scorecards"]["autonomous_fde_v1.1_evolved"]["metrics"]["composite_utility"] < sc_d["composite_utility"]
