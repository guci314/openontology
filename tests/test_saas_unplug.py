"""TK-ONTO-009 acceptance tests: 「用 Ontology 去掉 SaaS」双模式演进。

覆盖：
* 无 SaaS 原生建客下单、建工单、AI 分诊
* 超 30 天退货拦截、高欺诈分拦截、重复/超额退款拦截
* 小额 AI 自动秒退、大额店长审批、时光机一键撤回
* 寄生态 overlay 重刷存活
* 寄生态 vs 无 SaaS 态契约一致性（Action/Tool/Evals 零改动）
"""

from __future__ import annotations

import pytest

from examples import saas_unplug_demo as demo
from open_ontology import InvariantError

MANAGER = demo.STORE_MANAGER
AI = demo.AI_AGENT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def standalone():
    """脱壳态：全对象 __owned__=True，零外部 SaaS。"""
    model = demo.build_saas_model(standalone=True)
    rt = model.new_runtime()
    yield model, rt
    rt.close()


@pytest.fixture()
def parasitic():
    """寄生态：三套 SaaS 快照 + 字段级 overlay。"""
    model = demo.build_saas_model(standalone=False)
    rt = model.new_runtime()
    objects, links = demo.build_saas_snapshot(model)
    rt.load_snapshot(objects, links)
    yield model, rt
    rt.close()


def _seed_customer_order(rt, model, *, amount, age_days=5, cid="CUST-T1",
                        oid="ORD-T1"):
    rt.submit_action(
        f"SEED-CREATE-{oid}",
        "create_customer_and_order",
        {
            "customer_id": cid,
            "email": f"{cid.lower()}@example.com",
            "name": f"测试客户 {cid}",
            "order_id": oid,
            "item_sku": "SKU-T",
            "amount_cents": amount,
            "age_days": age_days,
        },
        AI,
    )
    return cid, oid


def _seed_ticket(rt, *, oid, cid, amount, tid="TKT-T1", issue="damaged"):
    rt.submit_action(
        f"SEED-TICKET-{tid}",
        "open_support_ticket",
        {
            "ticket_id": tid,
            "order_id": oid,
            "issue_type": issue,
            "requested_refund_cents": amount,
        },
        AI,
    )
    return tid


# ---------------------------------------------------------------------------
# 1. 模式声明：寄生 vs 脱壳的主权差异
# ---------------------------------------------------------------------------
def test_standalone_marks_every_object_fully_owned():
    model = demo.build_saas_model(standalone=True)
    for name in ("Customer", "ShopOrder", "SupportTicket", "RefundRecord"):
        cls = model.object_type(name)
        assert cls.owned_spec() is True, f"{name} must be fully owned in standalone"
        assert cls.is_fully_owned() is True


def test_parasitic_uses_field_level_overlay_sovereignty():
    model = demo.build_saas_model(standalone=False)
    customer = model.Customer
    assert customer.is_fully_owned() is False
    assert customer.owned_field_defaults() == {
        "fraud_score": 0,
        "vip_tier": "STANDARD",
    }
    # 规格书要求：RefundRecord 在两种模式下都 100% 自有
    assert model.RefundRecord.owned_spec() is True


def test_spec_factory_matches_model_factory():
    spec = demo.build_saas_ontology_spec(standalone=True)
    assert set(spec.object_types) == {
        "Customer",
        "ShopOrder",
        "SupportTicket",
        "RefundRecord",
    }
    assert set(spec.link_types) == {
        "customer_orders",
        "order_tickets",
        "ticket_refund",
    }
    assert set(spec.actions) == {
        "create_customer_and_order",
        "open_support_ticket",
        "ai_triage_ticket",
        "process_return_and_refund",
    }
    assert set(spec.functions) == {"workspace_overview"}


# ---------------------------------------------------------------------------
# 2. 无 SaaS 原生建客下单 + 建工单
# ---------------------------------------------------------------------------
def test_standalone_creates_customer_order_and_links(standalone):
    model, rt = standalone
    assert rt.count_objects() == 0  # 零外部数据源

    rt.submit_action(
        "T-CREATE-1",
        "create_customer_and_order",
        {
            "customer_id": "CUST-1",
            "email": "a@example.com",
            "name": "张三",
            "order_id": "ORD-1",
            "item_sku": "SKU-1",
            "amount_cents": 12_000,
            "age_days": 3,
        },
        AI,
    )
    customer = rt.get_object(model.Customer, "CUST-1", MANAGER)
    order = rt.get_object(model.ShopOrder, "ORD-1", MANAGER)
    assert customer.lifetime_value_cents == 12_000
    assert order.status == "paid"
    assert order.customer_id == "CUST-1"

    linked = rt.get_linked(
        customer, model.customer_orders, model.ShopOrder, MANAGER
    )
    assert [o.order_id for o in linked] == ["ORD-1"]


