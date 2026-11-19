"""Verification test for Stage 2B: Blind cross-domain BottleCRM SQLite induction."""

from __future__ import annotations

from examples.blind_crm_induction_benchmark import run_blind_crm_induction_benchmark


def test_blind_crm_induction_offline_bottlecrm_end_to_end() -> None:
    out = run_blind_crm_induction_benchmark(offline=True)

    assert out["source_sqlite_unmodified"] is True
    assert out["induction_kernel_frozen"] is True
    assert out["raw_crm_schema"]["total_sqlite_tables"] >= 100
    assert out["raw_crm_schema"]["real_m0_rows_grounded"] == 9

    g = out["act1_genesis"]
    assert g["verified"] is True
    assert g["noise_columns_retained"] == []
    assert g["purified_field_count"] <= 20

    e = out["act2_l1_evolution"]
    assert e["verified"] is True

    s = out["act3_sqlite_homomorphic_settlement"]
    assert s["acme_blocked_while_high_case_open"]["passed"] is True
    assert s["southstar_260k_loss_blocked_for_ai"]["passed"] is True
    assert s["northwind_medium_case_allows_win"]["passed"] is True
    assert s["acme_resolve_case_then_win"]["passed"] is True
