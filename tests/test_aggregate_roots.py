"""C-WS-02-R1 验收测试：聚合根类型声明与工作空间根对象清单。

覆盖：
1. ``ObjectType.__aggregate_root__`` 是**类型元数据**（默认 False、可显式声明、
   可显式覆盖为 False），绝不成为对象实例字段；
2. 已持久化的 ``ontology_registry`` 类型描述带 ``aggregate_root``，对象值不带；
3. 看板蓝图只把 ``Board`` 声明为聚合根，``Card`` 等保持 False；
4. ``CommandCenterRegistry.list_workspaces(actor)`` 列出全部工作空间及其聚合根，
   跨工作空间同 ID 的根对象身份不混淆；
5. 无入边的非根对象不会被当作根；
6. 「未声明根类型」与「声明了根类型但没有可见根对象」可区分；
7. 行级安全与列级脱敏照常生效，不泄露被限制的根与明文标题；
8. 不可用工作空间显式报错且不拖垮健康工作空间；
9. 清单是纯读投影：对象 / 关系 / overlay / 动作记录前后指纹不变。
"""

from __future__ import annotations

import json

import pytest

from open_ontology import Actor, OntologyRuntime
from open_ontology.kanban import (
    Board,
    BoardColumn,
    Card,
    CardComment,
    CardRun,
    Member,
    spec as kanban_spec,
)
from open_ontology.spec import ObjectType, OntologySpec
from open_ontology.workshop import CommandCenterRegistry

MANAGER = Actor(actor_id="manager-01", role="board_manager", clearance_tags={"*"})


# ---------------------------------------------------------------------------
# 测试用极小模型（声明层，不碰看板域）
# ---------------------------------------------------------------------------
class PlainThing(ObjectType):
    """未声明聚合根：默认 False。"""

    __primary_key__ = "thing_id"
    __title__ = "name"

    thing_id: str
    name: str


class RootThing(ObjectType):
    """显式声明聚合根。"""

    __primary_key__ = "thing_id"
    __title__ = "name"
    __aggregate_root__ = True

    thing_id: str
    name: str


class DemotedRoot(RootThing):
    """显式覆盖为 False：声明由模型作者决定，不做拓扑推断。"""

    __aggregate_root__ = False


class TenantRoot(ObjectType):
    """带行级安全标签与列级脱敏的聚合根。"""

    __primary_key__ = "root_id"
    __title__ = "title"
    __aggregate_root__ = True
    __security_tag_fields__ = ("tenant",)
    __masked_fields__ = {"title": ("auditor",)}

    root_id: str
    title: str
    tenant: str


def _spec(*classes: type[ObjectType]) -> OntologySpec:
    spec = OntologySpec()
    for cls in classes:
        spec.object_type(cls)
    return spec


def _runtime(*classes: type[ObjectType]) -> OntologyRuntime:
    return OntologyRuntime(_spec(*classes))


def _registry(ontology_id: str, spec: OntologySpec) -> CommandCenterRegistry:
    registry = CommandCenterRegistry()
    registry.register_ontology(ontology_id, ontology_id, spec)
    return registry


def _add(registry: CommandCenterRegistry, workspace_id: str, ontology_id: str, runtime: OntologyRuntime):
    return registry.register_instance(
        workspace_id,
        ontology_id,
        label=workspace_id,
        runtime=runtime,
        db_uri=str(runtime.db_path),
    )


def _row(rows: list[dict], workspace_id: str) -> dict:
    matches = [row for row in rows if row["workspace_id"] == workspace_id]
    assert len(matches) == 1, f"workspace {workspace_id!r} listed {len(matches)} times"
    return matches[0]


def _board(board_key: str, title: str, tenant: str = "acme") -> Board:
    return Board(
        board_key=board_key,
        title=title,
        tenant=tenant,
        created_by="tester",
    )


# ---------------------------------------------------------------------------
# 1. 类型元数据：默认 False / 显式 True / 显式覆盖 False
# ---------------------------------------------------------------------------
def test_aggregate_root_defaults_and_explicit_declarations():
    assert ObjectType.__aggregate_root__ is False
    assert PlainThing.__aggregate_root__ is False
    assert RootThing.__aggregate_root__ is True
    assert DemotedRoot.__aggregate_root__ is False

    assert ObjectType.is_aggregate_root() is False
    assert PlainThing.is_aggregate_root() is False
    assert RootThing.is_aggregate_root() is True
    assert DemotedRoot.is_aggregate_root() is False


