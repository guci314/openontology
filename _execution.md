# OpenOntology 执行台账 (`_execution.md`)

> **用途**：记录人类已拍板但尚未全部跑完的工单队列（Backlog & In-Flight Tickets）。主控 Argon 会话自动换代时，新会话通过读取本文件得知「当前跑到哪一单、验收通过后下一单该派什么」，绝不依赖聊天记录或施工队口头传话。

---

## 当前战役：微内核提纯（将 OpenOntology 巨石内核拆解为 微内核 + 设备驱动 + 系统调用切面）

### 1. `TK-ONTO-KERNEL-SLIM-1` —— 提取数据摄入驱动与清理微内核顶层导出
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，366 passed, 2 skipped）
* **已完成结果**：
  1. 新建设备驱动模块 `open_ontology/ingestion.py`（537 行，`SnapshotIngestionDriver`），`open_ontology/runtime.py` 由 1982 行降至 1700 行（净减 282 行）；
  2. `open_ontology/__init__.py` 彻底移除 `dev_factory`（6 个符号）与 `harness_drivers`（10 个符号）导出，`__all__` 降至 52 项纯微内核原语；
  3. 新增 4 条内核纯洁性红线测试，全仓 366 passed。

---

### 2. `TK-ONTO-KERNEL-SLIM-2` —— 从 `runtime.py` 提取五大系统调用切面（Syscall Aspects）+ 修复 OpenCode `Model.Ref` 换代载荷
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，405 passed, 2 skipped）
* **已完成结果**：
  1. 新建 `open_ontology/aspects.py`（591 行），完整提取六大系统调用切面（`ColumnMaskingAspect`、`SecurityAndAuthorityAspect`、`IdempotencyAndOCCAspect`、`ApprovalGateAspect`、`RejectionAuditAspect`、`AutomationReflexAspect`）；
  2. `open_ontology/runtime.py` 从 1,700 行进一步瘦身至 **1,400 行**（两单累计从 1,982 行净减 **582 行**）；
  3. 修复 `workflow_conductor.py` 与 `open_ontology/agent_hosts.py` 的 `to_model_ref`（将 `"provider/id"` 转为 `{"providerID": ..., "id": ...}`），新增 `tests/test_kernel_aspects.py`（28 条测试）与 `Model.Ref` 协议测试（11 条），全仓测试达 **405 passed, 2 skipped**。

---

## 当前战役：指挥台演示级 UI 全面体检与修复（面向企业信息化专家演示）

### 3. `TK-ONTO-UI-DEMO-AUDIT` —— 修复蓝图实例串台、角色列表串台与未汉化、脱敏展示反转、MCTS 按钮误露，并补齐 6 蓝图 × 8 实例全量 Playwright 界面测试
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，412 passed, 2 skipped）
* **已完成结果**：
  1. **实例列表跨蓝图串台**：`open_ontology/workshop.py` 中 `#filterToggle` 默认未勾选，导致点击某 Ontology 时 `#instances` 混入全部 8 个实例。修复：`#filterToggle` 默认 `checked`，且 `selectOntology(id)` 时强制 `$('filterToggle').checked = true`，使实例列表默认严格只展示当前选中 Ontology 的实例。
  2. **角色下拉列表跨蓝图串台 + 部分角色未汉化**：
     * 后端 `workshop.py` 新增 `_ontology_roles(spec, instances)`，从实例 `default_actor`、`spec.actions`（`allowed_roles` + `approver_roles`）、`spec.object_types`（`__masked_fields__`）自动提取每个 Ontology 的专属角色列表，并在 `_spec_meta` / `_catalog` / `_ontology_detail` / `_meta` 中返回 `roles`；
     * 前端新增 `syncRoleOptions(roles, preferredRole)`，在 `selectOntology` 与 `selectInstance` 时将 `#role` 下拉选项动态收敛为**仅当前 Ontology 的专属角色**（初始静态 HTML `<select id="role">` 保持原样以兼容静态断言）；
     * 在 `open_ontology/kanban.py`、`examples/mcts_world_model_demo.py`、`examples/steel_mdm_demo.py` 的 `spec.display["roles"]` 中补齐 `board_manager`、`developer`、`reviewer`、`tester`、`coordinator`、`ops_manager`、`warehouse`、`mes`、`production` 的中文名（**严禁修改 `open_ontology/ui_display.json` 与 `open_ontology/__init__.py`**）。
  3. **列级脱敏语义颠倒与数值脱敏列未显示「不可见」**：
     * 蓝图视图 `renderOntologyView` 中将 `🔒 不可见：` 修正为 `🔒 仅可见：`（因为 `__masked_fields__` 声明的是允许明文查看的白名单角色）；
     * 实例视图 `renderObjects` 与 `pickInterface` 中，根据 `f.masked && role() !== 'system' && !(f.masked_roles || []).includes(role())` 判定当前角色是否被脱敏，使数值型（`0.0`）和可空型（`null`）敏感列在无权限角色下同样渲染为 `<b class="masked">不可见</b>`；同时修复 `pickInterface` 的 Tab 高亮与字段汉化。
  4. **操作按钮防呆与状态清理**：
     * 仅在 `i.supports_mcts` 为真时渲染 `🌳 多步智能推演` 按钮；
     * `runAutomations()` 在 `!S.inst` 时提示 `请先选择一个运行实例` 并拦截，防止误触发默认实例；
     * `fillParams()` 切换动作时重置 `#result` 区域，避免残留上次执行结果。
  5. **全量 Playwright 界面测试 + 重启 `8765` 演示服务**：
     * 在 `tests/test_command_center_presentation.py` 中遍历全部 6 个蓝图与 8 个实例，断言实例列表隔离、角色列表隔离与中文显示、列脱敏角色切换动态生效、MCTS/Unplug 按钮按能力显隐、控制台零报错；
     * 重启 `http://127.0.0.1:8765` 常驻演示进程并验证线上实测生效。

---

## 当前战役：切面增强与全面切换至「方案 B · 看板流 (`/onto-board`)」

