"""Acceptance tests for WO-16: Pure M2 MCP Server & 5-Primitive REA Commonsense Interop."""

from __future__ import annotations

import ast
import http.client
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from open_ontology import (
    Actor,
    DeviceDriverRegistry,
    InvariantError,
    OntologyRuntime,
    PermissionDeniedError,
    SourceBridge,
)
from open_ontology.drivers import MountedDevice
from open_ontology.induction import (
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    InvariantRuleSpec,
    apply_overlay,
    compile_blueprint,
    compose_blueprints,
)
from open_ontology.mcp_server import OntologyMCPServer, start_mcp_http_server

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.commonsense_exchange import (
    ALLOWED_COMMONSENSE_TYPES,
    CommonsenseEnvelope,
    O2cCommonsensePort,
    P2pCommonsensePort,
    validate_commonsense_envelope,
)
from examples.std_core_ontology import build_std_core_blueprint
from examples.std_crm_ontology import build_std_crm_blueprint
from examples.std_o2c_ontology import build_std_o2c_blueprint
from examples.std_p2p_ontology import build_std_p2p_blueprint

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Factories for real subprocess CLI stdio test & HTTP MCP tests
# ---------------------------------------------------------------------------
def _seed_o2c_mcp_runtime(*, include_crm_overlay: bool = False) -> tuple[OntologyRuntime, dict[str, Any]]:
    bps = [build_std_core_blueprint(), build_std_o2c_blueprint()]
    if include_crm_overlay:
        bps = [build_std_core_blueprint(), build_std_crm_blueprint(), build_std_o2c_blueprint()]
    base = compose_blueprints(bps, model_id="mcp_o2c_demo", version="1.0.0")
    if include_crm_overlay:
        overlay = CustomerOverlaySpec(
            overlay_id="steel_seller_ticket_guard",
            base_model_id=base.model_id,
            target_version="1.1.0",
            add_action_preconditions={
                "confirm_o2c_order": [
                    InvariantRuleSpec(
                        name="no_open_urgent_ticket_before_order_confirm",
                        code="URGENT_TICKET_BLOCKS_ORDER",
                        target_type="O2cOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "not_exists",
                            "object_type": "CrmTicket",
                            "where": {
                                "op": "and",
                                "args": [
                                    {"op": "eq", "left": {"ref": "target.account.party"}, "right": {"ref": "obj.customer.party"}},
                                    {"op": "in", "left": {"ref": "target.priority"}, "right": {"lit": ["HIGH", "URGENT"]}},
                                    {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                                ],
                            },
                        },
                        description="Customer has unresolved HIGH/URGENT CrmTicket blocking order confirmation.",
                    )
                ]
            },
            restrict_allowed_roles={
                "resolve_crm_ticket": ["support_agent", "ops", "steward", "admin"],
            },
        )
        base = apply_overlay(base, overlay)

    spec = compile_blueprint(base, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, db_path=":memory:")
    admin = Actor(actor_id="seed-admin", role="admin", clearance_tags={"*"})

    ent_cls = spec.get_object_type("AccountingEntity")
    party_cls = spec.get_object_type("BusinessParty")
    prof_cls = spec.get_object_type("O2cCustomerProfile")
    ord_cls = spec.get_object_type("O2cOrder")
    del_cls = spec.get_object_type("O2cDelivery")
    inv_cls = spec.get_object_type("O2cInvoice")

    rt.put_object(ent_cls(entity_id="ENT-NORTH", name="华北钢贸主体", base_currency="CNY"), actor=admin)
    rt.put_object(
        party_cls(party_id="PTY-BUY-01", legal_name="华东建工集团有限公司", tax_id="91310000111111111A"),
        actor=admin,
    )
    rt.put_object(
        prof_cls(
            profile_id="PROF-BUY-01",
            party="PTY-BUY-01",
            accounting_entity="ENT-NORTH",
            credit_limit=1_000_000.0,
            payment_terms="NET30",
        ),
        actor=admin,
    )
    rt.put_object(
        ord_cls(
            order_id="ORD-OK-01",
            order_number="SO-OK-01",
            customer="PROF-BUY-01",
            accounting_entity="ENT-NORTH",
            amount_total=36_000.0,
            status="draft",
        ),
        actor=admin,
    )
    rt.put_object(
        ord_cls(
            order_id="ORD-ZERO-02",
            order_number="SO-ZERO-02",
            customer="PROF-BUY-01",
            accounting_entity="ENT-NORTH",
            amount_total=0.0,
            status="draft",
        ),
        actor=admin,
    )
    rt.put_object(del_cls(delivery_id="DEL-OK-01", order="ORD-OK-01", status="pending"), actor=admin)
    rt.put_object(
        inv_cls(
            invoice_id="INV-OK-01",
            order="ORD-OK-01",
            amount_total=36_000.0,
            amount_residual=36_000.0,
            payment_state="not_paid",
            status="draft",
        ),
        actor=admin,
    )
    return rt, {"admin": admin}


