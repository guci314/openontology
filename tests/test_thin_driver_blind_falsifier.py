"""WO-14 offline deterministic CI suite for AST thin-driver cage & blind hidden-world falsification.

Covers (zero live LLM, zero live Odoo required, completes in seconds):
1. All 5 ``audit_thin_driver_ast`` failure modes (forbidden imports, dynamic exec,
   filesystem writes, hardcoded World-A sample literals, exceeding line budget);
2. CLI fail-closed rejection (non-zero exit, structural error codes, offending
   driver never executed);
3. Overfit thin HTTP JSON-RPC driver that passes Public World A ``CORROBORATED``
   but breaks and is ``FALSIFIED`` on randomized Hidden World B;
4. Clean thin HTTP JSON-RPC driver that passes AST audit + CLI ``check`` + CLI
   ``verify`` on Public World A + blind Hidden World B + out-of-band external
   tamper triggering ``DeviceStateMismatchError`` (``DEVICE_STATE_MISMATCH``)
   with zero ABox dirty write;
5. Caching/lying driver whose ``observe()`` echoes expected diffs failing the
   OOB tamper check with ``OOB_TAMPER_UNDETECTED``.
"""

from __future__ import annotations

import copy
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from open_ontology.design import (
    audit_thin_driver_ast,
    main as design_main,
    run_design_check,
    run_design_verify,
    verify_thin_driver_on_hidden_world,
)
from open_ontology.induction import (
    ActionExecutionBinding,
    AdapterMappingSpec,
    EntityMappingSpec,
    FieldTransformSpec,
)

STD_O2C_BP_ARG = "examples.std_o2c_ontology:build_std_o2c_blueprint"
BUSINESS_SEED_ARG = "examples.business_ontology:BUSINESS_SEED_ONTOLOGY"
O2C_OWNED_TYPES = [
    "AccountingEntity",
    "BusinessParty",
    "O2cCustomerProfile",
    "O2cOrder",
    "O2cDelivery",
    "O2cInvoice",
]
O2C_HANDLED_ACTIONS = [
    "confirm_o2c_order",
    "complete_o2c_delivery",
    "post_o2c_invoice",
    "record_o2c_payment",
]


def _make_o2c_mapping() -> AdapterMappingSpec:
    return AdapterMappingSpec(
        mapping_id="mock_jsonrpc_o2c",
        source_kind="jsonrpc_erp",
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
                source_table="partners",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id", source_field="id", kind="id_prefix", prefix="PTY-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct"),
                    FieldTransformSpec(target_field="tax_id", source_field="tax_id", kind="direct"),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct"),
                ],
            ),
            EntityMappingSpec(
                source_table="partners",
                target_object_type="O2cCustomerProfile",
                id_transform=FieldTransformSpec(
                    target_field="profile_id", source_field="id", kind="id_prefix", prefix="CUST-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="party", source_field="id", kind="foreign_key_id", prefix="PTY-"),
                    FieldTransformSpec(
                        target_field="accounting_entity", source_field="company_id", kind="foreign_key_id", prefix="ENT-"
                    ),
                    FieldTransformSpec(target_field="credit_limit", source_field="credit_limit", kind="direct"),
                    FieldTransformSpec(target_field="payment_terms", source_field="payment_terms", kind="direct"),
                ],
            ),
            EntityMappingSpec(
                source_table="orders",
                target_object_type="O2cOrder",
                id_transform=FieldTransformSpec(
                    target_field="order_id", source_field="id", kind="id_prefix", prefix="ORD-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order_number", source_field="number", kind="direct"),
                    FieldTransformSpec(
                        target_field="customer", source_field="partner_id", kind="foreign_key_id", prefix="CUST-"
                    ),
                    FieldTransformSpec(
                        target_field="accounting_entity", source_field="company_id", kind="foreign_key_id", prefix="ENT-"
                    ),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct"),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={
                            "draft": "draft",
                            "sale": "confirmed",
                            "shipped": "delivered",
                            "billed": "invoiced",
                            "cancel": "cancelled",
                        },
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="deliveries",
                target_object_type="O2cDelivery",
                id_transform=FieldTransformSpec(
                    target_field="delivery_id", source_field="id", kind="id_prefix", prefix="DEL-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order", source_field="order_id", kind="foreign_key_id", prefix="ORD-"),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={"waiting": "pending", "done": "completed", "cancel": "cancelled"},
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="invoices",
                target_object_type="O2cInvoice",
                id_transform=FieldTransformSpec(
                    target_field="invoice_id", source_field="id", kind="id_prefix", prefix="INV-"
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order", source_field="order_id", kind="foreign_key_id", prefix="ORD-"),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct"),
                    FieldTransformSpec(target_field="amount_residual", source_field="amount_residual", kind="direct"),
                    FieldTransformSpec(
                        target_field="payment_state",
                        source_field="payment_state",
                        kind="enum_map",
                        enum_map={"not_paid": "not_paid", "partial": "partial", "paid": "paid"},
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={"draft": "draft", "posted": "posted", "cancel": "cancelled"},
                    ),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="confirm_o2c_order",
                external_table="orders",
                external_method="confirm_order",
                target_object_type="O2cOrder",
                id_param="order_id",
                id_prefix="ORD-",
                expected_state_field="status",
                expected_state_after="confirmed",
            ),
            ActionExecutionBinding(
                action_name="complete_o2c_delivery",
                external_table="deliveries",
                external_method="complete_delivery",
                target_object_type="O2cDelivery",
                id_param="delivery_id",
                id_prefix="DEL-",
                expected_state_field="status",
                expected_state_after="completed",
            ),
            ActionExecutionBinding(
                action_name="post_o2c_invoice",
                external_table="invoices",
                external_method="post_invoice",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
                id_prefix="INV-",
                expected_state_field="status",
                expected_state_after="posted",
            ),
            ActionExecutionBinding(
                action_name="record_o2c_payment",
                external_table="invoices",
                external_method="register_payment",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
                id_prefix="INV-",
                expected_state_field="payment_state",
                expected_state_after="paid",
            ),
        ],
    )


