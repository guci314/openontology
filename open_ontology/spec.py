"""L3 declarative ontology primitives.

This module contains the *declaration* layer only:

* :class:`Actor`          -- caller identity + row level security predicate
* :class:`ObjectType`     -- pydantic base class for domain entities
* :class:`LinkType`       -- typed directed edge declaration
* :class:`FunctionDef`    -- read-only computation
* :class:`ActionDef`      -- governed kinetic action
* :class:`OntologySpec`   -- registry / decorator DSL

No SQL and no mutation lives here.
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Actor",
    "ObjectType",
    "LinkType",
    "InterfaceType",
    "FunctionDef",
    "ActionDef",
    "AutomationDef",
    "OntologySpec",
    "OpenOntologyError",
    "OntologyError",
    "PermissionDeniedError",
    "ObjectNotFoundError",
    "UnknownObjectTypeError",
    "UnknownLinkTypeError",
    "ActionNotFoundError",
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


class UnknownLinkTypeError(OpenOntologyError):
    """Raised when a link name is not registered in the spec."""


class ActionNotFoundError(OpenOntologyError):
    """Raised when an action is not registered in the spec."""


class FunctionNotFoundError(OpenOntologyError):
    """Raised when a function is not registered in the spec."""


class AutomationNotFoundError(OpenOntologyError):
    """Raised when an automation is not registered in the spec."""


class InvariantError(OpenOntologyError):
    """Raised by action handlers when a business invariant is violated."""

    def __init__(self, message: str, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


class ConcurrencyConflictError(OpenOntologyError):
    """Raised when optimistic locking detects a stale write."""


class GovernanceViolationError(OpenOntologyError):
    """Raised when the physical write gate or authority boundary rejects a write."""


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


class LinkType(BaseModel):
    """Typed directed edge declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source: str
    target: str
    cardinality: Literal[
        "one-to-one", "one-to-many", "many-to-one", "many-to-many"
    ] = "many-to-many"
    owned: bool = False
    description: str = ""


class InterfaceType(BaseModel):
    """跨对象类型的多态接口声明（Palantir OSv2 Interface）。

    实现该接口的 ``ObjectType`` 必须在 ``__interfaces__`` 中列出接口名，
    并至少声明 ``required_fields`` 里的全部字段。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required_fields: tuple[str, ...] = ()
    description: str = ""


# ---------------------------------------------------------------------------
# Function / action declarations
# ---------------------------------------------------------------------------
class FunctionDef(BaseModel):
    """Read-only computation registered on the spec."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    params: Any  # type[BaseModel]
    handler: Any  # Callable[[EditBatch, BaseModel], Any]
    description: str = ""
    read_only: bool = True


