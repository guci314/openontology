"""C-WS-03 验收测试：TBox / Behavior 投影共用同一份声明源。

覆盖：

1. ``describe_tbox`` 只投影声明层（类型 / 字段约束 / 接口 / 关系 / 聚合根），
   输出 JSON 可序列化，不含对象值、审计记录、handler 源码或任意校验器；
2. ``describe_behavior`` 保留 action 参数 schema、角色 / 审批声明与既有元数据形状，
   且不泄漏 handler / condition 源码；
3. ``workshop._spec_meta`` 直接复用两个投影，键与键序不变；
   工作空间派生的 default-actor 角色留在纯 TBox 投影之外；
4. 共享同一模型的两个真实 runtime：事实互相隔离，模型投影不变，
   投影绝不暴露被编辑的事实值；fork 同样隔离。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from open_ontology import Actor, ObjectType, OntologyRuntime, OntologySpec
from open_ontology.model_views import describe_behavior, describe_tbox
from open_ontology.workshop import OntologyInstanceEntry, _spec_meta

ADMIN = Actor(actor_id="admin-01", role="admin", clearance_tags={"*"})


class Widget(ObjectType):
    __primary_key__ = "widget_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)
    __owned__ = {"status": "ops"}
    __masked_fields__ = {"price": ("finance",)}
    __aggregate_root__ = True
    __interfaces__ = ("Named",)

    widget_id: str
    name: str = Field(min_length=1, max_length=40)
    region: str | None = None
    price: float = Field(ge=0, le=1000)
    status: Literal["draft", "live"] = "draft"
    quantity: int = Field(default=1, ge=0)
    note: str = ""

    @field_validator("name")
    @classmethod
    def _no_leading_space(cls, value: str) -> str:
        if value != value.lstrip():
            raise ValueError("name must not start with whitespace")
        return value


class WidgetActionParams(BaseModel):
    widget_id: str
    reason: str = ""


class WidgetFunctionParams(BaseModel):
    widget_id: str


def _build_spec() -> OntologySpec:
    spec = OntologySpec()
    spec.interface("Named", required_fields=("name",), description="有名字的对象")
    spec.object_type(Widget)
    spec.object_property(
        "widget_parent",
        "Widget",
        "Widget",
        cardinality="one-to-many",
        description="父子关系",
    )

    @spec.function("widget_briefing", params=WidgetFunctionParams, description="只读摘要")
    def widget_briefing(runtime: Any, params: WidgetFunctionParams) -> dict[str, Any]:
        return {"widget_id": params.widget_id}

    @spec.action(
        "activate_widget",
        params=WidgetActionParams,
        allowed_roles=("ops",),
        approver_roles=("manager",),
        approval_rule=lambda params, actor: True,
        description="激活部件",
    )
    def activate_widget(runtime: Any, params: WidgetActionParams) -> None:
        return None

    @spec.automation(
        "watch_widget",
        Widget,
        condition=lambda obj: obj.status == "live",
        description="监听部件",
    )
    def watch_widget(runtime: Any, obj: Widget, actor: Actor) -> dict[str, Any] | None:
        return None

    return spec


# ---------------------------------------------------------------------------
# 1. TBox：声明结构 + 约束 + 聚合根，且不泄漏运行时事实
# ---------------------------------------------------------------------------
def test_describe_tbox_projects_only_declarations():
    spec = _build_spec()
    tbox = describe_tbox(spec)

    # JSON 可序列化（不允许 object / callable / 数据库游标混入）
    serialized = json.dumps(tbox, ensure_ascii=False)

    assert [row["name"] for row in tbox["object_types"]] == ["Widget"]
    widget = tbox["object_types"][0]
    assert widget["primary_key"] == "widget_id"
    assert widget["title"] == "name"
    assert widget["interfaces"] == ["Named"]
    assert widget["aggregate_root"] is True
    assert widget["owned"] is True
    assert widget["owned_fields"] == ["status"]
    assert widget["security_tag_fields"] == ["region"]

    fields = {row["name"]: row for row in widget["fields"]}
    assert fields["price"]["type"] == "float"
    assert fields["price"]["property_kind"] == "datatype"
    assert fields["price"]["masked"] is True
    assert fields["price"]["masked_roles"] == ["finance"]
    assert fields["price"]["required"] is True
    assert fields["price"]["constraints"] == {"ge": 0, "le": 1000}
    assert fields["name"]["constraints"] == {"min_length": 1, "max_length": 40}
    assert fields["quantity"]["constraints"] == {"ge": 0}
    assert fields["quantity"]["default"] == 1
    assert fields["status"]["choices"] == ["draft", "live"]
    assert fields["status"]["default"] == "draft"
    assert fields["note"]["default"] == ""

    assert tbox["interfaces"] == [
        {
            "name": "Named",
            "required_fields": ["name"],
            "description": "有名字的对象",
            "implementations": ["Widget"],
        }
    ]
    assert tbox["object_properties"] == [
        {
            "name": "widget_parent",
            "domain": "Widget",
            "range": "Widget",
            "cardinality": "one-to-many",
            "owned": False,
            "field": None,
            "description": "父子关系",
        }
    ]

    # 事实 / 审计 / 可执行实现 / 任意校验器都不属于 TBox
    for forbidden in (
        "objects",
        "object_values",
        '"properties"',
        "action_runs",
        "audit",
        "handler",
        "condition",
        "validator",
        "sqlite",
    ):
        assert forbidden not in serialized
    assert "def " not in serialized
    assert "lambda" not in serialized


# ---------------------------------------------------------------------------
# 2. Behavior：保留既有契约形状，不泄漏可执行实现
# ---------------------------------------------------------------------------
def test_describe_behavior_keeps_existing_contract_shape():
    spec = _build_spec()
    behavior = describe_behavior(spec)
    serialized = json.dumps(behavior, ensure_ascii=False)

    assert list(behavior) == ["functions", "actions", "automations", "roles"]

    assert behavior["functions"] == [
        {"name": "widget_briefing", "description": "只读摘要"}
    ]

    action = behavior["actions"][0]
    assert set(action) == {
        "name",
        "description",
        "allowed_roles",
        "approver_roles",
        "affects_source",
        "visibility",
        "params",
    }
    assert action["visibility"] == "public"
    assert action["name"] == "activate_widget"
    assert action["allowed_roles"] == ["ops"]
    assert action["approver_roles"] == ["manager"]
    assert action["affects_source"] is True
    assert set(action["params"]["properties"]) == {"widget_id", "reason"}

    assert behavior["automations"] == [
        {
            "name": "watch_widget",
            "watch_object_type": "Widget",
            "description": "监听部件",
        }
    ]

    assert "def " not in serialized
    assert "lambda" not in serialized


# ---------------------------------------------------------------------------
# 3. workshop._spec_meta 复用投影：键 / 键序 / 工作空间角色
# ---------------------------------------------------------------------------
def test_spec_meta_reuses_views_and_keeps_workspace_roles_outside_tbox():
    spec = _build_spec()
    runtime = OntologyRuntime(spec)
    try:
        entry = OntologyInstanceEntry(
            instance_id="ws-a",
            ontology_id="widget_model",
            label="部件工作空间",
            description="",
            db_uri="memory://",
            runtime=runtime,
            default_actor=Actor(
                actor_id="owner-01", role="workspace_owner", clearance_tags={"*"}
            ),
        )
        meta = _spec_meta(spec, [entry])
    finally:
        runtime.close()

    assert list(meta) == [
        "spec",
        "roles",
        "object_types",
        "interfaces",
        "object_properties",
        "functions",
        "actions",
        "automations",
        "role_defs",
    ]
    tbox = describe_tbox(spec)
    behavior = describe_behavior(spec)
    assert meta["object_types"] == tbox["object_types"]
    assert meta["interfaces"] == tbox["interfaces"]
    assert meta["object_properties"] == tbox["object_properties"]
    assert meta["functions"] == behavior["functions"]
    assert meta["actions"] == behavior["actions"]
    assert meta["automations"] == behavior["automations"]
    assert meta["role_defs"] == behavior["roles"]

    # default actor 角色来自工作空间，不属于纯 TBox 投影
    assert "workspace_owner" in meta["roles"]
    assert "ops" in meta["roles"]
    assert "manager" in meta["roles"]
    assert "finance" in meta["roles"]
    assert "roles" not in tbox


# ---------------------------------------------------------------------------
# 4. 两个真实 runtime 共享模型：事实隔离，投影不暴露事实值
# ---------------------------------------------------------------------------
def _widget(widget_id: str, name: str, price: float) -> Widget:
    return Widget(widget_id=widget_id, name=name, price=price)


def test_two_runtimes_share_model_but_isolate_facts_and_projections():
    spec = _build_spec()
    rt_a = OntologyRuntime(spec)
    rt_b = OntologyRuntime(spec)
    try:
        rt_a.put_object(_widget("W-1", "Alpha-fact", 10))
        rt_b.put_object(_widget("W-1", "Beta-fact", 20))

        tbox_before = describe_tbox(spec)
        behavior_before = describe_behavior(spec)

        rt_a.put_object(_widget("W-1", "Edited-fact-9f3a", 999))

        assert rt_a.get_object(Widget, "W-1", ADMIN).name == "Edited-fact-9f3a"
        assert rt_b.get_object(Widget, "W-1", ADMIN).name == "Beta-fact"

        assert describe_tbox(spec) == tbox_before
        assert describe_behavior(spec) == behavior_before

        serialized = json.dumps(
            {"tbox": describe_tbox(spec), "behavior": describe_behavior(spec)},
            ensure_ascii=False,
        )
        for fact_value in ("Alpha-fact", "Beta-fact", "Edited-fact-9f3a"):
            assert fact_value not in serialized

        # fork 隔离：分叉写入不回流母体，也不改变模型投影
        fork = rt_a.fork()
        try:
            fork.put_object(_widget("W-1", "Fork-only-fact", 1))
            assert fork.get_object(Widget, "W-1", ADMIN).name == "Fork-only-fact"
            assert rt_a.get_object(Widget, "W-1", ADMIN).name == "Edited-fact-9f3a"
        finally:
            fork.close()

        assert describe_tbox(spec) == tbox_before
        assert describe_behavior(spec) == behavior_before
        assert "Fork-only-fact" not in json.dumps(
            {"tbox": describe_tbox(spec), "behavior": describe_behavior(spec)},
            ensure_ascii=False,
        )
    finally:
        rt_a.close()
        rt_b.close()


def test_describe_tbox_emits_strict_plain_json_for_tuple_and_non_finite_defaults():
    from open_ontology.execution.fabric import digest

    class StrictSample(ObjectType):
        __primary_key__ = "sample_id"
        sample_id: str
        tags: tuple[str, ...] = ("a", "b")
        bad_float: float = float("nan")

    local_spec = OntologySpec()
    local_spec.object_type(StrictSample)

    tbox = describe_tbox(local_spec)
    # Must satisfy ExecutionFabric's strict plain-JSON contract (no tuples, no NaN)
    assert len(digest(tbox)) == 64
    fields = {f["name"]: f for f in tbox["object_types"][0]["fields"]}
    assert fields["tags"]["default"] == ["a", "b"]
    assert "default" not in fields["bad_float"]


def test_role_def_validation_and_behavior_projection():
    import pytest
    from open_ontology import RoleDef, UnknownRoleError

    class SecretItem(ObjectType):
        __primary_key__ = "item_id"
        __masked_fields__ = {"secret": ("typo_role",)}
        item_id: str
        secret: str = ""

    strict_spec = OntologySpec()
    role = strict_spec.role("operator", "值守操作员")
    assert isinstance(role, RoleDef)
    assert strict_spec.get_role("operator") == role
    assert strict_spec.summary()["roles"] == ["operator"]

    with pytest.raises(UnknownRoleError):
        strict_spec.object_type(SecretItem)

    @strict_spec.action("do_op", params=WidgetActionParams, allowed_roles=("operator",))
    def do_op(runtime: Any, params: WidgetActionParams) -> None:
        return None

    with pytest.raises(UnknownRoleError):

        @strict_spec.action("bad_op", params=WidgetActionParams, allowed_roles=("ghost",))
        def bad_op(runtime: Any, params: WidgetActionParams) -> None:
            return None

    with pytest.raises(UnknownRoleError):

        @strict_spec.action(
            "bad_approver",
            params=WidgetActionParams,
            allowed_roles=("operator",),
            approver_roles=("missing_mgr",),
        )
        def bad_approver(runtime: Any, params: WidgetActionParams) -> None:
            return None

    behavior = describe_behavior(strict_spec)
    assert behavior["roles"] == [
        {
            "name": "operator",
            "description": "值守操作员",
            "declared": True,
            "allowed_actions": ["do_op"],
            "approver_actions": [],
            "masked_fields": [],
        }
    ]

    meta = _spec_meta(strict_spec)
    assert meta["roles"][0] == "operator"
    assert meta["role_defs"] == behavior["roles"]


def test_describe_openapi_filters_actions_by_role_and_rewrites_refs():
    from open_ontology.model_views import describe_openapi

    class NestedInput(BaseModel):
        tag: str

    class ComplexActionParams(BaseModel):
        widget_id: str
        nested: NestedInput

    s = OntologySpec()
    s.role("worker")
    s.role("admin")
    s.role("finance")
    s.interface("Named", required_fields=("name",))
    s.object_type(Widget)

    @s.action("public_worker", params=ComplexActionParams, allowed_roles=("worker",))
    def public_worker(runtime: Any, params: ComplexActionParams) -> None:
        return None

    @s.action("admin_only", params=WidgetActionParams, allowed_roles=("admin",))
    def admin_only(runtime: Any, params: WidgetActionParams) -> None:
        return None

    @s.action(
        "secret_internal",
        params=WidgetActionParams,
        allowed_roles=("worker",),
        visibility="private",
    )
    def secret_internal(runtime: Any, params: WidgetActionParams) -> None:
        return None

    doc = describe_openapi(
        s,
        world_id="ws-demo",
        base_url="http://127.0.0.1:8765",
        role="worker",
    )
    assert doc["openapi"] == "3.1.0"
    assert doc["servers"] == [{"url": "http://127.0.0.1:8765"}]
    assert "/api/worlds/ws-demo/objects/Widget" in doc["paths"]
    assert "/api/worlds/ws-demo/objects/Widget/{widget_id}" in doc["paths"]
    assert "/api/worlds/ws-demo/actions/public_worker" in doc["paths"]
    assert "/api/worlds/ws-demo/actions/admin_only" not in doc["paths"]
    assert "/api/worlds/ws-demo/actions/secret_internal" not in doc["paths"]
    assert "NestedInput" in doc["components"]["schemas"]
    assert "#/$defs/" not in json.dumps(doc, ensure_ascii=False)

