"""河北钢厂主数据两大死结生产级 M1 插件（SteelMDMPlugin · Pure M1）。

挂接 ``std_core_ontology`` 细腰之上（复用 ``BusinessParty``、``TradeResource``、``WorkCard``），
提供两条生产级受治理闭环能力（零外部 I/O、零供应商硬编码）：

1. **死结 1（旁路散码“前松后紧”）**：
   * ``receive_steel_lot`` 允许采购未归并的旁路散码以 ``governance_state="PROVISIONAL"``
     正常过磅卸入 ``SteelStorageBin``（绝不阻断地磅车队），并自动挂靠或签发 ``WorkCard``；
   * 月底关账（``close_steel_cost_period``）与批次结算放行（``authorize_steel_lot_settlement``）
     构筑不可逾越的硬卡口：存在任何 ``PROVISIONAL`` 批次或未归并散码时固定抛出
     ``InvariantError(..., code="PROVISIONAL_SKU_BLOCKS_SETTLEMENT")``，须先由 Steward
     批结工单、归并散码并调用 ``reconcile_provisional_lot`` 转正并回填消耗账目方可通过。
2. **死结 2（多对多“规格 A/B × 品牌 1/2”无损物理溯源）**：
   * ``consume_steel_from_bin`` 直接从料仓 ``SteelStorageBin`` 报工扣料并写入
     ``SteelConsumptionEntry(cost_allocation_rule="BIN_EXACT")``，即使同品牌供不同规格
     也 100% 精确溯源；
   * ``consume_steel_by_brand_fifo`` 兼容无料仓计量工位，强制标记
     ``cost_allocation_rule="BRAND_FIFO"``，让精确物理溯源与财务估算分摊绝对分离；
   * 只读函数 ``reconcile_steel_three_view_ledger`` 输出按厂家、规格、发票散码三视角
     守恒台账及按分摊口径拆分统计。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from open_ontology.spec import (
    Actor,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)

try:
    from .business_ontology import MasterEntity, ProcessDocument, WorkCard
    from .std_core_ontology import (
        BusinessParty,
        TradeResource,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import MasterEntity, ProcessDocument, WorkCard
    from examples.std_core_ontology import (
        BusinessParty,
        TradeResource,
        spec as std_core_spec,
    )

__all__ = [
    "spec",
    "SteelMaterialSpec",
    "SteelManufacturer",
    "SteelContractSKU",
    "SteelStorageBin",
    "SteelMaterialLot",
    "SteelConsumptionEntry",
    "SteelCostPeriod",
    "STEEL_SKU_MAPS_TO_SPEC",
    "STEEL_LOT_IN_BIN",
    "STEEL_LOT_PRODUCED_BY",
    "STEEL_LOT_BILLED_AS",
    "STEEL_LOT_HAS_SPEC",
    "STEEL_ENTRY_FROM_LOT",
    "CreateSteelMaterialSpecParams",
    "CreateSteelStorageBinParams",
    "OpenSteelCostPeriodParams",
    "ReceiveSteelLotParams",
    "MapSteelSkuParams",
    "ReconcileProvisionalLotParams",
    "AuthorizeSteelLotSettlementParams",
    "CloseSteelCostPeriodParams",
    "ConsumeSteelFromBinParams",
    "ConsumeSteelByBrandFifoParams",
    "RecommendSteelSpecParams",
    "ReconcileSteelLedgerParams",
    "create_steel_material_spec",
    "create_steel_storage_bin",
    "open_steel_cost_period",
    "receive_steel_lot",
    "map_steel_sku_to_spec",
    "reconcile_provisional_lot",
    "authorize_steel_lot_settlement",
    "close_steel_cost_period",
    "consume_steel_from_bin",
    "consume_steel_by_brand_fifo",
    "recommend_steel_spec_for_sku",
    "reconcile_steel_three_view_ledger",
    "BUYER",
    "AI_AGENT",
    "STEWARD",
    "WEIGHBRIDGE",
    "MES_OPERATOR",
    "COST_ACCOUNTANT",
]

_COST_PERIOD_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "OPEN": ("CLOSED",),
    "CLOSED": (),
}

spec = OntologySpec(extends=(std_core_spec,))
spec.role("mdm_steward", "钢厂主数据管理员：负责物理规格核准、散码归并与临时批次转正")
spec.role("weighbridge", "地磅司磅员：负责过磅入仓卸料，不阻断旁路散码车队")
spec.role("mes_operator", "生产报工员：负责按料仓或按品牌记录投料消耗")
spec.role("cost_accountant", "成本会计：负责成本关账卡口与批次结算放行")


# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
@spec.object_type
class SteelMaterialSpec(MasterEntity):
    """物理规格型号（挂载至 REA TradeResource，完全由 Ontology 拥有）。"""

    __primary_key__: ClassVar[str] = "spec_id"
    __title__: ClassVar[str] = "name"
    __owned__: ClassVar[bool] = True

    spec_id: str
    resource: ObjectProperty[TradeResource]
    name: str
    grade: str = ""
    ash_max_pct: float
    sulfur_max_pct: float


@spec.object_type
class SteelManufacturer(MasterEntity):
    """生产厂家品牌（关联 BusinessParty，源端主数据）。"""

    __primary_key__: ClassVar[str] = "brand_id"
    __title__: ClassVar[str] = "name"
    __owned__: ClassVar[bool] = False

    brand_id: str
    party: ObjectProperty[BusinessParty]
    name: str
    status: str = "active"


@spec.object_type
class SteelContractSKU(MasterEntity):
    """ERP 存量与旁路开票散码（ERP 源字段只读保留，归并字段归 Ontology 自有）。"""

    __primary_key__: ClassVar[str] = "sku_id"
    __title__: ClassVar[str] = "invoice_title"
    __owned__: ClassVar[dict[str, Any]] = {"canonical_spec_id": None, "mapped_by": None}

    sku_id: str
    invoice_title: str
    brand_id: str
    unit_price_ton: float
    ash_pct: float
    sulfur_pct: float
    canonical_spec_id: str | None = None
    mapped_by: str | None = None


@spec.object_type
class SteelStorageBin(MasterEntity):
    """物理料仓/料堆观测锚点（完全由 Ontology 拥有）。"""

    __primary_key__: ClassVar[str] = "bin_id"
    __title__: ClassVar[str] = "bin_id"
    __owned__: ClassVar[bool] = True

    bin_id: str
    yard_name: str
    material_category: str = "coking_coal"
    status: Literal["ACTIVE", "MAINTENANCE", "CLOSED"] = "ACTIVE"


@spec.object_type
class SteelMaterialLot(ObjectType):
    """高维进厂批次桶：锚死散码 × 品牌 × 料仓 × 可选物理规格，显式区分 GOVERNED / PROVISIONAL。"""

    __primary_key__: ClassVar[str] = "lot_id"
    __title__: ClassVar[str] = "lot_id"
    __owned__: ClassVar[bool] = True

    lot_id: str
    sku_id: str
    brand_id: str
    bin_id: str
    spec_id: str | None = None
    period_id: str = ""
    governance_state: Literal["GOVERNED", "PROVISIONAL"] = "PROVISIONAL"
    settlement_authorized: bool = False
    initial_tons: float
    remaining_tons: float
    consumed_tons: float = 0.0
    unit_price_ton: float
    pending_work_card_id: str = ""


@spec.object_type
class SteelConsumptionEntry(ObjectType):
    """报工消耗账目：显式分离 BIN_EXACT 物理精确溯源与 BRAND_FIFO 财务估算分摊。"""

    __primary_key__: ClassVar[str] = "entry_id"
    __title__: ClassVar[str] = "entry_id"
    __owned__: ClassVar[bool] = True

    entry_id: str
    lot_id: str
    brand_id: str
    spec_id: str | None = None
    bin_id: str
    tons: float
    cost_amount: float
    cost_allocation_rule: Literal["BIN_EXACT", "BRAND_FIFO"]
    period_id: str = ""


@spec.object_type
class SteelCostPeriod(ProcessDocument):
    """成本关账期间（严格状态机：OPEN -> CLOSED）。"""

    __primary_key__: ClassVar[str] = "period_id"
    __title__: ClassVar[str] = "period_id"
    __owned__: ClassVar[bool] = True
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("OPEN",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _COST_PERIOD_TRANSITIONS

    period_id: str
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    closed_tons: float = 0.0
    closed_cost: float = 0.0


# ---------------------------------------------------------------------------
# Owned object properties
# ---------------------------------------------------------------------------
STEEL_SKU_MAPS_TO_SPEC = ObjectProperty(
    name="steel_sku_maps_to_spec",
    domain="SteelContractSKU",
    range="SteelMaterialSpec",
    cardinality="many-to-one",
    owned=True,
)
STEEL_LOT_IN_BIN = ObjectProperty(
    name="steel_lot_in_bin",
    domain="SteelMaterialLot",
    range="SteelStorageBin",
    cardinality="many-to-one",
    owned=True,
)
STEEL_LOT_PRODUCED_BY = ObjectProperty(
    name="steel_lot_produced_by",
    domain="SteelMaterialLot",
    range="SteelManufacturer",
    cardinality="many-to-one",
    owned=True,
)
STEEL_LOT_BILLED_AS = ObjectProperty(
    name="steel_lot_billed_as",
    domain="SteelMaterialLot",
    range="SteelContractSKU",
    cardinality="many-to-one",
    owned=True,
)
STEEL_LOT_HAS_SPEC = ObjectProperty(
    name="steel_lot_has_spec",
    domain="SteelMaterialLot",
    range="SteelMaterialSpec",
    cardinality="many-to-one",
    owned=True,
)
STEEL_ENTRY_FROM_LOT = ObjectProperty(
    name="steel_entry_from_lot",
    domain="SteelConsumptionEntry",
    range="SteelMaterialLot",
    cardinality="many-to-one",
    owned=True,
)

for _prop in (
    STEEL_SKU_MAPS_TO_SPEC,
    STEEL_LOT_IN_BIN,
    STEEL_LOT_PRODUCED_BY,
    STEEL_LOT_BILLED_AS,
    STEEL_LOT_HAS_SPEC,
    STEEL_ENTRY_FROM_LOT,
):
    spec.add_object_property(_prop)


# ---------------------------------------------------------------------------
# Action & Function Parameter Schemas
# ---------------------------------------------------------------------------
class CreateSteelMaterialSpecParams(BaseModel):
    spec_id: str
    resource_id: str
    name: str
    grade: str = ""
    ash_max_pct: float
    sulfur_max_pct: float


class CreateSteelStorageBinParams(BaseModel):
    bin_id: str
    yard_name: str
    material_category: str = "coking_coal"
    status: Literal["ACTIVE", "MAINTENANCE", "CLOSED"] = "ACTIVE"


class OpenSteelCostPeriodParams(BaseModel):
    period_id: str


class ReceiveSteelLotParams(BaseModel):
    lot_id: str
    sku_id: str
    bin_id: str
    initial_tons: float
    period_id: str = ""
    pending_work_card_id: str = ""
    open_review_card: bool = True


class MapSteelSkuParams(BaseModel):
    sku_id: str
    canonical_spec_id: str
    mapped_by: str | None = None
    work_card_id: str = ""


class ReconcileProvisionalLotParams(BaseModel):
    lot_id: str
    work_card_id: str = ""


class AuthorizeSteelLotSettlementParams(BaseModel):
    lot_id: str


class CloseSteelCostPeriodParams(BaseModel):
    period_id: str


class ConsumeSteelFromBinParams(BaseModel):
    entry_id: str
    bin_id: str
    tons: float
    period_id: str = ""


class ConsumeSteelByBrandFifoParams(BaseModel):
    entry_id: str
    brand_id: str
    tons: float
    period_id: str = ""


class RecommendSteelSpecParams(BaseModel):
    ash_pct: float | None = None
    sulfur_pct: float | None = None
    name_hint: str | None = None


class ReconcileSteelLedgerParams(BaseModel):
    period_id: str = ""
    include_zero: bool = False


# ---------------------------------------------------------------------------
# Pure Helpers
# ---------------------------------------------------------------------------
def _quality_fits(spec_obj: SteelMaterialSpec, ash_pct: float, sulfur_pct: float) -> bool:
    return ash_pct <= spec_obj.ash_max_pct and sulfur_pct <= spec_obj.sulfur_max_pct


def _deduct_lots(
    ctx: Any,
    *,
    entry_id: str,
    lots: list[SteelMaterialLot],
    requested_tons: float,
    allocation_rule: Literal["BIN_EXACT", "BRAND_FIFO"],
    period_id: str,
    insufficient_code: str,
    insufficient_msg: str,
) -> None:
    if requested_tons <= 0:
        raise InvariantError(
            "consumption tons must be positive",
            code="INVALID_CONSUMPTION",
        )
    if ctx.get(SteelConsumptionEntry, entry_id) is not None:
        raise InvariantError(
            f"consumption entry {entry_id!r} already exists",
            code="DUPLICATE_CONSUMPTION_ENTRY",
        )
    available = sum(lot.remaining_tons for lot in lots)
    if available + 1e-9 < requested_tons:
        raise InvariantError(insufficient_msg, code=insufficient_code)

    remaining_to_draw = float(requested_tons)
    splits: list[tuple[SteelMaterialLot, float]] = []
    for lot in lots:
        if remaining_to_draw <= 1e-9:
            break
        take = min(float(lot.remaining_tons), remaining_to_draw)
        if take <= 1e-9:
            continue
        new_remaining = max(0.0, round(float(lot.remaining_tons) - take, 6))
        new_consumed = round(float(lot.consumed_tons) + take, 6)
        ctx.modify(lot, remaining_tons=new_remaining, consumed_tons=new_consumed)
        splits.append((lot, round(take, 6)))
        remaining_to_draw = round(remaining_to_draw - take, 6)

    multi = len(splits) > 1
    for idx, (lot, take) in enumerate(splits, start=1):
        eid = f"{entry_id}#{idx}" if multi else entry_id
        if ctx.get(SteelConsumptionEntry, eid) is not None:
            raise InvariantError(
                f"consumption entry {eid!r} already exists",
                code="DUPLICATE_CONSUMPTION_ENTRY",
            )
        entry = SteelConsumptionEntry(
            entry_id=eid,
            lot_id=lot.lot_id,
            brand_id=lot.brand_id,
            spec_id=lot.spec_id,
            bin_id=lot.bin_id,
            tons=take,
            cost_amount=round(take * float(lot.unit_price_ton), 6),
            cost_allocation_rule=allocation_rule,
            period_id=period_id or lot.period_id,
        )
        ctx.create(entry)
        ctx.link(entry, STEEL_ENTRY_FROM_LOT, lot)


# ---------------------------------------------------------------------------
# Read-only functions
# ---------------------------------------------------------------------------
@spec.function(
    "recommend_steel_spec_for_sku",
    RecommendSteelSpecParams,
    description="按化验灰分/硫分阈值或名称模糊匹配候选 SteelMaterialSpec。",
)
def recommend_steel_spec_for_sku(
    ctx: Any, params: RecommendSteelSpecParams
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for candidate in ctx.query(SteelMaterialSpec):
        if params.ash_pct is not None and candidate.ash_max_pct < params.ash_pct:
            continue
        if params.sulfur_pct is not None and candidate.sulfur_max_pct < params.sulfur_pct:
            continue
        if params.name_hint and params.name_hint.lower() not in candidate.name.lower():
            continue
        candidates.append(
            {
                "spec_id": candidate.spec_id,
                "name": candidate.name,
                "grade": candidate.grade,
                "ash_max_pct": candidate.ash_max_pct,
                "sulfur_max_pct": candidate.sulfur_max_pct,
            }
        )
    candidates.sort(key=lambda item: item["spec_id"])
    return candidates


@spec.function(
    "reconcile_steel_three_view_ledger",
    ReconcileSteelLedgerParams,
    description="输出按厂家、物理规格、发票散码三视角守恒台账，并拆分 BIN_EXACT / BRAND_FIFO 与在途 PROVISIONAL 批次。",
)
def reconcile_steel_three_view_ledger(
    ctx: Any, params: ReconcileSteelLedgerParams
) -> dict[str, Any]:
    brand_rows: dict[str, dict[str, Any]] = {}
    spec_rows: dict[str, dict[str, Any]] = {}
    sku_rows: dict[str, dict[str, Any]] = {}

    total_tons = 0.0
    total_cost = 0.0
    bin_exact_tons = 0.0
    brand_fifo_tons = 0.0

    entries = sorted(ctx.query(SteelConsumptionEntry), key=lambda e: e.entry_id)
    for entry in entries:
        if params.period_id and entry.period_id != params.period_id:
            continue
        if entry.tons <= 0 and not params.include_zero:
            continue
        lot = ctx.require(SteelMaterialLot, entry.lot_id)
        tons = float(entry.tons)
        cost = float(entry.cost_amount)
        total_tons += tons
        total_cost += cost
        if entry.cost_allocation_rule == "BIN_EXACT":
            bin_exact_tons += tons
        else:
            brand_fifo_tons += tons

        b_row = brand_rows.setdefault(
            entry.brand_id, {"brand_id": entry.brand_id, "tons": 0.0, "cost": 0.0}
        )
        b_row["tons"] = round(b_row["tons"] + tons, 6)
        b_row["cost"] = round(b_row["cost"] + cost, 6)

        spec_key = entry.spec_id if entry.spec_id else "UNMAPPED"
        sp_row = spec_rows.setdefault(
            spec_key, {"spec_id": spec_key, "tons": 0.0, "cost": 0.0}
        )
        sp_row["tons"] = round(sp_row["tons"] + tons, 6)
        sp_row["cost"] = round(sp_row["cost"] + cost, 6)

        sk_row = sku_rows.setdefault(
            lot.sku_id, {"sku_id": lot.sku_id, "tons": 0.0, "cost": 0.0}
        )
        sk_row["tons"] = round(sk_row["tons"] + tons, 6)
        sk_row["cost"] = round(sk_row["cost"] + cost, 6)

    for row in brand_rows.values():
        m = ctx.get(SteelManufacturer, row["brand_id"])
        row["brand_name"] = m.name if m else None
        row["avg_price_ton"] = round(row["cost"] / row["tons"], 4) if row["tons"] else 0.0

    for row in spec_rows.values():
        ms = ctx.get(SteelMaterialSpec, row["spec_id"]) if row["spec_id"] != "UNMAPPED" else None
        row["spec_name"] = ms.name if ms else None
        row["avg_price_ton"] = round(row["cost"] / row["tons"], 4) if row["tons"] else 0.0

    for row in sku_rows.values():
        sku = ctx.get(SteelContractSKU, row["sku_id"])
        row["invoice_title"] = sku.invoice_title if sku else None
        row["avg_price_ton"] = round(row["cost"] / row["tons"], 4) if row["tons"] else 0.0

    lots = sorted(ctx.query(SteelMaterialLot), key=lambda lot: lot.lot_id)
    provisional_lot_ids = [
        lot.lot_id
        for lot in lots
        if (not params.period_id or lot.period_id == params.period_id)
        and (lot.governance_state == "PROVISIONAL" or not lot.spec_id)
    ]
    lot_consumed_tons = round(
        sum(
            float(lot.consumed_tons)
            for lot in lots
            if not params.period_id or lot.period_id == params.period_id
        ),
        6,
    )

    by_brand = sorted(brand_rows.values(), key=lambda r: r["brand_id"])
    by_spec = sorted(spec_rows.values(), key=lambda r: r["spec_id"])
    by_sku = sorted(sku_rows.values(), key=lambda r: r["sku_id"])

    total_tons = round(total_tons, 6)
    total_cost = round(total_cost, 6)
    brand_tons = round(sum(r["tons"] for r in by_brand), 6)
    spec_tons = round(sum(r["tons"] for r in by_spec), 6)
    sku_tons = round(sum(r["tons"] for r in by_sku), 6)
    brand_cost = round(sum(r["cost"] for r in by_brand), 6)
    spec_cost = round(sum(r["cost"] for r in by_spec), 6)
    sku_cost = round(sum(r["cost"] for r in by_sku), 6)

    balanced = (
        abs(brand_tons - total_tons) < 1e-6
        and abs(spec_tons - total_tons) < 1e-6
        and abs(sku_tons - total_tons) < 1e-6
        and abs(brand_cost - total_cost) < 1e-6
        and abs(spec_cost - total_cost) < 1e-6
        and abs(sku_cost - total_cost) < 1e-6
        and abs(lot_consumed_tons - total_tons) < 1e-6
    )

    return {
        "by_brand": by_brand,
        "by_spec": by_spec,
        "by_sku": by_sku,
        "totals": {"tons": total_tons, "cost": total_cost},
        "bin_exact_tons": round(bin_exact_tons, 6),
        "brand_fifo_tons": round(brand_fifo_tons, 6),
        "provisional_lot_ids": provisional_lot_ids,
        "settlement_ready": len(provisional_lot_ids) == 0,
        "views": {
            "brand_tons": brand_tons,
            "spec_tons": spec_tons,
            "sku_tons": sku_tons,
            "brand_cost": brand_cost,
            "spec_cost": spec_cost,
            "sku_cost": sku_cost,
        },
        "balanced": balanced,
    }


# ---------------------------------------------------------------------------
# Governed Actions
# ---------------------------------------------------------------------------
@spec.edit_function(
    "create_steel_material_spec",
    CreateSteelMaterialSpecParams,
    description="核准创建挂接 TradeResource 的钢厂标准物理规格。",
)
def create_steel_material_spec(ctx: Any, params: CreateSteelMaterialSpecParams) -> None:
    if params.ash_max_pct <= 0 or params.sulfur_max_pct <= 0:
        raise InvariantError(
            "material spec quality thresholds must be positive",
            code="INVALID_SPEC_THRESHOLDS",
        )
    if ctx.get(SteelMaterialSpec, params.spec_id) is not None:
        raise InvariantError(
            f"steel material spec {params.spec_id!r} already exists",
            code="SPEC_ALREADY_EXISTS",
        )
    resource = ctx.require(TradeResource, params.resource_id)
    ctx.create(
        SteelMaterialSpec(
            spec_id=params.spec_id,
            resource=resource.resource_id,
            name=params.name,
            grade=params.grade,
            ash_max_pct=params.ash_max_pct,
            sulfur_max_pct=params.sulfur_max_pct,
        )
    )

spec.declare_action(
    "create_steel_material_spec",
    edit_function="create_steel_material_spec",
    allowed_roles=("steward", "mdm_steward", "admin"),
    revert_roles=("steward", "mdm_steward", "admin"),
    affects_source=False,
    description="核准创建挂接 TradeResource 的钢厂标准物理规格。",
)


@spec.edit_function(
    "create_steel_storage_bin",
    CreateSteelStorageBinParams,
    description="登记钢厂料仓/料堆物理锚点。",
)
def create_steel_storage_bin(ctx: Any, params: CreateSteelStorageBinParams) -> None:
    if ctx.get(SteelStorageBin, params.bin_id) is not None:
        raise InvariantError(
            f"storage bin {params.bin_id!r} already exists",
            code="BIN_ALREADY_EXISTS",
        )
    ctx.create(
        SteelStorageBin(
            bin_id=params.bin_id,
            yard_name=params.yard_name,
            material_category=params.material_category,
            status=params.status,
        )
    )

spec.declare_action(
    "create_steel_storage_bin",
    edit_function="create_steel_storage_bin",
    allowed_roles=("warehouse", "ops", "steward", "mdm_steward", "admin"),
    revert_roles=("steward", "mdm_steward", "admin"),
    affects_source=False,
    description="登记钢厂料仓/料堆物理锚点。",
)


@spec.edit_function(
    "open_steel_cost_period",
    OpenSteelCostPeriodParams,
    description="开启月度成本核算关账期间。",
)
def open_steel_cost_period(ctx: Any, params: OpenSteelCostPeriodParams) -> None:
    if ctx.get(SteelCostPeriod, params.period_id) is not None:
        raise InvariantError(
            f"cost period {params.period_id!r} already exists",
            code="COST_PERIOD_EXISTS",
        )
    ctx.create(SteelCostPeriod(period_id=params.period_id, status="OPEN"))

spec.declare_action(
    "open_steel_cost_period",
    edit_function="open_steel_cost_period",
    allowed_roles=("cost_accountant", "finance", "steward", "mdm_steward", "admin"),
    revert_roles=("steward", "mdm_steward", "admin"),
    affects_source=False,
    description="开启月度成本核算关账期间。",
)


@spec.edit_function(
    "receive_steel_lot",
    ReceiveSteelLotParams,
    description="过磅卸入料仓（前松后紧）：已归并建GOVERNED批次，旁路未归并散码先放行PROVISIONAL并挂协同工单。",
)
def receive_steel_lot(ctx: Any, params: ReceiveSteelLotParams) -> None:
    if params.initial_tons <= 0:
        raise InvariantError(
            "initial_tons must be positive",
            code="INVALID_LOT_QUANTITY",
        )
    if ctx.get(SteelMaterialLot, params.lot_id) is not None:
        raise InvariantError(
            f"lot {params.lot_id!r} already exists",
            code="LOT_ALREADY_EXISTS",
        )

    sku = ctx.require(SteelContractSKU, params.sku_id)
    manufacturer = ctx.require(SteelManufacturer, sku.brand_id)
    storage_bin = ctx.require(SteelStorageBin, params.bin_id)
    if storage_bin.status != "ACTIVE":
        raise InvariantError(
            f"storage bin {storage_bin.bin_id!r} is not ACTIVE (status={storage_bin.status!r})",
            code="STORAGE_BIN_INACTIVE",
        )

    if sku.canonical_spec_id:
        material_spec = ctx.require(SteelMaterialSpec, sku.canonical_spec_id)
        if not _quality_fits(material_spec, sku.ash_pct, sku.sulfur_pct):
            raise InvariantError(
                f"sku {sku.sku_id} quality does not fit {material_spec.spec_id}",
                code="SPEC_QUALITY_MISMATCH",
            )
        lot = SteelMaterialLot(
            lot_id=params.lot_id,
            sku_id=sku.sku_id,
            brand_id=manufacturer.brand_id,
            bin_id=storage_bin.bin_id,
            spec_id=material_spec.spec_id,
            period_id=params.period_id,
            governance_state="GOVERNED",
            settlement_authorized=False,
            initial_tons=float(params.initial_tons),
            remaining_tons=float(params.initial_tons),
            consumed_tons=0.0,
            unit_price_ton=float(sku.unit_price_ton),
            pending_work_card_id="",
        )
        ctx.create(lot)
        ctx.link(lot, STEEL_LOT_IN_BIN, storage_bin)
        ctx.link(lot, STEEL_LOT_PRODUCED_BY, manufacturer)
        ctx.link(lot, STEEL_LOT_BILLED_AS, sku)
        ctx.link(lot, STEEL_LOT_HAS_SPEC, material_spec)
        return

    # Bypass unmapped ERP SKU: never block weighbridge trucks; admit as PROVISIONAL.
    resolved_card_id = ""
    for existing_card in sorted(ctx.query(WorkCard), key=lambda c: c.card_id):
        if (
            existing_card.status in ("OPEN", "IN_PROGRESS")
            and existing_card.target_object_type == "SteelContractSKU"
            and existing_card.target_object_id == sku.sku_id
        ):
            resolved_card_id = existing_card.card_id
            break

    if not resolved_card_id and params.pending_work_card_id:
        if ctx.get(WorkCard, params.pending_work_card_id) is not None:
            resolved_card_id = params.pending_work_card_id

    if not resolved_card_id and params.open_review_card:
        resolved_card_id = params.pending_work_card_id or f"WC-PROV-{params.lot_id}"
        ctx.create(
            WorkCard(
                card_id=resolved_card_id,
                card_kind="HUMAN_TASK",
                target_object_type="SteelContractSKU",
                target_object_id=sku.sku_id,
                requested_action="map_steel_sku_to_spec",
                requested_params_json="{}",
                requester_actor_id=ctx.actor.actor_id,
                assignee_role="steward",
                status="OPEN",
            )
        )

    lot = SteelMaterialLot(
        lot_id=params.lot_id,
        sku_id=sku.sku_id,
        brand_id=manufacturer.brand_id,
        bin_id=storage_bin.bin_id,
        spec_id=None,
        period_id=params.period_id,
        governance_state="PROVISIONAL",
        settlement_authorized=False,
        initial_tons=float(params.initial_tons),
        remaining_tons=float(params.initial_tons),
        consumed_tons=0.0,
        unit_price_ton=float(sku.unit_price_ton),
        pending_work_card_id=resolved_card_id,
    )
    ctx.create(lot)
    ctx.link(lot, STEEL_LOT_IN_BIN, storage_bin)
    ctx.link(lot, STEEL_LOT_PRODUCED_BY, manufacturer)
    ctx.link(lot, STEEL_LOT_BILLED_AS, sku)

spec.declare_action(
    "receive_steel_lot",
    edit_function="receive_steel_lot",
    allowed_roles=("weighbridge", "warehouse", "buyer", "ops", "steward", "mdm_steward", "admin"),
    revert_roles=("steward", "mdm_steward", "admin"),
    affects_source=False,
    description="过磅卸入料仓（前松后紧）：已归并建GOVERNED批次，旁路未归并散码先放行PROVISIONAL并挂协同工单。",
)


@spec.edit_function(
    "map_steel_sku_to_spec",
    MapSteelSkuParams,
    description="对存量/旁路散码只写 Ontology 自有归并字段与归并关系。",
)
def map_steel_sku_to_spec(ctx: Any, params: MapSteelSkuParams) -> None:
    sku = ctx.require(SteelContractSKU, params.sku_id)
    material_spec = ctx.require(SteelMaterialSpec, params.canonical_spec_id)
    if not _quality_fits(material_spec, sku.ash_pct, sku.sulfur_pct):
        raise InvariantError(
            f"sku {sku.sku_id} quality does not fit {material_spec.spec_id}",
            code="SPEC_QUALITY_MISMATCH",
        )
    if params.work_card_id:
        card = ctx.require(WorkCard, params.work_card_id)
        if card.target_object_id != sku.sku_id:
            raise InvariantError(
                f"WorkCard:{card.card_id} targets {card.target_object_id!r}, not {sku.sku_id!r}",
                code="WORK_CARD_TARGET_MISMATCH",
            )
        if card.status != "COMPLETED":
            raise InvariantError(
                f"WorkCard:{card.card_id} must be COMPLETED before mapping SKU (status={card.status!r})",
                code="WORK_CARD_NOT_COMPLETED",
            )

    ctx.modify(
        sku,
        canonical_spec_id=material_spec.spec_id,
        mapped_by=params.mapped_by or ctx.actor.actor_id,
    )
    ctx.link(sku, STEEL_SKU_MAPS_TO_SPEC, material_spec)

spec.declare_action(
    "map_steel_sku_to_spec",
    edit_function="map_steel_sku_to_spec",
    allowed_roles=("steward", "mdm_steward", "buyer", "admin"),
    revert_roles=("steward", "mdm_steward", "admin"),
    affects_source=False,
    description="对存量/旁路散码只写 Ontology 自有归并字段与归并关系。",
)


@spec.edit_function(
    "reconcile_provisional_lot",
    ReconcileProvisionalLotParams,
    description="依据已核准工单与散码归并结论，将 PROVISIONAL 批次升级为 GOVERNED 并回填消耗账目规格。",
)
def reconcile_provisional_lot(ctx: Any, params: ReconcileProvisionalLotParams) -> None:
    lot = ctx.require(SteelMaterialLot, params.lot_id)
    sku = ctx.require(SteelContractSKU, lot.sku_id)
    if not sku.canonical_spec_id:
        raise InvariantError(
            f"cannot reconcile provisional lot {lot.lot_id!r}: SKU {sku.sku_id!r} is still unmapped",
            code="PROVISIONAL_SKU_BLOCKS_SETTLEMENT",
        )
    card_id = params.work_card_id or lot.pending_work_card_id
    if card_id:
        card = ctx.require(WorkCard, card_id)
        if card.status != "COMPLETED":
            raise InvariantError(
                f"WorkCard:{card.card_id} must be COMPLETED before reconciling lot {lot.lot_id!r} "
                f"(status={card.status!r})",
                code="WORK_CARD_NOT_COMPLETED",
            )

    material_spec = ctx.require(SteelMaterialSpec, sku.canonical_spec_id)
    if not _quality_fits(material_spec, sku.ash_pct, sku.sulfur_pct):
        raise InvariantError(
            f"sku {sku.sku_id} quality does not fit {material_spec.spec_id}",
            code="SPEC_QUALITY_MISMATCH",
        )

    ctx.modify(
        lot,
        spec_id=material_spec.spec_id,
        governance_state="GOVERNED",
        pending_work_card_id="",
    )
    ctx.link(lot, STEEL_LOT_HAS_SPEC, material_spec)

    for entry in ctx.query(SteelConsumptionEntry):
        if entry.lot_id == lot.lot_id and entry.spec_id != material_spec.spec_id:
            ctx.modify(entry, spec_id=material_spec.spec_id)

spec.declare_action(
    "reconcile_provisional_lot",
    edit_function="reconcile_provisional_lot",
    allowed_roles=("steward", "mdm_steward", "cost_accountant", "finance", "admin"),
    revert_roles=("steward", "mdm_steward", "admin"),
    affects_source=False,
    description="依据已核准工单与散码归并结论，将 PROVISIONAL 批次升级为 GOVERNED 并回填消耗账目规格。",
)


@spec.edit_function(
    "authorize_steel_lot_settlement",
    AuthorizeSteelLotSettlementParams,
    description="付款结算放行硬卡口：任何 PROVISIONAL 或未归并散码批次一律拦阻。",
)
def authorize_steel_lot_settlement(ctx: Any, params: AuthorizeSteelLotSettlementParams) -> None:
    lot = ctx.require(SteelMaterialLot, params.lot_id)
    sku = ctx.require(SteelContractSKU, lot.sku_id)
    if lot.governance_state == "PROVISIONAL" or not lot.spec_id or not sku.canonical_spec_id:
        raise InvariantError(
            f"lot {lot.lot_id!r} cannot be authorized for settlement while PROVISIONAL or unmapped "
            f"(sku={sku.sku_id!r})",
            code="PROVISIONAL_SKU_BLOCKS_SETTLEMENT",
        )
    ctx.modify(lot, settlement_authorized=True)

spec.declare_action(
    "authorize_steel_lot_settlement",
    edit_function="authorize_steel_lot_settlement",
    allowed_roles=("cost_accountant", "finance", "steward", "mdm_steward", "admin"),
    revert_roles=("cost_accountant", "steward", "mdm_steward", "admin"),
    affects_source=False,
    description="付款结算放行硬卡口：任何 PROVISIONAL 或未归并散码批次一律拦阻。",
)


@spec.edit_function(
    "close_steel_cost_period",
    CloseSteelCostPeriodParams,
    description="成本月结硬卡口：期间内存在任意 PROVISIONAL 批次或未归并散码一律报错阻断。",
)
def close_steel_cost_period(ctx: Any, params: CloseSteelCostPeriodParams) -> None:
    period = ctx.require(SteelCostPeriod, params.period_id)

    period_lots = [
        lot
        for lot in sorted(ctx.query(SteelMaterialLot), key=lambda item: item.lot_id)
        if not lot.period_id or lot.period_id == params.period_id
    ]
    for lot in period_lots:
        sku = ctx.require(SteelContractSKU, lot.sku_id)
        if lot.governance_state == "PROVISIONAL" or not lot.spec_id or not sku.canonical_spec_id:
            raise InvariantError(
                f"cost period {params.period_id!r} blocked by PROVISIONAL lot {lot.lot_id!r} "
                f"(sku={sku.sku_id!r})",
                code="PROVISIONAL_SKU_BLOCKS_SETTLEMENT",
            )

    period_entries = [
        entry
        for entry in sorted(ctx.query(SteelConsumptionEntry), key=lambda item: item.entry_id)
        if not entry.period_id or entry.period_id == params.period_id
    ]
    for entry in period_entries:
        if not entry.spec_id:
            raise InvariantError(
                f"cost period {params.period_id!r} blocked by unmapped consumption entry {entry.entry_id!r}",
                code="PROVISIONAL_SKU_BLOCKS_SETTLEMENT",
            )

    closed_tons = round(sum(float(e.tons) for e in period_entries), 6)
    closed_cost = round(sum(float(e.cost_amount) for e in period_entries), 6)
    ctx.modify(
        period,
        status="CLOSED",
        closed_tons=closed_tons,
        closed_cost=closed_cost,
    )

spec.declare_action(
    "close_steel_cost_period",
    edit_function="close_steel_cost_period",
    allowed_roles=("cost_accountant", "finance", "steward", "mdm_steward", "admin"),
    revert_roles=("cost_accountant", "steward", "mdm_steward", "admin"),
    affects_source=False,
    description="成本月结硬卡口：期间内存在任意 PROVISIONAL 批次或未归并散码一律报错阻断。",
)


@spec.edit_function(
    "consume_steel_from_bin",
    ConsumeSteelFromBinParams,
    description="MES 按物理料仓报工消耗：100% 精确溯源至该料仓批次与规格（BIN_EXACT）。",
)
def consume_steel_from_bin(ctx: Any, params: ConsumeSteelFromBinParams) -> None:
    storage_bin = ctx.require(SteelStorageBin, params.bin_id)
    if storage_bin.status != "ACTIVE":
        raise InvariantError(
            f"storage bin {storage_bin.bin_id!r} is not ACTIVE",
            code="STORAGE_BIN_INACTIVE",
        )
    lots = sorted(
        (
            lot
            for lot in ctx.query(SteelMaterialLot)
            if lot.bin_id == params.bin_id and lot.remaining_tons > 1e-9
        ),
        key=lambda lot: lot.lot_id,
    )
    available = sum(float(lot.remaining_tons) for lot in lots)
    _deduct_lots(
        ctx,
        entry_id=params.entry_id,
        lots=lots,
        requested_tons=params.tons,
        allocation_rule="BIN_EXACT",
        period_id=params.period_id,
        insufficient_code="INSUFFICIENT_BIN_STOCK",
        insufficient_msg=f"bin {params.bin_id!r} has only {available} tons available, cannot consume {params.tons}",
    )

spec.declare_action(
    "consume_steel_from_bin",
    edit_function="consume_steel_from_bin",
    allowed_roles=("mes_operator", "ops", "warehouse", "steward", "mdm_steward", "admin"),
    revert_roles=("steward", "mdm_steward", "cost_accountant", "admin"),
    affects_source=False,
    description="MES 按物理料仓报工消耗：100% 精确溯源至该料仓批次与规格（BIN_EXACT）。",
)


@spec.edit_function(
    "consume_steel_by_brand_fifo",
    ConsumeSteelByBrandFifoParams,
    description="兼容无料仓计量工位按品牌 FIFO 扣减，强制打标 cost_allocation_rule='BRAND_FIFO'。",
)
def consume_steel_by_brand_fifo(ctx: Any, params: ConsumeSteelByBrandFifoParams) -> None:
    ctx.require(SteelManufacturer, params.brand_id)
    lots = sorted(
        (
            lot
            for lot in ctx.query(SteelMaterialLot)
            if lot.brand_id == params.brand_id and lot.remaining_tons > 1e-9
        ),
        key=lambda lot: lot.lot_id,
    )
    available = sum(float(lot.remaining_tons) for lot in lots)
    _deduct_lots(
        ctx,
        entry_id=params.entry_id,
        lots=lots,
        requested_tons=params.tons,
        allocation_rule="BRAND_FIFO",
        period_id=params.period_id,
        insufficient_code="INSUFFICIENT_BRAND_STOCK",
        insufficient_msg=f"brand {params.brand_id!r} has only {available} tons available, cannot consume {params.tons}",
    )

spec.declare_action(
    "consume_steel_by_brand_fifo",
    edit_function="consume_steel_by_brand_fifo",
    allowed_roles=("mes_operator", "ops", "warehouse", "steward", "mdm_steward", "admin"),
    revert_roles=("steward", "mdm_steward", "cost_accountant", "admin"),
    affects_source=False,
    description="兼容无料仓计量工位按品牌 FIFO 扣减，强制打标 cost_allocation_rule='BRAND_FIFO'。",
)


spec.update_display(
    {
        "ontologies": {
            "steel_mdm_plugin": "河北钢厂主数据与料仓精确溯源插件本体",
        },
        "categories": {
            "Master Data": "主数据治理",
            "Inventory": "料仓物料账实",
        },
        "objects": {
            "SteelMaterialSpec": "钢厂物料物理规格",
            "SteelManufacturer": "钢厂合作生产厂商",
            "SteelContractSKU": "ERP采购合同散码",
            "SteelStorageBin": "物理料仓锚点",
            "SteelMaterialLot": "高维进厂批次桶",
            "SteelConsumptionEntry": "高炉消耗台账明细",
            "SteelCostPeriod": "成本核算关账期间",
        },
        "object_properties": {
            "steel_sku_maps_to_spec": "散码归并规格",
            "steel_lot_in_bin": "批次存放料仓",
            "steel_lot_produced_by": "批次生产厂商",
            "steel_lot_billed_as": "批次结算散码",
            "steel_lot_has_spec": "批次对应物理规格",
            "steel_entry_from_lot": "消耗明细对应批次",
        },
        "actions": {
            "create_steel_material_spec": "新建标准物理规格",
            "create_steel_storage_bin": "登记物理料仓",
            "open_steel_cost_period": "开启成本关账期间",
            "receive_steel_lot": "地磅过磅入仓卸料",
            "map_steel_sku_to_spec": "散码归并标准规格",
            "reconcile_provisional_lot": "临时批次转正并回填",
            "authorize_steel_lot_settlement": "放行批次结算付款",
            "close_steel_cost_period": "成本期间关账检查",
            "consume_steel_from_bin": "按物理料仓精确扣料",
            "consume_steel_by_brand_fifo": "按品牌FIFO估算扣料",
        },
        "functions": {
            "recommend_steel_spec_for_sku": "推荐钢厂物理规格",
            "reconcile_steel_three_view_ledger": "三视角钢厂台账对账",
        },
    }
)


BUYER = Actor(actor_id="buyer-01", role="buyer", clearance_tags={"*"})
AI_AGENT = Actor(actor_id="ai-agent-01", role="ai_agent", clearance_tags={"*"})
STEWARD = Actor(actor_id="mdm-steward-01", role="steward", clearance_tags={"*"})
WEIGHBRIDGE = Actor(actor_id="weighbridge-01", role="weighbridge", clearance_tags={"*"})
MES_OPERATOR = Actor(actor_id="mes-op-01", role="mes_operator", clearance_tags={"*"})
COST_ACCOUNTANT = Actor(actor_id="cost-acc-01", role="cost_accountant", clearance_tags={"*"})
