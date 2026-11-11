"""TK-ONTO-012 验收测试：AlphaGo MCTS 在 OpenOntology 符号世界模型中的多步推演。

覆盖：
* ``fork_runtime`` 平行宇宙克隆的零副作用隔离
* 单步贪心策略在 Step 2 撞上 ``InvariantError`` 死局
* ``OntologyMCTSPlanner`` 的 PUCT 选择、符号剪枝、终局价值与反向传播
* MCTS 避开贪心陷阱，锁定全局最优序列，净收益 44,600 元
* 80 次模拟期间主库 0 写入 / 0 污染
* 正式提交后受治理账本与 ``revert_action`` 时光机回滚
"""

from __future__ import annotations

import pytest

from examples import mcts_world_model_demo as demo
from open_ontology import InvariantError

DISPATCHER = demo.DISPATCHER


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def rt():
    model = demo.build_demo()
    yield model.runtime
    model.runtime.close()


@pytest.fixture()
def planner(rt):
    with demo.OntologyMCTSPlanner(
        rt,
        DISPATCHER,
        order_sequence=("ORD-1", "ORD-2", "ORD-3"),
        simulations=80,
        c_puct=1.414,
        seed=17,
    ) as instance:
        yield instance


# ---------------------------------------------------------------------------
# 1. 初始局势
# ---------------------------------------------------------------------------
def test_build_demo_initial_state(rt):
    orders = rt.query_objects(demo.EmergencyOrder, DISPATCHER)
    trucks = rt.query_objects(demo.ColdTruck, DISPATCHER)
    assert {o.order_id for o in orders} == {"ORD-1", "ORD-2", "ORD-3"}
    assert {t.truck_id for t in trucks} == {"TRK-FAST", "TRK-MID", "TRK-HEAVY"}
    assert all(o.status == "pending" for o in orders)
    assert all(t.status == "available" for t in trucks)

    fast = rt.get_object(demo.ColdTruck, "TRK-FAST", DISPATCHER)
    assert fast.cold_level == 2
    assert fast.eta_hours == 1.0
    assert fast.freight_cost == 400.0

    plasma = rt.get_object(demo.EmergencyOrder, "ORD-2", DISPATCHER)
    assert plasma.max_eta_hours == 1.5
    assert plasma.min_cold_level == 2
    assert plasma.penalty == 50000.0


def test_initial_world_value_is_all_penalties(rt):
    value = rt.call_function("evaluate_world_value", {"label": "s0"}, DISPATCHER)
    assert value["fulfilled"] == 0
    assert value["unfulfilled"] == 3
    assert value["total_value"] == -85000.0
    assert value["penalty"] == 85000.0


# ---------------------------------------------------------------------------
# 2. fork_runtime：平行宇宙隔离
# ---------------------------------------------------------------------------
def test_fork_runtime_is_fully_isolated(rt):
    before = demo.state_fingerprint(rt)
    fork = demo.fork_runtime(rt)
    try:
        assert fork is not rt
        fork.submit_action(
            "FORK-0001",
            "dispatch_order",
            {"order_id": "ORD-1", "truck_id": "TRK-FAST"},
            DISPATCHER,
        )
        forked_order = fork.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER)
        assert forked_order.status == "assigned"
        assert forked_order.assigned_truck_id == "TRK-FAST"

        after = demo.state_fingerprint(rt)
        assert after == before, "fork 的写入绝不能泄漏到主库"

        main_order = rt.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER)
        assert main_order.status == "pending"
        assert rt.get_action_run("FORK-0001") is None
    finally:
        fork.close()


def test_fork_copies_objects_links_and_ledger(rt):
    rt.submit_action(
        "SEED-0001",
        "dispatch_order",
        {"order_id": "ORD-1", "truck_id": "TRK-MID"},
        DISPATCHER,
    )
    fork = demo.fork_runtime(rt)
    try:
        assert fork.count_objects("EmergencyOrder") == rt.count_objects("EmergencyOrder")
        assert fork.count_objects("ColdTruck") == rt.count_objects("ColdTruck")
        assert fork.get_action_run("SEED-0001") is not None
        links_main = rt.connection.execute(
            "SELECT COUNT(*) AS n FROM ontology_links"
        ).fetchone()["n"]
        links_fork = fork.connection.execute(
            "SELECT COUNT(*) AS n FROM ontology_links"
        ).fetchone()["n"]
        assert links_main == links_fork == 1
        # 写闸门也是闭合状态
        assert (
            fork.connection.execute(
                "SELECT is_open FROM ontology_write_gate WHERE id = 1"
            ).fetchone()["is_open"]
            == 0
        )
    finally:
        fork.close()


