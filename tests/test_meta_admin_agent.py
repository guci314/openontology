"""M2 平台管理员 Agent（MetaAdminService）端到端测试。"""

from __future__ import annotations

import json

import pytest

from open_ontology.meta_admin import MetaAdminService, build_declarative_spec
from open_ontology.osdk import AIPAgentExecutor, run_agent_turn
from open_ontology.runtime import OntologyRuntime
from open_ontology.spec import Actor, InvariantError, ObjectType, OntologySpec
from open_ontology.workshop import CommandCenterRegistry


def _base_registry() -> CommandCenterRegistry:
    spec = OntologySpec()

    class _Seed(ObjectType):
        id: str
        name: str = "seed"

    spec.add_object_type(_Seed)
    rt = OntologyRuntime(spec, db_path=":memory:")
    return CommandCenterRegistry.from_runtime(rt)


def test_declarative_spec_rejects_unsafe_types_and_ops() -> None:
    with pytest.raises(InvariantError) as exc1:
        build_declarative_spec(
            object_types_json='[{"name":"Bad","fields":{"x":"eval()"}}]'
        )
    assert exc1.value.code == "UNSUPPORTED_FIELD_TYPE"

    with pytest.raises(InvariantError) as exc2:
        build_declarative_spec(
            object_types_json='[{"name":"Item","fields":{"count":"int"}}]',
            actions_json='[{"name":"bad","object_type":"Item","op":"exec"}]',
        )
    assert exc2.value.code == "UNSUPPORTED_OP"


def test_meta_admin_full_lifecycle_and_world_agent_handover() -> None:
    reg = _base_registry()
    admin_svc = MetaAdminService(reg)
    actor = admin_svc.actor

    try:
        obj_json = json.dumps(
            [{"name": "Counter", "display": "计数器", "fields": {"count": "int"}}]
        )
        act_json = json.dumps(
            [
                {
                    "name": "create_counter",
                    "display": "新建计数器",
                    "object_type": "Counter",
                    "op": "create",
                    "params": {"id": "str", "count": "int"},
                },
                {
                    "name": "add_count",
                    "display": "累加",
                    "object_type": "Counter",
                    "op": "increment",
                    "field": "count",
                    "param_field": "n",
                    "params": {"n": "int"},
                },
            ]
        )

        # 1. 沙盘模拟创建模型：registry 零副作用
        sim = admin_svc.runtime.simulate_action(
            "create_model_spec",
            {
                "model_id": "counter_model",
                "name": "计数器模型",
                "object_types_json": obj_json,
                "actions_json": act_json,
            },
            actor,
        )
        assert sim.valid is True
        assert "counter_model" not in reg.ontologies

        # 2. 正式提交并同步：模型出现在 CommandCenterRegistry
        run1 = admin_svc.runtime.submit_action(
            "META-1",
            "create_model_spec",
            {
                "model_id": "counter_model",
                "name": "计数器模型",
                "object_types_json": obj_json,
                "actions_json": act_json,
            },
            actor,
        )
        assert run1["status"] == "COMMITTED"
        admin_svc.sync_to_registry()
        assert "counter_model" in reg.ontologies

        # 3. 装配 Runtime + 创建世界状态 + 克隆平行沙盘 + 注册外部设备
        admin_svc.runtime.submit_action(
            "META-2",
            "provision_runtime",
            {"runtime_id": "rt-counter", "label": "计数器内存宿体", "storage_kind": "memory"},
            actor,
        )
        admin_svc.runtime.submit_action(
            "META-3",
            "create_world",
            {
                "world_id": "counter-prod",
                "model_id": "counter_model",
                "runtime_id": "rt-counter",
                "label": "计数器主世界",
            },
            actor,
        )
        admin_svc.runtime.submit_action(
            "META-4",
            "register_device",
            {
                "device_id": "dev-erp",
                "kind": "symbolic",
                "name": "ERP 连接器",
                "endpoint": "https://erp.example.local/openapi",
            },
            actor,
        )
        admin_svc.sync_to_registry()

        assert "rt-counter" in reg.runtimes
        assert "counter-prod" in reg.instances
        assert "dev-erp" in reg.symbolic_devices

        # 4. 克隆平行沙盘
        admin_svc.runtime.submit_action(
            "META-6",
            "fork_world",
            {"source_world_id": "counter-prod", "new_world_id": "counter-lab"},
            actor,
        )
        admin_svc.sync_to_registry()
        assert "counter-lab" in reg.instances

        # 5. 交接给业务世界 Agent：在新世界里创建对象并执行加法
        world_rt = reg.instances["counter-prod"].runtime
        world_actor = Actor(actor_id="u:ops", role="admin", clearance_tags={"*"})
        world_rt.submit_action(
            "W-1", "create_counter", {"id": "C-1", "count": 10}, world_actor
        )
        world_ex = AIPAgentExecutor(world_rt, world_actor)
        sim_add = world_ex.dispatch("add_count", {"n": 7})
        assert sim_add["simulated_only"] is True
        assert sim_add["diff"]["object_changes"][0]["after"]["count"] == 17
    finally:
        admin_svc.runtime.close()
        for inst in list(reg.instances.values()):
            inst.runtime.close()


def test_meta_admin_agent_turn_emits_ui_artifacts() -> None:
    reg = _base_registry()
    admin_svc = MetaAdminService(reg)
    try:
        ex = AIPAgentExecutor(admin_svc.runtime, admin_svc.actor)
        t_list = run_agent_turn(ex, "查询 PlatformModel 列表")
        assert t_list.artifacts[0].kind == "object_table"

        t_form = run_agent_turn(ex, "打开 create_model_spec 表单")
        assert t_form.artifacts[0].kind == "action_form"
        assert "model_id" in t_form.artifacts[0].payload["schema"]["properties"]
    finally:
        admin_svc.runtime.close()
        for inst in list(reg.instances.values()):
            inst.runtime.close()


