"""Offline deterministic blind harness & falsification tests for WO-17 std_p2p thin drivers.

Runs completely offline (no Docker, no external LLM) to verify:
1. Pure M2 AST anti-cheat enforcement (`audit_thin_driver_ast`: <= 350 lines,
   no hardcoded Public Tenant A literals, no subprocess/eval/exec/file writes).
2. A genuine REST `std_p2p` thin driver passing independent dual-world
   `verify_thin_driver_on_hidden_world` against both Public Tenant A and a
   randomized ID/amount-shifted Hidden Tenant B (`CORROBORATED`, 100% target
   action coverage).
3. Out-of-band (OOB) source state tampering triggers `DEVICE_STATE_MISMATCH`
   with `REJECTED` audit trail and zero ABox corruption.
4. Short-weight receipt sets PO + receipt to `disputed` and blocks bill posting.
"""

from __future__ import annotations

import copy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from open_ontology.design import audit_thin_driver_ast, verify_thin_driver_on_hidden_world
from open_ontology.induction import (
    ActionExecutionBinding,
    AdapterMappingSpec,
    EntityMappingSpec,
    FieldTransformSpec,
)

STD_P2P_BP_ARG = "examples.std_p2p_ontology:build_std_p2p_blueprint"
BUSINESS_SEED_ARG = "examples.business_ontology:BUSINESS_SEED_ONTOLOGY"

P2P_OWNED_TYPES = [
    "AccountingEntity",
    "BusinessParty",
    "P2pSupplierProfile",
    "P2pPurchaseOrder",
    "P2pGoodsReceipt",
    "P2pVendorBill",
]
P2P_HANDLED_ACTIONS = [
    "confirm_p2p_order",
    "record_p2p_receipt",
    "post_p2p_bill",
    "record_p2p_payment",
]


