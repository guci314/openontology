# OpenOntology 当前状态真值表 (`_state.md`)

> **用途**：本文件是项目当前的唯一架构立场与基线快照。任何新开启的架构师会话或施工会话在开机时只需读取本文件、`docs/ARCHITECTURE.md`（M2 平台架构宪法）与 `docs/TASK_BOARD.md`（M1/M0 任务看板与自举协同规范），即可快速对齐全局上下文，无需回溯历史长会话。

---

## 最新执行检查点（2026-11-15 · C-102 编排器认领超时回收收官）

* **卡片与范围**：`Board / BD-CC-001` 上的 `C-102`（编排器认领超时回收），补齐执行账本 `CardRun` 的失联收口能力——此前一张卡被认领后若执行者进程消失，卡片会永久停在 `doing`、成员永久 `BUSY`，账本里的 `running` 记录无人收口。
* **M1 本体新增能力（`open_ontology/kanban.py`，2,178 行）**：
  1. **声明式时限**：`Board.claim_timeout_seconds`（建板时写入，`0` = 不启用回收）+ `CreateBoardParams.claim_timeout_seconds`，负值抛 `INVALID_CLAIM_TIMEOUT`。
  2. **存活上报**：`CardRun.heartbeat_at` 与 `heartbeat_claim(card, member, now?)`；非承接人或非本次尝试的成员上报抛 `CARD_NOT_CLAIMED_BY_MEMBER` / `RUN_OWNED_BY_OTHER_MEMBER`。
  3. **只读预览**：`stale_claims(board, now?, timeout_seconds?)` 返回失联清单（`liveness_at` / `idle_seconds`），未配置时限时返回 `configured=false` 且不写入。
  4. **受控回收**：`reclaim_stale_claims(board, now?, timeout_seconds?, skip_run_ids?)` 单事务内把尝试置为 `status="reclaimed"` / `outcome="abandoned"`、写 `ended_at` 与回收摘要，释放成员与卡片，`consecutive_failures += 1` 后按 `on_failed` 回可认领列、预算耗尽按 `on_exhausted` 升级；命中完成列整次拒绝（`COMPLETION_REQUIRES_RECORD_OUTCOME`），卡片已改派时只收口账本，未配置时限时拒绝回收（`CLAIM_TIMEOUT_NOT_CONFIGURED`）。
* **编排器侧配套（`open_ontology/agent_hosts.py`，1,943 行）**：`BoardRunner.claim_timeout_seconds` / `heartbeat_interval_seconds`；`run_card` 与 `_execute_running_run` 在宿主执行期间按间隔续报心跳并在 `finally` 停止；`reclaim_stale_claims(board_key)` 先只读预览、再用 `skip_run_ids` 排除本进程 `_active_run_ids` 中的 run；`dispatch_running_cards(reclaim_stale=None)` 缺省跟随看板策略，回收后重新查询在途 run 再派发，上一进程遗留的死认领不会被当成活任务交给宿主。
* **验收基线**：`test_kanban.py` **99 passed**、`test_agent_hosts.py` **84 passed, 2 skipped**；全仓 `pytest -q` **`620 passed, 2 skipped, 3 failed`**（109.37s）。3 条失败为 `tests/test_command_center_presentation.py` 中与 UI 状态页签重构相关的**既有失败**（`.tabs` / `.tab.on` 选择器与新 `#stateTabs` 冲突），已用「隐藏本卡新增动作/函数后复跑仍失败」的对照实验确认与本卡无关；`runtime.py` 未改动，仍为 **1,373 行**（预算 1,400）。
* **本次 goal 变更判断**：**`goal` 未变（`goal_changed = false`）**。本卡只补齐 M1 执行账本的失联回收能力，`docs/ARCHITECTURE.md`（M2 宪法）未被修改；`docs/TASK_BOARD.md` 增补的 §1.4 与 §2.2 属于已审定 M1/M0 契约的细化说明，不改变项目目标与架构红线，故 `Board.goal` 按现行 `ARCHITECTURE + TASK_BOARD` 组合装载口径保持同步即可。

