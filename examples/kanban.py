"""任务看板领域本体（TaskBoardOntology）—— 100% 业务纯洁层（PIM）。

本模块仅描述「任务看板」这一特定业务领域本身，不包含自举研发规程、
外部执行宿主或调度编排逻辑：

* 看板 ``Board``（聚合根与租户命名空间）
* 成员 ``Member``（角色与 WIP 在办上限）
* 卡片 ``Card``（固定 6 态生命周期、前置依赖 DAG 与失败/阻塞计数）
* 评论 ``CardComment``

世界模型只负责守住确定性不变量（无环依赖、前置完成门控、WIP 上限、
Actor 与 Member 身份绑定、禁止绕行进入 ``done``）；“谁在何时领哪张卡”
由上游 Agent 通过只读函数（如 ``eligible_cards``）自行决策后调用 ``ActionDef``。
"""

from __future__ import annotations

import time
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, Field

from open_ontology.spec import (
    Actor,
    AutoID,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)

__all__ = [
    "Board",
    "Member",
    "Card",
    "CardComment",
    "spec",
    "CARD_STATUSES",
    "CardStatus",
    "MEMBER_ROLES",
    "OUTCOMES",
    "BLOCK_KINDS",
    "BOARD_MANAGER",
    "render_card_briefing",
]


# ---------------------------------------------------------------------------
# 固定 6 态流定义
# ---------------------------------------------------------------------------
CARD_STATUSES: tuple[str, ...] = (
    "todo",
    "ready",
    "doing",
    "done",
    "blocked",
    "triage",
)
CardStatus = Literal["todo", "ready", "doing", "done", "blocked", "triage"]
MemberRole = Literal["developer", "reviewer", "tester", "coordinator"]
OutcomeType = Literal["completed", "blocked", "failed", "abandoned"]
BlockKind = Literal[
    "dependency",
    "awaiting_review",
    "external_dependency",
    "insufficient_input",
]


class _StatusSpec(NamedTuple):
    key: str
    label: str
    purpose: str
    order: int
    is_terminal: bool = False
    claimable: bool = False
    allowed_roles: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    max_retries: int = 3


STATUS_SPECS: dict[str, _StatusSpec] = {
    "todo": _StatusSpec(
        key="todo",
        label="待办",
        purpose="initial",
        order=0,
        allowed_targets=("ready", "blocked"),
    ),
    "ready": _StatusSpec(
        key="ready",
        label="可认领",
        purpose="ready",
        order=1,
        claimable=True,
        allowed_roles=("developer", "tester", "reviewer"),
        allowed_targets=("todo", "blocked"),
    ),
    "doing": _StatusSpec(
        key="doing",
        label="执行中",
        purpose="working",
        order=2,
    ),
    "done": _StatusSpec(
        key="done",
        label="已完成",
        purpose="completion",
        order=3,
        is_terminal=True,
        allowed_targets=("todo",),
    ),
    "blocked": _StatusSpec(
        key="blocked",
        label="阻塞",
        purpose="failure",
        order=4,
        allowed_targets=("ready", "todo"),
        max_retries=2,
    ),
    "triage": _StatusSpec(
        key="triage",
        label="分诊",
        purpose="escalation",
        order=5,
        allowed_targets=("todo",),
    ),
}

_FIXED_FLOW_EDGES: tuple[tuple[str, str, str], ...] = (
    ("todo", "ready", "前置就绪"),
    ("ready", "doing", "认领"),
    ("doing", "done", "完成"),
    ("doing", "ready", "失败重试"),
    ("doing", "blocked", "重试耗尽"),
    ("doing", "blocked", "阻塞"),
    ("doing", "todo", "依赖阻塞"),
    ("doing", "triage", "放弃"),
    ("blocked", "ready", "解阻"),
    ("blocked", "triage", "多次阻塞升级"),
)


# ---------------------------------------------------------------------------
# 业务对象（TBox）
# ---------------------------------------------------------------------------
class Board(ObjectType):
    """看板聚合根：承载租户边界与看板标识。"""

    __primary_key__ = "board_key"
    __title__ = "title"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)
    __aggregate_root__ = True

    board_key: str
    title: str
    tenant: str
    created_by: str
    description: str = ""


class Member(ObjectType):
    """看板成员：角色与在办上限（忙闲状态由当前 doing 卡片数实时派生）。"""

    __primary_key__ = "member_id"
    __title__ = "name"
    __owned__ = True

    member_id: str
    name: str
    role: str  # developer | reviewer | tester | coordinator
    wip_limit: int = 1
    completed_cards: int = 0


class Card(ObjectType):
    """任务卡片。"""

    __primary_key__ = "card_id"
    __title__ = "title"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)

    card_id: AutoID
    board: ObjectProperty[Board]
    tenant: str
    title: str
    body: str = ""
    status: CardStatus = "todo"
    priority: int = 0
    assignee: ObjectProperty[Member] | None = None
    waits_for: ObjectProperty[tuple[Card, ...]] = ()
    created_by: str = ""
    result: str = ""
    artifact_uri: str = ""
    block_kind: str | None = None
    block_recurrences: int = 0
    consecutive_failures: int = 0
    max_retries: int = 3


class CardComment(ObjectType):
    """卡片评论。"""

    __primary_key__ = "comment_id"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)

    comment_id: str
    card: ObjectProperty[Card]
    tenant: str
    author: str
    body: str
    created_at: int = 0


