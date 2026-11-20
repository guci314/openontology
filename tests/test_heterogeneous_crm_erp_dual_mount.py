"""Acceptance tests for Step 2.2: Heterogeneous BottleCRM (SQLite) x Odoo 19 Dual-Mount."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.heterogeneous_crm_erp_dual_mount import (
    SOURCE_BOTTLECRM_DB,
    build_heterogeneous_dual_mount_blueprint,
    file_sha256,
    run_heterogeneous_dual_mount_demo,
)
from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError, _jsonrpc


def test_dual_mount_m1_blueprint_is_domain_pure() -> None:
    bp = build_heterogeneous_dual_mount_blueprint()
    dumped = json.dumps(bp.model_dump(mode="json")).lower()
    for forbidden in (
        "bottlecrm",
        "odoo",
        "sqlite",
        "sale.order",
        "res.partner",
        "accounts",
        "action_confirm",
        "ontologysuntime",
        "ontologyruntime",
    ):
        assert forbidden not in dumped, f"M1 blueprint leaked implementation term {forbidden!r}"


def test_heterogeneous_crm_erp_dual_mount_offline_preserves_source_sqlite() -> None:
    sha_before = file_sha256(SOURCE_BOTTLECRM_DB)
    out = run_heterogeneous_dual_mount_demo(offline=True)
    sha_after = file_sha256(SOURCE_BOTTLECRM_DB)

    assert sha_before == sha_after, "source vendor_crm/bottlecrm.sqlite3 MUST never be mutated"
    assert out["sqlite_writes"] == 1
    assert out["odoo_writes"] == 2
    assert out["scenario_1_crm_block"] == {
        "status": "REJECTED",
        "error_code": "OPEN_CRITICAL_CRM_CASE_BLOCKS_ERP_ORDER",
        "odoo_state": "draft",
    }
    assert out["scenario_2_crm_unlock"] == {
        "crm_status": "Closed",
        "order_status": "COMMITTED",
        "odoo_state": "sale",
    }
    assert out["scenario_3_credit_approval"] == {
        "blocked_status": "REJECTED",
        "error_code": "APPROVAL_REQUIRED_CUMULATIVE_CREDIT_EXCEEDED",
        "work_card_status": "COMPLETED",
        "approved_status": "COMMITTED",
        "odoo_state": "sale",
        "pending_approval_runs": 0,
    }
    assert out["scenario_4_drift_guard"] == {
        "error_code": "DEVICE_STATE_MISMATCH",
        "memory_uncorrupted": True,
        "odoo_state": "draft",
    }


def test_heterogeneous_crm_erp_dual_mount_live_odoo19_zero_leak() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    sha_before = file_sha256(SOURCE_BOTTLECRM_DB)
    dbs_before = set(_jsonrpc("db", "list", []))
    counts_before = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order")
    }

    out = run_heterogeneous_dual_mount_demo(offline=False, source_db=DB_NORTH)

    sha_after = file_sha256(SOURCE_BOTTLECRM_DB)
    dbs_after = set(_jsonrpc("db", "list", []))
    counts_after = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order")
    }

    assert sha_before == sha_after
    assert dbs_before == dbs_after
    assert out["sandbox_label"] not in dbs_after
    assert counts_before == counts_after

    assert out["mode"] == "live-bottlecrm-x-odoo19"
    assert out["scenario_1_crm_block"]["error_code"] == "OPEN_CRITICAL_CRM_CASE_BLOCKS_ERP_ORDER"
    assert out["scenario_2_crm_unlock"]["order_status"] == "COMMITTED"
    assert out["scenario_2_crm_unlock"]["odoo_state"] == "sale"
    assert out["scenario_3_credit_approval"]["blocked_status"] == "REJECTED"
    assert out["scenario_3_credit_approval"]["error_code"] == "APPROVAL_REQUIRED_CUMULATIVE_CREDIT_EXCEEDED"
    assert out["scenario_3_credit_approval"]["work_card_status"] == "COMPLETED"
    assert out["scenario_3_credit_approval"]["approved_status"] == "COMMITTED"
    assert out["scenario_3_credit_approval"]["odoo_state"] == "sale"
    assert out["scenario_3_credit_approval"]["pending_approval_runs"] == 0
    assert out["scenario_4_drift_guard"]["error_code"] == "DEVICE_STATE_MISMATCH"
    assert out["scenario_4_drift_guard"]["memory_uncorrupted"] is True