# ---------------------------------------------------------------------------
# 3. 受治理动作与符号裁判
# ---------------------------------------------------------------------------
def test_dispatch_legal_path_writes_object_and_link(rt):
    run = rt.submit_action(
        "RUN-0001",
        "dispatch_order",
        {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        DISPATCHER,
    )
    assert run["status"] == "COMMITTED"
    order = rt.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER)
    truck = rt.get_object(demo.ColdTruck, "TRK-HEAVY", DISPATCHER)
    assert order.status == "assigned"
    assert order.assigned_truck_id == "TRK-HEAVY"
    assert truck.status == "assigned"

    link = rt.connection.execute(
        "SELECT * FROM ontology_links WHERE link_type = 'serves'"
    ).fetchone()
    assert link["source_id"] == "TRK-HEAVY"
    assert link["target_id"] == "ORD-1"


def test_plasma_order_rejects_weak_truck_with_symbolic_code(rt):
    for truck_id in ("TRK-MID", "TRK-HEAVY"):
        sim = rt.simulate_action(
            "dispatch_order",
            {"order_id": "ORD-2", "truck_id": truck_id},
            DISPATCHER,
        )
        assert sim.valid is False
        assert sim.error_code == "ETA_OR_COLD_LEVEL_VIOLATION"
        assert sim.diff.is_empty()

    with pytest.raises(InvariantError) as excinfo:
        rt.submit_action(
            "RUN-REJECT",
            "dispatch_order",
            {"order_id": "ORD-2", "truck_id": "TRK-MID"},
            DISPATCHER,
        )
    assert excinfo.value.code == "ETA_OR_COLD_LEVEL_VIOLATION"

    rejected = rt.get_action_run("RUN-REJECT")
    assert rejected["status"] == "REJECTED"
    assert rejected["object_diff"] == {"object_changes": [], "link_changes": []}
    # 拒绝审计不得改动对象
    order = rt.get_object(demo.EmergencyOrder, "ORD-2", DISPATCHER)
    assert order.status == "pending"


def test_dispatch_rejects_taken_truck_and_double_dispatch(rt):
    rt.submit_action(
        "RUN-A",
        "dispatch_order",
        {"order_id": "ORD-1", "truck_id": "TRK-FAST"},
        DISPATCHER,
    )
    taken = rt.simulate_action(
        "dispatch_order",
        {"order_id": "ORD-3", "truck_id": "TRK-FAST"},
        DISPATCHER,
    )
    assert taken.valid is False
    assert taken.error_code == "TRUCK_UNAVAILABLE"

    twice = rt.simulate_action(
        "dispatch_order",
        {"order_id": "ORD-1", "truck_id": "TRK-MID"},
        DISPATCHER,
    )
    assert twice.valid is False
    assert twice.error_code == "ALREADY_DISPATCHED"


# ---------------------------------------------------------------------------
# 4. 单步贪心：贪心陷阱
# ---------------------------------------------------------------------------
def test_greedy_policy_walks_into_dead_end(rt):
    steps, failed_at = demo.run_greedy_policy(
        rt, DISPATCHER, ("ORD-1", "ORD-2", "ORD-3")
    )
    assert failed_at == "ORD-2"
    assert steps[0].ok is True
    assert steps[0].truck_id == "TRK-FAST"  # 运费最低的救命车被贪心抢走
    assert steps[1].ok is False
    assert steps[1].error_code == "ETA_OR_COLD_LEVEL_VIOLATION"

    order1 = rt.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER)
    order2 = rt.get_object(demo.EmergencyOrder, "ORD-2", DISPATCHER)
    assert order1.assigned_truck_id == "TRK-FAST"
    assert order2.status == "pending"

    value = rt.call_function("evaluate_world_value", {"label": "greedy"}, DISPATCHER)
    assert value["fulfilled"] == 1
    assert value["penalty"] == 65000.0
    assert value["total_value"] < 0


