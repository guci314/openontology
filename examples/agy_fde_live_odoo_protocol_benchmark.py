"""WO-14 Deliverable 3C: Live Odoo 19 Network JSON-RPC ``agy`` CLI Blind FDE Benchmark.

Architecture & Isolation Guarantees:
1. Public World A (``wo14_pub_<rand>``) is cloned from ``odoo_demo``, cleaned of
   pre-existing demo orders, and seeded with 4 coherent O2C flows covering all 4
   ``std_o2c`` actions (``confirm_o2c_order``, ``complete_o2c_delivery``,
   ``post_o2c_invoice``, ``record_o2c_payment``).
2. A strictly isolated workspace is created:
   - ``target_abi.json`` (``0444``): locked ``std_o2c_ontology`` Target ABI
   - ``world_a_endpoint.json`` (``0444``): Odoo 19 JSON-RPC url/db/user/password
   - ``requirements.md`` (``0444``): zero Odoo model/field/method spoilers
   - ``SKILL.md`` (``0444``): FDE skill manual incl. Section 4 thin driver spec
   - ``.judge/`` (``0444``) + ``verify_driver.sh`` (``0555``): compiler + falsifier
   - ``out/`` (writable ONLY): ``agy`` must produce ``out/odoo_o2c_mapping.json``
     and ``out/driver.py`` (``<= 350`` lines, pure protocol bridge).
3. An ephemeral localhost reset HTTP server lets ``verify_driver.sh verify`` reset
   ``wo14_pub_<rand>`` from ``wo14_pub_tpl_<rand>`` cleanly between waves without
   exposing any Odoo credentials or master password inside `.judge/reset_hook.py`.
4. After ``agy`` exits with ``CORROBORATED`` on Public World A, the independent
   outer referee:
   - Verifies SHA-256 hashes of all read-only workspace files are untouched;
   - Runs ``audit_thin_driver_ast(..., forbidden_literals=<world_a_fingerprints>)``;
   - NOW creates a brand-new, randomly seeded & PK-shifted Hidden World B DB
     (``wo14_hid_<rand>``) with completely different customer names, amounts,
     and refs that ``agy`` has never seen;
   - Executes ``verify_thin_driver_on_hidden_world`` against Hidden World B,
     including out-of-band external Odoo ``sale.order.action_cancel`` tamper
     asserting ``DeviceStateMismatchError`` (``DEVICE_STATE_MISMATCH``) and zero
     ABox dirty write;
   - Unconditionally drops all ``wo14_*`` databases in ``try / finally`` and
     asserts ``localhost:8069`` DB list returns to
     ``['erp_north', 'erp_south', 'odoo_demo']``;
   - Writes full audit evidence to ``/tmp/wo14_live_odoo_agy_fde.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.std_o2c_ontology import build_std_o2c_blueprint  # noqa: E402
from open_ontology.design import audit_thin_driver_ast, verify_thin_driver_on_hidden_world  # noqa: E402
from open_ontology.induction import AdapterMappingSpec  # noqa: E402

ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069/jsonrpc")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")
SOURCE_DEMO_DB = "odoo_demo"
MASTER_CANDIDATES = tuple(
    p
    for p in (
        os.environ.get("ODOO_MASTER_PASSWORD"),
        "admin",
        "odoo-demo-master-2026",
    )
    if p
)
SKILL_MD_PATH = ROOT / "skills" / "openontology-fde" / "SKILL.md"
STD_O2C_BP_ARG = "examples.std_o2c_ontology:build_std_o2c_blueprint"
BUSINESS_SEED_ARG = "examples.business_ontology:BUSINESS_SEED_ONTOLOGY"
DEFAULT_AGY_MODELS: tuple[str, ...] = (
    "gemini-4-neon-medium",
    "gemini-4-argon-medium",
    "gemini-3.8-flash-medium",
)
DEFAULT_OPENCODE_DEEPSEEK_MODELS: tuple[str, ...] = (
    "opencode-go/deepseek-v4.1-flash",
    "opencode/deepseek-v4.1-flash",
    "opencode/deepseek-v4.1-flash-free",
    "openrouter/deepseek/deepseek-v4.1-flash",
    "codebuddy/deepseek-v4.1-flash",
)
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
_FORBIDDEN_SPOILER_TOKENS = (
    "sale.order",
    "stock.picking",
    "stock.move",
    "account.move",
    "res.partner",
    "res.company",
    "account.payment.register",
    "action_confirm",
    "button_validate",
    "action_post",
    "action_create_payments",
    "execute_kw",
    "client_order_ref",
    "move_type",
    "invoice_origin",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rpc(url: str, service: str, method: str, args: list[Any], *, timeout: int = 30) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"Odoo RPC error ({service}.{method}): {data['error']}")
    return data.get("result")


class _OdooAdminClient:
    """Referee-only Odoo 19 JSON-RPC + scoped Postgres cleanup client."""

    def __init__(self, url: str = ODOO_URL) -> None:
        self.url = url
        self.created_dbs: list[str] = []
        self.master_password = self._resolve_master_password()

    @staticmethod
    def _assert_safe_wo14_name(db_name: str) -> None:
        if not re.fullmatch(r"wo14_[a-z0-9_]+", db_name):
            raise ValueError(f"Refusing to drop non-wo14 database {db_name!r}")

    def db_exist(self, db_name: str) -> bool:
        return bool(_rpc(self.url, "db", "db_exist", [db_name], timeout=15))

    def list_wo14_pg_dbs(self) -> list[str]:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "odoo-demo-db",
                "psql",
                "-U",
                "odoo",
                "-d",
                "postgres",
                "-tAc",
                "SELECT datname FROM pg_database WHERE datname LIKE 'wo14_%' ORDER BY datname;",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _resolve_master_password(self) -> str:
        probe_db = f"wo14_authcheck_{uuid.uuid4().hex[:6]}"
        for cand in MASTER_CANDIDATES:
            try:
                _rpc(self.url, "db", "duplicate_database", [cand, SOURCE_DEMO_DB, probe_db], timeout=60)
                self.master_password = cand
                self.drop_db(probe_db)
                return cand
            except Exception:
                if self.db_exist(probe_db):
                    try:
                        self.master_password = cand
                        self.drop_db(probe_db)
                    except Exception:
                        pass
        raise RuntimeError(f"Unable to authenticate Odoo master password on {self.url}")

    def list_dbs(self) -> list[str]:
        return list(_rpc(self.url, "db", "list", [], timeout=15))

    def clone_db(self, source_db: str, target_db: str) -> None:
        self._assert_safe_wo14_name(target_db)
        _rpc(
            self.url,
            "db",
            "duplicate_database",
            [self.master_password, source_db, target_db],
            timeout=60,
        )
        if target_db not in self.created_dbs:
            self.created_dbs.append(target_db)

    def drop_db(self, db_name: str) -> None:
        self._assert_safe_wo14_name(db_name)
        if not self.db_exist(db_name):
            return
        try:
            _rpc(self.url, "db", "drop", [self.master_password, db_name], timeout=30)
        except Exception:
            pass
        if self.db_exist(db_name):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "odoo-demo-db",
                    "psql",
                    "-U",
                    "odoo",
                    "-d",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);',
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["docker", "exec", "odoo-demo-app", "rm", "-rf", f"/var/lib/odoo/filestore/{db_name}"],
                text=True,
                capture_output=True,
                check=True,
            )
        if self.db_exist(db_name):
            raise RuntimeError(f"Failed to drop temporary database {db_name!r}")

    def drop_all_created(self) -> list[str]:
        candidates = sorted(set(self.created_dbs) | set(self.list_wo14_pg_dbs()))
        for db in candidates:
            if db.startswith("wo14_"):
                self.drop_db(db)
        remaining = self.list_wo14_pg_dbs()
        if remaining:
            raise RuntimeError(f"Residual wo14_* databases remain in Postgres: {remaining}")
        return sorted(self.list_dbs())

    def login_kw(self, db_name: str) -> Any:
        uid = _rpc(self.url, "common", "authenticate", [db_name, ODOO_USER, ODOO_PASSWORD, {}])
        if not uid:
            raise RuntimeError(f"Failed login to {db_name!r} as {ODOO_USER!r}")

        def _kw(model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
            return _rpc(
                self.url,
                "object",
                "execute_kw",
                [db_name, uid, ODOO_PASSWORD, model, method, args, kwargs or {}],
                timeout=30,
            )

        return _kw


def _clean_and_seed_o2c_world(
    admin: _OdooAdminClient,
    db_name: str,
    *,
    tag: str,
    customer_a: dict[str, Any],
    customer_b: dict[str, Any],
    order_specs: Sequence[dict[str, Any]],
    shift_pks: int = 0,
) -> dict[str, Any]:
    """Strip pre-existing demo sales noise and seed 4 coherent O2C lifecycle flows."""
    kw = admin.login_kw(db_name)

    # 1. Cancel & remove pre-existing demo sale.orders, open pickings, and draft out_invoices
    old_orders = kw("sale.order", "search", [[]])
    if old_orders:
        kw("sale.order", "write", [old_orders, {"locked": False}])
        non_cancel = kw("sale.order", "search", [[("state", "!=", "cancel")]])
        if non_cancel:
            kw("sale.order", "action_cancel", [non_cancel])
        for oid in old_orders:
            try:
                kw("sale.order", "unlink", [[oid]])
            except Exception:
                pass

    old_picks = kw("stock.picking", "search", [[]])
    for pid in old_picks:
        try:
            st = kw("stock.picking", "read", [[pid], ["state"]])[0]["state"]
            if st not in ("done", "cancel"):
                kw("stock.picking", "action_cancel", [[pid]])
            kw("stock.picking", "unlink", [[pid]])
        except Exception:
            pass

    old_invs = kw("account.move", "search", [[("move_type", "=", "out_invoice")]])
    for iid in old_invs:
        try:
            st = kw("account.move", "read", [[iid], ["state"]])[0]["state"]
            if st == "posted":
                kw("account.move", "button_draft", [[iid]])
                st = "draft"
            if st == "draft":
                kw("account.move", "button_cancel", [[iid]])
            kw("account.move", "unlink", [[iid]])
        except Exception:
            pass

    # Reset customer_rank on pre-existing demo contacts without archiving admin/company partners (1, 3)
    ranked_old = kw("res.partner", "search", [[("customer_rank", ">", 0)]])
    if ranked_old:
        kw("res.partner", "write", [ranked_old, {"customer_rank": 0}])

    # 2. Optionally shift PostgreSQL sequences for Hidden World B so PKs cannot match World A
    if shift_pks > 0:
        for idx in range(shift_pks):
            tmp_p = kw("res.partner", "create", [{"name": f"__seq_shift_p_{idx}", "company_id": 1}])
            tmp_pr = kw("product.product", "create", [{"name": f"__seq_shift_pr_{idx}", "type": "consu"}])
            tmp_o = kw(
                "sale.order",
                "create",
                [{"partner_id": tmp_p, "order_line": [(0, 0, {"product_id": tmp_pr, "product_uom_qty": 1, "price_unit": 10.0})]}],
            )
            kw("sale.order", "unlink", [[tmp_o]])
            kw("product.product", "unlink", [[tmp_pr]])
            kw("res.partner", "unlink", [[tmp_p]])

    # 3. Create 2 clean customers + 1 tax-free storable product
    us_countries = kw("res.country", "search", [[("code", "=", customer_a.get("country_code", "CN"))]], {"limit": 1})
    country_id = int(us_countries[0]) if us_countries else False

    def _mk_partner(spec: dict[str, Any]) -> int:
        vals: dict[str, Any] = {
            "name": spec["name"],
            "ref": spec["ref"],
            "vat": spec["tax_id"],
            "is_company": True,
            "customer_rank": 1,
            "credit_limit": float(spec["credit_limit"]),
            "company_id": 1,
        }
        if country_id:
            vals["country_id"] = country_id
        return int(kw("res.partner", "create", [vals]))

    p_a_id = _mk_partner(customer_a)
    p_b_id = _mk_partner(customer_b)
    partner_ids = [p_a_id, p_b_id]

    prod_id = int(
        kw(
            "product.product",
            "create",
            [
                {
                    "name": f"O2C Standard Hardware {tag}",
                    "default_code": f"HW-{tag}",
                    "type": "consu",
                    "is_storable": True,
                    "list_price": 1000.0,
                    "sale_ok": True,
                    "taxes_id": [(6, 0, [])],
                }
            ],
        )
    )

    seeded_orders: list[dict[str, Any]] = []
    for idx, ospec in enumerate(order_specs):
        pid = partner_ids[idx % len(partner_ids)]
        stage = ospec["stage"]  # draft | confirmed | draft_invoice | posted_invoice
        qty = int(ospec["qty"])
        price = float(ospec["unit_price"])
        ref = str(ospec["client_ref"])

        so_id = int(
            kw(
                "sale.order",
                "create",
                [
                    {
                        "partner_id": pid,
                        "client_order_ref": ref,
                        "order_line": [
                            (
                                0,
                                0,
                                {
                                    "product_id": prod_id,
                                    "name": f"{tag} Line Item {idx + 1}",
                                    "product_uom_qty": qty,
                                    "price_unit": price,
                                    "tax_ids": [(6, 0, [])],
                                },
                            )
                        ],
                    }
                ],
            )
        )
        so_row = kw("sale.order", "read", [[so_id], ["id", "name", "state", "amount_total", "picking_ids"]])[0]
        picking_id = None
        invoice_id = None

        if stage in ("confirmed", "draft_invoice", "posted_invoice"):
            kw("sale.order", "action_confirm", [[so_id]])
            so_row = kw("sale.order", "read", [[so_id], ["id", "name", "state", "amount_total", "picking_ids"]])[0]
            picking_ids = [int(x) for x in (so_row.get("picking_ids") or [])]
            if not picking_ids:
                raise RuntimeError(f"Confirming order {so_row['name']} created no stock.picking")
            picking_id = picking_ids[0]

        if stage in ("draft_invoice", "posted_invoice"):
            kw("stock.picking", "action_assign", [[picking_id]])
            moves = kw("stock.move", "search_read", [[("picking_id", "=", picking_id)]], {"fields": ["id", "product_uom_qty"]})
            for mv in moves:
                kw("stock.move", "write", [[int(mv["id"])], {"quantity": float(mv["product_uom_qty"]), "picked": True}])
            kw("stock.picking", "button_validate", [[picking_id]])

            lines = kw(
                "sale.order.line",
                "search_read",
                [[("order_id", "=", so_id)]],
                {"fields": ["id", "product_id", "product_uom_qty", "price_unit"]},
            )
            line = lines[0]
            invoice_id = int(
                kw(
                    "account.move",
                    "create",
                    [
                        {
                            "move_type": "out_invoice",
                            "partner_id": pid,
                            "invoice_date": "2026-11-21",
                            "invoice_origin": str(so_row["name"]),
                            "ref": ref,
                            "invoice_line_ids": [
                                (
                                    0,
                                    0,
                                    {
                                        "product_id": int(line["product_id"][0]),
                                        "quantity": float(line["product_uom_qty"]),
                                        "price_unit": float(line["price_unit"]),
                                        "tax_ids": [(6, 0, [])],
                                        "sale_line_ids": [(6, 0, [int(line["id"])])],
                                    },
                                )
                            ],
                        }
                    ],
                )
            )
            if stage == "posted_invoice":
                kw("account.move", "action_post", [[invoice_id]])

        seeded_orders.append(
            {
                "stage": stage,
                "sale_order_id": so_id,
                "order_name": str(so_row["name"]),
                "client_ref": ref,
                "amount_total": float(so_row["amount_total"]),
                "picking_id": picking_id,
                "invoice_id": invoice_id,
            }
        )

    fingerprints = [
        db_name,
        customer_a["name"],
        customer_b["name"],
        customer_a["tax_id"],
        customer_b["tax_id"],
        *[o["order_name"] for o in seeded_orders],
        *[o["client_ref"] for o in seeded_orders],
    ]
    return {
        "db_name": db_name,
        "partners": [
            {"id": p_a_id, **customer_a},
            {"id": p_b_id, **customer_b},
        ],
        "orders": seeded_orders,
        "fingerprints": fingerprints,
    }


def _build_zero_spoiler_requirements() -> str:
    req = """# Autonomous Enterprise ERP Integration Specification (Blind O2C)

