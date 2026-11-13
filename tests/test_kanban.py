"""TK-ONTO-021 验收测试：业务纯洁任务看板本体（TaskBoardOntology）。

覆盖：
1. ``kanban.py`` 的 100% 业务纯洁性静态审计（源码 + AST 导入 + 运行时表面）；
2. 看板拓扑声明与校验（用途齐备、无歧义、终态列、可认领列必须声明角色、
   允许移动的目标列必须存在）；
3. 建卡与前置依赖护栏；
4. 认领护栏（列语义 + 角色 + 忙闲 + 前置门控）；
5. 登记结果护栏（收单必须有带数字的证据；失败重试熔断）；
6. 阻塞 / 解阻 / 反复阻塞升级分诊；
7. 前置完成后子卡自动提升；
8. 业务动作账本留痕（每一步都有审计记录）；
9. 租户行级安全隔离；
10. 只读看板与任务简报。
"""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest

from open_ontology import (
    Actor,
    InvariantError,
    ObjectNotFoundError,
    OntologyRuntime,
    PermissionDeniedError,
)
from open_ontology.kanban import (
    CARD_ASSIGNED_TO,
    CARD_COMMENTS,
    CARD_RUNS,
    CARD_WAITS_FOR,
    Board,
    Card,
    CardComment,
    CardRun,
    Member,
    render_board_handover,
    render_card_briefing,
    spec,
)

ROOT = Path(__file__).resolve().parents[1]

MANAGER = Actor(
    actor_id="board-manager-01",
    role="board_manager",
    clearance_tags={"*"},
)

_RUN_IDS = itertools.count(1)


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------
@pytest.fixture()
def board() -> OntologyRuntime:
    runtime = OntologyRuntime(spec)
    yield runtime
    runtime.close()


def act(runtime: OntologyRuntime, action: str, params: dict) -> dict:
    return runtime.submit_action(
        f"TEST-{next(_RUN_IDS):05d}", action, params, MANAGER
    )


def columns() -> list[dict]:
    return [
        {
            "key": "todo",
            "label": "待办",
            "purpose": "initial",
            "order": 0,
            "allowed_targets": ("ready", "blocked"),
        },
        {
            "key": "ready",
            "label": "可认领",
            "purpose": "ready",
            "order": 1,
            "claimable": True,
            "allowed_roles": ("developer", "tester"),
            "allowed_targets": ("todo", "blocked"),
            "on_claimed": "doing",
        },
        {
            "key": "doing",
            "label": "执行中",
            "purpose": "working",
            "order": 2,
            "on_completed": "done",
            "on_failed": "ready",
            "on_exhausted": "blocked",
            "on_blocked": "blocked",
            "on_abandoned": "triage",
        },
        {
            "key": "done",
            "label": "已完成",
            "purpose": "completion",
            "order": 3,
            "is_terminal": True,
            "allowed_targets": ("todo",),
        },
        {
            "key": "blocked",
            "label": "阻塞",
            "purpose": "failure",
            "order": 4,
            "max_retries": 2,
            "allowed_targets": ("ready", "todo"),
            "on_unblocked": "ready",
        },
        {
            "key": "triage",
            "label": "分诊",
            "purpose": "escalation",
            "order": 5,
            "allowed_targets": ("todo",),
        },
    ]


def make_board(
    runtime: OntologyRuntime,
    board_key: str = "BD-1",
    tenant: str = "acme",
    column_specs: list[dict] | None = None,
) -> None:
    act(
        runtime,
        "create_board",
        {
            "board_key": board_key,
            "title": "研发看板",
            "tenant": tenant,
            "created_by": "board-manager-01",
            "columns": column_specs if column_specs is not None else columns(),
        },
    )


def add_member(
    runtime: OntologyRuntime, member_id: str, role: str = "developer"
) -> None:
    act(
        runtime,
        "add_member",
        {"member_id": member_id, "name": member_id, "role": role},
    )


def card_of(runtime: OntologyRuntime, card_id: str) -> Card:
    return runtime.get_object(Card, card_id, MANAGER)


def make_card(
    runtime: OntologyRuntime, card_id: str, title: str = "写内核", **extra
) -> None:
    act(
        runtime,
        "create_card",
        {"card_id": card_id, "board_key": "BD-1", "title": title, **extra},
    )


# ---------------------------------------------------------------------------
# 1. 业务纯洁性静态审计
# ---------------------------------------------------------------------------
FORBIDDEN_SUBSTRINGS = (
    "opencode",
    "codebuddy",
    "hermes",
    "session_id",
    "http",
    "subprocess",
    "rate_limit",
    "urllib",
    "socket",
    "requests",
)
FORBIDDEN_WORDS = ("port", "cli", "api")
ALLOWED_IMPORTS = {"__future__", "typing", "pydantic", "spec", "time"}


def test_business_purity_static_scan():
    path = ROOT / "open_ontology" / "kanban.py"
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()

    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in lowered, f"{token!r} leaked into kanban.py"
    for token in FORBIDDEN_WORDS:
        assert not re.search(rf"(?<![a-z]){token}(?![a-z])", lowered), (
            f"word {token!r} leaked into kanban.py"
        )

    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, imported


def test_business_purity_runtime_surface():
    surface = [*spec.actions, *spec.functions, *spec.link_types]
    lowered = " ".join(surface).lower()
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in lowered
    assert set(spec.object_types) == {
        "Board",
        "BoardColumn",
        "Member",
        "Card",
        "CardComment",
        "CardRun",
    }


