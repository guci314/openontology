"""Unified L2 Enterprise Digital Twin Console Launcher (WO-26).

Mounts any of four isolated worlds onto the pure M2 ``open_ontology.console_server``
without contaminating the L1 kernel:

- ``standalone`` (default): zero external dependencies; assembles the 6-domain
  ``std_enterprise_world_model`` + org-level RLS + manufacturing ``CustomerOverlaySpec``
  with realistic multi-plant manufacturing & financial data ("华实智能装备股份有限公司").
- ``odoo19``: hybrid mount onto live Odoo 19 JSON-RPC (coupled O2C/P2P/R2R +
  standalone CRM/P2M/H2R + monotonic overlay).
- ``jsherp``: hybrid mount onto live jshERP REST API (coupled P2P/O2C/H2R +
  standalone CRM/P2M/R2R + monotonic overlay).
- ``starship``: completely fictional ``Starship / WarpCore`` domain proving L1
  ``console_server`` and L1 UI shell domain agnosticism.

Usage::

    python3 examples/enterprise_console_app.py --mode standalone --port 8788
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from open_ontology import Actor, InvariantError, ObjectProperty, ObjectType, OntologyRuntime, OntologySpec
from open_ontology.autonomy import (
    ActionAutonomyRule,
    AutonomyPolicy,
    AutonomyRampController,
)
from open_ontology.console_server import (
    ConsoleServerHandle,
    ConsoleService,
    DemoActorProfile,
    WorkQueueBinding,
    create_console_app,
    start_console_server,
)
from open_ontology.induction import CustomerOverlaySpec, FieldSpec, InvariantRuleSpec
from open_ontology.org_authority import OrgDirectory, hash_auth_token

from examples.adapters.erp_world_mount import (
    _attach_standard_functions,
    mount_jsherp_enterprise_world,
    mount_odoo19_enterprise_world,
)
from examples.business_ontology import WorkCard, settle_work_card
from examples.std_core_ontology import STD_ROLES, spec as std_core_spec
from examples.std_crm_ontology import spec as std_crm_spec
from examples.std_enterprise_world_model import (
    build_org_rls_enterprise_spec,
    project_and_post_event_journal,
    project_h2r_to_org_directory,
)
from examples.std_h2r_ontology import spec as std_h2r_spec
from examples.std_o2c_ontology import spec as std_o2c_spec
from examples.std_p2m_ontology import spec as std_p2m_spec
from examples.std_p2p_ontology import spec as std_p2p_spec
from examples.std_r2r_ontology import spec as std_r2r_spec

__all__ = [
    "HUASHI_MFG_OVERLAY",
    "ENTERPRISE_WORK_QUEUE",
    "ENTERPRISE_SANDBOX_PRESETS",
    "build_console_service",
    "build_standalone_enterprise_console",
    "build_starship_console",
    "enterprise_assistant_responder",
    "main",
]

HUASHI_MFG_OVERLAY = CustomerOverlaySpec(
    overlay_id="huashi_mfg_enterprise_overlay",
    base_model_id="std_enterprise_world_model",
    target_version="1.0.0",
    description="华实智能装备制造企业只增不破扩展：新增自有字段及客诉阻断赢单/发货先决发票红线。",
    add_fields={
        "CrmAccount": [
            FieldSpec(
                name="vip_tier",
                field_type="str",
                default="STANDARD",
                owned=True,
                description="客户战略等级（自有主数据扩展）",
            ),
        ],
        "O2cOrder": [
            FieldSpec(
                name="sales_channel",
                field_type="str",
                default="direct",
                owned=True,
                description="销售签约渠道（自有扩展）",
            ),
            FieldSpec(
                name="risk_note",
                field_type="str",
                default="",
                owned=True,
                description="风控授信备注（自有扩展）",
            ),
        ],
        "P2pPurchaseOrder": [
            FieldSpec(
                name="plant_cost_center",
                field_type="str",
                default="CC-HQ",
                owned=True,
                description="所属厂区成本中心（自有扩展）",
            ),
            FieldSpec(
                name="buyer_note",
                field_type="str",
                default="",
                owned=True,
                description="采购议价备注（自有扩展）",
            ),
        ],
        "WorkOrder": [
            FieldSpec(
                name="production_line",
                field_type="str",
                default="LINE-A1",
                owned=True,
                description="车间产线编号（自有扩展）",
            ),
        ],
    },
    add_action_preconditions={
        "win_crm_deal": [
            InvariantRuleSpec(
                name="overlay_no_open_high_ticket_win",
                code="OPEN_HIGH_PRIORITY_TICKET_BLOCKS_WIN",
                target_type="CrmDeal",
                id_param="deal_id",
                must_hold={
                    "op": "not_exists",
                    "object_type": "CrmTicket",
                    "where": {
                        "op": "and",
                        "args": [
                            {"op": "eq", "left": {"ref": "target.account"}, "right": {"ref": "obj.account"}},
                            {"op": "eq", "left": {"ref": "target.priority"}, "right": {"lit": "HIGH"}},
                            {
                                "op": "in",
                                "left": {"ref": "target.status"},
                                "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                            },
                        ],
                    },
                },
                description="客户存在未闭环高优客诉时禁止直接推进商机赢单。",
            ),
        ],
        "post_o2c_invoice": [
            InvariantRuleSpec(
                name="overlay_require_delivery_before_invoice",
                code="DELIVERY_REQUIRED_BEFORE_INVOICE",
                target_type="O2cInvoice",
                id_param="invoice_id",
                must_hold={
                    "op": "in",
                    "left": {"ref": "obj.order.status"},
                    "right": {"lit": ["delivered", "invoiced"]},
                },
                description="销售订单必须完成物流发货后方可正式开具应收发票。",
            ),
        ],
    },
)

ENTERPRISE_WORK_QUEUE = WorkQueueBinding(
    object_type="WorkCard",
    id_field="card_id",
    kind_field="card_kind",
    status_field="status",
    active_statuses=("OPEN", "IN_PROGRESS", "SLA_BREACHED"),
    target_type_field="target_object_type",
    target_id_field="target_object_id",
    intent_action_field="requested_action",
    intent_params_field="requested_params_json",
    requester_field="requester_actor_id",
    assignee_role_field="assignee_role",
    accept_op="complete_work_card",
    decline_op="reject_work_card",
    claim_op="claim_work_card",
    id_param="card_id",
    note_param="resolution_note",
    settled_run_param="settled_run_id",
)

ENTERPRISE_SANDBOX_PRESETS: list[dict[str, Any]] = [
    {
        "preset_id": "emergency_repair_20k",
        "title": "突发 ¥20,000 维修费测算",
        "subtitle": "在平行沙盘中过账华北压铸线突发维修费用凭证，实时评估银行现金、费用利润与试算平衡。",
        "badge": "财务压力测试",
        "steps": [
            {
                "action": "post_journal_entry",
                "params": {"entry_id": "JE-MAINT-20K"},
                "actor_id": "act-cfo",
            }
        ],
    },
    {
        "preset_id": "large_order_invoice_drill",
        "title": "大单开票合规演练",
        "subtitle": "模拟 SO-301（¥180,000 伺服阀组）发货、开票过账、全额回款及总账入账全链路。",
        "badge": "O2C→R2R 闭环",
        "steps": [
            {
                "action": "complete_o2c_delivery",
                "params": {"delivery_id": "DEL-301"},
                "actor_id": "act-cfo",
            },
            {
                "action": "post_o2c_invoice",
                "params": {"invoice_id": "INV-301"},
                "actor_id": "act-cfo",
            },
            {
                "action": "record_o2c_payment",
                "params": {"invoice_id": "INV-301", "payment_amount": 180000.0},
                "actor_id": "act-cfo",
            },
            {
                "action": "post_journal_entry",
                "params": {"entry_id": "JE-SO301-REV"},
                "actor_id": "act-cfo",
            },
            {
                "action": "post_journal_entry",
                "params": {"entry_id": "JE-SO301-CASH"},
                "actor_id": "act-cfo",
            },
        ],
    },
    {
        "preset_id": "short_weight_dispute_settle",
        "title": "短磅争议解封试算",
        "subtitle": "批结短磅工单并按 95 吨实收金额 ¥95,000 重算 PO-101 与应付账单，立省 ¥5,000 损耗。",
        "badge": "P2P 争议裁决",
        "steps": [
            {
                "action": "complete_work_card",
                "params": {
                    "card_id": "WC-PO-101-DISP",
                    "resolution_note": "沙盘试算：按 95 吨磅单净重核定结算",
                },
                "actor_id": "act-proc-mgr",
            },
            {
                "action": "resolve_p2p_dispute",
                "params": {
                    "po_id": "PO-101",
                    "receipt_id": "GR-101",
                    "work_card_id": "WC-PO-101-DISP",
                    "settled_amount": 95000.0,
                },
                "actor_id": "act-proc-mgr",
            },
            {
                "action": "reconcile_p2p_bill",
                "params": {"bill_id": "BILL-101"},
                "actor_id": "act-proc-mgr",
            },
            {
                "action": "post_p2p_bill",
                "params": {"bill_id": "BILL-101"},
                "actor_id": "act-proc-mgr",
            },
        ],
    },
]

DEMO_TOKEN_SECRET: dict[str, str] = {
    "act-cfo": "tok-demo-cfo",
    "act-north-pm": "tok-demo-north-pm",
    "act-south-sf": "tok-demo-south-sf",
    "act-proc-mgr": "tok-demo-proc-mgr",
    "act-fin": "tok-demo-fin",
    "act-admin": "tok-demo-admin",
}


def _merge_std_display(spec: OntologySpec) -> None:
    for src in (
        std_core_spec,
        std_crm_spec,
        std_o2c_spec,
        std_p2p_spec,
        std_p2m_spec,
        std_r2r_spec,
        std_h2r_spec,
    ):
        if src.display:
            spec.update_display(src.display)

    spec.update_display(
        {
            "ontologies": {
                "std_enterprise_world_model": "华实智能装备企业数字孪生标准世界模型",
            },
            "roles": {
                "finance_director": "财务总监 (CFO)",
                "plant_manager": "厂长",
                "shopfloor": "车间报工专员",
                "procurement_manager": "采购经理",
                "finance": "总账会计",
                "chief_accountant": "财务主管",
                "warehouse": "仓储专员",
                "sales_rep": "销售代表",
                "sales_manager": "销售主管",
                "support_agent": "客服支持",
                "production_planner": "生产计划员",
                "hr_admin": "组织人事专员",
                "hr_director": "人力总监",
                "buyer": "采购专员",
                "ops": "运营协同",
                "steward": "治理专员",
                "admin": "实施管理员",
            },
            "fields": {
                "org": "所属厂区/组织标签",
                "vip_tier": "客户战略等级",
                "sales_channel": "签约渠道",
                "risk_note": "风控备注",
                "plant_cost_center": "厂区成本中心",
                "buyer_note": "采购备注",
                "production_line": "产线编号",
                "approval_ceiling_cents": "审批额度上限（分）",
                "role_name": "岗位对应角色",
                "scope_tag": "组织权限标签",
                "department_id": "部门编码",
                "parent_department": "上级部门",
                "position_id": "岗位编码",
                "employee_id": "员工编码",
                "full_name": "员工姓名",
                "actor_id": "登录账号标识",
                "mandate_id": "动态授权单号",
                "grantor": "授权人",
                "grantee": "被授权人",
                "ceiling_cents": "授权额度（分）",
                "order": "关联销售订单",
                "customer": "客户档案",
                "account": "客户账户",
                "supplier": "供应商档案",
                "purchase_order": "关联采购单",
                "work_order": "关联生产工单",
                "bom": "BOM 配方",
                "good_qty": "良品数量",
                "scrap_qty": "报废数量",
                "absorbed_cost_cents": "吸收成本（分）",
                "issued_qty": "已领料数量",
                "issued_cost_cents": "已投入成本（分）",
                "completed_good_qty": "已完工良品",
                "completed_scrap_qty": "已报废数量",
                "planned_qty": "计划产量",
                "qty": "领料数量",
                "unit_cost_cents": "单位成本（分）",
                "total_cost_cents": "总成本（分）",
                "allocation_rule": "成本分摊策略",
                "source_event": "来源履约凭证",
                "output_event": "产出履约凭证",
            },
            "statuses": {
                "enterprise_direct": "企业直销",
                "key_account": "大客户直签",
                "partner_oem": "OEM 伙伴",
            },
        }
    )


def _enterprise_escalation_builder(**ctx: Any) -> dict[str, Any]:
    action_name = str(ctx.get("action") or "")
    params = dict(ctx.get("params") or {})
    rule = ctx.get("rule")
    target_type = "JournalEntry"
    target_id = str(params.get("entry_id") or "")
    if "order_id" in params:
        target_type, target_id = "O2cOrder", str(params["order_id"])
    elif "invoice_id" in params:
        target_type, target_id = "O2cInvoice", str(params["invoice_id"])
    elif "po_id" in params:
        target_type, target_id = "P2pPurchaseOrder", str(params["po_id"])
    elif "bill_id" in params:
        target_type, target_id = "P2pVendorBill", str(params["bill_id"])
    elif "work_order_id" in params:
        target_type, target_id = "WorkOrder", str(params["work_order_id"])
    elif "deal_id" in params:
        target_type, target_id = "CrmDeal", str(params["deal_id"])
    elif not target_id:
        for val in params.values():
            if isinstance(val, str) and val.strip():
                target_id = val.strip()
                break

    assignee = (
        getattr(rule, "escalation_assignee_role", None) or "finance_director"
    )
    import json
    import uuid

    return {
        "card_id": f"WC-ESC-{uuid.uuid4().hex[:8].upper()}",
        "card_kind": "APPROVAL",
        "target_object_type": target_type,
        "target_object_id": target_id or action_name,
        "requested_action": action_name,
        "requested_params_json": json.dumps(params, ensure_ascii=False),
        "assignee_role": assignee,
        "external_ref": f"AUTO-ESC-{action_name}",
    }


def enterprise_assistant_responder(
    runtime: OntologyRuntime,
    actor: Actor,
    question: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministic, zero-key, strictly RLS-scoped enterprise NL assistant."""
    q = question.strip().lower()
    cards: list[dict[str, Any]] = []
    highlights: list[dict[str, Any]] = []

    work_cards = list(runtime.query_objects("WorkCard", actor))
    open_cards = [
        c for c in work_cards if c.status in ("OPEN", "IN_PROGRESS", "SLA_BREACHED")
    ]
    pos = list(runtime.query_objects("P2pPurchaseOrder", actor))
    wos = list(runtime.query_objects("WorkOrder", actor))
    sos = list(runtime.query_objects("O2cOrder", actor))
    invs = list(runtime.query_objects("O2cInvoice", actor))

    try:
        tb = runtime.call_function("compute_trial_balance", {"accounting_entity": "ENT-GROUP"}, actor)
    except Exception:
        tb = {}

    bank_cash_yuan = 0.0
    for acc in runtime.query_objects("GlAccount", actor):
        if acc.account_code == "1001":
            bank_cash_yuan = (acc.debit_total_cents - acc.credit_total_cents) / 100.0
            break
    ar_residual = sum(
        float(i.amount_residual) for i in invs if i.status == "posted"
    )
    so_total = sum(float(o.amount_total) for o in sos if o.status != "cancelled")

    if any(kw in q for kw in ("待办", "审批", "工单", "短磅", "异常", "card", "work")):
        for card in open_cards[:4]:
            cards.append(
                {
                    "kind": "object",
                    "title": f"{card.card_id} · {card.target_object_type}:{card.target_object_id}",
                    "subtitle": f"指派角色：{card.assignee_role} | 状态：{card.status}",
                    "object_type": "WorkCard",
                    "object_id": card.card_id,
                }
            )
        highlights = [
            {"label": "当前可见待批工单", "value": f"{len(open_cards)} 项"},
            {"label": "可见采购争议", "value": f"{sum(1 for p in pos if p.status == 'disputed')} 笔"},
            {"label": "可见生产工单", "value": f"{len(wos)} 单"},
        ]
        reply = (
            f"在您当前的权限视角下，共有 {len(open_cards)} 项待处理协同审批："
            + (
                "；".join(
                    f"{c.card_id}（{c.target_object_type} {c.target_object_id} → 需 {c.assignee_role}）"
                    for c in open_cards[:3]
                )
                if open_cards
                else "当前待办队列已全部清零。"
            )
        )
        return {"reply": reply, "highlights": highlights, "cards": cards}

    if any(kw in q for kw in ("维修", "沙盘", "推演", "模拟", "假设", "如果", "what", "20000", "20,000")):
        cards.append(
            {
                "kind": "sandbox_preset",
                "title": "立即前往平行沙盘：突发 ¥20,000 维修费测算",
                "subtitle": "零影响分叉测试对现金、利润及总账试算平衡的精确影响",
                "preset_id": "emergency_repair_20k",
                "route": "/sandbox",
            }
        )
        cards.append(
            {
                "kind": "sandbox_preset",
                "title": "大单开票合规演练（SO-301 · ¥180,000）",
                "subtitle": "演练发货→开票→回款全链路状态机",
                "preset_id": "large_order_invoice_drill",
                "route": "/sandbox",
            }
        )
        reply = (
            f"当前主账套银行现金 ¥{bank_cash_yuan:,.0f}。"
            f"若模拟突发 ¥20,000 维修费入账，沙盘预估银行现金将变为 ¥{bank_cash_yuan - 20000:,.0f}，"
            "当期利润减少 ¥20,000，总账借贷依然严格保持试算平衡（主库数据零改动）。点击下方卡片可直接打开平行沙盘验证。"
        )
        highlights = [
            {"label": "当前银行现金", "value": f"¥{bank_cash_yuan:,.0f}"},
            {"label": "推演后预估现金", "value": f"¥{bank_cash_yuan - 20000:,.0f}"},
            {"label": "推演对真实库影响", "value": "0 改动"},
        ]
        return {"reply": reply, "highlights": highlights, "cards": cards}

    if any(kw in q for kw in ("厂", "权限", "隔离", "rls", "可见", "范围", "north", "south")):
        for wo in wos[:4]:
            cards.append(
                {
                    "kind": "object",
                    "title": f"生产工单 {wo.work_order_id}（{getattr(wo, 'production_line', '产线')}）",
                    "subtitle": f"厂区标签：{getattr(wo, 'org', '-')} | 状态：{wo.status}",
                    "object_type": "WorkOrder",
                    "object_id": wo.work_order_id,
                }
            )
        orgs = sorted({str(getattr(x, "org", "")) for x in (*pos, *wos, *sos) if getattr(x, "org", "")})
        highlights = [
            {"label": "可见厂区范围", "value": ", ".join(orgs) or "全部"},
            {"label": "可见生产工单", "value": f"{len(wos)} 单"},
            {"label": "可见销售订单", "value": f"{len(sos)} 单"},
        ]
        reply = (
            f"系统按您的岗位角色「{actor.role}」严格执行行级隔离（可见范围：{', '.join(sorted(actor.clearance_tags)) or '默认'}）。"
            f"当前可见采购单 {len(pos)} 笔、生产工单 {len(wos)} 笔、销售订单 {len(sos)} 笔（所属厂区：{', '.join(orgs) or '总部'}）。"
        )
        return {"reply": reply, "highlights": highlights, "cards": cards}

    if any(kw in q for kw in ("拦截", "红线", "审计", "报错", "为什么", "block")):
        rejected = list(context.get("visible_rejected_runs") or ())
        latest = rejected[-1] if rejected else None
        highlights = [
            {"label": "累计红线拦截", "value": f"{len(rejected)} 次"},
            {"label": "真实系统脏写", "value": "0 次"},
        ]
        if latest:
            reply = (
                f"最近一次拦截发生在动作「{latest['action_type']}」，触发规则 {latest.get('error_code')}。"
                "所有违规写入均在进入事务前被内核阻断，外部系统与主台账保持零脏写。"
            )
        else:
            reply = "当前审计账本尚未记录违规拦截事件，系统运行正常。"
        return {"reply": reply, "highlights": highlights, "cards": cards}

    # Default comprehensive executive brief scoped to current actor's RLS view
    balanced_text = "平衡绿灯" if tb.get("is_balanced", True) else "待核查"
    highlights = [
        {"label": "可见在手订单总额", "value": f"¥{so_total:,.0f}"},
        {"label": "应收未收余额", "value": f"¥{ar_residual:,.0f}"},
        {"label": "银行存款余额", "value": f"¥{bank_cash_yuan:,.0f}"},
        {"label": "待处理审批", "value": f"{len(open_cards)} 项"},
    ]
    for wo in wos[:2]:
        cards.append(
            {
                "kind": "object",
                "title": f"生产工单 {wo.work_order_id}",
                "subtitle": f"厂区：{getattr(wo, 'org', '集团')} · 计划 {wo.planned_qty:.0f}",
                "object_type": "WorkOrder",
                "object_id": wo.work_order_id,
            }
        )
    for po in pos[:2]:
        cards.append(
            {
                "kind": "object",
                "title": f"采购单 {po.po_number}",
                "subtitle": f"金额 ¥{po.amount_total:,.0f} · 状态 {po.status}",
                "object_type": "P2pPurchaseOrder",
                "object_id": po.po_id,
            }
        )
    reply = (
        f"截至目前，您权限范围内共有在手销售订单 ¥{so_total:,.0f}、待收应收账款 ¥{ar_residual:,.0f}、"
        f"银行现金 ¥{bank_cash_yuan:,.0f}，总账试算状态为【{balanced_text}】。"
        f"待办收件箱有 {len(open_cards)} 项异常或高额审批等待处理。"
    )
    return {"reply": reply, "highlights": highlights, "cards": cards}


