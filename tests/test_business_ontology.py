"""Tests for M2 ontology inheritance, __abstract__, __append_only__,
@spec.transition_invariant, and M1 abstract business ontology archetypes.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel

from examples.business_ontology import (
    BalanceLedger,
    EventVoucher,
    MasterEntity,
    ProcessDocument,
    WorkCard,
    register_work_card_primitives,
    settle_work_card,
    spec as business_spec,
    submit_or_open_work_card,
)
from open_ontology import (
    Actor,
    AutoID,
    EditBatch,
    GovernanceViolationError,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    TransitionInvariantDef,
    export_llm_tools,
)
from open_ontology.model_views import describe_tbox

ADMIN = Actor(actor_id="admin-01", role="admin", clearance_tags={"*"})
CLERK = Actor(actor_id="clerk-01", role="clerk", clearance_tags={"*"})


# ---------------------------------------------------------------------------
# 1. M2 OntologySpec inheritance (extends), __abstract__ & MRO metadata merge
# ---------------------------------------------------------------------------
def test_ontology_spec_extends_and_abstract_isolation():
    parent = OntologySpec()
    parent.role("admin")
    parent.role("clerk")
    parent.interface("Named", required_fields=("name",))

    class BaseNode(ObjectType):
        __abstract__ = True
        __aggregate_root__ = True
        __interfaces__ = ("Named",)
        __owned__ = {"internal_note": None}
        __masked_fields__ = {"secret_tag": ("admin",)}
        __mask_defaults__ = {"secret_tag": "***"}

        node_id: str
        name: str
        internal_note: str | None = None
        secret_tag: str = ""

    parent.object_type(BaseNode)
    assert BaseNode.is_abstract() is True
    assert "BaseNode" in parent.abstract_types
    assert "BaseNode" not in parent.object_types

    child = OntologySpec(extends=(parent,))
    child.role("auditor")
    child.interface("Regional", required_fields=("region",))

    class Store(BaseNode):
        __primary_key__ = "node_id"
        __interfaces__ = ("Regional",)
        __owned__ = {"extra_flag": False}
        __masked_fields__ = {"region_pin": ("admin", "auditor")}

        region: str
        region_pin: str = "0000"
        extra_flag: bool = False

    child.object_type(Store)

    assert Store.is_abstract() is False
    assert Store.is_aggregate_root() is True
    assert Store.ontology_superclasses() == ["BaseNode"]
    assert Store.__interfaces__ == ("Named", "Regional")
    assert Store.__owned__ == {"internal_note": None, "extra_flag": False}
    assert Store.__masked_fields__ == {
        "secret_tag": ("admin",),
        "region_pin": ("admin", "auditor"),
    }
    assert Store.__mask_defaults__ == {"secret_tag": "***"}

    # Abstract type cannot be instantiated or looked up as a concrete table
    with pytest.raises(GovernanceViolationError):
        child.get_object_type(BaseNode)
    with pytest.raises(GovernanceViolationError):
        child.get_object_type("BaseNode")

    # TBox & OSDK expose concrete types only, with superclasses + append_only metadata
    tbox = describe_tbox(child)
    assert [t["name"] for t in tbox["object_types"]] == ["Store"]
    assert tbox["object_types"][0]["superclasses"] == ["BaseNode"]
    assert tbox["object_types"][0]["append_only"] is False
    tool_names = [t["function"]["name"] for t in export_llm_tools(child)]
    assert all("BaseNode" not in name for name in tool_names)

    # Polymorphic query over abstract parent works in EditBatch (including masked read views)
    rt = OntologyRuntime(child)
    try:
        rt.put_object(
            Store(node_id="S-1", name="Flagship", region="east", secret_tag="TOP")
        )
        batch = EditBatch(child, rt, CLERK, mask_fn=rt._apply_masking)
        found = batch.query(BaseNode)
        assert len(found) == 1
        assert isinstance(found[0], Store)
        assert found[0].secret_tag == "***"
        assert rt.get_object(Store, "S-1", CLERK).secret_tag == "***"
        assert rt.get_object(Store, "S-1", ADMIN).secret_tag == "TOP"
        assert [c.__name__ for c in child.subtypes_of(BaseNode)] == ["Store"]

        with pytest.raises(GovernanceViolationError):
            rt.put_object(BaseNode(node_id="ABS-1", name="Bad"))
        with pytest.raises(GovernanceViolationError):
            rt.get_object(BaseNode, "S-1", ADMIN)
    finally:
        rt.close()


def test_ontology_spec_extend_detects_conflicts_and_supports_diamond():
    root = OntologySpec()
    root.role("admin")

    left = OntologySpec(extends=(root,))
    right = OntologySpec(extends=(root,))
    diamond = OntologySpec(extends=(left, right))
    assert "admin" in diamond.roles

    other = OntologySpec()
    other.role("admin", description="Conflicting description")
    with pytest.raises(ValueError, match="conflicting role"):
        OntologySpec(extends=(root, other))


# ---------------------------------------------------------------------------
# 2. M2 __append_only__ enforcement & @spec.transition_invariant
# ---------------------------------------------------------------------------
def test_append_only_blocks_action_update_delete_but_allows_revert():
    s = OntologySpec(read_only_source=True)
    s.role("admin")

    class LogEntry(ObjectType):
        __primary_key__ = "log_id"
        __owned__ = True
        __append_only__ = True

        log_id: str
        amount: int

    class WriteParams(BaseModel):
        log_id: str
        amount: int = 10

    class TargetParams(BaseModel):
        log_id: str

    s.object_type(LogEntry)

    @s.edit_function("append_log", WriteParams)
    def append_log(ctx, params):
        ctx.create(LogEntry(log_id=params.log_id, amount=params.amount))

    s.declare_action(
        "append_log",
        edit_function="append_log",
        allowed_roles=("admin",),
        affects_source=False,
    )

    @s.edit_function("tamper_log", WriteParams)
    def tamper_log(ctx, params):
        row = ctx.require(LogEntry, params.log_id)
        ctx.modify(row, amount=params.amount)

    s.declare_action(
        "tamper_log",
        edit_function="tamper_log",
        allowed_roles=("admin",),
        affects_source=False,
    )

    @s.edit_function("purge_log", TargetParams)
    def purge_log(ctx, params):
        row = ctx.require(LogEntry, params.log_id)
        ctx.delete(row)

    s.declare_action(
        "purge_log",
        edit_function="purge_log",
        allowed_roles=("admin",),
        affects_source=False,
    )

    rt = OntologyRuntime(s)
    try:
        rt.submit_action("RUN-ADD", "append_log", {"log_id": "L-1", "amount": 50}, ADMIN)

        with pytest.raises(InvariantError) as upd_exc:
            rt.submit_action("RUN-UPD", "tamper_log", {"log_id": "L-1", "amount": 99}, ADMIN)
        assert upd_exc.value.code == "APPEND_ONLY_VIOLATION"
        assert rt.get_action_run("RUN-UPD")["status"] == "REJECTED"

        with pytest.raises(InvariantError) as del_exc:
            rt.submit_action("RUN-DEL", "purge_log", {"log_id": "L-1"}, ADMIN)
        assert del_exc.value.code == "APPEND_ONLY_VIOLATION"
        assert rt.get_action_run("RUN-DEL")["status"] == "REJECTED"

        # Time-machine revert remains valid
        reverted = rt.revert_action("RUN-ADD", "RUN-REV", ADMIN)
        assert reverted["status"] == "COMMITTED"
        assert rt.query_objects(LogEntry, ADMIN) == []
    finally:
        rt.close()


def test_transition_invariant_supports_one_and_two_arg_handlers_and_approval_recheck():
    s = OntologySpec(read_only_source=True)
    s.role("admin")
    s.role("worker")

    class Counter(ObjectType):
        __primary_key__ = "cid"
        __owned__ = True

        cid: str
        value: int

    class StepParams(BaseModel):
        cid: str
        target: int
        needs_approval: bool = False

    s.object_type(Counter)

    register_work_card_primitives(s, opener_roles=("worker", "admin"), resolver_roles=("admin",))

    @s.edit_function("set_value", StepParams)
    def set_value(ctx, params):
        if params.needs_approval and ctx.actor.role != "admin":
            raise InvariantError("needs admin approval", code="APPROVAL_REQUIRED_STEP")
        c = ctx.require(Counter, params.cid)
        ctx.modify(c, value=params.target)

    s.declare_action(
        "set_value",
        edit_function="set_value",
        allowed_roles=("worker", "admin"),
        revert_roles=("admin",),
        affects_source=False,
    )

    @s.transition_invariant("monotonic_increase", code="NON_MONOTONIC")
    def monotonic_increase(diff) -> bool:
        for ch in diff.object_changes:
            if (
                ch.object_type == "Counter"
                and ch.kind == "update"
                and ch.after["value"] <= ch.before["value"]
            ):
                return False
        return True

    @s.transition_invariant("cap_from_view", code="STEP_TOO_LARGE")
    def cap_from_view(diff, view) -> bool:
        for ch in diff.object_changes:
            if ch.object_type == "Counter" and ch.kind == "update":
                live = view.get(Counter, ch.object_id)
                if live is not None and (ch.after["value"] - ch.before["value"]) > 10:
                    return False
        return True

    assert isinstance(
        s.get_transition_invariant("monotonic_increase"), TransitionInvariantDef
    )

    rt = OntologyRuntime(s)
    try:
        rt.put_object(Counter(cid="C-1", value=10))
        sim_bad = rt.simulate_action("set_value", {"cid": "C-1", "target": 5}, ADMIN)
        assert sim_bad.valid is False
        assert sim_bad.error_code == "NON_MONOTONIC"

        sim_big = rt.simulate_action("set_value", {"cid": "C-1", "target": 25}, ADMIN)
        assert sim_big.valid is False
        assert sim_big.error_code == "STEP_TOO_LARGE"

        worker = Actor(actor_id="worker-01", role="worker", clearance_tags={"*"})
        pending = submit_or_open_work_card(
            rt,
            "RUN-PEND",
            "set_value",
            {"cid": "C-1", "target": 15, "needs_approval": True},
            worker,
            target_object_type="Counter",
            target_object_id="C-1",
            assignee_role="admin",
            card_id="RUN-PEND",
        )
        assert pending["status"] == "AWAITING_WORK_CARD"
        assert pending["work_card_status"] == "OPEN"
        approved = settle_work_card(rt, "RUN-PEND", ADMIN)
        assert approved["status"] == "COMMITTED"
        assert rt.get_object(Counter, "C-1", ADMIN).value == 15
        assert rt.get_object(WorkCard, "RUN-PEND", ADMIN).status == "COMPLETED"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. M1 Abstract business_ontology archetypes (inherited in child spec)
# ---------------------------------------------------------------------------
def _make_wms_spec() -> tuple[
    OntologySpec,
    type[MasterEntity],
    type[BalanceLedger],
    type[BalanceLedger],
    type[ProcessDocument],
    type[EventVoucher],
]:
    wms = OntologySpec(extends=(business_spec,))
    wms.role("manager")
    wms.role("warehouse")

    class SkuMaster(MasterEntity):
        __primary_key__ = "sku_id"
        __title__ = "name"
        __owned__ = True

        sku_id: str
        name: str

    class InternalStockLedger(BalanceLedger):
        """内生总账台账（__owned__=True，禁止快照覆盖 + 必须与凭证增量守恒）。"""

        __primary_key__ = "ledger_id"
        __owned__ = True
        __non_negative_fields__: ClassVar[tuple[str, ...]] = ("on_hand_qty",)
        __voucher_delta_rules__: ClassVar[dict[str, tuple[str, str, str]]] = {
            "on_hand_qty": ("StockMoveVoucher", "ledger", "qty_delta")
        }

        ledger_id: str
        sku: ObjectProperty[SkuMaster]
        on_hand_qty: int = 0

    class ParasiticStockLedger(BalanceLedger):
        """寄居双轨台账（physical 允许 ERP 快照同步，ai_reserved 本体自有预占）。"""

        __primary_key__ = "p_ledger_id"
        __owned__ = {"ai_reserved_qty": 0}
        __non_negative_fields__: ClassVar[tuple[str, ...]] = (
            "physical_on_hand_qty",
            "ai_reserved_qty",
        )

        p_ledger_id: str
        sku: ObjectProperty[SkuMaster]
        physical_on_hand_qty: int = 0
        ai_reserved_qty: int = 0

    class PickTicket(ProcessDocument):
        __primary_key__ = "ticket_id"
        __owned__ = True
        __initial_statuses__: ClassVar[tuple[str, ...]] = ("open",)
        __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {
            "open": ("picked", "cancelled"),
            "picked": ("closed",),
        }
        __deletable_statuses__: ClassVar[tuple[str, ...]] = ("cancelled",)

        ticket_id: str
        status: str = "open"

    class StockMoveVoucher(EventVoucher):
        __primary_key__ = "voucher_id"
        __owned__ = True

        voucher_id: AutoID
        ledger: ObjectProperty[InternalStockLedger]
        qty_delta: int

    for cls in (
        SkuMaster,
        InternalStockLedger,
        ParasiticStockLedger,
        PickTicket,
        StockMoveVoucher,
    ):
        wms.object_type(cls)

    @wms.invariant("parasitic_available_non_negative", code="INSUFFICIENT_AVAILABLE_STOCK")
    def parasitic_available_non_negative(view) -> bool:
        for row in view.query(ParasiticStockLedger):
            if row.physical_on_hand_qty - row.ai_reserved_qty < 0:
                return False
        return True

    class AdjustWithVoucherParams(BaseModel):
        ledger: ObjectProperty[InternalStockLedger]
        voucher_id: str
        delta: int
        emit_voucher: bool = True

    @wms.edit_function("post_stock_move", AdjustWithVoucherParams)
    def post_stock_move(ctx, params):
        led = ctx.require(InternalStockLedger, params.ledger)
        ctx.modify(led, on_hand_qty=led.on_hand_qty + params.delta)
        if params.emit_voucher:
            ctx.create(
                StockMoveVoucher(
                    voucher_id=params.voucher_id,
                    ledger=led.ledger_id,
                    qty_delta=params.delta,
                )
            )

    wms.declare_action(
        "post_stock_move",
        edit_function="post_stock_move",
        allowed_roles=("warehouse", "manager"),
        affects_source=False,
    )

    class ReserveParams(BaseModel):
        ledger: ObjectProperty[ParasiticStockLedger]
        delta: int

    @wms.edit_function("reserve_stock", ReserveParams)
    def reserve_stock(ctx, params):
        led = ctx.require(ParasiticStockLedger, params.ledger)
        ctx.modify(led, ai_reserved_qty=led.ai_reserved_qty + params.delta)

    wms.declare_action(
        "reserve_stock",
        edit_function="reserve_stock",
        allowed_roles=("warehouse", "manager"),
        affects_source=False,
    )

    class TicketCreateParams(BaseModel):
        ticket_id: str
        status: str = "open"

    @wms.edit_function("create_ticket", TicketCreateParams)
    def create_ticket(ctx, params):
        ctx.create(PickTicket(ticket_id=params.ticket_id, status=params.status))

    wms.declare_action(
        "create_ticket",
        edit_function="create_ticket",
        allowed_roles=("warehouse", "manager"),
        affects_source=False,
    )

    @wms.edit_function("advance_ticket", TicketCreateParams)
    def advance_ticket(ctx, params):
        t = ctx.require(PickTicket, params.ticket_id)
        ctx.modify(t, status=params.status)

    wms.declare_action(
        "advance_ticket",
        edit_function="advance_ticket",
        allowed_roles=("warehouse", "manager"),
        affects_source=False,
    )

    class TicketDeleteParams(BaseModel):
        ticket_id: str

    @wms.edit_function("delete_ticket", TicketDeleteParams)
    def delete_ticket(ctx, params):
        ctx.delete(ctx.require(PickTicket, params.ticket_id))

    wms.declare_action(
        "delete_ticket",
        edit_function="delete_ticket",
        allowed_roles=("manager",),
        affects_source=False,
    )

    class TamperVoucherParams(BaseModel):
        voucher_id: str
        qty_delta: int

    @wms.edit_function("tamper_voucher", TamperVoucherParams)
    def tamper_voucher(ctx, params):
        v = ctx.require(StockMoveVoucher, params.voucher_id)
        ctx.modify(v, qty_delta=params.qty_delta)

    wms.declare_action(
        "tamper_voucher",
        edit_function="tamper_voucher",
        allowed_roles=("manager",),
        affects_source=False,
    )

    return (
        wms,
        SkuMaster,
        InternalStockLedger,
        ParasiticStockLedger,
        PickTicket,
        StockMoveVoucher,
    )


def test_business_archetypes_conservation_state_machine_vouchers_and_parasitic_sync():
    (
        wms,
        SkuMaster,
        InternalStockLedger,
        ParasiticStockLedger,
        PickTicket,
        StockMoveVoucher,
    ) = _make_wms_spec()

    assert SkuMaster.is_aggregate_root() is True
    assert StockMoveVoucher.is_append_only() is True
    assert {c.__name__ for c in wms.subtypes_of(BalanceLedger)} == {
        "InternalStockLedger",
        "ParasiticStockLedger",
    }

    mgr = Actor(actor_id="mgr-01", role="manager", clearance_tags={"*"})
    rt = OntologyRuntime(wms)
    try:
        rt.put_object(SkuMaster(sku_id="SKU-1", name="Widget"))
        rt.put_object(InternalStockLedger(ledger_id="LED-1", sku="SKU-1", on_hand_qty=0))

        # 1. Internal BalanceLedger: mutation without EventVoucher blocked
        with pytest.raises(InvariantError) as unbacked:
            rt.submit_action(
                "MOV-BAD",
                "post_stock_move",
                {"ledger": "LED-1", "voucher_id": "V-0", "delta": 10, "emit_voucher": False},
                mgr,
            )
        assert unbacked.value.code == "UNBACKED_BALANCE_MUTATION"

        # Valid inbound move with EventVoucher succeeds; negative balance blocked
        rt.submit_action(
            "MOV-IN",
            "post_stock_move",
            {"ledger": "LED-1", "voucher_id": "V-1", "delta": 20, "emit_voucher": True},
            mgr,
        )
        assert rt.get_object(InternalStockLedger, "LED-1", mgr).on_hand_qty == 20

        with pytest.raises(InvariantError) as neg_exc:
            rt.submit_action(
                "MOV-NEG",
                "post_stock_move",
                {"ledger": "LED-1", "voucher_id": "V-NEG", "delta": -25, "emit_voucher": True},
                mgr,
            )
        assert neg_exc.value.code == "NEGATIVE_BALANCE"

        # 2. EventVoucher is append-only (tampering rejected), red reversal allowed
        with pytest.raises(InvariantError) as append_exc:
            rt.submit_action(
                "V-TAMPER",
                "tamper_voucher",
                {"voucher_id": "V-1", "qty_delta": 999},
                mgr,
            )
        assert append_exc.value.code == "APPEND_ONLY_VIOLATION"

        rt.submit_action(
            "MOV-RED",
            "post_stock_move",
            {"ledger": "LED-1", "voucher_id": "V-1-RED", "delta": -5, "emit_voucher": True},
            mgr,
        )
        assert rt.get_object(InternalStockLedger, "LED-1", mgr).on_hand_qty == 15

        # Time-machine revert works cleanly for red reversal
        rt.revert_action("MOV-RED", "MOV-RED-REV", mgr)
        assert rt.get_object(InternalStockLedger, "LED-1", mgr).on_hand_qty == 20

        # Internal __owned__=True ledger cannot be overwritten by external snapshot
        with pytest.raises(GovernanceViolationError):
            rt.load_snapshot({"InternalStockLedger": [{"ledger_id": "LED-1", "sku": "SKU-1", "on_hand_qty": 999}]})

        # 3. Parasitic dual-column BalanceLedger: snapshot syncs physical, preserves ai_reserved_qty
        rt.load_snapshot(
            {
                "ParasiticStockLedger": [
                    {"p_ledger_id": "P-1", "sku": "SKU-1", "physical_on_hand_qty": 100}
                ]
            }
        )
        rt.submit_action("RES-1", "reserve_stock", {"ledger": "P-1", "delta": 30}, mgr)
        rt.load_snapshot(
            {
                "ParasiticStockLedger": [
                    {"p_ledger_id": "P-1", "sku": "SKU-1", "physical_on_hand_qty": 80}
                ]
            }
        )
        p_row = rt.get_object(ParasiticStockLedger, "P-1", mgr)
        assert p_row.physical_on_hand_qty == 80
        assert p_row.ai_reserved_qty == 30

        # 4. ProcessDocument state machine: initial, valid edge, illegal jump, deletable check
        with pytest.raises(InvariantError) as bad_init:
            rt.submit_action("T-INIT", "create_ticket", {"ticket_id": "T-1", "status": "closed"}, mgr)
        assert bad_init.value.code == "INVALID_INITIAL_DOCUMENT_STATUS"

        rt.submit_action("T-OK", "create_ticket", {"ticket_id": "T-1", "status": "open"}, mgr)
        with pytest.raises(InvariantError) as bad_jump:
            rt.submit_action("T-JUMP", "advance_ticket", {"ticket_id": "T-1", "status": "closed"}, mgr)
        assert bad_jump.value.code == "ILLEGAL_DOCUMENT_TRANSITION"

        with pytest.raises(InvariantError) as bad_del:
            rt.submit_action("T-DEL-OPEN", "delete_ticket", {"ticket_id": "T-1"}, mgr)
        assert bad_del.value.code == "NON_DELETABLE_DOCUMENT_STATUS"

        rt.submit_action("T-CANCEL", "advance_ticket", {"ticket_id": "T-1", "status": "cancelled"}, mgr)
        rt.submit_action("T-DEL-OK", "delete_ticket", {"ticket_id": "T-1"}, mgr)
        assert rt.query_objects(PickTicket, mgr) == []
    finally:
        rt.close()
