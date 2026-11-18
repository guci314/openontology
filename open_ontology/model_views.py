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

from .spec import (
    ObjectType,
    OntologySpec,
    extract_auto_id_meta,
    extract_object_property_meta,
)

__all__ = ["describe_tbox", "describe_behavior", "describe_openapi"]

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


def _describe_field(
    field_name: str,
    info: FieldInfo,
    masked: dict[str, Any],
    cls: type[ObjectType] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": field_name,
        "type": _type_name(info.annotation),
        "property_kind": "datatype",
        "masked": field_name in masked,
        "masked_roles": list(masked.get(field_name, ())),
        "owned": False,
        "required": info.is_required(),
        "constraints": _field_constraints(info),
    }
    obj_prop = (getattr(cls, "__object_properties__", None) or {}).get(field_name) if cls else None
    if obj_prop is not None:
        row["property_kind"] = "object"
        row["range"] = obj_prop.range
        row["multiple"] = obj_prop.cardinality in ("one-to-many", "many-to-many")
    else:
        meta = extract_object_property_meta(info)
        if meta is not None:
            row["property_kind"] = "object"
            row["range"] = meta[0]
            row["multiple"] = meta[1]
    if extract_auto_id_meta(info):
        row["auto_generated"] = True
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
            row = _describe_field(field_name, info, masked, cls=cls)
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
                "append_only": cls.is_append_only(),
                "superclasses": cls.ontology_superclasses(),
                "read_only_source": (
                    cls.is_read_only_source()
                    or (
                        bool(getattr(spec, "read_only_source", False))
                        and not cls.is_fully_owned()
                    )
                ),
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

    object_properties = []
    for decl in sorted(spec.object_properties.values(), key=lambda d: d.name):
        prop_row: dict[str, Any] = {
            "name": decl.name,
            "domain": decl.domain,
            "range": decl.range,
            "cardinality": decl.cardinality,
            "owned": decl.owned,
            "field": decl.field,
            "description": decl.description,
        }
        object_properties.append(prop_row)

    return {
        "object_types": object_types,
        "interfaces": interfaces,
        "object_properties": object_properties,
    }


def _enrich_params_schema(params_cls: type[Any]) -> dict[str, Any]:
    schema = params_cls.model_json_schema()
    props = schema.get("properties")
    if isinstance(props, dict):
        fields = getattr(params_cls, "model_fields", {})
        for pname, pschema in props.items():
            if not isinstance(pschema, dict):
                continue
            info = fields.get(pname)
            if info is not None:
                if extract_auto_id_meta(info):
                    pschema["auto_generated"] = True
                obj_meta = extract_object_property_meta(info)
                if obj_meta is not None:
                    pschema["property_kind"] = "object"
                    pschema["range"] = obj_meta[0]
                    pschema["multiple"] = obj_meta[1]
            # Also hoist from anyOf branches if present
            for branch in pschema.get("anyOf", ()) or ():
                if not isinstance(branch, dict):
                    continue
                if branch.get("auto_generated"):
                    pschema["auto_generated"] = True
                if branch.get("property_kind") == "object" and branch.get("range"):
                    pschema["property_kind"] = "object"
                    pschema["range"] = branch["range"]
                    pschema["multiple"] = bool(branch.get("multiple", False))
    return schema