# ---------------------------------------------------------------------------
# 2. 看板拓扑声明与校验
# ---------------------------------------------------------------------------
def test_create_board_persists_topology_as_data(board):
    make_board(board)
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.title == "研发看板"
    columns_seen = board.query_objects("BoardColumn", MANAGER)
    assert {row.key for row in columns_seen} == {
        "todo",
        "ready",
        "doing",
        "done",
        "blocked",
        "triage",
    }
    ready = next(row for row in columns_seen if row.key == "ready")
    assert ready.claimable is True
    assert ready.allowed_roles == ("developer", "tester")
    assert ready.allowed_targets == ("todo", "blocked")
    done = next(row for row in columns_seen if row.key == "done")
    assert done.is_terminal is True


def test_board_topology_missing_purpose_rejected(board):
    broken = []
    for column in columns():
        if column["purpose"] == "escalation":
            continue
        column = dict(column)
        # 指向被删列的流转出口一并清掉，好让「用途缺失」这条护栏先触发。
        for field_name in (
            "on_claimed",
            "on_completed",
            "on_failed",
            "on_exhausted",
            "on_blocked",
            "on_abandoned",
            "on_unblocked",
        ):
            if column.get(field_name) == "triage":
                column[field_name] = ""
        broken.append(column)
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "BOARD_TOPOLOGY_INCOMPLETE"
    with pytest.raises(ObjectNotFoundError):
        board.get_object(Board, "BD-1", MANAGER)


def test_board_topology_ambiguous_purpose_rejected(board):
    broken = columns() + [
        {
            "key": "ready2",
            "label": "又一个可认领",
            "purpose": "ready",
            "order": 9,
            "claimable": True,
            "allowed_roles": ("developer",),
        }
    ]
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "BOARD_TOPOLOGY_AMBIGUOUS"


def test_board_topology_requires_terminal_completion_column(board):
    broken = columns()
    for column in broken:
        if column["purpose"] == "completion":
            column["is_terminal"] = False
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "BOARD_TOPOLOGY_INVALID"


def test_claimable_column_must_declare_roles(board):
    broken = columns()
    for column in broken:
        if column["purpose"] == "ready":
            column["allowed_roles"] = ()
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "COLUMN_MISSING_ALLOWED_ROLES"


def test_allowed_target_must_exist(board):
    broken = columns()
    for column in broken:
        if column["purpose"] == "initial":
            column["allowed_targets"] = ("nowhere",)
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "BOARD_TOPOLOGY_INVALID"


def test_transition_target_must_exist(board):
    broken = columns()
    for column in broken:
        if column["purpose"] == "working":
            column["on_completed"] = "nowhere"
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "BOARD_TOPOLOGY_INVALID"


def test_completion_exit_must_land_on_terminal_column(board):
    broken = columns()
    for column in broken:
        if column["purpose"] == "working":
            column["on_completed"] = "ready"
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, column_specs=broken)
    assert excinfo.value.code == "BOARD_TOPOLOGY_INVALID"


# ---------------------------------------------------------------------------
# 3. 流转出口是数据
# ---------------------------------------------------------------------------
def test_transitions_drive_the_flow_not_hardcoded_branches(board):
    """换一张看板的流转出口，卡片就走另一条路。"""
    specs = columns()
    for column in specs:
        if column["purpose"] == "working":
            column["on_failed"] = "todo"  # 失败回待办，而不是回可认领
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    assert card_of(board, "C-1").column_key == "doing"
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "failed",
            "summary": "没跑通",
        },
    )
    assert card_of(board, "C-1").column_key == "todo"


def test_claim_exit_is_data(board):
    specs = columns()
    for column in specs:
        if column["purpose"] == "ready":
            column["on_claimed"] = "doing"
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    assert card_of(board, "C-1").column_key == "doing"


def test_board_flow_exposes_nodes_and_edges(board):
    make_board(board)
    make_card(board, "C-1", "写内核")
    flow = board.call_function("board_flow", {"board_key": "BD-1"}, MANAGER)
    node_ids = [node["id"] for node in flow["nodes"]]
    assert node_ids == ["todo", "ready", "doing", "done", "blocked", "triage"]
    edges = {(e["from"], e["to"], e["label"]) for e in flow["edges"]}
    assert ("ready", "doing", "认领") in edges
    assert ("doing", "done", "完成") in edges
    assert ("doing", "ready", "失败重试") in edges
    assert ("doing", "blocked", "重试耗尽") in edges
    assert ("doing", "triage", "放弃") in edges
    assert ("blocked", "ready", "解阻") in edges
    ready = next(node for node in flow["nodes"] if node["id"] == "ready")
    assert ready["claimable"] is True
    assert ready["cards"] == 1


def test_board_overview_includes_transition_exits(board):
    make_board(board)
    overview = board.call_function(
        "board_overview", {"board_key": "BD-1"}, MANAGER
    )
    doing = next(
        column for column in overview["columns"] if column["purpose"] == "working"
    )
    assert doing["on_completed"] == "done"
    assert doing["on_failed"] == "ready"


# ---------------------------------------------------------------------------
# 3. 建卡与依赖
# ---------------------------------------------------------------------------
def test_card_without_dependency_lands_on_claimable_column(board):
    make_board(board)
    make_card(board, "C-1", "写内核")
    card = card_of(board, "C-1")
    assert card.column_key == "ready"
    assert card.assignee_id is None
    assert card.tenant == "acme"


