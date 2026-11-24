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

from open_ontology.induction.dsl import (
    ActionBlueprint,
    ActionMutationSpec,
    DeclarativeBlueprint,
    InvariantRuleSpec,
    export_spec_blueprint,
    register_rule_actions,
)
from open_ontology.spec import ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )
except ImportError:
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        BusinessParty,
        WorkCard,
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


register_rule_actions(
    spec,
    [
        (
            ActionBlueprint(
                name="win_crm_deal",
                description="将处于报价/谈判阶段的 CRM 商机推进为赢单（CLOSED_WON）。",
                allowed_roles=[
                    "sales_rep",
                    "sales_manager",
                    "finance_director",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="crm_deal_winnable_stage",
                        code="INVALID_DEAL_STAGE",
                        target_type="CrmDeal",
                        id_param="deal_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.stage"},
                            "right": {"lit": ["PROPOSAL", "NEGOTIATION"]},
                        },
                        description="仅报价（PROPOSAL）或谈判（NEGOTIATION）阶段商机可推进为赢单。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="CrmDeal",
                        id_param="deal_id",
                        set_fields={"stage": {"lit": "CLOSED_WON"}},
                    ),
                ],
            ),
            WinCrmDealParams,
        ),
        (
            ActionBlueprint(
                name="lose_crm_deal",
                description="将进行中的 CRM 商机标记为输单（CLOSED_LOST）并记录输单原因。",
                allowed_roles=["sales_rep", "sales_manager", "steward", "ai_agent", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="crm_deal_losable_stage",
                        code="INVALID_DEAL_STAGE",
                        target_type="CrmDeal",
                        id_param="deal_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.stage"},
                            "right": {
                                "lit": [
                                    "PROSPECTING",
                                    "QUALIFICATION",
                                    "PROPOSAL",
                                    "NEGOTIATION",
                                ]
                            },
                        },
                        description="仅进行中商机可标记为输单（CLOSED_LOST）。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="CrmDeal",
                        id_param="deal_id",
                        set_fields={
                            "stage": {"lit": "CLOSED_LOST"},
                            "loss_reason": {"ref": "param.loss_reason"},
                        },
                    ),
                ],
            ),
            LoseCrmDealParams,
        ),
        (
            ActionBlueprint(
                name="resolve_crm_ticket",
                description="将待处理/处理中的客户工单解决为 RESOLVED。",
                allowed_roles=["support_agent", "ops", "steward", "ai_agent", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="crm_ticket_resolvable_status",
                        code="INVALID_TICKET_STATUS",
                        target_type="CrmTicket",
                        id_param="ticket_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                        },
                        description="仅 OPEN 或 IN_PROGRESS 状态工单可解决为 RESOLVED。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="CrmTicket",
                        id_param="ticket_id",
                        set_fields={
                            "status": {"lit": "RESOLVED"},
                            "resolution_summary": {"ref": "param.resolution_summary"},
                        },
                    ),
                ],
            ),
            ResolveCrmTicketParams,
        ),
    ],
)


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
            "title": "标题",
            "amount": "金额",
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
    """将 ``std_crm_ontology`` 唯一源码本体投影为标准 CRM 领域声明式蓝图。"""
    return export_spec_blueprint(
        spec,
        model_id="std_crm_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        imports=("std_core_ontology",),
        description="标准 CRM 领域本体蓝图：在核心底座上组合客户账户、商机与客服工单骨架。",
    )