---

## 上一执行检查点（2026-11-15 · M2 OWL ObjectProperty + AutoID 元模型纯化收官）

* **架构纯化成果**：彻底去除旧版独立连边类型原语及跨对象外键字段命名（无后兼容保留），将 M2 结构元模型收敛为 OWL 原语（`ObjectType` + `DatatypeProperty` + `ObjectProperty` + `AutoID`）：
  1. **`ObjectProperty` 自动三元组同步与参照完整性**：支持 `ObjectProperty[Target]` / `ObjectProperty[Target] | None` / `ObjectProperty[tuple[Target, ...]]` 与独立 `spec.object_property(...)`，在 `EditBatch` 增删改与 `put_object` 时自动维护 `ontology_links` 三元组，并在 `simulate_action` / `submit_action` 两路径强制校验 `InvariantError(..., code="TARGET_NOT_FOUND")`。
  2. **`AutoID` 与界面表单自动推导**：`describe_tbox(spec)` / `describe_behavior(spec)` 导出 `auto_generated: True` 与 `property_kind: "object"`、`range`、`multiple`，指挥台据此自动渲染系统生成只读编号与单选/多选关联下拉列表。
  3. **全仓领域模型与文档同步**：`task_board`（`Card.card_id: AutoID` 及 `BoardColumn.board`、`Member.active_card`、`Card.board`、`Card.assignee`、`Card.waits_for`、`CardComment.card`、`CardRun.card`、`CardRun.member` 8 项对象属性）、`software_factory`、`examples/*.py`、M2/M1 规范及术语表全面切换至语义化对象属性。
* **验收基线**：`open_ontology/runtime.py` 精简至 **1,353 行**（严格满足 ≤ 1,400 行微内核预算）；全量回归测试 **`603 passed, 2 skipped`**（2 条 skip 为 `OPENONTOLOGY_LIVE_HOST=1` 真宿主冒烟）；线上 `http://127.0.0.1:8765/` 指挥台经 Playwright 验证通过。

---

## 更早执行检查点（2026-11-14 · Model/Workspace 全系与 C-KERNEL-SEC-01 收官合入）

* **实施与发布状态**：`C-WS-01` ～ `C-WS-06`（含 `R1/R2/STATE-01`）共 10 张卡片及微内核门禁补强卡 `C-KERNEL-SEC-01` 已全部完成架构师独立终审验收、合入主仓库 `/Users/guci/work/open-ontology`（`main` 分支）并推送至 GitHub `origin/main`。
* **`C-KERNEL-SEC-01` 闭合的三项 M2 微内核护栏**：
  1. **`post_approval` 两段式增量主权复核**：`_run_handler` 先对 `handler` 阶段 diff 做 `_check_authority`，钩子执行后再由 `SecurityAndAuthorityAspect.check_post_approval_authority` 仅针对钩子增量（对称集合差覆盖新增/修改/删除/撤销的对象与连边）做单平面复核；增量自身混写双平面即抛 `InvariantError(code='MIXED_AUTHORITY')`，同时保留 `steel_mdm` 与 `saas_unplug` 的合法两段式单平面流转。
  2. **SQLite 15/15 写门禁触发器闭环**：`schema.sql` 为 `ontology_action_runs` 补齐 `BEFORE INSERT / UPDATE` 门禁触发器（保留无条件禁止 `DELETE`），为 `ontology_registry` 补齐 `BEFORE INSERT / UPDATE / DELETE` 门禁触发器；内核 `_sync_registry` 与切面挂起/拒绝落账统一走 `_write_gate()`。
  3. **外部边界 RLS 与只读函数列脱敏闭环**：`workshop.py` `/api/links` 过滤两端不可见或悬空连边，`/api/snapshot` 显式透传 `Actor`；`runtime.call_function` 为只读 `EditBatch` 注入 `mask_fn` 使 `ctx.get / query / query_linked` 按角色白名单脱敏（动作批处理保持明文不污染暂存 diff）；`http_server.py` 支持注入显式 `Actor`。