### 4. `TK-ONTO-IDLE-CALLBACK-ASPECT` —— 实现「忙闲感知防吞回调切面 (`IdleAwareCallbackAspect`)」与 `OpenCodeHost` Wait-Until-Idle 投递，并用看板流驱动执行
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，431 passed, 2 skipped）
* **已完成结果**：
  1. **`open_ontology/aspects.py` 新增 `IdleAwareCallbackAspect`**（模块由 591 行增至 706 行，`__all__` 同步加入该类）：
     * 注入 `is_idle_fn` / `send_fn` / 可选 `wait_idle_fn`，维护 `pending_queue` / `delivered` / `failed_deliveries` 三本账；
     * `deliver()` 返回 `DELIVERED` / `QUEUED` / `FAILED` / `SKIPPED`：空闲时先按 FIFO 补投该目标积压再投当前消息；忙时深拷贝入队、绝不打断或丢包；`send_fn` 异常一律捕获进 `failed_deliveries`，绝不外抛污染已提交的业务事务；
     * `flush(target_id=None)` 按 FIFO 重放已空闲目标，成功的移入 `delivered`、异常的移入 `failed_deliveries`、仍忙的原样保留，返回本轮成功条数。
  2. **`open_ontology/agent_hosts.py` 集成忙闲感知与看板回调**（863 → 976 行）：
     * 修复 `CodeBuddyHost.run` 的 CLI 参数吞噬 Bug：`--allowedTools` 改为逗号分隔单串，并在 `briefing` 前固定追加 `--permission-mode bypassPermissions`，位置参数不再被变长参数吞掉；
     * `OpenCodeHost` 新增 `is_session_idle`（`time.idle >= time.busy`，缺字段按空闲）、`wait_until_idle`（`/wait` + `quiet_seconds` 二次确认 + 超时返回 `False`）、`as_callback_aspect`（默认摘要格式或自定义 `formatter` 转文本后走 `_prompt`）；
     * `BoardRunner` 新增 `callback_aspect` / `notify_session_id` / `wait_for_idle_callback`，收单后回投摘要并在返回字典附带 `callback_status`（未配置时为 `None`，既有断言零影响）。
  3. **`workflow_conductor.py` 缓存阈值修正**：`ROTATION_DEFAULTS["max_conductor_cache_read"]` 由 `150000` 提升至 `2000000`，并同步更新 `tests/test_conductor_rotation.py` 的默认值断言。
  4. **测试门禁**：`tests/test_kernel_aspects.py` 新增 9 条、`tests/test_agent_hosts.py` 新增 10 条（合计 19 条）；指定三文件实测 **107 passed, 2 skipped**，全仓 `/Users/guci/miniforge3/bin/pytest -q` 实测 **431 passed, 2 skipped**（85.24s）。`open_ontology/__init__.py` 与 `open_ontology/ui_display.json` 本单 0 行修改。
* **实现范围与验收要求**：
  1. **`open_ontology/aspects.py` 新增 `IdleAwareCallbackAspect`（忙闲感知回调投递切面）**：
     * 支持注入 `is_idle_fn: Callable[[str], bool]`、`send_fn: Callable[[str, dict[str, Any]], Any]` 及可选 `wait_idle_fn: Callable[[str, float, float], bool] | None`；
     * `deliver(target_id, payload, *, wait=False, quiet_seconds=1.0, timeout=30.0)`：
       * 若 `wait=True` 且提供了 `wait_idle_fn`，先等待目标会话进入 `idle`；
       * 若目标当前处于 `idle`（`is_idle_fn(target_id) is True`），立即调用 `send_fn(target_id, payload)` 并记入 `delivered` 账本，返回 `"DELIVERED"`；
       * 若目标处于 `busy`，**绝不强行打断或丢弃**，而是将投递项追加进 `pending_queue` 暂存队列，返回 `"QUEUED"`；
       * 若 `send_fn` 抛出任何异常，捕获并记入 `failed_deliveries`，返回 `"FAILED"`，**绝不向外抛出异常污染已提交的本体业务状态**（切面非干涉铁律）。
     * `flush(target_id=None) -> int`：扫描 `pending_queue`，凡当前已变为 `idle` 的目标按 FIFO 顺序重放投递，返回本轮成功补投的条数。
     * 将 `IdleAwareCallbackAspect` 加入 `open_ontology/aspects.py` 的 `__all__`（注意：`open_ontology/__init__.py` 与 `open_ontology/ui_display.json` 保持 0 行修改）。
  2. **`open_ontology/agent_hosts.py` 集成 Wait-Until-Idle 与看板回调切面**：
     * `OpenCodeHost` 新增 `is_session_idle(session_id: str) -> bool`（检查 `/api/session/{id}` 的 `time.idle >= time.busy`）与 `wait_until_idle(session_id: str, quiet_seconds: float = 0.0, timeout: float = 120.0) -> bool`（调用 `/api/experimental/session/{id}/wait` 并确认 `is_session_idle` 且持续安静 `quiet_seconds`），以及 `as_callback_aspect(...) -> IdleAwareCallbackAspect` 工厂方法；
     * `BoardRunner` 新增可选字段 `callback_aspect: Any = None` 与 `notify_session_id: str = ""`：在 `run_card` 完成本体 `record_outcome`（或被护栏拒绝）后，若配置了 `callback_aspect` 与 `notify_session_id`，自动调用 `callback_aspect.deliver(notify_session_id, summary_dict)`，并在返回字典中附带 `"callback_status"`（`"DELIVERED"` / `"QUEUED"` / `"FAILED"` / `None`）。
  3. **修复 `~/.claude/skills/opencode-session-conductor/scripts/workflow_conductor.py` 缓存累计误触发过早换代**：
     * 将 `ROTATION_DEFAULTS["max_conductor_cache_read"]` 由 `150000` 提升至 `2000000`（避免单轮多次工具调用的 `cache.read` 累加导致仅 1 单就提前换窗，使回调落入新窗而旧窗无感知），并同步更新 `tests/test_conductor_rotation.py` 中使用默认 `max_conductor_cache_read` 的测试断言（如有）。
  4. **测试门禁**：
     * 在 `tests/test_kernel_aspects.py` 与 `tests/test_agent_hosts.py` 新增至少 8 条单元测试覆盖：`IdleAwareCallbackAspect` 空闲即投、忙时入队、`flush` 恢复重放、异常隔离不回滚业务状态、`OpenCodeHost.is_session_idle` / `wait_until_idle`、`BoardRunner` 挂载 `callback_aspect` 全链路；
     * 全仓 `/Users/guci/miniforge3/bin/pytest -q` 全绿通过。

---

### 5. `TK-ONTO-TOPOLOGY-FULL-METAMODEL` —— 升级本体关系图（`/topology` 与主界面预览），同图全景展示 Object + Link + Function + Action
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，432 passed, 2 skipped）
* **已完成结果**：
  1. `open_ontology/workshop_ui.py` 的 `TOPOLOGY_JS`（`topologyScene`）升级：空蓝图仍返回 `{inner: '', bbox: {0,0,1,1}}`；`ObjectType` 保持 `class="node"`，新增 🎯 业务动作层（`rect.action-node`，3 列网格卡片：中文名/英文名/可执行角色摘要/`作用于 <目标对象>`，目标对象由动作 `params.properties` 键命中某 `ObjectType.primary_key` 推断）与 🧮 查询与计算函数层（`rect.fn-node`，2 列网格）及可选 ⚡ 自动化（`rect.auto-node`）；两个面板并排位于关系环下方，所有坐标统一 `grow(...)` 进 `bbox`，`fit()` 与 `topologySvg(m)` 自动完整包裹；
  2. `TOPOLOGY_PAGE_HTML` 的 `#subtitle` 升级为 `对象类型 · 关系 · 业务动作 · 查询函数` 四项统计；
  3. `tests/test_command_center_presentation.py` 新增真浏览器测试 `test_browser_topology_shows_objects_links_actions_and_functions`：`task_board` 断言 `rect.node`=6、`rect.action-node`=9（含「认领任务卡片」「登记执行结果」「创建任务卡片」）、`rect.fn-node`=3（含「看板总览」「看板流程图」「生成任务简报」）、`#subtitle` 四项计数、`fitBtn` 后整图落在画布内且控制台零报错；
  4. 架构红线：`workshop.py` / `workshop_ui.py` 无任何 `BOARD_LEAKS` 词条，`open_ontology/__init__.py` 与 `open_ontology/ui_display.json` 本单 0 行修改；
  5. 门禁实测：指定文件 **34 passed**，全仓 **432 passed, 2 skipped**（90.76s）；`8765` 演示服务已重启，线上浏览器实测 `nodes 6 / actions 9 / fns 3`，`#subtitle` 为「智能体交付 · 6 个对象类型 · 4 条关系 · 9 个业务动作 · 3 个查询函数」。
