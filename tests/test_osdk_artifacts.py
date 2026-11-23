"""v1 OSDK UIArtifact + AIP Agent /api/agent/chat 闭环测试。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import ClassVar

from pydantic import BaseModel

from open_ontology.osdk import AIPAgentExecutor, UIArtifact, run_agent_turn
from open_ontology.runtime import OntologyRuntime
from open_ontology.spec import (
    Actor,
    InvariantError,
    ObjectType,
    OntologySpec,
)
from open_ontology.workshop import WorkshopServer, _serve


class _DemoItem(ObjectType):
    id: str
    title: str
    status: str = "OPEN"
    amount: int = 10


class _CloseItemParams(BaseModel):
    item_id: str
    note: str = "done"


def _build_runtime() -> tuple[OntologyRuntime, Actor]:
    spec = OntologySpec()
    spec.add_object_type(_DemoItem)

    @spec.edit_function("close_item", _CloseItemParams)
    def _close_handler(ctx, params: _CloseItemParams) -> dict:
        obj = ctx.get(_DemoItem, params.item_id)
        if obj.status == "CLOSED":
            raise InvariantError("item is already closed", code="ALREADY_CLOSED")
        if params.item_id == "ITM-2" and ctx.actor.role != "admin":
            raise InvariantError("admin approval required", code="APPROVAL_REQUIRED_ADMIN")
        ctx.modify(obj, status="CLOSED")
        return {"closed": params.item_id}

    spec.declare_action(
        "close_item",
        edit_function="close_item",
        allowed_roles=["admin", "ops"],
        revert_roles=["admin"],
    )

    rt = OntologyRuntime(spec, db_path=":memory:")
    admin = Actor(actor_id="u:admin", role="admin", clearance_tags={"*"})
    rt.put_object(_DemoItem(id="ITM-1", title="常规工单", amount=20))
    rt.put_object(_DemoItem(id="ITM-2", title="大额工单", amount=200))
    return rt, admin


def test_emit_ui_artifact_autofills_action_schema() -> None:
    rt, admin = _build_runtime()
    try:
        executor = AIPAgentExecutor(rt, admin)
        res = executor.dispatch(
            "emit_ui_artifact",
            {
                "kind": "action_form",
                "title": "关闭工单",
                "payload": {
                    "action": "close_item",
                    "prefill_params": {"item_id": "ITM-1"},
                },
            },
        )
        assert res["ok"] is True
        art = res["artifact"]
        assert art["kind"] == "action_form"
        assert art["payload"]["prefill_params"] == {"item_id": "ITM-1"}
        assert "item_id" in art["payload"]["schema"]["properties"]
        assert "note" in art["payload"]["schema"]["properties"]

        popped = executor.pop_artifacts()
        assert len(popped) == 1
        assert isinstance(popped[0], UIArtifact)
        assert executor.pop_artifacts() == []
    finally:
        rt.close()


def test_run_agent_turn_deterministic_flows() -> None:
    rt, admin = _build_runtime()
    try:
        executor = AIPAgentExecutor(rt, admin)

        # 1. 查询对象 -> object_table
        t1 = run_agent_turn(executor, "查询 _DemoItem 列表")
        assert len(t1.artifacts) == 1
        assert t1.artifacts[0].kind == "object_table"
        assert len(t1.artifacts[0].payload["rows"]) == 2

        # 2. 结合 ui_context 打开动作表单 -> action_form + prefill
        t2 = run_agent_turn(
            executor,
            "打开 close_item 表单",
            ui_context={"selected_object_ids": ["ITM-1"]},
        )
        assert len(t2.artifacts) == 1
        assert t2.artifacts[0].kind == "action_form"
        assert t2.artifacts[0].payload["prefill_params"] == {"item_id": "ITM-1"}

        # 3. 构造并发送 approval_card UIArtifact（M1 协同审批卡）
        run = rt.submit_action("RUN-APR-1", "close_item", {"item_id": "ITM-2"}, admin)
        assert run["status"] == "COMMITTED"
        art = executor.build_approval_card_artifact(run)
        assert art.kind == "approval_card"
        assert art.payload["run_id"] == "RUN-APR-1"
    finally:
        rt.close()


def test_run_agent_turn_custom_llm_fn() -> None:
    rt, admin = _build_runtime()
    try:
        executor = AIPAgentExecutor(rt, admin)

        def _fake_llm(*, executor, message, ui_context, **_kwargs):
            sim = executor.dispatch(
                "simulate_action",
                {"action": "close_item", "params": {"item_id": "ITM-1"}},
            )
            art = executor.build_diff_preview_artifact(
                "close_item", {"item_id": "ITM-1"}, sim
            )
            return {"reply_text": f"已为「{message}」完成推演", "artifacts": [art]}

        turn = run_agent_turn(executor, "试运行关闭 ITM-1", llm_fn=_fake_llm)
        assert "试运行关闭 ITM-1" in turn.reply_text
        assert len(turn.artifacts) == 1
        assert turn.artifacts[0].kind == "diff_preview"
        assert turn.artifacts[0].payload["simulation"]["valid"] is True
    finally:
        rt.close()


def test_workshop_agent_chat_endpoint() -> None:
    rt, admin = _build_runtime()
    server = WorkshopServer(("127.0.0.1", 0), rt, actor=admin)
    handle = _serve(server, "127.0.0.1", 0)
    try:
        req = urllib.request.Request(
            f"{handle.base_url}/api/agent/chat",
            data=json.dumps(
                {
                    "message": "查询 _DemoItem",
                    "ui_context": {"active_object_type": "_DemoItem"},
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["artifacts"][0]["kind"] == "object_table"
        assert len(data["artifacts"][0]["payload"]["rows"]) == 2

        # 空消息应返回 400
        bad_req = urllib.request.Request(
            f"{handle.base_url}/api/agent/chat",
            data=json.dumps({"message": "   "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_req, timeout=5)
            raise AssertionError("expected HTTP 400 for empty message")
        except urllib.error.HTTPError as err:
            assert err.code == 400
    finally:
        handle.stop()
        rt.close()


def test_v2_metric_chart_mcts_and_iter_stream() -> None:
    from open_ontology.osdk import iter_agent_turn

    rt, admin = _build_runtime()
    try:
        executor = AIPAgentExecutor(rt, admin)

        # 1. 聚合统计 -> metric_chart
        t_chart = run_agent_turn(executor, "统计 _DemoItem 分布")
        assert len(t_chart.artifacts) == 1
        assert t_chart.artifacts[0].kind == "metric_chart"
        assert t_chart.artifacts[0].payload["total"] == 2
        assert len(t_chart.artifacts[0].payload["bars"]) >= 1

        # 2. Ghost event 选中态传递 -> ui_command focus_object
        t_focus = run_agent_turn(
            executor,
            "聚焦刚刚选中的对象",
            ghost_events=[
                {"event_type": "selection_changed", "detail": {"object_id": "ITM-2"}}
            ],
            ui_context={"active_object_type": "_DemoItem"},
        )
        assert len(t_focus.artifacts) == 1
        assert t_focus.artifacts[0].kind == "ui_command"
        assert t_focus.artifacts[0].payload["command"] == "focus_object"
        assert t_focus.artifacts[0].payload["object_id"] == "ITM-2"

        # 3. MCTS plan card builder
        mcts_art = executor.build_mcts_plan_card_artifact(
            {"best_value": 95.0, "best_plan_labels": ["close_item(ITM-1)"], "simulations": 40}
        )
        assert mcts_art.kind == "mcts_plan_card"
        assert mcts_art.payload["best_value"] == 95.0

        # 4. iter_agent_turn 流式事件序列
        events = list(iter_agent_turn(executor, "统计 _DemoItem 分布", chunk_size=5))
        names = [e["event"] for e in events]
        assert "tool_call" in names
        assert "token" in names
        assert "artifact" in names
        assert names[-1] == "done"

        # 5. export_llm_tools 导出的业务动作名直调安全路由至 simulate_action（零副作用）
        before_fp = rt.state_fingerprint()
        sim_res = executor.dispatch("close_item", {"item_id": "ITM-1"})
        assert sim_res["simulated_only"] is True
        assert sim_res["valid"] is True
        assert rt.state_fingerprint() == before_fp
    finally:
        rt.close()


def test_v2_workshop_ghost_and_sse_streaming() -> None:
    rt, admin = _build_runtime()
    server = WorkshopServer(("127.0.0.1", 0), rt, actor=admin)
    handle = _serve(server, "127.0.0.1", 0)
    try:
        # 1. 静默上报 Ghost Message
        ghost_req = urllib.request.Request(
            f"{handle.base_url}/api/agent/ghost",
            data=json.dumps(
                {
                    "event_type": "selection_changed",
                    "detail": {"object_id": "ITM-2"},
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(ghost_req, timeout=5) as resp:
            gdata = json.loads(resp.read().decode("utf-8"))
        assert gdata["ok"] is True
        assert gdata["count"] == 1

        # 2. SSE 流式请求打开动作表单，自动命中服务端存储的 Ghost 选中对象 ITM-2
        sse_req = urllib.request.Request(
            f"{handle.base_url}/api/agent/chat",
            data=json.dumps({"message": "打开 close_item 表单", "stream": True}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(sse_req, timeout=5) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw_sse = resp.read().decode("utf-8")
        assert "text/event-stream" in ctype
        assert "event: token" in raw_sse
        assert "event: artifact" in raw_sse
        assert "event: done" in raw_sse
        assert "ITM-2" in raw_sse
    finally:
        handle.stop()
        rt.close()


def test_openai_agents_sdk_with_standard_skills(monkeypatch) -> None:
    import agents
    from open_ontology.osdk import discover_skills

    skills = discover_skills()
    assert any(s["name"] == "platform-genesis" for s in skills)

    rt, admin = _build_runtime()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-agents-sdk")

    captured: dict[str, Any] = {}

    async def _fake_runner_run(starting_agent, input_items, **kwargs):
        captured["agent"] = starting_agent
        captured["input"] = input_items
        tools_by_name = {t.name: t for t in starting_agent.tools}
        assert "load_skill" in tools_by_name
        assert "query_objects" in tools_by_name
        assert "close_item" in tools_by_name

        skill_out = json.loads(
            await tools_by_name["load_skill"].on_invoke_tool(
                None, json.dumps({"name": "platform-genesis"})
            )
        )
        assert skill_out["ok"] is True
        assert "create_adapter" in skill_out["content"]

        await tools_by_name["query_objects"].on_invoke_tool(
            None, json.dumps({"object_type": "_DemoItem"})
        )

        class _FakeResult:
            final_output = "已通过 OpenAI Agents SDK 完成查询。"

        return _FakeResult()

    monkeypatch.setattr(agents.Runner, "run", _fake_runner_run)

    try:
        executor = AIPAgentExecutor(rt, admin)
        res = run_agent_turn(executor, "请使用技能查询 _DemoItem")
        assert res.reply_text == "已通过 OpenAI Agents SDK 完成查询。"
        assert any(c["name"] == "load_skill" for c in res.tool_calls)
        assert any(c["name"] == "query_objects" for c in res.tool_calls)
        assert len(res.artifacts) == 1
        assert res.artifacts[0].kind == "object_table"
    finally:
        rt.close()


def test_openai_agents_sdk_true_streaming(monkeypatch) -> None:
    import types
    import agents
    from open_ontology.osdk import iter_agent_turn

    rt, admin = _build_runtime()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-stream")

    def _fake_run_streamed(starting_agent, input_items, **kwargs):
        tools_by_name = {t.name: t for t in starting_agent.tools}

        class _FakeStreamResult:
            final_output = "流式输出完成"

            async def stream_events(self):
                await tools_by_name["query_objects"].on_invoke_tool(
                    None, json.dumps({"object_type": "_DemoItem"})
                )
                for ch in ("流式", "输出", "完成"):
                    yield types.SimpleNamespace(
                        type="raw_response_event",
                        data=types.SimpleNamespace(
                            type="response.output_text.delta", delta=ch
                        ),
                    )

        return _FakeStreamResult()

    monkeypatch.setattr(agents.Runner, "run_streamed", _fake_run_streamed)

    try:
        executor = AIPAgentExecutor(rt, admin)
        events = list(iter_agent_turn(executor, "流式查询 _DemoItem"))
        names = [e["event"] for e in events]
        assert names == ["tool_call", "token", "token", "token", "artifact", "done"]
        tokens = [e["data"]["text"] for e in events if e["event"] == "token"]
        assert tokens == ["流式", "输出", "完成"]
        assert events[-1]["data"]["reply_text"] == "流式输出完成"
    finally:
        rt.close()
