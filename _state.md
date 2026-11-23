# OpenOntology 当前状态真值表 (`_state.md`)

> **用途**：本文件是项目当前的唯一架构立场与基线快照。任何新开启的架构师会话或施工会话在开机时只需读取本文件、`docs/ARCHITECTURE.md`（M2 平台架构宪法）与 `docs/TASK_BOARD.md`（M1/M0 任务看板与自举协同规范），即可快速对齐全局上下文，无需回溯历史长会话。

---

## 最新执行检查点（2026-11-23 · 里程碑 WO-26 + WO-27A + WO-27B 动力学三分法与单一源码重构）

* **演进脉络**：在 `ec29958` 之上完成 `WO-26`（`console_server.py` + `web/` 现代四视角指挥台）、`WO-27A`（`4709857`：动力学三分法 `FunctionDef` / `EditFunctionDef` / `ActionDef`、彻底退役旧动作装饰器、`apply_overlay_to_spec` 与 **Gate 8**）与 **`WO-27B`**（单一源码标准领域蓝图投影 `export_spec_blueprint`、定制插件与零补丁世界模型）。
* **WO-27A / WO-27B 核心交付**：
  1. **动力学三分法与单一源码投影（`dsl.py` / `crucible.py` / `falsifier.py` / `design.py`）**：`export_spec_blueprint(spec, ...)` 直接由 Python `OntologySpec` 投影声明式蓝图；`compose_blueprints` / `compile_blueprint` / `SymbolicCrucible` / `DifferentialFalsifier` / `open_ontology.design` 全链路保留 `_base_specs` 底层图灵完备 `@spec.edit_function` / `@spec.function` / `@spec.invariant`。
  2. **淘汰 7 大标准领域双写 JSON 蓝图**：`examples/std_{core,crm,o2c,p2p,p2m,r2r,h2r}_ontology.py` 全部改为 `export_spec_blueprint(spec, ...)` 薄封装（`open_ontology/runtime.py` 保持 **0 行 diff / 1,328 行**）；`examples/std_enterprise_world_model.py`（`build_org_rls_enterprise_spec`）与 `examples/adapters/erp_world_mount.py` 消除 `_attach_standard_functions` 运行期动态补丁；`apply_overlay_to_spec` 支持 `custom_edit_functions` / `custom_functions` / `custom_invariants` 单调插件与规则动作收紧守卫。
* **全量离线回归与双 ERP 实证**：
  - Python 3.13 离线全量回归 `/Users/guci/miniforge3/bin/python3 -m pytest --ignore=tests/test_agent_value_mcts.py -k "not odoo and not camel and not jsherp and not video_artifact_quality and not test_group_" -q` -> **`697 passed, 6 skipped, 81 deselected`**；
  - `tests/test_kinetic_split_and_overlay_on_spec.py` + `tests/test_std_world_model_domains.py` + `tests/test_wo24c_dual_erp_adapters.py`（含真实 Odoo 19 & 华夏 ERP 联调）-> **`37 passed`**。

---

## 历史执行检查点（2026-11-22 · 里程碑 WO-13 ~ WO-21 + WO-18 全栈收官：`ec29958`）

