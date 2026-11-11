# TK-ONTO-020 · 透明 Aspect 执行边界第一阶段

## 1. 目标与执行依据

用户已确认 `Agent → Ontology → Tool` 主干，并授权启动 Aspect 实施。按 `docs/plans/2026-11-12-aspect-execution-foundation.md` 的四个任务实施，整体约束见 `docs/plans/2026-11-12-transparent-aspect-architecture.md`。

本轮只建立内部执行边界基础，不是完整 AOP 框架，不接入 Agent SDK，不新增原始 Tool 的公开 HTTP 路由，不宣称完整血缘/可靠外部执行已经实现。

## 2. 隔离与修改范围

- 唯一施工目录：`/Users/guci/.config/superpowers/worktrees/open-ontology/aspect-foundation`，Git 分支 `feat/aspect-foundation`，基线 `2341b04`。
- 程序员可新增：`open_ontology/execution/__init__.py`、`open_ontology/execution/fabric.py`、`tests/test_execution_fabric.py`。
- 必要时可新增该 execution 子包的内部辅助文件，不在顶层 `open_ontology/__init__.py` 导出；保持第一阶段最小。
- 仅可更新整体架构文档的执行状态，不改已确认的设计、计划和本规格/工作流。日志、红绿证据及差异哈希写批准临时目录 `.../T/opencode/tk020_evidence/`。
- 禁止修改既有源码、既有测试、业务示例、业务模型、视频源码/产物、依赖配置和所有旧 workflow。
- 禁止跳回原 main 施工、reset/checkout 丢弃文件、自行合并、提交、推送或关闭 `8765`/`8767`。本地测试资产已独立复制，不能重新生成视频或重建/改种子数据库。

## 3. 必须实现的契约

### 3.1 内部能力绑定

`ExecutionFabric.bind(capability, implementation)` 注册受控 Tool 实现。未知能力调用拒绝，重复绑定拒绝，不同 Fabric 实例同名能力互不串台。不把 capability 字符串当作已认证权限，不宣称第一阶段内部 API 能阻止恶意 Agent 绕过治理。

本阶段值契约限于普通 JSON 业务参数和普通返回值。引用计划的完整例子，不机械扩展到任意 Python 对象/异步函数/外部网络工具。

### 3.2 透明可选观察

`invoke(capability, params, CallContext)` 内部调用一次 Tool。不可变上下文/记录保持请求身份、实例、operation_id、mode、attempt 关联。

- 不向参数注入技术 key，不让观察者接触并修改原始业务参数/结果。
- 观察者只得到不可变摘要记录（started/succeeded/failed，输入输出摘要，异常类型）；原始异常消息或参数不出现在日志里。
- 原参数的嵌套结构不被 Tool 修改反向污染（参数副本）；结果/异常保留真实对象与意义，观察者开/关等价。
- 可选观察者抛出普通 Exception 时，不能覆盖 Tool 成功结果或原异常，其他观察者仍按注册顺序收到记录；诊断不带敏感内容。
- 输入验证/摘要失败发生在 Tool 执行之前；工具完成后摘要失败不得制造可重试的“假失败”。测试非 JSON 值、无效数值/循环等本阶段实际支持的边界，并文档化，不无故增加复杂序列化。
- 不隐式重试；每个 invoke 的工具调用计数为 1 或验证拒绝时为 0。
- 不提供控制型 around hook，不让 Aspect 操作领域规则。
- 本阶段只保证普通 API 使用下的值不可变/非干涉，不宣称 Python 同进程插件可抵御恶意代码、反射、闭包副作用或网络 IO。

### 3.3 等价性与并发

以受控假工具验证不同绑定、正常返回、抛出异常、可选观察者失败、嵌套参数副本、不注入技术字段、上下文不可变、多实例并发关联正确及观察者开关等价。

观察模式不等于安全沙盘：mode 只标明调用场景；第一阶段无真实写入绑定，因此不声称 mode=simulate 自动阻断任意恶意工具。后续真实沙盘隔离另立工单。

## 4. 测试顺序与验收

1. 严格测试先行：Task 1 能力测试红→绿，再 Task 2 观察契约红→绿，再 Task 3 等价/隔离边界。保存实际失败原因，不能先写完整实现后声称 TDD。
2. 新专项：`/Users/guci/miniforge3/bin/python3 -m pytest -v tests/test_execution_fabric.py`。
3. 既有专项：`TK019_BROWSER=firefox /Users/guci/miniforge3/bin/python3 -m pytest -q tests/test_command_center_presentation.py tests/test_command_center.py tests/test_osv2_parity.py tests/test_dev_factory.py`。
4. 全仓：`TK019_BROWSER=firefox /Users/guci/miniforge3/bin/python3 -m pytest -q`；禁止用无 Playwright 环境跳过浏览器用例，不从历史条数推导本轮结果。
5. 开工前记录 `git diff` 与所有既有源码/测试哈希（架构师已更新的计划/规格/工作流不是程序员改动）；交卷前核对未经授权文件不变，并汇报新增/修改文件与实际退出码。
6. 汇报已实现能力与未实现项，特别是未接入真实 Ontology→Tool 路径、无持久化证据、无强制审计、无幂等/重试/缓存，不夸大为可生产可靠执行。

## 5. 基线

架构师在隔离工作树独立执行全仓：234 passed in 75.12s，exit 0，零 skip。新模块尚未实施。原 main 干净，原演示服务不修改。
