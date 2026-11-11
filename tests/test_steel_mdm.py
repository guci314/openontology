"""Acceptance tests for the steel MDM two-deadlock demo (TK-ONTO-005)."""

from __future__ import annotations

import json

import pytest

from examples import steel_mdm_demo as steel
from open_ontology import InvariantError, ObjectNotFoundError


@pytest.fixture()
def demo():
    instance = steel.build_demo(with_lots=True)
    yield instance
    instance.runtime.close()


@pytest.fixture()
def base_demo():
    instance = steel.build_demo(with_lots=False)
    yield instance
    instance.runtime.close()


def _sku(runtime, sku_id):
    return runtime.get_object(steel.ContractSKU, sku_id, steel.STEWARD)


def _lot(runtime, lot_id):
    return runtime.get_object(steel.MaterialLot, lot_id, steel.STEWARD)


def _linked_spec_ids(runtime, sku_id):
    sku = _sku(runtime, sku_id)
    return [
        spec.spec_id
        for spec in runtime.get_linked(
            sku, steel.SKU_MAPS_TO_SPEC, steel.MaterialSpec, steel.STEWARD
        )
    ]


def _new_spec_params(**overrides):
    params = {
        "sku_id": "ERP-SKU-9102",
        "invoice_title": "河北焦化新规格试采购",
        "brand_id": "BRAND-2",
        "unit_price_ton": 1400,
        "ash_pct": 14.0,
        "sulfur_pct": 1.0,
        "request_new_spec_id": "SPEC-C",
        "request_new_spec_name": "三级冶金焦 (CSR>=45%)",
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# 1. Legacy mapping survives a full source reindex
# ---------------------------------------------------------------------------
def test_legacy_mapping_and_overlay_survive_reindex(demo):
    runtime = demo.runtime
    expected = {
        "ERP-SKU-9001": "SPEC-A",
        "ERP-SKU-9002": "SPEC-A",
        "ERP-SKU-9003": "SPEC-B",
        "ERP-SKU-9004": "SPEC-A",
    }
    for sku_id, spec_id in expected.items():
        assert _sku(runtime, sku_id).canonical_spec_id == spec_id
        assert _linked_spec_ids(runtime, sku_id) == [spec_id]

    spec_count = runtime.count_objects("MaterialSpec")
    lot_count = runtime.count_objects("MaterialLot")
    snapshot = steel.snapshot_source_rows(runtime)
    runtime.load_snapshot(
        {
            "Manufacturer": snapshot["Manufacturer"],
            "ContractSKU": snapshot["ContractSKU"],
        }
    )

    for sku_id, spec_id in expected.items():
        assert _sku(runtime, sku_id).canonical_spec_id == spec_id
        assert _linked_spec_ids(runtime, sku_id) == [spec_id]

    # fully-owned specs and lots were not supplied, therefore untouched
    assert runtime.count_objects("MaterialSpec") == spec_count
    assert runtime.count_objects("MaterialLot") == lot_count

    overlay = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type='ContractSKU' AND object_id='ERP-SKU-9001'"
    ).fetchone()
    patch = json.loads(overlay["patch_json"])
    assert patch["canonical_spec_id"] == "SPEC-A"
    assert patch["mapped_by"] == steel.BUYER.actor_id


# ---------------------------------------------------------------------------
# 2. Unmapped new SKU is blocked and audited
# ---------------------------------------------------------------------------
def test_unmapped_registration_rejected_and_audited(base_demo):
    runtime = base_demo.runtime
    params = {
        "sku_id": "ERP-SKU-9100",
        "invoice_title": "无归属野码",
        "brand_id": "BRAND-1",
        "unit_price_ton": 1900,
        "ash_pct": 12.0,
        "sulfur_pct": 0.6,
    }
    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "REG-UNMAPPED", "register_contract_sku", params, steel.BUYER
        )
    assert excinfo.value.code == "UNMAPPED_PHYSICAL_SPEC"

    run = runtime.get_action_run("REG-UNMAPPED")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "UNMAPPED_PHYSICAL_SPEC"
    assert runtime.count_objects("ContractSKU") == 4
    with pytest.raises(ObjectNotFoundError):
        _sku(runtime, "ERP-SKU-9100")


# ---------------------------------------------------------------------------
# 3. Quality mismatch (inferior material mapped to premium spec) is blocked
# ---------------------------------------------------------------------------
def test_quality_mismatch_rejected(base_demo):
    runtime = base_demo.runtime
    params = {
        "sku_id": "ERP-SKU-9101",
        "invoice_title": "以次充好挂一级焦",
        "brand_id": "BRAND-1",
        "unit_price_ton": 2000,
        "ash_pct": 13.2,
        "sulfur_pct": 0.8,
        "canonical_spec_id": "SPEC-A",
    }
    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "REG-MISMATCH", "register_contract_sku", params, steel.BUYER
        )
    assert excinfo.value.code == "SPEC_QUALITY_MISMATCH"

    run = runtime.get_action_run("REG-MISMATCH")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "SPEC_QUALITY_MISMATCH"
    assert runtime.count_objects("ContractSKU") == 4


