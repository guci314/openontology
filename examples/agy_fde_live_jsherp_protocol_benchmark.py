"""WO-17 Deliverable B: Live Huaxia / jshERP ARM64 Docker + DeepSeek V4.1 Flash Blind FDE Benchmark.

Architecture & Isolation Guarantees:
1. Public Tenant A (``wo17_pub_<rand>``) is provisioned in live ``jshERP-boot`` (:19999)
   with isolated MySQL tenant rows and seeded with 4 progressive P2P flows covering all 4
   ``std_p2p`` actions (``confirm_p2p_order``, ``record_p2p_receipt``, ``post_p2p_bill``,
   ``record_p2p_payment``).
2. A strictly isolated workspace is created:
   - ``target_abi.json`` (``0444``): locked ``std_p2p_ontology`` Target ABI
   - ``world_a_endpoint.json`` (``0444``): generic HTTP REST base_url/openapi_url/login_url/credentials
   - ``requirements.md`` (``0444``): zero jshERP field/endpoint/table spoilers
   - ``SKILL.md`` (``0444``): vendor-neutral OpenOntology FDE skill manual
   - ``.judge/`` (``0444``) + ``verify_driver.sh`` (``0555``): compiler + falsifier
   - ``out/`` (writable ONLY): FDE must author ``out/p2p_mapping.json`` and ``out/driver.py`` (``<= 350`` lines)
3. An ephemeral localhost reset HTTP server lets ``verify_driver.sh verify`` restore
   Public Tenant A's exact MySQL snapshot between falsifier waves without exposing any
   MySQL/Docker credentials or other tenants' data to the workspace.
4. After the FDE achieves ``CORROBORATED`` on Public Tenant A, the independent outer referee:
   - Verifies SHA-256 hashes of all read-only workspace files are untouched;
   - Runs ``audit_thin_driver_ast(..., forbidden_literals=<world_a_fingerprints>, max_lines=350)``;
   - Verifies Public Tenant A independently via ``verify_thin_driver_on_hidden_world``;
   - Creates a brand-new, randomly seeded & PK-shifted Hidden Tenant B (``wo17_hid_<rand>``)
     that the FDE has never seen, and executes ``verify_thin_driver_on_hidden_world`` +
     out-of-band external ``jshERP`` draft PO deletion/cancellation asserting
     ``DEVICE_STATE_MISMATCH`` (``REJECTED``, ``abox_unchanged=True``, zero dirty writes);
   - Unconditionally cleans up all ``wo17_*`` tenant rows from MySQL & Redis in ``try / finally``
     while keeping the 3 ``jsherp-*`` containers healthy for downstream stages.
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.std_p2p_ontology import build_std_p2p_blueprint  # noqa: E402
from open_ontology.design import audit_thin_driver_ast, verify_thin_driver_on_hidden_world  # noqa: E402
from open_ontology.induction import AdapterMappingSpec  # noqa: E402

JSHERP_BASE_URL = os.environ.get("JSHERP_BASE_URL", "http://127.0.0.1:19999/jshERP-boot").rstrip("/")
SKILL_MD_PATH = ROOT / "skills" / "openontology-fde" / "SKILL.md"
STD_P2P_BP_ARG = "examples.std_p2p_ontology:build_std_p2p_blueprint"
BUSINESS_SEED_ARG = "examples.business_ontology:BUSINESS_SEED_ONTOLOGY"

DEFAULT_OPENCODE_MODELS: tuple[str, ...] = ("openrouter/deepseek/deepseek-v4.1-flash",)
DEFAULT_AGY_MODELS: tuple[str, ...] = ("gemini-4-neon-medium",)

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

# Programmatic anti-spoiler guard checked against requirements.md and agent prompts.
_FORBIDDEN_SPOILER_TOKENS: tuple[str, ...] = (
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
    "单据",
    "财务单据",
    "jsherp",
    "jishenghua",
    "管伊佳",
    "华夏",
)

_TENANT_MUTABLE_TABLES: tuple[str, ...] = (
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
)


def _sha256(path: Path) -> str:
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


class _JshErpAdminClient:
    """Referee-only jshERP HTTP + tenant-scoped MySQL/Redis isolation controller."""

    def __init__(self, base_url: str = JSHERP_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.created_tenants: list[dict[str, Any]] = []
        self._snapshots: dict[int, str] = {}

    @staticmethod
    def _assert_safe_wo17_name(login_name: str) -> None:
        if not re.fullmatch(r"wo17_[a-z0-9_]+", login_name):
            raise ValueError(f"Refusing non-wo17 tenant login_name {login_name!r}")

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
        proc = subprocess.run(
            cmd,
            input=stdin_sql,
            text=True,
            capture_output=True,
            check=True,
        )
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
        self._assert_safe_wo17_name(login_name)
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
        """Provision & seed a fresh isolated jshERP tenant with 4 coherent P2P flows."""
        token, tid, pw_md5 = self.register_tenant(login_name, password_plain)

        self._admin_rest("/systemConfig/add", method="POST", token=token, tenant_id=tid, body={"companyName": company_name})
        self._admin_rest(
            "/depot/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"name": depot_name, "type": 0, "isDefault": True, "enabled": True},
        )
        self._admin_rest(
            "/account/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={
                "name": account_name,
                "serialNo": account_serial,
                "initialAmount": 8000000.0,
                "isDefault": True,
                "enabled": True,
            },
        )
        self._admin_rest(
            "/inOutItem/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"name": f"AP-{login_name[-10:]}", "type": "支出", "enabled": True},
        )

        # Optional PK-shift noise created then deleted so Hidden Tenant B PKs never match World A
        for k in range(shift_pks):
            noise_name = f"PKShiftSupplier_{login_name}_{k}"
            self._admin_rest(
                "/supplier/add",
                method="POST",
                token=token,
                tenant_id=tid,
                body={"supplier": noise_name, "type": "供应商", "enabled": True},
            )
            lst = self._admin_rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商', 'supplier': noise_name}, ensure_ascii=False))}",
                token=token,
            )
            for row in (lst.get("data") or {}).get("rows") or []:
                self._admin_rest(f"/supplier/delete?id={row['id']}", method="DELETE", token=token, tenant_id=tid)

        seeded_suppliers: list[dict[str, Any]] = []
        for idx, sup in enumerate(suppliers):
            self._admin_rest(
                "/supplier/add",
                method="POST",
                token=token,
                tenant_id=tid,
                body={
                    "supplier": sup["name"],
                    "type": "供应商",
                    "taxNum": sup["tax_id"],
                    "description": sup.get("payment_terms", "NET30"),
                    "enabled": True,
                },
            )
            lst = self._admin_rest(
                f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商', 'supplier': sup['name']}, ensure_ascii=False))}",
                token=token,
            )
            row = next(r for r in (lst.get("data") or {}).get("rows") or [] if r.get("supplier") == sup["name"])
            seeded_suppliers.append(
                {
                    "id": int(row["id"]),
                    "name": sup["name"],
                    "tax_id": sup["tax_id"],
                    "payment_terms": sup.get("payment_terms", "NET30"),
                    "index": idx,
                }
            )

        self._admin_rest(
            "/material/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={
                "name": material_name,
                "unit": "吨",
                "enabled": True,
                "meList": [
                    {
                        "barCode": material_barcode,
                        "commodityUnit": "吨",
                        "purchaseDecimal": 4000,
                        "commodityDecimal": 4500,
                        "wholesaleDecimal": 4200,
                        "lowDecimal": 3900,
                    }
                ],
            },
        )

        depot_id = int(self._admin_rest("/depot/list", token=token)["data"]["rows"][0]["id"])
        acct_id = int(self._admin_rest("/account/list", token=token)["data"]["rows"][0]["id"])
        mat_row = self._admin_rest("/material/list", token=token)["data"]["rows"][0]
        mat_id = int(mat_row["id"])
        me_rows = self._mysql_exec(
            f"SELECT id FROM jsh_material_extend WHERE tenant_id = {tid} AND material_id = {mat_id} AND delete_flag <> '1' LIMIT 1;"
        ).strip().splitlines()
        me_id = int(me_rows[0]) if me_rows else None

        oper_time = "2026-11-21 10:30:00"
        seeded_flows: list[dict[str, Any]] = []

        for fidx, fspec in enumerate(flow_specs):
            # Optional PK gap inside depot_head / account_head as well
            for gk in range(1 if (shift_pks > 0 and fidx == 0) else 0):
                ghost_po = f"GHOST-{login_name}-{gk}"
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
                                "defaultNumber": ghost_po,
                                "number": ghost_po,
                                "operTime": oper_time,
                                "organId": seeded_suppliers[0]["id"],
                                "totalPrice": 100.0,
                                "discountLastMoney": 100.0,
                                "status": "0",
                            },
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps(
                            [
                                {
                                    "id": "r_ghost",
                                    "materialId": mat_id,
                                    "materialExtendId": me_id,
                                    "barCode": material_barcode,
                                    "name": material_name,
                                    "unit": "吨",
                                    "depotId": depot_id,
                                    "operNumber": 1.0,
                                    "unitPrice": 100.0,
                                    "allPrice": 100.0,
                                }
                            ],
                            ensure_ascii=False,
                        ),
                    },
                )
                gl = self._admin_rest(
                    f"/depotHead/getDetailByNumber?number={urllib.parse.quote(ghost_po)}",
                    token=token,
                )
                gid = int(gl["data"]["id"])
                self._admin_rest(f"/depotHead/delete?id={gid}", method="DELETE", token=token, tenant_id=tid)

            stage = str(fspec["stage"])
            sup = seeded_suppliers[int(fspec.get("supplier_idx", fidx % len(seeded_suppliers)))]
            qty = float(fspec["qty"])
            unit_price = float(fspec["unit_price"])
            total = round(qty * unit_price, 2)
            po_num = str(fspec["po_number"])
            rk_num = fspec.get("receipt_number")
            ah_num = fspec.get("bill_number")

            po_init_status = "0" if stage == "draft" else "1"
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
                            "status": po_init_status,
                        },
                        ensure_ascii=False,
                    ),
                    "rows": json.dumps(
                        [
                            {
                                "id": f"r_po_{fidx}",
                                "materialId": mat_id,
                                "materialExtendId": me_id,
                                "barCode": material_barcode,
                                "name": material_name,
                                "unit": "吨",
                                "depotId": depot_id,
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
            po_detail = self._admin_rest(
                f"/depotHead/getDetailByNumber?number={urllib.parse.quote(po_num)}",
                token=token,
            )["data"]
            po_id = int(po_detail["id"])

            rk_id: int | None = None
            if stage in ("pending_receipt", "draft_bill", "posted_bill"):
                assert rk_num
                rk_status = "0" if stage == "pending_receipt" else "1"
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
                                "status": rk_status,
                            },
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps(
                            [
                                {
                                    "id": f"r_rk_{fidx}",
                                    "materialId": mat_id,
                                    "materialExtendId": me_id,
                                    "barCode": material_barcode,
                                    "name": material_name,
                                    "unit": "吨",
                                    "depotId": depot_id,
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
                rk_detail = self._admin_rest(
                    f"/depotHead/getDetailByNumber?number={urllib.parse.quote(rk_num)}",
                    token=token,
                )["data"]
                rk_id = int(rk_detail["id"])

            ah_id: int | None = None
            if stage in ("draft_bill", "posted_bill"):
                assert ah_num and rk_num
                ah_status = "0" if stage == "draft_bill" else "1"
                self._admin_rest(
                    "/accountHead/addAccountHeadAndDetail",
                    method="POST",
                    token=token,
                    tenant_id=tid,
                    body={
                        "info": json.dumps(
                            {
                                "type": "付款",
                                "billNo": ah_num,
                                "billTime": oper_time,
                                "organId": sup["id"],
                                "accountId": acct_id,
                                "totalPrice": -total,
                                "changeAmount": 0.0,
                                "status": ah_status,
                            },
                            ensure_ascii=False,
                        ),
                        "rows": json.dumps(
                            [
                                {
                                    "accountId": acct_id,
                                    "billNumber": rk_num,
                                    "needDebt": total,
                                    "finishDebt": 0.0,
                                    "eachAmount": 0.0,
                                }
                            ],
                            ensure_ascii=False,
                        ),
                    },
                )
                ah_detail = self._admin_rest(
                    f"/accountHead/getDetailByNumber?billNo={urllib.parse.quote(ah_num)}",
                    token=token,
                )["data"]
                ah_id = int(ah_detail["id"])

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

        fingerprints = [
            login_name,
            password_plain,
            pw_md5,
            company_name,
            depot_name,
            account_name,
            account_serial,
            material_name,
            material_barcode,
            *[s["name"] for s in seeded_suppliers],
            *[s["tax_id"] for s in seeded_suppliers],
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
            "fingerprints": [str(x) for x in fingerprints if x and len(str(x)) >= 4],
        }

    def capture_tenant_snapshot(self, tenant_id: int) -> None:
        self._assert_safe_tenant_id(tenant_id)
        cmd = [
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
            f"--where=tenant_id={int(tenant_id)}",
            "jsh_erp",
            *_TENANT_MUTABLE_TABLES,
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
        self._snapshots[int(tenant_id)] = proc.stdout

    def restore_tenant_snapshot(self, tenant_id: int) -> None:
        self._assert_safe_tenant_id(tenant_id)
        snap = self._snapshots.get(int(tenant_id))
        if not snap:
            raise KeyError(f"Missing snapshot for tenant_id={tenant_id}")
        deletes = "\n".join(
            f"DELETE FROM `{tbl}` WHERE tenant_id = {int(tenant_id)};"
            for tbl in (*reversed(_TENANT_MUTABLE_TABLES), "jsh_log")
        )
        script = f"SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n{deletes}\n{snap}\nSET FOREIGN_KEY_CHECKS=1;\n"
        self._mysql_exec("", stdin_sql=script)

    def cleanup_all_wo17_tenants(self) -> list[int]:
        """Unconditionally remove all wo17_* tenant rows from MySQL and matching Redis keys."""
        raw_tids = self._mysql_exec(
            "SELECT DISTINCT id FROM jsh_user WHERE login_name LIKE 'wo17_%' "
            "UNION SELECT DISTINCT tenant_id FROM jsh_tenant WHERE login_name LIKE 'wo17_%';"
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
        stmts.append(f"DELETE FROM jsh_user WHERE id IN ({in_list});")
        stmts.append("SET FOREIGN_KEY_CHECKS=1;")
        self._mysql_exec("", stdin_sql="\n".join(stmts) + "\n")

        # Remove matching Redis session keys (leaving built-in '63' intact)
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

    def remaining_wo17_counts(self) -> dict[str, int]:
        u = int(self._mysql_exec("SELECT count(*) FROM jsh_user WHERE login_name LIKE 'wo17_%';").strip())
        t = int(self._mysql_exec("SELECT count(*) FROM jsh_tenant WHERE login_name LIKE 'wo17_%';").strip())
        return {"jsh_user": u, "jsh_tenant": t}


def _build_zero_spoiler_requirements() -> str:
    req = """# Autonomous Enterprise ERP Integration Specification (Blind P2P)