def test_card_with_dependency_waits_on_initial_column(board):
    make_board(board)
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    assert card_of(board, "C-1").column_key == "ready"
    assert card_of(board, "C-2").column_key == "todo"


def test_create_card_rejects_empty_title_and_duplicate(board):
    make_board(board)
    with pytest.raises(InvariantError) as excinfo:
        make_card(board, "C-1", "  ")
    assert excinfo.value.code == "EMPTY_CARD_TITLE"

    make_card(board, "C-1", "写内核")
    with pytest.raises(InvariantError) as excinfo:
        make_card(board, "C-1", "重复")
    assert excinfo.value.code == "CARD_ALREADY_EXISTS"


def test_create_card_rejects_self_dependency(board):
    make_board(board)
    with pytest.raises(InvariantError) as excinfo:
        make_card(board, "C-1", "自依赖", waits_for=("C-1",))
    assert excinfo.value.code == "CARD_DEPENDS_ON_ITSELF"


def test_create_card_rejects_cross_board_parent(board):
    make_board(board, "BD-1")
    make_board(board, "BD-2")
    make_card(board, "C-1", "父卡")
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "create_card",
            {
                "card_id": "C-2",
                "board_key": "BD-2",
                "title": "跨板子卡",
                "waits_for": ("C-1",),
            },
        )
    assert excinfo.value.code == "PARENT_CROSS_BOARD"


def test_dependency_edge_is_recorded(board):
    make_board(board)
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    child = card_of(board, "C-2")
    parents = board.get_linked(child, CARD_WAITS_FOR, Card, MANAGER)
    assert [p.card_id for p in parents] == ["C-1"]


# ---------------------------------------------------------------------------
# 4. 人工移动卡片
# ---------------------------------------------------------------------------
def test_manual_move_respects_allowed_targets(board):
    make_board(board)
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    with pytest.raises(InvariantError) as excinfo:
        act(board, "move_card", {"card_id": "C-2", "target_column_key": "done"})
    assert excinfo.value.code == "MANUAL_MOVE_NOT_ALLOWED"

    act(board, "move_card", {"card_id": "C-2", "target_column_key": "ready"})
    assert card_of(board, "C-2").column_key == "ready"


def test_manual_move_into_gated_column_requires_parents(board):
    specs = columns()
    for column in specs:
        if column["purpose"] == "ready":
            column["parent_gated"] = True
    make_board(board, column_specs=specs)
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    with pytest.raises(InvariantError) as excinfo:
        act(board, "move_card", {"card_id": "C-2", "target_column_key": "ready"})
    assert excinfo.value.code == "PARENTS_NOT_SATISFIED"


# ---------------------------------------------------------------------------
# 5. 认领护栏
# ---------------------------------------------------------------------------
def test_claim_moves_to_working_column_and_opens_run(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")

    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    card = card_of(board, "C-1")
    assert card.column_key == "doing"
    assert card.assignee_id == "M-1"
    member = board.get_object(Member, "M-1", MANAGER)
    assert member.availability == "BUSY"
    assert member.active_card_id == "C-1"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert len(runs) == 1
    assert runs[0].status == "running"
    assert runs[0].column_key == "ready"


def test_claim_rejects_non_claimable_column(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    # 子卡停在初始列（不可认领）
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card_id": "C-2", "member_id": "M-1"})
    assert excinfo.value.code == "CARD_NOT_CLAIMABLE"


def test_claim_rejects_role_not_allowed_by_column(board):
    make_board(board)
    add_member(board, "M-1", role="coordinator")
    make_card(board, "C-1", "写内核")
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    assert excinfo.value.code == "ROLE_NOT_ALLOWED_FOR_COLUMN"


def test_claim_rejects_busy_member(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "卡一")
    make_card(board, "C-2", "卡二")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card_id": "C-2", "member_id": "M-1"})
    assert excinfo.value.code == "MEMBER_NOT_IDLE"


def test_claim_rejects_second_claimant(board):
    make_board(board)
    add_member(board, "M-1")
    add_member(board, "M-2")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card_id": "C-1", "member_id": "M-2"})
    assert excinfo.value.code == "CARD_ALREADY_CLAIMED"


def test_claim_blocked_by_unsatisfied_parents(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    act(board, "move_card", {"card_id": "C-2", "target_column_key": "ready"})
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card_id": "C-2", "member_id": "M-1"})
    assert excinfo.value.code == "PARENTS_NOT_SATISFIED"


# ---------------------------------------------------------------------------
# 6. 登记结果护栏
# ---------------------------------------------------------------------------
def test_complete_requires_evidence(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "completed",
                "summary": "写完了",
            },
        )
    assert excinfo.value.code == "MISSING_EVIDENCE"


def test_complete_requires_numeric_evidence(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "completed",
                "summary": "写完了",
                "evidence": [
                    {"metric": "测试条数", "value": "全绿", "detail": ""}
                ],
            },
        )
    assert excinfo.value.code == "EVIDENCE_WITHOUT_NUMBER"


def test_complete_moves_to_terminal_column_and_keeps_evidence(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "内核写完",
            "evidence": [
                {"metric": "测试条数", "value": "52 passed", "detail": ""}
            ],
            "board_state": "新基线：52 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    card = card_of(board, "C-1")
    assert card.column_key == "done"
    assert card.result == "内核写完"
    member = board.get_object(Member, "M-1", MANAGER)
    assert member.availability == "IDLE"
    assert member.completed_cards == 1
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert runs[0].status == "done"
    assert runs[0].outcome == "completed"
    assert runs[0].evidence[0].metric == "测试条数"
    assert runs[0].evidence[0].value == "52 passed"


def test_record_outcome_rejects_non_assignee(board):
    make_board(board)
    add_member(board, "M-1")
    add_member(board, "M-2")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {"card_id": "C-1", "member_id": "M-2", "outcome": "failed"},
        )
    assert excinfo.value.code == "CARD_NOT_CLAIMED_BY_MEMBER"


def test_record_outcome_rejects_unknown_outcome(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {"card_id": "C-1", "member_id": "M-1", "outcome": "PASS"},
        )
    assert excinfo.value.code == "INVALID_OUTCOME"


def test_repeated_failure_trips_retry_limit_into_failure_column(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    # ready 列 max_retries 默认 3 → 第 4 次失败进阻塞列
    for _ in range(3):
        act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "failed",
                "summary": "没跑通",
            },
        )
    card = card_of(board, "C-1")
    assert card.column_key == "ready"
    assert card.consecutive_failures == 3

    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "failed",
            "summary": "又没跑通",
        },
    )
    card = card_of(board, "C-1")
    assert card.column_key == "blocked"
    assert card.consecutive_failures == 4


