"""WO-26 Two-Layer Enterprise Digital Twin Console Verification Suite.

Covers 4 verification gates:
1. Group A — Architecture & L1 Frontend Shell Purity:
   - Static AST/token reverse-scan across ``web/src/shell/**/*.{ts,tsx}`` proving
     0 M1 domain class/action tokens and 0 external vendor identifiers.
   - Zero technical jargon (``TBox``, ``ABox``, ``ObjectType``, ``state_fingerprint``,
     ``InvariantError``) across all business pages & components.
   - Zero diff on pure runtime core ``open_ontology/__init__.py`` & ``runtime.py`` and
     full Gate 2/3/6 compliance for ``open_ontology/console_server.py``.
2. Group B — Pure M2 Fictional Domain Blind Contract (``Starship / WarpCore``):
   - Full console API flow (meta, TBox, RLS object queries, lineage, function calls,
     ``fork()`` counterfactual sandbox + compare + diff + promote + destroy, audit,
     autonomy policy update, and NL assistant chat) with zero enterprise dependencies.
3. Group C — Enterprise Standard World Model Full-Loop API Suite (Py3.13 & Py3.11):
   - Bearer token auth & strict ``demo_actor_switch=False`` fail-closed guards.
   - Cross-plant H2R->M2 RLS isolation across CFO / North PM / South Shopfloor.
   - Human signoff on ``WorkCard`` enforcing SoD, role match, and $t_1$ atomic execution.
   - All 3 executive Sandbox ``fork()`` presets with ``compute_trial_balance`` deltas and
     unchanged primary ``state_fingerprint()``.
   - Redline interception zero dirty writes + ``/api/admin/*`` 3-stage autonomy switching.
   - RLS-scoped AI assistant endpoint.
4. Group D — Real Headless Chromium Playwright E2E + 4 Desktop HD Screenshots:
   - Live interactive flows over built ``web/dist/`` served by ``enterprise_console_app``,
     asserting 0 console errors, 0 uncaught pageerrors, 0 failed network requests,
     and capturing ``/tmp/wo26_ui_{cockpit,sandbox,audit,admin}.png``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_ontology.console_server import create_console_app, start_console_server
from examples.enterprise_console_app import (
    DEMO_TOKEN_SECRET,
    build_console_service,
    build_starship_console,
)
from tests.test_architecture_boundaries import M1_ONTOLOGY_MODULES, _VENDOR_WORD_RE


# ---------------------------------------------------------------------------
# Group A: Architecture & L1 Frontend Shell Purity Gates
# ---------------------------------------------------------------------------
def test_group_a_shell_frontend_purity_and_zero_core_mutation() -> None:
    tokens: set[str] = {"task_board"}
    for mod_name, attr in M1_ONTOLOGY_MODULES:
        mod = importlib.import_module(mod_name)
        target = getattr(mod, attr)
        spec = target() if callable(target) else target
        tokens.update(spec.object_types.keys())
        tokens.update(spec.abstract_types.keys())
        tokens.update(spec.object_properties.keys())
        tokens.update(spec.interfaces.keys())
        tokens.update(spec.actions.keys())
        tokens.update(spec.functions.keys())
        tokens.update((spec.display.get("ontologies") or {}).keys())

    explicit_forbidden = (
        "O2cOrder",
        "P2pPurchaseOrder",
        "WorkOrder",
        "GlAccount",
        "TrialBalanceSnapshot",
        "CrmDeal",
        "WorkCard",
    )
    for tok in explicit_forbidden:
        assert tok in tokens

    distinctive = sorted(tok for tok in tokens if "_" in tok or any(c.isupper() for c in tok))
    m1_re = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(t) for t in distinctive) + r")(?![A-Za-z0-9_])"
    )
    vendor_ext_re = re.compile(
        r"(?<![A-Za-z0-9_])(odoo|jsherp|bottlecrm|feishu|lark|salesforce|erpnext)(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )

    shell_dir = ROOT / "web" / "src" / "shell"
    shell_files = sorted(
        p for p in shell_dir.rglob("*") if p.is_file() and p.suffix in (".ts", ".tsx")
    )
    assert len(shell_files) >= 10

    for sf in shell_files:
        text = sf.read_text(encoding="utf-8")
        m1_hits = sorted({m.group(1) for m in m1_re.finditer(text)})
        v_hits = sorted(
            {m.group(1).lower() for m in _VENDOR_WORD_RE.finditer(text)}
            | {m.group(1).lower() for m in vendor_ext_re.finditer(text)}
        )
        rel = sf.relative_to(ROOT)
        assert m1_hits == [], f"L1 UI 壳文件 {rel} 泄漏 M1 业务词：{m1_hits}"
        assert v_hits == [], f"L1 UI 壳文件 {rel} 泄漏厂商标识：{v_hits}"

    jargon_tokens = ("TBox", "ABox", "ObjectType", "state_fingerprint", "InvariantError")
    for rel_path in (
        "pages/CockpitPage.tsx",
        "pages/SandboxPage.tsx",
        "pages/AuditPage.tsx",
        "components/WorkCardInbox.tsx",
        "components/DynamicObjectDrawer.tsx",
        "components/LineageGraph.tsx",
        "components/AssistantDrawer.tsx",
    ):
        text = (shell_dir / rel_path).read_text(encoding="utf-8")
        hits = [tok for tok in jargon_tokens if tok in text]
        assert hits == [], f"业务主页面/组件 {rel_path} 夹带技术黑话：{hits}"

    # Pure runtime core surface must remain completely untouched
    proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "open_ontology/__init__.py", "open_ontology/runtime.py"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", f"Pure runtime kernel was modified: {proc.stdout}"


# ---------------------------------------------------------------------------
# Group B: Pure M2 Fictional Domain Blind Contract (Starship / WarpCore)
# ---------------------------------------------------------------------------
def test_group_b_starship_blind_m2_console_contract() -> None:
    svc = build_starship_console(demo_actor_switch=True)
    try:
        client = TestClient(create_console_app(svc))

        meta = client.get("/api/meta").json()
        assert meta["ok"] is True
        assert meta["pack_hint"] == "blind"
        assert meta["actor"]["actor_id"] == "cmd-kirk"

        tbox = client.get("/api/tbox").json()
        assert {ot["name"] for ot in tbox["object_types"]} == {"WarpCore", "Starship"}

        # RLS check between commander (all sectors) and alpha navigator
        all_ships = [it["object_id"] for it in client.get("/api/objects/Starship").json()["items"]]
        alpha_ships = [
            it["object_id"]
            for it in client.get("/api/objects/Starship", headers={"X-Actor-Id": "nav-alpha"}).json()["items"]
        ]
        assert all_ships == ["SS-01", "SS-02"]
        assert alpha_ships == ["SS-01"]
        assert client.get("/api/objects/Starship/SS-02", headers={"X-Actor-Id": "nav-alpha"}).status_code == 403

        # Lineage
        lin = client.get("/api/objects/Starship/SS-01/lineage").json()
        assert {n["object_id"] for n in lin["nodes"]} >= {"SS-01", "WC-1"}

        # Direct function + autonomy shadow -> copilot -> auto
        fn_res = client.post("/api/functions/fleet_fuel_total", json={}).json()
        assert fn_res["result"]["total_fuel_cells"] == 14

        fp_before = client.get("/api/meta").json()["state_fingerprint"]

        # Switch to SHADOW mode -> submit_action never mutates primary runtime
        put_sh = client.put("/api/admin/autonomy", json={"default_mode": "SHADOW"}).json()
        assert put_sh["policy"]["default_mode"] == "SHADOW"
        sh_act = client.post(
            "/api/actions/submit",
            json={"action": "jump_sector", "params": {"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 2}},
        ).json()
        assert sh_act["committed"] is False
        assert sh_act["outcome"] == "SHADOW_RECORDED"
        assert client.get("/api/objects/Starship/SS-01").json()["item"]["properties"]["sector"] == "alpha"

        # Sandbox fork() isolation, compare, diff, promote & destroy
        sbx = client.post("/api/sandboxes").json()
        sid = sbx["sandbox_id"]
        sub_res = client.post(
            f"/api/sandboxes/{sid}/submit",
            json={"action": "jump_sector", "params": {"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 2}},
        ).json()
        assert sub_res["ok"] is True
        assert sub_res["primary_fingerprint_unchanged"] is True

        cmp_fn = client.post(f"/api/sandboxes/{sid}/function/fleet_fuel_total", json={}).json()
        assert cmp_fn["primary"]["total_fuel_cells"] == 14
        assert cmp_fn["sandbox"]["total_fuel_cells"] == 12
        assert cmp_fn["sandbox"]["total_value"] > cmp_fn["primary"]["total_value"]

        diff = client.get(f"/api/sandboxes/{sid}/diff").json()
        assert diff["total_changes"] == 1
        assert diff["updated"][0]["object_id"] == "SS-01"
        assert client.get("/api/meta").json()["state_fingerprint"] == fp_before

        # Switch autonomy back to AUTO_UNDER_CEILING and promote sandbox
        client.put("/api/admin/autonomy", json={"default_mode": "AUTO_UNDER_CEILING", "max_value": 100.0})
        promoted = client.post(f"/api/sandboxes/{sid}/promote").json()
        assert promoted["ok"] is True and promoted["promoted_count"] == 1
        assert client.get("/api/objects/Starship/SS-01").json()["item"]["properties"]["sector"] == "omega"

        del_res = client.delete(f"/api/sandboxes/{sid}").json()
        assert del_res["destroyed"] is True

        # Audit + NL assistant
        audit = client.get("/api/audit").json()
        assert audit["summary"]["committed"] >= 1
        assert audit["summary"]["external_dirty_writes"] == 0
        chat = client.post("/api/assistant/chat", json={"message": "Starship"}).json()
        assert chat["ok"] is True and chat["cards"]
    finally:
        svc.close()
        svc.runtime.close()


# ---------------------------------------------------------------------------
# Group C: Enterprise Standard World Model Full-Loop API Suite
# ---------------------------------------------------------------------------
def test_group_c_enterprise_api_auth_rls_workcard_sandbox_and_autonomy() -> None:
    # 1. Fail-closed Bearer token vs disabled demo switch
    strict_svc = build_console_service(mode="standalone", demo_actor_switch=False)
    try:
        strict_client = TestClient(create_console_app(strict_svc))
        # Forged X-Actor-Id rejected when demo_actor_switch=False
        assert strict_client.get("/api/meta", headers={"X-Actor-Id": "act-admin"}).status_code == 403
        # Valid Bearer token authenticates North PM with exact plant_north scope
        north_auth = {"Authorization": f"Bearer {DEMO_TOKEN_SECRET['act-north-pm']}"}
        m_north = strict_client.get("/api/meta", headers=north_auth).json()
        assert m_north["actor"]["actor_id"] == "act-north-pm"
        assert m_north["demo_actors"] == []
        north_wos = [it["object_id"] for it in strict_client.get("/api/objects/WorkOrder", headers=north_auth).json()["items"]]
        assert set(north_wos) == {"WO-200", "WO-201"}
        assert strict_client.get("/api/objects/WorkOrder/WO-202", headers=north_auth).status_code == 403
        assert strict_client.get("/api/meta", headers={"Authorization": "Bearer wrong-token"}).status_code == 403
    finally:
        strict_svc.close()
        strict_svc.runtime.close()

    # 2. Full interactive demo console API suite
    svc = build_console_service(mode="standalone", demo_actor_switch=True)
    try:
        c = TestClient(create_console_app(svc))

        # RLS across CFO / North PM / South SF
        assert {it["object_id"] for it in c.get("/api/objects/WorkOrder").json()["items"]} == {"WO-200", "WO-201", "WO-202"}
        assert {
            it["object_id"]
            for it in c.get("/api/objects/WorkOrder", headers={"X-Actor-Id": "act-north-pm"}).json()["items"]
        } == {"WO-200", "WO-201"}
        assert {
            it["object_id"]
            for it in c.get("/api/objects/WorkOrder", headers={"X-Actor-Id": "act-south-sf"}).json()["items"]
        } == {"WO-202"}
        assert c.get("/api/objects/WorkOrder/WO-201", headers={"X-Actor-Id": "act-south-sf"}).status_code == 403

        # Lineage & CustomerOverlay owned fields
        so_lin = c.get("/api/objects/O2cOrder/SO-301/lineage").json()
        assert so_lin["root"]["properties"]["sales_channel"] == "key_account"
        assert len(so_lin["nodes"]) >= 3

        # WorkCard RBAC, role-mismatch, and SoD self-approval blocks before valid CFO approval
        rbac_res = c.post(
            "/api/work-queue/WC-JE-BONUS-50K/resolve",
            headers={"X-Actor-Id": "act-fin"},
            json={"decision": "approve", "note": "finance lacks resolver role"},
        ).json()
        assert rbac_res["ok"] is False and rbac_res["error_code"] == "PERMISSION_DENIED"
        assert rbac_res["zero_external_dirty_write"] is True

        mismatch_res = c.post(
            "/api/work-queue/WC-JE-BONUS-50K/resolve",
            headers={"X-Actor-Id": "act-north-pm"},
            json={"decision": "approve", "note": "wrong resolver role"},
        ).json()
        assert mismatch_res["ok"] is False and mismatch_res["error_code"] == "WORK_CARD_ASSIGNEE_MISMATCH"

        # Create a CFO-requested WorkCard and assert CFO self-approval is blocked by SoD
        cfo_card = c.post(
            "/api/actions/submit",
            json={
                "action": "open_work_card",
                "params": {
                    "card_id": "WC-SOD-CHECK",
                    "card_kind": "HUMAN_TASK",
                    "target_object_type": "CrmDeal",
                    "target_object_id": "DEAL-EV-NEW",
                    "requested_action": "lose_crm_deal",
                    "assignee_role": "finance_director",
                },
            },
        ).json()
        assert cfo_card["ok"] is True
        sod_res = c.post(
            "/api/work-queue/WC-SOD-CHECK/resolve",
            json={"decision": "approve", "note": "self-approve attempt"},
        ).json()
        assert sod_res["ok"] is False and sod_res["error_code"] == "SOD_SELF_APPROVAL_FORBIDDEN"
        assert sod_res["zero_external_dirty_write"] is True
        # Cleanly cancel synthetic SoD card via steward/admin so queue stays tidy
        c.post(
            "/api/actions/submit",
            headers={"X-Actor-Id": "act-admin"},
            json={"action": "cancel_work_card", "params": {"card_id": "WC-SOD-CHECK", "resolution_note": "cleanup"}},
        )

        # CFO approves WC-JE-BONUS-50K -> completes WorkCard & posts JE-TECH-50K atomically
        ok_res = c.post(
            "/api/work-queue/WC-JE-BONUS-50K/resolve",
            json={"decision": "approve", "note": "CFO核准研发校准费用"},
        ).json()
        assert ok_res["ok"] is True and ok_res["committed"] is True
        assert c.get("/api/objects/WorkCard/WC-JE-BONUS-50K").json()["item"]["properties"]["status"] == "COMPLETED"
        assert c.get("/api/objects/JournalEntry/JE-TECH-50K").json()["item"]["properties"]["status"] == "posted"

        # All 3 Sandbox presets preserve primary state_fingerprint
        meta = c.get("/api/meta").json()
        fp_before = meta["state_fingerprint"]
        for preset in meta["sandbox_presets"]:
            sbx = c.post("/api/sandboxes").json()
            sid = sbx["sandbox_id"]
            sub = c.post(f"/api/sandboxes/{sid}/submit", json={"steps": preset["steps"]}).json()
            assert sub["ok"] is True, f"{preset['preset_id']} failed: {sub}"
            assert sub["primary_fingerprint_unchanged"] is True
            cmp_tb = c.post(
                f"/api/sandboxes/{sid}/function/compute_trial_balance",
                json={"accounting_entity": "ENT-GROUP"},
            ).json()
            assert cmp_tb["primary"]["is_balanced"] is True
            assert cmp_tb["sandbox"]["is_balanced"] is True
            diff = c.get(f"/api/sandboxes/{sid}/diff").json()
            assert diff["total_changes"] >= 3
            assert c.delete(f"/api/sandboxes/{sid}").json()["primary_fingerprint_unchanged"] is True
        assert c.get("/api/meta").json()["state_fingerprint"] == fp_before

        # Redline interception: unshipped invoice INV-301 & disputed PO bill BILL-101 -> 0 dirty writes
        red_inv = c.post(
            "/api/actions/submit",
            json={"action": "post_o2c_invoice", "params": {"invoice_id": "INV-301"}},
        ).json()
        assert red_inv["ok"] is False and red_inv["status"] == "REJECTED"
        assert red_inv["error_code"] == "DELIVERY_REQUIRED_BEFORE_INVOICE"
        assert red_inv["zero_external_dirty_write"] is True

        red_bill = c.post(
            "/api/actions/submit",
            json={"action": "post_p2p_bill", "params": {"bill_id": "BILL-101"}},
        ).json()
        assert red_bill["ok"] is False and red_bill["error_code"] == "PO_DISPUTED_BLOCKED"

        audit_rej = c.get("/api/audit?status=REJECTED").json()
        assert audit_rej["summary"]["rejected"] >= 1
        assert audit_rej["summary"]["external_dirty_writes"] == 0

        # /api/admin/* RBAC enforcement
        assert c.get("/api/admin/devices", headers={"X-Actor-Id": "act-south-sf"}).status_code == 403
        assert c.put(
            "/api/admin/autonomy",
            headers={"X-Actor-Id": "act-fin"},
            json={"default_mode": "SHADOW"},
        ).status_code == 403

        admin_hdr = {"X-Actor-Id": "act-admin"}
        assert c.get("/api/admin/devices", headers=admin_hdr).json()["world_mode"] == "standalone"
        org_res = c.get("/api/admin/org", headers=admin_hdr).json()
        assert len(org_res["units"]) >= 6 and len(org_res["members"]) >= 10
        assert all("auth_token_hash" not in m for m in org_res["members"])

        # Hot-swap autonomy modes: SHADOW -> COPILOT_ESCALATE -> AUTO_UNDER_CEILING
        c.put("/api/admin/autonomy", headers=admin_hdr, json={"default_mode": "SHADOW"})
        sh_res = c.post(
            "/api/actions/submit",
            json={"action": "confirm_p2p_order", "params": {"po_id": "PO-101"}},
        ).json()
        assert sh_res["committed"] is False and sh_res["outcome"] == "SHADOW_WOULD_REJECT"

        c.put("/api/admin/autonomy", headers=admin_hdr, json={"default_mode": "COPILOT_ESCALATE"})
        # Use win_crm_deal on valid DEAL-EV-NEW under copilot -> escalates by opening WorkCard without mutating deal stage
        cop_res = c.post(
            "/api/actions/submit",
            json={"action": "win_crm_deal", "params": {"deal_id": "DEAL-EV-NEW"}},
        ).json()
        assert cop_res["outcome"] == "ESCALATED_COMMITTED"
        assert c.get("/api/objects/CrmDeal/DEAL-EV-NEW").json()["item"]["properties"]["stage"] == "PROPOSAL"

        c.put(
            "/api/admin/autonomy",
            headers=admin_hdr,
            json={"default_mode": "AUTO_UNDER_CEILING", "max_value": 500000.0},
        )

        # AI assistant strict RLS scoping: North PM must never see South WO-202
        chat_north = c.post(
            "/api/assistant/chat",
            headers={"X-Actor-Id": "act-north-pm"},
            json={"message": "我现在能看到哪些厂区和生产工单？"},
        ).json()
        assert chat_north["ok"] is True
        card_ids = {card.get("object_id") for card in chat_north["cards"]}
        assert "WO-202" not in card_ids
        assert "WO-202" not in chat_north["reply"]
    finally:
        svc.close()
        svc.runtime.close()


def test_group_c_security_bearer_fail_closed_anonymous_403() -> None:
    """When demo_actor_switch=False, unauthenticated requests must fail closed with HTTP 403."""
    strict_svc = build_console_service(mode="standalone", demo_actor_switch=False)
    try:
        strict_client = TestClient(create_console_app(strict_svc))

        # Anonymous requests without Bearer token must be rejected across all protected surfaces
        assert strict_client.get("/api/meta").status_code == 403
        assert strict_client.get("/api/objects/O2cOrder").status_code == 403
        assert strict_client.get("/api/admin/devices").status_code == 403
        assert strict_client.post("/api/sandboxes").status_code == 403
        assert strict_client.get("/api/audit").status_code == 403
        assert strict_client.post("/api/assistant/chat", json={"message": "拦截统计"}).status_code == 403

        # Malformed or invalid Bearer credentials fail closed
        assert strict_client.get("/api/objects/O2cOrder", headers={"Authorization": "Basic abc"}).status_code == 403
        assert strict_client.get("/api/objects/O2cOrder", headers={"Authorization": "Bearer invalid"}).status_code == 403

        # Valid Bearer token is authenticated and scoped to plant_north only
        north_auth = {"Authorization": f"Bearer {DEMO_TOKEN_SECRET['act-north-pm']}"}
        orders = strict_client.get("/api/objects/O2cOrder", headers=north_auth)
        assert orders.status_code == 200
        order_ids = {it["object_id"] for it in orders.json()["items"]}
        assert order_ids == {"SO-300", "SO-301"}
        assert strict_client.get("/api/admin/devices", headers=north_auth).status_code == 403

        # Anonymous DELETE on existing sandbox must be rejected with HTTP 403
        created = strict_client.post("/api/sandboxes", headers=north_auth)
        assert created.status_code == 200
        sid = created.json()["sandbox_id"]
        assert strict_client.delete(f"/api/sandboxes/{sid}").status_code == 403
        assert strict_client.delete(f"/api/sandboxes/{sid}", headers=north_auth).status_code == 200
    finally:
        strict_svc.close()
        strict_svc.runtime.close()


def test_group_c_security_audit_and_assistant_cross_plant_rls() -> None:
    """Audit runs, shadow ledger, summary counts, and assistant replies obey strict cross-plant RLS."""
    svc = build_console_service(mode="standalone", demo_actor_switch=True)
    try:
        c = TestClient(create_console_app(svc))
        cfo_hdr = {"X-Actor-Id": "act-cfo"}
        north_hdr = {"X-Actor-Id": "act-north-pm"}

        # Generate CFO GL and South plant shadow/rejected events on top of seeded history
        assert c.put("/api/admin/autonomy", headers=cfo_hdr, json={"default_mode": "SHADOW"}).status_code == 200
        assert (
            c.post(
                "/api/actions/submit",
                headers=cfo_hdr,
                json={"action": "confirm_p2p_order", "params": {"po_id": "PO-102"}},
            ).status_code
            == 200
        )
        assert (
            c.post(
                "/api/actions/submit",
                headers=cfo_hdr,
                json={"action": "post_journal_entry", "params": {"entry_id": "JE-MAINT-20K"}},
            ).status_code
            == 200
        )
        assert (
            c.put(
                "/api/admin/autonomy",
                headers=cfo_hdr,
                json={"default_mode": "AUTO_UNDER_CEILING", "max_value": 500000.0},
            ).status_code
            == 200
        )
        # Trigger rejected runs in South plant & CFO GL domain (both shadow ledger and runtime runs)
        c.post(
            "/api/actions/submit",
            headers=cfo_hdr,
            json={"action": "confirm_p2p_order", "params": {"po_id": "PO-102"}},
        )
        c.post(
            "/api/actions/submit",
            headers=cfo_hdr,
            json={"action": "post_journal_entry", "params": {"entry_id": "JE-01-CAP"}},
        )
        cfo_actor = svc._demo_profiles["act-cfo"].to_actor(svc.org_directory)
        for rid, act_name, act_params in (
            ("RED-SOUTH-PO102", "confirm_p2p_order", {"po_id": "PO-102"}),
            ("RED-CFO-JE01", "post_journal_entry", {"entry_id": "JE-01-CAP"}),
        ):
            try:
                svc.runtime.submit_action(rid, act_name, act_params, cfo_actor)
            except Exception:
                pass

        # North PM must see ZERO South plant or CFO GL runs / shadow entries
        north_audit = c.get("/api/audit", headers=north_hdr).json()
        assert north_audit["ok"] is True
        runs_blob = json.dumps(north_audit["runs"], ensure_ascii=False)
        shadow_blob = json.dumps(north_audit["shadow_entries"], ensure_ascii=False)
        forbidden_ids = (
            "PO-102",
            "WO-202",
            "SO-302",
            "DEL-302",
            "INV-302",
            "CONS-202-1",
            "OUT-202-1",
            "SEED-JE-",
            "JE-MAINT-20K",
            "JE-01-CAP",
            "RED-SOUTH-PO102",
            "RED-CFO-JE01",
        )
        for leaked_id in forbidden_ids:
            assert leaked_id not in runs_blob, f"Cross-plant/GL run leaked to act-north-pm: {leaked_id}"
            assert leaked_id not in shadow_blob, f"Cross-plant/GL shadow leaked to act-north-pm: {leaked_id}"

        assert north_audit["summary"]["total_runs"] == len(north_audit["runs"])
        assert north_audit["summary"]["committed"] == sum(
            1 for r in north_audit["runs"] if r["status"] == "COMMITTED"
        )
        assert north_audit["summary"]["rejected"] == sum(
            1 for r in north_audit["runs"] if r["status"] == "REJECTED"
        )
        assert north_audit["shadow_report"]["total"] == len(north_audit["shadow_entries"])
        assert "PRE-SHD-01" not in shadow_blob
        assert [e["run_id"] for e in north_audit["shadow_entries"]] == ["PRE-SHD-02"]

        north_rej = c.get("/api/audit?status=REJECTED", headers=north_hdr).json()
        cfo_audit = c.get("/api/audit", headers=cfo_hdr).json()
        cfo_rej = c.get("/api/audit?status=REJECTED", headers=cfo_hdr).json()
        assert north_rej["summary"]["rejected"] == 1
        assert cfo_rej["summary"]["rejected"] >= 3
        assert len(cfo_audit["shadow_entries"]) >= 6

        # Assistant must only summarize RLS-visible rejected runs
        chat_north = c.post(
            "/api/assistant/chat",
            headers=north_hdr,
            json={"message": "最近有哪些红线拦截、审计和报错？"},
        ).json()
        assert chat_north["ok"] is True
        chat_blob = json.dumps(chat_north, ensure_ascii=False)
        for leaked_id in ("PO-102", "WO-202", "SO-302", "JE-MAINT-20K", "JE-01-CAP", "post_journal_entry"):
            assert leaked_id not in chat_blob, f"Assistant leaked {leaked_id} to act-north-pm"
        assert {"label": "累计红线拦截", "value": f"{north_rej['summary']['rejected']} 次"} in chat_north["highlights"]

        # CFO retains full global audit view
        cfo_run_ids = {r["run_id"] for r in cfo_audit["runs"]}
        assert "SEED-JE-01" in cfo_run_ids
        assert "SEED-SO302-INV" in cfo_run_ids
        assert "SEED-Z-RED-INV301" in cfo_run_ids
        assert "RED-SOUTH-PO102" in cfo_run_ids
    finally:
        svc.close()
        svc.runtime.close()

    # Blind M2 verification on Starship: nav-alpha must never see sector:beta runs or shadow entries
    star_svc = build_starship_console(demo_actor_switch=True)
    try:
        sc = TestClient(create_console_app(star_svc))
        cmd_hdr = {"X-Actor-Id": "cmd-kirk"}
        alpha_hdr = {"X-Actor-Id": "nav-alpha"}

        sc.post(
            "/api/actions/submit",
            headers=cmd_hdr,
            json={"action": "refuel_ship", "params": {"ship_id": "SS-02", "added_cells": 2}},
        )
        sc.put("/api/admin/autonomy", headers=cmd_hdr, json={"default_mode": "SHADOW"})
        sc.post(
            "/api/actions/submit",
            headers=cmd_hdr,
            json={"action": "jump_sector", "params": {"ship_id": "SS-02", "target_sector": "omega", "cost_cells": 99}},
        )
        sc.put("/api/admin/autonomy", headers=cmd_hdr, json={"default_mode": "AUTO_UNDER_CEILING", "max_value": 100.0})
        sc.post(
            "/api/actions/submit",
            headers=cmd_hdr,
            json={"action": "jump_sector", "params": {"ship_id": "SS-02", "target_sector": "omega", "cost_cells": 99}},
        )
        cmd_actor = star_svc._demo_profiles["cmd-kirk"].to_actor(star_svc.org_directory)
        alpha_actor = star_svc._demo_profiles["nav-alpha"].to_actor(star_svc.org_directory)
        try:
            star_svc.runtime.submit_action(
                "STAR-BETA-REJ",
                "jump_sector",
                {"ship_id": "SS-02", "target_sector": "omega", "cost_cells": 99},
                cmd_actor,
            )
        except Exception:
            pass
        try:
            star_svc.runtime.submit_action(
                "STAR-ALPHA-REJ",
                "jump_sector",
                {"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 99},
                alpha_actor,
            )
        except Exception:
            pass

        alpha_audit = sc.get("/api/audit", headers=alpha_hdr).json()
        assert "SS-02" not in json.dumps(alpha_audit["runs"], ensure_ascii=False)
        assert "SS-02" not in json.dumps(alpha_audit["shadow_entries"], ensure_ascii=False)
        assert "STAR-BETA-REJ" not in json.dumps(alpha_audit["runs"], ensure_ascii=False)
        assert alpha_audit["summary"]["total_runs"] == len(alpha_audit["runs"]) == 1
        assert alpha_audit["runs"][0]["run_id"] == "STAR-ALPHA-REJ"
        assert alpha_audit["shadow_report"]["total"] == len(alpha_audit["shadow_entries"]) == 0

        alpha_chat = sc.post(
            "/api/assistant/chat",
            headers=alpha_hdr,
            json={"message": "拦截审计 Starship"},
        ).json()
        assert "SS-02" not in json.dumps(alpha_chat, ensure_ascii=False)
        assert any(h["label"] == "红线拦截次数" and h["value"] == "1" for h in alpha_chat["highlights"])

        cmd_audit = sc.get("/api/audit", headers=cmd_hdr).json()
        assert "SS-02" in json.dumps(cmd_audit["runs"], ensure_ascii=False)
        assert len(cmd_audit["shadow_entries"]) >= 2
    finally:
        star_svc.close()
        star_svc.runtime.close()


def test_group_c_security_sandbox_cross_actor_idor_and_eviction() -> None:
    """Sandboxes reject non-owner non-admin IDOR across all 6 endpoints and evict bounded sessions."""
    svc = build_console_service(mode="standalone", demo_actor_switch=True)
    try:
        c = TestClient(create_console_app(svc))
        north_hdr = {"X-Actor-Id": "act-north-pm"}
        south_hdr = {"X-Actor-Id": "act-south-sf"}
        fin_hdr = {"X-Actor-Id": "act-fin"}
        cfo_hdr = {"X-Actor-Id": "act-cfo"}

        sbx = c.post("/api/sandboxes", headers=north_hdr).json()
        assert sbx["ok"] is True
        sid = sbx["sandbox_id"]

        for intruder in (south_hdr, fin_hdr):
            assert c.get(f"/api/sandboxes/{sid}/diff", headers=intruder).status_code == 403
            assert c.get(f"/api/sandboxes/{sid}/objects/WorkOrder", headers=intruder).status_code == 403
            assert (
                c.post(
                    f"/api/sandboxes/{sid}/submit",
                    headers=intruder,
                    json={"action": "release_work_order", "params": {"work_order_id": "WO-201"}},
                ).status_code
                == 403
            )
            assert (
                c.post(
                    f"/api/sandboxes/{sid}/function/compute_trial_balance",
                    headers=intruder,
                    json={},
                ).status_code
                == 403
            )
            assert c.post(f"/api/sandboxes/{sid}/promote", headers=intruder).status_code == 403
            assert c.delete(f"/api/sandboxes/{sid}", headers=intruder).status_code == 403

        # Owner and admin are allowed to inspect and manage the sandbox
        assert c.get(f"/api/sandboxes/{sid}/diff", headers=north_hdr).status_code == 200
        assert c.get(f"/api/sandboxes/{sid}/objects/WorkOrder", headers=north_hdr).status_code == 200
        assert c.get(f"/api/sandboxes/{sid}/diff", headers=cfo_hdr).status_code == 200
        assert c.delete(f"/api/sandboxes/{sid}", headers=north_hdr).status_code == 200

        # Bounded sandbox eviction (> 32 concurrent sandboxes evicts oldest)
        created_ids: list[str] = []
        for _ in range(35):
            r = c.post("/api/sandboxes", headers=north_hdr)
            assert r.status_code == 200
            created_ids.append(r.json()["sandbox_id"])
        assert len(svc._sandboxes) <= 32
        assert c.get(f"/api/sandboxes/{created_ids[0]}/diff", headers=north_hdr).status_code == 404
        assert c.get(f"/api/sandboxes/{created_ids[-1]}/diff", headers=north_hdr).status_code == 200
    finally:
        svc.close()
        svc.runtime.close()


# ---------------------------------------------------------------------------
# Group D: Headless Chromium Playwright E2E + 4 Desktop HD Screenshots
# ---------------------------------------------------------------------------
def test_group_d_playwright_e2e_and_four_screenshots() -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_sync.sync_playwright

    dist_index = ROOT / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Frontend dist build missing at {dist_index}; run `cd web && npm run build`"

    svc = build_console_service(mode="standalone", demo_actor_switch=True)
    handle = start_console_server(svc, host="127.0.0.1", port=0, static_dir=ROOT / "web" / "dist")

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
            )
            page = context.new_page()

            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "requestfailed",
                lambda req: failed_requests.append(f"{req.method} {req.url}"),
            )
            page.on(
                "response",
                lambda resp: bad_responses.append(f"{resp.status} {resp.url}")
                if resp.status >= 400
                else None,
            )

            # 1. Visit Cockpit `/`
            page.goto(f"{handle.base_url}/", wait_until="networkidle")
            page.wait_for_selector('[data-testid="enterprise-kpi-bar"]')
            page.wait_for_selector('[data-testid="workcard-WC-JE-BONUS-50K"]')
            page.wait_for_timeout(300)

            assert "¥570,000" in page.inner_text('[data-testid="kpi-orders-total"]')
            page.screenshot(path="/tmp/wo26_ui_cockpit.png", full_page=True)

            # Assert zero jargon on /
            cockpit_text = page.inner_text("body")
            for jword in ("TBox", "ABox", "ObjectType", "state_fingerprint", "InvariantError"):
                assert jword not in cockpit_text

            # Switch to North PM -> verify KPI orders total updates via RLS
            page.select_option('[data-testid="actor-switcher"]', "act-north-pm")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"kpi-orders-total\"]')?.textContent?.includes('420,000')"
            )

            # Switch to South SF -> verify KPI updates and Admin nav tab hidden
            page.select_option('[data-testid="actor-switcher"]', "act-south-sf")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"kpi-orders-total\"]')?.textContent?.includes('150,000')"
            )
            assert page.locator('[data-testid="nav-admin"]').count() == 0

            # Switch back to CFO
            page.select_option('[data-testid="actor-switcher"]', "act-cfo")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"kpi-orders-total\"]')?.textContent?.includes('570,000')"
            )

            # Open object detail drawer on SO-301 -> check CustomerOverlay owned badges -> close
            page.click('[data-testid="entity-tab-O2cOrder"]')
            page.wait_for_selector('[data-testid="record-row-SO-301"]')
            page.click('[data-testid="record-row-SO-301"]')
            page.wait_for_selector('[data-testid="object-detail-drawer"]')
            page.wait_for_selector('[data-testid="owned-badge-sales_channel"]')
            page.click('[data-testid="close-object-drawer"]')

            # Open AI Assistant drawer -> ask question -> verify reply -> close
            page.click('[data-testid="open-assistant-btn"]')
            page.wait_for_selector('[data-testid="assistant-drawer"]')
            page.fill('[data-testid="assistant-input"]', "现在有哪些待我审批的异常或工单？")
            page.click('[data-testid="assistant-send-btn"]')
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"assistant-drawer\"]')?.textContent?.includes('WC-')"
            )
            page.click('[data-testid="close-assistant-drawer"]')

            # Approve WorkCard WC-JE-BONUS-50K in Cockpit -> verify feedback banner
            page.click('[data-testid="approve-WC-JE-BONUS-50K"]')
            page.wait_for_selector('[data-testid="cockpit-action-banner"]')
            assert "已批准" in page.inner_text('[data-testid="cockpit-action-banner"]')

            # 2. Navigate to `/sandbox` and execute executive repair preset
            page.click('[data-testid="nav-sandbox"]')
            page.wait_for_selector('[data-testid="sandbox-page"]')
            assert "沙盘模式：对真实软件零影响" in page.inner_text('[data-testid="sandbox-page"]')
            page.click('[data-testid="sandbox-preset-emergency_repair_20k"]')
            page.wait_for_selector('[data-testid="sandbox-diff-list"]')
            page.wait_for_function(
                "() => document.querySelector('[data-testid=\"sandbox-metric-cash\"]')?.textContent?.includes('575,000')"
            )
            page.wait_for_timeout(300)
            sandbox_text = page.inner_text("body")
            for jword in ("TBox", "ABox", "ObjectType", "state_fingerprint", "InvariantError"):
                assert jword not in sandbox_text
            page.screenshot(path="/tmp/wo26_ui_sandbox.png", full_page=True)
            page.click('[data-testid="sandbox-reset-btn"]')
            page.wait_for_timeout(200)

            # 3. Navigate to `/audit`
            page.click('[data-testid="nav-audit"]')
            page.wait_for_selector('[data-testid="audit-page"]')
            page.wait_for_selector('[data-testid="audit-filter-rejected"]')
            page.wait_for_timeout(250)
            audit_text = page.inner_text("body")
            for jword in ("TBox", "ABox", "ObjectType", "state_fingerprint", "InvariantError"):
                assert jword not in audit_text
            assert "真实系统 0 改动" in audit_text
            page.screenshot(path="/tmp/wo26_ui_audit.png", full_page=True)
            page.click('[data-testid="audit-filter-rejected"]')
            page.wait_for_timeout(150)

            # 4. Navigate to `/admin`
            page.click('[data-testid="nav-admin"]')
            page.wait_for_selector('[data-testid="admin-page"]')
            inspector = page.locator('[data-testid="admin-collapsed-schema-inspector"]')
            assert inspector.get_attribute("open") is None
            page.click('[data-testid="autonomy-mode-SHADOW"]')
            page.wait_for_selector('[data-testid="autonomy-save-feedback"]')
            page.wait_for_timeout(250)
            page.screenshot(path="/tmp/wo26_ui_admin.png", full_page=True)

            browser.close()
    finally:
        handle.stop()

    assert console_errors == [], f"Browser console errors detected: {console_errors}"
    assert page_errors == [], f"Uncaught page errors detected: {page_errors}"
    assert failed_requests == [], f"Failed network requests detected: {failed_requests}"
    assert bad_responses == [], f"HTTP >=400 responses detected: {bad_responses}"

    for png in (
        "/tmp/wo26_ui_cockpit.png",
        "/tmp/wo26_ui_sandbox.png",
        "/tmp/wo26_ui_audit.png",
        "/tmp/wo26_ui_admin.png",
    ):
        fpath = Path(png)
        assert fpath.is_file() and fpath.stat().st_size > 30_000, f"Screenshot missing or too small: {png}"