* **问题背景**：
  * `http://127.0.0.1:8765/topology?ontology=task_board`（以及所有蓝图的拓扑图 `TOPOLOGY_JS`）目前只画了静态数据层的 `ObjectType` 和 `LinkType`，看不到动态语义层的 `Function`（只读查询计算）和 `Action`（受控业务动作）。
* **实现范围与验收要求**：
  1. **升级 `open_ontology/workshop_ui.py` 中的 `TOPOLOGY_JS`（`topologyScene`）**：
     * 保持 `ObjectType` 节点使用 `<rect class="node" ...>`（严禁给非 ObjectType 节点加 `class="node"`，确保既有 `#scene rect.node` 计数断言 100% 兼容）；当 `!(m.object_types || []).length` 时仍返回空场景 `{inner: '', bbox: ...}` 以兼容空蓝图测试。
     * 在对象关系环下方（同一个 `#scene` 坐标系内，统一通过 `grow(...)` 纳入 `bbox` 包围盒，使 `fit()` 自动适配全景），新增两个清晰美观看板区/泳道（带分区标题与边框背景容器）：
       * **🎯 业务动作区（GOVERNED ACTIONS）**：遍历 `m.actions || []`，每个动作绘制 `<rect class="action-node" ...>`（琥珀/青绿科技配色的圆角卡片），展示中文动作名 `zhAction(a.name)`、英文名 `a.name`、可执行角色 `(a.allowed_roles||[]).map(zhRole).join(' / ')`（若有 `approver_roles` 则标出审批角色）；若动作的 `a.params.properties` 命中了某个 `ObjectType` 的 `primary_key`，可绘制一条从动作指向该对象的柔和虚线或标注目标对象中文名，直观体现「动作受控改写对象」。
       * **🧮 查询与计算函数区（SEMANTIC FUNCTIONS，及可选 ⚡ 自动化规则 `m.automations`）**：遍历 `m.functions || []`，每个函数绘制 `<rect class="fn-node" ...>`（紫罗兰 `#a78bfa` 配色的圆角卡片），展示中文函数名 `zh('functions', f.name, f.name)`、英文名 `f.name` 与简短描述。
  2. **升级 `TOPOLOGY_PAGE_HTML` 顶栏 `#subtitle` 统计文案**：
     * 在 `/topology` 页面加载成功后，`#subtitle` 同时展示对象类型数、关系数、业务动作数、查询函数数（例如：`智能体交付 · 6 个对象类型 · 4 条关系 · 9 个业务动作 · 3 个查询函数`）。
  3. **严守架构红线**：
     * `open_ontology/workshop.py` 与 `open_ontology/workshop_ui.py` 严禁出现任何 `BOARD_LEAKS` 词汇；`open_ontology/__init__.py` 与 `open_ontology/ui_display.json` 保持 0 行修改。
  4. **Playwright 真浏览器测试与 8765 服务热更新**：
     * 在 `tests/test_command_center_presentation.py` 中新增测试验证 `/topology?ontology=task_board`（及主界面拓扑预览）在真实浏览器中同时渲染出 `rect.node`（6 个对象类型）、`rect.action-node`（9 个业务动作，含「认领任务卡片」「登记执行结果」等中文文本）、`rect.fn-node`（3 个查询函数，含「看板总览」「看板流程图」「生成任务简报」等中文文本），且 `fitBtn` 自适应窗口后全部落在画布可视区域内；
     * 实跑 `/Users/guci/miniforge3/bin/pytest -q tests/test_command_center_presentation.py` 及全仓测试全绿；
     * 重启本机 `8765` 端口演示服务并确认 `http://127.0.0.1:8765/topology?ontology=task_board` 实时返回新版全景拓扑图。

---

### 6. `TK-ONTO-BOARD-GOAL-STATE` —— 将项目管理三要素（`goal` 架构宪法 + `state` 折叠基线 + `Card` 执行流）内化进 `task_board` 本体与 Argon 世代交接
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，443 passed, 2 skipped）
* **已完成结果**：
  1. **`open_ontology/kanban.py` 扩展聚合根 `Board`、动作与函数**：
     * `Board` 新增 `goal: str = ""`（项目目标与架构红线）、`state: str = ""`（当前基线状态：“现在什么算数”）、`unfolded_completions: int = 0`（待折叠完工数）；
     * 新增受控动作 `update_board_state`（支持折叠更新 `state`、可选修订 `goal`，并将 `unfolded_completions` 归零；空 `state` 抛 `EMPTY_BOARD_STATE`）；
     * `record_outcome` 在 `outcome == "completed"` 时，若传入 `board_state` 则同步刷新 `Board.state` 并归零 `unfolded_completions`，否则 `unfolded_completions += 1`；
     * `board_overview`、`card_briefing`、`render_card_briefing` 自动携带 `goal` / `state` / `unfolded_completions`，并新增 `render_board_handover` 生成新一代架构师首轮交接包。
  2. **`open_ontology/agent_hosts.py` 打通新一代 Argon 动态本体交接**：
     * `OpenCodeHost` 与 `RoleRoutedHost.argon_and_codebuddy` 新增 `handover_fn: Callable[[], str] | None = None`，每次开启新世代会话时实时从本体拉取最新 `Board.goal + Board.state + 在途卡片`；
     * `BoardRunner` 新增 `make_handover_fn(board_key)` 与 `run_card(..., board_state=...)`。
  3. **演示服务与全量测试**：`board-prod` 实例已预置真实 `goal` 与 `state`，全仓测试升至 **443 passed, 2 skipped**，`8765` 服务已热更新。

---