# ---------------------------------------------------------------------------
# Spec 与展示词条
# ---------------------------------------------------------------------------
spec = OntologySpec(read_only_source=True)
for _role_name, _role_desc in (
    ("board_manager", "看板管理员：建板、建卡、维护成员与升级处理"),
    ("admin", "系统管理员：具备看板全量管理权限"),
    ("system", "系统角色：供内部调度与运维编排使用"),
    ("developer", "开发者：认领并交付开发类卡片"),
    ("reviewer", "评审员：认领并开展评审类卡片"),
    ("tester", "测试员：认领并执行测试验证卡片"),
    ("coordinator", "统筹：协调评论同步与阻塞沟通"),
):
    spec.role(_role_name, _role_desc)
for _cls in (Board, Member, Card, CardComment):
    spec.object_type(_cls)

spec.display = {
    "ontologies": {"task_board": "任务看板"},
    "categories": {"Collaboration": "协作交付"},
    "objects": {
        "Board": "看板",
        "Member": "看板成员",
        "Card": "任务卡片",
        "CardComment": "卡片评论",
    },
    "object_properties": {
        "Card.board": "所属看板",
        "Card.assignee": "当前承接人",
        "Card.waits_for": "卡片前置依赖",
        "CardComment.card": "所属卡片",
    },
    "actions": {
        "create_board": "创建看板",
        "add_member": "新增看板成员",
        "create_card": "创建任务卡片",
        "move_card": "移动任务卡片",
        "claim_card": "认领任务卡片",
        "assign_card": "指派承接人",
        "release_claim": "释放卡片认领",
        "record_outcome": "登记执行结果",
        "unblock_card": "解除卡片阻塞",
        "promote_children": "提升子卡片",
        "add_comment": "追加卡片评论",
    },
    "functions": {
        "board_overview": "看板总览",
        "board_flow": "看板流程图",
        "eligible_cards": "查询可认领卡片",
        "card_briefing": "生成任务简报",
    },
    "statuses": {
        "todo": "待办",
        "ready": "可认领",
        "doing": "执行中",
        "done": "已完成",
        "blocked": "阻塞",
        "triage": "分诊",
        "dependency": "依赖未齐",
        "awaiting_review": "等待评审",
        "external_dependency": "外部依赖",
        "insufficient_input": "输入不足",
        "failed": "失败",
        "abandoned": "已放弃",
        "completed": "已完成",
        "developer": "开发者",
        "reviewer": "评审员",
        "tester": "测试员",
        "coordinator": "统筹",
    },
    "fields": {
        "board_key": "看板编号",
        "board": "所属看板",
        "tenant": "租户",
        "created_by": "创建人",
        "description": "说明",
        "member_id": "成员编号",
        "member": "看板成员",
        "wip_limit": "在办上限",
        "completed_cards": "已完成卡片数",
        "card_id": "卡片编号",
        "card": "任务卡片",
        "assignee": "承接人",
        "waits_for": "前置卡片",
        "body": "正文",
        "status": "状态",
        "target_status": "目标状态",
        "priority": "优先级",
        "result": "结论",
        "artifact_uri": "产出链接",
        "block_kind": "阻塞原因",
        "block_recurrences": "反复阻塞次数",
        "consecutive_failures": "连续失败次数",
        "max_retries": "最大重试",
        "comment_id": "评论编号",
        "author": "作者",
        "created_at": "创建时间",
        "outcome": "执行结果",
    },
    "roles": {
        "board_manager": "看板管理员",
        "developer": "开发者",
        "reviewer": "评审员",
        "tester": "测试员",
        "coordinator": "统筹",
    },
}


MEMBER_ROLES: tuple[str, ...] = ("developer", "reviewer", "tester", "coordinator")
OUTCOMES: tuple[str, ...] = ("completed", "blocked", "failed", "abandoned")
BLOCK_KINDS: tuple[str, ...] = (
    "dependency",
    "awaiting_review",
    "external_dependency",
    "insufficient_input",
)

BOARD_MANAGER = Actor(
    actor_id="board-manager-01",
    role="board_manager",
    clearance_tags={"*"},
)

_ALLOWED_ROLES = ("board_manager", "admin", "system")
_WORKER_ROLES = _ALLOWED_ROLES + MEMBER_ROLES


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------
class CreateBoardParams(BaseModel):
    board_key: str
    title: str
    tenant: str
    created_by: str
    description: str = ""


class AddMemberParams(BaseModel):
    member_id: str
    name: str
    role: MemberRole
    wip_limit: int = 1


class CreateCardParams(BaseModel):
    card_id: AutoID | None = Field(
        default=None,
        description="卡片编号，留空由系统自动生成",
    )
    board: ObjectProperty[Board]
    title: str
    body: str = ""
    priority: int = 0
    assignee: ObjectProperty[Member] | None = None
    waits_for: ObjectProperty[tuple[Card, ...]] = ()


class ClaimCardParams(BaseModel):
    card: ObjectProperty[Card]
    member: ObjectProperty[Member]


class AssignCardParams(BaseModel):
    card: ObjectProperty[Card]
    member: ObjectProperty[Member]


class ReleaseClaimParams(BaseModel):
    card: ObjectProperty[Card]
    reason: str = ""


class MoveCardParams(BaseModel):
    card: ObjectProperty[Card]
    target_status: CardStatus
    reason: str = ""


class RecordOutcomeParams(BaseModel):
    card: ObjectProperty[Card]
    member: ObjectProperty[Member]
    outcome: OutcomeType
    summary: str = ""
    block_kind: BlockKind | None = None
    artifact_uri: str = ""


