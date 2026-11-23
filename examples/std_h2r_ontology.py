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

from open_ontology.induction.dsl import DeclarativeBlueprint, export_spec_blueprint
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        STD_ROLES,
        AccountingEntity,
        BusinessParty,
        WorkCard,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import MasterEntity, ProcessDocument
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


def _has_completed_mandate_escalation_card(ctx: Any, mandate_id: str) -> bool:
    for card in ctx.query(WorkCard):
        if (
            card.status == "COMPLETED"
            and card.card_kind == "APPROVAL"
            and card.assignee_role in ("hr_director", "admin")
            and card.target_object_type == "AuthorityMandate"
            and card.target_object_id == mandate_id
            and card.requested_action == "grant_authority_mandate"
        ):
            return True
    return False


@spec.edit_function(
    "reparent_department",
    ReparentDepartmentParams,
    description="变更部门上级归属节点；强制校验 DAG 防环（禁指向自身或任何后代）及同主体隔离。",
    touched_object_types=("Department",),
)
def reparent_department(ctx: Any, params: ReparentDepartmentParams) -> None:
    dept = ctx.require(Department, params.department_id)
    if dept.status != "active":
        raise InvariantError(
            f"Department:{dept.department_id} status is {dept.status!r}, expected 'active'",
            code="DEPARTMENT_ARCHIVED",
        )
    new_pid = params.new_parent_id.strip()
    if not new_pid:
        ctx.modify(dept, parent_department=None)
        return
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

    ctx.modify(dept, parent_department=parent.department_id)

spec.declare_action(
    "reparent_department",
    edit_function="reparent_department",
    allowed_roles=("hr_admin", "hr_director", "steward", "admin"),
    description="变更部门上级归属节点；强制校验 DAG 防环（禁指向自身或任何后代）及同主体隔离。",
)


@spec.edit_function(
    "assign_employee_position",
    AssignEmployeePositionParams,
    description="员工入职转正或调岗；禁止对已离职员工调岗，并校验目标部门/岗位激活且相互匹配。",
    touched_object_types=("Employee",),
)
def assign_employee_position(ctx: Any, params: AssignEmployeePositionParams) -> None:
    emp = ctx.require(Employee, params.employee_id)
    if emp.status == "offboarded":
        raise InvariantError(
            f"Employee:{emp.employee_id} is offboarded and cannot be assigned",
            code="EMPLOYEE_OFFBOARDED",
        )
    dept = ctx.require(Department, params.department_id)
    if dept.status != "active":
        raise InvariantError(
            f"Department:{dept.department_id} status is {dept.status!r}, expected 'active'",
            code="DEPARTMENT_NOT_ACTIVE",
        )
    pos = ctx.require(JobPosition, params.position_id)
    if pos.status != "active":
        raise InvariantError(
            f"JobPosition:{pos.position_id} status is {pos.status!r}, expected 'active'",
            code="POSITION_NOT_ACTIVE",
        )
    if pos.department != dept.department_id:
        raise InvariantError(
            f"JobPosition:{pos.position_id} belongs to Department:{pos.department}, not {dept.department_id}",
            code="POSITION_DEPARTMENT_MISMATCH",
        )
    if pos.role_name not in VALID_STD_ROLE_NAMES:
        raise InvariantError(
            f"JobPosition:{pos.position_id}.role_name={pos.role_name!r} is not in STD_ROLES",
            code="UNKNOWN_ROLE",
        )

    next_status = "active" if params.activate else emp.status
    ctx.modify(
        emp,
        department=dept.department_id,
        position=pos.position_id,
        status=next_status,
    )

spec.declare_action(
    "assign_employee_position",
    edit_function="assign_employee_position",
    allowed_roles=("hr_admin", "hr_director", "steward", "admin"),
    description="员工入职转正或调岗；禁止对已离职员工调岗，并校验目标部门/岗位激活且相互匹配。",
)