## Mission
Integrate the target external REST ERP system onto the locked OpenOntology standard contract
defined in `target_abi.json`. Read connection settings dynamically at runtime from
`world_a_endpoint.json` (`base_url`, `openapi_url`, `login_url`, `username`, `password`,
`password_md5`, `token_header`) and introspect the live HTTP REST service autonomously via
`openapi_url` and authenticated read-only probes.

Do NOT assume or hardcode any tenant login names, credentials, company names, supplier names,
sample record IDs, document numbers, or sample amounts in your generated code. After you finish,
the outer referee will evaluate your submission blindly against an unseen, randomized tenant
whose identifiers, master data, document numbers, quantities, and prices differ completely.

## Authentication & Server Rate-Limit Contract (Transport Only)
- Authenticate dynamically via `POST login_url` with JSON credentials matching the login schema
  exposed in `openapi_url` (using `username` and `password_md5` from `world_a_endpoint.json`,
  and falling back cleanly if `endpoint_config` in another harness omits `password_md5` by
  computing `hashlib.md5(password.encode()).hexdigest()`). Pass the returned auth token in the
  HTTP header named by `token_header`.
- **Per-Second Write Guard**: The live REST server enforces a strict 1-second anti-replay check on
  consecutive mutating API calls within the same functional module: issuing two writes in the
  same wall-clock second can invalidate your current session token and return HTTP 500 with body
  `loginOut`. Therefore, your HTTP helper in `out/driver.py` SHOULD space consecutive mutating
  requests (`time.sleep(1.05)` between writes when needed) AND automatically re-login and retry
  once if a request ever returns `loginOut` or HTTP 401/500 auth expiration.
