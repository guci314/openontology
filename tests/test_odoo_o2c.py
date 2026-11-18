"""Real Odoo 19 O2C end-to-end acceptance tests (TK-ONTO-006)."""

from __future__ import annotations

from pathlib import Path

import pytest

from examples import o2c_ontology as o2c
from examples import odoo_o2c_adapter as adapter_mod
from open_ontology import EditBatch, InvariantError, OntologyRuntime


@pytest.fixture()
def o2c_env():
    runtime = OntologyRuntime(o2c.spec)
    adapter = adapter_mod.OdooO2CAdapter(runtime)
    data = adapter.seed_odoo()
    adapter.sync_from_odoo()
    yield runtime, adapter, data
    runtime.close()


def _order(runtime, order_id):
    return runtime.get_object(o2c.SalesOrder, order_id, o2c.FINANCE_DIRECTOR)


def _customer(runtime):
    return runtime.get_object(
        o2c.Customer, "O2C-CUST-001", o2c.FINANCE_DIRECTOR
    )


def _ar_balance(runtime):
    return runtime.get_object(
        o2c.ReceivableBalance, "AR-O2C-CUST-001", o2c.FINANCE_DIRECTOR
    )


def _confirm(runtime, adapter, order_id="O2C-SO-001", run_id="CONFIRM", actor=o2c.SALES_REP):
    run = runtime.submit_action(
        run_id,
        "confirm_sales_order",
        {"order": order_id},
        actor,
    )
    if run["status"] == "COMMITTED":
        adapter.process_new_runs()
    return run


def _ship(runtime, adapter, order_id="O2C-SO-001", run_id="SHIP"):
    run = runtime.submit_action(
        run_id, "ship_order_delivery", {"order": order_id}, o2c.WAREHOUSE
    )
    if run["status"] == "COMMITTED":
        adapter.process_new_runs()
    return run


def _invoice(runtime, adapter, order_id="O2C-SO-001", run_id="INVOICE"):
    run = runtime.submit_action(
        run_id,
        "issue_and_post_invoice",
        {"order": order_id},
        o2c.FINANCE_DIRECTOR,
    )
    if run["status"] == "COMMITTED":
        adapter.process_new_runs()
    return run


def _pay(runtime, adapter, order_id="O2C-SO-001", amount=2_000_000, run_id="PAY"):
    run = runtime.submit_action(
        run_id,
        "record_customer_payment",
        {"order": order_id, "payment_cents": amount},
        o2c.FINANCE_DIRECTOR,
    )
    if run["status"] == "COMMITTED":
        adapter.process_new_runs()
    return run


# ---------------------------------------------------------------------------
# 1. PIM / PSM physical separation
# ---------------------------------------------------------------------------
def test_pim_psm_separation():
    root = Path(__file__).resolve().parents[1]
    ontology_text = (
        root / "examples" / "o2c_ontology.py"
    ).read_text(encoding="utf-8").lower()
    for forbidden in (
        "odoo",
        "jsonrpc",
        "urllib",
        "http://",
        "https://",
        "res.partner",
        "stock.picking",
        "account.move",
    ):
        assert forbidden not in ontology_text, forbidden

    adapter_text = (
        root / "examples" / "odoo_o2c_adapter.py"
    ).read_text(encoding="utf-8")
    assert "OdooO2CAdapter" in adapter_text
    assert "sale.order" in adapter_text
    assert "stock.picking" in adapter_text
    assert "account.payment.register" in adapter_text


# ---------------------------------------------------------------------------
# 2. Seed creates two real draft quotations and a synced ontology
# ---------------------------------------------------------------------------
def test_seed_and_sync_real_draft_orders(o2c_env):
    runtime, adapter, data = o2c_env
    assert set(data.orders) == {"O2C-SO-001", "O2C-SO-002"}

    for canonical_id, ref in data.orders.items():
        row = adapter.order_row(canonical_id)
        assert row["id"] == ref.odoo_id
        assert row["state"] == "draft"
        order = _order(runtime, canonical_id)
        assert order.status == "draft"
        assert order.total_cents in (2_000_000, 12_000_000)

    customer = _customer(runtime)
    assert customer.credit_limit_cents == 10_000_000
    assert _ar_balance(runtime).ar_balance_cents == 0