class UnblockCardParams(BaseModel):
    card: ObjectProperty[Card]
    reason: str = ""


class PromoteChildrenParams(BaseModel):
    card: ObjectProperty[Card]


class AddCommentParams(BaseModel):
    card: ObjectProperty[Card]
    author: str
    body: str


class BoardOverviewParams(BaseModel):
    board: str = ""


class EligibleCardsParams(BaseModel):
    board: str = ""
    role: str = ""


class CardBriefingParams(BaseModel):
    card: ObjectProperty[Card]


class BoardFlowParams(BaseModel):
    board: str = ""


# ---------------------------------------------------------------------------
# 纯业务辅助
# ---------------------------------------------------------------------------
def _require_board(ctx: Any, board_key: str) -> Board:
    board = ctx.get(Board, board_key)
    if board is None:
        raise InvariantError(
            f"看板 {board_key!r} 不存在", code="BOARD_NOT_FOUND"
        )
    return board


def _require_card(ctx: Any, card_id: str) -> Card:
    card = ctx.get(Card, card_id)
    if card is None:
        raise InvariantError(f"卡片 {card_id!r} 不存在", code="CARD_NOT_FOUND")
    return card


def _require_member(ctx: Any, member_id: str) -> Member:
    member = ctx.get(Member, member_id)
    if member is None:
        raise InvariantError(f"成员 {member_id!r} 不存在", code="MEMBER_NOT_FOUND")
    return member


def _status_spec(status: str) -> _StatusSpec:
    spec_row = STATUS_SPECS.get(status)
    if spec_row is None:
        raise InvariantError(
            f"非法卡片状态 {status!r}", code="INVALID_CARD_STATUS"
        )
    return spec_row


def _assert_completion_requires_outcome(target_status: str, *, entry: str) -> None:
    """完成态只能由 record_outcome(completed) 在全部校验通过后进入。"""
    if target_status == "done":
        raise InvariantError(
            f"{entry}不能把卡片直接设为完成态 'done'；"
            "完成态只能由 record_outcome(completed) 进入",
            code="COMPLETION_REQUIRES_RECORD_OUTCOME",
        )


def _assert_actor_matches_member(ctx: Any, member: Member) -> None:
    """管理角色可代办；执行角色只能操作本人对应的成员席位。"""
    actor = getattr(ctx, "actor", None)
    if actor is None or actor.role in _ALLOWED_ROLES:
        return
    if actor.actor_id != member.member_id:
        raise InvariantError(
            f"调用方 {actor.actor_id!r} 不能冒名操作成员 {member.member_id!r}",
            code="ACTOR_MEMBER_MISMATCH",
        )


def _member_active_wip(ctx: Any, member_id: str) -> int:
    return sum(
        1
        for row in ctx.query(Card)
        if row.assignee == member_id and row.status == "doing"
    )


def _parents_of(ctx: Any, card: Card) -> list[Card]:
    return ctx.query_linked(card, "waits_for", Card)


def _children_of(ctx: Any, card: Card) -> list[Card]:
    out: list[Card] = []
    for candidate in ctx.query(Card):
        if candidate.card_id == card.card_id:
            continue
        for parent in _parents_of(ctx, candidate):
            if parent.card_id == card.card_id:
                out.append(candidate)
                break
    return out


def _comments_of(ctx: Any, card: Card) -> list[CardComment]:
    rows = [row for row in ctx.query(CardComment) if row.card == card.card_id]
    rows.sort(key=lambda row: row.comment_id)
    return rows


def _is_working_claim(card: Card) -> bool:
    return card.assignee is not None and card.status == "doing"


def _parents_satisfied(ctx: Any, card: Card) -> bool:
    return all(parent.status == "done" for parent in _parents_of(ctx, card))


def _start_card_execution(
    ctx: Any,
    card: Card,
    member: Member,
    *,
    allow_preassigned: bool = False,
    allow_reassign: bool = False,
) -> None:
    """校验认领/指派不变量并推入 doing。"""
    if card.assignee is not None and not (
        (allow_preassigned and card.assignee == member.member_id)
        or (allow_reassign and card.status != "doing")
    ):
        raise InvariantError(
            f"卡片 {card.card_id!r} 已被 {card.assignee!r} 认领",
            code="CARD_ALREADY_CLAIMED",
        )
    status_spec = _status_spec(card.status)
    if not status_spec.claimable:
        raise InvariantError(
            f"状态 {card.status!r} 不可认领", code="CARD_NOT_CLAIMABLE"
        )
    if member.role not in status_spec.allowed_roles:
        raise InvariantError(
            f"角色 {member.role!r} 不能认领状态 {card.status!r} 的卡片",
            code="ROLE_NOT_ALLOWED_FOR_STATUS",
        )
    if _member_active_wip(ctx, member.member_id) >= member.wip_limit:
        raise InvariantError(
            f"成员 {member.member_id} 已达在办上限（wip_limit={member.wip_limit}）",
            code="WIP_LIMIT_EXCEEDED",
        )
    if not _parents_satisfied(ctx, card):
        raise InvariantError(
            f"卡片 {card.card_id!r} 的前置尚未全部完成",
            code="PARENTS_NOT_SATISFIED",
        )

    ctx.modify(
        card,
        status="doing",
        assignee=member.member_id,
        block_kind=None,
    )


