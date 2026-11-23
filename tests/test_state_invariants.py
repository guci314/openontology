"""Runtime @spec.invariant enforcement and Kanban stateful fuzzing tests."""

from __future__ import annotations

import itertools
import random

import pytest
from pydantic import BaseModel

from examples.business_ontology import (
    WorkCard,
    register_work_card_primitives,
    settle_work_card,
    submit_or_open_work_card,
)
from examples.kanban import (
    BLOCK_KINDS,
    BOARD_MANAGER,
    CARD_STATUSES,
    OUTCOMES,
    Card,
    spec as kanban_spec,
)
from open_ontology import (
    Actor,
    GovernanceViolationError,
    InvariantError,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    PermissionDeniedError,
)

ADMIN = Actor(actor_id="admin-01", role="admin", clearance_tags={"*"})
_RUN_SEQ = itertools.count(1)


def _next_run(prefix: str = "INV") -> str:
    return f"{prefix}-{next(_RUN_SEQ):05d}"


# ---------------------------------------------------------------------------
# 1. Kernel @spec.invariant behavior
# ---------------------------------------------------------------------------
class _Account(ObjectType):
    __primary_key__ = "account_id"
    __owned__ = True
    __security_tag_fields__ = ("tenant",)

    account_id: str
    tenant: str
    balance: int
    status: str = "active"


class _AdjustParams(BaseModel):
    account_id: str
    delta: int


class _CreateAccountParams(BaseModel):
    account_id: str
    tenant: str
    balance: int


class _DeleteAccountParams(BaseModel):
    account_id: str


class _RetagParams(BaseModel):
    account_id: str
    tenant: str


def _make_bank_spec(
    *,
    require_approval: bool = False,
    global_cap: int | None = None,
) -> OntologySpec:
    spec = OntologySpec(read_only_source=True)
    spec.role("admin")
    spec.role("operator")
    spec.object_type(_Account)

    @spec.edit_function("create_account", _CreateAccountParams)
    def create_account(ctx, params):
        ctx.create(
            _Account(
                account_id=params.account_id,
                tenant=params.tenant,
                balance=params.balance,
            )
        )

    spec.declare_action(
        "create_account",
        edit_function="create_account",
        allowed_roles=("admin", "operator"),
        affects_source=False,
    )

    if require_approval:
        register_work_card_primitives(
            spec,
            opener_roles=("admin", "operator"),
            resolver_roles=("admin",),
        )

    @spec.edit_function("adjust_balance", _AdjustParams)
    def adjust_balance(ctx, params):
        if require_approval and ctx.actor.role != "admin":
            raise InvariantError(
                "operator balance adjustment requires admin WorkCard approval",
                code="APPROVAL_REQUIRED_BALANCE_ADJUST",
            )
        acc = ctx.require(_Account, params.account_id)
        ctx.modify(acc, balance=acc.balance + params.delta)

    spec.declare_action(
        "adjust_balance",
        edit_function="adjust_balance",
        allowed_roles=("admin", "operator"),
        revert_roles=("admin",),
        affects_source=False,
    )

    @spec.edit_function("delete_account", _DeleteAccountParams)
    def delete_account(ctx, params):
        acc = ctx.require(_Account, params.account_id)
        ctx.delete(acc)
        # Verify EditBatch.query() excludes deleted objects in the same batch.
        remaining_ids = {item.account_id for item in ctx.query(_Account)}
        assert params.account_id not in remaining_ids

    spec.declare_action(
        "delete_account",
        edit_function="delete_account",
        allowed_roles=("admin",),
        affects_source=False,
    )

    @spec.edit_function("retag_account", _RetagParams)
    def retag_account(ctx, params):
        acc = ctx.require(_Account, params.account_id)
        ctx.modify(acc, tenant=params.tenant)
        visible_ids = {item.account_id for item in ctx.query(_Account)}
        if not ctx.actor.has_access([f"tenant:{params.tenant}"]):
            assert params.account_id not in visible_ids

    spec.declare_action(
        "retag_account",
        edit_function="retag_account",
        allowed_roles=("admin", "operator"),
        affects_source=False,
    )

    if global_cap is not None:

        @spec.invariant("global_total_cap", code="GLOBAL_CAP_EXCEEDED")
        def _global_cap(ctx):
            total = sum(acc.balance for acc in ctx.query(_Account))
            return total <= global_cap

    return spec


