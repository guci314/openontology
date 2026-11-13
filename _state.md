# OpenOntology 当前状态真值表 (`_state.md`)

> **用途**：本文件是项目当前的唯一架构立场与基线快照。任何新开启的架构师会话或施工会话在开机时只需读取本文件、`docs/ARCHITECTURE.md`（M2 平台架构宪法）与 `docs/TASK_BOARD.md`（M1/M0 任务看板与自举协同规范），即可快速对齐全局上下文，无需回溯历史长会话。

---

## 最新执行检查点（2026-11-14 · Gemini 4 Carbon 接管与架构分层整理）

* **会话接管与规范分层（已完成）**：
  * 主控架构师会话已由 `google/gemini-4-carbon` 正式从已停机的上一会话接管工作空间 `board-prod` 中的 `Board / BD-CC-001`。
  * 按 OMG MOF 元层次完成规范解耦：**`docs/ARCHITECTURE.md`** 纯化为 100% M2 平台与微内核宪法（修正了治理管线时序、`simulate/submit` 等价边界、防误用写门禁定性、外围支撑模块归位与显式 `Actor` 约束）；原第 5、6 节及看板量化证据门禁剥离并补齐防脑裂 SSoT / 单写者并发纪律后，独立落盘为 **`docs/TASK_BOARD.md`**（M1 `task_board` 模型与 M0 自举规程）。两份文档在工作树与原仓库保持字节级一致，并同步更新至 `BD-CC-001` 的 `Board.goal`。
* **实施目录**：`/Users/guci/.local/share/opencode/worktree/2341b0/model-workspace`。原目录 `/Users/guci/work/open-ontology` 的业务源码未覆盖，生产服务未重启；下述工作树交付不能当作线上生产服务已升级。
* **已完成验收收单的卡片**：`C-WS-01` / `C-WS-01-R1`（无损打开、六表预检、测试库隔离及接管零播种副作用）、`C-WS-02-R1`（`ObjectType.__aggregate_root__` 类型元数据与受控根对象清单）、`C-WS-STATE-01`（逐单 `completed` 检查点与 `goal` 评估原子门禁，独立验收基线 **494 passed, 2 skipped**）。
* **后续卡片执行队列（已整理落盘至 `BD-CC-001`）**：
  1. **`C-WS-01-R2`（待架构师独立复核直接验收，当前 `blocked`）**：上一会话中断前施工队已在工作树完成两处 `Path.resolve().as_uri() + "?mode=ro"` 修复与全量快照断言强化（施工日志 502 passed, 2 skipped）；下一步直接做独立 `git diff` 审查与复跑验证后解阻收单，不重复派单重跑。
  2. **`C-WS-02`（Batch 1，`ready`）**：持久化工作空间目录（`open_ontology/workspaces.py` + `.instances/workspaces.json` + 旧 `instance` 接口同源兼容 + `not_loaded` 根状态呈现）。
  3. **`C-WS-03`（Batch 1 收官，`todo`，依赖 `C-WS-02`）**：模型投影分离（`open_ontology/model_views.py` 的 `describe_tbox` / `describe_behavior` 与 `_spec_meta` 复用、双 Runtime 同模型隔离回归）。
  4. **`C-WS-04` ～ `C-WS-06`（Batch 2，`todo` 链式依赖）**：通用接管与重名消歧（`C-WS-04`）→ 指挥台 `Model/Workspace` 词汇与双栈路由兼容（`C-WS-05`）→ 术语表 `GLOSSARY.md`、`ARCHITECTURE.md + TASK_BOARD.md` 双文档 `Board.goal` 装载集成与全量折叠验收（`C-WS-06`）。
  5. **`C-KERNEL-SEC-01`（Post-Batch 独立加固卡，`todo`，依赖 `C-WS-06`）**：修复架构评审定位的 3 项 M2 内核缺口（`post_approval` 阶段字段主权复核、`schema.sql` 审计表与注册表写门禁触发器闭环、外部入口显式 `Actor` 护栏），排在 `C-WS-06` 之后以免触碰当前批次冻结的 `schema.sql`。