- Keep `world_a_endpoint` clean during exploration: prefer GET/read probes, and remember
  `./verify_driver.sh verify` resets World A between falsifier waves automatically.

## Required Deliverables (write ONLY inside `out/`)
1. `out/p2p_mapping.json`: valid `AdapterMappingSpec` JSON mapping:
   - All 6 non-owned entity types in `target_abi.json`:
     `AccountingEntity`, `BusinessParty`, `P2pSupplierProfile`, `P2pPurchaseOrder`,
     `P2pGoodsReceipt`, and `P2pVendorBill`
     (ensure every entity mapping uses consistent canonical ID prefixes).
   - All 4 target actions in `target_abi.json`:
     `confirm_p2p_order`, `record_p2p_receipt`, `post_p2p_bill`, and `record_p2p_payment`
     (keep matching `id_prefix`, `expected_state_field`, and `expected_state_after`).
2. `out/driver.py`: ultra-thin Python protocol driver (`<= 350` lines) exposing:
   `build_device_bridge(endpoint_config: Mapping[str, Any], mapping: AdapterMappingSpec) -> SourceBridge`

## Domain & Semantic Expectations
- Derive canonical IDs using `mapping` prefixes from live REST records. Inspect `target_abi.json`
  carefully for every required field type, non-negative numeric field, foreign-key link, and
  FSM transition.
