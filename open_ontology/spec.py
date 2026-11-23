"""L3 declarative ontology primitives.

This module contains the *declaration* layer only:

* :class:`Actor`          -- caller identity + row level security predicate
* :class:`ObjectType`     -- pydantic base class for domain entities
* :data:`AutoID`          -- system-generated identifier annotation
* :class:`ObjectProperty` -- OWL object property annotation & descriptor
* :class:`FunctionDef`    -- read-only computation
* :class:`EditFunctionDef` -- Turing-complete atomic write kernel
* :class:`ActionDef`      -- governed kinetic action
* :class:`OntologySpec`   -- registry / declaration DSL

No SQL and no mutation lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
import types as _types
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Iterable,
    Literal,
    Sequence,
    Union,
    get_args,
    get_origin,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo

__all__ = [
    "Actor",
    "AutoID",
    "ObjectType",
    "ObjectProperty",
    "InterfaceType",
    "RoleDef",
    "ScopedViewSpec",
    "FunctionDef",
    "EditFunctionDef",
    "ActionDef",
    "AutomationDef",
    "InvariantDef",
    "TransitionInvariantDef",
    "OntologySpec",
    "OpenOntologyError",
    "OntologyError",
    "PermissionDeniedError",
    "ObjectNotFoundError",
    "UnknownObjectTypeError",
    "UnknownObjectPropertyError",
    "UnknownRoleError",
    "ActionNotFoundError",
    "EditFunctionNotFoundError",
    "FunctionNotFoundError",
    "AutomationNotFoundError",
    "InvariantError",
    "ConcurrencyConflictError",
    "GovernanceViolationError",
    "ReindexConflictError",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class OpenOntologyError(Exception):
    """Base class for every OpenOntology error."""


# Literal alias used by the v3 specification.
OntologyError = OpenOntologyError


class PermissionDeniedError(OpenOntologyError):
    """Raised when RBAC or row level security rejects an operation."""


class ObjectNotFoundError(OpenOntologyError):
    """Raised when an object does not exist (or is not visible)."""


class UnknownObjectTypeError(OpenOntologyError):
    """Raised when a class / type name is not registered in the spec."""


class UnknownObjectPropertyError(OpenOntologyError):
    """Raised when an object property name is not registered in the spec."""


class ActionNotFoundError(OpenOntologyError):
    """Raised when an action is not registered in the spec."""


class EditFunctionNotFoundError(OpenOntologyError):
    """Raised when an edit function is not registered in the spec."""


class FunctionNotFoundError(OpenOntologyError):
    """Raised when a function is not registered in the spec."""


class AutomationNotFoundError(OpenOntologyError):
    """Raised when an automation is not registered in the spec."""


class UnknownRoleError(OpenOntologyError):
    """Raised when a role referenced by an action or object type is not declared."""


class InvariantError(OpenOntologyError):
    """Raised by action handlers when a business invariant is violated."""

    def __init__(self, message: str, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


class ConcurrencyConflictError(OpenOntologyError):
    """Raised when optimistic locking detects a stale write."""


class GovernanceViolationError(OpenOntologyError):
    """Raised when the physical write gate or authority boundary rejects a write."""

    def __init__(self, message: str, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


class ReindexConflictError(OpenOntologyError):
    """Raised when a snapshot reindex would orphan an ontology-owned patch."""


# ---------------------------------------------------------------------------
# Actor (RBAC + RLS)
# ---------------------------------------------------------------------------
class Actor(BaseModel):
    """Caller identity.

    ``clearance_tags`` is the row level security grant set.  A wildcard
    ``"*"`` grants access to every tagged object.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor_id: str
    role: str
    clearance_tags: set[str] = Field(default_factory=set)

    # -- RLS ---------------------------------------------------------------
    def has_access(self, object_security_tags: Iterable[str] | None) -> bool:
        """Non-raising visibility check used by query filters."""
        tags = set(object_security_tags or [])
        if not tags:
            return True
        if "*" in self.clearance_tags:
            return True
        return tags.issubset(self.clearance_tags)

    def can_access(self, object_security_tags: Iterable[str] | None) -> bool:
        """Strict RLS predicate.  Raises on denial."""
        if self.has_access(object_security_tags):
            return True
        missing = sorted(set(object_security_tags or []) - self.clearance_tags)
        raise PermissionDeniedError(
            f"actor {self.actor_id!r} (role={self.role!r}) lacks clearance tags {missing}"
        )


