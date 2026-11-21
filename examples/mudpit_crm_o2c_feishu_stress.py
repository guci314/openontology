"""WO-09 跨软件三外设“泥潭实战压力测试”模块（CRM + O2C + Feishu WorkCard）。

将一份组合标准本体（``compose_blueprints([std_core, std_crm, std_o2c])``）与一份
只增不破的 ``CustomerOverlaySpec`` 同时挂载到三类真实异构外设上：
1. **BottleCRM（临时副本 SQLite）** —— 承载 ``CrmAccount / CrmDeal / CrmTicket``；
2. **Odoo 19 克隆库（Live ``erp_north`` / 离线物理镜像）** —— 承载
   ``AccountingEntity / O2cCustomerProfile / O2cOrder / O2cDelivery / O2cInvoice``；
3. **飞书协同卡片网关（``FeishuApprovalGateway`` + HTTP Mock 传输）** —— 承载
   M1 ``WorkCard`` 跨系统签批、SoD 防自批、$t_0 \\to t_1$ 幻读攻击拦截与卡重算核销。

在不改动一行 ``std_core_ontology.py`` / ``std_crm_ontology.py`` / ``std_o2c_ontology.py``
的前提下，硬撞 6 个脏泥潭场景并接入 ``DifferentialFalsifier`` 实现实库证伪。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_ontology import (
    DEVICE_STATE_MISMATCH,
    Actor,
    AIPAgentExecutor,
    DeviceStateMismatchError,
    InvariantError,
    OntologyRuntime,
    OntologySpec,
    PermissionDeniedError,
    SourceBridge,
)
from open_ontology.induction import (
    ActionExecutionBinding,
    AdapterMappingSpec,
    AdversarialProbe,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    DifferentialFalsificationReport,
    DifferentialFalsifier,
    EntityMappingSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldTransformSpec,
    InvariantRuleSpec,
    ScopedViewBlueprint,
    apply_overlay,
    compile_blueprint,
    compose_blueprints,
    merge_projected_phenomena,
    project_external_phenomenon,
    project_single_record,
)

from .business_ontology import BUSINESS_SEED_ONTOLOGY
from .dual_device_driver_swap_benchmark import (
    BOTTLECRM_SQLITE_PATH,
    GenericSQLiteDriverBridge,
    file_sha256,
    scout_sqlite_device,
)
from .feishu_card_bridge import FeishuApprovalGateway, FeishuOpenAPIClient
from .odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError
from .odoo_fde_prober import EphemeralOdooSandbox
from .odoo_utility_benchmark import OfflineOdooPhysicalWorld, PhysicalOdooHarness
from .std_core_ontology import build_std_core_blueprint
from .std_crm_ontology import build_std_crm_blueprint
from .std_o2c_ontology import build_std_o2c_blueprint

ACME_BOTTLECRM_ID = "39e36940179f47c7be22805d72db1a99"
NORTHWIND_BOTTLECRM_ID = "9865ac3c44d1493da0a88913d2458f52"
SOUTHSTAR_BOTTLECRM_ID = "3fe3cc2bed70479ea9ed418d2667e06d"
ACME_EXISTING_TICKET_ID = "TK-400fde64ab63427a81765af00c3b2445"
DEFAULT_CREDIT_LIMIT = 100_000.0

AI_AGENT_ACTOR = Actor(actor_id="ai-agent-01", role="ai_agent")
OPS_ACTOR = Actor(actor_id="ops-01", role="ops")
SALES_REP_ACTOR = Actor(actor_id="sales-rep-01", role="sales_rep")
SUPPORT_ACTOR = Actor(actor_id="support-01", role="support_agent")
STEWARD_ACTOR = Actor(actor_id="steward-01", role="steward")
WAREHOUSE_ACTOR = Actor(actor_id="warehouse-01", role="warehouse")
FINANCE_ACTOR = Actor(actor_id="finance-01", role="finance")

CRM_ACTIONS = frozenset({"win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"})
O2C_ACTIONS = frozenset(
    {
        "confirm_o2c_order",
        "complete_o2c_delivery",
        "post_o2c_invoice",
        "record_o2c_payment",
    }
)


# ---------------------------------------------------------------------------
# 1. Composed Standard Core + CRM + O2C + Additive CustomerOverlaySpec
# ---------------------------------------------------------------------------
def build_mudpit_composed_blueprint() -> DeclarativeBlueprint:
    """无修改直接组合 ``std_core + std_crm + std_o2c`` 三套标准蓝图。"""
    return compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
        ],
        model_id="std_enterprise_crm_o2c",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="企业标准组合本体（Core + CRM + O2C）：共享 BusinessParty 与协同 WorkCard。",
    )


def build_mudpit_customer_overlay() -> CustomerOverlaySpec:
    """严格通过只增不破 ``CustomerOverlaySpec`` 注入跨两跳风控、敞口审批与双视图隔离。"""
    return CustomerOverlaySpec(
        overlay_id="mudpit-enterprise-crm-o2c-overlay",
        base_model_id="std_enterprise_crm_o2c",
        target_version="1.1.0",
        description="客户个性风控叠加：跨两跳未决高优工单拦截、累计授信审批门限、双岗位 ScopedView。",
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="block_o2c_order_on_open_critical_crm_ticket",
                    code="OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "eq",
                                    "left": {"ref": "target.account.party"},
                                    "right": {"ref": "obj.customer.party"},
                                },
                                {
                                    "op": "in",
                                    "left": {"ref": "target.priority"},
                                    "right": {"lit": ["HIGH", "URGENT"]},
                                },
                                {
                                    "op": "in",
                                    "left": {"ref": "target.status"},
                                    "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                                },
                            ],
                        },
                    },
                    description="同一共享 BusinessParty 在 CRM 存在未闭环 HIGH/URGENT 工单时禁止确认 O2C 订单。",
                ),
                InvariantRuleSpec(
                    name="require_steward_over_cumulative_credit_exposure",
                    code="APPROVAL_REQUIRED_CREDIT_LIMIT_EXCEEDED",
                    target_type="O2cOrder",
                    id_param="order_id",
                    when={
                        "op": "and",
                        "args": [
                            {
                                "op": "gt",
                                "left": {"ref": "obj.customer.credit_limit"},
                                "right": {"lit": 0},
                            },
                            {
                                "op": "gt",
                                "left": {
                                    "op": "sum",
                                    "object_type": "O2cOrder",
                                    "field": {
                                        "op": "if_else",
                                        "cond": {
                                            "op": "in",
                                            "left": {"ref": "target.status"},
                                            "right": {"lit": ["confirmed", "delivered", "invoiced"]},
                                        },
                                        "then": {"ref": "target.amount_total"},
                                        "else": {"lit": 0.0},
                                    },
                                    "where": {
                                        "op": "eq",
                                        "left": {"ref": "target.customer"},
                                        "right": {"ref": "obj.customer"},
                                    },
                                },
                                "right": {
                                    "op": "sub",
                                    "left": {"ref": "obj.customer.credit_limit"},
                                    "right": {"ref": "obj.amount_total"},
                                },
                            },
                        ],
                    },
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["steward", "sales_manager", "admin"]},
                    },
                    description="累计在途订单敞口超过客户授信额度时须由主管/Steward 通过 WorkCard 签批放行。",
                ),
            ],
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="block_win_deal_on_open_critical_crm_ticket",
                    code="OPEN_CRITICAL_CRM_TICKET_BLOCKS_WIN",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "eq",
                                    "left": {"ref": "target.account.party"},
                                    "right": {"ref": "obj.account.party"},
                                },
                                {
                                    "op": "in",
                                    "left": {"ref": "target.priority"},
                                    "right": {"lit": ["HIGH", "URGENT"]},
                                },
                                {
                                    "op": "in",
                                    "left": {"ref": "target.status"},
                                    "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                                },
                            ],
                        },
                    },
                    description="同往来主体存在未闭环 HIGH/URGENT 客服工单时禁止推进商机赢单。",
                ),
            ],
            "lose_crm_deal": [
                InvariantRuleSpec(
                    name="high_value_deal_loss_requires_steward",
                    code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 100_000.0}},
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["steward", "sales_manager", "admin"]},
                    },
                    description="金额高于 10 万美元的高价值商机输单须由 Steward/销售主管签批确认。",
                ),
            ],
        },
        add_views=[
            ScopedViewBlueprint(
                name="crm_ops_view",
                description="CRM 业务线窄视图：仅暴露往来主体与 CRM 账户/商机/工单及协同工单卡。",
                roles=["sales_rep", "sales_manager", "support_agent", "ai_agent", "steward", "admin"],
                object_types=["BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket", "WorkCard"],
                actions=["win_crm_deal", "lose_crm_deal", "resolve_crm_ticket", "open_work_card"],
                include_reachable_objects=True,
            ),
            ScopedViewBlueprint(
                name="o2c_ops_view",
                description="O2C 履约与财务窄视图：仅暴露核算主体/往来主体与 O2C 档案/订单/发货/发票及工单卡。",
                roles=["sales_rep", "sales_manager", "warehouse", "finance", "ops", "ai_agent", "steward", "admin"],
                object_types=[
                    "AccountingEntity",
                    "BusinessParty",
                    "O2cCustomerProfile",
                    "O2cOrder",
                    "O2cDelivery",
                    "O2cInvoice",
                    "WorkCard",
                ],
                actions=[
                    "confirm_o2c_order",
                    "complete_o2c_delivery",
                    "post_o2c_invoice",
                    "record_o2c_payment",
                    "open_work_card",
                ],
                include_reachable_objects=True,
            ),
        ],
    )


def build_mudpit_effective_blueprint() -> DeclarativeBlueprint:
    return apply_overlay(build_mudpit_composed_blueprint(), build_mudpit_customer_overlay())


# ---------------------------------------------------------------------------
# 2. Heterogeneous Entity Resolution + Declarative Dual-Device Mapping
# ---------------------------------------------------------------------------
@dataclass
class EntityResolutionTable:
    """适配器层跨系统实体解析映射表（不污染 M1 本体）。"""

    crm_account_to_party: dict[str, str] = field(
        default_factory=lambda: {
            ACME_BOTTLECRM_ID: "PTY-ACME",
            NORTHWIND_BOTTLECRM_ID: "PTY-NORTHWIND",
            SOUTHSTAR_BOTTLECRM_ID: "PTY-SOUTHSTAR",
        }
    )
    odoo_partner_to_party: dict[str, str] = field(default_factory=dict)
    odoo_partner_to_profile: dict[str, str] = field(default_factory=dict)


def build_bottlecrm_adapter_mapping(er: EntityResolutionTable) -> AdapterMappingSpec:
    """构造 BottleCRM SQLite -> ``BusinessParty + CrmAccount + CrmDeal + CrmTicket`` 声明式映射。"""
    return AdapterMappingSpec(
        mapping_id="mudpit_bottlecrm_driver",
        source_kind="sqlite_bottlecrm",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="PTY-",
                    enum_map=dict(er.crm_account_to_party),
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct", default="CN"),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="CrmAccount",
                id_transform=FieldTransformSpec(target_field="account_id", source_field="id", kind="id_prefix", prefix="ACC-"),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="party",
                        source_field="id",
                        kind="foreign_key_id",
                        prefix="PTY-",
                        enum_map=dict(er.crm_account_to_party),
                    ),
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="industry", source_field="industry", kind="direct", default=""),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="opportunity",
                target_object_type="CrmDeal",
                id_transform=FieldTransformSpec(target_field="deal_id", source_field="id", kind="id_prefix", prefix="DEAL-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account", source_field="account_id", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="title", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="amount", source_field="amount", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="stage", source_field="stage", kind="direct", default="PROSPECTING"),
                    FieldTransformSpec(target_field="loss_reason", source_field="description", kind="direct", default=""),
                ],
            ),
            EntityMappingSpec(
                source_table="case",
                target_object_type="CrmTicket",
                id_transform=FieldTransformSpec(target_field="ticket_id", source_field="id", kind="id_prefix", prefix="TK-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account", source_field="account_id", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="subject", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="priority",
                        source_field="priority",
                        kind="enum_map",
                        default="MEDIUM",
                        enum_map={
                            "Urgent": "URGENT",
                            "High": "HIGH",
                            "Medium": "MEDIUM",
                            "Low": "LOW",
                        },
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="status",
                        kind="enum_map",
                        default="OPEN",
                        enum_map={
                            "New": "OPEN",
                            "Assigned": "OPEN",
                            "In Progress": "IN_PROGRESS",
                            "Resolved": "RESOLVED",
                            "Closed": "CLOSED",
                        },
                    ),
                    FieldTransformSpec(target_field="resolution_summary", source_field="description", kind="direct", default=""),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="win_crm_deal",
                external_table="opportunity",
                external_method="stage=CLOSED_WON",
                target_object_type="CrmDeal",
                id_param="deal_id",
                id_prefix="DEAL-",
                expected_state_field="stage",
                expected_state_after="CLOSED_WON",
            ),
            ActionExecutionBinding(
                action_name="lose_crm_deal",
                external_table="opportunity",
                external_method="stage=CLOSED_LOST",
                target_object_type="CrmDeal",
                id_param="deal_id",
                id_prefix="DEAL-",
                expected_state_field="stage",
                expected_state_after="CLOSED_LOST",
            ),
            ActionExecutionBinding(
                action_name="resolve_crm_ticket",
                external_table="case",
                external_method="status=Resolved",
                target_object_type="CrmTicket",
                id_param="ticket_id",
                id_prefix="TK-",
                expected_state_field="status",
                expected_state_after="RESOLVED",
            ),
        ],
    )


def build_odoo_adapter_mapping(er: EntityResolutionTable) -> AdapterMappingSpec:
    """构造 Odoo 19 -> ``AccountingEntity + BusinessParty + O2c*`` 声明式映射。"""
    return AdapterMappingSpec(
        mapping_id="mudpit_odoo19_driver",
        source_kind="odoo19_jsonrpc",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="res.company",
                target_object_type="AccountingEntity",
                id_transform=FieldTransformSpec(target_field="entity_id", source_field="id", kind="id_prefix", prefix="ENT-"),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default="North Operating Entity"),
                    FieldTransformSpec(target_field="base_currency", source_field="currency_code", kind="direct", default="CNY"),
                    FieldTransformSpec(target_field="status", source_field="id", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="res.partner",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="PTY-",
                    enum_map=dict(er.odoo_partner_to_party),
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="tax_id", source_field="vat", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="country_code", kind="direct", default="US"),
                    FieldTransformSpec(target_field="status", source_field="id", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="res.partner",
                target_object_type="O2cCustomerProfile",
                id_transform=FieldTransformSpec(
                    target_field="profile_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="PROF-ERP-",
                    enum_map=dict(er.odoo_partner_to_profile),
                ),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="party",
                        source_field="id",
                        kind="lookup_join",
                        lookup_table="res.partner",
                        lookup_key_field="id",
                        lookup_target_type="BusinessParty",
                    ),
                    FieldTransformSpec(
                        target_field="accounting_entity",
                        source_field="company_id",
                        kind="const",
                        default="ENT-1",
                    ),
                    FieldTransformSpec(target_field="credit_limit", source_field="credit_limit", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="payment_terms", source_field="id", kind="const", default="NET30"),
                    FieldTransformSpec(target_field="status", source_field="id", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="sale.order",
                target_object_type="O2cOrder",
                id_transform=FieldTransformSpec(target_field="order_id", source_field="canonical_order_id", kind="direct"),
                field_transforms=[
                    FieldTransformSpec(target_field="order_number", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="customer",
                        source_field="partner_id",
                        kind="foreign_key_id",
                        prefix="PROF-ERP-",
                        enum_map=dict(er.odoo_partner_to_profile),
                    ),
                    FieldTransformSpec(target_field="accounting_entity", source_field="company_id", kind="const", default="ENT-1"),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct", default=0.0),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="canonical_status",
                        kind="direct",
                        default="draft",
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="stock.picking",
                target_object_type="O2cDelivery",
                id_transform=FieldTransformSpec(target_field="delivery_id", source_field="id", kind="id_prefix", prefix="DEL-"),
                field_transforms=[
                    FieldTransformSpec(target_field="order", source_field="canonical_order_id", kind="direct"),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        default="pending",
                        enum_map={"done": "completed", "cancel": "cancelled"},
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="account.move",
                target_object_type="O2cInvoice",
                id_transform=FieldTransformSpec(target_field="invoice_id", source_field="id", kind="id_prefix", prefix="INV-"),
                field_transforms=[
                    FieldTransformSpec(target_field="order", source_field="canonical_order_id", kind="direct"),
                    FieldTransformSpec(target_field="amount_total", source_field="amount_total", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="amount_residual", source_field="amount_residual", kind="direct", default=0.0),
                    FieldTransformSpec(
                        target_field="payment_state",
                        source_field="payment_state",
                        kind="enum_map",
                        default="not_paid",
                        enum_map={"paid": "paid", "in_payment": "paid", "partial": "partial", "not_paid": "not_paid"},
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="state",
                        kind="enum_map",
                        default="draft",
                        enum_map={"posted": "posted", "cancel": "cancelled", "draft": "draft"},
                    ),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="confirm_o2c_order",
                external_table="sale.order",
                external_method="action_confirm",
                target_object_type="O2cOrder",
                id_param="order_id",
            ),
            ActionExecutionBinding(
                action_name="complete_o2c_delivery",
                external_table="stock.picking",
                external_method="button_validate",
                target_object_type="O2cDelivery",
                id_param="delivery_id",
            ),
            ActionExecutionBinding(
                action_name="post_o2c_invoice",
                external_table="account.move",
                external_method="action_post",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
            ),
            ActionExecutionBinding(
                action_name="record_o2c_payment",
                external_table="account.payment.register",
                external_method="action_create_payments",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
            ),
        ],
    )


def build_composite_adapter_mapping(er: EntityResolutionTable) -> AdapterMappingSpec:
    crm = build_bottlecrm_adapter_mapping(er)
    o2c = build_odoo_adapter_mapping(er)
    return AdapterMappingSpec(
        mapping_id="mudpit_composite_crm_odoo_driver",
        source_kind="composite_crm_erp",
        treat_false_as_null=True,
        entity_mappings=[*crm.entity_mappings, *o2c.entity_mappings],
        action_bindings=[*crm.action_bindings, *o2c.action_bindings],
    )


# ---------------------------------------------------------------------------
# 3. Composite Multi-Device Driver Sandbox (BottleCRM + Odoo 19)
# ---------------------------------------------------------------------------
class CompositeMultiDeviceSandbox:
    """挂载临时副本 BottleCRM SQLite + Ephemeral Odoo 19（或离线镜像）的纯协议复合沙盒。"""

    def __init__(
        self,
        *,
        baseline_crm_db: Path,
        work_crm_db: Path,
        offline: bool = True,
        live_client: OdooJSONRPC | None = None,
    ) -> None:
        self.baseline_crm_db = baseline_crm_db
        self.work_crm_db = work_crm_db
        self.offline = offline
        self._live_client = live_client
        self._offline_world = OfflineOdooPhysicalWorld() if offline else None
        self.harness = PhysicalOdooHarness(
            live_client=None if offline else live_client,
            offline_world=self._offline_world,
        )
        self.er = EntityResolutionTable()
        self.acme_partner_id = 0
        self.northwind_partner_id = 0
        self._init_partners()
        self.crm_mapping = build_bottlecrm_adapter_mapping(self.er)
        self.odoo_mapping = build_odoo_adapter_mapping(self.er)
        self.composite_mapping = build_composite_adapter_mapping(self.er)

        self._order_tags: dict[str, int] = {}
        self._invoice_tags: dict[str, int] = {}
        self._retired_orders: list[int] = []
        self._open_sqlite_bridges: list[GenericSQLiteDriverBridge] = []
        self.sqlite_writes = 0
        self.odoo_writes = 0
        self._odoo_dirty = True
        self._crm_dirty = True

    def _init_partners(self) -> None:
        specs = (
            ("PTY-ACME", "PROF-ACME", "Acme Corporation", "MUD-ACME", "US12345673"),
            ("PTY-NORTHWIND", "PROF-NORTHWIND", "Northwind Manufacturing", "MUD-NW", "US98765432"),
        )
        for pty_id, prof_id, pname, pref, vat in specs:
            if self.harness.is_live:
                assert self._live_client is not None
                matches = self._live_client.search_read(
                    "res.partner", [("name", "=", pname)], ["id", "name", "vat"], limit=5
                )
                if matches:
                    pid = int(matches[0]["id"])
                    vals: dict[str, Any] = {"credit_limit": DEFAULT_CREDIT_LIMIT}
                    if not matches[0].get("vat"):
                        vals["vat"] = vat
                    self._live_client.write("res.partner", [pid], vals)
                else:
                    pid = self.harness.create_partner(pref, pname, credit_limit=DEFAULT_CREDIT_LIMIT)
                    self._live_client.write("res.partner", [pid], {"vat": vat})
            else:
                pid = self.harness.create_partner(pref, pname, credit_limit=DEFAULT_CREDIT_LIMIT)
                assert self._offline_world is not None
                self._offline_world.tables["res.partner"][pid]["vat"] = vat
            self.er.odoo_partner_to_party[str(pid)] = pty_id
            self.er.odoo_partner_to_profile[str(pid)] = prof_id
            if pty_id == "PTY-ACME":
                self.acme_partner_id = pid
            else:
                self.northwind_partner_id = pid

    def _close_sqlite_bridges(self) -> None:
        while self._open_sqlite_bridges:
            bridge = self._open_sqlite_bridges.pop()
            try:
                if bridge.conn.in_transaction:
                    bridge.conn.rollback()
            except Exception:
                pass
            try:
                bridge.close()
            except Exception:
                pass

    def close(self) -> None:
        self._close_sqlite_bridges()

    def set_adapter_mapping(self, adapter_mapping: AdapterMappingSpec) -> None:
        _ = adapter_mapping

    def reset(self) -> None:
        self._close_sqlite_bridges()
        if self._crm_dirty or not self.work_crm_db.exists():
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{self.work_crm_db}{suffix}").unlink(missing_ok=True)
            shutil.copy2(self.baseline_crm_db, self.work_crm_db)
            self._crm_dirty = False

        if self._odoo_dirty or not self._order_tags:
            self._reset_odoo_fixtures()
            self._odoo_dirty = False

    def _reset_odoo_fixtures(self) -> None:
        if self.harness.is_live and self._retired_orders:
            assert self._live_client is not None
            try:
                self._live_client.write("sale.order", list(self._retired_orders), {"active": False})
            except Exception:
                pass
        for oid in self._order_tags.values():
            if oid not in self._retired_orders:
                self._retired_orders.append(oid)
        if self.harness.is_live and self._order_tags:
            assert self._live_client is not None
            try:
                self._live_client.write("sale.order", list(self._order_tags.values()), {"active": False})
            except Exception:
                pass

        tag = uuid.uuid4().hex[:6].upper()
        f_acme_ok = self.harness.create_draft_order(
            partner_id=self.acme_partner_id,
            qty=2,
            unit_price=10_000.0,
            client_ref=f"MUDPIT-{tag}-ACME-OK",
        )
        f_nw_bill = self.harness.create_draft_order(
            partner_id=self.northwind_partner_id,
            qty=1,
            unit_price=15_000.0,
            client_ref=f"MUDPIT-{tag}-NW-BILL",
        )
        self.harness.confirm_order_physically(f_nw_bill.order_id)
        self.harness.ship_order_physically_full(f_nw_bill.order_id)
        self.harness.create_draft_invoice_for_order(f_nw_bill.order_id)

        f_acme_over = self.harness.create_draft_order(
            partner_id=self.acme_partner_id,
            qty=1,
            unit_price=125_000.0,
            client_ref=f"MUDPIT-{tag}-ACME-OVER",
        )
        f_nw_over = self.harness.create_draft_order(
            partner_id=self.northwind_partner_id,
            qty=1,
            unit_price=125_000.0,
            client_ref=f"MUDPIT-{tag}-NW-OVER",
        )
        f_acme_oob = self.harness.create_draft_order(
            partner_id=self.acme_partner_id,
            qty=1,
            unit_price=8_000.0,
            client_ref=f"MUDPIT-{tag}-ACME-OOB",
        )

        self._order_tags = {
            "ORD-MUD-ACME-OK": f_acme_ok.order_id,
            "ORD-MUD-NW-BILL": f_nw_bill.order_id,
            "ORD-MUD-ACME-OVER": f_acme_over.order_id,
            "ORD-MUD-NW-OVER": f_nw_over.order_id,
            "ORD-MUD-ACME-OOB": f_acme_oob.order_id,
        }
        self._invoice_tags = {}

    def order_id_for_suffix(self, suffix: str) -> str:
        for cid in self._order_tags:
            if cid.endswith(suffix):
                return cid
        raise KeyError(f"no tracked order matching {suffix!r}")

    def _invoices_for_order(self, order_name: str) -> list[dict[str, Any]]:
        if self.harness.is_live:
            assert self._live_client is not None
            return self._live_client.search_read(
                "account.move",
                [("move_type", "=", "out_invoice"), ("invoice_origin", "=", order_name)],
                ["id", "name", "state", "payment_state", "amount_total", "amount_residual", "invoice_origin"],
            )
        assert self._offline_world is not None
        return [
            copy.deepcopy(inv)
            for inv in self._offline_world.tables["account.move"].values()
            if inv.get("move_type") == "out_invoice" and inv.get("invoice_origin") == order_name
        ]

    @staticmethod
    def _canonical_order_status(
        orow: dict[str, Any],
        pickings: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
    ) -> str:
        raw_state = str(orow.get("state") or "draft")
        if raw_state == "cancel":
            return "cancelled"
        if raw_state in ("draft", "sent"):
            return "draft"
        if any(inv.get("state") == "posted" for inv in invoices):
            return "invoiced"
        if pickings and all(p.get("state") == "done" for p in pickings):
            return "delivered"
        return "confirmed"

    def _build_odoo_profile(self) -> ExternalPhenomenonProfile:
        company_rows = [{"id": 1, "name": "North Operating Entity", "currency_code": "CNY"}]
        partner_rows: list[dict[str, Any]] = []
        for pid in (self.acme_partner_id, self.northwind_partner_id):
            if self.harness.is_live:
                assert self._live_client is not None
                row = dict(
                    self._live_client.read(
                        "res.partner",
                        [pid],
                        ["id", "name", "vat", "credit_limit", "country_id"],
                    )[0]
                )
            else:
                assert self._offline_world is not None
                row = dict(self._offline_world.tables["res.partner"][pid])
            partner_rows.append(
                {
                    "id": int(row["id"]),
                    "name": str(row.get("name") or ""),
                    "vat": str(row.get("vat") or ""),
                    "credit_limit": float(row.get("credit_limit") or DEFAULT_CREDIT_LIMIT),
                    "country_code": "US",
                    "company_id": 1,
                }
            )

        order_rows: list[dict[str, Any]] = []
        picking_rows: list[dict[str, Any]] = []
        invoice_rows: list[dict[str, Any]] = []
        for cid, oid in sorted(self._order_tags.items()):
            orow = self.harness.read_order(oid)
            pickings = self.harness.list_pickings_for_order(oid)
            invoices = self._invoices_for_order(str(orow["name"]))
            raw_p = orow.get("partner_id")
            partner_id = int(raw_p[0] if isinstance(raw_p, (list, tuple)) else raw_p)
            order_rows.append(
                {
                    "canonical_order_id": cid,
                    "id": oid,
                    "name": str(orow["name"]),
                    "partner_id": partner_id,
                    "company_id": 1,
                    "amount_total": round(float(orow.get("amount_total") or 0.0), 2),
                    "canonical_status": self._canonical_order_status(orow, pickings, invoices),
                }
            )
            for pk in pickings:
                picking_rows.append(
                    {
                        "id": int(pk["id"]),
                        "canonical_order_id": cid,
                        "state": str(pk.get("state") or "assigned"),
                    }
                )
            for inv in invoices:
                iid_num = int(inv["id"])
                self._invoice_tags[f"INV-{iid_num}"] = iid_num
                invoice_rows.append(
                    {
                        "id": iid_num,
                        "canonical_order_id": cid,
                        "amount_total": round(float(inv.get("amount_total") or 0.0), 2),
                        "amount_residual": round(float(inv.get("amount_residual") or 0.0), 2),
                        "payment_state": str(inv.get("payment_state") or "not_paid"),
                        "state": str(inv.get("state") or "draft"),
                    }
                )

        return ExternalPhenomenonProfile(
            source_kind="odoo19_jsonrpc",
            source_locator=DB_NORTH if self.harness.is_live else "offline://erp_north",
            entities=[
                ExternalEntityProfile(
                    table_name="res.company",
                    row_count=len(company_rows),
                    fields=[ExternalFieldProfile(name="id"), ExternalFieldProfile(name="name")],
                    sample_rows=company_rows,
                ),
                ExternalEntityProfile(
                    table_name="res.partner",
                    row_count=len(partner_rows),
                    fields=[
                        ExternalFieldProfile(name="id"),
                        ExternalFieldProfile(name="name"),
                        ExternalFieldProfile(name="vat"),
                        ExternalFieldProfile(name="credit_limit", raw_type="float"),
                    ],
                    sample_rows=partner_rows,
                ),
                ExternalEntityProfile(
                    table_name="sale.order",
                    row_count=len(order_rows),
                    fields=[ExternalFieldProfile(name="canonical_order_id"), ExternalFieldProfile(name="name")],
                    sample_rows=order_rows,
                ),
                ExternalEntityProfile(
                    table_name="stock.picking",
                    row_count=len(picking_rows),
                    fields=[ExternalFieldProfile(name="id"), ExternalFieldProfile(name="state")],
                    sample_rows=picking_rows,
                ),
                ExternalEntityProfile(
                    table_name="account.move",
                    row_count=len(invoice_rows),
                    fields=[ExternalFieldProfile(name="id"), ExternalFieldProfile(name="state")],
                    sample_rows=invoice_rows,
                ),
            ],
        )

    def snapshot_m0(self) -> list[dict[str, Any]]:
        crm_profile = scout_sqlite_device(self.work_crm_db, source_kind="sqlite_bottlecrm")["profile"]
        crm_m0, crm_errs = project_external_phenomenon(
            crm_profile, self.crm_mapping, require_non_empty_per_entity=False
        )
        if crm_errs:
            raise RuntimeError(f"BottleCRM projection errors: {crm_errs}")
        odoo_profile = self._build_odoo_profile()
        odoo_m0, odoo_errs = project_external_phenomenon(
            odoo_profile, self.odoo_mapping, require_non_empty_per_entity=False
        )
        if odoo_errs:
            raise RuntimeError(f"Odoo projection errors: {odoo_errs}")
        return merge_projected_phenomena([crm_m0, odoo_m0])

    def _raw_odoo_id_for_picking(self, pick_id: int) -> int:
        if self.harness.is_live:
            assert self._live_client is not None
            pk = self._live_client.read("stock.picking", [pick_id], ["sale_id"])[0]
        else:
            assert self._offline_world is not None
            pk = self._offline_world.tables["stock.picking"][pick_id]
        sale_ref = pk["sale_id"]
        return int(sale_ref[0] if isinstance(sale_ref, list) else sale_ref)

    def make_source_bridge(
        self,
        spec: OntologySpec,
        adapter_mapping: AdapterMappingSpec,
    ) -> SourceBridge:
        _ = adapter_mapping
        self._close_sqlite_bridges()
        crm_profile = scout_sqlite_device(self.work_crm_db, source_kind="sqlite_bottlecrm")["profile"]
        crm_bridge = GenericSQLiteDriverBridge(
            self.work_crm_db,
            spec=spec,
            profile=crm_profile,
            adapter_mapping=self.crm_mapping,
        )
        self._open_sqlite_bridges.append(crm_bridge)

        def _actuate(mv: dict[str, Any]) -> Any:
            act_name = str(mv.get("action_type") or "")
            params = dict(mv.get("params") or {})
            if act_name in CRM_ACTIONS:
                self._crm_dirty = True
                before_changes = crm_bridge.conn.total_changes
                try:
                    crm_bridge.actuate(mv)
                except Exception:
                    try:
                        if crm_bridge.conn.in_transaction:
                            crm_bridge.conn.rollback()
                    except Exception:
                        pass
                    raise
                delta = crm_bridge.conn.total_changes - before_changes
                if delta > 0:
                    self.sqlite_writes += delta
                return True

            if act_name in O2C_ACTIONS:
                self._odoo_dirty = True
                if act_name == "confirm_o2c_order":
                    oid = self._order_tags[str(params["order_id"])]
                    try:
                        self.harness.confirm_order_physically(oid)
                        self.odoo_writes += 1
                    except (OdooRPCError, RuntimeError):
                        orow = self.harness.read_order(oid)
                        if str(orow.get("state") or "") != "draft":
                            return {"drift_detected_state": orow.get("state")}
                        raise
                    return True
                if act_name == "complete_o2c_delivery":
                    pick_id = int(str(params["delivery_id"]).split("-", 1)[1])
                    sale_id = self._raw_odoo_id_for_picking(pick_id)
                    self.harness.ship_order_physically_full(sale_id)
                    self.odoo_writes += 1
                    return True
                if act_name == "post_o2c_invoice":
                    iid = int(str(params["invoice_id"]).split("-", 1)[1])
                    self.harness.post_invoice_physically(iid)
                    self.odoo_writes += 1
                    return True
                if act_name == "record_o2c_payment":
                    iid = int(str(params["invoice_id"]).split("-", 1)[1])
                    amt = float(params["payment_amount"])
                    self.harness.register_payment_physically(iid, amount=amt)
                    self.odoo_writes += 1
                    return True

            raise ValueError(f"unsupported composite bridge action {act_name!r}")

        def _observe(mv: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
            act_name = str(mv.get("action_type") or "")
            if act_name in CRM_ACTIONS:
                return crm_bridge.observe(mv)
            changes = (mv.get("object_diff") or {}).get("object_changes") or []
            snap_by_key = {
                (str(e["object_type"]), str(e["object_id"])): dict(e["data"])
                for e in self.snapshot_m0()
            }
            observed: dict[str, list[dict[str, Any]]] = {}
            seen: set[tuple[str, str]] = set()
            for ch in changes:
                tname = str(ch.get("object_type") or "")
                oid = str(ch.get("object_id") or "")
                if (tname, oid) in seen or (tname, oid) not in snap_by_key:
                    continue
                seen.add((tname, oid))
                observed.setdefault(tname, []).append(snap_by_key[(tname, oid)])
            return observed

        return SourceBridge(actuate=_actuate, observe=_observe)

    def external_write_count(self) -> int:
        return self.sqlite_writes + self.odoo_writes

    # -- Direct Out-of-Band Physical Helpers for Mudpits 3 & 4 ---------------
    def insert_urgent_crm_ticket_raw(
        self,
        *,
        raw_ticket_id: str,
        account_uuid: str = ACME_BOTTLECRM_ID,
        subject: str = "URGENT: Acme 产线停线故障突袭",
        priority: str = "Urgent",
        status: str = "New",
    ) -> dict[str, Any]:
        """在 BottleCRM SQLite 侧物理插入一条真实工单并返还其 M0 投影字典。"""
        self._crm_dirty = True
        conn = sqlite3.connect(self.work_crm_db)
        conn.row_factory = sqlite3.Row
        try:
            org_row = conn.execute('SELECT org_id FROM "case" LIMIT 1').fetchone()
            org_id = str(org_row["org_id"]) if org_row else "00000000000000000000000000000001"
            conn.execute(
                """
                INSERT INTO "case" (
                    created_at, updated_at, id, name, status, priority, case_type,
                    description, is_active, account_id, org_id, kanban_order,
                    custom_fields, escalation_count, alt_thread_ids,
                    sla_paused_seconds, is_problem, is_sample
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, 'Problem',
                    'Phantom attack window insertion', 1, ?, ?, 1.0,
                    '{}', 0, '[]', 0, 1, 0
                )
                """,
                (raw_ticket_id, subject, status, priority, account_uuid, org_id),
            )
            conn.commit()
            raw = dict(conn.execute('SELECT * FROM "case" WHERE id = ?', (raw_ticket_id,)).fetchone())
        finally:
            conn.close()

        ticket_em = next(em for em in self.crm_mapping.entity_mappings if em.target_object_type == "CrmTicket")
        return {
            "object_type": "CrmTicket",
            "data": project_single_record(ticket_em, raw, treat_false_as_null=True),
        }

    def read_sqlite_ticket_status(self, canonical_ticket_id: str) -> str:
        raw_id = canonical_ticket_id.removeprefix("TK-")
        conn = sqlite3.connect(self.work_crm_db)
        try:
            row = conn.execute('SELECT status FROM "case" WHERE id = ?', (raw_id,)).fetchone()
            assert row is not None
            return str(row[0])
        finally:
            conn.close()

    def cancel_odoo_order_out_of_band(self, canonical_order_id: str) -> str:
        self._odoo_dirty = True
        ext_id = self._order_tags[canonical_order_id]
        if self.harness.is_live:
            assert self._live_client is not None
            try:
                self._live_client.execute_kw("sale.order", "action_cancel", [[ext_id]])
            except OdooRPCError:
                self._live_client.execute_kw("sale.order", "_action_cancel", [[ext_id]])
        else:
            assert self._offline_world is not None
            self._offline_world.call_method("sale.order", "action_cancel", [ext_id])
        return self.read_odoo_order_state(canonical_order_id)

    def read_odoo_order_state(self, canonical_order_id: str) -> str:
        ext_id = self._order_tags[canonical_order_id]
        return str(self.harness.read_order(ext_id)["state"])

    def resync_runtime(self, runtime: OntologyRuntime) -> list[dict[str, Any]]:
        """根据最新外设快照重同步指定 M1 对象。"""
        m0 = self.snapshot_m0()
        for entry in m0:
            tname = str(entry["object_type"])
            cls = runtime.spec.get_object_type(tname)
            runtime.put_object(cls(**entry["data"]))
        return m0


# ---------------------------------------------------------------------------
# 4. Mock HTTP Transport for Feishu OpenAPI Verification
# ---------------------------------------------------------------------------
class MockFeishuCardTransport:
    """拦截 ``FeishuOpenAPIClient`` 真实 HTTP POST/PATCH 并记录完整报文以便断言验签与终态渲染。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._seq = 0

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        _ = timeout
        payload = json.loads(body.decode("utf-8")) if body else {}
        rec: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "payload": payload,
        }
        if "content" in payload and isinstance(payload["content"], str):
            rec["decoded_card"] = json.loads(payload["content"])
        self.calls.append(rec)

        if url.endswith("/tenant_access_token/internal"):
            return 200, json.dumps(
                {"code": 0, "tenant_access_token": "t-mudpit-token", "expire": 7200}
            ).encode("utf-8")

        if headers.get("Authorization") != "Bearer t-mudpit-token":
            return 401, json.dumps({"code": 99991663, "msg": "missing bearer token"}).encode("utf-8")

        if method == "POST" and "/open-apis/im/v1/messages" in url:
            self._seq += 1
            return 200, json.dumps(
                {"code": 0, "data": {"message_id": f"om_mudpit_{self._seq}"}}
            ).encode("utf-8")

        if method == "PATCH" and "/open-apis/im/v1/messages/" in url:
            return 200, json.dumps({"code": 0, "data": {}}).encode("utf-8")

        return 404, json.dumps({"code": 404, "msg": "unknown endpoint"}).encode("utf-8")