def _promote_ready_children(ctx: Any, card: Card) -> list[str]:
    """前置已全部完成的子卡，从 todo 提升到 ready；若已预指派且未超 WIP 上限则直接开工。"""
    ready_spec = STATUS_SPECS["ready"]
    promoted: list[str] = []
    for child in _children_of(ctx, card):
        if child.status != "todo":
            continue
        if not _parents_satisfied(ctx, child):
            continue
        updated_child = ctx.modify(child, status="ready")
        if updated_child.assignee is not None:
            member = ctx.get(Member, updated_child.assignee)
            if (
                member is not None
                and member.role in ready_spec.allowed_roles
                and _member_active_wip(ctx, member.member_id) < member.wip_limit
            ):
                _start_card_execution(
                    ctx, updated_child, member, allow_preassigned=True
                )
        promoted.append(child.card_id)
    return promoted


def _status_subtitle(status_spec: _StatusSpec) -> str:
    marks: list[str] = []
    if status_spec.claimable:
        marks.append("可认领")
    if status_spec.is_terminal:
        marks.append("终态")
    purpose_labels = {
        "initial": "初始",
        "ready": "可开工",
        "working": "执行中",
        "completion": "完成",
        "failure": "失败",
        "escalation": "升级",
    }
    head = purpose_labels.get(status_spec.purpose, status_spec.purpose)
    if marks:
        return f"{head} · {' · '.join(marks)}"
    return head


# ---------------------------------------------------------------------------
# 动作：建板 / 加成员
# ---------------------------------------------------------------------------
@spec.edit_function("create_board", CreateBoardParams, description="建板：创建使用固定 6 态流的任务看板。")
def create_board(ctx: Any, params: CreateBoardParams) -> None:
    if ctx.get(Board, params.board_key) is not None:
        raise InvariantError(
            f"看板 {params.board_key!r} 已存在", code="BOARD_ALREADY_EXISTS"
        )
    if not params.tenant.strip():
        raise InvariantError("租户不能为空", code="EMPTY_TENANT")

    ctx.create(
        Board(
            board_key=params.board_key,
            title=params.title,
            tenant=params.tenant,
            created_by=params.created_by,
            description=params.description,
        )
    )

spec.declare_action(
    "create_board",
    edit_function="create_board",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="建板：创建使用固定 6 态流的任务看板。",
)


@spec.edit_function("add_member", AddMemberParams, description="新增看板成员（声明角色与在办 WIP 上限）。")
def add_member(ctx: Any, params: AddMemberParams) -> None:
    if params.role not in MEMBER_ROLES:
        raise InvariantError(
            f"非法角色 {params.role!r}，只允许 {list(MEMBER_ROLES)}",
            code="INVALID_MEMBER_ROLE",
        )
    if params.wip_limit < 1:
        raise InvariantError("wip_limit 必须 >= 1", code="INVALID_WIP_LIMIT")
    if ctx.get(Member, params.member_id) is not None:
        raise InvariantError(
            f"成员 {params.member_id!r} 已存在", code="MEMBER_ALREADY_EXISTS"
        )
    ctx.create(
        Member(
            member_id=params.member_id,
            name=params.name,
            role=params.role,
            wip_limit=params.wip_limit,
        )
    )

spec.declare_action(
    "add_member",
    edit_function="add_member",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="新增看板成员（声明角色与在办 WIP 上限）。",
)


# ---------------------------------------------------------------------------
# 动作：建卡
# ---------------------------------------------------------------------------
def _next_card_id(ctx: Any) -> str:
    max_num = 0
    width = 3
    for row in ctx.query(Card):
        cid = row.card_id.strip()
        if cid.startswith("C-") and cid[2:].isdigit():
            suffix = cid[2:]
            max_num = max(max_num, int(suffix))
            width = max(width, len(suffix))
    seq = max_num + 1
    while True:
        candidate = f"C-{seq:0{width}d}"
        if ctx.get(Card, candidate) is None:
            return candidate
        seq += 1


@spec.edit_function(
    "create_card",
    CreateCardParams,
    description="建卡：无前置落入 ready，有前置停在 todo 等待前置完成。",
)
def create_card(ctx: Any, params: CreateCardParams) -> None:
    board = _require_board(ctx, params.board)
    raw_card_id = (params.card_id or "").strip()
    card_id = raw_card_id or _next_card_id(ctx)
    if ctx.get(Card, card_id) is not None:
        raise InvariantError(
            f"卡片 {card_id!r} 已存在", code="CARD_ALREADY_EXISTS"
        )
    if not params.title.strip():
        raise InvariantError("卡片标题不能为空", code="EMPTY_CARD_TITLE")
    assignee_id = (params.assignee or "").strip() or None
    member: Member | None = None
    if assignee_id is not None:
        member = _require_member(ctx, assignee_id)
        if member.role not in STATUS_SPECS["ready"].allowed_roles:
            raise InvariantError(
                f"角色 {member.role!r} 不能承接执行卡片 {card_id!r}",
                code="ROLE_NOT_ALLOWED_FOR_STATUS",
            )

    parent_ids: list[str] = []
    for raw_parent_id in params.waits_for:
        parent_id = raw_parent_id.strip()
        if not parent_id:
            continue
        if parent_id == card_id:
            raise InvariantError(
                "卡片不能依赖自己", code="CARD_DEPENDS_ON_ITSELF"
            )
        parent = ctx.get(Card, parent_id)
        if parent is None:
            raise InvariantError(
                f"前置卡片 {parent_id!r} 不存在", code="PARENT_NOT_FOUND"
            )
        if parent.board != board.board_key:
            raise InvariantError(
                f"前置卡片 {parent_id!r} 不在同一块看板上",
                code="PARENT_CROSS_BOARD",
            )
        if parent_id not in parent_ids:
            parent_ids.append(parent_id)

    landing_status: CardStatus = "ready" if not parent_ids else "todo"

    card = ctx.create(
        Card(
            card_id=card_id,
            board=board.board_key,
            tenant=board.tenant,
            title=params.title,
            body=params.body,
            status=landing_status,
            priority=params.priority,
            assignee=assignee_id,
            waits_for=tuple(parent_ids),
            max_retries=STATUS_SPECS[landing_status].max_retries,
        )
    )

    _assert_no_dependency_cycle(ctx, card)

    if member is not None and landing_status == "ready":
        _start_card_execution(ctx, card, member, allow_preassigned=True)

