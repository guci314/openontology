# 通用 Outbound Webhook 订阅机制设计方案

**目标：** 为 OpenOntology 世界状态（World）建立领域无关、订阅者无关（CodeBuddy、OpenCode、人类通知网关、外部系统一视同仁）的 **Outbound Webhook 事件订阅与异步投递机制**，并附一个独立于内核之外的示例外部 Listener 脚本用于唤醒本地空闲 `codebuddy`。

**架构定位与硬边界（遵循 [ARCHITECTURE.md](file:///Users/guci/work/open-ontology/docs/ARCHITECTURE.md)）：**
1. **严格 Push 唤醒 + Pull 领单分离**：Outbound Webhook 只做**状态变更通知（Wake-up Signal）**，绝不在通知发出前替外部执行器“预指派/占坑”。外部 Agent 醒来后凭自身 `Actor` 凭据调用 `/api/worlds/{world_id}/...` OpenAPI 读取与认领。
2. **零订阅者特化**：内核、`webhooks.py` 与 `workshop.py` 不得出现任何具体 Agent / 产品名称（如 `CodeBuddy`、`OpenCode`、`Slack`、`飞书`）或私有会话协议，也不得在服务端 `subprocess` 拉起外部进程。
3. **RLS 与列脱敏不可被 Webhook 绕过**：事件信封默认**不直吐对象明文敏感列**（`after` 全量属性不直接外发），只发送变更索引摘要与可安全用于过滤/路由的状态线索；订阅者持自身 `Bearer Token` 拉取对象详情时，由服务端读出口强制执行 RLS 与 `__masked_fields__`。
4. **保持内核分层与预算**：
   - `open_ontology/runtime.py` 维持 `<= 1400` 行预算，复用已有的 `add_post_commit_hook(hook)` 挂接机制；
   - `open_ontology/webhooks.py` 保持传输层无关（不直接导入 `urllib` / `http.server`），只负责订阅模型、匹配过滤器、信封构造、HMAC-SHA256 签名与后台异步队列调度；
   - `open_ontology/workshop.py` 作为纯 HTTP 传输边界注入 `urllib.request` 发送器，并暴露 `/api/worlds/{world_id}/webhooks` 管理路由及 `describe_openapi` 投影；
   - `open_ontology/aspects.py` 补齐 `ApprovalGateAspect.approve_action` 与 `revert_action` 提交后的 `runtime._notify_post_commit(...)`，避免审批生效与回滚漏发通知。

---

## 1. 数据模型与通用契约（`open_ontology/webhooks.py`）

### 1.1 `WebhookFilter`
所有条件为可选 AND 匹配；未配置字段视为通配：
- `actions: tuple[str, ...]` —— 仅匹配指定 `action_type`（例如 `("create_card", "unblock_card")`）。
- `object_types: tuple[str, ...]` —— 仅当 diff 至少涉及其中一个 `object_type` 时匹配。
- `kinds: tuple[Literal["create", "update", "delete"], ...]` —— 变更类型过滤。
- `field_equals: dict[str, Any]` —— 匹配 `object_changes[].after` 中全部指定字段均等于目标值的变更（例如 `{"status": "ready"}` 或 `{"status": "triage"}`）。匹配仅在内存过滤阶段执行，原始字段值不外泄至未授权荷载。

### 1.2 `WebhookSubscription`
- `sub_id: str`（自动生成或调用方指定）
- `world_id: str`
- `target_url: str`（创建时校验 scheme 仅允许 `http` / `https`，拒绝 `file://` 等非网络方案）
- `filter: WebhookFilter`
- `secret: str = ""`（只写不明文回显：列表/详情接口返回 `has_secret: bool`，不返回原文）
- `description: str = ""`
- `enabled: bool = True`
- `created_by: str = ""`
- `created_at: str`

### 1.3 标准事件信封（`OntologyEvent` JSON）

```json
{
  "event_id": "evt-9f2a1c4b",
  "event_type": "world.action.committed",
  "world_id": "board-prod",
  "run_id": "API-0028-4f1a92",
  "action": "create_card",
  "actor": {"actor_id": "board-manager-01", "role": "board_manager"},
  "occurred_at": "2026-11-16T14:20:00+00:00",
  "changes": [
    {
      "kind": "create",
      "object_type": "Card",
      "object_id": "C-107",
      "matched_fields": {"status": "ready"}
    }
  ]
}
```

- 请求头：
  - `Content-Type: application/json; charset=utf-8`
  - `X-Ontology-Event: world.action.committed`
  - `X-Ontology-World: <world_id>`
  - `X-Ontology-Delivery: <delivery_id>`
  - 若配置了 `secret`：`X-Ontology-Signature-256: sha256=<hex_hmac_sha256>`（对原始 UTF-8 JSON bytes 计算）。

### 1.4 异步投递与事务隔离（`OutboundWebhookDispatcher`）
- `hook(actor, run)` 在事务提交**之后**触发（`submit_action`、`approve_action`、`revert_action`）。
- 投递逻辑与事务完全解耦：
  - `on_post_commit(world_id, actor, run)` 仅在内存中匹配订阅，为命中的订阅构造事件信封后放入有界 `queue.Queue` 立即返回（微秒级），**绝不阻塞主 HTTP 请求，失败也不回滚已提交事务**。
  - 后台守护工作线程执行发送函数 `send_fn(url, headers, body_bytes) -> (status_code, detail)`，支持配置超时与有限次指数退避重试（默认 `max_retries=2`）。
  - 每次投递结果记入受容量上限约束的 `WebhookDelivery` 记录（`delivery_id`, `sub_id`, `event_id`, `world_id`, `status`: `"DELIVERED"` / `"FAILED"`, `attempts`, `http_status`, `error`, `delivered_at`）。

---

## 2. 服务端挂载与 HTTP API（`workshop.py` + `model_views.py`）

- **自动挂载**：`CommandCenterRegistry` 内置单一 `OutboundWebhookDispatcher`（由 `WorkshopServer` 在启动时注入 HTTP `urllib.request` 发送器，停止时优雅 `close()`）；向 `register_instance` / `fork_instance` 注册的每个 `runtime` 自动挂载 `add_post_commit_hook`。
- **HTTP 管理端点（按 `world_id` 隔离，遵循既有 Actor 鉴权）**：
  - `GET /api/worlds/{world_id}/webhooks` —— 返回订阅列表（`secret` 脱敏为 `has_secret`）
  - `POST /api/worlds/{world_id}/webhooks` —— 注册订阅，校验 `target_url` 仅限 `http/https`
  - `DELETE /api/worlds/{world_id}/webhooks/{sub_id}` —— 注销订阅
  - `GET /api/worlds/{world_id}/webhooks/{sub_id}/deliveries` —— 查询近期投递历史（排障/验收）
- **OpenAPI 投影**：在 `describe_openapi` 保留或补充说明上述通用 World 级 Webhook 路径（仅暴露给具权管理视角或在 World 元信息中可发现）。

---

## 3. 三类订阅者接入模式（零核心改造）

| 订阅者类型 | 接收方角色 | 收到标准事件后做什么 |
| :--- | :--- | :--- |
| **CodeBuddy（CLI）** | 示例外置守护脚本 `examples/webhook_codebuddy_listener.py` | 验证 HMAC 签名 → 门控/防重入判定 → 空闲时拉起 `codebuddy -p`（配合 `skills/ontology-worker/SKILL.md`）自己调 OpenAPI 查领并交单 |
| **OpenCode（常驻 Server）** | 外部极薄转发桥或 OpenCode Plugin | 收到同一个 POST 后，转发 `POST http://127.0.0.1:49374/api/session/{sessionID}/inbox`（或 `/prompt`），唤醒指定工位常驻 Session 自己走 OpenAPI |
| **人类订阅者** | 企微/飞书/Slack 机器人 Webhook 或前端通知网关 | 订阅审批挂起 / `status=triage` 等升级事件，推一条可读卡片给责任人，由人在指挥台完成审批或解阻 |

---

## 4. 示例外置 Listener 设计（`examples/webhook_codebuddy_listener.py`）

定位为**仓库外置消费示例**（不进 `open_ontology/` 包）：
1. **启动参数 / 环境变量**：
   - `--world-url`（默认 `http://127.0.0.1:8765/api/worlds/board-prod`）
   - `--listen`（默认 `127.0.0.1:8790`）与 `--public-url`（回调地址，默认由 `--listen` 推导）
   - `--actor-id` / `--actor-role`（默认 `M-DEV-01` / `developer`）或 `--actor-token`
   - `--admin-role` / `--admin-token`（用于向 `--world-url/webhooks` 注册订阅）
   - `--secret`（用于校验 `X-Ontology-Signature-256`）
   - `--skill`（默认 `skills/ontology-worker/SKILL.md`）
   - `--once` / `--dry-run-command`（便于自动化验收或本地干跑观测而不必每次真烧模型）
2. **并发防踩踏与边沿合并（关键工程细节）**：
   - 收到有效 Webhook 后立即返回 `202 Accepted`（不让服务端等待 `codebuddy` 执行数十秒超时）；
   - 本地维护线程锁 + `running` 状态 + `pending_wakeup` 标志：
     - 若当前**空闲**：置 `running = True`，在后台工作线程拉起 `codebuddy -p ... -y`，执行完毕后检查 `pending_wakeup`；
     - 若当前**正在忙**：只置 `pending_wakeup = True`，**不并发新开第二个进程**；当前进程完成后若 `pending_wakeup` 为真，自动再跑一轮自检是否有新可领任务。

---

## 5. 验收测试计划

1. **单元测试（`tests/test_webhooks.py`）**：
   - `WebhookFilter` 匹配：`actions` / `object_types` / `kinds` / `field_equals`；
   - 安全约束：荷载不包含未匹配敏感字段明文、`secret` 不在摘要序列化明文回显、非法 URL scheme（如 `file://`）被拒绝；
   - HMAC-SHA256 签名验证；
   - 失败重投与隔离：下游抛错或返回 HTTP 500 时，主事务已提交且绝不抛出到调用方，投递账本记录 `FAILED`；成功重投记录 `DELIVERED`。
2. **集成测试（`tests/test_command_center.py`）**：
   - 注册订阅到测试 Server → 调用 `create_card` → 本地 `ThreadingHTTPServer` 真实收到带正确 `X-Ontology-Signature-256` 签名的 POST 信封；
   - 验证 `ApprovalGateAspect.approve_action`（审批通过路径）也能触发 Outbound Webhook；
   - 验证 `DELETE /api/worlds/{world_id}/webhooks/{sub_id}` 退订后不再投递。
3. **示例 Listener 验收**：
   - 用 `--dry-run-command` 或模拟执行命令启动 `examples/webhook_codebuddy_listener.py` 的可测试处理类，验证签名校验、`202 Accepted` 响应与并发边沿合并（忙时置 `pending_wakeup` 不并发重复唤醒）。