### 7. `TK-ONTO-BOARD-AUTO-ROTATE` —— 在 `BoardRunner` 中焊死「折叠 `Board.state` → 创建新一代 Argon 主窗 → 本体 `render_board_handover` 供血 → 返回新窗口提醒」四步自动换代闭环
* **状态**：🟢 `DONE`（终审 `VERDICT: PASS`，446 passed, 2 skipped）
* **已完成结果**：
  1. **`open_ontology/agent_hosts.py`（1,140 行）`BoardRunner` 新增 8 个全默认值字段**（`architect_host` / `architect_session_id` / `architect_generation` / `architect_title_prefix` / `rotate_every_n_completions=0` / `max_architect_tokens=50_000` / `wait_new_architect` / `architect_session_history`），默认 `rotate_every_n_completions=0` 且无 `architect_session_id` 时完全不触发，既有调用方 0 影响；
  2. **新增 `_resolve_architect_host()`**：优先显式 `architect_host`，其次 `self.host` 本身是 `OpenCodeHost`，再次 `RoleRoutedHost.host_for("architect")` 的 `OpenCodeHost`，否则 `None`；
  3. **新增 `should_rotate_architect(board_key)`**：按 `Board.unfolded_completions >= rotate_every_n_completions` 或 `arch_host._get_session_tokens(architect_session_id) > max_architect_tokens` 判定并给出中文原因；
  4. **新增 `fold_and_rotate_architect(board_key, reason)` 四步闭环**：① `board_overview` 取 `done` 卡摘要，剥离旧「最新折叠完工：」段后重写 `folded_state`（最多保留 5 条），经 `update_board_state` 动作写回并把 `unfolded_completions` 归零；② `_create_session(f"{prefix}-G{next_gen})")` + `_move` 挂载 `_workspace(board_key)`，更新 `architect_session_id` / `architect_generation` / `architect_session_history`；③ `make_handover_fn` 现场抽取折叠后的 `Board.goal + Board.state + 卡片分布`，组装含换代原因与上一代 ID 的首轮唤醒词经 `_prompt` 注入（`wait_new_architect` 可选阻塞）；④ 返回 `rotated` / `old_session_id` / `new_session_id` / `new_title` / `generation` / `reason` / `folded_state` / `notice`；
  5. **`run_card` 末尾挂载自动换代**：`record_outcome` 与回调投递之后调用 `should_rotate_architect`，命中则 `fold_and_rotate_architect` 并写入 `summary_dict["architect_rotation"]`（未触发为 `None`）；
  6. **测试门禁**：`tests/test_agent_hosts.py` 新增 3 条（`test_board_runner_auto_folds_and_rotates_architect_after_n_completions`、`test_board_runner_rotates_architect_when_session_tokens_exceed_limit`、`test_board_runner_resolves_architect_host_from_role_routed_host`）；`pytest -q tests/test_agent_hosts.py tests/test_kanban.py` 实测 **98 passed, 2 skipped**，全仓 `/Users/guci/miniforge3/bin/pytest -q` 实测 **446 passed, 2 skipped**（90.60s）；本单未改动 `open_ontology/__init__.py` 与 `open_ontology/ui_display.json`（`ui_display.json` 0 diff）。
* **实现范围与验收要求**：
  1. **`open_ontology/agent_hosts.py` 扩展 `BoardRunner` 四步自动换代触发器**：
     * `BoardRunner` 新增可选配置字段：
       * `architect_host: OpenCodeHost | None = None`（用于查询主控窗口 Token 与创建新一代 Argon 主控会话；若未显式传入且 `self.host` 是 `RoleRoutedHost`，可从 `self.host.host_for("architect")` 自动取用 `OpenCodeHost`）
       * `architect_session_id: str = ""`（当前正在与人类对话的主控架构师会话 ID）
       * `architect_generation: int = 1`（当前主控架构师代数，换代时自增为 `generation + 1`）
       * `architect_title_prefix: str = "🏛️ 架构师(Argon"`（新窗口标题格式：`f"{self.architect_title_prefix}-G{next_gen})"`）
       * `rotate_every_n_completions: int = 2`（当 `Board.unfolded_completions >= rotate_every_n_completions` 时触发换代；设为 `0` 表示禁用按完工数换代）
       * `max_architect_tokens: int = 50_000`（当 `architect_session_id` 的累计 token `> max_architect_tokens` 时触发换代；设为 `0` 表示禁用按 token 换代）
       * `architect_session_history: list[str] = field(default_factory=list)`（记录历代主控会话 ID）
     * 新增方法 `should_rotate_architect(self, board_key: str) -> tuple[bool, str]`：
       * 若 `self._resolve_architect_host() is None` 或未启用任何换代条件（无 `architect_session_id` 且 `rotate_every_n_completions <= 0`），返回 `(False, "")`；
       * 读取本体中 `Board(board_key)` 的 `unfolded_completions`：若 `self.rotate_every_n_completions > 0 and board.unfolded_completions >= self.rotate_every_n_completions`，返回 `(True, f"看板待折叠完工数已达 {board.unfolded_completions}/{self.rotate_every_n_completions}")`；
       * 若 `self.architect_session_id and self.max_architect_tokens > 0`，通过 `arch_host._get_session_tokens(self.architect_session_id)` 检查 token：若 `tokens > self.max_architect_tokens`，返回 `(True, f"当前架构师会话 token {tokens} 超过上限 {self.max_architect_tokens}")`；
       * 否则返回 `(False, "")`。
     * 新增方法 `fold_and_rotate_architect(self, board_key: str, reason: str = "") -> dict[str, Any]`（执行闭环四步走）：
       * **第 1 步（自动折叠 `Board.state`）**：读取该看板下已处于终态列（`done`）的卡片，将最新完工卡片的 `card_id`、`title`、`result` 折叠合入 `Board.state`（若原 `board.state` 非空，保留原基线核心并追加/更新最新完工摘要，然后调用本体动作 `update_board_state` 将更新后的 `state` 写回 `Board` 并把 `unfolded_completions` 归零！）；
       * **第 2 步（创建新一代 Argon 主控窗口）**：`next_gen = self.architect_generation + 1`，标题 `new_title = f"{self.architect_title_prefix}-G{next_gen})"`，调用 `arch_host._create_session(new_title)` 创建 `new_sid`，并调用 `arch_host._move(new_sid, self._workspace(board_key))` 挂载工作目录；更新 `old_sid = self.architect_session_id`、`self.architect_session_id = new_sid`、`self.architect_generation = next_gen`，并将 `new_sid` 记入 `self.architect_session_history`；
       * **第 3 步（本体实时供血）**：调用 `handover_text = self.make_handover_fn(board_key)()` 从本体实时抽取 `Board.goal + Board.state + 卡片分布与摘要`，组装首轮唤醒词（含换代原因、上一代 `old_sid`、本体交接全文，并要求新会话确认就位待命），调用 `arch_host._prompt(new_sid, prompt_text)` 发送给新窗口（注意：不调 `_wait` 阻塞，或可配置 `wait_new_architect: bool = False`，让当前主窗口能立即拿到返回结果并向人类发出提醒！）；
       * **第 4 步（返回提醒元数据）**：返回 `{"rotated": True, "old_session_id": old_sid, "new_session_id": new_sid, "new_title": new_title, "generation": next_gen, "reason": reason, "notice": f"🔔 架构师已换代：请在左侧会话列表打开新窗口【{new_title}】({new_sid}) 继续指挥！"}`。
     * 在 `BoardRunner.run_card` 末尾（`record_outcome` 与 `callback_aspect` 之后）：
       * 自动调用 `need, reason = self.should_rotate_architect(board_key)`；若 `need` 为 True，自动调用 `rot = self.fold_and_rotate_architect(board_key, reason)` 并将 `"architect_rotation": rot` 放入 `summary_dict`（未触发时 `"architect_rotation": None`）。
  2. **测试门禁**：
     * 在 `tests/test_agent_hosts.py` 中新增单元测试，验证：
       (a) 连续跑完 2 张卡后，`Board.unfolded_completions` 达到 2，`BoardRunner.run_card` 自动触发第 1 步 `update_board_state`（`Board.unfolded_completions` 归零、`Board.state` 包含新完工卡摘要）、第 2 步创建 `🏛️ 架构师(Argon-G2)` 新会话、第 3 步将本体 `Board.goal` 与折叠后的 `Board.state` 通过 `_prompt` 注入新会话、第 4 步在返回字典 `architect_rotation` 中给出 `new_session_id`、`new_title` 与 `notice` 提醒文案；
       (b) 当单张卡后主控会话 Token 超过 `max_architect_tokens`（如 `60,000 > 50,000`）时，即使只完工 1 张卡也立即触发四步折叠换代；
     * 全仓 `/Users/guci/miniforge3/bin/pytest -q` 100% 通过。