@spec.edit_function(
    "grant_authority_mandate",
    GrantAuthorityMandateParams,
    description="授予动态权限委派：强守闭集角色校验、双边在职校验、SoD 禁自授权及上限越权拦截/升级审批。",
    touched_object_types=("AuthorityMandate",),
)
def grant_authority_mandate(ctx: Any, params: GrantAuthorityMandateParams) -> None:
    if params.role_name not in VALID_STD_ROLE_NAMES:
        raise InvariantError(
            f"role_name {params.role_name!r} is not in closed STD_ROLES",
            code="UNKNOWN_ROLE",
        )
    if params.ceiling_cents <= 0:
        raise InvariantError("ceiling_cents must be > 0", code="NON_POSITIVE_MANDATE_CEILING")
    if ctx.get(AuthorityMandate, params.mandate_id) is not None:
        raise InvariantError(
            f"AuthorityMandate:{params.mandate_id} already exists",
            code="MANDATE_ALREADY_EXISTS",
        )

    grantor = ctx.require(Employee, params.grantor_id)
    grantee = ctx.require(Employee, params.grantee_id)
    if grantor.employee_id == grantee.employee_id or (
        grantor.actor_id and grantor.actor_id == grantee.actor_id
    ):
        raise InvariantError(
            f"SoD violation: Employee:{grantor.employee_id} cannot grant mandate to self",
            code="MANDATE_SELF_GRANT_FORBIDDEN",
        )
    if grantor.status != "active" or grantee.status != "active":
        raise InvariantError(
            f"Both grantor ({grantor.status!r}) and grantee ({grantee.status!r}) must be 'active'",
            code="EMPLOYEE_NOT_ACTIVE",
        )

    grantor_pos = ctx.require(JobPosition, grantor.position)
    if grantor_pos.status != "active":
        raise InvariantError(
            f"Grantor JobPosition:{grantor_pos.position_id} is {grantor_pos.status!r}",
            code="POSITION_NOT_ACTIVE",
        )
    if grantor_pos.role_name not in VALID_STD_ROLE_NAMES:
        raise InvariantError(
            f"Grantor JobPosition:{grantor_pos.position_id}.role_name={grantor_pos.role_name!r} is unknown",
            code="UNKNOWN_ROLE",
        )

    is_privileged = ctx.actor.role in ("hr_director", "admin") or _has_completed_mandate_escalation_card(
        ctx, params.mandate_id
    )
    if not is_privileged and (not grantor.actor_id or grantor.actor_id != ctx.actor.actor_id):
        raise InvariantError(
            f"Actor {ctx.actor.actor_id!r} ({ctx.actor.role!r}) cannot impersonate grantor Employee:{grantor.employee_id} (actor_id={grantor.actor_id!r})",
            code="MANDATE_GRANTOR_IMPERSONATION_FORBIDDEN",
        )
    if params.role_name == "admin" and ctx.actor.role != "admin":
        raise InvariantError(
            "Only 'admin' may grant an 'admin' AuthorityMandate",
            code="ADMIN_ROLE_DELEGATION_FORBIDDEN",
        )
    if params.role_name != grantor_pos.role_name and not is_privileged:
        raise InvariantError(
            f"Grantor Employee:{grantor.employee_id} role {grantor_pos.role_name!r} cannot escalate mandate to {params.role_name!r}",
            code="MANDATE_ROLE_ESCALATION_FORBIDDEN",
        )
    if params.ceiling_cents > grantor_pos.approval_ceiling_cents and not is_privileged:
        if params.escalate_approval:
            raise InvariantError(
                f"Mandate ceiling_cents={params.ceiling_cents} exceeds grantor ceiling {grantor_pos.approval_ceiling_cents}; requires hr_director approval",
                code="APPROVAL_REQUIRED_AUTHORITY_ESCALATION",
            )
        raise InvariantError(
            f"Mandate ceiling_cents={params.ceiling_cents} exceeds grantor approval_ceiling_cents={grantor_pos.approval_ceiling_cents}",
            code="MANDATE_EXCEEDS_GRANTOR_CEILING",
        )

    ctx.create(
        AuthorityMandate(
            mandate_id=params.mandate_id,
            grantor=grantor.employee_id,
            grantee=grantee.employee_id,
            role_name=params.role_name,
            ceiling_cents=params.ceiling_cents,
            scope_tag=params.scope_tag,
            status="active",
        )
    )

spec.declare_action(
    "grant_authority_mandate",
    edit_function="grant_authority_mandate",
    allowed_roles=(
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
    ),
    description="授予动态权限委派：强守闭集角色校验、双边在职校验、SoD 禁自授权及上限越权拦截/升级审批。",
)


@spec.edit_function(
    "offboard_employee",
    OffboardEmployeeParams,
    description="办理员工离职并在同一原子事务内自动吊销其作为授权人与被授权人的全部 active AuthorityMandate。",
    touched_object_types=("Employee", "AuthorityMandate"),
)
def offboard_employee(ctx: Any, params: OffboardEmployeeParams) -> None:
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
    active_mandates = [
        mandate
        for mandate in ctx.query(AuthorityMandate)
        if (mandate.grantee == emp.employee_id or mandate.grantor == emp.employee_id)
        and mandate.status == "active"
    ]
    if active_mandates and not same_authority:
        raise InvariantError(
            f"Employee:{emp.employee_id} has {len(active_mandates)} active AuthorityMandate(s) "
            "across split ownership boundary; revoke them before offboarding",
            code="OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN",
        )
    for mandate in active_mandates:
        ctx.modify(mandate, status="revoked")
    ctx.modify(emp, status="offboarded")

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
