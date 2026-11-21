#!/usr/bin/env python3
"""WO-18 Full-Stack Heterogeneous B2B Steel Trade Laboratory.

Architecture:
- Enterprise A (Buyer · Tangshan Steel Trading):
  * Real Huaxia ERP (``jshERP`` :19999) isolated tenant ``wo18_ent_a_*`` (or offline mirror)
  * Real PostgreSQL 16 (:15432) isolated schema ``wo18_ent_a_*`` (or SQLite in offline mirror)
  * Pure ``std_p2p_ontology`` runtime (zero ``O2c*``/``Crm*`` awareness)
  * ``SagaReconciler`` + ``AutonomyRampController`` (MCP) + ``MDMSidecarPlugin``
  * Real isolated local ``hermes`` CLI (``openrouter/deepseek/deepseek-v4.1-flash``) over Streamable HTTP MCP
- Cross-Enterprise Boundary:
  * Strictly ``examples/commonsense_exchange.py`` 5-primitive REA JSON envelopes
    (``BusinessParty``, ``TradeResource``, ``TradeCommitment``, ``FulfillmentEvent``, ``WorkCard``)
    hard-validated by ``validate_commonsense_envelope`` on every hop
- Enterprise B (Seller · Upstream Steel Mill Group):
  * Real Live Odoo 19 (:8069) isolated cloned DB ``wo18_ent_b_*`` + cloned BottleCRM SQLite
    multiplexed on ``DeviceDriverRegistry``
  * Real PostgreSQL 16 (:15432) isolated schema ``wo18_ent_b_*``
  * ``std_o2c_ontology + std_crm_ontology + CustomerOverlaySpec(URGENT_TICKET_BLOCKS_ORDER)``
  * ``SagaReconciler`` + Streamable HTTP ``OntologyMCPServer`` (actor pinned ``sales_rep``)
  * Real isolated local ``hermes`` CLI (``openrouter/deepseek/deepseek-v4.1-flash``) over Streamable HTTP MCP
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402

from open_ontology import (  # noqa: E402
    DEVICE_STATE_MISMATCH,
    Actor,
    DeviceDriverRegistry,
    DeviceStateMismatchError,
    InvariantError,
    MountedDevice,
    OntologyRuntime,
    OntologySpec,
    SourceBridge,
)
from open_ontology.autonomy import (  # noqa: E402
    ActionAutonomyRule,
    AutonomyPolicy,
    AutonomyRampController,
)
from open_ontology.induction import (  # noqa: E402
    AdapterMappingSpec,
    CustomerOverlaySpec,
    EntityMappingSpec,
    EntityResolutionDecision,
    EntityResolutionSpec,
    FieldTransformSpec,
    InvariantRuleSpec,
    SourceEntityBatch,
    SourceRef,
    apply_overlay,
    compile_blueprint,
    compose_blueprints,
)
from open_ontology.mcp_server import OntologyMCPServer, start_mcp_http_server  # noqa: E402
from open_ontology.mdm_plugin import MDMSidecarPlugin  # noqa: E402
from open_ontology.postgres_backend import open_postgres_runtime  # noqa: E402
from open_ontology.reconciliation import SagaReconciler  # noqa: E402

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY  # noqa: E402
from examples.commonsense_exchange import (  # noqa: E402
    CommonsenseEnvelope,
    O2cCommonsensePort,
    P2pCommonsensePort,
    validate_commonsense_envelope,
)
from examples.odoo_dual_erp_demo import MASTER_CANDIDATES, ODOO_PASSWORD as ODOO_PASS, ODOO_USER  # noqa: E402
from examples.std_core_ontology import build_std_core_blueprint  # noqa: E402
from examples.std_crm_ontology import build_std_crm_blueprint  # noqa: E402
from examples.std_o2c_ontology import build_std_o2c_blueprint  # noqa: E402
from examples.std_p2p_ontology import build_std_p2p_blueprint  # noqa: E402

PG_DSN = os.environ.get(
    "OPEN_ONTOLOGY_PG_DSN",
    "postgresql://open_ontology:open_ontology_dev@127.0.0.1:15432/open_ontology",
)
JSHERP_BASE_URL = os.environ.get("JSHERP_BASE_URL", "http://127.0.0.1:19999/jshERP-boot").rstrip("/")
ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069/jsonrpc")
SOURCE_DEMO_DB = "odoo_demo"
SOURCE_BOTTLECRM_DB = REPO_ROOT / "vendor_crm" / "bottlecrm.sqlite3"
OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
HERMES_BIN_DEFAULT = "/Users/guci/.local/bin/hermes"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


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


def _preflight_openrouter_deepseek() -> dict[str, str]:
    api_key = _read_secret_readonly("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not found in environment or ~/.hermes/.env (read-only lookup)")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(
            {
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 4,
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    return {"OPENROUTER_API_KEY": api_key}
                raise RuntimeError(f"OpenRouter preflight HTTP {resp.status}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_exc = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 402, 403):
                break
            if attempt < 2:
                time.sleep(1.0)
    raise RuntimeError(
        f"OpenRouter {OPENROUTER_MODEL} preflight failed: {type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def _scrub_secrets(text: str, secrets: Sequence[str]) -> str:
    cleaned = text
    for sec in secrets:
        if sec and len(sec) >= 6:
            cleaned = cleaned.replace(sec, "***REDACTED***")
    return cleaned


# ---------------------------------------------------------------------------
# Strict Enterprise Ontology Spec Builders (A: P2P only, B: O2C + CRM + Overlay)
# ---------------------------------------------------------------------------
def build_buyer_enterprise_spec() -> OntologySpec:
    """Enterprise A uses strictly compiled ``std_core + std_p2p`` (zero O2c/Crm concepts)."""
    composed = compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_p2p_blueprint(),
        ],
        model_id="std_p2p_ontology",
        version="1.0.0",
    )
    compiled = compile_blueprint(composed, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    assert not any(t.startswith(("O2c", "Crm")) for t in compiled.object_types)
    assert not any("_o2c_" in a or "_crm_" in a for a in compiled.actions)
    return compiled


def build_seller_enterprise_spec() -> OntologySpec:
    """Enterprise B uses ``std_o2c + std_crm + CustomerOverlaySpec`` (zero P2p concepts)."""
    base = compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
        ],
        model_id="wo18_seller_o2c_crm",
        version="1.0.0",
    )
    overlay = CustomerOverlaySpec(
        overlay_id="steel_seller_urgent_ticket_blocker",
        base_model_id=base.model_id,
        target_version="1.1.0",
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="block_order_confirm_when_urgent_crm_ticket_open",
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
                    description="Open HIGH/URGENT CrmTicket blocks confirm_o2c_order.",
                )
            ]
        },
        restrict_allowed_roles={
            "resolve_crm_ticket": ["support_agent", "ops", "steward", "admin"],
        },
    )
    governed = apply_overlay(base, overlay)
    spec = compile_blueprint(governed, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    assert not any(t.startswith("P2p") for t in spec.object_types)
    assert not any("_p2p_" in a for a in spec.actions)
    return spec


# ---------------------------------------------------------------------------
# BottleCRM SQLite Temp Copy & Driver (Enterprise B CRM Device)
# ---------------------------------------------------------------------------
_BOTTLE_STATUS_TO_CRM = {
    "New": "OPEN",
    "Open": "OPEN",
    "In Progress": "IN_PROGRESS",
    "Assigned": "IN_PROGRESS",
    "Closed": "RESOLVED",
    "Resolved": "RESOLVED",
}


class BottleCrmSqliteDriver:
    """Cloned ``vendor_crm/bottlecrm.sqlite3`` driver supporting managed CRM rows & readback."""

    def __init__(self, sqlite_path: Path, managed_parties: Sequence[dict[str, str]]) -> None:
        self.sqlite_path = sqlite_path
        self.writes = 0
        self.accounts_by_raw: dict[str, dict[str, str]] = {}
        self.tickets_by_raw: dict[str, dict[str, str]] = {}
        self.raw_by_ticket_id: dict[str, str] = {}
        self._parties = {str(p["party_id"]): dict(p) for p in managed_parties}

        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            org_row = conn.execute("SELECT org_id, created_by_id FROM accounts LIMIT 1").fetchone()
            self._org_id = str(org_row["org_id"]) if org_row and org_row["org_id"] else uuid.uuid4().hex
            self._user_id = str(org_row["created_by_id"]) if org_row and org_row["created_by_id"] else uuid.uuid4().hex
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

            # Seed deterministic isolated test accounts + URGENT ticket inside the ephemeral copy
            acc_seeds = [
                {
                    "raw_id": uuid.uuid4().hex,
                    "account_id": "ACC-BUYER-TS",
                    "name": "唐山钢铁供应链有限公司",
                    "party": "PTY-BUYER-TS",
                    "industry": "SteelTrade",
                },
                {
                    "raw_id": uuid.uuid4().hex,
                    "account_id": "ACC-BUYER-RISK",
                    "name": "江海重工钢构采购部",
                    "party": "PTY-BUYER-RISK",
                    "industry": "HeavySteel",
                },
                {
                    "raw_id": uuid.uuid4().hex,
                    "account_id": "ACC-MDM-XY",
                    "name": "唐山鑫源冶金材料厂",
                    "party": "PTY-CRM-XY",
                    "industry": "Metallurgy",
                },
            ]
            for acc in acc_seeds:
                conn.execute(
                    "INSERT INTO accounts (id, name, industry, description, is_active, custom_fields, created_at, updated_at, created_by_id, org_id, updated_by_id, is_sample) "
                    "VALUES (?, ?, ?, ?, 1, '{}', ?, ?, ?, ?, ?, 0)",
                    (
                        acc["raw_id"],
                        acc["name"],
                        acc["industry"],
                        f"party={acc['party']}",
                        now_iso,
                        now_iso,
                        self._user_id,
                        self._org_id,
                        self._user_id,
                    ),
                )
                self.accounts_by_raw[acc["raw_id"]] = acc

            urg_raw_id = uuid.uuid4().hex
            risk_acc_raw = acc_seeds[1]["raw_id"]
            conn.execute(
                'INSERT INTO "case" (id, name, status, priority, case_type, description, is_active, account_id, created_at, updated_at, created_by_id, org_id, updated_by_id, kanban_order, custom_fields, escalation_count, alt_thread_ids, sla_paused_seconds, is_problem, is_sample) '
                "VALUES (?, ?, 'New', 'URGENT', 'Quality', '', 1, ?, ?, ?, ?, ?, ?, 0, '{}', 0, '[]', 0, 0, 0)",
                (
                    urg_raw_id,
                    "上批HRB400E螺纹钢屈服强度复检异常投诉",
                    risk_acc_raw,
                    now_iso,
                    now_iso,
                    self._user_id,
                    self._org_id,
                    self._user_id,
                ),
            )
            self.tickets_by_raw[urg_raw_id] = {
                "raw_id": urg_raw_id,
                "ticket_id": "TK-CRM-URG-301",
                "account": "ACC-BUYER-RISK",
            }
            self.raw_by_ticket_id["TK-CRM-URG-301"] = urg_raw_id
            conn.commit()
        finally:
            conn.close()

    def seed_into_runtime(self, rt: OntologyRuntime, admin: Actor) -> None:
        obs = self.observe()
        acc_cls = rt.spec.get_object_type("CrmAccount")
        tk_cls = rt.spec.get_object_type("CrmTicket")
        pty_cls = rt.spec.get_object_type("BusinessParty")
        for p in obs.get("BusinessParty", []):
            rt.put_object(pty_cls(**p), actor=admin)
        for a in obs.get("CrmAccount", []):
            rt.put_object(acc_cls(**a), actor=admin)
        for t in obs.get("CrmTicket", []):
            rt.put_object(tk_cls(**t), actor=admin)

    def extract_mdm_source_rows(self) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            raw_ids = list(self.accounts_by_raw.keys())
            placeholders = ",".join("?" for _ in raw_ids)
            rows = conn.execute(
                f"SELECT id, name, description FROM accounts WHERE id IN ({placeholders}) ORDER BY name",
                raw_ids,
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                acc_meta = self.accounts_by_raw[str(r["id"])]
                pty_id = acc_meta["party"]
                tax_id = self._parties.get(pty_id, {}).get("tax_id", "")
                out.append(
                    {
                        "id": str(r["id"]),
                        "legal_name": str(r["name"]),
                        "tax_id": tax_id,
                        "country_code": "CN",
                    }
                )
            return out
        finally:
            conn.close()

    def actuate(self, move: dict[str, Any]) -> Any:
        action = str(move.get("action_type") or "")
        params = dict(move.get("params") or {})
        if action == "resolve_crm_ticket":
            tid = str(params["ticket_id"])
            summary = str(params.get("resolution_summary") or "Resolved")
            raw_id = self.raw_by_ticket_id[tid]
            conn = sqlite3.connect(self.sqlite_path)
            try:
                conn.execute(
                    'UPDATE "case" SET status = ?, description = ?, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                    ("Closed", summary, raw_id),
                )
                conn.commit()
                self.writes += 1
            finally:
                conn.close()
            return {"ok": True, "ticket_id": tid}
        raise ValueError(f"unsupported BottleCRM action {action!r}")

    def observe(self, _move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            acc_ids = list(self.accounts_by_raw.keys())
            acc_ph = ",".join("?" for _ in acc_ids)
            acc_rows = conn.execute(
                f"SELECT id, name, industry, is_active FROM accounts WHERE id IN ({acc_ph})",
                acc_ids,
            ).fetchall()

            tk_ids = list(self.tickets_by_raw.keys())
            tk_ph = ",".join("?" for _ in tk_ids)
            tk_rows = (
                conn.execute(
                    f'SELECT id, name, status, priority, description, account_id FROM "case" WHERE id IN ({tk_ph})',
                    tk_ids,
                ).fetchall()
                if tk_ids
                else []
            )

            accounts_out: list[dict[str, Any]] = []
            parties_out: list[dict[str, Any]] = []
            seen_pty: set[str] = set()
            for r in acc_rows:
                meta = self.accounts_by_raw[str(r["id"])]
                pty_id = meta["party"]
                if pty_id not in seen_pty and pty_id in self._parties:
                    seen_pty.add(pty_id)
                    parties_out.append(dict(self._parties[pty_id]))
                accounts_out.append(
                    {
                        "account_id": meta["account_id"],
                        "name": str(r["name"]),
                        "party": pty_id,
                        "industry": str(r["industry"] or ""),
                        "status": "active" if int(r["is_active"] or 0) else "inactive",
                    }
                )

            tickets_out: list[dict[str, Any]] = []
            for r in tk_rows:
                meta = self.tickets_by_raw[str(r["id"])]
                raw_status = str(r["status"] or "New")
                prio = str(r["priority"] or "MEDIUM").upper()
                if prio not in ("LOW", "MEDIUM", "HIGH", "URGENT"):
                    prio = "MEDIUM"
                tickets_out.append(
                    {
                        "ticket_id": meta["ticket_id"],
                        "subject": str(r["name"]),
                        "account": meta["account"],
                        "priority": prio,
                        "status": _BOTTLE_STATUS_TO_CRM.get(raw_status, "OPEN"),
                        "resolution_summary": str(r["description"] or ""),
                    }
                )

            return {
                "BusinessParty": parties_out,
                "CrmAccount": accounts_out,
                "CrmTicket": tickets_out,
            }
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Offline Simulators (Zero-Docker Mirror for pytest CI)
# ---------------------------------------------------------------------------
class OfflineJshErpSimulator:
    """In-memory deterministic mirror of jshERP P2P tenant state machine."""

    def __init__(self) -> None:
        self.raw_actuate_calls = 0
        self.inject_post_commit_timeout_for_po: str | None = None
        self.entities: dict[str, dict[str, Any]] = {
            "ENT-BUYER-TS": {
                "entity_id": "ENT-BUYER-TS",
                "name": "唐山钢铁供应链有限公司",
                "base_currency": "CNY",
                "status": "active",
            }
        }
        self.parties: dict[str, dict[str, Any]] = {
            "PTY-BUYER-TS": {
                "party_id": "PTY-BUYER-TS",
                "legal_name": "唐山钢铁供应链有限公司",
                "tax_id": "91130200BUYER001",
                "country_code": "CN",
                "status": "active",
            },
            "PTY-BUYER-RISK": {
                "party_id": "PTY-BUYER-RISK",
                "legal_name": "江海重工钢构采购部",
                "tax_id": "91320000RISK002",
                "country_code": "CN",
                "status": "active",
            },
            "PTY-SELLER-HB": {
                "party_id": "PTY-SELLER-HB",
                "legal_name": "河北华北特钢供应有限公司",
                "tax_id": "91130200STEEL001",
                "country_code": "CN",
                "status": "active",
            },
        }
        self.suppliers: dict[str, dict[str, Any]] = {
            "SUP-HB": {
                "profile_id": "SUP-HB",
                "party": "PTY-SELLER-HB",
                "accounting_entity": "ENT-BUYER-TS",
                "payment_terms": "NET30",
                "status": "active",
            }
        }
        self.extra_mdm_suppliers = [
            {"id": "101", "legal_name": "河北华北特钢供应有限公司", "tax_id": "91130200STEEL001", "country_code": "CN"},
            {"id": "102", "legal_name": "唐山鑫源冶金炉料厂", "tax_id": "", "country_code": "CN"},
        ]
        self.pos: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.bills: dict[str, dict[str, Any]] = {}

    def seed_flow(self, flow_key: str, *, po_number: str, qty: float, unit_price: float) -> None:
        total = round(qty * unit_price, 2)
        po_id = f"PO-{flow_key}"
        rec_id = f"REC-{flow_key}"
        bill_id = f"BILL-{flow_key}"
        self.pos[po_id] = {
            "po_id": po_id,
            "po_number": po_number,
            "supplier": "SUP-HB",
            "accounting_entity": "ENT-BUYER-TS",
            "ordered_qty": float(qty),
            "unit_price": float(unit_price),
            "amount_total": total,
            "status": "draft",
        }
        self.receipts[rec_id] = {
            "receipt_id": rec_id,
            "purchase_order": po_id,
            "expected_qty": float(qty),
            "received_qty": 0.0,
            "inspection_status": "pending",
            "status": "pending",
        }
        self.bills[bill_id] = {
            "bill_id": bill_id,
            "purchase_order": po_id,
            "amount_total": total,
            "amount_residual": total,
            "payment_state": "not_paid",
            "status": "draft",
        }

    def oob_cancel_po(self, po_id: str) -> None:
        self.pos[po_id]["status"] = "cancelled"

    def extract_mdm_supplier_batch(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.extra_mdm_suppliers)

    def actuate(self, move: dict[str, Any]) -> Any:
        self.raw_actuate_calls += 1
        action = str(move.get("action_type") or "")
        params = dict(move.get("params") or {})

        if action == "confirm_p2p_order":
            po_id = str(params["po_id"])
            po = self.pos.get(po_id)
            if po is not None and po["status"] == "draft":
                po["status"] = "confirmed"
            if self.inject_post_commit_timeout_for_po == po_id:
                self.inject_post_commit_timeout_for_po = None
                raise TimeoutError(f"Simulated jshERP ACK timeout after committing {po_id}")
            return {"ok": True}

        if action == "record_p2p_receipt":
            rec = self.receipts[str(params["receipt_id"])]
            po = self.pos[rec["purchase_order"]]
            rqty = float(params["received_qty"])
            insp = str(params.get("inspection_status") or "passed")
            disp = rqty < float(rec["expected_qty"]) or insp == "failed"
            rec["received_qty"] = rqty
            rec["inspection_status"] = insp
            rec["status"] = "disputed" if disp else "completed"
            po["status"] = "disputed" if disp else "received"
            return {"ok": True}

        if action == "resolve_p2p_dispute":
            po = self.pos[str(params["po_id"])]
            rec = self.receipts[str(params["receipt_id"])]
            po["amount_total"] = float(params["settled_amount"])
            po["status"] = "received"
            rec["status"] = "completed"
            return {"ok": True}

        if action == "reconcile_p2p_bill":
            bill = self.bills[str(params["bill_id"])]
            po = self.pos[bill["purchase_order"]]
            bill["amount_total"] = float(po["amount_total"])
            bill["amount_residual"] = float(po["amount_total"])
            return {"ok": True}

        if action == "post_p2p_bill":
            bill = self.bills[str(params["bill_id"])]
            po = self.pos[bill["purchase_order"]]
            bill["status"] = "posted"
            po["status"] = "billed"
            return {"ok": True}

        if action == "record_p2p_payment":
            bill = self.bills[str(params["bill_id"])]
            amt = float(params["payment_amount"])
            res = max(0.0, round(float(bill["amount_residual"]) - amt, 2))
            bill["amount_residual"] = res
            bill["payment_state"] = "paid" if res <= 1e-9 else "partial"
            return {"ok": True}

        raise ValueError(f"unsupported OfflineJshErpSimulator action {action!r}")

    def observe(self, _move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        return {
            "AccountingEntity": [copy.deepcopy(v) for v in self.entities.values()],
            "BusinessParty": [copy.deepcopy(v) for v in self.parties.values()],
            "P2pSupplierProfile": [copy.deepcopy(v) for v in self.suppliers.values()],
            "P2pPurchaseOrder": [copy.deepcopy(v) for v in self.pos.values()],
            "P2pGoodsReceipt": [copy.deepcopy(v) for v in self.receipts.values()],
            "P2pVendorBill": [copy.deepcopy(v) for v in self.bills.values()],
        }

    def as_source_bridge(self) -> SourceBridge:
        return SourceBridge(actuate=self.actuate, observe=self.observe)


class OfflineOdooO2cSimulator:
    """In-memory deterministic mirror of Odoo 19 O2C device (for offline pytest mirror)."""

    def __init__(self, managed_parties: Sequence[dict[str, str]]) -> None:
        self.writes = 0
        self.entities = {
            "ENT-SELLER-HB": {
                "entity_id": "ENT-SELLER-HB",
                "name": "河北华北特钢供应有限公司",
                "base_currency": "CNY",
                "status": "active",
            }
        }
        self.parties = {str(p["party_id"]): dict(p) for p in managed_parties}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.extra_mdm_partners = [
            {"id": "201", "legal_name": "华北特钢股份有限公司", "tax_id": "91130200STEEL001", "country_code": "CN"}
        ]

    def ensure_customer_profile(self, *, profile_id: str, party_id: str, credit_limit: float = 2_000_000.0) -> None:
        if profile_id not in self.profiles:
            self.profiles[profile_id] = {
                "profile_id": profile_id,
                "party": party_id,
                "accounting_entity": "ENT-SELLER-HB",
                "credit_limit": float(credit_limit),
                "payment_terms": "NET30",
                "status": "active",
            }

    def create_draft_order(self, *, order_id: str, order_number: str, customer_profile_id: str, qty: float, unit_price: float) -> None:
        self.orders[order_id] = {
            "order_id": order_id,
            "order_number": order_number,
            "customer": customer_profile_id,
            "accounting_entity": "ENT-SELLER-HB",
            "amount_total": round(qty * unit_price, 2),
            "status": "draft",
        }

    def extract_mdm_partner_batch(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.extra_mdm_partners)

    def raw_order_state(self, order_id: str) -> str:
        return str(self.orders[order_id]["status"])

    def actuate(self, move: dict[str, Any]) -> Any:
        self.writes += 1
        action = str(move.get("action_type") or "")
        params = dict(move.get("params") or {})
        changes = (move.get("object_diff") or {}).get("object_changes") or []

        if action == "confirm_o2c_order":
            oid = str(params["order_id"])
            self.orders[oid]["status"] = "confirmed"
            return {"ok": True}

        if action == "complete_o2c_delivery":
            did = str(params["delivery_id"])
            if did not in self.deliveries:
                for ch in changes:
                    if ch.get("object_type") == "O2cDelivery" and ch.get("object_id") == did:
                        before = ch.get("before") or {}
                        self.deliveries[did] = {
                            "delivery_id": did,
                            "order": str(before["order"]),
                            "status": "pending",
                        }
                        break
            deliv = self.deliveries[did]
            deliv["status"] = "completed"
            self.orders[deliv["order"]]["status"] = "delivered"
            return {"ok": True}

        if action == "post_o2c_invoice":
            iid = str(params["invoice_id"])
            if iid not in self.invoices:
                for ch in changes:
                    if ch.get("object_type") == "O2cInvoice" and ch.get("object_id") == iid:
                        before = ch.get("before") or {}
                        amt = float(before["amount_total"])
                        self.invoices[iid] = {
                            "invoice_id": iid,
                            "order": str(before["order"]),
                            "amount_total": amt,
                            "amount_residual": amt,
                            "payment_state": "not_paid",
                            "status": "draft",
                        }
                        break
            inv = self.invoices[iid]
            inv["status"] = "posted"
            self.orders[inv["order"]]["status"] = "invoiced"
            return {"ok": True}

        if action == "record_o2c_payment":
            inv = self.invoices[str(params["invoice_id"])]
            amt = float(params["payment_amount"])
            res = max(0.0, round(float(inv["amount_residual"]) - amt, 2))
            inv["amount_residual"] = res
            inv["payment_state"] = "paid" if res <= 1e-9 else "partial"
            return {"ok": True}

        raise ValueError(f"unsupported OfflineOdooO2cSimulator action {action!r}")

    def observe(self, _move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        return {
            "AccountingEntity": [copy.deepcopy(v) for v in self.entities.values()],
            "BusinessParty": [copy.deepcopy(v) for v in self.parties.values()],
            "O2cCustomerProfile": [copy.deepcopy(v) for v in self.profiles.values()],
            "O2cOrder": [copy.deepcopy(v) for v in self.orders.values()],
            "O2cDelivery": [copy.deepcopy(v) for v in self.deliveries.values()],
            "O2cInvoice": [copy.deepcopy(v) for v in self.invoices.values()],
        }

    def as_source_bridge(self) -> SourceBridge:
        return SourceBridge(actuate=self.actuate, observe=self.observe)


# ---------------------------------------------------------------------------
# Live Container Drivers: jshERP (:19999) + Odoo 19 (:8069)
# ---------------------------------------------------------------------------
def _jsherp_http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    token: str | None = None,
    timeout: int = 25,
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


class LiveJshErpAdmin:
    """Tenant lifecycle & strict ``wo18_%`` MySQL/Redis isolation controller."""

    def __init__(self, base_url: str = JSHERP_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.created_tenants: list[dict[str, Any]] = []

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

    def login(self, login_name: str, password_md5: str) -> tuple[str, int]:
        st, resp = _jsherp_http_json(
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
        if tid < 100:
            raise ValueError(f"Refusing non-test tenant_id={tid}")
        return tok, tid

    def register_tenant(self, login_name: str, password_plain: str) -> dict[str, Any]:
        if not re.fullmatch(r"wo18_[a-z0-9_]+", login_name):
            raise ValueError(f"Refusing non-wo18 login_name {login_name!r}")
        pw_md5 = hashlib.md5(password_plain.encode("utf-8")).hexdigest()
        st, reg = _jsherp_http_json(
            f"{self.base_url}/user/registerUser",
            method="POST",
            body={"loginName": login_name, "password": pw_md5},
        )
        if st != 200 or reg.get("code") != 200:
            raise RuntimeError(f"registerUser failed for {login_name!r}: {st} {reg}")
        tok, tid = self.login(login_name, pw_md5)
        info = {
            "tenant_id": tid,
            "login_name": login_name,
            "password_plain": password_plain,
            "password_md5": pw_md5,
            "token": tok,
        }
        self.created_tenants.append(info)
        return info

    def cleanup_all_wo18_tenants(self) -> list[int]:
        raw_tids = self._mysql_exec(
            "SELECT DISTINCT id FROM jsh_user WHERE login_name LIKE 'wo18_%' "
            "UNION SELECT DISTINCT tenant_id FROM jsh_tenant WHERE login_name LIKE 'wo18_%';"
        )
        tids = sorted({int(x.strip()) for x in raw_tids.splitlines() if x.strip() and int(x.strip()) >= 100})
        for meta in self.created_tenants:
            tid = int(meta["tenant_id"])
            if tid >= 100 and tid not in tids:
                tids.append(tid)
        if not tids:
            return []

        in_list = ",".join(str(t) for t in tids)
        tbl_out = self._mysql_exec(
            "SELECT TABLE_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA='jsh_erp' AND COLUMN_NAME='tenant_id' ORDER BY TABLE_NAME;"
        )
        stmts = ["SET FOREIGN_KEY_CHECKS=0;"]
        for tbl in [line.strip() for line in tbl_out.splitlines() if line.strip()]:
            stmts.append(f"DELETE FROM `{tbl}` WHERE tenant_id IN ({in_list});")
        stmts.append(f"DELETE FROM jsh_user WHERE id IN ({in_list});")
        stmts.append("SET FOREIGN_KEY_CHECKS=1;")
        self._mysql_exec("", stdin_sql="\n".join(stmts) + "\n")

        keys_proc = subprocess.run(
            ["docker", "exec", "jsherp-redis", "redis-cli", "-a", "1234abcd", "KEYS", "*"],
            text=True,
            capture_output=True,
            check=False,
        )
        tid_strs = {str(t) for t in tids}
        for key in (keys_proc.stdout or "").splitlines():
            k = key.strip()
            if not k or k == "63":
                continue
            val_proc = subprocess.run(
                ["docker", "exec", "jsherp-redis", "redis-cli", "-a", "1234abcd", "GET", k],
                text=True,
                capture_output=True,
                check=False,
            )
            if (val_proc.stdout or "").strip() in tid_strs:
                subprocess.run(
                    ["docker", "exec", "jsherp-redis", "redis-cli", "-a", "1234abcd", "DEL", k],
                    text=True,
                    capture_output=True,
                    check=False,
                )
        return tids

    def remaining_wo18_counts(self) -> dict[str, int]:
        u = int(self._mysql_exec("SELECT count(*) FROM jsh_user WHERE login_name LIKE 'wo18_%';").strip())
        t = int(self._mysql_exec("SELECT count(*) FROM jsh_tenant WHERE login_name LIKE 'wo18_%';").strip())
        return {"jsh_user": u, "jsh_tenant": t}


class LiveJshErpP2pAdapter:
    """Real jshERP REST SourceBridge for Enterprise A (respects 1s write guard & live readback)."""

    def __init__(self, admin: LiveJshErpAdmin, tenant_info: dict[str, Any]) -> None:
        self._lock = threading.RLock()
        self.admin = admin
        self.base_url = admin.base_url
        self.login_name = str(tenant_info["login_name"])
        self.password_md5 = str(tenant_info["password_md5"])
        self.tenant_id = int(tenant_info["tenant_id"])
        self.token = str(tenant_info["token"])
        self._last_write_ts = 0.0
        self.raw_actuate_calls = 0
        self.inject_post_commit_timeout_for_po: str | None = None

        self.company_name = "唐山钢铁供应链有限公司"
        self.depot_id = 0
        self.account_id = 0
        self.mat_id = 0
        self.me_id = 0
        self.mat_name = "HRB400E螺纹钢 Φ20mm"
        self.mat_barcode = "HRB400E-20"
        self.oper_time = "2026-11-22 10:00:00"

        self.supplier_id = 0
        self.party_map = {
            "PTY-BUYER-TS": {
                "party_id": "PTY-BUYER-TS",
                "legal_name": "唐山钢铁供应链有限公司",
                "tax_id": "91130200BUYER001",
                "country_code": "CN",
                "status": "active",
            },
            "PTY-BUYER-RISK": {
                "party_id": "PTY-BUYER-RISK",
                "legal_name": "江海重工钢构采购部",
                "tax_id": "91320000RISK002",
                "country_code": "CN",
                "status": "active",
            },
            "PTY-SELLER-HB": {
                "party_id": "PTY-SELLER-HB",
                "legal_name": "河北华北特钢供应有限公司",
                "tax_id": "91130200STEEL001",
                "country_code": "CN",
                "status": "active",
            },
        }
        self.flows: dict[str, dict[str, Any]] = {}  # flow_key -> ids/numbers/expected_qty
        self._seed_master_data()

    def _pace_write(self) -> None:
        wait = 1.08 - (time.monotonic() - self._last_write_ts)
        if wait > 0:
            time.sleep(wait)

    def _rest(self, path: str, *, method: str = "GET", body: Any = None) -> Any:
        with self._lock:
            is_write = method.upper() in ("POST", "PUT", "DELETE")
            for attempt in range(2):
                if is_write:
                    self._pace_write()
                st, parsed = _jsherp_http_json(
                    f"{self.base_url}{path}",
                    method=method,
                    body=body,
                    token=self.token,
                )
                if is_write:
                    self._last_write_ts = time.monotonic()
                data_val = parsed.get("data") if isinstance(parsed, dict) else None
                raw_txt = json.dumps(parsed, ensure_ascii=False)
                if (st in (401, 500) or "loginOut" in raw_txt or data_val == "loginOut") and attempt == 0:
                    self.token, _ = self.admin.login(self.login_name, self.password_md5)
                    continue
                if st != 200 or (isinstance(parsed, dict) and parsed.get("code") not in (None, 200)):
                    raise RuntimeError(f"jshERP {method} {path} failed: HTTP {st} {parsed}")
                return parsed
            raise RuntimeError(f"jshERP {method} {path} failed after re-login")

    def _seed_master_data(self) -> None:
        self._rest("/systemConfig/add", method="POST", body={"companyName": self.company_name})
        self._rest(
            "/depot/add",
            method="POST",
            body={"name": "唐山一库钢材仓", "type": 0, "isDefault": True, "enabled": True},
        )
        self._rest(
            "/account/add",
            method="POST",
            body={
                "name": "唐山钢贸结算户",
                "serialNo": f"TS-ACC-{self.tenant_id}",
                "initialAmount": 8_000_000.0,
                "isDefault": True,
                "enabled": True,
            },
        )
        self._rest(
            "/inOutItem/add",
            method="POST",
            body={"name": f"AP-STEEL-{self.tenant_id}", "type": "支出", "enabled": True},
        )
        self._rest(
            "/supplier/add",
            method="POST",
            body={
                "supplier": "河北华北特钢供应有限公司",
                "type": "供应商",
                "taxNum": "91130200STEEL001",
                "description": "NET30",
                "enabled": True,
            },
        )
        # Second supplier without tax_id for Scenario 4c MDM L4 cross-ERP resolution
        self._rest(
            "/supplier/add",
            method="POST",
            body={
                "supplier": "唐山鑫源冶金炉料厂",
                "type": "供应商",
                "taxNum": "",
                "description": "NET30",
                "enabled": True,
            },
        )
        self._rest(
            "/material/add",
            method="POST",
            body={
                "name": self.mat_name,
                "unit": "吨",
                "categoryId": None,
                "enabled": True,
                "meList": [
                    {
                        "barCode": self.mat_barcode,
                        "commodityUnit": "吨",
                        "purchaseDecimal": 3600,
                        "commodityDecimal": 3900,
                        "wholesaleDecimal": 3750,
                        "lowDecimal": 3500,
                    }
                ],
            },
        )
        self.depot_id = int(self._rest("/depot/list")["data"]["rows"][0]["id"])
        self.account_id = int(self._rest("/account/list")["data"]["rows"][0]["id"])
        sup_rows = self._rest(
            f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商', 'supplier': '河北华北特钢供应有限公司'}, ensure_ascii=False))}"
        )["data"]["rows"]
        self.supplier_id = int(next(r for r in sup_rows if r["supplier"] == "河北华北特钢供应有限公司")["id"])
        mat_row = self._rest("/material/list")["data"]["rows"][0]
        self.mat_id = int(mat_row["id"])
        me_rows = self.admin._mysql_exec(
            f"SELECT id FROM jsh_material_extend WHERE tenant_id = {self.tenant_id} AND material_id = {self.mat_id} AND delete_flag <> '1' LIMIT 1;"
        ).strip().splitlines()
        self.me_id = int(me_rows[0])

    def seed_flow(self, flow_key: str, *, po_number: str, qty: float, unit_price: float) -> dict[str, Any]:
        total = round(qty * unit_price, 2)
        rk_number = f"RK-{po_number}"
        fk_number = f"FK-{po_number}"
        self._rest(
            "/depotHead/addDepotHeadAndDetail",
            method="POST",
            body={
                "info": json.dumps(
                    {
                        "type": "其它",
                        "subType": "采购订单",
                        "defaultNumber": po_number,
                        "number": po_number,
                        "operTime": self.oper_time,
                        "organId": self.supplier_id,
                        "totalPrice": total,
                        "discountLastMoney": total,
                        "purchaseStatus": "0",
                        "status": "0",
                    },
                    ensure_ascii=False,
                ),
                "rows": json.dumps(
                    [
                        {
                            "id": f"r_po_{flow_key}",
                            "materialId": self.mat_id,
                            "materialExtendId": self.me_id,
                            "barCode": self.mat_barcode,
                            "name": self.mat_name,
                            "unit": "吨",
                            "depotId": self.depot_id,
                            "operNumber": qty,
                            "unitPrice": unit_price,
                            "allPrice": total,
                            "taxRate": 0,
                            "taxMoney": 0,
                            "taxLastMoney": total,
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        raw_po_id = int(
            self._rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(po_number)}")["data"]["id"]
        )

        self._rest(
            "/depotHead/addDepotHeadAndDetail",
            method="POST",
            body={
                "info": json.dumps(
                    {
                        "type": "入库",
                        "subType": "采购",
                        "defaultNumber": rk_number,
                        "number": rk_number,
                        "operTime": self.oper_time,
                        "organId": self.supplier_id,
                        "accountId": self.account_id,
                        "totalPrice": total,
                        "discountLastMoney": total,
                        "changeAmount": 0.0,
                        "debt": total,
                        "remark": f"[PO:{po_number}]",
                        "purchaseStatus": "0",
                        "status": "0",
                    },
                    ensure_ascii=False,
                ),
                "rows": json.dumps(
                    [
                        {
                            "id": f"r_rk_{flow_key}",
                            "materialId": self.mat_id,
                            "materialExtendId": self.me_id,
                            "barCode": self.mat_barcode,
                            "name": self.mat_name,
                            "unit": "吨",
                            "depotId": self.depot_id,
                            "operNumber": qty,
                            "unitPrice": unit_price,
                            "allPrice": total,
                            "taxRate": 0,
                            "taxMoney": 0,
                            "taxLastMoney": total,
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        raw_rk_id = int(
            self._rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(rk_number)}")["data"]["id"]
        )

        self._rest(
            "/accountHead/addAccountHeadAndDetail",
            method="POST",
            body={
                "info": json.dumps(
                    {
                        "type": "付款",
                        "billNo": fk_number,
                        "billTime": self.oper_time,
                        "organId": self.supplier_id,
                        "accountId": self.account_id,
                        "totalPrice": -total,
                        "changeAmount": 0.0,
                        "status": "0",
                    },
                    ensure_ascii=False,
                ),
                "rows": json.dumps(
                    [
                        {
                            "accountId": self.account_id,
                            "billNumber": rk_number,
                            "needDebt": total,
                            "finishDebt": 0.0,
                            "eachAmount": 0.0,
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        raw_fk_id = int(
            self._rest(f"/accountHead/getDetailByNumber?billNo={urllib.parse.quote(fk_number)}")["data"]["id"]
        )

        flow = {
            "flow_key": flow_key,
            "po_id": f"PO-{flow_key}",
            "rec_id": f"REC-{flow_key}",
            "bill_id": f"BILL-{flow_key}",
            "po_number": po_number,
            "rk_number": rk_number,
            "fk_number": fk_number,
            "raw_po_id": raw_po_id,
            "raw_rk_id": raw_rk_id,
            "raw_fk_id": raw_fk_id,
            "expected_qty": float(qty),
            "unit_price": float(unit_price),
        }
        self.flows[flow_key] = flow
        return flow

    def _flow_by_po_id(self, po_id: str) -> dict[str, Any]:
        for f in self.flows.values():
            if f["po_id"] == po_id:
                return f
        raise KeyError(f"unknown po_id {po_id!r}")

    def _flow_by_rec_id(self, rec_id: str) -> dict[str, Any]:
        for f in self.flows.values():
            if f["rec_id"] == rec_id:
                return f
        raise KeyError(f"unknown rec_id {rec_id!r}")

    def _flow_by_bill_id(self, bill_id: str) -> dict[str, Any]:
        for f in self.flows.values():
            if f["bill_id"] == bill_id:
                return f
        raise KeyError(f"unknown bill_id {bill_id!r}")

    def oob_cancel_po(self, po_id: str) -> None:
        flow = self._flow_by_po_id(po_id)
        self._rest(f"/depotHead/delete?id={flow['raw_po_id']}", method="DELETE")

    def extract_mdm_supplier_batch(self) -> list[dict[str, Any]]:
        rows = (
            self._rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商'}, ensure_ascii=False))}&currentPage=1&pageSize=100"
            )
            .get("data", {})
            .get("rows")
            or []
        )
        return [
            {
                "id": str(r["id"]),
                "legal_name": str(r.get("supplier") or ""),
                "tax_id": str(r.get("taxNum") or ""),
                "country_code": "CN",
            }
            for r in rows
        ]

    def _list_active_depot_heads(self) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for search_obj in (
            {"type": "其它", "subType": "采购订单"},
            {"type": "入库", "subType": "采购"},
        ):
            q = urllib.parse.quote(json.dumps(search_obj, ensure_ascii=False))
            rows = (
                self._rest(f"/depotHead/list?search={q}&currentPage=1&pageSize=200")
                .get("data", {})
                .get("rows")
                or []
            )
            for r in rows:
                if r.get("number"):
                    merged[str(r["number"])] = r
        return merged

    def _list_active_account_heads(self) -> dict[str, dict[str, Any]]:
        q = urllib.parse.quote(json.dumps({"type": "付款"}, ensure_ascii=False))
        rows = (
            self._rest(f"/accountHead/list?search={q}&currentPage=1&pageSize=200")
            .get("data", {})
            .get("rows")
            or []
        )
        return {
            str(r["number"] if "number" in r and not r.get("billNo") else r["billNo"]): r
            for r in rows
            if r.get("billNo") or r.get("number")
        }

    def actuate(self, move: dict[str, Any]) -> Any:
        self.raw_actuate_calls += 1
        action = str(move.get("action_type") or "")
        params = dict(move.get("params") or {})

        if action == "confirm_p2p_order":
            po_id = str(params["po_id"])
            flow = self._flow_by_po_id(po_id)
            active = self._list_active_depot_heads()
            row = active.get(flow["po_number"])
            if row is None or str(row.get("status")) != "0":
                return {"skipped": True, "reason": "oob_deleted_or_not_draft"}
            self._rest(
                "/depotHead/batchSetStatus",
                method="POST",
                body={"status": "1", "ids": str(flow["raw_po_id"])},
            )
            if self.inject_post_commit_timeout_for_po == po_id:
                self.inject_post_commit_timeout_for_po = None
                raise TimeoutError(f"Simulated ACK loss after committing {po_id} in jshERP")
            return {"ok": True}

        if action == "record_p2p_receipt":
            flow = self._flow_by_rec_id(str(params["receipt_id"]))
            rqty = float(params["received_qty"])
            insp = str(params.get("inspection_status") or "passed")
            exp_qty = float(flow["expected_qty"])
            unit_price = float(flow["unit_price"])
            total = round(rqty * unit_price, 2)
            disp = rqty < exp_qty or insp == "failed"
            tag = "DISPUTED" if disp else "COMPLETED"
            remark = f"[PO:{flow['po_number']}][{tag}:exp={exp_qty},rec={rqty},insp={insp}]"

            self._rest(
                "/depotHead/updateDepotHeadAndDetail",
                method="PUT",
                body={
                    "id": flow["raw_rk_id"],
                    "info": json.dumps(
                        {
                            "id": flow["raw_rk_id"],
                            "type": "入库",
                            "subType": "采购",
                            "defaultNumber": flow["rk_number"],
                            "number": flow["rk_number"],
                            "operTime": self.oper_time,
                            "organId": self.supplier_id,
                            "accountId": self.account_id,
                            "totalPrice": total,
                            "discountLastMoney": total,
                            "changeAmount": 0.0,
                            "debt": total,
                            "remark": remark,
                            "purchaseStatus": "0",
                            "status": "0",
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [
                            {
                                "id": f"r_rk_{flow['flow_key']}",
                                "materialId": self.mat_id,
                                "materialExtendId": self.me_id,
                                "barCode": self.mat_barcode,
                                "name": self.mat_name,
                                "unit": "吨",
                                "depotId": self.depot_id,
                                "operNumber": rqty,
                                "unitPrice": unit_price,
                                "allPrice": total,
                                "taxRate": 0,
                                "taxMoney": 0,
                                "taxLastMoney": total,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                },
            )
            self._rest(
                "/depotHead/batchSetStatus",
                method="POST",
                body={"status": "1", "ids": str(flow["raw_rk_id"])},
            )
            return {"ok": True}

        if action == "resolve_p2p_dispute":
            flow = self._flow_by_po_id(str(params["po_id"]))
            settled_amt = float(params["settled_amount"])
            qty = float(flow["expected_qty"])
            po_head = self._rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(flow['po_number'])}")["data"]
            if str(po_head.get("status")) != "0":
                self._rest("/depotHead/batchSetStatus", method="POST", body={"status": "0", "ids": str(flow["raw_po_id"])})
            self._rest(
                "/depotHead/updateDepotHeadAndDetail",
                method="PUT",
                body={
                    "id": flow["raw_po_id"],
                    "info": json.dumps(
                        {
                            "id": flow["raw_po_id"],
                            "type": "其它",
                            "subType": "采购订单",
                            "defaultNumber": flow["po_number"],
                            "number": flow["po_number"],
                            "operTime": self.oper_time,
                            "organId": self.supplier_id,
                            "totalPrice": settled_amt,
                            "discountLastMoney": settled_amt,
                            "remark": f"[SETTLED:{settled_amt}]",
                            "purchaseStatus": "0",
                            "status": "0",
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [
                            {
                                "id": f"r_po_{flow['flow_key']}",
                                "materialId": self.mat_id,
                                "materialExtendId": self.me_id,
                                "barCode": self.mat_barcode,
                                "name": self.mat_name,
                                "unit": "吨",
                                "depotId": self.depot_id,
                                "operNumber": qty,
                                "unitPrice": round(settled_amt / qty, 6),
                                "allPrice": settled_amt,
                                "taxRate": 0,
                                "taxMoney": 0,
                                "taxLastMoney": settled_amt,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                },
            )
            self._rest("/depotHead/batchSetStatus", method="POST", body={"status": "1", "ids": str(flow["raw_po_id"])})

            # Transition receipt tag in jshERP from DISPUTED -> COMPLETED while preserving actual received_qty
            rk_head = self._rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(flow['rk_number'])}")["data"]
            rk_items = self._rest(f"/depotItem/getDetailList?headerId={flow['raw_rk_id']}&mpList=&linkType=basic&isReadOnly=0").get("data", {}).get("rows") or []
            rec_qty = float(rk_items[0].get("operNumber") or qty) if rk_items else qty
            rk_remark = str(rk_head.get("remark") or "")
            new_remark = rk_remark.replace("[DISPUTED:", "[COMPLETED:") if "[DISPUTED:" in rk_remark else f"[PO:{flow['po_number']}][COMPLETED:exp={qty},rec={rec_qty},insp=passed]"
            if str(rk_head.get("status")) != "0":
                self._rest("/depotHead/batchSetStatus", method="POST", body={"status": "0", "ids": str(flow["raw_rk_id"])})
            self._rest(
                "/depotHead/updateDepotHeadAndDetail",
                method="PUT",
                body={
                    "id": flow["raw_rk_id"],
                    "info": json.dumps(
                        {
                            "id": flow["raw_rk_id"],
                            "type": "入库",
                            "subType": "采购",
                            "defaultNumber": flow["rk_number"],
                            "number": flow["rk_number"],
                            "operTime": self.oper_time,
                            "organId": self.supplier_id,
                            "accountId": self.account_id,
                            "totalPrice": settled_amt,
                            "discountLastMoney": settled_amt,
                            "changeAmount": 0.0,
                            "debt": settled_amt,
                            "remark": new_remark,
                            "purchaseStatus": "0",
                            "status": "0",
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [
                            {
                                "id": f"r_rk_{flow['flow_key']}",
                                "materialId": self.mat_id,
                                "materialExtendId": self.me_id,
                                "barCode": self.mat_barcode,
                                "name": self.mat_name,
                                "unit": "吨",
                                "depotId": self.depot_id,
                                "operNumber": rec_qty,
                                "unitPrice": float(flow["unit_price"]),
                                "allPrice": settled_amt,
                                "taxRate": 0,
                                "taxMoney": 0,
                                "taxLastMoney": settled_amt,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                },
            )
            self._rest("/depotHead/batchSetStatus", method="POST", body={"status": "1", "ids": str(flow["raw_rk_id"])})
            return {"ok": True}

        if action == "reconcile_p2p_bill":
            flow = self._flow_by_bill_id(str(params["bill_id"]))
            po_head = self._rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(flow['po_number'])}")["data"]
            po_total = round(float(po_head.get("discountLastMoney") or po_head.get("totalPrice") or 0.0), 2)
            self._rest(
                "/accountHead/updateAccountHeadAndDetail",
                method="PUT",
                body={
                    "id": flow["raw_fk_id"],
                    "info": json.dumps(
                        {
                            "id": flow["raw_fk_id"],
                            "type": "付款",
                            "billNo": flow["fk_number"],
                            "billTime": self.oper_time,
                            "organId": self.supplier_id,
                            "accountId": self.account_id,
                            "totalPrice": -po_total,
                            "changeAmount": 0.0,
                            "status": "0",
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [
                            {
                                "accountId": self.account_id,
                                "billNumber": flow["rk_number"],
                                "needDebt": po_total,
                                "finishDebt": 0.0,
                                "eachAmount": 0.0,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                },
            )
            return {"ok": True}

        if action == "post_p2p_bill":
            flow = self._flow_by_bill_id(str(params["bill_id"]))
            self._rest(
                "/accountHead/batchSetStatus",
                method="POST",
                body={"status": "1", "ids": str(flow["raw_fk_id"])},
            )
            return {"ok": True}

        if action == "record_p2p_payment":
            flow = self._flow_by_bill_id(str(params["bill_id"]))
            pay_amt = float(params["payment_amount"])
            ah_head = self._rest(f"/accountHead/getDetailByNumber?billNo={urllib.parse.quote(flow['fk_number'])}")["data"]
            ah_items = self._rest(f"/accountItem/getDetailList?headerId={flow['raw_fk_id']}").get("data", {}).get("rows") or []
            bill_total = round(abs(float(ah_head.get("totalPrice") or 0.0)), 2)
            prev_paid = round(abs(float(ah_head.get("changeAmount") or 0.0)), 2)
            new_paid = round(prev_paid + pay_amt, 2)
            need_debt = round(float(ah_items[0].get("needDebt") or bill_total), 2) if ah_items else bill_total
            if str(ah_head.get("status")) != "0":
                self._rest("/accountHead/batchSetStatus", method="POST", body={"status": "0", "ids": str(flow["raw_fk_id"])})
            self._rest(
                "/accountHead/updateAccountHeadAndDetail",
                method="PUT",
                body={
                    "id": flow["raw_fk_id"],
                    "info": json.dumps(
                        {
                            "id": flow["raw_fk_id"],
                            "type": "付款",
                            "billNo": flow["fk_number"],
                            "billTime": self.oper_time,
                            "organId": self.supplier_id,
                            "accountId": self.account_id,
                            "totalPrice": -bill_total,
                            "changeAmount": -new_paid,
                            "status": "0",
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [
                            {
                                "accountId": self.account_id,
                                "billNumber": flow["rk_number"],
                                "needDebt": need_debt,
                                "finishDebt": new_paid,
                                "eachAmount": new_paid,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                },
            )
            self._rest("/accountHead/batchSetStatus", method="POST", body={"status": "1", "ids": str(flow["raw_fk_id"])})
            return {"ok": True}

        raise ValueError(f"unsupported LiveJshErpP2pAdapter action {action!r}")

    def observe(self, _move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        active_depots = self._list_active_depot_heads()
        active_accts = self._list_active_account_heads()

        pos_out: list[dict[str, Any]] = []
        recs_out: list[dict[str, Any]] = []
        bills_out: list[dict[str, Any]] = []

        for flow in self.flows.values():
            po_row = active_depots.get(flow["po_number"])
            rk_row = active_depots.get(flow["rk_number"])
            fk_row = active_accts.get(flow["fk_number"])

            if po_row is None:
                pos_out.append(
                    {
                        "po_id": flow["po_id"],
                        "po_number": flow["po_number"],
                        "supplier": "SUP-HB",
                        "accounting_entity": "ENT-BUYER-TS",
                        "ordered_qty": flow["expected_qty"],
                        "unit_price": flow["unit_price"],
                        "amount_total": round(flow["expected_qty"] * flow["unit_price"], 2),
                        "status": "cancelled",
                    }
                )
            else:
                po_total = round(float(po_row.get("discountLastMoney") or po_row.get("totalPrice") or 0.0), 2)
                po_raw_status = str(po_row.get("status") or "0")

                # Parse receipt status & inspection/qty from real jshERP remark & line items
                rec_status = "pending"
                rec_qty = 0.0
                insp_status = "passed"
                if rk_row is not None and str(rk_row.get("status") or "0") == "1":
                    rk_remark = str(rk_row.get("remark") or "")
                    if "[DISPUTED:" in rk_remark:
                        rec_status = "disputed"
                    else:
                        rec_status = "completed"
                    m_rec = re.search(r"rec=([0-9.]+),insp=([a-zA-Z]+)", rk_remark)
                    if m_rec:
                        rec_qty = float(m_rec.group(1))
                        insp_status = m_rec.group(2)
                    else:
                        rk_items = self._rest(f"/depotItem/getDetailList?headerId={flow['raw_rk_id']}&mpList=&linkType=basic&isReadOnly=0").get("data", {}).get("rows") or []
                        rec_qty = float(rk_items[0].get("operNumber") or flow["expected_qty"]) if rk_items else flow["expected_qty"]

                    recs_out.append(
                        {
                            "receipt_id": flow["rec_id"],
                            "purchase_order": flow["po_id"],
                            "expected_qty": flow["expected_qty"],
                            "received_qty": rec_qty,
                            "inspection_status": insp_status,
                            "status": rec_status,
                        }
                    )
                elif rk_row is not None:
                    recs_out.append(
                        {
                            "receipt_id": flow["rec_id"],
                            "purchase_order": flow["po_id"],
                            "expected_qty": flow["expected_qty"],
                            "received_qty": 0.0,
                            "inspection_status": "pending",
                            "status": "pending",
                        }
                    )

                bill_posted = fk_row is not None and str(fk_row.get("status") or "0") == "1"
                if bill_posted:
                    po_status = "billed"
                elif rec_status == "disputed":
                    po_status = "disputed"
                elif rec_status == "completed":
                    po_status = "received"
                elif po_raw_status == "1":
                    po_status = "confirmed"
                else:
                    po_status = "draft"

                pos_out.append(
                    {
                        "po_id": flow["po_id"],
                        "po_number": flow["po_number"],
                        "supplier": "SUP-HB",
                        "accounting_entity": "ENT-BUYER-TS",
                        "ordered_qty": flow["expected_qty"],
                        "unit_price": flow["unit_price"],
                        "amount_total": po_total,
                        "status": po_status,
                    }
                )

            if fk_row is not None:
                bill_total = round(abs(float(fk_row.get("totalPrice") or 0.0)), 2)
                bill_paid = round(abs(float(fk_row.get("changeAmount") or 0.0)), 2)
                bill_res = max(0.0, round(bill_total - bill_paid, 2))
                pay_state = "paid" if bill_res <= 1e-9 else ("partial" if bill_paid > 1e-9 else "not_paid")
                bill_status = "posted" if str(fk_row.get("status") or "0") == "1" else "draft"
                bills_out.append(
                    {
                        "bill_id": flow["bill_id"],
                        "purchase_order": flow["po_id"],
                        "amount_total": bill_total,
                        "amount_residual": bill_res,
                        "payment_state": pay_state,
                        "status": bill_status,
                    }
                )

        return {
            "AccountingEntity": [
                {
                    "entity_id": "ENT-BUYER-TS",
                    "name": self.company_name,
                    "base_currency": "CNY",
                    "status": "active",
                }
            ],
            "BusinessParty": list(self.party_map.values()),
            "P2pSupplierProfile": [
                {
                    "profile_id": "SUP-HB",
                    "party": "PTY-SELLER-HB",
                    "accounting_entity": "ENT-BUYER-TS",
                    "payment_terms": "NET30",
                    "status": "active",
                }
            ],
            "P2pPurchaseOrder": pos_out,
            "P2pGoodsReceipt": recs_out,
            "P2pVendorBill": bills_out,
        }

    def as_source_bridge(self) -> SourceBridge:
        return SourceBridge(actuate=self.actuate, observe=self.observe)


def _odoo_rpc(url: str, service: str, method: str, args: list[Any], *, timeout: int = 30) -> Any:
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


class LiveOdoo19Admin:
    """Clones ``odoo_demo`` into ``wo18_*`` DB and drops + cleans filestore in ``finally``."""

    def __init__(self, url: str = ODOO_URL) -> None:
        self.url = url
        self.created_dbs: list[str] = []
        self.master_password = self._resolve_master_pw()

    def _resolve_master_pw(self) -> str:
        probe = f"wo18_auth_{uuid.uuid4().hex[:6]}"
        for cand in MASTER_CANDIDATES:
            if not cand:
                continue
            try:
                _odoo_rpc(self.url, "db", "duplicate_database", [cand, SOURCE_DEMO_DB, probe], timeout=60)
                self.master_password = cand
                self.drop_db(probe)
                return cand
            except Exception:
                if self.db_exist(probe):
                    try:
                        self.master_password = cand
                        self.drop_db(probe)
                    except Exception:
                        pass
        raise RuntimeError(f"Cannot resolve Odoo 19 master password on {self.url}")

    def list_dbs(self) -> list[str]:
        return list(_odoo_rpc(self.url, "db", "list", [], timeout=15))

    def db_exist(self, db_name: str) -> bool:
        return bool(_odoo_rpc(self.url, "db", "db_exist", [db_name], timeout=15))

    def list_wo18_pg_dbs(self) -> list[str]:
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
                "SELECT datname FROM pg_database WHERE datname LIKE 'wo18_%' ORDER BY datname;",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        return [x.strip() for x in proc.stdout.splitlines() if x.strip()]

    def clone_db(self, target_db: str) -> None:
        if not re.fullmatch(r"wo18_[a-z0-9_]+", target_db):
            raise ValueError(f"Refusing non-wo18 db {target_db!r}")
        _odoo_rpc(self.url, "db", "duplicate_database", [self.master_password, SOURCE_DEMO_DB, target_db], timeout=60)
        self.created_dbs.append(target_db)

    def drop_db(self, db_name: str) -> None:
        if not re.fullmatch(r"wo18_[a-z0-9_]+", db_name):
            raise ValueError(f"Refusing to drop non-wo18 db {db_name!r}")
        try:
            _odoo_rpc(self.url, "db", "drop", [self.master_password, db_name], timeout=30)
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

    def drop_all_created(self) -> list[str]:
        candidates = sorted(set(self.created_dbs) | set(self.list_wo18_pg_dbs()))
        for db in candidates:
            if db.startswith("wo18_"):
                self.drop_db(db)
        remaining = self.list_wo18_pg_dbs()
        if remaining:
            raise RuntimeError(f"Residual wo18_* Odoo DBs remain: {remaining}")
        return sorted(self.list_dbs())

    def login_kw(self, db_name: str) -> Callable[..., Any]:
        uid = _odoo_rpc(self.url, "common", "authenticate", [db_name, ODOO_USER, ODOO_PASS, {}])
        if not uid:
            raise RuntimeError(f"Odoo login failed for {db_name!r}")

        def _kw(model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
            return _odoo_rpc(self.url, "object", "execute_kw", [db_name, uid, ODOO_PASS, model, method, args, kwargs or {}], timeout=30)

        return _kw


class LiveOdooO2cDriver:
    """Real Odoo 19 O2C SourceBridge for Enterprise B with clean readback & MDM partner extraction."""

    def __init__(self, kw: Callable[..., Any], managed_parties: Sequence[dict[str, str]]) -> None:
        self.kw = kw
        self.writes = 0
        self.party_specs = {str(p["party_id"]): dict(p) for p in managed_parties}
        self.partner_id_by_party: dict[str, int] = {}
        self.party_by_partner_id: dict[int, str] = {}
        self.profile_by_party: dict[str, tuple[str, float]] = {}
        self.order_odoo_id: dict[str, int] = {}
        self.order_delivery_id: dict[str, str] = {}
        self.delivery_order_id: dict[str, str] = {}
        self.order_invoice_id: dict[str, str] = {}
        self.invoice_order_id: dict[str, str] = {}
        self.extra_mdm_partner_id: int = 0
        self.product_id: int = 0
        self._strip_and_seed()

    def _strip_and_seed(self) -> None:
        old_orders = self.kw("sale.order", "search", [[]])
        if old_orders:
            self.kw("sale.order", "write", [old_orders, {"locked": False}])
            non_cancel = self.kw("sale.order", "search", [[("state", "!=", "cancel")]])
            if non_cancel:
                self.kw("sale.order", "action_cancel", [non_cancel])
            for oid in old_orders:
                try:
                    self.kw("sale.order", "unlink", [[oid]])
                except Exception:
                    pass
        for pid in self.kw("stock.picking", "search", [[]]):
            try:
                st = self.kw("stock.picking", "read", [[pid], ["state"]])[0]["state"]
                if st not in ("done", "cancel"):
                    self.kw("stock.picking", "action_cancel", [[pid]])
                self.kw("stock.picking", "unlink", [[pid]])
            except Exception:
                pass
        for iid in self.kw("account.move", "search", [[("move_type", "=", "out_invoice")]]):
            try:
                st = self.kw("account.move", "read", [[iid], ["state"]])[0]["state"]
                if st == "posted":
                    self.kw("account.move", "button_draft", [[iid]])
                    st = "draft"
                if st == "draft":
                    self.kw("account.move", "button_cancel", [[iid]])
                self.kw("account.move", "unlink", [[iid]])
            except Exception:
                pass

        cn_ids = self.kw("res.country", "search", [[("code", "=", "CN")]], {"limit": 1})
        cn_id = int(cn_ids[0]) if cn_ids else False

        self.product_id = int(
            self.kw(
                "product.product",
                "create",
                [
                    {
                        "name": "HRB400E螺纹钢 Φ20mm",
                        "default_code": "HRB400E-20",
                        "type": "consu",
                        "is_storable": True,
                        "list_price": 3600.0,
                        "sale_ok": True,
                        "taxes_id": [(6, 0, [])],
                    }
                ],
            )
        )
        for pty_id, spec_d in self.party_specs.items():
            vals: dict[str, Any] = {
                "name": spec_d["legal_name"],
                "ref": pty_id,
                "vat": spec_d.get("tax_id") or False,
                "is_company": True,
                "customer_rank": 1,
                "credit_limit": 2_000_000.0,
                "company_id": 1,
            }
            if cn_id:
                vals["country_id"] = cn_id
            pid = int(self.kw("res.partner", "create", [vals]))
            self.partner_id_by_party[pty_id] = pid
            self.party_by_partner_id[pid] = pty_id

        self.extra_mdm_partner_id = int(
            self.kw(
                "res.partner",
                "create",
                [
                    {
                        "name": "华北特钢股份有限公司",
                        "ref": "ODOO-SUP-HB-DUP",
                        "vat": "91130200STEEL001",
                        "is_company": True,
                        "supplier_rank": 1,
                        "company_id": 1,
                    }
                ],
            )
        )

    def ensure_customer_profile(self, *, profile_id: str, party_id: str, credit_limit: float = 2_000_000.0) -> None:
        self.profile_by_party[party_id] = (profile_id, float(credit_limit))
        pid = self.partner_id_by_party[party_id]
        self.kw("res.partner", "write", [[pid], {"credit_limit": float(credit_limit)}])

    def create_draft_order(self, *, order_id: str, order_number: str, customer_profile_id: str, qty: float, unit_price: float) -> None:
        party_id = next(p for p, (pid, _) in self.profile_by_party.items() if pid == customer_profile_id)
        partner_id = self.partner_id_by_party[party_id]
        so_id = int(
            self.kw(
                "sale.order",
                "create",
                [
                    {
                        "partner_id": partner_id,
                        "client_order_ref": order_id,
                        "note": order_number,
                        "order_line": [
                            (
                                0,
                                0,
                                {
                                    "product_id": self.product_id,
                                    "name": "HRB400E螺纹钢 Φ20mm",
                                    "product_uom_qty": float(qty),
                                    "price_unit": float(unit_price),
                                    "tax_ids": [(6, 0, [])],
                                },
                            )
                        ],
                    }
                ],
            )
        )
        self.order_odoo_id[order_id] = so_id

    def extract_mdm_partner_batch(self) -> list[dict[str, Any]]:
        rows = self.kw(
            "res.partner",
            "read",
            [[self.extra_mdm_partner_id], ["id", "name", "vat"]],
        )
        return [
            {
                "id": str(r["id"]),
                "legal_name": str(r["name"]),
                "tax_id": str(r.get("vat") or ""),
                "country_code": "CN",
            }
            for r in rows
        ]

    def raw_order_state(self, order_id: str) -> str:
        so_id = self.order_odoo_id[order_id]
        row = self.kw("sale.order", "read", [[so_id], ["state"]])[0]
        return str(row["state"])

    def _handle_validate_result(self, result: Any, picking_id: int) -> None:
        if not isinstance(result, dict):
            return
        model = result.get("res_model")
        res_id = result.get("res_id")
        if model not in ("stock.immediate.transfer", "stock.backorder.confirmation"):
            return
        ctx = dict(result.get("context") or {})
        if res_id:
            self.kw(model, "process", [[res_id]], {"context": ctx})
            return
        if model == "stock.backorder.confirmation":
            wiz_id = self.kw(model, "create", [{"pick_ids": [(6, 0, [picking_id])]}], {"context": ctx})
            self.kw(model, "process", [[wiz_id]], {"context": ctx})

    def actuate(self, move: dict[str, Any]) -> Any:
        self.writes += 1
        action = str(move.get("action_type") or "")
        params = dict(move.get("params") or {})
        changes = (move.get("object_diff") or {}).get("object_changes") or []

        if action == "confirm_o2c_order":
            oid = str(params["order_id"])
            so_id = self.order_odoo_id[oid]
            row = self.kw("sale.order", "read", [[so_id], ["state"]])[0]
            if row["state"] in ("draft", "sent"):
                self.kw("sale.order", "action_confirm", [[so_id]])
            return {"ok": True}

        if action == "complete_o2c_delivery":
            did = str(params["delivery_id"])
            order_id = self.delivery_order_id.get(did)
            if not order_id:
                for ch in changes:
                    if ch.get("object_type") == "O2cDelivery" and ch.get("object_id") == did:
                        order_id = str((ch.get("before") or {}).get("order") or (ch.get("after") or {}).get("order"))
                        break
            assert order_id, f"cannot resolve order_id for delivery {did!r}"
            self.order_delivery_id[order_id] = did
            self.delivery_order_id[did] = order_id

            so_id = self.order_odoo_id[order_id]
            picks = self.kw(
                "stock.picking",
                "search_read",
                [[("sale_id", "=", so_id), ("state", "!=", "cancel")]],
                {"fields": ["id", "state"], "order": "id asc"},
            )
            picking_id = int(picks[-1]["id"])
            if picks[-1]["state"] != "done":
                self.kw("stock.picking", "action_assign", [[picking_id]])
                moves = self.kw("stock.move", "search_read", [[("picking_id", "=", picking_id)]], {"fields": ["id", "product_uom_qty"]})
                for mv in moves:
                    self.kw("stock.move", "write", [[int(mv["id"])], {"quantity": float(mv["product_uom_qty"]), "picked": True}])
                res = self.kw("stock.picking", "button_validate", [[picking_id]])
                self._handle_validate_result(res, picking_id)
            return {"ok": True}

        if action == "post_o2c_invoice":
            iid = str(params["invoice_id"])
            order_id = self.invoice_order_id.get(iid)
            amount_total: float | None = None
            for ch in changes:
                if ch.get("object_type") == "O2cInvoice" and ch.get("object_id") == iid:
                    snap = ch.get("before") or ch.get("after") or {}
                    order_id = order_id or str(snap["order"])
                    amount_total = float(snap["amount_total"])
                    break
            assert order_id and amount_total is not None, f"missing invoice metadata for {iid!r}"
            self.order_invoice_id[order_id] = iid
            self.invoice_order_id[iid] = order_id

            so_id = self.order_odoo_id[order_id]
            so = self.kw("sale.order", "read", [[so_id], ["name", "partner_id", "order_line"]])[0]
            line = self.kw(
                "sale.order.line",
                "read",
                [[int(so["order_line"][0])], ["id", "product_id", "price_unit"]],
            )[0]
            unit_price = float(line["price_unit"])
            inv_qty = round(amount_total / unit_price, 6)
            inv_id = int(
                self.kw(
                    "account.move",
                    "create",
                    [
                        {
                            "move_type": "out_invoice",
                            "partner_id": int(so["partner_id"][0]),
                            "invoice_date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
                            "invoice_origin": so["name"],
                            "ref": iid,
                            "invoice_line_ids": [
                                (
                                    0,
                                    0,
                                    {
                                        "product_id": int(line["product_id"][0]),
                                        "quantity": inv_qty,
                                        "price_unit": unit_price,
                                        "tax_ids": [(6, 0, [])],
                                        "sale_line_ids": [(6, 0, [int(line["id"])])],
                                    },
                                )
                            ],
                        }
                    ],
                )
            )
            self.kw("account.move", "action_post", [[inv_id]])
            return {"ok": True}

        if action == "record_o2c_payment":
            iid = str(params["invoice_id"])
            paid_amt = float(params["payment_amount"])
            mv_rows = self.kw(
                "account.move",
                "search_read",
                [[("move_type", "=", "out_invoice"), ("ref", "=", iid), ("state", "=", "posted")]],
                {"fields": ["id", "amount_residual"]},
            )
            odoo_inv_id = int(mv_rows[0]["id"])
            if float(mv_rows[0]["amount_residual"]) > 1e-9:
                ctx = {"active_model": "account.move", "active_ids": [odoo_inv_id]}
                wiz_id = self.kw(
                    "account.payment.register",
                    "create",
                    [
                        {
                            "payment_date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
                            "amount": paid_amt,
                        }
                    ],
                    {"context": ctx},
                )
                self.kw("account.payment.register", "action_create_payments", [[wiz_id]], {"context": ctx})
            return {"ok": True}

        raise ValueError(f"unsupported LiveOdooO2cDriver action {action!r}")

    def observe(self, _move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        profiles_out: list[dict[str, Any]] = []
        parties_out: list[dict[str, Any]] = []
        for pty_id, (prof_id, credit_limit) in self.profile_by_party.items():
            parties_out.append(dict(self.party_specs[pty_id]))
            profiles_out.append(
                {
                    "profile_id": prof_id,
                    "party": pty_id,
                    "accounting_entity": "ENT-SELLER-HB",
                    "credit_limit": credit_limit,
                    "payment_terms": "NET30",
                    "status": "active",
                }
            )

        orders_out: list[dict[str, Any]] = []
        deliveries_out: list[dict[str, Any]] = []
        invoices_out: list[dict[str, Any]] = []

        for oid, so_odoo_id in self.order_odoo_id.items():
            so = self.kw(
                "sale.order",
                "read",
                [[so_odoo_id], ["id", "name", "note", "state", "amount_total", "partner_id"]],
            )[0]
            partner_id = int(so["partner_id"][0])
            pty_id = self.party_by_partner_id[partner_id]
            prof_id = self.profile_by_party[pty_id][0]

            did = self.order_delivery_id.get(oid)
            deliv_done = False
            if did:
                picks = self.kw(
                    "stock.picking",
                    "search_read",
                    [[("sale_id", "=", so_odoo_id), ("state", "!=", "cancel")]],
                    {"fields": ["id", "state"], "order": "id asc"},
                )
                if picks:
                    deliv_done = picks[-1]["state"] == "done"
                    deliveries_out.append(
                        {
                            "delivery_id": did,
                            "order": oid,
                            "status": "completed" if deliv_done else "pending",
                        }
                    )

            iid = self.order_invoice_id.get(oid)
            inv_posted = False
            if iid:
                mvs = self.kw(
                    "account.move",
                    "search_read",
                    [[("move_type", "=", "out_invoice"), ("ref", "=", iid), ("state", "!=", "cancel")]],
                    {"fields": ["id", "state", "amount_total", "amount_residual"]},
                )
                if mvs:
                    mv = mvs[-1]
                    inv_posted = mv["state"] == "posted"
                    tot = float(mv["amount_total"])
                    res = float(mv["amount_residual"])
                    pstate = "paid" if res <= 1e-9 else ("partial" if res < tot - 1e-9 else "not_paid")
                    invoices_out.append(
                        {
                            "invoice_id": iid,
                            "order": oid,
                            "amount_total": tot,
                            "amount_residual": res,
                            "payment_state": pstate,
                            "status": "posted" if inv_posted else "draft",
                        }
                    )

            if inv_posted:
                ostatus = "invoiced"
            elif deliv_done:
                ostatus = "delivered"
            elif so["state"] in ("sale", "done"):
                ostatus = "confirmed"
            elif so["state"] == "cancel":
                ostatus = "cancelled"
            else:
                ostatus = "draft"

            orders_out.append(
                {
                    "order_id": oid,
                    "order_number": str(so.get("note") or so["name"]),
                    "customer": prof_id,
                    "accounting_entity": "ENT-SELLER-HB",
                    "amount_total": float(so["amount_total"]),
                    "status": ostatus,
                }
            )

        return {
            "AccountingEntity": [
                {
                    "entity_id": "ENT-SELLER-HB",
                    "name": "河北华北特钢供应有限公司",
                    "base_currency": "CNY",
                    "status": "active",
                }
            ],
            "BusinessParty": parties_out,
            "O2cCustomerProfile": profiles_out,
            "O2cOrder": orders_out,
            "O2cDelivery": deliveries_out,
            "O2cInvoice": invoices_out,
        }

    def as_source_bridge(self) -> SourceBridge:
        return SourceBridge(actuate=self.actuate, observe=self.observe)


# ---------------------------------------------------------------------------
# MCP Agent Dispatchers (Real Isolated Hermes vs Offline HTTP MCP Stepper)
# ---------------------------------------------------------------------------
def _run_real_hermes_mcp_turn(
    *,
    hermes_bin: str,
    mcp_srv: OntologyMCPServer,
    mcp_url: str,
    prompt: str,
    secret_env: Mapping[str, str],
    agent_label: str,
    isolated_home: Path | None = None,
) -> dict[str, Any]:
    owns_home = isolated_home is None
    home_dir = Path(tempfile.mkdtemp(prefix=f"wo18_hermes_{agent_label}_")) if isolated_home is None else isolated_home
    home_dir.mkdir(parents=True, exist_ok=True)
    before_idx = len(mcp_srv.call_history)
    t0 = time.monotonic()
    try:
        cfg_file = home_dir / "config.yaml"
        if not cfg_file.exists():
            cfg_lines = [
                "model:",
                f"  default: {OPENROUTER_MODEL}",
                "  provider: openrouter",
                "memory:",
                "  memory_enabled: false",
                "  user_profile_enabled: false",
                "mcp_servers:",
                "  open_ontology:",
                f"    url: {mcp_url}",
                "    enabled: true",
                "    trust: full",
                "    connect_timeout: 20",
            ]
            cfg_file.write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

        env_file = home_dir / ".env"
        if not env_file.exists():
            env_lines = [f"{k}={v}" for k, v in secret_env.items()]
            env_lines.extend(["NO_PROXY=127.0.0.1,localhost,::1", "no_proxy=127.0.0.1,localhost,::1"])
            env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
            os.chmod(env_file, 0o600)

        sub_env = dict(os.environ)
        sub_env["HERMES_HOME"] = str(home_dir)
        sub_env["NO_PROXY"] = os.environ["NO_PROXY"]
        sub_env["no_proxy"] = os.environ["no_proxy"]
        for k, v in secret_env.items():
            sub_env[k] = v

        proc = subprocess.run(
            [
                hermes_bin,
                "chat",
                "-q",
                prompt,
                "--oneshot",
                "-Q",
                "-t",
                "mcp-open_ontology",
                "--max-turns",
                "16",
            ],
            cwd=str(REPO_ROOT),
            env=sub_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=240,
        )
        elapsed = round(time.monotonic() - t0, 2)
        if proc.returncode != 0:
            raise RuntimeError(
                f"hermes {agent_label} exited rc={proc.returncode}\n"
                f"stdout={_scrub_secrets(proc.stdout[-1200:], list(secret_env.values()))}\n"
                f"stderr={_scrub_secrets(proc.stderr[-1200:], list(secret_env.values()))}"
            )
        calls = mcp_srv.call_history[before_idx:]
        return {
            "agent": agent_label,
            "mode": "real_hermes",
            "provider": "openrouter",
            "model": OPENROUTER_MODEL,
            "elapsed_sec": elapsed,
            "mcp_calls_count": len(calls),
            "call_trajectory": [
                {"tool": c["tool"], "is_error": c["is_error"], "arguments": c.get("arguments")}
                for c in calls
            ],
            "reply_preview": _scrub_secrets(proc.stdout.strip(), list(secret_env.values()))[:600],
        }
    finally:
        if owns_home:
            shutil.rmtree(home_dir, ignore_errors=True)


def _offline_mcp_http_post(mcp_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_offline_mcp_turn(
    *,
    mcp_srv: OntologyMCPServer,
    mcp_url: str,
    agent_label: str,
    requests_seq: Sequence[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    before_idx = len(mcp_srv.call_history)
    t0 = time.monotonic()
    _offline_mcp_http_post(
        mcp_url,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
    )
    for idx, (tool_name, args) in enumerate(requests_seq, start=2):
        _offline_mcp_http_post(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": idx,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            },
        )
    calls = mcp_srv.call_history[before_idx:]
    return {
        "agent": agent_label,
        "mode": "offline_http_mcp_mirror",
        "elapsed_sec": round(time.monotonic() - t0, 3),
        "mcp_calls_count": len(calls),
        "call_trajectory": [
            {"tool": c["tool"], "is_error": c["is_error"], "arguments": c.get("arguments")}
            for c in calls
        ],
    }


def _send_and_validate_envelope(env: CommonsenseEnvelope) -> str:
    raw = env.to_json()
    validate_commonsense_envelope(json.loads(raw))
    return raw


# ---------------------------------------------------------------------------
# Core 4-Scenario Lab Orchestrator (Shared by Live & Offline Mirror)
# ---------------------------------------------------------------------------
def _execute_four_scenarios(
    *,
    rt_a: OntologyRuntime,
    rt_b: OntologyRuntime,
    erp_a: Any,
    erp_b: Any,
    crm_b: BottleCrmSqliteDriver,
    buyer_reconciler: SagaReconciler,
    seller_reconciler: SagaReconciler,
    autonomy_a: AutonomyRampController,
    mdm_plugin: MDMSidecarPlugin,
    mcp_a: OntologyMCPServer,
    mcp_b: OntologyMCPServer,
    buyer_dispatch_fn: Callable[[str, str, Sequence[tuple[str, dict[str, Any]]]], dict[str, Any]],
    seller_dispatch_fn: Callable[[str, str, Sequence[tuple[str, dict[str, Any]]]], dict[str, Any]],
) -> dict[str, Any]:
    # Actors across both enterprises (distinct SoD roles)
    admin_a = Actor(actor_id="admin-a", role="admin", clearance_tags={"*"})
    buyer_actor = Actor(actor_id="buyer-ts-01", role="buyer", clearance_tags={"*"})
    buyer_wh = Actor(actor_id="wh-ts-01", role="warehouse", clearance_tags={"*"})
    buyer_pm = Actor(actor_id="pm-ts-01", role="procurement_manager", clearance_tags={"*"})
    buyer_fin = Actor(actor_id="fin-ts-01", role="finance", clearance_tags={"*"})
    steward_a = Actor(actor_id="stew-ts-01", role="steward", clearance_tags={"*"})

    admin_b = Actor(actor_id="admin-b", role="admin", clearance_tags={"*"})
    sales_rep = Actor(actor_id="rep-seller-01", role="sales_rep", clearance_tags={"*"})
    seller_wh = Actor(actor_id="wh-hb-01", role="warehouse", clearance_tags={"*"})
    seller_fin = Actor(actor_id="fin-hb-01", role="finance", clearance_tags={"*"})
    seller_sup = Actor(actor_id="sup-hb-01", role="support_agent", clearance_tags={"*"})

    # Seed trade resource on A, sync initial ERP/CRM snapshots into A and B
    res_cls = rt_a.spec.get_object_type("TradeResource")
    rt_a.put_object(
        res_cls(
            resource_id="RES-HRB400E",
            code="HRB400E-20",
            name="HRB400E螺纹钢 Φ20mm",
            unit="TON",
            spec_summary="Φ20mm*12m 抗震热轧带肋钢筋",
        ),
        actor=admin_a,
    )
    rt_a.load_snapshot(erp_a.observe(), actor=admin_a)
    crm_b.seed_into_runtime(rt_b, admin_b)
    rt_b.load_snapshot(erp_b.observe(), actor=admin_b)

    p2p_port = P2pCommonsensePort(
        rt_a,
        buyer_actor=buyer_actor,
        warehouse_actor=buyer_wh,
        procurement_manager_actor=buyer_pm,
        finance_actor=buyer_fin,
        sync_actor=admin_a,
    )
    o2c_port = O2cCommonsensePort(
        rt_b,
        sales_actor=sales_rep,
        warehouse_actor=seller_wh,
        finance_actor=seller_fin,
        accounting_entity_id="ENT-SELLER-HB",
        seller_party_id="PTY-SELLER-HB",
        default_credit_limit=2_000_000.0,
        sync_actor=admin_b,
    )

    envelopes_validated: list[str] = []

    def _wire(env: CommonsenseEnvelope) -> str:
        raw = _send_and_validate_envelope(env)
        envelopes_validated.append(env.message_kind)
        return raw

    # =======================================================================
    # SCENARIO 1: Autonomy Ramp SHADOW -> Governed Commit + Normal 100t Flow
    # =======================================================================
    s1_shadow_before_calls = erp_a.raw_actuate_calls
    s1a_trace = buyer_dispatch_fn(
        "s1_shadow",
        (
            "你是唐山钢贸的采购专员（buyer）。当前处于 SHADOW 影子冷启动试运行阶段，只能通过 `open_ontology` MCP 工具处理采购订单：\n"
            "1. 调用 `describe_tbox` 与 `query_objects`（object_type=`P2pPurchaseOrder`）查看订单 `PO-S1-100T`；\n"
            "2. 对 `PO-S1-100T` 调用 `submit_action`（run_id=`HERMES-A-S1-SHADOW`, action=`confirm_p2p_order`, params=`{\"po_id\": \"PO-S1-100T\"}`）。\n"
            "完成后用简短中文说明结果。"
        ),
        [
            ("describe_tbox", {}),
            ("query_objects", {"object_type": "P2pPurchaseOrder"}),
            ("submit_action", {"run_id": "HERMES-A-S1-SHADOW", "action": "confirm_p2p_order", "params": {"po_id": "PO-S1-100T"}}),
        ],
    )
    assert any(c["tool"] == "submit_action" for c in s1a_trace["call_trajectory"])
    assert erp_a.raw_actuate_calls == s1_shadow_before_calls, "SHADOW mode leaked actuation to jshERP!"
    assert rt_a.get_object("P2pPurchaseOrder", "PO-S1-100T", buyer_actor).status == "draft"
    assert any(e.outcome == "SHADOW_RECORDED" for e in autonomy_a.ledger)

    # Upgrade Enterprise A Autonomy Policy to AUTO_UNDER_CEILING <= 500,000 CNY
    autonomy_a.policy = AutonomyPolicy(
        default_mode="SHADOW",
        default_escalation_action="open_work_card",
        rules=(
            ActionAutonomyRule(
                action="confirm_p2p_order",
                roles=("buyer", "ai_agent", "admin"),
                mode="AUTO_UNDER_CEILING",
                ceiling_field="amount_total",
                max_value=500_000.0,
            ),
        ),
    )

    s1b_buyer_trace = buyer_dispatch_fn(
        "s1_buyer_confirm",
        (
            "你是唐山钢贸的采购专员（buyer），只能通过 `open_ontology` MCP 工具办理订单 `PO-S1-100T`（不要虚构或查询 TBox 中不存在的对象类型）：\n"
            "1. 调用 `describe_tbox` 与 `query_objects`（object_type=`P2pPurchaseOrder`）确认 `PO-S1-100T` 的当前状态与金额；\n"
            "2. 自治放行阈值与风控门禁由 MCP 服务端 `AutonomyRampController` 在 `submit_action` 内置强制仲裁（TBox 无独立审批策略对象），"
            "请直接调用 `submit_action`（run_id=`HERMES-A-S1-CONF`, action=`confirm_p2p_order`, params=`{\"po_id\": \"PO-S1-100T\"}`）提交执行，并用简短中文如实回报服务端返回结果。"
        ),
        [
            ("query_objects", {"object_type": "P2pPurchaseOrder"}),
            ("submit_action", {"run_id": "HERMES-A-S1-CONF", "action": "confirm_p2p_order", "params": {"po_id": "PO-S1-100T"}}),
        ],
    )
    assert rt_a.get_object("P2pPurchaseOrder", "PO-S1-100T", buyer_actor).status == "confirmed"
    assert any(e.outcome == "AUTO_COMMITTED" for e in autonomy_a.ledger)

    # Buyer emits 5-primitive TradeCommitment(proposed) -> Seller materializes draft SO in Odoo 19
    wire_s1_prop = _wire(
        p2p_port.emit_purchase_commitment(
            po_id="PO-S1-100T",
            resource_id="RES-HRB400E",
            commitment_id="CM-S1-100T",
            buyer_party_id="PTY-BUYER-TS",
            seller_party_id="PTY-SELLER-HB",
        )
    )
    env_s1_prop = CommonsenseEnvelope.from_json(wire_s1_prop)
    o2c_port.ingest_seller_envelope(wire_s1_prop) if hasattr(o2c_port, "ingest_seller_envelope") else None
    # Materialize incoming TradeCommitment + draft O2cOrder inside Enterprise B & Live Odoo 19
    o2c_port.runtime.put_object(
        rt_b.spec.get_object_type("TradeCommitment")(
            commitment_id="CM-S1-100T",
            buyer_party="PTY-BUYER-TS",
            seller_party="PTY-SELLER-HB",
            resource="RES-HRB400E",
            quantity=100.0,
            unit_price=3600.0,
            total_amount=360_000.0,
            status="proposed",
        ),
        actor=admin_b,
    )
    erp_b.ensure_customer_profile(profile_id="PROF-PTY-BUYER-TS", party_id="PTY-BUYER-TS")
    erp_b.create_draft_order(
        order_id="ORD-S1-100T",
        order_number="SO-S1-100T",
        customer_profile_id="PROF-PTY-BUYER-TS",
        qty=100.0,
        unit_price=3600.0,
    )
    rt_b.load_snapshot(erp_b.observe(), actor=admin_b)

    # Seller hermes confirms ORD-S1-100T through Seller MCP -> commits to real Odoo 19 + PG16
    s1b_seller_trace = seller_dispatch_fn(
        "s1_seller_confirm",
        (
            "你是华北特钢的销售代表（sales_rep）。请通过 `open_ontology` MCP 工具：\n"
            "1. 调用 `describe_tbox` 和 `query_objects`（object_type=`O2cOrder`）查询待确认订单 `ORD-S1-100T`；\n"
            "2. 先调用 `simulate_action`（action=`confirm_o2c_order`, params=`{\"order_id\": \"ORD-S1-100T\"}`）；\n"
            "3. 推演通过后调用 `submit_action`（run_id=`HERMES-B-S1-CONF`, action=`confirm_o2c_order`, params=`{\"order_id\": \"ORD-S1-100T\"}`）确认订单 `ORD-S1-100T`。"
        ),
        [
            ("describe_tbox", {}),
            ("query_objects", {"object_type": "O2cOrder"}),
            ("simulate_action", {"action": "confirm_o2c_order", "params": {"order_id": "ORD-S1-100T"}}),
            ("submit_action", {"run_id": "HERMES-B-S1-CONF", "action": "confirm_o2c_order", "params": {"order_id": "ORD-S1-100T"}}),
        ],
    )
    assert rt_b.get_object("O2cOrder", "ORD-S1-100T", sales_rep).status == "confirmed"
    assert erp_b.raw_order_state("ORD-S1-100T") in ("sale", "confirmed")

    # Mark agreement + seed delivery -> Seller ships 100t -> Buyer receives 100t -> Invoice -> Pay
    rt_b.put_object(
        rt_b.spec.get_object_type("TradeCommitment")(
            commitment_id="CM-S1-100T",
            buyer_party="PTY-BUYER-TS",
            seller_party="PTY-SELLER-HB",
            resource="RES-HRB400E",
            quantity=100.0,
            unit_price=3600.0,
            total_amount=360_000.0,
            status="agreed",
        ),
        actor=admin_b,
    )
    rt_b.put_object(
        rt_b.spec.get_object_type("O2cDelivery")(delivery_id="DEL-S1-100T", order="ORD-S1-100T", status="pending"),
        actor=admin_b,
    )
    wire_s1_ship = _wire(
        o2c_port.emit_shipment_event(
            delivery_id="DEL-S1-100T",
            order_id="ORD-S1-100T",
            commitment_id="CM-S1-100T",
            event_id="EV-S1-SHIP",
            shipped_qty=100.0,
            run_prefix="S1-SHIP",
        )
    )
    p2p_port.ingest_seller_envelope(wire_s1_ship)

    rt_a.submit_action(
        "S1-B-REC",
        "record_p2p_receipt",
        {"receipt_id": "REC-S1-100T", "received_qty": 100.0, "inspection_status": "passed"},
        buyer_wh,
    )
    _wire(
        p2p_port.emit_receipt_and_dispute_envelope(
            receipt_id="REC-S1-100T",
            commitment_id="CM-S1-100T",
            buyer_party_id="PTY-BUYER-TS",
            seller_party_id="PTY-SELLER-HB",
            event_id="EV-S1-REC",
        )
    )
    wire_s1_inv = _wire(
        o2c_port.emit_invoice_event(
            invoice_id="INV-S1-100T",
            order_id="ORD-S1-100T",
            commitment_id="CM-S1-100T",
            event_id="EV-S1-INV",
            qty=100.0,
            amount=360_000.0,
            run_prefix="S1-INV",
        )
    )
    p2p_port.ingest_seller_envelope(wire_s1_inv)
    rt_a.submit_action("S1-B-POST", "post_p2p_bill", {"bill_id": "BILL-S1-100T"}, buyer_fin)
    rt_a.submit_action(
        "S1-B-PAY",
        "record_p2p_payment",
        {"bill_id": "BILL-S1-100T", "payment_amount": 360_000.0},
        buyer_fin,
    )
    wire_s1_pay = _wire(
        p2p_port.emit_payment_event(
            bill_id="BILL-S1-100T",
            commitment_id="CM-S1-100T",
            buyer_party_id="PTY-BUYER-TS",
            seller_party_id="PTY-SELLER-HB",
            event_id="EV-S1-PAY",
        )
    )
    s1_o2c_pay = o2c_port.ingest_payment_event(wire_s1_pay, invoice_id="INV-S1-100T", run_prefix="S1-PAY")
    assert s1_o2c_pay["payment_state"] == "paid" and s1_o2c_pay["amount_residual"] <= 1e-6
    assert rt_a.get_object("P2pVendorBill", "BILL-S1-100T", buyer_fin).payment_state == "paid"

    # =======================================================================
    # SCENARIO 2: 100t Shipped vs 96t Weighbridge Short-Weight Dispute & WorkCard
    # =======================================================================
    rt_a.submit_action("S2-B-CONF", "confirm_p2p_order", {"po_id": "PO-S2-100T"}, buyer_actor)
    wire_s2_prop = _wire(
        p2p_port.emit_purchase_commitment(
            po_id="PO-S2-100T",
            resource_id="RES-HRB400E",
            commitment_id="CM-S2-DIS",
            buyer_party_id="PTY-BUYER-TS",
            seller_party_id="PTY-SELLER-HB",
        )
    )
    erp_b.ensure_customer_profile(profile_id="PROF-PTY-BUYER-TS", party_id="PTY-BUYER-TS")
    erp_b.create_draft_order(
        order_id="ORD-S2-100T",
        order_number="SO-S2-100T",
        customer_profile_id="PROF-PTY-BUYER-TS",
        qty=100.0,
        unit_price=3600.0,
    )
    rt_b.load_snapshot(erp_b.observe(), actor=admin_b)
    wire_s2_agr = _wire(
        o2c_port.ingest_buyer_commitment(
            wire_s2_prop,
            order_id="ORD-S2-100T",
            delivery_id="DEL-S2-100T",
            run_prefix="S2-IN",
        )
    )
    p2p_port.ingest_seller_envelope(wire_s2_agr)
    _wire(
        o2c_port.emit_shipment_event(
            delivery_id="DEL-S2-100T",
            order_id="ORD-S2-100T",
            commitment_id="CM-S2-DIS",
            event_id="EV-S2-SHIP",
            shipped_qty=100.0,
            run_prefix="S2-SHIP",
        )
    )

    # Buyer weighbridge weighs only 96t -> receipt & PO deterministically disputed
    rt_a.submit_action(
        "S2-B-REC96",
        "record_p2p_receipt",
        {"receipt_id": "REC-S2-100T", "received_qty": 96.0, "inspection_status": "passed"},
        buyer_wh,
    )
    assert rt_a.get_object("P2pPurchaseOrder", "PO-S2-100T", buyer_actor).status == "disputed"

    blocked_disputed_code = ""
    try:
        rt_a.submit_action("S2-POST-BLK", "post_p2p_bill", {"bill_id": "BILL-S2-100T"}, buyer_fin)
        raise AssertionError("Expected PO_DISPUTED_BLOCKED")
    except InvariantError as exc:
        blocked_disputed_code = exc.code
        assert exc.code == "PO_DISPUTED_BLOCKED"

    wire_s2_disp = _wire(
        p2p_port.emit_receipt_and_dispute_envelope(
            receipt_id="REC-S2-100T",
            commitment_id="CM-S2-DIS",
            buyer_party_id="PTY-BUYER-TS",
            seller_party_id="PTY-SELLER-HB",
            event_id="EV-S2-REC96",
            work_card_id="WC-S2-DIS-1",
            run_prefix="S2-DISP",
        )
    )
    assert "P2pPurchaseOrder" not in wire_s2_disp

    # Seller approves 4t deduction (96t * 3600 = 345,600) and posts adjusted invoice in Odoo 19
    wire_s2_set = _wire(
        o2c_port.settle_dispute_and_issue_invoice(
            wire_s2_disp,
            order_id="ORD-S2-100T",
            invoice_id="INV-S2-96T",
            event_id="EV-S2-INV96",
            consensus_qty=96.0,
            unit_price=3600.0,
            run_prefix="S2-SET",
        )
    )
    p2p_port.apply_settled_dispute_from_seller(
        wire_s2_set,
        po_id="PO-S2-100T",
        receipt_id="REC-S2-100T",
        local_work_card_id="WC-S2-DIS-1",
        run_prefix="S2-RES",
    )

    # Full 360,000 bill still blocked by BILL_EXCEEDS_PO_AMOUNT until reconciled to 345,600
    blocked_over_amt_code = ""
    try:
        rt_a.submit_action("S2-POST-OVER", "post_p2p_bill", {"bill_id": "BILL-S2-100T"}, buyer_fin)
        raise AssertionError("Expected BILL_EXCEEDS_PO_AMOUNT")
    except InvariantError as exc:
        blocked_over_amt_code = exc.code
        assert exc.code == "BILL_EXCEEDS_PO_AMOUNT"

    rt_a.submit_action("S2-RECON", "reconcile_p2p_bill", {"bill_id": "BILL-S2-100T"}, buyer_fin)
    rt_a.submit_action("S2-POST-OK", "post_p2p_bill", {"bill_id": "BILL-S2-100T"}, buyer_fin)
    rt_a.submit_action(
        "S2-PAY-OK",
        "record_p2p_payment",
        {"bill_id": "BILL-S2-100T", "payment_amount": 345_600.0},
        buyer_fin,
    )
    wire_s2_pay = _wire(
        p2p_port.emit_payment_event(
            bill_id="BILL-S2-100T",
            commitment_id="CM-S2-DIS",
            buyer_party_id="PTY-BUYER-TS",
            seller_party_id="PTY-SELLER-HB",
            event_id="EV-S2-PAY",
        )
    )
    s2_o2c_pay = o2c_port.ingest_payment_event(wire_s2_pay, invoice_id="INV-S2-96T", run_prefix="S2-PAY")
    assert s2_o2c_pay["payment_state"] == "paid"
    assert rt_a.get_object("P2pVendorBill", "BILL-S2-100T", buyer_fin).amount_total == 345_600.0
    assert rt_a.get_object("P2pVendorBill", "BILL-S2-100T", buyer_fin).amount_residual <= 1e-6

    # =======================================================================
    # SCENARIO 3: Seller BottleCRM Urgent Ticket Blocks Odoo Confirmation via Hermes
    # =======================================================================
    rt_a.submit_action("S3-B-CONF", "confirm_p2p_order", {"po_id": "PO-S3-50T"}, buyer_actor)
    wire_s3_prop = _wire(
        p2p_port.emit_purchase_commitment(
            po_id="PO-S3-50T",
            resource_id="RES-HRB400E",
            commitment_id="CM-S3-50T",
            buyer_party_id="PTY-BUYER-RISK",
            seller_party_id="PTY-SELLER-HB",
        )
    )
    rt_b.put_object(
        rt_b.spec.get_object_type("TradeCommitment")(
            commitment_id="CM-S3-50T",
            buyer_party="PTY-BUYER-RISK",
            seller_party="PTY-SELLER-HB",
            resource="RES-HRB400E",
            quantity=50.0,
            unit_price=3600.0,
            total_amount=180_000.0,
            status="proposed",
        ),
        actor=admin_b,
    )
    erp_b.ensure_customer_profile(profile_id="PROF-PTY-BUYER-RISK", party_id="PTY-BUYER-RISK")
    erp_b.create_draft_order(
        order_id="ORD-S3-50T",
        order_number="SO-S3-50T",
        customer_profile_id="PROF-PTY-BUYER-RISK",
        qty=50.0,
        unit_price=3600.0,
    )
    rt_b.load_snapshot(erp_b.observe(), actor=admin_b)

    erp_b_writes_before_s3 = erp_b.writes
    s3_seller_trace = seller_dispatch_fn(
        "s3_seller_crm_block",
        (
            "你是华北特钢的销售代表（sales_rep）。请通过 `open_ontology` MCP 工具处理草稿订单 `ORD-S3-50T`（严禁尝试越权关闭客服工单）：\n"
            "1. 调用 `query_objects`（object_type=`O2cOrder`）查看 `ORD-S3-50T`；\n"
            "2. 先调用 `simulate_action`（action=`confirm_o2c_order`, params=`{\"order_id\": \"ORD-S3-50T\"}`）预演确认条件；\n"
            "3. 如被紧急客诉工单 `URGENT_TICKET_BLOCKS_ORDER` 拦截，绝不要强行确认，也不得关闭 `CrmTicket`；而是调用 `submit_action` 签发内部人工审批工单：\n"
            "   run_id=`HERMES-B-S3-WC`, action=`open_work_card`, params={\n"
            "     \"card_id\": \"WC-ORD-S3-50T\",\n"
            "     \"card_kind\": \"HUMAN_TASK\",\n"
            "     \"target_object_type\": \"O2cOrder\",\n"
            "     \"target_object_id\": \"ORD-S3-50T\",\n"
            "     \"requested_action\": \"confirm_o2c_order\",\n"
            "     \"requested_params_json\": \"{\\\"order_id\\\": \\\"ORD-S3-50T\\\"}\",\n"
            "     \"assignee_role\": \"sales_manager\"\n"
            "   }。\n"
            "完成后用简短中文说明拦截原因及工单卡号。"
        ),
        [
            ("query_objects", {"object_type": "O2cOrder"}),
            ("simulate_action", {"action": "confirm_o2c_order", "params": {"order_id": "ORD-S3-50T"}}),
            (
                "submit_action",
                {
                    "run_id": "HERMES-B-S3-WC",
                    "action": "open_work_card",
                    "params": {
                        "card_id": "WC-ORD-S3-50T",
                        "card_kind": "HUMAN_TASK",
                        "target_object_type": "O2cOrder",
                        "target_object_id": "ORD-S3-50T",
                        "requested_action": "confirm_o2c_order",
                        "requested_params_json": '{"order_id": "ORD-S3-50T"}',
                        "assignee_role": "sales_manager",
                    },
                },
            ),
        ],
    )
    assert erp_b.writes == erp_b_writes_before_s3, "URGENT CrmTicket failed to block Odoo 19 writes!"
    assert erp_b.raw_order_state("ORD-S3-50T") == "draft"
    assert rt_b.get_object("O2cOrder", "ORD-S3-50T", sales_rep).status == "draft"
    s3_cards = [
        wc
        for wc in rt_b.query_objects("WorkCard", sales_rep)
        if wc.target_object_id == "ORD-S3-50T" and wc.status == "OPEN"
    ]
    assert len(s3_cards) >= 1

    # Seller projects hold over 5-primitive wire with ZERO CrmTicket/BottleCRM leakage
    env_s3_held = o2c_port.ingest_buyer_commitment(
        wire_s3_prop,
        order_id="ORD-S3-50T-WIRE",
        delivery_id="DEL-S3-50T-WIRE",
        profile_id="PROF-PTY-BUYER-RISK",
        run_prefix="S3-HOLD",
    )
    wire_s3_held = _wire(env_s3_held)
    assert env_s3_held.message_kind == "COMMITMENT_HELD"
    assert "CrmTicket" not in wire_s3_held and "bottlecrm" not in wire_s3_held.lower()
    p2p_port.ingest_seller_envelope(wire_s3_held)
    assert rt_a.get_object("TradeCommitment", "CM-S3-50T", buyer_actor).status == "disputed"

    # =======================================================================
    # SCENARIO 4: OOB Tamper Catch + Saga Post-Timeout + MDMSidecarPlugin
    # =======================================================================
    # 4a) Real OOB tamper on PO-S4-OOB -> DEVICE_STATE_MISMATCH
    erp_a.oob_cancel_po("PO-S4-OOB")
    oob_error_code = ""
    try:
        rt_a.submit_action("S4-OOB-CONF", "confirm_p2p_order", {"po_id": "PO-S4-OOB"}, buyer_actor)
        raise AssertionError("Expected DeviceStateMismatchError on OOB-cancelled PO-S4-OOB")
    except DeviceStateMismatchError as exc:
        oob_error_code = exc.code
        assert exc.code == DEVICE_STATE_MISMATCH
    oob_run = rt_a.get_action_run("S4-OOB-CONF")
    assert oob_run is not None and oob_run["status"] == "REJECTED"
    assert rt_a.get_object("P2pPurchaseOrder", "PO-S4-OOB", buyer_actor).status == "draft"

    # 4b) Real SagaReconciler post-commit TimeoutError on PO-S4-TIMEOUT -> RECONCILED_REMOTE_COMMITTED (1 actuate)
    calls_before_saga = erp_a.raw_actuate_calls
    erp_a.inject_post_commit_timeout_for_po = "PO-S4-TIMEOUT"
    run_saga = rt_a.submit_action("S4-SAGA-TIMEOUT", "confirm_p2p_order", {"po_id": "PO-S4-TIMEOUT"}, buyer_actor)
    assert run_saga["status"] == "COMMITTED"
    assert erp_a.raw_actuate_calls == calls_before_saga + 1
    assert rt_a.get_object("P2pPurchaseOrder", "PO-S4-TIMEOUT", buyer_actor).status == "confirmed"
    saga_rec = next(r for r in buyer_reconciler.records if r.run_id == "S4-SAGA-TIMEOUT")
    assert saga_rec.outcome == "RECONCILED_REMOTE_COMMITTED"
    assert saga_rec.actuate_calls == 1
    assert saga_rec.observe_calls == 1

    # 4c) MDMSidecarPlugin cross-ERP incremental reconciliation (jshERP <-> Odoo 19 / BottleCRM)
    batch_jsh = SourceEntityBatch(
        source_id="jsherp_ent_a",
        source_table="jsh_supplier",
        pk_field="id",
        records=erp_a.extract_mdm_supplier_batch(),
    )
    batch_odoo = SourceEntityBatch(
        source_id="odoo_ent_b",
        source_table="res_partner",
        pk_field="id",
        records=erp_b.extract_mdm_partner_batch(),
    )
    batch_crm = SourceEntityBatch(
        source_id="bottlecrm_ent_b",
        source_table="accounts",
        pk_field="id",
        records=crm_b.extract_mdm_source_rows(),
    )

    mdm_res1 = mdm_plugin.ingest_incremental([batch_jsh, batch_odoo, batch_crm], runtime=rt_a, actor=buyer_actor, run_id_prefix="mdm-s4c")
    assert len(mdm_res1.auto_merged_canonical_ids) >= 1
    # Identical tax_id pair (jshERP supplier & Odoo partner) L1 auto-merged + hot refreshed
    l1_entry = next(
        e for e in mdm_res1.crosswalk.entries
        if any(m.source_id == "jsherp_ent_a" for m in e.members)
        and any(m.source_id == "odoo_ent_b" for m in e.members)
    )
    assert l1_entry.match_tier == "L1_HARD_KEY"
    # L4 gray zone ("唐山鑫源冶金炉料厂" vs "唐山鑫源冶金材料厂") NOT merged; 1 WorkCard dispatched
    assert len(mdm_res1.pending_candidates) == 1
    assert len(mdm_res1.dispatched_review_run_ids) == 1
    cand = mdm_res1.pending_candidates[0]
    assert mdm_res1.crosswalk.resolve_canonical_id(cand.left.source_id, cand.left.raw_pk) != (
        mdm_res1.crosswalk.resolve_canonical_id(cand.right.source_id, cand.right.raw_pk)
    )

    # Re-poll same batches -> 0 duplicate WorkCards
    mdm_res_rep = mdm_plugin.ingest_incremental([batch_jsh, batch_odoo, batch_crm], runtime=rt_a, actor=buyer_actor, run_id_prefix="mdm-s4c")
    assert mdm_res_rep.dispatched_review_run_ids == []

    # Steward approves L4 WorkCard -> sync_resolved_reviews locks L0_CONFIRMED permanently
    mdm_card_id = f"WC-{cand.candidate_id}"
    rt_a.submit_action(
        "S4C-STEW-COMP",
        "complete_work_card",
        {"card_id": mdm_card_id, "resolution_note": "CONFIRM_MERGE: verified same Tangshan Xinyuan mill"},
        steward_a,
    )

    def _extract_mdm_dec(raw_card: dict[str, Any]) -> EntityResolutionDecision | None:
        if raw_card.get("status") != "COMPLETED" or raw_card.get("card_id") != mdm_card_id:
            return None
        payload = json.loads(raw_card.get("requested_params_json") or "{}")
        return EntityResolutionDecision(
            candidate_id=str(payload["candidate_id"]),
            left=SourceRef(**payload["left"]),
            right=SourceRef(**payload["right"]),
            verdict="CONFIRM_MERGE",
            decided_by=str(raw_card.get("resolved_by_actor_id") or ""),
            rationale=str(raw_card.get("resolution_note") or ""),
        )

    synced = mdm_plugin.sync_resolved_reviews(
        rt_a,
        steward_a,
        review_object_type="WorkCard",
        decision_extractor=_extract_mdm_dec,
    )
    assert len(synced) == 1
    assert mdm_plugin.crosswalk.resolve_canonical_id(cand.left.source_id, cand.left.raw_pk) == (
        mdm_plugin.crosswalk.resolve_canonical_id(cand.right.source_id, cand.right.raw_pk)
    )

    return {
        "envelopes_validated": envelopes_validated,
        "scenario_1": {
            "passed": True,
            "shadow_ledger_outcomes": [e.outcome for e in autonomy_a.ledger],
            "buyer_shadow_trace": s1a_trace,
            "buyer_confirm_trace": s1b_buyer_trace,
            "seller_confirm_trace": s1b_seller_trace,
            "buyer_bill_state": rt_a.get_object("P2pVendorBill", "BILL-S1-100T", buyer_fin).payment_state,
            "seller_invoice_state": s1_o2c_pay["payment_state"],
            "settled_amount_cny": 360_000.0,
        },
        "scenario_2": {
            "passed": True,
            "shipped_tons": 100.0,
            "received_tons": 96.0,
            "blocked_disputed_code": blocked_disputed_code,
            "blocked_over_amount_code": blocked_over_amt_code,
            "settled_amount_cny": 345_600.0,
            "seller_invoice_total": 345_600.0,
            "seller_payment_state": s2_o2c_pay["payment_state"],
        },
        "scenario_3": {
            "passed": True,
            "seller_hermes_trace": s3_seller_trace,
            "odoo_order_state": erp_b.raw_order_state("ORD-S3-50T"),
            "erp_b_writes_delta_during_hold": erp_b.writes - erp_b_writes_before_s3,
            "seller_open_work_cards": [c.card_id for c in s3_cards],
            "buyer_commitment_status": rt_a.get_object("TradeCommitment", "CM-S3-50T", buyer_actor).status,
            "wire_crm_leak_count": 0,
        },
        "scenario_4": {
            "passed": True,
            "oob_caught_code": oob_error_code,
            "oob_run_status": oob_run["status"],
            "saga_outcome": saga_rec.outcome,
            "saga_actuate_calls": saga_rec.actuate_calls,
            "saga_observe_calls": saga_rec.observe_calls,
            "mdm_l1_auto_merged_cid": l1_entry.canonical_id,
            "mdm_l4_dispatched_runs": mdm_res1.dispatched_review_run_ids,
            "mdm_l4_repeat_dispatched": mdm_res_rep.dispatched_review_run_ids,
            "mdm_l0_confirmed_cid": mdm_plugin.crosswalk.resolve_canonical_id(cand.left.source_id, cand.left.raw_pk),
        },
    }


def _shared_mdm_sidecar() -> MDMSidecarPlugin:
    res_spec = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="BP-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code"],
        strip_tokens=["有限公司", "供应有限公司", "股份有限公司", "股份"],
        auto_merge_threshold=0.92,
        review_threshold=0.55,
    )
    base_jsh_map = AdapterMappingSpec(
        mapping_id="wo18_jsh_party",
        source_kind="rest_erp",
        entity_mappings=[
            EntityMappingSpec(
                source_table="jsh_supplier",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="id", kind="direct"),
            )
        ],
    )
    base_odoo_map = AdapterMappingSpec(
        mapping_id="wo18_odoo_party",
        source_kind="odoo_rpc",
        entity_mappings=[
            EntityMappingSpec(
                source_table="res_partner",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="id", kind="direct"),
            )
        ],
    )

    def _mdm_card_payload(intent: dict[str, Any]) -> dict[str, Any]:
        return {
            "card_id": f"WC-{intent['target_object_id']}",
            "card_kind": "HUMAN_TASK",
            "target_object_type": "BusinessParty",
            "target_object_id": intent["target_object_id"],
            "requested_action": intent["requested_operation"],
            "requested_params_json": intent["params_json"],
            "assignee_role": "steward",
            "external_ref": intent["intent_code"],
        }

    return MDMSidecarPlugin(
        res_spec,
        adapter_mappings={"jsherp_ent_a": base_jsh_map, "odoo_ent_b": base_odoo_map},
        review_action="open_work_card",
        review_payload_builder=_mdm_card_payload,
    )


# ---------------------------------------------------------------------------
# Entrypoint 1: Offline Deterministic Mirror (Zero Docker, Zero Network)
# ---------------------------------------------------------------------------
def run_offline_b2b_steel_trade_lab(tmp_dir: Path) -> dict[str, Any]:
    _ensure_local_no_proxy()
    buyer_spec = build_buyer_enterprise_spec()
    seller_spec = build_seller_enterprise_spec()

    crm_copy = tmp_dir / "bottlecrm_wo18_offline.sqlite3"
    shutil.copy2(SOURCE_BOTTLECRM_DB, crm_copy)

    erp_a = OfflineJshErpSimulator()
    for fk, po_num, qty in (
        ("S1-100T", "PO-OFF-S1", 100.0),
        ("S2-100T", "PO-OFF-S2", 100.0),
        ("S3-50T", "PO-OFF-S3", 50.0),
        ("S4-OOB", "PO-OFF-S4A", 10.0),
        ("S4-TIMEOUT", "PO-OFF-S4B", 20.0),
    ):
        erp_a.seed_flow(fk, po_number=po_num, qty=qty, unit_price=3600.0)

    managed_parties = list(erp_a.parties.values()) + [
        {"party_id": "PTY-CRM-XY", "legal_name": "唐山鑫源冶金材料厂", "tax_id": "", "country_code": "CN", "status": "active"}
    ]
    erp_b = OfflineOdooO2cSimulator(managed_parties)
    crm_b = BottleCrmSqliteDriver(crm_copy, managed_parties)

    buyer_reconciler = SagaReconciler(erp_a.as_source_bridge(), buyer_spec)
    rt_a = OntologyRuntime(
        buyer_spec,
        db_path=":memory:",
        world_mode="coupled",
        source_bridge=buyer_reconciler.as_source_bridge(),
    )

    registry_b = DeviceDriverRegistry(spec=seller_spec)
    registry_b.mount(
        MountedDevice(
            device_id="odoo_sim",
            name="Offline Odoo 19 O2C Mirror",
            kind="odoo_rpc",
            endpoint="offline://odoo19",
            bridge=erp_b.as_source_bridge(),
            handled_actions=("confirm_o2c_order", "complete_o2c_delivery", "post_o2c_invoice", "record_o2c_payment"),
            owned_object_types=("AccountingEntity", "O2cCustomerProfile", "O2cOrder", "O2cDelivery", "O2cInvoice"),
            shared_object_types=("BusinessParty",),
        )
    )
    registry_b.mount(
        MountedDevice(
            device_id="bottlecrm_sqlite",
            name="BottleCRM Cloned SQLite",
            kind="sqlite",
            endpoint=str(crm_copy),
            bridge=SourceBridge(actuate=crm_b.actuate, observe=crm_b.observe),
            handled_actions=("resolve_crm_ticket",),
            owned_object_types=("CrmAccount", "CrmDeal", "CrmTicket"),
            shared_object_types=("BusinessParty",),
        )
    )
    seller_reconciler = SagaReconciler(registry_b.as_source_bridge(), seller_spec)
    rt_b = OntologyRuntime(
        seller_spec,
        db_path=":memory:",
        world_mode="coupled",
        source_bridge=seller_reconciler.as_source_bridge(),
    )

    autonomy_a = AutonomyRampController(
        AutonomyPolicy(default_mode="SHADOW"),
        ledger_path=tmp_dir / "shadow_ledger.jsonl",
    )
    mcp_a = OntologyMCPServer(
        rt_a,
        Actor(actor_id="buyer-ts-01", role="buyer", clearance_tags={"*"}),
        autonomy_controller=autonomy_a,
    )
    mcp_b = OntologyMCPServer(
        rt_b,
        Actor(actor_id="rep-seller-01", role="sales_rep", clearance_tags={"*"}),
    )
    mdm_plugin = _shared_mdm_sidecar()

    try:
        with start_mcp_http_server(mcp_a, host="127.0.0.1", port=0) as hdl_a, start_mcp_http_server(
            mcp_b, host="127.0.0.1", port=0
        ) as hdl_b:
            result = _execute_four_scenarios(
                rt_a=rt_a,
                rt_b=rt_b,
                erp_a=erp_a,
                erp_b=erp_b,
                crm_b=crm_b,
                buyer_reconciler=buyer_reconciler,
                seller_reconciler=seller_reconciler,
                autonomy_a=autonomy_a,
                mdm_plugin=mdm_plugin,
                mcp_a=mcp_a,
                mcp_b=mcp_b,
                buyer_dispatch_fn=lambda lbl, _p, reqs: _run_offline_mcp_turn(
                    mcp_srv=mcp_a, mcp_url=hdl_a.mcp_url, agent_label=lbl, requests_seq=reqs
                ),
                seller_dispatch_fn=lambda lbl, _p, reqs: _run_offline_mcp_turn(
                    mcp_srv=mcp_b, mcp_url=hdl_b.mcp_url, agent_label=lbl, requests_seq=reqs
                ),
            )
        return {"ok": True, "mode": "offline_mirror", **result}
    finally:
        rt_a.close()
        rt_b.close()


# ---------------------------------------------------------------------------
# Entrypoint 2: Real Multi-Container Live Lab (jshERP + Odoo 19 + BottleCRM + PG16 + Hermes)
# ---------------------------------------------------------------------------
def run_live_b2b_steel_trade_lab(
    output_path: Path = Path("/tmp/wo18_live_b2b_steel_trade_lab.json"),
) -> dict[str, Any]:
    _ensure_local_no_proxy()
    hermes_bin = HERMES_BIN_DEFAULT if Path(HERMES_BIN_DEFAULT).is_file() else shutil.which("hermes")
    if not hermes_bin:
        raise FileNotFoundError("local hermes CLI not found")
    secret_env = _preflight_openrouter_deepseek()
    bottle_sha_before = _file_sha256(SOURCE_BOTTLECRM_DB)

    rand_hex = uuid.uuid4().hex[:8]
    schema_a = f"wo18_ent_a_{rand_hex}"
    schema_b = f"wo18_ent_b_{rand_hex}"
    odoo_db = f"wo18_ent_b_{rand_hex}"
    jsh_login = f"wo18_ent_a_{rand_hex}"

    jsh_admin = LiveJshErpAdmin(JSHERP_BASE_URL)
    odoo_admin = LiveOdoo19Admin(ODOO_URL)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"wo18_lab_{rand_hex}_"))

    rt_a = None
    rt_b = None
    t_start = time.monotonic()

    try:
        # 1. Provision isolated Enterprise A (jshERP tenant + PG16 schema)
        tenant_info = jsh_admin.register_tenant(jsh_login, f"Wo18Pass_{rand_hex}!")
        erp_a = LiveJshErpP2pAdapter(jsh_admin, tenant_info)
        for fk, po_num, qty in (
            ("S1-100T", f"PO-A1-{rand_hex}", 100.0),
            ("S2-100T", f"PO-A2-{rand_hex}", 100.0),
            ("S3-50T", f"PO-A3-{rand_hex}", 50.0),
            ("S4-OOB", f"PO-A4A-{rand_hex}", 10.0),
            ("S4-TIMEOUT", f"PO-A4B-{rand_hex}", 20.0),
        ):
            erp_a.seed_flow(fk, po_number=po_num, qty=qty, unit_price=3600.0)
        jsh_admin._mysql_exec(f"DELETE FROM jsh_log WHERE tenant_id = {tenant_info['tenant_id']};")

        buyer_spec = build_buyer_enterprise_spec()
        buyer_reconciler = SagaReconciler(erp_a.as_source_bridge(), buyer_spec)
        rt_a = open_postgres_runtime(
            buyer_spec,
            PG_DSN,
            schema_name=schema_a,
            world_mode="coupled",
            source_bridge=buyer_reconciler.as_source_bridge(),
        )

        # 2. Provision isolated Enterprise B (cloned Odoo 19 + cloned BottleCRM + PG16 schema)
        odoo_admin.clone_db(odoo_db)
        odoo_kw = odoo_admin.login_kw(odoo_db)
        managed_parties = list(erp_a.party_map.values()) + [
            {"party_id": "PTY-CRM-XY", "legal_name": "唐山鑫源冶金材料厂", "tax_id": "", "country_code": "CN", "status": "active"}
        ]
        erp_b = LiveOdooO2cDriver(odoo_kw, managed_parties)

        crm_copy = tmp_dir / "bottlecrm_wo18_live.sqlite3"
        shutil.copy2(SOURCE_BOTTLECRM_DB, crm_copy)
        crm_b = BottleCrmSqliteDriver(crm_copy, managed_parties)

        seller_spec = build_seller_enterprise_spec()
        registry_b = DeviceDriverRegistry(spec=seller_spec)
        registry_b.mount(
            MountedDevice(
                device_id="odoo19_o2c",
                name="Live Odoo 19 O2C ERP",
                kind="odoo_rpc",
                endpoint=f"{ODOO_URL}#{odoo_db}",
                bridge=erp_b.as_source_bridge(),
                handled_actions=("confirm_o2c_order", "complete_o2c_delivery", "post_o2c_invoice", "record_o2c_payment"),
                owned_object_types=("AccountingEntity", "O2cCustomerProfile", "O2cOrder", "O2cDelivery", "O2cInvoice"),
                shared_object_types=("BusinessParty",),
            )
        )
        registry_b.mount(
            MountedDevice(
                device_id="bottlecrm_sqlite",
                name="Cloned BottleCRM SQLite",
                kind="sqlite",
                endpoint=str(crm_copy),
                bridge=SourceBridge(actuate=crm_b.actuate, observe=crm_b.observe),
                handled_actions=("resolve_crm_ticket",),
                owned_object_types=("CrmAccount", "CrmDeal", "CrmTicket"),
                shared_object_types=("BusinessParty",),
            )
        )
        seller_reconciler = SagaReconciler(registry_b.as_source_bridge(), seller_spec)
        rt_b = open_postgres_runtime(
            seller_spec,
            PG_DSN,
            schema_name=schema_b,
            world_mode="coupled",
            source_bridge=seller_reconciler.as_source_bridge(),
        )

        # 3. Safety Shells + MCP Servers + Live Real Hermes Dispatchers
        autonomy_a = AutonomyRampController(
            AutonomyPolicy(default_mode="SHADOW"),
            ledger_path=tmp_dir / "buyer_shadow_ledger.jsonl",
        )
        mcp_a = OntologyMCPServer(
            rt_a,
            Actor(actor_id="buyer-ts-01", role="buyer", clearance_tags={"*"}),
            autonomy_controller=autonomy_a,
        )
        mcp_b = OntologyMCPServer(
            rt_b,
            Actor(actor_id="rep-seller-01", role="sales_rep", clearance_tags={"*"}),
        )
        mdm_plugin = _shared_mdm_sidecar()

        hermes_home_a = tmp_dir / "hermes_buyer_a"
        hermes_home_b = tmp_dir / "hermes_seller_b"
        with start_mcp_http_server(mcp_a, host="127.0.0.1", port=0) as hdl_a, start_mcp_http_server(
            mcp_b, host="127.0.0.1", port=0
        ) as hdl_b:
            scenarios = _execute_four_scenarios(
                rt_a=rt_a,
                rt_b=rt_b,
                erp_a=erp_a,
                erp_b=erp_b,
                crm_b=crm_b,
                buyer_reconciler=buyer_reconciler,
                seller_reconciler=seller_reconciler,
                autonomy_a=autonomy_a,
                mdm_plugin=mdm_plugin,
                mcp_a=mcp_a,
                mcp_b=mcp_b,
                buyer_dispatch_fn=lambda lbl, prompt, _reqs: _run_real_hermes_mcp_turn(
                    hermes_bin=hermes_bin,
                    mcp_srv=mcp_a,
                    mcp_url=hdl_a.mcp_url,
                    prompt=prompt,
                    secret_env=secret_env,
                    agent_label=f"buyer_{lbl}",
                    isolated_home=hermes_home_a,
                ),
                seller_dispatch_fn=lambda lbl, prompt, _reqs: _run_real_hermes_mcp_turn(
                    hermes_bin=hermes_bin,
                    mcp_srv=mcp_b,
                    mcp_url=hdl_b.mcp_url,
                    prompt=prompt,
                    secret_env=secret_env,
                    agent_label=f"seller_{lbl}",
                    isolated_home=hermes_home_b,
                ),
            )
    finally:
        if rt_a is not None:
            rt_a.close()
        if rt_b is not None:
            rt_b.close()

        cleanup_errors: list[str] = []
        try:
            with psycopg.connect(PG_DSN, autocommit=True) as pg_conn:
                pg_conn.execute(f'DROP SCHEMA IF EXISTS "{schema_a}" CASCADE')
                pg_conn.execute(f'DROP SCHEMA IF EXISTS "{schema_b}" CASCADE')
                rem_pg = pg_conn.execute(
                    "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'wo18_%'"
                ).fetchall()
                if rem_pg:
                    cleanup_errors.append(f"PG residual wo18 schemas: {rem_pg}")
        except Exception as exc:
            cleanup_errors.append(f"PG cleanup error: {exc}")

        try:
            jsh_admin.cleanup_all_wo18_tenants()
            jsh_rem = jsh_admin.remaining_wo18_counts()
            if jsh_rem["jsh_user"] != 0 or jsh_rem["jsh_tenant"] != 0:
                cleanup_errors.append(f"jshERP residual wo18 rows: {jsh_rem}")
        except Exception as exc:
            cleanup_errors.append(f"jshERP cleanup error: {exc}")

        try:
            remaining_odoo_dbs = odoo_admin.drop_all_created()
            if remaining_odoo_dbs != ["erp_north", "erp_south", "odoo_demo"]:
                cleanup_errors.append(f"Unexpected Odoo DB list after cleanup: {remaining_odoo_dbs}")
        except Exception as exc:
            cleanup_errors.append(f"Odoo cleanup error: {exc}")

        shutil.rmtree(tmp_dir, ignore_errors=True)
        bottle_sha_after = _file_sha256(SOURCE_BOTTLECRM_DB)
        if bottle_sha_after != bottle_sha_before:
            cleanup_errors.append("bottlecrm.sqlite3 SHA-256 changed!")
        if cleanup_errors:
            raise RuntimeError("WO-18 cleanup failed: " + "; ".join(cleanup_errors))

    report = {
        "ok": True,
        "mode": "live_full_stack",
        "elapsed_sec": round(time.monotonic() - t_start, 2),
        "stack": {
            "enterprise_a_erp": JSHERP_BASE_URL,
            "enterprise_a_pg_schema_cleaned": schema_a,
            "enterprise_b_erp": ODOO_URL,
            "enterprise_b_odoo_db_cleaned": odoo_db,
            "enterprise_b_crm": "cloned vendor_crm/bottlecrm.sqlite3 (cleaned)",
            "enterprise_b_pg_schema_cleaned": schema_b,
            "agent_cli": hermes_bin,
            "agent_model": OPENROUTER_MODEL,
            "agent_provider": "openrouter",
        },
        "cleanup_verified": {
            "pg16_wo18_schemas_remaining": 0,
            "jsherp_wo18_rows_remaining": {"jsh_user": 0, "jsh_tenant": 0},
            "odoo_db_list": ["erp_north", "erp_south", "odoo_demo"],
            "bottlecrm_sha256": _file_sha256(SOURCE_BOTTLECRM_DB),
        },
        **scenarios,
    }
    raw_json = _scrub_secrets(
        json.dumps(report, ensure_ascii=False, indent=2),
        list(secret_env.values()),
    )
    output_path.write_text(raw_json + "\n", encoding="utf-8")
    return json.loads(raw_json)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WO-18 Live Heterogeneous Dual-Enterprise B2B Steel Trade Lab")
    parser.add_argument("--output", default="/tmp/wo18_live_b2b_steel_trade_lab.json")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_live_b2b_steel_trade_lab(output_path=Path(args.output))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