## Mission
Integrate the target external ERP system onto the locked OpenOntology standard contract
defined in `target_abi.json`. Read connection settings dynamically at runtime from
`world_a_endpoint.json` (which contains `url`, `db`, `username`, `password`) and inspect
the live system autonomously over HTTP JSON-RPC.

Do NOT assume or hardcode any external database names, customer names, sample IDs,
document numbers, credentials, or fixed amounts in your generated driver code. Your
submission will be evaluated blindly against an unseen, randomized tenant whose IDs,
customer names, and amounts differ completely from `world_a_endpoint.json`.

## Required Deliverables (write ONLY inside `out/`)
1. `out/odoo_o2c_mapping.json`: valid `AdapterMappingSpec` JSON mapping:
   - All 6 non-owned entity types in `target_abi.json`:
     `AccountingEntity`, `BusinessParty`, `O2cCustomerProfile`, `O2cOrder`,
     `O2cDelivery`, and `O2cInvoice`
     (keep non-empty `id_transform.source_field`, e.g. `"id"`, and consistent prefixes)
   - All 4 actions in `target_abi.json`:
     `confirm_o2c_order`, `complete_o2c_delivery`, `post_o2c_invoice`, `record_o2c_payment`
     (ensure every `ActionExecutionBinding` has matching `id_prefix`, valid `expected_state_field`,
      and `expected_state_after`, and that enum maps include both initial and target states).
