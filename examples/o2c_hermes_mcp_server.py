#!/usr/bin/env python3
"""OpenOntology O2C + 飞书交互卡片 stdio MCP Server（Hermes Agent 专用）。

特性：
1. 零外部依赖标准 stdio NDJSON JSON-RPC（协议版本 ``2024-11-05``）；
2. 跨进程持久化：将业务沙箱账本（``o2c_world.sqlite3``）与 Odoo/飞书映射元数据
   （``o2c_bridge_meta.json``）落盘到 ``--state-dir``，Hermes 每次唤醒或点击飞书卡片
   回调都能无损接续原订单、原审批挂起单与原飞书卡片 ``message_id``；
3. 自动发现 Hermes Profile（``--profile-dir``）中的 ``.env`` 飞书凭证、
   ``channel_directory.json`` 与 ``feishu-approved.json`` 配对列表；
4. 自动配置 ``NO_PROXY``，确保本地 Clash/HTTP 代理不劫持本机 Odoo 19（8069）与飞书开放平台。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

# 允许在任意 cwd 下直接以脚本方式启动
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_REQUIRED_NO_PROXY = (
    "127.0.0.1",
    "localhost",
    "::1",
    ".feishu.cn",
    ".larksuite.com",
    "open.feishu.cn",
    "msg-frontier.feishu.cn",
)


def _ensure_no_proxy() -> None:
    for env_key in ("NO_PROXY", "no_proxy"):
        existing = [item.strip() for item in os.environ.get(env_key, "").split(",") if item.strip()]
        seen = set(existing)
        for item in _REQUIRED_NO_PROXY:
            if item not in seen:
                existing.append(item)
                seen.add(item)
        os.environ[env_key] = ",".join(existing)


_ensure_no_proxy()

from open_ontology import Actor, InvariantError, OntologyRuntime  # noqa: E402

from examples import o2c_ontology as o2c  # noqa: E402
from examples.feishu_card_bridge import (  # noqa: E402
    FeishuApprovalGateway,
    FeishuOpenAPIClient,
    TransportFn,
    format_rmb,
)
from examples.odoo_o2c_adapter import (  # noqa: E402
    CREDIT_LIMIT_CENTS,
    O2CData,
    O2COrderRef,
    OdooO2CAdapter,
    PARTNER_REF,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "o2c-erp"
SERVER_VERSION = "1.0.0"

_STATUS_CN: dict[str, str] = {
    "draft": "草稿",
    "confirmed": "已确认",
    "sale": "已确认",
    "shipped": "已发货",
    "done": "已出库",
    "assigned": "待出库",
    "invoiced": "已开票",
    "posted": "已过账",
    "paid": "已收款",
    "in_payment": "核销中",
    "not_paid": "待收款",
    "cancel": "已取消",
    "NORMAL": "正常",
    "WATCHLIST": "重点关注",
    "BLOCKED": "信用冻结",
    "OPEN": "待签批挂起",
    "IN_PROGRESS": "签批处理中",
    "COMPLETED": "已签批归档",
    "CANCELLED": "已作废",
    "COMMITTED": "已生效",
    "REJECTED": "已拦截",
    "REVERTED": "已冲正",
}


def _cn(val: str | None) -> str:
    if not val:
        return "无"
    return _STATUS_CN.get(val, val)


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


class PersistentO2CSession:
    """跨进程持久化的 O2C 业务沙箱 + 真实 Odoo 19 适配器 + 飞书卡片网关。"""

    def __init__(
        self,
        state_dir: str | Path,
        profile_dir: str | Path | None = None,
        *,
        feishu_transport: TransportFn | None = None,
        reset_on_init: bool = False,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = Path(profile_dir).expanduser().resolve() if profile_dir else None
        self._feishu_transport = feishu_transport
        self._lock = threading.RLock()

        self.db_path = self.state_dir / "o2c_world.sqlite3"
        self.meta_path = self.state_dir / "o2c_bridge_meta.json"
        self._seq = 0
        self._state_stamp: tuple[int, int, int] | None = None

        self.runtime: OntologyRuntime
        self.adapter: OdooO2CAdapter
        self.gateway: FeishuApprovalGateway

        if reset_on_init or not self._try_hydrate():
            self.reset()
        self._record_state_stamp()

    # -- profile & Feishu auto-discovery ----------------------------------
    def _profile_env(self) -> dict[str, str]:
        if self.profile_dir is None:
            return {}
        return _parse_dotenv(self.profile_dir / ".env")

    def _resolve_approved_open_ids(self) -> list[str]:
        if self.profile_dir is None:
            return []
        pairing = self.profile_dir / "platforms" / "pairing" / "feishu-approved.json"
        if not pairing.is_file():
            return []
        try:
            data = json.loads(pairing.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        return [str(k) for k in data if str(k).startswith("ou_")]

    def _resolve_feishu_target(
        self, explicit_receive_id: str | None, env_map: dict[str, str]
    ) -> tuple[str, str]:
        if explicit_receive_id and explicit_receive_id.strip():
            rid = explicit_receive_id.strip()
            return rid, ("chat_id" if rid.startswith("oc_") else "open_id")

        for key in ("FEISHU_RECEIVE_ID", "FEISHU_HOME_CHANNEL"):
            val = (os.environ.get(key) or env_map.get(key) or "").strip()
            if val:
                rtype = (
                    os.environ.get("FEISHU_RECEIVE_ID_TYPE")
                    or env_map.get("FEISHU_RECEIVE_ID_TYPE")
                    or ("chat_id" if val.startswith("oc_") else "open_id")
                ).strip()
                return val, rtype

        if self.profile_dir is not None:
            ch_file = self.profile_dir / "channel_directory.json"
            if ch_file.is_file():
                try:
                    ch_data = json.loads(ch_file.read_text(encoding="utf-8"))
                    entries = (ch_data.get("platforms") or {}).get("feishu") or []
                    if entries and isinstance(entries[0], dict) and entries[0].get("id"):
                        rid = str(entries[0]["id"]).strip()
                        return rid, ("chat_id" if rid.startswith("oc_") else "open_id")
                except Exception:
                    pass

        approved = self._resolve_approved_open_ids()
        if approved:
            return approved[0], "open_id"
        return "", "open_id"

    def _build_feishu_client(self, receive_id: str | None = None) -> FeishuOpenAPIClient:
        env_map = self._profile_env()
        app_id = (os.environ.get("FEISHU_APP_ID") or env_map.get("FEISHU_APP_ID") or "").strip()
        app_secret = (
            os.environ.get("FEISHU_APP_SECRET") or env_map.get("FEISHU_APP_SECRET") or ""
        ).strip()
        rid, rtype = self._resolve_feishu_target(receive_id, env_map)
        return FeishuOpenAPIClient(
            app_id=app_id,
            app_secret=app_secret,
            receive_id=rid,
            receive_id_type=rtype,
            transport=self._feishu_transport,
        )

    def _refresh_gateway_feishu(self, receive_id: str | None = None) -> None:
        self.gateway.feishu_client = self._build_feishu_client(receive_id=receive_id)
        for open_id in self._resolve_approved_open_ids():
            self.gateway.operator_actors.setdefault(open_id, o2c.FINANCE_DIRECTOR)

    # -- persistence & hydration ------------------------------------------
    def _current_state_stamp(self) -> tuple[int, int, int] | None:
        if not self.db_path.exists() or not self.meta_path.exists():
            return None
        try:
            db_st = self.db_path.stat()
            meta_st = self.meta_path.stat()
            return (db_st.st_ino, db_st.st_mtime_ns, meta_st.st_mtime_ns)
        except OSError:
            return None

    def _record_state_stamp(self) -> None:
        self._state_stamp = self._current_state_stamp()

    def _reload_if_external_change(self) -> bool:
        """Reload SQLite + bridge metadata when modified by another process (e.g. Hermes /card plugin)."""
        with self._lock:
            stamp = self._current_state_stamp()
            if stamp is None or stamp == self._state_stamp:
                return False
            old_runtime = getattr(self, "runtime", None)
            if not self._try_hydrate():
                return False
            if old_runtime is not None and old_runtime is not self.runtime:
                try:
                    old_runtime.close()
                except Exception:
                    pass
            return True

    def _next_run_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _save_meta(self) -> None:
        with self._lock:
            data = self.adapter.data
            if data is None:
                return
            payload = {
                "seq": self._seq,
                "partner_id": data.partner_id,
                "product_id": data.product_id,
                "run_tag": data.run_tag,
                "credit_limit_cents": data.credit_limit_cents,
                "orders": {
                    cid: {
                        "canonical_id": ref.canonical_id,
                        "odoo_id": ref.odoo_id,
                        "odoo_name": ref.odoo_name,
                        "partner_id": ref.partner_id,
                        "product_id": ref.product_id,
                        "quantity": ref.quantity,
                        "unit_price": ref.unit_price,
                        "total_cents": ref.total_cents,
                        "run_tag": ref.run_tag,
                        "product_summary": ref.product_summary,
                    }
                    for cid, ref in data.orders.items()
                },
                "invoices": dict(data.invoices),
                "payments": dict(data.payments),
                "seen_run_ids": sorted(self.adapter._seen),
                "card_records": self.gateway.export_card_metadata(),
            }
            tmp = self.meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.meta_path)
            self._record_state_stamp()

    def _try_hydrate(self) -> bool:
        if not (self.db_path.is_file() and self.meta_path.is_file()):
            return False
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        if "partner_id" not in meta or "product_id" not in meta:
            return False
        orders_raw = meta.get("orders") or {}

        rt = OntologyRuntime(o2c.spec, db_path=self.db_path)
        try:
            adapter = OdooO2CAdapter(rt)
            odoo_ids = [int(item["odoo_id"]) for item in orders_raw.values()]
            if odoo_ids:
                found_ids = set(adapter.client.search("sale.order", [("id", "in", odoo_ids)]))
                if len(found_ids) != len(odoo_ids):
                    rt.close()
                    return False

            orders = {
                cid: O2COrderRef(
                    canonical_id=str(item["canonical_id"]),
                    odoo_id=int(item["odoo_id"]),
                    odoo_name=str(item["odoo_name"]),
                    partner_id=int(item["partner_id"]),
                    product_id=int(item["product_id"]),
                    quantity=int(item["quantity"]),
                    unit_price=float(item["unit_price"]),
                    total_cents=int(item["total_cents"]),
                    run_tag=str(item["run_tag"]),
                    product_summary=str(item.get("product_summary") or "O2C Widget"),
                )
                for cid, item in orders_raw.items()
            }
            adapter.data = O2CData(
                client=adapter.client,
                partner_id=int(meta["partner_id"]),
                product_id=int(meta["product_id"]),
                run_tag=str(meta["run_tag"]),
                credit_limit_cents=int(meta.get("credit_limit_cents", CREDIT_LIMIT_CENTS)),
                orders=orders,
                invoices={k: int(v) for k, v in (meta.get("invoices") or {}).items()},
                payments={k: int(v) for k, v in (meta.get("payments") or {}).items()},
            )
            adapter._seen = set(meta.get("seen_run_ids") or [])

            adapter.bind_coupled(allow_source_revert=True)
            gw = FeishuApprovalGateway(
                rt,
                adapter=adapter,
                feishu_client=self._build_feishu_client(),
                push_all_cards=True,
            )
            for open_id in self._resolve_approved_open_ids():
                gw.operator_actors.setdefault(open_id, o2c.FINANCE_DIRECTOR)
            for run_id, card_meta in (meta.get("card_records") or {}).items():
                gw.restore_card_record(
                    run_id,
                    message_id=card_meta.get("message_id"),
                    erp_evidence=card_meta.get("erp_evidence"),
                    conflict_error=card_meta.get("conflict_error"),
                    card_status_override=card_meta.get("card_status_override"),
                    settled_run_id=card_meta.get("settled_run_id"),
                    baseline_versions=card_meta.get("baseline_versions"),
                )

            self._seq = int(meta.get("seq", len(rt.list_action_runs())))
            self.runtime = rt
            self.adapter = adapter
            self.gateway = gw
            self._record_state_stamp()
            return True
        except Exception:
            rt.close()
            return False

    def close(self) -> None:
        with self._lock:
            if getattr(self, "runtime", None) is not None and self.runtime.is_open:
                self.runtime.close()

    def reset(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            empty_orders = bool((args or {}).get("empty_orders", False))
            old_rt = getattr(self, "runtime", None)
            if old_rt is not None and old_rt.is_open:
                old_rt.close()
            for p in (
                self.db_path,
                Path(f"{self.db_path}-wal"),
                Path(f"{self.db_path}-shm"),
                self.meta_path,
            ):
                p.unlink(missing_ok=True)

            self._seq = 0
            self.runtime = OntologyRuntime(o2c.spec, db_path=self.db_path)
            self.adapter = OdooO2CAdapter(self.runtime)
            self.adapter.seed_odoo(empty_orders=empty_orders)
            self.adapter.sync_from_odoo()
            self.adapter.bind_coupled(allow_source_revert=True)
            self.gateway = FeishuApprovalGateway(
                self.runtime,
                adapter=self.adapter,
                feishu_client=self._build_feishu_client(),
                push_all_cards=True,
            )
            for open_id in self._resolve_approved_open_ids():
                self.gateway.operator_actors.setdefault(open_id, o2c.FINANCE_DIRECTOR)
            self._save_meta()
            return self.overview()

    # -- executive summary helpers (zero jargon) ---------------------------
    def _order_summary(self, order_id: str) -> dict[str, Any]:
        sys_actor = Actor(actor_id="__mcp__", role="system", clearance_tags={"*"})
        order_obj = self.runtime.get_object(o2c.SalesOrder, order_id, sys_actor)
        docs = self.adapter.odoo_documents(order_id)
        return {
            "order_id": order_id,
            "product_summary": order_obj.product_summary,
            "quantity": order_obj.quantity,
            "amount_yuan": format_rmb(order_obj.total_cents),
            "sandbox_status": _cn(order_obj.status),
            "account_rep": order_obj.account_rep or "未分配",
            "fulfillment_note": order_obj.fulfillment_note or "无",
            "odoo_order_name": docs["order_name"],
            "odoo_order_state": _cn(docs["order_state"]),
            "odoo_delivery_name": docs["delivery_name"] or "无",
            "odoo_delivery_state": _cn(docs["delivery_state"]),
            "odoo_invoice_name": docs["invoice_name"] or "无",
            "odoo_invoice_state": _cn(docs["invoice_state"]),
            "odoo_payment_state": _cn(docs["payment_state"]),
            "odoo_residual_yuan": (
                format_rmb(float(docs["amount_residual"]) * 100)
                if docs["amount_residual"] is not None
                else "—"
            ),
        }

    def overview(self) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            sys_actor = Actor(actor_id="__mcp__", role="system", clearance_tags={"*"})
            cust = self.runtime.get_object(o2c.Customer, PARTNER_REF, sys_actor)
            ar_cents = o2c.get_customer_ar_balance(self.runtime, PARTNER_REF, sys_actor)
            remaining = max(0, cust.credit_limit_cents - ar_cents)
            orders = [
                self._order_summary(cid)
                for cid in sorted((self.adapter.data.orders if self.adapter.data else ()))
            ]
            vouchers = [
                {
                    "voucher_id": v.voucher_id,
                    "order_id": v.order,
                    "entry_kind": _cn(v.entry_kind),
                    "delta_yuan": format_rmb(v.delta_cents),
                }
                for v in sorted(
                    self.runtime.query_objects(o2c.ReceivableVoucher, sys_actor),
                    key=lambda item: item.voucher_id,
                )
            ]
            diag = self.runtime.call_function("diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR)
            bottlenecks = {
                "credit_exceeded_draft_orders": [
                    {
                        "order_id": row["order_id"],
                        "order_amount_yuan": format_rmb(row["total_cents"]),
                        "over_limit_yuan": format_rmb(row["over_limit_cents"]),
                    }
                    for row in diag["credit_exceeded_drafts"]
                ],
                "unshipped_confirmed_orders": [row["order_id"] for row in diag["unshipped_confirmed_orders"]],
                "unbilled_shipped_orders": [row["order_id"] for row in diag["unbilled_shipped_orders"]],
                "unpaid_invoices": [row["invoice_id"] for row in diag["unpaid_invoices"]],
            }
            recent_runs = [
                {
                    "run_id": r["run_id"],
                    "status": _cn(r["status"]),
                    "status_code": r["status"],
                    "action_type": r["action_type"],
                    "order_id": (r.get("params") or {}).get("order") or (r.get("params") or {}).get("order_id"),
                    "approved_by": r.get("approved_by"),
                    "error_code": r.get("error_code"),
                }
                for r in self.runtime.list_action_runs()[-8:]
            ]
            self._refresh_gateway_feishu()
            return {
                "ok": True,
                "customer": {
                    "customer_id": cust.customer_id,
                    "name": cust.name,
                    "credit_limit_yuan": format_rmb(cust.credit_limit_cents),
                    "ar_balance_yuan": format_rmb(ar_cents),
                    "remaining_credit_yuan": format_rmb(remaining),
                    "risk_tier": _cn(cust.risk_tier),
                    "credit_hold_note": cust.credit_hold_note or "无",
                },
                "orders": orders,
                "vouchers": vouchers,
                "bottlenecks": bottlenecks,
                "recent_runs": recent_runs,
                "feishu_push_ready": self.gateway.feishu_client.is_configured,
                "feishu_receive_id": self.gateway.feishu_client.receive_id or "未配置",
            }

    def annotate_governance(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            params: dict[str, Any] = {}
            cust_val = args.get("customer") if args.get("customer") is not None else args.get("customer_id")
            if cust_val is not None:
                params["customer"] = cust_val
            ord_val = args.get("order") if args.get("order") is not None else args.get("order_id")
            if ord_val is not None:
                params["order"] = ord_val
            for key in (
                "risk_tier",
                "credit_hold_note",
                "account_rep",
                "fulfillment_note",
            ):
                if args.get(key) is not None:
                    params[key] = args[key]
            run_id = self._next_run_id("O2C-NOTE")
            try:
                run = self.runtime.submit_action(
                    run_id, "annotate_o2c_governance", params, o2c.AI_AGENT
                )
            except InvariantError as exc:
                self._save_meta()
                return {
                    "ok": True,
                    "blocked": True,
                    "run_id": run_id,
                    "error_code": exc.code,
                    "message": f"风控标注被拒绝：{exc}",
                }
            self._save_meta()
            res: dict[str, Any] = {
                "ok": True,
                "run_id": run["run_id"],
                "status": _cn(run["status"]),
                "message": "已写入企业沙箱业务风控备注（真实 ERP 重刷不丢失）。",
            }
            if params.get("order"):
                res["order"] = self._order_summary(str(params["order"]))
            return res

    def _next_order_id(self) -> str:
        sys_actor = Actor(actor_id="__mcp__", role="system", clearance_tags={"*"})
        max_num = 0
        for order in self.runtime.query_objects(o2c.SalesOrder, sys_actor):
            oid = str(order.order_id or "")
            if oid.startswith("O2C-SO-") and oid[7:].isdigit():
                max_num = max(max_num, int(oid[7:]))
        return f"O2C-SO-{max_num + 1:03d}"

    def create_order(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            if args.get("quantity") is None:
                raise ValueError("quantity is required")
            order_id = str(args.get("order_id") or "").strip() or self._next_order_id()
            customer_id = str(args.get("customer_id") or PARTNER_REF).strip()
            product_summary = str(args.get("product_summary") or "O2C Widget").strip() or "O2C Widget"
            quantity = int(args["quantity"])
            unit_price_yuan = float(args.get("unit_price_yuan", 10000.0))
            unit_price_cents = int(round(unit_price_yuan * 100))

            run_id = self._next_run_id("O2C-CREATE")
            try:
                run = self.runtime.submit_action(
                    run_id,
                    "create_sales_order",
                    {
                        "order_id": order_id,
                        "customer": customer_id,
                        "product_summary": product_summary,
                        "quantity": quantity,
                        "unit_price_cents": unit_price_cents,
                    },
                    o2c.AI_AGENT,
                )
            except InvariantError as exc:
                self._save_meta()
                return {
                    "ok": True,
                    "blocked": True,
                    "run_id": run_id,
                    "status_code": "REJECTED",
                    "status": "已拦截",
                    "error_code": exc.code,
                    "message": f"新建销售订单被拒绝：{exc}",
                }

            erp_evidence = self.adapter.get_run_evidence(run["run_id"])

            note_run_id: str | None = None
            note_params: dict[str, Any] = {"order": order_id}
            if args.get("account_rep") is not None:
                note_params["account_rep"] = args["account_rep"]
            if args.get("fulfillment_note") is not None:
                note_params["fulfillment_note"] = args["fulfillment_note"]
            if len(note_params) > 1:
                note_run_id = self._next_run_id("O2C-NOTE")
                self.runtime.submit_action(
                    note_run_id, "annotate_o2c_governance", note_params, o2c.AI_AGENT
                )

            self._save_meta()
            summary = self._order_summary(order_id)
            return {
                "ok": True,
                "blocked": False,
                "run_id": run["run_id"],
                "note_run_id": note_run_id,
                "status_code": run["status"],
                "status": _cn(run["status"]),
                "message": (
                    f"已新建草稿销售订单 {order_id}（{product_summary} × {quantity}，总金额 {summary['amount_yuan']}），"
                    f"并同步写入真实 Odoo 19 报价单 {summary['odoo_order_name']}。"
                ),
                "erp_evidence": erp_evidence,
                "order": summary,
            }

    def confirm_order(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            order_id = str(args.get("order_id") or "").strip()
            if not order_id:
                raise ValueError("order_id is required")

            self._refresh_gateway_feishu(receive_id=args.get("receive_id"))

            # 先提交风控备注再提交订单确认，避免并发版本锁自冲突
            note_run_id: str | None = None
            note_params: dict[str, Any] = {"order": order_id}
            if args.get("account_rep") is not None:
                note_params["account_rep"] = args["account_rep"]
            if args.get("fulfillment_note") is not None:
                note_params["fulfillment_note"] = args["fulfillment_note"]
            if len(note_params) > 1:
                note_run_id = self._next_run_id("O2C-NOTE")
                self.runtime.submit_action(
                    note_run_id, "annotate_o2c_governance", note_params, o2c.AI_AGENT
                )

            confirm_run_id = self._next_run_id("O2C-CONFIRM")
            try:
                run = self.gateway.submit_or_escalate(
                    confirm_run_id,
                    "confirm_sales_order",
                    {"order": order_id},
                    o2c.AI_AGENT,
                )
            except InvariantError as exc:
                self._save_meta()
                return {
                    "ok": True,
                    "blocked": True,
                    "run_id": confirm_run_id,
                    "error_code": exc.code,
                    "message": f"确认订单被安全沙箱拦截：{exc}",
                    "order": self._order_summary(order_id),
                }

            erp_evidence: list[dict[str, Any]] = []
            if run["status"] == "COMMITTED":
                erp_evidence = self.adapter.get_run_evidence(confirm_run_id)
                self.gateway.register_committed_card(confirm_run_id, erp_evidence)

            rec = self.gateway.get_card_record(confirm_run_id)
            self._save_meta()

            if run["status"] == "OPEN":
                msg = (
                    f"订单 {order_id} 触发授信超限红线，已开立财务总监签批单（流水号 {confirm_run_id}）。"
                    "真实 Odoo 仍保持草稿未变动，等待财务总监签批放行。"
                )
            else:
                msg = f"订单 {order_id} 未超授信额度，已自动确认并同步写入真实 Odoo 19。"

            return {
                "ok": True,
                "run_id": confirm_run_id,
                "note_run_id": note_run_id,
                "status_code": run["status"],
                "status": _cn(run["status"]),
                "message": msg,
                "erp_evidence": erp_evidence,
                "card_pushed": bool(rec and rec.message_id),
                "card_message_id": rec.message_id if rec else None,
                "card_push_error": rec.push_error if rec else None,
                "order": self._order_summary(order_id),
            }

    def ship_order(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            order_id = str(args.get("order_id") or "").strip()
            if not order_id:
                raise ValueError("order_id is required")
            run_id = self._next_run_id("O2C-SHIP")
            try:
                run = self.runtime.submit_action(
                    run_id, "ship_order_delivery", {"order": order_id}, o2c.AI_AGENT
                )
            except InvariantError as exc:
                self._save_meta()
                return {
                    "ok": True,
                    "blocked": True,
                    "run_id": run_id,
                    "error_code": exc.code,
                    "message": f"仓库发货被拦截：{exc}",
                    "order": self._order_summary(order_id),
                }
            erp_evidence = self.adapter.get_run_evidence(run["run_id"])
            self._save_meta()
            return {
                "ok": True,
                "run_id": run["run_id"],
                "status_code": run["status"],
                "status": _cn(run["status"]),
                "message": f"订单 {order_id} 已完成真实 Odoo 仓库出库校验（物理出库不可直接撤单）。",
                "erp_evidence": erp_evidence,
                "order": self._order_summary(order_id),
            }

    def issue_invoice(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            order_id = str(args.get("order_id") or "").strip()
            if not order_id:
                raise ValueError("order_id is required")
            self._refresh_gateway_feishu(receive_id=args.get("receive_id"))
            run_id = self._next_run_id("O2C-INVOICE")
            try:
                run = self.runtime.submit_action(
                    run_id, "issue_and_post_invoice", {"order": order_id}, o2c.AI_AGENT
                )
            except InvariantError as exc:
                rec = self.gateway.get_card_record(run_id)
                self._save_meta()
                return {
                    "ok": True,
                    "blocked": True,
                    "run_id": run_id,
                    "status_code": "REJECTED",
                    "status": "已拦截",
                    "error_code": exc.code,
                    "message": (
                        f"安全沙箱已拦截：订单 {order_id} 尚未完成仓库出库，严禁违规开票！"
                        "真实 Odoo 未生成任何发票，违规企图已记入合规审计台。"
                    ),
                    "card_pushed": bool(rec and rec.message_id),
                    "card_message_id": rec.message_id if rec else None,
                    "order": self._order_summary(order_id),
                }

            erp_evidence = self.adapter.get_run_evidence(run["run_id"])
            rec = self.gateway.register_committed_card(run_id, erp_evidence)
            self._save_meta()
            return {
                "ok": True,
                "blocked": False,
                "run_id": run["run_id"],
                "status_code": run["status"],
                "status": _cn(run["status"]),
                "message": f"已为已发货订单 {order_id} 开具并过账真实 Odoo 应收发票。",
                "erp_evidence": erp_evidence,
                "card_pushed": bool(rec and rec.message_id),
                "card_message_id": rec.message_id if rec else None,
                "order": self._order_summary(order_id),
            }

    def record_payment(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            order_id = str(args.get("order_id") or "").strip()
            if not order_id or args.get("payment_yuan") is None:
                raise ValueError("order_id and payment_yuan are required")
            payment_cents = int(round(float(args["payment_yuan"]) * 100))
            run_id = self._next_run_id("O2C-PAY")
            try:
                run = self.runtime.submit_action(
                    run_id,
                    "record_customer_payment",
                    {"order": order_id, "payment_cents": payment_cents},
                    o2c.AI_AGENT,
                )
            except InvariantError as exc:
                self._save_meta()
                return {
                    "ok": True,
                    "blocked": True,
                    "run_id": run_id,
                    "status_code": "REJECTED",
                    "status": "已拦截",
                    "error_code": exc.code,
                    "message": f"收款核销被拦截：{exc}",
                    "order": self._order_summary(order_id),
                }
            erp_evidence = self.adapter.get_run_evidence(run["run_id"])
            self._save_meta()
            return {
                "ok": True,
                "blocked": False,
                "run_id": run["run_id"],
                "status_code": run["status"],
                "status": _cn(run["status"]),
                "message": f"订单 {order_id} 已登记收款 {format_rmb(payment_cents)} 并在真实 Odoo 完成核销。",
                "erp_evidence": erp_evidence,
                "order": self._order_summary(order_id),
            }

    @staticmethod
    def _parse_card_decision_args(args: dict[str, Any]) -> tuple[str, str, str, str]:
        op = str(args.get("op") or "").strip()
        run_id = str(args.get("run_id") or "").strip()
        token = str(args.get("token") or "").strip()
        raw_cmd = str(args.get("raw_card_command") or "").strip()

        if raw_cmd and (not op or not run_id):
            idx = raw_cmd.find("{")
            if idx >= 0:
                parsed = json.loads(raw_cmd[idx:])
                op = op or str(parsed.get("op") or "").strip()
                run_id = run_id or str(parsed.get("run_id") or "").strip()
                token = token or str(parsed.get("token") or "").strip()

        if op not in ("approve", "reject", "revert") or not run_id:
            raise ValueError(
                "必须提供有效的 op (approve|reject|revert) 与 run_id，或提供包含 JSON 的 raw_card_command"
            )
        open_id = str(args.get("operator_open_id") or "").strip()
        return op, run_id, token, open_id

    def handle_card_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            self._refresh_gateway_feishu()
            op, run_id, token, open_id = self._parse_card_decision_args(args)
            if not open_id:
                approved = self._resolve_approved_open_ids()
                open_id = approved[0] if approved else "demo-finance-director"

            val: dict[str, Any] = {"op": op, "run_id": run_id}
            if token:
                val["token"] = token
            payload = {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": open_id},
                    "action": {"value": val},
                },
            }
            result = self.gateway.handle_feishu_callback(payload)
            rec = self.gateway.get_card_record(run_id)
            settled_id = self.gateway._resolve_settled_run_id(run_id)
            run = self.runtime.get_action_run(settled_id) or self.runtime.get_action_run(run_id)
            p = (run or {}).get("params") or {}
            order_id = str(p.get("order") or p.get("order_id") or "")
            if not order_id and rec:
                order_id = str((rec.card_view or {}).get("order_id") or "")
            self._save_meta()

            toast = result.get("toast") or {}
            out: dict[str, Any] = {
                "ok": toast.get("type") != "error",
                "op": op,
                "run_id": run_id,
                "status_code": result.get("status") or (run["status"] if run else None),
                "status": _cn(result.get("status") or (run["status"] if run else None)),
                "error_code": result.get("error_code"),
                "message": toast.get("content") or "已处理卡片决策",
                "erp_evidence": result.get("erp_evidence") or [],
                "card_message_id": rec.message_id if rec else None,
                "card_patched": bool(rec and rec.message_id and not rec.push_error),
                "card_push_error": rec.push_error if rec else None,
            }
            if order_id:
                out["order"] = self._order_summary(order_id)
            return out

    def demo_fault_injection(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._reload_if_external_change()
            mode = str(args.get("mode") or "").strip()
            order_id = str(args.get("order_id") or "O2C-SO-002").strip()
            if mode == "tamper_price":
                new_price = float(args.get("new_unit_price_yuan") or 10500.0)
                info = self.adapter.tamper_draft_order_price(order_id, new_price)
                self._save_meta()
                return {
                    "ok": True,
                    "mode": mode,
                    "message": (
                        f"已在真实 Odoo 19 中将草稿订单 {order_id} ({info['odoo_name']}) 单价上调为 "
                        f"{format_rmb(new_price * 100)}（新总额 {format_rmb(info['total_cents'])}）并同步快照。"
                        "现在点击原飞书卡片【批准并写入 ERP】将触发并发版本锁自动熔断作废。"
                    ),
                    "order": self._order_summary(order_id),
                }
            if mode == "sync_from_erp":
                sync_res = self.adapter.sync_from_odoo()
                self._save_meta()
                return {
                    "ok": True,
                    "mode": mode,
                    "message": (
                        "已完成真实 Odoo 19 全量快照重刷对齐，企业数字沙箱中的风控备注与负责人信息已完整保留。"
                    ),
                    "synced_orders": sync_res["orders"],
                    "order": self._order_summary(order_id),
                }
            raise ValueError(f"unsupported mode {mode!r}; expected 'tamper_price' or 'sync_from_erp'")


# ---------------------------------------------------------------------------
# MCP Tool Registry & Dispatch (stdio NDJSON JSON-RPC 2024-11-05)
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "name": "o2c_overview",
        "description": "查看当前客户授信总额、应收占用、可用余额、真实 Odoo 19 订单/出库单/发票状态与风控备注。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "o2c_reset_demo",
        "description": "一键重置本机演练沙盘与真实 Odoo 19 测试单据（默认恢复 O2C-SO-001 ¥20,000 与 O2C-SO-002 ¥120,000 两笔草稿单；若 empty_orders=true 则清空订单台账从零新建）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "empty_orders": {
                    "type": "boolean",
                    "description": "若为 true，则重置为空白订单台账（仅保留客户与授信主数据，0 笔初始订单），便于从新建订单开始完整演练",
                },
            },
            "required": [],
        },
    },
    {
        "name": "o2c_create_order",
        "description": "为客户新建草稿销售订单（报价单）并同步创建真实 Odoo 19 草稿报价单。未传 order_id 时自动生成 O2C-SO-NNN；若客户处于信用冻结状态会被直接拦截。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "quantity": {"type": "integer", "description": "订购数量（正整数，如 2 或 12）"},
                "unit_price_yuan": {"type": "number", "description": "商品单价（人民币元，默认 10000）"},
                "product_summary": {"type": "string", "description": "商品名称或摘要，默认 O2C Widget"},
                "order_id": {"type": "string", "description": "可选：指定订单编号，如 O2C-SO-003；不传则自动递推"},
                "customer_id": {"type": "string", "description": "可选：客户编号，默认 O2C-CUST-001"},
                "account_rep": {"type": "string", "description": "可选：客户负责人"},
                "fulfillment_note": {"type": "string", "description": "可选：履约特批备注"},
            },
            "required": ["quantity"],
        },
    },
    {
        "name": "o2c_annotate_governance",
        "description": "为客户或销售订单登记风控等级、客户负责人或履约特批备注（即使 ERP 全量重刷也不丢失）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "客户编号，如 O2C-CUST-001"},
                "risk_tier": {"type": "string", "enum": ["NORMAL", "WATCHLIST", "BLOCKED"]},
                "credit_hold_note": {"type": "string", "description": "客户信用风控备注"},
                "order_id": {"type": "string", "description": "销售订单号，如 O2C-SO-002"},
                "account_rep": {"type": "string", "description": "负责人名称"},
                "fulfillment_note": {"type": "string", "description": "履约特批备注，如 战略特批大客户"},
            },
        },
    },
    {
        "name": "o2c_confirm_order",
        "description": "确认销售订单并生成待出库交货单。可附带风控备注；若金额超出可用授信额度，自动挂起并推送飞书审批卡片，真实 ERP 保持草稿。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单编号，如 O2C-SO-001 或 O2C-SO-002"},
                "account_rep": {"type": "string", "description": "可选：同时登记客户负责人"},
                "fulfillment_note": {"type": "string", "description": "可选：同时登记履约特批备注"},
                "receive_id": {"type": "string", "description": "可选：指定接收飞书卡片的会话 chat_id 或用户 open_id"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "o2c_ship_order",
        "description": "在真实 Odoo 19 仓库完成拣货与物理出库确认。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "已确认的订单编号，如 O2C-SO-001"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "o2c_issue_invoice",
        "description": "为订单开具并过账真实 Odoo 应收发票。若订单尚未物理出库，将被安全规则当场拦截并留痕。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单编号，如 O2C-SO-001"},
                "receive_id": {"type": "string", "description": "可选：接收卡片的会话 ID"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "o2c_record_payment",
        "description": "登记客户收款并在真实 Odoo 完成应收发票核销（实收金额必须与发票金额精确一致，单位：元）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单编号"},
                "payment_yuan": {"type": "number", "description": "收款金额（人民币元，例如 20000）"},
            },
            "required": ["order_id", "payment_yuan"],
        },
    },
    {
        "name": "o2c_handle_card_decision",
        "description": "处理飞书交互卡片按钮点击（approve 批准写回 ERP / reject 驳回 / revert 一键时光机冲正恢复 ERP），并原地更新原飞书卡片状态。支持直接传入 /card button {...} 原文或 op + run_id。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["approve", "reject", "revert"]},
                "run_id": {"type": "string", "description": "业务流水号，如 O2C-CONFIRM-0002"},
                "raw_card_command": {"type": "string", "description": "Hermes 转发的 /card button {...} 原始指令文本"},
                "operator_open_id": {"type": "string", "description": "可选：点击按钮的操作人飞书 open_id"},
            },
        },
    },
    {
        "name": "o2c_demo_fault_injection",
        "description": "演练辅助：tamper_price 在真实 Odoo 修改草稿单价触发并发版本锁熔断；sync_from_erp 触发全量重刷验证特批备注存活。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["tamper_price", "sync_from_erp"]},
                "order_id": {"type": "string", "description": "默认 O2C-SO-002"},
                "new_unit_price_yuan": {"type": "number", "description": "改价模式下的新单价（元），默认 10500"},
            },
            "required": ["mode"],
        },
    },
]


class O2CMCPHandler:
    """MCP 2024-11-05 JSON-RPC 请求分派器。"""

    def __init__(self, session: PersistentO2CSession) -> None:
        self.session = session

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        dispatch: dict[str, Callable[[], dict[str, Any]]] = {
            "o2c_overview": self.session.overview,
            "o2c_reset_demo": lambda: self.session.reset(args),
            "o2c_create_order": lambda: self.session.create_order(args),
            "o2c_annotate_governance": lambda: self.session.annotate_governance(args),
            "o2c_confirm_order": lambda: self.session.confirm_order(args),
            "o2c_ship_order": lambda: self.session.ship_order(args),
            "o2c_issue_invoice": lambda: self.session.issue_invoice(args),
            "o2c_record_payment": lambda: self.session.record_payment(args),
            "o2c_handle_card_decision": lambda: self.session.handle_card_decision(args),
            "o2c_demo_fault_injection": lambda: self.session.demo_fault_injection(args),
        }
        fn = dispatch.get(name)
        if fn is None:
            raise KeyError(name)
        return fn()

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None and method:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params") or {}
            name = str(params.get("name") or "")
            if not any(t["name"] == name for t in TOOLS):
                return {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"},
                }
            try:
                result = self.call_tool(name, params.get("arguments"))
                return {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, ensure_ascii=False, indent=2),
                            }
                        ]
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "content": [{"type": "text", "text": f"{name} failed: {exc}"}],
                        "isError": True,
                    },
                }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method and str(method).startswith("notifications/"):
            return None
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        }


def _format_card_notice_text(op: str, run_id: str, res: dict[str, Any]) -> str:
    status_code = str(res.get("status_code") or "").strip()
    status_cn = str(res.get("status") or status_code or "未知状态").strip()
    if op == "approve" and status_code == "COMMITTED":
        headline = f"✅ 已批准并写入真实 Odoo 19（{run_id}）"
    elif op == "reject":
        headline = f"⛔ 已驳回审批申请（{run_id}）"
    elif op == "revert" and status_code == "REVERTED":
        headline = f"⏪ 已完成时光机冲正并恢复 Odoo 草稿（{run_id}）"
    elif not res.get("ok") or status_code in ("CONFLICT", "REJECTED", "FAILED"):
        headline = f"⚠️ 卡片操作未放行（{run_id}，{status_cn}）"
    else:
        headline = f"📋 飞书卡片已处理（{run_id}，{status_cn}）"

    lines = [headline]
    detail = str(res.get("message") or "").strip()
    if detail:
        lines.append(detail)
    order = res.get("order")
    if isinstance(order, dict):
        lines.append(
            f"订单：{order.get('order_id', '-')} / Odoo {order.get('odoo_order_name', '-')}（{order.get('odoo_order_state', '-')}），"
            f"交货单：{order.get('odoo_delivery_name', '无')}（{order.get('odoo_delivery_state', '-')}）"
        )
    return "\n".join(lines[:3])


def _extract_raw_card_json(raw_args: str) -> dict[str, Any] | None:
    text = str(raw_args or "").strip()
    idx = text.find("{")
    if idx < 0:
        return None
    try:
        parsed = json.loads(text[idx:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _maybe_forward_studio_card_callback(
    raw_args: str,
    *,
    profile_dir: str | Path | None,
    operator_open_id: str,
    chat_id: str,
    feishu_transport: TransportFn | None,
) -> dict[str, Any] | None:
    """将带 studio_callback_url 的 /card 按钮回调直接转发至本地演播室，避免触碰 :8789 状态。"""
    parsed = _extract_raw_card_json(raw_args)
    if not parsed:
        return None
    callback_url = str(parsed.get("studio_callback_url") or "").strip()
    if not callback_url:
        return None

    from urllib.parse import urlparse
    import urllib.request

    parsed_url = urlparse(callback_url)
    if parsed_url.scheme != "http" or parsed_url.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(f"unsupported studio_callback_url: {callback_url!r}")

    _ensure_no_proxy()
    op = str(parsed.get("op") or "").strip()
    run_id = str(parsed.get("run_id") or "").strip()
    token = str(parsed.get("token") or "").strip()
    resolved_open_id = str(operator_open_id or "").strip() or "feishu-finance-director"
    callback_payload = {
        "schema": "2.0",
        "event": {
            "operator": {"open_id": resolved_open_id},
            "action": {
                "value": {
                    k: v
                    for k, v in (
                        ("op", op),
                        ("run_id", run_id),
                        ("token", token),
                    )
                    if v
                }
            },
        },
    }
    req = urllib.request.Request(
        callback_url,
        data=json.dumps(callback_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15.0) as resp:
        raw_body = resp.read().decode("utf-8", errors="replace")
    callback_result = json.loads(raw_body) if raw_body.strip() else {}
    toast = callback_result.get("toast") or {}
    toast_content = str(
        toast.get("content") or callback_result.get("message") or "已转发至三看板演播室"
    )

    receipt_sent = False
    target_id = str(chat_id or "").strip() or str(operator_open_id or "").strip()
    target_type = "chat_id" if target_id.startswith("oc_") else "open_id"
    if target_id:
        env: dict[str, str] = {}
        if profile_dir is not None:
            env_path = Path(profile_dir) / ".env"
            if env_path.exists():
                env = _parse_dotenv(env_path)
        client = FeishuOpenAPIClient(
            app_id=env.get("FEISHU_APP_ID", ""),
            app_secret=env.get("FEISHU_APP_SECRET", ""),
            receive_id=target_id,
            receive_id_type=target_type,
            transport=feishu_transport,
        )
        if client.is_configured:
            op_cn = {
                "approve": "批准并写入 ERP",
                "reject": "驳回并保留草稿",
                "revert": "时光机冲正",
            }.get(op, op or "审批")
            client.send_text(
                f"【三看板演播室飞书回执】已收到「{op_cn}」指令（{run_id}）：{toast_content}",
                receive_id=target_id,
                receive_id_type=target_type,
            )
            receipt_sent = True

    return {
        "ok": toast.get("type") != "error" and bool(callback_result.get("ok", True)),
        "forwarded_to": callback_url,
        "op": op,
        "run_id": run_id,
        "receipt_sent": receipt_sent,
        "message": toast_content,
        "callback_result": callback_result,
    }


def execute_feishu_card_slash_command(
    raw_args: str,
    *,
    state_dir: str | Path,
    profile_dir: str | Path | None = None,
    operator_open_id: str = "",
    chat_id: str = "",
    feishu_transport: TransportFn | None = None,
) -> dict[str, Any]:
    """直接处理 Hermes 网关转发的 /card 交互按钮指令并回发飞书文字回执（不经 LLM）。"""
    forwarded = _maybe_forward_studio_card_callback(
        raw_args,
        profile_dir=profile_dir,
        operator_open_id=operator_open_id,
        chat_id=chat_id,
        feishu_transport=feishu_transport,
    )
    if forwarded is not None:
        return forwarded
    session = PersistentO2CSession(
        state_dir,
        profile_dir=profile_dir,
        feishu_transport=feishu_transport,
    )
    try:
        call_args: dict[str, Any] = {"raw_card_command": raw_args}
        if operator_open_id:
            call_args["operator_open_id"] = operator_open_id
        op, run_id, _token, _open_id = PersistentO2CSession._parse_card_decision_args(call_args)
        if chat_id.strip():
            session._refresh_gateway_feishu(receive_id=chat_id.strip())
        res = session.handle_card_decision(call_args)
        notice_text = _format_card_notice_text(op, run_id, res)
        res["notice_text"] = notice_text
        client = session.gateway.feishu_client
        target = chat_id.strip() or client.receive_id or operator_open_id.strip()
        if client.app_id and client.app_secret and target:
            try:
                res["notice_message_id"] = client.send_text(notice_text, receive_id=target)
                res["notice_sent"] = True
                res["notice_error"] = None
            except Exception as exc:
                res["notice_sent"] = False
                res["notice_error"] = str(exc)
        else:
            res["notice_sent"] = False
            res["notice_error"] = None
        return res
    finally:
        session.close()


def _send(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenOntology O2C Hermes MCP Server")
    parser.add_argument(
        "--state-dir",
        default=os.path.expanduser("~/.hermes/profiles/o2c/state/o2c_erp"),
        help="Persistent directory for SQLite and Odoo/Feishu bridge metadata",
    )
    parser.add_argument(
        "--profile-dir",
        default=os.path.expanduser("~/.hermes/profiles/o2c"),
        help="Hermes profile directory for auto-discovering Feishu credentials and targets",
    )
    args = parser.parse_args()

    session = PersistentO2CSession(args.state_dir, profile_dir=args.profile_dir)
    handler = O2CMCPHandler(session)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, list):
                responses = [r for r in (handler.handle_message(m) for m in msg) if r is not None]
                if responses:
                    _send(responses)
                continue
            resp = handler.handle_message(msg)
            if resp is not None:
                _send(resp)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
