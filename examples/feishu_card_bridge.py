"""飞书交互式审批卡片、OpenAPI 双向桥接与 ERP 一键冲正网关。

架构定位：
* 属于外部协同网关与 PSM 表现层，不进入 ``open_ontology`` 微内核。
* 对接 ``OntologyRuntime`` 的全生命周期运行事件（待审批 / 已生效 / 已拦截 / 已冲正），
  把底层结构化变更集翻译为**零技术黑话**的高管业务审批卡片。
* 同时支持：
  1. 真实飞书自建应用推送（``open.feishu.cn``）与卡片回调（v1 / v2 / 长连接 WS）；
  2. 本地三栏演播室沙盘直接复用同一套回调处理器，无飞书账号也可完整演示与测试。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from open_ontology import (
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    InvariantError,
    OntologyRuntime,
    PermissionDeniedError,
)

from . import o2c_ontology as o2c

__all__ = [
    "FeishuOpenAPIClient",
    "FeishuApprovalGateway",
    "FeishuGatewayError",
    "format_run_for_executive",
    "build_feishu_card",
    "verify_feishu_signature",
    "start_feishu_ws_listener",
]

TransportFn = Callable[[str, str, dict[str, str], bytes, float], tuple[int, bytes]]

_ACTION_LABELS: dict[str, str] = {
    "create_sales_order": "新建草稿销售订单",
    "confirm_sales_order": "确认销售订单并锁库",
    "ship_order_delivery": "仓库确认发货出库",
    "issue_and_post_invoice": "开具并过账应收发票",
    "record_customer_payment": "登记客户收款核销",
    "annotate_o2c_governance": "登记业务风控特批备注",
}

_ACTOR_ROLE_LABELS: dict[str, str] = {
    "ai_agent": "AI 销售助理",
    "sales_rep": "销售专员",
    "warehouse": "仓储主管",
    "finance": "财务专员",
    "finance_director": "财务总监",
    "ops_manager": "运营总监",
}

_ACTOR_ID_LABELS: dict[str, str] = {
    "ai-agent-01": "AI 销售助理",
    "sales-rep-01": "销售专员",
    "warehouse-01": "仓储主管",
    "finance-director-01": "财务总监",
}

_OBJECT_LABELS: dict[str, str] = {
    "Customer": "客户授信档案",
    "ReceivableBalance": "客户应收余额台账",
    "SalesOrder": "销售订单",
    "Delivery": "仓库交货单",
    "Invoice": "应收账款发票",
    "ReceivableVoucher": "应收账务凭证",
}

_FIELD_LABELS: dict[str, str] = {
    "total_cents": "订单总金额",
    "unit_price_cents": "商品单价",
    "credit_limit_cents": "授信总额度",
    "ar_balance_cents": "当前应收占用",
    "amount_cents": "发票金额",
    "status": "单据状态",
    "risk_tier": "客户风险等级",
    "fulfillment_note": "履约特批备注",
    "credit_hold_note": "授信风控备注",
    "account_rep": "客户负责人",
    "product_summary": "商品摘要",
    "quantity": "订购数量",
    "order": "关联订单号",
    "order_id": "关联订单号",
    "delivery_id": "交货单号",
    "invoice_id": "发票号",
    "ledger_id": "应收台账编号",
    "voucher_id": "账务凭证号",
    "ledger": "关联应收台账",
    "entry_kind": "凭证记账类型",
    "delta_cents": "应收变动金额",
    "customer": "所属客户",
    "customer_id": "客户编号",
    "name": "客户名称",
}

_VALUE_LABELS: dict[str, str] = {
    "draft": "草稿",
    "confirmed": "已确认",
    "shipped": "已发货",
    "invoiced": "已开票",
    "paid": "已收款",
    "cancelled": "已取消",
    "pending": "待发货",
    "posted": "已过账",
    "invoice_post": "开票记账增加",
    "payment_settlement": "收款核销冲减",
    "NORMAL": "正常",
    "WATCHLIST": "重点关注",
    "BLOCKED": "信用冻结",
}

_ERROR_LABELS: dict[str, str] = {
    "CANNOT_INVOICE_UNSHIPPED_ORDER": "未完成出库严禁开具应收发票",
    "CUSTOMER_CREDIT_BLOCKED": "客户处于信用冻结状态，禁止下单确认",
    "ORDER_NOT_DRAFT": "订单非草稿状态，无法重复确认",
    "ORDER_NOT_CONFIRMED": "订单尚未确认，禁止仓库提前出库",
    "PAYMENT_AMOUNT_MISMATCH": "收款金额与应收发票不一致，拒绝核销",
    "INVOICE_ALREADY_PAID": "应收发票已核销结清，禁止重复收款",
    "UNBACKED_BALANCE_MUTATION": "应收台账变动缺少配平凭证，已拒绝入账",
    "APPEND_ONLY_VIOLATION": "历史账务凭证不可篡改或删除，仅允许红冲",
    "ILLEGAL_DOCUMENT_TRANSITION": "单据状态跃迁非法，已拦截跨步变更",
    "APPROVAL_REJECTED": "财务总监已驳回本次高风险变更申请",
    "REVERT_BLOCKED_ALREADY_SHIPPED": "货物已完成物理出库，禁止直接冲正撤单，请走退货入库流程",
    "REVERT_BLOCKED_ALREADY_PAID": "发票已完成收款核销，禁止直接冲正，请走红字退款流程",
}

_REVERSIBLE_ACTIONS = frozenset({"confirm_sales_order", "issue_and_post_invoice"})


class FeishuGatewayError(RuntimeError):
    """飞书开放平台通信或签名校验错误。"""


def format_rmb(cents: int | float | None) -> str:
    if cents is None:
        return "—"
    return f"¥{float(cents) / 100.0:,.2f}"


def _format_field_value(field_name: str, value: Any) -> str:
    if value is None or value == "":
        return "（空）"
    if field_name.endswith("_cents") and isinstance(value, (int, float)):
        return format_rmb(value)
    text = str(value)
    return _VALUE_LABELS.get(text, text)


def _is_revert_run(run: dict[str, Any]) -> bool:
    if run.get("action_type") == "confirm_sales_order":
        for ch in (run.get("object_diff") or {}).get("object_changes", []):
            if ch.get("object_type") == "SalesOrder" and (ch.get("after") or {}).get("status") == "draft":
                return True
    if run.get("action_type") == "issue_and_post_invoice":
        for ch in (run.get("object_diff") or {}).get("object_changes", []):
            if ch.get("object_type") == "SalesOrder" and (ch.get("after") or {}).get("status") == "shipped":
                return True
    return False


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=body if method in ("POST", "PUT", "PATCH") else None,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def verify_feishu_signature(
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    raw_body: bytes,
    signature_header: str,
) -> bool:
    """校验飞书事件/卡片回调请求头中的 ``X-Lark-Signature``。"""
    if not encrypt_key:
        return True
    if not signature_header or not timestamp or not nonce:
        return False
    raw_bytes = timestamp.encode("utf-8") + nonce.encode("utf-8") + encrypt_key.encode("utf-8") + raw_body
    expected = hashlib.sha256(raw_bytes).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def format_run_for_executive(
    runtime: OntologyRuntime,
    run: dict[str, Any],
    *,
    erp_evidence: list[dict[str, Any]] | None = None,
    conflict_error: str | None = None,
    card_status_override: str | None = None,
) -> dict[str, Any]:
    """把运行记录与 ObjectDiff 翻译为面向管理层的零技术黑话业务卡片视图。"""
    run_id = str(run.get("run_id") or "")
    action_type = str(run.get("action_type") or "")
    params = dict(run.get("params") or {})
    status = str(card_status_override or run.get("status") or "")
    is_revert = _is_revert_run(run)

    action_label = _ACTION_LABELS.get(action_type, action_type)
    if is_revert:
        action_label = f"紧急冲正撤回：{action_label}"

    actor_role = str(run.get("actor_role") or "")
    actor_label = _ACTOR_ROLE_LABELS.get(actor_role, str(run.get("actor_id") or "业务操作员"))
    order_id = str(params.get("order") or params.get("order_id") or "")

    # 提取结构化字段变更（Before -> After）
    changes_summary: list[dict[str, str]] = []
    diff = run.get("object_diff") or {}
    for ch in diff.get("object_changes") or []:
        obj_type = str(ch.get("object_type") or "")
        obj_id = str(ch.get("object_id") or "")
        obj_label = _OBJECT_LABELS.get(obj_type, obj_type)
        kind = str(ch.get("kind") or "")
        before = ch.get("before") or {}
        after = ch.get("after") or {}

        if kind == "create":
            changes_summary.append(
                {
                    "object_label": f"{obj_label} ({obj_id})",
                    "field_label": "新增单据",
                    "before_text": "无",
                    "after_text": _format_field_value("status", after.get("status") or "已生成"),
                }
            )
            continue
        if kind == "delete":
            changes_summary.append(
                {
                    "object_label": f"{obj_label} ({obj_id})",
                    "field_label": "单据撤回",
                    "before_text": _format_field_value("status", before.get("status") or "已生成"),
                    "after_text": "已撤销清理",
                }
            )
            continue

        keys = sorted(set(before) | set(after))
        for key in keys:
            if before.get(key) == after.get(key):
                continue
            changes_summary.append(
                {
                    "object_label": f"{obj_label} ({obj_id})",
                    "field_label": _FIELD_LABELS.get(key, key),
                    "before_text": _format_field_value(key, before.get(key)),
                    "after_text": _format_field_value(key, after.get(key)),
                }
            )

    # 补充业务风险提示与客户授信上下文
    risk_headline = ""
    customer_name = "目标客户"
    if order_id:
        try:
            order_obj = runtime.get_object(
                o2c.SalesOrder,
                order_id,
                Actor(actor_id="__card__", role="system", clearance_tags={"*"}),
            )
            cust_obj = runtime.get_object(
                o2c.Customer,
                order_obj.customer,
                Actor(actor_id="__card__", role="system", clearance_tags={"*"}),
            )
            ar_cents = o2c.get_customer_ar_balance(
                runtime,
                cust_obj.customer_id,
                Actor(actor_id="__card__", role="system", clearance_tags={"*"}),
            )
            customer_name = cust_obj.name
            remaining = max(0, cust_obj.credit_limit_cents - ar_cents)
            over = max(0, ar_cents + order_obj.total_cents - cust_obj.credit_limit_cents)
            if status == "PENDING_APPROVAL" and action_type == "confirm_sales_order" and over > 0:
                risk_headline = (
                    f"订单金额 {format_rmb(order_obj.total_cents)}，"
                    f"超出客户剩余可用授信 {format_rmb(over)}"
                    f"（授信总额 {format_rmb(cust_obj.credit_limit_cents)}，"
                    f"当前可用 {format_rmb(remaining)}），"
                    "已由数字沙箱自动锁死挂起，ERP 原单据保持草稿未变动。"
                )
        except Exception:
            pass

    if not risk_headline:
        if status == "PENDING_APPROVAL":
            risk_headline = "当前高风险业务变更已触发风控挂起锁，审批放行前 ERP 真实单据零修改。"
        elif status == "CONFLICT":
            risk_headline = conflict_error or "审批等待期间底层 ERP 单据已被修改，版本校验触发自动熔断作废。"
        elif status == "REJECTED":
            code = str(run.get("error_code") or "")
            reason = _ERROR_LABELS.get(code) or str(run.get("error_message") or "违反企业风控红线")
            risk_headline = f"安全闸门已拦截：{reason}。底层 ERP 零改动，企图已记入合规审计账本。"
        elif status == "REVERTED":
            risk_headline = "已执行一键时光机冲正，ERP 真实单据与授信占用已自动恢复至操作前状态。"
        elif is_revert and status == "COMMITTED":
            risk_headline = "逆向冲正指令已落盘并同步回写 ERP，单据已秒级恢复原状。"
        else:
            risk_headline = "变更已通过规则校验并正式生效，真实 ERP 单据同步完成。"

    # 拼装数据锁版本摘要
    read_versions = dict(run.get("read_versions") or {})
    version_badges = [
        f"{_OBJECT_LABELS.get(k.partition(':')[0], k.partition(':')[0])}({k.partition(':')[2]}) v{v}"
        for k, v in sorted(read_versions.items())
    ]
    version_lock_text = " / ".join(version_badges) if version_badges else "无前置锁定对象"

    # 拼装 ERP 回写凭据文案
    erp_lines: list[str] = []
    for ev in erp_evidence or []:
        ev_action = str(ev.get("action") or "")
        if ev.get("odoo_name") and ev.get("sale_state"):
            state_cn = "已确认生效" if ev["sale_state"] == "sale" else _VALUE_LABELS.get(ev["sale_state"], ev["sale_state"])
            pick_part = f"｜出库单：{ev['picking_name']}" if ev.get("picking_name") else ""
            erp_lines.append(f"Odoo 销售单号：{ev['odoo_name']}｜真实状态：{state_cn}{pick_part}")
        elif ev.get("invoice_name"):
            erp_lines.append(f"Odoo 发票号：{ev['invoice_name']}｜状态：已过账（金额 {format_rmb(float(ev.get('amount_total', 0)) * 100)}）")
        elif ev.get("reversed_invoice"):
            method_cn = "已开具红字冲销单" if ev.get("method") == "credit_note" else "已撤销作废"
            erp_lines.append(f"Odoo 原发票：{ev['reversed_invoice']}｜冲正方式：{method_cn}")
        elif ev.get("picking_name"):
            erp_lines.append(f"Odoo 发货单：{ev['picking_name']}｜真实状态：已完成出库")
        elif ev_action == "record_customer_payment":
            erp_lines.append(f"Odoo 收款核销完成｜剩余应收：¥{float(ev.get('amount_residual', 0)):,.2f}")

    if status == "PENDING_APPROVAL":
        header_title = f"【待签批】{action_label}（{order_id or run_id}）"
        header_template = "orange"
        status_label = "待签批挂起"
    elif status == "CONFLICT":
        header_title = f"【并发锁熔断】{action_label}（{order_id or run_id}）"
        header_template = "red"
        status_label = "版本冲突作废"
    elif status == "REJECTED":
        header_title = f"【风险已拦截】{action_label}（{order_id or run_id}）"
        header_template = "red"
        status_label = "已拦截"
    elif status == "REVERTED":
        header_title = f"【已冲正撤回】{action_label}（{order_id or run_id}）"
        header_template = "grey"
        status_label = "已冲正"
    else:
        header_title = (
            f"【已签批生效】{action_label}（{order_id or run_id}）"
            if run.get("approved_by")
            else f"【已生效】{action_label}（{order_id or run_id}）"
        )
        header_template = "green"
        status_label = "已生效"

    reversible = (
        status == "COMMITTED"
        and not is_revert
        and action_type in _REVERSIBLE_ACTIONS
    )

    return {
        "run_id": run_id,
        "action_type": action_type,
        "action_label": action_label,
        "actor_label": actor_label,
        "approved_by": run.get("approved_by"),
        "approved_by_label": (
            _ACTOR_ID_LABELS.get(
                str(run.get("approved_by")),
                _ACTOR_ROLE_LABELS.get(str(run.get("approved_by")), str(run.get("approved_by"))),
            )
            if run.get("approved_by")
            else None
        ),
        "reverted_by_run_id": run.get("reverted_by_run_id"),
        "order_id": order_id,
        "customer_name": customer_name,
        "status": status,
        "status_label": status_label,
        "header_title": header_title,
        "header_template": header_template,
        "risk_headline": risk_headline,
        "changes_summary": changes_summary,
        "version_lock_text": version_lock_text,
        "erp_lines": erp_lines,
        "executed_at": run.get("executed_at") or "",
        "reversible": reversible,
        "is_revert": is_revert,
    }


def build_feishu_card(
    card_view: dict[str, Any], *, callback_token: str = ""
) -> dict[str, Any]:
    """构造飞书交互式消息卡片 JSON（含待批双按钮、生效后冲正按钮与防连点终态）。"""
    diff_lines = [
        f"• **{item['object_label']} · {item['field_label']}**：{item['before_text']} → **{item['after_text']}**"
        for item in card_view.get("changes_summary") or []
    ]
    diff_md = "\n".join(diff_lines) if diff_lines else "• 无字段变更（拦截前已终止）"

    meta_lines = [
        f"**发起人**：{card_view['actor_label']}",
        f"**关联业务**：{card_view.get('customer_name') or '业务单据'} ｜ 单号 **{card_view.get('order_id') or card_view['run_id']}**",
        f"**风控评估**：{card_view['risk_headline']}",
        "",
        "**变更影响面预演（Before → After）**",
        diff_md,
    ]
    if card_view.get("approved_by"):
        meta_lines.append(
            f"\n**签批/处理人**：{card_view.get('approved_by_label') or card_view['approved_by']}"
        )
    if card_view.get("erp_lines"):
        meta_lines.append("\n**ERP 真实回写凭据**\n" + "\n".join(f"• {line}" for line in card_view["erp_lines"]))

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(meta_lines),
            },
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": (
                        f"流水号：{card_view['run_id']} ｜ 并发安全锁：{card_view['version_lock_text']}"
                    ),
                }
            ],
        },
    ]

    status = card_view.get("status")
    run_id = card_view["run_id"]
    if status == "PENDING_APPROVAL":
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准并写入 ERP"},
                        "type": "primary",
                        "value": {
                            "op": "approve",
                            "run_id": run_id,
                            "token": callback_token,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "驳回申请"},
                        "type": "danger",
                        "value": {
                            "op": "reject",
                            "run_id": run_id,
                            "token": callback_token,
                        },
                    },
                ],
            }
        )
    elif card_view.get("reversible"):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "紧急冲正撤回（1秒恢复 ERP）"},
                        "type": "danger",
                        "value": {
                            "op": "revert",
                            "run_id": run_id,
                            "token": callback_token,
                        },
                    }
                ],
            }
        )

    return {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": {
            "template": card_view.get("header_template", "blue"),
            "title": {
                "tag": "plain_text",
                "content": card_view["header_title"],
            },
        },
        "elements": elements,
    }


class FeishuOpenAPIClient:
    """轻量飞书开放平台 HTTP 客户端（纯标准库实现，支持测试注入 transport）。"""

    BASE_URL = "https://open.feishu.cn"

    def __init__(
        self,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
        transport: TransportFn | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.app_id = app_id if app_id is not None else os.environ.get("FEISHU_APP_ID", "").strip()
        self.app_secret = app_secret if app_secret is not None else os.environ.get("FEISHU_APP_SECRET", "").strip()
        self.receive_id = receive_id if receive_id is not None else os.environ.get("FEISHU_RECEIVE_ID", "").strip()
        self.receive_id_type = (
            receive_id_type
            if receive_id_type is not None
            else os.environ.get("FEISHU_RECEIVE_ID_TYPE", "open_id").strip() or "open_id"
        )
        self._transport: TransportFn = transport or _default_transport
        self._timeout = timeout
        self._cached_token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.receive_id)

    def _call_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        bearer_token: str = "",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, resp_bytes = self._transport(method, url, headers, raw, self._timeout)
        try:
            data = json.loads(resp_bytes.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise FeishuGatewayError(f"invalid JSON from Feishu HTTP {status}: {exc}") from exc
        if status >= 400 or int(data.get("code", 0)) != 0:
            raise FeishuGatewayError(
                f"Feishu API {method} {path} failed (http={status}, code={data.get('code')}): {data.get('msg')}"
            )
        return data

    def get_tenant_access_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._cached_token and now < self._token_expires_at:
                return self._cached_token
            data = self._call_json(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": self.app_id, "app_secret": self.app_secret},
            )
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise FeishuGatewayError("Feishu auth returned empty tenant_access_token")
            expire = int(data.get("expire") or 7200)
            self._cached_token = token
            self._token_expires_at = now + max(60, expire - 120)
            return token

    def _send_message(
        self,
        msg_type: str,
        content_obj: dict[str, Any],
        *,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> str:
        target = receive_id or self.receive_id
        if not target:
            raise FeishuGatewayError("FEISHU_RECEIVE_ID is required to send Feishu message")
        id_type = receive_id_type or (
            "chat_id" if str(target).startswith("oc_") else self.receive_id_type
        )
        token = self.get_tenant_access_token()
        data = self._call_json(
            "POST",
            f"/open-apis/im/v1/messages?receive_id_type={id_type}",
            {
                "receive_id": target,
                "msg_type": msg_type,
                "content": json.dumps(content_obj, ensure_ascii=False),
            },
            bearer_token=token,
        )
        msg_id = str(((data.get("data") or {}).get("message_id")) or "")
        if not msg_id:
            raise FeishuGatewayError(f"Feishu send_{msg_type} succeeded without message_id")
        return msg_id

    def send_card(
        self,
        card: dict[str, Any],
        *,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> str:
        return self._send_message(
            "interactive",
            card,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )

    def send_text(
        self,
        text: str,
        *,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
    ) -> str:
        """发送纯文本通知到飞书会话（不复用卡点击一次性 token 作为回复锚点）。"""
        return self._send_message(
            "text",
            {"text": text},
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        token = self.get_tenant_access_token()
        self._call_json(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            {"content": json.dumps(card, ensure_ascii=False)},
            bearer_token=token,
        )


@dataclass
class CardRecord:
    run_id: str
    card_view: dict[str, Any]
    card: dict[str, Any]
    message_id: str | None = None
    push_error: str | None = None
    updated_at: float = field(default_factory=time.time)


class FeishuApprovalGateway:
    """连接 OntologyRuntime、真实 ERP 适配器与飞书审批卡片的核心网关。"""

    def __init__(
        self,
        runtime: OntologyRuntime,
        *,
        adapter: Any | None = None,
        feishu_client: FeishuOpenAPIClient | None = None,
        operator_actors: dict[str, Actor] | None = None,
        default_approver: Actor = o2c.FINANCE_DIRECTOR,
        strict_operator_map: bool = False,
        encrypt_key: str | None = None,
        verification_token: str | None = None,
        callback_token: str = "",
        push_all_cards: bool = False,
    ) -> None:
        self.runtime = runtime
        self.adapter = adapter
        self.feishu_client = feishu_client or FeishuOpenAPIClient()
        self.operator_actors: dict[str, Actor] = dict(operator_actors or {})
        self.default_approver = default_approver
        self.strict_operator_map = strict_operator_map
        self.encrypt_key = (
            encrypt_key
            if encrypt_key is not None
            else os.environ.get("FEISHU_ENCRYPT_KEY", "").strip()
        )
        self.verification_token = (
            verification_token
            if verification_token is not None
            else os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip()
        )
        self.callback_token = callback_token
        self.push_all_cards = push_all_cards
        self._lock = threading.RLock()
        self._cards: dict[str, CardRecord] = {}
        self._card_order: list[str] = []
        self._evidence_by_run: dict[str, list[dict[str, Any]]] = {}
        self._suppress_event_run_ids: set[str] = set()

        self.runtime.add_run_event_hook(self._on_run_event)

    # -- lifecycle & card sync --------------------------------------------
    def _on_run_event(self, actor: Actor, run: dict[str, Any]) -> None:
        del actor
        run_id = str(run.get("run_id") or "")
        if not run_id:
            return
        with self._lock:
            if run_id in self._suppress_event_run_ids:
                return
            if run["status"] == "PENDING_APPROVAL":
                self._upsert_card(run, push_if_new=True)
            elif run["status"] == "REJECTED" and run.get("error_code") not in (None, "APPROVAL_REJECTED"):
                # 违规红线拦截告警卡片（用于演播室/飞书即时告警）
                self._upsert_card(run, push_if_new=self.push_all_cards)
            elif run["status"] == "COMMITTED":
                if _is_revert_run(run):
                    return
                if run_id in self._cards or run.get("action_type") in _REVERSIBLE_ACTIONS:
                    self._upsert_card(run, push_if_new=False)

    def register_committed_card(
        self, run_id: str, erp_evidence: list[dict[str, Any]] | None = None
    ) -> CardRecord | None:
        """为直通生效的动作补充 ERP 回写凭据并刷新/推送本地与飞书卡片。"""
        run = self.runtime.get_action_run(run_id)
        if run is None:
            return None
        with self._lock:
            return self._upsert_card(
                run,
                push_if_new=self.push_all_cards,
                erp_evidence=erp_evidence,
            )

    def restore_card_record(
        self,
        run_id: str,
        *,
        message_id: str | None = None,
        erp_evidence: list[dict[str, Any]] | None = None,
        conflict_error: str | None = None,
        card_status_override: str | None = None,
    ) -> CardRecord | None:
        """从持久化元数据恢复本地卡片索引与飞书 ``message_id``（不触发外发）。"""
        run = self.runtime.get_action_run(run_id)
        if run is None:
            return None
        with self._lock:
            if erp_evidence is not None:
                self._evidence_by_run[run_id] = list(erp_evidence)
            stored_evidence = self._evidence_by_run.get(run_id)
            view = format_run_for_executive(
                self.runtime,
                run,
                erp_evidence=stored_evidence,
                conflict_error=conflict_error,
                card_status_override=card_status_override,
            )
            card = build_feishu_card(view, callback_token=self.callback_token)
            rec = CardRecord(
                run_id=run_id,
                card_view=view,
                card=card,
                message_id=message_id,
            )
            self._cards[run_id] = rec
            if run_id not in self._card_order:
                self._card_order.append(run_id)
            return rec

    def export_card_metadata(self) -> dict[str, dict[str, Any]]:
        """导出可 JSON 序列化的卡片元数据（供跨进程持久化恢复）。"""
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for run_id in self._card_order:
                rec = self._cards[run_id]
                override = "CONFLICT" if rec.card_view.get("status") == "CONFLICT" else None
                out[run_id] = {
                    "message_id": rec.message_id,
                    "erp_evidence": list(self._evidence_by_run.get(run_id, [])),
                    "conflict_error": (
                        rec.card_view.get("risk_headline") if override == "CONFLICT" else None
                    ),
                    "card_status_override": override,
                }
            return out

    def _upsert_card(
        self,
        run: dict[str, Any],
        *,
        push_if_new: bool,
        erp_evidence: list[dict[str, Any]] | None = None,
        conflict_error: str | None = None,
        card_status_override: str | None = None,
    ) -> CardRecord:
        run_id = str(run["run_id"])
        if erp_evidence is not None:
            self._evidence_by_run[run_id] = list(erp_evidence)
        stored_evidence = self._evidence_by_run.get(run_id)
        view = format_run_for_executive(
            self.runtime,
            run,
            erp_evidence=stored_evidence,
            conflict_error=conflict_error,
            card_status_override=card_status_override,
        )
        card = build_feishu_card(view, callback_token=self.callback_token)
        existing = self._cards.get(run_id)
        message_id = existing.message_id if existing else None
        push_error: str | None = None

        if self.feishu_client.is_configured:
            try:
                if message_id:
                    self.feishu_client.patch_card(message_id, card)
                elif push_if_new:
                    message_id = self.feishu_client.send_card(card)
            except Exception as exc:
                push_error = str(exc)

        record = CardRecord(
            run_id=run_id,
            card_view=view,
            card=card,
            message_id=message_id,
            push_error=push_error,
        )
        self._cards[run_id] = record
        if run_id not in self._card_order:
            self._card_order.append(run_id)
        return record

    def list_cards(self) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for run_id in reversed(self._card_order):
                rec = self._cards[run_id]
                out.append(
                    {
                        "run_id": rec.run_id,
                        "message_id": rec.message_id,
                        "push_error": rec.push_error,
                        "view": rec.card_view,
                        "card": rec.card,
                    }
                )
            return out

    def get_card_record(self, run_id: str) -> CardRecord | None:
        with self._lock:
            return self._cards.get(run_id)

    # -- callback processing ----------------------------------------------
    def _resolve_actor(self, operator_open_id: str) -> Actor:
        if operator_open_id and operator_open_id in self.operator_actors:
            return self.operator_actors[operator_open_id]
        if self.strict_operator_map and operator_open_id not in ("", "demo-finance-director"):
            raise PermissionDeniedError(
                f"unmapped Feishu operator {operator_open_id!r} cannot approve or revert"
            )
        return self.default_approver

    def _check_revert_allowed(self, run: dict[str, Any]) -> None:
        action_type = str(run.get("action_type") or "")
        p = run.get("params") or {}
        order_id = str(p.get("order") or p.get("order_id") or "")
        if not order_id:
            return
        sys_actor = Actor(actor_id="__guard__", role="system", clearance_tags={"*"})

        if action_type == "confirm_sales_order":
            order_obj = self.runtime.get_object(o2c.SalesOrder, order_id, sys_actor)
            if order_obj.status in ("shipped", "invoiced", "paid"):
                raise InvariantError(
                    _ERROR_LABELS["REVERT_BLOCKED_ALREADY_SHIPPED"],
                    code="REVERT_BLOCKED_ALREADY_SHIPPED",
                )
            if self.adapter is not None:
                picking = self.adapter.delivery_row(order_id)
                if picking is not None and picking.get("state") == "done":
                    raise InvariantError(
                        _ERROR_LABELS["REVERT_BLOCKED_ALREADY_SHIPPED"],
                        code="REVERT_BLOCKED_ALREADY_SHIPPED",
                    )

        elif action_type == "issue_and_post_invoice":
            order_obj = self.runtime.get_object(o2c.SalesOrder, order_id, sys_actor)
            if order_obj.status == "paid":
                raise InvariantError(
                    _ERROR_LABELS["REVERT_BLOCKED_ALREADY_PAID"],
                    code="REVERT_BLOCKED_ALREADY_PAID",
                )
            if self.adapter is not None:
                inv = self.adapter.invoice_row(order_id)
                if (
                    inv is not None
                    and inv.get("payment_state") in ("paid", "in_payment")
                    and float(inv.get("amount_residual", 0.0)) == 0.0
                ):
                    raise InvariantError(
                        _ERROR_LABELS["REVERT_BLOCKED_ALREADY_PAID"],
                        code="REVERT_BLOCKED_ALREADY_PAID",
                    )

    @staticmethod
    def _extract_callback_fields(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if payload.get("schema") == "2.0" or "event" in payload:
            event = payload.get("event") or {}
            action = event.get("action") or {}
            operator = event.get("operator") or {}
            val = action.get("value") or {}
            if isinstance(val, str):
                val = json.loads(val)
            return dict(val), str(operator.get("open_id") or operator.get("user_id") or "")

        action = payload.get("action") or {}
        val = action.get("value") or {}
        if isinstance(val, str):
            val = json.loads(val)
        open_id = str(payload.get("open_id") or payload.get("user_id") or "")
        return dict(val), open_id

    def handle_feishu_callback(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> dict[str, Any]:
        """处理飞书交互卡片按钮回调（同时兼容 v1 / v2 / challenge 握手）。"""
        if payload.get("type") == "url_verification":
            if self.verification_token and payload.get("token") != self.verification_token:
                raise PermissionDeniedError("invalid Feishu verification token")
            return {"challenge": payload.get("challenge", "")}

        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        if self.encrypt_key and raw_body is not None:
            ok = verify_feishu_signature(
                hdrs.get("x-lark-request-timestamp", ""),
                hdrs.get("x-lark-request-nonce", ""),
                self.encrypt_key,
                raw_body,
                hdrs.get("x-lark-signature", ""),
            )
            if not ok:
                raise PermissionDeniedError("invalid Feishu callback signature")

        value, open_id = self._extract_callback_fields(payload)
        if self.callback_token and value.get("token") != self.callback_token:
            raise PermissionDeniedError("invalid card callback token")

        op = str(value.get("op") or "").strip()
        run_id = str(value.get("run_id") or "").strip()
        if not run_id or op not in ("approve", "reject", "revert"):
            raise GovernanceViolationError(f"unsupported Feishu card action {value!r}")

        with self._lock:
            actor = self._resolve_actor(open_id)
            if op == "approve":
                self._suppress_event_run_ids.add(run_id)
                try:
                    committed = self.runtime.approve_action(run_id, actor)
                except ConcurrencyConflictError as exc:
                    run = self.runtime.get_action_run(run_id)
                    if run is None:
                        return {"toast": {"type": "error", "content": str(exc)}}
                    msg = f"并发锁熔断：审批期间底层 ERP 单据已被修改，原提案自动作废（{exc}）"
                    rec = self._upsert_card(
                        run,
                        push_if_new=False,
                        conflict_error=msg,
                        card_status_override="CONFLICT",
                    )
                    return {
                        "toast": {"type": "error", "content": msg},
                        "card": rec.card,
                        "status": "CONFLICT",
                        "error_code": "CONCURRENCY_CONFLICT",
                    }
                except (PermissionDeniedError, GovernanceViolationError) as exc:
                    return {"toast": {"type": "error", "content": str(exc)}}
                finally:
                    self._suppress_event_run_ids.discard(run_id)

                evidence = self.adapter.process_new_runs() if self.adapter is not None else []
                rec = self._upsert_card(committed, push_if_new=False, erp_evidence=evidence)
                return {
                    "toast": {"type": "success", "content": "已签批并同步写入真实 ERP"},
                    "card": rec.card,
                    "status": committed["status"],
                    "erp_evidence": evidence,
                }

            if op == "reject":
                self._suppress_event_run_ids.add(run_id)
                try:
                    rejected = self.runtime.reject_action(
                        run_id, actor, reason="财务总监飞书卡片驳回"
                    )
                except (PermissionDeniedError, GovernanceViolationError) as exc:
                    return {"toast": {"type": "error", "content": str(exc)}}
                finally:
                    self._suppress_event_run_ids.discard(run_id)

                rec = self._upsert_card(rejected, push_if_new=False)
                return {
                    "toast": {"type": "info", "content": "已驳回该高风险操作，ERP 保持原状"},
                    "card": rec.card,
                    "status": rejected["status"],
                }

            # op == "revert"
            run = self.runtime.get_action_run(run_id)
            if run is None:
                return {"toast": {"type": "error", "content": f"流水号 {run_id} 不存在"}}

            revert_run_id = f"{run_id}-REVERT"
            self._suppress_event_run_ids.update({run_id, revert_run_id})
            try:
                self._check_revert_allowed(run)
                revert_run = self.runtime.revert_action(run_id, revert_run_id, actor)
            except (InvariantError, PermissionDeniedError, GovernanceViolationError) as exc:
                err_code = getattr(exc, "code", None)
                return {
                    "toast": {"type": "error", "content": str(exc)},
                    "error_code": err_code,
                }
            finally:
                self._suppress_event_run_ids.discard(run_id)
                self._suppress_event_run_ids.discard(revert_run_id)

            evidence = self.adapter.process_new_runs() if self.adapter is not None else []
            original_after = self.runtime.get_action_run(run_id) or run
            rec = self._upsert_card(
                original_after,
                push_if_new=False,
                erp_evidence=evidence,
            )
            return {
                "toast": {"type": "success", "content": "一键时光机冲正成功，ERP 已 1 秒恢复原状"},
                "card": rec.card,
                "status": original_after["status"],
                "revert_run_id": revert_run["run_id"],
                "erp_evidence": evidence,
            }


def start_feishu_ws_listener(
    gateway: FeishuApprovalGateway,
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> Any:
    """可选：若本地安装了 ``lark-oapi``，启动免公网 IP 的飞书 WebSocket 长连接监听。"""
    import lark_oapi as lark  # type: ignore[import-not-found]
    from lark_oapi.event.callback.model.p2_card_action_trigger import (  # type: ignore[import-not-found]
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    aid = app_id or gateway.feishu_client.app_id
    asecret = app_secret or gateway.feishu_client.app_secret
    if not aid or not asecret:
        raise FeishuGatewayError("FEISHU_APP_ID / FEISHU_APP_SECRET required for WebSocket mode")

    def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        raw_payload = json.loads(lark.JSON.marshal(data))
        result = gateway.handle_feishu_callback(raw_payload)
        resp_dict: dict[str, Any] = {}
        if "toast" in result:
            resp_dict["toast"] = result["toast"]
        if "card" in result:
            resp_dict["card"] = {"type": "raw", "data": result["card"]}
        return P2CardActionTriggerResponse(resp_dict)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(_on_card_action)
        .build()
    )
    cli = lark.ws.Client(aid, asecret, event_handler=event_handler)
    thread = threading.Thread(target=cli.start, daemon=True, name="feishu-ws-client")
    thread.start()
    return cli
