"""L1 零 eval/exec 声明式本体蓝图、补丁与安全谓词编译器（纯 M2，领域无关）。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections import defaultdict
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from ..sandbox import EditBatch, InvariantView, ObjectDiff
from ..spec import (
    Actor,
    AutoID,
    InvariantError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)
from .seed import SeedOntology

__all__ = [
    "FieldSpec",
    "RelationSpec",
    "ObjectTypeBlueprint",
    "FieldMigrationSpec",
    "InvariantRuleSpec",
    "ActionMutationSpec",
    "ActionBlueprint",
    "DeclarativeBlueprint",
    "EvalActorSpec",
    "EvalExpectationSpec",
    "EvalSetupObjectSpec",
    "ProposedEvalCase",
    "EvolutionPatch",
    "blueprint_fingerprint",
    "count_ast_nodes",
    "expr_depth",
    "validate_predicate_ast",
    "eval_predicate",
    "apply_evolution_patch",
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
ARITHMETIC_OPS = frozenset({"add", "sub", "mul"})
ALLOWED_EXPR_OPS = COMPARISON_OPS | LOGICAL_OPS | QUANTIFIER_OPS | ARITHMETIC_OPS

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


_DEFAULT_STANDALONE_ARCHETYPES: frozenset[str] = frozenset(
    {
        "ObjectType",
        "MasterEntity",
        "BalanceLedger",
        "ProcessDocument",
        "EventVoucher",
    }
)


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
    display_label: str = ""


class FieldMigrationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: str
    op: Literal["add_default", "rename_field", "cast_type"] = "add_default"
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


class ActionMutationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create", "modify", "delete"] = "modify"
    object_type: str
    id_param: str = "id"
    set_fields: dict[str, Any] = Field(default_factory=dict)


class ActionBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    allowed_roles: list[str] = Field(default_factory=lambda: ["admin", "ai_agent"])
    approver_roles: list[str] = Field(default_factory=lambda: ["admin"])
    approval_rule: dict[str, Any] | None = None
    affects_source: bool = True
    params: list[FieldSpec] = Field(default_factory=list)
    preconditions: list[InvariantRuleSpec] = Field(default_factory=list)
    mutations: list[ActionMutationSpec] = Field(default_factory=list)
    display_label: str = ""


class DeclarativeBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    version: str = "1.0.0"
    seed_ontology_id: str | None = None
    description: str = ""
    object_types: list[ObjectTypeBlueprint] = Field(default_factory=list)
    actions: list[ActionBlueprint] = Field(default_factory=list)
    invariants: list[InvariantRuleSpec] = Field(default_factory=list)
    migrations: list[FieldMigrationSpec] = Field(default_factory=list)
    display: dict[str, dict[str, str]] = Field(default_factory=dict)


class EvalActorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = "eval:actor"
    role: str = "ai_agent"
    clearance_tags: list[str] = Field(default_factory=lambda: ["*"])


class EvalExpectationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    requires_approval: bool | None = None
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
            and any(
                k in data
                for k in ("expect_valid", "expect_error_code", "expect_requires_approval")
            )
        ):
            copied = dict(data)
            expect: dict[str, Any] = {}
            if "expect_valid" in copied:
                expect["valid"] = copied.pop("expect_valid")
            if "expect_error_code" in copied:
                expect["error_code"] = copied.pop("expect_error_code")
            if "expect_requires_approval" in copied:
                expect["requires_approval"] = copied.pop("expect_requires_approval")
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
    update_transitions: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    add_actions: list[ActionBlueprint] = Field(default_factory=list)
    update_actions: list[ActionBlueprint] = Field(default_factory=list)
    add_invariants: list[InvariantRuleSpec] = Field(default_factory=list)
    remove_invariants: list[str] = Field(default_factory=list)
    update_approval_rules: dict[str, dict[str, Any]] = Field(default_factory=dict)
    update_approver_roles: dict[str, list[str]] = Field(default_factory=dict)
    migrations: list[FieldMigrationSpec] = Field(default_factory=list)
    proposed_eval_cases: list[ProposedEvalCase] = Field(default_factory=list)


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


def validate_predicate_ast(expr: Any, *, max_depth: int = 12) -> int:
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
    _validate_expr_node(expr, expect_bool=True)
    return depth


def _validate_value_node(node: Any) -> None:
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
        _validate_value_node(node["lit"])
        return
    if set(node.keys()) == {"ref"}:
        ref = node["ref"]
        if not isinstance(ref, str) or not ref:
            raise InvariantError("ref must be a non-empty string", code="INVALID_PREDICATE_AST")
        parts = ref.split(".")
        if parts[0] not in ("obj", "param", "actor", "before", "after", "target"):
            raise InvariantError(
                f"unsupported ref root {parts[0]!r}",
                code="INVALID_PREDICATE_AST",
            )
        for seg in parts[1:]:
            _assert_safe_identifier(seg, "ref path segment")
        return
    op = node.get("op")
    if op in ARITHMETIC_OPS:
        if "left" not in node or "right" not in node:
            raise InvariantError(
                f"arithmetic op {op!r} requires 'left' and 'right'",
                code="INVALID_PREDICATE_AST",
            )
        _validate_value_node(node["left"])
        _validate_value_node(node["right"])
        return
    raise InvariantError(
        f"disallowed value AST keys {sorted(node.keys())}",
        code="UNSAFE_EXPRESSION",
    )


def _validate_expr_node(node: Any, *, expect_bool: bool = True) -> None:
    if not isinstance(node, dict):
        raise InvariantError("predicate node must be a JSON object", code="INVALID_PREDICATE_AST")
    op = node.get("op")
    if op not in ALLOWED_EXPR_OPS:
        raise InvariantError(
            f"disallowed predicate operator {op!r}",
            code="UNSAFE_EXPRESSION",
        )
    if op in ("and", "or"):
        args = node.get("args")
        if not isinstance(args, list) or not args:
            raise InvariantError(f"{op!r} requires non-empty 'args' list", code="INVALID_PREDICATE_AST")
        for sub in args:
            _validate_expr_node(sub, expect_bool=True)
        return
    if op == "not":
        if "arg" not in node:
            raise InvariantError("'not' requires 'arg'", code="INVALID_PREDICATE_AST")
        _validate_expr_node(node["arg"], expect_bool=True)
        return
    if op in COMPARISON_OPS:
        if "left" not in node or "right" not in node:
            raise InvariantError(
                f"comparison {op!r} requires 'left' and 'right'",
                code="INVALID_PREDICATE_AST",
            )
        _validate_value_node(node["left"])
        _validate_value_node(node["right"])
        return
    if op in QUANTIFIER_OPS:
        obj_type = node.get("object_type")
        _assert_safe_identifier(str(obj_type or ""), "quantifier object_type")
        if "where" in node and node["where"] is not None:
            _validate_expr_node(node["where"], expect_bool=True)
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
    current = context.get(root_name)
    tail = parts[1:]
    for idx, seg in enumerate(tail):
        if current is None:
            return None
        if isinstance(current, ObjectType):
            prop_map = getattr(type(current), "__object_properties__", None) or {}
            has_more = idx < len(tail) - 1
            if seg in prop_map and view is not None and has_more:
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
            if where is None or eval_predicate(where, sub_ctx, view):
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
        existing_names = {f.name for f in ot_by_name[ot_name].fields}
        for nf in new_fields:
            if nf.name in existing_names:
                raise InvariantError(
                    f"field {nf.name!r} already exists on {ot_name!r}",
                    code="DUPLICATE_FIELD",
                )
            ot_by_name[ot_name].fields.append(copy.deepcopy(nf))
            existing_names.add(nf.name)

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

    for act_name, rule_expr in patch.update_approval_rules.items():
        if act_name not in act_by_name:
            raise InvariantError(
                f"patch sets approval_rule on unknown action {act_name!r}",
                code="UNKNOWN_ACTION",
            )
        candidate.actions[act_by_name[act_name]].approval_rule = copy.deepcopy(rule_expr)

    for act_name, roles in patch.update_approver_roles.items():
        if act_name not in act_by_name:
            raise InvariantError(
                f"patch sets approver_roles on unknown action {act_name!r}",
                code="UNKNOWN_ACTION",
            )
        candidate.actions[act_by_name[act_name]].approver_roles = list(roles)

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
    return candidate


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
            agg_root = bool(getattr(base_cls, "__aggregate_root__", False))
        else:
            agg_root = ot.archetype == "MasterEntity"

        if ot.append_only is not None:
            append_only = ot.append_only
        elif seed_ontology is not None:
            append_only = bool(getattr(base_cls, "__append_only__", False))
        else:
            append_only = ot.archetype == "EventVoucher"

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

        cls = create_model(ot.name, __base__=base_cls, **field_defs)
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

    if non_neg_map and "business_non_negative_fields" not in spec.invariants:

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

    # Only run fallback declarative FSM check on types not already covered by an inherited ProcessDocument axiom
    fallback_fsm_specs = dict(fsm_specs)
    if "process_document_state_transitions" in spec.transition_invariants and "ProcessDocument" in spec.abstract_types:
        pd_cls = spec.abstract_types["ProcessDocument"]
        fallback_fsm_specs = {
            tname: fspec
            for tname, fspec in fsm_specs.items()
            if not issubclass(created_classes[tname], pd_cls)
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
    fallback_voucher_rules = dict(voucher_rules_map)
    if "voucher_backed_balance_conservation" in spec.transition_invariants and "BalanceLedger" in spec.abstract_types:
        bl_cls = spec.abstract_types["BalanceLedger"]
        fallback_voucher_rules = {
            tname: rules
            for tname, rules in voucher_rules_map.items()
            if not issubclass(created_classes[tname], bl_cls)
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

    for inv in blueprint.invariants:
        _assert_safe_identifier(inv.name, "invariant name")
        if inv.target_type not in created_classes:
            raise InvariantError(
                f"invariant {inv.name!r} targets unknown object type {inv.target_type!r}",
                code="UNKNOWN_OBJECT_TYPE",
            )
        if inv.when is not None:
            validate_predicate_ast(inv.when)
        validate_predicate_ast(inv.must_hold)

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

        for pre in act.preconditions:
            if pre.target_type not in created_classes:
                raise InvariantError(
                    f"precondition {pre.name!r} targets unknown object type {pre.target_type!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            if pre.when is not None:
                validate_predicate_ast(pre.when)
            validate_predicate_ast(pre.must_hold)

        for mut in act.mutations:
            if mut.object_type not in created_classes:
                raise InvariantError(
                    f"action {act.name!r} mutates unknown object type {mut.object_type!r}",
                    code="UNKNOWN_OBJECT_TYPE",
                )
            for fv in mut.set_fields.values():
                _validate_value_node(fv)

        approval_fn: Callable[[BaseModel, Actor], bool] | None = None
        if act.approval_rule is not None:
            validate_predicate_ast(act.approval_rule)
            rule_ast = copy.deepcopy(act.approval_rule)

            def _make_approval(ast_node: dict[str, Any]) -> Callable[[BaseModel, Actor], bool]:
                def _needs_approval(params: BaseModel, actor: Actor) -> bool:
                    return eval_predicate(ast_node, {"param": params, "actor": actor}, None)

                return _needs_approval

            approval_fn = _make_approval(rule_ast)

        def _make_action_handler(action_bp: ActionBlueprint) -> Callable[[EditBatch, BaseModel], None]:
            def _handler(ctx: EditBatch, params: BaseModel) -> None:
                # 1. Preconditions (P1-1 fix: require exact target IDs instead of silent full-table scan)
                for pre in action_bp.preconditions:
                    target_cls = created_classes[pre.target_type]
                    explicit_id_params: list[str] = []
                    if pre.id_param:
                        explicit_id_params.append(pre.id_param)
                    else:
                        for m in action_bp.mutations:
                            if m.object_type == pre.target_type and m.op != "create":
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
                            updates: dict[str, Any] = {}
                            for k, v_expr in mut.set_fields.items():
                                updates[k] = _eval_value(v_expr, eval_ctx, ctx)
                            ctx.modify(existing, **updates)

            return _handler

        spec.action(
            act.name,
            params_cls,
            allowed_roles=tuple(act.allowed_roles),
            approver_roles=tuple(act.approver_roles),
            approval_rule=approval_fn,
            affects_source=act.affects_source,
            description=act.description,
        )(_make_action_handler(act))
        if act.display_label:
            action_display[act.name] = act.display_label

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
            "arithmetic": ["add", "sub", "mul (numeric left/right only)"],
            "values": ['{"ref": "obj.<field> | obj.<rel>.<field> | param.<field> | actor.role | target.<field>"}', '{"lit": <scalar_or_list>}'],
        },
        "quantifier_binding_rule": (
            "Inside exists/not_exists/count_* 'where' clauses, 'obj' refers to the OUTER object being checked, "
            "and 'target' refers to the CANDIDATE row of 'object_type' being scanned."
        ),
        "scope_rule": (
            "Global 'invariants' / 'add_invariants' run on all M0 records with ONLY 'obj' (no 'actor' or 'param'). "
            "Action-level guards referencing 'actor.role' or 'param.*' MUST be placed in "
            "'update_actions[].preconditions' (preserving all existing action preconditions and mutations)."
        ),
        "eval_case_rule": (
            "Every entry in 'proposed_eval_cases' MUST conform strictly to ProposedEvalCase "
            "(fields: name, description, actor, action, params, setup_objects, expect). "
            "Populate 'setup_objects' with self-contained synthetic records (using synthetic IDs such as "
            "'EVAL-CUST-1', 'EVAL-ORD-1') so the case executes deterministically in Gate 4 without depending on "
            "ephemeral M0 rows. Gate 4 immediately executes every proposed_eval_case against your candidate blueprint "
            "before crystallization."
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
