"""钢卷资产与 OSv2 四大原语治理纯业务本体（PIM）。"""

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
    "SteelCoil",
    "ScrapLot",
    "FEEDS",
    "Osv2SetGradeParams",
    "Osv2AdjustPriceParams",
    "Osv2EmptyParams",
    "osv2_spec",
    "OSV2_OPS",
    "OSV2_FINANCE",
]

osv2_spec = OntologySpec()
osv2_spec.interface("PricedAsset", ("asset_id", "unit_price_ton"), "可计价资产")


class SteelCoil(ObjectType):
    __primary_key__ = "asset_id"
    __title__ = "grade"
    __security_tag_fields__ = ("region",)
    __interfaces__ = ("PricedAsset",)
    __owned__ = {"quality_grade": "UNSET"}
    __masked_fields__ = {
        "unit_price_ton": ("finance",),
        "cost_note": ("finance", "auditor"),
        "internal_code": ("finance",),
    }
    __mask_defaults__ = {"internal_code": "REDACTED"}

    asset_id: str
    grade: str
    region: str = "north"
    unit_price_ton: float
    stock_tons: float = 0.0
    cost_note: str | None = None
    internal_code: str = "SECRET"
    quality_grade: str = "UNSET"


class ScrapLot(ObjectType):
    __primary_key__ = "asset_id"
    __title__ = "grade"
    __security_tag_fields__ = ("region",)
    __interfaces__ = ("PricedAsset",)
    __masked_fields__ = {"unit_price_ton": ("finance",)}

    asset_id: str
    grade: str
    region: str = "north"
    unit_price_ton: float
    stock_tons: float = 0.0


for _cls in (SteelCoil, ScrapLot):
    osv2_spec.object_type(_cls)
FEEDS = ObjectProperty(
    name="feeds", domain="ScrapLot", range="SteelCoil", cardinality="many-to-many"
)
osv2_spec.add_object_property(FEEDS)


class Osv2SetGradeParams(BaseModel):
    asset_id: str
    quality_grade: str


class Osv2AdjustPriceParams(BaseModel):
    asset_id: str
    unit_price_ton: float


class Osv2EmptyParams(BaseModel):
    pass


@osv2_spec.function("portfolio_value", Osv2EmptyParams, description="库存货值合计（只读）")
def portfolio_value(ctx: Any, params: Osv2EmptyParams) -> dict[str, Any]:
    total = 0.0
    for coil in ctx.query(SteelCoil):
        total += coil.unit_price_ton * coil.stock_tons
    return {"total_value": round(total, 2)}


@osv2_spec.action(
    "set_quality_grade",
    Osv2SetGradeParams,
    allowed_roles=("ops", "finance", "admin"),
    affects_source=False,
)
def set_quality_grade(ctx: Any, params: Osv2SetGradeParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    ctx.modify(coil, quality_grade=params.quality_grade)


@osv2_spec.action(
    "adjust_price",
    Osv2AdjustPriceParams,
    allowed_roles=("ops", "finance", "admin"),
)
def adjust_price(ctx: Any, params: Osv2AdjustPriceParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    if params.unit_price_ton <= 0:
        raise InvariantError("price must be positive", code="INVALID_PRICE")
    ctx.modify(coil, unit_price_ton=params.unit_price_ton)


@osv2_spec.action(
    "request_price_change",
    Osv2AdjustPriceParams,
    allowed_roles=("ops", "finance", "admin"),
    approver_roles=("finance_manager",),
    approval_rule=lambda params, actor: params.unit_price_ton > 20000,
    description="超过 2 万元/吨的改价需要财务经理批准",
)
def request_price_change(ctx: Any, params: Osv2AdjustPriceParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    ctx.modify(coil, unit_price_ton=params.unit_price_ton)


_AUTO_IDS = iter(range(1, 10_000))


@osv2_spec.automation(
    "fill_missing_grade",
    SteelCoil,
    lambda coil: coil.quality_grade == "UNSET",
    description="发现未定级钢卷时自动提交定级动作",
)
def fill_missing_grade(runtime: Any, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    run = runtime.submit_action(
        f"CC-AUTO-GRADE-{next(_AUTO_IDS):04d}",
        "set_quality_grade",
        {"asset_id": coil.asset_id, "quality_grade": "PENDING_REVIEW"},
        actor,
    )
    return {"run_id": run["run_id"], "status": run["status"]}


@osv2_spec.automation(
    "log_low_stock",
    "SteelCoil",
    lambda coil: coil.stock_tons < 100,
    description="低库存只记录不改状态",
)
def log_low_stock(runtime: Any, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    return {"flagged": coil.asset_id, "stock_tons": coil.stock_tons}


@osv2_spec.automation(
    "escalate_premium_price",
    SteelCoil,
    lambda coil: coil.unit_price_ton > 20000,
    description="高价钢卷改价走审批流",
)
def escalate_premium_price(runtime: Any, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    run = runtime.submit_action(
        f"CC-AUTO-ESCALATE-{next(_AUTO_IDS):04d}",
        "request_price_change",
        {"asset_id": coil.asset_id, "unit_price_ton": coil.unit_price_ton + 100.0},
        actor,
    )
    return {"run_id": run["run_id"], "status": run["status"]}


osv2_spec.update_display(
    {
        "ontologies": {
            "osv2_governance": "钢卷资产与 OSv2 四大原语治理",
        },
        "categories": {
            "Asset Governance": "资产治理",
        },
        "objects": {
            "SteelCoil": "钢卷",
            "ScrapLot": "废钢批",
        },
        "object_properties": {
            "feeds": "原料投料",
        },
        "interfaces": {
            "PricedAsset": "可计价资产",
        },
        "fields": {
            "asset_id": "资产编号",
            "grade": "牌号",
            "region": "区域",
            "unit_price_ton": "吨价",
            "stock_tons": "库存吨数",
            "cost_note": "成本备注",
            "internal_code": "内部编码",
            "quality_grade": "质量等级",
        },
        "actions": {
            "set_quality_grade": "设置质量等级",
            "adjust_price": "调整单价",
            "request_price_change": "申请调整单价",
        },
        "functions": {
            "portfolio_value": "库存货值汇总",
        },
        "automations": {
            "fill_missing_grade": "自动补齐质量等级",
            "log_low_stock": "记录低库存",
            "escalate_premium_price": "高价调价升级",
        },
        "roles": {
            "finance_manager": "财务经理",
        },
        "statuses": {
            "PENDING_REVIEW": "待复核",
        },
    }
)

OSV2_OPS = Actor(actor_id="cc-ops-01", role="ops", clearance_tags={"*"})
OSV2_FINANCE = Actor(actor_id="cc-fin-01", role="finance", clearance_tags={"*"})
