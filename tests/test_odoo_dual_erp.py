"""End-to-end acceptance tests against two real Odoo 19 databases.

These tests do NOT mock the ERP.  They require the local Odoo instance at
``http://localhost:8069`` with the ``erp_north`` / ``erp_south`` databases.
If Odoo is unreachable, the suite fails instead of skipping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from examples import odoo_dual_erp_demo as demo
from examples import odoo_orders_ontology as dom
from open_ontology import InvariantError, OntologyRuntime


@pytest.fixture(scope="session")
def odoo_data():
    """Provision both real databases and deterministic seed orders once."""
    return demo.setup_dual_odoo_erps(reset=True)


@pytest.fixture()
def env(odoo_data):
    """Fresh ontology runtime synced from a reset pair of real ERPs."""
    demo.reset_dual_odoo_orders(odoo_data)
    runtime = OntologyRuntime(dom.spec)
    demo.sync_from_both_odoos_to_ontology(runtime, odoo_data)
    yield runtime, odoo_data
    runtime.close()


def _pending(runtime, actor=None):
    return runtime.search_set(
        dom.Order, actor or demo.OPS_MANAGER, {"status": "pending"}
    )


# ---------------------------------------------------------------------------
# 1. Cross-ERP query, set algebra and RLS
# ---------------------------------------------------------------------------
def test_cross_erp_query_set_algebra_and_rls(env):
    runtime, _ = env

    pending = _pending(runtime)
    assert set(pending.ids) == {"N-SO001", "S-SO001", "S-SO002"}

    agg = runtime.aggregate(pending, group_by="region", sum_field="total_cents")
    by_region = {row.key: row.metrics for row in agg.rows}
    assert by_region["north"] == {"count": 1, "sum": 20000}
    assert by_region["south"] == {"count": 2, "sum": 6060000}

    summary = runtime.call_function(
        "customer_unshipped_summary",
        {"customer_id": "S-C01"},
        demo.OPS_MANAGER,
    )
    assert summary["unshipped_count"] == 2
    assert summary["unshipped_total_cents"] == 6060000
    assert summary["order_ids"] == ["S-SO001", "S-SO002"]

    north = _pending(runtime, demo.NORTH_DISPATCHER)
    south = _pending(runtime, demo.SOUTH_DISPATCHER)
    assert north.ids == ["N-SO001"]
    assert set(south.ids) == {"S-SO001", "S-SO002"}
    assert set(runtime.union(north, south).ids) == set(pending.ids)
    assert runtime.intersect(north, south).ids == []
    assert set(runtime.subtract(pending, north).ids) == {"S-SO001", "S-SO002"}

    customers = runtime.search_set(
        dom.Customer, demo.OPS_MANAGER, {"customer_id": "S-C01"}
    )
    orders = runtime.pivot(customers, dom.CUSTOMER_ORDERS, demo.OPS_MANAGER)
    assert set(orders.ids) == {"S-SO001", "S-SO002", "S-SO003"}
    back = runtime.pivot(
        orders, dom.CUSTOMER_ORDERS, demo.OPS_MANAGER, "reverse"
    )
    assert back.ids == ["S-C01"]


# ---------------------------------------------------------------------------
# 2. Shipped order cancellation is rejected and audited
# ---------------------------------------------------------------------------
def test_shipped_cancel_rejected_and_real_erp_untouched(env):
    runtime, data = env

    simulation = runtime.simulate_action(
        "cancel_order",
        {"order_id": "N-SO002", "reason": "too late"},
        demo.NORTH_DISPATCHER,
    )
    assert simulation.valid is False
    assert simulation.error_code == "SHIPPED_ORDER_CANNOT_BE_CANCELLED"

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "RUN-SHIPPED",
            "cancel_order",
            {"order_id": "N-SO002", "reason": "too late"},
            demo.NORTH_DISPATCHER,
        )
    assert excinfo.value.code == "SHIPPED_ORDER_CANNOT_BE_CANCELLED"

    run = runtime.get_action_run("RUN-SHIPPED")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "SHIPPED_ORDER_CANNOT_BE_CANCELLED"
    assert runtime.get_object(
        dom.Order, "N-SO002", demo.OPS_MANAGER
    ).status == "shipped"

    odoo_order = data.client("erp_north").order_by_ref("N-SO002")
    assert odoo_order["state"] == "sale"
    assert "STATUS:SHIPPED" in odoo_order["note"]


# ---------------------------------------------------------------------------
# 3. Pending order cancellation writes back to the real north ERP
# ---------------------------------------------------------------------------
def test_pending_cancel_writes_back_to_real_north(env):
    runtime, data = env
    bridge = demo.OdooWritebackBridge(runtime, data)
    client = data.client("erp_north")

    assert client.order_state("N-SO001") == "sale"
    run = runtime.submit_action(
        "RUN-CANCEL-N",
        "cancel_order",
        {"order_id": "N-SO001", "reason": "customer requested"},
        demo.NORTH_DISPATCHER,
    )
    assert run["status"] == "COMMITTED"
    assert runtime.get_object(
        dom.Order, "N-SO001", demo.OPS_MANAGER
    ).status == "cancelled"

    evidence = bridge.process_new_runs()
    assert len(evidence) == 1
    assert evidence[0]["order_id"] == "N-SO001"
    assert evidence[0]["db"] == "erp_north"
    assert evidence[0]["odoo_state_before"] == "sale"
    assert evidence[0]["odoo_state_after"] == "cancel"
    assert evidence[0]["verified"] is True

    # direct read from the real database
    assert client.order_state("N-SO001") == "cancel"


# ---------------------------------------------------------------------------
# 4. Time-machine revert restores the real north ERP
# ---------------------------------------------------------------------------
def test_revert_restores_real_north_odoo(env):
    runtime, data = env
    bridge = demo.OdooWritebackBridge(runtime, data)
    client = data.client("erp_north")

    runtime.submit_action(
        "RUN-CANCEL-REV",
        "cancel_order",
        {"order_id": "N-SO001", "reason": "customer requested"},
        demo.NORTH_DISPATCHER,
    )
    bridge.process_new_runs()
    assert client.order_state("N-SO001") == "cancel"

    revert = runtime.revert_action(
        "RUN-CANCEL-REV", "RUN-CANCEL-REV-BACK", demo.OPS_MANAGER
    )
    assert revert["status"] == "COMMITTED"
    assert runtime.get_action_run("RUN-CANCEL-REV")["status"] == "REVERTED"

    evidence = bridge.process_new_runs()
    assert len(evidence) == 1
    assert evidence[0]["order_id"] == "N-SO001"
    assert evidence[0]["odoo_state_before"] == "cancel"
    assert evidence[0]["odoo_state_after"] == "sale"
    assert client.order_state("N-SO001") == "sale"
    assert runtime.get_object(
        dom.Order, "N-SO001", demo.OPS_MANAGER
    ).status == "pending"


# ---------------------------------------------------------------------------
# 5. AI agent large cancellation -> manager approval -> real south ERP
# ---------------------------------------------------------------------------
def test_ai_large_cancel_requires_approval_then_writes_back(env):
    runtime, data = env
    bridge = demo.OdooWritebackBridge(runtime, data)
    client = data.client("erp_south")

    pending = runtime.submit_action(
        "RUN-AI-LARGE",
        "cancel_order",
        {
            "order_id": "S-SO002",
            "reason": "budget freeze",
            "large_order": True,
            "order_total_cents": 6_000_000,
        },
        demo.AI_AGENT,
    )
    assert pending["status"] == "PENDING_APPROVAL"
    assert bridge.process_new_runs() == []
    assert client.order_state("S-SO002") == "sale"
    assert runtime.get_object(
        dom.Order, "S-SO002", demo.OPS_MANAGER
    ).status == "pending"

    approved = runtime.approve_action("RUN-AI-LARGE", demo.OPS_MANAGER)
    assert approved["status"] == "COMMITTED"
    assert approved["approved_by"] == "ops-manager"

    evidence = bridge.process_new_runs()
    assert len(evidence) == 1
    assert evidence[0]["db"] == "erp_south"
    assert evidence[0]["odoo_state_before"] == "sale"
    assert evidence[0]["odoo_state_after"] == "cancel"
    assert client.order_state("S-SO002") == "cancel"
    assert runtime.get_object(
        dom.Order, "S-SO002", demo.OPS_MANAGER
    ).status == "cancelled"


# ---------------------------------------------------------------------------
# 6. Full reindex keeps overlay fields and owned OrderNote alive
# ---------------------------------------------------------------------------
def test_reindex_preserves_overlay_and_owned_notes(env):
    runtime, data = env

    runtime.submit_action(
        "RUN-ASSIGN",
        "assign_order",
        {
            "order_id": "N-SO001",
            "assignee": "north-ops",
            "ops_note": "priority customer",
        },
        demo.NORTH_DISPATCHER,
    )
    runtime.submit_action(
        "RUN-NOTE",
        "add_order_note",
        {
            "note_id": "NOTE-N-001",
            "order_id": "N-SO001",
            "body": "keep the original packaging",
        },
        demo.NORTH_DISPATCHER,
    )
    assert runtime.get_object(
        dom.Order, "N-SO001", demo.OPS_MANAGER
    ).assignee == "north-ops"
    assert runtime.get_object(
        dom.OrderNote, "NOTE-N-001", demo.OPS_MANAGER
    ).body == "keep the original packaging"

    old_total = runtime.get_object(
        dom.Order, "N-SO002", demo.OPS_MANAGER
    ).total_cents
    north = data.client("erp_north")
    old_price = north.get_order_line_price("N-SO002")

    try:
        north.set_order_line_price("N-SO002", old_price + 50.0)
        result = demo.sync_from_both_odoos_to_ontology(runtime, data)
        assert result["snapshot"]["overlays_reapplied"] == 1

        order = runtime.get_object(dom.Order, "N-SO001", demo.OPS_MANAGER)
        assert order.assignee == "north-ops"
        assert order.ops_note == "priority customer"

        note = runtime.get_object(
            dom.OrderNote, "NOTE-N-001", demo.OPS_MANAGER
        )
        assert note.body == "keep the original packaging"
        linked = runtime.get_linked(
            order, dom.ORDER_NOTES, dom.OrderNote, demo.OPS_MANAGER
        )
        assert [item.note_id for item in linked] == ["NOTE-N-001"]

        updated = runtime.get_object(
            dom.Order, "N-SO002", demo.OPS_MANAGER
        )
        assert updated.total_cents == old_total + 5 * 5000
    finally:
        north.set_order_line_price("N-SO002", old_price)


# ---------------------------------------------------------------------------
# 7. Empty reason is rejected, real ERP untouched
# ---------------------------------------------------------------------------
def test_empty_reason_rejected_and_real_erp_untouched(env):
    runtime, data = env
    client = data.client("erp_south")
    before = client.order_state("S-SO001")

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "RUN-EMPTY",
            "cancel_order",
            {"order_id": "S-SO001", "reason": "   "},
            demo.SOUTH_DISPATCHER,
        )
    assert excinfo.value.code == "EMPTY_REASON"

    run = runtime.get_action_run("RUN-EMPTY")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "EMPTY_REASON"
    assert client.order_state("S-SO001") == before == "sale"
    assert runtime.get_object(
        dom.Order, "S-SO001", demo.OPS_MANAGER
    ).status == "pending"


# ---------------------------------------------------------------------------
# 8. Already-cancelled order is refused without a second ERP write
# ---------------------------------------------------------------------------
def test_already_cancelled_is_rejected_without_second_write(env):
    runtime, data = env
    bridge = demo.OdooWritebackBridge(runtime, data)
    client = data.client("erp_south")

    runtime.submit_action(
        "RUN-CANCEL-1",
        "cancel_order",
        {"order_id": "S-SO001", "reason": "first cancellation"},
        demo.SOUTH_DISPATCHER,
    )
    bridge.process_new_runs()
    assert client.order_state("S-SO001") == "cancel"

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "RUN-CANCEL-2",
            "cancel_order",
            {"order_id": "S-SO001", "reason": "second cancellation"},
            demo.SOUTH_DISPATCHER,
        )
    assert excinfo.value.code == "ORDER_ALREADY_CANCELLED"
    assert runtime.get_action_run("RUN-CANCEL-2")["status"] == "REJECTED"
    assert bridge.process_new_runs() == []
    assert client.order_state("S-SO001") == "cancel"


# ---------------------------------------------------------------------------
# 9. Writeback bridge does not apply the same committed run twice
# ---------------------------------------------------------------------------
def test_writeback_bridge_is_idempotent(env):
    runtime, data = env
    bridge = demo.OdooWritebackBridge(runtime, data)
    client = data.client("erp_north")

    runtime.submit_action(
        "RUN-IDEMPOTENT",
        "cancel_order",
        {"order_id": "N-SO001", "reason": "once"},
        demo.NORTH_DISPATCHER,
    )
    first = bridge.process_new_runs()
    second = bridge.process_new_runs()
    assert len(first) == 1
    assert second == []
    assert client.order_state("N-SO001") == "cancel"


# ---------------------------------------------------------------------------
# 10. PIM / PSM separation: kernel + domain model stay pure
# ---------------------------------------------------------------------------
def test_pim_psm_separation():
    root = Path(__file__).resolve().parents[1]

    kernel_sources = [
        *root.glob("open_ontology/*.py"),
        root / "open_ontology" / "schema.sql",
    ]
    for path in kernel_sources:
        text = path.read_text(encoding="utf-8").lower()
        # No ERP / RPC vendor concepts anywhere in the kernel.
        for forbidden in ("odoo", "jsonrpc"):
            assert forbidden not in text, f"{forbidden!r} leaked into {path.name}"
        # ``erp`` 按词边界判定：内核合法 API ``state_fingerprint`` 含有
        # "erp" 子串，但它显然不是 ERP 概念。
        assert not re.search(r"(?<![a-z])erp(?![a-z])", text), (
            f"'erp' leaked into {path.name}"
        )
        # The pure HTTP boundaries are allowed to use the stdlib transport;
        # every other kernel module must stay fully transport-agnostic.
        if path.name not in ("http_server.py", "workshop.py", "harness_drivers.py"):
            for forbidden in ("urllib", "http://", "https://", "http.server"):
                assert forbidden not in text, f"{forbidden!r} leaked into {path.name}"

    domain_text = (
        root / "examples" / "odoo_orders_ontology.py"
    ).read_text(encoding="utf-8").lower()
    for forbidden in (
        "jsonrpc",
        "urllib",
        "http://",
        "https://",
        "import json",
        "import requests",
    ):
        assert forbidden not in domain_text

    adapter_text = (
        root / "examples" / "odoo_dual_erp_demo.py"
    ).read_text(encoding="utf-8")
    assert "OdooJSONRPC" in adapter_text
    assert "duplicate_database" in adapter_text
    assert "jsonrpc" in adapter_text.lower()
