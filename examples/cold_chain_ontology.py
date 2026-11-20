"""冷链医药运单重派领域本体（ColdChainOntology · 纯 PIM）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from open_ontology.spec import (
    Actor,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)

try:
    from .business_ontology import (
        WorkCard,
        register_work_card_primitives,
        settle_work_card,
        submit_or_open_work_card,
    )
except ImportError:
    from examples.business_ontology import (
        WorkCard,
        register_work_card_primitives,
        settle_work_card,
        submit_or_open_work_card,
    )

__all__ = [
    "Warehouse",
    "Truck",
    "Shipment",
    "Hospital",
    "WorkCard",
    "CARRIES",
    "SHIPS_FROM",
    "DELIVERS_TO",
    "RecommendParams",
    "RerouteParams",
    "AnnotateParams",
    "spec",
    "recommend_reroute",
    "reroute_shipment",
    "annotate_shipment",
    "settle_work_card",
    "submit_or_open_work_card",
    "DISPATCHER",
    "AI_AGENT",
    "OPS_MANAGER",
    "AUDITOR_NORTH",
]


# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class Warehouse(ObjectType):
    __primary_key__ = "warehouse_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)

    warehouse_id: str
    name: str
    region: str
    cold_chain_certified: bool = True
    batch_stock: dict[str, int] = Field(default_factory=dict)
    dock_load: int = 0


class Truck(ObjectType):
    __primary_key__ = "truck_id"
    __title__ = "plate"
    __security_tag_fields__ = ("region",)

    truck_id: str
    plate: str
    region: str
    cold_chain_certified: bool = True
    status: str = "available"  # available | assigned | unavailable
    eta_hours: float = 1.0
    current_warehouse_id: str | None = None


class Shipment(ObjectType):
    __primary_key__ = "shipment_id"
    __title__ = "product"
    __security_tag_fields__ = ("region",)
    __owned__ = {"dispatcher_note": None}

    shipment_id: str
    batch_no: str
    product: str
    quantity: int
    required_temp_c: float = 2.0
    status: str = "in_transit"  # in_transit | rerouted | delivered
    priority: str = "urgent"
    region: str = "north"
    origin_warehouse_id: str
    current_warehouse_id: str
    assigned_truck_id: str
    destination_hospital_id: str
    dispatcher_note: str | None = None


class Hospital(ObjectType):
    __primary_key__ = "hospital_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)

    hospital_id: str
    name: str
    region: str
    required_temp_c: float = 2.0


# ---------------------------------------------------------------------------
# Object properties
# ---------------------------------------------------------------------------
CARRIES = ObjectProperty(
    name="carries", domain="Truck", range="Shipment", cardinality="one-to-many"
)
SHIPS_FROM = ObjectProperty(
    name="ships_from", domain="Shipment", range="Warehouse", cardinality="many-to-one"
)
DELIVERS_TO = ObjectProperty(
    name="delivers_to", domain="Shipment", range="Hospital", cardinality="many-to-one"
)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class RecommendParams(BaseModel):
    shipment_id: str
    max_eta_hours: float = 3.0
    exclude_truck_id: str | None = None


class RerouteParams(BaseModel):
    shipment_id: str
    replacement_warehouse_id: str
    replacement_truck_id: str
    extra_cost: float = 0.0
    reason: str = "truck breakdown"


class AnnotateParams(BaseModel):
    shipment_id: str
    note: str | None = None


# ---------------------------------------------------------------------------
# Spec + handlers
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (Warehouse, Truck, Shipment, Hospital):
    spec.object_type(_cls)
for _prop in (CARRIES, SHIPS_FROM, DELIVERS_TO):
    spec.add_object_property(_prop)


@spec.function(
    "recommend_reroute",
    RecommendParams,
    description="列出满足冷链资质、同批次库存与 ETA 上限的接替仓/车组合（只读）。",
)
def recommend_reroute(ctx: Any, params: RecommendParams) -> dict[str, Any]:
    shipment = ctx.get(Shipment, params.shipment_id)
    if shipment is None:
        return {"shipment_id": params.shipment_id, "candidates": []}

    warehouses = [
        w
        for w in ctx.query(Warehouse)
        if w.cold_chain_certified
        and w.batch_stock.get(shipment.batch_no, 0) >= shipment.quantity
    ]
    trucks = [
        t
        for t in ctx.query(Truck)
        if t.cold_chain_certified
        and t.status == "available"
        and t.eta_hours <= params.max_eta_hours
        and t.truck_id != params.exclude_truck_id
    ]

    candidates: list[dict[str, Any]] = []
    for warehouse in warehouses:
        for truck in trucks:
            candidates.append(
                {
                    "warehouse_id": warehouse.warehouse_id,
                    "warehouse_name": warehouse.name,
                    "truck_id": truck.truck_id,
                    "eta_hours": truck.eta_hours,
                    "batch_stock": warehouse.batch_stock.get(shipment.batch_no, 0),
                }
            )
    candidates.sort(key=lambda c: (c["eta_hours"], c["warehouse_id"], c["truck_id"]))
    return {
        "shipment_id": shipment.shipment_id,
        "batch_no": shipment.batch_no,
        "quantity": shipment.quantity,
        "candidates": candidates,
    }


@spec.action(
    "reroute_shipment",
    RerouteParams,
    allowed_roles=("dispatcher", "ai_agent", "ops_manager"),
    revert_roles=("ops_manager",),
    description="把冷链运单从抛锚卡车重派到接替仓 + 接替车（受治理）。",
)
def reroute_shipment(ctx: Any, params: RerouteParams) -> None:
    shipment = ctx.require(Shipment, params.shipment_id)
    warehouse = ctx.require(Warehouse, params.replacement_warehouse_id)
    truck = ctx.require(Truck, params.replacement_truck_id)

    if shipment.status == "delivered":
        raise InvariantError("shipment already delivered", code="ALREADY_DELIVERED")
    if params.extra_cost < 0:
        raise InvariantError("extra_cost must be >= 0", code="INVALID_COST")
    if not truck.cold_chain_certified:
        raise InvariantError(
            f"truck {truck.truck_id} has no cold-chain certification",
            code="TRUCK_NOT_COLD_CHAIN",
        )
    if not warehouse.cold_chain_certified:
        raise InvariantError(
            f"warehouse {warehouse.warehouse_id} has no cold-chain certification",
            code="WAREHOUSE_NOT_COLD_CHAIN",
        )
    if truck.eta_hours > 3.0:
        raise InvariantError(
            f"truck {truck.truck_id} ETA {truck.eta_hours}h exceeds 3h cold-chain limit",
            code="ETA_TOO_LONG",
        )
    if truck.truck_id == shipment.assigned_truck_id:
        raise InvariantError(
            "replacement truck must differ from the broken truck",
            code="SAME_TRUCK",
        )
    if truck.status != "available":
        raise InvariantError(
            f"truck {truck.truck_id} is {truck.status}, not available",
            code="TRUCK_UNAVAILABLE",
        )
    stock = warehouse.batch_stock.get(shipment.batch_no, 0)
    if stock < shipment.quantity:
        raise InvariantError(
            f"warehouse {warehouse.warehouse_id} has {stock} units of "
            f"batch {shipment.batch_no}, need {shipment.quantity}",
            code="INSUFFICIENT_STOCK",
        )
    if ctx.actor.role == "ai_agent" and params.extra_cost >= 2000.0:
        raise InvariantError(
            f"AI agent extra_cost {params.extra_cost} >= 2000.0 requires ops_manager approval via WorkCard",
            code="APPROVAL_REQUIRED_HIGH_COST_REROUTE",
        )

    old_truck_id = shipment.assigned_truck_id
    old_truck = ctx.get(Truck, old_truck_id) if old_truck_id else None

    new_stock = stock - shipment.quantity
    ctx.modify(warehouse, batch_stock={**warehouse.batch_stock, shipment.batch_no: new_stock})

    if old_truck is not None:
        ctx.modify(old_truck, status="unavailable")
    ctx.modify(
        truck,
        status="assigned",
        current_warehouse_id=warehouse.warehouse_id,
    )
    ctx.modify(
        shipment,
        status="rerouted",
        assigned_truck_id=truck.truck_id,
        current_warehouse_id=warehouse.warehouse_id,
    )

    ctx.unlink_all_to(CARRIES, shipment)
    ctx.link(truck, CARRIES, shipment)

    if shipment.current_warehouse_id:
        old_warehouse = ctx.get(Warehouse, shipment.current_warehouse_id)
        if old_warehouse is not None:
            ctx.unlink(shipment, SHIPS_FROM, old_warehouse)
    ctx.link(shipment, SHIPS_FROM, warehouse)


@spec.action(
    "annotate_shipment",
    AnnotateParams,
    allowed_roles=("dispatcher", "ai_agent", "ops_manager"),
    affects_source=False,
    description="只修改 Ontology 自有的调度备注，不触碰任何源支撑业务字段。",
)
def annotate_shipment(ctx: Any, params: AnnotateParams) -> None:
    shipment = ctx.require(Shipment, params.shipment_id)
    ctx.modify(shipment, dispatcher_note=params.note)


DISPATCHER = Actor(
    actor_id="dispatcher-01",
    role="dispatcher",
    clearance_tags={"region:north"},
)
AI_AGENT = Actor(
    actor_id="aip-agent-07",
    role="ai_agent",
    clearance_tags={"*"},
)
OPS_MANAGER = Actor(
    actor_id="manager-01",
    role="ops_manager",
    clearance_tags={"*"},
)
AUDITOR_NORTH = Actor(
    actor_id="auditor-01",
    role="auditor",
    clearance_tags={"region:north"},
)

register_work_card_primitives(
    spec,
    opener_roles=("dispatcher", "ai_agent", "ops_manager"),
    resolver_roles=("ops_manager",),
)
