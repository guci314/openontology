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

from collections import defaultdict
from typing import Any, ClassVar

from open_ontology.spec import InvariantError, ObjectType, OntologySpec

__all__ = [
    "spec",
    "MasterEntity",
    "BalanceLedger",
    "ProcessDocument",
    "EventVoucher",
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


def build_business_seed_ontology() -> Any:
    """将 ``business_ontology`` 封装为可供符号训练器插拔的企业领域种子本体论底座。"""
    from open_ontology.induction.seed import seed_from_spec

    return seed_from_spec(
        seed_id="business_ontology",
        domain_name="Enterprise Business & ERP",
        base_spec=spec,
        description=(
            "企业商业领域通用种子本体：提供 MasterEntity（主数据）、ProcessDocument（状态机单据）、"
            "BalanceLedger（非负+账证守恒台账）、EventVoucher（只追加不可变凭证）四大原型及其可继承公理。"
        ),
        archetype_role_hints={
            "MasterEntity": ("master_data", "low_frequency_reference", "aggregate_root"),
            "ProcessDocument": ("lifecycle_state_machine", "workflow_document", "approval_subject"),
            "BalanceLedger": ("stock_or_balance_accumulator", "voucher_conserved", "non_negative"),
            "EventVoucher": ("append_only_fact", "ledger_delta_entry", "audit_voucher"),
        },
        default_archetype="ObjectType",
        extra_prior_notes=(
            "Every BalanceLedger numeric balance field mutated by an action MUST declare "
            "voucher_delta_rules and emit matching EventVoucher delta records in the same transaction.",
            "Every ProcessDocument status field MUST follow declared finite-state transitions.",
            "Every EventVoucher record is append-only and immutable after creation.",
        ),
    )


BUSINESS_SEED_ONTOLOGY = build_business_seed_ontology()
