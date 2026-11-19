"""TK-ONTO-009 · 「用 Ontology 去掉 SaaS」从寄生多云 SaaS 到脱壳独立运行。

同一个纯业务本体，两种主权模式：

* ``standalone=False``（Phase A · 寄生态）
  Customer / ShopOrder / SupportTicket 的基础字段来自 Shopify、Stripe、
  Zendesk 三套异构 SaaS 快照；只有 ``fraud_score`` / ``vip_tier`` /
  ``ops_memo`` / ``ai_triage_note`` 这几个字段由 Ontology 自有，
  存放在 ``ontology_overlay`` 补丁层里，源系统全量重刷也不会丢。

* ``standalone=True``（Phase B · 脱壳态）
  四个对象类型全部 ``__owned__ = True``，拔掉所有外部 SaaS 插头，
  零外部数据源，由 Ontology Action 原生跑完
  建客 → 下单 → 提工单 → AI 分诊 → 退货退款审批 → 时光机撤回 全生命周期。

**契约冻结**：上面两种模式下，Action 名、参数 schema、错误码、审批语义、
只读看板字段完全一致；上层 AI Agent 调用的 Tool 与 AIP Evals 测试集
一行代码都不用改 —— ``run_eval_suite()`` 在两种 Runtime 上跑同一套用例，
输出 ``contract_identical = True``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from open_ontology import (
    Actor,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
)
from .saas_unplug_ontology import (
    AI_AGENT,
    CS_AGENT,
    FRAUD_BLOCK_THRESHOLD,
    MANAGER_APPROVAL_THRESHOLD_CENTS,
    PARASITIC_OWNED_FIELDS,
    RETURN_WINDOW_DAYS,
    STORE_MANAGER,
    SaasOntologyBundle,
    build_saas_ontology,
    build_saas_ontology_spec,
)

__all__ = [
    "AI_AGENT",
    "CS_AGENT",
    "STORE_MANAGER",
    "SaasModel",
    "build_saas_model",
    "build_saas_ontology_spec",
    "build_saas_snapshot",
    "run_eval_suite",
    "agent_tools",
    "AIP_EVALS",
    "main",
]


@dataclass
class SaasModel(SaasOntologyBundle):
    """带 Runtime 启动入口的 SaaS 演示模型包装。"""

    def new_runtime(self, db_path: str = ":memory:") -> OntologyRuntime:
        return OntologyRuntime(self.spec, db_path)


def build_saas_model(standalone: bool = False) -> SaasModel:
    """构建整套 SaaS 业务本体（包装纯 M1 spec + new_runtime 便捷方法）。"""
    bundle = build_saas_ontology(standalone=standalone)
    return SaasModel(
        spec=bundle.spec,
        standalone=bundle.standalone,
        Customer=bundle.Customer,
        ShopOrder=bundle.ShopOrder,
        SupportTicket=bundle.SupportTicket,
        RefundRecord=bundle.RefundRecord,
        customer_orders=bundle.customer_orders,
        order_tickets=bundle.order_tickets,
        ticket_refund=bundle.ticket_refund,
    )


# ---------------------------------------------------------------------------
# Phase A 用：三套 SaaS 的快照（Shopify 订单 / Stripe 支付 / Zendesk 工单）
# ---------------------------------------------------------------------------
def build_saas_snapshot(model: SaasModel) -> tuple[dict[str, Any], dict[str, Any]]:
    """模拟从三套异构 SaaS 拉全量快照。

    返回 ``(objects, links)``，其中 objects 只含源系统字段
    （``source_properties()`` 已剥掉 Ontology 自有字段）。
    """
    customers = [
        model.Customer(
            customer_id="CUST-1001",
            email="wang@example.com",
            name="王小明",
            lifetime_value_cents=48_000,
        ),
        model.Customer(
            customer_id="CUST-1002",
            email="li@example.com",
            name="李静",
            lifetime_value_cents=25_999,
        ),
        model.Customer(
            customer_id="CUST-1003",
            email="zhao@example.com",
            name="赵六",
            lifetime_value_cents=15_000,
        ),
        model.Customer(
            customer_id="CUST-1004",
            email="sun@example.com",
            name="孙悦",
            lifetime_value_cents=32_000,
        ),
    ]
    orders = [
        model.ShopOrder(
            order_id="ORD-2001",
            customer_id="CUST-1001",
            item_sku="SKU-COFFEE-1KG",
            amount_cents=8_999,
            age_days=12,
            status="paid",
        ),
        model.ShopOrder(
            order_id="ORD-2002",
            customer_id="CUST-1002",
            item_sku="SKU-TEA-GIFT",
            amount_cents=25_999,
            age_days=45,
            status="shipped",
        ),
        model.ShopOrder(
            order_id="ORD-2003",
            customer_id="CUST-1003",
            item_sku="SKU-BLENDER",
            amount_cents=15_000,
            age_days=8,
            status="shipped",
        ),
        model.ShopOrder(
            order_id="ORD-2004",
            customer_id="CUST-1004",
            item_sku="SKU-ESPRESSO",
            amount_cents=32_000,
            age_days=3,
            status="paid",
        ),
    ]
    tickets = [
        model.SupportTicket(
            ticket_id="TKT-3001",
            order_id="ORD-2001",
            customer_id="CUST-1001",
            issue_type="damaged",
            requested_refund_cents=8_999,
        ),
        model.SupportTicket(
            ticket_id="TKT-3002",
            order_id="ORD-2002",
            customer_id="CUST-1002",
            issue_type="late",
            requested_refund_cents=25_999,
        ),
        model.SupportTicket(
            ticket_id="TKT-3003",
            order_id="ORD-2003",
            customer_id="CUST-1003",
            issue_type="remorse",
            requested_refund_cents=15_000,
        ),
        model.SupportTicket(
            ticket_id="TKT-3004",
            order_id="ORD-2004",
            customer_id="CUST-1004",
            issue_type="damaged",
            requested_refund_cents=32_000,
        ),
    ]
    objects = {
        "Customer": [c.source_properties() for c in customers],
        "ShopOrder": [o.source_properties() for o in orders],
        "SupportTicket": [t.source_properties() for t in tickets],
    }
    links = {
        "customer_orders": [(o.customer_id, o.order_id) for o in orders],
        "order_tickets": [(t.order_id, t.ticket_id) for t in tickets],
    }
    return objects, links


# ---------------------------------------------------------------------------
# 上层 AI Agent 的 Tool 层（两种模式共用同一份，零改动）
# ---------------------------------------------------------------------------
def agent_tools(runtime: OntologyRuntime, actor: Actor = AI_AGENT) -> dict[str, Any]:
    """把 Action / Function 暴露成 AI Agent 可直接调用的 Tool 字典。"""
    return {
        "create_customer_and_order": lambda run_id, **kw: runtime.submit_action(
            run_id, "create_customer_and_order", kw, actor
        ),
        "open_support_ticket": lambda run_id, **kw: runtime.submit_action(
            run_id, "open_support_ticket", kw, actor
        ),
        "ai_triage_ticket": lambda run_id, **kw: runtime.submit_action(
            run_id, "ai_triage_ticket", kw, actor
        ),
        "process_return_and_refund": lambda run_id, **kw: runtime.submit_action(
            run_id, "process_return_and_refund", kw, actor
        ),
        "workspace_overview": lambda: runtime.call_function(
            "workspace_overview", {}, actor
        ),
    }


# ---------------------------------------------------------------------------
# AIP Evals：同一套测试集，跑在两种 Runtime 上必须给出同一个契约答案
# ---------------------------------------------------------------------------
AIP_EVALS = [
    "eval_create_and_small_refund_auto",
    "eval_expired_window_blocked",
    "eval_high_fraud_blocked",
    "eval_big_refund_requires_manager",
    "eval_double_refund_blocked",
    "eval_revert_refund",
    "eval_overview_snapshot",
]


def run_eval_suite(standalone: bool) -> dict[str, Any]:
    """在指定主权模式下从零跑一遍 AIP Evals，返回可逐字段比对的契约结果。"""
    model = build_saas_model(standalone=standalone)
    rt = model.new_runtime()
    try:
        out: dict[str, Any] = {}

        # --- eval 1: 建客下单 + 小额 AI 秒退 ------------------------------
        rt.submit_action(
            "EVAL-CREATE-1",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E1",
                "email": "e1@example.com",
                "name": "评测客户一",
                "order_id": "ORD-E1",
                "item_sku": "SKU-E1",
                "amount_cents": 4_500,
                "age_days": 5,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-1",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E1",
                "order_id": "ORD-E1",
                "issue_type": "damaged",
                "requested_refund_cents": 4_500,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TRIAGE-1",
            "ai_triage_ticket",
            {"ticket_id": "TKT-E1", "fraud_score": 10, "vip_tier": "STANDARD"},
            AI_AGENT,
        )
        small = rt.submit_action(
            "EVAL-REFUND-1",
            "process_return_and_refund",
            {"ticket_id": "TKT-E1", "refund_id": "RFD-E1", "amount_cents": 4_500},
            AI_AGENT,
        )
        out["small_refund"] = {
            "run_status": small["status"],
            "order_status": rt.get_object(
                model.ShopOrder, "ORD-E1", STORE_MANAGER
            ).status,
            "ticket_status": rt.get_object(
                model.SupportTicket, "TKT-E1", STORE_MANAGER
            ).status,
            "refund_amount": rt.get_object(
                model.RefundRecord, "RFD-E1", STORE_MANAGER
            ).amount_cents,
        }

        # --- eval 2: 超 30 天退货被拦 ------------------------------------
        rt.submit_action(
            "EVAL-CREATE-2",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E2",
                "email": "e2@example.com",
                "name": "评测客户二",
                "order_id": "ORD-E2",
                "item_sku": "SKU-E2",
                "amount_cents": 25_999,
                "age_days": 40,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-2",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E2",
                "order_id": "ORD-E2",
                "issue_type": "late",
                "requested_refund_cents": 25_999,
            },
            AI_AGENT,
        )
        try:
            rt.submit_action(
                "EVAL-REFUND-2",
                "process_return_and_refund",
                {"ticket_id": "TKT-E2", "refund_id": "RFD-E2", "amount_cents": 25_999},
                AI_AGENT,
            )
            out["expired"] = {"error": None, "run_status": None}
        except InvariantError as exc:
            out["expired"] = {
                "error": exc.code,
                "run_status": rt.get_action_run("EVAL-REFUND-2")["status"],
            }

        # --- eval 3: 高欺诈分被拦 ----------------------------------------
        rt.submit_action(
            "EVAL-CREATE-3",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E3",
                "email": "e3@example.com",
                "name": "评测客户三",
                "order_id": "ORD-E3",
                "item_sku": "SKU-E3",
                "amount_cents": 15_000,
                "age_days": 8,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-3",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E3",
                "order_id": "ORD-E3",
                "issue_type": "remorse",
                "requested_refund_cents": 15_000,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TRIAGE-3",
            "ai_triage_ticket",
            {"ticket_id": "TKT-E3", "fraud_score": 85, "vip_tier": "STANDARD"},
            AI_AGENT,
        )
        try:
            rt.submit_action(
                "EVAL-REFUND-3",
                "process_return_and_refund",
                {"ticket_id": "TKT-E3", "refund_id": "RFD-E3", "amount_cents": 15_000},
                AI_AGENT,
            )
            out["fraud"] = {"error": None, "run_status": None}
        except InvariantError as exc:
            out["fraud"] = {
                "error": exc.code,
                "run_status": rt.get_action_run("EVAL-REFUND-3")["status"],
            }

        # --- eval 4: 大额退款挂起 -> 店长审批放行 --------------------------
        rt.submit_action(
            "EVAL-CREATE-4",
            "create_customer_and_order",
            {
                "customer_id": "CUST-E4",
                "email": "e4@example.com",
                "name": "评测客户四",
                "order_id": "ORD-E4",
                "item_sku": "SKU-E4",
                "amount_cents": 32_000,
                "age_days": 2,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TICKET-4",
            "open_support_ticket",
            {
                "ticket_id": "TKT-E4",
                "order_id": "ORD-E4",
                "issue_type": "damaged",
                "requested_refund_cents": 32_000,
            },
            AI_AGENT,
        )
        rt.submit_action(
            "EVAL-TRIAGE-4",
            "ai_triage_ticket",
            {"ticket_id": "TKT-E4", "fraud_score": 20, "vip_tier": "VIP"},
            AI_AGENT,
        )
        pending = rt.submit_action(
            "EVAL-REFUND-4",
            "process_return_and_refund",
            {"ticket_id": "TKT-E4", "refund_id": "RFD-E4", "amount_cents": 32_000},
            AI_AGENT,
        )
        order_while_pending = rt.get_object(model.ShopOrder, "ORD-E4", STORE_MANAGER)
        approved = rt.approve_action("EVAL-REFUND-4", STORE_MANAGER)
        out["big_refund"] = {
            "submit_status": pending["status"],
            "order_status_while_pending": order_while_pending.status,
            "approve_status": approved["status"],
            "approved_by": approved["approved_by"],
            "order_status_after": rt.get_object(
                model.ShopOrder, "ORD-E4", STORE_MANAGER
            ).status,
        }

        # --- eval 5: 重复退款被拦 ----------------------------------------
        try:
            rt.submit_action(
                "EVAL-REFUND-5",
                "process_return_and_refund",
                {"ticket_id": "TKT-E1", "refund_id": "RFD-E5", "amount_cents": 4_500},
                AI_AGENT,
            )
            out["double_refund"] = {"error": None, "run_status": None}
        except InvariantError as exc:
            out["double_refund"] = {
                "error": exc.code,
                "run_status": rt.get_action_run("EVAL-REFUND-5")["status"],
            }

        # --- eval 6: 时光机撤回退款 --------------------------------------
        reverted = rt.revert_action(
            "EVAL-REFUND-4", "EVAL-REFUND-4-REV", STORE_MANAGER
        )
        refund_exists = (
            rt.connection.execute(
                "SELECT 1 FROM ontology_objects "
                "WHERE object_type='RefundRecord' AND object_id='RFD-E4'"
            ).fetchone()
            is not None
        )
        out["revert"] = {
            "status": reverted["status"],
            "order_status": rt.get_object(
                model.ShopOrder, "ORD-E4", STORE_MANAGER
            ).status,
            "refund_exists": refund_exists,
            "customer_ltv": rt.get_object(
                model.Customer, "CUST-E4", STORE_MANAGER
            ).lifetime_value_cents,
        }

        # --- eval 7: 全景看板 --------------------------------------------
        out["overview"] = rt.call_function("workspace_overview", {}, STORE_MANAGER)
        return out
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 演示主程序
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 82)
    print(title)
    print("=" * 82)


def _err(exc: InvariantError) -> str:
    return f"{exc.code}: {exc}"


def phase_a_parasitic() -> dict[str, Any]:
    """Phase A：Ontology 寄生在 Shopify + Stripe + Zendesk 之上。"""
    _section("PHASE A · 寄生态：Ontology 盖在 Shopify + Stripe + Zendesk 三套 SaaS 上")

    model = build_saas_model(standalone=False)
    rt = model.new_runtime()
    try:
        objects, links = build_saas_snapshot(model)
        result = rt.load_snapshot(objects, links)
        print(
            f"  从 3 套 SaaS 导入快照: objects={result['objects']} "
            f"links={result['links']} types={result['object_types']}"
        )
        print(
            "  对象主权: "
            f"Customer.__owned__={model.Customer.owned_spec()} "
            f"ShopOrder.__owned__={model.ShopOrder.owned_spec()}"
        )
        print(f"          SupportTicket.__owned__={model.SupportTicket.owned_spec()}")
        print(f"          RefundRecord.__owned__={model.RefundRecord.owned_spec()}")

        # --- overlay 赋权 ------------------------------------------------
        print("\n  [A1] overlay 赋权：AI 分诊写入 Ontology 自有风控字段")
        rt.submit_action(
            "A-TRIAGE-3001",
            "ai_triage_ticket",
            {
                "ticket_id": "TKT-3001",
                "fraud_score": 12,
                "vip_tier": "VIP",
                "note": "AI: 老客 VIP，外箱破损，建议全额退",
            },
            AI_AGENT,
        )
        rt.submit_action(
            "A-TRIAGE-3003",
            "ai_triage_ticket",
            {
                "ticket_id": "TKT-3003",
                "fraud_score": 92,
                "vip_tier": "STANDARD",
                "note": "AI: 同设备 7 天内第 4 次退款申请，风控高危",
            },
            AI_AGENT,
        )
        rt.submit_action(
            "A-TRIAGE-3004",
            "ai_triage_ticket",
            {
                "ticket_id": "TKT-3004",
                "fraud_score": 18,
                "vip_tier": "VIP",
                "note": "AI: 高客单老客，建议店长复核后全额退",
            },
            AI_AGENT,
        )
        overlay_rows = rt.connection.execute(
            "SELECT object_type, object_id, patch_json FROM ontology_overlay "
            "ORDER BY object_id"
        ).fetchall()
        for row in overlay_rows:
            print(f"    overlay {row['object_type']}:{row['object_id']} -> {row['patch_json']}")
        print(f"    ontology_overlay 行数 = {len(overlay_rows)}")
        status_before = {
            t.ticket_id: t.status
            for t in rt.query_objects(model.SupportTicket, STORE_MANAGER)
        }
        print(f"    分诊后工单状态（Zendesk 源字段）: {status_before}")

        # --- 重刷存活 ----------------------------------------------------
        print("\n  [A2] 三套 SaaS 全量重刷（load_snapshot）后 overlay 是否存活")
        before = {
            c.customer_id: (c.fraud_score, c.vip_tier)
            for c in rt.query_objects(model.Customer, STORE_MANAGER)
        }
        objects2, links2 = build_saas_snapshot(model)
        result2 = rt.load_snapshot(objects2, links2)
        after = {
            c.customer_id: (c.fraud_score, c.vip_tier)
            for c in rt.query_objects(model.Customer, STORE_MANAGER)
        }
        print(
            f"    重刷结果: objects={result2['objects']} "
            f"overlays_reapplied={result2['overlays_reapplied']}"
        )
        print(f"    重刷前 {before}")
        print(f"    重刷后 {after}")
        print(f"    overlay preserved = {before == after}")
        status_after = {
            t.ticket_id: t.status
            for t in rt.query_objects(model.SupportTicket, STORE_MANAGER)
        }
        notes_after = {
            t.ticket_id: t.ai_triage_note
            for t in rt.query_objects(model.SupportTicket, STORE_MANAGER)
            if t.ai_triage_note
        }
        print(f"    重刷后工单状态（Zendesk 源字段，被还原）: {status_after}")
        print(f"    重刷后 AI 分诊备注（Ontology 自有字段，存活）: {sorted(notes_after)}")
        print(
            "    主权边界：源系统字段归 Zendesk，重刷即还原；"
            "风控字段归 Ontology，重刷杀不死。"
        )

        # --- 跨 SaaS 退款护栏 --------------------------------------------
        print("\n  [A3] 跨 SaaS 退货退款护栏（Shopify 订单 + Zendesk 工单 + Stripe 资金）")

        try:
            rt.submit_action(
                "A-REFUND-3002",
                "process_return_and_refund",
                {
                    "ticket_id": "TKT-3002",
                    "refund_id": "RFD-3002",
                    "amount_cents": 25_999,
                },
                AI_AGENT,
            )
            print("    ORD-2002: 未被拦截（异常）")
        except InvariantError as exc:
            run = rt.get_action_run("A-REFUND-3002")
            print(f"    ORD-2002 45 天 -> {_err(exc)}  [run={run['status']}]")

        try:
            rt.submit_action(
                "A-REFUND-3003",
                "process_return_and_refund",
                {
                    "ticket_id": "TKT-3003",
                    "refund_id": "RFD-3003",
                    "amount_cents": 15_000,
                },
                AI_AGENT,
            )
            print("    ORD-2003: 未被拦截（异常）")
        except InvariantError as exc:
            run = rt.get_action_run("A-REFUND-3003")
            print(f"    ORD-2003 fraud=92 -> {_err(exc)}  [run={run['status']}]")

        small = rt.submit_action(
            "A-REFUND-3001",
            "process_return_and_refund",
            {"ticket_id": "TKT-3001", "refund_id": "RFD-3001", "amount_cents": 8_999},
            AI_AGENT,
        )
        print(
            f"    ORD-2001 $89.99 小额 -> {small['status']} 自动秒退 "
            f"(无需审批)"
        )

        pending = rt.submit_action(
            "A-REFUND-3004",
            "process_return_and_refund",
            {"ticket_id": "TKT-3004", "refund_id": "RFD-3004", "amount_cents": 32_000},
            AI_AGENT,
        )
        print(
            f"    ORD-2004 $320.00 大额 -> {pending['status']} "
            f"(AI 提交，等店长审批)"
        )
        order_pending = rt.get_object(model.ShopOrder, "ORD-2004", STORE_MANAGER)
        print(f"      挂起期间 ORD-2004.status = {order_pending.status}（未动账）")
        approved = rt.approve_action("A-REFUND-3004", STORE_MANAGER)
        print(
            f"      店长审批 -> {approved['status']} by {approved['approved_by']}，"
            f"订单状态 = {rt.get_object(model.ShopOrder, 'ORD-2004', STORE_MANAGER).status}"
        )

        # --- 时光机撤回 --------------------------------------------------
        print("\n  [A4] 时光机撤回 A-REFUND-3004（把退款与扣减的 LTV 全部倒回）")
        reverted = rt.revert_action(
            "A-REFUND-3004", "A-REFUND-3004-REV", STORE_MANAGER
        )
        print(f"    撤回 run={reverted['run_id']} status={reverted['status']}")
        print(
            f"    ORD-2004.status = "
            f"{rt.get_object(model.ShopOrder, 'ORD-2004', STORE_MANAGER).status}，"
            f"CUST-1004.ltv = "
            f"{rt.get_object(model.Customer, 'CUST-1004', STORE_MANAGER).lifetime_value_cents}"
        )

        overview = rt.call_function("workspace_overview", {}, STORE_MANAGER)
        print("\n  [A5] 全景看板 workspace_overview")
        for key, value in overview.items():
            print(f"    {key} = {value}")
        return {"overview": overview, "overlay_rows": len(overlay_rows)}
    finally:
        rt.close()


def phase_b_standalone() -> dict[str, Any]:
    """Phase B：拔掉全部 SaaS 插头，纯 Ontology 独立运行。"""
    _section("PHASE B · 脱壳态：拔掉全部 SaaS 插头，standalone=True 纯 Ontology 原生运行")

    model = build_saas_model(standalone=True)
    rt = model.new_runtime()
    try:
        print("  零外部数据源：不导入任何 SaaS 快照，直接由 Ontology Action 造数据")
        print(
            "  对象主权: "
            f"Customer.__owned__={model.Customer.owned_spec()} "
            f"ShopOrder.__owned__={model.ShopOrder.owned_spec()} "
            f"SupportTicket.__owned__={model.SupportTicket.owned_spec()} "
            f"RefundRecord.__owned__={model.RefundRecord.owned_spec()}"
        )
        print(f"  启动时对象数 = {rt.count_objects()}（空库）")

        tools = agent_tools(rt, AI_AGENT)

        # --- 原生建客下单 -------------------------------------------------
        print("\n  [B1] 原生建客下单（替代 Shopify）")
        tools["create_customer_and_order"](
            "B-CREATE-1",
            customer_id="CUST-B01",
            email="b01@example.com",
            name="脱壳客户一",
            order_id="ORD-B01",
            item_sku="SKU-BLENDER",
            amount_cents=15_000,
            age_days=8,
        )
        tools["create_customer_and_order"](
            "B-CREATE-2",
            customer_id="CUST-B02",
            email="b02@example.com",
            name="脱壳客户二",
            order_id="ORD-B02",
            item_sku="SKU-TEA-GIFT",
            amount_cents=25_999,
            age_days=45,
        )
        tools["create_customer_and_order"](
            "B-CREATE-3",
            customer_id="CUST-B03",
            email="b03@example.com",
            name="脱壳客户三",
            order_id="ORD-B03",
            item_sku="SKU-ESPRESSO",
            amount_cents=32_000,
            age_days=2,
        )
        tools["create_customer_and_order"](
            "B-CREATE-4",
            customer_id="CUST-B04",
            email="b04@example.com",
            name="脱壳客户四",
            order_id="ORD-B04",
            item_sku="SKU-COFFEE-1KG",
            amount_cents=8_999,
            age_days=12,
        )
        print(f"    对象数 = {rt.count_objects()}（Customer 4 + ShopOrder 4）")
        print(
            f"    ontology_overlay 行数 = "
            f"{rt.connection.execute('SELECT COUNT(*) FROM ontology_overlay').fetchone()[0]}"
            "（全自有对象，无需补丁层）"
        )

        # --- 原生建工单 ---------------------------------------------------
        print("\n  [B2] 原生建客诉工单（替代 Zendesk）")
        for run_id, ticket_id, order_id, issue, amount in (
            ("B-TICKET-1", "TKT-B01", "ORD-B01", "remorse", 15_000),
            ("B-TICKET-2", "TKT-B02", "ORD-B02", "late", 25_999),
            ("B-TICKET-3", "TKT-B03", "ORD-B03", "damaged", 32_000),
            ("B-TICKET-4", "TKT-B04", "ORD-B04", "damaged", 8_999),
        ):
            tools["open_support_ticket"](
                run_id,
                ticket_id=ticket_id,
                order_id=order_id,
                issue_type=issue,
                requested_refund_cents=amount,
            )
        print(f"    对象数 = {rt.count_objects()}（+ SupportTicket 4）")

        # --- AI 分诊 ------------------------------------------------------
        print("\n  [B3] AI 风控分诊（写本体字段，无外部 SaaS 可写）")
        for run_id, ticket_id, fraud, vip in (
            ("B-TRIAGE-1", "TKT-B01", 92, "STANDARD"),
            ("B-TRIAGE-2", "TKT-B02", 15, "STANDARD"),
            ("B-TRIAGE-3", "TKT-B03", 18, "VIP"),
            ("B-TRIAGE-4", "TKT-B04", 8, "VIP"),
        ):
            tools["ai_triage_ticket"](
                run_id, ticket_id=ticket_id, fraud_score=fraud, vip_tier=vip
            )
        for ticket_id in ("TKT-B01", "TKT-B02", "TKT-B03", "TKT-B04"):
            ticket = rt.get_object(model.SupportTicket, ticket_id, STORE_MANAGER)
            print(
                f"    {ticket_id}: status={ticket.status} note={ticket.ai_triage_note}"
            )

        # --- 三条硬护栏 ---------------------------------------------------
        print("\n  [B4] 退货退款硬护栏（超期 / 高欺诈 / 重复超额）")
        try:
            tools["process_return_and_refund"](
                "B-REFUND-2",
                ticket_id="TKT-B02",
                refund_id="RFD-B02",
                amount_cents=25_999,
            )
        except InvariantError as exc:
            run = rt.get_action_run("B-REFUND-2")
            print(f"    ORD-B02 45 天 -> {_err(exc)}  [run={run['status']}]")

        try:
            tools["process_return_and_refund"](
                "B-REFUND-1",
                ticket_id="TKT-B01",
                refund_id="RFD-B01",
                amount_cents=15_000,
            )
        except InvariantError as exc:
            run = rt.get_action_run("B-REFUND-1")
            print(f"    ORD-B01 fraud=92 -> {_err(exc)}  [run={run['status']}]")

        # --- 小额秒退 / 大额审批 ------------------------------------------
        print("\n  [B5] 分级审批：小额 AI 秒退，大额店长签批")
        small = tools["process_return_and_refund"](
            "B-REFUND-4",
            ticket_id="TKT-B04",
            refund_id="RFD-B04",
            amount_cents=8_999,
        )
        print(f"    ORD-B04 $89.99 -> {small['status']} 自动秒退")

        pending = tools["process_return_and_refund"](
            "B-REFUND-3",
            ticket_id="TKT-B03",
            refund_id="RFD-B03",
            amount_cents=32_000,
        )
        print(f"    ORD-B03 $320.00 -> {pending['status']} 等待店长审批")
        order_pending = rt.get_object(model.ShopOrder, "ORD-B03", STORE_MANAGER)
        print(f"      挂起期间 ORD-B03.status = {order_pending.status}（未动账）")
        approved = rt.approve_action("B-REFUND-3", STORE_MANAGER)
        print(
            f"      店长审批 -> {approved['status']} by {approved['approved_by']}"
        )

        # --- 重复退款拦截 -------------------------------------------------
        print("\n  [B6] 防重复退款")
        try:
            tools["process_return_and_refund"](
                "B-REFUND-DUP",
                ticket_id="TKT-B04",
                refund_id="RFD-B04-DUP",
                amount_cents=8_999,
            )
        except InvariantError as exc:
            run = rt.get_action_run("B-REFUND-DUP")
            print(f"    TKT-B04 二次退款 -> {_err(exc)}  [run={run['status']}]")

        # --- 时光机撤回 ---------------------------------------------------
        print("\n  [B7] 时光机一键撤回 B-REFUND-3")
        reverted = rt.revert_action("B-REFUND-3", "B-REFUND-3-REV", STORE_MANAGER)
        print(f"    撤回 run={reverted['run_id']} status={reverted['status']}")
        print(
            f"    ORD-B03.status = "
            f"{rt.get_object(model.ShopOrder, 'ORD-B03', STORE_MANAGER).status}，"
            f"CUST-B03.ltv = "
            f"{rt.get_object(model.Customer, 'CUST-B03', STORE_MANAGER).lifetime_value_cents}"
        )

        overview = tools["workspace_overview"]()
        print("\n  [B8] 全景看板 workspace_overview")
        for key, value in overview.items():
            print(f"    {key} = {value}")
        return {"overview": overview}
    finally:
        rt.close()


def main() -> int:
    print("=" * 82)
    print("TK-ONTO-009 · 用 Ontology 去掉 SaaS：从寄生多云 SaaS 到脱壳独立运行")
    print("=" * 82)

    phase_a_parasitic()
    phase_b_standalone()

    _section("契约冻结证明 · 同一套 AIP Evals 跑在两种 Runtime 上")
    print(f"  Evals 用例 ({len(AIP_EVALS)} 条): {', '.join(AIP_EVALS)}")
    evals_parasitic = run_eval_suite(standalone=False)
    evals_standalone = run_eval_suite(standalone=True)

    for name in AIP_EVALS:
        key = name.replace("eval_", "")
        key = {
            "create_and_small_refund_auto": "small_refund",
            "expired_window_blocked": "expired",
            "high_fraud_blocked": "fraud",
            "big_refund_requires_manager": "big_refund",
            "double_refund_blocked": "double_refund",
            "revert_refund": "revert",
            "overview_snapshot": "overview",
        }[key]
        same = evals_parasitic[key] == evals_standalone[key]
        print(f"    {'✓' if same else '✗'} {name}: {'一致' if same else '不一致'}")

    print("\n  --- 寄生态 (SaaS) 结果 ---")
    for key in (
        "small_refund",
        "expired",
        "fraud",
        "big_refund",
        "double_refund",
        "revert",
    ):
        print(f"    {key} = {evals_parasitic[key]}")
    print("\n  --- 脱壳态 (standalone) 结果 ---")
    for key in (
        "small_refund",
        "expired",
        "fraud",
        "big_refund",
        "double_refund",
        "revert",
    ):
        print(f"    {key} = {evals_standalone[key]}")

    contract_identical = evals_parasitic == evals_standalone
    print()
    print("=" * 82)
    print(f"  contract_identical = {contract_identical}")
    print("  同一套 Action / Tool / Evals，拔掉 SaaS 后上层业务代码零改动。")
    print("=" * 82)
    return 0 if contract_identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
