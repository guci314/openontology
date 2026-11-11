# OpenOntology 规格说明书（TK-ONTO-012 · AlphaGo 蒙特卡洛树搜索 (MCTS) 在符号世界模型中的多步沙盘推演 Demo）

## 1. 核心原理与目标

**「AlphaGo 的蒙特卡洛树搜索（MCTS），本质上是在确定性的符号主义世界模型中做多步沙盘推演。」**
单步的 `simulate_action` 只能看一步（Greedy 局部贪心）；而在真实的复杂企业运营（如多订单连环履约调度、多炉次连续配料）中，**第一步看似最便宜的贪心动作，往往会把第二步、第三步逼入违反业务不变量的死胡同（Dead End）**。

本工单在 `/Users/guci/work/open-ontology` 中实现一套通用的 **基于 `OpenOntology` 符号世界模型的 AlphaGo MCTS 多步推演规划器（`OntologyMCTSPlanner`）** 与完整对比演示：
- 新增演示与引擎：`examples/mcts_world_model_demo.py`
- 新增测试套件：`tests/test_mcts_world_model.py`（至少 10 条测试）

> **⚠️ 并发隔离纪律**：
> 只允许创建和写入 `examples/mcts_world_model_demo.py` 与 `tests/test_mcts_world_model.py`，严禁修改 `open_ontology/` 已有文件或触碰 `video*` 目录，测试只运行 `pytest -v tests/test_mcts_world_model.py`。

---

## 2. AlphaGo MCTS 如何与 `OpenOntology` 原语严丝合缝结合？

```text
                 [ Root State s0 (主 OntologyRuntime 快照克隆) ]
                        /                |                \
         Action a1 (贪心近车)      Action a2 (次近车)     Action a3 (非法车)
                  /                      |                      \
         [ State s1 ]              [ State s2 ]           ❌ InvariantError
         /          \              /          \              (符号裁判当场剪枝!)
   ❌ Step2 死局   ❌ Step2 死局  [ State s2_1 ] [ State s2_2 ]
 (王牌车被占，血浆单超时!)             |
                               [ Terminal s3 ]
                                       ↓
                      调用 @spec.function("evaluate_state_value")
                      计算终局精确业务价值 V(s3) 并沿树反向传播 (UCT Backprop)
```

1. **零副作用平行宇宙分叉（`fork_runtime(runtime) -> OntologyRuntime`）**：
   - 利用 SQLite 原生内存备份 `runtime.connection.backup(fork_conn)`，在毫秒级将当前 `OntologyRuntime`（含全部 `objects`, `links`, `overlay`）克隆出一个完全独立的沙盘分支，主库在整个 MCTS 搜索期间 **0 写入、0 污染**。
2. **MCTS 四阶段循环（Selection $\to$ Expansion $\to$ Simulation/Rollout $\to$ Backpropagation）**：
   - **Selection（PUCT / UCT 选择）**：
     $$\text{Score}(s, a) = Q(s, a) + c_{\text{puct}} \cdot P(s, a) \cdot \frac{\sqrt{N(s)}}{1 + N(s, a)}$$
     其中 $P(s, a)$ 是策略先验（Policy Prior，例如启发式或 LLM 给出的单步偏好概率）。
   - **Expansion & Symbolic Legal Mask（符号规则裁判与剪枝）**：
     在沙盘分支上调用 `simulate_action` / `submit_action` 执行候选动作 $a$：
     - 若抛出 `InvariantError`（违反冷链、时效、库存、主权等硬护栏），符号世界模型直接将该分支标记为 **`ILLEGAL / DEAD_END`** 并赋予惩罚值，立即剪枝！
   - **Simulation / Rollout & Value Function $V(s)$**：
     往前推演完 $H$ 步（Horizon，例如连续分配 3 张急救订单）后，调用注册在 Ontology 上的 **`@spec.function("evaluate_world_value")`** 计算终局总净价值 $V(s_H)$（总履约收益 $-$ 运输成本 $-$ 未履约重罚）。
   - **Backpropagation（反向传播更新 $N(s,a), W(s,a), Q(s,a)$）**：
     将终局价值沿搜索路径回传更新每个节点的平均价值 $Q(s,a)$ 与访问次数 $N(s,a)$。

---

## 3. 业务场景设计：「三波次急救冷链连环调度 —— 为什么单步贪心必死、MCTS 破局」

构造一个经典的**「贪心陷阱（Greedy Trap）」**企业调度问题：

