"""WO-32 Verification Suite: M0/M1 Dual-Agent Isolation & M1 DeepSeek DOAL Workbench.

Covers:
1. Full CRUD on isolated M1 draft DOAL overlays (`upsert_draft_object` /
   `extend_base_object` / `delete_draft_object` / link / guarded Rule action /
   invariant / disjoint), deterministic Chinese back-translation (`_render_doal_clauses_zh`),
   live preview annotations (`draft_status`), and Z3 PO-1~PO-4 proofs (`PROVEN_CLEAN`).
2. Dead class detection (`UNSATISFIABLE_CLASS` + minimal `unsat_core`) blocking
   hot-apply with `DRAFT_PROOF_FAILED` (HTTP 400).
3. Code-enforced two-layer boundary: any attempt to delete or overwrite base
   standard `ObjectType` / `Action` / `Invariant` is hard-rejected with
   `OVERLAY_BREAKS_STANDARD_CONTRACT`.
4. DOAL Spec AG-3 escalation (`escalate_to_edit_function`), demo-only hot-apply
   onto live M0 runtime vs production HTTP 403 `PRODUCTION_APPLY_REQUIRES_GIT_PR`,
   plus fail-closed Bearer auth and cross-actor anti-IDOR 403.
5. Playwright E2E verification of strict M0/M1 top-bar agent isolation and
   end-to-end NL -> DOAL -> Z3 -> Export -> Apply workflow.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from open_ontology.console_server import create_console_app, start_console_server
from examples.enterprise_console_app import (
    DEMO_TOKEN_SECRET,
    build_console_service,
    build_starship_console,
)


ROOT = Path(__file__).resolve().parent.parent


def _has_z3() -> bool:
    return importlib.util.find_spec("z3") is not None


def _tc(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
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


def _multi_tc(*calls: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                        for cid, name, args in calls
                    ],
                }
            }
        ]
    }


def _txt(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_1_m1_expert_doal_crud_subtype_when_rule_and_z3_clean() -> None:
    def _mock_m1_crud(req: dict[str, Any]) -> dict[str, Any]:
        msgs = req["messages"]
        outs = [json.loads(m["content"]) for m in msgs if m.get("role") == "tool"]
        user_q = next(m["content"] for m in reversed(msgs) if m.get("role") == "user")

        if "crud_temp" in user_q:
            if not outs:
                return _multi_tc(
                    (
                        "t-add-tmp",
                        "upsert_draft_object",
                        {
                            "name": "TempProbe",
                            "display_label": "临时探测器",
                            "primary_key": "probe_id",
                            "fields": [
                                {"name": "probe_id", "field_type": "str", "primary_key": True, "required": True},
                                {"name": "range_km", "field_type": "int", "default": 10, "non_negative": True},
                            ],
                        },
                    ),
                    (
                        "t-lnk-tmp",
                        "upsert_draft_link",
                        {
                            "source_object": "TempProbe",
                            "field": "parent_ship",
                            "target_type": "Starship",
                            "required": False,
                            "display_label": "母舰",
                        },
                    ),
                    (
                        "t-act-tmp",
                        "upsert_draft_rule_action",
                        {
                            "name": "ping_probe",
                            "display_label": "探测信号",
                            "allowed_roles": ["admin"],
                            "params": [{"name": "probe_id", "field_type": "str", "required": True}],
                            "mutations": [
                                {
                                    "op": "modify",
                                    "object_type": "TempProbe",
                                    "id_param": "probe_id",
                                    "set_fields": {"range_km": {"op": "add", "left": {"ref": "obj.range_km"}, "right": {"lit": 5}}},
                                }
                            ],
                        },
                    ),
                    (
                        "t-inv-tmp",
                        "upsert_draft_invariant",
                        {
                            "name": "probe_range_ok",
                            "code": "PROBE_RANGE",
                            "target_type": "TempProbe",
                            "must_hold": {"op": "ge", "left": {"ref": "obj.range_km"}, "right": {"lit": 0}},
                        },
                    ),
                    (
                        "t-disj-tmp",
                        "upsert_draft_disjoint",
                        {"type_a": "TempProbe", "type_b": "WarpCore"},
                    ),
                )
            if len(outs) == 5:
                assert all(o["ok"] is True for o in outs)
                # Now delete every draft artifact cleanly (D of CRUD)
                return _multi_tc(
                    ("d-disj", "delete_draft_disjoint", {"type_a": "TempProbe", "type_b": "WarpCore"}),
                    ("d-inv", "delete_draft_invariant", {"name": "probe_range_ok"}),
                    ("d-act", "delete_draft_rule_action", {"name": "ping_probe"}),
                    ("d-lnk", "delete_draft_link", {"source_object": "TempProbe", "field": "parent_ship"}),
                    ("d-obj", "delete_draft_object", {"name": "TempProbe"}),
                )
            assert all(o["ok"] is True for o in outs[-5:])
            return _txt("已完成草稿层增删改查校验，所有临时元素已干净清除。")

        # Standard ExplorerShip derivation + guarded Rule action + invariant + Z3 prove
        if not outs:
            return _tc("t-insp", "inspect_m1_blueprint", {"include_draft": True})
        if len(outs) == 1:
            return _multi_tc(
                (
                    "t-sub",
                    "upsert_draft_object",
                    {
                        "name": "ExplorerShip",
                        "display_label": "深空探索舰",
                        "extends": "Starship",
                        "fields": [
                            {
                                "name": "x_shield_level",
                                "field_type": "int",
                                "default": 20,
                                "non_negative": True,
                                "display_label": "护盾强度",
                            }
                        ],
                        "relations": [
                            {
                                "field": "support_core",
                                "target_type": "WarpCore",
                                "required": False,
                                "min_cardinality": 0,
                                "max_cardinality": 1,
                                "display_label": "备用曲速核",
                            }
                        ],
                    },
                ),
                (
                    "t-inv",
                    "upsert_draft_invariant",
                    {
                        "name": "shield_ceiling",
                        "code": "SHIELD_TOO_HIGH",
                        "target_type": "ExplorerShip",
                        "must_hold": {"op": "le", "left": {"ref": "obj.x_shield_level"}, "right": {"lit": 100}},
                        "description": "探索舰护盾强度不得超过 100",
                    },
                ),
                (
                    "t-act",
                    "upsert_draft_rule_action",
                    {
                        "name": "boost_shield",
                        "display_label": "增强探索护盾",
                        "allowed_roles": ["commander", "admin"],
                        "params": [
                            {"name": "ship_id", "field_type": "str", "required": True},
                            {"name": "delta", "field_type": "int", "required": True, "non_negative": True},
                            {"name": "overload", "field_type": "bool", "default": False},
                        ],
                        "preconditions": [
                            {
                                "name": "shield_within_ceiling",
                                "code": "SHIELD_OVERFLOW",
                                "target_type": "ExplorerShip",
                                "id_param": "ship_id",
                                "must_hold": {
                                    "op": "le",
                                    "left": {
                                        "op": "if_else",
                                        "cond": {"op": "eq", "left": {"ref": "param.overload"}, "right": {"lit": True}},
                                        "then": {
                                            "op": "add",
                                            "left": {"ref": "obj.x_shield_level"},
                                            "right": {"op": "mul", "left": {"ref": "param.delta"}, "right": {"lit": 2}},
                                        },
                                        "else": {"op": "add", "left": {"ref": "obj.x_shield_level"}, "right": {"ref": "param.delta"}},
                                    },
                                    "right": {"lit": 100},
                                },
                                "description": "护盾充能后不可突破上限 100",
                            }
                        ],
                        "mutations": [
                            {
                                "op": "modify",
                                "object_type": "ExplorerShip",
                                "id_param": "ship_id",
                                "when": {"op": "eq", "left": {"ref": "param.overload"}, "right": {"lit": True}},
                                "set_fields": {
                                    "x_shield_level": {
                                        "op": "add",
                                        "left": {"ref": "obj.x_shield_level"},
                                        "right": {"op": "mul", "left": {"ref": "param.delta"}, "right": {"lit": 2}},
                                    }
                                },
                            },
                            {
                                "op": "modify",
                                "object_type": "ExplorerShip",
                                "id_param": "ship_id",
                                "when": {"op": "not", "arg": {"op": "eq", "left": {"ref": "param.overload"}, "right": {"lit": True}}},
                                "set_fields": {
                                    "x_shield_level": {"op": "add", "left": {"ref": "obj.x_shield_level"}, "right": {"ref": "param.delta"}}
                                },
                            },
                        ],
                    },
                ),
            )
        if len(outs) == 4:
            return _tc("t-prove", "prove_draft_mathematics", {})
        return _txt("已成功在 M1 草稿派生深空探索舰 ExplorerShip，定义护盾增强规则与上限约束。")

    svc = build_console_service(mode="starship", demo_actor_switch=True, assistant_llm_client=_mock_m1_crud)
    try:
        client = TestClient(create_console_app(svc))
        # CRUD round-trip for temporary elements
        tmp_resp = client.post("/api/blueprint/expert/chat", json={"message": "crud_temp"}).json()
        assert tmp_resp["ok"] is True
        assert tmp_resp["draft_has_changes"] is False

        # Derive ExplorerShip + action + invariant + Z3 prove
        r = client.post(
            "/api/blueprint/expert/chat",
            json={"message": "派生深空探索舰子类并添加护盾增强与上限不变量"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["draft_has_changes"] is True
        did = body["draft_id"]
        assert did.startswith("M1D-")

        # Deterministic Chinese clauses
        clauses_joined = "\n".join(body["draft_clauses_zh"])
        assert "ExplorerShip" in clauses_joined
        assert "shield_ceiling" in clauses_joined
        assert "boost_shield" in clauses_joined
        assert "100" in clauses_joined

        # Preview blueprint annotations
        prev = body["preview_blueprint"]
        ot_map = {o["name"]: o for o in prev["object_types"]}
        assert ot_map["ExplorerShip"]["draft_status"] == "draft_added"
        assert ot_map["ExplorerShip"]["extends"] == "Starship"
        assert ot_map["Starship"]["draft_status"] == "base"
        assert any(
            lk["source"] == "ExplorerShip" and lk["field"] == "support_core" and lk["draft_status"] == "draft_added"
            for lk in prev["links"]
        )
        act_map = {a["name"]: a for a in prev["actions"]}
        assert act_map["boost_shield"]["draft_status"] == "draft_added"

        # Z3 result (or graceful fallback on Py3.11)
        proof = body["proof_report"]
        if _has_z3():
            assert proof["prover_available"] is True
            assert proof["verdict"] == "PROVEN_CLEAN"
            assert proof["summary"]["dead_class_count"] == 0
            assert proof["summary"]["lsp_violation_count"] == 0
            assert proof["summary"]["violated_invariants"] == 0
        else:
            assert proof["prover_available"] is False
            assert proof["reason"] == "PROVER_UNAVAILABLE"

        # GET & DELETE draft endpoints
        fetched = client.get(f"/api/blueprint/drafts/{did}").json()
        assert fetched["draft_id"] == did and fetched["draft_has_changes"] is True
        assert client.delete(f"/api/blueprint/drafts/{did}").json()["deleted"] is True
        assert client.get(f"/api/blueprint/drafts/{did}").status_code == 404
    finally:
        svc.close()


@pytest.mark.skipif(not _has_z3(), reason="z3-solver not installed")
def test_2_dead_class_detection_and_apply_gate_draft_proof_failed() -> None:
    def _mock_dead_cls(req: dict[str, Any]) -> dict[str, Any]:
        outs = [json.loads(m["content"]) for m in req["messages"] if m.get("role") == "tool"]
        if not outs:
            return _multi_tc(
                (
                    "d1",
                    "upsert_draft_object",
                    {
                        "name": "DeadShip",
                        "display_label": "矛盾测试舰",
                        "extends": "Starship",
                        "fields": [{"name": "x_shield_level", "field_type": "int", "non_negative": True}],
                    },
                ),
                (
                    "d2",
                    "upsert_draft_invariant",
                    {
                        "name": "min_shield_80",
                        "code": "MIN_80",
                        "target_type": "DeadShip",
                        "must_hold": {"op": "ge", "left": {"ref": "obj.x_shield_level"}, "right": {"lit": 80}},
                    },
                ),
                (
                    "d3",
                    "upsert_draft_invariant",
                    {
                        "name": "max_shield_20",
                        "code": "MAX_20",
                        "target_type": "DeadShip",
                        "must_hold": {"op": "le", "left": {"ref": "obj.x_shield_level"}, "right": {"lit": 20}},
                    },
                ),
            )
        if len(outs) == 3:
            return _tc("d4", "prove_draft_mathematics", {})
        return _txt("Z3 检测到 DeadShip 为空类（min >= 80 与 max <= 20 矛盾），已阻止应用。")

    svc = build_console_service(mode="starship", demo_actor_switch=True, assistant_llm_client=_mock_dead_cls)
    try:
        client = TestClient(create_console_app(svc))
        res = client.post("/api/blueprint/expert/chat", json={"message": "创建一个护盾既>=80又<=20的死类"}).json()
        assert res["ok"] is True
        proof = res["proof_report"]
        assert proof["verdict"] == "ISSUES_FOUND"
        assert proof["summary"]["dead_class_count"] == 1
        bad = next(c for c in proof["class_proofs"] if c["object_type"] == "DeadShip")
        assert bad["status"] == "UNSATISFIABLE_CLASS"
        assert set(bad["unsat_core"]) == {"min_shield_80", "max_shield_20"}

        # Hard apply gate rejects unproved/dead-class draft with HTTP 400 DRAFT_PROOF_FAILED
        apply_resp = client.post(f"/api/blueprint/drafts/{res['draft_id']}/apply")
        assert apply_resp.status_code == 400
        assert apply_resp.json()["error_code"] == "DRAFT_PROOF_FAILED"
    finally:
        svc.close()


def test_3_base_core_protection_ag3_escalation_hot_apply_and_security_guards() -> None:
    def _mock_seq(req: dict[str, Any]) -> dict[str, Any]:
        msgs = req["messages"]
        outs = [json.loads(m["content"]) for m in msgs if m.get("role") == "tool"]
        user_q = next(m["content"] for m in reversed(msgs) if m.get("role") == "user")

        if "try_break_base" in user_q:
            if not outs:
                return _multi_tc(
                    ("b1", "delete_draft_object", {"name": "Starship"}),
                    ("b2", "delete_draft_rule_action", {"name": "jump_sector"}),
                    ("b3", "delete_draft_invariant", {"name": "starship_non_negative_fuel"}),
                    ("b4", "upsert_draft_object", {"name": "Starship", "fields": []}),
                )
            for out in outs:
                assert out["ok"] is False
                assert out["error_code"] == "OVERLAY_BREAKS_STANDARD_CONTRACT"
            return _txt("基座标准契约受保护，全部破坏性删除与覆盖均已阻断。")

        if "escalate_loop" in user_q:
            if not outs:
                return _tc(
                    "esc1",
                    "escalate_to_edit_function",
                    {
                        "title": "任意深度子图递归跃迁路径搜寻",
                        "reason": "需要可变深度图遍历与最短路动态规划，超出 QF_LIA DOAL 边界。",
                        "affected_objects": ["Starship", "WarpCore"],
                        "specification_notes": "需通过受审 Python @spec.edit_function 实现并绑定单元测试。",
                    },
                )
            return _txt("已升级为 EditFunction 工程提案，未编造近似 DOAL。")

        if not outs:
            return _multi_tc(
                (
                    "g1",
                    "upsert_draft_object",
                    {
                        "name": "ExplorerShip",
                        "display_label": "深空探索舰",
                        "extends": "Starship",
                        "fields": [{"name": "x_shield_level", "field_type": "int", "default": 10, "non_negative": True}],
                    },
                ),
                (
                    "g2",
                    "upsert_draft_rule_action",
                    {
                        "name": "boost_shield",
                        "display_label": "护盾充能",
                        "allowed_roles": ["commander", "admin"],
                        "params": [
                            {"name": "ship_id", "field_type": "str", "required": True},
                            {"name": "delta", "field_type": "int", "required": True, "non_negative": True},
                        ],
                        "mutations": [
                            {
                                "op": "modify",
                                "object_type": "ExplorerShip",
                                "id_param": "ship_id",
                                "set_fields": {"x_shield_level": {"op": "add", "left": {"ref": "obj.x_shield_level"}, "right": {"ref": "param.delta"}}},
                            }
                        ],
                    },
                ),
            )
        return _txt("已创建 ExplorerShip 与护盾充能动作。")

    svc = build_console_service(mode="starship", demo_actor_switch=True, assistant_llm_client=_mock_seq)
    try:
        client = TestClient(create_console_app(svc))

        # 3a. Base standard core deletion/overwrite hard-rejected
        brk = client.post("/api/blueprint/expert/chat", json={"message": "try_break_base"}).json()
        assert brk["draft_has_changes"] is False
        assert len(brk["tool_trace"]) == 4
        assert all(not t["ok"] and "OVERLAY_BREAKS_STANDARD_CONTRACT" in t["summary"] for t in brk["tool_trace"])

        # 3b. AG-3 escalation does not fabricate DOAL
        esc = client.post("/api/blueprint/expert/chat", json={"message": "escalate_loop"}).json()
        assert esc["draft_has_changes"] is False
        assert len(esc["escalations"]) == 1
        assert esc["cards"][0]["kind"] == "edit_function_escalation"

        # 3c. Cross-actor Anti-IDOR on drafts (nav-alpha cannot access cmd-kirk's draft)
        good = client.post("/api/blueprint/expert/chat", json={"message": "derive ExplorerShip"}).json()
        did = good["draft_id"]
        assert client.get(f"/api/blueprint/drafts/{did}", headers={"X-Actor-Id": "nav-alpha"}).status_code == 403
        assert (
            client.post(
                "/api/blueprint/expert/chat",
                headers={"X-Actor-Id": "nav-alpha"},
                json={"message": "hello", "draft_id": did},
            ).status_code
            == 403
        )
        assert client.delete(f"/api/blueprint/drafts/{did}", headers={"X-Actor-Id": "nav-alpha"}).status_code == 403
        assert (
            client.post(f"/api/blueprint/drafts/{did}/apply", headers={"X-Actor-Id": "nav-alpha"}).status_code == 403
        )

        # 3d. Demo hot-apply commits ExplorerShip + boost_shield to live M0 runtime!
        applied = client.post(f"/api/blueprint/drafts/{did}/apply")
        assert applied.status_code == 200
        assert applied.json()["ok"] is True

        tbox = client.get("/api/tbox").json()
        assert "ExplorerShip" in {o["name"] for o in tbox["object_types"]}
        assert any(a["name"] == "boost_shield" for a in tbox["actions"])

        # Instantiate ExplorerShip in live runtime & simulate boost_shield in M0 sandbox
        from open_ontology import Actor

        adm = Actor(actor_id="cmd-kirk", role="admin", clearance_tags={"*"})
        ex_cls = svc.runtime.spec.get_object_type("ExplorerShip")
        svc.runtime.put_object(
            ex_cls(ship_id="EX-01", callsign="Pathfinder", sector="alpha", fuel_cells=8, core="WC-1", x_shield_level=20),
            actor=adm,
        )
        sbx_id = client.post("/api/sandboxes").json()["sandbox_id"]
        sub = client.post(
            f"/api/sandboxes/{sbx_id}/submit",
            json={"action": "boost_shield", "params": {"ship_id": "EX-01", "delta": 15}},
        ).json()
        assert sub["ok"] is True
        diff = client.get(f"/api/sandboxes/{sbx_id}/diff").json()
        upd = next(u for u in diff["updated"] if u["object_id"] == "EX-01")
        assert upd["before"]["x_shield_level"] == 20 and upd["after"]["x_shield_level"] == 35
    finally:
        svc.close()

    # 3e. Production guard (demo_actor_switch=False): fail-closed auth & HTTP 403 PRODUCTION_APPLY_REQUIRES_GIT_PR
    strict_svc = build_console_service(mode="starship", demo_actor_switch=False, assistant_llm_client=_mock_seq)
    try:
        sc = TestClient(create_console_app(strict_svc))
        assert sc.post("/api/blueprint/expert/chat", json={"message": "derive ExplorerShip"}).status_code in (401, 403)
        headers = {"Authorization": f"Bearer {DEMO_TOKEN_SECRET['act-admin']}"}
        # Note Starship cmd token
        star_headers = {"Authorization": "Bearer tok-starship-cmd"}
        dr = sc.post("/api/blueprint/expert/chat", headers=star_headers, json={"message": "derive ExplorerShip"})
        assert dr.status_code == 200
        sdid = dr.json()["draft_id"]
        apply_strict = sc.post(f"/api/blueprint/drafts/{sdid}/apply", headers=star_headers)
        assert apply_strict.status_code == 403
        assert apply_strict.json()["error_code"] == "PRODUCTION_APPLY_REQUIRES_GIT_PR"
        _ = headers
    finally:
        strict_svc.close()


def test_4_playwright_e2e_dual_agent_isolation_and_m1_workbench() -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_sync.sync_playwright

    dist_index = ROOT / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Frontend dist build missing at {dist_index}"

    def _pw_m1_mock(req: dict[str, Any]) -> dict[str, Any]:
        outs = [json.loads(m["content"]) for m in req["messages"] if m.get("role") == "tool"]
        if not outs:
            return _multi_tc(
                (
                    "pw-o",
                    "upsert_draft_object",
                    {
                        "name": "ExplorerShip",
                        "display_label": "深空探索舰",
                        "extends": "Starship",
                        "fields": [{"name": "x_shield_level", "field_type": "int", "default": 25, "non_negative": True}],
                    },
                ),
                (
                    "pw-i",
                    "upsert_draft_invariant",
                    {
                        "name": "shield_bound",
                        "code": "SHIELD_BOUND",
                        "target_type": "ExplorerShip",
                        "must_hold": {"op": "le", "left": {"ref": "obj.x_shield_level"}, "right": {"lit": 100}},
                    },
                ),
            )
        if len(outs) == 2:
            return _tc("pw-p", "prove_draft_mathematics", {})
        return _txt("已在 M1 草稿添加深空探索舰 ExplorerShip，并通过 Z3 证明。")

    svc = build_console_service(mode="starship", demo_actor_switch=True, assistant_llm_client=_pw_m1_mock)
    handle = start_console_server(svc, host="127.0.0.1", port=0, static_dir=ROOT / "web" / "dist")

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
                lambda resp: bad_responses.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None,
            )

            # 1. M0 view (/): show ONLY AI 数字管家, hide AI 本体论专家
            page.goto(handle.base_url, wait_until="networkidle")
            assert page.get_by_test_id("open-assistant-btn").is_visible()
            assert page.get_by_test_id("open-ontology-expert-btn").count() == 0

            # 2. Switch to M1 view (/blueprint): HIDE AI 数字管家, show ONLY AI 本体论专家
            page.get_by_test_id("nav-blueprint").click()
            page.wait_for_url("**/blueprint")
            page.wait_for_selector('[data-testid="blueprint-page"]')
            assert page.get_by_test_id("open-assistant-btn").count() == 0
            assert page.get_by_test_id("open-ontology-expert-btn").is_visible()

            hdr_box = page.locator("header").bounding_box()
            assert hdr_box is not None and hdr_box["height"] <= 76
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            # 3. Open M1 Ontology Expert Drawer & author ExplorerShip + verify preview + export + apply
            page.get_by_test_id("open-ontology-expert-btn").click()
            page.wait_for_selector('[data-testid="ontology-expert-drawer"]')
            badge = page.get_by_test_id("expert-mode-badge").inner_text()
            assert "DeepSeek DOAL 专家" in badge

            page.get_by_test_id("expert-input").fill("派生深空探索舰 ExplorerShip 并完成 Z3 验证")
            page.get_by_test_id("expert-send-btn").click()

            page.wait_for_selector('[data-testid="expert-tool-trace"]')
            page.wait_for_selector('[data-testid="expert-clauses-zh"]')
            page.wait_for_selector('[data-testid="expert-proof-badge"]')
            assert "PROVEN_CLEAN" in page.get_by_test_id("expert-proof-badge").inner_text()

            page.wait_for_selector('[data-testid="blueprint-draft-banner"]')
            page.wait_for_selector('[data-testid="blueprint-node-ExplorerShip"]')
            page.wait_for_selector('[data-testid="blueprint-draft-badge-ExplorerShip"]')
            assert page.get_by_test_id("export-doal-btn").is_visible()

            # Apply draft to demo world -> draft banner clears, ExplorerShip remains live on blueprint!
            page.get_by_test_id("apply-doal-draft-btn").click()
            page.wait_for_selector('[data-testid="blueprint-draft-banner"]', state="detached")
            assert page.get_by_test_id("blueprint-node-ExplorerShip").is_visible()

            browser.close()
    finally:
        handle.stop()

    assert console_errors == []
    assert page_errors == []
    assert bad_responses == []
