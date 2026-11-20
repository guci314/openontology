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

from typing import Any, ClassVar

from open_ontology.induction.dsl import DeclarativeBlueprint
from open_ontology.spec import OntologySpec

try:
    from .business_ontology import (
        MasterEntity,
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
    "spec",
    "AccountingEntity",
    "BusinessParty",
    "WorkCard",
    "build_std_core_blueprint",
]

STD_ROLES: tuple[tuple[str, str], ...] = (
    ("admin", "系统管理员：拥有全量管理与核准权限"),
    ("steward", "本体治理专员：负责高风险审批与标准契约治理"),
    ("ai_agent", "智能体执行者：在护栏与最小视图内预演并提交标准动作"),
    ("ops", "运营协同专员：负责跨环节履约协同与异常跟进"),
    ("sales_rep", "销售代表：负责商机推进、成单/丢单申报与订单确认"),
    ("sales_manager", "销售主管：负责高价值商机风控审批与团队协同"),
    ("support_agent", "客服支持专员：负责客户服务工单处理与解决确认"),
    ("warehouse", "仓储履约专员：负责发货履约完成确认"),
    ("finance", "财务专员：负责应收发票过账与收款核销"),
    ("finance_director", "财务总监：负责高风险财务放行与大额核准"),
    ("system", "系统只读角色：用于内部诊断与不变量视图"),
)

STD_WORK_CARD_OPENERS: tuple[str, ...] = (
    "ai_agent",
    "sales_rep",
    "support_agent",
    "ops",
    "warehouse",
    "finance",
    "steward",
    "admin",
)

STD_WORK_CARD_RESOLVERS: tuple[str, ...] = (
    "steward",
    "sales_manager",
    "finance_director",
    "warehouse",
    "ops",
    "admin",
)

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


register_work_card_primitives(
    spec,
    opener_roles=STD_WORK_CARD_OPENERS,
    resolver_roles=STD_WORK_CARD_RESOLVERS,
)

spec.update_display(
    {
        "ontologies": {
            "std_core_ontology": "企业标准核心底座本体（核算主体·共同伙伴·协同工单）",
        },
        "objects": {
            "AccountingEntity": "法人核算主体",
            "BusinessParty": "往来业务伙伴",
        },
        "fields": {
            "entity_id": "核算主体编码",
            "name": "名称",
            "base_currency": "本位币",
            "party_id": "往来伙伴编码",
            "legal_name": "法定名称",
            "tax_id": "统一社会信用代码/税号",
            "country_code": "国家/地区代码",
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
        "description": "企业标准核心底座蓝图：核算主体、往来业务伙伴与统一协同 WorkCard。",
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
            build_work_card_object_blueprint(),
        ],
        "invariants": build_work_card_invariant_blueprints(),
        "actions": build_work_card_action_blueprints(
            opener_roles=STD_WORK_CARD_OPENERS,
            resolver_roles=STD_WORK_CARD_RESOLVERS,
        ),
        "display": {
            "ontologies": {
                "std_core_ontology": "企业标准核心底座本体（核算主体·共同伙伴·协同工单）",
            },
            "objects": {
                "AccountingEntity": "法人核算主体",
                "BusinessParty": "往来业务伙伴",
                "WorkCard": "协同审批工单卡",
            },
        },
    }
    return DeclarativeBlueprint.model_validate(payload)
