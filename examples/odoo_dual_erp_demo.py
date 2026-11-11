"""双真实 Odoo 19 ERP 的同步适配器与端到端演示（PSM）。

本模块是唯一允许出现 Odoo / JSON-RPC / HTTP / 数据库克隆代码的地方。
纯业务模型位于 ``examples/odoo_orders_ontology.py``，内核 ``open_ontology/``
不感知任何外部系统。

运行：``python3 -m examples.odoo_dual_erp_demo``
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from open_ontology import Actor, InvariantError, OntologyRuntime

from .odoo_orders_ontology import (
    CUSTOMER_ORDERS,
    ORDER_NOTES,
    Customer,
    Order,
    OrderNote,
    spec,
)

# ---------------------------------------------------------------------------
# Odoo connection parameters
# ---------------------------------------------------------------------------
ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069/jsonrpc")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")
SOURCE_DB = "odoo_demo"
DB_NORTH = "erp_north"
DB_SOUTH = "erp_south"

MASTER_CANDIDATES = (
    os.environ.get("ODOO_MASTER_PASSWORD"),
    "odoo-demo-master-2026",
    "admin",
)

# ---------------------------------------------------------------------------
# Business actors
# ---------------------------------------------------------------------------
NORTH_DISPATCHER = Actor(
    actor_id="north-dispatcher",
    role="dispatcher",
    clearance_tags={"region:north"},
)
SOUTH_DISPATCHER = Actor(
    actor_id="south-dispatcher",
    role="dispatcher",
    clearance_tags={"region:south"},
)
OPS_MANAGER = Actor(
    actor_id="ops-manager",
    role="ops_manager",
    clearance_tags={"*"},
)
AI_AGENT = Actor(
    actor_id="ai-agent",
    role="ai_agent",
    clearance_tags={"*"},
)

# ---------------------------------------------------------------------------
# Seed definitions
# ---------------------------------------------------------------------------
PARTNERS = {
    "north": ("N-C01", "Northwind Manufacturing"),
    "south": ("S-C01", "Southern Health Systems"),
}

PRODUCTS = {
    "N-WIDGET": ("North Industrial Widget", 100.0),
    "S-WIDGET": ("South Medical Widget", 200.0),
    "S-BIGWIDGET": ("Southern MRI Scanner", 60000.0),
}


@dataclass(frozen=True)
class OrderSeed:
    canonical_id: str
    region: str
    partner_ref: str
    product_code: str
    quantity: float
    unit_price: float
    shipped: bool


ORDER_SEEDS = (
    OrderSeed("N-SO001", "north", "N-C01", "N-WIDGET", 2, 100.0, False),
    OrderSeed("N-SO002", "north", "N-C01", "N-WIDGET", 5, 100.0, True),
    OrderSeed("S-SO001", "south", "S-C01", "S-WIDGET", 3, 200.0, False),
    OrderSeed("S-SO002", "south", "S-C01", "S-BIGWIDGET", 1, 60000.0, False),
    OrderSeed("S-SO003", "south", "S-C01", "S-WIDGET", 2, 200.0, True),
)


# ---------------------------------------------------------------------------
# A tiny JSON-RPC client (stdlib only)
# ---------------------------------------------------------------------------
class OdooRPCError(RuntimeError):
    """Raised when the Odoo JSON-RPC endpoint returns an error."""


def _jsonrpc(service: str, method: str, args: list[Any]) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1,
    }
    request = urllib.request.Request(
        ODOO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OdooRPCError(f"cannot reach {ODOO_URL}: {exc}") from exc

    data = json.loads(body)
    if data.get("error"):
        error = data["error"]
        detail = error.get("data") or {}
        message = detail.get("message") or error.get("message") or str(error)
        raise OdooRPCError(str(message))
    return data.get("result")


def duplicate_database(master_password: str, source: str, target: str) -> bool:
    return bool(
        _jsonrpc(
            "db",
            "duplicate_database",
            [master_password, source, target],
        )
    )


class OdooJSONRPC:
    """Minimal Odoo JSON-RPC client for the models used by this scenario."""

    def __init__(
        self,
        db: str,
        user: str = ODOO_USER,
        password: str = ODOO_PASSWORD,
    ) -> None:
        self.db = db
        self.user = user
        self.password = password
        self.uid: int | None = None

    # -- auth --------------------------------------------------------------
    def login(self) -> int:
        uid = _jsonrpc(
            "common", "login", [self.db, self.user, self.password]
        )
        if not uid:
            raise OdooRPCError(
                f"login failed for {self.db!r} as {self.user!r}"
            )
        self.uid = int(uid)
        return self.uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        if self.uid is None:
            self.login()
        return _jsonrpc(
            "object",
            "execute_kw",
            [
                self.db,
                self.uid,
                self.password,
                model,
                method,
                args or [],
                kwargs or {},
            ],
        )

    # -- generic CRUD ------------------------------------------------------
    def search(
        self,
        model: str,
        domain: list[Any] | None = None,
        limit: int | None = None,
    ) -> list[int]:
        kwargs: dict[str, Any] = {}
        if limit is not None:
            kwargs["limit"] = limit
        return list(
            self.execute_kw(model, "search", [domain or []], kwargs)
        )

    def search_read(
        self,
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        return list(
            self.execute_kw(model, "search_read", [domain or []], kwargs)
        )

    def create(self, model: str, values: dict[str, Any]) -> int:
        return int(self.execute_kw(model, "create", [values]))

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        return bool(self.execute_kw(model, "write", [ids, values]))

    def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        return list(self.execute_kw(model, "read", [ids], kwargs))

    def unlink(self, model: str, ids: list[int]) -> bool:
        return bool(self.execute_kw(model, "unlink", [ids]))

    # -- order helpers -----------------------------------------------------
    def order_by_ref(self, order_ref: str) -> dict[str, Any]:
        ids = self.search(
            "sale.order", [("client_order_ref", "=", order_ref)]
        )
        if len(ids) != 1:
            raise OdooRPCError(
                f"expected exactly one sale.order with client_order_ref="
                f"{order_ref!r} in {self.db}, found {len(ids)}"
            )
        return self.read(
            "sale.order",
            ids,
            [
                "id",
                "name",
                "client_order_ref",
                "partner_id",
                "state",
                "amount_total",
                "note",
                "order_line",
            ],
        )[0]

    def order_state(self, order_ref: str) -> str:
        return str(self.order_by_ref(order_ref)["state"])

    def cancel_order(self, order_ref: str) -> str:
        order = self.order_by_ref(order_ref)
        if order["state"] != "cancel":
            self.execute_kw(
                "sale.order", "action_cancel", [[int(order["id"])]]
            )
        return self.order_state(order_ref)

    def restore_order(self, order_ref: str) -> str:
        order = self.order_by_ref(order_ref)
        state = order["state"]
        if state == "sale":
            return state
        if state == "cancel":
            self.execute_kw(
                "sale.order", "action_draft", [[int(order["id"])]]
            )
        self.execute_kw(
            "sale.order", "action_confirm", [[int(order["id"])]]
        )
        return self.order_state(order_ref)

    def set_order_note(self, order_ref: str, note: str) -> None:
        order = self.order_by_ref(order_ref)
        self.write("sale.order", [int(order["id"])], {"note": note})

    def get_order_line_price(self, order_ref: str) -> float:
        order = self.order_by_ref(order_ref)
        lines = self.search_read(
            "sale.order.line",
            [("order_id", "=", int(order["id"]))],
            fields=["id", "price_unit"],
        )
        if not lines:
            raise OdooRPCError(f"order {order_ref!r} has no lines")
        return float(lines[0]["price_unit"])

    def set_order_line_price(self, order_ref: str, price: float) -> None:
        order = self.order_by_ref(order_ref)
        lines = self.search_read(
            "sale.order.line",
            [("order_id", "=", int(order["id"]))],
            fields=["id"],
        )
        for line in lines:
            self.write(
                "sale.order.line",
                [int(line["id"])],
                {"price_unit": float(price)},
            )


# ---------------------------------------------------------------------------
# Database provisioning
# ---------------------------------------------------------------------------
def _master_candidates() -> list[str]:
    result: list[str] = []
    for candidate in MASTER_CANDIDATES:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def ensure_database(db_name: str) -> tuple[OdooJSONRPC, str | None]:
    """Return a logged-in client, cloning ``odoo_demo`` if needed.

    Returns ``(client, used_master_password)``.  ``used_master_password`` is
    ``None`` when the database already existed.
    """
    client = OdooJSONRPC(db_name)
    try:
        client.login()
        return client, None
    except OdooRPCError:
        pass

    used_master: str | None = None
    last_error: Exception | None = None
    for master in _master_candidates():
        try:
            duplicate_database(master, SOURCE_DB, db_name)
            used_master = master
            break
        except OdooRPCError as exc:
            last_error = exc
            message = str(exc).lower()
            if "already exists" in message or "duplicate database" in message:
                used_master = None
                break

    for _ in range(60):
        try:
            client.login()
            return client, used_master
        except OdooRPCError as exc:
            last_error = exc
            time.sleep(0.5)

    raise OdooRPCError(
        f"database {db_name!r} is not reachable after provisioning: {last_error}"
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def _get_or_create_partner(
    client: OdooJSONRPC, partner_ref: str, name: str
) -> int:
    ids = client.search("res.partner", [("ref", "=", partner_ref)])
    values = {"name": name, "ref": partner_ref, "customer_rank": 1, "active": True}
    if ids:
        client.write("res.partner", [ids[0]], values)
        return ids[0]
    return client.create("res.partner", values)


def _get_or_create_product(
    client: OdooJSONRPC, default_code: str, name: str, price: float
) -> int:
    ids = client.search(
        "product.product", [("default_code", "=", default_code)]
    )
    values = {
        "name": name,
        "default_code": default_code,
        "type": "consu",
        "list_price": float(price),
        "sale_ok": True,
        "active": True,
        "taxes_id": [(6, 0, [])],
    }
    if ids:
        client.write("product.product", [ids[0]], values)
        return ids[0]
    return client.create("product.product", values)


@dataclass(frozen=True)
class OdooOrderRef:
    canonical_id: str
    db: str
    region: str
    odoo_id: int
    odoo_name: str
    partner_ref: str
    product_code: str
    quantity: float
    unit_price: float
    shipped: bool

    @property
    def note_marker(self) -> str:
        return "STATUS:SHIPPED" if self.shipped else "STATUS:PENDING"


@dataclass
class DualERPData:
    clients: dict[str, OdooJSONRPC]
    orders: dict[str, OdooOrderRef] = field(default_factory=dict)
    used_master_password: str | None = None

    def client(self, db: str) -> OdooJSONRPC:
        return self.clients[db]

    def order(self, canonical_id: str) -> OdooOrderRef:
        return self.orders[canonical_id]

    def state(self, canonical_id: str) -> str:
        order = self.order(canonical_id)
        return self.client(order.db).order_state(canonical_id)


def _delete_orders(client: OdooJSONRPC, order_refs: list[str]) -> None:
    if not order_refs:
        return
    ids = client.search(
        "sale.order", [("client_order_ref", "in", order_refs)]
    )
    for odoo_id in ids:
        row = client.read("sale.order", [odoo_id], ["state"])[0]
        if row["state"] != "cancel":
            try:
                client.execute_kw(
                    "sale.order", "action_cancel", [[odoo_id]]
                )
            except OdooRPCError:
                pass
    if not ids:
        return
    try:
        client.unlink("sale.order", ids)
    except OdooRPCError:
        # Free the canonical refs even if Odoo refuses the delete.
        for odoo_id in ids:
            try:
                client.write(
                    "sale.order",
                    [odoo_id],
                    {"client_order_ref": f"__stale__{odoo_id}"},
                )
            except OdooRPCError:
                pass


def seed_database(client: OdooJSONRPC, region: str) -> dict[str, OdooOrderRef]:
    partner_ref, partner_name = PARTNERS[region]
    partner_id = _get_or_create_partner(client, partner_ref, partner_name)

    product_ids: dict[str, int] = {}
    for product_code in {
        seed.product_code for seed in ORDER_SEEDS if seed.region == region
    }:
        name, price = PRODUCTS[product_code]
        product_ids[product_code] = _get_or_create_product(
            client, product_code, name, price
        )

    seeds = [seed for seed in ORDER_SEEDS if seed.region == region]
    _delete_orders(client, [seed.canonical_id for seed in seeds])

    orders: dict[str, OdooOrderRef] = {}
    for seed in seeds:
        marker = "STATUS:SHIPPED" if seed.shipped else "STATUS:PENDING"
        odoo_id = client.create(
            "sale.order",
            {
                "partner_id": partner_id,
                "client_order_ref": seed.canonical_id,
                "note": marker,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product_ids[seed.product_code],
                            "product_uom_qty": seed.quantity,
                            "price_unit": seed.unit_price,
                        },
                    )
                ],
            },
        )
        client.execute_kw("sale.order", "action_confirm", [[odoo_id]])
        client.write("sale.order", [odoo_id], {"note": marker})
        row = client.read(
            "sale.order",
            [odoo_id],
            ["id", "name", "state", "amount_total", "client_order_ref"],
        )[0]
        if row["state"] != "sale":
            raise OdooRPCError(
                f"order {seed.canonical_id} did not confirm: {row['state']!r}"
            )
        orders[seed.canonical_id] = OdooOrderRef(
            canonical_id=seed.canonical_id,
            db=client.db,
            region=region,
            odoo_id=odoo_id,
            odoo_name=row["name"],
            partner_ref=seed.partner_ref,
            product_code=seed.product_code,
            quantity=seed.quantity,
            unit_price=seed.unit_price,
            shipped=seed.shipped,
        )
    return orders


def connect_dual_odoo_erps() -> DualERPData:
    """Ensure both databases exist and return logged-in clients (no seeding)."""
    north, used_north = ensure_database(DB_NORTH)
    south, used_south = ensure_database(DB_SOUTH)
    return DualERPData(
        clients={DB_NORTH: north, DB_SOUTH: south},
        used_master_password=used_north or used_south,
    )


def setup_dual_odoo_erps(reset: bool = True) -> DualERPData:
    """Ensure both real ERP databases exist and contain deterministic data."""
    data = connect_dual_odoo_erps()
    data.orders.update(seed_database(data.client(DB_NORTH), "north"))
    data.orders.update(seed_database(data.client(DB_SOUTH), "south"))
    if reset:
        reset_dual_odoo_orders(data)
    return data


def reset_dual_odoo_orders(data: DualERPData) -> None:
    """Reset every canonical order to its initial confirmed/pending state."""
    for canonical_id, ref in data.orders.items():
        client = data.client(ref.db)
        row = client.order_by_ref(canonical_id)
        state = row["state"]
        if state == "cancel":
            client.execute_kw(
                "sale.order", "action_draft", [[int(row["id"])]]
            )
            client.execute_kw(
                "sale.order", "action_confirm", [[int(row["id"])]]
            )
        elif state in ("draft", "sent"):
            client.execute_kw(
                "sale.order", "action_confirm", [[int(row["id"])]]
            )
        elif state != "sale":
            raise OdooRPCError(
                f"cannot reset {canonical_id}: unexpected state {state!r}"
            )
        client.set_order_note(canonical_id, ref.note_marker)
        final_state = client.order_state(canonical_id)
        if final_state != "sale":
            raise OdooRPCError(
                f"reset of {canonical_id} failed: state={final_state!r}"
            )


# ---------------------------------------------------------------------------
# Upward snapshot: real Odoo -> pure ontology
# ---------------------------------------------------------------------------
def _map_order_status(row: dict[str, Any]) -> str:
    if row["state"] == "cancel":
        return "cancelled"
    note = row.get("note") or ""
    if "STATUS:SHIPPED" in note:
        return "shipped"
    return "pending"


def sync_from_both_odoos_to_ontology(
    runtime: OntologyRuntime,
    data: DualERPData | None = None,
) -> dict[str, Any]:
    """Read both real ERPs and replace the source-backed ontology snapshot."""
    if data is None:
        data = connect_dual_odoo_erps()

    customers: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    links: list[tuple[str, str]] = []

    for db, region in ((DB_NORTH, "north"), (DB_SOUTH, "south")):
        client = data.client(db)
        partner_ref, _ = PARTNERS[region]
        partner_rows = client.search_read(
            "res.partner",
            [("ref", "=", partner_ref)],
            fields=["id", "name", "ref"],
        )
        if not partner_rows:
            raise OdooRPCError(
                f"customer {partner_ref!r} missing from {db!r}"
            )
        partner = partner_rows[0]
        customers.append(
            Customer(
                customer_id=partner_ref,
                name=partner["name"],
                region=region,
            ).to_properties()
        )

        rows = client.search_read(
            "sale.order",
            [("client_order_ref", "!=", False)],
            fields=[
                "id",
                "name",
                "client_order_ref",
                "partner_id",
                "state",
                "amount_total",
                "note",
            ],
        )
        for row in rows:
            canonical_id = row.get("client_order_ref")
            expected_prefix = "N-SO" if region == "north" else "S-SO"
            if not canonical_id or not canonical_id.startswith(expected_prefix):
                continue
            order = Order(
                order_id=canonical_id,
                customer_id=partner_ref,
                region=region,
                status=_map_order_status(row),
                total_cents=int(round(float(row["amount_total"]) * 100)),
            )
            orders.append(order.source_properties())
            links.append((partner_ref, canonical_id))

    snapshot = runtime.load_snapshot(
        {"Customer": customers, "Order": orders},
        links={"customer_orders": links},
    )
    return {
        "customers": len(customers),
        "orders": len(orders),
        "links": len(links),
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# Downward writeback: committed ontology actions -> real Odoo
# ---------------------------------------------------------------------------
class OdooWritebackBridge:
    """Reads new committed ``cancel_order`` runs and applies them to Odoo."""

    def __init__(self, runtime: OntologyRuntime, data: DualERPData) -> None:
        self.runtime = runtime
        self.data = data
        # Pending runs must stay eligible: approval may change them later.
        self._seen: set[str] = {
            run["run_id"]
            for run in runtime.list_action_runs()
            if run["status"] != "PENDING_APPROVAL"
        }

    def process_new_runs(self) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []

        for run in self.runtime.list_action_runs():
            run_id = run["run_id"]
            if run_id in self._seen:
                continue
            if run["status"] == "PENDING_APPROVAL":
                # Approval may change this run later; do not consume it yet.
                continue
            if run["status"] != "COMMITTED":
                self._seen.add(run_id)
                continue
            if run["action_type"] != "cancel_order":
                self._seen.add(run_id)
                continue

            change = next(
                (
                    item
                    for item in run["object_diff"]["object_changes"]
                    if item["object_type"] == "Order"
                    and item["kind"] == "update"
                ),
                None,
            )
            if change is None:
                self._seen.add(run_id)
                continue

            order_id = change["object_id"]
            target_status = (change.get("after") or {}).get("status")
            if target_status not in ("cancelled", "pending"):
                self._seen.add(run_id)
                continue
            if order_id not in self.data.orders:
                raise OdooRPCError(
                    f"no Odoo routing for ontology order {order_id!r}"
                )

            ref = self.data.order(order_id)
            client = self.data.client(ref.db)
            before = client.order_state(order_id)

            if target_status == "cancelled":
                after = client.cancel_order(order_id)
                expected = "cancel"
            else:
                after = client.restore_order(order_id)
                expected = "sale"

            if after != expected:
                raise OdooRPCError(
                    f"writeback verification failed for {order_id}: "
                    f"expected {expected!r}, got {after!r}"
                )

            self._seen.add(run_id)
            evidence.append(
                {
                    "run_id": run_id,
                    "order_id": order_id,
                    "db": ref.db,
                    "ontology_status": target_status,
                    "odoo_state_before": before,
                    "odoo_state_after": after,
                    "verified": True,
                }
            )

        return evidence


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_orders(data: DualERPData) -> None:
    for canonical_id in sorted(data.orders):
        ref = data.order(canonical_id)
        row = data.client(ref.db).order_by_ref(canonical_id)
        print(
            f"  {canonical_id}  db={ref.db:<10} odoo_id={ref.odoo_id:<4}"
            f" odoo_name={ref.odoo_name:<8} state={row['state']:<7}"
            f" amount={row['amount_total']:>10.2f}"
        )


def main() -> dict[str, Any]:
    data = setup_dual_odoo_erps(reset=True)
    runtime = OntologyRuntime(spec)

    _section("1. 双真实 Odoo 19 ERP / 订单底账")
    print(
        "  databases: erp_north + erp_south"
        f"  (master password used: {data.used_master_password or 'existing DBs'})"
    )
    _print_orders(data)

    _section("2. 上行快照：两个 ERP -> 纯业务 Ontology")
    sync = sync_from_both_odoos_to_ontology(runtime, data)
    print(f"  {sync}")
    print(f"  customers={runtime.count_objects('Customer')}"
          f" orders={runtime.count_objects('Order')}"
          f" notes={runtime.count_objects('OrderNote')}")

    _section("3. 跨双 ERP 查询 / RLS / 集合代数")
    pending = runtime.search_set(Order, OPS_MANAGER, {"status": "pending"})
    print(f"  pending={pending.ids}")
    agg = runtime.aggregate(pending, group_by="region", sum_field="total_cents")
    for row in agg.rows:
        print(f"  aggregate region={row.key!r} metrics={row.metrics}")
    summary = runtime.call_function(
        "customer_unshipped_summary", {"customer_id": "S-C01"}, OPS_MANAGER
    )
    print(f"  customer_unshipped_summary(S-C01)={summary}")
    north_pending = runtime.search_set(
        Order, NORTH_DISPATCHER, {"status": "pending"}
    )
    south_pending = runtime.search_set(
        Order, SOUTH_DISPATCHER, {"status": "pending"}
    )
    print(f"  RLS north={north_pending.ids} south={south_pending.ids}")
    print(f"  union={runtime.union(north_pending, south_pending).ids}")
    print(f"  intersect={runtime.intersect(north_pending, south_pending).ids}")
    print(f"  subtract={runtime.subtract(pending, north_pending).ids}")

    south_customer = runtime.search_set(
        Customer, OPS_MANAGER, {"customer_id": "S-C01"}
    )
    customer_orders = runtime.pivot(
        south_customer, CUSTOMER_ORDERS, OPS_MANAGER
    )
    print(f"  pivot(S-C01 -> orders)={customer_orders.ids}")
    print(
        "  pivot orders -> customer="
        f"{runtime.pivot(customer_orders, CUSTOMER_ORDERS, OPS_MANAGER, 'reverse').ids}"
    )

    _section("4. 已发货订单取消被拒 + REJECTED 审计")
    try:
        runtime.submit_action(
            "RUN-REJECTED-SHIPPED",
            "cancel_order",
            {"order_id": "N-SO002", "reason": "customer changed mind"},
            NORTH_DISPATCHER,
        )
    except InvariantError as exc:
        print(f"  blocked code={exc.code}")
    rejected = runtime.get_action_run("RUN-REJECTED-SHIPPED")
    print(
        f"  audit status={rejected['status']} error_code={rejected['error_code']}"
        f" odoo_state={data.state('N-SO002')} (must stay sale)"
    )

    _section("5. 待发货订单取消 -> 真实回写 erp_north")
    bridge = OdooWritebackBridge(runtime, data)
    before = data.state("N-SO001")
    cancel_run = runtime.submit_action(
        "RUN-CANCEL-N-001",
        "cancel_order",
        {"order_id": "N-SO001", "reason": "customer requested cancellation"},
        NORTH_DISPATCHER,
    )
    print(f"  ontology run={cancel_run['run_id']} status={cancel_run['status']}")
    print(f"  ontology order status="
          f"{runtime.get_object(Order, 'N-SO001', OPS_MANAGER).status}")
    print(f"  erp_north N-SO001: before={before!r}")
    for item in bridge.process_new_runs():
        print(
            f"  writeback {item['order_id']} db={item['db']}"
            f" {item['odoo_state_before']} -> {item['odoo_state_after']}"
            f" verified={item['verified']}"
        )
    print(f"  erp_north N-SO001: after={data.state('N-SO001')!r}")

    _section("6. 时光机撤回 -> 真实恢复 erp_north")
    revert = runtime.revert_action(
        "RUN-CANCEL-N-001", "RUN-CANCEL-N-001-REV", OPS_MANAGER
    )
    print(f"  revert run={revert['run_id']} status={revert['status']}")
    print(f"  original run status="
          f"{runtime.get_action_run('RUN-CANCEL-N-001')['status']}")
    for item in bridge.process_new_runs():
        print(
            f"  writeback {item['order_id']} db={item['db']}"
            f" {item['odoo_state_before']} -> {item['odoo_state_after']}"
            f" verified={item['verified']}"
        )
    print(f"  erp_north N-SO001: after revert={data.state('N-SO001')!r}")
    print(f"  ontology order status="
          f"{runtime.get_object(Order, 'N-SO001', OPS_MANAGER).status}")

    _section("7. AI Agent 大额取消 -> 人工审批 -> 真实回写 erp_south")
    ai_pending = runtime.submit_action(
        "RUN-AI-LARGE",
        "cancel_order",
        {
            "order_id": "S-SO002",
            "reason": "budget freeze",
            "large_order": True,
            "order_total_cents": 6000000,
        },
        AI_AGENT,
    )
    print(f"  submit status={ai_pending['status']}"
          f" erp_south state={data.state('S-SO002')!r} (must stay sale)")
    print(f"  bridge while pending={bridge.process_new_runs()}")
    approved = runtime.approve_action("RUN-AI-LARGE", OPS_MANAGER)
    print(f"  approved by={approved['approved_by']} status={approved['status']}")
    for item in bridge.process_new_runs():
        print(
            f"  writeback {item['order_id']} db={item['db']}"
            f" {item['odoo_state_before']} -> {item['odoo_state_after']}"
            f" verified={item['verified']}"
        )
    print(f"  erp_south S-SO002: after={data.state('S-SO002')!r}")

    _section("8. 全量重索引：overlay / OrderNote 存活")
    runtime.submit_action(
        "RUN-ASSIGN",
        "assign_order",
        {
            "order_id": "N-SO001",
            "assignee": "north-ops",
            "ops_note": "priority customer; call before dispatch",
        },
        NORTH_DISPATCHER,
    )
    runtime.submit_action(
        "RUN-NOTE",
        "add_order_note",
        {
            "note_id": "NOTE-N-001",
            "order_id": "N-SO001",
            "body": "Customer called: keep the original packaging.",
        },
        NORTH_DISPATCHER,
    )
    north_client = data.client(DB_NORTH)
    old_price = north_client.get_order_line_price("N-SO002")
    north_client.set_order_line_price("N-SO002", old_price + 50.0)
    try:
        resync = sync_from_both_odoos_to_ontology(runtime, data)
        order = runtime.get_object(Order, "N-SO001", OPS_MANAGER)
        note = runtime.get_object(OrderNote, "NOTE-N-001", OPS_MANAGER)
        linked = runtime.get_linked(order, ORDER_NOTES, OrderNote, OPS_MANAGER)
        print(f"  resync={resync}")
        print(f"  N-SO001 assignee={order.assignee!r} ops_note={order.ops_note!r}")
        print(f"  OrderNote={note.note_id!r} body={note.body!r}")
        print(f"  order_notes link={[n.note_id for n in linked]}")
        print(
            "  N-SO002 new total_cents="
            f"{runtime.get_object(Order, 'N-SO002', OPS_MANAGER).total_cents}"
            f" (expected 75000)"
        )
    finally:
        north_client.set_order_line_price("N-SO002", old_price)

    _section("9. 纯业务审计账本 + 真实 Odoo 最终状态")
    for run in runtime.list_action_runs():
        print(
            f"  run {run['run_id']:<22} {run['status']:<16}"
            f" error_code={run['error_code']}"
        )
    _print_orders(data)
    final_pending = runtime.search_set(Order, OPS_MANAGER, {"status": "pending"})
    print(f"  final pending orders={final_pending.ids}")

    summary = {
        "databases": [DB_NORTH, DB_SOUTH],
        "pending_orders": final_pending.ids,
        "pending_total_cents": sum(
            runtime.get_object(Order, oid, OPS_MANAGER).total_cents
            for oid in final_pending.ids
        ),
        "north_cancelled_then_restored": (
            runtime.get_object(Order, "N-SO001", OPS_MANAGER).status
        ),
        "south_large_cancelled": (
            runtime.get_object(Order, "S-SO002", OPS_MANAGER).status
        ),
        "order_note_survived_reindex": (
            runtime.get_object(OrderNote, "NOTE-N-001", OPS_MANAGER).body
        ),
        "odoo_states": {
            canonical_id: data.state(canonical_id)
            for canonical_id in sorted(data.orders)
        },
        "action_runs": len(runtime.list_action_runs()),
        "rejected_runs": len(runtime.list_action_runs("REJECTED")),
    }
    _section("DONE")
    print("  summary:", summary)
    return summary


if __name__ == "__main__":
    main()
