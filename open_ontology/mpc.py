"""L4.5 · M2 闭环滚动时域控制器（Closed-Loop Receding-Horizon MPC Controller）。

直接驱动受治理的 ``OntologyRuntime`` 完成统一闭环：

1. **多步反事实内存想象（``OntologyMCTSPlanner``）**：在 ``runtime.fork()`` 衍生的纯
   ``:memory:`` 物理绝缘沙盘（``world_mode='counterfactual'``）上向未来展开多步推演；
2. **单步闭环提交与同态核验（``runtime.submit_action``）**：每次只把最优规划的
   **第一步**交给 ``OntologyRuntime`` 的三阶段同态结算管线执行（无锁外部驱动 +
   独立只读采样 + 核验提升）；一旦外部回读与预测不一致抛出
   :class:`DeviceStateMismatchError`，先用携带的真实观测刷新记忆棋盘，再基于新棋盘
   触发重规划。

严格遵守马尔可夫无队列状态原则：不维护第二套待执行棋步队列，挂起审批仅保留在
``ontology_action_runs (status='PENDING_APPROVAL')``，控制器冷重启后可从物理快照直接
续算。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .aspects import AUDIT_STATUS_COMMITTED, AUDIT_STATUS_PENDING, AUDIT_STATUS_REJECTED
from .mcts import (
    CandidateAction,
    CandidateGenerator,
    MCTSPlanResult,
    OntologyMCTSPlanner,
    ValueFn,
    _coerce_candidate,
)
from .runtime import OntologyRuntime
from .settlement import (
    DEVICE_STATE_MISMATCH,
    UNBOUND_SOURCE_BRIDGE,
    DeviceStateMismatchError,
    SourceBridge,
    call_observe,
)
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
    """闭环滚动规划过程中单步执行与同态核验的结构化轨迹记录。"""

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
        runtime: OntologyRuntime,
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
        observe_fn: Callable[..., Any] | None = None,
        max_steps: int = 20,
        max_replans: int = 3,
        run_id_prefix: str = "MPC",
        conflict_strategy: str = "user_edits_win",
        auto_trigger: bool = False,
        refresh_on_start: bool = False,
        pending_check: Callable[[OntologyRuntime], str | None] | None = None,
        on_blocked_step: Callable[[OntologyRuntime, CandidateAction, InvariantError], str | None] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if max_replans < 0:
            raise ValueError("max_replans must be >= 0")
        if (act_fn is None) != (observe_fn is None):
            raise GovernanceViolationError(
                "act_fn and observe_fn must both be provided or both omitted"
            )

        has_mcts = (candidate_generator is not None or candidate_table is not None) and (
            value_function is not None or value_fn is not None
        )
        if plan_fn is None and not has_mcts:
            raise ValueError(
                "ClosedLoopMPCController requires plan_fn or MCTS (candidate_* + value_*)"
            )

        self.runtime = runtime
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
        self.max_steps = int(max_steps)
        self.max_replans = int(max_replans)
        self.run_id_prefix = str(run_id_prefix)
        self.conflict_strategy = conflict_strategy
        self.auto_trigger = auto_trigger
        self.refresh_on_start = bool(refresh_on_start)
        self.pending_check = pending_check
        self.on_blocked_step = on_blocked_step
        self._seq = 0

        if act_fn is not None and observe_fn is not None:
            self.runtime.bind_source_bridge(
                SourceBridge(actuate=act_fn, observe=observe_fn),
                world_mode="coupled",
            )
        if self.runtime.world_mode == "coupled" and self.runtime.source_bridge is None:
            raise GovernanceViolationError(
                "coupled runtime requires a bound SourceBridge before starting MPC",
                code=UNBOUND_SOURCE_BRIDGE,
            )

    @property
    def memory_runtime(self) -> OntologyRuntime:
        return self.runtime

    def _is_goal_reached(self) -> bool:
        if callable(self.goal_fn):
            return bool(self.goal_fn(self.runtime))
        result = self.runtime.call_function(
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
            raw = self.plan_fn(self.runtime, self.actor)
            if isinstance(raw, MCTSPlanResult):
                return list(raw.best_plan)
            return [_coerce_candidate(item) for item in raw]

        with OntologyMCTSPlanner(
            self.runtime,
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

    def _next_run_id(self) -> str:
        while True:
            self._seq += 1
            rid = f"{self.run_id_prefix}-{self._seq:04d}"
            if self.runtime.get_action_run(rid) is None:
                return rid

    def _refresh_from_observation(self, objects: Any, links: Any = None) -> bool:
        if objects is None:
            return False
        self.runtime.load_snapshot(
            objects,
            links=links,
            conflict_strategy=self.conflict_strategy,
            auto_trigger=self.auto_trigger,
            actor=self.actor,
        )
        return True

    def run(self) -> MPCResult:
        """运行无队列马尔可夫闭环控制循环，直至达标、待审批或触及边界上限。"""
        steps_committed = 0
        replans_used = 0
        plans_generated = 0
        snapshots_refreshed = 0
        trajectory: list[MPCStepRecord] = []

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

        if self.refresh_on_start and self.runtime.source_bridge is not None:
            init_move = {
                "run_id": f"{self.run_id_prefix}-INIT",
                "action_type": "__observe__",
                "params": {},
                "is_observe_only": True,
            }
            obs_objects, obs_links = call_observe(
                self.runtime.source_bridge, init_move, spec=self.runtime.spec
            )
            if self._refresh_from_observation(obs_objects, obs_links):
                snapshots_refreshed += 1

        while True:
            # 1) 领域无关的在途协同/审批等待检查（优先 M1 工作卡回调，兼容 Phase 3 遗留 PENDING_APPROVAL）
            if self.pending_check is not None:
                pending_id = self.pending_check(self.runtime)
                if pending_id:
                    return _result(
                        MPC_AWAITING_APPROVAL,
                        awaiting_run_id=str(pending_id),
                    )
            awaiting = self.runtime.list_action_runs(status=AUDIT_STATUS_PENDING)
            if awaiting:
                return _result(
                    MPC_AWAITING_APPROVAL,
                    awaiting_run_id=str(awaiting[0]["run_id"]),
                )

            # 2) 目标判据满足 -> 收敛结束
            if self._is_goal_reached():
                return _result(MPC_GOAL_REACHED)

            # 3) 步数预算上限保护
            if steps_committed >= self.max_steps:
                return _result(MPC_MAX_STEPS_EXCEEDED)

            # 4) 在纯内存反事实沙盘上展开多步推演，但每轮只执行最优序列的第一步
            plan = self._plan_next()
            plans_generated += 1
            if not plan:
                return _result(MPC_UNREACHABLE)

            first = plan[0]
            rid = self._next_run_id()
            try:
                outcome = self.runtime.submit_action(
                    rid,
                    first.action,
                    first.params,
                    self.actor,
                    auto_trigger=self.auto_trigger,
                )
            except DeviceStateMismatchError as exc:
                # 回读不一致：先把真实物理观察刷进记忆棋盘，再重规划
                refreshed = self._refresh_from_observation(
                    exc.readback_objects, exc.readback_links
                )
                if refreshed:
                    snapshots_refreshed += 1
                trajectory.append(
                    MPCStepRecord(
                        run_id=rid,
                        action_type=first.action,
                        params=dict(first.params),
                        status=AUDIT_STATUS_REJECTED,
                        planned_horizon=len(plan),
                        resumed=False,
                        snapshot_refreshed=refreshed,
                        error_code=exc.code or DEVICE_STATE_MISMATCH,
                        error_message=str(exc),
                    )
                )
                if self._is_goal_reached():
                    return _result(MPC_GOAL_REACHED)
                if replans_used >= self.max_replans:
                    return _result(MPC_MAX_REPLANS_EXCEEDED)
                replans_used += 1
                continue
            except InvariantError as exc:
                if self.on_blocked_step is not None:
                    escalated_id = self.on_blocked_step(self.runtime, first, exc)
                    if escalated_id:
                        trajectory.append(
                            MPCStepRecord(
                                run_id=rid,
                                action_type=first.action,
                                params=dict(first.params),
                                status=MPC_AWAITING_APPROVAL,
                                planned_horizon=len(plan),
                                resumed=False,
                                snapshot_refreshed=False,
                                error_code=exc.code or "INVARIANT_ERROR",
                                error_message=str(exc),
                            )
                        )
                        return _result(
                            MPC_AWAITING_APPROVAL,
                            awaiting_run_id=str(escalated_id),
                        )
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

            if outcome["status"] == AUDIT_STATUS_PENDING:
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

            trajectory.append(
                MPCStepRecord(
                    run_id=rid,
                    action_type=first.action,
                    params=dict(first.params),
                    status=str(outcome["status"]),
                    planned_horizon=len(plan),
                    resumed=False,
                    snapshot_refreshed=False,
                )
            )
            if outcome["status"] == AUDIT_STATUS_COMMITTED:
                steps_committed += 1