class ActionDef(BaseModel):
    """Governed kinetic action registered on the spec."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    params: Any  # type[BaseModel]
    handler: Any  # Callable[[EditBatch, BaseModel], None]
    allowed_roles: tuple[str, ...] = ()
    approver_roles: tuple[str, ...] = ()
    approval_rule: Any = None  # Callable[[BaseModel, Actor], bool]
    affects_source: bool = True
    post_approval: Any = None  # optional Callable[[EditBatch, BaseModel], None]
    description: str = ""

    def requires_approval(self, params: BaseModel, actor: Actor) -> bool:
        if self.approval_rule is None:
            return False
        return bool(self.approval_rule(params, actor))


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


# ---------------------------------------------------------------------------
# Spec registry
# ---------------------------------------------------------------------------
class OntologySpec:
    """Declarative registry for object types, link types, functions, actions."""

    def __init__(self) -> None:
        self.object_types: dict[str, type[ObjectType]] = {}
        self.link_types: dict[str, LinkType] = {}
        self.interfaces: dict[str, InterfaceType] = {}
        self.functions: dict[str, FunctionDef] = {}
        self.actions: dict[str, ActionDef] = {}
        self.automations: dict[str, AutomationDef] = {}
        #: 本蓝图自己的展示词条（中文名等）。展示层会把这些词条动态合并，
        #: 因此新增一个业务本体不需要修改任何内核文件。
        #: 结构：``{"objects": {...}, "links": {...}, "fields": {...}, ...}``
        self.display: dict[str, dict[str, str]] = {}

    # -- object types ------------------------------------------------------
    def object_type(self, cls: type[ObjectType]) -> type[ObjectType]:
        if not (isinstance(cls, type) and issubclass(cls, ObjectType)):
            raise TypeError("object_type() expects an ObjectType subclass")
        self._validate_interfaces(cls)
        self.object_types[cls.__name__] = cls
        return cls

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
            if cls_or_name not in self.object_types:
                raise UnknownObjectTypeError(f"unknown object type: {cls_or_name!r}")
            return self.object_types[cls_or_name]
        if not issubclass(cls_or_name, ObjectType):
            raise UnknownObjectTypeError(f"not an ObjectType subclass: {cls_or_name!r}")
        name = cls_or_name.__name__
        if name not in self.object_types:
            raise UnknownObjectTypeError(f"object type not registered: {name!r}")
        return self.object_types[name]

    # -- link types --------------------------------------------------------
    def add_link_type(
        self,
        link: LinkType | str,
        source: str | None = None,
        target: str | None = None,
        **kwargs: Any,
    ) -> LinkType:
        if isinstance(link, LinkType):
            decl = link
        else:
            decl = LinkType(name=link, source=source, target=target, **kwargs)
        self.link_types[decl.name] = decl
        return decl

    def link_type(self, name: str, source: str, target: str, **kwargs: Any) -> LinkType:
        return self.add_link_type(name, source, target, **kwargs)

    def get_link_type(self, link: str | LinkType) -> LinkType:
        name = link.name if isinstance(link, LinkType) else str(link)
        if name not in self.link_types:
            raise UnknownLinkTypeError(f"unknown link type: {name!r}")
        return self.link_types[name]

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

    def object_types_for_interface(self, interface: str | InterfaceType) -> list[type[ObjectType]]:
        name = self.get_interface(interface).name
        return [
            cls
            for cls in self.object_types.values()
            if name in (getattr(cls, "__interfaces__", ()) or ())
        ]

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

    # -- functions ---------------------------------------------------------
    def function(
        self,
        name: str,
        params: type[BaseModel],
        description: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        _validate_params(params, name)

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.functions[name] = FunctionDef(
                name=name,
                params=params,
                handler=handler,
                description=description or (handler.__doc__ or "").strip(),
            )
            return handler

        return decorator

    def get_function(self, name: str) -> FunctionDef:
        if name not in self.functions:
            raise FunctionNotFoundError(f"unknown function: {name!r}")
        return self.functions[name]

    # -- actions -----------------------------------------------------------
    def action(
        self,
        name: str,
        params: type[BaseModel],
        allowed_roles: Sequence[str] = (),
        approver_roles: Sequence[str] = (),
        approval_rule: Callable[[Any, Actor], bool] | None = None,
        description: str = "",
        affects_source: bool = True,
        post_approval: Callable[[Any, Any], None] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        _validate_params(params, name)

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.actions[name] = ActionDef(
                name=name,
                params=params,
                handler=handler,
                allowed_roles=tuple(allowed_roles),
                approver_roles=tuple(approver_roles),
                approval_rule=approval_rule,
                affects_source=affects_source,
                post_approval=post_approval,
                description=description or (handler.__doc__ or "").strip(),
            )
            return handler

        return decorator

    def get_action(self, name: str) -> ActionDef:
        if name not in self.actions:
            raise ActionNotFoundError(f"unknown action: {name!r}")
        return self.actions[name]

    # -- introspection -----------------------------------------------------
    def summary(self) -> dict[str, list[str]]:
        return {
            "object_types": sorted(self.object_types),
            "link_types": sorted(self.link_types),
            "interfaces": sorted(self.interfaces),
            "functions": sorted(self.functions),
            "actions": sorted(self.actions),
            "automations": sorted(self.automations),
        }


def _validate_params(params: Any, name: str) -> None:
    if not (isinstance(params, type) and issubclass(params, BaseModel)):
        raise TypeError(
            f"params for {name!r} must be a pydantic BaseModel subclass, "
            f"got {params!r}"
        )
