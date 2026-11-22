"""标准核心底座纯业务本体（Std Core Ontology · Pure M1）。

定义跨标准域（CRM / O2C / 等）共享的最小企业骨干对象与协同原语：
* ``AccountingEntity`` —— 法人/核算主体维度（继承 ``MasterEntity``）；
* ``BusinessParty`` —— 极简共同往来主体（不超过 6 个字段，反上帝对象）；
* ``WorkCard`` —— 统一协同审批/人工任务/跨部门工单卡及 6 个生命周期同步动作。

子域本体通过 ``spec = OntologySpec(extends=(std_core_spec,))`` 与
``compose_blueprints([build_std_core_blueprint(), ...])`` 复用意图相同的标准底座，
确保菱形合并（Diamond Import）幂等去重且零冲突。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from open_ontology.induction.dsl import DeclarativeBlueprint
from open_ontology.spec import ObjectProperty, OntologySpec

try:
    from .business_ontology import (
        MasterEntity,
        ProcessDocument,
        WorkCard,
        build_work_card_action_blueprints,
        build_work_card_invariant_blueprints,
        build_work_card_object_blueprint,
        register_work_card_primitives,
        spec as business_spec,
    )
except ImportError:
    from examples.business_ontology import (
        MasterEntity,
        ProcessDocument,
        WorkCard,
        build_work_card_action_blueprints,
        build_work_card_invariant_blueprints,
        build_work_card_object_blueprint,
        register_work_card_primitives,
        spec as business_spec,
    )

__all__ = [
    "STD_ROLES",
    "STD_WORK_CARD_OPENERS",
    "STD_WORK_CARD_RESOLVERS",
    "FulfillmentEventKind",
    "spec",
    "AccountingEntity",
    "BusinessParty",
    "TradeResource",
    "TradeCommitment",
    "FulfillmentEvent",
    "WorkCard",
    "build_std_core_blueprint",
]

FulfillmentEventKind = Literal[
    "SHIPMENT",
    "RECEIPT",
    "INSPECTION",
    "INVOICE",
    "PAYMENT",
    "PRODUCTION_ISSUE",
    "PRODUCTION_OUTPUT",
    "JOURNAL_POST",
]

STD_ROLES: tuple[tuple[str, str], ...] = (
    ("admin", "系统管理员：拥有全量管理与核准权限"),
    ("steward", "本体治理专员：负责高风险审批与标准契约治理"),
    ("ai_agent", "智能体执行者：在护栏与最小视图内预演并提交标准动作"),
    ("ops", "运营协同专员：负责跨环节履约协同与异常跟进"),
    ("sales_rep", "销售代表：负责商机推进、成单/丢单申报与订单确认"),
    ("sales_manager", "销售主管：负责高价值商机风控审批与团队协同"),
    ("support_agent", "客服支持专员：负责客户服务工单处理与解决确认"),
    ("warehouse", "仓储履约专员：负责发货履约与收货入库确认"),
    ("buyer", "采购专员：负责采购订单确认、收货跟进与对账协同"),
    ("procurement_manager", "采购经理：负责采购争议裁决、异常审批与放行"),
    ("finance", "财务专员：负责应收/应付发票过账与收付款核销"),
    ("finance_director", "财务总监：负责高风险财务放行与大额核准"),
    ("production_planner", "生产计划员：负责工单排产下达、BOM 维护与工单关闭"),
    ("shopfloor", "车间报工专员：负责生产领料投料与完工报工记录"),
    ("plant_manager", "厂长/生产总监：负责生产异常损耗超标审批与工单放行"),
    ("chief_accountant", "财务主管/总账会计：负责总账凭证过账复核、红冲与会计期间结账"),
    ("hr_admin", "组织人事专员：负责部门架构维护、岗位管理与员工入转调离"),
    ("hr_director", "人力总监：负责越权授权审批、高阶岗位定编与组织权限治理"),
    ("system", "系统只读角色：用于内部诊断与不变量视图"),
)

STD_WORK_CARD_OPENERS: tuple[str, ...] = (
    "ai_agent",
    "sales_rep",
    "support_agent",
    "ops",
    "warehouse",
    "buyer",
    "procurement_manager",
    "finance",
    "production_planner",
    "shopfloor",
    "chief_accountant",
    "hr_admin",
    "steward",
    "admin",
)

STD_WORK_CARD_RESOLVERS: tuple[str, ...] = (
    "steward",
    "sales_manager",
    "procurement_manager",
    "finance_director",
    "plant_manager",
    "chief_accountant",
    "hr_director",
    "warehouse",
    "ops",
    "admin",
)

_TRADE_COMMITMENT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("agreed", "disputed", "cancelled"),
    "agreed": ("fulfilled", "disputed", "cancelled"),
    "disputed": ("agreed", "fulfilled", "cancelled"),
    "fulfilled": (),
    "cancelled": (),
}

_FULFILLMENT_EVENT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "recorded": ("confirmed", "disputed"),
    "confirmed": (),
    "disputed": (),
}

spec = OntologySpec(extends=(business_spec,))

for _role_name, _role_desc in STD_ROLES:
    spec.role(_role_name, _role_desc)


@spec.object_type
class AccountingEntity(MasterEntity):
    """法人核算主体主数据（最小标准底座维度）。"""

    __primary_key__: ClassVar[str] = "entity_id"
    __title__: ClassVar[str] = "name"

    entity_id: str
    name: str
    base_currency: str = "CNY"
    status: str = "active"


@spec.object_type
class BusinessParty(MasterEntity):
    """共同往来业务伙伴（<= 6 字段，极简反上帝对象）。"""

    __primary_key__: ClassVar[str] = "party_id"
    __title__: ClassVar[str] = "legal_name"

    party_id: str
    legal_name: str
    tax_id: str = ""
    country_code: str = "CN"
    status: str = "active"


@spec.object_type
class TradeResource(MasterEntity):
    """REA 交易标的资源原语（跨企业交互中性词表，<= 6 字段）。"""

    __primary_key__: ClassVar[str] = "resource_id"
    __title__: ClassVar[str] = "name"
    __aggregate_root__: ClassVar[bool] = True

    resource_id: str
    code: str = ""
    name: str
    unit: str = "EA"
    spec_summary: str = ""
    status: str = "active"


@spec.object_type
class TradeCommitment(ProcessDocument):
    """REA 交易承诺契约原语（买卖双方达成的数量/单价/总额共识）。"""

    __primary_key__: ClassVar[str] = "commitment_id"
    __title__: ClassVar[str] = "commitment_id"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "quantity",
        "unit_price",
        "total_amount",
    )
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("proposed", "agreed")
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _TRADE_COMMITMENT_TRANSITIONS

    commitment_id: str
    buyer_party: ObjectProperty[BusinessParty]
    seller_party: ObjectProperty[BusinessParty]
    resource: ObjectProperty[TradeResource]
    quantity: float = 0.0
    unit_price: float = 0.0
    total_amount: float = 0.0
    status: str = "proposed"


@spec.object_type
class FulfillmentEvent(ProcessDocument):
    """REA 单调履约事实原语（发运/收货/质检/开票/付款不可逆终态确认或争议标记）。"""

    __primary_key__: ClassVar[str] = "event_id"
    __title__: ClassVar[str] = "event_id"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("quantity", "amount")
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("recorded", "confirmed", "disputed")
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _FULFILLMENT_EVENT_TRANSITIONS

    event_id: str
    commitment: ObjectProperty[TradeCommitment]
    event_kind: FulfillmentEventKind = "SHIPMENT"
    quantity: float = 0.0
    amount: float = 0.0
    evidence_summary: str = ""
    status: str = "recorded"


register_work_card_primitives(
    spec,
    opener_roles=STD_WORK_CARD_OPENERS,
    resolver_roles=STD_WORK_CARD_RESOLVERS,
)

spec.update_display(
    {
        "ontologies": {
            "std_core_ontology": "企业标准核心底座本体（REA五原语常识细腰）",
        },
        "objects": {
            "AccountingEntity": "法人核算主体",
            "BusinessParty": "往来业务伙伴",
            "TradeResource": "交易标的资源",
            "TradeCommitment": "交易承诺契约",
            "FulfillmentEvent": "履约事实凭证",
        },
        "fields": {
            "entity_id": "核算主体编码",
            "name": "名称",
            "base_currency": "本位币",
            "party_id": "往来伙伴编码",
            "legal_name": "法定名称",
            "tax_id": "统一社会信用代码/税号",
            "country_code": "国家/地区代码",
            "resource_id": "标的资源编码",
            "code": "标准编码",
            "unit": "计量单位",
            "spec_summary": "规格摘要",
            "commitment_id": "交易承诺编码",
            "buyer_party": "买方伙伴",
            "seller_party": "卖方伙伴",
            "resource": "标的资源",
            "quantity": "数量",
            "unit_price": "单价",
            "total_amount": "总金额",
            "event_id": "履约事实编码",
            "commitment": "所属承诺",
            "event_kind": "履约事件类型",
            "amount": "金额",
            "evidence_summary": "凭证摘要",
            "status": "状态",
        },
    }
)


def build_std_core_blueprint() -> DeclarativeBlueprint:
    """构建与 ``std_core_ontology`` 等价的零 ``exec/eval`` 声明式核心标准蓝图。"""
    payload: dict[str, Any] = {
        "model_id": "std_core_ontology",
        "version": "1.0.0",
        "seed_ontology_id": "business_ontology",
        "imports": [],
        "roles": [r for r, _ in STD_ROLES],
        "description": "企业标准核心底座蓝图：核算主体、往来伙伴、REA 交易资源/承诺/履约事实与 WorkCard。",
        "object_types": [
            {
                "name": "AccountingEntity",
                "archetype": "MasterEntity",
                "primary_key": "entity_id",
                "title_field": "name",
                "aggregate_root": True,
                "owned": False,
                "display_label": "法人核算主体",
                "fields": [
                    {"name": "entity_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "name", "field_type": "str", "required": True},
                    {"name": "base_currency", "field_type": "str", "default": "CNY"},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
            },
            {
                "name": "BusinessParty",
                "archetype": "MasterEntity",
                "primary_key": "party_id",
                "title_field": "legal_name",
                "aggregate_root": True,
                "owned": False,
                "display_label": "往来业务伙伴",
                "fields": [
                    {"name": "party_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "legal_name", "field_type": "str", "required": True},
                    {"name": "tax_id", "field_type": "str", "default": ""},
                    {"name": "country_code", "field_type": "str", "default": "CN"},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
            },
            {
                "name": "TradeResource",
                "archetype": "MasterEntity",
                "primary_key": "resource_id",
                "title_field": "name",
                "aggregate_root": True,
                "owned": False,
                "display_label": "交易标的资源",
                "fields": [
                    {"name": "resource_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "code", "field_type": "str", "default": ""},
                    {"name": "name", "field_type": "str", "required": True},
                    {"name": "unit", "field_type": "str", "default": "EA"},
                    {"name": "spec_summary", "field_type": "str", "default": ""},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
            },
            {
                "name": "TradeCommitment",
                "archetype": "ProcessDocument",
                "primary_key": "commitment_id",
                "title_field": "commitment_id",
                "aggregate_root": True,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["proposed", "agreed"],
                "transitions": {k: list(v) for k, v in _TRADE_COMMITMENT_TRANSITIONS.items()},
                "display_label": "交易承诺契约",
                "fields": [
                    {"name": "commitment_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "quantity", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "unit_price", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "total_amount", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "proposed"},
                ],
                "relations": [
                    {"field": "buyer_party", "target_type": "BusinessParty", "required": True},
                    {"field": "seller_party", "target_type": "BusinessParty", "required": True},
                    {"field": "resource", "target_type": "TradeResource", "required": True},
                ],
            },
            {
                "name": "FulfillmentEvent",
                "archetype": "ProcessDocument",
                "primary_key": "event_id",
                "title_field": "event_id",
                "aggregate_root": True,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["recorded", "confirmed", "disputed"],
                "transitions": {k: list(v) for k, v in _FULFILLMENT_EVENT_TRANSITIONS.items()},
                "display_label": "履约事实凭证",
                "fields": [
                    {"name": "event_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "event_kind", "field_type": "str", "default": "SHIPMENT"},
                    {"name": "quantity", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "amount", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "evidence_summary", "field_type": "str", "default": ""},
                    {"name": "status", "field_type": "str", "default": "recorded"},
                ],
                "relations": [
                    {"field": "commitment", "target_type": "TradeCommitment", "required": True},
                ],
            },
            build_work_card_object_blueprint(),
        ],
        "invariants": build_work_card_invariant_blueprints(),
        "actions": build_work_card_action_blueprints(
            opener_roles=STD_WORK_CARD_OPENERS,
            resolver_roles=STD_WORK_CARD_RESOLVERS,
        ),
        "display": {
            "ontologies": {
                "std_core_ontology": "企业标准核心底座本体（REA五原语常识细腰）",
            },
            "objects": {
                "AccountingEntity": "法人核算主体",
                "BusinessParty": "往来业务伙伴",
                "TradeResource": "交易标的资源",
                "TradeCommitment": "交易承诺契约",
                "FulfillmentEvent": "履约事实凭证",
                "WorkCard": "协同审批工单卡",
            },
        },
    }
    return DeclarativeBlueprint.model_validate(payload)