def test_aggregate_root_is_not_an_instance_field():
    for cls in (PlainThing, RootThing, DemotedRoot):
        assert "__aggregate_root__" not in cls.model_fields
        assert "__aggregate_root__" not in cls.model_json_schema().get("properties", {})

    instance = RootThing(thing_id="T-1", name="根对象")
    assert "__aggregate_root__" not in instance.model_dump()
    assert "__aggregate_root__" not in instance.model_dump(mode="json")
    assert "__aggregate_root__" not in instance.to_properties()

    # 仅声明标志不改变实例字段集合
    assert set(RootThing.model_fields) == set(PlainThing.model_fields)
    assert (
        RootThing.model_json_schema()["properties"]
        == PlainThing.model_json_schema()["properties"]
    )


# ---------------------------------------------------------------------------
# 2. 已持久化的类型描述带 aggregate_root，对象值不带
# ---------------------------------------------------------------------------
def test_registry_type_json_carries_aggregate_root_but_object_values_do_not():
    runtime = _runtime(RootThing, PlainThing)
    try:
        runtime.put_object(RootThing(thing_id="R-1", name="根"))

        def type_spec(name: str) -> dict:
            row = runtime.connection.execute(
                "SELECT spec_json FROM ontology_registry "
                "WHERE kind = 'object_type' AND name = ?",
                (name,),
            ).fetchone()
            assert row is not None, f"缺少 {name} 的类型注册行"
            return json.loads(row["spec_json"])

        root_meta = type_spec("RootThing")
        plain_meta = type_spec("PlainThing")

        assert root_meta["aggregate_root"] is True
        assert plain_meta["aggregate_root"] is False

        # 既有元数据键一个不少
        for meta, cls in ((root_meta, RootThing), (plain_meta, PlainThing)):
            assert set(meta) == {
                "name",
                "primary_key",
                "title",
                "security_tag_fields",
                "aggregate_root",
                "schema",
            }
            assert meta["name"] == cls.__name__
            assert meta["primary_key"] == cls.primary_key_name()
            assert meta["title"] == cls.__title__
            assert "properties" in meta["schema"]

        object_row = runtime.connection.execute(
            "SELECT properties_json, title FROM ontology_objects "
            "WHERE object_type = 'RootThing' AND object_id = 'R-1'"
        ).fetchone()
        properties = json.loads(object_row["properties_json"])
        assert "aggregate_root" not in properties
        assert "__aggregate_root__" not in properties
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 3. 看板域：Board 是聚合根，Card 等不是
# ---------------------------------------------------------------------------
def test_kanban_board_is_aggregate_root_and_card_is_not():
    assert Board.is_aggregate_root() is True
    for cls in (BoardColumn, Member, Card, CardComment, CardRun):
        assert cls.is_aggregate_root() is False, f"{cls.__name__} 不应被声明为聚合根"


# ---------------------------------------------------------------------------
# 4. 跨工作空间同 ID 的根对象身份可区分
# ---------------------------------------------------------------------------
def test_two_workspaces_same_model_same_root_id_keep_distinct_identity():
    registry = _registry("task_board", kanban_spec)
    runtime_a = OntologyRuntime(kanban_spec)
    runtime_b = OntologyRuntime(kanban_spec)
    try:
        runtime_a.put_object(_board("BD-CC-001", "看板 A"))
        runtime_b.put_object(_board("BD-CC-001", "看板 B"))
        _add(registry, "ws-a", "task_board", runtime_a)
        _add(registry, "ws-b", "task_board", runtime_b)

        rows = registry.list_workspaces(MANAGER)
        json.dumps(rows)  # 必须 JSON 可序列化

        assert [row["workspace_id"] for row in rows] == ["ws-a", "ws-b"]
        row_a, row_b = _row(rows, "ws-a"), _row(rows, "ws-b")
        for row in (row_a, row_b):
            assert row["model_id"] == "task_board"
            assert row["availability"] == "available"
            assert row["root_status"] == "roots"
            assert row["aggregate_root_types"] == ["Board"]
            assert len(row["aggregate_roots"]) == 1

        root_a = row_a["aggregate_roots"][0]
        root_b = row_b["aggregate_roots"][0]
        assert root_a["object_type"] == root_b["object_type"] == "Board"
        assert root_a["object_id"] == root_b["object_id"] == "BD-CC-001"
        assert root_a["title"] == "看板 A"
        assert root_b["title"] == "看板 B"
        # 完整身份 = 工作空间 + 类型 + 对象 ID
        assert root_a["workspace_id"] == "ws-a"
        assert root_b["workspace_id"] == "ws-b"
    finally:
        runtime_a.close()
        runtime_b.close()