2. `out/driver.py`: ultra-thin Python protocol driver (`<= 350` lines) exposing:
   `build_device_bridge(endpoint_config: Mapping[str, Any], mapping: AdapterMappingSpec) -> SourceBridge`

## Domain & Semantic Expectations
- Derive canonical entity IDs strictly using the prefixes configured in `mapping`, reading raw
  identifiers, foreign keys, and fields dynamically from the live ERP over JSON-RPC.
- Reflect each order's true end-to-end O2C lifecycle state (`draft` -> `confirmed` -> `delivered`
  -> `invoiced`, or `cancelled`) by inspecting the live order together with its linked fulfillment
  delivery and posted billing records.
- Ignore unrelated internal contacts (non-customer contacts / missing legal entity links),
  orphaned stock movements without an active sales order, standalone non-sales journal entries,
  and historical cancelled invoices without a valid parent customer order so foreign keys stay clean.
- Preserve genuine `cancelled` customer orders in `observe()` if an order record itself is cancelled,
  so out-of-band external cancellation is immediately visible as physical truth.
- NEVER fabricate or echo `move_envelope["object_diff"]` inside `observe()`.
- In `actuate(move_envelope)`, perform only the true remote state transition (including confirming
  orders, completing delivery quantities/validation, posting draft invoices, and registering
  full or partial receivable settlement payments). If the external ERP rejects actuation due
  to a business-state conflict (e.g. an order cancelled out-of-band outside OpenOntology),
  catch that RPC state-conflict error cleanly in `actuate` so subsequent `observe()` reports the
  actual physical state and triggers `DEVICE_STATE_MISMATCH` with zero ABox corruption.