- Reconstruct the true end-to-end P2P lifecycle from live REST records:
  - Procurement order lifecycle: `draft` -> `confirmed` (once order is approved/confirmed and has no
    completed receipt) -> `received` (once a linked incoming receipt is completed/approved and has
    no posted payment/vendor bill) -> `billed` (once a linked vendor bill/payable document is
    approved/posted), or `cancelled` if deleted/closed externally. Watch carefully: if an incoming
    receipt already exists in draft/pending state linked to an approved order, the order may carry
    an intermediate link/completion flag upstream while still logically `confirmed` until the
    receipt itself is approved/completed.
  - Goods receipt (`P2pGoodsReceipt`): when still unapproved/pending (`pending`), report
    `received_qty = 0.0`, `inspection_status = "pending"`, and `status = "pending"`. Once approved
    by `record_p2p_receipt`, reflect `received_qty` (`expected_qty` when fully received),
    `inspection_status = "passed"` (or `"failed"` if short/rejected), and `status = "completed"`
    (or `"disputed"`), plus transition the linked `P2pPurchaseOrder` to `"received"` (or `"disputed"`).
  - Vendor bill (`P2pVendorBill`): map positive unsigned totals (`amount_total`), compute
    `amount_residual = max(0.0, amount_total - paid_amount)`, and derive `payment_state`
    (`"paid"` when residual <= 1e-9, `"partial"` when `0 < paid_amount < amount_total`, else
    `"not_paid"`). Ensure `post_p2p_bill` transitions the bill `draft` -> `posted` (and its parent
    order `received` -> `billed`), and `record_p2p_payment` updates the payment allocation so
    both the payable settlement header/lines and residual update consistently in live ERP storage.
- Handle non-zero API application error payloads cleanly: inspect returned JSON business codes
  (do not assume HTTP 200 always means success), and if an out-of-band external modification or
  deletion has invalidated a target record's state, do not mask or fabricate state in `observe()`
  (catch state-conflict faults cleanly in `actuate` so immediate live `observe()` triggers
  `DEVICE_STATE_MISMATCH` cleanly with zero ABox corruption).
- NEVER echo `move_envelope["object_diff"]` inside `observe()`.

