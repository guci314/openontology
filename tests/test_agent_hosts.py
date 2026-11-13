"""TK-ONTO-022 验收测试：智能体宿主适配层（PSM）。

覆盖：
1. 结构化输出契约本身的形状（宿主、本体、裁判共用同一份）；
2. CodeBuddy 输出解析（``--output-format json`` 的 ``structured_output``）；
3. OpenCode 输出解析（倒序消息里取助手正文，再抠 JSON）；
4. 抠 JSON 的边界（正文里有解释、有别人的 JSON、括号在字符串里）；
5. :class:`BoardRunner` 端到端（假宿主）：认领 → 执行 → 登记结果；
6. **护栏仍然生效**：宿主给出没有数字的证据时，本体拒绝收单，
   这一轮不会被记成成功；
7. 真宿主冒烟（默认跳过，需显式开启环境变量）。
"""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
from pathlib import Path

import pytest

from open_ontology import Actor, ObjectType, OntologyRuntime, OntologySpec
from open_ontology.agent_hosts import (
    RESULT_SCHEMA,
    AmbiguousTakeoverError,
    BoardRunner,
    CallableHost,
    CodeBuddyHost,
    HostResult,
    HostTask,
    OpenCodeHost,
    RoleRoutedHost,
    extract_json_object,
    find_ontology_sqlite_files,
    locate_object_in_databases,
    parse_codebuddy_output,
    parse_opencode_messages,
    takeover_board,
    takeover_board_from_db,
    takeover_workspace,
)
from open_ontology.kanban import Board, Card, spec
from open_ontology.workspaces import (
    MANIFEST_FILENAME,
    IncompleteKernelDatabaseError,
    WorkspaceCatalog,
    WorkspaceDatabaseMissingError,
    WorkspaceDescriptor,
    WorkspaceModelUnavailableError,
    WorkspaceNotFoundError,
)

MANAGER = Actor(
    actor_id="board-manager-01",
    role="board_manager",
    clearance_tags={"*"},
)

_RUN = [0]


def act(runtime: OntologyRuntime, action: str, params: dict) -> dict:
    _RUN[0] += 1
    return runtime.submit_action(f"T-{_RUN[0]:05d}", action, params, MANAGER)


@pytest.fixture()
def board() -> OntologyRuntime:
    runtime = OntologyRuntime(spec)
    yield runtime
    runtime.close()


def columns() -> list[dict]:
    return [
        {"key": "todo", "label": "待办", "purpose": "initial", "order": 0,
         "allowed_targets": ("ready",)},
        {"key": "ready", "label": "可认领", "purpose": "ready", "order": 1,
         "claimable": True, "allowed_roles": ("developer",),
         "allowed_targets": ("todo",)},
        {"key": "doing", "label": "执行中", "purpose": "working", "order": 2},
        {"key": "done", "label": "已完成", "purpose": "completion", "order": 3,
         "is_terminal": True},
        {"key": "blocked", "label": "阻塞", "purpose": "failure", "order": 4,
         "max_retries": 2},
        {"key": "triage", "label": "分诊", "purpose": "escalation", "order": 5},
    ]


def setup_board(runtime: OntologyRuntime) -> None:
    act(
        runtime,
        "create_board",
        {
            "board_key": "BD-1",
            "title": "研发看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
            "columns": columns(),
        },
    )
    act(
        runtime,
        "add_member",
        {"member_id": "M-1", "name": "开发者", "role": "developer"},
    )
    act(
        runtime,
        "create_card",
        {"card_id": "C-1", "board_key": "BD-1", "title": "写内核"},
    )


# ---------------------------------------------------------------------------
# 1. 契约形状
# ---------------------------------------------------------------------------
def test_result_schema_is_strict_about_numbers_and_verdict():
    assert RESULT_SCHEMA["required"] == ["status", "summary", "evidence"]
    assert RESULT_SCHEMA["properties"]["status"]["enum"] == [
        "completed",
        "blocked",
        "failed",
    ]
    assert RESULT_SCHEMA["properties"]["verdict"]["enum"] == ["PASS", "FAIL"]
    evidence = RESULT_SCHEMA["properties"]["evidence"]
    assert evidence["items"]["required"] == ["metric", "value"]


# ---------------------------------------------------------------------------
# 2. CodeBuddy 输出解析
# ---------------------------------------------------------------------------
def test_parse_codebuddy_structured_output():
    raw = json.dumps(
        [
            {"type": "message", "role": "user", "content": []},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "",
                "session_id": "sess-1",
                "structured_output": {
                    "status": "completed",
                    "verdict": "PASS",
                    "summary": "内核写完",
                    "evidence": [
                        {"metric": "测试条数", "value": "52 passed", "detail": ""}
                    ],
                },
            },
        ],
        ensure_ascii=False,
    )
    result = parse_codebuddy_output(raw)
    assert result.status == "completed"
    assert result.verdict == "PASS"
    assert result.summary == "内核写完"
    assert result.evidence[0]["value"] == "52 passed"
    assert result.session_id == "sess-1"
    assert result.ok is True


def test_parse_codebuddy_without_structured_output_falls_back_to_text():
    raw = json.dumps(
        [
            {
                "type": "result",
                "is_error": False,
                "result": '{"status":"completed","verdict":"PASS","summary":"x",'
                '"evidence":[{"metric":"m","value":"1 passed"}]}',
                "session_id": "sess-2",
            }
        ],
        ensure_ascii=False,
    )
    result = parse_codebuddy_output(raw)
    assert result.verdict == "PASS"
    assert result.evidence[0]["metric"] == "m"


def test_parse_codebuddy_invalid_json_is_failed_not_crash():
    result = parse_codebuddy_output("not json at all")
    assert result.status == "failed"
    assert result.error and result.error.startswith("HOST_OUTPUT_NOT_JSON")


def test_parse_codebuddy_rejects_unknown_status():
    raw = json.dumps(
        [
            {
                "type": "result",
                "session_id": "s",
                "structured_output": {
                    "status": "PASS",
                    "summary": "x",
                    "evidence": [],
                },
            }
        ],
        ensure_ascii=False,
    )
    result = parse_codebuddy_output(raw)
    assert result.status == "failed"
    assert result.error == "INVALID_HOST_STATUS:'PASS'"


# ---------------------------------------------------------------------------
# 3. OpenCode 输出解析
# ---------------------------------------------------------------------------
def test_parse_opencode_messages_takes_newest_assistant_text():
    raw = json.dumps(
        {
            "data": [
                {
                    "type": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "想一下"},
                        {
                            "type": "text",
                            "text": '{"status":"completed","verdict":"PASS",'
                            '"summary":"写完","evidence":[{"metric":"测试条数",'
                            '"value":"12 passed"}]}',
                        },
                    ],
                },
                {"type": "user", "content": None},
                {"type": "idle"},
            ]
        },
        ensure_ascii=False,
    )
    result = parse_opencode_messages(raw)
    assert result.verdict == "PASS"
    assert result.evidence[0]["value"] == "12 passed"


def test_parse_opencode_without_json_is_failed():
    raw = json.dumps(
        {"data": [{"type": "assistant", "content": [{"type": "text", "text": "做完了"}]}]}
    )
    result = parse_opencode_messages(raw)
    assert result.status == "failed"
    assert result.error == "HOST_OUTPUT_WITHOUT_JSON"


# ---------------------------------------------------------------------------
# 4. 抠 JSON 的边界
# ---------------------------------------------------------------------------
def test_extract_json_object_takes_the_last_object():
    text = '先说 {"a":1} ，最终结果：{"status":"completed"}'
    assert extract_json_object(text) == {"status": "completed"}


def test_extract_json_object_handles_braces_inside_strings():
    text = '{"summary":"用了 {x} 这个写法","evidence":[]}'
    assert extract_json_object(text)["summary"] == "用了 {x} 这个写法"


def test_extract_json_object_returns_none_without_json():
    assert extract_json_object("完全没有 JSON") is None


# ---------------------------------------------------------------------------
# 5. BoardRunner 端到端（假宿主）
# ---------------------------------------------------------------------------
def test_runner_claims_runs_and_records_completion(board, tmp_path):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        assert "C-1" in task.briefing
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="内核写完",
            evidence=({"metric": "测试条数", "value": "52 passed"},),
            artifact_uri="file:///tmp/report.md",
            board_state="新基线：内核 52 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")

    assert outcome["status"] == "completed"
    assert outcome["verdict"] == "PASS"
    assert outcome["rejected_by_ontology"] is None

    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "done"
    assert card.result == "内核写完"
    assert card.artifact_uri == "file:///tmp/report.md"

    runs = board.list_action_runs(status="COMMITTED")
    kinds = [row["action_type"] for row in runs]
    assert kinds[-2:] == ["claim_card", "record_outcome"]


