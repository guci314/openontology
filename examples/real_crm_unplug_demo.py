"""真实开源 CRM + 真实 Odoo 19 → 吸干数据 → 拔掉插头独立运行。

Phase A : 从本机真实 BottleCRM（django-crm 1.44.0，SQLite 真实表）和
          真实 Odoo 19（erp_north）抽取数据装入 OpenOntology，
          叠加 overlay 治理属性并验证重刷存活。
Phase B : unplug_all_saas() 物理断开两个外部系统，并把全部对象类型
          晋升为 __owned__=True；随后用完全相同的 Action 独立跑完
          商机赢单转订单 → 建工单 → 退货退款护栏 → 大额审批 → 时光机撤回。

Run:  python3 -m examples.real_crm_unplug_demo
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_ontology import (  # noqa: E402
    GovernanceViolationError,
    InvariantError,
    OntologyRuntime,
)
from examples import crm_unplug_ontology as dom  # noqa: E402
from examples.odoo_dual_erp_demo import (  # noqa: E402
    DB_NORTH,
    OdooJSONRPC,
    ensure_database,
)

VENDOR_DIR = ROOT / "vendor_crm"
BOTTLECRM_DB = VENDOR_DIR / "bottlecrm.sqlite3"
BOTTLECRM_SRC = VENDOR_DIR / "bottlecrm_src"
WHEEL = VENDOR_DIR / "django_crm-1.44.0-py3-none-any.whl"

ODOO_PARTNER_REF = "UNPLUG-CUST-001"
ODOO_PARTNER_NAME = "Unplug Demo Customer"
ODOO_PRODUCT_CODE = "UNPLUG-WIDGET"

UNPLUGGABLE_TYPES = (
    "CustomerAccount",
    "SalesOpportunity",
    "SupportCase",
    "SalesOrder",
    "RefundSettlement",
)

# The pre-unplug declarations, captured so the demo (and tests) can be re-run
# in the same process without leaking class-level mutations.
_ORIGINAL_OWNED: dict[str, Any] = {}
_ORIGINAL_LINKS: dict[str, Any] = {}
_ORIGINAL_ACTION_AFFECTS: dict[str, bool] = {}


def capture_original_declarations() -> None:
    for type_name in UNPLUGGABLE_TYPES:
        cls = dom.spec.get_object_type(type_name)
        _ORIGINAL_OWNED.setdefault(type_name, cls.__owned__)
    for link_name, decl in dom.spec.link_types.items():
        _ORIGINAL_LINKS.setdefault(link_name, decl.owned)
    for action_name, action in dom.spec.actions.items():
        _ORIGINAL_ACTION_AFFECTS.setdefault(action_name, action.affects_source)


def reset_unplug_state() -> None:
    """Restore the pre-unplug sovereignty declarations (idempotent)."""
    capture_original_declarations()
    for type_name, owned in _ORIGINAL_OWNED.items():
        setattr(dom.spec.get_object_type(type_name), "__owned__", owned)
    for link_name, owned in _ORIGINAL_LINKS.items():
        decl = dom.spec.link_types[link_name]
        if decl.owned != owned:
            dom.spec.link_types[link_name] = decl.model_copy(
                update={"owned": owned}
            )
    for action_name, affects in _ORIGINAL_ACTION_AFFECTS.items():
        dom.spec.actions[action_name].affects_source = affects


# ---------------------------------------------------------------------------
# Real SaaS connectors (Phase A only; physically disabled in Phase B)
# ---------------------------------------------------------------------------
class SaasDisconnectedError(RuntimeError):
    """Raised when any code touches an external SaaS after unplug."""


class BottleCRMConnector:
    """Reads the REAL django-crm SQLite database built by its own migrations."""

    name = "BottleCRM (django-crm 1.44.0)"

    def __init__(self, db_path: Path = BOTTLECRM_DB) -> None:
        self.db_path = db_path
        self.connected = True
        self.reads = 0

    def _connect(self) -> sqlite3.Connection:
        if not self.connected:
            raise SaasDisconnectedError(f"{self.name} connector is disconnected")
        if not self.db_path.exists():
            raise SaasDisconnectedError(
                f"BottleCRM database missing at {self.db_path}; "
                "run vendor_crm/init_bottlecrm.py first"
            )
        self.reads += 1
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, industry, city, country, annual_revenue "
                "FROM accounts ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_opportunities(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.id, o.name, o.stage, o.amount, o.currency, "
                "o.closed_on, a.id AS account_id "
                "FROM opportunity o LEFT JOIN accounts a ON a.id = o.account_id "
                "ORDER BY o.name"
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_cases(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.id, c.name, c.status, c.priority, c.case_type, "
                "c.created_at, c.description, a.id AS account_id "
                'FROM "case" c LEFT JOIN accounts a ON a.id = c.account_id '
                "ORDER BY c.name"
            ).fetchall()
        return [dict(row) for row in rows]

    def table_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                "accounts": conn.execute(
                    "SELECT COUNT(*) FROM accounts"
                ).fetchone()[0],
                "opportunity": conn.execute(
                    "SELECT COUNT(*) FROM opportunity"
                ).fetchone()[0],
                "case": conn.execute(
                    'SELECT COUNT(*) FROM "case"'
                ).fetchone()[0],
            }

    def disconnect(self) -> None:
        self.connected = False


class OdooERPConnector:
    """Reads the REAL Odoo 19 sale.order table over JSON-RPC."""

    name = "Odoo 19 (erp_north)"

    def __init__(self, client: OdooJSONRPC | None = None) -> None:
        self.client = client
        self.connected = True
        self.reads = 0

    def _client(self) -> OdooJSONRPC:
        if not self.connected:
            raise SaasDisconnectedError(f"{self.name} connector is disconnected")
        if self.client is None:
            raise SaasDisconnectedError(f"{self.name} client is not initialised")
        self.reads += 1
        return self.client

    def fetch_orders(self) -> list[dict[str, Any]]:
        client = self._client()
        return client.search_read(
            "sale.order",
            [("client_order_ref", "=like", "UNPLUG-%")],
            fields=[
                "id",
                "name",
                "client_order_ref",
                "state",
                "amount_total",
                "date_order",
            ],
        )

    def disconnect(self) -> None:
        self.connected = False
        self.client = None


# ---------------------------------------------------------------------------
# Real Odoo seeding
# ---------------------------------------------------------------------------
def _get_or_create_partner(client: OdooJSONRPC) -> int:
    ids = client.search("res.partner", [("ref", "=", ODOO_PARTNER_REF)])
    values: dict[str, Any] = {
        "name": ODOO_PARTNER_NAME,
        "ref": ODOO_PARTNER_REF,
        "customer_rank": 1,
        "street": "88 Zhangjiang Road",
        "city": "Shanghai",
        "email": "unplug@demo.example.com",
        "phone": "+86 21 5555 0200",
        "active": True,
    }
    country = client.search("res.country", [("code", "=", "CN")])
    if country:
        values["country_id"] = country[0]
    if ids:
        client.write("res.partner", [ids[0]], values)
        return ids[0]
    return client.create("res.partner", values)


def _get_or_create_product(client: OdooJSONRPC) -> int:
    ids = client.search(
        "product.product", [("default_code", "=", ODOO_PRODUCT_CODE)]
    )
    values = {
        "name": "Unplug Demo Widget",
        "default_code": ODOO_PRODUCT_CODE,
        "type": "consu",
        "is_storable": True,
        "list_price": 1200.0,
        "sale_ok": True,
        "active": True,
        "taxes_id": [(6, 0, [])],
    }
    if ids:
        client.write("product.product", [ids[0]], values)
        return ids[0]
    return client.create("product.product", values)


def _archive_old_orders(client: OdooJSONRPC) -> None:
    rows = client.search_read(
        "sale.order",
        [("client_order_ref", "=like", "UNPLUG-%")],
        fields=["id", "client_order_ref"],
    )
    for row in rows:
        client.write(
            "sale.order",
            [int(row["id"])],
            {
                "client_order_ref": (
                    f"__stale__{row['id']}__{row['client_order_ref']}"
                )
            },
        )


def seed_real_odoo_orders(client: OdooJSONRPC) -> list[dict[str, Any]]:
    partner_id = _get_or_create_partner(client)
    product_id = _get_or_create_product(client)
    _archive_old_orders(client)

    created: list[dict[str, Any]] = []
    for order_ref, qty, price in (
        ("UNPLUG-SO-001", 1, 1200.0),
        ("UNPLUG-SO-002", 2, 1200.0),
    ):
        odoo_id = client.create(
            "sale.order",
            {
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product_id,
                            "product_uom_qty": qty,
                            "price_unit": price,
                        },
                    )
                ],
            },
        )
        client.execute_kw("sale.order", "action_confirm", [[odoo_id]])
        created.append(
            client.read(
                "sale.order",
                [odoo_id],
                [
                    "id",
                    "name",
                    "client_order_ref",
                    "state",
                    "amount_total",
                    "date_order",
                ],
            )[0]
        )
    return created


# ---------------------------------------------------------------------------
# Phase A: absorb both real SaaS systems
# ---------------------------------------------------------------------------
@dataclass
class AbsorbResult:
    snapshot: dict[str, Any]
    order_refs: list[str] = field(default_factory=list)


def _cents(value: Any) -> int:
    if value is None:
        return 0
    return int(round(float(Decimal(str(value))) * 100))


def absorb_from_real_saas(
    runtime: OntologyRuntime,
    bottlecrm: BottleCRMConnector,
    erp: OdooERPConnector,
) -> AbsorbResult:
    accounts_raw = bottlecrm.fetch_accounts()
    opportunities_raw = bottlecrm.fetch_opportunities()
    cases_raw = bottlecrm.fetch_cases()
    orders_raw = erp.fetch_orders()

    accounts: list[dict[str, Any]] = []
    account_ids: set[str] = set()
    for row in accounts_raw:
        account = dom.CustomerAccount(
            account_id=str(row["id"]),
            name=row["name"] or "",
            industry=row.get("industry") or "",
            city=row.get("city") or "",
            country=row.get("country") or "",
            annual_revenue_cents=_cents(row.get("annual_revenue")),
        )
        accounts.append(account.source_properties())
        account_ids.add(account.account_id)

    opportunities: list[dict[str, Any]] = []
    opportunity_links: list[tuple[str, str]] = []
    for row in opportunities_raw:
        account_id = row.get("account_id")
        if account_id not in account_ids:
            continue
        opportunity = dom.SalesOpportunity(
            opportunity_id=str(row["id"]),
            name=row["name"] or "",
            account_id=str(account_id),
            stage=row.get("stage") or "PROSPECTING",
            amount_cents=_cents(row.get("amount")),
            currency=row.get("currency") or "USD",
            expected_close_date=(
                str(row["closed_on"]) if row.get("closed_on") else None
            ),
        )
        opportunities.append(opportunity.source_properties())
        opportunity_links.append((str(account_id), opportunity.opportunity_id))

    cases: list[dict[str, Any]] = []
    case_links: list[tuple[str, str]] = []
    for row in cases_raw:
        account_id = row.get("account_id")
        if account_id not in account_ids:
            continue
        opened_at = row.get("created_at")
        case = dom.SupportCase(
            case_id=str(row["id"]),
            subject=row["name"] or "",
            account_id=str(account_id),
            status=row.get("status") or "New",
            priority=row.get("priority") or "Medium",
            case_type=row.get("case_type") or "Problem",
            opened_at=str(opened_at) if opened_at else None,
            description=row.get("description") or "",
        )
        cases.append(case.source_properties())
        case_links.append((str(account_id), case.case_id))

    # Real Odoo orders attach to the first real CRM account.
    primary_account = accounts[0]["account_id"] if accounts else ""
    orders: list[dict[str, Any]] = []
    order_links: list[tuple[str, str]] = []
    order_refs: list[str] = []
    for row in orders_raw:
        order_ref = row["client_order_ref"]
        order = dom.SalesOrder(
            order_id=order_ref,
            order_ref=order_ref,
            account_id=primary_account,
            status="confirmed",
            total_cents=_cents(row["amount_total"]),
            currency="USD",
            ordered_on=(str(row.get("date_order") or "")[:10] or None),
        )
        orders.append(order.source_properties())
        order_links.append((primary_account, order_ref))
        order_refs.append(order_ref)

    snapshot = runtime.load_snapshot(
        {
            "CustomerAccount": accounts,
            "SalesOpportunity": opportunities,
            "SupportCase": cases,
            "SalesOrder": orders,
        },
        links={
            "account_opportunities": opportunity_links,
            "account_cases": case_links,
            "account_orders": order_links,
        },
    )
    return AbsorbResult(snapshot=snapshot, order_refs=order_refs)


# ---------------------------------------------------------------------------
# Overlay governance (Phase A)
# ---------------------------------------------------------------------------
def apply_overlay_governance(runtime: OntologyRuntime) -> dict[str, Any]:
    cases = sorted(
        runtime.query_objects(dom.SupportCase, dom.AI_AGENT),
        key=lambda case: case.case_id,
    )
    triage_specs = [
        (85, "STANDARD", "退款前需人工复核：欺诈分过高，建议拒绝"),
        (35, "SILVER", "标准退货流程：核对物流单后小额退款"),
        (15, "GOLD", "VIP 客户：优先换货并赠送补偿券"),
    ]
    for index, case in enumerate(cases):
        fraud, tier, plan = triage_specs[index % len(triage_specs)]
        runtime.submit_action(
            f"TRIAGE-{case.case_id[:8]}",
            "ai_triage_crm_case",
            {
                "case_id": case.case_id,
                "fraud_score": fraud,
                "vip_tier": tier,
                "ai_resolution_plan": plan,
            },
            dom.AI_AGENT,
        )
    return {"triage_runs": len(cases)}


def snapshot_overlay(runtime: OntologyRuntime) -> dict[str, Any]:
    rows = runtime.connection.execute(
        "SELECT object_type, object_id, patch_json FROM ontology_overlay "
        "ORDER BY object_type, object_id"
    ).fetchall()
    return {
        f"{row['object_type']}:{row['object_id']}": json.loads(row["patch_json"])
        for row in rows
    }


# ---------------------------------------------------------------------------
# Phase B: unplug
# ---------------------------------------------------------------------------
@dataclass
class UnplugResult:
    promoted_types: list[str]
    promoted_links: list[str]
    demoted_actions: list[str]
    disconnected: list[str]
    overlay_preserved: dict[str, Any]
    blocked_calls: list[str]
    snapshot_guard_rejected: list[str]
    counts: dict[str, int]


def unplug_all_saas(
    runtime: OntologyRuntime,
    bottlecrm: BottleCRMConnector,
    erp: OdooERPConnector,
) -> UnplugResult:
    """Physically cut both SaaS connectors and promote every type to owned."""
    overlay_before = snapshot_overlay(runtime)
    counts_before = {
        type_name: runtime.count_objects(type_name)
        for type_name in UNPLUGGABLE_TYPES
    }

    # 1) cut the physical connections
    bottlecrm.disconnect()
    erp.disconnect()

    # 2) promote every absorbed object type to fully ontology-owned
    for type_name in UNPLUGGABLE_TYPES:
        cls = dom.spec.get_object_type(type_name)
        setattr(cls, "__owned__", True)

    # 3) promote every relationship to ontology-owned: there is no longer an
    #    external system that could own an edge
    promoted_links: list[str] = []
    for link_name, decl in list(dom.spec.link_types.items()):
        if not decl.owned:
            dom.spec.link_types[link_name] = decl.model_copy(
                update={"owned": True}
            )
            promoted_links.append(link_name)

    # 4) re-declare every action that used to write to a source system.
    #    The action code is untouched; only the sovereignty declaration moves,
    #    because after the unplug there is no source left to affect.
    demoted_actions: list[str] = []
    for action in dom.spec.actions.values():
        if action.affects_source:
            action.affects_source = False
            demoted_actions.append(action.name)

    # 5) prove the connectors are dead
    blocked: list[str] = []
    for label, call in (
        ("bottlecrm.fetch_accounts", bottlecrm.fetch_accounts),
        ("bottlecrm.fetch_opportunities", bottlecrm.fetch_opportunities),
        ("bottlecrm.fetch_cases", bottlecrm.fetch_cases),
        ("erp.fetch_orders", erp.fetch_orders),
    ):
        try:
            call()
        except SaasDisconnectedError:
            blocked.append(label)

    # 6) prove that an external snapshot can no longer overwrite owned types
    guard_rejected: list[str] = []
    for type_name in UNPLUGGABLE_TYPES:
        try:
            runtime.load_snapshot({type_name: []})
        except GovernanceViolationError:
            guard_rejected.append(type_name)

    overlay_after = snapshot_overlay(runtime)
    counts_after = {
        type_name: runtime.count_objects(type_name)
        for type_name in UNPLUGGABLE_TYPES
    }
    if counts_before != counts_after:
        raise RuntimeError(
            f"unplug lost ontology objects: {counts_before} -> {counts_after}"
        )
    if overlay_before != overlay_after:
        raise RuntimeError("unplug lost overlay patches")

    return UnplugResult(
        promoted_types=list(UNPLUGGABLE_TYPES),
        promoted_links=promoted_links,
        demoted_actions=demoted_actions,
        disconnected=[bottlecrm.name, erp.name],
        overlay_preserved=overlay_after,
        blocked_calls=blocked,
        snapshot_guard_rejected=guard_rejected,
        counts=counts_after,
    )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def main() -> dict[str, Any]:
    capture_original_declarations()
    _section("0. 真实开源 CRM 与真实 Odoo 19 环境检查")
    if not WHEEL.exists():
        raise SystemExit(
            f"missing {WHEEL}; run: pip download django-crm==1.44.0 "
            "--no-deps -d vendor_crm"
        )
    if not BOTTLECRM_SRC.exists():
        raise SystemExit(f"missing extracted source at {BOTTLECRM_SRC}")
    if not BOTTLECRM_DB.exists():
        raise SystemExit(f"missing {BOTTLECRM_DB}; run vendor_crm/init_bottlecrm.py")

    bottlecrm = BottleCRMConnector()
    counts = bottlecrm.table_counts()
    print(f"  wheel      : {WHEEL.name} ({WHEEL.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  source dir : {BOTTLECRM_SRC}")
    print(
        f"  database   : {BOTTLECRM_DB.name}"
        f" ({BOTTLECRM_DB.stat().st_size / 1024:.0f} KB)"
    )
    print(f"  real tables: {counts}")

    odoo_client, _ = ensure_database(DB_NORTH)
    seeded = seed_real_odoo_orders(odoo_client)
    print(
        "  odoo orders: "
        + ", ".join(
            f"{row['client_order_ref']}={row['name']}({row['state']})"
            for row in seeded
        )
    )
    erp = OdooERPConnector(odoo_client)
    runtime = dom.new_runtime()

    _section("1. Phase A — 吸干两个真实开源 SaaS")
    absorb = absorb_from_real_saas(runtime, bottlecrm, erp)
    print(f"  snapshot: {absorb.snapshot}")
    print(
        f"  counts  : accounts={runtime.count_objects('CustomerAccount')}"
        f" opportunities={runtime.count_objects('SalesOpportunity')}"
        f" cases={runtime.count_objects('SupportCase')}"
        f" orders={runtime.count_objects('SalesOrder')}"
    )
    print(f"  bottlecrm reads={bottlecrm.reads}  odoo reads={erp.reads}")

    _section("2. Phase A — AI 分诊写入 overlay 治理属性")
    triage = apply_overlay_governance(runtime)
    cases = sorted(
        runtime.query_objects(dom.SupportCase, dom.AI_AGENT),
        key=lambda case: case.case_id,
    )
    for case in cases:
        print(
            f"  case {case.case_id[:8]} {case.subject[:20]:<22}"
            f" fraud={case.fraud_score:<3} vip={case.vip_tier:<8}"
            f" plan={case.ai_resolution_plan}"
        )
    overlay_before = snapshot_overlay(runtime)
    print(f"  triage runs={triage['triage_runs']} overlay rows={len(overlay_before)}")

    _section("3. Phase A — 外部 SaaS 全量重刷后 overlay 存活")
    absorb2 = absorb_from_real_saas(runtime, bottlecrm, erp)
    overlay_after_resync = snapshot_overlay(runtime)
    print(f"  resync snapshot: {absorb2.snapshot}")
    print(f"  overlay rows after resync={len(overlay_after_resync)}")
    preserved = overlay_before == overlay_after_resync
    print(f"  overlay identical: {preserved}")
    for case in sorted(
        runtime.query_objects(dom.SupportCase, dom.AI_AGENT),
        key=lambda case: case.case_id,
    ):
        print(
            f"  case {case.case_id[:8]} fraud={case.fraud_score}"
            f" vip={case.vip_tier}"
        )
    health_a = runtime.call_function(
        "portfolio_health_snapshot", {}, dom.OPS_MANAGER
    )
    print(f"  portfolio health: {health_a}")

    _section("4. Phase B — 一键拔掉 SaaS 插头 unplug_all_saas()")
    unplug = unplug_all_saas(runtime, bottlecrm, erp)
    print(f"  promoted to owned : {unplug.promoted_types}")
    print(f"  promoted links    : {unplug.promoted_links}")
    print(f"  actions re-declared: {unplug.demoted_actions}")
    print(f"  disconnected      : {unplug.disconnected}")
    print(f"  blocked SaaS calls: {unplug.blocked_calls}")
    print(f"  snapshot guard    : {unplug.snapshot_guard_rejected}")
    print(f"  preserved counts  : {unplug.counts}")
    print(f"  preserved overlay : {len(unplug.overlay_preserved)} rows")

    _section("5. Phase B — 零外部 SaaS 下商机赢单转订单")
    opportunities = sorted(
        runtime.query_objects(dom.SalesOpportunity, dom.AI_AGENT),
        key=lambda opp: opp.opportunity_id,
    )
    target_opp = next(
        opp
        for opp in opportunities
        if opp.stage not in ("CLOSED_WON", "CLOSED_LOST")
    )
    win_run = runtime.submit_action(
        "OFFLINE-WIN",
        "win_opportunity_and_create_order",
        {
            "opportunity_id": target_opp.opportunity_id,
            "order_ref": "SO-OFFLINE-001",
            "fulfillment_note": "created with zero external SaaS",
        },
        dom.AI_AGENT,
    )
    print(f"  run={win_run['run_id']} status={win_run['status']}")
    won = runtime.get_object(
        dom.SalesOpportunity, target_opp.opportunity_id, dom.OPS_MANAGER
    )
    offline_order = runtime.get_object(
        dom.SalesOrder, "SO-OFFLINE-001", dom.OPS_MANAGER
    )
    print(
        f"  opportunity {won.opportunity_id[:8]} stage={won.stage}"
        f" win_probability={won.win_probability}"
    )
    print(
        f"  new order {offline_order.order_id} status={offline_order.status}"
        f" total={_money(offline_order.total_cents)}"
    )
    print(f"  orders now={runtime.count_objects('SalesOrder')}")

    _section("6. Phase B — 原生建工单 + 小额 AI 自动退款")
    small_order = dom.SalesOrder(
        order_id="SO-OFFLINE-SMALL",
        order_ref="SO-OFFLINE-SMALL",
        account_id=offline_order.account_id,
        status="confirmed",
        total_cents=8_000,  # $80 — below the $100 approval threshold
        currency="USD",
        ordered_on=date.today().isoformat(),
    )
    runtime.put_object(small_order)
    print(
        f"  native small order {small_order.order_id}"
        f" total={_money(small_order.total_cents)}"
    )

    open_run = runtime.submit_action(
        "OFFLINE-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-OFFLINE-001",
            "account_id": offline_order.account_id,
            "subject": "离线新单到货包装破损",
            "priority": "High",
            "case_type": "Problem",
            "description": "created after unplug, no external SaaS involved",
            "order_id": "SO-OFFLINE-SMALL",
        },
        dom.SUPPORT_AGENT,
    )
    print(f"  open case run={open_run['run_id']} status={open_run['status']}")

    small_refund = runtime.submit_action(
        "OFFLINE-REFUND-SMALL",
        "resolve_case_with_refund",
        {
            "case_id": "CASE-OFFLINE-001",
            "order_id": "SO-OFFLINE-SMALL",
            "reason": "packaging damage",
        },
        dom.AI_AGENT,
    )
    print(
        f"  small refund run={small_refund['run_id']}"
        f" status={small_refund['status']}"
        f" approved_by={small_refund['approved_by']}"
    )
    refunded_order = runtime.get_object(
        dom.SalesOrder, "SO-OFFLINE-SMALL", dom.OPS_MANAGER
    )
    print(
        f"  order status={refunded_order.status}"
        f" refunded={_money(refunded_order.refunded_cents)}"
    )

    _section("7. Phase B — 大额退款触发 PENDING_APPROVAL + 经理放行")
    runtime.submit_action(
        "OFFLINE-BIG-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-OFFLINE-BIG",
            "account_id": offline_order.account_id,
            "subject": "大额订单退货申请",
            "priority": "Urgent",
            "order_id": "SO-OFFLINE-001",
        },
        dom.SUPPORT_AGENT,
    )
    pending = runtime.submit_action(
        "OFFLINE-REFUND-BIG",
        "resolve_case_with_refund",
        {
            "case_id": "CASE-OFFLINE-BIG",
            "order_id": "SO-OFFLINE-001",
            "reason": "customer changed mind",
        },
        dom.AI_AGENT,
    )
    print(
        f"  big refund submit status={pending['status']}"
        f" amount={_money(offline_order.total_cents)}"
    )
    approved = runtime.approve_action("OFFLINE-REFUND-BIG", dom.OPS_MANAGER)
    print(f"  approved by={approved['approved_by']} status={approved['status']}")
    big_order = runtime.get_object(
        dom.SalesOrder, "SO-OFFLINE-001", dom.OPS_MANAGER
    )
    print(
        f"  big order status={big_order.status}"
        f" refunded={_money(big_order.refunded_cents)}"
    )

    _section("8. Phase B — 护栏拦截：超期退货 / 高欺诈分")
    stale_order = dom.SalesOrder(
        order_id="SO-STALE-001",
        order_ref="SO-STALE-001",
        account_id=offline_order.account_id,
        status="confirmed",
        total_cents=5_000,
        currency="USD",
        ordered_on=(date.today() - timedelta(days=45)).isoformat(),
    )
    runtime.put_object(stale_order)
    runtime.submit_action(
        "OFFLINE-STALE-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-STALE",
            "account_id": offline_order.account_id,
            "subject": "45 天前订单退货",
            "order_id": "SO-STALE-001",
        },
        dom.SUPPORT_AGENT,
    )
    try:
        runtime.submit_action(
            "OFFLINE-REFUND-STALE",
            "resolve_case_with_refund",
            {"case_id": "CASE-STALE", "order_id": "SO-STALE-001"},
            dom.AI_AGENT,
        )
    except InvariantError as exc:
        print(f"  stale refund blocked: code={exc.code}")
    print(
        "  audit status="
        f"{runtime.get_action_run('OFFLINE-REFUND-STALE')['status']}"
    )

    runtime.submit_action(
        "OFFLINE-FRAUD-CASE",
        "open_customer_case",
        {
            "case_id": "CASE-FRAUD",
            "account_id": offline_order.account_id,
            "subject": "疑似欺诈退款申请",
        },
        dom.SUPPORT_AGENT,
    )
    runtime.submit_action(
        "OFFLINE-FRAUD-TRIAGE",
        "ai_triage_crm_case",
        {"case_id": "CASE-FRAUD", "fraud_score": 92},
        dom.AI_AGENT,
    )
    runtime.put_object(
        dom.SalesOrder(
            order_id="SO-FRAUD-001",
            order_ref="SO-FRAUD-001",
            account_id=offline_order.account_id,
            status="confirmed",
            total_cents=4_000,
            currency="USD",
            ordered_on=date.today().isoformat(),
        )
    )
    try:
        runtime.submit_action(
            "OFFLINE-REFUND-FRAUD",
            "resolve_case_with_refund",
            {"case_id": "CASE-FRAUD", "order_id": "SO-FRAUD-001"},
            dom.AI_AGENT,
        )
    except InvariantError as exc:
        print(f"  high fraud blocked: code={exc.code}")
    print(
        "  audit status="
        f"{runtime.get_action_run('OFFLINE-REFUND-FRAUD')['status']}"
    )

    _section("9. Phase B — 时光机一键撤回退款")
    revert = runtime.revert_action(
        "OFFLINE-REFUND-SMALL", "OFFLINE-REFUND-SMALL-REV", dom.OPS_MANAGER
    )
    print(f"  revert run={revert['run_id']} status={revert['status']}")
    print(
        "  original run status="
        f"{runtime.get_action_run('OFFLINE-REFUND-SMALL')['status']}"
    )
    restored_order = runtime.get_object(
        dom.SalesOrder, "SO-OFFLINE-SMALL", dom.OPS_MANAGER
    )
    restored_case = runtime.get_object(
        dom.SupportCase, "CASE-OFFLINE-001", dom.OPS_MANAGER
    )
    restored_account = runtime.get_object(
        dom.CustomerAccount, offline_order.account_id, dom.OPS_MANAGER
    )
    print(
        f"  order status={restored_order.status}"
        f" refunded={_money(restored_order.refunded_cents)}"
    )
    print(f"  case status={restored_case.status}")
    print(f"  account ltv={_money(restored_account.ltv_cents)}")

    _section("10. 最终状态：零外部 SaaS + 全部业务对象仍在 Ontology 内")
    health = runtime.call_function(
        "portfolio_health_snapshot", {}, dom.OPS_MANAGER
    )
    print(f"  portfolio health: {health}")
    print(
        "  SaaS connectivity: bottlecrm="
        f"{'connected' if bottlecrm.connected else 'DISCONNECTED'}"
        f" odoo={'connected' if erp.connected else 'DISCONNECTED'}"
    )
    runs = runtime.list_action_runs()
    print(f"  action runs: {len(runs)}")
    for row in runs:
        print(
            f"    {row['run_id']:<26} {row['action_type']:<34}"
            f" {row['status']:<17} {row['error_code'] or ''}"
        )

    summary = {
        "real_saas": {
            "bottlecrm_tables": counts,
            "bottlecrm_db_bytes": BOTTLECRM_DB.stat().st_size,
            "odoo_orders": [
                {
                    "ref": row["client_order_ref"],
                    "name": row["name"],
                    "state": row["state"],
                    "total": row["amount_total"],
                }
                for row in seeded
            ],
        },
        "phase_a": {
            "snapshot": absorb.snapshot,
            "overlay_rows": len(overlay_before),
            "overlay_survived_resync": preserved,
        },
        "phase_b": {
            "promoted": unplug.promoted_types,
            "disconnected": unplug.disconnected,
            "blocked_calls": unplug.blocked_calls,
            "snapshot_guard_rejected": unplug.snapshot_guard_rejected,
            "orders": runtime.count_objects("SalesOrder"),
            "cases": runtime.count_objects("SupportCase"),
            "refunds": runtime.count_objects("RefundSettlement"),
            "rejected_runs": [
                row["run_id"] for row in runtime.list_action_runs("REJECTED")
            ],
        },
        "final": {
            "portfolio_health": health,
            "small_order_status": restored_order.status,
            "case_status": restored_case.status,
            "account_ltv_cents": restored_account.ltv_cents,
        },
    }
    _section("DONE")
    print("  summary:", json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    main()