1. **初始局势 $s_0$**：
   - 待按序调度的 3 张急救冷链运单（每张运单有各自的基础收益、硬性最大抵达时限 `max_eta_hours`、所需冷链等级 `min_cold_level` 与断供违约金 `penalty`）：
     - **`ORD-1`（普通胰岛素补货）**：时限 $\le 4.0\text{h}$，冷链等级 $\ge 1$，基础价值 `10,000` 元；
     - **`ORD-2`（特级移植器官/血浆急单）**：时限 **$\le 1.5\text{h}$**，冷链等级 **$\ge 2$**，基础价值 `25,000` 元（若无合法车辆可派则触发断供违约 `-50,000` 元！）；
     - **`ORD-3`（郊区疫苗配送）**：时限 $\le 3.0\text{h}$，冷链等级 $\ge 1$，基础价值 `12,000` 元。
   - 可用的 3 辆冷链车（每辆车只能承运一张运单）：
     - **`TRK-FAST`（王牌超低温快车）**：ETA `1.0h`，冷链等级 `2`，运费仅 `400` 元（**注意：因为运费最低且最快，单步贪心 Policy Prior 对它的偏好度最高！但它更是全场的唯一一辆能满足 `ORD-2` $\le 1.5\text{h}$ 且等级 $\ge 2$ 的救命车！**）；
     - **`TRK-MID`（中型冷链车）**：ETA `2.2h`，冷链等级 `1`，运费 `900` 元；
     - **`TRK-HEAVY`（长途冷链车）**：ETA `3.5h`，冷链等级 `1`，运费 `1,100` 元。
2. **对比实验（Greedy vs. MCTS）**：
   - **方法 A：单步贪心策略（Greedy Policy / 普通单步 ReAct Agent）**：
     - 在第 1 步看 `ORD-1` 时，贪图 `TRK-FAST` 运费最便宜（400 元 vs 900 元），直接把 `TRK-FAST` 派给了 `ORD-1`；
     - 到了第 2 步处理 `ORD-2`（要求 ETA $\le 1.5\text{h}$ 且冷链等级 $\ge 2$）时，剩下的 `TRK-MID` 和 `TRK-HEAVY` 全部触发 `InvariantError(code="ETA_OR_COLD_LEVEL_VIOLATION")`，**第 2 步陷入死局（Dead End），造成血浆断供巨额亏损**！
   - **方法 B：AlphaGo MCTS 沙盘树搜索（`OntologyMCTSPlanner(simulations=80)`）**：
     - 虽然单步先验 $P(s_0, \text{TRK-FAST})$ 最高，但 MCTS 在沙盘里往前推演到第 2 步时发现 `ORD-1 -> TRK-FAST` 分支下的所有子节点全部撞上 `InvariantError` 死局，$Q(s_0, \text{TRK-FAST})$ 迅速暴跌为负值；
     - MCTS 自动探索并锁定全局最优三步序列：
       - Step 1: **`ORD-1 -> TRK-HEAVY`**（ETA 3.5h $\le 4.0$h，运费 1100）
       - Step 2: **`ORD-2 -> TRK-FAST`**（ETA 1.0h $\le 1.5$h，等级 2，运费 400 —— 救命车精准留给特级单！）
       - Step 3: **`ORD-3 -> TRK-MID`**（ETA 2.2h $\le 3.0$h，运费 900）
     - 三单全部合法履约，终局总净价值 $V = (10000 + 25000 + 12000) - (1100 + 400 + 900) = \mathbf{44,600}$ 元！
   - **主库零污染验证**：80 次 MCTS 树搜索跑完后，主 `runtime` 中的对象版本号与 `action_runs` 条数保持未变，随后将 MCTS 最优动作序列正式 `submit_action` 落盘，并验证支持 `revert_action` 一键回滚。

---

## 4. 验收要求

1. 运行 `python3 -m examples.mcts_world_model_demo` 打印：
   - 初始局势与单步贪心策略陷入死局的过程（`Greedy Failed at Step 2: InvariantError`）；
   - MCTS 树搜索过程统计（根节点各动作的访问次数 $N(s_0, a)$、平均价值 $Q(s_0, a)$、被符号裁判剪枝的非法节点数）；
   - MCTS 选出的全局最优三步规划序列及主库正式提交后的审计账本。
2. 在 `tests/test_mcts_world_model.py` 编写至少 10 条测试，运行 `pytest -v tests/test_mcts_world_model.py` 100% 通过。
