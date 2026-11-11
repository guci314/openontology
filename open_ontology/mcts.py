"""L4.5 · 领域无关的符号世界模型 MCTS 沙盘推演引擎。

AlphaGo 的蒙特卡洛树搜索，本质上是在**确定性的符号世界模型**里做多步
沙盘推演。本模块把这个能力沉淀为核心原语，供任何业务本体（冷链调度、
O2C 履约、钢厂配料、SaaS 运营）直接复用：

* :func:`fork_runtime` —— 用 SQLite 原生 ``Connection.backup()`` 把整库
  毫秒级克隆成完全独立的平行宇宙沙盘，主库 0 写入、0 污染。
* :func:`state_fingerprint` —— 结构化世界状态指纹，用于证明沙盘隔离。
* :class:`CandidateAction` —— 一条候选边 ``(s, a)``：动作名 + 参数 + 先验
  ``P(s,a)`` + 可读标签。完全不含任何业务字段。
* :class:`OntologyMCTSPlanner` —— 通用四阶段 MCTS：
  Selection(PUCT) → Expansion(符号裁判剪枝) → Simulation/Rollout(终局价值)
  → Backpropagation(N / W / Q)。
* :func:`commit_plan` —— 把收敛后的最优序列通过受治理 Action 正式落库。

领域接入只有两个回调：``candidate_generator(runtime, step_index)`` 给出每一步
的候选动作与先验，``value_function``（``@spec.function`` 名称）或
``value_fn(runtime)`` 给出终局价值。引擎本身不 import 任何业务模块。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .runtime import OntologyRuntime
from .spec import Actor, InvariantError

__all__ = [
    "CandidateAction",
    "MCTSNode",
    "RootActionStat",
    "MCTSPlanResult",
    "OntologyMCTSPlanner",
    "fork_runtime",
    "state_fingerprint",
    "commit_plan",
]

#: ``(runtime, step_index) -> 候选动作序列``
CandidateGenerator = Callable[[OntologyRuntime, int], Sequence["CandidateAction"]]
#: ``runtime -> 终局价值``
ValueFn = Callable[[OntologyRuntime], float]

_ROLLOUT_LIMIT = 64
"""未显式给出 ``max_steps`` 时的 rollout 安全上限。"""


# ---------------------------------------------------------------------------
# 平行宇宙沙盘
# ---------------------------------------------------------------------------
def fork_runtime(runtime: OntologyRuntime) -> OntologyRuntime:
    """把 ``runtime`` 整库克隆成一个完全独立的内存沙盘分支。

    等价于 ``runtime.fork()``；保留函数式入口是为了让 MCTS 引擎可以在
    不依赖具体 runtime 实例方法的情况下工作。
    """
    return runtime.fork()


def state_fingerprint(runtime: OntologyRuntime) -> dict[str, Any]:
    """结构化世界状态指纹（对象数 / 版本号 / 关系数 / overlay / 审计账本）。"""
    return runtime.state_fingerprint()


# ---------------------------------------------------------------------------
# 候选动作
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateAction:
    """一条候选边 ``(s, a)``：派发哪个受治理动作、带什么参数、先验多大。

    领域无关：引擎只认识 ``action`` 与 ``params``，业务语义由调用方在
    ``label`` 与 ``params`` 里自行表达。
    """

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    prior: float = 1.0
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            payload = json.dumps(
                self.params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            object.__setattr__(self, "label", f"{self.action}{payload}")

    @property
    def key(self) -> str:
        """搜索树里的唯一键（默认等于 label）。"""
        return self.label

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "params": dict(self.params),
            "prior": self.prior,
            "label": self.label,
        }


def _coerce_candidate(value: Any) -> CandidateAction:
    if isinstance(value, CandidateAction):
        return value
    if isinstance(value, dict):
        return CandidateAction(
            action=str(value["action"]),
            params=dict(value.get("params") or {}),
            prior=float(value.get("prior", 1.0)),
            label=str(value.get("label", "")),
        )
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return CandidateAction(
            action=str(value[0]),
            params=dict(value[1] or {}),
            prior=float(value[2]) if len(value) > 2 else 1.0,
        )
    raise TypeError(f"cannot coerce {value!r} into CandidateAction")


# ---------------------------------------------------------------------------
# 搜索树
# ---------------------------------------------------------------------------
@dataclass
class MCTSNode:
    """搜索树节点：一个独立的平行宇宙沙盘状态。"""

    state: OntologyRuntime | None
    step_index: int = 0
    parent: "MCTSNode | None" = None
    action: CandidateAction | None = None
    prior: float = 1.0
    visits: int = 0
    value_sum: float = 0.0
    children: dict[str, "MCTSNode"] = field(default_factory=dict)
    illegal_actions: list[dict[str, Any]] = field(default_factory=list)
    terminal: bool = False
    dead_end: bool = False

    @property
    def mean_value(self) -> float:
        return self.value_sum / self.visits if self.visits else 0.0

    @property
    def label(self) -> str:
        return self.action.label if self.action is not None else "<root>"


@dataclass
class RootActionStat:
    """根节点上一条候选边的统计量 N(s,a) / Q(s,a) / P(s,a)。"""

    label: str
    prior: float
    visits: int
    mean_value: float
    action: str = ""


@dataclass
class MCTSPlanResult:
    """一次 MCTS 搜索的完整结果。"""

    simulations: int
    best_plan: list[CandidateAction]
    best_value: float
    root_visits: int
    root_actions: list[RootActionStat]
    pruned_illegal_nodes: int
    forks_created: int
    explored_nodes: int
    dead_end_nodes: int
    sandbox_writes: int
    root: MCTSNode
    pruned_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def best_plan_labels(self) -> list[str]:
        return [cand.label for cand in self.best_plan]

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulations": self.simulations,
            "best_plan": [cand.to_dict() for cand in self.best_plan],
            "best_plan_labels": self.best_plan_labels,
            "best_value": self.best_value,
            "root_visits": self.root_visits,
            "root_actions": [
                {
                    "label": stat.label,
                    "action": stat.action,
                    "prior": stat.prior,
                    "visits": stat.visits,
                    "mean_value": stat.mean_value,
                }
                for stat in self.root_actions
            ],
            "pruned_illegal_nodes": self.pruned_illegal_nodes,
            "pruned_errors": list(self.pruned_errors),
            "forks_created": self.forks_created,
            "explored_nodes": self.explored_nodes,
            "dead_end_nodes": self.dead_end_nodes,
            "sandbox_writes": self.sandbox_writes,
        }


def _extract_value(result: Any) -> float:
    """从 ``@spec.function`` 的返回值里取出终局价值。"""
    if isinstance(result, dict):
        for key in ("total_value", "value", "score"):
            if key in result and isinstance(result[key], (int, float)):
                return float(result[key])
        for value in result.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        raise TypeError(f"value function returned no numeric field: {sorted(result)}")
    return float(result)


# ---------------------------------------------------------------------------
# 规划器
# ---------------------------------------------------------------------------
class OntologyMCTSPlanner:
    """领域无关的 AlphaGo MCTS 多步沙盘推演规划器。

    四阶段循环：

    * **Selection**：PUCT ``Q + c * P * sqrt(N(s)) / (1 + N(s,a))``
    * **Expansion**：在沙盘分支上真实执行 ``submit_action``；抛
      ``InvariantError`` 的候选动作被符号裁判当场剪枝（记 ``dead_end``）。
    * **Simulation/Rollout**：在另一个沙盘 fork 上把剩余步数推演到底，
      调用终局价值函数取 ``V(s_H)``。
    * **Backpropagation**：沿路径回传累加 ``N`` / ``W``，``Q = W / N``。

    领域接入点：

    * ``candidate_generator(runtime, step_index)`` 或静态 ``candidate_table``
      提供每一步的候选动作与先验；
    * ``value_function="evaluate_world_value"``（``@spec.function`` 名称）
      或 ``value_fn(runtime) -> float`` 提供终局价值。
    """

    def __init__(
        self,
        runtime: OntologyRuntime,
        actor: Actor,
        *,
        candidate_generator: CandidateGenerator | None = None,
        candidate_table: Sequence[Sequence[Any]] | None = None,
        value_function: str | None = None,
        value_fn: ValueFn | None = None,
        value_params: dict[str, Any] | None = None,
        simulations: int = 80,
        c_puct: float = 1.414,
        value_scale: float = 100000.0,
        seed: int = 17,
        max_steps: int | None = None,
    ) -> None:
        if candidate_generator is None and candidate_table is None:
            raise ValueError(
                "OntologyMCTSPlanner needs candidate_generator or candidate_table"
            )
        if value_function is None and value_fn is None:
            raise ValueError(
                "OntologyMCTSPlanner needs value_function or value_fn"
            )

        self.runtime = runtime
        self.root_runtime = runtime
        self.actor = actor
        self.simulations = int(simulations)
        self.c_puct = float(c_puct)
        self.value_scale = float(value_scale)
        self.random = random.Random(seed)
        self.value_function = value_function
        self.value_fn = value_fn
        self.value_params = dict(value_params or {"label": "mcts_terminal"})

        if candidate_generator is None:
            table = tuple(
                tuple(_coerce_candidate(item) for item in row)
                for row in (candidate_table or ())
            )

            def _table_generator(_runtime: OntologyRuntime, index: int) -> Sequence[CandidateAction]:
                return table[index] if 0 <= index < len(table) else ()

            candidate_generator = _table_generator
            if max_steps is None:
                max_steps = len(table)
        self.candidate_generator = candidate_generator
        self.max_steps = max_steps

        self._forks: list[OntologyRuntime] = []
        self.forks_created = 0
        self._run_seq = 0
        self.pruned_illegal_nodes = 0
        self.sandbox_writes = 0
        self.explored_nodes = 0
        self.dead_end_nodes = 0
        self.pruned_errors: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------
    def fork_runtime(self, runtime: OntologyRuntime) -> OntologyRuntime:
        """分叉一个被本规划器托管的沙盘，``close()`` 时统一回收。"""
        fork = fork_runtime(runtime)
        self._forks.append(fork)
        self.forks_created += 1
        return fork

    def close(self) -> None:
        """关闭所有沙盘 fork（主 runtime 不归本规划器管理）。"""
        for fork in self._forks:
            try:
                fork.close()
            except Exception:  # pragma: no cover - defensive
                pass
        self._forks.clear()

    def __enter__(self) -> "OntologyMCTSPlanner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- candidates --------------------------------------------------------
    def candidates_for(
        self, runtime: OntologyRuntime, step_index: int
    ) -> Sequence[CandidateAction]:
        """第 ``step_index`` 步在 ``runtime`` 沙盘状态下的候选动作。"""
        return self.candidate_generator(runtime, step_index)

    def _is_terminal(self, runtime: OntologyRuntime, step_index: int) -> bool:
        if self.max_steps is not None and step_index >= self.max_steps:
            return True
        return len(self.candidates_for(runtime, step_index)) == 0

    # -- sandbox action ----------------------------------------------------
    def _next_run_id(self, tag: str) -> str:
        self._run_seq += 1
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)[:40]
        return f"SIM-{self._run_seq:05d}-{safe}"

    def _try_action(
        self,
        state: OntologyRuntime,
        candidate: CandidateAction,
    ) -> tuple[bool, str | None, str | None]:
        """在沙盘上真实执行动作。返回 (ok, error_code, error_message)。"""
        self.sandbox_writes += 1
        try:
            state.submit_action(
                self._next_run_id(candidate.label),
                candidate.action,
                candidate.params,
                self.actor,
            )
        except InvariantError as exc:
            return False, exc.code, str(exc)
        return True, None, None

    # -- value function ----------------------------------------------------
    def evaluate(self, state: OntologyRuntime) -> float:
        """终局价值 V(s)：优先用回调，否则调用注册的只读 Function。"""
        if self.value_fn is not None:
            return float(self.value_fn(state))
        assert self.value_function is not None
        result = state.call_function(
            self.value_function, self.value_params, self.actor
        )
        return _extract_value(result)

    # -- MCTS phases -------------------------------------------------------
    def _best_child(self, node: MCTSNode) -> MCTSNode:
        sqrt_n = math.sqrt(max(node.visits, 1))
        best: MCTSNode | None = None
        best_score = -math.inf
        for child in node.children.values():
            exploit = child.mean_value / self.value_scale
            explore = self.c_puct * child.prior * sqrt_n / (1 + child.visits)
            score = exploit + explore
            if score > best_score:
                best_score = score
                best = child
        assert best is not None, "best_child() called on a childless node"
        return best

    def _select(self, node: MCTSNode) -> list[MCTSNode]:
        path = [node]
        while True:
            current = path[-1]
            if current.terminal or current.dead_end or not current.children:
                return path
            path.append(self._best_child(current))

    def _expand(self, node: MCTSNode) -> None:
        """展开一个节点：逐个候选动作在沙盘上真跑，符号裁判当场剪枝。"""
        assert node.state is not None
        if self._is_terminal(node.state, node.step_index):
            node.terminal = True
            return

        for candidate in self.candidates_for(node.state, node.step_index):
            if candidate.label in node.children:
                continue
            fork = self.fork_runtime(node.state)
            ok, code, message = self._try_action(fork, candidate)
            if not ok:
                self.pruned_illegal_nodes += 1
                record = {
                    "action": candidate.label,
                    "code": code,
                    "message": message,
                    "step_index": node.step_index,
                }
                node.illegal_actions.append(record)
                self.pruned_errors.append(record)
                fork.close()
                self._forks.remove(fork)
                continue

            child = MCTSNode(
                state=fork,
                step_index=node.step_index + 1,
                parent=node,
                action=candidate,
                prior=candidate.prior,
            )
            if self._is_terminal(fork, child.step_index):
                child.terminal = True
            self.explored_nodes += 1
            node.children[candidate.label] = child

        if not node.children:
            node.dead_end = True
            self.dead_end_nodes += 1

    def _rollout(self, node: MCTSNode) -> float:
        """从该节点推演到终局，返回终局价值 V(s_H)。"""
        if node.terminal or node.dead_end:
            assert node.state is not None
            return self.evaluate(node.state)

        assert node.state is not None
        fork = self.fork_runtime(node.state)
        try:
            step = node.step_index
            budget = (
                self.max_steps - step
                if self.max_steps is not None
                else _ROLLOUT_LIMIT
            )
            for _ in range(max(budget, 0)):
                candidates = self.candidates_for(fork, step)
                if not candidates:
                    break
                placed = False
                for candidate in candidates:
                    ok, _, _ = self._try_action(fork, candidate)
                    if ok:
                        placed = True
                        break
                if not placed:
                    break
                step += 1
            return self.evaluate(fork)
        finally:
            fork.close()
            self._forks.remove(fork)

    def _backpropagate(self, path: Iterable[MCTSNode], value: float) -> None:
        for node in path:
            node.visits += 1
            node.value_sum += value

    # -- public API --------------------------------------------------------
    def search(self) -> MCTSPlanResult:
        root = MCTSNode(state=self.fork_runtime(self.root_runtime), step_index=0)
        if self._is_terminal(root.state, 0):  # type: ignore[arg-type]
            root.terminal = True
        self.explored_nodes += 1

        for _ in range(self.simulations):
            path = self._select(root)
            leaf = path[-1]

            if not leaf.terminal and not leaf.dead_end:
                self._expand(leaf)
                if leaf.children:
                    leaf = self._best_child(leaf)
                    path.append(leaf)

            value = self._rollout(leaf)
            self._backpropagate(path, value)

        best_plan = self._best_plan(root)
        best_value = self._replay_value(best_plan)

        root_actions: list[RootActionStat] = []
        for label, child in sorted(root.children.items()):
            assert child.action is not None
            root_actions.append(
                RootActionStat(
                    label=label,
                    prior=child.prior,
                    visits=child.visits,
                    mean_value=round(child.mean_value, 2),
                    action=child.action.action,
                )
            )

        return MCTSPlanResult(
            simulations=self.simulations,
            best_plan=best_plan,
            best_value=round(best_value, 2),
            root_visits=root.visits,
            root_actions=root_actions,
            pruned_illegal_nodes=self.pruned_illegal_nodes,
            forks_created=self.forks_created,
            explored_nodes=self.explored_nodes,
            dead_end_nodes=self.dead_end_nodes,
            sandbox_writes=self.sandbox_writes,
            root=root,
            pruned_errors=list(self.pruned_errors),
        )

    def _best_plan(self, root: MCTSNode) -> list[CandidateAction]:
        plan: list[CandidateAction] = []
        node = root
        while node.children:
            node = max(
                node.children.values(),
                key=lambda c: (c.visits, c.mean_value),
            )
            assert node.action is not None
            plan.append(node.action)
        return plan

    def _replay_value(self, plan: Sequence[CandidateAction]) -> float:
        """在主库的沙盘克隆上精确重放最优序列，取真实终局价值。"""
        fork = self.fork_runtime(self.root_runtime)
        try:
            for index, candidate in enumerate(plan, start=1):
                fork.submit_action(
                    f"REPLAY-{index:02d}",
                    candidate.action,
                    candidate.params,
                    self.actor,
                )
            return self.evaluate(fork)
        finally:
            fork.close()
            self._forks.remove(fork)


# ---------------------------------------------------------------------------
# 落子
# ---------------------------------------------------------------------------
def commit_plan(
    runtime: OntologyRuntime,
    plan_result: MCTSPlanResult,
    actor: Actor,
    run_id_prefix: str = "MCTS-PLAN",
) -> list[dict[str, Any]]:
    """把 MCTS 收敛出的最优序列通过受治理 Action 正式提交到主库。

    搜索期间的全部试错都发生在沙盘 fork 里；只有这里才会真正写主库，
    并且每一步都走完整的 ``submit_action`` 治理链路（角色校验、不变量
    护栏、乐观锁、审计账本）。
    """
    runs: list[dict[str, Any]] = []
    for index, candidate in enumerate(plan_result.best_plan, start=1):
        runs.append(
            runtime.submit_action(
                f"{run_id_prefix}-{index}",
                candidate.action,
                candidate.params,
                actor,
            )
        )
    return runs
