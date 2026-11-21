"""L1 零 eval/exec 声明式本体蓝图、补丁与安全谓词编译器（纯 M2，领域无关）。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections import defaultdict
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from ..sandbox import EditBatch, InvariantView, ObjectDiff
from ..spec import (
    Actor,
    AutoID,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
    ScopedViewSpec,
)
from .phenomenon import ActionExecutionBinding, AdapterMappingSpec, EntityMappingSpec
from .seed import SeedOntology

__all__ = [
    "FieldSpec",
    "RelationSpec",
    "ObjectTypeBlueprint",
    "FieldMigrationSpec",
    "InvariantRuleSpec",
    "ActionMutationSpec",
    "ActionBlueprint",
    "ScopedViewBlueprint",
    "DeclarativeBlueprint",
    "EvalActorSpec",
    "EvalExpectationSpec",
    "EvalSetupObjectSpec",
    "ProposedEvalCase",
    "EvolutionPatch",
    "CustomerOverlaySpec",
    "blueprint_fingerprint",
    "count_ast_nodes",
    "expr_depth",
    "validate_predicate_ast",
    "eval_predicate",
    "compose_blueprints",
    "apply_overlay",
    "apply_evolution_patch",
    "apply_adapter_mapping_patch",
    "migrate_m0_snapshot",
    "compile_blueprint",
    "dsl_schema_prompt_guide",
]

_SCALAR_TYPES: dict[str, type[Any]] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "bool": bool,
    "boolean": bool,
}

COMPARISON_OPS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in", "not_in"})
LOGICAL_OPS = frozenset({"and", "or", "not"})
QUANTIFIER_OPS = frozenset({"exists", "not_exists", "count_eq", "count_ge", "count_le"})
ARITHMETIC_OPS = frozenset({"add", "sub", "mul", "div"})
AGGREGATE_OPS = frozenset({"sum", "min", "max"})
ALLOWED_EXPR_OPS = COMPARISON_OPS | LOGICAL_OPS | QUANTIFIER_OPS | ARITHMETIC_OPS | AGGREGATE_OPS

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_REF_ROOTS = frozenset({"obj", "source_obj", "param", "actor", "before", "after", "target"})


def _assert_safe_identifier(name: str, label: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name) or name.startswith("__"):
        raise InvariantError(
            f"unsafe or invalid {label}: {name!r}",
            code="INVALID_IDENTIFIER",
        )
    return name


def _resolve_scalar_type(type_name: str) -> type[Any]:
    py_type = _SCALAR_TYPES.get(str(type_name).lower())
    if py_type is None:
        raise InvariantError(
            f"unsupported scalar type {type_name!r}; allowed: {sorted(_SCALAR_TYPES)}",
            code="UNSUPPORTED_FIELD_TYPE",
        )
    return py_type


def _default_for_type(py_type: type[Any]) -> Any:
    if py_type is int:
        return 0
    if py_type is float:
        return 0.0
    if py_type is bool:
        return False
    return ""


def _is_real_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


# ---------------------------------------------------------------------------
# 1. L1 Zero-Exec Declarative Blueprint & Patch Schemas
# ---------------------------------------------------------------------------
class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    field_type: Literal["str", "int", "float", "bool"] = "str"
    default: Any = None
    required: bool = False
    primary_key: bool = False
    auto_id: bool = False
    owned: bool = False
    masked_roles: tuple[str, ...] = ()
    non_negative: bool = False
    description: str = ""


class RelationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    target_type: str
    multiple: bool = False
    owned: bool = False
    required: bool = False
    description: str = ""


_DEFAULT_STANDALONE_ARCHETYPES: frozenset[str] = frozenset({"ObjectType"})


class ObjectTypeBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    archetype: str = "ObjectType"
    primary_key: str = "id"
    title_field: str | None = None
    aggregate_root: bool | None = None
    append_only: bool | None = None
    owned: bool | dict[str, Any] = False
    fields: list[FieldSpec] = Field(default_factory=list)
    relations: list[RelationSpec] = Field(default_factory=list)
    status_field: str = "status"
    initial_statuses: list[str] | None = None
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    voucher_delta_rules: dict[str, tuple[str, str, str]] = Field(default_factory=dict)
    view_tags: list[str] = Field(default_factory=list)
    display_label: str = ""


class FieldMigrationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: str
    op: Literal["add_default", "rename_field", "cast_type", "drop_field"] = "add_default"
    field: str
    new_field: str | None = None
    default: Any = None
    target_type: Literal["str", "int", "float", "bool"] | None = None


class InvariantRuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    code: str
    target_type: str
    id_param: str | None = None
    when: dict[str, Any] | None = None
    must_hold: dict[str, Any]
    description: str = ""

    @field_validator("when", mode="before")
    @classmethod
    def _empty_when_to_none(cls, v: Any) -> Any:
        return None if v == {} else v


class ActionMutationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create", "modify", "delete"] = "modify"
    object_type: str
    id_param: str = "id"
    target_ref: str | None = None
    set_fields: dict[str, Any] = Field(default_factory=dict)


class ActionBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    allowed_roles: list[str] = Field(default_factory=lambda: ["admin", "ai_agent"])
    revert_roles: list[str] = Field(default_factory=list)
    affects_source: bool = True
    params: list[FieldSpec] = Field(default_factory=list)
    preconditions: list[InvariantRuleSpec] = Field(default_factory=list)
    mutations: list[ActionMutationSpec] = Field(default_factory=list)
    view_tags: list[str] = Field(default_factory=list)
    display_label: str = ""


class ScopedViewBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)
    view_tags: list[str] = Field(default_factory=list)
    object_types: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    include_reachable_objects: bool = True


class DeclarativeBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    version: str = "1.0.0"
    seed_ontology_id: str | None = None
    imports: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    description: str = ""
    object_types: list[ObjectTypeBlueprint] = Field(default_factory=list)
    actions: list[ActionBlueprint] = Field(default_factory=list)
    invariants: list[InvariantRuleSpec] = Field(default_factory=list)
    migrations: list[FieldMigrationSpec] = Field(default_factory=list)
    views: list[ScopedViewBlueprint] = Field(default_factory=list)
    display: dict[str, dict[str, str]] = Field(default_factory=dict)


class EvalActorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = "eval:actor"
    role: str = "ai_agent"
    clearance_tags: list[str] = Field(default_factory=lambda: ["*"])


class EvalExpectationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    error_code: str | None = None
    error_contains: str | None = None
    no_changes: bool | None = None
    object_changes: list[dict[str, Any]] = Field(default_factory=list)
    link_changes: list[dict[str, Any]] = Field(default_factory=list)


class EvalSetupObjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: str
    data: dict[str, Any]


class ProposedEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    actor: EvalActorSpec = Field(default_factory=EvalActorSpec)
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    setup_objects: list[EvalSetupObjectSpec] = Field(default_factory=list)
    expect: EvalExpectationSpec

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_flat_expectation(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "expect" not in data
            and any(k in data for k in ("expect_valid", "expect_error_code"))
        ):
            copied = dict(data)
            expect: dict[str, Any] = {}
            if "expect_valid" in copied:
                expect["valid"] = copied.pop("expect_valid")
            if "expect_error_code" in copied:
                expect["error_code"] = copied.pop("expect_error_code")
            copied["expect"] = expect
            return copied
        return data


class EvolutionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch_id: str = Field(default_factory=lambda: f"patch-{uuid.uuid4().hex[:8]}")
    base_version: str
    target_version: str
    rationale: str = ""
    add_object_types: list[ObjectTypeBlueprint] = Field(default_factory=list)
    add_fields: dict[str, list[FieldSpec]] = Field(default_factory=dict)
    remove_fields: dict[str, list[str]] = Field(default_factory=dict)
    add_relations: dict[str, list[RelationSpec]] = Field(default_factory=dict)
    update_transitions: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    add_actions: list[ActionBlueprint] = Field(default_factory=list)
    update_actions: list[ActionBlueprint] = Field(default_factory=list)
    add_invariants: list[InvariantRuleSpec] = Field(default_factory=list)
    remove_invariants: list[str] = Field(default_factory=list)
    update_revert_roles: dict[str, list[str]] = Field(default_factory=dict)
    add_views: list[ScopedViewBlueprint] = Field(default_factory=list)
    update_views: list[ScopedViewBlueprint] = Field(default_factory=list)
    migrations: list[FieldMigrationSpec] = Field(default_factory=list)
    proposed_eval_cases: list[ProposedEvalCase] = Field(default_factory=list)
    update_entity_mappings: list[EntityMappingSpec] = Field(default_factory=list)
    add_action_bindings: list[ActionExecutionBinding] = Field(default_factory=list)
    update_action_bindings: list[ActionExecutionBinding] = Field(default_factory=list)


class CustomerOverlaySpec(BaseModel):
    """Additive-and-monotonic overlay specification layered atop standard ontologies."""

    model_config = ConfigDict(extra="forbid")

    overlay_id: str = Field(default_factory=lambda: f"overlay-{uuid.uuid4().hex[:8]}")
    base_model_id: str | None = None
    target_version: str | None = None
    description: str = ""
    add_roles: list[str] = Field(default_factory=list)
    add_object_types: list[ObjectTypeBlueprint] = Field(default_factory=list)
    add_fields: dict[str, list[FieldSpec]] = Field(default_factory=dict)
    add_relations: dict[str, list[RelationSpec]] = Field(default_factory=dict)
    update_transitions: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    add_actions: list[ActionBlueprint] = Field(default_factory=list)
    update_actions: list[ActionBlueprint] = Field(default_factory=list)
    add_action_preconditions: dict[str, list[InvariantRuleSpec]] = Field(default_factory=dict)
    restrict_allowed_roles: dict[str, list[str]] = Field(default_factory=dict)
    update_revert_roles: dict[str, list[str]] = Field(default_factory=dict)
    add_invariants: list[InvariantRuleSpec] = Field(default_factory=list)
    add_views: list[ScopedViewBlueprint] = Field(default_factory=list)
    update_views: list[ScopedViewBlueprint] = Field(default_factory=list)
    migrations: list[FieldMigrationSpec] = Field(default_factory=list)
    display: dict[str, dict[str, str]] = Field(default_factory=dict)

    # Explicit trap fields so destructive overrides fail with OVERLAY_BREAKS_STANDARD_CONTRACT
    remove_object_types: list[str] = Field(default_factory=list)
    remove_fields: dict[str, list[str]] = Field(default_factory=dict)
    alter_fields: dict[str, list[FieldSpec]] = Field(default_factory=dict)
    update_primary_keys: dict[str, str] = Field(default_factory=dict)
    remove_invariants: list[str] = Field(default_factory=list)
    remove_action_preconditions: dict[str, list[str]] = Field(default_factory=dict)
    replace_transitions: dict[str, dict[str, list[str]]] = Field(default_factory=dict)


def blueprint_fingerprint(blueprint: DeclarativeBlueprint | str) -> str:
    """Compute a deterministic SHA-256 hash binding a blueprint to its Crucible report."""
    if isinstance(blueprint, str):
        parsed = json.loads(blueprint)
    else:
        parsed = blueprint.model_dump(mode="json")
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 2. Safe JSON Predicate / Value AST Validator & Interpreter (Zero exec/eval)
# ---------------------------------------------------------------------------
def expr_depth(node: Any) -> int:
    if isinstance(node, dict):
        children = [v for k, v in node.items() if k != "lit"]
        if not children:
            return 1
        return 1 + max(expr_depth(v) for v in children)
    if isinstance(node, list):
        return 1 + (max((expr_depth(x) for x in node), default=0))
    return 1


def count_ast_nodes(node: Any) -> int:
    if isinstance(node, dict):
        return 1 + sum(count_ast_nodes(v) for k, v in node.items() if k != "lit")
    if isinstance(node, list):
        return sum(count_ast_nodes(x) for x in node)
    return 0


def validate_predicate_ast(
    expr: Any,
    *,
    max_depth: int = 12,
    allowed_roots: frozenset[str] | None = None,
    allow_view: bool = True,
) -> int:
    """Strictly validate a declarative predicate/value JSON AST. Returns its depth."""
    if not isinstance(expr, dict):
        raise InvariantError(
            f"predicate AST node must be a dict, got {type(expr).__name__}",
            code="INVALID_PREDICATE_AST",
        )
    depth = expr_depth(expr)
    if depth > max_depth:
        raise InvariantError(
            f"predicate AST depth {depth} exceeds max_depth={max_depth}",
            code="PREDICATE_TOO_DEEP",
        )
    _validate_expr_node(expr, expect_bool=True, allowed_roots=allowed_roots, allow_view=allow_view)
    return depth


def _iter_ast_refs(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        if set(node.keys()) == {"lit"}:
            return refs
        if set(node.keys()) == {"ref"} and isinstance(node["ref"], str) and node["ref"]:
            refs.append(node["ref"])
            return refs
        for k, v in node.items():
            if k != "lit":
                refs.extend(_iter_ast_refs(v))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_iter_ast_refs(item))
    return refs


def _validate_value_node(
    node: Any,
    *,
    allowed_roots: frozenset[str] | None = None,
    allow_view: bool = True,
) -> None:
    if isinstance(node, (str, int, float, bool)) or node is None:
        return
    if isinstance(node, list):
        for item in node:
            if not isinstance(item, (str, int, float, bool)) and item is not None:
                raise InvariantError(
                    "list literal inside predicate may only contain scalars",
                    code="INVALID_PREDICATE_AST",
                )
        return
    if not isinstance(node, dict):
        raise InvariantError(
            f"unsupported value AST node {type(node).__name__}",
            code="INVALID_PREDICATE_AST",
        )
    if set(node.keys()) == {"lit"}:
        _validate_value_node(node["lit"], allowed_roots=allowed_roots, allow_view=allow_view)
        return
    if set(node.keys()) == {"ref"}:
        ref = node["ref"]
        if not isinstance(ref, str) or not ref:
            raise InvariantError("ref must be a non-empty string", code="INVALID_PREDICATE_AST")
        parts = ref.split(".")
        if parts[0] not in _VALID_REF_ROOTS:
            raise InvariantError(
                f"unsupported ref root {parts[0]!r}",
                code="INVALID_PREDICATE_AST",
            )
        if allowed_roots is not None and parts[0] not in allowed_roots:
            raise InvariantError(
                f"ref root {parts[0]!r} is unavailable in this context (allowed: {sorted(allowed_roots)})",
                code="UNAVAILABLE_REF_IN_CONTEXT",
            )
        for seg in parts[1:]:
            _assert_safe_identifier(seg, "ref path segment")
        if len(parts) > 2 and not allow_view:
            raise InvariantError(
                f"relation traversal {ref!r} requires a sandbox view",
                code="MISSING_VIEW_IN_CONTEXT",
            )
        return
    op = node.get("op")
    if op in ARITHMETIC_OPS:
        if "left" not in node or "right" not in node:
            raise InvariantError(
                f"arithmetic op {op!r} requires 'left' and 'right'",
                code="INVALID_PREDICATE_AST",
            )
        _validate_value_node(node["left"], allowed_roots=allowed_roots, allow_view=allow_view)
        _validate_value_node(node["right"], allowed_roots=allowed_roots, allow_view=allow_view)
        return
    if op in AGGREGATE_OPS:
        if not allow_view:
            raise InvariantError(
                f"aggregate {op!r} requires a sandbox view",
                code="MISSING_VIEW_IN_CONTEXT",
            )
        obj_type = node.get("object_type")
        _assert_safe_identifier(str(obj_type or ""), "aggregate object_type")
        field_spec = node.get("field")
        sub_allowed = (allowed_roots | {"target"}) if allowed_roots is not None else None
        if isinstance(field_spec, str):
            _assert_safe_identifier(field_spec, "aggregate field")
        elif isinstance(field_spec, dict):
            _validate_value_node(field_spec, allowed_roots=sub_allowed, allow_view=True)
        else:
            raise InvariantError(
                f"aggregate {op!r} requires string or value-AST 'field'",
                code="INVALID_PREDICATE_AST",
            )
        if node.get("where"):
            _validate_expr_node(
                node["where"],
                expect_bool=True,
                allowed_roots=sub_allowed,
                allow_view=True,
            )
        if "default" in node and node["default"] is not None and not _is_real_number(node["default"]):
            raise InvariantError(
                f"aggregate {op!r} 'default' must be numeric or None",
                code="INVALID_PREDICATE_AST",
            )
        return
    if op == "if_else" and set(node.keys()) == {"op", "cond", "then", "else"}:
        _validate_expr_node(node["cond"], expect_bool=True, allowed_roots=allowed_roots, allow_view=allow_view)
        _validate_value_node(node["then"], allowed_roots=allowed_roots, allow_view=allow_view)
        _validate_value_node(node["else"], allowed_roots=allowed_roots, allow_view=allow_view)
        return
    raise InvariantError(
        f"disallowed value AST keys {sorted(node.keys())}",
        code="UNSAFE_EXPRESSION",
    )


def _validate_expr_node(
    node: Any,
    *,
    expect_bool: bool = True,
    allowed_roots: frozenset[str] | None = None,
    allow_view: bool = True,
) -> None:
    if not isinstance(node, dict):
        raise InvariantError("predicate node must be a JSON object", code="INVALID_PREDICATE_AST")
    op = node.get("op")
    if op not in ALLOWED_EXPR_OPS:
        snippet = json.dumps(node, ensure_ascii=False)[:180]
        raise InvariantError(
            f"disallowed predicate operator {op!r} in {snippet} "
            f"(boolean AST requires 'op' in {sorted(ALLOWED_EXPR_OPS)})",
            code="UNSAFE_EXPRESSION",
        )
    if op in ("and", "or"):
        args = node.get("args")
        if not isinstance(args, list) or not args:
            raise InvariantError(f"{op!r} requires non-empty 'args' list", code="INVALID_PREDICATE_AST")
        for sub in args:
            _validate_expr_node(sub, expect_bool=True, allowed_roots=allowed_roots, allow_view=allow_view)
        return
    if op == "not":
        if "arg" not in node:
            raise InvariantError("'not' requires 'arg'", code="INVALID_PREDICATE_AST")
        _validate_expr_node(node["arg"], expect_bool=True, allowed_roots=allowed_roots, allow_view=allow_view)
        return
    if op in COMPARISON_OPS:
        if "left" not in node or "right" not in node:
            raise InvariantError(
                f"comparison {op!r} requires 'left' and 'right'",
                code="INVALID_PREDICATE_AST",
            )
        _validate_value_node(node["left"], allowed_roots=allowed_roots, allow_view=allow_view)
        _validate_value_node(node["right"], allowed_roots=allowed_roots, allow_view=allow_view)
        return
    if op in QUANTIFIER_OPS:
        if not allow_view:
            raise InvariantError(
                f"quantifier {op!r} requires a sandbox view",
                code="MISSING_VIEW_IN_CONTEXT",
            )
        obj_type = node.get("object_type")
        _assert_safe_identifier(str(obj_type or ""), "quantifier object_type")
        sub_allowed = (allowed_roots | {"target"}) if allowed_roots is not None else None
        if node.get("where"):
            _validate_expr_node(
                node["where"],
                expect_bool=True,
                allowed_roots=sub_allowed,
                allow_view=True,
            )
        if op.startswith("count_"):
            val = node.get("value")
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise InvariantError(
                    f"{op!r} requires non-negative int 'value'",
                    code="INVALID_PREDICATE_AST",
                )
        return
    if expect_bool:
        raise InvariantError(f"non-boolean op {op!r} in predicate position", code="INVALID_PREDICATE_AST")


def _resolve_ref(
    ref: str,
    context: dict[str, Any],
    view: EditBatch | InvariantView | None = None,
) -> Any:
    parts = ref.split(".")
    root_name = parts[0]
    if root_name not in context:
        raise InvariantError(
            f"ref root {root_name!r} is missing from evaluation context",
            code="UNAVAILABLE_REF_IN_CONTEXT",
        )
    current = context[root_name]
    tail = parts[1:]
    for idx, seg in enumerate(tail):
        if current is None:
            return None
        if isinstance(current, ObjectType):
            prop_map = getattr(type(current), "__object_properties__", None) or {}
            has_more = idx < len(tail) - 1
            if seg in prop_map and has_more:
                if view is None:
                    raise InvariantError(
                        f"resolving relation path {ref!r} requires a sandbox view",
                        code="MISSING_SANDBOX_VIEW",
                    )
                decl = prop_map[seg]
                raw_val = getattr(current, seg, None)
                if raw_val is None or isinstance(raw_val, (list, tuple)):
                    current = raw_val
                    continue
                try:
                    target_cls = view.spec.get_object_type(decl.range)
                    current = view.get(target_cls, str(raw_val))
                except Exception:
                    current = raw_val
                continue
            current = getattr(current, seg, None)
        elif isinstance(current, (Actor, BaseModel)):
            current = getattr(current, seg, None)
        elif isinstance(current, dict):
            current = current.get(seg)
        else:
            current = getattr(current, seg, None)
    return current


def _eval_value(
    node: Any,
    context: dict[str, Any],
    view: EditBatch | InvariantView | None = None,
) -> Any:
    if isinstance(node, (str, int, float, bool)) or node is None or isinstance(node, list):
        return node
    if isinstance(node, dict):
        if "lit" in node and len(node) == 1:
            return node["lit"]
        if "ref" in node and len(node) == 1:
            return _resolve_ref(node["ref"], context, view)
        op = node.get("op")
        if op in ARITHMETIC_OPS:
            lv = _eval_value(node["left"], context, view)
            rv = _eval_value(node["right"], context, view)
            if lv is None or rv is None:
                return None
            # P1-3 fix: strictly require numeric operands to prevent string*int memory DoS
            if not (_is_real_number(lv) and _is_real_number(rv)):
                raise InvariantError(
                    f"arithmetic op {op!r} requires numeric operands, got "
                    f"{type(lv).__name__} and {type(rv).__name__}",
                    code="INVALID_ARITHMETIC_OPERAND",
                )
            if op == "add":
                return lv + rv
            if op == "sub":
                return lv - rv
            if op == "mul":
                return lv * rv
            if op == "div":
                if rv == 0:
                    raise InvariantError(
                        "division by zero in declarative expression",
                        code="DIVISION_BY_ZERO",
                    )
                return lv / rv
        if op in AGGREGATE_OPS:
            if view is None:
                raise InvariantError(
                    f"aggregate {op!r} requires a sandbox view",
                    code="MISSING_SANDBOX_VIEW",
                )
            target_cls = view.spec.get_object_type(node["object_type"])
            where = node.get("where")
            field_spec = node["field"]
            vals: list[int | float] = []
            for cand in view.query(target_cls):
                sub_ctx = dict(context)
                sub_ctx["target"] = cand
                if where and not eval_predicate(where, sub_ctx, view):
                    continue
                v = (
                    getattr(cand, field_spec, None)
                    if isinstance(field_spec, str)
                    else _eval_value(field_spec, sub_ctx, view)
                )
                if v is None:
                    continue
                if not _is_real_number(v):
                    raise InvariantError(
                        f"aggregate {op!r} requires numeric values, got {type(v).__name__}",
                        code="INVALID_ARITHMETIC_OPERAND",
                    )
                vals.append(v)
            if not vals:
                if "default" in node:
                    return node["default"]
                return 0 if op == "sum" else None
            if op == "sum":
                return sum(vals)
            if op == "min":
                return min(vals)
            return max(vals)
        if op == "if_else" and set(node.keys()) == {"op", "cond", "then", "else"}:
            if eval_predicate(node["cond"], context, view):
                return _eval_value(node["then"], context, view)
            return _eval_value(node["else"], context, view)
    raise InvariantError(f"cannot evaluate value AST node: {node!r}", code="INVALID_PREDICATE_AST")


def eval_predicate(
    expr: dict[str, Any],
    context: dict[str, Any],
    view: EditBatch | InvariantView | None = None,
) -> bool:
    """Safely evaluate a validated JSON predicate AST with zero eval/exec."""
    op = expr["op"]
    if op == "and":
        return all(eval_predicate(sub, context, view) for sub in expr["args"])
    if op == "or":
        return any(eval_predicate(sub, context, view) for sub in expr["args"])
    if op == "not":
        return not eval_predicate(expr["arg"], context, view)
    if op in COMPARISON_OPS:
        lv = _eval_value(expr["left"], context, view)
        rv = _eval_value(expr["right"], context, view)
        if op == "eq":
            return lv == rv
        if op == "ne":
            return lv != rv
        if op == "in":
            return lv in (rv or ())
        if op == "not_in":
            return lv not in (rv or ())
        if lv is None or rv is None:
            return False
        if op == "lt":
            return bool(lv < rv)
        if op == "le":
            return bool(lv <= rv)
        if op == "gt":
            return bool(lv > rv)
        if op == "ge":
            return bool(lv >= rv)
    if op in QUANTIFIER_OPS:
        if view is None:
            raise InvariantError(
                f"quantifier {op!r} requires a sandbox view",
                code="MISSING_SANDBOX_VIEW",
            )
        target_cls = view.spec.get_object_type(expr["object_type"])
        candidates = view.query(target_cls)
        where = expr.get("where")
        matched = 0
        for cand in candidates:
            sub_ctx = dict(context)
            sub_ctx["target"] = cand
            if not where or eval_predicate(where, sub_ctx, view):
                matched += 1
        if op == "exists":
            return matched > 0
        if op == "not_exists":
            return matched == 0
        expected = int(expr["value"])
        if op == "count_eq":
            return matched == expected
        if op == "count_ge":
            return matched >= expected
        if op == "count_le":
            return matched <= expected
    raise InvariantError(f"unsupported predicate op {op!r}", code="UNSAFE_EXPRESSION")


# ---------------------------------------------------------------------------
# 3. Blueprint Patching, M0 Migration & Zero-Exec Spec Compiler
# ---------------------------------------------------------------------------
def _ref_hits_removed_field(
    ref: str,
    *,
    removed_by_type: dict[str, set[str]],
    ot_by_name: dict[str, ObjectTypeBlueprint],
    root_types: dict[str, str],
) -> tuple[str, str] | None:
    parts = ref.split(".")
    if len(parts) < 2:
        return None
    cur_type = root_types.get(parts[0])
    for idx, seg in enumerate(parts[1:]):
        if cur_type is None:
            return None
        if seg in removed_by_type.get(cur_type, ()):
            return cur_type, seg
        ot = ot_by_name.get(cur_type)
        if ot is None or idx == len(parts) - 2:
            return None
        rel = next((r for r in ot.relations if r.field == seg), None)
        cur_type = rel.target_type if rel is not None else None
    return None


def _expr_removed_field_hit(
    node: Any,
    *,
    removed_by_type: dict[str, set[str]],
    ot_by_name: dict[str, ObjectTypeBlueprint],
    root_types: dict[str, str],
) -> tuple[str, str] | None:
    if isinstance(node, dict):
        if set(node.keys()) == {"lit"}:
            return None
        if set(node.keys()) == {"ref"} and isinstance(node["ref"], str):
            return _ref_hits_removed_field(
                node["ref"],
                removed_by_type=removed_by_type,
                ot_by_name=ot_by_name,
                root_types=root_types,
            )
        op = node.get("op")
        if op in QUANTIFIER_OPS or op in AGGREGATE_OPS:
            sub_type = str(node.get("object_type") or "")
            if op in AGGREGATE_OPS and isinstance(node.get("field"), str):
                if node["field"] in removed_by_type.get(sub_type, ()):
                    return sub_type, node["field"]
            sub_roots = {**root_types, "target": sub_type}
            for k, v in node.items():
                if k in ("where", "field") and isinstance(v, dict):
                    hit = _expr_removed_field_hit(
                        v,
                        removed_by_type=removed_by_type,
                        ot_by_name=ot_by_name,
                        root_types=sub_roots,
                    )
                    if hit is not None:
                        return hit
            return None
        for k, v in node.items():
            if k != "lit":
                hit = _expr_removed_field_hit(
                    v,
                    removed_by_type=removed_by_type,
                    ot_by_name=ot_by_name,
                    root_types=root_types,
                )
                if hit is not None:
                    return hit
    elif isinstance(node, list):
        for item in node:
            hit = _expr_removed_field_hit(
                item,
                removed_by_type=removed_by_type,
                ot_by_name=ot_by_name,
                root_types=root_types,
            )
            if hit is not None:
                return hit
    return None


def _resolve_blueprint_action_subject(act: ActionBlueprint) -> str | None:
    for mut in act.mutations:
        if mut.op != "create" and mut.target_ref is None:
            return mut.object_type
    for pre in act.preconditions:
        return pre.target_type
    return None


def _coerce_blueprint(bp: DeclarativeBlueprint | Mapping[str, Any]) -> DeclarativeBlueprint:
    if isinstance(bp, DeclarativeBlueprint):
        return copy.deepcopy(bp)
    if isinstance(bp, Mapping):
        return DeclarativeBlueprint.model_validate(bp)
    raise InvariantError(
        f"expected DeclarativeBlueprint or mapping, got {type(bp).__name__}",
        code="BLUEPRINT_COMPOSITION_CONFLICT",
    )


def _merge_unique_definitions(
    items: Sequence[tuple[str, Sequence[BaseModel]]],
    *,
    key_attr: str,
    kind: str,
) -> tuple[list[Any], dict[str, Any]]:
    merged: list[Any] = []
    by_key: dict[str, tuple[str, Any]] = {}
    for source_id, defs in items:
        for item in defs:
            key = str(getattr(item, key_attr))
            if key not in by_key:
                copied = copy.deepcopy(item)
                merged.append(copied)
                by_key[key] = (source_id, copied)
                continue
            prev_source, existing = by_key[key]
            if existing.model_dump(mode="json") != item.model_dump(mode="json"):
                raise InvariantError(
                    f"conflicting {kind} definition for {key!r} between {prev_source!r} and {source_id!r}",
                    code="BLUEPRINT_COMPOSITION_CONFLICT",
                )
    return merged, {k: v for k, (_, v) in by_key.items()}


def _dedup_clean_roles(roles: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for role in roles:
        name = str(role).strip()
        if not name:
            raise InvariantError("role name must not be empty", code="UNKNOWN_ROLE")
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


def _assert_known_roles(
    roles: Sequence[str],
    valid_roles: set[str],
    *,
    context: str,
) -> None:
    cleaned = _dedup_clean_roles(roles)
    if not valid_roles:
        return
    for role_name in cleaned:
        if role_name not in valid_roles:
            raise InvariantError(
                f"{context}: unknown role {role_name!r} (declared roles: {sorted(valid_roles)})",
                code="UNKNOWN_ROLE",
            )


def _is_actor_role_ref(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and set(node.keys()) == {"ref"}
        and node.get("ref") == "actor.role"
    )


def _unwrap_literal(node: Any) -> tuple[bool, Any]:
    if isinstance(node, dict) and set(node.keys()) == {"lit"}:
        return True, node["lit"]
    if isinstance(node, (str, list, tuple)):
        return True, node
    return False, None


def _iter_ast_actor_role_literals(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if set(node.keys()) == {"lit"}:
            return found
        op = node.get("op")
        if op in ("eq", "ne"):
            left = node.get("left")
            right = node.get("right")
            if _is_actor_role_ref(left):
                ok, val = _unwrap_literal(right)
                if ok and isinstance(val, str):
                    found.append(val)
            elif _is_actor_role_ref(right):
                ok, val = _unwrap_literal(left)
                if ok and isinstance(val, str):
                    found.append(val)
        elif op in ("in", "not_in"):
            left = node.get("left")
            right = node.get("right")
            if _is_actor_role_ref(left):
                ok, val = _unwrap_literal(right)
                if ok and isinstance(val, (list, tuple)):
                    for item in val:
                        if isinstance(item, str):
                            found.append(item)
        for k, v in node.items():
            if k == "lit":
                continue
            if isinstance(v, (dict, list, tuple)):
                found.extend(_iter_ast_actor_role_literals(v))
    elif isinstance(node, (list, tuple)):
        for item in node:
            if isinstance(item, (dict, list, tuple)):
                found.extend(_iter_ast_actor_role_literals(item))
    return found


def _validate_expr_closed_roles(
    node: Any,
    valid_roles: set[str],
    *,
    context: str,
) -> None:
    if not valid_roles or node is None:
        return
    role_literals = _iter_ast_actor_role_literals(node)
    if role_literals:
        _assert_known_roles(role_literals, valid_roles, context=context)


def compose_blueprints(
    blueprints: Sequence[DeclarativeBlueprint | Mapping[str, Any]],
    *,
    model_id: str,
    version: str = "1.0.0",
    description: str = "",
    seed_ontology_id: str | None = None,
) -> DeclarativeBlueprint:
    """Compose multiple DeclarativeBlueprints idempotently like module imports."""
    _assert_safe_identifier(model_id, "model_id")
    if not blueprints:
        raise InvariantError(
            "compose_blueprints requires at least one blueprint",
            code="BLUEPRINT_COMPOSITION_CONFLICT",
        )
    coerced = [_coerce_blueprint(bp) for bp in blueprints]

    seed_ids: list[str] = []
    for bp in coerced:
        if bp.seed_ontology_id is not None and bp.seed_ontology_id not in seed_ids:
            seed_ids.append(bp.seed_ontology_id)
    if seed_ontology_id is not None and seed_ontology_id not in seed_ids:
        seed_ids.append(seed_ontology_id)
    if len(seed_ids) > 1:
        raise InvariantError(
            f"conflicting seed_ontology_id values during composition: {seed_ids}",
            code="BLUEPRINT_COMPOSITION_CONFLICT",
        )
    resolved_seed = seed_ids[0] if seed_ids else None

    merged_imports: list[str] = []
    seen_imports: set[str] = set()
    for bp in coerced:
        for inc in (*bp.imports, bp.model_id):
            if inc and inc != model_id and inc not in seen_imports:
                merged_imports.append(inc)
                seen_imports.add(inc)

    object_types, ot_by_name = _merge_unique_definitions(
        [(bp.model_id, bp.object_types) for bp in coerced],
        key_attr="name",
        kind="object_type",
    )
    actions, _ = _merge_unique_definitions(
        [(bp.model_id, bp.actions) for bp in coerced],
        key_attr="name",
        kind="action",
    )
    invariants, _ = _merge_unique_definitions(
        [(bp.model_id, bp.invariants) for bp in coerced],
        key_attr="name",
        kind="invariant",
    )
    views, _ = _merge_unique_definitions(
        [(bp.model_id, bp.views) for bp in coerced],
        key_attr="name",
        kind="scoped view",
    )

    merged_migrations: list[FieldMigrationSpec] = []
    mig_by_sig: dict[tuple[str, str, str], dict[str, Any]] = {}
    for bp in coerced:
        for mig in bp.migrations:
            sig = (mig.object_type, mig.field, mig.op)
            dumped = mig.model_dump(mode="json")
            if sig not in mig_by_sig:
                mig_by_sig[sig] = dumped
                merged_migrations.append(copy.deepcopy(mig))
            elif mig_by_sig[sig] != dumped:
                raise InvariantError(
                    f"conflicting migration {sig!r} during composition",
                    code="BLUEPRINT_COMPOSITION_CONFLICT",
                )

    merged_display: dict[str, dict[str, str]] = {}
    for bp in coerced:
        for bucket, mapping in bp.display.items():
            dest = merged_display.setdefault(bucket, {})
            for key, label in mapping.items():
                if not label:
                    dest.setdefault(key, label)
                elif key in dest and dest[key] and dest[key] != label:
                    raise InvariantError(
                        f"conflicting display label for {bucket}.{key!r}: {dest[key]!r} vs {label!r}",
                        code="BLUEPRINT_COMPOSITION_CONFLICT",
                    )
                else:
                    dest[key] = label

    for ot in object_types:
        for rel in ot.relations:
            if rel.target_type not in ot_by_name:
                raise InvariantError(
                    f"relation {ot.name}.{rel.field} references unknown target {rel.target_type!r}",
                    code="UNKNOWN_RELATION_TARGET",
                )
        for bal_field, (v_type, _v_link, _v_delta) in ot.voucher_delta_rules.items():
            if v_type not in ot_by_name:
                raise InvariantError(
                    f"voucher_delta_rules on {ot.name}.{bal_field} references unknown voucher type {v_type!r}",
                    code="UNKNOWN_VOUCHER_TYPE",
                )

    merged_roles: list[str] = []
    seen_roles: set[str] = set()
    for bp in coerced:
        for role_name in _dedup_clean_roles(bp.roles):
            if role_name not in seen_roles:
                seen_roles.add(role_name)
                merged_roles.append(role_name)

    resolved_desc = description or " + ".join(bp.model_id for bp in coerced)
    return DeclarativeBlueprint(
        model_id=model_id,
        version=version,
        seed_ontology_id=resolved_seed,
        imports=merged_imports,
        roles=merged_roles,
        description=resolved_desc,
        object_types=object_types,
        actions=actions,
        invariants=invariants,
        migrations=merged_migrations,
        views=views,
        display=merged_display,
    )


def _coerce_overlay(
    overlay: CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any],
) -> CustomerOverlaySpec:
    if isinstance(overlay, CustomerOverlaySpec):
        return copy.deepcopy(overlay)
    if isinstance(overlay, EvolutionPatch):
        return CustomerOverlaySpec(
            overlay_id=overlay.patch_id,
            target_version=overlay.target_version,
            description=overlay.rationale,
            add_object_types=copy.deepcopy(overlay.add_object_types),
            add_fields=copy.deepcopy(overlay.add_fields),
            remove_fields=copy.deepcopy(overlay.remove_fields),
            add_relations=copy.deepcopy(overlay.add_relations),
            update_transitions=copy.deepcopy(overlay.update_transitions),
            add_actions=copy.deepcopy(overlay.add_actions),
            update_actions=copy.deepcopy(overlay.update_actions),
            add_invariants=copy.deepcopy(overlay.add_invariants),
            remove_invariants=copy.deepcopy(overlay.remove_invariants),
            update_revert_roles=copy.deepcopy(overlay.update_revert_roles),
            add_views=copy.deepcopy(overlay.add_views),
            update_views=copy.deepcopy(overlay.update_views),
            migrations=copy.deepcopy(overlay.migrations),
        )
    if isinstance(overlay, Mapping):
        return CustomerOverlaySpec.model_validate(overlay)
    raise InvariantError(
        f"expected CustomerOverlaySpec, EvolutionPatch, or mapping, got {type(overlay).__name__}",
        code="OVERLAY_BREAKS_STANDARD_CONTRACT",
    )


def _validate_fsm_monotonic(
    ot_name: str,
    base_transitions: dict[str, list[str]],
    incoming_transitions: dict[str, list[str]],
    *,
    replace: bool,
) -> dict[str, list[str]]:
    if replace:
        for state in base_transitions:
            if state not in incoming_transitions:
                raise InvariantError(
                    f"overlay removes FSM state {ot_name}.{state!r} from standard contract",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
    merged = {} if replace else {k: list(v) for k, v in base_transitions.items()}
    for state, new_targets in incoming_transitions.items():
        if state in base_transitions:
            std_targets = base_transitions[state]
            if len(std_targets) == 0 and len(new_targets) > 0:
                raise InvariantError(
                    f"overlay reopens terminal FSM state {ot_name}.{state!r} with {new_targets}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
            if not set(std_targets).issubset(set(new_targets)):
                raise InvariantError(
                    f"overlay drops standard FSM edges on {ot_name}.{state!r}: "
                    f"expected superset of {std_targets}, got {new_targets}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
        merged[state] = list(new_targets)
    return merged


def _validate_expr_schema_refs(
    node: Any,
    *,
    ot_by_name: Mapping[str, ObjectTypeBlueprint],
    root_types: Mapping[str, str],
    param_names: set[str] | None = None,
) -> None:
    """Validate that ref paths, quantifier targets, and aggregate fields exist on declared schemas."""
    if isinstance(node, (str, int, float, bool)) or node is None or isinstance(node, list):
        return
    if not isinstance(node, dict):
        return
    if set(node.keys()) == {"lit"}:
        return
    if set(node.keys()) == {"ref"} and isinstance(node["ref"], str):
        ref_str = node["ref"]
        parts = ref_str.split(".")
        root = parts[0]
        tail = parts[1:]
        if root == "actor":
            return
        if root == "param":
            if param_names is not None and tail and tail[0] not in param_names:
                raise InvariantError(
                    f"ref {ref_str!r} references undeclared param {tail[0]!r}",
                    code="UNKNOWN_PARAM_IN_REF",
                )
            return
        if root in root_types:
            cur_type = root_types[root]
            for idx, seg in enumerate(tail):
                ot = ot_by_name.get(cur_type)
                if ot is None:
                    raise InvariantError(
                        f"ref {ref_str!r} targets unknown object type {cur_type!r}",
                        code="UNKNOWN_OBJECT_TYPE",
                    )
                has_more = idx < len(tail) - 1
                rel_map = {r.field: r.target_type for r in ot.relations}
                valid_fields = {ot.primary_key} | {f.name for f in ot.fields} | set(rel_map.keys())
                if has_more:
                    if seg not in rel_map:
                        raise InvariantError(
                            f"ref {ref_str!r} traverses unknown relation {cur_type}.{seg!r}",
                            code="UNKNOWN_RELATION_IN_REF",
                        )
                    cur_type = rel_map[seg]
                elif seg not in valid_fields:
                    raise InvariantError(
                        f"ref {ref_str!r} references unknown field {cur_type}.{seg!r}",
                        code="UNKNOWN_FIELD_IN_REF",
                    )
        return

    op = node.get("op")
    if op in QUANTIFIER_OPS or op in AGGREGATE_OPS:
        sub_type = str(node.get("object_type") or "")
        sub_ot = ot_by_name.get(sub_type)
        if sub_ot is None:
            raise InvariantError(
                f"{op!r} references unknown object_type {sub_type!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        sub_roots = {**root_types, "target": sub_type}
        if op in AGGREGATE_OPS:
            fspec = node.get("field")
            if isinstance(fspec, str):
                valid_sub_fields = (
                    {sub_ot.primary_key}
                    | {f.name for f in sub_ot.fields}
                    | {r.field for r in sub_ot.relations}
                )
                if fspec not in valid_sub_fields:
                    raise InvariantError(
                        f"aggregate {op!r} references unknown field {sub_type}.{fspec!r}",
                        code="UNKNOWN_FIELD_IN_REF",
                    )
            elif isinstance(fspec, dict):
                _validate_expr_schema_refs(
                    fspec,
                    ot_by_name=ot_by_name,
                    root_types=sub_roots,
                    param_names=param_names,
                )
        if node.get("where"):
            _validate_expr_schema_refs(
                node["where"],
                ot_by_name=ot_by_name,
                root_types=sub_roots,
                param_names=param_names,
            )
        return

    for k, v in node.items():
        if k != "lit":
            if isinstance(v, list):
                for item in v:
                    _validate_expr_schema_refs(
                        item,
                        ot_by_name=ot_by_name,
                        root_types=root_types,
                        param_names=param_names,
                    )
            else:
                _validate_expr_schema_refs(
                    v,
                    ot_by_name=ot_by_name,
                    root_types=root_types,
                    param_names=param_names,
                )


def _validate_action_preconditions_ast(
    preconditions: Sequence[InvariantRuleSpec],
    *,
    ot_by_name: Mapping[str, ObjectTypeBlueprint],
    param_names: set[str] | None = None,
) -> None:
    pre_roots = frozenset({"obj", "param", "actor"})
    for pre in preconditions:
        _assert_safe_identifier(pre.name, "precondition name")
        if pre.target_type not in ot_by_name:
            raise InvariantError(
                f"precondition {pre.name!r} targets unknown object type {pre.target_type!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        ref_roots = {"obj": pre.target_type}
        if pre.when is not None:
            validate_predicate_ast(pre.when, allowed_roots=pre_roots, allow_view=True)
            _validate_expr_schema_refs(
                pre.when, ot_by_name=ot_by_name, root_types=ref_roots, param_names=param_names
            )
        validate_predicate_ast(pre.must_hold, allowed_roots=pre_roots, allow_view=True)
        _validate_expr_schema_refs(
            pre.must_hold, ot_by_name=ot_by_name, root_types=ref_roots, param_names=param_names
        )


def _validate_action_mutations_ast(
    act: ActionBlueprint,
    *,
    ot_by_name: Mapping[str, ObjectTypeBlueprint],
) -> None:
    param_names = {p.name for p in act.params}
    subject_type = _resolve_blueprint_action_subject(act)
    has_subject = any(
        (m.op != "create" and m.target_ref is None and m.id_param in param_names)
        for m in act.mutations
    ) or any(
        (pre.id_param or (ot_by_name[pre.target_type].primary_key if pre.target_type in ot_by_name else "")) in param_names
        for pre in act.preconditions
    )
    for mut in act.mutations:
        if mut.object_type not in ot_by_name:
            raise InvariantError(
                f"action {act.name!r} mutates unknown object type {mut.object_type!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        if mut.target_ref is not None:
            mut_roots = frozenset({"obj", "source_obj", "param", "actor"})
            if mut.target_ref.startswith("obj.") and not has_subject:
                raise InvariantError(
                    f"action {act.name!r} mutation target_ref {mut.target_ref!r} requires a primary subject object",
                    code="UNAVAILABLE_REF_IN_CONTEXT",
                )
            ref_roots: dict[str, str] = {"obj": mut.object_type}
            if subject_type:
                ref_roots["source_obj"] = subject_type
            if mut.target_ref.startswith("obj.") and subject_type:
                _validate_expr_schema_refs(
                    {"ref": mut.target_ref},
                    ot_by_name=ot_by_name,
                    root_types={"obj": subject_type},
                    param_names=param_names,
                )
            elif mut.target_ref.startswith("param."):
                _validate_expr_schema_refs(
                    {"ref": mut.target_ref},
                    ot_by_name=ot_by_name,
                    root_types={},
                    param_names=param_names,
                )
        elif mut.op == "create":
            mut_roots = frozenset({"param", "actor"})
            ref_roots = {}
        else:
            mut_roots = frozenset({"obj", "param", "actor"})
            ref_roots = {"obj": mut.object_type}
        for fv in mut.set_fields.values():
            _validate_value_node(fv, allowed_roots=mut_roots, allow_view=True)
            _validate_expr_schema_refs(
                fv, ot_by_name=ot_by_name, root_types=ref_roots, param_names=param_names
            )


def _validate_action_update_monotonic(
    base_act: ActionBlueprint,
    upd_act: ActionBlueprint,
) -> None:
    if upd_act.affects_source != base_act.affects_source:
        raise InvariantError(
            f"overlay alters affects_source on standard action {base_act.name!r}",
            code="OVERLAY_BREAKS_STANDARD_CONTRACT",
        )
    if [m.model_dump(mode="json") for m in upd_act.mutations] != [
        m.model_dump(mode="json") for m in base_act.mutations
    ]:
        raise InvariantError(
            f"overlay alters mutations on standard action {base_act.name!r}",
            code="OVERLAY_BREAKS_STANDARD_CONTRACT",
        )
    upd_params = {p.name: p for p in upd_act.params}
    for bp in base_act.params:
        np = upd_params.get(bp.name)
        if np is None or np.field_type != bp.field_type:
            raise InvariantError(
                f"overlay removes or re-types param {base_act.name}.{bp.name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        if (bp.required and not np.required) or (bp.non_negative and not np.non_negative):
            raise InvariantError(
                f"overlay weakens param constraint on {base_act.name}.{bp.name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
    if not upd_act.allowed_roles or not set(upd_act.allowed_roles).issubset(set(base_act.allowed_roles)):
        raise InvariantError(
            f"overlay expands or empties allowed_roles on standard action {base_act.name!r}: "
            f"expected non-empty subset of {base_act.allowed_roles}, got {upd_act.allowed_roles}",
            code="OVERLAY_BREAKS_STANDARD_CONTRACT",
        )
    upd_pres = {pre.name: pre.model_dump(mode="json") for pre in upd_act.preconditions}
    for base_pre in base_act.preconditions:
        if upd_pres.get(base_pre.name) != base_pre.model_dump(mode="json"):
            raise InvariantError(
                f"overlay removes or weakens standard precondition {base_pre.name!r} on action {base_act.name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )


def _validate_action_closed_roles(
    act: ActionBlueprint,
    valid_roles: set[str],
    *,
    context_prefix: str,
) -> None:
    if not valid_roles:
        return
    _assert_known_roles(
        act.allowed_roles,
        valid_roles,
        context=f"{context_prefix} {act.name}.allowed_roles",
    )
    _assert_known_roles(
        act.revert_roles,
        valid_roles,
        context=f"{context_prefix} {act.name}.revert_roles",
    )
    for param in act.params:
        if param.masked_roles:
            _assert_known_roles(
                param.masked_roles,
                valid_roles,
                context=f"{context_prefix} {act.name}.params.{param.name}.masked_roles",
            )
    for pre in act.preconditions:
        _validate_expr_closed_roles(
            pre.when,
            valid_roles,
            context=f"{context_prefix} {act.name}.preconditions.{pre.name}.when",
        )
        _validate_expr_closed_roles(
            pre.must_hold,
            valid_roles,
            context=f"{context_prefix} {act.name}.preconditions.{pre.name}.must_hold",
        )
    for mut in act.mutations:
        for fv in mut.set_fields.values():
            _validate_expr_closed_roles(
                fv,
                valid_roles,
                context=f"{context_prefix} {act.name}.mutations.{mut.object_type}",
            )


def apply_overlay(
    base_blueprint: DeclarativeBlueprint | Mapping[str, Any],
    overlay: CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any],
    *,
    target_version: str | None = None,
) -> DeclarativeBlueprint:
    """Layer a customer overlay onto a standard blueprint under monotonic只增不破 guards."""
    base = _coerce_blueprint(base_blueprint)
    spec_overlay = _coerce_overlay(overlay)
    candidate = copy.deepcopy(base)

    if spec_overlay.base_model_id and spec_overlay.base_model_id != base.model_id:
        raise InvariantError(
            f"overlay targets base_model_id={spec_overlay.base_model_id!r}, got {base.model_id!r}",
            code="OVERLAY_BREAKS_STANDARD_CONTRACT",
        )

    destructive_checks = (
        ("remove_object_types", bool(spec_overlay.remove_object_types)),
        ("remove_fields", bool(spec_overlay.remove_fields)),
        ("update_primary_keys", bool(spec_overlay.update_primary_keys)),
        ("remove_invariants", bool(spec_overlay.remove_invariants)),
        ("remove_action_preconditions", bool(spec_overlay.remove_action_preconditions)),
    )
    for label, hit in destructive_checks:
        if hit:
            raise InvariantError(
                f"overlay violates standard contract via forbidden directive {label!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )

    if base.roles:
        merged_roles = _dedup_clean_roles([*base.roles, *spec_overlay.add_roles])
        candidate.roles = merged_roles
        valid_roles: set[str] = set(merged_roles)
    else:
        valid_roles = set()

    base_ot_by_name = {ot.name: ot for ot in base.object_types}
    ot_by_name = {ot.name: ot for ot in candidate.object_types}

    for ot_name, mod_fields in spec_overlay.alter_fields.items():
        if ot_name not in base_ot_by_name:
            raise InvariantError(
                f"overlay alter_fields targets non-standard object type {ot_name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        std_fields = {f.name: f for f in base_ot_by_name[ot_name].fields}
        cand_fields = ot_by_name[ot_name].fields
        for fld in mod_fields:
            std_fld = std_fields.get(fld.name)
            if std_fld is None:
                raise InvariantError(
                    f"overlay alter_fields targets unknown standard field {ot_name}.{fld.name!r}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
            if (
                fld.field_type != std_fld.field_type
                or fld.primary_key != std_fld.primary_key
                or fld.name == base_ot_by_name[ot_name].primary_key
                or (std_fld.required and not fld.required)
                or (std_fld.non_negative and not fld.non_negative)
            ):
                raise InvariantError(
                    f"overlay weakens or breaks standard field {ot_name}.{fld.name!r}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
            if fld.masked_roles:
                _assert_known_roles(
                    fld.masked_roles,
                    valid_roles,
                    context=f"overlay alter_fields {ot_name}.{fld.name}.masked_roles",
                )
            for idx, cur in enumerate(cand_fields):
                if cur.name == fld.name:
                    cand_fields[idx] = copy.deepcopy(fld)
                    break

    for new_ot in spec_overlay.add_object_types:
        _assert_safe_identifier(new_ot.name, "object type")
        if new_ot.name in ot_by_name:
            raise InvariantError(
                f"overlay cannot overwrite existing object type {new_ot.name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        for fld in new_ot.fields:
            if fld.masked_roles:
                _assert_known_roles(
                    fld.masked_roles,
                    valid_roles,
                    context=f"overlay add_object_types {new_ot.name}.{fld.name}.masked_roles",
                )
        candidate.object_types.append(copy.deepcopy(new_ot))
        ot_by_name[new_ot.name] = candidate.object_types[-1]

    for ot_name, new_fields in spec_overlay.add_fields.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"overlay adds field to unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        ot = ot_by_name[ot_name]
        existing_names = {f.name for f in ot.fields} | {r.field for r in ot.relations} | {ot.primary_key}
        for nf in new_fields:
            _assert_safe_identifier(nf.name, "field name")
            if nf.primary_key or nf.name in existing_names:
                raise InvariantError(
                    f"overlay cannot overwrite or redefine field {ot_name}.{nf.name!r}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
            if nf.masked_roles:
                _assert_known_roles(
                    nf.masked_roles,
                    valid_roles,
                    context=f"overlay add_fields {ot_name}.{nf.name}.masked_roles",
                )
            ot.fields.append(copy.deepcopy(nf))
            existing_names.add(nf.name)

    for ot_name, new_rels in spec_overlay.add_relations.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"overlay adds relation to unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        ot = ot_by_name[ot_name]
        existing_names = {f.name for f in ot.fields} | {r.field for r in ot.relations} | {ot.primary_key}
        for nr in new_rels:
            _assert_safe_identifier(nr.field, "relation field")
            if nr.target_type not in ot_by_name:
                raise InvariantError(
                    f"relation {ot_name}.{nr.field} references unknown target {nr.target_type!r}",
                    code="UNKNOWN_RELATION_TARGET",
                )
            if nr.field in existing_names:
                raise InvariantError(
                    f"overlay cannot overwrite relation or field {ot_name}.{nr.field!r}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
            ot.relations.append(copy.deepcopy(nr))
            existing_names.add(nr.field)

    for ot_name, new_map in spec_overlay.replace_transitions.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"overlay replaces transitions on unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        base_trans = base_ot_by_name[ot_name].transitions if ot_name in base_ot_by_name else ot_by_name[ot_name].transitions
        ot_by_name[ot_name].transitions = _validate_fsm_monotonic(
            ot_name, base_trans, new_map, replace=True
        )

    for ot_name, trans_map in spec_overlay.update_transitions.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"overlay updates transitions on unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        base_trans = ot_by_name[ot_name].transitions
        ot_by_name[ot_name].transitions = _validate_fsm_monotonic(
            ot_name, base_trans, trans_map, replace=False
        )

    base_field_names = {
        ot.name: ({f.name for f in ot.fields} | {r.field for r in ot.relations} | {ot.primary_key})
        for ot in base.object_types
    }
    for mig in spec_overlay.migrations:
        if (
            mig.object_type in base_field_names
            and mig.field in base_field_names[mig.object_type]
            and mig.op in ("drop_field", "rename_field", "cast_type")
        ):
            raise InvariantError(
                f"overlay migration {mig.op!r} cannot mutate standard field {mig.object_type}.{mig.field!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
    candidate.migrations.extend(copy.deepcopy(spec_overlay.migrations))

    inv_roots = frozenset({"obj"})
    base_inv_by_name = {inv.name: inv for inv in base.invariants}
    inv_names = {inv.name for inv in candidate.invariants}
    for new_inv in spec_overlay.add_invariants:
        _assert_safe_identifier(new_inv.name, "invariant name")
        if new_inv.target_type not in ot_by_name:
            raise InvariantError(
                f"invariant {new_inv.name!r} targets unknown object type {new_inv.target_type!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        inv_ref_roots = {"obj": new_inv.target_type}
        _validate_expr_closed_roles(
            new_inv.when,
            valid_roles,
            context=f"overlay add_invariants {new_inv.name}.when",
        )
        _validate_expr_closed_roles(
            new_inv.must_hold,
            valid_roles,
            context=f"overlay add_invariants {new_inv.name}.must_hold",
        )
        if new_inv.when is not None:
            validate_predicate_ast(new_inv.when, allowed_roots=inv_roots, allow_view=True)
            _validate_expr_schema_refs(new_inv.when, ot_by_name=ot_by_name, root_types=inv_ref_roots)
        validate_predicate_ast(new_inv.must_hold, allowed_roots=inv_roots, allow_view=True)
        _validate_expr_schema_refs(new_inv.must_hold, ot_by_name=ot_by_name, root_types=inv_ref_roots)
        if new_inv.name in base_inv_by_name:
            if new_inv.model_dump(mode="json") != base_inv_by_name[new_inv.name].model_dump(mode="json"):
                raise InvariantError(
                    f"overlay cannot overwrite standard invariant {new_inv.name!r}",
                    code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                )
            continue
        if new_inv.name in inv_names:
            raise InvariantError(
                f"duplicate invariant {new_inv.name!r} in overlay",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        candidate.invariants.append(copy.deepcopy(new_inv))
        inv_names.add(new_inv.name)

    base_act_by_name = {act.name: act for act in base.actions}
    act_by_name = {act.name: act for act in candidate.actions}
    for new_act in spec_overlay.add_actions:
        _assert_safe_identifier(new_act.name, "action name")
        if new_act.name in act_by_name:
            raise InvariantError(
                f"overlay cannot redefine existing action {new_act.name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        _validate_action_closed_roles(new_act, valid_roles, context_prefix="overlay add_actions")
        _validate_action_preconditions_ast(
            new_act.preconditions,
            ot_by_name=ot_by_name,
            param_names={p.name for p in new_act.params},
        )
        _validate_action_mutations_ast(new_act, ot_by_name=ot_by_name)
        candidate.actions.append(copy.deepcopy(new_act))
        act_by_name[new_act.name] = candidate.actions[-1]

    for upd_act in spec_overlay.update_actions:
        if upd_act.name not in act_by_name:
            raise InvariantError(
                f"overlay updates unknown action {upd_act.name!r}",
                code="UNKNOWN_ACTION",
            )
        _validate_action_closed_roles(upd_act, valid_roles, context_prefix="overlay update_actions")
        _validate_action_preconditions_ast(
            upd_act.preconditions,
            ot_by_name=ot_by_name,
            param_names={p.name for p in upd_act.params},
        )
        _validate_action_mutations_ast(upd_act, ot_by_name=ot_by_name)
        if upd_act.name in base_act_by_name:
            _validate_action_update_monotonic(base_act_by_name[upd_act.name], upd_act)
        idx = next(i for i, a in enumerate(candidate.actions) if a.name == upd_act.name)
        candidate.actions[idx] = copy.deepcopy(upd_act)
        act_by_name[upd_act.name] = candidate.actions[idx]

    for act_name, extra_pres in spec_overlay.add_action_preconditions.items():
        if act_name not in act_by_name:
            raise InvariantError(
                f"overlay adds preconditions to unknown action {act_name!r}",
                code="UNKNOWN_ACTION",
            )
        target_act = act_by_name[act_name]
        for pre in extra_pres:
            _validate_expr_closed_roles(
                pre.when,
                valid_roles,
                context=f"overlay add_action_preconditions {act_name}.{pre.name}.when",
            )
            _validate_expr_closed_roles(
                pre.must_hold,
                valid_roles,
                context=f"overlay add_action_preconditions {act_name}.{pre.name}.must_hold",
            )
        _validate_action_preconditions_ast(
            extra_pres,
            ot_by_name=ot_by_name,
            param_names={p.name for p in target_act.params},
        )
        existing_pre_by_name = {p.name: p.model_dump(mode="json") for p in target_act.preconditions}
        for pre in extra_pres:
            if pre.name in existing_pre_by_name:
                if existing_pre_by_name[pre.name] != pre.model_dump(mode="json"):
                    raise InvariantError(
                        f"overlay cannot overwrite precondition {pre.name!r} on action {act_name!r}",
                        code="OVERLAY_BREAKS_STANDARD_CONTRACT",
                    )
                continue
            target_act.preconditions.append(copy.deepcopy(pre))
            existing_pre_by_name[pre.name] = pre.model_dump(mode="json")

    for act_name, roles in spec_overlay.restrict_allowed_roles.items():
        if act_name not in act_by_name:
            raise InvariantError(
                f"overlay restricts roles on unknown action {act_name!r}",
                code="UNKNOWN_ACTION",
            )
        dedup_roles = _dedup_clean_roles(roles) if roles else []
        _assert_known_roles(
            dedup_roles,
            valid_roles,
            context=f"overlay restrict_allowed_roles {act_name}",
        )
        current_roles = act_by_name[act_name].allowed_roles
        if not dedup_roles or not set(dedup_roles).issubset(set(current_roles)):
            raise InvariantError(
                f"overlay expands or empties allowed_roles on {act_name!r}: "
                f"expected non-empty subset of {current_roles}, got {dedup_roles}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        act_by_name[act_name].allowed_roles = dedup_roles

    for act_name, roles in spec_overlay.update_revert_roles.items():
        if act_name not in act_by_name:
            raise InvariantError(
                f"overlay updates revert_roles on unknown action {act_name!r}",
                code="UNKNOWN_ACTION",
            )
        dedup_roles = _dedup_clean_roles(roles) if roles else []
        _assert_known_roles(
            dedup_roles,
            valid_roles,
            context=f"overlay update_revert_roles {act_name}",
        )
        if (
            act_name in base_act_by_name
            and base_act_by_name[act_name].revert_roles
            and not set(dedup_roles).issubset(set(base_act_by_name[act_name].revert_roles))
        ):
            raise InvariantError(
                f"overlay expands revert_roles beyond standard action {act_name!r}",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        act_by_name[act_name].revert_roles = dedup_roles

    valid_ot_names = set(ot_by_name.keys())
    valid_act_names = set(act_by_name.keys())
    view_by_name = {v.name: idx for idx, v in enumerate(candidate.views)}
    for new_view in spec_overlay.add_views:
        _assert_known_roles(
            new_view.roles,
            valid_roles,
            context=f"overlay add_views {new_view.name}.roles",
        )
        _validate_scoped_view_bp(new_view, valid_ot_names=valid_ot_names, valid_act_names=valid_act_names)
        if new_view.name in view_by_name:
            raise InvariantError(
                f"scoped view {new_view.name!r} already exists",
                code="OVERLAY_BREAKS_STANDARD_CONTRACT",
            )
        candidate.views.append(copy.deepcopy(new_view))
        view_by_name[new_view.name] = len(candidate.views) - 1
    for upd_view in spec_overlay.update_views:
        _assert_known_roles(
            upd_view.roles,
            valid_roles,
            context=f"overlay update_views {upd_view.name}.roles",
        )
        _validate_scoped_view_bp(upd_view, valid_ot_names=valid_ot_names, valid_act_names=valid_act_names)
        if upd_view.name not in view_by_name:
            raise InvariantError(
                f"overlay updates unknown scoped view {upd_view.name!r}",
                code="INVALID_SCOPED_VIEW",
            )
        candidate.views[view_by_name[upd_view.name]] = copy.deepcopy(upd_view)

    for bucket, entries in spec_overlay.display.items():
        dest = candidate.display.setdefault(bucket, {})
        for key, label in entries.items():
            if label:
                dest[key] = label

    resolved_ver = target_version or spec_overlay.target_version
    if resolved_ver:
        candidate.version = resolved_ver
    return candidate


def apply_evolution_patch(
    base: DeclarativeBlueprint,
    patch: EvolutionPatch,
) -> DeclarativeBlueprint:
    """Return a new DeclarativeBlueprint by immutably applying an EvolutionPatch."""
    if patch.base_version != base.version:
        raise InvariantError(
            f"patch base_version {patch.base_version!r} != blueprint version {base.version!r}",
            code="PATCH_VERSION_MISMATCH",
        )
    candidate = copy.deepcopy(base)
    candidate.version = patch.target_version

    ot_by_name = {ot.name: ot for ot in candidate.object_types}
    for new_ot in patch.add_object_types:
        if new_ot.name in ot_by_name:
            raise InvariantError(
                f"object type {new_ot.name!r} already exists",
                code="DUPLICATE_OBJECT_TYPE",
            )
        candidate.object_types.append(copy.deepcopy(new_ot))
        ot_by_name[new_ot.name] = candidate.object_types[-1]

    for ot_name, new_fields in patch.add_fields.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"patch adds field to unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        existing_names = {f.name for f in ot_by_name[ot_name].fields} | {
            r.field for r in ot_by_name[ot_name].relations
        }
        for nf in new_fields:
            if nf.name in existing_names:
                raise InvariantError(
                    f"field {nf.name!r} already exists on {ot_name!r}",
                    code="DUPLICATE_FIELD",
                )
            ot_by_name[ot_name].fields.append(copy.deepcopy(nf))
            existing_names.add(nf.name)

    for ot_name, new_rels in patch.add_relations.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"patch adds relation to unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        existing_names = {f.name for f in ot_by_name[ot_name].fields} | {
            r.field for r in ot_by_name[ot_name].relations
        }
        for nr in new_rels:
            if nr.target_type not in ot_by_name:
                raise InvariantError(
                    f"relation {ot_name}.{nr.field} references unknown target {nr.target_type!r}",
                    code="UNKNOWN_RELATION_TARGET",
                )
            if nr.field in existing_names:
                raise InvariantError(
                    f"relation field {nr.field!r} already exists on {ot_name!r}",
                    code="DUPLICATE_RELATION",
                )
            ot_by_name[ot_name].relations.append(copy.deepcopy(nr))
            existing_names.add(nr.field)

    for ot_name, trans_map in patch.update_transitions.items():
        if ot_name not in ot_by_name:
            raise InvariantError(
                f"patch updates transitions on unknown object type {ot_name!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        merged = dict(ot_by_name[ot_name].transitions)
        merged.update({k: list(v) for k, v in trans_map.items()})
        ot_by_name[ot_name].transitions = merged

    act_by_name = {a.name: idx for idx, a in enumerate(candidate.actions)}
    for new_act in patch.add_actions:
        if new_act.name in act_by_name:
            raise InvariantError(
                f"action {new_act.name!r} already exists",
                code="DUPLICATE_ACTION",
            )
        candidate.actions.append(copy.deepcopy(new_act))
        act_by_name[new_act.name] = len(candidate.actions) - 1

    for upd_act in patch.update_actions:
        if upd_act.name not in act_by_name:
            raise InvariantError(
                f"patch updates unknown action {upd_act.name!r}",
                code="UNKNOWN_ACTION",
            )
        candidate.actions[act_by_name[upd_act.name]] = copy.deepcopy(upd_act)

    for act_name, roles in patch.update_revert_roles.items():
        if act_name not in act_by_name:
            raise InvariantError(
                f"patch sets revert_roles on unknown action {act_name!r}",
                code="UNKNOWN_ACTION",
            )
        candidate.actions[act_by_name[act_name]].revert_roles = list(roles)

    removed = set(patch.remove_invariants)
    candidate.invariants = [inv for inv in candidate.invariants if inv.name not in removed]
    inv_names = {inv.name for inv in candidate.invariants}
    for new_inv in patch.add_invariants:
        if new_inv.name in inv_names:
            candidate.invariants = [
                copy.deepcopy(new_inv) if inv.name == new_inv.name else inv
                for inv in candidate.invariants
            ]
        else:
            candidate.invariants.append(copy.deepcopy(new_inv))
            inv_names.add(new_inv.name)

    candidate.migrations.extend(copy.deepcopy(patch.migrations))

    if patch.remove_fields:
        removed_by_type: dict[str, set[str]] = {}
        for ot_name, fnames in patch.remove_fields.items():
            if ot_name not in ot_by_name:
                raise InvariantError(
                    f"patch removes fields from unknown object type {ot_name!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            ot = ot_by_name[ot_name]
            fields_by_name = {f.name: f for f in ot.fields}
            for fname in fnames:
                if fname not in fields_by_name:
                    raise InvariantError(
                        f"cannot remove unknown field {ot_name}.{fname}",
                        code="UNKNOWN_FIELD",
                    )
                fld = fields_by_name[fname]
                if fname == ot.primary_key or fld.primary_key:
                    raise InvariantError(
                        f"cannot remove primary key field {ot_name}.{fname}",
                        code="CANNOT_REMOVE_PRIMARY_KEY",
                    )
                if fname == ot.title_field:
                    raise InvariantError(
                        f"cannot remove title_field {ot_name}.{fname}",
                        code="FIELD_IN_USE",
                    )
                if (ot.transitions or ot.initial_statuses is not None) and fname == ot.status_field:
                    raise InvariantError(
                        f"cannot remove FSM status_field {ot_name}.{fname}",
                        code="FIELD_IN_USE",
                    )
                for bal_field, (_vt, v_link, v_delta) in ot.voucher_delta_rules.items():
                    if fname in (bal_field, v_link, v_delta):
                        raise InvariantError(
                            f"cannot remove voucher-conservation field {ot_name}.{fname}",
                            code="FIELD_IN_USE",
                        )
                removed_by_type.setdefault(ot_name, set()).add(fname)

        for inv in candidate.invariants:
            for expr in (inv.when, inv.must_hold):
                hit = _expr_removed_field_hit(
                    expr,
                    removed_by_type=removed_by_type,
                    ot_by_name=ot_by_name,
                    root_types={"obj": inv.target_type},
                )
                if hit is not None:
                    raise InvariantError(
                        f"cannot remove {hit[0]}.{hit[1]} referenced by invariant {inv.name!r}",
                        code="FIELD_IN_USE",
                    )

        for act in candidate.actions:
            anchor_type = _resolve_blueprint_action_subject(act)
            for pre in act.preconditions:
                for expr in (pre.when, pre.must_hold):
                    hit = _expr_removed_field_hit(
                        expr,
                        removed_by_type=removed_by_type,
                        ot_by_name=ot_by_name,
                        root_types={"obj": pre.target_type},
                    )
                    if hit is not None:
                        raise InvariantError(
                            f"cannot remove {hit[0]}.{hit[1]} referenced by precondition {pre.name!r}",
                            code="FIELD_IN_USE",
                        )
            for mut in act.mutations:
                if mut.target_ref and anchor_type is not None:
                    hit = _ref_hits_removed_field(
                        mut.target_ref,
                        removed_by_type=removed_by_type,
                        ot_by_name=ot_by_name,
                        root_types={"obj": anchor_type},
                    )
                    if hit is not None:
                        raise InvariantError(
                            f"cannot remove {hit[0]}.{hit[1]} referenced by mutation target_ref on {act.name!r}",
                            code="FIELD_IN_USE",
                        )
                mut_roots = {"obj": mut.object_type}
                if mut.target_ref and anchor_type is not None:
                    mut_roots["source_obj"] = anchor_type
                for k, v_expr in mut.set_fields.items():
                    if k in removed_by_type.get(mut.object_type, ()):
                        raise InvariantError(
                            f"cannot remove {mut.object_type}.{k} written by action {act.name!r}",
                            code="FIELD_IN_USE",
                        )
                    hit = _expr_removed_field_hit(
                        v_expr,
                        removed_by_type=removed_by_type,
                        ot_by_name=ot_by_name,
                        root_types=mut_roots,
                    )
                    if hit is not None:
                        raise InvariantError(
                            f"cannot remove {hit[0]}.{hit[1]} read by mutation on {act.name!r}",
                            code="FIELD_IN_USE",
                        )

        existing_drops = {
            (m.object_type, m.field)
            for m in candidate.migrations
            if m.op == "drop_field"
        }
        for ot_name, fset in removed_by_type.items():
            ot = ot_by_name[ot_name]
            ot.fields = [f for f in ot.fields if f.name not in fset]
            for fname in sorted(fset):
                if (ot_name, fname) not in existing_drops:
                    candidate.migrations.append(
                        FieldMigrationSpec(object_type=ot_name, op="drop_field", field=fname)
                    )
                    existing_drops.add((ot_name, fname))

    valid_ot_names = {ot.name for ot in candidate.object_types}
    valid_act_names = {act.name for act in candidate.actions}
    view_by_name = {v.name: idx for idx, v in enumerate(candidate.views)}
    for new_view in patch.add_views:
        _validate_scoped_view_bp(new_view, valid_ot_names=valid_ot_names, valid_act_names=valid_act_names)
        if new_view.name in view_by_name:
            raise InvariantError(
                f"scoped view {new_view.name!r} already exists",
                code="INVALID_SCOPED_VIEW",
            )
        candidate.views.append(copy.deepcopy(new_view))
        view_by_name[new_view.name] = len(candidate.views) - 1
    for upd_view in patch.update_views:
        _validate_scoped_view_bp(upd_view, valid_ot_names=valid_ot_names, valid_act_names=valid_act_names)
        if upd_view.name not in view_by_name:
            raise InvariantError(
                f"patch updates unknown scoped view {upd_view.name!r}",
                code="INVALID_SCOPED_VIEW",
            )
        candidate.views[view_by_name[upd_view.name]] = copy.deepcopy(upd_view)

    return candidate


def _validate_scoped_view_bp(
    view_bp: ScopedViewBlueprint,
    *,
    valid_ot_names: set[str] | frozenset[str],
    valid_act_names: set[str] | frozenset[str],
    valid_fn_names: set[str] | frozenset[str] | None = None,
) -> None:
    _assert_safe_identifier(view_bp.name, "scoped view name")
    for ot_name in view_bp.object_types:
        if ot_name not in valid_ot_names:
            raise InvariantError(
                f"scoped view {view_bp.name!r} references unknown object type {ot_name!r}",
                code="INVALID_SCOPED_VIEW",
            )
    for act_name in view_bp.actions:
        if act_name not in valid_act_names:
            raise InvariantError(
                f"scoped view {view_bp.name!r} references unknown action {act_name!r}",
                code="INVALID_SCOPED_VIEW",
            )
    if valid_fn_names is not None:
        for fn_name in view_bp.functions:
            if fn_name not in valid_fn_names:
                raise InvariantError(
                    f"scoped view {view_bp.name!r} references unknown function {fn_name!r}",
                    code="INVALID_SCOPED_VIEW",
                )


def apply_adapter_mapping_patch(
    base_mapping: AdapterMappingSpec | None,
    patch: EvolutionPatch,
) -> AdapterMappingSpec | None:
    """Immutably apply L2 entity/action binding updates from an EvolutionPatch onto AdapterMappingSpec."""
    if (
        base_mapping is None
        and not patch.update_entity_mappings
        and not patch.add_action_bindings
        and not patch.update_action_bindings
        and not patch.remove_fields
    ):
        return None
    updated = copy.deepcopy(base_mapping) if base_mapping is not None else AdapterMappingSpec()

    replaced_types = {em.target_object_type for em in patch.update_entity_mappings}
    em_idx = {em.target_object_type: idx for idx, em in enumerate(updated.entity_mappings)}
    for new_em in patch.update_entity_mappings:
        cloned_em = copy.deepcopy(new_em)
        if new_em.target_object_type in em_idx:
            updated.entity_mappings[em_idx[new_em.target_object_type]] = cloned_em
        else:
            em_idx[new_em.target_object_type] = len(updated.entity_mappings)
            updated.entity_mappings.append(cloned_em)

    for ot_name, fnames in patch.remove_fields.items():
        if ot_name in replaced_types or ot_name not in em_idx:
            continue
        drop_set = set(fnames)
        em = updated.entity_mappings[em_idx[ot_name]]
        em.field_transforms = [tf for tf in em.field_transforms if tf.target_field not in drop_set]

    ab_idx = {ab.action_name: idx for idx, ab in enumerate(updated.action_bindings)}
    for new_ab in (*patch.add_action_bindings, *patch.update_action_bindings):
        cloned_ab = copy.deepcopy(new_ab)
        if new_ab.action_name in ab_idx:
            updated.action_bindings[ab_idx[new_ab.action_name]] = cloned_ab
        else:
            ab_idx[new_ab.action_name] = len(updated.action_bindings)
            updated.action_bindings.append(cloned_ab)

    return updated


def migrate_m0_snapshot(
    objects: Sequence[dict[str, Any]],
    migrations: Sequence[FieldMigrationSpec],
) -> list[dict[str, Any]]:
    """Deterministically migrate M0 object dicts before loading into a candidate M1."""
    migrated: list[dict[str, Any]] = []
    for raw in objects:
        entry = copy.deepcopy(raw)
        obj_type = str(entry.get("object_type") or "")
        data = entry.get("data")
        if not isinstance(data, dict):
            migrated.append(entry)
            continue
        for mig in migrations:
            if mig.object_type != obj_type:
                continue
            if mig.op == "add_default":
                if mig.field not in data:
                    data[mig.field] = copy.deepcopy(mig.default)
            elif mig.op == "rename_field":
                if mig.new_field and mig.field in data and mig.new_field not in data:
                    data[mig.new_field] = data.pop(mig.field)
            elif mig.op == "cast_type" and mig.target_type and mig.field in data:
                py_type = _resolve_scalar_type(mig.target_type)
                data[mig.field] = py_type(data[mig.field])
            elif mig.op == "drop_field":
                data.pop(mig.field, None)
        migrated.append(entry)
    return migrated


def compile_blueprint(
    blueprint: DeclarativeBlueprint,
    *,
    seed_ontology: SeedOntology | None = None,
) -> OntologySpec:
    """Compile a L1 DeclarativeBlueprint into a real OntologySpec with ZERO eval/exec.

    When ``seed_ontology`` is provided, the compiled spec explicitly extends
    ``seed_ontology.base_spec``, resolves every ``ot.archetype`` to its real
    abstract Python superclass, and automatically inherits all seed axioms
    (non-negativity, FSM transitions, voucher-backed balance conservation, or
    any custom industry axioms).
    """
    if not blueprint.object_types:
        raise InvariantError(
            "declarative blueprint must declare at least one object type",
            code="EMPTY_OBJECT_TYPES",
        )
    if (
        seed_ontology is not None
        and blueprint.seed_ontology_id is not None
        and blueprint.seed_ontology_id != seed_ontology.seed_id
    ):
        raise InvariantError(
            f"blueprint targets seed_ontology_id={blueprint.seed_ontology_id!r}, "
            f"but compiler was given seed {seed_ontology.seed_id!r}",
            code="SEED_ONTOLOGY_MISMATCH",
        )

    spec = OntologySpec(extends=(seed_ontology.base_spec,)) if seed_ontology is not None else OntologySpec()
    seed_roles = set(seed_ontology.base_spec.roles) if seed_ontology is not None else set()
    if blueprint.roles:
        for role_name in _dedup_clean_roles(blueprint.roles):
            if role_name not in seed_roles and role_name not in spec.roles:
                spec.role(role_name)
    compiled_valid_roles = set(spec.roles)

    created_classes: dict[str, type[ObjectType]] = {}
    type_names = {_assert_safe_identifier(ot.name, "object type") for ot in blueprint.object_types}

    for ot in blueprint.object_types:
        _assert_safe_identifier(ot.primary_key, "primary_key")
        for rel in ot.relations:
            _assert_safe_identifier(rel.field, "relation field")
            if rel.target_type not in type_names:
                raise InvariantError(
                    f"relation {ot.name}.{rel.field} references unknown target {rel.target_type!r}",
                    code="UNKNOWN_RELATION_TARGET",
                )
        for bal_field, rule_triple in ot.voucher_delta_rules.items():
            _assert_safe_identifier(bal_field, "voucher_delta_rules balance field")
            v_type, v_link, v_delta = rule_triple
            if v_type not in type_names:
                raise InvariantError(
                    f"voucher_delta_rules on {ot.name}.{bal_field} references unknown voucher type {v_type!r}",
                    code="UNKNOWN_VOUCHER_TYPE",
                )
            _assert_safe_identifier(v_link, "voucher link field")
            _assert_safe_identifier(v_delta, "voucher delta field")

    object_display: dict[str, str] = {}
    non_neg_map: dict[str, tuple[str, ...]] = {}
    fsm_specs: dict[str, tuple[str, tuple[str, ...] | None, dict[str, tuple[str, ...]]]] = {}
    voucher_rules_map: dict[str, dict[str, tuple[str, str, str]]] = {}

    for ot in blueprint.object_types:
        if seed_ontology is not None:
            base_cls = seed_ontology.resolve_base_class(ot.archetype)
        else:
            if ot.archetype not in _DEFAULT_STANDALONE_ARCHETYPES:
                raise InvariantError(
                    f"unknown archetype {ot.archetype!r} without seed_ontology; "
                    f"allowed standalone archetypes: {sorted(_DEFAULT_STANDALONE_ARCHETYPES)}",
                    code="UNKNOWN_SEED_ARCHETYPE",
                )
            base_cls = ObjectType

        field_defs: dict[str, Any] = {}
        owned_fields: dict[str, Any] = {}
        masked_fields: dict[str, tuple[str, ...]] = {}
        non_neg_fields: list[str] = []
        optional_scalar_defaults: dict[str, Any] = {}

        pk_declared = False
        for fld in ot.fields:
            _assert_safe_identifier(fld.name, "field name")
            py_type = _resolve_scalar_type(fld.field_type)
            ann: Any = AutoID if (fld.auto_id and py_type is str) else py_type
            is_pk = fld.primary_key or fld.name == ot.primary_key
            if is_pk:
                pk_declared = True
            if fld.required or is_pk:
                default_val: Any = ... if fld.default is None else fld.default
            else:
                default_val = _default_for_type(py_type) if fld.default is None else fld.default
                optional_scalar_defaults[fld.name] = default_val
            field_defs[fld.name] = (ann, default_val)
            if fld.owned:
                owned_fields[fld.name] = None if default_val is ... else default_val
            if fld.masked_roles:
                masked_fields[fld.name] = tuple(fld.masked_roles)
            if fld.non_negative:
                non_neg_fields.append(fld.name)

        if not pk_declared:
            field_defs[ot.primary_key] = (str, ...)

        for rel in ot.relations:
            extra_schema = {
                "property_kind": "object",
                "range": rel.target_type,
                "multiple": rel.multiple,
            }
            if rel.multiple:
                field_defs[rel.field] = (
                    tuple[str, ...],
                    Field(default=(), json_schema_extra=extra_schema),
                )
            else:
                if rel.required:
                    field_defs[rel.field] = (str, Field(..., json_schema_extra=extra_schema))
                else:
                    field_defs[rel.field] = (str | None, Field(default=None, json_schema_extra=extra_schema))
            if rel.owned:
                owned_fields[rel.field] = () if rel.multiple else None

        if ot.aggregate_root is not None:
            agg_root = ot.aggregate_root
        elif seed_ontology is not None:
            agg_root = seed_ontology.default_aggregate_root_for(ot.archetype, base_cls)
        else:
            agg_root = False

        if ot.append_only is not None:
            append_only = ot.append_only
        elif seed_ontology is not None:
            append_only = seed_ontology.default_append_only_for(ot.archetype, base_cls)
        else:
            append_only = False

        resolved_owned: bool | dict[str, Any]
        if ot.owned is True:
            resolved_owned = True
        elif isinstance(ot.owned, dict):
            merged_owned = dict(ot.owned)
            merged_owned.update(owned_fields)
            resolved_owned = merged_owned
        elif owned_fields:
            resolved_owned = owned_fields
        else:
            resolved_owned = False

        validators: dict[str, Any] | None = None
        if optional_scalar_defaults:
            def _make_null_coercer(defaults_map: dict[str, Any]) -> Any:
                @model_validator(mode="before")
                @classmethod
                def _coerce_null_optional_scalars(_cls_inner: Any, data: Any) -> Any:
                    if isinstance(data, dict):
                        return {
                            k: (defaults_map[k] if (v is None and k in defaults_map) else v)
                            for k, v in data.items()
                        }
                    return data

                return _coerce_null_optional_scalars

            validators = {"_coerce_null_optional_scalars": _make_null_coercer(dict(optional_scalar_defaults))}

        cls = create_model(ot.name, __base__=base_cls, __validators__=validators, **field_defs)
        cls.__primary_key__ = ot.primary_key
        cls.__title__ = ot.title_field or ot.primary_key
        cls.__aggregate_root__ = bool(agg_root)
        cls.__append_only__ = bool(append_only)
        cls.__owned__ = resolved_owned
        if non_neg_fields:
            cls.__non_negative_fields__ = tuple(non_neg_fields)
        if ot.transitions or ot.initial_statuses is not None:
            cls.__status_field__ = ot.status_field
            cls.__initial_statuses__ = (
                tuple(ot.initial_statuses) if ot.initial_statuses is not None else None
            )
            cls.__transitions__ = {k: tuple(v) for k, v in ot.transitions.items()}
        if ot.voucher_delta_rules:
            cls.__voucher_delta_rules__ = {k: tuple(v) for k, v in ot.voucher_delta_rules.items()}
        if masked_fields:
            cls.__masked_fields__ = masked_fields
        if ot.view_tags:
            cls.__view_tags__ = tuple(ot.view_tags)

        spec.object_type(cls)
        created_classes[ot.name] = cls
        if ot.display_label:
            object_display[ot.name] = ot.display_label
        if non_neg_fields:
            non_neg_map[ot.name] = tuple(non_neg_fields)
        if ot.transitions or ot.initial_statuses is not None:
            fsm_specs[ot.name] = (
                ot.status_field,
                tuple(ot.initial_statuses) if ot.initial_statuses is not None else None,
                {k: tuple(v) for k, v in ot.transitions.items()},
            )
        if ot.voucher_delta_rules:
            voucher_rules_map[ot.name] = {k: tuple(v) for k, v in ot.voucher_delta_rules.items()}

    for ot in blueprint.object_types:
        cls = created_classes[ot.name]
        prop_map: dict[str, ObjectProperty] = dict(getattr(cls, "__object_properties__", None) or {})
        for rel in ot.relations:
            prop_name = f"{ot.name}.{rel.field}"
            decl = ObjectProperty(
                name=prop_name,
                domain=ot.name,
                range=rel.target_type,
                cardinality="many-to-many" if rel.multiple else "many-to-one",
                owned=bool(rel.owned or cls.is_fully_owned()),
                field=rel.field,
                description=rel.description,
            )
            spec.add_object_property(decl)
            prop_map[rel.field] = decl
        cls.__object_properties__ = prop_map

    seed_covers_non_neg = seed_ontology is not None and seed_ontology.covers_non_negative()
    if non_neg_map and not seed_covers_non_neg:

        @spec.invariant(
            "declarative_non_negative_fields",
            code="NEGATIVE_VALUE",
            description="Declared non-negative fields must remain >= 0.",
        )
        def _check_non_negative(view: InvariantView) -> bool:
            for type_name, field_names in non_neg_map.items():
                cls = view.spec.get_object_type(type_name)
                for obj in view.query(cls):
                    for fn in field_names:
                        val = getattr(obj, fn, 0)
                        if val is not None and val < 0:
                            raise InvariantError(
                                f"{type_name}:{obj.primary_key_value()}.{fn}={val} < 0",
                                code="NEGATIVE_VALUE",
                            )
            return True

    # Only run fallback declarative FSM check on types not already covered by an inherited seed archetype axiom
    fallback_fsm_specs = {
        tname: fspec
        for tname, fspec in fsm_specs.items()
        if not (seed_ontology is not None and seed_ontology.covers_fsm_for(created_classes[tname]))
    }

    if fallback_fsm_specs:

        @spec.transition_invariant(
            "declarative_fsm_transitions",
            code="ILLEGAL_STATE_TRANSITION",
            description="Status transitions must follow declared FSM edges.",
        )
        def _check_fsm(diff: ObjectDiff, _view: InvariantView) -> bool:
            for change in diff.object_changes:
                if change.object_type not in fallback_fsm_specs:
                    continue
                status_field, initial_allowed, transitions = fallback_fsm_specs[change.object_type]
                if change.kind == "create" and initial_allowed is not None and change.after:
                    after_s = change.after.get(status_field)
                    if after_s not in initial_allowed:
                        raise InvariantError(
                            f"{change.object_type}:{change.object_id} invalid initial "
                            f"{status_field}={after_s!r}; allowed={initial_allowed}",
                            code="ILLEGAL_INITIAL_STATUS",
                        )
                if change.kind == "update" and change.before and change.after:
                    before_s = change.before.get(status_field)
                    after_s = change.after.get(status_field)
                    if before_s != after_s and transitions:
                        allowed_next = transitions.get(str(before_s), ())
                        if after_s not in allowed_next:
                            raise InvariantError(
                                f"{change.object_type}:{change.object_id} illegal transition "
                                f"{before_s!r} -> {after_s!r}; allowed={allowed_next}",
                                code="ILLEGAL_STATE_TRANSITION",
                            )
            return True

    # Balance conservation fallback when not already inherited from seed_ontology
    fallback_voucher_rules = {
        tname: rules
        for tname, rules in voucher_rules_map.items()
        if not (
            seed_ontology is not None
            and seed_ontology.covers_balance_conservation_for(created_classes[tname])
        )
    }

    if fallback_voucher_rules:

        @spec.transition_invariant(
            "declarative_voucher_balance_conservation",
            code="UNBACKED_BALANCE_MUTATION",
            description="Balance ledger field changes must equal same-transaction voucher deltas.",
        )
        def _check_voucher_conservation(diff: ObjectDiff, _view: InvariantView) -> bool:
            for ledger_type, rules in fallback_voucher_rules.items():
                for balance_field, (voucher_type, link_field, delta_field) in rules.items():
                    ledger_deltas: dict[str, int | float] = defaultdict(int)
                    for change in diff.object_changes:
                        if change.object_type != ledger_type:
                            continue
                        if change.kind == "create":
                            delta = (change.after or {}).get(balance_field, 0) or 0
                        elif change.kind == "update":
                            after_val = (change.after or {}).get(balance_field, 0) or 0
                            before_val = (change.before or {}).get(balance_field, 0) or 0
                            delta = after_val - before_val
                        elif change.kind == "delete":
                            before_val = (change.before or {}).get(balance_field, 0) or 0
                            delta = -before_val
                        else:
                            delta = 0
                        if delta != 0:
                            ledger_deltas[change.object_id] += delta

                    voucher_sums: dict[str, int | float] = defaultdict(int)
                    for change in diff.object_changes:
                        if change.object_type != voucher_type or change.kind != "create":
                            continue
                        after_props = change.after or {}
                        target_id = after_props.get(link_field)
                        delta_val = after_props.get(delta_field, 0) or 0
                        if target_id is None:
                            continue
                        voucher_sums[str(target_id)] += delta_val

                    for ledger_id in sorted(set(ledger_deltas) | set(voucher_sums)):
                        expected_ledger = ledger_deltas.get(ledger_id, 0)
                        actual_voucher = voucher_sums.get(ledger_id, 0)
                        if abs(expected_ledger - actual_voucher) > 1e-9:
                            raise InvariantError(
                                f"{ledger_type}:{ledger_id}.{balance_field} changed by "
                                f"{expected_ledger}, but {voucher_type}.{delta_field} sum "
                                f"in transaction is {actual_voucher}",
                                code="UNBACKED_BALANCE_MUTATION",
                            )
            return True

    ot_by_name = {ot.name: ot for ot in blueprint.object_types}
    for inv in blueprint.invariants:
        _assert_safe_identifier(inv.name, "invariant name")
        if inv.target_type not in created_classes:
            raise InvariantError(
                f"invariant {inv.name!r} targets unknown object type {inv.target_type!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        inv_roots = frozenset({"obj"})
        inv_ref_roots = {"obj": inv.target_type}
        if compiled_valid_roles:
            _validate_expr_closed_roles(
                inv.when,
                compiled_valid_roles,
                context=f"invariant {inv.name}.when",
            )
            _validate_expr_closed_roles(
                inv.must_hold,
                compiled_valid_roles,
                context=f"invariant {inv.name}.must_hold",
            )
        if inv.when is not None:
            validate_predicate_ast(inv.when, allowed_roots=inv_roots, allow_view=True)
            _validate_expr_schema_refs(inv.when, ot_by_name=ot_by_name, root_types=inv_ref_roots)
        validate_predicate_ast(inv.must_hold, allowed_roots=inv_roots, allow_view=True)
        _validate_expr_schema_refs(inv.must_hold, ot_by_name=ot_by_name, root_types=inv_ref_roots)

        def _make_invariant(rule: InvariantRuleSpec) -> Callable[[InvariantView], bool]:
            target_cls = created_classes[rule.target_type]

            def _check(view: InvariantView) -> bool:
                for obj in view.query(target_cls):
                    ctx = {"obj": obj}
                    if rule.when is not None and not eval_predicate(rule.when, ctx, view):
                        continue
                    if not eval_predicate(rule.must_hold, ctx, view):
                        msg = (
                            f"[{rule.code}] {rule.description}"
                            if rule.description
                            else f"[{rule.code}] invariant {rule.name} violated on "
                            f"{rule.target_type}:{obj.primary_key_value()}"
                        )
                        raise InvariantError(msg, code=rule.code)
                return True

            return _check

        spec.invariant(inv.name, code=inv.code, description=inv.description)(_make_invariant(inv))

    def _resolve_subject(
        action_bp: ActionBlueprint,
        pdefs: dict[str, Any],
    ) -> tuple[type[ObjectType] | None, str | None]:
        for m in action_bp.mutations:
            if m.op != "create" and m.target_ref is None and m.id_param in pdefs:
                return created_classes[m.object_type], m.id_param
        for pre in action_bp.preconditions:
            s_cls = created_classes[pre.target_type]
            s_idp = pre.id_param or s_cls.__primary_key__
            if s_idp in pdefs:
                return s_cls, s_idp
        return None, None

    action_display: dict[str, str] = {}
    for act in blueprint.actions:
        _assert_safe_identifier(act.name, "action name")
        param_defs: dict[str, Any] = {}
        for p in act.params:
            _assert_safe_identifier(p.name, "action param")
            py_type = _resolve_scalar_type(p.field_type)
            if p.required:
                default_val = ... if p.default is None else p.default
            else:
                default_val = _default_for_type(py_type) if p.default is None else p.default
            param_defs[p.name] = (py_type, default_val)
        params_cls = create_model(f"{act.name}_Params", **param_defs)

        subject_cls, subject_id_param = _resolve_subject(act, param_defs)

        pre_roots = frozenset({"obj", "param", "actor"})
        act_param_names = set(param_defs.keys())
        for pre in act.preconditions:
            if pre.target_type not in created_classes:
                raise InvariantError(
                    f"precondition {pre.name!r} targets unknown object type {pre.target_type!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            if compiled_valid_roles:
                _validate_expr_closed_roles(
                    pre.when,
                    compiled_valid_roles,
                    context=f"action {act.name}.preconditions.{pre.name}.when",
                )
                _validate_expr_closed_roles(
                    pre.must_hold,
                    compiled_valid_roles,
                    context=f"action {act.name}.preconditions.{pre.name}.must_hold",
                )
            pre_ref_roots = {"obj": pre.target_type}
            if pre.when is not None:
                validate_predicate_ast(pre.when, allowed_roots=pre_roots, allow_view=True)
                _validate_expr_schema_refs(
                    pre.when, ot_by_name=ot_by_name, root_types=pre_ref_roots, param_names=act_param_names
                )
            validate_predicate_ast(pre.must_hold, allowed_roots=pre_roots, allow_view=True)
            _validate_expr_schema_refs(
                pre.must_hold, ot_by_name=ot_by_name, root_types=pre_ref_roots, param_names=act_param_names
            )

        for mut in act.mutations:
            if mut.object_type not in created_classes:
                raise InvariantError(
                    f"action {act.name!r} mutates unknown object type {mut.object_type!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            if mut.target_ref is not None:
                if mut.op == "create":
                    raise InvariantError(
                        f"action {act.name!r} create mutation cannot use target_ref",
                        code="INVALID_MUTATION_TARGET_REF",
                    )
                tref_parts = mut.target_ref.split(".")
                if tref_parts[0] not in ("obj", "param") or len(tref_parts) < 2:
                    raise InvariantError(
                        f"action {act.name!r} target_ref {mut.target_ref!r} must start with 'obj.' or 'param.'",
                        code="INVALID_MUTATION_TARGET_REF",
                    )
                for seg in tref_parts[1:]:
                    _assert_safe_identifier(seg, "target_ref segment")
                if tref_parts[0] == "obj" and subject_cls is None:
                    raise InvariantError(
                        f"action {act.name!r} mutation target_ref {mut.target_ref!r} requires a primary subject object",
                        code="UNAVAILABLE_REF_IN_CONTEXT",
                    )
                mut_roots = frozenset({"obj", "source_obj", "param", "actor"})
                mut_ref_roots: dict[str, str] = {"obj": mut.object_type}
                if subject_cls is not None:
                    mut_ref_roots["source_obj"] = subject_cls.__name__
                if tref_parts[0] == "obj" and subject_cls is not None:
                    _validate_expr_schema_refs(
                        {"ref": mut.target_ref},
                        ot_by_name=ot_by_name,
                        root_types={"obj": subject_cls.__name__},
                        param_names=act_param_names,
                    )
                elif tref_parts[0] == "param":
                    _validate_expr_schema_refs(
                        {"ref": mut.target_ref},
                        ot_by_name=ot_by_name,
                        root_types={},
                        param_names=act_param_names,
                    )
            elif mut.op == "create":
                mut_roots = frozenset({"param", "actor"})
                mut_ref_roots = {}
            else:
                mut_roots = frozenset({"obj", "param", "actor"})
                mut_ref_roots = {"obj": mut.object_type}
            for fv in mut.set_fields.values():
                if compiled_valid_roles:
                    _validate_expr_closed_roles(
                        fv,
                        compiled_valid_roles,
                        context=f"action {act.name}.mutations.{mut.object_type}",
                    )
                _validate_value_node(fv, allowed_roots=mut_roots, allow_view=True)
                _validate_expr_schema_refs(
                    fv, ot_by_name=ot_by_name, root_types=mut_ref_roots, param_names=act_param_names
                )

        def _make_action_handler(
            action_bp: ActionBlueprint,
            anchor_cls: type[ObjectType] | None,
            anchor_id_param: str | None,
        ) -> Callable[[EditBatch, BaseModel], None]:
            def _handler(ctx: EditBatch, params: BaseModel) -> None:
                # 1. Preconditions (P1-1 fix: require exact target IDs instead of silent full-table scan)
                for pre in action_bp.preconditions:
                    target_cls = created_classes[pre.target_type]
                    explicit_id_params: list[str] = []
                    if pre.id_param:
                        explicit_id_params.append(pre.id_param)
                    else:
                        for m in action_bp.mutations:
                            if m.object_type == pre.target_type and m.op != "create" and m.target_ref is None:
                                explicit_id_params.append(m.id_param)
                        if not explicit_id_params and hasattr(params, target_cls.__primary_key__):
                            explicit_id_params.append(target_cls.__primary_key__)

                    checked_objs: list[ObjectType] = []
                    if explicit_id_params:
                        for idp in explicit_id_params:
                            if not hasattr(params, idp):
                                raise InvariantError(
                                    f"precondition {pre.name!r} references missing param {idp!r}",
                                    code="MISSING_ID_PARAM",
                                )
                            cid = getattr(params, idp)
                            if cid is None:
                                raise InvariantError(
                                    f"precondition {pre.name!r} requires non-null {idp!r}",
                                    code="MISSING_ID_PARAM",
                                )
                            checked_objs.append(ctx.require(target_cls, str(cid)))
                    else:
                        # Only scan when no target ID parameter exists on this action
                        checked_objs = list(ctx.query(target_cls))

                    for obj in checked_objs:
                        eval_ctx = {"obj": obj, "param": params, "actor": ctx.actor}
                        if pre.when is not None and not eval_predicate(pre.when, eval_ctx, ctx):
                            continue
                        if not eval_predicate(pre.must_hold, eval_ctx, ctx):
                            msg = (
                                f"[{pre.code}] {pre.description}"
                                if pre.description
                                else f"[{pre.code}] precondition {pre.name} failed on "
                                f"{pre.target_type}:{obj.primary_key_value()}"
                            )
                            raise InvariantError(msg, code=pre.code)

                anchor_obj: ObjectType | None = None
                if anchor_cls is not None and anchor_id_param is not None and hasattr(params, anchor_id_param):
                    raw_anchor_id = getattr(params, anchor_id_param, None)
                    if raw_anchor_id is not None:
                        anchor_obj = ctx.require(anchor_cls, str(raw_anchor_id))

                # 2. Mutations
                for mut in action_bp.mutations:
                    target_cls = created_classes[mut.object_type]
                    if mut.op == "create":
                        eval_ctx = {"param": params, "actor": ctx.actor}
                        payload: dict[str, Any] = {}
                        for k, v_expr in mut.set_fields.items():
                            payload[k] = _eval_value(v_expr, eval_ctx, ctx)
                        if target_cls.__primary_key__ not in payload and hasattr(params, mut.id_param):
                            payload[target_cls.__primary_key__] = getattr(params, mut.id_param)
                        obj = target_cls(**payload)
                        ctx.create(obj)
                    elif mut.target_ref is not None:
                        ref_ctx: dict[str, Any] = {"param": params, "actor": ctx.actor}
                        if anchor_obj is not None:
                            ref_ctx["obj"] = anchor_obj
                        ref_val = _resolve_ref(mut.target_ref, ref_ctx, ctx)
                        if ref_val is None:
                            raise InvariantError(
                                f"action {action_bp.name!r} mutation target_ref {mut.target_ref!r} resolved to None",
                                code="MISSING_TARGET_REF",
                            )
                        target_id = (
                            ref_val.primary_key_value()
                            if isinstance(ref_val, ObjectType)
                            else str(ref_val)
                        )
                        existing = ctx.require(target_cls, target_id)
                        if mut.op == "delete":
                            ctx.delete(existing)
                        else:
                            eval_ctx = {"obj": existing, "param": params, "actor": ctx.actor}
                            if anchor_obj is not None:
                                eval_ctx["source_obj"] = anchor_obj
                            updates: dict[str, Any] = {}
                            for k, v_expr in mut.set_fields.items():
                                updates[k] = _eval_value(v_expr, eval_ctx, ctx)
                            ctx.modify(existing, **updates)
                    else:
                        if not hasattr(params, mut.id_param):
                            raise InvariantError(
                                f"action {action_bp.name!r} missing id_param {mut.id_param!r}",
                                code="MISSING_ID_PARAM",
                            )
                        target_id = str(getattr(params, mut.id_param))
                        existing = ctx.require(target_cls, target_id)
                        if mut.op == "delete":
                            ctx.delete(existing)
                        else:
                            eval_ctx = {"obj": existing, "param": params, "actor": ctx.actor}
                            updates = {}
                            for k, v_expr in mut.set_fields.items():
                                updates[k] = _eval_value(v_expr, eval_ctx, ctx)
                            ctx.modify(existing, **updates)

            return _handler

        touched_types = {mut.object_type for mut in act.mutations} | {
            pre.target_type for pre in act.preconditions
        }
        spec.action(
            act.name,
            params_cls,
            allowed_roles=tuple(act.allowed_roles),
            revert_roles=tuple(act.revert_roles),
            affects_source=act.affects_source,
            description=act.description,
            view_tags=tuple(act.view_tags),
            touched_object_types=tuple(sorted(touched_types)),
        )(_make_action_handler(act, subject_cls, subject_id_param))
        if act.display_label:
            action_display[act.name] = act.display_label

    valid_ot_names = set(spec.object_types.keys())
    valid_act_names = set(spec.actions.keys())
    valid_fn_names = set(spec.functions.keys())
    for view_bp in blueprint.views:
        _validate_scoped_view_bp(
            view_bp,
            valid_ot_names=valid_ot_names,
            valid_act_names=valid_act_names,
            valid_fn_names=valid_fn_names,
        )
        spec.register_view(
            ScopedViewSpec(
                name=view_bp.name,
                description=view_bp.description,
                roles=tuple(view_bp.roles),
                view_tags=tuple(view_bp.view_tags),
                object_types=tuple(view_bp.object_types),
                actions=tuple(view_bp.actions),
                functions=tuple(view_bp.functions),
                include_reachable_objects=view_bp.include_reachable_objects,
            )
        )

    spec.update_display(
        {
            "objects": {**object_display, **blueprint.display.get("objects", {})},
            "actions": {**action_display, **blueprint.display.get("actions", {})},
        }
    )
    return spec


def dsl_schema_prompt_guide(
    *,
    mode: Literal["genesis", "evolution"],
    seed_ontology: SeedOntology | None = None,
) -> str:
    """Compact JSON Schema & AST operator reference injected into live LLM prompts."""
    ast_guide = {
        "predicate_ops": {
            "logical": ["and (args:[...])", "or (args:[...])", "not (arg:{...})"],
            "comparison": ["eq", "ne", "lt", "le", "gt", "ge", "in", "not_in (left:{...}, right:{...})"],
            "quantifier": ["exists", "not_exists", "count_eq", "count_ge", "count_le (object_type, where, value?)"],
            "aggregate": ["sum", "min", "max (object_type, field, where?, default?)"],
            "arithmetic": ["add", "sub", "mul", "div (numeric left/right; zero divisor -> DIVISION_BY_ZERO)"],
            "conditional": ["if_else (cond:<predicate>, then:<value>, else:<value>)"],
            "values": [
                '{"ref": "obj.<field> | obj.<rel>.<field> | param.<field> | actor.role | target.<field> | source_obj.<field>"}',
                '{"lit": <scalar_or_list>}',
                '{"op": "sum|min|max", "object_type": "<Type>", "field": "<field>", "where": {...}}',
                '{"op": "if_else", "cond": {...}, "then": {...}, "else": {...}}',
            ],
        },
        "quantifier_binding_rule": (
            "Inside exists/not_exists/count_* and sum/min/max 'where' clauses, 'obj' refers to the OUTER object "
            "being checked, and 'target' refers to the CANDIDATE row of 'object_type' being scanned."
        ),
        "scope_rule": (
            "Global 'invariants' / 'add_invariants' run on all M0 records with ONLY 'obj' (no 'actor' or 'param'). "
            "Action 'preconditions' can access 'obj' (including relations obj.<rel>.<field>), 'param.*', "
            "'actor.role', and cross-object quantifiers/aggregates; encode over-limit, reversal, or human-approval "
            "gates as role-conditional action preconditions (e.g., when=<condition>, "
            "must_hold={'op': 'in', 'left': {'ref': 'actor.role'}, 'right': {'lit': ['steward', 'admin']}}, "
            "code='APPROVAL_REQUIRED_<REASON>') so unprivileged roles are cleanly rejected with APPROVAL_REQUIRED_* "
            "and escalated via M1 owned approval workflow objects before t1 re-execution. "
            "Mutations may use 'target_ref' (e.g. 'obj.customer') to update linked entities ('source_obj.*' refers to "
            "the primary anchor object). EvolutionPatch supports 'add_relations' and 'remove_fields' with reference checks."
        ),
        "eval_case_rule": (
            "Every entry in 'proposed_eval_cases' MUST conform strictly to ProposedEvalCase "
            "(fields: name, description, actor, action, params, setup_objects, expect). "
            "Populate 'setup_objects' with self-contained synthetic records (using synthetic IDs such as "
            "'EVAL-CUST-1', 'EVAL-ORD-1') so the case executes deterministically in Gate 4 without depending on "
            "ephemeral M0 rows. Gate 4 immediately executes every proposed_eval_case against your candidate blueprint "
            "before crystallization. In simulation, a triggered 'preconditions' rule (including 'APPROVAL_REQUIRED_*' "
            "gates for non-privileged roles) yields expect={'valid': false, 'error_code': '<code>'}, whereas an "
            "authorized actor or compliant record yields expect={'valid': true}."
        ),
    }
    if mode == "genesis":
        schema_bundle: dict[str, Any] = {
            "expected_root_keys": ["rationale", "blueprint", "adapter_mapping (required if external_profile provided)", "proposed_eval_cases"],
            "blueprint_schema": DeclarativeBlueprint.model_json_schema(),
            "proposed_eval_case_schema": ProposedEvalCase.model_json_schema(),
            "predicate_ast": ast_guide,
        }
    else:
        schema_bundle = {
            "expected_root_keys": ["patch"],
            "patch_schema": EvolutionPatch.model_json_schema(),
            "predicate_ast": ast_guide,
        }
    if seed_ontology is not None:
        schema_bundle["seed_ontology"] = seed_ontology.prompt_summary()
    return json.dumps(schema_bundle, ensure_ascii=False, separators=(",", ":"))
