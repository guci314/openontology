"""真实 Odoo 19 O2C 适配器与全流程演示（PSM）。

上层 O2C 本体只产生业务决议（action_runs）；本适配器读取已 COMMITTED 的
业务运行，翻译为真实 Odoo 的确认订单、出库、开票过账与收款核销连招。
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

from open_ontology import InvariantError, OntologyRuntime

from .odoo_dual_erp_demo import (
    DB_NORTH,
    OdooJSONRPC,
    OdooRPCError,
    ensure_database,
)
from . import o2c_ontology as o2c

PARTNER_REF = "O2C-CUST-001"
PARTNER_NAME = "O2C Demo Customer"
PRODUCT_CODE = "O2C-WIDGET"
PRODUCT_NAME = "O2C Widget"
CREDIT_LIMIT_CENTS = 10_000_000

ORDER_SEEDS = (
    ("O2C-SO-001", 2, 10_000.0),
    ("O2C-SO-002", 12, 10_000.0),
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class O2COrderRef:
    canonical_id: str
    odoo_id: int
    odoo_name: str
    partner_id: int
    product_id: int
    quantity: int
    unit_price: float
    total_cents: int
    run_tag: str


@dataclass
class O2CData:
    client: OdooJSONRPC
    partner_id: int
    product_id: int
    run_tag: str
    credit_limit_cents: int = CREDIT_LIMIT_CENTS
    orders: dict[str, O2COrderRef] = field(default_factory=dict)
    invoices: dict[str, int] = field(default_factory=dict)
    payments: dict[str, int] = field(default_factory=dict)

    def order(self, canonical_id: str) -> O2COrderRef:
        return self.orders[canonical_id]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class OdooO2CAdapter:
    """Odoo 19 sales -> delivery -> invoice -> payment state-machine bridge."""

    def __init__(
        self,
        runtime: OntologyRuntime,
        client: OdooJSONRPC | None = None,
    ) -> None:
        self.runtime = runtime
        if client is None:
            client, _ = ensure_database(DB_NORTH)
        self.client = client
        self.data: O2CData | None = None
        self._seen: set[str] = {
            run["run_id"]
            for run in runtime.list_action_runs()
            if run["status"] != "PENDING_APPROVAL"
        }

    # -- seeding -----------------------------------------------------------
    def _get_or_create_partner(self) -> int:
        ids = self.client.search(
            "res.partner", [("ref", "=", PARTNER_REF)]
        )
        values = {
            "name": PARTNER_NAME,
            "ref": PARTNER_REF,
            "customer_rank": 1,
            "credit_limit": CREDIT_LIMIT_CENTS / 100.0,
            "active": True,
        }
        if ids:
            self.client.write("res.partner", [ids[0]], values)
            return ids[0]
        return self.client.create("res.partner", values)

    def _get_or_create_product(self) -> int:
        ids = self.client.search(
            "product.product", [("default_code", "=", PRODUCT_CODE)]
        )
        values = {
            "name": PRODUCT_NAME,
            "default_code": PRODUCT_CODE,
            "type": "consu",
            "is_storable": True,
            "list_price": 10_000.0,
            "sale_ok": True,
            "active": True,
            "taxes_id": [(6, 0, [])],
        }
        if ids:
            self.client.write("product.product", [ids[0]], values)
            return ids[0]
        return self.client.create("product.product", values)

    def _archive_old_orders(self) -> None:
        """Free the canonical client_order_ref on previous demo runs.

        Old orders/invoices are kept for audit, but search by canonical
        reference will find exactly the current run's real documents.
        """
        canonical_refs = [seed[0] for seed in ORDER_SEEDS]
        rows = self.client.search_read(
            "sale.order",
            [("client_order_ref", "in", canonical_refs)],
            fields=["id", "client_order_ref"],
        )
        for row in rows:
            self.client.write(
                "sale.order",
                [int(row["id"])],
                {
                    "client_order_ref": (
                        f"__stale__{row['id']}__{row['client_order_ref']}"
                    )
                },
            )

    def seed_odoo(self) -> O2CData:
        """Create a fresh pair of real draft quotations for this run."""
        partner_id = self._get_or_create_partner()
        product_id = self._get_or_create_product()
        self._archive_old_orders()
        run_tag = f"O2C-RUN-{uuid.uuid4().hex[:8]}"
        data = O2CData(
            client=self.client,
            partner_id=partner_id,
            product_id=product_id,
            run_tag=run_tag,
        )

        for canonical_id, quantity, unit_price in ORDER_SEEDS:
            odoo_id = self.client.create(
                "sale.order",
                {
                    "partner_id": partner_id,
                    "client_order_ref": canonical_id,
                    "note": run_tag,
                    "order_line": [
                        (
                            0,
                            0,
                            {
                                "product_id": product_id,
                                "product_uom_qty": quantity,
                                "price_unit": unit_price,
                            },
                        )
                    ],
                },
            )
            row = self.client.read(
                "sale.order",
                [odoo_id],
                [
                    "id",
                    "name",
                    "client_order_ref",
                    "state",
                    "amount_total",
                    "partner_id",
                ],
            )[0]
            if row["state"] != "draft":
                raise OdooRPCError(
                    f"new quotation {canonical_id} is {row['state']!r}"
                )
            data.orders[canonical_id] = O2COrderRef(
                canonical_id=canonical_id,
                odoo_id=odoo_id,
                odoo_name=row["name"],
                partner_id=partner_id,
                product_id=product_id,
                quantity=quantity,
                unit_price=unit_price,
                total_cents=int(round(float(row["amount_total"]) * 100)),
                run_tag=run_tag,
            )
        self.data = data
        return data

    # -- Odoo document helpers --------------------------------------------
    def order_row(self, canonical_id: str) -> dict[str, Any]:
        ref = self._ref(canonical_id)
        return self.client.read(
            "sale.order",
            [ref.odoo_id],
            [
                "id",
                "name",
                "client_order_ref",
                "state",
                "amount_total",
                "delivery_status",
                "partner_id",
                "order_line",
            ],
        )[0]

    def delivery_row(self, canonical_id: str) -> dict[str, Any] | None:
        ref = self._ref(canonical_id)
        rows = self.client.search_read(
            "stock.picking",
            [("sale_id", "=", ref.odoo_id)],
            fields=["id", "name", "state", "origin"],
        )
        if not rows:
            return None
        rows.sort(key=lambda row: int(row["id"]), reverse=True)
        return rows[0]

    def invoice_row(self, canonical_id: str) -> dict[str, Any] | None:
        ref = self._ref(canonical_id)
        rows = self.client.search_read(
            "account.move",
            [
                ("move_type", "=", "out_invoice"),
                ("invoice_origin", "=", ref.odoo_name),
            ],
            fields=[
                "id",
                "name",
                "state",
                "payment_state",
                "amount_total",
                "amount_residual",
                "invoice_origin",
            ],
        )
        if not rows:
            return None
        rows.sort(key=lambda row: int(row["id"]), reverse=True)
        return rows[0]

    def _ref(self, canonical_id: str) -> O2COrderRef:
        if self.data is None:
            raise RuntimeError("seed_odoo() must be called first")
        if canonical_id not in self.data.orders:
            raise RuntimeError(f"unknown O2C order {canonical_id!r}")
        return self.data.orders[canonical_id]

    # -- upward snapshot ---------------------------------------------------
    def sync_from_odoo(self) -> dict[str, Any]:
        if self.data is None:
            raise RuntimeError("seed_odoo() must be called first")
        data = self.data

        partner = self.client.read(
            "res.partner",
            [data.partner_id],
            ["id", "name", "ref", "credit_limit"],
        )[0]

        order_rows: list[dict[str, Any]] = []
        sales_orders: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any]] = []
        invoices: list[dict[str, Any]] = []
        links: dict[str, list[tuple[str, str]]] = {
            "customer_sales_orders": [],
            "order_deliveries": [],
            "order_invoices": [],
        }
        invoice_residual_cents = 0

        for canonical_id in sorted(data.orders):
            ref = data.order(canonical_id)
            order = self.order_row(canonical_id)
            delivery = self.delivery_row(canonical_id)
            invoice = self.invoice_row(canonical_id)
            delivery_state = delivery["state"] if delivery else None
            order_status = _map_order_status(
                order["state"], delivery_state, invoice
            )
            order_rows.append(order)

            sales_orders.append(
                o2c.SalesOrder(
                    order_id=canonical_id,
                    customer_id=PARTNER_REF,
                    product_summary=PRODUCT_NAME,
                    quantity=ref.quantity,
                    unit_price_cents=int(round(ref.unit_price * 100)),
                    total_cents=int(round(float(order["amount_total"]) * 100)),
                    status=order_status,
                ).source_properties()
            )
            links["customer_sales_orders"].append(
                (PARTNER_REF, canonical_id)
            )

            if delivery is not None:
                deliveries.append(
                    o2c.Delivery(
                        delivery_id=f"DEL-{canonical_id}",
                        order_id=canonical_id,
                        status=(
                            "shipped"
                            if delivery["state"] == "done"
                            else "pending"
                        ),
                    ).source_properties()
                )
                links["order_deliveries"].append(
                    (canonical_id, f"DEL-{canonical_id}")
                )

            if invoice is not None and invoice["state"] == "posted":
                paid = (
                    invoice["payment_state"] in ("paid", "in_payment")
                    or float(invoice["amount_residual"]) == 0.0
                )
                invoices.append(
                    o2c.Invoice(
                        invoice_id=f"INV-{canonical_id}",
                        order_id=canonical_id,
                        customer_id=PARTNER_REF,
                        amount_cents=int(
                            round(float(invoice["amount_total"]) * 100)
                        ),
                        status="paid" if paid else "posted",
                    ).to_properties()
                )
                links["order_invoices"].append(
                    (canonical_id, f"INV-{canonical_id}")
                )
                invoice_residual_cents += int(
                    round(float(invoice["amount_residual"]) * 100)
                )
                data.invoices[canonical_id] = int(invoice["id"])

        customer = o2c.Customer(
            customer_id=PARTNER_REF,
            name=partner["name"],
            credit_limit_cents=int(
                round(float(partner.get("credit_limit") or 0.0) * 100)
            ) or data.credit_limit_cents,
            ar_balance_cents=invoice_residual_cents,
        )
        snapshot = self.runtime.load_snapshot(
            {
                "Customer": [customer.source_properties()],
                "SalesOrder": sales_orders,
                "Delivery": deliveries,
                "Invoice": invoices,
            },
            links=links,
        )
        return {
            "customers": 1,
            "orders": len(sales_orders),
            "deliveries": len(deliveries),
            "invoices": len(invoices),
            "snapshot": snapshot,
            "orders": {
                order["client_order_ref"]: {
                    "odoo_id": order["id"],
                    "odoo_name": order["name"],
                    "state": order["state"],
                }
                for order in order_rows
            },
        }

    # -- downward writeback ------------------------------------------------
    def process_new_runs(self) -> list[dict[str, Any]]:
        phase = {
            "confirm_sales_order": 0,
            "ship_order_delivery": 1,
            "issue_and_post_invoice": 2,
            "record_customer_payment": 3,
        }
        candidates: list[tuple[int, dict[str, Any]]] = []

        for run in self.runtime.list_action_runs():
            run_id = run["run_id"]
            if run_id in self._seen:
                continue
            if run["status"] == "PENDING_APPROVAL":
                continue
            if run["status"] != "COMMITTED":
                self._seen.add(run_id)
                continue

            action = run["action_type"]
            params = run.get("params") or {}
            order_id = params.get("order_id")
            if order_id not in (self.data.orders if self.data else {}):
                self._seen.add(run_id)
                continue

            expected = {
                "confirm_sales_order": "confirmed",
                "ship_order_delivery": "shipped",
                "issue_and_post_invoice": "invoiced",
                "record_customer_payment": "paid",
            }.get(action)
            target_status = _target_status(run)
            if expected is None or target_status != expected:
                # Revert runs (or unrelated edits) are not forward O2C steps.
                self._seen.add(run_id)
                continue

            candidates.append((phase[action], run))

        candidates.sort(
            key=lambda item: (
                item[0],
                item[1].get("executed_at", ""),
                item[1]["run_id"],
            )
        )

        evidence: list[dict[str, Any]] = []
        for _, run in candidates:
            run_id = run["run_id"]
            action = run["action_type"]
            order_id = (run.get("params") or {}).get("order_id")

            if action == "confirm_sales_order":
                item = self._apply_confirm(run_id, order_id)
            elif action == "ship_order_delivery":
                item = self._apply_ship(run_id, order_id)
            elif action == "issue_and_post_invoice":
                item = self._apply_invoice(run_id, order_id)
            else:
                item = self._apply_payment(run_id, order_id)

            self._seen.add(run_id)
            evidence.append(item)
        return evidence

    def _apply_confirm(self, run_id: str, order_id: str) -> dict[str, Any]:
        ref = self._ref(order_id)
        row = self.order_row(order_id)
        if row["state"] in ("draft", "sent"):
            self.client.execute_kw(
                "sale.order", "action_confirm", [[ref.odoo_id]]
            )
        row = self.order_row(order_id)
        if row["state"] != "sale":
            raise OdooRPCError(
                f"confirm failed for {order_id}: state={row['state']!r}"
            )
        picking = self.delivery_row(order_id)
        if picking is None:
            raise OdooRPCError(
                f"confirm {order_id} produced no stock.picking"
            )
        return {
            "run_id": run_id,
            "action": "confirm_sales_order",
            "order_id": order_id,
            "odoo_id": ref.odoo_id,
            "odoo_name": ref.odoo_name,
            "sale_state": row["state"],
            "picking_id": picking["id"],
            "picking_name": picking["name"],
        }

    def _apply_ship(self, run_id: str, order_id: str) -> dict[str, Any]:
        ref = self._ref(order_id)
        picking = self.delivery_row(order_id)
        if picking is None:
            raise OdooRPCError(f"no stock.picking for {order_id}")
        picking_id = int(picking["id"])

        if picking["state"] != "done":
            self.client.execute_kw(
                "stock.picking", "action_assign", [[picking_id]]
            )
            moves = self.client.search_read(
                "stock.move",
                [("picking_id", "=", picking_id)],
                fields=["id", "product_uom_qty", "quantity", "picked"],
            )
            for move in moves:
                self.client.write(
                    "stock.move",
                    [int(move["id"])],
                    {
                        "quantity": move["product_uom_qty"],
                        "picked": True,
                    },
                )
            result = self.client.execute_kw(
                "stock.picking", "button_validate", [[picking_id]]
            )
            self._handle_validate_result(result)
            picking = self.delivery_row(order_id)

        if picking is None or picking["state"] != "done":
            raise OdooRPCError(
                f"delivery validation failed for {order_id}: "
                f"state={picking['state'] if picking else None!r}"
            )
        return {
            "run_id": run_id,
            "action": "ship_order_delivery",
            "order_id": order_id,
            "odoo_id": ref.odoo_id,
            "picking_id": picking_id,
            "picking_name": picking["name"],
            "picking_state": picking["state"],
        }

    def _apply_invoice(self, run_id: str, order_id: str) -> dict[str, Any]:
        ref = self._ref(order_id)
        invoice = self.invoice_row(order_id)
        if invoice is None:
            lines = self.client.search_read(
                "sale.order.line",
                [("order_id", "=", ref.odoo_id)],
                fields=[
                    "id",
                    "product_id",
                    "product_uom_qty",
                    "price_unit",
                ],
            )
            if not lines:
                raise OdooRPCError(f"{order_id} has no sale.order.line")
            line = lines[0]
            invoice_id = self.client.create(
                "account.move",
                {
                    "move_type": "out_invoice",
                    "partner_id": ref.partner_id,
                    "invoice_date": datetime.date.today().isoformat(),
                    "invoice_origin": ref.odoo_name,
                    "ref": f"{order_id}/{ref.run_tag}",
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "product_id": line["product_id"][0],
                                "quantity": line["product_uom_qty"],
                                "price_unit": line["price_unit"],
                                "tax_ids": [(6, 0, [])],
                                "sale_line_ids": [(6, 0, [line["id"]])],
                            },
                        )
                    ],
                },
            )
            self.client.execute_kw(
                "account.move", "action_post", [[invoice_id]]
            )
            invoice = self.invoice_row(order_id)
            if invoice is None:
                raise OdooRPCError(f"invoice creation failed for {order_id}")
            self.data.invoices[order_id] = int(invoice["id"])
        elif invoice["state"] == "draft":
            self.client.execute_kw(
                "account.move", "action_post", [[int(invoice["id"])]]
            )
            invoice = self.invoice_row(order_id)

        if invoice is None or invoice["state"] != "posted":
            raise OdooRPCError(
                f"invoice posting failed for {order_id}: "
                f"state={invoice['state'] if invoice else None!r}"
            )
        self.data.invoices[order_id] = int(invoice["id"])
        return {
            "run_id": run_id,
            "action": "issue_and_post_invoice",
            "order_id": order_id,
            "odoo_id": ref.odoo_id,
            "invoice_id": int(invoice["id"]),
            "invoice_name": invoice["name"],
            "invoice_state": invoice["state"],
            "amount_total": float(invoice["amount_total"]),
        }

    def _apply_payment(self, run_id: str, order_id: str) -> dict[str, Any]:
        invoice = self.invoice_row(order_id)
        if invoice is None:
            raise OdooRPCError(f"no invoice for {order_id}")
        invoice_id = int(invoice["id"])

        payment_id = None
        if float(invoice["amount_residual"]) > 0.0:
            context = {
                "active_model": "account.move",
                "active_ids": [invoice_id],
            }
            wizard_id = self.client.execute_kw(
                "account.payment.register",
                "create",
                [{"payment_date": datetime.date.today().isoformat()}],
                {"context": context},
            )
            result = self.client.execute_kw(
                "account.payment.register",
                "action_create_payments",
                [[wizard_id]],
                {"context": context},
            )
            if isinstance(result, dict):
                payment_id = result.get("res_id")

        invoice = self.invoice_row(order_id)
        if invoice is None:
            raise OdooRPCError(f"invoice vanished for {order_id}")
        residual = float(invoice["amount_residual"])
        if residual != 0.0:
            raise OdooRPCError(
                f"payment failed for {order_id}: residual={residual}"
            )
        if invoice["payment_state"] not in ("paid", "in_payment"):
            raise OdooRPCError(
                f"payment state for {order_id} is {invoice['payment_state']!r}"
            )
        self.data.payments[order_id] = int(
            payment_id or 0
        )
        return {
            "run_id": run_id,
            "action": "record_customer_payment",
            "order_id": order_id,
            "invoice_id": invoice_id,
            "invoice_name": invoice["name"],
            "payment_id": payment_id,
            "payment_state": invoice["payment_state"],
            "amount_residual": residual,
        }

    # -- helpers -----------------------------------------------------------
    def _handle_validate_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        model = result.get("res_model")
        res_id = result.get("res_id")
        if not model or not res_id:
            return
        if model in (
            "stock.immediate.transfer",
            "stock.backorder.confirmation",
        ):
            self.client.execute_kw(model, "process", [[res_id]])

    def odoo_documents(self, canonical_id: str) -> dict[str, Any]:
        order = self.order_row(canonical_id)
        delivery = self.delivery_row(canonical_id)
        invoice = self.invoice_row(canonical_id)
        return {
            "order_id": order["id"],
            "order_name": order["name"],
            "order_state": order["state"],
            "delivery_id": delivery["id"] if delivery else None,
            "delivery_name": delivery["name"] if delivery else None,
            "delivery_state": delivery["state"] if delivery else None,
            "invoice_id": invoice["id"] if invoice else None,
            "invoice_name": invoice["name"] if invoice else None,
            "invoice_state": invoice["state"] if invoice else None,
            "payment_state": invoice["payment_state"] if invoice else None,
            "amount_residual": (
                float(invoice["amount_residual"]) if invoice else None
            ),
        }


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------
def _map_order_status(
    sale_state: str,
    delivery_state: str | None,
    invoice: dict[str, Any] | None,
) -> str:
    if sale_state == "cancel":
        return "cancelled"
    if sale_state in ("draft", "sent"):
        return "draft"
    if invoice is not None and invoice["state"] == "posted":
        if (
            invoice["payment_state"] in ("paid", "in_payment")
            or float(invoice["amount_residual"]) == 0.0
        ):
            return "paid"
        return "invoiced"
    if delivery_state == "done":
        return "shipped"
    return "confirmed"


def _target_status(run: dict[str, Any]) -> str | None:
    for change in run.get("object_diff", {}).get("object_changes", []):
        if (
            change["object_type"] == "SalesOrder"
            and change["kind"] == "update"
        ):
            return (change.get("after") or {}).get("status")
    return None


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> dict[str, Any]:
    runtime = OntologyRuntime(o2c.spec)
    adapter = OdooO2CAdapter(runtime)
    data = adapter.seed_odoo()
    adapter.sync_from_odoo()

    _section("1. 真实 Odoo 19 草稿报价单与信用主数据")
    for canonical_id in sorted(data.orders):
        ref = data.order(canonical_id)
        docs = adapter.odoo_documents(canonical_id)
        print(
            f"  {canonical_id} odoo_id={docs['order_id']}"
            f" odoo_name={docs['order_name']} state={docs['order_state']}"
            f" total={ref.total_cents / 100:.2f} 元"
        )
    customer = runtime.get_object(
        o2c.Customer, PARTNER_REF, o2c.FINANCE_DIRECTOR
    )
    print(
        f"  customer credit_limit={customer.credit_limit_cents / 100:.2f} 元"
        f" ar_balance={customer.ar_balance_cents / 100:.2f} 元"
    )

    _section("2. 初始 O2C 经营体检")
    view = runtime.call_function(
        "diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR
    )
    print(f"  credit_exceeded_drafts={view['credit_exceeded_drafts']}")
    print(f"  totals={view['totals']}")

    _section("3. 确认小额订单 O2C-SO-001 -> 真实 sale.order.action_confirm")
    confirm_run = runtime.submit_action(
        "O2C-CONFIRM-001",
        "confirm_sales_order",
        {"order_id": "O2C-SO-001"},
        o2c.SALES_REP,
    )
    print(f"  ontology run={confirm_run['run_id']} status={confirm_run['status']}")
    for item in adapter.process_new_runs():
        print(f"  writeback {item}")
    docs = adapter.odoo_documents("O2C-SO-001")
    print(
        f"  Odoo sale.order={docs['order_name']} state={docs['order_state']}"
        f" picking={docs['delivery_name']} state={docs['delivery_state']}"
    )

    _section("4. 未发货提前开票硬拦截")
    try:
        runtime.submit_action(
            "O2C-INVOICE-EARLY",
            "issue_and_post_invoice",
            {"order_id": "O2C-SO-001"},
            o2c.FINANCE_DIRECTOR,
        )
    except InvariantError as exc:
        print(f"  blocked code={exc.code}")
    print(
        f"  REJECTED audit="
        f"{runtime.get_action_run('O2C-INVOICE-EARLY')['status']}"
    )
    print(f"  Odoo invoice={adapter.invoice_row('O2C-SO-001')}")

    _section("5. 仓库出库 -> 真实 stock.picking.button_validate")
    ship_run = runtime.submit_action(
        "O2C-SHIP-001",
        "ship_order_delivery",
        {"order_id": "O2C-SO-001"},
        o2c.WAREHOUSE,
    )
    print(f"  ontology run={ship_run['run_id']} status={ship_run['status']}")
    for item in adapter.process_new_runs():
        print(f"  writeback {item}")
    docs = adapter.odoo_documents("O2C-SO-001")
    print(
        f"  Odoo picking={docs['delivery_name']} state={docs['delivery_state']}"
    )

    _section("6. 开票过账 -> 真实 account.move.action_post")
    invoice_run = runtime.submit_action(
        "O2C-INVOICE-001",
        "issue_and_post_invoice",
        {"order_id": "O2C-SO-001"},
        o2c.FINANCE_DIRECTOR,
    )
    print(f"  ontology run={invoice_run['run_id']} status={invoice_run['status']}")
    for item in adapter.process_new_runs():
        print(f"  writeback {item}")
    docs = adapter.odoo_documents("O2C-SO-001")
    print(
        f"  Odoo account.move={docs['invoice_name']} state={docs['invoice_state']}"
        f" residual={docs['amount_residual']}"
    )

    _section("7. 收款核销 -> 真实 account.payment.register")
    payment_run = runtime.submit_action(
        "O2C-PAY-001",
        "record_customer_payment",
        {"order_id": "O2C-SO-001", "payment_cents": 2_000_000},
        o2c.FINANCE_DIRECTOR,
    )
    print(f"  ontology run={payment_run['run_id']} status={payment_run['status']}")
    for item in adapter.process_new_runs():
        print(f"  writeback {item}")
    docs = adapter.odoo_documents("O2C-SO-001")
    print(
        f"  Odoo payment_state={docs['payment_state']}"
        f" amount_residual={docs['amount_residual']}"
    )

    _section("8. AI 提交超信用额度大单 -> 财务总监审批 -> 真实 Odoo 确认")
    ai_run = runtime.submit_action(
        "O2C-CONFIRM-002",
        "confirm_sales_order",
        {"order_id": "O2C-SO-002"},
        o2c.AI_AGENT,
    )
    print(f"  AI submit status={ai_run['status']}")
    print(f"  Odoo while pending={adapter.order_row('O2C-SO-002')['state']}")
    approved = runtime.approve_action("O2C-CONFIRM-002", o2c.FINANCE_DIRECTOR)
    print(f"  approved by={approved['approved_by']} status={approved['status']}")
    for item in adapter.process_new_runs():
        print(f"  writeback {item}")
    print(f"  Odoo after approve={adapter.order_row('O2C-SO-002')['state']}")

    _section("9. 治理 overlay + Odoo 全量重刷存活")
    runtime.submit_action(
        "O2C-GOV-001",
        "annotate_o2c_governance",
        {
            "customer_id": PARTNER_REF,
            "risk_tier": "WATCHLIST",
            "credit_hold_note": "watch Q4 credit exposure",
            "order_id": "O2C-SO-001",
            "account_rep": "sales-rep-01",
            "fulfillment_note": "paid in full; keep packaging record",
        },
        o2c.SALES_REP,
    )
    adapter.sync_from_odoo()
    customer = runtime.get_object(
        o2c.Customer, PARTNER_REF, o2c.FINANCE_DIRECTOR
    )
    order = runtime.get_object(
        o2c.SalesOrder, "O2C-SO-001", o2c.FINANCE_DIRECTOR
    )
    print(
        f"  risk_tier={customer.risk_tier!r}"
        f" credit_hold_note={customer.credit_hold_note!r}"
    )
    print(
        f"  account_rep={order.account_rep!r}"
        f" fulfillment_note={order.fulfillment_note!r}"
    )

    _section("10. 最终 O2C 经营体检与真实单据状态")
    final_view = runtime.call_function(
        "diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR
    )
    print(f"  totals={final_view['totals']}")
    for canonical_id in sorted(data.orders):
        docs = adapter.odoo_documents(canonical_id)
        print(f"  {canonical_id}: {docs}")

    summary = {
        "orders": {
            canonical_id: adapter.odoo_documents(canonical_id)
            for canonical_id in sorted(data.orders)
        },
        "small_order_status": runtime.get_object(
            o2c.SalesOrder, "O2C-SO-001", o2c.FINANCE_DIRECTOR
        ).status,
        "large_order_status": runtime.get_object(
            o2c.SalesOrder, "O2C-SO-002", o2c.FINANCE_DIRECTOR
        ).status,
        "ar_balance_cents": runtime.get_object(
            o2c.Customer, PARTNER_REF, o2c.FINANCE_DIRECTOR
        ).ar_balance_cents,
        "overlay": {
            "risk_tier": customer.risk_tier,
            "credit_hold_note": customer.credit_hold_note,
            "account_rep": order.account_rep,
            "fulfillment_note": order.fulfillment_note,
        },
        "rejected_runs": [
            run["run_id"] for run in runtime.list_action_runs("REJECTED")
        ],
    }
    _section("DONE")
    print("  summary:", summary)
    runtime.close()
    return summary


if __name__ == "__main__":
    main()