def _make_p2p_mapping() -> AdapterMappingSpec:
    return AdapterMappingSpec(
        mapping_id="mock_rest_p2p",
        source_kind="rest_erp",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="companies",
                target_object_type="AccountingEntity",
                id_transform=FieldTransformSpec(
                    target_field="entity_id", source_field="id", kind="id_prefix", prefix="ENT-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct"),
                    FieldTransformSpec(target_field="base_currency", source_field="currency", kind="direct"),
                ],
            ),
            EntityMappingSpec(
                source_table="suppliers",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id", source_field="id", kind="id_prefix", prefix="PTY-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct"),
                    FieldTransformSpec(target_field="tax_id", source_field="tax_no", kind="direct"),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct"),
                ],
            ),
            EntityMappingSpec(
                source_table="suppliers",
                target_object_type="P2pSupplierProfile",
                id_transform=FieldTransformSpec(
                    target_field="profile_id", source_field="id", kind="id_prefix", prefix="SUP-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="party", source_field="id", kind="foreign_key_id", prefix="PTY-"),
                    FieldTransformSpec(
                        target_field="accounting_entity", source_field="company_id", kind="foreign_key_id", prefix="ENT-"
                    ),
                    FieldTransformSpec(target_field="payment_terms", source_field="payment_terms", kind="direct"),
                ],
            ),
            EntityMappingSpec(
                source_table="purchase_orders",
                target_object_type="P2pPurchaseOrder",
                id_transform=FieldTransformSpec(
                    target_field="po_id", source_field="id", kind="id_prefix", prefix="PO-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="po_number", source_field="number", kind="direct"),
                    FieldTransformSpec(
                        target_field="supplier", source_field="supplier_id", kind="foreign_key_id", prefix="SUP-"
                    ),
                    FieldTransformSpec(
                        target_field="accounting_entity", source_field="company_id", kind="foreign_key_id", prefix="ENT-"
                    ),
                    FieldTransformSpec(target_field="ordered_qty", source_field="ordered_qty", kind="direct"),
                    FieldTransformSpec(target_field="unit_price", source_field="unit_price", kind="direct"),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct"),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={
                            "draft": "draft",
                            "confirmed": "confirmed",
                            "received": "received",
                            "billed": "billed",
                            "disputed": "disputed",
                            "cancelled": "cancelled",
                        },
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="goods_receipts",
                target_object_type="P2pGoodsReceipt",
                id_transform=FieldTransformSpec(
                    target_field="receipt_id", source_field="id", kind="id_prefix", prefix="REC-"
                ),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="purchase_order", source_field="po_id", kind="foreign_key_id", prefix="PO-"
                    ),
                    FieldTransformSpec(target_field="expected_qty", source_field="expected_qty", kind="direct"),
                    FieldTransformSpec(target_field="received_qty", source_field="received_qty", kind="direct"),
                    FieldTransformSpec(
                        target_field="inspection_status", source_field="inspection_status", kind="direct"
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={
                            "pending": "pending",
                            "completed": "completed",
                            "disputed": "disputed",
                            "cancelled": "cancelled",
                        },
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="vendor_bills",
                target_object_type="P2pVendorBill",
                id_transform=FieldTransformSpec(
                    target_field="bill_id", source_field="id", kind="id_prefix", prefix="BILL-"
                ),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="purchase_order", source_field="po_id", kind="foreign_key_id", prefix="PO-"
                    ),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct"),
                    FieldTransformSpec(target_field="amount_residual", source_field="amount_residual", kind="direct"),
                    FieldTransformSpec(target_field="payment_state", source_field="payment_state", kind="direct"),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={
                            "draft": "draft",
                            "posted": "posted",
                            "cancelled": "cancelled",
                        },
                    ),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="confirm_p2p_order",
                external_table="purchase_orders",
                external_method="confirm_po",
                target_object_type="P2pPurchaseOrder",
                id_param="po_id",
                id_prefix="PO-",
                expected_state_field="status",
                expected_state_after="confirmed",
            ),
            ActionExecutionBinding(
                action_name="record_p2p_receipt",
                external_table="goods_receipts",
                external_method="record_receipt",
                target_object_type="P2pGoodsReceipt",
                id_param="receipt_id",
                id_prefix="REC-",
                expected_state_field="status",
                expected_state_after="completed",
            ),
            ActionExecutionBinding(
                action_name="post_p2p_bill",
                external_table="vendor_bills",
                external_method="post_bill",
                target_object_type="P2pVendorBill",
                id_param="bill_id",
                id_prefix="BILL-",
                expected_state_field="status",
                expected_state_after="posted",
            ),
            ActionExecutionBinding(
                action_name="record_p2p_payment",
                external_table="vendor_bills",
                external_method="pay_bill",
                target_object_type="P2pVendorBill",
                id_param="bill_id",
                id_prefix="BILL-",
                expected_state_field="payment_state",
                expected_state_after="paid",
            ),
        ],
    )


