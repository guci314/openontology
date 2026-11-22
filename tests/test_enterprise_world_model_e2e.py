"""WO-24B 全链路企业标准世界模型总装 + H2R→M2 投影桥 + Standalone（SQLite / Live PG16）双底座验收套件。

覆盖：
1. ``std_enterprise_world_model`` 六域全量总装（29 对象 / 31 动作 / 19 闭集角色）、子集裁剪及蓝图/Python 规范一致性；
2. 零外部 ERP ``standalone`` 全链路企业经营闭环（SQLite ``:memory:`` 与 Live PostgreSQL 16 ``127.0.0.1:15432`` 双底座）：
   * Step 1（H2R + M2 ``org_authority``）：集团→南北分厂→采销财部门树、在岗定编、动态授权、跨厂区 RLS 隔离、
     分级额度判定、离职重投影自动失效；
   * Step 2（L2O / ``std_crm``）：客户账户、商机阶段跃迁、高危客诉处理、赢单签约 ``TradeCommitment``；
   * Step 3（P2P / ``std_p2p``）：采购确认、短磅争议挂起、``PO_DISPUTED_BLOCKED`` 拦截、``WorkCard`` 裁决解封、
     对账收票付款与已确认履约事件产出；
   * Step 4（P2M / ``std_p2m``）：BOM/工单下达、领料投料、超损耗率 ``WorkCard`` 厂长 $t_1$ 审批、
     ``diagnose_work_order_conservation`` 成本守恒校验、完工关闭；
   * Step 5（O2C / ``std_o2c``）：未出库禁开票拦截、发货出库、开票入账与全额回款核销；
   * Step 6（R2R / ``std_r2r`` + ``runtime.fork()``）：全链路履约事件确定性投影过账 8 笔复式凭证、
     ``runtime.fork()`` 反事实沙盘试演大额费用（验证主库指纹零变化）、``compute_trial_balance`` 精确对账与会计恒等式、
     关账落盘 ``TrialBalanceSnapshot`` 及关账后禁补记/禁红冲锁定；
3. ``build_org_rls_enterprise_spec`` 基于 H2R→M2 投影实现跨厂区实体行级安全（RLS）物理隔离（SQLite + PG16）；
4. ``CustomerOverlaySpec`` 只增不破扩展验证（``open_ontology.design check`` CLI + API 零报错、破坏性 Overlay 拦截、
   制造企业专属 ``owned=True`` 字段与扩展风控前置实跑通过，SQLite + PG16）；
5. Live PostgreSQL 16 临时 schema 零残留卫生检查。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterator
import uuid

import psycopg
import pytest

from open_ontology import Actor, InvariantError, OntologyRuntime, PermissionDeniedError
from open_ontology.design import run_design_check
from open_ontology.induction import (
    CustomerOverlaySpec,
    FieldSpec,
    InvariantRuleSpec,
    blueprint_fingerprint,
    compile_blueprint,
)
from open_ontology.org_authority import (
    AUTHORITY_ORG_SCOPE_DENIED,
    AUTHORITY_TIER_INSUFFICIENT,
    ORG_MEMBER_INACTIVE,
    ORG_MEMBER_NOT_FOUND,
    SOD_SELF_APPROVAL_FORBIDDEN,
    OrgAuthorityError,
    OrgDirectory,
    hash_auth_token,
)
from open_ontology.postgres_backend import PostgresOntologyRuntime, open_postgres_runtime

from examples.business_ontology import (
    BUSINESS_SEED_ONTOLOGY,
    WorkCard,
    settle_work_card,
    submit_or_open_work_card,
)
from examples.std_core_ontology import STD_ROLES, build_std_core_blueprint
from examples.std_crm_ontology import build_std_crm_blueprint
from examples.std_enterprise_world_model import (
    ENTERPRISE_DOMAIN_KEYS,
    build_enterprise_standard_blueprint,
    build_enterprise_standard_spec,
    build_h2r_tiered_authority_policy,
    build_org_rls_enterprise_spec,
    evaluate_h2r_employee_authority,
    open_standalone_enterprise_world,
    project_and_post_event_journal,
    project_event_to_draft_journal,
    project_h2r_to_org_directory,
    verify_h2r_approver_authority,
)
from examples.std_h2r_ontology import AuthorityMandate, Employee, build_std_h2r_blueprint
from examples.std_o2c_ontology import O2cInvoice, O2cOrder, build_std_o2c_blueprint
from examples.std_p2m_ontology import WorkOrder, build_std_p2m_blueprint
from examples.std_p2p_ontology import P2pGoodsReceipt, P2pPurchaseOrder, P2pVendorBill, build_std_p2p_blueprint
from examples.std_r2r_ontology import FiscalPeriod, TrialBalanceSnapshot, build_std_r2r_blueprint

PG_DSN = os.environ.get(
    "OPEN_ONTOLOGY_PG_DSN",
    "postgresql://open_ontology:open_ontology_dev@127.0.0.1:15432/open_ontology",
)
_CREATED_PG_SCHEMAS: list[str] = []


@contextmanager
def _open_enterprise_world(
    backend: str,
    spec: Any = None,
) -> Iterator[OntologyRuntime]:
    if backend == "sqlite":
        rt = (
            open_standalone_enterprise_world(":memory:")
            if spec is None
            else OntologyRuntime(spec, db_path=":memory:", world_mode="standalone")
        )
        try:
            yield rt
        finally:
            rt.close()
        return

    assert backend == "postgres"
    eff_spec = spec if spec is not None else build_enterprise_standard_spec()
    schema = f"wo24b_{uuid.uuid4().hex[:10]}"
    _CREATED_PG_SCHEMAS.append(schema)
    rt = open_postgres_runtime(eff_spec, PG_DSN, schema_name=schema, world_mode="standalone")
    try:
        assert isinstance(rt, PostgresOntologyRuntime)
        yield rt
    finally:
        rt.close()
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            remaining = conn.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            ).fetchall()
            assert remaining == [], f"PG16 temporary schema leaked: {remaining}"


def _seed_common_hr_and_accounting_world(rt: OntologyRuntime, admin: Actor) -> None:
    spec = rt.spec
    EntCls = spec.get_object_type("AccountingEntity")
    DeptCls = spec.get_object_type("Department")
    PosCls = spec.get_object_type("JobPosition")
    EmpCls = spec.get_object_type("Employee")

    rt.put_object(EntCls(entity_id="ENT-GROUP", name="华实智能装备股份有限公司", base_currency="CNY"), actor=admin)

    depts = [
        ("DEPT-HQ", "集团总部", None, "org:hq", "active"),
        ("DEPT-NORTH-MFG", "华北制造厂", "DEPT-HQ", "org:plant_north", "active"),
        ("DEPT-SOUTH-MFG", "华南制造厂", "DEPT-HQ", "org:plant_south", "active"),
        ("DEPT-SALES", "华实销售中心", "DEPT-HQ", "org:sales", "active"),
        ("DEPT-PROC", "集团采购中心", "DEPT-HQ", "org:proc", "active"),
        ("DEPT-FIN", "集团财务中心", "DEPT-HQ", "org:fin", "active"),
        ("DEPT-OLD-ARCH", "已撤并老车间", "DEPT-HQ", "org:legacy_arch", "archived"),
        ("DEPT-ARCH-CHILD", "老车间遗留班组", "DEPT-OLD-ARCH", "org:legacy_child", "active"),
    ]
    for did, name, pid, stag, st in depts:
        rt.put_object(
            DeptCls(
                department_id=did,
                accounting_entity="ENT-GROUP",
                name=name,
                parent_department=pid,
                scope_tag=stag,
                status=st,
            ),
            actor=admin,
        )

    positions = [
        ("POS-CFO", "DEPT-HQ", "集团财务总监", "finance_director", 500_000_000),
        ("POS-HR-ADMIN", "DEPT-HQ", "集团组织人事专员", "hr_admin", 5_000_000),
        ("POS-HR-DIR", "DEPT-HQ", "集团人力总监", "hr_director", 100_000_000),
        ("POS-PLANNER", "DEPT-NORTH-MFG", "生产计划员", "production_planner", 10_000_000),
        ("POS-PM-NORTH", "DEPT-NORTH-MFG", "华北厂长", "plant_manager", 100_000_000),
        ("POS-SF-NORTH", "DEPT-NORTH-MFG", "华北车间报工", "shopfloor", 5_000_000),
        ("POS-SF-SOUTH", "DEPT-SOUTH-MFG", "华南车间报工", "shopfloor", 5_000_000),
        ("POS-SALES-REP", "DEPT-SALES", "销售代表", "sales_rep", 5_000_000),
        ("POS-SUPPORT", "DEPT-SALES", "客户支持专员", "support_agent", 5_000_000),
        ("POS-PROC-MGR", "DEPT-PROC", "采购经理", "procurement_manager", 20_000_000),
        ("POS-BUYER", "DEPT-PROC", "采购专员", "buyer", 5_000_000),
        ("POS-WH", "DEPT-NORTH-MFG", "仓储专员", "warehouse", 5_000_000),
        ("POS-FIN", "DEPT-FIN", "总账会计", "finance", 10_000_000),
        ("POS-CA", "DEPT-FIN", "财务主管", "chief_accountant", 50_000_000),
    ]
    for pid, did, title, rname, ceil_cents in positions:
        rt.put_object(
            PosCls(
                position_id=pid,
                department=did,
                title=title,
                role_name=rname,
                approval_ceiling_cents=ceil_cents,
                status="active",
            ),
            actor=admin,
        )

    active_emps = [
        ("EMP-CFO", "DEPT-HQ", "POS-CFO", "陈总", "act-cfo", "active"),
        ("EMP-HR-ADMIN", "DEPT-HQ", "POS-HR-ADMIN", "林人事", "act-hr-admin", "active"),
        ("EMP-HR-DIR", "DEPT-HQ", "POS-HR-DIR", "苏人力", "act-hr-dir", "active"),
        ("EMP-PLANNER", "DEPT-NORTH-MFG", "POS-PLANNER", "周计划", "act-planner", "active"),
        ("EMP-NORTH-PM", "DEPT-NORTH-MFG", "POS-PM-NORTH", "张厂长", "act-north-pm", "active"),
        ("EMP-NORTH-SF", "DEPT-NORTH-MFG", "POS-SF-NORTH", "赵师傅", "act-north-sf", "active"),
        ("EMP-SOUTH-SF", "DEPT-SOUTH-MFG", "POS-SF-SOUTH", "钱师傅", "act-south-sf", "active"),
        ("EMP-SALES", "DEPT-SALES", "POS-SALES-REP", "孙销售", "act-sales", "active"),
        ("EMP-SUPPORT", "DEPT-SALES", "POS-SUPPORT", "吴客服", "act-support", "active"),
        ("EMP-PROC-MGR", "DEPT-PROC", "POS-PROC-MGR", "郑采购经理", "act-proc-mgr", "active"),
        ("EMP-WH", "DEPT-NORTH-MFG", "POS-WH", "冯仓管", "act-wh", "active"),
        ("EMP-FIN", "DEPT-FIN", "POS-FIN", "韩会计", "act-fin", "active"),
        ("EMP-CA", "DEPT-FIN", "POS-CA", "褚主管", "act-ca", "active"),
        # Archived-ancestor employee & onboarding employee (later activated)
        ("EMP-ARCH", "DEPT-ARCH-CHILD", "POS-SF-NORTH", "张归档", "act-arch", "active"),
        ("EMP-TEMP-BUYER", "DEPT-PROC", "POS-BUYER", "蒋临时采购", "act-temp-buyer", "onboarding"),
    ]
    for eid, did, pid, name, act_id, st in active_emps:
        rt.put_object(
            EmpCls(
                employee_id=eid,
                department=did,
                position=pid,
                full_name=name,
                actor_id=act_id,
                status=st,
            ),
            actor=admin,
        )


def test_1_assembler_subset_full_composition_and_spec_parity() -> None:
    assert ENTERPRISE_DOMAIN_KEYS == ("crm", "o2c", "p2p", "p2m", "r2r", "h2r")

    full_bp = build_enterprise_standard_blueprint()
    full_compiled = compile_blueprint(full_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    full_spec = build_enterprise_standard_spec()

    assert set(full_spec.roles.keys()) == {r for r, _ in STD_ROLES} == set(full_compiled.roles.keys())
    assert len(full_spec.object_types) == 29
    assert len(full_spec.actions) == 31
    assert set(full_spec.object_types.keys()) == set(full_compiled.object_types.keys())
    assert set(full_spec.actions.keys()) == set(full_compiled.actions.keys())
    assert {
        "diagnose_work_order_conservation",
        "compute_trial_balance",
        "resolve_employee_effective_mandates",
    } <= set(full_spec.functions.keys())
    assert "std_enterprise_world_model" in (full_spec.display.get("ontologies") or {})

    # Subset assembly keeps core + only selected domains
    sub_spec = build_enterprise_standard_spec(domains=["std_p2p_ontology", "p2m", "r2r"])
    sub_bp = build_enterprise_standard_blueprint(domains=("p2p", "p2m", "r2r"))
    assert "P2pPurchaseOrder" in sub_spec.object_types
    assert "WorkOrder" in sub_spec.object_types
    assert "JournalEntry" in sub_spec.object_types
    assert "CrmDeal" not in sub_spec.object_types
    assert "O2cOrder" not in sub_spec.object_types
    assert "Employee" not in sub_spec.object_types
    assert set(sub_spec.object_types.keys()) == {ot.name for ot in sub_bp.object_types}

    with pytest.raises(InvariantError) as e_unk:
        build_enterprise_standard_spec(domains=("crm", "nonexistent_domain"))
    assert e_unk.value.code == "UNKNOWN_ENTERPRISE_DOMAIN"

    rt = open_standalone_enterprise_world(":memory:")
    try:
        assert rt.world_mode == "standalone"
    finally:
        rt.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_2_standalone_six_domain_full_enterprise_closed_loop_and_fork(backend: str) -> None:
    admin = Actor(actor_id="adm-ent", role="admin", clearance_tags={"*"})
    with _open_enterprise_world(backend) as rt:
        assert rt.world_mode == "standalone"
        _seed_common_hr_and_accounting_world(rt, admin)

        # ------------------------------------------------------------------
        # Step 1: H2R Org Tree + Mandate + M2 org_authority Projection & RLS
        # ------------------------------------------------------------------
        hr_admin = Actor(actor_id="act-hr-admin", role="hr_admin")
        hr_dir = Actor(actor_id="act-hr-dir", role="hr_director")
        proc_mgr = Actor(actor_id="act-proc-mgr", role="procurement_manager")

        # Activate EMP-TEMP-BUYER -> active
        rt.submit_action(
            "S1-ACTIVATE-BUYER",
            "assign_employee_position",
            {
                "employee_id": "EMP-TEMP-BUYER",
                "department_id": "DEPT-PROC",
                "position_id": "POS-BUYER",
                "activate": True,
            },
            hr_admin,
        )
        assert rt.get_object(Employee, "EMP-TEMP-BUYER", hr_admin).status == "active"

        # EMP-PROC-MGR grants MND-PROC-150K (¥150,000 = 15,000,000 cents) to EMP-TEMP-BUYER
        rt.submit_action(
            "S1-GRANT-MND",
            "grant_authority_mandate",
            {
                "mandate_id": "MND-PROC-150K",
                "grantor_id": "EMP-PROC-MGR",
                "grantee_id": "EMP-TEMP-BUYER",
                "role_name": "procurement_manager",
                "ceiling_cents": 15_000_000,
                "scope_tag": "org:proc",
            },
            proc_mgr,
        )

        tokens = {
            "act-proc-mgr": "tok-proc-mgr-secret",
            "act-cfo": "tok-cfo-secret",
        }
        directory = project_h2r_to_org_directory(
            rt,
            admin,
            token_hashes={k: hash_auth_token(v) for k, v in tokens.items()},
        )
        assert isinstance(directory, OrgDirectory)
        unit_ids = {u.unit_id for u in directory.list_units()}
        assert "DEPT-OLD-ARCH" not in unit_ids
        assert "DEPT-ARCH-CHILD" not in unit_ids
        with pytest.raises(OrgAuthorityError) as e_arch_emp:
            directory.get_member("act-arch")
        assert e_arch_emp.value.code == ORG_MEMBER_NOT_FOUND
        assert {"DEPT-HQ", "DEPT-NORTH-MFG", "DEPT-SOUTH-MFG", "DEPT-PROC"} <= unit_ids

        north_sf_actor = directory.resolve_actor("act-north-sf")
        cfo_actor = directory.resolve_actor("act-cfo")
        assert north_sf_actor.clearance_tags == {"org:plant_north"}
        assert {"org:hq", "org:plant_north", "org:plant_south", "org:proc", "org:fin"} <= cfo_actor.clearance_tags
        # Token authentication works against projected OrgDirectory
        assert directory.authenticate_token("tok-proc-mgr-secret").actor_id == "act-proc-mgr"

        # Cross-plant scope isolation check
        with pytest.raises(OrgAuthorityError) as e_scope:
            evaluate_h2r_employee_authority(
                rt,
                admin,
                employee_or_actor_id="EMP-NORTH-SF",
                target_unit_id="DEPT-SOUTH-MFG",
                amount_cents=100_000,
                directory=directory,
            )
        assert e_scope.value.code == AUTHORITY_ORG_SCOPE_DENIED

        # Dynamic mandate boosts EMP-TEMP-BUYER's procurement_manager limit from ¥0 (base role=buyer) to ¥150,000
        d_auto = evaluate_h2r_employee_authority(
            rt,
            admin,
            employee_or_actor_id="EMP-TEMP-BUYER",
            target_unit_id="DEPT-PROC",
            amount_cents=12_000_000,  # ¥120,000 <= ¥150,000
            required_role="procurement_manager",
            directory=directory,
        )
        assert d_auto.decision == "AUTO"
        assert d_auto.effective_ceiling_cents == 15_000_000

        proc_ladder = ((20_000_000, "procurement_manager"), (None, "finance_director"))
        d_esc = evaluate_h2r_employee_authority(
            rt,
            admin,
            employee_or_actor_id="EMP-TEMP-BUYER",
            target_unit_id="DEPT-PROC",
            amount_cents=25_000_000,  # ¥250,000 > ¥150,000 & > procurement_manager ¥200,000 -> CFO
            required_role="procurement_manager",
            escalation_ladder_cents=proc_ladder,
            directory=directory,
        )
        assert d_esc.decision == "ESCALATE"
        assert d_esc.tier_evaluation.eligible_approver_ids == ["act-cfo"]

        policy_for_check = build_h2r_tiered_authority_policy(
            rt,
            admin,
            auto_ceiling_cents=d_esc.effective_ceiling_cents,
            escalation_ladder_cents=proc_ladder,
        )
        auto_policy = build_h2r_tiered_authority_policy(
            rt, admin, auto_ceiling_cents=d_esc.effective_ceiling_cents
        )
        assert auto_policy.resolve_tier(1_200_000.0).required_approver_role == "finance_director"
        # SoD self-approval rejected
        with pytest.raises(OrgAuthorityError) as e_sod_m2:
            verify_h2r_approver_authority(
                rt,
                admin,
                approver_employee_or_actor_id="EMP-TEMP-BUYER",
                requester_employee_or_actor_id="EMP-TEMP-BUYER",
                target_unit_id="DEPT-PROC",
                amount_cents=25_000_000,
                policy=policy_for_check,
                directory=directory,
            )
        assert e_sod_m2.value.code == SOD_SELF_APPROVAL_FORBIDDEN
        # Cross-plant manager (North plant covering only DEPT-NORTH-MFG) rejected for DEPT-PROC
        with pytest.raises(OrgAuthorityError) as e_cross_pm:
            verify_h2r_approver_authority(
                rt,
                admin,
                approver_employee_or_actor_id="EMP-NORTH-PM",
                requester_employee_or_actor_id="EMP-TEMP-BUYER",
                target_unit_id="DEPT-PROC",
                amount_cents=25_000_000,
                policy=policy_for_check,
                directory=directory,
            )
        assert e_cross_pm.value.code == AUTHORITY_ORG_SCOPE_DENIED
        # EMP-PROC-MGR ceiling ¥200,000 < ¥250,000 & role_rank < finance_director -> AUTHORITY_TIER_INSUFFICIENT
        with pytest.raises(OrgAuthorityError) as e_ceil_m2:
            verify_h2r_approver_authority(
                rt,
                admin,
                approver_employee_or_actor_id="EMP-PROC-MGR",
                requester_employee_or_actor_id="EMP-TEMP-BUYER",
                target_unit_id="DEPT-PROC",
                amount_cents=25_000_000,
                policy=policy_for_check,
                directory=directory,
            )
        assert e_ceil_m2.value.code == AUTHORITY_TIER_INSUFFICIENT
        # EMP-CFO (DEPT-HQ, ceiling ¥5,000,000) approves successfully
        approved_tier = verify_h2r_approver_authority(
            rt,
            admin,
            approver_employee_or_actor_id="EMP-CFO",
            requester_employee_or_actor_id="EMP-TEMP-BUYER",
            target_unit_id="DEPT-PROC",
            amount_cents=25_000_000,
            policy=policy_for_check,
            directory=directory,
        )
        assert approved_tier.required_approver_role == "finance_director"

        # Offboard EMP-TEMP-BUYER -> atomic mandate revocation & immediate loss of M2 OrgDirectory access
        rt.submit_action("S1-OFFBOARD-TEMP", "offboard_employee", {"employee_id": "EMP-TEMP-BUYER"}, hr_dir)
        assert rt.get_object(AuthorityMandate, "MND-PROC-150K", admin).status == "revoked"
        dir_after_offboard = project_h2r_to_org_directory(rt, admin)
        with pytest.raises(OrgAuthorityError) as e_missing:
            dir_after_offboard.get_member("act-temp-buyer")
        assert e_missing.value.code == ORG_MEMBER_NOT_FOUND
        with pytest.raises(OrgAuthorityError) as e_inact:
            evaluate_h2r_employee_authority(
                rt,
                admin,
                employee_or_actor_id="EMP-TEMP-BUYER",
                target_unit_id="DEPT-PROC",
                amount_cents=100_000,
                directory=dir_after_offboard,
            )
        assert e_inact.value.code == ORG_MEMBER_INACTIVE

        # Resolve live M2 Actors from projected OrgDirectory for business loop steps
        sales_actor = directory.resolve_actor("act-sales")
        support_actor = directory.resolve_actor("act-support")
        proc_mgr_actor = directory.resolve_actor("act-proc-mgr")
        wh_actor = directory.resolve_actor("act-wh")
        planner_actor = directory.resolve_actor("act-planner")
        sf_actor = directory.resolve_actor("act-north-sf")
        pm_actor = directory.resolve_actor("act-north-pm")
        fin_actor = directory.resolve_actor("act-fin")
        ca_actor = directory.resolve_actor("act-ca")

        # ------------------------------------------------------------------
        # Step 2: L2O (std_crm) 获客与签约
        # ------------------------------------------------------------------
        PartyCls = rt.spec.get_object_type("BusinessParty")
        ResCls = rt.spec.get_object_type("TradeResource")
        CommCls = rt.spec.get_object_type("TradeCommitment")
        EvtCls = rt.spec.get_object_type("FulfillmentEvent")
        AccCls = rt.spec.get_object_type("CrmAccount")
        DealCls = rt.spec.get_object_type("CrmDeal")
        TicketCls = rt.spec.get_object_type("CrmTicket")

        rt.put_object(PartyCls(party_id="PTY-CORP", legal_name="华实智能装备股份有限公司"), actor=admin)
        rt.put_object(PartyCls(party_id="PTY-SUP-STEEL", legal_name="鞍山精品特钢有限公司"), actor=admin)
        rt.put_object(PartyCls(party_id="PTY-CUST-EV", legal_name="华南新能源汽车有限公司"), actor=admin)
        rt.put_object(ResCls(resource_id="RES-COIL", code="STEEL-Q235", name="汽车级冷轧钢卷", unit="TON"), actor=admin)
        rt.put_object(ResCls(resource_id="RES-CHASSIS", code="EV-CHS-800", name="新能源一体压铸车架", unit="EA"), actor=admin)

        rt.put_object(AccCls(account_id="ACC-EV", name="华南新能源", party="PTY-CUST-EV", industry="EV"), actor=admin)
        rt.put_object(
            DealCls(deal_id="DEAL-PREM", title="早期接触试单", account="ACC-EV", amount=50_000.0, stage="PROSPECTING"),
            actor=admin,
        )
        with pytest.raises(InvariantError) as e_prem_win:
            rt.submit_action("S2-RED-WIN", "win_crm_deal", {"deal_id": "DEAL-PREM"}, sales_actor)
        assert e_prem_win.value.code == "INVALID_DEAL_STAGE"
        rt.put_object(
            DealCls(deal_id="DEAL-EV-1", title="50套车架总成年度采购", account="ACC-EV", amount=240_000.0, stage="PROPOSAL"),
            actor=admin,
        )
        rt.put_object(
            TicketCls(ticket_id="TK-EV-1", subject="焊点探伤标准确认", account="ACC-EV", priority="HIGH", status="OPEN"),
            actor=admin,
        )
        rt.submit_action(
            "S2-SOLVE-TK",
            "resolve_crm_ticket",
            {"ticket_id": "TK-EV-1", "resolution_summary": "完成超声波探伤技术签认"},
            support_actor,
        )
        rt.submit_action("S2-WIN-DEAL", "win_crm_deal", {"deal_id": "DEAL-EV-1"}, sales_actor)
        assert rt.get_object("CrmDeal", "DEAL-EV-1", sales_actor).stage == "CLOSED_WON"

        rt.put_object(
            CommCls(
                commitment_id="CM-SALE-EV",
                buyer_party="PTY-CUST-EV",
                seller_party="PTY-CORP",
                resource="RES-CHASSIS",
                quantity=50.0,
                unit_price=4800.0,
                total_amount=240_000.0,
                status="agreed",
            ),
            actor=admin,
        )

        # ------------------------------------------------------------------
        # Step 3: P2P (std_p2p) 采购入料与短磅争议（100t @ ¥1000 -> 实收 95t）
        # ------------------------------------------------------------------
        SupCls = rt.spec.get_object_type("P2pSupplierProfile")
        PoCls = rt.spec.get_object_type("P2pPurchaseOrder")
        GrCls = rt.spec.get_object_type("P2pGoodsReceipt")
        BillCls = rt.spec.get_object_type("P2pVendorBill")

        rt.put_object(
            CommCls(
                commitment_id="CM-PO-STEEL",
                buyer_party="PTY-CORP",
                seller_party="PTY-SUP-STEEL",
                resource="RES-COIL",
                quantity=100.0,
                unit_price=1000.0,
                total_amount=100_000.0,
                status="agreed",
            ),
            actor=admin,
        )
        rt.put_object(SupCls(profile_id="SUP-STEEL", party="PTY-SUP-STEEL", accounting_entity="ENT-GROUP"), actor=admin)
        rt.put_object(
            PoCls(
                po_id="PO-100",
                po_number="PO-2026-100",
                supplier="SUP-STEEL",
                accounting_entity="ENT-GROUP",
                ordered_qty=100.0,
                unit_price=1000.0,
                amount_total=100_000.0,
                status="draft",
            ),
            actor=admin,
        )
        rt.put_object(
            GrCls(receipt_id="GR-100", purchase_order="PO-100", expected_qty=100.0, status="pending"),
            actor=admin,
        )
        rt.put_object(
            BillCls(
                bill_id="BILL-100",
                purchase_order="PO-100",
                amount_total=100_000.0,
                amount_residual=100_000.0,
                status="draft",
            ),
            actor=admin,
        )
        rt.submit_action("S3-CONF-PO", "confirm_p2p_order", {"po_id": "PO-100"}, proc_mgr_actor)
        rt.submit_action(
            "S3-RCV-SHORT",
            "record_p2p_receipt",
            {"receipt_id": "GR-100", "received_qty": 95.0, "inspection_status": "passed"},
            wh_actor,
        )
        assert rt.get_object(P2pPurchaseOrder, "PO-100", wh_actor).status == "disputed"
        assert rt.get_object(P2pGoodsReceipt, "GR-100", wh_actor).status == "disputed"

        # Disputed PO blocks vendor bill posting
        with pytest.raises(InvariantError) as e_p2p_disp:
            rt.submit_action("S3-RED-BILL", "post_p2p_bill", {"bill_id": "BILL-100"}, fin_actor)
        assert e_p2p_disp.value.code == "PO_DISPUTED_BLOCKED"

        # WorkCard dispute resolution by procurement_manager
        rt.submit_action(
            "S3-OPEN-WC",
            "open_work_card",
            {
                "card_id": "WC-PO-100-DISP",
                "card_kind": "HUMAN_TASK",
                "target_object_type": "P2pPurchaseOrder",
                "target_object_id": "PO-100",
                "requested_action": "resolve_p2p_dispute",
                "assignee_role": "procurement_manager",
            },
            wh_actor,
        )
        rt.submit_action(
            "S3-COMP-WC",
            "complete_work_card",
            {"card_id": "WC-PO-100-DISP", "resolution_note": "磅单核对实收 95 吨，按 95,000 元结算"},
            proc_mgr_actor,
        )
        rt.submit_action(
            "S3-RESOLVE-DISP",
            "resolve_p2p_dispute",
            {
                "po_id": "PO-100",
                "receipt_id": "GR-100",
                "work_card_id": "WC-PO-100-DISP",
                "settled_amount": 95_000.0,
            },
            proc_mgr_actor,
        )
        rt.submit_action("S3-RECON-BILL", "reconcile_p2p_bill", {"bill_id": "BILL-100"}, fin_actor)
        rt.submit_action("S3-POST-BILL", "post_p2p_bill", {"bill_id": "BILL-100"}, fin_actor)
        rt.submit_action("S3-PAY-BILL", "record_p2p_payment", {"bill_id": "BILL-100", "payment_amount": 95_000.0}, fin_actor)
        assert rt.get_object(P2pVendorBill, "BILL-100", fin_actor).payment_state == "paid"

        # Emit confirmed P2P events
        rt.put_object(
            EvtCls(
                event_id="EVT-P2P-RCV",
                commitment="CM-PO-STEEL",
                event_kind="RECEIPT",
                quantity=95.0,
                amount=95_000.0,
                status="confirmed",
            ),
            actor=admin,
        )
        rt.put_object(
            EvtCls(
                event_id="EVT-P2P-PAY",
                commitment="CM-PO-STEEL",
                event_kind="PAYMENT",
                quantity=1.0,
                amount=95_000.0,
                status="confirmed",
            ),
            actor=admin,
        )

        # ------------------------------------------------------------------
        # Step 4: P2M (std_p2m) 投料生产与成本守恒（80t = ¥80,000 -> 50 台车架）
        # ------------------------------------------------------------------
        BomCls = rt.spec.get_object_type("BomSpec")
        WoCls = rt.spec.get_object_type("WorkOrder")
        rt.put_object(
            BomCls(
                bom_id="BOM-CHASSIS-V1",
                output_resource="RES-CHASSIS",
                input_resource="RES-COIL",
                input_per_unit=1.5,
                max_scrap_ratio=0.05,
                version="1.0",
                status="active",
            ),
            actor=admin,
        )
        rt.put_object(
            WoCls(
                work_order_id="WO-200",
                accounting_entity="ENT-GROUP",
                bom="BOM-CHASSIS-V1",
                commitment="CM-SALE-EV",
                output_resource="RES-CHASSIS",
                planned_qty=50.0,
                status="draft",
            ),
            actor=admin,
        )
        rt.put_object(
            EvtCls(
                event_id="EVT-P2M-ISS",
                commitment="CM-SALE-EV",
                event_kind="PRODUCTION_ISSUE",
                quantity=80.0,
                amount=80_000.0,
                status="confirmed",
            ),
            actor=admin,
        )
        rt.submit_action("S4-REL-WO", "release_work_order", {"work_order_id": "WO-200"}, planner_actor)
        rt.submit_action(
            "S4-ISS-MAT",
            "issue_material_to_work_order",
            {
                "consumption_id": "CONS-200-1",
                "work_order_id": "WO-200",
                "input_resource": "RES-COIL",
                "qty": 80.0,
                "unit_cost_cents": 100_000,
                "allocation_rule": "FIFO",
                "source_event": "EVT-P2M-ISS",
            },
            wh_actor,
        )
        # Output batch 1: 40 good, 0 scrap, absorb 6,000,000 cents
        rt.submit_action(
            "S4-OUT-1",
            "record_production_output",
            {
                "output_id": "OUT-200-1",
                "work_order_id": "WO-200",
                "output_resource": "RES-CHASSIS",
                "good_qty": 40.0,
                "scrap_qty": 0.0,
                "absorbed_cost_cents": 6_000_000,
            },
            sf_actor,
        )
        # Output batch 2: 10 good + 5 scrap => cumulative scrap ratio 5/55 = 9.09% > 5% -> WorkCard + plant_manager t1
        esc_p2m = submit_or_open_work_card(
            rt,
            "S4-OUT-2-REQ",
            "record_production_output",
            {
                "output_id": "OUT-200-2",
                "work_order_id": "WO-200",
                "output_resource": "RES-CHASSIS",
                "good_qty": 10.0,
                "scrap_qty": 5.0,
                "absorbed_cost_cents": 2_000_000,
            },
            sf_actor,
            target_object_type="ProductionOutput",
            target_object_id="OUT-200-2",
            assignee_role="plant_manager",
            card_id="WC-P2M-SCRAP-200",
        )
        assert esc_p2m["status"] == "AWAITING_WORK_CARD"
        assert esc_p2m["error_code"] == "APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED"
        settled_p2m = settle_work_card(rt, "WC-P2M-SCRAP-200", pm_actor, resolution_note="压铸首件调模废品核销通过")
        assert settled_p2m["settled_action_run"]["status"] == "COMMITTED"

        diag = rt.call_function("diagnose_work_order_conservation", {"work_order_id": "WO-200"})
        assert diag["all_cost_conserved"] is True
        wo_diag = diag["work_orders"][0]
        assert wo_diag["issued_cost_cents"] == 8_000_000
        assert wo_diag["absorbed_cost_cents"] == 8_000_000
        assert wo_diag["remaining_unabsorbed_cost_cents"] == 0

        rt.submit_action("S4-CLOSE-WO", "close_work_order", {"work_order_id": "WO-200"}, planner_actor)
        assert rt.get_object(WorkOrder, "WO-200", planner_actor).status == "completed"
        rt.put_object(
            EvtCls(
                event_id="EVT-P2M-FG",
                commitment="CM-SALE-EV",
                event_kind="PRODUCTION_OUTPUT",
                quantity=50.0,
                amount=80_000.0,
                status="confirmed",
            ),
            actor=admin,
        )

        # ------------------------------------------------------------------
        # Step 5: O2C (std_o2c) 销售履约与全额回款（¥240,000）
        # ------------------------------------------------------------------
        CustProfCls = rt.spec.get_object_type("O2cCustomerProfile")
        SoCls = rt.spec.get_object_type("O2cOrder")
        DelCls = rt.spec.get_object_type("O2cDelivery")
        InvCls = rt.spec.get_object_type("O2cInvoice")

        rt.put_object(
            CustProfCls(profile_id="CUST-EV", party="PTY-CUST-EV", accounting_entity="ENT-GROUP", credit_limit=1_000_000.0),
            actor=admin,
        )
        rt.put_object(
            SoCls(
                order_id="SO-300",
                order_number="SO-2026-300",
                customer="CUST-EV",
                accounting_entity="ENT-GROUP",
                amount_total=240_000.0,
                status="draft",
            ),
            actor=admin,
        )
        rt.put_object(DelCls(delivery_id="DEL-300", order="SO-300", status="pending"), actor=admin)
        rt.put_object(
            InvCls(
                invoice_id="INV-300",
                order="SO-300",
                amount_total=240_000.0,
                amount_residual=240_000.0,
                status="draft",
            ),
            actor=admin,
        )
        # Red check: cannot invoice draft/unconfirmed order
        with pytest.raises(InvariantError) as e_o2c_bill:
            rt.submit_action("S5-RED-INV", "post_o2c_invoice", {"invoice_id": "INV-300"}, fin_actor)
        assert e_o2c_bill.value.code == "ORDER_NOT_BILLABLE"

        rt.submit_action("S5-CONF-SO", "confirm_o2c_order", {"order_id": "SO-300"}, sales_actor)
        rt.submit_action("S5-DELIV-SO", "complete_o2c_delivery", {"delivery_id": "DEL-300"}, wh_actor)
        rt.submit_action("S5-POST-INV", "post_o2c_invoice", {"invoice_id": "INV-300"}, fin_actor)
        rt.submit_action("S5-PAY-INV", "record_o2c_payment", {"invoice_id": "INV-300", "payment_amount": 240_000.0}, fin_actor)
        assert rt.get_object(O2cInvoice, "INV-300", fin_actor).payment_state == "paid"

        for eid, kind, qty, amt in (
            ("EVT-O2C-SHIP", "SHIPMENT", 50.0, 80_000.0),
            ("EVT-O2C-INV", "INVOICE", 50.0, 240_000.0),
            ("EVT-O2C-PAY", "PAYMENT", 1.0, 240_000.0),
        ):
            rt.put_object(
                EvtCls(
                    event_id=eid,
                    commitment="CM-SALE-EV",
                    event_kind=kind,
                    quantity=qty,
                    amount=amt,
                    status="confirmed",
                ),
                actor=admin,
            )

        # ------------------------------------------------------------------
        # Step 6: R2R (std_r2r) 总账过账、fork() 沙盘、试算平衡与关账封口
        # ------------------------------------------------------------------
        PerCls = rt.spec.get_object_type("FiscalPeriod")
        AccCls = rt.spec.get_object_type("GlAccount")

        rt.put_object(
            PerCls(
                period_id="2026-11",
                accounting_entity="ENT-GROUP",
                start_date="2026-11-01",
                end_date="2026-11-30",
                status="open",
            ),
            actor=admin,
        )
        gl_seed = [
            ("1001", "银行存款", "ASSET", "DEBIT"),
            ("1122", "应收账款", "ASSET", "DEBIT"),
            ("1403", "原材料-钢卷", "ASSET", "DEBIT"),
            ("1404", "生产成本-WIP", "ASSET", "DEBIT"),
            ("1405", "库存商品-车架", "ASSET", "DEBIT"),
            ("2202", "应付账款", "LIABILITY", "CREDIT"),
            ("4001", "实收资本", "EQUITY", "CREDIT"),
            ("6001", "主营业务收入", "REVENUE", "CREDIT"),
            ("6401", "主营业务成本", "EXPENSE", "DEBIT"),
        ]
        for code, name, cat, side in gl_seed:
            rt.put_object(
                AccCls(
                    account_code=code,
                    accounting_entity="ENT-GROUP",
                    name=name,
                    category=cat,
                    normal_side=side,
                    status="active",
                ),
                actor=admin,
            )
        rt.put_object(
            EvtCls(
                event_id="EVT-CAP-1",
                commitment="CM-SALE-EV",
                event_kind="JOURNAL_POST",
                quantity=1.0,
                amount=500_000.0,
                status="confirmed",
            ),
            actor=admin,
        )
        rt.put_object(
            EvtCls(
                event_id="EVT-REC-UNCONF",
                commitment="CM-SALE-EV",
                event_kind="JOURNAL_POST",
                quantity=1.0,
                amount=10_000.0,
                status="recorded",
            ),
            actor=admin,
        )
        with pytest.raises(InvariantError) as e_proj_unconf:
            project_event_to_draft_journal(
                rt,
                admin,
                entry_id="JE-BAD-UNCONF",
                accounting_entity="ENT-GROUP",
                fiscal_period="2026-11",
                source_event_id="EVT-REC-UNCONF",
                debit_account="1001",
                credit_account="4001",
            )
        assert e_proj_unconf.value.code == "SOURCE_EVENT_NOT_CONFIRMED"
        with pytest.raises(InvariantError) as e_proj_over:
            project_event_to_draft_journal(
                rt,
                admin,
                entry_id="JE-BAD-OVER",
                accounting_entity="ENT-GROUP",
                fiscal_period="2026-11",
                source_event_id="EVT-CAP-1",
                debit_account="1001",
                credit_account="4001",
                amount_cents=50_000_001,
            )
        assert e_proj_over.value.code == "JOURNAL_EXCEEDS_SOURCE_EVENT"

        postings = [
            ("J-01", "JE-01-CAP", "EVT-CAP-1", "1001", "4001", 50_000_000, "股东货币出资 ¥500,000"),
            ("J-02", "JE-02-P2P-RCV", "EVT-P2P-RCV", "1403", "2202", 9_500_000, "采购入库 95t 钢卷 ¥95,000"),
            ("J-03", "JE-03-P2P-PAY", "EVT-P2P-PAY", "2202", "1001", 9_500_000, "支付钢材货款 ¥95,000"),
            ("J-04", "JE-04-P2M-ISS", "EVT-P2M-ISS", "1404", "1403", 8_000_000, "生产领料 80t 钢卷 ¥80,000"),
            ("J-05", "JE-05-P2M-FG", "EVT-P2M-FG", "1405", "1404", 8_000_000, "完工结转 50 台车架 ¥80,000"),
            ("J-06", "JE-06-O2C-REV", "EVT-O2C-INV", "1122", "6001", 24_000_000, "销售开票确认收入 ¥240,000"),
            ("J-07", "JE-07-O2C-COGS", "EVT-O2C-SHIP", "6401", "1405", 8_000_000, "出库结转主营成本 ¥80,000"),
            ("J-08", "JE-08-O2C-CASH", "EVT-O2C-PAY", "1001", "1122", 24_000_000, "客户回款核销 ¥240,000"),
        ]
        for run_id, eid, ev_id, dr, cr, cents, memo in postings:
            project_and_post_event_journal(
                rt,
                seed_actor=admin,
                post_actor=fin_actor,
                run_id=run_id,
                entry_id=eid,
                accounting_entity="ENT-GROUP",
                fiscal_period="2026-11",
                source_event_id=ev_id,
                debit_account=dr,
                credit_account=cr,
                amount_cents=cents,
                memo=memo,
            )

        # Counterfactual sandbox via runtime.fork() leaves primary SQLite/PG16 world completely untouched
        fp_before_fork = rt.state_fingerprint()
        fork_rt = rt.fork()
        try:
            assert fork_rt.world_mode == "counterfactual"
            JeForkCls = fork_rt.spec.get_object_type("JournalEntry")
            fork_rt.put_object(
                JeForkCls(
                    entry_id="JE-FORK-LOSS",
                    accounting_entity="ENT-GROUP",
                    fiscal_period="2026-11",
                    debit_account="6401",
                    credit_account="1001",
                    amount_cents=2_000_000,
                    memo="沙盘假设：突发 ¥20,000 设备维修费",
                ),
                actor=admin,
            )
            with pytest.raises(InvariantError) as e_fork_man:
                fork_rt.submit_action("FORK-FAIL", "post_journal_entry", {"entry_id": "JE-FORK-LOSS"}, fin_actor)
            assert e_fork_man.value.code == "APPROVAL_REQUIRED_MANUAL_JOURNAL"
            fork_rt.submit_action("FORK-OK", "post_journal_entry", {"entry_id": "JE-FORK-LOSS"}, cfo_actor)
            tb_fork = fork_rt.call_function("compute_trial_balance", {"accounting_entity": "ENT-GROUP"})
            assert tb_fork["category_net_cents"]["EXPENSE"] == 10_000_000
            assert fork_rt.state_fingerprint() != fp_before_fork
        finally:
            fork_rt.close()
        assert rt.state_fingerprint() == fp_before_fork

        # Reconcile Trial Balance & Five Accounting Elements on live primary world
        tb = rt.call_function(
            "compute_trial_balance",
            {"accounting_entity": "ENT-GROUP", "fiscal_period": "2026-11"},
        )
        assert tb["entry_count"] == 8
        assert tb["total_debit_cents"] == tb["total_credit_cents"] == 141_000_000
        assert tb["is_balanced"] is True
        assert tb["accounting_equation_holds"] is True
        assert tb["category_net_cents"] == {
            "ASSET": 66_000_000,
            "EXPENSE": 8_000_000,
            "LIABILITY": 0,
            "EQUITY": 50_000_000,
            "REVENUE": 24_000_000,
        }
        assert tb["asset_plus_expense_cents"] == tb["liability_equity_revenue_cents"] == 74_000_000
        # Real business balances check
        acc_cash = rt.get_object("GlAccount", "1001", fin_actor)
        acc_raw = rt.get_object("GlAccount", "1403", fin_actor)
        acc_wip = rt.get_object("GlAccount", "1404", fin_actor)
        acc_fg = rt.get_object("GlAccount", "1405", fin_actor)
        assert acc_cash.debit_total_cents - acc_cash.credit_total_cents == 64_500_000
        assert acc_raw.debit_total_cents - acc_raw.credit_total_cents == 1_500_000
        assert acc_wip.debit_total_cents - acc_wip.credit_total_cents == 0
        assert acc_fg.debit_total_cents - acc_fg.credit_total_cents == 0

        # Closing & immutability lock
        rt.submit_action("S6-START-CLS", "start_fiscal_period_closing", {"period_id": "2026-11"}, ca_actor)
        assert rt.get_object(FiscalPeriod, "2026-11", ca_actor).status == "closing"
        rt.submit_action(
            "S6-CLOSE-CLS",
            "close_fiscal_period",
            {"period_id": "2026-11", "snapshot_id": "TBS-2026-11"},
            ca_actor,
        )
        assert rt.get_object(FiscalPeriod, "2026-11", ca_actor).status == "closed"
        snap = rt.get_object(TrialBalanceSnapshot, "TBS-2026-11", ca_actor)
        assert snap.is_balanced is True
        assert snap.total_debit_cents == snap.total_credit_cents == 141_000_000
        assert snap.entry_count == 8

        # Closed period rejects late posting & late reversal
        JeCls = rt.spec.get_object_type("JournalEntry")
        rt.put_object(
            JeCls(
                entry_id="JE-LATE-ATTEMPT",
                accounting_entity="ENT-GROUP",
                fiscal_period="2026-11",
                debit_account="6401",
                credit_account="1001",
                amount_cents=10_000,
            ),
            actor=admin,
        )
        with pytest.raises(InvariantError) as e_cls_post:
            rt.submit_action("S6-RED-POST", "post_journal_entry", {"entry_id": "JE-LATE-ATTEMPT"}, fin_actor)
        assert e_cls_post.value.code == "PERIOD_CLOSED_POSTING_FORBIDDEN"

        with pytest.raises(InvariantError) as e_cls_rev:
            rt.submit_action("S6-RED-REV", "reverse_journal_entry", {"entry_id": "JE-08-O2C-CASH"}, ca_actor)
        assert e_cls_rev.value.code == "PERIOD_CLOSED_POSTING_FORBIDDEN"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_3_org_rls_physical_row_isolation_from_h2r_projection(backend: str) -> None:
    admin = Actor(actor_id="adm-rls", role="admin", clearance_tags={"*"})
    rls_spec = build_org_rls_enterprise_spec()
    with _open_enterprise_world(backend, spec=rls_spec) as rt:
        _seed_common_hr_and_accounting_world(rt, admin)
        ResCls = rls_spec.get_object_type("TradeResource")
        BomCls = rls_spec.get_object_type("BomSpec")
        WoCls = rls_spec.get_object_type("WorkOrder")

        rt.put_object(ResCls(resource_id="RES-RAW", name="钢卷"), actor=admin)
        rt.put_object(ResCls(resource_id="RES-OUT", name="成品"), actor=admin)
        rt.put_object(
            BomCls(
                bom_id="BOM-RLS",
                output_resource="RES-OUT",
                input_resource="RES-RAW",
                input_per_unit=1.0,
                max_scrap_ratio=0.05,
            ),
            actor=admin,
        )
        rt.put_object(
            WoCls(
                work_order_id="WO-NORTH-1",
                accounting_entity="ENT-GROUP",
                bom="BOM-RLS",
                output_resource="RES-OUT",
                planned_qty=20.0,
                org="plant_north",
            ),
            actor=admin,
        )
        rt.put_object(
            WoCls(
                work_order_id="WO-SOUTH-1",
                accounting_entity="ENT-GROUP",
                bom="BOM-RLS",
                output_resource="RES-OUT",
                planned_qty=30.0,
                org="plant_south",
            ),
            actor=admin,
        )

        directory = project_h2r_to_org_directory(rt, admin)
        north_actor = directory.resolve_actor("act-north-sf")
        south_actor = directory.resolve_actor("act-south-sf")
        cfo_actor = directory.resolve_actor("act-cfo")

        north_rows = rt.query_objects("WorkOrder", north_actor)
        assert [row.work_order_id for row in north_rows] == ["WO-NORTH-1"]
        with pytest.raises(PermissionDeniedError):
            rt.get_object("WorkOrder", "WO-SOUTH-1", north_actor)

        south_rows = rt.query_objects("WorkOrder", south_actor)
        assert [row.work_order_id for row in south_rows] == ["WO-SOUTH-1"]
        with pytest.raises(PermissionDeniedError):
            rt.get_object("WorkOrder", "WO-NORTH-1", south_actor)

        cfo_rows = rt.query_objects("WorkOrder", cfo_actor)
        assert {row.work_order_id for row in cfo_rows} == {"WO-NORTH-1", "WO-SOUTH-1"}


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_4_customer_overlay_monotonic_extension_design_check_and_execution(
    backend: str,
    tmp_path: Path,
) -> None:
    std_fps_before = {
        "core": blueprint_fingerprint(build_std_core_blueprint()),
        "crm": blueprint_fingerprint(build_std_crm_blueprint()),
        "o2c": blueprint_fingerprint(build_std_o2c_blueprint()),
        "p2p": blueprint_fingerprint(build_std_p2p_blueprint()),
        "p2m": blueprint_fingerprint(build_std_p2m_blueprint()),
        "r2r": blueprint_fingerprint(build_std_r2r_blueprint()),
        "h2r": blueprint_fingerprint(build_std_h2r_blueprint()),
    }

    mfg_overlay = CustomerOverlaySpec(
        overlay_id="huashi_mfg_enterprise_overlay",
        base_model_id="std_enterprise_world_model",
        target_version="1.1.0",
        description="制造企业专属只增不破扩展：新增自有工艺路线/VIP 字段与两道门禁规则。",
        add_fields={
            "CrmAccount": [
                FieldSpec(name="vip_tier", field_type="str", default="STANDARD", owned=True),
            ],
            "WorkOrder": [
                FieldSpec(name="process_route_code", field_type="str", default="STD_ROUTE", owned=True),
            ],
        },
        add_action_preconditions={
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="overlay_no_open_high_ticket_win",
                    code="OPEN_HIGH_PRIORITY_TICKET_BLOCKS_WIN",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "target.account"}, "right": {"ref": "obj.account"}},
                                {"op": "eq", "left": {"ref": "target.priority"}, "right": {"lit": "HIGH"}},
                                {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                            ],
                        },
                    },
                )
            ],
            "post_o2c_invoice": [
                InvariantRuleSpec(
                    name="overlay_require_delivery_before_invoice",
                    code="DELIVERY_REQUIRED_BEFORE_INVOICE",
                    target_type="O2cInvoice",
                    id_param="invoice_id",
                    must_hold={
                        "op": "in",
                        "left": {"ref": "obj.order.status"},
                        "right": {"lit": ["delivered", "invoiced"]},
                    },
                )
            ],
        },
    )

    ov_path = tmp_path / "huashi_mfg_overlay.json"
    ov_path.write_text(json.dumps(mfg_overlay.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")

    # API + CLI open_ontology.design check must both pass with 0 errors
    chk = run_design_check(
        blueprints=["examples.std_enterprise_world_model:build_enterprise_standard_blueprint"],
        overlay=str(ov_path),
        seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
    )
    assert chk["ok"] is True, chk["errors"]

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_ontology.design",
            "check",
            "--blueprint",
            "examples.std_enterprise_world_model:build_enterprise_standard_blueprint",
            "--overlay",
            str(ov_path),
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout)["ok"] is True

    # Destructive overlay cannot bypass standard invariants
    bad_overlay = CustomerOverlaySpec(
        overlay_id="destructive_overlay",
        base_model_id="std_enterprise_world_model",
        remove_invariants=["r2r_journal_no_self_balancing"],
    )
    with pytest.raises(InvariantError) as e_bad_ov:
        build_enterprise_standard_blueprint(overlays=[bad_overlay])
    assert e_bad_ov.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"

    # Standard blueprints untouched
    assert std_fps_before == {
        "core": blueprint_fingerprint(build_std_core_blueprint()),
        "crm": blueprint_fingerprint(build_std_crm_blueprint()),
        "o2c": blueprint_fingerprint(build_std_o2c_blueprint()),
        "p2p": blueprint_fingerprint(build_std_p2p_blueprint()),
        "p2m": blueprint_fingerprint(build_std_p2m_blueprint()),
        "r2r": blueprint_fingerprint(build_std_r2r_blueprint()),
        "h2r": blueprint_fingerprint(build_std_h2r_blueprint()),
    }

    overlaid_bp = build_enterprise_standard_blueprint(overlays=[mfg_overlay])
    overlaid_spec = compile_blueprint(overlaid_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    admin = Actor(actor_id="adm-ov", role="admin", clearance_tags={"*"})
    sales = Actor(actor_id="sales-ov", role="sales_rep")
    support = Actor(actor_id="sup-ov", role="support_agent")
    wh = Actor(actor_id="wh-ov", role="warehouse")
    fin = Actor(actor_id="fin-ov", role="finance")

    with _open_enterprise_world(backend, spec=overlaid_spec) as rt:
        EntCls = overlaid_spec.get_object_type("AccountingEntity")
        PartyCls = overlaid_spec.get_object_type("BusinessParty")
        ResCls = overlaid_spec.get_object_type("TradeResource")
        BomCls = overlaid_spec.get_object_type("BomSpec")
        WoCls = overlaid_spec.get_object_type("WorkOrder")
        AccCls = overlaid_spec.get_object_type("CrmAccount")
        DealCls = overlaid_spec.get_object_type("CrmDeal")
        TicketCls = overlaid_spec.get_object_type("CrmTicket")
        CustProfCls = overlaid_spec.get_object_type("O2cCustomerProfile")
        SoCls = overlaid_spec.get_object_type("O2cOrder")
        DelCls = overlaid_spec.get_object_type("O2cDelivery")
        InvCls = overlaid_spec.get_object_type("O2cInvoice")

        rt.put_object(EntCls(entity_id="ENT-OV", name="覆盖测试主体"), actor=admin)
        rt.put_object(PartyCls(party_id="PTY-OV", legal_name="专精特新客户"), actor=admin)
        rt.put_object(ResCls(resource_id="RES-OV-RAW", name="钢板"), actor=admin)
        rt.put_object(ResCls(resource_id="RES-OV-FG", name="模具"), actor=admin)
        rt.put_object(BomCls(bom_id="BOM-OV", output_resource="RES-OV-FG", input_resource="RES-OV-RAW", input_per_unit=1.0), actor=admin)
        rt.put_object(
            WoCls(
                work_order_id="WO-OV-1",
                accounting_entity="ENT-OV",
                bom="BOM-OV",
                output_resource="RES-OV-FG",
                planned_qty=10.0,
                process_route_code="DIE_CAST_5AXIS",
            ),
            actor=admin,
        )
        assert rt.get_object("WorkOrder", "WO-OV-1", admin).process_route_code == "DIE_CAST_5AXIS"

        rt.put_object(AccCls(account_id="ACC-OV", name="精工VIP客户", party="PTY-OV", vip_tier="STRATEGIC"), actor=admin)
        assert rt.get_object("CrmAccount", "ACC-OV", admin).vip_tier == "STRATEGIC"
        rt.put_object(DealCls(deal_id="DEAL-OV", title="VIP专线订单", account="ACC-OV", amount=100_000.0, stage="PROPOSAL"), actor=admin)
        rt.put_object(TicketCls(ticket_id="TK-OV", subject="首件尺寸复测", account="ACC-OV", priority="HIGH", status="OPEN"), actor=admin)

        # Overlay gate 1: open HIGH ticket blocks win_crm_deal -> resolve ticket -> win succeeds
        with pytest.raises(InvariantError) as e_ov_win:
            rt.submit_action("OV-RED-WIN", "win_crm_deal", {"deal_id": "DEAL-OV"}, sales)
        assert e_ov_win.value.code == "OPEN_HIGH_PRIORITY_TICKET_BLOCKS_WIN"

        rt.submit_action("OV-SOLVE-TK", "resolve_crm_ticket", {"ticket_id": "TK-OV", "resolution_summary": "复测合格"}, support)
        rt.submit_action("OV-OK-WIN", "win_crm_deal", {"deal_id": "DEAL-OV"}, sales)
        assert rt.get_object("CrmDeal", "DEAL-OV", sales).stage == "CLOSED_WON"

        # Overlay gate 2: confirmed but unshipped O2cOrder blocks post_o2c_invoice -> deliver -> post succeeds
        rt.put_object(CustProfCls(profile_id="CUST-OV", party="PTY-OV", accounting_entity="ENT-OV"), actor=admin)
        rt.put_object(SoCls(order_id="SO-OV", order_number="SO-OV-1", customer="CUST-OV", accounting_entity="ENT-OV", amount_total=100_000.0), actor=admin)
        rt.put_object(DelCls(delivery_id="DEL-OV", order="SO-OV", status="pending"), actor=admin)
        rt.put_object(InvCls(invoice_id="INV-OV", order="SO-OV", amount_total=100_000.0, amount_residual=100_000.0), actor=admin)

        rt.submit_action("OV-CONF-SO", "confirm_o2c_order", {"order_id": "SO-OV"}, sales)
        with pytest.raises(InvariantError) as e_ov_inv:
            rt.submit_action("OV-RED-INV", "post_o2c_invoice", {"invoice_id": "INV-OV"}, fin)
        assert e_ov_inv.value.code == "DELIVERY_REQUIRED_BEFORE_INVOICE"

        rt.submit_action("OV-DELIV-SO", "complete_o2c_delivery", {"delivery_id": "DEL-OV"}, wh)
        rt.submit_action("OV-OK-INV", "post_o2c_invoice", {"invoice_id": "INV-OV"}, fin)
        assert rt.get_object("O2cInvoice", "INV-OV", fin).status == "posted"


def test_5_postgres16_zero_leaked_temporary_schemas() -> None:
    assert len(_CREATED_PG_SCHEMAS) >= 3, (
        "Expected live PG16 runs for standalone 6-domain loop, org RLS, and overlay E2E"
    )
    with psycopg.connect(PG_DSN) as conn:
        leaked = conn.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'wo24b_%'"
        ).fetchall()
        assert leaked == [], f"Live PG16 temporary schemas leaked: {leaked}"
