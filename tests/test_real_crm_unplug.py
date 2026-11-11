"""Acceptance tests: real BottleCRM + real Odoo 19 → unplug SaaS (TK-ONTO-010).

The suite touches two REAL open-source systems:

* BottleCRM (PyPI ``django-crm==1.44.0``) — its SQLite database was created by
  its own Django migrations, then seeded through its real ORM.
* Odoo 19 (``erp_north``) — real ``sale.order`` rows over JSON-RPC.

Nothing here mocks an external SaaS.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from examples import crm_unplug_ontology as dom
from examples import real_crm_unplug_demo as demo
from examples.odoo_dual_erp_demo import DB_NORTH, ensure_database
from open_ontology import GovernanceViolationError, InvariantError

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor_crm"
WHEEL = VENDOR / "django_crm-1.44.0-py3-none-any.whl"
SRC = VENDOR / "bottlecrm_src"
DB = VENDOR / "bottlecrm.sqlite3"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def odoo_client():
    client, _ = ensure_database(DB_NORTH)
    return client


@pytest.fixture()
def absorbed(odoo_client):
    """Phase A state: real data from both SaaS systems is inside the ontology."""
    demo.reset_unplug_state()
    bottlecrm = demo.BottleCRMConnector()
    demo.seed_real_odoo_orders(odoo_client)
    erp = demo.OdooERPConnector(odoo_client)
    runtime = dom.new_runtime()
    demo.absorb_from_real_saas(runtime, bottlecrm, erp)
    demo.apply_overlay_governance(runtime)
    yield runtime, bottlecrm, erp
    runtime.close()
    demo.reset_unplug_state()


@pytest.fixture()
def unplugged(absorbed):
    """Phase B state: connectors cut, all types promoted to owned."""
    runtime, bottlecrm, erp = absorbed
    result = demo.unplug_all_saas(runtime, bottlecrm, erp)
    return runtime, bottlecrm, erp, result


def _small_order(runtime, account_id: str, order_id: str, cents: int) -> dom.SalesOrder:
    order = dom.SalesOrder(
        order_id=order_id,
        order_ref=order_id,
        account_id=account_id,
        status="confirmed",
        total_cents=cents,
        currency="USD",
        ordered_on=date.today().isoformat(),
    )
    runtime.put_object(order)
    return order


# ---------------------------------------------------------------------------
# 1. The open-source CRM was really downloaded and initialised
# ---------------------------------------------------------------------------
def test_real_bottlecrm_wheel_and_source_are_present():
    assert WHEEL.exists(), f"missing {WHEEL}"
    assert WHEEL.stat().st_size > 1_000_000
    assert SRC.is_dir()
    for app in ("accounts", "opportunity", "cases", "contacts"):
        assert (SRC / app / "models.py").exists(), f"missing real app {app}"
    assert (SRC / "accounts" / "migrations").is_dir()


def test_real_bottlecrm_database_has_real_tables_and_rows():
    assert DB.exists(), f"missing {DB}"
    conn = sqlite3.connect(DB)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in ("accounts", "opportunity", "case", "contacts"):
            assert table in tables, f"missing real table {table}"

        accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        opportunities = conn.execute(
            "SELECT COUNT(*) FROM opportunity"
        ).fetchone()[0]
        cases = conn.execute('SELECT COUNT(*) FROM "case"').fetchone()[0]
        assert accounts >= 3
        assert opportunities >= 3
        assert cases >= 3

        # the rows carry real business content, not placeholders
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM accounts")
        }
        assert "Acme China Manufacturing" in names
    finally:
        conn.close()


def test_real_odoo_orders_exist_for_the_demo(odoo_client):
    rows = odoo_client.search_read(
        "sale.order",
        [("client_order_ref", "=like", "UNPLUG-%")],
        fields=["id", "name", "client_order_ref", "state", "amount_total"],
    )
    assert len(rows) >= 2
    refs = {row["client_order_ref"] for row in rows}
    assert {"UNPLUG-SO-001", "UNPLUG-SO-002"} <= refs
    assert all(row["state"] == "sale" for row in rows)


# ---------------------------------------------------------------------------
# 2. Phase A absorption
# ---------------------------------------------------------------------------
def test_phase_a_absorbs_both_real_saas(absorbed):
    runtime, bottlecrm, erp = absorbed
    assert runtime.count_objects("CustomerAccount") == 3
    assert runtime.count_objects("SalesOpportunity") == 3
    assert runtime.count_objects("SupportCase") == 3
    assert runtime.count_objects("SalesOrder") >= 2
    assert bottlecrm.reads > 0
    assert erp.reads > 0

    # data really came from BottleCRM
    account_names = {
        account.name
        for account in runtime.query_objects(dom.CustomerAccount, dom.AI_AGENT)
    }
    assert "Acme China Manufacturing" in account_names

    # links follow the real CRM relations
    account = next(
        row
        for row in runtime.query_objects(dom.CustomerAccount, dom.AI_AGENT)
        if row.name == "Acme China Manufacturing"
    )
    opportunities = runtime.get_linked(
        account, dom.ACCOUNT_OPPORTUNITIES, dom.SalesOpportunity, dom.AI_AGENT
    )
    cases = runtime.get_linked(
        account, dom.ACCOUNT_CASES, dom.SupportCase, dom.AI_AGENT
    )
    assert len(opportunities) >= 1
    assert len(cases) >= 1


def test_phase_a_overlay_survives_real_saas_resync(absorbed):
    runtime, bottlecrm, erp = absorbed
    before = demo.snapshot_overlay(runtime)
    assert len(before) == 3

    case_before = {
        case.case_id: (case.fraud_score, case.vip_tier)
        for case in runtime.query_objects(dom.SupportCase, dom.AI_AGENT)
    }

    # a real full re-index from both SaaS systems
    result = demo.absorb_from_real_saas(runtime, bottlecrm, erp)
    assert result.snapshot["overlays_reapplied"] == 3

    after = demo.snapshot_overlay(runtime)
    assert before == after

    case_after = {
        case.case_id: (case.fraud_score, case.vip_tier)
        for case in runtime.query_objects(dom.SupportCase, dom.AI_AGENT)
    }
    assert case_before == case_after


# ---------------------------------------------------------------------------
# 3. Phase B unplug
# ---------------------------------------------------------------------------
def test_unplug_disconnects_both_saas_physically(unplugged):
    runtime, bottlecrm, erp, result = unplugged
    assert bottlecrm.connected is False
    assert erp.connected is False
    assert erp.client is None
    assert set(result.blocked_calls) == {
        "bottlecrm.fetch_accounts",
        "bottlecrm.fetch_opportunities",
        "bottlecrm.fetch_cases",
        "erp.fetch_orders",
    }

    # every connector call must now raise
    for call in (
        bottlecrm.fetch_accounts,
        bottlecrm.fetch_opportunities,
        bottlecrm.fetch_cases,
        erp.fetch_orders,
    ):
        with pytest.raises(demo.SaasDisconnectedError):
            call()


def test_unplug_promotes_every_type_to_owned(unplugged):
    runtime, _, _, result = unplugged
    assert set(result.promoted_types) == set(demo.UNPLUGGABLE_TYPES)
    for type_name in demo.UNPLUGGABLE_TYPES:
        cls = dom.spec.get_object_type(type_name)
        assert cls.is_fully_owned() is True, type_name

    # owned types reject any external snapshot
    assert set(result.snapshot_guard_rejected) == set(demo.UNPLUGGABLE_TYPES)
    with pytest.raises(GovernanceViolationError):
        runtime.load_snapshot({"SalesOrder": []})


def test_unplug_preserves_all_absorbed_data_and_overlay(unplugged):
    runtime, _, _, result = unplugged
    assert runtime.count_objects("CustomerAccount") == 3
    assert runtime.count_objects("SalesOpportunity") == 3
    assert runtime.count_objects("SupportCase") == 3
    assert runtime.count_objects("SalesOrder") >= 2
    assert len(result.overlay_preserved) == 3

    triaged = [
        case
        for case in runtime.query_objects(dom.SupportCase, dom.AI_AGENT)
        if case.fraud_score > 0
    ]
    assert len(triaged) == 3
    assert {case.fraud_score for case in triaged} == {85, 35, 15}


# ---------------------------------------------------------------------------
# 4. Phase B offline business flows
# ---------------------------------------------------------------------------
def test_offline_win_opportunity_creates_order(unplugged):
    runtime, _, _, _ = unplugged
    opportunity = next(
        opp
        for opp in runtime.query_objects(dom.SalesOpportunity, dom.AI_AGENT)
        if opp.stage not in ("CLOSED_WON", "CLOSED_LOST")
    )
    orders_before = runtime.count_objects("SalesOrder")

    run = runtime.submit_action(
        "T-WIN",
        "win_opportunity_and_create_order",
        {
            "opportunity_id": opportunity.opportunity_id,
            "order_ref": "SO-TEST-WIN",
        },
        dom.AI_AGENT,
    )
    assert run["status"] == "COMMITTED"
    assert runtime.count_objects("SalesOrder") == orders_before + 1

    won = runtime.get_object(
        dom.SalesOpportunity, opportunity.opportunity_id, dom.OPS_MANAGER
    )
    assert won.stage == "CLOSED_WON"
    assert won.win_probability == 100

    order = runtime.get_object(dom.SalesOrder, "SO-TEST-WIN", dom.OPS_MANAGER)
    assert order.status == "confirmed"
    assert order.total_cents == opportunity.amount_cents


def test_offline_small_refund_is_auto_committed(unplugged):
    runtime, _, _, _ = unplugged
    account_id = runtime.query_objects(
        dom.CustomerAccount, dom.AI_AGENT
    )[0].account_id
    _small_order(runtime, account_id, "SO-SMALL", 8_000)  # $80

    runtime.submit_action(
        "T-SMALL-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-SMALL",
            "account_id": account_id,
            "subject": "packaging damage",
            "order_id": "SO-SMALL",
        },
        dom.SUPPORT_AGENT,
    )
    run = runtime.submit_action(
        "T-SMALL-REFUND",
        "resolve_case_with_refund",
        {"case_id": "CASE-SMALL", "order_id": "SO-SMALL"},
        dom.AI_AGENT,
    )
    assert run["status"] == "COMMITTED"
    assert run["approved_by"] is None

    order = runtime.get_object(dom.SalesOrder, "SO-SMALL", dom.OPS_MANAGER)
    case = runtime.get_object(dom.SupportCase, "CASE-SMALL", dom.OPS_MANAGER)
    assert order.status == "refunded"
    assert order.refunded_cents == 8_000
    assert case.status == "Resolved"

    settlements = runtime.query_objects(dom.RefundSettlement, dom.OPS_MANAGER)
    assert [row.settlement_id for row in settlements] == ["RFD-CASE-SMALL"]


def test_offline_large_refund_needs_manager_approval(unplugged):
    runtime, _, _, _ = unplugged
    account_id = runtime.query_objects(
        dom.CustomerAccount, dom.AI_AGENT
    )[0].account_id
    _small_order(runtime, account_id, "SO-BIG", 50_000)  # $500

    runtime.submit_action(
        "T-BIG-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-BIG",
            "account_id": account_id,
            "subject": "large refund",
            "order_id": "SO-BIG",
        },
        dom.SUPPORT_AGENT,
    )
    pending = runtime.submit_action(
        "T-BIG-REFUND",
        "resolve_case_with_refund",
        {"case_id": "CASE-BIG", "order_id": "SO-BIG"},
        dom.AI_AGENT,
    )
    assert pending["status"] == "PENDING_APPROVAL"

    # nothing changed while pending
    order = runtime.get_object(dom.SalesOrder, "SO-BIG", dom.OPS_MANAGER)
    assert order.status == "confirmed"
    assert order.refunded_cents == 0

    approved = runtime.approve_action("T-BIG-REFUND", dom.OPS_MANAGER)
    assert approved["status"] == "COMMITTED"
    assert approved["approved_by"] == dom.OPS_MANAGER.actor_id

    order = runtime.get_object(dom.SalesOrder, "SO-BIG", dom.OPS_MANAGER)
    assert order.status == "refunded"
    assert order.refunded_cents == 50_000


def test_offline_return_window_guard_blocks_and_audits(unplugged):
    runtime, _, _, _ = unplugged
    account_id = runtime.query_objects(
        dom.CustomerAccount, dom.AI_AGENT
    )[0].account_id
    stale = dom.SalesOrder(
        order_id="SO-STALE",
        order_ref="SO-STALE",
        account_id=account_id,
        status="confirmed",
        total_cents=5_000,
        currency="USD",
        ordered_on=(date.today() - timedelta(days=45)).isoformat(),
    )
    runtime.put_object(stale)
    runtime.submit_action(
        "T-STALE-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-STALE",
            "account_id": account_id,
            "subject": "45 day old order",
            "order_id": "SO-STALE",
        },
        dom.SUPPORT_AGENT,
    )

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "T-STALE-REFUND",
            "resolve_case_with_refund",
            {"case_id": "CASE-STALE", "order_id": "SO-STALE"},
            dom.AI_AGENT,
        )
    assert excinfo.value.code == "RETURN_WINDOW_EXPIRED"

    run = runtime.get_action_run("T-STALE-REFUND")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "RETURN_WINDOW_EXPIRED"

    order = runtime.get_object(dom.SalesOrder, "SO-STALE", dom.OPS_MANAGER)
    assert order.status == "confirmed"
    assert order.refunded_cents == 0


def test_offline_high_fraud_guard_blocks_and_audits(unplugged):
    runtime, _, _, _ = unplugged
    account_id = runtime.query_objects(
        dom.CustomerAccount, dom.AI_AGENT
    )[0].account_id
    _small_order(runtime, account_id, "SO-FRAUD", 4_000)
    runtime.submit_action(
        "T-FRAUD-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-FRAUD",
            "account_id": account_id,
            "subject": "suspicious refund",
            "order_id": "SO-FRAUD",
        },
        dom.SUPPORT_AGENT,
    )
    runtime.submit_action(
        "T-FRAUD-TRIAGE",
        "ai_triage_crm_case",
        {"case_id": "CASE-FRAUD", "fraud_score": 92},
        dom.AI_AGENT,
    )

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "T-FRAUD-REFUND",
            "resolve_case_with_refund",
            {"case_id": "CASE-FRAUD", "order_id": "SO-FRAUD"},
            dom.AI_AGENT,
        )
    assert excinfo.value.code == "HIGH_FRAUD_RISK_BLOCKED"

    run = runtime.get_action_run("T-FRAUD-REFUND")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "HIGH_FRAUD_RISK_BLOCKED"
    order = runtime.get_object(dom.SalesOrder, "SO-FRAUD", dom.OPS_MANAGER)
    assert order.status == "confirmed"


def test_offline_time_machine_restores_refund(unplugged):
    runtime, _, _, _ = unplugged
    account_id = runtime.query_objects(
        dom.CustomerAccount, dom.AI_AGENT
    )[0].account_id
    _small_order(runtime, account_id, "SO-REV", 6_000)
    runtime.submit_action(
        "T-REV-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-REV",
            "account_id": account_id,
            "subject": "refund then revert",
            "order_id": "SO-REV",
        },
        dom.SUPPORT_AGENT,
    )
    runtime.submit_action(
        "T-REV-REFUND",
        "resolve_case_with_refund",
        {"case_id": "CASE-REV", "order_id": "SO-REV"},
        dom.AI_AGENT,
    )
    assert (
        runtime.get_object(dom.SalesOrder, "SO-REV", dom.OPS_MANAGER).status
        == "refunded"
    )

    revert = runtime.revert_action(
        "T-REV-REFUND", "T-REV-REFUND-BACK", dom.OPS_MANAGER
    )
    assert revert["status"] == "COMMITTED"
    assert runtime.get_action_run("T-REV-REFUND")["status"] == "REVERTED"

    order = runtime.get_object(dom.SalesOrder, "SO-REV", dom.OPS_MANAGER)
    case = runtime.get_object(dom.SupportCase, "CASE-REV", dom.OPS_MANAGER)
    assert order.status == "confirmed"
    assert order.refunded_cents == 0
    assert case.status == "New"

    # the settlement itself was rolled back
    with pytest.raises(Exception):
        runtime.get_object(dom.RefundSettlement, "RFD-CASE-REV", dom.OPS_MANAGER)


# ---------------------------------------------------------------------------
# 5. Full offline portfolio health + purity
# ---------------------------------------------------------------------------
def test_offline_portfolio_health_runs_with_zero_saas(unplugged):
    runtime, bottlecrm, erp, _ = unplugged
    health = runtime.call_function(
        "portfolio_health_snapshot", {}, dom.OPS_MANAGER
    )
    assert health["accounts"] == 3
    assert health["opportunities"]["total"] == 3
    assert health["cases"]["total"] == 3
    assert health["orders"]["total"] >= 2
    assert bottlecrm.connected is False
    assert erp.connected is False


def test_domain_model_is_pure_business():
    text = (ROOT / "examples" / "crm_unplug_ontology.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in (
        "sqlite3",
        "jsonrpc",
        "urllib",
        "http://",
        "https://",
        "odoo",
        "bottlecrm",
        "res.partner",
        "sale.order",
    ):
        assert forbidden not in text, forbidden
