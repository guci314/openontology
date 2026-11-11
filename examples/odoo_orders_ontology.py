"""双源订单统一运营场景的纯业务本体模型（PIM）。

本模块只描述业务对象、关系、只读推演函数和受治理动作。
不包含任何传输协议、外部系统客户端或同步代码。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from open_ontology import Actor, InvariantError, LinkType, ObjectType, OntologySpec

# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class Customer(ObjectType):
    """客户主数据（source-backed）。"""

    __primary_key__ = "customer_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)

    customer_id: str
    name: str
    region: str  # "north" | "south"


class Order(ObjectType):
    """订单（混合主权：业务字段来自源系统，指派人与运营备注归 Ontology）。"""

    __primary_key__ = "order_id"
    __title__ = "order_id"
    __security_tag_fields__ = ("region",)
    __owned__ = {"assignee": None, "ops_note": None}

    order_id: str
    customer_id: str
    region: str  # "north" | "south"
    status: str = "pending"  # "pending" | "shipped" | "cancelled"
    total_cents: int
    assignee: str | None = None
    ops_note: str | None = None


class OrderNote(ObjectType):
    """运营备注单（完全由 Ontology 自有）。"""

    __primary_key__ = "note_id"
    __title__ = "body"
    __security_tag_fields__ = ("region",)
    __owned__ = True

    note_id: str
    order_id: str
    author: str
    body: str
    region: str = "north"


# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------
CUSTOMER_ORDERS = LinkType(
    name="customer_orders",
    source="Customer",
    target="Order",
    cardinality="one-to-many",
    owned=False,
)
ORDER_NOTES = LinkType(
    name="order_notes",
    source="Order",
    target="OrderNote",
    cardinality="one-to-many",
    owned=True,
)


# ---------------------------------------------------------------------------
# Spec registration
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (Customer, Order, OrderNote):
    spec.object_type(_cls)
for _link in (CUSTOMER_ORDERS, ORDER_NOTES):
    spec.add_link_type(_link)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class CustomerSummaryParams(BaseModel):
    customer_id: str | None = None


class CancelOrderParams(BaseModel):
    order_id: str
    reason: str
    order_total_cents: int = 0
    large_order: bool = False


class AssignOrderParams(BaseModel):
    order_id: str
    assignee: str | None
    ops_note: str | None = None


class AddOrderNoteParams(BaseModel):
    note_id: str
    order_id: str
    body: str


# ---------------------------------------------------------------------------
# Read-only function
# ---------------------------------------------------------------------------
@spec.function(
    "customer_unshipped_summary",
    CustomerSummaryParams,
    description="统计指定客户（或全量）在当前视角下的未发货订单数与总金额。",
)
def customer_unshipped_summary(
    ctx: Any, params: CustomerSummaryParams
) -> dict[str, Any]:
    orders = ctx.query(Order)
    if params.customer_id is not None:
        orders = [order for order in orders if order.customer_id == params.customer_id]

    pending = [order for order in orders if order.status == "pending"]
    return {
        "customer_id": params.customer_id,
        "unshipped_count": len(pending),
        "unshipped_total_cents": sum(order.total_cents for order in pending),
        "order_ids": [order.order_id for order in pending],
    }


# ---------------------------------------------------------------------------
# Governed actions
# ---------------------------------------------------------------------------
def _cancel_needs_approval(params: CancelOrderParams, actor: Actor) -> bool:
    """AI agent cancelling a large order always needs a human manager."""
    return actor.role == "ai_agent" and (
        params.large_order or params.order_total_cents >= 50_000_00
    )


@spec.action(
    "cancel_order",
    CancelOrderParams,
    allowed_roles=("dispatcher", "ai_agent", "ops_manager"),
    approver_roles=("ops_manager",),
    approval_rule=_cancel_needs_approval,
    affects_source=True,
    description="取消一张尚未发货的订单（会写回源业务系统）。",
)
def cancel_order(ctx: Any, params: CancelOrderParams) -> None:
    order = ctx.require(Order, params.order_id)

    if order.status == "shipped":
        raise InvariantError(
            f"order {order.order_id} has already shipped and cannot be cancelled",
            code="SHIPPED_ORDER_CANNOT_BE_CANCELLED",
        )
    if order.status == "cancelled":
        raise InvariantError(
            f"order {order.order_id} is already cancelled",
            code="ORDER_ALREADY_CANCELLED",
        )
    if len(params.reason.strip()) == 0:
        raise InvariantError(
            "a non-empty cancellation reason is required",
            code="EMPTY_REASON",
        )

    ctx.modify(order, status="cancelled")


@spec.action(
    "assign_order",
    AssignOrderParams,
    allowed_roles=("dispatcher", "ai_agent", "ops_manager"),
    affects_source=False,
    description="只修改订单的 Ontology 自有字段：指派人与运营备注。",
)
def assign_order(ctx: Any, params: AssignOrderParams) -> None:
    order = ctx.require(Order, params.order_id)
    ctx.modify(order, assignee=params.assignee, ops_note=params.ops_note)


@spec.action(
    "add_order_note",
    AddOrderNoteParams,
    allowed_roles=("dispatcher", "ai_agent", "ops_manager"),
    affects_source=False,
    description="创建纯 Ontology 自有的运营备注单并挂到订单上。",
)
def add_order_note(ctx: Any, params: AddOrderNoteParams) -> None:
    order = ctx.require(Order, params.order_id)
    note = OrderNote(
        note_id=params.note_id,
        order_id=order.order_id,
        author=ctx.actor.actor_id,
        body=params.body,
        region=order.region,
    )
    ctx.create(note)
    ctx.link(order, ORDER_NOTES, note)
