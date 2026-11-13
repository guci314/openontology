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
import time
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel

from open_ontology import (
    Actor,
    InvariantError,
    LinkType,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    start_command_center_server,
)
from open_ontology.dev_factory import FACTORY_MANAGER
from open_ontology.dev_factory import spec as factory_spec
from open_ontology.kanban import BOARD_MANAGER
from open_ontology.kanban import spec as board_spec
from open_ontology.workshop import (
    CommandCenterRegistry,
    ConnectorMount,
    OntologyInstanceEntry,
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
    "build_osv2_governance",
    "coldchain_mcts_config",
    "main",
]

#: 仓库根目录：``docs/ARCHITECTURE.md`` 与 ``_state.md`` 都从这里取全文。
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: 默认持久化目录：实例库真实落盘，供新会话按对象 ID 直接从 SQLite 接管。
_DEFAULT_DATA_DIR = _REPO_ROOT / ".instances"

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



def _read_repo_doc(relpath: str) -> str:
    """读取仓库根目录下的文档全文；缺失时返回空串，由调用方回退。"""
    path = _REPO_ROOT / relpath
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()



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
        {"ticket_id": "TK-CC-101", "engineer_id": "ENG-CODER-A"},
    )
    act(
        "dispatch_ticket",
        {"ticket_id": "TK-CC-103", "engineer_id": "ENG-CODER-B"},
    )
    act(
        "submit_work",
        {
            "ticket_id": "TK-CC-103",
            "engineer_id": "ENG-CODER-B",
            "summary": "16 条验收测试全绿",
            "artifact_uri": "tests/test_dev_factory.py",
        },
    )
    act(
        "review_ticket",
        {
            "ticket_id": "TK-CC-103",
            "reviewer_id": "ENG-TEST-01",
            "verdict": "PASS",
            "feedback": "单测与静态审计全部通过",
        },
    )
    act(
        "review_ticket",
        {
            "ticket_id": "TK-CC-103",
            "reviewer_id": "ENG-ARCH-01",
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
                "ticket_id": "TK-CC-102",
                "engineer_id": "ENG-CODER-C",
            },
            "submit_work": {
                "ticket_id": "TK-CC-101",
                "engineer_id": "ENG-CODER-A",
                "summary": "多实例路由实现完成",
                "artifact_uri": "open_ontology/workshop.py",
            },
            "review_ticket": {
                "ticket_id": "TK-CC-101",
                "reviewer_id": "ENG-ARCH-01",
                "verdict": "PASS",
                "feedback": "通过",
            },
        },
    )


# ---------------------------------------------------------------------------
# 2. task_board
# ---------------------------------------------------------------------------
BOARD_COLUMNS: list[dict[str, Any]] = [
    {
        "key": "todo",
        "label": "待办",
        "purpose": "initial",
        "order": 0,
        "allowed_targets": ("ready", "blocked"),
    },
    {
        "key": "ready",
        "label": "可认领",
        "purpose": "ready",
        "order": 1,
        "claimable": True,
        "allowed_roles": ("developer", "tester"),
        "allowed_targets": ("todo", "blocked"),
        "on_claimed": "doing",
    },
    {
        "key": "doing",
        "label": "执行中",
        "purpose": "working",
        "order": 2,
        "on_completed": "done",
        "on_failed": "ready",
        "on_exhausted": "blocked",
        "on_blocked": "blocked",
        "on_abandoned": "triage",
    },
    {
        "key": "done",
        "label": "已完成",
        "purpose": "completion",
        "order": 3,
        "is_terminal": True,
        "allowed_targets": ("todo",),
    },
    {
        "key": "blocked",
        "label": "阻塞",
        "purpose": "failure",
        "order": 4,
        "max_retries": 2,
        "allowed_targets": ("ready", "todo"),
        "on_unblocked": "ready",
    },
    {
        "key": "triage",
        "label": "分诊",
        "purpose": "escalation",
        "order": 5,
        "allowed_targets": ("todo",),
    },
]


