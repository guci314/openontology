"""Ontology Builder 实战：输入「真实外部世界 Odoo 19 + 销售 Agent 自然语言需求文档」→ 输出「已证伪结晶的 O2C Ontology」。

完整演示：
1. **只读勘察外部世界 Odoo 19**（``OdooPhenomenonProfiler``）：
   仅调用 ``fields_get`` / ``search_count`` / ``search_read``（对 Odoo 零写入），
   抽取 ``res.partner``、``sale.order``、``stock.picking``、``account.move`` 的真实表结构、
   枚举状态及代表性样本行（含真实红字退款单、无销售归属调拨单、非客户联系人等脏噪声）。
2. **加载自然语言业务需求文档**：
   读取 ``examples/docs/sales_agent_o2c_requirement.md``（面向 ``sales_agent`` 的 O2C 业务目标、
   动作权限与财务合规红线）。
3. **Ontology Builder 溯因提议 + Symbolic Crucible 五关沙箱证伪**：
   - 第 1 轮：Builder 漏掉 ``account.move`` 的 ``move_type='out_invoice'`` 过滤并漏写
     ``CANNOT_INVOICE_UNSHIPPED_ORDER`` 不变量 → 真实 Odoo 脏数据在 **关 2** 触发引用/状态破损，
     真实草稿单在 **关 3** 暴露未发货开票漏洞，被熔炉当场打回！
   - 第 2 轮：Builder 根据熔炉反例补齐 ``AdapterMappingSpec`` 过滤映射 + 跨 ``ObjectProperty``
     存在量词不变量（``exists Delivery where order==obj.order and status=='done'``）+ 大额审批法则
     + 本体自有风控字段（``__owned__``）→ **在真实 Odoo 19 数据上五关全绿**！
4. **Steward 审批结晶 + 销售 AI Agent 实弹试用产出的 O2C Ontology**：
   审批结晶为 ``o2c_ontology v1.0.0``，编译成 ``OntologySpec``，装载真实 Odoo 投影后的 M0 世界状态，
   导出标准 OSDK Tool Schema 并以 ``AIPAgentExecutor`` 驱动 ``sales_agent`` 实测查询脱敏、
   未发货开票拦截、超 10 万大单自动挂起审批与客户自有风控备注写回 ``ontology_overlay``。

运行方式::

    # 直连本机真实 Odoo 19 (http://localhost:8069, erp_north)
    python3 -m examples.odoo_builder_o2c_demo

    # 无 Odoo 环境时使用真实结构镜像离线运行
    python3 -m examples.odoo_builder_o2c_demo --offline
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from open_ontology import AIPAgentExecutor, Actor, OntologyRuntime, export_llm_tools
from open_ontology.induction import (
    AbductionBackend,
    ActionBlueprint,
    ActionMutationSpec,
    AdapterMappingSpec,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EntityMappingSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldSpec,
    FieldTransformSpec,
    GeminiAbductionBackend,
    InductionCycleResult,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    OpenAICompatibleAbductionBackend,
    RelationSpec,
    ReplayEvent,
    RowFilterSpec,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
)
from open_ontology.model_views import describe_behavior, describe_tbox

from .business_ontology import BUSINESS_SEED_ONTOLOGY
from .odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError

REQUIREMENT_DOC_PATH = Path(__file__).resolve().parent / "docs" / "sales_agent_o2c_requirement.md"

SALES_AGENT = Actor(
    actor_id="agent:sales-copilot",
    role="sales_agent",
    clearance_tags=frozenset({"*"}),
)
FINANCE_DIRECTOR = Actor(
    actor_id="human:finance-director",
    role="finance_director",
    clearance_tags=frozenset({"*"}),
)
WAREHOUSE_OPS = Actor(
    actor_id="human:warehouse-ops",
    role="warehouse_ops",
    clearance_tags=frozenset({"*"}),
)


# ---------------------------------------------------------------------------
# 1. 外部世界 Odoo 19 只读现象探测器（严格只读，零写入）
# ---------------------------------------------------------------------------
_ODOO_MODEL_FIELDS: dict[str, list[str]] = {
    "res.partner": ["id", "name", "ref", "credit_limit", "customer_rank", "active"],
    "sale.order": ["id", "name", "partner_id", "amount_total", "state", "client_order_ref"],
    "stock.picking": ["id", "name", "origin", "sale_id", "partner_id", "state"],
    "account.move": [
        "id",
        "name",
        "move_type",
        "partner_id",
        "invoice_origin",
        "amount_total",
        "amount_residual",
        "state",
        "payment_state",
    ],
}


def _fields_get_to_profiles(raw_meta: dict[str, dict[str, Any]]) -> list[ExternalFieldProfile]:
    profiles: list[ExternalFieldProfile] = []
    for fname, info in sorted(raw_meta.items()):
        raw_sel = info.get("selection")
        enums = [str(item[0]) for item in raw_sel] if isinstance(raw_sel, list) else []
        profiles.append(
            ExternalFieldProfile(
                name=fname,
                raw_type=str(info.get("type") or "string"),
                label=str(info.get("string") or fname),
                relation_target=str(info["relation"]) if info.get("relation") else None,
                enum_values=enums,
            )
        )
    return profiles


class OdooPhenomenonProfiler:
    """Strictly read-only Odoo 19 JSON-RPC profiler (zero writes to Odoo)."""

    def __init__(self, db: str = DB_NORTH, *, client: OdooJSONRPC | None = None) -> None:
        self.db = db
        self.client = client or OdooJSONRPC(db)

    def profile(self, *, max_invoices: int = 5, max_draft_orders: int = 4) -> ExternalPhenomenonProfile:
        self.client.login()

        # 1) Read-only schema metadata & row counts
        meta_by_model: dict[str, list[ExternalFieldProfile]] = {}
        count_by_model: dict[str, int] = {}
        for model, flds in _ODOO_MODEL_FIELDS.items():
            raw_meta = self.client.execute_kw(
                model,
                "fields_get",
                [flds],
                {"attributes": ["string", "type", "relation", "selection"]},
            )
            meta_by_model[model] = _fields_get_to_profiles(raw_meta)
            count_by_model[model] = int(self.client.execute_kw(model, "search_count", [[]]))

        # 2) Sample a coherent live O2C subgraph + real noisy records from Odoo 19
        invoices = self.client.execute_kw(
            "account.move",
            "search_read",
            [[("move_type", "=", "out_invoice"), ("invoice_origin", "!=", False), ("state", "=", "posted")]],
            {"fields": _ODOO_MODEL_FIELDS["account.move"], "limit": max_invoices, "order": "id desc"},
        )
        # Include real non-invoice noise (e.g. out_refund) so Crucible verifies filtering
        noise_moves = self.client.execute_kw(
            "account.move",
            "search_read",
            [[("move_type", "!=", "out_invoice")]],
            {"fields": _ODOO_MODEL_FIELDS["account.move"], "limit": 2, "order": "id desc"},
        )
        move_rows = list(invoices) + list(noise_moves)

        origins = sorted({str(i["invoice_origin"]) for i in invoices if i.get("invoice_origin")})
        invoiced_orders = (
            self.client.execute_kw(
                "sale.order",
                "search_read",
                [[("name", "in", origins)]],
                {"fields": _ODOO_MODEL_FIELDS["sale.order"]},
            )
            if origins
            else []
        )
        small_drafts = self.client.execute_kw(
            "sale.order",
            "search_read",
            [[("state", "=", "draft"), ("amount_total", "<=", 100000.0), ("amount_total", ">", 0.0)]],
            {"fields": _ODOO_MODEL_FIELDS["sale.order"], "limit": max_draft_orders, "order": "id desc"},
        )
        large_drafts = self.client.execute_kw(
            "sale.order",
            "search_read",
            [[("state", "=", "draft"), ("amount_total", ">", 100000.0)]],
            {"fields": _ODOO_MODEL_FIELDS["sale.order"], "limit": max_draft_orders, "order": "id desc"},
        )
        order_by_id: dict[int, dict[str, Any]] = {}
        for row in [*invoiced_orders, *small_drafts, *large_drafts]:
            order_by_id[int(row["id"])] = row
        order_rows = list(order_by_id.values())
        order_ids = list(order_by_id.keys())

        linked_pickings = (
            self.client.execute_kw(
                "stock.picking",
                "search_read",
                [[("sale_id", "in", order_ids)]],
                {"fields": _ODOO_MODEL_FIELDS["stock.picking"]},
            )
            if order_ids
            else []
        )
        # Include real internal transfer noise (sale_id == False)
        noise_pickings = self.client.execute_kw(
            "stock.picking",
            "search_read",
            [[("sale_id", "=", False)]],
            {"fields": _ODOO_MODEL_FIELDS["stock.picking"], "limit": 2, "order": "id asc"},
        )
        picking_rows = list(linked_pickings) + list(noise_pickings)

        partner_ids: set[int] = set()
        for row in [*order_rows, *move_rows]:
            p = row.get("partner_id")
            if isinstance(p, (list, tuple)) and p:
                partner_ids.add(int(p[0]))
        linked_partners = (
            self.client.execute_kw(
                "res.partner",
                "search_read",
                [[("id", "in", sorted(partner_ids))]],
                {"fields": _ODOO_MODEL_FIELDS["res.partner"]},
            )
            if partner_ids
            else []
        )
        # Include real non-customer contact noise (customer_rank == 0)
        noise_partners = self.client.execute_kw(
            "res.partner",
            "search_read",
            [[("customer_rank", "=", 0)]],
            {"fields": _ODOO_MODEL_FIELDS["res.partner"], "limit": 2, "order": "id asc"},
        )
        partner_by_id = {int(r["id"]): r for r in [*linked_partners, *noise_partners]}
        partner_rows = list(partner_by_id.values())

        return ExternalPhenomenonProfile(
            source_kind="odoo19_erp",
            source_locator=f"odoo://{self.db}",
            entities=[
                ExternalEntityProfile(
                    table_name="res.partner",
                    row_count=count_by_model["res.partner"],
                    fields=meta_by_model["res.partner"],
                    sample_rows=partner_rows,
                ),
                ExternalEntityProfile(
                    table_name="sale.order",
                    row_count=count_by_model["sale.order"],
                    fields=meta_by_model["sale.order"],
                    sample_rows=order_rows,
                ),
                ExternalEntityProfile(
                    table_name="stock.picking",
                    row_count=count_by_model["stock.picking"],
                    fields=meta_by_model["stock.picking"],
                    sample_rows=picking_rows,
                ),
                ExternalEntityProfile(
                    table_name="account.move",
                    row_count=count_by_model["account.move"],
                    fields=meta_by_model["account.move"],
                    sample_rows=move_rows,
                ),
            ],
        )


def build_offline_odoo_mirror_profile() -> ExternalPhenomenonProfile:
    """Faithful offline mirror of real Odoo 19 erp_north schemas & messy records (for CI / no-Odoo)."""
    return ExternalPhenomenonProfile(
        source_kind="odoo19_erp",
        source_locator="odoo://erp_north(offline-mirror)",
        entities=[
            ExternalEntityProfile(
                table_name="res.partner",
                row_count=56,
                fields=[
                    ExternalFieldProfile(name="id", raw_type="integer", label="ID"),
                    ExternalFieldProfile(name="name", raw_type="char", label="Name"),
                    ExternalFieldProfile(name="ref", raw_type="char", label="Reference"),
                    ExternalFieldProfile(name="credit_limit", raw_type="float", label="Credit Limit"),
                    ExternalFieldProfile(name="customer_rank", raw_type="integer", label="Customer Rank"),
                    ExternalFieldProfile(name="active", raw_type="boolean", label="Active"),
                ],
                sample_rows=[
                    {"id": 77, "name": "星河零售客户", "ref": "CUST-STAR-01", "credit_limit": 100000.0, "customer_rank": 3, "active": True},
                    {"id": 71, "name": "华东集团大客户", "ref": "CUST-EAST-01", "credit_limit": 100000.0, "customer_rank": 1, "active": True},
                    {"id": 36, "name": "内部测试联系人(非客户)", "ref": False, "credit_limit": 0.0, "customer_rank": 0, "active": True},
                ],
            ),
            ExternalEntityProfile(
                table_name="sale.order",
                row_count=6726,
                fields=[
                    ExternalFieldProfile(name="id", raw_type="integer", label="ID"),
                    ExternalFieldProfile(name="name", raw_type="char", label="Order Reference"),
                    ExternalFieldProfile(name="partner_id", raw_type="many2one", label="Customer", relation_target="res.partner"),
                    ExternalFieldProfile(name="amount_total", raw_type="monetary", label="Total"),
                    ExternalFieldProfile(name="state", raw_type="selection", label="Status", enum_values=["draft", "sent", "sale", "cancel"]),
                    ExternalFieldProfile(name="client_order_ref", raw_type="char", label="Customer Reference"),
                ],
                sample_rows=[
                    {"id": 7289, "name": "S07289", "partner_id": [77, "星河零售客户"], "amount_total": 20000.0, "state": "sale", "client_order_ref": "PO-001"},
                    {"id": 7286, "name": "S07286", "partner_id": [71, "华东集团大客户"], "amount_total": 20000.0, "state": "draft", "client_order_ref": "PO-SMALL"},
                    {"id": 7287, "name": "S07287", "partner_id": [71, "华东集团大客户"], "amount_total": 120000.0, "state": "draft", "client_order_ref": "PO-LARGE"},
                ],
            ),
            ExternalEntityProfile(
                table_name="stock.picking",
                row_count=5241,
                fields=[
                    ExternalFieldProfile(name="id", raw_type="integer", label="ID"),
                    ExternalFieldProfile(name="name", raw_type="char", label="Reference"),
                    ExternalFieldProfile(name="origin", raw_type="char", label="Source Document"),
                    ExternalFieldProfile(name="sale_id", raw_type="many2one", label="Sales Order", relation_target="sale.order"),
                    ExternalFieldProfile(name="partner_id", raw_type="many2one", label="Contact", relation_target="res.partner"),
                    ExternalFieldProfile(name="state", raw_type="selection", label="Status", enum_values=["draft", "waiting", "confirmed", "assigned", "done", "cancel"]),
                ],
                sample_rows=[
                    {"id": 5240, "name": "WH/OUT/05234", "origin": "S07289", "sale_id": [7289, "S07289"], "partner_id": [77, "星河零售客户"], "state": "done"},
                    {"id": 31, "name": "WH/OUT/00025", "origin": False, "sale_id": False, "partner_id": False, "state": "done"},
                ],
            ),
            ExternalEntityProfile(
                table_name="account.move",
                row_count=1068,
                fields=[
                    ExternalFieldProfile(name="id", raw_type="integer", label="ID"),
                    ExternalFieldProfile(name="name", raw_type="char", label="Number"),
                    ExternalFieldProfile(name="move_type", raw_type="selection", label="Type", enum_values=["entry", "out_invoice", "out_refund", "in_invoice"]),
                    ExternalFieldProfile(name="partner_id", raw_type="many2one", label="Partner", relation_target="res.partner"),
                    ExternalFieldProfile(name="invoice_origin", raw_type="char", label="Source"),
                    ExternalFieldProfile(name="amount_total", raw_type="monetary", label="Total"),
                    ExternalFieldProfile(name="amount_residual", raw_type="monetary", label="Amount Due"),
                    ExternalFieldProfile(name="state", raw_type="selection", label="Status", enum_values=["draft", "posted", "cancel"]),
                    ExternalFieldProfile(name="payment_state", raw_type="selection", label="Payment Status", enum_values=["not_paid", "in_payment", "paid", "partial"]),
                ],
                sample_rows=[
                    {"id": 1087, "name": "INV/2026/00581", "move_type": "out_invoice", "partner_id": [77, "星河零售客户"], "invoice_origin": "S07289", "amount_total": 20000.0, "amount_residual": 0.0, "state": "posted", "payment_state": "paid"},
                    {"id": 1063, "name": "RINV/2026/00020", "move_type": "out_refund", "partner_id": [77, "星河零售客户"], "invoice_origin": "S99999", "amount_total": 20000.0, "amount_residual": 20000.0, "state": "posted", "payment_state": "not_paid"},
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 2. Builder 溯因输出：O2C Ontology 蓝图 + Odoo->O2C 映射契约
# ---------------------------------------------------------------------------
def _synthesize_o2c_blueprint(*, include_unshipped_guard: bool) -> dict[str, Any]:
    invariants: list[InvariantRuleSpec] = []
    if include_unshipped_guard:
        invariants.append(
            InvariantRuleSpec(
                name="no_invoice_without_done_delivery",
                code="CANNOT_INVOICE_UNSHIPPED_ORDER",
                target_type="Invoice",
                must_hold={
                    "op": "exists",
                    "object_type": "Delivery",
                    "where": {
                        "op": "and",
                        "args": [
                            {"op": "eq", "left": {"ref": "target.order"}, "right": {"ref": "obj.order"}},
                            {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "done"}},
                        ],
                    },
                },
                description="财务红线：开具应收发票前，对应销售订单必须存在至少一张已完成出库（done）的交货单。",
            )
        )

    bp = DeclarativeBlueprint(
        model_id="o2c_ontology",
        version="1.0.0",
        description="面向销售 AI Agent 的纯业务 O2C（订单到收款）本体，屏蔽底层 Odoo 技术表差异。",
        object_types=[
            ObjectTypeBlueprint(
                name="Customer",
                archetype="MasterEntity",
                primary_key="customer_id",
                title_field="name",
                display_label="客户",
                fields=[
                    FieldSpec(name="customer_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="name", field_type="str", required=True),
                    FieldSpec(
                        name="credit_limit_cents",
                        field_type="int",
                        default=0,
                        non_negative=True,
                        masked_roles=("finance_director", "admin"),
                        description="授信额度（分），对销售 Agent 脱敏",
                    ),
                    FieldSpec(
                        name="risk_tier",
                        field_type="str",
                        default="NORMAL",
                        owned=True,
                        description="本体自有风控等级：NORMAL / WATCHLIST / BLOCKED",
                    ),
                    FieldSpec(
                        name="sales_followup_note",
                        field_type="str",
                        default="",
                        owned=True,
                        description="销售 Agent 自有跟进备注（Odoo 刷新不覆盖）",
                    ),
                ],
            ),
            ObjectTypeBlueprint(
                name="SalesOrder",
                archetype="ProcessDocument",
                primary_key="order_id",
                title_field="order_code",
                display_label="销售订单",
                initial_statuses=["draft"],
                transitions={
                    "draft": ["confirmed", "cancelled"],
                    "confirmed": ["cancelled"],
                },
                fields=[
                    FieldSpec(name="order_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="order_code", field_type="str", required=True),
                    FieldSpec(name="amount_cents", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="draft"),
                ],
                relations=[
                    RelationSpec(
                        field="customer",
                        target_type="Customer",
                        required=True,
                        description="下单客户",
                    )
                ],
            ),
            ObjectTypeBlueprint(
                name="Delivery",
                archetype="ProcessDocument",
                primary_key="delivery_id",
                title_field="delivery_code",
                display_label="出库交货单",
                initial_statuses=["ready"],
                transitions={
                    "ready": ["done", "cancelled"],
                },
                fields=[
                    FieldSpec(name="delivery_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="delivery_code", field_type="str", required=True),
                    FieldSpec(name="status", field_type="str", default="ready"),
                ],
                relations=[
                    RelationSpec(
                        field="order",
                        target_type="SalesOrder",
                        required=True,
                        description="关联销售订单",
                    )
                ],
            ),
            ObjectTypeBlueprint(
                name="Invoice",
                archetype="ProcessDocument",
                primary_key="invoice_id",
                title_field="invoice_code",
                display_label="应收发票",
                initial_statuses=["posted"],
                transitions={"posted": ["paid"]},
                fields=[
                    FieldSpec(name="invoice_id", field_type="str", primary_key=True, auto_id=True, required=True),
                    FieldSpec(name="invoice_code", field_type="str", required=True),
                    FieldSpec(name="amount_cents", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="residual_cents", field_type="int", default=0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="posted"),
                ],
                relations=[
                    RelationSpec(field="order", target_type="SalesOrder", required=True, description="关联销售订单"),
                    RelationSpec(field="customer", target_type="Customer", required=True, description="开票客户"),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="confirm_sales_order",
                description="确认草稿销售订单；超 10 万元（10,000,000 分）且非财务总监提交时自动挂起审批。",
                allowed_roles=["sales_agent", "finance_director", "admin"],
                approver_roles=["finance_director", "admin"],
                approval_rule={
                    "op": "and",
                    "args": [
                        {"op": "gt", "left": {"ref": "param.order_amount_cents"}, "right": {"lit": 10000000}},
                        {"op": "ne", "left": {"ref": "actor.role"}, "right": {"lit": "finance_director"}},
                    ],
                },
                affects_source=True,
                display_label="确认销售订单",
                params=[
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="order_amount_cents", field_type="int", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="confirm_order_amount_matches",
                        code="ORDER_AMOUNT_MISMATCH",
                        target_type="SalesOrder",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.amount_cents"},
                            "right": {"ref": "param.order_amount_cents"},
                        },
                        description="入参金额必须与订单账面金额精确一致。",
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SalesOrder",
                        id_param="order_id",
                        set_fields={"status": {"lit": "confirmed"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="ship_order_delivery",
                description="完成出库交货单发货；关联订单必须已确认。",
                allowed_roles=["warehouse_ops", "sales_agent", "admin"],
                affects_source=True,
                display_label="完成交货出库",
                params=[FieldSpec(name="delivery_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="delivery_requires_confirmed_order",
                        code="ORDER_NOT_CONFIRMED",
                        target_type="Delivery",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.order.status"},
                            "right": {"lit": "confirmed"},
                        },
                        description="关联销售订单未确认前严禁出库。",
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Delivery",
                        id_param="delivery_id",
                        set_fields={"status": {"lit": "done"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="issue_sales_invoice",
                description="为已发货销售订单开具客户应收发票。",
                allowed_roles=["sales_agent", "finance_director", "admin"],
                affects_source=True,
                display_label="开具应收发票",
                params=[
                    FieldSpec(name="invoice_id", field_type="str", required=True),
                    FieldSpec(name="invoice_code", field_type="str", required=True),
                    FieldSpec(name="order_id", field_type="str", required=True),
                    FieldSpec(name="customer_id", field_type="str", required=True),
                    FieldSpec(name="amount_cents", field_type="int", required=True),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="Invoice",
                        id_param="invoice_id",
                        set_fields={
                            "invoice_id": {"ref": "param.invoice_id"},
                            "invoice_code": {"ref": "param.invoice_code"},
                            "order": {"ref": "param.order_id"},
                            "customer": {"ref": "param.customer_id"},
                            "amount_cents": {"ref": "param.amount_cents"},
                            "residual_cents": {"ref": "param.amount_cents"},
                            "status": {"lit": "posted"},
                        },
                    )
                ],
            ),
            ActionBlueprint(
                name="record_invoice_payment",
                description="登记客户收款并全额核销应收发票。",
                allowed_roles=["finance_director", "sales_agent", "admin"],
                affects_source=True,
                display_label="登记收款核销",
                params=[
                    FieldSpec(name="invoice_id", field_type="str", required=True),
                    FieldSpec(name="paid_amount_cents", field_type="int", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="invoice_not_already_paid",
                        code="INVOICE_ALREADY_PAID",
                        target_type="Invoice",
                        must_hold={
                            "op": "ne",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "paid"},
                        },
                        description="已结清发票严禁重复收款核销。",
                    ),
                    InvariantRuleSpec(
                        name="payment_matches_residual",
                        code="PAYMENT_AMOUNT_MISMATCH",
                        target_type="Invoice",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "param.paid_amount_cents"},
                            "right": {"ref": "obj.residual_cents"},
                        },
                        description="实收金额必须精确等于发票待收余额。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Invoice",
                        id_param="invoice_id",
                        set_fields={"residual_cents": {"lit": 0}, "status": {"lit": "paid"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="annotate_customer_risk",
                description="销售 Agent 标注客户自有风控等级与跟进备注（落盘本体自有覆盖层，不污染 Odoo）。",
                allowed_roles=["sales_agent", "finance_director", "admin"],
                affects_source=False,
                display_label="标注客户风控备注",
                params=[
                    FieldSpec(name="customer_id", field_type="str", required=True),
                    FieldSpec(name="risk_tier", field_type="str", required=True),
                    FieldSpec(name="sales_followup_note", field_type="str", required=True),
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="valid_risk_tier_enum",
                        code="INVALID_RISK_TIER",
                        target_type="Customer",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "param.risk_tier"},
                            "right": {"lit": ["NORMAL", "WATCHLIST", "BLOCKED"]},
                        },
                        description="风控等级仅允许 NORMAL / WATCHLIST / BLOCKED。",
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Customer",
                        id_param="customer_id",
                        set_fields={
                            "risk_tier": {"ref": "param.risk_tier"},
                            "sales_followup_note": {"ref": "param.sales_followup_note"},
                        },
                    )
                ],
            ),
        ],
        invariants=invariants,
    )
    return bp.model_dump(mode="json")


def _synthesize_odoo_adapter_mapping(*, clean_filters: bool) -> dict[str, Any]:
    partner_filters = [RowFilterSpec(field="customer_rank", op="gt", value=0)] if clean_filters else []
    picking_filters = [RowFilterSpec(field="sale_id", op="truthy")] if clean_filters else []
    move_filters = (
        [
            RowFilterSpec(field="move_type", op="eq", value="out_invoice"),
            RowFilterSpec(field="state", op="eq", value="posted"),
        ]
        if clean_filters
        else []
    )
    drop_refs_picking = ["order"] if clean_filters else []
    drop_refs_invoice = ["order", "customer"] if clean_filters else []

    mapping = AdapterMappingSpec(
        mapping_id="odoo19_to_o2c_v1",
        source_kind="odoo19_erp",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="res.partner",
                target_object_type="Customer",
                row_filters=partner_filters,
                id_transform=FieldTransformSpec(
                    target_field="customer_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="CUST-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default="Unnamed"),
                    FieldTransformSpec(
                        target_field="credit_limit_cents",
                        source_field="credit_limit",
                        kind="scale_int",
                        scale=100,
                        default=0,
                    ),
                    FieldTransformSpec(target_field="risk_tier", kind="const", default="NORMAL"),
                    FieldTransformSpec(target_field="sales_followup_note", kind="const", default=""),
                ],
            ),
            EntityMappingSpec(
                source_table="sale.order",
                target_object_type="SalesOrder",
                id_transform=FieldTransformSpec(
                    target_field="order_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="SO-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order_code", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="customer",
                        source_field="partner_id",
                        kind="foreign_key_id",
                        prefix="CUST-",
                    ),
                    FieldTransformSpec(
                        target_field="amount_cents",
                        source_field="amount_total",
                        kind="scale_int",
                        scale=100,
                        default=0,
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={
                            "draft": "draft",
                            "sent": "draft",
                            "sale": "confirmed",
                            "cancel": "cancelled",
                        },
                        default="draft",
                    ),
                ],
                drop_if_missing_refs=["customer"] if clean_filters else [],
            ),
            EntityMappingSpec(
                source_table="stock.picking",
                target_object_type="Delivery",
                row_filters=picking_filters,
                id_transform=FieldTransformSpec(
                    target_field="delivery_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="DLV-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="delivery_code", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="order",
                        source_field="sale_id",
                        kind="foreign_key_id",
                        prefix="SO-",
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        enum_map={
                            "draft": "ready",
                            "waiting": "ready",
                            "confirmed": "ready",
                            "assigned": "ready",
                            "done": "done",
                            "cancel": "cancelled",
                        },
                        default="ready",
                    ),
                ],
                drop_if_missing_refs=drop_refs_picking,
            ),
            EntityMappingSpec(
                source_table="account.move",
                target_object_type="Invoice",
                row_filters=move_filters,
                id_transform=FieldTransformSpec(
                    target_field="invoice_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="INV-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="invoice_code", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="order",
                        source_field="invoice_origin",
                        kind="lookup_join",
                        lookup_table="sale.order",
                        lookup_key_field="name",
                    ),
                    FieldTransformSpec(
                        target_field="customer",
                        source_field="partner_id",
                        kind="foreign_key_id",
                        prefix="CUST-",
                    ),
                    FieldTransformSpec(
                        target_field="amount_cents",
                        source_field="amount_total",
                        kind="scale_int",
                        scale=100,
                        default=0,
                    ),
                    FieldTransformSpec(
                        target_field="residual_cents",
                        source_field="amount_residual",
                        kind="scale_int",
                        scale=100,
                        default=0,
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="payment_state",
                        kind="enum_map",
                        enum_map={
                            "paid": "paid",
                            "in_payment": "paid",
                            "not_paid": "posted",
                            "partial": "posted",
                        },
                        default="posted",
                    ),
                ],
                drop_if_missing_refs=drop_refs_invoice,
            ),
        ],
    )
    return mapping.model_dump(mode="json")


def make_odoo_builder_offline_backend() -> CallableAbductionBackend:
    """Offline deterministic Builder:
    - Round 1: naive mapping (leaks Odoo out_refund & internal transfer noise) + missing unshipped-invoice guard
      -> caught by Symbolic Crucible Gate 2 (real Odoo dirty rows!) & Gate 3 (real Odoo draft order!)
    - Round 2: fixes row filters, lookup_join, and cross-ObjectProperty Delivery exists guard -> 5 gates PASS!
    """

    def _propose(_sys: str, _user: str, round_index: int) -> dict[str, Any]:
        if round_index == 1:
            return {
                "rationale": "第 1 轮初稿：直接将 Odoo 4 张核心表映射为 O2C 四对象（尚未过滤杂糅单据，尚未加跨单据开票护栏）。",
                "blueprint": _synthesize_o2c_blueprint(include_unshipped_guard=False),
                "adapter_mapping": _synthesize_odoo_adapter_mapping(clean_filters=False),
                "proposed_eval_cases": [],
            }
        return {
            "rationale": "第 2 轮修正：按熔炉反例在 Odoo 映射层过滤 out_refund / 无销售调拨单 / 非客户联系人，并在本体层补齐跨 Delivery 存在量词不变量。",
            "blueprint": _synthesize_o2c_blueprint(include_unshipped_guard=True),
            "adapter_mapping": _synthesize_odoo_adapter_mapping(clean_filters=True),
            "proposed_eval_cases": [],
        }

    return CallableAbductionBackend(_propose)


# ---------------------------------------------------------------------------
# 3. 基于真实 Odoo 投影记录动态构造关 3 考卷（零硬编码假订单号）
# ---------------------------------------------------------------------------
def build_replay_events_from_projected_odoo(
    projected_m0: list[dict[str, Any]],
) -> tuple[list[ReplayEvent], list[ReplayEvent]]:
    orders = [o["data"] for o in projected_m0 if o["object_type"] == "SalesOrder"]
    deliveries = [o["data"] for o in projected_m0 if o["object_type"] == "Delivery"]
    invoices = [o["data"] for o in projected_m0 if o["object_type"] == "Invoice"]
    customers = [o["data"] for o in projected_m0 if o["object_type"] == "Customer"]

    done_delivery_orders = {d["order"] for d in deliveries if d.get("status") == "done"}
    small_draft = next(
        (o for o in orders if o.get("status") == "draft" and 0 < int(o.get("amount_cents", 0)) <= 10_000_000),
        None,
    )
    large_draft = next(
        (o for o in orders if o.get("status") == "draft" and int(o.get("amount_cents", 0)) > 10_000_000),
        None,
    )
    shipped_order = next(
        (o for o in orders if o.get("status") == "confirmed" and o.get("order_id") in done_delivery_orders),
        None,
    )
    paid_invoice = next((i for i in invoices if i.get("status") == "paid"), None)

    pos: list[ReplayEvent] = []
    neg: list[ReplayEvent] = []

    if small_draft is not None:
        pos.append(
            ReplayEvent(
                name="pos_sales_agent_confirm_small_odoo_order",
                action="confirm_sales_order",
                params={
                    "order_id": small_draft["order_id"],
                    "order_amount_cents": int(small_draft["amount_cents"]),
                },
                actor=SALES_AGENT,
                expected_valid=True,
                expected_requires_approval=False,
            )
        )
        # 对真实 Odoo 未发货草稿单强行开票 -> 必须被红牌拦截
        neg.append(
            ReplayEvent(
                name="neg_invoice_unshipped_real_odoo_order",
                action="issue_sales_invoice",
                params={
                    "invoice_id": "INV-TEST-ILLEGAL",
                    "invoice_code": "INV/TEST/ILLEGAL",
                    "order_id": small_draft["order_id"],
                    "customer_id": small_draft["customer"],
                    "amount_cents": int(small_draft["amount_cents"]),
                },
                actor=SALES_AGENT,
                expected_valid=False,
                expected_error_code="CANNOT_INVOICE_UNSHIPPED_ORDER",
            )
        )

    if large_draft is not None:
        pos.append(
            ReplayEvent(
                name="pos_large_odoo_order_triggers_approval_for_sales_agent",
                action="confirm_sales_order",
                params={
                    "order_id": large_draft["order_id"],
                    "order_amount_cents": int(large_draft["amount_cents"]),
                },
                actor=SALES_AGENT,
                expected_valid=True,
                expected_requires_approval=True,
            )
        )
        pos.append(
            ReplayEvent(
                name="pos_large_odoo_order_direct_for_finance_director",
                action="confirm_sales_order",
                params={
                    "order_id": large_draft["order_id"],
                    "order_amount_cents": int(large_draft["amount_cents"]),
                },
                actor=FINANCE_DIRECTOR,
                expected_valid=True,
                expected_requires_approval=False,
            )
        )

    if shipped_order is not None:
        pos.append(
            ReplayEvent(
                name="pos_invoice_shipped_real_odoo_order",
                action="issue_sales_invoice",
                params={
                    "invoice_id": "INV-TEST-VALID",
                    "invoice_code": "INV/TEST/VALID",
                    "order_id": shipped_order["order_id"],
                    "customer_id": shipped_order["customer"],
                    "amount_cents": int(shipped_order["amount_cents"]),
                },
                actor=SALES_AGENT,
                expected_valid=True,
            )
        )

    if paid_invoice is not None:
        neg.append(
            ReplayEvent(
                name="neg_double_pay_real_odoo_paid_invoice",
                action="record_invoice_payment",
                params={
                    "invoice_id": paid_invoice["invoice_id"],
                    "paid_amount_cents": int(paid_invoice["amount_cents"]),
                },
                actor=SALES_AGENT,
                expected_valid=False,
                expected_error_code="INVOICE_ALREADY_PAID",
            )
        )

    if customers:
        pos.append(
            ReplayEvent(
                name="pos_sales_agent_annotate_owned_customer_risk",
                action="annotate_customer_risk",
                params={
                    "customer_id": customers[0]["customer_id"],
                    "risk_tier": "WATCHLIST",
                    "sales_followup_note": "季度大额复购跟进中",
                },
                actor=SALES_AGENT,
                expected_valid=True,
                expected_requires_approval=False,
            )
        )

    return pos, neg


# ---------------------------------------------------------------------------
# 4. 完整执行：输入 (Odoo + 自然语言需求) -> 输出 (结晶 O2C Ontology + Agent 实弹验收)
# ---------------------------------------------------------------------------
def _select_builder_backend(use_live_llm: bool) -> tuple[AbductionBackend, str]:
    if use_live_llm:
        if os.environ.get("GEMINI_API_KEY"):
            return GeminiAbductionBackend(), "gemini"
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAICompatibleAbductionBackend(), "openai-compatible"
    return make_odoo_builder_offline_backend(), "offline-deterministic"


def run_odoo_to_o2c_builder_demo(
    *,
    offline_odoo: bool = False,
    use_live_llm: bool = False,
    db: str = DB_NORTH,
) -> dict[str, Any]:
    # 1) 输入一：外部世界 Odoo 19 只读采样
    if offline_odoo:
        profile = build_offline_odoo_mirror_profile()
        odoo_mode = "offline-mirror"
    else:
        try:
            profile = OdooPhenomenonProfiler(db=db).profile()
            odoo_mode = f"live-odoo19:{db}"
        except OdooRPCError:
            profile = build_offline_odoo_mirror_profile()
            odoo_mode = "offline-mirror-fallback"

    # 2) 输入二：面向销售 Agent 的自然语言需求文档
    requirement_doc = REQUIREMENT_DOC_PATH.read_text(encoding="utf-8")

    backend, llm_mode = _select_builder_backend(use_live_llm=use_live_llm)

    with InductionGovernanceService(db_path=":memory:") as governance:
        builder = OntologyBuilderAgent(
            backend=backend,
            governance=governance,
            crucible=SymbolicCrucible(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        crucible_ctx = CrucibleContext(
            mode="genesis",
            replay_factory=build_replay_events_from_projected_odoo,
            default_actor=SALES_AGENT,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        result: InductionCycleResult = builder.run_genesis_cycle(
            model_id="o2c_ontology",
            requirement_doc=requirement_doc,
            external_profile=profile,
            crucible_context=crucible_ctx,
            max_rounds=3,
        )
        assert result.verified and result.pending_approval_run_id is not None
        governance.approve_crystallization(result.pending_approval_run_id, STEWARD_ACTOR)

        crystallized_bp = governance.get_active_blueprint("o2c_ontology")
        assert crystallized_bp is not None

        # 3) 输出验证：将结晶的 O2C Ontology 编译为真实可运行的 OntologySpec + 装载真实 Odoo M0 数据
        o2c_spec = compile_blueprint(crystallized_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
        tbox_view = describe_tbox(o2c_spec)
        behavior_view = describe_behavior(o2c_spec)
        osdk_tools = export_llm_tools(o2c_spec)

        o2c_rt = OntologyRuntime(o2c_spec, db_path=":memory:")
        try:
            order_rank = {name: i for i, name in enumerate(o2c_spec.object_types)}
            for entry in sorted(
                result.projected_m0_objects,
                key=lambda e: order_rank.get(e["object_type"], 999),
            ):
                cls = o2c_spec.get_object_type(entry["object_type"])
                o2c_rt.put_object(cls(**entry["data"]))

            # 4) 销售 AI Agent 通过 OSDK AIPAgentExecutor 实弹使用新生成的 O2C Ontology
            agent_exec = AIPAgentExecutor(o2c_rt, SALES_AGENT)
            director_exec = AIPAgentExecutor(o2c_rt, FINANCE_DIRECTOR)
            customers_visible = agent_exec.dispatch("query_objects", {"object_type": "Customer"})["objects"]
            customers_for_director = director_exec.dispatch("query_objects", {"object_type": "Customer"})["objects"]
            orders_visible = agent_exec.dispatch("query_objects", {"object_type": "SalesOrder"})["objects"]

            small_draft = next(
                (o for o in orders_visible if o["status"] == "draft" and 0 < o["amount_cents"] <= 10_000_000),
                None,
            )
            large_draft = next(
                (o for o in orders_visible if o["status"] == "draft" and o["amount_cents"] > 10_000_000),
                None,
            )

            # 4a) 销售 Agent 尝试对真实未发货草稿单开票 -> 必须被红牌拦截
            unshipped_invoice_sim = None
            if small_draft is not None:
                unshipped_invoice_sim = agent_exec.dispatch(
                    "simulate_action",
                    {
                        "action": "issue_sales_invoice",
                        "params": {
                            "invoice_id": "INV-AGENT-TRY",
                            "invoice_code": "INV/TRY/001",
                            "order_id": small_draft["order_id"],
                            "customer_id": small_draft["customer"],
                            "amount_cents": small_draft["amount_cents"],
                        },
                    },
                )

            # 4b) 销售 Agent 提交超 10 万真实大单确认 -> 自动挂起 PENDING_APPROVAL
            large_order_run = None
            if large_draft is not None:
                large_order_run = agent_exec.dispatch(
                    "submit_action",
                    {
                        "run_id": "SALES-AGENT-LARGE-1",
                        "action": "confirm_sales_order",
                        "params": {
                            "order_id": large_draft["order_id"],
                            "order_amount_cents": large_draft["amount_cents"],
                        },
                    },
                )

            # 4c) 销售 Agent 写客户自有风控备注 -> COMMITTED 进 ontology_overlay
            overlay_run = None
            if customers_visible:
                overlay_run = agent_exec.dispatch(
                    "submit_action",
                    {
                        "run_id": "SALES-AGENT-NOTE-1",
                        "action": "annotate_customer_risk",
                        "params": {
                            "customer_id": customers_visible[0]["customer_id"],
                            "risk_tier": "WATCHLIST",
                            "sales_followup_note": "来自真实 Odoo 客户的高价值商机跟进",
                        },
                    },
                )

            counts_by_type = {
                ot_name: o2c_rt.count_objects(ot_name) for ot_name in o2c_spec.object_types
            }
            return {
                "input": {
                    "odoo_mode": odoo_mode,
                    "odoo_source_locator": profile.source_locator,
                    "odoo_raw_table_counts": {e.table_name: e.row_count for e in profile.entities},
                    "odoo_sampled_rows": {e.table_name: len(e.sample_rows) for e in profile.entities},
                    "requirement_doc_path": str(REQUIREMENT_DOC_PATH),
                    "requirement_doc_chars": len(requirement_doc),
                },
                "builder_cegis": {
                    "llm_mode": llm_mode,
                    "rounds": [
                        {
                            "round": r.round_index,
                            "passed": r.passed,
                            "failed_gates": list(r.failed_gates),
                        }
                        for r in result.rounds
                    ],
                    "final_status": "CRYSTALLIZED",
                    "version": crystallized_bp.version,
                },
                "output_o2c_ontology": {
                    "model_id": crystallized_bp.model_id,
                    "object_types": [ot["name"] for ot in tbox_view["object_types"]],
                    "object_properties": [p["name"] for p in tbox_view["object_properties"]],
                    "actions": [a["name"] for a in behavior_view["actions"]],
                    "invariants": list(o2c_spec.invariants.keys()),
                    "transition_invariants": list(o2c_spec.transition_invariants.keys()),
                    "exported_osdk_tools": [t["function"]["name"] for t in osdk_tools],
                    "grounded_m0_counts_from_odoo": counts_by_type,
                },
                "sales_agent_verification": {
                    "credit_limit_seen_by_sales_agent": (
                        customers_visible[0]["credit_limit_cents"] if customers_visible else None
                    ),
                    "credit_limit_seen_by_finance_director": (
                        customers_for_director[0]["credit_limit_cents"] if customers_for_director else None
                    ),
                    "customer_credit_masked_for_sales_agent": (
                        bool(customers_visible)
                        and bool(customers_for_director)
                        and customers_visible[0]["credit_limit_cents"]
                        != customers_for_director[0]["credit_limit_cents"]
                    ),
                    "unshipped_invoice_blocked": (
                        unshipped_invoice_sim is not None
                        and unshipped_invoice_sim["valid"] is False
                        and "CANNOT_INVOICE_UNSHIPPED_ORDER" in str(unshipped_invoice_sim.get("error") or "")
                    ),
                    "large_order_status": large_order_run["run"]["status"] if large_order_run else None,
                    "overlay_note_status": overlay_run["run"]["status"] if overlay_run else None,
                },
            }
        finally:
            o2c_rt.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ontology Builder: Live Odoo 19 + Sales Agent Requirement -> O2C Ontology"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use offline Odoo 19 mirror instead of connecting to localhost:8069",
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="Use GEMINI_API_KEY / OPENAI_API_KEY if configured",
    )
    parser.add_argument("--db", default=DB_NORTH, help=f"Odoo database name (default: {DB_NORTH})")
    args = parser.parse_args(argv)
    out = run_odoo_to_o2c_builder_demo(
        offline_odoo=args.offline,
        use_live_llm=args.live_llm,
        db=args.db,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