## Verification
Iterate with:
- `./verify_driver.sh check`
- `./verify_driver.sh verify`
until `verify` exits `0` with `ok == true` and `verdict == "CORROBORATED"`.
"""
    lowered = req.lower()
    leaked = [tok for tok in _FORBIDDEN_SPOILER_TOKENS if tok in lowered]
    assert not leaked, f"requirements.md leaked forbidden spoilers: {leaked}"
    return req


class _EphemeralResetServer:
    """Host-side localhost reset server keeping Docker/MySQL reset logic out of workspace."""

    def __init__(self, admin: _JshErpAdminClient, tenant_id: int) -> None:
        self._lock = threading.RLock()
        self.admin = admin
        self.tenant_id = int(tenant_id)
        self._srv: ThreadingHTTPServer | None = None
        self.port: int = 0

    def reset_now(self) -> None:
        with self._lock:
            self.admin.restore_tenant_snapshot(self.tenant_id)

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


def prepare_isolated_jsherp_workspace(
    ws_root: Path,
    *,
    world_a_endpoint: Mapping[str, Any],
    reset_url: str,
) -> dict[str, Any]:
    """Create strictly isolated read-only workspace with writable ``out/`` only."""
    ws_root = ws_root.resolve()
    ws_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(ws_root), check=True)
    out_dir = ws_root / "out"
    judge_dir = ws_root / ".judge"
    out_dir.mkdir(exist_ok=True)
    judge_dir.mkdir(exist_ok=True)

    target_bp = build_std_p2p_blueprint()
    abi_path = ws_root / "target_abi.json"
    abi_path.write_text(json.dumps(target_bp.model_dump(mode="json"), ensure_ascii=False, indent=2), "utf-8")

    ep_path = ws_root / "world_a_endpoint.json"
    ep_path.write_text(json.dumps(dict(world_a_endpoint), ensure_ascii=False, indent=2), "utf-8")

    skill_dest = ws_root / "SKILL.md"
    shutil.copy2(SKILL_MD_PATH, skill_dest)

    req_path = ws_root / "requirements.md"
    req_path.write_text(_build_zero_spoiler_requirements(), "utf-8")

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

    dev_cfg_path = judge_dir / "device_p2p.json"
    dev_cfg_path.write_text(
        json.dumps(
            {
                "device_id": "device_p2p_erp",
                "name": "External P2P REST ERP",
                "kind": "rest_erp",
                "endpoint": str(world_a_endpoint["base_url"]),
                "mapping_path": "../out/p2p_mapping.json",
                "driver_module_path": "../out/driver.py",
                "endpoint_config_path": "../world_a_endpoint.json",
                "reset_hook": "./reset_hook.py:reset_sandbox",
                "owned_object_types": P2P_OWNED_TYPES,
                "handled_actions": P2P_HANDLED_ACTIONS,
            },
            indent=2,
        ),
        "utf-8",
    )

    target_flags = " \\\n    ".join(f'--target-action "{act}"' for act in P2P_HANDLED_ACTIONS)
    verify_sh_path = ws_root / "verify_driver.sh"
    verify_sh_path.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
MODE="${{1:-verify}}"
if [[ "$MODE" != "check" && "$MODE" != "verify" ]]; then
  echo "Usage: ./verify_driver.sh [check|verify]" >&2
  exit 2
fi
export NO_PROXY="127.0.0.1,localhost,::1,${{NO_PROXY:-}}"
export no_proxy="$NO_PROXY"
export PYTHONPATH="{ROOT}:${{PYTHONPATH:-}}"
if [[ "$MODE" == "verify" ]]; then
  exec "{sys.executable}" -m open_ontology.design verify \\
    --blueprint target_abi.json \\
    --seed "{BUSINESS_SEED_ARG}" \\
    --device .judge/device_p2p.json \\
    {target_flags} \\
    --min-action-coverage 1.0 \\
    --min-transition-coverage 0.80 \\
    --max-waves 2
else
  "{sys.executable}" -c "import sys; sys.path.insert(0, '.judge'); import reset_hook; reset_hook.reset_sandbox({{}})"
  exec "{sys.executable}" -m open_ontology.design check \\
    --blueprint target_abi.json \\
    --seed "{BUSINESS_SEED_ARG}" \\
    --device .judge/device_p2p.json
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
    hashes: dict[str, str] = {}
    for fpath in readonly_files:
        hashes[str(fpath.relative_to(ws_root))] = _sha256(fpath)
        fpath.chmod(0o444)
    hashes["verify_driver.sh"] = _sha256(verify_sh_path)
    verify_sh_path.chmod(0o555)
    judge_dir.chmod(0o555)

    return {
        "ws_root": ws_root,
        "readonly_hashes": hashes,
    }


def _verify_readonly_integrity(ws_root: Path, expected_hashes: Mapping[str, str]) -> None:
    for rel, digest in expected_hashes.items():
        path = ws_root / rel
        if not path.exists():
            raise RuntimeError(f"Workspace tamper detected: missing {rel}")
        actual = _sha256(path)
        if actual != digest:
            raise RuntimeError(f"Workspace tamper detected on {rel}: {actual} != {digest}")


def resolve_fde_cli_binary(fde_runner: str) -> str:
    runner = str(fde_runner).strip().lower()
    if runner == "opencode":
        found = shutil.which("opencode")
        if not found and Path("/opt/homebrew/bin/opencode").is_file():
            found = "/opt/homebrew/bin/opencode"
        if not found:
            raise RuntimeError("opencode CLI binary not found")
        return found
    if runner == "agy":
        found = shutil.which("agy")
        if not found:
            raise RuntimeError("agy CLI binary not found")
        return found
    raise ValueError(f"Unsupported fde_runner {fde_runner!r}; expected 'opencode' or 'agy'")


def build_fde_runner_command(
    *,
    fde_runner: str,
    cli_path: str,
    model_name: str,
    prompt: str,
    timeout_sec: int,
    session_id: str | None = None,
) -> list[str]:
    runner = str(fde_runner).strip().lower()
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
    raise ValueError(f"Unsupported fde_runner {fde_runner!r}")


def parse_fde_runner_output(
    *,
    fde_runner: str,
    stdout: str,
    wall_elapsed_sec: float | None = None,
) -> dict[str, Any] | None:
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


def _create_sandbox_shim_bin(shim_dir: Path) -> Path:
    """Create blocking PATH shims preventing blind FDE access to container/DB internals."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/usr/bin/env sh\n"
        'echo "Blocked by blind FDE sandbox: use HTTP REST against world_a_endpoint.json only." >&2\n'
        "exit 1\n"
    )
    for cmd_name in ("docker", "mysql", "mysqldump", "redis-cli", "javap"):
        cmd_path = shim_dir / cmd_name
        cmd_path.write_text(body, encoding="utf-8")
        cmd_path.chmod(0o555)
    return shim_dir


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


