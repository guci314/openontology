"""Read-only projections of an :class:`OntologySpec`.

The model layer has two reusable views, both derived from the *existing*
declarations -- never from a database, runtime instance, audit ledger or
handler implementation:

* :func:`describe_tbox` -- structural declarations: object types, fields and
  their declaratively available constraints, interfaces, link types and the
  explicit aggregate-root flag.
* :func:`describe_behavior` -- action / function / automation contracts,
  including action parameter schemas and role / approval declarations.

These projections are the single source of truth reused by the workshop
metadata endpoint.  They deliberately do **not** serialize arbitrary Python
validators or callables: only values that are available as JSON-safe
declaration data are exposed.
"""

from __future__ import annotations

import json
import types as _types
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic.fields import FieldInfo

from .spec import ObjectType, OntologySpec

__all__ = ["describe_tbox", "describe_behavior"]

_MISSING = object()

#: pydantic 2 stores declarative field constraints in ``FieldInfo.metadata``.
_CONSTRAINT_KEYS = (
    "ge",
    "le",
    "gt",
    "lt",
    "multiple_of",
    "min_length",
    "max_length",
    "pattern",
)


# ---------------------------------------------------------------------------
# small declaration helpers
# ---------------------------------------------------------------------------
def _owned_field_names(cls: type[ObjectType]) -> list[str]:
    owned = cls.owned_spec()
    if isinstance(owned, dict):
        return sorted(owned)
    if owned is True:
        return sorted(cls.model_fields)
    return []


def _type_name(annotation: Any) -> str:
    """把 Python 注解渲染成简洁的类型名（``<class 'str'>`` → ``str``）。"""
    if annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    if origin is Union or origin is _types.UnionType:
        return " | ".join(_type_name(arg) for arg in get_args(annotation))
    if origin is not None:
        name = getattr(origin, "__name__", str(origin))
        args = ", ".join(_type_name(arg) for arg in get_args(annotation))
        return f"{name}[{args}]" if args else str(name)
    name = getattr(annotation, "__name__", None)
    return str(name) if name else str(annotation).replace("typing.", "")


def _json_safe(value: Any) -> Any:
    """Return a strict plain-JSON copy of a declaration value, or ``_MISSING``."""
    if isinstance(value, Enum):
        value = value.value
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return _MISSING
    return json.loads(encoded)


def _field_constraints(info: FieldInfo) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    for meta in info.metadata:
        for key in _CONSTRAINT_KEYS:
            value = getattr(meta, key, None)
            if value is None:
                continue
            safe = _json_safe(value)
            if safe is not _MISSING:
                constraints[key] = safe
    return constraints


def _field_choices(annotation: Any) -> list[Any] | None:
    if get_origin(annotation) is Literal:
        candidates = list(get_args(annotation))
    elif isinstance(annotation, type) and issubclass(annotation, Enum):
        candidates = [member.value for member in annotation]
    else:
        return None
    choices = [_json_safe(item) for item in candidates]
    if any(item is _MISSING for item in choices):
        return None
    return choices


def _field_default(info: FieldInfo) -> Any:
    if info.is_required() or info.default_factory is not None:
        return _MISSING
    return _json_safe(info.default)


def _describe_field(field_name: str, info: FieldInfo, masked: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": field_name,
        "type": _type_name(info.annotation),
        "masked": field_name in masked,
        "masked_roles": list(masked.get(field_name, ())),
        "owned": False,
        "required": info.is_required(),
        "constraints": _field_constraints(info),
    }
    choices = _field_choices(info.annotation)
    if choices is not None:
        row["choices"] = choices
    default = _field_default(info)
    if default is not _MISSING:
        row["default"] = default
    return row


# ---------------------------------------------------------------------------
# TBox: structural declarations only
# ---------------------------------------------------------------------------
def describe_tbox(spec: OntologySpec) -> dict[str, Any]:
    """Structural, JSON-serializable view of the spec's declarations."""
    object_types: list[dict[str, Any]] = []
    for name, cls in spec.object_types.items():  # declaration order
        owned_fields = _owned_field_names(cls)
        masked = getattr(cls, "__masked_fields__", {}) or {}
        fields = []
        for field_name, info in cls.model_fields.items():
            row = _describe_field(field_name, info, masked)
            row["owned"] = field_name in owned_fields
            fields.append(row)
        object_types.append(
            {
                "name": name,
                "primary_key": cls.primary_key_name(),
                "title": getattr(cls, "__title__", None),
                "fields": fields,
                "interfaces": list(getattr(cls, "__interfaces__", ()) or ()),
                "owned": bool(cls.owned_spec()),
                "owned_fields": owned_fields,
                "security_tag_fields": list(
                    getattr(cls, "__security_tag_fields__", ()) or ()
                ),
                "aggregate_root": cls.is_aggregate_root(),
            }
        )

    interfaces = [
        {
            "name": decl.name,
            "required_fields": list(decl.required_fields),
            "description": decl.description,
            "implementations": [
                cls.__name__ for cls in spec.object_types_for_interface(decl)
            ],
        }
        for decl in sorted(spec.interfaces.values(), key=lambda d: d.name)
    ]

    link_types = [
        {
            "name": decl.name,
            "source": decl.source,
            "target": decl.target,
            "cardinality": decl.cardinality,
            "owned": decl.owned,
            "description": decl.description,
        }
        for decl in sorted(spec.link_types.values(), key=lambda d: d.name)
    ]

    return {
        "object_types": object_types,
        "interfaces": interfaces,
        "link_types": link_types,
    }


# ---------------------------------------------------------------------------
# Behavior: declared contracts only
# ---------------------------------------------------------------------------
def describe_behavior(spec: OntologySpec) -> dict[str, Any]:
    """Action / function / automation contracts declared on the spec."""
    return {
        "functions": [
            {"name": fn.name, "description": fn.description}
            for fn in sorted(spec.functions.values(), key=lambda d: d.name)
        ],
        "actions": [
            {
                "name": action.name,
                "description": action.description,
                "allowed_roles": list(action.allowed_roles),
                "approver_roles": list(action.approver_roles),
                "affects_source": action.affects_source,
                "params": action.params.model_json_schema(),
            }
            for action in sorted(spec.actions.values(), key=lambda d: d.name)
        ],
        "automations": [
            {
                "name": decl.name,
                "watch_object_type": decl.watch_object_type,
                "description": decl.description,
            }
            for decl in sorted(spec.automations.values(), key=lambda d: d.name)
        ],
    }
