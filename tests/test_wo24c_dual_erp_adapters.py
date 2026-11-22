"""WO-24C Dual-ERP blind AI-FDE adapters offline & live hybrid world verification suite.

Covers:
1. Offline deterministic gate checks across all 6 persisted domain packages:
   - Odoo 19: ``o2c``, ``p2p``, ``r2r``
   - jshERP:  ``p2p``, ``o2c``, ``h2r``
   Including AST safety / <= 350 line budget, ``AdapterMappingSpec`` + ``run_design_check``,
   SHA-256 parity with blind-exam telemetry reports, monotonic ``CustomerOverlaySpec``
   API + CLI validation, destructive overlay rejection, and ``DeviceDriverRegistry``
   disjoint ownership / unplugged-device / duplicate-action contracts.
2. Live Docker Hybrid Enterprise World E2E suites:
   - **Odoo 19** (``localhost:8069``): coupled O2C/P2P/R2R + overlay blocks with zero
     dirty ERP writes + ``owned=True`` overlay fields + standalone CRM/P2M/H2R +
     ``runtime.fork()`` counterfactual isolation, inside a disposable ``wo24c_*`` DB
     dropped in ``finally``.
   - **jshERP** (``127.0.0.1:19999``): unified ``AccountingEntity`` across P2P/O2C/H2R,
     coupled P2P/O2C/H2R + overlay blocks + ontology-owned ``AuthorityMandate`` +
     ``revoke_authority_mandate`` -> coupled ``offboard_employee`` + ``project_h2r_to_org_directory`` +
     standalone CRM/P2M/R2R + ``runtime.fork()``, inside a disposable ``wo24c_*``
     tenant purged across MySQL + Redis in ``finally``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.adapters import (  # noqa: E402
    ADAPTER_ROOT,
    MountedErpWorld,
    load_customer_overlay,
    mount_jsherp_enterprise_world,
    mount_odoo19_enterprise_world,
)
from examples.business_ontology import BUSINESS_SEED_ONTOLOGY  # noqa: E402
from examples.std_enterprise_world_model import (  # noqa: E402
    build_enterprise_standard_blueprint,
    evaluate_h2r_employee_authority,
    project_event_to_draft_journal,
    project_h2r_to_org_directory,
)
from examples.wo24c_dual_erp_fde_orchestrator import (  # noqa: E402
    DOMAIN_SPECS,
    JSHERP_BASE_URL,
    ODOO_PASSWORD,
    ODOO_URL,
    ODOO_USER,
    JshErp24cAdminClient,
    Odoo24cAdminClient,
    _strip_odoo_demo_noise,
)
from open_ontology.design import _load_thin_driver_factory, audit_thin_driver_ast, run_design_check  # noqa: E402
from open_ontology.drivers import DUPLICATE_ACTION_DRIVER, MountedDevice  # noqa: E402
from open_ontology.induction import AdapterMappingSpec, blueprint_fingerprint  # noqa: E402
from open_ontology.settlement import UNBOUND_SOURCE_BRIDGE, SourceBridge  # noqa: E402
from open_ontology.spec import Actor, GovernanceViolationError, InvariantError  # noqa: E402

EXPECTED_SHA256: dict[tuple[str, str], tuple[str, str]] = {
    (
        "odoo19",
        "o2c",
    ): (
        "efcfdc366dad391670cab0bf6e1e6c70a9498d16a3672bfa01eb7e0359906bba",
        "97b238bcf3cdc506c800c916973a31821cb22bca190abee9dda48b03d1035bfa",
    ),
    (
        "odoo19",
        "p2p",
    ): (
        "14b801bdbeca9ba706a8af18c016ee71b80da2a2f4c59970afc9850ea2ab5df9",
        "55de12cd0cf9be76011f99ca67db0ff35a5e0ef42864b30e531e0a0d3e46f6f6",
    ),
    (
        "odoo19",
        "r2r",
    ): (
        "56722dbb1232fbb26f9fb0b713f85ecb1a69aeffb950476bfef2f8bb7165fea5",
        "9b3a413a36b00e548e509967a61875c6a93e270a1d5091aae9c62ec70e90a049",
    ),
    (
        "jsherp",
        "p2p",
    ): (
        "53d70a917d202a4c91ef961e4f9e1cce57782e8f676cd833015035776d9663ab",
        "0fdc16738d3139269402326adc2d6ccb3ac954b82ca7b2682dd2436ec95c3c2d",
    ),
    (
        "jsherp",
        "o2c",
    ): (
        "b992790e5ae36a344b75b4f3144e5707de4133a5ffda959f72ea64a0dec5d828",
        "e5ade56efef3854805715cbbac75571ef65151d4c09e5d4002dedafb6aceaed3",
    ),
    (
        "jsherp",
        "h2r",
    ): (
        "289e4622a5838b4148004cba40ab0178377dc1d5fe71eea394f1ebc75418655d",
        "018dfa7596a961e1c910c13920954a52b9156443d36f46a12b5f9df0535b9539",
    ),
}

DOMAIN_CASES = list(EXPECTED_SHA256.keys())


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping_prefixes(erp: str, domain: str) -> dict[str, str]:
    mapping = json.loads((ADAPTER_ROOT / erp / domain / "mapping.json").read_text("utf-8"))
    return {
        str(em["target_object_type"]): str((em.get("id_transform") or {}).get("prefix") or "")
        for em in mapping.get("entity_mappings") or ()
    }


# ---------------------------------------------------------------------------
# 1. Offline Deterministic Verification Suite (6 Domains + 2 Overlays + Mount)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("erp", "domain"), DOMAIN_CASES)
def test_1_persisted_blind_adapter_package_offline_contract(erp: str, domain: str) -> None:
    dom_dir = ADAPTER_ROOT / erp / domain
    driver_path = dom_dir / "driver.py"
    mapping_path = dom_dir / "mapping.json"
    assert driver_path.is_file(), f"Missing persisted driver: {driver_path}"
    assert mapping_path.is_file(), f"Missing persisted mapping: {mapping_path}"

    line_count = len(driver_path.read_text("utf-8").splitlines())
    assert line_count <= 350, f"{erp}/{domain} driver exceeded 350 lines: {line_count}"

    ast_issues = audit_thin_driver_ast(driver_path, max_lines=350)
    assert ast_issues == [], f"{erp}/{domain} AST audit issues: {ast_issues}"

    factory = _load_thin_driver_factory(driver_path)
    assert callable(factory)

    mapping_obj = AdapterMappingSpec.model_validate(json.loads(mapping_path.read_text("utf-8")))
    target = DOMAIN_SPECS[(erp, domain)]
    mapped_actions = {ab.action_name for ab in mapping_obj.action_bindings}
    assert set(target.handled_actions) <= mapped_actions

    chk = run_design_check(
        blueprints=[target.blueprint_arg],
        seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
    )
    assert chk["ok"] is True, chk["errors"]

    exp_drv_sha, exp_map_sha = EXPECTED_SHA256[(erp, domain)]
    actual_drv_sha = _sha256_file(driver_path)
    actual_map_sha = _sha256_file(mapping_path)
    assert actual_drv_sha == exp_drv_sha
    assert actual_map_sha == exp_map_sha

    report_path = Path(f"/tmp/wo24c_{erp}_fde_report.json")
    if report_path.is_file():
        rep = json.loads(report_path.read_text("utf-8"))
        dom_rep = rep["domains"][domain]
        assert actual_drv_sha == dom_rep["driver_sha256"]
        assert actual_map_sha == dom_rep["mapping_sha256"]
        assert dom_rep["ok"] is True
        assert dom_rep["world_a"]["verdict"] == "CORROBORATED"
        assert dom_rep["world_b_hidden"]["verdict"] == "CORROBORATED"
        assert dom_rep["oob_tamper"]["caught_error_code"] == "DEVICE_STATE_MISMATCH"
        assert dom_rep["oob_tamper"]["abox_unchanged"] is True


@pytest.mark.parametrize("erp", ["odoo19", "jsherp"])
def test_2_customer_overlay_api_cli_and_destructive_rejection(erp: str) -> None:
    overlay_path = ADAPTER_ROOT / erp / "overlay.json"
    assert overlay_path.is_file()

    base_fp_before = blueprint_fingerprint(build_enterprise_standard_blueprint())
    chk = run_design_check(
        blueprints=["examples.std_enterprise_world_model:build_enterprise_standard_blueprint"],
        overlay=str(overlay_path),
        seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
    )
    assert chk["ok"] is True, chk["errors"]
    assert blueprint_fingerprint(build_enterprise_standard_blueprint()) == base_fp_before

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_ontology.design",
            "check",
            "--blueprint",
            "examples.std_enterprise_world_model:build_enterprise_standard_blueprint",
            "--overlay",
            str(overlay_path),
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
        ],
        text=True,
        capture_output=True,
        check=False,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    cli_json = json.loads(proc.stdout)
    assert cli_json["ok"] is True
    assert cli_json["errors"] == []

    # Verify destructive mutations fail monotonically with OVERLAY_BREAKS_STANDARD_CONTRACT
    raw_overlay = json.loads(overlay_path.read_text("utf-8"))
    destructive = copy.deepcopy(raw_overlay)
    destructive["remove_fields"] = {"O2cOrder": ["order_number"]}
    with pytest.raises(InvariantError) as exc_info:
        build_enterprise_standard_blueprint(overlays=(destructive,))
    assert exc_info.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"

    weakened = copy.deepcopy(raw_overlay)
    weakened["update_actions"][0]["preconditions"] = [weakened["update_actions"][0]["preconditions"][0]]
    with pytest.raises(InvariantError) as weak_exc:
        build_enterprise_standard_blueprint(overlays=(weakened,))
    assert weak_exc.value.code == "OVERLAY_BREAKS_STANDARD_CONTRACT"


def test_3_hybrid_mount_contracts_and_registry_guards_offline() -> None:
    with mount_odoo19_enterprise_world(
        {"url": "http://localhost:8069/jsonrpc", "db": "odoo_demo", "username": ODOO_USER, "password": ODOO_PASSWORD},
        hydrate=False,
    ) as odoo_world:
        devices = odoo_world.registry.list_devices()
        assert [d["device_id"] for d in devices] == ["odoo19_o2c", "odoo19_p2p", "odoo19_r2r"]
        assert all(d["status"] == "MOUNTED" for d in devices)
        assert "odoo_sales_channel" in odoo_world.spec.get_object_type("O2cOrder").owned_field_defaults()
        assert "odoo_buyer_note" in odoo_world.spec.get_object_type("P2pPurchaseOrder").owned_field_defaults()
        assert "odoo_journal_code" in odoo_world.spec.get_object_type("JournalEntry").owned_field_defaults()
        for fn_name in ("diagnose_work_order_conservation", "compute_trial_balance", "resolve_employee_effective_mandates"):
            assert fn_name in odoo_world.spec.functions
        assert odoo_world.spec.get_object_type("CrmDeal").__owned__ is True
        assert odoo_world.spec.get_object_type("WorkOrder").__owned__ is True
        assert odoo_world.spec.get_object_type("Employee").__owned__ is True
        assert odoo_world.spec.get_object_type("TrialBalanceSnapshot").__owned__ is True
        assert odoo_world.spec.actions["win_crm_deal"].affects_source is False
        assert odoo_world.spec.actions["release_work_order"].affects_source is False
        assert odoo_world.spec.actions["grant_authority_mandate"].affects_source is False
        assert odoo_world.spec.actions["tag_odoo_order_sales_channel"].affects_source is False
        assert odoo_world.spec.actions["confirm_o2c_order"].affects_source is True

        # Reject duplicate active action claim
        with pytest.raises(InvariantError) as dup_exc:
            odoo_world.registry.mount(
                MountedDevice(
                    device_id="rogue_dup_o2c",
                    name="Rogue",
                    kind="odoo_jsonrpc",
                    endpoint="http://localhost:8069/jsonrpc",
                    bridge=SourceBridge(actuate=lambda _m: None, observe=lambda _m=None: {}),
                    handled_actions=("confirm_o2c_order",),
                    owned_object_types=(),
                )
            )
        assert dup_exc.value.code == DUPLICATE_ACTION_DRIVER

        # Unmounting a mounted device must fail closed on write (UNPLUGGED_DEVICE_WRITE)
        ent_cls = odoo_world.spec.get_object_type("AccountingEntity")
        party_cls = odoo_world.spec.get_object_type("BusinessParty")
        prof_cls = odoo_world.spec.get_object_type("O2cCustomerProfile")
        ord_cls = odoo_world.spec.get_object_type("O2cOrder")
        odoo_world.runtime.put_object(ent_cls(entity_id="ENT-1", name="Test Co", base_currency="USD"))
        odoo_world.runtime.put_object(party_cls(party_id="PTY-1", legal_name="Test Cust", tax_id="T1"))
        odoo_world.runtime.put_object(
            prof_cls(profile_id="CUST-1", credit_limit=10000.0, party="PTY-1", accounting_entity="ENT-1")
        )
        odoo_world.runtime.put_object(
            ord_cls(
                order_id="SO-1",
                order_number="SO0001",
                amount_total=100.0,
                status="draft",
                customer="CUST-1",
                accounting_entity="ENT-1",
            )
        )
        odoo_world.registry.unmount("odoo19_o2c")
        sales_actor = Actor(actor_id="test:sales", role="sales_rep")
        with pytest.raises(GovernanceViolationError) as unplug_exc:
            odoo_world.runtime.submit_action("RUN-UNPLUG", "confirm_o2c_order", {"order_id": "SO-1"}, sales_actor)
        assert unplug_exc.value.code == UNBOUND_SOURCE_BRIDGE

    with mount_jsherp_enterprise_world(
        {"base_url": JSHERP_BASE_URL, "username": "unused", "password": "unused"},
        hydrate=False,
    ) as jsh_world:
        devices = jsh_world.registry.list_devices()
        assert [d["device_id"] for d in devices] == ["jsherp_p2p", "jsherp_o2c", "jsherp_h2r"]
        assert all(d["status"] == "MOUNTED" for d in devices)
        assert "jsh_settlement_account_tag" in jsh_world.spec.get_object_type("P2pPurchaseOrder").owned_field_defaults()
        assert "jsh_depot_tag" in jsh_world.spec.get_object_type("O2cOrder").owned_field_defaults()
        assert "jsh_roster_note" in jsh_world.spec.get_object_type("Employee").owned_field_defaults()
        assert jsh_world.spec.get_object_type("AuthorityMandate").__owned__ is True
        assert jsh_world.spec.get_object_type("JournalEntry").__owned__ is True
        assert jsh_world.spec.actions["grant_authority_mandate"].affects_source is False
        assert jsh_world.spec.actions["revoke_authority_mandate"].affects_source is False
        assert jsh_world.spec.actions["post_journal_entry"].affects_source is False
        assert jsh_world.spec.actions["offboard_employee"].affects_source is True


# ---------------------------------------------------------------------------
# 2. Shared Standalone Domain Exercises inside a Coupled OntologyRuntime
# ---------------------------------------------------------------------------
def _exercise_standalone_crm_and_p2m(world: MountedErpWorld, *, entity_id: str, party_id: str) -> None:
    rt = world.runtime
    spec = world.spec
    steward = Actor(actor_id="test:steward", role="steward", clearance_tags={"*"})
    sales = Actor(actor_id="test:sales", role="sales_rep")
    support = Actor(actor_id="test:support", role="support_agent")
    planner = Actor(actor_id="test:planner", role="production_planner")
    shop = Actor(actor_id="test:shopfloor", role="shopfloor")

    crm_acc = spec.get_object_type("CrmAccount")(
        account_id="CRM-A1",
        name="Hybrid Key Account",
        industry="Manufacturing",
        party=party_id,
    )
    crm_deal = spec.get_object_type("CrmDeal")(
        deal_id="CRM-D1",
        title="Annual ERP Suite Deal",
        amount=88000.0,
        stage="PROPOSAL",
        account="CRM-A1",
    )
    crm_tkt = spec.get_object_type("CrmTicket")(
        ticket_id="CRM-T1",
        subject="Onboarding Question",
        status="OPEN",
        account="CRM-A1",
    )
    for obj in (crm_acc, crm_deal, crm_tkt):
        rt.put_object(obj, actor=steward)

    assert rt.submit_action("HYB-CRM-WIN", "win_crm_deal", {"deal_id": "CRM-D1"}, sales)["status"] == "COMMITTED"
    assert rt.get_object("CrmDeal", "CRM-D1", steward).stage == "CLOSED_WON"
    assert (
        rt.submit_action(
            "HYB-CRM-TKT",
            "resolve_crm_ticket",
            {"ticket_id": "CRM-T1", "resolution_summary": "Resolved cleanly in hybrid runtime"},
            support,
        )["status"]
        == "COMMITTED"
    )
    assert rt.get_object("CrmTicket", "CRM-T1", steward).status == "RESOLVED"

    tr_in = spec.get_object_type("TradeResource")(
        resource_id="MAT-HYB-RAW",
        code="RAW-100",
        name="Hybrid Copper Billet",
        unit="kg",
        spec_summary="99.9% Cu billet",
    )
    tr_out = spec.get_object_type("TradeResource")(
        resource_id="MAT-HYB-FG",
        code="FG-200",
        name="Hybrid Stator Core",
        unit="pcs",
        spec_summary="Laminated stator",
    )
    bom = spec.get_object_type("BomSpec")(
        bom_id="BOM-HYB-1",
        input_per_unit=2.0,
        max_scrap_ratio=0.10,
        output_resource="MAT-HYB-FG",
        input_resource="MAT-HYB-RAW",
    )
    wo = spec.get_object_type("WorkOrder")(
        work_order_id="WO-HYB-1",
        planned_qty=10.0,
        status="draft",
        accounting_entity=entity_id,
        bom="BOM-HYB-1",
        output_resource="MAT-HYB-FG",
    )
    for obj in (tr_in, tr_out, bom, wo):
        rt.put_object(obj, actor=steward)

    assert rt.submit_action("HYB-P2M-REL", "release_work_order", {"work_order_id": "WO-HYB-1"}, planner)["status"] == "COMMITTED"
    assert (
        rt.submit_action(
            "HYB-P2M-ISS",
            "issue_material_to_work_order",
            {
                "consumption_id": "CONS-HYB-1",
                "work_order_id": "WO-HYB-1",
                "input_resource": "MAT-HYB-RAW",
                "qty": 20.0,
                "unit_cost_cents": 2500,
                "total_cost_cents": 50000,
            },
            shop,
        )["status"]
        == "COMMITTED"
    )
    assert (
        rt.submit_action(
            "HYB-P2M-OUT",
            "record_production_output",
            {
                "output_id": "OUT-HYB-1",
                "work_order_id": "WO-HYB-1",
                "output_resource": "MAT-HYB-FG",
                "good_qty": 10.0,
                "scrap_qty": 0.0,
                "absorbed_cost_cents": 50000,
            },
            shop,
        )["status"]
        == "COMMITTED"
    )
    assert (
        rt.submit_action(
            "HYB-P2M-CLS",
            "close_work_order",
            {"work_order_id": "WO-HYB-1", "target_status": "completed"},
            planner,
        )["status"]
        == "COMMITTED"
    )
    diag = rt.call_function("diagnose_work_order_conservation", {"work_order_id": "WO-HYB-1"}, steward)
    assert diag["all_cost_conserved"] is True
    assert diag["work_orders"][0]["remaining_unabsorbed_cost_cents"] == 0


# ---------------------------------------------------------------------------
# 3. Live Docker Hybrid Enterprise World E2E: Odoo 19
# ---------------------------------------------------------------------------
def _seed_combined_odoo19_world(admin: Odoo24cAdminClient, db_name: str, *, tag: str) -> dict[str, Any]:
    kw = admin.login_kw(db_name)
    _strip_odoo_demo_noise(kw)
    kw("res.company", "write", [[1], {"po_lock": "edit", "po_double_validation": "one_step", "fiscalyear_lock_date": False, "hard_lock_date": False}])

    countries = kw("res.country", "search", [[("code", "=", "CN")]], {"limit": 1})
    country_id = int(countries[0]) if countries else False

    # 1. Seed O2C: 2 sales orders (1 draft, 1 confirmed + unvalidated delivery + draft out_invoice)
    cust_vals: dict[str, Any] = {
        "name": f"Hybrid Customer {tag}",
        "ref": f"CUST-{tag}",
        "vat": f"TAX-C-{tag}",
        "is_company": True,
        "customer_rank": 1,
        "credit_limit": 500000.0,
        "company_id": 1,
    }
    if country_id:
        cust_vals["country_id"] = country_id
    cust_id = int(kw("res.partner", "create", [cust_vals]))

    sale_prod = int(
        kw(
            "product.product",
            "create",
            [
                {
                    "name": f"Odoo Hybrid Server {tag}",
                    "default_code": f"SRV-{tag}",
                    "type": "consu",
                    "is_storable": True,
                    "list_price": 1200.0,
                    "sale_ok": True,
                    "taxes_id": [(6, 0, [])],
                }
            ],
        )
    )

    so_draft = int(
        kw(
            "sale.order",
            "create",
            [
                {
                    "partner_id": cust_id,
                    "client_order_ref": f"SO-DRAFT-{tag}",
                    "order_line": [(0, 0, {"product_id": sale_prod, "name": "Draft Item", "product_uom_qty": 2, "price_unit": 1000.0, "tax_ids": [(6, 0, [])]})],
                }
            ],
        )
    )
    so_confirmed = int(
        kw(
            "sale.order",
            "create",
            [
                {
                    "partner_id": cust_id,
                    "client_order_ref": f"SO-CONF-{tag}",
                    "order_line": [(0, 0, {"product_id": sale_prod, "name": "Undelivered Item", "product_uom_qty": 3, "price_unit": 1200.0, "tax_ids": [(6, 0, [])]})],
                }
            ],
        )
    )
    kw("sale.order", "action_confirm", [[so_confirmed]])
    so_conf_row = kw("sale.order", "read", [[so_confirmed], ["name", "picking_ids"]])[0]
    so_picking = int(so_conf_row["picking_ids"][0])
    sol_row = kw("sale.order.line", "search_read", [[("order_id", "=", so_confirmed)]], {"fields": ["id", "product_id", "product_uom_qty", "price_unit"]})[0]
    so_inv_draft = int(
        kw(
            "account.move",
            "create",
            [
                {
                    "move_type": "out_invoice",
                    "partner_id": cust_id,
                    "invoice_date": "2026-11-22",
                    "invoice_origin": str(so_conf_row["name"]),
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "product_id": int(sol_row["product_id"][0]),
                                "quantity": float(sol_row["product_uom_qty"]),
                                "price_unit": float(sol_row["price_unit"]),
                                "tax_ids": [(6, 0, [])],
                                "sale_line_ids": [(6, 0, [int(sol_row["id"])])],
                            },
                        )
                    ],
                }
            ],
        )
    )

    # 2. Seed P2P: 2 purchase orders (1 draft, 1 confirmed + unvalidated receipt + draft in_invoice)
    sup_vals: dict[str, Any] = {
        "name": f"Hybrid Supplier {tag}",
        "ref": f"SUP-{tag}",
        "vat": f"TAX-S-{tag}",
        "is_company": True,
        "supplier_rank": 1,
        "company_id": 1,
    }
    if country_id:
        sup_vals["country_id"] = country_id
    sup_id = int(kw("res.partner", "create", [sup_vals]))

    incoming_pt = int(kw("stock.picking.type", "search", [[("code", "=", "incoming")]], {"limit": 1})[0])
    buy_prod = int(
        kw(
            "product.product",
            "create",
            [
                {
                    "name": f"Odoo Hybrid Alloy {tag}",
                    "default_code": f"ALY-{tag}",
                    "type": "consu",
                    "is_storable": True,
                    "standard_price": 400.0,
                    "purchase_ok": True,
                    "supplier_taxes_id": [(6, 0, [])],
                    "taxes_id": [(6, 0, [])],
                }
            ],
        )
    )

    po_draft = int(
        kw(
            "purchase.order",
            "create",
            [
                {
                    "partner_id": sup_id,
                    "partner_ref": f"PO-DRAFT-{tag}",
                    "picking_type_id": incoming_pt,
                    "order_line": [(0, 0, {"product_id": buy_prod, "name": "Draft Buy", "product_qty": 2, "price_unit": 500.0, "tax_ids": [(6, 0, [])]})],
                }
            ],
        )
    )
    po_confirmed = int(
        kw(
            "purchase.order",
            "create",
            [
                {
                    "partner_id": sup_id,
                    "partner_ref": f"PO-CONF-{tag}",
                    "picking_type_id": incoming_pt,
                    "order_line": [(0, 0, {"product_id": buy_prod, "name": "Unreceived Buy", "product_qty": 4, "price_unit": 450.0, "tax_ids": [(6, 0, [])]})],
                }
            ],
        )
    )
    kw("purchase.order", "button_confirm", [[po_confirmed]])
    po_conf_row = kw("purchase.order", "read", [[po_confirmed], ["name", "picking_ids", "order_line"]])[0]
    po_picking = int(po_conf_row["picking_ids"][0])
    po_bill_draft = int(
        kw(
            "account.move",
            "create",
            [
                {
                    "move_type": "in_invoice",
                    "partner_id": sup_id,
                    "invoice_date": "2026-11-22",
                    "invoice_origin": str(po_conf_row["name"]),
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "product_id": buy_prod,
                                "quantity": 4.0,
                                "price_unit": 450.0,
                                "tax_ids": [(6, 0, [])],
                                "purchase_line_id": int(po_conf_row["order_line"][0]),
                            },
                        )
                    ],
                }
            ],
        )
    )

    # 3. Seed R2R: dedicated Cash + Expense accounts & balanced draft JournalEntry
    cash_acc = int(
        kw(
            "account.account",
            "create",
            [{"code": f"581{abs(hash(tag)) % 9}", "name": f"R2R Cash {tag}", "account_type": "asset_cash", "active": True, "company_ids": [(6, 0, [1])]}],
        )
    )
    exp_acc = int(
        kw(
            "account.account",
            "create",
            [{"code": f"586{abs(hash(tag)) % 9}", "name": f"R2R Expense {tag}", "account_type": "expense", "active": True, "company_ids": [(6, 0, [1])]}],
        )
    )
    j_misc = int(kw("account.journal", "search", [[("type", "=", "general"), ("company_id", "=", 1)]], {"limit": 1})[0])
    je_draft = int(
        kw(
            "account.move",
            "create",
            [
                {
                    "move_type": "entry",
                    "journal_id": j_misc,
                    "date": "2026-11-20",
                    "ref": f"JE-HYB-{tag}",
                    "line_ids": [
                        (0, 0, {"account_id": exp_acc, "name": "Service Fee", "debit": 320.0, "credit": 0.0}),
                        (0, 0, {"account_id": cash_acc, "name": "Cash Paid", "debit": 0.0, "credit": 320.0}),
                    ],
                }
            ],
        )
    )

    return {
        "kw": kw,
        "cust_id": cust_id,
        "sup_id": sup_id,
        "so_draft_id": so_draft,
        "so_conf_id": so_confirmed,
        "so_picking_id": so_picking,
        "so_inv_draft_id": so_inv_draft,
        "po_draft_id": po_draft,
        "po_conf_id": po_confirmed,
        "po_picking_id": po_picking,
        "po_bill_draft_id": po_bill_draft,
        "je_draft_id": je_draft,
    }


def test_4_live_odoo19_hybrid_enterprise_world_e2e() -> None:
    admin = Odoo24cAdminClient(ODOO_URL)
    tag = uuid.uuid4().hex[:6].upper()
    temp_db = f"wo24c_e2e_odoo_{tag.lower()}"
    admin.drop_all_wo24c_dbs()
    admin.clone_db("odoo_demo", temp_db)

    try:
        seeded = _seed_combined_odoo19_world(admin, temp_db, tag=tag)
        kw = seeded["kw"]
        ep_cfg = {"url": ODOO_URL, "db": temp_db, "username": ODOO_USER, "password": ODOO_PASSWORD}

        with mount_odoo19_enterprise_world(ep_cfg, include_overlay=True, hydrate=True) as world:
            rt = world.runtime
            spec = world.spec
            steward = Actor(actor_id="e2e:steward", role="steward", clearance_tags={"*"})
            sales = Actor(actor_id="e2e:sales", role="sales_rep")
            wh = Actor(actor_id="e2e:wh", role="warehouse")
            fin = Actor(actor_id="e2e:fin", role="finance")
            buyer = Actor(actor_id="e2e:buyer", role="buyer")
            chief = Actor(actor_id="e2e:chief", role="chief_accountant")
            hr_dir = Actor(actor_id="e2e:hrdir", role="hr_director", clearance_tags={"*"})

            entities = rt.query_objects("AccountingEntity", steward)
            entity_ids = {e.entity_id for e in entities}
            assert "ENT-1" in entity_ids
            ent_id = "ENT-1"
            o2c_pfx = _mapping_prefixes("odoo19", "o2c")
            p2p_pfx = _mapping_prefixes("odoo19", "p2p")
            r2r_pfx = _mapping_prefixes("odoo19", "r2r")
            cust_party_id = f"{o2c_pfx['BusinessParty']}{seeded['cust_id']}"

            # --- A. Coupled R2R + Overlay tag BEFORE O2C/P2P invoices update GL accounts ---
            je_id = f"{r2r_pfx['JournalEntry']}{seeded['je_draft_id']}"
            assert (
                rt.submit_action(
                    "ODOO-R2R-TAG",
                    "tag_odoo_journal_code",
                    {"entry_id": je_id, "journal_code": "FIN-OPS"},
                    chief,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("JournalEntry", je_id, steward).odoo_journal_code == "FIN-OPS"
            assert rt.submit_action("ODOO-R2R-POST", "post_journal_entry", {"entry_id": je_id}, chief)["status"] == "COMMITTED"
            assert kw("account.move", "read", [[seeded["je_draft_id"]], ["state"]])[0]["state"] == "posted"
            tb_mid = rt.call_function("compute_trial_balance", {"accounting_entity": ent_id}, steward)
            assert tb_mid["is_balanced"] is True
            assert tb_mid["accounting_equation_holds"] is True
            assert rt.submit_action("ODOO-R2R-REV", "reverse_journal_entry", {"entry_id": je_id, "memo": "Period close cleanup"}, chief)["status"] == "COMMITTED"
            tb_after_rev = rt.call_function("compute_trial_balance", {"accounting_entity": ent_id}, steward)
            assert tb_after_rev["is_balanced"] is True

            # --- B. Coupled O2C + Overlay (DELIVERY_REQUIRED_BEFORE_INVOICE + owned field) ---
            so_draft_oid = f"{o2c_pfx['O2cOrder']}{seeded['so_draft_id']}"
            assert rt.submit_action("ODOO-O2C-CONF", "confirm_o2c_order", {"order_id": so_draft_oid}, sales)["status"] == "COMMITTED"
            assert kw("sale.order", "read", [[seeded["so_draft_id"]], ["state"]])[0]["state"] in ("sale", "done")

            so_conf_oid = f"{o2c_pfx['O2cOrder']}{seeded['so_conf_id']}"
            del_oid = f"{o2c_pfx['O2cDelivery']}{seeded['so_picking_id']}"
            inv_oid = f"{o2c_pfx['O2cInvoice']}{seeded['so_inv_draft_id']}"
            assert (
                rt.submit_action(
                    "ODOO-O2C-TAG",
                    "tag_odoo_order_sales_channel",
                    {"order_id": so_conf_oid, "sales_channel": "enterprise_direct"},
                    sales,
                )["status"]
                == "COMMITTED"
            )
            with pytest.raises(InvariantError) as o2c_blk:
                rt.submit_action("ODOO-O2C-BLK", "post_o2c_invoice", {"invoice_id": inv_oid}, fin)
            assert o2c_blk.value.code == "DELIVERY_REQUIRED_BEFORE_INVOICE"
            assert kw("account.move", "read", [[seeded["so_inv_draft_id"]], ["state"]])[0]["state"] == "draft"

            assert rt.submit_action("ODOO-O2C-DEL", "complete_o2c_delivery", {"delivery_id": del_oid}, wh)["status"] == "COMMITTED"
            assert rt.submit_action("ODOO-O2C-INV", "post_o2c_invoice", {"invoice_id": inv_oid}, fin)["status"] == "COMMITTED"
            inv_obj = rt.get_object("O2cInvoice", inv_oid, steward)
            assert (
                rt.submit_action(
                    "ODOO-O2C-PAY",
                    "record_o2c_payment",
                    {"invoice_id": inv_oid, "payment_amount": inv_obj.amount_total},
                    fin,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("O2cInvoice", inv_oid, steward).payment_state == "paid"
            assert rt.get_object("O2cOrder", so_conf_oid, steward).odoo_sales_channel == "enterprise_direct"

            # --- C. Coupled P2P + Overlay (RECEIPT_REQUIRED_BEFORE_BILL + owned field) ---
            po_draft_oid = f"{p2p_pfx['P2pPurchaseOrder']}{seeded['po_draft_id']}"
            assert rt.submit_action("ODOO-P2P-CONF", "confirm_p2p_order", {"po_id": po_draft_oid}, buyer)["status"] == "COMMITTED"
            assert kw("purchase.order", "read", [[seeded["po_draft_id"]], ["state"]])[0]["state"] in ("purchase", "done")

            po_conf_oid = f"{p2p_pfx['P2pPurchaseOrder']}{seeded['po_conf_id']}"
            gr_oid = f"{p2p_pfx['P2pGoodsReceipt']}{seeded['po_picking_id']}"
            bill_oid = f"{p2p_pfx['P2pVendorBill']}{seeded['po_bill_draft_id']}"
            assert (
                rt.submit_action(
                    "ODOO-P2P-NOTE",
                    "set_odoo_po_buyer_note",
                    {"po_id": po_conf_oid, "buyer_note": "Priority alloy lot"},
                    buyer,
                )["status"]
                == "COMMITTED"
            )
            with pytest.raises(InvariantError) as p2p_blk:
                rt.submit_action("ODOO-P2P-BLK", "post_p2p_bill", {"bill_id": bill_oid}, fin)
            assert p2p_blk.value.code == "RECEIPT_REQUIRED_BEFORE_BILL"
            assert kw("account.move", "read", [[seeded["po_bill_draft_id"]], ["state"]])[0]["state"] == "draft"

            gr_obj = rt.get_object("P2pGoodsReceipt", gr_oid, steward)
            assert (
                rt.submit_action(
                    "ODOO-P2P-GR",
                    "record_p2p_receipt",
                    {"receipt_id": gr_oid, "received_qty": gr_obj.expected_qty, "inspection_status": "passed"},
                    wh,
                )["status"]
                == "COMMITTED"
            )
            assert rt.submit_action("ODOO-P2P-BILL", "post_p2p_bill", {"bill_id": bill_oid}, fin)["status"] == "COMMITTED"
            bill_obj = rt.get_object("P2pVendorBill", bill_oid, steward)
            assert (
                rt.submit_action(
                    "ODOO-P2P-PAY",
                    "record_p2p_payment",
                    {"bill_id": bill_oid, "payment_amount": bill_obj.amount_total},
                    fin,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("P2pVendorBill", bill_oid, steward).payment_state == "paid"
            assert rt.get_object("P2pPurchaseOrder", po_conf_oid, steward).odoo_buyer_note == "Priority alloy lot"

            # --- D. Standalone CRM + P2M + H2R inside the SAME coupled OntologyRuntime ---
            _exercise_standalone_crm_and_p2m(world, entity_id=ent_id, party_id=cust_party_id)

            dept_hq = spec.get_object_type("Department")(
                department_id="DEPT-OHQ",
                name="Odoo HQ",
                scope_tag="hq",
                accounting_entity=ent_id,
            )
            dept_fin = spec.get_object_type("Department")(
                department_id="DEPT-OFIN",
                name="Odoo Finance",
                scope_tag="finance",
                accounting_entity=ent_id,
                parent_department="DEPT-OHQ",
            )
            pos_dir = spec.get_object_type("JobPosition")(
                position_id="POS-ODIR",
                title="Finance Director",
                role_name="finance_director",
                approval_ceiling_cents=5000000,
                department="DEPT-OHQ",
            )
            pos_fin = spec.get_object_type("JobPosition")(
                position_id="POS-OFIN",
                title="Finance Specialist",
                role_name="finance",
                approval_ceiling_cents=1000000,
                department="DEPT-OFIN",
            )
            emp_dir = spec.get_object_type("Employee")(
                employee_id="EMP-ODIR",
                full_name="Grace Director",
                actor_id="uid:grace",
                status="active",
                department="DEPT-OHQ",
                position="POS-ODIR",
            )
            emp_fin = spec.get_object_type("Employee")(
                employee_id="EMP-OFIN",
                full_name="Hank Analyst",
                actor_id="uid:hank",
                status="active",
                department="DEPT-OFIN",
                position="POS-OFIN",
            )
            for obj in (dept_hq, dept_fin, pos_dir, pos_fin, emp_dir, emp_fin):
                rt.put_object(obj, actor=steward)

            assert (
                rt.submit_action(
                    "ODOO-H2R-MND",
                    "grant_authority_mandate",
                    {
                        "mandate_id": "MND-O1",
                        "grantor_id": "EMP-ODIR",
                        "grantee_id": "EMP-OFIN",
                        "role_name": "finance_director",
                        "ceiling_cents": 2500000,
                        "scope_tag": "finance",
                    },
                    hr_dir,
                )["status"]
                == "COMMITTED"
            )
            eff = rt.call_function("resolve_employee_effective_mandates", {"employee_id": "EMP-OFIN"}, steward)
            assert eff["effective_ceilings_by_role"]["finance_director"] == 2500000
            org_dir = project_h2r_to_org_directory(rt, steward)
            hank_member = org_dir.get_member("uid:hank")
            assert "org:finance" in hank_member.extra_clearance_tags

            # --- E. Counterfactual runtime.fork() isolation ---
            parent_order_count = len(rt.query_objects("O2cOrder", steward))
            with rt.fork() as sim_rt:
                assert (
                    sim_rt.submit_action(
                        "ODOO-FORK-TAG",
                        "tag_odoo_order_sales_channel",
                        {"order_id": so_conf_oid, "sales_channel": "fork_counterfactual"},
                        sales,
                    )["status"]
                    == "COMMITTED"
                )
                assert sim_rt.get_object("O2cOrder", so_conf_oid, steward).odoo_sales_channel == "fork_counterfactual"
                assert len(sim_rt.query_objects("O2cOrder", steward)) == parent_order_count
            assert rt.get_object("O2cOrder", so_conf_oid, steward).odoo_sales_channel == "enterprise_direct"
    finally:
        rem_dbs = admin.drop_all_wo24c_dbs()
        assert rem_dbs == ["erp_north", "erp_south", "odoo_demo"]


# ---------------------------------------------------------------------------
# 4. Live Docker Hybrid Enterprise World E2E: jshERP
# ---------------------------------------------------------------------------
def _seed_combined_jsherp_tenant(admin: JshErp24cAdminClient, *, tag: str) -> dict[str, Any]:
    login_name = f"wo24c_e2e_jsh_{tag.lower()}"
    pw_plain = f"PassHybrid_{tag}!"
    token, tid, pw_md5 = admin.register_tenant(login_name, pw_plain)

    admin._admin_rest(
        "/systemConfig/add",
        method="POST",
        token=token,
        tenant_id=tid,
        body={"companyName": f"Huaxia Hybrid Corp {tag}", "minusStockFlag": "1"},
    )
    admin._admin_rest(
        "/depot/add",
        method="POST",
        token=token,
        tenant_id=tid,
        body={"name": f"WH-HYB-{tag}", "type": 0, "isDefault": True, "enabled": True},
    )
    admin._admin_rest(
        "/account/add",
        method="POST",
        token=token,
        tenant_id=tid,
        body={"name": f"Bank-HYB-{tag}", "serialNo": f"BK-{tag}", "initialAmount": 9000000.0, "isDefault": True, "enabled": True},
    )
    admin._admin_rest("/inOutItem/add", method="POST", token=token, tenant_id=tid, body={"name": f"IO-AP-{tag}", "type": "支出", "enabled": True})
    admin._admin_rest("/inOutItem/add", method="POST", token=token, tenant_id=tid, body={"name": f"IO-AR-{tag}", "type": "收入", "enabled": True})

    sup_name = f"Hybrid Alloy Supplier {tag}"
    cust_name = f"Hybrid OEM Customer {tag}"
    admin._admin_rest(
        "/supplier/add",
        method="POST",
        token=token,
        tenant_id=tid,
        body={"supplier": sup_name, "type": "供应商", "taxNum": f"TAX-JS-{tag}", "description": "NET30", "enabled": True},
    )
    admin._admin_rest(
        "/supplier/add",
        method="POST",
        token=token,
        tenant_id=tid,
        body={"supplier": cust_name, "type": "客户", "taxNum": f"TAX-JC-{tag}", "advanceIn": 600000.0, "description": "NET45", "enabled": True},
    )
    sup_rows = admin._admin_rest(
        f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '供应商', 'supplier': sup_name}, ensure_ascii=False))}",
        token=token,
    )["data"]["rows"]
    cust_rows = admin._admin_rest(
        f"/supplier/list?search={urllib.parse.quote(json.dumps({'type': '客户', 'supplier': cust_name}, ensure_ascii=False))}",
        token=token,
    )["data"]["rows"]
    sup_id = int(next(r["id"] for r in sup_rows if r.get("supplier") == sup_name))
    cust_id = int(next(r["id"] for r in cust_rows if r.get("supplier") == cust_name))

    depot_id = int(admin._admin_rest("/depot/list", token=token)["data"]["rows"][0]["id"])
    acct_id = int(admin._admin_rest("/account/list", token=token)["data"]["rows"][0]["id"])
    admin._admin_rest(
        "/material/add",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "name": f"Hybrid Steel Plate {tag}",
            "unit": "吨",
            "enabled": True,
            "meList": [
                {
                    "barCode": f"MAT-HYB-{tag}",
                    "commodityUnit": "吨",
                    "purchaseDecimal": 2000,
                    "commodityDecimal": 3200,
                    "wholesaleDecimal": 3000,
                    "lowDecimal": 100,
                }
            ],
            "stock": [{"depotId": depot_id, "number": 10000.0}],
        },
    )
    mat_id = int(admin._admin_rest("/material/list", token=token)["data"]["rows"][0]["id"])
    me_id = int(
        admin._mysql_exec(
            f"SELECT id FROM jsh_material_extend WHERE tenant_id = {tid} AND material_id = {mat_id} AND delete_flag <> '1' LIMIT 1;"
        ).strip().splitlines()[0]
    )

    oper_time = "2026-11-22 12:00:00"
    admin.clear_tenant_logs(tid)

    # P2P: PO1 draft + PO2 confirmed + pending RK2 (status='0')
    po1_num = f"PO1-{tag}"
    po2_num = f"PO2-{tag}"
    rk2_num = f"RK2-{tag}"
    admin._admin_rest(
        "/depotHead/addDepotHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "其它", "subType": "采购订单", "defaultNumber": po1_num, "number": po1_num, "operTime": oper_time, "organId": sup_id, "totalPrice": 4000.0, "discountLastMoney": 4000.0, "status": "0"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_po1", "materialId": mat_id, "materialExtendId": me_id, "barCode": f"MAT-HYB-{tag}", "name": f"Hybrid Steel Plate {tag}", "unit": "吨", "depotId": depot_id, "operNumber": 2.0, "unitPrice": 2000.0, "allPrice": 4000.0, "taxRate": 0, "taxMoney": 0, "taxLastMoney": 4000.0}], ensure_ascii=False),
        },
    )
    po1_id = int(admin._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(po1_num)}", token=token)["data"]["id"])

    admin.clear_tenant_logs(tid)
    admin._admin_rest(
        "/depotHead/addDepotHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "其它", "subType": "采购订单", "defaultNumber": po2_num, "number": po2_num, "operTime": oper_time, "organId": sup_id, "totalPrice": 6000.0, "discountLastMoney": 6000.0, "status": "1"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_po2", "materialId": mat_id, "materialExtendId": me_id, "barCode": f"MAT-HYB-{tag}", "name": f"Hybrid Steel Plate {tag}", "unit": "吨", "depotId": depot_id, "operNumber": 3.0, "unitPrice": 2000.0, "allPrice": 6000.0, "taxRate": 0, "taxMoney": 0, "taxLastMoney": 6000.0}], ensure_ascii=False),
        },
    )
    po2_id = int(admin._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(po2_num)}", token=token)["data"]["id"])

    admin.clear_tenant_logs(tid)
    admin._admin_rest(
        "/depotHead/addDepotHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "入库", "subType": "采购", "defaultNumber": rk2_num, "number": rk2_num, "operTime": oper_time, "organId": sup_id, "accountId": acct_id, "linkNumber": po2_num, "totalPrice": 6000.0, "discountLastMoney": 6000.0, "changeAmount": 0.0, "debt": 6000.0, "status": "0"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_rk2", "materialId": mat_id, "materialExtendId": me_id, "barCode": f"MAT-HYB-{tag}", "name": f"Hybrid Steel Plate {tag}", "unit": "吨", "depotId": depot_id, "preNumber": 3.0, "finishNumber": 0.0, "operNumber": 3.0, "unitPrice": 2000.0, "allPrice": 6000.0, "taxRate": 0, "taxMoney": 0, "taxLastMoney": 6000.0}], ensure_ascii=False),
        },
    )
    rk2_id = int(admin._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(rk2_num)}", token=token)["data"]["id"])

    # O2C: SO1 draft + SO2 confirmed + pending CK2 (status='0') + draft SK2 (accountHead status='0')
    so1_num = f"SO1-{tag}"
    so2_num = f"SO2-{tag}"
    ck2_num = f"CK2-{tag}"
    sk2_num = f"SK2-{tag}"
    admin.clear_tenant_logs(tid)
    admin._admin_rest(
        "/depotHead/addDepotHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "其它", "subType": "销售订单", "defaultNumber": so1_num, "number": so1_num, "operTime": oper_time, "organId": cust_id, "totalPrice": 6400.0, "discountLastMoney": 6400.0, "status": "0"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_so1", "materialId": mat_id, "materialExtendId": me_id, "barCode": f"MAT-HYB-{tag}", "name": f"Hybrid Steel Plate {tag}", "unit": "吨", "depotId": depot_id, "operNumber": 2.0, "unitPrice": 3200.0, "allPrice": 6400.0, "taxRate": 0, "taxMoney": 0, "taxLastMoney": 6400.0}], ensure_ascii=False),
        },
    )
    so1_id = int(admin._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(so1_num)}", token=token)["data"]["id"])

    admin.clear_tenant_logs(tid)
    admin._admin_rest(
        "/depotHead/addDepotHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "其它", "subType": "销售订单", "defaultNumber": so2_num, "number": so2_num, "operTime": oper_time, "organId": cust_id, "totalPrice": 9600.0, "discountLastMoney": 9600.0, "status": "1"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_so2", "materialId": mat_id, "materialExtendId": me_id, "barCode": f"MAT-HYB-{tag}", "name": f"Hybrid Steel Plate {tag}", "unit": "吨", "depotId": depot_id, "operNumber": 3.0, "unitPrice": 3200.0, "allPrice": 9600.0, "taxRate": 0, "taxMoney": 0, "taxLastMoney": 9600.0}], ensure_ascii=False),
        },
    )
    so2_id = int(admin._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(so2_num)}", token=token)["data"]["id"])

    admin.clear_tenant_logs(tid)
    admin._admin_rest(
        "/depotHead/addDepotHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "出库", "subType": "销售", "defaultNumber": ck2_num, "number": ck2_num, "operTime": oper_time, "organId": cust_id, "accountId": acct_id, "linkNumber": so2_num, "totalPrice": 9600.0, "discountLastMoney": 9600.0, "changeAmount": 0.0, "debt": 9600.0, "status": "0"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_ck2", "materialId": mat_id, "materialExtendId": me_id, "barCode": f"MAT-HYB-{tag}", "name": f"Hybrid Steel Plate {tag}", "unit": "吨", "depotId": depot_id, "preNumber": 3.0, "finishNumber": 0.0, "operNumber": 3.0, "unitPrice": 3200.0, "allPrice": 9600.0, "taxRate": 0, "taxMoney": 0, "taxLastMoney": 9600.0}], ensure_ascii=False),
        },
    )
    ck2_id = int(admin._admin_rest(f"/depotHead/getDetailByNumber?number={urllib.parse.quote(ck2_num)}", token=token)["data"]["id"])

    admin.clear_tenant_logs(tid)
    admin._admin_rest(
        "/accountHead/addAccountHeadAndDetail",
        method="POST",
        token=token,
        tenant_id=tid,
        body={
            "info": json.dumps({"type": "收款", "organId": cust_id, "accountId": acct_id, "billNo": sk2_num, "billTime": oper_time, "totalPrice": 9600.0, "changeAmount": 0.0, "discountMoney": 0.0, "status": "0", "remark": "HYB AR"}, ensure_ascii=False),
            "rows": json.dumps([{"id": "r_sk2", "billNumber": ck2_num, "needDebt": 9600.0, "finishDebt": 0.0, "eachAmount": 0.0}], ensure_ascii=False),
        },
    )
    sk2_id = int(admin._admin_rest(f"/accountHead/getDetailByNumber?billNo={urllib.parse.quote(sk2_num)}", token=token)["data"]["id"])

    # H2R: 3 orgs, 3 roles, 3 employees (onboarding, active, active finance_director grantor)
    admin.clear_tenant_logs(tid)
    admin._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"HQ {tag}", "orgNo": f"ORG-HQ-{tag}", "remark": "hq", "sort": "1"})
    hq_id = int(next(n["id"] for n in admin._admin_rest("/organization/getOrganizationTree?id=", token=token) if n.get("attributes") == f"ORG-HQ-{tag}"))
    admin._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"Sales {tag}", "orgNo": f"ORG-SAL-{tag}", "parentId": hq_id, "remark": "sales", "sort": "2"})
    admin._admin_rest("/organization/add", method="POST", token=token, tenant_id=tid, body={"orgAbr": f"Ops {tag}", "orgNo": f"ORG-OPS-{tag}", "parentId": hq_id, "remark": "ops", "sort": "3"})
    org_sql = admin._mysql_exec(
        f"SELECT id, org_abr FROM jsh_organization WHERE tenant_id = {tid} AND delete_flag = '0' ORDER BY id;"
    ).strip().splitlines()
    org_ids = [int(line.split("\t")[0]) for line in org_sql]
    sales_org_id, ops_org_id = org_ids[1], org_ids[2]

    role_defs = [
        {"title": f"Director {tag}", "department_id": hq_id, "role_name": "finance_director", "approval_ceiling_cents": 50000000},
        {"title": f"Sales Mgr {tag}", "department_id": sales_org_id, "role_name": "sales_manager", "approval_ceiling_cents": 20000000},
        {"title": f"Ops Spec {tag}", "department_id": ops_org_id, "role_name": "ops", "approval_ceiling_cents": 5000000},
    ]
    for rd in role_defs:
        admin._admin_rest(
            "/role/add",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"name": rd["title"], "type": "全部数据", "description": json.dumps({"department_id": rd["department_id"], "role_name": rd["role_name"], "approval_ceiling_cents": rd["approval_ceiling_cents"]}, ensure_ascii=False)},
        )
    all_roles = admin._admin_rest("/role/list?search=%7B%7D", token=token)["data"]["rows"]
    role_ids = [int(next(r["id"] for r in all_roles if r.get("tenantId") == tid and r.get("name") == rd["title"])) for rd in role_defs]

    emp_defs = [
        {"full_name": f"Alice {tag}", "login_name": f"hyb_a_{tag.lower()}", "org_id": ops_org_id, "role_id": role_ids[2], "title": role_defs[2]["title"], "status_label": "onboarding"},
        {"full_name": f"Bob {tag}", "login_name": f"hyb_b_{tag.lower()}", "org_id": sales_org_id, "role_id": role_ids[1], "title": role_defs[1]["title"], "status_label": "active"},
        {"full_name": f"Carol {tag}", "login_name": f"hyb_c_{tag.lower()}", "org_id": hq_id, "role_id": role_ids[0], "title": role_defs[0]["title"], "status_label": "active"},
    ]
    for ed in emp_defs:
        admin._admin_rest(
            "/user/addUser",
            method="POST",
            token=token,
            tenant_id=tid,
            body={"loginName": ed["login_name"], "username": ed["full_name"], "position": ed["title"], "roleId": ed["role_id"], "orgaId": ed["org_id"], "description": ed["status_label"]},
        )
    ulist = admin._admin_rest("/user/list?search=%7B%7D", token=token)["data"]["rows"]
    emp_ids = [int(next(u["id"] for u in ulist if u.get("loginName") == ed["login_name"])) for ed in emp_defs]

    admin.clear_tenant_logs(tid)
    return {
        "tenant_id": tid,
        "token": token,
        "ep_cfg": {
            "base_url": JSHERP_BASE_URL,
            "openapi_url": f"{JSHERP_BASE_URL}/v2/api-docs",
            "login_url": f"{JSHERP_BASE_URL}/user/login",
            "username": login_name,
            "password": pw_plain,
            "password_md5": pw_md5,
            "token_header": "X-Access-Token",
        },
        "cust_id": cust_id,
        "sup_id": sup_id,
        "po1_id": po1_id,
        "po2_id": po2_id,
        "rk2_id": rk2_id,
        "so1_id": so1_id,
        "so2_id": so2_id,
        "ck2_id": ck2_id,
        "sk2_id": sk2_id,
        "hq_id": hq_id,
        "sales_org_id": sales_org_id,
        "ops_org_id": ops_org_id,
        "role_ids": role_ids,
        "emp_ids": emp_ids,
        "emp_logins": [e["login_name"] for e in emp_defs],
    }


def test_5_live_jsherp_hybrid_enterprise_world_e2e() -> None:
    admin = JshErp24cAdminClient(JSHERP_BASE_URL)
    admin.cleanup_all_wo24c_tenants()
    tag = uuid.uuid4().hex[:6].upper()

    try:
        seeded = _seed_combined_jsherp_tenant(admin, tag=tag)
        tid = seeded["tenant_id"]

        with mount_jsherp_enterprise_world(seeded["ep_cfg"], include_overlay=True, hydrate=True) as world:
            rt = world.runtime
            spec = world.spec
            steward = Actor(actor_id="e2e:steward", role="steward", clearance_tags={"*"})
            buyer = Actor(actor_id="e2e:buyer", role="buyer")
            wh = Actor(actor_id="e2e:wh", role="warehouse")
            fin = Actor(actor_id="e2e:fin", role="finance")
            sales = Actor(actor_id="e2e:sales", role="sales_rep")
            hr_adm = Actor(actor_id="e2e:hradm", role="hr_admin", clearance_tags={"*"})
            hr_dir = Actor(actor_id="e2e:hrdir", role="hr_director", clearance_tags={"*"})
            chief = Actor(actor_id="e2e:chief", role="chief_accountant")

            # --- A. Verify single unified AccountingEntity across p2p, o2c, h2r ---
            entities = rt.query_objects("AccountingEntity", steward)
            assert len(entities) == 1
            ent_id = entities[0].entity_id
            assert ent_id == f"ENT-{tid}"
            assert all(po.accounting_entity == ent_id for po in rt.query_objects("P2pPurchaseOrder", steward))
            assert all(so.accounting_entity == ent_id for so in rt.query_objects("O2cOrder", steward))
            assert all(d.accounting_entity == ent_id for d in rt.query_objects("Department", steward))

            # --- B. Coupled P2P + Overlay ---
            po1_oid = f"PO-{seeded['po1_id']}"
            assert rt.submit_action("JSH-P2P-CONF", "confirm_p2p_order", {"po_id": po1_oid}, buyer)["status"] == "COMMITTED"
            admin.clear_tenant_logs(tid)

            po2_oid = f"PO-{seeded['po2_id']}"
            gr2_oid = f"GR-{seeded['rk2_id']}"
            bill2_oid = f"BILL-{seeded['rk2_id']}"
            assert (
                rt.submit_action(
                    "JSH-P2P-TAG",
                    "tag_jsh_po_settlement_account",
                    {"po_id": po2_oid, "account_tag": "CAPEX-AP"},
                    buyer,
                )["status"]
                == "COMMITTED"
            )
            with pytest.raises(InvariantError) as p2p_blk:
                rt.submit_action("JSH-P2P-BLK", "post_p2p_bill", {"bill_id": bill2_oid}, fin)
            assert p2p_blk.value.code == "RECEIPT_REQUIRED_BEFORE_BILL"
            assert admin._mysql_exec(f"SELECT status FROM jsh_depot_head WHERE id = {seeded['rk2_id']};").strip() == "0"

            assert (
                rt.submit_action(
                    "JSH-P2P-GR",
                    "record_p2p_receipt",
                    {"receipt_id": gr2_oid, "received_qty": 3.0, "inspection_status": "passed"},
                    wh,
                )["status"]
                == "COMMITTED"
            )
            admin.clear_tenant_logs(tid)
            assert rt.submit_action("JSH-P2P-BILL", "post_p2p_bill", {"bill_id": bill2_oid}, fin)["status"] == "COMMITTED"
            admin.clear_tenant_logs(tid)
            assert (
                rt.submit_action(
                    "JSH-P2P-PAY",
                    "record_p2p_payment",
                    {"bill_id": bill2_oid, "payment_amount": 6000.0},
                    fin,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("P2pVendorBill", bill2_oid, steward).payment_state == "paid"
            assert rt.get_object("P2pPurchaseOrder", po2_oid, steward).jsh_settlement_account_tag == "CAPEX-AP"

            # --- C. Coupled O2C + Overlay ---
            admin.clear_tenant_logs(tid)
            o2c_pfx = _mapping_prefixes("jsherp", "o2c")
            so1_oid = f"{o2c_pfx['O2cOrder']}{seeded['so1_id']}"
            assert rt.submit_action("JSH-O2C-CONF", "confirm_o2c_order", {"order_id": so1_oid}, sales)["status"] == "COMMITTED"
            admin.clear_tenant_logs(tid)

            so2_oid = f"{o2c_pfx['O2cOrder']}{seeded['so2_id']}"
            del2_oid = f"{o2c_pfx['O2cDelivery']}{seeded['ck2_id']}"
            inv2_oid = f"{o2c_pfx['O2cInvoice']}{seeded['sk2_id']}"
            assert (
                rt.submit_action(
                    "JSH-O2C-TAG",
                    "tag_jsh_order_depot",
                    {"order_id": so2_oid, "depot_tag": "EAST-HUB"},
                    sales,
                )["status"]
                == "COMMITTED"
            )
            with pytest.raises(InvariantError) as o2c_blk:
                rt.submit_action("JSH-O2C-BLK", "post_o2c_invoice", {"invoice_id": inv2_oid}, fin)
            assert o2c_blk.value.code == "DELIVERY_REQUIRED_BEFORE_INVOICE"
            assert admin._mysql_exec(f"SELECT status FROM jsh_account_head WHERE id = {seeded['sk2_id']};").strip() == "0"

            assert rt.submit_action("JSH-O2C-DEL", "complete_o2c_delivery", {"delivery_id": del2_oid}, wh)["status"] == "COMMITTED"
            admin.clear_tenant_logs(tid)
            assert rt.submit_action("JSH-O2C-INV", "post_o2c_invoice", {"invoice_id": inv2_oid}, fin)["status"] == "COMMITTED"
            admin.clear_tenant_logs(tid)
            assert (
                rt.submit_action(
                    "JSH-O2C-PAY",
                    "record_o2c_payment",
                    {"invoice_id": inv2_oid, "payment_amount": 9600.0},
                    fin,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("O2cInvoice", inv2_oid, steward).payment_state == "paid"
            assert rt.get_object("O2cOrder", so2_oid, steward).jsh_depot_tag == "EAST-HUB"

            # --- D. Coupled H2R + Ontology-Owned AuthorityMandate + Clean Revoke/Offboard ---
            admin.clear_tenant_logs(tid)
            alice_eid = f"EMP-{seeded['emp_ids'][0]}"
            bob_eid = f"EMP-{seeded['emp_ids'][1]}"
            carol_eid = f"EMP-{seeded['emp_ids'][2]}"
            ops_dept = f"DEPT-{seeded['ops_org_id']}"
            sal_dept = f"DEPT-{seeded['sales_org_id']}"
            sal_pos = f"POS-{seeded['role_ids'][1]}"

            assert (
                rt.submit_action(
                    "JSH-H2R-REP",
                    "reparent_department",
                    {"department_id": ops_dept, "new_parent_id": sal_dept},
                    hr_adm,
                )["status"]
                == "COMMITTED"
            )
            admin.clear_tenant_logs(tid)
            assert (
                rt.submit_action(
                    "JSH-H2R-ASG",
                    "assign_employee_position",
                    {"employee_id": alice_eid, "department_id": sal_dept, "position_id": sal_pos, "activate": True},
                    hr_adm,
                )["status"]
                == "COMMITTED"
            )
            assert (
                rt.submit_action(
                    "JSH-H2R-NOTE",
                    "annotate_jsh_employee_roster",
                    {"employee_id": bob_eid, "roster_note": "Delegated sales lead"},
                    hr_adm,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("Employee", bob_eid, steward).jsh_roster_note == "Delegated sales lead"

            # Grant ontology-owned AuthorityMandate from Carol (finance_director) -> Bob
            assert (
                rt.submit_action(
                    "JSH-H2R-MND",
                    "grant_authority_mandate",
                    {
                        "mandate_id": "MND-J1",
                        "grantor_id": carol_eid,
                        "grantee_id": bob_eid,
                        "role_name": "finance_director",
                        "ceiling_cents": 20000000,
                        "scope_tag": "sales",
                    },
                    hr_dir,
                )["status"]
                == "COMMITTED"
            )
            org_dir = project_h2r_to_org_directory(rt, steward)
            auth_dec = evaluate_h2r_employee_authority(
                rt,
                steward,
                employee_or_actor_id=bob_eid,
                target_unit_id=sal_dept,
                amount_cents=15000000,
                required_role="finance_director",
                directory=org_dir,
            )
            assert auth_dec.mandate_ceiling_cents == 20000000
            assert auth_dec.effective_ceiling_cents == 20000000
            assert auth_dec.decision == "AUTO"

            # Offboarding Bob while active mandate exists MUST be blocked with zero jshERP write
            with pytest.raises(InvariantError) as off_blk:
                rt.submit_action("JSH-H2R-OFFBLK", "offboard_employee", {"employee_id": bob_eid, "reason": "resigned"}, hr_adm)
            assert off_blk.value.code == "OFFBOARDED_GRANTEE_ACTIVE_MANDATE_FORBIDDEN"
            assert admin._mysql_exec(f"SELECT status FROM jsh_user WHERE id = {seeded['emp_ids'][1]};").strip() == "0"

            assert (
                rt.submit_action("JSH-H2R-REV", "revoke_authority_mandate", {"mandate_id": "MND-J1"}, hr_adm)["status"]
                == "COMMITTED"
            )
            admin.clear_tenant_logs(tid)
            assert (
                rt.submit_action("JSH-H2R-OFF", "offboard_employee", {"employee_id": bob_eid, "reason": "clean exit"}, hr_adm)[
                    "status"
                ]
                == "COMMITTED"
            )
            assert rt.get_object("Employee", bob_eid, steward).status == "offboarded"
            assert admin._mysql_exec(f"SELECT status FROM jsh_user WHERE id = {seeded['emp_ids'][1]};").strip() == "2"

            # --- E. Standalone CRM + P2M + R2R (with FulfillmentEvent projection & Close Period) ---
            _exercise_standalone_crm_and_p2m(world, entity_id=ent_id, party_id=f"PTY-{seeded['cust_id']}")

            fp = spec.get_object_type("FiscalPeriod")(
                period_id="FP-J2026-11",
                accounting_entity=ent_id,
                start_date="2026-11-01",
                end_date="2026-11-30",
                status="open",
            )
            acc_bank = spec.get_object_type("GlAccount")(
                account_code="J-1002",
                accounting_entity=ent_id,
                name="Bank RMB",
                category="ASSET",
                normal_side="DEBIT",
            )
            acc_rev = spec.get_object_type("GlAccount")(
                account_code="J-6001",
                accounting_entity=ent_id,
                name="Main Revenue",
                category="REVENUE",
                normal_side="CREDIT",
            )
            tr_res = spec.get_object_type("TradeResource")(
                resource_id="RES-JS-1",
                code="JS-SKU-1",
                name="Trade SKU",
                unit="ton",
                spec_summary="Standard steel lot",
            )
            tc = spec.get_object_type("TradeCommitment")(
                commitment_id="CMT-JS-1",
                buyer_party=f"PTY-{seeded['cust_id']}",
                seller_party=f"PTY-{seeded['sup_id']}",
                resource="RES-JS-1",
                quantity=3.0,
                unit_price=3200.0,
                total_amount=9600.0,
                status="agreed",
            )
            fev = spec.get_object_type("FulfillmentEvent")(
                event_id="FEV-JS-1",
                event_kind="INVOICE",
                quantity=3.0,
                amount=9600.0,
                evidence_summary=f"AR receipt {seeded['sk2_id']}",
                status="confirmed",
                commitment="CMT-JS-1",
            )
            for obj in (fp, acc_bank, acc_rev, tr_res, tc, fev):
                rt.put_object(obj, actor=steward)

            project_event_to_draft_journal(
                rt,
                steward,
                entry_id="JE-JSH-1",
                accounting_entity=ent_id,
                fiscal_period="FP-J2026-11",
                source_event_id="FEV-JS-1",
                debit_account="J-1002",
                credit_account="J-6001",
                amount_cents=960000,
                memo="Recognize jshERP O2C revenue",
            )
            assert rt.submit_action("JSH-R2R-POST", "post_journal_entry", {"entry_id": "JE-JSH-1"}, chief)["status"] == "COMMITTED"
            tb_jsh = rt.call_function("compute_trial_balance", {"accounting_entity": ent_id, "fiscal_period": "FP-J2026-11"}, steward)
            assert tb_jsh["is_balanced"] is True
            assert tb_jsh["total_debit_cents"] == 960000
            assert tb_jsh["total_credit_cents"] == 960000
            assert (
                rt.submit_action(
                    "JSH-R2R-CLS",
                    "close_fiscal_period",
                    {"period_id": "FP-J2026-11", "snapshot_id": "TBS-JSH-2026-11"},
                    chief,
                )["status"]
                == "COMMITTED"
            )
            assert rt.get_object("FiscalPeriod", "FP-J2026-11", steward).status == "closed"

            # --- F. Counterfactual runtime.fork() isolation ---
            with rt.fork() as sim_rt:
                assert (
                    sim_rt.submit_action(
                        "JSH-FORK-TAG",
                        "tag_jsh_order_depot",
                        {"order_id": so2_oid, "depot_tag": "FORK-WEST"},
                        sales,
                    )["status"]
                    == "COMMITTED"
                )
                assert sim_rt.get_object("O2cOrder", so2_oid, steward).jsh_depot_tag == "FORK-WEST"
            assert rt.get_object("O2cOrder", so2_oid, steward).jsh_depot_tag == "EAST-HUB"
    finally:
        admin.cleanup_all_wo24c_tenants()
        rem = admin.remaining_wo24c_counts()
        assert rem == {"jsh_user": 0, "jsh_tenant": 0}, f"jshERP cleanup residual rows: {rem}"
