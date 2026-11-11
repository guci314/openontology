# OpenOntology 规格说明书（TK-ONTO-017 · 业务纯洁的研发团队与工单本体 SoftwareFactoryOntology + 跨平台机械臂适配器）

## 1. 背景与第一性原理（彻底消除研发编排器的技术污染）

此前用于编排多角色研发流水线的 `workflow_conductor.py` 存在严重的 **IT 技术污染**：
- 把「创建程序员、立项工单、派单、交卷、测试审查、架构终审、返工」等**纯研发管理业务概念（PIM）**，与 `opencode_http`、`session_id (ses_xxx)`、`codebuddy -p -r`、端口号、JSONL 文件路径、API 429 限流重试等**底层工具宿主细节（PSM）**死死耦合在一起；
- 导致一旦某个宿主（如 OpenCode 第三方渠道或腾讯 CodeBuddy 服务器）限流，或者需要将 Skill 移植到 **Hermes Agent、CodeBuddy、Claude Code、Codex** 等其他 Agent 环境，就必须重写或翻译逻辑。

本工单用 `OpenOntology` 完成一次真正的 **自举（Self-Hosting / Dogfooding）**：
1. **构建 100% 业务纯洁的《软件研发工厂本体》（`open_ontology/dev_factory.py`）**：只负责管理 **程序员（`Engineer`）、工单（`WorkTicket`）、交付物（`Deliverable`）** 以及完整的流转与护栏（招募、立项、派单、交卷、评审、打回返工、无损换人），**内部严禁出现任何 `opencode`、`codebuddy`、`hermes`、`session_id`、`port`、`http`、`cli`、`rate_limit` 等 IT 技术词汇**！
2. **构建外围可插拔机械臂驱动层（`open_ontology/harness_drivers.py`）**：将 `CodeBuddyDriver`、`OpenCodeDriver`、`HermesDriver`、`CallableDriver` 封装为可随时热插拔、支持限流自动故障转移（Failover）的底层执行机械臂，消费 `SoftwareFactoryOntology` 中的工单状态并自动回填执行结果。

---

## 2. 纯业务本体层规格（`open_ontology/dev_factory.py` —— 100% 零技术污染）

### 2.1 纯业务对象（`ObjectType`，全部 `__owned__ = True`）

1. **`Engineer`（研发成员：架构师 / 程序员 / 测试员 / 审计官）**
   - `engineer_id: str`（主键，如 `"ENG-ARCH-01"`, `"ENG-CODER-01"`, `"ENG-TESTER-01"`）
   - `name: str`（显示名，如 `"首席架构师"`, `"核心程序员A"`）
   - `role: str`（`"architect"` | `"coder"` | `"tester"` | `"auditor"`）
   - `specialty: str = "general"`（专长领域，如 `"kernel"`, `"video"`, `"integration"`）
   - `availability: str = "IDLE"`（`"IDLE"` | `"BUSY"` | `"UNAVAILABLE"`）
   - `active_ticket_id: Optional[str] = None`
   - `completed_tickets: int = 0`
   - `Security_Classification: str = "INTERNAL"`

2. **`WorkTicket`（研发工单）**
   - `ticket_id: str`（主键，如 `"TK-ONTO-017"`）
   - `title: str`
   - `spec_uri: str`（规格说明书路径或标识）
   - `isolated_scope: str`（该工单允许修改的隔离目录/范围，如 `"open_ontology/"` 或 `"video_saas/"`）
   - `stage: str = "OPEN"`（状态机：`"OPEN"` | `"CODING"` | `"TESTING"` | `"ARCH_REVIEW"` | `"DONE"` | `"BLOCKED"`）
   - `require_testing: bool = False`（交卷后是否先经过 `"TESTING"` 再进 `"ARCH_REVIEW"`）
   - `assignee_id: Optional[str] = None`（当前负责的程序员 ID）
   - `reviewer_id: Optional[str] = None`（当前负责评审的测试员/架构师 ID）
   - `rework_round: int = 0`
   - `max_rework: int = 5`
   - `instructions: str = ""`
   - `latest_submission: Optional[str] = None`
   - `latest_feedback: Optional[str] = None`
   - `verdict: str = "PENDING"`（`"PENDING"` | `"PASS"` | `"FAIL"`）
   - `Security_Classification: str = "INTERNAL"`