def _seed_task_board(runtime: OntologyRuntime) -> None:
    """首次创建时播种演示数据：成员、四张卡、认领、裁决、阻塞。

    只在「库不存在」或「显式 reset」时调用；重复打开时一行都不执行，
    因此种子动作（``CC-BOARD-001``…）绝不会重放。
    """
    run = 0

    # Board.goal / Board.state 直接承载完整文档全文，指挥台与换代供血都不再缺上下文。
    arch_doc = _read_repo_doc("docs/ARCHITECTURE.md")
    state_doc = _read_repo_doc("_state.md")

    def act(action: str, params: dict[str, Any]) -> None:
        nonlocal run
        run += 1
        _submit(runtime, f"CC-BOARD-{run:03d}", action, params, BOARD_MANAGER)

    act(
        "create_board",
        {
            "board_key": BOARD_KEY,
            "title": "多智能体任务看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
            "description": "列拓扑即数据：谁可认领、哪列是终态、失败回哪列，全部声明式。",
            "goal": arch_doc
            or "四层元模型（M2→M1→PSM→M0）单向依赖，M2 内核零业务污染，脑手分离协同。",
            # 建板只是初始化；首个完工才会把完整基线真值表折叠进 Board.state。
            "state": "初始化：看板已建立，等待首个完工基线。",
            "columns": BOARD_COLUMNS,
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
            "board_key": "BD-CC-001",
            "title": "任务看板本体与宿主胶水",
            "body": "本体负责认领与裁决，宿主负责执行。",
        },
    )
    act(
        "create_card",
        {
            "card_id": "C-101",
            "board_key": "BD-CC-001",
            "title": "PSM 接入 CodeBuddy 与 OpenCode",
            "body": "两个宿主统一返回结构化裁决。",
        },
    )
    act(
        "create_card",
        {
            "card_id": "C-102",
            "board_key": "BD-CC-001",
            "title": "编排器认领超时回收",
            "body": "技术执行账本的后续能力。",
        },
    )
    act(
        "create_card",
        {
            "card_id": "C-103",
            "board_key": "BD-CC-001",
            "title": "看板流程视图",
            "body": "把列拓扑画成可读的流程图。",
            "waits_for": ("C-101",),
        },
    )

    # 一张已完成的卡（带数字证据）、一张在途卡、一张阻塞卡。
    act("claim_card", {"card_id": "C-100", "member_id": "M-DEV-01"})
    act(
        "record_outcome",
        {
            "card_id": "C-100",
            "member_id": "M-DEV-01",
            "outcome": "completed",
            "summary": "本体与宿主胶水跑通",
            "evidence": [
                {"metric": "测试条数", "value": "17 passed", "detail": ""},
                {"metric": "新模块行数", "value": "627 行", "detail": ""},
            ],
            "artifact_uri": "open_ontology/agent_hosts.py",
            # 首个完工把完整基线真值表作为新 state 折叠进来（与初始化占位不同）。
            "board_state": state_doc
            or "微内核 1,400 行 + 7 大系统调用切面，全仓测试 430+ passed，看板流自举就绪。",
            "goal_changed": False,
            "goal_review_reason": "完工未改变架构目标，沿用建板时的架构宪法。",
        },
    )
    act("claim_card", {"card_id": "C-102", "member_id": "M-DEV-02"})
    act(
        "add_comment",
        {
            "card_id": "C-102",
            "author": "M-REV-01",
            "body": "先做技术执行账本，别塞进业务对象。",
        },
    )
    act("claim_card", {"card_id": "C-101", "member_id": "M-DEV-01"})
    act(
        "record_outcome",
        {
            "card_id": "C-101",
            "member_id": "M-DEV-01",
            "outcome": "blocked",
            "summary": "等宿主额度窗口",
            "block_kind": "awaiting_review",
            "evidence": [
                {"metric": "已接通宿主数", "value": "2 个", "detail": ""}
            ],
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
        "多智能体任务看板",
        board_spec,
        category="Agentic Delivery",
        description="看板列拓扑即数据；认领、裁决、带数字的证据全部由本体判定。",
    )
    registry.register_instance(
        "board-prod",
        "task_board",
        "任务看板 · 生产实例",
        runtime,
        db_uri="sqlite://board_prod.sqlite3",
        description="4 名成员 + 4 张卡（已完成 / 执行中 / 阻塞 / 待前置）。",
        connectors=[
            ConnectorMount("CodeBuddy CLI", "Harness", "cli://codebuddy", "CONNECTED"),
            ConnectorMount("OpenCode HTTP", "Harness", "http://localhost:4096", "CONNECTED"),
        ],
        default_actor=BOARD_MANAGER,
        sample_action_params={
            "claim_card": {"card_id": "C-102", "member_id": "M-DEV-02"},
            "record_outcome": {
                "card_id": "C-102",
                "member_id": "M-DEV-02",
                "outcome": "completed",
                "summary": "回收策略已实现",
                "evidence": [
                    {"metric": "测试条数", "value": "9 passed", "detail": ""}
                ],
                "board_state": "新基线：回收策略已实现，9 passed。",
                "goal_changed": False,
                "goal_review_reason": "回收策略不改变架构目标。",
            },
            "create_card": {
                "card_id": "C-104",
                "board_key": "BD-CC-001",
                "title": "新卡片",
                "body": "在这里写任务说明。",
            },
            "add_comment": {
                "card_id": "C-102",
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
# 5. osv2_governance
# ---------------------------------------------------------------------------
osv2_spec = OntologySpec()
osv2_spec.interface("PricedAsset", ("asset_id", "unit_price_ton"), "可计价资产")


class SteelCoil(ObjectType):
    __primary_key__ = "asset_id"
    __title__ = "grade"
    __security_tag_fields__ = ("region",)
    __interfaces__ = ("PricedAsset",)
    __owned__ = {"quality_grade": "UNSET"}
    __masked_fields__ = {
        "unit_price_ton": ("finance",),
        "cost_note": ("finance", "auditor"),
        "internal_code": ("finance",),
    }
    __mask_defaults__ = {"internal_code": "REDACTED"}

    asset_id: str
    grade: str
    region: str = "north"
    unit_price_ton: float
    stock_tons: float = 0.0
    cost_note: str | None = None
    internal_code: str = "SECRET"
    quality_grade: str = "UNSET"


class ScrapLot(ObjectType):
    __primary_key__ = "asset_id"
    __title__ = "grade"
    __security_tag_fields__ = ("region",)
    __interfaces__ = ("PricedAsset",)
    __masked_fields__ = {"unit_price_ton": ("finance",)}

    asset_id: str
    grade: str
    region: str = "north"
    unit_price_ton: float
    stock_tons: float = 0.0


for _cls in (SteelCoil, ScrapLot):
    osv2_spec.object_type(_cls)
osv2_spec.add_link_type(
    LinkType(name="feeds", source="ScrapLot", target="SteelCoil", cardinality="many-to-many")
)


class Osv2SetGradeParams(BaseModel):
    asset_id: str
    quality_grade: str


class Osv2AdjustPriceParams(BaseModel):
    asset_id: str
    unit_price_ton: float


class Osv2EmptyParams(BaseModel):
    pass


@osv2_spec.function("portfolio_value", Osv2EmptyParams, description="库存货值合计（只读）")
def portfolio_value(ctx: Any, params: Osv2EmptyParams) -> dict[str, Any]:
    total = 0.0
    for coil in ctx.query(SteelCoil):
        total += coil.unit_price_ton * coil.stock_tons
    return {"total_value": round(total, 2)}


@osv2_spec.action(
    "set_quality_grade",
    Osv2SetGradeParams,
    allowed_roles=("ops", "finance", "admin"),
    affects_source=False,
)
def set_quality_grade(ctx: Any, params: Osv2SetGradeParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    ctx.modify(coil, quality_grade=params.quality_grade)


@osv2_spec.action(
    "adjust_price",
    Osv2AdjustPriceParams,
    allowed_roles=("ops", "finance", "admin"),
)
def adjust_price(ctx: Any, params: Osv2AdjustPriceParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    if params.unit_price_ton <= 0:
        raise InvariantError("price must be positive", code="INVALID_PRICE")
    ctx.modify(coil, unit_price_ton=params.unit_price_ton)


@osv2_spec.action(
    "request_price_change",
    Osv2AdjustPriceParams,
    allowed_roles=("ops", "finance", "admin"),
    approver_roles=("finance_manager",),
    approval_rule=lambda params, actor: params.unit_price_ton > 20000,
    description="超过 2 万元/吨的改价需要财务经理批准",
)
def request_price_change(ctx: Any, params: Osv2AdjustPriceParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    ctx.modify(coil, unit_price_ton=params.unit_price_ton)


_AUTO_IDS = iter(range(1, 10_000))


@osv2_spec.automation(
    "fill_missing_grade",
    SteelCoil,
    lambda coil: coil.quality_grade == "UNSET",
    description="发现未定级钢卷时自动提交定级动作",
)
def fill_missing_grade(runtime: OntologyRuntime, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    run = runtime.submit_action(
        f"CC-AUTO-GRADE-{next(_AUTO_IDS):04d}",
        "set_quality_grade",
        {"asset_id": coil.asset_id, "quality_grade": "PENDING_REVIEW"},
        actor,
    )
    return {"run_id": run["run_id"], "status": run["status"]}


@osv2_spec.automation(
    "log_low_stock",
    "SteelCoil",
    lambda coil: coil.stock_tons < 100,
    description="低库存只记录不改状态",
)
def log_low_stock(runtime: OntologyRuntime, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    return {"flagged": coil.asset_id, "stock_tons": coil.stock_tons}


@osv2_spec.automation(
    "escalate_premium_price",
    SteelCoil,
    lambda coil: coil.unit_price_ton > 20000,
    description="高价钢卷改价走审批流",
)
def escalate_premium_price(runtime: OntologyRuntime, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    run = runtime.submit_action(
        f"CC-AUTO-ESCALATE-{next(_AUTO_IDS):04d}",
        "request_price_change",
        {"asset_id": coil.asset_id, "unit_price_ton": coil.unit_price_ton + 100.0},
        actor,
    )
    return {"run_id": run["run_id"], "status": run["status"]}


OSV2_OPS = Actor(actor_id="cc-ops-01", role="ops", clearance_tags={"*"})
OSV2_FINANCE = Actor(actor_id="cc-fin-01", role="finance", clearance_tags={"*"})


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


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def build_default_command_center(
    data_dir: str | Path | None = None,
    *,
    reset: bool = False,
) -> CommandCenterRegistry:
    """预装 5 个 Ontology 蓝图 + 7 个真实数据 Instance。

    默认把 ``board-prod`` 的 SQLite 库落在 ``.instances/board_prod.sqlite3``，
    使实例真实落盘可被新会话按对象 ID 检索。

    **默认非破坏性**：库已存在且含看板时直接打开，原有对象值、版本、关系与
    动作账本全部保留。只有显式 ``reset=True`` 才会重建**指定的这一个库**
    （绝不删除目录）；传入不兼容 / 半成品 / 损坏的库会明确报错。
    """
    registry = CommandCenterRegistry()
    _build_software_factory(registry)
    _build_task_board(
        registry,
        db_path=Path(data_dir or _DEFAULT_DATA_DIR) / "board_prod.sqlite3",
        reset=reset,
    )
    _build_coldchain(registry)
    _build_saas(registry)
    _build_steel(registry)
    build_osv2_governance(registry)
    # 第 7 个实例：冷链 MCTS 实验室（毫秒级 fork，破坏性实验不碰生产）。
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
    args = parser.parse_args(list(argv) if argv is not None else None)

    registry = build_default_command_center(reset=args.reset)
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
