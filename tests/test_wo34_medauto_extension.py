"""Tests for WO-34 MedAuto Export Manufacturing Secondary-Development Pack on Pure L1 Console."""

from __future__ import annotations

import json

import pytest

from open_ontology import Actor, InvariantError, OntologyRuntime
from open_ontology.console_server import start_console_server
from open_ontology.induction import prove_blueprint

from examples.enterprise_console_app import build_console_service
from examples.medauto_export_extension import (
    MEDAUTO_DOAL_OVERLAY,
    build_medauto_console,
    build_medauto_spec,
    seed_medauto_world,
)
from examples.std_enterprise_world_model import build_enterprise_standard_spec


def _fresh_medauto_runtime() -> tuple[OntologyRuntime, Actor, Actor, Actor, Actor]:
    spec = build_medauto_spec()
    rt = OntologyRuntime(spec, db_path=":memory:", world_mode="standalone")
    admin = Actor(actor_id="act-admin", role="admin", clearance_tags={"*"})
    seed_medauto_world(rt, admin)
    cfo = Actor(actor_id="act-cfo", role="finance_director", clearance_tags={"*"})
    pm = Actor(actor_id="act-pm", role="plant_manager", clearance_tags={"*"})
    ops = Actor(actor_id="act-ops", role="ops", clearance_tags={"*"})
    return rt, admin, cfo, pm, ops


def test_medauto_extension_architecture_counts_and_liskov_inheritance() -> None:
    base = build_enterprise_standard_spec()
    base_rules = [k for k, v in base.actions.items() if v.implementation_kind == "rule"]
    base_efs = [k for k, v in base.actions.items() if v.implementation_kind == "edit_function"]
    assert len(base.actions) == 31
    assert len(base_rules) == 22
    assert len(base_efs) == 9

    med = build_medauto_spec()
    med_rules = [k for k, v in med.actions.items() if v.implementation_kind == "rule"]
    med_efs = [k for k, v in med.actions.items() if v.implementation_kind == "edit_function"]
    assert len(med.actions) == 43
    assert len(med_rules) == 29
    assert len(med_efs) == 14
    assert len(med_rules) - len(base_rules) == 7
    assert len(med_efs) - len(base_efs) == 5

    # 8 structural / DL / invariant DOAL items + 7 DOAL Rule actions = 15 DOAL; + 5 EditFunctions = 20 items (75% DOAL)
    assert len(MEDAUTO_DOAL_OVERLAY.derive_object_types) == 3
    assert len(MEDAUTO_DOAL_OVERLAY.add_object_types) == 4
    assert len(MEDAUTO_DOAL_OVERLAY.add_disjoint_types) == 1
    assert len(MEDAUTO_DOAL_OVERLAY.add_actions) == 7

    # Liskov inheritance hierarchy preserved on derived spec types
    assert issubclass(med.object_types["ExportSalesOrder"], med.object_types["O2cOrder"])
    assert issubclass(med.object_types["DomesticSampleOrder"], med.object_types["O2cOrder"])
    assert issubclass(med.object_types["ReworkWorkOrder"], med.object_types["WorkOrder"])
    assert "x_lot_code" in med.object_types["ReworkWorkOrder"].model_fields
    assert "x_quarantined" in med.object_types["ReworkWorkOrder"].model_fields

    # Mode switcher wires --mode medauto onto Pure L1 shell (pack_hint="none")
    svc = build_console_service(mode="medauto", enable_llm_assistant=False)
    try:
        assert svc.pack_hint == "none"
        assert len(svc.sandbox_presets) == 4
    finally:
        svc.close()
        svc.runtime.close()


def test_medauto_z3_formal_proof_clean() -> None:
    pytest.importorskip("z3")
    report = prove_blueprint(build_medauto_spec(), check_redundancy=True)

    assert report.ok is True
    assert report.dead_class_count == 0
    assert report.dl_conflict_count == 0
    assert report.lsp_violation_count == 0
    assert report.total_deadlock_count == 0
    assert report.conditional_deadlock_count == 0
    assert report.invariant_violation_count == 0
    assert report.redundant_rule_count == 0

    assert len(report.reachability) == 43
    rule_reach = [item for item in report.reachability if item.implementation_kind == "rule"]
    assert len(rule_reach) == 29
    assert all(item.status == "REACHABLE" for item in report.reachability)
    assert len(report.invariant_preservation) == 29
    assert all(item.status == "PROVED" for item in report.invariant_preservation)