* **实现范围与验收要求**：
  1. **`open_ontology/kanban.py` 扩展聚合根 `Board`、动作与函数**（⚠️ 严禁出现 `tests/test_kanban.py` 中的 `FORBIDDEN_SUBSTRINGS` 与 `FORBIDDEN_WORDS` 如 `opencode`/`codebuddy`/`session_id`/`http`/`port`/`cli`/`api`）：
     * `Board` 新增字段：`goal: str = ""`（项目目标与架构红线）、`state: str = ""`（当前基线状态：“现在什么算数”）、`unfolded_completions: int = 0`（自上次更新 `state` 以来新完成的卡片数）；
     * `CreateBoardParams` 与 `create_board` 支持可选传入 `goal: str = ""`、`state: str = ""`；
     * 新增受控动作 `update_board_state`（`UpdateBoardStateParams(board_key: str, state: str, goal: str | None = None)`，`allowed_roles=("board_manager", "admin", "system", "reviewer", "coordinator")`）：校验 `state.strip()` 非空（否则抛 `InvariantError(..., code="EMPTY_BOARD_STATE")`），更新 `Board.state`（及可选 `Board.goal`）并将 `Board.unfolded_completions` 重置为 `0`；
     * `RecordOutcomeParams` 新增可选 `board_state: str | None = None`：当 `outcome == "completed"` 时，若传入了非空 `board_state` 则同步更新 `Board.state = params.board_state.strip()` 且 `unfolded_completions = 0`；若未传则 `Board.unfolded_completions += 1`；
     * `board_overview`（`_overview_of`）返回字典新增 `"goal"`、`"state"`、`"unfolded_completions"` 与 `"cards_summary"`（各卡片 `card_id`、`title`、`column_key`、`assignee_id`、`result` 列表）；
     * `card_briefing` 返回字典新增 `"board_goal": board.goal`、`"board_state": board.state`、`"unfolded_completions": board.unfolded_completions`，`render_card_briefing` 在存在 `board_goal` / `board_state` 时将其渲染在简报头部；新增并导出 `render_board_handover(overview: dict[str, Any]) -> str`（将 `board_overview` 渲染为新一代架构师交接简报文本）；
     * `spec.display["fields"]` 补齐 `"goal"`、`"state"`、`"unfolded_completions"`、`"board_state"`、`"board_goal"` 中文词条，`spec.display["actions"]` 补齐 `"update_board_state": "更新看板基线状态"`。
  2. **`open_ontology/agent_hosts.py` 支持动态从 `Board` 拉取新一代 Argon 交接包**：
     * `OpenCodeHost` 新增可选 `handover_fn: Callable[[], str] | None = None`：开启新世代会话时，若配置了 `handover_fn` 则实时调用 `handover_fn()` 获取最新交接文本（为空或未配置时回退静态 `handover_briefing`）；
     * `RoleRoutedHost.argon_and_codebuddy` 新增可选 `handover_fn: Callable[[], str] | None = None` 参数透传给 `OpenCodeHost`；
     * `BoardRunner.run_card` 新增可选 `board_state: str | None = None` 参数，若提供则在 `record_outcome` 时写入 `params["board_state"]`；且新增便捷方法 `runner.make_handover_fn(board_key: str) -> Callable[[], str]`（调用 `runtime.call_function("board_overview", {"board_key": board_key})` + `render_board_handover`）。
  3. **演示数据与测试同步更新**：
     * `examples/command_center_demo.py` 的 `_build_task_board` 在 `create_board` 时填入真实的 `goal` 与 `state`；
     * `tests/test_command_center_presentation.py` 中 `task_board` 动作数由 `9` 更新为 `10`（`"10 个业务动作"`）；
     * 在 `tests/test_kanban.py` 与 `tests/test_agent_hosts.py` 中新增单元测试覆盖 `Board.goal`/`Board.state`/`unfolded_completions`、`update_board_state`、`record_outcome` 顺带折叠或累加 `unfolded_completions`、`card_briefing` 携带 `board_goal`/`board_state`、以及 `OpenCodeHost` 通过 `handover_fn` 在换代时实时拉取最新 `Board.state`；
     * 实跑全量 `/Users/guci/miniforge3/bin/pytest -q` 全绿并重启 `8765` 演示服务。

---

### 8. `TK-ONTO-PURGE-M2-FLOW` —— 清除 M2 指挥台中的 M1「业务流程图 (`/flow` / `flow_function`)」跨层污染
* **状态**：🟢 `DONE`（看板卡 `C-PURGE-M2-FLOW-1`，438 passed, 2 skipped）
* **问题根因**：
  * M2 元模型仅包含四大通用原语（`ObjectType` / `LinkType` / `Action` / `Function`），并非所有本体都有“业务流程”；且 `task_board` 的 `BoardColumn` 只是单张卡片的生命周期状态机，并非业务流程图。
  * 当前 M2 指挥台（`open_ontology/workshop.py` 与 `open_ontology/workshop_ui.py`）为了给 `board-prod` 渲染 `board_flow`，塞入了 `flow_function` 注册字段、实例主界面 `🧭 业务流程图 (PROCESS FLOW)` 专属面板、`loadFlow()`、`/flow` 独立路由、`FLOW_JS` 与 `FLOW_PAGE_HTML`，属于典型的 **M1 反向污染 M2**。