def test_block_requires_typed_reason(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "blocked",
                "summary": "卡住了",
            },
        )
    assert excinfo.value.code == "INVALID_BLOCK_KIND"


def test_dependency_block_returns_card_to_initial_column(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "blocked",
            "summary": "等前置",
            "block_kind": "dependency",
        },
    )
    card = card_of(board, "C-1")
    assert card.column_key == "todo"
    assert card.block_kind == "dependency"


def test_repeated_block_escalates_to_triage(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    # 阻塞列 max_retries = 2 → 第 3 次解阻后升级
    for _ in range(3):
        act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "blocked",
                "summary": "还是卡",
                "block_kind": "awaiting_review",
            },
        )
        if card_of(board, "C-1").column_key == "blocked":
            act(board, "unblock_card", {"card_id": "C-1"})
    card = card_of(board, "C-1")
    assert card.column_key == "triage"
    assert card.block_recurrences == 3


# ---------------------------------------------------------------------------
# 7. 前置完成后子卡自动提升
# ---------------------------------------------------------------------------
def test_completing_parent_promotes_child_to_ready(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "父卡完成",
            "evidence": [{"metric": "测试条数", "value": "10 passed"}],
            "board_state": "新基线：父卡完成，10 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    assert card_of(board, "C-2").column_key == "ready"


# ---------------------------------------------------------------------------
# 8. 审计账本
# ---------------------------------------------------------------------------
def test_every_business_action_is_audited(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "7 passed"}],
            "board_state": "新基线：7 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    runs = board.list_action_runs(status="COMMITTED")
    kinds = [row["action_type"] for row in runs]
    assert kinds == [
        "create_board",
        "add_member",
        "create_card",
        "claim_card",
        "record_outcome",
    ]
    assert all(row["actor_id"] == "board-manager-01" for row in runs)


def test_rejected_action_leaves_rejected_audit(board):
    make_board(board)
    with pytest.raises(InvariantError):
        make_card(board, "C-1", "")
    rejected = board.list_action_runs(status="REJECTED")
    assert rejected and rejected[-1]["error_code"] == "EMPTY_CARD_TITLE"


# ---------------------------------------------------------------------------
# 9. 租户行级安全
# ---------------------------------------------------------------------------
def test_tenant_isolation_on_cards(board):
    make_board(board, "BD-1", tenant="acme")
    make_card(board, "C-1", "acme 的卡")
    make_board(board, "BD-2", tenant="other")
    act(
        board,
        "create_card",
        {"card_id": "C-2", "board_key": "BD-2", "title": "其他租户的卡"},
    )
    other = Actor(
        actor_id="intruder",
        role="board_manager",
        clearance_tags={"tenant:acme"},
    )
    visible = {row.card_id for row in board.query_objects("Card", other)}
    assert visible == {"C-1"}
    with pytest.raises(PermissionDeniedError):
        board.get_object(Card, "C-2", other)


# ---------------------------------------------------------------------------
# 10. 只读看板与简报
# ---------------------------------------------------------------------------
def test_board_overview_exposes_topology(board):
    make_board(board)
    make_card(board, "C-1", "写内核")
    overview = board.call_function(
        "board_overview", {"board_key": "BD-1"}, MANAGER
    )
    assert overview["cards_total"] == 1
    purposes = [column["purpose"] for column in overview["columns"]]
    assert purposes == [
        "initial",
        "ready",
        "working",
        "completion",
        "failure",
        "escalation",
    ]
    ready = next(
        column for column in overview["columns"] if column["purpose"] == "ready"
    )
    assert ready["claimable"] is True
    assert ready["allowed_roles"] == ["developer", "tester"]
    assert ready["cards"] == 1


def test_board_overview_without_board_key_lists_every_board(board):
    make_board(board, "BD-1")
    make_board(board, "BD-2")
    overview = board.call_function("board_overview", {}, MANAGER)
    assert overview["boards_total"] == 2
    assert {row["board_key"] for row in overview["boards"]} == {"BD-1", "BD-2"}