# ---------------------------------------------------------------------------
# Behavior: declared contracts only
# ---------------------------------------------------------------------------
def _describe_roles(spec: OntologySpec) -> list[dict[str, Any]]:
    """Role vocabulary + allowed/approver action matrix + masked field grants."""
    allowed_map: dict[str, list[str]] = {}
    approver_map: dict[str, list[str]] = {}
    masked_map: dict[str, list[str]] = {}
    seen_order: list[str] = []

    def ensure(role_name: str) -> None:
        if role_name and role_name not in seen_order:
            seen_order.append(role_name)

    for role_name in spec.roles:
        ensure(role_name)

    for action in sorted(spec.actions.values(), key=lambda d: d.name):
        for role_name in action.allowed_roles:
            ensure(role_name)
            allowed_map.setdefault(role_name, []).append(action.name)
        for role_name in action.approver_roles:
            ensure(role_name)
            approver_map.setdefault(role_name, []).append(action.name)

    for cls in sorted(spec.object_types.values(), key=lambda c: c.__name__):
        masked = getattr(cls, "__masked_fields__", {}) or {}
        for field_name in sorted(masked):
            for role_name in masked[field_name] or ():
                ensure(role_name)
                masked_map.setdefault(role_name, []).append(f"{cls.__name__}.{field_name}")

    result: list[dict[str, Any]] = []
    for role_name in seen_order:
        decl = spec.roles.get(role_name)
        result.append(
            {
                "name": role_name,
                "description": decl.description if decl is not None else "",
                "declared": decl is not None,
                "allowed_actions": allowed_map.get(role_name, []),
                "approver_actions": approver_map.get(role_name, []),
                "masked_fields": masked_map.get(role_name, []),
            }
        )
    return result


def describe_behavior(spec: OntologySpec) -> dict[str, Any]:
    """Action / function / automation / role contracts declared on the spec."""
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
                "visibility": action.visibility,
                "params": _enrich_params_schema(action.params),
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
        "roles": _describe_roles(spec),
    }


