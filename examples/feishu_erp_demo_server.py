"""三栏同屏商业演播室与 3 分钟实机剧本：AI 数字员工 + 飞书审批卡片 + 真实 Odoo 19 双向回写与 1 秒冲正。

运行方式：
    # 1. 启动三栏演播室 Web 服务（支持对接真实飞书卡片回调与内置手机飞书模拟器）
    python3 -m examples.feishu_erp_demo_server --port 8788

    # 2. 一键执行 3 分钟四幕全链路无头实机演练（直连真实 Odoo 19）
    python3 -m examples.feishu_erp_demo_server --walkthrough
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from open_ontology import (
    AIPAgentExecutor,
    Actor,
    InvariantError,
    OpenOntologyError,
    OntologyRuntime,
)

from . import o2c_ontology as o2c
from .feishu_card_bridge import (
    FeishuApprovalGateway,
    FeishuOpenAPIClient,
    format_rmb,
    format_run_for_executive,
    start_feishu_ws_listener,
)
from .odoo_o2c_adapter import OdooO2CAdapter, PARTNER_REF

__all__ = [
    "FeishuERPDemoApp",
    "FeishuERPDemoHandle",
    "start_feishu_erp_demo_server",
    "run_walkthrough",
    "SHOWROOM_HTML",
]


_STATUS_BADGE_CN: dict[str, str] = {
    "draft": "草稿",
    "confirmed": "已确认",
    "sale": "已确认 (Sale)",
    "shipped": "已发货",
    "done": "已出库 (Done)",
    "assigned": "待出库 (Assigned)",
    "confirmed_picking": "待拣货",
    "invoiced": "已开票",
    "posted": "已过账 (Posted)",
    "paid": "已收款",
    "in_payment": "收款核销中",
    "not_paid": "待收款",
    "cancel": "已取消",
}


def _cn_state(raw: str | None) -> str:
    if not raw:
        return "无"
    return _STATUS_BADGE_CN.get(raw, raw)


class FeishuERPDemoApp:
    """线程安全的演播室状态容器：封装沙箱运行时、Odoo 适配器与飞书网关。"""

    def __init__(
        self,
        *,
        feishu_client: FeishuOpenAPIClient | None = None,
        callback_token: str = "",
        encrypt_key: str | None = None,
        verification_token: str | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        profile_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._feishu_client = feishu_client
        self._callback_token = callback_token
        self._encrypt_key = encrypt_key
        self._verification_token = verification_token
        self._state_dir = state_dir
        self._profile_dir = profile_dir
        self._session: Any = None
        self._last_meta_mtime: int = 0

        self.runtime: OntologyRuntime
        self.adapter: OdooO2CAdapter
        self.gateway: FeishuApprovalGateway
        self.executor: AIPAgentExecutor
        self.agent_logs: list[dict[str, Any]] = []
        if self._state_dir is not None:
            self._attach_persistent_session(reset=False)
        else:
            self.reset()

    def _attach_persistent_session(self, *, reset: bool) -> None:
        from .o2c_hermes_mcp_server import PersistentO2CSession

        old_session = self._session
        if old_session is not None:
            old_session.close()
        session = PersistentO2CSession(
            self._state_dir,  # type: ignore[arg-type]
            profile_dir=self._profile_dir,
            reset_on_init=reset,
        )
        if self._feishu_client is not None:
            session.gateway.feishu_client = self._feishu_client
        if self._callback_token:
            session.gateway.callback_token = self._callback_token
        if self._encrypt_key is not None:
            session.gateway.encrypt_key = self._encrypt_key
        if self._verification_token is not None:
            session.gateway.verification_token = self._verification_token
        self._session = session
        self.runtime = session.runtime
        self.adapter = session.adapter
        self.gateway = session.gateway
        self.executor = AIPAgentExecutor(self.runtime, o2c.AI_AGENT)
        self._last_meta_mtime = (
            session.meta_path.stat().st_mtime_ns if session.meta_path.is_file() else 0
        )
        self.agent_logs = [
            {
                "title": "已连接 Hermes o2c 持久化账本",
                "detail": "浏览器演播室与飞书 Hermes 助手共享同一份 Odoo 19 账套与沙箱状态。",
                "level": "info",
            }
        ]

    def _sync_external_session_if_needed(self) -> None:
        if self._session is None:
            return
        meta_path = self._session.meta_path
        mtime = meta_path.stat().st_mtime_ns if meta_path.is_file() else 0
        if mtime != self._last_meta_mtime:
            self._attach_persistent_session(reset=False)

    def _save_persistent_if_needed(self) -> None:
        if self._session is not None:
            self._session._save_meta()
            meta_path = self._session.meta_path
            if meta_path.is_file():
                self._last_meta_mtime = meta_path.stat().st_mtime_ns

    def _next_run_id(self, prefix: str) -> str:
        if self._session is not None:
            return self._session._next_run_id(prefix)
        return f"{prefix}-{next(self._seq):04d}"

    def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._state_dir is not None:
                self._attach_persistent_session(reset=True)
                return self.get_state()

            old_rt = getattr(self, "runtime", None)
            if old_rt is not None and old_rt.is_open:
                old_rt.close()

            self.runtime = OntologyRuntime(o2c.spec)
            self.adapter = OdooO2CAdapter(self.runtime)
            self.adapter.seed_odoo()
            self.adapter.sync_from_odoo()
            self.gateway = FeishuApprovalGateway(
                self.runtime,
                adapter=self.adapter,
                feishu_client=self._feishu_client or FeishuOpenAPIClient(),
                callback_token=self._callback_token,
                encrypt_key=self._encrypt_key,
                verification_token=self._verification_token,
            )
            self.executor = AIPAgentExecutor(self.runtime, o2c.AI_AGENT)
            self.agent_logs = [
                {
                    "title": "演播室初始化就绪",
                    "detail": (
                        "已连接真实 Odoo 19 账套并同步初始单据："
                        "小额草稿单 O2C-SO-001（¥20,000.00）与大额草稿单 O2C-SO-002（¥120,000.00），"
                        "客户剩余可用授信 ¥100,000.00。"
                    ),
                    "level": "info",
                }
            ]
            return self.get_state()

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
            elif getattr(self, "runtime", None) is not None and self.runtime.is_open:
                self.runtime.close()

    def _append_log(self, title: str, detail: str, level: str = "info") -> None:
        self.agent_logs.insert(0, {"title": title, "detail": detail, "level": level})
        del self.agent_logs[30:]

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            self._sync_external_session_if_needed()
            sys_actor = Actor(actor_id="__ui__", role="system", clearance_tags={"*"})
            cust = self.runtime.get_object(o2c.Customer, PARTNER_REF, sys_actor)
            ar_cents = o2c.get_customer_ar_balance(self.runtime, PARTNER_REF, sys_actor)
            orders_view: list[dict[str, Any]] = []
            for order_id in sorted(self.adapter.data.orders if self.adapter.data else ()):
                order_obj = self.runtime.get_object(o2c.SalesOrder, order_id, sys_actor)
                docs = self.adapter.odoo_documents(order_id)
                orders_view.append(
                    {
                        "order_id": order_id,
                        "product_summary": order_obj.product_summary,
                        "quantity": order_obj.quantity,
                        "amount_text": format_rmb(order_obj.total_cents),
                        "sandbox_status": _cn_state(order_obj.status),
                        "sandbox_status_raw": order_obj.status,
                        "account_rep": order_obj.account_rep or "未分配",
                        "fulfillment_note": order_obj.fulfillment_note or "无",
                        "odoo_order_name": docs["order_name"],
                        "odoo_order_state": _cn_state(docs["order_state"]),
                        "odoo_order_state_raw": docs["order_state"],
                        "odoo_delivery_name": docs["delivery_name"] or "无",
                        "odoo_delivery_state": _cn_state(docs["delivery_state"]),
                        "odoo_invoice_name": docs["invoice_name"] or "无",
                        "odoo_invoice_state": _cn_state(docs["invoice_state"]),
                        "odoo_payment_state": _cn_state(docs["payment_state"]),
                        "odoo_residual_text": (
                            format_rmb(docs["amount_residual"] * 100)
                            if docs["amount_residual"] is not None
                            else "—"
                        ),
                    }
                )

            audit_runs: list[dict[str, Any]] = []
            for run in reversed(self.runtime.list_action_runs()):
                view = format_run_for_executive(self.runtime, run)
                audit_runs.append(view)

            remaining = max(0, cust.credit_limit_cents - ar_cents)
            return {
                "customer": {
                    "customer_id": cust.customer_id,
                    "name": cust.name,
                    "credit_limit_text": format_rmb(cust.credit_limit_cents),
                    "ar_balance_text": format_rmb(ar_cents),
                    "remaining_credit_text": format_rmb(remaining),
                    "risk_tier": cust.risk_tier,
                    "credit_hold_note": cust.credit_hold_note or "无",
                },
                "orders": orders_view,
                "audit_runs": audit_runs,
                "cards": self.gateway.list_cards(),
                "agent_logs": list(self.agent_logs),
                "feishu_configured": self.gateway.feishu_client.is_configured,
            }

    @staticmethod
    def _infer_scenario_from_prompt(prompt: str) -> str:
        text = prompt.strip()
        if "改价" in text or "冲突" in text or "tamper" in text.lower():
            return "tamper_erp_price"
        if "同步" in text or "刷新" in text or "sync" in text.lower():
            return "sync_erp"
        if "发货" in text or "出库" in text or "ship" in text.lower():
            if "开票" in text or "invoice" in text.lower():
                return "act_invoice_small"
            return "act_confirm_small_and_ship"
        if "O2C-SO-001" in text or "开票" in text or "月底" in text or "invoice" in text.lower():
            return "act1_illegal_invoice"
        if "O2C-SO-002" in text or "12" in text or "特批" in text or "加急" in text:
            return "act2_over_credit_order"
        return "act2_over_credit_order"

    def run_agent_command(
        self,
        *,
        scenario: str | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self._sync_external_session_if_needed()
            try:
                return self._run_agent_command_inner(scenario=scenario, prompt=prompt)
            finally:
                self._save_persistent_if_needed()

    def _run_agent_command_inner(
        self,
        *,
        scenario: str | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
            chosen = (scenario or "").strip() or self._infer_scenario_from_prompt(prompt)

            if chosen == "act1_illegal_invoice":
                run_id = self._next_run_id("ACT1-INVOICE")
                try:
                    self.executor.dispatch(
                        "submit_action",
                        {
                            "run_id": run_id,
                            "action": "issue_and_post_invoice",
                            "params": {"order": "O2C-SO-001"},
                        },
                    )
                except InvariantError as exc:
                    msg = (
                        "安全规则拦截成功：订单 O2C-SO-001 尚未完成仓库出库，"
                        "禁止违规开具应收发票。真实 Odoo 保持零变动，违规企图已记入合规审计台。"
                    )
                    self._append_log("第一幕 · 违规开票被拦截", msg, level="danger")
                    return {
                        "ok": True,
                        "blocked": True,
                        "error_code": exc.code,
                        "run_id": run_id,
                        "message": msg,
                        "state": self.get_state(),
                    }

            if chosen == "act2_over_credit_order":
                note_run_id = self._next_run_id("ACT2-NOTE")
                confirm_run_id = self._next_run_id("ACT2-CONFIRM")
                self.executor.dispatch(
                    "submit_action",
                    {
                        "run_id": note_run_id,
                        "action": "annotate_o2c_governance",
                        "params": {
                            "order": "O2C-SO-002",
                            "account_rep": "AI销售助理",
                            "fulfillment_note": "战略特批大客户",
                        },
                    },
                )
                resp = self.executor.dispatch(
                    "submit_action",
                    {
                        "run_id": confirm_run_id,
                        "action": "confirm_sales_order",
                        "params": {"order": "O2C-SO-002"},
                    },
                )
                run = resp["run"]
                msg = (
                    "AI 已给订单 O2C-SO-002 打上「战略特批大客户」风控备注；"
                    "确认下单时检测到金额 ¥120,000.00 超出可用授信 ¥20,000.00，"
                    "系统已自动挂起并推送飞书审批卡片，等待财务总监签字。此时真实 Odoo 仍为草稿。"
                )
                self._append_log("第二幕 · 超限大单挂起等签批", msg, level="warn")
                return {
                    "ok": True,
                    "run_id": run["run_id"],
                    "note_run_id": note_run_id,
                    "status": run["status"],
                    "message": msg,
                    "state": self.get_state(),
                }

            if chosen == "act_confirm_small_and_ship":
                sys_actor = Actor(actor_id="__ui__", role="system", clearance_tags={"*"})
                order_now = self.runtime.get_object(o2c.SalesOrder, "O2C-SO-001", sys_actor)
                confirm_run_id: str | None = None
                if order_now.status == "draft":
                    confirm_run_id = self._next_run_id("SMALL-CONFIRM")
                    self.executor.dispatch(
                        "submit_action",
                        {
                            "run_id": confirm_run_id,
                            "action": "confirm_sales_order",
                            "params": {"order": "O2C-SO-001"},
                        },
                    )
                    ev_c = self.adapter.process_new_runs()
                    self.gateway.register_committed_card(confirm_run_id, ev_c)

                ship_run_id = self._next_run_id("SMALL-SHIP")
                self.executor.dispatch(
                    "submit_action",
                    {
                        "run_id": ship_run_id,
                        "action": "ship_order_delivery",
                        "params": {"order": "O2C-SO-001"},
                    },
                )
                ev_s = self.adapter.process_new_runs()
                msg = (
                    "小额订单 O2C-SO-001 已完成确认与仓库物理出库（Odoo 出库单状态已为 Done）。"
                    "现在若对确认单强行点击冲正撤单，系统将触发物理出库边界拦截！"
                )
                self._append_log("物理出库护栏演示准备就绪", msg, level="success")
                return {
                    "ok": True,
                    "confirm_run_id": confirm_run_id,
                    "ship_run_id": ship_run_id,
                    "erp_evidence": ev_s,
                    "message": msg,
                    "state": self.get_state(),
                }

            if chosen == "act_invoice_small":
                sys_actor = Actor(actor_id="__ui__", role="system", clearance_tags={"*"})
                order_now = self.runtime.get_object(o2c.SalesOrder, "O2C-SO-001", sys_actor)
                if order_now.status == "draft":
                    c_id = self._next_run_id("SMALL-CONFIRM")
                    self.executor.dispatch(
                        "submit_action",
                        {
                            "run_id": c_id,
                            "action": "confirm_sales_order",
                            "params": {"order": "O2C-SO-001"},
                        },
                    )
                    ev_c = self.adapter.process_new_runs()
                    self.gateway.register_committed_card(c_id, ev_c)
                    order_now = self.runtime.get_object(o2c.SalesOrder, "O2C-SO-001", sys_actor)
                if order_now.status == "confirmed":
                    s_id = self._next_run_id("SMALL-SHIP")
                    self.executor.dispatch(
                        "submit_action",
                        {
                            "run_id": s_id,
                            "action": "ship_order_delivery",
                            "params": {"order": "O2C-SO-001"},
                        },
                    )
                    self.adapter.process_new_runs()

                inv_run_id = self._next_run_id("SMALL-INVOICE")
                self.executor.dispatch(
                    "submit_action",
                    {
                        "run_id": inv_run_id,
                        "action": "issue_and_post_invoice",
                        "params": {"order": "O2C-SO-001"},
                    },
                )
                ev_i = self.adapter.process_new_runs()
                self.gateway.register_committed_card(inv_run_id, ev_i)
                msg = (
                    "已对已发货订单 O2C-SO-001 开具并过账真实 Odoo 应收发票。"
                    "若点击该卡片的【紧急冲正撤回】，将在 Odoo 自动开具红字冲销单！"
                )
                self._append_log("合规开票过账成功", msg, level="success")
                return {
                    "ok": True,
                    "invoice_run_id": inv_run_id,
                    "erp_evidence": ev_i,
                    "message": msg,
                    "state": self.get_state(),
                }

            if chosen == "tamper_erp_price":
                info = self.adapter.tamper_draft_order_price("O2C-SO-002", 10500.0)
                msg = (
                    f"已在真实 Odoo 19 中把草稿单 O2C-SO-002 ({info['odoo_name']}) 单价悄悄上调为 "
                    f"¥10,500.00（总额 {format_rmb(info['total_cents'])}）并同步快照。"
                    "此时去飞书卡片点击【批准并写入 ERP】，将触发并发版本锁自动熔断作废！"
                )
                self._append_log("模拟审批期间 ERP 单据被外部修改", msg, level="warn")
                return {
                    "ok": True,
                    "tamper": info,
                    "message": msg,
                    "state": self.get_state(),
                }

            if chosen == "sync_erp":
                sync_res = self.adapter.sync_from_odoo()
                msg = (
                    "已完成真实 Odoo 19 全量重刷同步（订单/交货/发票状态全部对齐），"
                    "AI 标记的「战略特批大客户」业务备注 100% 保留未丢失。"
                )
                self._append_log("ERP 全量快照重刷完成", msg, level="info")
                return {
                    "ok": True,
                    "sync": {
                        "customers": sync_res["customers"],
                        "orders": sync_res["orders"],
                    },
                    "message": msg,
                    "state": self.get_state(),
                }

            raise ValueError(f"unknown demo scenario {chosen!r}")

    def handle_feishu_callback(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._sync_external_session_if_needed()
            result = self.gateway.handle_feishu_callback(
                payload, headers=headers, raw_body=raw_body
            )
            self._save_persistent_if_needed()
            toast = result.get("toast") or {}
            if toast.get("content"):
                level = "danger" if toast.get("type") == "error" else "success"
                self._append_log("飞书卡片交互回调", str(toast["content"]), level=level)
            if "challenge" not in result:
                result["state"] = self.get_state()
            return result


# ---------------------------------------------------------------------------
# 三栏同屏演播室 HTML（零技术黑话，面向老板/财务总监/IT 总监）
# ---------------------------------------------------------------------------
SHOWROOM_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI 安全操作 ERP · 飞书签批与 1 秒冲正演播室</title>
<style>
  :root {
    --bg: #0b1020;
    --panel: #12192e;
    --panel-2: #18223d;
    --border: #253358;
    --text: #e6ecf8;
    --muted: #93a3c7;
    --blue: #3b82f6;
    --green: #10b981;
    --orange: #f59e0b;
    --red: #ef4444;
    --feishu-bg: #f5f6f7;
    --feishu-card: #ffffff;
    --feishu-text: #1f2329;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 22px;
    background: linear-gradient(90deg, #0f172a, #172554);
    border-bottom: 1px solid var(--border);
  }
  .topbar h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: .3px; }
  .topbar .sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .topbar .actions { display: flex; gap: 10px; align-items: center; }
  .pill {
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 999px;
    background: rgba(16, 185, 129, .14);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, .35);
  }
  .btn {
    border: 0;
    border-radius: 8px;
    padding: 8px 13px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    color: #fff;
    background: var(--blue);
    transition: opacity .15s ease;
  }
  .btn:hover { opacity: .9; }
  .btn.secondary { background: #334155; }
  .btn.warn { background: #d97706; }
  .btn.danger { background: #dc2626; }
  .btn.success { background: #059669; }
  .grid {
    display: grid;
    grid-template-columns: 1.05fr 1.1fr 0.95fr;
    gap: 14px;
    padding: 14px;
    min-height: calc(100vh - 68px);
  }
  .col {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .col-hd {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--panel-2);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .col-hd .title { font-size: 15px; font-weight: 700; }
  .col-hd .tag { font-size: 11px; color: var(--muted); }
  .col-bd { padding: 14px; overflow-y: auto; flex: 1; }
  .metric-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }
  .metric {
    background: rgba(255,255,255,.03);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 10px;
  }
  .metric .k { font-size: 11px; color: var(--muted); }
  .metric .v { font-size: 15px; font-weight: 700; margin-top: 4px; }
  .order-card {
    background: rgba(255,255,255,.03);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    margin-bottom: 10px;
  }
  .order-hd {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
  }
  .badge.draft { background: rgba(148,163,184,.2); color: #cbd5e1; }
  .badge.sale, .badge.confirmed, .badge.COMMITTED { background: rgba(16,185,129,.2); color: #34d399; }
  .badge.PENDING_APPROVAL { background: rgba(245,158,11,.22); color: #fbbf24; }
  .badge.REJECTED, .badge.CONFLICT { background: rgba(239,68,68,.2); color: #fca5a5; }
  .badge.REVERTED { background: rgba(100,116,139,.3); color: #cbd5e1; }
  .kv { font-size: 12px; color: var(--muted); line-height: 1.7; }
  .kv strong { color: var(--text); font-weight: 600; }
  .prompt-box {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  .prompt-box input {
    flex: 1;
    background: #0b1020;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 12px;
    font-size: 13px;
  }
  .script-btns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-bottom: 14px;
  }
  .script-btns .btn { text-align: left; font-size: 12px; padding: 9px 10px; }
  .audit-item {
    border-left: 3px solid var(--blue);
    background: rgba(255,255,255,.03);
    border-radius: 0 8px 8px 0;
    padding: 10px 12px;
    margin-bottom: 9px;
    font-size: 12px;
  }
  .audit-item.REJECTED, .audit-item.CONFLICT { border-left-color: var(--red); }
  .audit-item.PENDING_APPROVAL { border-left-color: var(--orange); }
  .audit-item.COMMITTED { border-left-color: var(--green); }
  .audit-item.REVERTED { border-left-color: #64748b; }
  /* 右屏：飞书移动端交互卡片风格 */
  .feishu-shell {
    background: var(--feishu-bg);
    color: var(--feishu-text);
    border-radius: 12px;
    padding: 12px;
    min-height: 100%;
  }
  .feishu-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 10px;
    margin-bottom: 10px;
    border-bottom: 1px solid #dee0e3;
    font-size: 13px;
    font-weight: 700;
  }
  .f-card {
    background: var(--feishu-card);
    border: 1px solid #dee0e3;
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 12px;
    box-shadow: 0 2px 6px rgba(31,35,41,.06);
  }
  .f-hd {
    padding: 10px 13px;
    font-size: 13px;
    font-weight: 700;
    color: #fff;
  }
  .f-hd.orange { background: #ea580c; }
  .f-hd.green { background: #059669; }
  .f-hd.red { background: #dc2626; }
  .f-hd.grey { background: #64748b; }
  .f-hd.blue { background: #2563eb; }
  .f-bd { padding: 12px 13px; font-size: 12px; line-height: 1.6; }
  .f-diff {
    background: #f8f9fa;
    border: 1px solid #ebedf0;
    border-radius: 6px;
    padding: 8px 10px;
    margin: 8px 0;
  }
  .f-note {
    font-size: 11px;
    color: #646a73;
    border-top: 1px solid #ebedf0;
    padding-top: 7px;
    margin-top: 8px;
  }
  .f-actions {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }
  .f-btn {
    flex: 1;
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid #d0d3d6;
    background: #fff;
    color: #1f2329;
  }
  .f-btn.primary { background: #3370ff; border-color: #3370ff; color: #fff; }
  .f-btn.danger { background: #fff; border-color: #f54a45; color: #f54a45; }
  .toast {
    position: fixed;
    top: 18px;
    right: 22px;
    background: #1e293b;
    color: #fff;
    border: 1px solid #475569;
    padding: 11px 16px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    box-shadow: 0 10px 30px rgba(0,0,0,.45);
    display: none;
    z-index: 99;
  }
</style>
</head>
<body>
  <div id="toast" class="toast"></div>
  <div class="topbar">
    <div>
      <h1>AI 数字员工安全操作 ERP · 飞书签批与 1 秒冲正演播室</h1>
      <div class="sub">左屏：真实 Odoo 19 生产单据 ｜ 中屏：AI 安全沙箱与审计流 ｜ 右屏：财务总监飞书审批卡片</div>
    </div>
    <div class="actions">
      <span id="feishuModePill" class="pill">本地模拟 + 真实飞书双通道</span>
      <button class="btn secondary" onclick="runScenario('sync_erp')">刷新并验证特批备注存活</button>
      <button class="btn danger" onclick="resetDemo()">重置初始演示账套</button>
    </div>
  </div>

  <div class="grid">
    <section class="col">
      <div class="col-hd">
        <span class="title">1. 真实 Odoo 19 生产账本</span>
        <span class="tag">http://localhost:8069 (erp_north)</span>
      </div>
      <div class="col-bd" id="leftPane">加载中...</div>
    </section>

    <section class="col">
      <div class="col-hd">
        <span class="title">2. AI 数字员工控制台与安全审计流</span>
        <span class="tag">先预演防呆 · 再签批写库</span>
      </div>
      <div class="col-bd">
        <div class="prompt-box">
          <input id="promptInput" placeholder="输入业务指令，例如：给华北机电未发货单 O2C-SO-001 开票 / 确认 12 万大单 O2C-SO-002" />
          <button class="btn" onclick="sendPrompt()">派发 AI</button>
        </div>
        <div class="script-btns">
          <button class="btn danger" onclick="runScenario('act1_illegal_invoice')">第一幕：AI 尝试未发货违规开票</button>
          <button class="btn warn" onclick="runScenario('act2_over_credit_order')">第二幕：AI 提交 12 万超限单 + 备注</button>
          <button class="btn secondary" onclick="runScenario('tamper_erp_price')">加分项：审批期 ERP 改价触发熔断</button>
          <button class="btn success" onclick="runScenario('act_confirm_small_and_ship')">护栏项：完成小单物理出库(禁撤)</button>
        </div>
        <div id="centerPane">加载中...</div>
      </div>
    </section>

    <section class="col">
      <div class="col-hd">
        <span class="title">3. 财务总监飞书交互式审批卡片</span>
        <span class="tag">手机点签批 · 点冲正 1 秒恢复</span>
      </div>
      <div class="col-bd">
        <div class="feishu-shell">
          <div class="feishu-top">
            <span>飞书 · 企业风控与 ERP 审批助手</span>
            <span style="color:#646a73;font-weight:500;">财务总监工作台</span>
          </div>
          <div id="rightPane">等待风险审批卡片...</div>
        </div>
      </div>
    </section>
  </div>

<script>
function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { el.style.display = 'none'; }, 4200);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderState(state) {
  const pill = document.getElementById('feishuModePill');
  pill.textContent = state.feishu_configured ? '已直连真实飞书 + 本地同步' : '飞书演播室模式（可配密钥直连真实飞书）';

  const c = state.customer;
  let leftHtml = `
    <div class="metric-row">
      <div class="metric"><div class="k">客户授信总额</div><div class="v">${esc(c.credit_limit_text)}</div></div>
      <div class="metric"><div class="k">当前应收占用</div><div class="v">${esc(c.ar_balance_text)}</div></div>
      <div class="metric"><div class="k">剩余可用授信</div><div class="v" style="color:#34d399">${esc(c.remaining_credit_text)}</div></div>
    </div>
  `;
  for (const o of state.orders) {
    leftHtml += `
      <div class="order-card">
        <div class="order-hd">
          <strong>${esc(o.order_id)}（Odoo ${esc(o.odoo_order_name)}）</strong>
          <span class="badge ${esc(o.odoo_order_state_raw)}">ERP 状态：${esc(o.odoo_order_state)}</span>
        </div>
        <div class="kv">
          金额：<strong>${esc(o.amount_text)}</strong>（${esc(o.product_summary)} × ${esc(o.quantity)}）<br/>
          出库单：<strong>${esc(o.odoo_delivery_name)}</strong>（${esc(o.odoo_delivery_state)}） ｜
          发票：<strong>${esc(o.odoo_invoice_name)}</strong>（${esc(o.odoo_invoice_state)}）<br/>
          负责人：<strong>${esc(o.account_rep)}</strong> ｜
          风控特批备注：<strong style="color:#fbbf24">${esc(o.fulfillment_note)}</strong>
        </div>
      </div>
    `;
  }
  document.getElementById('leftPane').innerHTML = leftHtml;

  let centerHtml = '<div style="font-size:12px;color:#93a3c7;margin-bottom:8px;">实时业务审计流（成功 / 挂起 / 违规拦截 / 冲正全部留痕）</div>';
  for (const r of state.audit_runs) {
    centerHtml += `
      <div class="audit-item ${esc(r.status)}">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
          <strong>${esc(r.action_label)}（${esc(r.order_id || r.run_id)}）</strong>
          <span class="badge ${esc(r.status)}">${esc(r.status_label)}</span>
        </div>
        <div style="color:#cbd5e1;">${esc(r.risk_headline)}</div>
        <div style="color:#94a3b8;margin-top:4px;">发起：${esc(r.actor_label)} ｜ 流水号：${esc(r.run_id)}</div>
      </div>
    `;
  }
  document.getElementById('centerPane').innerHTML = centerHtml;

  let rightHtml = '';
  if (!state.cards.length) {
    rightHtml = '<div style="color:#646a73;font-size:12px;padding:18px;text-align:center;">暂无卡片。点击左侧「第一幕」或「第二幕」触发 AI 执行。</div>';
  }
  for (const item of state.cards) {
    const v = item.view;
    const cls = v.header_template || 'blue';
    let diffRows = (v.changes_summary || []).map(ch =>
      `<div>• <strong>${esc(ch.object_label)} · ${esc(ch.field_label)}</strong>：<code>${esc(ch.before_text)}</code> → <strong>${esc(ch.after_text)}</strong></div>`
    ).join('') || '<div>• 无字段变更（拦截前已终止）</div>';

    let erpBlock = (v.erp_lines && v.erp_lines.length)
      ? `<div style="margin-top:7px;color:#047857;"><strong>ERP 真实回写凭据</strong><br/>${v.erp_lines.map(l => '• ' + esc(l)).join('<br/>')}</div>`
      : '';

    let actionBlock = '';
    if (v.status === 'PENDING_APPROVAL') {
      actionBlock = `
        <div class="f-actions">
          <button class="f-btn primary" onclick="cardAction('approve','${esc(v.run_id)}')">批准并写入 ERP</button>
          <button class="f-btn danger" onclick="cardAction('reject','${esc(v.run_id)}')">驳回申请</button>
        </div>`;
    } else if (v.reversible) {
      actionBlock = `
        <div class="f-actions">
          <button class="f-btn danger" onclick="cardAction('revert','${esc(v.run_id)}')">紧急冲正撤回（1秒恢复 ERP）</button>
        </div>`;
    }

    rightHtml += `
      <div class="f-card">
        <div class="f-hd ${esc(cls)}">${esc(v.header_title)}</div>
        <div class="f-bd">
          <div><strong>发起人：</strong>${esc(v.actor_label)} ｜ <strong>客户：</strong>${esc(v.customer_name)}</div>
          <div style="margin-top:4px;"><strong>风控评估：</strong>${esc(v.risk_headline)}</div>
          <div class="f-diff"><strong>变更影响面预演</strong>${diffRows}</div>
          ${erpBlock}
          ${actionBlock}
          <div class="f-note">流水号：${esc(v.run_id)} ｜ 并发安全锁：${esc(v.version_lock_text)}</div>
        </div>
      </div>
    `;
  }
  document.getElementById('rightPane').innerHTML = rightHtml;
}

async function refreshState() {
  const res = await fetch('/api/state');
  renderState(await res.json());
}

async function runScenario(scenario) {
  const res = await fetch('/api/agent/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scenario})
  });
  const data = await res.json();
  if (data.message) showToast(data.message);
  if (data.state) renderState(data.state);
}

async function sendPrompt() {
  const input = document.getElementById('promptInput');
  const prompt = input.value.trim();
  if (!prompt) return;
  const res = await fetch('/api/agent/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt})
  });
  const data = await res.json();
  if (data.message) showToast(data.message);
  if (data.state) renderState(data.state);
}

async function cardAction(op, runId) {
  const res = await fetch('/api/feishu/card-callback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      schema: '2.0',
      event: {
        operator: {open_id: 'demo-finance-director'},
        action: {value: {op, run_id: runId}}
      }
    })
  });
  const data = await res.json();
  if (data.toast && data.toast.content) showToast(data.toast.content);
  if (data.state) renderState(data.state);
}

async function resetDemo() {
  const res = await fetch('/api/reset', {method: 'POST'});
  const data = await res.json();
  showToast('已重置 Odoo 19 账套与演示沙盘');
  renderState(data);
}

refreshState();
</script>
</body>
</html>
"""


