"""标准 H2R（Hire-to-Retire，组织人事与动态授权）纯业务领域本体（Pure M1）。

严守常识细腰架构、Gate 3 零 M2 词表碰撞（绝对避开 ``OrgUnit / OrgMember / OrgDirectory``）
与 Gate 7 零横向互耦原则，仅基于 ``std_core_ontology`` 扩展：
* 主数据与单据对象：``Department``（部门树）、``JobPosition``（岗位编制与闭集角色定编）、
  ``Employee``（员工档案）、``AuthorityMandate``（动态授权委派凭证）；
* 受控动作：``reparent_department``、``assign_employee_position``、
  ``grant_authority_mandate``、``offboard_employee``；
* 只读查询函数：``resolve_employee_effective_mandates``；
* 同步硬不变量：部门 DAG 绝对无环（``DEPARTMENT_CYCLE_DETECTED``）、
  岗位/授权角色强制命中 ``STD_ROLES`` 闭集（``UNKNOWN_ROLE``）、
  离职员工禁调岗（``EMPLOYEE_OFFBOARDED``）、
  授权禁自授（``MANDATE_SELF_GRANT_FORBIDDEN``）、
  禁越权委派（``MANDATE_EXCEEDS_GRANTOR_CEILING`` / ``APPROVAL_REQUIRED_AUTHORITY_ESCALATION``）、
  员工离职同事务自动吊销全部名下 ``active`` 授权防幽灵权限残留。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from open_ontology.induction.dsl import (
    ActionBlueprint,
    ActionMutationSpec,
    DeclarativeBlueprint,
    InvariantRuleSpec,
    enforce_edit_function_contracts,
    export_spec_blueprint,
    register_rule_actions,
)
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import (
        MasterEntity,
        ProcessDocument,
        apply_planned_ops,
        collect_read_set,
    )
    from .std_core_ontology import (
        STD_ROLES,
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import (
        MasterEntity,
        ProcessDocument,
        apply_planned_ops,
        collect_read_set,
    )
    from examples.std_core_ontology import (
        STD_ROLES,
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )

__all__ = [
    "VALID_STD_ROLE_NAMES",
    "spec",
    "AccountingEntity",
    "BusinessParty",
    "WorkCard",
    "Department",
    "JobPosition",
    "Employee",
    "AuthorityMandate",
    "ReparentDepartmentParams",
    "AssignEmployeePositionParams",
    "GrantAuthorityMandateParams",
    "OffboardEmployeeParams",
    "ResolveEmployeeMandatesParams",
    "build_std_h2r_blueprint",
]

VALID_STD_ROLE_NAMES: tuple[str, ...] = tuple(role_name for role_name, _ in STD_ROLES)

_EMPLOYEE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "onboarding": ("active", "offboarded"),
    "active": ("suspended", "offboarded"),
    "suspended": ("active", "offboarded"),
    "offboarded": (),
}

_AUTHORITY_MANDATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "active": ("revoked", "expired"),
    "revoked": (),
    "expired": (),
}

spec = OntologySpec(extends=(std_core_spec,))


@spec.object_type
class Department(MasterEntity):
    """标准 H2R 部门组织节点（支持上级自引用与 DAG 无环护栏）。"""

    __primary_key__: ClassVar[str] = "department_id"
    __title__: ClassVar[str] = "name"
    __aggregate_root__: ClassVar[bool] = True

    department_id: str
    accounting_entity: ObjectProperty[AccountingEntity]
    name: str
    parent_department: ObjectProperty["Department"] | None = None
    scope_tag: str = ""
    status: str = "active"


@spec.object_type
class JobPosition(MasterEntity):
    """标准 H2R 岗位定编（强制绑定闭集 ``STD_ROLES`` 与审批金额上限）。"""

    __primary_key__: ClassVar[str] = "position_id"
    __title__: ClassVar[str] = "title"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("approval_ceiling_cents",)

    position_id: str
    department: ObjectProperty[Department]
    title: str
    role_name: str
    approval_ceiling_cents: int = 0
    status: str = "active"


@spec.object_type
class Employee(MasterEntity):
    """标准 H2R 员工主数据档案（入职 -> 在职 -> 停职 / 离职生命周期）。"""

    __primary_key__: ClassVar[str] = "employee_id"
    __title__: ClassVar[str] = "full_name"
    __aggregate_root__: ClassVar[bool] = True
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("onboarding", "active")
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _EMPLOYEE_TRANSITIONS

    employee_id: str
    party: ObjectProperty[BusinessParty] | None = None
    department: ObjectProperty[Department]
    position: ObjectProperty[JobPosition]
    full_name: str
    actor_id: str
    status: str = "onboarding"


@spec.object_type
class AuthorityMandate(ProcessDocument):
    """标准 H2R 动态权限委派凭证（强守 SoD 自授权禁止、上限不越权与离职同事务收权）。"""

    __primary_key__: ClassVar[str] = "mandate_id"
    __title__: ClassVar[str] = "mandate_id"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("ceiling_cents",)
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("active",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _AUTHORITY_MANDATE_TRANSITIONS

    mandate_id: str
    grantor: ObjectProperty[Employee]
    grantee: ObjectProperty[Employee]
    role_name: str
    ceiling_cents: int
    scope_tag: str = ""
    status: str = "active"


class ReparentDepartmentParams(BaseModel):
    department_id: str
    new_parent_id: str = ""


class AssignEmployeePositionParams(BaseModel):
    employee_id: str
    department_id: str
    position_id: str
    activate: bool = True


class GrantAuthorityMandateParams(BaseModel):
    mandate_id: str
    grantor_id: str
    grantee_id: str
    role_name: str
    ceiling_cents: int
    scope_tag: str = ""
    escalate_approval: bool = False


class OffboardEmployeeParams(BaseModel):
    employee_id: str
    reason: str = ""


class ResolveEmployeeMandatesParams(BaseModel):
    employee_id: str


@spec.invariant(
    "h2r_department_dag_acyclic",
    code="DEPARTMENT_CYCLE_DETECTED",
    description="Department 组织层级必须为无环有向树（DAG），禁止指向自身或后代部门。",
)
def h2r_department_dag_acyclic(view: Any) -> bool:
    depts = {d.department_id: d for d in view.query(Department)}
    for dept_id in sorted(depts):
        seen: set[str] = set()
        cur_id: str | None = dept_id
        while cur_id:
            if cur_id in seen:
                raise InvariantError(
                    f"Cycle detected in Department hierarchy involving {cur_id!r}",
                    code="DEPARTMENT_CYCLE_DETECTED",
                )
            seen.add(cur_id)
            cur_dept = depts.get(cur_id)
            cur_id = cur_dept.parent_department if cur_dept is not None else None
    return True


@spec.invariant(
    "h2r_job_position_closed_role",
    code="UNKNOWN_ROLE",
    description="JobPosition.role_name 必须命中闭集 STD_ROLES。",
)
def h2r_job_position_closed_role(view: Any) -> bool:
    valid = set(VALID_STD_ROLE_NAMES)
    for pos in view.query(JobPosition):
        if pos.role_name not in valid:
            raise InvariantError(
                f"JobPosition:{pos.position_id}.role_name={pos.role_name!r} is not in STD_ROLES",
                code="UNKNOWN_ROLE",
            )
    return True


@spec.invariant(
    "h2r_mandate_closed_role",
    code="UNKNOWN_ROLE",
    description="AuthorityMandate.role_name 必须命中闭集 STD_ROLES。",
)
def h2r_mandate_closed_role(view: Any) -> bool:
    valid = set(VALID_STD_ROLE_NAMES)
    for mandate in view.query(AuthorityMandate):
        if mandate.role_name not in valid:
            raise InvariantError(
                f"AuthorityMandate:{mandate.mandate_id}.role_name={mandate.role_name!r} is not in STD_ROLES",
                code="UNKNOWN_ROLE",
            )
    return True


@spec.invariant(
    "h2r_mandate_no_self_grant",
    code="MANDATE_SELF_GRANT_FORBIDDEN",
    description="职责分离（SoD）：AuthorityMandate 授权人与被授权人不得为同一员工。",
)
def h2r_mandate_no_self_grant(view: Any) -> bool:
    for mandate in view.query(AuthorityMandate):
        if mandate.grantor == mandate.grantee:
            raise InvariantError(
                f"AuthorityMandate:{mandate.mandate_id} grantor == grantee == {mandate.grantor!r}",
                code="MANDATE_SELF_GRANT_FORBIDDEN",
            )
    return True


@spec.invariant(
    "h2r_no_active_mandate_for_offboarded",
    code="OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN",
    description="已离职员工（无论作为被授权人还是原授权人）名下不得保留任何 active 状态的 AuthorityMandate。",
)
def h2r_no_active_mandate_for_offboarded(view: Any) -> bool:
    for mandate in view.query(AuthorityMandate):
        if mandate.status != "active":
            continue
        grantee = view.get(Employee, mandate.grantee)
        if grantee is not None and grantee.status == "offboarded":
            raise InvariantError(
                f"AuthorityMandate:{mandate.mandate_id} remains active for offboarded grantee Employee:{grantee.employee_id}",
                code="OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN",
            )
        grantor = view.get(Employee, mandate.grantor)
        if grantor is not None and grantor.status == "offboarded":
            raise InvariantError(
                f"AuthorityMandate:{mandate.mandate_id} remains active for offboarded grantor Employee:{grantor.employee_id}",
                code="OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN",
            )
    return True


@spec.transition_invariant(
    "h2r_employee_fsm",
    code="ILLEGAL_DOCUMENT_TRANSITION",
    description="Employee 状态跃迁必须符合 onboarding -> active -> suspended / offboarded 生命周期。",
)
def h2r_employee_fsm(diff: Any, _view: Any) -> bool:
    for change in diff.object_changes:
        if change.object_type != "Employee":
            continue
        if change.kind == "create":
            after_status = (change.after or {}).get("status")
            if after_status not in ("onboarding", "active"):
                raise InvariantError(
                    f"Employee:{change.object_id} created with invalid status {after_status!r}",
                    code="INVALID_INITIAL_DOCUMENT_STATUS",
                )
        elif change.kind == "update":
            before_status = (change.before or {}).get("status")
            after_status = (change.after or {}).get("status")
            if before_status != after_status:
                allowed = _EMPLOYEE_TRANSITIONS.get(str(before_status), ())
                if after_status not in allowed:
                    raise InvariantError(
                        f"Employee:{change.object_id} illegal transition {before_status!r} -> {after_status!r}",
                        code="ILLEGAL_DOCUMENT_TRANSITION",
                    )
    return True


_H2R_MANDATE_VALID_ROLE_LIST = list(VALID_STD_ROLE_NAMES)
_H2R_MANDATE_PRIVILEGED_ROLE = {
    "op": "in",
    "left": {"ref": "actor.role"},
    "right": {"lit": ["hr_director", "admin"]},
}
_H2R_MANDATE_COMPLETED_APPROVAL_CARD = {
    "op": "exists",
    "object_type": "WorkCard",
    "where": {
        "op": "and",
        "args": [
            {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "COMPLETED"}},
            {"op": "eq", "left": {"ref": "target.card_kind"}, "right": {"lit": "APPROVAL"}},
            {
                "op": "in",
                "left": {"ref": "target.assignee_role"},
                "right": {"lit": ["hr_director", "admin"]},
            },
            {
                "op": "eq",
                "left": {"ref": "target.target_object_type"},
                "right": {"lit": "AuthorityMandate"},
            },
            {
                "op": "eq",
                "left": {"ref": "target.target_object_id"},
                "right": {"ref": "param.mandate_id"},
            },
            {
                "op": "eq",
                "left": {"ref": "target.requested_action"},
                "right": {"lit": "grant_authority_mandate"},
            },
        ],
    },
}

register_rule_actions(
    spec,
    [
        (
            ActionBlueprint(
                name="assign_employee_position",
                description="员工入职转正或调岗；禁止对已离职员工调岗，并校验目标部门/岗位激活且相互匹配。",
                allowed_roles=["hr_admin", "hr_director", "steward", "admin"],
                preconditions=[
                    InvariantRuleSpec(
                        name="h2r_assign_not_offboarded",
                        code="EMPLOYEE_OFFBOARDED",
                        target_type="Employee",
                        id_param="employee_id",
                        must_hold={
                            "op": "ne",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "offboarded"},
                        },
                        description="已离职（offboarded）员工禁止调岗或转正。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_assign_dept_active",
                        code="DEPARTMENT_NOT_ACTIVE",
                        target_type="Department",
                        id_param="department_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "active"},
                        },
                        description="目标部门必须处于 active 状态。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_assign_pos_active",
                        code="POSITION_NOT_ACTIVE",
                        target_type="JobPosition",
                        id_param="position_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "active"},
                        },
                        description="目标岗位必须处于 active 状态。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_assign_pos_dept_match",
                        code="POSITION_DEPARTMENT_MISMATCH",
                        target_type="JobPosition",
                        id_param="position_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.department"},
                            "right": {"ref": "param.department_id"},
                        },
                        description="目标岗位必须隶属于目标部门。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_assign_pos_valid_role",
                        code="UNKNOWN_ROLE",
                        target_type="JobPosition",
                        id_param="position_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.role_name"},
                            "right": {"lit": _H2R_MANDATE_VALID_ROLE_LIST},
                        },
                        description="目标岗位绑定角色必须属于闭集 STD_ROLES。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Employee",
                        id_param="employee_id",
                        set_fields={
                            "department": {"ref": "param.department_id"},
                            "position": {"ref": "param.position_id"},
                            "status": {
                                "op": "if_else",
                                "cond": {
                                    "op": "eq",
                                    "left": {"ref": "param.activate"},
                                    "right": {"lit": True},
                                },
                                "then": {"lit": "active"},
                                "else": {"ref": "obj.status"},
                            },
                        },
                    ),
                ],
            ),
            AssignEmployeePositionParams,
        ),
        (
            ActionBlueprint(
                name="grant_authority_mandate",
                description="授予动态权限委派：强守闭集角色校验、双边在职校验、SoD 禁自授权及上限越权拦截/升级审批。",
                allowed_roles=[
                    "hr_admin",
                    "hr_director",
                    "plant_manager",
                    "procurement_manager",
                    "sales_manager",
                    "finance_director",
                    "chief_accountant",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                preconditions=[
                    InvariantRuleSpec(
                        name="h2r_mandate_valid_role",
                        code="UNKNOWN_ROLE",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "param.role_name"},
                            "right": {"lit": _H2R_MANDATE_VALID_ROLE_LIST},
                        },
                        description="委派角色 role_name 必须属于闭集 STD_ROLES。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_positive_ceiling",
                        code="NON_POSITIVE_MANDATE_CEILING",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "gt",
                            "left": {"ref": "param.ceiling_cents"},
                            "right": {"lit": 0},
                        },
                        description="授权上限 ceiling_cents 必须严格大于 0。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_id_fresh",
                        code="MANDATE_ALREADY_EXISTS",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "not_exists",
                            "object_type": "AuthorityMandate",
                            "where": {
                                "op": "eq",
                                "left": {"ref": "target.mandate_id"},
                                "right": {"ref": "param.mandate_id"},
                            },
                        },
                        description="委派凭证 mandate_id 不可重复创建。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_no_self_grant_rule",
                        code="MANDATE_SELF_GRANT_FORBIDDEN",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "and",
                            "args": [
                                {
                                    "op": "ne",
                                    "left": {"ref": "obj.employee_id"},
                                    "right": {"ref": "param.grantee_id"},
                                },
                                {
                                    "op": "not_exists",
                                    "object_type": "Employee",
                                    "where": {
                                        "op": "and",
                                        "args": [
                                            {
                                                "op": "eq",
                                                "left": {"ref": "target.employee_id"},
                                                "right": {"ref": "param.grantee_id"},
                                            },
                                            {
                                                "op": "ne",
                                                "left": {"ref": "obj.actor_id"},
                                                "right": {"lit": ""},
                                            },
                                            {
                                                "op": "eq",
                                                "left": {"ref": "target.actor_id"},
                                                "right": {"ref": "obj.actor_id"},
                                            },
                                        ],
                                    },
                                },
                            ],
                        },
                        description="职责分离（SoD）：授权人与被授权人（含相同 actor_id）不得为同一人。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_grantor_active",
                        code="EMPLOYEE_NOT_ACTIVE",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "active"},
                        },
                        description="授权人必须处于在职（active）状态。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_grantee_active",
                        code="EMPLOYEE_NOT_ACTIVE",
                        target_type="Employee",
                        id_param="grantee_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": "active"},
                        },
                        description="被授权人必须处于在职（active）状态。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_grantor_pos_active",
                        code="POSITION_NOT_ACTIVE",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "obj.position.status"},
                            "right": {"lit": "active"},
                        },
                        description="授权人岗位必须处于 active 状态。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_grantor_pos_role_known",
                        code="UNKNOWN_ROLE",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.position.role_name"},
                            "right": {"lit": _H2R_MANDATE_VALID_ROLE_LIST},
                        },
                        description="授权人岗位角色必须属于闭集 STD_ROLES。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_no_impersonation",
                        code="MANDATE_GRANTOR_IMPERSONATION_FORBIDDEN",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "or",
                            "args": [
                                _H2R_MANDATE_PRIVILEGED_ROLE,
                                {
                                    "op": "and",
                                    "args": [
                                        {
                                            "op": "ne",
                                            "left": {"ref": "obj.actor_id"},
                                            "right": {"lit": ""},
                                        },
                                        {
                                            "op": "eq",
                                            "left": {"ref": "obj.actor_id"},
                                            "right": {"ref": "actor.actor_id"},
                                        },
                                    ],
                                },
                            ],
                        },
                        description="非高权批准者提交授权委派时必须为授权人本人身份（禁冒充他人授权）。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_admin_delegation_guard",
                        code="ADMIN_ROLE_DELEGATION_FORBIDDEN",
                        target_type="Employee",
                        id_param="grantor_id",
                        when={
                            "op": "eq",
                            "left": {"ref": "param.role_name"},
                            "right": {"lit": "admin"},
                        },
                        must_hold={
                            "op": "eq",
                            "left": {"ref": "actor.role"},
                            "right": {"lit": "admin"},
                        },
                        description="仅 admin 角色可委派 admin 权限。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_no_role_escalation",
                        code="MANDATE_ROLE_ESCALATION_FORBIDDEN",
                        target_type="Employee",
                        id_param="grantor_id",
                        must_hold={
                            "op": "or",
                            "args": [
                                _H2R_MANDATE_PRIVILEGED_ROLE,
                                {
                                    "op": "eq",
                                    "left": {"ref": "param.role_name"},
                                    "right": {"ref": "obj.position.role_name"},
                                },
                            ],
                        },
                        description="非高权批准者不得越级授予超出授权人自身岗位角色的委派权限。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_escalation_approval_guard",
                        code="APPROVAL_REQUIRED_AUTHORITY_ESCALATION",
                        target_type="Employee",
                        id_param="grantor_id",
                        when={
                            "op": "eq",
                            "left": {"ref": "param.escalate_approval"},
                            "right": {"lit": True},
                        },
                        must_hold={
                            "op": "or",
                            "args": [
                                _H2R_MANDATE_PRIVILEGED_ROLE,
                                _H2R_MANDATE_COMPLETED_APPROVAL_CARD,
                                {
                                    "op": "le",
                                    "left": {"ref": "param.ceiling_cents"},
                                    "right": {"ref": "obj.position.approval_ceiling_cents"},
                                },
                            ],
                        },
                        description="启用升级审批标志且授权上限超出授权人审批阈值时，触发 hr_director WorkCard 审批门禁。",
                    ),
                    InvariantRuleSpec(
                        name="h2r_mandate_ceiling_guard",
                        code="MANDATE_EXCEEDS_GRANTOR_CEILING",
                        target_type="Employee",
                        id_param="grantor_id",
                        when={
                            "op": "eq",
                            "left": {"ref": "param.escalate_approval"},
                            "right": {"lit": False},
                        },
                        must_hold={
                            "op": "or",
                            "args": [
                                _H2R_MANDATE_PRIVILEGED_ROLE,
                                {
                                    "op": "le",
                                    "left": {"ref": "param.ceiling_cents"},
                                    "right": {"ref": "obj.position.approval_ceiling_cents"},
                                },
                            ],
                        },
                        description="未启用升级审批时，非高权委派额度严禁超出授权人岗位审批上限。",
                    ),
                ],
                mutations=[
                    ActionMutationSpec(
                        op="create",
                        object_type="AuthorityMandate",
                        id_param="mandate_id",
                        set_fields={
                            "mandate_id": {"ref": "param.mandate_id"},
                            "grantor": {"ref": "param.grantor_id"},
                            "grantee": {"ref": "param.grantee_id"},
                            "role_name": {"ref": "param.role_name"},
                            "ceiling_cents": {"ref": "param.ceiling_cents"},
                            "scope_tag": {"ref": "param.scope_tag"},
                            "status": {"lit": "active"},
                        },
                    ),
                ],
            ),
            GrantAuthorityMandateParams,
        ),
    ],
)


@spec.function(
    "plan_reparent_department",
    ReparentDepartmentParams,
    description="M2.5 纯算大脑（Class A · 零写句柄）：执行部门 DAG 无环与同核算主体校验，输出调整上级部门的 WriteBatchProposal。",
)
def plan_reparent_department(ctx: Any, params: ReparentDepartmentParams) -> dict[str, Any]:
    dept = ctx.require(Department, params.department_id)
    if dept.status != "active":
        raise InvariantError(
            f"Department:{dept.department_id} status is {dept.status!r}, expected 'active'",
            code="DEPARTMENT_ARCHIVED",
        )
    new_pid = params.new_parent_id.strip()
    target_parent_id: str | None = None
    if new_pid:
        if new_pid == dept.department_id:
            raise InvariantError(
                f"Department:{dept.department_id} cannot reference itself as parent",
                code="DEPARTMENT_CYCLE_DETECTED",
            )
        parent = ctx.require(Department, new_pid)
        if parent.status != "active":
            raise InvariantError(
                f"Target parent Department:{parent.department_id} status is {parent.status!r}, expected 'active'",
                code="DEPARTMENT_ARCHIVED",
            )
        if parent.accounting_entity != dept.accounting_entity:
            raise InvariantError(
                f"Department:{dept.department_id} ({dept.accounting_entity}) cannot be reparented under {parent.department_id} ({parent.accounting_entity})",
                code="CROSS_ENTITY_DEPARTMENT_FORBIDDEN",
            )

        seen: set[str] = {dept.department_id}
        cur_id: str | None = parent.department_id
        while cur_id:
            if cur_id in seen:
                raise InvariantError(
                    f"Reparenting Department:{dept.department_id} under {parent.department_id} creates cycle at {cur_id!r}",
                    code="DEPARTMENT_CYCLE_DETECTED",
                )
            seen.add(cur_id)
            cur = ctx.get(Department, cur_id)
            cur_id = cur.parent_department if cur is not None else None
        target_parent_id = parent.department_id

    return {
        "contract_name": "reparent_department",
        "params": params.model_dump(mode="json"),
        "read_set": collect_read_set(ctx),
        "ops": [
            {
                "op": "modify",
                "object_type": "Department",
                "object_id": str(dept.department_id),
                "set_fields": {"parent_department": target_parent_id},
            }
        ],
        "brain_source": "pure_function:plan_reparent_department",
    }


@spec.edit_function(
    "reparent_department",
    ReparentDepartmentParams,
    description="变更部门上级归属节点；强制校验 DAG 防环（禁指向自身或任何后代）及同主体隔离。",
    touched_object_types=("Department",),
    modifies=("Department.parent_department",),
    trust_mode="untrusted_verifiable",
    require_read_set_cas=True,
    planner_function="plan_reparent_department",
    ensures=(
        {
            "name": "reparent_dept_no_self_loop",
            "code": "DEPARTMENT_CYCLE_DETECTED",
            "target_type": "Department",
            "id_param": "department_id",
            "must_hold": {
                "op": "and",
                "args": [
                    {"op": "eq", "left": {"ref": "before.status"}, "right": {"lit": "active"}},
                    {"op": "eq", "left": {"ref": "after.status"}, "right": {"lit": "active"}},
                    {
                        "op": "or",
                        "args": [
                            {"op": "eq", "left": {"ref": "after.parent_department"}, "right": {"lit": None}},
                            {
                                "op": "ne",
                                "left": {"ref": "after.parent_department"},
                                "right": {"ref": "after.department_id"},
                            },
                        ],
                    },
                ],
            },
            "description": "仅 active 部门可调整上级部门，且目标上级不得为自身。",
        },
        {
            "name": "reparent_dept_parent_valid",
            "code": "CROSS_ENTITY_DEPARTMENT_FORBIDDEN",
            "target_type": "Department",
            "id_param": "department_id",
            "when": {
                "op": "ne",
                "left": {"ref": "after.parent_department"},
                "right": {"lit": None},
            },
            "must_hold": {
                "op": "and",
                "args": [
                    {
                        "op": "eq",
                        "left": {"ref": "after.parent_department.status"},
                        "right": {"lit": "active"},
                    },
                    {
                        "op": "eq",
                        "left": {"ref": "after.parent_department.accounting_entity"},
                        "right": {"ref": "after.accounting_entity"},
                    },
                ],
            },
            "description": "若指定新上级部门，目标上级必须为 active 且隶属同一核算主体。",
        },
    ),
)
def reparent_department(ctx: Any, params: ReparentDepartmentParams) -> None:
    plan = plan_reparent_department(ctx, params)
    apply_planned_ops(ctx, plan["ops"])

spec.declare_action(
    "reparent_department",
    edit_function="reparent_department",
    allowed_roles=("hr_admin", "hr_director", "steward", "admin"),
    description="变更部门上级归属节点；强制校验 DAG 防环（禁指向自身或任何后代）及同主体隔离。",
)


@spec.function(
    "plan_offboard_employee",
    OffboardEmployeeParams,
    description="M2.5 纯算大脑（Class A · 零写句柄）：扫描员工名下全部 active 动态授权并输出离职+原子收权 WriteBatchProposal。",
)
def plan_offboard_employee(ctx: Any, params: OffboardEmployeeParams) -> dict[str, Any]:
    emp = ctx.require(Employee, params.employee_id)
    if emp.status == "offboarded":
        raise InvariantError(
            f"Employee:{emp.employee_id} is already offboarded",
            code="EMPLOYEE_ALREADY_OFFBOARDED",
        )
    if emp.status not in ("active", "suspended"):
        raise InvariantError(
            f"Employee:{emp.employee_id} status is {emp.status!r}; must be active/suspended to offboard",
            code="INVALID_EMPLOYEE_OFFBOARD_STATUS",
        )
    same_authority = (
        ctx.spec.get_object_type(Employee).is_fully_owned()
        == ctx.spec.get_object_type(AuthorityMandate).is_fully_owned()
    )
    active_mandates = sorted(
        (
            mandate
            for mandate in ctx.query(AuthorityMandate)
            if (mandate.grantee == emp.employee_id or mandate.grantor == emp.employee_id)
            and mandate.status == "active"
        ),
        key=lambda m: str(m.mandate_id),
    )
    if active_mandates and not same_authority:
        raise InvariantError(
            f"Employee:{emp.employee_id} has {len(active_mandates)} active AuthorityMandate(s) "
            "across split ownership boundary; revoke them before offboarding",
            code="OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN",
        )
    ops: list[dict[str, Any]] = [
        {
            "op": "modify",
            "object_type": "AuthorityMandate",
            "object_id": str(mandate.mandate_id),
            "set_fields": {"status": "revoked"},
        }
        for mandate in active_mandates
    ]
    ops.append(
        {
            "op": "modify",
            "object_type": "Employee",
            "object_id": str(emp.employee_id),
            "set_fields": {"status": "offboarded"},
        }
    )
    return {
        "contract_name": "offboard_employee",
        "params": params.model_dump(mode="json"),
        "read_set": collect_read_set(ctx),
        "ops": ops,
        "brain_source": "pure_function:plan_offboard_employee",
    }


@spec.edit_function(
    "offboard_employee",
    OffboardEmployeeParams,
    description="办理员工离职并在同一原子事务内自动吊销其作为授权人与被授权人的全部 active AuthorityMandate。",
    touched_object_types=("Employee", "AuthorityMandate"),
    modifies=("Employee.status", "AuthorityMandate.status"),
    trust_mode="untrusted_verifiable",
    require_read_set_cas=True,
    planner_function="plan_offboard_employee",
    ensures=(
        {
            "name": "offboard_emp_status_post",
            "code": "CONTRACT_ENSURES_VIOLATION",
            "target_type": "Employee",
            "id_param": "employee_id",
            "must_hold": {
                "op": "and",
                "args": [
                    {
                        "op": "in",
                        "left": {"ref": "before.status"},
                        "right": {"lit": ["active", "suspended"]},
                    },
                    {"op": "eq", "left": {"ref": "after.status"}, "right": {"lit": "offboarded"}},
                ],
            },
            "description": "离职员工须由 active/suspended 原子跃迁为 offboarded。",
        },
        {
            "name": "offboard_mandate_revoke_post",
            "code": "CONTRACT_ENSURES_VIOLATION",
            "target_type": "AuthorityMandate",
            "must_hold": {
                "op": "or",
                "args": [
                    {"op": "eq", "left": {"ref": "after.status"}, "right": {"ref": "before.status"}},
                    {
                        "op": "and",
                        "args": [
                            {"op": "eq", "left": {"ref": "before.status"}, "right": {"lit": "active"}},
                            {"op": "eq", "left": {"ref": "after.status"}, "right": {"lit": "revoked"}},
                            {
                                "op": "or",
                                "args": [
                                    {
                                        "op": "eq",
                                        "left": {"ref": "before.grantor"},
                                        "right": {"ref": "param.employee_id"},
                                    },
                                    {
                                        "op": "eq",
                                        "left": {"ref": "before.grantee"},
                                        "right": {"ref": "param.employee_id"},
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            "description": "受员工离职连带影响的动态授权凭证仅允许维持原状态，或针对该离职员工相关 active 凭证吊销为 revoked。",
        },
    ),
)
def offboard_employee(ctx: Any, params: OffboardEmployeeParams) -> None:
    plan = plan_offboard_employee(ctx, params)
    apply_planned_ops(ctx, plan["ops"])

spec.declare_action(
    "offboard_employee",
    edit_function="offboard_employee",
    allowed_roles=("hr_admin", "hr_director", "steward", "admin"),
    description="办理员工离职并在同一原子事务内自动吊销其作为授权人与被授权人的全部 active AuthorityMandate。",
)


@spec.function(
    "resolve_employee_effective_mandates",
    ResolveEmployeeMandatesParams,
    description="只读汇总员工归属部门链、基础岗位权限与名下有效动态授权委派。",
)
def resolve_employee_effective_mandates(
    ctx: Any, params: ResolveEmployeeMandatesParams
) -> dict[str, Any]:
    emp = ctx.require(Employee, params.employee_id)
    pos = ctx.require(JobPosition, emp.position)
    dept = ctx.require(Department, emp.department)

    chain: list[str] = []
    seen: set[str] = set()
    cur: Department | None = dept
    while cur is not None and cur.department_id not in seen:
        chain.append(cur.department_id)
        seen.add(cur.department_id)
        cur = ctx.get(Department, cur.parent_department) if cur.parent_department else None

    active_mandates = [
        {
            "mandate_id": m.mandate_id,
            "grantor": m.grantor,
            "role_name": m.role_name,
            "ceiling_cents": m.ceiling_cents,
            "scope_tag": m.scope_tag,
        }
        for m in sorted(ctx.query(AuthorityMandate), key=lambda x: x.mandate_id)
        if m.grantee == emp.employee_id and m.status == "active"
    ]
    effective_ceilings: dict[str, int] = {}
    if emp.status == "active" and pos.status == "active":
        effective_ceilings[pos.role_name] = pos.approval_ceiling_cents
        for m in active_mandates:
            r = m["role_name"]
            effective_ceilings[r] = max(effective_ceilings.get(r, 0), int(m["ceiling_cents"]))

    return {
        "employee_id": emp.employee_id,
        "actor_id": emp.actor_id,
        "status": emp.status,
        "department_id": dept.department_id,
        "department_ancestry": chain,
        "position_id": pos.position_id,
        "base_role_name": pos.role_name,
        "base_ceiling_cents": pos.approval_ceiling_cents,
        "active_mandates": active_mandates,
        "effective_ceilings_by_role": effective_ceilings,
    }


spec.update_display(
    {
        "ontologies": {
            "std_h2r_ontology": "标准 H2R 领域本体（部门树·岗位定编·员工档案·动态授权委派）",
        },
        "objects": {
            "Department": "部门组织",
            "JobPosition": "岗位定编",
            "Employee": "员工档案",
            "AuthorityMandate": "动态授权委派凭证",
        },
        "actions": {
            "reparent_department": "调整上级部门",
            "assign_employee_position": "员工入职与调岗",
            "grant_authority_mandate": "授予动态权限委派",
            "offboard_employee": "员工离职与自动收权",
        },
        "functions": {
            "resolve_employee_effective_mandates": "解析员工有效权限委派",
            "plan_reparent_department": "M2.5 纯算大脑 · 部门上级调整提案",
            "plan_offboard_employee": "M2.5 纯算大脑 · 员工离职与原子收权提案",
        },
        "fields": {
            "department_id": "部门编码",
            "accounting_entity": "核算主体",
            "name": "名称",
            "parent_department": "上级部门",
            "scope_tag": "权限范围标签",
            "position_id": "岗位编码",
            "department": "所属部门",
            "title": "标题",
            "role_name": "标准系统角色",
            "approval_ceiling_cents": "审批金额上限（分）",
            "employee_id": "员工编码",
            "party": "往来伙伴",
            "position": "所属岗位",
            "full_name": "员工姓名",
            "actor_id": "系统执行身份标识",
            "mandate_id": "授权凭证编码",
            "grantor": "授权人",
            "grantee": "被授权人",
            "ceiling_cents": "委派额度上限（分）",
            "status": "状态",
        },
    }
)


def build_std_h2r_blueprint() -> DeclarativeBlueprint:
    """将 ``std_h2r_ontology`` 唯一源码本体投影为标准 H2R 领域声明式蓝图。"""
    return export_spec_blueprint(
        spec,
        model_id="std_h2r_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        imports=("std_core_ontology",),
        description="标准 H2R 领域本体蓝图：在核心底座上组合部门组织、岗位定编、员工档案与动态授权委派。",
    )


enforce_edit_function_contracts(spec)
