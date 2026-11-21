"""标准 CRM 纯业务领域本体（Std CRM Ontology · Pure M1）。

基于 ``std_core_ontology`` 最小核心扩展 CRM 领域骨架（绝不固化客户个性化风控门槛）：
* 实体：``CrmAccount``（客户账户）、``CrmDeal``（销售商机）、``CrmTicket``（客户工单）；
* 动作：``win_crm_deal``、``lose_crm_deal``、``resolve_crm_ticket``（纯标准状态机跃迁）。

客户特有的审批门限、跨对象拦截（如存在未关闭高优工单禁止赢单）、角色窄视图均通过
``CustomerOverlaySpec`` 与 ``apply_overlay`` 只增不破地叠加。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from open_ontology.induction.dsl import DeclarativeBlueprint, compose_blueprints
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        BusinessParty,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )
except ImportError:
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        BusinessParty,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )

__all__ = [
    "spec",
    "BusinessParty",
    "WorkCard",
    "CrmAccount",
    "CrmDeal",
    "CrmTicket",
    "WinCrmDealParams",
    "LoseCrmDealParams",
    "ResolveCrmTicketParams",
    "build_std_crm_blueprint",
]

_CRM_DEAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PROSPECTING": ("QUALIFICATION", "CLOSED_LOST"),
    "QUALIFICATION": ("PROPOSAL", "CLOSED_LOST"),
    "PROPOSAL": ("NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"),
    "NEGOTIATION": ("PROPOSAL", "CLOSED_WON", "CLOSED_LOST"),
    "CLOSED_WON": (),
    "CLOSED_LOST": (),
}

_CRM_TICKET_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "OPEN": ("IN_PROGRESS", "RESOLVED", "CLOSED"),
    "IN_PROGRESS": ("RESOLVED", "CLOSED"),
    "RESOLVED": ("CLOSED", "IN_PROGRESS"),
    "CLOSED": (),
}

spec = OntologySpec(extends=(std_core_spec,))


@spec.object_type
class CrmAccount(MasterEntity):
    """标准 CRM 客户账户（关联共同往来伙伴 ``BusinessParty``）。"""

    __primary_key__: ClassVar[str] = "account_id"
    __title__: ClassVar[str] = "name"

    account_id: str
    name: str
    party: ObjectProperty[BusinessParty]
    industry: str = ""
    status: str = "active"


@spec.object_type
class CrmDeal(ProcessDocument):
    """标准 CRM 销售商机单据（纯标准阶段状态机，无硬编码审批门限）。"""

    __primary_key__: ClassVar[str] = "deal_id"
    __title__: ClassVar[str] = "title"
    __status_field__: ClassVar[str] = "stage"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("PROSPECTING",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _CRM_DEAL_TRANSITIONS
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("amount",)

    deal_id: str
    title: str
    account: ObjectProperty[CrmAccount]
    amount: float = Field(default=0.0, ge=0.0)
    stage: str = "PROSPECTING"
    loss_reason: str = ""


@spec.object_type
class CrmTicket(ProcessDocument):
    """标准 CRM 客户支持工单（纯标准状态机）。"""

    __primary_key__: ClassVar[str] = "ticket_id"
    __title__: ClassVar[str] = "subject"
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("OPEN",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _CRM_TICKET_TRANSITIONS

    ticket_id: str
    subject: str
    account: ObjectProperty[CrmAccount]
    priority: str = "MEDIUM"
    status: str = "OPEN"
    resolution_summary: str = ""


class WinCrmDealParams(BaseModel):
    deal_id: str


class LoseCrmDealParams(BaseModel):
    deal_id: str
    loss_reason: str = ""


class ResolveCrmTicketParams(BaseModel):
    ticket_id: str
    resolution_summary: str = ""


@spec.action(
    "win_crm_deal",
    WinCrmDealParams,
    allowed_roles=("sales_rep", "sales_manager", "finance_director", "steward", "ai_agent", "admin"),
    touched_object_types=("CrmDeal",),
    description="将处于报价/谈判阶段的 CRM 商机推进为赢单（CLOSED_WON）。",
)
def win_crm_deal(ctx: Any, params: WinCrmDealParams) -> None:
    deal = ctx.require(CrmDeal, params.deal_id)
    if deal.stage not in ("PROPOSAL", "NEGOTIATION"):
        raise InvariantError(
            f"CrmDeal:{deal.deal_id} stage {deal.stage!r} cannot transition to CLOSED_WON",
            code="INVALID_DEAL_STAGE",
        )
    ctx.modify(deal, stage="CLOSED_WON")


@spec.action(
    "lose_crm_deal",
    LoseCrmDealParams,
    allowed_roles=("sales_rep", "sales_manager", "steward", "ai_agent", "admin"),
    touched_object_types=("CrmDeal",),
    description="将进行中的 CRM 商机标记为输单（CLOSED_LOST）并记录输单原因。",
)
def lose_crm_deal(ctx: Any, params: LoseCrmDealParams) -> None:
    deal = ctx.require(CrmDeal, params.deal_id)
    if deal.stage not in ("PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION"):
        raise InvariantError(
            f"CrmDeal:{deal.deal_id} stage {deal.stage!r} cannot transition to CLOSED_LOST",
            code="INVALID_DEAL_STAGE",
        )
    ctx.modify(deal, stage="CLOSED_LOST", loss_reason=params.loss_reason)


@spec.action(
    "resolve_crm_ticket",
    ResolveCrmTicketParams,
    allowed_roles=("support_agent", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("CrmTicket",),
    description="将待处理/处理中的客户工单解决为 RESOLVED。",
)
def resolve_crm_ticket(ctx: Any, params: ResolveCrmTicketParams) -> None:
    ticket = ctx.require(CrmTicket, params.ticket_id)
    if ticket.status not in ("OPEN", "IN_PROGRESS"):
        raise InvariantError(
            f"CrmTicket:{ticket.ticket_id} status {ticket.status!r} cannot transition to RESOLVED",
            code="INVALID_TICKET_STATUS",
        )
    ctx.modify(ticket, status="RESOLVED", resolution_summary=params.resolution_summary)


spec.update_display(
    {
        "ontologies": {
            "std_crm_ontology": "标准 CRM 领域本体（账户·商机·工单）",
        },
        "objects": {
            "CrmAccount": "CRM客户账户",
            "CrmDeal": "CRM销售商机",
            "CrmTicket": "CRM客户工单",
        },
        "actions": {
            "win_crm_deal": "商机赢单",
            "lose_crm_deal": "商机输单",
            "resolve_crm_ticket": "解决客户工单",
        },
        "fields": {
            "account_id": "客户账户编码",
            "deal_id": "商机编码",
            "title": "商机标题",
            "amount": "预计金额",
            "stage": "商机阶段",
            "loss_reason": "输单原因",
            "ticket_id": "工单编码",
            "subject": "工单主题",
            "priority": "优先级",
            "resolution_summary": "处理总结",
        },
    }
)


def build_std_crm_blueprint() -> DeclarativeBlueprint:
    """构建标准 CRM 领域声明式蓝图（内部通过 ``compose_blueprints`` 导入核心底座）。"""
    crm_fragment: dict[str, Any] = {
        "model_id": "_std_crm_domain_fragment",
        "version": "1.0.0",
        "seed_ontology_id": "business_ontology",
        "object_types": [
            {
                "name": "CrmAccount",
                "archetype": "MasterEntity",
                "primary_key": "account_id",
                "title_field": "name",
                "aggregate_root": True,
                "owned": False,
                "display_label": "CRM客户账户",
                "fields": [
                    {"name": "account_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "name", "field_type": "str", "required": True},
                    {"name": "industry", "field_type": "str", "default": ""},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
                "relations": [
                    {"field": "party", "target_type": "BusinessParty", "required": True},
                ],
            },
            {
                "name": "CrmDeal",
                "archetype": "ProcessDocument",
                "primary_key": "deal_id",
                "title_field": "title",
                "owned": False,
                "status_field": "stage",
                "initial_statuses": ["PROSPECTING"],
                "transitions": {k: list(v) for k, v in _CRM_DEAL_TRANSITIONS.items()},
                "display_label": "CRM销售商机",
                "fields": [
                    {"name": "deal_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "title", "field_type": "str", "required": True},
                    {"name": "amount", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "stage", "field_type": "str", "default": "PROSPECTING"},
                    {"name": "loss_reason", "field_type": "str", "default": ""},
                ],
                "relations": [
                    {"field": "account", "target_type": "CrmAccount", "required": True},
                ],
            },
            {
                "name": "CrmTicket",
                "archetype": "ProcessDocument",
                "primary_key": "ticket_id",
                "title_field": "subject",
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["OPEN"],
                "transitions": {k: list(v) for k, v in _CRM_TICKET_TRANSITIONS.items()},
                "display_label": "CRM客户工单",
                "fields": [
                    {"name": "ticket_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "subject", "field_type": "str", "required": True},
                    {"name": "priority", "field_type": "str", "default": "MEDIUM"},
                    {"name": "status", "field_type": "str", "default": "OPEN"},
                    {"name": "resolution_summary", "field_type": "str", "default": ""},
                ],
                "relations": [
                    {"field": "account", "target_type": "CrmAccount", "required": True},
                ],
            },
        ],
        "actions": [
            {
                "name": "win_crm_deal",
                "display_label": "商机赢单",
                "description": "将处于报价/谈判阶段的 CRM 商机推进为赢单（CLOSED_WON）。",
                "allowed_roles": ["sales_rep", "sales_manager", "finance_director", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "deal_id", "field_type": "str", "required": True},
                ],
                "preconditions": [
                    {
                        "name": "crm_deal_winnable_stage",
                        "code": "INVALID_DEAL_STAGE",
                        "target_type": "CrmDeal",
                        "id_param": "deal_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.stage"},
                            "right": {"lit": ["PROPOSAL", "NEGOTIATION"]},
                        },
                        "description": "CrmDeal must be in PROPOSAL or NEGOTIATION to win.",
                    }
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "CrmDeal",
                        "id_param": "deal_id",
                        "set_fields": {"stage": {"lit": "CLOSED_WON"}},
                    }
                ],
            },
            {
                "name": "lose_crm_deal",
                "display_label": "商机输单",
                "description": "将进行中的 CRM 商机标记为输单（CLOSED_LOST）并记录输单原因。",
                "allowed_roles": ["sales_rep", "sales_manager", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "deal_id", "field_type": "str", "required": True},
                    {"name": "loss_reason", "field_type": "str", "default": ""},
                ],
                "preconditions": [
                    {
                        "name": "crm_deal_losable_stage",
                        "code": "INVALID_DEAL_STAGE",
                        "target_type": "CrmDeal",
                        "id_param": "deal_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.stage"},
                            "right": {"lit": ["PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION"]},
                        },
                        "description": "CrmDeal must be open before marking CLOSED_LOST.",
                    }
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "CrmDeal",
                        "id_param": "deal_id",
                        "set_fields": {
                            "stage": {"lit": "CLOSED_LOST"},
                            "loss_reason": {"ref": "param.loss_reason"},
                        },
                    }
                ],
            },
            {
                "name": "resolve_crm_ticket",
                "display_label": "解决客户工单",
                "description": "将待处理/处理中的客户工单解决为 RESOLVED。",
                "allowed_roles": ["support_agent", "ops", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "ticket_id", "field_type": "str", "required": True},
                    {"name": "resolution_summary", "field_type": "str", "default": ""},
                ],
                "preconditions": [
                    {
                        "name": "crm_ticket_resolvable_status",
                        "code": "INVALID_TICKET_STATUS",
                        "target_type": "CrmTicket",
                        "id_param": "ticket_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                        },
                        "description": "CrmTicket must be OPEN or IN_PROGRESS to resolve.",
                    }
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "CrmTicket",
                        "id_param": "ticket_id",
                        "set_fields": {
                            "status": {"lit": "RESOLVED"},
                            "resolution_summary": {"ref": "param.resolution_summary"},
                        },
                    }
                ],
            },
        ],
        "display": {
            "ontologies": {
                "std_crm_ontology": "标准 CRM 领域本体（账户·商机·工单）",
            },
            "objects": {
                "CrmAccount": "CRM客户账户",
                "CrmDeal": "CRM销售商机",
                "CrmTicket": "CRM客户工单",
            },
            "actions": {
                "win_crm_deal": "商机赢单",
                "lose_crm_deal": "商机输单",
                "resolve_crm_ticket": "解决客户工单",
            },
        },
    }
    composed = compose_blueprints(
        [build_std_core_blueprint(), crm_fragment],
        model_id="std_crm_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="标准 CRM 领域本体蓝图：在核心底座上组合客户账户、商机与客服工单骨架。",
    )
    composed.imports = ["std_core_ontology"]
    return composed