def test_medauto_doal_rule_actions_and_invariants_runtime() -> None:
    # 1. FAI branches + delivery_requires_passed_fai precondition + expired cert block
    rt, admin, cfo, pm, ops = _fresh_medauto_runtime()
    try:
        # Expired cert on CUST-JP-02 blocks confirm_o2c_order
        with pytest.raises(InvariantError) as exc_cert:
            rt.submit_action("T-CERT-BLK", "confirm_o2c_order", {"order_id": "SO-JP-BLOCKED-01"}, admin)
        assert exc_cert.value.code == "CUSTOMER_REGULATORY_CERT_EXPIRED"

        # Delivery DLV-MED-001 blocked while QI-001 is still pending
        with pytest.raises(InvariantError) as exc_dlv:
            rt.submit_action("T-DLV-BLK", "complete_o2c_delivery", {"delivery_id": "DLV-MED-001"}, admin)
        assert exc_dlv.value.code == "DELIVERY_MISSING_PASSED_FAI"

        # Negative defect_count blocked; defect_count == 0 passes QI-001 and unlocks delivery
        with pytest.raises(InvariantError) as exc_neg:
            rt.submit_action(
                "T-QI-NEG",
                "submit_first_article_inspection",
                {"inspection_id": "QI-001", "defect_count": -1},
                ops,
            )
        assert exc_neg.value.code == "NEGATIVE_DEFECT_COUNT"

        rt.submit_action(
            "T-QI-PASS",
            "submit_first_article_inspection",
            {"inspection_id": "QI-001", "defect_count": 0, "inspector_note": "PASS"},
            ops,
        )
        assert rt.get_object("QualityInspection", "QI-001", admin).status == "passed"
        rt.submit_action("T-DLV-OK", "complete_o2c_delivery", {"delivery_id": "DLV-MED-001"}, admin)
        assert rt.get_object("O2cDelivery", "DLV-MED-001", admin).status == "completed"

        # 2. High-value ExportSalesOrder requires verified L/C before confirming (polymorphic confirm_o2c_order)
        with pytest.raises(InvariantError) as exc_lc:
            rt.submit_action("T-EXP-NOLC", "confirm_o2c_order", {"order_id": "EXP-SO-001"}, admin)
        assert exc_lc.value.code == "EXPORT_HIGH_VALUE_REQUIRES_LC"

        rt.submit_action("T-EXP-LC", "verify_export_letter_of_credit", {"order_id": "EXP-SO-001"}, cfo)
        assert rt.get_object("ExportSalesOrder", "EXP-SO-001", admin).x_lc_verified is True
        rt.submit_action("T-EXP-CONF", "confirm_o2c_order", {"order_id": "EXP-SO-001"}, admin)
        assert rt.get_object("ExportSalesOrder", "EXP-SO-001", admin).status == "confirmed"

        # Discount cap invariant (> 12%) blocked
        ExpCls = rt.spec.get_object_type("ExportSalesOrder")
        rt.put_object(
            ExpCls(
                order_id="EXP-BAD-DISC",
                order_number="EXP-BAD-DISC",
                customer="CUST-DE-01",
                accounting_entity="ACC-ENT-MEDAUTO",
                amount_total=50_000.0,
                x_discount_pct=15,
            ),
            actor=admin,
        )
        with pytest.raises(InvariantError) as exc_disc_inv:
            rt.submit_action("T-DISC-CAP2", "confirm_o2c_order", {"order_id": "EXP-BAD-DISC"}, admin)
        assert exc_disc_inv.value.code == "EXPORT_DISCOUNT_CAP_EXCEEDED"
        # Clean up invalid discount row so subsequent global invariant scans stay valid
        rt.put_object(
            ExpCls(
                order_id="EXP-BAD-DISC",
                order_number="EXP-BAD-DISC",
                customer="CUST-DE-01",
                accounting_entity="ACC-ENT-MEDAUTO",
                amount_total=50_000.0,
                x_discount_pct=5,
            ),
            actor=admin,
        )

        # 3. declare_customs_clearance updates CustomsDeclaration and ExportSalesOrder.x_customs_no via target_ref
        rt.submit_action(
            "T-CUST-CLR",
            "declare_customs_clearance",
            {"declaration_id": "CD-001", "customs_fee_cents": 85_000},
            ops,
        )
        assert rt.get_object("CustomsDeclaration", "CD-001", admin).status == "cleared"
        assert rt.get_object("ExportSalesOrder", "EXP-SO-002", admin).x_customs_no == "CUS-2026-8801"

        # 4. flag_customer_credit_hold triggers when unpaid posted invoice exists, blocks otherwise
        rt.submit_action("T-HOLD-DE", "flag_customer_credit_hold", {"account_id": "ACC-DE-01"}, cfo)
        assert rt.get_object("CrmAccount", "ACC-DE-01", admin).x_credit_status == "HOLD"
        with pytest.raises(InvariantError) as exc_hold_cn:
            rt.submit_action("T-HOLD-CN", "flag_customer_credit_hold", {"account_id": "ACC-CN-03"}, cfo)
        assert exc_hold_cn.value.code == "NO_UNPAID_INVOICE"

        # 5. classify_export_risk_tier (HIGH > 500k, MEDIUM 200k..500k, LOW < 200k)
        rt.put_object(
            ExpCls(
                order_id="EXP-LOW-100",
                order_number="EXP-LOW-100",
                customer="CUST-DE-01",
                accounting_entity="ACC-ENT-MEDAUTO",
                amount_total=120_000.0,
                x_risk_tier="HIGH",
            ),
            actor=admin,
        )
        rt.submit_action("T-RISK-HI", "classify_export_risk_tier", {"order_id": "EXP-SO-001"}, ops)
        rt.submit_action("T-RISK-MD", "classify_export_risk_tier", {"order_id": "EXP-SO-002"}, ops)
        rt.submit_action("T-RISK-LO", "classify_export_risk_tier", {"order_id": "EXP-LOW-100"}, ops)
        assert rt.get_object("ExportSalesOrder", "EXP-SO-001", admin).x_risk_tier == "HIGH"
        assert rt.get_object("ExportSalesOrder", "EXP-SO-002", admin).x_risk_tier == "MEDIUM"
        assert rt.get_object("ExportSalesOrder", "EXP-LOW-100", admin).x_risk_tier == "LOW"

        # 6. open_rework_work_order copies source_obj.work_order.*, enforces planned_qty cap, blocks duplicate
        with pytest.raises(InvariantError) as exc_rw_cap:
            rt.submit_action(
                "T-RW-CAP",
                "open_rework_work_order",
                {
                    "inspection_id": "QI-003",
                    "rework_work_order_id": "RWO-TOO-BIG",
                    "rework_qty": 99,
                    "defect_reason": "oversized",
                },
                pm,
            )
        assert exc_rw_cap.value.code == "REWORK_EXCEEDS_PLANNED_QTY"

        rt.submit_action(
            "T-RW-OK",
            "open_rework_work_order",
            {
                "inspection_id": "QI-003",
                "rework_work_order_id": "RWO-MED-001",
                "rework_qty": 10,
                "defect_reason": "内腔微毛刺",
            },
            pm,
        )
        rwo = rt.get_object("ReworkWorkOrder", "RWO-MED-001", admin)
        assert rwo is not None
        assert rwo.parent_work_order == "WO-MED-001"
        assert rwo.bom == "BOM-IMPLANT-L1"
        assert rwo.output_resource == "RES-TI-IMPLANT"
        assert rwo.planned_qty == 50.0
        assert rwo.x_rework_qty == 10
        assert rwo.x_lot_code == "LOT-2026-09"
        assert rt.get_object("QualityInspection", "QI-003", admin).rework_opened is True
        assert rt.get_object("QualityInspection", "QI-003", admin).status == "failed"
        with pytest.raises(InvariantError) as exc_rw_dup:
            rt.submit_action(
                "T-RW-DUP",
                "open_rework_work_order",
                {
                    "inspection_id": "QI-003",
                    "rework_work_order_id": "RWO-DUP",
                    "rework_qty": 5,
                    "defect_reason": "dup",
                },
                pm,
            )
        assert exc_rw_dup.value.code == "REWORK_ALREADY_OPENED"
        # Verify RWO-MED-001 inherits WorkOrder at runtime with owned=True sovereignty preserved
        WoCls = rt.spec.get_object_type("WorkOrder")
        assert isinstance(rwo, WoCls)
        assert rwo.status == "draft"
        # Standard source-backed WorkOrder continues using release_work_order cleanly
        rt.submit_action("T-WO3-REL", "release_work_order", {"work_order_id": "WO-MED-003"}, admin)
        assert rt.get_object("WorkOrder", "WO-MED-003", admin).status == "released"

        # 7. approve_sample_free_shipment
        rt.submit_action("T-SMP-OK", "approve_sample_free_shipment", {"order_id": "SMP-SO-001"}, admin)
        smp = rt.get_object("DomesticSampleOrder", "SMP-SO-001", admin)
        assert smp.x_sample_approved is True
        assert smp.status == "confirmed"

        SampleCls = rt.spec.get_object_type("DomesticSampleOrder")
        rt.put_object(
            SampleCls(
                order_id="SMP-TOO-MANY",
                order_number="SMP-TOO-MANY",
                customer="CUST-CN-03",
                accounting_entity="ACC-ENT-MEDAUTO",
                amount_total=500.0,
                x_sample_qty=6,
            ),
            actor=admin,
        )
        with pytest.raises(InvariantError) as exc_smp:
            rt.submit_action("T-SMP-BLK", "approve_sample_free_shipment", {"order_id": "SMP-TOO-MANY"}, admin)
        assert exc_smp.value.code == "SAMPLE_QTY_EXCEEDS_FREE_LIMIT"
    finally:
        rt.close()