def test_invariant_supports_assert_return_false_and_raise_with_rejection_audit():
    spec = _make_bank_spec()

    @spec.invariant
    def no_negative_balance(ctx):
        """账户余额不能为负。"""
        for acc in ctx.query(_Account):
            assert acc.balance >= 0, f"{acc.account_id} negative"

    @spec.invariant("nonzero_active", code="ZERO_ACTIVE")
    def _nonzero_active(ctx):
        for acc in ctx.query(_Account):
            if acc.status == "active" and acc.balance == 0:
                return False

    @spec.invariant("explicit_raise", code="DEFAULT_RAISE_CODE")
    def _explicit_raise(ctx):
        for acc in ctx.query(_Account):
            if acc.balance > 1000:
                raise InvariantError("balance too large")

    rt = OntologyRuntime(spec)
    try:
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-1", "tenant": "alpha", "balance": 100},
            ADMIN,
        )

        # 1) AssertionError -> InvariantError with derived code INVARIANT_<NAME>
        sim_neg = rt.simulate_action(
            "adjust_balance", {"account_id": "A-1", "delta": -150}, ADMIN
        )
        assert sim_neg.valid is False
        assert sim_neg.diff.is_empty()
        assert sim_neg.error_code == "INVARIANT_NO_NEGATIVE_BALANCE"
        with pytest.raises(InvariantError) as exc_neg:
            rt.submit_action(
                _next_run(),
                "adjust_balance",
                {"account_id": "A-1", "delta": -150},
                ADMIN,
            )
        assert exc_neg.value.code == "INVARIANT_NO_NEGATIVE_BALANCE"

        # 2) return False -> InvariantError with configured code
        sim_zero = rt.simulate_action(
            "adjust_balance", {"account_id": "A-1", "delta": -100}, ADMIN
        )
        assert sim_zero.valid is False
        assert sim_zero.error_code == "ZERO_ACTIVE"
        with pytest.raises(InvariantError) as exc_zero:
            rt.submit_action(
                _next_run(),
                "adjust_balance",
                {"account_id": "A-1", "delta": -100},
                ADMIN,
            )
        assert exc_zero.value.code == "ZERO_ACTIVE"

        # 3) raise InvariantError without code -> defaults to invariant's code
        with pytest.raises(InvariantError) as exc_raise:
            rt.submit_action(
                _next_run(),
                "adjust_balance",
                {"account_id": "A-1", "delta": 950},
                ADMIN,
            )
        assert exc_raise.value.code == "DEFAULT_RAISE_CODE"

        # DB untouched and all 3 rejections persisted
        assert rt.get_object(_Account, "A-1", ADMIN).balance == 100
        rejected = rt.list_action_runs(status="REJECTED")
        assert [r["error_code"] for r in rejected] == [
            "INVARIANT_NO_NEGATIVE_BALANCE",
            "ZERO_ACTIVE",
            "DEFAULT_RAISE_CODE",
        ]
    finally:
        rt.close()


def test_invariant_has_full_rls_visibility_without_polluting_occ_read_versions():
    spec = _make_bank_spec(require_approval=True, global_cap=150)
    rt = OntologyRuntime(spec)
    try:
        alpha_actor = Actor(
            actor_id="op-alpha", role="operator", clearance_tags={"tenant:alpha"}
        )
        beta_actor = Actor(
            actor_id="op-beta", role="operator", clearance_tags={"tenant:beta"}
        )
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-ALPHA", "tenant": "alpha", "balance": 50},
            alpha_actor,
        )
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-BETA", "tenant": "beta", "balance": 70},
            beta_actor,
        )

        alpha_admin = Actor(
            actor_id="admin-alpha", role="admin", clearance_tags={"tenant:alpha"}
        )
        # Alpha admin cannot see Beta via RLS, yet invariant sees A-BETA (50 + 70 + 40 = 160 > 150)
        with pytest.raises(InvariantError) as excinfo:
            rt.submit_action(
                _next_run(),
                "adjust_balance",
                {"account_id": "A-ALPHA", "delta": 40},
                alpha_admin,
            )
        assert excinfo.value.code == "GLOBAL_CAP_EXCEEDED"

        # Valid simulation by alpha_admin must NOT pull A-BETA into read_versions,
        # while operator escalates via M1 WorkCard
        sim_ok = rt.simulate_action(
            "adjust_balance",
            {"account_id": "A-ALPHA", "delta": 20},
            alpha_admin,
        )
        assert sim_ok.valid is True
        assert set(sim_ok.read_versions) == {"_Account:A-ALPHA"}
        pending = submit_or_open_work_card(
            rt,
            _next_run(),
            "adjust_balance",
            {"account_id": "A-ALPHA", "delta": 20},
            alpha_actor,
            target_object_type="_Account",
            target_object_id="A-ALPHA",
            assignee_role="admin",
        )
        assert pending["status"] == "AWAITING_WORK_CARD"
        assert pending["work_card_status"] == "OPEN"
    finally:
        rt.close()


