"""Verification test for Stage 2A: Zero-shot erp_north -> erp_south ontology transfer."""

from __future__ import annotations

from examples.cross_erp_transfer_benchmark import run_cross_erp_transfer_benchmark


def test_cross_erp_transfer_offline_zero_shot_parity() -> None:
    out = run_cross_erp_transfer_benchmark(offline=True)

    assert out["zero_shot_immutable"] is True
    assert out["induction_kernel_frozen"] is True
    assert out["frozen_ontology_sha256_before"] == out["frozen_ontology_sha256_after"]

    m0 = out["south_m0_grounding"]
    assert m0["total_projected_objects"] >= 4
    assert m0["grounding_errors"] == []

    sc_n = out["north"]
    sc_s = out["south"]
    assert sc_n["anti_smuggling"]["passed"] is True
    assert sc_s["anti_smuggling"]["passed"] is True
    assert sc_s["metrics"]["t2_dirty_external_writes"] == 0
    assert sc_s["metrics"]["t2_guardrail_f1"] == 1.0
    assert sc_s["metrics"]["t3_homomorphic_fidelity"] == 1.0
    assert sc_s["metrics"]["t1_task_sr"] == 1.0
    assert sc_s["metrics"]["composite_utility"] > 0.60

    for pid in ("T1-1", "T1-2", "T1-3", "T2-1", "T2-2", "T2-3", "T2-4", "T3-1", "T3-2", "T4-1", "T4-2"):
        assert out["probe_parity"][pid]["north_passed"] is True
        assert out["probe_parity"][pid]["south_passed"] is True
        assert out["probe_parity"][pid]["parity"] is True