* **旧种子卡保持不动**：`C-101`（`blocked`）、`C-102`（`doing`）、`C-103`（`todo`）作为历史基线保全对象保持原样，不伪造推进。
* **本次 goal 变更判断**：**`goal` 已显式更新（`goal_changed = true`）**。原因：应用户架构评审决议，按 OMG MOF 元模型将原混合在 `docs/ARCHITECTURE.md` 中的 M1 `task_board` 与 M0 研发协同章节剥离至 `docs/TASK_BOARD.md`，并在 `BD-CC-001` 的 `Board.goal` 中组合承载 M2 宪法 + M1/M0 看板规范全文。

---

## 1. 现在什么算数（Canonical Baseline）

### 1.1 M2 系统内核与切面/驱动分层（稳定冻结，新增业务本体时 0 行修改）
* **`open_ontology/spec.py` + `runtime.py`（当前工作树 1,393 行，预算 1,400） + `sandbox.py` + `query.py` + `schema.sql`**：声明式元模型原语、SQLite 微内核页表读写、内存叠加沙箱 `EditBatch`、`ObjectDiff` 计算、隔离复制 `fork()`；通用类型元数据由 ObjectType.type_metadata 声明，包含 aggregate_root。
* **`open_ontology/aspects.py`（706 行，系统调用切面层）**：从 `runtime.py` 提纯出的七大具名切面（`ColumnMaskingAspect` 列脱敏、`SecurityAndAuthorityAspect` RBAC/RLS 与字段主权、`IdempotencyAndOCCAspect` 幂等与乐观锁、`ApprovalGateAspect` 审批挂起与冲正、`RejectionAuditAspect` 拒绝审计、`AutomationReflexAspect` 提交后反射触发、`IdleAwareCallbackAspect` 忙闲感知回调投递：忙时入队暂存、闲时 FIFO 重放、异常隔离不污染业务事务）。
* **`open_ontology/ingestion.py`（537 行，数据摄入设备驱动）**：`SnapshotIngestionDriver`，负责外部快照同步与多数据源按列拼接（Column-wise MDO Stitching）。
* **`open_ontology/__init__.py`（129 行，纯微内核导出）**：仅导出 52 项微内核与通用引擎原语，0 业务本体或外设驱动导出。
* **`open_ontology/osdk.py` + `mcts.py`**：LLM 工具导出、受控 Agent 执行器与基于沙盘克隆的 MCTS 多步规划器。
* **open_ontology/workshop.py + workshop_ui.py + ui_display.json**：统一作战指挥中心（Command Center）。现有 UI 代码仍保留旧标签与 DOM 标识，这是待 C-WS-05 迁移的实现现状；架构正文已统一 Model / Runtime / Workspace / Device，不再由四标签页推出四个正交资源。已有功能包括：
  * `/`：四个一级标签页（`#pillarTabs`：`#tab-ontology` 未激活的知识、`#tab-runtime` 算力与治理宿体 [`Core + Aspect + DeviceAdapter`]、`#tab-instance` 激活的知识与虚拟现实沙盘推演、`#tab-device` 外部世界双标准设备注册表 [`symbolic_devices` OpenAPI Schema + `connectionist_devices` A2A Agent Card]），侧栏与主舞台按标签页严格正交隔离（`Ontology` 页侧栏仅展示 `#ontologiesPanel` 本体蓝图列表，不出现实例列表；`Instance` 页侧栏仅展示 `#instancesPanel` 运行实例列表，隐藏本体蓝图列表；`Runtime` 与 `Device` 页隐藏整个 `#side` 侧栏并以 `main.full` 全宽展示主舞台），并兼容多蓝图 / 多实例主控台（动态合并各蓝图 `spec.display` 中文词条、专属中文角色下拉、按角色与 `f.masked_roles` 白名单动态渲染列脱敏、按 `supports_mcts` 显隐推演按钮）；
  * `/api/runtimes` 与 `/api/devices`：暴露 9 台 `RuntimeCatalogEntry`（含 1 台 `rt-default-memory` 默认纯内存宿体与 8 台实例宿体）及双设备注册表（17 台符号主义 OpenAPI 设备 + 5 台联结主义 A2A Agent Card 设备）；
  * `/topology?ontology=<id>`：独立可缩放、可拖拽的矢量本体关系图；同一 `#scene` 坐标系内全景展示四层语义——ObjectType（`rect.node`）+ LinkType 关系环、🎯 业务动作层（`rect.action-node`，含中文名/英文名/可执行角色/动作作用的目标对象）、🧮 查询与计算函数层（`rect.fn-node`）及可选 ⚡ 自动化规则（`rect.auto-node`），全部坐标统一 `grow(...)` 进 `bbox` 以让 `fit()` 完整适配；顶栏统计同步展示对象/关系/动作/函数四项计数；
  * 指挥台只认四大通用原语（ObjectType / LinkType / Action / Function），不含任何「业务流程图」专属面板、`/flow` 路由或实例注册字段（零流程图污染）。