# ---------------------------------------------------------------------------
# 4. New physical variety requires steward approval and only then appears
# ---------------------------------------------------------------------------
def test_new_spec_requires_approval_then_creates_spec_and_sku(base_demo):
    runtime = base_demo.runtime
    params = _new_spec_params()

    simulation = runtime.simulate_action(
        "register_contract_sku", params, steel.BUYER
    )
    assert simulation.valid is True
    assert simulation.requires_approval is True
    created = {
        (change.object_type, change.object_id, change.kind)
        for change in simulation.diff.object_changes
    }
    assert ("ContractSKU", "ERP-SKU-9102", "create") in created
    assert ("MaterialSpec", "SPEC-C", "create") in created
    assert runtime.count_objects("MaterialSpec") == 2

    pending = runtime.submit_action(
        "REG-NEW-SPEC", "register_contract_sku", params, steel.BUYER
    )
    assert pending["status"] == "PENDING_APPROVAL"
    with pytest.raises(ObjectNotFoundError):
        runtime.get_object(steel.MaterialSpec, "SPEC-C", steel.STEWARD)
    with pytest.raises(ObjectNotFoundError):
        _sku(runtime, "ERP-SKU-9102")

    approved = runtime.approve_action("REG-NEW-SPEC", steel.STEWARD)
    assert approved["status"] == "COMMITTED"
    assert approved["approved_by"] == steel.STEWARD.actor_id

    new_spec = runtime.get_object(
        steel.MaterialSpec, "SPEC-C", steel.STEWARD
    )
    assert new_spec.name == "三级冶金焦 (CSR>=45%)"
    assert new_spec.ash_max_pct == 14.0
    assert new_spec.sulfur_max_pct == 1.0

    new_sku = _sku(runtime, "ERP-SKU-9102")
    assert new_sku.canonical_spec_id == "SPEC-C"
    assert new_sku.mapped_by == steel.BUYER.actor_id
    assert _linked_spec_ids(runtime, "ERP-SKU-9102") == ["SPEC-C"]


def test_revert_new_spec_removes_spec_sku_and_link(base_demo):
    runtime = base_demo.runtime
    runtime.submit_action(
        "REG-NEW-SPEC",
        "register_contract_sku",
        _new_spec_params(),
        steel.BUYER,
    )
    runtime.approve_action("REG-NEW-SPEC", steel.STEWARD)

    revert = runtime.revert_action(
        "REG-NEW-SPEC", "REG-NEW-SPEC-REV", steel.STEWARD
    )
    assert revert["status"] == "COMMITTED"
    assert runtime.get_action_run("REG-NEW-SPEC")["status"] == "REVERTED"

    with pytest.raises(ObjectNotFoundError):
        runtime.get_object(steel.MaterialSpec, "SPEC-C", steel.STEWARD)
    with pytest.raises(ObjectNotFoundError):
        _sku(runtime, "ERP-SKU-9102")
    assert runtime.count_objects("MaterialSpec") == 2
    assert runtime.count_objects("ContractSKU") == 4


# ---------------------------------------------------------------------------
# 5. Binding an existing spec is instant and links the SKU
# ---------------------------------------------------------------------------
def test_existing_spec_registration_is_instant_and_linked(base_demo):
    runtime = base_demo.runtime
    params = {
        "sku_id": "ERP-SKU-9103",
        "invoice_title": "河北焦化二级焦新合同码",
        "brand_id": "BRAND-2",
        "unit_price_ton": 1500,
        "ash_pct": 13.0,
        "sulfur_pct": 0.8,
        "canonical_spec_id": "SPEC-B",
    }
    run = runtime.submit_action(
        "REG-EXISTING", "register_contract_sku", params, steel.BUYER
    )
    assert run["status"] == "COMMITTED"
    assert run["approved_by"] is None

    sku = _sku(runtime, "ERP-SKU-9103")
    assert sku.canonical_spec_id == "SPEC-B"
    assert sku.mapped_by == steel.BUYER.actor_id
    assert _linked_spec_ids(runtime, "ERP-SKU-9103") == ["SPEC-B"]

    overlay = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type='ContractSKU' AND object_id='ERP-SKU-9103'"
    ).fetchone()
    assert json.loads(overlay["patch_json"])["canonical_spec_id"] == "SPEC-B"