## Verification
Iterate with:
- `./verify_driver.sh check`
- `./verify_driver.sh verify`
until `verify` exits `0` with `ok == true` and `verdict == "CORROBORATED"`.
"""
    lowered = req.lower()
    leaked = [tok for tok in _FORBIDDEN_SPOILER_TOKENS if tok in lowered]
    assert not leaked, f"requirements.md leaked forbidden ERP spoilers: {leaked}"
    return req


class _EphemeralResetServer:
    """Host-side HTTP reset server keeping Odoo master credentials out of workspace."""

    def __init__(self, admin: _OdooAdminClient, template_db: str, live_db: str) -> None:
        self._lock = threading.RLock()
        self.admin = admin
        self.template_db = template_db
        self.live_db = live_db
        self._srv: ThreadingHTTPServer | None = None
        self.port: int = 0

    def reset_now(self) -> None:
        with self._lock:
            self.admin.drop_db(self.live_db)
            self.admin.clone_db(self.template_db, self.live_db)

    def start(self) -> str:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                parent.reset_now()
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._srv.server_port
        th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        th.start()
        return f"http://127.0.0.1:{self.port}/reset"

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None


def prepare_isolated_odoo_workspace(
    ws_root: Path,
    *,
    world_a_db: str,
    reset_url: str,
) -> dict[str, Any]:
    """Build strictly isolated read-only workspace with writable ``out/`` only."""
    ws_root.mkdir(parents=True, exist_ok=True)
    out_dir = ws_root / "out"
    judge_dir = ws_root / ".judge"
    out_dir.mkdir(exist_ok=True)
    judge_dir.mkdir(exist_ok=True)

    target_bp = build_std_o2c_blueprint()
    abi_path = ws_root / "target_abi.json"
    abi_path.write_text(json.dumps(target_bp.model_dump(mode="json"), ensure_ascii=False, indent=2), "utf-8")

    ep_path = ws_root / "world_a_endpoint.json"
    ep_path.write_text(
        json.dumps(
            {
                "url": ODOO_URL,
                "db": world_a_db,
                "username": ODOO_USER,
                "password": ODOO_PASSWORD,
            },
            indent=2,
        ),
        "utf-8",
    )

    skill_dest = ws_root / "SKILL.md"
    shutil.copy2(SKILL_MD_PATH, skill_dest)

    req_path = ws_root / "requirements.md"
    req_path.write_text(_build_zero_spoiler_requirements(), "utf-8")

    reset_hook_path = judge_dir / "reset_hook.py"
    reset_hook_path.write_text(
        f'''"""Generic HTTP reset callback (contains zero external system credentials)."""
from __future__ import annotations
import urllib.request

def reset_sandbox(_endpoint_config: dict) -> None:
    req = urllib.request.Request({reset_url!r}, data=b"{{}}", method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()
''',
        "utf-8",
    )

    dev_cfg_path = judge_dir / "device_erp.json"
    dev_cfg_path.write_text(
        json.dumps(
            {
                "device_id": "device_erp",
                "name": "External O2C ERP",
                "kind": "jsonrpc_erp",
                "endpoint": ODOO_URL,
                "mapping_path": "../out/odoo_o2c_mapping.json",
                "driver_module_path": "../out/driver.py",
                "endpoint_config_path": "../world_a_endpoint.json",
                "reset_hook": "./reset_hook.py:reset_sandbox",
                "owned_object_types": O2C_OWNED_TYPES,
                "handled_actions": O2C_HANDLED_ACTIONS,
            },
            indent=2,
        ),
        "utf-8",
    )

    verify_sh_path = ws_root / "verify_driver.sh"
    verify_sh_path.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
MODE="${{1:-verify}}"
if [[ "$MODE" != "check" && "$MODE" != "verify" ]]; then
  echo "Usage: ./verify_driver.sh [check|verify]" >&2
  exit 2
fi
export PYTHONPATH="{ROOT}:${{PYTHONPATH:-}}"
if [[ "$MODE" == "verify" ]]; then
  exec "{sys.executable}" -m open_ontology.design verify \\
    --blueprint "{STD_O2C_BP_ARG}" \\
    --seed "{BUSINESS_SEED_ARG}" \\
    --device .judge/device_erp.json \\
    --min-action-coverage 1.0 \\
    --min-transition-coverage 0.80 \\
    --max-waves 2
else
  exec "{sys.executable}" -m open_ontology.design check \\
    --blueprint "{STD_O2C_BP_ARG}" \\
    --seed "{BUSINESS_SEED_ARG}" \\
    --device .judge/device_erp.json
fi
""",
        "utf-8",
    )

    readonly_files = [
        abi_path,
        ep_path,
        skill_dest,
        req_path,
        reset_hook_path,
        dev_cfg_path,
    ]
    for p in readonly_files:
        os.chmod(p, 0o444)
    os.chmod(verify_sh_path, 0o555)

    hashes = {str(p.relative_to(ws_root)): _sha256(p) for p in [*readonly_files, verify_sh_path]}
    return {"readonly_hashes": hashes}


def _verify_readonly_integrity(ws_root: Path, expected_hashes: Mapping[str, str]) -> None:
    tampered: list[str] = []
    for rel, old_digest in expected_hashes.items():
        p = ws_root / rel
        if not p.is_file() or _sha256(p) != old_digest:
            tampered.append(rel)
    assert not tampered, f"Read-only workspace files modified: {tampered}"