def test_medauto_custom_edit_functions_runtime() -> None:
    rt, admin, cfo, pm, ops = _fresh_medauto_runtime()
    try:
        # 8. apply_export_tax_rebate_claim (nonlinear rounded FX calculation)
        rt.submit_action("EF-CLR", "declare_customs_clearance", {"declaration_id": "CD-001"}, ops)
        rt.submit_action(
            "EF-REBATE",
            "apply_export_tax_rebate_claim",
            {"declaration_id": "CD-001", "rebate_rate": 0.13, "fx_rate": 7.20},
            cfo,
        )
        cd = rt.get_object("CustomsDeclaration", "CD-001", admin)
        assert cd.rebate_claimed is True
        assert cd.rebate_amount_cents == int(round(320_000.0 * 100.0 * 0.13 * 7.20))
        with pytest.raises(InvariantError) as exc_dup_reb:
            rt.submit_action("EF-REBATE-2", "apply_export_tax_rebate_claim", {"declaration_id": "CD-001"}, cfo)
        assert exc_dup_reb.value.code == "REBATE_ALREADY_CLAIMED"

        # 9. batch_recall_defective_lot (create rework WO first so WorkOrder + ReworkWorkOrder + O2cDelivery all quarantine)
        rt.submit_action(
            "EF-RW-SEED",
            "open_rework_work_order",
            {
                "inspection_id": "QI-003",
                "rework_work_order_id": "RWO-MED-LOT",
                "rework_qty": 8,
                "defect_reason": "批号异常",
            },
            pm,
        )
        rt.submit_action(
            "EF-RECALL",
            "batch_recall_defective_lot",
            {"lot_code": "LOT-2026-09", "reason": "微裂纹跨产线召回"},
            pm,
        )
        assert rt.get_object("WorkOrder", "WO-MED-001", admin).x_quarantined is True
        assert rt.get_object("WorkOrder", "WO-MED-002", admin).x_quarantined is True
        assert rt.get_object("ReworkWorkOrder", "RWO-MED-LOT", admin).x_quarantined is True
        assert rt.get_object("O2cDelivery", "DLV-MED-001", admin).x_quarantined is True
        assert rt.get_object("O2cDelivery", "DLV-MED-002", admin).x_quarantined is True
        recall_cards = [
            c
            for c in rt.query_objects("WorkCard", admin)
            if c.requested_action == "batch_recall_defective_lot"
        ]
        assert len(recall_cards) == 5

        # 10. explode_multilevel_substitute_bom (2-level BFS + fallback to substitute BOM-CORE-SUB)
        rt.submit_action(
            "EF-BOM",
            "explode_multilevel_substitute_bom",
            {
                "parent_work_order_id": "WO-MED-003",
                "available_stock_json": json.dumps({"RES-TI-BAR-DE": 5.0, "RES-TI-BAR-CN": 10.0}),
                "child_wo_prefix": "EXPL-WO",
            },
            admin,
        )
        sub1 = rt.get_object("WorkOrder", "EXPL-WO-01", admin)
        sub2 = rt.get_object("WorkOrder", "EXPL-WO-02", admin)
        assert sub1 is not None and sub1.bom == "BOM-CORE-SUB" and sub1.planned_qty == 20.0
        assert sub2 is not None and sub2.bom == "BOM-BAR-CN" and sub2.planned_qty == 20.0

        # 11. settle_annual_tiered_dealer_rebate (4-tier progressive math + FIFO invoice offset)
        rt.submit_action(
            "EF-DEALER-REB",
            "settle_annual_tiered_dealer_rebate",
            {
                "settlement_id": "STL-DE-2026",
                "settlement_code": "REB-2026-DE-01",
                "account_id": "ACC-DE-01",
            },
            cfo,
        )
        stl = rt.get_object("DealerRebateSettlement", "STL-DE-2026", admin)
        # Sales: SO-STD-001 (420k) + SO-STD-002 (860k) + EXP-SO-002 (320k) = 1,600,000 RMB
        # Progressive rebate: 200k*2% + 300k*4% + 500k*6% + 600k*8% = 4k + 12k + 30k + 48k = 94,000 RMB
        assert stl.annual_sales_cents == 160_000_000
        assert stl.rebate_cents == 9_400_000
        assert stl.offset_invoice_count == 2
        inv1 = rt.get_object("O2cInvoice", "INV-DE-001", admin)
        inv2 = rt.get_object("O2cInvoice", "INV-DE-002", admin)
        assert inv1.amount_residual == 0.0 and inv1.payment_state == "paid"
        assert inv2.amount_residual == 4000.0 and inv2.payment_state == "partial"

        # 12. allocate_container_shipping_cost (weight-proportional with exact penny remainder)
        rt.submit_action(
            "EF-FREIGHT",
            "allocate_container_shipping_cost",
            {"container_no": "CNTR-DE-01", "total_freight_cents": 100003},
            ops,
        )
        e1 = rt.get_object("ExportSalesOrder", "EXP-SO-001", admin)
        e2 = rt.get_object("ExportSalesOrder", "EXP-SO-002", admin)
        assert e1.x_allocated_freight_cents == 60001
        assert e2.x_allocated_freight_cents == 40002
        assert e1.x_allocated_freight_cents + e2.x_allocated_freight_cents == 100003
    finally:
        rt.close()


