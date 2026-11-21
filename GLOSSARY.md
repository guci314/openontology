# OpenOntology 术语表 (`GLOSSARY.md`)

> **用途**：统一认知哲学纲领与 Model / Workspace 迁移后的跨文档标准词汇，消除「工作空间 = 业务对象」「TBox/ABox = 知识两阶段」「四个展示视角 = 四套资源账本」「反事实推演 = 外部持久化副作用队列」四类误读。
> 规范正文见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（M2 平台宪法）与 [docs/TASK_BOARD.md](docs/TASK_BOARD.md)（M1 `task_board` / M0 自举规程）；实现水位见 `_state.md`。

---

## 1. 元层次（OMG MOF）

| 术语 | 含义 | 不是什么 |
|---|---|---|
| **M2 / 平台元模型** | 跨领域通用的对象、关系、接口、动作、函数声明原语与微内核治理切面 | 不是任何具体行业的业务规则 |
| **M1 / 领域模型** | 某个业务领域的可复用类型与行为声明，例如 `task_board` | 不是具体对象数据，也不是一个工作空间 |
| **M0 / 工作空间事实** | 绑定模型后产生的具体对象、关系、执行与审计历史 | 不是模型定义的第二份副本 |

## 2. 认知本体论与 M2 核心概念

> **总纲**：智能体无法直接触碰**外部世界（External World）**，只能接收高维的感官**现象（Phenomenon）**并将其压缩接地为内部结构化的**世界状态（World State）**。**世界模型（World Model）**是世界状态的元模型（联结主义中体现为神经网络突触权重 `θ`，符号主义中体现为本体的概念结构与因果转移法则）。在付诸外部行动之前，**规划器（Planner）**以当前世界状态为起点，用同一个世界模型在纯内存的**反事实空间（Counterfactual Space，`world_mode="counterfactual"`）**中推演可能未来，依据**价值函数（Value Function）**择优决策，并由 `OntologyRuntime`（`world_mode="coupled"` + `SourceBridge`）以“沙箱预演 $\hat{\Delta}$ → 外部驱动 → 独立只读回读核验 → 同态原子提交或零污染拒收”的默认闭环通道持续作用于外部世界。

