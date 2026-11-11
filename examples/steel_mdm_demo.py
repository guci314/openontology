"""钢厂主数据两大死结的破局领域模型与 7 步演示。

纯业务本体（PIM），不包含任何外部系统、传输协议或数据库客户端代码。

- 死结一：采购随合同建散码 -> ContractSKU 字段级 overlay 寄生归并，不改 ERP 底表。
- 死结二：采购按规格、生产按厂家 -> MaterialLot 高维批次桶 + 按品牌 FIFO 投料，
  再用 reconcile_multi_view_ledger 无损拆回规格与原始发票散码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from open_ontology import (
    Actor,
    InvariantError,
    LinkType,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
)

# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class MaterialSpec(ObjectType):
    """物理规格型号（采购比价 / ERP 报产口径，完全由 Ontology 拥有）。"""

    __primary_key__ = "spec_id"
    __title__ = "name"
    __owned__ = True

    spec_id: str
    name: str
    ash_max_pct: float
    sulfur_max_pct: float


class Manufacturer(ObjectType):
    """生产厂家品牌（MES 领料 / 成本科核算口径，source-backed）。"""

    __primary_key__ = "brand_id"
    __title__ = "name"
    __owned__ = False

    brand_id: str
    name: str


class ContractSKU(ObjectType):
    """ERP 采购开票散码（源字段来自老 ERP，归并字段归 Ontology 自有）。"""

    __primary_key__ = "sku_id"
    __title__ = "invoice_title"
    __owned__ = {"canonical_spec_id": None, "mapped_by": None}

    sku_id: str
    invoice_title: str
    brand_id: str
    unit_price_ton: int
    ash_pct: float
    sulfur_pct: float
    canonical_spec_id: str | None = None
    mapped_by: str | None = None


class MaterialLot(ObjectType):
    """进厂原料批次桶：连接散码、物理规格、厂家品牌的高维锚点。"""

    __primary_key__ = "lot_id"
    __title__ = "lot_id"
    __owned__ = True

    lot_id: str
    sku_id: str
    spec_id: str
    brand_id: str
    initial_tons: int
    remaining_tons: int
    consumed_tons: int = 0
    unit_price_ton: int


# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------
SKU_MAPS_TO_SPEC = LinkType(
    name="sku_maps_to_spec",
    source="ContractSKU",
    target="MaterialSpec",
    cardinality="many-to-one",
    owned=True,
)
LOT_HAS_SPEC = LinkType(
    name="lot_has_spec",
    source="MaterialLot",
    target="MaterialSpec",
    cardinality="many-to-one",
    owned=True,
)
LOT_PRODUCED_BY = LinkType(
    name="lot_produced_by",
    source="MaterialLot",
    target="Manufacturer",
    cardinality="many-to-one",
    owned=True,
)
LOT_BILLED_AS = LinkType(
    name="lot_billed_as",
    source="MaterialLot",
    target="ContractSKU",
    cardinality="many-to-one",
    owned=True,
)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (MaterialSpec, Manufacturer, ContractSKU, MaterialLot):
    spec.object_type(_cls)
for _link in (SKU_MAPS_TO_SPEC, LOT_HAS_SPEC, LOT_PRODUCED_BY, LOT_BILLED_AS):
    spec.add_link_type(_link)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class RecommendParams(BaseModel):
    ash_pct: float | None = None
    sulfur_pct: float | None = None
    name_hint: str | None = None


class ReconcileParams(BaseModel):
    include_zero: bool = False


class MapSkuParams(BaseModel):
    sku_id: str
    canonical_spec_id: str
    mapped_by: str | None = None


class RegisterSkuParams(BaseModel):
    sku_id: str
    invoice_title: str
    brand_id: str
    unit_price_ton: int
    ash_pct: float
    sulfur_pct: float
    canonical_spec_id: str | None = None
    request_new_spec_id: str | None = None
    request_new_spec_name: str | None = None


class ReceiveLotParams(BaseModel):
    lot_id: str
    sku_id: str
    initial_tons: int


class ConsumeBrandParams(BaseModel):
    brand_id: str
    tons: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _quality_fits(spec_obj: MaterialSpec, ash_pct: float, sulfur_pct: float) -> bool:
    return (
        ash_pct <= spec_obj.ash_max_pct
        and sulfur_pct <= spec_obj.sulfur_max_pct
    )


def _register_needs_approval(
    params: RegisterSkuParams, actor: Actor
) -> bool:
    """A brand-new physical variety always needs the MDM steward."""
    return params.request_new_spec_id is not None


def _register_post_approval(ctx: Any, params: RegisterSkuParams) -> None:
    """Ontology-owned follow-up: create a new spec if requested, then link it."""
    sku = ctx.require(ContractSKU, params.sku_id)
    target_spec_id = params.request_new_spec_id or params.canonical_spec_id
    if target_spec_id is None:  # pragma: no cover - guarded by the action
        raise InvariantError(
            "contract sku has no physical spec target",
            code="UNMAPPED_PHYSICAL_SPEC",
        )

    spec_obj = ctx.get(MaterialSpec, target_spec_id)
    if spec_obj is None:
        if params.request_new_spec_id is None:
            raise InvariantError(
                f"canonical spec {target_spec_id!r} does not exist",
                code="SPEC_NOT_FOUND",
            )
        spec_obj = MaterialSpec(
            spec_id=params.request_new_spec_id,
            name=params.request_new_spec_name or params.invoice_title,
            ash_max_pct=params.ash_pct,
            sulfur_max_pct=params.sulfur_pct,
        )
        ctx.create(spec_obj)

    ctx.link(sku, SKU_MAPS_TO_SPEC, spec_obj)


# ---------------------------------------------------------------------------
# Read-only functions
# ---------------------------------------------------------------------------
@spec.function(
    "recommend_spec_for_sku",
    RecommendParams,
    description="按化验指标或名称匹配现有物理规格，供采购一键归并。",
)
def recommend_spec_for_sku(
    ctx: Any, params: RecommendParams
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for candidate in ctx.query(MaterialSpec):
        if params.ash_pct is not None and candidate.ash_max_pct < params.ash_pct:
            continue
        if (
            params.sulfur_pct is not None
            and candidate.sulfur_max_pct < params.sulfur_pct
        ):
            continue
        if (
            params.name_hint
            and params.name_hint.lower() not in candidate.name.lower()
        ):
            continue
        candidates.append(
            {
                "spec_id": candidate.spec_id,
                "name": candidate.name,
                "ash_max_pct": candidate.ash_max_pct,
                "sulfur_max_pct": candidate.sulfur_max_pct,
            }
        )
    candidates.sort(key=lambda item: item["spec_id"])
    return candidates


@spec.function(
    "reconcile_multi_view_ledger",
    ReconcileParams,
    description="按厂家品牌、物理规格、原始发票散码三视角无损对账。",
)
def reconcile_multi_view_ledger(
    ctx: Any, params: ReconcileParams
) -> dict[str, Any]:
    brand_rows: dict[str, dict[str, Any]] = {}
    spec_rows: dict[str, dict[str, Any]] = {}
    sku_rows: dict[str, dict[str, Any]] = {}
    total_tons = 0
    total_cost = 0

    lots = sorted(ctx.query(MaterialLot), key=lambda lot: lot.lot_id)
    for lot in lots:
        if lot.consumed_tons <= 0 and not params.include_zero:
            continue
        consumed = lot.consumed_tons
        cost = consumed * lot.unit_price_ton
        total_tons += consumed
        total_cost += cost

        brand_row = brand_rows.setdefault(
            lot.brand_id,
            {"brand_id": lot.brand_id, "tons": 0, "cost": 0},
        )
        brand_row["tons"] += consumed
        brand_row["cost"] += cost

        spec_row = spec_rows.setdefault(
            lot.spec_id,
            {"spec_id": lot.spec_id, "tons": 0, "cost": 0},
        )
        spec_row["tons"] += consumed
        spec_row["cost"] += cost

        sku_row = sku_rows.setdefault(
            lot.sku_id,
            {"sku_id": lot.sku_id, "tons": 0, "cost": 0},
        )
        sku_row["tons"] += consumed
        sku_row["cost"] += cost

    for row in brand_rows.values():
        manufacturer = ctx.get(Manufacturer, row["brand_id"])
        row["brand_name"] = manufacturer.name if manufacturer else None
        row["avg_price_ton"] = (
            row["cost"] / row["tons"] if row["tons"] else 0.0
        )

    for row in spec_rows.values():
        material_spec = ctx.get(MaterialSpec, row["spec_id"])
        row["spec_name"] = material_spec.name if material_spec else None
        row["avg_price_ton"] = (
            row["cost"] / row["tons"] if row["tons"] else 0.0
        )

    for row in sku_rows.values():
        sku = ctx.get(ContractSKU, row["sku_id"])
        row["invoice_title"] = sku.invoice_title if sku else None
        row["avg_price_ton"] = (
            row["cost"] / row["tons"] if row["tons"] else 0.0
        )

    by_brand = sorted(brand_rows.values(), key=lambda row: row["brand_id"])
    by_spec = sorted(spec_rows.values(), key=lambda row: row["spec_id"])
    by_sku = sorted(sku_rows.values(), key=lambda row: row["sku_id"])

    brand_tons = sum(row["tons"] for row in by_brand)
    spec_tons = sum(row["tons"] for row in by_spec)
    sku_tons = sum(row["tons"] for row in by_sku)
    brand_cost = sum(row["cost"] for row in by_brand)
    spec_cost = sum(row["cost"] for row in by_spec)
    sku_cost = sum(row["cost"] for row in by_sku)

    return {
        "by_brand": by_brand,
        "by_spec": by_spec,
        "by_sku": by_sku,
        "totals": {"tons": total_tons, "cost": total_cost},
        "views": {
            "brand_tons": brand_tons,
            "spec_tons": spec_tons,
            "sku_tons": sku_tons,
            "brand_cost": brand_cost,
            "spec_cost": spec_cost,
            "sku_cost": sku_cost,
        },
        "balanced": (
            brand_tons == spec_tons == sku_tons == total_tons
            and brand_cost == spec_cost == sku_cost == total_cost
        ),
    }


# ---------------------------------------------------------------------------
# Governed actions
# ---------------------------------------------------------------------------
@spec.action(
    "map_legacy_sku_to_spec",
    MapSkuParams,
    allowed_roles=("buyer", "ai_agent", "mdm_steward"),
    affects_source=False,
    description="存量散码零侵入寄生归并：只写 Ontology 自有字段与归并边。",
)
def map_legacy_sku_to_spec(ctx: Any, params: MapSkuParams) -> None:
    sku = ctx.require(ContractSKU, params.sku_id)
    material_spec = ctx.require(MaterialSpec, params.canonical_spec_id)

    if not _quality_fits(material_spec, sku.ash_pct, sku.sulfur_pct):
        raise InvariantError(
            f"sku {sku.sku_id} quality does not fit {material_spec.spec_id}",
            code="SPEC_QUALITY_MISMATCH",
        )

    ctx.modify(
        sku,
        canonical_spec_id=material_spec.spec_id,
        mapped_by=params.mapped_by or ctx.actor.actor_id,
    )
    ctx.link(sku, SKU_MAPS_TO_SPEC, material_spec)


@spec.action(
    "register_contract_sku",
    RegisterSkuParams,
    allowed_roles=("buyer", "ai_agent", "mdm_steward"),
    approver_roles=("mdm_steward",),
    approval_rule=_register_needs_approval,
    affects_source=True,
    post_approval=_register_post_approval,
    description="增量建码硬护栏：必须绑定规格；新物理品种必须主数据管理员审批。",
)
def register_contract_sku(ctx: Any, params: RegisterSkuParams) -> None:
    if not params.canonical_spec_id and not params.request_new_spec_id:
        raise InvariantError(
            "采购新建开票散码必须绑定现有物理规格，或申请新增物理品种审批",
            code="UNMAPPED_PHYSICAL_SPEC",
        )
    if params.unit_price_ton <= 0:
        raise InvariantError(
            "unit_price_ton must be positive",
            code="INVALID_UNIT_PRICE",
        )
    if ctx.get(Manufacturer, params.brand_id) is None:
        raise InvariantError(
            f"manufacturer {params.brand_id!r} not found",
            code="BRAND_NOT_FOUND",
        )

    if params.request_new_spec_id is not None:
        target_spec_id = params.request_new_spec_id
    else:
        material_spec = ctx.require(
            MaterialSpec, params.canonical_spec_id
        )
        if not _quality_fits(
            material_spec, params.ash_pct, params.sulfur_pct
        ):
            raise InvariantError(
                f"sku quality does not fit {material_spec.spec_id}",
                code="SPEC_QUALITY_MISMATCH",
            )
        target_spec_id = params.canonical_spec_id

    sku = ContractSKU(
        sku_id=params.sku_id,
        invoice_title=params.invoice_title,
        brand_id=params.brand_id,
        unit_price_ton=params.unit_price_ton,
        ash_pct=params.ash_pct,
        sulfur_pct=params.sulfur_pct,
        canonical_spec_id=target_spec_id,
        mapped_by=ctx.actor.actor_id,
    )
    ctx.create(sku)
    # The spec/link creation happens in the post-approval hook: the main
    # edit plan stays source-only, the hook is ontology-only.


@spec.action(
    "receive_material_lot",
    ReceiveLotParams,
    allowed_roles=("buyer", "mdm_steward", "warehouse", "ops_manager"),
    affects_source=False,
    description="按已治理散码入库，建立规格/厂家/发票三边齐备的高维批次桶。",
)
def receive_material_lot(ctx: Any, params: ReceiveLotParams) -> None:
    sku = ctx.require(ContractSKU, params.sku_id)
    if not sku.canonical_spec_id:
        raise InvariantError(
            f"contract sku {sku.sku_id} is not mapped to a physical spec",
            code="SKU_NOT_GOVERNED",
        )
    material_spec = ctx.get(MaterialSpec, sku.canonical_spec_id)
    if material_spec is None:
        raise InvariantError(
            f"contract sku {sku.sku_id} points to missing spec "
            f"{sku.canonical_spec_id!r}",
            code="SKU_NOT_GOVERNED",
        )
    manufacturer = ctx.get(Manufacturer, sku.brand_id)
    if manufacturer is None:
        raise InvariantError(
            f"contract sku {sku.sku_id} points to missing manufacturer "
            f"{sku.brand_id!r}",
            code="SKU_NOT_GOVERNED",
        )

    if params.initial_tons <= 0:
        raise InvariantError(
            "initial_tons must be positive",
            code="INVALID_LOT_QUANTITY",
        )

    lot = MaterialLot(
        lot_id=params.lot_id,
        sku_id=sku.sku_id,
        spec_id=material_spec.spec_id,
        brand_id=manufacturer.brand_id,
        initial_tons=params.initial_tons,
        remaining_tons=params.initial_tons,
        consumed_tons=0,
        unit_price_ton=sku.unit_price_ton,
    )
    ctx.create(lot)
    ctx.link(lot, LOT_HAS_SPEC, material_spec)
    ctx.link(lot, LOT_PRODUCED_BY, manufacturer)
    ctx.link(lot, LOT_BILLED_AS, sku)


@spec.action(
    "consume_by_brand_fifo",
    ConsumeBrandParams,
    allowed_roles=("mes", "mes_operator", "production", "ops_manager"),
    affects_source=False,
    description="MES 只按厂家报工；本体自动按 FIFO 扣减高维批次桶。",
)
def consume_by_brand_fifo(ctx: Any, params: ConsumeBrandParams) -> None:
    if params.tons <= 0:
        raise InvariantError(
            "consumption tons must be positive",
            code="INVALID_CONSUMPTION",
        )

    lots = [
        lot
        for lot in ctx.query(MaterialLot)
        if lot.brand_id == params.brand_id and lot.remaining_tons > 0
    ]
    lots.sort(key=lambda lot: lot.lot_id)

    available = sum(lot.remaining_tons for lot in lots)
    if available < params.tons:
        raise InvariantError(
            f"brand {params.brand_id} has only {available} tons available, "
            f"cannot consume {params.tons}",
            code="INSUFFICIENT_BRAND_STOCK",
        )

    remaining = params.tons
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.remaining_tons, remaining)
        ctx.modify(
            lot,
            remaining_tons=lot.remaining_tons - take,
            consumed_tons=lot.consumed_tons + take,
        )
        remaining -= take


# ---------------------------------------------------------------------------
# Demo seed
# ---------------------------------------------------------------------------
BUYER = Actor(
    actor_id="buyer-01",
    role="buyer",
    clearance_tags={"*"},
)
AI_AGENT = Actor(
    actor_id="ai-agent-01",
    role="ai_agent",
    clearance_tags={"*"},
)
STEWARD = Actor(
    actor_id="mdm-steward-01",
    role="mdm_steward",
    clearance_tags={"*"},
)
MES = Actor(
    actor_id="mes-operator-01",
    role="mes_operator",
    clearance_tags={"*"},
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