def test_greedy_policy_leaves_no_trace_when_run_on_fork(rt):
    before = demo.state_fingerprint(rt)
    fork = demo.fork_runtime(rt)
    try:
        demo.run_greedy_policy(fork, DISPATCHER, ("ORD-1", "ORD-2", "ORD-3"))
    finally:
        fork.close()
    assert demo.state_fingerprint(rt) == before


# ---------------------------------------------------------------------------
# 5. MCTS 规划器内部机制
# ---------------------------------------------------------------------------
def test_priors_favour_cheapest_truck(planner):
    candidates = planner.candidates("ORD-1")
    assert [c.truck_id for c in candidates] == ["TRK-FAST", "TRK-MID", "TRK-HEAVY"]
    assert candidates[0].prior == pytest.approx(0.553, abs=1e-3)
    assert sum(c.prior for c in candidates) == pytest.approx(1.0)
    # 先验故意短视：王牌车偏好最高
    assert candidates[0].prior > candidates[1].prior > candidates[2].prior


def test_planner_value_function_uses_registered_function(planner, rt):
    fork = planner.fork_runtime(rt)
    try:
        assert planner.evaluate(fork) == -85000.0
        fork.submit_action(
            "VAL-0001",
            "dispatch_order",
            {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
            DISPATCHER,
        )
        assert planner.evaluate(fork) == pytest.approx(10000.0 - 1100.0 - 50000.0 - 15000.0)
    finally:
        fork.close()


def test_symbolic_referee_prunes_illegal_branches(planner):
    report = planner.search()
    assert report.pruned_illegal_nodes > 0
    assert report.dead_end_nodes > 0
    # 死局分支上必须留下符号裁判的错误码证据
    root_child = report.root.children["ORD-1->TRK-FAST"]
    illegal_codes = {
        item["code"] for item in root_child.illegal_actions
    }
    assert "ETA_OR_COLD_LEVEL_VIOLATION" in illegal_codes
    assert report.root.children["ORD-1->TRK-FAST"].dead_end is True


def test_mcts_visits_are_distributed_by_puct(planner):
    report = planner.search()
    assert report.root_visits == report.simulations == 80
    assert len(report.root_actions) == 3
    total = sum(stat.visits for stat in report.root_actions)
    assert total == report.root_visits
    by_label = {stat.label: stat for stat in report.root_actions}
    # 高先验的贪心动作 Q 暴跌为负，搜索预算被 PUCT 转给正解
    assert by_label["ORD-1->TRK-FAST"].mean_value < 0
    assert by_label["ORD-1->TRK-HEAVY"].mean_value == pytest.approx(44600.0)
    assert (
        by_label["ORD-1->TRK-HEAVY"].visits
        > by_label["ORD-1->TRK-FAST"].visits
    )


def test_mcts_returns_globally_optimal_plan(planner):
    report = planner.search()
    assert report.best_plan_labels == demo.optimal_plan_labels()
    assert report.best_value == demo.EXPECTED_OPTIMAL_VALUE == 44600.0
    assert report.best_plan[0].truck_id == "TRK-HEAVY"
    assert report.best_plan[1].truck_id == "TRK-FAST"
    assert report.best_plan[2].truck_id == "TRK-MID"


# ---------------------------------------------------------------------------
# 6. 主库零污染
# ---------------------------------------------------------------------------
def test_search_does_not_touch_main_database(rt, planner):
    before = demo.state_fingerprint(rt)
    report = planner.search()
    after = demo.state_fingerprint(rt)

    assert after == before
    assert after["runs"] == 0
    assert after["links"] == 0
    assert report.sandbox_writes > 0
    assert report.forks_created > 0
    for order in rt.query_objects(demo.EmergencyOrder, DISPATCHER):
        assert order.status == "pending"
        assert order.assigned_truck_id is None


def test_planner_closes_every_fork(rt):
    planner = demo.OntologyMCTSPlanner(
        rt, DISPATCHER, simulations=20, seed=17
    )
    report = planner.search()
    assert report.forks_created >= report.explored_nodes
    # 搜索期间树节点持有自己的沙盘状态（这是 MCTS 的语义），
    # 但 close() 必须一次性回收全部 fork，绝不泄漏连接。
    assert planner._forks, "搜索期间树节点应持有沙盘 fork"
    planner.close()
    assert planner._forks == [], "所有沙盘 fork 必须在 close() 时关闭"


# ---------------------------------------------------------------------------
# 7. 端到端：提交 + 回滚
# ---------------------------------------------------------------------------
def test_commit_optimal_plan_then_revert(rt, planner):
    report = planner.search()
    for index, candidate in enumerate(report.best_plan, start=1):
        run = rt.submit_action(
            f"PLAN-{index}",
            "dispatch_order",
            candidate.params,
            DISPATCHER,
        )
        assert run["status"] == "COMMITTED"

    value = rt.call_function("evaluate_world_value", {"label": "final"}, DISPATCHER)
    assert value["fulfilled"] == 3
    assert value["freight_cost"] == 2400.0
    assert value["penalty"] == 0.0
    assert value["total_value"] == 44600.0
    assert [row["order_id"] for row in value["per_order"]] == [
        "ORD-1",
        "ORD-2",
        "ORD-3",
    ]

    revert = rt.revert_action("PLAN-3", "PLAN-3-REV", DISPATCHER)
    assert revert["status"] == "COMMITTED"
    assert rt.get_action_run("PLAN-3")["status"] == "REVERTED"

    order3 = rt.get_object(demo.EmergencyOrder, "ORD-3", DISPATCHER)
    truck_mid = rt.get_object(demo.ColdTruck, "TRK-MID", DISPATCHER)
    assert order3.status == "pending"
    assert order3.assigned_truck_id is None
    assert truck_mid.status == "available"
    assert rt.count_objects("EmergencyOrder") == 3

    reverted_value = rt.call_function(
        "evaluate_world_value", {"label": "reverted"}, DISPATCHER
    )
    assert reverted_value["fulfilled"] == 2
    assert reverted_value["total_value"] == pytest.approx(
        44600.0 - 12000.0 + 900.0 - 15000.0
    )


def test_main_runs_end_to_end():
    summary = demo.main()
    assert summary["greedy_failed_at"] == "ORD-2"
    assert summary["mcts_best_plan"] == demo.optimal_plan_labels()
    assert summary["mcts_best_value"] == 44600.0
    assert summary["final_value"] == 44600.0
    assert summary["main_db_writes_during_search"] == 0
    assert summary["mcts_pruned_illegal_nodes"] > 0
    assert summary["action_runs"] == 5


# ---------------------------------------------------------------------------
# 8. TK-ONTO-014：核心包集成（open_ontology.mcts / runtime / osdk）
# ---------------------------------------------------------------------------
from open_ontology import (  # noqa: E402  (分组在文件末尾，便于对照旧测试)
    AIPAgentExecutor,
    CandidateAction,
    MCTSNode,
    MCTSPlanResult,
    OntologyMCTSPlanner,
    commit_plan,
    fork_runtime,
    state_fingerprint,
)

ORDERS = ("ORD-1", "ORD-2", "ORD-3")
TRUCK_PRIORS = (("TRK-FAST", 0.553), ("TRK-MID", 0.246), ("TRK-HEAVY", 0.201))


def dispatch_candidates(_runtime, step_index):
    """领域无关的候选生成回调：第 k 步处理第 k 张运单。"""
    if step_index >= len(ORDERS):
        return ()
    order_id = ORDERS[step_index]
    return [
        CandidateAction(
            action="dispatch_order",
            params={"order_id": order_id, "truck_id": truck_id},
            prior=prior,
            label=f"{order_id}->{truck_id}",
        )
        for truck_id, prior in TRUCK_PRIORS
    ]


def test_core_package_exports_are_domain_free():
    import open_ontology
    import open_ontology.mcts as core_mcts

    for name in (
        "OntologyMCTSPlanner",
        "CandidateAction",
        "MCTSNode",
        "MCTSPlanResult",
        "fork_runtime",
        "state_fingerprint",
    ):
        assert name in open_ontology.__all__
        assert hasattr(open_ontology, name)

    assert open_ontology.OntologyMCTSPlanner is core_mcts.OntologyMCTSPlanner
    # 核心候选动作只认识 action / params / prior / label，不含任何业务字段
    candidate = CandidateAction(action="dispatch_order", params={"x": 1}, prior=0.5)
    assert set(candidate.to_dict()) == {"action", "params", "prior", "label"}
    assert candidate.label == 'dispatch_order{"x":1}'


def test_runtime_fork_is_native_and_supports_nesting(rt):
    before = rt.state_fingerprint()
    fork = rt.fork()
    nested = fork.fork()
    try:
        fork.submit_action(
            "NATIVE-FORK-1",
            "dispatch_order",
            {"order_id": "ORD-1", "truck_id": "TRK-FAST"},
            DISPATCHER,
        )
        assert fork.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER).status == "assigned"
        # 嵌套分叉基于 fork 的当前状态
        assert nested.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER).status == "pending"
        assert rt.get_object(demo.EmergencyOrder, "ORD-1", DISPATCHER).status == "pending"
        assert rt.state_fingerprint() == before
        assert rt.get_action_run("NATIVE-FORK-1") is None
    finally:
        nested.close()
        fork.close()