| 术语 | 定义 | 边界 |
|---|---|---|
| **External World & Phenomenon / 外部世界与现象** | 独立于系统运转的客观业务现场与异构源系统（**External World**），经设备驱动与只读观测（`observe_fn` / `SnapshotIngestionDriver`）投射为边界上的观测快照（**Phenomenon / Observation**） | 客观世界不等于观测现象；系统只能通过现象刷新内部世界状态，通过受控动作干预外部世界 |
| **World Model / 世界模型**（符号实现：**Symbolic World Model / 符号世界模型**） | 主体对世界存在结构（Schema）与因果演化律（Dynamics）的内部生成模型，是所有内部状态的**元模型**；在 OpenOntology 中由 `Model`（世界法则） + `Runtime`（确定性转移引擎）承载，并与 `World` 状态实例协作构成完整的 **Symbolic World Model** | 世界模型只负责客观**事实演化判断**（`s_{t+1} = F(s_t, a_t)`），本身不含特定时刻的业务实例数据（联结主义中突触权重是世界模型，神经元瞬时激活向量才是世界状态） |
| **Model / 领域模型** | 以 `model_id` 标识的可复用世界模型定义包：结构（TBox）+ 因果转移法则（Behavior） | 不因启动执行而变成事实；不是数据库 |
| **TBox** | 类型侧结构投影：`ObjectType`、字段（含 `AutoID` 与 `DatatypeProperty`）、`InterfaceType`、`ObjectProperty`、声明式约束、类型元数据（含 `aggregate_root`） | 不是知识流转阶段，也不是运行时实例值 |
| **`ObjectProperty` / 对象属性** | OWL 跨对象关系原语：以 `ObjectProperty[Target]` / `ObjectProperty[tuple[Target, ...]]` 或 `spec.object_property(...)` 声明 `domain`、`range`、`cardinality` 与连边主权；写入时自动同步 `ontology_links` 三元组并强制引用完整性（`TARGET_NOT_FOUND`） | 替代关系表外键命名与旧版独立连边原语；不是可任意手填的标量文本字段 |
| **`AutoID` / 系统自动编号** | 主键/动作编号注解：在 TBox 与 Behavior 契约投影 `auto_generated: True`，用于生成只读系统编号表单控件并由动作自动分配序号 | 不是手填外键，也不跳过唯一性与不变量校验 |
| **Behavior** | 受控因果变化契约：`ActionDef`、`FunctionDef`、`AutomationDef`、`InvariantDef`、`TransitionInvariantDef`、角色授权与参数 Schema | 不能全部塞进静态结构 TBox，也不是第二套执行引擎；不设 M2 异步审批挂起态 |
| **ABox** | 事实侧投影：具体对象属性值、关系断言、执行与审计历史 | 不是知识流转阶段，也不是模型声明 |
| **World / 世界状态（信念态 Belief State）**（界面 `World · 世界状态`；曾用名 `业务世界`；代码兼容别名 `Workspace / 工作空间`） | 现象经世界模型降维接地后，以 `workspace_id`（`world_id`）标识的当下**实然状态**实例：`model_id` 引用 + ABox + Runtime 绑定 + 演化历史（契合方向为 *Mind-to-World* 心智顺应世界） | **不是外部客观世界本身**，**不是单个业务对象**，也不是源代码工作目录 |
| **Counterfactual Space / 反事实空间（想象空间 Imagination）** | 由**同一个世界模型**生成的可能世界空间（Possible Worlds）：通过 `runtime.fork()` 克隆至严格纯 `:memory:` 沙盘（`world_mode="counterfactual"`、`source_bridge=None`）推演假想未来（*What-if*） | 与世界状态共享同一元模型，但与外部物理设备彻底绝缘，对实然世界状态及外部系统零写入 |
| **Homomorphic Settlement / 同态核销闭环（`SourceBridge`）** | `OntologyRuntime` 在 `world_mode="coupled"` 下的默认三阶段无锁纯同步结算路径（实现在 `open_ontology/settlement.py`）：Phase A 沙箱推演预期差集 $\hat{\Delta}$ 并门控权限与前置不变量；Phase B 脱离写门禁调用单写设备 `SourceBridge.actuate` 并只采信独立 `SourceBridge.observe`；Phase C 逐项核对回读终态，一致原子入库（`COMMITTED`），失配抛 `DeviceStateMismatchError` 且主库零改动（`REJECTED`） | 同步事务闭环；不落独立持久化动作队列表，不引入 M2 挂起态 |
| **Value Function / 价值函数（评价体系 Valuation）** | 对世界状态好坏与目的性的标量度量 `V(s)`（联结主义中为连续直觉价值网络，符号主义中为 `@spec.function` 显式规则效用函数与搜索树节点动作价值估计 `Q(s, a)`） | 世界模型管“事实判断（将会怎样）”，价值函数管“价值判断（想要怎样）” |
| **Planner / 规划器** | 连接“世界模型”与“价值函数”的实践理性中枢：由前瞻树搜索（`open_ontology.mcts.OntologyMCTSPlanner`）与滚动时域闭环控制器（`open_ontology.mpc.ClosedLoopMPCController`）构成，在反事实空间中剪除违反不变量的死局并滚动重算 | 位于显式按需导入的扩展子包，搜索阶段永不直写实然世界状态或外部设备 |
| **Runtime / 执行引擎** | 执行模型查询、受控动作、治理切面、持久化与反事实沙盘推演的纯同步微内核实例（`open_ontology/runtime.py`），以 `runtime_id` 标识 | 不是额外一份业务事实，也不依赖 Design-Time / Induction / Workshop / MCTS |
| **Device / 外部设备** | 通过 `DeviceDriverRegistry` 与 `SourceBridge`（或 `SnapshotIngestionDriver`）挂载的外部确定性系统与观测连接器；外部智能体直接作为 `Actor` 通过 OSDK / `ActionDef` 与世界模型交互 | 不是未经校验即可写入事实图的可信数据 |

