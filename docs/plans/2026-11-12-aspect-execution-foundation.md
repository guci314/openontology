# Transparent Aspect Execution Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 验证实例级工具替换与观察型 Aspect 的透明性，建立内部执行边界基础；不宣称已实现完整溯源、可靠外部执行或 Agent 写入口治理。

**Architecture:** 新建独立 `open_ontology/execution/` 基础设施子包，能力绑定与不可变技术调用上下文只存在于该子包。业务参数/结果不加入 Aspect 元数据，可选观察者仅接收不可变摘要记录；不提前增加控制型 around/retry 能力。与现有 Runtime 的真实治理路径接入另立第二阶段工单。

**Tech Stack:** Python 标准库、pytest；不引入 AOP 框架、数据库迁移或外部服务。

---

## 基线与范围

- 设计依据：`docs/plans/2026-11-12-transparent-aspect-architecture.md`。
- 上一轮已独立验证的项目基线为 234 passed；这是历史基线，不是本计划实施结果。
- 项目当前不是 Git 仓库，不创建虚构工作树或运行 git 提交。用限定文件范围与检查前后哈希保护既有工作。
- 暂不修改 `runtime.py`、`spec.py`、`dev_factory.py`、`harness_drivers.py`、业务示例、Workshop、视频目录或现有测试。
- 内部 `ExecutionFabric` 不加入 Agent SDK，不接到公开 HTTP/LLM 工具入口。未接入真实路径前，不声称 Agent 不能绕过工具已由本模块保证。
- 本阶段记录调用级摘要，不保存原始敏感参数，不宣称逐字段血缘；观察者失败策略只适用于可选观察者，不适用于强制合规证据。

## Task 1：定义并测试能力绑定

**Files:**
- Create: `open_ontology/execution/__init__.py`
- Create: `open_ontology/execution/fabric.py`
- Create: `tests/test_execution_fabric.py`

**Step 1 — Write failing tests.** 先写测试，验证未知能力拒绝、禁止重复绑定、不同 Fabric 的同名能力互不串台。以下测试是完整初始案例：

```python
import pytest
from open_ontology.execution.fabric import CallContext, ExecutionFabric


def context(instance="north"):
    return CallContext(instance_id=instance, operation_id="OP-1", actor_id="USER-1")


def test_binding_unknown_and_duplicate():
    fabric = ExecutionFabric()
    with pytest.raises(KeyError):
        fabric.invoke("stock.lookup", {}, context())
    fabric.bind("stock.lookup", lambda p: {"site": "north"})
    with pytest.raises(ValueError):
        fabric.bind("stock.lookup", lambda p: {"site": "other"})


def test_instance_bindings_are_separate():
    north, south = ExecutionFabric(), ExecutionFabric()
    north.bind("stock.lookup", lambda p: {"site": "north"})
    south.bind("stock.lookup", lambda p: {"site": "south"})
    assert north.invoke("stock.lookup", {}, context("north")) == {"site": "north"}
    assert south.invoke("stock.lookup", {}, context("south")) == {"site": "south"}
```

**Step 2 — Verify failure.** Run: `/Users/guci/miniforge3/bin/python3 -m pytest -v tests/test_execution_fabric.py`. Expected: 新模块尚不存在而失败，不是环境或依赖错误。

**Step 3 — Minimal implementation.** 先仅实现下文的 CallContext、bind 与无观察者 invoke 所需行为；Task 2 的观察逻辑必须等相应失败测试出现后再实现。

**Step 4 — Verify green.** 运行相同命令，确认当前两条测试通过，且无 IO。

## Task 2：透明观察、关联上下文与失败语义

**Files:**
- Modify: `open_ontology/execution/fabric.py`
- Modify: `tests/test_execution_fabric.py`

**Step 1 — Add failing tests.** 添加以下完整案例：

```python
from dataclasses import FrozenInstanceError


def test_observer_does_not_change_payload_result_or_context():
    seen = []
    original_result = {"stock": 7}
    payload = {"sku": "SKU-1", "nested": {"n": 1}}
    received = []

    def tool(p):
        received.append(p)
        p["nested"]["n"] = 2
        return original_result

    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", tool)
    ctx = context()
    result = fabric.invoke("stock.lookup", payload, ctx)
    assert result is original_result
    assert payload == {"sku": "SKU-1", "nested": {"n": 1}}
    assert set(received[0]) == {"sku", "nested"}
    assert [e.phase for e in seen] == ["started", "succeeded"]
    assert all(e.context is ctx for e in seen)
    assert not hasattr(seen[0], "payload")
    with pytest.raises(FrozenInstanceError):
        seen[0].phase = "tampered"


def test_tool_failure_is_not_wrapped_as_success():
    seen = []
    failure = RuntimeError("tool failed")

    def tool(p):
        raise failure

    fabric = ExecutionFabric(observers=(seen.append,))
    fabric.bind("stock.lookup", tool)
    with pytest.raises(RuntimeError) as exc:
        fabric.invoke("stock.lookup", {}, context())
    assert exc.value is failure
    assert [e.phase for e in seen] == ["started", "failed"]
    assert seen[-1].error_type == "RuntimeError"


def test_optional_observer_failure_preserves_success():
    def broken(event):
        raise RuntimeError("optional observer failed")

    fabric = ExecutionFabric(observers=(broken,))
    fabric.bind("stock.lookup", lambda p: {"stock": 7})
    assert fabric.invoke("stock.lookup", {}, context()) == {"stock": 7}
```