def test_state_fingerprint_reports_all_planes(rt):
    fingerprint = rt.state_fingerprint()
    assert set(fingerprint) == {"versions", "objects", "links", "overlay", "runs"}
    assert fingerprint["objects"] == 6
    assert fingerprint["links"] == 0
    assert fingerprint["overlay"] == 0
    assert fingerprint["runs"] == 0
    assert fingerprint["versions"]["EmergencyOrder:ORD-2"] == 1
    assert state_fingerprint(rt) == fingerprint
    assert fork_runtime(rt).state_fingerprint() == fingerprint


def test_runtime_plan_mcts_native_method(rt):
    before = rt.state_fingerprint()
    result = rt.plan_mcts(
        DISPATCHER,
        candidate_generator=dispatch_candidates,
        value_function="evaluate_world_value",
        simulations=80,
        seed=17,
        max_steps=len(ORDERS),
    )
    assert isinstance(result, MCTSPlanResult)
    assert isinstance(result.root, MCTSNode)
    assert result.best_plan_labels == demo.optimal_plan_labels()
    assert result.best_value == demo.EXPECTED_OPTIMAL_VALUE
    assert result.pruned_illegal_nodes == 12
    assert result.forks_created >= result.explored_nodes
    assert result.pruned_errors and all("code" in err for err in result.pruned_errors)
    # 搜索期间主库零写入
    assert rt.state_fingerprint() == before