* **实现范围与验收要求**：
  1. **`open_ontology/workshop.py` 彻底剥离流程面板与 `/flow` 路由**：
     * 从 `from .workshop_ui import (...)` 移除 `FLOW_JS` 与 `FLOW_PAGE_HTML`；
     * 从 `OntologyInstanceEntry` 与 `CommandCenterRegistry.register_instance` 移除 `flow_function: str = ""` 字段与入参；
     * 从 `_meta(...)` 返回的 `"instance"` 字典移除 `"flow_function"` 键；
     * 从 `_WORKSHOP_HTML` 移除 `__FLOW_JS__`、`loadFlow()` 调用与函数定义、以及 `renderInstanceView` 中的 `🧭 业务流程图<small>PROCESS FLOW</small>` 面板渲染；
     * 从 `_render_ui_page` 移除 `.replace("__FLOW_JS__", FLOW_JS)`；
     * 从 `_WorkshopHandler.do_GET` 移除 `if path == "/flow":` 路由分支（访问 `/flow` 返回 404）。
  2. **`open_ontology/workshop_ui.py` 彻底移除流程图模板与渲染脚本**：
     * 从 `__all__` 移除 `"FLOW_JS"` 与 `"FLOW_PAGE_HTML"`；
     * 删除 `FLOW_JS` 与 `FLOW_PAGE_HTML` 定义，清理模块 docstring 中关于 `FLOW_JS` 的描述。
  3. **`examples/command_center_demo.py` 移除 `flow_function` 挂载**：
     * 在 `_build_task_board` 的 `registry.register_instance("board-prod", ...)` 中删除 `flow_function="board_flow",` 参数（注：`open_ontology/kanban.py` 内的 M1 函数 `board_flow` 保留为普通只读 `@spec.function`，由 `test_kanban.py` 与查询计算面板正常消费，不污染 M2）。
  4. **`tests/test_command_center_presentation.py` 替换为 M2 零流程图污染护栏测试**：
     * 删除旧的 `/flow`、`FLOW_JS`、`flow_function` 测试（`test_ui_kernel_flow_protocol_is_domain_agnostic`、`test_flow_page_served_and_refreshable`、`test_flow_page_does_not_echo_query`、`test_flow_page_meta_exposes_flow_function`、`test_instance_without_flow_function_has_no_panel`、`test_main_page_flow_preview_link_attributes`、`test_flow_scene_provides_bbox_and_scale`、`test_browser_flow_page_zoom_drag_reset`、`test_browser_main_panel_opens_flow_in_new_tab`、`test_browser_flow_page_error_states`）；
     * 新增防回退测试（静态源码护栏 + HTTP 404 护栏 + Playwright 真浏览器护栏）：
       - `test_m2_workshop_has_no_process_flow_pollution(base)`：断言 `open_ontology/workshop.py` 与 `open_ontology/workshop_ui.py` 源码中不含 `"flow_function"`、`"FLOW_JS"`、`"FLOW_PAGE_HTML"`、`"业务流程图"`、`"PROCESS FLOW"`、`"/flow"`；断言 `/api/meta?instance=board-prod` 的 `instance` 字典不含 `"flow_function"`；断言请求 `base + "/flow?instance=board-prod"` 返回 HTTP 404；
       - `test_browser_task_board_instance_has_no_flow_panel(live, browser)`：在真实浏览器中进入 `多智能体任务看板` → `任务看板 · 生产实例`，断言页面不含 `#flow` 且不含 `"业务流程图"` 面板，而 `当前业务状态`、`业务动作与沙盘推演台`、`语义关系与 🧮 查询计算`、`动作执行记录` 四大通用 M2 面板完整渲染且 0 控制台报错。
  5. **文档更新、全仓测试与 8765 演示服务重启**：
     * 更新 `docs/ARCHITECTURE.md` 与 `_state.md`（移除 `/flow` 与 `flow_function` 描述）；
     * 实跑 `/Users/guci/miniforge3/bin/pytest -q` 全量测试 100% 通过，`git diff open_ontology/__init__.py open_ontology/ui_display.json` 为空；
     * 重启本机 `8765` 端口演示服务并验证生效。

---

### 9. `TK-ONTO-BOARD-FULL-DOCS` —— 将完整 `docs/ARCHITECTURE.md` 与 `_state.md` 全文写入看板 `Board.goal` 与 `Board.state`
* **状态**：🟢 `DONE`（看板卡 `C-BOARD-FULL-DOCS-1`，415 passed, 27 skipped）
* **背景与目标**：
  * 此前指挥台 `board-prod`（`BD-CC-001`）与看板的 `Board.goal`、`Board.state` 仅写入了一两句简短摘要，导致在指挥台查看看板或向新一代架构师做本体供血时缺少完整的架构宪法与基线真值表。
  * 需要将 `docs/ARCHITECTURE.md` 全文写入 `Board.goal`，将 `_state.md` 全文写入 `Board.state`，并让 `BoardRunner` 支持直接从工作目录同步这两份文档全文。
* **实现范围与验收要求**：
  1. **`examples/command_center_demo.py`（`_build_task_board`）装入完整文档**：
     * 在 `_build_task_board` 中读取仓库根目录下的 `docs/ARCHITECTURE.md` 与 `_state.md` 全文（使用 `Path(__file__).resolve().parent.parent` 定位），并在 `create_board("BD-CC-001", ...)` 时将 `docs/ARCHITECTURE.md` 全文传入 `"goal"`、将 `_state.md` 全文传入 `"state"`；
     * 在 `_build_task_board` 的 `C-100` 调 `record_outcome` 完成时，或紧随其后，保持 `BD-CC-001` 的 `goal` 为完整 `ARCHITECTURE.md`、`state` 为完整 `_state.md`。
  2. **`open_ontology/agent_hosts.py`（`BoardRunner`）支持自动同步工作目录 `docs/ARCHITECTURE.md` 与 `_state.md` 全文**：
     * 在 `BoardRunner` 中新增方法 `sync_board_docs(self, board_key: str, *, goal_relpath: str = "docs/ARCHITECTURE.md", state_relpath: str = "_state.md") -> dict[str, str]`：
       - 从 `workspace = Path(self._workspace(board_key))` 读取 `goal_relpath`（若存在且非空）与 `state_relpath`（若存在且非空）；
       - 调用 `self.runtime.submit_action(f"sync-docs-{board_key}-{uuid.uuid4().hex[:8]}", "update_board_state", {"board_key": board_key, "state": state_text, "goal": goal_text}, self._actor())` 将完整文档写入 `Board.goal` 与 `Board.state` 并返回 `{"goal": goal_text, "state": state_text}`；
     * 在 `fold_and_rotate_architect(self, board_key: str, reason: str = "")` 的第 1 步中：
       - 检查 `workspace = Path(self._workspace(board_key))` 下是否存在 `_state.md` 与 `docs/ARCHITECTURE.md`：若存在且非空，则用工作目录最新 `_state.md` 全文作为 `prev_state` 基线，并在调用 `update_board_state` 时同时传入 `"goal": arch_doc_text`（若存在且非空），确保每次折叠换代后本体中的 `Board.goal` 与 `Board.state` 始终保存完整的最新 `docs/ARCHITECTURE.md` 与 `_state.md`（外加末尾的 `最新折叠完工：...`）。
  3. **测试与 8765 演示服务热重启**：
     * 在 `tests/test_command_center_presentation.py` 中新增测试 `test_task_board_instance_carries_full_architecture_and_state_docs(base)`：请求 `/api/objects/Board?instance=board-prod`，断言 `BD-CC-001` 的 `goal` 含有 `"# OpenOntology 架构设计规范"` 且长度 `> 1000`，`state` 含有 `"# OpenOntology 当前状态真值表"` 且长度 `> 1000`；
     * 在 `tests/test_agent_hosts.py` 中新增单元测试 `test_board_runner_syncs_full_architecture_and_state_docs(tmp_path)`，验证 `sync_board_docs` 及 `fold_and_rotate_architect` 能把工作目录下的 `docs/ARCHITECTURE.md` 与 `_state.md` 全文写入本体 `Board.goal` 与 `Board.state`；
     * 更新 `_state.md`（移除已退役 `opencode-session-conductor` 的条目，补充 `Board.goal` 与 `Board.state` 完整承载 `docs/ARCHITECTURE.md` 与 `_state.md`），并将 `_execution.md` 本单标为 `DONE`；
     * 实跑 `/Users/guci/miniforge3/bin/pytest -q` 全量测试 100% 通过，`git diff open_ontology/__init__.py open_ontology/ui_display.json` 为空；