def test_runner_reports_blocked_result(board, tmp_path):
    setup_board(board)

    def blocked_host(task: HostTask) -> HostResult:
        return HostResult(
            status="blocked",
            summary="需要人审",
            evidence=({"metric": "已跑用例", "value": "3 passed"},),
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(blocked_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")
    assert outcome["outcome_recorded"] == "blocked"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "blocked"
    assert card.block_kind == "awaiting_review"


def test_runner_maps_fail_verdict_to_failed_outcome(board, tmp_path):
    setup_board(board)

    def fail_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="FAIL",
            summary="测试没过",
            evidence=({"metric": "失败用例", "value": "2 failed"},),
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fail_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")
    assert outcome["outcome_recorded"] == "failed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "ready"
    assert card.consecutive_failures == 1


# ---------------------------------------------------------------------------
# 6. 护栏仍然生效
# ---------------------------------------------------------------------------
def test_ontology_rejects_evidence_without_numbers(board, tmp_path):
    setup_board(board)

    def sloppy_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="全部搞定",
            evidence=({"metric": "测试条数", "value": "全绿"},),
            board_state="新基线：全绿",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(sloppy_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")

    assert outcome["rejected_by_ontology"] == "InvariantError:EVIDENCE_WITHOUT_NUMBER"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key != "done"
    rejected = board.list_action_runs(status="REJECTED")
    assert rejected and rejected[-1]["error_code"] == "EVIDENCE_WITHOUT_NUMBER"


def test_ontology_rejects_completion_without_evidence(board, tmp_path):
    setup_board(board)

    def talkative_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="做完了",
            board_state="新基线：做完了",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(talkative_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")
    assert outcome["rejected_by_ontology"] == "InvariantError:MISSING_EVIDENCE"


# ---------------------------------------------------------------------------
# 7. 真宿主冒烟（默认跳过）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("OPENONTOLOGY_LIVE_HOST") != "1",
    reason="需要真实宿主与额度：OPENONTOLOGY_LIVE_HOST=1 才跑",
)
def test_live_codebuddy_host_returns_structured_verdict(tmp_path):
    host = CodeBuddyHost(model="deepseek-v4.1-flash")
    task = HostTask(
        task_id="LIVE-1",
        briefing=(
            "这是一个连通性探针，不要使用任何工具，不要读写任何文件。"
            "直接给出结论：把 1 和 2 相加，答案是 3。"
        ),
        workspace_dir=str(tmp_path),
        max_turns=2,
        timeout=300.0,
    )
    result = host.run(task)
    assert result.status == "completed", result.error
    assert result.verdict == "PASS"
    assert result.evidence and any(
        "3" in item["value"] for item in result.evidence
    )


@pytest.mark.skipif(
    os.environ.get("OPENONTOLOGY_LIVE_HOST") != "1",
    reason="需要真实宿主与额度：OPENONTOLOGY_LIVE_HOST=1 才跑",
)
def test_live_opencode_host_returns_structured_verdict(tmp_path):
    host = OpenCodeHost()
    task = HostTask(
        task_id="LIVE-2",
        briefing=(
            "这是一个连通性探针，不要使用任何工具，不要读写任何文件。"
            "把 1 和 2 相加，答案是 3。"
        ),
        workspace_dir=str(tmp_path),
        timeout=300.0,
    )
    result = host.run(task)
    assert result.status == "completed", result.error
    assert result.verdict == "PASS"
    assert result.evidence


# ---------------------------------------------------------------------------
# 8. 会话世代轮转（Session Epoch Rotation）
# ---------------------------------------------------------------------------
class FakeTransportHost(OpenCodeHost):
    """把传输层换成内存假实现，专测轮转逻辑本身。

    只覆盖 ``_request``，其余（轮转判定、交接注入、世代计数）走真实代码。
    """

    def __init__(self, *args, tokens_per_session: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.created: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.tokens_per_session = tokens_per_session
        self._counter = 0
        self._calls = 0

    def _request(self, method: str, path: str, payload=None) -> str:
        if method.lower() == "post" and path == "/api/session":
            self._counter += 1
            sid = f"ses_fake_{self._counter}"
            self.created.append(sid)
            return json.dumps({"data": {"id": sid, "title": (payload or {}).get("title")}})
        if method.lower() == "post" and path.endswith("/move"):
            return json.dumps({"data": {}})
        if method.lower() == "post" and path.endswith("/wait"):
            return json.dumps({"data": {}})
        if method.lower() == "post" and path.endswith("/prompt"):
            sid = path.split("/")[-2]
            self.prompts.append((sid, (payload or {}).get("text", "")))
            return json.dumps({"data": {}})
        if method.lower() == "get" and path.endswith("/message"):
            sid = path.split("/")[-2]
            return json.dumps(
                {
                    "data": [
                        {
                            "type": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "status": "completed",
                                            "verdict": "PASS",
                                            "summary": f"完成 {sid}",
                                            "evidence": [
                                                {"metric": "测试条数", "value": "3 passed"}
                                            ],
                                            "board_state": f"新基线：{sid} 3 passed",
                                            "goal_changed": False,
                                            "goal_review_reason": "目标未变。",
                                        },
                                        ensure_ascii=False,
                                    ),
                                }
                            ],
                        }
                    ]
                }
            )
        if method.lower() == "get" and path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[-1]
            self._calls += 1
            return json.dumps(
                {
                    "data": {
                        "id": sid,
                        "tokens": {
                            "input": self.tokens_per_session,
                            "output": 0,
                            "reasoning": 0,
                        },
                    }
                }
            )
        raise RuntimeError(f"unexpected request {method} {path}")


def _task(task_id: str, role: str = "") -> HostTask:
    return HostTask(task_id=task_id, briefing=f"做 {task_id}", workspace_dir="", role=role)


def test_opencode_host_rotates_every_n_tasks():
    host = FakeTransportHost(rotate_every_n_tasks=2, handover_briefing="项目准则摘要")
    r1 = host.run(_task("C-1"))
    r2 = host.run(_task("C-2"))
    r3 = host.run(_task("C-3"))

    # 前两个复用同一 Session，第三个才换新世代
    assert r1.session_id == r2.session_id
    assert r3.session_id != r1.session_id
    assert len(host.session_history) == 2
    assert host.session_history == [r1.session_id, r3.session_id]

    # 交接摘要只在新世代首轮注入一次
    first_text = dict(host.prompts)[r1.session_id] if False else None
    texts_by_sid: dict[str, list[str]] = {}
    for sid, text in host.prompts:
        texts_by_sid.setdefault(sid, []).append(text)
    assert sum("项目准则摘要" in t for t in texts_by_sid[r1.session_id]) == 1
    assert sum("项目准则摘要" in t for t in texts_by_sid[r3.session_id]) == 1
    assert "项目准则摘要" in texts_by_sid[r1.session_id][0]
    assert "项目准则摘要" not in texts_by_sid[r1.session_id][1]


def test_opencode_host_rotates_on_token_limit():
    """未满次数阈值，但 token 超限也要立刻换代。"""
    host = FakeTransportHost(rotate_every_n_tasks=10, max_session_tokens=50_000)

    class TokenedHost(FakeTransportHost):
        def _get_session_tokens(self, session_id: str) -> int:
            return 60_000 if session_id else 0

    host = TokenedHost(rotate_every_n_tasks=10, max_session_tokens=50_000)
    r1 = host.run(_task("C-1"))
    r2 = host.run(_task("C-2"))
    assert r1.session_id != r2.session_id
    assert len(host.session_history) == 2


def test_opencode_host_without_rotation_creates_session_per_task():
    """未配置轮转时保持原行为：每个任务新建会话（向后兼容）。"""
    host = FakeTransportHost()
    r1 = host.run(_task("C-1"))
    r2 = host.run(_task("C-2"))
    assert r1.session_id != r2.session_id
    assert len(host.session_history) == 2


def test_opencode_host_resume_session_bypasses_rotation():
    host = FakeTransportHost(rotate_every_n_tasks=1)
    host.run(_task("C-1"))
    resumed = host.run(
        HostTask(task_id="C-2", briefing="x", workspace_dir="", resume_session_id="ses_explicit")
    )
    assert resumed.session_id == "ses_explicit"


def test_opencode_host_session_tokens_falls_back_to_zero():
    host = FakeTransportHost()
    assert host._get_session_tokens("") == 0


# ---------------------------------------------------------------------------
# 9. 按角色路由宿主
# ---------------------------------------------------------------------------
def test_role_routed_host_dispatches_by_role():
    from open_ontology.agent_hosts import RoleRoutedHost

    seen: list[str] = []

    def maker(tag):
        def fn(task: HostTask) -> HostResult:
            seen.append(f"{tag}:{task.role}")
            return HostResult(
                status="completed",
                verdict="PASS",
                summary=f"{tag} 完成",
                evidence=({"metric": "测试条数", "value": "1 passed"},),
            )

        return CallableHost(fn)

    routed = RoleRoutedHost(
        routes={"architect": maker("argon"), "developer": maker("codebuddy")},
        default_host=maker("fallback"),
    )
    routed.run(_task("C-1", role="architect"))
    routed.run(_task("C-2", role="developer"))
    routed.run(_task("C-3", role="tester"))
    assert seen == ["argon:architect", "codebuddy:developer", "fallback:tester"]