class _MultiTenantMockP2pREST:
    """In-memory multi-tenant REST P2P server supporting reset & OOB tampering."""

    def __init__(self, initial_tenants: dict[str, dict[str, Any]]) -> None:
        self._lock = threading.RLock()
        self._baselines = copy.deepcopy(initial_tenants)
        self._tenants = copy.deepcopy(initial_tenants)
        self._tokens: dict[str, str] = {}

    def login(self, tenant: str, username: str, password: str) -> str | None:
        with self._lock:
            t = self._tenants.get(tenant)
            if not t or t["username"] != username or t["password"] != password:
                return None
            tok = f"tok-{tenant}"
            self._tokens[tok] = tenant
            return tok

    def reset_tenant(self, tenant: str) -> None:
        with self._lock:
            self._tenants[tenant] = copy.deepcopy(self._baselines[tenant])

    def _data_for_token(self, token: str) -> dict[str, Any]:
        tenant = self._tokens.get(token)
        if not tenant:
            raise PermissionError("invalid token")
        return self._tenants[tenant]

    def oob_cancel_po(self, tenant: str, po_id: int) -> None:
        with self._lock:
            for row in self._tenants[tenant]["data"]["purchase_orders"]:
                if int(row["id"]) == int(po_id):
                    row["state"] = "cancelled"
                    return
            raise KeyError(po_id)

    def handle_post(self, path: str, token: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if path == "/api/login":
                tok = self.login(str(payload.get("tenant") or ""), str(payload.get("username") or ""), str(payload.get("password") or ""))
                if not tok:
                    return 401, {"ok": False, "error": "unauthorized"}
                return 200, {"ok": True, "token": tok}

            if path == "/api/reset":
                tenant = str(payload.get("tenant") or "")
                self.reset_tenant(tenant)
                return 200, {"ok": True}

            try:
                data = self._data_for_token(token)["data"]
            except PermissionError as exc:
                return 401, {"ok": False, "error": str(exc)}

            if path == "/api/orders/confirm":
                po_id = int(payload["id"])
                for row in data["purchase_orders"]:
                    if int(row["id"]) == po_id:
                        if row["state"] != "draft":
                            return 409, {"ok": False, "conflict": True, "error": f"po {po_id} state={row['state']}"}
                        row["state"] = "confirmed"
                        return 200, {"ok": True}
                return 404, {"ok": False, "error": "po missing"}

            if path == "/api/receipts/record":
                rec_id = int(payload["id"])
                rqty = float(payload["received_qty"])
                insp = str(payload.get("inspection_status") or "passed")
                for r in data["goods_receipts"]:
                    if int(r["id"]) == rec_id:
                        if r["state"] != "pending":
                            return 409, {"ok": False, "conflict": True, "error": f"receipt {rec_id} state={r['state']}"}
                        r["received_qty"] = rqty
                        r["inspection_status"] = insp
                        disputed = rqty < float(r["expected_qty"]) or insp == "failed"
                        r["state"] = "disputed" if disputed else "completed"
                        for po in data["purchase_orders"]:
                            if int(po["id"]) == int(r["po_id"]):
                                po["state"] = "disputed" if disputed else "received"
                        return 200, {"ok": True, "state": r["state"]}
                return 404, {"ok": False, "error": "receipt missing"}

            if path == "/api/bills/post":
                bid = int(payload["id"])
                for b in data["vendor_bills"]:
                    if int(b["id"]) == bid:
                        if b["state"] != "draft":
                            return 409, {"ok": False, "conflict": True, "error": f"bill {bid} state={b['state']}"}
                        for po in data["purchase_orders"]:
                            if int(po["id"]) == int(b["po_id"]):
                                if po["state"] == "disputed":
                                    return 409, {"ok": False, "conflict": True, "error": "po is disputed"}
                                po["state"] = "billed"
                        b["state"] = "posted"
                        return 200, {"ok": True}
                return 404, {"ok": False, "error": "bill missing"}

            if path == "/api/bills/pay":
                bid = int(payload["id"])
                amt = float(payload["amount"])
                for b in data["vendor_bills"]:
                    if int(b["id"]) == bid:
                        if b["state"] != "posted":
                            return 409, {"ok": False, "conflict": True, "error": f"bill {bid} state={b['state']}"}
                        rem = max(0.0, float(b["amount_residual"]) - amt)
                        b["amount_residual"] = rem
                        b["payment_state"] = "paid" if rem <= 1e-9 else "partial"
                        return 200, {"ok": True, "payment_state": b["payment_state"]}
                return 404, {"ok": False, "error": "bill missing"}

            return 404, {"ok": False, "error": f"unknown POST {path}"}

    def handle_get(self, path: str, token: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            try:
                data = self._data_for_token(token)["data"]
            except PermissionError as exc:
                return 401, {"ok": False, "error": str(exc)}
            if path == "/api/snapshot":
                return 200, {"ok": True, "tables": copy.deepcopy(data)}
            return 404, {"ok": False, "error": f"unknown GET {path}"}


def _start_mock_rest_server(erp: _MultiTenantMockP2pREST) -> tuple[ThreadingHTTPServer, str]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            tok = self.headers.get("X-Access-Token") or ""
            st, body = erp.handle_get(self.path.split("?", 1)[0], tok)
            self._reply(st, body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(max(0, length))
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            tok = self.headers.get("X-Access-Token") or ""
            st, body = erp.handle_post(self.path.split("?", 1)[0], tok, payload)
            self._reply(st, body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _build_two_worlds() -> dict[str, dict[str, Any]]:
    return {
        "pub_tenant_a": {
            "username": "pub_buyer",
            "password": "pub_pw_alpha",
            "data": {
                "companies": [{"id": 1, "name": "Alpha Foundry", "currency": "CNY"}],
                "suppliers": [
                    {
                        "id": 11,
                        "name": "Public Mill Co",
                        "tax_no": "9131PUB001",
                        "country": "CN",
                        "company_id": 1,
                        "payment_terms": "NET30",
                    }
                ],
                "purchase_orders": [
                    {"id": 101, "number": "PO-A-01", "supplier_id": 11, "company_id": 1, "ordered_qty": 10.0, "unit_price": 3600.0, "amount_total": 36000.0, "state": "draft"},
                    {"id": 102, "number": "PO-A-02", "supplier_id": 11, "company_id": 1, "ordered_qty": 15.0, "unit_price": 3600.0, "amount_total": 54000.0, "state": "confirmed"},
                    {"id": 103, "number": "PO-A-03", "supplier_id": 11, "company_id": 1, "ordered_qty": 20.0, "unit_price": 3600.0, "amount_total": 72000.0, "state": "received"},
                    {"id": 104, "number": "PO-A-04", "supplier_id": 11, "company_id": 1, "ordered_qty": 25.0, "unit_price": 3600.0, "amount_total": 90000.0, "state": "billed"},
                ],
                "goods_receipts": [
                    {"id": 202, "po_id": 102, "expected_qty": 15.0, "received_qty": 0.0, "inspection_status": "pending", "state": "pending"},
                    {"id": 203, "po_id": 103, "expected_qty": 20.0, "received_qty": 20.0, "inspection_status": "passed", "state": "completed"},
                    {"id": 204, "po_id": 104, "expected_qty": 25.0, "received_qty": 25.0, "inspection_status": "passed", "state": "completed"},
                ],
                "vendor_bills": [
                    {"id": 303, "po_id": 103, "amount_total": 72000.0, "amount_residual": 72000.0, "payment_state": "not_paid", "state": "draft"},
                    {"id": 304, "po_id": 104, "amount_total": 90000.0, "amount_residual": 90000.0, "payment_state": "not_paid", "state": "posted"},
                ],
            },
        },
        "hid_tenant_b": {
            "username": "hid_buyer_99",
            "password": "hid_pw_zeta_91",
            "data": {
                "companies": [{"id": 701, "name": "Omega Heavy Steel", "currency": "CNY"}],
                "suppliers": [
                    {
                        "id": 711,
                        "name": "Hidden Anshan Vendor",
                        "tax_no": "9121HID777",
                        "country": "CN",
                        "company_id": 701,
                        "payment_terms": "NET45",
                    }
                ],
                "purchase_orders": [
                    {"id": 7101, "number": "PO-HID-901", "supplier_id": 711, "company_id": 701, "ordered_qty": 12.0, "unit_price": 4100.0, "amount_total": 49200.0, "state": "draft"},
                    {"id": 7102, "number": "PO-HID-902", "supplier_id": 711, "company_id": 701, "ordered_qty": 18.0, "unit_price": 4100.0, "amount_total": 73800.0, "state": "confirmed"},
                    {"id": 7103, "number": "PO-HID-903", "supplier_id": 711, "company_id": 701, "ordered_qty": 22.0, "unit_price": 4100.0, "amount_total": 90200.0, "state": "received"},
                    {"id": 7104, "number": "PO-HID-904", "supplier_id": 711, "company_id": 701, "ordered_qty": 30.0, "unit_price": 4100.0, "amount_total": 123000.0, "state": "billed"},
                ],
                "goods_receipts": [
                    {"id": 7202, "po_id": 7102, "expected_qty": 18.0, "received_qty": 0.0, "inspection_status": "pending", "state": "pending"},
                    {"id": 7203, "po_id": 7103, "expected_qty": 22.0, "received_qty": 22.0, "inspection_status": "passed", "state": "completed"},
                    {"id": 7204, "po_id": 7104, "expected_qty": 30.0, "received_qty": 30.0, "inspection_status": "passed", "state": "completed"},
                ],
                "vendor_bills": [
                    {"id": 7303, "po_id": 7103, "amount_total": 90200.0, "amount_residual": 90200.0, "payment_state": "not_paid", "state": "draft"},
                    {"id": 7304, "po_id": 7104, "amount_total": 123000.0, "amount_residual": 123000.0, "payment_state": "not_paid", "state": "posted"},
                ],
            },
        },
    }


_THIN_REST_P2P_DRIVER_SRC = '''"""Ultra-thin REST P2P driver built strictly from endpoint_config & mapping."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from open_ontology import SourceBridge


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: Any) -> SourceBridge:
    base_url = str(endpoint_config["url"]).rstrip("/")
    tenant = str(endpoint_config["tenant"])
    username = str(endpoint_config["username"])
    password = str(endpoint_config["password"])
    prefix_by_type = {em.target_object_type: em.id_transform.prefix for em in mapping.entity_mappings}

    def _req(method: str, path: str, body: dict[str, Any] | None = None, token: str = "") -> dict[str, Any]:
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["X-Access-Token"] = token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base_url + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_txt = exc.read().decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(err_txt)
            except Exception:
                parsed = {"ok": False, "error": err_txt}
            if parsed.get("conflict"):
                return parsed
            raise

    def _token() -> str:
        res = _req("POST", "/api/login", {"tenant": tenant, "username": username, "password": password})
        tok = res.get("token")
        if not tok:
            raise RuntimeError("ERP authentication failed")
        return str(tok)

    def _raw_id(tname: str, oid: str) -> int:
        pfx = prefix_by_type.get(tname, "")
        return int(oid[len(pfx) :] if pfx and oid.startswith(pfx) else oid)

    def _observe(_move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        tok = _token()
        tables = _req("GET", "/api/snapshot", None, tok)["tables"]
        p_ent = prefix_by_type.get("AccountingEntity", "ENT-")
        p_pty = prefix_by_type.get("BusinessParty", "PTY-")
        p_sup = prefix_by_type.get("P2pSupplierProfile", "SUP-")
        p_po = prefix_by_type.get("P2pPurchaseOrder", "PO-")
        p_rec = prefix_by_type.get("P2pGoodsReceipt", "REC-")
        p_bill = prefix_by_type.get("P2pVendorBill", "BILL-")
        return {
            "AccountingEntity": [
                {"entity_id": f"{p_ent}{c['id']}", "name": c["name"], "base_currency": c["currency"], "status": "active"}
                for c in tables["companies"]
            ],
            "BusinessParty": [
                {"party_id": f"{p_pty}{s['id']}", "legal_name": s["name"], "tax_id": s["tax_no"], "country_code": s["country"], "status": "active"}
                for s in tables["suppliers"]
            ],
            "P2pSupplierProfile": [
                {"profile_id": f"{p_sup}{s['id']}", "party": f"{p_pty}{s['id']}", "accounting_entity": f"{p_ent}{s['company_id']}", "payment_terms": s["payment_terms"], "status": "active"}
                for s in tables["suppliers"]
            ],
            "P2pPurchaseOrder": [
                {"po_id": f"{p_po}{o['id']}", "po_number": o["number"], "supplier": f"{p_sup}{o['supplier_id']}", "accounting_entity": f"{p_ent}{o['company_id']}", "ordered_qty": float(o["ordered_qty"]), "unit_price": float(o["unit_price"]), "amount_total": float(o["amount_total"]), "status": o["state"]}
                for o in tables["purchase_orders"]
            ],
            "P2pGoodsReceipt": [
                {"receipt_id": f"{p_rec}{r['id']}", "purchase_order": f"{p_po}{r['po_id']}", "expected_qty": float(r["expected_qty"]), "received_qty": float(r["received_qty"]), "inspection_status": r["inspection_status"], "status": r["state"]}
                for r in tables["goods_receipts"]
            ],
            "P2pVendorBill": [
                {"bill_id": f"{p_bill}{b['id']}", "purchase_order": f"{p_po}{b['po_id']}", "amount_total": float(b["amount_total"]), "amount_residual": float(b["amount_residual"]), "payment_state": b["payment_state"], "status": b["state"]}
                for b in tables["vendor_bills"]
            ],
        }

    def _actuate(move: dict[str, Any]) -> None:
        tok = _token()
        act = str(move.get("action_type") or "")
        params = dict(move.get("params") or {})
        if act == "confirm_p2p_order":
            _req("POST", "/api/orders/confirm", {"id": _raw_id("P2pPurchaseOrder", str(params["po_id"]))}, tok)
        elif act == "record_p2p_receipt":
            _req("POST", "/api/receipts/record", {"id": _raw_id("P2pGoodsReceipt", str(params["receipt_id"])), "received_qty": float(params["received_qty"]), "inspection_status": str(params.get("inspection_status", "passed"))}, tok)
        elif act == "post_p2p_bill":
            _req("POST", "/api/bills/post", {"id": _raw_id("P2pVendorBill", str(params["bill_id"]))}, tok)
        elif act == "record_p2p_payment":
            _req("POST", "/api/bills/pay", {"id": _raw_id("P2pVendorBill", str(params["bill_id"])), "amount": float(params["payment_amount"])}, tok)

    return SourceBridge(actuate=_actuate, observe=_observe, allow_source_revert=False)
'''


def test_jsherp_p2p_ast_cage_rejects_cheating(tmp_path: Path) -> None:
    cheating_driver = tmp_path / "cheating_driver.py"
    cheating_driver.write_text(
        _THIN_REST_P2P_DRIVER_SRC + '\n_HARDCODED_LEAK = "Alpha Foundry"\n',
        encoding="utf-8",
    )
    errs = audit_thin_driver_ast(
        cheating_driver,
        forbidden_literals=["Alpha Foundry", "PO-A-01"],
        max_lines=350,
    )
    assert any("Alpha Foundry" in e for e in errs)

    fat_driver = tmp_path / "fat_driver.py"
    fat_driver.write_text(_THIN_REST_P2P_DRIVER_SRC + ("\n# pad" * 300), encoding="utf-8")
    fat_errs = audit_thin_driver_ast(fat_driver, forbidden_literals=[], max_lines=350)
    assert any("350" in e for e in fat_errs)


def test_jsherp_p2p_dual_world_blind_verify_and_oob_tamper(tmp_path: Path) -> None:
    erp = _MultiTenantMockP2pREST(_build_two_worlds())
    srv, url = _start_mock_rest_server(erp)
    try:
        driver_path = tmp_path / "driver.py"
        mapping_path = tmp_path / "p2p_mapping.json"
        driver_path.write_text(_THIN_REST_P2P_DRIVER_SRC, encoding="utf-8")
        mapping_path.write_text(
            json.dumps(_make_p2p_mapping().model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )

        # Public Tenant A verification
        res_a = verify_thin_driver_on_hidden_world(
            blueprints=[STD_P2P_BP_ARG],
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config={"url": url, "tenant": "pub_tenant_a", "username": "pub_buyer", "password": "pub_pw_alpha"},
            forbidden_literals=["Omega Heavy Steel", "PO-HID-901"],
            max_driver_lines=350,
            seed=BUSINESS_SEED_ARG,
            device_id="p2p_pub_rest",
            device_kind="rest_erp",
            owned_object_types=P2P_OWNED_TYPES,
            handled_actions=P2P_HANDLED_ACTIONS,
            target_actions=P2P_HANDLED_ACTIONS,
            reset_fn=lambda ep: erp.reset_tenant(str(ep["tenant"])),
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=2,
        )
        assert res_a["ok"] is True, f"World A failed: {res_a}"
        assert res_a["verdict"] == "CORROBORATED"

        # Hidden Tenant B verification with randomized IDs, amounts & OOB tamper red test
        def _oob_tamper_hidden(ep: dict[str, Any], m0: list[dict[str, Any]]) -> dict[str, Any]:
            draft_pos = [
                e for e in m0
                if e.get("object_type") == "P2pPurchaseOrder" and (e.get("data") or {}).get("status") == "draft"
            ]
            assert draft_pos
            po_id = str(draft_pos[0]["object_id"])
            raw_id = int(po_id.split("-", 1)[1])
            erp.oob_cancel_po(str(ep["tenant"]), raw_id)
            return {"action": "confirm_p2p_order", "params": {"po_id": po_id}, "actor_role": "buyer"}

        res_b = verify_thin_driver_on_hidden_world(
            blueprints=[STD_P2P_BP_ARG],
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config={"url": url, "tenant": "hid_tenant_b", "username": "hid_buyer_99", "password": "hid_pw_zeta_91"},
            forbidden_literals=["Alpha Foundry", "Public Mill Co", "PO-A-01", "pub_tenant_a"],
            max_driver_lines=350,
            seed=BUSINESS_SEED_ARG,
            device_id="p2p_hid_rest",
            device_kind="rest_erp",
            owned_object_types=P2P_OWNED_TYPES,
            handled_actions=P2P_HANDLED_ACTIONS,
            target_actions=P2P_HANDLED_ACTIONS,
            reset_fn=lambda ep: erp.reset_tenant(str(ep["tenant"])),
            oob_tamper_check=_oob_tamper_hidden,
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=2,
        )
        assert res_b["ok"] is True, f"Hidden World B failed: {res_b}"
        assert res_b["verdict"] == "CORROBORATED"
        oob = res_b["oob_tamper_result"]
        assert oob["passed"] is True
        assert oob["caught_error_code"] == "DEVICE_STATE_MISMATCH"
        assert oob["abox_unchanged"] is True
        assert oob["audit_status"] == "REJECTED"
    finally:
        srv.shutdown()
        srv.server_close()


def test_jsherp_isolated_workspace_zero_spoilers_and_permissions(tmp_path: Path) -> None:
    from examples.agy_fde_live_jsherp_protocol_benchmark import (
        _FORBIDDEN_SPOILER_TOKENS,
        P2P_HANDLED_ACTIONS,
        _build_zero_spoiler_requirements,
        prepare_isolated_jsherp_workspace,
    )

    req_txt = _build_zero_spoiler_requirements()
    lowered = req_txt.lower()
    assert not [tok for tok in _FORBIDDEN_SPOILER_TOKENS if tok in lowered]

    ws_root = tmp_path / "ws_jsherp"
    prep = prepare_isolated_jsherp_workspace(
        ws_root,
        world_a_endpoint={
            "base_url": "http://127.0.0.1:19999/jshERP-boot",
            "openapi_url": "http://127.0.0.1:19999/jshERP-boot/v2/api-docs",
            "login_url": "http://127.0.0.1:19999/jshERP-boot/user/login",
            "username": "wo17_pub_test",
            "password": "TestPassword!1",
            "password_md5": "00000000000000000000000000000000",
            "token_header": "X-Access-Token",
        },
        reset_url="http://127.0.0.1:1/reset",
    )
    assert set(prep["readonly_hashes"]) == {
        "target_abi.json",
        "world_a_endpoint.json",
        "SKILL.md",
        "requirements.md",
        ".judge/reset_hook.py",
        ".judge/device_p2p.json",
        "verify_driver.sh",
    }
    try:
        assert (ws_root / "requirements.md").stat().st_mode & 0o777 == 0o444
        assert (ws_root / "verify_driver.sh").stat().st_mode & 0o777 == 0o555
        verify_sh = (ws_root / "verify_driver.sh").read_text(encoding="utf-8")
        for action_name in P2P_HANDLED_ACTIONS:
            assert f'--target-action "{action_name}"' in verify_sh
    finally:
        # Restore writable dir perm so pytest tmp_path cleanup succeeds cleanly.
        if (ws_root / ".judge").exists():
            (ws_root / ".judge").chmod(0o755)
            for child in (ws_root / ".judge").iterdir():
                child.chmod(0o644)
        for child in ws_root.iterdir():
            if child.is_file():
                child.chmod(0o644)