# ---------------------------------------------------------------------------
# 5. 无入边的非根对象不被当作根
# ---------------------------------------------------------------------------
def test_non_root_object_without_incoming_links_is_not_listed():
    registry = _registry("task_board", kanban_spec)
    runtime = OntologyRuntime(kanban_spec)
    try:
        runtime.put_object(_board("BD-1", "看板"))
        runtime.put_object(
            Card(
                card_id="CD-1",
                board_key="BD-1",
                tenant="acme",
                title="孤立卡片",
                column_key="todo",
            )
        )
        _add(registry, "ws-1", "task_board", runtime)

        row = _row(registry.list_workspaces(MANAGER), "ws-1")
        assert [root["object_type"] for root in row["aggregate_roots"]] == ["Board"]
        assert [root["object_id"] for root in row["aggregate_roots"]] == ["BD-1"]
        assert "CD-1" not in json.dumps(row["aggregate_roots"])
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 6. 未声明根类型 vs 声明了根但零根对象
# ---------------------------------------------------------------------------
def test_no_declared_roots_and_empty_root_set_are_distinguishable():
    registry = _registry("plain_model", _spec(PlainThing))
    registry.register_ontology("task_board", "task_board", kanban_spec)
    plain_runtime = OntologyRuntime(_spec(PlainThing))
    empty_runtime = OntologyRuntime(kanban_spec)
    try:
        plain_runtime.put_object(PlainThing(thing_id="P-1", name="非根"))
        _add(registry, "ws-plain", "plain_model", plain_runtime)
        _add(registry, "ws-empty", "task_board", empty_runtime)

        rows = registry.list_workspaces(MANAGER)
        plain = _row(rows, "ws-plain")
        empty = _row(rows, "ws-empty")

        assert plain["availability"] == empty["availability"] == "available"
        assert plain["root_status"] == "no_declared_roots"
        assert plain["aggregate_root_types"] == []
        assert plain["aggregate_roots"] == []

        assert empty["root_status"] == "empty"
        assert empty["aggregate_root_types"] == ["Board"]
        assert empty["aggregate_roots"] == []
    finally:
        plain_runtime.close()
        empty_runtime.close()


# ---------------------------------------------------------------------------
# 7. 行级安全与列级脱敏
# ---------------------------------------------------------------------------
def test_row_restricted_and_masked_roots_are_not_leaked():
    registry = _registry("tenant_model", _spec(TenantRoot))
    runtime = OntologyRuntime(_spec(TenantRoot))
    try:
        runtime.put_object(TenantRoot(root_id="R-ACME", title="acme-secret", tenant="acme"))
        runtime.put_object(TenantRoot(root_id="R-GLOBEX", title="globex-secret", tenant="globex"))
        _add(registry, "ws-tenant", "tenant_model", runtime)

        viewer = Actor(
            actor_id="viewer-01", role="viewer", clearance_tags={"tenant:acme"}
        )
        viewer_rows = registry.list_workspaces(viewer)
        viewer_roots = _row(viewer_rows, "ws-tenant")["aggregate_roots"]

        assert [root["object_id"] for root in viewer_roots] == ["R-ACME"]
        assert viewer_roots[0]["title"] == "***MASKED***"
        dumped = json.dumps(viewer_rows, ensure_ascii=False)
        assert "globex-secret" not in dumped
        assert "acme-secret" not in dumped

        auditor = Actor(
            actor_id="auditor-01", role="auditor", clearance_tags={"*"}
        )
        auditor_roots = _row(registry.list_workspaces(auditor), "ws-tenant")[
            "aggregate_roots"
        ]
        assert [root["object_id"] for root in auditor_roots] == ["R-ACME", "R-GLOBEX"]
        assert [root["title"] for root in auditor_roots] == [
            "acme-secret",
            "globex-secret",
        ]
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 8. 不可用工作空间显式报告，不拖垮其它工作空间
# ---------------------------------------------------------------------------
def test_unavailable_workspace_is_reported_while_healthy_ones_remain():
    registry = _registry("task_board", kanban_spec)
    healthy = OntologyRuntime(kanban_spec)
    broken = OntologyRuntime(kanban_spec)
    try:
        healthy.put_object(_board("BD-HEALTHY", "健康看板"))
        broken.put_object(_board("BD-BROKEN", "已关闭看板"))
        _add(registry, "ws-healthy", "task_board", healthy)
        _add(registry, "ws-broken", "task_board", broken)
        broken.close()

        rows = registry.list_workspaces(MANAGER)
        assert {row["workspace_id"] for row in rows} == {"ws-healthy", "ws-broken"}

        broken_row = _row(rows, "ws-broken")
        assert broken_row["availability"] == "unavailable"
        assert broken_row["root_status"] == "unavailable"
        # 不可读的根必须是显式的 None + 原因，绝不能伪装成「成功但为空」。
        assert broken_row["aggregate_roots"] is None
        assert broken_row["reason"] == "runtime_closed"

        healthy_row = _row(rows, "ws-healthy")
        assert healthy_row["availability"] == "available"
        assert healthy_row["root_status"] == "roots"
        assert [root["object_id"] for root in healthy_row["aggregate_roots"]] == [
            "BD-HEALTHY"
        ]
    finally:
        healthy.close()