def test_invariant_view_blocks_all_mutations():
    spec = _make_bank_spec()

    @spec.invariant("forbid_writes", code="FORBID_WRITES")
    def _try_write(ctx):
        for acc in ctx.query(_Account):
            ctx.modify(acc, balance=acc.balance + 1)

    rt = OntologyRuntime(spec)
    try:
        with pytest.raises(GovernanceViolationError):
            rt.submit_action(
                _next_run(),
                "create_account",
                {"account_id": "A-1", "tenant": "alpha", "balance": 10},
                ADMIN,
            )
        assert rt.count_objects("_Account") == 0
    finally:
        rt.close()


def test_invariant_enforced_on_approve_and_revert():
    spec = _make_bank_spec(require_approval=True, global_cap=100)
    rt = OntologyRuntime(spec)
    try:
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-1", "tenant": "alpha", "balance": 40},
            ADMIN,
        )
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-2", "tenant": "alpha", "balance": 20},
            ADMIN,
        )
        # Stage A-1 +40 as operator (40+40 + 20 = 100 <= 100, opens WorkCard)
        operator = Actor(actor_id="op-01", role="operator", clearance_tags={"*"})
        pending_id = _next_run("PEND")
        pending = submit_or_open_work_card(
            rt,
            pending_id,
            "adjust_balance",
            {"account_id": "A-1", "delta": 40},
            operator,
            target_object_type="_Account",
            target_object_id="A-1",
            assignee_role="admin",
            card_id=pending_id,
        )
        assert pending["status"] == "AWAITING_WORK_CARD"
        assert pending["work_card_status"] == "OPEN"

        # Meanwhile A-2 increases by 10 as admin (still <= 100: 40 + 30 = 70), committed immediately
        inc_id = _next_run("INC")
        rt.submit_action(inc_id, "adjust_balance", {"account_id": "A-2", "delta": 10}, ADMIN)
        assert rt.get_object(_Account, "A-2", ADMIN).balance == 30

        # Settling pending_id now at t1 would yield 80 + 30 = 110 > 100 -> rejected & rolled back
        with pytest.raises(InvariantError) as exc_approve:
            settle_work_card(rt, pending_id, ADMIN)
        assert exc_approve.value.code == "GLOBAL_CAP_EXCEEDED"
        assert rt.get_object(_Account, "A-1", ADMIN).balance == 40
        assert rt.get_object(WorkCard, pending_id, ADMIN).status == "OPEN"

        # Test revert_action enforcement with a cross-object reserve invariant
        spec_reserve = OntologySpec(read_only_source=True)
        spec_reserve.role("admin")
        spec_reserve.object_type(_Account)

        @spec_reserve.edit_function("create_account", _CreateAccountParams)
        def _create(ctx, params):
            ctx.create(
                _Account(
                    account_id=params.account_id,
                    tenant=params.tenant,
                    balance=params.balance,
                )
            )

        spec_reserve.declare_action(
            "create_account",
            edit_function="create_account",
            allowed_roles=("admin",),
            affects_source=False,
        )

        @spec_reserve.edit_function("adjust_balance", _AdjustParams)
        def _adjust(ctx, params):
            acc = ctx.require(_Account, params.account_id)
            ctx.modify(acc, balance=acc.balance + params.delta)

        spec_reserve.declare_action(
            "adjust_balance",
            edit_function="adjust_balance",
            allowed_roles=("admin",),
            affects_source=False,
        )

        @spec_reserve.invariant("min_reserve", code="RESERVE_BELOW_MIN")
        def _min_reserve(ctx):
            total = sum(acc.balance for acc in ctx.query(_Account))
            assert total >= 50

        rt2 = OntologyRuntime(spec_reserve)
        try:
            rt2.submit_action(
                "R-CREATE-1",
                "create_account",
                {"account_id": "A-1", "tenant": "t", "balance": 50},
                ADMIN,
            )
            rt2.submit_action(
                "R-CREATE-2",
                "create_account",
                {"account_id": "A-2", "tenant": "t", "balance": 10},
                ADMIN,
            )
            # Raise A-2 from 10 -> 60 (total=110), then drain A-1 from 50 -> 0 (total=60)
            rt2.submit_action(
                "R-ADD-A2",
                "adjust_balance",
                {"account_id": "A-2", "delta": 50},
                ADMIN,
            )
            rt2.submit_action(
                "R-SUB-A1",
                "adjust_balance",
                {"account_id": "A-1", "delta": -50},
                ADMIN,
            )
            assert rt2.get_object(_Account, "A-1", ADMIN).balance == 0
            assert rt2.get_object(_Account, "A-2", ADMIN).balance == 60

            # Reverting R-ADD-A2 sets A-2 back to 10 -> total becomes 0 + 10 = 10 < 50 -> blocked!
            with pytest.raises(InvariantError) as exc_revert:
                rt2.revert_action("R-ADD-A2", "R-REV-A2", ADMIN)
            assert exc_revert.value.code == "RESERVE_BELOW_MIN"
            assert rt2.get_object(_Account, "A-2", ADMIN).balance == 60
            assert rt2.get_action_run("R-ADD-A2")["status"] == "COMMITTED"
            assert rt2.get_action_run("R-REV-A2") is None
        finally:
            rt2.close()
    finally:
        rt.close()