# ---------------------------------------------------------------------------
# 5. End-to-End 6-Mudpit Orchestrator
# ---------------------------------------------------------------------------
def _load_m0_into_runtime(rt: OntologyRuntime, spec: OntologySpec, m0: list[dict[str, Any]]) -> None:
    order = {name: idx for idx, name in enumerate(spec.object_types)}
    for entry in sorted(m0, key=lambda e: order.get(str(e["object_type"]), 9999)):
        cls = spec.get_object_type(str(entry["object_type"]))
        rt.put_object(cls(**entry["data"]))


def _read_version(rt: OntologyRuntime, obj_type: str, obj_id: str) -> int:
    row = rt.connection.execute(
        "SELECT version FROM ontology_objects WHERE object_type = ? AND object_id = ?",
        (obj_type, obj_id),
    ).fetchone()
    assert row is not None
    return int(row["version"])


def _run_with_sandbox(
    sandbox: CompositeMultiDeviceSandbox,
    *,
    live_mode: bool,
) -> dict[str, Any]:
    effective_bp = build_mudpit_effective_blueprint()
    spec = compile_blueprint(effective_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    sandbox.reset()
    m0 = sandbox.snapshot_m0()
    bridge = sandbox.make_source_bridge(spec, sandbox.composite_mapping)
    rt = OntologyRuntime(spec, db_path=":memory:", world_mode="coupled", source_bridge=bridge)

    try:
        _load_m0_into_runtime(rt, spec, m0)
        sys_actor = Actor(actor_id="__verify__", role="system", clearance_tags={"*"})

        # --- Mudpit 1: Verify Entity Resolution + Cross-System Link Coalescing ---
        pty_acme = rt.get_object(spec.get_object_type("BusinessParty"), "PTY-ACME", sys_actor)
        acc_acme = rt.get_object(spec.get_object_type("CrmAccount"), f"ACC-{ACME_BOTTLECRM_ID}", sys_actor)
        prof_acme = rt.get_object(spec.get_object_type("O2cCustomerProfile"), "PROF-ACME", sys_actor)
        assert pty_acme.legal_name == "Acme China Manufacturing"
        assert pty_acme.tax_id == "US12345673"
        assert acc_acme.party == "PTY-ACME"
        assert prof_acme.party == "PTY-ACME"

        # --- Mudpit 2: 2-Hop Overlay Guard Blocks While High BottleCRM Ticket Open ---
        ok_oid = sandbox.order_id_for_suffix("-ACME-OK")
        odoo_before_m2 = sandbox.odoo_writes
        m2_block_code = ""
        try:
            rt.submit_action("MUD2-BLK", "confirm_o2c_order", {"order_id": ok_oid}, SALES_REP_ACTOR)
        except InvariantError as exc:
            m2_block_code = str(exc.code or "")
        assert m2_block_code == "OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER"
        assert sandbox.odoo_writes == odoo_before_m2
        assert sandbox.read_odoo_order_state(ok_oid) == "draft"

        # Resolve initial Acme High ticket -> unlocks ok_oid & sets clean t0 baseline
        res_tk0 = rt.submit_action(
            "MUD2-UNBLOCK-TK",
            "resolve_crm_ticket",
            {"ticket_id": ACME_EXISTING_TICKET_ID, "resolution_summary": "现场已更换破损外箱"},
            SUPPORT_ACTOR,
        )
        assert res_tk0["status"] == "COMMITTED"
        assert sandbox.read_sqlite_ticket_status(ACME_EXISTING_TICKET_ID) == "Resolved"

        # --- Mudpit 3: Feishu WorkCard Cross-System Phantom-Read Attack ---
        over_oid = sandbox.order_id_for_suffix("-ACME-OVER")
        transport = MockFeishuCardTransport()
        feishu_cli = FeishuOpenAPIClient(
            app_id="cli_mudpit",
            app_secret="sec_mudpit",
            receive_id="oc_mudpit_exec",
            receive_id_type="chat_id",
            transport=transport,
        )
        gw = FeishuApprovalGateway(
            rt,
            feishu_client=feishu_cli,
            operator_actors={
                "ou_ai_requester": AI_AGENT_ACTOR,
                "ou_ops_requester": OPS_ACTOR,
                "ou_steward_exec": STEWARD_ACTOR,
            },
            default_approver=STEWARD_ACTOR,
            strict_operator_map=True,
            encrypt_key="mudpit-enc-key",
            callback_token="mudpit-cb-token",
        )

        # t0: ops-01 triggers APPROVAL_REQUIRED_CREDIT_LIMIT_EXCEEDED -> opens WorkCard & POSTs card
        t0_esc = gw.submit_or_escalate(
            "RUN-MUD3-OVER",
            "confirm_o2c_order",
            {"order_id": over_oid},
            OPS_ACTOR,
            assignee_role="steward",
            target_object_type="O2cOrder",
        )
        assert t0_esc["status"] == "OPEN"
        assert t0_esc["error_code"] == "APPROVAL_REQUIRED_CREDIT_LIMIT_EXCEEDED"
        card_rec = gw.get_card_record("RUN-MUD3-OVER")
        assert card_rec is not None and card_rec.message_id is not None
        assert card_rec.card_view["changes_summary"], "Expected non-empty Before->After diff preview on WorkCard"
        post_msgs = [c for c in transport.calls if c["method"] == "POST" and "/im/v1/messages" in c["url"]]
        assert len(post_msgs) == 1
        assert post_msgs[0]["decoded_card"]["header"]["template"] == "orange"

        ord_ver_t0 = _read_version(rt, "O2cOrder", over_oid)
        prof_ver_t0 = _read_version(rt, "O2cCustomerProfile", "PROF-ACME")
        wc_cls = spec.get_object_type("WorkCard")
        assert rt.get_object(wc_cls, "RUN-MUD3-OVER", sys_actor).target_object_type == "O2cOrder"

        # t0 -> t1 Window Attack: raw BottleCRM INSERT of Urgent ticket on same BusinessParty PTY-ACME
        phantom_raw_id = f"phantom{uuid.uuid4().hex[:24]}"
        phantom_m0 = sandbox.insert_urgent_crm_ticket_raw(raw_ticket_id=phantom_raw_id)
        tk_cls = spec.get_object_type("CrmTicket")
        rt.put_object(tk_cls(**phantom_m0["data"]))
        phantom_tk_id = phantom_m0["data"]["ticket_id"]

        # Verify Odoo O2cOrder & O2cCustomerProfile versions did NOT change!
        assert _read_version(rt, "O2cOrder", over_oid) == ord_ver_t0
        assert _read_version(rt, "O2cCustomerProfile", "PROF-ACME") == prof_ver_t0

        # Bad signature check + unauthorized resolver hijack check + Self-approval SoD check
        raw_ai_body = json.dumps(
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "ou_ai_requester"},
                    "action": {"value": {"op": "approve", "run_id": "RUN-MUD3-OVER", "token": "mudpit-cb-token"}},
                },
            }
        ).encode("utf-8")
        bad_sig_caught = False
        try:
            gw.handle_feishu_callback(
                json.loads(raw_ai_body),
                headers={"x-lark-request-timestamp": "1700000000", "x-lark-request-nonce": "n1", "x-lark-signature": "bad"},
                raw_body=raw_ai_body,
            )
        except PermissionDeniedError:
            bad_sig_caught = True
        assert bad_sig_caught is True

        ai_sig = hashlib.sha256(b"1700000000n1mudpit-enc-key" + raw_ai_body).hexdigest()
        ai_hijack_res = gw.handle_feishu_callback(
            json.loads(raw_ai_body),
            headers={"x-lark-request-timestamp": "1700000000", "x-lark-request-nonce": "n1", "x-lark-signature": ai_sig},
            raw_body=raw_ai_body,
        )
        assert ai_hijack_res.get("error_code") == "WORK_CARD_RESOLVER_FORBIDDEN"

        raw_sod_body = json.dumps(
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "ou_ops_requester"},
                    "action": {"value": {"op": "approve", "run_id": "RUN-MUD3-OVER", "token": "mudpit-cb-token"}},
                },
            }
        ).encode("utf-8")
        sod_sig = hashlib.sha256(b"1700000000n1b" + b"mudpit-enc-key" + raw_sod_body).hexdigest()
        sod_res = gw.handle_feishu_callback(
            json.loads(raw_sod_body),
            headers={"x-lark-request-timestamp": "1700000000", "x-lark-request-nonce": "n1b", "x-lark-signature": sod_sig},
            raw_body=raw_sod_body,
        )
        assert sod_res.get("error_code") == "SOD_SELF_APPROVAL_FORBIDDEN"

        # t1 Steward approves on Feishu card -> live recomputation MUST catch BottleCRM urgent ticket!
        steward_body = json.dumps(
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "ou_steward_exec"},
                    "action": {"value": {"op": "approve", "run_id": "RUN-MUD3-OVER", "token": "mudpit-cb-token"}},
                },
            }
        ).encode("utf-8")
        stew_sig = hashlib.sha256(b"1700000001n2mudpit-enc-key" + steward_body).hexdigest()
        odoo_before_phantom = sandbox.odoo_writes
        t1_blocked_res = gw.handle_feishu_callback(
            json.loads(steward_body),
            headers={"x-lark-request-timestamp": "1700000001", "x-lark-request-nonce": "n2", "x-lark-signature": stew_sig},
            raw_body=steward_body,
        )
        assert t1_blocked_res.get("error_code") == "OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER"
        assert sandbox.odoo_writes == odoo_before_phantom
        assert sandbox.read_odoo_order_state(over_oid) == "draft"
        assert rt.get_object(wc_cls, "RUN-MUD3-OVER", sys_actor).status == "OPEN"

        # t2 Support closes Urgent ticket in BottleCRM -> Steward clicks Approve AGAIN on SAME Feishu card!
        rt.submit_action(
            "MUD3-RESOLVE-PHANTOM",
            "resolve_crm_ticket",
            {"ticket_id": phantom_tk_id, "resolution_summary": "已隔离备件并完成复检"},
            SUPPORT_ACTOR,
        )
        assert sandbox.read_sqlite_ticket_status(phantom_tk_id) == "Resolved"

        t2_settled = gw.handle_feishu_callback(
            json.loads(steward_body),
            headers={"x-lark-request-timestamp": "1700000002", "x-lark-request-nonce": "n3", "x-lark-signature": hashlib.sha256(b"1700000002n3mudpit-enc-key" + steward_body).hexdigest()},
            raw_body=steward_body,
        )
        assert t2_settled.get("status") == "COMMITTED"
        assert sandbox.read_odoo_order_state(over_oid) == "sale"
        assert rt.get_object(wc_cls, "RUN-MUD3-OVER", sys_actor).status == "COMPLETED"
        patch_msgs = [c for c in transport.calls if c["method"] == "PATCH"]
        assert len(patch_msgs) >= 1
        assert patch_msgs[-1]["decoded_card"]["header"]["template"] == "green"

        # --- Mudpit 4: Out-of-Band Odoo Mutation (action_cancel) & DEVICE_STATE_MISMATCH ---
        oob_oid = sandbox.order_id_for_suffix("-ACME-OOB")
        raw_oob_state = sandbox.cancel_odoo_order_out_of_band(oob_oid)
        assert raw_oob_state == "cancel"

        fp_before_oob = rt.state_fingerprint()
        mismatch_caught: DeviceStateMismatchError | None = None
        try:
            rt.submit_action("MUD4-OOB-CONFIRM", "confirm_o2c_order", {"order_id": oob_oid}, STEWARD_ACTOR)
        except DeviceStateMismatchError as exc:
            mismatch_caught = exc
        assert mismatch_caught is not None
        assert mismatch_caught.run["error_code"] == DEVICE_STATE_MISMATCH
        fp_after_oob = rt.state_fingerprint()
        assert fp_after_oob["versions"] == fp_before_oob["versions"]
        assert fp_after_oob["objects"] == fp_before_oob["objects"]
        assert fp_after_oob["links"] == fp_before_oob["links"]
        assert rt.get_object(spec.get_object_type("O2cOrder"), oob_oid, sys_actor).status == "draft"

        sandbox.resync_runtime(rt)
        assert rt.get_object(spec.get_object_type("O2cOrder"), oob_oid, sys_actor).status == "cancelled"

        # --- Mudpit 5: Composed Dual-Role ScopedView Isolation ---
        full_exec = AIPAgentExecutor(rt, STEWARD_ACTOR, view=None)
        crm_exec = AIPAgentExecutor(rt, SALES_REP_ACTOR, view="crm_ops_view", enforce_view_scope=True)
        o2c_exec = AIPAgentExecutor(rt, SALES_REP_ACTOR, view="o2c_ops_view", enforce_view_scope=True)

        def _schema_chars(ex: AIPAgentExecutor) -> int:
            bundle = {"tbox": ex.describe_tbox(), "tools": ex.tools()}
            return len(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))

        full_chars = _schema_chars(full_exec)
        crm_ratio = _schema_chars(crm_exec) / full_chars
        o2c_ratio = _schema_chars(o2c_exec) / full_chars
        assert crm_ratio < 0.65, f"crm_ops_view ratio {crm_ratio:.2%} >= 65%"
        assert o2c_ratio < 0.65, f"o2c_ops_view ratio {o2c_ratio:.2%} >= 65%"

        crm_blocked_inv = False
        try:
            crm_exec.dispatch("query_objects", {"object_type": "O2cInvoice"})
        except PermissionDeniedError:
            crm_blocked_inv = True
        crm_blocked_confirm = False
        try:
            crm_exec.dispatch("submit_action", {"run_id": "MUD5-BLK-1", "action": "confirm_o2c_order", "params": {"order_id": ok_oid}})
        except PermissionDeniedError:
            crm_blocked_confirm = True
        o2c_blocked_tk = False
        try:
            o2c_exec.dispatch("query_objects", {"object_type": "CrmTicket"})
        except PermissionDeniedError:
            o2c_blocked_tk = True
        assert crm_blocked_inv and crm_blocked_confirm and o2c_blocked_tk

        # Verify shared cross-system invariant STILL works through o2c_ops_view after re-opening urgent ticket
        new_tk_raw = f"scoped{uuid.uuid4().hex[:25]}"
        new_tk_m0 = sandbox.insert_urgent_crm_ticket_raw(raw_ticket_id=new_tk_raw)
        rt.put_object(tk_cls(**new_tk_m0["data"]))
        scoped_block_code = ""
        try:
            o2c_exec.dispatch("submit_action", {"run_id": "MUD5-SHARED-BLK", "action": "confirm_o2c_order", "params": {"order_id": ok_oid}})
        except InvariantError as exc:
            scoped_block_code = str(exc.code or "")
        assert scoped_block_code == "OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER"

    finally:
        rt.close()

    # --- Mudpit 6: Full 7-Action DifferentialFalsifier Across Both Real Backends ---
    sandbox.reset()
    probes = [
        AdversarialProbe(
            name="MUD6-ADV-CRM-BLOCKS-O2C",
            action="confirm_o2c_order",
            params={"order_id": sandbox.order_id_for_suffix("-ACME-OK")},
            actor=STEWARD_ACTOR,
            expect_valid=False,
            expect_error_code="OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER",
        ),
        AdversarialProbe(
            name="MUD6-ADV-OVER-CREDIT-GATE",
            action="confirm_o2c_order",
            params={"order_id": sandbox.order_id_for_suffix("-NW-OVER")},
            actor=SALES_REP_ACTOR,
            expect_valid=False,
            expect_error_code="APPROVAL_REQUIRED_CREDIT_LIMIT_EXCEEDED",
        ),
    ]
    falsifier = DifferentialFalsifier(
        min_action_coverage=1.0,
        min_transition_coverage=0.8,
        adversarial_probes=probes,
        max_waves=3,
    )
    fal_report: DifferentialFalsificationReport = falsifier.evaluate(
        blueprint=effective_bp,
        adapter_mapping=sandbox.composite_mapping,
        sandbox=sandbox,
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        target_actions=(
            "win_crm_deal",
            "lose_crm_deal",
            "resolve_crm_ticket",
            "confirm_o2c_order",
            "complete_o2c_delivery",
            "post_o2c_invoice",
            "record_o2c_payment",
        ),
    )
    assert fal_report.verdict == "CORROBORATED", fal_report.diagnostic_feedback()
    assert fal_report.action_coverage_ratio == 1.0
    assert fal_report.transition_coverage_ratio >= 0.8
    assert fal_report.adversarial_escapes == 0
    assert fal_report.differential_mismatches == 0
    assert fal_report.dirty_external_writes == 0

    return {
        "live_mode": live_mode,
        "pty_acme_legal_name": pty_acme.legal_name,
        "pty_acme_tax_id": pty_acme.tax_id,
        "crm_view_token_ratio": round(crm_ratio, 4),
        "o2c_view_token_ratio": round(o2c_ratio, 4),
        "sqlite_writes": sandbox.sqlite_writes,
        "odoo_writes": sandbox.odoo_writes,
        "feishu_http_posts": len(post_msgs),
        "feishu_http_patches": len(patch_msgs),
        "falsification": fal_report.to_dict(),
    }