* **终审验收基线**：独立全仓回归 **`584 passed, 2 skipped in 103.58s`**（2 条 skip 为需显式开启 `OPENONTOLOGY_LIVE_HOST=1` 的真宿主冒烟）。`runtime.py` **1,388 行**（预算 ≤ 1,400），`aspects.py` 零反向依赖，`__init__.py` 与 `ui_display.json` 零改动。
* **本次 goal 变更判断**：**`goal` 已显式更新（`goal_changed = true`）**。原因：`docs/ARCHITECTURE.md` 第 3.2 节已从“目标补强方向”更新为已落地的五表 15/15 门禁覆盖、`post_approval` 增量复核与只读函数脱敏红线，按防脑裂纪律同步写入 `Board.goal` 并折叠 `Board.state`。

---

## 更早执行检查点（2026-11-14 · C-WS-06 集成验收与 Batch 2 收官）

* **会话接管与规范分层（已完成）**：
  * 主控架构师会话已由 `google/gemini-4-carbon` 正式从已停机的上一会话接管工作空间 `board-prod` 中的 `Board / BD-CC-001`。
  * 按 OMG MOF 元层次完成规范解耦：**`docs/ARCHITECTURE.md`** 纯化为 100% M2 平台与微内核宪法；M1 `task_board` 模型与 M0 自举规程独立落盘为 **`docs/TASK_BOARD.md`**。两份文档在工作树与原仓库保持字节级一致。
* **实施目录**：`/Users/guci/.local/share/opencode/worktree/2341b0/model-workspace`。原目录 `/Users/guci/work/open-ontology` 的业务源码未覆盖，生产服务未重启；下述工作树交付不能当作线上生产服务已升级。
* **已完成验收收单的卡片（Batch 1 + Batch 2 全部完成）**：
  * `C-WS-01` / `C-WS-01-R1` / `C-WS-01-R2`：无损打开、六表预检、RFC 3986 只读 URI 转义防旁路建库、全字段快照保全、测试库隔离及接管零播种副作用；
  * `C-WS-02-R1`：`ObjectType.__aggregate_root__` 类型元数据与受控根对象清单；
  * `C-WS-STATE-01`：逐单 `completed` 检查点与 `goal` 评估原子门禁；
  * `C-WS-02`：`open_ontology/workspaces.py` 工作空间身份清单 `workspaces.json`、可信模型解析、`register_workspace/get_workspace` 与旧 `instance` 同源别名、`list_workspaces` 的 `not_loaded` 清单呈现及双内存 Runtime 冲突检测与未注册模型原因标注（`test_workspaces.py` 35 passed）；
  * `C-WS-03`：`open_ontology/model_views.py` 只读结构投影 `describe_tbox(spec)`（含 `aggregate_root` 与严格 plain-JSON 约束/默认值归一化）与行为投影 `describe_behavior(spec)`，`workshop._spec_meta` 复用单一声明源，双 Runtime 与 `fork()` 同模型事实隔离回归（`test_model_views.py` 5 passed）；
  * `C-WS-04`：通用模型无关 `takeover_workspace(workspace_id)`（零 `kanban` 耦合、六表只读预检、显式 `Actor` 根摘要）与看板领域适配 `takeover_board` / `takeover_board_from_db`（支持 `workspace_id + Board/Card`，多库同名对象抛 `AmbiguousTakeoverError` 并附清单 `workspace_id`，架构复审补强无关残缺库扫描跳过）；
  * `C-WS-05`：指挥台一级导航升级为 `Model · 领域模型` / `Workspace · 工作空间`（保留既有 DOM ID，不新增 TBox/ABox 一级页），模型详情分区展示 TBox 结构与 Behavior 受控行为、工作空间状态标 ABox；HTTP 全面双栈兼容（`?workspace=`、`/topology?model=`、`models/workspaces` 目录键、`/api/model/<id>`、`/api/workspaces`、`/api/workspace/{fork,mcts,unplug}`），新旧或重复参数冲突一律返回 HTTP 400（定向含浏览器回归 **127 passed in 56.20s**）；
  * `C-WS-06`：新增 `GLOSSARY.md` 统一术语与身份边界；`README.md` / `_state.md` / `_execution.md` 同步实际实现状态；`BoardRunner.sync_board_docs` / `fold_board_state` 与演示种子在同时存在 `docs/ARCHITECTURE.md` + `docs/TASK_BOARD.md` 时按 `ARCHITECTURE + '\n\n---\n\n' + TASK_BOARD` 组合装载 `Board.goal`，单文件目录逐字节保持旧行为；新增文档一致性门禁 `tests/test_docs_consistency.py`。