def test_card_briefing_contains_history_and_evidence(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "9 passed"}],
            "board_state": "新基线：9 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    briefing = board.call_function("card_briefing", {"card_id": "C-1"}, MANAGER)
    assert briefing["column"]["purpose"] == "completion"
    assert briefing["runs"][0]["evidence"][0]["value"] == "9 passed"


# ---------------------------------------------------------------------------
# 11. 评论与承接关系
# ---------------------------------------------------------------------------
def test_comments_are_recorded(board):
    make_board(board)
    make_card(board, "C-1", "写内核")
    act(
        board,
        "add_comment",
        {"card_id": "C-1", "author": "M-1", "body": "先做内核"},
    )
    card = card_of(board, "C-1")
    comments = board.get_linked(card, CARD_COMMENTS, CardComment, MANAGER)
    assert len(comments) == 1
    assert comments[0].body == "先做内核"


def test_assignment_link_released_after_completion(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    card = card_of(board, "C-1")
    assert [
        m.member_id
        for m in board.get_linked(card, CARD_ASSIGNED_TO, Member, MANAGER)
    ] == ["M-1"]
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "3 passed"}],
            "board_state": "新基线：3 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    card = card_of(board, "C-1")
    assert board.get_linked(card, CARD_ASSIGNED_TO, Member, MANAGER) == []


# ---------------------------------------------------------------------------
# 12. 项目管理三要素：goal / state / unfolded_completions
# ---------------------------------------------------------------------------
GOAL = "四层元模型单向依赖，内核零业务污染。"
STATE = "微内核就绪，看板流自举。"


def make_board_with_goal(runtime: OntologyRuntime) -> None:
    act(
        runtime,
        "create_board",
        {
            "board_key": "BD-1",
            "title": "研发看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
            "columns": columns(),
            "goal": GOAL,
            "state": STATE,
        },
    )


def test_create_board_persists_goal_and_state(board):
    make_board_with_goal(board)
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.goal == GOAL
    assert stored.state == STATE
    assert stored.unfolded_completions == 0


def test_completion_without_board_state_is_refused_and_counter_stays(board):
    make_board_with_goal(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "completed",
                "summary": "完成",
                "evidence": [{"metric": "测试条数", "value": "5 passed"}],
                "goal_changed": False,
                "goal_review_reason": "目标未变。",
            },
        )
    assert excinfo.value.code == "MISSING_BOARD_STATE"
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.unfolded_completions == 0
    assert stored.state == STATE


def test_completion_with_board_state_folds_state_and_resets_counter(board):
    make_board_with_goal(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "5 passed"}],
            "board_state": "  内核 1,400 行，全仓 430 passed。  ",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == "内核 1,400 行，全仓 430 passed。"
    assert stored.unfolded_completions == 0


def test_update_board_state_folds_state_and_resets_counter(board):
    make_board_with_goal(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "5 passed"}],
            "board_state": "新基线：5 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    assert board.get_object(Board, "BD-1", MANAGER).unfolded_completions == 0

    act(
        board,
        "update_board_state",
        {"board_key": "BD-1", "state": "新一代基线", "goal": "新架构红线"},
    )
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == "新一代基线"
    assert stored.goal == "新架构红线"
    assert stored.unfolded_completions == 0


def test_update_board_state_rejects_empty_state(board):
    make_board_with_goal(board)
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "update_board_state",
            {"board_key": "BD-1", "state": "   "},
        )
    assert excinfo.value.code == "EMPTY_BOARD_STATE"
    assert board.get_object(Board, "BD-1", MANAGER).state == STATE


def test_briefing_overview_and_handover_carry_goal_and_state(board):
    make_board_with_goal(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "5 passed"}],
            "board_state": "新基线：5 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )

    briefing = board.call_function("card_briefing", {"card_id": "C-1"}, MANAGER)
    assert briefing["board_goal"] == GOAL
    assert briefing["board_state"] == "新基线：5 passed"
    assert briefing["unfolded_completions"] == 0
    text = render_card_briefing(briefing)
    assert GOAL in text
    assert "新基线：5 passed" in text

    overview = board.call_function(
        "board_overview", {"board_key": "BD-1"}, MANAGER
    )
    assert overview["goal"] == GOAL
    assert overview["state"] == "新基线：5 passed"
    assert overview["unfolded_completions"] == 0
    assert overview["cards_summary"] == [
        {
            "card_id": "C-1",
            "title": "写内核",
            "column_key": "done",
            "assignee_id": None,
            "result": "完成",
        }
    ]

    handover = render_board_handover(overview)
    assert GOAL in handover
    assert "新基线：5 passed" in handover
    assert "待折叠完工数：0" in handover
    # 也支持整块总览（含 boards）取第一块板
    every = board.call_function("board_overview", {}, MANAGER)
    assert GOAL in render_board_handover(every)


# ---------------------------------------------------------------------------
# 13. 完工必须显式更新 state 并给出 goal 评估
# ---------------------------------------------------------------------------
NEW_STATE = "新基线：内核 1,500 行，全仓 460 passed。"
NEW_GOAL = "新目标：看板完工必须显式交接 state 与 goal 评估。"
REVIEW_REASON = "完工改变了项目基线，目标未变。"