def test_role_routed_host_factory_wires_argon_and_codebuddy():
    from open_ontology.agent_hosts import RoleRoutedHost

    routed = RoleRoutedHost.argon_and_codebuddy()
    assert isinstance(routed.host_for("architect"), OpenCodeHost)
    assert isinstance(routed.host_for("reviewer"), OpenCodeHost)
    assert isinstance(routed.host_for("developer"), CodeBuddyHost)
    assert isinstance(routed.host_for("tester"), CodeBuddyHost)

    argon = routed.host_for("architect")
    assert argon.model == "opencode-go/gemini-4-argon"
    # 默认不开启世代轮转：会话何时开新窗由人类手动决定。
    assert argon.rotate_every_n_tasks is None
    assert argon.max_session_tokens is None
    assert routed.host_for("developer").model == "deepseek-v4.1-flash"


def test_role_routed_host_returns_error_for_unrouted_role():
    from open_ontology.agent_hosts import RoleRoutedHost

    routed = RoleRoutedHost(routes={}, default_host=None)
    result = routed.run(_task("C-1", role="unknown"))
    assert result.status == "failed"
    assert result.error and result.error.startswith("HOST_NOT_ROUTED")


# ---------------------------------------------------------------------------
# 10. BoardRunner × RoleRoutedHost 端到端
# ---------------------------------------------------------------------------
def test_board_runner_routes_developer_and_reviewer_to_different_hosts(
    board, tmp_path
):
    """developer 走 CodeBuddy 分支，reviewer 走 Argon 分支且按阈值轮转。"""
    from open_ontology.agent_hosts import RoleRoutedHost

    specs = columns()
    for column in specs:
        if column["purpose"] == "ready":
            column["allowed_roles"] = ("developer", "reviewer")
    act(
        board,
        "create_board",
        {
            "board_key": "BD-1",
            "title": "研发看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
            "columns": specs,
        },
    )
    act(board, "add_member", {"member_id": "M-DEV", "name": "开发", "role": "developer"})
    act(board, "add_member", {"member_id": "M-REV", "name": "评审", "role": "reviewer"})
    act(board, "create_card", {"card_id": "C-DEV", "board_key": "BD-1", "title": "写内核"})
    act(board, "create_card", {"card_id": "C-REV", "board_key": "BD-1", "title": "评审内核"})

    argon = FakeTransportHost(rotate_every_n_tasks=2)
    worker_calls: list[str] = []

    def worker_fn(task: HostTask) -> HostResult:
        worker_calls.append(task.role)
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="CodeBuddy 完成",
            evidence=({"metric": "测试条数", "value": "5 passed"},),
            board_state="新基线：5 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    routed = RoleRoutedHost(
        routes={"developer": CallableHost(worker_fn), "reviewer": argon},
        default_host=CallableHost(worker_fn),
    )
    runner = BoardRunner(runtime=board, host=routed, workspace_root=str(tmp_path))

    out_dev = runner.run_card("C-DEV", "M-DEV")
    assert out_dev["status"] == "completed"
    assert worker_calls == ["developer"]
    assert board.get_object(Card, "C-DEV", MANAGER).column_key == "done"

    out_rev = runner.run_card("C-REV", "M-REV")
    assert out_rev["status"] == "completed"
    # reviewer 卡走的是 Argon（OpenCodeHost），已创建世代会话
    assert len(argon.session_history) == 1
    assert out_rev["session_id"] == argon.session_history[0]
    assert board.get_object(Card, "C-REV", MANAGER).column_key == "done"


# ---------------------------------------------------------------------------
# 11. Model.Ref 载荷（OpenCode 只接受 {"providerID", "id"} 或 None）
# ---------------------------------------------------------------------------
def test_opencode_host_to_model_ref_splits_string():
    assert OpenCodeHost.to_model_ref("opencode-go/gemini-4-argon") == {
        "providerID": "opencode-go",
        "id": "gemini-4-argon",
    }


def test_opencode_host_to_model_ref_passes_dict():
    given = {"providerID": "opencode-go", "id": "gemini-4-argon"}
    assert OpenCodeHost.to_model_ref(given) == given


def test_opencode_host_to_model_ref_handles_empty():
    assert OpenCodeHost.to_model_ref(None) is None
    assert OpenCodeHost.to_model_ref("") is None
    assert OpenCodeHost.to_model_ref("no-slash") is None
    assert OpenCodeHost.to_model_ref({"providerID": "x"}) is None


def test_opencode_host_sends_model_ref_not_string():
    """prompt 与 session 创建都必须发 Model.Ref 字典，不能发字符串。"""
    sent: list[dict] = []

    class RecordingHost(FakeTransportHost):
        def _request(self, method: str, path: str, payload=None) -> str:
            if payload and "model" in payload:
                sent.append({"path": path, "model": payload["model"]})
            return super()._request(method, path, payload)

    host = RecordingHost(model="opencode-go/gemini-4-argon")
    host.run(_task("C-1"))

    assert sent, "没有捕获到任何带 model 的请求"
    for item in sent:
        assert isinstance(item["model"], dict), (
            f"{item['path']} 发送了非字典 model：{item['model']!r}"
        )
        assert set(item["model"]) >= {"providerID", "id"}
    # 建会话与投喂各一次
    paths = [item["path"] for item in sent]
    assert "/api/session" in paths


# ---------------------------------------------------------------------------
# 12. CodeBuddy CLI 参数不被吞噬
# ---------------------------------------------------------------------------
def test_codebuddy_host_keeps_briefing_as_positional_arg(monkeypatch, tmp_path):
    """``--allowedTools`` 必须是逗号串，否则会把 briefing 当成工具名吞掉。"""
    captured: dict = {}

    class _Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        kwargs["stdout"].write(
            json.dumps(
                [
                    {
                        "type": "result",
                        "session_id": "sess-cb",
                        "structured_output": {
                            "status": "completed",
                            "verdict": "PASS",
                            "summary": "做完",
                            "evidence": [{"metric": "测试条数", "value": "1 passed"}],
                        },
                    }
                ],
                ensure_ascii=False,
            ).encode("utf-8")
        )
        return _Completed()

    monkeypatch.setattr("open_ontology.agent_hosts.subprocess.run", fake_run)
    monkeypatch.setattr(CodeBuddyHost, "_binary", lambda self: "fake-codebuddy")

    host = CodeBuddyHost()
    result = host.run(
        HostTask(task_id="T-1", briefing="把 1 和 2 相加", workspace_dir=str(tmp_path))
    )

    command = captured["command"]
    tools_at = command.index("--allowedTools")
    assert command[tools_at + 1] == ",".join(host.tools)
    assert "--permission-mode" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    # briefing 仍是最后一个位置参数，且完整带上了输出契约
    assert command[-1].startswith("把 1 和 2 相加")
    assert result.verdict == "PASS"