def make_stdio_demo_runtime() -> OntologyRuntime:
    rt, _ = _seed_o2c_mcp_runtime(include_crm_overlay=False)
    return rt


def _send_stdio_rpc(proc: subprocess.Popen[str], msg: dict[str, Any]) -> dict[str, Any] | None:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    if "id" not in msg or msg["id"] is None:
        return None
    line = proc.stdout.readline()
    assert line, f"unexpected EOF from stdio MCP server; stderr={proc.stderr.read() if proc.stderr else ''}"
    return json.loads(line)


# ---------------------------------------------------------------------------
# 1. Subprocess MCP stdio test
# ---------------------------------------------------------------------------
def test_mcp_stdio_subprocess_full_protocol_and_guardrails() -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "open_ontology.mcp_server",
            "--factory",
            "tests.test_commonsense_interop_and_mcp:make_stdio_demo_runtime",
            "--transport",
            "stdio",
            "--actor-id",
            "rep-stdio-01",
            "--actor-role",
            "sales_rep",
        ],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        init_res = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "pytest", "version": "1.0"}},
            },
        )
        assert init_res is not None
        assert init_res["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in init_res["result"]["capabilities"]
        assert "resources" in init_res["result"]["capabilities"]

        # Notification must produce zero stdout reply; immediate ping must carry id=2 next.
        assert _send_stdio_rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) is None
        ping_res = _send_stdio_rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert ping_res == {"jsonrpc": "2.0", "id": 2, "result": {}}

        tools_res = _send_stdio_rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert tools_res is not None
        tool_names = {t["name"] for t in tools_res["result"]["tools"]}
        assert {"describe_tbox", "query_objects", "simulate_action", "submit_action", "confirm_o2c_order"} <= tool_names
        assert "emit_ui_artifact" not in tool_names
        assert "plan_mcts" not in tool_names

        res_list = _send_stdio_rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        assert res_list is not None
        uris = {r["uri"] for r in res_list["result"]["resources"]}
        assert uris == {"ontology://tbox", "ontology://devices"}

        tbox_read = _send_stdio_rpc(
            proc,
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "ontology://tbox"}},
        )
        assert tbox_read is not None
        tbox_json = json.loads(tbox_read["result"]["contents"][0]["text"])
        assert "O2cOrder" in {ot["name"] for ot in tbox_json["object_types"]}

        dev_read = _send_stdio_rpc(
            proc,
            {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "ontology://devices"}},
        )
        assert dev_read is not None
        assert json.loads(dev_read["result"]["contents"][0]["text"]) == []

        # Query + simulate_action (dry-run) + submit_action (commit)
        q_res = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "query_objects", "arguments": {"object_type": "O2cOrder"}},
            },
        )
        assert q_res is not None and q_res["result"]["isError"] is False
        q_payload = json.loads(q_res["result"]["content"][0]["text"])
        assert q_payload["count"] == 2

        sim_res = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "simulate_action",
                    "arguments": {"action": "confirm_o2c_order", "params": {"order_id": "ORD-OK-01"}},
                },
            },
        )
        assert sim_res is not None and sim_res["result"]["isError"] is False
        sim_payload = json.loads(sim_res["result"]["content"][0]["text"])
        assert sim_payload["valid"] is True and sim_payload["error"] is None

        # Still draft after simulation
        chk_res = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "get_object", "arguments": {"object_type": "O2cOrder", "object_id": "ORD-OK-01"}},
            },
        )
        assert chk_res is not None
        assert json.loads(chk_res["result"]["content"][0]["text"])["object"]["status"] == "draft"

        sub_ok = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "submit_action",
                    "arguments": {"run_id": "RUN-MCP-OK", "action": "confirm_o2c_order", "params": {"order_id": "ORD-OK-01"}},
                },
            },
        )
        assert sub_ok is not None and sub_ok["result"]["isError"] is False
        assert json.loads(sub_ok["result"]["content"][0]["text"])["run"]["status"] == "COMMITTED"

        # Invariant violation returns structured isError=True & leaves order draft
        sub_bad = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "submit_action",
                    "arguments": {"run_id": "RUN-MCP-BAD", "action": "confirm_o2c_order", "params": {"order_id": "ORD-ZERO-02"}},
                },
            },
        )
        assert sub_bad is not None and sub_bad["result"]["isError"] is True
        bad_err = json.loads(sub_bad["result"]["content"][0]["text"])
        assert bad_err["ok"] is False
        assert bad_err["code"] == "NON_POSITIVE_ORDER_AMOUNT"

        # RBAC violation: sales_rep cannot post_o2c_invoice
        sub_rbac = _send_stdio_rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "submit_action",
                    "arguments": {"run_id": "RUN-MCP-RBAC", "action": "post_o2c_invoice", "params": {"invoice_id": "INV-OK-01"}},
                },
            },
        )
        assert sub_rbac is not None and sub_rbac["result"]["isError"] is True
        rbac_err = json.loads(sub_rbac["result"]["content"][0]["text"])
        assert rbac_err["error_type"] == "PermissionDeniedError"
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.wait(timeout=5)
        if proc.stderr:
            stderr_txt = proc.stderr.read()
            assert stderr_txt == ""
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# 2. Streamable HTTP MCP test + Header-Spoofing Security Red Test
# ---------------------------------------------------------------------------
def _http_req(
    host: str,
    port: int,
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        hdrs = dict(headers or {})
        raw = ""
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False)
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=raw.encode("utf-8") if body is not None else None, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        resp_hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_hdrs, data
    finally:
        conn.close()