* **基线版本**：`main` 本地已收口提交至 `ec29958afd9b7fcfd8df92f1db641f95bd801751`（`WO-13` ～ `WO-21` + `WO-18` 全部独立验收 PASS，待本轮 `WO-22` 活文档对齐验收后统一 `git push origin main` 并启动 `WO-23`）。
* **核心成果（Pure Runtime 10 模块 JVM 核 + 5 大零耦合生产运行壳 + 双异构真实 ERP × 国产模型全栈实证）**：
  1. **标准领域模块零互耦门禁（Gate 7，`WO-14`, commit `5811de7`）**：`examples/std_*_ontology.py`（`std_core` / `std_crm` / `std_o2c` / `std_p2p`）统一通过 5 原语常识细腰协作，严禁横向交叉导入。
  2. **标准 MCP Server + REA 5 原语常识细腰（`WO-16`, commit `11517d2`）**：新增 `open_ontology/mcp_server.py`（`1,020` 行，纯 stdlib stdio + Streamable HTTP `POST /mcp`，服务端钉死 `Actor` / `ScopedView`）、`examples/std_p2p_ontology.py`、`examples/commonsense_exchange.py` 与 `examples/live_mcp_domestic_agent_demo.py`（实证隔离 `hermes` CLI + `deepseek/deepseek-v4.1-flash` 自动受控开立 `WorkCard`）。
  3. **生产安全双壳：`AutonomyRampController` & `SagaReconciler`（`WO-19`, commit `9169d5f`）**：新增 `open_ontology/autonomy.py`（`543` 行，三级爬坡 `SHADOW` / `COPILOT_ESCALATE` / `AUTO_UNDER_CEILING`）与 `open_ontology/reconciliation.py`（`339` 行，写后超时失联先 `observe` 再决断 `RECONCILED_REMOTE_COMMITTED` / `RECONCILED_NOT_COMMITTED` / `AMBIGUOUS_ESCALATED`）。
  4. **PostgreSQL 16 多进程持久底座与 PL/pgSQL 物理写门禁（`WO-20`, commit `39c1bca`）**：新增 `open_ontology/schema_pg.sql`（`155` 行）、`open_ontology/postgres_backend.py`（`608` 行）与 `docker/postgres/`；`Gate 5` 双守卫 SQLite + PG16 同构 6 表；跨连接并发 OCC、`fork()` 零锁 `:memory:` 沙盘全部通过；`open_ontology/runtime.py` 降至 **1,328 行**（`<= 1,335` 预算）。
  5. **零内核污染外挂 MDM Sidecar + 河北钢厂两死结插件（`WO-21`, commit `ed979aa`）**：新增 `open_ontology/mdm_plugin.py`（`353` 行，`MDMSidecarPlugin`）与 `examples/steel_mdm_plugin.py`（`SteelStorageBin` `BIN_EXACT` 精确溯源 vs 品牌 FIFO + 旁路散码前松 `PROVISIONAL` 不卡地磅、关账/付款前硬拦；SQLite & PG16 `6/6` 全绿）。
  6. **欧亚双异构真实开源 ERP × 国产模型盲考与双企 B2B 全栈实验室（`WO-15`/`WO-17`/`WO-18`, `c808c60`/`917cd0b`/`ec29958`）**：真实 **Odoo 19**（`:8069`）与 **华夏 ERP `jshERP`**（`:19999`，`docker/jsherp/`）均已通过 `deepseek-v4.1-flash` 零提示盲考反编译薄驱动及隐藏世界差分证伪；[live_b2b_steel_trade_lab.py](file:///Users/guci/work/open-ontology/examples/live_b2b_steel_trade_lab.py) 完成 **jshERP + Odoo 19 + 克隆 BottleCRM + PG16 + 双端隔离 `hermes` (`deepseek/deepseek-v4.1-flash`)** 经 5 原语常识信封互操作，4/4 场景 `235.85s` 零报错全通（18 次真实 MCP 调用 `0 is_error`，三端残留全归 0）。
* **全量回归与边界实证（`ec29958`）**：
  - Python 3.11.15 & 3.13.12 双版本 7 文件核心/生产壳/双企实验室套件 **38/38 passed**；
  - 全量离线回归 `/Users/guci/miniforge3/bin/pytest --ignore=tests/test_agent_value_mcts.py -k "not odoo and not camel and not jsherp and not video_artifact_quality" -q` -> **`650 passed, 6 skipped, 63 deselected`**（6 条 skip 为未构建视频产物的 `tests/test_video_artifacts.py`）；`vendor_crm/bottlecrm.sqlite3` 保持只读未改（SHA-256 `4a6c51da5e14079a598b3565b06eff886e8073d6ba9c545d867ee1619ccc8130`）。

---

## 历史执行检查点（2026-11-21 · 里程碑 WO-01 ~ WO-12 收官：027fb58）

* **基线版本**：`origin/main` 已推送至 `027fb58`（`WO-10` ～ `WO-12` 连续独立审计 PASS 收官；下方 2026-11-15 及更早检查点均已作为历史档案保留，现行架构与能力以下列实证为准）。
* **核心成果（Java 式 Pure Runtime + 极简 `javac` Design-Time + `agy` CLI AI-FDE）**：
  1. **Pure Runtime 物理瘦身与 Gate 6 零例外锁死（`WO-12`, commit `027fb58`）**：删除 `OntologyRuntime.plan_mcts()`（`open_ontology/runtime.py` 降至 **1,331 行**，硬预算 `<= 1,335`），打破 `osdk.py -> model_views.py` 耦合并移除 `__init__.py` 中非核心导出（零 `__getattr__` 延迟后门）；干净子进程 `import open_ontology` 从原先 `33` 模块 / `30,326` 行收口为精确 **10 个纯运行时模块 / 8,729 行（-71.2%）**。`tests/test_architecture_boundaries.py` **Gate 1–6** 零例外检验 Runtime 对 `design` / `induction` / `workshop` / `mcts` / `mpc` / `model_views` 完全不可知。
  2. **`DeviceDriverRegistry` 总线与极简 `design.py` 裁判 CLI（`WO-11`, commit `fb16e79`）**：新增 `open_ontology/drivers.py`（`mount` / `unmount` / `replace` 原子回滚、共享主数据合并、`CROSS_DEVICE_WRITE_FORBIDDEN` 单动作单写设备红线）并实时接入 `workshop.py` 连接器状态投影；新增零 LLM 的 `python -m open_ontology.design check | verify` 编译器与孤立沙箱裁判 CLI；外部 `agy` CLI（`gemini-4-neon-medium`）依据 `skills/openontology-fde/SKILL.md` 在只读锁定隔离工作区独立探索 145 表 BottleCRM 克隆 + 30 表脏 ERP 库并自愈通过两轮 `CORROBORATED` 验证及外层反作弊独立复核（动作覆盖 `1.0`、迁移覆盖 `0.9231`、0 逃逸 0 脏写）。
  3. **纯同步 6 表核销 + M1 统一 `WorkCard` + 标准薄本体与四级实体解析（`WO-01` ～ `WO-10`）**：彻底废除 M2 `ApprovalGateAspect` / `PENDING_APPROVAL` / `post_approval` 等挂起机制，`ontology_action_runs.status` 仅允许 `COMMITTED / REVERTED / REJECTED`；审批/人工/跨部门协同统一用 M1 `WorkCard`（`$t_1$` 防幻读重算 + SoD 分离）；固化 `std_core` / `std_crm` / `std_o2c` 标准骨架 + 单调 `CustomerOverlay` + 闭集角色 + `open_ontology/induction/resolution.py` L0–L4 四级 `CrosswalkTable`（`CONFLICTING_HARD_KEY` 硬阻断 + L4 `WorkCard` 拍板）与 `DifferentialFalsifier` 覆盖率驱动差分证伪。
* **全量离线回归实证**：`/Users/guci/miniforge3/bin/pytest -m "not live_llm" --ignore=tests/test_video_artifacts.py --ignore=tests/test_agent_value_mcts.py -k "not video" -q` -> **`663 passed, 4 deselected`**；`schema.sql` 维持 6 表未改，`vendor_crm/bottlecrm.sqlite3` 保持只读未改（SHA-256 `4a6c51da5e14079a598b3565b06eff886e8073d6ba9c545d867ee1619ccc8130`）。

---

## 历史执行检查点（2026-11-15 · C-102 编排器认领超时回收收官 · 已归档）

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

## 1. 现在什么算数（Canonical Baseline · `ec29958`）

### 1.1 M2 系统内核、可插拔生产运行壳与 Design-Time 工具链分层
* **Pure Runtime 10 模块白名单（`import open_ontology` 总计 `8,781` 行，零延迟 `__getattr__` 后门）**：
  * **`open_ontology/__init__.py`（142 行）**：严格仅导出纯 Runtime 符号（`.spec`、`.sandbox`、`.runtime`、`.settlement`、`.drivers`、`.query`、`.osdk`），零扩展模块与业务模型泄漏。
  * **`open_ontology/spec.py` + `runtime.py`（1,328 行，预算 `<= 1,335`） + `sandbox.py` + `query.py` + `schema.sql`**：声明式 OWL 元模型原语（`ObjectType` / `ObjectProperty` / `AutoID`）、同构 6 表页表读写（`ontology_action_runs` 仅允许 `COMMITTED / REVERTED / REJECTED`）、内存叠加沙箱 `EditBatch`、`ObjectDiff` 计算、隔离复制 `fork()`（纯内存 `:memory:`）。
  * **`open_ontology/aspects.py`（1,043 行，六大纯同步系统调用切面）**：`ColumnMaskingAspect` 列脱敏、`SecurityAndAuthorityAspect` RBAC/RLS 与字段主权、`IdempotencyAndOCCAspect` 幂等与乐观锁、`RejectionAuditAspect` 拒绝审计、`AutomationReflexAspect` 提交后反射触发、`IdleAwareCallbackAspect` 忙闲感知回调投递。
  * **`open_ontology/settlement.py`（1,172 行）**：`SourceBridge` 与 `HomomorphicSettlementAspect`，提供 `world_mode="coupled"` 下的三阶段无锁同态回读核销与授权冲正。
  * **`open_ontology/drivers.py`（448 行）**：纯 M2 多外设驱动总线 `DeviceDriverRegistry` / `MountedDevice`（`mount` / `unmount` / 原子 `replace`、`CROSS_DEVICE_WRITE_FORBIDDEN` 单动作单写设备红线、共享主数据合并）。
  * **`open_ontology/ingestion.py`（551 行）**：`SnapshotIngestionDriver` 快照摄入与多源列拼接（MDO）。
  * **`open_ontology/osdk.py`（1,904 行）**：内置只读 `describe_tbox` / `resolve_scoped_names`（与 `model_views.py` 同源无环依赖）、`export_llm_tools`、`AIPAgentExecutor`（可选 `planner_factory` 零耦合 MCTS）与确定性评测 `run_eval_suite`。
* **可插拔生产运行壳层（总计 `3,018` 行 · Gate 6 验证对 `import open_ontology` 零加载）**：
  * **`open_ontology/mcp_server.py`（1,020 行）**：`OntologyMCPServer` / `start_mcp_http_server`，纯 stdlib 支持 stdio + Streamable HTTP `POST /mcp`，服务端钉死 `Actor` / `ScopedView`。
  * **`open_ontology/autonomy.py`（543 行）**：`AutonomyRampController` / `AutonomyPolicy`（`SHADOW` / `COPILOT_ESCALATE` / `AUTO_UNDER_CEILING` 三级爬坡与追加式影子账本）。
  * **`open_ontology/reconciliation.py`（339 行）**：`SagaReconciler` 写后超时失联 Observe-Before-Retry 幂等对账。
  * **`open_ontology/postgres_backend.py`（608 行） + `open_ontology/schema_pg.sql`（155 行）**：PostgreSQL 16 跨 schema 同构 6 表持久底座，PL/pgSQL 物理写门禁 + 追加防删触发器 + 跨进程并发 OCC（容器 `docker/postgres/`）。
  * **`open_ontology/mdm_plugin.py`（353 行）**：`MDMSidecarPlugin` 零内核污染跨系统增量 L0–L4 主数据协调插件。
* **Design-Time 编译与波普尔证伪工具链（对 Runtime 零例外物理不可见）**：
  * **`open_ontology/design.py`**：`python -m open_ontology.design check | verify` 确定性命令行裁判，含只读 `?mode=ro` SQLite 画像探测、`DeclarativeSQLiteDriverBridge` 及隔离沙箱复验；
  * **`open_ontology/induction/`**（`dsl.py`、`resolution.py`、`phenomenon.py`、`crucible.py`、`falsifier.py`、`decompiler.py` 等）：单一源码投影 `export_spec_blueprint`、直接面向 `OntologySpec` 的只增不破叠加 `apply_overlay_to_spec`（支持 `overlay.json + custom_edits.py` 客户 `EditFunctionDef` / `FunctionDef` / `InvariantDef` 插件）、`L0–L4 CrosswalkTable`、五关 `SymbolicCrucible`、覆盖率驱动 `DifferentialFalsifier`；
  * **`skills/openontology-fde/SKILL.md`**：隔离只读工作区 AI-FDE（`agy` / `opencode` CLI）反编译工程规程。
* **显式按需导入扩展模块**：
  * **`open_ontology/console_server.py`（`WO-26`）**：现代四视角指挥台 API/交互壳层，服务化驱动 `web/`（Model / Runtime / World / Device、沙盘预演冲正与 H2R/RLS 隔离演示）；
  * **`open_ontology/mcts.py` & `mpc.py`**：`OntologyMCTSPlanner`（反事实 `fork()` 前瞻树搜索）与 `ClosedLoopMPCController`（滚动时域闭环规划）；
  * **`open_ontology/workspaces.py`**：工作空间身份清单、`not_loaded` 目录呈现与通用 `takeover_workspace`；
  * **`open_ontology/model_views.py`**：从 `.osdk` 复用 `describe_tbox` / `resolve_scoped_names`，并提供 `describe_behavior` / `describe_openapi`；
  * **`open_ontology/workshop.py` + `workshop_ui.py` + `http_server.py` + `webhooks.py` + `meta_admin.py`**：四视角指挥台（支持绑定 `driver_registry` 实时投影连接器状态）、拓扑大图、Webhook 与 HTTP 传输边界；
  * **`open_ontology/execution/fabric.py`**：透明执行织入层。

### 1.2 标准领域骨架、跨企常识细腰与演示本体（`examples/*`）
1. **标准薄领域模型与跨企 REA 常识细腰**：`examples/std_core_ontology.py`（含 `AccountingEntity` / `BusinessParty` / `TradeResource` / `TradeCommitment` / `FulfillmentEvent` 5 原语、`WorkCard` 与闭集 `STD_ROLES`）、`std_crm_ontology.py`、`std_o2c_ontology.py`、`std_p2p_ontology.py`、`commonsense_exchange.py`（受 Gate 7 零交叉互耦保护）。
2. **既有 6 大业务蓝图 / 实例**：`software_factory`（`dev_factory.py`）、`task_board`（`kanban.py`）、`coldchain_mcts`、`crm_saas_unplug`、`steel_mdm`（`APPROVAL_REQUIRED_NEW_MATERIAL_SPEC` + `WorkCard`）、`osv2_governance`。
3. **跨异构外设、AI-FDE、MDM Sidecar 与全栈 B2B 实验室**：`examples/dual_device_driver_swap_benchmark.py`、`mudpit_crm_o2c_feishu_stress.py`、`agy_fde_mudpit_decompile_benchmark.py`、`agy_fde_live_odoo19_protocol_benchmark.py`、`agy_fde_live_jsherp_protocol_benchmark.py`、`live_mcp_domestic_agent_demo.py`、`steel_mdm_plugin.py`、`live_b2b_steel_trade_lab.py`。

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

* **本地扩展与多容器服务实例**：
  * PostgreSQL 16（`docker/postgres/`，`127.0.0.1:15432`）：`open_ontology` 持久化与 PL/pgSQL 物理写门禁容器；
  * 华夏 ERP（`docker/jsherp/`，`http://127.0.0.1:19999/jshERP-boot`）：国产开源 ERP 集成与 AI-FDE 盲考靶场；
  * Odoo 19（`http://localhost:8069`）：保留 `['erp_north', 'erp_south', 'odoo_demo']` 三库供 O2C / 双 ERP / 全栈双企联调回归使用；
  * 作战台服务可按需通过 `open_ontology.workshop.start_workshop_server` 拉起（`/topology?model=<id>`）。
* **全量离线回归基准（`WO-27B`）**：
  ```bash
  /Users/guci/miniforge3/bin/python3 -m pytest --ignore=tests/test_agent_value_mcts.py -k "not odoo and not camel and not jsherp and not video_artifact_quality and not test_group_" -q
  ```
  离线回归实测 **`697 passed, 6 skipped, 81 deselected`**（`0 failures`），覆盖 **Gate 1–8** 零例外边界门禁、动力学三分法与单一源码插件（`tests/test_kinetic_split_and_overlay_on_spec.py`）、`OntologyMCPServer`、`AutonomyRampController`、`SagaReconciler`、`open_postgres_runtime`、`MDMSidecarPlugin` & 钢厂插件、`live_b2b_steel_trade_lab` 4 场景、Odoo 19 & 华夏 ERP 集成以及文档防漂移门禁（`tests/test_docs_consistency.py`）。

---

## 3. 架构与工程纪律红线（不可违背）

1. **Pure Runtime 与 M2 纯洁性铁笼（Gate 1–8）**：`open_ontology/runtime.py <= 1,335` 行（现 `1,328` 行），`import open_ontology` 严格收口至 10 模块白名单，零 `__getattr__` 后门；Gate 6 禁止加载任何设计期/生产壳/`psycopg`/UI/`console_server`/规划扩展；Gate 7 禁止 `examples/std_*_ontology.py` 横向交叉导入；Gate 8 锁死动力学三分法（`FunctionDef` / `EditFunctionDef` / `ActionDef`）正交隔离与声明式 DSL 反 xtUML 纯洁性；`ui_display.json`、`workshop.py`/`workshop_ui.py` 严禁业务词汇或私有字段泄漏。
2. **Design-Time 选手/裁判物理隔离**：外部 AI-FDE（`agy` CLI）严禁修改只读输入、样本源文件（如 `vendor_crm/bottlecrm.sqlite3`）或裁判脚本，产物一律经过 `open_ontology.design check | verify` 证伪并满足覆盖率阈值后方可签发。
3. **双模型协同与证据原则**：主控架构师只做顶层建模、计划设计与独立审计；执行侧遵循实证输出，文档与 `_state.md` 严禁遗留已退役架构符号与过期基线数字。

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