def test_sync_device_to_world_ingests_backing_snapshot(monkeypatch) -> None:
    import io
    import urllib.request

    reg = _base_registry()
    admin_svc = MetaAdminService(reg)
    actor = admin_svc.actor

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=5: _FakeResp(
            json.dumps({"items": [{"id": "INT-1", "name": "十"}, {"id": "INT-2", "name": "二十"}]}).encode("utf-8")
        ),
    )

    try:
        admin_svc.runtime.submit_action(
            "DEV-1",
            "register_device",
            {
                "device_id": "dev-int",
                "kind": "symbolic",
                "name": "整数 REST 服务",
                "endpoint": "http://127.0.0.1:8767/openapi.json",
            },
            actor,
        )
        admin_svc.sync_to_registry()

        world_id = next(iter(reg.instances))
        obj_type = next(iter(reg.instances[world_id].runtime.spec.object_types))

        # 先沙盘模拟：目标世界零副作用
        sim = admin_svc.runtime.simulate_action(
            "sync_device_to_world",
            {
                "device_id": "dev-int",
                "world_id": world_id,
                "object_type": obj_type,
                "data_path": "/integers",
            },
            actor,
        )
        assert sim.valid is True
        assert reg.instances[world_id].runtime.count_objects() == 0

        # 正式提交并同步：目标世界成功摄入 2 条外部对象
        run = admin_svc.runtime.submit_action(
            "SYNC-1",
            "sync_device_to_world",
            {
                "device_id": "dev-int",
                "world_id": world_id,
                "object_type": obj_type,
                "data_path": "/integers",
            },
            actor,
        )
        assert run["status"] == "COMMITTED"
        admin_svc.sync_to_registry()
        assert reg.instances[world_id].runtime.count_objects() == 2
    finally:
        admin_svc.runtime.close()
        for inst in list(reg.instances.values()):
            inst.runtime.close()


def test_apply_field_mapping_and_adapter_lifecycle(monkeypatch) -> None:
    import io
    import urllib.request
    from open_ontology.meta_admin import apply_field_mapping

    assert apply_field_mapping([{"a": 1}], None) == [{"a": 1}]
    assert apply_field_mapping([{"ext_id": "X-1", "num": 42}], {"ext_id": "id", "num": "value"}) == [
        {"id": "X-1", "value": 42}
    ]

    reg = _base_registry()
    admin_svc = MetaAdminService(reg)
    actor = admin_svc.actor

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=5: _FakeResp(
            json.dumps({"items": [{"ext_id": "INT-1", "ext_name": "十"}, {"ext_id": "INT-2", "ext_name": "二十"}]}).encode("utf-8")
        ),
    )

    try:
        admin_svc.runtime.submit_action(
            "DEV-ADP",
            "register_device",
            {
                "device_id": "dev-ext",
                "kind": "symbolic",
                "name": "异构设备",
                "endpoint": "http://127.0.0.1:8767/openapi.json",
            },
            actor,
        )
        admin_svc.sync_to_registry()

        world_id = next(iter(reg.instances))
        obj_type = next(iter(reg.instances[world_id].runtime.spec.object_types))
        runtime_id = reg.instances[world_id].runtime_id

        res = admin_svc.runtime.submit_action(
            "ADP-1",
            "create_adapter",
            {
                "adapter_id": "adp-test",
                "device_id": "dev-ext",
                "world_id": world_id,
                "object_type": obj_type,
                "data_path": "/integers",
                "field_mapping_json": json.dumps({"ext_id": "id", "ext_name": "name"}),
                "interval_sec": 5,
            },
            actor,
        )
        assert res["status"] == "COMMITTED"
        admin_svc.sync_to_registry()

        assert any("adp-test" in label for label in reg.runtimes[runtime_id].adapters)

        sync_res = admin_svc.run_adapter_once("adp-test")
        assert sync_res["row_count"] == 2
        assert sync_res["sync_count"] == 1
        assert reg.instances[world_id].runtime.count_objects() == 2

        adapters = admin_svc.list_adapters()
        assert len(adapters) == 1
        assert adapters[0]["status"] == "RUNNING"
        assert adapters[0]["sync_count"] == 1
        assert adapters[0]["sync_error"] is None
        assert adapters[0]["last_sync_at"] is not None

        admin_svc.toggle_adapter("adp-test", "PAUSED")
        adapters_after = admin_svc.list_adapters()
        assert adapters_after[0]["status"] == "PAUSED"
        assert any("PAUSED" in label for label in reg.runtimes[runtime_id].adapters)

        wh_res = admin_svc.ingest_adapter_webhook(
            "adp-test",
            {
                "event": "integer.updated",
                "item": {"ext_id": "INT-3", "ext_name": "三十"},
                "items": [
                    {"ext_id": "INT-1", "ext_name": "十"},
                    {"ext_id": "INT-2", "ext_name": "二十"},
                    {"ext_id": "INT-3", "ext_name": "三十"},
                ],
            },
        )
        assert wh_res["row_count"] == 3
        assert wh_res["sync_count"] == 2
        assert reg.instances[world_id].runtime.count_objects() == 3
    finally:
        admin_svc.stop()
        admin_svc.runtime.close()
        for inst in list(reg.instances.values()):
            inst.runtime.close()