# ---------------------------------------------------------------------------
# 13. OpenCodeHost 忙闲感知（is_session_idle / wait_until_idle / 切面工厂）
# ---------------------------------------------------------------------------
class IdleTrackingHost(OpenCodeHost):
    """只覆盖 ``_request``：用内存状态模拟会话忙闲。"""

    def __init__(self, *args, idle: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.idle = idle
        self.waited: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def _request(self, method: str, path: str, payload=None) -> str:
        if method.lower() == "post" and path.endswith("/wait"):
            self.waited.append(path)
            return json.dumps({"data": {}})
        if method.lower() == "post" and path.endswith("/prompt"):
            sid = path.split("/")[-2]
            self.prompts.append((sid, (payload or {}).get("text", "")))
            return json.dumps({"data": {}})
        if method.lower() == "get" and path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[-1]
            return json.dumps(
                {
                    "data": {
                        "id": sid,
                        "time": {
                            "busy": 0 if self.idle else 5,
                            "idle": 5 if self.idle else 0,
                        },
                    }
                }
            )
        raise RuntimeError(f"unexpected request {method} {path}")


def test_is_session_idle_reads_time_busy_and_idle():
    assert IdleTrackingHost(idle=True).is_session_idle("ses-1") is True
    assert IdleTrackingHost(idle=False).is_session_idle("ses-1") is False


def test_is_session_idle_defaults_to_idle_without_time_field():
    class NoTimeHost(IdleTrackingHost):
        def _request(self, method: str, path: str, payload=None) -> str:
            return json.dumps({"data": {"id": "ses-1"}})

    assert NoTimeHost().is_session_idle("ses-1") is True


def test_wait_until_idle_returns_true_after_wait_endpoint():
    host = IdleTrackingHost(idle=True)
    assert host.wait_until_idle("ses-1", timeout=0.0) is True
    assert host.waited  # 确实调用了 /wait


def test_wait_until_idle_times_out_while_busy():
    host = IdleTrackingHost(idle=False)
    assert host.wait_until_idle("ses-1", timeout=0.0) is False


def test_wait_until_idle_requires_quiet_confirmation(monkeypatch):
    host = IdleTrackingHost(idle=True)
    checks = [True, False]
    host.is_session_idle = lambda sid: checks.pop(0) if checks else False
    monkeypatch.setattr("open_ontology.agent_hosts.time.sleep", lambda _s: None)

    assert host.wait_until_idle("ses-1", quiet_seconds=1.0, timeout=0.0) is False


def test_as_callback_aspect_prompts_with_custom_formatter():
    host = IdleTrackingHost(idle=True)
    aspect = host.as_callback_aspect(
        formatter=lambda payload: f"卡片 {payload['card_id']} 已收单"
    )

    assert aspect.deliver("ses-9", {"card_id": "C-9"}) == "DELIVERED"
    assert host.prompts == [("ses-9", "卡片 C-9 已收单")]


def test_as_callback_aspect_default_formatter_includes_summary_and_evidence():
    host = IdleTrackingHost(idle=True)
    aspect = host.as_callback_aspect()

    aspect.deliver(
        "ses-9",
        {
            "card_id": "C-9",
            "status": "completed",
            "verdict": "PASS",
            "summary": "内核写完",
            "evidence": [{"metric": "测试条数", "value": "52 passed"}],
        },
    )

    text = host.prompts[0][1]
    assert "C-9" in text and "内核写完" in text
    assert "测试条数" in text and "52 passed" in text


# ---------------------------------------------------------------------------
# 14. BoardRunner × 回调切面：忙时暂存、闲时补投的完整闭环
# ---------------------------------------------------------------------------
def test_board_runner_queues_callback_when_target_busy_then_flushes(
    board, tmp_path
):
    setup_board(board)
    host = IdleTrackingHost(idle=False)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="内核写完",
            evidence=({"metric": "测试条数", "value": "52 passed"},),
            board_state="新基线：52 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    aspect = host.as_callback_aspect()
    runner = BoardRunner(
        runtime=board,
        host=CallableHost(fake_host),
        workspace_root=str(tmp_path),
        callback_aspect=aspect,
        notify_session_id="ses-arch",
    )

    outcome = runner.run_card("C-1", "M-1")

    # 业务照常收单，回调只被暂存，没有打断也没有丢
    assert outcome["status"] == "completed"
    assert outcome["callback_status"] == "QUEUED"
    assert board.get_object(Card, "C-1", MANAGER).column_key == "done"
    assert host.prompts == []
    assert len(aspect.pending_queue) == 1

    # 目标恢复空闲 → flush 补投
    host.idle = True
    assert aspect.flush("ses-arch") == 1
    assert len(host.prompts) == 1
    assert host.prompts[0][0] == "ses-arch"
    assert "C-1" in host.prompts[0][1]


def test_board_runner_reports_none_callback_status_without_aspect(board, tmp_path):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="内核写完",
            evidence=({"metric": "测试条数", "value": "52 passed"},),
            board_state="新基线：52 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    assert runner.run_card("C-1", "M-1")["callback_status"] is None


# ---------------------------------------------------------------------------
# 15. 看板基线状态 × 世代交接（goal / state / unfolded_completions）
# ---------------------------------------------------------------------------
def test_run_card_folds_board_state_when_given(board, tmp_path):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="内核写完",
            evidence=({"metric": "测试条数", "value": "52 passed"},),
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    runner.run_card(
        "C-1",
        "M-1",
        board_state="新一代基线：52 passed",
        goal_changed=False,
        goal_review_reason="目标未变。",
    )
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.state == "新一代基线：52 passed"
    assert stored.unfolded_completions == 0


def test_run_card_without_checkpoint_is_rejected_not_counted(board, tmp_path):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="内核写完",
            evidence=({"metric": "测试条数", "value": "52 passed"},),
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")
    assert outcome["rejected_by_ontology"] == "InvariantError:MISSING_BOARD_STATE"
    assert outcome["outcome_recorded"] != "completed"
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.state == ""
    assert stored.unfolded_completions == 0
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "doing"


def test_make_handover_fn_renders_live_board_state(board, tmp_path):
    setup_board(board)
    act(
        board,
        "update_board_state",
        {"board_key": "BD-1", "state": "基线甲", "goal": "红线甲"},
    )

    runner = BoardRunner(
        runtime=board, host=CallableHost(lambda task: HostResult(status="failed")),
        workspace_root=str(tmp_path),
    )
    handover = runner.make_handover_fn("BD-1")()
    assert "研发看板" in handover
    assert "红线甲" in handover
    assert "基线甲" in handover

    act(board, "update_board_state", {"board_key": "BD-1", "state": "基线乙"})
    assert "基线乙" in runner.make_handover_fn("BD-1")()


def test_opencode_host_injects_live_board_state_on_generation_change(board, tmp_path):
    """换代首轮提示词里必须是最新的 Board.state，而不是建板时的旧文本。"""
    setup_board(board)
    act(
        board,
        "update_board_state",
        {"board_key": "BD-1", "state": "第一代基线", "goal": "架构红线"},
    )

    runner = BoardRunner(
        runtime=board, host=CallableHost(lambda task: HostResult(status="failed")),
        workspace_root=str(tmp_path),
    )
    host = FakeTransportHost(
        rotate_every_n_tasks=1,
        handover_briefing="静态旧摘要",
        handover_fn=runner.make_handover_fn("BD-1"),
    )

    host.run(_task("C-1"))
    first = host.prompts[0][1]
    assert "第一代基线" in first
    assert "架构红线" in first
    assert "静态旧摘要" not in first
    assert "【交接摘要 · 由上一代会话自动交接】" in first

    act(
        board,
        "update_board_state",
        {"board_key": "BD-1", "state": "第二代基线"},
    )
    host.run(_task("C-2"))
    second = host.prompts[1][1]
    assert "第二代基线" in second
    assert "第一代基线" not in second


def test_opencode_host_falls_back_to_static_handover_when_dynamic_empty():
    host = FakeTransportHost(
        rotate_every_n_tasks=1,
        handover_briefing="静态摘要",
        handover_fn=lambda: "",
    )
    host.run(_task("C-1"))
    assert "静态摘要" in host.prompts[0][1]


# ---------------------------------------------------------------------------
# 16. 数据库搜索定位实例与空会话接管
#     （locate_object_in_databases & takeover_board_from_db）
# ---------------------------------------------------------------------------
GOAL_TEXT = "目标：四层元模型单向依赖，M2 内核零业务污染。"
STATE_TEXT = "基线：微内核 1,400 行 + 7 大切面，全仓测试全绿。"


def _seed_persistent_board(db_path: Path) -> None:
    """在真实 SQLite 文件里建一块带 goal/state 的看板与一张卡，随后关库。"""
    runtime = OntologyRuntime(spec, db_path=db_path)
    try:
        act(
            runtime,
            "create_board",
            {
                "board_key": "BD-CC-001",
                "title": "数据库接管看板",
                "tenant": "acme",
                "created_by": "board-manager-01",
                "goal": GOAL_TEXT,
                "state": STATE_TEXT,
                "columns": columns(),
            },
        )
        act(
            runtime,
            "add_member",
            {"member_id": "M-1", "name": "开发者", "role": "developer"},
        )
        act(
            runtime,
            "create_card",
            {
                "card_id": "C-100",
                "board_key": "BD-CC-001",
                "title": "数据库接管验收",
            },
        )
    finally:
        runtime.close()


def test_locate_and_takeover_board_instance_by_id_from_sqlite(tmp_path):
    """空会话按 board_key 扫库定位实例、还原 runtime 并继续执行卡片。"""
    db_path = tmp_path / "board_prod.sqlite3"
    _seed_persistent_board(db_path)

    assert find_ontology_sqlite_files(tmp_path) == [db_path]

    matches = locate_object_in_databases("BD-CC-001", tmp_path)
    assert len(matches) == 1
    assert matches[0]["db_path"] == str(db_path.resolve())
    assert matches[0]["object_type"] == "Board"
    assert matches[0]["object_id"] == "BD-CC-001"
    assert matches[0]["title"] == "数据库接管看板"
    assert "Board" in matches[0]["registered_types"]
    assert matches[0]["properties"]["goal"] == GOAL_TEXT

    taken = takeover_board_from_db("BD-CC-001", tmp_path)
    assert taken["db_path"] == str(db_path.resolve())
    assert taken["board_key"] == "BD-CC-001"
    assert taken["overview"]["board_key"] == "BD-CC-001"
    assert taken["overview"]["cards_total"] == 1

    context = taken["context"]
    assert "BD-CC-001" in context
    assert GOAL_TEXT in context
    assert STATE_TEXT in context
    assert "卡片分布" in context
    assert "可认领（ready）：1 张" in context

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="接管后完工",
            evidence=({"metric": "测试条数", "value": "8 passed"},),
            board_state="新基线：接管后 8 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=taken["runtime"],
        host=CallableHost(fake_host),
        workspace_root=str(tmp_path / "ws"),
    )
    out = runner.run_card("C-100", "M-1")
    assert out["status"] == "completed"
    assert out["outcome_recorded"] == "completed"
    taken["runtime"].close()

    # 落盘校验：另开一条连接，卡片确实已经写进 done 列。
    reopened = OntologyRuntime(spec, db_path=db_path)
    try:
        assert reopened.get_object("Card", "C-100", MANAGER).column_key == "done"
    finally:
        reopened.close()