# ---------------------------------------------------------------------------
# 6. Receiving requires a governed SKU
# ---------------------------------------------------------------------------
def test_receive_lot_requires_governed_sku(base_demo):
    runtime = base_demo.runtime
    runtime.put_object(
        steel.ContractSKU(
            sku_id="ERP-SKU-9999",
            invoice_title="未归并散码",
            brand_id="BRAND-1",
            unit_price_ton=1900,
            ash_pct=12.0,
            sulfur_pct=0.6,
        )
    )
    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "RECV-UNMAPPED",
            "receive_material_lot",
            {"lot_id": "LOT-X", "sku_id": "ERP-SKU-9999", "initial_tons": 10},
            steel.BUYER,
        )
    assert excinfo.value.code == "SKU_NOT_GOVERNED"
    assert runtime.get_action_run("RECV-UNMAPPED")["status"] == "REJECTED"
    assert runtime.count_objects("MaterialLot") == 0


# ---------------------------------------------------------------------------
# 7. Receiving creates a high-dimensional lot with three orthogonal links
# ---------------------------------------------------------------------------
def test_receive_lot_creates_high_dimensional_bucket(base_demo):
    runtime = base_demo.runtime
    run = runtime.submit_action(
        "RECV-LOT-04",
        "receive_material_lot",
        {"lot_id": "LOT-04", "sku_id": "ERP-SKU-9002", "initial_tons": 50},
        steel.BUYER,
    )
    assert run["status"] == "COMMITTED"

    lot = _lot(runtime, "LOT-04")
    assert lot.sku_id == "ERP-SKU-9002"
    assert lot.spec_id == "SPEC-A"
    assert lot.brand_id == "BRAND-1"
    assert lot.initial_tons == 50
    assert lot.remaining_tons == 50
    assert lot.consumed_tons == 0
    assert lot.unit_price_ton == 2050

    specs = runtime.get_linked(
        lot, steel.LOT_HAS_SPEC, steel.MaterialSpec, steel.STEWARD
    )
    brands = runtime.get_linked(
        lot, steel.LOT_PRODUCED_BY, steel.Manufacturer, steel.STEWARD
    )
    skus = runtime.get_linked(
        lot, steel.LOT_BILLED_AS, steel.ContractSKU, steel.STEWARD
    )
    assert [item.spec_id for item in specs] == ["SPEC-A"]
    assert [item.brand_id for item in brands] == ["BRAND-1"]
    assert [item.sku_id for item in skus] == ["ERP-SKU-9002"]


# ---------------------------------------------------------------------------
# 8. MES consumes by brand; ontology splits the lot FIFO
# ---------------------------------------------------------------------------
def test_consume_by_brand_fifo_splits_multi_to_many(demo):
    runtime = demo.runtime
    run = runtime.submit_action(
        "CONSUME-BRAND-1",
        "consume_by_brand_fifo",
        {"brand_id": "BRAND-1", "tons": 150},
        steel.MES,
    )
    assert run["status"] == "COMMITTED"

    lot1 = _lot(runtime, "LOT-01")
    lot2 = _lot(runtime, "LOT-02")
    lot3 = _lot(runtime, "LOT-03")
    assert (lot1.remaining_tons, lot1.consumed_tons) == (0, 100)
    assert (lot2.remaining_tons, lot2.consumed_tons) == (30, 50)
    assert (lot3.remaining_tons, lot3.consumed_tons) == (120, 0)


def test_insufficient_brand_stock_is_refused(demo):
    runtime = demo.runtime
    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "CONSUME-TOO-MUCH",
            "consume_by_brand_fifo",
            {"brand_id": "BRAND-2", "tons": 200},
            steel.MES,
        )
    assert excinfo.value.code == "INSUFFICIENT_BRAND_STOCK"
    assert runtime.get_action_run("CONSUME-TOO-MUCH")["status"] == "REJECTED"
    assert _lot(runtime, "LOT-03").remaining_tons == 120
    assert _lot(runtime, "LOT-01").remaining_tons == 100
    assert _lot(runtime, "LOT-02").remaining_tons == 80