def test_static_candidate_table_and_commit_plan(rt):
    table = [
        [
            {
                "action": "dispatch_order",
                "params": {"order_id": order, "truck_id": truck},
                "prior": prior,
                "label": f"{order}->{truck}",
            }
            for truck, prior in TRUCK_PRIORS
        ]
        for order in ORDERS
    ]
    result = rt.plan_mcts(
        DISPATCHER,
        candidate_table=table,
        value_function="evaluate_world_value",
        simulations=60,
        seed=17,
    )
    assert result.best_plan_labels == demo.optimal_plan_labels()

    runs = commit_plan(rt, result, DISPATCHER, run_id_prefix="CORE-PLAN")
    assert [run["status"] for run in runs] == ["COMMITTED"] * 3
    value = rt.call_function("evaluate_world_value", {"label": "core"}, DISPATCHER)
    assert value["fulfilled"] == 3
    assert value["total_value"] == demo.EXPECTED_OPTIMAL_VALUE
    assert [run["run_id"] for run in runs] == [
        "CORE-PLAN-1",
        "CORE-PLAN-2",
        "CORE-PLAN-3",
    ]


def test_planner_requires_candidates_and_value_function(rt):
    with pytest.raises(ValueError):
        OntologyMCTSPlanner(rt, DISPATCHER, value_function="evaluate_world_value")
    with pytest.raises(ValueError):
        OntologyMCTSPlanner(rt, DISPATCHER, candidate_generator=dispatch_candidates)


