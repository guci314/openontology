# OpenOntology 规格说明书（TK-ONTO-018 · 统一作战指挥中心 Command Center：多 Ontology 蓝图列表 + 多 Instance 实例列表 + 世界状态 / Action / 沙盘推演联动台）

## 1. 目标与核心交互理念

升级 `/Users/guci/work/open-ontology/open_ontology/workshop.py` 并新增全量演示入口 `/Users/guci/work/open-ontology/examples/command_center_demo.py`，构建 **《OpenOntology 统一作战指挥中心（Command Center）》**。

核心交互结构严格遵循以下二元模型：
1. **主界面左侧为「一组 Ontology 蓝图列表」与「一组 Instance 实例列表」**：
   - **`Ontology`（本体蓝图 / PIM）**：纯静态业务元模型定义。**点击一个 `Ontology`**，右侧主舞台显示该本体的 **元模型拓扑（内联 SVG 关系图）与完整 `ObjectTypes`、`Interfaces`、`LinkTypes`、`Functions`、`Actions`、`Automations` 规格定义**。
   - **`Instance`（本体实例化 / Runtime + PSM）**：某个 `Ontology` 配置了具体数据库（SQLite / 内存 / 文件）及后台外部系统（ERP、CRM、SaaS、WMS、Agent Harness）后的活体运行环境。**点击一个 `Instance`**，右侧主舞台显示该实例的 **后台挂载配置栏（DB + ERP/CRM/SaaS 连接状态）、实时世界状态（Objects + Links + Overlay 补丁）、Action 下拉列表、自动参数表单、单步干跑 / MCTS 多步沙盘推演按钮、正式执行按钮**；点击执行 Action 或沙盘推演后，**实时显示执行结果（含 `Before -> After` Diff 或 MCTS 搜索树）并同步更新世界状态与审计账本（支持一键 `Approve` 审批与 `Revert` 时光机撤回）**！

> **向后兼容铁律**：必须完整保留现有 `WorkshopServer(server_address, runtime, ...)` 与 `start_workshop_server(runtime, ...)` 的单 `runtime` 调用签名（内部可自动将单个 `runtime` 包装为含 1 个默认 Ontology 与 1 个默认 Instance 的 `CommandCenterRegistry`），确保 `tests/test_osv2_parity.py` 与 `tests/test_dev_factory.py` 中已有的全部 204 条测试 100% 继续通过！

---

## 2. 后端数据结构与 API 设计（`open_ontology/workshop.py`）

### 2.1 核心注册表数据结构

1. **`ConnectorMount`（后台数据库 / ERP / CRM / SaaS 挂载状态）**
   ```python
   @dataclass
   class ConnectorMount:
       name: str          # 如 "Odoo 19 ERP (erp_north)", "BottleCRM (django-crm 1.44.0)"
       kind: str          # "Database" | "ERP" | "CRM" | "SaaS" | "WMS" | "Harness"
       endpoint: str      # 如 "http://localhost:8069/erp_north", "sqlite://bottlecrm.sqlite3"
       status: str = "CONNECTED"  # "CONNECTED" | "UNPLUGGED" | "STANDBY"
   ```

2. **`OntologyCatalogEntry`（Ontology 蓝图条目）**
   ```python
   @dataclass
   class OntologyCatalogEntry:
       ontology_id: str
       name: str
       category: str = "Enterprise"
       description: str = ""
       spec: OntologySpec = ...
   ```

3. **`OntologyInstanceEntry`（Instance 实例化条目）**
   ```python
   @dataclass
   class OntologyInstanceEntry:
       instance_id: str
       ontology_id: str
       label: str
       description: str
       db_uri: str
       runtime: OntologyRuntime
       connectors: list[ConnectorMount] = field(default_factory=list)
       default_actor: Actor | None = None
       sample_action_params: dict[str, dict[str, Any]] = field(default_factory=dict)
       mcts_config: dict[str, Any] | None = None  # 若提供则支持一键运行 MCTS 多步推演
       unplug_fn: Callable[[], dict[str, Any]] | None = None  # 若提供则支持一键拔除 SaaS
   ```

