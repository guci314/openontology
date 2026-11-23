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
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CustomerOverlaySpec,
    FieldSpec,
    InvariantRuleSpec,
)
from open_ontology.org_authority import OrgDirectory, hash_auth_token

from examples.adapters.erp_world_mount import (
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
    add_actions=[
        ActionBlueprint(
            name="annotate_order_risk_note",
            display_label="标注销售订单风控备注",
            description="声明式规则动作：仅更新企业自有扩展风控备注，不写回外部主业务系统。",
            allowed_roles=["sales_rep", "sales_manager", "finance_director", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="order_id", field_type="str", required=True),
                FieldSpec(name="risk_note", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="order_not_cancelled_for_risk_note",
                    code="ORDER_CANCELLED_NO_RISK_NOTE",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "ne",
                        "left": {"ref": "obj.status"},
                        "right": {"lit": "cancelled"},
                    },
                    description="已取消订单不再追加风控备注。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="O2cOrder",
                    id_param="order_id",
                    set_fields={"risk_note": {"ref": "param.risk_note"}},
                ),
            ],
        ),
        ActionBlueprint(
            name="upgrade_account_vip_tier",
            display_label="调级客户战略等级",
            description="声明式规则动作：在合规等级枚举内调整客户战略分级。",
            allowed_roles=["sales_manager", "finance_director", "admin"],
            affects_source=False,
            params=[
                FieldSpec(name="account_id", field_type="str", required=True),
                FieldSpec(name="vip_tier", field_type="str", required=True),
            ],
            preconditions=[
                InvariantRuleSpec(
                    name="valid_vip_tier_enum",
                    code="INVALID_VIP_TIER",
                    target_type="CrmAccount",
                    id_param="account_id",
                    must_hold={
                        "op": "in",
                        "left": {"ref": "param.vip_tier"},
                        "right": {"lit": ["STANDARD", "VIP_KEY", "STRATEGIC"]},
                    },
                    description="客户战略等级必须为 STANDARD / VIP_KEY / STRATEGIC。",
                ),
            ],
            mutations=[
                ActionMutationSpec(
                    op="modify",
                    object_type="CrmAccount",
                    id_param="account_id",
                    set_fields={"vip_tier": {"ref": "param.vip_tier"}},
                ),
            ],
        ),
    ],
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

ENTERPRISE_QUICK_PROMPTS: list[str] = [
    "现在有哪些待我审批的异常或工单？",
    "当前在手订单、应收和现金余额情况如何？",
    "如果突发 ¥20,000 维修费对利润有什么影响？",
    "我当前能看到哪些厂区的单据？",
]