# ---------------------------------------------------------------------------
# 9. AIPAgentExecutor：simulate_sequence 多步连续沙盘推演
# ---------------------------------------------------------------------------
def test_executor_simulate_sequence_chains_state_and_keeps_main_clean(rt):
    executor = AIPAgentExecutor(rt, DISPATCHER)
    before = rt.state_fingerprint()

    result = executor.dispatch(
        "simulate_sequence",
        {
            "steps": [
                {"action": "dispatch_order", "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"}},
                {"action": "dispatch_order", "params": {"order_id": "ORD-2", "truck_id": "TRK-FAST"}},
                {"action": "dispatch_order", "params": {"order_id": "ORD-3", "truck_id": "TRK-MID"}},
            ],
            "value_function": "evaluate_world_value",
        },
    )

    assert result["ok"] is True
    assert result["executed_steps"] == result["total_steps"] == 3
    assert result["failed"] is None
    assert result["main_db_untouched"] is True
    assert result["value"]["total_value"] == demo.EXPECTED_OPTIMAL_VALUE
    assert result["value"]["fulfilled"] == 3
    # 每步都返回真实 ObjectDiff
    assert all(step["diff"]["object_changes"] for step in result["steps"])
    assert result["steps"][2]["diff"]["link_changes"][0]["link_type"] == "serves"
    assert rt.state_fingerprint() == before


def test_executor_simulate_sequence_stops_on_invariant_error(rt):
    executor = AIPAgentExecutor(rt, DISPATCHER)
    before = rt.state_fingerprint()

    result = executor.dispatch(
        "simulate_sequence",
        {
            "steps": [
                {"action": "dispatch_order", "params": {"order_id": "ORD-1", "truck_id": "TRK-FAST"}},
                {"action": "dispatch_order", "params": {"order_id": "ORD-2", "truck_id": "TRK-MID"}},
                {"action": "dispatch_order", "params": {"order_id": "ORD-3", "truck_id": "TRK-MID"}},
            ],
            "value_function": "evaluate_world_value",
        },
    )

    assert result["ok"] is False
    assert result["executed_steps"] == 1
    assert result["failed"]["step"] == 2
    assert result["failed"]["error_code"] == "ETA_OR_COLD_LEVEL_VIOLATION"
    assert result["value"] is None
    assert result["main_db_untouched"] is True
    assert rt.state_fingerprint() == before


def test_executor_simulate_sequence_sees_previous_step_mutation(rt):
    """第 2 步必须基于第 1 步修改后的沙盘状态运行。"""
    executor = AIPAgentExecutor(rt, DISPATCHER)
    result = executor.dispatch(
        "simulate_sequence",
        {
            "steps": [
                {"action": "dispatch_order", "params": {"order_id": "ORD-1", "truck_id": "TRK-FAST"}},
                {"action": "dispatch_order", "params": {"order_id": "ORD-3", "truck_id": "TRK-FAST"}},
            ]
        },
    )
    assert result["ok"] is False
    assert result["failed"]["step"] == 2
    assert result["failed"]["error_code"] == "TRUCK_UNAVAILABLE"


def test_executor_plan_mcts_tool(rt):
    executor = AIPAgentExecutor(rt, DISPATCHER)
    before = rt.state_fingerprint()
    table = [
        [
            {
                "action": "dispatch_order",
                "params": {"order_id": order, "truck_id": truck},
                "prior": prior,
                "label": f"{order}->{truck}",
            }
            for truck, prior in TRUCK_PRIORS
        ]
        for order in ORDERS
    ]
    payload = executor.dispatch(
        "plan_mcts",
        {
            "candidates": table,
            "value_function": "evaluate_world_value",
            "simulations": 60,
            "seed": 17,
        },
    )
    assert payload["best_plan_labels"] == demo.optimal_plan_labels()
    assert payload["best_value"] == demo.EXPECTED_OPTIMAL_VALUE
    assert payload["pruned_illegal_nodes"] == 12
    assert payload["simulations"] == 60
    assert rt.state_fingerprint() == before

    with pytest.raises(ValueError):
        executor.dispatch("plan_mcts", {"candidates": table})