spec.declare_action(
    "create_card",
    edit_function="create_card",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="建卡：无前置落入 ready，有前置停在 todo 等待前置完成。",
)


def _assert_no_dependency_cycle(ctx: Any, card: Card) -> None:
    seen: set[str] = set()
    frontier = [card]
    while frontier:
        current = frontier.pop()
        if current.card_id in seen:
            continue
        seen.add(current.card_id)
        for parent in _parents_of(ctx, current):
            if parent.card_id == card.card_id:
                raise InvariantError(
                    f"卡片 {card.card_id!r} 的依赖形成环",
                    code="DEPENDENCY_CYCLE",
                )
            frontier.append(parent)


# ---------------------------------------------------------------------------
# 动作：人工移动 / 认领 / 指派 / 释放
# ---------------------------------------------------------------------------
@spec.edit_function("move_card", MoveCardParams, description="人工把未认领卡片移到允许的下一状态（禁止绕行进入 done）。")
def move_card(ctx: Any, params: MoveCardParams) -> None:
    card = _require_card(ctx, params.card)
    source = _status_spec(card.status)
    target = _status_spec(params.target_status)

    _assert_completion_requires_outcome(target.key, entry="人工移动")
    if target.key not in source.allowed_targets:
        raise InvariantError(
            f"状态 {source.key!r} 不允许把卡片移动到 {target.key!r}",
            code="MANUAL_MOVE_NOT_ALLOWED",
        )
    if card.assignee is not None:
        raise InvariantError(
            f"卡片 {card.card_id!r} 已被 {card.assignee!r} 认领，不能人工移动",
            code="CARD_ALREADY_CLAIMED",
        )
    updates: dict[str, Any] = {
        "status": target.key,
        "max_retries": target.max_retries,
    }
    if source.key in ("blocked", "triage", "done") and target.key in ("todo", "ready"):
        updates["consecutive_failures"] = 0
        updates["block_kind"] = None
    ctx.modify(card, **updates)

spec.declare_action(
    "move_card",
    edit_function="move_card",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="人工把未认领卡片移到允许的下一状态（禁止绕行进入 done）。",
)


@spec.edit_function(
    "claim_card",
    ClaimCardParams,
    description="认领卡片：校验前置完成、角色资格、WIP 上限与 Actor/Member 身份绑定。",
)
def claim_card(ctx: Any, params: ClaimCardParams) -> None:
    card = _require_card(ctx, params.card)
    member = _require_member(ctx, params.member)
    _assert_actor_matches_member(ctx, member)
    _start_card_execution(
        ctx,
        card,
        member,
        allow_preassigned=_status_spec(card.status).claimable,
    )

spec.declare_action(
    "claim_card",
    edit_function="claim_card",
    allowed_roles=_WORKER_ROLES,
    affects_source=False,
    description="认领卡片：校验前置完成、角色资格、WIP 上限与 Actor/Member 身份绑定。",
)


@spec.edit_function(
    "assign_card",
    AssignCardParams,
    description="指派承接人：将 ready 卡片指派给未超 WIP 上限的成员并推入 doing。",
)
def assign_card(ctx: Any, params: AssignCardParams) -> None:
    card = _require_card(ctx, params.card)
    member = _require_member(ctx, params.member)
    _start_card_execution(
        ctx,
        card,
        member,
        allow_preassigned=_status_spec(card.status).claimable,
        allow_reassign=(card.status != "doing"),
    )

spec.declare_action(
    "assign_card",
    edit_function="assign_card",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="指派承接人：将 ready 卡片指派给未超 WIP 上限的成员并推入 doing。",
)


@spec.edit_function(
    "release_claim",
    ReleaseClaimParams,
    description="释放认领：把 doing 卡片退回 ready 或 blocked 并计入一次失败重试。",
)
def release_claim(ctx: Any, params: ReleaseClaimParams) -> None:
    card = _require_card(ctx, params.card)
    if not _is_working_claim(card):
        raise InvariantError(
            f"卡片 {card.card_id!r} 没有在途的执行尝试",
            code="NO_RUNNING_ATTEMPT",
        )
    failures = card.consecutive_failures + 1
    target: CardStatus = "blocked" if failures > card.max_retries else "ready"
    ctx.modify(
        card,
        assignee=None,
        status=target,
        consecutive_failures=failures,
    )

spec.declare_action(
    "release_claim",
    edit_function="release_claim",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="释放认领：把 doing 卡片退回 ready 或 blocked 并计入一次失败重试。",
)