* **`open_ontology/execution/fabric.py`**：透明执行织入层（`ExecutionFabric`、`CallContext`、观察型 Aspect 非干涉与异常隔离）。

### 1.2 M1 业务本体蓝图与 M0 预装实例（共 6 大蓝图 / 8 个实例）
1. **`software_factory`**（`open_ontology/dev_factory.py`）→ 实例 `factory-prod`：研发成员、工单、交付物与门禁流转。
2. **`task_board`**（`open_ontology/kanban.py`）→ 实例 `board-prod`：项目管理三要素内化（`Board.goal` 架构宪法 + `Board.state` 折叠基线 + `Board.unfolded_completions` 待折叠计数 + `Card` 微分执行流）、列语义即数据（`Board` / `BoardColumn` / `Member` / `Card` / `CardComment` / `CardRun` / `Evidence`），支持 `update_board_state`、`render_board_handover` 与 `BoardRunner.make_handover_fn` 动态向新一代 Argon 注入实时本体状态，收单强制要求含数字的 `Evidence`。
3. **`coldchain_mcts`** → 实例 `coldchain-wave3`、`coldchain-wave3-lab`：冷链医药调度与 MCTS 多步推演。
4. **`crm_saas_unplug`** → 实例 `saas-parasitic`、`saas-standalone`：多云 SaaS 寄居吸纳与一键拔除（Unplug）。
5. **`steel_mdm`** → 实例 `steel-mill-north`：钢厂主数据破局与三视角对账。
6. **`osv2_governance`** → 实例 `osv2-coil-prod`：钢卷资产、`PricedAsset` 多态接口、角色列脱敏、调价审批与回滚。