**Step 2 — Verify failure.** 运行专项测试，确认新观察契约因未实现而失败。

**Step 3 — Implement only the needed behavior.** 最小参考实现如下；公开值契约在第一阶段限定为 JSON 值，避免替任意 Python 对象作不可靠的隐式序列化。摘要失败不允许在工具已经执行后覆盖真实执行结果。

```python
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallContext:
    instance_id: str
    operation_id: str
    actor_id: str
    mode: str = "live"
    attempt: int = 1


@dataclass(frozen=True)
class Observation:
    context: CallContext
    capability: str
    phase: str
    input_digest: str
    output_digest: str | None = None
    error_type: str | None = None


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class ExecutionFabric:
    def __init__(self, observers=()):
        self._tools: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._observers = tuple(observers)

    def bind(self, capability, implementation):
        if capability in self._tools:
            raise ValueError("capability already bound")
        self._tools[capability] = implementation

    def _observe(self, observation):
        for observer in self._observers:
            try:
                observer(observation)
            except Exception:
                # Optional observers only. Do not include raw values or exception
                # messages in logs, which may contain sensitive business data.
                LOGGER.warning("optional execution observer failed")

    def invoke(self, capability, params, context):
        implementation = self._tools[capability]
        request = deepcopy(params)
        input_digest = digest(request)
        self._observe(Observation(context, capability, "started", input_digest))
        try:
            result = implementation(request)
        except Exception as exc:
            self._observe(Observation(
                context, capability, "failed", input_digest,
                error_type=type(exc).__name__,
            ))
            raise
        try:
            output_digest = digest(result)
        except (TypeError, ValueError):
            # Preserve the actual result; no observer may turn an already
            # successful external effect into a retry-inducing failure.
            output_digest = None
            LOGGER.warning("execution result digest unavailable")
        self._observe(Observation(
            context, capability, "succeeded", input_digest, output_digest,
        ))
        return result
```

**Step 4 — Verify green.** 运行所有当前专项测试。不得通过修改业务参数、吞掉 Tool 异常或删断言修复失败。

## Task 3：补足隔离与等价性验证

**Files:**
- Modify: `tests/test_execution_fabric.py`

每个行为先写独立失败测试，再最小实现：

1. 对同一受控 Tool，观察者开/关时参数值、返回值、异常以及调用次数相同。
2. 两个实例并行调用的记录与实例/operation_id 正确关联，不使用全局上下文。
3. 非法输入在 Tool 调用前失败；非 JSON 返回值摘要不可用时仍保留真实返回，不误标成工具失败。
4. 若未知能力或执行前输入验证失败尚未记录，明确测试其拒绝契约；不要宣称第一版覆盖完整治理拒绝审计。
5. Tool 无隐式重试；一个 invoke 只能执行实现一次。
6. 业务模块不导入 execution 子包；工具参数不带 context/aspect/source 等注入元数据。

只读工具与模拟工具在此阶段通过独立绑定验证。真实副作用隔离和可信身份只在后续接入真实治理路径时验收，不用假工具测试代替。

## Task 4：全回归与交付

**Files:**
- Update execution state: `docs/plans/2026-11-12-transparent-aspect-architecture.md`

1. 使用带 pytest、Playwright 的基础解释器运行：
   `/Users/guci/miniforge3/bin/python3 -m pytest -v tests/test_execution_fabric.py`
2. 运行既有专项：
   `/Users/guci/miniforge3/bin/python3 -m pytest -q tests/test_command_center_presentation.py tests/test_command_center.py tests/test_osv2_parity.py tests/test_dev_factory.py`
3. 运行全仓：`/Users/guci/miniforge3/bin/python3 -m pytest -q`。
4. 确认 Firefox 浏览器测试没有 skip；不得根据历史 234 条推断本轮条数。
5. 对允许范围之外的源码/测试做前后 SHA256 比对，确认未改动。
6. 汇报新文件、失败先行证据、实际测试数字、公开/内部边界与未实现项。不宣称已获得完整溯源或可靠外部事务。

本阶段无真实 ERP/支付调用，无指挥台重启，无数据库迁移。项目无 Git，因此以限定范围、哈希与验收报告交付，不运行 Git 提交。

## 执行选择

计划已准备，尚未实施。可选择：①使用既有 `cb-coder-01 + workflow_conductor.py` 流水线做第一阶段，架构师独立验收；②本轮只保留设计，暂不施工。未经执行确认不启动工作流。
