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
    "build_feishu_gray_zone_ruling_card",
    "parse_feishu_ruling_callback",
    "verify_feishu_signature",
    "start_feishu_ws_listener",
    "submit_with_feishu_approval",
]

TransportFn = Callable[[str, str, dict[str, str], bytes, float], tuple[int, bytes]]

_ACTION_LABELS: dict[str, str] = {
    "create_sales_order": "新建草稿销售订单",
    "confirm_sales_order": "确认销售订单并锁库",
    "ship_order_delivery": "仓库确认发货出库",
    "issue_and_post_invoice": "开具并过账应收发票",
    "record_customer_payment": "登记客户收款核销",
    "issue_credit_note_refund": "开具红字退款贷项单",
    "annotate_o2c_governance": "登记业务风控特批备注",
    "open_work_card": "发起财务总监签批申请",
    "claim_work_card": "认领协同签批单",
    "complete_work_card": "完成主管签批归档",
    "reject_work_card": "驳回高风险签批申请",
    "mark_work_card_sla_breached": "标记签批单超时",
    "cancel_work_card": "作废失效签批单",
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
    "WorkCard": "风险协同签批单",
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
    "APPROVAL_REQUIRED_CREDIT_LIMIT_EXCEEDED": "订单金额超出剩余可用授信，须发起财务总监签批",
    "APPROVAL_REQUIRED_CREDIT_NOTE_REFUND": "红字退款须由财务总监签批放行",
    "WORK_CARD_ASSIGNEE_MISMATCH": "当前操作角色无权签批此高风险申请",
    "WORK_CARD_RESOLVER_FORBIDDEN": "当前操作角色无权批结签批单",
    "SOD_SELF_APPROVAL_FORBIDDEN": "职责分离限制：禁止申请人自批自结",
    "REVERT_BLOCKED_ALREADY_SHIPPED": "货物已完成物理出库，禁止直接冲正撤单，请走退货入库流程",
    "REVERT_BLOCKED_ALREADY_PAID": "发票已完成收款核销，禁止直接冲正，请走红字退款流程",
}

_REVERSIBLE_ACTIONS = frozenset({"confirm_sales_order", "issue_and_post_invoice"})


def _read_target_version(runtime: OntologyRuntime, obj_type: str, obj_id: str) -> int | None:
    if not obj_type or not obj_id:
        return None
    row = runtime.connection.execute(
        "SELECT version FROM ontology_objects WHERE object_type = ? AND object_id = ?",
        (obj_type, obj_id),
    ).fetchone()
    return int(row["version"]) if row is not None else None


def _parse_occ_external_ref(external_ref: str) -> tuple[str, str, int] | None:
    text = str(external_ref or "").strip()
    if not text.startswith("occ:"):
        return None
    parts = text.split(":")
    if len(parts) != 4 or not parts[3].startswith("v"):
        return None
    try:
        return parts[1], parts[2], int(parts[3][1:])
    except ValueError:
        return None


