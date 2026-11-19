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
    ObjectProperty,
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
        "o2c_order_to_cash",
    ]
    assert len(registry.instances) == 9
    assert set(registry.instances) == {
        "factory-prod",
        "board-prod",
        "coldchain-wave3",
        "coldchain-wave3-lab",
        "saas-parasitic",
        "saas-standalone",
        "steel-mill-north",
        "osv2-coil-prod",
        "o2c-erp-north",
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
    assert len(data["ontologies"]) == 7
    assert len(data["instances"]) == 9

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
    assert {l["name"] for l in data["object_properties"]} == {
        "Engineer.active_ticket",
        "WorkTicket.assignee",
        "WorkTicket.reviewer",
        "Deliverable.ticket",
        "Deliverable.engineer",
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
        "世界状态列表",
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


# ---------------------------------------------------------------------------
# 15. 四元模型：Runtime 宿体 + 外部设备（OpenAPI）注册表
# ---------------------------------------------------------------------------
def test_catalog_carries_runtimes_and_dual_device_registry(base):
    data = _get(base, "/api/catalog")

    runtimes = {row["runtime_id"]: row for row in data["runtimes"]}
    assert len(data["runtimes"]) == 10  # 1 台预注册内存宿体 + 9 个实例各一台
    assert "rt-default-memory" in runtimes

    default = runtimes["rt-default-memory"]
    assert default["storage_kind"] == "memory"
    assert default["db_uri"] == "sqlite://:memory:"
    assert len(default["aspects"]) == 8
    assert "IdleAwareCallbackAspect" in default["aspects"]
    assert "HomomorphicSettlementAspect" in default["aspects"]

    coldchain = runtimes["rt-coldchain-wave3"]
    assert coldchain["storage_kind"] == "sqlite"
    assert coldchain["db_uri"] == "sqlite://coldchain_wave3.sqlite3"
    assert coldchain["instance_ids"] == ["coldchain-wave3"]
    assert coldchain["ontology_ids"] == ["coldchain_mcts"]
    assert set(coldchain["adapters"]) == {"WMS 仓储", "TMS 运输", "温控遥测"}

    symbolic = data["devices"]["symbolic"]
    assert symbolic
    assert "connectionist" not in data["devices"]

    wms = next(d for d in symbolic if d["name"] == "WMS 仓储")
    assert wms["kind"] == "WMS"
    assert wms["status"] == "CONNECTED"
    assert wms["openapi_schema"]["openapi"] == "3.1.0"
    assert {"info", "paths"} <= set(wms["openapi_schema"])


def test_runtime_and_device_endpoints(base):
    runtimes = _get(base, "/api/runtimes")
    assert runtimes["ok"] is True
    ids = {row["runtime_id"] for row in runtimes["runtimes"]}
    assert {"rt-default-memory", "rt-factory-prod", "rt-coldchain-wave3"} <= ids

    devices = _get(base, "/api/devices")
    assert devices["ok"] is True
    assert {"WMS", "SaaS"} <= {d["kind"] for d in devices["symbolic"]}
    assert "connectionist" not in devices
    assert all("openapi_schema" in d for d in devices["symbolic"])


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
    assert len(forked_runtime["aspects"]) == 8

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
    spec.add_object_property(
        ObjectProperty(
            name="node_links",
            domain="Node",
            range="Node",
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
    spec.add_object_property(
        ObjectProperty(
            name="node_links",
            domain="Node",
            range="Node",
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


def test_simulation_preview_ui_hooks_and_zero_write_diff(base):
    """沙盘预演不正式执行时，前端应具备左侧世界状态预览渲染钩子且主库零写入。"""
    import urllib.request

    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        html = resp.read().decode("utf-8")

    for marker in (
        "stateTabs",
        "renderStateTabs",
        "pickStateTab",
        "当前业务状态",
        "沙盘推演状态",
        "applySimPreview",
        "clearSimPreview",
        "mergeObjectChanges",
        "mergeLinkChanges",
        "sim-banner",
        "sim-create",
        "sim-update",
        "sim-delete",
        "sim-link-create",
        "sim-link-delete",
        "renderObjectTabs",
    ):
        assert marker in html

    before_fp = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    sim = _post(
        base,
        "/api/simulate?instance=coldchain-wave3",
        {
            "instance": "coldchain-wave3",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )
    assert sim["ok"] is True
    assert sim["valid"] is True
    assert sim["diff"]["object_changes"]
    assert sim["diff"]["link_changes"]
    after_fp = _get(base, "/api/fingerprint", instance="coldchain-wave3")
    assert after_fp == before_fp


def test_sequential_sandbox_simulate_add_comment_creates_distinct_ids(base):
    """连续多次沙盘推演追加卡片评论时，应基于前序沙盘状态生成递增的评论 ID 且主库零写入。"""
    before_fp = _get(base, "/api/fingerprint", instance="board-prod")
    cards = _get(base, "/api/objects/Card", instance="board-prod")["objects"]
    card_id = cards[0]["card_id"]

    step1_params = {"card": card_id, "author": "alice", "body": "沙盘评论 1"}
    sim1 = _post(
        base,
        "/api/simulate?instance=board-prod",
        {"instance": "board-prod", "action": "add_comment", "params": step1_params},
    )
    assert sim1["valid"] is True
    id1 = sim1["diff"]["object_changes"][0]["object_id"]

    step2_params = {"card": card_id, "author": "bob", "body": "沙盘评论 2"}
    sim2 = _post(
        base,
        "/api/simulate?instance=board-prod",
        {
            "instance": "board-prod",
            "action": "add_comment",
            "params": step2_params,
            "prior_steps": [{"action": "add_comment", "params": step1_params}],
        },
    )
    assert sim2["valid"] is True
    id2 = sim2["diff"]["object_changes"][0]["object_id"]

    assert id1 != id2
    assert _get(base, "/api/fingerprint", instance="board-prod") == before_fp

    # 正式执行全部沙盘步骤：3 条评论应全部落库（而非只提交最后 1 条）
    step3_params = {"card": card_id, "author": "carol", "body": "沙盘评论 3"}
    submitted = _post(
        base,
        "/api/submit?instance=board-prod",
        {
            "instance": "board-prod",
            "steps": [
                {"action": "add_comment", "params": step1_params},
                {"action": "add_comment", "params": step2_params},
                {"action": "add_comment", "params": step3_params},
            ],
        },
    )
    assert submitted["ok"] is True
    assert len(submitted["runs"]) == 3
    comments = _get(base, "/api/objects/CardComment", instance="board-prod")["objects"]
    bodies = {c["body"] for c in comments}
    assert {"沙盘评论 1", "沙盘评论 2", "沙盘评论 3"} <= bodies


# ---------------------------------------------------------------------------
# 19. /api/worlds/{world}/openapi.json + REST 展开路由 + Actor Token 身份绑定
# ---------------------------------------------------------------------------
def test_world_openapi_role_scoping_and_actor_token_binding(center):
    registry, handle = center
    base = handle.base_url
    registry.register_actor_token(
        "tok-dev-01",
        Actor(actor_id="M-DEV-01", role="developer", clearance_tags={"tenant:acme"}),
        workspace_id="board-prod",
    )

    def _req(
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                **({"Content-Type": "application/json"} if payload is not None else {}),
                **(headers or {}),
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # 1) Token 强制锁定 role=developer：即使 query 尝试伪造 board_manager 也被忽略
    oa_dev = _req(
        "/api/worlds/board-prod/openapi.json?role=board_manager",
        headers={"Authorization": "Bearer tok-dev-01"},
    )
    assert oa_dev["openapi"] == "3.1.0"
    assert oa_dev["x-actor-role"] == "developer"
    assert "/api/worlds/board-prod/actions/claim_card" in oa_dev["paths"]
    assert "/api/worlds/board-prod/actions/record_outcome" in oa_dev["paths"]
    assert "/api/worlds/board-prod/actions/create_board" not in oa_dev["paths"]
    assert "/api/worlds/board-prod/functions/eligible_cards" in oa_dev["paths"]

    # 2) 无效 Token -> 401；跨工作空间 Token -> 403
    with pytest.raises(urllib.error.HTTPError) as exc401:
        _req("/api/worlds/board-prod", headers={"Authorization": "Bearer bogus"})
    assert exc401.value.code == 401
    exc401.value.close()

    with pytest.raises(urllib.error.HTTPError) as exc403:
        _req("/api/worlds/factory-prod", headers={"Authorization": "Bearer tok-dev-01"})
    assert exc403.value.code == 403
    exc403.value.close()

    # 3) 用管理员造一张 ready 卡片，然后让 M-DEV-01 通过 Bearer Token 查卡→dry_run→正式认领→汇报完成
    created = _req(
        "/api/worlds/board-prod/actions/create_card",
        method="POST",
        payload={"board": "BD-CC-001", "title": "OpenAPI 直连验证卡"},
        headers={"X-Actor-Id": "board-manager-01", "X-Actor-Role": "board_manager"},
    )
    assert created["run"]["status"] == "COMMITTED"
    card_id = created["run"]["object_diff"]["object_changes"][0]["object_id"]

    # 不可冒名认领他人席位 (ACTOR_MEMBER_MISMATCH)
    with pytest.raises(urllib.error.HTTPError) as exc_mismatch:
        _req(
            "/api/worlds/board-prod/actions/claim_card",
            method="POST",
            payload={"card": card_id, "member": "M-DEV-02"},
            headers={"Authorization": "Bearer tok-dev-01"},
        )
    assert exc_mismatch.value.code == 400
    assert "不能冒名操作成员" in exc_mismatch.value.read().decode("utf-8")
    exc_mismatch.value.close()

    # dry_run 预演
    dry = _req(
        "/api/worlds/board-prod/actions/claim_card?dry_run=true",
        method="POST",
        payload={"card": card_id, "member": "M-DEV-01"},
        headers={"Authorization": "Bearer tok-dev-01"},
    )
    assert dry["dry_run"] is True and dry["valid"] is True

    # 通过 OpenAPI 路径正式认领并读取单卡，然后 record_outcome 交付
    eligible = _req(
        "/api/worlds/board-prod/functions/eligible_cards",
        method="POST",
        payload={"board": "BD-CC-001", "role": "developer"},
        headers={"Authorization": "Bearer tok-dev-01"},
    )
    assert any(c["card_id"] == card_id for c in eligible["result"]["cards"])

    claimed = _req(
        "/api/worlds/board-prod/actions/claim_card",
        method="POST",
        payload={"card": card_id, "member": "M-DEV-01"},
        headers={"Authorization": "Bearer tok-dev-01"},
    )
    assert claimed["actor"] == {"actor_id": "M-DEV-01", "role": "developer"}
    assert claimed["run"]["status"] == "COMMITTED"

    card_row = _req(
        f"/api/worlds/board-prod/objects/Card/{card_id}",
        headers={"Authorization": "Bearer tok-dev-01"},
    )
    assert card_row["object"]["status"] == "doing"

    done_run = _req(
        "/api/worlds/board-prod/actions/record_outcome",
        method="POST",
        payload={
            "card": card_id,
            "member": "M-DEV-01",
            "outcome": "completed",
            "summary": "通过 OpenAPI 完成交付",
        },
        headers={"Authorization": "Bearer tok-dev-01"},
    )
    assert done_run["run"]["status"] == "COMMITTED"
    assert (
        _req(
            f"/api/worlds/board-prod/objects/Card/{card_id}",
            headers={"Authorization": "Bearer tok-dev-01"},
        )["object"]["status"]
        == "done"
    )


def test_kanban_end_to_end_lifecycle_updates_overview_and_promotes_children(center):
    """端到端覆盖：建板开卡（含 waits_for 父子依赖）→ 查可领卡 → 认领 → 交付 →
    验证 Card.status、Member.completed_cards/WIP、子卡自动解锁及 board_overview 汇总同步更新。
    """
    registry, handle = center
    base = handle.base_url
    registry.register_actor_token(
        "tok-mgr-e2e",
        Actor(actor_id="board-manager-01", role="board_manager", clearance_tags={"*"}),
        workspace_id="board-prod",
    )
    registry.register_actor_token(
        "tok-dev-e2e",
        Actor(actor_id="M-DEV-01", role="developer", clearance_tags={"tenant:acme"}),
        workspace_id="board-prod",
    )

    def _call(
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        token: str,
    ) -> dict:
        req = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {token}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _overview(board_key: str) -> dict:
        res = _call(
            "/api/worlds/board-prod/functions/board_overview",
            method="POST",
            payload={"board": board_key},
            token="tok-mgr-e2e",
        )["result"]
        return {
            **res,
            "counts": {row["key"]: row["cards"] for row in res["statuses"]},
            "member_map": {m["member_id"]: m for m in res["members"]},
        }

    # 1. 记录成员初始已完成计数与 WIP
    initial_dev = _call(
        "/api/worlds/board-prod/objects/Member/M-DEV-01", token="tok-mgr-e2e"
    )["object"]
    initial_completed = initial_dev["completed_cards"]

    # 2. 管理员建看板并开立父卡 + 依赖子卡
    _call(
        "/api/worlds/board-prod/actions/create_board",
        method="POST",
        payload={
            "board_key": "BD-E2E",
            "title": "端到端验收看板",
            "tenant": "acme",
            "created_by": "board-manager-01",
            "description": "验证从开卡到完成的状态联动与实时聚合",
        },
        token="tok-mgr-e2e",
    )
    parent_res = _call(
        "/api/worlds/board-prod/actions/create_card",
        method="POST",
        payload={"board": "BD-E2E", "title": "父任务", "priority": 10},
        token="tok-mgr-e2e",
    )
    parent_id = parent_res["run"]["object_diff"]["object_changes"][0]["object_id"]

    child_res = _call(
        "/api/worlds/board-prod/actions/create_card",
        method="POST",
        payload={
            "board": "BD-E2E",
            "title": "子任务",
            "priority": 5,
            "waits_for": [parent_id],
        },
        token="tok-mgr-e2e",
    )
    child_id = child_res["run"]["object_diff"]["object_changes"][0]["object_id"]

    ov0 = _overview("BD-E2E")
    assert ov0["cards_total"] == 2
    assert ov0["counts"] == {
        "todo": 1,
        "ready": 1,
        "doing": 0,
        "done": 0,
        "blocked": 0,
        "triage": 0,
    }

    # 3. 开发者仅能看到 ready 父卡，认领后进入 doing 且 active_wip=+1
    elig1 = _call(
        "/api/worlds/board-prod/functions/eligible_cards",
        method="POST",
        payload={"board": "BD-E2E", "role": "developer"},
        token="tok-dev-e2e",
    )["result"]
    assert [c["card_id"] for c in elig1["cards"]] == [parent_id]

    _call(
        "/api/worlds/board-prod/actions/claim_card",
        method="POST",
        payload={"card": parent_id, "member": "M-DEV-01"},
        token="tok-dev-e2e",
    )
    ov1 = _overview("BD-E2E")
    assert ov1["counts"]["doing"] == 1 and ov1["counts"]["ready"] == 0
    assert ov1["member_map"]["M-DEV-01"]["active_wip"] == 1

    # 4. 父卡完成：Card->done，Member.completed_cards +1，WIP 归零，子卡自动由 todo->ready
    _call(
        "/api/worlds/board-prod/actions/record_outcome",
        method="POST",
        payload={
            "card": parent_id,
            "member": "M-DEV-01",
            "outcome": "completed",
            "summary": "父任务已交付",
            "artifact_uri": "src/parent.py",
        },
        token="tok-dev-e2e",
    )
    parent_obj = _call(
        f"/api/worlds/board-prod/objects/Card/{parent_id}", token="tok-dev-e2e"
    )["object"]
    child_obj = _call(
        f"/api/worlds/board-prod/objects/Card/{child_id}", token="tok-dev-e2e"
    )["object"]
    assert parent_obj["status"] == "done"
    assert parent_obj["assignee"] is None
    assert parent_obj["result"] == "父任务已交付"
    assert parent_obj["artifact_uri"] == "src/parent.py"
    assert child_obj["status"] == "ready"

    ov2 = _overview("BD-E2E")
    assert ov2["counts"] == {
        "todo": 0,
        "ready": 1,
        "doing": 0,
        "done": 1,
        "blocked": 0,
        "triage": 0,
    }
    assert ov2["member_map"]["M-DEV-01"]["active_wip"] == 0
    assert ov2["member_map"]["M-DEV-01"]["completed_cards"] == initial_completed + 1

    # 5. 开发者认领并完成解锁后的子卡，board_overview 全卡落入 done，Board 无冗余 state 字段
    elig2 = _call(
        "/api/worlds/board-prod/functions/eligible_cards",
        method="POST",
        payload={"board": "BD-E2E", "role": "developer"},
        token="tok-dev-e2e",
    )["result"]
    assert [c["card_id"] for c in elig2["cards"]] == [child_id]

    _call(
        "/api/worlds/board-prod/actions/claim_card",
        method="POST",
        payload={"card": child_id, "member": "M-DEV-01"},
        token="tok-dev-e2e",
    )
    _call(
        "/api/worlds/board-prod/actions/record_outcome",
        method="POST",
        payload={
            "card": child_id,
            "member": "M-DEV-01",
            "outcome": "completed",
            "summary": "子任务已交付",
            "artifact_uri": "src/child.py",
        },
        token="tok-dev-e2e",
    )

    ov3 = _overview("BD-E2E")
    assert ov3["counts"] == {
        "todo": 0,
        "ready": 0,
        "doing": 0,
        "done": 2,
        "blocked": 0,
        "triage": 0,
    }
    assert ov3["member_map"]["M-DEV-01"]["active_wip"] == 0
    assert ov3["member_map"]["M-DEV-01"]["completed_cards"] == initial_completed + 2

    board_obj = _call(
        "/api/worlds/board-prod/objects/Board/BD-E2E", token="tok-dev-e2e"
    )["object"]
    assert "state" not in board_obj
    assert board_obj["board_key"] == "BD-E2E"


def test_sandbox_dropdown_and_enum_ui_helpers_present(base):
    html = urllib.request.urlopen(f"{base}/", timeout=10).read().decode("utf-8")
    for marker in (
        "effectiveObjects",
        "enumValuesFor",
        "enumOptionsFor",
        "refreshParamOptions",
        "renderSimulations",
        "正式执行第 1 步",
        "[沙盘]",
    ):
        assert marker in html
    assert "{steps: S.simSteps}" not in html


def test_simulation_ledger_persists_bugs_to_db_without_polluting_main_world(tmp_path):
    db_file = tmp_path / "board_ledger_test.sqlite3"

    def _start_server():
        reg = build_default_command_center(data_dir=tmp_path)
        from examples.kanban import BOARD_MANAGER, spec as board_spec
        rt = OntologyRuntime(board_spec, db_path=db_file)
        rt.submit_action(
            "INIT-BD",
            "create_board",
            {
                "board_key": "BD-SIM",
                "title": "沙盘持久化测试",
                "tenant": "acme",
                "created_by": "board-manager-01",
            },
            BOARD_MANAGER,
        )
        reg.register_workspace(
            "board-sim-persist",
            "task_board",
            "沙盘测试世界",
            rt,
            db_uri=f"sqlite://{db_file.name}",
            default_actor=BOARD_MANAGER,
        )
        handle = start_command_center_server(reg, port=0, title="Sim Ledger Test")
        return reg, rt, handle

    _, rt1, h1 = _start_server()
    try:
        base1 = h1.base_url
        before_fp = _get(base1, "/api/fingerprint", instance="board-sim-persist")

        # 1) 第 1 步合法推演：开卡
        sim_ok = _post(
            base1,
            "/api/simulate?instance=board-sim-persist",
            {
                "instance": "board-sim-persist",
                "session_id": "SESS-BUG-1",
                "action": "create_card",
                "params": {"board": "BD-SIM", "card_id": "C-SIM-1", "title": "测试卡"},
            },
        )
        assert sim_ok["valid"] is True
        assert sim_ok["sim_id"].startswith("SIM-")

        # 2) 第 2 步推演触发本体约束拒绝：直接手动移动到 done（用于定位模型规则/Bug）
        sim_bad = _post(
            base1,
            "/api/simulate?instance=board-sim-persist",
            {
                "instance": "board-sim-persist",
                "session_id": "SESS-BUG-1",
                "prior_steps": [
                    {
                        "action": "create_card",
                        "params": {"board": "BD-SIM", "card_id": "C-SIM-1", "title": "测试卡"},
                    }
                ],
                "action": "move_card",
                "params": {"card": "C-SIM-1", "target_status": "done"},
            },
        )
        assert sim_bad["valid"] is False
        assert sim_bad["error_code"] == "COMPLETION_REQUIRES_RECORD_OUTCOME"

        # 主库零写入
        assert _get(base1, "/api/fingerprint", instance="board-sim-persist") == before_fp

        # 可按 valid=false 过滤出全部异常推演线索
        bugs = _get(base1, "/api/simulations", instance="board-sim-persist", valid="false")
        assert bugs["count"] == 1
        assert bugs["simulations"][0]["error_code"] == "COMPLETION_REQUIRES_RECORD_OUTCOME"
    finally:
        h1.stop()
        rt1.close()

    # 3) 模拟服务重启，从磁盘 SQLite 重开，推演与 Bug 留痕必须仍在
    assert db_file.with_suffix(".sim_ledger.sqlite3").is_file()
    _, rt2, h2 = _start_server()
    try:
        all_sims = _get(h2.base_url, "/api/simulations", instance="board-sim-persist")
        assert all_sims["count"] == 2
        codes = [row["error_code"] for row in all_sims["simulations"]]
        assert "COMPLETION_REQUIRES_RECORD_OUTCOME" in codes
    finally:
        h2.stop()
        rt2.close()


def test_live_fingerprint_revision_tracks_external_writes_and_auto_poll_ui(tmp_path):
    from examples import o2c_ontology as o2c
    from examples.command_center_demo import build_o2c_command_center

    db_file = tmp_path / "live_o2c_poll.sqlite3"
    seed_rt = OntologyRuntime(o2c.spec, db_path=db_file)
    seed_rt.put_object(
        o2c.Customer(
            customer_id="O2C-CUST-001",
            name="O2C Demo Customer",
            credit_limit_cents=10_000_000,
        )
    )
    seed_rt.put_object(
        o2c.ReceivableBalance(
            ledger_id=o2c.ar_ledger_id("O2C-CUST-001"),
            customer="O2C-CUST-001",
            ar_balance_cents=0,
        )
    )
    seed_rt.close()

    reg = build_o2c_command_center(db_path=db_file)
    handle = start_command_center_server(reg, port=0, title="Live Auto-Refresh Test")
    try:
        base = handle.base_url
        html = urllib.request.urlopen(f"{base}/", timeout=10).read().decode("utf-8")
        assert "pollLiveFingerprint" in html
        assert "preserveSim" in html

        fp0 = _get(base, "/api/fingerprint", instance="o2c-erp-north")
        assert fp0.get("revision")

        ext_rt = OntologyRuntime(o2c.spec, db_path=db_file)
        try:
            r_create = ext_rt.submit_action(
                "EXT-CREATE-001",
                "create_sales_order",
                {
                    "order_id": "O2C-SO-003",
                    "customer": "O2C-CUST-001",
                    "product_summary": "O2C Widget",
                    "quantity": 12,
                    "unit_price_cents": 1_000_000,
                },
                o2c.AI_AGENT,
            )
            assert r_create["status"] == "COMMITTED"
            fp1 = _get(base, "/api/fingerprint", instance="o2c-erp-north")
            assert fp1["revision"] != fp0["revision"]

            r_note = ext_rt.submit_action(
                "EXT-NOTE-002",
                "annotate_o2c_governance",
                {"order": "O2C-SO-003", "fulfillment_note": "外部进程标注"},
                o2c.AI_AGENT,
            )
            assert r_note["status"] == "COMMITTED"
            fp2 = _get(base, "/api/fingerprint", instance="o2c-erp-north")
            assert fp2["revision"] != fp1["revision"]

            r_pending = ext_rt.submit_action(
                "EXT-CONFIRM-003",
                "confirm_sales_order",
                {"order": "O2C-SO-003"},
                o2c.AI_AGENT,
            )
            assert r_pending["status"] == "PENDING_APPROVAL"
            fp3 = _get(base, "/api/fingerprint", instance="o2c-erp-north")
            assert fp3["revision"] != fp2["revision"]

            r_rejected = ext_rt.reject_action(
                "EXT-CONFIRM-003",
                o2c.FINANCE_DIRECTOR,
                reason="演练驳回测试",
            )
            assert r_rejected["status"] == "REJECTED"
            fp4 = _get(base, "/api/fingerprint", instance="o2c-erp-north")
            assert fp4["revision"] != fp3["revision"]
        finally:
            ext_rt.close()
    finally:
        handle.stop()






