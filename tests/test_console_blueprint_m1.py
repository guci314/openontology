"""WO-29 Verification Suite: Read-Only Visual M1 Blueprint Explorer & Z3 Formal Prove."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_ontology import (
    Actor,
    DeviceDriverRegistry,
    MountedDevice,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    SourceBridge,
)
from open_ontology.console_server import (
    ConsoleService,
    create_console_app,
    start_console_server,
)
from open_ontology.induction.prover import ProverUnavailableError
from examples.enterprise_console_app import (
    DEMO_TOKEN_SECRET,
    build_console_service,
    build_starship_console,
)


def _has_z3() -> bool:
    try:
        import z3  # noqa: F401
        return True
    except ImportError:
        return False


def test_blueprint_zero_eager_z3_or_induction_import() -> None:
    """Gate 6 discipline: importing console_server never imports z3 or induction eagerly."""
    code = (
        "import sys, open_ontology.console_server\n"
        "assert 'z3' not in sys.modules, 'z3 must never be imported at console_server import time'\n"
        "bad = [m for m in sys.modules if m == 'open_ontology.induction' or m.startswith('open_ontology.induction.')]\n"
        "assert bad == [], f'induction leaked on console_server import: {bad}'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_blueprint_enterprise_standalone_rls_and_tripartite_kinetics() -> None:
    svc = build_console_service(mode="standalone", demo_actor_switch=True)
    try:
        client = TestClient(create_console_app(svc))

        # Backwards compatibility: /api/tbox intact
        tbox = client.get("/api/tbox").json()
        assert tbox["ok"] is True
        assert len(tbox["object_types"]) == 29

        # CFO blueprint inspection
        cfo_bp = client.get("/api/blueprint").json()
        assert cfo_bp["ok"] is True
        summary = cfo_bp["summary"]
        assert summary["object_type_count"] == 29
        assert summary["rule_action_count"] >= 2
        assert summary["edit_function_action_count"] >= 31
        assert summary["action_count"] == (
            summary["rule_action_count"] + summary["edit_function_action_count"]
        )
        assert summary["edit_function_count"] >= 31
        assert summary["function_count"] == 3
        assert summary["invariant_count"] >= 13
        assert summary["fsm_object_count"] >= 20

        # Zero M0 instance record leak in blueprint payload
        bp_blob = json.dumps(cfo_bp, ensure_ascii=False)
        for sample_id in ("SO-301", "SO-302", "WC-JE-BONUS-50K", "PO-201"):
            assert sample_id not in bp_blob, f"Blueprint leaked M0 instance ID {sample_id}"

        # Ownership & FSM semantics
        by_type = {o["name"]: o for o in cfo_bp["object_types"]}
        o2c = by_type["O2cOrder"]
        o2c_fields = {f["name"]: f for f in o2c["fields"]}
        assert o2c_fields["sales_channel"]["ownership"] == "overlay_extension"
        assert o2c_fields["risk_note"]["ownership"] == "overlay_extension"
        assert o2c_fields["order_id"]["is_primary_key"] is True
        assert o2c_fields["status"]["is_status_field"] is True
        assert "invoiced" in o2c["terminal_statuses"]
        assert "cancelled" in o2c["terminal_statuses"]
        assert "annotate_order_risk_note" in o2c["touched_by_actions"]

        wc = by_type["WorkCard"]
        wc_fields = {f["name"]: f for f in wc["fields"]}
        assert wc_fields["card_id"]["ownership"] == "ontology_owned"

        # Tripartite action kinds: declarative rule vs audited edit_function
        act_by_name = {a["name"]: a for a in cfo_bp["actions"]}
        rule_act = act_by_name["annotate_order_risk_note"]
        assert rule_act["implementation_kind"] == "rule"
        assert rule_act["edit_function_name"] is None
        assert rule_act["rule_summary"]["precondition_count"] == 1
        assert rule_act["rule_summary"]["mutation_count"] == 1
        assert "O2cOrder" in rule_act["touched_object_types"]

        ef_act = act_by_name["post_o2c_invoice"]
        assert ef_act["implementation_kind"] == "edit_function"
        assert ef_act["edit_function_name"] == "post_o2c_invoice"
        assert ef_act["rule_summary"] is None

        # WO-29B: generic rule precondition enum enrichment on /api/blueprint & /api/tbox
        vip_bp_prop = act_by_name["upgrade_account_vip_tier"]["params_schema"]["properties"]["vip_tier"]
        assert vip_bp_prop["enum"] == ["STANDARD", "VIP_KEY", "STRATEGIC"]
        tbox_acts = {a["name"]: a for a in tbox["actions"]}
        vip_tbox_prop = tbox_acts["upgrade_account_vip_tier"]["params"]["properties"]["vip_tier"]
        assert vip_tbox_prop["enum"] == ["STANDARD", "VIP_KEY", "STRATEGIC"]
        meta_payload = client.get("/api/meta").json()
        assert meta_payload["display"]["values"]["STANDARD"] == "标准客户"
        assert meta_payload["display"]["values"]["VIP_KEY"] == "重点大客户"
        assert meta_payload["display"]["values"]["STRATEGIC"] == "战略级客户"

        # Strictly RLS-filtered visible_count (CFO vs North PM)
        pm_bp = client.get(
            "/api/blueprint",
            headers={"X-Actor-Id": "act-north-pm"},
        ).json()
        pm_by_type = {o["name"]: o for o in pm_bp["object_types"]}
        assert cfo_bp["summary"]["object_type_count"] == pm_bp["summary"]["object_type_count"]
        assert pm_by_type["O2cOrder"]["visible_count"] < o2c["visible_count"]
        assert pm_by_type["GlAccount"]["visible_count"] == 0
    finally:
        svc.runtime.close()


def test_blueprint_starship_blind_and_coupled_ownership() -> None:
    # 1. Starship blind M2 blueprint
    star_svc = build_starship_console(demo_actor_switch=True)
    try:
        client = TestClient(create_console_app(star_svc))
        bp = client.get("/api/blueprint").json()
        assert bp["ok"] is True
        assert bp["summary"] == {
            "object_type_count": 2,
            "link_count": 1,
            "action_count": 2,
            "rule_action_count": 0,
            "edit_function_action_count": 2,
            "edit_function_count": 2,
            "function_count": 1,
            "invariant_count": 1,
            "fsm_object_count": 1,
        }
        assert "SS-01" not in json.dumps(bp, ensure_ascii=False)
        by_type = {o["name"]: o for o in bp["object_types"]}
        assert by_type["Starship"]["terminal_statuses"] == ["retired"]
        assert by_type["Starship"]["visible_count"] == 2
        assert by_type["WarpCore"]["transitions"] == {}

        # WO-29B: Literal param enum schema on /api/blueprint & /api/tbox
        jump_bp_enum = next(a for a in bp["actions"] if a["name"] == "jump_sector")["params_schema"]["properties"]["target_sector"]["enum"]
        assert jump_bp_enum == ["alpha", "beta", "omega", "gamma"]
        tbox = client.get("/api/tbox").json()
        jump_tbox_enum = next(a for a in tbox["actions"] if a["name"] == "jump_sector")["params"]["properties"]["target_sector"]["enum"]
        assert jump_tbox_enum == ["alpha", "beta", "omega", "gamma"]

        alpha_bp = client.get(
            "/api/blueprint",
            headers={"X-Actor-Id": "nav-alpha"},
        ).json()
        alpha_types = {o["name"]: o for o in alpha_bp["object_types"]}
        assert alpha_types["Starship"]["visible_count"] == 1
    finally:
        star_svc.runtime.close()

    # 2. Coupled device-mounted blueprint verifying external_synced + overlay_extension
    class RemoteWidget(ObjectType):
        __primary_key__ = "widget_id"
        __owned__ = {"x_tag": "default"}
        widget_id: str
        label: str
        x_tag: str = "default"

    coupled_spec = OntologySpec()
    coupled_spec.object_type(RemoteWidget)
    bus = DeviceDriverRegistry(spec=coupled_spec)
    dummy_bridge = SourceBridge(
        actuate=lambda *_args, **_kwargs: {},
        observe=lambda _ot, _oid, _act: None,
    )
    bus.mount(
        MountedDevice(
            device_id="dev-ext",
            name="External System Driver",
            kind="rest",
            endpoint="http://127.0.0.1:0/mock",
            bridge=dummy_bridge,
            handled_actions=(),
            owned_object_types=("RemoteWidget",),
        )
    )
    coupled_rt = OntologyRuntime(
        coupled_spec,
        source_bridge=bus.as_source_bridge(),
        world_mode="coupled",
    )
    coupled_svc = ConsoleService(coupled_rt, demo_actor_switch=True)
    try:
        c_client = TestClient(create_console_app(coupled_svc))
        c_bp = c_client.get("/api/blueprint").json()
        rw = c_bp["object_types"][0]
        assert rw["name"] == "RemoteWidget"
        assert len(rw["attached_devices"]) == 1
        assert rw["attached_devices"][0]["device_id"] == "dev-ext"
        fmap = {f["name"]: f for f in rw["fields"]}
        assert fmap["widget_id"]["ownership"] == "external_synced"
        assert fmap["label"]["ownership"] == "external_synced"
        assert fmap["x_tag"]["ownership"] == "overlay_extension"
    finally:
        coupled_svc.close()
        coupled_rt.close()


def test_blueprint_endpoints_fail_closed_bearer_auth() -> None:
    svc = build_console_service(mode="standalone", demo_actor_switch=False)
    try:
        client = TestClient(create_console_app(svc))
        r1 = client.get("/api/blueprint")
        assert r1.status_code in (401, 403)
        r2 = client.post("/api/blueprint/prove", json={})
        assert r2.status_code in (401, 403)

        cfo_headers = {"Authorization": f"Bearer {DEMO_TOKEN_SECRET['act-cfo']}"}
        ok1 = client.get("/api/blueprint", headers=cfo_headers)
        assert ok1.status_code == 200
        assert ok1.json()["ok"] is True
        ok2 = client.post("/api/blueprint/prove", headers=cfo_headers, json={})
        assert ok2.status_code == 200
        assert ok2.json()["ok"] is True
    finally:
        svc.runtime.close()


def test_blueprint_prove_caching_and_unavailable_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    star_svc = build_starship_console(demo_actor_switch=True)
    ent_svc = build_console_service(mode="standalone", demo_actor_switch=True)
    try:
        star_client = TestClient(create_console_app(star_svc))
        ent_client = TestClient(create_console_app(ent_svc))

        p1 = star_client.post("/api/blueprint/prove", json={}).json()
        if _has_z3():
            assert p1["ok"] is True
            assert p1["prover_available"] is True
            assert p1["cached"] is False
            assert p1["verdict"] == "PROVEN_CLEAN"
            assert p1["summary"]["deadlocked_actions"] == 0
            assert p1["summary"]["violated_invariants"] == 0
            assert p1["summary"]["fragment_counts"]

            # Second call hits fingerprint cache
            p2 = star_client.post("/api/blueprint/prove").json()
            assert p2["prover_available"] is True
            assert p2["cached"] is True

            # Force refresh bypasses cache
            p3 = star_client.post("/api/blueprint/prove", json={"force_refresh": True}).json()
            assert p3["cached"] is False

            # Enterprise prove also PROVEN_CLEAN with caching
            ep1 = ent_client.post("/api/blueprint/prove", json={}).json()
            assert ep1["prover_available"] is True
            assert ep1["verdict"] == "PROVEN_CLEAN"
            assert ep1["cached"] is False
            ep2 = ent_client.post("/api/blueprint/prove", json={}).json()
            assert ep2["cached"] is True
        else:
            # Py3.11 environment without z3-solver
            assert p1["ok"] is True
            assert p1["prover_available"] is False
            assert p1["reason"] == "PROVER_UNAVAILABLE"

        # Explicit ProverUnavailableError simulation in ALL Python envs
        fresh_star = build_starship_console(demo_actor_switch=True)
        try:
            def _raise_unavail(*_args, **_kwargs):
                raise ProverUnavailableError("simulated missing z3")

            monkeypatch.setattr("open_ontology.induction.prover.prove_blueprint", _raise_unavail)
            m_client = TestClient(create_console_app(fresh_star))
            unavail = m_client.post("/api/blueprint/prove", json={"force_refresh": True})
            assert unavail.status_code == 200
            body = unavail.json()
            assert body["ok"] is True
            assert body["prover_available"] is False
            assert body["reason"] == "PROVER_UNAVAILABLE"
            assert "install_hint" in body
        finally:
            fresh_star.runtime.close()
    finally:
        star_svc.runtime.close()
        ent_svc.runtime.close()


def test_blueprint_playwright_e2e_enterprise_and_starship() -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_sync.sync_playwright

    dist_index = ROOT / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Frontend dist build missing at {dist_index}"

    ent_svc = build_console_service(mode="standalone", demo_actor_switch=True)
    star_svc = build_starship_console(demo_actor_switch=True)
    ent_handle = start_console_server(
        ent_svc, host="127.0.0.1", port=0, static_dir=ROOT / "web" / "dist"
    )
    star_handle = start_console_server(
        star_svc, host="127.0.0.1", port=0, static_dir=ROOT / "web" / "dist"
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []

    def _wire_listeners(pg) -> None:
        pg.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        pg.on("pageerror", lambda exc: page_errors.append(str(exc)))
        pg.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url}"))
        pg.on(
            "response",
            lambda resp: bad_responses.append(f"{resp.status} {resp.url}")
            if resp.status >= 400
            else None,
        )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})

            # 1. Enterprise standalone /blueprint at 1280x800 and 1440x900
            page = context.new_page()
            _wire_listeners(page)
            page.goto(f"{ent_handle.base_url}/blueprint", wait_until="networkidle")
            page.wait_for_selector('[data-testid="blueprint-page"]')
            page.wait_for_selector('[data-testid="blueprint-topology-svg"]')
            page.wait_for_selector('[data-testid="blueprint-node-O2cOrder"]')
            page.wait_for_selector('[data-testid="fsm-diagram-svg"]')

            # Header & horizontal overflow check at 1280x800
            hdr_1280 = page.locator("header").bounding_box()
            assert hdr_1280 is not None and hdr_1280["height"] <= 76, f"Header wrapped at 1280x800: {hdr_1280}"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            # Also check 1440x900
            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_timeout(100)
            hdr_1440 = page.locator("header").bounding_box()
            assert hdr_1440 is not None and hdr_1440["height"] <= 76
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            # Filter declarative rules & run Z3 prove
            page.click('[data-testid="action-filter-rule"]')
            page.wait_for_selector('[data-testid="blueprint-action-annotate_order_risk_note"]')
            page.click('[data-testid="blueprint-tab-primitives"]')
            page.click('[data-testid="blueprint-tab-invariants"]')
            page.click('[data-testid="blueprint-tab-actions"]')

            page.click('[data-testid="run-z3-prove-btn"]')
            page.wait_for_selector('[data-testid="z3-verdict-clean"]')
            verdict_text = page.inner_text('[data-testid="z3-verdict-clean"]')
            assert "0 死锁" in verdict_text
            assert "EXACT_QF_LIA" in verdict_text

            # WO-29B: PROVEN_CLEAN defaults to collapsed details so SVG topology stays visible above the fold
            assert page.locator('[data-testid="z3-action-proof-list"]').count() == 0
            toggle_text = page.inner_text('[data-testid="z3-toggle-collapse-btn"]')
            assert "展开 33 条证明明细" in toggle_text
            topo_box = page.locator('[data-testid="blueprint-topology-svg"]').bounding_box()
            assert topo_box is not None and topo_box["y"] < 700
            page.click('[data-testid="z3-toggle-collapse-btn"]')
            page.wait_for_selector('[data-testid="z3-action-proof-list"]')
            page.close()

            # 2. Starship Pure L1 /blueprint (Zero enterprise copy + Z3 prove + FSM toggle)
            star_page = context.new_page()
            _wire_listeners(star_page)
            star_page.goto(f"{star_handle.base_url}/blueprint", wait_until="networkidle")
            star_page.wait_for_selector('[data-testid="blueprint-page"]')
            star_page.wait_for_selector('[data-testid="blueprint-node-Starship"]')
            star_page.wait_for_selector('[data-testid="blueprint-node-WarpCore"]')
            star_page.wait_for_selector('[data-testid="fsm-diagram-svg"]')

            star_page.click('[data-testid="blueprint-node-WarpCore"]')
            star_page.wait_for_selector('[data-testid="fsm-empty-state"]')

            star_page.click('[data-testid="run-z3-prove-btn"]')
            star_page.wait_for_selector('[data-testid="z3-verdict-clean"]')
            assert star_page.locator('[data-testid="z3-action-proof-list"]').count() == 0

            star_body = star_page.inner_text("body")
            for forbidden in ("经营驾驶舱", "业务台账", "单据", "客户自有", "客户", "业务"):
                assert forbidden not in star_body, (
                    f"Starship /blueprint 泄漏企业词汇：{forbidden}"
                )
            for jargon in ("TBox", "ABox", "ObjectType", "state_fingerprint", "InvariantError"):
                assert jargon not in star_body, (
                    f"Starship /blueprint 夹带底层技术黑话：{jargon}"
                )

            # 3. WO-29B: Starship /sandbox semantic 3-way pickers (Combobox + localized Enum <select>)
            star_page.click('[data-testid="nav-sandbox"]')
            star_page.wait_for_selector('[data-testid="sandbox-page"]')
            star_page.select_option('[data-testid="sandbox-action-select"]', "jump_sector")
            star_page.wait_for_selector('[data-testid="sandbox-param-ship_id"]')

            # Open ship_id combobox -> strictly Starship records (Enterprise & Voyager), ZERO WarpCore
            star_page.click('[data-testid="sandbox-param-ship_id"]')
            star_page.wait_for_selector('[data-testid="combobox-menu-ship_id"]')
            menu_text = star_page.inner_text('[data-testid="combobox-menu-ship_id"]')
            assert "Enterprise" in menu_text
            assert "Voyager" in menu_text
            assert "WC-1" not in menu_text
            assert "WC-2" not in menu_text

            star_page.click('[data-testid="combobox-option-SS-01"]')
            assert star_page.locator('[data-testid="sandbox-param-ship_id"]').input_value() == "SS-01"
            resolved_pill = star_page.inner_text('[data-testid="combobox-resolved-ship_id"]')
            assert "Enterprise" in resolved_pill
            assert "SS-01" in resolved_pill

            # Inspect target_sector <select> -> localized options, ZERO object IDs
            sector_tag = star_page.locator('[data-testid="sandbox-param-target_sector"]').evaluate(
                "el => el.tagName"
            )
            assert sector_tag == "SELECT"
            sector_options_text = star_page.inner_text('[data-testid="sandbox-param-target_sector"]')
            assert "Omega 星域（omega）" in sector_options_text
            assert "SS-01" not in sector_options_text
            assert "WC-1" not in sector_options_text

            star_page.select_option('[data-testid="sandbox-param-target_sector"]', "omega")
            star_page.click('[data-testid="sandbox-custom-submit"]')
            star_page.wait_for_selector('[data-testid="sandbox-diff-list"]')
            diff_text = star_page.inner_text('[data-testid="sandbox-diff-list"]')
            assert "SS-01" in diff_text
            assert "Enterprise" in diff_text

            sandbox_body = star_page.inner_text("body")
            for forbidden in ("经营驾驶舱", "业务台账", "单据", "客户自有", "客户", "业务"):
                assert forbidden not in sandbox_body, (
                    f"Starship /sandbox 泄漏企业词汇：{forbidden}"
                )
            star_page.close()

            browser.close()
    finally:
        ent_handle.stop()
        star_handle.stop()

    assert console_errors == [], f"Console errors: {console_errors}"
    assert page_errors == [], f"Page errors: {page_errors}"
    assert failed_requests == [], f"Failed requests: {failed_requests}"
    assert bad_responses == [], f"HTTP >=400 responses: {bad_responses}"
