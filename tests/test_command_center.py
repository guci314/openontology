"""TK-ONTO-018 验收测试：统一作战指挥中心 Command Center。

覆盖：
1. 5 大 Ontology 蓝图 + 7 个预装 Instance；
2. ``/api/catalog`` 与 ``/api/ontology/<id>`` 的元模型投影；
3. ``?instance=`` 多实例路由与世界状态隔离；
4. 单步沙盘（零副作用）与正式执行（落库）；
5. 冷链 MCTS 多步推演：最优序列 / 剪枝 / 主库零污染 / 一键提交；
6. Instance Fork、SaaS Unplug、Interface 多态、Functions、列级脱敏、审批与时光机；
7. 旧单 runtime 签名 100% 向后兼容。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

from open_ontology import (
    Actor,
    CommandCenterRegistry,
    LinkType,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    WorkshopServer,
    start_command_center_server,
    start_workshop_server,
)
from examples.command_center_demo import (
    build_default_command_center,
    osv2_spec,
)

OPTIMAL_SEQUENCE = ["ORD-1->TRK-HEAVY", "ORD-2->TRK-FAST", "ORD-3->TRK-MID"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _request(url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(base: str, path: str, **params) -> dict:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return _request(f"{base}{path}" + (f"?{query}" if query else ""))


def _post(base: str, path: str, body: dict) -> dict:
    return _request(f"{base}{path}", body)


@pytest.fixture()
def center(tmp_path):
    # 每个测试一个独立数据目录：绝不触碰仓库的 .instances 生产库。
    registry = build_default_command_center(data_dir=tmp_path)
    handle = start_command_center_server(registry, port=0, title="测试指挥中心")
    yield registry, handle
    handle.stop()


@pytest.fixture()
def base(center):
    return center[1].base_url


# ---------------------------------------------------------------------------
# 1. 预装蓝图与实例
# ---------------------------------------------------------------------------
def test_default_command_center_blueprints_and_instances(center):
    registry, _ = center
    assert list(registry.ontologies) == [
        "software_factory",
        "task_board",
        "coldchain_mcts",
        "crm_saas_unplug",
        "steel_mdm",
        "osv2_governance",
    ]
    assert len(registry.instances) == 8
    assert set(registry.instances) == {
        "factory-prod",
        "board-prod",
        "coldchain-wave3",
        "coldchain-wave3-lab",
        "saas-parasitic",
        "saas-standalone",
        "steel-mill-north",
        "osv2-coil-prod",
    }
    coldchain = registry.get_instance("coldchain-wave3")
    assert coldchain.mcts_config is not None
    assert len(coldchain.connectors) == 3
    saas = registry.get_instance("saas-parasitic")
    assert saas.unplug_fn is not None


# ---------------------------------------------------------------------------
# 2. /api/catalog
# ---------------------------------------------------------------------------
def test_catalog_payload(base):
    data = _get(base, "/api/catalog")
    assert data["ok"] is True
    assert len(data["ontologies"]) == 6
    assert len(data["instances"]) == 8

    by_id = {row["instance_id"]: row for row in data["instances"]}
    assert by_id["coldchain-wave3"]["db_uri"] == "sqlite://coldchain_wave3.sqlite3"
    assert [c["kind"] for c in by_id["coldchain-wave3"]["connectors"]] == [
        "WMS",
        "SaaS",
        "SaaS",
    ]
    assert by_id["coldchain-wave3"]["supports_mcts"] is True
    assert by_id["saas-parasitic"]["supports_unplug"] is True
    assert by_id["factory-prod"]["counts"]["objects"] >= 9

    ontology = {row["ontology_id"]: row for row in data["ontologies"]}
    assert ontology["software_factory"]["counts"]["object_types"] == 3
    assert ontology["osv2_governance"]["counts"]["interfaces"] == 1
    assert ontology["crm_saas_unplug"]["counts"]["instances"] == 2


# ---------------------------------------------------------------------------
# 3. /api/ontology/<id>
# ---------------------------------------------------------------------------
def test_ontology_detail_metamodel(base):
    data = _get(base, "/api/ontology/software_factory")
    assert data["ontology"]["ontology_id"] == "software_factory"
    assert {t["name"] for t in data["object_types"]} == {
        "Engineer",
        "WorkTicket",
        "Deliverable",
    }
    assert {l["name"] for l in data["link_types"]} == {
        "ticket_assigned_to",
        "ticket_reviewed_by",
        "ticket_deliverables",
    }
    assert {"recruit_engineer", "dispatch_ticket"} <= {
        a["name"] for a in data["actions"]
    }
    assert {"factory_board_summary", "build_engineer_briefing"} <= {
        f["name"] for f in data["functions"]
    }
    work_ticket = next(t for t in data["object_types"] if t["name"] == "WorkTicket")
    assert work_ticket["primary_key"] == "ticket_id"
    assert "stage" in work_ticket["owned_fields"]

    osv2 = _get(base, "/api/ontology/osv2_governance")
    assert osv2["interfaces"][0]["name"] == "PricedAsset"
    assert osv2["interfaces"][0]["implementations"] == ["SteelCoil", "ScrapLot"]
    coil = next(t for t in osv2["object_types"] if t["name"] == "SteelCoil")
    masked = {f["name"] for f in coil["fields"] if f["masked"]}
    assert {"unit_price_ton", "cost_note", "internal_code"} <= masked


# ---------------------------------------------------------------------------
# 4. 多实例世界状态隔离
# ---------------------------------------------------------------------------
def test_instance_world_state_isolation(base):
    parasitic = _get(base, "/api/objects/Customer", instance="saas-parasitic")
    standalone = _get(base, "/api/objects/Customer", instance="saas-standalone")
    assert parasitic["count"] == 4
    assert standalone["count"] == 3
    assert {row["customer_id"] for row in parasitic["objects"]} != {
        row["customer_id"] for row in standalone["objects"]
    }

    coldchain = _get(base, "/api/objects/EmergencyOrder", instance="coldchain-wave3")
    factory = _get(base, "/api/objects/WorkTicket", instance="factory-prod")
    assert coldchain["count"] == 3
    assert factory["count"] == 3

    with pytest.raises(urllib.error.HTTPError):
        _get(base, "/api/objects/EmergencyOrder", instance="factory-prod")


# ---------------------------------------------------------------------------
# 5. 单步沙盘 / 正式执行
# ---------------------------------------------------------------------------
def test_simulate_is_side_effect_free(base):
    before = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    sim = _post(
        base,
        "/api/simulate?instance=coldchain-wave3",
        {
            "instance": "coldchain-wave3",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )
    assert sim["valid"] is True
    assert sim["requires_approval"] is False
    assert sim["diff"]["object_changes"]
    after = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    assert before == after

    order = _get(base, "/api/objects/EmergencyOrder", instance="coldchain-wave3")
    row = next(o for o in order["objects"] if o["order_id"] == "ORD-1")
    assert row["status"] == "pending"


def test_submit_updates_instance_world(base):
    before = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    result = _post(
        base,
        "/api/submit?instance=coldchain-wave3",
        {
            "instance": "coldchain-wave3",
            "run_id": "CC-TEST-SUBMIT-1",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )
    assert result["run"]["status"] == "COMMITTED"

    after = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    assert after["runs"] == before["runs"] + 1
    assert after["versions"]["EmergencyOrder:ORD-1"] > before["versions"]["EmergencyOrder:ORD-1"]
    assert _get(base, "/api/runs", instance="coldchain-wave3")["runs"][-1]["status"] == "COMMITTED"

    order = _get(base, "/api/objects/EmergencyOrder", instance="coldchain-wave3")
    row = next(o for o in order["objects"] if o["order_id"] == "ORD-1")
    assert row["status"] == "assigned"
    assert row["assigned_truck_id"] == "TRK-HEAVY"

    truck = _get(base, "/api/objects/ColdTruck", instance="coldchain-wave3")
    chosen = next(t for t in truck["objects"] if t["truck_id"] == "TRK-HEAVY")
    assert chosen["status"] == "assigned"


# ---------------------------------------------------------------------------
# 6. MCTS 多步沙盘
# ---------------------------------------------------------------------------
def test_mcts_best_plan_and_zero_pollution(base):
    before = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    data = _post(
        base,
        "/api/instance/mcts",
        {"instance": "coldchain-wave3", "num_simulations": 80},
    )
    assert data["ok"] is True
    assert data["best_value"] == 44600
    assert data["best_sequence"] == OPTIMAL_SEQUENCE
    assert len(data["root_stats"]) == 3
    assert {row["label"] for row in data["root_stats"]} == {
        "ORD-1->TRK-FAST",
        "ORD-1->TRK-MID",
        "ORD-1->TRK-HEAVY",
    }
    assert all(row["visits"] >= 0 for row in data["root_stats"])
    assert data["pruned_illegal_nodes"] > 0
    assert isinstance(data["pruned_branches"], list)

    after = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    assert before == after  # 搜索期间主库零写入


def test_mcts_commit_updates_world(base):
    data = _post(
        base,
        "/api/instance/mcts",
        {"instance": "coldchain-wave3", "num_simulations": 80, "commit": True},
    )
    assert len(data["committed_runs"]) == 3
    assert data["best_sequence"] == OPTIMAL_SEQUENCE

    orders = _get(base, "/api/objects/EmergencyOrder", instance="coldchain-wave3")
    status = {row["order_id"]: row["status"] for row in orders["objects"]}
    assert status == {"ORD-1": "assigned", "ORD-2": "assigned", "ORD-3": "assigned"}

    value = _post(
        base,
        "/api/function",
        {
            "instance": "coldchain-wave3",
            "function": "evaluate_world_value",
            "params": {"label": "after_commit"},
        },
    )
    assert value["result"]["total_value"] == 44600.0
    assert value["result"]["fulfilled"] == 3


# ---------------------------------------------------------------------------
# 7. Instance Fork
# ---------------------------------------------------------------------------
def test_fork_instance_appears_in_catalog(base):
    forked = _post(
        base,
        "/api/instance/fork",
        {"instance": "coldchain-wave3", "label": "测试沙盘分支"},
    )
    new_id = forked["instance"]["instance_id"]
    assert new_id.startswith("coldchain-wave3-fork-")
    assert forked["instance"]["counts"]["objects"] == 6

    catalog = _get(base, "/api/catalog")
    ids = {row["instance_id"] for row in catalog["instances"]}
    assert new_id in ids

    # 沙盘内写入不影响母体
    _post(
        base,
        "/api/submit",
        {
            "instance": new_id,
            "run_id": "FORK-WRITE-1",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )
    mother = _get(base, "/api/objects/EmergencyOrder", instance="coldchain-wave3")
    row = next(o for o in mother["objects"] if o["order_id"] == "ORD-1")
    assert row["status"] == "pending"
    child = _get(base, "/api/objects/EmergencyOrder", instance=new_id)
    row = next(o for o in child["objects"] if o["order_id"] == "ORD-1")
    assert row["status"] == "assigned"


# ---------------------------------------------------------------------------
# 8. SaaS Unplug
# ---------------------------------------------------------------------------
def test_unplug_marks_connectors(base):
    data = _post(
        base, "/api/instance/unplug", {"instance": "saas-parasitic"}
    )
    assert data["ok"] is True
    assert data["result"]["mode"] == "standalone-ready"
    assert {c["status"] for c in data["connectors"]} == {"UNPLUGGED"}

    catalog = _get(base, "/api/catalog")
    entry = next(
        row for row in catalog["instances"] if row["instance_id"] == "saas-parasitic"
    )
    assert {c["status"] for c in entry["connectors"]} == {"UNPLUGGED"}


# ---------------------------------------------------------------------------
# 9. 前端主界面
# ---------------------------------------------------------------------------
def test_command_center_html_markers(base):
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
    lowered = html.lower()
    for marker in (
        "<!doctype html>",
        "统一作战指挥中心",
        "abox · world state",  # C-WS-05：事实面板改标 ABox，原 OBJECT EXPLORER 退役
        "governed action",
        "action runs",
        "当前状态指纹",
        "本体蓝图列表",
        "运行实例列表",
        "/api/catalog",
        "/api/instance/mcts",
        "/api/instance/fork",
        "/api/instance/unplug",
        "/api/simulate",
        "/api/submit",
        "/api/automations/run",
        "自动化规则",
        "mcts",
        "fork",
    ):
        assert marker.lower() in lowered, marker


# ---------------------------------------------------------------------------
# 10. 旧单 runtime 签名兼容
# ---------------------------------------------------------------------------
def test_legacy_single_runtime_compatibility():
    runtime = OntologyRuntime(osv2_spec)
    handle = start_workshop_server(runtime, port=0, title="单实例兼容")
    try:
        catalog = _get(handle.base_url, "/api/catalog")
        assert len(catalog["ontologies"]) == 1
        assert len(catalog["instances"]) == 1
        assert catalog["instances"][0]["instance_id"] == "default"
        meta = _get(handle.base_url, "/api/meta")
        assert {t["name"] for t in meta["object_types"]} == {"SteelCoil", "ScrapLot"}
        assert meta["instance"]["instance_id"] == "default"
    finally:
        handle.stop()

    # 直接构造 WorkshopServer 也保持旧签名
    runtime2 = OntologyRuntime(osv2_spec)
    server = WorkshopServer(("127.0.0.1", 0), runtime2, title="直接构造")
    assert server.runtime is runtime2
    assert server.registry.default_instance().runtime is runtime2
    server.server_close()


# ---------------------------------------------------------------------------
# 11. Interface 多态 + Functions
# ---------------------------------------------------------------------------
def test_interface_query_and_function_call(base):
    data = _get(
        base, "/api/interface", instance="osv2-coil-prod", interface="PricedAsset"
    )
    assert data["count"] == 3
    assert {row["asset_id"] for row in data["objects"]} == {"C-1", "C-2", "LOT-1"}

    result = _post(
        base,
        "/api/function",
        {
            "instance": "osv2-coil-prod",
            "function": "portfolio_value",
            "params": {},
            "role": "finance",
        },
    )
    assert result["result"]["total_value"] == 4_080_000.0


# ---------------------------------------------------------------------------
# 12. 列级脱敏按角色生效
# ---------------------------------------------------------------------------
def test_masking_by_role(base):
    ops_view = _get(
        base, "/api/objects/SteelCoil", instance="osv2-coil-prod", role="ops"
    )
    coil = next(row for row in ops_view["objects"] if row["asset_id"] == "C-1")
    assert coil["unit_price_ton"] == 0.0
    assert coil["cost_note"] is None
    assert coil["internal_code"] == "REDACTED"

    finance_view = _get(
        base, "/api/objects/SteelCoil", instance="osv2-coil-prod", role="finance"
    )
    coil = next(row for row in finance_view["objects"] if row["asset_id"] == "C-1")
    assert coil["unit_price_ton"] == 18000.0
    assert coil["cost_note"] == "主力现货"
    assert coil["internal_code"] == "NC-304"


# ---------------------------------------------------------------------------
# 13. 审批与时光机
# ---------------------------------------------------------------------------
def test_approve_and_revert_flow(base):
    submitted = _post(
        base,
        "/api/submit",
        {
            "instance": "osv2-coil-prod",
            "run_id": "CC-APPROVE-1",
            "action": "request_price_change",
            "params": {"asset_id": "C-2", "unit_price_ton": 24500.0},
        },
    )
    assert submitted["run"]["status"] == "PENDING_APPROVAL"

    approved = _post(
        base,
        "/api/approve?role=finance_manager",
        {"instance": "osv2-coil-prod", "run_id": "CC-APPROVE-1"},
    )
    assert approved["run"]["status"] == "COMMITTED"

    coil = _get(
        base, "/api/objects/SteelCoil", instance="osv2-coil-prod", role="finance"
    )
    row = next(o for o in coil["objects"] if o["asset_id"] == "C-2")
    assert row["unit_price_ton"] == 24500.0

    reverted = _post(
        base,
        "/api/revert?role=finance_manager",
        {"instance": "osv2-coil-prod", "run_id": "CC-APPROVE-1"},
    )
    assert reverted["run"]["status"] == "COMMITTED"
    coil = _get(
        base, "/api/objects/SteelCoil", instance="osv2-coil-prod", role="finance"
    )
    row = next(o for o in coil["objects"] if o["asset_id"] == "C-2")
    assert row["unit_price_ton"] == 24000.0


# ---------------------------------------------------------------------------
# 14. 研发工厂实例的真实数据
# ---------------------------------------------------------------------------
def test_factory_instance_board_data(base):
    tickets = _get(base, "/api/objects/WorkTicket", instance="factory-prod")
    stages = {row["ticket_id"]: row["stage"] for row in tickets["objects"]}
    assert stages["TK-CC-101"] == "CODING"
    assert stages["TK-CC-102"] == "OPEN"
    assert stages["TK-CC-103"] == "DONE"

    engineers = _get(base, "/api/objects/Engineer", instance="factory-prod")
    assert engineers["count"] == 5
    assert {row["role"] for row in engineers["objects"]} == {
        "architect",
        "coder",
        "tester",
    }

    deliverables = _get(base, "/api/objects/Deliverable", instance="factory-prod")
    assert deliverables["count"] == 1
    assert deliverables["objects"][0]["verified"] is True


def test_seeded_board_goal_combines_architecture_and_task_board_docs(base):
    """预装看板的 goal 同时承载 M2 宪法与 M1/M0 看板规范（单文件目录仍兼容）。"""
    repo_root = Path(__file__).resolve().parent.parent
    architecture = (repo_root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8").strip()
    task_board = (repo_root / "docs" / "TASK_BOARD.md").read_text(encoding="utf-8").strip()

    payload = _get(base, "/api/objects/Board", instance="board-prod")
    boards = {item["board_key"]: item for item in payload["objects"]}
    goal = boards["BD-CC-001"]["goal"]

    assert goal == architecture + "\n\n---\n\n" + task_board
    assert goal.startswith("# OpenOntology 架构设计规范")
    assert goal.endswith(task_board[-40:])


# ---------------------------------------------------------------------------
# 15. 四元模型：Runtime 宿体 + 符号 / 联结双设备注册表
# ---------------------------------------------------------------------------
def test_catalog_carries_runtimes_and_dual_device_registry(base):
    data = _get(base, "/api/catalog")

    runtimes = {row["runtime_id"]: row for row in data["runtimes"]}
    assert len(data["runtimes"]) == 9  # 1 台预注册内存宿体 + 8 个实例各一台
    assert "rt-default-memory" in runtimes

    default = runtimes["rt-default-memory"]
    assert default["storage_kind"] == "memory"
    assert default["db_uri"] == "sqlite://:memory:"
    assert len(default["aspects"]) == 7
    assert "IdleAwareCallbackAspect" in default["aspects"]

    coldchain = runtimes["rt-coldchain-wave3"]
    assert coldchain["storage_kind"] == "sqlite"
    assert coldchain["db_uri"] == "sqlite://coldchain_wave3.sqlite3"
    assert coldchain["instance_ids"] == ["coldchain-wave3"]
    assert coldchain["ontology_ids"] == ["coldchain_mcts"]
    assert set(coldchain["adapters"]) == {"WMS 仓储", "TMS 运输", "温控遥测"}

    symbolic = data["devices"]["symbolic"]
    connectionist = data["devices"]["connectionist"]
    assert symbolic and connectionist

    wms = next(d for d in symbolic if d["name"] == "WMS 仓储")
    assert wms["kind"] == "WMS"
    assert wms["status"] == "CONNECTED"
    assert wms["openapi_schema"]["openapi"] == "3.1.0"
    assert {"info", "paths"} <= set(wms["openapi_schema"])
    assert all(d["kind"] not in {"Harness", "Agent", "LLM"} for d in symbolic)

    card = next(d for d in connectionist if d["name"] == "CodeBuddy CLI")
    assert card["kind"] == "Harness"
    assert card["agent_card"]["protocol"] == "A2A/1.0"
    assert card["agent_card"]["roles"] == ["developer", "tester", "architect"]
    assert card["agent_card"]["output_contract"] == "HostResult + Numeric Evidence"
    assert all(d["kind"] in {"Harness", "Agent", "LLM"} for d in connectionist)


def test_runtime_and_device_endpoints(base):
    runtimes = _get(base, "/api/runtimes")
    assert runtimes["ok"] is True
    ids = {row["runtime_id"] for row in runtimes["runtimes"]}
    assert {"rt-default-memory", "rt-factory-prod", "rt-coldchain-wave3"} <= ids

    devices = _get(base, "/api/devices")
    assert devices["ok"] is True
    assert {"WMS", "SaaS"} <= {d["kind"] for d in devices["symbolic"]}
    assert {d["kind"] for d in devices["connectionist"]} == {"Harness"}
    assert all("openapi_schema" in d for d in devices["symbolic"])
    assert all("agent_card" in d for d in devices["connectionist"])


def test_instance_summary_exposes_runtime_binding(base):
    data = _get(base, "/api/catalog")
    by_id = {row["instance_id"]: row for row in data["instances"]}

    coldchain = by_id["coldchain-wave3"]
    assert coldchain["runtime_id"] == "rt-coldchain-wave3"
    assert coldchain["formula"] == (
        "coldchain-wave3 = coldchain_mcts + rt-coldchain-wave3"
    )

    meta = _get(base, "/api/meta", instance="factory-prod")
    assert meta["instance"]["runtime_id"] == "rt-factory-prod"
    assert meta["instance"]["formula"] == (
        "factory-prod = software_factory + rt-factory-prod"
    )


def test_fork_registers_its_own_runtime_and_devices(base):
    forked = _post(
        base,
        "/api/instance/fork",
        {"instance": "coldchain-wave3", "label": "设备沙盘分支"},
    )
    new_id = forked["instance"]["instance_id"]
    assert forked["instance"]["runtime_id"] == f"rt-{new_id}"
    assert forked["instance"]["formula"] == (
        f"{new_id} = coldchain_mcts + rt-{new_id}"
    )

    runtimes = {row["runtime_id"]: row for row in _get(base, "/api/runtimes")["runtimes"]}
    forked_runtime = runtimes[f"rt-{new_id}"]
    assert forked_runtime["storage_kind"] == "memory"  # 克隆沙盘纯内存
    assert forked_runtime["instance_ids"] == [new_id]
    assert len(forked_runtime["aspects"]) == 7

    devices = _get(base, "/api/devices")
    cloned = [
        d for d in devices["symbolic"] if d["device_id"].startswith(f"dev-{new_id}-")
    ]
    assert len(cloned) == 3
    assert all(d["openapi_schema"]["openapi"] == "3.1.0" for d in cloned)


def test_model_and_workspaces_additive_endpoints_and_repeated_query_conflict(base):
    model_detail = _get(base, "/api/model/task_board")
    onto_detail = _get(base, "/api/ontology/task_board")
    assert model_detail["ok"] is True
    assert model_detail["model"] == model_detail["ontology"]
    assert model_detail["object_types"] == onto_detail["object_types"]

    ws_payload = _get(base, "/api/workspaces", role="board_manager")
    assert ws_payload["ok"] is True
    by_ws = {row["workspace_id"]: row for row in ws_payload["workspaces"]}
    assert "board-prod" in by_ws
    assert [r["object_id"] for r in by_ws["board-prod"]["aggregate_roots"]] == ["BD-CC-001"]

    forked = _post(
        base,
        "/api/workspace/fork",
        {"workspace": "factory-prod", "label": "工作空间沙盘分支"},
    )
    assert forked["ok"] is True
    assert forked["workspace"] == forked["instance"]

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _request(f"{base}/api/meta?workspace=board-prod&workspace=factory-prod")
    assert excinfo.value.code == 400
    excinfo.value.close()



# ---------------------------------------------------------------------------
# 15. C-KERNEL-SEC-01：外部边界的 RLS / 列脱敏闭合
# ---------------------------------------------------------------------------
#: 只有 north 区域可见的受限调用方。
ACTOR_SCOPE = Actor(
    actor_id="scope-north", role="ops", clearance_tags={"region:north"}
)


def test_links_endpoint_hides_rows_the_actor_cannot_see():
    """``/api/links`` 不得把 RLS 隐藏对象、悬空端点暴露给调用方。"""
    spec = OntologySpec()

    class Node(ObjectType):
        __primary_key__ = "node_id"
        __security_tag_fields__ = ("region",)

        node_id: str
        region: str = "north"

    spec.object_type(Node)
    spec.add_link_type(
        LinkType(
            name="node_links",
            source="Node",
            target="Node",
            cardinality="many-to-many",
            owned=False,
        )
    )

    runtime = OntologyRuntime(spec)
    try:
        for node in (
            Node(node_id="N-NORTH-1", region="north"),
            Node(node_id="N-NORTH-2", region="north"),
            Node(node_id="N-SOUTH-1", region="south"),
        ):
            runtime.put_object(node)
        north_1 = runtime.get_object(Node, "N-NORTH-1", ACTOR_SCOPE)
        north_2 = runtime.get_object(Node, "N-NORTH-2", ACTOR_SCOPE)
        south_1 = Node(node_id="N-SOUTH-1", region="south")
        runtime.link_objects(north_1, "node_links", north_2)
        runtime.link_objects(north_1, "node_links", south_1)
        # 悬空边：目标对象从未落库
        runtime.link_objects(
            north_1, "node_links", Node(node_id="N-GHOST", region="north")
        )

        registry = CommandCenterRegistry.from_runtime(runtime, actor=ACTOR_SCOPE)
        handle = start_command_center_server(registry, port=0, title="RLS 链路")
        try:
            payload = _get(handle.base_url, "/api/links")
        finally:
            handle.stop()
    finally:
        runtime.close()

    assert payload["ok"] is True
    pairs = {(row["source_id"], row["target_id"]) for row in payload["links"]}
    assert pairs == {("N-NORTH-1", "N-NORTH-2")}
    assert payload["count"] == 1


def test_function_reads_are_masked_for_non_whitelisted_roles(base):
    """``/api/function`` 用调用方 Actor 执行，列级白名单外必须脱敏。"""
    def portfolio(role: str | None) -> float:
        body = {
            "instance": "osv2-coil-prod",
            "function": "portfolio_value",
            "params": {},
        }
        if role is not None:
            body["role"] = role
        return _post(base, "/api/function", body)["result"]["total_value"]

    assert portfolio("finance") == 4_080_000.0
    assert portfolio("ops") == 0.0
    assert portfolio("admin") == 0.0


def test_http_boundary_enumeration_honours_explicit_actor():
    """裸 HTTP 边界的枚举接口在给了 Actor 时必须走 RLS + 脱敏投影。"""
    from open_ontology.http_server import start_http_server

    spec = OntologySpec()

    class Node(ObjectType):
        __primary_key__ = "node_id"
        __security_tag_fields__ = ("region",)
        __masked_fields__ = {"secret": ("auditor",)}

        node_id: str
        region: str = "north"
        secret: str = "s3cr3t"

    spec.object_type(Node)
    spec.add_link_type(
        LinkType(
            name="node_links",
            source="Node",
            target="Node",
            cardinality="many-to-many",
        )
    )

    runtime = OntologyRuntime(spec)
    try:
        runtime.put_object(Node(node_id="N-1", region="north"))
        runtime.put_object(Node(node_id="N-2", region="south"))
        handle = start_http_server(
            runtime,
            actor=Actor(
                actor_id="audit", role="auditor", clearance_tags={"region:north"}
            ),
        )
        try:
            rows = _get(handle.base_url, "/api/objects/Node")
        finally:
            handle.stop()
    finally:
        runtime.close()

    assert [row["object_id"] for row in rows] == ["N-1"]
    assert rows[0]["properties"]["secret"] == "s3cr3t"  # auditor 在白名单内