### 2.1 同一元模型下的运行模式与双空间绝缘（同构 6 表原理）

OpenOntology 的实然主库与反事实分叉复用完全同构的 6 表结构（5 张业务治理表 + `ontology_write_gate` 写门禁表，`ontology_action_runs.status` 仅允许 `COMMITTED / REVERTED / REJECTED`；除 SQLite `:memory:` / `.sqlite3` 外，亦可运行在 PostgreSQL 16 `open_postgres_runtime` + `schema_pg.sql` 跨 schema 物理写门禁底座上），并通过 `world_mode` 与物理连接器绑定严格区分职责与读写边界：

| 运行模式（`world_mode`） | 标准术语 | 现行代码载体 | 现实承诺与隔离红线 |
|---|---|---|---|
| **`standalone`** | **World State / 独立世界状态** | 主 `OntologyRuntime`（SQLite 或 PostgreSQL 16，`source_bridge=None`） | 仅治理本体自有业务状态，不关联外部源系统 |
| **`coupled`** | **World State / 耦合同态世界状态** | 主 `OntologyRuntime` + `SourceBridge` / `DeviceDriverRegistry`（`settlement.py` / `drivers.py` / 可选 `SagaReconciler`） | 凡触碰非自有（source-backed）对象的纯同步提交与授权冲正，必须走无锁三阶段同态核销（*World-to-Mind*），受单动作单写设备红线约束 |
| **`counterfactual`** | **Counterfactual Space / 反事实空间** | `runtime.fork()`（强制 `db_path=':memory:'` 且 `source_bridge=None`） | 纯内存想象与 MCTS 试错分叉（即使主库为 PostgreSQL 16 亦零锁克隆为内存沙盘）；物理上拒绝挂载 `SourceBridge` 与磁盘持久化 |

### 2.2 Pure Runtime、Design-Time、生产运行壳与归纳 / 解析原语