* **后续卡片执行队列**：
  1. **`C-KERNEL-SEC-01`（Post-Batch 独立加固卡，施工已完成、待架构师终审）**：3 项 M2 内核缺口已在工作树闭合（`post_approval` 增量主权复核、审计表/注册表写门禁触发器闭环、外部入口显式 `Actor` 与只读函数脱敏）。
* **旧种子卡保持不动**：`C-101`（`blocked`）、`C-102`（`doing`）、`C-103`（`todo`）作为历史基线保全对象保持原样，不伪造推进。
* **本次 goal 变更判断**：**`goal` 已显式更新（`goal_changed = true`）**。原因：C-WS-06 将 `Board.goal` 装载口径收敛为 `docs/ARCHITECTURE.md（M2 宪法） + docs/TASK_BOARD.md（M1/M0 看板规范）` 组合全文，并在 M2 宪法中落定已完成的指挥台四视角与外围支撑模块描述；`Board.state` 已随终审验收同步折叠。

---

## 1. 现在什么算数（Canonical Baseline）

### 1.1 M2 系统内核与切面/驱动分层（稳定冻结，新增业务本体时 0 行修改）
* **`open_ontology/spec.py` + `runtime.py`（当前工作树 1,353 行，预算 1,400） + `sandbox.py` + `query.py` + `schema.sql`**：声明式 OWL 元模型原语（`ObjectType` / `ObjectProperty` / `AutoID`）、SQLite 微内核页表读写、内存叠加沙箱 `EditBatch`、`ObjectDiff` 计算、隔离复制 `fork()`；通用类型元数据由 ObjectType.type_metadata 声明，包含 aggregate_root。
* **`open_ontology/aspects.py`（789 行，系统调用切面层）**：从 `runtime.py` 提纯出的七大具名切面（`ColumnMaskingAspect` 列脱敏、`SecurityAndAuthorityAspect` RBAC/RLS 与字段主权、`IdempotencyAndOCCAspect` 幂等与乐观锁、`ApprovalGateAspect` 审批挂起与冲正、`RejectionAuditAspect` 拒绝审计、`AutomationReflexAspect` 提交后反射触发、`IdleAwareCallbackAspect` 忙闲感知回调投递：忙时入队暂存、闲时 FIFO 重放、异常隔离不污染业务事务）。
* **`open_ontology/ingestion.py`（537 行，数据摄入设备驱动）**：`SnapshotIngestionDriver`，负责外部快照同步与多数据源按列拼接（Column-wise MDO Stitching）。
* **`open_ontology/__init__.py`（129 行，纯微内核导出）**：仅导出 52 项微内核与通用引擎原语，0 业务本体或外设驱动导出。
* **`open_ontology/osdk.py` + `mcts.py`**：LLM 工具导出、受控 Agent 执行器与基于沙盘克隆的 MCTS 多步规划器。
* **`open_ontology/workspaces.py`**：工作空间身份层——`WorkspaceDescriptor`（`workspace_id` / `model_id` / `runtime_id` / `db_path`）、带显式格式版本的 `workspaces.json` 清单、可信模型解析、`register_workspace` / `get_workspace` 与旧 `instance` 同源别名、只读 `list_workspaces(actor)` 聚合根清单，以及模型无关 `takeover_workspace`。不新增业务表，不按 JSON 任意导入代码。
* **`open_ontology/model_views.py`**：模型只读投影层——`describe_tbox(spec)`（结构类型/字段/接口/关系/约束/`aggregate_root`）与 `describe_behavior(spec)`（动作/函数/自动化契约），纯 JSON、零数据库访问；`workshop._spec_meta` 复用同一份声明源。
* **open_ontology/workshop.py + workshop_ui.py + ui_display.json**：统一作战指挥中心（Command Center）。一级导航已迁移为 `Model · 领域模型` / `World · 世界状态`（曾用 `Workspace · 工作空间`、`World · 业务世界`） / `Runtime · 算力与治理宿体` / `Device · 外部世界(符号+联结)`，DOM ID `#tab-ontology` / `#tab-instance` 保留兼容；四视角是同一份资源的观察方式，不由标签页推出四套割裂账本。已有功能包括：
  * `/`：四个一级标签页（`#pillarTabs`），侧栏与主舞台按标签页隔离（`Model` 页侧栏仅展示 `#ontologiesPanel` 模型列表；`Workspace` 页侧栏仅展示 `#instancesPanel` 工作空间列表；`Runtime` 与 `Device` 页隐藏 `#side` 并以 `main.full` 全宽展示），模型详情区分 TBox 结构与 Behavior 行为，工作空间事实标注 ABox；并兼容多模型 / 多工作空间主控台（动态合并各模型 `spec.display` 中文词条、专属中文角色下拉、按角色与 `f.masked_roles` 白名单动态渲染列脱敏、按 `supports_mcts` 显隐推演按钮）；
  * HTTP 双栈：`?workspace=` 与 `?instance=`、`/topology?model=` 与 `?ontology=`、`models` / `workspaces` 目录键与 `ontologies` / `instances` 并存；新旧参数冲突返回 HTTP 400，不静默择一；
  * `/api/runtimes` 与 `/api/devices`：暴露 9 台 `RuntimeCatalogEntry`（含 1 台 `rt-default-memory` 默认纯内存宿体与 8 台实例宿体）及双设备注册表（17 台符号主义 OpenAPI 设备 + 5 台联结主义 A2A Agent Card 设备）；
  * `/topology?ontology=<id>`：独立可缩放、可拖拽的矢量本体关系图；同一 `#scene` 坐标系内全景展示四层语义——ObjectType（`rect.node`）+ `ObjectProperty` 对象属性关系环、🎯 业务动作层（`rect.action-node`，含中文名/英文名/可执行角色/动作作用的目标对象）、🧮 查询与计算函数层（`rect.fn-node`）及可选 ⚡ 自动化规则（`rect.auto-node`），全部坐标统一 `grow(...)` 进 `bbox` 以让 `fit()` 完整适配；顶栏统计同步展示对象/对象属性/动作/函数四项计数；
  * 指挥台只认四大通用原语（ObjectType / ObjectProperty / Action / Function），不含任何「业务流程图」专属面板、`/flow` 路由或实例注册字段（零流程图污染）。
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
  * `BoardRunner`：连接 `task_board` 本体与宿主的执行器（认领 → 宿主执行 → 数值证据门禁验收入账）；新增可选 `callback_aspect` / `notify_session_id` / `wait_for_idle_callback`，收单后在返回字典附带 `callback_status`（`DELIVERED` / `QUEUED` / `FAILED` / `None`）；**不再自动换代**（已移除 `architect_host` / `architect_session_id` / `should_rotate_architect` / `fold_and_rotate_architect` 与 `architect_rotation`），仅保留纯折叠方法 `fold_board_state(board_key)`：把已完工卡摘要与工作目录 `_state.md` / 目标规范全文写回 `Board.state` / `Board.goal` 并归零 `unfolded_completions`，绝不创建或唤醒任何 Session；保留 `sync_board_docs(board_key)` 同步文档全文。目标规范默认装载 `docs/ARCHITECTURE.md`，同目录存在 `docs/TASK_BOARD.md` 时按 `ARCHITECTURE + '\n\n---\n\n' + TASK_BOARD` 组合，单文件目录逐字节保持旧行为；**认领失联回收**：`claim_timeout_seconds` / `heartbeat_interval_seconds` 让宿主执行期间自动续报心跳，`reclaim_stale_claims(board_key)` 只回收失联认领并用 `skip_run_ids` 排除本进程 `_active_run_ids` 中的在途 run，`dispatch_running_cards` 先回收再重新查询派发。
  * **显式工作空间寻址（`takeover_workspace` / `takeover_board` / `takeover_board_from_db`）**：通用入口按 `workspace_id` 经可信目录与模型注册表解析，零 `kanban` 耦合；看板适配层支持 `workspace_id + Board/Card` 目标，多库同名对象或多看板命中时抛 `AmbiguousTakeoverError` 并附候选，绝不盲取第一个；未命中时抛 `LookupError` 且不建目录 / 不建库。不把 Board 对象 ID 称作空间 ID。

