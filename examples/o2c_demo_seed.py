"""O2C 演示世界种子装配器（从纯业务 M1 本体 o2c_ontology 剥离出的运行时构建函数）。"""

from __future__ import annotations

from dataclasses import dataclass

from open_ontology import OntologyRuntime

from .o2c_ontology import (
    Customer,
    ReceivableBalance,
    SalesOrder,
    ar_ledger_id,
    spec,
)

__all__ = ["O2CDemo", "build_o2c_demo"]


@dataclass
class O2CDemo:
    runtime: OntologyRuntime
    customer_id: str = "O2C-CUST-001"
    small_order_id: str = "O2C-SO-001"
    large_order_id: str = "O2C-SO-002"


def build_o2c_demo() -> O2CDemo:
    runtime = OntologyRuntime(spec)
    customer = Customer(
        customer_id="O2C-CUST-001",
        name="O2C Demo Customer",
        credit_limit_cents=10_000_000,
    )
    runtime.put_object(customer)
    runtime.put_object(
        ReceivableBalance(
            ledger_id=ar_ledger_id(customer.customer_id),
            customer=customer.customer_id,
            ar_balance_cents=0,
        )
    )
    for order in (
        SalesOrder(
            order_id="O2C-SO-001",
            customer=customer.customer_id,
            product_summary="O2C Widget",
            quantity=2,
            unit_price_cents=1_000_000,
            total_cents=2_000_000,
            status="draft",
        ),
        SalesOrder(
            order_id="O2C-SO-002",
            customer=customer.customer_id,
            product_summary="O2C Widget",
            quantity=12,
            unit_price_cents=1_000_000,
            total_cents=12_000_000,
            status="draft",
        ),
    ):
        runtime.put_object(order)
    return O2CDemo(runtime=runtime)
