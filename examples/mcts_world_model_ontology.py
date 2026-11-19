"""急救冷链 MCTS 世界模型纯业务本体（ColdChainMCTSOntology · PIM）。"""

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
    "EmergencyOrder",
    "ColdTruck",
    "SERVES",
    "DispatchParams",
    "EvaluateValueParams",
    "spec",
    "evaluate_world_value",
    "dispatch_order",
    "DISPATCHER",
    "AUDITOR",
    "EXPECTED_OPTIMAL_VALUE",
]


class EmergencyOrder(ObjectType):
    """急救冷链运单。"""

    __primary_key__ = "order_id"
    __title__ = "cargo"
    __security_tag_fields__ = ("region",)

    order_id: str
    cargo: str
    region: str = "north"
    base_value: float
    max_eta_hours: float
    min_cold_level: int
    penalty: float
    status: str = "pending"  # pending | assigned
    assigned_truck_id: str | None = None


class ColdTruck(ObjectType):
    """冷链车（每辆车只能承运一张运单）。"""

    __primary_key__ = "truck_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)

    truck_id: str
    name: str
    region: str = "north"
    eta_hours: float
    cold_level: int
    freight_cost: float
    status: str = "available"  # available | assigned


SERVES = ObjectProperty(
    name="serves",
    domain="ColdTruck",
    range="EmergencyOrder",
    cardinality="one-to-many",
    description="冷链车承运某张急救运单",
)


class DispatchParams(BaseModel):
    order_id: str
    truck_id: str
    note: str = ""


class EvaluateValueParams(BaseModel):
    label: str = "terminal"


spec = OntologySpec()
for _cls in (EmergencyOrder, ColdTruck):
    spec.object_type(_cls)
spec.add_object_property(SERVES)


@spec.function(
    "evaluate_world_value",
    EvaluateValueParams,
    description=(
        "终局精确业务价值 V(s) = 总履约收益 − 运输成本 − 未履约重罚（只读）。"
    ),
)
def evaluate_world_value(ctx: Any, params: EvaluateValueParams) -> dict[str, Any]:
    orders = sorted(ctx.query(EmergencyOrder), key=lambda o: o.order_id)
    trucks = {t.truck_id: t for t in ctx.query(ColdTruck)}

    revenue = 0.0
    freight_cost = 0.0
    penalty = 0.0
    fulfilled = 0
    unfulfilled = 0
    per_order: list[dict[str, Any]] = []

    for order in orders:
        if order.status == "assigned" and order.assigned_truck_id:
            truck = trucks.get(order.assigned_truck_id)
            cost = truck.freight_cost if truck is not None else 0.0
            revenue += order.base_value
            freight_cost += cost
            fulfilled += 1
            per_order.append(
                {
                    "order_id": order.order_id,
                    "truck_id": order.assigned_truck_id,
                    "revenue": order.base_value,
                    "freight_cost": cost,
                    "penalty": 0.0,
                    "value": order.base_value - cost,
                    "fulfilled": True,
                }
            )
        else:
            penalty += order.penalty
            unfulfilled += 1
            per_order.append(
                {
                    "order_id": order.order_id,
                    "truck_id": None,
                    "revenue": 0.0,
                    "freight_cost": 0.0,
                    "penalty": order.penalty,
                    "value": -order.penalty,
                    "fulfilled": False,
                }
            )

    total = revenue - freight_cost - penalty
    return {
        "label": params.label,
        "total_value": round(total, 2),
        "revenue": round(revenue, 2),
        "freight_cost": round(freight_cost, 2),
        "penalty": round(penalty, 2),
        "fulfilled": fulfilled,
        "unfulfilled": unfulfilled,
        "per_order": per_order,
    }


@spec.action(
    "dispatch_order",
    DispatchParams,
    allowed_roles=("dispatcher", "ops_manager", "ai_agent"),
    description="把一张急救运单派给一辆可用冷链车（受治理，硬护栏当场拒绝）。",
)
def dispatch_order(ctx: Any, params: DispatchParams) -> None:
    order = ctx.require(EmergencyOrder, params.order_id)
    truck = ctx.require(ColdTruck, params.truck_id)

    if order.status == "assigned":
        raise InvariantError(
            f"order {order.order_id} already dispatched to "
            f"{order.assigned_truck_id}",
            code="ALREADY_DISPATCHED",
        )
    if truck.status != "available":
        raise InvariantError(
            f"truck {truck.truck_id} is {truck.status}, not available",
            code="TRUCK_UNAVAILABLE",
        )
    if truck.cold_level < order.min_cold_level:
        raise InvariantError(
            f"truck {truck.truck_id} cold_level {truck.cold_level} < "
            f"required {order.min_cold_level} for {order.order_id}",
            code="ETA_OR_COLD_LEVEL_VIOLATION",
        )
    if truck.eta_hours > order.max_eta_hours:
        raise InvariantError(
            f"truck {truck.truck_id} ETA {truck.eta_hours}h > "
            f"max_eta {order.max_eta_hours}h for {order.order_id}",
            code="ETA_OR_COLD_LEVEL_VIOLATION",
        )

    ctx.modify(
        order,
        status="assigned",
        assigned_truck_id=truck.truck_id,
    )
    ctx.modify(truck, status="assigned")
    ctx.link(truck, SERVES, order)


spec.update_display(
    {
        "ontologies": {
            "coldchain_mcts": "冷链医药调度与 MCTS 规划",
        },
        "categories": {
            "Supply Chain": "供应链",
        },
        "objects": {
            "EmergencyOrder": "急救订单",
            "ColdTruck": "冷链车辆",
        },
        "object_properties": {
            "serves": "承运关系",
        },
        "fields": {
            "order_id": "订单编号",
            "cargo": "货物",
            "region": "区域",
            "base_value": "基础价值",
            "max_eta_hours": "最长时效（小时）",
            "min_cold_level": "最低冷链等级",
            "penalty": "违约罚金",
            "assigned_truck_id": "承运车辆编号",
            "truck_id": "车辆编号",
            "eta_hours": "预计时效（小时）",
            "cold_level": "冷链等级",
            "freight_cost": "运费",
        },
        "actions": {
            "dispatch_order": "分配配送车辆",
        },
        "functions": {
            "evaluate_world_value": "计算全局业务价值",
        },
        "roles": {
            "dispatcher": "调度员",
            "ops_manager": "运营主管",
            "auditor": "审计员",
        },
        "statuses": {
            "pending": "待处理",
            "assigned": "已分配",
            "available": "可用",
        },
    }
)


DISPATCHER = Actor(
    actor_id="dispatcher-01",
    role="dispatcher",
    clearance_tags={"region:north"},
)
AUDITOR = Actor(
    actor_id="auditor-01",
    role="auditor",
    clearance_tags={"*"},
)

EXPECTED_OPTIMAL_VALUE = 44600.0
