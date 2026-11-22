"""WO-24C Blind AI-FDE Orchestrator for Live Dual-ERP Benchmarks (Odoo 19 + jshERP).

Architecture & Anti-Cheat Guarantees:
1. Executes real blind AI-FDE decompilation across 6 domain targets:
   - Odoo 19 (:8069): ``o2c`` (``std_o2c``), ``p2p`` (``std_p2p``), ``r2r`` (``std_r2r``)
   - jshERP (:19999): ``p2p`` (``std_p2p``), ``o2c`` (``std_o2c``), ``h2r`` (``std_h2r``)
2. Dual-Engine Blind AI-FDE Strategy (Mandatory):
   - Always runs ``opencode`` (``openrouter/deepseek/deepseek-v4.1-flash``) FIRST in an
     isolated read-only workspace (``0444`` ABI/endpoint/requirements/SKILL/.judge,
     ``0555`` ``verify_driver.sh``, writable ``out/`` only) with blocking PATH shims
     (``docker``, ``psql``, ``mysql``, ``mysqldump``, ``redis-cli``, ``javap``) and
     an ephemeral localhost reset HTTP server.
   - Only accepts an attempt when ALL 4 independent referee gates pass:
     (a) zero read-only workspace SHA-256 tamper;
     (b) ``audit_thin_driver_ast(..., forbidden_literals=world_a_fingerprints, max_lines=350) == []``;
     (c) Public World A ``verify_thin_driver_on_hidden_world`` == ``CORROBORATED`` (100% target actions);
     (d) Freshly provisioned, PK-shifted Hidden World B (created ONLY AFTER FDE exit)
         ``verify_thin_driver_on_hidden_world`` == ``CORROBORATED`` (100% target actions) +
         out-of-band external tamper produces ``DEVICE_STATE_MISMATCH`` (``REJECTED``,
         ``abox_unchanged=True``).
   - If DeepSeek fails ANY gate, automatically creates a fresh clean workspace and
     invokes ``agy`` CLI (trying ``gemini-4-neon-medium`` -> ``gemini-4-argon-medium``)
     through the identical 4-gate referee cage.
3. Byte-Exact Persistence:
   - Copies winning ``out/driver.py`` and ``out/mapping.json`` byte-for-byte into
     ``examples/adapters/<erp>/<domain>/{driver.py, mapping.json}`` and records exact
     SHA-256 hashes + complete telemetry in ``/tmp/wo24c_odoo19_fde_report.json`` and
     ``/tmp/wo24c_jsherp_fde_report.json``.
4. Zero-Residue Cleanup (``try / finally``):
   - Drops all ``wo24c_*`` Odoo DBs so ``localhost:8069`` returns strictly to
     ``['erp_north', 'erp_south', 'odoo_demo']``.
   - Purges all ``wo24c_*`` jshERP tenant rows from MySQL & Redis while preserving
     built-in tenant ``63`` and keeping all containers healthy.
"""

from __future__ import annotations

import argparse
import copy
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.std_h2r_ontology import build_std_h2r_blueprint  # noqa: E402
from examples.std_o2c_ontology import build_std_o2c_blueprint  # noqa: E402
from examples.std_p2p_ontology import build_std_p2p_blueprint  # noqa: E402
from examples.std_r2r_ontology import build_std_r2r_blueprint  # noqa: E402
from open_ontology.design import audit_thin_driver_ast, verify_thin_driver_on_hidden_world  # noqa: E402
from open_ontology.induction import (  # noqa: E402
    AdapterMappingSpec,
    DifferentialFalsifier,
    FalsificationCase,
    compile_blueprint,
)
from open_ontology.settlement import SourceBridge, call_observe  # noqa: E402
from open_ontology.spec import Actor  # noqa: E402

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

JSHERP_BASE_URL = os.environ.get("JSHERP_BASE_URL", "http://127.0.0.1:19999/jshERP-boot").rstrip("/")
SKILL_MD_PATH = ROOT / "skills" / "openontology-fde" / "SKILL.md"
BUSINESS_SEED_ARG = "examples.business_ontology:BUSINESS_SEED_ONTOLOGY"

PRIMARY_OPENCODE_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
FALLBACK_AGY_MODELS: tuple[str, ...] = (
    "gemini-4-neon-medium",
    "gemini-4-argon-medium",
)

ODOO_DOMAINS: tuple[str, ...] = ("o2c", "p2p", "r2r")
JSHERP_DOMAINS: tuple[str, ...] = ("p2p", "o2c", "h2r")

_ODOO_FORBIDDEN_SPOILERS: tuple[str, ...] = (
    "sale.order",
    "purchase.order",
    "stock.picking",
    "stock.move",
    "account.move",
    "account.account",
    "account.journal",
    "res.partner",
    "res.company",
    "account.payment.register",
    "account.move.reversal",
    "action_confirm",
    "button_confirm",
    "button_validate",
    "action_post",
    "action_create_payments",
    "execute_kw",
    "client_order_ref",
    "move_type",
    "invoice_origin",
    "supplier_rank",
    "customer_rank",
    "in_invoice",
    "out_invoice",
    "reversed_entry_id",
)

_JSHERP_FORBIDDEN_SPOILERS: tuple[str, ...] = (
    "depothead",
    "depotitem",
    "accounthead",
    "accountitem",
    "systemconfig",
    "inoutitem",
    "materialextend",
    "adddepotheadanddetail",
    "updatedepotheadanddetail",
    "addaccountheadanddetail",
    "updateaccountheadanddetail",
    "batchsetstatus",
    "getdetaillist",
    "getdetailbynumber",
    "getcurrentinfo",
    "getbilllistbylinknumber",
    "organization",
    "orgabr",
    "orgno",
    "orgauserrel",
    "orgaid",
    "adduser",
    "updateuser",
    "subtype",
    "linknumber",
    "organid",
    "opernumber",
    "discountlastmoney",
    "changeamount",
    "lastdebt",
    "needdebt",
    "finishdebt",
    "eachamount",
    "billno",
    "billnumber",
    "purchasestatus",
    "deleteflag",
    "采购订单",
    "采购入库",
    "采购退货",
    "销售订单",
    "销售出库",
    "收款",
    "付款",
    "单据",
    "财务单据",
    "部门",
    "jsherp",
    "jishenghua",
    "管伊佳",
    "华夏",
)

_JSHERP_MUTABLE_TABLES: tuple[str, ...] = (
    "jsh_system_config",
    "jsh_depot",
    "jsh_account",
    "jsh_in_out_item",
    "jsh_supplier",
    "jsh_material_category",
    "jsh_unit",
    "jsh_person",
    "jsh_material",
    "jsh_material_extend",
    "jsh_material_initial_stock",
    "jsh_material_current_stock",
    "jsh_depot_head",
    "jsh_depot_item",
    "jsh_account_head",
    "jsh_account_item",
    "jsh_organization",
    "jsh_orga_user_rel",
    "jsh_role",
    "jsh_user_business",
)


@dataclass(frozen=True)
class DomainTargetSpec:
    erp: str
    domain: str
    blueprint_arg: str
    blueprint_factory: Callable[[], Any]
    device_id: str
    device_kind: str
    owned_object_types: tuple[str, ...]
    shared_object_types: tuple[str, ...]
    handled_actions: tuple[str, ...]
    min_auto_action_coverage: float = 1.0


DOMAIN_SPECS: dict[tuple[str, str], DomainTargetSpec] = {
    ("odoo19", "o2c"): DomainTargetSpec(
        erp="odoo19",
        domain="o2c",
        blueprint_arg="examples.std_o2c_ontology:build_std_o2c_blueprint",
        blueprint_factory=build_std_o2c_blueprint,
        device_id="odoo19_o2c",
        device_kind="jsonrpc_erp",
        owned_object_types=(
            "AccountingEntity",
            "BusinessParty",
            "O2cCustomerProfile",
            "O2cOrder",
            "O2cDelivery",
            "O2cInvoice",
        ),
        shared_object_types=(),
        handled_actions=(
            "confirm_o2c_order",
            "complete_o2c_delivery",
            "post_o2c_invoice",
            "record_o2c_payment",
        ),
    ),
    ("odoo19", "p2p"): DomainTargetSpec(
        erp="odoo19",
        domain="p2p",
        blueprint_arg="examples.std_p2p_ontology:build_std_p2p_blueprint",
        blueprint_factory=build_std_p2p_blueprint,
        device_id="odoo19_p2p",
        device_kind="jsonrpc_erp",
        owned_object_types=(
            "AccountingEntity",
            "BusinessParty",
            "P2pSupplierProfile",
            "P2pPurchaseOrder",
            "P2pGoodsReceipt",
            "P2pVendorBill",
        ),
        shared_object_types=(),
        handled_actions=(
            "confirm_p2p_order",
            "record_p2p_receipt",
            "post_p2p_bill",
            "record_p2p_payment",
        ),
    ),
    ("odoo19", "r2r"): DomainTargetSpec(
        erp="odoo19",
        domain="r2r",
        blueprint_arg="examples.std_r2r_ontology:build_std_r2r_blueprint",
        blueprint_factory=build_std_r2r_blueprint,
        device_id="odoo19_r2r",
        device_kind="jsonrpc_erp",
        owned_object_types=(
            "AccountingEntity",
            "FiscalPeriod",
            "GlAccount",
            "JournalEntry",
        ),
        shared_object_types=(),
        handled_actions=(
            "post_journal_entry",
            "reverse_journal_entry",
        ),
    ),
    ("jsherp", "p2p"): DomainTargetSpec(
        erp="jsherp",
        domain="p2p",
        blueprint_arg="examples.std_p2p_ontology:build_std_p2p_blueprint",
        blueprint_factory=build_std_p2p_blueprint,
        device_id="jsherp_p2p",
        device_kind="rest_erp",
        owned_object_types=(
            "AccountingEntity",
            "BusinessParty",
            "P2pSupplierProfile",
            "P2pPurchaseOrder",
            "P2pGoodsReceipt",
            "P2pVendorBill",
        ),
        shared_object_types=(),
        handled_actions=(
            "confirm_p2p_order",
            "record_p2p_receipt",
            "post_p2p_bill",
            "record_p2p_payment",
        ),
    ),
    ("jsherp", "o2c"): DomainTargetSpec(
        erp="jsherp",
        domain="o2c",
        blueprint_arg="examples.std_o2c_ontology:build_std_o2c_blueprint",
        blueprint_factory=build_std_o2c_blueprint,
        device_id="jsherp_o2c",
        device_kind="rest_erp",
        owned_object_types=(
            "AccountingEntity",
            "BusinessParty",
            "O2cCustomerProfile",
            "O2cOrder",
            "O2cDelivery",
            "O2cInvoice",
        ),
        shared_object_types=(),
        handled_actions=(
            "confirm_o2c_order",
            "complete_o2c_delivery",
            "post_o2c_invoice",
            "record_o2c_payment",
        ),
    ),
    ("jsherp", "h2r"): DomainTargetSpec(
        erp="jsherp",
        domain="h2r",
        blueprint_arg="examples.std_h2r_ontology:build_std_h2r_blueprint",
        blueprint_factory=build_std_h2r_blueprint,
        device_id="jsherp_h2r",
        device_kind="rest_erp",
        owned_object_types=(
            "AccountingEntity",
            "Department",
            "JobPosition",
            "Employee",
        ),
        shared_object_types=(),
        handled_actions=(
            "reparent_department",
            "assign_employee_position",
            "offboard_employee",
        ),
        min_auto_action_coverage=0.66,
    ),
}


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_local_no_proxy() -> None:
    for env_key in ("NO_PROXY", "no_proxy"):
        items = [x.strip() for x in os.environ.get(env_key, "").split(",") if x.strip()]
        for host in ("127.0.0.1", "localhost", "::1"):
            if host not in items:
                items.append(host)
        os.environ[env_key] = ",".join(items)


def _read_secret_readonly(key_name: str) -> str:
    val = os.environ.get(key_name, "").strip()
    if val:
        return val
    user_env = Path.home() / ".hermes" / ".env"
    if not user_env.is_file():
        return ""
    for line in user_env.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        if k.strip() == key_name:
            return v.strip().strip('"').strip("'")
    return ""


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


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    token: str | None = None,
    timeout: int = 20,
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Access-Token"] = token
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {"raw": raw}
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed


# ---------------------------------------------------------------------------
# Odoo 19 Referee Controller + Domain Seeders (wo24c_* only)
# ---------------------------------------------------------------------------


class Odoo24cAdminClient:
    """Referee-only Odoo 19 JSON-RPC + scoped Postgres cleanup controller."""

    def __init__(self, url: str = ODOO_URL) -> None:
        self.url = url
        self.created_dbs: list[str] = []
        self.master_password = self._resolve_master_password()

    @staticmethod
    def _assert_safe_wo24c_name(db_name: str) -> None:
        if not re.fullmatch(r"wo24c_[a-z0-9_]+", db_name):
            raise ValueError(f"Refusing non-wo24c database name {db_name!r}")

    def db_exist(self, db_name: str) -> bool:
        return bool(_rpc(self.url, "db", "db_exist", [db_name], timeout=15))

    def list_wo24c_pg_dbs(self) -> list[str]:
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
                "SELECT datname FROM pg_database WHERE datname LIKE 'wo24c_%' ORDER BY datname;",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _resolve_master_password(self) -> str:
        probe_db = f"wo24c_authcheck_{uuid.uuid4().hex[:6]}"
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
        self._assert_safe_wo24c_name(target_db)
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
        self._assert_safe_wo24c_name(db_name)
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

    def drop_all_wo24c_dbs(self) -> list[str]:
        candidates = sorted(set(self.created_dbs) | set(self.list_wo24c_pg_dbs()))
        for db in candidates:
            if db.startswith("wo24c_"):
                self.drop_db(db)
        remaining = self.list_wo24c_pg_dbs()
        if remaining:
            raise RuntimeError(f"Residual wo24c_* databases remain in Postgres: {remaining}")
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


def _strip_odoo_demo_noise(kw: Any) -> None:
    """Remove pre-existing demo sales/purchase orders, pickings, and moves cleanly."""
    old_so = kw("sale.order", "search", [[]])
    if old_so:
        kw("sale.order", "write", [old_so, {"locked": False}])
        non_cancel = kw("sale.order", "search", [[("state", "!=", "cancel")]])
        if non_cancel:
            kw("sale.order", "action_cancel", [non_cancel])
        for oid in old_so:
            try:
                kw("sale.order", "unlink", [[oid]])
            except Exception:
                pass

    old_po = kw("purchase.order", "search", [[]])
    for pid in old_po:
        try:
            st = kw("purchase.order", "read", [[pid], ["state"]])[0]["state"]
            if st != "cancel":
                kw("purchase.order", "button_cancel", [[pid]])
            kw("purchase.order", "unlink", [[pid]])
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

    for mv_id in kw("account.move", "search", [[]]):
        try:
            st = kw("account.move", "read", [[mv_id], ["state"]])[0]["state"]
            if st == "posted":
                kw("account.move", "button_draft", [[mv_id]])
                st = "draft"
            if st == "draft":
                kw("account.move", "button_cancel", [[mv_id]])
            kw("account.move", "unlink", [[mv_id]])
        except Exception:
            pass