def run_mudpit_stress_suite(*, offline: bool = True) -> dict[str, Any]:
    """运行三外设联合泥潭实战压测（支持 ``offline=True`` 与 Live Odoo 19 克隆库 ``offline=False``）。"""
    src_sha_before = file_sha256(BOTTLECRM_SQLITE_PATH)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        base_crm = tmp / "baseline_bottlecrm.sqlite3"
        work_crm = tmp / "work_bottlecrm.sqlite3"
        shutil.copy2(BOTTLECRM_SQLITE_PATH, base_crm)

        if offline:
            sandbox = CompositeMultiDeviceSandbox(
                baseline_crm_db=base_crm,
                work_crm_db=work_crm,
                offline=True,
            )
            try:
                out = _run_with_sandbox(sandbox, live_mode=False)
            finally:
                sandbox.close()
        else:
            with EphemeralOdooSandbox(source_db=DB_NORTH) as sb_client:
                sb_client._ensure_login()
                sandbox = CompositeMultiDeviceSandbox(
                    baseline_crm_db=base_crm,
                    work_crm_db=work_crm,
                    offline=False,
                    live_client=sb_client._client,
                )
                try:
                    out = _run_with_sandbox(sandbox, live_mode=True)
                finally:
                    sandbox.close()

    assert file_sha256(BOTTLECRM_SQLITE_PATH) == src_sha_before
    out["bottlecrm_sha256_preserved"] = True
    return out
