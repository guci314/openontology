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
from urllib.parse import urlencode

import pytest

from open_ontology import (
    OntologyRuntime,
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
def center():
    registry = build_default_command_center()
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
        "coldchain_mcts",
        "crm_saas_unplug",
        "steel_mdm",
        "osv2_governance",
    ]
    assert len(registry.instances) == 7
    assert set(registry.instances) == {
        "factory-prod",
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
    assert len(data["ontologies"]) == 5
    assert len(data["instances"]) == 7

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
        "object explorer",
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
