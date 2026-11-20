"""验收测试：第一步 + 第二步合龙（无人 FDE 自主侦察 → 零剧本自动合成 → 五关证伪结晶 → 双向同态回读核销）。

覆盖：
1. 源码零硬编码守卫：``examples/odoo_fde_autonomous_pipeline.py`` 零业务表名、零字段名、零方法名、零阈值；
2. 离线跨领域全链路（合成诊所 ERP）：证明 Probe 1+2+3 → 零剧本自动合成 → 五关熔炉 → Steward 结晶
   → ``DeclarativeOdooBridge`` 双向驱动 + 闭环同态核销（``COMMITTED``）
   与外部漂移保护（``DEVICE_STATE_MISMATCH``）全自动跑通；
3. 本机真实 Odoo 19 (``erp_north``) 全链路验收：验证生产库零写入、临时克隆库退出自动销毁、
   五关全绿结晶且真实双向调用克隆 Odoo 核销 ``COMMITTED`` + 漂移拦截 ``DEVICE_STATE_MISMATCH``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError, _jsonrpc
from examples.odoo_fde_autonomous_pipeline import run_autonomous_fde_end_to_end
from examples.odoo_fde_prober import EphemeralOdooSandbox
from examples.odoo_fde_scout import ReadOnlyOdooClient
from tests.test_odoo_fde_prober import _SyntheticProbeSandbox
from tests.test_odoo_fde_scout import _SyntheticOdooMock


def test_autonomous_pipeline_source_has_zero_hardcoded_business_identifiers() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "examples" / "odoo_fde_autonomous_pipeline.py"
    ).read_text(encoding="utf-8").lower()
    for forbidden in (
        "sale.order",
        "stock.picking",
        "account.move",
        "res.partner",
        "action_confirm",
        "action_post",
        "button_validate",
        "credit_limit",
        "100000",
    ):
        assert forbidden not in text, f"统一自主管道不得硬编码业务词条 {forbidden!r}"


def test_autonomous_pipeline_end_to_end_offline_synthetic_clinic_erp() -> None:
    base_ro = _SyntheticOdooMock()
    # 添加额外 draft 诊单，确保 Probe 3 消耗 2 张后，Step 2 仍有 draft 诊单做正向同态核销 + 漂移检测
    base_ro.tables["clinic.visit"].extend(
        [
            {"id": 14, "name": "VST-005", "patient_id": [2, "Bob"], "state": "draft", "total_amount": 150.0},
            {"id": 15, "name": "VST-006", "patient_id": [1, "Alice"], "state": "draft", "total_amount": 180.0},
            {"id": 16, "name": "VST-007", "patient_id": [2, "Bob"], "state": "draft", "total_amount": 210.0},
        ]
    )
    sandbox = _SyntheticProbeSandbox(base_ro)

    out = run_autonomous_fde_end_to_end(
        ro_client=base_ro,
        sandbox_client=sandbox,
        source_locator="mock://clinic",
        sandbox_db="mock_clone_clinic",
        model_id="autonomous_clinic_ontology",
        task_intent="",
        max_models=5,
        use_live_llm=False,
    )

    cryst = out["crystallized_ontology"]
    assert cryst["crucible_gates_passed"] is True
    assert set(cryst["object_types"]) == {"Patient", "ClinicVisit", "PatientBill", "InternalAdj"}
    assert "confirm_visit" in cryst["actions"]
    assert all("__" not in a for a in cryst["actions"])
    assert cryst["gate_3_metrics"]["positive_rate"] == 1.0
    assert cryst["gate_3_metrics"]["negative_rate"] == 1.0

    # Step 2: 双向执行 + 闭环同态核销
    homo = out["homomorphic_settlement_runs"]
    assert len(homo) >= 1
    assert all(r["settled_status"] == "COMMITTED" and r["homomorphic_match"] for r in homo)

    # 外部漂移时精准触发 DEVICE_STATE_MISMATCH
    drift = out["drift_protection_run"]
    assert drift is not None
    assert drift["mismatch_caught"] is True
    assert drift["error_code"] == "DEVICE_STATE_MISMATCH"


def test_autonomous_synthesis_promotes_joins_prunes_ui_noise_and_compiles_approval_rule() -> None:
    import json

    from open_ontology import InvariantError, OntologyRuntime
    from open_ontology.induction import compile_blueprint, project_external_phenomenon
    from examples.business_ontology import (
        WorkCard,
        build_business_seed_ontology,
        register_work_card_primitives,
        settle_work_card,
    )
    from examples.odoo_fde_autonomous_pipeline import (
        AI_OPS_ACTOR,
        STEWARD_OPS_ACTOR,
        build_auto_replay_factory,
        build_closed_phenomenon_profile,
        synthesize_from_fde_evidence,
    )
    from examples.odoo_fde_prober import OdooActiveProber
    from examples.odoo_fde_scout import OdooFDEScout

    base_ro = _SyntheticOdooMock()
    base_ro.tables["ir.model.fields"].append(
        {
            "id": 199,
            "model": "clinic.patient",
            "name": "can_image_1024_be_zoomed",
            "field_description": "Can Image 1024 Be Zoomed",
            "ttype": "boolean",
            "relation": False,
            "store": True,
            "required": False,
            "readonly": False,
        }
    )
    base_ro.tables["clinic.patient"][0]["can_image_1024_be_zoomed"] = True
    base_ro.tables["clinic.patient"][1]["can_image_1024_be_zoomed"] = False
    base_ro.tables["clinic.visit"].append(
        {"id": 14, "name": "VST-005", "patient_id": [2, "Bob"], "state": "draft", "total_amount": 150.0}
    )

    sandbox = _SyntheticProbeSandbox(base_ro)
    scout_report = OdooFDEScout(base_ro, source_locator="mock://clinic_unit", max_models=5).scout()
    profile = build_closed_phenomenon_profile(scout_report, client=base_ro)
    probe_report = OdooActiveProber(sandbox, sandbox_db="mock_clone_unit").probe(scout_report)

    seed = build_business_seed_ontology()
    bundle = synthesize_from_fde_evidence(
        scout_report,
        probe_report,
        profile=profile,
        seed_ontology=seed,
        model_id="clinic_approval_test",
        apply_gray_zone_approval_rules=True,
    )
    ot_map = {ot.name: ot for ot in bundle.blueprint.object_types}
    assert "can_image_1024_be_zoomed" not in {f.name for f in ot_map["Patient"].fields}
    pb_ot = ot_map["PatientBill"]
    assert any(r.field == "clinic_visit" and r.target_type == "ClinicVisit" for r in pb_ot.relations)
    assert "visit_origin" not in {f.name for f in pb_ot.fields}

    compiled = compile_blueprint(bundle.blueprint, seed_ontology=seed)
    register_work_card_primitives(compiled)
    projected, proj_errors = project_external_phenomenon(profile, bundle.adapter_mapping)
    assert proj_errors == []

    # Verify build_auto_replay_factory emits AI approval-gate denial + Steward approval replay events
    pos_events, neg_events = build_auto_replay_factory(bundle, seed_ontology=seed)(projected)
    assert any(ev.expected_error_code == "APPROVAL_REQUIRED_COVERAGE_LIMIT_EXCEEDED" for ev in neg_events)
    assert any(ev.actor.role == "steward" and ev.action == "confirm_visit" for ev in pos_events)

    rt = OntologyRuntime(compiled, db_path=":memory:")
    try:
        order_rank = {name: i for i, name in enumerate(compiled.object_types)}
        for entry in sorted(projected, key=lambda e: order_rank.get(e["object_type"], 999)):
            cls = compiled.get_object_type(entry["object_type"])
            rt.put_object(cls(**entry["data"]))

        visit_cls = compiled.get_object_type("ClinicVisit")
        over_limit = next(v for v in rt.query_objects(visit_cls, AI_OPS_ACTOR) if v.state == "draft" and float(v.total_amount) > 500.0)
        within_limit = next(v for v in rt.query_objects(visit_cls, AI_OPS_ACTOR) if v.state == "draft" and float(v.total_amount) <= 500.0)

        with pytest.raises(InvariantError) as exc_over:
            rt.submit_action("SYNTH-OVER", "confirm_visit", {"record_id": over_limit.record_id}, AI_OPS_ACTOR)
        assert exc_over.value.code == "APPROVAL_REQUIRED_COVERAGE_LIMIT_EXCEEDED"
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []

        rt.submit_action(
            "SYNTH-WC-OPEN",
            "open_work_card",
            {
                "card_id": "WC-SYNTH-OVR",
                "card_kind": "APPROVAL",
                "target_object_type": "ClinicVisit",
                "target_object_id": over_limit.record_id,
                "requested_action": "confirm_visit",
                "requested_params_json": json.dumps({"record_id": over_limit.record_id}),
                "assignee_role": "steward",
            },
            AI_OPS_ACTOR,
        )
        settled = settle_work_card(rt, "WC-SYNTH-OVR", STEWARD_OPS_ACTOR, resolution_note="approved over limit")
        assert settled["status"] == "COMMITTED"
        assert settled["settled_action_run"]["status"] == "COMMITTED"
        assert rt.get_object(WorkCard, "WC-SYNTH-OVR", STEWARD_OPS_ACTOR).status == "COMPLETED"
        assert rt.list_action_runs(status="PENDING_APPROVAL") == []

        res_ok = rt.submit_action("SYNTH-OK", "confirm_visit", {"record_id": within_limit.record_id}, AI_OPS_ACTOR)
        assert res_ok["status"] == "COMMITTED"
    finally:
        rt.close()


def test_autonomous_pipeline_live_odoo19_step1_and_step2_zero_leak() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    dbs_before = set(_jsonrpc("db", "list", []))
    counts_before = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    ro_client = ReadOnlyOdooClient(db=DB_NORTH, client=raw)
    with EphemeralOdooSandbox(source_db=DB_NORTH) as sandbox_client:
        clone_name = sandbox_client.db
        out = run_autonomous_fde_end_to_end(
            ro_client=ro_client,
            sandbox_client=sandbox_client,
            source_locator=f"odoo://{DB_NORTH}",
            sandbox_db=clone_name,
            model_id="autonomous_erp_ontology",
            task_intent="",
            use_live_llm=False,
        )

    dbs_after = set(_jsonrpc("db", "list", []))
    counts_after = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    # 源生产库零写入 + 临时克隆库彻底销毁
    assert clone_name not in dbs_after
    assert dbs_before == dbs_after
    assert counts_before == counts_after

    cryst = out["crystallized_ontology"]
    assert cryst["crucible_gates_passed"] is True
    assert {"Contact", "SalesOrder", "Transfer", "CustomerInvoice", "JournalEntry"}.issubset(set(cryst["object_types"]))
    assert {"ResPartner", "SaleOrder", "StockPicking", "AccountMove"}.isdisjoint(set(cryst["object_types"]))
    assert all("__" not in a for a in cryst["actions"])
    assert cryst["gate_3_metrics"]["positive_total"] >= 3
    assert cryst["gate_3_metrics"]["negative_total"] >= 2
    assert cryst["gate_3_metrics"]["positive_rate"] == 1.0
    assert cryst["gate_3_metrics"]["negative_rate"] == 1.0

    # Step 2: 至少两条真实双向调用经闭环同态核销 COMMITTED
    homo = out["homomorphic_settlement_runs"]
    assert len(homo) >= 2
    assert all(r["settled_status"] == "COMMITTED" and r["homomorphic_match"] for r in homo)

    # 外部漂移触发 DEVICE_STATE_MISMATCH
    drift = out["drift_protection_run"]
    assert drift is not None and drift["mismatch_caught"] is True