# ---------------------------------------------------------------------------
# 3. Confirm translates to sale.order.action_confirm + real picking
# ---------------------------------------------------------------------------
def test_confirm_writes_back_real_sale_order_and_picking(o2c_env):
    runtime, adapter, _ = o2c_env
    run = _confirm(runtime, adapter)
    assert run["status"] == "COMMITTED"

    row = adapter.order_row("O2C-SO-001")
    assert row["state"] == "sale"
    picking = adapter.delivery_row("O2C-SO-001")
    assert picking is not None
    assert picking["state"] != "cancel"

    assert _order(runtime, "O2C-SO-001").status == "confirmed"
    delivery = runtime.get_object(
        o2c.Delivery, "DEL-O2C-SO-001", o2c.FINANCE_DIRECTOR
    )
    assert delivery.status == "pending"


# ---------------------------------------------------------------------------
# 4. Ship translates to stock.picking.button_validate
# ---------------------------------------------------------------------------
def test_ship_validates_real_picking(o2c_env):
    runtime, adapter, _ = o2c_env
    _confirm(runtime, adapter)
    run = _ship(runtime, adapter)
    assert run["status"] == "COMMITTED"

    picking = adapter.delivery_row("O2C-SO-001")
    assert picking is not None
    assert picking["state"] == "done"
    assert _order(runtime, "O2C-SO-001").status == "shipped"
    delivery = runtime.get_object(
        o2c.Delivery, "DEL-O2C-SO-001", o2c.FINANCE_DIRECTOR
    )
    assert delivery.status == "shipped"


# ---------------------------------------------------------------------------
# 5. Invoice before shipping is rejected; no fake invoice in Odoo
# ---------------------------------------------------------------------------
def test_invoice_before_shipping_rejected_no_fake_invoice(o2c_env):
    runtime, adapter, _ = o2c_env
    _confirm(runtime, adapter)

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "EARLY-INVOICE",
            "issue_and_post_invoice",
            {"order": "O2C-SO-001"},
            o2c.FINANCE_DIRECTOR,
        )
    assert excinfo.value.code == "CANNOT_INVOICE_UNSHIPPED_ORDER"
    run = runtime.get_action_run("EARLY-INVOICE")
    assert run["status"] == "REJECTED"
    assert run["error_code"] == "CANNOT_INVOICE_UNSHIPPED_ORDER"

    assert adapter.invoice_row("O2C-SO-001") is None
    assert adapter.order_row("O2C-SO-001")["state"] == "sale"
    assert adapter.delivery_row("O2C-SO-001")["state"] != "done"


# ---------------------------------------------------------------------------
# 6. Full flow: confirm -> ship -> invoice -> payment
# ---------------------------------------------------------------------------
def test_full_o2c_flow_reaches_real_paid_invoice(o2c_env):
    runtime, adapter, _ = o2c_env
    _confirm(runtime, adapter)
    _ship(runtime, adapter)
    invoice_run = _invoice(runtime, adapter)
    assert "ReceivableBalance:AR-O2C-CUST-001" in invoice_run["read_versions"]
    assert "Customer:O2C-CUST-001" not in invoice_run["read_versions"]
    _pay(runtime, adapter)

    docs = adapter.odoo_documents("O2C-SO-001")
    assert docs["order_state"] == "sale"
    assert docs["delivery_state"] == "done"
    assert docs["invoice_state"] == "posted"
    assert docs["payment_state"] in ("paid", "in_payment")
    assert docs["amount_residual"] == 0.0

    assert _order(runtime, "O2C-SO-001").status == "paid"
    invoice = runtime.get_object(
        o2c.Invoice, "INV-O2C-SO-001", o2c.FINANCE_DIRECTOR
    )
    assert invoice.status == "paid"
    assert _ar_balance(runtime).ar_balance_cents == 0
    inv_voucher = runtime.get_object(
        o2c.ReceivableVoucher, "ARV-INV-O2C-SO-001", o2c.FINANCE_DIRECTOR
    )
    pay_voucher = runtime.get_object(
        o2c.ReceivableVoucher, "ARV-PAY-O2C-SO-001", o2c.FINANCE_DIRECTOR
    )
    assert inv_voucher.delta_cents == 2_000_000
    assert pay_voucher.delta_cents == -2_000_000

    adapter.sync_from_odoo()
    assert _ar_balance(runtime).ar_balance_cents == 0
    assert runtime.get_object(
        o2c.ReceivableVoucher, "ARV-INV-O2C-SO-001", o2c.FINANCE_DIRECTOR
    ).delta_cents == 2_000_000
    assert runtime.get_object(
        o2c.ReceivableVoucher, "ARV-PAY-O2C-SO-001", o2c.FINANCE_DIRECTOR
    ).delta_cents == -2_000_000