| 术语 | 含义 | 边界 / 代码载体 |
|---|---|---|
| **Pure Runtime vs Design-Time（Gate 6 物理隔离）** | 像 Java JVM 与 `javac`/JUnit 一样拆分：`import open_ontology` 只加载 10 个核心 Runtime 模块（8,781 行），对编译/证伪/AI-FDE/作战台/MCTS/MPC 及可插拔生产运行壳完全不可知；`python -m open_ontology.design check \| verify` 在设计期负责静态编译、沙箱校验与签发 | `open_ontology/__init__.py` 零 `__getattr__`；`tests/test_architecture_boundaries.py` **Gate 6** 零例外门禁校验静态闭包与子进程 `sys.modules` 白名单 |
| **`DeviceDriverRegistry`（设备驱动注册表与单动作单写设备红线）** | 纯 M2 多外设驱动总线：统一管理外部 `MountedDevice` 的 `mount` / `unmount` / `set_status` / 原子 `replace` 回滚、防重冲突校验、共享主数据合并观测，并实时投影至 `Workshop` 连接器视图 | `open_ontology/drivers.py`；单动作触及 $\ge 2$ 台外设或跨路由写即在任何外写发生前以 `CROSS_DEVICE_WRITE_FORBIDDEN` fail-closed 拒收，严禁伪造 2PC |
| **`WorkCard`（M1 统一广义协同工单）** | 替代已退役 M2 挂起机制的 M1 显式协同领域实体（覆盖 `APPROVAL` / `HUMAN_TASK` / `DEPT_TICKET` / `ENTITY_DISAMBIGUATION` 等工作卡），配合 `APPROVAL_REQUIRED_*` 前置不变量实现“$t_0$ 阻塞开卡 → 责任人认领处理 → $t_1$ 现场防幻读重算执行” | `examples/std_core_ontology.py`、`examples/business_ontology.py`、`tests/test_work_card_m1_architecture.py`；强制 SoD 职责分离与完成防静默写 |
| **Standard Domain Ontology + Monotonic `CustomerOverlay`（标准领域本体与只增不破覆层 / 闭集角色）** | 行业标准薄骨架（`std_core` / `std_crm` / `std_o2c` / `std_p2p`）仅固化领域公共不变量与闭合标准角色表（`STD_ROLES`），企业个性化规则、字段、前置拦截与窄视图通过零 `eval/exec` 的单调 Overlay 叠加编译 | `open_ontology/induction/dsl.py`（`compose_blueprints` / `apply_overlay`）；破坏标准契约或拼错角色即在编译期抛 `OVERLAY_BREAKS_STANDARD_CONTRACT` / `UNKNOWN_ROLE` |
| **4-Tier Entity Resolution & `CrosswalkTable`（L0–L4 实体解析漏斗与主数据别名映射）** | 面向跨设备同名异构主数据的纯 M2 四级归并漏斗：L0 人工已拍板确认、L1 标准化硬键（如税号）精确合并并刚性阻断 `CONFLICTING_HARD_KEY`、L2–L3 归一化准键与邻域加权自动合并、L4 灰区生成 `WorkCard(HUMAN_TASK)` 供 Steward 确认或分拆；产出不可变 `CrosswalkTable` 注入适配器 | `open_ontology/induction/resolution.py`（`resolve_entities` / `apply_resolution_decisions` / `inject_crosswalk_into_mapping`）；严禁无硬键灰区强行合并 |
| **Popperian `DifferentialFalsifier`（覆盖率驱动的波普尔差分证伪）** | 设计期零 LLM 确定性裁判：从真实源库与 `Target ABI` 自动生成多波次覆盖探针与红队越权对抗用例，要求真实沙箱与参考本体在动作与状态跃迁高覆盖率（`action_coverage` / `transition_coverage`）下双边状态差分完全一致且零逃逸、零脏写，通过后方签发 `CORROBORATED` | `open_ontology/induction/falsifier.py` 与 `open_ontology/design.py`（`verify`） |
| **Commonsense Thin Waist（常识细腰 / REA 5 原语）** | 跨企业、跨标准域互操作的最小公开常识协议面（`AccountingEntity` / `BusinessParty` / `TradeResource` / `TradeCommitment` / `FulfillmentEvent`），配合字段白名单信封严格屏蔽各自内部 CRM/ERP 私有概念，受 **Gate 7** 零交叉导入门禁保护 | `examples/std_core_ontology.py`、`examples/commonsense_exchange.py`、`tests/test_architecture_boundaries.py`（Gate 7） |
| **Autonomy Ramp（三级自治度影子爬坡）** | 智能体上线的可控权限开关，支持按动作与角色配置 **`SHADOW`**（零外写记录影子账本 `SHADOW_RECORDED`）、**`COPILOT_ESCALATE`**（只出建议转人工工单）与 **`AUTO_UNDER_CEILING`**（定量上限内自动执行、超限或指标缺失 fail-closed 升级） | `open_ontology/autonomy.py`（`AutonomyRampController` / `AutonomyPolicy`）；独立于 M2 Pure Runtime |
| **Observe-Before-Retry Saga Reconciliation（写后失联先回读对账）** | 封装耦合外设执行期间的网络超时、连接断开或 ACK 丢失保护：异常发生时不盲目重试，先只读 `observe()` 外部物理终态后再分流为 `RECONCILED_REMOTE_COMMITTED`、`RECONCILED_NOT_COMMITTED` 或 `AMBIGUOUS_ESCALATED` | `open_ontology/reconciliation.py`（`SagaReconciler`）；保证耦合外写至多一次语义 |
| **MDM Sidecar Plugin（外挂主数据协调插件）** | 零侵入 M2 内核的外挂主数据协调插件：消费多 ERP/CRM 批次并驱动增量 `L0–L4 CrosswalkTable` 对账、安全映射热刷新、L4 灰区幂等派发 `WorkCard` 及 Steward 核准后锁定 `L0_CONFIRMED` | `open_ontology/mdm_plugin.py`（`MDMSidecarPlugin`）；支持 SQLite 与 PostgreSQL 16 |
| **MCP Runtime Shell（标准 MCP 生产运行壳）** | 纯 Python stdlib 实现的 JSON-RPC 2.0 MCP 运行壳，同时支持 **stdio** 与 **Streamable HTTP (`POST /mcp`)**，服务端硬绑定调用方 `Actor` 与 `ScopedView` 防止提示词/参数篡改身份 | `open_ontology/mcp_server.py`（`OntologyMCPServer` / `start_mcp_http_server`） |