def test_edit_batch_query_excludes_deleted_and_rls_revoked_objects():
    from open_ontology import EditBatch

    spec = _make_bank_spec()

    @spec.invariant("non_negative", code="NON_NEGATIVE")
    def _non_neg(ctx):
        for acc in ctx.query(_Account):
            assert acc.balance >= 0

    rt = OntologyRuntime(spec)
    try:
        alpha = Actor(
            actor_id="op-alpha", role="operator", clearance_tags={"tenant:alpha"}
        )
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-1", "tenant": "alpha", "balance": 10},
            ADMIN,
        )
        rt.submit_action(
            _next_run(),
            "create_account",
            {"account_id": "A-2", "tenant": "alpha", "balance": 20},
            ADMIN,
        )
        rt.submit_action(_next_run(), "delete_account", {"account_id": "A-1"}, ADMIN)
        assert [a.account_id for a in rt.query_objects(_Account, ADMIN)] == ["A-2"]

        # Direct EditBatch overlay test: deleted DB row and staged row whose
        # security tags are outside actor's clearance must both be excluded.
        batch = EditBatch(spec, rt, alpha)
        a2 = batch.require(_Account, "A-2")
        batch.delete(a2)
        assert batch.query(_Account) == []

        batch2 = EditBatch(spec, rt, alpha)
        batch2._objects["_Account:A-2"] = {
            "account_id": "A-2",
            "tenant": "beta",
            "balance": 20,
            "status": "active",
        }
        batch2._kinds["_Account:A-2"] = "update"
        assert batch2.query(_Account) == []
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. Kanban bug regressions
# ---------------------------------------------------------------------------
def _seed_kanban_runtime() -> OntologyRuntime:
    rt = OntologyRuntime(kanban_spec)
    rt.submit_action(
        _next_run("KB"),
        "create_board",
        {
            "board_key": "BD-1",
            "title": "主看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
        },
        BOARD_MANAGER,
    )
    for member_id, role, wip_limit in (
        ("dev-1", "developer", 1),
        ("dev-2", "developer", 2),
        ("rev-1", "reviewer", 1),
        ("tst-1", "tester", 1),
        ("coord-1", "coordinator", 1),
    ):
        rt.submit_action(
            _next_run("KB"),
            "add_member",
            {
                "member_id": member_id,
                "name": member_id,
                "role": role,
                "wip_limit": wip_limit,
            },
            BOARD_MANAGER,
        )
    return rt