> **看板三要素承载项目真值**：工作空间 `board-prod` 中看板对象 `Board / BD-CC-001` 的 goal/state 与审定后的目标/状态文档同步，卡片保留执行细节。实现工作树的已有数据库默认无损打开，只有显式 reset 才重新播种；不能把旧版“每次清库”当作现行行为。空会话从 SQLite 接管，不再依赖自动换代。


---

## 2. 线上服务与测试水位

* **常驻指挥台演示服务**：`http://127.0.0.1:8765`（单实例运行，已清理重复的 `8767` 实例）。
  * 本体关系图大图示例：`http://127.0.0.1:8765/topology?ontology=task_board`
* **全量测试命令与通过基线**：
  ```bash
  /Users/guci/miniforge3/bin/python -m pytest -q
  ```
   当前全仓独立验收基线为 **620 passed, 2 skipped, 3 failed**（3 条失败为 `tests/test_command_center_presentation.py` 中 `#stateTabs` 引入后 `.tabs` / `.tab.on` 选择器冲突的既有 UI 失败，与 M1 执行账本改动无关），保留既有 6 模型 × 8 演示空间 Playwright、治理、沙盘与接管回归，并含 `tests/test_docs_consistency.py` 文档一致性门禁、`C-KERNEL-SEC-01` 微内核护栏行为测试、`ObjectProperty` + `AutoID` 自动三元组同步/参照完整性测试及 `C-102` 认领失联回收（`stale_claims` / `reclaim_stale_claims` / `heartbeat_claim`）测试；2 条 skip 仍为需显式开启 `OPENONTOLOGY_LIVE_HOST=1` 的真宿主冒烟。Model / Workspace 迁移全系 10 张卡片、`C-KERNEL-SEC-01` 微内核门禁补强卡、M2 OWL `ObjectProperty` + `AutoID` 重构与 `C-102` 执行账本回收已全部完成验收并合入当前工作基线。