def test_standalone_opens_support_ticket_with_link(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=12_000)
    rt.submit_action(
        "T-TICKET-1",
        "open_support_ticket",
        {
            "ticket_id": "TKT-1",
            "order_id": "ORD-T1",
            "issue_type": "damaged",
            "requested_refund_cents": 12_000,
        },
        AI,
    )
    ticket = rt.get_object(model.SupportTicket, "TKT-1", MANAGER)
    assert ticket.status == "open"
    assert ticket.customer_id == "CUST-T1"

    order = rt.get_object(model.ShopOrder, "ORD-T1", MANAGER)
    linked = rt.get_linked(order, model.order_tickets, model.SupportTicket, MANAGER)
    assert [t.ticket_id for t in linked] == ["TKT-1"]


def test_open_ticket_on_missing_order_is_rejected(standalone):
    from open_ontology import ObjectNotFoundError

    model, rt = standalone
    with pytest.raises(ObjectNotFoundError):
        rt.submit_action(
            "T-TICKET-MISSING",
            "open_support_ticket",
            {
                "ticket_id": "TKT-X",
                "order_id": "ORD-NOT-EXIST",
                "issue_type": "late",
                "requested_refund_cents": 1000,
            },
            AI,
        )
    # 引用不存在的对象属于「调用错误」，不产生业务拒绝审计
    assert rt.get_action_run("T-TICKET-MISSING") is None
    assert rt.count_objects("SupportTicket") == 0


# ---------------------------------------------------------------------------
# 3. AI 智能分诊
# ---------------------------------------------------------------------------
def test_ai_triage_writes_risk_fields_and_note(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=12_000)
    _seed_ticket(rt, oid="ORD-T1", cid="CUST-T1", amount=12_000)

    rt.submit_action(
        "T-TRIAGE-1",
        "ai_triage_ticket",
        {"ticket_id": "TKT-T1", "fraud_score": 33, "vip_tier": "VIP"},
        AI,
    )
    customer = rt.get_object(model.Customer, "CUST-T1", MANAGER)
    ticket = rt.get_object(model.SupportTicket, "TKT-T1", MANAGER)
    assert customer.fraud_score == 33
    assert customer.vip_tier == "VIP"
    assert ticket.status == "triaged"
    assert "fraud_score=33" in (ticket.ai_triage_note or "")


def test_ai_triage_rejects_out_of_range_fraud_score(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=12_000)
    _seed_ticket(rt, oid="ORD-T1", cid="CUST-T1", amount=12_000)
    with pytest.raises(InvariantError) as exc:
        rt.submit_action(
            "T-TRIAGE-BAD",
            "ai_triage_ticket",
            {"ticket_id": "TKT-T1", "fraud_score": 140},
            AI,
        )
    assert exc.value.code == "INVALID_FRAUD_SCORE"


# ---------------------------------------------------------------------------
# 4. 三条硬护栏
# ---------------------------------------------------------------------------
def test_return_window_expired_is_blocked_and_audited(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=25_999, age_days=45, oid="ORD-OLD")
    _seed_ticket(
        rt, oid="ORD-OLD", cid="CUST-T1", amount=25_999, tid="TKT-OLD"
    )
    with pytest.raises(InvariantError) as exc:
        rt.submit_action(
            "T-REFUND-OLD",
            "process_return_and_refund",
            {"ticket_id": "TKT-OLD", "refund_id": "RFD-OLD", "amount_cents": 25_999},
            AI,
        )
    assert exc.value.code == "RETURN_WINDOW_EXPIRED"
    run = rt.get_action_run("T-REFUND-OLD")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "RETURN_WINDOW_EXPIRED"
    # 订单未被改动
    assert rt.get_object(model.ShopOrder, "ORD-OLD", MANAGER).status == "paid"


