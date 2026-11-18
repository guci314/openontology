"""M2 闭环滚动时域控制器（ClosedLoopMPCController）验收测试（I-006）。

覆盖：
(a) 多步 MCTS 规划下「看多步、每次只走一步」直达 GOAL_REACHED，且对撒谎的 act_fn 返回值完全免疫；
(b) 外部扰动导致第 1 步回读不一致被拒 → 先 load_snapshot 刷新记忆棋盘并 rebase → MCTS 自动改道达成目标；
(c) 执行器持续故障时触发 max_replans 干净停机（MAX_REPLANS_EXCEEDED），记忆零幻觉、被拒审计数准确；
    以及 UNREACHABLE / MAX_STEPS_EXCEEDED 边界；
(d) 崩溃重启续跑优先结算遗留 pending_moves（保持原 run_id），以及 PENDING_APPROVAL 暂停与批准续跑。
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

from open_ontology import (
    DEVICE_STATE_MISMATCH,
    MPC_AWAITING_APPROVAL,
    MPC_GOAL_REACHED,
    MPC_MAX_REPLANS_EXCEEDED,
    MPC_MAX_STEPS_EXCEEDED,
    MPC_UNREACHABLE,
    Actor,
    CandidateAction,
    ClosedLoopMPCController,
    InvariantError,
    LatentSpace,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
)


def _build_routing_spec() -> tuple[OntologySpec, type[ObjectType], type[ObjectType]]:
    spec = OntologySpec()

    class RouteLane(ObjectType):
        __primary_key__ = "lane_id"
        __title__ = "lane_id"
        __owned__ = False

        lane_id: str
        status: str = "open"
        score: int = 10

    class DeliveryTask(ObjectType):
        __primary_key__ = "task_id"
        __title__ = "task_id"
        __owned__ = {"dispatch_note": None}

        task_id: str
        status: str = "pending"
        lane_id: str | None = None
        requires_dir_approval: bool = False
        dispatch_note: str | None = None

    spec.object_type(RouteLane)
    spec.object_type(DeliveryTask)

    class AssignLaneParams(BaseModel):
        task_id: str
        lane_id: str

    class DeliverParams(BaseModel):
        task_id: str

    class NoteParams(BaseModel):
        task_id: str
        dispatch_note: str

    class EmptyParams(BaseModel):
        pass

    def _needs_approval(params: AssignLaneParams, _actor: Actor) -> bool:
        return params.lane_id == "L-APPR"

    @spec.action(
        "assign_lane",
        AssignLaneParams,
        allowed_roles=("ops", "director"),
        approver_roles=("director",),
        approval_rule=_needs_approval,
        affects_source=True,
    )
    def assign_lane(ctx: Any, params: AssignLaneParams) -> None:
        task = ctx.require(DeliveryTask, params.task_id)
        lane = ctx.require(RouteLane, params.lane_id)
        if lane.status != "open":
            raise InvariantError(f"lane {lane.lane_id} is closed", code="LANE_CLOSED")
        ctx.modify(task, status="routed", lane_id=lane.lane_id)

    @spec.action(
        "complete_delivery",
        DeliverParams,
        allowed_roles=("ops", "director"),
        affects_source=True,
    )
    def complete_delivery(ctx: Any, params: DeliverParams) -> None:
        task = ctx.require(DeliveryTask, params.task_id)
        if task.status != "routed" or not task.lane_id:
            raise InvariantError("task must be routed first", code="NOT_ROUTED")
        lane = ctx.require(RouteLane, task.lane_id)
        if lane.status != "open":
            raise InvariantError("assigned lane is closed", code="LANE_CLOSED")
        ctx.modify(task, status="delivered")

    @spec.action(
        "annotate_task",
        NoteParams,
        allowed_roles=("ops", "director"),
        affects_source=False,
    )
    def annotate_task(ctx: Any, params: NoteParams) -> None:
        task = ctx.require(DeliveryTask, params.task_id)
        ctx.modify(task, dispatch_note=params.dispatch_note)

    @spec.function("eval_delivery_value", EmptyParams, description="评估调度棋盘终局分")
    def eval_delivery_value(view: Any, _params: EmptyParams) -> float:
        task = view.get(DeliveryTask, "T-1")
        if task.status != "delivered" or not task.lane_id:
            return 0.0
        lane = view.get(RouteLane, task.lane_id)
        return float(lane.score)

    @spec.function("is_task_delivered", EmptyParams, description="判据：任务是否已送达")
    def is_task_delivered(view: Any, _params: EmptyParams) -> dict[str, Any]:
        task = view.get(DeliveryTask, "T-1")
        return {"goal_reached": task.status == "delivered"}

    return spec, RouteLane, DeliveryTask


OPS = Actor(actor_id="ops-1", role="ops")
DIRECTOR = Actor(actor_id="dir-1", role="director")


def _seed_external_world(
    *,
    lanes: list[dict[str, Any]] | None = None,
    task_status: str = "pending",
    task_lane: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "RouteLane": lanes
        or [
            {"lane_id": "L-FAST", "status": "open", "score": 100},
            {"lane_id": "L-BACKUP", "status": "open", "score": 60},
            {"lane_id": "L-CLOSED", "status": "closed", "score": 200},
        ],
        "DeliveryTask": [
            {
                "task_id": "T-1",
                "status": task_status,
                "lane_id": task_lane,
                "requires_dir_approval": False,
            }
        ],
    }


def _route_candidates(rt: OntologyRuntime, step_index: int) -> list[CandidateAction]:
    """根据当前沙盘棋面动态生成多步调度候选（包含被封车道以验证 MCTS 剪枝）。"""
    task_rows = rt.list_objects("DeliveryTask")
    if not task_rows:
        return []
    task_props = task_rows[0]["properties"]
    status = task_props["status"]
    if status == "pending":
        priors = {"L-CLOSED": 0.8, "L-FAST": 0.5, "L-BACKUP": 0.4}
        return [
            CandidateAction(
                "assign_lane",
                {"task_id": "T-1", "lane_id": lane["object_id"]},
                prior=priors.get(lane["object_id"], 0.3),
            )
            for lane in rt.list_objects("RouteLane")
        ]
    if status == "routed":
        return [
            CandidateAction("complete_delivery", {"task_id": "T-1"}, prior=1.0),
        ]
    return []


def _copy_snapshot(world: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return copy.deepcopy(world)


def test_mpc_receding_horizon_goal_and_ignores_lying_actuator(tmp_path):
    """(a) 多步 MCTS「看两步、每次只走一步」直达 GOAL_REACHED，且绝不采信撒谎的 act_fn。"""
    spec, RouteLane, DeliveryTask = _build_routing_spec()
    ext_world = _seed_external_world()
    mem = OntologyRuntime(spec, db_path=tmp_path / "mem.sqlite3")
    mem.load_snapshot(_copy_snapshot(ext_world))

    acted_calls: list[dict[str, Any]] = []

    def _honest_act(move: dict[str, Any]) -> dict[str, Any]:
        acted_calls.append(copy.deepcopy(move))
        action = move["action_type"]
        if action == "assign_lane":
            ext_world["DeliveryTask"][0]["status"] = "routed"
            ext_world["DeliveryTask"][0]["lane_id"] = move["params"]["lane_id"]
        elif action == "complete_delivery":
            ext_world["DeliveryTask"][0]["status"] = "delivered"
        return {"ok": True, "fake_claim": "whatever"}

    with mem.open_latent_space(tmp_path / "latent.sqlite3") as latent:
        ctrl = ClosedLoopMPCController(
            latent,
            OPS,
            goal_fn="is_task_delivered",
            candidate_generator=_route_candidates,
            value_function="eval_delivery_value",
            simulations=24,
            horizon=2,
            seed=11,
            act_fn=_honest_act,
            observe_fn=lambda: _copy_snapshot(ext_world),
            max_steps=5,
            max_replans=2,
        )
        res = ctrl.run()
        assert res.status == MPC_GOAL_REACHED
        assert res.goal_reached is True
        assert res.steps_committed == 2
        assert res.replans_used == 0
        assert res.plans_generated == 2  # 每步各规划 1 次
        # 第 1 轮向前看 2 步 (horizon==2)，但每轮只执行并结算 1 步
        assert [(s.action_type, s.planned_horizon, s.status) for s in res.trajectory] == [
            ("assign_lane", 2, "COMMITTED"),
            ("complete_delivery", 1, "COMMITTED"),
        ]
        assert res.trajectory[0].params["lane_id"] == "L-FAST"
        final_task = mem.get_object(DeliveryTask, "T-1", OPS)
        assert final_task.status == "delivered"
        assert final_task.lane_id == "L-FAST"
        assert len(acted_calls) == 2

    mem.close()

    # 子验证：act_fn 撒谎声称成功（返回值伪造成功），但 observe_fn 揭示外部未变 -> 绝不被骗
    ext_frozen = _seed_external_world()
    mem2 = OntologyRuntime(spec, db_path=tmp_path / "mem_lie.sqlite3")
    mem2.load_snapshot(_copy_snapshot(ext_frozen))

    def _lying_act(move: dict[str, Any]) -> dict[str, Any]:
        # 返回看起来像已成功的消息，但根本不改 ext_frozen
        return {"ok": True, "status": "routed", "lane_id": move["params"]["lane_id"]}

    with mem2.open_latent_space(tmp_path / "lat_lie.sqlite3") as lat2:
        ctrl2 = ClosedLoopMPCController(
            lat2,
            OPS,
            goal_fn=lambda rt: rt.get_object(DeliveryTask, "T-1", OPS).status == "delivered",
            candidate_generator=_route_candidates,
            value_function="eval_delivery_value",
            simulations=18,
            horizon=2,
            act_fn=_lying_act,
            observe_fn=lambda: _copy_snapshot(ext_frozen),
            max_replans=0,
        )
        res2 = ctrl2.run()
        assert res2.status == MPC_MAX_REPLANS_EXCEEDED
        assert res2.goal_reached is False
        assert res2.steps_committed == 0
        assert res2.trajectory[0].status == "REJECTED"
        assert res2.trajectory[0].error_code == DEVICE_STATE_MISMATCH
        assert mem2.get_object(DeliveryTask, "T-1", OPS).status == "pending"
    mem2.close()


def test_mpc_external_perturbation_refreshes_memory_and_detours_to_goal(tmp_path):
    """(b) 外部扰动封锁最优通道 → 拒签后先 load_snapshot 刷新棋盘并 rebase → 自动改走备选路径达标。"""
    spec, RouteLane, DeliveryTask = _build_routing_spec()
    ext_world = _seed_external_world()
    mem = OntologyRuntime(spec, db_path=tmp_path / "detour_mem.sqlite3")
    mem.load_snapshot(_copy_snapshot(ext_world))
    # 叠加一个自有字段，验证感知刷新不会冲掉本体 overlay
    mem.submit_action(
        "MEM-NOTE-1",
        "annotate_task",
        {"task_id": "T-1", "dispatch_note": "priority-vip"},
        OPS,
    )

    def _perturbed_act(move: dict[str, Any]) -> None:
        action = move["action_type"]
        params = move["params"]
        if action == "assign_lane" and params["lane_id"] == "L-FAST":
            # 外部扰动：L-FAST 突然关闭，调度未能落地，T-1 仍为 pending
            for lane in ext_world["RouteLane"]:
                if lane["lane_id"] == "L-FAST":
                    lane["status"] = "closed"
            return
        if action == "assign_lane" and params["lane_id"] == "L-BACKUP":
            ext_world["DeliveryTask"][0]["status"] = "routed"
            ext_world["DeliveryTask"][0]["lane_id"] = "L-BACKUP"
        elif action == "complete_delivery":
            ext_world["DeliveryTask"][0]["status"] = "delivered"

    with mem.open_latent_space(tmp_path / "detour_lat.sqlite3") as latent:
        ctrl = ClosedLoopMPCController(
            latent,
            OPS,
            goal_fn=lambda rt: rt.get_object(DeliveryTask, "T-1", OPS).status == "delivered",
            candidate_generator=_route_candidates,
            value_function="eval_delivery_value",
            simulations=24,
            horizon=2,
            seed=11,
            act_fn=_perturbed_act,
            observe_fn=lambda: _copy_snapshot(ext_world),
            max_steps=5,
            max_replans=3,
        )
        res = ctrl.run()

        assert res.status == MPC_GOAL_REACHED
        assert res.goal_reached is True
        assert res.replans_used == 1
        assert res.snapshots_refreshed == 1
        assert res.steps_committed == 2
        assert [
            (s.action_type, s.params.get("lane_id"), s.status, s.snapshot_refreshed)
            for s in res.trajectory
        ] == [
            ("assign_lane", "L-FAST", "REJECTED", True),
            ("assign_lane", "L-BACKUP", "COMMITTED", False),
            ("complete_delivery", None, "COMMITTED", False),
        ]
        # 记忆棋盘上：L-FAST 已通过 load_snapshot 变成 closed，T-1 改道 L-BACKUP 送达且 overlay 未丢失
        assert mem.get_object(RouteLane, "L-FAST", OPS).status == "closed"
        task = mem.get_object(DeliveryTask, "T-1", OPS)
        assert task.status == "delivered"
        assert task.lane_id == "L-BACKUP"
        assert task.dispatch_note == "priority-vip"
    mem.close()


def test_mpc_persistent_actuator_failure_stops_at_max_replans_and_bounds(tmp_path):
    """(c) 执行器持续失灵时触发 max_replans 干净停机，零幻觉且被拒审计精确；另验 UNREACHABLE / max_steps。"""
    spec, _, DeliveryTask = _build_routing_spec()
    ext_world = _seed_external_world()
    mem = OntologyRuntime(spec, db_path=tmp_path / "fail_mem.sqlite3")
    mem.load_snapshot(_copy_snapshot(ext_world))

    with mem.open_latent_space(tmp_path / "fail_lat.sqlite3") as latent:
        ctrl = ClosedLoopMPCController(
            latent,
            OPS,
            goal_fn=lambda rt: rt.get_object(DeliveryTask, "T-1", OPS).status == "delivered",
            candidate_generator=_route_candidates,
            value_function="eval_delivery_value",
            simulations=16,
            horizon=2,
            act_fn=lambda _m: {"ok": True},  # 始终不写入外部世界
            observe_fn=lambda: _copy_snapshot(ext_world),
            max_steps=10,
            max_replans=2,
        )
        res = ctrl.run()
        assert res.status == MPC_MAX_REPLANS_EXCEEDED
        assert res.goal_reached is False
        assert res.replans_used == 2
        assert res.snapshots_refreshed == 3  # 初次尝试 + 2 次重规划，共 3 次刷新感知
        assert res.steps_committed == 0
        assert len(res.trajectory) == 3
        assert all(
            s.status == "REJECTED" and s.error_code == DEVICE_STATE_MISMATCH
            for s in res.trajectory
        )
        # 记忆零幻觉，ledger 中 REJECTED 条数精确为 3
        assert mem.get_object(DeliveryTask, "T-1", OPS).status == "pending"
        rejected_runs = mem.list_action_runs(status="REJECTED")
        assert len(rejected_runs) == 3
        assert all(r["error_code"] == DEVICE_STATE_MISMATCH for r in rejected_runs)
    mem.close()

    # 边界 2：无可用路时干净返回 UNREACHABLE
    ext_dead = _seed_external_world(
        lanes=[{"lane_id": "L-CLOSED", "status": "closed", "score": 10}]
    )
    mem_dead = OntologyRuntime(spec, db_path=tmp_path / "dead_mem.sqlite3")
    mem_dead.load_snapshot(_copy_snapshot(ext_dead))
    with mem_dead.open_latent_space(tmp_path / "dead_lat.sqlite3") as lat_dead:
        ctrl_dead = lat_dead.make_controller(
            OPS,
            goal_fn="is_task_delivered",
            candidate_generator=_route_candidates,
            value_function="eval_delivery_value",
            simulations=12,
            horizon=2,
            act_fn=lambda _m: None,
            observe_fn=lambda: _copy_snapshot(ext_dead),
        )
        res_dead = ctrl_dead.run()
        assert res_dead.status == MPC_UNREACHABLE
        assert res_dead.steps_committed == 0
    mem_dead.close()

    # 边界 3：max_steps 预算封顶 -> MAX_STEPS_EXCEEDED
    ext_ok = _seed_external_world()
    mem_cap = OntologyRuntime(spec, db_path=tmp_path / "cap_mem.sqlite3")
    mem_cap.load_snapshot(_copy_snapshot(ext_ok))

    def _step_act(move: dict[str, Any]) -> None:
        if move["action_type"] == "assign_lane":
            ext_ok["DeliveryTask"][0]["status"] = "routed"
            ext_ok["DeliveryTask"][0]["lane_id"] = move["params"]["lane_id"]
        elif move["action_type"] == "complete_delivery":
            ext_ok["DeliveryTask"][0]["status"] = "delivered"

    with mem_cap.open_latent_space(tmp_path / "cap_lat.sqlite3") as lat_cap:
        ctrl_cap = ClosedLoopMPCController(
            lat_cap,
            OPS,
            goal_fn="is_task_delivered",
            candidate_generator=_route_candidates,
            value_function="eval_delivery_value",
            simulations=16,
            horizon=2,
            act_fn=_step_act,
            observe_fn=lambda: _copy_snapshot(ext_ok),
            max_steps=1,
        )
        res_cap = ctrl_cap.run()
        assert res_cap.status == MPC_MAX_STEPS_EXCEEDED
        assert res_cap.steps_committed == 1
        assert mem_cap.get_object(DeliveryTask, "T-1", OPS).status == "routed"
    mem_cap.close()


def test_mpc_crash_restart_and_approval_pause_resume(tmp_path):
    """(d) 崩溃重启优先结算既有 pending_moves（原 run_id）+ AWAITING_APPROVAL 暂停与批准续跑。"""
    spec, _, DeliveryTask = _build_routing_spec()

    # 场景 1：落子持久隐空间后进程崩溃（关闭两端 runtime），重启新控制器必须先结算原 run_id
    mem_db = tmp_path / "crash_mem.sqlite3"
    lat_db = tmp_path / "crash_lat.sqlite3"
    ext_world = _seed_external_world()

    mem1 = OntologyRuntime(spec, db_path=mem_db)
    mem1.load_snapshot(_copy_snapshot(ext_world))
    lat1 = LatentSpace(mem1, lat_db)
    lat1.stage_move(
        "MPC-0001",
        "assign_lane",
        {"task_id": "T-1", "lane_id": "L-BACKUP"},
        OPS,
    )
    lat1.close()
    mem1.close()

    executed_run_ids: list[str] = []

    def _resume_act(move: dict[str, Any]) -> None:
        executed_run_ids.append(move["run_id"])
        if move["action_type"] == "assign_lane":
            ext_world["DeliveryTask"][0]["status"] = "routed"
            ext_world["DeliveryTask"][0]["lane_id"] = move["params"]["lane_id"]
        elif move["action_type"] == "complete_delivery":
            ext_world["DeliveryTask"][0]["status"] = "delivered"

    mem2 = OntologyRuntime(spec, db_path=mem_db)
    try:
        with LatentSpace(mem2, lat_db) as lat2:
            ctrl_resumed = ClosedLoopMPCController(
                lat2,
                OPS,
                goal_fn="is_task_delivered",
                candidate_generator=_route_candidates,
                value_function="eval_delivery_value",
                simulations=16,
                horizon=2,
                act_fn=_resume_act,
                observe_fn=lambda: _copy_snapshot(ext_world),
                run_id_prefix="MPC",
            )
            res = ctrl_resumed.run()
            assert res.status == MPC_GOAL_REACHED
            # 首步必是恢复结算遗留的 MPC-0001（L-BACKUP），而非重新规划去走 L-FAST
            assert executed_run_ids[0] == "MPC-0001"
            assert res.trajectory[0].run_id == "MPC-0001"
            assert res.trajectory[0].resumed is True
            assert res.trajectory[1].run_id == "MPC-0002"
            assert res.trajectory[1].resumed is False
            task = mem2.get_object(DeliveryTask, "T-1", OPS)
            assert task.status == "delivered"
            assert task.lane_id == "L-BACKUP"
    finally:
        mem2.close()

    # 场景 2：隐空间落子触发 PENDING_APPROVAL → 暂停返回 AWAITING_APPROVAL → 批准后无缝续跑达标
    ext_appr = _seed_external_world(
        lanes=[
            {"lane_id": "L-APPR", "status": "open", "score": 300},
            {"lane_id": "L-SLOW", "status": "open", "score": 10},
        ]
    )
    mem_a = OntologyRuntime(spec, db_path=tmp_path / "appr_mem.sqlite3")
    mem_a.load_snapshot(_copy_snapshot(ext_appr))

    def _appr_candidates(rt: OntologyRuntime, _step: int) -> list[CandidateAction]:
        task_props = rt.list_objects("DeliveryTask")[0]["properties"]
        if task_props["status"] == "pending":
            return [
                CandidateAction("assign_lane", {"task_id": "T-1", "lane_id": "L-APPR"}, prior=0.8),
                CandidateAction("assign_lane", {"task_id": "T-1", "lane_id": "L-SLOW"}, prior=0.2),
            ]
        if task_props["status"] == "routed":
            return [CandidateAction("complete_delivery", {"task_id": "T-1"}, prior=1.0)]
        return []

    appr_acts: list[str] = []

    def _appr_act(move: dict[str, Any]) -> None:
        appr_acts.append(move["run_id"])
        if move["action_type"] == "assign_lane":
            ext_appr["DeliveryTask"][0]["status"] = "routed"
            ext_appr["DeliveryTask"][0]["lane_id"] = move["params"]["lane_id"]
        elif move["action_type"] == "complete_delivery":
            ext_appr["DeliveryTask"][0]["status"] = "delivered"

    with mem_a.open_latent_space(tmp_path / "appr_lat.sqlite3") as lat_a:
        ctrl_a = ClosedLoopMPCController(
            lat_a,
            OPS,
            goal_fn="is_task_delivered",
            candidate_generator=_appr_candidates,
            value_function="eval_delivery_value",
            simulations=24,
            horizon=2,
            act_fn=_appr_act,
            observe_fn=lambda: _copy_snapshot(ext_appr),
        )
        paused = ctrl_a.run()
        assert paused.status == MPC_AWAITING_APPROVAL
        assert paused.goal_reached is False
        assert paused.awaiting_run_id is not None
        assert appr_acts == []
        assert mem_a.get_object(DeliveryTask, "T-1", OPS).status == "pending"

        # 批准前再次运行控制器仍停留在同一步 AWAITING_APPROVAL
        still_paused = ctrl_a.run()
        assert still_paused.status == MPC_AWAITING_APPROVAL
        assert still_paused.awaiting_run_id == paused.awaiting_run_id
        assert appr_acts == []

        # 外部审批通过后重跑控制器：优先执行原 awaiting_run_id，随后推进完成第 2 步
        lat_a.approve_move(paused.awaiting_run_id, DIRECTOR)
        finished = ctrl_a.run()
        assert finished.status == MPC_GOAL_REACHED
        assert finished.trajectory[0].run_id == paused.awaiting_run_id
        assert finished.trajectory[0].resumed is True
        assert appr_acts[0] == paused.awaiting_run_id
        assert mem_a.get_object(DeliveryTask, "T-1", OPS).status == "delivered"
        assert mem_a.get_object(DeliveryTask, "T-1", OPS).lane_id == "L-APPR"
    mem_a.close()
