"""OpenOntology 统一作战指挥中心 · 全量预装启动器。

一条命令拉起 6 个核心 Ontology 蓝图与 8 个真实数据实例：

1. ``software_factory``  AI 软件研发工厂（研发成员 / 工单 / 交付物）
2. ``task_board``        多智能体任务看板（看板 / 列语义 / 卡片 / 认领 / 裁决）
3. ``coldchain_mcts``    冷链医药调度与 MCTS 多步规划
4. ``crm_saas_unplug``   多云 SaaS 寄居吸纳与脱壳
5. ``steel_mdm``         钢厂主数据破局与三视角对账
6. ``osv2_governance``   钢卷资产与 OSv2 四大原语治理

运行::

    python3 -m examples.command_center_demo --port 8765
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel

from open_ontology import (
    Actor,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
)
from open_ontology.workshop import start_command_center_server
from .dev_factory_ontology import FACTORY_MANAGER
from .dev_factory_ontology import spec as factory_spec
from open_ontology.workshop import (
    CommandCenterRegistry,
    ConnectorMount,
    OntologyInstanceEntry,
)
from open_ontology.workspaces import WorkspaceCatalog, check_kernel_database

from .kanban import BOARD_MANAGER
from .kanban import spec as board_spec
from .o2c_demo_seed import build_o2c_demo
from .o2c_ontology import (
    FINANCE_DIRECTOR as O2C_FINANCE_DIRECTOR,
    spec as o2c_spec,
)
from .osv2_governance_ontology import (
    OSV2_FINANCE,
    OSV2_OPS,
    ScrapLot,
    SteelCoil,
    osv2_spec,
)

from .mcts_world_model_demo import (
    ActionCandidate,
    ColdTruck,
    DISPATCHER,
    EmergencyOrder,
    build_demo as build_coldchain_demo,
)
from .saas_unplug_demo import (
    AI_AGENT as SAAS_AI_AGENT,
    build_saas_model,
    build_saas_snapshot,
)
from .steel_mdm_demo import (
    ContractSKU,
    STEWARD,
    build_demo as build_steel_demo,
)

__all__ = [
    "BOARD_KEY",
    "IncompatibleBoardDatabaseError",
    "build_default_command_center",
    "build_o2c_command_center",
    "build_osv2_governance",
    "coldchain_mcts_config",
    "main",
]

#: 仓库根目录：``docs/ARCHITECTURE.md`` 与 ``_state.md`` 都从这里取全文。
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: 默认持久化目录：实例库真实落盘，供新会话按对象 ID 直接从 SQLite 接管。
_DEFAULT_DATA_DIR = _REPO_ROOT / ".instances"

#: Hermes O2C 助手实时同步的 SQLite 状态库（存在时默认接入指挥台）。
_DEFAULT_HERMES_O2C_DB = (
    Path.home()
    / ".hermes"
    / "profiles"
    / "o2c"
    / "state"
    / "o2c_erp"
    / "o2c_world.sqlite3"
)

#: 演示看板的对象 ID。
BOARD_KEY = "BD-CC-001"

#: 可复用的看板库必须**完整**具备当前内核 schema；缺任何一张即视为半成品 /
#: 外来库。``OntologyRuntime`` 打开时会 ``executescript(schema.sql)``，漏检的表
#: 会被静默补建——预检必须把六张核心表一次性收齐，才能保证「要么无损打开，
#: 要么明确失败且不改动文件」。
_REQUIRED_TABLES = frozenset(
    {
        "ontology_registry",
        "ontology_objects",
        "ontology_links",
        "ontology_overlay",
        "ontology_action_runs",
        "ontology_write_gate",
    }
)


class IncompatibleBoardDatabaseError(ValueError):
    """指定路径上的数据库不是可安全复用的看板库（外来 / 半成品 / 损坏）。"""


def _reset_sqlite_file(db_path: str | Path) -> None:
    """删除**这一个**库文件及其 WAL/SHM 旁文件。

    只作用于调用方明确指定的数据库，绝不删除所在目录——同目录的其它文件
    （配置、备份、其它实例库）必须原样保留。
    """
    base = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        stale = Path(f"{base}{suffix}")
        if stale.exists():
            stale.unlink()


def _board_database_state(db_path: str | Path, board_key: str) -> str:
    """判断指定库该「新建」还是「直接打开」。

    返回 ``"missing"``（文件不存在，可以首次播种）或 ``"ready"``（已含目标
    看板，必须非破坏性打开）。其它情况——外来 SQLite 文件、缺内核表的半成品、
    损坏文件、没有该看板的库——一律抛 :class:`IncompatibleBoardDatabaseError`：
    宁可明确失败，也不覆盖别人的库或在上面静默补种演示数据。
    """
    path = Path(db_path)
    if not path.exists():
        return "missing"
    # 必须用标准 ``file://`` URI 编码：路径里的 ``#`` / ``?`` / ``%`` / 空格
    # 直接拼进 ``file:{path}?mode=ro`` 会被当作片段 / 查询串 / 百分号转义，
    # 从而打开（甚至创建）一个被截断的旁路文件，违背只读预检的无副作用约定。
    readonly_uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(readonly_uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        raise IncompatibleBoardDatabaseError(
            f"{path} 无法以只读方式打开，拒绝覆盖：{exc}"
        ) from exc
    try:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not _REQUIRED_TABLES <= tables:
            missing = ", ".join(sorted(_REQUIRED_TABLES - tables))
            raise IncompatibleBoardDatabaseError(
                f"{path} 不是可复用的看板库：缺少内核表 {missing}。"
                f"需要重建请显式传 reset=True（只重建该库，不删目录）。"
            )
        found = conn.execute(
            "SELECT 1 FROM ontology_objects "
            "WHERE object_type = 'Board' AND object_id = ? LIMIT 1",
            (board_key,),
        ).fetchone()
        if found is None:
            raise IncompatibleBoardDatabaseError(
                f"{path} 中没有看板 {board_key}（半成品或别的实例库）。"
                f"需要重建请显式传 reset=True（只重建该库，不删目录）。"
            )
    except sqlite3.Error as exc:
        raise IncompatibleBoardDatabaseError(
            f"{path} 不是可复用的看板库：无法读取内核表（{exc}）。"
            f"需要重建请显式传 reset=True（只重建该库，不删目录）。"
        ) from exc
    finally:
        conn.close()
    return "ready"



def _submit(runtime: OntologyRuntime, run_id: str, action: str, params: dict[str, Any], actor: Actor) -> dict[str, Any]:
    return runtime.submit_action(run_id, action, params, actor)


# ---------------------------------------------------------------------------
# 1. software_factory
# ---------------------------------------------------------------------------
def _build_software_factory(registry: CommandCenterRegistry) -> None:
    runtime = OntologyRuntime(factory_spec)
    engineer = FACTORY_MANAGER
    run = 0

    def act(action: str, params: dict[str, Any]) -> None:
        nonlocal run
        run += 1
        _submit(runtime, f"CC-FACTORY-{run:03d}", action, params, engineer)

    for engineer_id, name, role, specialty in (
        ("ENG-ARCH-01", "首席架构师", "architect", "kernel"),
        ("ENG-CODER-A", "核心程序员A", "coder", "kernel"),
        ("ENG-CODER-B", "核心程序员B", "coder", "integration"),
        ("ENG-CODER-C", "核心程序员C", "coder", "video"),
        ("ENG-TEST-01", "测试官", "tester", "quality"),
    ):
        act(
            "recruit_engineer",
            {
                "engineer_id": engineer_id,
                "name": name,
                "role": role,
                "specialty": specialty,
            },
        )

    act(
        "open_ticket",
        {
            "ticket_id": "TK-CC-101",
            "title": "指挥中心多实例路由",
            "spec_uri": "SPEC_COMMAND_CENTER.md",
            "isolated_scope": "open_ontology/",
            "instructions": "为 Workshop 增加多蓝图 / 多实例注册中心。",
            "max_rework": 3,
        },
    )
    act(
        "open_ticket",
        {
            "ticket_id": "TK-CC-102",
            "title": "Instance 沙盘 Fork",
            "spec_uri": "SPEC_COMMAND_CENTER.md",
            "isolated_scope": "examples/",
            "instructions": "实现毫秒级 fork_instance。",
        },
    )
    act(
        "open_ticket",
        {
            "ticket_id": "TK-CC-103",
            "title": "研发工厂本体验收",
            "spec_uri": "SPEC_DEV_FACTORY.md",
            "isolated_scope": "tests/",
            "require_testing": True,
            "max_rework": 5,
        },
    )
    act(
        "dispatch_ticket",
        {"ticket": "TK-CC-101", "engineer": "ENG-CODER-A"},
    )
    act(
        "dispatch_ticket",
        {"ticket": "TK-CC-103", "engineer": "ENG-CODER-B"},
    )
    act(
        "submit_work",
        {
            "ticket": "TK-CC-103",
            "engineer": "ENG-CODER-B",
            "summary": "16 条验收测试全绿",
            "artifact_uri": "tests/test_dev_factory.py",
        },
    )
    act(
        "review_ticket",
        {
            "ticket": "TK-CC-103",
            "reviewer": "ENG-TEST-01",
            "verdict": "PASS",
            "feedback": "单测与静态审计全部通过",
        },
    )
    act(
        "review_ticket",
        {
            "ticket": "TK-CC-103",
            "reviewer": "ENG-ARCH-01",
            "verdict": "PASS",
            "feedback": "架构验收通过",
        },
    )

    registry.register_ontology(
        "software_factory",
        "AI 软件研发工厂",
        factory_spec,
        category="Agentic Delivery",
        description="用本体治理研发团队、工单流转、交卷评审与无损换人。",
    )
    registry.register_instance(
        "factory-prod",
        "software_factory",
        "研发工厂 · 生产实例",
        runtime,
        db_uri="sqlite://factory_prod.sqlite3",
        description="5 名研发成员 + 在途 / 完结工单，挂载三个 Agent Harness。",
        connectors=[
            ConnectorMount("CodeBuddy CLI", "Harness", "cli://codebuddy", "CONNECTED"),
            ConnectorMount("OpenCode HTTP", "Harness", "http://localhost:4096", "CONNECTED"),
            ConnectorMount("Hermes Agent", "Harness", "cli://hermes", "CONNECTED"),
        ],
        default_actor=FACTORY_MANAGER,
        sample_action_params={
            "dispatch_ticket": {
                "ticket": "TK-CC-102",
                "engineer": "ENG-CODER-C",
            },
            "submit_work": {
                "ticket": "TK-CC-101",
                "engineer": "ENG-CODER-A",
                "summary": "多实例路由实现完成",
                "artifact_uri": "open_ontology/workshop.py",
            },
            "review_ticket": {
                "ticket": "TK-CC-101",
                "reviewer": "ENG-ARCH-01",
                "verdict": "PASS",
                "feedback": "通过",
            },
        },
    )


# ---------------------------------------------------------------------------
# 2. task_board
# ---------------------------------------------------------------------------
def _seed_task_board(runtime: OntologyRuntime) -> None:
    """首次创建时播种演示数据：成员、四张卡、认领、阻塞。"""
    run = 0

    def act(action: str, params: dict[str, Any]) -> None:
        nonlocal run
        run += 1
        _submit(runtime, f"CC-BOARD-{run:03d}", action, params, BOARD_MANAGER)

    act(
        "create_board",
        {
            "board_key": BOARD_KEY,
            "title": "任务看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
            "description": "固定 6 态流（todo/ready/doing/done/blocked/triage），全部不变量由本体裁决。",
        },
    )

    for member_id, name, role in (
        ("M-DEV-01", "开发者 A", "developer"),
        ("M-DEV-02", "开发者 B", "developer"),
        ("M-TEST-01", "测试员", "tester"),
        ("M-REV-01", "评审员", "reviewer"),
    ):
        act(
            "add_member",
            {"member_id": member_id, "name": name, "role": role},
        )

    act(
        "create_card",
        {
            "card_id": "C-100",
            "board": "BD-CC-001",
            "title": "搭建任务看板领域模型",
            "body": "定义看板、成员、卡片与固定 6 态流不变量。",
        },
    )
    act(
        "create_card",
        {
            "card_id": "C-101",
            "board": "BD-CC-001",
            "title": "实现外部 OpenAPI 快照接入",
            "body": "完成源字段与自有覆盖层同步。",
        },
    )
    act(
        "create_card",
        {
            "card_id": "C-102",
            "board": "BD-CC-001",
            "title": "完善认领超时释放动作",
            "body": "通过 release_claim 释放滞留卡片并累计重试。",
        },
    )
    act(
        "create_card",
        {
            "card_id": "C-103",
            "board": "BD-CC-001",
            "title": "看板状态流视图",
            "body": "展示固定 6 态节点与流转关系。",
            "waits_for": ("C-101",),
        },
    )

    act("claim_card", {"card": "C-100", "member": "M-DEV-01"})
    act(
        "record_outcome",
        {
            "card": "C-100",
            "member": "M-DEV-01",
            "outcome": "completed",
            "summary": "看板领域模型与状态机完成",
            "artifact_uri": "examples/kanban.py",
        },
    )
    act("claim_card", {"card": "C-102", "member": "M-DEV-02"})
    act(
        "add_comment",
        {
            "card": "C-102",
            "author": "M-REV-01",
            "body": "注意补充超时释放的单元测试。",
        },
    )
    act("claim_card", {"card": "C-101", "member": "M-DEV-01"})
    act(
        "record_outcome",
        {
            "card": "C-101",
            "member": "M-DEV-01",
            "outcome": "blocked",
            "summary": "等待接口字段确认",
            "block_kind": "awaiting_review",
        },
    )


def _build_task_board(
    registry: CommandCenterRegistry,
    db_path: str | Path = ":memory:",
    *,
    reset: bool = False,
) -> None:
    """任务看板：列拓扑是数据，卡片靠带数字的证据收单。

    ``db_path`` 传文件路径时实例真实落盘（默认演示走
    ``.instances/board_prod.sqlite3``），空会话可直接从 SQLite 接管。

    打开语义：

    * 文件不存在 → 首次创建并播种演示数据；
    * 文件已存在且含目标看板 → **非破坏性打开**，对象值 / 版本 / 关系 /
      动作账本原样保留，不重放任何种子动作；
    * ``reset=True`` → 只删除并重建这一个库文件（及其 WAL/SHM），
      绝不删除所在目录；
    * 外来 / 半成品 / 损坏库 → 抛 :class:`IncompatibleBoardDatabaseError`。
    """
    if str(db_path) == ":memory:":
        runtime = OntologyRuntime(board_spec)
        _seed_task_board(runtime)
    else:
        path = Path(db_path)
        if reset:
            _reset_sqlite_file(path)
        if _board_database_state(path, BOARD_KEY) == "missing":
            path.parent.mkdir(parents=True, exist_ok=True)
            runtime = OntologyRuntime(board_spec, db_path=path)
            _seed_task_board(runtime)
        else:
            runtime = OntologyRuntime(board_spec, db_path=path)

    registry.register_ontology(
        "task_board",
        "任务看板",
        board_spec,
        category="Collaboration",
        description="固定 6 态流任务看板：依赖 DAG、WIP 上限与 Actor/Member 身份绑定。",
    )
    registry.register_workspace(
        "board-prod",
        "task_board",
        "任务看板 · 示例实例",
        runtime,
        db_uri="sqlite://board_prod.sqlite3",
        description="4 名成员 + 4 张卡（已完成 / 执行中 / 阻塞 / 待前置）。",
        default_actor=BOARD_MANAGER,
        sample_action_params={
            "claim_card": {"card": "C-102", "member": "M-DEV-02"},
            "record_outcome": {
                "card": "C-102",
                "member": "M-DEV-02",
                "outcome": "completed",
                "summary": "任务完成",
            },
            "create_card": {
                "board": "BD-CC-001",
                "title": "新卡片",
                "body": "在这里写任务说明。",
            },
            "add_comment": {
                "card": "C-102",
                "author": "M-REV-01",
                "body": "补一条评论。",
            },
        },
    )


# ---------------------------------------------------------------------------
# 3. coldchain_mcts
# ---------------------------------------------------------------------------
def coldchain_mcts_config(
    runtime: OntologyRuntime, actor: Actor = DISPATCHER
) -> dict[str, Any]:
    """为某个冷链实例生成 MCTS 规划配置（候选 + 先验 + 价值函数）。"""
    order_sequence = tuple(
        order.order_id
        for order in sorted(
            runtime.query_objects(EmergencyOrder, actor),
            key=lambda row: row.order_id,
        )
        if order.status == "pending"
    )
    trucks = runtime.query_objects(ColdTruck, actor)
    inverse = {
        truck.truck_id: 1.0 / max(truck.freight_cost, 1e-9) for truck in trucks
    }
    total = sum(inverse.values()) or 1.0
    priors = {truck_id: weight / total for truck_id, weight in inverse.items()}

    def generator(fork_runtime: OntologyRuntime, step_index: int) -> Sequence[ActionCandidate]:
        if step_index >= len(order_sequence):
            return ()
        order_id = order_sequence[step_index]
        return [
            ActionCandidate(order_id=order_id, truck_id=truck_id, prior=prior)
            for truck_id, prior in sorted(priors.items())
        ]

    return {
        "candidate_generator": generator,
        "value_function": "evaluate_world_value",
        "simulations": 80,
        "c_puct": 1.414,
        "value_scale": 100000.0,
        "seed": 17,
        "max_steps": len(order_sequence),
    }


def _build_coldchain(registry: CommandCenterRegistry) -> None:
    demo = build_coldchain_demo()
    runtime = demo.runtime

    registry.register_ontology(
        "coldchain_mcts",
        "冷链医药调度与 MCTS 规划",
        runtime.spec,
        category="Supply Chain",
        description="三波急救冷链运单 + 三辆冷链车，单步贪心会掉进死局，MCTS 多步推演锁定全局最优。",
    )
    registry.register_instance(
        "coldchain-wave3",
        "coldchain_mcts",
        "冷链第三波 · 急救调度",
        runtime,
        db_uri="sqlite://coldchain_wave3.sqlite3",
        description="WMS / TMS / 温控遥测在线，可直接跑 80 次 MCTS 模拟。",
        connectors=[
            ConnectorMount("WMS 仓储", "WMS", "http://wms.local/api", "CONNECTED"),
            ConnectorMount("TMS 运输", "SaaS", "http://tms.local/api", "CONNECTED"),
            ConnectorMount("温控遥测", "SaaS", "mqtt://coldchain/telemetry", "CONNECTED"),
        ],
        default_actor=DISPATCHER,
        sample_action_params={
            "dispatch_order": {"order_id": "ORD-1", "truck_id": "TRK-FAST"},
        },
        mcts_config=coldchain_mcts_config(runtime, DISPATCHER),
    )


# ---------------------------------------------------------------------------
# 3. crm_saas_unplug
# ---------------------------------------------------------------------------
def _build_saas(registry: CommandCenterRegistry) -> None:
    parasitic = build_saas_model(standalone=False)
    runtime = parasitic.new_runtime()
    objects, links = build_saas_snapshot(parasitic)
    runtime.load_snapshot(objects, links)
    _submit(
        runtime,
        "CC-SAAS-001",
        "ai_triage_ticket",
        {
            "ticket_id": "TKT-3004",
            "fraud_score": 88,
            "vip_tier": "GOLD",
            "note": "高风险客户，优先人工复核",
        },
        SAAS_AI_AGENT,
    )

    registry.register_ontology(
        "crm_saas_unplug",
        "多云 SaaS 寄居吸纳与脱壳",
        parasitic.spec,
        category="Customer 360",
        description="Shopify / Stripe / Zendesk 快照寄生，自有治理字段沉淀在 overlay；一键拔除后完全独立运行。",
    )

    def unplug() -> dict[str, Any]:
        return {
            "mode": "standalone-ready",
            "objects": runtime.count_objects(),
            "overlay_preserved": True,
            "message": "外部 SaaS 连接器已全部拔除，Ontology 自有字段完整保留。",
        }

    entry = registry.register_instance(
        "saas-parasitic",
        "crm_saas_unplug",
        "SaaS 寄生态 · 三云在线",
        runtime,
        db_uri="sqlite://saas_parasitic.sqlite3",
        description="客户 / 订单 / 工单来自三套 SaaS，AI 分诊结果写 overlay。",
        connectors=[
            ConnectorMount("Shopify", "SaaS", "https://shopify.local/admin", "CONNECTED"),
            ConnectorMount("Stripe", "SaaS", "https://api.stripe.local", "CONNECTED"),
            ConnectorMount("Zendesk", "SaaS", "https://zendesk.local/api", "CONNECTED"),
        ],
        default_actor=SAAS_AI_AGENT,
        sample_action_params={
            "ai_triage_ticket": {
                "ticket_id": "TKT-3004",
                "fraud_score": 92,
                "vip_tier": "GOLD",
                "note": "二次复核：保持人工介入",
            },
            "create_customer_and_order": {
                "customer_id": "CUST-2001",
                "email": "new@example.com",
                "name": "新客户",
                "order_id": "ORD-2101",
                "item_sku": "SKU-COFFEE-1KG",
                "amount_cents": 9900,
                "age_days": 0,
            },
        },
        unplug_fn=unplug,
    )
    assert isinstance(entry, OntologyInstanceEntry)

    standalone = build_saas_model(standalone=True)
    runtime_b = standalone.new_runtime()
    customers = [
        standalone.Customer(customer_id="CUST-1001", email="wang@example.com", name="王小明", lifetime_value_cents=48_000),
        standalone.Customer(customer_id="CUST-1002", email="li@example.com", name="李静", lifetime_value_cents=25_999),
        standalone.Customer(customer_id="CUST-1003", email="zhao@example.com", name="赵六", lifetime_value_cents=15_000),
    ]
    orders = [
        standalone.ShopOrder(order_id="ORD-2001", customer_id="CUST-1001", item_sku="SKU-COFFEE-1KG", amount_cents=8_999, age_days=12, status="paid"),
        standalone.ShopOrder(order_id="ORD-2002", customer_id="CUST-1002", item_sku="SKU-TEA-GIFT", amount_cents=25_999, age_days=45, status="shipped"),
        standalone.ShopOrder(order_id="ORD-2003", customer_id="CUST-1003", item_sku="SKU-BLENDER", amount_cents=15_000, age_days=8, status="shipped"),
    ]
    tickets = [
        standalone.SupportTicket(ticket_id="TKT-3001", order_id="ORD-2001", customer_id="CUST-1001", issue_type="damaged", requested_refund_cents=8_999),
        standalone.SupportTicket(ticket_id="TKT-3002", order_id="ORD-2002", customer_id="CUST-1002", issue_type="late", requested_refund_cents=25_999),
    ]
    for obj in [*customers, *orders, *tickets]:
        runtime_b.put_object(obj)
    for order in orders:
        customer = next(row for row in customers if row.customer_id == order.customer_id)
        runtime_b.link_objects(customer, standalone.customer_orders, order)
    for ticket in tickets:
        order = next(row for row in orders if row.order_id == ticket.order_id)
        runtime_b.link_objects(order, standalone.order_tickets, ticket)

    registry.register_instance(
        "saas-standalone",
        "crm_saas_unplug",
        "SaaS 脱壳态 · 完全独立",
        runtime_b,
        db_uri="sqlite://saas_standalone.sqlite3",
        description="全部对象 __owned__=True，外部连接器全部 UNPLUGGED。",
        connectors=[
            ConnectorMount("Shopify", "SaaS", "https://shopify.local/admin", "UNPLUGGED"),
            ConnectorMount("Stripe", "SaaS", "https://api.stripe.local", "UNPLUGGED"),
            ConnectorMount("Zendesk", "SaaS", "https://zendesk.local/api", "UNPLUGGED"),
        ],
        default_actor=SAAS_AI_AGENT,
        sample_action_params={
            "ai_triage_ticket": {
                "ticket_id": "TKT-3001",
                "fraud_score": 12,
                "vip_tier": "SILVER",
                "note": "脱壳态原生分诊",
            },
        },
    )


# ---------------------------------------------------------------------------
# 4. steel_mdm
# ---------------------------------------------------------------------------
def _build_steel(registry: CommandCenterRegistry) -> None:
    demo = build_steel_demo(with_lots=True)
    runtime = demo.runtime

    registry.register_ontology(
        "steel_mdm",
        "钢厂主数据破局与三视角对账",
        runtime.spec,
        category="Master Data",
        description="标准牌号 / 散码合同 SKU / 进厂批次 / 高炉配料单，三视角台账实时对账。",
    )
    registry.register_instance(
        "steel-mill-north",
        "steel_mdm",
        "华北钢厂 · 主数据治理",
        runtime,
        db_uri="sqlite://steel_mill_north.sqlite3",
        description="ERP 合同库 + WMS 料场库 + MES 高炉配料系统三源在线。",
        connectors=[
            ConnectorMount("ERP 合同库", "ERP", "http://erp.local/contracts", "CONNECTED"),
            ConnectorMount("WMS 料场库", "WMS", "http://wms.local/yards", "CONNECTED"),
            ConnectorMount("MES 高炉配料", "SaaS", "http://mes.local/burden", "CONNECTED"),
        ],
        default_actor=STEWARD,
        sample_action_params={
            "map_legacy_sku_to_spec": {
                "sku_id": "ERP-SKU-9001",
                "canonical_spec_id": "SPEC-A",
            },
            "recommend_spec_for_sku": {"ash_pct": 7.5, "sulfur_pct": 0.6},
        },
    )


# ---------------------------------------------------------------------------
# 5. osv2_governance (纯 M1 模型定义于 examples/osv2_governance_ontology.py)
# ---------------------------------------------------------------------------
def build_osv2_governance(registry: CommandCenterRegistry | None = None) -> CommandCenterRegistry:
    """单独构建 OSv2 治理蓝图 + 实例（也供测试复用）。"""
    registry = registry or CommandCenterRegistry()
    runtime = OntologyRuntime(osv2_spec)
    runtime.put_object(
        SteelCoil(
            asset_id="C-1",
            grade="SUS304",
            unit_price_ton=18000.0,
            stock_tons=120.0,
            cost_note="主力现货",
            internal_code="NC-304",
        )
    )
    runtime.put_object(
        SteelCoil(
            asset_id="C-2",
            grade="SUS316L",
            unit_price_ton=24000.0,
            stock_tons=80.0,
            cost_note="进口批次",
            internal_code="NC-316L",
        )
    )
    runtime.put_object(
        ScrapLot(
            asset_id="LOT-1",
            grade="废钢重料",
            region="south",
            unit_price_ton=300.0,
            stock_tons=50.0,
        )
    )
    registry.register_ontology(
        "osv2_governance",
        "钢卷资产与 OSv2 四大原语治理",
        osv2_spec,
        category="Asset Governance",
        description="Interface 多态、列级脱敏、Automations 条件反射与 MDO 冲突裁决。",
    )
    registry.register_instance(
        "osv2-coil-prod",
        "osv2_governance",
        "钢卷资产 · 治理生产实例",
        runtime,
        db_uri="sqlite://osv2_coil_prod.sqlite3",
        description="华北现货库 + 财务核价中心；切换角色体验列级脱敏。",
        connectors=[
            ConnectorMount("华北现货库", "WMS", "http://wms.local/north", "CONNECTED"),
            ConnectorMount("财务核价中心", "ERP", "http://finance.local/pricing", "CONNECTED"),
        ],
        default_actor=OSV2_OPS,
        sample_action_params={
            "set_quality_grade": {"asset_id": "C-1", "quality_grade": "A"},
            "adjust_price": {"asset_id": "C-1", "unit_price_ton": 18500},
            "request_price_change": {"asset_id": "C-2", "unit_price_ton": 24500},
        },
    )
    return registry


class _LiveSQLiteRuntimeProxy:
    """按文件 inode 自动重载的只读/交互式 SQLite Runtime 代理。

    使 ``http://127.0.0.1:8765`` 在 O2C MCP 服务执行 ``重置演示`` 重建数据库文件后，
    无需重启指挥台即可自动追踪到最新 inode 上的状态与账本。
    """

    def __init__(self, spec: OntologySpec, db_path: Path) -> None:
        self._spec = spec
        self._db_path = Path(db_path).expanduser().resolve()
        self._lock = threading.RLock()
        self._hooks: list[Callable[[Actor, dict[str, Any]], None]] = []
        self._inner: OntologyRuntime | None = None
        self._inode: int | None = None
        self._fallback: OntologyRuntime | None = None

    @property
    def spec(self) -> OntologySpec:
        return self._spec

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _current(self) -> OntologyRuntime:
        with self._lock:
            if self._db_path.is_file():
                inode = self._db_path.stat().st_ino
                if self._inner is not None and self._inode == inode:
                    return self._inner
                check_kernel_database(self._db_path)
                new_rt = OntologyRuntime(self._spec, db_path=self._db_path)
                for hook in self._hooks:
                    if hook not in new_rt._post_commit_hooks:
                        new_rt.add_post_commit_hook(hook)
                old_inner = self._inner
                self._inner = new_rt
                self._inode = inode
                if old_inner is not None:
                    try:
                        old_inner.close()
                    except Exception:
                        pass
                return new_rt

            if self._inner is not None:
                try:
                    self._inner.close()
                except Exception:
                    pass
                self._inner = None
                self._inode = None
            if self._fallback is None:
                self._fallback = build_o2c_demo().runtime
                for hook in self._hooks:
                    if hook not in self._fallback._post_commit_hooks:
                        self._fallback.add_post_commit_hook(hook)
            return self._fallback

    def add_post_commit_hook(
        self, hook: Callable[[Actor, dict[str, Any]], None]
    ) -> None:
        with self._lock:
            if hook not in self._hooks:
                self._hooks.append(hook)
            rt = self._current()
            if hook not in rt._post_commit_hooks:
                rt.add_post_commit_hook(hook)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current(), name)


def build_o2c_command_center(
    registry: CommandCenterRegistry | None = None,
    *,
    db_path: str | Path = ":memory:",
) -> CommandCenterRegistry:
    """注册标准 O2C 业财治理蓝图及其内存/实时工作空间。"""
    registry = registry or CommandCenterRegistry()
    if str(db_path) == ":memory:":
        runtime: Any = build_o2c_demo().runtime
        db_uri = "sqlite://o2c_erp_north.sqlite3"
    else:
        runtime = _LiveSQLiteRuntimeProxy(o2c_spec, Path(db_path))
        db_uri = "sqlite://o2c_world.sqlite3"

    registry.register_ontology(
        "o2c_order_to_cash",
        "订单到收款业财治理",
        o2c_spec,
        category="Order-to-Cash",
        description="客户授信红线、未发货禁开票硬护栏、飞书交互签批与真实 ERP 一键时光机冲正。",
    )
    registry.register_workspace(
        "o2c-erp-north",
        "o2c_order_to_cash",
        "订单到收款 · 华北 ERP 实时沙盘",
        runtime,
        db_uri=db_uri,
        description="连接真实 Odoo 19 (erp_north) 与飞书 O2C业财助手，实时同步挂起审批、回写凭据与冲正账本。",
        connectors=[
            ConnectorMount("华北 Odoo 19 账套", "ERP", "http://127.0.0.1:8069/erp_north", "CONNECTED"),
            ConnectorMount("飞书交互审批网关", "SaaS", "wss://msg-frontier.feishu.cn/ws/v2", "CONNECTED"),
        ],
        default_actor=O2C_FINANCE_DIRECTOR,
        sample_action_params={
            "create_sales_order": {
                "order_id": "O2C-SO-003",
                "customer": "O2C-CUST-001",
                "product_summary": "O2C Widget",
                "quantity": 5,
                "unit_price_cents": 1000000,
            },
            "confirm_sales_order": {"order": "O2C-SO-002"},
            "ship_order_delivery": {"order": "O2C-SO-001"},
            "issue_and_post_invoice": {"order": "O2C-SO-001"},
            "record_customer_payment": {"order": "O2C-SO-001", "payment_cents": 2000000},
            "annotate_o2c_governance": {
                "order": "O2C-SO-002",
                "account_rep": "智能销售助理",
                "fulfillment_note": "战略特批大客户",
            },
        },
    )
    return registry


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def build_default_command_center(
    data_dir: str | Path | None = None,
    *,
    reset: bool = False,
    workspace_catalog: WorkspaceCatalog | None = None,
    o2c_db_path: str | Path | None = None,
) -> CommandCenterRegistry:
    """预装 7 个 Ontology 蓝图 + 9 个真实数据 Instance。

    默认把 ``board-prod`` 的 SQLite 库落在 ``.instances/board_prod.sqlite3``，
    使实例真实落盘可被新会话按对象 ID 检索。文件落盘的 runtime 身份写入
    ``.instances/workspaces.json``（可选清单，显式格式版本），供新的清单读取器
    在不构造 / 重置演示的情况下定位工作空间并校验可信模型绑定。

    **默认非破坏性**：库已存在且含看板时直接打开，原有对象值、版本、关系与
    动作账本全部保留。只有显式 ``reset=True`` 才会重建**指定的这一个库**
    （绝不删除目录）；传入不兼容 / 半成品 / 损坏的库会明确报错。
    """
    if data_dir is not None and str(data_dir).strip() == ":memory:":
        raise ValueError("data_dir must be a filesystem directory path, not ':memory:'")
    data_root = Path(data_dir or _DEFAULT_DATA_DIR)
    if workspace_catalog is None:
        workspace_catalog = WorkspaceCatalog.load(data_root / "workspaces.json")
    registry = CommandCenterRegistry(catalog=workspace_catalog)
    registry.register_doc(
        "task_board",
        "架构演进与当前状态真值表",
        "docs/TASK_BOARD.md",
        tab_label="📋 演进清单",
    )
    _build_software_factory(registry)
    _build_task_board(
        registry,
        db_path=data_root / "board_prod.sqlite3",
        reset=reset,
    )
    _build_coldchain(registry)
    _build_saas(registry)
    _build_steel(registry)
    build_osv2_governance(registry)
    build_o2c_command_center(registry, db_path=o2c_db_path or ":memory:")
    # 第 9 个工作空间：冷链 MCTS 实验室（毫秒级 fork，破坏性实验不碰生产）。
    registry.fork_instance(
        "coldchain-wave3",
        "coldchain-wave3-lab",
        "冷链第三波 · MCTS 实验室",
    )
    return registry


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenOntology 统一作战指挥中心")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--title", default="OpenOntology 统一作战指挥中心")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="显式重建演示看板库（只重建 .instances/board_prod.sqlite3，不删目录）",
    )
    default_o2c_db = (
        str(_DEFAULT_HERMES_O2C_DB)
        if _DEFAULT_HERMES_O2C_DB.is_file()
        else ":memory:"
    )
    parser.add_argument(
        "--o2c-db",
        default=default_o2c_db,
        help="O2C 实时工作空间挂载的 SQLite 数据库路径（默认连接 Hermes o2c 状态库）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    registry = build_default_command_center(
        reset=args.reset,
        o2c_db_path=args.o2c_db,
    )
    handle = start_command_center_server(
        registry, args.host, args.port, title=args.title
    )
    print(f"Command Center ready → {handle.base_url}")
    print(
        "ontologies:",
        ", ".join(entry.name for entry in registry.ontologies.values()),
    )
    print("instances:", ", ".join(registry.instances))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        handle.stop()
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
