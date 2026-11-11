"""OpenOntology 统一作战指挥中心 · 全量预装启动器。

一条命令拉起 5 个核心 Ontology 蓝图与 7 个真实数据实例：

1. ``software_factory``  AI 软件研发工厂（研发成员 / 工单 / 交付物）
2. ``coldchain_mcts``    冷链医药调度与 MCTS 多步规划
3. ``crm_saas_unplug``   多云 SaaS 寄居吸纳与脱壳
4. ``steel_mdm``         钢厂主数据破局与三视角对账
5. ``osv2_governance``   钢卷资产与 OSv2 四大原语治理

运行::

    python3 -m examples.command_center_demo --port 8765
"""

from __future__ import annotations

import argparse
import time
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
    "build_default_command_center",
    "build_osv2_governance",
    "coldchain_mcts_config",
    "main",
]


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
# 2. coldchain_mcts
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
def build_default_command_center() -> CommandCenterRegistry:
    """预装 5 个 Ontology 蓝图 + 7 个真实数据 Instance。"""
    registry = CommandCenterRegistry()
    _build_software_factory(registry)
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
    args = parser.parse_args(list(argv) if argv is not None else None)

    registry = build_default_command_center()
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