### 1.3 PSM 智能体宿主与多角色协同层
* **`open_ontology/agent_hosts.py`**：
  * `CodeBuddyHost`：通过 `codebuddy -p -y --output-format json --json-schema` 直接获取符合 `RESULT_SCHEMA` 的结构化裁决；`--allowedTools` 以逗号分隔单串传递并固定追加 `--permission-mode bypassPermissions`，杜绝变长参数吞掉末尾位置参数 `briefing`；
  * `OpenCodeHost`：通过本机 OpenCode HTTP/CLI 驱动会话，支持按任务数（`rotate_every_n_tasks`）与 Token 阈值（`max_session_tokens`）自动开启新一代干净会话并注入交接简报；新增忙闲感知三件套 `is_session_idle`（`time.idle >= time.busy`）、`wait_until_idle`（`/wait` + 静默确认）与 `as_callback_aspect`（返回绑定本宿主的 `IdleAwareCallbackAspect`）；
  * `RoleRoutedHost`：按角色路由（`architect`/`reviewer` 走 Gemini 4 Argon，`developer`/`tester` 走免费/包月 `CodeBuddyHost`）；默认**不开启世代轮转**（`rotate_every_n_tasks=None`、`max_session_tokens=None`）；
  * `BoardRunner`：连接 `task_board` 本体与宿主的执行器（认领 → 宿主执行 → 数值证据门禁验收入账）；新增可选 `callback_aspect` / `notify_session_id` / `wait_for_idle_callback`，收单后在返回字典附带 `callback_status`（`DELIVERED` / `QUEUED` / `FAILED` / `None`）；**不再自动换代**（已移除 `architect_host` / `architect_session_id` / `should_rotate_architect` / `fold_and_rotate_architect` 与 `architect_rotation`），仅保留纯折叠方法 `fold_board_state(board_key)`：把已完工卡摘要与工作目录 `_state.md` / `docs/ARCHITECTURE.md` 全文写回 `Board.state` / `Board.goal` 并归零 `unfolded_completions`，绝不创建或唤醒任何 Session；保留 `sync_board_docs(board_key)` 同步文档全文。
  * **空会话按对象 ID 接管（`find_ontology_sqlite_files` / `locate_object_in_databases` / `takeover_board_from_db`）**：手动开启空 OpenCode Session 后说「接管看板 `BD-CC-001` 做 X」，递归扫描合法 SQLite 工作空间存储，按对象 ID 命中（也支持卡片 ID 反查看板），恢复 `board_overview` + `render_board_handover`；未命中时抛 LookupError。工作空间目录与多命中消歧仍待 C-WS-02/04，不把 Board 对象 ID 称作空间 ID。

> **看板三要素承载项目真值**：工作空间 `board-prod` 中看板对象 `Board / BD-CC-001` 的 goal/state 与审定后的目标/状态文档同步，卡片保留执行细节。实现工作树的已有数据库默认无损打开，只有显式 reset 才重新播种；不能把旧版“每次清库”当作现行行为。空会话从 SQLite 接管，不再依赖自动换代。


---

## 2. 线上服务与测试水位

* **常驻指挥台演示服务**：`http://127.0.0.1:8765`（单实例运行，已清理重复的 `8767` 实例）。
  * 本体关系图大图示例：`http://127.0.0.1:8765/topology?ontology=task_board`
* **全量测试命令与通过基线**：
  ```bash
  /Users/guci/miniforge3/bin/pytest -q
  ```
   当前工作树独立验收基线为 **494 passed, 2 skipped（91.66s）**，保留既有 6 模型 × 8 演示空间 Playwright、治理、沙盘与接管回归；2 条 skip 仍为真宿主冒烟。工作树通过不是原生产服务升级证明；下一步先处理 C-WS-01-R2，再继续 C-WS-02/03。

---

## 3. 架构师与施工队协同铁律（不可违背）

1. **M2 内核零业务污染**：新增或修改任何业务 Ontology 时，`git diff open_ontology/__init__.py open_ontology/ui_display.json` 必须为空，`workshop.py` 与 `workshop_ui.py` 不得出现任何业务领域私有字段。
2. **脑手分离省额度**：
   * 架构师会话（`opencode-go/gemini-4-argon`）只做顶层建模、规格编写、`git diff` 审查与终审裁决，**严禁亲自跑多轮编码/调试循环**；
   * 施工与测试一律派单给 `codebuddy/deepseek-v4.1-flash`（严禁用 `opencode-go/deepseek-*` 消耗主套餐额度）。
3. **状态外置 + 按需接管**：不做架构师自动换代。需要新开会话时，人类说「接管看板 `BD-CC-001` 做 X」，从持久工作空间按对象 ID 恢复 Board.goal / Board.state / 卡片；有歧义时需要明确 workspace_id，不猜第一个命中。