def test_high_fraud_score_is_blocked_and_audited(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=15_000, oid="ORD-FRD")
    _seed_ticket(rt, oid="ORD-FRD", cid="CUST-T1", amount=15_000, tid="TKT-FRD")
    rt.submit_action(
        "T-TRIAGE-FRD",
        "ai_triage_ticket",
        {"ticket_id": "TKT-FRD", "fraud_score": 80},
        AI,
    )
    with pytest.raises(InvariantError) as exc:
        rt.submit_action(
            "T-REFUND-FRD",
            "process_return_and_refund",
            {"ticket_id": "TKT-FRD", "refund_id": "RFD-FRD", "amount_cents": 15_000},
            AI,
        )
    assert exc.value.code == "HIGH_FRAUD_RISK_BLOCKED"
    assert rt.get_action_run("T-REFUND-FRD")["status"] == "REJECTED"


def test_double_refund_is_blocked(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=8_999, oid="ORD-DUP")
    _seed_ticket(rt, oid="ORD-DUP", cid="CUST-T1", amount=8_999, tid="TKT-DUP")
    rt.submit_action(
        "T-REFUND-DUP-1",
        "process_return_and_refund",
        {"ticket_id": "TKT-DUP", "refund_id": "RFD-DUP", "amount_cents": 8_999},
        AI,
    )
    with pytest.raises(InvariantError) as exc:
        rt.submit_action(
            "T-REFUND-DUP-2",
            "process_return_and_refund",
            {"ticket_id": "TKT-DUP", "refund_id": "RFD-DUP2", "amount_cents": 8_999},
            AI,
        )
    assert exc.value.code == "INVALID_REFUND_STATE"


def test_refund_amount_over_order_amount_is_blocked(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=5_000, oid="ORD-OVER")
    # 工单申请额本身超过订单额：护栏应拦在 INVALID_REFUND_STATE
    _seed_ticket(rt, oid="ORD-OVER", cid="CUST-T1", amount=9_999, tid="TKT-OVER")
    with pytest.raises(InvariantError) as exc:
        rt.submit_action(
            "T-REFUND-OVER",
            "process_return_and_refund",
            {"ticket_id": "TKT-OVER", "refund_id": "RFD-OVER", "amount_cents": 9_999},
            AI,
        )
    assert exc.value.code == "INVALID_REFUND_STATE"


# ---------------------------------------------------------------------------
# 5. 分级审批：小额秒退 / 大额挂起
# ---------------------------------------------------------------------------
def test_small_refund_is_auto_committed(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=8_999, oid="ORD-S")
    _seed_ticket(rt, oid="ORD-S", cid="CUST-T1", amount=8_999, tid="TKT-S")
    run = rt.submit_action(
        "T-REFUND-S",
        "process_return_and_refund",
        {"ticket_id": "TKT-S", "refund_id": "RFD-S", "amount_cents": 8_999},
        AI,
    )
    assert run["status"] == "COMMITTED"
    assert rt.get_object(model.ShopOrder, "ORD-S", MANAGER).status == "refunded"
    assert rt.get_object(model.SupportTicket, "TKT-S", MANAGER).status == "resolved"
    refund = rt.get_object(model.RefundRecord, "RFD-S", MANAGER)
    assert refund.amount_cents == 8_999
    assert refund.approved_by == AI.actor_id
    # LTV 被扣减
    assert (
        rt.get_object(model.Customer, "CUST-T1", MANAGER).lifetime_value_cents == 0
    )


def test_big_refund_requires_manager_approval_then_commits(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=32_000, oid="ORD-BIG")
    _seed_ticket(rt, oid="ORD-BIG", cid="CUST-T1", amount=32_000, tid="TKT-BIG")

    pending = rt.submit_action(
        "T-REFUND-BIG",
        "process_return_and_refund",
        {"ticket_id": "TKT-BIG", "refund_id": "RFD-BIG", "amount_cents": 32_000},
        AI,
    )
    assert pending["status"] == "PENDING_APPROVAL"
    # 挂起期间绝不提前动账
    assert rt.get_object(model.ShopOrder, "ORD-BIG", MANAGER).status == "paid"
    assert (
        rt.get_object(model.Customer, "CUST-T1", MANAGER).lifetime_value_cents
        == 32_000
    )

    approved = rt.approve_action("T-REFUND-BIG", MANAGER)
    assert approved["status"] == "COMMITTED"
    assert approved["approved_by"] == MANAGER.actor_id
    assert rt.get_object(model.ShopOrder, "ORD-BIG", MANAGER).status == "refunded"