# ---------------------------------------------------------------------------
# 7. Credit exposure and initial bottleneck diagnosis
# ---------------------------------------------------------------------------
def test_credit_exposure_and_initial_bottlenecks(o2c_env):
    runtime, _, _ = o2c_env

    large = runtime.call_function(
        "evaluate_credit_exposure",
        {"order": "O2C-SO-002"},
        o2c.FINANCE_DIRECTOR,
    )
    assert large["over_limit_cents"] == 2_000_000
    assert large["requires_finance_director"] is True

    small = runtime.call_function(
        "evaluate_credit_exposure",
        {"order": "O2C-SO-001"},
        o2c.FINANCE_DIRECTOR,
    )
    assert small["over_limit_cents"] == 0
    assert small["requires_finance_director"] is False

    view = runtime.call_function(
        "diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR
    )
    assert [row["order_id"] for row in view["credit_exceeded_drafts"]] == [
        "O2C-SO-002"
    ]
    assert view["unshipped_confirmed_orders"] == []
    assert view["unbilled_shipped_orders"] == []
    assert view["unpaid_invoices"] == []


# ---------------------------------------------------------------------------
# 8. AI large order -> finance director approval -> real Odoo confirm
# ---------------------------------------------------------------------------
def test_ai_large_order_requires_finance_director(o2c_env):
    runtime, adapter, _ = o2c_env

    pending = runtime.submit_action(
        "AI-CONFIRM-LARGE",
        "confirm_sales_order",
        {"order": "O2C-SO-002"},
        o2c.AI_AGENT,
    )
    assert pending["status"] == "PENDING_APPROVAL"
    assert adapter.process_new_runs() == []
    assert adapter.order_row("O2C-SO-002")["state"] == "draft"
    assert _order(runtime, "O2C-SO-002").status == "draft"

    approved = runtime.approve_action(
        "AI-CONFIRM-LARGE", o2c.FINANCE_DIRECTOR
    )
    assert approved["status"] == "COMMITTED"
    assert approved["approved_by"] == o2c.FINANCE_DIRECTOR.actor_id

    evidence = adapter.process_new_runs()
    assert len(evidence) == 1
    assert evidence[0]["action"] == "confirm_sales_order"
    assert adapter.order_row("O2C-SO-002")["state"] == "sale"
    assert _order(runtime, "O2C-SO-002").status == "confirmed"


# ---------------------------------------------------------------------------
# 9. Overlay survives a full Odoo re-sync
# ---------------------------------------------------------------------------
def test_overlay_survives_odoo_resync(o2c_env):
    runtime, adapter, _ = o2c_env
    runtime.submit_action(
        "GOVERNANCE",
        "annotate_o2c_governance",
        {
            "customer": "O2C-CUST-001",
            "risk_tier": "WATCHLIST",
            "credit_hold_note": "watch Q4 exposure",
            "order": "O2C-SO-001",
            "account_rep": "sales-rep-01",
            "fulfillment_note": "call before dispatch",
        },
        o2c.SALES_REP,
    )
    adapter.sync_from_odoo()

    customer = _customer(runtime)
    assert customer.risk_tier == "WATCHLIST"
    assert customer.credit_hold_note == "watch Q4 exposure"
    order = _order(runtime, "O2C-SO-001")
    assert order.account_rep == "sales-rep-01"
    assert order.fulfillment_note == "call before dispatch"
    # source-backed order state is still the fresh Odoo draft
    assert order.status == "draft"


# ---------------------------------------------------------------------------
# 10. Payment amount mismatch is rejected; Odoo invoice stays open
# ---------------------------------------------------------------------------
def test_payment_amount_mismatch_rejected(o2c_env):
    runtime, adapter, _ = o2c_env
    _confirm(runtime, adapter)
    _ship(runtime, adapter)
    _invoice(runtime, adapter)

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "PAY-BAD",
            "record_customer_payment",
            {"order": "O2C-SO-001", "payment_cents": 1_999_999},
            o2c.FINANCE_DIRECTOR,
        )
    assert excinfo.value.code == "PAYMENT_AMOUNT_MISMATCH"
    assert runtime.get_action_run("PAY-BAD")["status"] == "REJECTED"
    assert adapter.process_new_runs() == []

    invoice = adapter.invoice_row("O2C-SO-001")
    assert invoice is not None
    assert invoice["state"] == "posted"
    assert float(invoice["amount_residual"]) == 20_000.0
    assert _ar_balance(runtime).ar_balance_cents == 2_000_000