def _claim_default_card(runtime: OntologyRuntime) -> None:
    add_member(runtime, "M-1")
    make_card(runtime, "C-1", "写内核")
    act(runtime, "claim_card", {"card_id": "C-1", "member_id": "M-1"})


def _complete_default(
    runtime: OntologyRuntime, **extra: object
) -> dict:
    params: dict = {
        "card_id": "C-1",
        "member_id": "M-1",
        "outcome": "completed",
        "summary": "完成",
        "evidence": [{"metric": "测试条数", "value": "5 passed"}],
    }
    params.update(extra)
    return act(runtime, "record_outcome", params)


def _assert_refused_completion_is_atomic(
    runtime: OntologyRuntime, expected_state: str = STATE
) -> None:
    card = card_of(runtime, "C-1")
    assert card.column_key == "doing"
    assert card.assignee_id == "M-1"
    member = runtime.get_object(Member, "M-1", MANAGER)
    assert member.availability == "BUSY"
    assert member.active_card_id == "C-1"
    assert member.completed_cards == 0
    runs = runtime.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert len(runs) == 1
    assert runs[0].status == "running"
    stored = runtime.get_object(Board, "BD-1", MANAGER)
    assert stored.state == expected_state
    assert stored.goal == GOAL
    assert stored.unfolded_completions == 0


def test_completion_without_board_state_is_refused_atomically(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            goal_changed=False,
            goal_review_reason=REVIEW_REASON,
        )
    assert excinfo.value.code == "MISSING_BOARD_STATE"
    _assert_refused_completion_is_atomic(board)


def test_completion_with_blank_board_state_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state="   ",
            goal_changed=False,
            goal_review_reason=REVIEW_REASON,
        )
    assert excinfo.value.code == "MISSING_BOARD_STATE"
    _assert_refused_completion_is_atomic(board)


def test_completion_with_unchanged_normalized_board_state_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state=f"  {STATE}  ",
            goal_changed=False,
            goal_review_reason=REVIEW_REASON,
        )
    assert excinfo.value.code == "UNCHANGED_BOARD_STATE"
    _assert_refused_completion_is_atomic(board)


def test_completion_without_goal_decision_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state=NEW_STATE,
            goal_review_reason=REVIEW_REASON,
        )
    assert excinfo.value.code == "MISSING_GOAL_ASSESSMENT"
    _assert_refused_completion_is_atomic(board)


def test_completion_without_goal_review_reason_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state=NEW_STATE,
            goal_changed=False,
            goal_review_reason="   ",
        )
    assert excinfo.value.code == "MISSING_GOAL_REVIEW_REASON"
    _assert_refused_completion_is_atomic(board)


def test_completion_with_explicit_unchanged_goal_keeps_goal_and_stores_review(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    _complete_default(
        board,
        board_state=f"  {NEW_STATE}  ",
        goal_changed=False,
        goal_review_reason=REVIEW_REASON,
    )

    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == NEW_STATE
    assert stored.goal == GOAL
    assert stored.unfolded_completions == 0

    card = card_of(board, "C-1")
    assert card.column_key == "done"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert runs[0].status == "done"
    assert runs[0].board_state == NEW_STATE
    assert runs[0].goal_changed is False
    assert runs[0].goal_review_reason == REVIEW_REASON
    assert runs[0].board_goal is None


def test_completion_with_goal_change_but_no_new_goal_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state=NEW_STATE,
            goal_changed=True,
            goal_review_reason=REVIEW_REASON,
        )
    assert excinfo.value.code == "MISSING_BOARD_GOAL"
    _assert_refused_completion_is_atomic(board)


def test_completion_with_goal_change_to_same_goal_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state=NEW_STATE,
            goal_changed=True,
            goal_review_reason=REVIEW_REASON,
            board_goal=GOAL,
        )
    assert excinfo.value.code == "UNCHANGED_BOARD_GOAL"
    _assert_refused_completion_is_atomic(board)


def test_completion_without_goal_change_but_contradictory_goal_is_refused(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            board_state=NEW_STATE,
            goal_changed=False,
            goal_review_reason=REVIEW_REASON,
            board_goal=NEW_GOAL,
        )
    assert excinfo.value.code == "GOAL_CHANGED_CONTRADICTION"
    _assert_refused_completion_is_atomic(board)


def test_completion_with_changed_goal_updates_board_and_stores_review(board):
    make_board_with_goal(board)
    _claim_default_card(board)
    _complete_default(
        board,
        board_state=NEW_STATE,
        goal_changed=True,
        goal_review_reason="目标随基线一起修订。",
        board_goal=NEW_GOAL,
    )

    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == NEW_STATE
    assert stored.goal == NEW_GOAL
    assert stored.unfolded_completions == 0

    card = card_of(board, "C-1")
    assert card.column_key == "done"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert runs[0].board_state == NEW_STATE
    assert runs[0].goal_changed is True
    assert runs[0].goal_review_reason == "目标随基线一起修订。"
    assert runs[0].board_goal == NEW_GOAL


def test_rejected_completion_then_corrected_call_succeeds_once(board):
    make_board_with_goal(board)
    _claim_default_card(board)

    with pytest.raises(InvariantError) as excinfo:
        _complete_default(
            board,
            goal_changed=False,
            goal_review_reason=REVIEW_REASON,
        )
    assert excinfo.value.code == "MISSING_BOARD_STATE"
    _assert_refused_completion_is_atomic(board)

    _complete_default(
        board,
        board_state=NEW_STATE,
        goal_changed=False,
        goal_review_reason=REVIEW_REASON,
    )

    card = card_of(board, "C-1")
    assert card.column_key == "done"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert len(runs) == 1
    assert runs[0].status == "done"
    member = board.get_object(Member, "M-1", MANAGER)
    assert member.completed_cards == 1
    assert board.get_object(Board, "BD-1", MANAGER).unfolded_completions == 0