def test_manager_submitted_big_refund_skips_approval(standalone):
    """店长本人提交的大额退款不需要二次审批（approval_rule 只针对 ai_agent）。"""
    model, rt = standalone
    _seed_customer_order(rt, model, amount=32_000, oid="ORD-MGR")
    _seed_ticket(rt, oid="ORD-MGR", cid="CUST-T1", amount=32_000, tid="TKT-MGR")
    run = rt.submit_action(
        "T-REFUND-MGR",
        "process_return_and_refund",
        {"ticket_id": "TKT-MGR", "refund_id": "RFD-MGR", "amount_cents": 32_000},
        MANAGER,
    )
    assert run["status"] == "COMMITTED"


# ---------------------------------------------------------------------------
# 6. 时光机一键撤回
# ---------------------------------------------------------------------------
def test_revert_refund_restores_all_state(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=32_000, oid="ORD-REV")
    _seed_ticket(rt, oid="ORD-REV", cid="CUST-T1", amount=32_000, tid="TKT-REV")
    rt.submit_action(
        "T-REFUND-REV",
        "process_return_and_refund",
        {"ticket_id": "TKT-REV", "refund_id": "RFD-REV", "amount_cents": 32_000},
        MANAGER,
    )
    assert rt.get_object(model.ShopOrder, "ORD-REV", MANAGER).status == "refunded"

    reverted = rt.revert_action("T-REFUND-REV", "T-REFUND-REV-UNDO", MANAGER)
    assert reverted["status"] == "COMMITTED"

    order = rt.get_object(model.ShopOrder, "ORD-REV", MANAGER)
    customer = rt.get_object(model.Customer, "CUST-T1", MANAGER)
    ticket = rt.get_object(model.SupportTicket, "TKT-REV", MANAGER)
    assert order.status == "paid"
    assert customer.lifetime_value_cents == 32_000
    assert ticket.status == "open"
    # RefundRecord 已被逆 diff 删除
    assert (
        rt.connection.execute(
            "SELECT 1 FROM ontology_objects "
            "WHERE object_type='RefundRecord' AND object_id='RFD-REV'"
        ).fetchone()
        is None
    )
    assert rt.get_action_run("T-REFUND-REV")["status"] == "REVERTED"


# ---------------------------------------------------------------------------
# 7. 全景看板
# ---------------------------------------------------------------------------
def test_workspace_overview_reports_blocked_and_amounts(standalone):
    model, rt = standalone
    _seed_customer_order(rt, model, amount=8_999, oid="ORD-V1")
    _seed_ticket(rt, oid="ORD-V1", cid="CUST-T1", amount=8_999, tid="TKT-V1")
    rt.submit_action(
        "T-REFUND-V1",
        "process_return_and_refund",
        {"ticket_id": "TKT-V1", "refund_id": "RFD-V1", "amount_cents": 8_999},
        AI,
    )
    _seed_customer_order(
        rt, model, amount=20_000, age_days=99, cid="CUST-V2", oid="ORD-V2"
    )
    _seed_ticket(rt, oid="ORD-V2", cid="CUST-V2", amount=20_000, tid="TKT-V2")
    with pytest.raises(InvariantError):
        rt.submit_action(
            "T-REFUND-V2",
            "process_return_and_refund",
            {"ticket_id": "TKT-V2", "refund_id": "RFD-V2", "amount_cents": 20_000},
            AI,
        )

    view = rt.call_function("workspace_overview", {}, MANAGER)
    assert view["customers"] == 2
    assert view["orders"] == 2
    assert view["gmv_cents"] == 28_999
    assert view["refund_count"] == 1
    assert view["refunded_cents"] == 8_999
    assert view["blocked_runs"] == {"RETURN_WINDOW_EXPIRED": 1}
    assert view["order_status_amounts"] == {"paid": 20_000, "refunded": 8_999}
    assert view["pivot_customers"] == ["CUST-T1", "CUST-V2"]


# ---------------------------------------------------------------------------
# 8. 寄生态：overlay 重刷存活
# ---------------------------------------------------------------------------
def test_parasitic_overlay_survives_full_saas_resync(parasitic):
    model, rt = parasitic
    rt.submit_action(
        "P-TRIAGE-1",
        "ai_triage_ticket",
        {"ticket_id": "TKT-3001", "fraud_score": 12, "vip_tier": "VIP"},
        AI,
    )
    before = {
        c.customer_id: (c.fraud_score, c.vip_tier)
        for c in rt.query_objects(model.Customer, MANAGER)
    }
    assert before["CUST-1001"] == (12, "VIP")

    objects, links = demo.build_saas_snapshot(model)
    result = rt.load_snapshot(objects, links)
    assert result["overlays_reapplied"] >= 1

    after = {
        c.customer_id: (c.fraud_score, c.vip_tier)
        for c in rt.query_objects(model.Customer, MANAGER)
    }
    assert after == before


