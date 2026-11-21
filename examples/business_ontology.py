"""通用商业领域 M1 抽象上位本体（Abstract Business Ontology）。

为企业 ERP / 供应链 / 业财融合等具体领域本体（如 O2C、P2P、WMS、总账）提供
可继承的领域原型与公理约束：

1. :class:`MasterEntity` —— 主数据实体（默认聚合根，低频管护）；
2. :class:`BalanceLedger` —— 存量结存台账（支持账证 $\\Delta$ 守恒与非负边界）；
3. :class:`ProcessDocument` —— 流程作业单据（有限状态机合法跃迁边约束）；
4. :class:`EventVoucher` —— 业务事件凭证/流水（默认 ``__append_only__ = True`` 不可篡改）。

具体业务本体通过 ``spec = OntologySpec(extends=(business_spec,))`` 导入并继承：
* 四个抽象类本身声明 ``__abstract__ = True``，只进入 TBox 谱系与多态检索，不会
  变成具体落库实体表、OSDK 工具或界面卡片；
* 本模块声明的全局状态不变量与跃迁不变量随 ``extends`` 自动注入所有子本体。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, ClassVar, Literal, Sequence

from pydantic import BaseModel

from open_ontology.spec import (
    AutoID,
    InvariantError,
    ObjectType,
    OntologySpec,
    PermissionDeniedError,
)

__all__ = [
    "spec",
    "MasterEntity",
    "BalanceLedger",
    "ProcessDocument",
    "EventVoucher",
    "WorkCardKind",
    "WorkCardStatus",
    "WorkCard",
    "register_work_card_primitives",
    "settle_work_card",
    "submit_or_open_work_card",
    "build_work_card_object_blueprint",
    "build_work_card_invariant_blueprints",
    "build_work_card_action_blueprints",
    "build_no_active_work_card_precondition",
    "build_business_seed_ontology",
    "BUSINESS_SEED_ONTOLOGY",
]

spec = OntologySpec()


# ---------------------------------------------------------------------------
# Abstract Business Archetypes (__abstract__ = True; M1 superclasses)
# ---------------------------------------------------------------------------
class MasterEntity(ObjectType):
    """主数据实体原型：低频管护、多为领域聚合根（如客户、物料、仓库）。"""

    __abstract__: ClassVar[bool] = True
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ()


class BalanceLedger(ObjectType):
    """存量结存台账原型：记录多维组合的当前余额/库存，遵循非负与账证守恒。"""

    __abstract__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ()
    #: 账证守恒规则：{余额字段名: (凭证类型名, 凭证指向本台账的对象属性字段名, 凭证有符号增量字段名)}
    __voucher_delta_rules__: ClassVar[dict[str, tuple[str, str, str]]] = {}


class ProcessDocument(ObjectType):
    """流程作业单据原型：承载履行契约与审批流转的有限状态机。"""

    __abstract__: ClassVar[bool] = True
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...] | None] = None
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {}
    __deletable_statuses__: ClassVar[tuple[str, ...] | None] = None


class EventVoucher(ObjectType):
    """业务事件凭证/流水原型：已定格的法律或财务事实，默认仅允许追加与红冲。"""

    __abstract__: ClassVar[bool] = True
    __append_only__: ClassVar[bool] = True


for _abstract_cls in (MasterEntity, BalanceLedger, ProcessDocument, EventVoucher):
    spec.object_type(_abstract_cls)


# ---------------------------------------------------------------------------
# Inheritable Business Axioms (State & Transition Invariants)
# ---------------------------------------------------------------------------
@spec.invariant(
    "business_non_negative_fields",
    code="NEGATIVE_BALANCE",
    description="商业台账及主数据声明的非负数值字段（库存量、余额、额度等）均不得小于零。",
)
def business_non_negative_fields(view: Any) -> bool:
    for cls in view.spec.object_types.values():
        non_neg_fields = tuple(getattr(cls, "__non_negative_fields__", ()) or ())
        if not non_neg_fields:
            continue
        for obj in view.query(cls):
            for field_name in non_neg_fields:
                value = getattr(obj, field_name, None)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                    raise InvariantError(
                        f"{cls.__name__}:{obj.primary_key_value()}.{field_name} "
                        f"cannot be negative ({value})",
                        code="NEGATIVE_BALANCE",
                    )
    return True


@spec.transition_invariant(
    "process_document_state_transitions",
    code="ILLEGAL_DOCUMENT_TRANSITION",
    description="流程作业单据的创建初态、状态跃迁边及删除前置状态必须符合模型状态机声明。",
)
def process_document_state_transitions(diff: Any, view: Any) -> bool:
    for change in diff.object_changes:
        cls = view.spec.get_object_type(change.object_type)
        if not issubclass(cls, ProcessDocument):
            continue
        status_field = getattr(cls, "__status_field__", "status")
        initial_statuses = getattr(cls, "__initial_statuses__", None)
        transitions = getattr(cls, "__transitions__", {}) or {}
        deletable_statuses = getattr(cls, "__deletable_statuses__", None)

        if change.kind == "create":
            if initial_statuses is not None:
                after_status = (change.after or {}).get(status_field)
                if after_status not in initial_statuses:
                    raise InvariantError(
                        f"{cls.__name__}:{change.object_id} created with invalid "
                        f"initial {status_field}={after_status!r}; "
                        f"expected one of {list(initial_statuses)}",
                        code="INVALID_INITIAL_DOCUMENT_STATUS",
                    )
        elif change.kind == "update":
            before_status = (change.before or {}).get(status_field)
            after_status = (change.after or {}).get(status_field)
            if transitions and before_status != after_status:
                allowed_targets = transitions.get(before_status, ())
                if after_status not in allowed_targets:
                    raise InvariantError(
                        f"{cls.__name__}:{change.object_id} illegal transition "
                        f"{before_status!r} -> {after_status!r}; "
                        f"allowed targets: {list(allowed_targets)}",
                        code="ILLEGAL_DOCUMENT_TRANSITION",
                    )
        elif change.kind == "delete":
            if deletable_statuses is not None:
                before_status = (change.before or {}).get(status_field)
                if before_status not in deletable_statuses:
                    raise InvariantError(
                        f"{cls.__name__}:{change.object_id} cannot be deleted "
                        f"from status {before_status!r}; "
                        f"allowed: {list(deletable_statuses)}",
                        code="NON_DELETABLE_DOCUMENT_STATUS",
                    )
    return True


@spec.transition_invariant(
    "voucher_backed_balance_conservation",
    code="UNBACKED_BALANCE_MUTATION",
    description="受账证守恒规则约束的存量结存台账变更必须与同事务新增事件凭证增量精确配平。",
)
def voucher_backed_balance_conservation(diff: Any, view: Any) -> bool:
    for ledger_cls in view.spec.subtypes_of(BalanceLedger):
        rules = getattr(ledger_cls, "__voucher_delta_rules__", {}) or {}
        if not rules:
            continue
        ledger_type = ledger_cls.__name__
        for balance_field, (voucher_type, link_field, delta_field) in rules.items():
            ledger_deltas: dict[str, int | float] = defaultdict(int)
            for change in diff.object_changes:
                if change.object_type != ledger_type:
                    continue
                if change.kind == "create":
                    delta = (change.after or {}).get(balance_field, 0) or 0
                elif change.kind == "update":
                    after_val = (change.after or {}).get(balance_field, 0) or 0
                    before_val = (change.before or {}).get(balance_field, 0) or 0
                    delta = after_val - before_val
                elif change.kind == "delete":
                    before_val = (change.before or {}).get(balance_field, 0) or 0
                    delta = -before_val
                else:
                    delta = 0
                if delta != 0:
                    ledger_deltas[change.object_id] += delta

            voucher_sums: dict[str, int | float] = defaultdict(int)
            for change in diff.object_changes:
                if change.object_type != voucher_type or change.kind != "create":
                    continue
                after_props = change.after or {}
                target_id = after_props.get(link_field)
                delta_val = after_props.get(delta_field, 0) or 0
                if target_id is None:
                    continue
                voucher_sums[str(target_id)] += delta_val

            for ledger_id in sorted(set(ledger_deltas) | set(voucher_sums)):
                expected_ledger = ledger_deltas.get(ledger_id, 0)
                actual_voucher = voucher_sums.get(ledger_id, 0)
                if abs(expected_ledger - actual_voucher) > 1e-9:
                    raise InvariantError(
                        f"{ledger_type}:{ledger_id}.{balance_field} changed by "
                        f"{expected_ledger}, but {voucher_type}.{delta_field} sum "
                        f"in transaction is {actual_voucher}",
                        code="UNBACKED_BALANCE_MUTATION",
                    )
    return True


spec.display = {
    "ontologies": {
        "business_ontology": "商业领域抽象上位本体",
    },
    "categories": {
        "Business": "商业核心",
    },
}


# ---------------------------------------------------------------------------
# Unified M1 Collaboration WorkCard (Approval + Human Task + Dept Work)
# ---------------------------------------------------------------------------
WorkCardKind = Literal["APPROVAL", "HUMAN_TASK", "DEPT_WORK"]
WorkCardStatus = Literal[
    "OPEN",
    "IN_PROGRESS",
    "COMPLETED",
    "REJECTED",
    "SLA_BREACHED",
    "CANCELLED",
]

_WORK_CARD_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "OPEN": ("IN_PROGRESS", "COMPLETED", "REJECTED", "SLA_BREACHED", "CANCELLED"),
    "IN_PROGRESS": ("COMPLETED", "REJECTED", "SLA_BREACHED", "CANCELLED"),
    "SLA_BREACHED": ("IN_PROGRESS", "COMPLETED", "REJECTED", "CANCELLED"),
    "COMPLETED": (),
    "REJECTED": (),
    "CANCELLED": (),
}


class WorkCard(ProcessDocument):
    """统一 M1 协作工单卡（收编审批卡、人工任务卡与跨部门协同卡）。

    只承载原始意图与协作元数据，绝不缓存任何跨时间 ``object_diff_json``；
    批准 ``APPROVAL`` 卡时在 $t_1$ 同一 ``EditBatch`` 事务内重新执行
    ``requested_action``，完整重跑全部前置校验与全局不变量并以最新 OCC 版本号落库。
    """

    __primary_key__: ClassVar[str] = "card_id"
    __title__: ClassVar[str] = "requested_action"
    __aggregate_root__: ClassVar[bool] = True
    __owned__: ClassVar[bool] = True
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("OPEN",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _WORK_CARD_TRANSITIONS
    __deletable_statuses__: ClassVar[tuple[str, ...]] = ("CANCELLED",)

    card_id: AutoID
    card_kind: WorkCardKind = "APPROVAL"
    target_object_type: str
    target_object_id: str
    requested_action: str
    requested_params_json: str = "{}"
    requester_actor_id: str
    assignee_role: str = "steward"
    assignee_dept: str = ""
    status: WorkCardStatus = "OPEN"
    sla_due_at: str = ""
    external_ref: str = ""
    resolution_note: str = ""
    claimed_by_actor_id: str = ""
    resolved_by_actor_id: str = ""


class OpenWorkCardParams(BaseModel):
    card_id: str = ""
    card_kind: WorkCardKind = "APPROVAL"
    target_object_type: str
    target_object_id: str
    requested_action: str
    requested_params_json: str = "{}"
    assignee_role: str = "steward"
    assignee_dept: str = ""
    sla_due_at: str = ""
    external_ref: str = ""


class ClaimWorkCardParams(BaseModel):
    card_id: str
    external_ref: str = ""


class CompleteWorkCardParams(BaseModel):
    card_id: str
    resolution_note: str = ""
    external_ref: str = ""
    settled_run_id: str = ""


class RejectWorkCardParams(BaseModel):
    card_id: str
    resolution_note: str = ""


class MarkWorkCardSlaBreachedParams(BaseModel):
    card_id: str
    resolution_note: str = ""


class CancelWorkCardParams(BaseModel):
    card_id: str
    resolution_note: str = ""


def _assert_work_card_assignee_and_sod(
    ctx: Any,
    card: WorkCard,
    *,
    enforce_sod: bool,
) -> None:
    if enforce_sod and ctx.actor.actor_id == card.requester_actor_id:
        raise InvariantError(
            f"SoD violation: requester {ctx.actor.actor_id!r} cannot self-resolve WorkCard:{card.card_id}",
            code="SOD_SELF_APPROVAL_FORBIDDEN",
        )
    if card.assignee_role and ctx.actor.role not in (card.assignee_role, "admin"):
        raise InvariantError(
            f"WorkCard:{card.card_id} assigned to role {card.assignee_role!r}, "
            f"cannot be handled by role {ctx.actor.role!r}",
            code="WORK_CARD_ASSIGNEE_MISMATCH",
        )


def register_work_card_primitives(
    target_spec: OntologySpec,
    *,
    opener_roles: Sequence[str] | None = None,
    resolver_roles: Sequence[str] | None = None,
) -> OntologySpec:
    """将统一 M1 ``WorkCard``、职责分离/防重复不变量与 6 个同步动作注册到具体领域本体。"""
    default_openers = (
        "ai_agent",
        "sales_rep",
        "buyer",
        "ops",
        "clerk",
        "warehouse",
        "steward",
        "admin",
    )
    default_resolvers = (
        "steward",
        "finance_director",
        "sales_manager",
        "dept_lead",
        "warehouse",
        "ops",
        "admin",
    )
    declared_roles = set(target_spec.roles)
    eff_openers = (
        tuple(opener_roles)
        if opener_roles is not None
        else (
            tuple(r for r in default_openers if not declared_roles or r in declared_roles)
            or tuple(sorted(declared_roles))
        )
    )
    eff_resolvers = (
        tuple(resolver_roles)
        if resolver_roles is not None
        else (
            tuple(r for r in default_resolvers if not declared_roles or r in declared_roles)
            or tuple(sorted(declared_roles))
        )
    )

    if "process_document_state_transitions" not in target_spec.transition_invariants:
        target_spec.transition_invariant(
            "process_document_state_transitions",
            code="ILLEGAL_DOCUMENT_TRANSITION",
            description="流程作业单据的创建初态、状态跃迁边及删除前置状态必须符合模型状态机声明。",
        )(process_document_state_transitions)

    if "WorkCard" not in target_spec.object_types:
        target_spec.object_type(WorkCard)

    if "work_card_unique_active_intent" not in target_spec.invariants:

        @target_spec.invariant(
            "work_card_unique_active_intent",
            code="DUPLICATE_ACTIVE_WORK_CARD",
            description="同一目标对象上的同类型同动作在途工单（OPEN/IN_PROGRESS）不得重复开卡。",
        )
        def _unique_active_work_card(view: Any) -> bool:
            seen: dict[tuple[str, str, str, str], str] = {}
            for card in view.query(WorkCard):
                if card.status not in ("OPEN", "IN_PROGRESS"):
                    continue
                sig = (
                    card.card_kind,
                    card.target_object_type,
                    card.target_object_id,
                    card.requested_action,
                )
                prev_id = seen.get(sig)
                if prev_id is not None and prev_id != card.card_id:
                    raise InvariantError(
                        f"duplicate active WorkCard for {sig}: {prev_id} and {card.card_id}",
                        code="DUPLICATE_ACTIVE_WORK_CARD",
                    )
                seen[sig] = card.card_id
            return True

    if "work_card_segregation_of_duties" not in target_spec.invariants:

        @target_spec.invariant(
            "work_card_segregation_of_duties",
            code="SOD_SELF_APPROVAL_FORBIDDEN",
            description="职责分离（SoD）：WorkCard 的申请人（requester_actor_id）与批结人（resolved_by_actor_id）不得为同一身份。",
        )
        def _sod_invariant(view: Any) -> bool:
            for card in view.query(WorkCard):
                if (
                    card.status in ("COMPLETED", "REJECTED")
                    and card.resolved_by_actor_id
                    and card.resolved_by_actor_id == card.requester_actor_id
                ):
                    raise InvariantError(
                        f"SoD violation on WorkCard:{card.card_id}: "
                        f"requester_actor_id == resolved_by_actor_id == {card.requester_actor_id!r}",
                        code="SOD_SELF_APPROVAL_FORBIDDEN",
                    )
            return True

    all_card_roles = tuple(dict.fromkeys([*eff_openers, *eff_resolvers]))

    if "open_work_card" not in target_spec.actions:

        @target_spec.action(
            "open_work_card",
            OpenWorkCardParams,
            allowed_roles=eff_openers,
            affects_source=False,
            touched_object_types=("WorkCard",),
            description="开立 M1 WorkCard（只记录原始意图与参数，绝不缓存 object_diff_json）。",
        )
        def _open_work_card(ctx: Any, params: OpenWorkCardParams) -> None:
            cid = params.card_id.strip() or f"WC-{uuid.uuid4().hex[:10].upper()}"
            if ctx.get(WorkCard, cid) is not None:
                raise InvariantError(f"WorkCard:{cid} already exists", code="WORK_CARD_EXISTS")
            if params.card_kind == "APPROVAL":
                try:
                    act_def = ctx.spec.get_action(params.requested_action)
                    act_def.params.model_validate_json(params.requested_params_json or "{}")
                except Exception as exc:
                    raise InvariantError(
                        f"invalid APPROVAL intent for action {params.requested_action!r}: {exc}",
                        code="INVALID_WORK_CARD_INTENT",
                    ) from exc
            ctx.create(
                WorkCard(
                    card_id=cid,
                    card_kind=params.card_kind,
                    target_object_type=params.target_object_type,
                    target_object_id=params.target_object_id,
                    requested_action=params.requested_action,
                    requested_params_json=params.requested_params_json or "{}",
                    requester_actor_id=ctx.actor.actor_id,
                    assignee_role=params.assignee_role,
                    assignee_dept=params.assignee_dept,
                    status="OPEN",
                    sla_due_at=params.sla_due_at,
                    external_ref=params.external_ref,
                )
            )

    if "claim_work_card" not in target_spec.actions:

        @target_spec.action(
            "claim_work_card",
            ClaimWorkCardParams,
            allowed_roles=eff_resolvers,
            affects_source=False,
            touched_object_types=("WorkCard",),
            description="认领推进 WorkCard（OPEN/SLA_BREACHED -> IN_PROGRESS）并记录外部回执号。",
        )
        def _claim_work_card(ctx: Any, params: ClaimWorkCardParams) -> None:
            card = ctx.require(WorkCard, params.card_id)
            _assert_work_card_assignee_and_sod(ctx, card, enforce_sod=False)
            ctx.modify(
                card,
                status="IN_PROGRESS",
                claimed_by_actor_id=ctx.actor.actor_id,
                external_ref=params.external_ref or card.external_ref,
            )

    if "complete_work_card" not in target_spec.actions:

        @target_spec.action(
            "complete_work_card",
            CompleteWorkCardParams,
            allowed_roles=eff_resolvers,
            revert_roles=eff_resolvers,
            affects_source=False,
            touched_object_types=("WorkCard",),
            description="批结 WorkCard；若为 APPROVAL 卡且目标动作纯自有则在 t1 当场重算执行，源系统写动作须经 settle_work_card 提供 settled_run_id。",
        )
        def _complete_work_card(ctx: Any, params: CompleteWorkCardParams) -> None:
            card = ctx.require(WorkCard, params.card_id)
            _assert_work_card_assignee_and_sod(ctx, card, enforce_sod=True)
            if card.card_kind == "APPROVAL":
                target_action = ctx.spec.get_action(card.requested_action)
                if target_action.affects_source:
                    if not params.settled_run_id.strip():
                        raise InvariantError(
                            f"APPROVAL WorkCard:{card.card_id} for source-backed action "
                            f"{card.requested_action!r} requires settled_run_id via settle_work_card",
                            code="SOURCE_APPROVAL_REQUIRES_SETTLED_RUN",
                        )
                else:
                    if (
                        target_action.allowed_roles
                        and ctx.actor.role not in target_action.allowed_roles
                    ):
                        raise PermissionDeniedError(
                            f"actor {ctx.actor.actor_id!r} role {ctx.actor.role!r} may not execute "
                            f"{target_action.name!r}; allowed roles: {list(target_action.allowed_roles)}"
                        )
                    target_params = target_action.params.model_validate_json(
                        card.requested_params_json or "{}"
                    )
                    target_action.handler(ctx, target_params)
            ctx.modify(
                card,
                status="COMPLETED",
                resolved_by_actor_id=ctx.actor.actor_id,
                resolution_note=params.resolution_note or card.resolution_note,
                external_ref=params.external_ref or card.external_ref,
            )

    if "reject_work_card" not in target_spec.actions:

        @target_spec.action(
            "reject_work_card",
            RejectWorkCardParams,
            allowed_roles=eff_resolvers,
            affects_source=False,
            touched_object_types=("WorkCard",),
            description="驳回 WorkCard（校验 SoD 后跃迁为 REJECTED）。",
        )
        def _reject_work_card(ctx: Any, params: RejectWorkCardParams) -> None:
            card = ctx.require(WorkCard, params.card_id)
            _assert_work_card_assignee_and_sod(ctx, card, enforce_sod=True)
            ctx.modify(
                card,
                status="REJECTED",
                resolved_by_actor_id=ctx.actor.actor_id,
                resolution_note=params.resolution_note or card.resolution_note,
            )

    if "mark_work_card_sla_breached" not in target_spec.actions:

        @target_spec.action(
            "mark_work_card_sla_breached",
            MarkWorkCardSlaBreachedParams,
            allowed_roles=all_card_roles,
            affects_source=False,
            touched_object_types=("WorkCard",),
            description="标记超时未处理 WorkCard 为 SLA_BREACHED。",
        )
        def _mark_sla_breached(ctx: Any, params: MarkWorkCardSlaBreachedParams) -> None:
            card = ctx.require(WorkCard, params.card_id)
            ctx.modify(
                card,
                status="SLA_BREACHED",
                resolution_note=params.resolution_note or card.resolution_note,
            )

    if "cancel_work_card" not in target_spec.actions:

        @target_spec.action(
            "cancel_work_card",
            CancelWorkCardParams,
            allowed_roles=all_card_roles,
            affects_source=False,
            touched_object_types=("WorkCard",),
            description="撤销不再需要的 WorkCard（仅原申请人或 steward/admin 可撤销）。",
        )
        def _cancel_work_card(ctx: Any, params: CancelWorkCardParams) -> None:
            card = ctx.require(WorkCard, params.card_id)
            if ctx.actor.actor_id != card.requester_actor_id and ctx.actor.role not in (
                "steward",
                "admin",
            ):
                raise InvariantError(
                    f"actor {ctx.actor.actor_id!r} may not cancel WorkCard:{card.card_id}",
                    code="WORK_CARD_CANCEL_FORBIDDEN",
                )
            ctx.modify(
                card,
                status="CANCELLED",
                resolution_note=params.resolution_note or card.resolution_note,
            )

    target_spec.update_display(
        {
            "objects": {
                "WorkCard": "协同审批工单卡",
            },
            "actions": {
                "open_work_card": "发起协同工单卡",
                "claim_work_card": "认领协同工单卡",
                "complete_work_card": "批结协同工单卡",
                "reject_work_card": "驳回协同工单卡",
                "mark_work_card_sla_breached": "标记工单卡超时",
                "cancel_work_card": "撤销协同工单卡",
            },
            "fields": {
                "card_id": "工单卡编号",
                "card_kind": "工单卡类别",
                "target_object_type": "目标对象类型",
                "target_object_id": "目标对象编号",
                "requested_action": "申请执行动作",
                "requested_params_json": "申请动作参数",
                "requester_actor_id": "发起人标识",
                "assignee_role": "指派审批角色",
                "assignee_dept": "指派协同部门",
                "sla_due_at": "时效截止时间",
                "external_ref": "外部回执编号",
                "resolution_note": "审批处理意见",
                "claimed_by_actor_id": "认领人标识",
                "resolved_by_actor_id": "批结人标识",
                "settled_run_id": "关联结算流水号",
            },
            "statuses": {
                "OPEN": "待处理",
                "IN_PROGRESS": "处理中",
                "COMPLETED": "已批结",
                "SLA_BREACHED": "已超时",
                "CANCELLED": "已撤销",
            },
        }
    )

    return target_spec


class _WorkCardActorContext:
    def __init__(self, actor: Any, spec: Any) -> None:
        self.actor = actor
        self.spec = spec


def settle_work_card(
    rt_handle: Any,
    card_id: str,
    approver: Any,
    *,
    resolution_note: str = "",
    external_ref: str = "",
    action_run_id: str | None = None,
    complete_run_id: str | None = None,
) -> dict[str, Any]:
    """按严格双面隔离语义结算一张 ``WorkCard``。

    - 若目标动作纯自有（``affects_source=False``）或非 ``APPROVAL`` 卡：
      直接单次提交 ``complete_work_card``，在同一个 ``EditBatch`` 内于 $t_1$ 重算落库。
    - 若为 ``APPROVAL`` 卡且目标动作写回外部源（``affects_source=True``）：
      先在内存校验状态流转、职责分离（SoD）与指派角色，再在 $t_1$ 单独提交
      ``requested_action``（触发完整前置/不变量校验与 ``SourceBridge``），最后提交
      ``complete_work_card`` 记录 ``settled_run_id`` 并合卡。
    """
    import uuid

    card = rt_handle.get_object(WorkCard, card_id, approver)
    if "COMPLETED" not in _WORK_CARD_TRANSITIONS.get(card.status, ()):
        raise InvariantError(
            f"WorkCard:{card.card_id} in status {card.status!r} cannot transition to 'COMPLETED'",
            code="WORK_CARD_NOT_ACTIVE",
        )

    complete_action = rt_handle.spec.get_action("complete_work_card")
    if complete_action.allowed_roles and approver.role not in complete_action.allowed_roles:
        raise InvariantError(
            f"role {approver.role!r} is not allowed to settle WorkCard:{card.card_id}",
            code="WORK_CARD_RESOLVER_FORBIDDEN",
        )

    _assert_work_card_assignee_and_sod(
        _WorkCardActorContext(approver, rt_handle.spec),
        card,
        enforce_sod=True,
    )

    comp_rid = complete_run_id or f"WC-COMP-{uuid.uuid4().hex[:12]}"
    if card.card_kind == "APPROVAL":
        target_action = rt_handle.spec.get_action(card.requested_action)
        if target_action.affects_source:
            target_params = target_action.params.model_validate_json(
                card.requested_params_json or "{}"
            )
            act_rid = action_run_id or f"WC-ACT-{uuid.uuid4().hex[:12]}"
            settled_run = rt_handle.submit_action(
                act_rid,
                card.requested_action,
                target_params,
                approver,
            )
            if settled_run.get("status") != "COMMITTED":
                raise InvariantError(
                    f"WorkCard:{card_id} target action run {act_rid!r} did not commit "
                    f"(status={settled_run.get('status')!r}, error_code={settled_run.get('error_code')!r})",
                    code=str(settled_run.get("error_code") or "WORK_CARD_SETTLEMENT_NOT_COMMITTED"),
                )
            comp_run = rt_handle.submit_action(
                comp_rid,
                "complete_work_card",
                {
                    "card_id": card_id,
                    "resolution_note": resolution_note,
                    "external_ref": external_ref,
                    "settled_run_id": settled_run.get("run_id", act_rid),
                },
                approver,
            )
            return {**comp_run, "settled_action_run": settled_run}

    return rt_handle.submit_action(
        comp_rid,
        "complete_work_card",
        {
            "card_id": card_id,
            "resolution_note": resolution_note,
            "external_ref": external_ref,
        },
        approver,
    )


def submit_or_open_work_card(
    rt_handle: Any,
    run_id: str,
    action_name: str,
    params: BaseModel | dict[str, Any] | None,
    actor: Any,
    *,
    target_object_type: str,
    target_object_id: str,
    assignee_role: str = "steward",
    assignee_dept: str = "",
    card_id: str | None = None,
    sla_due_at: str = "",
    external_ref: str = "",
) -> dict[str, Any]:
    """尝试同步执行目标动作；遇 ``APPROVAL_REQUIRED_*`` 时改开一张 M1 ``WorkCard``。"""
    try:
        return rt_handle.submit_action(run_id, action_name, params, actor)
    except InvariantError as exc:
        if not str(exc.code or "").startswith("APPROVAL_REQUIRED_"):
            raise
        target_action = rt_handle.spec.get_action(action_name)
        if isinstance(params, BaseModel):
            params_json = params.model_dump_json()
        else:
            params_model = target_action.params.model_validate(params or {})
            params_json = params_model.model_dump_json()
        cid = (card_id or run_id).strip()
        card_run = rt_handle.submit_action(
            f"{cid}-OPEN",
            "open_work_card",
            {
                "card_id": cid,
                "card_kind": "APPROVAL",
                "target_object_type": target_object_type,
                "target_object_id": target_object_id,
                "requested_action": action_name,
                "requested_params_json": params_json,
                "assignee_role": assignee_role,
                "assignee_dept": assignee_dept,
                "sla_due_at": sla_due_at,
                "external_ref": external_ref,
            },
            actor,
        )
        return {
            "status": "AWAITING_WORK_CARD",
            "card_id": cid,
            "work_card_status": "OPEN",
            "blocked_run_id": run_id,
            "work_card_run": card_run,
            "error_code": exc.code,
            "error_message": str(exc),
        }


def build_work_card_object_blueprint() -> dict[str, Any]:
    """返回符合 L1 ``ObjectTypeBlueprint`` 结构的纯声明式 ``WorkCard`` 定义。"""
    return {
        "name": "WorkCard",
        "archetype": "ProcessDocument",
        "primary_key": "card_id",
        "title_field": "requested_action",
        "aggregate_root": True,
        "owned": True,
        "status_field": "status",
        "initial_statuses": ["OPEN"],
        "transitions": {k: list(v) for k, v in _WORK_CARD_TRANSITIONS.items()},
        "fields": [
            {"name": "card_id", "field_type": "str", "primary_key": True, "auto_id": True},
            {"name": "card_kind", "field_type": "str", "default": "APPROVAL", "required": True},
            {"name": "target_object_type", "field_type": "str", "required": True},
            {"name": "target_object_id", "field_type": "str", "required": True},
            {"name": "requested_action", "field_type": "str", "required": True},
            {"name": "requested_params_json", "field_type": "str", "default": "{}"},
            {"name": "requester_actor_id", "field_type": "str", "required": True},
            {"name": "assignee_role", "field_type": "str", "default": "steward"},
            {"name": "assignee_dept", "field_type": "str", "default": ""},
            {"name": "status", "field_type": "str", "default": "OPEN"},
            {"name": "sla_due_at", "field_type": "str", "default": ""},
            {"name": "external_ref", "field_type": "str", "default": ""},
            {"name": "resolution_note", "field_type": "str", "default": ""},
            {"name": "claimed_by_actor_id", "field_type": "str", "default": ""},
            {"name": "resolved_by_actor_id", "field_type": "str", "default": ""},
        ],
    }


def build_work_card_invariant_blueprints() -> list[dict[str, Any]]:
    """返回与 M1 ``WorkCard`` 原生规则等价的 L1 声明式 ``InvariantRuleSpec`` 列表。"""
    return [
        {
            "name": "work_card_unique_active_intent",
            "code": "DUPLICATE_ACTIVE_WORK_CARD",
            "target_type": "WorkCard",
            "when": {"op": "in", "left": {"ref": "obj.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
            "must_hold": {
                "op": "not_exists",
                "object_type": "WorkCard",
                "where": {
                    "op": "and",
                    "args": [
                        {"op": "ne", "left": {"ref": "target.card_id"}, "right": {"ref": "obj.card_id"}},
                        {"op": "eq", "left": {"ref": "target.card_kind"}, "right": {"ref": "obj.card_kind"}},
                        {
                            "op": "eq",
                            "left": {"ref": "target.target_object_type"},
                            "right": {"ref": "obj.target_object_type"},
                        },
                        {
                            "op": "eq",
                            "left": {"ref": "target.target_object_id"},
                            "right": {"ref": "obj.target_object_id"},
                        },
                        {
                            "op": "eq",
                            "left": {"ref": "target.requested_action"},
                            "right": {"ref": "obj.requested_action"},
                        },
                        {
                            "op": "in",
                            "left": {"ref": "target.status"},
                            "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                        },
                    ],
                },
            },
            "description": "同一目标对象上的同类型同动作在途工单不得重复开卡。",
        },
        {
            "name": "work_card_segregation_of_duties",
            "code": "SOD_SELF_APPROVAL_FORBIDDEN",
            "target_type": "WorkCard",
            "when": {
                "op": "and",
                "args": [
                    {"op": "in", "left": {"ref": "obj.status"}, "right": {"lit": ["COMPLETED", "REJECTED"]}},
                    {"op": "ne", "left": {"ref": "obj.resolved_by_actor_id"}, "right": {"lit": ""}},
                ],
            },
            "must_hold": {
                "op": "ne",
                "left": {"ref": "obj.resolved_by_actor_id"},
                "right": {"ref": "obj.requester_actor_id"},
            },
            "description": "职责分离（SoD）：禁止 WorkCard 申请人自批自结。",
        },
    ]


def build_work_card_action_blueprints(
    *,
    opener_roles: Sequence[str] = ("ai_agent", "steward", "admin"),
    resolver_roles: Sequence[str] = ("steward", "admin"),
) -> list[dict[str, Any]]:
    """返回符合 L1 ``ActionBlueprint`` 结构的 ``WorkCard`` 全生命周期声明式动作族。"""
    sod_pre = {
        "name": "sod_no_self_resolution",
        "code": "SOD_SELF_APPROVAL_FORBIDDEN",
        "target_type": "WorkCard",
        "id_param": "card_id",
        "must_hold": {
            "op": "ne",
            "left": {"ref": "actor.actor_id"},
            "right": {"ref": "obj.requester_actor_id"},
        },
        "description": "SoD: approver/resolver actor_id must differ from WorkCard.requester_actor_id.",
    }
    assignee_pre = {
        "name": "assignee_role_match",
        "code": "WORK_CARD_ASSIGNEE_MISMATCH",
        "target_type": "WorkCard",
        "id_param": "card_id",
        "when": {"op": "ne", "left": {"ref": "obj.assignee_role"}, "right": {"lit": ""}},
        "must_hold": {
            "op": "or",
            "args": [
                {"op": "eq", "left": {"ref": "actor.role"}, "right": {"ref": "obj.assignee_role"}},
                {"op": "eq", "left": {"ref": "actor.role"}, "right": {"lit": "admin"}},
            ],
        },
        "description": "Actor role must match WorkCard.assignee_role or admin.",
    }
    all_roles = list(dict.fromkeys([*opener_roles, *resolver_roles]))
    return [
        {
            "name": "open_work_card",
            "allowed_roles": list(opener_roles),
            "affects_source": False,
            "params": [
                {"name": "card_id", "field_type": "str", "required": True},
                {"name": "card_kind", "field_type": "str", "default": "APPROVAL"},
                {"name": "target_object_type", "field_type": "str", "required": True},
                {"name": "target_object_id", "field_type": "str", "required": True},
                {"name": "requested_action", "field_type": "str", "required": True},
                {"name": "requested_params_json", "field_type": "str", "default": "{}"},
                {"name": "assignee_role", "field_type": "str", "default": "steward"},
                {"name": "assignee_dept", "field_type": "str", "default": ""},
                {"name": "sla_due_at", "field_type": "str", "default": ""},
                {"name": "external_ref", "field_type": "str", "default": ""},
            ],
            "mutations": [
                {
                    "op": "create",
                    "object_type": "WorkCard",
                    "id_param": "card_id",
                    "set_fields": {
                        "card_id": {"ref": "param.card_id"},
                        "card_kind": {"ref": "param.card_kind"},
                        "target_object_type": {"ref": "param.target_object_type"},
                        "target_object_id": {"ref": "param.target_object_id"},
                        "requested_action": {"ref": "param.requested_action"},
                        "requested_params_json": {"ref": "param.requested_params_json"},
                        "requester_actor_id": {"ref": "actor.actor_id"},
                        "assignee_role": {"ref": "param.assignee_role"},
                        "assignee_dept": {"ref": "param.assignee_dept"},
                        "status": {"lit": "OPEN"},
                        "sla_due_at": {"ref": "param.sla_due_at"},
                        "external_ref": {"ref": "param.external_ref"},
                    },
                }
            ],
        },
        {
            "name": "claim_work_card",
            "allowed_roles": list(resolver_roles),
            "affects_source": False,
            "params": [
                {"name": "card_id", "field_type": "str", "required": True},
                {"name": "external_ref", "field_type": "str", "default": ""},
            ],
            "preconditions": [assignee_pre],
            "mutations": [
                {
                    "op": "modify",
                    "object_type": "WorkCard",
                    "id_param": "card_id",
                    "set_fields": {
                        "status": {"lit": "IN_PROGRESS"},
                        "claimed_by_actor_id": {"ref": "actor.actor_id"},
                        "external_ref": {"ref": "param.external_ref"},
                    },
                }
            ],
        },
        {
            "name": "complete_work_card",
            "allowed_roles": list(resolver_roles),
            "affects_source": False,
            "params": [
                {"name": "card_id", "field_type": "str", "required": True},
                {"name": "resolution_note", "field_type": "str", "default": ""},
                {"name": "external_ref", "field_type": "str", "default": ""},
                {"name": "settled_run_id", "field_type": "str", "default": ""},
            ],
            "preconditions": [sod_pre, assignee_pre],
            "mutations": [
                {
                    "op": "modify",
                    "object_type": "WorkCard",
                    "id_param": "card_id",
                    "set_fields": {
                        "status": {"lit": "COMPLETED"},
                        "resolved_by_actor_id": {"ref": "actor.actor_id"},
                        "resolution_note": {"ref": "param.resolution_note"},
                        "external_ref": {
                            "op": "if_else",
                            "cond": {"op": "ne", "left": {"ref": "param.external_ref"}, "right": {"lit": ""}},
                            "then": {"ref": "param.external_ref"},
                            "else": {"ref": "obj.external_ref"},
                        },
                    },
                }
            ],
        },
        {
            "name": "reject_work_card",
            "allowed_roles": list(resolver_roles),
            "affects_source": False,
            "params": [
                {"name": "card_id", "field_type": "str", "required": True},
                {"name": "resolution_note", "field_type": "str", "default": ""},
            ],
            "preconditions": [sod_pre, assignee_pre],
            "mutations": [
                {
                    "op": "modify",
                    "object_type": "WorkCard",
                    "id_param": "card_id",
                    "set_fields": {
                        "status": {"lit": "REJECTED"},
                        "resolved_by_actor_id": {"ref": "actor.actor_id"},
                        "resolution_note": {"ref": "param.resolution_note"},
                    },
                }
            ],
        },
        {
            "name": "mark_work_card_sla_breached",
            "allowed_roles": all_roles,
            "affects_source": False,
            "params": [
                {"name": "card_id", "field_type": "str", "required": True},
                {"name": "resolution_note", "field_type": "str", "default": ""},
            ],
            "mutations": [
                {
                    "op": "modify",
                    "object_type": "WorkCard",
                    "id_param": "card_id",
                    "set_fields": {
                        "status": {"lit": "SLA_BREACHED"},
                        "resolution_note": {"ref": "param.resolution_note"},
                    },
                }
            ],
        },
        {
            "name": "cancel_work_card",
            "allowed_roles": all_roles,
            "affects_source": False,
            "params": [
                {"name": "card_id", "field_type": "str", "required": True},
                {"name": "resolution_note", "field_type": "str", "default": ""},
            ],
            "preconditions": [
                {
                    "name": "cancel_work_card_authority",
                    "code": "WORK_CARD_CANCEL_FORBIDDEN",
                    "target_type": "WorkCard",
                    "id_param": "card_id",
                    "must_hold": {
                        "op": "or",
                        "args": [
                            {"op": "eq", "left": {"ref": "actor.actor_id"}, "right": {"ref": "obj.requester_actor_id"}},
                            {"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["steward", "admin"]}},
                        ],
                    },
                }
            ],
            "mutations": [
                {
                    "op": "modify",
                    "object_type": "WorkCard",
                    "id_param": "card_id",
                    "set_fields": {
                        "status": {"lit": "CANCELLED"},
                        "resolution_note": {"ref": "param.resolution_note"},
                    },
                }
            ],
        },
    ]


def build_no_active_work_card_precondition(
    *,
    target_object_type: str,
    id_param: str,
    name: str = "no_active_work_card_on_target",
    code: str = "ACTIVE_WORK_CARD_BLOCKS_ACTION",
) -> dict[str, Any]:
    """生成阻断带在途 ``WorkCard`` 单据被直接撤单/篡改的 L1 ``InvariantRuleSpec`` 字典。"""
    return {
        "name": name,
        "code": code,
        "target_type": target_object_type,
        "id_param": id_param,
        "must_hold": {
            "op": "not_exists",
            "object_type": "WorkCard",
            "where": {
                "op": "and",
                "args": [
                    {"op": "eq", "left": {"ref": "target.target_object_type"}, "right": {"lit": target_object_type}},
                    {"op": "eq", "left": {"ref": "target.target_object_id"}, "right": {"ref": f"param.{id_param}"}},
                    {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                ],
            },
        },
        "description": f"Cannot execute while active OPEN/IN_PROGRESS WorkCard exists for {target_object_type}.",
    }


def build_business_seed_ontology() -> Any:
    """将 ``business_ontology`` 封装为可供符号训练器插拔的企业领域种子本体论底座。"""
    from open_ontology.induction.seed import seed_from_spec

    return seed_from_spec(
        seed_id="business_ontology",
        domain_name="Enterprise Business & ERP",
        base_spec=spec,
        description=(
            "企业商业领域通用种子本体：提供 MasterEntity（主数据）、ProcessDocument（状态机单据）、"
            "BalanceLedger（非负+账证守恒台账）、EventVoucher（只追加不可变凭证）四大原型，"
            "以及统一协作工单 WorkCard（Approval / Human Task / Dept Work）及其 t1 现场重算执行范式。"
        ),
        archetype_role_hints={
            "MasterEntity": ("master_data", "low_frequency_reference", "aggregate_root"),
            "ProcessDocument": ("lifecycle_state_machine", "workflow_document", "approval_subject"),
            "BalanceLedger": ("stock_or_balance_accumulator", "voucher_conserved", "non_negative"),
            "EventVoucher": ("append_only_fact", "ledger_delta_entry", "audit_voucher"),
        },
        default_archetype="ObjectType",
        extra_prior_notes=(
            "Cohesive Decision Domain Principle: one accounting entity / unified synchronous control loop maps to one unified OntologySpec, anchored by an explicit AccountingEntity dimension for balance conservation.",
            "Multi-entity group coordination models AccountingEntity as a first-class ledger/scope dimension inside the unified ontology instead of splitting isolated specs.",
            "Internal non-financial collaboration domains without shared pre-write guardrails belong in separate lightweight ontology specs.",
            "Anti-God-Object discipline: anchor shared counterparties and materials in lean MasterEntity types (e.g., Party + CustomerRole/SupplierRole facets) rather than wide 100-column tables.",
            "Expose role/context-filtered ScopedViewSpec projections to downstream agents to keep C_agent small without fracturing synchronous invariants.",
            "Unified Collaboration WorkCard Principle: approvals, human tasks, and department work items use first-class M1 WorkCard(ProcessDocument) storing only raw intent (never cached object_diff_json), enforcing SoD (requester != resolver) and live t1 re-execution upon APPROVAL completion.",
            "Every BalanceLedger numeric balance field mutated by an action MUST declare "
            "voucher_delta_rules and emit matching EventVoucher delta records in the same transaction.",
            "Every ProcessDocument status field MUST follow declared finite-state transitions.",
            "Every EventVoucher record is append-only and immutable after creation.",
        ),
        provides_non_negative=True,
        provides_fsm=True,
        provides_balance_conservation=True,
    )


BUSINESS_SEED_ONTOLOGY = build_business_seed_ontology()
