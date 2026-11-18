"""L4.5 · M2 闭环滚动时域控制器（Closed-Loop Receding-Horizon MPC Controller）。

将两条关键机制合龙为单条确定性闭环：

1. **多步内存想象（``OntologyMCTSPlanner``）**：在 ``memory_runtime`` 的 ``:memory:``
   分叉沙盘上向未来展开多步推演；
2. **单步持久隐空间决策与独立回读结算（``LatentSpace``）**：每次只把最优规划的
   **第一步**写入持久隐空间，经独立执行器（``act_fn``）与独立只读传感器（``observe_fn``）
   完成核对；一致则提升进记忆继续下一轮，不一致则**先刷新感知并 ``rebase()``，再在新棋盘上重规划**。

严格遵循传感器与执行器分离铁律：``act_fn`` 的返回值一律丢弃，绝不采信。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .aspects import AUDIT_STATUS_COMMITTED, AUDIT_STATUS_PENDING, AUDIT_STATUS_REJECTED
from .latent import DEVICE_STATE_MISMATCH, LatentSpace
from .mcts import (
    CandidateAction,
    CandidateGenerator,
    MCTSPlanResult,
    OntologyMCTSPlanner,
    ValueFn,
    _coerce_candidate,
)
from .runtime import OntologyRuntime
from .spec import Actor, GovernanceViolationError, InvariantError

__all__ = [
    "ClosedLoopMPCController",
    "MPCResult",
    "MPCStepRecord",
    "MPC_GOAL_REACHED",
    "MPC_AWAITING_APPROVAL",
    "MPC_MAX_REPLANS_EXCEEDED",
    "MPC_MAX_STEPS_EXCEEDED",
    "MPC_UNREACHABLE",
]

MPC_GOAL_REACHED = "GOAL_REACHED"
MPC_AWAITING_APPROVAL = "AWAITING_APPROVAL"
MPC_MAX_REPLANS_EXCEEDED = "MAX_REPLANS_EXCEEDED"
MPC_MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
MPC_UNREACHABLE = "UNREACHABLE"


@dataclass
class MPCStepRecord:
    """闭环滚动规划过程中单步执行或结算的结构化轨迹记录。"""

    run_id: str
    action_type: str
    params: dict[str, Any]
    status: str
    planned_horizon: int = 0
    resumed: bool = False
    snapshot_refreshed: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "action_type": self.action_type,
            "params": copy.deepcopy(self.params),
            "status": self.status,
            "planned_horizon": self.planned_horizon,
            "resumed": self.resumed,
            "snapshot_refreshed": self.snapshot_refreshed,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass
class MPCResult:
    """``ClosedLoopMPCController.run()`` 返回的闭环执行报告。"""

    status: str
    goal_reached: bool
    steps_committed: int
    replans_used: int
    plans_generated: int
    snapshots_refreshed: int
    awaiting_run_id: str | None = None
    trajectory: list[MPCStepRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "goal_reached": self.goal_reached,
            "steps_committed": self.steps_committed,
            "replans_used": self.replans_used,
            "plans_generated": self.plans_generated,
            "snapshots_refreshed": self.snapshots_refreshed,
            "awaiting_run_id": self.awaiting_run_id,
            "trajectory": [step.to_dict() for step in self.trajectory],
        }


class ClosedLoopMPCController:
    """领域无关 M2 闭环滚动时域（Receding-Horizon MPC）控制器。"""

    def __init__(
        self,
        latent: LatentSpace,
        actor: Actor,
        *,
        goal_fn: Callable[[OntologyRuntime], bool] | str,
        goal_params: dict[str, Any] | None = None,
        plan_fn: Callable[[OntologyRuntime, Actor], Sequence[Any] | MCTSPlanResult] | None = None,
        candidate_generator: CandidateGenerator | None = None,
        candidate_table: Sequence[Sequence[Any]] | None = None,
        value_function: str | None = None,
        value_fn: ValueFn | None = None,
        value_params: dict[str, Any] | None = None,
        simulations: int = 60,
        c_puct: float = 1.414,
        value_scale: float = 100000.0,
        seed: int = 17,
        horizon: int | None = None,
        act_fn: Callable[[dict[str, Any]], Any] | None = None,
        observe_fn: Callable[[], Any] | None = None,
        max_steps: int = 20,
        max_replans: int = 3,
        run_id_prefix: str = "MPC",
        conflict_strategy: str = "user_edits_win",
        auto_trigger: bool = False,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if max_replans < 0:
            raise ValueError("max_replans must be >= 0")

        has_mcts = (candidate_generator is not None or candidate_table is not None) and (
            value_function is not None or value_fn is not None
        )
        if plan_fn is None and not has_mcts:
            raise ValueError(
                "ClosedLoopMPCController requires plan_fn or MCTS (candidate_* + value_*)"
            )

        self.latent = latent
        self.actor = actor
        self.goal_fn = goal_fn
        self.goal_params = dict(goal_params or {})
        self.plan_fn = plan_fn
        self.candidate_generator = candidate_generator
        self.candidate_table = candidate_table
        self.value_function = value_function
        self.value_fn = value_fn
        self.value_params = dict(value_params or {"label": "mpc_value"})
        self.simulations = int(simulations)
        self.c_puct = float(c_puct)
        self.value_scale = float(value_scale)
        self.seed = int(seed)
        self.horizon = horizon
        self.act_fn = act_fn
        self.observe_fn = observe_fn
        self.max_steps = int(max_steps)
        self.max_replans = int(max_replans)
        self.run_id_prefix = str(run_id_prefix)
        self.conflict_strategy = conflict_strategy
        self.auto_trigger = auto_trigger
        self._seq = 0

    @property
    def memory_runtime(self) -> OntologyRuntime:
        return self.latent.memory_runtime

    def _is_goal_reached(self) -> bool:
        if callable(self.goal_fn):
            return bool(self.goal_fn(self.memory_runtime))
        result = self.memory_runtime.call_function(
            self.goal_fn, self.goal_params, self.actor
        )
        if isinstance(result, dict):
            for key in ("goal_reached", "reached", "ok"):
                if key in result:
                    return bool(result[key])
            return False
        return bool(result)

    def _plan_next(self) -> list[CandidateAction]:
        if self.plan_fn is not None:
            raw = self.plan_fn(self.memory_runtime, self.actor)
            if isinstance(raw, MCTSPlanResult):
                return list(raw.best_plan)
            return [_coerce_candidate(item) for item in raw]

        with OntologyMCTSPlanner(
            self.memory_runtime,
            self.actor,
            candidate_generator=self.candidate_generator,
            candidate_table=self.candidate_table,
            value_function=self.value_function,
            value_fn=self.value_fn,
            value_params=self.value_params,
            simulations=self.simulations,
            c_puct=self.c_puct,
            value_scale=self.value_scale,
            seed=self.seed,
            max_steps=self.horizon,
        ) as planner:
            return list(planner.search().best_plan)

    def _observe(
        self,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, list[tuple[str, str]]] | None,
    ]:
        if self.observe_fn is None:
            raise GovernanceViolationError(
                "observe_fn is required to verify or refresh external source state"
            )
        raw = self.observe_fn()
        if isinstance(raw, tuple) and len(raw) == 2:
            return copy.deepcopy(raw[0]), copy.deepcopy(raw[1])
        if isinstance(raw, dict):
            return copy.deepcopy(raw), None
        raise GovernanceViolationError(
            "observe_fn() must return objects dict or (objects, links) tuple"
        )

    def _next_run_id(self) -> str:
        while True:
            self._seq += 1
            rid = f"{self.run_id_prefix}-{self._seq:04d}"
            if (
                self.memory_runtime.get_action_run(rid) is None
                and self.latent.latent_runtime.get_action_run(rid) is None
            ):
                return rid

    def _execute_pending_move(
        self,
        move: dict[str, Any],
        *,
        planned_horizon: int,
        resumed: bool,
    ) -> tuple[MPCStepRecord, bool]:
        """执行并结算一条已在持久隐空间确立的棋步。返回 (步骤记录, 是否触发重规划)。"""
        rid = str(move["run_id"])
        if not move["affects_source"]:
            settled = self.latent.settle_move(rid, auto_trigger=self.auto_trigger)
            return (
                MPCStepRecord(
                    run_id=rid,
                    action_type=str(move["action_type"]),
                    params=dict(move["params"]),
                    status=str(settled["status"]),
                    planned_horizon=planned_horizon,
                    resumed=resumed,
                ),
                False,
            )

        if self.act_fn is None or self.observe_fn is None:
            raise GovernanceViolationError(
                f"act_fn and observe_fn are required for affects_source=True move {rid!r}"
            )

        # 法则 2：传感器与执行器隔离——act_fn 的返回值一律丢弃，只认独立 observe_fn 回读
        _ = self.act_fn(copy.deepcopy(move))
        obs_objects, obs_links = self._observe()
        settled = self.latent.settle_move(
            rid,
            readback_objects=obs_objects,
            readback_links=obs_links,
            auto_trigger=self.auto_trigger,
        )
        if settled["status"] == AUDIT_STATUS_COMMITTED:
            return (
                MPCStepRecord(
                    run_id=rid,
                    action_type=str(move["action_type"]),
                    params=dict(move["params"]),
                    status=AUDIT_STATUS_COMMITTED,
                    planned_horizon=planned_horizon,
                    resumed=resumed,
                    snapshot_refreshed=False,
                ),
                False,
            )

        # 核心铁律：回读不一致被拒后，必须先把真实观察刷进记忆棋盘并 rebase，再重规划
        self.memory_runtime.load_snapshot(
            obs_objects,
            links=obs_links,
            conflict_strategy=self.conflict_strategy,
            auto_trigger=self.auto_trigger,
            actor=self.actor,
        )
        self.latent.rebase()
        return (
            MPCStepRecord(
                run_id=rid,
                action_type=str(move["action_type"]),
                params=dict(move["params"]),
                status=AUDIT_STATUS_REJECTED,
                planned_horizon=planned_horizon,
                resumed=resumed,
                snapshot_refreshed=True,
                error_code=settled.get("error_code") or DEVICE_STATE_MISMATCH,
                error_message=settled.get("error_message"),
            ),
            True,
        )

    def run(self) -> MPCResult:
        """运行闭环滚动规划控制循环，直至目标达成、挂起审批或触及边界上限。"""
        steps_committed = 0
        replans_used = 0
        plans_generated = 0
        snapshots_refreshed = 0
        trajectory: list[MPCStepRecord] = []
        staged_horizons: dict[str, int] = {}

        def _result(status: str, *, awaiting_run_id: str | None = None) -> MPCResult:
            return MPCResult(
                status=status,
                goal_reached=(status == MPC_GOAL_REACHED),
                steps_committed=steps_committed,
                replans_used=replans_used,
                plans_generated=plans_generated,
                snapshots_refreshed=snapshots_refreshed,
                awaiting_run_id=awaiting_run_id,
                trajectory=list(trajectory),
            )

        while True:
            # 1) 重启/审批后优先排空隐空间里已决定但未结算的棋步（携带原 run_id 幂等执行）
            pending = self.latent.pending_moves()
            if pending:
                move = pending[0]
                rid = str(move["run_id"])
                resumed = rid not in staged_horizons
                horizon_len = staged_horizons.pop(rid, 0)
                record, needs_replan = self._execute_pending_move(
                    move, planned_horizon=horizon_len, resumed=resumed
                )
                trajectory.append(record)
                if record.snapshot_refreshed:
                    snapshots_refreshed += 1
                if not needs_replan:
                    steps_committed += 1
                    continue
                if self._is_goal_reached():
                    return _result(MPC_GOAL_REACHED)
                if replans_used >= self.max_replans:
                    return _result(MPC_MAX_REPLANS_EXCEEDED)
                replans_used += 1
                continue

            # 2) 若有尚未人工批准的棋步，保持干净暂停态等待外部审批
            awaiting = self.latent.awaiting_approval_moves()
            if awaiting:
                return _result(
                    MPC_AWAITING_APPROVAL,
                    awaiting_run_id=str(awaiting[0]["run_id"]),
                )

            # 3) 目标判据满足 -> 收敛结束
            if self._is_goal_reached():
                return _result(MPC_GOAL_REACHED)

            # 4) 步数预算上限保护
            if steps_committed >= self.max_steps:
                return _result(MPC_MAX_STEPS_EXCEEDED)

            # 5) 对齐隐空间并运行多步内存想象，但本轮只把第一步落入持久隐空间
            self.latent.rebase()
            plan = self._plan_next()
            plans_generated += 1
            if not plan:
                return _result(MPC_UNREACHABLE)

            first = plan[0]
            rid = self._next_run_id()
            try:
                staged = self.latent.stage_move(
                    rid, first.action, first.params, self.actor
                )
            except InvariantError as exc:
                self.latent.rebase()
                trajectory.append(
                    MPCStepRecord(
                        run_id=rid,
                        action_type=first.action,
                        params=dict(first.params),
                        status=AUDIT_STATUS_REJECTED,
                        planned_horizon=len(plan),
                        resumed=False,
                        snapshot_refreshed=False,
                        error_code=exc.code or "INVARIANT_ERROR",
                        error_message=str(exc),
                    )
                )
                if replans_used >= self.max_replans:
                    return _result(MPC_MAX_REPLANS_EXCEEDED)
                replans_used += 1
                continue

            if staged["status"] == AUDIT_STATUS_PENDING:
                trajectory.append(
                    MPCStepRecord(
                        run_id=rid,
                        action_type=first.action,
                        params=dict(first.params),
                        status=AUDIT_STATUS_PENDING,
                        planned_horizon=len(plan),
                        resumed=False,
                    )
                )
                return _result(MPC_AWAITING_APPROVAL, awaiting_run_id=rid)

            staged_horizons[rid] = len(plan)