### 10. `TK-ONTO-TAKEOVER-BY-DB` —— 去掉架构师自动换代逻辑，支持手动开启空 Session 按对象 ID（如 `BD-CC-001`）从 SQLite 数据库自动定位本体实例、恢复上下文并接管执行
* **状态**：🟢 `DONE`（看板卡 `C-TAKEOVER-1`，终审 `VERDICT: PASS`，414 passed, 2 skipped）
* **已完成结果**：
  1. **`open_ontology/agent_hosts.py` 去掉架构师自动换代**：`BoardRunner` 删除 8 个换代字段（`architect_host` / `architect_session_id` / `architect_generation` / `architect_title_prefix` / `rotate_every_n_completions` / `max_architect_tokens` / `wait_new_architect` / `architect_session_history`）与 `_resolve_architect_host` / `should_rotate_architect` / `fold_and_rotate_architect` 三个方法；`run_card` 末尾不再触发开新窗、不再返回 `architect_rotation`；新增纯折叠 `fold_board_state(board_key) -> str`（只写 `update_board_state`，绝不创建或唤醒 Session）；`RoleRoutedHost.argon_and_codebuddy` 默认 `rotate_every_n_tasks=None`、`max_session_tokens=None`。
  2. **新增 SQLite 实例接管三件套并加入 `__all__`**：`find_ontology_sqlite_files`（递归扫描 `.sqlite3` / `.db` / `.sqlite`，只读校验 `ontology_objects` 表）、`locate_object_in_databases`（按 `object_id` 精确命中，回退 `properties_json` 值匹配，返回 `db_path` / `object_type` / `object_id` / `title` / `properties` / `registered_types`）、`takeover_board_from_db`（Board 直取 `board_key`，Card 等反查 `properties.board_key`，还原 `OntologyRuntime` 并产出 `board_overview` + `render_board_handover` 上下文，未命中抛 `LookupError`，必要时自动初始化默认演示库）。
  3. **`examples/command_center_demo.py` 实例真实落盘**：新增 `_DEFAULT_DATA_DIR = _REPO_ROOT / ".instances"`、`_reset_sqlite_file`（清理 `-wal` / `-shm`）、`_build_task_board(registry, db_path=":memory:")` 与 `build_default_command_center(data_dir=None)`；默认无参调用把 `board-prod` 落到 `.instances/board_prod.sqlite3`，重复调用始终是干净种子数据（幂等，无 `BOARD_ALREADY_EXISTS`），`db_uri="sqlite://board_prod.sqlite3"` 保持不变；`.gitignore` 增加 `.instances/`。
  4. **测试清理与新增**：删除 `tests/test_conductor_rotation.py`（27 条 skip 一并消失）；`tests/test_agent_hosts.py` 删除 3 条自动换代测试、工厂测试断言默认不轮转、文档同步测试改调 `fold_board_state`，新增第 16 节 2 条数据库接管测试（按 board_key 定位 + 还原 runtime + 接管后继续执行并落盘校验；按卡片 ID 反查 + 不存在 ID 抛 `LookupError`）。
  5. **文档与服务**：`docs/ARCHITECTURE.md` 第 5 节改为「状态驻留 SQLite 本体库，按需空会话接管」；`_state.md` 同步；全仓 `/Users/guci/miniforge3/bin/pytest -q` 实测 **414 passed, 2 skipped**（79.50s）；`git diff open_ontology/__init__.py open_ontology/ui_display.json` 本单 0 行新增修改；`8765` 服务已重启，`.instances/board_prod.sqlite3` 已生成且 `takeover_board_from_db("BD-CC-001")` 直接命中。
* **背景与架构决策**：
  * 用户明确指示：**去掉架构师自动换代逻辑**。工作流改为：用户手动打开一个空的 OpenCode Session，对架构师说「接管 openontology 实例 `BD-CC-001` 做 X」，架构师直接扫描 SQLite 数据库查找 ontology 数据，搜索 `BD-CC-001` 定位到所属数据库与实例（如 `board_prod.sqlite3` 中的 `Board("BD-CC-001")`），抽出 `Board.goal`、`Board.state` 与卡片状态构成完整上下文，并立即推进任务 X。
* **实现范围与验收要求**：
  1. **`open_ontology/agent_hosts.py` 去掉架构师自动换代逻辑，新增 SQLite 实例接管发现器**：
     * **移除 `BoardRunner` 上的架构师自动换代属性与方法**：删除 `architect_host`、`architect_session_id`、`architect_generation`、`architect_title_prefix`、`rotate_every_n_completions`、`max_architect_tokens`、`wait_new_architect`、`architect_session_history` 字段，删除 `_resolve_architect_host`、`should_rotate_architect`、`fold_and_rotate_architect` 方法，以及 `run_card` 末尾调用 `should_rotate_architect` / `fold_and_rotate_architect` 和返回 `"architect_rotation"` 的逻辑；
     * **保留 `fold_board_state(self, board_key: str) -> str` 纯折叠辅助方法**：仅将已完工卡片摘要与工作目录 `_state.md` / `docs/ARCHITECTURE.md` 折叠写回本体 `Board.state` / `Board.goal` 并归零 `unfolded_completions`，绝不创建或唤醒任何新 Session；
     * **`RoleRoutedHost.argon_and_codebuddy` 默认禁用世代轮转**：将默认参数 `rotate_every_n_tasks: int | None = None`、`max_session_tokens: int | None = None`（不再默认设为 `2` 和 `50_000`）；
     * **新增数据库搜索与实例接管函数（写入 `open_ontology/agent_hosts.py` 并加入 `__all__`）**：
       - `find_ontology_sqlite_files(search_root: str | Path) -> list[Path]`：在给定目录（如仓库根目录或 `.instances/`）下扫描所有 SQLite 数据库文件（`.sqlite3` / `.db` / `.sqlite`），过滤出包含 `ontology_objects` 表的合法 OpenOntology 数据库；
       - `locate_object_in_databases(target_id: str, search_root: str | Path = ".") -> list[dict[str, Any]]`：在所有 OpenOntology SQLite 库中搜索 `object_id == target_id`（或 `properties_json` 命中 `target_id`）的记录，返回匹配列表 `[{"db_path": str, "object_type": str, "object_id": str, "title": str, "properties": dict, "registered_types": list[str]}]`；
       - `takeover_board_from_db(target_id: str, search_root: str | Path = ".", *, workspace_root: str | Path | None = None) -> dict[str, Any]`：在 `search_root` 下搜索 `target_id`（如 `"BD-CC-001"`，支持直接传 `board_key` 或某张 `card_id` 反查所属 `board_key`），自动以命中的 `db_path` 打开 `OntologyRuntime(kanban.spec, db_path=db_path)`，调用 `board_overview` + `render_board_handover` 组装上下文，返回 `{"db_path": str, "board_key": str, "runtime": OntologyRuntime, "overview": dict, "context": str}`（未找到时抛出 `LookupError`）。
  2. **`examples/command_center_demo.py` 支持持久化 SQLite 实例库（默认落盘 `.instances/`）**：
     * 新增可选参数 `build_default_command_center(data_dir: str | Path | None = None)`（默认落在仓库根目录下 `.instances/` 目录，使 `board_prod.sqlite3` 等实例真实落盘可被跨进程/新 Session 直接从 SQLite 检索 `BD-CC-001`；若每次启动重建种子数据，先清理旧文件再初始化，确保幂等）。
  3. **清理测试与更新技能/架构文档**：
     * 删除 `tests/test_conductor_rotation.py`（旧工单流换代测试，已随换代逻辑一并废弃）；
     * 更新 `tests/test_agent_hosts.py`：移除 `test_board_runner_auto_folds_and_rotates_architect_*` 等架构师自动换代测试，更新 `test_role_routed_host_factory_wires_argon_and_codebuddy` 断言默认不开启轮转，新增单元测试验证 `locate_object_in_databases("BD-CC-001", ...)` 与 `takeover_board_from_db("BD-CC-001", ...)` 能直接从 SQLite 数据库定位实例、还原 `OntologyRuntime` 并产出完整上下文（含 `Board.goal`、`Board.state` 与卡片列表）；
     * 更新 `~/.claude/skills/onto-board/SKILL.md`、`docs/ARCHITECTURE.md` 与 `_state.md`：去掉架构师自动换代描述，改为「手动开启空 OpenCode Session → 说『接管 openontology 实例 BD-CC-001 做 X』→ 自动检索 SQLite 数据库定位实例构成上下文 → 驱动任务 X」；
     * 实跑 `/Users/guci/miniforge3/bin/pytest -q` 全量测试 100% 通过（0 failed, 0 skipped 或仅必要 skip），`git diff open_ontology/__init__.py open_ontology/ui_display.json` 为空，并重启 `8765` 端口常驻服务使 `.instances/board_prod.sqlite3` 真实落盘。