def _seed_standalone_enterprise_world(rt: OntologyRuntime, admin: Actor) -> tuple[OrgDirectory, list[DemoActorProfile]]:
    spec = rt.spec
    EntCls = spec.get_object_type("AccountingEntity")
    DeptCls = spec.get_object_type("Department")
    PosCls = spec.get_object_type("JobPosition")
    EmpCls = spec.get_object_type("Employee")
    MndCls = spec.get_object_type("AuthorityMandate")

    rt.put_object(
        EntCls(entity_id="ENT-GROUP", name="华实智能装备股份有限公司", base_currency="CNY"),
        actor=admin,
    )

    depts = [
        ("DEPT-HQ", "集团总部", None, "org:hq", "active"),
        ("DEPT-NORTH-MFG", "华北智能压铸厂", "DEPT-HQ", "org:plant_north", "active"),
        ("DEPT-SOUTH-MFG", "华南精密装配厂", "DEPT-HQ", "org:plant_south", "active"),
        ("DEPT-SALES", "华实销售中心", "DEPT-HQ", "org:sales", "active"),
        ("DEPT-PROC", "集团采购中心", "DEPT-HQ", "org:proc", "active"),
        ("DEPT-FIN", "集团财务中心", "DEPT-HQ", "org:fin", "active"),
        ("DEPT-OLD-ARCH", "已撤并老车间", "DEPT-HQ", "org:legacy_arch", "archived"),
    ]
    for did, name, pid, stag, st in depts:
        rt.put_object(
            DeptCls(
                department_id=did,
                accounting_entity="ENT-GROUP",
                name=name,
                parent_department=pid,
                scope_tag=stag,
                status=st,
            ),
            actor=admin,
        )

    positions = [
        ("POS-CFO", "DEPT-HQ", "集团财务总监 (CFO)", "finance_director", 500_000_000),
        ("POS-ADMIN", "DEPT-HQ", "系统实施管理员", "admin", 999_999_999),
        ("POS-PM-NORTH", "DEPT-NORTH-MFG", "华北厂长", "plant_manager", 100_000_000),
        ("POS-SF-NORTH", "DEPT-NORTH-MFG", "华北车间报工", "shopfloor", 5_000_000),
        ("POS-SF-SOUTH", "DEPT-SOUTH-MFG", "华南车间装配", "shopfloor", 5_000_000),
        ("POS-SALES-REP", "DEPT-SALES", "华北销售经理", "sales_rep", 5_000_000),
        ("POS-SUPPORT", "DEPT-SALES", "客户支持专员", "support_agent", 5_000_000),
        ("POS-PROC-MGR", "DEPT-PROC", "采购经理", "procurement_manager", 30_000_000),
        ("POS-WH", "DEPT-NORTH-MFG", "中心仓管主管", "warehouse", 5_000_000),
        ("POS-PLANNER", "DEPT-NORTH-MFG", "主生产计划员", "production_planner", 10_000_000),
        ("POS-FIN", "DEPT-FIN", "总账会计", "finance", 10_000_000),
    ]
    for pid, did, title, rname, ceil_cents in positions:
        rt.put_object(
            PosCls(
                position_id=pid,
                department=did,
                title=title,
                role_name=rname,
                approval_ceiling_cents=ceil_cents,
                status="active",
            ),
            actor=admin,
        )

    emps = [
        ("EMP-CFO", "DEPT-HQ", "POS-CFO", "陈总", "act-cfo", "active"),
        ("EMP-ADMIN", "DEPT-HQ", "POS-ADMIN", "实施管理员", "act-admin", "active"),
        ("EMP-NORTH-PM", "DEPT-NORTH-MFG", "POS-PM-NORTH", "张厂长", "act-north-pm", "active"),
        ("EMP-NORTH-SF", "DEPT-NORTH-MFG", "POS-SF-NORTH", "赵师傅", "act-north-sf", "active"),
        ("EMP-SOUTH-SF", "DEPT-SOUTH-MFG", "POS-SF-SOUTH", "钱师傅", "act-south-sf", "active"),
        ("EMP-SALES", "DEPT-SALES", "POS-SALES-REP", "孙经理", "act-sales", "active"),
        ("EMP-SUPPORT", "DEPT-SALES", "POS-SUPPORT", "吴客服", "act-support", "active"),
        ("EMP-PROC-MGR", "DEPT-PROC", "POS-PROC-MGR", "郑经理", "act-proc-mgr", "active"),
        ("EMP-WH", "DEPT-NORTH-MFG", "POS-WH", "冯仓管", "act-wh", "active"),
        ("EMP-PLANNER", "DEPT-NORTH-MFG", "POS-PLANNER", "周计划", "act-planner", "active"),
        ("EMP-FIN", "DEPT-FIN", "POS-FIN", "韩会计", "act-fin", "active"),
    ]
    for eid, did, pid, name, aid, st in emps:
        rt.put_object(
            EmpCls(
                employee_id=eid,
                department=did,
                position=pid,
                full_name=name,
                actor_id=aid,
                status=st,
            ),
            actor=admin,
        )

    # Scoped cross-plant read mandates from HQ CFO -> Procurement, Finance & Sales roles
    mandates = [
        ("MND-PROC-N", "EMP-CFO", "EMP-PROC-MGR", "procurement_manager", 30_000_000, "org:plant_north"),
        ("MND-PROC-S", "EMP-CFO", "EMP-PROC-MGR", "procurement_manager", 30_000_000, "org:plant_south"),
        ("MND-FIN-N", "EMP-CFO", "EMP-FIN", "finance", 10_000_000, "org:plant_north"),
        ("MND-FIN-S", "EMP-CFO", "EMP-FIN", "finance", 10_000_000, "org:plant_south"),
        ("MND-FIN-SALES", "EMP-CFO", "EMP-FIN", "finance", 10_000_000, "org:sales"),
        ("MND-SALES-N", "EMP-CFO", "EMP-SALES", "sales_rep", 5_000_000, "org:plant_north"),
        ("MND-SALES-S", "EMP-CFO", "EMP-SALES", "sales_rep", 5_000_000, "org:plant_south"),
    ]
    for mid, grantor, grantee, rname, ceil, stag in mandates:
        rt.put_object(
            MndCls(
                mandate_id=mid,
                grantor=grantor,
                grantee=grantee,
                role_name=rname,
                ceiling_cents=ceil,
                scope_tag=stag,
                status="active",
            ),
            actor=admin,
        )

    token_hashes = {aid: hash_auth_token(tok) for aid, tok in DEMO_TOKEN_SECRET.items()}
    directory = project_h2r_to_org_directory(rt, admin, token_hashes=token_hashes)

    # Actors projected cleanly via M2 OrgDirectory
    cfo_actor = directory.resolve_actor("act-cfo")
    sales_actor = directory.resolve_actor("act-sales")
    support_actor = directory.resolve_actor("act-support")
    proc_actor = directory.resolve_actor("act-proc-mgr")
    wh_actor = directory.resolve_actor("act-wh")
    planner_actor = directory.resolve_actor("act-planner")
    north_sf_actor = directory.resolve_actor("act-north-sf")
    fin_actor = directory.resolve_actor("act-fin")

    # Core Master Data + REA Commitments
    PartyCls = spec.get_object_type("BusinessParty")
    ResCls = spec.get_object_type("TradeResource")
    CommCls = spec.get_object_type("TradeCommitment")
    EvtCls = spec.get_object_type("FulfillmentEvent")

    for pid, lname, tid in (
        ("PTY-CORP", "华实智能装备股份有限公司", "91110108MA01HUASHI"),
        ("PTY-SUP-STEEL", "鞍山精品特钢有限公司", "91210300MA02ANSHAN"),
        ("PTY-SUP-VALVE", "苏州精控液压阀业有限公司", "91320500MA03SUZHOU"),
        ("PTY-CUST-EV", "华南新能源汽车有限公司", "91440300MA04NEVEVX"),
        ("PTY-CUST-RAIL", "北方轨道交通装备集团", "91120116MA05RAILGY"),
    ):
        rt.put_object(PartyCls(party_id=pid, legal_name=lname, tax_id=tid), actor=admin)

    for rid, code, name, unit, summary in (
        ("RES-COIL", "STEEL-Q235", "汽车级冷轧钢卷", "TON", "1.2mm 高强冲压用"),
        ("RES-CHASSIS", "EV-CHS-800", "新能源一体压铸车架", "EA", "800V 平台后副车架"),
        ("RES-VALVE", "HYD-VLV-320", "高压伺服液压阀组", "SET", "闭环比例压力控制"),
    ):
        rt.put_object(
            ResCls(resource_id=rid, code=code, name=name, unit=unit, spec_summary=summary),
            actor=admin,
        )

    for cid, bp, sp, res, qty, up, tot in (
        ("CM-SALE-EV", "PTY-CUST-EV", "PTY-CORP", "RES-CHASSIS", 50.0, 4800.0, 240_000.0),
        ("CM-SALE-RAIL", "PTY-CUST-RAIL", "PTY-CORP", "RES-VALVE", 30.0, 6000.0, 180_000.0),
        ("CM-SALE-SOUTH", "PTY-CUST-EV", "PTY-CORP", "RES-VALVE", 25.0, 6000.0, 150_000.0),
        ("CM-PO-STEEL", "PTY-CORP", "PTY-SUP-STEEL", "RES-COIL", 100.0, 1000.0, 100_000.0),
        ("CM-PO-VALVE", "PTY-CORP", "PTY-SUP-VALVE", "RES-COIL", 50.0, 1200.0, 60_000.0),
    ):
        rt.put_object(
            CommCls(
                commitment_id=cid,
                buyer_party=bp,
                seller_party=sp,
                resource=res,
                quantity=qty,
                unit_price=up,
                total_amount=tot,
                status="agreed",
            ),
            actor=admin,
        )

    # CRM
    AccCls = spec.get_object_type("CrmAccount")
    DealCls = spec.get_object_type("CrmDeal")
    TicketCls = spec.get_object_type("CrmTicket")

    rt.put_object(
        AccCls(
            account_id="ACC-EV",
            name="华南新能源汽车",
            party="PTY-CUST-EV",
            industry="新能源汽车制造",
            vip_tier="STRATEGIC",
        ),
        actor=admin,
    )
    rt.put_object(
        AccCls(
            account_id="ACC-RAIL",
            name="北方轨道交通装备",
            party="PTY-CUST-RAIL",
            industry="轨道交通与高端装备",
            vip_tier="VIP_KEY",
        ),
        actor=admin,
    )
    rt.put_object(
        DealCls(
            deal_id="DEAL-EV-200",
            title="50套新能源一体压铸车架年度框架",
            account="ACC-EV",
            amount=240_000.0,
            stage="PROPOSAL",
        ),
        actor=admin,
    )
    rt.put_object(
        DealCls(
            deal_id="DEAL-RAIL-180",
            title="30套伺服液压阀组二期增补",
            account="ACC-RAIL",
            amount=180_000.0,
            stage="NEGOTIATION",
        ),
        actor=admin,
    )
    rt.put_object(
        DealCls(
            deal_id="DEAL-EV-NEW",
            title="华南三期纯电平台试制批量订单",
            account="ACC-EV",
            amount=320_000.0,
            stage="PROPOSAL",
        ),
        actor=admin,
    )
    rt.put_object(
        TicketCls(
            ticket_id="TK-EV-200",
            subject="压铸车架焊点超声波探伤标准确认",
            account="ACC-EV",
            priority="HIGH",
            status="OPEN",
        ),
        actor=admin,
    )
    rt.put_object(
        TicketCls(
            ticket_id="TK-RAIL-01",
            subject="高压阀组低温耐压 72h 复测报告待签认",
            account="ACC-RAIL",
            priority="HIGH",
            status="OPEN",
        ),
        actor=admin,
    )
    rt.submit_action(
        "SEED-CRM-TK",
        "resolve_crm_ticket",
        {"ticket_id": "TK-EV-200", "resolution_summary": "完成超声波探伤抽检并签认标准"},
        support_actor,
    )
    rt.submit_action("SEED-CRM-WIN", "win_crm_deal", {"deal_id": "DEAL-EV-200"}, sales_actor)

    # P2P (North settled PO-100, North live disputed PO-101, South active PO-102)
    SupCls = spec.get_object_type("P2pSupplierProfile")
    PoCls = spec.get_object_type("P2pPurchaseOrder")
    GrCls = spec.get_object_type("P2pGoodsReceipt")
    BillCls = spec.get_object_type("P2pVendorBill")

    rt.put_object(SupCls(profile_id="SUP-STEEL", party="PTY-SUP-STEEL", accounting_entity="ENT-GROUP", payment_terms="NET30"), actor=admin)
    rt.put_object(SupCls(profile_id="SUP-VALVE", party="PTY-SUP-VALVE", accounting_entity="ENT-GROUP", payment_terms="NET45"), actor=admin)

    rt.put_object(
        PoCls(
            po_id="PO-100",
            po_number="PO-2026-100（华北钢卷首批）",
            supplier="SUP-STEEL",
            accounting_entity="ENT-GROUP",
            ordered_qty=100.0,
            unit_price=1000.0,
            amount_total=100_000.0,
            status="draft",
            org="plant_north",
            plant_cost_center="CC-NORTH-01",
            buyer_note="已完成短磅核对并结清",
        ),
        actor=admin,
    )
    rt.put_object(GrCls(receipt_id="GR-100", purchase_order="PO-100", expected_qty=100.0, status="pending"), actor=admin)
    rt.put_object(BillCls(bill_id="BILL-100", purchase_order="PO-100", amount_total=100_000.0, amount_residual=100_000.0, status="draft"), actor=admin)
    rt.submit_action("SEED-PO100-CONF", "confirm_p2p_order", {"po_id": "PO-100"}, proc_actor)
    rt.submit_action("SEED-PO100-RCV", "record_p2p_receipt", {"receipt_id": "GR-100", "received_qty": 95.0, "inspection_status": "passed"}, wh_actor)
    rt.submit_action(
        "SEED-PO100-WC-O",
        "open_work_card",
        {
            "card_id": "WC-PO-100-HIST",
            "card_kind": "HUMAN_TASK",
            "target_object_type": "P2pPurchaseOrder",
            "target_object_id": "PO-100",
            "requested_action": "resolve_p2p_dispute",
            "assignee_role": "procurement_manager",
        },
        wh_actor,
    )
    rt.submit_action(
        "SEED-PO100-WC-C",
        "complete_work_card",
        {"card_id": "WC-PO-100-HIST", "resolution_note": "首单核对实收 95 吨，扣减 ¥5,000"},
        proc_actor,
    )
    rt.submit_action(
        "SEED-PO100-RES",
        "resolve_p2p_dispute",
        {"po_id": "PO-100", "receipt_id": "GR-100", "work_card_id": "WC-PO-100-HIST", "settled_amount": 95_000.0},
        proc_actor,
    )
    rt.submit_action("SEED-PO100-REC", "reconcile_p2p_bill", {"bill_id": "BILL-100"}, fin_actor)
    rt.submit_action("SEED-PO100-POST", "post_p2p_bill", {"bill_id": "BILL-100"}, fin_actor)
    rt.submit_action("SEED-PO100-PAY", "record_p2p_payment", {"bill_id": "BILL-100", "payment_amount": 95_000.0}, fin_actor)

    # Live North PO-101 in short-weight dispute (awaiting procurement_manager signoff / Sandbox Preset 3)
    rt.put_object(
        PoCls(
            po_id="PO-101",
            po_number="PO-2026-101（华北特钢二批·短磅争议）",
            supplier="SUP-STEEL",
            accounting_entity="ENT-GROUP",
            ordered_qty=100.0,
            unit_price=1000.0,
            amount_total=100_000.0,
            status="draft",
            org="plant_north",
            plant_cost_center="CC-NORTH-01",
            buyer_note="实收 95t 较合同短磅 5t，待采购经理裁决",
        ),
        actor=admin,
    )
    rt.put_object(GrCls(receipt_id="GR-101", purchase_order="PO-101", expected_qty=100.0, status="pending"), actor=admin)
    rt.put_object(BillCls(bill_id="BILL-101", purchase_order="PO-101", amount_total=100_000.0, amount_residual=100_000.0, status="draft"), actor=admin)
    rt.submit_action("SEED-PO101-CONF", "confirm_p2p_order", {"po_id": "PO-101"}, proc_actor)
    rt.submit_action("SEED-PO101-RCV", "record_p2p_receipt", {"receipt_id": "GR-101", "received_qty": 95.0, "inspection_status": "passed"}, wh_actor)

    # Live South PO-102 strictly visible to South plant + CFO/Procurement/Finance
    rt.put_object(
        PoCls(
            po_id="PO-102",
            po_number="PO-2026-102（华南精控阀体坯料）",
            supplier="SUP-VALVE",
            accounting_entity="ENT-GROUP",
            ordered_qty=50.0,
            unit_price=1200.0,
            amount_total=60_000.0,
            status="draft",
            org="plant_south",
            plant_cost_center="CC-SOUTH-02",
            buyer_note="华南装配线月度常备料",
        ),
        actor=admin,
    )
    rt.put_object(GrCls(receipt_id="GR-102", purchase_order="PO-102", expected_qty=50.0, status="pending"), actor=admin)
    rt.put_object(BillCls(bill_id="BILL-102", purchase_order="PO-102", amount_total=60_000.0, amount_residual=60_000.0, status="draft"), actor=admin)
    rt.submit_action("SEED-PO102-CONF", "confirm_p2p_order", {"po_id": "PO-102"}, proc_actor)

    # Confirmed events for historical GL
    for eid, cid, kind, qty, amt in (
        ("EVT-CAP-1", "CM-SALE-EV", "JOURNAL_POST", 1.0, 500_000.0),
        ("EVT-P2P-RCV", "CM-PO-STEEL", "RECEIPT", 95.0, 95_000.0),
        ("EVT-P2P-PAY", "CM-PO-STEEL", "PAYMENT", 1.0, 95_000.0),
        ("EVT-P2M-ISS", "CM-SALE-EV", "PRODUCTION_ISSUE", 80.0, 80_000.0),
        ("EVT-P2M-FG", "CM-SALE-EV", "PRODUCTION_OUTPUT", 50.0, 80_000.0),
        ("EVT-O2C-SHIP", "CM-SALE-EV", "SHIPMENT", 50.0, 80_000.0),
        ("EVT-O2C-INV", "CM-SALE-EV", "INVOICE", 50.0, 240_000.0),
        ("EVT-O2C-PAY", "CM-SALE-EV", "PAYMENT", 1.0, 240_000.0),
        ("EVT-SO301-INV", "CM-SALE-RAIL", "INVOICE", 30.0, 180_000.0),
        ("EVT-SO301-PAY", "CM-SALE-RAIL", "PAYMENT", 1.0, 180_000.0),
    ):
        rt.put_object(
            EvtCls(
                event_id=eid,
                commitment=cid,
                event_kind=kind,
                quantity=qty,
                amount=amt,
                status="confirmed",
            ),
            actor=admin,
        )

    # P2M (North completed WO-200, North live WO-201 in_progress, South live WO-202 in_progress)
    BomCls = spec.get_object_type("BomSpec")
    WoCls = spec.get_object_type("WorkOrder")

    rt.put_object(
        BomCls(
            bom_id="BOM-CHASSIS-V1",
            output_resource="RES-CHASSIS",
            input_resource="RES-COIL",
            input_per_unit=1.5,
            max_scrap_ratio=0.05,
            version="1.0",
        ),
        actor=admin,
    )
    rt.put_object(
        BomCls(
            bom_id="BOM-VALVE-V2",
            output_resource="RES-VALVE",
            input_resource="RES-COIL",
            input_per_unit=0.8,
            max_scrap_ratio=0.05,
            version="2.0",
        ),
        actor=admin,
    )

    rt.put_object(
        WoCls(
            work_order_id="WO-200",
            accounting_entity="ENT-GROUP",
            bom="BOM-CHASSIS-V1",
            commitment="CM-SALE-EV",
            output_resource="RES-CHASSIS",
            planned_qty=50.0,
            org="plant_north",
            production_line="NORTH-PRESS-L1",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-WO200-REL", "release_work_order", {"work_order_id": "WO-200"}, planner_actor)
    rt.submit_action(
        "SEED-WO200-ISS",
        "issue_material_to_work_order",
        {
            "consumption_id": "CONS-200-1",
            "work_order_id": "WO-200",
            "input_resource": "RES-COIL",
            "qty": 80.0,
            "unit_cost_cents": 100_000,
            "allocation_rule": "FIFO",
            "source_event": "EVT-P2M-ISS",
        },
        wh_actor,
    )
    rt.submit_action(
        "SEED-WO200-OUT",
        "record_production_output",
        {
            "output_id": "OUT-200-1",
            "work_order_id": "WO-200",
            "output_resource": "RES-CHASSIS",
            "good_qty": 50.0,
            "scrap_qty": 0.0,
            "absorbed_cost_cents": 8_000_000,
            "output_event": "EVT-P2M-FG",
        },
        north_sf_actor,
    )
    rt.submit_action("SEED-WO200-CLOSE", "close_work_order", {"work_order_id": "WO-200"}, planner_actor)

    rt.put_object(
        WoCls(
            work_order_id="WO-201",
            accounting_entity="ENT-GROUP",
            bom="BOM-CHASSIS-V1",
            commitment="CM-SALE-EV",
            output_resource="RES-CHASSIS",
            planned_qty=50.0,
            org="plant_north",
            production_line="NORTH-PRESS-L2",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-WO201-REL", "release_work_order", {"work_order_id": "WO-201"}, planner_actor)
    rt.submit_action(
        "SEED-WO201-ISS",
        "issue_material_to_work_order",
        {
            "consumption_id": "CONS-201-1",
            "work_order_id": "WO-201",
            "input_resource": "RES-COIL",
            "qty": 80.0,
            "unit_cost_cents": 100_000,
            "allocation_rule": "FIFO",
            "source_event": "EVT-P2M-ISS",
        },
        wh_actor,
    )
    rt.submit_action(
        "SEED-WO201-OUT1",
        "record_production_output",
        {
            "output_id": "OUT-201-1",
            "work_order_id": "WO-201",
            "output_resource": "RES-CHASSIS",
            "good_qty": 40.0,
            "scrap_qty": 0.0,
            "absorbed_cost_cents": 6_000_000,
        },
        north_sf_actor,
    )

    # South plant WO-202 strictly scoped to plant_south
    rt.put_object(
        WoCls(
            work_order_id="WO-202",
            accounting_entity="ENT-GROUP",
            bom="BOM-VALVE-V2",
            commitment="CM-SALE-SOUTH",
            output_resource="RES-VALVE",
            planned_qty=30.0,
            org="plant_south",
            production_line="SOUTH-VALVE-L3",
        ),
        actor=admin,
    )
    rt.submit_action("SEED-WO202-REL", "release_work_order", {"work_order_id": "WO-202"}, admin)
    rt.submit_action(
        "SEED-WO202-ISS",
        "issue_material_to_work_order",
        {
            "consumption_id": "CONS-202-1",
            "work_order_id": "WO-202",
            "input_resource": "RES-COIL",
            "qty": 24.0,
            "unit_cost_cents": 120_000,
            "allocation_rule": "EXACT_LOT",
        },
        admin,
    )
    rt.submit_action(
        "SEED-WO202-OUT1",
        "record_production_output",
        {
            "output_id": "OUT-202-1",
            "work_order_id": "WO-202",
            "output_resource": "RES-VALVE",
            "good_qty": 20.0,
            "scrap_qty": 0.0,
            "absorbed_cost_cents": 1_920_000,
        },
        admin,
    )

    # O2C Orders (North paid SO-300, North confirmed pending delivery SO-301, South invoiced unpaid SO-302)
    CustCls = spec.get_object_type("O2cCustomerProfile")
    SoCls = spec.get_object_type("O2cOrder")
    DelCls = spec.get_object_type("O2cDelivery")
    InvCls = spec.get_object_type("O2cInvoice")

    rt.put_object(
        CustCls(profile_id="CUST-EV", party="PTY-CUST-EV", accounting_entity="ENT-GROUP", credit_limit=1_200_000.0),
        actor=admin,
    )
    rt.put_object(
        CustCls(profile_id="CUST-RAIL", party="PTY-CUST-RAIL", accounting_entity="ENT-GROUP", credit_limit=2_000_000.0),
        actor=admin,
    )

    rt.put_object(
        SoCls(
            order_id="SO-300",
            order_number="SO-2026-300（华南新能源·已收款）",
            customer="CUST-EV",
            accounting_entity="ENT-GROUP",
            amount_total=240_000.0,
            status="draft",
            org="plant_north",
            sales_channel="enterprise_direct",
            risk_note="战略大客户首期交付回款已完成",
        ),
        actor=admin,
    )
    rt.put_object(DelCls(delivery_id="DEL-300", order="SO-300", status="pending"), actor=admin)
    rt.put_object(InvCls(invoice_id="INV-300", order="SO-300", amount_total=240_000.0, amount_residual=240_000.0, status="draft"), actor=admin)
    rt.submit_action("SEED-SO300-CONF", "confirm_o2c_order", {"order_id": "SO-300"}, sales_actor)
    rt.submit_action("SEED-SO300-DEL", "complete_o2c_delivery", {"delivery_id": "DEL-300"}, wh_actor)
    rt.submit_action("SEED-SO300-INV", "post_o2c_invoice", {"invoice_id": "INV-300"}, fin_actor)
    rt.submit_action("SEED-SO300-PAY", "record_o2c_payment", {"invoice_id": "INV-300", "payment_amount": 240_000.0}, fin_actor)

    rt.put_object(
        SoCls(
            order_id="SO-301",
            order_number="SO-2026-301（北方轨交大单·待发货）",
            customer="CUST-RAIL",
            accounting_entity="ENT-GROUP",
            amount_total=180_000.0,
            status="draft",
            org="plant_north",
            sales_channel="key_account",
            risk_note="未发货前严格禁开应收发票",
        ),
        actor=admin,
    )
    rt.put_object(DelCls(delivery_id="DEL-301", order="SO-301", status="pending"), actor=admin)
    rt.put_object(InvCls(invoice_id="INV-301", order="SO-301", amount_total=180_000.0, amount_residual=180_000.0, status="draft"), actor=admin)
    rt.submit_action("SEED-SO301-CONF", "confirm_o2c_order", {"order_id": "SO-301"}, sales_actor)

    rt.put_object(
        SoCls(
            order_id="SO-302",
            order_number="SO-2026-302（华南阀组·已开票待回款）",
            customer="CUST-EV",
            accounting_entity="ENT-GROUP",
            amount_total=150_000.0,
            status="draft",
            org="plant_south",
            sales_channel="partner_oem",
            risk_note="账期 NET30 待回款核销",
        ),
        actor=admin,
    )
    rt.put_object(DelCls(delivery_id="DEL-302", order="SO-302", status="pending"), actor=admin)
    rt.put_object(InvCls(invoice_id="INV-302", order="SO-302", amount_total=150_000.0, amount_residual=150_000.0, status="draft"), actor=admin)
    rt.submit_action("SEED-SO302-CONF", "confirm_o2c_order", {"order_id": "SO-302"}, admin)
    rt.submit_action("SEED-SO302-DEL", "complete_o2c_delivery", {"delivery_id": "DEL-302"}, admin)
    rt.submit_action("SEED-SO302-INV", "post_o2c_invoice", {"invoice_id": "INV-302"}, admin)

    # R2R Chart of Accounts + Open Fiscal Period + 8 Balanced Posted Entries + Draft Entries
    PerCls = spec.get_object_type("FiscalPeriod")
    AccCls = spec.get_object_type("GlAccount")
    JeCls = spec.get_object_type("JournalEntry")

    rt.put_object(
        PerCls(
            period_id="2026-11",
            accounting_entity="ENT-GROUP",
            start_date="2026-11-01",
            end_date="2026-11-30",
            status="open",
        ),
        actor=admin,
    )
    for code, name, cat, side in (
        ("1001", "银行存款", "ASSET", "DEBIT"),
        ("1122", "应收账款", "ASSET", "DEBIT"),
        ("1403", "原材料-钢卷", "ASSET", "DEBIT"),
        ("1404", "生产成本-WIP", "ASSET", "DEBIT"),
        ("1405", "库存商品-车架", "ASSET", "DEBIT"),
        ("2202", "应付账款", "LIABILITY", "CREDIT"),
        ("4001", "实收资本", "EQUITY", "CREDIT"),
        ("6001", "主营业务收入", "REVENUE", "CREDIT"),
        ("6401", "主营业务成本", "EXPENSE", "DEBIT"),
        ("6602", "制造维修及校准费用", "EXPENSE", "DEBIT"),
    ):
        rt.put_object(
            AccCls(
                account_code=code,
                accounting_entity="ENT-GROUP",
                name=name,
                category=cat,
                normal_side=side,
                status="active",
            ),
            actor=admin,
        )

    for idx, (eid, ev_id, dr, cr, cents, memo) in enumerate(
        (
            ("JE-01-CAP", "EVT-CAP-1", "1001", "4001", 50_000_000, "股东注资实收资本到位"),
            ("JE-02-P2P-RCV", "EVT-P2P-RCV", "1403", "2202", 9_500_000, "PO-100 鞍山特钢 95t 暂估入库"),
            ("JE-03-P2P-PAY", "EVT-P2P-PAY", "2202", "1001", 9_500_000, "付清特钢货款 BILL-100"),
            ("JE-04-P2M-ISS", "EVT-P2M-ISS", "1404", "1403", 8_000_000, "WO-200 压铸车架领用钢卷投料"),
            ("JE-05-P2M-FG", "EVT-P2M-FG", "1405", "1404", 8_000_000, "WO-200 50套车架完工入库"),
            ("JE-06-O2C-SHIP", "EVT-O2C-SHIP", "6401", "1405", 8_000_000, "SO-300 销售出库结转主营成本"),
            ("JE-07-O2C-INV", "EVT-O2C-INV", "1122", "6001", 24_000_000, "SO-300 销售开票确认主营收入"),
            ("JE-08-O2C-PAY", "EVT-O2C-PAY", "1001", "1122", 24_000_000, "SO-300 银行收到全额回款"),
        ),
        start=1,
    ):
        project_and_post_event_journal(
            rt,
            seed_actor=admin,
            post_actor=fin_actor,
            run_id=f"SEED-JE-{idx:02d}",
            entry_id=eid,
            accounting_entity="ENT-GROUP",
            fiscal_period="2026-11",
            source_event_id=ev_id,
            debit_account=dr,
            credit_account=cr,
            amount_cents=cents,
            memo=memo,
        )

    # Draft entries supporting WorkCard approval + Sandbox executive presets
    for eid, ev_id, dr, cr, cents, memo in (
        ("JE-TECH-50K", None, "6602", "1001", 5_000_000, "精密模具激光校准外包费（待CFO审批）"),
        ("JE-MAINT-20K", None, "6602", "1001", 2_000_000, "华北压铸线主轴突发抢修费"),
        ("JE-SO301-REV", "EVT-SO301-INV", "1122", "6001", 18_000_000, "SO-301 伺服阀组大单开票收入"),
        ("JE-SO301-CASH", "EVT-SO301-PAY", "1001", "1122", 18_000_000, "SO-301 伺服阀组银行回款"),
    ):
        rt.put_object(
            JeCls(
                entry_id=eid,
                accounting_entity="ENT-GROUP",
                fiscal_period="2026-11",
                source_event=ev_id,
                debit_account=dr,
                credit_account=cr,
                amount_cents=cents,
                memo=memo,
                status="draft",
            ),
            actor=admin,
        )

    # Seed 3 Live Pending WorkCards across CFO, Procurement Manager & North Plant Manager
    WcCls = spec.get_object_type("WorkCard")
    rt.put_object(
        WcCls(
            card_id="WC-JE-BONUS-50K",
            card_kind="APPROVAL",
            target_object_type="JournalEntry",
            target_object_id="JE-TECH-50K",
            requested_action="post_journal_entry",
            requested_params_json='{"entry_id": "JE-TECH-50K"}',
            requester_actor_id="act-fin",
            assignee_role="finance_director",
            assignee_dept="集团财务中心",
            status="OPEN",
            sla_due_at="2026-11-24 18:00",
            external_ref="精密模具校准费 ¥50,000（超手工凭证¥5,000上限，需CFO核准）",
            org="plant_north",
        ),
        actor=admin,
    )
    rt.put_object(
        WcCls(
            card_id="WC-PO-101-DISP",
            card_kind="HUMAN_TASK",
            target_object_type="P2pPurchaseOrder",
            target_object_id="PO-101",
            requested_action="resolve_p2p_dispute",
            requested_params_json='{"po_id": "PO-101", "receipt_id": "GR-101", "work_card_id": "WC-PO-101-DISP", "settled_amount": 95000.0}',
            requester_actor_id="act-wh",
            assignee_role="procurement_manager",
            assignee_dept="集团采购中心",
            status="OPEN",
            sla_due_at="2026-11-24 12:00",
            external_ref="华北特钢磅差争议（应收100t/实收95t，建议按 ¥95,000 核定结算）",
            org="plant_north",
        ),
        actor=admin,
    )
    rt.put_object(
        WcCls(
            card_id="WC-P2M-SCRAP-201",
            card_kind="APPROVAL",
            target_object_type="ProductionOutput",
            target_object_id="OUT-201-2",
            requested_action="record_production_output",
            requested_params_json='{"output_id": "OUT-201-2", "work_order_id": "WO-201", "output_resource": "RES-CHASSIS", "good_qty": 10.0, "scrap_qty": 5.0, "absorbed_cost_cents": 2000000}',
            requester_actor_id="act-north-sf",
            assignee_role="plant_manager",
            assignee_dept="华北智能压铸厂",
            status="OPEN",
            sla_due_at="2026-11-23 20:00",
            external_ref="WO-201 尾批损耗率达 9.1%（超BOM 5%上限），需华北厂长签批放行",
            org="plant_north",
        ),
        actor=admin,
    )

    # Record one genuine REJECTED redline attempt (unshipped INV-301) with Z-prefix so it stays prominent in top audit stream
    try:
        rt.submit_action("SEED-Z-RED-INV301", "post_o2c_invoice", {"invoice_id": "INV-301"}, fin_actor)
    except InvariantError:
        pass

    profiles = [
        DemoActorProfile(
            actor_id="act-cfo",
            display_name="陈总",
            role="finance_director",
            unit_id="DEPT-HQ",
            unit_name="集团总部",
            title="集团 CFO · 财务总监",
            badge="全集团视角 · 拥有大额财务与全部工作流核准权",
            token=DEMO_TOKEN_SECRET["act-cfo"],
            actor=cfo_actor,
        ),
        DemoActorProfile(
            actor_id="act-north-pm",
            display_name="张厂长",
            role="plant_manager",
            unit_id="DEPT-NORTH-MFG",
            unit_name="华北智能压铸厂",
            title="华北厂长",
            badge="仅限华北厂区视角（严格隔离华南厂区单据）",
            token=DEMO_TOKEN_SECRET["act-north-pm"],
        ),
        DemoActorProfile(
            actor_id="act-south-sf",
            display_name="钱师傅",
            role="shopfloor",
            unit_id="DEPT-SOUTH-MFG",
            unit_name="华南精密装配厂",
            title="华南车间装配主管",
            badge="仅限华南车间视角（严密隔离华北厂数据）",
            token=DEMO_TOKEN_SECRET["act-south-sf"],
        ),
        DemoActorProfile(
            actor_id="act-proc-mgr",
            display_name="郑经理",
            role="procurement_manager",
            unit_id="DEPT-PROC",
            unit_name="集团采购中心",
            title="采购经理",
            badge="负责供应商采购单与短磅争议核定",
            token=DEMO_TOKEN_SECRET["act-proc-mgr"],
        ),
        DemoActorProfile(
            actor_id="act-fin",
            display_name="韩会计",
            role="finance",
            unit_id="DEPT-FIN",
            unit_name="集团财务中心",
            title="总账会计",
            badge="负责总账凭证过账与发票收付款核销（大额须总监审批）",
            token=DEMO_TOKEN_SECRET["act-fin"],
        ),
        DemoActorProfile(
            actor_id="act-admin",
            display_name="实施管理员",
            role="admin",
            unit_id="DEPT-HQ",
            unit_name="系统运维中心",
            title="系统实施与运维管理员",
            badge="可访问 /admin 外设状态与自主爬坡控制",
            token=DEMO_TOKEN_SECRET["act-admin"],
            actor=admin,
        ),
    ]
    return directory, profiles


def build_standalone_enterprise_console(
    *,
    db_path: str = ":memory:",
    demo_actor_switch: bool = True,
) -> ConsoleService:
    spec = build_org_rls_enterprise_spec(
        overlays=[HUASHI_MFG_OVERLAY],
        rls_object_types=("P2pPurchaseOrder", "WorkOrder", "O2cOrder", "WorkCard"),
        model_id="std_enterprise_world_model",
    )
    _attach_standard_functions(spec)
    _merge_std_display(spec)

    rt = OntologyRuntime(spec, db_path=db_path, world_mode="standalone")
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    directory, profiles = _seed_standalone_enterprise_world(rt, admin)

    # Pre-warm shadow telemetry entries so admin telemetry board displays live history immediately
    shadow_policy = AutonomyPolicy(
        default_mode="SHADOW",
        default_escalation_action="open_work_card",
        default_escalation_assignee_role="finance_director",
        rules=(
            ActionAutonomyRule(
                action="*",
                mode="SHADOW",
                max_value=300_000.0,
                escalation_action="open_work_card",
                escalation_assignee_role="finance_director",
            ),
        ),
    )
    autonomy = AutonomyRampController(
        shadow_policy,
        escalation_payload_builder=_enterprise_escalation_builder,
    )
    cfo_actor = directory.resolve_actor("act-cfo")
    fin_actor = directory.resolve_actor("act-fin")
    autonomy.execute(rt, cfo_actor, "PRE-SHD-01", "post_journal_entry", {"entry_id": "JE-MAINT-20K"})
    autonomy.execute(rt, fin_actor, "PRE-SHD-02", "post_o2c_invoice", {"invoice_id": "INV-301"})

    # Default operational policy: AUTO_UNDER_CEILING (auto up to ¥300,000, escalate above or switchable in /admin)
    autonomy.policy = AutonomyPolicy(
        default_mode="AUTO_UNDER_CEILING",
        default_escalation_action="open_work_card",
        default_escalation_assignee_role="finance_director",
        rules=(
            ActionAutonomyRule(
                action="*",
                mode="AUTO_UNDER_CEILING",
                max_value=300_000.0,
                escalation_action="open_work_card",
                escalation_assignee_role="finance_director",
            ),
        ),
    )

    return ConsoleService(
        rt,
        default_actor=cfo_actor,
        org_directory=directory,
        autonomy_controller=autonomy,
        demo_actors=profiles,
        demo_actor_switch=demo_actor_switch,
        app_title="华实智能装备 · 企业数字孪生运营台",
        app_subtitle="六闭环标准世界模型（独立自治模式）",
        pack_hint="enterprise",
        work_queue_binding=ENTERPRISE_WORK_QUEUE,
        sandbox_presets=ENTERPRISE_SANDBOX_PRESETS,
        assistant_responder=enterprise_assistant_responder,
        escalation_action="open_work_card",
        escalation_assignee_role="finance_director",
        escalation_payload_builder=_enterprise_escalation_builder,
    )


def _build_erp_console(
    erp: str,
    *,
    db_path: str = ":memory:",
    demo_actor_switch: bool = True,
) -> ConsoleService:
    import os

    if erp == "odoo19":
        cfg = {
            "url": os.environ.get("ODOO_URL", "http://localhost:8069/jsonrpc"),
            "db": os.environ.get("ODOO_DB", "odoo_demo"),
            "username": os.environ.get("ODOO_USER", "admin"),
            "password": os.environ.get("ODOO_PASSWORD", "admin"),
        }
        mounted = mount_odoo19_enterprise_world(cfg, db_path=db_path, hydrate=True)
        subtitle = "Odoo 19 混合耦合模式（O2C/P2P/R2R 外挂 + CRM/P2M/H2R 本体自有）"
    elif erp == "jsherp":
        base = os.environ.get("JSHERP_BASE_URL", "http://127.0.0.1:19999/jshERP-boot").rstrip("/")
        cfg = {
            "base_url": base,
            "openapi_url": f"{base}/v2/api-docs",
            "login_url": f"{base}/user/login",
            "username": os.environ.get("JSHERP_USER", "jsh"),
            "password": os.environ.get("JSHERP_PASSWORD", "123456"),
        }
        mounted = mount_jsherp_enterprise_world(cfg, db_path=db_path, hydrate=True)
        subtitle = "华夏 jshERP 混合耦合模式（P2P/O2C/H2R 外挂 + CRM/P2M/R2R 本体自有）"
    else:
        raise ValueError(f"unsupported erp mode {erp!r}")

    _merge_std_display(mounted.spec)
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    profiles = [
        DemoActorProfile(
            actor_id="act-cfo",
            display_name="陈总",
            role="finance_director",
            title="集团财务总监 (CFO)",
            badge="全量耦合世界视角",
            clearance_tags=("*",),
            token=DEMO_TOKEN_SECRET["act-cfo"],
        ),
        DemoActorProfile(
            actor_id="act-admin",
            display_name="实施管理员",
            role="admin",
            title="系统实施管理员",
            badge="可调度外设连接与自主策略",
            clearance_tags=("*",),
            token=DEMO_TOKEN_SECRET["act-admin"],
            actor=admin,
        ),
    ]
    return ConsoleService(
        mounted.runtime,
        default_actor=profiles[0].to_actor(),
        demo_actors=profiles,
        demo_actor_switch=demo_actor_switch,
        app_title=f"企业数字孪生运营台 · {erp.upper()} 实景挂载",
        app_subtitle=subtitle,
        pack_hint="enterprise",
        work_queue_binding=ENTERPRISE_WORK_QUEUE,
        sandbox_presets=ENTERPRISE_SANDBOX_PRESETS,
        assistant_responder=enterprise_assistant_responder,
        escalation_action="open_work_card",
        escalation_assignee_role="finance_director",
        escalation_payload_builder=_enterprise_escalation_builder,
    )


def build_starship_console(*, demo_actor_switch: bool = True) -> ConsoleService:
    """Pure fictional M2 blind-test ontology proving zero enterprise assumptions in L1 UI."""

    class WarpCore(ObjectType):
        __primary_key__ = "core_id"
        __title__ = "label"
        core_id: str
        label: str
        output_gw: int = 120
        status: str = "active"

    class Starship(ObjectType):
        __primary_key__ = "ship_id"
        __title__ = "callsign"
        __aggregate_root__ = True
        __security_tag_fields__ = ("sector",)
        ship_id: str
        callsign: str
        sector: str = "alpha"
        fuel_cells: int = 8
        status: str = "active"
        core: ObjectProperty[WarpCore]

    class JumpSectorParams(BaseModel):
        ship_id: str
        target_sector: str
        cost_cells: int = 2

    class RefuelCoreParams(BaseModel):
        ship_id: str
        added_cells: int = 2

    class FleetFuelParams(BaseModel):
        label: str = "score"

    blind_spec = OntologySpec()
    blind_spec.role("navigator", "星舰领航员")
    blind_spec.role("commander", "舰队指挥官")
    blind_spec.role("admin", "舰载系统管理员")
    blind_spec.object_type(WarpCore)
    blind_spec.object_type(Starship)

    @blind_spec.action("jump_sector", JumpSectorParams, allowed_roles=("navigator", "commander", "admin"))
    def jump_sector(ctx: Any, params: JumpSectorParams) -> None:
        ship = ctx.require(Starship, params.ship_id)
        if ship.fuel_cells < params.cost_cells:
            raise InvariantError(
                f"Starship:{ship.ship_id} fuel_cells={ship.fuel_cells} < {params.cost_cells}",
                code="INSUFFICIENT_FUEL",
            )
        ctx.modify(
            ship,
            sector=params.target_sector,
            fuel_cells=ship.fuel_cells - params.cost_cells,
        )

    @blind_spec.action("refuel_ship", RefuelCoreParams, allowed_roles=("commander", "admin"))
    def refuel_ship(ctx: Any, params: RefuelCoreParams) -> None:
        ship = ctx.require(Starship, params.ship_id)
        if params.added_cells <= 0:
            raise InvariantError("added_cells must be positive", code="INVALID_FUEL_AMOUNT")
        ctx.modify(ship, fuel_cells=ship.fuel_cells + params.added_cells)

    @blind_spec.function("fleet_fuel_total", FleetFuelParams, description="计算当前可见舰队燃料总量与跃迁能量值")
    def fleet_fuel_total(ctx: Any, params: FleetFuelParams) -> dict[str, Any]:
        ships = list(ctx.query(Starship))
        return {
            "ship_count": len(ships),
            "total_fuel_cells": sum(int(s.fuel_cells) for s in ships),
            "total_value": sum(s.fuel_cells * 10 + (50 if s.sector == "omega" else 0) for s in ships),
        }

    blind_spec.update_display(
        {
            "ontologies": {"starship_blind": "星舰舰队跃迁本体"},
            "objects": {"Starship": "星际战舰", "WarpCore": "曲速核心"},
            "actions": {"jump_sector": "跃迁至目标星域", "refuel_ship": "燃料舱充能"},
            "functions": {"fleet_fuel_total": "舰队燃料汇总"},
            "fields": {
                "ship_id": "舰船编号",
                "callsign": "呼号",
                "sector": "所在星域",
                "fuel_cells": "燃料电池",
                "core_id": "核心编号",
                "label": "型号标签",
                "output_gw": "功率 (GW)",
            },
        }
    )

    rt = OntologyRuntime(blind_spec, db_path=":memory:", world_mode="standalone")
    adm = Actor(actor_id="cmd-kirk", role="admin", clearance_tags={"*"})
    rt.put_object(WarpCore(core_id="WC-1", label="Mark-IV 曲速核", output_gw=180), actor=adm)
    rt.put_object(WarpCore(core_id="WC-2", label="Mark-V 脉冲核", output_gw=240), actor=adm)
    rt.put_object(Starship(ship_id="SS-01", callsign="Enterprise", sector="alpha", fuel_cells=8, core="WC-1"), actor=adm)
    rt.put_object(Starship(ship_id="SS-02", callsign="Voyager", sector="beta", fuel_cells=6, core="WC-2"), actor=adm)

    profiles = [
        DemoActorProfile(
            actor_id="cmd-kirk",
            display_name="舰队指挥官",
            role="admin",
            title="全星域指挥官",
            clearance_tags=("*",),
            token="tok-starship-cmd",
            actor=adm,
        ),
        DemoActorProfile(
            actor_id="nav-alpha",
            display_name="Alpha 领航员",
            role="navigator",
            title="Alpha 星域领航员",
            clearance_tags=("sector:alpha",),
            token="tok-starship-alpha",
        ),
    ]
    presets = [
        {
            "preset_id": "starship_jump_omega",
            "title": "沙盘演练：Enterprise 跃迁至 Omega 星域",
            "subtitle": "消耗 2 格燃料细胞执行安全跃迁，验证主星图位置不变",
            "badge": "星舰盲测场景",
            "steps": [
                {
                    "action": "jump_sector",
                    "params": {"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 2},
                }
            ],
        }
    ]
    return ConsoleService(
        rt,
        default_actor=adm,
        demo_actors=profiles,
        demo_actor_switch=demo_actor_switch,
        app_title="OpenOntology 通用 M2 盲测控制台",
        app_subtitle="Starship / WarpCore 领域无关验证模式",
        pack_hint="blind",
        sandbox_presets=presets,
    )


def build_console_service(
    *,
    mode: str = "standalone",
    db_path: str = ":memory:",
    demo_actor_switch: bool = True,
) -> ConsoleService:
    cleaned = str(mode or "standalone").strip().lower()
    if cleaned == "standalone":
        return build_standalone_enterprise_console(
            db_path=db_path, demo_actor_switch=demo_actor_switch
        )
    if cleaned in ("odoo19", "jsherp"):
        return _build_erp_console(
            cleaned, db_path=db_path, demo_actor_switch=demo_actor_switch
        )
    if cleaned == "starship":
        return build_starship_console(demo_actor_switch=demo_actor_switch)
    raise ValueError(f"unsupported console mode {mode!r}; expected standalone|odoo19|jsherp|starship")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch OpenOntology Enterprise Digital Twin Console")
    parser.add_argument(
        "--mode",
        choices=("standalone", "odoo19", "jsherp", "starship"),
        default="standalone",
        help="Runtime world mounting mode (default: standalone)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8788, help="Bind port (default: 8788)")
    parser.add_argument("--db-path", default=":memory:", help="SQLite DB path (default: :memory:)")
    args = parser.parse_args(argv)

    import uvicorn

    svc = build_console_service(mode=args.mode, db_path=args.db_path, demo_actor_switch=True)
    app = create_console_app(svc)
    print(f"[WO-26] Starting Enterprise Console ({args.mode}) at http://{args.host}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        svc.close()
        svc.runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
