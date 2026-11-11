# OpenOntology 规格说明书（TK-ONTO-014 · 将多步平行宇宙沙盘 fork() 与通用 MCTS 规划引擎正式整合进 open_ontology 核心包）

## 1. 目标

将此前在 `examples/mcts_world_model_demo.py` 中验证通过的「平行宇宙沙盘克隆（`fork_runtime`）」与「通用 AlphaGo 蒙特卡洛树搜索规划器（`OntologyMCTSPlanner`）」正式提炼并整合进 `/Users/guci/work/open-ontology/open_ontology/` 核心包，使其成为任何业务领域本体（冷链、O2C、钢厂配料、SaaS 运营）开箱即用的一等公民核心原语！

---

## 2. 核心包改造要求

```text
/Users/guci/work/open-ontology/
├── open_ontology/
│   ├── __init__.py           # 导出 fork_runtime, CandidateAction, MCTSNode, MCTSPlanResult, OntologyMCTSPlanner
│   ├── runtime.py            # OntologyRuntime 新增原生 .fork() 与 .plan_mcts(...) 方法
│   ├── mcts.py               # ★ 新增：领域无关的通用符号世界模型 MCTS 沙盘推演引擎
│   └── osdk.py               # AIPAgentExecutor 新增 simulate_sequence 与 plan_mcts 工具分派
├── examples/
│   └── mcts_world_model_demo.py # 改为直接从 open_ontology 导入核心 MCTS 组件
└── tests/
    └── test_mcts_world_model.py # 新增核心包集成与 AIPAgentExecutor MCTS 调用测试
```

### 2.1 `open_ontology/runtime.py` 新增原生方法

1. **`runtime.fork() -> OntologyRuntime`**：
   - 使用 SQLite 原生 `self._conn.backup(fork._conn)` 将当前 `OntologyRuntime`（包含 `ontology_registry`, `ontology_objects`, `ontology_links`, `ontology_overlay`, `ontology_action_runs` 及写门禁状态）毫秒级克隆为一个完全独立的内存沙盘 `OntologyRuntime` 实例；
   - 支持嵌套分叉（`fork1 = runtime.fork(); fork2 = fork1.fork()`），任何在 `fork` 上的修改对母体 `runtime` 100% 零写入、零污染。
2. **`runtime.state_fingerprint() -> dict[str, Any]`**：
   - 返回当前世界状态的结构化指纹（对象数、各对象版本号、关系数、overlay 数、action_runs 数），用于验证沙盘推演前后主库零污染。
3. **`runtime.plan_mcts(...) -> MCTSPlanResult`**：
   - 快捷调用 `OntologyMCTSPlanner(self, ...).search(...)`。

### 2.2 `open_ontology/mcts.py`（领域无关的通用 MCTS 核心组件）

将 `OntologyMCTSPlanner`、`CandidateAction`、`MCTSNode`、`MCTSPlanResult`、`fork_runtime`、`state_fingerprint` 沉淀到 `open_ontology/mcts.py`：
- **完全领域无关（零硬编码特定业务类名）**：
  - 支持通过回调函数 `candidate_generator(runtime, step_index) -> Sequence[CandidateAction]` 或静态候选表生成每步合法/候选动作及先验概率 $P(s,a)$；
  - 支持通过 `@spec.function` 名称（如 `value_function="evaluate_world_value"`）或自定义 `value_fn(runtime)` 计算终局价值 $V(s)$；
  - 内置 PUCT 公式选择、基于 `InvariantError` 的符号规则裁判自动剪枝（记录 `pruned_errors`）、多步沙盘 Rollout、反向传播更新 $N(s,a) / W(s,a) / Q(s,a)$、以及 `commit_plan(runtime, plan_result, actor, run_id_prefix)` 一键将最优规划序列正式提交到主库。

### 2.3 `open_ontology/osdk.py`（`AIPAgentExecutor` 工具集成）

在 `AIPAgentExecutor.dispatch` 中新增支持：
- **`simulate_sequence`**：传入一组动作序列 `[{"action": ..., "params": ...}, ...]`，自动在 `runtime.fork()` 沙盘分支上按序连续推演多步（第 $k$ 步基于前 $k-1$ 步修改后的沙盘状态运行！），若中途某步触发 `InvariantError` 则返回失败步号与错误码，若全部通过则返回每步 `ObjectDiff` 与可选 `value_function` 评估值，**主库零修改**！

### 2.4 `open_ontology/__init__.py` 顶层导出

在 `open_ontology/__init__.py` 的 `__all__` 中正式导出：
- `fork_runtime`, `state_fingerprint`, `CandidateAction`, `MCTSNode`, `MCTSPlanResult`, `OntologyMCTSPlanner`

---

## 3. 验收要求

1. `examples/mcts_world_model_demo.py` 改为直接从 `open_ontology` 核心包导入 `OntologyMCTSPlanner`、`CandidateAction`、`fork_runtime`、`state_fingerprint`，实跑 `python3 -m examples.mcts_world_model_demo` 输出正常。
2. 在 `tests/test_mcts_world_model.py` 中新增测试：
   - 验证 `from open_ontology import OntologyMCTSPlanner, CandidateAction, fork_runtime` 顶层导入与 `runtime.fork()` / `runtime.plan_mcts(...)` 原生方法；
   - 验证 `AIPAgentExecutor.dispatch("simulate_sequence", ...)` 多步连续沙盘推演（含中途触发 `InvariantError` 拦截与主库零污染断言）。
3. 运行 `pytest -v tests/test_mcts_world_model.py tests/test_open_ontology.py` 100% 通过后汇报。
