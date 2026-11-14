# OpenOntology 术语表 (`GLOSSARY.md`)

> **用途**：统一 Model / Workspace 迁移后的跨文档词汇，消除「工作空间 = 业务对象」「TBox/ABox = 知识是否激活」「四个展示视角 = 四套资源账本」三类误读。
> 规范正文见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（M2 平台宪法）与 [docs/TASK_BOARD.md](docs/TASK_BOARD.md)（M1 `task_board` / M0 自举规程）；实现水位见 `_state.md`。

---

## 1. 元层次（OMG MOF）

| 术语 | 含义 | 不是什么 |
|---|---|---|
| **M2 / 平台元模型** | 跨领域通用的对象、关系、接口、动作、函数声明原语与微内核治理切面 | 不是任何具体行业的业务规则 |
| **M1 / 领域模型** | 某个业务领域的可复用类型与行为声明，例如 `task_board` | 不是具体对象数据，也不是一个工作空间 |
| **M0 / 工作空间事实** | 绑定模型后产生的具体对象、关系、执行与审计历史 | 不是模型定义的第二份副本 |

## 2. 核心概念

| 术语 | 定义 | 边界 |
|---|---|---|
| **Symbolic World Model / 符号世界模型** | `Model`（世界法则） + `Runtime`（世界模拟器） + `World`（世界状态实例）构成的完整受控推演体系 | 并非单一存储或纯静态规则；三者协作完成状态转移与 MCTS 推演 |
| **Model / 领域模型** | 以 `model_id` 标识的可复用定义包：结构（TBox）+ 行为/转移法则（Behavior） | 不因启动执行而变成事实；不是数据库 |
| **TBox** | 类型侧结构投影：`ObjectType`、字段、`InterfaceType`、`LinkType`、声明式约束、类型元数据（含 `aggregate_root`） | 不是知识激活流程中的阶段，也不是运行时实例值 |
| **Behavior** | 受控变化契约：`ActionDef`、`FunctionDef`、`AutomationDef`、角色授权、审批与参数 Schema | 不能全部塞进静态结构 TBox，也不是第二套执行引擎 |
| **ABox** | 事实侧投影：具体对象属性值、关系断言、执行与审计历史 | 不是知识激活流程中的阶段，也不是模型声明 |
| **World / 业务世界**（代码兼容别名 `Workspace / 工作空间`） | 以 `workspace_id`（`world_id`）标识的隔离业务世界实例：`model_id` 引用 + ABox + Runtime 绑定 + 演化历史（含主世界与 `fork()` 平行沙盘世界） | **不是单个业务对象**，也不是源代码工作目录 |
| **Runtime / 执行引擎** | 执行模型查询、动作、治理切面、持久化与沙盘推演的引擎实例，以 `runtime_id` 标识 | 不是额外一份业务事实 |
| **Device / 外部设备** | 登记并适配外部系统（OpenAPI）与智能体（A2A Agent Card）的边界 | 不是未经校验即可写入事实图的可信数据 |

## 3. 身份与寻址

```text
Model      <model_id>                      可复用定义 / 世界法则
World      <world_id / workspace_id>       隔离业务世界（绑定 model_id + runtime_id，代码标识符 workspace_id）
Object     <workspace_id> / <object_type> / <object_id>
```

* **业务对象地址**是 `workspace_id / object_type / object_id` 三级；完整身份 = 业务世界（工作空间）+ 类型 + 对象 ID。
* **工作空间身份**（`workspace_id`，即业务世界身份 `world_id`）与**业务对象身份**（`object_type / object_id`）是两种不同身份：聚合根对象 ID（如 `BD-CC-001`）绝不能当作工作空间 / 业务世界 ID 使用。
* 不同业务世界允许存在相同的 `object_id`；跨工作空间按对象寻址出现多命中时必须显式返回候选（`AmbiguousTakeoverError`），严禁盲取第一个命中。
* **`workspace_root` / `workdir`** 是文件系统代码工作目录参数，仅表示本地源码位置，**不是**业务世界 `workspace_id`。历史命名保留，行为不变。

## 4. 兼容拼写

| 旧拼写 | 现行含义 | 关系 |
|---|---|---|
| `Workspace` / `workspace_id` | 业务世界（`World`）在代码、HTTP 参数与目录清单中的兼容标识 | 与界面 `World · 业务世界` 指向同一实体 |
| `OntologySpec` / `ontology_id` | M1 领域模型定义容器 / 模型身份 | `ontology_id` 与 `model_id` 指向同一个值 |
| `instance_id` / `?instance=` | 早期工作空间 / 业务世界身份 / HTTP 查询参数 | 与 `workspace_id` / `?workspace=` 双栈兼容；冲突参数显式 400 |
| `register_instance` / `get_instance` | 工作空间 / 业务世界登记 / 查找 | 与 `register_workspace` / `get_workspace` 指向同一份底层条目 |
| `#tab-ontology` / `#tab-instance` | 兼容期 DOM ID | 界面展示为 `Model · 领域模型` / `World · 业务世界`（兼容旧称 `Workspace · 工作空间`） |
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
