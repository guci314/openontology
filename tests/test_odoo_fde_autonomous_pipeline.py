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
    assert set(cryst["object_types"]) == {"ClinicPatient", "ClinicVisit", "ClinicBill"}
    assert "clinic_visit__do_confirm_visit" in cryst["actions"]
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
    assert {"SaleOrder", "StockPicking", "AccountMove", "ResPartner"}.issubset(set(cryst["object_types"]))
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
