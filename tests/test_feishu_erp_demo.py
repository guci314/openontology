"""飞书交互式审批卡片 + 真实 Odoo 19 双向回写 + 一键时光机冲正演播室验收测试。"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any

import pytest

from examples import o2c_ontology as o2c
from examples.o2c_demo_seed import build_o2c_demo
from examples.feishu_card_bridge import (
    FeishuApprovalGateway,
    FeishuOpenAPIClient,
    build_feishu_card,
    format_run_for_executive,
    verify_feishu_signature,
)
from examples.feishu_erp_demo_server import (
    SHOWROOM_HTML,
    start_feishu_erp_demo_server,
)
from examples.odoo_o2c_adapter import OdooO2CAdapter
from open_ontology import (
    GovernanceViolationError,
    InvariantError,
    OntologyRuntime,
    PermissionDeniedError,
)


# ---------------------------------------------------------------------------
# 1. M1 WorkCard 审批拒止/流转 与 全生命周期 run_event_hooks
# ---------------------------------------------------------------------------
def test_reject_action_and_run_event_hooks():
    demo = build_o2c_demo()
    rt = demo.runtime
    events: list[tuple[str, str]] = []
    rt.add_run_event_hook(lambda actor, run: events.append((run["run_id"], run["status"])))

    try:
        # Invariant rejection emits REJECTED event
        with pytest.raises(InvariantError):
            rt.submit_action(
                "RUN-INV-BAD",
                "issue_and_post_invoice",
                {"order": "O2C-SO-001"},
                o2c.AI_AGENT,
            )
        assert ("RUN-INV-BAD", "REJECTED") in events

        # Over-credit confirm is blocked by precondition and opens M1 WorkCard (zero PENDING_APPROVAL)
        pending = o2c.submit_or_open_work_card(
            rt,
            "RUN-OVER-001",
            "confirm_sales_order",
            {"order": "O2C-SO-002"},
            o2c.AI_AGENT,
            target_object_type="SalesOrder",
            target_object_id="O2C-SO-002",
            assignee_role="finance_director",
            card_id="RUN-OVER-001",
        )
        assert pending["status"] == "AWAITING_WORK_CARD"
        assert pending["work_card_status"] == "OPEN"
        assert all(r["status"] in {"COMMITTED", "REJECTED", "REVERTED"} for r in rt.list_action_runs())
        assert ("RUN-OVER-001", "REJECTED") in events
        assert ("RUN-OVER-001-OPEN", "COMMITTED") in events

        # Non-approver cannot reject WorkCard
        with pytest.raises(PermissionDeniedError):
            rt.submit_action(
                "RUN-OVER-001-REJ-BAD",
                "reject_work_card",
                {"card_id": "RUN-OVER-001", "resolution_note": "nope"},
                o2c.SALES_REP,
            )

        # Approver rejects WorkCard -> REJECTED + objects untouched + hook fires
        rej_run = rt.submit_action(
            "RUN-OVER-001-REJ",
            "reject_work_card",
            {"card_id": "RUN-OVER-001", "resolution_note": "风控超额驳回"},
            o2c.FINANCE_DIRECTOR,
        )
        assert rej_run["status"] == "COMMITTED"
        wc_after_rej = rt.get_object(o2c.WorkCard, "RUN-OVER-001", o2c.FINANCE_DIRECTOR)
        assert wc_after_rej.status == "REJECTED"
        assert wc_after_rej.resolved_by_actor_id == o2c.FINANCE_DIRECTOR.actor_id
        assert wc_after_rej.resolution_note == "风控超额驳回"
        assert rt.get_object(o2c.SalesOrder, "O2C-SO-002", o2c.FINANCE_DIRECTOR).status == "draft"
        assert ("RUN-OVER-001-REJ", "COMMITTED") in events

        # Cannot settle/approve an already-rejected WorkCard
        with pytest.raises(InvariantError):
            o2c.settle_work_card(rt, "RUN-OVER-001", o2c.FINANCE_DIRECTOR)

        # Open WorkCard -> settle_work_card -> Revert lifecycle hooks
        o2c.submit_or_open_work_card(
            rt,
            "RUN-OVER-002",
            "confirm_sales_order",
            {"order": "O2C-SO-002"},
            o2c.AI_AGENT,
            target_object_type="SalesOrder",
            target_object_id="O2C-SO-002",
            assignee_role="finance_director",
            card_id="RUN-OVER-002",
        )
        o2c.settle_work_card(
            rt,
            "RUN-OVER-002",
            o2c.FINANCE_DIRECTOR,
            action_run_id="RUN-OVER-002-ACT",
        )
        rt.revert_action("RUN-OVER-002-ACT", "RUN-OVER-002-REV", o2c.FINANCE_DIRECTOR)
        assert ("RUN-OVER-002-ACT", "COMMITTED") in events
        assert ("RUN-OVER-002-REV", "COMMITTED") in events
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. 飞书交互卡片零黑话格式化、签名校验与 OpenAPI 推送/更新
# ---------------------------------------------------------------------------
def test_feishu_card_jargon_free_and_openapi_push_patch():
    demo = build_o2c_demo()
    rt = demo.runtime
    http_calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_transport(
        method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        del headers, timeout
        payload = json.loads(body.decode("utf-8") or "{}")
        http_calls.append((method, url, payload))
        if url.endswith("/tenant_access_token/internal"):
            return 200, json.dumps({"code": 0, "tenant_access_token": "t-123", "expire": 7200}).encode()
        if "/open-apis/im/v1/messages?" in url:
            return 200, json.dumps({"code": 0, "data": {"message_id": "om_msg_001"}}).encode()
        if "/open-apis/im/v1/messages/om_msg_001" in url:
            return 200, json.dumps({"code": 0, "msg": "ok"}).encode()
        raise AssertionError(f"unexpected Feishu HTTP call: {method} {url}")

    try:
        client = FeishuOpenAPIClient(
            app_id="cli_test",
            app_secret="sec_test",
            receive_id="ou_director",
            transport=fake_transport,
        )
        gw = FeishuApprovalGateway(
            rt,
            feishu_client=client,
            operator_actors={
                "ou_director": o2c.FINANCE_DIRECTOR,
                "ou_sales": o2c.SALES_REP,
            },
            strict_operator_map=True,
            encrypt_key="enc_key_demo",
            verification_token="ver_tok_demo",
            callback_token="cb_tok_demo",
        )

        # URL verification handshake
        assert gw.handle_feishu_callback(
            {"type": "url_verification", "token": "ver_tok_demo", "challenge": "ch-999"}
        ) == {"challenge": "ch-999"}

        # Stage WorkCard -> auto pushes Feishu interactive card
        pending = gw.submit_or_escalate(
            "FEISHU-PUSH-001",
            "confirm_sales_order",
            {"order": "O2C-SO-002"},
            o2c.AI_AGENT,
        )
        assert pending["status"] == "OPEN"
        assert len(http_calls) == 2  # tenant_access_token + send message
        assert http_calls[1][0] == "POST"

        view = format_run_for_executive(rt, pending)
        card = build_feishu_card(view, callback_token="cb_tok_demo")
        card_blob = json.dumps(card, ensure_ascii=False)
        assert "¥120,000.00" in card_blob
        assert "¥20,000.00" in card_blob
        assert "批准并写入 ERP" in card_blob
        assert "驳回申请" in card_blob
        assert "`" not in card_blob
        for jargon in ("ontology", "tbox", "abox", "object_diff", "objecttype", "total_cents"):
            assert jargon not in card_blob.lower(), jargon

        # Non-approver role in Feishu callback is blocked
        sales_cb = {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "ou_sales"},
                "action": {"value": {"op": "approve", "run_id": "FEISHU-PUSH-001", "token": "cb_tok_demo"}},
            },
        }
        sales_res = gw.handle_feishu_callback(sales_cb)
        assert sales_res["toast"]["type"] == "error"

        # Valid signed v2 callback from finance director -> approves + patches card
        approve_cb = {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "ou_director"},
                "action": {"value": {"op": "approve", "run_id": "FEISHU-PUSH-001", "token": "cb_tok_demo"}},
            },
        }
        raw = json.dumps(approve_cb).encode("utf-8")
        ts, nonce = "1700000000", "nonce-1"
        sig = hashlib.sha256(
            ts.encode() + nonce.encode() + b"enc_key_demo" + raw
        ).hexdigest()
        assert verify_feishu_signature(ts, nonce, "enc_key_demo", raw, sig) is True

        approve_res = gw.handle_feishu_callback(
            approve_cb,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": sig,
            },
            raw_body=raw,
        )
        assert approve_res["status"] == "COMMITTED"
        approved_card_blob = json.dumps(approve_res["card"], ensure_ascii=False)
        assert "紧急冲正撤回" in approved_card_blob
        assert "finance-director-01" not in approved_card_blob
        assert "财务总监" in approved_card_blob
        assert "`" not in approved_card_blob
        assert any(
            method == "PATCH" and "/messages/om_msg_001" in url
            for method, url, _ in http_calls
        )

        # Verify v1 callback format for reject after reverting O2C-SO-002 back to draft
        gw.handle_feishu_callback(
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "ou_director"},
                    "action": {"value": {"op": "revert", "run_id": "FEISHU-PUSH-001", "token": "cb_tok_demo"}},
                },
            }
        )
        gw.submit_or_escalate(
            "FEISHU-REJ-003",
            "confirm_sales_order",
            {"order": "O2C-SO-002"},
            o2c.AI_AGENT,
        )
        rej_res = gw.handle_feishu_callback(
            {
                "open_id": "ou_director",
                "action": {"value": {"op": "reject", "run_id": "FEISHU-REJ-003", "token": "cb_tok_demo"}},
            }
        )
        assert rej_res["status"] == "REJECTED"
        assert rt.get_object(o2c.SalesOrder, "O2C-SO-002", o2c.FINANCE_DIRECTOR).status == "draft"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. 真实 Odoo 19 四幕闭环：拦违规 -> 挂起飞书签批 -> 生效并留备注 -> 1 秒冲正退草稿
# ---------------------------------------------------------------------------
@pytest.fixture()
def live_o2c_gateway():
    rt = OntologyRuntime(o2c.spec)
    adapter = OdooO2CAdapter(rt)
    adapter.seed_odoo()
    adapter.sync_from_odoo()
    gw = FeishuApprovalGateway(rt, adapter=adapter)
    yield rt, adapter, gw
    rt.close()


def test_four_act_real_odoo_feishu_approve_and_revert_loop(live_o2c_gateway):
    rt, adapter, gw = live_o2c_gateway

    # Act 1: AI attempts early invoice -> REJECTED, Odoo untouched
    with pytest.raises(InvariantError) as exc_info:
        rt.submit_action(
            "LIVE-ACT1-INV",
            "issue_and_post_invoice",
            {"order": "O2C-SO-001"},
            o2c.AI_AGENT,
        )
    assert exc_info.value.code == "CANNOT_INVOICE_UNSHIPPED_ORDER"
    assert rt.get_action_run("LIVE-ACT1-INV")["status"] == "REJECTED"
    assert adapter.invoice_row("O2C-SO-001") is None

    # Act 2: AI annotates overlay + escalates 120k order -> M1 WorkCard OPEN, Odoo still draft
    rt.submit_action(
        "LIVE-ACT2-NOTE",
        "annotate_o2c_governance",
        {
            "order": "O2C-SO-002",
            "account_rep": "AI销售助理",
            "fulfillment_note": "战略特批大客户",
        },
        o2c.AI_AGENT,
    )
    pending = gw.submit_or_escalate(
        "LIVE-ACT2-CONFIRM",
        "confirm_sales_order",
        {"order": "O2C-SO-002"},
        o2c.AI_AGENT,
    )
    assert pending["status"] == "OPEN"
    assert adapter.order_row("O2C-SO-002")["state"] == "draft"
    assert gw.get_card_record("LIVE-ACT2-CONFIRM") is not None

    # Act 3: Feishu approve callback -> Odoo confirmed + picking created; sync preserves overlay
    approve_res = gw.handle_feishu_callback(
        {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "demo-finance-director"},
                "action": {"value": {"op": "approve", "run_id": "LIVE-ACT2-CONFIRM"}},
            },
        }
    )
    assert approve_res["status"] == "COMMITTED"
    assert adapter.order_row("O2C-SO-002")["state"] == "sale"
    assert adapter.delivery_row("O2C-SO-002") is not None

    adapter.sync_from_odoo()
    so2 = rt.get_object(o2c.SalesOrder, "O2C-SO-002", o2c.FINANCE_DIRECTOR)
    assert so2.status == "confirmed"
    assert so2.fulfillment_note == "战略特批大客户"
    assert so2.account_rep == "AI销售助理"

    # Act 4: Feishu revert callback -> Odoo returns to draft, picking cancelled, overlay preserved
    revert_res = gw.handle_feishu_callback(
        {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "demo-finance-director"},
                "action": {"value": {"op": "revert", "run_id": "LIVE-ACT2-CONFIRM"}},
            },
        }
    )
    assert revert_res["status"] == "REVERTED"
    assert adapter.order_row("O2C-SO-002")["state"] == "draft"
    assert adapter.delivery_row("O2C-SO-002") is None

    adapter.sync_from_odoo()
    so2_reverted = rt.get_object(o2c.SalesOrder, "O2C-SO-002", o2c.FINANCE_DIRECTOR)
    assert so2_reverted.status == "draft"
    assert so2_reverted.fulfillment_note == "战略特批大客户"


# ---------------------------------------------------------------------------
# 4. 审批等待期间真实 ERP 改价 -> OCC 并发锁自动熔断
# ---------------------------------------------------------------------------
def test_occ_conflict_meltdown_when_erp_changes_during_pending(live_o2c_gateway):
    rt, adapter, gw = live_o2c_gateway

    gw.submit_or_escalate(
        "LIVE-OCC-PENDING",
        "confirm_sales_order",
        {"order": "O2C-SO-002"},
        o2c.AI_AGENT,
    )
    assert adapter.order_row("O2C-SO-002")["state"] == "draft"

    # Mutate real Odoo order price while approval is waiting
    tamper = adapter.tamper_draft_order_price("O2C-SO-002", 10500.0)
    assert tamper["total_cents"] == 12_600_000

    res = gw.handle_feishu_callback(
        {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "demo-finance-director"},
                "action": {"value": {"op": "approve", "run_id": "LIVE-OCC-PENDING"}},
            },
        }
    )
    assert res["status"] == "CONFLICT"
    assert res["error_code"] == "CONCURRENCY_CONFLICT"
    assert res["toast"]["type"] == "error"
    assert adapter.order_row("O2C-SO-002")["state"] == "draft"


# ---------------------------------------------------------------------------
# 5. 物理出库禁撤边界 & 未收款已过账发票真实红冲回退
# ---------------------------------------------------------------------------
def test_physical_shipment_blocks_revert_and_invoice_revert_credit_note(live_o2c_gateway):
    rt, adapter, gw = live_o2c_gateway

    rt.submit_action(
        "SO1-CONFIRM",
        "confirm_sales_order",
        {"order": "O2C-SO-001"},
        o2c.AI_AGENT,
    )
    adapter.process_new_runs()
    rt.submit_action(
        "SO1-SHIP",
        "ship_order_delivery",
        {"order": "O2C-SO-001"},
        o2c.AI_AGENT,
    )
    adapter.process_new_runs()

    assert adapter.delivery_row("O2C-SO-001")["state"] == "done"

    # Attempting to revert confirm after physical shipment must be blocked
    blocked_res = gw.handle_feishu_callback(
        {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "demo-finance-director"},
                "action": {"value": {"op": "revert", "run_id": "SO1-CONFIRM"}},
            },
        }
    )
    assert blocked_res["error_code"] == "REVERT_BLOCKED_ALREADY_SHIPPED"
    assert blocked_res["toast"]["type"] == "error"
    assert adapter.order_row("O2C-SO-001")["state"] == "sale"
    assert adapter.delivery_row("O2C-SO-001")["state"] == "done"

    # Issue and post real invoice -> revert via Feishu callback -> invoice reversed/cancelled
    rt.submit_action(
        "SO1-INV",
        "issue_and_post_invoice",
        {"order": "O2C-SO-001"},
        o2c.AI_AGENT,
    )
    adapter.process_new_runs()
    assert adapter.invoice_row("O2C-SO-001")["state"] == "posted"

    inv_rev = gw.handle_feishu_callback(
        {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "demo-finance-director"},
                "action": {"value": {"op": "revert", "run_id": "SO1-INV"}},
            },
        }
    )
    assert inv_rev["status"] == "REVERTED"
    assert adapter.invoice_row("O2C-SO-001") is None

    adapter.sync_from_odoo()
    assert rt.get_object(o2c.SalesOrder, "O2C-SO-001", o2c.FINANCE_DIRECTOR).status == "shipped"


# ---------------------------------------------------------------------------
# 6. 三栏演播室 HTTP Server 端到端接口与界面零黑话门禁
# ---------------------------------------------------------------------------
def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


def test_demo_http_server_endpoints_and_ui_jargon_guard():
    lower_html = SHOWROOM_HTML.lower()
    for forbidden in ("ontology", "tbox", "abox", "objecttype", "objectdiff", "total_cents"):
        assert forbidden not in lower_html, forbidden

    handle = start_feishu_erp_demo_server(host="127.0.0.1", port=0)
    try:
        base = handle.base_url
        with urllib.request.urlopen(f"{base}/", timeout=10) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "AI 数字员工安全操作 ERP" in html

        initial = _http_json("GET", f"{base}/api/state")
        assert len(initial["orders"]) == 2

        act1 = _http_json(
            "POST",
            f"{base}/api/agent/command",
            {"scenario": "act1_illegal_invoice"},
        )
        assert act1["error_code"] == "CANNOT_INVOICE_UNSHIPPED_ORDER"

        act2 = _http_json(
            "POST",
            f"{base}/api/agent/command",
            {"scenario": "act2_over_credit_order"},
        )
        run_id = act2["run_id"]
        assert act2["status"] == "OPEN"

        approved = _http_json(
            "POST",
            f"{base}/api/feishu/card-callback",
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "demo-finance-director"},
                    "action": {"value": {"op": "approve", "run_id": run_id}},
                },
            },
        )
        assert approved["status"] == "COMMITTED"
        so2_approved = next(o for o in approved["state"]["orders"] if o["order_id"] == "O2C-SO-002")
        assert so2_approved["odoo_order_state_raw"] == "sale"

        reverted = _http_json(
            "POST",
            f"{base}/api/feishu/card-callback",
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "demo-finance-director"},
                    "action": {"value": {"op": "revert", "run_id": run_id}},
                },
            },
        )
        assert reverted["status"] == "REVERTED"
        so2_rev = next(o for o in reverted["state"]["orders"] if o["order_id"] == "O2C-SO-002")
        assert so2_rev["odoo_order_state_raw"] == "draft"

        reset_state = _http_json("POST", f"{base}/api/reset", {})
        assert len(reset_state["audit_runs"]) == 0
    finally:
        handle.close()