def test_mcp_streamable_http_batch_devices_and_actor_pinning_security() -> None:
    rt, ctx_actors = _seed_o2c_mcp_runtime(include_crm_overlay=False)
    try:
        # Attach a mounted device manifest via DeviceDriverRegistry without changing runtime.py
        reg = DeviceDriverRegistry()
        reg.mount(
            MountedDevice(
                device_id="mock-erp-north",
                name="North Mock Driver",
                kind="mock_jsonrpc",
                endpoint="mock://north",
                bridge=SourceBridge(
                    actuate=lambda m: None,
                    observe=lambda m=None: {},
                    allow_source_revert=False,
                ),
                owned_object_types=("O2cOrder",),
                handled_actions=("confirm_o2c_order",),
                status="MOUNTED",
            )
        )
        rt.bind_source_bridge(reg.as_source_bridge(), world_mode="coupled")

        pinned_actor = Actor(actor_id="rep-http-01", role="sales_rep", clearance_tags={"*"})
        mcp_srv = OntologyMCPServer(rt, pinned_actor)
        with start_mcp_http_server(mcp_srv, host="127.0.0.1", port=0) as hdl:
            st, _, health_raw = _http_req(hdl.host, hdl.port, "GET", "/health")
            assert st == 200
            health = json.loads(health_raw)
            assert health["ok"] is True
            assert health["actor_role"] == "sales_rep"
            assert health["tool_count"] >= 10

            # Initialize + Notification (202 Accepted) + Session header
            st_init, hdr_init, init_raw = _http_req(
                hdl.host,
                hdl.port,
                "POST",
                "/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            )
            assert st_init == 200
            sid = hdr_init.get("mcp-session-id")
            assert sid
            assert json.loads(init_raw)["result"]["protocolVersion"] == "2024-11-05"

            st_notif, _, notif_raw = _http_req(
                hdl.host,
                hdl.port,
                "POST",
                "/mcp",
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={"Mcp-Session-Id": sid},
            )
            assert st_notif == 202
            assert notif_raw == ""

            # Security Red Test: forged client headers & role escalation args MUST be ignored!
            before_fp = rt.state_fingerprint()
            spoof_headers = {
                "Mcp-Session-Id": sid,
                "X-Ontology-Role": "admin",
                "X-Actor-Role": "admin",
                "X-Actor-Id": "forged-superuser",
                "Authorization": "Bearer admin-secret",
            }
            st_spoof, _, spoof_raw = _http_req(
                hdl.host,
                hdl.port,
                "POST",
                "/mcp",
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 21,
                        "method": "tools/call",
                        "params": {
                            "name": "submit_action",
                            "arguments": {
                                "run_id": "RUN-FORGED-ADMIN",
                                "action": "post_o2c_invoice",
                                "params": {"invoice_id": "INV-OK-01"},
                                "actor_role": "admin",
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 22,
                        "method": "resources/read",
                        "params": {"uri": "ontology://devices"},
                    },
                ],
                headers=spoof_headers,
            )
            assert st_spoof == 200
            batch_res = json.loads(spoof_raw)
            assert len(batch_res) == 2

            inv_call = batch_res[0]["result"]
            assert inv_call["isError"] is True
            err_obj = json.loads(inv_call["content"][0]["text"])
            assert err_obj["error_type"] == "PermissionDeniedError"
            assert rt.state_fingerprint() == before_fp
            assert rt.get_object("O2cInvoice", "INV-OK-01", ctx_actors["admin"]).status == "draft"

            devices = json.loads(batch_res[1]["result"]["contents"][0]["text"])
            assert len(devices) == 1
            assert devices[0]["device_id"] == "mock-erp-north"
            assert devices[0]["status"] == "MOUNTED"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. Cross-Enterprise 5-Primitive REA Commonsense Steel-Trading Tests
# ---------------------------------------------------------------------------
def test_commonsense_exchange_strict_isolation_and_whitelist() -> None:
    p2p_src = (REPO_ROOT / "examples" / "std_p2p_ontology.py").read_text(encoding="utf-8")
    assert "std_o2c_ontology" not in p2p_src
    assert "std_crm_ontology" not in p2p_src
    for forbidden in ("O2cOrder", "O2cInvoice", "O2cDelivery", "CrmTicket", "CrmDeal"):
        assert forbidden not in p2p_src

    ex_src = (REPO_ROOT / "examples" / "commonsense_exchange.py").read_text(encoding="utf-8")
    tree = ast.parse(ex_src)
    cls_src = {
        node.name: ast.get_source_segment(ex_src, node) or ""
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    p2p_port_src = cls_src["P2pCommonsensePort"]
    o2c_port_src = cls_src["O2cCommonsensePort"]
    assert "O2c" not in p2p_port_src and "Crm" not in p2p_port_src
    assert "P2p" not in o2c_port_src and "Crm" not in o2c_port_src

    with pytest.raises(ValueError):
        validate_commonsense_envelope(
            {
                "protocol": "open-ontology-rea-waist/v1",
                "envelope_id": "ENV-BAD",
                "sender_party_id": "A",
                "receiver_party_id": "B",
                "correlation_id": "CM-1",
                "message_kind": "DISPUTE_RAISED",
                "parties": [],
                "resources": [],
                "commitments": [],
                "events": [],
                "work_cards": [
                    {
                        "card_id": "WC-1",
                        "target_object_type": "CrmTicket",
                        "target_object_id": "TK-1",
                    }
                ],
            }
        )


def _make_steel_buyer_seller_worlds(
    *,
    seller_urgent_ticket: bool = False,
) -> tuple[OntologyRuntime, P2pCommonsensePort, OntologyRuntime, O2cCommonsensePort, dict[str, Actor]]:
    p2p_bp = build_std_p2p_blueprint()
    p2p_spec = compile_blueprint(p2p_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    buyer_rt = OntologyRuntime(p2p_spec, db_path=":memory:")

    if seller_urgent_ticket:
        base_seller = compose_blueprints(
            [build_std_core_blueprint(), build_std_crm_blueprint(), build_std_o2c_blueprint()],
            model_id="steel_seller_crm_o2c",
            version="1.0.0",
        )
        overlay = CustomerOverlaySpec(
            overlay_id="steel_seller_urgent_ticket_guard",
            base_model_id=base_seller.model_id,
            target_version="1.1.0",
            add_action_preconditions={
                "confirm_o2c_order": [
                    InvariantRuleSpec(
                        name="no_open_urgent_ticket_before_order_confirm",
                        code="URGENT_TICKET_BLOCKS_ORDER",
                        target_type="O2cOrder",
                        id_param="order_id",
                        must_hold={
                            "op": "not_exists",
                            "object_type": "CrmTicket",
                            "where": {
                                "op": "and",
                                "args": [
                                    {"op": "eq", "left": {"ref": "target.account.party"}, "right": {"ref": "obj.customer.party"}},
                                    {"op": "in", "left": {"ref": "target.priority"}, "right": {"lit": ["HIGH", "URGENT"]}},
                                    {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                                ],
                            },
                        },
                    )
                ]
            },
        )
        seller_bp = apply_overlay(base_seller, overlay)
    else:
        seller_bp = build_std_o2c_blueprint()

    seller_spec = compile_blueprint(seller_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    seller_rt = OntologyRuntime(seller_spec, db_path=":memory:")

    admin = Actor(actor_id="sys-admin", role="admin", clearance_tags={"*"})
    buyer_actor = Actor(actor_id="buyer-lin", role="buyer", clearance_tags={"*"})
    buyer_wh = Actor(actor_id="buyer-wh", role="warehouse", clearance_tags={"*"})
    buyer_pm = Actor(actor_id="buyer-pm", role="procurement_manager", clearance_tags={"*"})
    buyer_fin = Actor(actor_id="buyer-fin", role="finance", clearance_tags={"*"})

    seller_sales = Actor(actor_id="seller-sales", role="sales_rep", clearance_tags={"*"})
    seller_wh = Actor(actor_id="seller-wh", role="warehouse", clearance_tags={"*"})
    seller_fin = Actor(actor_id="seller-fin", role="finance", clearance_tags={"*"})
    seller_sup = Actor(actor_id="seller-sup", role="support_agent", clearance_tags={"*"})

    # Seed steel-trading instance data strictly inside test (100t HRB400E rebar @ 3600 CNY/t)
    buyer_ent_cls = p2p_spec.get_object_type("AccountingEntity")
    buyer_party_cls = p2p_spec.get_object_type("BusinessParty")
    buyer_res_cls = p2p_spec.get_object_type("TradeResource")
    sup_cls = p2p_spec.get_object_type("P2pSupplierProfile")
    po_cls = p2p_spec.get_object_type("P2pPurchaseOrder")

    buyer_rt.put_object(buyer_ent_cls(entity_id="ENT-BUYER", name="华东建工采购主体", base_currency="CNY"), actor=admin)
    buyer_rt.put_object(
        buyer_party_cls(party_id="PTY-BUYER-HD", legal_name="华东建工集团有限公司", tax_id="91310000888888881X"),
        actor=admin,
    )
    buyer_rt.put_object(
        buyer_party_cls(party_id="PTY-SELLER-BG", legal_name="宝山特钢贸易有限公司", tax_id="91310000999999992Y"),
        actor=admin,
    )
    buyer_rt.put_object(
        buyer_res_cls(
            resource_id="RES-HRB400E-16",
            code="HRB400E-16MM",
            name="HRB400E 抗震螺纹钢",
            unit="TON",
            spec_summary="Φ16mm 国标热轧带肋钢筋",
        ),
        actor=admin,
    )
    buyer_rt.put_object(
        sup_cls(
            profile_id="SUP-BG-01",
            party="PTY-SELLER-BG",
            accounting_entity="ENT-BUYER",
            payment_terms="NET30",
        ),
        actor=admin,
    )
    buyer_rt.put_object(
        po_cls(
            po_id="PO-ST-100",
            po_number="PO-2026-HRB-100",
            supplier="SUP-BG-01",
            accounting_entity="ENT-BUYER",
            ordered_qty=100.0,
            unit_price=3600.0,
            amount_total=360_000.0,
            status="draft",
        ),
        actor=admin,
    )

    seller_ent_cls = seller_spec.get_object_type("AccountingEntity")
    seller_party_cls = seller_spec.get_object_type("BusinessParty")
    seller_rt.put_object(seller_ent_cls(entity_id="ENT-SELLER", name="宝山特钢销售主体", base_currency="CNY"), actor=admin)
    seller_rt.put_object(
        seller_party_cls(party_id="PTY-SELLER-BG", legal_name="宝山特钢贸易有限公司", tax_id="91310000999999992Y"),
        actor=admin,
    )
    if seller_urgent_ticket:
        acc_cls = seller_spec.get_object_type("CrmAccount")
        tk_cls = seller_spec.get_object_type("CrmTicket")
        # Buyer BusinessParty will be projected via CommonsenseEnvelope; seed internal seller CRM state linked to buyer party_id
        seller_rt.put_object(
            seller_party_cls(party_id="PTY-BUYER-HD", legal_name="华东建工集团有限公司", tax_id="91310000888888881X"),
            actor=admin,
        )
        seller_rt.put_object(
            acc_cls(account_id="ACC-HD-01", name="华东建工", party="PTY-BUYER-HD", industry="Construction"),
            actor=admin,
        )
        seller_rt.put_object(
            tk_cls(
                ticket_id="TK-ST-URGENT-01",
                subject="上批次钢材理化质保书未归档",
                account="ACC-HD-01",
                priority="URGENT",
                status="OPEN",
            ),
            actor=admin,
        )

    p2p_port = P2pCommonsensePort(
        buyer_rt,
        buyer_actor=buyer_actor,
        warehouse_actor=buyer_wh,
        procurement_manager_actor=buyer_pm,
        finance_actor=buyer_fin,
        sync_actor=admin,
    )
    o2c_port = O2cCommonsensePort(
        seller_rt,
        sales_actor=seller_sales,
        warehouse_actor=seller_wh,
        finance_actor=seller_fin,
        accounting_entity_id="ENT-SELLER",
        seller_party_id="PTY-SELLER-BG",
        default_credit_limit=1_000_000.0,
        sync_actor=admin,
    )
    return buyer_rt, p2p_port, seller_rt, o2c_port, {
        "admin": admin,
        "buyer": buyer_actor,
        "buyer_wh": buyer_wh,
        "buyer_pm": buyer_pm,
        "buyer_fin": buyer_fin,
        "seller_sales": seller_sales,
        "seller_wh": seller_wh,
        "seller_fin": seller_fin,
        "seller_sup": seller_sup,
    }


def test_steel_b2b_scenario1_normal_100t_rebar_flow() -> None:
    buyer_rt, p2p_port, seller_rt, o2c_port, actors = _make_steel_buyer_seller_worlds()
    try:
        buyer_rt.submit_action("B-CONF", "confirm_p2p_order", {"po_id": "PO-ST-100"}, actors["buyer"])
        env_prop = p2p_port.emit_purchase_commitment(
            po_id="PO-ST-100",
            resource_id="RES-HRB400E-16",
            commitment_id="CM-ST-100",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
        )
        env_agreed = CommonsenseEnvelope.from_json(
            o2c_port.ingest_buyer_commitment(
                env_prop.to_json(),
                order_id="ORD-ST-100",
                delivery_id="DEL-ST-100",
            ).to_json()
        )
        assert env_agreed.message_kind == "COMMITMENT_AGREED"
        p2p_port.ingest_seller_envelope(env_agreed.to_json())

        env_ship = CommonsenseEnvelope.from_json(
            o2c_port.emit_shipment_event(
                delivery_id="DEL-ST-100",
                order_id="ORD-ST-100",
                commitment_id="CM-ST-100",
                event_id="EV-SHIP-100",
                shipped_qty=100.0,
            ).to_json()
        )
        p2p_port.ingest_seller_envelope(env_ship.to_json())

        rec_cls = buyer_rt.spec.get_object_type("P2pGoodsReceipt")
        buyer_rt.put_object(
            rec_cls(receipt_id="REC-ST-100", purchase_order="PO-ST-100", expected_qty=100.0),
            actor=actors["admin"],
        )
        buyer_rt.submit_action(
            "B-REC",
            "record_p2p_receipt",
            {"receipt_id": "REC-ST-100", "received_qty": 100.0, "inspection_status": "passed"},
            actors["buyer_wh"],
        )
        env_rec = p2p_port.emit_receipt_and_dispute_envelope(
            receipt_id="REC-ST-100",
            commitment_id="CM-ST-100",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
            event_id="EV-REC-100",
        )
        assert env_rec.message_kind == "RECEIPT_EVENT"

        env_inv = o2c_port.emit_invoice_event(
            invoice_id="INV-ST-100",
            order_id="ORD-ST-100",
            commitment_id="CM-ST-100",
            event_id="EV-INV-100",
            qty=100.0,
            amount=360_000.0,
        )
        p2p_port.ingest_seller_envelope(env_inv.to_json())

        bill_cls = buyer_rt.spec.get_object_type("P2pVendorBill")
        buyer_rt.put_object(
            bill_cls(
                bill_id="BILL-ST-100",
                purchase_order="PO-ST-100",
                amount_total=360_000.0,
                amount_residual=360_000.0,
            ),
            actor=actors["admin"],
        )
        buyer_rt.submit_action("B-POST", "post_p2p_bill", {"bill_id": "BILL-ST-100"}, actors["buyer_fin"])
        buyer_rt.submit_action(
            "B-PAY",
            "record_p2p_payment",
            {"bill_id": "BILL-ST-100", "payment_amount": 360_000.0},
            actors["buyer_fin"],
        )

        env_pay = p2p_port.emit_payment_event(
            bill_id="BILL-ST-100",
            commitment_id="CM-ST-100",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
            event_id="EV-PAY-100",
        )
        o2c_pay = o2c_port.ingest_payment_event(env_pay.to_json(), invoice_id="INV-ST-100")

        assert buyer_rt.get_object("P2pVendorBill", "BILL-ST-100", actors["buyer_fin"]).payment_state == "paid"
        assert o2c_pay["payment_state"] == "paid"
        assert o2c_pay["amount_residual"] == pytest.approx(0.0)
        assert seller_rt.get_object("TradeCommitment", "CM-ST-100", actors["admin"]).status == "fulfilled"
        assert buyer_rt.get_object("TradeCommitment", "CM-ST-100", actors["admin"]).status == "fulfilled"
    finally:
        buyer_rt.close()
        seller_rt.close()


def test_steel_b2b_scenario2_short_weight_96t_dispute_and_work_card_settlement() -> None:
    buyer_rt, p2p_port, seller_rt, o2c_port, actors = _make_steel_buyer_seller_worlds()
    try:
        buyer_rt.submit_action("B-CONF", "confirm_p2p_order", {"po_id": "PO-ST-100"}, actors["buyer"])
        env_prop = p2p_port.emit_purchase_commitment(
            po_id="PO-ST-100",
            resource_id="RES-HRB400E-16",
            commitment_id="CM-ST-DIS",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
        )
        env_agreed = o2c_port.ingest_buyer_commitment(
            env_prop.to_json(), order_id="ORD-ST-DIS", delivery_id="DEL-ST-DIS"
        )
        p2p_port.ingest_seller_envelope(env_agreed.to_json())
        o2c_port.emit_shipment_event(
            delivery_id="DEL-ST-DIS",
            order_id="ORD-ST-DIS",
            commitment_id="CM-ST-DIS",
            event_id="EV-SHIP-DIS",
            shipped_qty=100.0,
        )

        # Buyer weighbridge records 96t instead of 100t -> receipt & PO enter disputed
        rec_cls = buyer_rt.spec.get_object_type("P2pGoodsReceipt")
        bill_cls = buyer_rt.spec.get_object_type("P2pVendorBill")
        buyer_rt.put_object(
            rec_cls(receipt_id="REC-ST-DIS", purchase_order="PO-ST-100", expected_qty=100.0),
            actor=actors["admin"],
        )
        buyer_rt.put_object(
            bill_cls(
                bill_id="BILL-ST-DIS",
                purchase_order="PO-ST-100",
                amount_total=360_000.0,
                amount_residual=360_000.0,
            ),
            actor=actors["admin"],
        )
        buyer_rt.submit_action(
            "B-REC-96",
            "record_p2p_receipt",
            {"receipt_id": "REC-ST-DIS", "received_qty": 96.0, "inspection_status": "passed"},
            actors["buyer_wh"],
        )
        assert buyer_rt.get_object("P2pPurchaseOrder", "PO-ST-100", actors["buyer"]).status == "disputed"

        # Buyer M1 invariant blocks full 360,000 bill posting with zero dirty ABox writes (audit run recorded)
        fp_before_block = buyer_rt.state_fingerprint()
        with pytest.raises(InvariantError) as exc_blk:
            buyer_rt.submit_action("B-POST-FAIL", "post_p2p_bill", {"bill_id": "BILL-ST-DIS"}, actors["buyer_fin"])
        assert exc_blk.value.code == "PO_DISPUTED_BLOCKED"
        fp_after_block = buyer_rt.state_fingerprint()
        assert fp_after_block["objects"] == fp_before_block["objects"]
        assert fp_after_block["versions"] == fp_before_block["versions"]
        assert buyer_rt.get_object("P2pVendorBill", "BILL-ST-DIS", actors["buyer_fin"]).status == "draft"

        # Buyer emits commonsense FulfillmentEvent(RECEIPT, disputed) + WorkCard (with P2p target redacted on wire)
        env_dispute = p2p_port.emit_receipt_and_dispute_envelope(
            receipt_id="REC-ST-DIS",
            commitment_id="CM-ST-DIS",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
            event_id="EV-REC-DIS",
            work_card_id="WC-P2P-DIS-1",
        )
        assert env_dispute.message_kind == "DISPUTE_RAISED"
        assert env_dispute.events[0]["status"] == "disputed"
        assert env_dispute.work_cards[0]["target_object_type"] == "TradeCommitment"

        # Seller accepts 4t deduction (96t * 3600 = 345,600) and issues adjusted invoice
        env_settled = o2c_port.settle_dispute_and_issue_invoice(
            env_dispute.to_json(),
            order_id="ORD-ST-DIS",
            invoice_id="INV-ST-DIS",
            event_id="EV-INV-DIS",
            consensus_qty=96.0,
            unit_price=3600.0,
        )
        assert env_settled.message_kind == "DISPUTE_SETTLED"
        assert env_settled.commitments[0]["total_amount"] == pytest.approx(345_600.0)

        # Buyer procurement_manager settles WorkCard (SoD: procurement_manager != buyer) -> PO becomes 345,600 received
        p2p_port.apply_settled_dispute_from_seller(
            env_settled.to_json(),
            po_id="PO-ST-100",
            receipt_id="REC-ST-DIS",
            local_work_card_id="WC-P2P-DIS-1",
        )
        po_resolved = buyer_rt.get_object("P2pPurchaseOrder", "PO-ST-100", actors["buyer_pm"])
        assert po_resolved.status == "received"
        assert po_resolved.amount_total == pytest.approx(345_600.0)

        # Even after dispute resolution, posting the stale 360,000 bill is still blocked until reconciled!
        with pytest.raises(InvariantError) as exc_amt:
            buyer_rt.submit_action("B-POST-OVER", "post_p2p_bill", {"bill_id": "BILL-ST-DIS"}, actors["buyer_fin"])
        assert exc_amt.value.code == "BILL_EXCEEDS_PO_AMOUNT"

        buyer_rt.submit_action("B-RECON", "reconcile_p2p_bill", {"bill_id": "BILL-ST-DIS"}, actors["buyer_fin"])
        buyer_rt.submit_action("B-POST-OK", "post_p2p_bill", {"bill_id": "BILL-ST-DIS"}, actors["buyer_fin"])
        buyer_rt.submit_action(
            "B-PAY-OK",
            "record_p2p_payment",
            {"bill_id": "BILL-ST-DIS", "payment_amount": 345_600.0},
            actors["buyer_fin"],
        )
        env_pay = p2p_port.emit_payment_event(
            bill_id="BILL-ST-DIS",
            commitment_id="CM-ST-DIS",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
            event_id="EV-PAY-DIS",
        )
        o2c_pay = o2c_port.ingest_payment_event(env_pay.to_json(), invoice_id="INV-ST-DIS")

        assert buyer_rt.get_object("P2pVendorBill", "BILL-ST-DIS", actors["buyer_fin"]).amount_residual == pytest.approx(0.0)
        assert o2c_pay["payment_state"] == "paid"
        assert seller_rt.get_object("O2cInvoice", "INV-ST-DIS", actors["seller_fin"]).amount_total == pytest.approx(345_600.0)
    finally:
        buyer_rt.close()
        seller_rt.close()


def test_steel_b2b_scenario3_seller_crm_urgent_ticket_hold_zero_leak_to_buyer() -> None:
    buyer_rt, p2p_port, seller_rt, o2c_port, actors = _make_steel_buyer_seller_worlds(
        seller_urgent_ticket=True
    )
    try:
        assert "CrmTicket" not in buyer_rt.spec.object_types
        buyer_rt.submit_action("B-CONF-3", "confirm_p2p_order", {"po_id": "PO-ST-100"}, actors["buyer"])
        env_prop = p2p_port.emit_purchase_commitment(
            po_id="PO-ST-100",
            resource_id="RES-HRB400E-16",
            commitment_id="CM-ST-HOLD",
            buyer_party_id="PTY-BUYER-HD",
            seller_party_id="PTY-SELLER-BG",
        )

        # Seller CustomerOverlay blocks confirm_o2c_order due to internal CrmTicket, emits commonsense hold envelope
        env_held = o2c_port.ingest_buyer_commitment(
            env_prop.to_json(),
            order_id="ORD-ST-HOLD",
            delivery_id="DEL-ST-HOLD",
        )
        raw_wire = env_held.to_json()
        assert "CrmTicket" not in raw_wire
        assert env_held.message_kind == "COMMITMENT_HELD"
        assert env_held.commitments[0]["status"] == "disputed"
        assert env_held.work_cards[0]["target_object_type"] == "TradeCommitment"

        p2p_port.ingest_seller_envelope(raw_wire)
        assert buyer_rt.get_object("TradeCommitment", "CM-ST-HOLD", actors["buyer"]).status == "disputed"
        assert buyer_rt.get_object("WorkCard", "WC-HOLD-CM-ST-HOLD", actors["buyer"]).status == "OPEN"

        # Seller support resolves internal urgent ticket -> seller releases commitment hold cleanly
        seller_rt.submit_action(
            "S-RES-TK",
            "resolve_crm_ticket",
            {"ticket_id": "TK-ST-URGENT-01", "resolution_summary": "质保书已补发并归档"},
            actors["seller_sup"],
        )
        env_agreed = o2c_port.retry_held_commitment(
            commitment_id="CM-ST-HOLD",
            order_id="ORD-ST-HOLD",
            delivery_id="DEL-ST-HOLD",
        )
        assert env_agreed.message_kind == "COMMITMENT_AGREED"
        assert seller_rt.get_object("O2cOrder", "ORD-ST-HOLD", actors["seller_sales"]).status == "confirmed"

        p2p_port.ingest_seller_envelope(env_agreed.to_json())
        assert buyer_rt.get_object("TradeCommitment", "CM-ST-HOLD", actors["buyer"]).status == "agreed"
    finally:
        buyer_rt.close()
        seller_rt.close()
