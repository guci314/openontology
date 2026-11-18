"""纯业务任务看板本体（examples/kanban.py）验收测试。

覆盖：
1. 100% 业务纯洁性静态审计（源码 + AST 导入 + 运行时表面）；
2. 固定 6 态流、建板校验与 board_flow / board_overview；
3. 建卡、AutoID 与依赖 DAG（无环、禁止跨板与自依赖）；
4. 人工移动护栏（allowed_targets 与禁止绕行进入 done）；
5. 认领/指派护栏（状态、角色、WIP 上限、前置门控、Actor/Member 身份绑定）；
6. 登记结果（completed / failed / blocked / abandoned）与子卡自动提升；
7. 解阻与反复阻塞升级、认领超时释放（release_claim）；
8. eligible_cards / card_briefing / 评论 / 租户行级安全隔离 / 审计账本。
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
    OntologyRuntime,
    PermissionDeniedError,
)
from examples.kanban import (
    Board,
    Card,
    CardComment,
    Member,
    render_card_briefing,
    spec,
)
from open_ontology.model_views import describe_behavior, describe_tbox

ROOT = Path(__file__).resolve().parents[1]

MANAGER = Actor(
    actor_id="board-manager-01",
    role="board_manager",
    clearance_tags={"*"},
)

_RUN_IDS = itertools.count(1)


@pytest.fixture()
def board() -> OntologyRuntime:
    runtime = OntologyRuntime(spec)
    yield runtime
    runtime.close()


def act(runtime: OntologyRuntime, action: str, params: dict) -> dict:
    return runtime.submit_action(
        f"TEST-{next(_RUN_IDS):05d}", action, params, MANAGER
    )


def act_as(
    runtime: OntologyRuntime, actor: Actor, action: str, params: dict
) -> dict:
    return runtime.submit_action(
        f"TEST-{next(_RUN_IDS):05d}", action, params, actor
    )


def make_board(
    runtime: OntologyRuntime,
    board_key: str = "BD-1",
    tenant: str = "acme",
) -> None:
    act(
        runtime,
        "create_board",
        {
            "board_key": board_key,
            "title": "研发看板",
            "tenant": tenant,
            "created_by": "board-manager-01",
        },
    )


def add_member(
    runtime: OntologyRuntime,
    member_id: str,
    role: str = "developer",
    wip_limit: int = 1,
) -> None:
    act(
        runtime,
        "add_member",
        {
            "member_id": member_id,
            "name": member_id,
            "role": role,
            "wip_limit": wip_limit,
        },
    )


def card_of(runtime: OntologyRuntime, card_id: str) -> Card:
    return runtime.get_object(Card, card_id, MANAGER)


def make_card(
    runtime: OntologyRuntime, card_id: str, title: str = "写内核", **extra
) -> None:
    act(
        runtime,
        "create_card",
        {"card_id": card_id, "board": "BD-1", "title": title, **extra},
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
ALLOWED_IMPORTS = {"__future__", "typing", "pydantic", "open_ontology", "time"}


def test_business_purity_static_scan():
    path = ROOT / "examples" / "kanban.py"
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
    surface = [*spec.actions, *spec.functions, *spec.object_properties]
    lowered = " ".join(surface).lower()
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in lowered
    assert set(spec.object_types) == {
        "Board",
        "Member",
        "Card",
        "CardComment",
    }
    # 世界模型不做调度也不混入自举文档检查点
    for removed in ("board_run", "update_board_state", "heartbeat_claim", "reclaim_stale_claims"):
        assert removed not in spec.actions
    assert "auto_board_run" not in spec.automations
    assert "availability" not in Member.model_fields
    assert "active_card" not in Member.model_fields
    for removed_board_field in ("goal", "state", "unfolded_completions", "status"):
        assert removed_board_field not in Board.model_fields


# ---------------------------------------------------------------------------
# 2. 固定 6 态流与看板创建
# ---------------------------------------------------------------------------
def test_create_board_and_fixed_six_state_flow(board):
    make_board(board)
    stored = board.get_object(Board, "BD-1", MANAGER)
    assert stored.title == "研发看板"

    make_card(board, "C-1", "写内核")
    flow = board.call_function("board_flow", {"board": "BD-1"}, MANAGER)
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


def test_create_board_rejects_empty_tenant_and_duplicate(board):
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, "BD-1", tenant="  ")
    assert excinfo.value.code == "EMPTY_TENANT"

    make_board(board, "BD-1", tenant="acme")
    with pytest.raises(InvariantError) as excinfo:
        make_board(board, "BD-1", tenant="acme")
    assert excinfo.value.code == "BOARD_ALREADY_EXISTS"


def test_board_overview_exposes_fixed_statuses_and_member_wip(board):
    make_board(board)
    add_member(board, "M-1", wip_limit=2)
    make_card(board, "C-1", "任务一")
    act(board, "claim_card", {"card": "C-1", "member": "M-1"})

    overview = board.call_function(
        "board_overview", {"board": "BD-1"}, MANAGER
    )
    assert [s["key"] for s in overview["statuses"]] == [
        "todo",
        "ready",
        "doing",
        "done",
        "blocked",
        "triage",
    ]
    assert overview["members"] == [
        {
            "member_id": "M-1",
            "name": "M-1",
            "role": "developer",
            "active_wip": 1,
            "wip_limit": 2,
            "completed_cards": 0,
        }
    ]


# ---------------------------------------------------------------------------
# 3. 建卡、AutoID 与依赖 DAG
# ---------------------------------------------------------------------------
def test_card_without_dependency_lands_on_ready(board):
    make_board(board)
    make_card(board, "C-1", "写内核")
    card = card_of(board, "C-1")
    assert card.status == "ready"
    assert card.assignee is None
    assert card.tenant == "acme"


def test_card_with_dependency_waits_on_todo(board):
    make_board(board)
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    assert card_of(board, "C-1").status == "ready"
    assert card_of(board, "C-2").status == "todo"


def test_create_card_rejects_empty_title_duplicate_self_and_cross_board(board):
    make_board(board, "BD-1")
    make_board(board, "BD-2")

    with pytest.raises(InvariantError) as excinfo:
        make_card(board, "C-1", "  ")
    assert excinfo.value.code == "EMPTY_CARD_TITLE"

    make_card(board, "C-1", "写内核")
    with pytest.raises(InvariantError) as excinfo:
        make_card(board, "C-1", "重复")
    assert excinfo.value.code == "CARD_ALREADY_EXISTS"

    with pytest.raises(InvariantError) as excinfo:
        make_card(board, "C-X", "自依赖", waits_for=("C-X",))
    assert excinfo.value.code == "CARD_DEPENDS_ON_ITSELF"

    with pytest.raises(InvariantError) as excinfo:
        act(
            board,
            "create_card",
            {
                "card_id": "C-2",
                "board": "BD-2",
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
    parents = board.get_linked(child, "Card.waits_for", Card, MANAGER)
    assert [p.card_id for p in parents] == ["C-1"]


def test_create_card_auto_generates_card_id_and_schema_hints(board):
    make_board(board)
    add_member(board, "M-1")

    act(board, "create_card", {"board": "BD-1", "title": "首张自动编号卡"})
    c1 = card_of(board, "C-001")
    assert c1.title == "首张自动编号卡"

    act(
        board,
        "create_card",
        {
            "card_id": "",
            "board": "BD-1",
            "title": "第二张自动编号卡",
            "assignee": "",
            "waits_for": ("C-001",),
        },
    )
    c2 = card_of(board, "C-002")
    assert c2.status == "todo"

    make_card(board, "C-103", "跳号卡")
    act(
        board,
        "create_card",
        {"board": "BD-1", "title": "续号卡", "assignee": "M-1"},
    )
    assert card_of(board, "C-104").assignee == "M-1"

    tbox = describe_tbox(spec)
    card_tbox = next(obj for obj in tbox["object_types"] if obj["name"] == "Card")
    card_fields = {f["name"]: f for f in card_tbox["fields"]}
    assert card_fields["card_id"].get("auto_generated") is True
    assert card_fields["board"].get("property_kind") == "object"
    assert card_fields["assignee"].get("property_kind") == "object"
    assert card_fields["waits_for"].get("multiple") is True

    behavior = describe_behavior(spec)
    create_action = next(a for a in behavior["actions"] if a["name"] == "create_card")
    props = create_action["params"]["properties"]
    assert props["card_id"].get("auto_generated") is True

    outcome_action = next(a for a in behavior["actions"] if a["name"] == "record_outcome")
    oprops = outcome_action["params"]["properties"]
    assert oprops["outcome"].get("enum") == ["completed", "blocked", "failed", "abandoned"]
    assert any(
        b.get("enum") == ["dependency", "awaiting_review", "external_dependency", "insufficient_input"]
        for b in oprops["block_kind"].get("anyOf", [])
    )

    role_names = [r["name"] for r in behavior["roles"]]
    assert role_names == [
        "board_manager",
        "admin",
        "system",
        "developer",
        "reviewer",
        "tester",
        "coordinator",
    ]
    assert all(r["declared"] is True for r in behavior["roles"])
    by_role = {r["name"]: r for r in behavior["roles"]}
    assert "claim_card" in by_role["developer"]["allowed_actions"]
    assert "record_outcome" in by_role["developer"]["allowed_actions"]
    assert "create_card" not in by_role["developer"]["allowed_actions"]
    assert "create_card" in by_role["board_manager"]["allowed_actions"]


# ---------------------------------------------------------------------------
# 4. 人工移动护栏
# ---------------------------------------------------------------------------
def test_manual_move_respects_allowed_targets_and_blocks_done(board):
    make_board(board)
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))

    with pytest.raises(InvariantError) as excinfo:
        act(board, "move_card", {"card": "C-2", "target_status": "done"})
    assert excinfo.value.code == "COMPLETION_REQUIRES_RECORD_OUTCOME"

    with pytest.raises(InvariantError) as excinfo:
        act(board, "move_card", {"card": "C-2", "target_status": "doing"})
    assert excinfo.value.code == "MANUAL_MOVE_NOT_ALLOWED"

    act(board, "move_card", {"card": "C-2", "target_status": "ready"})
    assert card_of(board, "C-2").status == "ready"


# ---------------------------------------------------------------------------
# 5. 认领 / WIP 上限 / Actor-Member 身份绑定
# ---------------------------------------------------------------------------
def test_claim_moves_to_doing_and_enforces_wip_limit(board):
    make_board(board)
    add_member(board, "M-1", wip_limit=1)
    add_member(board, "M-2", wip_limit=2)
    make_card(board, "C-1", "卡一")
    make_card(board, "C-2", "卡二")
    make_card(board, "C-3", "卡三")

    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    assert card_of(board, "C-1").status == "doing"
    assert card_of(board, "C-1").assignee == "M-1"

    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card": "C-2", "member": "M-1"})
    assert excinfo.value.code == "WIP_LIMIT_EXCEEDED"

    # wip_limit=2 允许同时认领两张卡
    act(board, "claim_card", {"card": "C-2", "member": "M-2"})
    act(board, "claim_card", {"card": "C-3", "member": "M-2"})
    assert card_of(board, "C-2").status == "doing"
    assert card_of(board, "C-3").status == "doing"


def test_claim_rejects_non_claimable_role_and_second_claimant(board):
    make_board(board)
    add_member(board, "M-1")
    add_member(board, "M-2")
    add_member(board, "M-COORD", role="coordinator")
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))

    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card": "C-2", "member": "M-1"})
    assert excinfo.value.code == "CARD_NOT_CLAIMABLE"

    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card": "C-1", "member": "M-COORD"})
    assert excinfo.value.code == "ROLE_NOT_ALLOWED_FOR_STATUS"

    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card": "C-1", "member": "M-2"})
    assert excinfo.value.code == "CARD_ALREADY_CLAIMED"


def test_claim_blocked_by_unsatisfied_parents(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",))
    act(board, "move_card", {"card": "C-2", "target_status": "ready"})
    with pytest.raises(InvariantError) as excinfo:
        act(board, "claim_card", {"card": "C-2", "member": "M-1"})
    assert excinfo.value.code == "PARENTS_NOT_SATISFIED"


def test_worker_actor_cannot_impersonate_another_member(board):
    make_board(board)
    add_member(board, "M-1", role="developer")
    add_member(board, "M-2", role="developer")
    make_card(board, "C-1", "任务")

    m1_actor = Actor(actor_id="M-1", role="developer", clearance_tags={"*"})
    m2_actor = Actor(actor_id="M-2", role="developer", clearance_tags={"*"})

    # M-1 不能冒名 M-2 认领
    with pytest.raises(InvariantError) as excinfo:
        act_as(board, m1_actor, "claim_card", {"card": "C-1", "member": "M-2"})
    assert excinfo.value.code == "ACTOR_MEMBER_MISMATCH"

    # 本人 Actor 认领成功
    act_as(board, m1_actor, "claim_card", {"card": "C-1", "member": "M-1"})
    assert card_of(board, "C-1").assignee == "M-1"

    # M-2 不能冒名 M-1 登记结果
    with pytest.raises(InvariantError) as excinfo:
        act_as(
            board,
            m2_actor,
            "record_outcome",
            {"card": "C-1", "member": "M-1", "outcome": "completed", "summary": "代交"},
        )
    assert excinfo.value.code == "ACTOR_MEMBER_MISMATCH"

    # 本人交单成功
    act_as(
        board,
        m1_actor,
        "record_outcome",
        {"card": "C-1", "member": "M-1", "outcome": "completed", "summary": "完成"},
    )
    assert card_of(board, "C-1").status == "done"


# ---------------------------------------------------------------------------
# 6. 登记结果、子卡提升与阻塞升级
# ---------------------------------------------------------------------------
def test_complete_moves_to_done_and_promotes_child(board):
    make_board(board)
    add_member(board, "M-1")
    add_member(board, "M-2")
    make_card(board, "C-1", "父卡")
    make_card(board, "C-2", "子卡", waits_for=("C-1",), assignee="M-2")
    assert card_of(board, "C-2").status == "todo"

    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card": "C-1",
            "member": "M-1",
            "outcome": "completed",
            "summary": "父卡完成",
            "artifact_uri": "examples/kanban.py",
        },
    )
    c1 = card_of(board, "C-1")
    assert c1.status == "done"
    assert c1.assignee is None
    assert c1.result == "父卡完成"
    assert board.get_object(Member, "M-1", MANAGER).completed_cards == 1
    # 预指派且未超 WIP 的子卡自动进入 doing
    c2 = card_of(board, "C-2")
    assert c2.status == "doing"
    assert c2.assignee == "M-2"


def test_repeated_failure_trips_retry_limit_into_blocked(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")
    for _ in range(3):
        act(board, "claim_card", {"card": "C-1", "member": "M-1"})
        act(
            board,
            "record_outcome",
            {"card": "C-1", "member": "M-1", "outcome": "failed", "summary": "失败"},
        )
    assert card_of(board, "C-1").status == "ready"
    assert card_of(board, "C-1").consecutive_failures == 3

    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    act(
        board,
        "record_outcome",
        {"card": "C-1", "member": "M-1", "outcome": "failed", "summary": "再次失败"},
    )
    assert card_of(board, "C-1").status == "blocked"
    assert card_of(board, "C-1").consecutive_failures == 4


def test_block_and_unblock_escalation(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "写内核")

    # dependency 阻塞回到 todo
    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card": "C-1",
            "member": "M-1",
            "outcome": "blocked",
            "summary": "等前置",
            "block_kind": "dependency",
        },
    )
    assert card_of(board, "C-1").status == "todo"
    act(board, "move_card", {"card": "C-1", "target_status": "ready"})
    act(board, "unblock_card", {"card": "C-1"})

    # 反复阻塞超过上限 (2) 后进入 triage
    for _ in range(2):
        act(board, "claim_card", {"card": "C-1", "member": "M-1"})
        act(
            board,
            "record_outcome",
            {
                "card": "C-1",
                "member": "M-1",
                "outcome": "blocked",
                "summary": "还是卡",
                "block_kind": "awaiting_review",
            },
        )
        act(board, "unblock_card", {"card": "C-1"})
    assert card_of(board, "C-1").status == "triage"
    assert card_of(board, "C-1").block_recurrences == 3


def test_noncompletion_outcomes_never_land_on_done(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "卡")
    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    act(
        board,
        "record_outcome",
        {"card": "C-1", "member": "M-1", "outcome": "abandoned", "summary": "放弃"},
    )
    assert card_of(board, "C-1").status == "triage"


# ---------------------------------------------------------------------------
# 7. release_claim 与 eligible_cards / card_briefing / RLS / 审计
# ---------------------------------------------------------------------------
def test_release_claim_frees_wip_and_exhausts_retry_budget(board):
    make_board(board)
    add_member(board, "M-1", wip_limit=1)
    add_member(board, "M-2", wip_limit=1)
    make_card(board, "C-1")

    act(board, "claim_card", {"card": "C-1", "member": "M-1"})
    act(board, "release_claim", {"card": "C-1", "reason": "超时释放"})
    assert card_of(board, "C-1").status == "ready"
    assert card_of(board, "C-1").assignee is None
    assert card_of(board, "C-1").consecutive_failures == 1

    # M-1 已释放 WIP，M-2 可以接手
    act(board, "claim_card", {"card": "C-1", "member": "M-2"})
    assert card_of(board, "C-1").status == "doing"


def test_eligible_cards_returns_ready_unassigned_satisfied_cards(board):
    make_board(board)
    add_member(board, "M-1")
    make_card(board, "C-1", "低优卡", priority=1)
    make_card(board, "C-2", "高优卡", priority=10)
    make_card(board, "C-3", "依赖卡", waits_for=("C-1",))

    res = board.call_function("eligible_cards", {"board": "BD-1"}, MANAGER)
    assert [c["card_id"] for c in res["cards"]] == ["C-2", "C-1"]

    act(board, "claim_card", {"card": "C-2", "member": "M-1"})
    res2 = board.call_function("eligible_cards", {"board": "BD-1"}, MANAGER)
    assert [c["card_id"] for c in res2["cards"]] == ["C-1"]

    res_bad_role = board.call_function(
        "eligible_cards", {"board": "BD-1", "role": "coordinator"}, MANAGER
    )
    assert res_bad_role["total"] == 0


def test_briefing_comments_tenant_rls_and_audit(board):
    make_board(board, "BD-1", tenant="acme")
    make_board(board, "BD-2", tenant="other")
    add_member(board, "M-1")
    make_card(board, "C-1", "acme 卡")
    act(
        board,
        "create_card",
        {"card_id": "C-2", "board": "BD-2", "title": "other 卡"},
    )
    act(board, "add_comment", {"card": "C-1", "author": "M-1", "body": "备注"})

    other_actor = Actor(
        actor_id="intruder", role="board_manager", clearance_tags={"tenant:acme"}
    )
    assert {c.card_id for c in board.query_objects(Card, other_actor)} == {"C-1"}
    with pytest.raises(PermissionDeniedError):
        board.get_object(Card, "C-2", other_actor)

    briefing = board.call_function("card_briefing", {"card": "C-1"}, MANAGER)
    assert briefing["status"] == "ready"
    assert len(briefing["comments"]) == 1
    text = render_card_briefing(briefing)
    assert "C-1" in text and "acme 卡" in text

    with pytest.raises(InvariantError):
        make_card(board, "C-BAD", "  ")
    rejected = board.list_action_runs(status="REJECTED")
    assert rejected and rejected[-1]["error_code"] == "EMPTY_CARD_TITLE"
