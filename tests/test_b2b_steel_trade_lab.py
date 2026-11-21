"""Offline deterministic mirror tests for WO-18 Dual-Enterprise B2B Steel Trade Lab."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.commonsense_exchange import CommonsenseEnvelope, PROTOCOL_ID, validate_commonsense_envelope
from examples.live_b2b_steel_trade_lab import (
    SOURCE_BOTTLECRM_DB,
    _file_sha256,
    build_buyer_enterprise_spec,
    build_seller_enterprise_spec,
    run_offline_b2b_steel_trade_lab,
)


def test_offline_full_stack_b2b_steel_trade_lab_four_scenarios(tmp_path: Path) -> None:
    sha_before = _file_sha256(SOURCE_BOTTLECRM_DB)
    report = run_offline_b2b_steel_trade_lab(tmp_path)

    assert report["ok"] is True
    assert report["mode"] == "offline_mirror"
    assert set(report["envelopes_validated"]) >= {
        "COMMITMENT_PROPOSED",
        "SHIPMENT_EVENT",
        "RECEIPT_EVENT",
        "INVOICE_EVENT",
        "PAYMENT_EVENT",
        "DISPUTE_RAISED",
        "DISPUTE_SETTLED",
        "COMMITMENT_HELD",
    }

    # Scenario 1: SHADOW (0 writes) -> AUTO_UNDER_CEILING -> 100t / 360,000 settled
    s1 = report["scenario_1"]
    assert s1["passed"] is True
    assert "SHADOW_RECORDED" in s1["shadow_ledger_outcomes"]
    assert "AUTO_COMMITTED" in s1["shadow_ledger_outcomes"]
    assert s1["buyer_shadow_trace"]["mcp_calls_count"] >= 3
    assert s1["buyer_confirm_trace"]["mcp_calls_count"] >= 2
    assert s1["seller_confirm_trace"]["mcp_calls_count"] >= 4
    assert s1["buyer_bill_state"] == "paid"
    assert s1["seller_invoice_state"] == "paid"
    assert s1["settled_amount_cny"] == pytest.approx(360_000.0)

    # Scenario 2: 100t shipped vs 96t received short-weight dispute & WorkCard settlement
    s2 = report["scenario_2"]
    assert s2["passed"] is True
    assert s2["blocked_disputed_code"] == "PO_DISPUTED_BLOCKED"
    assert s2["blocked_over_amount_code"] == "BILL_EXCEEDS_PO_AMOUNT"
    assert s2["settled_amount_cny"] == pytest.approx(345_600.0)
    assert s2["seller_invoice_total"] == pytest.approx(345_600.0)
    assert s2["seller_payment_state"] == "paid"

    # Scenario 3: BottleCRM URGENT ticket blocks seller Odoo confirmation with 0 writes & 0 leak
    s3 = report["scenario_3"]
    assert s3["passed"] is True
    assert s3["odoo_order_state"] == "draft"
    assert s3["erp_b_writes_delta_during_hold"] == 0
    assert s3["seller_open_work_cards"]
    assert s3["buyer_commitment_status"] == "disputed"
    assert s3["wire_crm_leak_count"] == 0

    # Scenario 4: OOB tamper + Saga post-timeout + MDMSidecarPlugin cross-ERP L1/L4/L0
    s4 = report["scenario_4"]
    assert s4["passed"] is True
    assert s4["oob_caught_code"] == "DEVICE_STATE_MISMATCH"
    assert s4["oob_run_status"] == "REJECTED"
    assert s4["saga_outcome"] == "RECONCILED_REMOTE_COMMITTED"
    assert s4["saga_actuate_calls"] == 1
    assert s4["saga_observe_calls"] == 1
    assert s4["mdm_l1_auto_merged_cid"].startswith("BP-")
    assert len(s4["mdm_l4_dispatched_runs"]) == 1
    assert s4["mdm_l4_repeat_dispatched"] == []
    assert s4["mdm_l0_confirmed_cid"].startswith("BP-")

    assert _file_sha256(SOURCE_BOTTLECRM_DB) == sha_before


def test_enterprise_ontology_isolation_and_wire_guardrails() -> None:
    buyer_spec = build_buyer_enterprise_spec()
    seller_spec = build_seller_enterprise_spec()

    assert not any(t.startswith(("O2c", "Crm")) for t in buyer_spec.object_types)
    assert not any(t.startswith("P2p") for t in seller_spec.object_types)
    assert "CrmTicket" in seller_spec.object_types
    assert "confirm_o2c_order" in seller_spec.actions

    # Tampered envelope carrying internal WorkCard targeting CrmTicket must fail validation
    bad_card_env = {
        "protocol": PROTOCOL_ID,
        "envelope_id": "ENV-BAD-1",
        "sender_party_id": "PTY-SELLER",
        "receiver_party_id": "PTY-BUYER",
        "correlation_id": "CM-1",
        "message_kind": "COMMITMENT_HELD",
        "parties": [],
        "resources": [],
        "commitments": [],
        "events": [],
        "work_cards": [
            {
                "card_id": "WC-LEAK",
                "target_object_type": "CrmTicket",
                "target_object_id": "TK-1",
            }
        ],
        "summary": "leaked crm ticket",
    }
    with pytest.raises(ValueError, match="target_object_type"):
        validate_commonsense_envelope(bad_card_env)

    # Extra unknown top-level keys must fail wire validation
    bad_extra = json.loads(
        CommonsenseEnvelope(
            sender_party_id="A",
            receiver_party_id="B",
            correlation_id="CM-1",
            message_kind="PING",
        ).to_json()
    )
    bad_extra["crm_internal_secret"] = {"ticket": "TK-99"}
    with pytest.raises(ValueError, match="forbidden envelope keys"):
        validate_commonsense_envelope(bad_extra)
