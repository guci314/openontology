"""M2 平台管理员元本体（Meta-Admin Ontology）。

将「创建领域模型、装配 Runtime、拉起世界状态、克隆平行沙盘、注册外部设备」
封装为受控的 M2 元对象与元动作，直接复用 ``AIPAgentExecutor`` 与 ``UIArtifact``
（``action_form`` / ``diff_preview`` / ``object_table``），实现沙盘先行、零裸写。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, create_model

from .http_server import _fetch_device_rows
from .runtime import OntologyRuntime
from .spec import Actor, InvariantError, ObjectType, OntologySpec

if TYPE_CHECKING:
    from .workshop import CommandCenterRegistry

__all__ = [
    "PlatformModel",
    "PlatformRuntime",
    "PlatformWorld",
    "PlatformDevice",
    "PlatformSyncRecord",
    "PlatformAdapter",
    "apply_field_mapping",
    "build_declarative_spec",
    "MetaAdminService",
]

_FIELD_TYPE_MAP: dict[str, type[Any]] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "bool": bool,
    "boolean": bool,
}


# ---------------------------------------------------------------------------
# M2 元对象（与指挥台四大视角对应）
# ---------------------------------------------------------------------------
class PlatformModel(ObjectType):
    id: str
    name: str
    object_types_json: str = "[]"
    actions_json: str = "[]"
    description: str = ""
    dynamic: bool = False


class PlatformRuntime(ObjectType):
    id: str
    label: str
    storage_kind: str = "memory"
    db_uri: str = "sqlite://:memory:"
    description: str = ""


class PlatformWorld(ObjectType):
    id: str
    model_id: str
    runtime_id: str = "rt-default-memory"
    label: str = ""
    description: str = ""
    source_world_id: str = ""


class PlatformDevice(ObjectType):
    id: str
    kind: str = "symbolic"  # "symbolic" | "agent_card"
    name: str
    device_type: str = "openapi"
    endpoint: str
    status: str = "CONNECTED"


class PlatformSyncRecord(ObjectType):
    id: str
    device_id: str
    world_id: str
    object_type: str
    data_url: str
    rows_json: str = "[]"
    row_count: int = 0


class PlatformAdapter(ObjectType):
    id: str
    device_id: str
    world_id: str
    object_type: str
    data_path: str = "/integers"
    items_key: str = "items"
    field_mapping_json: str = "{}"
    interval_sec: int = 3
    status: str = "RUNNING"  # "RUNNING" | "PAUSED"


# ---------------------------------------------------------------------------
# 元动作参数模型
# ---------------------------------------------------------------------------
class CreateModelSpecParams(BaseModel):
    model_id: str = Field(description="模型唯一标识，如 inventory_model")
    name: str = Field(description="模型中文名称，如 库存管理模型")
    object_types_json: str = Field(
        default='[{"name":"Item","display":"物品","fields":{"count":"int"}}]',
        description="对象类型声明 JSON 数组",
    )
    actions_json: str = Field(
        default='[{"name":"add_count","display":"增加数量","object_type":"Item","op":"increment","field":"count","param_field":"n","params":{"n":"int"}}]',
        description="受控动作声明 JSON 数组（支持 op: create / update / increment）",
    )
    description: str = Field(default="", description="模型说明")


class ProvisionRuntimeParams(BaseModel):
    runtime_id: str = Field(description="宿体编号，如 rt-sqlite-01")
    label: str = Field(description="宿体展示名称")
    storage_kind: Literal["memory", "sqlite"] = Field(
        default="memory", description="存储类型：memory 或 sqlite"
    )
    db_uri: str = Field(
        default="sqlite://:memory:", description="连接串或 SQLite 路径"
    )
    description: str = Field(default="", description="宿体描述")


class CreateWorldParams(BaseModel):
    world_id: str = Field(description="世界状态编号，如 inv-prod")
    model_id: str = Field(description="绑定的领域模型编号")
    label: str = Field(default="", description="世界状态展示名称")
    runtime_id: str = Field(default="rt-default-memory", description="目标宿体编号")
    description: str = Field(default="", description="世界状态说明")


class ForkWorldParams(BaseModel):
    source_world_id: str = Field(description="源世界状态编号")
    new_world_id: str = Field(description="新平行沙盘编号")
    label: str = Field(default="", description="新沙盘展示名称")


class RegisterDeviceParams(BaseModel):
    device_id: str = Field(description="设备唯一编号，如 dev-orders-01")
    kind: Literal["symbolic"] = Field(
        default="symbolic", description="设备类型：symbolic"
    )
    name: str = Field(description="设备名称")
    endpoint: str = Field(description="服务端点 URL")
    device_type: str = Field(default="openapi", description="协议类型，如 openapi")


class SyncDeviceParams(BaseModel):
    device_id: str = Field(description="已注册的外部设备编号")
    world_id: str = Field(description="目标世界状态编号")
    object_type: str = Field(description="目标本体对象类型名，如 IntegerValue")
    data_path: str = Field(default="/integers", description="设备数据接口相对路径或完整 URL")
    items_key: str = Field(default="items", description="JSON 响应中的数组字段名（根即为数组时留空）")


class CreateAdapterParams(BaseModel):
    adapter_id: str = Field(description="适配器编号，如 adp-int-sync")
    device_id: str = Field(description="源外部设备编号")
    world_id: str = Field(description="目标世界状态编号")
    object_type: str = Field(description="目标本体对象类型名，如 IntegerValue")
    data_path: str = Field(default="/integers", description="设备数据接口相对路径或完整 URL")
    items_key: str = Field(default="items", description="JSON 响应中的数组字段名")
    field_mapping_json: str = Field(
        default="{}",
        description='异构字段映射 JSON，如 {"ext_id":"id","num":"value"}；空 {} 表示同名透传',
    )
    interval_sec: int = Field(default=3, description="自动同步轮询周期（秒），0 表示仅手动触发")


class ToggleAdapterParams(BaseModel):
    adapter_id: str = Field(description="适配器编号")
    status: Literal["RUNNING", "PAUSED"] = Field(description="目标状态：RUNNING 或 PAUSED")


def apply_field_mapping(
    rows: list[dict[str, Any]],
    field_mapping: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """将外部设备异构字段映射为本体目标字段；未配置映射时原样透传。"""
    if not field_mapping:
        return [dict(r) for r in rows]
    mapped: list[dict[str, Any]] = []
    for row in rows:
        out: dict[str, Any] = {}
        for k, v in row.items():
            out[field_mapping.get(k, k)] = v
        mapped.append(out)
    return mapped


# ---------------------------------------------------------------------------
# 声明式安全 OntologySpec 构建器（零 eval / exec）
# ---------------------------------------------------------------------------
def _resolve_field_type(type_name: str) -> type[Any]:
    t = _FIELD_TYPE_MAP.get(str(type_name).lower())
    if t is None:
        raise InvariantError(
            f"unsupported field type {type_name!r}; allowed: {sorted(_FIELD_TYPE_MAP)}",
            code="UNSUPPORTED_FIELD_TYPE",
        )
    return t


def build_declarative_spec(
    *,
    object_types_json: str,
    actions_json: str = "[]",
) -> OntologySpec:
    """从受限 JSON 声明安全构建 ``OntologySpec``（不使用 eval/exec）。"""
    try:
        obj_defs = json.loads(object_types_json or "[]")
        act_defs = json.loads(actions_json or "[]")
    except json.JSONDecodeError as exc:
        raise InvariantError(f"invalid JSON spec: {exc}", code="INVALID_SPEC_JSON") from exc

    if not isinstance(obj_defs, list) or not obj_defs:
        raise InvariantError(
            "object_types_json must be a non-empty JSON array",
            code="EMPTY_OBJECT_TYPES",
        )
    if not isinstance(act_defs, list):
        raise InvariantError("actions_json must be a JSON array", code="INVALID_ACTIONS_JSON")

    spec = OntologySpec()
    obj_display: dict[str, str] = {}
    act_display: dict[str, str] = {}
    created_classes: dict[str, type[ObjectType]] = {}

    for od in obj_defs:
        if not isinstance(od, dict) or not od.get("name"):
            raise InvariantError("each object type requires 'name'", code="INVALID_OBJECT_DEF")
        cls_name = str(od["name"])
        if not cls_name.isidentifier():
            raise InvariantError(f"invalid object type name: {cls_name!r}", code="INVALID_IDENTIFIER")
        raw_fields = od.get("fields") or {}
        if not isinstance(raw_fields, dict):
            raise InvariantError("object fields must be a JSON object", code="INVALID_FIELDS")
        field_defs: dict[str, Any] = {"id": (str, ...)}
        for fname, ftype in raw_fields.items():
            if not str(fname).isidentifier():
                raise InvariantError(f"invalid field name: {fname!r}", code="INVALID_IDENTIFIER")
            py_type = _resolve_field_type(str(ftype))
            default_val: Any = 0 if py_type in (int, float) else (False if py_type is bool else "")
            field_defs[str(fname)] = (py_type, default_val)

        cls = create_model(cls_name, __base__=ObjectType, **field_defs)
        spec.add_object_type(cls)
        created_classes[cls_name] = cls
        if od.get("display"):
            obj_display[cls_name] = str(od["display"])

    for ad in act_defs:
        if not isinstance(ad, dict) or not ad.get("name") or not ad.get("object_type"):
            raise InvariantError(
                "each action requires 'name' and 'object_type'", code="INVALID_ACTION_DEF"
            )
        act_name = str(ad["name"])
        target_name = str(ad["object_type"])
        if target_name not in created_classes:
            raise InvariantError(
                f"action {act_name!r} references unknown object_type {target_name!r}",
                code="UNKNOWN_TARGET_OBJECT",
            )
        target_cls = created_classes[target_name]
        op = str(ad.get("op") or "update")
        if op not in ("create", "update", "increment"):
            raise InvariantError(f"unsupported declarative op {op!r}", code="UNSUPPORTED_OP")

        raw_params = ad.get("params") or {}
        param_defs: dict[str, Any] = {}
        for pname, ptype in raw_params.items():
            param_defs[str(pname)] = (_resolve_field_type(str(ptype)), ...)
        if op == "create" and "id" not in param_defs:
            param_defs["id"] = (str, ...)
        params_cls = create_model(f"{act_name}_Params", **param_defs)

        field_name = str(ad.get("field") or "")
        param_field = str(ad.get("param_field") or "")

        def _make_handler(
            _cls: type[ObjectType],
            _op: str,
            _field: str,
            _pfield: str,
        ):
            def _handler(ctx: Any, params: BaseModel) -> dict[str, Any]:
                pdata = params.model_dump()
                if _op == "create":
                    obj = _cls(**pdata)
                    ctx.create(obj)
                    return {"created": obj.id}
                objs = ctx.query(_cls)
                if not objs:
                    raise InvariantError(f"no {_cls.__name__} object found", code="NOT_FOUND")
                target_id = pdata.get("id")
                target = next((o for o in objs if o.id == target_id), objs[0])
                if _op == "increment":
                    if not _field or not _pfield:
                        raise InvariantError("increment requires 'field' and 'param_field'", code="INVALID_OP")
                    cur = getattr(target, _field)
                    delta = pdata[_pfield]
                    ctx.modify(target, **{_field: cur + delta})
                    return {"id": target.id, _field: cur + delta}
                updates = {k: v for k, v in pdata.items() if k != "id" and k in _cls.model_fields}
                ctx.modify(target, **updates)
                return {"id": target.id, **updates}

            return _handler

        spec.declare_rule_action(
            name=act_name,
            params=params_cls,
            handler=_make_handler(target_cls, op, field_name, param_field),
            allowed_roles=["admin", "ops", "ai_agent"],
            description=str(ad.get("description") or ad.get("display") or act_name),
        )

        if ad.get("display"):
            act_display[act_name] = str(ad["display"])

    spec.display = {
        "objects": obj_display,
        "actions": act_display,
    }
    return spec


# ---------------------------------------------------------------------------
# 平台管理员服务（持有元本体 Runtime 并同步至 CommandCenterRegistry）
# ---------------------------------------------------------------------------
class MetaAdminService:
    """管理平台级 M2 元本体，并在提交后安全同步至 ``CommandCenterRegistry``。"""

    def __init__(self, registry: CommandCenterRegistry) -> None:
        self.registry = registry
        self._applied_sync_ids: set[str] = set()
        self._adapters: dict[str, dict[str, Any]] = {}
        self._adapter_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._adapter_thread: threading.Thread | None = None
        self.spec = self._build_spec()
        self.runtime = OntologyRuntime(self.spec, db_path=":memory:")
        self.actor = Actor(actor_id="platform:admin", role="admin", clearance_tags={"*"})
        self._bootstrap_from_registry()

    def _build_spec(self) -> OntologySpec:
        spec = OntologySpec()
        spec.display = {
            "objects": {
                "PlatformModel": "领域模型",
                "PlatformRuntime": "算力宿体",
                "PlatformWorld": "世界状态",
                "PlatformDevice": "外部设备",
                "PlatformSyncRecord": "设备同步记录",
                "PlatformAdapter": "设备适配器",
            },
            "actions": {
                "create_model_spec": "创建领域模型",
                "provision_runtime": "装配算力宿体",
                "create_world": "创建世界状态",
                "fork_world": "克隆平行沙盘",
                "register_device": "注册外部设备",
                "sync_device_to_world": "同步设备到世界状态",
                "create_adapter": "创建设备适配器",
                "toggle_adapter": "启停设备适配器",
            },
        }
        for cls in (
            PlatformModel,
            PlatformRuntime,
            PlatformWorld,
            PlatformDevice,
            PlatformSyncRecord,
            PlatformAdapter,
        ):
            spec.add_object_type(cls)

        @spec.edit_function(
            "create_model_spec",
            CreateModelSpecParams,
            description="声明式创建并校验新的 M1 领域模型",
        )
        def _create_model(ctx: Any, params: CreateModelSpecParams) -> dict[str, Any]:
            if params.model_id in self.registry.ontologies:
                raise InvariantError(f"model {params.model_id!r} already exists", code="MODEL_EXISTS")
            build_declarative_spec(
                object_types_json=params.object_types_json,
                actions_json=params.actions_json,
            )
            obj = PlatformModel(
                id=params.model_id,
                name=params.name,
                object_types_json=params.object_types_json,
                actions_json=params.actions_json,
                description=params.description,
                dynamic=True,
            )
            ctx.create(obj)
            return {"model_id": params.model_id}

        spec.declare_action(
            "create_model_spec",
            edit_function="create_model_spec",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "provision_runtime",
            ProvisionRuntimeParams,
            description="装配内存或 SQLite 运行时宿体",
        )
        def _provision_runtime(ctx: Any, params: ProvisionRuntimeParams) -> dict[str, Any]:
            if params.runtime_id in self.registry.runtimes:
                raise InvariantError(f"runtime {params.runtime_id!r} already exists", code="RUNTIME_EXISTS")
            obj = PlatformRuntime(
                id=params.runtime_id,
                label=params.label,
                storage_kind=params.storage_kind,
                db_uri=params.db_uri,
                description=params.description,
            )
            ctx.create(obj)
            return {"runtime_id": params.runtime_id}

        spec.declare_action(
            "provision_runtime",
            edit_function="provision_runtime",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "create_world",
            CreateWorldParams,
            description="绑定领域模型与宿体，创建新的世界状态",
        )
        def _create_world(ctx: Any, params: CreateWorldParams) -> dict[str, Any]:
            if params.world_id in self.registry.instances:
                raise InvariantError(f"world {params.world_id!r} already exists", code="WORLD_EXISTS")
            models = {m.id for m in ctx.query(PlatformModel)}
            if params.model_id not in models and params.model_id not in self.registry.ontologies:
                raise InvariantError(f"unknown model {params.model_id!r}", code="UNKNOWN_MODEL")
            obj = PlatformWorld(
                id=params.world_id,
                model_id=params.model_id,
                runtime_id=params.runtime_id,
                label=params.label or params.world_id,
                description=params.description,
            )
            ctx.create(obj)
            return {"world_id": params.world_id}

        spec.declare_action(
            "create_world",
            edit_function="create_world",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "fork_world",
            ForkWorldParams,
            description="从现有世界状态毫秒级克隆平行沙盘",
        )
        def _fork_world(ctx: Any, params: ForkWorldParams) -> dict[str, Any]:
            if params.source_world_id not in self.registry.instances:
                raise InvariantError(f"unknown source world {params.source_world_id!r}", code="UNKNOWN_WORLD")
            if params.new_world_id in self.registry.instances:
                raise InvariantError(f"world {params.new_world_id!r} already exists", code="WORLD_EXISTS")
            src = self.registry.instances[params.source_world_id]
            obj = PlatformWorld(
                id=params.new_world_id,
                model_id=src.ontology_id,
                runtime_id=src.runtime_id,
                label=params.label or f"{src.label} · 沙盘",
                source_world_id=params.source_world_id,
            )
            ctx.create(obj)
            return {"world_id": params.new_world_id}

        spec.declare_action(
            "fork_world",
            edit_function="fork_world",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "register_device",
            RegisterDeviceParams,
            description="注册外部工具/设备（OpenAPI）",
        )
        def _register_device(ctx: Any, params: RegisterDeviceParams) -> dict[str, Any]:
            obj = PlatformDevice(
                id=params.device_id,
                kind=params.kind,
                name=params.name,
                device_type=params.device_type,
                endpoint=params.endpoint,
            )
            ctx.create(obj)
            return {"device_id": params.device_id}

        spec.declare_action(
            "register_device",
            edit_function="register_device",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "sync_device_to_world",
            SyncDeviceParams,
            description="从外部设备拉取最新数据并同步写入目标世界状态的 Backing 平面",
        )
        def _sync_device_to_world(ctx: Any, params: SyncDeviceParams) -> dict[str, Any]:
            devices = {d.id: d for d in ctx.query(PlatformDevice)}
            if params.device_id not in devices:
                raise InvariantError(f"unknown device {params.device_id!r}", code="UNKNOWN_DEVICE")
            if params.world_id not in self.registry.instances:
                raise InvariantError(f"unknown world {params.world_id!r}", code="UNKNOWN_WORLD")
            target_rt = self.registry.instances[params.world_id].runtime
            if params.object_type not in target_rt.spec.object_types:
                raise InvariantError(
                    f"unknown object_type {params.object_type!r} in world {params.world_id!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            endpoint = devices[params.device_id].endpoint
            data_url, rows = _fetch_device_rows(endpoint, params.data_path, params.items_key)
            rec = PlatformSyncRecord(
                id=f"SYNC-{uuid.uuid4().hex[:8]}",
                device_id=params.device_id,
                world_id=params.world_id,
                object_type=params.object_type,
                data_url=data_url,
                rows_json=json.dumps(rows, ensure_ascii=False),
                row_count=len(rows),
            )
            ctx.create(rec)
            return {"sync_id": rec.id, "row_count": len(rows)}

        spec.declare_action(
            "sync_device_to_world",
            edit_function="sync_device_to_world",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "create_adapter",
            CreateAdapterParams,
            description="创建绑定外部设备与世界状态的轻量级同步适配器，支持异构字段映射与周期轮询",
        )
        def _create_adapter(ctx: Any, params: CreateAdapterParams) -> dict[str, Any]:
            devices = {d.id: d for d in ctx.query(PlatformDevice)}
            if params.device_id not in devices:
                raise InvariantError(f"unknown device {params.device_id!r}", code="UNKNOWN_DEVICE")
            if params.world_id not in self.registry.instances:
                raise InvariantError(f"unknown world {params.world_id!r}", code="UNKNOWN_WORLD")
            target_rt = self.registry.instances[params.world_id].runtime
            if params.object_type not in target_rt.spec.object_types:
                raise InvariantError(
                    f"unknown object_type {params.object_type!r} in world {params.world_id!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            try:
                mapping = json.loads(params.field_mapping_json or "{}")
            except json.JSONDecodeError as exc:
                raise InvariantError(f"invalid field_mapping_json: {exc}", code="INVALID_MAPPING") from exc
            if not isinstance(mapping, dict):
                raise InvariantError("field_mapping_json must be a JSON object", code="INVALID_MAPPING")
            obj = PlatformAdapter(
                id=params.adapter_id,
                device_id=params.device_id,
                world_id=params.world_id,
                object_type=params.object_type,
                data_path=params.data_path,
                items_key=params.items_key,
                field_mapping_json=json.dumps(mapping, ensure_ascii=False),
                interval_sec=max(0, int(params.interval_sec)),
                status="RUNNING",
            )
            ctx.create(obj)
            return {"adapter_id": params.adapter_id, "status": "RUNNING"}

        spec.declare_action(
            "create_adapter",
            edit_function="create_adapter",
            allowed_roles=["admin", "ai_agent"],
        )

        @spec.edit_function(
            "toggle_adapter",
            ToggleAdapterParams,
            description="暂停或恢复指定设备适配器（RUNNING / PAUSED）",
        )
        def _toggle_adapter(ctx: Any, params: ToggleAdapterParams) -> dict[str, Any]:
            adapters = {a.id: a for a in ctx.query(PlatformAdapter)}
            if params.adapter_id not in adapters:
                raise InvariantError(f"unknown adapter {params.adapter_id!r}", code="UNKNOWN_ADAPTER")
            adp = adapters[params.adapter_id]
            ctx.modify(adp, status=params.status)
            return {"adapter_id": params.adapter_id, "status": params.status}

        spec.declare_action(
            "toggle_adapter",
            edit_function="toggle_adapter",
            allowed_roles=["admin", "ai_agent"],
        )

        return spec

    def _bootstrap_from_registry(self) -> None:
        for oid, entry in self.registry.ontologies.items():
            self.runtime.put_object(
                PlatformModel(
                    id=oid,
                    name=entry.name,
                    description=entry.description,
                    dynamic=False,
                )
            )
        for rid, rentry in self.registry.runtimes.items():
            self.runtime.put_object(
                PlatformRuntime(
                    id=rid,
                    label=rentry.label,
                    storage_kind=rentry.storage_kind,
                    db_uri=rentry.db_uri,
                    description=rentry.description,
                )
            )
        for wid, wentry in self.registry.instances.items():
            self.runtime.put_object(
                PlatformWorld(
                    id=wid,
                    model_id=wentry.ontology_id,
                    runtime_id=wentry.runtime_id,
                    label=wentry.label,
                    description=wentry.description,
                )
            )
        for did, dentry in self.registry.symbolic_devices.items():
            self.runtime.put_object(
                PlatformDevice(
                    id=did,
                    kind="symbolic",
                    name=dentry.name,
                    device_type=dentry.kind,
                    endpoint=dentry.endpoint,
                    status=dentry.status,
                )
            )

    def sync_to_registry(self) -> None:
        """将已提交的元对象增量同步到 ``CommandCenterRegistry``。"""
        for m in self.runtime.query_objects(PlatformModel, self.actor):
            if m.id not in self.registry.ontologies:
                spec = build_declarative_spec(
                    object_types_json=m.object_types_json,
                    actions_json=m.actions_json,
                )
                self.registry.register_ontology(
                    m.id, m.name, spec, category="Dynamic", description=m.description
                )

        for r in self.runtime.query_objects(PlatformRuntime, self.actor):
            if r.id not in self.registry.runtimes:
                self.registry.register_runtime(
                    r.id, r.label, r.storage_kind, r.db_uri, description=r.description
                )

        for w in self.runtime.query_objects(PlatformWorld, self.actor):
            if w.id in self.registry.instances:
                continue
            if w.source_world_id and w.source_world_id in self.registry.instances:
                self.registry.fork_instance(w.source_world_id, w.id, w.label or None)
            elif w.model_id in self.registry.ontologies:
                spec = self.registry.ontologies[w.model_id].spec
                rt_entry = self.registry.runtimes.get(w.runtime_id)
                db_path = ":memory:"
                db_uri = "sqlite://:memory:"
                if rt_entry and rt_entry.storage_kind == "sqlite" and rt_entry.db_uri:
                    db_path = rt_entry.db_uri
                    db_uri = rt_entry.db_uri
                new_rt = OntologyRuntime(spec, db_path=db_path)
                self.registry.register_workspace(
                    w.id,
                    w.model_id,
                    w.label or w.id,
                    new_rt,
                    db_uri=db_uri,
                    description=w.description,
                )

        for d in self.runtime.query_objects(PlatformDevice, self.actor):
            if d.kind == "symbolic" and d.id not in self.registry.symbolic_devices:
                self.registry.register_symbolic_device(
                    d.id, d.name, d.device_type, d.endpoint, status=d.status
                )

        for rec in self.runtime.query_objects(PlatformSyncRecord, self.actor):
            if rec.id in self._applied_sync_ids:
                continue
            if rec.world_id in self.registry.instances:
                target_rt = self.registry.instances[rec.world_id].runtime
                rows = json.loads(rec.rows_json or "[]")
                target_rt.load_snapshot(
                    objects={rec.object_type: rows},
                    conflict_strategy="user_edits_win",
                    actor=self.actor,
                )
                self._applied_sync_ids.add(rec.id)

        with self._adapter_lock:
            for adp in self.runtime.query_objects(PlatformAdapter, self.actor):
                mapping = json.loads(adp.field_mapping_json or "{}")
                existing = self._adapters.get(adp.id)
                if existing is None:
                    self._adapters[adp.id] = {
                        "id": adp.id,
                        "device_id": adp.device_id,
                        "world_id": adp.world_id,
                        "object_type": adp.object_type,
                        "data_path": adp.data_path,
                        "items_key": adp.items_key,
                        "field_mapping": mapping,
                        "interval_sec": adp.interval_sec,
                        "status": adp.status,
                        "sync_count": 0,
                        "last_sync_at": None,
                        "last_sync_ts": 0.0,
                        "sync_error": None,
                    }
                else:
                    existing["status"] = adp.status
                    existing["field_mapping"] = mapping
                    existing["interval_sec"] = adp.interval_sec

            # 将已注册适配器标签同步到对应 RuntimeCatalogEntry.adapters
            rt_adapters: dict[str, list[str]] = {}
            for adp in self._adapters.values():
                wentry = self.registry.instances.get(adp["world_id"])
                if not wentry:
                    continue
                rid = wentry.runtime_id
                rt_adapters.setdefault(rid, []).append(
                    f"{adp['id']}: {adp['device_id']} → {adp['world_id']} [{adp['status']}]"
                )
            for rid, rentry in self.registry.runtimes.items():
                if rid in rt_adapters:
                    rentry.adapters = rt_adapters[rid]

    def list_adapters(self) -> list[dict[str, Any]]:
        """返回当前所有设备适配器的实时运行状态列表。"""
        self.sync_to_registry()
        with self._adapter_lock:
            return [
                {k: v for k, v in adp.items() if k != "last_sync_ts"}
                for adp in self._adapters.values()
            ]

    def run_adapter_once(self, adapter_id: str) -> dict[str, Any]:
        """立即执行一次适配器同步：从源设备拉取数据 -> 字段映射 -> 写入目标世界状态。"""
        self.sync_to_registry()
        with self._adapter_lock:
            adp = self._adapters.get(adapter_id)
            if adp is None:
                raise KeyError(f"unknown adapter {adapter_id!r}")
            device_id = adp["device_id"]
            world_id = adp["world_id"]
            object_type = adp["object_type"]
            data_path = adp["data_path"]
            items_key = adp["items_key"]
            mapping = dict(adp["field_mapping"])

        dentry = self.registry.symbolic_devices.get(device_id)
        if dentry is None:
            err = f"unknown symbolic device {device_id!r}"
            with self._adapter_lock:
                self._adapters[adapter_id]["sync_error"] = err
            raise KeyError(err)
        wentry = self.registry.instances.get(world_id)
        if wentry is None:
            err = f"unknown world {world_id!r}"
            with self._adapter_lock:
                self._adapters[adapter_id]["sync_error"] = err
            raise KeyError(err)

        try:
            _, raw_rows = _fetch_device_rows(dentry.endpoint, data_path, items_key)
            mapped_rows = apply_field_mapping(raw_rows, mapping)
            wentry.runtime.load_snapshot(
                objects={object_type: mapped_rows},
                conflict_strategy="user_edits_win",
                actor=self.actor,
            )
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._adapter_lock:
                state = self._adapters[adapter_id]
                state["sync_count"] += 1
                state["last_sync_at"] = now_iso
                state["last_sync_ts"] = time.monotonic()
                state["sync_error"] = None
            return {
                "adapter_id": adapter_id,
                "row_count": len(mapped_rows),
                "sync_count": state["sync_count"],
                "last_sync_at": now_iso,
            }
        except Exception as exc:
            with self._adapter_lock:
                self._adapters[adapter_id]["sync_error"] = str(exc)
            raise

    def ingest_adapter_webhook(
        self,
        adapter_id: str,
        payload: dict[str, Any] | list[dict[str, Any]],
    ) -> dict[str, Any]:
        """接收外部设备 Webhook 推送并经字段映射后即时写入目标世界状态。"""
        self.sync_to_registry()
        with self._adapter_lock:
            adp = self._adapters.get(adapter_id)
            if adp is None:
                raise KeyError(f"unknown adapter {adapter_id!r}")
            world_id = adp["world_id"]
            object_type = adp["object_type"]
            items_key = adp["items_key"]
            mapping = dict(adp["field_mapping"])

        wentry = self.registry.instances.get(world_id)
        if wentry is None:
            err = f"unknown world {world_id!r}"
            with self._adapter_lock:
                self._adapters[adapter_id]["sync_error"] = err
            raise KeyError(err)

        if isinstance(payload, list):
            raw_rows = payload
        elif isinstance(payload, dict):
            if items_key and isinstance(payload.get(items_key), list):
                raw_rows = payload[items_key]
            elif isinstance(payload.get("items"), list):
                raw_rows = payload["items"]
            elif isinstance(payload.get("item"), dict):
                raw_rows = [payload["item"]]
            else:
                raw_rows = [payload]
        else:
            raise ValueError("webhook payload must be a JSON object or array")

        mapped_rows = apply_field_mapping(raw_rows, mapping)
        wentry.runtime.load_snapshot(
            objects={object_type: mapped_rows},
            conflict_strategy="user_edits_win",
            actor=self.actor,
        )
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._adapter_lock:
            state = self._adapters[adapter_id]
            state["sync_count"] += 1
            state["last_sync_at"] = now_iso
            state["last_sync_ts"] = time.monotonic()
            state["sync_error"] = None
        return {
            "adapter_id": adapter_id,
            "row_count": len(mapped_rows),
            "sync_count": state["sync_count"],
            "last_sync_at": now_iso,
        }

    def toggle_adapter(self, adapter_id: str, status: str) -> dict[str, Any]:
        """直接切换适配器状态（RUNNING / PAUSED）并同步到元数据与注册表。"""
        if status not in ("RUNNING", "PAUSED"):
            raise ValueError(f"invalid status {status!r}")
        res = self.runtime.submit_action(
            f"ADP-TOGGLE-{uuid.uuid4().hex[:8]}",
            "toggle_adapter",
            {"adapter_id": adapter_id, "status": status},
            self.actor,
        )
        self.sync_to_registry()
        return res

    def start_adapter_loop(self, poll_tick_sec: float = 0.5) -> None:
        """启动后台守护线程，按各适配器的 ``interval_sec`` 周期性同步。"""
        if self._adapter_thread is not None and self._adapter_thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.sync_to_registry()
                    now = time.monotonic()
                    with self._adapter_lock:
                        due_ids = [
                            aid
                            for aid, adp in self._adapters.items()
                            if adp["status"] == "RUNNING"
                            and adp["interval_sec"] > 0
                            and (now - adp["last_sync_ts"]) >= adp["interval_sec"]
                        ]
                    for aid in due_ids:
                        if self._stop_event.is_set():
                            break
                        try:
                            self.run_adapter_once(aid)
                        except Exception:
                            pass
                except Exception:
                    pass
                self._stop_event.wait(poll_tick_sec)

        self._adapter_thread = threading.Thread(
            target=_loop,
            name="open-ontology-adapter-loop",
            daemon=True,
        )
        self._adapter_thread.start()

    def stop(self) -> None:
        """停止后台适配器轮询线程。"""
        self._stop_event.set()
        if self._adapter_thread is not None and self._adapter_thread.is_alive():
            self._adapter_thread.join(timeout=2.0)
        self._adapter_thread = None
