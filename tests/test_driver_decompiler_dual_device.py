"""Phase B (WO-07) 自动化验收测试：DriverDecompilerAgent 与一标准接口双异构外设热插拔。

覆盖：
1. Device A（真实 BottleCRM 临时副本）与 Device B（29 表含 26 噪声表 + 整数主键 + ``phase_*/sev_*/st_*``
   专有枚举的异构 CRM）在锁定同一份 ``std_crm_ontology + CustomerOverlaySpec`` 下热插拔双外设驱动，
   零改动上层业务执行器跑通全部 4 项端到端物理 SQLite 同态核销场景；
2. CEGIS 自纠红测：
   - 第 1 轮 LLM 返回破坏性 Overlay（试图删除标准字段）→ 精确捕获 ``OVERLAY_BREAKS_STANDARD_CONTRACT``
     并回喂给下一轮自动修复过 5 关；
   - 第 1 轮 LLM 遗漏 ``enum_map``（暴露原始枚举字面量）及遗漏 ``resolve_crm_ticket`` 绑定 → 关 2
     精确捕获 ``INVALID_FSM_STATE_PROJECTION`` 与 ``MISSING_REQUIRED_ACTION_BINDING``，第 2 轮自纠全绿；
3. 验证源数据库 ``vendor_crm/bottlecrm.sqlite3`` SHA-256 零修改、``runtime.py`` 行数预算及 M2 纯洁性。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.dual_device_driver_swap_benchmark import (
    BOTTLECRM_SQLITE_PATH,
    GenericSQLiteDriverBridge,
    build_crm_decompiler_replay_factory,
    build_device_b_heterogeneous_sqlite,
    build_locked_crm_target_blueprint,
    build_offline_device_b_mapping,
    build_standard_crm_customer_overlay,
    file_sha256,
    run_dual_device_driver_swap_benchmark,
    scout_sqlite_device,
)
from examples.std_crm_ontology import build_std_crm_blueprint
from open_ontology import Actor, DeviceStateMismatchError, OntologyRuntime, SourceBridge
from open_ontology.induction import (
    CallableAbductionBackend,
    CrucibleContext,
    CustomerOverlaySpec,
    DriverDecompilerAgent,
    FieldSpec,
    InductionGovernanceService,
    ProposedEvalCase,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
    project_external_phenomenon,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Dual-Device Hot-Swap End-to-End (BottleCRM + Heterogeneous Device B)
# ---------------------------------------------------------------------------
def test_1_dual_device_hot_swap_identical_business_scenarios_and_physical_writeback() -> None:
    src_sha_before = file_sha256(BOTTLECRM_SQLITE_PATH)

    res = run_dual_device_driver_swap_benchmark(offline=True)

    assert res["bottlecrm_source_unmodified"] is True
    assert res["bottlecrm_source_sha256"] == src_sha_before == file_sha256(BOTTLECRM_SQLITE_PATH)
    assert res["locked_abi_model_id"] == "std_crm_ontology"
    assert res["locked_abi_version"] == "1.1.0"

    dev_a = res["device_a"]
    assert dev_a["scout_metrics"]["total_sqlite_tables"] >= 100
    assert dev_a["scout_metrics"]["core_tables"] == ["accounts", "case", "opportunity"]
    assert dev_a["scout_metrics"]["projected_m0_count"] == 12
    assert dev_a["decompilation"]["verified"] is True
    assert dev_a["decompilation"]["rounds_used"] == 1

    wa = dev_a["workload_scenarios"]
    assert wa["scenario_1_critical_ticket_blocks_win"] == {
        "blocked": True,
        "error_code": "OPEN_CRITICAL_TICKET_BLOCKS_WIN",
        "stage_unchanged": True,
    }
    assert wa["scenario_2_resolve_then_win"] == {
        "ticket_run_status": "COMMITTED",
        "ticket_status": "RESOLVED",
        "win_run_status": "COMMITTED",
        "deal_stage": "CLOSED_WON",
    }
    assert wa["scenario_3_high_value_loss_work_card"] == {
        "escalation_status": "AWAITING_WORK_CARD",
        "escalation_error_code": "APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
        "sod_blocked_code": "SOD_SELF_APPROVAL_FORBIDDEN",
        "settled_action_status": "COMMITTED",
        "work_card_status": "COMPLETED",
        "deal_stage": "CLOSED_LOST",
    }
    assert wa["scenario_4_medium_ticket_allows_win"] == {
        "win_run_status": "COMMITTED",
        "deal_stage": "CLOSED_WON",
    }
    assert dev_a["physical_sqlite_verification"] == {
        "blocked_deal_native_stage": "CLOSED_WON",
        "critical_ticket_native_status": "Resolved",
        "big_loss_deal_native_stage": "CLOSED_LOST",
        "medium_deal_native_stage": "CLOSED_WON",
        "all_runs_sync_terminal": True,
    }

    dev_b = res["device_b"]
    assert dev_b["scout_metrics"]["total_sqlite_tables"] == 29
    assert dev_b["scout_metrics"]["core_tables"] == ["corp_clients", "helpdesk_issues", "sales_pipeline"]
    assert dev_b["scout_metrics"]["projected_m0_count"] == 18
    assert dev_b["decompilation"]["verified"] is True
    assert dev_b["decompilation"]["rounds_used"] == 1

    wb = dev_b["workload_scenarios"]
    assert wb["scenario_1_critical_ticket_blocks_win"] == wa["scenario_1_critical_ticket_blocks_win"]
    assert wb["scenario_2_resolve_then_win"] == wa["scenario_2_resolve_then_win"]
    assert wb["scenario_3_high_value_loss_work_card"] == wa["scenario_3_high_value_loss_work_card"]
    assert wb["scenario_4_medium_ticket_allows_win"] == wa["scenario_4_medium_ticket_allows_win"]
    assert dev_b["physical_sqlite_verification"] == {
        "blocked_deal_native_stage": "phase_won",
        "critical_ticket_native_status": "st_resolved",
        "big_loss_deal_native_stage": "phase_lost",
        "medium_deal_native_stage": "phase_won",
        "all_runs_sync_terminal": True,
    }


# ---------------------------------------------------------------------------
# 2. CEGIS Self-Repair Red Tests (Destructive Overlay + Broken Enum Mapping)
# ---------------------------------------------------------------------------
def test_2a_decompiler_cegis_catches_destructive_overlay_and_repairs_in_round_2() -> None:
    std_base = build_std_crm_blueprint()
    valid_overlay = build_standard_crm_customer_overlay()
    mapping_b = build_offline_device_b_mapping()
    seen_feedbacks: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        dev_b_db = Path(tmpdir) / "dev_b.sqlite3"
        build_device_b_heterogeneous_sqlite(dev_b_db)
        scout = scout_sqlite_device(dev_b_db, source_kind="sqlite_dev_b")
        profile = scout["profile"]

        def _self_repair_backend(_sys_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
            payload = json.loads(user_prompt)
            seen_feedbacks.append(str(payload.get("previous_crucible_feedback") or ""))
            if round_index == 1:
                bad_overlay = CustomerOverlaySpec(
                    overlay_id="bad_destructive_overlay",
                    base_model_id="std_crm_ontology",
                    remove_fields={"CrmDeal": ["title"]},
                )
                return {
                    "rationale": "Round 1 accidentally attempts to delete a standard field",
                    "adapter_mapping": mapping_b.model_dump(mode="json"),
                    "customer_overlay": bad_overlay.model_dump(mode="json"),
                    "proposed_eval_cases": [],
                }
            return {
                "rationale": "Round 2 replaces destructive overlay with strictly monotonic overlay",
                "adapter_mapping": mapping_b.model_dump(mode="json"),
                "customer_overlay": valid_overlay.model_dump(mode="json"),
                "proposed_eval_cases": [
                    ProposedEvalCase(
                        name="RED-REPAIR-EVAL-WIN",
                        actor={"actor_id": "eval:sales", "role": "sales_rep"},
                        action="win_crm_deal",
                        params={"deal_id": "DEAL-EV-1"},
                        setup_objects=[
                            {"object_type": "BusinessParty", "data": {"party_id": "PTY-EV-1", "legal_name": "EvalCo"}},
                            {"object_type": "CrmAccount", "data": {"account_id": "ACC-EV-1", "name": "EvalAcc", "party": "PTY-EV-1"}},
                            {"object_type": "CrmDeal", "data": {"deal_id": "DEAL-EV-1", "title": "EvalDeal", "account": "ACC-EV-1", "amount": 5000.0, "stage": "NEGOTIATION"}},
                        ],
                        expect={"valid": True},
                    ).model_dump(mode="json")
                ],
            }

        with InductionGovernanceService(db_path=":memory:") as gov:
            agent = DriverDecompilerAgent(
                backend=CallableAbductionBackend(_self_repair_backend),
                governance=gov,
                crucible=SymbolicCrucible(),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            res = agent.run_decompilation_cycle(
                model_id="dev_b_cegis_overlay_repair",
                target_blueprint=std_base,
                external_profile=profile,
                overlay_requirement_doc="Add owned vip_tier to CrmAccount, block critical tickets on win, and require steward above 100000 on loss.",
                required_object_types=("BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket"),
                required_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
                crucible_context=CrucibleContext(
                    mode="genesis",
                    replay_factory=build_crm_decompiler_replay_factory(),
                    default_actor=Actor(actor_id="sales", role="sales_rep"),
                    seed_ontology=BUSINESS_SEED_ONTOLOGY,
                ),
                max_rounds=3,
            )

            assert res.verified is True
            assert len(res.rounds) == 2
            assert res.rounds[0].passed is False
            assert "gate_1_static_safety" in res.rounds[0].failed_gates
            assert "OVERLAY_BREAKS_STANDARD_CONTRACT" in res.rounds[0].feedback
            assert "OVERLAY_BREAKS_STANDARD_CONTRACT" in seen_feedbacks[1]
            assert res.rounds[1].passed is True
            assert res.customer_overlay is not None
            assert res.effective_blueprint is not None
            gov.approve_crystallization(res.pending_approval_run_id, STEWARD_ACTOR)
            active_bp = gov.get_active_blueprint("dev_b_cegis_overlay_repair")
            assert active_bp is not None
            deal_bp = next(ot for ot in active_bp.object_types if ot.name == "CrmDeal")
            assert any(f.name == "title" for f in deal_bp.fields)


def test_2b_decompiler_cegis_catches_missing_enum_map_and_action_binding_in_round_2() -> None:
    locked_bp = build_locked_crm_target_blueprint()
    good_mapping = build_offline_device_b_mapping()
    broken_mapping = build_offline_device_b_mapping()
    # Sabotage round 1: remove enum_map on CrmDeal.stage and omit resolve_crm_ticket binding
    deal_em = next(em for em in broken_mapping.entity_mappings if em.target_object_type == "CrmDeal")
    for tf in deal_em.field_transforms:
        if tf.target_field == "stage":
            tf.kind = "direct"
            tf.enum_map = {}
    broken_mapping.action_bindings = [
        b for b in broken_mapping.action_bindings if b.action_name != "resolve_crm_ticket"
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        dev_b_db = Path(tmpdir) / "dev_b_bad_map.sqlite3"
        build_device_b_heterogeneous_sqlite(dev_b_db)
        profile = scout_sqlite_device(dev_b_db, source_kind="sqlite_dev_b")["profile"]

        def _enum_repair_backend(_sys_prompt: str, user_prompt: str, round_index: int) -> dict[str, Any]:
            _ = user_prompt
            chosen = broken_mapping if round_index == 1 else good_mapping
            return {
                "rationale": f"round {round_index}",
                "adapter_mapping": chosen.model_dump(mode="json"),
                "customer_overlay": None,
                "proposed_eval_cases": [],
            }

        with InductionGovernanceService(db_path=":memory:") as gov:
            agent = DriverDecompilerAgent(
                backend=CallableAbductionBackend(_enum_repair_backend),
                governance=gov,
                crucible=SymbolicCrucible(),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            res = agent.run_decompilation_cycle(
                model_id="dev_b_cegis_enum_repair",
                target_blueprint=locked_bp,
                external_profile=profile,
                required_object_types=("BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket"),
                required_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
                crucible_context=CrucibleContext(
                    mode="genesis",
                    replay_factory=build_crm_decompiler_replay_factory(),
                    default_actor=Actor(actor_id="sales", role="sales_rep"),
                    seed_ontology=BUSINESS_SEED_ONTOLOGY,
                ),
                max_rounds=3,
            )

            assert res.verified is True
            assert len(res.rounds) == 2
            assert res.rounds[0].passed is False
            assert "gate_2_m0_grounding" in res.rounds[0].failed_gates
            assert "INVALID_FSM_STATE_PROJECTION" in res.rounds[0].feedback
            assert "MISSING_REQUIRED_ACTION_BINDING" in res.rounds[0].feedback
            assert res.rounds[1].passed is True


# ---------------------------------------------------------------------------
# 3. Zero-Trust Physical Readback Mismatch Guard on Integer-PK Device B
# ---------------------------------------------------------------------------
def test_3_zero_trust_readback_catches_tampered_device_b_state() -> None:
    locked_bp = build_locked_crm_target_blueprint()
    spec = compile_blueprint(locked_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    mapping_b = build_offline_device_b_mapping()

    with tempfile.TemporaryDirectory() as tmpdir:
        dev_b_db = Path(tmpdir) / "dev_b_tamper.sqlite3"
        build_device_b_heterogeneous_sqlite(dev_b_db)
        profile = scout_sqlite_device(dev_b_db, source_kind="sqlite_dev_b")["profile"]
        projected, errs = project_external_phenomenon(profile, mapping_b)
        assert not errs

        bridge = GenericSQLiteDriverBridge(
            dev_b_db,
            spec=spec,
            profile=profile,
            adapter_mapping=mapping_b,
        )

        # Simulate a lying/failing physical driver that ignores writeback on observe
        def _tampered_actuate(_mv: dict[str, Any]) -> None:
            return None

        tamper_bridge = SourceBridge(actuate=_tampered_actuate, observe=bridge.observe)
        rt = OntologyRuntime(
            spec,
            db_path=":memory:",
            world_mode="coupled",
            source_bridge=tamper_bridge,
        )
        try:
            for item in projected:
                cls = spec.get_object_type(item["object_type"])
                rt.put_object(cls(**item["data"]))

            sales = Actor(actor_id="sales-tamper", role="sales_rep")
            try:
                rt.submit_action("RUN-TAMPER", "win_crm_deal", {"deal_id": "DEAL-202"}, sales)
                raise AssertionError("expected DeviceStateMismatchError")
            except DeviceStateMismatchError:
                pass
            # Memory must NOT be polluted when physical SQLite did not advance
            assert rt.get_object("CrmDeal", "DEAL-202", sales).stage == "PROPOSAL"
        finally:
            rt.close()
            bridge.close()


# ---------------------------------------------------------------------------
# 4. Line Budget, Pure M2 Check & Source Untouched Guard
# ---------------------------------------------------------------------------
def test_4_decompiler_purity_runtime_budget_and_bottlecrm_untouched() -> None:
    runtime_lines = (REPO_ROOT / "open_ontology" / "runtime.py").read_text(encoding="utf-8").splitlines()
    assert len(runtime_lines) <= 1335

    dec_src = (REPO_ROOT / "open_ontology" / "induction" / "decompiler.py").read_text(encoding="utf-8")
    for forbidden in ("BottleCRM", "bottlecrm", "CrmAccount", "CrmDeal", "CrmTicket", "odoo", "erp_north"):
        assert forbidden not in dec_src, f"forbidden token {forbidden!r} leaked into decompiler.py"