def run_live_jsherp_benchmark(
    *,
    fde_runner: str = "opencode",
    models: tuple[str, ...] | None = None,
    timeout_sec: int = 720,
    output_json_path: Path = Path("/tmp/wo17_live_jsherp_deepseek_fde.json"),
) -> dict[str, Any]:
    """Run blind live jshERP P2P FDE benchmark + 4-gate independent referee cage."""
    _ensure_local_no_proxy()
    runner_name = str(fde_runner).strip().lower()
    cli_path = resolve_fde_cli_binary(runner_name)
    resolved_models = (
        models
        if models is not None
        else (DEFAULT_OPENCODE_MODELS if runner_name == "opencode" else DEFAULT_AGY_MODELS)
    )
    admin = _JshErpAdminClient(JSHERP_BASE_URL)
    rand_id = uuid.uuid4().hex[:6]
    ws_root = Path(tempfile.mkdtemp(prefix="wo17_jsherp_ws_")).resolve()
    shim_bin = _create_sandbox_shim_bin(Path(tempfile.mkdtemp(prefix="wo17_shim_bin_")).resolve())
    agent_env = _build_agent_env(ws_root=ws_root, shim_bin=shim_bin)
    if runner_name == "opencode" and any(m.startswith("openrouter/") for m in resolved_models):
        if not agent_env.get("OPENROUTER_API_KEY"):
            shutil.rmtree(ws_root, ignore_errors=True)
            shutil.rmtree(shim_bin, ignore_errors=True)
            raise RuntimeError("OPENROUTER_API_KEY is required for openrouter/* models")
    reset_srv: _EphemeralResetServer | None = None

    try:
        # 1. Seed isolated Public Tenant A before invoking FDE agent
        pub_tag = rand_id.upper()
        world_a_info = admin.seed_p2p_tenant(
            login_name=f"wo17_pub_{rand_id}",
            password_plain=f"PubPass_{rand_id}!",
            company_name=f"Apex Procurement Group {pub_tag}",
            depot_name=f"WH-N-{pub_tag}",
            account_name=f"Operating Bank {pub_tag}",
            account_serial=f"ACC-PUB-{pub_tag}",
            material_name=f"Hot Rolled Coil {pub_tag}",
            material_barcode=f"MAT-PUB-{pub_tag}",
            suppliers=[
                {"name": f"Foundry Alpha {pub_tag}", "tax_id": f"TAX-PUB-A-{pub_tag}", "payment_terms": "NET30"},
                {"name": f"Mill Beta {pub_tag}", "tax_id": f"TAX-PUB-B-{pub_tag}", "payment_terms": "NET45"},
            ],
            flow_specs=[
                {"stage": "draft", "supplier_idx": 0, "qty": 10.0, "unit_price": 4000.0, "po_number": f"PO-PUB1-{pub_tag}"},
                {
                    "stage": "pending_receipt",
                    "supplier_idx": 1,
                    "qty": 8.0,
                    "unit_price": 4150.0,
                    "po_number": f"PO-PUB2-{pub_tag}",
                    "receipt_number": f"RK-PUB2-{pub_tag}",
                },
                {
                    "stage": "draft_bill",
                    "supplier_idx": 0,
                    "qty": 6.0,
                    "unit_price": 4300.0,
                    "po_number": f"PO-PUB3-{pub_tag}",
                    "receipt_number": f"RK-PUB3-{pub_tag}",
                    "bill_number": f"FK-PUB3-{pub_tag}",
                },
                {
                    "stage": "posted_bill",
                    "supplier_idx": 1,
                    "qty": 5.0,
                    "unit_price": 4450.0,
                    "po_number": f"PO-PUB4-{pub_tag}",
                    "receipt_number": f"RK-PUB4-{pub_tag}",
                    "bill_number": f"FK-PUB4-{pub_tag}",
                },
            ],
            shift_pks=0,
        )

        world_a_ep = {
            "base_url": JSHERP_BASE_URL,
            "openapi_url": f"{JSHERP_BASE_URL}/v2/api-docs",
            "login_url": f"{JSHERP_BASE_URL}/user/login",
            "username": world_a_info["login_name"],
            "password": world_a_info["password_plain"],
            "password_md5": world_a_info["password_md5"],
            "token_header": "X-Access-Token",
        }

        reset_srv = _EphemeralResetServer(admin, world_a_info["tenant_id"])
        reset_url = reset_srv.start()
        prep = prepare_isolated_jsherp_workspace(
            ws_root,
            world_a_endpoint=world_a_ep,
            reset_url=reset_url,
        )

        prompt = (
            "Your current directory '.' already contains ./SKILL.md, ./requirements.md, ./target_abi.json, "
            "./world_a_endpoint.json, ./verify_driver.sh, and the writable ./out/ directory — read these local files directly "
            "and do NOT search outside '.' or touch external containers/repos. "
            "Autonomously inspect openapi_url (via a short Python script filtering relevant endpoints/schemas) and the live "
            "authenticated HTTP REST API of world_a_endpoint.json, author ONLY ./out/p2p_mapping.json (AdapterMappingSpec) "
            "and ./out/driver.py (<= 350 lines exposing build_device_bridge(endpoint_config, mapping) -> SourceBridge), "
            "and iterate with ./verify_driver.sh check and ./verify_driver.sh verify until ./verify_driver.sh verify exits 0 "
            "with ok=true and verdict=CORROBORATED. "
            "NEVER hardcode tenant credentials, company/supplier names, IDs, document numbers, or amounts in driver.py; "
            "an unseen randomized tenant will be tested after you exit."
        )
        assert not [tok for tok in _FORBIDDEN_SPOILER_TOKENS if tok in prompt.lower()]

        # 2. Execute external coding-agent FDE inside isolated workspace
        attempts: list[dict[str, Any]] = []
        chosen_model: str | None = None
        runner_envelope: dict[str, Any] | None = None
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

            parsed_env = parse_fde_runner_output(fde_runner=runner_name, stdout=stdout, wall_elapsed_sec=wall_sec)
            out_ready = (ws_root / "out" / "p2p_mapping.json").is_file() and (ws_root / "out" / "driver.py").is_file()
            probe_ver: dict[str, Any] | None = None
            ast_precheck: list[str] = []
            if out_ready:
                ast_precheck = audit_thin_driver_ast(
                    ws_root / "out" / "driver.py",
                    forbidden_literals=world_a_info["fingerprints"],
                    max_lines=350,
                )
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
                    "ast_errors": ast_precheck[:5],
                    "verify_ok": bool(probe_ver and probe_ver.get("ok") is True),
                    "verify_errors": (probe_ver or {}).get("errors", [])[:5],
                    "stderr_tail": stderr[-400:],
                }
            )

            if out_ready and not ast_precheck and probe_ver and probe_ver.get("ok") is True:
                chosen_model = model_name
                runner_envelope = parsed_env
                break

            conv_id = str((parsed_env or {}).get("conversation_id") or "")
            if conv_id and not timed_out and rc == 0:
                for repair_idx in range(1, 4):
                    ast_note = f" Static AST gate issues: {ast_precheck[:3]}." if ast_precheck else ""
                    repair_prompt = (
                        "Run ./verify_driver.sh check and ./verify_driver.sh verify right now, "
                        "read every diagnostic error or counterexample carefully, and fix "
                        "out/p2p_mapping.json and/or out/driver.py until ./verify_driver.sh verify "
                        f"exits 0 with ok=true and verdict=CORROBORATED.{ast_note}"
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
                    out_ready_rep = (ws_root / "out" / "p2p_mapping.json").is_file() and (
                        ws_root / "out" / "driver.py"
                    ).is_file()
                    probe_rep: dict[str, Any] | None = None
                    ast_precheck = []
                    if out_ready_rep:
                        ast_precheck = audit_thin_driver_ast(
                            ws_root / "out" / "driver.py",
                            forbidden_literals=world_a_info["fingerprints"],
                            max_lines=350,
                        )
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
                            "ast_errors": ast_precheck[:5],
                            "verify_ok": bool(probe_rep and probe_rep.get("ok") is True),
                            "verify_errors": (probe_rep or {}).get("errors", [])[:5],
                            "stderr_tail": stderr_rep[-400:],
                        }
                    )
                    if out_ready_rep and not ast_precheck and probe_rep and probe_rep.get("ok") is True:
                        chosen_model = model_name
                        runner_envelope = parsed_rep
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
                (ws_root / "out" / "p2p_mapping.json").read_text("utf-8")
                if (ws_root / "out" / "p2p_mapping.json").is_file()
                else None
            )
            fail_report = {
                "ok": False,
                "fde_runner": runner_name,
                "workspace": str(ws_root),
                "world_a": {k: v for k, v in world_a_info.items() if "password" not in k},
                "attempts": attempts,
                "last_verify_probe": last_verify_probe,
                "partial_driver_source": partial_driver,
                "partial_mapping_raw": partial_mapping,
                "error": f"{runner_name} CLI did not achieve CORROBORATED on Public Tenant A",
            }
            output_json_path.write_text(json.dumps(fail_report, ensure_ascii=False, indent=2), "utf-8")
            raise RuntimeError(f"Live {runner_name} benchmark failed on Public Tenant A: see {output_json_path}")

        # 3. Outer Referee Gate 1 & Gate 2: integrity + AST + independent Public Tenant A verification
        _verify_readonly_integrity(ws_root, prep["readonly_hashes"])

        driver_path = ws_root / "out" / "driver.py"
        mapping_path = ws_root / "out" / "p2p_mapping.json"
        driver_source = driver_path.read_text("utf-8")
        driver_lines = len(driver_source.splitlines())
        mapping_obj = AdapterMappingSpec.model_validate(json.loads(mapping_path.read_text("utf-8")))

        ast_issues = audit_thin_driver_ast(
            driver_path,
            forbidden_literals=world_a_info["fingerprints"],
            max_lines=350,
        )
        assert ast_issues == [], f"Outer AST cage rejected driver.py: {ast_issues}"

        world_a_verify = verify_thin_driver_on_hidden_world(
            blueprints=[STD_P2P_BP_ARG],
            seed=BUSINESS_SEED_ARG,
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config=world_a_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id="device_p2p_pub",
            device_kind="rest_erp",
            owned_object_types=P2P_OWNED_TYPES,
            handled_actions=P2P_HANDLED_ACTIONS,
            target_actions=P2P_HANDLED_ACTIONS,
            reset_fn=lambda _ep: admin.restore_tenant_snapshot(world_a_info["tenant_id"]),
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=2,
        )
        assert world_a_verify["ok"] and world_a_verify["verdict"] == "CORROBORATED", world_a_verify

        # 4. Outer Referee Gate 3 & Gate 4: Blind Hidden Tenant B (provisioned ONLY AFTER FDE exited)
        hid_rand = uuid.uuid4().hex[:6]
        hid_tag = f"HID{hid_rand.upper()}"
        rnd = random.SystemRandom()
        shift_pks = rnd.randint(4, 9)
        q1, p1 = float(rnd.randint(7, 13)), round(rnd.uniform(3100.0, 4900.0), 2)
        q2, p2 = float(rnd.randint(5, 11)), round(rnd.uniform(3300.0, 5200.0), 2)
        q3, p3 = float(rnd.randint(4, 9)), round(rnd.uniform(3500.0, 5600.0), 2)
        q4, p4 = float(rnd.randint(3, 8)), round(rnd.uniform(3800.0, 5900.0), 2)

        world_b_info = admin.seed_p2p_tenant(
            login_name=f"wo17_hid_{hid_rand}",
            password_plain=f"HidPass_{hid_rand}!",
            company_name=f"Borealis Heavy Trade {hid_tag}",
            depot_name=f"WH-S-{hid_tag}",
            account_name=f"Settlement Treasury {hid_tag}",
            account_serial=f"ACC-HID-{hid_tag}",
            material_name=f"Cold Rolled Plate {hid_tag}",
            material_barcode=f"MAT-HID-{hid_tag}",
            suppliers=[
                {"name": f"Zenith Metallurgy {hid_tag}", "tax_id": f"TAX-HID-A-{hid_tag}", "payment_terms": "NET15"},
                {"name": f"Solstice Alloys {hid_tag}", "tax_id": f"TAX-HID-B-{hid_tag}", "payment_terms": "NET60"},
            ],
            flow_specs=[
                {"stage": "draft", "supplier_idx": 0, "qty": q1, "unit_price": p1, "po_number": f"PO-HID1-{hid_tag}"},
                {
                    "stage": "pending_receipt",
                    "supplier_idx": 1,
                    "qty": q2,
                    "unit_price": p2,
                    "po_number": f"PO-HID2-{hid_tag}",
                    "receipt_number": f"RK-HID2-{hid_tag}",
                },
                {
                    "stage": "draft_bill",
                    "supplier_idx": 0,
                    "qty": q3,
                    "unit_price": p3,
                    "po_number": f"PO-HID3-{hid_tag}",
                    "receipt_number": f"RK-HID3-{hid_tag}",
                    "bill_number": f"FK-HID3-{hid_tag}",
                },
                {
                    "stage": "posted_bill",
                    "supplier_idx": 1,
                    "qty": q4,
                    "unit_price": p4,
                    "po_number": f"PO-HID4-{hid_tag}",
                    "receipt_number": f"RK-HID4-{hid_tag}",
                    "bill_number": f"FK-HID4-{hid_tag}",
                },
            ],
            shift_pks=shift_pks,
        )

        hid_ep = {
            "base_url": JSHERP_BASE_URL,
            "openapi_url": f"{JSHERP_BASE_URL}/v2/api-docs",
            "login_url": f"{JSHERP_BASE_URL}/user/login",
            "username": world_b_info["login_name"],
            "password": world_b_info["password_plain"],
            "password_md5": world_b_info["password_md5"],
            "token_header": "X-Access-Token",
        }

        po_em = mapping_obj.get_entity_mapping("P2pPurchaseOrder")
        po_prefix = po_em.id_transform.prefix if po_em is not None else "PO-"

        def _oob_tamper_hidden_jsherp(_ep: Mapping[str, Any], m0_before: Sequence[dict[str, Any]]) -> dict[str, Any]:
            draft_po = next(
                e for e in m0_before if e["object_type"] == "P2pPurchaseOrder" and e["data"]["status"] == "draft"
            )
            canonical_pid = str(draft_po["object_id"])
            stripped = (
                canonical_pid[len(po_prefix) :]
                if po_prefix and canonical_pid.startswith(po_prefix)
                else canonical_pid
            )
            if stripped.isdigit():
                raw_id = int(stripped)
            else:
                m = re.search(r"(\d+)$", stripped)
                raw_id = int(m.group(1)) if m else int(world_b_info["flows"][0]["po_id"])

            tok_b, tid_b = admin.login(world_b_info["login_name"], world_b_info["password_md5"])
            admin._admin_rest(f"/depotHead/delete?id={raw_id}", method="DELETE", token=tok_b, tenant_id=tid_b)
            return {
                "action": "confirm_p2p_order",
                "params": {"po_id": canonical_pid},
                "actor_role": "buyer",
            }

        hidden_verify = verify_thin_driver_on_hidden_world(
            blueprints=[STD_P2P_BP_ARG],
            seed=BUSINESS_SEED_ARG,
            driver_path=driver_path,
            mapping_path=mapping_path,
            hidden_endpoint_config=hid_ep,
            forbidden_literals=world_a_info["fingerprints"],
            max_driver_lines=350,
            device_id="device_p2p_hid",
            device_kind="rest_erp",
            owned_object_types=P2P_OWNED_TYPES,
            handled_actions=P2P_HANDLED_ACTIONS,
            target_actions=P2P_HANDLED_ACTIONS,
            reset_fn=lambda _ep: admin.restore_tenant_snapshot(world_b_info["tenant_id"]),
            oob_tamper_check=_oob_tamper_hidden_jsherp,
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=2,
        )
        oob_ok = (
            hidden_verify.get("oob_tamper_result") is not None
            and hidden_verify["oob_tamper_result"].get("passed") is True
            and hidden_verify["oob_tamper_result"].get("caught_error_code") == "DEVICE_STATE_MISMATCH"
            and hidden_verify["oob_tamper_result"].get("audit_status") == "REJECTED"
            and hidden_verify["oob_tamper_result"].get("abox_unchanged") is True
        )
        assert hidden_verify["ok"] and hidden_verify["verdict"] == "CORROBORATED", hidden_verify
        assert oob_ok, hidden_verify.get("oob_tamper_result")

        pub_fal = world_a_verify.get("falsification") or {}
        hid_fal = hidden_verify.get("falsification") or {}
        report = {
            "ok": True,
            "fde_runner": runner_name,
            "chosen_model": chosen_model,
            "runner_telemetry": runner_envelope,
            "attempts": attempts,
            "driver_lines": driver_lines,
            "ast_audit_issues": ast_issues,
            "world_a": {
                "tenant_id": world_a_info["tenant_id"],
                "login_name": world_a_info["login_name"],
                "verdict": world_a_verify["verdict"],
                "action_coverage_ratio": pub_fal.get("action_coverage_ratio"),
                "transition_coverage_ratio": pub_fal.get("transition_coverage_ratio"),
                "covered_actions": pub_fal.get("covered_actions"),
                "flows": world_a_info["flows"],
            },
            "world_b_hidden": {
                "tenant_id": world_b_info["tenant_id"],
                "login_name": world_b_info["login_name"],
                "shift_pks": shift_pks,
                "verdict": hidden_verify["verdict"],
                "action_coverage_ratio": hid_fal.get("action_coverage_ratio"),
                "transition_coverage_ratio": hid_fal.get("transition_coverage_ratio"),
                "covered_actions": hid_fal.get("covered_actions"),
                "flows": world_b_info["flows"],
            },
            "oob_tamper": hidden_verify["oob_tamper_result"],
            "driver_source": driver_source,
            "mapping_json": json.loads(mapping_path.read_text("utf-8")),
        }
        output_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return report
    finally:
        if reset_srv is not None:
            reset_srv.stop()
        cleaned_tids = admin.cleanup_all_wo17_tenants()
        rem = admin.remaining_wo17_counts()
        shutil.rmtree(ws_root, ignore_errors=True)
        shutil.rmtree(shim_bin, ignore_errors=True)
        if rem["jsh_user"] != 0 or rem["jsh_tenant"] != 0:
            raise RuntimeError(f"Tenant cleanup left residual wo17 rows: {rem} (cleaned={cleaned_tids})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WO-17 Live jshERP x DeepSeek Blind P2P FDE Benchmark")
    parser.add_argument("--fde-runner", default="opencode", choices=("opencode", "agy"))
    parser.add_argument("--model", action="append", default=None, help="Override candidate model(s)")
    parser.add_argument("--timeout", type=int, default=720)
    parser.add_argument("--output", default="/tmp/wo17_live_jsherp_deepseek_fde.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    models = tuple(args.model) if args.model else None
    report = run_live_jsherp_benchmark(
        fde_runner=args.fde_runner,
        models=models,
        timeout_sec=args.timeout,
        output_json_path=Path(args.output),
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "fde_runner": report["fde_runner"],
                "chosen_model": report["chosen_model"],
                "driver_lines": report["driver_lines"],
                "world_a": {
                    k: report["world_a"][k]
                    for k in ("tenant_id", "verdict", "action_coverage_ratio", "transition_coverage_ratio", "covered_actions")
                },
                "world_b_hidden": {
                    k: report["world_b_hidden"][k]
                    for k in ("tenant_id", "shift_pks", "verdict", "action_coverage_ratio", "transition_coverage_ratio", "covered_actions")
                },
                "oob_tamper": report["oob_tamper"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