def test_takeover_board_from_card_id_and_missing_id_raises(tmp_path):
    """传卡片 ID 也能反查所属看板；不存在的 ID 抛 LookupError。"""
    db_path = tmp_path / "board_prod.sqlite3"
    _seed_persistent_board(db_path)

    matches = locate_object_in_databases("C-100", tmp_path)
    assert [item["object_type"] for item in matches] == ["Card"]
    assert matches[0]["properties"]["board_key"] == "BD-CC-001"

    taken = takeover_board_from_db("C-100", tmp_path)
    assert taken["board_key"] == "BD-CC-001"
    assert taken["match"]["object_type"] == "Card"
    assert taken["overview"]["cards_summary"][0]["card_id"] == "C-100"
    assert "可认领（ready）：1 张" in taken["context"]
    taken["runtime"].close()

    with pytest.raises(LookupError):
        takeover_board_from_db("NO-SUCH-ID", tmp_path)


def test_takeover_missing_object_has_no_bootstrapping_side_effect(tmp_path):
    """查不到对象时只抛 LookupError：不建 .instances、不建库、不种看板与动作。

    查找不是初始化——接管入口绝不能顺手把演示数据写到搜索目录里。
    """
    search_root = tmp_path / "empty"
    search_root.mkdir()

    with pytest.raises(LookupError):
        takeover_board_from_db("BD-CC-001", search_root)

    assert list(search_root.iterdir()) == []
    assert not (search_root / ".instances").exists()
    assert find_ontology_sqlite_files(search_root) == []


#: 会破坏 ``file:<path>?mode=ro`` 拼接的目录名：``#`` 起片段、``?`` 起查询串、
#: ``%20`` 被百分号解码成空格。裸空格用于确认正常路径不被误伤。
RESERVED_DIRNAMES = (
    "board#1",
    "literal%20space",
    "query?dir",
    "space dir",
)


@pytest.mark.parametrize(
    "dirname",
    RESERVED_DIRNAMES,
    ids=["hash-fragment", "percent-encoded", "question-query", "literal-space"],
)
def test_readonly_scan_and_locate_survive_uri_reserved_directory(tmp_path, dirname):
    """只读扫描 / 定位 / 接管在保留字符目录下必须命中目标库，且不产生旁路文件。

    ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` 会把路径里的 ``#``
    当片段起点、``?`` 当查询串起点、``%20`` 当百分号转义：拼接方式不变时，
    只读连接要么打开（并创建）一个被截断的兄弟文件，要么直接打不开。这里
    用真实落盘的看板库验证扫描、定位、接管三条路径都仍然落在目标库上。
    """
    data_dir = tmp_path / dirname
    data_dir.mkdir()
    db_path = data_dir / "board_prod.sqlite3"
    _seed_persistent_board(db_path)

    entries_after_seed = sorted(path.name for path in data_dir.iterdir())
    assert entries_after_seed == ["board_prod.sqlite3"]

    assert find_ontology_sqlite_files(data_dir) == [db_path]

    matches = locate_object_in_databases("BD-CC-001", data_dir)
    assert len(matches) == 1
    assert matches[0]["db_path"] == str(db_path.resolve())
    assert matches[0]["object_type"] == "Board"
    assert matches[0]["properties"]["goal"] == GOAL_TEXT

    taken = takeover_board_from_db("BD-CC-001", data_dir)
    try:
        assert taken["db_path"] == str(db_path.resolve())
        assert taken["board_key"] == "BD-CC-001"
        assert taken["overview"]["cards_total"] == 1
        assert GOAL_TEXT in taken["context"]
    finally:
        taken["runtime"].close()

    # 只读路径无副作用：库目录里只有这个库（及其 WAL 旁文件），
    # 上级目录里没有 ``board`` / ``query`` / ``literal space`` 之类截断文件。
    allowed = {db_path.name, db_path.name + "-wal", db_path.name + "-shm"}
    assert {path.name for path in data_dir.iterdir()} <= allowed
    assert sorted(path.name for path in tmp_path.iterdir()) == [dirname]



# ---------------------------------------------------------------------------
# 8. 工作目录文档全文同步（TK-ONTO-BOARD-FULL-DOCS）
# ---------------------------------------------------------------------------
ARCH_DOC_TEXT = "# OpenOntology 架构设计规范\n\n" + "四层元模型单向依赖，M2 内核零业务污染。\n" * 60
STATE_DOC_TEXT = "# OpenOntology 当前状态真值表\n\n" + "微内核 1,400 行，七大切面稳定冻结。\n" * 60


def test_board_runner_syncs_full_architecture_and_state_docs(board, tmp_path):
    """工作目录下的 docs/ARCHITECTURE.md 与 _state.md 全文写回 Board.goal / Board.state。"""
    setup_board(board)
    workspace = tmp_path / "BD-1"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "_state.md").write_text(STATE_DOC_TEXT, encoding="utf-8")
    (workspace / "docs" / "ARCHITECTURE.md").write_text(ARCH_DOC_TEXT, encoding="utf-8")

    runner = BoardRunner(
        runtime=board,
        host=CallableHost(lambda task: None),
        workspace_root=str(tmp_path),
    )

    synced = runner.sync_board_docs("BD-1")
    assert synced == {
        "goal": ARCH_DOC_TEXT.strip(),
        "state": STATE_DOC_TEXT.strip(),
    }
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.goal == ARCH_DOC_TEXT.strip()
    assert stored.state == STATE_DOC_TEXT.strip()
    assert stored.unfolded_completions == 0
    assert len(stored.goal) > 1000 and len(stored.state) > 1000

    # 折叠基线后仍保留完整文档全文，只在末尾追加「最新折叠完工」。
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "内核写完",
            "evidence": [{"metric": "测试条数", "value": "9 passed"}],
            "board_state": "新基线：9 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    folded_state = runner.fold_board_state("BD-1")
    assert "最新折叠完工：C-1(写内核)" in folded_state

    folded = board.get_object("Board", "BD-1", MANAGER)
    assert folded.goal == ARCH_DOC_TEXT.strip()
    assert folded.state.startswith(STATE_DOC_TEXT.strip())
    assert "最新折叠完工：C-1(写内核)" in folded.state
    assert len(folded.state) > len(STATE_DOC_TEXT)


TASK_BOARD_DOC_TEXT = "# 任务看板领域模型与自举协同规范\n\n" + "M1 看板契约与 M0 自举规程。\n" * 40


def test_board_runner_combines_architecture_and_task_board_docs(board, tmp_path):
    """M2 宪法与 M1/M0 看板规范同时存在时，组合装载为 Board.goal。

    单文件目录（只有 ``docs/ARCHITECTURE.md``）的旧行为由上一个用例锁定；
    本用例锁定双文档组合：``ARCHITECTURE + '\\n\\n---\\n\\n' + TASK_BOARD``。
    """
    setup_board(board)
    workspace = tmp_path / "BD-1"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "_state.md").write_text(STATE_DOC_TEXT, encoding="utf-8")
    (workspace / "docs" / "ARCHITECTURE.md").write_text(ARCH_DOC_TEXT, encoding="utf-8")
    (workspace / "docs" / "TASK_BOARD.md").write_text(
        TASK_BOARD_DOC_TEXT, encoding="utf-8"
    )

    runner = BoardRunner(
        runtime=board,
        host=CallableHost(lambda task: None),
        workspace_root=str(tmp_path),
    )
    expected_goal = (
        ARCH_DOC_TEXT.strip() + "\n\n---\n\n" + TASK_BOARD_DOC_TEXT.strip()
    )

    synced = runner.sync_board_docs("BD-1")
    assert synced["goal"] == expected_goal
    assert synced["state"] == STATE_DOC_TEXT.strip()

    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.goal == expected_goal
    assert stored.state == STATE_DOC_TEXT.strip()
    assert stored.goal.count("\n\n---\n\n") == 1

    # 折叠只在 state 末尾追加摘要，不拆散组合后的目标全文。
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})
    act(
        board,
        "record_outcome",
        {
            "card_id": "C-1",
            "member_id": "M-1",
            "outcome": "completed",
            "summary": "内核写完",
            "evidence": [{"metric": "测试条数", "value": "9 passed"}],
            "board_state": "新基线：9 passed",
            "goal_changed": False,
            "goal_review_reason": "目标未变。",
        },
    )
    folded_state = runner.fold_board_state("BD-1")
    assert "最新折叠完工：C-1(写内核)" in folded_state
    folded = board.get_object("Board", "BD-1", MANAGER)
    assert folded.goal == expected_goal
    assert folded.state.startswith(STATE_DOC_TEXT.strip())


