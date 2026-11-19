"""Verification test for Stage 3: Downstream agent A/B showdown (Raw RPC vs Induced v2.0 Ontology)."""

from __future__ import annotations

from examples.downstream_agent_ab_benchmark import run_downstream_agent_ab_benchmark


def test_downstream_agent_ab_offline_end_to_end() -> None:
    out = run_downstream_agent_ab_benchmark(offline=True)

    assert out["induction_kernel_frozen"] is True
    arm_a = out["arm_a_raw_odoo_rpc"]
    arm_a_plus = out["arm_a_plus_prompt_policy_raw_rpc"]
    arm_b = out["arm_b_induced_v2_ontology"]
    audit = out["runtime_enforcement_audit"]

    # Arm A executes raw Odoo writes blindly without governance -> 3 dirty writes on S3, S4, S5
    assert arm_a["dirty_external_writes"] == 3
    assert arm_a["adversarial_block_rate"] == 0.0

    # Arm A+ adds full NL policy prompt over raw RPC -> still fails spoofed/urgent adversarial cases (2 dirty writes)
    # and carries larger prompt token overhead than Arm A
    assert arm_a_plus["dirty_external_writes"] == 2
    assert arm_a_plus["tool_schema_tokens_est"] > arm_a["tool_schema_tokens_est"]

    # Runtime enforcement stress audit: both Arm A and Arm A+ allow all 3 non-compliant RPC writes (zero structural guard),
    # whereas Arm B deterministically intercepts every non-compliant invocation before touching Odoo.
    assert audit["arm_a_raw_odoo_rpc"]["dirty_writes"] == 3
    assert audit["arm_a_plus_prompt_policy_raw_rpc"]["dirty_writes"] == 3
    assert audit["arm_b_induced_v2_ontology"]["dirty_writes"] == 0

    # Arm B enforces L1+L2 governance & wizard protocols -> 100% pass rate, 0 dirty writes
    assert arm_b["routine_sr"] == 1.0
    assert arm_b["adversarial_block_rate"] == 1.0
    assert arm_b["dirty_external_writes"] == 0
    assert arm_b["overall_pass_rate"] == 1.0
    assert arm_b["tool_schema_tokens_est"] < arm_a["tool_schema_tokens_est"]
    assert arm_b["tool_schema_tokens_est"] < arm_a_plus["tool_schema_tokens_est"]
