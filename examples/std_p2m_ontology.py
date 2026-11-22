"""标准 P2M（Plan-to-Manufacture，计划到生产/制造执行）纯业务领域本体（Pure M1）。

严守常识细腰架构与 Gate 7 零横向互耦原则，仅基于 ``std_core_ontology`` 最小核心扩展：
* 主数据与单据对象：``BomSpec``（物料清单规格）、``WorkOrder``（生产工单）、
  ``MaterialConsumption``（生产领料/投料耗用记录）、``ProductionOutput``（完工/报废报工产出记录）；
* 受控动作：``release_work_order``、``issue_material_to_work_order``、
  ``record_production_output``、``close_work_order``；
* 只读查询函数：``diagnose_work_order_conservation``；
* 同步硬不变量：未下达禁领料、未领料禁报工与禁完工、已领料禁直接取消、
  吸收成本不得超耗已领料成本（成本守恒）、超 BOM 允许损耗率触发 ``APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED``
  交由 M1 ``WorkCard`` 审批与 $t_1$ 现场重算放行。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from open_ontology.induction.dsl import DeclarativeBlueprint, compose_blueprints
from open_ontology.spec import InvariantError, ObjectProperty, OntologySpec

try:
    from .business_ontology import MasterEntity, ProcessDocument
    from .std_core_ontology import (
        AccountingEntity,
        FulfillmentEvent,
        TradeCommitment,
        TradeResource,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import MasterEntity, ProcessDocument
    from examples.std_core_ontology import (
        AccountingEntity,
        FulfillmentEvent,
        TradeCommitment,
        TradeResource,
        WorkCard,
        build_std_core_blueprint,
        spec as std_core_spec,
    )

__all__ = [
    "spec",
    "AccountingEntity",
    "TradeResource",
    "TradeCommitment",
    "FulfillmentEvent",
    "WorkCard",
    "BomSpec",
    "WorkOrder",
    "MaterialConsumption",
    "ProductionOutput",
    "ReleaseWorkOrderParams",
    "IssueMaterialToWorkOrderParams",
    "RecordProductionOutputParams",
    "CloseWorkOrderParams",
    "DiagnoseWorkOrderConservationParams",
    "build_std_p2m_blueprint",
]

_WORK_ORDER_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("released", "cancelled"),
    "released": ("in_progress", "cancelled"),
    "in_progress": ("completed",),
    "completed": (),
    "cancelled": (),
}

_IMMUTABLE_RECORD_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "recorded": (),
}

spec = OntologySpec(extends=(std_core_spec,))


@spec.object_type
class BomSpec(MasterEntity):
    """标准 P2M 物料清单规格（薄公理配方骨架，不固化行业工艺排程细节）。"""

    __primary_key__: ClassVar[str] = "bom_id"
    __title__: ClassVar[str] = "bom_id"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = ("input_per_unit", "max_scrap_ratio")

    bom_id: str
    output_resource: ObjectProperty[TradeResource]
    input_resource: ObjectProperty[TradeResource]
    input_per_unit: float
    max_scrap_ratio: float = 0.05
    version: str = "1.0"
    status: str = "active"


@spec.object_type
class WorkOrder(ProcessDocument):
    """标准 P2M 生产工单（成本守恒与投入产出闭环聚合根）。"""

    __primary_key__: ClassVar[str] = "work_order_id"
    __title__: ClassVar[str] = "work_order_id"
    __aggregate_root__: ClassVar[bool] = True
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "planned_qty",
        "issued_qty",
        "issued_cost_cents",
        "completed_good_qty",
        "completed_scrap_qty",
    )
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("draft",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _WORK_ORDER_TRANSITIONS

    work_order_id: str
    accounting_entity: ObjectProperty[AccountingEntity]
    bom: ObjectProperty[BomSpec]
    commitment: ObjectProperty[TradeCommitment] | None = None
    output_resource: ObjectProperty[TradeResource]
    planned_qty: float
    issued_qty: float = 0.0
    issued_cost_cents: int = 0
    completed_good_qty: float = 0.0
    completed_scrap_qty: float = 0.0
    status: str = "draft"


@spec.object_type
class MaterialConsumption(ProcessDocument):
    """标准 P2M 生产领料/投料成本消耗凭证。"""

    __primary_key__: ClassVar[str] = "consumption_id"
    __title__: ClassVar[str] = "consumption_id"
    __aggregate_root__: ClassVar[bool] = False
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "qty",
        "unit_cost_cents",
        "total_cost_cents",
    )
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("recorded",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _IMMUTABLE_RECORD_TRANSITIONS

    consumption_id: str
    work_order: ObjectProperty[WorkOrder]
    input_resource: ObjectProperty[TradeResource]
    qty: float
    unit_cost_cents: int
    total_cost_cents: int
    allocation_rule: Literal["EXACT_LOT", "FIFO"] = "EXACT_LOT"
    source_event: ObjectProperty[FulfillmentEvent] | None = None
    status: str = "recorded"


@spec.object_type
class ProductionOutput(ProcessDocument):
    """标准 P2M 生产报工产出与吸收成本凭证。"""

    __primary_key__: ClassVar[str] = "output_id"
    __title__: ClassVar[str] = "output_id"
    __aggregate_root__: ClassVar[bool] = False
    __non_negative_fields__: ClassVar[tuple[str, ...]] = (
        "good_qty",
        "scrap_qty",
        "absorbed_cost_cents",
    )
    __status_field__: ClassVar[str] = "status"
    __initial_statuses__: ClassVar[tuple[str, ...]] = ("recorded",)
    __transitions__: ClassVar[dict[str, tuple[str, ...]]] = _IMMUTABLE_RECORD_TRANSITIONS

    output_id: str
    work_order: ObjectProperty[WorkOrder]
    output_resource: ObjectProperty[TradeResource]
    good_qty: float
    scrap_qty: float = 0.0
    absorbed_cost_cents: int
    output_event: ObjectProperty[FulfillmentEvent] | None = None
    status: str = "recorded"


class ReleaseWorkOrderParams(BaseModel):
    work_order_id: str


class IssueMaterialToWorkOrderParams(BaseModel):
    consumption_id: str
    work_order_id: str
    input_resource: str
    qty: float
    unit_cost_cents: int
    total_cost_cents: int = -1
    allocation_rule: str = "EXACT_LOT"
    source_event: str = ""


class RecordProductionOutputParams(BaseModel):
    output_id: str
    work_order_id: str
    output_resource: str
    good_qty: float
    scrap_qty: float = 0.0
    absorbed_cost_cents: int = 0
    output_event: str = ""


class CloseWorkOrderParams(BaseModel):
    work_order_id: str
    target_status: str = "completed"


class DiagnoseWorkOrderConservationParams(BaseModel):
    work_order_id: str = ""


@spec.invariant(
    "p2m_bom_positive_ratio",
    code="INVALID_BOM_RATIO",
    description="BomSpec 单耗必须大于 0 且最大损耗率须位于 [0, 1]。",
)
def p2m_bom_positive_ratio(view: Any) -> bool:
    for bom in view.query(BomSpec):
        if bom.input_per_unit <= 0:
            raise InvariantError(
                f"BomSpec:{bom.bom_id}.input_per_unit={bom.input_per_unit} must be > 0",
                code="INVALID_BOM_RATIO",
            )
        if bom.max_scrap_ratio < 0 or bom.max_scrap_ratio > 1.0:
            raise InvariantError(
                f"BomSpec:{bom.bom_id}.max_scrap_ratio={bom.max_scrap_ratio} must be in [0, 1]",
                code="INVALID_BOM_RATIO",
            )
    return True


@spec.invariant(
    "p2m_work_order_cost_conservation",
    code="COST_CONSERVATION_VIOLATION",
    description="生产工单累计产出吸收成本不得超过已领料投入总成本。",
)
def p2m_work_order_cost_conservation(view: Any) -> bool:
    absorbed_by_wo: dict[str, int] = {}
    for out in view.query(ProductionOutput):
        absorbed_by_wo[out.work_order] = absorbed_by_wo.get(out.work_order, 0) + int(out.absorbed_cost_cents)
    for wo in view.query(WorkOrder):
        absorbed = absorbed_by_wo.get(wo.work_order_id, 0)
        if absorbed > wo.issued_cost_cents:
            raise InvariantError(
                f"WorkOrder:{wo.work_order_id} absorbed_cost_cents={absorbed} exceeds issued_cost_cents={wo.issued_cost_cents}",
                code="COST_CONSERVATION_VIOLATION",
            )
    return True


def _has_completed_scrap_work_card(ctx: Any, output_id: str) -> bool:
    for card in ctx.query(WorkCard):
        if (
            card.status == "COMPLETED"
            and card.card_kind == "APPROVAL"
            and card.assignee_role in ("plant_manager", "admin")
            and card.target_object_type == "ProductionOutput"
            and card.target_object_id == output_id
            and card.requested_action == "record_production_output"
        ):
            return True
    return False


@spec.action(
    "release_work_order",
    ReleaseWorkOrderParams,
    allowed_roles=("production_planner", "plant_manager", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("WorkOrder",),
    description="校验 BOM 有效与正计划量后将草稿生产工单下达至车间（draft -> released）。",
)
def release_work_order(ctx: Any, params: ReleaseWorkOrderParams) -> None:
    wo = ctx.require(WorkOrder, params.work_order_id)
    if wo.status != "draft":
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} status is {wo.status!r}, expected 'draft'",
            code="WORK_ORDER_NOT_DRAFT",
        )
    bom = ctx.require(BomSpec, wo.bom)
    if bom.status != "active":
        raise InvariantError(
            f"BomSpec:{bom.bom_id} status is {bom.status!r}, expected 'active'",
            code="BOM_NOT_ACTIVE",
        )
    if bom.input_per_unit <= 0:
        raise InvariantError(
            f"BomSpec:{bom.bom_id} input_per_unit={bom.input_per_unit} must be > 0",
            code="INVALID_BOM_RATIO",
        )
    if bom.output_resource != wo.output_resource:
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} output_resource={wo.output_resource!r} != BomSpec output_resource={bom.output_resource!r}",
            code="WORK_ORDER_OUTPUT_MISMATCH",
        )
    if wo.planned_qty <= 0:
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} planned_qty={wo.planned_qty} must be > 0",
            code="NON_POSITIVE_PLANNED_QTY",
        )
    ctx.modify(wo, status="released")


@spec.action(
    "issue_material_to_work_order",
    IssueMaterialToWorkOrderParams,
    allowed_roles=("warehouse", "shopfloor", "production_planner", "plant_manager", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("WorkOrder", "MaterialConsumption"),
    description="向已下达或生产中工单领料投料，自动推进至 in_progress 并累加投入成本。",
)
def issue_material_to_work_order(ctx: Any, params: IssueMaterialToWorkOrderParams) -> None:
    wo = ctx.require(WorkOrder, params.work_order_id)
    if wo.status not in ("released", "in_progress"):
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} status {wo.status!r} is not open for material issue",
            code="WORK_ORDER_NOT_OPEN_FOR_ISSUE",
        )
    if ctx.get(MaterialConsumption, params.consumption_id) is not None:
        raise InvariantError(
            f"MaterialConsumption:{params.consumption_id} already exists",
            code="CONSUMPTION_ALREADY_EXISTS",
        )
    if params.qty <= 0:
        raise InvariantError("qty must be > 0", code="NON_POSITIVE_ISSUE_QTY")
    if params.unit_cost_cents < 0:
        raise InvariantError("unit_cost_cents must be >= 0", code="NEGATIVE_UNIT_COST")
    if params.allocation_rule not in ("EXACT_LOT", "FIFO"):
        raise InvariantError(
            f"allocation_rule {params.allocation_rule!r} must be 'EXACT_LOT' or 'FIFO'",
            code="INVALID_ALLOCATION_RULE",
        )
    bom = ctx.require(BomSpec, wo.bom)
    if params.input_resource != bom.input_resource:
        raise InvariantError(
            f"input_resource={params.input_resource!r} does not match BomSpec:{bom.bom_id}.input_resource={bom.input_resource!r}",
            code="MATERIAL_NOT_IN_BOM",
        )
    expected_cost = int(round(params.qty * params.unit_cost_cents))
    if params.total_cost_cents >= 0 and params.total_cost_cents != expected_cost:
        raise InvariantError(
            f"total_cost_cents={params.total_cost_cents} != round(qty * unit_cost_cents)={expected_cost}",
            code="ISSUE_COST_MISMATCH",
        )
    eff_cost = params.total_cost_cents if params.total_cost_cents >= 0 else expected_cost
    rule: Literal["EXACT_LOT", "FIFO"] = "FIFO" if params.allocation_rule == "FIFO" else "EXACT_LOT"
    ctx.create(
        MaterialConsumption(
            consumption_id=params.consumption_id,
            work_order=wo.work_order_id,
            input_resource=params.input_resource,
            qty=params.qty,
            unit_cost_cents=params.unit_cost_cents,
            total_cost_cents=eff_cost,
            allocation_rule=rule,
            source_event=params.source_event or None,
            status="recorded",
        )
    )
    ctx.modify(
        wo,
        issued_qty=round(wo.issued_qty + params.qty, 6),
        issued_cost_cents=wo.issued_cost_cents + eff_cost,
        status="in_progress",
    )


@spec.action(
    "record_production_output",
    RecordProductionOutputParams,
    allowed_roles=("shopfloor", "production_planner", "plant_manager", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("WorkOrder", "ProductionOutput"),
    description="登记工单完工与报废产出；强守未投料禁报工、吸收成本守恒及超损耗率 WorkCard 审批门禁。",
)
def record_production_output(ctx: Any, params: RecordProductionOutputParams) -> None:
    wo = ctx.require(WorkOrder, params.work_order_id)
    if wo.status in ("completed", "cancelled"):
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} status is {wo.status!r}, expected 'in_progress'",
            code="WORK_ORDER_NOT_IN_PROGRESS",
        )
    if wo.status != "in_progress" or wo.issued_qty <= 0:
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} cannot record production output before material issue",
            code="CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE",
        )
    if ctx.get(ProductionOutput, params.output_id) is not None:
        raise InvariantError(
            f"ProductionOutput:{params.output_id} already exists",
            code="OUTPUT_ALREADY_EXISTS",
        )
    if params.good_qty < 0 or params.scrap_qty < 0:
        raise InvariantError("good_qty and scrap_qty must be >= 0", code="NEGATIVE_OUTPUT_QTY")
    if params.good_qty + params.scrap_qty <= 0:
        raise InvariantError("good_qty + scrap_qty must be > 0", code="NON_POSITIVE_OUTPUT_QTY")
    if params.absorbed_cost_cents < 0:
        raise InvariantError("absorbed_cost_cents must be >= 0", code="NEGATIVE_ABSORBED_COST")
    if params.output_resource != wo.output_resource:
        raise InvariantError(
            f"output_resource={params.output_resource!r} != WorkOrder.output_resource={wo.output_resource!r}",
            code="OUTPUT_RESOURCE_MISMATCH",
        )

    already_absorbed = sum(
        int(out.absorbed_cost_cents)
        for out in ctx.query(ProductionOutput)
        if out.work_order == wo.work_order_id
    )
    remaining_unabsorbed = wo.issued_cost_cents - already_absorbed
    if params.absorbed_cost_cents > remaining_unabsorbed:
        raise InvariantError(
            f"absorbed_cost_cents={params.absorbed_cost_cents} exceeds remaining unabsorbed cost {remaining_unabsorbed}",
            code="COST_CONSERVATION_VIOLATION",
        )

    bom = ctx.require(BomSpec, wo.bom)
    cum_good = round(wo.completed_good_qty + params.good_qty, 6)
    cum_scrap = round(wo.completed_scrap_qty + params.scrap_qty, 6)
    cum_total = cum_good + cum_scrap
    if (
        cum_scrap > bom.max_scrap_ratio * cum_total + 1e-9
        and ctx.actor.role not in ("plant_manager", "admin")
        and not _has_completed_scrap_work_card(ctx, params.output_id)
    ):
        scrap_ratio = cum_scrap / cum_total
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} cumulative scrap ratio {scrap_ratio:.4f} exceeds max_scrap_ratio={bom.max_scrap_ratio:.4f}; requires plant_manager approval",
            code="APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED",
        )

    ctx.create(
        ProductionOutput(
            output_id=params.output_id,
            work_order=wo.work_order_id,
            output_resource=params.output_resource,
            good_qty=params.good_qty,
            scrap_qty=params.scrap_qty,
            absorbed_cost_cents=params.absorbed_cost_cents,
            output_event=params.output_event or None,
            status="recorded",
        )
    )
    ctx.modify(
        wo,
        completed_good_qty=cum_good,
        completed_scrap_qty=cum_scrap,
    )


@spec.action(
    "close_work_order",
    CloseWorkOrderParams,
    allowed_roles=("production_planner", "plant_manager", "shopfloor", "ops", "steward", "ai_agent", "admin"),
    touched_object_types=("WorkOrder",),
    description="完工关闭工单（须已领料且有良品产出）或取消未领料工单（已领料工单禁直接取消）。",
)
def close_work_order(ctx: Any, params: CloseWorkOrderParams) -> None:
    wo = ctx.require(WorkOrder, params.work_order_id)
    if params.target_status not in ("completed", "cancelled"):
        raise InvariantError(
            f"target_status={params.target_status!r} must be 'completed' or 'cancelled'",
            code="INVALID_WORK_ORDER_CLOSE_TARGET",
        )
    if params.target_status == "cancelled":
        if wo.issued_qty > 0 or wo.status == "in_progress":
            raise InvariantError(
                f"WorkOrder:{wo.work_order_id} already has material issued (issued_qty={wo.issued_qty}); cannot cancel",
                code="CANNOT_CANCEL_ISSUED_WORK_ORDER",
            )
        if wo.status not in ("draft", "released"):
            raise InvariantError(
                f"WorkOrder:{wo.work_order_id} status {wo.status!r} cannot transition to 'cancelled'",
                code="WORK_ORDER_NOT_CANCELLABLE",
            )
        ctx.modify(wo, status="cancelled")
        return

    if wo.status in ("completed", "cancelled"):
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} status {wo.status!r} cannot transition to 'completed'",
            code="WORK_ORDER_NOT_IN_PROGRESS",
        )
    if wo.status != "in_progress" or wo.issued_qty <= 0:
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} cannot complete without material issue",
            code="CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE",
        )
    if wo.completed_good_qty <= 0:
        raise InvariantError(
            f"WorkOrder:{wo.work_order_id} requires completed_good_qty > 0 before closing",
            code="WORK_ORDER_NO_GOOD_OUTPUT",
        )
    ctx.modify(wo, status="completed")


@spec.function(
    "diagnose_work_order_conservation",
    DiagnoseWorkOrderConservationParams,
    description="只读诊断生产工单的投入产出数量、吸收成本守恒残差与损耗率指标。",
)
def diagnose_work_order_conservation(
    ctx: Any, params: DiagnoseWorkOrderConservationParams
) -> dict[str, Any]:
    work_orders = (
        [ctx.require(WorkOrder, params.work_order_id)]
        if params.work_order_id.strip()
        else sorted(ctx.query(WorkOrder), key=lambda w: w.work_order_id)
    )
    consumptions = list(ctx.query(MaterialConsumption))
    outputs = list(ctx.query(ProductionOutput))

    rows: list[dict[str, Any]] = []
    all_conserved = True
    for wo in work_orders:
        wo_cons = [c for c in consumptions if c.work_order == wo.work_order_id]
        wo_outs = [o for o in outputs if o.work_order == wo.work_order_id]
        cons_cost = sum(int(c.total_cost_cents) for c in wo_cons)
        absorbed_cost = sum(int(o.absorbed_cost_cents) for o in wo_outs)
        unabsorbed_cost = wo.issued_cost_cents - absorbed_cost
        total_out = wo.completed_good_qty + wo.completed_scrap_qty
        scrap_ratio = (wo.completed_scrap_qty / total_out) if total_out > 0 else 0.0
        cost_conserved = absorbed_cost <= wo.issued_cost_cents and cons_cost == wo.issued_cost_cents
        if not cost_conserved:
            all_conserved = False
        rows.append(
            {
                "work_order_id": wo.work_order_id,
                "status": wo.status,
                "issued_qty": wo.issued_qty,
                "issued_cost_cents": wo.issued_cost_cents,
                "consumption_cost_cents": cons_cost,
                "absorbed_cost_cents": absorbed_cost,
                "remaining_unabsorbed_cost_cents": unabsorbed_cost,
                "completed_good_qty": wo.completed_good_qty,
                "completed_scrap_qty": wo.completed_scrap_qty,
                "scrap_ratio": round(scrap_ratio, 6),
                "cost_conserved": cost_conserved,
            }
        )
    return {
        "work_order_count": len(rows),
        "all_cost_conserved": all_conserved,
        "work_orders": rows,
    }


spec.update_display(
    {
        "ontologies": {
            "std_p2m_ontology": "标准 P2M 领域本体（物料清单·生产工单·领料耗用·报工产出）",
        },
        "objects": {
            "BomSpec": "物料清单规格",
            "WorkOrder": "生产工单",
            "MaterialConsumption": "生产领料耗用",
            "ProductionOutput": "生产报工产出",
        },
        "actions": {
            "release_work_order": "下达生产工单",
            "issue_material_to_work_order": "工单领料投料",
            "record_production_output": "生产报工产出",
            "close_work_order": "关闭或取消工单",
        },
        "functions": {
            "diagnose_work_order_conservation": "诊断工单成本守恒",
        },
        "fields": {
            "bom_id": "BOM 编码",
            "output_resource": "产出物料资源",
            "input_resource": "投入物料资源",
            "input_per_unit": "单位产品耗用量",
            "max_scrap_ratio": "最大允许损耗率",
            "version": "版本号",
            "work_order_id": "生产工单编码",
            "accounting_entity": "核算主体",
            "bom": "关联 BOM",
            "commitment": "关联交易承诺",
            "planned_qty": "计划产量",
            "issued_qty": "已领料数量",
            "issued_cost_cents": "已领料总成本（分）",
            "completed_good_qty": "累计良品数量",
            "completed_scrap_qty": "累计报废数量",
            "consumption_id": "领料耗用编码",
            "work_order": "所属生产工单",
            "qty": "领料数量",
            "unit_cost_cents": "单位成本（分）",
            "total_cost_cents": "总成本（分）",
            "allocation_rule": "成本分摊规则",
            "source_event": "来源履约事件",
            "output_id": "报工产出编码",
            "good_qty": "良品数量",
            "scrap_qty": "报废数量",
            "absorbed_cost_cents": "吸收成本（分）",
            "output_event": "产出履约事件",
            "status": "状态",
        },
    }
)


def build_std_p2m_blueprint() -> DeclarativeBlueprint:
    """构建与 ``std_p2m_ontology`` 对称的零 ``exec/eval`` 声明式领域蓝图（基于 std_core 组合）。"""
    eff_cost_expr: dict[str, Any] = {
        "op": "if_else",
        "cond": {"op": "ge", "left": {"ref": "param.total_cost_cents"}, "right": {"lit": 0}},
        "then": {"ref": "param.total_cost_cents"},
        "else": {
            "op": "mul",
            "left": {"ref": "param.qty"},
            "right": {"ref": "param.unit_cost_cents"},
        },
    }
    absorbed_sum_expr: dict[str, Any] = {
        "op": "sum",
        "object_type": "ProductionOutput",
        "field": "absorbed_cost_cents",
        "where": {
            "op": "eq",
            "left": {"ref": "target.work_order"},
            "right": {"ref": "obj.work_order_id"},
        },
    }
    cum_scrap_expr: dict[str, Any] = {
        "op": "add",
        "left": {"ref": "obj.completed_scrap_qty"},
        "right": {"ref": "param.scrap_qty"},
    }
    cum_good_expr: dict[str, Any] = {
        "op": "add",
        "left": {"ref": "obj.completed_good_qty"},
        "right": {"ref": "param.good_qty"},
    }
    cum_total_expr: dict[str, Any] = {
        "op": "add",
        "left": cum_good_expr,
        "right": cum_scrap_expr,
    }

    p2m_fragment: dict[str, Any] = {
        "model_id": "_std_p2m_domain_fragment",
        "version": "1.0.0",
        "seed_ontology_id": "business_ontology",
        "object_types": [
            {
                "name": "BomSpec",
                "archetype": "MasterEntity",
                "primary_key": "bom_id",
                "title_field": "bom_id",
                "aggregate_root": True,
                "owned": False,
                "display_label": "物料清单规格",
                "fields": [
                    {"name": "bom_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "input_per_unit", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "max_scrap_ratio", "field_type": "float", "default": 0.05, "non_negative": True},
                    {"name": "version", "field_type": "str", "default": "1.0"},
                    {"name": "status", "field_type": "str", "default": "active"},
                ],
                "relations": [
                    {"field": "output_resource", "target_type": "TradeResource", "required": True},
                    {"field": "input_resource", "target_type": "TradeResource", "required": True},
                ],
            },
            {
                "name": "WorkOrder",
                "archetype": "ProcessDocument",
                "primary_key": "work_order_id",
                "title_field": "work_order_id",
                "aggregate_root": True,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["draft"],
                "transitions": {k: list(v) for k, v in _WORK_ORDER_TRANSITIONS.items()},
                "display_label": "生产工单",
                "fields": [
                    {"name": "work_order_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "planned_qty", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "issued_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "issued_cost_cents", "field_type": "int", "default": 0, "non_negative": True},
                    {"name": "completed_good_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "completed_scrap_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "draft"},
                ],
                "relations": [
                    {"field": "accounting_entity", "target_type": "AccountingEntity", "required": True},
                    {"field": "bom", "target_type": "BomSpec", "required": True},
                    {"field": "commitment", "target_type": "TradeCommitment", "required": False},
                    {"field": "output_resource", "target_type": "TradeResource", "required": True},
                ],
            },
            {
                "name": "MaterialConsumption",
                "archetype": "ProcessDocument",
                "primary_key": "consumption_id",
                "title_field": "consumption_id",
                "aggregate_root": False,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["recorded"],
                "transitions": {k: list(v) for k, v in _IMMUTABLE_RECORD_TRANSITIONS.items()},
                "display_label": "生产领料耗用",
                "fields": [
                    {"name": "consumption_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "qty", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "unit_cost_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "total_cost_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "allocation_rule", "field_type": "str", "default": "EXACT_LOT"},
                    {"name": "status", "field_type": "str", "default": "recorded"},
                ],
                "relations": [
                    {"field": "work_order", "target_type": "WorkOrder", "required": True},
                    {"field": "input_resource", "target_type": "TradeResource", "required": True},
                    {"field": "source_event", "target_type": "FulfillmentEvent", "required": False},
                ],
            },
            {
                "name": "ProductionOutput",
                "archetype": "ProcessDocument",
                "primary_key": "output_id",
                "title_field": "output_id",
                "aggregate_root": False,
                "owned": False,
                "status_field": "status",
                "initial_statuses": ["recorded"],
                "transitions": {k: list(v) for k, v in _IMMUTABLE_RECORD_TRANSITIONS.items()},
                "display_label": "生产报工产出",
                "fields": [
                    {"name": "output_id", "field_type": "str", "primary_key": True, "required": True},
                    {"name": "good_qty", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "scrap_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "absorbed_cost_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "status", "field_type": "str", "default": "recorded"},
                ],
                "relations": [
                    {"field": "work_order", "target_type": "WorkOrder", "required": True},
                    {"field": "output_resource", "target_type": "TradeResource", "required": True},
                    {"field": "output_event", "target_type": "FulfillmentEvent", "required": False},
                ],
            },
        ],
        "invariants": [
            {
                "name": "p2m_bom_positive_ratio",
                "code": "INVALID_BOM_RATIO",
                "target_type": "BomSpec",
                "must_hold": {
                    "op": "and",
                    "args": [
                        {"op": "gt", "left": {"ref": "obj.input_per_unit"}, "right": {"lit": 0}},
                        {"op": "ge", "left": {"ref": "obj.max_scrap_ratio"}, "right": {"lit": 0.0}},
                        {"op": "le", "left": {"ref": "obj.max_scrap_ratio"}, "right": {"lit": 1.0}},
                    ],
                },
                "description": "BomSpec 单耗必须大于 0 且最大损耗率须位于 [0, 1]。",
            },
            {
                "name": "p2m_work_order_cost_conservation",
                "code": "COST_CONSERVATION_VIOLATION",
                "target_type": "WorkOrder",
                "must_hold": {
                    "op": "le",
                    "left": absorbed_sum_expr,
                    "right": {"ref": "obj.issued_cost_cents"},
                },
                "description": "生产工单累计产出吸收成本不得超过已领料投入总成本。",
            },
        ],
        "actions": [
            {
                "name": "release_work_order",
                "display_label": "下达生产工单",
                "description": "校验 BOM 有效与正计划量后将草稿生产工单下达至车间（draft -> released）。",
                "allowed_roles": ["production_planner", "plant_manager", "ops", "steward", "ai_agent", "admin"],
                "params": [{"name": "work_order_id", "field_type": "str", "required": True}],
                "preconditions": [
                    {
                        "name": "p2m_release_wo_must_be_draft",
                        "code": "WORK_ORDER_NOT_DRAFT",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "draft"}},
                    },
                    {
                        "name": "p2m_release_bom_must_be_active",
                        "code": "BOM_NOT_ACTIVE",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {"op": "eq", "left": {"ref": "obj.bom.status"}, "right": {"lit": "active"}},
                    },
                    {
                        "name": "p2m_release_bom_positive_ratio",
                        "code": "INVALID_BOM_RATIO",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {"op": "gt", "left": {"ref": "obj.bom.input_per_unit"}, "right": {"lit": 0}},
                    },
                    {
                        "name": "p2m_release_output_matches_bom",
                        "code": "WORK_ORDER_OUTPUT_MISMATCH",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "obj.output_resource"},
                            "right": {"ref": "obj.bom.output_resource"},
                        },
                    },
                    {
                        "name": "p2m_release_positive_planned_qty",
                        "code": "NON_POSITIVE_PLANNED_QTY",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {"op": "gt", "left": {"ref": "obj.planned_qty"}, "right": {"lit": 0}},
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "set_fields": {"status": {"lit": "released"}},
                    }
                ],
            },
            {
                "name": "issue_material_to_work_order",
                "display_label": "工单领料投料",
                "description": "向已下达或生产中工单领料投料，自动推进至 in_progress 并累加投入成本。",
                "allowed_roles": [
                    "warehouse",
                    "shopfloor",
                    "production_planner",
                    "plant_manager",
                    "ops",
                    "steward",
                    "ai_agent",
                    "admin",
                ],
                "params": [
                    {"name": "consumption_id", "field_type": "str", "required": True},
                    {"name": "work_order_id", "field_type": "str", "required": True},
                    {"name": "input_resource", "field_type": "str", "required": True},
                    {"name": "qty", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "unit_cost_cents", "field_type": "int", "required": True, "non_negative": True},
                    {"name": "total_cost_cents", "field_type": "int", "default": -1},
                    {"name": "allocation_rule", "field_type": "str", "default": "EXACT_LOT"},
                    {"name": "source_event", "field_type": "str", "default": ""},
                ],
                "preconditions": [
                    {
                        "name": "p2m_issue_wo_open",
                        "code": "WORK_ORDER_NOT_OPEN_FOR_ISSUE",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["released", "in_progress"]},
                        },
                    },
                    {
                        "name": "p2m_issue_positive_qty",
                        "code": "NON_POSITIVE_ISSUE_QTY",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {"op": "gt", "left": {"ref": "param.qty"}, "right": {"lit": 0}},
                    },
                    {
                        "name": "p2m_issue_non_neg_unit_cost",
                        "code": "NEGATIVE_UNIT_COST",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {"op": "ge", "left": {"ref": "param.unit_cost_cents"}, "right": {"lit": 0}},
                    },
                    {
                        "name": "p2m_issue_valid_allocation_rule",
                        "code": "INVALID_ALLOCATION_RULE",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "param.allocation_rule"},
                            "right": {"lit": ["EXACT_LOT", "FIFO"]},
                        },
                    },
                    {
                        "name": "p2m_issue_resource_in_bom",
                        "code": "MATERIAL_NOT_IN_BOM",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "param.input_resource"},
                            "right": {"ref": "obj.bom.input_resource"},
                        },
                    },
                    {
                        "name": "p2m_issue_cost_matches_qty_unit_cost",
                        "code": "ISSUE_COST_MISMATCH",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {"op": "ge", "left": {"ref": "param.total_cost_cents"}, "right": {"lit": 0}},
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "le",
                                    "left": {
                                        "op": "sub",
                                        "left": {"ref": "param.total_cost_cents"},
                                        "right": {
                                            "op": "mul",
                                            "left": {"ref": "param.qty"},
                                            "right": {"ref": "param.unit_cost_cents"},
                                        },
                                    },
                                    "right": {"lit": 0.5},
                                },
                                {
                                    "op": "ge",
                                    "left": {
                                        "op": "sub",
                                        "left": {"ref": "param.total_cost_cents"},
                                        "right": {
                                            "op": "mul",
                                            "left": {"ref": "param.qty"},
                                            "right": {"ref": "param.unit_cost_cents"},
                                        },
                                    },
                                    "right": {"lit": -0.5},
                                },
                            ],
                        },
                    },
                ],
                "mutations": [
                    {
                        "op": "create",
                        "object_type": "MaterialConsumption",
                        "id_param": "consumption_id",
                        "set_fields": {
                            "consumption_id": {"ref": "param.consumption_id"},
                            "work_order": {"ref": "param.work_order_id"},
                            "input_resource": {"ref": "param.input_resource"},
                            "qty": {"ref": "param.qty"},
                            "unit_cost_cents": {"ref": "param.unit_cost_cents"},
                            "total_cost_cents": eff_cost_expr,
                            "allocation_rule": {"ref": "param.allocation_rule"},
                            "source_event": {
                                "op": "if_else",
                                "cond": {"op": "eq", "left": {"ref": "param.source_event"}, "right": {"lit": ""}},
                                "then": {"lit": None},
                                "else": {"ref": "param.source_event"},
                            },
                            "status": {"lit": "recorded"},
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "set_fields": {
                            "issued_qty": {
                                "op": "add",
                                "left": {"ref": "obj.issued_qty"},
                                "right": {"ref": "param.qty"},
                            },
                            "issued_cost_cents": {
                                "op": "add",
                                "left": {"ref": "obj.issued_cost_cents"},
                                "right": eff_cost_expr,
                            },
                            "status": {"lit": "in_progress"},
                        },
                    },
                ],
            },
            {
                "name": "record_production_output",
                "display_label": "生产报工产出",
                "description": "登记工单完工与报废产出；强守未投料禁报工、吸收成本守恒及超损耗率 WorkCard 审批门禁。",
                "allowed_roles": ["shopfloor", "production_planner", "plant_manager", "ops", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "output_id", "field_type": "str", "required": True},
                    {"name": "work_order_id", "field_type": "str", "required": True},
                    {"name": "output_resource", "field_type": "str", "required": True},
                    {"name": "good_qty", "field_type": "float", "required": True, "non_negative": True},
                    {"name": "scrap_qty", "field_type": "float", "default": 0.0, "non_negative": True},
                    {"name": "absorbed_cost_cents", "field_type": "int", "default": 0, "non_negative": True},
                    {"name": "output_event", "field_type": "str", "default": ""},
                ],
                "preconditions": [
                    {
                        "name": "p2m_output_wo_not_terminal",
                        "code": "WORK_ORDER_NOT_IN_PROGRESS",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "not_in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["completed", "cancelled"]},
                        },
                    },
                    {
                        "name": "p2m_output_requires_material_issue",
                        "code": "CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "in_progress"}},
                                {"op": "gt", "left": {"ref": "obj.issued_qty"}, "right": {"lit": 0}},
                            ],
                        },
                    },
                    {
                        "name": "p2m_output_non_neg_qtys",
                        "code": "NEGATIVE_OUTPUT_QTY",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "and",
                            "args": [
                                {"op": "ge", "left": {"ref": "param.good_qty"}, "right": {"lit": 0}},
                                {"op": "ge", "left": {"ref": "param.scrap_qty"}, "right": {"lit": 0}},
                            ],
                        },
                    },
                    {
                        "name": "p2m_output_positive_total_qty",
                        "code": "NON_POSITIVE_OUTPUT_QTY",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "gt",
                            "left": {"op": "add", "left": {"ref": "param.good_qty"}, "right": {"ref": "param.scrap_qty"}},
                            "right": {"lit": 0},
                        },
                    },
                    {
                        "name": "p2m_output_resource_match",
                        "code": "OUTPUT_RESOURCE_MISMATCH",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "eq",
                            "left": {"ref": "param.output_resource"},
                            "right": {"ref": "obj.output_resource"},
                        },
                    },
                    {
                        "name": "p2m_output_cost_conservation",
                        "code": "COST_CONSERVATION_VIOLATION",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "le",
                            "left": {
                                "op": "add",
                                "left": absorbed_sum_expr,
                                "right": {"ref": "param.absorbed_cost_cents"},
                            },
                            "right": {"ref": "obj.issued_cost_cents"},
                        },
                    },
                    {
                        "name": "p2m_output_scrap_ratio_approval_gate",
                        "code": "APPROVAL_REQUIRED_SCRAP_RATIO_EXCEEDED",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {
                            "op": "gt",
                            "left": cum_scrap_expr,
                            "right": {
                                "op": "add",
                                "left": {
                                    "op": "mul",
                                    "left": {"ref": "obj.bom.max_scrap_ratio"},
                                    "right": cum_total_expr,
                                },
                                "right": {"lit": 1e-9},
                            },
                        },
                        "must_hold": {
                            "op": "or",
                            "args": [
                                {"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["plant_manager", "admin"]}},
                                {
                                    "op": "exists",
                                    "object_type": "WorkCard",
                                    "where": {
                                        "op": "and",
                                        "args": [
                                            {"op": "eq", "left": {"ref": "target.status"}, "right": {"lit": "COMPLETED"}},
                                            {"op": "eq", "left": {"ref": "target.card_kind"}, "right": {"lit": "APPROVAL"}},
                                            {"op": "in", "left": {"ref": "target.assignee_role"}, "right": {"lit": ["plant_manager", "admin"]}},
                                            {"op": "eq", "left": {"ref": "target.target_object_type"}, "right": {"lit": "ProductionOutput"}},
                                            {"op": "eq", "left": {"ref": "target.target_object_id"}, "right": {"ref": "param.output_id"}},
                                            {"op": "eq", "left": {"ref": "target.requested_action"}, "right": {"lit": "record_production_output"}},
                                        ],
                                    },
                                },
                            ],
                        },
                    },
                ],
                "mutations": [
                    {
                        "op": "create",
                        "object_type": "ProductionOutput",
                        "id_param": "output_id",
                        "set_fields": {
                            "output_id": {"ref": "param.output_id"},
                            "work_order": {"ref": "param.work_order_id"},
                            "output_resource": {"ref": "param.output_resource"},
                            "good_qty": {"ref": "param.good_qty"},
                            "scrap_qty": {"ref": "param.scrap_qty"},
                            "absorbed_cost_cents": {"ref": "param.absorbed_cost_cents"},
                            "output_event": {
                                "op": "if_else",
                                "cond": {"op": "eq", "left": {"ref": "param.output_event"}, "right": {"lit": ""}},
                                "then": {"lit": None},
                                "else": {"ref": "param.output_event"},
                            },
                            "status": {"lit": "recorded"},
                        },
                    },
                    {
                        "op": "modify",
                        "object_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "set_fields": {
                            "completed_good_qty": cum_good_expr,
                            "completed_scrap_qty": cum_scrap_expr,
                        },
                    },
                ],
            },
            {
                "name": "close_work_order",
                "display_label": "关闭或取消工单",
                "description": "完工关闭工单（须已领料且有良品产出）或取消未领料工单（已领料工单禁直接取消）。",
                "allowed_roles": ["production_planner", "plant_manager", "shopfloor", "ops", "steward", "ai_agent", "admin"],
                "params": [
                    {"name": "work_order_id", "field_type": "str", "required": True},
                    {"name": "target_status", "field_type": "str", "default": "completed"},
                ],
                "preconditions": [
                    {
                        "name": "p2m_close_valid_target",
                        "code": "INVALID_WORK_ORDER_CLOSE_TARGET",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "param.target_status"},
                            "right": {"lit": ["completed", "cancelled"]},
                        },
                    },
                    {
                        "name": "p2m_cancel_forbids_issued_wo",
                        "code": "CANNOT_CANCEL_ISSUED_WORK_ORDER",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {"op": "eq", "left": {"ref": "param.target_status"}, "right": {"lit": "cancelled"}},
                        "must_hold": {"op": "le", "left": {"ref": "obj.issued_qty"}, "right": {"lit": 0}},
                    },
                    {
                        "name": "p2m_cancel_requires_draft_or_released",
                        "code": "WORK_ORDER_NOT_CANCELLABLE",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {"op": "eq", "left": {"ref": "param.target_status"}, "right": {"lit": "cancelled"}},
                        "must_hold": {
                            "op": "in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["draft", "released"]},
                        },
                    },
                    {
                        "name": "p2m_complete_wo_not_terminal",
                        "code": "WORK_ORDER_NOT_IN_PROGRESS",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {"op": "eq", "left": {"ref": "param.target_status"}, "right": {"lit": "completed"}},
                        "must_hold": {
                            "op": "not_in",
                            "left": {"ref": "obj.status"},
                            "right": {"lit": ["completed", "cancelled"]},
                        },
                    },
                    {
                        "name": "p2m_complete_requires_material_issue",
                        "code": "CANNOT_COMPLETE_WITHOUT_MATERIAL_ISSUE",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {"op": "eq", "left": {"ref": "param.target_status"}, "right": {"lit": "completed"}},
                        "must_hold": {"op": "gt", "left": {"ref": "obj.issued_qty"}, "right": {"lit": 0}},
                    },
                    {
                        "name": "p2m_complete_requires_good_output",
                        "code": "WORK_ORDER_NO_GOOD_OUTPUT",
                        "target_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "when": {"op": "eq", "left": {"ref": "param.target_status"}, "right": {"lit": "completed"}},
                        "must_hold": {"op": "gt", "left": {"ref": "obj.completed_good_qty"}, "right": {"lit": 0}},
                    },
                ],
                "mutations": [
                    {
                        "op": "modify",
                        "object_type": "WorkOrder",
                        "id_param": "work_order_id",
                        "set_fields": {"status": {"ref": "param.target_status"}},
                    }
                ],
            },
        ],
        "display": {
            "ontologies": {
                "std_p2m_ontology": "标准 P2M 领域本体（物料清单·生产工单·领料耗用·报工产出）",
            },
            "objects": {
                "BomSpec": "物料清单规格",
                "WorkOrder": "生产工单",
                "MaterialConsumption": "生产领料耗用",
                "ProductionOutput": "生产报工产出",
            },
            "actions": {
                "release_work_order": "下达生产工单",
                "issue_material_to_work_order": "工单领料投料",
                "record_production_output": "生产报工产出",
                "close_work_order": "关闭或取消工单",
            },
        },
    }
    composed = compose_blueprints(
        [build_std_core_blueprint(), p2m_fragment],
        model_id="std_p2m_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="标准 P2M 领域本体蓝图：在核心底座上组合 BOM 规格、生产工单、领料耗用与报工产出。",
    )
    composed.imports = ["std_core_ontology"]
    return composed