4. **`CommandCenterRegistry`（统一注册中心）**
   - `register_ontology(ontology_id, name, spec, category="Enterprise", description="") -> OntologyCatalogEntry`
   - `register_instance(instance_id, ontology_id, label, runtime, db_uri="sqlite://:memory:", description="", connectors=..., default_actor=..., sample_action_params=..., mcts_config=..., unplug_fn=...) -> OntologyInstanceEntry`
   - `fork_instance(source_instance_id: str, new_instance_id: str | None = None, label: str | None = None) -> OntologyInstanceEntry`：
     - 调用 `source.runtime.fork()` 毫秒级克隆出一个独立的平行沙盘 Instance，挂在同一 `ontology_id` 下，方便用户在界面上随时分叉做破坏性实验！

### 2.2 REST JSON API 扩展（同时兼容单实例旧路由与多实例新路由）

- `GET /api/catalog`：返回全部 `ontologies`（含每个蓝图的 `ObjectTypes / Interfaces / LinkTypes / Functions / Actions / Automations` 完整 schema 与统计）与全部 `instances`（含所属 `ontology_id`、`db_uri`、`connectors`、对象计数、账本计数、是否支持 MCTS / Unplug）。
- `GET /api/ontology/<ontology_id>`：返回指定 Ontology 蓝图的完整元模型详情（字段类型、`__owned__` 主权、`__masked_fields__` 脱敏规则、Link 基数、Function 描述、Action 参数 JSON Schema 与角色护栏、Automation 描述）。
- 在已有 `/api/meta`、`/api/objects`、`/api/interface`、`/api/links`、`/api/runs`、`/api/fingerprint`、`/api/simulate`、`/api/submit`、`/api/approve`、`/api/revert`、`/api/function`、`/api/automations/run` 上支持通过查询参数 `?instance=<instance_id>`（或 JSON body 中的 `"instance": "<instance_id>"`）指定目标实例；若未传 `instance` 参数，则默认指向第一个注册的实例（100% 兼容旧测试！）。
- 新增实例级高级操作路由：
  - `POST /api/instance/fork`：传入 `{"instance": "<source_id>", "label": "..."}`，调用 `registry.fork_instance` 克隆出一个新沙盘实例并返回新实例信息；
  - `POST /api/instance/mcts`：传入 `{"instance": "<instance_id>", "num_simulations": 60}`，在目标实例上运行 `plan_mcts`（使用该实例预设的 `mcts_config` 或请求传入的配置），返回搜索树根节点统计 `root_stats`、`best_sequence`、`best_value`、`pruned_branches`、`pruned_errors`，且主库零污染；支持 `{"commit": true}` 一键把最优序列正式提交到该实例；
  - `POST /api/instance/unplug`：对配置了 `unplug_fn` 的实例执行一键拔除外部 SaaS，将连接器状态更新为 `"UNPLUGGED"` 并返回结果。

---

## 3. 前端界面设计（`GET /` 深色科技风统一指挥台）

1. **左栏（双列表导航区）**：
   - **上半区：`📐 Ontology 蓝图列表`**：
     - 展示所有 Ontology 蓝图卡片（含名称、类别标签、`Objects / Links / Actions` 数量徽章）；
     - **点击某个 Ontology 卡片**：将该 Ontology 设为选中态，**右侧主舞台立即切换为「📐 蓝图定义视图（Ontology Schema View）」**，同时左栏下半区的 Instance 列表高亮/筛选属于该 Ontology 的实例（并提供「显示全部实例」开关）。
   - **下半区：`🌍 Instance 实例列表`**：
     - 展示实例卡片（含实例名、所属 Ontology 名、`DB` 标识、各 `ConnectorMount` 在线绿灯 `🟢 CONNECTED` 或脱壳紫灯 `🟣 UNPLUGGED`、当前对象总数与动作数）；
     - 底部带 **`[+ Fork 克隆当前实例沙盘]`** 按钮；
     - **点击某个 Instance 卡片**：将该 Instance 设为激活态，**右侧主舞台立即切换为「🌍 实例作战室视图（Instance Operational View）」**！

