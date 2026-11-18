"""Hermes Agent o2c MCP Server 与跨进程持久化、飞书卡片闭环测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from examples.o2c_hermes_mcp_server import (
    O2CMCPHandler,
    PersistentO2CSession,
    execute_feishu_card_slash_command,
)

ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = ROOT / "examples" / "o2c_hermes_mcp_server.py"

BANNED_JARGON = ("ontology", "tbox", "abox", "objecttype", "object_diff", "_cents")


def _assert_no_jargon(payload: Any) -> None:
    blob = json.dumps(payload, ensure_ascii=False).lower()
    for term in BANNED_JARGON:
        assert term not in blob, f"jargon {term!r} leaked into MCP payload"


# ---------------------------------------------------------------------------
# 1. stdio NDJSON JSON-RPC 协议握手与工具注册（子进程实测）
# ---------------------------------------------------------------------------
def test_mcp_stdio_jsonrpc_protocol_and_zero_jargon(tmp_path):
    state_dir = tmp_path / "state"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "o2c_overview", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
    ]
    stdin_blob = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, str(SERVER_SCRIPT), "--state-dir", str(state_dir)],
        input=stdin_blob,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 4  # notifications/initialized produces no response

    init_resp, list_resp, call_resp, ping_resp = lines
    assert init_resp["id"] == 1
    assert init_resp["result"]["protocolVersion"] == "2024-11-05"
    assert init_resp["result"]["serverInfo"]["name"] == "o2c-erp"

    tools = list_resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert tool_names == [
        "o2c_overview",
        "o2c_reset_demo",
        "o2c_create_order",
        "o2c_annotate_governance",
        "o2c_confirm_order",
        "o2c_ship_order",
        "o2c_issue_invoice",
        "o2c_record_payment",
        "o2c_handle_card_decision",
        "o2c_demo_fault_injection",
    ]
    _assert_no_jargon(tools)

    overview_text = call_resp["result"]["content"][0]["text"]
    overview_data = json.loads(overview_text)
    assert overview_data["ok"] is True
    assert len(overview_data["orders"]) == 2
    _assert_no_jargon(overview_data)
    assert ping_resp == {"jsonrpc": "2.0", "id": 4, "result": {}}


# ---------------------------------------------------------------------------
# 2. 跨进程持久化 + 自动发现 Hermes Profile 飞书配置 + 真实 Odoo 19 四幕闭环
# ---------------------------------------------------------------------------
def test_persistent_session_hydration_across_restarts_and_feishu_cards(tmp_path):
    state_dir = tmp_path / "o2c_state"
    profile_dir = tmp_path / "profile_o2c"
    (profile_dir / "platforms" / "pairing").mkdir(parents=True)
    (profile_dir / ".env").write_text(
        "FEISHU_APP_ID=cli_test_o2c\nFEISHU_APP_SECRET=sec_test_o2c\n",
        encoding="utf-8",
    )
    (profile_dir / "channel_directory.json").write_text(
        json.dumps(
            {
                "platforms": {
                    "feishu": [{"id": "oc_test_chat_001", "type": "dm"}]
                }
            }
        ),
        encoding="utf-8",
    )
    (profile_dir / "platforms" / "pairing" / "feishu-approved.json").write_text(
        json.dumps({"ou_6d42d7d8888a78710605e6245258f13c": {"user_name": "Guci"}}),
        encoding="utf-8",
    )

    feishu_calls: list[tuple[str, str, dict[str, Any]]] = []
    msg_counter = {"n": 0}

    def fake_feishu(
        method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        del headers, timeout
        payload = json.loads(body.decode("utf-8") or "{}")
        feishu_calls.append((method, url, payload))
        if url.endswith("/tenant_access_token/internal"):
            return 200, json.dumps({"code": 0, "tenant_access_token": "t-o2c", "expire": 7200}).encode()
        if "/open-apis/im/v1/messages?" in url:
            msg_counter["n"] += 1
            return 200, json.dumps(
                {"code": 0, "data": {"message_id": f"om_hermes_{msg_counter['n']:03d}"}}
            ).encode()
        if "/open-apis/im/v1/messages/om_hermes_" in url and method == "PATCH":
            return 200, json.dumps({"code": 0, "msg": "ok"}).encode()
        raise AssertionError(f"unexpected Feishu HTTP call: {method} {url}")

    # --- Session 1: 第一幕拦截 + 第二幕挂起并推飞书审批卡片 ---
    s1 = PersistentO2CSession(state_dir, profile_dir=profile_dir, feishu_transport=fake_feishu)
    try:
        h1 = O2CMCPHandler(s1)
        ov = h1.call_tool("o2c_overview")
        assert ov["feishu_push_ready"] is True
        assert ov["feishu_receive_id"] == "oc_test_chat_001"

        act1 = h1.call_tool("o2c_issue_invoice", {"order_id": "O2C-SO-001"})
        assert act1["blocked"] is True
        assert act1["error_code"] == "CANNOT_INVOICE_UNSHIPPED_ORDER"
        assert act1["card_pushed"] is True
        assert s1.adapter.invoice_row("O2C-SO-001") is None
        _assert_no_jargon(act1)

        act2 = h1.call_tool(
            "o2c_confirm_order",
            {
                "order_id": "O2C-SO-002",
                "account_rep": "AI销售助理",
                "fulfillment_note": "战略特批大客户",
            },
        )
        assert act2["status_code"] == "PENDING_APPROVAL"
        assert act2["card_pushed"] is True
        confirm_run_id = act2["run_id"]
        confirm_msg_id = act2["card_message_id"]
        assert confirm_msg_id == "om_hermes_002"
        assert s1.adapter.order_row("O2C-SO-002")["state"] == "draft"
        _assert_no_jargon(act2)
        # Confirmed push targeted chat_id from channel_directory.json
        assert any(
            method == "POST" and "receive_id_type=chat_id" in url
            and payload.get("receive_id") == "oc_test_chat_001"
            for method, url, payload in feishu_calls
        )
    finally:
        s1.close()

    # --- Session 2: 模拟进程重启后收到 Hermes /card button approve 指令 ---
    s2 = PersistentO2CSession(state_dir, profile_dir=profile_dir, feishu_transport=fake_feishu)
    try:
        h2 = O2CMCPHandler(s2)
        ov2 = h2.call_tool("o2c_overview")
        so2_before = next(o for o in ov2["orders"] if o["order_id"] == "O2C-SO-002")
        assert so2_before["fulfillment_note"] == "战略特批大客户"
        assert so2_before["odoo_order_state"] == "草稿"

        approve_res = h2.call_tool(
            "o2c_handle_card_decision",
            {
                "raw_card_command": f'/card button {{"op": "approve", "run_id": "{confirm_run_id}"}}',
            },
        )
        assert approve_res["ok"] is True
        assert approve_res["status_code"] == "COMMITTED"
        assert approve_res["card_message_id"] == confirm_msg_id
        assert approve_res["card_patched"] is True
        assert s2.adapter.order_row("O2C-SO-002")["state"] == "sale"
        assert any(
            method == "PATCH"
            and f"/messages/{confirm_msg_id}" in url
            and "紧急冲正撤回" in payload.get("content", "")
            for method, url, payload in feishu_calls
        )

        # Full ERP snapshot resync keeps fulfillment_note alive
        sync_res = h2.call_tool("o2c_demo_fault_injection", {"mode": "sync_from_erp"})
        assert sync_res["order"]["fulfillment_note"] == "战略特批大客户"
    finally:
        s2.close()

    # --- Session 3: 模拟再次重启后收到 Hermes /card button revert 指令 ---
    s3 = PersistentO2CSession(state_dir, profile_dir=profile_dir, feishu_transport=fake_feishu)
    try:
        h3 = O2CMCPHandler(s3)
        revert_res = h3.call_tool(
            "o2c_handle_card_decision",
            {
                "raw_card_command": f'/card button {{"op": "revert", "run_id": "{confirm_run_id}"}}',
            },
        )
        assert revert_res["ok"] is True
        assert revert_res["status_code"] == "REVERTED"
        assert revert_res["card_patched"] is True
        assert s3.adapter.order_row("O2C-SO-002")["state"] == "draft"
        assert s3.adapter.delivery_row("O2C-SO-002") is None
        assert revert_res["order"]["fulfillment_note"] == "战略特批大客户"
        assert any(
            method == "PATCH"
            and f"/messages/{confirm_msg_id}" in url
            and "已冲正撤回" in payload.get("content", "")
            for method, url, payload in feishu_calls
        )
    finally:
        s3.close()


# ---------------------------------------------------------------------------
# 3. 飞书 /card 插件直通命令（无 LLM）+ 既有长驻会话热重载感知
# ---------------------------------------------------------------------------
def test_execute_feishu_card_slash_command_reloads_live_session_and_sends_text(tmp_path):
    state_dir = tmp_path / "o2c_state"
    profile_dir = tmp_path / "profile_o2c"
    (profile_dir / "platforms" / "pairing").mkdir(parents=True)
    (profile_dir / ".env").write_text(
        "FEISHU_APP_ID=cli_test_o2c\nFEISHU_APP_SECRET=sec_test_o2c\n",
        encoding="utf-8",
    )
    (profile_dir / "channel_directory.json").write_text(
        json.dumps({"platforms": {"feishu": [{"id": "oc_test_chat_001", "type": "dm"}]}}),
        encoding="utf-8",
    )
    (profile_dir / "platforms" / "pairing" / "feishu-approved.json").write_text(
        json.dumps({"ou_6d42d7d8888a78710605e6245258f13c": {"user_name": "Guci"}}),
        encoding="utf-8",
    )

    feishu_calls: list[tuple[str, str, dict[str, Any]]] = []
    msg_counter = {"n": 0}

    def fake_feishu(
        method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        del headers, timeout
        payload = json.loads(body.decode("utf-8") or "{}")
        feishu_calls.append((method, url, payload))
        if url.endswith("/tenant_access_token/internal"):
            return 200, json.dumps({"code": 0, "tenant_access_token": "t-o2c", "expire": 7200}).encode()
        if "/open-apis/im/v1/messages?" in url:
            msg_counter["n"] += 1
            return 200, json.dumps(
                {"code": 0, "data": {"message_id": f"om_card_{msg_counter['n']:03d}"}}
            ).encode()
        if "/open-apis/im/v1/messages/om_card_" in url and method == "PATCH":
            return 200, json.dumps({"code": 0, "msg": "ok"}).encode()
        raise AssertionError(f"unexpected Feishu HTTP call: {method} {url}")

    live_session = PersistentO2CSession(
        state_dir, profile_dir=profile_dir, feishu_transport=fake_feishu
    )
    try:
        confirm_res = live_session.confirm_order(
            {
                "order_id": "O2C-SO-002",
                "account_rep": "AI销售助理",
                "fulfillment_note": "战略特批大客户",
            }
        )
        assert confirm_res["status_code"] == "PENDING_APPROVAL"
        confirm_run_id = confirm_res["run_id"]
        card_msg_id = confirm_res["card_message_id"]
        assert live_session.adapter.order_row("O2C-SO-002")["state"] == "draft"

        approve_res = execute_feishu_card_slash_command(
            f'button {{"op": "approve", "run_id": "{confirm_run_id}", "token": ""}}',
            state_dir=state_dir,
            profile_dir=profile_dir,
            operator_open_id="ou_6d42d7d8888a78710605e6245258f13c",
            chat_id="oc_test_chat_001",
            feishu_transport=fake_feishu,
        )
        assert approve_res["ok"] is True
        assert approve_res["status_code"] == "COMMITTED"
        assert approve_res["card_message_id"] == card_msg_id
        assert approve_res["card_patched"] is True
        assert approve_res["notice_sent"] is True
        assert approve_res["notice_error"] is None
        _assert_no_jargon(approve_res)

        assert any(
            method == "PATCH"
            and f"/messages/{card_msg_id}" in url
            and "紧急冲正撤回" in payload.get("content", "")
            for method, url, payload in feishu_calls
        )
        assert any(
            method == "POST"
            and "/open-apis/im/v1/messages?receive_id_type=chat_id" in url
            and payload.get("msg_type") == "text"
            and confirm_run_id in json.loads(payload.get("content", "{}")).get("text", "")
            for method, url, payload in feishu_calls
        )

        # 长驻旧会话不重启直接 overview，自动感知跨进程批准写入
        ov_after_approve = live_session.overview()
        so2_approved = next(o for o in ov_after_approve["orders"] if o["order_id"] == "O2C-SO-002")
        assert so2_approved["odoo_order_state"] == "已确认"
        assert so2_approved["fulfillment_note"] == "战略特批大客户"
        assert live_session.adapter.order_row("O2C-SO-002")["state"] == "sale"

        revert_res = execute_feishu_card_slash_command(
            f'button {{"op": "revert", "run_id": "{confirm_run_id}", "token": ""}}',
            state_dir=state_dir,
            profile_dir=profile_dir,
            operator_open_id="ou_6d42d7d8888a78710605e6245258f13c",
            chat_id="oc_test_chat_001",
            feishu_transport=fake_feishu,
        )
        assert revert_res["ok"] is True
        assert revert_res["status_code"] == "REVERTED"
        assert revert_res["card_patched"] is True
        assert revert_res["notice_sent"] is True
        assert revert_res["notice_error"] is None
        _assert_no_jargon(revert_res)

        ov_after_revert = live_session.overview()
        so2_reverted = next(o for o in ov_after_revert["orders"] if o["order_id"] == "O2C-SO-002")
        assert so2_reverted["odoo_order_state"] == "草稿"
        assert live_session.adapter.order_row("O2C-SO-002")["state"] == "draft"
    finally:
        live_session.close()


# ---------------------------------------------------------------------------
# 4. 从零建单演练：空白重置 → 新建销售订单 → 确认/出库/开票/收款终态闭环
# ---------------------------------------------------------------------------
def test_create_order_from_zero_orders_and_full_lifecycle(tmp_path):
    from examples import o2c_ontology as o2c

    state_dir = tmp_path / "o2c_zero_state"
    session = PersistentO2CSession(state_dir)
    try:
        handler = O2CMCPHandler(session)

        reset_res = handler.call_tool("o2c_reset_demo", {"empty_orders": True})
        assert reset_res["ok"] is True
        assert reset_res["orders"] == []
        _assert_no_jargon(reset_res)

        created = handler.call_tool(
            "o2c_create_order",
            {
                "quantity": 5,
                "unit_price_yuan": 10000,
                "account_rep": "AI销售助理",
                "fulfillment_note": "从零新建首单",
            },
        )
        assert created["ok"] is True
        assert created["blocked"] is False
        assert created["order"]["order_id"] == "O2C-SO-001"
        assert created["order"]["amount_yuan"] == "¥50,000.00"
        assert created["order"]["odoo_order_state"] == "草稿"
        assert session.adapter.order_row("O2C-SO-001")["state"] == "draft"
        assert float(session.adapter.order_row("O2C-SO-001")["amount_total"]) == 50000.0
        ont_order = session.runtime.get_object(o2c.SalesOrder, "O2C-SO-001", o2c.FINANCE_DIRECTOR)
        assert ont_order.status == "draft"
        assert ont_order.fulfillment_note == "从零新建首单"
        _assert_no_jargon(created)

        confirmed = handler.call_tool("o2c_confirm_order", {"order_id": "O2C-SO-001"})
        assert confirmed["status_code"] == "COMMITTED"
        assert session.adapter.order_row("O2C-SO-001")["state"] == "sale"

        shipped = handler.call_tool("o2c_ship_order", {"order_id": "O2C-SO-001"})
        assert shipped["status_code"] == "COMMITTED"

        invoiced = handler.call_tool("o2c_issue_invoice", {"order_id": "O2C-SO-001"})
        assert invoiced["status_code"] == "COMMITTED"

        paid = handler.call_tool(
            "o2c_record_payment",
            {"order_id": "O2C-SO-001", "payment_yuan": 50000},
        )
        assert paid["status_code"] == "COMMITTED"
        final_order = session.runtime.get_object(o2c.SalesOrder, "O2C-SO-001", o2c.FINANCE_DIRECTOR)
        assert final_order.status == "paid"
        ledger = session.runtime.get_object(
            o2c.ReceivableBalance, "AR-O2C-CUST-001", o2c.FINANCE_DIRECTOR
        )
        assert ledger.ar_balance_cents == 0
    finally:
        session.close()
