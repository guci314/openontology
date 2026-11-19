"""Verification test for Stage 2B: Blind cross-domain BottleCRM SQLite induction."""

from __future__ import annotations

import inspect
from pathlib import Path

from examples.blind_crm_induction_benchmark import (
    CRM_NOISE_COLUMNS,
    build_blind_crm_genesis_requirement_doc,
    run_blind_crm_induction_benchmark,
    scout_bottlecrm_sqlite,
)


def test_blind_crm_scout_and_prompts_have_no_schema_or_dsl_leakage() -> None:
    scout_src = inspect.getsource(scout_bottlecrm_sqlite).lower()
    for forbidden_tbl in ("accounts", "opportunity", "opportunity_pipeline_stage", '"case"'):
        assert forbidden_tbl not in scout_src, f"scout leaked table literal {forbidden_tbl!r}"
    for forbidden_col in ("case_type", "industry", "priority"):
        assert forbidden_col not in scout_src, f"scout leaked column name {forbidden_col!r}"

    req_doc = build_blind_crm_genesis_requirement_doc()
    req_low = req_doc.lower()
    for leaked in (
        *CRM_NOISE_COLUMNS,
        "accounts",
        "opportunity",
        "opportunity_pipeline_stage",
        "acct-",
        "opp-",
        "case-",
        "win_opportunity",
        "close_lost_opportunity",
        "resolve_support_case",
    ):
        assert leaked not in req_low, f"req_doc leaked identifier {leaked!r}"

    bench_src = Path(inspect.getsourcefile(run_blind_crm_induction_benchmark) or "").read_text(encoding="utf-8")
    act2_segment = bench_src.split("cluster = FrictionClusterSummary(", 1)[1].split("evolver.run_evolution_cycle", 1)[0]
    for dsl_hint in ("actor.role", "precondition", "['High','Urgent']", "['Resolved','Closed']", "['steward', 'admin']"):
        assert dsl_hint not in act2_segment, f"Act 2 FrictionEvidence leaked DSL syntax {dsl_hint!r}"


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