---

## 3. 架构师与施工队协同铁律（不可违背）

1. **M2 内核零业务污染**：新增或修改任何业务 Ontology 时，`git diff open_ontology/__init__.py open_ontology/ui_display.json` 必须为空，`workshop.py` 与 `workshop_ui.py` 不得出现任何业务领域私有字段。
2. **脑手分离省额度**：
   * 架构师会话（`opencode-go/gemini-4-argon`）只做顶层建模、规格编写、`git diff` 审查与终审裁决，**严禁亲自跑多轮编码/调试循环**；
   * 施工与测试一律派单给 `codebuddy/deepseek-v4.1-flash`（严禁用 `opencode-go/deepseek-*` 消耗主套餐额度）。
3. **状态外置 + 按需接管**：不做架构师自动换代。需要新开会话时，人类说「接管看板 `BD-CC-001` 做 X」，从持久工作空间按对象 ID 恢复 Board.goal / Board.state / 卡片；有歧义时需要明确 workspace_id，不猜第一个命中。

---

## 4. 已知技术债（AIP Agent 驱动 UI · v1/v2，2026-11-14 引入，暂缓处理）

> 涉及文件：`open_ontology/osdk.py`（`UIArtifact` / `run_agent_turn` / `iter_agent_turn`）、`open_ontology/workshop.py`（`/api/agent/chat`、`/api/agent/ghost`、Agent 抽屉前端）、`tests/test_osdk_artifacts.py`。以下问题均**未修复**，当前实现仅可用于演示与确定性单测。