def test_medauto_pure_l1_console_playwright_e2e() -> None:
    pytest.importorskip("z3")
    sync_api = pytest.importorskip("playwright.sync_api")

    svc = build_medauto_console(enable_llm_assistant=False)
    srv = start_console_server(svc, host="127.0.0.1", port=0)
    try:
        console_errors: list[str] = []
        page_errors: list[str] = []
        http_errors: list[int] = []

        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            page.on(
                "response",
                lambda resp: http_errors.append(resp.status) if resp.status >= 400 else None,
            )

            # 1. Pure L1 shell verification at / + /api/meta check
            meta_payload = page.request.get(f"{srv.base_url}/api/meta").json()
            assert meta_payload["pack_hint"] == "none"

            page.goto(f"{srv.base_url}/", wait_until="networkidle")
            page.get_by_test_id("cockpit-page").wait_for(state="visible", timeout=15000)
            body_text = page.locator("body").inner_text()
            assert "总览工作台" in body_text
            assert "实时对象目录与关系穿透" in body_text
            assert page.locator("text=经营驾驶舱").count() == 0
            assert page.locator("text=实时业务台账与关系穿透").count() == 0

            # 2. Blueprint explorer + Inheritance badge + Z3 Proof
            page.get_by_test_id("nav-blueprint").click()
            page.get_by_test_id("blueprint-page").wait_for(state="visible", timeout=15000)
            bp_text = page.get_by_test_id("blueprint-page").inner_text()
            assert "Rule 29" in bp_text
            assert "EditFn 14" in bp_text

            page.get_by_test_id("blueprint-node-ExportSalesOrder").click()
            inspector = page.get_by_test_id("blueprint-object-inspector")
            inspector.wait_for(state="visible", timeout=10000)
            assert "继承自 O2C销售订单" in page.get_by_test_id("blueprint-extends-badge").inner_text()

            page.get_by_test_id("run-z3-prove-btn").click()
            verdict = page.get_by_test_id("z3-verdict-clean")
            verdict.wait_for(state="visible", timeout=30000)
            verdict_text = verdict.inner_text()
            assert "0 死锁 / 0 不变量违例" in verdict_text
            assert "29 / 29" in verdict_text
            assert "冗余前置约束：0" in verdict_text.replace("\n", "")

            # 3. All 4 Sandbox presets execute cleanly without touching live DB
            page.get_by_test_id("nav-sandbox").click()
            page.get_by_test_id("sandbox-page").wait_for(state="visible", timeout=15000)
            for pid in (
                "medauto_doal_fai_and_risk",
                "medauto_doal_rework_wo",
                "medauto_ef_batch_recall",
                "medauto_ef_container_freight",
            ):
                page.get_by_test_id(f"sandbox-preset-{pid}").click()
                page.get_by_test_id("sandbox-diff-list").wait_for(state="visible", timeout=15000)
                page.get_by_test_id("sandbox-reset-btn").click()

            browser.close()

        assert console_errors == []
        assert page_errors == []
        assert http_errors == []
    finally:
        srv.stop()
        svc.runtime.close()
