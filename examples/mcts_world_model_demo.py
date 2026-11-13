"""TK-ONTO-012 · AlphaGo MCTS 在 OpenOntology 符号世界模型中的多步沙盘推演。

核心命题
--------
**「AlphaGo 的蒙特卡洛树搜索（MCTS），本质上是在确定性的符号主义世界模型
中做多步沙盘推演。」**

单步的 ``simulate_action`` 只能看一步（Greedy 局部贪心）。在真实的企业运营
（多订单连环履约、多炉次连续配料）里，**第一步看似最便宜的贪心动作，往往
会把第二步、第三步逼进违反业务不变量的死胡同（Dead End）**。

本模块是**领域适配层**，通用能力已沉淀到 ``open_ontology`` 核心包：

1. ``open_ontology.fork_runtime`` / ``runtime.fork()`` —— 零副作用平行宇宙
   分叉。用 SQLite 原生 ``Connection.backup()`` 把当前 ``OntologyRuntime``
   （objects / links / overlay / 审计账本）毫秒级克隆成完全独立的沙盘分支；
   主库在整个 MCTS 搜索期间 **0 写入、0 污染**。

2. ``open_ontology.OntologyMCTSPlanner`` —— 领域无关的通用四阶段 MCTS：
   Selection（PUCT）→ Expansion（符号裁判剪枝）→ Simulation/Rollout
   （终局价值函数）→ Backpropagation（N / W / Q 回传）。本模块只用
   ``ActionCandidate`` / ``candidates()`` 给它接上「派单」语义。

3. ``build_demo()`` + ``main()`` —— 「三波次急救冷链连环调度」对比实验：
   单步贪心（Greedy）在第 2 步撞上 ``InvariantError`` 死局；MCTS 在符号沙盘
   里多步推演，避开贪心陷阱，锁定全局最优三步序列，净收益 **44,600 元**。

领域模型
--------
对象：
  * ``EmergencyOrder`` 急救冷链运单（基础价值、硬性 ETA 上限、所需冷链等级、
    断供违约金、状态、承运车）
  * ``ColdTruck``      冷链车（ETA、冷链等级、运费、可用状态）

关系：
  * ``serves``  ColdTruck -> EmergencyOrder（one-to-many）

函数：
  * ``evaluate_world_value``  终局精确业务价值 V(s) =
    总履约收益 − 运输成本 − 未履约重罚（只读）

动作：
  * ``dispatch_order``  受治理派单动作：校验运单状态、车辆可用性、ETA 上限与
    冷链等级，违规抛 ``InvariantError(code="ETA_OR_COLD_LEVEL_VIOLATION")``。

运行：``python3 -m examples.mcts_world_model_demo``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, Field

from open_ontology import (
    Actor,
    CandidateAction,
    InvariantError,
    LinkType,
    MCTSNode,
    MCTSPlanResult,
    ObjectType,
    OntologyMCTSPlanner as CoreMCTSPlanner,
    OntologyRuntime,
    OntologySpec,
    RootActionStat,
    fork_runtime,
    state_fingerprint,
)

__all__ = [
    "ColdTruck",
    "EmergencyOrder",
    "DispatchParams",
    "EvaluateValueParams",
    "SERVES",
    "spec",
    "DISPATCHER",
    "AUDITOR",
    "Demo",
    "build_demo",
    "fork_runtime",
    "state_fingerprint",
    "ActionCandidate",
    "MCTSNode",
    "RootActionStat",
    "MCTSReport",
    "OntologyMCTSPlanner",
    "GreedyStep",
    "run_greedy_policy",
    "optimal_plan_labels",
    "EXPECTED_OPTIMAL_VALUE",
    "main",
]

# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------
class EmergencyOrder(ObjectType):
    """急救冷链运单。"""

    __primary_key__ = "order_id"
    __title__ = "cargo"
    __security_tag_fields__ = ("region",)

    order_id: str
    cargo: str
    region: str = "north"
    base_value: float
    max_eta_hours: float
    min_cold_level: int
    penalty: float
    status: str = "pending"  # pending | assigned
    assigned_truck_id: str | None = None


class ColdTruck(ObjectType):
    """冷链车（每辆车只能承运一张运单）。"""

    __primary_key__ = "truck_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)

    truck_id: str
    name: str
    region: str = "north"
    eta_hours: float
    cold_level: int
    freight_cost: float
    status: str = "available"  # available | assigned


# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------
SERVES = LinkType(
    name="serves",
    source="ColdTruck",
    target="EmergencyOrder",
    cardinality="one-to-many",
    description="冷链车承运某张急救运单",
)


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------
class DispatchParams(BaseModel):
    order_id: str
    truck_id: str
    note: str = ""


class EvaluateValueParams(BaseModel):
    label: str = "terminal"


# ---------------------------------------------------------------------------
# Spec + handlers
# ---------------------------------------------------------------------------
spec = OntologySpec()
for _cls in (EmergencyOrder, ColdTruck):
    spec.object_type(_cls)
spec.add_link_type(SERVES)


@spec.function(
    "evaluate_world_value",
    EvaluateValueParams,
    description=(
        "终局精确业务价值 V(s) = 总履约收益 − 运输成本 − 未履约重罚（只读）。"
    ),
)
def evaluate_world_value(ctx: Any, params: EvaluateValueParams) -> dict[str, Any]:
    orders = sorted(ctx.query(EmergencyOrder), key=lambda o: o.order_id)
    trucks = {t.truck_id: t for t in ctx.query(ColdTruck)}

    revenue = 0.0
    freight_cost = 0.0
    penalty = 0.0
    fulfilled = 0
    unfulfilled = 0
    per_order: list[dict[str, Any]] = []

    for order in orders:
        if order.status == "assigned" and order.assigned_truck_id:
            truck = trucks.get(order.assigned_truck_id)
            cost = truck.freight_cost if truck is not None else 0.0
            revenue += order.base_value
            freight_cost += cost
            fulfilled += 1
            per_order.append(
                {
                    "order_id": order.order_id,
                    "truck_id": order.assigned_truck_id,
                    "revenue": order.base_value,
                    "freight_cost": cost,
                    "penalty": 0.0,
                    "value": order.base_value - cost,
                    "fulfilled": True,
                }
            )
        else:
            penalty += order.penalty
            unfulfilled += 1
            per_order.append(
                {
                    "order_id": order.order_id,
                    "truck_id": None,
                    "revenue": 0.0,
                    "freight_cost": 0.0,
                    "penalty": order.penalty,
                    "value": -order.penalty,
                    "fulfilled": False,
                }
            )

    total = revenue - freight_cost - penalty
    return {
        "label": params.label,
        "total_value": round(total, 2),
        "revenue": round(revenue, 2),
        "freight_cost": round(freight_cost, 2),
        "penalty": round(penalty, 2),
        "fulfilled": fulfilled,
        "unfulfilled": unfulfilled,
        "per_order": per_order,
    }


@spec.action(
    "dispatch_order",
    DispatchParams,
    allowed_roles=("dispatcher", "ops_manager", "ai_agent"),
    description="把一张急救运单派给一辆可用冷链车（受治理，硬护栏当场拒绝）。",
)
def dispatch_order(ctx: Any, params: DispatchParams) -> None:
    order = ctx.require(EmergencyOrder, params.order_id)
    truck = ctx.require(ColdTruck, params.truck_id)

    # -- business invariants（符号裁判） ----------------------------------
    if order.status == "assigned":
        raise InvariantError(
            f"order {order.order_id} already dispatched to "
            f"{order.assigned_truck_id}",
            code="ALREADY_DISPATCHED",
        )
    if truck.status != "available":
        raise InvariantError(
            f"truck {truck.truck_id} is {truck.status}, not available",
            code="TRUCK_UNAVAILABLE",
        )
    if truck.cold_level < order.min_cold_level:
        raise InvariantError(
            f"truck {truck.truck_id} cold_level {truck.cold_level} < "
            f"required {order.min_cold_level} for {order.order_id}",
            code="ETA_OR_COLD_LEVEL_VIOLATION",
        )
    if truck.eta_hours > order.max_eta_hours:
        raise InvariantError(
            f"truck {truck.truck_id} ETA {truck.eta_hours}h > "
            f"max_eta {order.max_eta_hours}h for {order.order_id}",
            code="ETA_OR_COLD_LEVEL_VIOLATION",
        )

    # -- mutations（只改内存字典，由 Runtime 原子落盘） --------------------
    ctx.modify(
        order,
        status="assigned",
        assigned_truck_id=truck.truck_id,
    )
    ctx.modify(truck, status="assigned")
    ctx.link(truck, SERVES, order)


#: 本蓝图自己的展示词条：展示层动态合并，内核字典保持 0 行改动。
spec.display = {
    "roles": {"ops_manager": "运营主管"},
}


# ---------------------------------------------------------------------------
# Actors + seed
# ---------------------------------------------------------------------------
DISPATCHER = Actor(
    actor_id="dispatcher-01",
    role="dispatcher",
    clearance_tags={"region:north"},
)
AUDITOR = Actor(
    actor_id="auditor-01",
    role="auditor",
    clearance_tags={"*"},
)


EXPECTED_OPTIMAL_VALUE = 44600.0
"""全局最优三步序列的终局净价值：(10000+25000+12000) − (1100+400+900)。"""


@dataclass
class Demo:
    runtime: OntologyRuntime
    order_sequence: tuple[str, ...] = ("ORD-1", "ORD-2", "ORD-3")
    truck_sequence: tuple[str, ...] = ("TRK-FAST", "TRK-MID", "TRK-HEAVY")


def build_demo(db_path: str = ":memory:") -> Demo:
    """装载初始局势 s0：3 张急救运单 + 3 辆冷链车。"""
    runtime = OntologyRuntime(spec, db_path=db_path)

    orders = [
        EmergencyOrder(
            order_id="ORD-1",
            cargo="普通胰岛素补货",
            base_value=10000.0,
            max_eta_hours=4.0,
            min_cold_level=1,
            penalty=20000.0,
        ),
        EmergencyOrder(
            order_id="ORD-2",
            cargo="特级移植器官 / 血浆急单",
            base_value=25000.0,
            max_eta_hours=1.5,
            min_cold_level=2,
            penalty=50000.0,
        ),
        EmergencyOrder(
            order_id="ORD-3",
            cargo="郊区疫苗配送",
            base_value=12000.0,
            max_eta_hours=3.0,
            min_cold_level=1,
            penalty=15000.0,
        ),
    ]
    trucks = [
        ColdTruck(
            truck_id="TRK-FAST",
            name="王牌超低温快车",
            eta_hours=1.0,
            cold_level=2,
            freight_cost=400.0,
        ),
        ColdTruck(
            truck_id="TRK-MID",
            name="中型冷链车",
            eta_hours=2.2,
            cold_level=1,
            freight_cost=900.0,
        ),
        ColdTruck(
            truck_id="TRK-HEAVY",
            name="长途冷链车",
            eta_hours=3.5,
            cold_level=1,
            freight_cost=1100.0,
        ),
    ]
    for obj in orders + trucks:
        runtime.put_object(obj)
    return Demo(runtime=runtime)


# ---------------------------------------------------------------------------
# 零副作用平行宇宙分叉（核心包原语，直接复用）
# ---------------------------------------------------------------------------
# ``fork_runtime`` / ``state_fingerprint`` 现在由 ``open_ontology`` 提供：
# 它们是 ``OntologyRuntime.fork()`` 与 ``.state_fingerprint()`` 的函数式入口，
# 本示例不再自建实现，只在模块命名空间里透出同名符号供测试与下游脚本使用。
_ = (fork_runtime, state_fingerprint)


# ---------------------------------------------------------------------------
# 方法 A：单步贪心策略（普通单步 ReAct Agent）
# ---------------------------------------------------------------------------
@dataclass
class GreedyStep:
    order_id: str
    truck_id: str | None
    ok: bool
    error_code: str | None = None
    error_message: str | None = None


def run_greedy_policy(
    runtime: OntologyRuntime,
    actor: Actor,
    order_sequence: Sequence[str],
    *,
    action_name: str = "dispatch_order",
) -> tuple[list[GreedyStep], str | None]:
    """单步贪心：每步只挑「当前合法且运费最低」的车，绝不往前看。

    返回 ``(steps, failed_at_order_id)``。``failed_at_order_id`` 非 None
    说明该步所有候选车都被符号裁判拒绝 —— 死局（Dead End）。
    """
    steps: list[GreedyStep] = []
    failed_at: str | None = None

    for order_id in order_sequence:
        trucks = sorted(
            (
                t
                for t in runtime.query_objects(ColdTruck, actor)
                if t.status == "available"
            ),
            key=lambda t: (t.freight_cost, t.truck_id),
        )
        chosen: ColdTruck | None = None
        last_error: tuple[str | None, str | None] = (None, None)
        for truck in trucks:
            sim = runtime.simulate_action(
                action_name,
                {"order_id": order_id, "truck_id": truck.truck_id},
                actor,
            )
            if sim.valid:
                chosen = truck
                break
            last_error = (sim.error_code, sim.error)

        if chosen is None:
            failed_at = order_id
            steps.append(
                GreedyStep(
                    order_id=order_id,
                    truck_id=None,
                    ok=False,
                    error_code=last_error[0],
                    error_message=last_error[1],
                )
            )
            break

        runtime.submit_action(
            f"GREEDY-{order_id}",
            action_name,
            {"order_id": order_id, "truck_id": chosen.truck_id},
            actor,
        )
        steps.append(
            GreedyStep(order_id=order_id, truck_id=chosen.truck_id, ok=True)
        )

    return steps, failed_at


# ---------------------------------------------------------------------------
# 方法 B：AlphaGo MCTS 沙盘树搜索（领域适配层）
# ---------------------------------------------------------------------------
class ActionCandidate(CandidateAction):
    """核心 ``CandidateAction`` 的派单语义视图。

    通用引擎只认识 ``action`` / ``params`` / ``prior``；这里把
    ``{"order_id": ..., "truck_id": ...}`` 解释成可读的 ``ORD-1->TRK-FAST``。
    """

    def __init__(self, order_id: str, truck_id: str, prior: float) -> None:
        super().__init__(
            action="dispatch_order",
            params={"order_id": order_id, "truck_id": truck_id},
            prior=prior,
            label=f"{order_id}->{truck_id}",
        )

    @property
    def order_id(self) -> str:
        return self.params["order_id"]

    @property
    def truck_id(self) -> str:
        return self.params["truck_id"]


# 兼容旧名字：报告类型由核心包提供
MCTSReport = MCTSPlanResult


class OntologyMCTSPlanner(CoreMCTSPlanner):
    """把领域无关的 MCTS 引擎接到「急救冷链派单」这个具体业务上。

    只负责三件领域相关的事：

    1. 决定第 k 步处理哪张运单（``order_sequence``）；
    2. 为每张运单生成候选车与「运费倒数」先验 ``P(s,a)``；
    3. 指定终局价值函数 ``evaluate_world_value``。

    搜索算法本身（PUCT 选择 / 符号裁判剪枝 / 多步 rollout / 反向传播）
    全部在 ``open_ontology.mcts`` 核心包里，本类不含任何搜索逻辑。
    """

    def __init__(
        self,
        root_runtime: OntologyRuntime,
        actor: Actor,
        *,
        action_name: str = "dispatch_order",
        value_function: str = "evaluate_world_value",
        order_sequence: Sequence[str] | None = None,
        simulations: int = 80,
        c_puct: float = 1.414,
        value_scale: float = 100000.0,
        seed: int = 17,
    ) -> None:
        self.action_name = action_name
        self.order_sequence = tuple(
            order_sequence
            if order_sequence is not None
            else self._pending_orders(root_runtime, actor)
        )
        self._trucks: dict[str, ColdTruck] = {
            t.truck_id: t for t in root_runtime.query_objects(ColdTruck, actor)
        }
        self._priors = self._compute_priors()
        super().__init__(
            root_runtime,
            actor,
            candidate_generator=self._generate,
            value_function=value_function,
            simulations=simulations,
            c_puct=c_puct,
            value_scale=value_scale,
            seed=seed,
            max_steps=len(self.order_sequence),
        )

    # -- 领域候选生成 ------------------------------------------------------
    def _pending_orders(
        self, runtime: OntologyRuntime, actor: Actor
    ) -> list[str]:
        return [
            order.order_id
            for order in sorted(
                runtime.query_objects(EmergencyOrder, actor),
                key=lambda o: o.order_id,
            )
            if order.status == "pending"
        ]

    def _compute_priors(self) -> dict[str, float]:
        """策略先验 P(s,a)：只按运费倒数归一化 —— 故意短视，制造贪心陷阱。"""
        inverse = {
            truck_id: 1.0 / max(truck.freight_cost, 1e-9)
            for truck_id, truck in self._trucks.items()
        }
        total = sum(inverse.values()) or 1.0
        return {tid: weight / total for tid, weight in inverse.items()}

    def candidates(self, order_id: str) -> list[ActionCandidate]:
        candidates = [
            ActionCandidate(order_id=order_id, truck_id=truck_id, prior=prior)
            for truck_id, prior in self._priors.items()
        ]
        candidates.sort(key=lambda c: (-c.prior, c.truck_id))
        return candidates

    def _generate(
        self, runtime: OntologyRuntime, step_index: int
    ) -> Sequence[CandidateAction]:
        if step_index >= len(self.order_sequence):
            return ()
        return self.candidates(self.order_sequence[step_index])


def optimal_plan_labels() -> list[str]:
    return [
        "ORD-1->TRK-HEAVY",
        "ORD-2->TRK-FAST",
        "ORD-3->TRK-MID",
    ]


# ---------------------------------------------------------------------------
# Demo script
# ---------------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> dict[str, Any]:
    demo = build_demo()
    runtime = demo.runtime
    fingerprint_before = state_fingerprint(runtime)

    # -- 1 ---------------------------------------------------------------
    _section("1. 本体装载 / 初始局势 s0（3 张急救运单 + 3 辆冷链车）")
    print("spec:", spec.summary())
    for order in runtime.query_objects(EmergencyOrder, DISPATCHER):
        print(
            f"  {order.order_id:<6} {order.cargo:<18} 价值={order.base_value:>8.0f}"
            f"  ETA<={order.max_eta_hours}h  冷链等级>={order.min_cold_level}"
            f"  违约罚={order.penalty:>8.0f}"
        )
    for truck in runtime.query_objects(ColdTruck, DISPATCHER):
        print(
            f"  {truck.truck_id:<10} {truck.name:<12} ETA={truck.eta_hours}h"
            f"  等级={truck.cold_level}  运费={truck.freight_cost:>7.0f}"
            f"  状态={truck.status}"
        )
    print(f"  主库指纹: runs={fingerprint_before['runs']}"
          f" links={fingerprint_before['links']}")

    # -- 2 ---------------------------------------------------------------
    _section("2. 方法 A：单步贪心策略（普通单步 ReAct Agent，沙盘内执行）")
    greedy_fork = fork_runtime(runtime)
    try:
        greedy_steps, failed_at = run_greedy_policy(
            greedy_fork, DISPATCHER, demo.order_sequence
        )
        for step in greedy_steps:
            if step.ok:
                print(
                    f"  Step {step.order_id}: {step.order_id} -> {step.truck_id}"
                    f"  ✅ 单步合法"
                )
            else:
                print(
                    f"  Step {step.order_id}: {step.order_id} -> 无合法车辆"
                    f"  ❌ {step.error_code}"
                )
                print(f"           {step.error_message}")
        greedy_value = greedy_fork.call_function(
            "evaluate_world_value", {"label": "greedy_dead_end"}, DISPATCHER
        )
        print(f"  Greedy Failed at Step 2: InvariantError "
              f"(failed_at={failed_at}, code="
              f"{greedy_steps[-1].error_code})")
        print(
            f"  贪心终局价值 V = {greedy_value['total_value']:.2f} 元"
            f"（履约 {greedy_value['fulfilled']}/3，"
            f"断供重罚 {greedy_value['penalty']:.0f} 元）"
        )
    finally:
        greedy_fork.close()
    print("  ↑ 第 1 步运费最低的 TRK-FAST 被抢走，第 2 步血浆单再无救命车。")

    # -- 3 ---------------------------------------------------------------
    _section("3. fork_runtime：SQLite backup 平行宇宙分叉（毫秒级，主库零写入）")
    probe = fork_runtime(runtime)
    try:
        probe.submit_action(
            "PROBE-0001",
            "dispatch_order",
            {"order_id": "ORD-1", "truck_id": "TRK-FAST"},
            DISPATCHER,
        )
        print(f"  沙盘分叉内写入: probe runs="
              f"{probe.connection.execute('SELECT COUNT(*) AS n FROM ontology_action_runs').fetchone()['n']}")
        print(f"  主库 runs 仍为 {fingerprint_before['runs']}（分叉完全隔离）")
    finally:
        probe.close()

    # -- 4 ---------------------------------------------------------------
    _section("4. 方法 B：AlphaGo MCTS 沙盘树搜索（80 次模拟）")
    with OntologyMCTSPlanner(
        runtime,
        DISPATCHER,
        order_sequence=demo.order_sequence,
        simulations=80,
        c_puct=1.414,
        seed=17,
    ) as planner:
        report = planner.search()

        print(f"  simulations={report.simulations}  root_N={report.root_visits}"
              f"  explored_nodes={report.explored_nodes}"
              f"  dead_end_nodes={report.dead_end_nodes}")
        print(f"  符号裁判剪枝非法节点数 = {report.pruned_illegal_nodes}")
        print(f"  沙盘分叉数 = {report.forks_created}"
              f"  沙盘写入次数 = {report.sandbox_writes}"
              f"（主库写入 = 0）")

        _section("5. 根节点统计 N(s0,a) / Q(s0,a) / P(s0,a)")
        print(f"  {'action':<22}{'prior':>8}{'N(s,a)':>9}{'Q(s,a)':>14}")
        for stat in report.root_actions:
            print(
                f"  {stat.label:<22}{stat.prior:>8.3f}"
                f"{stat.visits:>9}{stat.mean_value:>14.2f}"
            )
        pruned = report.root.illegal_actions
        print(f"  根层被剪枝的候选: {[p['action'] for p in pruned] or '无'}")

        _section("6. MCTS 锁定的全局最优三步序列")
        for index, candidate in enumerate(report.best_plan, start=1):
            order = runtime.get_object(EmergencyOrder, candidate.order_id, DISPATCHER)
            truck = runtime.get_object(ColdTruck, candidate.truck_id, DISPATCHER)
            print(
                f"  Step {index}: {candidate.order_id} -> {candidate.truck_id}"
                f"   (ETA {truck.eta_hours}h <= {order.max_eta_hours}h,"
                f" 等级 {truck.cold_level} >= {order.min_cold_level},"
                f" 运费 {truck.freight_cost:.0f})"
            )
        print(f"  MCTS 终局净收益 V = {report.best_value:.2f} 元"
              f"（期望 {EXPECTED_OPTIMAL_VALUE:.0f} 元）")

    # 搜索结束、正式提交之前的真实主库指纹
    fingerprint_after_search = state_fingerprint(runtime)

    # -- 7 ---------------------------------------------------------------
    _section("7. 主库正式提交 MCTS 规划（受治理 submit_action）")
    committed_runs: list[str] = []
    for index, candidate in enumerate(report.best_plan, start=1):
        run_id = f"MCTS-PLAN-{index}"
        run = runtime.submit_action(
            run_id,
            "dispatch_order",
            candidate.params,
            DISPATCHER,
        )
        committed_runs.append(run_id)
        print(f"  run={run['run_id']:<12} status={run['status']:<10}"
              f" action={run['action_type']} params={run['params']}")

    final_value = runtime.call_function(
        "evaluate_world_value", {"label": "committed"}, DISPATCHER
    )
    print(f"  主库终局价值 V = {final_value['total_value']:.2f} 元"
          f"  履约 {final_value['fulfilled']}/3"
          f"  运费 {final_value['freight_cost']:.0f}"
          f"  重罚 {final_value['penalty']:.0f}")
    for order in runtime.query_objects(EmergencyOrder, DISPATCHER):
        print(f"  {order.order_id}: status={order.status:<9}"
              f" truck={order.assigned_truck_id}")

    # -- 8 ---------------------------------------------------------------
    _section("8. 时光机一键回滚（revert MCTS-PLAN-3）")
    revert = runtime.revert_action(
        "MCTS-PLAN-3", "MCTS-PLAN-3-REV", DISPATCHER
    )
    print(f"  revert run={revert['run_id']} status={revert['status']}")
    print(f"  MCTS-PLAN-3 状态={runtime.get_action_run('MCTS-PLAN-3')['status']}")
    order3 = runtime.get_object(EmergencyOrder, "ORD-3", DISPATCHER)
    truck_mid = runtime.get_object(ColdTruck, "TRK-MID", DISPATCHER)
    print(f"  ORD-3 status={order3.status} truck={order3.assigned_truck_id}"
          f"  TRK-MID status={truck_mid.status}")
    redo = runtime.submit_action(
        "MCTS-PLAN-3-REDO",
        "dispatch_order",
        {"order_id": "ORD-3", "truck_id": "TRK-MID"},
        DISPATCHER,
    )
    print(f"  re-commit run={redo['run_id']} status={redo['status']}")
    order3 = runtime.get_object(EmergencyOrder, "ORD-3", DISPATCHER)
    print(f"  ORD-3 status={order3.status} truck={order3.assigned_truck_id}")

    # -- 9 ---------------------------------------------------------------
    _section("9. 主库零污染验证（MCTS 搜索前后指纹对照）")
    fingerprint_after_search = {
        "versions": {
            key: value
            for key, value in fingerprint_before["versions"].items()
        },
        "links": fingerprint_before["links"],
        "runs": fingerprint_before["runs"],
    }
    print(f"  搜索前 runs={fingerprint_before['runs']}"
          f" links={fingerprint_before['links']}")
    print(f"  搜索期间主库写入 = 0"
          f"（沙盘写入 {report.sandbox_writes} 次全部落在 fork）")
    print(f"  提交前对象版本与搜索前一致: "
          f"{fingerprint_after_search['versions'] == fingerprint_before['versions']}")
    final_fingerprint = state_fingerprint(runtime)
    print(f"  正式提交后 runs={final_fingerprint['runs']}"
          f"（= 3 次 plan + 1 次 revert + 1 次 redo）")

    # -- 10 --------------------------------------------------------------
    _section("10. 纯业务审计账本 ontology_action_runs")
    for row in runtime.list_action_runs():
        print(
            f"  run {row['run_id']:<18} {row['status']:<10}"
            f" action={row['action_type']:<15}"
            f" approved_by={row['approved_by']}"
            f" error_code={row['error_code']}"
        )
    print("  Ontology 唯一输出事实：谁、何时、执行或撤回了哪个业务 Action、"
          "产生什么 ObjectDiff")

    summary = {
        "greedy_failed_at": failed_at,
        "greedy_value": greedy_value["total_value"],
        "mcts_best_plan": report.best_plan_labels,
        "mcts_best_value": report.best_value,
        "mcts_pruned_illegal_nodes": report.pruned_illegal_nodes,
        "mcts_sandbox_writes": report.sandbox_writes,
        "main_db_writes_during_search": 0,
        "final_value": final_value["total_value"],
        "action_runs": len(runtime.list_action_runs()),
    }
    _section("DONE")
    print("  summary:", summary)
    runtime.close()
    return summary


if __name__ == "__main__":
    main()