## 3. 身份与寻址

```text
Model      <model_id>                      可复用定义 / 世界模型法则
World      <world_id / workspace_id>       隔离世界状态实例（绑定 model_id + runtime_id，代码标识符 workspace_id）
Object     <workspace_id> / <object_type> / <object_id>
```

* **业务对象地址**是 `workspace_id / object_type / object_id` 三级；完整身份 = 业务世界（工作空间）+ 类型 + 对象 ID。
* **工作空间身份**（`workspace_id`，即业务世界身份 `world_id`）与**业务对象身份**（`object_type / object_id`）是两种不同身份：聚合根对象 ID（如 `BD-CC-001`）绝不能当作工作空间 / 业务世界 ID 使用。
* 不同业务世界允许存在相同的 `object_id`；跨工作空间按对象寻址出现多命中时必须显式返回候选（`AmbiguousTakeoverError`），严禁盲取第一个命中。
* **`workspace_root` / `workdir`** 是文件系统代码工作目录参数，仅表示本地源码位置，**不是**业务世界 `workspace_id`。历史命名保留，行为不变。

## 4. 兼容拼写

| 旧拼写 | 现行含义 | 关系 |
|---|---|---|
| `Workspace` / `workspace_id` | 世界状态实例（`World`）在代码、HTTP 参数与目录清单中的兼容标识 | 与界面 `World · 世界状态` 指向同一实体 |
| `OntologySpec` / `ontology_id` | M1 领域模型定义容器 / 模型身份 | `ontology_id` 与 `model_id` 指向同一个值 |
| `instance_id` / `?instance=` | 早期工作空间 / 业务世界身份 / HTTP 查询参数 | 与 `workspace_id` / `?workspace=` 双栈兼容；冲突参数显式 400 |
| `register_instance` / `get_instance` | 工作空间 / 业务世界登记 / 查找 | 与 `register_workspace` / `get_workspace` 指向同一份底层条目 |
| `#tab-ontology` / `#tab-instance` | 兼容期 DOM ID | 界面展示为 `Model · 领域模型` / `World · 世界状态`（曾用名 `World · 业务世界`、`Workspace · 工作空间`） |
| `OntologyRuntime` | 绑定单个模型与单个存储的执行引擎 | 不因术语收敛而重写 |

## 5. 聚合根与可用性

* **聚合根（aggregate root）**：`ObjectType.__aggregate_root__` 显式声明的**类型级元数据**（默认 `False`），用于业务世界导航与入口暴露。严禁按图入度、类型名或主键命名自动猜测；它不改变动作级事务边界。
* **业务世界可用性（`availability`）与根状态（`root_status`）**：
  * `available`：已装配 Runtime，可受控查询（此时 `root_status` 区分 `roots` 有可见根、`empty` 已声明根但当前无可见根、`no_declared_roots` 模型未声明根类型）；
  * `not_loaded`：仅在目录清单登记，尚未打开（`aggregate_roots` 为 `None`，不是空世界）；
  * `unavailable`：存储不可读、已关闭或损坏（`aggregate_roots` 为 `None`，不是零根对象）；
  * 模型**未声明根类型** ≠ **已声明但当前无可见根对象** ≠ `not_loaded` ≠ `unavailable`。

## 6. 展示视角

指挥台的 `Model / Runtime / World / Device` 是**同一份资源的四种观察视角**，不是四个完全正交、各自独立管理生命周期的资源账本。TBox 与 ABox 是逻辑投影，不拆成两个一级标签页；业务世界事实在界面标注为 ABox，模型侧区分 TBox 结构与 Behavior 行为。
