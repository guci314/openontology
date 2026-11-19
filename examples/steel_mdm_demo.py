"""钢厂主数据两大死结的破局领域模型与 7 步演示。

纯业务本体（PIM），不包含任何外部系统、传输协议或数据库客户端代码。

- 死结一：采购随合同建散码 -> ContractSKU 字段级 overlay 寄生归并，不改 ERP 底表。
- 死结二：采购按规格、生产按厂家 -> MaterialLot 高维批次桶 + 按品牌 FIFO 投料，
  再用 reconcile_multi_view_ledger 无损拆回规格与原始发票散码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_ontology import (
    Actor,
    InvariantError,
    OntologyRuntime,
)
from .steel_mdm_ontology import (
    AI_AGENT,
    BUYER,
    ConsumeBrandParams,
    ContractSKU,
    LOT_BILLED_AS,
    LOT_HAS_SPEC,
    LOT_PRODUCED_BY,
    MES,
    Manufacturer,
    MapSkuParams,
    MaterialLot,
    MaterialSpec,
    ReceiveLotParams,
    RecommendParams,
    ReconcileParams,
    RegisterSkuParams,
    SKU_MAPS_TO_SPEC,
    STEWARD,
    consume_by_brand_fifo,
    map_legacy_sku_to_spec,
    receive_material_lot,
    recommend_spec_for_sku,
    reconcile_multi_view_ledger,
    register_contract_sku,
    spec,
)


@dataclass
class SteelDemo:
    runtime: OntologyRuntime
    spec_ids: tuple[str, ...] = ("SPEC-A", "SPEC-B")
    brand_ids: tuple[str, ...] = ("BRAND-1", "BRAND-2")
    sku_ids: tuple[str, ...] = (
        "ERP-SKU-9001",
        "ERP-SKU-9002",
        "ERP-SKU-9003",
        "ERP-SKU-9004",
    )
    lot_ids: tuple[str, ...] = ("LOT-01", "LOT-02", "LOT-03")


def _seed_specs(runtime: OntologyRuntime) -> None:
    for material_spec in (
        MaterialSpec(
            spec_id="SPEC-A",
            name="一级冶金焦 (CSR>=65%)",
            ash_max_pct=12.5,
            sulfur_max_pct=0.7,
        ),
        MaterialSpec(
            spec_id="SPEC-B",
            name="二级冶金焦 (CSR>=55%)",
            ash_max_pct=13.5,
            sulfur_max_pct=0.9,
        ),
    ):
        runtime.put_object(material_spec)


def _seed_manufacturers(runtime: OntologyRuntime) -> None:
    runtime.put_object(Manufacturer(brand_id="BRAND-1", name="山西焦化"))
    runtime.put_object(Manufacturer(brand_id="BRAND-2", name="河北焦化"))


def _seed_legacy_skus(runtime: OntologyRuntime) -> None:
    for sku in (
        ContractSKU(
            sku_id="ERP-SKU-9001",
            invoice_title="山西焦化11月特供高强焦(含税专票)",
            brand_id="BRAND-1",
            unit_price_ton=2000,
            ash_pct=12.0,
            sulfur_pct=0.6,
        ),
        ContractSKU(
            sku_id="ERP-SKU-9002",
            invoice_title="山西焦化冬季合同焦A批",
            brand_id="BRAND-1",
            unit_price_ton=2050,
            ash_pct=11.8,
            sulfur_pct=0.5,
        ),
        ContractSKU(
            sku_id="ERP-SKU-9003",
            invoice_title="山西焦化二级焦散码",
            brand_id="BRAND-1",
            unit_price_ton=1600,
            ash_pct=13.2,
            sulfur_pct=0.8,
        ),
        ContractSKU(
            sku_id="ERP-SKU-9004",
            invoice_title="河北焦化一级焦合同码",
            brand_id="BRAND-2",
            unit_price_ton=1950,
            ash_pct=12.2,
            sulfur_pct=0.6,
        ),
    ):
        runtime.put_object(sku)


def _map_legacy(runtime: OntologyRuntime) -> None:
    mappings = {
        "ERP-SKU-9001": "SPEC-A",
        "ERP-SKU-9002": "SPEC-A",
        "ERP-SKU-9003": "SPEC-B",
        "ERP-SKU-9004": "SPEC-A",
    }
    for sku_id, spec_id in mappings.items():
        runtime.submit_action(
            f"MAP-{sku_id}",
            "map_legacy_sku_to_spec",
            {
                "sku_id": sku_id,
                "canonical_spec_id": spec_id,
                "mapped_by": BUYER.actor_id,
            },
            BUYER,
        )


def _receive_lots(runtime: OntologyRuntime) -> None:
    for lot_id, sku_id, tons in (
        ("LOT-01", "ERP-SKU-9001", 100),
        ("LOT-02", "ERP-SKU-9003", 80),
        ("LOT-03", "ERP-SKU-9004", 120),
    ):
        runtime.submit_action(
            f"RECV-{lot_id}",
            "receive_material_lot",
            {"lot_id": lot_id, "sku_id": sku_id, "initial_tons": tons},
            BUYER,
        )


def build_demo(with_lots: bool = True) -> SteelDemo:
    runtime = OntologyRuntime(spec)
    _seed_specs(runtime)
    _seed_manufacturers(runtime)
    _seed_legacy_skus(runtime)
    _map_legacy(runtime)
    if with_lots:
        _receive_lots(runtime)
    return SteelDemo(runtime=runtime)


def snapshot_source_rows(runtime: OntologyRuntime) -> dict[str, list[dict[str, Any]]]:
    """Source-backed rows only; owned fields are stripped for reindex."""
    manufacturers = [
        row.source_properties()
        for row in runtime.query_objects(Manufacturer, STEWARD)
    ]
    contract_skus = [
        row.source_properties()
        for row in runtime.query_objects(ContractSKU, STEWARD)
    ]
    return {
        "Manufacturer": manufacturers,
        "ContractSKU": contract_skus,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_sku_matrix(runtime: OntologyRuntime) -> None:
    for sku in runtime.query_objects(ContractSKU, STEWARD):
        print(
            f"  {sku.sku_id}  brand={sku.brand_id:<8}"
            f" price={sku.unit_price_ton:>5}"
            f" ash={sku.ash_pct:<4} sulfur={sku.sulfur_pct:<4}"
            f" spec={sku.canonical_spec_id}"
        )


def main() -> dict[str, Any]:
    demo = build_demo(with_lots=True)
    runtime = demo.runtime

    _section("1. 老 ERP 存量：4 个混乱散码 + 同一厂家跨两个规格")
    print("  MaterialSpec:", [s.spec_id for s in runtime.query_objects(MaterialSpec, STEWARD)])
    print("  Manufacturer:", [m.brand_id for m in runtime.query_objects(Manufacturer, STEWARD)])
    _print_sku_matrix(runtime)
    print("  多对多：BRAND-1(山西焦化) 同时供应 SPEC-A 与 SPEC-B")

    _section("2. 存量治乱：零侵入寄生归并 + ERP 重刷后 overlay 存活")
    before = {
        sku.sku_id: sku.canonical_spec_id
        for sku in runtime.query_objects(ContractSKU, STEWARD)
    }
    snapshot = snapshot_source_rows(runtime)
    runtime.load_snapshot(
        {"Manufacturer": snapshot["Manufacturer"],
         "ContractSKU": snapshot["ContractSKU"]}
    )
    after = {
        sku.sku_id: sku.canonical_spec_id
        for sku in runtime.query_objects(ContractSKU, STEWARD)
    }
    print(f"  mapped before reindex: {before}")
    print(f"  mapped after  reindex: {after}")
    print(f"  overlay preserved: {before == after}")
    print(
        "  sku_maps_to_spec links: "
        f"{[(sku.sku_id, [s.spec_id for s in runtime.get_linked(sku, SKU_MAPS_TO_SPEC, MaterialSpec, STEWARD)]) for sku in runtime.query_objects(ContractSKU, STEWARD)]}"
    )

    _section("3. 源头增量：硬护栏 + 以次充好 + 新物理品种审批")
    try:
        runtime.submit_action(
            "REG-UNMAPPED",
            "register_contract_sku",
            {
                "sku_id": "ERP-SKU-9100",
                "invoice_title": "无归属野码",
                "brand_id": "BRAND-1",
                "unit_price_ton": 1900,
                "ash_pct": 12.0,
                "sulfur_pct": 0.6,
            },
            BUYER,
        )
    except InvariantError as exc:
        print(f"  blocked unmapped: code={exc.code}")

    try:
        runtime.submit_action(
            "REG-MISMATCH",
            "register_contract_sku",
            {
                "sku_id": "ERP-SKU-9101",
                "invoice_title": "以次充好挂一级焦",
                "brand_id": "BRAND-1",
                "unit_price_ton": 2000,
                "ash_pct": 13.2,
                "sulfur_pct": 0.8,
                "canonical_spec_id": "SPEC-A",
            },
            BUYER,
        )
    except InvariantError as exc:
        print(f"  blocked mismatch: code={exc.code}")

    pending = runtime.submit_action(
        "REG-NEW-SPEC",
        "register_contract_sku",
        {
            "sku_id": "ERP-SKU-9102",
            "invoice_title": "河北焦化新规格试采购",
            "brand_id": "BRAND-2",
            "unit_price_ton": 1400,
            "ash_pct": 14.0,
            "sulfur_pct": 1.0,
            "request_new_spec_id": "SPEC-C",
            "request_new_spec_name": "三级冶金焦 (CSR>=45%)",
        },
        BUYER,
    )
    print(f"  new spec request status={pending['status']}")
    spec_c_exists = runtime.connection.execute(
        "SELECT 1 FROM ontology_objects "
        "WHERE object_type='MaterialSpec' AND object_id='SPEC-C'"
    ).fetchone() is not None
    print(f"  before approval: SPEC-C exists={spec_c_exists}")
    approved = runtime.approve_action("REG-NEW-SPEC", STEWARD)
    print(f"  approved by={approved['approved_by']} status={approved['status']}")
    c_spec = runtime.get_object(MaterialSpec, "SPEC-C", STEWARD)
    c_sku = runtime.get_object(ContractSKU, "ERP-SKU-9102", STEWARD)
    print(
        f"  SPEC-C={c_spec.name!r} ash_max={c_spec.ash_max_pct}"
        f" sku={c_sku.sku_id} canonical={c_sku.canonical_spec_id}"
    )
    print(
        "  SPEC-C link: "
        f"{[s.spec_id for s in runtime.get_linked(c_sku, SKU_MAPS_TO_SPEC, MaterialSpec, STEWARD)]}"
    )

    _section("4. 高维批次桶：规格 / 厂家 / 原始发票三边齐备")
    for lot in runtime.query_objects(MaterialLot, STEWARD):
        print(
            f"  {lot.lot_id}  sku={lot.sku_id:<14} spec={lot.spec_id:<7}"
            f" brand={lot.brand_id:<8} remaining={lot.remaining_tons:>4}"
            f" @{lot.unit_price_ton}"
        )

    _section("5. 多对多死结高潮：MES 只按厂家报工 BRAND-1 150 吨")
    consume = runtime.submit_action(
        "CONSUME-BRAND-1",
        "consume_by_brand_fifo",
        {"brand_id": "BRAND-1", "tons": 150},
        MES,
    )
    print(f"  consume run={consume['run_id']} status={consume['status']}")
    for lot in runtime.query_objects(MaterialLot, STEWARD):
        print(
            f"  {lot.lot_id}: initial={lot.initial_tons}"
            f" remaining={lot.remaining_tons} consumed={lot.consumed_tons}"
        )

    _section("6. 三视角无损对账：成本科 / 采购报产 / 财务发票")
    view = runtime.call_function(
        "reconcile_multi_view_ledger", {}, STEWARD
    )
    print("  视角 1 成本科（按厂家品牌）:")
    for row in view["by_brand"]:
        print(
            f"    {row['brand_id']} {row['brand_name']}: "
            f"{row['tons']} 吨 / {row['cost']} 元"
            f" / 均价 {row['avg_price_ton']:.2f}"
        )
    print("  视角 2 采购 & ERP 报产（按物理规格）:")
    for row in view["by_spec"]:
        print(
            f"    {row['spec_id']} {row['spec_name']}: "
            f"{row['tons']} 吨 / {row['cost']} 元"
            f" / 均价 {row['avg_price_ton']:.2f}"
        )
    print("  视角 3 财务税务（按原始发票散码）:")
    for row in view["by_sku"]:
        print(
            f"    {row['sku_id']}: {row['tons']} 吨 / "
            f"{row['cost']} 元 / {row['invoice_title']}"
        )
    print(f"  totals={view['totals']} balanced={view['balanced']}")

    print("  ObjectSet + pivot + aggregate:")
    brand_lots = runtime.search_set(
        MaterialLot, STEWARD, {"brand_id": "BRAND-1"}
    )
    specs = runtime.pivot(brand_lots, LOT_HAS_SPEC, STEWARD)
    skus = runtime.pivot(brand_lots, LOT_BILLED_AS, STEWARD)
    agg = runtime.aggregate(
        brand_lots, group_by="spec_id", sum_field="consumed_tons"
    )
    print(f"    brand_lots={brand_lots.ids} -> specs={specs.ids}")
    print(f"    -> skus={skus.ids}")
    for row in agg.rows:
        print(f"    aggregate spec={row.key!r} consumed={row.metrics['sum']} 吨")

    _section("7. 时光机撤回投料：库存恢复如初")
    reverts = runtime.revert_action(
        "CONSUME-BRAND-1", "CONSUME-BRAND-1-REV", MES
    )
    print(f"  revert run={reverts['run_id']} status={reverts['status']}")
    for lot in runtime.query_objects(MaterialLot, STEWARD):
        print(
            f"  {lot.lot_id}: remaining={lot.remaining_tons}"
            f" consumed={lot.consumed_tons}"
        )

    summary = {
        "specs": [s.spec_id for s in runtime.query_objects(MaterialSpec, STEWARD)],
        "skus": [s.sku_id for s in runtime.query_objects(ContractSKU, STEWARD)],
        "lots": [l.lot_id for l in runtime.query_objects(MaterialLot, STEWARD)],
        "consumed_before_revert": view["totals"],
        "balanced": view["balanced"],
        "inventory_after_revert": {
            lot.lot_id: {
                "remaining": lot.remaining_tons,
                "consumed": lot.consumed_tons,
            }
            for lot in runtime.query_objects(MaterialLot, STEWARD)
        },
        "rejected_runs": [
            run["run_id"]
            for run in runtime.list_action_runs("REJECTED")
        ],
    }
    _section("DONE")
    print("  summary:", summary)
    return summary


if __name__ == "__main__":
    main()