def test_board_runner_custom_goal_relpath_keeps_single_document_semantics(
    board, tmp_path
):
    """显式自定义 goal_relpath 时不偷偷拼入看板规范，保持单文档语义。"""
    setup_board(board)
    workspace = tmp_path / "BD-1"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "ARCHITECTURE.md").write_text(ARCH_DOC_TEXT, encoding="utf-8")
    (workspace / "docs" / "TASK_BOARD.md").write_text(
        TASK_BOARD_DOC_TEXT, encoding="utf-8"
    )
    custom = workspace / "docs" / "CUSTOM.md"
    custom.write_text("# 自定义目标\n\n只装这一份。\n", encoding="utf-8")

    runner = BoardRunner(
        runtime=board,
        host=CallableHost(lambda task: None),
        workspace_root=str(tmp_path),
    )
    synced = runner.sync_board_docs("BD-1", goal_relpath="docs/CUSTOM.md")
    assert synced["goal"] == "# 自定义目标\n\n只装这一份。"


# ---------------------------------------------------------------------------
# 16. 完工检查点与 goal 评估契约（宿主 → 本体）
# ---------------------------------------------------------------------------
def test_result_schema_carries_checkpoint_and_review_fields():
    props = RESULT_SCHEMA["properties"]
    assert {"board_state", "goal_changed", "goal_review_reason", "board_goal"} <= set(
        props
    )
    assert props["goal_changed"]["type"] == ["boolean", "null"]
    assert "completed" in props["board_state"]["description"]


def test_parse_codebuddy_structured_output_carries_review():
    raw = json.dumps(
        [
            {
                "type": "result",
                "structured_output": {
                    "status": "completed",
                    "verdict": "PASS",
                    "summary": "完工",
                    "evidence": [{"metric": "测试条数", "value": "11 passed"}],
                    "board_state": "新基线：11 passed",
                    "goal_changed": True,
                    "goal_review_reason": "目标修订。",
                    "board_goal": "新目标",
                },
            }
        ],
        ensure_ascii=False,
    )
    result = parse_codebuddy_output(raw)
    assert result.board_state == "新基线：11 passed"
    assert result.goal_changed is True
    assert result.goal_review_reason == "目标修订。"
    assert result.board_goal == "新目标"


def test_parse_without_review_fields_leaves_them_unset():
    raw = json.dumps(
        [
            {
                "type": "result",
                "structured_output": {
                    "status": "completed",
                    "verdict": "PASS",
                    "summary": "完工",
                    "evidence": [{"metric": "测试条数", "value": "1 passed"}],
                },
            }
        ]
    )
    result = parse_codebuddy_output(raw)
    assert result.board_state is None
    assert result.goal_changed is None
    assert result.goal_review_reason == ""
    assert result.board_goal is None


def test_runner_forwards_review_to_board_and_card_run(board, tmp_path):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="完工",
            evidence=({"metric": "测试条数", "value": "12 passed"},),
            board_state="新基线：12 passed",
            goal_changed=True,
            goal_review_reason="目标随基线修订。",
            board_goal="新目标：显式交接。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")

    assert outcome["rejected_by_ontology"] is None
    assert outcome["outcome_recorded"] == "completed"
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.state == "新基线：12 passed"
    assert stored.goal == "新目标：显式交接。"
    assert stored.unfolded_completions == 0
    card = board.get_object(Card, "C-1", MANAGER)
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert runs[0].board_state == "新基线：12 passed"
    assert runs[0].goal_changed is True
    assert runs[0].goal_review_reason == "目标随基线修订。"
    assert runs[0].board_goal == "新目标：显式交接。"


def test_runner_explicit_caller_override_wins_over_host_review(board, tmp_path):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="完工",
            evidence=({"metric": "测试条数", "value": "3 passed"},),
            board_state="宿主给的基线",
            goal_changed=True,
            goal_review_reason="宿主评估。",
            board_goal="宿主给的目标",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card(
        "C-1",
        "M-1",
        board_state="调用方基线",
        goal_changed=False,
        goal_review_reason="调用方评估。",
    )

    assert outcome["rejected_by_ontology"] is None
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.state == "调用方基线"
    assert stored.goal == ""
    card = board.get_object(Card, "C-1", MANAGER)
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert runs[0].board_state == "调用方基线"
    assert runs[0].goal_changed is False
    assert runs[0].goal_review_reason == "调用方评估。"


def test_runner_retries_rejected_completion_without_duplicate_card_run(board, tmp_path):
    setup_board(board)
    responses = [
        HostResult(
            status="completed",
            verdict="PASS",
            summary="第一次缺检查点",
            evidence=({"metric": "测试条数", "value": "4 passed"},),
        ),
        HostResult(
            status="completed",
            verdict="PASS",
            summary="第二次补齐",
            evidence=({"metric": "测试条数", "value": "4 passed"},),
            board_state="新基线：4 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        ),
    ]
    calls = {"n": 0}

    def fake_host(task: HostTask) -> HostResult:
        result = responses[calls["n"]]
        calls["n"] += 1
        return result

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )

    first = runner.run_card("C-1", "M-1")
    assert first["rejected_by_ontology"] == "InvariantError:MISSING_BOARD_STATE"
    assert first["outcome_recorded"] != "completed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "doing"
    member = board.get_object("Member", "M-1", MANAGER)
    assert member.availability == "BUSY"
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert len(runs) == 1 and runs[0].status == "running"

    second = runner.run_card("C-1", "M-1")
    assert second["rejected_by_ontology"] is None
    assert second["outcome_recorded"] == "completed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "done"
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert len(runs) == 1
    assert runs[0].status == "done"
    member = board.get_object("Member", "M-1", MANAGER)
    assert member.completed_cards == 1
    assert board.get_object("Board", "BD-1", MANAGER).state == "新基线：4 passed"


