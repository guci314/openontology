# 任务看板领域模型与自举协同规范 (M1 `task_board` & M0 Dogfooding)

> **元模型定位（OMG MOF M1 / M0）**：
> * **M2 平台底座**：遵循 [docs/ARCHITECTURE.md](ARCHITECTURE.md) 定义的通用元模型、沙箱铁笼与切面规范；
> * **M1 领域模型（`task_board`）**：在 `open_ontology/kanban.py` 中定义的通用多角色任务看板本体，包含领域类型、列驱动状态流转与完工验收不变量；
> * **M0 自举工作空间（`board-prod`）**：绑定 `task_board` 模型与 `.instances/board_prod.sqlite3` 存储、以聚合根 **`Board / BD-CC-001`** 管理 OpenOntology 自身研发演进的运行空间。

---

## 1. M1 `task_board` 领域模型契约

### 1.1 核心对象类型、关系与聚合根

* **聚合根声明**：`Board` 显式声明 `__aggregate_root__ = True`，是工作空间内看板业务的唯一聚合根类型；其余从属对象不声明为根：
  * `Board`（主键 `board_key`）：承载看板标题、租户、`goal`（目标与架构约束全文）、`state`（当前基线快照全文）与 `unfolded_completions`（待折叠完工计数）。
  * `BoardColumn`（列语义即数据）：通过 `purpose`（`initial` / `ready` / `working` / `completion` / `failure` / `escalation`）与事件路由字段（`on_claimed`、`on_completed`、`on_failed`、`on_exhausted`、`on_blocked`、`on_abandoned`、`on_unblocked`）驱动卡片流转，不在内核写死列名。
  * `Member`：团队成员及角色（`developer` / `tester` / `reviewer` / `coordinator`）、忙闲状态（`IDLE` / `BUSY`）与当前认领卡片。
  * `Card`：工作项卡片，通过 `card_waits_for` 连边表达前置依赖，未满足前置时驻留 `initial` 列，前置全部完成后由 `promote_children` 提升至 `ready` 列。
  * `CardRun` 与 `CardComment`：每次认领执行的技术运行记录与评审/协调评论轨迹。
  * `Evidence(metric, value, detail)`：收单验收的值对象契约。

### 1.2 M1 专属收单门禁①：量化数值证据硬门禁

`task_board` 的 `record_outcome(outcome="completed")` 动作强制校验量化实测证据：
* 缺少证据条目直接抛出 `InvariantError(code="MISSING_EVIDENCE")`；
* 证据 `value` 中不含真实数字（如通过用例数、耗时、实测指标）直接抛出 `InvariantError(code="EVIDENCE_WITHOUT_NUMBER")`；
* 纯散文式的口头“已测试通过”一律由本体动作门禁原子拒绝并记入 `REJECTED` 审计账本。

### 1.3 M1 专属收单门禁②：每次 `completed` 完工的状态与目标原子检查点

`task_board` 的每一次 `completed` 完成态收单必须满足以下五项约束：

1. **必须提交非空且真正更新后的 `Board.state`**：严禁只把卡片标为 `done` 而原样沿用旧基线或传入全空白文本。
2. **必须显式判断 `goal` 是否变更并记录理由**：明确传入布尔判断 `goal_changed` 与非空 `goal_review_reason`；需要变更项目目标/规范时必须同时提交新 `board_goal`（且调用方须具备目标修订角色权限），不需要变更时保持原 `Board.goal`。
3. **机器可读持久化**：目标判断必须写入 `CardRun` 或直接关联的机器可读完工记录，不能只在会话聊天窗口里口头声明。
4. **单事务原子生效**：卡片移入完成列、`CardRun` 完结、成员释放、`Board.state` 更新、目标评估记录以及必要的 `Board.goal` 更新必须在**同一次本体动作事务**内原子提交，并归零 `unfolded_completions`；任一要素缺失、类型不符或前后矛盾即拒绝整次收单。
5. **严禁绕行进入完成列**：人工 `move_card`、`claim_card`、非 `completed` 结果路由或 `unblock_card` 一律禁止直接落入 `purpose == "completion"` 的完成列；宿主进程返回回执不等于业务卡片已验收，被本体拒绝的收单严禁在外部汇报为已完成。

