"""统一同态闭环结算测试（HomomorphicSettlementAspect + WorldMode + SourceBridge）。

替代原持久隐空间队列测试，端到端验证统一进 ``OntologyRuntime`` 默认动作管线后的核心不变量：
(a) ``coupled`` 模式未绑定 ``SourceBridge`` 时 fail-closed（``UNBOUND_SOURCE_BRIDGE``），
    而 ``affects_source=False`` 纯自有动作照常本地原子提交；
(b) 撒谎执行器（``actuate`` 伪造成功返回值而物理状态未变/变错）在 **3 个公开入口**
    （``runtime.submit_action`` / ``WorkshopServer`` HTTP ``/api/submit`` /
    ``AIPAgentExecutor``）均触发 ``DEVICE_STATE_MISMATCH``，内存世界零改动；
(c) Phase A 先于外部驱动校验不变量与 OCC 版本，并发漂移或前置不变量失败直接 abort 且
    ``actuate_calls == 0``；
(d) Phase B 无锁脱离 ``_write_gate()``：后台 ``load_snapshot`` 抢跑收敛至同终态时不误报并发冲突，
    唯有收敛至冲突第三值时判 ``REJECTED(DEVICE_STATE_MISMATCH)``；
(e) ``runtime.fork()`` 强制 ``world_mode='counterfactual'`` 与 ``source_bridge=None``，
    沙盘与 MCTS 推演期间对外部驱动 100% 物理绝缘，且禁止绑定 ``SourceBridge``；
(f) ``revert_action`` 在 ``coupled`` 模式下默认禁止触及外部源（``SOURCE_REVERT_FORBIDDEN``），
    显式启用 ``allow_source_revert=True`` 后走同等三阶段逆向闭环核销。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import BaseModel

from open_ontology import (
    DEVICE_STATE_MISMATCH,
    SOURCE_REVERT_FORBIDDEN,
    UNBOUND_SOURCE_BRIDGE,
    AIPAgentExecutor,
    Actor,
    ConcurrencyConflictError,
    DeviceStateMismatchError,
    GovernanceViolationError,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    SourceBridge,
)
from open_ontology.mcts import CandidateAction, OntologyMCTSPlanner
from open_ontology.workshop import start_workshop_server


def _build_settlement_spec() -> (
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

    @spec.action(
        "confirm_order",
        ConfirmParams,
        allowed_roles=("ops", "director"),
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


def _post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# (a) coupled fail-closed + affects_source=False 本地提交 + 无额外 SQLite 表
# ---------------------------------------------------------------------------
def test_coupled_unbound_bridge_fails_closed_while_owned_commits(tmp_path):
    spec, SalesOrder, _, DecisionMemo = _build_settlement_spec()
    rt = OntologyRuntime(
        spec, db_path=tmp_path / "coupled.sqlite3", world_mode="coupled"
    )
    _seed_world(rt)
    try:
        tables = {
            str(r[0])
            for r in rt.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {
            "ontology_write_gate",
            "ontology_registry",
            "ontology_objects",
            "ontology_links",
            "ontology_action_runs",
            "ontology_overlay",
        }
        assert list(tmp_path.glob("*lat*")) == []

        with pytest.raises(GovernanceViolationError) as exc_info:
            rt.submit_action("RUN-UNBOUND", "confirm_order", {"order_id": "O-1"}, OPS)
        assert exc_info.value.code == UNBOUND_SOURCE_BRIDGE
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "draft"

        # affects_source=False 纯自有动作不依赖外部 SourceBridge，直接原子提交
        owned = rt.submit_action(
            "RUN-OWNED",
            "annotate_order",
            {"order_id": "O-1", "ops_note": "priority-vip", "memo_id": "MEMO-1"},
            OPS,
        )
        assert owned["status"] == "COMMITTED"
        assert rt.get_object(SalesOrder, "O-1", OPS).ops_note == "priority-vip"
        assert rt.get_object(DecisionMemo, "MEMO-1", OPS).text == "priority-vip"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# (b) 撒谎执行器在全部 4 个入口均被 DEVICE_STATE_MISMATCH 拦下且零污染
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("case_name", "action_name", "params", "readback_objs", "readback_links"),
    [
        (
            "unchanged_state",
            "confirm_order",
            {"order_id": "O-1"},
            {"SalesOrder": [{"order_id": "O-1", "status": "draft", "qty": 5}]},
            None,
        ),
        (
            "wrong_third_state",
            "confirm_order",
            {"order_id": "O-1"},
            {"SalesOrder": [{"order_id": "O-1", "status": "cancelled", "qty": 5}]},
            None,
        ),
        (
            "created_missing",
            "create_order",
            {"order_id": "O-99", "status": "draft", "qty": 2},
            {"SalesOrder": [{"order_id": "O-1", "status": "draft", "qty": 5}]},
            None,
        ),
        (
            "deleted_still_present",
            "delete_order",
            {"order_id": "O-2"},
            {"SalesOrder": [{"order_id": "O-2", "status": "draft", "qty": 3}]},
            None,
        ),
        (
            "missing_link_after_confirm",
            "confirm_order",
            {"order_id": "O-1", "attach_tag_id": "T-EXP"},
            {
                "SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}],
                "ShippingTag": [{"tag_id": "T-EXP", "label": "加急"}],
            },
            {"order_tags": []},
        ),
    ],
)
def test_readback_mismatch_matrix_rejects_and_preserves_memory(
    tmp_path, case_name, action_name, params, readback_objs, readback_links
):
    spec, SalesOrder, _, _ = _build_settlement_spec()
    bridge = SourceBridge(
        actuate=lambda _mv: {"ok": True, "claimed": "done"},
        observe=lambda: (readback_objs, readback_links)
        if readback_links is not None
        else readback_objs,
    )
    rt = OntologyRuntime(
        spec,
        db_path=tmp_path / f"{case_name}.sqlite3",
        world_mode="coupled",
        source_bridge=bridge,
    )
    _seed_world(rt)
    before = rt.state_fingerprint()
    try:
        run_id = f"FAIL-{case_name}"
        with pytest.raises(DeviceStateMismatchError) as exc_info:
            rt.submit_action(run_id, action_name, params, OPS)
        assert exc_info.value.code == DEVICE_STATE_MISMATCH
        assert exc_info.value.readback_objects is not None

        run = rt.get_action_run(run_id)
        assert run is not None and run["status"] == "REJECTED"
        assert run["error_code"] == DEVICE_STATE_MISMATCH

        after = rt.state_fingerprint()
        assert after["versions"] == before["versions"]
        assert after["objects"] == before["objects"]
        assert after["links"] == before["links"]
        assert after["overlay"] == before["overlay"]
        assert after["runs"] == before["runs"] + 1
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "draft"
    finally:
        rt.close()


def test_lying_actuator_caught_across_submit_workshop_and_osdk(tmp_path):
    spec, SalesOrder, _, _ = _build_settlement_spec()
    lying_bridge = SourceBridge(
        actuate=lambda _mv: {"ok": True, "fake_success": True},
        observe=lambda: {
            "SalesOrder": [
                {"order_id": "O-1", "status": "draft", "qty": 5},
                {"order_id": "O-APPR-1", "status": "draft", "qty": 8},
            ]
        },
    )
    rt = OntologyRuntime(
        spec,
        db_path=tmp_path / "entrypoints.sqlite3",
        world_mode="coupled",
        source_bridge=lying_bridge,
    )
    _seed_world(rt)
    handle = start_workshop_server(rt, actor=OPS, port=0)
    try:
        # 1) Direct runtime.submit_action 入口捕获撒谎执行器
        with pytest.raises(DeviceStateMismatchError):
            rt.submit_action("SUB-LIE", "confirm_order", {"order_id": "O-1"}, OPS)
        assert rt.get_action_run("SUB-LIE")["status"] == "REJECTED"
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "draft"

        # 2) Workshop HTTP /api/submit 入口捕获并返回非 200 结构化错误码
        st_sub, body_sub = _post_json(
            handle.base_url + "/api/submit",
            {"run_id": "WS-LIE", "action": "confirm_order", "params": {"order_id": "O-1"}},
        )
        assert st_sub == 400
        assert body_sub["ok"] is False
        assert body_sub["error_code"] == DEVICE_STATE_MISMATCH
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "draft"

        # 3) AIPAgentExecutor (OSDK) 入口抛出 DeviceStateMismatchError 且阻断多步推演混淆
        executor = AIPAgentExecutor(rt, OPS)
        with pytest.raises(DeviceStateMismatchError) as osdk_exc:
            executor.dispatch(
                "submit_action",
                {"run_id": "OSDK-LIE", "action": "confirm_order", "params": {"order_id": "O-1"}},
            )
        assert osdk_exc.value.code == DEVICE_STATE_MISMATCH
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "draft"
    finally:
        handle.stop()
        rt.close()


# ---------------------------------------------------------------------------
# (c) Phase A 先检不变量与 OCC，失败绝不触碰外部执行器
# ---------------------------------------------------------------------------
def test_phase_a_validation_and_occ_abort_before_actuate(tmp_path):
    spec, SalesOrder, _, _ = _build_settlement_spec()
    act_calls = 0

    def _actuate(_mv: dict[str, Any]) -> None:
        nonlocal act_calls
        act_calls += 1

    rt = OntologyRuntime(
        spec,
        db_path=tmp_path / "occ_appr.sqlite3",
        world_mode="coupled",
        source_bridge=SourceBridge(
            actuate=_actuate,
            observe=lambda: {"SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}]},
        ),
    )
    _seed_world(rt)
    try:
        with pytest.raises(InvariantError):
            rt.submit_action("BAD-PHASE-A", "confirm_order", {"order_id": "O-BAD"}, OPS)
        assert act_calls == 0

        class _BumpParams(BaseModel):
            order_id: str

        @spec.action("confirm_with_concurrent_snapshot", _BumpParams, allowed_roles=("ops",), affects_source=True)
        def _confirm_race(ctx: Any, params: _BumpParams) -> None:
            doc = ctx.require(SalesOrder, params.order_id)
            rt.load_snapshot({"SalesOrder": [{"order_id": params.order_id, "status": "draft", "qty": 99}]})
            ctx.modify(doc, status="confirmed")

        with pytest.raises(ConcurrencyConflictError):
            rt.submit_action("OCC-RACE", "confirm_with_concurrent_snapshot", {"order_id": "O-1"}, OPS)
        assert act_calls == 0
        assert rt.get_object(SalesOrder, "O-1", OPS).qty == 99
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "draft"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# (d) Phase B 无锁脱离 _write_gate() + Phase C 收敛合并与冲突判据
# ---------------------------------------------------------------------------
def test_phase_b_runs_lock_free_and_converges_on_background_snapshot(tmp_path):
    spec, SalesOrder, _, _ = _build_settlement_spec()
    rt = OntologyRuntime(spec, db_path=tmp_path / "conv.sqlite3", world_mode="coupled")
    _seed_world(rt)
    rt.submit_action(
        "MEM-NOTE", "annotate_order", {"order_id": "O-1", "ops_note": "keep-vip"}, OPS
    )

    entered_actuate = threading.Event()
    allow_actuate_return = threading.Event()
    concurrent_read_ok: list[bool] = []
    ext_orders = {
        "O-1": {"order_id": "O-1", "status": "draft", "qty": 5},
        "O-2": {"order_id": "O-2", "status": "draft", "qty": 3},
        "O-APPR-1": {"order_id": "O-APPR-1", "status": "draft", "qty": 8},
        "O-BAD": {"order_id": "O-BAD", "status": "draft", "qty": 0},
    }

    def _actuate(move: dict[str, Any]) -> None:
        entered_actuate.set()
        allow_actuate_return.wait(timeout=3.0)
        act = move["action_type"]
        p = move["params"]
        if act in ("confirm_with_stamp", "confirm_order"):
            ext_orders[p["order_id"]] = {"order_id": p["order_id"], "status": "confirmed", "qty": 50}
        elif act == "create_order":
            ext_orders[p["order_id"]] = {"order_id": p["order_id"], "status": p["status"], "qty": p["qty"]}
        elif act == "delete_order":
            ext_orders.pop(p["order_id"], None)

    rt.bind_source_bridge(
        SourceBridge(
            actuate=_actuate,
            observe=lambda: {
                "SalesOrder": list(ext_orders.values()),
                "ShippingTag": [{"tag_id": "T-EXP", "label": "加急"}],
            },
        )
    )
    try:
        err_box: list[BaseException] = []

        def _worker() -> None:
            try:
                rt.submit_action(
                    "CONV-1",
                    "confirm_order",
                    {"order_id": "O-1"},
                    DIRECTOR,
                )
            except BaseException as exc:  # pragma: no cover
                err_box.append(exc)

        t = threading.Thread(target=_worker)
        t.start()
        assert entered_actuate.wait(timeout=2.0)

        # Phase B 期间另一线程执行读操作和 load_snapshot 抢跑都不应被 SQLite 写锁阻塞死锁
        concurrent_read_ok.append(rt.get_object(SalesOrder, "O-1", OPS).status == "draft")
        rt.load_snapshot(
            {
                "SalesOrder": [
                    {"order_id": "O-1", "status": "confirmed", "qty": 50},
                    {"order_id": "O-2", "status": "draft", "qty": 3},
                    {"order_id": "O-APPR-1", "status": "draft", "qty": 8},
                    {"order_id": "O-BAD", "status": "draft", "qty": 0},
                ],
                "ShippingTag": [{"tag_id": "T-EXP", "label": "加急"}],
            },
            links={"order_tags": []},
        )
        allow_actuate_return.set()
        t.join(timeout=3.0)

        assert err_box == []
        assert concurrent_read_ok == [True]
        o1 = rt.get_object(SalesOrder, "O-1", OPS)
        assert o1.status == "confirmed"
        assert o1.qty == 50
        assert o1.ops_note == "keep-vip"
    finally:
        rt.close()

    # 冲突第三值：Phase B 期间内存被后台快照改到 cancelled，即便外部回读 confirmed 也必须拒签
    rt_conf = OntologyRuntime(spec, db_path=tmp_path / "conflict.sqlite3", world_mode="coupled")
    _seed_world(rt_conf)

    def _drift_act(_mv: dict[str, Any]) -> None:
        rt_conf.load_snapshot(
            {
                "SalesOrder": [
                    {"order_id": "O-1", "status": "cancelled", "qty": 5},
                    {"order_id": "O-2", "status": "draft", "qty": 3},
                ]
            }
        )

    rt_conf.bind_source_bridge(
        SourceBridge(
            actuate=_drift_act,
            observe=lambda: {"SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}]},
        )
    )
    try:
        with pytest.raises(DeviceStateMismatchError):
            rt_conf.submit_action("CONF-THIRD", "confirm_order", {"order_id": "O-1"}, OPS)
        assert rt_conf.get_action_run("CONF-THIRD")["status"] == "REJECTED"
        assert rt_conf.get_object(SalesOrder, "O-1", OPS).status == "cancelled"
    finally:
        rt_conf.close()


# ---------------------------------------------------------------------------
# (e) fork() 与 MCTS 推演物理绝缘
# ---------------------------------------------------------------------------
def test_fork_and_mcts_are_counterfactual_and_physically_insulated(tmp_path):
    spec, SalesOrder, _, _ = _build_settlement_spec()
    ext_calls: list[str] = []
    rt = OntologyRuntime(
        spec,
        db_path=tmp_path / "mcts_iso.sqlite3",
        world_mode="coupled",
        source_bridge=SourceBridge(
            actuate=lambda mv: ext_calls.append(f"act:{mv['run_id']}"),
            observe=lambda: (
                ext_calls.append("obs")
                or {"SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}]}
            ),
        ),
    )
    _seed_world(rt)
    try:
        forked = rt.fork()
        try:
            assert forked.world_mode == "counterfactual"
            assert forked.source_bridge is None
            assert forked.db_path == ":memory:"
            with pytest.raises(GovernanceViolationError):
                forked.bind_source_bridge(rt.source_bridge)
            forked.submit_action("FORK-RUN", "confirm_order", {"order_id": "O-1"}, OPS)
            assert ext_calls == []
        finally:
            forked.close()

        fp_before = rt.state_fingerprint()

        def _cands(_r: OntologyRuntime, step_idx: int) -> list[CandidateAction]:
            if step_idx > 0:
                return []
            return [
                CandidateAction("confirm_order", {"order_id": "O-BAD"}, prior=0.9),
                CandidateAction("confirm_order", {"order_id": "O-1"}, prior=0.5),
            ]

        plan = OntologyMCTSPlanner(
            rt,
            OPS,
            candidate_generator=_cands,
            value_function="count_confirmed_orders",
            simulations=10,
            max_steps=1,
        ).search()
        assert plan.best_plan[0].params["order_id"] == "O-1"
        assert ext_calls == []
        assert rt.state_fingerprint() == fp_before

        committed = rt.submit_action(
            "REAL-1", plan.best_plan[0].action, plan.best_plan[0].params, OPS
        )
        assert committed["status"] == "COMMITTED"
        assert ext_calls == ["act:REAL-1", "obs"]
        assert rt.get_object(SalesOrder, "O-1", OPS).status == "confirmed"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# (f) coupled revert_action 默认禁止，allow_source_revert=True 走闭环核销
# ---------------------------------------------------------------------------
def test_coupled_revert_requires_allow_source_revert_and_verifies_readback(tmp_path):
    spec, SalesOrder, _, _ = _build_settlement_spec()

    @spec.transition_invariant("forward_only_status", code="ILLEGAL_DOCUMENT_TRANSITION")
    def _forward_only_status(diff, _view) -> bool:
        for ch in diff.object_changes:
            if (
                ch.object_type == "SalesOrder"
                and ch.kind == "update"
                and (ch.before or {}).get("status") == "confirmed"
                and (ch.after or {}).get("status") == "draft"
            ):
                raise InvariantError(
                    "forward action cannot regress confirmed -> draft",
                    code="ILLEGAL_DOCUMENT_TRANSITION",
                )
        return True

    ext_status = {"O-1": "draft"}

    def _act(mv: dict[str, Any]) -> None:
        if mv.get("is_revert"):
            ext_status["O-1"] = "draft"
        else:
            ext_status["O-1"] = "confirmed"

    def _obs() -> dict[str, Any]:
        return {"SalesOrder": [{"order_id": "O-1", "status": ext_status["O-1"], "qty": 5}]}

    rt = OntologyRuntime(
        spec,
        db_path=tmp_path / "rev_denied.sqlite3",
        world_mode="coupled",
        source_bridge=SourceBridge(actuate=_act, observe=_obs, allow_source_revert=False),
    )
    _seed_world(rt)
    try:
        rt.submit_action("R-CONF", "confirm_order", {"order_id": "O-1"}, DIRECTOR)
        with pytest.raises(GovernanceViolationError) as exc_info:
            rt.revert_action("R-CONF", "R-REV-BLOCKED", DIRECTOR)
        assert exc_info.value.code == SOURCE_REVERT_FORBIDDEN
        assert rt.get_object(SalesOrder, "O-1", DIRECTOR).status == "confirmed"

        # 启用 allow_source_revert=True，先模拟外部冲正撒谎被拒，再真冲正成功
        rt.bind_source_bridge(
            SourceBridge(
                actuate=lambda _mv: {"ok": True},
                observe=lambda: {"SalesOrder": [{"order_id": "O-1", "status": "confirmed", "qty": 5}]},
                allow_source_revert=True,
            )
        )
        with pytest.raises(DeviceStateMismatchError):
            rt.revert_action("R-CONF", "R-REV-LIE", DIRECTOR)
        assert rt.get_action_run("R-CONF")["status"] == "COMMITTED"
        assert rt.get_action_run("R-REV-LIE")["status"] == "REJECTED"

        rt.bind_source_bridge(
            SourceBridge(actuate=_act, observe=_obs, allow_source_revert=True)
        )
        rev_ok = rt.revert_action("R-CONF", "R-REV-OK", DIRECTOR)
        assert rev_ok["status"] == "COMMITTED"
        assert rt.get_action_run("R-CONF")["status"] == "REVERTED"
        assert rt.get_object(SalesOrder, "O-1", DIRECTOR).status == "draft"
    finally:
        rt.close()