@pytest.mark.parametrize(
    "outcome,extra",
    [
        ("failed", {}),
        ("blocked", {"block_kind": "awaiting_review"}),
        ("abandoned", {}),
    ],
)
def test_non_completion_outcomes_need_no_checkpoint(board, outcome, extra):
    make_board_with_goal(board)
    _claim_default_card(board)
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": outcome,
            "summary": "未完工",
            **extra,
        },
    )
    runs = board.get_linked(card_of(board, "C-1"), CARD_RUNS, CardRun, MANAGER)
    assert runs[0].status == outcome
    assert runs[0].board_state is None
    assert runs[0].goal_changed is None
    assert runs[0].goal_review_reason == ""
    assert runs[0].board_goal is None
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == STATE
    assert stored.goal == GOAL


def test_old_card_run_json_defaults_new_review_fields():
    old = CardRun.model_validate(
        {
            "run_id": "R-OLD",
            "card_id": "C-1",
            "tenant": "acme",
            "member_id": "M-1",
            "column_key": "ready",
        }
    )
    assert old.board_state is None
    assert old.goal_changed is None
    assert old.goal_review_reason == ""
    assert old.board_goal is None


# ---------------------------------------------------------------------------
# 14. 目标写入沿用既有授权边界（record_outcome 不扩权）
# ---------------------------------------------------------------------------
def worker_actor(role: str) -> Actor:
    return Actor(
        actor_id=f"{role}-01",
        role=role,
        clearance_tags={"tenant:acme"},
    )


def act_as(
    runtime: OntologyRuntime, actor: Actor, action: str, params: dict
) -> dict:
    return runtime.submit_action(
        f"TEST-{next(_RUN_IDS):05d}", action, params, actor
    )


@pytest.mark.parametrize("role", ["developer", "tester"])
def test_worker_goal_change_is_refused_before_mutation(board, role):
    make_board_with_goal(board)
    actor = worker_actor(role)
    add_member(board, "M-1", role=role)
    make_card(board, "C-1", "写内核")
    act_as(board, actor, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    with pytest.raises(InvariantError) as excinfo:
        act_as(
            board,
            actor,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "completed",
                "summary": "完成",
                "evidence": [{"metric": "测试条数", "value": "5 passed"}],
                "board_state": NEW_STATE,
                "goal_changed": True,
                "goal_review_reason": "工人试图改目标。",
                "board_goal": NEW_GOAL,
            },
        )
    assert excinfo.value.code == "GOAL_WRITE_NOT_AUTHORIZED"
    _assert_refused_completion_is_atomic(board)


@pytest.mark.parametrize("role", ["developer", "tester"])
def test_worker_unchanged_goal_completion_succeeds(board, role):
    make_board_with_goal(board)
    actor = worker_actor(role)
    add_member(board, "M-1", role=role)
    make_card(board, "C-1", "写内核")
    act_as(board, actor, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    act_as(
        board,
        actor,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "5 passed"}],
            "board_state": NEW_STATE,
            "goal_changed": False,
            "goal_review_reason": REVIEW_REASON,
        },
    )

    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == NEW_STATE
    assert stored.goal == GOAL
    assert stored.unfolded_completions == 0
    card = card_of(board, "C-1")
    assert card.column_key == "done"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert runs[0].goal_changed is False
    assert runs[0].goal_review_reason == REVIEW_REASON


@pytest.mark.parametrize(
    "role", ["board_manager", "admin", "system", "reviewer", "coordinator"]
)
def test_authorized_goal_writer_can_change_goal(board, role):
    make_board_with_goal(board)
    add_member(board, "M-1", role="developer")
    make_card(board, "C-1", "写内核")
    developer = worker_actor("developer")
    act_as(board, developer, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    caller = worker_actor(role)
    act_as(
        board,
        caller,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "5 passed"}],
            "board_state": NEW_STATE,
            "goal_changed": True,
            "goal_review_reason": "授权角色修订目标。",
            "board_goal": NEW_GOAL,
        },
    )

    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == NEW_STATE
    assert stored.goal == NEW_GOAL
    assert stored.unfolded_completions == 0
    runs = board.get_linked(card_of(board, "C-1"), CARD_RUNS, CardRun, MANAGER)
    assert runs[0].goal_changed is True
    assert runs[0].board_goal == NEW_GOAL


def test_update_board_state_clears_legacy_nonzero_counter(board):
    make_board_with_goal(board)
    legacy = board.get_object(Board, "BD-1", MANAGER).model_copy(
        update={"unfolded_completions": 3}
    )
    board.put_object(legacy, actor=MANAGER)
    assert board.get_object(Board, "BD-1", MANAGER).unfolded_completions == 3

    act(
        board,
        "update_board_state",
        {"board_key": "BD-1", "state": "折叠后的新基线"},
    )
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.unfolded_completions == 0
    assert stored.state == "折叠后的新基线"


