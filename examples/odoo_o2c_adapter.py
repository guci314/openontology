"""真实 Odoo 19 O2C 适配器与全流程演示（PSM）。

上层 O2C 本体只产生业务决议（action_runs）；本适配器读取已 COMMITTED 的
业务运行，翻译为真实 Odoo 的确认订单、出库、开票过账与收款核销连招。
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from open_ontology import Actor, InvariantError, OntologyRuntime

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
    product_summary: str = PRODUCT_NAME


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
        *,
        partner_ref: str = PARTNER_REF,
        partner_name: str = PARTNER_NAME,
        order_seeds: Sequence[tuple[str, int, float]] = ORDER_SEEDS,
        credit_limit_cents: int = CREDIT_LIMIT_CENTS,
    ) -> None:
        self.runtime = runtime
        if client is None:
            client, _ = ensure_database(os.environ.get("ODOO_O2C_DB", DB_NORTH))
        self.client = client
        self.partner_ref = partner_ref
        self.partner_name = partner_name
        self.order_seeds = tuple(
            (str(cid), int(qty), float(price)) for cid, qty, price in order_seeds
        )
        self.credit_limit_cents = int(credit_limit_cents)
        self.data: O2CData | None = None
        self._seen: set[str] = {
            run["run_id"]
            for run in runtime.list_action_runs()
            if run["status"] != "PENDING_APPROVAL"
        }

    # -- seeding -----------------------------------------------------------
    def _get_or_create_partner(self) -> int:
        ids = self.client.search(
            "res.partner", [("ref", "=", self.partner_ref)]
        )
        values = {
            "name": self.partner_name,
            "ref": self.partner_ref,
            "customer_rank": 1,
            "credit_limit": self.credit_limit_cents / 100.0,
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

    def _archive_old_orders(self, partner_id: int | None = None) -> None:
        """Free the canonical client_order_ref on previous demo runs.

        Old orders/invoices are kept for audit, but search by canonical
        reference will find exactly the current run's real documents.
        """
        canonical_refs = [seed[0] for seed in self.order_seeds]
        if partner_id is not None and self.partner_ref != PARTNER_REF:
            domain: list[Any] = [
                ("partner_id", "=", partner_id),
                ("client_order_ref", "!=", False),
            ]
        elif partner_id is not None:
            domain = [
                "|",
                ("client_order_ref", "in", canonical_refs),
                "&",
                ("partner_id", "=", partner_id),
                ("client_order_ref", "!=", False),
            ]
        else:
            domain = [("client_order_ref", "in", canonical_refs)]
        rows = self.client.search_read(
            "sale.order",
            domain,
            fields=["id", "client_order_ref"],
        )
        for row in rows:
            ref_val = str(row.get("client_order_ref") or "")
            if not ref_val or ref_val.startswith("__stale__"):
                continue
            self.client.write(
                "sale.order",
                [int(row["id"])],
                {
                    "client_order_ref": f"__stale__{row['id']}__{ref_val}"
                },
            )

    def seed_odoo(self, *, empty_orders: bool = False) -> O2CData:
        """Create a fresh pair of real draft quotations (or empty order book)."""
        partner_id = self._get_or_create_partner()
        product_id = self._get_or_create_product()
        self._archive_old_orders(partner_id=partner_id)
        run_tag = f"O2C-RUN-{uuid.uuid4().hex[:8]}"
        data = O2CData(
            client=self.client,
            partner_id=partner_id,
            product_id=product_id,
            run_tag=run_tag,
            credit_limit_cents=self.credit_limit_cents,
        )

        if not empty_orders:
            for canonical_id, quantity, unit_price in self.order_seeds:
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
                "amount_untaxed",
                "amount_total",
                "delivery_status",
                "partner_id",
                "order_line",
            ],
        )[0]

    def delivery_rows(self, canonical_id: str) -> list[dict[str, Any]]:
        ref = self._ref(canonical_id)
        rows = self.client.search_read(
            "stock.picking",
            [("sale_id", "=", ref.odoo_id), ("state", "!=", "cancel")],
            fields=["id", "name", "state", "origin"],
        )
        rows.sort(key=lambda row: int(row["id"]))
        return rows

    def delivery_row(self, canonical_id: str) -> dict[str, Any] | None:
        rows = self.delivery_rows(canonical_id)
        if not rows:
            return None
        # Prefer open (not-done) picking if a backorder exists, else latest done picking
        open_rows = [r for r in rows if r["state"] != "done"]
        if open_rows:
            return open_rows[-1]
        return rows[-1]

    def shipped_qty_for_order(self, canonical_id: str) -> int:
        rows = self.delivery_rows(canonical_id)
        done_ids = [int(r["id"]) for r in rows if r["state"] == "done"]
        if not done_ids:
            return 0
        moves = self.client.search_read(
            "stock.move",
            [("picking_id", "in", done_ids), ("state", "=", "done")],
            fields=["id", "quantity"],
        )
        return int(round(sum(float(m.get("quantity") or 0.0) for m in moves)))

    def invoice_row(self, canonical_id: str) -> dict[str, Any] | None:
        ref = self._ref(canonical_id)
        rows = self.client.search_read(
            "account.move",
            [
                ("move_type", "=", "out_invoice"),
                ("invoice_origin", "=", ref.odoo_name),
                ("state", "!=", "cancel"),
                ("payment_state", "!=", "reversed"),
            ],
            fields=[
                "id",
                "name",
                "state",
                "payment_state",
                "amount_untaxed",
                "amount_total",
                "amount_residual",
                "invoice_origin",
                "journal_id",
            ],
        )
        if not rows:
            return None
        rows.sort(key=lambda row: int(row["id"]), reverse=True)
        return rows[0]

    def credit_note_row(self, canonical_id: str) -> dict[str, Any] | None:
        ref = self._ref(canonical_id)
        rows = self.client.search_read(
            "account.move",
            [
                ("move_type", "=", "out_refund"),
                ("invoice_origin", "=", ref.odoo_name),
                ("state", "=", "posted"),
            ],
            fields=[
                "id",
                "name",
                "move_type",
                "state",
                "amount_total",
                "invoice_origin",
                "ref",
            ],
        )
        # Exclude internal time-machine invoice-revert reversals so revert returns order to 'shipped'
        rows = [
            r
            for r in rows
            if "OpenOntology Time-Machine Revert" not in str(r.get("ref") or "")
        ]
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

    def attach_existing_odoo(self) -> O2CData | None:
        """Reconnect self.data to an active partner/order set in Odoo after restart."""
        partner_rows = self.client.search_read(
            "res.partner",
            [("ref", "=", self.partner_ref)],
            fields=["id", "credit_limit"],
        )
        if not partner_rows:
            return None
        partner_id = int(partner_rows[0]["id"])
        product_ids = self.client.search(
            "product.product", [("default_code", "=", PRODUCT_CODE)]
        )
        if not product_ids:
            return None
        product_id = int(product_ids[0])
        order_rows = self.client.search_read(
            "sale.order",
            [
                ("partner_id", "=", partner_id),
                ("client_order_ref", "!=", False),
                ("state", "!=", "cancel"),
            ],
            fields=[
                "id",
                "name",
                "client_order_ref",
                "amount_total",
                "note",
                "order_line",
            ],
        )
        order_rows.sort(key=lambda row: int(row["id"]))
        active_rows: dict[str, dict[str, Any]] = {}
        for row in order_rows:
            cid = str(row.get("client_order_ref") or "")
            if not cid or cid.startswith("__stale__"):
                continue
            active_rows[cid] = row
        if not active_rows:
            return None

        credit_cents = int(
            round(float(partner_rows[0].get("credit_limit") or 0.0) * 100)
        ) or self.credit_limit_cents
        run_tag = str(
            next(iter(active_rows.values())).get("note")
            or f"O2C-REATTACH-{uuid.uuid4().hex[:8]}"
        )
        data = O2CData(
            client=self.client,
            partner_id=partner_id,
            product_id=product_id,
            run_tag=run_tag,
            credit_limit_cents=credit_cents,
        )
        line_ids = [
            int(row["order_line"][0])
            for row in active_rows.values()
            if row.get("order_line")
        ]
        line_by_id: dict[int, dict[str, Any]] = {}
        if line_ids:
            for line in self.client.read(
                "sale.order.line",
                line_ids,
                ["id", "product_uom_qty", "price_unit", "name"],
            ):
                line_by_id[int(line["id"])] = line

        for cid, row in active_rows.items():
            line_id = int(row["order_line"][0]) if row.get("order_line") else 0
            line = line_by_id.get(line_id, {})
            qty = int(round(float(line.get("product_uom_qty") or 1)))
            unit_price = float(line.get("price_unit") or 0.0)
            summary = str(line.get("name") or PRODUCT_NAME)
            data.orders[cid] = O2COrderRef(
                canonical_id=cid,
                odoo_id=int(row["id"]),
                odoo_name=str(row["name"]),
                partner_id=partner_id,
                product_id=product_id,
                quantity=qty,
                unit_price=unit_price,
                total_cents=int(round(float(row["amount_total"]) * 100)),
                run_tag=run_tag,
                product_summary=summary,
            )
        self.data = data
        return data

    def _collect_readback(self) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
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
        vouchers: list[dict[str, Any]] = []
        invoice_residual_cents = 0

        for canonical_id in sorted(data.orders):
            ref = data.order(canonical_id)
            order = self.order_row(canonical_id)
            all_pickings = self.delivery_rows(canonical_id)
            delivery = all_pickings[-1] if all_pickings else None
            invoice = self.invoice_row(canonical_id)
            credit_note = self.credit_note_row(canonical_id)

            has_done_picking = any(p["state"] == "done" for p in all_pickings)
            has_open_picking = any(p["state"] != "done" for p in all_pickings)
            if has_done_picking and has_open_picking:
                delivery_agg_state: str | None = "partial"
            elif has_done_picking:
                delivery_agg_state = "done"
            elif delivery is not None:
                delivery_agg_state = str(delivery["state"])
            else:
                delivery_agg_state = None
            if order.get("delivery_status") == "partial" and delivery_agg_state is not None:
                delivery_agg_state = "partial"

            order_status = _map_order_status(
                order["state"], delivery_agg_state, invoice, credit_note=credit_note
            )
            order_rows.append(order)

            untaxed_cents = int(round(float(order.get("amount_untaxed") or order["amount_total"]) * 100))
            total_cents = int(round(float(order["amount_total"]) * 100))
            tax_cents = max(0, total_cents - untaxed_cents)
            tax_rate_bps = (
                int(round(tax_cents * 10_000 / untaxed_cents))
                if untaxed_cents > 0 and tax_cents > 0
                else 0
            )
            sales_orders.append(
                o2c.SalesOrder(
                    order_id=canonical_id,
                    customer=self.partner_ref,
                    product_summary=ref.product_summary or PRODUCT_NAME,
                    quantity=ref.quantity,
                    unit_price_cents=int(round(ref.unit_price * 100)),
                    tax_rate_bps=tax_rate_bps,
                    tax_cents=tax_cents,
                    total_cents=total_cents,
                    status=order_status,
                ).source_properties()
            )

            if delivery is not None:
                shipped_qty = (
                    ref.quantity
                    if delivery_agg_state == "done"
                    else self.shipped_qty_for_order(canonical_id)
                )
                deliveries.append(
                    o2c.Delivery(
                        delivery_id=f"DEL-{canonical_id}",
                        order=canonical_id,
                        ordered_qty=ref.quantity,
                        shipped_qty=shipped_qty,
                        status=(
                            "shipped"
                            if delivery_agg_state == "done"
                            else ("partial" if delivery_agg_state == "partial" else "pending")
                        ),
                    ).source_properties()
                )

            if credit_note is not None:
                cn_cents = int(round(float(credit_note["amount_total"]) * 100))
                invoices.append(
                    o2c.Invoice(
                        invoice_id=f"INV-{canonical_id}",
                        order=canonical_id,
                        customer=self.partner_ref,
                        amount_cents=cn_cents,
                        tax_cents=tax_cents,
                        paid_cents=0,
                        refunded_cents=cn_cents,
                        status="refunded",
                    ).to_properties()
                )
                vouchers.append(
                    o2c.ReceivableVoucher(
                        voucher_id=f"ARV-INV-{canonical_id}",
                        ledger=o2c.ar_ledger_id(self.partner_ref),
                        customer=self.partner_ref,
                        order=canonical_id,
                        entry_kind="invoice_post",
                        delta_cents=cn_cents,
                    ).to_properties()
                )
                vouchers.append(
                    o2c.ReceivableVoucher(
                        voucher_id=f"ARV-REF-{canonical_id}",
                        ledger=o2c.ar_ledger_id(self.partner_ref),
                        customer=self.partner_ref,
                        order=canonical_id,
                        entry_kind="credit_note_refund",
                        delta_cents=-cn_cents,
                    ).to_properties()
                )
            elif invoice is not None and invoice["state"] == "posted":
                inv_untaxed_cents = int(
                    round(float(invoice.get("amount_untaxed") or invoice["amount_total"]) * 100)
                )
                inv_total_cents = int(round(float(invoice["amount_total"]) * 100))
                inv_tax_cents = max(0, inv_total_cents - inv_untaxed_cents)
                inv_residual_cents = int(
                    round(float(invoice["amount_residual"]) * 100)
                )
                paid_cents = max(0, inv_total_cents - inv_residual_cents)
                paid = (
                    invoice["payment_state"] in ("paid", "in_payment")
                    or inv_residual_cents == 0
                )
                partial = (not paid) and paid_cents > 0
                invoices.append(
                    o2c.Invoice(
                        invoice_id=f"INV-{canonical_id}",
                        order=canonical_id,
                        customer=self.partner_ref,
                        amount_cents=inv_total_cents,
                        tax_cents=inv_tax_cents,
                        paid_cents=paid_cents,
                        refunded_cents=0,
                        status="paid" if paid else ("partial" if partial else "posted"),
                    ).to_properties()
                )
                vouchers.append(
                    o2c.ReceivableVoucher(
                        voucher_id=f"ARV-INV-{canonical_id}",
                        ledger=o2c.ar_ledger_id(self.partner_ref),
                        customer=self.partner_ref,
                        order=canonical_id,
                        entry_kind="invoice_post",
                        delta_cents=inv_total_cents,
                    ).to_properties()
                )
                if paid_cents > 0:
                    vouchers.append(
                        o2c.ReceivableVoucher(
                            voucher_id=f"ARV-PAY-{canonical_id}",
                            ledger=o2c.ar_ledger_id(self.partner_ref),
                            customer=self.partner_ref,
                            order=canonical_id,
                            entry_kind="payment_settlement",
                            delta_cents=-paid_cents,
                        ).to_properties()
                    )
                invoice_residual_cents += inv_residual_cents
                data.invoices[canonical_id] = int(invoice["id"])

        customer = o2c.Customer(
            customer_id=self.partner_ref,
            name=partner["name"],
            credit_limit_cents=int(
                round(float(partner.get("credit_limit") or 0.0) * 100)
            ) or data.credit_limit_cents,
        )
        ar_ledger = o2c.ReceivableBalance(
            ledger_id=o2c.ar_ledger_id(self.partner_ref),
            customer=self.partner_ref,
            ar_balance_cents=invoice_residual_cents,
        )
        payload = {
            "Customer": [customer.source_properties()],
            "ReceivableBalance": [ar_ledger.to_properties()],
            "SalesOrder": sales_orders,
            "Delivery": deliveries,
            "Invoice": invoices,
            "ReceivableVoucher": vouchers,
        }
        return payload, order_rows

    def build_readback_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """从真实 Odoo 19 构造供 LatentSpace.settle_move 使用的多表状态读回。"""
        payload, _ = self._collect_readback()
        return payload

    # -- upward snapshot ---------------------------------------------------
    def sync_from_odoo(self) -> dict[str, Any]:
        payload, order_rows = self._collect_readback()
        snapshot = self.runtime.load_snapshot(payload)
        return {
            "customers": 1,
            "orders": len(payload["SalesOrder"]),
            "deliveries": len(payload["Delivery"]),
            "invoices": len(payload["Invoice"]),
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

    def execute_move(self, move: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a LatentSpace pending move into real Odoo and return execution evidence."""
        if self.data is None:
            raise RuntimeError("seed_odoo() must be called first")
        run_id = str(move["run_id"])
        action = str(move["action_type"])
        params = move.get("params") or {}
        order_id = params.get("order") or params.get("order_id")
        if not order_id:
            raise ValueError(f"move {run_id!r} missing order / order_id")
        order_key = str(order_id)
        if action != "create_sales_order" and order_key not in self.data.orders:
            raise KeyError(f"unknown order {order_key!r} for move {run_id!r}")

        target_status = _target_status(move)
        if action == "confirm_sales_order" and target_status == "draft":
            evidence = self._apply_revert_confirm(run_id, order_key)
        elif action == "issue_and_post_invoice" and target_status == "shipped":
            evidence = self._apply_revert_invoice(run_id, order_key)
        elif action == "create_sales_order":
            evidence = self._apply_create_order(run_id, move, order_key)
        elif action == "confirm_sales_order":
            evidence = self._apply_confirm(run_id, order_key)
        elif action == "ship_order_delivery":
            evidence = self._apply_ship(
                run_id, order_key, ship_quantity=params.get("ship_quantity")
            )
        elif action == "issue_and_post_invoice":
            evidence = self._apply_invoice(run_id, order_key)
        elif action == "record_customer_payment":
            evidence = self._apply_payment(
                run_id,
                order_key,
                payment_cents=params.get("payment_cents"),
                allow_partial=bool(params.get("allow_partial", False)),
            )
        elif action == "issue_credit_note_refund":
            evidence = self._apply_credit_note_refund(
                run_id,
                order_key,
                reason=str(params.get("reason") or "Customer credit note refund"),
            )
        else:
            raise ValueError(f"unsupported Odoo move action {action!r}")

        self._seen.add(run_id)
        return evidence

    # -- downward writeback ------------------------------------------------
    def process_new_runs(self) -> list[dict[str, Any]]:
        forward_phase = {
            ("create_sales_order", "draft"): -1,
            ("confirm_sales_order", "confirmed"): 0,
            ("ship_order_delivery", "partially_shipped"): 1,
            ("ship_order_delivery", "shipped"): 1,
            ("issue_and_post_invoice", "invoiced"): 2,
            ("record_customer_payment", "paid"): 3,
            ("record_customer_payment", None): 3,
            ("issue_credit_note_refund", "refunded"): 4,
        }
        revert_phase = {
            ("confirm_sales_order", "draft"): 10,
            ("issue_and_post_invoice", "shipped"): 11,
        }
        candidates: list[tuple[int, str, dict[str, Any]]] = []

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
            order_id = params.get("order") or params.get("order_id")
            if self.data is None or not order_id:
                self._seen.add(run_id)
                continue
            if action != "create_sales_order" and order_id not in self.data.orders:
                self._seen.add(run_id)
                continue

            target_status = _target_status(run)
            key = (action, target_status)
            if key in forward_phase:
                candidates.append((forward_phase[key], "forward", run))
            elif key in revert_phase:
                candidates.append((revert_phase[key], "revert", run))
            else:
                self._seen.add(run_id)
                continue

        candidates.sort(
            key=lambda item: (
                item[0],
                item[2].get("executed_at", ""),
                item[2]["run_id"],
            )
        )

        evidence: list[dict[str, Any]] = []
        for _, direction, run in candidates:
            run_id = run["run_id"]
            action = run["action_type"]
            p = run.get("params") or {}
            order_id = p.get("order") or p.get("order_id")

            if direction == "revert":
                if action == "confirm_sales_order":
                    item = self._apply_revert_confirm(run_id, order_id)
                else:
                    item = self._apply_revert_invoice(run_id, order_id)
            elif action == "create_sales_order":
                item = self._apply_create_order(run_id, run, order_id)
            elif action == "confirm_sales_order":
                item = self._apply_confirm(run_id, order_id)
            elif action == "ship_order_delivery":
                item = self._apply_ship(
                    run_id, order_id, ship_quantity=p.get("ship_quantity")
                )
            elif action == "issue_and_post_invoice":
                item = self._apply_invoice(run_id, order_id)
            elif action == "issue_credit_note_refund":
                item = self._apply_credit_note_refund(
                    run_id,
                    order_id,
                    reason=str(p.get("reason") or "Customer credit note refund"),
                )
            else:
                item = self._apply_payment(
                    run_id,
                    order_id,
                    payment_cents=p.get("payment_cents"),
                    allow_partial=bool(p.get("allow_partial", False)),
                )

            self._seen.add(run_id)
            evidence.append(item)
        return evidence

    def _apply_create_order(
        self, run_id: str, run: dict[str, Any], order_id: str
    ) -> dict[str, Any]:
        if self.data is None:
            raise RuntimeError("seed_odoo() must be called first")
        if order_id in self.data.orders:
            ref = self.data.orders[order_id]
            row = self.order_row(order_id)
            return {
                "run_id": run_id,
                "action": "create_sales_order",
                "order_id": order_id,
                "odoo_id": ref.odoo_id,
                "odoo_name": row["name"],
                "sale_state": row["state"],
            }

        after: dict[str, Any] | None = None
        for change in run.get("object_diff", {}).get("object_changes", []):
            if (
                change.get("object_type") == "SalesOrder"
                and change.get("object_id") == order_id
                and change.get("kind") == "create"
            ):
                after = dict(change.get("after") or {})
                break
        if after is None:
            sys_actor = Actor(actor_id="__adapter__", role="system", clearance_tags={"*"})
            obj = self.runtime.get_object(o2c.SalesOrder, order_id, sys_actor)
            after = obj.to_properties()

        quantity = int(after["quantity"])
        unit_price_cents = int(after["unit_price_cents"])
        unit_price = round(unit_price_cents / 100.0, 2)
        product_summary = str(after.get("product_summary") or PRODUCT_NAME)

        odoo_id = self.client.create(
            "sale.order",
            {
                "partner_id": self.data.partner_id,
                "client_order_ref": order_id,
                "note": self.data.run_tag,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.data.product_id,
                            "name": product_summary,
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
            ["id", "name", "client_order_ref", "state", "amount_total", "partner_id"],
        )[0]
        if row["state"] != "draft":
            raise OdooRPCError(
                f"new quotation {order_id} is {row['state']!r}"
            )
        self.data.orders[order_id] = O2COrderRef(
            canonical_id=order_id,
            odoo_id=odoo_id,
            odoo_name=row["name"],
            partner_id=self.data.partner_id,
            product_id=self.data.product_id,
            quantity=quantity,
            unit_price=unit_price,
            total_cents=int(round(float(row["amount_total"]) * 100)),
            run_tag=self.data.run_tag,
            product_summary=product_summary,
        )
        return {
            "run_id": run_id,
            "action": "create_sales_order",
            "order_id": order_id,
            "odoo_id": odoo_id,
            "odoo_name": row["name"],
            "sale_state": row["state"],
        }

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

    def _apply_ship(
        self,
        run_id: str,
        order_id: str,
        *,
        ship_quantity: int | None = None,
    ) -> dict[str, Any]:
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
                qty_to_ship = (
                    min(float(ship_quantity), float(move["product_uom_qty"]))
                    if ship_quantity is not None
                    else move["product_uom_qty"]
                )
                self.client.write(
                    "stock.move",
                    [int(move["id"])],
                    {
                        "quantity": qty_to_ship,
                        "picked": True,
                    },
                )
            result = self.client.execute_kw(
                "stock.picking", "button_validate", [[picking_id]]
            )
            self._handle_validate_result(result, picking_id=picking_id)
            validated = self.client.read("stock.picking", [picking_id], ["id", "name", "state"])[0]
            picking = validated

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
                    "tax_ids",
                ],
            )
            if not lines:
                raise OdooRPCError(f"{order_id} has no sale.order.line")
            line = lines[0]
            tax_ids = [int(t) for t in (line.get("tax_ids") or [])]
            invoice_id = self.client.create(
                "account.move",
                {
                    "move_type": "out_invoice",
                    "partner_id": ref.partner_id,
                    "invoice_date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
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
                                "tax_ids": [(6, 0, tax_ids)],
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

    def _apply_payment(
        self,
        run_id: str,
        order_id: str,
        *,
        payment_cents: int | None = None,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
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
            wiz_vals: dict[str, Any] = {
                "payment_date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            }
            if payment_cents is not None:
                wiz_vals["amount"] = round(int(payment_cents) / 100.0, 2)
            wizard_id = self.client.execute_kw(
                "account.payment.register",
                "create",
                [wiz_vals],
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
        if not allow_partial:
            if residual != 0.0:
                raise OdooRPCError(
                    f"payment failed for {order_id}: residual={residual}"
                )
            if invoice["payment_state"] not in ("paid", "in_payment"):
                raise OdooRPCError(
                    f"payment state for {order_id} is {invoice['payment_state']!r}"
                )
        elif invoice["payment_state"] not in ("partial", "paid", "in_payment"):
            raise OdooRPCError(
                f"partial payment state for {order_id} is {invoice['payment_state']!r}"
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

    def _apply_credit_note_refund(
        self,
        run_id: str,
        order_id: str,
        *,
        reason: str = "Customer credit note refund",
    ) -> dict[str, Any]:
        invoice = self.invoice_row(order_id)
        if invoice is None:
            existing_cn = self.credit_note_row(order_id)
            if existing_cn is not None:
                return {
                    "run_id": run_id,
                    "action": "issue_credit_note_refund",
                    "order_id": order_id,
                    "credit_note_id": int(existing_cn["id"]),
                    "credit_note_name": existing_cn["name"],
                    "credit_note_state": existing_cn["state"],
                }
            raise OdooRPCError(f"no invoice to refund for {order_id}")
        invoice_id = int(invoice["id"])
        journal_field = invoice.get("journal_id")
        journal_id = (
            int(journal_field[0])
            if isinstance(journal_field, (list, tuple)) and journal_field
            else None
        )
        ctx = {"active_model": "account.move", "active_ids": [invoice_id]}
        wiz_vals: dict[str, Any] = {
            "move_ids": [(6, 0, [invoice_id])],
            "date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            "reason": reason,
        }
        if journal_id is not None:
            wiz_vals["journal_id"] = journal_id
        wiz_id = self.client.execute_kw(
            "account.move.reversal",
            "create",
            [wiz_vals],
            {"context": ctx},
        )
        try:
            rev_res = self.client.execute_kw(
                "account.move.reversal", "refund_moves", [[wiz_id]], {"context": ctx}
            )
        except OdooRPCError:
            rev_res = self.client.execute_kw(
                "account.move.reversal", "reverse_moves", [[wiz_id]], {"context": ctx}
            )
        if isinstance(rev_res, dict) and rev_res.get("res_id"):
            cn_id = int(rev_res["res_id"])
            cn_rows = self.client.read("account.move", [cn_id], ["id", "name", "state", "move_type"])
            if cn_rows and cn_rows[0]["state"] == "draft":
                self.client.execute_kw("account.move", "action_post", [[cn_id]])
        cn = self.credit_note_row(order_id)
        if cn is None:
            raise OdooRPCError(f"credit note refund failed for {order_id}")
        return {
            "run_id": run_id,
            "action": "issue_credit_note_refund",
            "order_id": order_id,
            "credit_note_id": int(cn["id"]),
            "credit_note_name": cn["name"],
            "credit_note_state": cn["state"],
        }

    def _apply_revert_confirm(self, run_id: str, order_id: str) -> dict[str, Any]:
        ref = self._ref(order_id)
        picking = self.delivery_row(order_id)
        if picking is not None and picking["state"] == "done":
            raise OdooRPCError(
                f"cannot revert {order_id}: delivery {picking['name']} already shipped"
            )
        row = self.order_row(order_id)
        if row["state"] not in ("draft", "cancel"):
            res = self.client.execute_kw(
                "sale.order",
                "action_cancel",
                [[ref.odoo_id]],
                {"context": {"disable_cancel_warning": True}},
            )
            if isinstance(res, dict) and res.get("res_model") == "sale.order.cancel":
                wiz_id = self.client.create(
                    "sale.order.cancel", {"order_id": ref.odoo_id}
                )
                self.client.execute_kw(
                    "sale.order.cancel", "action_cancel", [[wiz_id]]
                )
            row = self.order_row(order_id)
        if row["state"] == "cancel":
            self.client.execute_kw("sale.order", "action_draft", [[ref.odoo_id]])
            row = self.order_row(order_id)
        if row["state"] != "draft":
            raise OdooRPCError(
                f"revert confirm failed for {order_id}: state={row['state']!r}"
            )
        if self.delivery_row(order_id) is not None:
            raise OdooRPCError(
                f"revert confirm left active delivery for {order_id}"
            )
        return {
            "run_id": run_id,
            "action": "revert_confirm_sales_order",
            "order_id": order_id,
            "odoo_id": ref.odoo_id,
            "odoo_name": ref.odoo_name,
            "sale_state": row["state"],
        }

    def _apply_revert_invoice(self, run_id: str, order_id: str) -> dict[str, Any]:
        ref = self._ref(order_id)
        invoice = self.invoice_row(order_id)
        if invoice is None:
            return {
                "run_id": run_id,
                "action": "revert_issue_and_post_invoice",
                "order_id": order_id,
                "odoo_id": ref.odoo_id,
                "invoice_state": "none",
            }
        if (
            invoice["payment_state"] in ("paid", "in_payment")
            and float(invoice["amount_residual"]) == 0.0
        ):
            raise OdooRPCError(
                f"cannot revert paid invoice {invoice['name']} for {order_id}"
            )

        invoice_id = int(invoice["id"])
        invoice_name = str(invoice["name"])
        journal_field = invoice.get("journal_id")
        journal_id = (
            int(journal_field[0])
            if isinstance(journal_field, (list, tuple)) and journal_field
            else None
        )
        method_used = "credit_note"

        try:
            ctx = {"active_model": "account.move", "active_ids": [invoice_id]}
            wiz_vals: dict[str, Any] = {
                "move_ids": [(6, 0, [invoice_id])],
                "date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
                "reason": f"OpenOntology Time-Machine Revert ({run_id})",
            }
            if journal_id is not None:
                wiz_vals["journal_id"] = journal_id
            wiz_id = self.client.execute_kw(
                "account.move.reversal",
                "create",
                [wiz_vals],
                {"context": ctx},
            )
            rev_res: Any = None
            try:
                rev_res = self.client.execute_kw(
                    "account.move.reversal",
                    "refund_moves",
                    [[wiz_id]],
                    {"context": ctx},
                )
            except OdooRPCError:
                rev_res = self.client.execute_kw(
                    "account.move.reversal",
                    "reverse_moves",
                    [[wiz_id]],
                    {"context": ctx},
                )
            if isinstance(rev_res, dict) and rev_res.get("res_id"):
                cn_id = int(rev_res["res_id"])
                cn_rows = self.client.read("account.move", [cn_id], ["state"])
                if cn_rows and cn_rows[0]["state"] == "draft":
                    self.client.execute_kw("account.move", "action_post", [[cn_id]])
        except OdooRPCError:
            method_used = "cancel"

        if self.invoice_row(order_id) is not None:
            method_used = "cancel"
            inv_now = self.client.read("account.move", [invoice_id], ["state"])[0]
            if inv_now["state"] == "posted":
                self.client.execute_kw("account.move", "button_draft", [[invoice_id]])
            self.client.execute_kw("account.move", "button_cancel", [[invoice_id]])

        if self.invoice_row(order_id) is not None:
            raise OdooRPCError(
                f"revert invoice failed for {order_id}: {invoice_name} still active"
            )
        return {
            "run_id": run_id,
            "action": "revert_issue_and_post_invoice",
            "order_id": order_id,
            "odoo_id": ref.odoo_id,
            "reversed_invoice": invoice_name,
            "method": method_used,
        }

    def tamper_draft_order_price(
        self, order_id: str, new_unit_price: float
    ) -> dict[str, Any]:
        """在真实 Odoo 19 中修改草稿单价并刷新快照，用于演示 OCC 版本冲突熔断。"""
        ref = self._ref(order_id)
        lines = self.client.search_read(
            "sale.order.line",
            [("order_id", "=", ref.odoo_id)],
            fields=["id"],
        )
        if not lines:
            raise OdooRPCError(f"order {order_id!r} has no sale.order.line")
        for line in lines:
            self.client.write(
                "sale.order.line",
                [int(line["id"])],
                {"price_unit": float(new_unit_price)},
            )
        row = self.order_row(order_id)
        ref.unit_price = float(new_unit_price)
        ref.total_cents = int(round(float(row["amount_total"]) * 100))
        self.sync_from_odoo()
        return {
            "order_id": order_id,
            "odoo_id": ref.odoo_id,
            "odoo_name": ref.odoo_name,
            "state": row["state"],
            "unit_price": float(new_unit_price),
            "total_cents": ref.total_cents,
        }

    # -- helpers -----------------------------------------------------------
    def _handle_validate_result(
        self, result: Any, *, picking_id: int | None = None
    ) -> None:
        if not isinstance(result, dict):
            return
        model = result.get("res_model")
        res_id = result.get("res_id")
        if model not in (
            "stock.immediate.transfer",
            "stock.backorder.confirmation",
        ):
            return
        ctx = dict(result.get("context") or {})
        if res_id:
            self.client.execute_kw(model, "process", [[res_id]], {"context": ctx})
            return
        if picking_id is not None and model == "stock.backorder.confirmation":
            wiz_id = self.client.execute_kw(
                model,
                "create",
                [{"pick_ids": [(6, 0, [picking_id])]}],
                {"context": ctx},
            )
            self.client.execute_kw(model, "process", [[wiz_id]], {"context": ctx})

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
    *,
    credit_note: dict[str, Any] | None = None,
) -> str:
    if sale_state == "cancel":
        return "cancelled"
    if sale_state in ("draft", "sent"):
        return "draft"
    if credit_note is not None and credit_note.get("state") == "posted":
        return "refunded"
    if invoice is not None and invoice["state"] == "posted":
        if (
            invoice["payment_state"] in ("paid", "in_payment")
            or float(invoice["amount_residual"]) == 0.0
        ):
            return "paid"
        return "invoiced"
    if delivery_state == "done":
        return "shipped"
    if delivery_state == "partial":
        return "partially_shipped"
    return "confirmed"


def _target_status(run: dict[str, Any]) -> str | None:
    for change in run.get("object_diff", {}).get("object_changes", []):
        if (
            change["object_type"] == "SalesOrder"
            and change["kind"] in ("create", "update")
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
    ar_balance_cents = o2c.get_customer_ar_balance(
        runtime, PARTNER_REF, o2c.FINANCE_DIRECTOR
    )
    print(
        f"  customer credit_limit={customer.credit_limit_cents / 100:.2f} 元"
        f" ar_balance={ar_balance_cents / 100:.2f} 元"
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
        {"order": "O2C-SO-001"},
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
            {"order": "O2C-SO-001"},
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
        {"order": "O2C-SO-001"},
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
        {"order": "O2C-SO-001"},
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
        {"order": "O2C-SO-001", "payment_cents": 2_000_000},
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
        {"order": "O2C-SO-002"},
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
            "customer": PARTNER_REF,
            "risk_tier": "WATCHLIST",
            "credit_hold_note": "watch Q4 credit exposure",
            "order": "O2C-SO-001",
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
        "ar_balance_cents": o2c.get_customer_ar_balance(
            runtime, PARTNER_REF, o2c.FINANCE_DIRECTOR
        ),
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