def resolve_fde_cli_binary(fde_runner: str) -> str:
    """Locate the external coding-agent CLI binary for ``agy`` or ``opencode``."""
    runner = str(fde_runner).strip().lower()
    if runner == "agy":
        found = shutil.which("agy")
        if not found:
            raise RuntimeError("agy CLI binary not found on PATH")
        return found
    if runner == "opencode":
        found = shutil.which("opencode")
        if not found and Path("/opt/homebrew/bin/opencode").is_file():
            found = "/opt/homebrew/bin/opencode"
        if not found:
            raise RuntimeError("opencode CLI binary not found on PATH or /opt/homebrew/bin/opencode")
        return found
    raise ValueError(f"Unsupported fde_runner {fde_runner!r}; expected 'agy' or 'opencode'")


def build_fde_runner_command(
    *,
    fde_runner: str,
    cli_path: str,
    model_name: str,
    prompt: str,
    timeout_sec: int,
    session_id: str | None = None,
) -> list[str]:
    """Build non-interactive headless subprocess argv for ``agy`` or ``opencode``."""
    runner = str(fde_runner).strip().lower()
    if runner == "agy":
        cmd = [cli_path, "--model", model_name]
        if session_id:
            cmd.extend(["--conversation", session_id])
        cmd.extend(
            [
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
                "--output-format",
                "json",
                "--print-timeout",
                f"{timeout_sec}s",
                "--print",
                prompt,
            ]
        )
        return cmd
    if runner == "opencode":
        cmd = [cli_path, "run", "--auto", "--format", "json", "-m", model_name]
        if session_id:
            cmd.extend(["--session", session_id])
        cmd.append(prompt)
        return cmd
    raise ValueError(f"Unsupported fde_runner {fde_runner!r}; expected 'agy' or 'opencode'")


def parse_fde_runner_output(
    *,
    fde_runner: str,
    stdout: str,
    wall_elapsed_sec: float | None = None,
) -> dict[str, Any] | None:
    """Parse structured JSON (``agy``) or JSONL event stream (``opencode``) output."""
    runner = str(fde_runner).strip().lower()
    raw = (stdout or "").strip()
    if not raw:
        return None
    if runner == "agy":
        if "{" not in raw or "}" not in raw:
            return None
        try:
            obj = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    if runner != "opencode":
        raise ValueError(f"Unsupported fde_runner {fde_runner!r}; expected 'agy' or 'opencode'")

    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except Exception:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    if not events:
        return None

    session_id: str | None = None
    step_finishes = 0
    tool_calls_count = 0
    tools_used: list[str] = []
    text_parts: list[str] = []
    errors: list[Any] = []
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    total_cost = 0.0
    timestamps: list[float] = []

    for ev in events:
        if ev.get("sessionID") and not session_id:
            session_id = str(ev["sessionID"])
        ts = ev.get("timestamp")
        if isinstance(ts, (int, float)):
            timestamps.append(float(ts))
        ev_type = str(ev.get("type") or "")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if ev_type == "tool_use":
            tool_calls_count += 1
            tname = str(part.get("tool") or "")
            if tname and tname not in tools_used:
                tools_used.append(tname)
        elif ev_type == "text":
            txt = str(part.get("text") or "").strip()
            if txt:
                text_parts.append(txt)
        elif ev_type == "step_finish":
            step_finishes += 1
            cost_val = part.get("cost")
            if isinstance(cost_val, (int, float)):
                total_cost += float(cost_val)
            toks = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            input_tokens += int(toks.get("input") or 0)
            output_tokens += int(toks.get("output") or 0)
            reasoning_tokens += int(toks.get("reasoning") or 0)
            cache = toks.get("cache") if isinstance(toks.get("cache"), dict) else {}
            cache_read_tokens += int(cache.get("read") or 0)
            cache_write_tokens += int(cache.get("write") or 0)
        elif ev_type == "error":
            errors.append(ev.get("error") or ev)

    duration_sec = (
        round(float(wall_elapsed_sec), 3)
        if wall_elapsed_sec is not None
        else (
            round((max(timestamps) - min(timestamps)) / 1000.0, 3)
            if len(timestamps) >= 2
            else 0.0
        )
    )
    status = "ERROR" if (errors and step_finishes == 0) else "SUCCESS"
    return {
        "runner": "opencode",
        "conversation_id": session_id,
        "session_id": session_id,
        "status": status,
        "num_turns": step_finishes,
        "tool_calls_count": tool_calls_count,
        "tools_used": tools_used,
        "duration_seconds": duration_sec,
        "total_cost_usd": round(total_cost, 6),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "total_tokens": input_tokens + output_tokens + reasoning_tokens,
        },
        "response": "\n".join(text_parts),
        "errors": errors,
        "event_count": len(events),
    }


