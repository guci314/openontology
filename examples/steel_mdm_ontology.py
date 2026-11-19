"""钢厂主数据两大死结纯业务本体（SteelMDMOntology · PIM）。

纯业务本体（PIM），不包含任何外部系统、传输协议或运行时/数据库代码。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from open_ontology.spec import (
    Actor,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)

__all__ = [
    "MaterialSpec",
    "Manufacturer",
    "ContractSKU",
    "MaterialLot",
    "SKU_MAPS_TO_SPEC",
    "LOT_HAS_SPEC",
    "LOT_PRODUCED_BY",
    "LOT_BILLED_AS",
    "RecommendParams",
    "ReconcileParams",
    "MapSkuParams",
    "RegisterSkuParams",
    "ReceiveLotParams",
    "ConsumeBrandParams",
    "spec",
    "recommend_spec_for_sku",
    "reconcile_multi_view_ledger",
    "map_legacy_sku_to_spec",
    "register_contract_sku",
    "receive_material_lot",
    "consume_by_brand_fifo",
    "BUYER",
    "AI_AGENT",
    "STEWARD",
    "MES",
]


# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class MaterialSpec(ObjectType):
    """物理规格型号（采购比价 / 报产口径，完全由 Ontology 拥有）。"""

    __primary_key__ = "spec_id"
    __title__ = "name"
    __owned__ = True

    spec_id: str
    name: str
    ash_max_pct: float
    sulfur_max_pct: float


class Manufacturer(ObjectType):
    """生产厂家品牌（领料 / 成本核算口径，source-backed）。"""

    __primary_key__ = "brand_id"
    __title__ = "name"
    __owned__ = False

    brand_id: str
    name: str


class ContractSKU(ObjectType):
    """采购开票散码（源字段来自外部系统，归并字段归 Ontology 自有）。"""

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
# Object properties
# ---------------------------------------------------------------------------
SKU_MAPS_TO_SPEC = ObjectProperty(
    name="sku_maps_to_spec",
    domain="ContractSKU",
    range="MaterialSpec",
    cardinality="many-to-one",
    owned=True,
)
LOT_HAS_SPEC = ObjectProperty(
    name="lot_has_spec",
    domain="MaterialLot",
    range="MaterialSpec",
    cardinality="many-to-one",
    owned=True,
)
LOT_PRODUCED_BY = ObjectProperty(
    name="lot_produced_by",
    domain="MaterialLot",
    range="Manufacturer",
    cardinality="many-to-one",
    owned=True,
)
LOT_BILLED_AS = ObjectProperty(
    name="lot_billed_as",
    domain="MaterialLot",
    range="ContractSKU",
    cardinality="many-to-one",
    owned=True,
)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (MaterialSpec, Manufacturer, ContractSKU, MaterialLot):
    spec.object_type(_cls)
for _prop in (SKU_MAPS_TO_SPEC, LOT_HAS_SPEC, LOT_PRODUCED_BY, LOT_BILLED_AS):
    spec.add_object_property(_prop)


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
    description="按厂家报工后自动按 FIFO 扣减高维批次桶。",
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


spec.update_display(
    {
        "ontologies": {
            "steel_mdm": "钢厂主数据破局与三视角对账",
        },
        "categories": {
            "Master Data": "主数据治理",
        },
        "objects": {
            "MaterialSpec": "物料规格",
            "Manufacturer": "生产厂商",
            "ContractSKU": "合同物料编码",
            "MaterialLot": "进厂批次",
        },
        "object_properties": {
            "sku_maps_to_spec": "物料归并规格",
            "lot_has_spec": "批次对应规格",
            "lot_produced_by": "批次生产厂商",
            "lot_billed_as": "批次结算编码",
        },
        "fields": {
            "spec_id": "规格编号",
            "ash_max_pct": "灰分上限（%）",
            "sulfur_max_pct": "硫分上限（%）",
            "brand_id": "品牌编号",
            "sku_id": "物料编码",
            "invoice_title": "开票名称",
            "unit_price_ton": "吨价",
            "ash_pct": "灰分（%）",
            "sulfur_pct": "硫分（%）",
            "canonical_spec_id": "归并规格编号",
            "mapped_by": "归并人",
            "lot_id": "批次编号",
            "initial_tons": "入库吨数",
            "remaining_tons": "剩余吨数",
            "consumed_tons": "已耗吨数",
        },
        "actions": {
            "map_legacy_sku_to_spec": "归并历史物料编码",
            "register_contract_sku": "登记合同物料编码",
            "receive_material_lot": "登记进厂批次",
            "consume_by_brand_fifo": "按品牌先进先出投料",
        },
        "functions": {
            "recommend_spec_for_sku": "推荐物料规格",
            "reconcile_multi_view_ledger": "多视角台账对账",
        },
        "roles": {
            "buyer": "采购员",
            "mdm_steward": "主数据管理员",
            "mes_operator": "高炉操作员",
            "ops_manager": "运营主管",
            "warehouse": "库管员",
            "mes": "制造执行系统",
            "production": "生产主管",
        },
    }
)


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