class _MultiTenantMockERP:
    """In-memory multi-tenant JSON-RPC ERP supporting reset and OOB mutations."""

    def __init__(self, initial_tenants: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._lock = threading.RLock()
        self._baselines = copy.deepcopy(initial_tenants)
        self._tenants = copy.deepcopy(initial_tenants)

    def reset_tenant(self, db_name: str) -> None:
        with self._lock:
            self._tenants[db_name] = copy.deepcopy(self._baselines[db_name])

    def _tenant(self, db_name: str, token: str) -> dict[str, list[dict[str, Any]]]:
        if token != f"tok-{db_name}":
            raise PermissionError(f"invalid token for {db_name!r}")
        return self._tenants[db_name]

    def handle_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            db_name = str(params["db"])
            if method == "reset":
                self.reset_tenant(db_name)
                return {"ok": True}
            data = self._tenant(db_name, str(params.get("token") or ""))
            if method == "list_records":
                table = str(params["table"])
                return {"records": copy.deepcopy(data[table])}
            if method == "confirm_order":
                oid = int(params["order_id"])
                for row in data["orders"]:
                    if int(row["id"]) == oid:
                        if row["state"] != "draft":
                            return {"ok": False, "conflict": True, "error": f"order {oid} state is {row['state']}"}
                        row["state"] = "sale"
                        next_del_id = max((int(d["id"]) for d in data["deliveries"]), default=100) + 1
                        data["deliveries"].append({"id": next_del_id, "order_id": oid, "state": "waiting"})
                        return {"ok": True, "delivery_id": next_del_id}
                return {"ok": False, "conflict": True, "error": f"order {oid} missing"}
            if method == "complete_delivery":
                did = int(params["delivery_id"])
                for d in data["deliveries"]:
                    if int(d["id"]) == did:
                        if d["state"] != "waiting":
                            return {"ok": False, "conflict": True, "error": f"delivery {did} is {d['state']}"}
                        d["state"] = "done"
                        for o in data["orders"]:
                            if int(o["id"]) == int(d["order_id"]):
                                o["state"] = "shipped"
                        return {"ok": True}
                return {"ok": False, "conflict": True, "error": f"delivery {did} missing"}
            if method == "post_invoice":
                iid = int(params["invoice_id"])
                for inv in data["invoices"]:
                    if int(inv["id"]) == iid:
                        if inv["state"] != "draft":
                            return {"ok": False, "conflict": True, "error": f"invoice {iid} is {inv['state']}"}
                        inv["state"] = "posted"
                        for o in data["orders"]:
                            if int(o["id"]) == int(inv["order_id"]):
                                o["state"] = "billed"
                        return {"ok": True}
                return {"ok": False, "conflict": True, "error": f"invoice {iid} missing"}
            if method == "register_payment":
                iid = int(params["invoice_id"])
                amt = float(params["amount"])
                for inv in data["invoices"]:
                    if int(inv["id"]) == iid:
                        if inv["state"] != "posted" or amt > float(inv["amount_residual"]) + 1e-6:
                            return {"ok": False, "conflict": True, "error": "cannot register payment"}
                        new_res = max(0.0, round(float(inv["amount_residual"]) - amt, 6))
                        inv["amount_residual"] = new_res
                        inv["payment_state"] = "paid" if new_res <= 1e-9 else "partial"
                        return {"ok": True, "amount_residual": new_res}
                return {"ok": False, "conflict": True, "error": f"invoice {iid} missing"}
            if method == "oob_cancel_order":
                oid = int(params["order_id"])
                for o in data["orders"]:
                    if int(o["id"]) == oid:
                        o["state"] = "cancel"
                        return {"ok": True}
                return {"ok": False, "error": f"order {oid} missing"}
            raise ValueError(f"unknown rpc method {method!r}")


def _build_world_seed(*, base_pk: int, order_ref: str, cust_name: str, amount: float, with_noise: bool) -> dict[str, list[dict[str, Any]]]:
    inv1_amt = round(amount * 0.8, 2)
    inv2_amt = round(amount * 0.5, 2)
    deliveries = [
        {"id": base_pk + 31, "order_id": base_pk + 21, "state": "waiting"},
    ]
    invoices = [
        {
            "id": base_pk + 40,
            "order_id": base_pk + 22,
            "amount_total": inv1_amt,
            "amount_residual": inv1_amt,
            "payment_state": "not_paid",
            "state": "draft",
        },
        {
            "id": base_pk + 41,
            "order_id": base_pk + 22,
            "amount_total": inv2_amt,
            "amount_residual": inv2_amt,
            "payment_state": "not_paid",
            "state": "posted",
        },
    ]
    orders = [
        {
            "id": base_pk + 20,
            "number": f"{order_ref}-01",
            "partner_id": base_pk + 10,
            "company_id": base_pk + 1,
            "amount_total": amount,
            "state": "draft",
        },
        {
            "id": base_pk + 21,
            "number": f"{order_ref}-02",
            "partner_id": base_pk + 10,
            "company_id": base_pk + 1,
            "amount_total": round(amount * 1.1, 2),
            "state": "sale",
        },
        {
            "id": base_pk + 22,
            "number": f"{order_ref}-03",
            "partner_id": base_pk + 10,
            "company_id": base_pk + 1,
            "amount_total": inv1_amt,
            "state": "shipped",
        },
    ]
    if with_noise:
        # Simulate real-world orphan stock transfer / standalone invoice without parent O2C order
        deliveries.append({"id": base_pk + 99, "order_id": None, "state": "done"})
        invoices.append(
            {
                "id": base_pk + 98,
                "order_id": False,
                "amount_total": 99.0,
                "amount_residual": 99.0,
                "payment_state": "not_paid",
                "state": "posted",
            }
        )
    return {
        "companies": [{"id": base_pk + 1, "name": f"{cust_name} HQ", "currency": "CNY"}],
        "partners": [
            {
                "id": base_pk + 10,
                "name": cust_name,
                "tax_id": f"TAX-{base_pk}",
                "country": "CN",
                "company_id": base_pk + 1,
                "credit_limit": amount * 10.0,
                "payment_terms": "NET30",
            }
        ],
        "orders": orders,
        "deliveries": deliveries,
        "invoices": invoices,
    }


@pytest.fixture
def mock_erp_server() -> Iterator[tuple[str, _MultiTenantMockERP, dict[str, dict[str, Any]]]]:
    world_a_data = _build_world_seed(
        base_pk=100, order_ref="PUBSO", cust_name="Public Alpha Photonics", amount=1500.0, with_noise=False
    )
    rnd_pk = random.randint(2000, 7000)
    rnd_amt = round(random.uniform(3200.0, 9800.0), 2)
    world_b_data = _build_world_seed(
        base_pk=rnd_pk,
        order_ref=f"HIDSO{rnd_pk}",
        cust_name=f"Hidden Beta Robotics {rnd_pk}",
        amount=rnd_amt,
        with_noise=True,
    )
    erp = _MultiTenantMockERP({"world_a_tenant": world_a_data, "world_b_tenant": world_b_data})

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            try:
                res = erp.handle_rpc(str(req["method"]), dict(req.get("params") or {}))
                body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": res}).encode("utf-8")
                self.send_response(200)
            except Exception as exc:
                body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "error": str(exc)}).encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{srv.server_port}/jsonrpc"
    endpoints = {
        "world_a": {"url": url, "db": "world_a_tenant", "token": "tok-world_a_tenant"},
        "world_b": {"url": url, "db": "world_b_tenant", "token": "tok-world_b_tenant"},
    }
    try:
        yield url, erp, endpoints
    finally:
        srv.shutdown()
        srv.server_close()


