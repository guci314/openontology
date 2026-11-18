"""M2 持久化隐空间（LatentSpace）与棋步生命周期测试（I-005）。

覆盖：
(a) 隐空间落子与 ``PENDING_APPROVAL`` 审批期间，记忆世界 100% 零污染；
(b) 隐空间决定棋步后关闭进程，重开后 ``pending_moves()`` 仍保留棋步与 ``run_id``，
    经独立回读核对一致后正确提升进记忆世界；
(c) 独立回读不一致（设备未变、变成第三值、创建缺失、删除仍在、连边缺失）时，
    记忆世界事实零改动，写入 ``REJECTED``（``DEVICE_STATE_MISMATCH``）并自动 ``rebase()``；
(d) 后台 ``load_snapshot`` 抢跑收敛至预期终态时，不误报并发冲突并保留自有 overlay
    正常结算入账；唯有外部收敛至冲突第三值时才判 ``REJECTED``；
(e) ``affects_source=False`` 纯自有动作无需回读，直接校验提升进记忆并对齐隐空间；
(f) 既有 ``runtime.fork()`` 与 ``OntologyMCTSPlanner`` 内存海选推演与持久隐空间协同无回归。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from open_ontology import (
    DEVICE_STATE_MISMATCH,
    Actor,
    CandidateAction,
    GovernanceViolationError,
    InvariantError,
    LatentSpace,
    ObjectProperty,
    ObjectType,
    OntologyMCTSPlanner,
    OntologyRuntime,
    OntologySpec,
)


def _build_latent_spec() -> (
    tuple[OntologySpec, type[ObjectType], type[ObjectType], type[ObjectType]]
):
    spec = OntologySpec()

    class SalesOrder(ObjectType):
        __primary_key__ = "order_id"
        __title__ = "order_id"
        __owned__ = {"ops_note": None, "approval_tag": None}

        order_id: str
        status: str = "draft"
        qty: int = 1
        ops_note: str | None = None
        approval_tag: str | None = None

    class ShippingTag(ObjectType):
        __primary_key__ = "tag_id"
        __title__ = "label"
        __owned__ = False

        tag_id: str
        label: str

    class DecisionMemo(ObjectType):
        __primary_key__ = "memo_id"
        __title__ = "text"
        __owned__ = True

        memo_id: str
        text: str

    spec.object_type(SalesOrder)
    spec.object_type(ShippingTag)
    spec.object_type(DecisionMemo)
    spec.add_object_property(
        ObjectProperty(
            name="order_tags",
            domain="SalesOrder",
            range="ShippingTag",
            cardinality="many-to-many",
            owned=False,
        )
    )

    class ConfirmParams(BaseModel):
        order_id: str
        target_status: str = "confirmed"
        attach_tag_id: str | None = None

    class CreateOrderParams(BaseModel):
        order_id: str
        status: str = "draft"
        qty: int = 1

    class DeleteOrderParams(BaseModel):
        order_id: str

    class NoteParams(BaseModel):
        order_id: str
        ops_note: str
        memo_id: str | None = None

    class TwoPhaseParams(BaseModel):
        order_id: str
        target_status: str = "confirmed"
        approval_tag: str = "director-cleared"

    def _needs_approval(params: ConfirmParams, _actor: Actor) -> bool:
        return params.order_id.startswith("O-APPR")

    @spec.action(
        "confirm_order",
        ConfirmParams,
        allowed_roles=("ops", "director"),
        approver_roles=("director",),
        approval_rule=_needs_approval,
        affects_source=True,
    )
    def confirm_order(ctx: Any, params: ConfirmParams) -> None:
        doc = ctx.require(SalesOrder, params.order_id)
        if doc.qty <= 0:
            raise InvariantError("qty must be positive", code="NON_POSITIVE_QTY")
        ctx.modify(doc, status=params.target_status)
        if params.attach_tag_id:
            tag = ctx.require(ShippingTag, params.attach_tag_id)
            ctx.link(doc, "order_tags", tag)

    @spec.action(
        "create_order",
        CreateOrderParams,
        allowed_roles=("ops", "director"),
        affects_source=True,
    )
    def create_order(ctx: Any, params: CreateOrderParams) -> None:
        if params.qty <= 0:
            raise InvariantError("qty must be positive", code="NON_POSITIVE_QTY")
        ctx.create(
            SalesOrder(
                order_id=params.order_id,
                status=params.status,
                qty=params.qty,
            )
        )

    @spec.action(
        "delete_order",
        DeleteOrderParams,
        allowed_roles=("ops", "director"),
        affects_source=True,
    )
    def delete_order(ctx: Any, params: DeleteOrderParams) -> None:
        doc = ctx.require(SalesOrder, params.order_id)
        ctx.delete(doc)

    @spec.action(
        "annotate_order",
        NoteParams,
        allowed_roles=("ops", "director"),
        affects_source=False,
    )
    def annotate_order(ctx: Any, params: NoteParams) -> None:
        doc = ctx.require(SalesOrder, params.order_id)
        ctx.modify(doc, ops_note=params.ops_note)
        if params.memo_id:
            ctx.create(DecisionMemo(memo_id=params.memo_id, text=params.ops_note))

    def _post_approval_stamp(ctx: Any, params: TwoPhaseParams) -> None:
        doc = ctx.require(SalesOrder, params.order_id)
        ctx.modify(doc, approval_tag=params.approval_tag)

    @spec.action(
        "confirm_with_stamp",
        TwoPhaseParams,
        allowed_roles=("ops", "director"),
        approver_roles=("director",),
        approval_rule=lambda _p, _a: False,
        affects_source=True,
        post_approval=_post_approval_stamp,
    )
    def confirm_with_stamp(ctx: Any, params: TwoPhaseParams) -> None:
        doc = ctx.require(SalesOrder, params.order_id)
        ctx.modify(doc, status=params.target_status)

    @spec.invariant(
        "order_qty_positive",
        code="ORDER_QTY_POSITIVE",
        description="已确认订单数量必须为正数",
    )
    def _inv_positive_qty(view: Any) -> bool:
        return all(
            o.qty > 0 for o in view.query(SalesOrder) if o.status == "confirmed"
        )

    class EmptyParams(BaseModel):
        pass

    @spec.function(
        "count_confirmed_orders",
        EmptyParams,
        description="统计已确认订单数",
    )
    def count_confirmed_orders(view: Any, _params: EmptyParams) -> int:
        return sum(1 for o in view.query(SalesOrder) if o.status == "confirmed")

    return spec, SalesOrder, ShippingTag, DecisionMemo


OPS = Actor(actor_id="ops-1", role="ops")
DIRECTOR = Actor(actor_id="dir-1", role="director")


def _table_names(runtime: OntologyRuntime) -> set[str]:
    return {
        str(row[0])
        for row in runtime.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _seed_world(rt: OntologyRuntime) -> None:
    rt.load_snapshot(
        {
            "SalesOrder": [
                {"order_id": "O-1", "status": "draft", "qty": 5},
                {"order_id": "O-2", "status": "draft", "qty": 3},
                {"order_id": "O-APPR-1", "status": "draft", "qty": 8},
                {"order_id": "O-BAD", "status": "draft", "qty": 0},
            ],
            "ShippingTag": [
                {"tag_id": "T-EXP", "label": "加急"},
            ],
        },
        links={"order_tags": []},
    )


def test_stage_and_approval_lifecycle_leaves_memory_zero_polluted(tmp_path):
    """(a) 隐空间落子与 PENDING_APPROVAL 审批期间，记忆世界 100% 零污染且严守 6 表同构。"""
    spec, SalesOrder, _, _ = _build_latent_spec()
    mem_db = tmp_path / "memory.sqlite3"
    latent_db = tmp_path / "latent.sqlite3"

    mem = OntologyRuntime(spec, db_path=mem_db)
    _seed_world(mem)
    mem_fp_before = mem.state_fingerprint()
    mem_runs_before = mem.list_action_runs()

    with mem.open_latent_space(latent_db) as latent:
        expected_tables = {
            "ontology_write_gate",
            "ontology_registry",
            "ontology_objects",
            "ontology_links",
            "ontology_action_runs",
            "ontology_overlay",
        }
        assert _table_names(mem) == expected_tables
        assert _table_names(latent.latent_runtime) == expected_tables

        # 隐空间拒绝 `:memory:`
        with pytest.raises(ValueError, match="persistent"):
            LatentSpace(mem, ":memory:")

        # 1) 隐空间直接落子
        r1 = latent.stage_move(
            "M-1", "confirm_order", {"order_id": "O-1"}, OPS
        )
        assert r1["status"] == "COMMITTED"
        assert latent.latent_runtime.get_object(SalesOrder, "O-1", OPS).status == "confirmed"

        # 2) 隐空间不变量拦截照常走 REJECTED（仅在隐空间内留痕）
        with pytest.raises(InvariantError, match="positive|已确认订单数量"):
            latent.stage_move("M-BAD", "confirm_order", {"order_id": "O-BAD"}, OPS)
        assert latent.latent_runtime.get_action_run("M-BAD")["status"] == "REJECTED"

        # 3) 隐空间审批挂起 + 驳回 + 再批准，记忆世界全过程 0 写入
        r_pend = latent.stage_move(
            "M-APPR", "confirm_order", {"order_id": "O-APPR-1"}, OPS
        )
        assert r_pend["status"] == "PENDING_APPROVAL"
        assert latent.latent_runtime.get_object(SalesOrder, "O-APPR-1", OPS).status == "draft"
        assert mem.state_fingerprint() == mem_fp_before
        assert mem.list_action_runs() == mem_runs_before

        # 尚未批准的 PENDING_APPROVAL 与 REJECTED 都不进入待执行 pending_moves
        assert [m["run_id"] for m in latent.pending_moves()] == ["M-1"]

        latent.approve_move("M-APPR", DIRECTOR)
        assert (
            latent.latent_runtime.get_object(SalesOrder, "O-APPR-1", OPS).status
            == "confirmed"
        )
        assert [m["run_id"] for m in latent.pending_moves()] == ["M-1", "M-APPR"]

        # 4) 另一种挂起走 reject_move
        r_rej = latent.stage_move(
            "M-APPR-REJ",
            "confirm_order",
            {"order_id": "O-APPR-1", "target_status": "shipped"},
            OPS,
        )
        # 在隐空间内再改回 shipped 需要审批或改其它订单
        assert r_rej["status"] == "PENDING_APPROVAL"
        latent.reject_move("M-APPR-REJ", DIRECTOR, reason="预算冻结")
        assert latent.latent_runtime.get_action_run("M-APPR-REJ")["status"] == "REJECTED"

        # 记忆世界依然 100% 零污染
        assert mem.get_object(SalesOrder, "O-1", OPS).status == "draft"
        assert mem.get_object(SalesOrder, "O-APPR-1", OPS).status == "draft"
        assert mem.state_fingerprint() == mem_fp_before
        assert mem.list_action_runs() == mem_runs_before
    mem.close()


def test_pending_moves_survive_restart_and_promote_on_matching_readback(tmp_path):
    """(b) 重启重开保留隐空间待执行棋步，独立回读核对一致后提升进记忆世界。"""
    spec, SalesOrder, ShippingTag, _ = _build_latent_spec()
    mem_db = tmp_path / "memory.sqlite3"
    latent_db = tmp_path / "latent.sqlite3"

    # 会话 1：在隐空间决定两步外部行动（更新 + 加连边，以及建单），未结算直接关闭
    mem1 = OntologyRuntime(spec, db_path=mem_db)
    _seed_world(mem1)
    with mem1.open_latent_space(latent_db) as lat1:
        lat1.stage_move(
            "M-CONF-LINK",
            "confirm_order",
            {"order_id": "O-1", "attach_tag_id": "T-EXP"},
            OPS,
        )
        lat1.stage_move(
            "M-CREATE-O9",
            "create_order",
            {"order_id": "O-9", "status": "draft", "qty": 9},
            OPS,
        )
        # 自有属性写入在隐空间里，外部行动未执行且未结算前，记忆世界保持不变
        assert mem1.get_object(SalesOrder, "O-1", OPS).status == "draft"
        with pytest.raises(Exception):
            mem1.get_object(SalesOrder, "O-9", OPS)
    mem1.close()

    # 会话 2：重开持久库与持久隐空间；记忆库多一个不影响棋步的备注，重开绝不覆盖隐空间
    mem2 = OntologyRuntime(spec, db_path=mem_db)
    try:
        lat2 = LatentSpace(mem2, latent_db)
        try:
            moves = lat2.pending_moves()
            assert [m["run_id"] for m in moves] == ["M-CONF-LINK", "M-CREATE-O9"]
            assert moves[0]["action_type"] == "confirm_order"
            assert moves[0]["params"] == {
                "order_id": "O-1",
                "target_status": "confirmed",
                "attach_tag_id": "T-EXP",
            }
            assert moves[0]["actor"] == {"actor_id": "ops-1", "role": "ops"}
            assert moves[0]["affects_source"] is True
            assert moves[0]["object_diff"]["link_changes"][0]["target_id"] == "T-EXP"

            # 外部动作需要独立回读：不传 readback_objects 直接 settle 必须拒绝
            with pytest.raises(GovernanceViolationError, match="readback"):
                lat2.settle_move("M-CONF-LINK")

            # 按外部通道独立回读（符合领域归一化规则）核对并结算第 1 步
            settled1 = lat2.settle_move(
                "M-CONF-LINK",
                readback_objects={
                    "SalesOrder": [
                        {"order_id": "O-1", "status": "confirmed", "qty": 5},
                        {"order_id": "O-2", "status": "draft", "qty": 3},
                    ],
                    "ShippingTag": [{"tag_id": "T-EXP", "label": "加急"}],
                },
                readback_links={"order_tags": [("O-1", "T-EXP")]},
            )
            assert settled1["status"] == "COMMITTED"
            assert mem2.get_object(SalesOrder, "O-1", OPS).status == "confirmed"
            linked_tags = mem2.get_linked(
                mem2.get_object(SalesOrder, "O-1", OPS),
                "order_tags",
                ShippingTag,
                OPS,
            )
            assert [t.tag_id for t in linked_tags] == ["T-EXP"]
            # M-CREATE-O9 尚未结算，rebase 不得误清它
            assert [m["run_id"] for m in lat2.pending_moves()] == ["M-CREATE-O9"]

            # 结算第 2 步：独立回读确认 O-9 已在外部建成
            settled2 = lat2.settle_move(
                "M-CREATE-O9",
                readback_objects={
                    "SalesOrder": [
                        {"order_id": "O-1", "status": "confirmed", "qty": 5},
                        {"order_id": "O-9", "status": "draft", "qty": 9},
                    ]
                },
            )
            assert settled2["status"] == "COMMITTED"
            assert mem2.get_object(SalesOrder, "O-9", OPS).qty == 9
            assert lat2.pending_moves() == []

            # 幂等重入 settle_move 与 stage_move
            assert lat2.settle_move("M-CONF-LINK")["run_id"] == "M-CONF-LINK"
            assert (
                lat2.stage_move("M-CONF-LINK", "confirm_order", {"order_id": "O-1"}, OPS)[
                    "status"
                ]
                == "COMMITTED"
            )
        finally:
            lat2.close()
    finally:
        mem2.close()


@pytest.mark.parametrize(
    "case_name, action_name, params, readback_objs, readback_links",
    [
        (
            "device-unchanged",
            "confirm_order",
            {"order_id": "O-1"},
            {"SalesOrder": [{"order_id": "O-1", "status": "draft", "qty": 5}]},
            None,
        ),
        (
            "third-value-drift",
            "confirm_order",
            {"order_id": "O-1"},
            {"SalesOrder": [{"order_id": "O-1", "status": "cancelled", "qty": 5}]},
            None,
        ),
        (
            "missing-link",
            "confirm_order",
            {"order_id": "O-1", "attach_tag_id": "T-EXP"},
            {"SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}]},
            {"order_tags": []},
        ),
        (
            "created-object-missing",
            "create_order",
            {"order_id": "O-99", "qty": 2},
            {"SalesOrder": [{"order_id": "O-1", "status": "draft", "qty": 5}]},
            None,
        ),
        (
            "deleted-object-still-present",
            "delete_order",
            {"order_id": "O-2"},
            {"SalesOrder": [{"order_id": "O-2", "status": "draft", "qty": 3}]},
            None,
        ),
    ],
)
def test_readback_mismatch_records_rejected_and_rebases_latent_space(
    tmp_path, case_name, action_name, params, readback_objs, readback_links
):
    """(c) 独立回读与预期终态不一致时，记忆零改动、记 REJECTED(DEVICE_STATE_MISMATCH) 并自动 rebase。"""
    spec, SalesOrder, _, _ = _build_latent_spec()
    mem = OntologyRuntime(spec, db_path=tmp_path / f"{case_name}_mem.sqlite3")
    _seed_world(mem)
    run_id = f"M-FAIL-{case_name}"

    with mem.open_latent_space(tmp_path / f"{case_name}_lat.sqlite3") as latent:
        latent.stage_move(run_id, action_name, params, OPS)
        mem_before = mem.state_fingerprint()

        res = latent.settle_move(
            run_id,
            readback_objects=readback_objs,
            readback_links=readback_links,
        )
        assert res["status"] == "REJECTED"
        assert res["error_code"] == DEVICE_STATE_MISMATCH
        assert res["error_message"]

        # 记忆事实、版本号、连边、overlay 零改动（仅 ontology_action_runs 追加 1 条 REJECTED）
        mem_after = mem.state_fingerprint()
        assert mem_after["versions"] == mem_before["versions"]
        assert mem_after["objects"] == mem_before["objects"]
        assert mem_after["links"] == mem_before["links"]
        assert mem_after["overlay"] == mem_before["overlay"]
        assert mem_after["runs"] == mem_before["runs"] + 1
        assert mem.get_object(SalesOrder, "O-1", OPS).status == "draft"

        # 隐空间已自动 rebase 回最新记忆状态
        assert latent.pending_moves() == []
        assert latent.latent_runtime.state_fingerprint() == mem.state_fingerprint()
        if action_name == "create_order":
            with pytest.raises(Exception):
                latent.latent_runtime.get_object(SalesOrder, "O-99", OPS)
        elif action_name == "delete_order":
            assert latent.latent_runtime.get_object(SalesOrder, "O-2", OPS).qty == 3
        else:
            assert latent.latent_runtime.get_object(SalesOrder, "O-1", OPS).status == "draft"
    mem.close()


def test_snapshot_front_running_convergence_and_conflicting_third_state(tmp_path):
    """(d) 后台快照抢跑已收敛至同一预期终态时不误报 OCC；唯有收敛到冲突第三值才判 REJECTED。"""
    spec, SalesOrder, _, _ = _build_latent_spec()

    # 场景 1：update + create + delete + 两相 post_approval 自有字段，期间被快照抢跑刷到同终态
    mem = OntologyRuntime(spec, db_path=tmp_path / "converged_mem.sqlite3")
    _seed_world(mem)
    mem.submit_action(
        "MEM-NOTE",
        "annotate_order",
        {"order_id": "O-1", "ops_note": "keep-vip"},
        OPS,
    )
    with mem.open_latent_space(tmp_path / "converged_lat.sqlite3") as latent:
        latent.stage_move(
            "M-TWO-PHASE",
            "confirm_with_stamp",
            {"order_id": "O-1", "approval_tag": "stamped-by-dir"},
            DIRECTOR,
        )
        latent.stage_move(
            "M-CREATE-CONV",
            "create_order",
            {"order_id": "O-10", "status": "confirmed", "qty": 10},
            OPS,
        )
        latent.stage_move(
            "M-DEL-CONV",
            "delete_order",
            {"order_id": "O-2"},
            OPS,
        )

        # 结算前，后台 load_snapshot 抢跑先把记忆刷成了相同的外部终态（且版本号递增，同时附带新 qty=50）
        mem.load_snapshot(
            {
                "SalesOrder": [
                    {"order_id": "O-1", "status": "confirmed", "qty": 50},
                    {"order_id": "O-APPR-1", "status": "draft", "qty": 8},
                    {"order_id": "O-BAD", "status": "draft", "qty": 0},
                    {"order_id": "O-10", "status": "confirmed", "qty": 10},
                ],
                "ShippingTag": [{"tag_id": "T-EXP", "label": "加急"}],
            },
            links={"order_tags": []},
        )
        assert mem.load_object("SalesOrder", "O-1").version >= 3

        # settle_move 不得因 version 递增误报 ConcurrencyConflictError，且要补齐自有字段 + COMMITTED
        r_tp = latent.settle_move(
            "M-TWO-PHASE",
            readback_objects={
                "SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 50}]
            },
        )
        assert r_tp["status"] == "COMMITTED"
        o1 = mem.get_object(SalesOrder, "O-1", OPS)
        assert o1.status == "confirmed"
        assert o1.qty == 50  # 快照刷新的无冲突源端列未被覆盖倒退
        assert o1.ops_note == "keep-vip"
        assert o1.approval_tag == "stamped-by-dir"

        r_cr = latent.settle_move(
            "M-CREATE-CONV",
            readback_objects={
                "SalesOrder": [{"order_id": "O-10", "status": "confirmed", "qty": 10}]
            },
        )
        assert r_cr["status"] == "COMMITTED"

        r_del = latent.settle_move(
            "M-DEL-CONV",
            readback_objects={"SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 50}]},
        )
        assert r_del["status"] == "COMMITTED"
        assert latent.pending_moves() == []
    mem.close()

    # 场景 2：记忆已被后台快照刷到冲突第三值 cancelled，必须拒绝并保留 cancelled
    mem_c = OntologyRuntime(spec, db_path=tmp_path / "conflict_mem.sqlite3")
    _seed_world(mem_c)
    with mem_c.open_latent_space(tmp_path / "conflict_lat.sqlite3") as lat_c:
        lat_c.stage_move("M-CONFLICT", "confirm_order", {"order_id": "O-1"}, OPS)
        mem_c.load_snapshot(
            {
                "SalesOrder": [
                    {"order_id": "O-1", "status": "cancelled", "qty": 5},
                    {"order_id": "O-2", "status": "draft", "qty": 3},
                ]
            }
        )
        res = lat_c.settle_move(
            "M-CONFLICT",
            readback_objects={
                "SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}]
            },
        )
        assert res["status"] == "REJECTED"
        assert res["error_code"] == DEVICE_STATE_MISMATCH
        assert mem_c.get_object(SalesOrder, "O-1", OPS).status == "cancelled"
        assert lat_c.latent_runtime.get_object(SalesOrder, "O-1", OPS).status == "cancelled"
    mem_c.close()


def test_pure_ontology_owned_move_settles_without_readback(tmp_path):
    """(e) affects_source=False 自有动作无需外部回读，直接校验提升入记忆并对齐隐空间。"""
    spec, SalesOrder, _, DecisionMemo = _build_latent_spec()
    mem = OntologyRuntime(spec, db_path=tmp_path / "owned_mem.sqlite3")
    _seed_world(mem)

    with mem.open_latent_space(tmp_path / "owned_lat.sqlite3") as latent:
        latent.stage_move(
            "M-OWNED-1",
            "annotate_order",
            {"order_id": "O-1", "ops_note": "priority-vip", "memo_id": "MEMO-1"},
            OPS,
        )
        assert [m["affects_source"] for m in latent.pending_moves()] == [False]
        assert mem.get_object(SalesOrder, "O-1", OPS).ops_note is None

        # 不传任何 readback_objects，直接提升进记忆
        settled = latent.settle_move("M-OWNED-1")
        assert settled["status"] == "COMMITTED"
        assert mem.get_object(SalesOrder, "O-1", OPS).ops_note == "priority-vip"
        assert mem.get_object(DecisionMemo, "MEMO-1", OPS).text == "priority-vip"
        assert latent.pending_moves() == []
        assert latent.latent_runtime.state_fingerprint() == mem.state_fingerprint()
    mem.close()


def test_in_memory_fork_and_mcts_coexist_with_latent_space_without_regression(
    tmp_path,
):
    """(f) 既有 fork() 与 OntologyMCTSPlanner 内存海选推演正常，零污染后可落子进持久隐空间。"""
    spec, SalesOrder, _, _ = _build_latent_spec()
    mem = OntologyRuntime(spec, db_path=tmp_path / "mcts_mem.sqlite3")
    _seed_world(mem)
    mem_fp = mem.state_fingerprint()

    # fork() 依然返回内存隔离沙盘
    mem_fork = mem.fork()
    assert mem_fork.db_path == ":memory:"
    mem_fork.close()

    def _candidates(rt: OntologyRuntime, step_idx: int) -> list[CandidateAction]:
        if step_idx > 0:
            return []
        return [
            CandidateAction("confirm_order", {"order_id": "O-BAD"}, prior=0.9),
            CandidateAction("confirm_order", {"order_id": "O-1"}, prior=0.5),
            CandidateAction("confirm_order", {"order_id": "O-2"}, prior=0.4),
        ]

    with mem.open_latent_space(tmp_path / "mcts_lat.sqlite3") as latent:
        lat_fp = latent.latent_runtime.state_fingerprint()
        with OntologyMCTSPlanner(
            mem,
            OPS,
            candidate_generator=_candidates,
            value_function="count_confirmed_orders",
            simulations=12,
            max_steps=1,
            seed=7,
        ) as planner:
            plan = planner.search()

        # 非法候选 O-BAD 被内存 fork 剪枝，主记忆与隐空间均 0 污染
        assert plan.best_plan
        assert plan.best_plan[0].params["order_id"] in ("O-1", "O-2")
        assert mem.state_fingerprint() == mem_fp
        assert latent.latent_runtime.state_fingerprint() == lat_fp

        # 选用 MCTS 最优着法落入持久隐空间 -> 独立回读 -> 提升进记忆
        best = plan.best_plan[0]
        latent.stage_move("M-MCTS-1", best.action, best.params, OPS)
        assert len(latent.pending_moves()) == 1
        target_id = best.params["order_id"]
        settled = latent.settle_move(
            "M-MCTS-1",
            readback_objects={
                "SalesOrder": [
                    {
                        "order_id": target_id,
                        "status": "confirmed",
                        "qty": mem.get_object(SalesOrder, target_id, OPS).qty,
                    }
                ]
            },
        )
        assert settled["status"] == "COMMITTED"
        assert mem.get_object(SalesOrder, target_id, OPS).status == "confirmed"
    mem.close()