def submit_with_feishu_approval(
    runtime: OntologyRuntime,
    run_id: str,
    action_name: str,
    params: dict[str, Any],
    actor: Actor,
    *,
    assignee_role: str = "finance_director",
    target_object_type: str = "SalesOrder",
) -> dict[str, Any]:
    """提交 O2C 动作；若触发 ``APPROVAL_REQUIRED_*`` 预条件，同步开立 M1 签批协同卡。"""
    try:
        return runtime.submit_action(run_id, action_name, params, actor)
    except InvariantError as exc:
        if not str(exc.code or "").startswith("APPROVAL_REQUIRED_"):
            raise
        order_id = str(params.get("order") or params.get("order_id") or "")
        ver = _read_target_version(runtime, target_object_type, order_id)
        ext_ref = f"occ:{target_object_type}:{order_id}:v{ver}" if ver is not None else ""
        target_action = runtime.spec.get_action(action_name)
        params_json = target_action.params.model_validate(params or {}).model_dump_json()
        open_run = runtime.submit_action(
            f"{run_id}-OPEN",
            "open_work_card",
            {
                "card_id": run_id,
                "card_kind": "APPROVAL",
                "target_object_type": target_object_type,
                "target_object_id": order_id,
                "requested_action": action_name,
                "requested_params_json": params_json,
                "assignee_role": assignee_role,
                "external_ref": ext_ref,
            },
            actor,
        )
        return {
            "run_id": run_id,
            "card_id": run_id,
            "action_type": action_name,
            "status": "OPEN",
            "work_card_status": "OPEN",
            "blocked_run_id": run_id,
            "open_run_id": open_run["run_id"],
            "error_code": exc.code,
            "error_message": str(exc),
        }


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
    """把运行记录或 M1 签批单翻译为面向管理层的零技术黑话业务卡片视图。"""
    run_id = str(run.get("card_id") or run.get("run_id") or "")
    raw_action = str(run.get("action_type") or "")
    params = dict(run.get("params") or {})
    status = str(card_status_override or run.get("status") or "")
    diff = dict(run.get("object_diff") or {})
    read_versions = dict(run.get("read_versions") or {})
    approved_by = run.get("approved_by")
    actor_role = str(run.get("actor_role") or "")
    actor_id = str(run.get("actor_id") or "")

    # 若传入的是 open_work_card / reject_work_card / complete_work_card 或 WorkCard 对象，展开目标业务动作与 live 推演预览
    work_card: o2c.WorkCard | None = None
    card_id_hint = str(run.get("card_id") or params.get("card_id") or "")
    if card_id_hint and (raw_action == "open_work_card" or run.get("work_card_status")):
        try:
            sys_actor = Actor(actor_id="__card__", role="system", clearance_tags={"*"})
            work_card = runtime.get_object(o2c.WorkCard, card_id_hint, sys_actor)
        except Exception:
            work_card = None

    if work_card is not None:
        run_id = work_card.card_id
        action_type = work_card.requested_action
        try:
            params = json.loads(work_card.requested_params_json or "{}")
        except Exception:
            params = {}
        if card_status_override is None:
            if work_card.status in ("OPEN", "IN_PROGRESS"):
                status = "OPEN"
            elif work_card.status == "REJECTED":
                status = "REJECTED"
            elif work_card.status == "CANCELLED":
                status = "CONFLICT" if conflict_error else "REJECTED"
            elif work_card.status == "COMPLETED":
                status = "COMMITTED"
        approved_by = work_card.resolved_by_actor_id or approved_by
        actor_id = work_card.requester_actor_id or actor_id
        if status == "OPEN":
            try:
                sim_res = runtime.simulate_action(
                    work_card.requested_action,
                    params,
                    o2c.FINANCE_DIRECTOR,
                )
                if sim_res.valid:
                    diff = sim_res.diff.to_dict()
                    read_versions = dict(sim_res.read_versions)
            except Exception:
                pass
    else:
        action_type = raw_action

    is_revert = _is_revert_run({"action_type": action_type, "object_diff": diff})

    action_label = _ACTION_LABELS.get(action_type, action_type)
    if is_revert:
        action_label = f"紧急冲正撤回：{action_label}"

    actor_label = (
        _ACTOR_ID_LABELS.get(actor_id)
        or _ACTOR_ROLE_LABELS.get(actor_role)
        or actor_id
        or "业务操作员"
    )
    order_id = str(params.get("order") or params.get("order_id") or "")

    # 提取结构化字段变更（Before -> After）
    changes_summary: list[dict[str, str]] = []
    for ch in diff.get("object_changes") or []:
        obj_type = str(ch.get("object_type") or "")
        if obj_type == "WorkCard" and work_card is not None:
            continue
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
            if status in ("OPEN", "IN_PROGRESS") and action_type == "confirm_sales_order" and over > 0:
                risk_headline = (
                    f"订单金额 {format_rmb(order_obj.total_cents)}，"
                    f"超出客户剩余可用授信 {format_rmb(over)}"
                    f"（授信总额 {format_rmb(cust_obj.credit_limit_cents)}，"
                    f"当前可用 {format_rmb(remaining)}），"
                    "已由数字沙箱安全拦截并开立财务签批单，ERP 原单据保持草稿未变动。"
                )
        except Exception:
            pass

    if not risk_headline:
        if status in ("OPEN", "IN_PROGRESS"):
            risk_headline = "当前高风险业务变更已由签批工单锁定等待复核，签批放行前 ERP 真实单据零修改。"
        elif status == "CONFLICT":
            risk_headline = conflict_error or "审批等待期间底层 ERP 单据已被修改，版本校验触发自动熔断作废。"
        elif status == "REJECTED":
            code = str(run.get("error_code") or "")
            reason = _ERROR_LABELS.get(code) or str(run.get("error_message") or "财务总监已驳回本次高风险变更")
            risk_headline = f"安全闸门已拦截：{reason}。底层 ERP 零改动，企图已记入合规审计账本。"
        elif status == "REVERTED":
            risk_headline = "已执行一键时光机冲正，ERP 真实单据与授信占用已自动恢复至操作前状态。"
        elif is_revert and status == "COMMITTED":
            risk_headline = "逆向冲正指令已落盘并同步回写 ERP，单据已秒级恢复原状。"
        else:
            risk_headline = "变更已通过规则校验并正式生效，真实 ERP 单据同步完成。"

    # 拼装数据锁版本摘要
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

    if status in ("OPEN", "IN_PROGRESS"):
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
            if approved_by
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
        "approved_by": approved_by,
        "approved_by_label": (
            _ACTOR_ID_LABELS.get(
                str(approved_by),
                _ACTOR_ROLE_LABELS.get(str(approved_by), str(approved_by)),
            )
            if approved_by
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
    if status in ("OPEN", "IN_PROGRESS"):
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


def build_feishu_gray_zone_ruling_card(
    card_view: dict[str, Any],
    *,
    callback_token: str = "",
) -> dict[str, Any]:
    """构造 AI-FDE 0.7 -> 1.0 未决灰区交互式裁决卡（附反例样本与可选裁决分支）。"""
    status = str(card_view.get("status") or "PENDING_RULING")
    card_id = str(card_view.get("card_id") or "")
    ambiguity_id = str(card_view.get("ambiguity_id") or "")
    title = str(card_view.get("title") or f"语义灰区待裁决（{ambiguity_id}）")
    model_id = str(card_view.get("model_id") or "")
    target_ot = str(card_view.get("target_object_type") or "")
    target_action = str(card_view.get("target_action") or "")
    summary = str(card_view.get("summary") or "")
    sop_excerpt = str(card_view.get("sop_excerpt") or "")
    samples = list(card_view.get("samples") or [])
    options = list(card_view.get("options") or [])
    recommended = str(card_view.get("recommended_choice") or "")

    md_lines = [
        f"**模型 / 目标对象**：`{model_id}` ｜ `{target_ot}` · 动作 `{target_action}`",
        f"**歧义摘要**：{summary}",
    ]
    if sop_excerpt:
        md_lines.append(f"**SOP / 文档条款摘录**：> {sop_excerpt}")
    if samples:
        md_lines.append("\n**真实账本反例样本（Observed Counterexamples）**")
        for idx, smp in enumerate(samples[:5], start=1):
            pretty = " · ".join(f"{k}={v}" for k, v in smp.items())
            md_lines.append(f"• 样本 {idx}：{pretty}")
    if recommended:
        md_lines.append(f"\n**保守推荐策略**：`{recommended}`")

    if status == "CRYSTALLIZED":
        header_template = "green"
        decided_choice = str(card_view.get("decided_choice") or "")
        decided_by = str(card_view.get("decided_by") or "steward")
        target_ver = str(card_view.get("crystallized_version") or "")
        header_title = f"【已裁决结晶 v{target_ver}】{title}"
        md_lines.append(
            f"\n**裁决结果**：`{decided_choice}`（裁决人：{decided_by} ｜ 已通过 5-Gate 证伪并写入 GoldenEvalCase）"
        )
    else:
        header_template = "orange"
        header_title = f"【0.7→1.0 灰区待裁决】{title}"

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(md_lines)}},
        {"tag": "hr"},
    ]

    if status == "PENDING_RULING" and options:
        btn_types = {
            "HARD_REJECT": "danger",
            "REQUIRE_APPROVAL": "primary",
            "ALLOW_AUTONOMOUS": "default",
        }
        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": str(opt.get("label") or opt["choice_id"])},
                "type": btn_types.get(str(opt["choice_id"]), "default"),
                "value": {
                    "kind": "gray_zone_ruling",
                    "card_id": card_id,
                    "ambiguity_id": ambiguity_id,
                    "choice_id": str(opt["choice_id"]),
                    "token": callback_token,
                },
            }
            for opt in options
        ]
        elements.append({"tag": "action", "actions": buttons})
    else:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"裁决单 {card_id} 已锁定终态（{status}）。",
                    }
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": header_template,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