# ---------------------------------------------------------------------------
# Object / link declarations
# ---------------------------------------------------------------------------
class ObjectType(BaseModel):
    """Base class for every ontology object type.

    Subclasses declare three class attributes::

        class Warehouse(ObjectType):
            __primary_key__ = "warehouse_id"
            __title__ = "name"
            __security_tag_fields__ = ("region",)

            warehouse_id: str
            name: str
            region: str | None = None

    ``__security_tag_fields__`` values are turned into security tags of the
    form ``"<field>:<value>"`` (e.g. ``region:north``).
    """

    model_config = ConfigDict(extra="forbid")

    __primary_key__: str = "id"
    __title__: str | None = None
    __security_tag_fields__: tuple[str, ...] = ()
    __owned__: ClassVar[bool | dict[str, Any]] = False

    #: 该对象类型实现的接口名（由 ``spec.object_type()`` 校验必填字段）。
    __interfaces__: ClassVar[tuple[str, ...]] = ()
    #: 列级脱敏声明：``{字段名: (允许看明文的角色, ...)}``。
    __masked_fields__: ClassVar[dict[str, tuple[str, ...]]] = {}
    #: 可选的显式掩码值：``{字段名: 掩码值}``。
    __mask_defaults__: ClassVar[dict[str, Any]] = {}
    #: 是否为聚合根：纯声明式导航元数据，由模型作者显式声明，
    #: 不从图拓扑 / 归属 / 主键 / 类型名 / 连边情况推断。
    __aggregate_root__: ClassVar[bool] = False
    #: 外部源平面是否只读：为 True 时允许 SnapshotIngestionDriver 写入外部快照，
    #: 禁止任何 Action 修改该对象类型的外部源字段、创建/删除底表行或写入非自有关系。
    __read_only_source__: ClassVar[bool] = False
    #: 抽象类型标记：仅看当前类自身 ``__dict__``（不沿 MRO 自动传递到具体子类），
    #: 注册到 ``spec.abstract_types`` 供子本体继承与多态遍历，不对应具体对象表。
    __abstract__: ClassVar[bool] = False
    #: 只追加不可变事实标记：一旦 create 落库，禁止后续 Action update/delete（时光机冲正除外）。
    __append_only__: ClassVar[bool] = False
    #: 可选声明式视图标签，用于 Scoped OSDK View 按上下文投影裁剪。
    __view_tags__: ClassVar[tuple[str, ...]] = ()
    #: 从字段 ``ObjectProperty[...]`` 注解自动提取的关系索引（按类型隔离）。
    __object_properties__: ClassVar[dict[str, "ObjectProperty"]] = {}

    # -- identity ----------------------------------------------------------
    @classmethod
    def object_type_name(cls) -> str:
        return cls.__name__

    @classmethod
    def primary_key_name(cls) -> str:
        return getattr(cls, "__primary_key__", "id")

    @classmethod
    def owned_spec(cls) -> bool | dict[str, Any]:
        """Return the field-level sovereignty declaration."""
        return getattr(cls, "__owned__", False)

    @classmethod
    def is_fully_owned(cls) -> bool:
        return cls.owned_spec() is True

    @classmethod
    def owned_field_defaults(cls) -> dict[str, Any]:
        owned = cls.owned_spec()
        if isinstance(owned, dict):
            return dict(owned)
        return {}

    @classmethod
    def is_aggregate_root(cls) -> bool:
        """聚合根是**声明**出来的，不是从图结构推断出来的。"""
        return bool(getattr(cls, "__aggregate_root__", False))

    @classmethod
    def is_read_only_source(cls) -> bool:
        """外部源平面是否声明为只读（仅允许快照摄入，禁止 Action 改写）。"""
        return bool(getattr(cls, "__read_only_source__", False))

    @classmethod
    def is_abstract(cls) -> bool:
        """仅检查当前类自身 ``__dict__`` 是否声明为抽象类型（不沿 MRO 传染给子类）。"""
        return bool(cls.__dict__.get("__abstract__", False))

    @classmethod
    def is_append_only(cls) -> bool:
        """对象是否声明为只追加不可变事实（沿 MRO 继承，子类可覆写）。"""
        return bool(getattr(cls, "__append_only__", False))

    @classmethod
    def ontology_superclasses(cls) -> list[str]:
        """沿 MRO 返回继承的上位 ``ObjectType`` 类名列表（不含 ``ObjectType`` 自身）。"""
        return [
            base.__name__
            for base in cls.__mro__[1:]
            if isinstance(base, type)
            and issubclass(base, ObjectType)
            and base is not ObjectType
        ]

    @classmethod
    def type_metadata(cls) -> dict[str, Any]:
        """``ontology_registry`` 持久化的 TYPE 元数据（JSON 安全，不含实例值）。"""
        return {
            "name": cls.__name__,
            "primary_key": cls.primary_key_name(),
            "title": getattr(cls, "__title__", None),
            "security_tag_fields": list(
                getattr(cls, "__security_tag_fields__", ()) or ()
            ),
            "aggregate_root": cls.is_aggregate_root(),
            "append_only": cls.is_append_only(),
        }

    def primary_key_value(self) -> str:
        value = getattr(self, self.primary_key_name(), None)
        if value is None or str(value) == "":
            raise ValueError(
                f"{type(self).__name__}.{self.primary_key_name()} is empty; "
                "a primary key is required"
            )
        return str(value)

    # -- presentation ------------------------------------------------------
    def title_value(self) -> str | None:
        title_field = getattr(type(self), "__title__", None)
        if not title_field:
            return None
        value = getattr(self, title_field, None)
        return None if value is None else str(value)

    def security_tags(self) -> list[str]:
        """Derive ``field:value`` security tags from configured fields."""
        tags: list[str] = []
        for field_name in getattr(type(self), "__security_tag_fields__", ()) or ():
            value = getattr(self, field_name, None)
            if value is None or value == "":
                continue
            values = value if isinstance(value, (list, tuple, set)) else [value]
            for item in values:
                if item is None or item == "":
                    continue
                tags.append(f"{field_name}:{item}")
        # deterministic order, no duplicates
        return sorted(dict.fromkeys(tags))

    # -- serialization -----------------------------------------------------
    def to_properties(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def source_properties(self) -> dict[str, Any]:
        """Base-row payload: strip every field declared in ``__owned__``."""
        data = self.to_properties()
        for field_name in self.owned_field_defaults():
            data.pop(field_name, None)
        return data


@dataclass(frozen=True)
class _AutoIDMeta:
    prefix: str = ""


AutoID = Annotated[
    str,
    _AutoIDMeta(),
    WithJsonSchema({"type": "string", "auto_generated": True}),
]


@dataclass(frozen=True)
class _ObjectPropertyMeta:
    range: str
    multiple: bool = False
    property_name: str | None = None
    owned: bool | None = None


def _resolve_target_name(target: Any) -> str:
    if isinstance(target, str):
        return target
    if hasattr(target, "__forward_arg__"):
        return str(target.__forward_arg__)
    if hasattr(target, "__name__"):
        return str(target.__name__)
    raise TypeError(
        f"ObjectProperty[...] expects an ObjectType class or name, got {target!r}"
    )


def _walk_annotation_meta(annotation: Any, target_cls: type[Any]) -> Any | None:
    if annotation is None:
        return None
    for item in getattr(annotation, "__metadata__", ()) or ():
        if isinstance(item, target_cls):
            return item
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        for meta in args[1:]:
            if isinstance(meta, target_cls):
                return meta
        if args:
            return _walk_annotation_meta(args[0], target_cls)
    if origin in (Union, _types.UnionType):
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _walk_annotation_meta(arg, target_cls)
            if found is not None:
                return found
    return None


def extract_auto_id_meta(info: FieldInfo) -> bool:
    for meta in info.metadata:
        if isinstance(meta, _AutoIDMeta):
            return True
        js = getattr(meta, "json_schema", None)
        if isinstance(js, dict) and js.get("auto_generated"):
            return True
    if _walk_annotation_meta(info.annotation, _AutoIDMeta) is not None:
        return True
    extra = info.json_schema_extra
    if isinstance(extra, dict) and extra.get("auto_generated"):
        return True
    return False


def extract_object_property_meta(info: FieldInfo) -> tuple[str, bool] | None:
    for meta in info.metadata:
        if isinstance(meta, _ObjectPropertyMeta):
            return meta.range, meta.multiple
    found = _walk_annotation_meta(info.annotation, _ObjectPropertyMeta)
    if isinstance(found, _ObjectPropertyMeta):
        return found.range, found.multiple
    extra = info.json_schema_extra
    if isinstance(extra, dict) and extra.get("property_kind") == "object" and extra.get("range"):
        return str(extra["range"]), bool(extra.get("multiple", False))
    return None


class ObjectProperty(BaseModel):
    """OWL object property declaration & type annotation helper.

    As a field annotation on an :class:`ObjectType` or parameter model::

        assignee: ObjectProperty[Member] | None = None
        waits_for: ObjectProperty[tuple[Card, ...]] = ()

    As an explicit M2 descriptor (for many-to-many or unattached relations)::

        ObjectProperty(name="feeds", domain="ScrapLot", range="SteelCoil", cardinality="many-to-many")
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    domain: str
    range: str
    cardinality: Literal[
        "one-to-one", "one-to-many", "many-to-one", "many-to-many"
    ] = "many-to-many"
    owned: bool = False
    field: str | None = None
    description: str = ""

    @field_validator("domain", "range", mode="before")
    @classmethod
    def _coerce_type_name(cls, value: Any) -> str:
        return value.__name__ if isinstance(value, type) else str(value)

    @field_validator("cardinality", mode="before")
    @classmethod
    def _coerce_cardinality(cls, value: Any) -> str:
        return str(value).replace("_", "-")

    @classmethod
    def __class_getitem__(cls, item: Any) -> Any:
        origin = get_origin(item)
        if origin in (tuple, list, set, frozenset):
            args = get_args(item)
            if not args:
                raise TypeError("ObjectProperty[tuple[Target, ...]] requires a target type")
            target_name = _resolve_target_name(args[0])
            return Annotated[
                tuple[str, ...],
                _ObjectPropertyMeta(range=target_name, multiple=True),
                WithJsonSchema(
                    {
                        "type": "array",
                        "items": {"type": "string"},
                        "property_kind": "object",
                        "range": target_name,
                        "multiple": True,
                    }
                ),
            ]
        target_name = _resolve_target_name(item)
        return Annotated[
            str,
            _ObjectPropertyMeta(range=target_name, multiple=False),
            WithJsonSchema(
                {
                    "type": "string",
                    "property_kind": "object",
                    "range": target_name,
                    "multiple": False,
                }
            ),
        ]


class InterfaceType(BaseModel):
    """跨对象类型的多态接口声明（Palantir OSv2 Interface）。

    实现该接口的 ``ObjectType`` 必须在 ``__interfaces__`` 中列出接口名，
    并至少声明 ``required_fields`` 里的全部字段。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required_fields: tuple[str, ...] = ()
    description: str = ""


class RoleDef(BaseModel):
    """Declarative role definition belonging to an ontology model (M1).

    Declares a named role in the ontology's authorization vocabulary so actions
    and masked fields can be validated against an explicit set of model roles.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""


class ScopedViewSpec(BaseModel):
    """Declarative consumption-layer projection (Scoped OSDK View) over an OntologySpec.

    Constrains which object types, actions, and functions a downstream consumer
    role or bounded context sees while preserving full underlying spec invariants.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    roles: tuple[str, ...] = ()
    view_tags: tuple[str, ...] = ()
    object_types: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    include_reachable_objects: bool = True


# ---------------------------------------------------------------------------
# Function / edit-function / action declarations
# ---------------------------------------------------------------------------
class FunctionDef(BaseModel):
    """Read-only computation registered on the spec."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    params: Any  # type[BaseModel]
    handler: Any  # Callable[[EditBatch, BaseModel], Any]
    description: str = ""
    read_only: bool = True
    view_tags: tuple[str, ...] = ()


class EditFunctionDef(BaseModel):
    """Atomic Turing-complete write transformation kernel (free of governance shell)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    params: Any  # type[BaseModel]
    handler: Any  # Callable[[EditBatch, BaseModel], Any]
    description: str = ""
    touched_object_types: tuple[str, ...] = ()


class ActionDef(BaseModel):
    """Governed kinetic action registered on the spec."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    params: Any  # type[BaseModel]
    handler: Any  # Callable[[EditBatch, BaseModel], None]
    implementation_kind: Literal["rule", "edit_function"] = "edit_function"
    edit_function: str | None = None
    allowed_roles: tuple[str, ...] = ()
    revert_roles: tuple[str, ...] = ()
    affects_source: bool = True
    visibility: Literal["public", "private"] = "public"
    description: str = ""
    view_tags: tuple[str, ...] = ()
    touched_object_types: tuple[str, ...] = ()
    submission_criteria: tuple[Any, ...] = ()

    @model_validator(mode="after")
    def _validate_implementation_binding(self) -> "ActionDef":
        if self.implementation_kind == "edit_function":
            if not isinstance(self.edit_function, str) or not self.edit_function.strip():
                raise ValueError(
                    f"ActionDef {self.name!r} with implementation_kind='edit_function' "
                    "requires a non-empty edit_function name"
                )
        elif self.implementation_kind == "rule":
            if self.edit_function is not None:
                raise ValueError(
                    f"ActionDef {self.name!r} with implementation_kind='rule' "
                    "must have edit_function=None"
                )
        return self


class AutomationDef(BaseModel):
    """事件驱动的条件反射声明（Palantir OSv2 Automation）。

    ``handler(runtime, obj, actor)`` 可以自由调用 ``runtime.call_function`` /
    ``runtime.submit_action``；返回值会原样记入触发历史。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    watch_object_type: str
    condition: Any  # Callable[[ObjectType], bool]
    handler: Any  # Callable[[OntologyRuntime, ObjectType, Actor], dict | None]
    description: str = ""


class InvariantDef(BaseModel):
    """Post-state invariant registered on the spec."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    handler: Any  # Callable[[InvariantView], Any]
    code: str | None = None
    description: str = ""


class TransitionInvariantDef(BaseModel):
    """State-transition invariant evaluated over ``ObjectDiff`` and post-state."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    handler: Any  # Callable[[ObjectDiff, InvariantView], Any] | Callable[[ObjectDiff], Any]
    code: str | None = None
    description: str = ""


_UNSET = object()


def _merge_dict_attr(
    ancestors: list[type[ObjectType]],
    attr: str,
    valid_fields: Any,
    *,
    coerce_value: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for base in ancestors:
        raw = base.__dict__.get(attr)
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if key in valid_fields:
                merged[key] = coerce_value(value) if coerce_value else value
    return merged


def _merge_inherited_object_metadata(cls: type[ObjectType]) -> None:
    """Along MRO (base -> child), merge interfaces, masks, defaults, and ownership."""
    ancestors = [
        base
        for base in reversed(cls.__mro__)
        if isinstance(base, type)
        and issubclass(base, ObjectType)
        and base is not ObjectType
    ]
    if len(ancestors) <= 1:
        return

    merged_interfaces: list[str] = []
    for base in ancestors:
        for iface in base.__dict__.get("__interfaces__", ()) or ():
            if iface not in merged_interfaces:
                merged_interfaces.append(iface)
    if merged_interfaces:
        cls.__interfaces__ = tuple(merged_interfaces)

    valid_fields = cls.model_fields
    merged_masked = _merge_dict_attr(
        ancestors, "__masked_fields__", valid_fields, coerce_value=lambda v: tuple(v or ())
    )
    if merged_masked:
        cls.__masked_fields__ = merged_masked

    merged_defaults = _merge_dict_attr(ancestors, "__mask_defaults__", valid_fields)
    if merged_defaults:
        cls.__mask_defaults__ = merged_defaults

    own_local = cls.__dict__.get("__owned__", _UNSET)
    if isinstance(own_local, bool):
        return
    if isinstance(own_local, dict):
        merged_owned = _merge_dict_attr(ancestors[:-1], "__owned__", valid_fields)
        merged_owned.update(own_local)
        cls.__owned__ = merged_owned
        return

    dict_ancestors: list[dict[str, Any]] = []
    for base in reversed(ancestors[:-1]):
        val = base.__dict__.get("__owned__", _UNSET)
        if isinstance(val, bool):
            if not dict_ancestors:
                cls.__owned__ = val
                return
            break
        if isinstance(val, dict):
            dict_ancestors.append(val)
    if dict_ancestors:
        merged_owned: dict[str, Any] = {}
        for raw in reversed(dict_ancestors):
            for key, value in raw.items():
                if key in valid_fields:
                    merged_owned[key] = value
        cls.__owned__ = merged_owned


def _merge_spec_registry(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    kind: str,
    identity: bool = False,
) -> None:
    for name, incoming in source.items():
        if name in target:
            existing = target[name]
            conflict = existing is not incoming if identity else existing != incoming
            if conflict:
                raise ValueError(
                    f"conflicting {kind} {name!r} when extending OntologySpec"
                )
        target[name] = incoming


# ---------------------------------------------------------------------------
# Spec registry
# ---------------------------------------------------------------------------
class OntologySpec:
    """Declarative registry for object types, object properties, functions, actions."""

    def __init__(
        self,
        read_only_source: bool = False,
        *,
        extends: Sequence["OntologySpec"] = (),
    ) -> None:
        self.read_only_source: bool = bool(read_only_source)
        self.parents: list[OntologySpec] = []
        self.abstract_types: dict[str, type[ObjectType]] = {}
        self.object_types: dict[str, type[ObjectType]] = {}
        self.object_properties: dict[str, ObjectProperty] = {}
        self.interfaces: dict[str, InterfaceType] = {}
        self.roles: dict[str, RoleDef] = {}
        self.views: dict[str, ScopedViewSpec] = {}
        self.functions: dict[str, FunctionDef] = {}
        self.edit_functions: dict[str, EditFunctionDef] = {}
        self.actions: dict[str, ActionDef] = {}
        self.automations: dict[str, AutomationDef] = {}
        self.invariants: dict[str, InvariantDef] = {}
        self.transition_invariants: dict[str, TransitionInvariantDef] = {}
        #: 本蓝图自己的展示词条（中文名等）。展示层会把这些词条动态合并，
        #: 因此新增一个业务本体不需要修改任何内核文件。
        #: 结构：``{"objects": {...}, "object_properties": {...}, "fields": {...}, ...}``
        self.display: dict[str, dict[str, str]] = {}
        for parent in extends:
            self.extend(parent)

    def extend(self, parent: "OntologySpec") -> "OntologySpec":
        """Inherit abstract types, object types, relations, roles, invariants, and display."""
        if not isinstance(parent, OntologySpec):
            raise TypeError(f"extend() expects an OntologySpec, got {parent!r}")
        if parent is self:
            raise ValueError("an OntologySpec cannot extend itself")
        if parent in self.parents:
            return self
        self.parents.append(parent)
        _merge_spec_registry(self.abstract_types, parent.abstract_types, kind="abstract type", identity=True)
        _merge_spec_registry(self.object_types, parent.object_types, kind="object type", identity=True)
        _merge_spec_registry(self.object_properties, parent.object_properties, kind="object property")
        _merge_spec_registry(self.interfaces, parent.interfaces, kind="interface")
        _merge_spec_registry(self.roles, parent.roles, kind="role")
        _merge_spec_registry(self.views, parent.views, kind="view")
        _merge_spec_registry(self.invariants, parent.invariants, kind="invariant")
        _merge_spec_registry(
            self.transition_invariants,
            parent.transition_invariants,
            kind="transition invariant",
        )
        _merge_spec_registry(self.functions, parent.functions, kind="function")
        _merge_spec_registry(self.edit_functions, parent.edit_functions, kind="edit function")
        _merge_spec_registry(self.actions, parent.actions, kind="action")
        _merge_spec_registry(self.automations, parent.automations, kind="automation")
        for section, mapping in parent.display.items():
            bucket = self.display.setdefault(section, {})
            for key, value in mapping.items():
                bucket.setdefault(key, value)
        return self

    def update_display(self, mapping: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        """Merge child display labels on top of inherited display dictionaries."""
        for section, entries in mapping.items():
            bucket = self.display.setdefault(section, {})
            bucket.update(entries)
        return self.display

    def _rebuild_model(self, model_cls: type[BaseModel]) -> None:
        if not getattr(model_cls, "__pydantic_complete__", True):
            ns: dict[str, Any] = {
                "ObjectProperty": ObjectProperty,
                "AutoID": AutoID,
                **self.abstract_types,
                **self.object_types,
            }
            if issubclass(model_cls, ObjectType):
                ns[model_cls.__name__] = model_cls
            model_cls.model_rebuild(force=True, _types_namespace=ns)

    # -- object types ------------------------------------------------------
    def object_type(self, cls: type[ObjectType]) -> type[ObjectType]:
        if not (isinstance(cls, type) and issubclass(cls, ObjectType)):
            raise TypeError("object_type() expects an ObjectType subclass")
        self._rebuild_model(cls)
        _merge_inherited_object_metadata(cls)
        if cls.is_read_only_source() and cls.is_fully_owned():
            raise ValueError(
                f"{cls.__name__}: __read_only_source__=True cannot be combined "
                "with __owned__=True (fully owned types have no external source)"
            )
        self._validate_interfaces(cls)
        masked = getattr(cls, "__masked_fields__", {}) or {}
        for field_name, allowed in masked.items():
            self._validate_roles(
                allowed or (),
                context=f"object type {cls.__name__} masked field {field_name!r}",
            )
        if cls.is_abstract():
            self.abstract_types[cls.__name__] = cls
            return cls
        self.object_types[cls.__name__] = cls
        self._register_field_object_properties(cls)
        return cls

    def _register_field_object_properties(self, cls: type[ObjectType]) -> None:
        owned_spec = cls.owned_spec()
        extracted: dict[str, ObjectProperty] = {}
        for field_name, info in cls.model_fields.items():
            meta = extract_object_property_meta(info)
            if meta is None:
                continue
            range_name, multiple = meta
            if owned_spec is True:
                is_owned = True
            elif isinstance(owned_spec, dict):
                is_owned = bool(owned_spec.get(field_name, False))
            else:
                is_owned = False
            prop_name = f"{cls.__name__}.{field_name}"
            decl = ObjectProperty(
                name=prop_name,
                domain=cls.__name__,
                range=range_name,
                cardinality="many-to-many" if multiple else "many-to-one",
                owned=is_owned,
                field=field_name,
                description=info.description or "",
            )
            extracted[field_name] = decl
            self.object_properties[decl.name] = decl
        cls.__object_properties__ = extracted

    def _validate_interfaces(self, cls: type[ObjectType]) -> None:
        for interface_name in getattr(cls, "__interfaces__", ()) or ():
            if interface_name not in self.interfaces:
                raise UnknownObjectTypeError(
                    f"object type {cls.__name__!r} declares unknown interface "
                    f"{interface_name!r}; register it with spec.interface() first"
                )
            required = self.interfaces[interface_name].required_fields
            missing = [name for name in required if name not in cls.model_fields]
            if missing:
                raise TypeError(
                    f"object type {cls.__name__!r} implements interface "
                    f"{interface_name!r} but is missing required fields {missing}"
                )

    def add_object_type(self, cls: type[ObjectType]) -> type[ObjectType]:
        return self.object_type(cls)

    def get_object_type(self, cls_or_name: type[ObjectType] | str) -> type[ObjectType]:
        if isinstance(cls_or_name, str):
            if cls_or_name in self.abstract_types:
                raise GovernanceViolationError(
                    f"abstract object type {cls_or_name!r} cannot be instantiated "
                    "or accessed as a concrete table"
                )
            if cls_or_name not in self.object_types:
                raise UnknownObjectTypeError(f"unknown object type: {cls_or_name!r}")
            return self.object_types[cls_or_name]
        if not issubclass(cls_or_name, ObjectType):
            raise UnknownObjectTypeError(f"not an ObjectType subclass: {cls_or_name!r}")
        name = cls_or_name.__name__
        if name in self.abstract_types or cls_or_name.is_abstract():
            raise GovernanceViolationError(
                f"abstract object type {name!r} cannot be instantiated "
                "or accessed as a concrete table"
            )
        if name not in self.object_types:
            raise UnknownObjectTypeError(f"object type not registered: {name!r}")
        return self.object_types[name]

    def subtypes_of(self, base: type[ObjectType] | str) -> list[type[ObjectType]]:
        """Return all registered concrete subclasses of ``base`` (abstract or concrete)."""
        if isinstance(base, str):
            base_cls = self.abstract_types.get(base) or self.object_types.get(base)
            if base_cls is None:
                raise UnknownObjectTypeError(f"unknown object type: {base!r}")
        elif isinstance(base, type) and issubclass(base, ObjectType):
            if (
                base is not ObjectType
                and base.__name__ not in self.abstract_types
                and base.__name__ not in self.object_types
            ):
                raise UnknownObjectTypeError(
                    f"object type not registered: {base.__name__!r}"
                )
            base_cls = base
        else:
            raise UnknownObjectTypeError(f"not an ObjectType subclass: {base!r}")
        return [
            cls
            for cls in self.object_types.values()
            if issubclass(cls, base_cls)
        ]

    # -- object properties -------------------------------------------------
    def add_object_property(
        self,
        prop: ObjectProperty | str,
        domain: str | type[ObjectType] | None = None,
        range: str | type[ObjectType] | None = None,
        **kwargs: Any,
    ) -> ObjectProperty:
        if isinstance(prop, ObjectProperty):
            decl = prop
        else:
            decl = ObjectProperty(name=prop, domain=domain, range=range, **kwargs)
        self.object_properties[decl.name] = decl
        if decl.field and decl.domain in self.object_types:
            dom_cls = self.object_types[decl.domain]
            dom_props = dict(getattr(dom_cls, "__object_properties__", {}) or {})
            dom_props[decl.field] = decl
            dom_cls.__object_properties__ = dom_props
        return decl

    def object_property(
        self,
        name: str,
        domain: str | type[ObjectType],
        range: str | type[ObjectType],
        **kwargs: Any,
    ) -> ObjectProperty:
        return self.add_object_property(name, domain=domain, range=range, **kwargs)

    def get_object_property(
        self,
        prop: str | ObjectProperty,
        domain: str | type[ObjectType] | None = None,
    ) -> ObjectProperty:
        if isinstance(prop, ObjectProperty):
            if prop.name in self.object_properties:
                return self.object_properties[prop.name]
            raise UnknownObjectPropertyError(f"unknown object property: {prop.name!r}")
        name = str(prop)
        if name in self.object_properties:
            return self.object_properties[name]
        if domain is not None:
            dom_name = domain.__name__ if isinstance(domain, type) else str(domain)
            dom_cls = self.object_types.get(dom_name)
            if dom_cls is not None:
                found = getattr(dom_cls, "__object_properties__", {}).get(name)
                if found is not None:
                    return found
        matches = [
            decl for decl in self.object_properties.values() if decl.field == name
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise UnknownObjectPropertyError(
                f"ambiguous object property {name!r}; use '<Domain>.{name}'"
            )
        raise UnknownObjectPropertyError(f"unknown object property: {name!r}")

    # -- interfaces --------------------------------------------------------
    def interface(
        self,
        name: str,
        required_fields: Sequence[str] = (),
        description: str = "",
    ) -> InterfaceType:
        decl = InterfaceType(
            name=name,
            required_fields=tuple(required_fields),
            description=description,
        )
        self.interfaces[decl.name] = decl
        return decl

    def add_interface(self, interface: InterfaceType) -> InterfaceType:
        self.interfaces[interface.name] = interface
        return interface

    def get_interface(self, interface: str | InterfaceType) -> InterfaceType:
        name = interface.name if isinstance(interface, InterfaceType) else str(interface)
        if name not in self.interfaces:
            raise UnknownObjectTypeError(f"unknown interface: {name!r}")
        return self.interfaces[name]

    # -- roles -------------------------------------------------------------
    def role(self, name: str, description: str = "") -> RoleDef:
        """Declare a model role on this ontology spec."""
        return self.add_role(RoleDef(name=name, description=description))

    def add_role(self, role: RoleDef) -> RoleDef:
        clean = role.name.strip()
        if not clean:
            raise ValueError("role name must not be empty")
        if clean != role.name:
            role = RoleDef(name=clean, description=role.description)
        self.roles[role.name] = role
        return role

    def get_role(self, role: str | RoleDef) -> RoleDef:
        name = role.name if isinstance(role, RoleDef) else str(role)
        if name not in self.roles:
            raise UnknownRoleError(f"unknown role: {name!r}")
        return self.roles[name]

    def _validate_roles(self, roles: Sequence[str], *, context: str) -> None:
        if not self.roles:
            return
        for role_name in roles:
            if role_name not in self.roles:
                raise UnknownRoleError(
                    f"{context}: unknown role {role_name!r} "
                    f"(declared roles: {sorted(self.roles)})"
                )

    def object_types_for_interface(self, interface: str | InterfaceType) -> list[type[ObjectType]]:
        name = self.get_interface(interface).name
        return [
            cls
            for cls in self.object_types.values()
            if name in (getattr(cls, "__interfaces__", ()) or ())
        ]

    # -- scoped views ------------------------------------------------------
    def register_view(self, view: ScopedViewSpec) -> ScopedViewSpec:
        """Register a declarative Scoped OSDK View over this ontology spec."""
        clean = view.name.strip()
        if not clean:
            raise ValueError("view name must not be empty")
        self._validate_roles(view.roles, context=f"view {clean!r} roles")
        for ot_name in view.object_types:
            if ot_name not in self.object_types:
                raise UnknownObjectTypeError(
                    f"view {clean!r} references unknown object type {ot_name!r}"
                )
        for act_name in view.actions:
            if act_name not in self.actions:
                raise ActionNotFoundError(
                    f"view {clean!r} references unknown action {act_name!r}"
                )
        for fn_name in view.functions:
            if fn_name not in self.functions:
                raise FunctionNotFoundError(
                    f"view {clean!r} references unknown function {fn_name!r}"
                )
        if clean != view.name:
            view = view.model_copy(update={"name": clean})
        self.views[clean] = view
        return view

    def get_view(self, name: str) -> ScopedViewSpec:
        if name not in self.views:
            raise OpenOntologyError(f"unknown view: {name!r}")
        return self.views[name]

    # -- automations -------------------------------------------------------
    def automation(
        self,
        name: str,
        watch_object_type: type[ObjectType] | str,
        condition: Callable[[Any], bool],
        description: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        watch = (
            watch_object_type
            if isinstance(watch_object_type, str)
            else watch_object_type.__name__
        )

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.automations[name] = AutomationDef(
                name=name,
                watch_object_type=watch,
                condition=condition,
                handler=handler,
                description=description or (handler.__doc__ or "").strip(),
            )
            return handler

        return decorator

    def get_automation(self, name: str) -> AutomationDef:
        if name not in self.automations:
            raise AutomationNotFoundError(f"unknown automation: {name!r}")
        return self.automations[name]

    # -- invariants & transition invariants --------------------------------
    @staticmethod
    def _register_invariant_entry(
        registry: dict[str, Any],
        entry_cls: type[Any],
        name: str | Callable[..., Any],
        *,
        code: str | None = None,
        description: str = "",
        kind: str = "invariant",
    ) -> Callable[..., Any]:
        if callable(name):
            handler = name
            inv_name = handler.__name__
            registry[inv_name] = entry_cls(
                name=inv_name,
                handler=handler,
                code=code,
                description=description or (handler.__doc__ or "").strip(),
            )
            return handler

        inv_name = str(name).strip()
        if not inv_name:
            raise ValueError(f"{kind} name must not be empty")

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            registry[inv_name] = entry_cls(
                name=inv_name,
                handler=handler,
                code=code,
                description=description or (handler.__doc__ or "").strip(),
            )
            return handler

        return decorator

    def invariant(
        self,
        name: str | Callable[..., Any],
        *,
        code: str | None = None,
        description: str = "",
    ) -> Callable[..., Any]:
        return self._register_invariant_entry(
            self.invariants,
            InvariantDef,
            name,
            code=code,
            description=description,
            kind="invariant",
        )

    def get_invariant(self, name: str) -> InvariantDef:
        if name not in self.invariants:
            raise OpenOntologyError(f"unknown invariant: {name!r}")
        return self.invariants[name]

    def transition_invariant(
        self,
        name: str | Callable[..., Any],
        *,
        code: str | None = None,
        description: str = "",
    ) -> Callable[..., Any]:
        return self._register_invariant_entry(
            self.transition_invariants,
            TransitionInvariantDef,
            name,
            code=code,
            description=description,
            kind="transition invariant",
        )

    def get_transition_invariant(self, name: str) -> TransitionInvariantDef:
        if name not in self.transition_invariants:
            raise OpenOntologyError(f"unknown transition invariant: {name!r}")
        return self.transition_invariants[name]

    # -- functions ---------------------------------------------------------
    def function(
        self,
        name: str,
        params: type[BaseModel],
        description: str = "",
        view_tags: Iterable[str] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        _validate_params(params, name)
        self._rebuild_model(params)

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.functions[name] = FunctionDef(
                name=name,
                params=params,
                handler=handler,
                description=description or (handler.__doc__ or "").strip(),
                view_tags=tuple(view_tags),
            )
            return handler

        return decorator

    def get_function(self, name: str) -> FunctionDef:
        if name not in self.functions:
            raise FunctionNotFoundError(f"unknown function: {name!r}")
        return self.functions[name]

    # -- edit functions ----------------------------------------------------
    def edit_function(
        self,
        name: str,
        params: type[BaseModel],
        *,
        description: str = "",
        touched_object_types: Iterable[str] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a pure Turing-complete write kernel (free of governance shell)."""
        clean = str(name).strip()
        if not clean:
            raise ValueError("edit_function name must not be empty")
        _validate_params(params, clean)
        self._rebuild_model(params)

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.edit_functions[clean] = EditFunctionDef(
                name=clean,
                params=params,
                handler=handler,
                description=description or (handler.__doc__ or "").strip(),
                touched_object_types=tuple(touched_object_types),
            )
            return handler

        return decorator

    def get_edit_function(self, name: str) -> EditFunctionDef:
        if name not in self.edit_functions:
            raise EditFunctionNotFoundError(f"unknown edit function: {name!r}")
        return self.edit_functions[name]

    # -- actions -----------------------------------------------------------
    def declare_action(
        self,
        name: str,
        *,
        edit_function: str,
        allowed_roles: Sequence[str] = (),
        revert_roles: Sequence[str] = (),
        description: str = "",
        affects_source: bool = True,
        visibility: Literal["public", "private"] = "public",
        view_tags: Iterable[str] = (),
        touched_object_types: Iterable[str] = (),
        submission_criteria: Sequence[Any] = (),
        params: type[BaseModel] | None = None,
    ) -> ActionDef:
        """Bind a governed Action shell onto an already-registered EditFunctionDef."""
        clean = str(name).strip()
        if not clean:
            raise ValueError("action name must not be empty")
        if self.read_only_source and affects_source:
            raise GovernanceViolationError(
                f"action {clean!r} cannot declare affects_source=True on a "
                "read_only_source OntologySpec"
            )
        ef = self.get_edit_function(edit_function)
        if params is None:
            eff_params = ef.params
        else:
            _validate_params(params, clean)
            if params is not ef.params and not issubclass(params, ef.params):
                raise TypeError(
                    f"action {clean!r} params {params.__name__} must match or subclass "
                    f"edit_function {ef.name!r} params {ef.params.__name__}"
                )
            self._rebuild_model(params)
            eff_params = params
        self._validate_roles(allowed_roles, context=f"action {clean!r} allowed_roles")
        self._validate_roles(revert_roles, context=f"action {clean!r} revert_roles")
        eff_touched = tuple(touched_object_types) or ef.touched_object_types
        eff_desc = description or ef.description
        action_def = ActionDef(
            name=clean,
            params=eff_params,
            handler=ef.handler,
            implementation_kind="edit_function",
            edit_function=ef.name,
            allowed_roles=tuple(allowed_roles),
            revert_roles=tuple(revert_roles),
            affects_source=affects_source,
            visibility=visibility,
            description=eff_desc,
            view_tags=tuple(view_tags),
            touched_object_types=eff_touched,
            submission_criteria=tuple(submission_criteria),
        )
        self.actions[clean] = action_def
        return action_def

    def declare_rule_action(
        self,
        name: str,
        *,
        params: type[BaseModel],
        handler: Callable[..., Any],
        allowed_roles: Sequence[str] = (),
        revert_roles: Sequence[str] = (),
        description: str = "",
        affects_source: bool = True,
        visibility: Literal["public", "private"] = "public",
        view_tags: Iterable[str] = (),
        touched_object_types: Iterable[str] = (),
        submission_criteria: Sequence[Any] = (),
    ) -> ActionDef:
        """Register a non-Turing-complete declarative rule Action."""
        clean = str(name).strip()
        if not clean:
            raise ValueError("action name must not be empty")
        if self.read_only_source and affects_source:
            raise GovernanceViolationError(
                f"action {clean!r} cannot declare affects_source=True on a "
                "read_only_source OntologySpec"
            )
        _validate_params(params, clean)
        self._validate_roles(allowed_roles, context=f"action {clean!r} allowed_roles")
        self._validate_roles(revert_roles, context=f"action {clean!r} revert_roles")
        self._rebuild_model(params)
        action_def = ActionDef(
            name=clean,
            params=params,
            handler=handler,
            implementation_kind="rule",
            edit_function=None,
            allowed_roles=tuple(allowed_roles),
            revert_roles=tuple(revert_roles),
            affects_source=affects_source,
            visibility=visibility,
            description=description or (handler.__doc__ or "").strip(),
            view_tags=tuple(view_tags),
            touched_object_types=tuple(touched_object_types),
            submission_criteria=tuple(submission_criteria),
        )
        self.actions[clean] = action_def
        return action_def

    def get_action(self, name: str) -> ActionDef:
        if name not in self.actions:
            raise ActionNotFoundError(f"unknown action: {name!r}")
        return self.actions[name]

    # -- introspection -----------------------------------------------------
    def summary(self) -> dict[str, list[str]]:
        return {
            "object_types": sorted(self.object_types),
            "object_properties": sorted(self.object_properties),
            "interfaces": sorted(self.interfaces),
            "roles": sorted(self.roles),
            "views": sorted(self.views),
            "functions": sorted(self.functions),
            "edit_functions": sorted(self.edit_functions),
            "actions": sorted(self.actions),
            "automations": sorted(self.automations),
        }


def _validate_params(params: Any, name: str) -> None:
    if not (isinstance(params, type) and issubclass(params, BaseModel)):
        raise TypeError(
            f"params for {name!r} must be a pydantic BaseModel subclass, "
            f"got {params!r}"
        )