def test_parse_opencode_messages_carries_review():
    payload = {
        "status": "completed",
        "verdict": "PASS",
        "summary": "完工",
        "evidence": [{"metric": "测试条数", "value": "7 passed"}],
        "board_state": "新基线：7 passed",
        "goal_changed": False,
        "goal_review_reason": "目标未变。",
    }
    raw = json.dumps(
        {
            "data": [
                {
                    "type": "assistant",
                    "content": [
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
    result = parse_opencode_messages(raw)
    assert result.board_state == "新基线：7 passed"
    assert result.goal_changed is False
    assert result.goal_review_reason == "目标未变。"
    assert result.board_goal is None


def test_runner_host_failure_then_corrected_completion_gets_new_attempt(board, tmp_path):
    setup_board(board)
    responses = [
        HostResult(status="failed", summary="宿主失败"),
        HostResult(
            status="completed",
            verdict="PASS",
            summary="重试成功",
            evidence=({"metric": "测试条数", "value": "6 passed"},),
            board_state="新基线：6 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        ),
    ]
    calls = {"n": 0}

    def fake_host(task: HostTask) -> HostResult:
        result = responses[calls["n"]]
        calls["n"] += 1
        return result

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )

    first = runner.run_card("C-1", "M-1")
    assert first["outcome_recorded"] == "failed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "ready"
    assert card.assignee_id is None
    member = board.get_object("Member", "M-1", MANAGER)
    assert member.availability == "IDLE"
    assert member.completed_cards == 0
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert len(runs) == 1 and runs[0].status == "failed"

    second = runner.run_card("C-1", "M-1")
    assert second["outcome_recorded"] == "completed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "done"
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert [run.status for run in runs] == ["failed", "done"]
    member = board.get_object("Member", "M-1", MANAGER)
    assert member.completed_cards == 1
    assert board.get_object("Board", "BD-1", MANAGER).state == "新基线：6 passed"


def test_runner_repeated_call_on_completed_card_does_not_rerun_host(board, tmp_path):
    setup_board(board)
    calls: list[str] = []

    def fake_host(task: HostTask) -> HostResult:
        calls.append(task.task_id)
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="完工",
            evidence=({"metric": "测试条数", "value": "2 passed"},),
            board_state="新基线：2 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    first = runner.run_card("C-1", "M-1")
    assert first["outcome_recorded"] == "completed"

    again = runner.run_card("C-1", "M-1")
    assert again["outcome_recorded"] == "already_completed"
    assert calls == ["C-1"]
    card = board.get_object(Card, "C-1", MANAGER)
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert len(runs) == 1
    member = board.get_object("Member", "M-1", MANAGER)
    assert member.completed_cards == 1


def test_runner_reuses_current_running_claim_without_new_card_run(board, tmp_path):
    setup_board(board)
    act(board, "claim_card", {"card_id": "C-1", "member_id": "M-1"})

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="接管在途尝试",
            evidence=({"metric": "测试条数", "value": "3 passed"},),
            board_state="新基线：3 passed",
            goal_changed=False,
            goal_review_reason="目标未变。",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card("C-1", "M-1")
    assert outcome["outcome_recorded"] == "completed"
    card = board.get_object(Card, "C-1", MANAGER)
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert len(runs) == 1
    assert runs[0].status == "done"


@pytest.mark.parametrize(
    "bad_value",
    [
        {"unexpected": "object"},
        ["unexpected", "array"],
        17,
        True,
    ],
)
def test_parse_rejects_non_string_checkpoint_values(bad_value):
    raw = json.dumps(
        [
            {
                "type": "result",
                "structured_output": {
                    "status": "completed",
                    "verdict": "PASS",
                    "summary": "malformed",
                    "evidence": [{"metric": "测试条数", "value": "4 passed"}],
                    "board_state": bad_value,
                    "goal_changed": False,
                    "goal_review_reason": bad_value,
                    "board_goal": bad_value,
                },
            }
        ],
        ensure_ascii=False,
    )
    result = parse_codebuddy_output(raw)
    assert result.board_state is None
    assert result.goal_review_reason == ""
    assert result.board_goal is None
    assert result.goal_changed is False


def test_parse_trims_valid_checkpoint_strings_and_keeps_false():
    raw = json.dumps(
        [
            {
                "type": "result",
                "structured_output": {
                    "status": "completed",
                    "verdict": "PASS",
                    "summary": "ok",
                    "evidence": [{"metric": "测试条数", "value": "1 passed"}],
                    "board_state": "  新基线  ",
                    "goal_changed": False,
                    "goal_review_reason": "  目标未变  ",
                    "board_goal": "  新目标  ",
                },
            }
        ],
        ensure_ascii=False,
    )
    result = parse_codebuddy_output(raw)
    assert result.board_state == "新基线"
    assert result.goal_review_reason == "目标未变"
    assert result.board_goal == "新目标"
    assert result.goal_changed is False


def test_runner_malformed_parsed_checkpoint_cannot_complete(board, tmp_path):
    setup_board(board)
    raw = json.dumps(
        [
            {
                "type": "result",
                "structured_output": {
                    "status": "completed",
                    "verdict": "PASS",
                    "summary": "malformed",
                    "evidence": [{"metric": "测试条数", "value": "4 passed"}],
                    "board_state": {"unexpected": "object"},
                    "goal_changed": False,
                    "goal_review_reason": ["unexpected", "array"],
                    "board_goal": 17,
                },
            }
        ],
        ensure_ascii=False,
    )
    parsed = parse_codebuddy_output(raw)
    runner = BoardRunner(
        runtime=board,
        host=CallableHost(lambda task: parsed),
        workspace_root=str(tmp_path),
    )
    outcome = runner.run_card("C-1", "M-1")

    assert outcome["rejected_by_ontology"] == "InvariantError:MISSING_BOARD_STATE"
    assert outcome["outcome_recorded"] != "completed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "doing"
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.state == ""
    assert stored.goal == ""


@pytest.mark.parametrize("blank_reason", ["", "   "])
def test_runner_explicit_blank_caller_reason_is_rejected(
    board, tmp_path, blank_reason
):
    setup_board(board)

    def fake_host(task: HostTask) -> HostResult:
        return HostResult(
            status="completed",
            verdict="PASS",
            summary="宿主提议",
            evidence=({"metric": "测试条数", "value": "5 passed"},),
            board_state="宿主基线",
            goal_changed=False,
            goal_review_reason="宿主理由",
        )

    runner = BoardRunner(
        runtime=board, host=CallableHost(fake_host), workspace_root=str(tmp_path)
    )
    outcome = runner.run_card(
        "C-1",
        "M-1",
        board_state="调用方基线",
        goal_changed=False,
        goal_review_reason=blank_reason,
    )

    assert outcome["rejected_by_ontology"] == "InvariantError:MISSING_GOAL_REVIEW_REASON"
    assert outcome["outcome_recorded"] != "completed"
    card = board.get_object(Card, "C-1", MANAGER)
    assert card.column_key == "doing"
    member = board.get_object("Member", "M-1", MANAGER)
    assert member.availability == "BUSY"
    runs = board.get_linked(card, "card_runs", "CardRun", MANAGER)
    assert len(runs) == 1 and runs[0].status == "running"
    stored = board.get_object("Board", "BD-1", MANAGER)
    assert stored.state == ""
    assert stored.goal == ""


# ---------------------------------------------------------------------------
# 17. C-WS-04 显式接管寻址：workspace + Board/Card，重名绝不猜
# ---------------------------------------------------------------------------
class Ledger(ObjectType):
    """本地极小模型：与看板无关，用来证明通用接管不依赖 kanban。"""

    __primary_key__ = "ledger_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)
    __aggregate_root__ = True

    ledger_id: str
    name: str
    region: str


def _ledger_spec() -> OntologySpec:
    spec_ = OntologySpec()
    spec_.object_type(Ledger)
    return spec_


LEDGER_SPEC = _ledger_spec()
EAST_READER = Actor(
    actor_id="reader-east", role="reader", clearance_tags={"region:east"}
)


def _seed_ledger_workspace(
    tmp_path: Path,
    *,
    workspace_id: str = "ledger-ws",
    model_id: str = "ledger",
) -> Path:
    """真实落盘两个聚合根事实 + 一份身份清单。"""
    data_dir = tmp_path / ".instances"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "ledger.sqlite3"
    runtime = OntologyRuntime(LEDGER_SPEC, db_path=db_path)
    try:
        runtime.put_object(Ledger(ledger_id="L-1", name="东区台账", region="east"))
        runtime.put_object(Ledger(ledger_id="L-2", name="西区台账", region="west"))
    finally:
        runtime.close()
    catalog = WorkspaceCatalog(data_dir / MANIFEST_FILENAME)
    catalog.register(
        WorkspaceDescriptor(
            workspace_id=workspace_id,
            model_id=model_id,
            runtime_id=f"rt-{workspace_id}",
            db_path="ledger.sqlite3",
        )
    )
    return db_path


def _table_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0
    )
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def test_takeover_workspace_is_model_agnostic_and_respects_actor_visibility(
    tmp_path, monkeypatch
):
    """通用接管只用可信模型注册表 + 目录清单，绝不 import kanban。

    传入 actor 时聚合根摘要走 ``runtime.query_objects``（RLS / 脱敏照常）；
    不传 actor 时绝不臆造一个 system 角色。
    """
    db_path = _seed_ledger_workspace(tmp_path)

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "open_ontology.kanban":
            raise AssertionError(
                "generic workspace takeover must not import open_ontology.kanban"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    taken = takeover_workspace(
        "ledger-ws",
        models={"ledger": LEDGER_SPEC},
        search_root=tmp_path,
        actor=EAST_READER,
    )
    try:
        assert taken["workspace_id"] == "ledger-ws"
        assert taken["model_id"] == "ledger"
        assert taken["runtime_id"] == "rt-ledger-ws"
        assert taken["db_path"] == str(db_path.resolve())
        assert taken["model"] is LEDGER_SPEC
        assert taken["aggregate_root_types"] == ["Ledger"]
        assert taken["aggregate_roots"] == [
            {
                "workspace_id": "ledger-ws",
                "object_type": "Ledger",
                "object_id": "L-1",
                "title": "东区台账",
            }
        ]
    finally:
        taken["runtime"].close()

    unscoped = takeover_workspace(
        "ledger-ws", models={"ledger": LEDGER_SPEC}, search_root=tmp_path
    )
    try:
        assert unscoped["aggregate_root_types"] == ["Ledger"]
        assert unscoped["aggregate_roots"] is None
    finally:
        unscoped["runtime"].close()


def test_takeover_workspace_missing_workspace_model_and_file_create_nothing(
    tmp_path,
):
    """缺工作空间 / 缺模型 / 缺数据库都明确失败，且不建目录、不建库。"""
    data_dir = tmp_path / ".instances"
    data_dir.mkdir()
    catalog = WorkspaceCatalog(data_dir / MANIFEST_FILENAME)
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-model",
            model_id="ledger",
            runtime_id="rt-ws-model",
            db_path="future.sqlite3",
        )
    )
    before = sorted(path.name for path in data_dir.iterdir())

    with pytest.raises(WorkspaceNotFoundError):
        takeover_workspace("ghost", models={"ledger": LEDGER_SPEC}, catalog=catalog)
    with pytest.raises(WorkspaceModelUnavailableError):
        takeover_workspace(
            "ws-model", models={"other_model": LEDGER_SPEC}, catalog=catalog
        )
    with pytest.raises(WorkspaceDatabaseMissingError):
        takeover_workspace("ws-model", models={"ledger": LEDGER_SPEC}, catalog=catalog)

    assert sorted(path.name for path in data_dir.iterdir()) == before
    assert not (data_dir / "future.sqlite3").exists()

    # 默认清单发现同样是纯读的：空目录里不会冒出 .instances / workspaces.json。
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WorkspaceNotFoundError):
        takeover_workspace("ghost", models={"ledger": LEDGER_SPEC}, search_root=empty)
    assert list(empty.iterdir()) == []


def test_takeover_workspace_rejects_partial_kernel_database_without_creating_tables(
    tmp_path,
):
    """半成品库在实例化 runtime 之前就被只读预检拒绝，绝不补齐缺失表。"""
    data_dir = tmp_path / ".instances"
    data_dir.mkdir()
    db_path = data_dir / "partial.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ontology_objects (object_type TEXT, object_id TEXT)")
    conn.commit()
    conn.close()
    before_tables = _table_names(db_path)
    before_bytes = db_path.read_bytes()

    catalog = WorkspaceCatalog(data_dir / MANIFEST_FILENAME)
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-partial",
            model_id="ledger",
            runtime_id="rt-ws-partial",
            db_path="partial.sqlite3",
        )
    )

    with pytest.raises(IncompleteKernelDatabaseError):
        takeover_workspace(
            "ws-partial", models={"ledger": LEDGER_SPEC}, catalog=catalog
        )

    assert _table_names(db_path) == before_tables
    assert db_path.read_bytes() == before_bytes
    assert sorted(path.name for path in data_dir.iterdir()) == [
        "partial.sqlite3",
        MANIFEST_FILENAME,
    ]