def test_kanban_rejects_coordinator_preassigned_on_dependent_todo_card():
    rt = _seed_kanban_runtime()
    try:
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {"card_id": "C-1", "board": "BD-1", "title": "前置卡"},
            BOARD_MANAGER,
        )
        with pytest.raises(InvariantError) as excinfo:
            rt.submit_action(
                _next_run("KB"),
                "create_card",
                {
                    "card_id": "C-2",
                    "board": "BD-1",
                    "title": "子卡",
                    "waits_for": ("C-1",),
                    "assignee": "coord-1",
                },
                BOARD_MANAGER,
            )
        assert excinfo.value.code == "ROLE_NOT_ALLOWED_FOR_STATUS"
        rt.check_invariants()
    finally:
        rt.close()


def test_kanban_manager_can_reassign_preassigned_ready_card():
    rt = _seed_kanban_runtime()
    try:
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {"card_id": "C-1", "board": "BD-1", "title": "前置卡"},
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {
                "card_id": "C-2",
                "board": "BD-1",
                "title": "子卡",
                "waits_for": ("C-1",),
                "assignee": "dev-1",
            },
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {"card_id": "C-3", "board": "BD-1", "title": "占位卡"},
            BOARD_MANAGER,
        )
        # dev-1 claims C-1, completes it, then takes C-3 to hit wip_limit=1 while C-2 waits
        rt.submit_action(
            _next_run("KB"),
            "claim_card",
            {"card": "C-1", "member": "dev-1"},
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "record_outcome",
            {"card": "C-1", "member": "dev-1", "outcome": "completed", "summary": "done"},
            BOARD_MANAGER,
        )
        # C-2 auto-started on dev-1 when C-1 completed; release C-2 back to ready so dev-1 takes C-3
        rt.submit_action(
            _next_run("KB"),
            "release_claim",
            {"card": "C-2", "reason": "yield"},
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "claim_card",
            {"card": "C-3", "member": "dev-1"},
            BOARD_MANAGER,
        )
        # Reassign another pre-assigned ready card C-4 (created blocked by C-3 while dev-1 busy)
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {
                "card_id": "C-4",
                "board": "BD-1",
                "title": "子卡二",
                "waits_for": ("C-3",),
                "assignee": "dev-1",
            },
            BOARD_MANAGER,
        )
        # Complete C-3? Wait: if C-3 completes, dev-1 drops to WIP 0 and picks up C-4!
        # Instead hold dev-1 at WIP 1 via C-5 after creating C-4, and complete C-3?
        # Even simpler: complete C-3 on dev-1 (auto-starts C-4 on dev-1), then test assign_card
        # on an active doing card raises CARD_ALREADY_CLAIMED, and for ready preassigned:
        rt.submit_action(
            _next_run("KB"),
            "record_outcome",
            {"card": "C-3", "member": "dev-1", "outcome": "completed", "summary": "ok"},
            BOARD_MANAGER,
        )
        assert rt.get_object(Card, "C-4", BOARD_MANAGER).status == "doing"
        with pytest.raises(InvariantError) as exc_doing:
            rt.submit_action(
                _next_run("KB"),
                "assign_card",
                {"card": "C-4", "member": "dev-2"},
                BOARD_MANAGER,
            )
        assert exc_doing.value.code == "CARD_ALREADY_CLAIMED"

        # Now create C-6 depending on C-2, preassigned to dev-1 while dev-1 stays busy on C-4,
        # complete C-2 via dev-2 -> C-6 lands in (status="ready", assignee="dev-1")!
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {
                "card_id": "C-6",
                "board": "BD-1",
                "title": "待重派卡",
                "waits_for": ("C-2",),
                "assignee": "dev-1",
            },
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "claim_card",
            {"card": "C-2", "member": "dev-2"},
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "record_outcome",
            {"card": "C-2", "member": "dev-2", "outcome": "completed", "summary": "ok"},
            BOARD_MANAGER,
        )
        c6 = rt.get_object(Card, "C-6", BOARD_MANAGER)
        assert c6.status == "ready"
        assert c6.assignee == "dev-1"

        rt.submit_action(
            _next_run("KB"),
            "assign_card",
            {"card": "C-6", "member": "dev-2"},
            BOARD_MANAGER,
        )
        c6_after = rt.get_object(Card, "C-6", BOARD_MANAGER)
        assert c6_after.status == "doing"
        assert c6_after.assignee == "dev-2"
        rt.check_invariants()
    finally:
        rt.close()