> **非完成态说明**：`failed`、`blocked`、`abandoned` 不是完工，不要求伪造完成检查点，且允许纠正后重试而不产生重复认领脏状态。历史已落盘旧记录保持只读兼容。

---

## 2. M0 自举研发协同规程（`board-prod / Board / BD-CC-001`）

### 2.1 脑手分离角色路由（基于 A2A 联结主义设备）

1. **架构与评审角色（`architect` / `reviewer`）**：
   * 路由至高阶推理宿主（如 OpenCode 主控会话中的 Gemini 4 Carbon / Argon），负责领域建模、卡片规格制定、独立 `git diff` 与测试核验、以及带状态/目标评估的终审验收收单。
   * 架构师严禁亲自陷入多轮重复编码调试循环。
2. **编码与测试角色（`developer` / `tester`）**：
   * 由 `BoardRunner` + `RoleRoutedHost.argon_and_codebuddy` 路由至独立进程的高吞吐施工宿主（`CodeBuddyHost(model="deepseek-v4.1-flash")`）；
   * 施工宿主仅在指定代码工作树（Worktree）内按 TDD 执行修改与测试，返回符合 `RESULT_SCHEMA` 的结构化回执与数值证据。
3. **关闭自动换代**：
   * `RoleRoutedHost.argon_and_codebuddy` 默认关闭按任务数或 Token 阈值的自动开窗换代；`BoardRunner` 绝不后台自动创建或唤醒主控会话。

### 2.2 按需会话接管与寻址契约

* **标准三级地址**：工作空间 `board-prod` 中的聚合根 **`Board / BD-CC-001`**（日常简写口令：「接管看板 `BD-CC-001` 做 X」，目标完整口令：「接管工作空间 `board-prod` 中的 `Board / BD-CC-001`」）。
* **恢复边界**：持久化在 SQLite 中的 `Board.goal`、`Board.state`、`Card` 与 `CardRun` 可跨会话无损恢复业务上下文；但可执行 Python 模型代码与工作空间定位配置仍须由代码仓库提供，且卡片处于 `doing` 列仅代表存在未决认领或待审回执，不代表后台子进程仍然存活。

### 2.3 唯一真值源（SSoT）、防脑裂同步与单写者并发纪律

为消除仓库 Markdown 文件与 SQLite 本体数据库之间的双真值源冲突（Split-Brain），自举协同严格执行以下三条纪律：

1. **运行态卡片与检查点的唯一权威源**：
   * 卡片列位置、依赖关系、成员认领状态、`CardRun` 记录以及当前已审定生效的 `Board.goal` / `Board.state`，一律以主工作空间数据库（`.instances/board_prod.sqlite3`）的已提交事务为唯一真值源。
2. **文档修订与 `Board.goal / Board.state` 的显式对账同步**：
   * 仓库中的 **`docs/ARCHITECTURE.md`（M2 宪法）+ `docs/TASK_BOARD.md`（M1/M0 看板规范）** 共同构成 `BD-CC-001` 的目标规范源文件，**`_state.md`** 构成基线状态源文件。
   * 当架构师或用户修订上述规范文档时，必须计算文档 `sha256` 摘要并通过受控本体动作（`update_board_state` 或携带 `goal_changed=True` 的 `record_outcome`）显式同步至 `Board.goal` / `Board.state`；
   * 严禁 `fold_board_state` 或外部脚本在未核对摘要的情况下，用陈旧工作目录文件静默覆盖数据库中已审定的新状态；同样严禁在规范文档已变更时谎报 `goal_changed=False`。
3. **SQLite 单写者协调纪律（Single-Writer Coordination）**：
   * 施工子进程（CodeBuddy Worker）只允许修改隔离代码工作树（Worktree）及临时证据目录，严禁直接连接或写入主仓库的 `.instances/board_prod.sqlite3`（所有数据库单测一律使用 `tmp_path`）；
   * 对 `BD-CC-001` 的建卡、认领、解阻、评论与验收收单动作，统一由当前接管的主控架构师会话串行执行，从根源上杜绝跨进程 SQLite 写锁竞争与未经审定的自动抢跑收单。