def parse_feishu_ruling_callback(
    payload: dict[str, Any],
    *,
    callback_token: str = "",
) -> dict[str, str]:
    """解析并校验飞书 Steward 灰区裁决回调，返回标准裁决字段。"""
    val, open_id = FeishuApprovalGateway._extract_callback_fields(payload)
    if callback_token:
        supplied = str(val.get("token") or "")
        if not hmac.compare_digest(supplied, callback_token):
            raise PermissionDeniedError("invalid gray-zone ruling callback token")
    if str(val.get("kind") or "") != "gray_zone_ruling":
        raise FeishuGatewayError(f"unsupported callback kind {val.get('kind')!r}")
    card_id = str(val.get("card_id") or "")
    ambiguity_id = str(val.get("ambiguity_id") or "")
    choice_id = str(val.get("choice_id") or "")
    if not card_id or not ambiguity_id or not choice_id:
        raise FeishuGatewayError("gray_zone_ruling callback missing card_id/ambiguity_id/choice_id")
    return {
        "card_id": card_id,
        "ambiguity_id": ambiguity_id,
        "choice_id": choice_id,
        "operator_open_id": open_id,
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
    settled_run_id: str | None = None
    baseline_versions: dict[str, int] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


class FeishuApprovalGateway:
    """连接 OntologyRuntime、真实 ERP 适配器与飞书 M1 协同签批卡的核心网关。"""

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
        self._settled_run_by_card: dict[str, str] = {}
        self._baseline_versions: dict[str, dict[str, int]] = {}
        self._suppress_event_run_ids: set[str] = set()

        self.runtime.add_run_event_hook(self._on_run_event)

    def submit_or_escalate(
        self,
        run_id: str,
        action_name: str,
        params: dict[str, Any],
        actor: Actor,
        *,
        assignee_role: str = "finance_director",
        target_object_type: str = "SalesOrder",
    ) -> dict[str, Any]:
        """便捷入口：提交业务动作并在 ``APPROVAL_REQUIRED_*`` 时自动开立 M1 WorkCard。"""
        return submit_with_feishu_approval(
            self.runtime,
            run_id,
            action_name,
            params,
            actor,
            assignee_role=assignee_role,
            target_object_type=target_object_type,
        )

    def _capture_card_baseline(self, card_id: str) -> dict[str, int]:
        if card_id in self._baseline_versions and self._baseline_versions[card_id]:
            return dict(self._baseline_versions[card_id])
        baseline: dict[str, int] = {}
        try:
            sys_actor = Actor(actor_id="__card__", role="system", clearance_tags={"*"})
            wc = self.runtime.get_object(o2c.WorkCard, card_id, sys_actor)
            parsed = _parse_occ_external_ref(wc.external_ref)
            if parsed is not None:
                baseline[f"{parsed[0]}:{parsed[1]}"] = parsed[2]
            elif wc.target_object_type and wc.target_object_id:
                ver = _read_target_version(
                    self.runtime, wc.target_object_type, wc.target_object_id
                )
                if ver is not None:
                    baseline[f"{wc.target_object_type}:{wc.target_object_id}"] = ver
        except Exception:
            pass
        if baseline:
            self._baseline_versions[card_id] = dict(baseline)
        return baseline

    def _resolve_settled_run_id(self, key_id: str) -> str:
        if key_id in self._settled_run_by_card:
            return self._settled_run_by_card[key_id]
        rec = self._cards.get(key_id)
        if rec and rec.settled_run_id:
            self._settled_run_by_card[key_id] = rec.settled_run_id
            return rec.settled_run_id
        for r in self.runtime.list_action_runs("COMMITTED"):
            if r.get("action_type") == "complete_work_card":
                p = r.get("params") or {}
                if p.get("card_id") == key_id and p.get("settled_run_id"):
                    sid = str(p["settled_run_id"])
                    self._settled_run_by_card[key_id] = sid
                    return sid
        if self.runtime.get_action_run(f"{key_id}-ACT") is not None:
            return f"{key_id}-ACT"
        return key_id

    # -- lifecycle & card sync --------------------------------------------
    def _on_run_event(self, actor: Actor, run: dict[str, Any]) -> None:
        del actor
        run_id = str(run.get("run_id") or "")
        if not run_id:
            return
        with self._lock:
            if run_id in self._suppress_event_run_ids:
                return
            action_type = str(run.get("action_type") or "")
            if action_type == "open_work_card" and run["status"] == "COMMITTED":
                card_id = str((run.get("params") or {}).get("card_id") or "")
                if not card_id:
                    for ch in (run.get("object_diff") or {}).get("object_changes", []):
                        if ch.get("object_type") == "WorkCard":
                            card_id = str(ch.get("object_id") or "")
                            break
                if card_id:
                    self._capture_card_baseline(card_id)
                    wc_run = dict(run, run_id=card_id, card_id=card_id)
                    self._upsert_card(wc_run, push_if_new=True)
            elif run["status"] == "REJECTED":
                err_code = str(run.get("error_code") or "")
                if err_code.startswith("APPROVAL_REQUIRED_"):
                    return
                if err_code not in ("", "APPROVAL_REJECTED"):
                    # 违规红线拦截告警卡片（用于演播室/飞书即时告警）
                    self._upsert_card(run, push_if_new=self.push_all_cards)
            elif run["status"] == "COMMITTED":
                if _is_revert_run(run):
                    return
                if run_id in self._cards or action_type in _REVERSIBLE_ACTIONS:
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
        settled_run_id: str | None = None,
        baseline_versions: dict[str, int] | None = None,
    ) -> CardRecord | None:
        """从持久化元数据恢复本地卡片索引、OCC 基线与飞书 ``message_id``（不触发外发）。"""
        with self._lock:
            if baseline_versions:
                self._baseline_versions[run_id] = {
                    str(k): int(v) for k, v in baseline_versions.items()
                }
            eff_settled_id = settled_run_id or self._resolve_settled_run_id(run_id)
            if settled_run_id:
                self._settled_run_by_card[run_id] = settled_run_id
            run: dict[str, Any] | None = None
            if eff_settled_id and eff_settled_id != run_id:
                settled = self.runtime.get_action_run(eff_settled_id)
                if settled is not None:
                    run = dict(settled)
                    run["run_id"] = run_id
                    if settled.get("approved_by") is None:
                        for r in reversed(self.runtime.list_action_runs("COMMITTED")):
                            if r.get("action_type") == "complete_work_card" and (r.get("params") or {}).get("card_id") == run_id:
                                run["approved_by"] = r.get("actor_id")
                                break
            if run is None:
                try:
                    sys_actor = Actor(actor_id="__card__", role="system", clearance_tags={"*"})
                    wc = self.runtime.get_object(o2c.WorkCard, run_id, sys_actor)
                    self._capture_card_baseline(run_id)
                    run = {
                        "run_id": wc.card_id,
                        "card_id": wc.card_id,
                        "action_type": "open_work_card",
                        "params": {"card_id": wc.card_id},
                        "status": wc.status,
                    }
                except Exception:
                    run = self.runtime.get_action_run(run_id)
            if run is None:
                return None
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
                settled_run_id=eff_settled_id if eff_settled_id != run_id else None,
                baseline_versions=dict(self._baseline_versions.get(run_id) or {}),
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
                    "settled_run_id": rec.settled_run_id or self._settled_run_by_card.get(run_id),
                    "baseline_versions": dict(
                        self._baseline_versions.get(run_id) or rec.baseline_versions
                    ),
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
        settled_run_id: str | None = None,
    ) -> CardRecord:
        run_id = str(run.get("card_id") or run["run_id"])
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
        eff_settled = settled_run_id or self._settled_run_by_card.get(run_id) or (
            existing.settled_run_id if existing else None
        )
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
            settled_run_id=eff_settled,
            baseline_versions=dict(self._baseline_versions.get(run_id) or {}),
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
                act_rid = f"{run_id}-ACT"
                comp_rid = f"{run_id}-COMP"
                self._suppress_event_run_ids.update({run_id, act_rid, comp_rid})
                try:
                    baseline = self._capture_card_baseline(run_id)
                    for obj_key, expected_ver in baseline.items():
                        obj_type, _, obj_id = obj_key.partition(":")
                        live_ver = _read_target_version(self.runtime, obj_type, obj_id)
                        if live_ver != expected_ver:
                            raise ConcurrencyConflictError(
                                f"stale approval on {obj_key}: card opened at v{expected_ver}, "
                                f"live version is v{live_ver}"
                            )
                    settled_res = o2c.settle_work_card(
                        self.runtime,
                        run_id,
                        actor,
                        resolution_note="财务总监飞书卡片批准",
                        action_run_id=act_rid,
                        complete_run_id=comp_rid,
                    )
                    committed = settled_res.get("settled_action_run") or settled_res
                except ConcurrencyConflictError as exc:
                    cancel_rid = f"{run_id}-CONFLICT-CANCEL"
                    self._suppress_event_run_ids.add(cancel_rid)
                    try:
                        wc = self.runtime.get_object(
                            o2c.WorkCard,
                            run_id,
                            Actor(actor_id="__occ__", role="system", clearance_tags={"*"}),
                        )
                        if wc.status in ("OPEN", "IN_PROGRESS"):
                            self.runtime.submit_action(
                                cancel_rid,
                                "cancel_work_card",
                                {"card_id": run_id, "resolution_note": str(exc)},
                                Actor(actor_id="__occ__", role="admin", clearance_tags={"*"}),
                            )
                    except Exception:
                        pass
                    finally:
                        self._suppress_event_run_ids.discard(cancel_rid)
                    msg = f"并发锁熔断：审批期间底层 ERP 单据已被修改，原签批单自动作废（{exc}）"
                    card_run = {
                        "run_id": run_id,
                        "card_id": run_id,
                        "action_type": "open_work_card",
                        "params": {"card_id": run_id},
                        "status": "CONFLICT",
                    }
                    rec = self._upsert_card(
                        card_run,
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
                except (InvariantError, PermissionDeniedError, GovernanceViolationError) as exc:
                    err_code = getattr(exc, "code", None)
                    return {
                        "toast": {"type": "error", "content": str(exc)},
                        **({"error_code": err_code} if err_code else {}),
                    }
                finally:
                    self._suppress_event_run_ids.discard(run_id)
                    self._suppress_event_run_ids.discard(act_rid)
                    self._suppress_event_run_ids.discard(comp_rid)

                settled_rid = str(committed["run_id"])
                self._settled_run_by_card[run_id] = settled_rid
                evidence = self.adapter.process_new_runs() if self.adapter is not None else []
                if (
                    not evidence
                    and self.adapter is not None
                    and hasattr(self.adapter, "get_run_evidence")
                ):
                    evidence = self.adapter.get_run_evidence(settled_rid)
                card_view_run = dict(committed)
                card_view_run["card_id"] = run_id
                card_view_run["run_id"] = run_id
                card_view_run["approved_by"] = committed.get("approved_by") or actor.actor_id
                rec = self._upsert_card(
                    card_view_run,
                    push_if_new=False,
                    erp_evidence=evidence,
                    settled_run_id=settled_rid,
                )
                return {
                    "toast": {"type": "success", "content": "已签批并同步写入真实 ERP"},
                    "card": rec.card,
                    "status": committed["status"],
                    "settled_run_id": settled_rid,
                    "erp_evidence": evidence,
                }

            if op == "reject":
                rej_rid = f"{run_id}-REJ"
                self._suppress_event_run_ids.update({run_id, rej_rid})
                try:
                    self.runtime.submit_action(
                        rej_rid,
                        "reject_work_card",
                        {"card_id": run_id, "resolution_note": "财务总监飞书卡片驳回"},
                        actor,
                    )
                except (InvariantError, PermissionDeniedError, GovernanceViolationError) as exc:
                    err_code = getattr(exc, "code", None)
                    return {
                        "toast": {"type": "error", "content": str(exc)},
                        **({"error_code": err_code} if err_code else {}),
                    }
                finally:
                    self._suppress_event_run_ids.discard(run_id)
                    self._suppress_event_run_ids.discard(rej_rid)

                card_view_run = {
                    "run_id": run_id,
                    "card_id": run_id,
                    "action_type": "open_work_card",
                    "params": {"card_id": run_id},
                    "status": "REJECTED",
                    "error_code": "APPROVAL_REJECTED",
                    "error_message": "财务总监飞书卡片驳回",
                    "approved_by": actor.actor_id,
                }
                rec = self._upsert_card(card_view_run, push_if_new=False, card_status_override="REJECTED")
                return {
                    "toast": {"type": "info", "content": "已驳回该高风险操作，ERP 保持原状"},
                    "card": rec.card,
                    "status": "REJECTED",
                }

            # op == "revert"
            target_run_id = self._resolve_settled_run_id(run_id)
            run = self.runtime.get_action_run(target_run_id)
            if run is None:
                return {"toast": {"type": "error", "content": f"流水号 {run_id} 不存在"}}

            revert_run_id = f"{target_run_id}-REVERT"
            self._suppress_event_run_ids.update({run_id, target_run_id, revert_run_id})
            try:
                self._check_revert_allowed(run)
                revert_run = self.runtime.revert_action(target_run_id, revert_run_id, actor)
            except (InvariantError, PermissionDeniedError, GovernanceViolationError) as exc:
                err_code = getattr(exc, "code", None)
                return {
                    "toast": {"type": "error", "content": str(exc)},
                    "error_code": err_code,
                }
            finally:
                self._suppress_event_run_ids.discard(run_id)
                self._suppress_event_run_ids.discard(target_run_id)
                self._suppress_event_run_ids.discard(revert_run_id)

            evidence = self.adapter.process_new_runs() if self.adapter is not None else []
            if (
                not evidence
                and self.adapter is not None
                and hasattr(self.adapter, "get_run_evidence")
            ):
                evidence = self.adapter.get_run_evidence(revert_run["run_id"])
            original_after = dict(self.runtime.get_action_run(target_run_id) or run)
            card_view_run = dict(original_after)
            card_view_run["card_id"] = run_id
            card_view_run["run_id"] = run_id
            if card_view_run.get("approved_by") is None and target_run_id != run_id:
                card_view_run["approved_by"] = actor.actor_id
            rec = self._upsert_card(
                card_view_run,
                push_if_new=False,
                erp_evidence=evidence,
                settled_run_id=target_run_id if target_run_id != run_id else None,
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