# ---------------------------------------------------------------------------
# 动作：登记结果 / 解阻 / 提升子卡 / 评论
# ---------------------------------------------------------------------------
@spec.edit_function(
    "record_outcome",
    RecordOutcomeParams,
    description="登记执行结果：completed / blocked / failed / abandoned。",
)
def record_outcome(ctx: Any, params: RecordOutcomeParams) -> None:
    card = _require_card(ctx, params.card)
    member = _require_member(ctx, params.member)
    _assert_actor_matches_member(ctx, member)

    if params.outcome not in OUTCOMES:
        raise InvariantError(
            f"非法结果 {params.outcome!r}，只允许 {list(OUTCOMES)}",
            code="INVALID_OUTCOME",
        )
    if card.assignee != member.member_id:
        raise InvariantError(
            f"卡片 {card.card_id!r} 当前承接人是 {card.assignee!r}，"
            f"{member.member_id!r} 无法登记结果",
            code="CARD_NOT_CLAIMED_BY_MEMBER",
        )
    if not _is_working_claim(card):
        raise InvariantError(
            f"卡片 {card.card_id!r} 没有在途的执行尝试",
            code="NO_RUNNING_ATTEMPT",
        )

    if params.outcome == "completed":
        if not _parents_satisfied(ctx, card):
            raise InvariantError(
                f"卡片 {card.card_id!r} 的前置尚未全部完成，不能完成",
                code="PARENTS_NOT_SATISFIED",
            )
        ctx.modify(
            card,
            assignee=None,
            status="done",
            result=params.summary,
            artifact_uri=params.artifact_uri,
            consecutive_failures=0,
            block_kind=None,
        )
        ctx.modify(member, completed_cards=member.completed_cards + 1)
        _promote_ready_children(ctx, card)
        return

    if params.outcome == "blocked":
        if params.block_kind not in BLOCK_KINDS:
            raise InvariantError(
                f"阻塞必须声明阻塞原因，取值 {list(BLOCK_KINDS)}",
                code="INVALID_BLOCK_KIND",
            )
        blocked_target: CardStatus = (
            "todo" if params.block_kind == "dependency" else "blocked"
        )
        ctx.modify(
            card,
            assignee=None,
            status=blocked_target,
            block_kind=params.block_kind,
        )
        return

    if params.outcome == "failed":
        failures = card.consecutive_failures + 1
        failed_target: CardStatus = (
            "blocked" if failures > card.max_retries else "ready"
        )
        ctx.modify(
            card,
            assignee=None,
            status=failed_target,
            consecutive_failures=failures,
        )
        return

    ctx.modify(card, assignee=None, status="triage")

spec.declare_action(
    "record_outcome",
    edit_function="record_outcome",
    allowed_roles=_WORKER_ROLES,
    affects_source=False,
    description="登记执行结果：completed / blocked / failed / abandoned。",
)


@spec.edit_function("unblock_card", UnblockCardParams, description="解除阻塞；反复阻塞超过上限则升级至 triage。")
def unblock_card(ctx: Any, params: UnblockCardParams) -> None:
    card = _require_card(ctx, params.card)
    if card.block_kind is None and card.status != "blocked":
        raise InvariantError(
            f"卡片 {card.card_id!r} 当前没有阻塞标记",
            code="CARD_NOT_BLOCKED",
        )
    if card.status == "doing" or card.assignee is not None:
        raise InvariantError(
            f"卡片 {card.card_id!r} 已被 {card.assignee!r} 占用，不能解阻",
            code="CARD_ALREADY_CLAIMED",
        )
    recurrences = card.block_recurrences + 1
    target: CardStatus = (
        "triage"
        if recurrences > STATUS_SPECS["blocked"].max_retries
        else "ready"
    )
    ctx.modify(
        card,
        status=target,
        block_kind=None,
        block_recurrences=recurrences,
        consecutive_failures=0,
    )

spec.declare_action(
    "unblock_card",
    edit_function="unblock_card",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="解除阻塞；反复阻塞超过上限则升级至 triage。",
)


@spec.edit_function(
    "promote_children",
    PromoteChildrenParams,
    description="把前置已全部完成的子卡从 todo 提升到 ready。",
)
def promote_children(ctx: Any, params: PromoteChildrenParams) -> None:
    card = _require_card(ctx, params.card)
    _promote_ready_children(ctx, card)

spec.declare_action(
    "promote_children",
    edit_function="promote_children",
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="把前置已全部完成的子卡从 todo 提升到 ready。",
)


@spec.edit_function("add_comment", AddCommentParams, description="给卡片追加一条评论。")
def add_comment(ctx: Any, params: AddCommentParams) -> None:
    card = _require_card(ctx, params.card)
    if not params.body.strip():
        raise InvariantError("评论内容不能为空", code="EMPTY_COMMENT_BODY")
    ctx.create(
        CardComment(
            comment_id=f"{card.card_id}-C{len(_comments_of(ctx, card)) + 1:02d}",
            card=card.card_id,
            tenant=card.tenant,
            author=params.author,
            body=params.body,
            created_at=int(time.time()),
        )
    )

spec.declare_action(
    "add_comment",
    edit_function="add_comment",
    allowed_roles=_WORKER_ROLES,
    affects_source=False,
    description="给卡片追加一条评论。",
)


# ---------------------------------------------------------------------------
# 只读查询（供上游 Agent 感知看板状态与可领任务）
# ---------------------------------------------------------------------------
@spec.function(
    "board_overview",
    BoardOverviewParams,
    description="看板总览：固定 6 态分布、卡片列表与成员 WIP 负载。",
)
def board_overview(ctx: Any, params: BoardOverviewParams) -> dict[str, Any]:
    if not params.board.strip():
        boards = sorted(ctx.query(Board), key=lambda row: row.board_key)
        return {
            "boards": [_overview_of(ctx, board) for board in boards],
            "boards_total": len(boards),
        }
    return _overview_of(ctx, _require_board(ctx, params.board))