---

## 当前战役：Model / Workspace 术语迁移（Batch 1 + Batch 2）

### 11. `C-WS-06` —— 集成验收：术语文档、全量回归与看板基线折叠
* **状态**：🟢 `DONE`（工作树实施完成，等待架构师独立复审与 `Board.goal` / `Board.state` 折叠收单）
* **已完成结果**：
  1. **新增 `GLOSSARY.md`**：统一 Model / Workspace / TBox / ABox / Behavior / Runtime / Device 词汇；明确 `workspace_id / object_type / object_id` 三级地址、工作空间身份与业务对象身份的区别、`workspace_root` 只是代码目录、聚合根声明与 `loaded / not_loaded / unavailable` 可用性语义。
  2. **规范分层保持**：`docs/ARCHITECTURE.md` 维持 M2 平台宪法（移除旧「知识激活」措辞、更新外围模块与四视角标签为实际实现、保留兼容映射）；`docs/TASK_BOARD.md` 维持 M1 `task_board` 与 M0 `BD-CC-001` 契约，两份文档互链。
  3. **实现状态同步**：`README.md` / `_state.md` / `_execution.md` 反映 Model/Workspace 迁移、工作空间目录、TBox/Behavior 投影、显式接管与双栈 HTTP 的实际水位；不再把已完成能力写成「施工中 / 尚未验收」。
  4. **双文档 `Board.goal` 装载**：`BoardRunner.sync_board_docs` / `fold_board_state` 与 `examples/command_center_demo.py` 种子在 `docs/ARCHITECTURE.md` 与 `docs/TASK_BOARD.md` 同时存在时按 `ARCHITECTURE + '\n\n---\n\n' + TASK_BOARD` 组合；单文件目录（仅有宪法）逐字节保持旧行为，显式自定义 `goal_relpath` 不追加看板规范。
  5. **文档一致性门禁**：新增 `tests/test_docs_consistency.py`，锁定术语表存在、M2 宪法不夹带 M0 看板 ID、实现状态文档不再使用旧标签措辞。
  6. **验收范围**：无损重开、目录身份、显式寻址、重复 ID 歧义、模型/事实隔离、`fork()` 隔离、旧 API 兼容与治理门禁均由既有测试与新增用例覆盖；全部数据库行为使用 `tmp_path`，未触碰生产看板库或服务。
* **限制**：`Board.goal` / `Board.state` 的生产库同步与已完成卡片折叠须在架构师复审通过后由主控会话执行；本单只交付机制与证据，不代替业务收单。

---

## 当前战役：M2 微内核门禁补强（Post-Batch）

### 12. `C-KERNEL-SEC-01` —— `post_approval` 主权复核、审计/注册表写门禁与外部入口 `Actor` 护栏
* **状态**：🟢 `DONE`（工作树实施完成，等待架构师独立复审与 `Board.state` 折叠收单）
* **已完成结果**：
  1. **两相 `post_approval` 主权复核**：`_run_handler` 先对 `handler` 阶段 diff 执行 `_check_authority`；钩子执行后再由 `SecurityAndAuthorityAspect.check_post_approval_authority(spec, action, pre_diff, full_diff)` 只判定**钩子追加出来的增量**（新增/删除对象、新增/删除连边、既有对象在 `pre_diff.after → full_diff.after` 之间的字段差），增量自身跨 source / ontology 双平面即抛 `InvariantError(code='MIXED_AUTHORITY')`。合并后的整份 diff 不参与跨平面比对，`steel_mdm_demo`（source → 自有）与 `saas_unplug_demo`（自有 → source）两段式单平面动作继续合法；抽出 `_field_side` 复用字段级平面判定，`object_change_side` 语义保持不变。
  2. **SQLite 写门禁触发器闭环**：`ontology_action_runs` 新增 `BEFORE INSERT / UPDATE` 门禁触发器（保留 `BEFORE DELETE` 无条件禁止），`ontology_registry` 新增 `BEFORE INSERT / UPDATE / DELETE` 门禁触发器；内核与切面自身的合法写入改走 `_write_gate()`（`_sync_registry`、`ApprovalGateAspect.stage_pending_approval`、`RejectionAuditAspect.record_rejection`），裸连接直写全部被 `GOVERNANCE_VIOLATION` 拦截且门禁事后保持关闭。
  3. **外部边界 RLS / 脱敏闭合**：`workshop` 的 `/api/links` 过滤掉任一端对象缺失（悬空边）或被 RLS 隐藏的连边；`/api/snapshot` 显式传入调用方 `Actor`；`runtime.call_function` 为只读函数批处理注入 `EditBatch(mask_fn=self._apply_masking)`，`ctx.get / ctx.query / ctx.query_linked` 对列白名单外角色脱敏，动作批处理不注入（暂存 diff 绝不写入 `***MASKED***`）；`http_server` 新增可选 `actor`，注入后枚举接口按 RLS + 列脱敏投影，不注入则逐字节保持历史裸行行为。
  4. **TDD 与验收**：先写 RED（`tests/test_kernel_aspects.py` 新增 10 条、`tests/test_command_center.py` 新增 3 条并实跑确认失败）再实现；定向 `test_kernel_aspects.py` **54 passed**、`test_command_center.py` **25 passed**；全仓 `pytest -q` **584 passed, 2 skipped（103.03s）**。
  5. **红线守恒**：`runtime.py` **1,388** 行（预算 1,400）、`aspects.py` **789** 行且不 import `runtime.py`、`git diff open_ontology/__init__.py open_ontology/ui_display.json` 为空、`workshop.py` / `workshop_ui.py` 仍无业务领域字段。
* **背景与架构决策**：`post_approval` 是「另一主权平面的第二次单相提交」，因此不能用合并 diff 比对 `action.affects_source`，否则会误杀既有的两段式演示动作；正确口径是「两段各自单平面、钩子增量自身不得跨平面」。
* **限制**：既有 `/api/function` 用例改为以 `finance` 角色调用 `portfolio_value`（断言值 4,080,000 不变）——非白名单角色（`ops` / `admin`）读到脱敏后的 0.0，这是本卡刻意关闭的读出口缺口；生产 `Board.goal` / `Board.state` 折叠仍须架构师复审后由主控会话执行。