def run_live_odoo_agy_fde_benchmark(
    *,
    fde_runner: str = "agy",
    models: tuple[str, ...] | None = None,
    timeout_sec: int = 480,
    output_json_path: Path = Path("/tmp/wo14_live_odoo_agy_fde.json"),
) -> dict[str, Any]:
    """Run isolated live coding-agent benchmark (``agy`` or ``opencode``) on Odoo 19 + blind Hidden World B."""
    runner_name = str(fde_runner).strip().lower()
    cli_path = resolve_fde_cli_binary(runner_name)
    resolved_models: tuple[str, ...] = (
        models
        if models is not None
        else (DEFAULT_OPENCODE_DEEPSEEK_MODELS if runner_name == "opencode" else DEFAULT_AGY_MODELS)
    )
    agent_env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}

    admin = _OdooAdminClient(ODOO_URL)
    rand_id = uuid.uuid4().hex[:6]
    pub_tpl_db = f"wo14_pub_tpl_{rand_id}"
    pub_live_db = f"wo14_pub_{rand_id}"
    hid_tpl_db = f"wo14_hid_tpl_{rand_id}"
    hid_live_db = f"wo14_hid_{rand_id}"

    ws_root = Path(tempfile.mkdtemp(prefix="wo14_agy_odoo_ws_"))
    reset_srv: _EphemeralResetServer | None = None

    try:
        # 1. Provision & seed Public World A cleanly (before launching agy)
        admin.clone_db(SOURCE_DEMO_DB, pub_tpl_db)
        world_a_info = _clean_and_seed_o2c_world(
            admin,
            pub_tpl_db,
            tag=f"PUB{rand_id.upper()}",
            customer_a={
                "name": "Apex Horizon Precision",
                "ref": f"PUB-C1-{rand_id}",
                "tax_id": f"TAX-PUB-A-{rand_id}",
                "credit_limit": 500000.0,
                "country_code": "CN",
            },
            customer_b={
                "name": "Meridian Supply Chain",
                "ref": f"PUB-C2-{rand_id}",
                "tax_id": f"TAX-PUB-B-{rand_id}",
                "credit_limit": 420000.0,
                "country_code": "CN",
            },
            order_specs=[
                {"stage": "draft", "qty": 2, "unit_price": 1500.0, "client_ref": f"PUB-REF1-{rand_id}"},
                {"stage": "confirmed", "qty": 3, "unit_price": 2000.0, "client_ref": f"PUB-REF2-{rand_id}"},
                {"stage": "draft_invoice", "qty": 2, "unit_price": 2500.0, "client_ref": f"PUB-REF3-{rand_id}"},
                {"stage": "posted_invoice", "qty": 1, "unit_price": 3200.0, "client_ref": f"PUB-REF4-{rand_id}"},
            ],
            shift_pks=0,
        )
        admin.clone_db(pub_tpl_db, pub_live_db)
        world_a_forbidden = [pub_live_db, *world_a_info["fingerprints"]]
        world_a_info["live_db_name"] = pub_live_db
        world_a_info["fingerprints"] = world_a_forbidden

        reset_srv = _EphemeralResetServer(admin, pub_tpl_db, pub_live_db)
        reset_url = reset_srv.start()

        prep = prepare_isolated_odoo_workspace(ws_root, world_a_db=pub_live_db, reset_url=reset_url)

        prompt = (
            "Read SKILL.md, requirements.md, target_abi.json, and world_a_endpoint.json in your current directory. "
            "Autonomously introspect the live ERP system at world_a_endpoint.json over HTTP JSON-RPC, "
            "author ONLY out/odoo_o2c_mapping.json (AdapterMappingSpec) and out/driver.py (<= 350 lines exposing "
            "build_device_bridge(endpoint_config, mapping) -> SourceBridge), and iterate with "
            "./verify_driver.sh check and ./verify_driver.sh verify until ./verify_driver.sh verify "
            "exits 0 with ok=true and verdict=CORROBORATED. "
            "Remember: NEVER hardcode DB names, credentials, customer names, order numbers, or amounts in driver.py, "
            "because an unseen randomized tenant will be tested after you finish."
        )

        # 2. Run live external coding agent (agy or opencode) inside isolated workspace
        attempts: list[dict[str, Any]] = []
        chosen_model: str | None = None
        agy_envelope: dict[str, Any] | None = None
        last_verify_probe: dict[str, Any] | None = None

        for model_name in resolved_models:
            cmd = build_fde_runner_command(
                fde_runner=runner_name,
                cli_path=cli_path,
                model_name=model_name,
                prompt=prompt,
                timeout_sec=timeout_sec,
            )
            t0 = time.monotonic()
            timed_out = False
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(ws_root),
                    env=agent_env,
                    text=True,
                    capture_output=True,
                    timeout=timeout_sec + 60,
                )
                rc = proc.returncode
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                rc = 124
                stdout = (exc.stdout or "").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
                stderr = (exc.stderr or "").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            wall_sec = round(time.monotonic() - t0, 3)

            parsed_env = parse_fde_runner_output(
                fde_runner=runner_name,
                stdout=stdout,
                wall_elapsed_sec=wall_sec,
            )

            out_ready = (ws_root / "out" / "odoo_o2c_mapping.json").is_file() and (
                ws_root / "out" / "driver.py"
            ).is_file()

            probe_ver = None
            if out_ready:
                pv = subprocess.run(["./verify_driver.sh", "verify"], cwd=str(ws_root), text=True, capture_output=True)
                try:
                    probe_ver = json.loads(pv.stdout)
                except Exception:
                    probe_ver = {"ok": False, "raw": pv.stdout[:500], "stderr": pv.stderr[:400]}
                last_verify_probe = probe_ver

            attempts.append(
                {
                    "fde_runner": runner_name,
                    "model": model_name,
                    "returncode": rc,
                    "timed_out": timed_out,
                    "agent_status": parsed_env.get("status") if parsed_env else None,
                    "num_turns": parsed_env.get("num_turns") if parsed_env else None,
                    "duration_seconds": (parsed_env.get("duration_seconds") if parsed_env else None) or wall_sec,
                    "tool_calls_count": parsed_env.get("tool_calls_count") if parsed_env else None,
                    "total_cost_usd": parsed_env.get("total_cost_usd") if parsed_env else None,
                    "usage": parsed_env.get("usage") if parsed_env else None,
                    "conversation_id": parsed_env.get("conversation_id") if parsed_env else None,
                    "out_ready": out_ready,
                    "verify_ok": bool(probe_ver and probe_ver.get("ok") is True),
                    "verify_errors": (probe_ver or {}).get("errors", [])[:5],
                    "stderr_tail": stderr[-400:],
                }
            )

            if out_ready and probe_ver and probe_ver.get("ok") is True:
                chosen_model = model_name
                agy_envelope = parsed_env
                break

            # Allow up to 2 continuation repair turns on existing session/conversation if not yet verified
            conv_id = str((parsed_env or {}).get("conversation_id") or "")
            if conv_id and not timed_out and rc == 0:
                for repair_idx in range(1, 3):
                    repair_prompt = (
                        "Run ./verify_driver.sh check and ./verify_driver.sh verify right now, "
                        "read every diagnostic error or counterexample, and fix "
                        "out/odoo_o2c_mapping.json and/or out/driver.py until ./verify_driver.sh verify "
                        "exits 0 with ok=true and verdict=CORROBORATED."
                    )
                    cmd_rep = build_fde_runner_command(
                        fde_runner=runner_name,
                        cli_path=cli_path,
                        model_name=model_name,
                        prompt=repair_prompt,
                        timeout_sec=timeout_sec,
                        session_id=conv_id,
                    )
                    t_rep0 = time.monotonic()
                    rep_to = False
                    try:
                        proc_rep = subprocess.run(
                            cmd_rep,
                            cwd=str(ws_root),
                            env=agent_env,
                            text=True,
                            capture_output=True,
                            timeout=timeout_sec + 60,
                        )
                        rc_rep = proc_rep.returncode
                        stdout_rep = (proc_rep.stdout or "").strip()
                        stderr_rep = (proc_rep.stderr or "").strip()
                    except subprocess.TimeoutExpired as exc:
                        rep_to = True
                        rc_rep = 124
                        stdout_rep = (exc.stdout or "").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
                        stderr_rep = (exc.stderr or "").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
                    rep_wall = round(time.monotonic() - t_rep0, 3)

                    parsed_rep = parse_fde_runner_output(
                        fde_runner=runner_name,
                        stdout=stdout_rep,
                        wall_elapsed_sec=rep_wall,
                    )
                    out_ready_rep = (ws_root / "out" / "odoo_o2c_mapping.json").is_file() and (
                        ws_root / "out" / "driver.py"
                    ).is_file()
                    probe_rep = None
                    if out_ready_rep:
                        pv_rep = subprocess.run(
                            ["./verify_driver.sh", "verify"], cwd=str(ws_root), text=True, capture_output=True
                        )
                        try:
                            probe_rep = json.loads(pv_rep.stdout)
                        except Exception:
                            probe_rep = {"ok": False, "raw": pv_rep.stdout[:500], "stderr": pv_rep.stderr[:400]}
                        last_verify_probe = probe_rep

                    attempts.append(
                        {
                            "fde_runner": runner_name,
                            "model": model_name,
                            "repair_turn": repair_idx,
                            "returncode": rc_rep,
                            "timed_out": rep_to,
                            "agent_status": parsed_rep.get("status") if parsed_rep else None,
                            "num_turns": parsed_rep.get("num_turns") if parsed_rep else None,
                            "duration_seconds": (parsed_rep.get("duration_seconds") if parsed_rep else None) or rep_wall,
                            "tool_calls_count": parsed_rep.get("tool_calls_count") if parsed_rep else None,
                            "total_cost_usd": parsed_rep.get("total_cost_usd") if parsed_rep else None,
                            "usage": parsed_rep.get("usage") if parsed_rep else None,
                            "conversation_id": conv_id,
                            "out_ready": out_ready_rep,
                            "verify_ok": bool(probe_rep and probe_rep.get("ok") is True),
                            "verify_errors": (probe_rep or {}).get("errors", [])[:5],
                            "stderr_tail": stderr_rep[-400:],
                        }
                    )
                    if out_ready_rep and probe_rep and probe_rep.get("ok") is True:
                        chosen_model = model_name
                        agy_envelope = parsed_rep
                        break
                    if rep_to or rc_rep != 0:
                        break
                if chosen_model is not None:
                    break

        if chosen_model is None:
            partial_driver = (
                (ws_root / "out" / "driver.py").read_text("utf-8")
                if (ws_root / "out" / "driver.py").is_file()
                else None
            )
            partial_mapping = (
                (ws_root / "out" / "odoo_o2c_mapping.json").read_text("utf-8")
                if (ws_root / "out" / "odoo_o2c_mapping.json").is_file()
                else None
            )
            partial_ast = (
                audit_thin_driver_ast(
                    ws_root / "out" / "driver.py",
                    forbidden_literals=world_a_info["fingerprints"],
                    max_lines=350,
                )
                if partial_driver is not None
                else None
            )
            fail_report = {
                "ok": False,
                "fde_runner": runner_name,
                "workspace": str(ws_root),
                "world_a": world_a_info,
                "attempts": attempts,
                "last_verify_probe": last_verify_probe,
                "partial_ast_audit": partial_ast,
                "partial_driver_source": partial_driver,
                "partial_mapping_raw": partial_mapping,
                "error": f"{runner_name} CLI did not achieve CORROBORATED on Public World A",
            }
            output_json_path.write_text(json.dumps(fail_report, ensure_ascii=False, indent=2), "utf-8")
            raise RuntimeError(f"Live {runner_name} benchmark failed on Public World A: see {output_json_path}")

        # 3. Outer Referee Audit: workspace tamper + AST forbidden_literals
        _verify_readonly_integrity(ws_root, prep["readonly_hashes"])

        driver_path = ws_root / "out" / "driver.py"
        mapping_path = ws_root / "out" / "odoo_o2c_mapping.json"
        driver_source = driver_path.read_text("utf-8")
        driver_lines = len(driver_source.splitlines())
        mapping_obj = AdapterMappingSpec.model_validate(json.loads(mapping_path.read_text("utf-8")))

        ast_issues = audit_thin_driver_ast(
            driver_path,
            forbidden_literals=world_a_info["fingerprints"],
            max_lines=350,
        )
        assert ast_issues == [], f"Outer AST cage rejected agy driver.py: {ast_issues}"

        # Final Public World A verification output
        reset_srv.reset_now()
        pub_proc = subprocess.run(["./verify_driver.sh", "verify"], cwd=str(ws_root), text=True, capture_output=True)
        world_a_verify = json.loads(pub_proc.stdout)
        assert world_a_verify["ok"] and world_a_verify["verdict"] == "CORROBORATED", world_a_verify

        # 4. Blind Hidden World B (created ONLY AFTER agy exited, shifted PKs, randomized entities/amounts)
        hid_tag = f"HID{uuid.uuid4().hex[:5].upper()}"
        rnd = random.SystemRandom()
        p1_amt = round(rnd.uniform(2100.0, 4800.0), 2)
        p2_amt = round(rnd.uniform(1700.0, 5200.0), 2)
        p3_amt = round(rnd.uniform(2400.0, 6100.0), 2)
        p4_amt = round(rnd.uniform(2900.0, 7400.0), 2)

        admin.clone_db(SOURCE_DEMO_DB, hid_tpl_db)
        world_b_info = _clean_and_seed_o2c_world(
            admin,
            hid_tpl_db,
            tag=hid_tag,
            customer_a={
                "name": f"Borealis Cryogenics {hid_tag}",
                "ref": f"BLD-PA-{hid_tag}",
                "tax_id": f"TAX-{hid_tag}-A",
                "credit_limit": 750000.0,
                "country_code": "CN",
            },
            customer_b={
                "name": f"Solstice Bionics {hid_tag}",
                "ref": f"BLD-PB-{hid_tag}",
                "tax_id": f"TAX-{hid_tag}-B",
                "credit_limit": 680000.0,
                "country_code": "CN",
            },
            order_specs=[
                {"stage": "draft", "qty": 3, "unit_price": p1_amt, "client_ref": f"BLD-R1-{hid_tag}"},
                {"stage": "confirmed", "qty": 2, "unit_price": p2_amt, "client_ref": f"BLD-R2-{hid_tag}"},
                {"stage": "draft_invoice", "qty": 4, "unit_price": p3_amt, "client_ref": f"BLD-R3-{hid_tag}"},
                {"stage": "posted_invoice", "qty": 2, "unit_price": p4_amt, "client_ref": f"BLD-R4-{hid_tag}"},
            ],
            shift_pks=rnd.randint(5, 11),
        )
        admin.clone_db(hid_tpl_db, hid_live_db)

        def _reset_hidden_world_b(_ep: Mapping[str, Any]) -> None:
            admin.drop_db(hid_live_db)
            admin.clone_db(hid_tpl_db, hid_live_db)

        order_em = mapping_obj.get_entity_mapping("O2cOrder")
        order_prefix = order_em.id_transform.prefix if order_em is not None else "ORD-"

        def _oob_tamper_hidden_odoo(ep: Mapping[str, Any], m0_before: Sequence[dict[str, Any]]) -> dict[str, Any]:
            draft_order = next(
                e for e in m0_before if e["object_type"] == "O2cOrder" and e["data"]["status"] == "draft"
            )
            canonical_oid = str(draft_order["object_id"])
            stripped = (
                canonical_oid[len(order_prefix) :]
                if order_prefix and canonical_oid.startswith(order_prefix)
                else canonical_oid
            )
            if stripped.isdigit():
                raw_id = int(stripped)
            else:
                m = re.search(r"(\d+)$", stripped)
                raw_id = int(m.group(1)) if m else int(world_b_info["orders"][0]["sale_order_id"])

            kw_b = admin.login_kw(str(ep["db"]))
            kw_b("sale.order", "action_cancel", [[raw_id]])
            row = kw_b("sale.order", "read", [[raw_id], ["state"]])[0]
            assert row["state"] == "cancel", f"Expected OOB cancel to set sale.order state='cancel', got {row}"
            return {
                "action": "confirm_o2c_order",
                "params": {"order_id": canonical_oid},
                "actor_role": "sales_rep",
            }

        hidden_ep = {
            "url": ODOO_URL,
            "db": hid_live_db,
            "username": ODOO_USER,
            "password": ODOO_PASSWORD,
        }
        hidden_verify = verify_thin_driver_on_hidden_world(
            blueprints=[STD_O2C_BP_ARG],
            seed=BUSINESS_SEED_ARG,
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config=hidden_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id="device_erp",
            device_kind="jsonrpc_erp",
            owned_object_types=O2C_OWNED_TYPES,
            handled_actions=O2C_HANDLED_ACTIONS,
            reset_fn=_reset_hidden_world_b,
            oob_tamper_check=_oob_tamper_hidden_odoo,
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=2,
        )
        oob_ok = (
            hidden_verify.get("oob_tamper_result") is not None
            and hidden_verify["oob_tamper_result"].get("passed") is True
            and hidden_verify["oob_tamper_result"].get("caught_error_code") == "DEVICE_STATE_MISMATCH"
            and hidden_verify["oob_tamper_result"].get("abox_unchanged") is True
        )
        overall_ok = bool(hidden_verify["ok"] and hidden_verify["verdict"] == "CORROBORATED" and oob_ok)

        report: dict[str, Any] = {
            "ok": overall_ok,
            "fde_runner": runner_name,
            "chosen_model": chosen_model,
            "driver_line_count": driver_lines,
            "ast_audit_errors": ast_issues,
            "readonly_tamper_check_passed": True,
            "attempts": attempts,
            "agy_envelope": agy_envelope,
            "agent_envelope": agy_envelope,
            "world_a": world_a_info,
            "world_a_verify": world_a_verify,
            "world_b_hidden": {
                k: v for k, v in world_b_info.items() if k != "fingerprints"
            },
            "hidden_world_b_verify": hidden_verify,
            "generated_mapping": mapping_obj.model_dump(mode="json"),
            "generated_driver_source": driver_source,
        }
    finally:
        if reset_srv is not None:
            reset_srv.stop()
        final_dbs = admin.drop_all_created()
        shutil.rmtree(ws_root, ignore_errors=True)

    assert final_dbs == ["erp_north", "erp_south", "odoo_demo"], (
        f"Odoo DB cleanup invariant violated: remaining={final_dbs}"
    )
    report["final_odoo_dbs"] = final_dbs
    report["remaining_wo14_pg_dbs"] = []
    output_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    assert overall_ok, f"Hidden World B verification failed (see {output_json_path}): {hidden_verify}"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live Odoo 19 pluggable AI-FDE protocol benchmark")
    parser.add_argument(
        "--fde-runner",
        choices=("agy", "opencode"),
        default="agy",
        help="Headless coding agent CLI runner to benchmark",
    )
    parser.add_argument("--model", default=None, help="Primary model to try first")
    parser.add_argument("--timeout-sec", type=int, default=480)
    parser.add_argument("--output", default="/tmp/wo14_live_odoo_agy_fde.json")
    args = parser.parse_args()

    defaults = DEFAULT_OPENCODE_DEEPSEEK_MODELS if args.fde_runner == "opencode" else DEFAULT_AGY_MODELS
    if args.model:
        models = tuple(dict.fromkeys((args.model, *defaults)))
    else:
        models = defaults

    res = run_live_odoo_agy_fde_benchmark(
        fde_runner=args.fde_runner,
        models=models,
        timeout_sec=args.timeout_sec,
        output_json_path=Path(args.output),
    )
    print(
        json.dumps(
            {
                "ok": res["ok"],
                "fde_runner": res["fde_runner"],
                "chosen_model": res["chosen_model"],
                "driver_line_count": res["driver_line_count"],
                "world_a_verdict": res["world_a_verify"]["verdict"],
                "hidden_world_b_verdict": res["hidden_world_b_verify"]["verdict"],
                "hidden_b_action_cov": res["hidden_world_b_verify"]["falsification"]["action_coverage_ratio"],
                "hidden_b_transition_cov": res["hidden_world_b_verify"]["falsification"]["transition_coverage_ratio"],
                "oob_tamper_result": res["hidden_world_b_verify"]["oob_tamper_result"],
                "final_odoo_dbs": res["final_odoo_dbs"],
                "output_path": args.output,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