def _overview_of(ctx: Any, board: Board) -> dict[str, Any]:
    cards = [row for row in ctx.query(Card) if row.board == board.board_key]

    by_status: dict[str, int] = {key: 0 for key in CARD_STATUSES}
    for card in cards:
        by_status[card.status] = by_status.get(card.status, 0) + 1

    ordered_specs = sorted(STATUS_SPECS.values(), key=lambda s: s.order)
    members = sorted(ctx.query(Member), key=lambda m: m.member_id)
    return {
        "board_key": board.board_key,
        "title": board.title,
        "tenant": board.tenant,
        "cards_total": len(cards),
        "cards_summary": [
            {
                "card_id": card.card_id,
                "title": card.title,
                "status": card.status,
                "assignee": card.assignee,
                "result": card.result,
            }
            for card in sorted(cards, key=lambda row: row.card_id)
        ],
        "statuses": [
            {
                "key": st.key,
                "label": st.label,
                "purpose": st.purpose,
                "order": st.order,
                "is_terminal": st.is_terminal,
                "claimable": st.claimable,
                "allowed_roles": list(st.allowed_roles),
                "allowed_targets": list(st.allowed_targets),
                "max_retries": st.max_retries,
                "cards": by_status.get(st.key, 0),
            }
            for st in ordered_specs
        ],
        "members": [
            {
                "member_id": m.member_id,
                "name": m.name,
                "role": m.role,
                "active_wip": _member_active_wip(ctx, m.member_id),
                "wip_limit": m.wip_limit,
                "completed_cards": m.completed_cards,
            }
            for m in members
        ],
    }


@spec.function(
    "eligible_cards",
    EligibleCardsParams,
    description="查询当前可被 Agent 认领的卡片（status=ready、前置已全 done、未指派），按优先级排序。",
)
def eligible_cards(ctx: Any, params: EligibleCardsParams) -> dict[str, Any]:
    board_key = params.board.strip()
    if board_key:
        _require_board(ctx, board_key)
    role = params.role.strip()
    ready_roles = STATUS_SPECS["ready"].allowed_roles
    if role and role not in ready_roles:
        return {"cards": [], "total": 0}

    candidates = [
        card
        for card in ctx.query(Card)
        if (not board_key or card.board == board_key)
        and card.status == "ready"
        and card.assignee is None
        and _parents_satisfied(ctx, card)
    ]
    candidates.sort(key=lambda c: (-c.priority, c.card_id))
    return {
        "cards": [
            {
                "card_id": c.card_id,
                "board": c.board,
                "title": c.title,
                "priority": c.priority,
                "status": c.status,
            }
            for c in candidates
        ],
        "total": len(candidates),
    }


@spec.function(
    "board_flow",
    BoardFlowParams,
    description="看板流程图：固定 6 态节点与流转边。",
)
def board_flow(ctx: Any, params: BoardFlowParams) -> dict[str, Any]:
    if params.board.strip():
        boards = [_require_board(ctx, params.board)]
    else:
        boards = sorted(ctx.query(Board), key=lambda row: row.board_key)

    ordered_specs = sorted(STATUS_SPECS.values(), key=lambda s: s.order)
    flows: list[dict[str, Any]] = []
    for board in boards:
        cards = [
            row for row in ctx.query(Card) if row.board == board.board_key
        ]
        counts: dict[str, int] = {key: 0 for key in CARD_STATUSES}
        for card in cards:
            counts[card.status] = counts.get(card.status, 0) + 1

        nodes = [
            {
                "id": st.key,
                "label": st.label,
                "subtitle": _status_subtitle(st),
                "badge": counts.get(st.key, 0),
                "order": st.order,
                "purpose": st.purpose,
                "cards": counts.get(st.key, 0),
                "is_terminal": st.is_terminal,
                "claimable": st.claimable,
            }
            for st in ordered_specs
        ]

        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def add_edge(source: str, target: str, label: str) -> None:
            if not target or source == target:
                return
            key = (source, target, label)
            if key in seen:
                return
            seen.add(key)
            edges.append({"from": source, "to": target, "label": label})

        for src, dst, label in _FIXED_FLOW_EDGES:
            add_edge(src, dst, label)
        for st in ordered_specs:
            for target in st.allowed_targets:
                add_edge(st.key, target, "人工移动")

        flows.append(
            {
                "board_key": board.board_key,
                "title": board.title,
                "nodes": nodes,
                "edges": edges,
            }
        )

    if params.board.strip():
        return flows[0]
    return {"flows": flows, "flows_total": len(flows)}