_CLEAN_DRIVER_SOURCE = '''"""Clean thin HTTP JSON-RPC protocol driver parameterized by endpoint_config + mapping."""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Mapping

from open_ontology.induction import AdapterMappingSpec
from open_ontology.settlement import SourceBridge


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: AdapterMappingSpec) -> SourceBridge:
    url = str(endpoint_config["url"])
    db = str(endpoint_config["db"])
    token = str(endpoint_config["token"])

    em_by_ot = {em.target_object_type: em for em in mapping.entity_mappings}
    ab_by_name = {ab.action_name: ab for ab in mapping.action_bindings}

    def _prefix(ot: str) -> str:
        return em_by_ot[ot].id_transform.prefix

    def _strip(ot: str, cid: Any) -> int:
        pfx = _prefix(ot)
        raw = str(cid)
        return int(raw[len(pfx):] if pfx and raw.startswith(pfx) else raw)

    def _enum(ot: str, field: str, raw_val: Any) -> str:
        for tf in em_by_ot[ot].field_transforms:
            if tf.target_field == field and tf.kind == "enum_map":
                return str((tf.enum_map or {}).get(str(raw_val), raw_val))
        return str(raw_val)

    def _rpc(method: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"db": db, "token": token, **dict(extra or {})}}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))["result"]

    def observe(move_envelope: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        _ = move_envelope
        companies = _rpc("list_records", {"table": "companies"})["records"]
        partners = _rpc("list_records", {"table": "partners"})["records"]
        orders = _rpc("list_records", {"table": "orders"})["records"]
        deliveries = _rpc("list_records", {"table": "deliveries"})["records"]
        invoices = _rpc("list_records", {"table": "invoices"})["records"]

        valid_order_ids = {int(o["id"]) for o in orders if o.get("partner_id") and o.get("company_id")}
        return {
            "AccountingEntity": [
                {"entity_id": f"{_prefix('AccountingEntity')}{c['id']}", "name": c["name"], "base_currency": c["currency"], "status": "active"}
                for c in companies
            ],
            "BusinessParty": [
                {"party_id": f"{_prefix('BusinessParty')}{p['id']}", "legal_name": p["name"], "tax_id": p["tax_id"], "country_code": p["country"], "status": "active"}
                for p in partners
            ],
            "O2cCustomerProfile": [
                {
                    "profile_id": f"{_prefix('O2cCustomerProfile')}{p['id']}",
                    "party": f"{_prefix('BusinessParty')}{p['id']}",
                    "accounting_entity": f"{_prefix('AccountingEntity')}{p['company_id']}",
                    "credit_limit": float(p["credit_limit"]),
                    "payment_terms": str(p["payment_terms"]),
                    "status": "active",
                }
                for p in partners
            ],
            "O2cOrder": [
                {
                    "order_id": f"{_prefix('O2cOrder')}{o['id']}",
                    "order_number": str(o["number"]),
                    "customer": f"{_prefix('O2cCustomerProfile')}{o['partner_id']}",
                    "accounting_entity": f"{_prefix('AccountingEntity')}{o['company_id']}",
                    "amount_total": float(o["amount_total"]),
                    "status": _enum("O2cOrder", "status", o["state"]),
                }
                for o in orders
                if int(o["id"]) in valid_order_ids
            ],
            "O2cDelivery": [
                {
                    "delivery_id": f"{_prefix('O2cDelivery')}{d['id']}",
                    "order": f"{_prefix('O2cOrder')}{d['order_id']}",
                    "status": _enum("O2cDelivery", "status", d["state"]),
                }
                for d in deliveries
                if d.get("order_id") and int(d["order_id"]) in valid_order_ids
            ],
            "O2cInvoice": [
                {
                    "invoice_id": f"{_prefix('O2cInvoice')}{inv['id']}",
                    "order": f"{_prefix('O2cOrder')}{inv['order_id']}",
                    "amount_total": float(inv["amount_total"]),
                    "amount_residual": float(inv["amount_residual"]),
                    "payment_state": _enum("O2cInvoice", "payment_state", inv["payment_state"]),
                    "status": _enum("O2cInvoice", "status", inv["state"]),
                }
                for inv in invoices
                if inv.get("order_id") and int(inv["order_id"]) in valid_order_ids
            ],
        }

    def actuate(move_envelope: dict[str, Any]) -> None:
        action = str(move_envelope["action_type"])
        params = dict(move_envelope.get("params") or {})
        method = ab_by_name[action].external_method
        if action == "confirm_o2c_order":
            _rpc(method, {"order_id": _strip("O2cOrder", params["order_id"])})
        elif action == "complete_o2c_delivery":
            _rpc(method, {"delivery_id": _strip("O2cDelivery", params["delivery_id"])})
        elif action == "post_o2c_invoice":
            _rpc(method, {"invoice_id": _strip("O2cInvoice", params["invoice_id"])})
        elif action == "record_o2c_payment":
            _rpc(method, {"invoice_id": _strip("O2cInvoice", params["invoice_id"]), "amount": float(params["payment_amount"])})

    return SourceBridge(actuate=actuate, observe=observe)
'''