def test_kanban_retry_exhausted_blocked_card_recovers_and_resets_failures():
    rt = _seed_kanban_runtime()
    try:
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {"card_id": "C-1", "board": "BD-1", "title": "重试卡"},
            BOARD_MANAGER,
        )
        rt.submit_action(
            _next_run("KB"),
            "create_card",
            {"card_id": "C-2", "board": "BD-1", "title": "重试卡二"},
            BOARD_MANAGER,
        )
        for cid in ("C-1", "C-2"):
            for _ in range(4):
                rt.submit_action(
                    _next_run("KB"),
                    "claim_card",
                    {"card": cid, "member": "dev-1"},
                    BOARD_MANAGER,
                )
                rt.submit_action(
                    _next_run("KB"),
                    "record_outcome",
                    {"card": cid, "member": "dev-1", "outcome": "failed", "summary": "fail"},
                    BOARD_MANAGER,
                )
            card = rt.get_object(Card, cid, BOARD_MANAGER)
            assert card.status == "blocked"
            assert card.block_kind is None
            assert card.consecutive_failures == 4

        # Path 1: unblock_card recovers retry-exhausted blocked card and resets failures
        rt.submit_action(_next_run("KB"), "unblock_card", {"card": "C-1"}, BOARD_MANAGER)
        c1 = rt.get_object(Card, "C-1", BOARD_MANAGER)
        assert c1.status == "ready"
        assert c1.consecutive_failures == 0

        # Path 2: move_card("ready") also resets consecutive_failures
        rt.submit_action(
            _next_run("KB"),
            "move_card",
            {"card": "C-2", "target_status": "ready"},
            BOARD_MANAGER,
        )
        c2 = rt.get_object(Card, "C-2", BOARD_MANAGER)
        assert c2.status == "ready"
        assert c2.consecutive_failures == 0
        rt.check_invariants()
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. Kanban stateful fuzzing + constructive resolution
# ---------------------------------------------------------------------------
def _topological_cards(rt: OntologyRuntime) -> list[Card]:
    cards = {c.card_id: c for c in rt.query_objects(Card, BOARD_MANAGER)}
    ordered: list[Card] = []
    visited: set[str] = set()

    def visit(card_id: str) -> None:
        if card_id in visited:
            return
        visited.add(card_id)
        card = cards[card_id]
        for parent_id in sorted(card.waits_for):
            if parent_id in cards:
                visit(parent_id)
        ordered.append(card)

    for cid in sorted(cards):
        visit(cid)
    return ordered


def _drain_doing_cards(rt: OntologyRuntime) -> None:
    while True:
        doing_cards = [
            c
            for c in rt.query_objects(Card, BOARD_MANAGER)
            if c.status == "doing"
        ]
        if not doing_cards:
            return
        for card in doing_cards:
            rt.submit_action(
                _next_run("RES"),
                "record_outcome",
                {
                    "card": card.card_id,
                    "member": card.assignee,
                    "outcome": "completed",
                    "summary": "resolved",
                },
                BOARD_MANAGER,
            )


def _constructively_resolve_all(rt: OntologyRuntime) -> None:
    """Resolve every card on the forked board to 'done' or 'triage'."""
    _drain_doing_cards(rt)
    for proto in _topological_cards(rt):
        _drain_doing_cards(rt)
        card = rt.get_object(Card, proto.card_id, BOARD_MANAGER)
        if card.status == "done":
            continue
        if card.status == "triage":
            rt.submit_action(
                _next_run("RES"),
                "move_card",
                {"card": card.card_id, "target_status": "todo"},
                BOARD_MANAGER,
            )
            card = rt.get_object(Card, card.card_id, BOARD_MANAGER)
        if card.status == "blocked":
            rt.submit_action(
                _next_run("RES"),
                "move_card",
                {"card": card.card_id, "target_status": "ready"},
                BOARD_MANAGER,
            )
            card = rt.get_object(Card, card.card_id, BOARD_MANAGER)
        if card.status == "todo":
            if card.assignee is not None and card.waits_for:
                rt.submit_action(
                    _next_run("RES"),
                    "promote_children",
                    {"card": card.waits_for[0]},
                    BOARD_MANAGER,
                )
            else:
                rt.submit_action(
                    _next_run("RES"),
                    "move_card",
                    {"card": card.card_id, "target_status": "ready"},
                    BOARD_MANAGER,
                )
            card = rt.get_object(Card, card.card_id, BOARD_MANAGER)
        if card.status == "ready":
            member_id = card.assignee or "dev-1"
            rt.submit_action(
                _next_run("RES"),
                "assign_card",
                {"card": card.card_id, "member": member_id},
                BOARD_MANAGER,
            )
        _drain_doing_cards(rt)

    for card in rt.query_objects(Card, BOARD_MANAGER):
        assert card.status in ("done", "triage"), (
            f"card {card.card_id} stuck in {card.status!r}"
        )
    rt.check_invariants()


