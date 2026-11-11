# OpenOntology 规格说明书（TK-ONTO-015 · 100% 追平 Palantir OSv2：补齐 Automations + Interfaces + 列级脱敏 + MDO/LWW 冲突策略 + 通用交互式 Workshop Web 作战台）

## 1. 目标

在已整合 MCTS 沙盘引擎（`TK-ONTO-014`）的 `OpenOntology` 内核基础上，一鼓作气补齐与 **Palantir Foundry OSv2 + Workshop** 对标的最后 **4 个核心原语 + 1 个通用交互式 Web 作战台**：

1. **原语一：`Automations`（事件驱动的条件反射神经）**
2. **原语二：`Interfaces`（跨对象多态接口与多态 Action / 查询）**
3. **原语三：`Column-Level Security / Masking`（敏感字段列级权限脱敏）**
4. **原语四：`Column-wise MDO`（多源按列拼装）与 `Timestamp LWW`（冲突裁决策略 2）**
5. **放大器五：`Workshop Lite`（`open_ontology/workshop.py` —— 传入任意 `OntologyRuntime` 即可在浏览器打开的通用交互式 Web 作战指挥台）**

---

## 2. 四大核心原语详细规格

### 2.1 原语一：`Automations`（事件驱动条件反射，`spec.py` & `runtime.py`）

- 在 `OntologySpec` 上新增装饰器 `@spec.automation(name, watch_object_type, condition, description="")`：
  - `watch_object_type: type[ObjectType] | str`：监听的目标对象类型；
  - `condition: Callable[[ObjectType], bool]`：触发谓词（例如 `lambda sku: sku.canonical_spec_id is None` 检测到未归并黑户散码，或 `lambda order: order.status == "delayed"`）；
  - 处理函数签名：`def handler(runtime: OntologyRuntime, obj: ObjectType, actor: Actor) -> dict[str, Any] | None`，可在内部自动调用 `runtime.call_function(...)` 或 `runtime.submit_action(...)`（包括触发 `PENDING_APPROVAL` 审批单）。
- 在 `OntologyRuntime` 上新增：
  - `evaluate_automations(actor: Actor | None = None) -> list[dict[str, Any]]`：扫描受监听对象，对满足 `condition` 且尚未被同一 `(automation_name, object_id, object_version)` 触发过的对象执行自动化处理器，并记录触发历史（防同一版本重复死循环触发！）。
  - 支持在 `put_object` / `load_snapshot` / `submit_action` 后通过 `auto_trigger=True` 或显式调用 `evaluate_automations()` 运行。

### 2.2 原语二：`Interfaces`（接口多态，`spec.py` & `runtime.py`）

- 新增 `InterfaceType` 声明：`InterfaceType(name: str, required_fields: tuple[str, ...], description: str = "")`；
- `ObjectType` 新增可选类属性 `__interfaces__: ClassVar[tuple[str, ...]] = ()`：
  - 在 `spec.object_type(cls)` 注册时，自动校验该 `ObjectType` 是否包含所声明接口的全部 `required_fields`（若缺失字段则抛 `TypeError` 或 `OntologyError`）。
- `OntologyRuntime` 新增多态查询：
  - `query_interface(interface: str | InterfaceType, actor: Actor, filters=None) -> list[ObjectType]`：跨所有实现了该接口的 `ObjectType` 联合查询并返回可见对象实例列表！

### 2.3 原语三：`Column-Level Masking`（敏感字段列级权限脱敏，`spec.py` & `runtime.py`）

- `ObjectType` 新增可选类属性 `__masked_fields__: ClassVar[dict[str, tuple[str, ...]]]`：
  - 键为敏感字段名（如 `"unit_price_ton"` 或 `"cost_cents"`），值为允许查看明文的角色元组（如 `("finance", "buyer", "admin")`）；
  - 提供可选默认掩码值 `__mask_defaults__: ClassVar[dict[str, Any]]`（若未指定则整数/浮点数脱敏为 `0` 或 `0.0`，字符串脱敏为 `"***MASKED***"`，`Optional` 字段脱敏为 `None`）。
