"""软件研发工厂本体（SoftwareFactoryOntology）—— 100% 业务纯洁层（PIM）。

本模块只描述「研发管理」这件事本身：

* 研发成员 ``Engineer``（架构师 / 程序员 / 测试员 / 审计官）
* 研发工单 ``WorkTicket``（立项 → 派单 → 交卷 → 评审 → 返工 / 完结）
* 交付物 ``Deliverable``（每一轮的产物与验证状态）

以及派单时的**并发目录隔离锁**、评审打回时的**返工熔断**、
失联换人时的**无损交接**。

这里不出现任何执行宿主、会话、端口、传输协议或限流细节 ——
那些都属于外围机械臂适配层（见 ``harness_drivers``）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .spec import Actor, InvariantError, LinkType, ObjectType, OntologySpec

__all__ = [
    "Engineer",
    "WorkTicket",
    "Deliverable",
    "TICKET_ASSIGNED_TO",
    "TICKET_REVIEWED_BY",
    "TICKET_DELIVERABLES",
    "spec",
    "ENGINEER_ROLES",
    "TICKET_STAGES",
    "FACTORY_MANAGER",
    "render_briefing",
]


# ---------------------------------------------------------------------------
# 纯业务对象
# ---------------------------------------------------------------------------
class Engineer(ObjectType):
    """研发成员。"""

    __primary_key__ = "engineer_id"
    __title__ = "name"
    __owned__ = True

    engineer_id: str
    name: str
    role: str  # architect | coder | tester | auditor
    specialty: str = "general"
    availability: str = "IDLE"  # IDLE | BUSY | UNAVAILABLE
    active_ticket_id: str | None = None
    completed_tickets: int = 0
    Security_Classification: str = "INTERNAL"


class WorkTicket(ObjectType):
    """研发工单。"""

    __primary_key__ = "ticket_id"
    __title__ = "title"
    __owned__ = True

    ticket_id: str
    title: str
    spec_uri: str
    isolated_scope: str
    stage: str = "OPEN"  # OPEN | CODING | TESTING | ARCH_REVIEW | DONE | BLOCKED
    require_testing: bool = False
    assignee_id: str | None = None
    reviewer_id: str | None = None
    rework_round: int = 0
    max_rework: int = 5
    instructions: str = ""
    latest_submission: str | None = None
    latest_feedback: str | None = None
    verdict: str = "PENDING"  # PENDING | PASS | FAIL
    Security_Classification: str = "INTERNAL"


class Deliverable(ObjectType):
    """工单交付物。"""

    __primary_key__ = "deliverable_id"
    __title__ = "summary"
    __owned__ = True

    deliverable_id: str
    ticket_id: str
    engineer_id: str
    round_index: int
    artifact_uri: str
    summary: str
    verified: bool = False
    Security_Classification: str = "INTERNAL"


# ---------------------------------------------------------------------------
# 纯业务关系
# ---------------------------------------------------------------------------
TICKET_ASSIGNED_TO = LinkType(
    name="ticket_assigned_to",
    source="WorkTicket",
    target="Engineer",
    cardinality="many-to-one",
    owned=True,
    description="工单当前承接人",
)
TICKET_REVIEWED_BY = LinkType(
    name="ticket_reviewed_by",
    source="WorkTicket",
    target="Engineer",
    cardinality="many-to-one",
    owned=True,
    description="工单最近一次评审人",
)
TICKET_DELIVERABLES = LinkType(
    name="ticket_deliverables",
    source="WorkTicket",
    target="Deliverable",
    cardinality="one-to-many",
    owned=True,
    description="工单历次交付物",
)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (Engineer, WorkTicket, Deliverable):
    spec.object_type(_cls)
for _link in (TICKET_ASSIGNED_TO, TICKET_REVIEWED_BY, TICKET_DELIVERABLES):
    spec.add_link_type(_link)


ENGINEER_ROLES: tuple[str, ...] = ("architect", "coder", "tester", "auditor")
TICKET_STAGES: tuple[str, ...] = (
    "OPEN",
    "CODING",
    "TESTING",
    "ARCH_REVIEW",
    "DONE",
    "BLOCKED",
)

FACTORY_MANAGER = Actor(
    actor_id="factory-manager-01",
    role="factory_manager",
    clearance_tags={"*"},
)

_ALLOWED_ROLES = ("factory_manager", "admin", "system")


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------
class RecruitEngineerParams(BaseModel):
    engineer_id: str
    name: str
    role: str
    specialty: str = "general"


class OpenTicketParams(BaseModel):
    ticket_id: str
    title: str
    spec_uri: str
    isolated_scope: str
    instructions: str = ""
    max_rework: int = 5
    require_testing: bool = False


class DispatchTicketParams(BaseModel):
    ticket_id: str
    engineer_id: str
    instructions_override: str | None = None


class SubmitWorkParams(BaseModel):
    ticket_id: str
    engineer_id: str
    summary: str
    artifact_uri: str = ""


class ReviewTicketParams(BaseModel):
    ticket_id: str
    reviewer_id: str
    verdict: str
    feedback: str = ""


class ReassignTicketParams(BaseModel):
    ticket_id: str
    new_engineer_id: str
    reason: str = ""


class BoardSummaryParams(BaseModel):
    pass


class EngineerBriefingParams(BaseModel):
    ticket_id: str


# ---------------------------------------------------------------------------
# 纯业务辅助
# ---------------------------------------------------------------------------
def _normalize_scope(scope: str) -> str:
    """把目录范围归一化为 ``a/b`` 形式；空串代表仓库根。"""
    text = (scope or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    parts = [part for part in text.split("/") if part and part != "."]
    return "/".join(parts)


def _scopes_overlap(left: str, right: str) -> bool:
    """同一目录、或父子目录互相包含，都视为写冲突。"""
    a = _normalize_scope(left)
    b = _normalize_scope(right)
    if not a or not b:
        return True
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def _require_ticket(ctx: Any, ticket_id: str) -> WorkTicket:
    ticket = ctx.get(WorkTicket, ticket_id)
    if ticket is None:
        raise InvariantError(
            f"工单 {ticket_id!r} 不存在", code="TICKET_NOT_FOUND"
        )
    return ticket


def _require_engineer(ctx: Any, engineer_id: str) -> Engineer:
    engineer = ctx.get(Engineer, engineer_id)
    if engineer is None:
        raise InvariantError(
            f"研发成员 {engineer_id!r} 不存在", code="ENGINEER_NOT_FOUND"
        )
    return engineer


def _latest_deliverable(ctx: Any, ticket: WorkTicket) -> Deliverable | None:
    rows = [
        row
        for row in ctx.query(Deliverable)
        if row.ticket_id == ticket.ticket_id
    ]
    if not rows:
        return None
    rows.sort(key=lambda row: (row.round_index, row.deliverable_id))
    return rows[-1]


# ---------------------------------------------------------------------------
# 动作：招募 / 立项
# ---------------------------------------------------------------------------
@spec.action(
    "recruit_engineer",
    RecruitEngineerParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="招募一名研发成员（架构师 / 程序员 / 测试员 / 审计官）。",
)
def recruit_engineer(ctx: Any, params: RecruitEngineerParams) -> None:
    if params.role not in ENGINEER_ROLES:
        raise InvariantError(
            f"非法角色 {params.role!r}，只允许 {list(ENGINEER_ROLES)}",
            code="INVALID_ENGINEER_ROLE",
        )
    if ctx.get(Engineer, params.engineer_id) is not None:
        raise InvariantError(
            f"研发成员 {params.engineer_id!r} 已存在",
            code="ENGINEER_ALREADY_EXISTS",
        )
    ctx.create(
        Engineer(
            engineer_id=params.engineer_id,
            name=params.name,
            role=params.role,
            specialty=params.specialty,
        )
    )


@spec.action(
    "open_ticket",
    OpenTicketParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="立项：创建一张处于 OPEN 状态的研发工单。",
)
def open_ticket(ctx: Any, params: OpenTicketParams) -> None:
    if params.max_rework < 1:
        raise InvariantError(
            f"max_rework 必须 >= 1，收到 {params.max_rework}",
            code="INVALID_MAX_REWORK",
        )
    if not _normalize_scope(params.isolated_scope):
        raise InvariantError(
            "isolated_scope 不能为空",
            code="EMPTY_ISOLATED_SCOPE",
        )
    if ctx.get(WorkTicket, params.ticket_id) is not None:
        raise InvariantError(
            f"工单 {params.ticket_id!r} 已存在",
            code="TICKET_ALREADY_EXISTS",
        )
    ctx.create(
        WorkTicket(
            ticket_id=params.ticket_id,
            title=params.title,
            spec_uri=params.spec_uri,
            isolated_scope=params.isolated_scope.strip(),
            stage="OPEN",
            require_testing=params.require_testing,
            max_rework=params.max_rework,
            instructions=params.instructions,
        )
    )


# ---------------------------------------------------------------------------
# 动作：派单（含并发目录隔离锁）
# ---------------------------------------------------------------------------
@spec.action(
    "dispatch_ticket",
    DispatchTicketParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="把 OPEN 工单派给空闲程序员；同一隔离目录不允许并发在途。",
)
def dispatch_ticket(ctx: Any, params: DispatchTicketParams) -> None:
    ticket = _require_ticket(ctx, params.ticket_id)
    if ticket.stage != "OPEN":
        raise InvariantError(
            f"工单 {ticket.ticket_id} 处于 {ticket.stage}，只有 OPEN 可以派单",
            code="INVALID_DISPATCH_STATE",
        )

    engineer = _require_engineer(ctx, params.engineer_id)
    if engineer.role != "coder":
        raise InvariantError(
            f"{engineer.engineer_id} 的角色是 {engineer.role}，派单对象必须是 coder",
            code="ASSIGNEE_MUST_BE_CODER",
        )
    if engineer.availability != "IDLE":
        raise InvariantError(
            f"{engineer.engineer_id} 当前 {engineer.availability}，无法接单",
            code="ENGINEER_NOT_IDLE",
        )

    # 并发目录锁：任何在途工单与本工单目录相同或父子重叠，立即拒绝。
    for other in ctx.query(WorkTicket):
        if other.ticket_id == ticket.ticket_id or other.stage != "CODING":
            continue
        if _scopes_overlap(other.isolated_scope, ticket.isolated_scope):
            raise InvariantError(
                f"目录冲突：{other.ticket_id} 正在 {other.isolated_scope} 施工，"
                f"与本工单 {ticket.isolated_scope} 重叠",
                code="WORKSPACE_SCOPE_CONFLICT",
            )

    instructions = ticket.instructions
    if params.instructions_override is not None:
        instructions = params.instructions_override

    ctx.modify(
        ticket,
        stage="CODING",
        assignee_id=engineer.engineer_id,
        instructions=instructions,
    )
    ctx.modify(
        engineer,
        availability="BUSY",
        active_ticket_id=ticket.ticket_id,
    )
    ctx.link(ticket, TICKET_ASSIGNED_TO, engineer)


# ---------------------------------------------------------------------------
# 动作：交卷
# ---------------------------------------------------------------------------
@spec.action(
    "submit_work",
    SubmitWorkParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="程序员交卷：生成交付物、释放程序员、推进到测试或架构评审。",
)
def submit_work(ctx: Any, params: SubmitWorkParams) -> None:
    ticket = _require_ticket(ctx, params.ticket_id)
    if ticket.stage != "CODING" or ticket.assignee_id != params.engineer_id:
        raise InvariantError(
            f"工单 {ticket.ticket_id} 处于 {ticket.stage}，"
            f"承接人是 {ticket.assignee_id}，{params.engineer_id} 无法交卷",
            code="INVALID_SUBMISSION_STATE",
        )
    engineer = _require_engineer(ctx, params.engineer_id)

    round_rows = [
        row for row in ctx.query(Deliverable) if row.ticket_id == ticket.ticket_id
    ]
    deliverable = Deliverable(
        deliverable_id=f"{ticket.ticket_id}-D{len(round_rows) + 1}",
        ticket_id=ticket.ticket_id,
        engineer_id=engineer.engineer_id,
        round_index=ticket.rework_round,
        artifact_uri=params.artifact_uri,
        summary=params.summary,
        verified=False,
    )
    ctx.create(deliverable)
    ctx.link(ticket, TICKET_DELIVERABLES, deliverable)

    ctx.modify(engineer, availability="IDLE", active_ticket_id=None)
    next_stage = "TESTING" if ticket.require_testing else "ARCH_REVIEW"
    ctx.modify(
        ticket,
        stage=next_stage,
        latest_submission=params.summary,
        verdict="PENDING",
    )


# ---------------------------------------------------------------------------
# 动作：评审（测试 / 架构终审 / 打回返工 / 熔断）
# ---------------------------------------------------------------------------
@spec.action(
    "review_ticket",
    ReviewTicketParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="测试员或架构师裁决；FAIL 自动返工，超过 max_rework 熔断。",
)
def review_ticket(ctx: Any, params: ReviewTicketParams) -> None:
    ticket = _require_ticket(ctx, params.ticket_id)
    if params.verdict not in ("PASS", "FAIL"):
        raise InvariantError(
            f"非法裁决 {params.verdict!r}，只允许 PASS / FAIL",
            code="INVALID_VERDICT",
        )
    if ticket.stage not in ("TESTING", "ARCH_REVIEW"):
        raise InvariantError(
            f"工单 {ticket.ticket_id} 处于 {ticket.stage}，当前不可评审",
            code="INVALID_REVIEW_STATE",
        )

    reviewer = _require_engineer(ctx, params.reviewer_id)
    if ticket.stage == "TESTING":
        allowed = ("tester", "architect")
    else:
        allowed = ("architect", "auditor")
    if reviewer.role not in allowed:
        raise InvariantError(
            f"{reviewer.engineer_id} 的角色 {reviewer.role} 不能评审 {ticket.stage}",
            code="REVIEWER_ROLE_NOT_ALLOWED",
        )

    ctx.modify(ticket, reviewer_id=reviewer.engineer_id)
    ctx.link(ticket, TICKET_REVIEWED_BY, reviewer)

    if params.verdict == "PASS":
        if ticket.stage == "TESTING":
            ctx.modify(
                ticket,
                stage="ARCH_REVIEW",
                verdict="PENDING",
                latest_feedback=params.feedback,
            )
            return

        deliverable = _latest_deliverable(ctx, ticket)
        if deliverable is not None:
            ctx.modify(deliverable, verified=True)
        ctx.modify(
            ticket,
            stage="DONE",
            verdict="PASS",
            latest_feedback=params.feedback,
        )
        if ticket.assignee_id:
            engineer = ctx.get(Engineer, ticket.assignee_id)
            if engineer is not None:
                ctx.modify(
                    engineer,
                    completed_tickets=engineer.completed_tickets + 1,
                )
        return

    # ---- FAIL：返工或熔断 ------------------------------------------------
    if ticket.rework_round + 1 > ticket.max_rework:
        ctx.modify(
            ticket,
            stage="BLOCKED",
            verdict="FAIL",
            latest_feedback=params.feedback,
        )
        return

    ctx.modify(
        ticket,
        rework_round=ticket.rework_round + 1,
        verdict="FAIL",
        latest_feedback=params.feedback,
    )
    assignee = ctx.get(Engineer, ticket.assignee_id) if ticket.assignee_id else None
    if assignee is not None and assignee.availability == "IDLE":
        ctx.modify(
            ticket,
            stage="CODING",
        )
        ctx.modify(
            assignee,
            availability="BUSY",
            active_ticket_id=ticket.ticket_id,
        )
    else:
        ctx.modify(
            ticket,
            stage="OPEN",
            assignee_id=None,
        )


# ---------------------------------------------------------------------------
# 动作：无损换人
# ---------------------------------------------------------------------------
@spec.action(
    "reassign_ticket",
    ReassignTicketParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="原承接人失联时，把工单连同全部历史上下文原子交接给空闲程序员。",
)
def reassign_ticket(ctx: Any, params: ReassignTicketParams) -> None:
    ticket = _require_ticket(ctx, params.ticket_id)
    new_engineer = _require_engineer(ctx, params.new_engineer_id)

    if ticket.assignee_id == new_engineer.engineer_id:
        raise InvariantError(
            f"{new_engineer.engineer_id} 已经是工单承接人",
            code="REASSIGN_SAME_ENGINEER",
        )
    if new_engineer.role != "coder":
        raise InvariantError(
            f"{new_engineer.engineer_id} 的角色是 {new_engineer.role}，承接人必须是 coder",
            code="ASSIGNEE_MUST_BE_CODER",
        )
    if new_engineer.availability != "IDLE":
        raise InvariantError(
            f"{new_engineer.engineer_id} 当前 {new_engineer.availability}，无法接手",
            code="ENGINEER_NOT_IDLE",
        )

    previous = (
        ctx.get(Engineer, ticket.assignee_id) if ticket.assignee_id else None
    )
    if previous is not None:
        ctx.modify(previous, availability="UNAVAILABLE", active_ticket_id=None)
        ctx.unlink(ticket, TICKET_ASSIGNED_TO, previous)

    ctx.modify(
        new_engineer,
        availability="BUSY",
        active_ticket_id=ticket.ticket_id,
    )
    ctx.modify(
        ticket,
        assignee_id=new_engineer.engineer_id,
        stage="CODING",
    )
    ctx.link(ticket, TICKET_ASSIGNED_TO, new_engineer)


# ---------------------------------------------------------------------------
# 只读看板与任务简报
# ---------------------------------------------------------------------------
@spec.function(
    "factory_board_summary",
    BoardSummaryParams,
    description="研发工厂看板：成员忙闲、各阶段工单统计、在途目录锁。",
)
def factory_board_summary(ctx: Any, params: BoardSummaryParams) -> dict[str, Any]:
    engineers = ctx.query(Engineer)
    tickets = ctx.query(WorkTicket)
    deliverables = ctx.query(Deliverable)

    by_availability = {state: 0 for state in ("IDLE", "BUSY", "UNAVAILABLE")}
    by_role = {role: 0 for role in ENGINEER_ROLES}
    for engineer in engineers:
        by_availability[engineer.availability] = (
            by_availability.get(engineer.availability, 0) + 1
        )
        by_role[engineer.role] = by_role.get(engineer.role, 0) + 1

    by_stage = {stage: 0 for stage in TICKET_STAGES}
    for ticket in tickets:
        by_stage[ticket.stage] = by_stage.get(ticket.stage, 0) + 1

    locks = [
        {
            "ticket_id": ticket.ticket_id,
            "scope": _normalize_scope(ticket.isolated_scope),
            "engineer_id": ticket.assignee_id,
        }
        for ticket in sorted(tickets, key=lambda row: row.ticket_id)
        if ticket.stage == "CODING"
    ]

    return {
        "engineers": {
            "total": len(engineers),
            "by_role": by_role,
            "by_availability": by_availability,
            "busy": sorted(
                row.engineer_id for row in engineers if row.availability == "BUSY"
            ),
            "idle": sorted(
                row.engineer_id for row in engineers if row.availability == "IDLE"
            ),
            "unavailable": sorted(
                row.engineer_id
                for row in engineers
                if row.availability == "UNAVAILABLE"
            ),
        },
        "tickets": {
            "total": len(tickets),
            "by_stage": by_stage,
            "in_rework": sorted(
                row.ticket_id for row in tickets if row.rework_round > 0
            ),
            "blocked": sorted(
                row.ticket_id for row in tickets if row.stage == "BLOCKED"
            ),
        },
        "deliverables": {
            "total": len(deliverables),
            "verified": sum(1 for row in deliverables if row.verified),
        },
        "active_workspace_locks": locks,
    }


@spec.function(
    "build_engineer_briefing",
    EngineerBriefingParams,
    description="纯粹从工单与交付物历史拼装任务简报，不依赖任何外部会话记忆。",
)
def build_engineer_briefing(
    ctx: Any, params: EngineerBriefingParams
) -> dict[str, Any]:
    ticket = _require_ticket(ctx, params.ticket_id)
    assignee = (
        ctx.get(Engineer, ticket.assignee_id) if ticket.assignee_id else None
    )
    reviewer = (
        ctx.get(Engineer, ticket.reviewer_id) if ticket.reviewer_id else None
    )

    history = [
        {
            "deliverable_id": row.deliverable_id,
            "engineer_id": row.engineer_id,
            "round_index": row.round_index,
            "artifact_uri": row.artifact_uri,
            "summary": row.summary,
            "verified": row.verified,
        }
        for row in sorted(
            (row for row in ctx.query(Deliverable) if row.ticket_id == ticket.ticket_id),
            key=lambda row: (row.round_index, row.deliverable_id),
        )
    ]

    return {
        "ticket_id": ticket.ticket_id,
        "title": ticket.title,
        "spec_uri": ticket.spec_uri,
        "isolated_scope": ticket.isolated_scope,
        "normalized_scope": _normalize_scope(ticket.isolated_scope),
        "stage": ticket.stage,
        "instructions": ticket.instructions,
        "assignee_id": ticket.assignee_id,
        "assignee_name": assignee.name if assignee is not None else None,
        "reviewer_id": ticket.reviewer_id,
        "reviewer_name": reviewer.name if reviewer is not None else None,
        "rework_round": ticket.rework_round,
        "max_rework": ticket.max_rework,
        "require_testing": ticket.require_testing,
        "verdict": ticket.verdict,
        "previous_submission": ticket.latest_submission,
        "review_feedback": ticket.latest_feedback,
        "history": history,
    }


def render_briefing(briefing: dict[str, Any]) -> str:
    """把结构化简报渲染成纯文本，供任意执行机械臂直接消费。"""
    lines = [
        f"工单：{briefing['ticket_id']} · {briefing['title']}",
        f"规格书：{briefing['spec_uri']}",
        f"隔离目录：{briefing['isolated_scope']}",
        f"当前阶段：{briefing['stage']}（返工 {briefing['rework_round']}/"
        f"{briefing['max_rework']}）",
        f"承接人：{briefing.get('assignee_name') or briefing.get('assignee_id')}",
    ]
    if briefing.get("instructions"):
        lines.append(f"任务说明：{briefing['instructions']}")
    if briefing.get("previous_submission"):
        lines.append(f"上一轮交卷：{briefing['previous_submission']}")
    if briefing.get("review_feedback"):
        lines.append(f"评审意见：{briefing['review_feedback']}")
    if briefing.get("history"):
        lines.append("交付物历史：")
        for row in briefing["history"]:
            mark = "已验收" if row["verified"] else "待验收"
            lines.append(
                f"  - 第 {row['round_index']} 轮 {row['engineer_id']} "
                f"[{mark}] {row['summary']}"
            )
    return "\n".join(lines)