# ---------------------------------------------------------------------------
# 11. Paying an already-paid invoice is rejected
# ---------------------------------------------------------------------------
def test_already_paid_invoice_rejected(o2c_env):
    runtime, adapter, _ = o2c_env
    _confirm(runtime, adapter)
    _ship(runtime, adapter)
    _invoice(runtime, adapter)
    _pay(runtime, adapter)

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "PAY-AGAIN",
            "record_customer_payment",
            {"order": "O2C-SO-001", "payment_cents": 2_000_000},
            o2c.FINANCE_DIRECTOR,
        )
    assert excinfo.value.code == "INVOICE_ALREADY_PAID"
    assert runtime.get_action_run("PAY-AGAIN")["status"] == "REJECTED"
    assert float(adapter.invoice_row("O2C-SO-001")["amount_residual"]) == 0.0


# ---------------------------------------------------------------------------
# 12. Blocked customer cannot confirm; Odoo stays draft
# ---------------------------------------------------------------------------
def test_blocked_customer_confirm_rejected(o2c_env):
    runtime, adapter, _ = o2c_env
    runtime.submit_action(
        "BLOCK-CUSTOMER",
        "annotate_o2c_governance",
        {"customer": "O2C-CUST-001", "risk_tier": "BLOCKED"},
        o2c.FINANCE_DIRECTOR,
    )

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "CONFIRM-BLOCKED",
            "confirm_sales_order",
            {"order": "O2C-SO-001"},
            o2c.SALES_REP,
        )
    assert excinfo.value.code == "CUSTOMER_CREDIT_BLOCKED"
    assert runtime.get_action_run("CONFIRM-BLOCKED")["status"] == "REJECTED"
    assert adapter.process_new_runs() == []
    assert adapter.order_row("O2C-SO-001")["state"] == "draft"
    assert _order(runtime, "O2C-SO-001").status == "draft"


# ---------------------------------------------------------------------------
# 13. Bottleneck radar changes correctly at each O2C stage
# ---------------------------------------------------------------------------
def test_diagnose_bottlenecks_at_each_stage(o2c_env):
    runtime, adapter, _ = o2c_env

    def view():
        return runtime.call_function(
            "diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR
        )

    initial = view()
    assert [row["order_id"] for row in initial["credit_exceeded_drafts"]] == [
        "O2C-SO-002"
    ]

    _confirm(runtime, adapter)
    confirmed = view()
    assert [row["order_id"] for row in confirmed["unshipped_confirmed_orders"]] == [
        "O2C-SO-001"
    ]

    _ship(runtime, adapter)
    shipped = view()
    assert [row["order_id"] for row in shipped["unbilled_shipped_orders"]] == [
        "O2C-SO-001"
    ]

    _invoice(runtime, adapter)
    invoiced = view()
    assert [row["invoice_id"] for row in invoiced["unpaid_invoices"]] == [
        "INV-O2C-SO-001"
    ]

    _pay(runtime, adapter)
    paid = view()
    assert paid["unpaid_invoices"] == []
    assert paid["unbilled_shipped_orders"] == []
    assert paid["unshipped_confirmed_orders"] == []
    # the large draft order is still a credit-exposure item
    assert [row["order_id"] for row in paid["credit_exceeded_drafts"]] == [
        "O2C-SO-002"
    ]


# ---------------------------------------------------------------------------
# 14. Confirming a non-draft order is rejected
# ---------------------------------------------------------------------------
def test_confirm_twice_rejected(o2c_env):
    runtime, adapter, _ = o2c_env
    _confirm(runtime, adapter)

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "CONFIRM-AGAIN",
            "confirm_sales_order",
            {"order": "O2C-SO-001"},
            o2c.SALES_REP,
        )
    assert excinfo.value.code == "ORDER_NOT_DRAFT"
    assert runtime.get_action_run("CONFIRM-AGAIN")["status"] == "REJECTED"
    assert adapter.order_row("O2C-SO-001")["state"] == "sale"