3. **`Deliverable`（工单交付物）**
   - `deliverable_id: str`（主键）
   - `ticket_id: str`
   - `engineer_id: str`
   - `round_index: int`
   - `artifact_uri: str`
   - `summary: str`
   - `verified: bool = False`
   - `Security_Classification: str = "INTERNAL"`

### 2.2 纯业务关系（`LinkType`，全部 `owned = True`）

- `ticket_assigned_to`: `WorkTicket` $\to$ `Engineer`
- `ticket_reviewed_by`: `WorkTicket` $\to$ `Engineer`
- `ticket_deliverables`: `WorkTicket` $\to$ `Deliverable`

### 2.3 纯业务动作（`@spec.action`）与符号护栏（`InvariantError`）

1. **`recruit_engineer`（创建/招募研发成员）**
   - 参数：`engineer_id`, `name`, `role`, `specialty="general"`
   - 护栏：`role` 必须属于 `{"architect", "coder", "tester", "auditor"}`，否则抛 `InvariantError("INVALID_ENGINEER_ROLE")`。

2. **`open_ticket`（创建研发工单）**
   - 参数：`ticket_id`, `title`, `spec_uri`, `isolated_scope`, `instructions=""`, `max_rework=5`, `require_testing=False`
   - 护栏：`max_rework >= 1`，`isolated_scope` 非空，创建初始状态为 `"OPEN"` 的 `WorkTicket`。

3. **`dispatch_ticket`（派发工单给程序员）**
   - 参数：`ticket_id`, `engineer_id`, `instructions_override: Optional[str] = None`
   - **三大符号护栏**：
     - 目标 `Engineer` 必须 `role == "coder"`，否则抛 `InvariantError("ASSIGNEE_MUST_BE_CODER")`；
     - 目标 `Engineer.availability` 必须为 `"IDLE"`，否则抛 `InvariantError("ENGINEER_NOT_IDLE")`；
     - **目录冲突锁（防止并发写坏代码！）**：检查当前所有处于 `stage == "CODING"` 的其它工单，若存在另一张在途工单与本工单的 `isolated_scope` 相同或存在父子目录重叠，立即抛 `InvariantError("WORKSPACE_SCOPE_CONFLICT")`！
   - 状态变更：`WorkTicket.stage = "CODING"`，`assignee_id = engineer_id`；`Engineer.availability = "BUSY"`，`active_ticket_id = ticket_id`；建立 `ticket_assigned_to` 关系边。

4. **`submit_work`（程序员交卷）**
   - 参数：`ticket_id`, `engineer_id`, `summary`, `artifact_uri=""`
   - 护栏：工单必须处于 `"CODING"` 且 `assignee_id == engineer_id`，否则抛 `InvariantError("INVALID_SUBMISSION_STATE")`；
   - 状态变更：
     - 创建 `Deliverable` 对象并挂载 `ticket_deliverables` 边；
     - 释放程序员：`Engineer.availability = "IDLE"`，`active_ticket_id = None`；
     - 工单流转：若 `require_testing=True` 则 `stage = "TESTING"`，否则 `stage = "ARCH_REVIEW"`，`latest_submission = summary`，`verdict = "PENDING"`。

5. **`review_ticket`（测试员或架构师裁决）**
   - 参数：`ticket_id`, `reviewer_id`, `verdict`（`"PASS"` | `"FAIL"`），`feedback=""`
   - 护栏：
     - 若 `stage == "TESTING"`，要求 `reviewer.role in ("tester", "architect")`；若 `stage == "ARCH_REVIEW"`，要求 `reviewer.role in ("architect", "auditor")`；
   - 状态变更：
     - 若 `verdict == "PASS"`：
       - 当处于 `"TESTING"` 时推进到 `"ARCH_REVIEW"`；
       - 当处于 `"ARCH_REVIEW"` 时推进到终态 **`"DONE"`**，将最新 `Deliverable.verified = True`，并为原 `assignee` 的 `completed_tickets += 1`！
     - 若 `verdict == "FAIL"`：
       - 若 `rework_round + 1 > max_rework`：将工单置为 `"BLOCKED"`（或抛 `InvariantError("MAX_REWORK_EXCEEDED")`）；
       - 否则：`rework_round += 1`，`latest_feedback = feedback`，`verdict = "FAIL"`；若原程序员目前 `"IDLE"` 则自动重新挂载回 `"CODING"` 并占用该程序员，若原程序员不可用则置回 `"OPEN"` 等待改派。

