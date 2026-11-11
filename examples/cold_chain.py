"""冷链医药运单重派示例（Cold-chain shipment reroute）。

领域模型
--------
对象：
  * ``Warehouse``  冷库（冷链资质、同批次库存）
  * ``Truck``      冷藏车（冷链资质、到仓 ETA、可用状态）
  * ``Shipment``   运单（批次、数量、温区、状态、承运车、当前所在仓）
    - 源支撑字段来自底表；``dispatcher_note`` 为 Ontology 自有字段
  * ``Hospital``   收货医院（温区要求）

关系：
  * ``carries``     Truck    -> Shipment
  * ``ships_from``  Shipment -> Warehouse
  * ``delivers_to`` Shipment -> Hospital

函数：
  * ``recommend_reroute``  只读推演，列出满足冷链资质 / 库存的接替组合

动作：
  * ``reroute_shipment``   受治理动作：校验冷链资质、ETA <= 3h、同批次
    库存充足；``ai_agent`` 触发且额外成本 >= 2000 元时自动挂起等经理审批。
  * ``annotate_shipment``  纯本体自有动作（affects_source=False），只写
    ``dispatcher_note``，重刷底表后依靠 ``ontology_overlay`` 存活。

额外演示：
  * ObjectSet union / intersect / subtract / pivot / aggregate
  * 违规动作被拒后写入 status='REJECTED' 的纯业务审计记录

运行：``python3 -m examples.cold_chain``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from open_ontology import (
    Actor,
    InvariantError,
    LinkType,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    run_eval_suite,
)

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
    # source-backed row, but the dispatcher's field note is ontology-owned
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
# Link types
# ---------------------------------------------------------------------------
CARRIES = LinkType(
    name="carries", source="Truck", target="Shipment", cardinality="one-to-many"
)
SHIPS_FROM = LinkType(
    name="ships_from", source="Shipment", target="Warehouse", cardinality="many-to-one"
)
DELIVERS_TO = LinkType(
    name="delivers_to", source="Shipment", target="Hospital", cardinality="many-to-one"
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
for _link in (CARRIES, SHIPS_FROM, DELIVERS_TO):
    spec.add_link_type(_link)


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


def _approval_rule(params: RerouteParams, actor: Actor) -> bool:
    """AI agent spending >= 2000 CNY always needs a human manager."""
    return actor.role == "ai_agent" and params.extra_cost >= 2000.0


@spec.action(
    "reroute_shipment",
    RerouteParams,
    allowed_roles=("dispatcher", "ai_agent", "ops_manager"),
    approver_roles=("ops_manager",),
    approval_rule=_approval_rule,
    description="把冷链运单从抛锚卡车重派到接替仓 + 接替车（受治理）。",
)
def reroute_shipment(ctx: Any, params: RerouteParams) -> None:
    shipment = ctx.require(Shipment, params.shipment_id)
    warehouse = ctx.require(Warehouse, params.replacement_warehouse_id)
    truck = ctx.require(Truck, params.replacement_truck_id)

    # -- business invariants ------------------------------------------------
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

    # -- mutations (staged only; never direct DB writes) -------------------
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

    # -- relationship rewiring --------------------------------------------
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


# ---------------------------------------------------------------------------
# Demo seed + actors
# ---------------------------------------------------------------------------
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


@dataclass
class Demo:
    runtime: OntologyRuntime
    shipment_id: str = "SHP-001"
    warehouse_id: str = "WH-002"
    truck_id: str = "TRK-002"


def build_demo(db_path: str = ":memory:") -> Demo:
    runtime = OntologyRuntime(spec, db_path=db_path)

    objects = [
        Warehouse(
            warehouse_id="WH-001",
            name="北区中心冷库",
            region="north",
            cold_chain_certified=True,
            batch_stock={"BATCH-001": 500},
            dock_load=2,
        ),
        Warehouse(
            warehouse_id="WH-002",
            name="北区备用冷库",
            region="north",
            cold_chain_certified=True,
            batch_stock={"BATCH-001": 300},
            dock_load=0,
        ),
        Warehouse(
            warehouse_id="WH-003",
            name="南区普通仓",
            region="south",
            cold_chain_certified=False,
            batch_stock={"BATCH-001": 999},
        ),
        Truck(
            truck_id="TRK-001",
            plate="京A-1001",
            region="north",
            cold_chain_certified=True,
            status="assigned",
            eta_hours=0.5,
            current_warehouse_id="WH-001",
        ),
        Truck(
            truck_id="TRK-002",
            plate="京A-2002",
            region="north",
            cold_chain_certified=True,
            status="available",
            eta_hours=2.5,
            current_warehouse_id="WH-002",
        ),
        Truck(
            truck_id="TRK-003",
            plate="京A-3003",
            region="north",
            cold_chain_certified=False,
            status="available",
            eta_hours=1.0,
            current_warehouse_id="WH-002",
        ),
        Truck(
            truck_id="TRK-004",
            plate="京A-4004",
            region="north",
            cold_chain_certified=True,
            status="available",
            eta_hours=5.5,
            current_warehouse_id="WH-003",
        ),
        Shipment(
            shipment_id="SHP-001",
            batch_no="BATCH-001",
            product="胰岛素注射液",
            quantity=120,
            required_temp_c=2.0,
            status="in_transit",
            priority="urgent",
            region="north",
            origin_warehouse_id="WH-001",
            current_warehouse_id="WH-001",
            assigned_truck_id="TRK-001",
            destination_hospital_id="HOS-001",
        ),
        Hospital(
            hospital_id="HOS-001",
            name="市第一人民医院",
            region="north",
            required_temp_c=2.0,
        ),
    ]
    for obj in objects:
        runtime.put_object(obj)

    shipment = runtime.get_object(Shipment, "SHP-001", OPS_MANAGER)
    truck = runtime.get_object(Truck, "TRK-001", OPS_MANAGER)
    warehouse = runtime.get_object(Warehouse, "WH-001", OPS_MANAGER)
    hospital = runtime.get_object(Hospital, "HOS-001", OPS_MANAGER)

    # Seed relations through a governed runtime transaction: a tiny no-op is
    # not enough, so we replay them via a controlled submit-free helper.
    _seed_links(runtime, shipment, truck, warehouse, hospital)
    return Demo(runtime=runtime)


def _seed_links(
    runtime: OntologyRuntime,
    shipment: Shipment,
    truck: Truck,
    warehouse: Warehouse,
    hospital: Hospital,
) -> None:
    """Insert initial links through the runtime (gate-protected)."""
    runtime.link_objects(truck, CARRIES, shipment)
    runtime.link_objects(shipment, SHIPS_FROM, warehouse)
    runtime.link_objects(shipment, DELIVERS_TO, hospital)


# ---------------------------------------------------------------------------
# Demo script
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _carriers(runtime: OntologyRuntime, shipment_id: str) -> list[str]:
    shipment = runtime.get_object(Shipment, shipment_id, OPS_MANAGER)
    return sorted(
        t.truck_id
        for t in runtime.get_linked_to(shipment, CARRIES, Truck, OPS_MANAGER)
    )


def _base_snapshot(
    runtime: OntologyRuntime,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[tuple[str, str]]]]:
    """Build a source snapshot with every ontology-owned field stripped."""
    def rows(cls: type, owned: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        result = []
        for obj in runtime.query_objects(cls, OPS_MANAGER):
            data = obj.model_dump(mode="json")
            for field in owned:
                data.pop(field, None)
            result.append(data)
        return result

    objects = {
        "Warehouse": rows(Warehouse),
        "Truck": rows(Truck),
        "Shipment": rows(Shipment, ("dispatcher_note",)),
        "Hospital": rows(Hospital),
    }
    links: dict[str, list[tuple[str, str]]] = {}
    for link_type, source_id, target_id in runtime.connection.execute(
        "SELECT link_type, source_id, target_id FROM ontology_links "
        "ORDER BY link_type, source_id, target_id"
    ):
        links.setdefault(link_type, []).append((source_id, target_id))
    return objects, links


def main() -> dict[str, Any]:
    demo = build_demo()
    runtime = demo.runtime

    _section("1. 本体装载 / 初始状态")
    print("spec:", spec.summary())
    for object_type in ("Warehouse", "Truck", "Shipment", "Hospital"):
        print(f"  {object_type:<10} count={runtime.count_objects(object_type)}")
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    print(f"  {shipment.shipment_id} status={shipment.status} truck={shipment.assigned_truck_id}"
          f" version={runtime.list_objects('Shipment')[0]['version']}")

    _section("2. 只读推演函数 recommend_reroute")
    recommendation = runtime.call_function(
        "recommend_reroute",
        {"shipment_id": demo.shipment_id},
        DISPATCHER,
    )
    print("  candidates:")
    for candidate in recommendation["candidates"]:
        print(
            f"    - {candidate['warehouse_id']} + {candidate['truck_id']}"
            f" eta={candidate['eta_hours']}h stock={candidate['batch_stock']}"
        )
    assert recommendation["candidates"], "expected at least one reroute candidate"

    _section("3. AIP Evals（确定性评测器，零数据库写入）")
    eval_cases = [
        {
            "name": "dispatcher_low_cost_auto_commit",
            "action": "reroute_shipment",
            "params": {
                "shipment_id": "SHP-001",
                "replacement_warehouse_id": "WH-002",
                "replacement_truck_id": "TRK-002",
                "extra_cost": 800,
            },
            "expect": {
                "valid": True,
                "requires_approval": False,
                "object_changes": [
                    {
                        "object_type": "Shipment",
                        "object_id": "SHP-001",
                        "kind": "update",
                        "after": {"status": "rerouted", "assigned_truck_id": "TRK-002"},
                    }
                ],
                "link_changes": [
                    {
                        "link_type": "carries",
                        "source_id": "TRK-002",
                        "target_id": "SHP-001",
                        "kind": "create",
                    }
                ],
            },
        },
        {
            "name": "ai_agent_high_cost_requires_manager",
            "actor": AI_AGENT,
            "action": "reroute_shipment",
            "params": {
                "shipment_id": "SHP-001",
                "replacement_warehouse_id": "WH-002",
                "replacement_truck_id": "TRK-002",
                "extra_cost": 2400,
            },
            "expect": {"valid": True, "requires_approval": True},
        },
        {
            "name": "eta_over_limit_rejected",
            "action": "reroute_shipment",
            "params": {
                "shipment_id": "SHP-001",
                "replacement_warehouse_id": "WH-002",
                "replacement_truck_id": "TRK-004",
                "extra_cost": 500,
            },
            "expect": {"valid": False, "error_contains": "ETA"},
        },
    ]
    report = run_eval_suite(runtime, DISPATCHER, eval_cases)
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.name}")
        for failure in result.failures:
            print(f"         {failure}")
    print(f"  evals: {report.passed}/{report.total} passed, failed={report.failed}")

    _section("4. 字段级主权：annotate_shipment 只写 ontology-owned 字段")
    annotate_run = runtime.submit_action(
        "RUN-NOTE",
        "annotate_shipment",
        {
            "shipment_id": demo.shipment_id,
            "note": "优先保障胰岛素，到院前 30 分钟电话确认",
        },
        DISPATCHER,
    )
    print(f"  run={annotate_run['run_id']} status={annotate_run['status']}")
    change = annotate_run["object_diff"]["object_changes"][0]
    print(
        f"  diff={change['kind']}:{change['object_type']}:{change['object_id']}"
        f" dispatcher_note={change['after']['dispatcher_note']!r}"
    )
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    overlay_row = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Shipment' AND object_id = ?",
        (demo.shipment_id,),
    ).fetchone()
    print(f"  dispatcher_note={shipment.dispatcher_note!r}")
    print(f"  overlay={overlay_row['patch_json'] if overlay_row else None}")
    print(f"  version={runtime.list_objects('Shipment')[0]['version']}")

    _section("5. ObjectSet 集合代数 / pivot / aggregate")
    shipments = runtime.search_set(Shipment, OPS_MANAGER)
    carrying_trucks = runtime.pivot(shipments, CARRIES, OPS_MANAGER)
    warehouses = runtime.pivot(shipments, SHIPS_FROM, OPS_MANAGER, "forward")
    all_trucks = runtime.search_set(Truck, OPS_MANAGER)
    available_trucks = runtime.search_set(Truck, OPS_MANAGER, {"status": "available"})
    assigned_trucks = runtime.search_set(Truck, OPS_MANAGER, {"status": "assigned"})
    print(f"  shipments={shipments.ids}")
    print(f"  pivot(shipments -carries-> trucks)={carrying_trucks.ids}")
    print(f"  pivot(shipments -ships_from-> warehouses)={warehouses.ids}")
    print(
        "  union(available, assigned)="
        f"{runtime.union(available_trucks, assigned_trucks).ids}"
    )
    print(
        "  intersect(all, assigned)="
        f"{runtime.intersect(all_trucks, assigned_trucks).ids}"
    )
    print(
        "  subtract(all, assigned)="
        f"{runtime.subtract(all_trucks, assigned_trucks).ids}"
    )
    agg = runtime.aggregate(shipments, group_by="status", sum_field="quantity")
    for row in agg.rows:
        print(f"  aggregate status={row.key!r} metrics={row.metrics} ids={row.object_ids}")
    filtered = runtime.filter_aggregation(agg, lambda row: row.key == "in_transit")
    print(f"  filter_aggregation -> {filtered.object_set.ids}")

    _section("6. load_snapshot 重刷底表：owned 字段依靠 overlay 存活")
    snapshot_objects, snapshot_links = _base_snapshot(runtime)
    snapshot_result = runtime.load_snapshot(snapshot_objects, snapshot_links)
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    overlay_row = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Shipment' AND object_id = ?",
        (demo.shipment_id,),
    ).fetchone()
    print(f"  snapshot={snapshot_result}")
    print(f"  dispatcher_note after reindex={shipment.dispatcher_note!r} (must survive)")
    print(f"  overlay after reindex={overlay_row['patch_json'] if overlay_row else None}")
    print(f"  version after reindex={runtime.list_objects('Shipment')[0]['version']}")

    _section("7. 沙箱模拟（ai_agent 高成本 -> 需审批）")
    high_cost = {
        "shipment_id": "SHP-001",
        "replacement_warehouse_id": "WH-002",
        "replacement_truck_id": "TRK-002",
        "extra_cost": 2400,
        "reason": "冷链车抛锚，需紧急重派",
    }
    simulation = runtime.simulate_action("reroute_shipment", high_cost, AI_AGENT)
    version_before_sim = runtime.list_objects("Shipment")[0]["version"]
    print(f"  valid={simulation.valid} requires_approval={simulation.requires_approval}")
    print(f"  diff: {simulation.diff.summary()}")
    print(f"  read_versions={simulation.read_versions}")
    print(f"  db shipment version after simulate="
          f"{runtime.list_objects('Shipment')[0]['version']} (must stay {version_before_sim})")

    _section("8. 受控提交（调度员低成本 -> 原子提交）")
    low_cost = dict(high_cost, extra_cost=800)
    run = runtime.submit_action("RUN-0001", "reroute_shipment", low_cost, DISPATCHER)
    print(f"  run={run['run_id']} status={run['status']} approved_by={run['approved_by']}")
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    print(f"  shipment status={shipment.status} truck={shipment.assigned_truck_id}"
          f" warehouse={shipment.current_warehouse_id}")
    print(f"  carriers(SHP-001)={_carriers(runtime, demo.shipment_id)}")
    print(f"  action_runs={[(r['run_id'], r['status']) for r in runtime.list_action_runs()]}")

    _section("9. 时光机回滚（revert RUN-0001）")
    revert = runtime.revert_action("RUN-0001", "RUN-0001-REV", OPS_MANAGER)
    print(f"  revert run={revert['run_id']} status={revert['status']}")
    print(f"  original run status={runtime.get_action_run('RUN-0001')['status']}")
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    warehouse = runtime.get_object(Warehouse, "WH-002", OPS_MANAGER)
    print(f"  shipment status={shipment.status} truck={shipment.assigned_truck_id}"
          f" version={runtime.list_objects('Shipment')[0]['version']}")
    print(f"  carriers(SHP-001)={_carriers(runtime, demo.shipment_id)}")
    print(f"  WH-002 stock={warehouse.batch_stock}")

    _section("10. 治理审批（ai_agent 高成本 -> 经理审批）")
    pending = runtime.submit_action("RUN-0002", "reroute_shipment", high_cost, AI_AGENT)
    print(f"  run={pending['run_id']} status={pending['status']}")
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    print(f"  before approve: status={shipment.status} truck={shipment.assigned_truck_id}"
          f" (must be unchanged)")
    approved = runtime.approve_action("RUN-0002", OPS_MANAGER)
    print(f"  approved by={approved['approved_by']} status={approved['status']}")
    shipment = runtime.get_object(Shipment, demo.shipment_id, OPS_MANAGER)
    print(f"  after approve: status={shipment.status} truck={shipment.assigned_truck_id}"
          f" warehouse={shipment.current_warehouse_id}")
    print(f"  carriers(SHP-001)={_carriers(runtime, demo.shipment_id)}")

    _section("11. 被拒企图审计（Refusal Auditing）")
    violating = dict(high_cost, replacement_truck_id="TRK-004")
    try:
        runtime.submit_action(
            "RUN-REJECTED", "reroute_shipment", violating, DISPATCHER
        )
    except InvariantError as exc:
        print(f"  rejected code={exc.code} message={exc}")
    rejected = runtime.get_action_run("RUN-REJECTED")
    print(f"  persisted status={rejected['status']} error_code={rejected['error_code']}")
    print(f"  error_message={rejected['error_message']}")
    print(f"  object_diff={rejected['object_diff']} (must be empty)")
    print(f"  rejected_runs={[r['run_id'] for r in runtime.list_action_runs('REJECTED')]}")

    _section("12. 纯业务审计账本 ontology_action_runs")
    for row in runtime.list_action_runs():
        print(
            f"  run {row['run_id']:<14} {row['status']:<16}"
            f" approved_by={row['approved_by']}"
            f" error_code={row['error_code']}"
        )
    print("  Ontology 唯一输出事实：谁、何时、执行或撤回了哪个业务 Action、产生什么 ObjectDiff")

    summary = {
        "evals_passed": report.passed,
        "evals_total": report.total,
        "final_status": shipment.status,
        "final_truck": shipment.assigned_truck_id,
        "dispatcher_note": shipment.dispatcher_note,
        "action_runs": len(runtime.list_action_runs()),
        "rejected_runs": len(runtime.list_action_runs("REJECTED")),
    }
    _section("DONE")
    print("  summary:", summary)
    return summary


if __name__ == "__main__":
    main()
