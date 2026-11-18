---
name: ontology-worker
description: 通用本体世界状态上岗协议：让外部 Agent（如 CodeBuddy）通过 OpenAPI 3.1 自发现世界中的对象、查询函数与受控动作，以绑定 Actor 身份完成领单、执行与结果回写，无需任何专用适配器。
---

# Ontology Worker 上岗协议（基于 OpenAPI 3.1 自发现）

本 Skill 不写死任何具体业务动作或状态机字段——当前世界里有哪些对象、只读查询函数和允许你执行的动作，全部实时以服务端的 `openapi.json` 为准。

## 1. 上岗凭据（由调用方通过环境变量或提示词注入）

开始工作前确认以下上下文：

- `WORLD_URL`：世界状态根路径，例如 `http://127.0.0.1:8765/api/worlds/board-prod`
- 身份认证（二选一，优先使用 Bearer Token）：
  - **Token 模式**（推荐）：设置 `ACTOR_TOKEN`，所有请求携带
    `Authorization: Bearer $ACTOR_TOKEN`
  - **本地联调 Header 模式**：未配置 Token 时，携带
    `X-Actor-Id: $ACTOR_ID` 与 `X-Actor-Role: $ACTOR_ROLE`（若存在行级密级限制，可附 `X-Clearance-Tags`）

## 2. 第一步：拉取角色裁剪后的 OpenAPI 契约

先获取自己可见、可调用的全部资源与动作 Schema（带 Token 时服务端会自动按绑定角色过滤）：

```bash
curl -fsS -H "Authorization: Bearer $ACTOR_TOKEN" \
  "$WORLD_URL/openapi.json"
# 或本地 Header 模式：
curl -fsS -H "X-Actor-Id: $ACTOR_ID" -H "X-Actor-Role: $ACTOR_ROLE" \
  "$WORLD_URL/openapi.json"
```

从返回的 `paths` 与 `components.schemas` 中自发现：
- `GET .../objects/{Type}` 与 `GET .../objects/{Type}/{id}`：读取当前世界状态中的业务对象（后端强制行级安全与列脱敏）；
- `POST .../functions/{fn}`：只读查询与语义聚合（如查找当前角色可认领的任务、读取任务详情简报）；
- `POST .../actions/{action}`：受本体不变量约束的写动作，请求体直接传对应 `components.schemas` 中的参数 JSON。

## 3. 标准作业闭环

1. **查询可领工作与上下文**：调用 OpenAPI 中的 `Functions` / `Objects` 端点获取待办与关联上下文（如依赖、历史评论），确认任务确实由你当前角色负责且前置已满足。
2. **正式认领 / 占坑**：调用对应的认领动作正式锁定任务，并确认返回 `run.status == "COMMITTED"`。
3. **沙盘预演（可选）**：对不确定是否会触犯不变量的写操作，先在动作路径后加 `?dry_run=true` 干跑检查 diff 与校验结果。
4. **本地编码 / 验证**：在分配的工作目录内完成实现、运行相关测试与检查，保留真实的产物路径与验证结果。
5. **汇报交单 / 异常上报**：无论完成、受阻还是失败，都必须调用 OpenAPI 中对应的结果登记 / 评论动作写回世界状态（必要时附 `?run_id=<唯一键>` 保证重试幂等）；禁止在未汇报状态的情况下直接结束。

## 4. 必须遵守的不变量与安全铁律

- **禁止绕过世界状态 API**：绝对不要直接读写 `.instances/*.sqlite3` 或任何后端数据库文件；所有状态迁移与业务审计必须经过 `Actions` 端点裁决。
- **严禁冒用身份**：只能操作与自身 `$ACTOR_ID` / `$ACTOR_TOKEN` 绑定的工位与成员席位；切勿试图伪造他人的 `member` / `actor_id` 或越权调用不属于自己角色的动作。
- **如实汇报**：不得把未通过的测试、猜想的结果或缺失产物写进交付摘要。
