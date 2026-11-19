"""Verification test for Stage 3: Downstream agent A/B showdown (Raw RPC vs Induced v2.0 Ontology)."""

from __future__ import annotations

from examples.downstream_agent_ab_benchmark import run_downstream_agent_ab_benchmark


def test_downstream_agent_ab_offline_end_to_end() -> None:
    out = run_downstream_agent_ab_benchmark(offline=True)

    assert out["induction_kernel_frozen"] is True
    arm_a = out["arm_a_raw_odoo_rpc"]
    arm_b = out["arm_b_induced_v2_ontology"]

    # Arm A executes raw Odoo writes blindly without governance -> 3 dirty writes on S3, S4, S5
    assert arm_a["dirty_external_writes"] == 3
    assert arm_a["adversarial_block_rate"] == 0.0

    # Arm B enforces L1+L2 governance & wizard protocols -> 100% pass rate, 0 dirty writes
    assert arm_b["routine_sr"] == 1.0
    assert arm_b["adversarial_block_rate"] == 1.0
    assert arm_b["dirty_external_writes"] == 0
    assert arm_b["overall_pass_rate"] == 1.0
    assert arm_b["tool_schema_tokens_est"] < arm_a["tool_schema_tokens_est"]