# ---------------------------------------------------------------------------
# 1. AST Security & Anti-Cheat Cage Red Tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("bad_code", "expected_code", "forbidden_lits", "max_lines"),
    [
        (
            "import subprocess\ndef build_device_bridge(e, m): pass\n",
            "FORBIDDEN_DRIVER_IMPORT",
            (),
            350,
        ),
        (
            "from open_ontology.design import run_design_verify\ndef build_device_bridge(e, m): pass\n",
            "FORBIDDEN_DRIVER_IMPORT",
            (),
            350,
        ),
        (
            "from open_ontology.induction import DifferentialFalsifier\ndef build_device_bridge(e, m): pass\n",
            "FORBIDDEN_DRIVER_IMPORT",
            (),
            350,
        ),
        (
            "def build_device_bridge(e, m):\n    return eval('1+1')\n",
            "FORBIDDEN_DYNAMIC_EXEC",
            (),
            350,
        ),
        (
            "def build_device_bridge(e, m):\n    open('/tmp/leak.txt', 'w').write('x')\n",
            "FORBIDDEN_FILESYSTEM_WRITE",
            (),
            350,
        ),
        (
            "def build_device_bridge(e, m):\n    return {'ref': 'PUBSO-01'}\n",
            "FORBIDDEN_HARDCODED_SAMPLE",
            ("PUBSO-01", "world_a_tenant"),
            350,
        ),
        (
            "\n".join(f"# pad {i}" for i in range(40)) + "\ndef build_device_bridge(e, m): pass\n",
            "DRIVER_EXCEEDS_LINE_BUDGET",
            (),
            20,
        ),
    ],
)
def test_audit_thin_driver_ast_red_cases(
    tmp_path: Path,
    bad_code: str,
    expected_code: str,
    forbidden_lits: tuple[str, ...],
    max_lines: int,
) -> None:
    drv = tmp_path / "bad_driver.py"
    drv.write_text(bad_code, encoding="utf-8")
    issues = audit_thin_driver_ast(drv, forbidden_literals=forbidden_lits, max_lines=max_lines)
    assert any(msg.startswith(f"{expected_code}:") for msg in issues), (
        f"expected {expected_code}, got {issues}"
    )