# ---------------------------------------------------------------------------
# 15. M2 OWL metamodel: RoleDef, ObjectProperty, Invariants & Column Masking
# ---------------------------------------------------------------------------
def test_owl_metamodel_roles_invariants_and_masking():
    assert set(o2c.spec.roles) >= {
        "sales_rep",
        "buyer",
        "warehouse",
        "finance",
        "finance_director",
        "ops_manager",
        "ai_agent",
        "system",
    }
    assert set(o2c.spec.object_properties) == {
        "ReceivableBalance.customer",
        "SalesOrder.customer",
        "Delivery.order",
        "Invoice.order",
        "Invoice.customer",
        "ReceivableVoucher.ledger",
        "ReceivableVoucher.customer",
        "ReceivableVoucher.order",
    }
    assert set(o2c.spec.abstract_types) >= {
        "MasterEntity",
        "BalanceLedger",
        "ProcessDocument",
        "EventVoucher",
    }
    assert o2c.Customer.is_aggregate_root() is True
    assert set(o2c.Customer.ontology_superclasses()) == {"MasterEntity"}
    assert o2c.ReceivableBalance.ontology_superclasses() == ["BalanceLedger"]
    assert set(o2c.SalesOrder.ontology_superclasses()) == {"ProcessDocument"}
    assert o2c.ReceivableVoucher.ontology_superclasses() == ["EventVoucher"]
    assert o2c.ReceivableVoucher.is_append_only() is True
    assert set(o2c.spec.invariants) >= {
        "business_non_negative_fields",
        "invoice_requires_shipped_order",
        "delivery_requires_confirmed_order",
        "customer_ar_non_negative",
    }
    assert set(o2c.spec.transition_invariants) >= {
        "process_document_state_transitions",
        "voucher_backed_balance_conservation",
    }

    demo = o2c.build_o2c_demo()
    rt = demo.runtime
    try:
        # 1. Referential integrity (TARGET_NOT_FOUND) on dangling customer
        batch = EditBatch(rt.spec, rt, o2c.FINANCE_DIRECTOR)
        batch.create(
            o2c.SalesOrder(
                order_id="O2C-SO-BAD",
                customer="O2C-CUST-MISSING",
                product_summary="O2C Widget",
                quantity=1,
                unit_price_cents=100_000,
                total_cents=100_000,
            )
        )
        with pytest.raises(InvariantError) as excinfo:
            batch.validate_object_properties()
        assert excinfo.value.code == "TARGET_NOT_FOUND"

        # 2. Declarative @spec.invariant rejects invoice on unshipped draft order
        inv_batch = EditBatch(rt.spec, rt, o2c.FINANCE_DIRECTOR)
        inv_batch.create(
            o2c.Invoice(
                invoice_id="INV-BAD-DRAFT",
                order="O2C-SO-001",
                customer="O2C-CUST-001",
                amount_cents=2_000_000,
            )
        )
        inv_batch.validate_object_properties()
        with pytest.raises(InvariantError) as inv_exc:
            inv_batch.validate_invariants()
        assert inv_exc.value.code == "CANNOT_INVOICE_UNSHIPPED_ORDER"

        # 2. Column masking on Customer.credit_hold_note
        rt.submit_action(
            "MASK-NOTE",
            "annotate_o2c_governance",
            {
                "customer": "O2C-CUST-001",
                "credit_hold_note": "内部授信授意：高风险观察",
            },
            o2c.FINANCE_DIRECTOR,
        )
        assert rt.get_object(o2c.Customer, "O2C-CUST-001", o2c.WAREHOUSE).credit_hold_note == "***"
        assert rt.get_object(o2c.Customer, "O2C-CUST-001", o2c.AI_AGENT).credit_hold_note == "***"
        assert (
            rt.get_object(o2c.Customer, "O2C-CUST-001", o2c.FINANCE_DIRECTOR).credit_hold_note
            == "内部授信授意：高风险观察"
        )
        assert (
            rt.get_object(o2c.Customer, "O2C-CUST-001", o2c.SALES_REP).credit_hold_note
            == "内部授信授意：高风险观察"
        )

        # 4. Inherited ProcessDocument transition invariant rejects illegal status jump
        jump_batch = EditBatch(rt.spec, rt, o2c.FINANCE_DIRECTOR)
        draft_order = jump_batch.require(o2c.SalesOrder, "O2C-SO-001")
        jump_batch.modify(draft_order, status="paid")
        with pytest.raises(InvariantError) as jump_exc:
            jump_batch.validate_transition_invariants()
        assert jump_exc.value.code == "ILLEGAL_DOCUMENT_TRANSITION"

        # 5. Inherited BalanceLedger conservation rejects unbacked AR mutation
        ar_batch = EditBatch(rt.spec, rt, o2c.FINANCE_DIRECTOR)
        ledger = ar_batch.require(o2c.ReceivableBalance, "AR-O2C-CUST-001")
        ar_batch.modify(ledger, ar_balance_cents=500_000)
        with pytest.raises(InvariantError) as ar_exc:
            ar_batch.validate_transition_invariants()
        assert ar_exc.value.code == "UNBACKED_BALANCE_MUTATION"
    finally:
        rt.close()