def _ledger_model(amount_type: type) -> type[ObjectType]:
    """同名的旧/新版本模型：用真实的 Pydantic 校验失败制造不可读工作空间。"""
    return type(
        "Ledger",
        (ObjectType,),
        {
            "__annotations__": {
                "ledger_id": str,
                "title": str,
                "amount": amount_type,
            },
            "__primary_key__": "ledger_id",
            "__title__": "title",
            "__aggregate_root__": True,
        },
    )


def test_unavailable_reason_does_not_leak_sensitive_values(tmp_path):
    """旧库里的明文取值不得通过「不可读原因」泄露出去。"""
    sensitive = "SECRET-AMOUNT-4242"
    db_path = tmp_path / "ledger.sqlite3"

    old_spec = OntologySpec()
    old_model = _ledger_model(str)
    old_spec.object_type(old_model)
    old_runtime = OntologyRuntime(old_spec, db_path)
    try:
        old_runtime.put_object(
            old_model(ledger_id="L-1", title="账本", amount=sensitive)
        )
    finally:
        old_runtime.close()

    # 新版本把 amount 收紧为 int：读取旧行时 Pydantic 校验失败，
    # 异常消息里会带上未脱敏的原始取值。
    new_spec = OntologySpec()
    new_spec.object_type(_ledger_model(int))
    new_runtime = OntologyRuntime(new_spec, db_path)
    registry = _registry("ledger_model", new_spec)
    try:
        _add(registry, "ws-ledger", "ledger_model", new_runtime)
        rows = registry.list_workspaces(MANAGER)

        row = _row(rows, "ws-ledger")
        assert row["availability"] == "unavailable"
        assert row["root_status"] == "unavailable"

        # 公开输出里不得出现任何原始取值（含异常消息夹带的明文）。
        dumped = json.dumps(rows, ensure_ascii=False)
        assert sensitive not in dumped
        assert "4242" not in dumped
        assert sensitive not in row["reason"]

        assert row["aggregate_roots"] is None
        # 只暴露异常类型，不回传可能夹带明文的消息。
        assert row["reason"] == "ValidationError"
    finally:
        new_runtime.close()


# ---------------------------------------------------------------------------
# 9. 纯读投影：不写对象 / 关系 / overlay / 动作账本
# ---------------------------------------------------------------------------
def test_listing_does_not_mutate_runtime_state():
    registry = _registry("task_board", kanban_spec)
    runtime = OntologyRuntime(kanban_spec)
    try:
        runtime.put_object(_board("BD-1", "看板"))
        _add(registry, "ws-1", "task_board", runtime)

        before = runtime.state_fingerprint()
        runs_before = len(runtime.list_action_runs())
        spec_before = runtime.spec.summary()

        registry.list_workspaces(MANAGER)

        assert runtime.state_fingerprint() == before
        assert len(runtime.list_action_runs()) == runs_before
        assert runtime.spec.summary() == spec_before
    finally:
        runtime.close()


def test_list_workspaces_requires_an_explicit_actor():
    registry = CommandCenterRegistry()
    with pytest.raises(TypeError):
        registry.list_workspaces()  # type: ignore[call-arg]