def test_design_cli_blocks_cheating_driver_before_execution(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed.flag"
    # Module-level side effect MUST NEVER run because AST audit rejects before spec_from_file_location
    cheating = tmp_path / "cheat_driver.py"
    cheating.write_text(
        f"import subprocess\nopen({str(sentinel)!r}, 'w').write('ran')\ndef build_device_bridge(e, m): pass\n",
        encoding="utf-8",
    )
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps(_make_o2c_mapping().model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    dev_path = tmp_path / "device.json"
    dev_path.write_text(
        json.dumps(
            {
                "device_id": "mock_erp",
                "kind": "jsonrpc",
                "mapping_path": str(mapping_path),
                "driver_module_path": str(cheating),
                "endpoint_config": {"url": "http://127.0.0.1:9/unused", "db": "x", "token": "y"},
                "owned_object_types": O2C_OWNED_TYPES,
                "handled_actions": O2C_HANDLED_ACTIONS,
            }
        ),
        encoding="utf-8",
    )

    rc = design_main(
        [
            "check",
            "--blueprint",
            STD_O2C_BP_ARG,
            "--seed",
            BUSINESS_SEED_ARG,
            "--device",
            str(dev_path),
            "--forbidden-literal",
            "unused",
        ]
    )
    assert rc == 1
    assert not sentinel.exists(), "offending driver was executed instead of blocked by AST cage"

    chk = run_design_check(
        blueprints=[STD_O2C_BP_ARG],
        seed=BUSINESS_SEED_ARG,
        devices=[str(dev_path)],
        forbidden_literals=["unused"],
    )
    assert not chk["ok"]
    assert {"FORBIDDEN_DRIVER_IMPORT", "FORBIDDEN_FILESYSTEM_WRITE"} <= set(chk["error_codes"])


# ---------------------------------------------------------------------------
# 2. Overfit Driver Passes World A, Falsified on Randomized Hidden World B
# ---------------------------------------------------------------------------
def test_overfit_driver_passes_world_a_but_falsified_on_hidden_world_b(
    tmp_path: Path,
    mock_erp_server: tuple[str, _MultiTenantMockERP, dict[str, dict[str, Any]]],
) -> None:
    _, erp, endpoints = mock_erp_server
    mapping = _make_o2c_mapping()
    mapping_path = tmp_path / "o2c_mapping.json"
    mapping_path.write_text(json.dumps(mapping.model_dump(mode="json"), indent=2), encoding="utf-8")

    # Subtly overfit driver:
    # 1. Contains NO World-A string literals (so it slips past naive literal grep)
    # 2. Works cleanly on World A where base_pk == 100 and there are NO unlinked noise rows
    # 3. Silently breaks on Hidden World B:
    #    - does NOT filter unlinked deliveries/invoices (null/False FK crashes schema/crucible!)
    #    - assumes single company / fixed PK math (id - 10 == partner_id) on orders
    overfit_code = _CLEAN_DRIVER_SOURCE.replace(
        'if d.get("order_id") and int(d["order_id"]) in valid_order_ids',
        'if True',
    ).replace(
        'if inv.get("order_id") and int(inv["order_id"]) in valid_order_ids',
        'if True',
    ).replace(
        'o[\'partner_id\']',
        '(110 if o["partner_id"] == 110 else int(o["partner_id"]) - 1)',
    )
    driver_path = tmp_path / "overfit_driver.py"
    driver_path.write_text(overfit_code, encoding="utf-8")

    # Passes AST audit (no banned imports, <= 350 lines, no World-A DB/name literals)
    assert audit_thin_driver_ast(
        driver_path, forbidden_literals=["world_a_tenant", "Public Alpha Photonics", "PUBSO-01"]
    ) == []

    # Passes Public World A!
    world_a_dev = {
        "device_id": "mock_erp",
        "kind": "jsonrpc_erp",
        "mapping_path": str(mapping_path),
        "driver_module_path": str(driver_path),
        "endpoint_config": endpoints["world_a"],
        "owned_object_types": O2C_OWNED_TYPES,
        "handled_actions": O2C_HANDLED_ACTIONS,
    }
    pub_ver = run_design_verify(
        blueprints=[STD_O2C_BP_ARG],
        seed=BUSINESS_SEED_ARG,
        devices=[json.dumps(world_a_dev)],
        reset_hooks={"mock_erp": lambda ep: erp.reset_tenant(str(ep["db"]))},
    )
    assert pub_ver["ok"] and pub_ver["verdict"] == "CORROBORATED", f"expected overfit to pass World A, got {pub_ver}"

    # FAILS on randomized Hidden World B!
    hid_ver = verify_thin_driver_on_hidden_world(
        blueprints=[STD_O2C_BP_ARG],
        seed=BUSINESS_SEED_ARG,
        driver_path=driver_path,
        mapping_path=mapping_path,
        hidden_endpoint_config=endpoints["world_b"],
        forbidden_literals=["world_a_tenant", "Public Alpha Photonics", "PUBSO-01"],
        device_id="mock_erp",
        owned_object_types=O2C_OWNED_TYPES,
        handled_actions=O2C_HANDLED_ACTIONS,
        reset_fn=lambda ep: erp.reset_tenant(str(ep["db"])),
    )
    assert not hid_ver["ok"]
    assert hid_ver["verdict"] == "FALSIFIED"


# ---------------------------------------------------------------------------
# 3. Clean Thin Driver Green Path + Out-of-Band Tamper -> DEVICE_STATE_MISMATCH
# ---------------------------------------------------------------------------
def test_clean_thin_http_driver_passes_cli_world_a_and_blind_hidden_world_b(
    tmp_path: Path,
    mock_erp_server: tuple[str, _MultiTenantMockERP, dict[str, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, erp, endpoints = mock_erp_server
    mapping = _make_o2c_mapping()
    mapping_path = tmp_path / "o2c_mapping.json"
    mapping_path.write_text(json.dumps(mapping.model_dump(mode="json"), indent=2), encoding="utf-8")

    driver_path = tmp_path / "driver.py"
    driver_path.write_text(_CLEAN_DRIVER_SOURCE, encoding="utf-8")

    reset_hook_file = tmp_path / "reset_hook.py"
    reset_hook_file.write_text(
        """from __future__ import annotations
import json, urllib.request
def reset_erp(ep):
    req = urllib.request.Request(
        ep["url"],
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "reset", "params": {"db": ep["db"]}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()
""",
        encoding="utf-8",
    )

    world_a_dev_path = tmp_path / "device_world_a.json"
    world_a_dev_path.write_text(
        json.dumps(
            {
                "device_id": "mock_erp",
                "kind": "jsonrpc_erp",
                "mapping_path": str(mapping_path),
                "driver_module_path": str(driver_path),
                "endpoint_config": endpoints["world_a"],
                "reset_hook": f"{reset_hook_file}:reset_erp",
                "owned_object_types": O2C_OWNED_TYPES,
                "handled_actions": O2C_HANDLED_ACTIONS,
            }
        ),
        encoding="utf-8",
    )

    forbidden_world_a = [
        "world_a_tenant",
        "Public Alpha Photonics",
        "PUBSO-01",
        "PUBSO-02",
        *(f"UNUSED_WORLD_A_REF_{i:02d}_{'x' * 24}" for i in range(30)),
    ]
    assert design_main(
        [
            "check",
            "--blueprint",
            STD_O2C_BP_ARG,
            "--seed",
            BUSINESS_SEED_ARG,
            "--device",
            str(world_a_dev_path),
            "--forbidden-literal",
            "world_a_tenant",
        ]
    ) == 0
    chk_out = json.loads(capsys.readouterr().out)
    assert chk_out["ok"]

    assert design_main(
        [
            "verify",
            "--blueprint",
            STD_O2C_BP_ARG,
            "--seed",
            BUSINESS_SEED_ARG,
            "--device",
            str(world_a_dev_path),
            "--forbidden-literal",
            "world_a_tenant",
        ]
    ) == 0
    ver_out = json.loads(capsys.readouterr().out)
    assert ver_out["ok"] and ver_out["verdict"] == "CORROBORATED"

    def _oob_tamper_hidden_world_b(ep: dict[str, Any], m0_before: list[dict[str, Any]]) -> dict[str, Any]:
        draft_ord = next(
            e for e in m0_before if e["object_type"] == "O2cOrder" and e["data"]["status"] == "draft"
        )
        raw_oid = int(str(draft_ord["object_id"]).removeprefix("ORD-"))
        erp.handle_rpc("oob_cancel_order", {"db": ep["db"], "token": ep["token"], "order_id": raw_oid})
        return {
            "action": "confirm_o2c_order",
            "params": {"order_id": draft_ord["object_id"]},
            "actor_role": "sales_rep",
        }

    hid_res = verify_thin_driver_on_hidden_world(
        blueprints=[STD_O2C_BP_ARG],
        seed=BUSINESS_SEED_ARG,
        driver_path=driver_path,
        mapping_path=mapping_path,
        hidden_endpoint_config=endpoints["world_b"],
        forbidden_literals=forbidden_world_a,
        device_id="mock_erp",
        owned_object_types=O2C_OWNED_TYPES,
        handled_actions=O2C_HANDLED_ACTIONS,
        reset_fn=lambda ep: erp.reset_tenant(str(ep["db"])),
        oob_tamper_check=_oob_tamper_hidden_world_b,
    )
    assert hid_res["ok"], hid_res
    assert hid_res["verdict"] == "CORROBORATED"
    assert hid_res["oob_tamper_result"] is not None
    assert hid_res["oob_tamper_result"]["passed"] is True
    assert hid_res["oob_tamper_result"]["caught_error_code"] == "DEVICE_STATE_MISMATCH"
    assert hid_res["oob_tamper_result"]["abox_unchanged"] is True
    assert hid_res["oob_tamper_result"]["audit_status"] == "REJECTED"


def test_lying_driver_that_echoes_diff_caught_by_oob_tamper_check(
    tmp_path: Path,
    mock_erp_server: tuple[str, _MultiTenantMockERP, dict[str, dict[str, Any]]],
) -> None:
    _, erp, endpoints = mock_erp_server
    mapping = _make_o2c_mapping()
    mapping_path = tmp_path / "o2c_mapping.json"
    mapping_path.write_text(json.dumps(mapping.model_dump(mode="json"), indent=2), encoding="utf-8")

    # Dishonest observe() echoes expected post-state from move_envelope instead of physical ERP state
    lying_source = _CLEAN_DRIVER_SOURCE.replace(
        "        _ = move_envelope",
        """        if move_envelope is not None:
            out: dict[str, list[dict[str, Any]]] = {}
            for ch in (move_envelope.get("object_diff") or {}).get("object_changes") or []:
                out.setdefault(str(ch["object_type"]), []).append(dict(ch["after"]))
            return out""",
    )
    driver_path = tmp_path / "lying_driver.py"
    driver_path.write_text(lying_source, encoding="utf-8")

    def _oob_tamper(ep: dict[str, Any], m0_before: list[dict[str, Any]]) -> dict[str, Any]:
        draft_ord = next(
            e for e in m0_before if e["object_type"] == "O2cOrder" and e["data"]["status"] == "draft"
        )
        raw_oid = int(str(draft_ord["object_id"]).removeprefix("ORD-"))
        erp.handle_rpc("oob_cancel_order", {"db": ep["db"], "token": ep["token"], "order_id": raw_oid})
        return {"action": "confirm_o2c_order", "params": {"order_id": draft_ord["object_id"]}, "actor_role": "sales_rep"}

    hid_res = verify_thin_driver_on_hidden_world(
        blueprints=[STD_O2C_BP_ARG],
        seed=BUSINESS_SEED_ARG,
        driver_path=driver_path,
        mapping_path=mapping_path,
        hidden_endpoint_config=endpoints["world_b"],
        forbidden_literals=["world_a_tenant"],
        device_id="mock_erp",
        owned_object_types=O2C_OWNED_TYPES,
        handled_actions=O2C_HANDLED_ACTIONS,
        reset_fn=lambda ep: erp.reset_tenant(str(ep["db"])),
        oob_tamper_check=_oob_tamper,
    )
    assert not hid_res["ok"]
    assert hid_res["verdict"] == "FALSIFIED"
    assert "OOB_TAMPER_UNDETECTED" in hid_res["error_codes"]
