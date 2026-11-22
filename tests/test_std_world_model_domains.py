"""WO-24A 验收与红测套件：std_core 增量扩展 + P2M / R2R / H2R 三大纯 M1 标准世界模型本体。

覆盖：
1. ``std_core_ontology`` 向后兼容增量：新增 ``FulfillmentEventKind``（``PRODUCTION_ISSUE /
   PRODUCTION_OUTPUT / JOURNAL_POST``）、6 个闭集标准角色及 ``WorkCard`` 开立/核准角色分工；
2. **P2M（计划到生产）**：正向闭环（建单→下达→领料→报工→守恒诊断→完工；未领料取消），以及全量负向红测
   （未下达领料、领料成本不守恒、未领料报工/完工、超吸收成本守恒、超损耗率触发
   ``APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED`` + ``WorkCard`` SoD + 厂长 $t_1$ 重算放行、已领料禁取消）；
3. **R2R（记录到报告 / 复式总账）**：正向闭环（多凭证过账→实时试算平衡与五大类会计恒等式→红字冲正→
   启动结账→关账生成 ``TrialBalanceSnapshot``），以及全量负向红测（借贷同科目自平衡、跨主体串户、
   冻结科目、手工大额调账触发 ``APPROVAL_REQUIRED_MANUAL_JOURNAL`` + ``WorkCard`` SoD + 财务总监 $t_1$
   放行、未过账凭证冲正、重复冲正、关账后过账/红冲拦截、试算不平关账拦截、无权限角色关账拦截）；
4. **H2R（组织人事与动态授权）**：正向闭环（部门建树调整→定岗激活→委派授权→有效授权解析→离职自动收权），
   以及全量负向红测（部门 1 跳自环与多跳环路拦截、离职员工禁调岗、未知角色校验、双边在职校验、
   自授权 SoD 拦截（含同 ``employee_id`` 与同 ``actor_id``）、越权委派拦截 ``MANDATE_EXCEEDS_GRANTOR_CEILING``、
   升级审批触发 ``APPROVAL_REQUIRED_AUTHORITY_ESCALATION`` + ``WorkCard`` SoD + 人力总监 $t_1$ 放行、
   离职原子吊销全部 ``active`` 授权防幽灵权限残留）；
5. **声明式蓝图编译、组合与 ``open_ontology.design check``**：P2M / R2R / H2R 各自与 ``std_core`` 组合、
   全六域（CRM + O2C + P2P + P2M + R2R + H2R）菱形合并与双端编译一致性及实跑。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from examples.business_ontology import (
    BUSINESS_SEED_ONTOLOGY,
    WorkCard,
    settle_work_card,
    submit_or_open_work_card,
)
from examples.std_core_ontology import (
    STD_ROLES,
    STD_WORK_CARD_OPENERS,
    STD_WORK_CARD_RESOLVERS,
    AccountingEntity,
    BusinessParty,
    FulfillmentEvent,
    TradeCommitment,
    TradeResource,
    build_std_core_blueprint,
)
from examples.std_crm_ontology import build_std_crm_blueprint, spec as std_crm_spec
from examples.std_h2r_ontology import (
    AuthorityMandate,
    Department,
    Employee,
    JobPosition,
    build_std_h2r_blueprint,
    spec as std_h2r_spec,
)
from examples.std_o2c_ontology import build_std_o2c_blueprint, spec as std_o2c_spec
from examples.std_p2m_ontology import (
    BomSpec,
    MaterialConsumption,
    ProductionOutput,
    WorkOrder,
    build_std_p2m_blueprint,
    spec as std_p2m_spec,
)
from examples.std_p2p_ontology import build_std_p2p_blueprint, spec as std_p2p_spec
from examples.std_r2r_ontology import (
    MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS,
    FiscalPeriod,
    GlAccount,
    JournalEntry,
    TrialBalanceSnapshot,
    build_std_r2r_blueprint,
    spec as std_r2r_spec,
)
from open_ontology import Actor, InvariantError, OntologyRuntime, OntologySpec, PermissionDeniedError
from open_ontology.design import run_design_check, run_design_verify
from open_ontology.induction import compile_blueprint, compose_blueprints


# ---------------------------------------------------------------------------
# 1. std_core_ontology Backward-Compatible Expansion Tests
# ---------------------------------------------------------------------------
def test_1_std_core_incremental_event_kinds_and_closed_roles() -> None:
    core_bp = build_std_core_blueprint()
    core_spec = compile_blueprint(core_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(core_spec, ":memory:")
    try:
        admin = Actor(actor_id="adm-core", role="admin", clearance_tags={"*"})
        PartyCls = core_spec.get_object_type("BusinessParty")
        ResCls = core_spec.get_object_type("TradeResource")
        CommCls = core_spec.get_object_type("TradeCommitment")
        EvtCls = core_spec.get_object_type("FulfillmentEvent")

        rt.put_object(PartyCls(party_id="PTY-A", legal_name="华北制造集团"), actor=admin)
        rt.put_object(PartyCls(party_id="PTY-B", legal_name="华东供应链公司"), actor=admin)
        rt.put_object(ResCls(resource_id="RES-01", code="COIL", name="冷轧卷板", unit="TON"), actor=admin)
        rt.put_object(
            CommCls(
                commitment_id="CM-CORE-1",
                buyer_party="PTY-A",
                seller_party="PTY-B",
                resource="RES-01",
                quantity=10.0,
                unit_price=4000.0,
                total_amount=40_000.0,
                status="agreed",
            ),
            actor=admin,
        )

        # Original + new FulfillmentEventKind values all work seamlessly
        for idx, kind in enumerate(
            (
                "SHIPMENT",
                "RECEIPT",
                "INSPECTION",
                "INVOICE",
                "PAYMENT",
                "PRODUCTION_ISSUE",
                "PRODUCTION_OUTPUT",
                "JOURNAL_POST",
            ),
            start=1,
        ):
            evt = EvtCls(
                event_id=f"EVT-{idx}",
                commitment="CM-CORE-1",
                event_kind=kind,
                quantity=1.0,
                amount=100.0,
                status="recorded",
            )
            rt.put_object(evt, actor=admin)
            assert rt.get_object("FulfillmentEvent", f"EVT-{idx}", admin).event_kind == kind

        role_names = {r for r, _ in STD_ROLES}
        expected_new_roles = {
            "production_planner",
            "shopfloor",
            "plant_manager",
            "chief_accountant",
            "hr_admin",
            "hr_director",
        }
        assert expected_new_roles <= role_names
        assert set(core_bp.roles) == role_names
        assert {"production_planner", "shopfloor", "chief_accountant", "hr_admin"} <= set(STD_WORK_CARD_OPENERS)
        assert {"plant_manager", "chief_accountant", "hr_director"} <= set(STD_WORK_CARD_RESOLVERS)
        # Operational front-line roles cannot self-resolve WorkCards
        assert "shopfloor" not in STD_WORK_CARD_RESOLVERS
        assert "hr_admin" not in STD_WORK_CARD_RESOLVERS
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. P2M Domain: Positive Flow & Red Tests
# ---------------------------------------------------------------------------
def _seed_p2m_world(rt: OntologyRuntime, spec: OntologySpec) -> dict[str, Actor]:
    admin = Actor(actor_id="adm-p2m", role="admin", clearance_tags={"*"})
    EntCls = spec.get_object_type("AccountingEntity")
    PartyCls = spec.get_object_type("BusinessParty")
    ResCls = spec.get_object_type("TradeResource")
    CommCls = spec.get_object_type("TradeCommitment")
    EvtCls = spec.get_object_type("FulfillmentEvent")
    BomCls = spec.get_object_type("BomSpec")
    WoCls = spec.get_object_type("WorkOrder")

    rt.put_object(EntCls(entity_id="ENT-MFG", name="华北智能制造工厂", base_currency="CNY"), actor=admin)
    rt.put_object(PartyCls(party_id="PTY-MFG", legal_name="华北智造有限公司"), actor=admin)
    rt.put_object(PartyCls(party_id="PTY-CUST", legal_name="华东基建有限公司"), actor=admin)
    rt.put_object(ResCls(resource_id="RES-RAW-STEEL", code="RAW-Q235", name="热轧带钢", unit="KG"), actor=admin)
    rt.put_object(ResCls(resource_id="RES-RAW-OTHER", code="RAW-ALT", name="辅助焊丝", unit="KG"), actor=admin)
    rt.put_object(ResCls(resource_id="RES-FG-BEAM", code="FG-H200", name="H型钢构件", unit="EA"), actor=admin)
    rt.put_object(
        CommCls(
            commitment_id="CM-MFG-1",
            buyer_party="PTY-CUST",
            seller_party="PTY-MFG",
            resource="RES-FG-BEAM",
            quantity=100.0,
            unit_price=500.0,
            total_amount=50_000.0,
            status="agreed",
        ),
        actor=admin,
    )
    rt.put_object(
        EvtCls(
            event_id="EVT-ISSUE-1",
            commitment="CM-MFG-1",
            event_kind="PRODUCTION_ISSUE",
            quantity=200.0,
            amount=1000.0,
            status="confirmed",
        ),
        actor=admin,
    )
    rt.put_object(
        EvtCls(
            event_id="EVT-OUT-1",
            commitment="CM-MFG-1",
            event_kind="PRODUCTION_OUTPUT",
            quantity=95.0,
            amount=950.0,
            status="confirmed",
        ),
        actor=admin,
    )
    rt.put_object(
        BomCls(
            bom_id="BOM-BEAM-V1",
            output_resource="RES-FG-BEAM",
            input_resource="RES-RAW-STEEL",
            input_per_unit=2.0,
            max_scrap_ratio=0.05,
            version="1.0",
            status="active",
        ),
        actor=admin,
    )
    rt.put_object(
        BomCls(
            bom_id="BOM-RETIRED",
            output_resource="RES-FG-BEAM",
            input_resource="RES-RAW-STEEL",
            input_per_unit=2.0,
            max_scrap_ratio=0.05,
            version="0.9",
            status="retired",
        ),
        actor=admin,
    )
    rt.put_object(
        WoCls(
            work_order_id="WO-100",
            accounting_entity="ENT-MFG",
            bom="BOM-BEAM-V1",
            commitment="CM-MFG-1",
            output_resource="RES-FG-BEAM",
            planned_qty=100.0,
            status="draft",
        ),
        actor=admin,
    )
    rt.put_object(
        WoCls(
            work_order_id="WO-CANCEL-OK",
            accounting_entity="ENT-MFG",
            bom="BOM-BEAM-V1",
            output_resource="RES-FG-BEAM",
            planned_qty=10.0,
            status="draft",
        ),
        actor=admin,
    )
    return {
        "admin": admin,
        "planner": Actor(actor_id="plan-1", role="production_planner"),
        "shopfloor": Actor(actor_id="sf-1", role="shopfloor"),
        "warehouse": Actor(actor_id="wh-1", role="warehouse"),
        "plant_manager": Actor(actor_id="pm-1", role="plant_manager"),
        "same_sf_pm": Actor(actor_id="sf-1", role="plant_manager"),
    }


def test_2_p2m_positive_full_lifecycle_and_blueprint_parity() -> None:
    # Run positive flow on Python spec (including @spec.function diagnostics)
    rt = OntologyRuntime(std_p2m_spec, ":memory:")
    try:
        actors = _seed_p2m_world(rt, std_p2m_spec)

        # Unissued work order can be cleanly cancelled
        rt.submit_action("P2M-REL-CAN", "release_work_order", {"work_order_id": "WO-CANCEL-OK"}, actors["planner"])
        rt.submit_action(
            "P2M-CAN-OK",
            "close_work_order",
            {"work_order_id": "WO-CANCEL-OK", "target_status": "cancelled"},
            actors["planner"],
        )
        assert rt.get_object(WorkOrder, "WO-CANCEL-OK", actors["planner"]).status == "cancelled"

        # Main flow: release -> issue -> output -> diagnose -> close
        rt.submit_action("P2M-REL-1", "release_work_order", {"work_order_id": "WO-100"}, actors["planner"])
        assert rt.get_object(WorkOrder, "WO-100", actors["planner"]).status == "released"

        rt.submit_action(
            "P2M-ISS-1",
            "issue_material_to_work_order",
            {
                "consumption_id": "CONS-100-1",
                "work_order_id": "WO-100",
                "input_resource": "RES-RAW-STEEL",
                "qty": 200.0,
                "unit_cost_cents": 500,
                "allocation_rule": "FIFO",
                "source_event": "EVT-ISSUE-1",
            },
            actors["warehouse"],
        )
        wo = rt.get_object(WorkOrder, "WO-100", actors["shopfloor"])
        assert wo.status == "in_progress"
        assert wo.issued_qty == pytest.approx(200.0)
        assert wo.issued_cost_cents == 100_000
        cons = rt.get_object(MaterialConsumption, "CONS-100-1", actors["shopfloor"])
        assert cons.total_cost_cents == 100_000
        assert cons.allocation_rule == "FIFO"

        rt.submit_action(
            "P2M-OUT-1",
            "record_production_output",
            {
                "output_id": "OUT-100-1",
                "work_order_id": "WO-100",
                "output_resource": "RES-FG-BEAM",
                "good_qty": 96.0,
                "scrap_qty": 4.0,  # 4 / 100 = 0.04 <= max_scrap_ratio 0.05
                "absorbed_cost_cents": 95_000,
                "output_event": "EVT-OUT-1",
            },
            actors["shopfloor"],
        )
        diag = rt.call_function("diagnose_work_order_conservation", {"work_order_id": "WO-100"})
        assert diag["all_cost_conserved"] is True
        row = diag["work_orders"][0]
        assert row["remaining_unabsorbed_cost_cents"] == 5_000
        assert row["scrap_ratio"] == pytest.approx(0.04)

        rt.submit_action(
            "P2M-CLOSE-1",
            "close_work_order",
            {"work_order_id": "WO-100", "target_status": "completed"},
            actors["planner"],
        )
        assert rt.get_object(WorkOrder, "WO-100", actors["planner"]).status == "completed"
        out = rt.get_object(ProductionOutput, "OUT-100-1", actors["planner"])
        assert out.absorbed_cost_cents == 95_000
    finally:
        rt.close()

    # Run identical positive flow on compiled DeclarativeBlueprint!
    bp_spec = compile_blueprint(build_std_p2m_blueprint(), seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt_bp = OntologyRuntime(bp_spec, ":memory:")
    try:
        actors_bp = _seed_p2m_world(rt_bp, bp_spec)
        rt_bp.submit_action("BP-REL", "release_work_order", {"work_order_id": "WO-100"}, actors_bp["planner"])
        rt_bp.submit_action(
            "BP-ISS",
            "issue_material_to_work_order",
            {
                "consumption_id": "CONS-BP-1",
                "work_order_id": "WO-100",
                "input_resource": "RES-RAW-STEEL",
                "qty": 200.0,
                "unit_cost_cents": 500,
                "total_cost_cents": 100_000,
            },
            actors_bp["warehouse"],
        )
        rt_bp.submit_action(
            "BP-OUT",
            "record_production_output",
            {
                "output_id": "OUT-BP-1",
                "work_order_id": "WO-100",
                "output_resource": "RES-FG-BEAM",
                "good_qty": 96.0,
                "scrap_qty": 4.0,
                "absorbed_cost_cents": 100_000,
            },
            actors_bp["shopfloor"],
        )
        rt_bp.submit_action("BP-CLOSE", "close_work_order", {"work_order_id": "WO-100"}, actors_bp["planner"])
        assert rt_bp.get_object("WorkOrder", "WO-100", actors_bp["planner"]).status == "completed"
    finally:
        rt_bp.close()


def test_3_p2m_red_tests_and_scrap_work_card_t1_settlement() -> None:
    rt = OntologyRuntime(std_p2m_spec, ":memory:")
    try:
        actors = _seed_p2m_world(rt, std_p2m_spec)
        WoCls = std_p2m_spec.get_object_type("WorkOrder")

        # Red 1: issue material on draft WO -> WORK_ORDER_NOT_OPEN_FOR_ISSUE
        with pytest.raises(InvariantError) as e_draft:
            rt.submit_action(
                "RED-ISS-DRAFT",
                "issue_material_to_work_order",
                {
                    "consumption_id": "C-BAD-1",
                    "work_order_id": "WO-100",
                    "input_resource": "RES-RAW-STEEL",
                    "qty": 10.0,
                    "unit_cost_cents": 500,
                },
                actors["warehouse"],
            )
        assert e_draft.value.code == "WORK_ORDER_NOT_OPEN_FOR_ISSUE"

        # Red 2: output on draft or released WO before material issue -> CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE
        with pytest.raises(InvariantError) as e_no_issue_draft:
            rt.submit_action(
                "RED-OUT-DRAFT",
                "record_production_output",
                {
                    "output_id": "O-BAD-1",
                    "work_order_id": "WO-100",
                    "output_resource": "RES-FG-BEAM",
                    "good_qty": 10.0,
                },
                actors["shopfloor"],
            )
        assert e_no_issue_draft.value.code == "CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE"

        # Release with retired BOM blocked -> BOM_NOT_ACTIVE
        rt.put_object(
            WoCls(
                work_order_id="WO-RETIRED-BOM",
                accounting_entity="ENT-MFG",
                bom="BOM-RETIRED",
                output_resource="RES-FG-BEAM",
                planned_qty=10.0,
                status="draft",
            ),
            actor=actors["admin"],
        )
        with pytest.raises(InvariantError) as e_bom:
            rt.submit_action("RED-REL-BOM", "release_work_order", {"work_order_id": "WO-RETIRED-BOM"}, actors["planner"])
        assert e_bom.value.code == "BOM_NOT_ACTIVE"

        # Release WO-100, verify output before issue still blocked
        rt.submit_action("REL-100", "release_work_order", {"work_order_id": "WO-100"}, actors["planner"])
        with pytest.raises(InvariantError) as e_no_issue_rel:
            rt.submit_action(
                "RED-OUT-REL",
                "record_production_output",
                {
                    "output_id": "O-BAD-2",
                    "work_order_id": "WO-100",
                    "output_resource": "RES-FG-BEAM",
                    "good_qty": 5.0,
                },
                actors["shopfloor"],
            )
        assert e_no_issue_rel.value.code == "CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE"

        # Close before issue also blocked
        with pytest.raises(InvariantError) as e_close_no_iss:
            rt.submit_action("RED-CLOSE-NOISS", "close_work_order", {"work_order_id": "WO-100"}, actors["planner"])
        assert e_close_no_iss.value.code == "CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE"

        # Red 3: non-BOM material or mismatched issue cost blocked
        with pytest.raises(InvariantError) as e_mat:
            rt.submit_action(
                "RED-ISS-MAT",
                "issue_material_to_work_order",
                {
                    "consumption_id": "C-BAD-2",
                    "work_order_id": "WO-100",
                    "input_resource": "RES-RAW-OTHER",
                    "qty": 10.0,
                    "unit_cost_cents": 100,
                },
                actors["warehouse"],
            )
        assert e_mat.value.code == "MATERIAL_NOT_IN_BOM"

        with pytest.raises(InvariantError) as e_cost_mis:
            rt.submit_action(
                "RED-ISS-COST",
                "issue_material_to_work_order",
                {
                    "consumption_id": "C-BAD-3",
                    "work_order_id": "WO-100",
                    "input_resource": "RES-RAW-STEEL",
                    "qty": 10.0,
                    "unit_cost_cents": 100,
                    "total_cost_cents": 999,
                },
                actors["warehouse"],
            )
        assert e_cost_mis.value.code == "ISSUE_COST_MISMATCH"

        # Issue 100 units @ 500 cents = 50,000 cents
        rt.submit_action(
            "ISS-100",
            "issue_material_to_work_order",
            {
                "consumption_id": "CONS-1",
                "work_order_id": "WO-100",
                "input_resource": "RES-RAW-STEEL",
                "qty": 100.0,
                "unit_cost_cents": 500,
            },
            actors["warehouse"],
        )

        # Red 4: cannot cancel issued work order -> CANNOT_CANCEL_ISSUED_WORK_ORDER
        with pytest.raises(InvariantError) as e_cancel:
            rt.submit_action(
                "RED-CANCEL-ISS",
                "close_work_order",
                {"work_order_id": "WO-100", "target_status": "cancelled"},
                actors["planner"],
            )
        assert e_cancel.value.code == "CANNOT_CANCEL_ISSUED_WORK_ORDER"

        # Red 5: cost conservation violation -> COST_CONSERVATION_VIOLATION
        with pytest.raises(InvariantError) as e_cons:
            rt.submit_action(
                "RED-ABSORB",
                "record_production_output",
                {
                    "output_id": "OUT-OVER",
                    "work_order_id": "WO-100",
                    "output_resource": "RES-FG-BEAM",
                    "good_qty": 40.0,
                    "scrap_qty": 0.0,
                    "absorbed_cost_cents": 50_001,
                },
                actors["shopfloor"],
            )
        assert e_cons.value.code == "COST_CONSERVATION_VIOLATION"

        # Record first valid output absorbing 30,000 cents (remaining 20,000)
        rt.submit_action(
            "OUT-1",
            "record_production_output",
            {
                "output_id": "OUT-OK-1",
                "work_order_id": "WO-100",
                "output_resource": "RES-FG-BEAM",
                "good_qty": 40.0,
                "scrap_qty": 0.0,
                "absorbed_cost_cents": 30_000,
            },
            actors["shopfloor"],
        )
        # Second output absorbing 20,001 > remaining 20,000 -> COST_CONSERVATION_VIOLATION
        with pytest.raises(InvariantError) as e_cons2:
            rt.submit_action(
                "RED-ABSORB-2",
                "record_production_output",
                {
                    "output_id": "OUT-OVER-2",
                    "work_order_id": "WO-100",
                    "output_resource": "RES-FG-BEAM",
                    "good_qty": 10.0,
                    "scrap_qty": 0.0,
                    "absorbed_cost_cents": 20_001,
                },
                actors["shopfloor"],
            )
        assert e_cons2.value.code == "COST_CONSERVATION_VIOLATION"

        # Red 6: scrap ratio > 0.05 (40 good + 10 good + 10 scrap => 10/60 = 16.67%) -> WorkCard + SoD + t1 settle
        esc = submit_or_open_work_card(
            rt,
            "RUN-SCRAP-EXC",
            "record_production_output",
            {
                "output_id": "OUT-SCRAP-2",
                "work_order_id": "WO-100",
                "output_resource": "RES-FG-BEAM",
                "good_qty": 10.0,
                "scrap_qty": 10.0,
                "absorbed_cost_cents": 20_000,
            },
            actors["shopfloor"],
            target_object_type="WorkOrder",
            target_object_id="WO-100",
            assignee_role="plant_manager",
            card_id="WC-SCRAP-100",
        )
        assert esc["status"] == "AWAITING_WORK_CARD"
        assert esc["error_code"] == "APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED"
        assert rt.get_object(WorkCard, "WC-SCRAP-100", actors["shopfloor"]).status == "OPEN"

        # SoD: same actor_id with plant_manager role cannot self-approve
        with pytest.raises(InvariantError) as e_sod:
            settle_work_card(rt, "WC-SCRAP-100", actors["same_sf_pm"])
        assert e_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        # Independent plant_manager settles WorkCard -> t1 commits output
        settled = settle_work_card(rt, "WC-SCRAP-100", actors["plant_manager"], resolution_note="核实模具异常，批准超损耗报工")
        assert settled["settled_action_run"]["status"] == "COMMITTED"
        assert rt.get_object(WorkCard, "WC-SCRAP-100", actors["plant_manager"]).status == "COMPLETED"
        wo_after = rt.get_object(WorkOrder, "WO-100", actors["plant_manager"])
        assert wo_after.completed_good_qty == pytest.approx(50.0)
        assert wo_after.completed_scrap_qty == pytest.approx(10.0)

        # Complete WO and verify issue on completed WO is blocked
        rt.submit_action("CLOSE-100", "close_work_order", {"work_order_id": "WO-100"}, actors["planner"])
        with pytest.raises(InvariantError) as e_done_iss:
            rt.submit_action(
                "RED-ISS-DONE",
                "issue_material_to_work_order",
                {
                    "consumption_id": "C-BAD-4",
                    "work_order_id": "WO-100",
                    "input_resource": "RES-RAW-STEEL",
                    "qty": 1.0,
                    "unit_cost_cents": 500,
                },
                actors["warehouse"],
            )
        assert e_done_iss.value.code == "WORK_ORDER_NOT_OPEN_FOR_ISSUE"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. R2R Domain: Double-Entry Positive Flow & Red Tests
# ---------------------------------------------------------------------------
def _seed_r2r_world(rt: OntologyRuntime, spec: OntologySpec) -> dict[str, Actor]:
    admin = Actor(actor_id="adm-r2r", role="admin", clearance_tags={"*"})
    EntCls = spec.get_object_type("AccountingEntity")
    PartyCls = spec.get_object_type("BusinessParty")
    ResCls = spec.get_object_type("TradeResource")
    CommCls = spec.get_object_type("TradeCommitment")
    EvtCls = spec.get_object_type("FulfillmentEvent")
    PerCls = spec.get_object_type("FiscalPeriod")
    AccCls = spec.get_object_type("GlAccount")

    rt.put_object(EntCls(entity_id="ENT-HQ", name="总账集团总部", base_currency="CNY"), actor=admin)
    rt.put_object(EntCls(entity_id="ENT-SUB", name="华南子公司", base_currency="CNY"), actor=admin)
    rt.put_object(PartyCls(party_id="PTY-1", legal_name="总部"), actor=admin)
    rt.put_object(PartyCls(party_id="PTY-2", legal_name="客户"), actor=admin)
    rt.put_object(ResCls(resource_id="RES-SVC", code="SVC", name="工业服务", unit="EA"), actor=admin)
    rt.put_object(
        CommCls(
            commitment_id="CM-R2R-1",
            buyer_party="PTY-2",
            seller_party="PTY-1",
            resource="RES-SVC",
            quantity=1.0,
            unit_price=8000.0,
            total_amount=8000.0,
            status="agreed",
        ),
        actor=admin,
    )
    rt.put_object(
        EvtCls(
            event_id="EVT-R2R-1",
            commitment="CM-R2R-1",
            event_kind="JOURNAL_POST",
            quantity=1.0,
            amount=8000.0,
            status="confirmed",
        ),
        actor=admin,
    )
    rt.put_object(
        PerCls(period_id="2026-11", accounting_entity="ENT-HQ", start_date="2026-11-01", end_date="2026-11-30", status="open"),
        actor=admin,
    )
    rt.put_object(
        PerCls(period_id="2026-10", accounting_entity="ENT-HQ", start_date="2026-10-01", end_date="2026-10-31", status="closing"),
        actor=admin,
    )
    rt.put_object(
        PerCls(period_id="2026-11-SUB", accounting_entity="ENT-SUB", start_date="2026-11-01", end_date="2026-11-30", status="open"),
        actor=admin,
    )

    accounts = [
        ("1001", "ENT-HQ", "银行存款", "ASSET", "DEBIT", "active"),
        ("1122", "ENT-HQ", "应收账款", "ASSET", "DEBIT", "active"),
        ("2202", "ENT-HQ", "应付账款", "LIABILITY", "CREDIT", "active"),
        ("4001", "ENT-HQ", "实收资本", "EQUITY", "CREDIT", "active"),
        ("6001", "ENT-HQ", "主营业务收入", "REVENUE", "CREDIT", "active"),
        ("6401", "ENT-HQ", "主营业务成本", "EXPENSE", "DEBIT", "active"),
        ("6602-FRZ", "ENT-HQ", "冻结费用科目", "EXPENSE", "DEBIT", "frozen"),
        ("1001-SUB", "ENT-SUB", "子公司银行存款", "ASSET", "DEBIT", "active"),
    ]
    for code, ent, name, cat, side, st in accounts:
        rt.put_object(
            AccCls(
                account_code=code,
                accounting_entity=ent,
                name=name,
                category=cat,
                normal_side=side,
                status=st,
            ),
            actor=admin,
        )

    return {
        "admin": admin,
        "finance": Actor(actor_id="fin-1", role="finance"),
        "chief_acc": Actor(actor_id="ca-1", role="chief_accountant"),
        "fin_dir": Actor(actor_id="fd-1", role="finance_director"),
        "same_fin_dir": Actor(actor_id="fin-1", role="finance_director"),
    }


def test_4_r2r_positive_double_entry_reversal_trial_balance_and_close() -> None:
    rt = OntologyRuntime(std_r2r_spec, ":memory:")
    try:
        actors = _seed_r2r_world(rt, std_r2r_spec)
        JeCls = std_r2r_spec.get_object_type("JournalEntry")

        # 1. Capital injection: Dr 1001 Cash 1,000,000 / Cr 4001 Equity 1,000,000 (backed by EVT-R2R-1)
        rt.put_object(
            JeCls(
                entry_id="JE-1",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                source_event="EVT-R2R-1",
                debit_account="1001",
                credit_account="4001",
                amount_cents=1_000_000,
                memo="资本金注入",
            ),
            actor=actors["admin"],
        )
        # 2. Revenue recognition: Dr 1122 AR 200,000 / Cr 6001 Revenue 200,000 (manual < 500,000)
        rt.put_object(
            JeCls(
                entry_id="JE-2",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1122",
                credit_account="6001",
                amount_cents=200_000,
                memo="销售确认收入",
            ),
            actor=actors["admin"],
        )
        # 3. Operating expense: Dr 6401 Expense 50,000 / Cr 2202 AP 50,000
        rt.put_object(
            JeCls(
                entry_id="JE-3",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="6401",
                credit_account="2202",
                amount_cents=50_000,
                memo="服务成本计提",
            ),
            actor=actors["admin"],
        )
        # 4. Erroneous manual entry to be reversed: Dr 6401 / Cr 1001 30,000
        rt.put_object(
            JeCls(
                entry_id="JE-4-ERR",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="6401",
                credit_account="1001",
                amount_cents=30_000,
                memo="误记银行手续费",
            ),
            actor=actors["admin"],
        )

        for eid in ("JE-1", "JE-2", "JE-3", "JE-4-ERR"):
            rt.submit_action(f"POST-{eid}", "post_journal_entry", {"entry_id": eid}, actors["finance"])

        tb_mid = rt.call_function("compute_trial_balance", {"accounting_entity": "ENT-HQ"})
        assert tb_mid["is_balanced"] is True
        assert tb_mid["accounting_equation_holds"] is True
        assert tb_mid["total_debit_cents"] == tb_mid["total_credit_cents"] == 1_280_000

        # Red-letter reversal of JE-4-ERR creates swapped posted entry & marks original reversed
        rt.submit_action(
            "REV-JE4",
            "reverse_journal_entry",
            {"entry_id": "JE-4-ERR", "reversal_entry_id": "JE-4-REV"},
            actors["chief_acc"],
        )
        orig_je4 = rt.get_object(JournalEntry, "JE-4-ERR", actors["chief_acc"])
        rev_je4 = rt.get_object(JournalEntry, "JE-4-REV", actors["chief_acc"])
        assert orig_je4.status == "reversed"
        assert rev_je4.status == "posted"
        assert rev_je4.reversal_of == "JE-4-ERR"
        assert rev_je4.debit_account == "1001"
        assert rev_je4.credit_account == "6401"
        assert rev_je4.amount_cents == 30_000

        # Trial balance after reversal remains balanced and net effect of JE-4-ERR is zeroed out
        tb_final = rt.call_function(
            "compute_trial_balance",
            {"accounting_entity": "ENT-HQ", "fiscal_period": "2026-11"},
        )
        assert tb_final["is_balanced"] is True
        assert tb_final["accounting_equation_holds"] is True
        assert tb_final["total_debit_cents"] == tb_final["total_credit_cents"] == 1_310_000
        assert tb_final["category_net_cents"] == {
            "ASSET": 1_200_000,
            "EXPENSE": 50_000,
            "LIABILITY": 50_000,
            "EQUITY": 1_000_000,
            "REVENUE": 200_000,
        }
        assert tb_final["asset_plus_expense_cents"] == 1_250_000
        assert tb_final["liability_equity_revenue_cents"] == 1_250_000
        assert tb_final["entry_count"] == 5

        # Closing transition & snapshot creation
        rt.submit_action("START-CLS", "start_fiscal_period_closing", {"period_id": "2026-11"}, actors["chief_acc"])
        assert rt.get_object(FiscalPeriod, "2026-11", actors["chief_acc"]).status == "closing"
        rt.submit_action(
            "CLOSE-2026-11",
            "close_fiscal_period",
            {"period_id": "2026-11", "snapshot_id": "TBS-HQ-2026-11"},
            actors["chief_acc"],
        )
        assert rt.get_object(FiscalPeriod, "2026-11", actors["chief_acc"]).status == "closed"
        snap = rt.get_object(TrialBalanceSnapshot, "TBS-HQ-2026-11", actors["chief_acc"])
        assert snap.is_balanced is True
        assert snap.total_debit_cents == 1_310_000
        assert snap.total_credit_cents == 1_310_000
        assert snap.entry_count == 5
    finally:
        rt.close()


def test_5_r2r_red_tests_manual_work_card_sod_and_unbalanced_close_block() -> None:
    # Isolated red test for global self-balancing invariant (seeded invalid JE-SELF must not poison shared DB)
    rt_self = OntologyRuntime(std_r2r_spec, ":memory:")
    try:
        actors_self = _seed_r2r_world(rt_self, std_r2r_spec)
        JeSelfCls = std_r2r_spec.get_object_type("JournalEntry")
        rt_self.put_object(
            JeSelfCls(
                entry_id="JE-SELF",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="1001",
                amount_cents=10_000,
            ),
            actor=actors_self["admin"],
        )
        with pytest.raises(InvariantError) as e_self:
            rt_self.submit_action("RED-SELF", "post_journal_entry", {"entry_id": "JE-SELF"}, actors_self["finance"])
        assert e_self.value.code == "SELF_BALANCING_ACCOUNT_FORBIDDEN"
    finally:
        rt_self.close()

    rt = OntologyRuntime(std_r2r_spec, ":memory:")
    try:
        actors = _seed_r2r_world(rt, std_r2r_spec)
        JeCls = std_r2r_spec.get_object_type("JournalEntry")
        AccCls = std_r2r_spec.get_object_type("GlAccount")

        # Red 2: cross-entity journal -> CROSS_ENTITY_JOURNAL_FORBIDDEN
        rt.put_object(
            JeCls(
                entry_id="JE-CROSS",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="1001-SUB",
                amount_cents=10_000,
            ),
            actor=actors["admin"],
        )
        with pytest.raises(InvariantError) as e_cross:
            rt.submit_action("RED-CROSS", "post_journal_entry", {"entry_id": "JE-CROSS"}, actors["finance"])
        assert e_cross.value.code == "CROSS_ENTITY_JOURNAL_FORBIDDEN"

        # Red 3: frozen account -> GL_ACCOUNT_FROZEN
        rt.put_object(
            JeCls(
                entry_id="JE-FROZEN",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="6602-FRZ",
                credit_account="1001",
                amount_cents=10_000,
            ),
            actor=actors["admin"],
        )
        with pytest.raises(InvariantError) as e_frz:
            rt.submit_action("RED-FRZ", "post_journal_entry", {"entry_id": "JE-FROZEN"}, actors["finance"])
        assert e_frz.value.code == "GL_ACCOUNT_FROZEN"

        # Red 4: reverse draft entry -> ENTRY_NOT_POSTED
        rt.put_object(
            JeCls(
                entry_id="JE-DRAFT",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1122",
                credit_account="6001",
                amount_cents=25_000,
            ),
            actor=actors["admin"],
        )
        with pytest.raises(InvariantError) as e_rev_draft:
            rt.submit_action("RED-REV-DRAFT", "reverse_journal_entry", {"entry_id": "JE-DRAFT"}, actors["chief_acc"])
        assert e_rev_draft.value.code == "ENTRY_NOT_POSTED"

        # Red 5: manual journal >= threshold triggers APPROVAL_REQUIRED_MANUAL_JOURNAL -> WorkCard + SoD + t1 settle
        rt.put_object(
            JeCls(
                entry_id="JE-BIG-MAN",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                source_event=None,
                debit_account="6401",
                credit_account="2202",
                amount_cents=MANUAL_JOURNAL_APPROVAL_THRESHOLD_CENTS,
                memo="大额手工调账",
            ),
            actor=actors["admin"],
        )
        esc = submit_or_open_work_card(
            rt,
            "RUN-BIG-MAN",
            "post_journal_entry",
            {"entry_id": "JE-BIG-MAN"},
            actors["finance"],
            target_object_type="JournalEntry",
            target_object_id="JE-BIG-MAN",
            assignee_role="finance_director",
            card_id="WC-JE-BIG",
        )
        assert esc["status"] == "AWAITING_WORK_CARD"
        assert esc["error_code"] == "APPROVAL_REQUIRED_MANUAL_JOURNAL"
        assert rt.get_object(JournalEntry, "JE-BIG-MAN", actors["finance"]).status == "draft"

        with pytest.raises(InvariantError) as e_sod:
            settle_work_card(rt, "WC-JE-BIG", actors["same_fin_dir"])
        assert e_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        settled = settle_work_card(rt, "WC-JE-BIG", actors["fin_dir"], resolution_note="复核调账依据无误，批准过账")
        assert settled["settled_action_run"]["status"] == "COMMITTED"
        assert rt.get_object(JournalEntry, "JE-BIG-MAN", actors["fin_dir"]).status == "posted"

        # Reverse JE-BIG-MAN once, then reject duplicate reversal -> ENTRY_ALREADY_REVERSED
        rt.submit_action(
            "REV-BIG-1",
            "reverse_journal_entry",
            {"entry_id": "JE-BIG-MAN", "reversal_entry_id": "JE-BIG-REV"},
            actors["chief_acc"],
        )
        with pytest.raises(InvariantError) as e_dup_rev:
            rt.submit_action("REV-BIG-2", "reverse_journal_entry", {"entry_id": "JE-BIG-MAN"}, actors["chief_acc"])
        assert e_dup_rev.value.code == "ENTRY_ALREADY_REVERSED"

        # Red 6: close period permission check & closed period posting/reversal block
        with pytest.raises(PermissionDeniedError):
            rt.submit_action("RED-CLOSE-ROLE", "close_fiscal_period", {"period_id": "2026-11"}, actors["finance"])

        # Seed a posted entry in 2026-11 before closing so we can verify reversal after close is blocked
        rt.put_object(
            JeCls(
                entry_id="JE-POST-BEFORE-CLOSE",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="6001",
                amount_cents=12_000,
                status="draft",
            ),
            actor=actors["admin"],
        )
        rt.submit_action("POST-BC", "post_journal_entry", {"entry_id": "JE-POST-BEFORE-CLOSE"}, actors["finance"])
        rt.submit_action("CLOSE-HQ", "close_fiscal_period", {"period_id": "2026-11"}, actors["chief_acc"])

        # Posting into closed period blocked
        rt.put_object(
            JeCls(
                entry_id="JE-AFTER-CLOSE",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="6001",
                amount_cents=10_000,
            ),
            actor=actors["admin"],
        )
        with pytest.raises(InvariantError) as e_post_cls:
            rt.submit_action("RED-POST-CLS", "post_journal_entry", {"entry_id": "JE-AFTER-CLOSE"}, actors["finance"])
        assert e_post_cls.value.code == "PERIOD_CLOSED_POSTING_FORBIDDEN"

        # Reversing into closed period blocked
        with pytest.raises(InvariantError) as e_rev_cls:
            rt.submit_action("RED-REV-CLS", "reverse_journal_entry", {"entry_id": "JE-POST-BEFORE-CLOSE"}, actors["chief_acc"])
        assert e_rev_cls.value.code == "PERIOD_CLOSED_POSTING_FORBIDDEN"

        # Red 7: unbalanced subsidiary entity blocks close_fiscal_period with TRIAL_BALANCE_UNBALANCED
        rt.put_object(
            AccCls(
                account_code="1001-SUB",
                accounting_entity="ENT-SUB",
                name="子公司银行存款",
                category="ASSET",
                normal_side="DEBIT",
                debit_total_cents=99_999,
                credit_total_cents=0,
                status="active",
            ),
            actor=actors["admin"],
        )
        with pytest.raises(InvariantError) as e_unbal:
            rt.submit_action("RED-CLOSE-UNBAL", "close_fiscal_period", {"period_id": "2026-11-SUB"}, actors["chief_acc"])
        assert e_unbal.value.code == "TRIAL_BALANCE_UNBALANCED"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 4. H2R Domain: Org Hierarchy DAG, Authority Mandates & Offboarding Tests
# ---------------------------------------------------------------------------
def _seed_h2r_world(rt: OntologyRuntime, spec: OntologySpec) -> dict[str, Actor]:
    admin = Actor(actor_id="adm-h2r", role="admin", clearance_tags={"*"})
    EntCls = spec.get_object_type("AccountingEntity")
    DeptCls = spec.get_object_type("Department")
    PosCls = spec.get_object_type("JobPosition")
    EmpCls = spec.get_object_type("Employee")

    rt.put_object(EntCls(entity_id="ENT-CORP", name="集团主体", base_currency="CNY"), actor=admin)
    rt.put_object(EntCls(entity_id="ENT-OTHER", name="兄弟法人主体", base_currency="CNY"), actor=admin)

    rt.put_object(DeptCls(department_id="DEPT-ROOT", accounting_entity="ENT-CORP", name="集团总部", scope_tag="org:root"), actor=admin)
    rt.put_object(DeptCls(department_id="DEPT-OPS", accounting_entity="ENT-CORP", name="运营中心", parent_department="DEPT-ROOT", scope_tag="org:ops"), actor=admin)
    rt.put_object(DeptCls(department_id="DEPT-MFG", accounting_entity="ENT-CORP", name="制造一部", parent_department="DEPT-OPS", scope_tag="org:mfg"), actor=admin)
    rt.put_object(DeptCls(department_id="DEPT-LINE", accounting_entity="ENT-CORP", name="轧钢车间", parent_department="DEPT-MFG", scope_tag="org:line"), actor=admin)
    rt.put_object(DeptCls(department_id="DEPT-OTHER-ENT", accounting_entity="ENT-OTHER", name="外部子公司部门"), actor=admin)
    rt.put_object(DeptCls(department_id="DEPT-ARCHIVED", accounting_entity="ENT-CORP", name="已撤并部门", status="archived"), actor=admin)

    rt.put_object(PosCls(position_id="POS-PM", department="DEPT-OPS", title="厂长", role_name="plant_manager", approval_ceiling_cents=1_000_000), actor=admin)
    rt.put_object(PosCls(position_id="POS-BUYER", department="DEPT-MFG", title="采购专员", role_name="buyer", approval_ceiling_cents=200_000), actor=admin)
    rt.put_object(PosCls(position_id="POS-SF", department="DEPT-LINE", title="车间报工", role_name="shopfloor", approval_ceiling_cents=50_000), actor=admin)

    rt.put_object(
        EmpCls(employee_id="EMP-MGR", department="DEPT-OPS", position="POS-PM", full_name="张厂长", actor_id="act-mgr", status="active"),
        actor=admin,
    )
    rt.put_object(
        EmpCls(employee_id="EMP-LI", department="DEPT-MFG", position="POS-BUYER", full_name="李工", actor_id="act-li", status="onboarding"),
        actor=admin,
    )
    rt.put_object(
        EmpCls(employee_id="EMP-WANG", department="DEPT-LINE", position="POS-SF", full_name="王师傅", actor_id="act-wang", status="active"),
        actor=admin,
    )
    rt.put_object(
        EmpCls(employee_id="EMP-DUP-ACT", department="DEPT-LINE", position="POS-SF", full_name="双工号分身", actor_id="act-mgr", status="active"),
        actor=admin,
    )

    return {
        "admin": admin,
        "hr_admin": Actor(actor_id="hr-1", role="hr_admin"),
        "hr_director": Actor(actor_id="hrd-1", role="hr_director"),
        "same_hr_dir": Actor(actor_id="hr-1", role="hr_director"),
    }


def test_6_h2r_positive_flow_reparent_assign_mandate_and_offboard_auto_revoke() -> None:
    rt = OntologyRuntime(std_h2r_spec, ":memory:")
    try:
        actors = _seed_h2r_world(rt, std_h2r_spec)

        # Reparent DEPT-LINE directly under DEPT-ROOT
        rt.submit_action(
            "H2R-REPARENT",
            "reparent_department",
            {"department_id": "DEPT-LINE", "new_parent_id": "DEPT-ROOT"},
            actors["hr_admin"],
        )
        assert rt.get_object(Department, "DEPT-LINE", actors["hr_admin"]).parent_department == "DEPT-ROOT"

        # Assign & activate onboarding employee EMP-LI -> active
        rt.submit_action(
            "H2R-ASSIGN",
            "assign_employee_position",
            {
                "employee_id": "EMP-LI",
                "department_id": "DEPT-OPS",
                "position_id": "POS-PM",
                "activate": True,
            },
            actors["hr_admin"],
        )
        emp_li = rt.get_object(Employee, "EMP-LI", actors["hr_admin"])
        assert emp_li.status == "active"
        assert emp_li.department == "DEPT-OPS"
        assert emp_li.position == "POS-PM"

        # Grant two mandates within grantor ceiling (EMP-MGR ceiling = 1,000,000) to EMP-WANG
        rt.submit_action(
            "H2R-GRANT-1",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-1",
                "grantor_id": "EMP-MGR",
                "grantee_id": "EMP-WANG",
                "role_name": "procurement_manager",
                "ceiling_cents": 800_000,
                "scope_tag": "org:mfg",
            },
            actors["hr_admin"],
        )
        rt.submit_action(
            "H2R-GRANT-2",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-2",
                "grantor_id": "EMP-MGR",
                "grantee_id": "EMP-WANG",
                "role_name": "plant_manager",
                "ceiling_cents": 500_000,
                "scope_tag": "org:line",
            },
            actors["hr_admin"],
        )

        # Read-only effective mandates resolution
        eff = rt.call_function("resolve_employee_effective_mandates", {"employee_id": "EMP-WANG"})
        assert eff["department_ancestry"] == ["DEPT-LINE", "DEPT-ROOT"]
        assert len(eff["active_mandates"]) == 2
        assert eff["effective_ceilings_by_role"]["shopfloor"] == 50_000
        assert eff["effective_ceilings_by_role"]["procurement_manager"] == 800_000
        assert eff["effective_ceilings_by_role"]["plant_manager"] == 500_000

        # Offboard EMP-WANG -> atomically revokes all active AuthorityMandates (zero ghost mandates!)
        rt.submit_action(
            "H2R-OFFBOARD",
            "offboard_employee",
            {"employee_id": "EMP-WANG", "reason": "合同期满离职"},
            actors["hr_admin"],
        )
        assert rt.get_object(Employee, "EMP-WANG", actors["hr_admin"]).status == "offboarded"
        assert rt.get_object(AuthorityMandate, "MND-1", actors["hr_admin"]).status == "revoked"
        assert rt.get_object(AuthorityMandate, "MND-2", actors["hr_admin"]).status == "revoked"
        eff_after = rt.call_function("resolve_employee_effective_mandates", {"employee_id": "EMP-WANG"})
        assert eff_after["active_mandates"] == []
        assert eff_after["effective_ceilings_by_role"] == {}
    finally:
        rt.close()


def test_7_h2r_red_tests_dag_cycles_closed_roles_sod_ceilings_and_escalation() -> None:
    rt = OntologyRuntime(std_h2r_spec, ":memory:")
    try:
        actors = _seed_h2r_world(rt, std_h2r_spec)

        # Red 1: 1-hop self-loop & multi-hop cycle in reparent_department -> DEPARTMENT_CYCLE_DETECTED
        with pytest.raises(InvariantError) as e_self_loop:
            rt.submit_action(
                "RED-CYCLE-1",
                "reparent_department",
                {"department_id": "DEPT-OPS", "new_parent_id": "DEPT-OPS"},
                actors["hr_admin"],
            )
        assert e_self_loop.value.code == "DEPARTMENT_CYCLE_DETECTED"

        # DEPT-LINE -> DEPT-MFG -> DEPT-OPS -> DEPT-ROOT; reparenting DEPT-ROOT under DEPT-LINE forms a 4-node cycle!
        with pytest.raises(InvariantError) as e_multi_cycle:
            rt.submit_action(
                "RED-CYCLE-4",
                "reparent_department",
                {"department_id": "DEPT-ROOT", "new_parent_id": "DEPT-LINE"},
                actors["hr_admin"],
            )
        assert e_multi_cycle.value.code == "DEPARTMENT_CYCLE_DETECTED"

        # Cross-entity reparent blocked
        with pytest.raises(InvariantError) as e_cross_dept:
            rt.submit_action(
                "RED-CROSS-DEPT",
                "reparent_department",
                {"department_id": "DEPT-MFG", "new_parent_id": "DEPT-OTHER-ENT"},
                actors["hr_admin"],
            )
        assert e_cross_dept.value.code == "CROSS_ENTITY_DEPARTMENT_FORBIDDEN"

        # Red 2: unknown role in JobPosition (isolated) or AuthorityMandate -> UNKNOWN_ROLE
        rt_rogue_pos = OntologyRuntime(std_h2r_spec, ":memory:")
        try:
            actors_rp = _seed_h2r_world(rt_rogue_pos, std_h2r_spec)
            PosCls = std_h2r_spec.get_object_type("JobPosition")
            rt_rogue_pos.put_object(
                PosCls(position_id="POS-ROGUE", department="DEPT-OPS", title="幽灵角色岗", role_name="finace_director"),
                actor=actors_rp["admin"],
            )
            with pytest.raises(InvariantError) as e_pos_role:
                rt_rogue_pos.submit_action(
                    "RED-POS-ROLE",
                    "assign_employee_position",
                    {"employee_id": "EMP-LI", "department_id": "DEPT-OPS", "position_id": "POS-ROGUE"},
                    actors_rp["hr_admin"],
                )
            assert e_pos_role.value.code == "UNKNOWN_ROLE"
        finally:
            rt_rogue_pos.close()

        with pytest.raises(InvariantError) as e_mnd_role:
            rt.submit_action(
                "RED-MND-ROLE",
                "grant_authority_mandate",
                {
                    "mandate_id": "MND-BAD-ROLE",
                    "grantor_id": "EMP-MGR",
                    "grantee_id": "EMP-WANG",
                    "role_name": "super_hacker",
                    "ceiling_cents": 100_000,
                },
                actors["hr_admin"],
            )
        assert e_mnd_role.value.code == "UNKNOWN_ROLE"

        # Red 3: self-grant mandate (same employee_id and same actor_id) -> MANDATE_SELF_GRANT_FORBIDDEN
        with pytest.raises(InvariantError) as e_self_mnd:
            rt.submit_action(
                "RED-SELF-MND",
                "grant_authority_mandate",
                {
                    "mandate_id": "MND-SELF",
                    "grantor_id": "EMP-MGR",
                    "grantee_id": "EMP-MGR",
                    "role_name": "plant_manager",
                    "ceiling_cents": 100_000,
                },
                actors["hr_admin"],
            )
        assert e_self_mnd.value.code == "MANDATE_SELF_GRANT_FORBIDDEN"

        with pytest.raises(InvariantError) as e_self_actor_mnd:
            rt.submit_action(
                "RED-SELF-ACT-MND",
                "grant_authority_mandate",
                {
                    "mandate_id": "MND-SELF-ACT",
                    "grantor_id": "EMP-MGR",
                    "grantee_id": "EMP-DUP-ACT",
                    "role_name": "plant_manager",
                    "ceiling_cents": 100_000,
                },
                actors["hr_admin"],
            )
        assert e_self_actor_mnd.value.code == "MANDATE_SELF_GRANT_FORBIDDEN"

        # Red 4: onboarding/inactive employee cannot grant or receive mandate -> EMPLOYEE_NOT_ACTIVE
        with pytest.raises(InvariantError) as e_inact:
            rt.submit_action(
                "RED-INACT",
                "grant_authority_mandate",
                {
                    "mandate_id": "MND-INACT",
                    "grantor_id": "EMP-MGR",
                    "grantee_id": "EMP-LI",  # still onboarding
                    "role_name": "buyer",
                    "ceiling_cents": 100_000,
                },
                actors["hr_admin"],
            )
        assert e_inact.value.code == "EMPLOYEE_NOT_ACTIVE"

        # Activate EMP-LI as buyer (ceiling 200,000), then attempt over-ceiling delegation -> MANDATE_EXCEEDS_GRANTOR_CEILING
        rt.submit_action(
            "ACT-LI",
            "assign_employee_position",
            {"employee_id": "EMP-LI", "department_id": "DEPT-MFG", "position_id": "POS-BUYER", "activate": True},
            actors["hr_admin"],
        )
        with pytest.raises(InvariantError) as e_ceil:
            rt.submit_action(
                "RED-CEIL",
                "grant_authority_mandate",
                {
                    "mandate_id": "MND-OVER",
                    "grantor_id": "EMP-LI",
                    "grantee_id": "EMP-WANG",
                    "role_name": "procurement_manager",
                    "ceiling_cents": 500_000,  # > EMP-LI's 200,000
                    "escalate_approval": False,
                },
                actors["hr_admin"],
            )
        assert e_ceil.value.code == "MANDATE_EXCEEDS_GRANTOR_CEILING"

        # Escalated over-ceiling request triggers APPROVAL_REQUIRED_AUTHORITY_ESCALATION -> WorkCard + SoD + t1 settle
        esc = submit_or_open_work_card(
            rt,
            "RUN-ESC-MND",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-OVER-ESC",
                "grantor_id": "EMP-LI",
                "grantee_id": "EMP-WANG",
                "role_name": "procurement_manager",
                "ceiling_cents": 500_000,
                "escalate_approval": True,
            },
            actors["hr_admin"],
            target_object_type="Employee",
            target_object_id="EMP-WANG",
            assignee_role="hr_director",
            card_id="WC-H2R-ESC",
        )
        assert esc["status"] == "AWAITING_WORK_CARD"
        assert esc["error_code"] == "APPROVAL_REQUIRED_AUTHORITY_ESCALATION"

        with pytest.raises(InvariantError) as e_sod:
            settle_work_card(rt, "WC-H2R-ESC", actors["same_hr_dir"])
        assert e_sod.value.code == "SOD_SELF_APPROVAL_FORBIDDEN"

        settled = settle_work_card(rt, "WC-H2R-ESC", actors["hr_director"], resolution_note="批准临时扩权委派")
        assert settled["settled_action_run"]["status"] == "COMMITTED"
        assert rt.get_object(AuthorityMandate, "MND-OVER-ESC", actors["hr_director"]).status == "active"

        # Red 5: offboard EMP-WANG -> MND-OVER-ESC auto-revoked; subsequent reassignment blocked -> EMPLOYEE_OFFBOARDED
        rt.submit_action("OFF-WANG", "offboard_employee", {"employee_id": "EMP-WANG"}, actors["hr_admin"])
        assert rt.get_object(AuthorityMandate, "MND-OVER-ESC", actors["hr_admin"]).status == "revoked"

        with pytest.raises(InvariantError) as e_rehire_off:
            rt.submit_action(
                "RED-OFF-ASSIGN",
                "assign_employee_position",
                {"employee_id": "EMP-WANG", "department_id": "DEPT-LINE", "position_id": "POS-SF"},
                actors["hr_admin"],
            )
        assert e_rehire_off.value.code == "EMPLOYEE_OFFBOARDED"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 5. Declarative Blueprint Composition, design check/verify & 6-Domain Unification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("domain_mod", "builder_name"),
    [
        ("examples.std_p2m_ontology", "build_std_p2m_blueprint"),
        ("examples.std_r2r_ontology", "build_std_r2r_blueprint"),
        ("examples.std_h2r_ontology", "build_std_h2r_blueprint"),
    ],
)
def test_8_declarative_blueprint_composition_design_check_and_crucible(
    domain_mod: str,
    builder_name: str,
) -> None:
    core_bp = build_std_core_blueprint()
    mod = __import__(domain_mod, fromlist=[builder_name])
    domain_bp = getattr(mod, builder_name)()
    assert domain_bp.imports == ["std_core_ontology"]

    composed = compose_blueprints(
        [core_bp, domain_bp],
        model_id=f"{domain_bp.model_id}_composed_check",
        version="1.0.0",
    )
    compiled = compile_blueprint(composed, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    assert set(compiled.roles.keys()) == {r for r, _ in STD_ROLES}

    chk = run_design_check(
        blueprints=["examples.std_core_ontology:build_std_core_blueprint", f"{domain_mod}:{builder_name}"],
        seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
    )
    assert chk["ok"] is True, chk["errors"]

    ver = run_design_verify(
        blueprints=["examples.std_core_ontology:build_std_core_blueprint", f"{domain_mod}:{builder_name}"],
        seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
    )
    assert ver["ok"] is True, ver["crucible_feedback"]

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_ontology.design",
            "check",
            "--blueprint",
            "examples.std_core_ontology:build_std_core_blueprint",
            "--blueprint",
            f"{domain_mod}:{builder_name}",
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    cli_res = json.loads(proc.stdout)
    assert cli_res["ok"] is True


def test_9_all_six_standard_domains_compose_idempotently_and_match_python_specs() -> None:
    py_unified = OntologySpec(
        extends=(
            std_crm_spec,
            std_o2c_spec,
            std_p2p_spec,
            std_p2m_spec,
            std_r2r_spec,
            std_h2r_spec,
        )
    )
    bp_unified = compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
            build_std_p2p_blueprint(),
            build_std_p2m_blueprint(),
            build_std_r2r_blueprint(),
            build_std_h2r_blueprint(),
        ],
        model_id="std_six_domains_unified",
        version="1.0.0",
        seed_ontology_id="business_ontology",
    )
    compiled_unified = compile_blueprint(bp_unified, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    assert bp_unified.imports == [
        "std_core_ontology",
        "std_crm_ontology",
        "std_o2c_ontology",
        "std_p2p_ontology",
        "std_p2m_ontology",
        "std_r2r_ontology",
        "std_h2r_ontology",
    ]
    assert set(py_unified.roles.keys()) == set(compiled_unified.roles.keys())
    assert set(py_unified.object_types.keys()) == set(compiled_unified.object_types.keys())
    assert set(py_unified.actions.keys()) == set(compiled_unified.actions.keys())
    assert set(py_unified.object_properties.keys()) == set(compiled_unified.object_properties.keys())
    assert len(py_unified.object_types) == 29
    assert len(py_unified.actions) == 31
    for ot_name, py_cls in py_unified.object_types.items():
        bp_cls = compiled_unified.get_object_type(ot_name)
        assert py_cls.__primary_key__ == bp_cls.__primary_key__
        assert py_cls.is_aggregate_root() == bp_cls.is_aggregate_root()
        assert getattr(py_cls, "__transitions__", {}) == getattr(bp_cls, "__transitions__", {})

    # Smoke-test compiled blueprint execution on R2R and H2R positive paths as well
    bp_r2r = compile_blueprint(build_std_r2r_blueprint(), seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt_r2r = OntologyRuntime(bp_r2r, ":memory:")
    try:
        actors_r2r = _seed_r2r_world(rt_r2r, bp_r2r)
        JeBp = bp_r2r.get_object_type("JournalEntry")
        rt_r2r.put_object(
            JeBp(
                entry_id="JE-BP-1",
                accounting_entity="ENT-HQ",
                fiscal_period="2026-11",
                debit_account="1001",
                credit_account="4001",
                amount_cents=100_000,
            ),
            actor=actors_r2r["admin"],
        )
        rt_r2r.submit_action("BP-POST-JE", "post_journal_entry", {"entry_id": "JE-BP-1"}, actors_r2r["finance"])
        assert rt_r2r.get_object("JournalEntry", "JE-BP-1", actors_r2r["finance"]).status == "posted"
        assert rt_r2r.get_object("GlAccount", "1001", actors_r2r["finance"]).debit_total_cents == 100_000
        assert rt_r2r.get_object("GlAccount", "4001", actors_r2r["finance"]).credit_total_cents == 100_000
    finally:
        rt_r2r.close()

    bp_h2r = compile_blueprint(build_std_h2r_blueprint(), seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt_h2r = OntologyRuntime(bp_h2r, ":memory:")
    try:
        actors_h2r = _seed_h2r_world(rt_h2r, bp_h2r)
        rt_h2r.submit_action(
            "BP-REPARENT",
            "reparent_department",
            {"department_id": "DEPT-LINE", "new_parent_id": "DEPT-ROOT"},
            actors_h2r["hr_admin"],
        )
        rt_h2r.submit_action(
            "BP-GRANT",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-BP-1",
                "grantor_id": "EMP-MGR",
                "grantee_id": "EMP-WANG",
                "role_name": "buyer",
                "ceiling_cents": 300_000,
            },
            actors_h2r["hr_admin"],
        )
        assert rt_h2r.get_object("AuthorityMandate", "MND-BP-1", actors_h2r["hr_admin"]).status == "active"
    finally:
        rt_h2r.close()
