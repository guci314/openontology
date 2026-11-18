"""Tests for OntologyRuntime.inspect_health(), /api/world-health, and Workshop Health Badge."""

from __future__ import annotations

import itertools
import json
import os
import urllib.request
from urllib.parse import urlencode

import pytest

from examples.command_center_demo import build_default_command_center
from examples.kanban import (
    BOARD_MANAGER,
    Card,
    spec as kanban_spec,
)
from open_ontology import (
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    start_command_center_server,
)

_RUN_SEQ = itertools.count(1)


def _next_run(prefix: str = "WH") -> str:
    return f"{prefix}-{next(_RUN_SEQ):05d}"


def _get_json(base: str, path: str, **params) -> dict:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base}{path}" + (f"?{query}" if query else "")
    with urllib.request.urlopen(url, timeout=30) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Runtime inspect_health(): clean & multi-violation non-aborting collection
# ---------------------------------------------------------------------------
def test_inspect_health_clean_kanban_and_zero_invariant_spec():
    # Clean Kanban runtime (3 registered invariants)
    rt = OntologyRuntime(kanban_spec)
    try:
        rt.submit_action(
            _next_run(),
            "create_board",
            {
                "board_key": "BD-1",
                "title": "健康看板",
                "tenant": "acme",
                "created_by": "board-manager-01",
            },
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run(),
            "add_member",
            {
                "member_id": "M-1",
                "name": "开发者",
                "role": "developer",
                "wip_limit": 1,
            },
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run(),
            "create_card",
            {"card_id": "C-1", "board": "BD-1", "title": "干净卡"},
            BOARD_MANAGER,
        )

        report = rt.inspect_health()
        assert report["healthy"] is True
        assert report["objects_checked"] == 3
        assert report["links_checked"] == 1
        assert report["invariants_total"] == 3
        assert report["invariants_passed"] == 3
        assert report["violations"] == []
        assert [inv["name"] for inv in report["invariants"]] == [
            "wip_and_claim_integrity",
            "retry_and_status_consistency",
            "dependency_dag_and_board_scope",
        ]
        assert all(inv["passed"] is True for inv in report["invariants"])
    finally:
        rt.close()

    # Zero-invariant spec still validates schema and references cleanly
    class _Item(ObjectType):
        __primary_key__ = "item_id"
        item_id: str
        label: str

    plain_spec = OntologySpec()
    plain_spec.object_type(_Item)
    rt_plain = OntologyRuntime(plain_spec)
    try:
        rt_plain.put_object(_Item(item_id="I-1", label="ok"))
        report = rt_plain.inspect_health()
        assert report["healthy"] is True
        assert report["objects_checked"] == 1
        assert report["invariants_total"] == 0
        assert report["invariants_passed"] == 0
        assert report["violations"] == []
    finally:
        rt_plain.close()