def test_kanban_stateful_fuzzing():
    worker_actors = {
        "dev-1": Actor(actor_id="dev-1", role="developer", clearance_tags={"*"}),
        "dev-2": Actor(actor_id="dev-2", role="developer", clearance_tags={"*"}),
        "rev-1": Actor(actor_id="rev-1", role="reviewer", clearance_tags={"*"}),
        "tst-1": Actor(actor_id="tst-1", role="tester", clearance_tags={"*"}),
        "coord-1": Actor(actor_id="coord-1", role="coordinator", clearance_tags={"*"}),
    }
    member_ids = tuple(worker_actors)

    total_committed = 0
    for seed in range(8):
        rng = random.Random(2026 + seed)
        rt = _seed_kanban_runtime()
        try:
            # Seed two initial cards so the action space is immediately populated.
            for idx in range(2):
                rt.submit_action(
                    _next_run("FZ"),
                    "create_card",
                    {"board": "BD-1", "title": f"seed-{idx}", "priority": idx},
                    BOARD_MANAGER,
                )

            for step in range(150):
                cards = rt.query_objects(Card, BOARD_MANAGER)
                card_ids = [c.card_id for c in cards]
                card_id = rng.choice(card_ids)
                member_id = rng.choice(member_ids)
                actor = (
                    BOARD_MANAGER
                    if rng.random() < 0.7
                    else worker_actors[rng.choice(member_ids)]
                )

                op = rng.choices(
                    (
                        "create_card",
                        "claim_card",
                        "assign_card",
                        "move_card",
                        "release_claim",
                        "record_outcome",
                        "unblock_card",
                        "promote_children",
                        "add_comment",
                    ),
                    weights=(2, 3, 2, 2, 2, 4, 2, 1, 1),
                    k=1,
                )[0]

                if op == "create_card":
                    if len(card_ids) >= 10:
                        op = "claim_card"
                    else:
                        parent_count = rng.randint(0, min(2, len(card_ids)))
                        waits_for = tuple(rng.sample(card_ids, k=parent_count))
                        assignee = (
                            None if rng.random() < 0.6 else rng.choice(member_ids)
                        )
                        params = {
                            "board": "BD-1",
                            "title": f"fuzz-{seed}-{step}",
                            "priority": rng.randint(0, 10),
                            "waits_for": waits_for,
                            "assignee": assignee,
                        }
                if op == "claim_card":
                    params = {"card": card_id, "member": member_id}
                elif op == "assign_card":
                    params = {"card": card_id, "member": member_id}
                elif op == "move_card":
                    params = {
                        "card": card_id,
                        "target_status": rng.choice(CARD_STATUSES),
                        "reason": "fuzz",
                    }
                elif op == "release_claim":
                    params = {"card": card_id, "reason": "fuzz"}
                elif op == "record_outcome":
                    outcome = rng.choice(OUTCOMES)
                    block_kind = (
                        rng.choice(BLOCK_KINDS)
                        if outcome == "blocked" and rng.random() < 0.85
                        else None
                    )
                    params = {
                        "card": card_id,
                        "member": member_id,
                        "outcome": outcome,
                        "summary": "fuzz",
                        "block_kind": block_kind,
                    }
                elif op == "unblock_card":
                    params = {"card": card_id, "reason": "fuzz"}
                elif op == "promote_children":
                    params = {"card": card_id}
                elif op == "add_comment":
                    params = {"card": card_id, "author": member_id, "body": "note"}

                try:
                    rt.submit_action(_next_run("FZ"), op, params, actor)
                    total_committed += 1
                except (InvariantError, PermissionDeniedError):
                    pass

                rt.check_invariants()

            with rt.fork() as fork:
                _constructively_resolve_all(fork)
        finally:
            rt.close()

    assert total_committed > 150
