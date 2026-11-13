"""多智能体任务看板本体（TaskBoardOntology）—— 100% 业务纯洁层（PIM）。

本模块只描述「一块任务看板」这件事本身：

* 看板 ``Board``（一个租户的一块板）
* 看板列 ``BoardColumn``（列的**语义**——可认领？终态？父卡门控？失败回哪一列？）
* 成员 ``Member``（承接卡片的人）
* 卡片 ``Card``（任务本体）
* 评论 ``CardComment`` 与执行尝试 ``CardRun``

以及建板 / 建卡 / 认领 / 登记结果 / 解阻 / 提升子卡的流转与全部护栏。

**列语义进本体是本模块的核心。** 看板拓扑不是散落在代码里的 if/else，
而是 ``BoardColumn`` 上的数据：谁可认领、哪一列是终态、父卡没做完能不能进、
失败回哪一列、允许重试几次——全部是声明，并在建板时一次性校验。

这里不出现任何执行宿主、会话、协议、进程、凭据或速率细节 ——
那些都属于外围机械臂适配层，不属于业务蓝图。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from .spec import Actor, InvariantError, LinkType, ObjectType, OntologySpec

__all__ = [
    "Board",
    "BoardColumn",
    "Member",
    "Card",
    "CardComment",
    "CardRun",
    "Evidence",
    "CARD_WAITS_FOR",
    "CARD_COMMENTS",
    "CARD_RUNS",
    "CARD_ASSIGNED_TO",
    "spec",
    "MEMBER_ROLES",
    "OUTCOMES",
    "BLOCK_KINDS",
    "COLUMN_PURPOSES",
    "BOARD_MANAGER",
    "render_card_briefing",
    "render_board_handover",
]


# ---------------------------------------------------------------------------
# 纯业务对象
# ---------------------------------------------------------------------------
class Board(ObjectType):
    """看板。租户是板内的软命名空间，同时是行级安全标签。

    看板同时承载**项目管理的三要素**：

    * ``goal``：项目目标与架构红线（这块板为什么存在、什么绝不能破）；
    * ``state``：当前基线状态（「现在什么算数」，一句话折叠已发生的历史）；
    * ``unfolded_completions``：自上次更新 ``state`` 以来新完成的卡片数
      （折叠压力表——攒够了就该把历史压进 ``state``）。
    """

    __primary_key__ = "board_key"
    __title__ = "title"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)
    #: 一块看板是一个聚合根：卡片 / 列 / 成员都是它的组成部分。
    __aggregate_root__ = True

    board_key: str
    title: str
    tenant: str
    created_by: str
    description: str = ""
    #: 项目目标与架构红线。
    goal: str = ""
    #: 当前基线状态：现在什么算数。
    state: str = ""
    #: 自上次更新 state 以来新完成的卡片数。
    unfolded_completions: int = 0


class BoardColumn(ObjectType):
    """看板列。

    每一列都带**用途**（``purpose``）、**语义**（能否认领 / 是否终态 /
    是否父卡门控 / 允许哪些角色 / 允许重试几次）与**流转出口**
    （``on_*``：一张卡在我这里结束本轮工作时去哪）。

    流转出口是数据，因此整张流程图可被查询、可审计、可画出来；
    代码里不再出现任何硬编码的阶段名或状态分支。
    """

    __primary_key__ = "column_uid"
    __title__ = "label"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)

    column_uid: str
    board_key: str
    key: str
    label: str
    tenant: str
    purpose: str = "initial"
    order: int = 0
    is_terminal: bool = False
    claimable: bool = False
    parent_gated: bool = False
    allowed_roles: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    max_retries: int = 3
    #: 本列结束时的流转出口（目标列标识；空串表示留在本列）。
    on_claimed: str = ""
    on_completed: str = ""
    on_failed: str = ""
    on_exhausted: str = ""
    on_blocked: str = ""
    on_abandoned: str = ""
    on_unblocked: str = ""


class Member(ObjectType):
    """看板成员：承接卡片的人。"""

    __primary_key__ = "member_id"
    __title__ = "name"
    __owned__ = True

    member_id: str
    name: str
    role: str  # developer | reviewer | tester | coordinator
    availability: str = "IDLE"  # IDLE | BUSY | UNAVAILABLE
    active_card_id: str | None = None
    completed_cards: int = 0


class Card(ObjectType):
    """任务卡片。"""

    __primary_key__ = "card_id"
    __title__ = "title"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)

    card_id: str
    board_key: str
    tenant: str
    title: str
    body: str = ""
    column_key: str
    priority: int = 0
    assignee_id: str | None = None
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
    card_id: str
    tenant: str
    author: str
    body: str
    created_at: int = 0


class Evidence(BaseModel):
    """一条**带数字**的验收证据。

    ``value`` 必须含数字：这把「数字必须来自实际运行，不许复述转述稿」
    这条纪律从提示词里的口头要求，变成机器可判的护栏。
    """

    metric: str
    value: str
    detail: str = ""


class CardRun(ObjectType):
    """一次执行尝试。

    认领一次 = 一条。它是「这张卡被谁、在哪一列、第几次、结果如何、证据是什么」
    的唯一权威记录。
    """

    __primary_key__ = "run_id"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)

    run_id: str
    card_id: str
    tenant: str
    member_id: str
    column_key: str
    status: str = "running"  # running | done | blocked | failed | abandoned
    outcome: str | None = None
    summary: str = ""
    evidence: list[Evidence] = []
    started_at: int = 0
    ended_at: int | None = None
    #: 完工时折叠进看板的新基线状态（旧记录没有该字段，默认 None）。
    board_state: str | None = None
    #: 完工时的目标评估：True=目标已改，False=显式确认未改，None=未评估（旧记录）。
    goal_changed: bool | None = None
    #: 目标评估理由；完工时必填。
    goal_review_reason: str = ""
    #: goal_changed=True 时写入 Board.goal 的新目标。
    board_goal: str | None = None


# ---------------------------------------------------------------------------
# 纯业务关系
# ---------------------------------------------------------------------------
CARD_WAITS_FOR = LinkType(
    name="card_waits_for",
    source="Card",
    target="Card",
    cardinality="many-to-many",
    owned=True,
    description="卡片依赖的前置卡片（前置全部完成后本卡才可认领）",
)

CARD_COMMENTS = LinkType(
    name="card_comments",
    source="Card",
    target="CardComment",
    cardinality="one-to-many",
    owned=True,
    description="卡片的评论",
)

CARD_RUNS = LinkType(
    name="card_runs",
    source="Card",
    target="CardRun",
    cardinality="one-to-many",
    owned=True,
    description="卡片的历次执行尝试",
)

CARD_ASSIGNED_TO = LinkType(
    name="card_assigned_to",
    source="Card",
    target="Member",
    cardinality="many-to-one",
    owned=True,
    description="卡片当前承接人",
)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (Board, BoardColumn, Member, Card, CardComment, CardRun):
    spec.object_type(_cls)
for _link in (CARD_WAITS_FOR, CARD_COMMENTS, CARD_RUNS, CARD_ASSIGNED_TO):
    spec.add_link_type(_link)

#: 本蓝图自己的展示词条。
#: 展示层会把这些词条动态合并进全局展示字典，因此新增业务本体
#: **不需要修改任何内核文件**（``ui_display.json`` 保持 0 行 diff）。
spec.display = {
    "ontologies": {"task_board": "多智能体任务看板"},
    "categories": {"Agentic Delivery": "智能体交付"},
    "objects": {
        "Board": "看板",
        "BoardColumn": "看板列",
        "Member": "看板成员",
        "Card": "任务卡片",
        "CardComment": "卡片评论",
        "CardRun": "执行尝试",
    },
    "links": {
        "card_waits_for": "卡片前置依赖",
        "card_comments": "卡片评论",
        "card_runs": "卡片执行尝试",
        "card_assigned_to": "卡片承接人",
    },
    "actions": {
        "create_board": "创建看板",
        "update_board_state": "更新看板基线状态",
        "add_member": "新增看板成员",
        "create_card": "创建任务卡片",
        "move_card": "移动任务卡片",
        "claim_card": "认领任务卡片",
        "record_outcome": "登记执行结果",
        "unblock_card": "解除卡片阻塞",
        "promote_children": "提升子卡片",
        "add_comment": "追加卡片评论",
    },
    "functions": {
        "board_overview": "看板总览",
        "board_flow": "看板流程图",
        "card_briefing": "生成任务简报",
    },
    "statuses": {
        "todo": "待办",
        "ready": "可认领",
        "doing": "执行中",
        "done": "已完成",
        "blocked": "阻塞",
        "triage": "分诊",
        "initial": "初始列",
        "working": "执行中列",
        "completion": "完成列",
        "failure": "失败列",
        "escalation": "升级列",
        "dependency": "依赖未齐",
        "awaiting_review": "等待评审",
        "external_dependency": "外部依赖",
        "insufficient_input": "输入不足",
        "IDLE": "空闲",
        "BUSY": "忙碌",
        "UNAVAILABLE": "不可用",
        "running": "执行中",
        "done_run": "已完成",
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
        "tenant": "租户",
        "created_by": "创建人",
        "description": "说明",
        "goal": "项目目标与架构红线",
        "state": "当前基线状态",
        "unfolded_completions": "待折叠完工数",
        "board_state": "看板基线状态",
        "board_goal": "看板目标",
        "goal_changed": "目标是否变化",
        "goal_review_reason": "目标评估理由",
        "column_uid": "列编号",
        "key": "列标识",
        "label": "显示名",
        "purpose": "列用途",
        "order": "排序",
        "is_terminal": "终态列",
        "claimable": "可认领",
        "parent_gated": "父卡门控",
        "allowed_roles": "允许角色",
        "allowed_targets": "允许移动到",
        "max_retries": "最大重试",
        "on_claimed": "认领后去哪",
        "on_completed": "完成后去哪",
        "on_failed": "失败后去哪",
        "on_exhausted": "重试耗尽后去哪",
        "on_blocked": "阻塞后去哪",
        "on_abandoned": "放弃后去哪",
        "on_unblocked": "解阻后去哪",
        "member_id": "成员编号",
        "active_card_id": "在办卡片",
        "completed_cards": "已完成卡片数",
        "card_id": "卡片编号",
        "body": "正文",
        "column_key": "所在列",
        "priority": "优先级",
        "result": "结论",
        "block_kind": "阻塞原因",
        "block_recurrences": "反复阻塞次数",
        "consecutive_failures": "连续失败次数",
        "comment_id": "评论编号",
        "author": "作者",
        "created_at": "创建时间",
        "run_id": "尝试编号",
        "outcome": "执行结果",
        "evidence": "验收证据",
        "started_at": "开始时间",
        "ended_at": "结束时间",
        "cards": "卡片数",
        "subtitle": "副标题",
        "badge": "角标",
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

#: 每一块看板必须有且只有一列承担每个用途。
COLUMN_PURPOSES: tuple[str, ...] = (
    "initial",  # 卡片进入看板的落点
    "ready",  # 父卡已齐，可以认领
    "working",  # 已认领，执行中
    "completion",  # 完成（终态）
    "failure",  # 失败或阻塞
    "escalation",  # 反复失败，升级分诊
)

#: 列上的流转出口字段（一张卡在本列结束本轮工作时去哪）。
TRANSITION_FIELDS: tuple[str, ...] = (
    "on_claimed",
    "on_completed",
    "on_failed",
    "on_exhausted",
    "on_blocked",
    "on_abandoned",
    "on_unblocked",
)

BOARD_MANAGER = Actor(
    actor_id="board-manager-01",
    role="board_manager",
    clearance_tags={"*"},
)

_ALLOWED_ROLES = ("board_manager", "admin", "system")
#: 允许写 Board.goal 的既有角色边界；update_board_state 与完工改目标共用。
_GOAL_WRITER_ROLES = _ALLOWED_ROLES + ("reviewer", "coordinator")
_WORKER_ROLES = _ALLOWED_ROLES + MEMBER_ROLES


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------
class ColumnSpec(BaseModel):
    """建板时声明的一列。"""

    key: str
    label: str
    purpose: str
    order: int = 0
    is_terminal: bool = False
    claimable: bool = False
    parent_gated: bool = False
    allowed_roles: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    max_retries: int = 3
    on_claimed: str = ""
    on_completed: str = ""
    on_failed: str = ""
    on_exhausted: str = ""
    on_blocked: str = ""
    on_abandoned: str = ""
    on_unblocked: str = ""


class CreateBoardParams(BaseModel):
    board_key: str
    title: str
    tenant: str
    created_by: str
    columns: tuple[ColumnSpec, ...]
    description: str = ""
    goal: str = ""
    state: str = ""


class UpdateBoardStateParams(BaseModel):
    board_key: str
    state: str
    goal: str | None = None


class AddMemberParams(BaseModel):
    member_id: str
    name: str
    role: str


class CreateCardParams(BaseModel):
    card_id: str
    board_key: str
    title: str
    body: str = ""
    priority: int = 0
    assignee_id: str | None = None
    waits_for: tuple[str, ...] = ()


class ClaimCardParams(BaseModel):
    card_id: str
    member_id: str


class MoveCardParams(BaseModel):
    card_id: str
    target_column_key: str
    reason: str = ""


class RecordOutcomeParams(BaseModel):
    card_id: str
    member_id: str
    outcome: str
    summary: str = ""
    evidence: tuple[Evidence, ...] = ()
    block_kind: str | None = None
    artifact_uri: str = ""
    #: 完工时的新看板基线状态；completed 必填，且必须与当前 state 不同。
    board_state: str | None = None
    #: 目标评估：True=目标变化，False=显式确认未变，None=未评估（completed 拒绝）。
    goal_changed: bool | None = None
    #: 目标评估理由；completed 必填。
    goal_review_reason: str = ""
    #: goal_changed=True 时要写入 Board.goal 的新目标；False 时不得与本卡矛盾。
    board_goal: str | None = None


class UnblockCardParams(BaseModel):
    card_id: str
    reason: str = ""


class PromoteChildrenParams(BaseModel):
    card_id: str


class AddCommentParams(BaseModel):
    card_id: str
    author: str
    body: str


class BoardOverviewParams(BaseModel):
    board_key: str = ""


class CardBriefingParams(BaseModel):
    card_id: str


class BoardFlowParams(BaseModel):
    board_key: str = ""


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


def _columns_of(ctx: Any, board_key: str) -> list[BoardColumn]:
    rows = [
        row for row in ctx.query(BoardColumn) if row.board_key == board_key
    ]
    rows.sort(key=lambda row: (row.order, row.key))
    return rows


def _column_by_key(ctx: Any, board_key: str, key: str) -> BoardColumn:
    for column in _columns_of(ctx, board_key):
        if column.key == key:
            return column
    raise InvariantError(
        f"看板 {board_key!r} 没有列 {key!r}", code="COLUMN_NOT_FOUND"
    )


def _column_by_purpose(ctx: Any, board_key: str, purpose: str) -> BoardColumn:
    for column in _columns_of(ctx, board_key):
        if column.purpose == purpose:
            return column
    raise InvariantError(
        f"看板 {board_key!r} 缺少承担 {purpose!r} 用途的列",
        code="BOARD_TOPOLOGY_INCOMPLETE",
    )


def _assert_completion_requires_outcome(
    ctx: Any, board_key: str, target_key: str, *, entry: str
) -> None:
    """完成列只能由 record_outcome(completed) 在全部校验通过后进入。

    按列用途识别，不依赖硬编码的列 key，因此重命名完成列不会绕过。
    """
    completion = _column_by_purpose(ctx, board_key, "completion")
    if target_key == completion.key:
        raise InvariantError(
            f"{entry}不能把卡片直接放入完成列 {completion.key!r}；"
            "完成列只能由 record_outcome(completed) 在证据、检查点与目标评估"
            "全部通过后进入",
            code="COMPLETION_REQUIRES_RECORD_OUTCOME",
        )


def _parents_of(ctx: Any, card: Card) -> list[Card]:
    return ctx.query_linked(card, CARD_WAITS_FOR, Card)


def _children_of(ctx: Any, card: Card) -> list[Card]:
    """反向遍历：哪些卡片在等这张卡。"""
    out: list[Card] = []
    for candidate in ctx.query(Card):
        if candidate.card_id == card.card_id:
            continue
        for parent in _parents_of(ctx, candidate):
            if parent.card_id == card.card_id:
                out.append(candidate)
                break
    return out


def _runs_of(ctx: Any, card: Card) -> list[CardRun]:
    rows = ctx.query_linked(card, CARD_RUNS, CardRun)
    rows.sort(key=lambda row: row.run_id)
    return rows


def _running_run(ctx: Any, card: Card) -> CardRun | None:
    for run in _runs_of(ctx, card):
        if run.status == "running":
            return run
    return None


def _parents_satisfied(ctx: Any, card: Card) -> bool:
    """前置卡片是否全部进入终态列。"""
    for parent in _parents_of(ctx, card):
        try:
            column = _column_by_key(ctx, card.board_key, parent.column_key)
        except InvariantError:
            return False
        if not column.is_terminal:
            return False
    return True


def _claimable_column(ctx: Any, card: Card) -> BoardColumn:
    return _column_by_key(ctx, card.board_key, card.column_key)


def _next_run_id(ctx: Any, card: Card) -> str:
    return f"{card.card_id}-R{len(_runs_of(ctx, card)) + 1:02d}"


def _origin_column_key(ctx: Any, card: Card, fallback: str) -> str:
    """这张卡上一次是从哪一列被领走的（失败后退回那一列）。"""
    runs = _runs_of(ctx, card)
    if runs:
        return runs[-1].column_key
    return fallback


def _promote_ready_children(ctx: Any, card: Card) -> list[str]:
    """前置已全部完成的子卡，从初始列提升到可认领列。"""
    ready = _column_by_purpose(ctx, card.board_key, "ready")
    initial = _column_by_purpose(ctx, card.board_key, "initial")
    promoted: list[str] = []
    for child in _children_of(ctx, card):
        if child.column_key != initial.key:
            continue
        if not _parents_satisfied(ctx, child):
            continue
        ctx.modify(child, column_key=ready.key)
        promoted.append(child.card_id)
    return promoted


def _evidence_has_number(evidence: list[Evidence]) -> bool:
    return all(
        any(char.isdigit() for char in item.value) for item in evidence
    )


def _column_subtitle(column: BoardColumn) -> str:
    """把列语义拼成一句给人看的副标题（供通用流程渲染器显示）。

    领域知识留在本体里：通用渲染器只认 ``subtitle`` 字符串。
    """
    marks: list[str] = []
    if column.claimable:
        marks.append("可认领")
    if column.is_terminal:
        marks.append("终态")
    purpose_labels = {
        "initial": "初始",
        "ready": "可开工",
        "working": "执行中",
        "completion": "完成",
        "failure": "失败",
        "escalation": "升级",
    }
    head = purpose_labels.get(column.purpose, column.purpose)
    if marks:
        return f"{head} · {' · '.join(marks)}"
    return head


# ---------------------------------------------------------------------------
# 动作：建板 / 加成员
# ---------------------------------------------------------------------------
@spec.action(
    "create_board",
    CreateBoardParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="建板：声明看板的列拓扑（每列的用途与语义），一次性校验。",
)
def create_board(ctx: Any, params: CreateBoardParams) -> None:
    if ctx.get(Board, params.board_key) is not None:
        raise InvariantError(
            f"看板 {params.board_key!r} 已存在", code="BOARD_ALREADY_EXISTS"
        )
    if not params.tenant.strip():
        raise InvariantError("租户不能为空", code="EMPTY_TENANT")

    # ---- 拓扑校验：每个用途有且只有一列 ----
    declared_keys = {column.key for column in params.columns}
    seen: dict[str, str] = {}
    for column in params.columns:
        if column.purpose not in COLUMN_PURPOSES:
            raise InvariantError(
                f"列 {column.key!r} 的用途 {column.purpose!r} 非法",
                code="INVALID_COLUMN_PURPOSE",
            )
        if column.purpose in seen:
            raise InvariantError(
                f"用途 {column.purpose!r} 有两列（{seen[column.purpose]!r} 与 "
                f"{column.key!r}），看板拓扑不允许歧义",
                code="BOARD_TOPOLOGY_AMBIGUOUS",
            )
        if column.max_retries < 1:
            raise InvariantError(
                f"列 {column.key!r} 的 max_retries 必须 >= 1",
                code="INVALID_MAX_RETRIES",
            )
        for target in column.allowed_targets:
            if target not in declared_keys:
                raise InvariantError(
                    f"列 {column.key!r} 允许移动到不存在的列 {target!r}",
                    code="BOARD_TOPOLOGY_INVALID",
                )
            if target == column.key:
                raise InvariantError(
                    f"列 {column.key!r} 不能允许移动到自己",
                    code="BOARD_TOPOLOGY_INVALID",
                )
        for field_name in TRANSITION_FIELDS:
            target = getattr(column, field_name)
            if not target:
                continue
            if target not in declared_keys:
                raise InvariantError(
                    f"列 {column.key!r} 的 {field_name} 指向不存在的列 {target!r}",
                    code="BOARD_TOPOLOGY_INVALID",
                )
            if target == column.key:
                raise InvariantError(
                    f"列 {column.key!r} 的 {field_name} 不能指向自己",
                    code="BOARD_TOPOLOGY_INVALID",
                )
        if column.claimable and not column.allowed_roles:
            raise InvariantError(
                f"可认领列 {column.key!r} 必须声明允许的角色",
                code="COLUMN_MISSING_ALLOWED_ROLES",
            )
        for role in column.allowed_roles:
            if role not in MEMBER_ROLES:
                raise InvariantError(
                    f"列 {column.key!r} 声明了非法角色 {role!r}",
                    code="INVALID_MEMBER_ROLE",
                )
        seen[column.purpose] = column.key

    missing = [p for p in COLUMN_PURPOSES if p not in seen]
    if missing:
        raise InvariantError(
            f"看板拓扑缺少用途 {missing}", code="BOARD_TOPOLOGY_INCOMPLETE"
        )
    if not seen["completion"]:
        raise InvariantError(
            "看板拓扑缺少完成列", code="BOARD_TOPOLOGY_INCOMPLETE"
        )

    by_purpose = {c.purpose: c for c in params.columns}
    if not by_purpose["completion"].is_terminal:
        raise InvariantError(
            "完成列必须标记为终态列", code="BOARD_TOPOLOGY_INVALID"
        )
    if not by_purpose["ready"].claimable:
        raise InvariantError(
            "可认领列必须标记为可认领", code="BOARD_TOPOLOGY_INVALID"
        )
    if by_purpose["working"].claimable:
        raise InvariantError(
            "执行中列不能同时可认领", code="BOARD_TOPOLOGY_INVALID"
        )
    # 流转出口必须把工作列导向终态，否则卡片永远收不了尾。
    working = by_purpose["working"]
    completion_target = working.on_completed or by_purpose["completion"].key
    if completion_target not in declared_keys:
        raise InvariantError(
            f"执行中列的完成出口 {completion_target!r} 不存在",
            code="BOARD_TOPOLOGY_INVALID",
        )
    if not next(
        (c for c in params.columns if c.key == completion_target), None
    ).is_terminal:
        raise InvariantError(
            f"执行中列的完成出口 {completion_target!r} 不是终态列",
            code="BOARD_TOPOLOGY_INVALID",
        )

    ctx.create(
        Board(
            board_key=params.board_key,
            title=params.title,
            tenant=params.tenant,
            created_by=params.created_by,
            description=params.description,
            goal=params.goal,
            state=params.state,
            unfolded_completions=0,
        )
    )
    for column in sorted(params.columns, key=lambda c: (c.order, c.key)):
        ctx.create(
            BoardColumn(
                column_uid=f"{params.board_key}:{column.key}",
                board_key=params.board_key,
                key=column.key,
                label=column.label,
                tenant=params.tenant,
                purpose=column.purpose,
                order=column.order,
                is_terminal=column.is_terminal,
                claimable=column.claimable,
                parent_gated=column.parent_gated,
                allowed_roles=column.allowed_roles,
                allowed_targets=column.allowed_targets,
                max_retries=column.max_retries,
                on_claimed=column.on_claimed,
                on_completed=column.on_completed,
                on_failed=column.on_failed,
                on_exhausted=column.on_exhausted,
                on_blocked=column.on_blocked,
                on_abandoned=column.on_abandoned,
                on_unblocked=column.on_unblocked,
            )
        )


@spec.action(
    "update_board_state",
    UpdateBoardStateParams,
    allowed_roles=_GOAL_WRITER_ROLES,
    affects_source=False,
    description=(
        "折叠看板基线状态：把已发生的历史压成一句「现在什么算数」，"
        "并清空待折叠完工计数；可一并修订项目目标与架构红线。"
    ),
)
def update_board_state(ctx: Any, params: UpdateBoardStateParams) -> None:
    board = _require_board(ctx, params.board_key)
    if not params.state.strip():
        raise InvariantError(
            "看板当前基线状态不能为空", code="EMPTY_BOARD_STATE"
        )
    if params.goal is not None:
        ctx.modify(board, goal=params.goal)
    ctx.modify(board, state=params.state, unfolded_completions=0)


@spec.action(
    "add_member",
    AddMemberParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="新增看板成员（开发者 / 评审 / 测试 / 统筹）。",
)
def add_member(ctx: Any, params: AddMemberParams) -> None:
    if params.role not in MEMBER_ROLES:
        raise InvariantError(
            f"非法角色 {params.role!r}，只允许 {list(MEMBER_ROLES)}",
            code="INVALID_MEMBER_ROLE",
        )
    if ctx.get(Member, params.member_id) is not None:
        raise InvariantError(
            f"成员 {params.member_id!r} 已存在", code="MEMBER_ALREADY_EXISTS"
        )
    ctx.create(
        Member(
            member_id=params.member_id,
            name=params.name,
            role=params.role,
        )
    )


# ---------------------------------------------------------------------------
# 动作：建卡
# ---------------------------------------------------------------------------
@spec.action(
    "create_card",
    CreateCardParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="建卡：落到初始列；带前置依赖的卡片等待前置全部完成。",
)
def create_card(ctx: Any, params: CreateCardParams) -> None:
    board = _require_board(ctx, params.board_key)
    if ctx.get(Card, params.card_id) is not None:
        raise InvariantError(
            f"卡片 {params.card_id!r} 已存在", code="CARD_ALREADY_EXISTS"
        )
    if not params.title.strip():
        raise InvariantError("卡片标题不能为空", code="EMPTY_CARD_TITLE")
    if params.assignee_id is not None:
        _require_member(ctx, params.assignee_id)

    initial = _column_by_purpose(ctx, board.board_key, "initial")
    ready = _column_by_purpose(ctx, board.board_key, "ready")

    parents: list[Card] = []
    for parent_id in params.waits_for:
        if parent_id == params.card_id:
            raise InvariantError(
                "卡片不能依赖自己", code="CARD_DEPENDS_ON_ITSELF"
            )
        parent = ctx.get(Card, parent_id)
        if parent is None:
            raise InvariantError(
                f"前置卡片 {parent_id!r} 不存在", code="PARENT_NOT_FOUND"
            )
        if parent.board_key != board.board_key:
            raise InvariantError(
                f"前置卡片 {parent_id!r} 不在同一块看板上",
                code="PARENT_CROSS_BOARD",
            )
        parents.append(parent)

    # 没有前置依赖的卡片直接可认领；有前置的先停在初始列等待提升。
    landing = ready if not parents else initial

    card = ctx.create(
        Card(
            card_id=params.card_id,
            board_key=board.board_key,
            tenant=board.tenant,
            title=params.title,
            body=params.body,
            column_key=landing.key,
            priority=params.priority,
            assignee_id=params.assignee_id,
            max_retries=landing.max_retries,
        )
    )
    for parent in parents:
        ctx.link(card, CARD_WAITS_FOR, parent)

    _assert_no_dependency_cycle(ctx, card)


def _assert_no_dependency_cycle(ctx: Any, card: Card) -> None:
    """新建依赖后不得成环。"""
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
# 动作：人工移动卡片
# ---------------------------------------------------------------------------
@spec.action(
    "move_card",
    MoveCardParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="人工把卡片移到另一列；允许的目标列由源列的语义声明。",
)
def move_card(ctx: Any, params: MoveCardParams) -> None:
    card = _require_card(ctx, params.card_id)
    source = _column_by_key(ctx, card.board_key, card.column_key)
    target = _column_by_key(ctx, card.board_key, params.target_column_key)

    if target.key not in source.allowed_targets:
        raise InvariantError(
            f"列 {source.key!r} 不允许把卡片移动到 {target.key!r}",
            code="MANUAL_MOVE_NOT_ALLOWED",
        )
    _assert_completion_requires_outcome(
        ctx, card.board_key, target.key, entry="人工移动"
    )
    if card.assignee_id is not None:
        raise InvariantError(
            f"卡片 {card.card_id!r} 已被 {card.assignee_id!r} 认领，不能人工移动",
            code="CARD_ALREADY_CLAIMED",
        )
    if target.parent_gated and not _parents_satisfied(ctx, card):
        raise InvariantError(
            f"卡片 {card.card_id!r} 的前置尚未全部完成，不能进入 {target.key!r}",
            code="PARENTS_NOT_SATISFIED",
        )
    ctx.modify(card, column_key=target.key, max_retries=target.max_retries)


# ---------------------------------------------------------------------------
# 动作：认领
# ---------------------------------------------------------------------------
@spec.action(
    "claim_card",
    ClaimCardParams,
    allowed_roles=_WORKER_ROLES,
    affects_source=False,
    description="认领卡片：列必须可认领、角色必须被该列允许、前置必须全部完成。",
)
def claim_card(ctx: Any, params: ClaimCardParams) -> None:
    card = _require_card(ctx, params.card_id)
    member = _require_member(ctx, params.member_id)

    column = _claimable_column(ctx, card)
    if card.assignee_id is not None:
        raise InvariantError(
            f"卡片 {card.card_id!r} 已被 {card.assignee_id!r} 认领",
            code="CARD_ALREADY_CLAIMED",
        )
    if not column.claimable:
        raise InvariantError(
            f"列 {column.key!r} 不可认领", code="CARD_NOT_CLAIMABLE"
        )
    if member.role not in column.allowed_roles:
        raise InvariantError(
            f"角色 {member.role!r} 不能认领列 {column.key!r}",
            code="ROLE_NOT_ALLOWED_FOR_COLUMN",
        )
    if member.availability != "IDLE":
        raise InvariantError(
            f"成员 {member.member_id} 当前 {member.availability}，无法接单",
            code="MEMBER_NOT_IDLE",
        )
    if not _parents_satisfied(ctx, card):
        raise InvariantError(
            f"卡片 {card.card_id!r} 的前置尚未全部完成",
            code="PARENTS_NOT_SATISFIED",
        )

    target_key = column.on_claimed or _column_by_purpose(
        ctx, card.board_key, "working"
    ).key
    _assert_completion_requires_outcome(
        ctx, card.board_key, target_key, entry="认领流转"
    )
    ctx.modify(
        card,
        column_key=target_key,
        assignee_id=member.member_id,
    )
    ctx.modify(
        member,
        availability="BUSY",
        active_card_id=card.card_id,
    )
    ctx.link(card, CARD_ASSIGNED_TO, member)

    run = ctx.create(
        CardRun(
            run_id=_next_run_id(ctx, card),
            card_id=card.card_id,
            tenant=card.tenant,
            member_id=member.member_id,
            column_key=column.key,
            status="running",
            started_at=int(time.time()),
        )
    )
    ctx.link(card, CARD_RUNS, run)


# ---------------------------------------------------------------------------
# 动作：登记结果
# ---------------------------------------------------------------------------
@spec.action(
    "record_outcome",
    RecordOutcomeParams,
    allowed_roles=_WORKER_ROLES,
    affects_source=False,
    description=(
        "登记一次执行尝试的结果：completed / blocked / failed / abandoned。"
        "完成必须有前置齐备、带数字的证据、新的看板基线状态，"
        "以及显式的目标评估与理由。"
    ),
)
def record_outcome(ctx: Any, params: RecordOutcomeParams) -> None:
    card = _require_card(ctx, params.card_id)
    member = _require_member(ctx, params.member_id)

    if params.outcome not in OUTCOMES:
        raise InvariantError(
            f"非法结果 {params.outcome!r}，只允许 {list(OUTCOMES)}",
            code="INVALID_OUTCOME",
        )
    if card.assignee_id != member.member_id:
        raise InvariantError(
            f"卡片 {card.card_id!r} 当前承接人是 {card.assignee_id!r}，"
            f"{member.member_id!r} 无法登记结果",
            code="CARD_NOT_CLAIMED_BY_MEMBER",
        )
    run = _running_run(ctx, card)
    if run is None:
        raise InvariantError(
            f"卡片 {card.card_id!r} 没有在途的执行尝试",
            code="NO_RUNNING_ATTEMPT",
        )

    # 流转出口是**数据**：本轮工作在执行中列，就由执行中列声明去哪；
    # 没声明时回落到被认领的那一列，再回落到用途推断。
    current = _column_by_key(ctx, card.board_key, card.column_key)
    origin_key = _origin_column_key(ctx, card, card.column_key)
    origin = _column_by_key(ctx, card.board_key, origin_key)

    def route(field_name: str, fallback: str = "") -> str:
        return (
            getattr(current, field_name)
            or getattr(origin, field_name)
            or fallback
        )

    completion: dict[str, Any] | None = None
    if params.outcome == "completed":
        if not _parents_satisfied(ctx, card):
            raise InvariantError(
                f"卡片 {card.card_id!r} 的前置尚未全部完成，不能收单",
                code="PARENTS_NOT_SATISFIED",
            )
        if not params.evidence:
            raise InvariantError(
                f"卡片 {card.card_id!r} 收单必须提供验收证据",
                code="MISSING_EVIDENCE",
            )
        for item in params.evidence:
            if not item.metric.strip() or not item.value.strip():
                raise InvariantError(
                    "验收证据的指标与取值都不能为空",
                    code="INVALID_EVIDENCE",
                )
        if not _evidence_has_number(list(params.evidence)):
            raise InvariantError(
                "验收证据必须包含实际数字（测试条数、覆盖格数……）",
                code="EVIDENCE_WITHOUT_NUMBER",
            )
        # 完工必须显式交接新的看板基线，并对目标是否变化作出判断。
        # 这里只校验契约是否被明确履行，不替调用方推断自然语言摘要的语义。
        board = _require_board(ctx, card.board_key)
        new_state = (params.board_state or "").strip()
        if not new_state:
            raise InvariantError(
                f"卡片 {card.card_id!r} 完工必须提供新的看板基线状态 board_state",
                code="MISSING_BOARD_STATE",
            )
        if new_state == board.state.strip():
            raise InvariantError(
                f"卡片 {card.card_id!r} 完工必须更新看板基线状态，不能原样提交",
                code="UNCHANGED_BOARD_STATE",
            )
        if params.goal_changed is None:
            raise InvariantError(
                f"卡片 {card.card_id!r} 完工必须显式评估目标是否变化 "
                "(goal_changed=true/false)",
                code="MISSING_GOAL_ASSESSMENT",
            )
        review_reason = params.goal_review_reason.strip()
        if not review_reason:
            raise InvariantError(
                f"卡片 {card.card_id!r} 完工必须给出目标评估理由",
                code="MISSING_GOAL_REVIEW_REASON",
            )
        new_goal: str | None = None
        if params.goal_changed:
            # 改目标是既有目标写入权限，不因“完工”动作对工人角色扩权。
            if ctx.actor.role not in _GOAL_WRITER_ROLES:
                raise InvariantError(
                    f"角色 {ctx.actor.role!r} 无权在完工时修改看板目标",
                    code="GOAL_WRITE_NOT_AUTHORIZED",
                )
            candidate = (params.board_goal or "").strip()
            if not candidate:
                raise InvariantError(
                    "目标已变化时必须提供新的 board_goal",
                    code="MISSING_BOARD_GOAL",
                )
            if candidate == board.goal.strip():
                raise InvariantError(
                    "目标已变化时必须提供与现有目标不同的 board_goal",
                    code="UNCHANGED_BOARD_GOAL",
                )
            new_goal = candidate
        else:
            supplied = (params.board_goal or "").strip()
            if supplied and supplied != board.goal.strip():
                raise InvariantError(
                    "goal_changed=false 时不得提交与现有目标不同的 board_goal",
                    code="GOAL_CHANGED_CONTRADICTION",
                )
        completion = {
            "board": board,
            "state": new_state,
            "goal_changed": params.goal_changed,
            "goal_review_reason": review_reason,
            "board_goal": new_goal,
        }
    if params.outcome == "blocked":
        if params.block_kind not in BLOCK_KINDS:
            raise InvariantError(
                f"阻塞必须声明阻塞原因，取值 {list(BLOCK_KINDS)}",
                code="INVALID_BLOCK_KIND",
            )

    # 非完成结果也走数据声明出口；任何出口落到完成列都必须在任何
    # 状态变更之前拒绝，避免用失败/阻塞/放弃绕开完工检查点。
    failed_target: str | None = None
    failures = card.consecutive_failures
    blocked_target: str | None = None
    abandoned_target: str | None = None
    if params.outcome == "failed":
        failures += 1
        if failures > card.max_retries:
            failed_target = route(
                "on_exhausted",
                _column_by_purpose(ctx, card.board_key, "failure").key,
            )
        else:
            failed_target = route("on_failed", origin_key)
        _assert_completion_requires_outcome(
            ctx, card.board_key, failed_target, entry="失败流转"
        )
    elif params.outcome == "blocked":
        if params.block_kind == "dependency":
            # 依赖未齐的卡回待办列等前置，不占用阻塞列。
            blocked_target = _column_by_purpose(ctx, card.board_key, "initial").key
        else:
            blocked_target = route(
                "on_blocked",
                _column_by_purpose(ctx, card.board_key, "failure").key,
            )
        _assert_completion_requires_outcome(
            ctx, card.board_key, blocked_target, entry="阻塞流转"
        )
    elif params.outcome == "abandoned":
        abandoned_target = route(
            "on_abandoned",
            _column_by_purpose(ctx, card.board_key, "escalation").key,
        )
        _assert_completion_requires_outcome(
            ctx, card.board_key, abandoned_target, entry="放弃流转"
        )

    now = int(time.time())
    run_updates: dict[str, Any] = {
        "status": {
            "completed": "done",
            "blocked": "blocked",
            "failed": "failed",
            "abandoned": "abandoned",
        }[params.outcome],
        "outcome": params.outcome,
        "summary": params.summary,
        "evidence": list(params.evidence),
        "ended_at": now,
    }
    if completion is not None:
        run_updates.update(
            board_state=completion["state"],
            goal_changed=completion["goal_changed"],
            goal_review_reason=completion["goal_review_reason"],
            board_goal=completion["board_goal"],
        )
    ctx.modify(run, **run_updates)

    ctx.modify(
        member,
        availability="IDLE",
        active_card_id=None,
    )
    ctx.unlink(card, CARD_ASSIGNED_TO, member)
    ctx.modify(card, assignee_id=None)

    if params.outcome == "completed":
        target = route(
            "on_completed",
            _column_by_purpose(ctx, card.board_key, "completion").key,
        )
        ctx.modify(
            card,
            column_key=target,
            result=params.summary,
            artifact_uri=params.artifact_uri,
            consecutive_failures=0,
            block_kind=None,
        )
        ctx.modify(member, completed_cards=member.completed_cards + 1)
        _promote_ready_children(ctx, card)
        # 收单与看板折叠在同一动作事务里：新基线必然落盘，待折叠数清零；
        # 目标有变化时同一事务写入新目标。
        assert completion is not None
        board_updates: dict[str, Any] = {
            "state": completion["state"],
            "unfolded_completions": 0,
        }
        if completion["board_goal"] is not None:
            board_updates["goal"] = completion["board_goal"]
        ctx.modify(completion["board"], **board_updates)
        return

    if params.outcome == "failed":
        assert failed_target is not None
        ctx.modify(
            card,
            column_key=failed_target,
            consecutive_failures=failures,
        )
        return

    if params.outcome == "blocked":
        assert blocked_target is not None
        ctx.modify(
            card,
            column_key=blocked_target,
            block_kind=params.block_kind,
        )
        return

    assert abandoned_target is not None
    ctx.modify(card, column_key=abandoned_target)


# ---------------------------------------------------------------------------
# 动作：解阻 / 提升子卡 / 评论
# ---------------------------------------------------------------------------
@spec.action(
    "unblock_card",
    UnblockCardParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="解除阻塞；反复阻塞同一张卡超过上限则升级分诊。",
)
def unblock_card(ctx: Any, params: UnblockCardParams) -> None:
    card = _require_card(ctx, params.card_id)
    if card.block_kind is None:
        raise InvariantError(
            f"卡片 {card.card_id!r} 当前没有阻塞标记",
            code="CARD_NOT_BLOCKED",
        )
    failure = _column_by_purpose(ctx, card.board_key, "failure")
    recurrences = card.block_recurrences + 1
    if recurrences > failure.max_retries:
        escalation = _column_by_purpose(ctx, card.board_key, "escalation")
        ctx.modify(
            card,
            column_key=escalation.key,
            block_kind=None,
            block_recurrences=recurrences,
        )
        return
    target = failure.on_unblocked or _column_by_purpose(
        ctx, card.board_key, "ready"
    ).key
    _assert_completion_requires_outcome(
        ctx, card.board_key, target, entry="解阻流转"
    )
    ctx.modify(
        card,
        column_key=target,
        block_kind=None,
        block_recurrences=recurrences,
    )


@spec.action(
    "promote_children",
    PromoteChildrenParams,
    allowed_roles=_ALLOWED_ROLES,
    affects_source=False,
    description="把前置已全部完成的子卡从初始列提升到可认领列。",
)
def promote_children(ctx: Any, params: PromoteChildrenParams) -> None:
    card = _require_card(ctx, params.card_id)
    _promote_ready_children(ctx, card)


@spec.action(
    "add_comment",
    AddCommentParams,
    allowed_roles=_WORKER_ROLES,
    affects_source=False,
    description="给卡片追加一条评论。",
)
def add_comment(ctx: Any, params: AddCommentParams) -> None:
    card = _require_card(ctx, params.card_id)
    if not params.body.strip():
        raise InvariantError("评论内容不能为空", code="EMPTY_COMMENT_BODY")
    comment = ctx.create(
        CardComment(
            comment_id=f"{card.card_id}-C{len(ctx.query_linked(card, CARD_COMMENTS, CardComment)) + 1:02d}",
            card_id=card.card_id,
            tenant=card.tenant,
            author=params.author,
            body=params.body,
            created_at=int(time.time()),
        )
    )
    ctx.link(card, CARD_COMMENTS, comment)


# ---------------------------------------------------------------------------
# 只读看板与任务简报
# ---------------------------------------------------------------------------
@spec.function(
    "board_overview",
    BoardOverviewParams,
    description="看板总览：列拓扑（用途与语义）与各列卡片分布；不给看板编号时返回全部看板。",
)
def board_overview(ctx: Any, params: BoardOverviewParams) -> dict[str, Any]:
    if not params.board_key.strip():
        boards = sorted(ctx.query(Board), key=lambda row: row.board_key)
        return {
            "boards": [_overview_of(ctx, board) for board in boards],
            "boards_total": len(boards),
        }
    return _overview_of(ctx, _require_board(ctx, params.board_key))


def _overview_of(ctx: Any, board: Board) -> dict[str, Any]:
    columns = _columns_of(ctx, board.board_key)
    cards = [row for row in ctx.query(Card) if row.board_key == board.board_key]

    by_column: dict[str, int] = {column.key: 0 for column in columns}
    for card in cards:
        by_column[card.column_key] = by_column.get(card.column_key, 0) + 1

    return {
        "board_key": board.board_key,
        "title": board.title,
        "tenant": board.tenant,
        "goal": board.goal,
        "state": board.state,
        "unfolded_completions": board.unfolded_completions,
        "cards_total": len(cards),
        "cards_summary": [
            {
                "card_id": card.card_id,
                "title": card.title,
                "column_key": card.column_key,
                "assignee_id": card.assignee_id,
                "result": card.result,
            }
            for card in sorted(cards, key=lambda row: row.card_id)
        ],
        "columns": [
            {
                "key": column.key,
                "label": column.label,
                "purpose": column.purpose,
                "order": column.order,
                "is_terminal": column.is_terminal,
                "claimable": column.claimable,
                "parent_gated": column.parent_gated,
                "allowed_roles": list(column.allowed_roles),
                "max_retries": column.max_retries,
                "cards": by_column.get(column.key, 0),
                **{field_name: getattr(column, field_name) for field_name in TRANSITION_FIELDS},
            }
            for column in columns
        ],
    }


@spec.function(
    "board_flow",
    BoardFlowParams,
    description="看板流程图：节点（列）与边（流转出口），供指挥台画流程。",
)
def board_flow(ctx: Any, params: BoardFlowParams) -> dict[str, Any]:
    """把列拓扑摊平成节点 + 边，供任何通用流程渲染器使用。"""
    if params.board_key.strip():
        boards = [_require_board(ctx, params.board_key)]
    else:
        boards = sorted(ctx.query(Board), key=lambda row: row.board_key)

    flows: list[dict[str, Any]] = []
    for board in boards:
        columns = _columns_of(ctx, board.board_key)
        cards = [
            row for row in ctx.query(Card) if row.board_key == board.board_key
        ]
        counts: dict[str, int] = {column.key: 0 for column in columns}
        for card in cards:
            counts[card.column_key] = counts.get(card.column_key, 0) + 1

        nodes = [
            {
                # 通用视觉协议：id / label / subtitle / badge / order
                "id": column.key,
                "label": column.label,
                "subtitle": _column_subtitle(column),
                "badge": counts.get(column.key, 0),
                "order": column.order,
                # 保留原有字段，兼容既有单元测试与其它消费者。
                "purpose": column.purpose,
                "cards": counts.get(column.key, 0),
                "is_terminal": column.is_terminal,
                "claimable": column.claimable,
            }
            for column in columns
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

        for column in columns:
            add_edge(column.key, column.on_claimed, "认领")
            add_edge(column.key, column.on_completed, "完成")
            add_edge(column.key, column.on_failed, "失败重试")
            add_edge(column.key, column.on_exhausted, "重试耗尽")
            add_edge(column.key, column.on_blocked, "阻塞")
            add_edge(column.key, column.on_abandoned, "放弃")
            add_edge(column.key, column.on_unblocked, "解阻")
            for target in column.allowed_targets:
                add_edge(column.key, target, "人工移动")
        # 依赖未齐的阻塞回到待办列。
        initial = next(
            (c for c in columns if c.purpose == "initial"), None
        )
        if initial is not None:
            for column in columns:
                if column.on_blocked:
                    add_edge(column.key, initial.key, "依赖阻塞")

        flows.append(
            {
                "board_key": board.board_key,
                "title": board.title,
                "nodes": nodes,
                "edges": edges,
            }
        )

    if params.board_key.strip():
        return flows[0]
    return {"flows": flows, "flows_total": len(flows)}


@spec.function(
    "card_briefing",
    CardBriefingParams,
    description="结构化任务简报：卡片事实 + 列语义 + 前置 + 历次尝试与证据。",
)
def card_briefing(ctx: Any, params: CardBriefingParams) -> dict[str, Any]:
    card = _require_card(ctx, params.card_id)
    column = _column_by_key(ctx, card.board_key, card.column_key)
    board = _require_board(ctx, card.board_key)

    parents = _parents_of(ctx, card)
    children = _children_of(ctx, card)
    runs = _runs_of(ctx, card)
    comments = ctx.query_linked(card, CARD_COMMENTS, CardComment)

    return {
        "card_id": card.card_id,
        "board_key": card.board_key,
        "board_title": board.title,
        "board_goal": board.goal,
        "board_state": board.state,
        "unfolded_completions": board.unfolded_completions,
        "title": card.title,
        "body": card.body,
        "column": {
            "key": column.key,
            "label": column.label,
            "purpose": column.purpose,
            "claimable": column.claimable,
            "allowed_roles": list(column.allowed_roles),
        },
        "priority": card.priority,
        "assignee_id": card.assignee_id,
        "consecutive_failures": card.consecutive_failures,
        "max_retries": card.max_retries,
        "block_kind": card.block_kind,
        "block_recurrences": card.block_recurrences,
        "result": card.result,
        "parents": [
            {
                "card_id": parent.card_id,
                "title": parent.title,
                "column_key": parent.column_key,
                "satisfied": _parents_satisfied(ctx, parent),
            }
            for parent in parents
        ],
        "children": [
            {"card_id": child.card_id, "title": child.title}
            for child in children
        ],
        "runs": [
            {
                "run_id": run.run_id,
                "member_id": run.member_id,
                "column_key": run.column_key,
                "status": run.status,
                "outcome": run.outcome,
                "summary": run.summary,
                "evidence": [
                    {"metric": item.metric, "value": item.value, "detail": item.detail}
                    for item in run.evidence
                ],
            }
            for run in runs
        ],
        "comments": [
            {"comment_id": item.comment_id, "author": item.author, "body": item.body}
            for item in comments
        ],
    }


def render_card_briefing(briefing: dict[str, Any]) -> str:
    """把结构化简报渲染成纯文本，供任意执行机械臂直接消费。"""
    lines = [
        f"卡片：{briefing['card_id']} · {briefing['title']}",
        f"看板：{briefing['board_title']}（{briefing['board_key']}）",
        f"当前列：{briefing['column']['label']}"
        f"（{briefing['column']['purpose']}）",
    ]
    if briefing.get("body"):
        lines.append(f"任务说明：{briefing['body']}")
    if briefing.get("board_goal"):
        goal_text = str(briefing["board_goal"])
        if len(goal_text) > 600:
            goal_text = goal_text[:600] + " ...[完整架构规范见 docs/ARCHITECTURE.md]"
        lines.append(f"项目目标与架构红线：{goal_text}")
    if briefing.get("board_state"):
        state_text = str(briefing["board_state"])
        if len(state_text) > 600:
            state_text = state_text[:600] + " ...[完整状态基线见 _state.md]"
        lines.append(f"当前基线状态：{state_text}")
    if briefing.get("parents"):
        lines.append("前置卡片：")
        for parent in briefing["parents"]:
            mark = "已满足" if parent["satisfied"] else "未完成"
            lines.append(
                f"  - {parent['card_id']} {parent['title']} [{mark}]"
            )
    if briefing.get("runs"):
        lines.append("历次尝试：")
        for run in briefing["runs"]:
            lines.append(
                f"  - {run['run_id']} {run['member_id']} "
                f"[{run['outcome'] or run['status']}] {run['summary']}"
            )
            for item in run["evidence"]:
                lines.append(f"      · {item['metric']}: {item['value']}")
    return "\n".join(lines)


def render_board_handover(overview: dict[str, Any]) -> str:
    """把 ``board_overview`` 的结果渲染成交接简报文本。

    新一代架构师上任时最需要三件事：这块板为什么存在（``goal``）、
    现在什么算数（``state``）、还有多少完工没被折叠（``unfolded_completions``）。
    传整块总览（含 ``boards``）时取第一块板。
    """
    boards = overview.get("boards")
    if isinstance(boards, list):
        if not boards:
            return "看板交接：当前没有任何看板。"
        overview = boards[0]

    lines = [
        f"看板交接：{overview.get('title', '')}（{overview.get('board_key', '')}）",
    ]
    goal = str(overview.get("goal") or "").strip()
    if goal:
        lines.append(f"项目目标与架构红线：{goal}")
    state = str(overview.get("state") or "").strip()
    if state:
        lines.append(f"当前基线状态：{state}")
    lines.append(f"待折叠完工数：{overview.get('unfolded_completions', 0)}")
    lines.append(f"卡片总数：{overview.get('cards_total', 0)}")

    columns = overview.get("columns") or []
    if columns:
        lines.append("卡片分布：")
        for column in columns:
            lines.append(
                f"  - {column.get('label', '')}（{column.get('key', '')}）："
                f"{column.get('cards', 0)} 张"
            )

    cards = overview.get("cards_summary") or []
    in_flight = [card for card in cards if card.get("assignee_id")]
    if in_flight:
        lines.append("在途卡片：")
        for card in in_flight:
            lines.append(
                f"  - {card.get('card_id', '')} {card.get('title', '')}"
                f" @ {card.get('assignee_id', '')}"
                f"（{card.get('column_key', '')}）"
            )
    return "\n".join(lines)