def test_inspect_health_collects_all_reference_and_invariant_violations_without_aborting():
    rt = OntologyRuntime(kanban_spec)
    try:
        rt.submit_action(
            _next_run(),
            "create_board",
            {
                "board_key": "BD-1",
                "title": "看板",
                "tenant": "acme",
                "created_by": "board-manager-01",
            },
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run(),
            "add_member",
            {
                "member_id": "M-1",
                "name": "开发者",
                "role": "developer",
                "wip_limit": 1,
            },
            BOARD_MANAGER,
        )
        # Bypass ActionDef via put_object:
        # - board="BD-MISSING" -> DANGLING_OBJECT_PROPERTY + INVARIANT_DEPENDENCY_DAG
        # - status="doing" without assignee -> INVARIANT_WIP_AND_CLAIM
        # - consecutive_failures=9 > max_retries=3 -> INVARIANT_RETRY_CONSISTENCY
        rt.put_object(
            Card(
                card_id="C-DIRTY",
                board="BD-MISSING",
                tenant="acme",
                title="旁路脏卡",
                status="doing",
                assignee=None,
                consecutive_failures=9,
                max_retries=3,
            )
        )
        # Direct orphan row in ontology_links -> DANGLING_LINK
        with rt._write_gate() as conn:
            conn.execute(
                """
                INSERT INTO ontology_links (
                    link_type, source_type, source_id, target_type, target_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("Card.assignee", "Card", "C-GHOST", "Member", "M-GHOST"),
            )

        report = rt.inspect_health()
        assert report["healthy"] is False
        assert report["invariants_total"] == 3
        assert report["invariants_passed"] == 0

        violation_codes = {v["code"] for v in report["violations"]}
        assert {
            "DANGLING_OBJECT_PROPERTY",
            "DANGLING_LINK",
            "INVARIANT_WIP_AND_CLAIM",
            "INVARIANT_RETRY_CONSISTENCY",
            "INVARIANT_DEPENDENCY_DAG",
        }.issubset(violation_codes)
        # Dangling ObjectProperty synced into ontology_links must be reported once, not twice
        prop_hits = [
            v for v in report["violations"] if v["subject"] == "Card:C-DIRTY.board"
        ]
        assert len(prop_hits) == 1
    finally:
        rt.close()


def test_inspect_health_detects_schema_drift_and_unknown_type():
    rt = OntologyRuntime(kanban_spec)
    try:
        rt.submit_action(
            _next_run(),
            "create_board",
            {
                "board_key": "BD-1",
                "title": "看板",
                "tenant": "acme",
                "created_by": "board-manager-01",
            },
            BOARD_MANAGER,
        )
        # Simulate schema drift (invalid status literal) and unknown object type directly in DB
        with rt._write_gate() as conn:
            conn.execute(
                """
                INSERT INTO ontology_objects (
                    object_type, object_id, title, properties_json, version,
                    security_tags_json, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, 1, '[]', 'hash1', '2026-11-17T00:00:00Z')
                """,
                (
                    "Card",
                    "C-SCHEMA-BAD",
                    "坏状态卡",
                    json.dumps(
                        {
                            "card_id": "C-SCHEMA-BAD",
                            "board": "BD-1",
                            "tenant": "acme",
                            "title": "坏状态卡",
                            "status": "corrupted_status",
                        }
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO ontology_objects (
                    object_type, object_id, title, properties_json, version,
                    security_tags_json, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, 1, '[]', 'hash2', '2026-11-17T00:00:00Z')
                """,
                ("GhostType", "G-1", "幽灵对象", "{}"),
            )

        report = rt.inspect_health()
        assert report["healthy"] is False
        assert report["objects_checked"] == 3
        schema_violations = {
            (v["subject"], v["code"])
            for v in report["violations"]
            if v["kind"] == "schema"
        }
        assert ("Card:C-SCHEMA-BAD", "SCHEMA_VALIDATION_ERROR") in schema_violations
        assert ("GhostType:G-1", "UNKNOWN_OBJECT_TYPE") in schema_violations
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. Workshop HTTP routes & Playwright Health Badge UI
# ---------------------------------------------------------------------------
@pytest.fixture()
def live(tmp_path):
    registry = build_default_command_center(data_dir=tmp_path)
    handle = start_command_center_server(
        registry, port=0, title="OpenOntology 统一作战指挥中心"
    )
    yield registry, handle
    handle.stop()


def test_workshop_health_endpoints_preserve_liveness_and_report_dirty_world(live):
    registry, handle = live
    base = handle.base_url

    # Existing server liveness probe remains intact
    liveness = _get_json(base, "/api/health")
    assert liveness == {"ok": True}

    clean_world = _get_json(base, "/api/world-health", workspace="board-prod")
    assert clean_world["ok"] is True
    assert clean_world["instance"] == "board-prod"
    assert clean_world["workspace"] == "board-prod"
    assert clean_world["healthy"] is True
    assert clean_world["invariants_total"] == 3
    assert clean_world["invariants_passed"] == 3
    assert clean_world["violations"] == []

    clean_rest = _get_json(base, "/api/worlds/board-prod/health")
    assert clean_rest["ok"] is True
    assert clean_rest["world_id"] == "board-prod"
    assert clean_rest["healthy"] is True
    assert clean_rest["violations"] == []

    # Inject bypass dirty object directly into board-prod runtime
    board_rt = registry.get_workspace("board-prod").runtime
    board_rt.put_object(
        Card(
            card_id="C-BYPASS",
            board="BD-UNKNOWN",
            tenant="acme",
            title="未校验脏数据",
            status="doing",
            assignee=None,
            consecutive_failures=8,
            max_retries=3,
        )
    )

    dirty_world = _get_json(base, "/api/world-health", workspace="board-prod")
    assert dirty_world["healthy"] is False
    assert dirty_world["invariants_passed"] == 0
    codes = {v["code"] for v in dirty_world["violations"]}
    assert {
        "DANGLING_OBJECT_PROPERTY",
        "INVARIANT_WIP_AND_CLAIM",
        "INVARIANT_RETRY_CONSISTENCY",
        "INVARIANT_DEPENDENCY_DAG",
    }.issubset(codes)

    dirty_rest = _get_json(base, "/api/worlds/board-prod/health")
    assert dirty_rest["healthy"] is False
    assert dirty_rest["violations"] == dirty_world["violations"]