def _make_handler(app: FeishuERPDemoApp) -> type[BaseHTTPRequestHandler]:
    class _DemoHandler(BaseHTTPRequestHandler):
        server_version = "FeishuERPDemo/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def _read_body(self) -> tuple[bytes, dict[str, Any]]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            return raw, body

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/":
                    self._send_bytes(
                        200,
                        "text/html; charset=utf-8",
                        SHOWROOM_HTML.encode("utf-8"),
                    )
                    return
                if path == "/api/state":
                    self._send_json(200, app.get_state())
                    return
                if path == "/api/health":
                    self._send_json(200, {"ok": True})
                    return
                self._send_json(404, {"ok": False, "error": "not found"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                raw, body = self._read_body()
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": f"invalid JSON: {exc}"})
                return

            try:
                if path == "/api/agent/command":
                    res = app.run_agent_command(
                        scenario=body.get("scenario"),
                        prompt=str(body.get("prompt") or ""),
                    )
                    self._send_json(200, res)
                    return
                if path == "/api/feishu/card-callback":
                    hdrs = {k: v for k, v in self.headers.items()}
                    res = app.handle_feishu_callback(body, headers=hdrs, raw_body=raw)
                    self._send_json(200, res)
                    return
                if path == "/api/reset":
                    self._send_json(200, app.reset())
                    return
                self._send_json(404, {"ok": False, "error": "not found"})
            except OpenOntologyError as exc:
                self._send_json(400, {"ok": False, "error": str(exc), "code": getattr(exc, "code", None)})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

    return _DemoHandler


@dataclass
class FeishuERPDemoHandle:
    app: FeishuERPDemoApp
    server: ThreadingHTTPServer
    thread: threading.Thread
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)
        self.app.close()


def start_feishu_erp_demo_server(
    host: str = "127.0.0.1",
    port: int = 8788,
    *,
    app: FeishuERPDemoApp | None = None,
) -> FeishuERPDemoHandle:
    demo_app = app or FeishuERPDemoApp()
    server = ThreadingHTTPServer((host, port), _make_handler(demo_app))
    bound_port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="feishu-erp-demo-http"
    )
    thread.start()
    return FeishuERPDemoHandle(
        app=demo_app,
        server=server,
        thread=thread,
        port=bound_port,
    )


