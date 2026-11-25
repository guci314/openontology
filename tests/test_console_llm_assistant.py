"""WO-31 Verification Suite: Pure M2 LLM Tool-Calling AI Assistant & Sandbox Handoff.

Covers:
1. DeepSeek model alias normalization (`deepseek-v4.1-flash` -> `deepseek-flash`
   on `api.deepseek.com`), provider priority config, and pytest isolation.
2. Pure M2 multi-round tool calling (`list_objects`, `run_sandbox_action`,
   `get_sandbox_diff`) executing in an isolated `fork()` sandbox with ZERO primary
   world writes, plus multi-turn sandbox session reuse and `sandbox_session` cards.
3. Strict RBAC, RLS, anti-IDOR (HTTP 403 on foreign `sandbox_id`), and
   fail-closed Bearer auth when `demo_actor_switch=False`.
4. Enterprise standalone domain compatibility (LLM sandbox takes precedence when
   configured, leaving existing deterministic fallback intact under default pytest).
5. Playwright E2E handoff: chat command in AssistantDrawer -> tool trace &
   `sandbox_session` card -> click card opens `/sandbox` with that exact sandbox diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from open_ontology.console_server import (
    _normalize_llm_model_name,
    _resolve_llm_assistant_config,
    create_console_app,
    start_console_server,
)
from examples.enterprise_console_app import build_console_service, build_starship_console


ROOT = Path(__file__).resolve().parent.parent


def _tool_call_resp(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _text_resp(text: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                }
            }
        ]
    }


def test_1_deepseek_model_normalization_config_and_pytest_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _normalize_llm_model_name("https://api.deepseek.com/v1", "deepseek-v4.1-flash")
        == "deepseek-flash"
    )
    assert (
        _normalize_llm_model_name("https://api.deepseek.com/v1", "deepseek/deepseek-v4.1-flash")
        == "deepseek-flash"
    )
    assert (
        _normalize_llm_model_name("https://openrouter.ai/api/v1", "deepseek/deepseek-v4.1-flash")
        == "deepseek/deepseek-v4.1-flash"
    )

    # Clear all provider envs first, then verify priority resolution
    for var in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPEN_ONTOLOGY_ASSISTANT_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)

    assert _resolve_llm_assistant_config() is None

    monkeypatch.setenv("OPENAI_API_KEY", "test-oa-placeholder")
    oa_cfg = _resolve_llm_assistant_config()
    assert oa_cfg is not None and oa_cfg.provider == "openai" and oa_cfg.model == "gpt-4o-mini"

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-placeholder")
    or_cfg = _resolve_llm_assistant_config()
    assert or_cfg is not None
    assert or_cfg.provider == "openrouter"
    assert or_cfg.model == "deepseek/deepseek-v4.1-flash"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-ds-placeholder")
    monkeypatch.setenv("OPEN_ONTOLOGY_ASSISTANT_MODEL", "deepseek-v4.1-flash")
    ds_cfg = _resolve_llm_assistant_config()
    assert ds_cfg is not None
    assert ds_cfg.provider == "deepseek"
    assert ds_cfg.base_url == "https://api.deepseek.com/v1"
    assert ds_cfg.model == "deepseek-flash"

    # Pytest isolation: even with DEEPSEEK_API_KEY set, default ConsoleService does not auto-enable live HTTP
    svc = build_starship_console(demo_actor_switch=True)
    try:
        client = TestClient(create_console_app(svc))
        meta = client.get("/api/meta").json()
        assert meta["assistant_mode"] == "offline_fallback"
        assert meta["assistant_model"] is None
        assert "test-ds-placeholder" not in json.dumps(meta)

        # Honest offline fallback notice when asking to execute an action without LLM
        off_chat = client.post(
            "/api/assistant/chat",
            json={"message": "在沙盘中让 voyage跳跃到alpha星系"},
        ).json()
        assert off_chat["ok"] is True
        assert off_chat["assistant_mode"] == "offline_fallback"
        assert "离线检索模式" in off_chat["reply"]
        # Primary untouched
        assert client.get("/api/objects/Starship/SS-02").json()["item"]["properties"]["sector"] == "beta"
    finally:
        svc.close()


def test_2_starship_llm_sandbox_execution_and_multiturn_reuse() -> None:
    calls: list[dict[str, Any]] = []

    def _mock_llm(req: dict[str, Any]) -> dict[str, Any]:
        calls.append(req)
        msgs = req["messages"]
        tool_names = [t["function"]["name"] for t in req.get("tools", ())]
        assert set(tool_names) == {
            "list_objects",
            "run_sandbox_action",
            "get_sandbox_diff",
            "run_function",
            "get_audit_summary",
        }
        assert "submit_action" not in tool_names
        assert "promote_sandbox" not in tool_names

        last_user = next(
            (m["content"] for m in reversed(msgs) if m.get("role") == "user"),
            "",
        )
        tool_outputs = [json.loads(m["content"]) for m in msgs if m.get("role") == "tool"]

        if "跃迁" in last_user or "alpha" in last_user.lower():
            if not tool_outputs:
                return _tool_call_resp("tc-1", "list_objects", {"object_type": "Starship"})
            if len(tool_outputs) == 1:
                items = tool_outputs[0]["items"]
                voyager = next(it for it in items if it["object_id"] == "SS-02")
                assert voyager["properties"]["callsign"] == "Voyager"
                assert voyager["properties"]["sector"] == "beta"
                assert voyager["properties"]["fuel_cells"] == 6
                return _tool_call_resp(
                    "tc-2",
                    "run_sandbox_action",
                    {
                        "action_name": "jump_sector",
                        "params": {"ship_id": "SS-02", "target_sector": "alpha", "cost_cells": 2},
                    },
                )
            step_out = tool_outputs[-1]
            assert step_out["ok"] is True
            assert step_out["primary_fingerprint_unchanged"] is True
            return _text_resp(
                "已在平行沙盘将 Voyager（SS-02）从 Beta 星域跃迁至 Alpha 星域，燃料由 6 降至 4，主世界零脏写。"
            )

        # Second turn: refuel inside the SAME sandbox
        if not tool_outputs:
            return _tool_call_resp(
                "tc-3",
                "run_sandbox_action",
                {"action_name": "refuel_ship", "params": {"ship_id": "SS-02", "added_cells": 3}},
            )
        assert tool_outputs[-1]["diff_summary"]["step_count"] == 2
        return _text_resp("已在同一沙盘分支为 Voyager 充能 3 格燃料（4 -> 7）。")

    svc = build_console_service(
        mode="starship",
        demo_actor_switch=True,
        assistant_llm_client=_mock_llm,
    )
    try:
        client = TestClient(create_console_app(svc))
        meta = client.get("/api/meta").json()
        assert meta["assistant_mode"] == "llm_sandbox"
        assert meta["assistant_model"] == "deepseek-flash"
        fp_before = meta["state_fingerprint"]

        r1 = client.post(
            "/api/assistant/chat",
            json={"message": "在沙盘中让 voyage跳跃到alpha星系"},
        )
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["ok"] is True
        assert b1["assistant_mode"] == "llm_sandbox"
        assert b1["assistant_model"] == "deepseek-flash"
        sid = b1["sandbox_id"]
        assert sid and sid.startswith("SBX-")
        assert [t["tool"] for t in b1["tool_trace"]] == ["list_objects", "run_sandbox_action"]
        assert all(t["ok"] for t in b1["tool_trace"])
        assert b1["cards"][0]["kind"] == "sandbox_session"
        assert b1["cards"][0]["sandbox_id"] == sid

        # Primary world remains completely unmodified!
        prim_ss02 = client.get("/api/objects/Starship/SS-02").json()["item"]["properties"]
        assert prim_ss02["sector"] == "beta"
        assert prim_ss02["fuel_cells"] == 6
        assert client.get("/api/meta").json()["state_fingerprint"] == fp_before

        # Sandbox diff reflects the jump accurately
        d1 = client.get(f"/api/sandboxes/{sid}/diff").json()
        assert d1["step_count"] == 1
        assert d1["primary_fingerprint_unchanged"] is True
        upd = next(u for u in d1["updated"] if u["object_id"] == "SS-02")
        deltas = {d["field"]: (d["primary"], d["sandbox"]) for d in upd["field_deltas"]}
        assert deltas["sector"] == ("beta", "alpha")
        assert deltas["fuel_cells"] == (6, 4)

        # Turn 2: reuse same sandbox_id + pass conversation history
        r2 = client.post(
            "/api/assistant/chat",
            json={
                "message": "继续在这个沙盘里给它充能 3 格燃料",
                "sandbox_id": sid,
                "history": [
                    {"role": "user", "content": "在沙盘中让 voyage跳跃到alpha星系"},
                    {"role": "assistant", "content": b1["reply"]},
                ],
            },
        )
        assert r2.status_code == 200
        b2 = r2.json()
        assert b2["sandbox_id"] == sid

        d2 = client.get(f"/api/sandboxes/{sid}/diff").json()
        assert d2["step_count"] == 2
        upd2 = next(u for u in d2["updated"] if u["object_id"] == "SS-02")
        assert upd2["after"]["sector"] == "alpha"
        assert upd2["after"]["fuel_cells"] == 7
        assert client.get("/api/objects/Starship/SS-02").json()["item"]["properties"]["fuel_cells"] == 6
    finally:
        svc.close()


def test_3_security_rbac_rls_anti_idor_and_fail_closed_auth() -> None:
    def _nav_red_llm(req: dict[str, Any]) -> dict[str, Any]:
        msgs = req["messages"]
        outs = [json.loads(m["content"]) for m in msgs if m.get("role") == "tool"]
        if not outs:
            # Step 1: attempt listing Starships as nav-alpha -> must only see SS-01
            return _tool_call_resp("tc-rls", "list_objects", {"object_type": "Starship"})
        if len(outs) == 1:
            ids = [it["object_id"] for it in outs[0]["items"]]
            assert ids == ["SS-01"], f"RLS leak: nav-alpha saw {ids}"
            # Step 2: attempt unauthorized action refuel_ship -> must fail PERMISSION_DENIED
            return _tool_call_resp(
                "tc-rbac",
                "run_sandbox_action",
                {"action_name": "refuel_ship", "params": {"ship_id": "SS-01", "added_cells": 2}},
            )
        rbac_out = outs[-1]
        assert rbac_out["ok"] is False
        assert rbac_out["error_code"] == "PERMISSION_DENIED"
        return _text_resp("已拦截：当前领航员角色无权执行充能动作，且仅可见 Alpha 星域星舰。")

    svc = build_console_service(
        mode="starship",
        demo_actor_switch=True,
        assistant_llm_client=_nav_red_llm,
    )
    try:
        client = TestClient(create_console_app(svc))
        # Owner (cmd-kirk) creates a sandbox first
        kirk_sbx = client.post("/api/sandboxes").json()["sandbox_id"]

        # Non-owner (nav-alpha) passing kirk's sandbox_id must be rejected with HTTP 403 (anti-IDOR)
        idor_resp = client.post(
            "/api/assistant/chat",
            headers={"X-Actor-Id": "nav-alpha"},
            json={"message": "继续推演", "sandbox_id": kirk_sbx},
        )
        assert idor_resp.status_code == 403

        # nav-alpha clean session strictly obeys RLS & RBAC inside tools
        nav_resp = client.post(
            "/api/assistant/chat",
            headers={"X-Actor-Id": "nav-alpha"},
            json={"message": "查看战舰并在沙盘尝试充能"},
        )
        assert nav_resp.status_code == 200
        body = nav_resp.json()
        assert body["rls_scope"] == ["sector:alpha"]
        assert body["tool_trace"][0]["ok"] is True
        assert body["tool_trace"][1]["ok"] is False
        assert "PERMISSION_DENIED" in body["tool_trace"][1]["summary"]
    finally:
        svc.close()

    # Fail-closed auth check when demo_actor_switch=False
    strict_svc = build_console_service(
        mode="starship",
        demo_actor_switch=False,
        assistant_llm_client=_nav_red_llm,
    )
    try:
        strict_client = TestClient(create_console_app(strict_svc))
        unauth = strict_client.post("/api/assistant/chat", json={"message": "hello"})
        assert unauth.status_code in (401, 403)
    finally:
        strict_svc.close()


def test_4_enterprise_standalone_llm_sandbox_annotation() -> None:
    def _ent_mock(req: dict[str, Any]) -> dict[str, Any]:
        outs = [json.loads(m["content"]) for m in req["messages"] if m.get("role") == "tool"]
        if not outs:
            return _tool_call_resp("tc-e1", "list_objects", {"object_type": "O2cOrder"})
        if len(outs) == 1:
            assert any(it["object_id"] == "SO-300" for it in outs[0]["items"])
            return _tool_call_resp(
                "tc-e2",
                "run_sandbox_action",
                {
                    "action_name": "annotate_order_risk_note",
                    "params": {"order_id": "SO-300", "risk_note": "AI沙盘风控复核"},
                },
            )
        assert outs[-1]["ok"] is True
        return _text_resp("已在平行沙盘为订单 SO-300 添加风控备注，外部主世界无写入。")

    ent_svc = build_console_service(
        mode="standalone",
        demo_actor_switch=True,
        assistant_llm_client=_ent_mock,
    )
    try:
        client = TestClient(create_console_app(ent_svc))
        before_note = client.get("/api/objects/O2cOrder/SO-300").json()["item"]["properties"].get("risk_note")
        resp = client.post(
            "/api/assistant/chat",
            json={"message": "在沙盘中给 SO-300 添加风控备注"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assistant_mode"] == "llm_sandbox"
        assert body["cards"][0]["kind"] == "sandbox_session"
        sid = body["sandbox_id"]
        assert client.get("/api/objects/O2cOrder/SO-300").json()["item"]["properties"].get("risk_note") == before_note
        diff = client.get(f"/api/sandboxes/{sid}/diff").json()
        assert diff["step_count"] == 1
        upd = next(u for u in diff["updated"] if u["object_id"] == "SO-300")
        assert upd["after"]["risk_note"] == "AI沙盘风控复核"
    finally:
        ent_svc.close()


def test_5_playwright_e2e_assistant_to_sandbox_handoff() -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_sync.sync_playwright

    dist_index = ROOT / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Frontend dist build missing at {dist_index}"

    def _pw_mock(req: dict[str, Any]) -> dict[str, Any]:
        outs = [json.loads(m["content"]) for m in req["messages"] if m.get("role") == "tool"]
        if not outs:
            return _tool_call_resp("tc-pw1", "list_objects", {"object_type": "Starship"})
        if len(outs) == 1:
            return _tool_call_resp(
                "tc-pw2",
                "run_sandbox_action",
                {
                    "action_name": "jump_sector",
                    "params": {"ship_id": "SS-02", "target_sector": "alpha", "cost_cells": 2},
                },
            )
        return _text_resp("已在隔离沙盘让 Voyager (SS-02) 跃迁至 Alpha 星域，燃料消耗 2 格。")

    svc = build_console_service(
        mode="starship",
        demo_actor_switch=True,
        assistant_llm_client=_pw_mock,
    )
    handle = start_console_server(
        svc, host="127.0.0.1", port=0, static_dir=ROOT / "web" / "dist"
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    bad_responses: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "response",
                lambda resp: bad_responses.append(f"{resp.status} {resp.url}")
                if resp.status >= 400
                else None,
            )

            page.goto(handle.base_url, wait_until="networkidle")
            page.get_by_test_id("open-assistant-btn").click()
            page.wait_for_selector('[data-testid="assistant-drawer"]')
            badge = page.get_by_test_id("assistant-mode-badge").inner_text()
            assert "DeepSeek 沙盘代理" in badge

            page.get_by_test_id("assistant-input").fill("在沙盘中让 voyage跳跃到alpha星系")
            page.get_by_test_id("assistant-send-btn").click()

            page.wait_for_selector('[data-testid="assistant-tool-trace"]')
            page.wait_for_selector('[data-testid="assistant-card-sandbox_session"]')

            page.get_by_test_id("assistant-card-sandbox_session").click()
            page.wait_for_url("**/sandbox")
            page.wait_for_selector('[data-testid="sandbox-diff-list"]')
            page.wait_for_selector('[data-testid="sandbox-status-banner"]')

            banner = page.get_by_test_id("sandbox-status-banner").inner_text()
            assert "已载入 AI 数字管家推演沙盘" in banner
            promote_txt = page.get_by_test_id("sandbox-promote-btn").inner_text()
            assert "(1)" in promote_txt
            diff_txt = page.get_by_test_id("sandbox-diff-list").inner_text()
            assert "SS-02" in diff_txt
            assert "Voyager" in diff_txt

            browser.close()
    finally:
        handle.stop()

    assert console_errors == []
    assert page_errors == []
    assert bad_responses == []


def test_6_playwright_e2e_bidirectional_sandbox_drawer_sync() -> None:
    """WO-34B: SandboxPage <-> AssistantDrawer share single activeSandboxId & live-refresh behind drawer."""
    playwright_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_sync.sync_playwright

    dist_index = ROOT / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Frontend dist build missing at {dist_index}"

    captured_diffs: list[dict[str, Any]] = []
    chat_requests: list[dict[str, Any]] = []

    def _sync_mock(req: dict[str, Any]) -> dict[str, Any]:
        user_msgs = [m["content"] for m in req["messages"] if m.get("role") == "user"]
        latest_user = user_msgs[-1] if user_msgs else ""
        outs = [json.loads(m["content"]) for m in req["messages"] if m.get("role") == "tool"]

        if "Voyager" in latest_user:
            if not outs:
                return _tool_call_resp(
                    "tc-sync-act",
                    "run_sandbox_action",
                    {
                        "action_name": "jump_sector",
                        "params": {"ship_id": "SS-02", "target_sector": "alpha", "cost_cells": 2},
                    },
                )
            assert outs[-1]["ok"] is True
            return _text_resp("已在同一沙盘追加第 2 步推演：Voyager (SS-02) 跃迁至 Alpha 星域。")

        if not outs:
            return _tool_call_resp("tc-sync-diff", "get_sandbox_diff", {})
        captured_diffs.append(outs[0])
        return _text_resp(
            f"当前沙盘共 {outs[0].get('step_count', 0)} 步推演，变更 {outs[0].get('total_changes', 0)} 个对象。"
        )

    svc = build_console_service(
        mode="starship",
        demo_actor_switch=True,
        assistant_llm_client=_sync_mock,
    )
    handle = start_console_server(
        svc, host="127.0.0.1", port=0, static_dir=ROOT / "web" / "dist"
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    bad_responses: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "response",
                lambda resp: bad_responses.append(f"{resp.status} {resp.url}")
                if resp.status >= 400
                else None,
            )
            page.on(
                "request",
                lambda req: chat_requests.append(dict(req.post_data_json or {}))
                if req.method == "POST" and req.url.endswith("/api/assistant/chat")
                else None,
            )

            # 1. Start on /sandbox and run 1-step preset creating SBX-xxxx
            page.goto(f"{handle.base_url}/sandbox", wait_until="networkidle")
            page.wait_for_selector('[data-testid="sandbox-page"]')
            page.get_by_test_id("sandbox-preset-starship_jump_omega").click()
            page.wait_for_selector('[data-testid="sandbox-diff-list"]')
            assert "(1)" in page.get_by_test_id("sandbox-promote-btn").inner_text()

            # 2. Open AssistantDrawer immediately on /sandbox -> shared SBX badge visible
            page.get_by_test_id("open-assistant-btn").click()
            badge = page.get_by_test_id("assistant-active-sandbox-badge")
            badge.wait_for(state="visible", timeout=10000)
            badge_text = badge.inner_text()
            assert "已同步沙盘 SBX-" in badge_text
            shared_sid = badge_text.split("已同步沙盘 ", 1)[1].strip()

            # 3. Ask assistant about current diff -> tool get_sandbox_diff receives exact shared SBX with step_count==1
            page.get_by_test_id("assistant-input").fill("看看当前沙盘推演的 diff 有哪些变化？")
            page.get_by_test_id("assistant-send-btn").click()
            page.wait_for_selector("text=当前沙盘共 1 步推演", timeout=10000)

            assert chat_requests[0]["sandbox_id"] == shared_sid
            assert len(captured_diffs) == 1
            assert captured_diffs[0]["sandbox_id"] == shared_sid
            assert captured_diffs[0]["step_count"] == 1
            assert any(u["object_id"] == "SS-01" for u in captured_diffs[0]["updated"])

            # 4. Have assistant run 2nd step in same sandbox -> /sandbox behind drawer live-refreshes to (2)
            page.get_by_test_id("assistant-input").fill("再在同一个沙盘里让 Voyager 跃迁到 Alpha 星域")
            page.get_by_test_id("assistant-send-btn").click()
            page.wait_for_selector("text=已在同一沙盘追加第 2 步推演", timeout=10000)
            assert chat_requests[1]["sandbox_id"] == shared_sid

            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"sandbox-promote-btn\"]')?.textContent?.includes('(2)')"
            )
            diff_txt = page.get_by_test_id("sandbox-diff-list").inner_text()
            assert "SS-01" in diff_txt
            assert "SS-02" in diff_txt

            # 5. Navigate away to / and back to /sandbox -> SandboxPage adopts same shared_sid with 2 steps intact
            page.get_by_test_id("close-assistant-drawer").click()
            page.get_by_test_id("nav-cockpit").click()
            page.wait_for_selector('[data-testid="cockpit-page"]')
            page.get_by_test_id("nav-sandbox").click()
            page.wait_for_selector('[data-testid="sandbox-diff-list"]')
            assert "(2)" in page.get_by_test_id("sandbox-promote-btn").inner_text()

            # 6. Switch actor -> activeSandboxId resets to null cleanly with zero 403/IDOR errors
            page.get_by_test_id("nav-cockpit").click()
            page.wait_for_selector('[data-testid="cockpit-page"]')
            page.get_by_test_id("actor-switcher").select_option("nav-alpha")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"actor-switcher\"]')?.value === 'nav-alpha'"
            )
            page.get_by_test_id("open-assistant-btn").click()
            page.wait_for_selector('[data-testid="assistant-drawer"]')
            assert page.get_by_test_id("assistant-active-sandbox-badge").count() == 0
            page.get_by_test_id("close-assistant-drawer").click()

            # Entering /sandbox as new actor forks a clean actor-owned sandbox (!= shared_sid, 0 steps)
            page.get_by_test_id("nav-sandbox").click()
            page.wait_for_selector('[data-testid="sandbox-page"]')
            page.get_by_test_id("open-assistant-btn").click()
            new_badge = page.get_by_test_id("assistant-active-sandbox-badge")
            new_badge.wait_for(state="visible", timeout=10000)
            assert new_badge.inner_text().split("已同步沙盘 ", 1)[1].strip() != shared_sid

            browser.close()
    finally:
        handle.stop()
        svc.runtime.close()

    assert console_errors == []
    assert page_errors == []
    assert bad_responses == []