def test_parasitic_snapshot_cannot_supply_owned_fields(parasitic):
    """源系统快照不允许自带 Ontology 自有字段（主权边界）。"""
    from open_ontology import GovernanceViolationError

    model, rt = parasitic
    objects, links = demo.build_saas_snapshot(model)
    objects["Customer"][0]["fraud_score"] = 99
    with pytest.raises(GovernanceViolationError):
        rt.load_snapshot(objects, links)


# ---------------------------------------------------------------------------
# 9. 契约一致性：同一套 Evals 在两种模式给出同一个答案
# ---------------------------------------------------------------------------
def test_eval_suite_is_identical_across_modes():
    parasitic_result = demo.run_eval_suite(standalone=False)
    standalone_result = demo.run_eval_suite(standalone=True)
    assert parasitic_result == standalone_result, (
        "同一套 Action / Tool / Evals 在寄生态与脱壳态必须给出完全一致的契约结果"
    )
    assert parasitic_result["small_refund"]["run_status"] == "COMMITTED"
    assert parasitic_result["expired"]["error"] == "RETURN_WINDOW_EXPIRED"
    assert parasitic_result["fraud"]["error"] == "HIGH_FRAUD_RISK_BLOCKED"
    assert (
        parasitic_result["big_refund"]["submit_status"] == "PENDING_APPROVAL"
    )
    assert parasitic_result["double_refund"]["error"] == "INVALID_REFUND_STATE"
    assert parasitic_result["revert"]["refund_exists"] is False


def test_action_and_tool_surface_is_unchanged_across_modes():
    """拔掉 SaaS 后，Agent 看到的 Tool 名与 Action schema 必须一模一样。"""
    para = demo.build_saas_model(standalone=False)
    stand = demo.build_saas_model(standalone=True)

    assert para.spec.summary() == stand.spec.summary()

    para_rt = para.new_runtime()
    stand_rt = stand.new_runtime()
    try:
        para_tools = set(demo.agent_tools(para_rt).keys())
        stand_tools = set(demo.agent_tools(stand_rt).keys())
        assert para_tools == stand_tools
        assert para_tools == {
            "create_customer_and_order",
            "open_support_ticket",
            "ai_triage_ticket",
            "process_return_and_refund",
            "workspace_overview",
        }
    finally:
        para_rt.close()
        stand_rt.close()

    # 参数 schema 逐字一致
    for action_name in (
        "create_customer_and_order",
        "open_support_ticket",
        "ai_triage_ticket",
        "process_return_and_refund",
    ):
        assert (
            para.spec.get_action(action_name).params.model_json_schema()
            == stand.spec.get_action(action_name).params.model_json_schema()
        )
    for link_name in ("customer_orders", "order_tickets", "ticket_refund"):
        para_link = para.spec.get_link_type(link_name)
        stand_link = stand.spec.get_link_type(link_name)
        assert (para_link.source, para_link.target, para_link.cardinality) == (
            stand_link.source,
            stand_link.target,
            stand_link.cardinality,
        )


def test_standalone_writes_no_overlay_rows(standalone):
    """脱壳态全对象自有，overlay 补丁层应当始终为空。"""
    model, rt = standalone
    _seed_customer_order(rt, model, amount=12_000)
    _seed_ticket(rt, oid="ORD-T1", cid="CUST-T1", amount=12_000)
    rt.submit_action(
        "T-TRIAGE-NOOVERLAY",
        "ai_triage_ticket",
        {"ticket_id": "TKT-T1", "fraud_score": 20},
        AI,
    )
    assert (
        rt.connection.execute("SELECT COUNT(*) FROM ontology_overlay").fetchone()[0]
        == 0
    )


def test_main_returns_zero_and_reports_identical(capsys):
    assert demo.main() == 0
    output = capsys.readouterr().out
    assert "contract_identical = True" in output
    assert "RETURN_WINDOW_EXPIRED" in output
    assert "HIGH_FRAUD_RISK_BLOCKED" in output