- 在 `runtime.get_object`、`runtime.query_objects`、`runtime.search_set`、`runtime.get_linked`、`runtime.query_interface` 返回给调用者 `actor` 时：
  - 若 `actor.role` 不在该敏感字段的允许角色列表中（且 `actor.role != "system"`），自动将返回对象副本上的该字段替换为脱敏掩码值（底层数据库存储不受影响）！

### 2.4 原语四：`Column-wise MDO`（多源按列拼装）与 `Timestamp LWW`（冲突裁决策略 2）

1. **`load_mdo_snapshots(object_type: type[ObjectType] | str, sources: list[dict[str, Any]])`**：
   - 支持将来自多个不同数据源（如 ERP 表提供价格列、CRM 表提供客户等级列、WMS 表提供库存列）的局部列字典列表，按主键自动按列合并（Column-wise Join）后装入 `ontology_objects` 并重盖 `ontology_overlay`！
2. **冲突裁决策略 2（`conflict_strategy="user_edits_win" | "most_recent_value"`）**：
   - `load_snapshot(..., conflict_strategy: str = "user_edits_win", source_timestamps: dict[tuple[str, str], str] | None = None)`：
   - 默认 `"user_edits_win"`（即 Palantir OSv2 Strategy 1：`ontology_overlay` 恒胜）；
   - 当指定 `conflict_strategy="most_recent_value"`（即 Palantir OSv2 Strategy 2）且传入 `source_timestamps={(object_type, object_id): utc_iso_ts}` 时：比对 `source_ts` 与 `ontology_overlay.updated_at`——若数据源的 UTC 时间戳严格晚于 `overlay.updated_at`，则清除过期的 `overlay` 补丁（或采用源系统的最新值）；若 `overlay.updated_at` 更新，则继续保留 `overlay` 补丁！

---

## 3. 放大器五：通用交互式 Web 作战指挥台（`open_ontology/workshop.py`）

在 `open_ontology/workshop.py`（升级并扩展现有 `http_server.py`）实现 `WorkshopServer`：
- 传入任意 `OntologyRuntime`，不仅提供 JSON API，还在根路径 `GET /` 直接渲染一个**零依赖、深色科技风的通用交互式 Palantir Workshop Lite Web 作战指挥台（HTML + 原生 JS）**：
  - 顶部显示当前本体注册的全部 `ObjectTypes`、`Interfaces`、`LinkTypes`、`Functions`、`Actions`、`Automations` 概览；
  - 左侧支持切换查看任意 `ObjectType` 的实时对象列表、点击触发 `simulate_action`（干跑预览 `ObjectDiff`）、`submit_action`、`approve_action`（批准待办）、`revert_action`（时光机撤回）以及一键触发 `evaluate_automations`；
  - 右侧实时展示 `ontology_action_runs` 审计账本（`COMMITTED / PENDING_APPROVAL / REJECTED / REVERTED`）与 `state_fingerprint`。

---

## 4. 验收测试要求（`tests/test_osv2_parity.py`）

新增 `tests/test_osv2_parity.py`（至少 12 条测试），全面验证：
1. `Automations`：新对象入库触发条件反射自动执行动作，且同一版本不会重复死循环触发；
2. `Interfaces`：多对象类型实现同一接口，缺失接口必填字段报错，`query_interface` 跨类型多态返回并遵守 RLS；
3. `Column-Level Masking`：非授权角色读取对象时敏感字段（如底价）被自动脱敏，授权角色（如 `finance`）读取到真实明文；
4. `Column-wise MDO`：两个不同数据源按主键按列拼装为完整对象；
5. `Conflict Strategy 2 (most_recent_value)`：当外部源 UTC 时间戳更新时覆盖旧 `overlay`，当 `overlay` 时间戳更新时保留 `overlay`；
6. `WorkshopServer`：启动服务后 `GET /` 返回完整交互式 HTML 作战台，`POST /api/simulate`、`POST /api/submit`、`POST /api/automations/run` 正常工作。