# ---------------------------------------------------------------------------
# 9. Three-view reconciliation is exact to the yuan
# ---------------------------------------------------------------------------
def test_reconcile_three_views_balanced(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "CONSUME-BRAND-1",
        "consume_by_brand_fifo",
        {"brand_id": "BRAND-1", "tons": 150},
        steel.MES,
    )

    view = runtime.call_function(
        "reconcile_multi_view_ledger", {}, steel.STEWARD
    )
    assert view["balanced"] is True
    assert view["totals"] == {"tons": 150, "cost": 280_000}
    assert view["views"] == {
        "brand_tons": 150,
        "spec_tons": 150,
        "sku_tons": 150,
        "brand_cost": 280_000,
        "spec_cost": 280_000,
        "sku_cost": 280_000,
    }

    by_brand = {row["brand_id"]: row for row in view["by_brand"]}
    assert by_brand["BRAND-1"]["tons"] == 150
    assert by_brand["BRAND-1"]["cost"] == 280_000
    assert by_brand["BRAND-1"]["brand_name"] == "山西焦化"

    by_spec = {row["spec_id"]: row for row in view["by_spec"]}
    assert by_spec["SPEC-A"]["tons"] == 100
    assert by_spec["SPEC-A"]["cost"] == 200_000
    assert by_spec["SPEC-A"]["avg_price_ton"] == 2000.0
    assert by_spec["SPEC-B"]["tons"] == 50
    assert by_spec["SPEC-B"]["cost"] == 80_000
    assert by_spec["SPEC-B"]["avg_price_ton"] == 1600.0

    by_sku = {row["sku_id"]: row for row in view["by_sku"]}
    assert by_sku["ERP-SKU-9001"]["tons"] == 100
    assert by_sku["ERP-SKU-9001"]["cost"] == 200_000
    assert by_sku["ERP-SKU-9003"]["tons"] == 50
    assert by_sku["ERP-SKU-9003"]["cost"] == 80_000
    assert by_sku["ERP-SKU-9003"]["invoice_title"] == "山西焦化二级焦散码"


# ---------------------------------------------------------------------------
# 10. Time-machine revert restores the physical inventory
# ---------------------------------------------------------------------------
def test_revert_consumption_restores_inventory(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "CONSUME-BRAND-1",
        "consume_by_brand_fifo",
        {"brand_id": "BRAND-1", "tons": 150},
        steel.MES,
    )
    revert = runtime.revert_action(
        "CONSUME-BRAND-1", "CONSUME-BRAND-1-REV", steel.MES
    )
    assert revert["status"] == "COMMITTED"
    assert runtime.get_action_run("CONSUME-BRAND-1")["status"] == "REVERTED"

    assert _lot(runtime, "LOT-01").remaining_tons == 100
    assert _lot(runtime, "LOT-01").consumed_tons == 0
    assert _lot(runtime, "LOT-02").remaining_tons == 80
    assert _lot(runtime, "LOT-02").consumed_tons == 0

    view = runtime.call_function(
        "reconcile_multi_view_ledger", {}, steel.STEWARD
    )
    assert view["totals"] == {"tons": 0, "cost": 0}
    assert view["balanced"] is True


# ---------------------------------------------------------------------------
# 11. AI/spec recommendation helper
# ---------------------------------------------------------------------------
def test_recommend_spec_for_sku(base_demo):
    runtime = base_demo.runtime

    infeasible = runtime.call_function(
        "recommend_spec_for_sku",
        {"ash_pct": 13.2, "sulfur_pct": 0.8},
        steel.STEWARD,
    )
    assert [row["spec_id"] for row in infeasible] == ["SPEC-B"]

    both = runtime.call_function(
        "recommend_spec_for_sku",
        {"ash_pct": 12.0, "sulfur_pct": 0.6},
        steel.STEWARD,
    )
    assert [row["spec_id"] for row in both] == ["SPEC-A", "SPEC-B"]

    by_name = runtime.call_function(
        "recommend_spec_for_sku",
        {"name_hint": "二级"},
        steel.STEWARD,
    )
    assert [row["spec_id"] for row in by_name] == ["SPEC-B"]


# ---------------------------------------------------------------------------
# 12. ObjectSet + pivot + aggregate resolves the many-to-many split
# ---------------------------------------------------------------------------
def test_object_set_pivot_aggregate_multi_to_many(demo):
    runtime = demo.runtime
    runtime.submit_action(
        "CONSUME-BRAND-1",
        "consume_by_brand_fifo",
        {"brand_id": "BRAND-1", "tons": 150},
        steel.MES,
    )

    brand_lots = runtime.search_set(
        steel.MaterialLot, steel.STEWARD, {"brand_id": "BRAND-1"}
    )
    assert set(brand_lots.ids) == {"LOT-01", "LOT-02"}

    specs = runtime.pivot(
        brand_lots, steel.LOT_HAS_SPEC, steel.STEWARD
    )
    assert set(specs.ids) == {"SPEC-A", "SPEC-B"}

    skus = runtime.pivot(
        brand_lots, steel.LOT_BILLED_AS, steel.STEWARD
    )
    assert set(skus.ids) == {"ERP-SKU-9001", "ERP-SKU-9003"}

    brands = runtime.pivot(
        brand_lots, steel.LOT_PRODUCED_BY, steel.STEWARD
    )
    assert brands.ids == ["BRAND-1"]

    agg = runtime.aggregate(
        brand_lots, group_by="spec_id", sum_field="consumed_tons"
    )
    metrics = {row.key: row.metrics["sum"] for row in agg.rows}
    assert metrics == {"SPEC-A": 100, "SPEC-B": 50}