2. **右侧主舞台模式 A：「📐 蓝图定义视图」（当点击左侧某个 Ontology 时显示）**：
   - 顶部：蓝图标题、简介、以及一键 **`[🚀 进入该蓝图的首个运行实例]`** 按钮；
   - **动态内联 SVG 元模型拓扑图**：自动将该 Ontology 的 `ObjectTypes` 排布为节点、将 `LinkTypes` 绘制为带箭头与基数标签的有向连线；
   - **四大元模型规格面板**：
     - `📦 ObjectTypes & Interfaces`：展示每个对象类的字段表，用醒目徽章标记 `主键 PK`、`★ __owned__ 本体自有`、`🔒 __masked__ 列级脱敏（允许角色）`、`🛡️ RLS 安全标签`；
     - `🔗 LinkTypes`：展示源对象 $\to$ 目标对象、基数、`owned` 主权；
     - `🧮 Functions & ⚡ Automations`：展示全部只读函数与条件反射触发器；
     - `🎯 Actions`：展示每个动作的参数字段、允许角色、审批角色、`affects_source` 标记。

3. **右侧主舞台模式 B：「🌍 实例作战室视图」（当点击左侧某个 Instance 时显示）**：
   - **顶部「实例环境与后台挂载栏（Instance & PSM Mount Bar）」**：
     - 显示当前实例名称、`db_uri`、所有后台连接器徽章（如 `ERP: Odoo 19 erp_north [🟢 CONNECTED]`、`CRM: BottleCRM [🟢 CONNECTED]`）、状态指纹摘要（`Objects / Links / Overlay / Runs`），以及操作按钮：**`[🔄 触发 Automations]`**、**`[🍴 Fork 平行沙盘]`**、**`[🔌 拔除外部 SaaS]`**（当实例支持 unplug 时显示）。
   - **左侧「🌍 世界状态（World State）」**：
     - 顶部 Tab 切换各 `ObjectType`（含对象数量）与 `Interfaces` 多态视图；
     - 对象卡片列表：清晰展示每个对象的主键、标题、`v1/v2` 版本号、各个字段的当前值（**属于 `__owned__` / `ontology_overlay` 的字段用青色/金色星标高亮**，被列级脱敏的字段显示 `REDACTED / 0`）；
     - 下方实时展示当前实例的 `Links（语义关系边）` 与 `Functions（点击即可实时调用函数查看返回 JSON）`。
   - **右侧上半区「🎯 Action 执行与沙盘推演台」**：
     - **Action 下拉列表**：切换 Action 时，自动根据该 Action 的参数 Schema 渲染可编辑表单，并**自动填入该实例预设的 `sample_action_params`（或从当前对象列表中智能提取首个对象 ID 预填）**，让用户点开就能直接点按钮试跑，无需手敲 JSON！
     - **并排三大推演与执行按钮**：
       1. **`[🔍 单步沙盘推演 (Simulate)]`**：零副作用干跑，在下方「执行结果面板」高亮展示 `valid: true/false`、护栏拦截错误码 `error_code`、是否需要审批、以及 **`Before -> After` 字段级 Diff 表**（世界状态不改变）；
       2. **`[🌲 MCTS 多步沙盘推演]`**：（当实例带 MCTS 配置时）一键在平行宇宙执行蒙特卡洛树搜索，展示根节点各动作的 `P / N / Q` 表格、被符号规则剪枝的非法分支列表、全局最优动作序列与终局价值，并提供 **`[⚡ 一键提交最优序列]`** 按钮；
       3. **`[⚡ 正式执行 (Submit)]`**：真正执行该 Action，立即在「执行结果面板」显示本次提交状态（`COMMITTED` / `PENDING_APPROVAL` / `REJECTED`）与 `ObjectDiff`，并**毫秒级同步刷新左侧「世界状态」对象列表与下方「审计账本」**！
   - **右侧下半区「📋 动作审计账本与时光机（Action Runs）」**：
     - 实时显示每次动作执行记录；
     - 对 `PENDING_APPROVAL` 提供 **`[✅ 批准 Approve]`** 按钮；
     - 对 `COMMITTED` 提供 **`[↩️ 撤回 Revert]`** 按钮，点击后同步回滚左侧世界状态！

---

## 4. 全量预装启动器（`examples/command_center_demo.py`）