@spec.function(
    "card_briefing",
    CardBriefingParams,
    description="结构化任务简报：卡片事实 + 状态语义 + 前置依赖 + 评论。",
)
def card_briefing(ctx: Any, params: CardBriefingParams) -> dict[str, Any]:
    card = _require_card(ctx, params.card)
    status_spec = _status_spec(card.status)
    board = _require_board(ctx, card.board)

    parents = _parents_of(ctx, card)
    children = _children_of(ctx, card)
    comments = _comments_of(ctx, card)

    return {
        "card_id": card.card_id,
        "board": card.board,
        "board_title": board.title,
        "title": card.title,
        "body": card.body,
        "status": card.status,
        "status_meta": {
            "key": status_spec.key,
            "label": status_spec.label,
            "purpose": status_spec.purpose,
            "claimable": status_spec.claimable,
            "allowed_roles": list(status_spec.allowed_roles),
        },
        "priority": card.priority,
        "assignee": card.assignee,
        "consecutive_failures": card.consecutive_failures,
        "max_retries": card.max_retries,
        "block_kind": card.block_kind,
        "block_recurrences": card.block_recurrences,
        "result": card.result,
        "artifact_uri": card.artifact_uri,
        "parents": [
            {
                "card_id": parent.card_id,
                "title": parent.title,
                "status": parent.status,
                "satisfied": parent.status == "done",
            }
            for parent in parents
        ],
        "children": [
            {"card_id": child.card_id, "title": child.title}
            for child in children
        ],
        "comments": [
            {"comment_id": item.comment_id, "author": item.author, "body": item.body}
            for item in comments
        ],
    }


def render_card_briefing(briefing: dict[str, Any]) -> str:
    """把结构化简报渲染成纯文本。"""
    status_meta = briefing.get("status_meta") or {}
    lines = [
        f"卡片：{briefing['card_id']} · {briefing['title']}",
        f"看板：{briefing['board_title']}（{briefing['board']}）",
        f"当前状态：{status_meta.get('label', briefing.get('status', ''))}"
        f"（{briefing.get('status', '')}）",
    ]
    if briefing.get("body"):
        lines.append(f"任务说明：{briefing['body']}")
    if briefing.get("parents"):
        lines.append("前置卡片：")
        for parent in briefing["parents"]:
            mark = "已满足" if parent["satisfied"] else "未完成"
            lines.append(
                f"  - {parent['card_id']} {parent['title']} [{mark}]"
            )
    if briefing.get("result"):
        lines.append(f"最近结论：{briefing['result']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 全局状态后置不变量（@spec.invariant）
# ---------------------------------------------------------------------------
@spec.invariant("wip_and_claim_integrity", code="INVARIANT_WIP_AND_CLAIM")
def _invariant_wip_and_claim(ctx: Any) -> None:
    """WIP 上限、承接资格、前置就绪与非在途清理必须在每次提交后成立。"""
    members_by_id = {member.member_id: member for member in ctx.query(Member)}
    active_counts: dict[str, int] = {member_id: 0 for member_id in members_by_id}
    allowed_roles = STATUS_SPECS["ready"].allowed_roles

    for card in ctx.query(Card):
        if card.assignee is not None:
            member = members_by_id.get(card.assignee)
            assert member is not None, (
                f"卡片 {card.card_id!r} 承接人 {card.assignee!r} 不存在"
            )
            assert member.role in allowed_roles, (
                f"成员 {member.member_id!r} 角色 {member.role!r} 不能承接卡片 {card.card_id!r}"
            )

        if card.status == "doing":
            assert card.assignee is not None, (
                f"执行中卡片 {card.card_id!r} 缺少承接人"
            )
            assert _parents_satisfied(ctx, card), (
                f"执行中卡片 {card.card_id!r} 的前置尚未全部完成"
            )
            active_counts[card.assignee] = active_counts.get(card.assignee, 0) + 1
        elif card.status in ("done", "blocked", "triage"):
            assert card.assignee is None, (
                f"状态 {card.status!r} 的卡片 {card.card_id!r} 不能保留承接人 {card.assignee!r}"
            )

    for member in members_by_id.values():
        assert member.wip_limit >= 1, (
            f"成员 {member.member_id!r} wip_limit 必须 >= 1"
        )
        assert active_counts.get(member.member_id, 0) <= member.wip_limit, (
            f"成员 {member.member_id!r} 在办卡片超出 wip_limit={member.wip_limit}"
        )


@spec.invariant("retry_and_status_consistency", code="INVARIANT_RETRY_CONSISTENCY")
def _invariant_retry_and_status(ctx: Any) -> None:
    """卡片状态合法且 todo/ready/doing 卡片的连续失败次数不得超过上限。"""
    for card in ctx.query(Card):
        assert card.status in CARD_STATUSES, f"非法卡片状态 {card.status!r}"
        assert card.consecutive_failures >= 0 and card.block_recurrences >= 0, (
            f"卡片 {card.card_id!r} 失败或阻塞计数不能为负"
        )
        if card.status in ("todo", "ready", "doing"):
            assert card.consecutive_failures <= card.max_retries, (
                f"状态 {card.status!r} 的卡片 {card.card_id!r} "
                f"连续失败次数 {card.consecutive_failures} 超出上限 {card.max_retries}"
            )


@spec.invariant("dependency_dag_and_board_scope", code="INVARIANT_DEPENDENCY_DAG")
def _invariant_dependency_dag(ctx: Any) -> None:
    """所有卡片必须归属有效看板且前置依赖同板无环。"""
    boards_by_key = {board.board_key: board for board in ctx.query(Board)}
    for card in ctx.query(Card):
        board = boards_by_key.get(card.board)
        assert board is not None, (
            f"卡片 {card.card_id!r} 所属看板 {card.board!r} 不存在"
        )
        assert card.tenant == board.tenant, (
            f"卡片 {card.card_id!r} 租户 {card.tenant!r} 与看板 {board.tenant!r} 不一致"
        )
        for parent in _parents_of(ctx, card):
            assert parent.card_id != card.card_id, "卡片不能依赖自己"
            assert parent.board == card.board, (
                f"前置卡片 {parent.card_id!r} 不在同一块看板上"
            )
        _assert_no_dependency_cycle(ctx, card)