# ---------------------------------------------------------------------------
# 15. 完成列只能由 record_outcome(completed) 通过校验后进入
# ---------------------------------------------------------------------------
def specs_with(column_key: str, **updates: object) -> list[dict]:
    specs = columns()
    for column in specs:
        if column["key"] == column_key:
            column.update(updates)
    return specs


def renamed_completion_specs() -> list[dict]:
    specs = columns()
    for column in specs:
        if column["key"] == "done":
            column["key"] = "shipped"
            column["label"] = "已交付"
        for field_name in (
            "on_claimed",
            "on_completed",
            "on_failed",
            "on_exhausted",
            "on_blocked",
            "on_abandoned",
            "on_unblocked",
        ):
            if column.get(field_name) == "done":
                column[field_name] = "shipped"
        if "done" in column.get("allowed_targets", ()):
            column["allowed_targets"] = tuple(
                "shipped" if target == "done" else target
                for target in column["allowed_targets"]
            )
    return specs


def test_manual_move_into_completion_column_is_refused(board):
    specs = specs_with("ready", allowed_targets=("todo", "blocked", "done"))
    make_board(board, column_specs=specs)
    make_card(board, "C-1", "写内核")

    with pytest.raises(InvariantError) as excinfo:
        act(board, "move_card", {"card_id": "C-1", "target_column_key": "done"})

    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    card = card_of(board, "C-1")
    assert card.column_key == "ready"
    assert card.assignee_id is None
    assert board.get_linked(card, CARD_RUNS, CardRun, MANAGER) == []
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.state == ""
    assert stored.goal == ""


def test_renamed_completion_column_is_guarded_by_purpose(board):
    specs = renamed_completion_specs()
    for column in specs:
        if column["key"] == "ready":
            column["allowed_targets"] = ("todo", "blocked", "shipped")
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")

    with pytest.raises(InvariantError) as excinfo:
        act(board, "move_card", {"card_id": "C-1", "target_column_key": "shipped"})
    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    assert card_of(board, "C-1").column_key == "ready"

    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "完成",
            "evidence": [{"metric": "测试条数", "value": "5 passed"}],
            "board_state": "新基线：5 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    assert card_of(board, "C-1").column_key == "shipped"


def test_claim_route_into_completion_column_is_refused(board):
    specs = specs_with("ready", on_claimed="done")
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")

    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    card = card_of(board, "C-1")
    assert card.column_key == "ready"
    assert card.assignee_id is None
    member = board.get_object(Member, "M-1", MANAGER)
    assert member.availability == "IDLE"
    assert member.active_card_id is None
    assert board.get_linked(card, CARD_RUNS, CardRun, MANAGER) == []


def test_failed_route_into_completion_column_is_refused(board):
    specs = specs_with("doing", on_failed="done")
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "failed",
                "summary": "失败",
            },
        )

    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    card = card_of(board, "C-1")
    assert card.column_key == "doing"
    assert card.assignee_id == "M-1"
    assert card.consecutive_failures == 0
    member = board.get_object(Member, "M-1", MANAGER)
    assert member.availability == "BUSY"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert len(runs) == 1 and runs[0].status == "running"


def test_exhausted_route_into_completion_column_is_refused(board):
    specs = specs_with("doing", on_exhausted="done")
    for column in specs:
        if column["key"] == "ready":
            column["max_retries"] = 1
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")

    # 第一次失败回可认领列（默认 on_failed），不触发耗尽出口。
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "failed",
            "summary": "第一次失败",
        },
    )
    assert card_of(board, "C-1").column_key == "ready"
    assert card_of(board, "C-1").consecutive_failures == 1

    # 第二次失败会走 on_exhausted=done，必须在任何状态变更前被拒绝。
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": "failed",
                "summary": "第二次失败",
            },
        )

    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    card = card_of(board, "C-1")
    assert card.column_key == "doing"
    assert card.consecutive_failures == 1
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert [run.status for run in runs] == ["failed", "running"]


@pytest.mark.parametrize(
    "outcome,extra,route_field",
    [
        ("blocked", {"block_kind": "awaiting_review"}, "on_blocked"),
        ("abandoned", {}, "on_abandoned"),
    ],
)
def test_noncompletion_routes_into_completion_column_are_refused(
    board, outcome, extra, route_field
):
    specs = specs_with("doing", **{route_field: "done"})
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "record_outcome",
            {
                "card_id": "C-1",
                "member_id": "M-1",
                "outcome": outcome,
                "summary": "未完工",
                **extra,
            },
        )

    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    card = card_of(board, "C-1")
    assert card.column_key == "doing"
    assert card.block_kind is None
    member = board.get_object(Member, "M-1", MANAGER)
    assert member.availability == "BUSY"
    runs = board.get_linked(card, CARD_RUNS, CardRun, MANAGER)
    assert len(runs) == 1 and runs[0].status == "running"


def test_unblock_route_into_completion_column_is_refused(board):
    specs = specs_with("blocked", on_unblocked="done")
    make_board(board, column_specs=specs)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "blocked",
            "summary": "卡住",
            "block_kind": "awaiting_review",
        },
    )
    assert card_of(board, "C-1").column_key == "blocked"

    with pytest.raises(InvariantError) as excinfo:
        act(board, "unblock_card", {"card_id": "C-1"})

    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    card = card_of(board, "C-1")
    assert card.column_key == "blocked"
    assert card.block_kind == "awaiting_review"
    assert card.block_recurrences == 0