ENTERPRISE_SANDBOX_PRESETS: list[dict[str, Any]] = [
    {
        "preset_id": "emergency_repair_20k",
        "title": "突发 ¥20,000 维修费测算",
        "subtitle": "在平行沙盘中过账华北压铸线突发维修费用凭证，实时评估银行现金、费用利润与试算平衡。",
        "badge": "财务压力测试",
        "required_clearance": ["org:hq_fin"],
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
        "required_clearance": ["org:hq_fin", "org:plant_north"],
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
        "allowed_roles": ["procurement_manager", "finance_director", "steward", "admin"],
        "required_clearance": ["org:plant_north"],
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
            "actions": {
                "annotate_order_risk_note": "标注销售订单风控备注",
                "upgrade_account_vip_tier": "调级客户战略等级",
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
                "order_id": "销售订单编号",
                "customer": "客户档案",
                "account": "客户账户",
                "supplier": "供应商档案",
                "purchase_order": "关联采购单",
                "po_id": "采购订单编号",
                "receipt_id": "收货验收单号",
                "bill_id": "应付账单编号",
                "delivery_id": "发货履约单号",
                "invoice_id": "应收发票编号",
                "entry_id": "总账凭证编号",
                "work_order": "关联生产工单",
                "work_order_id": "生产工单编号",
                "work_card_id": "协同工单号",
                "deal_id": "销售商机编号",
                "ticket_id": "客户工单编号",
                "bom": "BOM 配方",
                "good_qty": "良品数量 (件)",
                "scrap_qty": "报废数量 (件)",
                "absorbed_cost_cents": "吸收成本（分）",
                "issued_qty": "已领料数量",
                "issued_cost_cents": "已投入成本（分）",
                "completed_good_qty": "已完工良品 (件)",
                "completed_scrap_qty": "已报废数量 (件)",
                "planned_qty": "计划产量 (件)",
                "ordered_qty": "订购数量 (吨)",
                "received_qty": "实收数量 (吨)",
                "expected_qty": "应收数量 (吨)",
                "inspection_status": "质检结果",
                "qty": "领料数量",
                "unit_cost_cents": "单位成本（分）",
                "total_cost_cents": "总成本（分）",
                "allocation_rule": "成本分摊策略",
                "source_event": "来源履约凭证",
                "output_event": "产出履约凭证",
                "settled_amount": "核定结算金额",
                "payment_amount": "实收/实付金额",
                "resolution_summary": "处理总结",
                "reversal_entry_id": "冲正凭证编号",
                "target_period_id": "冲正会计期间",
                "memo": "凭证摘要",
                "received_qty_delta": "实收偏差",
            },
            "field_overrides": {
                "WorkOrder.planned_qty": "计划产量 (件)",
                "WorkOrder.completed_good_qty": "已完工良品 (件)",
                "WorkOrder.completed_scrap_qty": "已报废数量 (件)",
                "P2pPurchaseOrder.ordered_qty": "订购数量 (吨)",
                "P2pGoodsReceipt.expected_qty": "应收数量 (吨)",
                "P2pGoodsReceipt.received_qty": "实收数量 (吨)",
            },
            "statuses": {
                "enterprise_direct": "企业直销",
                "key_account": "大客户直签",
                "partner_oem": "OEM 伙伴",
                "STRATEGIC": "战略大客户",
                "VIP_KEY": "重点核心客户",
                "STANDARD": "标准客户",
                "HIGH": "高优",
                "MEDIUM": "中等",
                "LOW": "常规",
                "ASSET": "资产类",
                "LIABILITY": "负债类",
                "EQUITY": "所有者权益",
                "REVENUE": "营业收入",
                "EXPENSE": "成本费用",
                "DEBIT": "借方",
                "CREDIT": "贷方",
                "APPROVAL": "高危放行审批",
                "HUMAN_TASK": "人工裁决协同",
                "JOURNAL_POST": "总账入账凭证",
                "PRODUCTION_OUTPUT": "完工入库事实",
                "PRODUCTION_ISSUE": "生产领料事实",
                "RECEIPT": "采购收货事实",
                "SHIPMENT": "销售出库事实",
                "INVOICE": "开票确认事实",
                "PAYMENT": "资金收付事实",
                "EXACT_LOT": "按批次实价",
                "FIFO": "先进先出",
                "recorded": "已记录",
                "agreed": "已生效",
                "pending": "待处理",
                "passed": "检验合格",
                "plant_north": "华北厂",
                "plant_south": "华南厂",
                "hq_fin": "集团财务",
                "hq": "集团总部",
                "draft": "草稿",
                "confirmed": "已确认",
                "released": "已下达",
                "in_progress": "进行中",
                "completed": "已完成",
                "cancelled": "已取消",
                "disputed": "争议挂起",
                "received": "已收货待账",
                "billed": "已对账开票",
                "delivered": "已发货",
                "invoiced": "已开票",
                "posted": "已过账",
                "reversed": "已冲正",
                "paid": "已结清",
                "partial": "部分结清",
                "not_paid": "待核销",
                "open": "开放记账中",
                "closing": "结账中",
                "closed": "已关账锁定",
                "offboarded": "已离职",
                "onboarding": "待入职",
                "resolved": "已闭环",
                "RESOLVED": "已闭环",
                "CLOSED_WON": "已赢单",
                "PROPOSAL": "方案报价",
                "NEGOTIATION": "商务谈判",
            },
            "errors": {
                "ILLEGAL_DOCUMENT_TRANSITION": "当前单据状态不允许直接跃迁到目标状态。",
                "COST_CONSERVATION_VIOLATION": "违反成本守恒铁律：核销吸收成本不得超过已投入物料总成本。",
                "SELF_BALANCING_ACCOUNT_FORBIDDEN": "违反复式记账规范：借方与贷方不得为同一科目。",
                "PERIOD_CLOSED_POSTING_FORBIDDEN": "该会计期间已关账，严禁补记或修改凭证。",
                "PO_DISPUTED_BLOCKED": "关联采购单处于争议挂起状态，须先完成差异裁决后方可过账。",
                "ORDER_NOT_BILLABLE": "订单当前阶段尚不满足开票条件，请先完成履约发货。",
                "DELIVERY_REQUIRED_BEFORE_INVOICE": "红线拦截：未完成发货出库前严禁提前开具客户发票。",
                "RECEIPT_REQUIRED_BEFORE_BILL": "红线拦截：未完成收货验收前严禁过账应付账单。",
                "OPEN_HIGH_PRIORITY_TICKET_BLOCKS_WIN": "客户尚有未关闭的高优先级客诉，须先闭环客诉才可赢单。",
                "APPROVAL_REQUIRED_MANUAL_JOURNAL": "触发大额手工调账红线：需财务总监签批复核后方可过账。",
                "APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED": "损耗率超出标准上限，已转交厂长签批放行。",
            },
            "invariants": {
                "declarative_fsm_transitions": "单据状态流转必须严格遵循系统预设状态机跃迁路径。",
                "process_document_state_transitions": "业务作业单据的初始状态、流转路线与作废前置状态必须符合流程规范。",
                "business_non_negative_fields": "台账余额、库存、数量与应收/应付未清金额均不得为负。",
                "p2m_work_order_cost_conservation": "生产工单累计产出吸收成本不得超过已领料投入总成本（成本守恒铁律）。",
                "p2m_bom_positive_ratio": "BOM 配方单位耗量必须大于 0，最大允许损耗率须处于 0%~100% 之间。",
                "r2r_gl_account_valid_enums": "总账科目类别与余额方向必须符合标准复式会计准则枚举。",
                "r2r_journal_no_self_balancing": "复式记账凭证借方科目与贷方科目不得为同一科目。",
                "voucher_backed_balance_conservation": "资金或存量结存余额变化必须与同事务凭证增量精确借贷配平。",
                "work_card_segregation_of_duties": "职责分离（SoD）：禁止审批工单申请人自批自结自己提交的事项。",
                "work_card_unique_active_intent": "同一目标单据的同类协同审批工单不得重复发起。",
                "h2r_department_dag_acyclic": "组织部门层级必须保持无环树状结构，禁止循环指向。",
                "h2r_job_position_closed_role": "岗位对应角色与授权角色均须在合法标准角色清单内。",
                "h2r_mandate_no_self_grant": "职责分离（SoD）：动态授权的授权人与被授权人不得为同一人。",
                "h2r_no_active_mandate_for_offboarded": "已离职员工名下不得保留任何生效中的审批授权。",
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
    display = context.get("display") or {}
    obj_labels = display.get("objects") or {}
    role_labels = display.get("roles") or {}
    act_labels = display.get("actions") or {}
    status_labels = display.get("statuses") or {}
    visible_presets = {
        str(p.get("preset_id") or ""): p for p in (context.get("visible_presets") or ())
    }

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
    gl_accounts = list(runtime.query_objects("GlAccount", actor))
    has_gl_access = len(gl_accounts) > 0

    try:
        tb = runtime.call_function("compute_trial_balance", {"accounting_entity": "ENT-GROUP"}, actor)
    except Exception:
        tb = {}

    bank_cash_yuan = 0.0
    for acc in gl_accounts:
        if acc.account_code == "1001":
            bank_cash_yuan = (acc.debit_total_cents - acc.credit_total_cents) / 100.0
            break
    ar_residual = sum(
        float(i.amount_residual) for i in invs if i.status == "posted"
    )
    so_total = sum(float(o.amount_total) for o in sos if o.status != "cancelled")

    if any(kw in q for kw in ("待办", "审批", "工单", "短磅", "异常", "card", "work")):
        for card in open_cards[:4]:
            t_label = obj_labels.get(card.target_object_type) or card.target_object_type
            r_label = role_labels.get(card.assignee_role) or card.assignee_role
            s_label = status_labels.get(card.status) or card.status
            cards.append(
                {
                    "kind": "object",
                    "title": f"{card.card_id} · {t_label} {card.target_object_id}",
                    "subtitle": f"指派岗位：{r_label} | 状态：{s_label}",
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
                    f"{c.card_id}（{obj_labels.get(c.target_object_type) or c.target_object_type} {c.target_object_id} → 需 {role_labels.get(c.assignee_role) or c.assignee_role}签批）"
                    for c in open_cards[:3]
                )
                if open_cards
                else "当前待办队列已全部清零。"
            )
        )
        return {"reply": reply, "highlights": highlights, "cards": cards}

    if any(kw in q for kw in ("维修", "沙盘", "推演", "模拟", "假设", "如果", "what", "20000", "20,000")):
        if not has_gl_access or "emergency_repair_20k" not in visible_presets:
            highlights = [
                {"label": "总账测算权限", "value": "仅 CFO/财务可见"},
                {"label": "可见生产工单", "value": f"{len(wos)} 单"},
                {"label": "推演对真实库影响", "value": "0 改动"},
            ]
            reply = (
                "突发维修费用总账测算涉及全集团总账与银行现金科目（仅 CFO 及财务人员可见）。"
                "您当前可在「平行沙盘推演」页使用「手动单步假设试演」对本厂区单据进行零脏写合规验证。"
            )
            return {"reply": reply, "highlights": highlights, "cards": cards}

        cards.append(
            {
                "kind": "sandbox_preset",
                "title": "立即前往平行沙盘：突发 ¥20,000 维修费测算",
                "subtitle": "零影响分叉测试对现金、利润及总账试算平衡的精确影响",
                "preset_id": "emergency_repair_20k",
                "route": "/sandbox",
            }
        )
        if "large_order_invoice_drill" in visible_presets:
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
            org_code = str(getattr(wo, "org", "") or "")
            org_zh = status_labels.get(org_code) or org_code or "集团"
            st_zh = status_labels.get(wo.status) or wo.status
            cards.append(
                {
                    "kind": "object",
                    "title": f"生产工单 {wo.work_order_id}（{getattr(wo, 'production_line', '产线')}）",
                    "subtitle": f"所属厂区：{org_zh} | 状态：{st_zh}",
                    "object_type": "WorkOrder",
                    "object_id": wo.work_order_id,
                }
            )
        orgs = sorted({str(getattr(x, "org", "")) for x in (*pos, *wos, *sos) if getattr(x, "org", "")})
        orgs_zh = [status_labels.get(o) or o for o in orgs]
        role_zh = role_labels.get(actor.role) or actor.role
        highlights = [
            {"label": "可见厂区范围", "value": "、".join(orgs_zh) or "总部"},
            {"label": "可见生产工单", "value": f"{len(wos)} 单"},
            {"label": "可见销售订单", "value": f"{len(sos)} 单"},
        ]
        reply = (
            f"系统按您的岗位角色「{role_zh}」严格执行厂区与子账行级隔离。"
            f"当前可见采购单 {len(pos)} 笔、生产工单 {len(wos)} 笔、销售订单 {len(sos)} 笔（所属厂区：{'、'.join(orgs_zh) or '总部'}）。"
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
            act_name = str(latest.get("action_type") or "")
            act_zh = act_labels.get(act_name) or act_name
            hint = str(latest.get("hint_zh") or "").strip()
            if not hint:
                from open_ontology.console_server import _translate_error_hint

                hint = _translate_error_hint(latest.get("error_code"), latest.get("error_message"))
            reply = (
                f"您权限范围内最近一次拦截发生在动作「{act_zh}」"
                + (f"（{hint}）" if hint else "。")
                + "所有违规写入均在进入事务前被内核阻断，外部系统与主台账保持零脏写。"
            )
        else:
            reply = "当前您权限范围内的审计账本尚未记录违规拦截事件，系统运行正常。"
        return {"reply": reply, "highlights": highlights, "cards": cards}

    # Default comprehensive executive brief scoped to current actor's RLS view
    balanced_text = "平衡绿灯" if tb.get("is_balanced", True) else "待核查"
    cash_str = f"¥{bank_cash_yuan:,.0f}" if has_gl_access else "仅 CFO/财务可见"
    highlights = [
        {"label": "可见在手订单总额", "value": f"¥{so_total:,.0f}"},
        {"label": "应收未收余额", "value": f"¥{ar_residual:,.0f}"},
        {"label": "银行存款余额", "value": cash_str},
        {"label": "待处理审批", "value": f"{len(open_cards)} 项"},
    ]
    for wo in wos[:2]:
        org_zh = status_labels.get(str(getattr(wo, "org", "") or "")) or "集团"
        cards.append(
            {
                "kind": "object",
                "title": f"生产工单 {wo.work_order_id}",
                "subtitle": f"厂区：{org_zh} · 计划 {wo.planned_qty:.0f} 件",
                "object_type": "WorkOrder",
                "object_id": wo.work_order_id,
            }
        )
    for po in pos[:2]:
        st_zh = status_labels.get(po.status) or po.status
        cards.append(
            {
                "kind": "object",
                "title": f"采购单 {po.po_number}",
                "subtitle": f"金额 ¥{po.amount_total:,.0f} · 状态 {st_zh}",
                "object_type": "P2pPurchaseOrder",
                "object_id": po.po_id,
            }
        )
    if has_gl_access:
        reply = (
            f"截至目前，您权限范围内共有在手销售订单 ¥{so_total:,.0f}、待收应收账款 ¥{ar_residual:,.0f}、"
            f"银行现金 ¥{bank_cash_yuan:,.0f}，总账试算状态为【{balanced_text}】。"
            f"待办收件箱有 {len(open_cards)} 项异常或高额审批等待处理。"
        )
    else:
        reply = (
            f"截至目前，您当前厂区视角内共有在手销售订单 ¥{so_total:,.0f}、待收应收账款 ¥{ar_residual:,.0f}、"
            f"可见生产工单 {len(wos)} 笔（集团总账银行现金余额仅 CFO/财务可见）。"
            f"待办收件箱有 {len(open_cards)} 项审批事项等待处理。"
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
        ("DEPT-FIN", "集团财务中心", "DEPT-HQ", "org:hq_fin", "active"),
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
    rt.put_object(GrCls(receipt_id="GR-100", purchase_order="PO-100", expected_qty=100.0, status="pending", org="plant_north"), actor=admin)
    rt.put_object(BillCls(bill_id="BILL-100", purchase_order="PO-100", amount_total=100_000.0, amount_residual=100_000.0, status="draft", org="plant_north"), actor=admin)
    rt.submit_action("SEED-PO100-CONF", "confirm_p2p_order", {"po_id": "PO-100"}, proc_actor)
    rt.submit_action("SEED-PO100-RCV", "record_p2p_receipt", {"receipt_id": "GR-100", "received_qty": 95.0, "inspection_status": "passed"}, wh_actor)
    WcCls = spec.get_object_type("WorkCard")
    rt.put_object(
        WcCls(
            card_id="WC-PO-100-HIST",
            card_kind="HUMAN_TASK",
            target_object_type="P2pPurchaseOrder",
            target_object_id="PO-100",
            requested_action="resolve_p2p_dispute",
            requester_actor_id="act-wh",
            assignee_role="procurement_manager",
            status="OPEN",
            org="plant_north",
        ),
        actor=admin,
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
    rt.put_object(GrCls(receipt_id="GR-101", purchase_order="PO-101", expected_qty=100.0, status="pending", org="plant_north"), actor=admin)
    rt.put_object(BillCls(bill_id="BILL-101", purchase_order="PO-101", amount_total=100_000.0, amount_residual=100_000.0, status="draft", org="plant_north"), actor=admin)
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
    rt.put_object(GrCls(receipt_id="GR-102", purchase_order="PO-102", expected_qty=50.0, status="pending", org="plant_south"), actor=admin)
    rt.put_object(BillCls(bill_id="BILL-102", purchase_order="PO-102", amount_total=60_000.0, amount_residual=60_000.0, status="draft", org="plant_south"), actor=admin)
    rt.submit_action("SEED-PO102-CONF", "confirm_p2p_order", {"po_id": "PO-102"}, proc_actor)

    # Confirmed events strictly aligned with settled / invoiced reality (no premature unshipped SO-301 events)
    for eid, cid, kind, qty, amt, org_tag in (
        ("EVT-CAP-1", "CM-SALE-EV", "JOURNAL_POST", 1.0, 500_000.0, "hq_fin"),
        ("EVT-P2P-RCV", "CM-PO-STEEL", "RECEIPT", 95.0, 95_000.0, "plant_north"),
        ("EVT-P2P-PAY", "CM-PO-STEEL", "PAYMENT", 1.0, 95_000.0, "plant_north"),
        ("EVT-P2M-ISS", "CM-SALE-EV", "PRODUCTION_ISSUE", 80.0, 80_000.0, "plant_north"),
        ("EVT-P2M-FG", "CM-SALE-EV", "PRODUCTION_OUTPUT", 50.0, 80_000.0, "plant_north"),
        ("EVT-O2C-SHIP", "CM-SALE-EV", "SHIPMENT", 50.0, 80_000.0, "plant_north"),
        ("EVT-O2C-INV", "CM-SALE-EV", "INVOICE", 50.0, 240_000.0, "plant_north"),
        ("EVT-O2C-PAY", "CM-SALE-EV", "PAYMENT", 1.0, 240_000.0, "plant_north"),
        ("EVT-SO302-INV", "CM-SALE-SOUTH", "INVOICE", 25.0, 150_000.0, "plant_south"),
    ):
        rt.put_object(
            EvtCls(
                event_id=eid,
                commitment=cid,
                event_kind=kind,
                quantity=qty,
                amount=amt,
                status="confirmed",
                org=org_tag,
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
    rt.put_object(DelCls(delivery_id="DEL-300", order="SO-300", status="pending", org="plant_north"), actor=admin)
    rt.put_object(InvCls(invoice_id="INV-300", order="SO-300", amount_total=240_000.0, amount_residual=240_000.0, status="draft", org="plant_north"), actor=admin)
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
    rt.put_object(DelCls(delivery_id="DEL-301", order="SO-301", status="pending", org="plant_north"), actor=admin)
    rt.put_object(InvCls(invoice_id="INV-301", order="SO-301", amount_total=180_000.0, amount_residual=180_000.0, status="draft", org="plant_north"), actor=admin)
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
    rt.put_object(DelCls(delivery_id="DEL-302", order="SO-302", status="pending", org="plant_south"), actor=admin)
    rt.put_object(InvCls(invoice_id="INV-302", order="SO-302", amount_total=150_000.0, amount_residual=150_000.0, status="draft", org="plant_south"), actor=admin)
    rt.submit_action("SEED-SO302-CONF", "confirm_o2c_order", {"order_id": "SO-302"}, admin)
    rt.submit_action("SEED-SO302-DEL", "complete_o2c_delivery", {"delivery_id": "DEL-302"}, admin)
    rt.submit_action("SEED-SO302-INV", "post_o2c_invoice", {"invoice_id": "INV-302"}, admin)

    # R2R Chart of Accounts (hq_fin RLS) + Open Fiscal Period + 9 Balanced Posted Entries + Draft Entries
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
                org="hq_fin",
            ),
            actor=admin,
        )

    for idx, (eid, ev_id, dr, cr, cents, memo, org_tag) in enumerate(
        (
            ("JE-01-CAP", "EVT-CAP-1", "1001", "4001", 50_000_000, "股东注资实收资本到位", "hq_fin"),
            ("JE-02-P2P-RCV", "EVT-P2P-RCV", "1403", "2202", 9_500_000, "PO-100 鞍山特钢 95t 暂估入库", "plant_north"),
            ("JE-03-P2P-PAY", "EVT-P2P-PAY", "2202", "1001", 9_500_000, "付清特钢货款 BILL-100", "plant_north"),
            ("JE-04-P2M-ISS", "EVT-P2M-ISS", "1404", "1403", 8_000_000, "WO-200 压铸车架领用钢卷投料", "plant_north"),
            ("JE-05-P2M-FG", "EVT-P2M-FG", "1405", "1404", 8_000_000, "WO-200 50套车架完工入库", "plant_north"),
            ("JE-06-O2C-SHIP", "EVT-O2C-SHIP", "6401", "1405", 8_000_000, "SO-300 销售出库结转主营成本", "plant_north"),
            ("JE-07-O2C-INV", "EVT-O2C-INV", "1122", "6001", 24_000_000, "SO-300 销售开票确认主营收入", "plant_north"),
            ("JE-08-O2C-PAY", "EVT-O2C-PAY", "1001", "1122", 24_000_000, "SO-300 银行收到全额回款", "plant_north"),
            ("JE-09-SO302-INV", "EVT-SO302-INV", "1122", "6001", 15_000_000, "SO-302 华南阀组开票确认应收账款", "plant_south"),
        ),
        start=1,
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
                org=org_tag,
            ),
            actor=admin,
        )
        rt.submit_action(
            f"SEED-JE-{idx:02d}",
            "post_journal_entry",
            {"entry_id": eid},
            fin_actor,
        )

    # Draft entries supporting WorkCard approval + Sandbox executive presets (strictly source-free until fulfilled)
    for eid, ev_id, dr, cr, cents, memo, org_tag in (
        ("JE-TECH-50K", None, "6602", "1001", 5_000_000, "精密模具激光校准外包费（待CFO审批）", "hq_fin"),
        ("JE-MAINT-20K", None, "6602", "1001", 2_000_000, "华北压铸线主轴突发抢修费", "plant_north"),
        ("JE-SO301-REV", None, "1122", "6001", 18_000_000, "SO-301 伺服阀组大单开票收入（沙盘演练）", "plant_north"),
        ("JE-SO301-CASH", None, "1001", "1122", 18_000_000, "SO-301 伺服阀组银行回款（沙盘演练）", "plant_north"),
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
                org=org_tag,
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
            badge="全集团视角 · 拥有财务大额核准与全局经营洞察权限",
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
        rls_object_types=(
            "P2pPurchaseOrder",
            "P2pGoodsReceipt",
            "P2pVendorBill",
            "WorkOrder",
            "O2cOrder",
            "O2cDelivery",
            "O2cInvoice",
            "WorkCard",
            "JournalEntry",
            "GlAccount",
            "FulfillmentEvent",
        ),
        model_id="std_enterprise_world_model",
    )
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
        app_subtitle="五闭环业务运营 + 组织权限治理（独立自治模式）",
        pack_hint="enterprise",
        work_queue_binding=ENTERPRISE_WORK_QUEUE,
        sandbox_presets=ENTERPRISE_SANDBOX_PRESETS,
        quick_prompts=ENTERPRISE_QUICK_PROMPTS,
        assistant_welcome="您好 {display_name}！我是运行在「{title}」权限视图内的 AI 数字经营管家，可实时为您检索待批异常、销售应收指标与平行沙盘预演。",
        value_unit_label="¥",
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
        quick_prompts=ENTERPRISE_QUICK_PROMPTS,
        assistant_welcome="您好 {display_name}！我是运行在「{title}」权限视图内的 AI 数字经营管家，可实时为您检索待批异常、外设挂载状态与平行沙盘预演。",
        value_unit_label="¥",
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
        __status_field__ = "status"
        __initial_statuses__ = ("active", "docked")
        __transitions__ = {
            "docked": ("active", "retired"),
            "active": ("docked", "retired"),
            "retired": (),
        }
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

    @blind_spec.edit_function("jump_sector", JumpSectorParams)
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

    blind_spec.declare_action(
        "jump_sector",
        edit_function="jump_sector",
        allowed_roles=("navigator", "commander", "admin"),
        touched_object_types=("Starship",),
    )

    @blind_spec.edit_function("refuel_ship", RefuelCoreParams)
    def refuel_ship(ctx: Any, params: RefuelCoreParams) -> None:
        ship = ctx.require(Starship, params.ship_id)
        if params.added_cells <= 0:
            raise InvariantError("added_cells must be positive", code="INVALID_FUEL_AMOUNT")
        ctx.modify(ship, fuel_cells=ship.fuel_cells + params.added_cells)

    blind_spec.declare_action(
        "refuel_ship",
        edit_function="refuel_ship",
        allowed_roles=("commander", "admin"),
        touched_object_types=("Starship",),
    )

    @blind_spec.function("fleet_fuel_total", FleetFuelParams, description="计算当前可见舰队燃料总量与跃迁能量值")
    def fleet_fuel_total(ctx: Any, params: FleetFuelParams) -> dict[str, Any]:
        ships = list(ctx.query(Starship))
        return {
            "ship_count": len(ships),
            "total_fuel_cells": sum(int(s.fuel_cells) for s in ships),
            "total_value": sum(s.fuel_cells * 10 + (50 if s.sector == "omega" else 0) for s in ships),
        }

    @blind_spec.invariant(
        "starship_non_negative_fuel",
        code="NEGATIVE_FUEL",
        description="星际战舰燃料电池储备与曲速核心功率严禁为负。",
    )
    def starship_non_negative_fuel(view: Any) -> bool:
        for s in view.query(Starship):
            if int(s.fuel_cells) < 0:
                raise InvariantError(
                    f"Starship:{s.ship_id} fuel_cells={s.fuel_cells} < 0",
                    code="NEGATIVE_FUEL",
                )
        for core in view.query(WarpCore):
            if int(core.output_gw) < 0:
                raise InvariantError(
                    f"WarpCore:{core.core_id} output_gw={core.output_gw} < 0",
                    code="NEGATIVE_FUEL",
                )
        return True

    blind_spec.update_display(
        {
            "ontologies": {"starship_blind": "星舰舰队跃迁本体"},
            "objects": {"Starship": "星际战舰", "WarpCore": "曲速核心"},
            "actions": {"jump_sector": "跃迁至目标星域", "refuel_ship": "燃料舱充能"},
            "functions": {"fleet_fuel_total": "舰队燃料汇总"},
            "roles": {
                "navigator": "星舰领航员",
                "commander": "舰队指挥官",
                "admin": "舰载系统管理员",
            },
            "fields": {
                "ship_id": "舰船编号",
                "callsign": "呼号",
                "sector": "所在星域",
                "target_sector": "目标星域",
                "fuel_cells": "燃料电池",
                "cost_cells": "消耗燃料电池数",
                "added_cells": "充能燃料格数",
                "core": "关联曲速核心",
                "core_id": "核心编号",
                "label": "型号标签",
                "output_gw": "功率 (GW)",
                "status": "舰船状态",
            },
            "statuses": {
                "alpha": "Alpha 星域",
                "beta": "Beta 星域",
                "omega": "Omega 星域",
                "gamma": "Gamma 星域",
                "docked": "停泊待命",
                "retired": "已退役",
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
    starship_prompts = [
        "当前可见星舰与曲速核心状态如何？",
        "我当前的星域权限范围是什么？",
        "最近有哪些跃迁拦截或审计记录？",
        "查看 Enterprise 号战舰详情",
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
        quick_prompts=starship_prompts,
        assistant_welcome="您好 {display_name}！我是运行在「{title}」权限视角内的星舰指挥终端智能助手，可为您检索可见战舰、燃料指标与星域跃迁沙盘推演。",
        value_unit_label="",
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
