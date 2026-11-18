# 任务看板领域模型规范 (M1 `task_board`)

> **元模型定位（OMG MOF M1）**：
> * **M2 平台底座**：遵循 [docs/ARCHITECTURE.md](ARCHITECTURE.md) 定义的通用元模型、沙箱铁笼与切面规范；
> * **M1 领域模型（`task_board`）**：在 `examples/kanban.py` 中定义的特定业务领域任务看板本体（通用多角色协作看板，非平台自举机制），包含固定 6 态流转、依赖 DAG、WIP 上限与 Actor/Member 身份绑定不变量。

---

## 1. M1 `task_board` 领域模型契约

### 1.1 核心对象类型、对象属性与聚合根

* **聚合根声明**：`Board` 显式声明 `__aggregate_root__ = True`，是工作空间内看板业务的唯一聚合根类型；其余从属对象不声明为根，通过 `ObjectProperty` 声明以下领域实体与关系：
  * `Board`（主键 `board_key`）：承载看板标题、租户（RLS 标签）与创建信息。
  * `Member`（主键 `member_id`）：团队成员角色（`developer` / `tester` / `reviewer` / `coordinator`）、在办上限 `wip_limit: int = 1` 与累计完成数 `completed_cards`；忙闲状态由当前 `doing` 卡片数实时派生，不冗余存储 `availability` 或 `active_card`。
  * `Card`（主键 `card_id: AutoID`）：工作项卡片，采用固定 6 态生命周期 `status: Literal["todo", "ready", "doing", "done", "blocked", "triage"]`（状态语义与允许流转收口于 `STATUS_SPECS`）；通过 `board: ObjectProperty[Board]`、`assignee: ObjectProperty[Member] | None` 与 `waits_for: ObjectProperty[tuple[Card, ...]]` 声明归属看板、承接成员与前置依赖，未满足前置时驻留 `todo`，前置全部完成后自动或通过 `promote_children` 提升至 `ready`。
  * `CardComment`（主键 `comment_id`）：评审/协调评论，通过 `CardComment.card`（`ObjectProperty`）关联所属卡片。

### 1.2 核心领域不变量

1. **依赖无环与前置门控**：`create_card` 禁止自依赖、跨板依赖与成环依赖（`DEPENDENCY_CYCLE`）；前置卡片未全部处于 `done` 之前，禁止认领或指派。
2. **WIP 上限与 Actor/Member 身份绑定**：成员在办 `doing` 卡片数达到 `wip_limit` 时拒绝新认领/指派（`WIP_LIMIT_EXCEEDED`）；执行角色调用 `claim_card` / `record_outcome` 时，`ctx.actor.actor_id` 必须与目标 `member_id` 一致（`ACTOR_MEMBER_MISMATCH`），管理角色不受此限。
3. **禁止绕行进入完成态**：`move_card`、`claim_card`、`release_claim`、`unblock_card` 与非 `completed` 结果均禁止直接落入 `done`（`COMPLETION_REQUIRES_RECORD_OUTCOME`）。
4. **调度与租约分离**：世界模型只守不变量、不内嵌自动派单调度器；上游 Agent 通过只读函数 `eligible_cards` / `board_overview` / `card_briefing` 自主发现任务后调用 `ActionDef`，外部超时控制面通过 `release_claim` 受控释放滞留认领。