| # | 问题 | 现状 | 后续方向 |
|---|------|------|----------|
| TD-AGENT-01 | **SSE 伪流式** | `iter_agent_turn` 先同步跑完整轮 `run_agent_turn`，再把 `reply_text` 按 `chunk_size` 切片推送 `token` 事件；首字延迟 = 整轮耗时 | OpenAI 路径改 `stream: true` 真 token 流；工具调用中途实时推送 `tool_call` / `tool_result` |
| TD-AGENT-02 | **真实 LLM 路径工具契约断裂**（✅ 已于 2026-11-15 修复动作/函数直调路由；待补真实模型端到端压测） | `AIPAgentExecutor.dispatch` 已支持将 `export_llm_tools(spec)` 导出的函数名自动路由至 `call_function`、动作名安全路由至 `simulate_action`（只做沙盘预演，不直通写库） | 后续按需补充 mock HTTP 的 OpenAI/DeepSeek 工具循环单测 |
| TD-AGENT-03 | **本地兜底是关键词匹配** | 无 `OPENAI_API_KEY` 时 `_deterministic_local_turn` 按「统计/审批/表单/聚焦」等关键词分支，不具备推理能力 | 明确定位为演示/测试桩；或接入 `agent_hosts.py` 现有宿主 |
| TD-AGENT-04 | **Ghost 事件重复计入** | 前端 `pushGhostEvent` 既上报 `/api/agent/ghost` 存服务端，又在 chat 请求体携带 `ghost_events`，服务端 `stored + req` 拼接导致同一事件计两次；服务端缓存为进程内 dict，重启即丢 | 确定单一事实源（推荐服务端），去掉请求体重复携带或按事件 ID 去重 |
| TD-AGENT-05 | **前端内联 onclick 注入隐患** | `diff_preview` 的「确认提交」把参数 JSON 拼进内联 `onclick` 字符串，参数值含单引号即出错并存在注入面 | 改为 `data-*` 属性 + 事件委托，参数存于 JS 侧 Map |
| TD-AGENT-06 | **Agent 写权限边界未定** | 当前只有用户点击卡片才会 `submit`；`AIPAgentExecutor` 仍保留 `submit_action` 工具，若真实 LLM 路径修通后可能绕过「先模拟」 | 明确策略：Agent 是否可直接提交、是否强制 simulate 前置与审批门禁 |
| TD-AGENT-07 | **验证缺口** | v2 的 `test_v2_workshop_ghost_and_sse_streaming` 从未实际跑通（沙箱禁回环 socket）；改动后未跑全量 `pytest -q`，未确认第 2 节 584 passed 基线是否保持；Agent 抽屉前端 JS 未在浏览器中实测 | 本机跑全量回归 + Playwright 覆盖抽屉/卡片闭环 |
| TD-AGENT-08 | **`workshop.py` 体量膨胀** | Agent 抽屉 CSS/HTML/JS 直接写入 `_WORKSHOP_HTML`，文件已近 3000 行 | 拆入 `workshop_ui.py` 或独立 `agent_ui.py` 资源模块 |