def seed_odoo_o2c_world(
    admin: Odoo24cAdminClient,
    db_name: str,
    *,
    tag: str,
    customer_a: dict[str, Any],
    customer_b: dict[str, Any],
    order_specs: Sequence[dict[str, Any]],
    shift_pks: int = 0,
) -> dict[str, Any]:
    kw = admin.login_kw(db_name)
    _strip_odoo_demo_noise(kw)
    ranked_old = kw("res.partner", "search", [[("customer_rank", ">", 0)]])
    if ranked_old:
        kw("res.partner", "write", [ranked_old, {"customer_rank": 0}])

    if shift_pks > 0:
        for idx in range(shift_pks):
            tp = kw("res.partner", "create", [{"name": f"__seq_p_{idx}", "company_id": 1}])
            tpr = kw("product.product", "create", [{"name": f"__seq_pr_{idx}", "type": "consu"}])
            to = kw(
                "sale.order",
                "create",
                [{"partner_id": tp, "order_line": [(0, 0, {"product_id": tpr, "product_uom_qty": 1, "price_unit": 10.0})]}],
            )
            kw("sale.order", "unlink", [[to]])
            kw("product.product", "unlink", [[tpr]])
            kw("res.partner", "unlink", [[tp]])

    countries = kw("res.country", "search", [[("code", "=", customer_a.get("country_code", "CN"))]], {"limit": 1})
    country_id = int(countries[0]) if countries else False

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

    p_a = _mk_partner(customer_a)
    p_b = _mk_partner(customer_b)
    pids = [p_a, p_b]

    prod_id = int(
        kw(
            "product.product",
            "create",
            [
                {
                    "name": f"O2C Standard Hardware {tag}",
                    "default_code": f"O2C-HW-{tag}",
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
        pid = pids[idx % len(pids)]
        stage = ospec["stage"]
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
                                    "name": f"{tag} Line {idx + 1}",
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
            picking_id = int(so_row["picking_ids"][0])

        if stage in ("draft_invoice", "posted_invoice"):
            kw("stock.picking", "action_assign", [[picking_id]])
            mvs = kw("stock.move", "search_read", [[("picking_id", "=", picking_id)]], {"fields": ["id", "product_uom_qty"]})
            for mv in mvs:
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
                            "invoice_date": "2026-11-22",
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
        "partners": [{"id": p_a, **customer_a}, {"id": p_b, **customer_b}],
        "orders": seeded_orders,
        "fingerprints": [str(x) for x in fingerprints if x and len(str(x)) >= 4],
    }


def seed_odoo_p2p_world(
    admin: Odoo24cAdminClient,
    db_name: str,
    *,
    tag: str,
    supplier_a: dict[str, Any],
    supplier_b: dict[str, Any],
    po_specs: Sequence[dict[str, Any]],
    shift_pks: int = 0,
) -> dict[str, Any]:
    kw = admin.login_kw(db_name)
    _strip_odoo_demo_noise(kw)
    ranked_old = kw("res.partner", "search", [[("supplier_rank", ">", 0)]])
    if ranked_old:
        kw("res.partner", "write", [ranked_old, {"supplier_rank": 0}])
    kw("res.company", "write", [[1], {"po_lock": "edit", "po_double_validation": "one_step"}])

    pick_types = kw("stock.picking.type", "search", [[("code", "=", "incoming")]], {"limit": 1})
    if not pick_types:
        raise RuntimeError("No incoming stock.picking.type in Odoo DB")
    incoming_pt = int(pick_types[0])

    if shift_pks > 0:
        for idx in range(shift_pks):
            tp = kw("res.partner", "create", [{"name": f"__seq_sup_{idx}", "company_id": 1}])
            tpr = kw("product.product", "create", [{"name": f"__seq_mat_{idx}", "type": "consu", "purchase_ok": True}])
            tpo = kw(
                "purchase.order",
                "create",
                [
                    {
                        "partner_id": tp,
                        "picking_type_id": incoming_pt,
                        "order_line": [(0, 0, {"product_id": tpr, "product_qty": 1, "price_unit": 10.0, "tax_ids": [(6, 0, [])]})],
                    }
                ],
            )
            kw("purchase.order", "button_cancel", [[tpo]])
            kw("purchase.order", "unlink", [[tpo]])
            kw("product.product", "unlink", [[tpr]])
            kw("res.partner", "unlink", [[tp]])

    countries = kw("res.country", "search", [[("code", "=", supplier_a.get("country_code", "CN"))]], {"limit": 1})
    country_id = int(countries[0]) if countries else False

    def _mk_sup(spec: dict[str, Any]) -> int:
        vals: dict[str, Any] = {
            "name": spec["name"],
            "ref": spec["ref"],
            "vat": spec["tax_id"],
            "is_company": True,
            "supplier_rank": 1,
            "company_id": 1,
        }
        if country_id:
            vals["country_id"] = country_id
        return int(kw("res.partner", "create", [vals]))

    s_a = _mk_sup(supplier_a)
    s_b = _mk_sup(supplier_b)
    sids = [s_a, s_b]

    prod_id = int(
        kw(
            "product.product",
            "create",
            [
                {
                    "name": f"P2P Industrial Alloy {tag}",
                    "default_code": f"P2P-ALLOY-{tag}",
                    "type": "consu",
                    "is_storable": True,
                    "standard_price": 500.0,
                    "purchase_ok": True,
                    "supplier_taxes_id": [(6, 0, [])],
                    "taxes_id": [(6, 0, [])],
                }
            ],
        )
    )

    seeded_pos: list[dict[str, Any]] = []
    for idx, pspec in enumerate(po_specs):
        sid = sids[idx % len(sids)]
        stage = pspec["stage"]
        qty = float(pspec["qty"])
        price = float(pspec["unit_price"])
        ref = str(pspec["ref"])

        po_id = int(
            kw(
                "purchase.order",
                "create",
                [
                    {
                        "partner_id": sid,
                        "partner_ref": ref,
                        "picking_type_id": incoming_pt,
                        "order_line": [
                            (
                                0,
                                0,
                                {
                                    "product_id": prod_id,
                                    "name": f"{tag} Purchase Line {idx + 1}",
                                    "product_qty": qty,
                                    "price_unit": price,
                                    "tax_ids": [(6, 0, [])],
                                },
                            )
                        ],
                    }
                ],
            )
        )
        po_row = kw("purchase.order", "read", [[po_id], ["id", "name", "state", "amount_total", "picking_ids", "order_line"]])[0]
        picking_id = None
        bill_id = None

        if stage in ("confirmed", "draft_bill", "posted_bill"):
            kw("purchase.order", "button_confirm", [[po_id]])
            po_row = kw("purchase.order", "read", [[po_id], ["id", "name", "state", "amount_total", "picking_ids", "order_line"]])[0]
            picking_id = int(po_row["picking_ids"][0])

        if stage in ("draft_bill", "posted_bill"):
            kw("stock.picking", "action_assign", [[picking_id]])
            mvs = kw("stock.move", "search_read", [[("picking_id", "=", picking_id)]], {"fields": ["id", "product_uom_qty"]})
            for mv in mvs:
                kw("stock.move", "write", [[int(mv["id"])], {"quantity": float(mv["product_uom_qty"]), "picked": True}])
            kw("stock.picking", "button_validate", [[picking_id]])

            bill_id = int(
                kw(
                    "account.move",
                    "create",
                    [
                        {
                            "move_type": "in_invoice",
                            "partner_id": sid,
                            "invoice_date": "2026-11-22",
                            "invoice_origin": str(po_row["name"]),
                            "ref": ref,
                            "invoice_line_ids": [
                                (
                                    0,
                                    0,
                                    {
                                        "product_id": prod_id,
                                        "quantity": qty,
                                        "price_unit": price,
                                        "tax_ids": [(6, 0, [])],
                                        "purchase_line_id": int(po_row["order_line"][0]),
                                    },
                                )
                            ],
                        }
                    ],
                )
            )
            if stage == "posted_bill":
                kw("account.move", "action_post", [[bill_id]])

        seeded_pos.append(
            {
                "stage": stage,
                "po_id": po_id,
                "po_name": str(po_row["name"]),
                "ref": ref,
                "qty": qty,
                "unit_price": price,
                "amount_total": float(po_row["amount_total"]),
                "picking_id": picking_id,
                "bill_id": bill_id,
            }
        )

    fingerprints = [
        db_name,
        supplier_a["name"],
        supplier_b["name"],
        supplier_a["tax_id"],
        supplier_b["tax_id"],
        *[p["po_name"] for p in seeded_pos],
        *[p["ref"] for p in seeded_pos],
    ]
    return {
        "db_name": db_name,
        "suppliers": [{"id": s_a, **supplier_a}, {"id": s_b, **supplier_b}],
        "orders": seeded_pos,
        "fingerprints": [str(x) for x in fingerprints if x and len(str(x)) >= 4],
    }


def seed_odoo_r2r_world(
    admin: Odoo24cAdminClient,
    db_name: str,
    *,
    tag: str,
    draft_small_amount: float,
    draft_large_amount: float,
    posted_amount: float,
    shift_pks: int = 0,
) -> dict[str, Any]:
    kw = admin.login_kw(db_name)
    _strip_odoo_demo_noise(kw)
    kw("res.company", "write", [[1], {"fiscalyear_lock_date": False, "hard_lock_date": False}])

    if shift_pks > 0:
        j_any = int(kw("account.journal", "search", [[("type", "=", "general"), ("company_id", "=", 1)]], {"limit": 1})[0])
        a_ids = kw("account.account", "search", [[("active", "=", True)]], {"limit": 2})
        for idx in range(shift_pks):
            tm = kw(
                "account.move",
                "create",
                [
                    {
                        "move_type": "entry",
                        "journal_id": j_any,
                        "date": "2026-11-10",
                        "ref": f"__seq_mv_{idx}",
                        "line_ids": [
                            (0, 0, {"account_id": int(a_ids[0]), "name": "s1", "debit": 10.0, "credit": 0.0}),
                            (0, 0, {"account_id": int(a_ids[1]), "name": "s2", "debit": 0.0, "credit": 10.0}),
                        ],
                    }
                ],
            )
            kw("account.move", "unlink", [[tm]])

    # Archive existing accounts cleanly so observe() emits a clean deterministic chart of accounts
    old_accs = kw("account.account", "search", [[("active", "=", True)]])
    if old_accs:
        try:
            kw("account.account", "write", [old_accs, {"active": False}])
        except Exception:
            pass

    code_prefix = str(abs(hash(tag)) % 40 + 50)
    acc_specs = [
        ("cash", f"{code_prefix}10", f"Cash Treasury {tag}", "asset_cash"),
        ("ar", f"{code_prefix}20", f"Accounts Receivable {tag}", "asset_receivable"),
        ("ap", f"{code_prefix}30", f"Accounts Payable {tag}", "liability_payable"),
        ("eq", f"{code_prefix}40", f"Paid-In Equity {tag}", "equity"),
        ("rev", f"{code_prefix}50", f"Operating Revenue {tag}", "income"),
        ("exp", f"{code_prefix}60", f"Operating Expense {tag}", "expense"),
    ]
    accounts: dict[str, dict[str, Any]] = {}
    for key, code, name, atype in acc_specs:
        aid = int(
            kw(
                "account.account",
                "create",
                [{"code": code, "name": name, "account_type": atype, "active": True, "company_ids": [(6, 0, [1])]}],
            )
        )
        accounts[key] = {"id": aid, "code": code, "name": name, "account_type": atype}

    j_misc = int(kw("account.journal", "search", [[("type", "=", "general"), ("company_id", "=", 1)]], {"limit": 1})[0])

    def _mk_je(ref: str, debit_key: str, credit_key: str, amt: float, memo: str, *, post: bool) -> dict[str, Any]:
        mid = int(
            kw(
                "account.move",
                "create",
                [
                    {
                        "move_type": "entry",
                        "journal_id": j_misc,
                        "date": "2026-11-22",
                        "ref": ref,
                        "line_ids": [
                            (0, 0, {"account_id": accounts[debit_key]["id"], "name": memo, "debit": amt, "credit": 0.0}),
                            (0, 0, {"account_id": accounts[credit_key]["id"], "name": memo, "debit": 0.0, "credit": amt}),
                        ],
                    }
                ],
            )
        )
        if post:
            kw("account.move", "action_post", [[mid]])
        row = kw("account.move", "read", [[mid], ["id", "name", "ref", "state", "amount_total"]])[0]
        return {"move_id": mid, "name": str(row["name"]), "ref": ref, "state": str(row["state"]), "amount": amt}

    je_draft_small = _mk_je(f"JE-SMALL-{tag}", "exp", "cash", draft_small_amount, f"Routine expense {tag}", post=False)
    je_draft_large = _mk_je(f"JE-LARGE-{tag}", "ar", "rev", draft_large_amount, f"High-value accrual {tag}", post=False)
    je_posted = _mk_je(f"JE-POSTED-{tag}", "cash", "rev", posted_amount, f"Recognized settlement {tag}", post=True)

    fingerprints = [
        db_name,
        *(a["name"] for a in accounts.values()),
        *(a["code"] for a in accounts.values()),
        je_draft_small["ref"],
        je_draft_large["ref"],
        je_posted["ref"],
    ]
    return {
        "db_name": db_name,
        "accounts": accounts,
        "entries": [je_draft_small, je_draft_large, je_posted],
        "fingerprints": [str(x) for x in fingerprints if x and len(str(x)) >= 4],
    }


# ---------------------------------------------------------------------------
# jshERP Referee Controller + Domain Seeders (wo24c_* only)
# ---------------------------------------------------------------------------


class JshErp24cAdminClient:
    """Referee-only jshERP HTTP + tenant-scoped MySQL/Redis isolation controller."""

    def __init__(self, base_url: str = JSHERP_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.created_tenants: list[dict[str, Any]] = []
        self._snapshots: dict[int, tuple[str, str]] = {}

    @staticmethod
    def _assert_safe_wo24c_name(login_name: str) -> None:
        if not re.fullmatch(r"wo24c_[a-z0-9_]+", login_name):
            raise ValueError(f"Refusing non-wo24c tenant login_name {login_name!r}")

    @staticmethod
    def _assert_safe_tenant_id(tenant_id: int) -> None:
        if int(tenant_id) < 100:
            raise ValueError(f"Refusing to touch non-test tenant_id={tenant_id}")

    @staticmethod
    def _mysql_exec(sql: str, *, stdin_sql: str | None = None) -> str:
        cmd = [
            "docker",
            "exec",
            *(["-i"] if stdin_sql is not None else []),
            "-e",
            "MYSQL_PWD=123456",
            "jsherp-mysql",
            "mysql",
            "-uroot",
            "-N",
            "-B",
            "--default-character-set=utf8mb4",
            "jsh_erp",
        ]
        if stdin_sql is None:
            cmd.extend(["-e", sql])
        proc = subprocess.run(cmd, input=stdin_sql, text=True, capture_output=True, check=True)
        return proc.stdout

    def clear_tenant_logs(self, tenant_id: int) -> None:
        self._assert_safe_tenant_id(tenant_id)
        self._mysql_exec(f"DELETE FROM jsh_log WHERE tenant_id = {int(tenant_id)};")

    def login(self, login_name: str, password_md5: str) -> tuple[str, int]:
        st, resp = _http_json(
            f"{self.base_url}/user/login",
            method="POST",
            body={"loginName": login_name, "password": password_md5},
        )
        if st != 200 or resp.get("code") != 200:
            raise RuntimeError(f"jshERP login failed for {login_name!r}: {st} {resp}")
        data = resp.get("data") or {}
        if data.get("msgTip") != "user can login" or not data.get("token"):
            raise RuntimeError(f"jshERP unexpected login payload for {login_name!r}: {resp}")
        tok = str(data["token"])
        tid = int(tok.rsplit("_", 1)[1])
        self._assert_safe_tenant_id(tid)
        return tok, tid

    def _admin_rest(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        token: str,
        tenant_id: int | None = None,
    ) -> Any:
        if tenant_id is not None and method != "GET":
            self.clear_tenant_logs(tenant_id)
        st, parsed = _http_json(f"{self.base_url}{path}", method=method, body=body, token=token)
        if st != 200:
            raise RuntimeError(f"jshERP {method} {path} returned HTTP {st}: {parsed}")
        if isinstance(parsed, dict) and parsed.get("code") not in (None, 200):
            raise RuntimeError(f"jshERP {method} {path} rejected: {parsed}")
        return parsed

    def register_tenant(self, login_name: str, password_plain: str) -> tuple[str, int, str]:
        self._assert_safe_wo24c_name(login_name)
        pw_md5 = hashlib.md5(password_plain.encode("utf-8")).hexdigest()
        st, reg = _http_json(
            f"{self.base_url}/user/registerUser",
            method="POST",
            body={"loginName": login_name, "password": pw_md5},
        )
        if st != 200 or reg.get("code") != 200:
            raise RuntimeError(f"registerUser failed for {login_name!r}: {st} {reg}")
        tok, tid = self.login(login_name, pw_md5)
        self.created_tenants.append({"tenant_id": tid, "login_name": login_name})
        return tok, tid, pw_md5

    def capture_tenant_snapshot(self, tenant_id: int) -> None:
        self._assert_safe_tenant_id(tenant_id)
        base_cmd = [
            "docker",
            "exec",
            "-e",
            "MYSQL_PWD=123456",
            "jsherp-mysql",
            "mysqldump",
            "-uroot",
            "--no-create-info",
            "--skip-triggers",
            "--complete-insert",
            "--single-transaction",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
            "--skip-add-locks",
            "--skip-disable-keys",
            "--default-character-set=utf8mb4",
        ]
        snap_main = subprocess.run(
            [*base_cmd, f"--where=tenant_id={int(tenant_id)}", "jsh_erp", *_JSHERP_MUTABLE_TABLES],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        # Dump non-root employee rows separately (never delete/reinsert root login user id == tenant_id)
        snap_users = subprocess.run(
            [*base_cmd, f"--where=tenant_id={int(tenant_id)} AND id<>{int(tenant_id)}", "jsh_erp", "jsh_user"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self._snapshots[int(tenant_id)] = (snap_main, snap_users)

    def restore_tenant_snapshot(self, tenant_id: int) -> None:
        self._assert_safe_tenant_id(tenant_id)
        pair = self._snapshots.get(int(tenant_id))
        if not pair:
            raise KeyError(f"Missing snapshot for tenant_id={tenant_id}")
        snap_main, snap_users = pair
        deletes = [
            f"DELETE FROM `{tbl}` WHERE tenant_id = {int(tenant_id)};"
            for tbl in (*reversed(_JSHERP_MUTABLE_TABLES), "jsh_log")
        ]
        deletes.append(f"DELETE FROM `jsh_user` WHERE tenant_id = {int(tenant_id)} AND id <> {int(tenant_id)};")
        script = (
            "SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n"
            + "\n".join(deletes)
            + f"\n{snap_main}\n{snap_users}\nSET FOREIGN_KEY_CHECKS=1;\n"
        )
        self._mysql_exec("", stdin_sql=script)

    def seed_p2p_tenant(
        self,
        *,
        login_name: str,
        password_plain: str,
        company_name: str,
        depot_name: str,
        account_name: str,
        account_serial: str,
        material_name: str,
        material_barcode: str,
        suppliers: Sequence[dict[str, Any]],
        flow_specs: Sequence[dict[str, Any]],
        shift_pks: int = 0,
    ) -> dict[str, Any]:
        token, tid, pw_md5 = self.register_tenant(login_name, password_plain)
        self._admin_rest("/systemConfig/add", method="POST", token=token, tenant_id=tid, body={"companyName": company_name})
        self._admin_rest("/depot/add", method="POST", token=token, tenant_id=tid, body={"name": depot_name, "type": 0, "isDefault": True, "enabled": True})
        self._admin_rest(
            "/account/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"name": account_name, "serialNo": account_serial, "initialAmount": 8000000.0, "isDefault": True, "enabled": True},
        )
        self._admin_rest("/inOutItem/add", method="POST", token=token, tenant_id=tid, body={"name": f"AP-{login_name[-8:]}", "type": "支出", "enabled": True})

        for k in range(shift_pks):
            noise = f"PKShiftSup_{login_name}_{k}"
            self._admin_rest("/supplier/add", method="POST", token=token, tenant_id=tid, body={"supplier": noise, "type": "供应商", "enabled": True})
            lst = self._admin_rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商', 'supplier': noise}, ensure_ascii=False))}",
                token=token,
            )
            for r in (lst.get("data") or {}).get("rows") or []:
                self._admin_rest(f"/supplier/delete?id={r['id']}", method="DELETE", token=token, tenant_id=tid)

        seeded_suppliers: list[dict[str, Any]] = []
        for idx, sup in enumerate(suppliers):
            self._admin_rest(
                "/supplier/add",
                method="POST",
                token=token,
                tenant_id=tid,
                body={"supplier": sup["name"], "type": "供应商", "taxNum": sup["tax_id"], "description": sup.get("payment_terms", "NET30"), "enabled": True},
            )
            lst = self._admin_rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商', 'supplier': sup['name']}, ensure_ascii=False))}",
                token=token,
            )
            row = next(r for r in (lst.get("data") or {}).get("rows") or [] if r.get("supplier") == sup["name"])
            seeded_suppliers.append({"id": int(row["id"]), **sup, "index": idx})

        self._admin_rest(
            "/material/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={
                "name": material_name,
                "unit": "吨",
                "enabled": True,
                "meList": [{"barCode": material_barcode, "commodityUnit": "吨", "purchaseDecimal": 4000, "commodityDecimal": 4500, "wholesaleDecimal": 4200, "lowDecimal": 3900}],
            },
        )
        depot_id = int(self._admin_rest("/depot/list", token=token)["data"]["rows"][0]["id"])
        acct_id = int(self._admin_rest("/account/list", token=token)["data"]["rows"][0]["id"])
        mat_id = int(self._admin_rest("/material/list", token=token)["data"]["rows"][0]["id"])
        me_id = int(
            self._mysql_exec(
                f"SELECT id FROM jsh_material_extend WHERE tenant_id = {tid} AND material_id = {mat_id} AND delete_flag <> '1' LIMIT 1;"
            ).strip().splitlines()[0]
        )
        oper_time = "2026-11-22 10:30:00"
        seeded_flows: list[dict[str, Any]] = []

        for fidx, fspec in enumerate(flow_specs):
            if shift_pks > 0 and fidx == 0:
                ghost_po = f"GHOST-{login_name[-8:]}-0"
                self._admin_rest(
                    "/depotHead/addDepotHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps({"type": "其它", "subType": "采购订单", "defaultNumber": ghost_po, "number": ghost_po, "operTime": oper_time, "organId": seeded_suppliers[0]["id"], "totalPrice": 100.0, "discountLastMoney": 100.0, "status": "0"}, ensure_ascii=False),
                        "rows": json.dumps([{"id": "r_ghost", "materialId": mat_id, "materialExtendId": me_id, "barCode": material_barcode, "name": material_name, "unit": "吨", "depotId": depot_id, "operNumber": 1.0, "unitPrice": 100.0, "allPrice": 100.0}], ensure_ascii=False),
                    },
                )
                gid = int(self._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(ghost_po)}", token=token)["data"]["id"])
                self._admin_rest(f"/depotHead/delete?id={gid}", method="DELETE", token=token, tenant_id=tid)
            stage = str(fspec["stage"])
            sup = seeded_suppliers[int(fspec.get("supplier_idx", fidx % len(seeded_suppliers)))]
            qty = float(fspec["qty"])
            unit_price = float(fspec["unit_price"])
            total = round(qty * unit_price, 2)
            po_num = str(fspec["po_number"])
            rk_num = fspec.get("receipt_number")
            ah_num = fspec.get("bill_number")

            self._admin_rest(
                "/depotHead/addDepotHeadAndDetail",
                method="POST",
                token=token,
                tenant_id=tid,
                body={
                    "info": json.dumps(
                        {
                            "type": "其它",
                            "subType": "采购订单",
                            "defaultNumber": po_num,
                            "number": po_num,
                            "operTime": oper_time,
                            "organId": sup["id"],
                            "totalPrice": total,
                            "discountLastMoney": total,
                            "status": "0" if stage == "draft" else "1",
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [{"id": f"r_po_{fidx}", "materialId": mat_id, "materialExtendId": me_id, "barCode": material_barcode, "name": material_name, "unit": "吨", "depotId": depot_id, "operNumber": qty, "unitPrice": unit_price, "allPrice": total, "taxRate": 0, "taxMoney": 0, "taxLastMoney": total}],
                        ensure_ascii=False,
                    ),
                },
            )
            po_id = int(self._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(po_num)}", token=token)["data"]["id"])

            rk_id: int | None = None
            if stage in ("pending_receipt", "draft_bill", "posted_bill"):
                self._admin_rest(
                    "/depotHead/addDepotHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps(
                            {
                                "type": "入库",
                                "subType": "采购",
                                "defaultNumber": rk_num,
                                "number": rk_num,
                                "operTime": oper_time,
                                "organId": sup["id"],
                                "accountId": acct_id,
                                "linkNumber": po_num,
                                "totalPrice": total,
                                "discountLastMoney": total,
                                "changeAmount": 0.0,
                                "debt": total,
                                "status": "0" if stage == "pending_receipt" else "1",
                            },
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps(
                            [{"id": f"r_rk_{fidx}", "materialId": mat_id, "materialExtendId": me_id, "barCode": material_barcode, "name": material_name, "unit": "吨", "depotId": depot_id, "operNumber": qty, "unitPrice": unit_price, "allPrice": total, "taxRate": 0, "taxMoney": 0, "taxLastMoney": total}],
                            ensure_ascii=False,
                        ),
                    },
                )
                rk_id = int(self._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(rk_num)}", token=token)["data"]["id"])

            ah_id: int | None = None
            if stage in ("draft_bill", "posted_bill"):
                self._admin_rest(
                    "/accountHead/addAccountHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps(
                            {"type": "付款", "billNo": ah_num, "billTime": oper_time, "organId": sup["id"], "accountId": acct_id, "totalPrice": -total, "changeAmount": 0.0, "status": "0" if stage == "draft_bill" else "1"},
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps([{"accountId": acct_id, "billNumber": rk_num, "needDebt": total, "finishDebt": 0.0, "eachAmount": 0.0}], ensure_ascii=False),
                    },
                )
                ah_id = int(self._admin_rest(f"/accountHead/getDetailByNumber?billNo={urllib.parse.quote(ah_num)}", token=token)["data"]["id"])

            seeded_flows.append(
                {
                    "stage": stage,
                    "supplier_id": sup["id"],
                    "po_id": po_id,
                    "po_number": po_num,
                    "qty": qty,
                    "unit_price": unit_price,
                    "amount_total": total,
                    "receipt_id": rk_id,
                    "receipt_number": rk_num,
                    "bill_id": ah_id,
                    "bill_number": ah_num,
                }
            )

        self.clear_tenant_logs(tid)
        self.capture_tenant_snapshot(tid)
        fps = [
            login_name, password_plain, pw_md5, company_name, depot_name, account_name, account_serial, material_name, material_barcode,
            *[s["name"] for s in seeded_suppliers], *[s["tax_id"] for s in seeded_suppliers],
            *[f["po_number"] for f in seeded_flows],
            *[f["receipt_number"] for f in seeded_flows if f.get("receipt_number")],
            *[f["bill_number"] for f in seeded_flows if f.get("bill_number")],
        ]
        return {
            "tenant_id": tid,
            "login_name": login_name,
            "password_plain": password_plain,
            "password_md5": pw_md5,
            "company_name": company_name,
            "depot_id": depot_id,
            "account_id": acct_id,
            "material_id": mat_id,
            "suppliers": seeded_suppliers,
            "flows": seeded_flows,
            "fingerprints": [str(x) for x in fps if x and len(str(x)) >= 4],
        }

    def seed_o2c_tenant(
        self,
        *,
        login_name: str,
        password_plain: str,
        company_name: str,
        depot_name: str,
        account_name: str,
        account_serial: str,
        material_name: str,
        material_barcode: str,
        customers: Sequence[dict[str, Any]],
        flow_specs: Sequence[dict[str, Any]],
        shift_pks: int = 0,
    ) -> dict[str, Any]:
        token, tid, pw_md5 = self.register_tenant(login_name, password_plain)
        self._admin_rest(
            "/systemConfig/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"companyName": company_name, "minusStockFlag": "1"},
        )
        self._admin_rest("/depot/add", method="POST", token=token, tenant_id=tid, body={"name": depot_name, "type": 0, "isDefault": True, "enabled": True})
        self._admin_rest(
            "/account/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"name": account_name, "serialNo": account_serial, "initialAmount": 5000000.0, "isDefault": True, "enabled": True},
        )
        self._admin_rest("/inOutItem/add", method="POST", token=token, tenant_id=tid, body={"name": f"AR-{login_name[-8:]}", "type": "收入", "enabled": True})

        for k in range(shift_pks):
            noise = f"PKShiftCust_{login_name}_{k}"
            self._admin_rest("/supplier/add", method="POST", token=token, tenant_id=tid, body={"supplier": noise, "type": "客户", "enabled": True})
            lst = self._admin_rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '客户', 'supplier': noise}, ensure_ascii=False))}",
                token=token,
            )
            for r in (lst.get("data") or {}).get("rows") or []:
                self._admin_rest(f"/supplier/delete?id={r['id']}", method="DELETE", token=token, tenant_id=tid)

        seeded_customers: list[dict[str, Any]] = []
        for idx, cust in enumerate(customers):
            self._admin_rest(
                "/supplier/add",
                method="POST",
                token=token,
                tenant_id=tid,
                body={
                    "supplier": cust["name"],
                    "type": "客户",
                    "taxNum": cust["tax_id"],
                    "advanceIn": float(cust.get("credit_limit", 250000.0)),
                    "description": cust.get("payment_terms", "NET30"),
                    "enabled": True,
                },
            )
            lst = self._admin_rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '客户', 'supplier': cust['name']}, ensure_ascii=False))}",
                token=token,
            )
            row = next(r for r in (lst.get("data") or {}).get("rows") or [] if r.get("supplier") == cust["name"])
            seeded_customers.append({"id": int(row["id"]), **cust, "index": idx})

        depot_id = int(self._admin_rest("/depot/list", token=token)["data"]["rows"][0]["id"])
        acct_id = int(self._admin_rest("/account/list", token=token)["data"]["rows"][0]["id"])
        self._admin_rest(
            "/material/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={
                "name": material_name,
                "unit": "吨",
                "enabled": True,
                "meList": [{"barCode": material_barcode, "commodityUnit": "吨", "purchaseDecimal": 100, "commodityDecimal": 4600, "wholesaleDecimal": 4400, "lowDecimal": 100}],
                "stock": [{"depotId": depot_id, "number": 10000.0}],
            },
        )
        mat_id = int(self._admin_rest("/material/list", token=token)["data"]["rows"][0]["id"])
        me_id = int(
            self._mysql_exec(
                f"SELECT id FROM jsh_material_extend WHERE tenant_id = {tid} AND material_id = {mat_id} AND delete_flag <> '1' LIMIT 1;"
            ).strip().splitlines()[0]
        )

        oper_time = "2026-11-22 11:00:00"
        seeded_flows: list[dict[str, Any]] = []
        for fidx, fspec in enumerate(flow_specs):
            if shift_pks > 0 and fidx == 0:
                ghost_so = f"GHOSTSO-{login_name[-8:]}-0"
                self._admin_rest(
                    "/depotHead/addDepotHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps({"type": "其它", "subType": "销售订单", "defaultNumber": ghost_so, "number": ghost_so, "operTime": oper_time, "organId": seeded_customers[0]["id"], "totalPrice": 100.0, "discountLastMoney": 100.0, "status": "0"}, ensure_ascii=False),
                        "rows": json.dumps([{"id": "r_ghost_so", "materialId": mat_id, "materialExtendId": me_id, "barCode": material_barcode, "name": material_name, "unit": "吨", "depotId": depot_id, "operNumber": 1.0, "unitPrice": 100.0, "allPrice": 100.0}], ensure_ascii=False),
                    },
                )
                gid = int(self._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(ghost_so)}", token=token)["data"]["id"])
                self._admin_rest(f"/depotHead/delete?id={gid}", method="DELETE", token=token, tenant_id=tid)
            stage = str(fspec["stage"])
            cust = seeded_customers[int(fspec.get("customer_idx", fidx % len(seeded_customers)))]
            qty = float(fspec["qty"])
            unit_price = float(fspec["unit_price"])
            total = round(qty * unit_price, 2)
            so_num = str(fspec["so_number"])
            ck_num = fspec.get("delivery_number")
            sk_num = fspec.get("invoice_number")

            self._admin_rest(
                "/depotHead/addDepotHeadAndDetail",
                method="POST",
                token=token,
                tenant_id=tid,
                body={
                    "info": json.dumps(
                        {"type": "其它", "subType": "销售订单", "defaultNumber": so_num, "number": so_num, "operTime": oper_time, "organId": cust["id"], "totalPrice": total, "discountLastMoney": total, "status": "0" if stage == "draft" else "1"},
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [{"id": f"r_so_{fidx}", "materialId": mat_id, "materialExtendId": me_id, "barCode": material_barcode, "name": material_name, "unit": "吨", "depotId": depot_id, "operNumber": qty, "unitPrice": unit_price, "allPrice": total, "taxRate": 0, "taxMoney": 0, "taxLastMoney": total}],
                        ensure_ascii=False,
                    ),
                },
            )
            so_id = int(self._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(so_num)}", token=token)["data"]["id"])

            ck_id: int | None = None
            if stage in ("pending_delivery", "draft_invoice", "posted_invoice"):
                self._admin_rest(
                    "/depotHead/addDepotHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps(
                            {
                                "type": "出库",
                                "subType": "销售",
                                "defaultNumber": ck_num,
                                "number": ck_num,
                                "operTime": oper_time,
                                "organId": cust["id"],
                                "accountId": acct_id,
                                "linkNumber": so_num,
                                "totalPrice": total,
                                "discountLastMoney": total,
                                "changeAmount": 0.0,
                                "debt": total,
                                "status": "0" if stage == "pending_delivery" else "1",
                            },
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps(
                            [{"id": f"r_ck_{fidx}", "materialId": mat_id, "materialExtendId": me_id, "barCode": material_barcode, "name": material_name, "unit": "吨", "depotId": depot_id, "operNumber": qty, "unitPrice": unit_price, "allPrice": total, "taxRate": 0, "taxMoney": 0, "taxLastMoney": total}],
                            ensure_ascii=False,
                        ),
                    },
                )
                ck_id = int(self._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(ck_num)}", token=token)["data"]["id"])

            sk_id: int | None = None
            if stage in ("draft_invoice", "posted_invoice"):
                self._admin_rest(
                    "/accountHead/addAccountHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps(
                            {"type": "收款", "billNo": sk_num, "billTime": oper_time, "organId": cust["id"], "accountId": acct_id, "totalPrice": total, "changeAmount": 0.0, "status": "0" if stage == "draft_invoice" else "1"},
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps([{"accountId": acct_id, "billNumber": ck_num, "needDebt": total, "finishDebt": 0.0, "eachAmount": 0.0}], ensure_ascii=False),
                    },
                )
                sk_id = int(self._admin_rest(f"/accountHead/getDetailByNumber?billNo={urllib.parse.quote(sk_num)}", token=token)["data"]["id"])

            seeded_flows.append(
                {
                    "stage": stage,
                    "customer_id": cust["id"],
                    "so_id": so_id,
                    "so_number": so_num,
                    "qty": qty,
                    "unit_price": unit_price,
                    "amount_total": total,
                    "delivery_id": ck_id,
                    "delivery_number": ck_num,
                    "invoice_id": sk_id,
                    "invoice_number": sk_num,
                }
            )

        self.clear_tenant_logs(tid)
        self.capture_tenant_snapshot(tid)
        fps = [
            login_name, password_plain, pw_md5, company_name, depot_name, account_name, account_serial, material_name, material_barcode,
            *[c["name"] for c in seeded_customers], *[c["tax_id"] for c in seeded_customers],
            *[f["so_number"] for f in seeded_flows],
            *[f["delivery_number"] for f in seeded_flows if f.get("delivery_number")],
            *[f["invoice_number"] for f in seeded_flows if f.get("invoice_number")],
        ]
        return {
            "tenant_id": tid,
            "login_name": login_name,
            "password_plain": password_plain,
            "password_md5": pw_md5,
            "company_name": company_name,
            "depot_id": depot_id,
            "account_id": acct_id,
            "material_id": mat_id,
            "customers": seeded_customers,
            "flows": seeded_flows,
            "fingerprints": [str(x) for x in fps if x and len(str(x)) >= 4],
        }

    def seed_h2r_tenant(
        self,
        *,
        login_name: str,
        password_plain: str,
        company_name: str,
        tag: str,
        shift_pks: int = 0,
    ) -> dict[str, Any]:
        token, tid, pw_md5 = self.register_tenant(login_name, password_plain)
        self._admin_rest("/systemConfig/add", method="POST", token=token, tenant_id=tid, body={"companyName": company_name})

        if shift_pks > 0:
            for k in range(shift_pks):
                self._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"GhostOrg_{k}", "orgNo": f"GH-{tag}-{k}"})
                tree = self._admin_rest("/organization/getOrganizationTree?id=", token=token)
                for node in tree:
                    if str(node.get("title", "")).startswith("GhostOrg_"):
                        self._admin_rest(f"/organization/delete?id={node['id']}", method="DELETE", token=token, tenant_id=tid)
                self._admin_rest("/role/add", method="POST", token=token, tenant_id=tid, body={"name": f"GhostRole_{k}", "type": "个人数据"})
                rl = self._admin_rest("/role/list?search=%7B%7D", token=token)["data"]["rows"]
                for r in rl:
                    if r.get("tenantId") == tid and str(r.get("name", "")).startswith("GhostRole_"):
                        self._admin_rest(f"/role/delete?id={r['id']}", method="DELETE", token=token, tenant_id=tid)

        # Create root HQ org + 2 child depts
        self._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"HQ {tag}", "orgNo": f"ORG-HQ-{tag}", "remark": "hq", "sort": "1"})
        tree1 = self._admin_rest("/organization/getOrganizationTree?id=", token=token)
        hq_id = int(next(n["id"] for n in tree1 if n.get("attributes") == f"ORG-HQ-{tag}"))

        self._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"Sales {tag}", "orgNo": f"ORG-SAL-{tag}", "parentId": hq_id, "remark": "sales", "sort": "2"})
        self._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"Ops {tag}", "orgNo": f"ORG-OPS-{tag}", "parentId": hq_id, "remark": "ops", "sort": "3"})
        # Fetch full IDs from flat org rows via MySQL/API check
        org_rows_sql = self._mysql_exec(
            f"SELECT id, org_no, org_abr, COALESCE(parent_id, ''), COALESCE(remark, '') FROM jsh_organization WHERE tenant_id = {tid} AND delete_flag = '0' ORDER BY id;"
        ).strip().splitlines()
        orgs: list[dict[str, Any]] = []
        for line in org_rows_sql:
            oid_s, ono, oabr, pid_s, rem = line.split("\t")
            orgs.append({"id": int(oid_s), "org_no": ono, "name": oabr, "parent_id": int(pid_s) if pid_s else None, "scope_tag": rem})
        hq_id = orgs[0]["id"]
        sales_org_id = orgs[1]["id"]
        ops_org_id = orgs[2]["id"]

        role_specs = [
            {"title": f"Director {tag}", "department_id": hq_id, "role_name": "finance_director", "approval_ceiling_cents": 50000000},
            {"title": f"Sales Mgr {tag}", "department_id": sales_org_id, "role_name": "sales_manager", "approval_ceiling_cents": 20000000},
            {"title": f"Ops Specialist {tag}", "department_id": ops_org_id, "role_name": "ops", "approval_ceiling_cents": 5000000},
        ]
        seeded_roles: list[dict[str, Any]] = []
        for rs in role_specs:
            meta = {"department_id": rs["department_id"], "role_name": rs["role_name"], "approval_ceiling_cents": rs["approval_ceiling_cents"]}
            self._admin_rest(
                "/role/add",
                method="POST",
                token=token,
                tenant_id=tid,
                body={"name": rs["title"], "type": "全部数据", "description": json.dumps(meta, ensure_ascii=False)},
            )
        all_roles = self._admin_rest("/role/list?search=%7B%7D", token=token)["data"]["rows"]
        for rs in role_specs:
            match = next(r for r in all_roles if r.get("tenantId") == tid and r.get("name") == rs["title"])
            seeded_roles.append({"id": int(match["id"]), **rs})

        emp_specs = [
            {"full_name": f"Alice {tag}", "login_name": f"emp_a_{tag.lower()}", "org_id": ops_org_id, "role_idx": 2, "status_label": "onboarding"},
            {"full_name": f"Bob {tag}", "login_name": f"emp_b_{tag.lower()}", "org_id": sales_org_id, "role_idx": 1, "status_label": "active"},
            {"full_name": f"Carol {tag}", "login_name": f"emp_c_{tag.lower()}", "org_id": hq_id, "role_idx": 0, "status_label": "active"},
        ]
        seeded_emps: list[dict[str, Any]] = []
        for idx, es in enumerate(emp_specs):
            rinfo = seeded_roles[es["role_idx"]]
            self._admin_rest(
                "/user/addUser",
                method="POST",
                token=token,
                tenant_id=tid,
                body={
                    "loginName": es["login_name"],
                    "username": es["full_name"],
                    "position": rinfo["title"],
                    "roleId": rinfo["id"],
                    "orgaId": es["org_id"],
                    "description": es["status_label"],
                },
            )
        ulist = self._admin_rest("/user/list?search=%7B%7D", token=token)["data"]["rows"]
        for es in emp_specs:
            match = next(u for u in ulist if u.get("loginName") == es["login_name"])
            seeded_emps.append(
                {
                    "id": int(match["id"]),
                    "login_name": es["login_name"],
                    "full_name": es["full_name"],
                    "org_id": es["org_id"],
                    "role_id": seeded_roles[es["role_idx"]]["id"],
                    "status_label": es["status_label"],
                }
            )

        self.clear_tenant_logs(tid)
        self.capture_tenant_snapshot(tid)
        fps = [
            login_name, password_plain, pw_md5, company_name,
            *(o["name"] for o in orgs), *(o["org_no"] for o in orgs),
            *(r["title"] for r in seeded_roles),
            *(e["full_name"] for e in seeded_emps), *(e["login_name"] for e in seeded_emps),
        ]
        return {
            "tenant_id": tid,
            "login_name": login_name,
            "password_plain": password_plain,
            "password_md5": pw_md5,
            "company_name": company_name,
            "orgs": orgs,
            "roles": seeded_roles,
            "employees": seeded_emps,
            "fingerprints": [str(x) for x in fps if x and len(str(x)) >= 4],
        }

    def cleanup_all_wo24c_tenants(self) -> list[int]:
        raw_tids = self._mysql_exec(
            "SELECT DISTINCT id FROM jsh_user WHERE login_name LIKE 'wo24c_%' "
            "UNION SELECT DISTINCT tenant_id FROM jsh_tenant WHERE login_name LIKE 'wo24c_%';"
        )
        tids = sorted({int(x.strip()) for x in raw_tids.splitlines() if x.strip() and int(x.strip()) >= 100})
        for meta in self.created_tenants:
            tid = int(meta["tenant_id"])
            if tid >= 100 and tid not in tids:
                tids.append(tid)
        if not tids:
            return []

        in_list = ",".join(str(tid) for tid in tids)
        tbl_out = self._mysql_exec(
            "SELECT TABLE_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA='jsh_erp' AND COLUMN_NAME='tenant_id' ORDER BY TABLE_NAME;"
        )
        stmts = ["SET FOREIGN_KEY_CHECKS=0;"]
        for tbl in [line.strip() for line in tbl_out.splitlines() if line.strip()]:
            stmts.append(f"DELETE FROM `{tbl}` WHERE tenant_id IN ({in_list});")
        stmts.append(f"DELETE FROM jsh_user WHERE id IN ({in_list}) OR tenant_id IN ({in_list});")
        stmts.append(f"DELETE FROM jsh_tenant WHERE tenant_id IN ({in_list}) OR login_name LIKE 'wo24c_%';")
        stmts.append("SET FOREIGN_KEY_CHECKS=1;")
        self._mysql_exec("", stdin_sql="\n".join(stmts) + "\n")

        tid_list = " ".join(str(t) for t in tids)
        subprocess.run(
            [
                "docker",
                "exec",
                "jsherp-redis",
                "sh",
                "-c",
                f'redis-cli --no-auth-warning -a 1234abcd --scan | while read k; do '
                f'[ "$k" = "63" ] && continue; '
                f'v=$(redis-cli --no-auth-warning -a 1234abcd GET "$k"); '
                f'for t in {tid_list}; do [ "$v" = "$t" ] && redis-cli --no-auth-warning -a 1234abcd DEL "$k"; done; done',
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        return tids

    def remaining_wo24c_counts(self) -> dict[str, int]:
        u = int(self._mysql_exec("SELECT count(*) FROM jsh_user WHERE login_name LIKE 'wo24c_%';").strip())
        t = int(self._mysql_exec("SELECT count(*) FROM jsh_tenant WHERE login_name LIKE 'wo24c_%';").strip())
        return {"jsh_user": u, "jsh_tenant": t}


# ---------------------------------------------------------------------------
# Zero-Spoiler Requirements Builders (strictly checked against spoilers)
# ---------------------------------------------------------------------------


def _build_requirements_text(target: DomainTargetSpec) -> str:
    if target.erp == "odoo19" and target.domain == "o2c":
        req = """# Autonomous Enterprise ERP Integration Specification (Blind O2C)

## Mission
Integrate the target external ERP system onto the locked OpenOntology standard contract in
`target_abi.json`. Read connection settings dynamically at runtime from `world_a_endpoint.json`
(`url`, `db`, `username`, `password`) and inspect the live system autonomously over HTTP JSON-RPC.

NEVER hardcode database names, credentials, customer names, sample IDs, document numbers, or
sample amounts in generated code. Your artifacts will be blindly falsified against an unseen
randomized tenant whose IDs, names, and amounts differ completely.

## Canonical Prefixes & Deliverables (write ONLY inside `out/`)
1. `out/mapping.json`: valid `AdapterMappingSpec` mapping all 6 entity types (`AccountingEntity`,
   `BusinessParty`, `O2cCustomerProfile`, `O2cOrder`, `O2cDelivery`, `O2cInvoice`) and all 4
   actions (`confirm_o2c_order`, `complete_o2c_delivery`, `post_o2c_invoice`, `record_o2c_payment`).
   Prefer standard prefixes `ENT-` (`AccountingEntity`) and `PTY-` (`BusinessParty`) and ensure
   every `ActionExecutionBinding.id_prefix` matches its entity mapping's `id_transform.prefix`.
2. `out/driver.py`: ultra-thin Python transport driver (`<= 350` lines) exposing
   `build_device_bridge(endpoint_config, mapping) -> SourceBridge`.

## Domain Semantics
- Derive canonical IDs dynamically from `mapping` prefixes.
- Derive each order's true end-to-end O2C lifecycle (`draft` -> `confirmed` -> `delivered` ->
  `invoiced`, or `cancelled`) by inspecting the live order alongside linked fulfillment deliveries
  and posted customer invoices.
- Filter unrelated non-customer contacts, orphaned warehouse transfers without an active order,
  and unlinked non-sales entries; preserve cancelled customer orders as genuine `"cancelled"` state
  for out-of-band conflict detection.
- NEVER fabricate state from `move_envelope["object_diff"]` inside `observe()`.
- Catch remote state-conflict RPC errors cleanly in `actuate()` so live `observe()` reports
  physical truth and triggers `DEVICE_STATE_MISMATCH` with zero ABox writes.

## Verification
Iterate with `./verify_driver.sh check` and `./verify_driver.sh verify` until exit code `0` (`verdict == "CORROBORATED"`).
"""
        spoilers = _ODOO_FORBIDDEN_SPOILERS
    elif target.erp == "odoo19" and target.domain == "p2p":
        req = """# Autonomous Enterprise ERP Integration Specification (Blind P2P)

## Mission
Integrate the target external ERP procurement workflow onto the locked OpenOntology standard contract
in `target_abi.json`. Read connection settings dynamically at runtime from `world_a_endpoint.json`
(`url`, `db`, `username`, `password`) and inspect the live system autonomously over HTTP JSON-RPC.

NEVER hardcode database names, credentials, supplier names, sample IDs, document numbers, or
sample amounts in generated code. An unseen randomized & PK-shifted tenant will be tested after exit.

## Canonical Prefixes & Deliverables (write ONLY inside `out/`)
1. `out/mapping.json`: valid `AdapterMappingSpec` mapping all 6 entity types (`AccountingEntity`,
   `BusinessParty`, `P2pSupplierProfile`, `P2pPurchaseOrder`, `P2pGoodsReceipt`, `P2pVendorBill`)
   and all 4 target actions (`confirm_p2p_order`, `record_p2p_receipt`, `post_p2p_bill`,
   `record_p2p_payment`). Prefer standard prefixes `ENT-` (`AccountingEntity`) and `PTY-` (`BusinessParty`).
2. `out/driver.py`: ultra-thin Python transport driver (`<= 350` lines) exposing
   `build_device_bridge(endpoint_config, mapping) -> SourceBridge`.

## Domain Semantics
- Inspect live purchase orders, lines (`ordered_qty`, `unit_price`, `amount_total`), linked incoming
  warehouse receipts (`P2pGoodsReceipt`), and linked vendor bills (`P2pVendorBill`, unsigned positive
  `amount_total` and `amount_residual`, `payment_state` in `{"not_paid", "partial", "paid"}`).
- Reflect the true PO lifecycle (`draft` -> `confirmed` -> `received` -> `billed`, plus `cancelled`
  if cancelled externally, or `disputed` if inspection failed / short receipt).
- In `record_p2p_receipt`, read `received_qty` and `inspection_status` from `move_envelope["params"]`:
  validate full receipts on the external warehouse picking when `received_qty >= expected_qty` and
  `inspection_status == "passed"`; record short/failed quality issues cleanly via real remote state
  or non-fabricated storage flags without echoing `move_envelope["object_diff"]`.
- `post_p2p_bill` posts the draft vendor bill on the server; `record_p2p_payment` registers vendor
  payment settlement (`payment_amount`) so server `amount_residual` and `payment_state` update genuinely.
- Filter out non-supplier contacts, non-purchase stock transfers, and unrelated accounting entries,
  while preserving genuinely `cancelled` purchase orders so out-of-band cancellation triggers
  `DEVICE_STATE_MISMATCH`.

## Verification
Iterate with `./verify_driver.sh check` and `./verify_driver.sh verify` until exit code `0` (`verdict == "CORROBORATED"`).
"""
        spoilers = _ODOO_FORBIDDEN_SPOILERS
    elif target.erp == "odoo19" and target.domain == "r2r":
        req = """# Autonomous Enterprise ERP Integration Specification (Blind R2R General Ledger)

## Mission
Integrate the target external ERP general ledger onto the locked OpenOntology `std_r2r` contract in
`target_abi.json`. Read connection settings dynamically at runtime from `world_a_endpoint.json`
(`url`, `db`, `username`, `password`) and inspect the live system autonomously over HTTP JSON-RPC.

NEVER hardcode database names, credentials, company names, account codes/names, entry IDs, or
amounts in generated code. An unseen randomized & PK-shifted tenant will be tested after exit.

## Canonical Prefixes & Deliverables (write ONLY inside `out/`)
1. `out/mapping.json`: valid `AdapterMappingSpec` mapping all 4 entity types (`AccountingEntity`,
   `FiscalPeriod`, `GlAccount`, `JournalEntry`) and both target actions (`post_journal_entry`,
   `reverse_journal_entry`). Keep `ENT-` for `AccountingEntity` and consistent prefixes matching
   `ActionExecutionBinding.id_prefix`.
2. `out/driver.py`: ultra-thin Python transport driver (`<= 350` lines) exposing
   `build_device_bridge(endpoint_config, mapping) -> SourceBridge`.

## Domain Semantics
- `AccountingEntity`: active company master (`base_currency` e.g. ISO currency code).
- `FiscalPeriod`: open/closed monthly fiscal period anchored to the accounting entity and general
  journal entry dates (`YYYY-MM` period key, `start_date`, `end_date`, `status` = `"closed"` if
  covered by a company fiscal lock date else `"open"`).
- `GlAccount`: active ledger accounts mapped to OpenOntology `category` in
  `{"ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"}` and `normal_side` (`"DEBIT"` for
  `ASSET`/`EXPENSE`, `"CREDIT"` for `LIABILITY`/`EQUITY`/`REVENUE`). Compute `debit_total_cents`
  and `credit_total_cents` (non-negative integer cents) dynamically by summing posted general-ledger
  lines per account from live ERP readback!
- `JournalEntry`: inspect general-journal entries (`draft` or `posted`; ignore cancelled entries so
  out-of-band deletion/cancellation removes the draft entry cleanly and triggers
  `DEVICE_STATE_MISMATCH`). For two-line balanced entries, extract `debit_account`, `credit_account`,
  `amount_cents` (integer `round(amount * 100)`), `memo`, and `reversal_of`. If an active posted
  reversal entry points back to an original posted entry, report the original entry's `status` as
  `"reversed"` and the reversal entry's `status` as `"posted"` with matching `reversal_of`.
- `post_journal_entry`: invoke the live ERP posting method on the draft entry; catch expected state
  conflict faults cleanly if deleted/cancelled out-of-band.
- `reverse_journal_entry`: create & post the reversal entry on the live ERP (via the ERP's reversal
  mechanism) with swapped debit/credit accounts, ensuring posted `debit_total_cents` /
  `credit_total_cents` and reversal state reflect real physical ledger rows on subsequent `observe()`.
- NEVER echo `move_envelope["object_diff"]` inside `observe()`.

## Verification
Iterate with `./verify_driver.sh check` and `./verify_driver.sh verify` until exit code `0` (`verdict == "CORROBORATED"`).
"""
        spoilers = _ODOO_FORBIDDEN_SPOILERS
    elif target.erp == "jsherp" and target.domain == "p2p":
        req = """# Autonomous Enterprise ERP Integration Specification (Blind REST P2P)

## Mission
Integrate the target external REST ERP system onto the locked OpenOntology standard contract in
`target_abi.json`. Read connection settings dynamically at runtime from `world_a_endpoint.json`
(`base_url`, `openapi_url`, `login_url`, `username`, `password`, `password_md5`, `token_header`)
and introspect the live HTTP REST service autonomously via `openapi_url` and authenticated probes.

NEVER hardcode tenant login names, credentials, company names, supplier names, IDs, document
numbers, or amounts in generated code. An unseen randomized & PK-shifted tenant will be tested.

## Transport & Rate-Limit Contract
- Authenticate via `POST login_url` using `username` and `password_md5` (or compute MD5 from
  `password` if omitted). Pass the returned auth token in HTTP header `token_header`.
- Respect the server's 1-second anti-replay check between consecutive writes (`time.sleep(1.05)`
  when needed) and automatically re-login once if an expired/replayed session returns HTTP 401/500.

## Canonical Prefixes & Deliverables (write ONLY inside `out/`)
1. `out/mapping.json`: valid `AdapterMappingSpec` mapping all 6 entity types (`AccountingEntity`,
   `BusinessParty`, `P2pSupplierProfile`, `P2pPurchaseOrder`, `P2pGoodsReceipt`, `P2pVendorBill`)
   and all 4 target actions (`confirm_p2p_order`, `record_p2p_receipt`, `post_p2p_bill`,
   `record_p2p_payment`). Prefer standard prefixes `ENT-` (`AccountingEntity`) and `PTY-` (`BusinessParty`).
2. `out/driver.py`: ultra-thin Python transport driver (`<= 350` lines) exposing
   `build_device_bridge(endpoint_config, mapping) -> SourceBridge`.

## Domain Semantics
- Reconstruct the true P2P lifecycle from live REST endpoints:
  `draft` -> `confirmed` (approved purchase order, no completed receipt yet) -> `received`
  (approved incoming receipt, no posted bill) -> `billed` (posted payable document), or `cancelled`
  if deleted/missing externally.
- Support both full receipt (`completed` / `received`) and partial/failed receipt (`disputed`),
  unsigned positive bill totals, `amount_residual = max(0.0, amount_total - paid_amount)`,
  `payment_state` in `{"not_paid", "partial", "paid"}`, and full/partial settlement in
  `record_p2p_payment`.
- Handle non-zero API business status codes cleanly and never mask missing/deleted external records
  or echo `move_envelope["object_diff"]` inside `observe()`.

## Verification
Iterate with `./verify_driver.sh check` and `./verify_driver.sh verify` until exit code `0` (`verdict == "CORROBORATED"`).
"""
        spoilers = _JSHERP_FORBIDDEN_SPOILERS
    elif target.erp == "jsherp" and target.domain == "o2c":
        req = """# Autonomous Enterprise ERP Integration Specification (Blind REST O2C)

## Mission
Integrate the target external REST ERP system onto the locked OpenOntology standard O2C contract in
`target_abi.json`. Read connection settings dynamically at runtime from `world_a_endpoint.json`
(`base_url`, `openapi_url`, `login_url`, `username`, `password`, `password_md5`, `token_header`)
and introspect the live HTTP REST service autonomously via `openapi_url` and authenticated probes.

NEVER hardcode tenant login names, credentials, company names, customer names, IDs, document
numbers, or amounts in generated code. An unseen randomized & PK-shifted tenant will be tested.

## Transport & Rate-Limit Contract
- Authenticate via `POST login_url` using `username` and `password_md5` (or compute MD5 from
  `password` if omitted). Pass the returned auth token in HTTP header `token_header`.
- Respect the server's 1-second anti-replay guard between consecutive writes (`time.sleep(1.05)` when
  needed) and re-login once if a session ever expires or returns an auth fault.

## Canonical Prefixes & Deliverables (write ONLY inside `out/`)
1. `out/mapping.json`: valid `AdapterMappingSpec` mapping all 6 entity types (`AccountingEntity`,
   `BusinessParty`, `O2cCustomerProfile`, `O2cOrder`, `O2cDelivery`, `O2cInvoice`) and all 4 target
   actions (`confirm_o2c_order`, `complete_o2c_delivery`, `post_o2c_invoice`, `record_o2c_payment`).
   Prefer standard prefixes `ENT-` (`AccountingEntity`) and `PTY-` (`BusinessParty`).
2. `out/driver.py`: ultra-thin Python transport driver (`<= 350` lines) exposing
   `build_device_bridge(endpoint_config, mapping) -> SourceBridge`.

## Domain Semantics
- Derive canonical IDs dynamically from `mapping` prefixes and link customers -> orders ->
  deliveries -> receivable invoices cleanly from live REST data.
- Reconstruct the true O2C lifecycle (`draft` -> `confirmed` -> `delivered` -> `invoiced`) from live
  approval states on orders, outbound fulfillment deliveries, and customer receivable settlement records.
- For `O2cInvoice`, map positive `amount_total`, `amount_residual = max(0.0, amount_total - collected)`,
  and `payment_state` in `{"not_paid", "partial", "paid"}`. Update header and line settlement amounts
  genuinely during `record_o2c_payment` so subsequent live `observe()` matches residual arithmetic.
- Handle non-zero API business codes and external deletion/conflict cleanly without masking state or
  echoing `move_envelope["object_diff"]` inside `observe()`.

## Verification
Iterate with `./verify_driver.sh check` and `./verify_driver.sh verify` until exit code `0` (`verdict == "CORROBORATED"`).
"""
        spoilers = _JSHERP_FORBIDDEN_SPOILERS
    elif target.erp == "jsherp" and target.domain == "h2r":
        req = """# Autonomous Enterprise ERP Integration Specification (Blind REST H2R Directory)

## Mission
Integrate the target external REST ERP system's department hierarchy, job roles/positions, and staff
directory onto the locked OpenOntology standard `std_h2r` contract in `target_abi.json`. Read
connection settings dynamically at runtime from `world_a_endpoint.json` (`base_url`, `openapi_url`,
`login_url`, `username`, `password`, `password_md5`, `token_header`) and introspect the live HTTP
REST service autonomously via `openapi_url` and authenticated probes.

NEVER hardcode tenant login names, credentials, company names, department names, role titles,
employee names, or record IDs in generated code. An unseen randomized & PK-shifted tenant will
be tested after exit.

## Transport & Rate-Limit Contract
- Authenticate via `POST login_url` using `username` and `password_md5` (or compute MD5 from
  `password` if omitted). Pass the returned auth token in HTTP header `token_header`.
- Respect the server's 1-second anti-replay check between consecutive writes (`time.sleep(1.05)`
  when needed) and automatically re-login once if an expired/replayed session returns HTTP 401/500.

## Canonical Prefixes & Deliverables (write ONLY inside `out/`)
1. `out/mapping.json`: valid `AdapterMappingSpec` mapping all 4 external-owned entity types in
   `target_abi.json` (`AccountingEntity`, `Department`, `JobPosition`, `Employee` — note
   `AuthorityMandate` is held natively by OpenOntology and is not mapped from external REST) and all
   3 target actions (`reparent_department`, `assign_employee_position`, `offboard_employee`).
   Keep prefix `ENT-` for `AccountingEntity` and matching prefixes between `EntityMappingSpec` and
   `ActionExecutionBinding`.
2. `out/driver.py`: ultra-thin Python transport driver (`<= 350` lines) exposing
   `build_device_bridge(endpoint_config, mapping) -> SourceBridge`.

## Domain Semantics
- `AccountingEntity`: tenant enterprise entity (`status="active"`).
- `Department`: hierarchy tree queried from live endpoints; populate `parent_department` cleanly as
  `None` for root units or prefixed canonical parent ID, and `scope_tag` from unit metadata.
- `JobPosition`: inspect tenant-scoped role records (filter out built-in/global template roles that
  lack tenant department linkage so `department`, closed-set `role_name`, and `approval_ceiling_cents`
  always resolve cleanly to valid active tenant departments and closed `STD_ROLES`).
- `Employee`: inspect tenant staff accounts, excluding the API integration login account itself
  (`login == endpoint_config["username"]` or tenant admin account without a staff position) so
  actuation never locks out the integration session! Map `department`, `position`, `full_name`,
  `actor_id`, and `status` in `{"onboarding", "active", "suspended", "offboarded"}` accurately from
  live account status & lifecycle metadata (when deactivated/disabled externally, report `"offboarded"`
  if not deleted; if deleted externally, omit so out-of-band deletion triggers `DEVICE_STATE_MISMATCH`).
- Actuate `reparent_department` (updating unit parent), `assign_employee_position` (updating staff
  unit, role/position, and lifecycle metadata cleanly), and `offboard_employee` (disabling the staff
  account on the remote server).
- NEVER echo `move_envelope["object_diff"]` inside `observe()`.

## Verification
Iterate with `./verify_driver.sh check` and `./verify_driver.sh verify` until exit code `0` (`verdict == "CORROBORATED"`).
"""
        spoilers = _JSHERP_FORBIDDEN_SPOILERS
    else:
        raise ValueError(f"Unsupported target: {target}")

    lowered = req.lower()
    leaked = [tok for tok in spoilers if tok in lowered]
    assert not leaked, f"requirements.md leaked forbidden spoilers for {target.erp}/{target.domain}: {leaked}"
    return req


# ---------------------------------------------------------------------------
# Ephemeral Reset Server & Workspace Builder
# ---------------------------------------------------------------------------


class EphemeralResetServer:
    """Host-side localhost HTTP reset server keeping DB/master reset logic out of workspace."""

    def __init__(self, reset_cb: Callable[[], None]) -> None:
        self._lock = threading.RLock()
        self._reset_cb = reset_cb
        self._srv: ThreadingHTTPServer | None = None
        self.port: int = 0

    def reset_now(self) -> None:
        with self._lock:
            self._reset_cb()

    def start(self) -> str:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                try:
                    parent.reset_now()
                    payload = json.dumps({"ok": True}).encode("utf-8")
                    self.send_response(200)
                except Exception as exc:
                    payload = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

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


def evaluate_h2r_extra_assignment_case(
    *,
    target: DomainTargetSpec,
    driver_path: Path,
    mapping_path: Path,
    endpoint_config: Mapping[str, Any],
    reset_fn: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    """Deterministic full-coverage referee for ``assign_employee_position`` (3 required params)."""
    from open_ontology.design import _load_thin_driver_factory, _observe_payload_to_m0_objects
    from open_ontology.drivers import DeviceDriverRegistry, MountedDevice

    bp = target.blueprint_factory()
    mapping_obj = AdapterMappingSpec.model_validate(json.loads(mapping_path.read_text("utf-8")))
    factory = _load_thin_driver_factory(driver_path)
    from examples.business_ontology import BUSINESS_SEED_ONTOLOGY

    spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    class _H2rAssignSandbox:
        def __init__(self) -> None:
            self._ep = dict(endpoint_config)
            self._writes = 0

        def reset(self) -> None:
            reset_fn(dict(self._ep))

        def snapshot_m0(self) -> list[dict[str, Any]]:
            br = factory(dict(self._ep), mapping_obj)
            objs, _ = call_observe(br, None, spec=spec)
            return _observe_payload_to_m0_objects(objs, spec, validate_models=False)

        def make_source_bridge(self, sp: Any, _mp: AdapterMappingSpec) -> SourceBridge:
            br = factory(dict(self._ep), mapping_obj)

            def _act(mv: dict[str, Any]) -> Any:
                self._writes += 1
                return br.actuate(mv)

            reg = DeviceDriverRegistry(spec=sp)
            reg.mount(
                MountedDevice(
                    device_id=target.device_id,
                    name=target.device_id,
                    kind=target.device_kind,
                    endpoint=str(self._ep.get("base_url") or "rest://h2r"),
                    bridge=SourceBridge(actuate=_act, observe=br.observe),
                    handled_actions=list(target.handled_actions),
                    owned_object_types=list(target.owned_object_types),
                )
            )
            return reg.as_source_bridge()

        def external_write_count(self) -> int:
            return self._writes

    sandbox = _H2rAssignSandbox()
    sandbox.reset()
    m0 = sandbox.snapshot_m0()
    positions = [e for e in m0 if e["object_type"] == "JobPosition"]
    employees = [e for e in m0 if e["object_type"] == "Employee" and e["data"].get("status") != "offboarded"]
    chosen_emp = next((e for e in employees if e["data"].get("status") == "onboarding"), employees[0] if employees else None)
    if not positions or not chosen_emp:
        return {"ok": False, "verdict": "FALSIFIED", "error": "Insufficient JobPosition/Employee seed rows for assign_employee_position"}

    alt_pos = next(
        (p for p in positions if p["object_id"] != chosen_emp["data"].get("position")),
        positions[0],
    )
    depts = [e for e in m0 if e["object_type"] == "Department" and e["data"].get("status") == "active"]
    child_d = next((d for d in depts if d["data"].get("parent_department")), None)
    root_d = next((d for d in depts if d["data"].get("parent_department") is None and d["object_id"] != (child_d or {}).get("object_id")), None)
    custom_cov = [
        FalsificationCase(
            name="COV-H2R-ASSIGN-EMPLOYEE-POSITION",
            track="coverage",
            action="assign_employee_position",
            params={
                "employee_id": str(chosen_emp["object_id"]),
                "department_id": str(alt_pos["data"]["department"]),
                "position_id": str(alt_pos["object_id"]),
                "activate": True,
            },
            actor=Actor(actor_id="referee:hr_admin", role="hr_admin"),
            expected_valid=True,
        ),
    ]
    if child_d is not None:
        custom_cov.append(
            FalsificationCase(
                name="COV-H2R-REPARENT-CLEAR",
                track="coverage",
                action="reparent_department",
                params={"department_id": str(child_d["object_id"]), "new_parent_id": ""},
                actor=Actor(actor_id="referee:hr_admin", role="hr_admin"),
                expected_valid=True,
            )
        )
        if root_d is not None:
            custom_cov.append(
                FalsificationCase(
                    name="COV-H2R-REPARENT-SET",
                    track="coverage",
                    action="reparent_department",
                    params={"department_id": str(child_d["object_id"]), "new_parent_id": str(root_d["object_id"])},
                    actor=Actor(actor_id="referee:hr_admin", role="hr_admin"),
                    expected_valid=True,
                )
            )
    fal = DifferentialFalsifier(min_action_coverage=1.0, min_transition_coverage=0.80, max_waves=2)
    report = fal.evaluate(
        blueprint=bp,
        adapter_mapping=mapping_obj,
        sandbox=sandbox,
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        target_actions=list(target.handled_actions),
        custom_cases=custom_cov,
        auto_generate=True,
    )
    return {
        "ok": report.verdict == "CORROBORATED",
        "verdict": report.verdict,
        "falsification": report.to_dict(),
    }


def verify_h2r_thin_driver(
    *,
    target: DomainTargetSpec,
    driver_path: Path | str,
    mapping_path: Path | str,
    endpoint_config: Mapping[str, Any],
    forbidden_literals: Sequence[str] = (),
    max_driver_lines: int = 350,
    device_id: str = "jsherp_h2r",
    reset_fn: Callable[[Mapping[str, Any]], None] | None = None,
    oob_tamper_check: Callable[[Mapping[str, Any], Sequence[dict[str, Any]]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """H2R-aware referee verifier avoiding Crucible's ``_name`` false-positive on ``JobPosition.role_name``.

    ``open_ontology/induction/crucible.py:181`` adds any M0 field whose name ends with
    ``_name`` (including ``JobPosition.role_name`` values in closed ``STD_ROLES``) into
    ``known_codes``, causing ``gate_5_parsimony_anti_overfitting`` to flag the locked
    standard invariant ``h2r_job_position_closed_role`` with ``OVERFITTING_INSTANCE_CODE``.
    This verifier runs the full 4-gate stack: AST/line-budget audit, ``run_design_check``
    device alignment, ``SymbolicCrucible`` (blueprint+mapping) + live M0 grounding and
    post-state invariant checks, full multi-wave ``DifferentialFalsifier`` (100% of H2R
    actions including 4-param ``assign_employee_position``), and optional OOB tamper check.
    """
    from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
    from open_ontology.design import _load_thin_driver_factory, _observe_payload_to_m0_objects, run_design_check
    from open_ontology.drivers import DeviceDriverRegistry, MountedDevice
    from open_ontology.induction.crucible import (
        CrucibleContext,
        SymbolicCrucible,
        _load_objects_into_sandbox,
        _validate_all_post_state_invariants,
    )
    from open_ontology.runtime import OntologyRuntime
    from open_ontology.settlement import DEVICE_STATE_MISMATCH, DeviceStateMismatchError

    resolved_driver = Path(driver_path).resolve()
    resolved_mapping = Path(mapping_path).resolve()
    ep_copy = copy.deepcopy(dict(endpoint_config))

    ast_issues = audit_thin_driver_ast(
        resolved_driver,
        forbidden_literals=forbidden_literals,
        max_lines=max_driver_lines,
    )
    if ast_issues:
        return {
            "ok": False,
            "command": "h2r_verify",
            "verdict": "FALSIFIED",
            "errors": list(ast_issues),
            "error_codes": ["AST_AUDIT_FAILED"],
            "ast_errors": ast_issues,
            "crucible_passed": False,
            "crucible_feedback": "\n".join(ast_issues),
            "falsification": None,
            "oob_tamper_result": None,
        }

    errors: list[str] = []
    error_codes: list[str] = []
    if not resolved_mapping.is_file():
        return {
            "ok": False,
            "command": "h2r_verify",
            "verdict": "FALSIFIED",
            "errors": [f"MISSING_MAPPING: {resolved_mapping}"],
            "error_codes": ["MISSING_MAPPING"],
            "ast_errors": [],
            "crucible_passed": False,
            "crucible_feedback": f"Missing mapping: {resolved_mapping}",
            "falsification": None,
            "oob_tamper_result": None,
        }

    mapping_obj = AdapterMappingSpec.model_validate(json.loads(resolved_mapping.read_text("utf-8")))
    with tempfile.TemporaryDirectory(prefix="oo_h2r_chk_") as tmpdir:
        tmp_path = Path(tmpdir)
        dev_file = tmp_path / "device.json"
        dev_file.write_text(
            json.dumps(
                {
                    "device_id": device_id,
                    "name": f"Blind Target ({device_id})",
                    "kind": target.device_kind,
                    "endpoint": str(ep_copy.get("base_url") or ep_copy.get("url") or "rest://h2r"),
                    "mapping_path": str(resolved_mapping),
                    "driver_module_path": str(resolved_driver),
                    "endpoint_config": ep_copy,
                    "forbidden_literals": list(forbidden_literals),
                    "max_driver_lines": max_driver_lines,
                    "owned_object_types": list(target.owned_object_types),
                    "shared_object_types": list(target.shared_object_types),
                    "handled_actions": list(target.handled_actions),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        chk = run_design_check(
            blueprints=[target.blueprint_arg],
            seed=BUSINESS_SEED_ARG,
            devices=[str(dev_file)],
            forbidden_literals=forbidden_literals,
            max_driver_lines=max_driver_lines,
            reset_hooks={device_id: reset_fn} if reset_fn is not None else None,
        )
    if not chk.get("ok"):
        errors.extend(chk.get("errors") or [])
        error_codes.extend(chk.get("error_codes") or [])
        return {
            "ok": False,
            "command": "h2r_verify",
            "verdict": "FALSIFIED",
            "errors": errors,
            "error_codes": error_codes,
            "ast_errors": [],
            "crucible_passed": False,
            "crucible_feedback": "\n".join(errors),
            "falsification": None,
            "oob_tamper_result": None,
        }

    bp = target.blueprint_factory()
    compiled_spec = compile_blueprint(bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    # Gate 1, 3, 4, 5 via SymbolicCrucible (without role_name -> known_codes collision)
    crucible_report = SymbolicCrucible().evaluate(
        bp,
        CrucibleContext(
            mode="genesis",
            m0_objects=[],
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            adapter_mapping=mapping_obj,
        ),
    )
    crucible_feedback = crucible_report.diagnostic_feedback()
    if not crucible_report.passed:
        return {
            "ok": False,
            "command": "h2r_verify",
            "verdict": "FALSIFIED",
            "errors": [crucible_feedback],
            "error_codes": ["CRUCIBLE_FAILED"],
            "ast_errors": [],
            "crucible_passed": False,
            "crucible_feedback": crucible_feedback,
            "falsification": None,
            "oob_tamper_result": None,
        }

    # Gate 2: Live M0 Pydantic schema + FK + invariant grounding
    if reset_fn is not None:
        reset_fn(copy.deepcopy(ep_copy))
    factory = _load_thin_driver_factory(resolved_driver)
    live_bridge = factory(copy.deepcopy(ep_copy), mapping_obj)
    obs_objs, _ = call_observe(live_bridge, None, spec=compiled_spec)
    m0_live = _observe_payload_to_m0_objects(obs_objs, compiled_spec, validate_models=True)
    m0_rt, load_errs = _load_objects_into_sandbox(compiled_spec, m0_live)
    try:
        inv_errs = _validate_all_post_state_invariants(m0_rt)
    finally:
        m0_rt.close()
    grounding_errs = [*load_errs, *inv_errs]
    if grounding_errs:
        return {
            "ok": False,
            "command": "h2r_verify",
            "verdict": "FALSIFIED",
            "errors": grounding_errs,
            "error_codes": ["M0_GROUNDING_FAILED"],
            "ast_errors": [],
            "crucible_passed": False,
            "crucible_feedback": "\n".join(grounding_errs),
            "falsification": None,
            "oob_tamper_result": None,
        }

    # DifferentialFalsifier full-coverage waves (reparent_department, assign_employee_position, offboard_employee)
    extra = evaluate_h2r_extra_assignment_case(
        target=target,
        driver_path=resolved_driver,
        mapping_path=resolved_mapping,
        endpoint_config=copy.deepcopy(ep_copy),
        reset_fn=reset_fn if reset_fn is not None else (lambda _ep: None),
    )
    fal_dict = extra.get("falsification") or {}
    if not extra.get("ok"):
        for ce in fal_dict.get("counterexamples") or []:
            errors.append(f"{ce.get('kind')}: ({ce.get('case_name')}/{ce.get('action')}) {ce.get('message')}")
            error_codes.append(str(ce.get("kind") or "FALSIFIED"))
        return {
            "ok": False,
            "command": "h2r_verify",
            "verdict": str(extra.get("verdict") or "FALSIFIED"),
            "errors": errors or [str(extra.get("error") or "Falsification failed")],
            "error_codes": error_codes or ["FALSIFIED"],
            "ast_errors": [],
            "crucible_passed": True,
            "crucible_feedback": crucible_feedback,
            "falsification": fal_dict,
            "oob_tamper_result": None,
        }

    oob_result: dict[str, Any] | None = None
    ok = True
    verdict = str(extra.get("verdict") or "CORROBORATED")
    if oob_tamper_check is not None:
        if reset_fn is not None:
            reset_fn(copy.deepcopy(ep_copy))
        drv_bridge = factory(copy.deepcopy(ep_copy), mapping_obj)
        bus = DeviceDriverRegistry(spec=compiled_spec)
        bus.mount(
            MountedDevice(
                device_id=device_id,
                name=device_id,
                kind=target.device_kind,
                endpoint=str(ep_copy.get("base_url") or ep_copy.get("url") or "rest://h2r"),
                bridge=drv_bridge,
                handled_actions=list(target.handled_actions),
                owned_object_types=list(target.owned_object_types),
                shared_object_types=list(target.shared_object_types),
                allow_source_revert=drv_bridge.allow_source_revert,
            )
        )
        bus_bridge = bus.as_source_bridge()
        obs_before, _ = call_observe(bus_bridge, None, spec=compiled_spec)
        m0_before = _observe_payload_to_m0_objects(obs_before, compiled_spec, validate_models=True)
        coupled_rt = OntologyRuntime(
            compiled_spec,
            db_path=":memory:",
            world_mode="coupled",
            source_bridge=bus_bridge,
        )
        try:
            type_order = {name: idx for idx, name in enumerate(compiled_spec.object_types)}
            for entry in sorted(
                m0_before,
                key=lambda item: type_order.get(str(item.get("object_type") or ""), 9999),
            ):
                tname = str(entry["object_type"])
                cls = compiled_spec.get_object_type(tname)
                coupled_rt.put_object(cls(**entry["data"]))

            audit_actor = Actor(actor_id="referee:audit", role="admin", clearance_tags={"*"})
            abox_before = {
                ot_name: {
                    obj.primary_key_value(): obj.to_properties()
                    for obj in coupled_rt.query_objects(ot_name, audit_actor)
                }
                for ot_name in compiled_spec.object_types
            }
            probe = dict(oob_tamper_check(copy.deepcopy(ep_copy), m0_before))
            probe_action = str(probe["action"])
            probe_params = dict(probe["params"])
            probe_role = str(probe.get("actor_role") or "admin")
            probe_actor = Actor(actor_id="referee:oob_tamper", role=probe_role)
            run_id = "WO14-OOB-TAMPER"

            caught_code: str | None = None
            caught_msg = ""
            committed_unexpectedly = False
            try:
                submit_res = coupled_rt.submit_action(run_id, probe_action, probe_params, probe_actor)
                committed_unexpectedly = submit_res.get("status") == "COMMITTED"
            except DeviceStateMismatchError as exc:
                caught_code = getattr(exc, "code", None) or DEVICE_STATE_MISMATCH
                caught_msg = str(exc)
            except Exception as exc:
                caught_code = getattr(exc, "code", None) or type(exc).__name__
                caught_msg = str(exc)

            abox_after = {
                ot_name: {
                    obj.primary_key_value(): obj.to_properties()
                    for obj in coupled_rt.query_objects(ot_name, audit_actor)
                }
                for ot_name in compiled_spec.object_types
            }
            abox_unchanged = abox_after == abox_before
            audit_row = coupled_rt.get_action_run(run_id)
            audit_status = audit_row.get("status") if audit_row else None
            oob_passed = (
                not committed_unexpectedly
                and caught_code == DEVICE_STATE_MISMATCH
                and abox_unchanged
                and audit_status == "REJECTED"
            )
            oob_result = {
                "passed": oob_passed,
                "action": probe_action,
                "params": probe_params,
                "caught_error_code": caught_code,
                "caught_message": caught_msg,
                "committed_unexpectedly": committed_unexpectedly,
                "abox_unchanged": abox_unchanged,
                "audit_status": audit_status,
            }
            if not oob_passed:
                ok = False
                verdict = "FALSIFIED"
                errors.append(f"OOB_TAMPER_UNDETECTED: {oob_result}")
                error_codes.append("OOB_TAMPER_UNDETECTED")
        finally:
            coupled_rt.close()

    return {
        "ok": ok,
        "command": "h2r_verify",
        "verdict": verdict,
        "errors": errors,
        "error_codes": error_codes,
        "ast_errors": [],
        "crucible_passed": True,
        "crucible_feedback": crucible_feedback,
        "falsification": fal_dict,
        "oob_tamper_result": oob_result,
    }


def prepare_isolated_workspace(
    ws_root: Path,
    *,
    target: DomainTargetSpec,
    endpoint_cfg: Mapping[str, Any],
    reset_url: str,
) -> dict[str, Any]:
    """Create a 0444/0555 read-only exam workspace with writable ``out/`` only."""
    ws_root = ws_root.resolve()
    ws_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(ws_root), check=True)
    out_dir = ws_root / "out"
    judge_dir = ws_root / ".judge"
    out_dir.mkdir(exist_ok=True)
    judge_dir.mkdir(exist_ok=True)

    target_bp = target.blueprint_factory()
    abi_path = ws_root / "target_abi.json"
    abi_path.write_text(json.dumps(target_bp.model_dump(mode="json"), ensure_ascii=False, indent=2), "utf-8")

    ep_path = ws_root / "world_a_endpoint.json"
    ep_path.write_text(json.dumps(dict(endpoint_cfg), ensure_ascii=False, indent=2), "utf-8")

    skill_dest = ws_root / "SKILL.md"
    shutil.copy2(SKILL_MD_PATH, skill_dest)

    req_path = ws_root / "requirements.md"
    req_text = _build_requirements_text(target)
    req_path.write_text(req_text, "utf-8")

    reset_hook_path = judge_dir / "reset_hook.py"
    reset_hook_path.write_text(
        f'''"""Generic HTTP reset callback (contains zero database/container credentials)."""
from __future__ import annotations
import urllib.request

def reset_sandbox(_endpoint_config: dict) -> None:
    req = urllib.request.Request({reset_url!r}, data=b"{{}}", method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()
''',
        "utf-8",
    )

    dev_cfg_path = judge_dir / "device.json"
    ep_label = str(endpoint_cfg.get("url") or endpoint_cfg.get("base_url") or "")
    dev_cfg_path.write_text(
        json.dumps(
            {
                "device_id": target.device_id,
                "name": f"External {target.domain.upper()} ERP",
                "kind": target.device_kind,
                "endpoint": ep_label,
                "mapping_path": "../out/mapping.json",
                "driver_module_path": "../out/driver.py",
                "endpoint_config_path": "../world_a_endpoint.json",
                "reset_hook": "./reset_hook.py:reset_sandbox",
                "owned_object_types": list(target.owned_object_types),
                "shared_object_types": list(target.shared_object_types),
                "handled_actions": list(target.handled_actions),
            },
            indent=2,
        ),
        "utf-8",
    )

    target_flags = " \\\n    ".join(f'--target-action "{act}"' for act in target.handled_actions)
    verify_sh_path = ws_root / "verify_driver.sh"

    extra_ro_files: list[Path] = []
    if target.erp == "jsherp" and target.domain == "h2r":
        h2r_runner_path = judge_dir / "run_verify.py"
        h2r_runner_path.write_text(
            f'''from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, {str(ROOT)!r})
from examples.wo24c_dual_erp_fde_orchestrator import DOMAIN_SPECS, verify_h2r_thin_driver  # noqa: E402
import reset_hook  # noqa: E402

ep_cfg = json.loads(Path("world_a_endpoint.json").read_text("utf-8"))
res = verify_h2r_thin_driver(
    target=DOMAIN_SPECS[("jsherp", "h2r")],
    driver_path=Path("out/driver.py"),
    mapping_path=Path("out/mapping.json"),
    endpoint_config=ep_cfg,
    forbidden_literals=(),
    max_driver_lines=350,
    device_id="jsherp_h2r",
    reset_fn=reset_hook.reset_sandbox,
)
print(json.dumps(res, ensure_ascii=False, indent=2))
raise SystemExit(0 if res.get("ok") else 1)
''',
            "utf-8",
        )
        extra_ro_files.append(h2r_runner_path)

    verify_body = f'''#!/usr/bin/env bash
set -euo pipefail
MODE="${{1:-verify}}"
if [[ "$MODE" != "check" && "$MODE" != "verify" ]]; then
  echo "Usage: ./verify_driver.sh [check|verify]" >&2
  exit 2
fi
export NO_PROXY="127.0.0.1,localhost,::1,${{NO_PROXY:-}}"
export no_proxy="$NO_PROXY"
export PYTHONPATH="{ROOT}:${{PYTHONPATH:-}}"
if [[ "$MODE" == "verify" ]]; then'''
    if target.erp == "jsherp" and target.domain == "h2r":
        verify_body += f'''
  exec "{sys.executable}" -c "import sys; sys.path.insert(0, '.judge'); import run_verify"'''
    else:
        verify_body += f'''
  exec "{sys.executable}" -m open_ontology.design verify \\
    --blueprint "{target.blueprint_arg}" \\
    --seed "{BUSINESS_SEED_ARG}" \\
    --device .judge/device.json \\
    {target_flags} \\
    --min-action-coverage {target.min_auto_action_coverage} \\
    --min-transition-coverage 0.80 \\
    --max-waves 2'''
    verify_body += f'''
else
  "{sys.executable}" -c "import sys; sys.path.insert(0, '.judge'); import reset_hook; reset_hook.reset_sandbox({{}})"
  exec "{sys.executable}" -m open_ontology.design check \\
    --blueprint "{target.blueprint_arg}" \\
    --seed "{BUSINESS_SEED_ARG}" \\
    --device .judge/device.json
fi
'''
    verify_sh_path.write_text(verify_body, "utf-8")

    readonly_files = [abi_path, ep_path, skill_dest, req_path, reset_hook_path, dev_cfg_path, *extra_ro_files]
    hashes: dict[str, str] = {}
    for p in readonly_files:
        hashes[str(p.relative_to(ws_root))] = _sha256_path(p)
        p.chmod(0o444)
    hashes["verify_driver.sh"] = _sha256_path(verify_sh_path)
    verify_sh_path.chmod(0o555)
    judge_dir.chmod(0o555)
    return {"ws_root": ws_root, "readonly_hashes": hashes}


def _verify_readonly_integrity(ws_root: Path, expected_hashes: Mapping[str, str]) -> None:
    tampered: list[str] = []
    for rel, old_digest in expected_hashes.items():
        p = ws_root / rel
        if not p.is_file() or _sha256_path(p) != old_digest:
            tampered.append(rel)
    if tampered:
        raise RuntimeError(f"Workspace tamper detected on read-only files: {tampered}")


def _create_sandbox_shim_bin(shim_dir: Path) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/usr/bin/env sh\n"
        'echo "Blocked by WO-24C blind FDE sandbox: use protocol endpoint in world_a_endpoint.json only." >&2\n'
        "exit 1\n"
    )
    for cmd_name in ("docker", "psql", "mysql", "mysqldump", "redis-cli", "javap"):
        cmd_path = shim_dir / cmd_name
        cmd_path.write_text(body, "utf-8")
        cmd_path.chmod(0o555)
    return shim_dir


def resolve_cli_binary(runner_name: str) -> str:
    runner = runner_name.strip().lower()
    if runner == "opencode":
        found = shutil.which("opencode")
        if not found and Path("/opt/homebrew/bin/opencode").is_file():
            found = "/opt/homebrew/bin/opencode"
        if not found:
            raise RuntimeError("opencode CLI binary not found")
        return found
    if runner == "agy":
        found = shutil.which("agy")
        if not found and (Path.home() / ".local" / "bin" / "agy").is_file():
            found = str(Path.home() / ".local" / "bin" / "agy")
        if not found:
            raise RuntimeError("agy CLI binary not found")
        return found
    raise ValueError(f"Unsupported runner {runner_name!r}")


def build_cli_command(
    *,
    runner_name: str,
    cli_path: str,
    model_name: str,
    prompt: str,
    timeout_sec: int,
    session_id: str | None = None,
) -> list[str]:
    runner = runner_name.strip().lower()
    if runner == "opencode":
        cmd = [cli_path, "--standalone", "run", "--auto", "--format", "json", "-m", model_name]
        if session_id:
            cmd.extend(["--session", session_id])
        cmd.append(prompt)
        return cmd
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
    raise ValueError(f"Unsupported runner {runner_name!r}")


def parse_cli_output(
    *,
    runner_name: str,
    stdout: str,
    wall_elapsed_sec: float | None = None,
) -> dict[str, Any] | None:
    runner = runner_name.strip().lower()
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

    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("{"):
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
        else (round((max(timestamps) - min(timestamps)) / 1000.0, 3) if len(timestamps) >= 2 else 0.0)
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


def _build_agent_env(*, ws_root: Path, shim_bin: Path) -> dict[str, str]:
    _ensure_local_no_proxy()
    env = {k: v for k, v in os.environ.items() if k not in ("PWD", "OLDPWD")}
    env["PWD"] = str(ws_root.resolve())
    env["PATH"] = f"{shim_bin}:{Path(sys.executable).parent}:{env.get('PATH', '')}"
    or_key = _read_secret_readonly("OPENROUTER_API_KEY")
    if or_key:
        env["OPENROUTER_API_KEY"] = or_key
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {
            "provider": {
                "openrouter": {
                    "options": {"apiKey": "{env:OPENROUTER_API_KEY}"},
                    "models": {"deepseek/deepseek-v4.1-flash": {"name": "DeepSeek V4.1 Flash"}},
                }
            }
        }
    )
    return env


def _build_blind_prompt(target: DomainTargetSpec) -> str:
    proto = "HTTP JSON-RPC" if target.erp == "odoo19" else "OpenAPI + HTTP REST"
    prompt = (
        "Your current directory '.' already contains ./SKILL.md, ./requirements.md, ./target_abi.json, "
        "./world_a_endpoint.json, ./verify_driver.sh, and the writable ./out/ directory — read these local files "
        "directly and do NOT search outside '.' or touch external containers/repos. "
        f"Autonomously inspect the live system configured in world_a_endpoint.json over {proto}, "
        "author ONLY ./out/mapping.json (AdapterMappingSpec) and ./out/driver.py (<= 350 lines exposing "
        "build_device_bridge(endpoint_config, mapping) -> SourceBridge), and iterate with ./verify_driver.sh check "
        "and ./verify_driver.sh verify until ./verify_driver.sh verify exits 0 with ok=true and verdict=CORROBORATED. "
        "NEVER hardcode tenant/DB names, credentials, names, IDs, document numbers, or amounts in out/driver.py; "
        "an unseen randomized & PK-shifted tenant will be tested after you exit."
    )
    spoilers = _ODOO_FORBIDDEN_SPOILERS if target.erp == "odoo19" else _JSHERP_FORBIDDEN_SPOILERS
    leaked = [tok for tok in spoilers if tok in prompt.lower()]
    assert not leaked, f"prompt leaked spoilers: {leaked}"
    return prompt


def _extract_raw_int_id(canonical_id: str, prefix: str, fallback: int) -> int:
    stripped = canonical_id[len(prefix) :] if prefix and canonical_id.startswith(prefix) else canonical_id
    if stripped.isdigit():
        return int(stripped)
    m = re.search(r"(\d+)$", stripped)
    return int(m.group(1)) if m else int(fallback)


# ---------------------------------------------------------------------------
# Per-Domain Blind Exam + Dual-Engine Fallback Loop
# ---------------------------------------------------------------------------


def _provision_world(
    *,
    target: DomainTargetSpec,
    odoo_admin: Odoo24cAdminClient | None = None,
    jsherp_admin: JshErp24cAdminClient | None = None,
    kind: str,  # "pub" | "hid"
    shift_pks: int,
) -> tuple[dict[str, Any], dict[str, Any], Callable[[Mapping[str, Any]], None], Callable[[Mapping[str, Any], Sequence[dict[str, Any]]], dict[str, Any]]]:
    """Provision World A or Hidden World B + reset + OOB tamper callback."""
    rnd = random.SystemRandom()
    rid = uuid.uuid4().hex[:6]
    tag = f"{kind.upper()}{rid.upper()}"
    is_pub = kind == "pub"

    if target.erp == "odoo19":
        assert odoo_admin is not None
        tpl_db = f"wo24c_{target.domain}_{kind}_tpl_{rid}"
        live_db = f"wo24c_{target.domain}_{kind}_{rid}"
        odoo_admin.clone_db(SOURCE_DEMO_DB, tpl_db)

        if target.domain == "o2c":
            info = seed_odoo_o2c_world(
                odoo_admin,
                tpl_db,
                tag=tag,
                customer_a={"name": f"{'Horizon' if is_pub else 'Borealis'} Precision {tag}", "ref": f"{tag}-C1", "tax_id": f"TAX-{tag}-A", "credit_limit": 520000.0 if is_pub else 740000.0, "country_code": "CN"},
                customer_b={"name": f"{'Meridian' if is_pub else 'Solstice'} Logistics {tag}", "ref": f"{tag}-C2", "tax_id": f"TAX-{tag}-B", "credit_limit": 410000.0 if is_pub else 650000.0, "country_code": "CN"},
                order_specs=[
                    {"stage": "draft", "qty": 2 if is_pub else rnd.randint(3, 5), "unit_price": 1500.0 if is_pub else round(rnd.uniform(2100.0, 3900.0), 2), "client_ref": f"REF1-{tag}"},
                    {"stage": "confirmed", "qty": 3 if is_pub else rnd.randint(2, 4), "unit_price": 2000.0 if is_pub else round(rnd.uniform(1800.0, 4200.0), 2), "client_ref": f"REF2-{tag}"},
                    {"stage": "draft_invoice", "qty": 2 if is_pub else rnd.randint(3, 6), "unit_price": 2500.0 if is_pub else round(rnd.uniform(2300.0, 5100.0), 2), "client_ref": f"REF3-{tag}"},
                    {"stage": "posted_invoice", "qty": 1 if is_pub else rnd.randint(2, 4), "unit_price": 3200.0 if is_pub else round(rnd.uniform(2800.0, 5900.0), 2), "client_ref": f"REF4-{tag}"},
                ],
                shift_pks=shift_pks,
            )

            def _oob(ep: Mapping[str, Any], m0: Sequence[dict[str, Any]]) -> dict[str, Any]:
                draft_o = next(e for e in m0 if e["object_type"] == "O2cOrder" and e["data"]["status"] == "draft")
                cid = str(draft_o["object_id"])
                raw_id = next((o["sale_order_id"] for o in info["orders"] if o["client_ref"] == draft_o["data"].get("order_number") or str(o["sale_order_id"]) in cid), info["orders"][0]["sale_order_id"])
                kw_b = odoo_admin.login_kw(str(ep["db"]))
                kw_b("sale.order", "action_cancel", [[raw_id]])
                return {"action": "confirm_o2c_order", "params": {"order_id": cid}, "actor_role": "sales_rep"}

        elif target.domain == "p2p":
            info = seed_odoo_p2p_world(
                odoo_admin,
                tpl_db,
                tag=tag,
                supplier_a={"name": f"{'Foundry' if is_pub else 'Zenith'} Heavy {tag}", "ref": f"{tag}-S1", "tax_id": f"TAX-{tag}-S1", "country_code": "CN"},
                supplier_b={"name": f"{'Mill' if is_pub else 'Atlas'} Materials {tag}", "ref": f"{tag}-S2", "tax_id": f"TAX-{tag}-S2", "country_code": "CN"},
                po_specs=[
                    {"stage": "draft", "qty": 6 if is_pub else float(rnd.randint(4, 9)), "unit_price": 1200.0 if is_pub else round(rnd.uniform(1400.0, 2900.0), 2), "ref": f"PO1-{tag}"},
                    {"stage": "confirmed", "qty": 4 if is_pub else float(rnd.randint(3, 8)), "unit_price": 1800.0 if is_pub else round(rnd.uniform(1700.0, 3400.0), 2), "ref": f"PO2-{tag}"},
                    {"stage": "draft_bill", "qty": 5 if is_pub else float(rnd.randint(4, 7)), "unit_price": 1500.0 if is_pub else round(rnd.uniform(1900.0, 3800.0), 2), "ref": f"PO3-{tag}"},
                    {"stage": "posted_bill", "qty": 3 if is_pub else float(rnd.randint(2, 6)), "unit_price": 2100.0 if is_pub else round(rnd.uniform(2200.0, 4100.0), 2), "ref": f"PO4-{tag}"},
                ],
                shift_pks=shift_pks,
            )

            def _oob(ep: Mapping[str, Any], m0: Sequence[dict[str, Any]]) -> dict[str, Any]:
                draft_po = next(e for e in m0 if e["object_type"] == "P2pPurchaseOrder" and e["data"]["status"] == "draft")
                cid = str(draft_po["object_id"])
                m = re.search(r"(\d+)$", cid)
                raw_id = int(m.group(1)) if m else int(info["orders"][0]["po_id"])
                kw_b = odoo_admin.login_kw(str(ep["db"]))
                kw_b("purchase.order", "button_cancel", [[raw_id]])
                return {"action": "confirm_p2p_order", "params": {"po_id": cid}, "actor_role": "buyer"}

        elif target.domain == "r2r":
            info = seed_odoo_r2r_world(
                odoo_admin,
                tpl_db,
                tag=tag,
                draft_small_amount=1250.0 if is_pub else round(rnd.uniform(900.0, 3500.0), 2),
                draft_large_amount=6800.0 if is_pub else round(rnd.uniform(5500.0, 9500.0), 2),
                posted_amount=2400.0 if is_pub else round(rnd.uniform(1600.0, 4200.0), 2),
                shift_pks=shift_pks,
            )

            def _oob(ep: Mapping[str, Any], m0: Sequence[dict[str, Any]]) -> dict[str, Any]:
                draft_je = next(
                    e
                    for e in sorted(m0, key=lambda x: str(x["object_id"]))
                    if e["object_type"] == "JournalEntry" and e["data"]["status"] == "draft" and int(e["data"]["amount_cents"]) < 500_000
                )
                cid = str(draft_je["object_id"])
                m = re.search(r"(\d+)$", cid)
                raw_id = int(m.group(1)) if m else int(info["entries"][0]["move_id"])
                kw_b = odoo_admin.login_kw(str(ep["db"]))
                kw_b("account.move", "button_cancel", [[raw_id]])
                kw_b("account.move", "unlink", [[raw_id]])
                return {"action": "post_journal_entry", "params": {"entry_id": cid}, "actor_role": "finance"}

        else:
            raise ValueError(f"Unsupported Odoo domain {target.domain}")

        odoo_admin.clone_db(tpl_db, live_db)
        info["live_db_name"] = live_db
        info["fingerprints"] = [live_db, *info["fingerprints"]]
        ep_cfg = {"url": ODOO_URL, "db": live_db, "username": ODOO_USER, "password": ODOO_PASSWORD}

        def _reset(_ep: Mapping[str, Any] | None = None) -> None:
            odoo_admin.drop_db(live_db)
            odoo_admin.clone_db(tpl_db, live_db)

        return info, ep_cfg, _reset, _oob

    # jshERP worlds
    assert jsherp_admin is not None
    login_name = f"wo24c_{target.domain}_{kind}_{rid}"
    pw_plain = f"Pass_{kind}_{rid}!"

    if target.domain == "p2p":
        info = jsherp_admin.seed_p2p_tenant(
            login_name=login_name,
            password_plain=pw_plain,
            company_name=f"{'Apex' if is_pub else 'Borealis'} Procurement {tag}",
            depot_name=f"WH-{tag}",
            account_name=f"Treasury {tag}",
            account_serial=f"ACC-{tag}",
            material_name=f"Hot Rolled Coil {tag}",
            material_barcode=f"MAT-{tag}",
            suppliers=[
                {"name": f"{'Foundry' if is_pub else 'Zenith'} Alpha {tag}", "tax_id": f"TAX-A-{tag}", "payment_terms": "NET30"},
                {"name": f"{'Mill' if is_pub else 'Solstice'} Beta {tag}", "tax_id": f"TAX-B-{tag}", "payment_terms": "NET45"},
            ],
            flow_specs=[
                {"stage": "draft", "supplier_idx": 0, "qty": 10.0 if is_pub else float(rnd.randint(6, 12)), "unit_price": 4000.0 if is_pub else round(rnd.uniform(3100.0, 4900.0), 2), "po_number": f"PO1-{tag}"},
                {"stage": "pending_receipt", "supplier_idx": 1, "qty": 8.0 if is_pub else float(rnd.randint(5, 11)), "unit_price": 4150.0 if is_pub else round(rnd.uniform(3300.0, 5200.0), 2), "po_number": f"PO2-{tag}", "receipt_number": f"RK2-{tag}"},
                {"stage": "draft_bill", "supplier_idx": 0, "qty": 6.0 if is_pub else float(rnd.randint(4, 9)), "unit_price": 4300.0 if is_pub else round(rnd.uniform(3500.0, 5600.0), 2), "po_number": f"PO3-{tag}", "receipt_number": f"RK3-{tag}", "bill_number": f"FK3-{tag}"},
                {"stage": "posted_bill", "supplier_idx": 1, "qty": 5.0 if is_pub else float(rnd.randint(3, 8)), "unit_price": 4450.0 if is_pub else round(rnd.uniform(3800.0, 5900.0), 2), "po_number": f"PO4-{tag}", "receipt_number": f"RK4-{tag}", "bill_number": f"FK4-{tag}"},
            ],
            shift_pks=shift_pks,
        )

        def _oob(_ep: Mapping[str, Any], m0: Sequence[dict[str, Any]]) -> dict[str, Any]:
            draft_po = next(e for e in m0 if e["object_type"] == "P2pPurchaseOrder" and e["data"]["status"] == "draft")
            cid = str(draft_po["object_id"])
            raw_id = _extract_raw_int_id(cid, "", int(info["flows"][0]["po_id"]))
            tok, tid = jsherp_admin.login(info["login_name"], info["password_md5"])
            jsherp_admin._admin_rest(f"/depotHead/delete?id={raw_id}", method="DELETE", token=tok, tenant_id=tid)
            jsherp_admin.clear_tenant_logs(tid)
            return {"action": "confirm_p2p_order", "params": {"po_id": cid}, "actor_role": "buyer"}

    elif target.domain == "o2c":
        info = jsherp_admin.seed_o2c_tenant(
            login_name=login_name,
            password_plain=pw_plain,
            company_name=f"{'Beacon' if is_pub else 'Calypso'} Trading {tag}",
            depot_name=f"WH-S-{tag}",
            account_name=f"AR Bank {tag}",
            account_serial=f"AR-ACC-{tag}",
            material_name=f"Galvanized Plate {tag}",
            material_barcode=f"SMAT-{tag}",
            customers=[
                {"name": f"{'Orion' if is_pub else 'Vega'} Manufacturing {tag}", "tax_id": f"TAX-CA-{tag}", "credit_limit": 500000.0 if is_pub else 780000.0, "payment_terms": "NET30"},
                {"name": f"{'Polaris' if is_pub else 'Cygnus'} Retail {tag}", "tax_id": f"TAX-CB-{tag}", "credit_limit": 420000.0 if is_pub else 610000.0, "payment_terms": "NET60"},
            ],
            flow_specs=[
                {"stage": "draft", "customer_idx": 0, "qty": 4.0 if is_pub else float(rnd.randint(3, 8)), "unit_price": 4600.0 if is_pub else round(rnd.uniform(3200.0, 5100.0), 2), "so_number": f"SO1-{tag}"},
                {"stage": "pending_delivery", "customer_idx": 1, "qty": 6.0 if is_pub else float(rnd.randint(4, 9)), "unit_price": 4800.0 if is_pub else round(rnd.uniform(3500.0, 5400.0), 2), "so_number": f"SO2-{tag}", "delivery_number": f"CK2-{tag}"},
                {"stage": "draft_invoice", "customer_idx": 0, "qty": 5.0 if is_pub else float(rnd.randint(3, 7)), "unit_price": 5100.0 if is_pub else round(rnd.uniform(3900.0, 5800.0), 2), "so_number": f"SO3-{tag}", "delivery_number": f"CK3-{tag}", "invoice_number": f"SK3-{tag}"},
                {"stage": "posted_invoice", "customer_idx": 1, "qty": 3.0 if is_pub else float(rnd.randint(2, 6)), "unit_price": 5300.0 if is_pub else round(rnd.uniform(4100.0, 6200.0), 2), "so_number": f"SO4-{tag}", "delivery_number": f"CK4-{tag}", "invoice_number": f"SK4-{tag}"},
            ],
            shift_pks=shift_pks,
        )

        def _oob(_ep: Mapping[str, Any], m0: Sequence[dict[str, Any]]) -> dict[str, Any]:
            draft_so = next(e for e in m0 if e["object_type"] == "O2cOrder" and e["data"]["status"] == "draft")
            cid = str(draft_so["object_id"])
            raw_id = _extract_raw_int_id(cid, "", int(info["flows"][0]["so_id"]))
            tok, tid = jsherp_admin.login(info["login_name"], info["password_md5"])
            jsherp_admin._admin_rest(f"/depotHead/delete?id={raw_id}", method="DELETE", token=tok, tenant_id=tid)
            jsherp_admin.clear_tenant_logs(tid)
            return {"action": "confirm_o2c_order", "params": {"order_id": cid}, "actor_role": "sales_rep"}

    elif target.domain == "h2r":
        info = jsherp_admin.seed_h2r_tenant(
            login_name=login_name,
            password_plain=pw_plain,
            company_name=f"{'Titan' if is_pub else 'Hyperion'} Holdings {tag}",
            tag=tag,
            shift_pks=shift_pks,
        )

        def _oob(_ep: Mapping[str, Any], m0: Sequence[dict[str, Any]]) -> dict[str, Any]:
            active_emp = next(e for e in m0 if e["object_type"] == "Employee" and e["data"]["status"] == "active")
            cid = str(active_emp["object_id"])
            raw_id = _extract_raw_int_id(cid, "", int(info["employees"][1]["id"]))
            tok, tid = jsherp_admin.login(info["login_name"], info["password_md5"])
            jsherp_admin._admin_rest(f"/user/delete?id={raw_id}", method="DELETE", token=tok, tenant_id=tid)
            jsherp_admin.clear_tenant_logs(tid)
            return {"action": "offboard_employee", "params": {"employee_id": cid, "reason": "oob_test"}, "actor_role": "hr_admin"}

    else:
        raise ValueError(f"Unsupported jshERP domain {target.domain}")

    ep_cfg = {
        "base_url": JSHERP_BASE_URL,
        "openapi_url": f"{JSHERP_BASE_URL}/v2/api-docs",
        "login_url": f"{JSHERP_BASE_URL}/user/login",
        "username": info["login_name"],
        "password": info["password_plain"],
        "password_md5": info["password_md5"],
        "token_header": "X-Access-Token",
    }

    def _reset(_ep: Mapping[str, Any] | None = None) -> None:
        jsherp_admin.restore_tenant_snapshot(info["tenant_id"])

    return info, ep_cfg, _reset, _oob


def _run_verify_in_workspace(ws_root: Path, target: DomainTargetSpec, forbidden_literals: Sequence[str]) -> tuple[bool, list[str], dict[str, Any] | None]:
    driver_path = ws_root / "out" / "driver.py"
    mapping_path = ws_root / "out" / "mapping.json"
    if not driver_path.is_file() or not mapping_path.is_file():
        return False, ["OUT_FILES_MISSING"], None
    ast_issues = audit_thin_driver_ast(driver_path, forbidden_literals=forbidden_literals, max_lines=350)
    try:
        AdapterMappingSpec.model_validate(json.loads(mapping_path.read_text("utf-8")))
    except Exception as exc:
        return False, [*ast_issues, f"INVALID_MAPPING_JSON: {exc}"], None
    pv = subprocess.run(["./verify_driver.sh", "verify"], cwd=str(ws_root), text=True, capture_output=True)
    try:
        probe = json.loads(pv.stdout)
    except Exception:
        probe = {"ok": False, "verdict": "FALSIFIED", "errors": [pv.stdout[:400], pv.stderr[:400]]}
    return bool(pv.returncode == 0 and probe.get("ok") is True and not ast_issues), ast_issues, probe


def _run_engine_in_fresh_workspace(
    *,
    target: DomainTargetSpec,
    runner_name: str,
    models: Sequence[str],
    world_a_info: Mapping[str, Any],
    world_a_ep: Mapping[str, Any],
    reset_a: Callable[[Mapping[str, Any]], None],
    timeout_sec: int,
    max_repair_turns: int = 3,
) -> dict[str, Any]:
    """Run one AI-FDE engine inside its own isolated read-only workspace and verify World A + World B."""
    cli_path = resolve_cli_binary(runner_name)
    ws_root = Path(tempfile.mkdtemp(prefix=f"wo24c_{target.erp}_{target.domain}_{runner_name}_")).resolve()
    shim_bin = _create_sandbox_shim_bin(Path(tempfile.mkdtemp(prefix="wo24c_shim_")).resolve())
    agent_env = _build_agent_env(ws_root=ws_root, shim_bin=shim_bin)
    if runner_name == "opencode" and any(m.startswith("openrouter/") for m in models) and not agent_env.get("OPENROUTER_API_KEY"):
        shutil.rmtree(ws_root, ignore_errors=True)
        shutil.rmtree(shim_bin, ignore_errors=True)
        raise RuntimeError("OPENROUTER_API_KEY is required for opencode DeepSeek runner")

    reset_srv = EphemeralResetServer(lambda: reset_a(world_a_ep))
    reset_url = reset_srv.start()

    try:
        reset_srv.reset_now()
        prep = prepare_isolated_workspace(ws_root, target=target, endpoint_cfg=world_a_ep, reset_url=reset_url)
        prompt = _build_blind_prompt(target)

        attempts: list[dict[str, Any]] = []
        chosen_model: str | None = None
        winning_envelope: dict[str, Any] | None = None
        last_probe: dict[str, Any] | None = None

        for model_name in models:
            cmd = build_cli_command(
                runner_name=runner_name,
                cli_path=cli_path,
                model_name=model_name,
                prompt=prompt,
                timeout_sec=timeout_sec,
            )
            t0 = time.monotonic()
            timed_out = False
            try:
                proc = subprocess.run(cmd, cwd=str(ws_root), env=agent_env, text=True, capture_output=True, timeout=timeout_sec + 60)
                rc, stdout, stderr = proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
            except subprocess.TimeoutExpired as exc:
                timed_out, rc = True, 124
                stdout = (exc.stdout or "").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
                stderr = (exc.stderr or "").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            wall_sec = round(time.monotonic() - t0, 3)

            parsed_env = parse_cli_output(runner_name=runner_name, stdout=stdout, wall_elapsed_sec=wall_sec)
            passed_ws, ast_errs, probe_ver = _run_verify_in_workspace(ws_root, target, world_a_info["fingerprints"])
            last_probe = probe_ver
            attempts.append(
                {
                    "fde_runner": runner_name,
                    "model": model_name,
                    "repair_turn": 0,
                    "returncode": rc,
                    "timed_out": timed_out,
                    "agent_status": parsed_env.get("status") if parsed_env else None,
                    "num_turns": parsed_env.get("num_turns") if parsed_env else None,
                    "duration_seconds": (parsed_env.get("duration_seconds") if parsed_env else None) or wall_sec,
                    "tool_calls_count": parsed_env.get("tool_calls_count") if parsed_env else None,
                    "total_cost_usd": parsed_env.get("total_cost_usd") if parsed_env else None,
                    "usage": parsed_env.get("usage") if parsed_env else None,
                    "conversation_id": parsed_env.get("conversation_id") if parsed_env else None,
                    "ast_errors": ast_errs[:5],
                    "verify_ok": passed_ws,
                    "verify_errors": (probe_ver or {}).get("errors", [])[:5],
                    "stderr_tail": stderr[-300:],
                }
            )
            if passed_ws:
                chosen_model = model_name
                winning_envelope = parsed_env
                break

            conv_id = str((parsed_env or {}).get("conversation_id") or "")
            if conv_id and not timed_out and rc == 0:
                for rep_idx in range(1, max_repair_turns + 1):
                    ast_note = f" Static AST/mapping issues: {ast_errs[:3]}." if ast_errs else ""
                    rep_prompt = (
                        "Run ./verify_driver.sh check and ./verify_driver.sh verify right now, read every error or "
                        f"counterexample carefully, and fix out/mapping.json and/or out/driver.py until ./verify_driver.sh verify "
                        f"exits 0 with ok=true and verdict=CORROBORATED.{ast_note}"
                    )
                    cmd_rep = build_cli_command(
                        runner_name=runner_name,
                        cli_path=cli_path,
                        model_name=model_name,
                        prompt=rep_prompt,
                        timeout_sec=timeout_sec,
                        session_id=conv_id,
                    )
                    t_r0 = time.monotonic()
                    rep_to = False
                    try:
                        pr = subprocess.run(cmd_rep, cwd=str(ws_root), env=agent_env, text=True, capture_output=True, timeout=timeout_sec + 60)
                        rc_r, so_r, se_r = pr.returncode, (pr.stdout or "").strip(), (pr.stderr or "").strip()
                    except subprocess.TimeoutExpired as exc:
                        rep_to, rc_r = True, 124
                        so_r = (exc.stdout or "").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
                        se_r = (exc.stderr or "").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
                    rep_wall = round(time.monotonic() - t_r0, 3)

                    parsed_r = parse_cli_output(runner_name=runner_name, stdout=so_r, wall_elapsed_sec=rep_wall)
                    passed_r, ast_r, probe_r = _run_verify_in_workspace(ws_root, target, world_a_info["fingerprints"])
                    last_probe = probe_r
                    attempts.append(
                        {
                            "fde_runner": runner_name,
                            "model": model_name,
                            "repair_turn": rep_idx,
                            "returncode": rc_r,
                            "timed_out": rep_to,
                            "agent_status": parsed_r.get("status") if parsed_r else None,
                            "num_turns": parsed_r.get("num_turns") if parsed_r else None,
                            "duration_seconds": (parsed_r.get("duration_seconds") if parsed_r else None) or rep_wall,
                            "tool_calls_count": parsed_r.get("tool_calls_count") if parsed_r else None,
                            "total_cost_usd": parsed_r.get("total_cost_usd") if parsed_r else None,
                            "usage": parsed_r.get("usage") if parsed_r else None,
                            "conversation_id": conv_id,
                            "ast_errors": ast_r[:5],
                            "verify_ok": passed_r,
                            "verify_errors": (probe_r or {}).get("errors", [])[:5],
                            "stderr_tail": se_r[-300:],
                        }
                    )
                    if passed_r:
                        chosen_model = model_name
                        winning_envelope = parsed_r
                        break
                    if rep_to or rc_r != 0:
                        break
            if chosen_model is not None:
                break

        if chosen_model is None:
            return {
                "passed": False,
                "fde_runner": runner_name,
                "chosen_model": None,
                "failure_stage": "workspace_world_a",
                "attempts": attempts,
                "last_verify_probe": last_probe,
            }

        # Gate A: Read-only SHA-256 check
        _verify_readonly_integrity(ws_root, prep["readonly_hashes"])

        driver_path = ws_root / "out" / "driver.py"
        mapping_path = ws_root / "out" / "mapping.json"
        driver_source = driver_path.read_text("utf-8")
        mapping_text = mapping_path.read_text("utf-8")
        driver_lines = len(driver_source.splitlines())
        driver_sha256 = _sha256_path(driver_path)
        mapping_sha256 = _sha256_path(mapping_path)

        ast_issues = audit_thin_driver_ast(driver_path, forbidden_literals=world_a_info["fingerprints"], max_lines=350)
        if ast_issues:
            return {
                "passed": False,
                "fde_runner": runner_name,
                "chosen_model": chosen_model,
                "failure_stage": "outer_ast_gate",
                "attempts": attempts,
                "ast_issues": ast_issues,
            }

        # Gate C: Outer Referee independent Public World A check
        if target.erp == "jsherp" and target.domain == "h2r":
            world_a_verify = verify_h2r_thin_driver(
                target=target,
                driver_path=driver_path,
                mapping_path=mapping_path,
                endpoint_config=world_a_ep,
                forbidden_literals=world_a_info["fingerprints"],
                max_driver_lines=350,
                device_id=f"{target.device_id}_pub",
                reset_fn=reset_a,
            )
        else:
            world_a_verify = verify_thin_driver_on_hidden_world(
                blueprints=[target.blueprint_arg],
                seed=BUSINESS_SEED_ARG,
                driver_path=driver_path,
                mapping_path=mapping_path,
                hidden_endpoint_config=world_a_ep,
                forbidden_literals=world_a_info["fingerprints"],
                max_driver_lines=350,
                device_id=f"{target.device_id}_pub",
                device_kind=target.device_kind,
                owned_object_types=list(target.owned_object_types),
                shared_object_types=list(target.shared_object_types),
                handled_actions=list(target.handled_actions),
                target_actions=list(target.handled_actions),
                reset_fn=reset_a,
                min_action_coverage=target.min_auto_action_coverage,
                min_transition_coverage=0.80,
                max_waves=2,
            )

        if not (world_a_verify.get("ok") and world_a_verify.get("verdict") == "CORROBORATED"):
            return {
                "passed": False,
                "fde_runner": runner_name,
                "chosen_model": chosen_model,
                "failure_stage": "outer_world_a",
                "attempts": attempts,
                "world_a_verify": world_a_verify,
            }

        return {
            "passed": True,
            "fde_runner": runner_name,
            "chosen_model": chosen_model,
            "attempts": attempts,
            "runner_envelope": winning_envelope,
            "driver_source": driver_source,
            "mapping_text": mapping_text,
            "driver_lines": driver_lines,
            "driver_sha256": driver_sha256,
            "mapping_sha256": mapping_sha256,
            "ast_issues": ast_issues,
            "world_a_verify": world_a_verify,
        }
    finally:
        reset_srv.stop()
        shutil.rmtree(ws_root, ignore_errors=True)
        shutil.rmtree(shim_bin, ignore_errors=True)


def run_blind_domain(
    *,
    target: DomainTargetSpec,
    persist_dir: Path,
    odoo_admin: Odoo24cAdminClient | None = None,
    jsherp_admin: JshErp24cAdminClient | None = None,
    timeout_sec: int = 480,
) -> dict[str, Any]:
    """Execute blind AI-FDE on one ERP domain: DeepSeek primary -> auto Gemini fallback -> Hidden World B + OOB."""
    world_a_info, world_a_ep, reset_a, _oob_a = _provision_world(
        target=target,
        odoo_admin=odoo_admin,
        jsherp_admin=jsherp_admin,
        kind="pub",
        shift_pks=0,
    )

    all_attempts: list[dict[str, Any]] = []
    engine_plans: list[tuple[str, tuple[str, ...]]] = [
        ("opencode", (PRIMARY_OPENCODE_MODEL,)),
        ("agy", FALLBACK_AGY_MODELS),
    ]
    deepseek_passed_first_try = False
    triggered_gemini_fallback = False
    winning_result: dict[str, Any] | None = None
    world_b_info: dict[str, Any] | None = None
    world_b_verify: dict[str, Any] | None = None

    for engine_idx, (runner_name, models) in enumerate(engine_plans):
        if engine_idx > 0:
            triggered_gemini_fallback = True
            reset_a(world_a_ep)

        candidate = _run_engine_in_fresh_workspace(
            target=target,
            runner_name=runner_name,
            models=models,
            world_a_info=world_a_info,
            world_a_ep=world_a_ep,
            reset_a=reset_a,
            timeout_sec=timeout_sec,
        )
        all_attempts.extend(candidate.get("attempts") or [])
        if not candidate.get("passed"):
            continue

        # Gate D: Provision brand-new PK-shifted Hidden World B AFTER the FDE CLI exits!
        shift_pks = random.SystemRandom().randint(5, 11)
        with tempfile.TemporaryDirectory(prefix=f"wo24c_ref_{target.erp}_{target.domain}_") as ref_dir:
            ref_path = Path(ref_dir)
            drv_file = ref_path / "driver.py"
            map_file = ref_path / "mapping.json"
            drv_file.write_text(candidate["driver_source"], "utf-8")
            map_file.write_text(candidate["mapping_text"], "utf-8")

            world_b_info, world_b_ep, reset_b, oob_b = _provision_world(
                target=target,
                odoo_admin=odoo_admin,
                jsherp_admin=jsherp_admin,
                kind="hid",
                shift_pks=shift_pks,
            )
            reset_b(world_b_ep)
            if target.erp == "jsherp" and target.domain == "h2r":
                vb = verify_h2r_thin_driver(
                    target=target,
                    driver_path=drv_file,
                    mapping_path=map_file,
                    endpoint_config=world_b_ep,
                    forbidden_literals=world_a_info["fingerprints"],
                    max_driver_lines=350,
                    device_id=f"{target.device_id}_hid",
                    reset_fn=reset_b,
                    oob_tamper_check=oob_b,
                )
            else:
                vb = verify_thin_driver_on_hidden_world(
                    blueprints=[target.blueprint_arg],
                    seed=BUSINESS_SEED_ARG,
                    driver_path=drv_file,
                    mapping_path=map_file,
                    hidden_endpoint_config=world_b_ep,
                    forbidden_literals=world_a_info["fingerprints"],
                    max_driver_lines=350,
                    device_id=f"{target.device_id}_hid",
                    device_kind=target.device_kind,
                    owned_object_types=list(target.owned_object_types),
                    shared_object_types=list(target.shared_object_types),
                    handled_actions=list(target.handled_actions),
                    target_actions=list(target.handled_actions),
                    reset_fn=reset_b,
                    oob_tamper_check=oob_b,
                    min_action_coverage=target.min_auto_action_coverage,
                    min_transition_coverage=0.80,
                    max_waves=2,
                )

        oob_res = vb.get("oob_tamper_result") or {}
        oob_ok = (
            oob_res.get("passed") is True
            and oob_res.get("caught_error_code") == "DEVICE_STATE_MISMATCH"
            and oob_res.get("audit_status") == "REJECTED"
            and oob_res.get("abox_unchanged") is True
        )
        hid_ok = bool(vb.get("ok") and vb.get("verdict") == "CORROBORATED" and oob_ok)
        if hid_ok:
            winning_result = candidate
            world_b_verify = vb
            world_b_info["shift_pks"] = shift_pks
            if runner_name == "opencode" and len(candidate.get("attempts") or []) == 1:
                deepseek_passed_first_try = True
            break

        # Log Hidden World B rejection on the last attempt so telemetry stays 100% honest
        if all_attempts:
            all_attempts[-1]["hidden_world_b_ok"] = False
            all_attempts[-1]["hidden_world_b_errors"] = list(vb.get("errors") or [])[:5]
            all_attempts[-1]["hidden_world_b_oob"] = oob_res

    if winning_result is None or world_b_verify is None or world_b_info is None:
        raise RuntimeError(
            f"All AI-FDE engines failed blind 4-gate verification for {target.erp}/{target.domain}; "
            f"attempts={json.dumps(all_attempts, ensure_ascii=False)[:1500]}"
        )

    # Persist byte-for-byte identical copies to examples/adapters/<erp>/<domain>/
    out_domain_dir = persist_dir / target.erp / target.domain
    out_domain_dir.mkdir(parents=True, exist_ok=True)
    persisted_driver = out_domain_dir / "driver.py"
    persisted_mapping = out_domain_dir / "mapping.json"
    persisted_driver.write_bytes(winning_result["driver_source"].encode("utf-8"))
    persisted_mapping.write_bytes(winning_result["mapping_text"].encode("utf-8"))

    assert _sha256_path(persisted_driver) == winning_result["driver_sha256"]
    assert _sha256_path(persisted_mapping) == winning_result["mapping_sha256"]

    fal_a = (winning_result["world_a_verify"].get("falsification") or {})
    fal_b = (world_b_verify.get("falsification") or {})
    total_duration = round(sum(float(a.get("duration_seconds") or 0.0) for a in all_attempts), 3)
    total_cost = round(sum(float(a.get("total_cost_usd") or 0.0) for a in all_attempts), 6)

    return {
        "ok": True,
        "erp": target.erp,
        "domain": target.domain,
        "blueprint": target.blueprint_arg,
        "winning_runner": winning_result["fde_runner"],
        "chosen_model": winning_result["chosen_model"],
        "deepseek_passed_first_try": deepseek_passed_first_try,
        "triggered_gemini_fallback": triggered_gemini_fallback,
        "total_duration_seconds": total_duration,
        "total_cost_usd": total_cost,
        "attempts_count": len(all_attempts),
        "attempts": all_attempts,
        "driver_lines": winning_result["driver_lines"],
        "driver_sha256": winning_result["driver_sha256"],
        "mapping_sha256": winning_result["mapping_sha256"],
        "persisted_driver_path": str(persisted_driver.relative_to(ROOT)),
        "persisted_mapping_path": str(persisted_mapping.relative_to(ROOT)),
        "ast_audit_issues": winning_result["ast_issues"],
        "world_a": {
            "verdict": winning_result["world_a_verify"]["verdict"],
            "action_coverage_ratio": fal_a.get("action_coverage_ratio"),
            "transition_coverage_ratio": fal_a.get("transition_coverage_ratio"),
            "covered_actions": fal_a.get("covered_actions"),
        },
        "world_b_hidden": {
            "shift_pks": world_b_info.get("shift_pks"),
            "verdict": world_b_verify["verdict"],
            "action_coverage_ratio": fal_b.get("action_coverage_ratio"),
            "transition_coverage_ratio": fal_b.get("transition_coverage_ratio"),
            "covered_actions": fal_b.get("covered_actions"),
        },
        "oob_tamper": world_b_verify["oob_tamper_result"],
    }


def verify_persisted_domain_artifacts(
    *,
    erp: str,
    domain: str,
    persist_dir: Path = ROOT / "examples" / "adapters",
    odoo_admin: Odoo24cAdminClient | None = None,
    jsherp_admin: JshErp24cAdminClient | None = None,
) -> dict[str, Any]:
    """Re-verify an already persisted ``driver.py`` + ``mapping.json`` against fresh World A & Hidden World B."""
    target = DOMAIN_SPECS[(erp, domain)]
    domain_dir = persist_dir / erp / domain
    driver_path = domain_dir / "driver.py"
    mapping_path = domain_dir / "mapping.json"
    if not driver_path.is_file() or not mapping_path.is_file():
        return {"ok": False, "error": f"Missing persisted files in {domain_dir}"}

    world_a_info, world_a_ep, reset_a, _ = _provision_world(
        target=target, odoo_admin=odoo_admin, jsherp_admin=jsherp_admin, kind="pub", shift_pks=0
    )
    ast_issues = audit_thin_driver_ast(driver_path, forbidden_literals=world_a_info["fingerprints"], max_lines=350)
    if ast_issues:
        return {"ok": False, "ast_issues": ast_issues}

    if erp == "jsherp" and domain == "h2r":
        va = verify_h2r_thin_driver(
            target=target,
            driver_path=driver_path,
            mapping_path=mapping_path,
            endpoint_config=world_a_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id=f"{target.device_id}_pub_check",
            reset_fn=reset_a,
        )
    else:
        va = verify_thin_driver_on_hidden_world(
            blueprints=[target.blueprint_arg],
            seed=BUSINESS_SEED_ARG,
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config=world_a_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id=f"{target.device_id}_pub_check",
            device_kind=target.device_kind,
            owned_object_types=list(target.owned_object_types),
            shared_object_types=list(target.shared_object_types),
            handled_actions=list(target.handled_actions),
            target_actions=list(target.handled_actions),
            reset_fn=reset_a,
            min_action_coverage=target.min_auto_action_coverage,
            min_transition_coverage=0.80,
            max_waves=2,
        )

    shift_pks = random.SystemRandom().randint(5, 11)
    world_b_info, world_b_ep, reset_b, oob_b = _provision_world(
        target=target, odoo_admin=odoo_admin, jsherp_admin=jsherp_admin, kind="hid", shift_pks=shift_pks
    )
    reset_b(world_b_ep)
    if erp == "jsherp" and domain == "h2r":
        vb = verify_h2r_thin_driver(
            target=target,
            driver_path=driver_path,
            mapping_path=mapping_path,
            endpoint_config=world_b_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id=f"{target.device_id}_hid_check",
            reset_fn=reset_b,
            oob_tamper_check=oob_b,
        )
    else:
        vb = verify_thin_driver_on_hidden_world(
            blueprints=[target.blueprint_arg],
            seed=BUSINESS_SEED_ARG,
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config=world_b_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id=f"{target.device_id}_hid_check",
            device_kind=target.device_kind,
            owned_object_types=list(target.owned_object_types),
            shared_object_types=list(target.shared_object_types),
            handled_actions=list(target.handled_actions),
            target_actions=list(target.handled_actions),
            reset_fn=reset_b,
            oob_tamper_check=oob_b,
            min_action_coverage=target.min_auto_action_coverage,
            min_transition_coverage=0.80,
            max_waves=2,
        )

    return {
        "ok": bool(va.get("ok") and vb.get("ok") and (vb.get("oob_tamper_result") or {}).get("passed") is True),
        "world_a": va.get("verdict"),
        "world_b": vb.get("verdict"),
        "oob": vb.get("oob_tamper_result"),
        "errors": [*(va.get("errors") or []), *(vb.get("errors") or [])],
    }


def _merge_and_write_report(report_path: Path, erp: str, domain_results: Mapping[str, dict[str, Any]], cleanup_proof: dict[str, Any]) -> dict[str, Any]:
    existing_domains: dict[str, Any] = {}
    if report_path.is_file():
        try:
            old = json.loads(report_path.read_text("utf-8"))
            if isinstance(old.get("domains"), dict):
                existing_domains.update(old["domains"])
        except Exception:
            pass
    existing_domains.update(domain_results)
    payload = {
        "ok": all(bool(v.get("ok")) for v in existing_domains.values()),
        "erp": erp,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "domains": existing_domains,
        "cleanup": cleanup_proof,
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    return payload


def run_orchestrator(
    *,
    erp: str = "all",
    domains: Sequence[str] | None = None,
    timeout_sec: int = 480,
    persist_dir: Path = ROOT / "examples" / "adapters",
    odoo_report_path: Path = Path("/tmp/wo24c_odoo19_fde_report.json"),
    jsherp_report_path: Path = Path("/tmp/wo24c_jsherp_fde_report.json"),
) -> dict[str, Any]:
    _ensure_local_no_proxy()
    summary: dict[str, Any] = {}

    if erp in ("odoo19", "all"):
        target_domains = tuple(domains) if domains else ODOO_DOMAINS
        admin = Odoo24cAdminClient(ODOO_URL)
        odoo_res: dict[str, dict[str, Any]] = {}
        try:
            for dom in target_domains:
                spec_obj = DOMAIN_SPECS[("odoo19", dom)]
                print(f"[WO-24C] Running blind AI-FDE for odoo19/{dom}...", flush=True)
                res = run_blind_domain(target=spec_obj, persist_dir=persist_dir, odoo_admin=admin, timeout_sec=timeout_sec)
                odoo_res[dom] = res
                print(f"[WO-24C] DONE odoo19/{dom}: model={res['chosen_model']} lines={res['driver_lines']} sha={res['driver_sha256'][:12]}", flush=True)
        finally:
            final_dbs = admin.drop_all_wo24c_dbs()
        assert final_dbs == ["erp_north", "erp_south", "odoo_demo"], f"Odoo cleanup violation: {final_dbs}"
        summary["odoo19"] = _merge_and_write_report(odoo_report_path, "odoo19", odoo_res, {"final_odoo_dbs": final_dbs, "residual_wo24c_pg_dbs": []})

    if erp in ("jsherp", "all"):
        target_domains = tuple(domains) if domains else JSHERP_DOMAINS
        jadmin = JshErp24cAdminClient(JSHERP_BASE_URL)
        jsh_res: dict[str, dict[str, Any]] = {}
        try:
            for dom in target_domains:
                spec_obj = DOMAIN_SPECS[("jsherp", dom)]
                print(f"[WO-24C] Running blind AI-FDE for jsherp/{dom}...", flush=True)
                res = run_blind_domain(target=spec_obj, persist_dir=persist_dir, jsherp_admin=jadmin, timeout_sec=timeout_sec)
                jsh_res[dom] = res
                print(f"[WO-24C] DONE jsherp/{dom}: model={res['chosen_model']} lines={res['driver_lines']} sha={res['driver_sha256'][:12]}", flush=True)
        finally:
            cleaned = jadmin.cleanup_all_wo24c_tenants()
            rem = jadmin.remaining_wo24c_counts()
            if rem["jsh_user"] != 0 or rem["jsh_tenant"] != 0:
                raise RuntimeError(f"jshERP cleanup residual rows: {rem} (cleaned={cleaned})")
        summary["jsherp"] = _merge_and_write_report(jsherp_report_path, "jsherp", jsh_res, {"cleaned_tenants": cleaned, "remaining_wo24c_counts": rem})

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WO-24C Live Dual-ERP Blind AI-FDE Orchestrator")
    parser.add_argument("--erp", choices=("odoo19", "jsherp", "all"), default="all")
    parser.add_argument("--domain", action="append", default=None, help="Domain(s) to run (repeatable)")
    parser.add_argument("--timeout-sec", type=int, default=480)
    parser.add_argument("--persist-dir", default=str(ROOT / "examples" / "adapters"))
    parser.add_argument("--odoo-report", default="/tmp/wo24c_odoo19_fde_report.json")
    parser.add_argument("--jsherp-report", default="/tmp/wo24c_jsherp_fde_report.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    summary = run_orchestrator(
        erp=args.erp,
        domains=args.domain,
        timeout_sec=args.timeout_sec,
        persist_dir=Path(args.persist_dir),
        odoo_report_path=Path(args.odoo_report),
        jsherp_report_path=Path(args.jsherp_report),
    )
    compact = {
        erp_k: {
            dom_k: {
                "winning_runner": d["winning_runner"],
                "chosen_model": d["chosen_model"],
                "deepseek_1st": d["deepseek_passed_first_try"],
                "gemini_fallback": d["triggered_gemini_fallback"],
                "lines": d["driver_lines"],
                "driver_sha256": d["driver_sha256"][:16],
                "world_a": d["world_a"]["action_coverage_ratio"],
                "world_b": d["world_b_hidden"]["action_coverage_ratio"],
                "oob": d["oob_tamper"]["caught_error_code"],
            }
            for dom_k, d in erp_v["domains"].items()
        }
        for erp_k, erp_v in summary.items()
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
