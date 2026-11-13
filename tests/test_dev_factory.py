"""TK-ONTO-017 验收测试：业务纯洁研发工厂本体 + 可插拔机械臂驱动层。

覆盖：
1. ``dev_factory.py`` 的 100% 业务纯洁性静态审计（源码 + AST 导入）；
2. 招募 / 立项 / 派单 / 交卷 / 测试 / 架构终审的完整生命周期；
3. 并发目录隔离锁（同目录、父子目录冲突，异目录并行）；
4. 打回返工递增与 ``max_rework`` 熔断；
5. 无损换人 + 限流 Failover（简报完整保留历史）；
6. ``WorkshopServer`` 直接展示研发工厂实时看板。
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import urllib.request
from pathlib import Path

import pytest

from open_ontology import (
    Actor,
    InvariantError,
    OntologyRuntime,
    start_workshop_server,
)
from open_ontology.dev_factory import Deliverable, Engineer, WorkTicket, spec
from open_ontology.harness_drivers import (
    CallableHarnessDriver,
    FactoryOrchestrator,
    HarnessRateLimitError,
)

ROOT = Path(__file__).resolve().parents[1]
MANAGER = Actor(
    actor_id="factory-manager-01",
    role="factory_manager",
    clearance_tags={"*"},
)

_RUN_IDS = itertools.count(1)


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------
@pytest.fixture()
def factory() -> OntologyRuntime:
    runtime = OntologyRuntime(spec)
    yield runtime
    runtime.close()


def act(runtime: OntologyRuntime, action: str, params: dict) -> dict:
    return runtime.submit_action(
        f"TEST-{next(_RUN_IDS):05d}", action, params, MANAGER
    )


def recruit(
    runtime: OntologyRuntime,
    engineer_id: str,
    name: str,
    role: str,
    specialty: str = "general",
) -> None:
    act(
        runtime,
        "recruit_engineer",
        {
            "engineer_id": engineer_id,
            "name": name,
            "role": role,
            "specialty": specialty,
        },
    )


def open_ticket(
    runtime: OntologyRuntime,
    ticket_id: str,
    scope: str,
    *,
    require_testing: bool = False,
    max_rework: int = 5,
    title: str | None = None,
) -> None:
    act(
        runtime,
        "open_ticket",
        {
            "ticket_id": ticket_id,
            "title": title or f"工单 {ticket_id}",
            "spec_uri": f"SPEC_{ticket_id}.md",
            "isolated_scope": scope,
            "max_rework": max_rework,
            "require_testing": require_testing,
        },
    )


def dispatch(runtime: OntologyRuntime, ticket_id: str, engineer_id: str) -> None:
    act(
        runtime,
        "dispatch_ticket",
        {"ticket_id": ticket_id, "engineer_id": engineer_id},
    )


def submit(
    runtime: OntologyRuntime,
    ticket_id: str,
    engineer_id: str,
    summary: str,
) -> None:
    act(
        runtime,
        "submit_work",
        {
            "ticket_id": ticket_id,
            "engineer_id": engineer_id,
            "summary": summary,
            "artifact_uri": f"artifacts/{ticket_id}.zip",
        },
    )


def review(
    runtime: OntologyRuntime,
    ticket_id: str,
    reviewer_id: str,
    verdict: str,
    feedback: str = "",
) -> None:
    act(
        runtime,
        "review_ticket",
        {
            "ticket_id": ticket_id,
            "reviewer_id": reviewer_id,
            "verdict": verdict,
            "feedback": feedback,
        },
    )


def reassign(
    runtime: OntologyRuntime,
    ticket_id: str,
    new_engineer_id: str,
    reason: str = "hand-off",
) -> None:
    act(
        runtime,
        "reassign_ticket",
        {
            "ticket_id": ticket_id,
            "new_engineer_id": new_engineer_id,
            "reason": reason,
        },
    )


def ticket_of(runtime: OntologyRuntime, ticket_id: str) -> WorkTicket:
    return runtime.get_object(WorkTicket, ticket_id, MANAGER)


def engineer_of(runtime: OntologyRuntime, engineer_id: str) -> Engineer:
    return runtime.get_object(Engineer, engineer_id, MANAGER)


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


def test_business_purity_static_scan():
    path = ROOT / "open_ontology" / "dev_factory.py"
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()

    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in lowered, f"{token!r} leaked into dev_factory.py"
    for token in FORBIDDEN_WORDS:
        assert not re.search(rf"(?<![a-z]){token}(?![a-z])", lowered), (
            f"word {token!r} leaked into dev_factory.py"
        )

    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "typing", "pydantic", "spec"}, imported


def test_business_purity_runtime_surface():
    """本体层的公开动作 / 函数名里也不能混入宿主词汇。"""
    surface = [*spec.actions, *spec.functions, *spec.link_types]
    lowered = " ".join(surface).lower()
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in lowered
    assert set(spec.object_types) == {"Engineer", "WorkTicket", "Deliverable"}


# ---------------------------------------------------------------------------
# 2. 招募 / 立项
# ---------------------------------------------------------------------------
def test_recruit_engineer_and_role_guard(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect", "kernel")
    engineer = engineer_of(factory, "ENG-ARCH-01")
    assert engineer.availability == "IDLE"
    assert engineer.completed_tickets == 0
    assert engineer.specialty == "kernel"

    with pytest.raises(InvariantError) as exc:
        recruit(factory, "ENG-X-01", "越界角色", "manager")
    assert exc.value.code == "INVALID_ENGINEER_ROLE"

    with pytest.raises(InvariantError) as exc:
        recruit(factory, "ENG-ARCH-01", "重名", "architect")
    assert exc.value.code == "ENGINEER_ALREADY_EXISTS"


def test_open_ticket_guards(factory):
    with pytest.raises(InvariantError) as exc:
        open_ticket(factory, "TK-BAD-1", "open_ontology/", max_rework=0)
    assert exc.value.code == "INVALID_MAX_REWORK"

    with pytest.raises(InvariantError) as exc:
        open_ticket(factory, "TK-BAD-2", "   ")
    assert exc.value.code == "EMPTY_ISOLATED_SCOPE"

    open_ticket(factory, "TK-OK-1", "open_ontology/")
    ticket = ticket_of(factory, "TK-OK-1")
    assert ticket.stage == "OPEN"
    assert ticket.rework_round == 0
    assert ticket.verdict == "PENDING"

    with pytest.raises(InvariantError) as exc:
        open_ticket(factory, "TK-OK-1", "other/")
    assert exc.value.code == "TICKET_ALREADY_EXISTS"


# ---------------------------------------------------------------------------
# 3. 派单护栏
# ---------------------------------------------------------------------------
def test_dispatch_requires_coder(factory):
    recruit(factory, "ENG-TEST-01", "测试员", "tester")
    open_ticket(factory, "TK-1", "open_ontology/")

    with pytest.raises(InvariantError) as exc:
        dispatch(factory, "TK-1", "ENG-TEST-01")
    assert exc.value.code == "ASSIGNEE_MUST_BE_CODER"
    assert ticket_of(factory, "TK-1").stage == "OPEN"


def test_dispatch_requires_idle_engineer(factory):
    recruit(factory, "ENG-C1", "程序员一", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    open_ticket(factory, "TK-2", "video_saas/")
    dispatch(factory, "TK-1", "ENG-C1")

    with pytest.raises(InvariantError) as exc:
        dispatch(factory, "TK-2", "ENG-C1")
    assert exc.value.code == "ENGINEER_NOT_IDLE"


def test_dispatch_links_assignee(factory):
    recruit(factory, "ENG-C1", "程序员一", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    dispatch(factory, "TK-1", "ENG-C1")

    ticket = ticket_of(factory, "TK-1")
    engineer = engineer_of(factory, "ENG-C1")
    assert ticket.stage == "CODING"
    assert ticket.assignee_id == "ENG-C1"
    assert engineer.availability == "BUSY"
    assert engineer.active_ticket_id == "TK-1"

    linked = factory.get_linked(ticket, "ticket_assigned_to", Engineer, MANAGER)
    assert [row.engineer_id for row in linked] == ["ENG-C1"]


# ---------------------------------------------------------------------------
# 4. 全生命周期
# ---------------------------------------------------------------------------
def test_full_lifecycle_to_done(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    recruit(factory, "ENG-TEST-01", "测试员", "tester")
    open_ticket(
        factory,
        "TK-ONTO-017",
        "open_ontology/",
        require_testing=True,
        title="研发工厂本体",
    )
    dispatch(factory, "TK-ONTO-017", "ENG-C1")

    submit(factory, "TK-ONTO-017", "ENG-C1", "实现完成，含 16 条测试")
    ticket = ticket_of(factory, "TK-ONTO-017")
    assert ticket.stage == "TESTING"
    assert ticket.latest_submission == "实现完成，含 16 条测试"
    assert ticket.verdict == "PENDING"
    assert engineer_of(factory, "ENG-C1").availability == "IDLE"

    review(factory, "TK-ONTO-017", "ENG-TEST-01", "PASS", "单测全绿")
    ticket = ticket_of(factory, "TK-ONTO-017")
    assert ticket.stage == "ARCH_REVIEW"
    assert ticket.reviewer_id == "ENG-TEST-01"

    review(factory, "TK-ONTO-017", "ENG-ARCH-01", "PASS", "架构验收通过")
    ticket = ticket_of(factory, "TK-ONTO-017")
    assert ticket.stage == "DONE"
    assert ticket.verdict == "PASS"

    deliverables = factory.query_objects(Deliverable, MANAGER)
    assert len(deliverables) == 1
    assert deliverables[0].verified is True
    assert deliverables[0].round_index == 0
    assert deliverables[0].engineer_id == "ENG-C1"

    assert engineer_of(factory, "ENG-C1").completed_tickets == 1


# ---------------------------------------------------------------------------
# 5. 并发目录隔离锁
# ---------------------------------------------------------------------------
def test_workspace_scope_conflict_same_directory(factory):
    recruit(factory, "ENG-C1", "程序员一", "coder")
    recruit(factory, "ENG-C2", "程序员二", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    open_ticket(factory, "TK-2", "open_ontology/")
    dispatch(factory, "TK-1", "ENG-C1")

    with pytest.raises(InvariantError) as exc:
        dispatch(factory, "TK-2", "ENG-C2")
    assert exc.value.code == "WORKSPACE_SCOPE_CONFLICT"
    assert ticket_of(factory, "TK-2").stage == "OPEN"
    assert engineer_of(factory, "ENG-C2").availability == "IDLE"


def test_workspace_scope_conflict_parent_child(factory):
    recruit(factory, "ENG-C1", "程序员一", "coder")
    recruit(factory, "ENG-C2", "程序员二", "coder")
    recruit(factory, "ENG-C3", "程序员三", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    open_ticket(factory, "TK-2", "open_ontology/sub/")
    dispatch(factory, "TK-1", "ENG-C1")

    with pytest.raises(InvariantError) as exc:
        dispatch(factory, "TK-2", "ENG-C2")
    assert exc.value.code == "WORKSPACE_SCOPE_CONFLICT"

    # 反向父子关系：先占窄目录，再派其父目录同样冲突
    open_ticket(factory, "TK-3", "video_saas/render/")
    open_ticket(factory, "TK-4", "video_saas/")
    dispatch(factory, "TK-3", "ENG-C2")
    with pytest.raises(InvariantError) as exc:
        dispatch(factory, "TK-4", "ENG-C3")
    assert exc.value.code == "WORKSPACE_SCOPE_CONFLICT"


def test_parallel_different_scopes_allowed(factory):
    recruit(factory, "ENG-C1", "程序员一", "coder")
    recruit(factory, "ENG-C2", "程序员二", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    open_ticket(factory, "TK-2", "video_saas/")

    dispatch(factory, "TK-1", "ENG-C1")
    dispatch(factory, "TK-2", "ENG-C2")

    assert ticket_of(factory, "TK-1").stage == "CODING"
    assert ticket_of(factory, "TK-2").stage == "CODING"


# ---------------------------------------------------------------------------
# 6. 返工与熔断
# ---------------------------------------------------------------------------
def test_rework_round_increments_and_reattaches(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    dispatch(factory, "TK-1", "ENG-C1")
    submit(factory, "TK-1", "ENG-C1", "第一版")

    review(factory, "TK-1", "ENG-ARCH-01", "FAIL", "缺少边界测试")
    ticket = ticket_of(factory, "TK-1")
    assert ticket.stage == "CODING"
    assert ticket.rework_round == 1
    assert ticket.verdict == "FAIL"
    assert ticket.latest_feedback == "缺少边界测试"
    assert engineer_of(factory, "ENG-C1").availability == "BUSY"

    submit(factory, "TK-1", "ENG-C1", "第二版：补齐边界测试")
    deliverables = sorted(
        factory.query_objects(Deliverable, MANAGER),
        key=lambda row: row.round_index,
    )
    assert [row.round_index for row in deliverables] == [0, 1]

    review(factory, "TK-1", "ENG-ARCH-01", "PASS", "通过")
    assert ticket_of(factory, "TK-1").stage == "DONE"
    assert engineer_of(factory, "ENG-C1").completed_tickets == 1


def test_rework_circuit_breaker(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    open_ticket(factory, "TK-1", "open_ontology/", max_rework=2)
    dispatch(factory, "TK-1", "ENG-C1")

    for round_index in range(3):
        submit(factory, "TK-1", "ENG-C1", f"第 {round_index} 版")
        review(factory, "TK-1", "ENG-ARCH-01", "FAIL", f"第 {round_index} 次打回")

    ticket = ticket_of(factory, "TK-1")
    assert ticket.stage == "BLOCKED"
    assert ticket.rework_round == 2
    assert ticket.verdict == "FAIL"

    board = factory.call_function("factory_board_summary", {}, MANAGER)
    assert board["tickets"]["blocked"] == ["TK-1"]


def test_review_role_guard(factory):
    recruit(factory, "ENG-TEST-01", "测试员", "tester")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    dispatch(factory, "TK-1", "ENG-C1")
    submit(factory, "TK-1", "ENG-C1", "第一版")

    with pytest.raises(InvariantError) as exc:
        review(factory, "TK-1", "ENG-C1", "PASS", "自评")
    assert exc.value.code == "REVIEWER_ROLE_NOT_ALLOWED"

    with pytest.raises(InvariantError) as exc:
        review(factory, "TK-1", "ENG-TEST-01", "PASS", "程序员不能评审架构终审")
    # 测试员可以评 TESTING，但当前在 ARCH_REVIEW，角色不匹配
    assert exc.value.code == "REVIEWER_ROLE_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# 7. 无损换人（零失忆）
# ---------------------------------------------------------------------------
def test_reassign_preserves_full_context(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    recruit(factory, "ENG-C2", "核心程序员B", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    dispatch(factory, "TK-1", "ENG-C1")
    submit(factory, "TK-1", "ENG-C1", "第一版实现")
    review(factory, "TK-1", "ENG-ARCH-01", "FAIL", "错误处理不完整")

    reassign(factory, "TK-1", "ENG-C2", "原承接人请假")

    ticket = ticket_of(factory, "TK-1")
    assert ticket.stage == "CODING"
    assert ticket.assignee_id == "ENG-C2"
    assert ticket.rework_round == 1
    assert ticket.latest_submission == "第一版实现"
    assert ticket.latest_feedback == "错误处理不完整"

    old = engineer_of(factory, "ENG-C1")
    new = engineer_of(factory, "ENG-C2")
    assert old.availability == "UNAVAILABLE"
    assert old.active_ticket_id is None
    assert new.availability == "BUSY"
    assert new.active_ticket_id == "TK-1"

    briefing = factory.call_function(
        "build_engineer_briefing", {"ticket_id": "TK-1"}, MANAGER
    )
    assert briefing["spec_uri"] == "SPEC_TK-1.md"
    assert briefing["isolated_scope"] == "open_ontology/"
    assert briefing["rework_round"] == 1
    assert briefing["previous_submission"] == "第一版实现"
    assert briefing["review_feedback"] == "错误处理不完整"
    assert len(briefing["history"]) == 1
    assert briefing["history"][0]["engineer_id"] == "ENG-C1"

    submit(factory, "TK-1", "ENG-C2", "第二版：补齐错误处理")
    review(factory, "TK-1", "ENG-ARCH-01", "PASS", "通过")
    assert ticket_of(factory, "TK-1").stage == "DONE"
    assert engineer_of(factory, "ENG-C2").completed_tickets == 1
    assert engineer_of(factory, "ENG-C1").completed_tickets == 0


# ---------------------------------------------------------------------------
# 8. 看板与简报
# ---------------------------------------------------------------------------
def test_factory_board_summary(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    recruit(factory, "ENG-C2", "核心程序员B", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    open_ticket(factory, "TK-2", "video_saas/")
    open_ticket(factory, "TK-3", "docs/")
    dispatch(factory, "TK-1", "ENG-C1")

    board = factory.call_function("factory_board_summary", {}, MANAGER)
    assert board["engineers"]["total"] == 3
    assert board["engineers"]["by_role"]["coder"] == 2
    assert board["engineers"]["busy"] == ["ENG-C1"]
    assert board["tickets"]["by_stage"]["CODING"] == 1
    assert board["tickets"]["by_stage"]["OPEN"] == 2
    assert board["active_workspace_locks"] == [
        {"ticket_id": "TK-1", "scope": "open_ontology", "engineer_id": "ENG-C1"}
    ]

    submit(factory, "TK-1", "ENG-C1", "交卷")
    review(factory, "TK-1", "ENG-ARCH-01", "PASS", "通过")
    board = factory.call_function("factory_board_summary", {}, MANAGER)
    assert board["tickets"]["by_stage"]["DONE"] == 1
    assert board["deliverables"] == {"total": 1, "verified": 1}
    assert board["active_workspace_locks"] == []


def test_build_engineer_briefing_includes_history(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    open_ticket(
        factory,
        "TK-1",
        "open_ontology/",
        require_testing=True,
        title="核心 MCTS",
    )
    dispatch(factory, "TK-1", "ENG-C1")
    submit(factory, "TK-1", "ENG-C1", "MCTS 内核完成")
    review(factory, "TK-1", "ENG-ARCH-01", "FAIL", "需要补充并发测试")

    briefing = factory.call_function(
        "build_engineer_briefing", {"ticket_id": "TK-1"}, MANAGER
    )
    assert briefing["title"] == "核心 MCTS"
    assert briefing["require_testing"] is True
    assert briefing["assignee_id"] == "ENG-C1"
    assert briefing["assignee_name"] == "核心程序员A"
    assert briefing["history"][0]["summary"] == "MCTS 内核完成"
    assert briefing["history"][0]["artifact_uri"] == "artifacts/TK-1.zip"

    from open_ontology.dev_factory import render_briefing

    text = render_briefing(briefing)
    assert "SPEC_TK-1.md" in text
    assert "需要补充并发测试" in text


# ---------------------------------------------------------------------------
# 9. 限流 Failover：自动换人且零失忆
# ---------------------------------------------------------------------------
def test_rate_limit_failover_via_orchestrator(factory, tmp_path):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    recruit(factory, "ENG-C2", "核心程序员B", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")
    dispatch(factory, "TK-1", "ENG-C1")

    def limited(engineer_id: str, briefing: str, workspace: str) -> str:
        raise HarnessRateLimitError("429 too many requests")

    primary = CallableHarnessDriver(fn=limited, name="primary")
    backup = CallableHarnessDriver(
        fn=lambda engineer_id, briefing, workspace: f"{engineer_id} 已完成交付",
        name="backup",
    )

    orchestrator = FactoryOrchestrator(factory, actor=MANAGER, workspace_root=tmp_path)
    orchestrator.register(primary)
    orchestrator.register(backup)
    orchestrator.bind("ENG-C1", "primary")
    orchestrator.bind("ENG-C2", "backup")

    result = orchestrator.step_ticket("TK-1", fallback_engineer_id="ENG-C2")

    assert result["engineer_id"] == "ENG-C2"
    assert result["driver"] == "backup"
    assert len(result["failovers"]) == 1
    assert result["failovers"][0]["error"] == "HarnessRateLimitError"

    ticket = ticket_of(factory, "TK-1")
    assert ticket.stage == "ARCH_REVIEW"
    assert ticket.assignee_id == "ENG-C2"
    assert ticket.latest_submission == "ENG-C2 已完成交付"

    assert engineer_of(factory, "ENG-C1").availability == "UNAVAILABLE"
    assert engineer_of(factory, "ENG-C2").availability == "IDLE"

    deliverables = factory.query_objects(Deliverable, MANAGER)
    assert [row.engineer_id for row in deliverables] == ["ENG-C2"]

    briefing = factory.call_function(
        "build_engineer_briefing", {"ticket_id": "TK-1"}, MANAGER
    )
    assert briefing["assignee_id"] == "ENG-C2"
    assert briefing["history"][0]["summary"] == "ENG-C2 已完成交付"

    run_types = [run["action_type"] for run in factory.list_action_runs()]
    assert "reassign_ticket" in run_types
    assert "submit_work" in run_types

    review(factory, "TK-1", "ENG-ARCH-01", "PASS", "接力交付通过")
    assert ticket_of(factory, "TK-1").stage == "DONE"
    assert engineer_of(factory, "ENG-C2").completed_tickets == 1


def test_orchestrator_refuses_non_coding_ticket(factory, tmp_path):
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    open_ticket(factory, "TK-1", "open_ontology/")

    orchestrator = FactoryOrchestrator(
        factory,
        actor=MANAGER,
        workspace_root=tmp_path,
        drivers={"only": CallableHarnessDriver(fn=lambda e, b, w: "ok", name="only")},
    )
    with pytest.raises(Exception) as exc:
        orchestrator.step_ticket("TK-1")
    assert "not CODING" in str(exc.value)


# ---------------------------------------------------------------------------
# 10. WorkshopServer 看板
# ---------------------------------------------------------------------------
def test_workshop_board_shows_factory(factory):
    recruit(factory, "ENG-ARCH-01", "首席架构师", "architect")
    recruit(factory, "ENG-C1", "核心程序员A", "coder")
    open_ticket(factory, "TK-1", "open_ontology/", title="研发工厂本体")
    dispatch(factory, "TK-1", "ENG-C1")

    handle = start_workshop_server(factory, port=0, title="研发工厂作战台")
    try:
        with urllib.request.urlopen(f"{handle.base_url}/", timeout=10) as response:
            html = response.read().decode("utf-8")
        assert "研发工厂作战台" in html

        with urllib.request.urlopen(
            f"{handle.base_url}/api/meta", timeout=10
        ) as response:
            meta = json.loads(response.read().decode("utf-8"))
        names = {row["name"] for row in meta["object_types"]}
        assert {"Engineer", "WorkTicket", "Deliverable"} <= names
        assert {"recruit_engineer", "dispatch_ticket"} <= {
            row["name"] for row in meta["actions"]
        }

        with urllib.request.urlopen(
            f"{handle.base_url}/api/objects/WorkTicket", timeout=10
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["count"] == 1
        assert payload["objects"][0]["ticket_id"] == "TK-1"
        assert payload["objects"][0]["stage"] == "CODING"

        with urllib.request.urlopen(
            f"{handle.base_url}/api/objects/Engineer", timeout=10
        ) as response:
            engineers = json.loads(response.read().decode("utf-8"))
        assert engineers["count"] == 2
    finally:
        handle.stop()
