"""标准 R2R（Record-to-Report，记录到报告 / 复式总账）纯业务领域本体（Pure M1）。

严守复式记账公理、常识细腰架构与 Gate 7 零横向互耦原则，仅基于 ``std_core_ontology`` 扩展：
* 主数据与单据对象：``FiscalPeriod``（会计期间）、``GlAccount``（总账科目）、
  ``JournalEntry``（复式记账凭证）、``TrialBalanceSnapshot``（关账试算平衡快照）；
* 受控动作：``post_journal_entry``、``reverse_journal_entry``、
  ``start_fiscal_period_closing``、``close_fiscal_period``；
* 只读试算函数：``compute_trial_balance``；
* 同步硬不变量：借贷不可同科目（``SELF_BALANCING_ACCOUNT_FORBIDDEN``）、
  严禁跨核算主体串户记账（``CROSS_ENTITY_JOURNAL_FORBIDDEN``）、
  关账期间禁过账与红冲（``PERIOD_CLOSED_POSTING_FORBIDDEN``）、
  无来源事件且超阈值手工调账触发 ``APPROVAL_REQUIRED_MANUAL_JOURNAL``（M1 ``WorkCard`` + $t_1$ 重算）、
  已过账凭证不可直接删改仅允许红字冲正且禁重复冲正（``ENTRY_ALREADY_REVERSED``）、
  关账前强制校验全实体试算平衡与会计恒等式（``TRIAL_BALANCE_UNBALANCED``）。
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from open_ontology.induction.dsl import DeclarativeBlueprint, compose_blueprints
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        AccountingEntity,
        FulfillmentEvent,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        AccountingEntity,
        FulfillmentEvent,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )

__all__ = [
    "MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS",
    "GlAccountCategory",
    "GlNormalSide",
    "spec",
    "AccountingEntity",
    "FulfillmentEvent",
    "WorkCard",
    "FiscalPeriod",
    "GlAccount",
    "JournalEntry",
    "TrialBalanceSnapshot",
    "PostJournalEntryParams",
    "ReverseJournalEntryParams",
    "StartFiscalPeriodClosingParams",
    "CloseFiscalPeriodParams",
    "ComputeTrialBalanceParams",
    "build_std_r2r_blueprint",
]

MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS: int = 500_000

GlAccountCategory = Literal["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"]
GlNormalSide = Literal["DEBIT", "CREDIT"]

_FISCAL_PERIOD_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("closing", "closed"),
    "closing": ("closed",),
    "closed": (),
}

_JOURNAL_ENTRY_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("posted",),
    "posted": ("reversed",),
    "reversed": (),
}

_TRIAL_BALANCE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "verified": (),
}

spec = OntologySpec(extends=(std_core_spec,))


@spec.object_type
class FiscalPeriod(MasterEntity):
    """标准 R2R 会计期间主数据（open -> closing -> closed 单向不可逆关账状态机）。"""

    __primary_key__: ClassVar[str] = "period_id"
    __title__: ClassVar[str] = "period_id"
    __aggregate_root__: ClassVar[bool] = True
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("open", "closing")
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _FISCAL_PERIOD_TRANSITIONS

    period_id: str
    accounting_entity: ObjectProperty[AccountingEntity]
    start_date: str
    end_date: str
    status: str = "open"


@spec.object_type
class GlAccount(MasterEntity):
    """标准 R2R 总账会计科目及借贷累计发生额台账。"""

    __primary_key__: ClassVar[str] = "account_code"
    __title__: ClassVar[str] = "name"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "debit_total_cents",
        "credit_total_cents",
    )

    account_code: str
    accounting_entity: ObjectProperty[AccountingEntity]
    name: str
    category: GlAccountCategory
    normal_side: GlNormalSide
    debit_total_cents: int = 0
    credit_total_cents: int = 0
    status: str = "active"


@spec.object_type
class JournalEntry(ProcessDocument):
    """标准 R2R 复式记账凭证（已过账凭证不可删改，只允许红字冲正）。"""

    __primary_key__: ClassVar[str] = "entry_id"
    __title__: ClassVar[str] = "entry_id"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("amount_cents",)
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft", "posted")
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _JOURNAL_ENTRY_TRANSITIONS

    entry_id: str
    accounting_entity: ObjectProperty[AccountingEntity]
    fiscal_period: ObjectProperty[FiscalPeriod]
    source_event: ObjectProperty[FulfillmentEvent] | None = None
    debit_account: ObjectProperty[GlAccount]
    credit_account: ObjectProperty[GlAccount]
    amount_cents: int
    memo: str = ""
    reversal_of: str = ""
    status: str = "draft"


@spec.object_type
class TrialBalanceSnapshot(ProcessDocument):
    """标准 R2R 关账试算平衡核验不可变快照凭证。"""

    __primary_key__: ClassVar[str] = "snapshot_id"
    __title__: ClassVar[str] = "snapshot_id"
    __aggregate_root__: ClassVar[bool] = False
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "total_debit_cents",
        "total_credit_cents",
        "entry_count",
    )
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("verified",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _TRIAL_BALANCE_TRANSITIONS

    snapshot_id: str
    accounting_entity: ObjectProperty[AccountingEntity]
    fiscal_period: ObjectProperty[FiscalPeriod]
    total_debit_cents: int
    total_credit_cents: int
    is_balanced: bool
    entry_count: int
    status: str = "verified"


class PostJournalEntryParams(BaseModel):
    entry_id: str


class ReverseJournalEntryParams(BaseModel):
    entry_id: str
    reversal_entry_id: str = ""
    target_period_id: str = ""
    memo: str = ""


class StartFiscalPeriodClosingParams(BaseModel):
    period_id: str


class CloseFiscalPeriodParams(BaseModel):
    period_id: str
    snapshot_id: str = ""


class ComputeTrialBalanceParams(BaseModel):
    accounting_entity: str
    fiscal_period: str = ""


@spec.invariant(
    "r2r_gl_account_valid_enums",
    code="INVALID_GL_ACCOUNT_ENUM",
    description="GlAccount 科目类别与余额方向必须属于标准会计枚举。",
)
def r2r_gl_account_valid_enums(view: Any) -> bool:
    for acc in view.query(GlAccount):
        if acc.category not in ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"):
            raise InvariantError(
                f"GlAccount:{acc.account_code} invalid category {acc.category!r}",
                code="INVALID_GL_ACCOUNT_ENUM",
            )
        if acc.normal_side not in ("DEBIT", "CREDIT"):
            raise InvariantError(
                f"GlAccount:{acc.account_code} invalid normal_side {acc.normal_side!r}",
                code="INVALID_GL_ACCOUNT_ENUM",
            )
    return True


@spec.invariant(
    "r2r_journal_no_self_balancing",
    code="SELF_BALANCING_ACCOUNT_FORBIDDEN",
    description="复式记账凭证借方科目与贷方科目不得为同一科目。",
)
def r2r_journal_no_self_balancing(view: Any) -> bool:
    for entry in view.query(JournalEntry):
        if entry.debit_account == entry.credit_account:
            raise InvariantError(
                f"JournalEntry:{entry.entry_id} debit_account == credit_account == {entry.debit_account!r}",
                code="SELF_BALANCING_ACCOUNT_FORBIDDEN",
            )
    return True


@spec.transition_invariant(
    "r2r_fiscal_period_fsm",
    code="ILLEGAL_DOCUMENT_TRANSITION",
    description="FiscalPeriod 必须严格遵守 open -> closing -> closed 不可逆关账状态机。",
)
def r2r_fiscal_period_fsm(diff: Any, _view: Any) -> bool:
    for change in diff.object_changes:
        if change.object_type != "FiscalPeriod":
            continue
        if change.kind == "create":
            after_status = (change.after or {}).get("status")
            if after_status not in ("open", "closing"):
                raise InvariantError(
                    f"FiscalPeriod:{change.object_id} created with invalid status {after_status!r}",
                    code="INVALID_INITIAL_DOCUMENT_STATUS",
                )
        elif change.kind == "update":
            before_status = (change.before or {}).get("status")
            after_status = (change.after or {}).get("status")
            if before_status != after_status:
                allowed = _FISCAL_PERIOD_TRANSITIONS.get(str(before_status), ())
                if after_status not in allowed:
                    raise InvariantError(
                        f"FiscalPeriod:{change.object_id} illegal transition {before_status!r} -> {after_status!r}",
                        code="ILLEGAL_DOCUMENT_TRANSITION",
                    )
    return True


def _has_completed_manual_journal_work_card(ctx: Any, entry_id: str) -> bool:
    for card in ctx.query(WorkCard):
        if (
            card.status == "COMPLETED"
            and card.target_object_type == "JournalEntry"
            and card.target_object_id == entry_id
            and card.requested_action == "post_journal_entry"
        ):
            return True
    return False


def _compute_entity_trial_balance_dict(
    ctx: Any,
    accounting_entity_id: str,
    fiscal_period_id: str = "",
) -> dict[str, Any]:
    accounts = [
        acc
        for acc in ctx.query(GlAccount)
        if acc.accounting_entity == accounting_entity_id
    ]
    total_debit = sum(int(acc.debit_total_cents) for acc in accounts)
    total_credit = sum(int(acc.credit_total_cents) for acc in accounts)
    category_nets: dict[str, int] = {
        "ASSET": 0,
        "EXPENSE": 0,
        "LIABILITY": 0,
        "EQUITY": 0,
        "REVENUE": 0,
    }
    for acc in accounts:
        if acc.category in ("ASSET", "EXPENSE"):
            category_nets[acc.category] += int(acc.debit_total_cents) - int(acc.credit_total_cents)
        else:
            category_nets[acc.category] += int(acc.credit_total_cents) - int(acc.debit_total_cents)

    asset_plus_expense = category_nets["ASSET"] + category_nets["EXPENSE"]
    liability_equity_revenue = (
        category_nets["LIABILITY"] + category_nets["EQUITY"] + category_nets["REVENUE"]
    )
    entries = [
        e
        for e in ctx.query(JournalEntry)
        if e.accounting_entity == accounting_entity_id
        and (not fiscal_period_id or e.fiscal_period == fiscal_period_id)
        and e.status in ("posted", "reversed")
    ]
    return {
        "accounting_entity": accounting_entity_id,
        "fiscal_period": fiscal_period_id,
        "account_count": len(accounts),
        "entry_count": len(entries),
        "total_debit_cents": total_debit,
        "total_credit_cents": total_credit,
        "is_balanced": total_debit == total_credit,
        "category_net_cents": category_nets,
        "asset_plus_expense_cents": asset_plus_expense,
        "liability_equity_revenue_cents": liability_equity_revenue,
        "accounting_equation_holds": asset_plus_expense == liability_equity_revenue,
    }


@spec.action(
    "post_journal_entry",
    PostJournalEntryParams,
    allowed_roles=("finance", "chief_accountant", "finance_director", "steward", "ai_agent", "admin"),
    touched_object_types=("JournalEntry", "GlAccount"),
    description="复式总账凭证过账：校验正金额、借贷异目、同主体、科目激活、期间开放及大额手工调账 WorkCard 审批门禁。",
)
def post_journal_entry(ctx: Any, params: PostJournalEntryParams) -> None:
    entry = ctx.require(JournalEntry, params.entry_id)
    if entry.status != "draft":
        raise InvariantError(
            f"JournalEntry:{entry.entry_id} status is {entry.status!r}, expected 'draft'",
            code="ENTRY_NOT_DRAFT",
        )
    if entry.amount_cents <= 0:
        raise InvariantError(
            f"JournalEntry:{entry.entry_id} amount_cents={entry.amount_cents} must be > 0",
            code="NON_POSITIVE_JOURNAL_AMOUNT",
        )
    if entry.debit_account == entry.credit_account:
        raise InvariantError(
            f"JournalEntry:{entry.entry_id} debit_account == credit_account == {entry.debit_account!r}",
            code="SELF_BALANCING_ACCOUNT_FORBIDDEN",
        )

    debit_acc = ctx.require(GlAccount, entry.debit_account)
    credit_acc = ctx.require(GlAccount, entry.credit_account)
    period = ctx.require(FiscalPeriod, entry.fiscal_period)

    if (
        debit_acc.accounting_entity != entry.accounting_entity
        or credit_acc.accounting_entity != entry.accounting_entity
        or period.accounting_entity != entry.accounting_entity
    ):
        raise InvariantError(
            f"JournalEntry:{entry.entry_id} spans multiple AccountingEntity scopes",
            code="CROSS_ENTITY_JOURNAL_FORBIDDEN",
        )
    if debit_acc.status != "active" or credit_acc.status != "active":
        raise InvariantError(
            f"JournalEntry:{entry.entry_id} references frozen GlAccount",
            code="GL_ACCOUNT_FROZEN",
        )
    if period.status != "open":
        raise InvariantError(
            f"FiscalPeriod:{period.period_id} status is {period.status!r}; posting is forbidden",
            code="PERIOD_CLOSED_POSTING_FORBIDDEN",
        )

    is_manual = not entry.source_event
    if (
        is_manual
        and entry.amount_cents >= MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS
        and ctx.actor.role not in ("finance_director", "admin")
        and not _has_completed_manual_journal_work_card(ctx, entry.entry_id)
    ):
        raise InvariantError(
            f"Manual JournalEntry:{entry.entry_id} amount_cents={entry.amount_cents} >= {MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS} requires finance_director approval",
            code="APPROVAL_REQUIRED_MANUAL_JOURNAL",
        )

    ctx.modify(debit_acc, debit_total_cents=debit_acc.debit_total_cents + entry.amount_cents)
    ctx.modify(credit_acc, credit_total_cents=credit_acc.credit_total_cents + entry.amount_cents)
    ctx.modify(entry, status="posted")


@spec.action(
    "reverse_journal_entry",
    ReverseJournalEntryParams,
    allowed_roles=("chief_accountant", "finance_director", "finance", "steward", "admin"),
    touched_object_types=("JournalEntry", "GlAccount"),
    description="对已过账凭证执行借贷互换红字冲正（禁直接删改已过账凭证、禁重复冲正、禁跨关账期间冲正）。",
)
def reverse_journal_entry(ctx: Any, params: ReverseJournalEntryParams) -> None:
    orig = ctx.require(JournalEntry, params.entry_id)
    if orig.status == "reversed" or any(
        e.reversal_of == orig.entry_id for e in ctx.query(JournalEntry)
    ):
        raise InvariantError(
            f"JournalEntry:{orig.entry_id} is already reversed",
            code="ENTRY_ALREADY_REVERSED",
        )
    if orig.status != "posted":
        raise InvariantError(
            f"JournalEntry:{orig.entry_id} status is {orig.status!r}, expected 'posted' to reverse",
            code="ENTRY_NOT_POSTED",
        )

    target_period_id = params.target_period_id.strip() or orig.fiscal_period
    period = ctx.require(FiscalPeriod, target_period_id)
    if period.accounting_entity != orig.accounting_entity:
        raise InvariantError(
            f"Target FiscalPeriod:{period.period_id} belongs to {period.accounting_entity!r}, expected {orig.accounting_entity!r}",
            code="CROSS_ENTITY_JOURNAL_FORBIDDEN",
        )
    if period.status != "open":
        raise InvariantError(
            f"FiscalPeriod:{period.period_id} status is {period.status!r}; reversal posting is forbidden",
            code="PERIOD_CLOSED_POSTING_FORBIDDEN",
        )

    orig_debit = ctx.require(GlAccount, orig.debit_account)
    orig_credit = ctx.require(GlAccount, orig.credit_account)
    if orig_debit.status != "active" or orig_credit.status != "active":
        raise InvariantError(
            f"JournalEntry:{orig.entry_id} references frozen GlAccount",
            code="GL_ACCOUNT_FROZEN",
        )

    rev_id = params.reversal_entry_id.strip() or f"{orig.entry_id}-REV-{uuid.uuid4().hex[:8].upper()}"
    if ctx.get(JournalEntry, rev_id) is not None:
        raise InvariantError(f"JournalEntry:{rev_id} already exists", code="JOURNAL_ENTRY_EXISTS")

    ctx.create(
        JournalEntry(
            entry_id=rev_id,
            accounting_entity=orig.accounting_entity,
            fiscal_period=period.period_id,
            source_event=orig.source_event,
            debit_account=orig.credit_account,
            credit_account=orig.debit_account,
            amount_cents=orig.amount_cents,
            memo=params.memo.strip() or f"Reversal of {orig.entry_id}",
            reversal_of=orig.entry_id,
            status="posted",
        )
    )
    ctx.modify(orig_credit, debit_total_cents=orig_credit.debit_total_cents + orig.amount_cents)
    ctx.modify(orig_debit, credit_total_cents=orig_debit.credit_total_cents + orig.amount_cents)
    ctx.modify(orig, status="reversed")


@spec.action(
    "start_fiscal_period_closing",
    StartFiscalPeriodClosingParams,
    allowed_roles=("chief_accountant", "finance_director", "admin"),
    touched_object_types=("FiscalPeriod",),
    description="将开放中的会计期间切入结账中状态（open -> closing），冻结日常凭证过账。",
)
def start_fiscal_period_closing(ctx: Any, params: StartFiscalPeriodClosingParams) -> None:
    period = ctx.require(FiscalPeriod, params.period_id)
    if period.status != "open":
        raise InvariantError(
            f"FiscalPeriod:{period.period_id} status is {period.status!r}, expected 'open'",
            code="PERIOD_NOT_OPEN",
        )
    ctx.modify(period, status="closing")


@spec.action(
    "close_fiscal_period",
    CloseFiscalPeriodParams,
    allowed_roles=("chief_accountant", "finance_director", "admin"),
    touched_object_types=("FiscalPeriod", "TrialBalanceSnapshot"),
    description="关账前强制复核全实体试算平衡并落库不可变试算快照，将期间闭环为 closed。",
)
def close_fiscal_period(ctx: Any, params: CloseFiscalPeriodParams) -> None:
    period = ctx.require(FiscalPeriod, params.period_id)
    if period.status not in ("open", "closing"):
        raise InvariantError(
            f"FiscalPeriod:{period.period_id} is already {period.status!r}",
            code="PERIOD_ALREADY_CLOSED",
        )

    tb = _compute_entity_trial_balance_dict(ctx, period.accounting_entity, period.period_id)
    if not tb["is_balanced"] or not tb["accounting_equation_holds"]:
        raise InvariantError(
            f"Trial balance unbalanced for AccountingEntity:{period.accounting_entity}: "
            f"debit={tb['total_debit_cents']} != credit={tb['total_credit_cents']}",
            code="TRIAL_BALANCE_UNBALANCED",
        )

    snap_id = params.snapshot_id.strip() or f"TBS-{period.accounting_entity}-{period.period_id}"
    if ctx.get(TrialBalanceSnapshot, snap_id) is not None:
        raise InvariantError(
            f"TrialBalanceSnapshot:{snap_id} already exists",
            code="TRIAL_BALANCE_SNAPSHOT_EXISTS",
        )

    ctx.create(
        TrialBalanceSnapshot(
            snapshot_id=snap_id,
            accounting_entity=period.accounting_entity,
            fiscal_period=period.period_id,
            total_debit_cents=tb["total_debit_cents"],
            total_credit_cents=tb["total_credit_cents"],
            is_balanced=True,
            entry_count=tb["entry_count"],
            status="verified",
        )
    )
    ctx.modify(period, status="closed")


@spec.function(
    "compute_trial_balance",
    ComputeTrialBalanceParams,
    description="实时按法人核算主体计算借贷总发生额、试算是否平衡及资产+费用=负债+权益+收入恒等式。",
)
def compute_trial_balance(ctx: Any, params: ComputeTrialBalanceParams) -> dict[str, Any]:
    ctx.require(AccountingEntity, params.accounting_entity)
    return _compute_entity_trial_balance_dict(
        ctx,
        params.accounting_entity,
        params.fiscal_period.strip(),
    )


spec.update_display(
    {
        "ontologies": {
            "std_r2r_ontology": "标准 R2R 领域本体（会计期间·总账科目·复式凭证·试算平衡）",
        },
        "objects": {
            "FiscalPeriod": "会计期间",
            "GlAccount": "总账科目",
            "JournalEntry": "复式记账凭证",
            "TrialBalanceSnapshot": "试算平衡快照",
        },
        "actions": {
            "post_journal_entry": "总账凭证过账",
            "reverse_journal_entry": "凭证红字冲正",
            "start_fiscal_period_closing": "启动期间结账",
            "close_fiscal_period": "会计期间关账",
        },
        "functions": {
            "compute_trial_balance": "计算实时试算平衡",
        },
        "fields": {
            "period_id": "会计期间编码",
            "accounting_entity": "核算主体",
            "start_date": "开始日期",
            "end_date": "结束日期",
            "account_code": "会计科目编码",
            "name": "名称",
            "category": "科目五大类别",
            "normal_side": "余额正常方向",
            "debit_total_cents": "累计借方发生额（分）",
            "credit_total_cents": "累计贷方发生额（分）",
            "entry_id": "记账凭证编码",
            "fiscal_period": "所属会计期间",
            "source_event": "来源履约事件",
            "debit_account": "借方科目",
            "credit_account": "贷方科目",
            "amount_cents": "凭证金额（分）",
            "memo": "摘要",
            "reversal_of": "被冲正原凭证",
            "snapshot_id": "试算快照编码",
            "total_debit_cents": "总借方发生额（分）",
            "total_credit_cents": "总贷方发生额（分）",
            "is_balanced": "是否试算平衡",
            "entry_count": "凭证笔数",
            "status": "状态",
        },
    }
)


def build_std_r2r_blueprint() -> DeclarativeBlueprint:
    """构建与 ``std_r2r_ontology`` 对称的零 ``exec/eval`` 声明式领域蓝图（基于 std_core 组合）。"""
    r2r_fragment: dict[str, Any] = {
        "model_id": "_std_r2r_domain_fragment",
        "version": "1.0.0",
        "seed_ontology_id": "business_ontology",
        "object_types": [
            {
                "name": "FiscalPeriod",
                "archetype": "MasterEntity",
                "primary_key": "period_id",
                "title_field": "period_id",
                "aggregate_root": True,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["open", "closing"],
                "transitions": {k: list(v) for k, v in _FISCAL_PERIOD_TRANSITIONS.items()},
                "display_label": "会计期间",
                "fields": [
                    {"name": "period_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "start_date", "field_type": "str", "required": True},
                    {"name": "end_date", "field_type": "str", "required": True},
                    {"name": "status", "field_type": "str", "default": "open"},
                ],
                "relations": [
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                ],
            },
            {
                "name": "GlAccount",
                "archetype": "MasterEntity",
                "primary_key": "account_code",
                "title_field": "name",
                "aggregate_root": True,
                "owned": False,
                "display_label": "总账科目",
                "fields": [
                    {"name": "account_code", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "name", "field_type": "str", "required": True},
                    {"name": "category", "field_type": "str", "required": True},
                    {"name": "normal_side", "field_type": "str", "required": True},
                    {"name": "debit_total_cents", "field_type": "int", "default": 0, "non_negative": True},
                    {"name": "credit_total_cents", "field_type": "int", "default": 0, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
                "relations": [
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                ],
            },
            {
                "name": "JournalEntry",
                "archetype": "ProcessDocument",
                "primary_key": "entry_id",
                "title_field": "entry_id",
                "aggregate_root": True,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["draft", "posted"],
                "transitions": {k: list(v) for k, v in _JOURNAL_ENTRY_TRANSITIONS.items()},
                "display_label": "复式记账凭证",
                "fields": [
                    {"name": "entry_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "amount_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "memo", "field_type": "str", "default": ""},
                    {"name": "reversal_of", "field_type": "str", "default": ""},
                    {"name": "status", "field_type": "str", "default": "draft"},
                ],
                "relations": [
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                    {"field": "fiscal_period", "target_type": "FiscalPeriod", "required": True},
                    {"field": "source_event", "target_type": "FulfillmentEvent", "required": False},
                    {"field": "debit_account", "target_type": "GlAccount", "required": True},
                    {"field": "credit_account", "target_type": "GlAccount", "required": True},
                ],
            },
            {
                "name": "TrialBalanceSnapshot",
                "archetype": "ProcessDocument",
                "primary_key": "snapshot_id",
                "title_field": "snapshot_id",
                "aggregate_root": False,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["verified"],
                "transitions": {k: list(v) for k, v in _TRIAL_BALANCE_TRANSITIONS.items()},
                "display_label": "试算平衡快照",
                "fields": [
                    {"name": "snapshot_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "total_debit_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "total_credit_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "is_balanced", "field_type": "bool", "required": True},
                    {"name": "entry_count", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "verified"},
                ],
                "relations": [
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                    {"field": "fiscal_period", "target_type": "FiscalPeriod", "required": True},
                ],
            },
        ],
        "invariants": [
            {
                "name": "r2r_gl_account_valid_enums",
                "code": "INVALID_GL_ACCOUNT_ENUM",
                "target_type": "GlAccount",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {
                            "op": "in",
                            "left": {"ref": "obj.category"},
                            "right": {"lit": ["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"]},
                        },
                        {
                            "op": "in",
                            "left": {"ref": "obj.normal_side"},
                            "right": {"lit": ["DEBIT", "CREDIT"]},
                        },
                    ],
                },
                "description": "GlAccount 科目类别与余额方向必须属于标准会计枚举。",
            },
            {
                "name": "r2r_journal_no_self_balancing",
                "code": "SELF_BALANCING_ACCOUNT_FORBIDDEN",
                "target_type": "JournalEntry",
                "must_hold": {
                    "op": "ne",
                    "left": {"ref": "obj.debit_account"},
                    "right": {"ref": "obj.credit_account"},
                },
                "description": "复式记账凭证借方科目与贷方科目不得为同一科目。",
            },
        ],
        "actions": [
            {
                "name": "post_journal_entry",
                "display_label": "总账凭证过账",
                "description": "复式总账凭证过账：校验正金额、借贷异目、同主体、科目激活、期间开放及大额手工调账审批门禁。",
                "allowed_roles": ["finance", "chief_accountant", "finance_director", "steward", "ai_agent", "admin"],
                "params": [{"name": "entry_id", "field_type": "str", "required": True}],
                "preconditions": [
                    {
                        "name": "r2r_post_entry_must_be_draft",
                        "code": "ENTRY_NOT_DRAFT",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    },
                    {
                        "name": "r2r_post_positive_amount",
                        "code": "NON_POSITIVE_JOURNAL_AMOUNT",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {"op": "gt", "left": {"ref": "obj.amount_cents"}, "right": {"lit": 0}},
                    },
                    {
                        "name": "r2r_post_distinct_accounts",
                        "code": "SELF_BALANCING_ACCOUNT_FORBIDDEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {
                            "op": "ne",
                            "left": {"ref": "obj.debit_account"},
                            "right": {"ref": "obj.credit_account"},
                        },
                    },
                    {
                        "name": "r2r_post_same_accounting_entity",
                        "code": "CROSS_ENTITY_JOURNAL_FORBIDDEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "eq",
                                    "left": {"ref": "obj.debit_account.accounting_entity"},
                                    "right": {"ref": "obj.accounting_entity"},
                                },
                                {
                                    "op": "eq",
                                    "left": {"ref": "obj.credit_account.accounting_entity"},
                                    "right": {"ref": "obj.accounting_entity"},
                                },
                                {
                                    "op": "eq",
                                    "left": {"ref": "obj.fiscal_period.accounting_entity"},
                                    "right": {"ref": "obj.accounting_entity"},
                                },
                            ],
                        },
                    },
                    {
                        "name": "r2r_post_accounts_active",
                        "code": "GL_ACCOUNT_FROZEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "obj.debit_account.status"}, "right": {"lit": "active"}},
                                {"op": "eq", "left": {"ref": "obj.credit_account.status"}, "right": {"lit": "active"}},
                            ],
                        },
                    },
                    {
                        "name": "r2r_post_period_open",
                        "code": "PERIOD_CLOSED_POSTING_FORBIDDEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.fiscal_period.status"},
                            "right": {"lit": "open"},
                        },
                    },
                    {
                        "name": "r2r_post_manual_journal_approval_gate",
                        "code": "APPROVAL_REQUIRED_MANUAL_JOURNAL",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "when": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "or",
                                    "args": [
                                        {"op": "eq", "left": {"ref": "obj.source_event"}, "right": {"lit": None}},
                                        {"op": "eq", "left": {"ref": "obj.source_event"}, "right": {"lit": ""}},
                                    ],
                                },
                                {
                                    "op": "ge",
                                    "left": {"ref": "obj.amount_cents"},
                                    "right": {"lit": MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS},
                                },
                            ],
                        },
                        "must_hold": {
                            "op": "or",
                            "args": [
                                {"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["finance_director", "admin"]}},
                                {
                                    "op": "exists",
                                    "object_type": "WorkCard",
                                    "where": {
                                        "op": "and",
                                        "args": [
                                            {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "COMPLETED"}},
                                            {"op": "eq", "left": {"ref": "target.target_object_type"}, "right": {"lit": "JournalEntry"}},
                                            {"op": "eq", "left": {"ref": "target.target_object_id"}, "right": {"ref": "obj.entry_id"}},
                                            {"op": "eq", "left": {"ref": "target.requested_action"}, "right": {"lit": "post_journal_entry"}},
                                        ],
                                    },
                                },
                            ],
                        },
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "GlAccount",
                        "target_ref": "obj.debit_account",
                        "set_fields": {
                            "debit_total_cents": {
                                "op": "add",
                                "left": {"ref": "obj.debit_total_cents"},
                                "right": {"ref": "source_obj.amount_cents"},
                            }
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "GlAccount",
                        "target_ref": "obj.credit_account",
                        "set_fields": {
                            "credit_total_cents": {
                                "op": "add",
                                "left": {"ref": "obj.credit_total_cents"},
                                "right": {"ref": "source_obj.amount_cents"},
                            }
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "JournalEntry",
                        "id_param": "entry_id",
                        "set_fields": {"status": {"lit": "posted"}},
                    },
                ],
            },
            {
                "name": "reverse_journal_entry",
                "display_label": "凭证红字冲正",
                "description": "对已过账凭证执行借贷互换红字冲正（禁直接删改已过账凭证、禁重复冲正、禁跨关账期间冲正）。",
                "allowed_roles": ["chief_accountant", "finance_director", "finance", "steward", "admin"],
                "params": [
                    {"name": "entry_id", "field_type": "str", "required": True},
                    {"name": "reversal_entry_id", "field_type": "str", "default": ""},
                    {"name": "target_period_id", "field_type": "str", "default": ""},
                    {"name": "memo", "field_type": "str", "default": ""},
                ],
                "preconditions": [
                    {
                        "name": "r2r_reverse_not_already_reversed",
                        "code": "ENTRY_ALREADY_REVERSED",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "ne", "left": {"ref": "obj.status"}, "right": {"lit": "reversed"}},
                                {
                                    "op": "not_exists",
                                    "object_type": "JournalEntry",
                                    "where": {
                                        "op": "eq",
                                        "left": {"ref": "target.reversal_of"},
                                        "right": {"ref": "obj.entry_id"},
                                    },
                                },
                            ],
                        },
                    },
                    {
                        "name": "r2r_reverse_must_be_posted",
                        "code": "ENTRY_NOT_POSTED",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "posted"}},
                    },
                    {
                        "name": "r2r_reverse_default_period_open",
                        "code": "PERIOD_CLOSED_POSTING_FORBIDDEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "when": {"op": "eq", "left": {"ref": "param.target_period_id"}, "right": {"lit": ""}},
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.fiscal_period.status"},
                            "right": {"lit": "open"},
                        },
                    },
                    {
                        "name": "r2r_reverse_target_period_open",
                        "code": "PERIOD_CLOSED_POSTING_FORBIDDEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "when": {"op": "ne", "left": {"ref": "param.target_period_id"}, "right": {"lit": ""}},
                        "must_hold": {
                            "op": "exists",
                            "object_type": "FiscalPeriod",
                            "where": {
                                "op": "and",
                                "args": [
                                    {"op": "eq", "left": {"ref": "target.period_id"}, "right": {"ref": "param.target_period_id"}},
                                    {"op": "eq", "left": {"ref": "target.accounting_entity"}, "right": {"ref": "obj.accounting_entity"}},
                                    {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "open"}},
                                ],
                            },
                        },
                    },
                    {
                        "name": "r2r_reverse_accounts_active",
                        "code": "GL_ACCOUNT_FROZEN",
                        "target_type": "JournalEntry",
                        "id_param": "entry_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "obj.debit_account.status"}, "right": {"lit": "active"}},
                                {"op": "eq", "left": {"ref": "obj.credit_account.status"}, "right": {"lit": "active"}},
                            ],
                        },
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "GlAccount",
                        "target_ref": "obj.credit_account",
                        "set_fields": {
                            "debit_total_cents": {
                                "op": "add",
                                "left": {"ref": "obj.debit_total_cents"},
                                "right": {"ref": "source_obj.amount_cents"},
                            }
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "GlAccount",
                        "target_ref": "obj.debit_account",
                        "set_fields": {
                            "credit_total_cents": {
                                "op": "add",
                                "left": {"ref": "obj.credit_total_cents"},
                                "right": {"ref": "source_obj.amount_cents"},
                            }
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "JournalEntry",
                        "id_param": "entry_id",
                        "set_fields": {"status": {"lit": "reversed"}},
                    },
                ],
            },
            {
                "name": "start_fiscal_period_closing",
                "display_label": "启动期间结账",
                "description": "将开放中的会计期间切入结账中状态（open -> closing），冻结日常凭证过账。",
                "allowed_roles": ["chief_accountant", "finance_director", "admin"],
                "params": [{"name": "period_id", "field_type": "str", "required": True}],
                "preconditions": [
                    {
                        "name": "r2r_start_closing_period_open",
                        "code": "PERIOD_NOT_OPEN",
                        "target_type": "FiscalPeriod",
                        "id_param": "period_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "open"}},
                    }
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "FiscalPeriod",
                        "id_param": "period_id",
                        "set_fields": {"status": {"lit": "closing"}},
                    }
                ],
            },
            {
                "name": "close_fiscal_period",
                "display_label": "会计期间关账",
                "description": "关账前强制复核全实体试算平衡，将期间闭环为 closed。",
                "allowed_roles": ["chief_accountant", "finance_director", "admin"],
                "params": [
                    {"name": "period_id", "field_type": "str", "required": True},
                    {"name": "snapshot_id", "field_type": "str", "default": ""},
                ],
                "preconditions": [
                    {
                        "name": "r2r_close_period_not_closed",
                        "code": "PERIOD_ALREADY_CLOSED",
                        "target_type": "FiscalPeriod",
                        "id_param": "period_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["open", "closing"]},
                        },
                    },
                    {
                        "name": "r2r_close_trial_balance_must_equal",
                        "code": "TRIAL_BALANCE_UNBALANCED",
                        "target_type": "FiscalPeriod",
                        "id_param": "period_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {
                                "op": "sum",
                                "object_type": "GlAccount",
                                "field": "debit_total_cents",
                                "where": {
                                    "op": "eq",
                                    "left": {"ref": "target.accounting_entity"},
                                    "right": {"ref": "obj.accounting_entity"},
                                },
                            },
                            "right": {
                                "op": "sum",
                                "object_type": "GlAccount",
                                "field": "credit_total_cents",
                                "where": {
                                    "op": "eq",
                                    "left": {"ref": "target.accounting_entity"},
                                    "right": {"ref": "obj.accounting_entity"},
                                },
                            },
                        },
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "FiscalPeriod",
                        "id_param": "period_id",
                        "set_fields": {"status": {"lit": "closed"}},
                    }
                ],
            },
        ],
        "display": {
            "ontologies": {
                "std_r2r_ontology": "标准 R2R 领域本体（会计期间·总账科目·复式凭证·试算平衡）",
            },
            "objects": {
                "FiscalPeriod": "会计期间",
                "GlAccount": "总账科目",
                "JournalEntry": "复式记账凭证",
                "TrialBalanceSnapshot": "试算平衡快照",
            },
            "actions": {
                "post_journal_entry": "总账凭证过账",
                "reverse_journal_entry": "凭证红字冲正",
                "start_fiscal_period_closing": "启动期间结账",
                "close_fiscal_period": "会计期间关账",
            },
        },
    }
    composed = compose_blueprints(
        [build_std_core_blueprint(), r2r_fragment],
        model_id="std_r2r_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="标准 R2R 领域本体蓝图：在核心底座上组合会计期间、总账科目、复式记账凭证与试算平衡快照。",
    )
    composed.imports = ["std_core_ontology"]
    return composed