def run_walkthrough() -> dict[str, Any]:
    """一气呵成运行 4 幕 3 分钟实机商业剧本，并校验真实 Odoo 19 单据状态。"""
    app = FeishuERPDemoApp()
    try:
        print("=" * 72)
        print("3 分钟杀手级 Demo：AI 安全操作 ERP · 飞书签批 · 1 秒时光机冲正")
        print("=" * 72)

        # 第一幕：防幻觉拦截
        act1 = app.run_agent_command(scenario="act1_illegal_invoice")
        print(f"[第一幕 · 防幻觉] code={act1['error_code']} | {act1['message']}")
        assert act1["error_code"] == "CANNOT_INVOICE_UNSHIPPED_ORDER"
        assert app.adapter.invoice_row("O2C-SO-001") is None

        # 第二幕：超限 12 万挂起 + 弹出飞书交互卡片
        act2 = app.run_agent_command(scenario="act2_over_credit_order")
        confirm_run_id = act2["run_id"]
        print(
            f"[第二幕 · 人机共治] run={confirm_run_id} status={act2['status']} "
            f"| Odoo O2C-SO-002={app.adapter.order_row('O2C-SO-002')['state']}"
        )
        assert act2["status"] == "PENDING_APPROVAL"
        assert app.adapter.order_row("O2C-SO-002")["state"] == "draft"

        # 第三幕：财务总监飞书卡片点【批准并写入 ERP】+ 同步重刷保留特批备注
        act3 = app.handle_feishu_callback(
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "demo-finance-director"},
                    "action": {"value": {"op": "approve", "run_id": confirm_run_id}},
                },
            }
        )
        app.run_agent_command(scenario="sync_erp")
        sys_actor = Actor(actor_id="__verify__", role="system", clearance_tags={"*"})
        so2_after_approve = app.runtime.get_object(o2c.SalesOrder, "O2C-SO-002", sys_actor)
        odoo_after_approve = app.adapter.order_row("O2C-SO-002")["state"]
        print(
            f"[第三幕 · 双向生效] Odoo O2C-SO-002={odoo_after_approve} "
            f"| 特批备注={so2_after_approve.fulfillment_note!r}"
        )
        assert odoo_after_approve == "sale"
        assert so2_after_approve.fulfillment_note == "战略特批大客户"

        # 第四幕：飞书卡片点【紧急冲正撤回】-> 真实 Odoo 19 退回 draft
        act4 = app.handle_feishu_callback(
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "demo-finance-director"},
                    "action": {"value": {"op": "revert", "run_id": confirm_run_id}},
                },
            }
        )
        odoo_after_revert = app.adapter.order_row("O2C-SO-002")["state"]
        print(
            f"[第四幕 · 1秒冲正] status={act4['status']} "
            f"| Odoo O2C-SO-002={odoo_after_revert} | picking={app.adapter.delivery_row('O2C-SO-002')}"
        )
        assert act4["status"] == "REVERTED"
        assert odoo_after_revert == "draft"
        assert app.adapter.delivery_row("O2C-SO-002") is None

        print("=" * 72)
        print("ALL 4 ACTS PASSED AGAINST LIVE ODOO 19")
        print("=" * 72)
        return {
            "act1": act1["error_code"],
            "act2": act2["status"],
            "act3_odoo_state": odoo_after_approve,
            "act3_note": so2_after_approve.fulfillment_note,
            "act4_odoo_state": odoo_after_revert,
        }
    finally:
        app.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu + Odoo 19 AI ERP Demo Showroom")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--walkthrough", action="store_true", help="Run 4-act CLI walkthrough")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Optional persistent directory shared with Hermes o2c profile",
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="Optional Hermes profile directory for Feishu credentials and targets",
    )
    parser.add_argument(
        "--feishu-ws",
        action="store_true",
        help="Start Feishu WebSocket long-connection listener if lark-oapi is installed",
    )
    args = parser.parse_args()

    if args.walkthrough:
        run_walkthrough()
        return

    demo_app = FeishuERPDemoApp(
        state_dir=args.state_dir,
        profile_dir=args.profile_dir,
    )
    handle = start_feishu_erp_demo_server(host=args.host, port=args.port, app=demo_app)
    print(f"演播室已启动：{handle.base_url}")
    print(f"飞书卡片回调地址：{handle.base_url}/api/feishu/card-callback")
    if args.feishu_ws or (
        os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")
    ):
        try:
            start_feishu_ws_listener(handle.app.gateway)
            print("已启动飞书 WebSocket 长连接监听（免公网 IP）")
        except ImportError:
            print("未安装 lark-oapi，已启用 HTTP 回调与内置飞书手机模拟器")
        except Exception as exc:
            print(f"飞书长连接未启用（继续使用内置模拟与 HTTP 回调）：{exc}")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n正在关闭演播室...")
    finally:
        handle.close()


if __name__ == "__main__":
    main()