def _seed_board_copy(root: Path) -> Path:
    root.mkdir(parents=True)
    db_path = root / "board.sqlite3"
    _seed_persistent_board(db_path)
    return db_path


def _workspace_catalog(tmp_path: Path, pairs) -> WorkspaceCatalog:
    catalog = WorkspaceCatalog(tmp_path / ".instances" / MANIFEST_FILENAME)
    for workspace_id, db_path in pairs:
        catalog.register(
            WorkspaceDescriptor(
                workspace_id=workspace_id,
                model_id="task_board",
                runtime_id=f"rt-{workspace_id}",
                db_path=str(db_path),
            )
        )
    return catalog


def test_board_takeover_with_same_board_id_in_two_databases_is_ambiguous(tmp_path):
    """两个库都有 BD-CC-001：显式列出候选，绝不取第一个。"""
    db_a = _seed_board_copy(tmp_path / "a")
    db_b = _seed_board_copy(tmp_path / "b")

    with pytest.raises(AmbiguousTakeoverError) as excinfo:
        takeover_board_from_db("BD-CC-001", tmp_path)

    candidates = excinfo.value.candidates
    assert len(candidates) == 2
    assert {item["db_path"] for item in candidates} == {
        str(db_a.resolve()),
        str(db_b.resolve()),
    }
    assert {item["board_key"] for item in candidates} == {"BD-CC-001"}
    assert all(item["workspace_id"] is None for item in candidates)
    assert "BD-CC-001" in str(excinfo.value)

    # 歧义发现是纯读的：不建库、不建目录。
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a", "b"]


def test_board_takeover_by_workspace_isolates_duplicate_board_ids(tmp_path):
    """给了 workspace_id 就只在那个工作空间里找；工作空间内可只给工作空间。"""
    db_a = _seed_board_copy(tmp_path / "a")
    db_b = _seed_board_copy(tmp_path / "b")
    catalog = _workspace_catalog(
        tmp_path, [("ws-a", db_a), ("ws-b", db_b)]
    )

    taken_a = takeover_board("ws-a", "BD-CC-001", catalog=catalog)
    try:
        assert taken_a["workspace_id"] == "ws-a"
        assert taken_a["db_path"] == str(db_a.resolve())
        assert taken_a["board_key"] == "BD-CC-001"
        assert taken_a["overview"]["cards_total"] == 1
    finally:
        taken_a["runtime"].close()

    # 只给工作空间：该工作空间里只有一块看板，直接接管它。
    taken_b = takeover_board("ws-b", catalog=catalog)
    try:
        assert taken_b["workspace_id"] == "ws-b"
        assert taken_b["db_path"] == str(db_b.resolve())
        assert taken_b["board_key"] == "BD-CC-001"
    finally:
        taken_b["runtime"].close()

    # 旧入口保留，并支持显式 workspace_id（清单从 search_root/.instances 发现）。
    legacy = takeover_board_from_db("BD-CC-001", tmp_path, workspace_id="ws-a")
    try:
        assert legacy["workspace_id"] == "ws-a"
        assert legacy["db_path"] == str(db_a.resolve())
    finally:
        legacy["runtime"].close()


def test_board_takeover_resolves_card_to_board_inside_workspace(tmp_path):
    """Board/Card 身份解析落在指定工作空间内：卡片反查所属看板。"""
    db_a = _seed_board_copy(tmp_path / "a")
    db_b = _seed_board_copy(tmp_path / "b")
    catalog = _workspace_catalog(tmp_path, [("ws-a", db_a), ("ws-b", db_b)])

    taken = takeover_board("ws-a", "C-100", catalog=catalog)
    try:
        assert taken["workspace_id"] == "ws-a"
        assert taken["board_key"] == "BD-CC-001"
        assert taken["match"]["object_type"] == "Card"
        assert taken["match"]["object_id"] == "C-100"
        assert taken["overview"]["cards_summary"][0]["card_id"] == "C-100"
        assert "可认领（ready）：1 张" in taken["context"]
    finally:
        taken["runtime"].close()


def test_board_takeover_without_target_in_multi_board_workspace_is_ambiguous(
    tmp_path,
):
    """工作空间里有多块看板且没指定 target：显式候选，不猜。"""
    db_path = _seed_board_copy(tmp_path / "multi")
    runtime = OntologyRuntime(spec, db_path=db_path)
    try:
        act(
            runtime,
            "create_board",
            {
                "board_key": "BD-CC-002",
                "title": "第二块看板",
                "tenant": "acme",
                "created_by": "board-manager-01",
                "goal": GOAL_TEXT,
                "state": STATE_TEXT,
                "columns": columns(),
            },
        )
    finally:
        runtime.close()

    catalog = WorkspaceCatalog(tmp_path / "workspaces.json")
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-multi",
            model_id="task_board",
            runtime_id="rt-ws-multi",
            db_path=str(db_path),
        )
    )

    with pytest.raises(AmbiguousTakeoverError) as excinfo:
        takeover_board("ws-multi", catalog=catalog)

    assert {item["board_key"] for item in excinfo.value.candidates} == {
        "BD-CC-001",
        "BD-CC-002",
    }
    assert all(item["workspace_id"] == "ws-multi" for item in excinfo.value.candidates)


def test_board_takeover_missing_workspace_creates_nothing(tmp_path):
    """工作空间不存在时只抛 LookupError：不建 .instances，也不建库。"""
    catalog = WorkspaceCatalog(tmp_path / ".instances" / MANIFEST_FILENAME)

    with pytest.raises(WorkspaceNotFoundError):
        takeover_board("ghost", "BD-CC-001", catalog=catalog)

    assert not (tmp_path / ".instances").exists()
    assert list(tmp_path.iterdir()) == []


def test_board_takeover_scan_skips_unrelated_partial_db_and_enriches_catalog_workspace_ids(
    tmp_path,
):
    """扫描目录遇到无关残缺库且无命中时不得中断后续合法看板；有清单时候选应附带 workspace_id。"""
    broken_dir = tmp_path / "a_broken"
    broken_dir.mkdir()
    broken_db = broken_dir / "partial.sqlite3"
    conn = sqlite3.connect(broken_db)
    conn.execute(
        "CREATE TABLE ontology_objects ("
        "object_type TEXT, object_id TEXT, title TEXT, properties_json TEXT)"
    )
    conn.commit()
    conn.close()

    valid_db = _seed_board_copy(tmp_path / "b_valid")
    taken = takeover_board_from_db("BD-CC-001", tmp_path)
    try:
        assert taken["db_path"] == str(valid_db.resolve())
        assert taken["board_key"] == "BD-CC-001"
    finally:
        taken["runtime"].close()

    # 再加第二个合法看板库并写入清单：歧义候选应精确带出各自 workspace_id。
    second_db = _seed_board_copy(tmp_path / "c_valid")
    _workspace_catalog(tmp_path, [("ws-b", valid_db), ("ws-c", second_db)])

    with pytest.raises(AmbiguousTakeoverError) as excinfo:
        takeover_board_from_db("BD-CC-001", tmp_path)

    assert {item["workspace_id"] for item in excinfo.value.candidates} == {
        "ws-b",
        "ws-c",
    }