sync_api = pytest.importorskip("playwright.sync_api")
BROWSER_ENGINE = os.environ.get("TK019_BROWSER", "firefox").strip().lower()
if BROWSER_ENGINE not in {"firefox", "chromium"}:
    BROWSER_ENGINE = "firefox"


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        launched = getattr(pw, BROWSER_ENGINE).launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()


def test_browser_world_health_badge_and_diagnostics_panel(live, browser):
    registry, handle = live
    base = handle.base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    try:
        page.goto(base, wait_until="networkidle")
        page.locator("#tab-instance").click()
        page.get_by_text("任务看板 · 示例实例").first.click()
        page.wait_for_function(
            "() => (document.getElementById('stage')?.innerText || '').includes('board-prod') && !!document.getElementById('healthChip')"
        )

        # 1) Clean state: passive green badge, collapsed drawer, intact #fp
        assert "🟢 自洽 (3/3 不变量)" in page.inner_text("#healthChip")
        assert page.locator("#worldHealthPanel").is_hidden() is True
        fp_text_before = page.inner_text("#fp")
        assert "对象" in fp_text_before and "执行记录" in fp_text_before

        # 2) Clicking chip expands diagnostics panel with recheck button
        page.locator("#healthChip").click()
        page.wait_for_selector("#recheckHealthBtn")
        assert page.locator("#worldHealthPanel").is_visible() is True
        assert "已校验对象" in page.inner_text("#worldHealthPanel")

        # 3) Inject bypass dirty card and click recheck -> red badge + violations shown
        board_rt = registry.get_workspace("board-prod").runtime
        board_rt.put_object(
            Card(
                card_id="C-DIRTY-UI",
                board="BD-NOPE",
                tenant="acme",
                title="UI脏数据",
                status="doing",
                assignee=None,
                consecutive_failures=7,
                max_retries=3,
            )
        )
        page.locator("#recheckHealthBtn").click()
        page.wait_for_function(
            "() => (document.getElementById('healthChip')?.innerText || '').includes('🔴')"
        )
        chip_text = page.inner_text("#healthChip")
        panel_text = page.inner_text("#worldHealthPanel")
        assert "🔴" in chip_text and "项状态违背" in chip_text
        assert "DANGLING_OBJECT_PROPERTY" in panel_text
        assert "INVARIANT_WIP_AND_CLAIM" in panel_text
        assert "INVARIANT_RETRY_CONSISTENCY" in panel_text
        assert page.inner_text("#fp") == fp_text_before

        # 4) Reloading world shows red badge immediately on mount and toggles open on click
        page.reload(wait_until="networkidle")
        page.locator("#tab-instance").click()
        page.get_by_text("任务看板 · 示例实例").first.click()
        page.wait_for_function(
            "() => (document.getElementById('stage')?.innerText || '').includes('board-prod') && !!document.getElementById('healthChip')"
        )
        assert "🔴" in page.inner_text("#healthChip")
        assert page.locator("#worldHealthPanel").is_hidden() is True
        page.locator("#healthChip").click()
        assert page.locator("#worldHealthPanel").is_visible() is True
        assert "BD-NOPE" in page.inner_text("#worldHealthPanel")
        assert errors == []
    finally:
        context.close()