def _rewrite_openapi_refs(node: Any) -> Any:
    """Rewrite JSON Schema ``#/$defs/...`` references to OpenAPI ``#/components/schemas/...``."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/$defs/"):
                out[k] = "#/components/schemas/" + v[len("#/$defs/"):]
            else:
                out[k] = _rewrite_openapi_refs(v)
        return out
    if isinstance(node, list):
        return [_rewrite_openapi_refs(item) for item in node]
    return node


def _register_component_schema(
    components_schemas: dict[str, Any],
    name: str,
    raw_schema: dict[str, Any],
) -> None:
    safe = _json_safe(raw_schema)
    if safe is _MISSING or not isinstance(safe, dict):
        components_schemas[name] = {"type": "object"}
        return
    defs = safe.pop("$defs", None)
    if isinstance(defs, dict):
        for def_name, def_schema in defs.items():
            components_schemas.setdefault(
                def_name, _rewrite_openapi_refs(def_schema)
            )
    components_schemas[name] = _rewrite_openapi_refs(safe)


def describe_openapi(
    spec: OntologySpec,
    *,
    world_id: str,
    base_url: str = "",
    role: str | None = None,
    title: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Project an :class:`OntologySpec` into a role-filtered OpenAPI 3.1 contract."""
    prefix = f"/api/worlds/{world_id}"
    schemas: dict[str, Any] = {}

    for cls_name, cls in spec.object_types.items():
        _register_component_schema(schemas, cls_name, cls.model_json_schema())

    paths: dict[str, Any] = {
        prefix: {
            "get": {
                "operationId": "get_world",
                "tags": ["World"],
                "summary": f"Inspect world summary & fingerprint for {world_id}",
                "responses": {"200": {"description": "World summary JSON"}},
            }
        },
        f"{prefix}/links": {
            "get": {
                "operationId": "list_links",
                "tags": ["Objects"],
                "summary": "List semantic links visible to the caller Actor",
                "responses": {"200": {"description": "Visible semantic edges"}},
            }
        },
        f"{prefix}/action-runs": {
            "get": {
                "operationId": "list_action_runs",
                "tags": ["Audit"],
                "summary": "List recorded action runs",
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "Action run ledger"}},
            }
        },
    }

    if not role or role in ("admin", "system"):
        paths[f"{prefix}/webhooks"] = {
            "get": {
                "operationId": "list_webhooks",
                "tags": ["Webhooks"],
                "summary": "List outbound webhook subscriptions for this world",
                "responses": {"200": {"description": "Webhook subscription list"}},
            },
            "post": {
                "operationId": "register_webhook",
                "tags": ["Webhooks"],
                "summary": "Register an outbound webhook subscription",
                "responses": {"201": {"description": "Registered subscription"}},
            },
        }
        paths[f"{prefix}/webhooks/{{sub_id}}"] = {
            "delete": {
                "operationId": "delete_webhook",
                "tags": ["Webhooks"],
                "summary": "Unregister an outbound webhook subscription",
                "parameters": [
                    {
                        "name": "sub_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "Deleted subscription"}},
            }
        }

    for cls_name, cls in spec.object_types.items():
        pk = cls.primary_key_name()
        paths[f"{prefix}/objects/{cls_name}"] = {
            "get": {
                "operationId": f"list_{cls_name}",
                "tags": ["Objects"],
                "summary": f"List visible {cls_name} objects (RLS + column masking enforced)",
                "responses": {
                    "200": {
                        "description": f"List of {cls_name}",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean"},
                                        "object_type": {"type": "string"},
                                        "count": {"type": "integer"},
                                        "objects": {
                                            "type": "array",
                                            "items": {
                                                "$ref": f"#/components/schemas/{cls_name}"
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
        paths[f"{prefix}/objects/{cls_name}/{{{pk}}}"] = {
            "get": {
                "operationId": f"get_{cls_name}",
                "tags": ["Objects"],
                "summary": f"Fetch one {cls_name} by primary key ({pk})",
                "parameters": [
                    {
                        "name": pk,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": cls_name,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean"},
                                        "object_type": {"type": "string"},
                                        "object": {
                                            "$ref": f"#/components/schemas/{cls_name}"
                                        },
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }

    for fn in sorted(spec.functions.values(), key=lambda d: d.name):
        schema_name = f"fn_{fn.name}_Params"
        params_schema = _enrich_params_schema(fn.params)
        _register_component_schema(schemas, schema_name, params_schema)
        has_required = bool(params_schema.get("required"))
        paths[f"{prefix}/functions/{fn.name}"] = {
            "post": {
                "operationId": f"call_{fn.name}",
                "tags": ["Functions"],
                "summary": fn.description or f"Call read-only function {fn.name}",
                "requestBody": {
                    "required": has_required,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{schema_name}"}
                        }
                    },
                },
                "responses": {"200": {"description": "Function JSON result"}},
            }
        }

    for action in sorted(spec.actions.values(), key=lambda d: d.name):
        if action.visibility == "private":
            continue
        if role and action.allowed_roles and role not in action.allowed_roles:
            continue
        schema_name = f"act_{action.name}_Params"
        params_schema = _enrich_params_schema(action.params)
        _register_component_schema(schemas, schema_name, params_schema)
        has_required = bool(params_schema.get("required"))
        op: dict[str, Any] = {
            "operationId": action.name,
            "tags": ["Actions"],
            "summary": action.description or f"Execute governed action {action.name}",
            "parameters": [
                {
                    "name": "dry_run",
                    "in": "query",
                    "required": False,
                    "description": "Set true to simulate without committing.",
                    "schema": {"type": "boolean", "default": False},
                },
                {
                    "name": "run_id",
                    "in": "query",
                    "required": False,
                    "description": "Optional idempotency run identifier.",
                    "schema": {"type": "string"},
                },
            ],
            "requestBody": {
                "required": has_required,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema_name}"}
                    }
                },
            },
            "responses": {"200": {"description": "ActionRun receipt or dry-run diff"}},
            "x-allowed-roles": list(action.allowed_roles),
            "x-approver-roles": list(action.approver_roles),
        }
        paths[f"{prefix}/actions/{action.name}"] = {"post": op}

    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": title or f"OpenOntology World: {world_id}",
            "version": "1.0.0",
            "description": description
            or (
                f"Role-scoped OpenAPI 3.1 contract for world '{world_id}'. "
                "Authenticate via Authorization: Bearer <token> or X-Actor-Id + X-Actor-Role."
            ),
        },
        "servers": [{"url": base_url}] if base_url else [],
        "security": [
            {"ActorBearer": []},
            {"ActorIdHeader": [], "ActorRoleHeader": []},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ActorBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Actor token bound to (actor_id, role, clearance_tags).",
                },
                "ActorIdHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Actor-Id",
                },
                "ActorRoleHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Actor-Role",
                },
            },
            "schemas": schemas,
        },
        "x-world-id": world_id,
    }
    if role:
        doc["x-actor-role"] = role
    return doc