编写 `/Users/guci/work/open-ontology/examples/command_center_demo.py`：
- 提供 `build_default_command_center() -> CommandCenterRegistry`，预装 **5 个核心 Ontology 蓝图** 与 **7 个配置好真实数据的 Instance 实例**：
  1. **Ontology 1：`software_factory`（AI 软件研发工厂本体）**
     - `Instance 1A (factory-prod)`：挂载 SQLite + `CodeBuddy CLI` / `OpenCode HTTP` / `Hermes Agent` 三个 Harness 机械臂连接器，预装 5 名研发成员（架构师、程序员 A/B/C、测试官）与在途/完结工单。
  2. **Ontology 2：`coldchain_mcts`（冷链医药调度与 MCTS 规划本体）**
     - `Instance 2A (coldchain-wave3)`：挂载 WMS / TMS / 温控遥测连接器，预装 3 波医药订单（`ORD-1/2/3`）与 3 辆冷链车（`TRK-FAST/MID/HEAVY`），并挂载 MCTS 规划配置（在界面点击 `[🌲 MCTS 多步沙盘推演]` 即可现场跑出 `-55,400 -> +44,600` 的三步最优规划并支持一键提交到世界状态！）。
  3. **Ontology 3：`crm_saas_unplug`（多云 SaaS 寄居吸纳与脱壳本体）**
     - `Instance 3A (saas-parasitic)`：寄生态实例，挂载 `Shopify` / `Stripe` / `Zendesk`（`CONNECTED`），预装客户、订单、工单与 AI 分诊 `overlay`，并绑定 `unplug_fn` 支持在网页上一键拔除 SaaS 插头；
     - `Instance 3B (saas-standalone)`：脱壳态实例，外部连接器全部处于 `UNPLUGGED`，所有对象 `__owned__=True` 独立运行。
  4. **Ontology 4：`steel_mdm`（钢厂主数据破局与三视角对账本体）**
     - `Instance 4A (steel-mill-north)`：挂载 `ERP 合同库`、`WMS 料场库`、`MES 高炉配料系统`，预装标准牌号、12 条散码合同 SKU、进厂批次与高炉配料单，支持在界面上跑 `recommend_spec_for_sku`、`approve_sku_mapping` 与 `reconcile_multi_view_ledger` 三视角对账。
  5. **Ontology 5：`osv2_governance`（钢卷资产与 OSv2 四大原语治理本体）**
     - `Instance 5A (osv2-coil-prod)`：挂载 `华北现货库` 与 `财务核价中心`，预装 `SteelCoil` 与 `ScrapLot`，支持在界面切换角色体验 `__masked_fields__` 列级脱敏、`Interface: PricedAsset` 多态查询与 3 条 `Automations` 条件反射。
- 支持命令行直接启动：`python3 -m examples.command_center_demo --port 8765`。

---

## 5. 验收测试要求（`tests/test_command_center.py`）

新增 `tests/test_command_center.py`（至少 12 条测试），验证：
1. `build_default_command_center()` 成功注册全部 5 大 Ontology 蓝图与所有预装 Instance；
2. `GET /api/catalog` 返回完整的 `ontologies` 与 `instances`（含每个实例的 `db_uri` 与 `connectors` 列表）；
3. `GET /api/ontology/<id>` 正确返回各本体的 `ObjectTypes`、`LinkTypes`、`Functions`、`Actions` 元模型定义；
4. 通过 `?instance=<id>` 切换不同实例查询 `/api/objects`，验证各实例世界状态完全隔离；
5. 对实例调用 `/api/simulate`（单步沙盘推演返回 Diff 且不改状态）与 `/api/submit`（正式执行并同步更新该实例的世界状态）；
6. 对 `coldchain-wave3` 实例调用 `POST /api/instance/mcts`，验证返回 `best_value == 44600`、剪枝统计与最优三步序列，且支持 `commit=True` 同步更新世界状态；
7. 对实例调用 `POST /api/instance/fork`，验证动态克隆出新的沙盘实例并出现在 `/api/catalog` 的实例列表中；
8. 对 `saas-parasitic` 实例调用 `POST /api/instance/unplug`，验证连接器状态变为 `UNPLUGGED`；
9. 全仓 `pytest -q`（包含原有的 204 条测试 + 新增测试）100% 通过！