6. **`reassign_ticket`（无损换人 / 故障交接）**
   - 参数：`ticket_id`, `new_engineer_id`, `reason=""`
   - 作用：当原程序员失联、请假（或外部机械臂遇到服务器限流将原程序员标为 `"UNAVAILABLE"`）时，将工单及其全部历史上下文（`spec_uri`、`rework_round`、`latest_submission`、`latest_feedback`）原子交接给新的 `"IDLE"` 程序员，原程序员置为 `"UNAVAILABLE"` 并清空 `active_ticket_id`，新程序员置为 `"BUSY"` 并接手 `"CODING"`，**实现跨 Agent / 跨服务器 100% 零失忆接力**！

### 2.4 只读看板函数（`@spec.function`）

- **`factory_board_summary(runtime)`**：返回当前所有程序员忙闲状态、各阶段工单统计、在途目录锁列表。
- **`build_engineer_briefing(runtime, ticket_id)`**：纯粹从 `WorkTicket` 与历史 `Deliverable` 拼装出给程序员的结构化任务简报（含规格书路径、隔离目录、当前返工轮次、上一轮交卷摘要、架构师打回修改意见），**不依赖任何外部 LLM 会话记忆**！

---

## 3. 外围可插拔机械臂驱动层（`open_ontology/harness_drivers.py` —— PSM 适配层）

在纯业务本体之外，提供轻量的机械臂执行器 `FactoryOrchestrator` 与 `HarnessDriver` 协议：
- `HarnessDriver` 接口：`execute_briefing(engineer_id: str, briefing: str, workspace_dir: str) -> str`
- 内置四种可插拔 Driver：
  1. `CallableHarnessDriver`：接收任意 Python 函数（用于单元测试、内嵌 Agent 或自定义宿主）；
  2. `CodeBuddyCLIDriver`：封装 `codebuddy -p -y` CLI 调用；
  3. `OpenCodeHTTPDriver`：封装 OpenCode HTTP Session 调用；
  4. `HermesCLIDriver` / `GenericCLIDriver`：支持通过命令行模板接入 Hermes Agent、Claude Code、Codex 等任意 CLI Agent。
- **自动限流切换（Failover Router）**：
  - `FailoverPoolDriver` 或 `FactoryOrchestrator.step_ticket(ticket_id)`：当主机械臂抛出限流/连接异常（`HarnessRateLimitError` / `HarnessUnavailableError`）时，自动调用本体的 `reassign_ticket` 将工单无缝交接给绑定了备用机械臂的另一名 `Engineer` 继续执行，Ontology 账本完整记录交接全过程！

---

## 4. 验收测试（`tests/test_dev_factory.py`）

编写 `tests/test_dev_factory.py`（至少 12 条测试），全面验证：
1. **100% 业务纯洁性静态审计**：扫描 `open_ontology/dev_factory.py` AST 与源码，断言零 `opencode`、零 `codebuddy`、零 `hermes`、零 `session_id`、零 `http`、零 `subprocess`；
2. 招募程序员、创建工单、正常派单、交卷、测试通过、架构终审 `DONE` 全生命周期；
3. **并发目录隔离锁**：两张工单若 `isolated_scope` 冲突（如都是 `"open_ontology/"` 或父子包含），同时派单进入 `CODING` 时必抛 `InvariantError("WORKSPACE_SCOPE_CONFLICT")`；不同目录（如 `"open_ontology/"` 与 `"video_saas/"`）则允许并行派给两个程序员；
4. 架构师或测试员打回 `FAIL` 自动递增 `rework_round` 返工，超过 `max_rework` 自动熔断拦截；
5. **无损换人与抗限流 Failover**：模拟程序员 1 遭遇限流离线，调用 `reassign_ticket`（或通过 `FactoryOrchestrator` 自动 Failover）交接给程序员 2，验证 `build_engineer_briefing` 完整保留前序返工反馈与交卷记录，程序员 2 顺利接力完成 `DONE`；
6. 接入 `WorkshopServer`：验证 `WorkshopServer(factory_runtime)` 可直接展示研发工厂的 `Engineer`、`WorkTicket`、`Deliverable` 实时看板。
