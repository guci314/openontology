"""Optional Z3-backed SMT formal verifier for declarative blueprints (Pure M2, domain-agnostic).

Lazily imports ``z3`` only inside proof calls so core ``open_ontology`` and
``open_ontology.induction`` operate with zero external SMT dependency until
``prove_*`` routines are invoked. Explicitly classifies every check by logical
fragment (``EXACT_QF_LIA``, ``EXACT_QF_LRA``, ``QF_NIA_HEURISTIC``,
``BMC_BOUNDED(k=...)``, ``UNKNOWN``) and extracts minimal UNSAT cores and
concrete counterexamples for deadlocks, redundant guards, invariant violations,
and standard-model upgrade regressions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import itertools
import time
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..spec import InvariantError, OntologySpec, OpenOntologyError
from .dsl import (
    AGGREGATE_OPS,
    ARITHMETIC_OPS,
    COMPARISON_OPS,
    QUANTIFIER_OPS,
    ActionBlueprint,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    EvolutionPatch,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    _effective_relation_bounds,
    _relation_cardinality_issue,
    apply_overlay,
    blueprint_fingerprint,
    export_spec_blueprint,
)

PROVER_UNAVAILABLE = "PROVER_UNAVAILABLE"

__all__ = [
    "PROVER_UNAVAILABLE",
    "ProverUnavailableError",
    "LogicFragment",
    "ClassSatisfiabilityProof",
    "ConditionalSliceResult",
    "ActionReachabilityProof",
    "RedundantRuleFinding",
    "InvariantCounterexample",
    "InvariantPreservationProof",
    "WhenBranchProof",
    "WorkflowLivenessProof",
    "UpgradeRegressionItem",
    "UpgradeCompatibilityReport",
    "ProofReport",
    "prove_class_satisfiability",
    "prove_action_reachability",
    "prove_redundant_rules",
    "prove_invariant_preservation",
    "prove_when_branches",
    "prove_workflow_liveness",
    "prove_upgrade_compatibility",
    "prove_blueprint",
]

LogicFragment = Literal[
    "EXACT_QF_LIA",
    "EXACT_QF_LRA",
    "EXACT_GRAPH",
    "QF_NIA_HEURISTIC",
    "UNKNOWN",
] | str

_NULL_SENTINEL = "__oo_null__"
_OTHER_PREFIX = "__oo_other__"


class ProverUnavailableError(OpenOntologyError):
    """Raised when Z3 solver bindings are not installed in the active Python env."""

    def __init__(self, message: str | None = None) -> None:
        self.code = PROVER_UNAVAILABLE
        super().__init__(
            message
            or (
                "Z3 solver ('z3-solver') is not installed. Install optional extra with "
                "`pip install 'z3-solver>=4.13'` or `pip install '.[prove]'` to run formal proofs."
            )
        )


def _require_z3() -> Any:
    try:
        import z3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProverUnavailableError() from exc
    if z3 is None:
        raise ProverUnavailableError()
    return z3


# ---------------------------------------------------------------------------
# Structured Verification Report Schemas (Pure M2)
# ---------------------------------------------------------------------------
class ConditionalSliceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slice_name: str
    slice_kind: Literal["rule_when", "joint_when", "custom_slice"]
    status: Literal["SAT", "CONDITIONAL_DEADLOCK", "UNKNOWN"]
    unsat_core: list[str] = Field(default_factory=list)
    logic_fragment: str = "EXACT_QF_LIA"
    elapsed_ms: float = 0.0


class ActionReachabilityProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_name: str
    implementation_kind: Literal["rule", "edit_function"]
    status: Literal["REACHABLE", "TOTAL_DEADLOCK", "CONDITIONAL_DEADLOCK", "UNKNOWN"]
    rule_count: int = 0
    unsat_core: list[str] = Field(default_factory=list)
    conditional_slices: list[ConditionalSliceResult] = Field(default_factory=list)
    deadlocked_slices: list[ConditionalSliceResult] = Field(default_factory=list)
    witness: dict[str, Any] | None = None
    logic_fragment: str = "EXACT_QF_LIA"
    elapsed_ms: float = 0.0


class RedundantRuleFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_name: str
    rule_name: str
    rule_code: str = ""
    status: Literal["REDUNDANT_RULE"] = "REDUNDANT_RULE"
    reason: Literal[
        "IMPLIED_BY_OTHER_RULES",
        "INTRINSIC_TAUTOLOGY",
        "VACUOUS_WHEN_GUARD",
    ]
    entailed_by: list[str] = Field(default_factory=list)
    logic_fragment: str = "EXACT_QF_LIA"
    elapsed_ms: float = 0.0


class InvariantCounterexample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violated_invariant: str
    violated_code: str = ""
    actor: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, dict[str, Any]] = Field(default_factory=dict)
    after: dict[str, dict[str, Any]] = Field(default_factory=dict)


class InvariantPreservationProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_name: str
    status: Literal[
        "PROVED",
        "CONTRACT_PROVED",
        "VIOLATED",
        "VACUOUS_CONTRACT",
        "VACUOUS_DEADLOCK",
        "SKIPPED_UNCONTRACTED",
        "UNKNOWN",
    ]
    proof_kind: Literal["exact_wp", "contract_assume_guarantee", "skipped"] = "exact_wp"
    checked_invariants: list[str] = Field(default_factory=list)
    violated_invariants: list[str] = Field(default_factory=list)
    unsat_core: list[str] = Field(default_factory=list)
    detail: str = ""
    counterexamples: list[InvariantCounterexample] = Field(default_factory=list)
    logic_fragment: str = "EXACT_QF_LIA"
    elapsed_ms: float = 0.0


class UpgradeRegressionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_name: str
    regression_kind: Literal[
        "NEW_TOTAL_DEADLOCK",
        "NEW_CONDITIONAL_DEADLOCK",
        "NEW_INVARIANT_VIOLATION",
        "NEW_VACUOUS_CONTRACT",
        "STRUCTURAL_INCOMPATIBILITY",
    ]
    slice_name: str | None = None
    v1_status: str = ""
    v2_status: str = ""
    unsat_core: list[str] = Field(default_factory=list)
    detail: str = ""


class UpgradeCompatibilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_v1_id: str
    base_v2_id: str
    compatible: bool
    structural_errors: list[str] = Field(default_factory=list)
    regressions: list[UpgradeRegressionItem] = Field(default_factory=list)
    v1_summary: dict[str, Any] = Field(default_factory=dict)
    v2_summary: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: float = 0.0


class ClassSatisfiabilityProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: str
    extends: str | None = None
    status: Literal[
        "SATISFIABLE",
        "UNSATISFIABLE_CLASS",
        "DL_AXIOM_CONFLICT",
        "LSP_VIOLATION",
        "UNKNOWN",
    ]
    logic_fragment: str = "EXACT_QF_LIA"
    inherited_invariants: list[str] = Field(default_factory=list)
    own_invariants: list[str] = Field(default_factory=list)
    unsat_core: list[str] = Field(default_factory=list)
    detail: str = ""
    witness: dict[str, Any] | None = None
    elapsed_ms: float = 0.0


class WhenBranchProof(BaseModel):
    """PO-6: Guarded `when` branch reachability, exhaustiveness, and pairwise disjointness proof."""

    model_config = ConfigDict(extra="forbid")

    action_name: str
    status: Literal[
        "EXHAUSTIVE",
        "CONDITIONAL_SIDE_EFFECT",
        "INCOMPLETE_WHEN_BRANCHES",
        "DEAD_BRANCH",
        "OVERLAPPING_WHEN_BRANCHES",
        "UNKNOWN",
    ]
    branch_count: int = 0
    dead_branch_indices: list[int] = Field(default_factory=list)
    overlapping_pairs: list[tuple[int, int]] = Field(default_factory=list)
    uncovered_witness: dict[str, Any] | None = None
    overlap_witness: dict[str, Any] | None = None
    detail: str = ""
    logic_fragment: str = "EXACT_QF_LIA"
    elapsed_ms: float = 0.0


class WorkflowLivenessProof(BaseModel):
    """PO-7: FSM structural liveness + bounded Z3 multi-step action terminal reachability proof."""

    model_config = ConfigDict(extra="forbid")

    object_type: str
    extends: str | None = None
    status_field: str = "status"
    status: Literal[
        "LIVE",
        "GRAPH_LIVE_PARTIAL_ACTIONS",
        "NO_INITIAL_STATE",
        "UNREACHABLE_STATE",
        "NO_TERMINAL_STATE",
        "TRAP_STATE",
        "ACTION_CHAIN_DEADLOCKED",
        "UNKNOWN",
    ]
    action_terminal_status: Literal[
        "TRIVIAL_TERMINAL",
        "ACTION_TERMINAL_REACHABLE",
        "PARTIAL_ACTION_COVERAGE",
        "ACTION_CHAIN_DEADLOCKED",
        "SKIPPED_STRUCTURAL_FAILURE",
        "UNKNOWN",
    ] = "PARTIAL_ACTION_COVERAGE"
    initial_statuses: list[str] = Field(default_factory=list)
    reachable_states: list[str] = Field(default_factory=list)
    terminal_states: list[str] = Field(default_factory=list)
    unreachable_states: list[str] = Field(default_factory=list)
    pruned_inherited_states: list[str] = Field(default_factory=list)
    trap_states: list[str] = Field(default_factory=list)
    missing_action_edges: list[str] = Field(default_factory=list)
    terminal_witness_trace: list[str] = Field(default_factory=list)
    reached_terminal_status: str | None = None
    max_steps: int = 4
    detail: str = ""
    logic_fragment: str = "EXACT_GRAPH"
    elapsed_ms: float = 0.0


class ProofReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    model_id: str
    version: str
    blueprint_fingerprint: str
    bmc_bound: int = 4
    total_actions_checked: int = 0
    total_rules_checked: int = 0
    total_deadlock_count: int = 0
    conditional_deadlock_count: int = 0
    invariant_violation_count: int = 0
    redundant_rule_count: int = 0
    dead_class_count: int = 0
    dl_conflict_count: int = 0
    lsp_violation_count: int = 0
    exact_wp_proved_count: int = 0
    contract_proved_count: int = 0
    vacuous_contract_count: int = 0
    uncontracted_edit_function_count: int = 0
    when_branch_checked_count: int = 0
    when_branch_issue_count: int = 0
    workflow_checked_count: int = 0
    workflow_live_count: int = 0
    workflow_action_verified_count: int = 0
    workflow_issue_count: int = 0
    logic_fragment_counts: dict[str, int] = Field(default_factory=dict)
    class_proofs: list[ClassSatisfiabilityProof] = Field(default_factory=list)
    reachability: list[ActionReachabilityProof] = Field(default_factory=list)
    redundant_rules: list[RedundantRuleFinding] = Field(default_factory=list)
    invariant_preservation: list[InvariantPreservationProof] = Field(default_factory=list)
    when_branch_proofs: list[WhenBranchProof] = Field(default_factory=list)
    workflow_liveness_proofs: list[WorkflowLivenessProof] = Field(default_factory=list)
    upgrade_compatibility: UpgradeCompatibilityReport | None = None
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Input Coercion & Blueprint Resolution
# ---------------------------------------------------------------------------
def _to_blueprint(
    target: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    default_model_id: str = "model_under_proof",
) -> DeclarativeBlueprint:
    if isinstance(target, DeclarativeBlueprint):
        return copy.deepcopy(target)
    if isinstance(target, OntologySpec):
        ont_map = target.display.get("ontologies") or {}
        mid = str(list(ont_map.keys())[-1]) if ont_map else default_model_id
        return export_spec_blueprint(target, model_id=mid)
    if isinstance(target, Mapping):
        return DeclarativeBlueprint.model_validate(target)
    raise InvariantError(
        f"expected DeclarativeBlueprint, OntologySpec, or mapping, got {type(target).__name__}",
        code="INVALID_BLUEPRINT",
    )


def _normalize_overlays(
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None,
) -> list[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]:
    if overlay is None:
        return []
    if isinstance(overlay, (CustomerOverlaySpec, EvolutionPatch, Mapping)):
        return [overlay]
    if isinstance(overlay, Sequence):
        return list(overlay)
    raise InvariantError(
        f"unsupported overlay argument type: {type(overlay).__name__}",
        code="OVERLAY_BREAKS_STANDARD_CONTRACT",
    )


_STRUCTURAL_PROOF_CODES = frozenset(
    {
        "LSP_VIOLATION",
        "INHERITANCE_CYCLE",
        "DL_DISJOINT_VIOLATION",
        "DL_CARDINALITY_CONFLICT",
    }
)


def _ancestor_closure(ot_name: str, ancestors_map: Mapping[str, Sequence[str]]) -> set[str]:
    return {ot_name, *ancestors_map.get(ot_name, ())}


def _merge_structural_overlay_tolerantly(
    eff: DeclarativeBlueprint,
    ov_raw: CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any],
) -> DeclarativeBlueprint:
    """Layer overlay additives onto a copy without throwing on LSP/DL/cycle axioms."""
    from .dsl import RelationSpec, _coerce_overlay

    ov = _coerce_overlay(ov_raw)
    out = copy.deepcopy(eff)
    if ov.add_roles:
        for r in ov.add_roles:
            if r not in out.roles:
                out.roles.append(r)
    ot_map = {ot.name: ot for ot in out.object_types}
    for nt in (*ov.add_object_types, *ov.derive_object_types):
        if nt.name not in ot_map:
            cloned = copy.deepcopy(nt)
            out.object_types.append(cloned)
            ot_map[cloned.name] = cloned
    for pair in ov.add_disjoint_types:
        if pair not in out.disjoint_types:
            out.disjoint_types.append(tuple(pair))
    for ot_n, flist in ov.add_fields.items():
        if ot_n in ot_map:
            existing_f = {f.name for f in ot_map[ot_n].fields}
            for f in flist:
                if f.name not in existing_f:
                    ot_map[ot_n].fields.append(copy.deepcopy(f))
    for ot_n, rlist in ov.add_relations.items():
        if ot_n in ot_map:
            existing_r = {r.field for r in ot_map[ot_n].relations}
            for r in rlist:
                if isinstance(r, RelationSpec) and r.field not in existing_r:
                    ot_map[ot_n].relations.append(copy.deepcopy(r))
    out.invariants.extend(copy.deepcopy(ov.add_invariants))
    out.actions.extend(copy.deepcopy(ov.add_actions))
    for act_n, extra_pres in ov.add_action_preconditions.items():
        for a in out.actions:
            if a.name == act_n:
                a.preconditions.extend(copy.deepcopy(extra_pres))
                break
    return out


def _resolve_effective_blueprint(
    target: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    allow_structural_errors: bool = False,
) -> DeclarativeBlueprint:
    eff = _to_blueprint(target)
    for ov in _normalize_overlays(overlay):
        try:
            eff = apply_overlay(eff, ov)
        except InvariantError as exc:
            if allow_structural_errors and getattr(exc, "code", None) in _STRUCTURAL_PROOF_CODES:
                eff = _merge_structural_overlay_tolerantly(eff, ov)
            else:
                raise
    return eff


def _prepare_blueprint_for_prover(
    bp: DeclarativeBlueprint,
) -> tuple[
    DeclarativeBlueprint,
    dict[str, tuple[str, ...]],
    dict[str, tuple[Literal["DL_AXIOM_CONFLICT", "LSP_VIOLATION"], list[str], str]],
]:
    """Expand monotonic ``extends`` inheritance & collect structural LSP/DL conflicts."""
    from .dsl import RelationSpec

    raw_by_name = {ot.name: ot for ot in bp.object_types}
    ancestors_map: dict[str, tuple[str, ...]] = {}
    issues: dict[str, tuple[Literal["DL_AXIOM_CONFLICT", "LSP_VIOLATION"], list[str], str]] = {}
    topo_valid: list[str] = []

    # 1. Cycles & missing parents per node
    visited_state: dict[str, int] = {}

    def _walk(name: str, trail: list[str]) -> bool:
        st = visited_state.get(name, 0)
        if st == 2:
            return name not in issues
        if st == 1:
            cyc = " -> ".join([*trail, name])
            for node_n in trail[trail.index(name) :] if name in trail else trail:
                issues[node_n] = (
                    "LSP_VIOLATION",
                    [f"cycle:{cyc}"],
                    f"Inheritance cycle detected: {cyc}",
                )
            return False
        visited_state[name] = 1
        ot = raw_by_name[name]
        parent_n = ot.extends
        ok_chain = True
        if parent_n is not None:
            if parent_n == name:
                issues[name] = (
                    "LSP_VIOLATION",
                    [f"cycle:{name}->{name}"],
                    f"Self-inheritance cycle detected on {name!r}",
                )
                ok_chain = False
            elif parent_n not in raw_by_name:
                issues[name] = (
                    "LSP_VIOLATION",
                    [f"unknown_parent:{parent_n}"],
                    f"ObjectType {name!r} extends unknown ObjectType {parent_n!r}",
                )
                ok_chain = False
            else:
                ok_chain = _walk(parent_n, [*trail, name])
                if not ok_chain and name not in issues:
                    issues[name] = (
                        "LSP_VIOLATION",
                        [f"ancestor_invalid:{parent_n}"],
                        f"ObjectType {name!r} inherits from invalid parent {parent_n!r}",
                    )
        visited_state[name] = 2
        if ok_chain:
            ancestors_map[name] = (
                (parent_n, *ancestors_map.get(parent_n, ())) if parent_n is not None else ()
            )
            topo_valid.append(name)
        else:
            ancestors_map.setdefault(name, ())
        return ok_chain

    for ot in bp.object_types:
        _walk(ot.name, [])

    # 2. Disjointness axioms & local relation cardinality checks
    for ot in bp.object_types:
        for rel in ot.relations:
            c_issue = _relation_cardinality_issue(ot.name, rel)
            if c_issue is not None and ot.name not in issues:
                issues[ot.name] = (
                    "DL_AXIOM_CONFLICT",
                    [f"cardinality:{ot.name}.{rel.field}"],
                    c_issue,
                )
        ancestors = {ot.name, *ancestors_map.get(ot.name, ())}
        for a, b in bp.disjoint_types:
            if (a == b and ot.name == a) or (a != b and a in ancestors and b in ancestors):
                if ot.name not in issues:
                    issues[ot.name] = (
                        "DL_AXIOM_CONFLICT",
                        [f"disjoint:{a}!={b}"],
                        f"Class {ot.name!r} violates disjointness axiom Disjoint({a!r}, {b!r})",
                    )

    # 3. Monotonic LSP checks & field/rel/FSM expansion in topological order
    resolved_map: dict[str, ObjectTypeBlueprint] = {}
    for name in topo_valid:
        raw_ot = raw_by_name[name]
        if raw_ot.exclude_fields and name not in issues:
            issues[name] = (
                "LSP_VIOLATION",
                [f"exclude_fields:{','.join(raw_ot.exclude_fields)}"],
                f"Subtype {name!r} excludes parent fields {raw_ot.exclude_fields}",
            )
        if raw_ot.extends is None:
            resolved_map[name] = copy.deepcopy(raw_ot)
            continue

        parent = resolved_map[raw_ot.extends]
        lsp_reasons: list[str] = []
        lsp_cores: list[str] = []
        sub_field_names = {sf.name for sf in raw_ot.fields}

        pk_explicit = (
            "primary_key" in raw_ot.model_fields_set
            and raw_ot.primary_key != parent.primary_key
            and (raw_ot.primary_key != "id" or "id" in sub_field_names)
        )
        if pk_explicit:
            lsp_cores.append(f"primary_key:{parent.primary_key}->{raw_ot.primary_key}")
            lsp_reasons.append(f"overrides primary_key {parent.primary_key!r} -> {raw_ot.primary_key!r}")
        for sf in raw_ot.fields:
            if sf.primary_key and sf.name != parent.primary_key:
                lsp_cores.append(f"primary_key:{parent.primary_key}->{sf.name}")
                lsp_reasons.append(f"redeclares primary_key field {sf.name!r}")

        status_explicit = (
            "status_field" in raw_ot.model_fields_set
            and raw_ot.status_field != parent.status_field
            and (raw_ot.status_field != "status" or "status" in sub_field_names)
        )
        if status_explicit:
            lsp_cores.append(f"status_field:{parent.status_field}->{raw_ot.status_field}")
            lsp_reasons.append(f"overrides status_field {parent.status_field!r} -> {raw_ot.status_field!r}")

        if parent.append_only is True and raw_ot.append_only is False:
            lsp_cores.append("append_only_dropped")
            lsp_reasons.append("drops append_only=True")

        p_fields = {f.name: f for f in parent.fields}
        merged_fields = [copy.deepcopy(f) for f in parent.fields]
        mf_idx = {f.name: idx for idx, f in enumerate(merged_fields)}
        for sf in raw_ot.fields:
            if sf.name in p_fields:
                pf = p_fields[sf.name]
                if sf.field_type != pf.field_type:
                    lsp_cores.append(f"field_type:{sf.name}")
                    lsp_reasons.append(f"re-types {sf.name!r} {pf.field_type} -> {sf.field_type}")
                if pf.required and not sf.required and "required" in sf.model_fields_set:
                    lsp_cores.append(f"field_required:{sf.name}")
                    lsp_reasons.append(f"drops required on {sf.name!r}")
                if pf.non_negative and not sf.non_negative and "non_negative" in sf.model_fields_set:
                    lsp_cores.append(f"field_non_negative:{sf.name}")
                    lsp_reasons.append(f"drops non_negative on {sf.name!r}")
                merged_fields[mf_idx[sf.name]] = FieldSpec(
                    name=pf.name,
                    field_type=pf.field_type,
                    default=sf.default if "default" in sf.model_fields_set else pf.default,
                    required=bool(pf.required or sf.required),
                    primary_key=bool(pf.primary_key),
                    auto_id=bool(pf.auto_id or sf.auto_id),
                    owned=bool(pf.owned or sf.owned),
                    masked_roles=tuple(dict.fromkeys([*pf.masked_roles, *sf.masked_roles])),
                    non_negative=bool(pf.non_negative or sf.non_negative),
                    description=sf.description or pf.description,
                )
            else:
                mf_idx[sf.name] = len(merged_fields)
                merged_fields.append(copy.deepcopy(sf))

        p_rels = {r.field: r for r in parent.relations}
        merged_rels = [copy.deepcopy(r) for r in parent.relations]
        mr_idx = {r.field: idx for idx, r in enumerate(merged_rels)}
        for sr in raw_ot.relations:
            if sr.field in p_rels:
                pr = p_rels[sr.field]
                if not pr.multiple and sr.multiple:
                    lsp_cores.append(f"relation_multiple:{sr.field}")
                    lsp_reasons.append(f"relaxes {sr.field!r} to multiple=True")
                target_ok = (
                    sr.target_type == pr.target_type
                    or pr.target_type in ancestors_map.get(sr.target_type, ())
                )
                if not target_ok:
                    lsp_cores.append(f"relation_target:{sr.field}")
                    lsp_reasons.append(f"widens {sr.field!r} target to {sr.target_type!r}")
                p_min, p_max = _effective_relation_bounds(pr)
                min_set = "min_cardinality" in sr.model_fields_set
                max_set = "max_cardinality" in sr.model_fields_set
                if pr.required and "required" in sr.model_fields_set and not sr.required:
                    lsp_cores.append(f"relation_required:{sr.field}")
                    lsp_reasons.append(f"drops required on relation {sr.field!r}")
                eff_sub_req = bool(pr.required or sr.required)
                eff_sub_min_raw = int(sr.min_cardinality) if min_set else int(pr.min_cardinality)
                eff_sub_min = max(eff_sub_min_raw, 1 if eff_sub_req else 0)
                if eff_sub_min < p_min:
                    lsp_cores.append(f"relation_min:{sr.field}")
                    lsp_reasons.append(f"weakens {sr.field!r} min_cardinality {p_min} -> {eff_sub_min}")
                if max_set:
                    eff_sub_max_raw = sr.max_cardinality
                    eff_sub_max = eff_sub_max_raw if sr.multiple else (
                        eff_sub_max_raw if eff_sub_max_raw is not None else 1
                    )
                else:
                    eff_sub_max_raw = pr.max_cardinality
                    eff_sub_max = eff_sub_max_raw if sr.multiple else (
                        min(eff_sub_max_raw, 1) if eff_sub_max_raw is not None else 1
                    )
                if p_max is not None and (eff_sub_max is None or eff_sub_max > p_max):
                    lsp_cores.append(f"relation_max:{sr.field}")
                    lsp_reasons.append(f"relaxes {sr.field!r} max_cardinality {p_max} -> {eff_sub_max}")
                merged_rel = RelationSpec(
                    field=sr.field,
                    target_type=sr.target_type,
                    multiple=sr.multiple if "multiple" in sr.model_fields_set else pr.multiple,
                    owned=bool(pr.owned or sr.owned),
                    required=eff_sub_req,
                    min_cardinality=max(eff_sub_min_raw, pr.min_cardinality),
                    max_cardinality=eff_sub_max_raw,
                    description=sr.description or pr.description,
                )
                c_issue = _relation_cardinality_issue(name, merged_rel)
                if c_issue is not None and name not in issues and not lsp_reasons:
                    issues[name] = (
                        "DL_AXIOM_CONFLICT",
                        [f"cardinality:{name}.{sr.field}"],
                        c_issue,
                    )
                merged_rels[mr_idx[sr.field]] = merged_rel
            else:
                mr_idx[sr.field] = len(merged_rels)
                merged_rels.append(copy.deepcopy(sr))

        if parent.initial_statuses is not None:
            if raw_ot.initial_statuses is None:
                resolved_init: list[str] | None = list(parent.initial_statuses)
            else:
                if not raw_ot.initial_statuses or not set(raw_ot.initial_statuses).issubset(
                    set(parent.initial_statuses)
                ):
                    lsp_cores.append("fsm_initial_statuses")
                    lsp_reasons.append(f"widens/empties initial_statuses {raw_ot.initial_statuses}")
                resolved_init = list(raw_ot.initial_statuses)
        else:
            resolved_init = copy.deepcopy(raw_ot.initial_statuses)

        if parent.transitions:
            if not raw_ot.transitions:
                resolved_trans = copy.deepcopy(parent.transitions)
            else:
                for src_s, dst_list in raw_ot.transitions.items():
                    if src_s not in parent.transitions:
                        lsp_cores.append(f"fsm_state:{src_s}")
                        lsp_reasons.append(f"introduces unknown FSM state {src_s!r}")
                    else:
                        extra = [d for d in dst_list if d not in set(parent.transitions[src_s])]
                        if extra:
                            lsp_cores.append(f"fsm_edge:{src_s}->{','.join(extra)}")
                            lsp_reasons.append(f"widens FSM transition {src_s!r} -> {extra}")
                resolved_trans = copy.deepcopy(raw_ot.transitions)
        else:
            resolved_trans = copy.deepcopy(raw_ot.transitions)

        if lsp_reasons and name not in issues:
            issues[name] = (
                "LSP_VIOLATION",
                lsp_cores,
                f"Subtype {name!r} violates parent {parent.name!r} behavioral contract: {'; '.join(lsp_reasons)}",
            )

        merged_vrules = dict(parent.voucher_delta_rules)
        for bk, bv in raw_ot.voucher_delta_rules.items():
            if bk in merged_vrules and tuple(merged_vrules[bk]) != tuple(bv) and name not in issues:
                issues[name] = (
                    "LSP_VIOLATION",
                    [f"voucher_delta:{bk}"],
                    f"Subtype {name!r} alters parent voucher_delta_rules[{bk!r}]",
                )
            merged_vrules[bk] = tuple(bv)

        resolved_map[name] = ObjectTypeBlueprint(
            name=name,
            extends=raw_ot.extends,
            archetype=raw_ot.archetype if ("archetype" in raw_ot.model_fields_set and raw_ot.archetype != "ObjectType") else parent.archetype,
            primary_key=parent.primary_key,
            title_field=raw_ot.title_field if raw_ot.title_field is not None else parent.title_field,
            aggregate_root=raw_ot.aggregate_root if raw_ot.aggregate_root is not None else parent.aggregate_root,
            append_only=raw_ot.append_only if raw_ot.append_only is not None else parent.append_only,
            owned=raw_ot.owned,
            fields=merged_fields,
            exclude_fields=[],
            relations=merged_rels,
            status_field=parent.status_field,
            initial_statuses=resolved_init,
            transitions=resolved_trans,
            voucher_delta_rules=merged_vrules,
            view_tags=list(dict.fromkeys([*parent.view_tags, *raw_ot.view_tags])),
            display_label=raw_ot.display_label or parent.display_label,
        )

    expanded = copy.deepcopy(bp)
    expanded.object_types = [
        resolved_map.get(ot.name, copy.deepcopy(ot)) for ot in bp.object_types
    ]
    return expanded, ancestors_map, issues


# ---------------------------------------------------------------------------
# AST Dependency & String Vocabulary Extraction
# ---------------------------------------------------------------------------
def _collect_expr_strings(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        if set(node.keys()) == {"lit"}:
            val = node["lit"]
            if isinstance(val, str):
                out.add(val)
            elif isinstance(val, list):
                for elem in val:
                    if isinstance(elem, str):
                        out.add(elem)
            return
        if set(node.keys()) == {"ref"}:
            return
        for k, v in node.items():
            if k != "lit":
                _collect_expr_strings(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_expr_strings(item, out)
    elif isinstance(node, str):
        out.add(node)


def _collect_expr_refs(node: Any, out: set[str], *, quant_types: set[str] | None = None) -> None:
    if isinstance(node, dict):
        if set(node.keys()) == {"lit"}:
            return
        if set(node.keys()) == {"ref"} and isinstance(node["ref"], str):
            out.add(node["ref"])
            return
        op = node.get("op")
        if (op in QUANTIFIER_OPS or op in AGGREGATE_OPS) and quant_types is not None:
            ot_name = str(node.get("object_type") or "")
            if ot_name:
                quant_types.add(ot_name)
        for k, v in node.items():
            if k != "lit":
                _collect_expr_refs(v, out, quant_types=quant_types)
    elif isinstance(node, list):
        for item in node:
            _collect_expr_refs(item, out, quant_types=quant_types)


def _action_symbol_keys(expr: Any) -> set[str]:
    """Return coarse variable dependency keys used to prune disjoint rule sets."""
    refs: set[str] = set()
    qtypes: set[str] = set()
    _collect_expr_refs(expr, refs, quant_types=qtypes)
    keys: set[str] = set()
    for ref in refs:
        parts = ref.split(".")
        if len(parts) >= 2:
            keys.add(f"{parts[0]}.{parts[1]}")
        else:
            keys.add(parts[0])
    for qt in qtypes:
        keys.add(f"quant:{qt}")
    return keys


class _StringCodec:
    """Finite string domain encoding preserving sound equality & sentinel witnesses."""

    def __init__(self, bp: DeclarativeBlueprint, extra_exprs: Sequence[Any] = ()) -> None:
        collected: set[str] = {""}
        collected.update(bp.roles)
        for ot in bp.object_types:
            if ot.initial_statuses:
                collected.update(ot.initial_statuses)
            for src_s, dst_list in ot.transitions.items():
                collected.add(src_s)
                collected.update(dst_list)
            for fld in ot.fields:
                if fld.field_type == "str" and isinstance(fld.default, str):
                    collected.add(fld.default)
        for act in bp.actions:
            collected.update(act.allowed_roles)
            collected.update(act.revert_roles)
            for param in act.params:
                if param.field_type == "str" and isinstance(param.default, str):
                    collected.add(param.default)
            for pre in act.preconditions:
                _collect_expr_strings(pre.when, collected)
                _collect_expr_strings(pre.must_hold, collected)
            for mut in act.mutations:
                if mut.when is not None:
                    _collect_expr_strings(mut.when, collected)
                for val_expr in mut.set_fields.values():
                    _collect_expr_strings(val_expr, collected)
            for ens in act.ensures:
                _collect_expr_strings(ens.when, collected)
                _collect_expr_strings(ens.must_hold, collected)
        for inv in bp.invariants:
            _collect_expr_strings(inv.when, collected)
            _collect_expr_strings(inv.must_hold, collected)
        for expr in extra_exprs:
            _collect_expr_strings(expr, collected)

        collected.discard(_NULL_SENTINEL)
        ordered = [_NULL_SENTINEL, "", *sorted(s for s in collected if s != "")]
        self.str_to_id: dict[str, int] = {s: idx for idx, s in enumerate(ordered)}
        self.id_to_str: list[str] = ordered
        self.null_id: int = 0
        self.known_limit: int = len(ordered)
        self.max_id: int = self.known_limit + 24

    def encode(self, val: str | None) -> int:
        if val is None:
            return self.null_id
        if val in self.str_to_id:
            return self.str_to_id[val]
        idx = len(self.id_to_str)
        self.str_to_id[val] = idx
        self.id_to_str.append(val)
        self.known_limit = len(self.id_to_str)
        self.max_id = max(self.max_id, self.known_limit + 24)
        return idx

    def decode(self, code_val: int) -> str | None:
        if code_val == self.null_id:
            return None
        if 0 <= code_val < len(self.id_to_str):
            return self.id_to_str[code_val]
        offset = code_val - len(self.id_to_str)
        return _OTHER_PREFIX if offset == 0 else f"{_OTHER_PREFIX}_{offset}"


# ---------------------------------------------------------------------------
# AST -> Z3 Symbolic Compiler
# ---------------------------------------------------------------------------
@dataclass
class _SymVal:
    expr: Any
    kind: Literal["int", "float", "bool", "str", "null"]
    is_null: Any = False


@dataclass
class _SymEntity:
    ot: ObjectTypeBlueprint
    tag: str
    fields: dict[str, _SymVal] = field(default_factory=dict)
    linked: dict[str, "_SymEntity"] = field(default_factory=dict)


@dataclass
class _SymRow:
    present: Any
    entity: _SymEntity


class _Z3Compiler:
    """Translates validated DSL expressions and mutations into Z3 constraints."""

    def __init__(
        self,
        z3_mod: Any,
        bp: DeclarativeBlueprint,
        codec: _StringCodec,
        *,
        bmc_bound: int = 4,
    ) -> None:
        self.z3 = z3_mod
        self.bp = bp
        self.ot_by_name: dict[str, ObjectTypeBlueprint] = {
            ot.name: ot for ot in bp.object_types
        }
        self.codec = codec
        self.bmc_bound = max(1, int(bmc_bound))

        self._counter = 0
        self.background: list[Any] = []
        self.named_entities: dict[str, list[_SymEntity]] = {}
        self.bmc_tables: dict[str, list[_SymRow]] = {}
        self.bmc_linked_tags: set[tuple[str, str]] = set()
        self.used_real = False
        self.used_nonlinear = False
        self.used_bmc = False

    def next_name(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter}"

    def make_scalar_var(
        self,
        name: str,
        field_type: str,
        *,
        non_negative: bool = False,
        nullable: bool = False,
        allowed_strings: Sequence[str] | None = None,
        add_domain_axioms: bool = True,
    ) -> _SymVal:
        z3 = self.z3
        vname = self.next_name(name)
        if field_type == "int":
            sym = z3.Int(vname)
            if add_domain_axioms and non_negative:
                self.background.append(sym >= 0)
            return _SymVal(sym, "int", is_null=False)
        if field_type == "float":
            sym = z3.Real(vname)
            if add_domain_axioms and non_negative:
                self.background.append(sym >= z3.RealVal("0"))
            return _SymVal(sym, "float", is_null=False)
        if field_type == "bool":
            sym = z3.Bool(vname)
            return _SymVal(sym, "bool", is_null=False)

        sym = z3.Int(vname)
        min_code = 0 if nullable else 1
        if add_domain_axioms:
            if allowed_strings:
                codes = [self.codec.encode(s) for s in allowed_strings]
                if nullable:
                    codes = [self.codec.null_id, *codes]
                self.background.append(z3.Or(*[sym == c for c in codes]))
            else:
                self.background.append(z3.And(sym >= min_code, sym < self.codec.max_id))
        null_cond: Any = (sym == self.codec.null_id) if nullable else False
        return _SymVal(sym, "str", is_null=null_cond)

    def domain_axioms_for_entity(self, ent: _SymEntity) -> list[Any]:
        z3 = self.z3
        axioms: list[Any] = []
        ot = ent.ot
        for fld in ot.fields:
            sv = ent.fields[fld.name]
            if fld.field_type == "int" and fld.non_negative:
                axioms.append(sv.expr >= 0)
            elif fld.field_type == "float" and fld.non_negative:
                axioms.append(sv.expr >= z3.RealVal("0"))
            elif fld.field_type == "str":
                if (
                    fld.name == ot.status_field
                    and (ot.initial_statuses is not None or ot.transitions)
                ):
                    valid_states = self._valid_fsm_states(ot)
                    if valid_states:
                        axioms.append(
                            z3.Or(*[sv.expr == self.codec.encode(s) for s in valid_states])
                        )
                        continue
                axioms.append(z3.And(sv.expr >= 1, sv.expr < self.codec.max_id))
        for rel in ot.relations:
            sv = ent.fields[rel.field]
            min_code = 0 if (not rel.required and not rel.multiple) else 1
            axioms.append(z3.And(sv.expr >= min_code, sv.expr < self.codec.max_id))
        return axioms

    @staticmethod
    def _valid_fsm_states(ot: ObjectTypeBlueprint) -> list[str]:
        states: set[str] = set()
        if ot.initial_statuses:
            states.update(ot.initial_statuses)
        for src_s, targets in ot.transitions.items():
            states.add(src_s)
            states.update(targets)
        return sorted(states)

    def build_entity(
        self,
        ot_name: str,
        tag: str,
        *,
        pre_state: bool = True,
        register_congruence: bool = True,
    ) -> _SymEntity:
        z3 = self.z3
        ot = self.ot_by_name[ot_name]
        ent = _SymEntity(ot=ot, tag=tag)
        seen_names: set[str] = set()

        for fld in ot.fields:
            seen_names.add(fld.name)
            ent.fields[fld.name] = self.make_scalar_var(
                f"{tag}_{fld.name}",
                fld.field_type,
                add_domain_axioms=False,
            )
        if ot.primary_key not in seen_names:
            ent.fields[ot.primary_key] = self.make_scalar_var(
                f"{tag}_{ot.primary_key}",
                "str",
                add_domain_axioms=False,
            )
        for rel in ot.relations:
            if rel.field in ent.fields:
                continue
            nullable = (not rel.required) and (not rel.multiple)
            ent.fields[rel.field] = self.make_scalar_var(
                f"{tag}_{rel.field}",
                "str",
                nullable=nullable,
                add_domain_axioms=False,
            )

        if pre_state:
            self.background.extend(self.domain_axioms_for_entity(ent))

        if register_congruence:
            existing = self.named_entities.setdefault(ot_name, [])
            pk_new = ent.fields[ot.primary_key].expr
            for prev in existing:
                pk_prev = prev.fields[ot.primary_key].expr
                eq_fields = [
                    ent.fields[fn].expr == prev.fields[fn].expr
                    for fn in ent.fields
                    if fn != ot.primary_key
                ]
                if eq_fields:
                    self.background.append(z3.Implies(pk_new == pk_prev, z3.And(*eq_fields)))
            existing.append(ent)

            if ot_name in self.bmc_tables:
                self._link_entity_to_bmc(ot_name, ent)

        return ent

    def _link_entity_to_bmc(self, ot_name: str, ent: _SymEntity) -> None:
        z3 = self.z3
        key = (ot_name, ent.tag)
        if key in self.bmc_linked_tags:
            return
        self.bmc_linked_tags.add(key)
        rows = self.bmc_tables[ot_name]
        pk_field = ent.ot.primary_key
        match_disj = []
        for row in rows:
            eqs = [ent.fields[fn].expr == row.entity.fields[fn].expr for fn in ent.fields]
            match_disj.append(z3.And(row.present, *eqs))
        if match_disj:
            self.background.append(z3.Or(*match_disj))

    def get_bmc_table(self, ot_name: str) -> list[_SymRow]:
        z3 = self.z3
        self.used_bmc = True
        if ot_name in self.bmc_tables:
            return self.bmc_tables[ot_name]
        existing_named = self.named_entities.get(ot_name, [])
        row_count = max(self.bmc_bound, len(existing_named))
        ot = self.ot_by_name[ot_name]
        rows: list[_SymRow] = []
        for idx in range(row_count):
            present = z3.Bool(self.next_name(f"bmc_{ot_name}_present_{idx}"))
            row_ent = self.build_entity(
                ot_name,
                f"bmc_{ot_name}_{idx}",
                pre_state=False,
                register_congruence=False,
            )
            row_axioms = self.domain_axioms_for_entity(row_ent)
            if row_axioms:
                self.background.append(z3.Implies(present, z3.And(*row_axioms)))
            rows.append(_SymRow(present=present, entity=row_ent))

        pk_field = ot.primary_key
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                self.background.append(
                    z3.Implies(
                        z3.And(rows[i].present, rows[j].present),
                        rows[i].entity.fields[pk_field].expr != rows[j].entity.fields[pk_field].expr,
                    )
                )

        self.bmc_tables[ot_name] = rows
        for ent in existing_named:
            self._link_entity_to_bmc(ot_name, ent)
        return rows

    def resolve_linked_entity(
        self,
        parent: _SymEntity,
        rel_field: str,
        *,
        pre_state: bool = True,
    ) -> tuple[_SymEntity, Any]:
        z3 = self.z3
        rel = next((r for r in parent.ot.relations if r.field == rel_field), None)
        if rel is None or rel.target_type not in self.ot_by_name:
            raise InvariantError(
                f"cannot traverse unknown relation {parent.ot.name}.{rel_field!r}",
                code="UNKNOWN_RELATION_IN_REF",
            )
        fk_sym = parent.fields[rel_field]
        if rel_field in parent.linked:
            return parent.linked[rel_field], fk_sym.is_null

        target_ent = self.build_entity(
            rel.target_type,
            f"{parent.tag}__{rel_field}",
            pre_state=pre_state,
            register_congruence=pre_state,
        )
        target_pk = target_ent.fields[target_ent.ot.primary_key].expr
        if isinstance(fk_sym.is_null, bool) and not fk_sym.is_null:
            self.background.append(target_pk == fk_sym.expr)
        else:
            self.background.append(z3.Implies(z3.Not(fk_sym.is_null), target_pk == fk_sym.expr))
        parent.linked[rel_field] = target_ent
        return target_ent, fk_sym.is_null

    def build_params(self, act: ActionBlueprint) -> dict[str, _SymVal]:
        params: dict[str, _SymVal] = {}
        for p in act.params:
            params[p.name] = self.make_scalar_var(
                f"param_{act.name}_{p.name}",
                p.field_type,
                non_negative=p.non_negative,
                nullable=False,
                add_domain_axioms=True,
            )
        return params

    def build_actor(self, allowed_roles: Sequence[str]) -> dict[str, _SymVal]:
        roles_pool = list(allowed_roles) if allowed_roles else list(self.bp.roles)
        role_val = self.make_scalar_var(
            "actor_role",
            "str",
            allowed_strings=roles_pool or None,
            add_domain_axioms=True,
        )
        actor_id_val = self.make_scalar_var("actor_id", "str", add_domain_axioms=True)
        return {"role": role_val, "actor_id": actor_id_val}

    def _combine_null(self, n1: Any, n2: Any) -> Any:
        z3 = self.z3
        if n1 is True or n2 is True:
            return True
        if n1 is False:
            return n2
        if n2 is False:
            return n1
        return z3.Or(n1, n2)

    def _is_truthy(self, cond: Any) -> bool:
        return cond is True

    def _is_falsy(self, cond: Any) -> bool:
        return cond is False

    def _not_null(self, cond: Any) -> Any:
        if cond is False:
            return True
        if cond is True:
            return False
        return self.z3.Not(cond)

    def _resolve_ref_val(
        self,
        ref: str,
        ctx: Mapping[str, Any],
        *,
        pre_state: bool = True,
        state_override: Mapping[str, Sequence[_SymRow]] | None = None,
    ) -> _SymVal:
        z3 = self.z3
        parts = ref.split(".")
        root = parts[0]
        tail = parts[1:]
        if root not in ctx:
            raise InvariantError(
                f"ref root {root!r} missing from symbolic context",
                code="UNAVAILABLE_REF_IN_CONTEXT",
            )
        target = ctx[root]
        if isinstance(target, Mapping) and not isinstance(target, _SymEntity):
            if not tail or tail[0] not in target:
                if tail:
                    return self.make_scalar_var(f"ext_{root}_{tail[0]}", "str")
                raise InvariantError(f"invalid ref {ref!r}", code="INVALID_PREDICATE_AST")
            val = target[tail[0]]
            if isinstance(val, _SymVal):
                if val.kind == "float":
                    self.used_real = True
                return val
            raise InvariantError(f"unexpected context entry for {ref!r}", code="INVALID_PREDICATE_AST")

        if not isinstance(target, _SymEntity):
            raise InvariantError(f"invalid entity root {root!r} in ref {ref!r}", code="INVALID_PREDICATE_AST")

        cur = target
        null_accum: Any = False
        for idx, seg in enumerate(tail):
            has_more = idx < len(tail) - 1
            if has_more:
                nxt, fk_null = self.resolve_linked_entity(cur, seg, pre_state=True)
                if state_override and nxt.ot.name in state_override:
                    nxt = self._overlay_entity_lookup(cur.fields[seg], nxt, state_override[nxt.ot.name])
                null_accum = self._combine_null(null_accum, fk_null)
                cur = nxt
            else:
                sv = cur.fields.get(seg)
                if sv is None:
                    raise InvariantError(
                        f"unknown field {cur.ot.name}.{seg!r} in ref {ref!r}",
                        code="UNKNOWN_FIELD_IN_REF",
                    )
                if sv.kind == "float":
                    self.used_real = True
                combined_null = self._combine_null(null_accum, sv.is_null)
                return _SymVal(sv.expr, sv.kind, is_null=combined_null)
        raise InvariantError(f"ref {ref!r} has empty path", code="INVALID_PREDICATE_AST")

    def _overlay_entity_lookup(
        self,
        fk_sv: _SymVal,
        base_ent: _SymEntity,
        post_rows: Sequence[_SymRow],
    ) -> _SymEntity:
        """Build a merged symbolic view selecting matching post-state row fields by PK."""
        z3 = self.z3
        pk_name = base_ent.ot.primary_key
        out = _SymEntity(ot=base_ent.ot, tag=f"{base_ent.tag}_post", linked=dict(base_ent.linked))
        for fname, base_sv in base_ent.fields.items():
            expr = base_sv.expr
            for row in post_rows:
                row_pk = row.entity.fields[pk_name].expr
                row_fv = row.entity.fields[fname].expr
                expr = z3.If(z3.And(row.present, row_pk == fk_sv.expr), row_fv, expr)
            out.fields[fname] = _SymVal(expr, base_sv.kind, is_null=base_sv.is_null)
        return out

    def _literal_val(self, val: Any) -> _SymVal:
        z3 = self.z3
        if val is None:
            return _SymVal(z3.IntVal(self.codec.null_id), "null", is_null=True)
        if isinstance(val, bool):
            return _SymVal(z3.BoolVal(val), "bool", is_null=False)
        if isinstance(val, int):
            return _SymVal(z3.IntVal(val), "int", is_null=False)
        if isinstance(val, float):
            self.used_real = True
            return _SymVal(z3.RealVal(repr(val)), "float", is_null=False)
        if isinstance(val, str):
            return _SymVal(z3.IntVal(self.codec.encode(val)), "str", is_null=False)
        if isinstance(val, (list, tuple)):
            return _SymVal(
                z3.IntVal(self.codec.encode(",".join(str(x) for x in val))),
                "str",
                is_null=False,
            )
        raise InvariantError(f"unsupported scalar literal {val!r}", code="INVALID_PREDICATE_AST")

    @staticmethod
    def _is_const_numeric(node: Any) -> bool:
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            return True
        if isinstance(node, dict) and set(node.keys()) == {"lit"}:
            lit = node["lit"]
            return isinstance(lit, (int, float)) and not isinstance(lit, bool)
        return False

    @staticmethod
    def _const_numeric_value(node: Any) -> int | float | None:
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            return node
        if isinstance(node, dict) and set(node.keys()) == {"lit"}:
            lit = node["lit"]
            if isinstance(lit, (int, float)) and not isinstance(lit, bool):
                return lit
        return None

    def _coerce_arith_pair(self, lhs: _SymVal, rhs: _SymVal) -> tuple[Any, Any, Literal["int", "float"]]:
        z3 = self.z3
        l_is_real = lhs.kind == "float" or z3.is_real(lhs.expr)
        r_is_real = rhs.kind == "float" or z3.is_real(rhs.expr)
        if l_is_real or r_is_real:
            self.used_real = True
            le = lhs.expr if l_is_real else z3.ToReal(lhs.expr)
            re_ = rhs.expr if r_is_real else z3.ToReal(rhs.expr)
            return le, re_, "float"
        return lhs.expr, rhs.expr, "int"

    def _compile_rows(
        self,
        ot_name: str,
        state_override: Mapping[str, Sequence[_SymRow]] | None = None,
    ) -> list[_SymRow]:
        self.used_bmc = True
        if state_override and ot_name in state_override:
            return list(state_override[ot_name])
        return self.get_bmc_table(ot_name)

    def compile_value(
        self,
        node: Any,
        ctx: Mapping[str, Any],
        *,
        pre_state: bool = True,
        state_override: Mapping[str, Sequence[_SymRow]] | None = None,
        guards: list[Any] | None = None,
    ) -> _SymVal:
        z3 = self.z3
        if isinstance(node, (str, int, float, bool, list, tuple)) or node is None:
            return self._literal_val(node)
        if not isinstance(node, dict):
            raise InvariantError(f"unsupported value AST node: {node!r}", code="INVALID_PREDICATE_AST")
        if set(node.keys()) == {"lit"}:
            return self._literal_val(node["lit"])
        if set(node.keys()) == {"ref"}:
            return self._resolve_ref_val(
                node["ref"], ctx, pre_state=pre_state, state_override=state_override
            )
        op = node.get("op")
        if op in ARITHMETIC_OPS:
            lv = self.compile_value(
                node["left"], ctx, pre_state=pre_state, state_override=state_override, guards=guards
            )
            rv = self.compile_value(
                node["right"], ctx, pre_state=pre_state, state_override=state_override, guards=guards
            )
            le, re_, res_kind = self._coerce_arith_pair(lv, rv)
            null_cond = self._combine_null(lv.is_null, rv.is_null)
            if op == "add":
                return _SymVal(le + re_, res_kind, is_null=null_cond)
            if op == "sub":
                return _SymVal(le - re_, res_kind, is_null=null_cond)
            if op == "mul":
                if not (self._is_const_numeric(node["left"]) or self._is_const_numeric(node["right"])):
                    self.used_nonlinear = True
                return _SymVal(le * re_, res_kind, is_null=null_cond)
            if op == "div":
                self.used_real = True
                le_r = le if z3.is_real(le) else z3.ToReal(le)
                re_r = re_ if z3.is_real(re_) else z3.ToReal(re_)
                c_rhs = self._const_numeric_value(node["right"])
                if c_rhs is None:
                    self.used_nonlinear = True
                    if guards is not None:
                        guards.append(re_r != z3.RealVal("0"))
                elif c_rhs == 0:
                    if guards is not None:
                        guards.append(z3.BoolVal(False))
                    return _SymVal(z3.RealVal("0"), "float", is_null=True)
                return _SymVal(le_r / re_r, "float", is_null=null_cond)

        if op in AGGREGATE_OPS:
            ot_name = str(node["object_type"])
            rows = self._compile_rows(ot_name, state_override=state_override)
            where_ast = node.get("where")
            fspec = node["field"]
            matched_vals: list[tuple[Any, _SymVal]] = []
            infer_kind: Literal["int", "float"] = "int"
            for row in rows:
                sub_ctx = {**ctx, "target": row.entity}
                if where_ast:
                    w_bool = self.compile_bool(
                        where_ast,
                        sub_ctx,
                        pre_state=pre_state,
                        state_override=state_override,
                    )
                    m_flag = z3.And(row.present, w_bool)
                else:
                    m_flag = row.present
                if isinstance(fspec, str):
                    fval = row.entity.fields.get(fspec)
                    if fval is None:
                        raise InvariantError(
                            f"aggregate references unknown field {ot_name}.{fspec!r}",
                            code="UNKNOWN_FIELD_IN_REF",
                        )
                else:
                    fval = self.compile_value(
                        fspec,
                        sub_ctx,
                        pre_state=pre_state,
                        state_override=state_override,
                        guards=guards,
                    )
                if fval.kind == "float":
                    infer_kind = "float"
                    self.used_real = True
                matched_vals.append((m_flag, fval))

            has_default = "default" in node
            def_val = node.get("default")
            if isinstance(def_val, float):
                infer_kind = "float"
                self.used_real = True

            if op == "sum":
                zero_expr = z3.RealVal("0") if infer_kind == "float" else z3.IntVal(0)
                terms = []
                any_flags = []
                for m_flag, fv in matched_vals:
                    f_expr = fv.expr if (infer_kind != "float" or z3.is_real(fv.expr)) else z3.ToReal(fv.expr)
                    active = z3.And(m_flag, self._not_null(fv.is_null))
                    any_flags.append(active)
                    terms.append(z3.If(active, f_expr, zero_expr))
                total = z3.Sum(*terms) if terms else zero_expr
                if has_default and def_val is None:
                    any_expr = z3.Or(*any_flags) if any_flags else z3.BoolVal(False)
                    return _SymVal(total, infer_kind, is_null=z3.Not(any_expr))
                if has_default and def_val != 0:
                    any_expr = z3.Or(*any_flags) if any_flags else z3.BoolVal(False)
                    d_lit = self._literal_val(def_val)
                    d_expr = (
                        d_lit.expr
                        if (infer_kind != "float" or z3.is_real(d_lit.expr))
                        else z3.ToReal(d_lit.expr)
                    )
                    total = z3.If(any_expr, total, d_expr)
                return _SymVal(total, infer_kind, is_null=False)

            # min / max
            seen: Any = z3.BoolVal(False)
            best: Any = z3.RealVal("0") if infer_kind == "float" else z3.IntVal(0)
            for m_flag, fv in matched_vals:
                active = z3.And(m_flag, self._not_null(fv.is_null))
                f_expr = fv.expr if (infer_kind != "float" or z3.is_real(fv.expr)) else z3.ToReal(fv.expr)
                take = (f_expr < best) if op == "min" else (f_expr > best)
                cond_upd = z3.And(active, z3.Or(z3.Not(seen), take))
                best = z3.If(cond_upd, f_expr, best)
                seen = z3.Or(seen, active)
            if has_default and def_val is not None:
                d_lit = self._literal_val(def_val)
                d_expr = (
                    d_lit.expr
                    if (infer_kind != "float" or z3.is_real(d_lit.expr))
                    else z3.ToReal(d_lit.expr)
                )
                return _SymVal(z3.If(seen, best, d_expr), infer_kind, is_null=False)
            return _SymVal(best, infer_kind, is_null=z3.Not(seen))

        if op == "if_else":
            cond_z3 = self.compile_bool(
                node["cond"], ctx, pre_state=pre_state, state_override=state_override
            )
            t_val = self.compile_value(
                node["then"], ctx, pre_state=pre_state, state_override=state_override, guards=guards
            )
            e_val = self.compile_value(
                node["else"], ctx, pre_state=pre_state, state_override=state_override, guards=guards
            )
            if t_val.kind == "null":
                return _SymVal(
                    e_val.expr,
                    e_val.kind,
                    is_null=z3.If(cond_z3, True, e_val.is_null),
                )
            if e_val.kind == "null":
                return _SymVal(
                    t_val.expr,
                    t_val.kind,
                    is_null=z3.If(cond_z3, t_val.is_null, True),
                )
            if t_val.kind in ("int", "float") and e_val.kind in ("int", "float"):
                te, ee, rkind = self._coerce_arith_pair(t_val, e_val)
                null_branch = (
                    False
                    if (self._is_falsy(t_val.is_null) and self._is_falsy(e_val.is_null))
                    else z3.If(cond_z3, z3.BoolVal(True) if t_val.is_null is True else (z3.BoolVal(False) if t_val.is_null is False else t_val.is_null), z3.BoolVal(True) if e_val.is_null is True else (z3.BoolVal(False) if e_val.is_null is False else e_val.is_null))
                )
                return _SymVal(z3.If(cond_z3, te, ee), rkind, is_null=null_branch)
            null_branch = (
                False
                if (self._is_falsy(t_val.is_null) and self._is_falsy(e_val.is_null))
                else z3.If(cond_z3, z3.BoolVal(True) if t_val.is_null is True else (z3.BoolVal(False) if t_val.is_null is False else t_val.is_null), z3.BoolVal(True) if e_val.is_null is True else (z3.BoolVal(False) if e_val.is_null is False else e_val.is_null))
            )
            return _SymVal(z3.If(cond_z3, t_val.expr, e_val.expr), t_val.kind, is_null=null_branch)

        raise InvariantError(f"unsupported value AST op {op!r}", code="INVALID_PREDICATE_AST")

    def _compile_list_literal(self, node: Any) -> list[Any]:
        if isinstance(node, list):
            return node
        if isinstance(node, dict) and set(node.keys()) == {"lit"} and isinstance(node["lit"], list):
            return node["lit"]
        raise InvariantError(
            f"'in'/'not_in' right operand must be a literal list, got {node!r}",
            code="INVALID_PREDICATE_AST",
        )

    def _compare_eq(self, lv: _SymVal, rv: _SymVal) -> Any:
        z3 = self.z3
        if lv.kind == "null" and rv.kind == "null":
            return z3.BoolVal(True)
        if rv.kind == "null":
            return z3.BoolVal(True) if self._is_truthy(lv.is_null) else (z3.BoolVal(False) if self._is_falsy(lv.is_null) else lv.is_null)
        if lv.kind == "null":
            return z3.BoolVal(True) if self._is_truthy(rv.is_null) else (z3.BoolVal(False) if self._is_falsy(rv.is_null) else rv.is_null)

        if lv.kind in ("int", "float") and rv.kind in ("int", "float"):
            le, re_, _ = self._coerce_arith_pair(lv, rv)
            raw_eq = le == re_
        else:
            raw_eq = lv.expr == rv.expr

        if self._is_falsy(lv.is_null) and self._is_falsy(rv.is_null):
            return raw_eq
        ln = z3.BoolVal(True) if lv.is_null is True else (z3.BoolVal(False) if lv.is_null is False else lv.is_null)
        rn = z3.BoolVal(True) if rv.is_null is True else (z3.BoolVal(False) if rv.is_null is False else rv.is_null)
        return z3.Or(z3.And(ln, rn), z3.And(z3.Not(ln), z3.Not(rn), raw_eq))

    def compile_bool(
        self,
        expr: Any,
        ctx: Mapping[str, Any],
        *,
        pre_state: bool = True,
        state_override: Mapping[str, Sequence[_SymRow]] | None = None,
    ) -> Any:
        z3 = self.z3
        if not isinstance(expr, dict):
            raise InvariantError("predicate AST must be a dict", code="INVALID_PREDICATE_AST")
        op = expr.get("op")
        if op == "and":
            subs = [
                self.compile_bool(a, ctx, pre_state=pre_state, state_override=state_override)
                for a in expr["args"]
            ]
            return z3.And(*subs) if subs else z3.BoolVal(True)
        if op == "or":
            subs = [
                self.compile_bool(a, ctx, pre_state=pre_state, state_override=state_override)
                for a in expr["args"]
            ]
            return z3.Or(*subs) if subs else z3.BoolVal(False)
        if op == "not":
            return z3.Not(
                self.compile_bool(
                    expr["arg"], ctx, pre_state=pre_state, state_override=state_override
                )
            )
        if op in ("in", "not_in"):
            lv = self.compile_value(
                expr["left"], ctx, pre_state=pre_state, state_override=state_override
            )
            items = self._compile_list_literal(expr["right"])
            if not items:
                return z3.BoolVal(op == "not_in")
            eqs = [self._compare_eq(lv, self._literal_val(item)) for item in items]
            mem = z3.Or(*eqs)
            return mem if op == "in" else z3.Not(mem)

        if op in COMPARISON_OPS:
            guards: list[Any] = []
            lv = self.compile_value(
                expr["left"],
                ctx,
                pre_state=pre_state,
                state_override=state_override,
                guards=guards,
            )
            rv = self.compile_value(
                expr["right"],
                ctx,
                pre_state=pre_state,
                state_override=state_override,
                guards=guards,
            )
            if op == "eq":
                core = self._compare_eq(lv, rv)
            elif op == "ne":
                core = z3.Not(self._compare_eq(lv, rv))
            else:
                if self._is_truthy(lv.is_null) or self._is_truthy(rv.is_null):
                    return z3.BoolVal(False)
                le, re_, _ = self._coerce_arith_pair(lv, rv)
                if op == "lt":
                    rel_cmp = le < re_
                elif op == "le":
                    rel_cmp = le <= re_
                elif op == "gt":
                    rel_cmp = le > re_
                else:
                    rel_cmp = le >= re_
                non_null_parts = [self._not_null(lv.is_null), self._not_null(rv.is_null)]
                active_nn = [p for p in non_null_parts if p is not True]
                core = z3.And(*active_nn, rel_cmp) if active_nn else rel_cmp
            return z3.And(*guards, core) if guards else core

        if op in QUANTIFIER_OPS:
            ot_name = str(expr["object_type"])
            rows = self._compile_rows(ot_name, state_override=state_override)
            where_ast = expr.get("where")
            flags = []
            for row in rows:
                sub_ctx = {**ctx, "target": row.entity}
                if where_ast:
                    wb = self.compile_bool(
                        where_ast, sub_ctx, pre_state=pre_state, state_override=state_override
                    )
                    flags.append(z3.And(row.present, wb))
                else:
                    flags.append(row.present)
            if op == "exists":
                return z3.Or(*flags) if flags else z3.BoolVal(False)
            if op == "not_exists":
                return z3.Not(z3.Or(*flags)) if flags else z3.BoolVal(True)
            cnt = z3.Sum(*[z3.If(f, 1, 0) for f in flags]) if flags else z3.IntVal(0)
            target_n = int(expr["value"])
            if op == "count_eq":
                return cnt == target_n
            if op == "count_ge":
                return cnt >= target_n
            return cnt <= target_n

        raise InvariantError(f"unsupported predicate op {op!r}", code="INVALID_PREDICATE_AST")

    def logic_fragment(self) -> str:
        if self.used_bmc:
            return f"BMC_BOUNDED(k={self.bmc_bound})"
        if self.used_nonlinear:
            return "QF_NIA_HEURISTIC"
        if self.used_real:
            return "EXACT_QF_LRA"
        return "EXACT_QF_LIA"


# ---------------------------------------------------------------------------
# Action Subject Resolution & Model Decoding Helpers
# ---------------------------------------------------------------------------
def _resolve_rule_id_param(
    rule: InvariantRuleSpec,
    act: ActionBlueprint,
    ot_by_name: Mapping[str, ObjectTypeBlueprint],
) -> str:
    if rule.id_param:
        return rule.id_param
    created_id_params = {
        m.id_param
        for m in act.mutations
        if m.object_type == rule.target_type and m.op == "create"
    }
    for m in act.mutations:
        if m.object_type == rule.target_type and m.op != "create" and m.target_ref is None:
            return m.id_param
    ot = ot_by_name.get(rule.target_type)
    pk_name = ot.primary_key if ot is not None else "id"
    param_names = {p.name for p in act.params}
    if pk_name in param_names and pk_name not in created_id_params:
        return pk_name
    return f"__singleton_{rule.target_type}"


def _build_action_pre_context(
    comp: _Z3Compiler,
    act: ActionBlueprint,
) -> tuple[dict[str, _SymVal], dict[str, _SymVal], dict[tuple[str, str], _SymEntity], _SymEntity | None]:
    actor = comp.build_actor(act.allowed_roles)
    params = comp.build_params(act)
    subjects: dict[tuple[str, str], _SymEntity] = {}
    anchor: _SymEntity | None = None

    # First materialize primary anchor from mutations if available
    for m in act.mutations:
        if m.op != "create" and m.target_ref is None and m.id_param in params:
            key = (m.object_type, m.id_param)
            if key not in subjects:
                ent = comp.build_entity(m.object_type, f"subj_{m.object_type}_{m.id_param}")
                pk_name = ent.ot.primary_key
                comp.background.append(ent.fields[pk_name].expr == params[m.id_param].expr)
                subjects[key] = ent
                if anchor is None:
                    anchor = ent

    # Materialize subjects referenced by preconditions
    for pre in act.preconditions:
        if pre.target_type not in comp.ot_by_name:
            continue
        idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
        key = (pre.target_type, idp)
        if key not in subjects:
            ent = comp.build_entity(pre.target_type, f"subj_{pre.target_type}_{idp}")
            if idp in params:
                pk_name = ent.ot.primary_key
                comp.background.append(ent.fields[pk_name].expr == params[idp].expr)
            subjects[key] = ent
            if anchor is None:
                anchor = ent

    # Check if custom slice or global invariants target an action-related type
    return actor, params, subjects, anchor


def _collect_action_referenced_types(
    act: ActionBlueprint,
    subjects: Mapping[tuple[str, str], _SymEntity],
    extra_slices: Sequence[tuple[str, Any]] = (),
) -> set[str]:
    types = {ot_name for ot_name, _ in subjects.keys()}
    for m in act.mutations:
        if m.object_type:
            types.add(m.object_type)
    for pre in act.preconditions:
        qtypes: set[str] = set()
        _collect_expr_refs(pre.when, set(), quant_types=qtypes)
        _collect_expr_refs(pre.must_hold, set(), quant_types=qtypes)
        types.update(qtypes)
    for _, sl_expr in extra_slices:
        qtypes = set()
        _collect_expr_refs(sl_expr, set(), quant_types=qtypes)
        types.update(qtypes)
    return types


def _decode_z3_scalar(z3_mod: Any, model: Any, sv: _SymVal, codec: _StringCodec) -> Any:
    raw = model.eval(sv.expr, model_completion=True)
    if sv.kind == "bool":
        return bool(z3_mod.is_true(raw))
    if sv.kind == "int":
        return raw.as_long()
    if sv.kind == "float":
        frac = raw.as_fraction()
        val = float(frac.numerator) / float(frac.denominator)
        return round(val, 6)
    if sv.kind == "str":
        return codec.decode(raw.as_long())
    return None


def _decode_entity(
    z3_mod: Any,
    model: Any,
    ent: _SymEntity,
    codec: _StringCodec,
) -> dict[str, Any]:
    return {
        fname: _decode_z3_scalar(z3_mod, model, sv, codec)
        for fname, sv in ent.fields.items()
    }


def _decode_witness(
    z3_mod: Any,
    model: Any,
    comp: _Z3Compiler,
    actor: Mapping[str, _SymVal],
    params: Mapping[str, _SymVal],
    subjects: Mapping[tuple[str, str], _SymEntity],
) -> dict[str, Any]:
    objects_map: dict[str, Any] = {}
    for (ot_name, idp), ent in subjects.items():
        objects_map[f"{ot_name}({idp})"] = _decode_entity(z3_mod, model, ent, comp.codec)
        for rel_name, linked_ent in ent.linked.items():
            objects_map[f"{ot_name}({idp}).{rel_name}"] = _decode_entity(
                z3_mod, model, linked_ent, comp.codec
            )
    return {
        "actor": {k: _decode_z3_scalar(z3_mod, model, v, comp.codec) for k, v in actor.items()},
        "params": {k: _decode_z3_scalar(z3_mod, model, v, comp.codec) for k, v in params.items()},
        "objects": objects_map,
    }


def _extract_core_rule_names(raw_core: Sequence[Any], slice_tracker_map: Mapping[str, str] | None = None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    tracker_map = slice_tracker_map or {}
    for atom in raw_core:
        raw_name = str(atom)
        if raw_name in tracker_map:
            mapped = tracker_map[raw_name]
            if mapped and mapped not in seen:
                names.append(mapped)
                seen.add(mapped)
            continue
        if raw_name.startswith("__slice_trigger__"):
            continue
        if raw_name not in seen:
            names.append(raw_name)
            seen.add(raw_name)
    return names


def _eval_model_bool(z3_mod: Any, model: Any, b_expr: Any) -> bool:
    return bool(z3_mod.is_true(model.eval(b_expr, model_completion=True)))


# ---------------------------------------------------------------------------
# 1. Action Reachability + Conditional Slice Deadlocks + Minimal Unsat Core
# ---------------------------------------------------------------------------
def _normalize_custom_slices(
    raw_slices: Mapping[str, Any] | None,
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    if not raw_slices:
        return out
    for act_name, entry in raw_slices.items():
        if isinstance(entry, Mapping):
            out[act_name] = [(str(k), dict(v)) for k, v in entry.items()]
        else:
            out[act_name] = [(str(k), dict(v)) for k, v in entry]
    return out


def prove_action_reachability(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    action_names: Sequence[str] | None = None,
    slices: Mapping[str, Any] | None = None,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> list[ActionReachabilityProof]:
    """Prove global & conditional-branch reachability for every governed action."""
    z3 = _require_z3()
    raw_bp = _resolve_effective_blueprint(blueprint_or_spec, overlay=overlay)
    bp, ancestors_map, _ = _prepare_blueprint_for_prover(raw_bp)
    custom_slices_map = _normalize_custom_slices(slices)
    extra_exprs = [
        expr for pairs in custom_slices_map.values() for _, expr in pairs
    ]
    codec = _StringCodec(bp, extra_exprs=extra_exprs)

    target_filter = set(action_names) if action_names is not None else None
    results: list[ActionReachabilityProof] = []

    for act in bp.actions:
        if target_filter is not None and act.name not in target_filter:
            continue
        t0 = time.perf_counter()
        comp = _Z3Compiler(z3, bp, codec, bmc_bound=bmc_bound)
        actor, params, subjects, _ = _build_action_pre_context(comp, act)
        act_custom_slices = custom_slices_map.get(act.name, [])
        ref_types = _collect_action_referenced_types(act, subjects, act_custom_slices)

        # Ensure a subject exists if a custom slice references obj without preconditions
        if act_custom_slices and not subjects:
            for m in act.mutations:
                if m.object_type in comp.ot_by_name:
                    ent = comp.build_entity(m.object_type, f"subj_{m.object_type}_mut")
                    subjects[(m.object_type, m.id_param)] = ent
                    ref_types.add(m.object_type)
                    break

        solver = z3.Solver()
        solver.set("unsat_core", True)
        solver.set("core.minimize", True)
        solver.set("timeout", int(timeout_ms))

        # Track preconditions first so linked traversal types are registered
        compiled_pres: list[tuple[InvariantRuleSpec, Any, Any, Any]] = []
        for pre in act.preconditions:
            idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
            subj = subjects[(pre.target_type, idp)]
            p_ctx = {"obj": subj, "param": params, "actor": actor}
            when_z3 = comp.compile_bool(pre.when, p_ctx) if pre.when is not None else None
            must_z3 = comp.compile_bool(pre.must_hold, p_ctx)
            body_z3 = z3.Implies(when_z3, must_z3) if when_z3 is not None else must_z3
            compiled_pres.append((pre, when_z3, must_z3, body_z3))

        # Compile custom slices before global invariant lookup
        default_subj = next(iter(subjects.values()), None)
        compiled_custom: list[tuple[str, Any]] = []
        for sl_name, sl_ast in act_custom_slices:
            sl_ctx: dict[str, Any] = {"param": params, "actor": actor}
            if default_subj is not None:
                sl_ctx["obj"] = default_subj
            compiled_custom.append((sl_name, comp.compile_bool(sl_ast, sl_ctx)))

        # Track applicable global invariants (including inherited ancestor invariants)
        inv_scope_types = ref_types | set(comp.named_entities.keys())
        inv_scope_ancestors: set[str] = set()
        for tname in inv_scope_types:
            inv_scope_ancestors.update(_ancestor_closure(tname, ancestors_map))
        inv_trackers: list[tuple[str, Any]] = []
        for inv in bp.invariants:
            if inv.target_type not in inv_scope_ancestors:
                continue
            matching_types = sorted(
                tname
                for tname in inv_scope_types
                if inv.target_type in _ancestor_closure(tname, ancestors_map)
            )
            row_formulas = []
            for mt in matching_types:
                rows = comp.get_bmc_table(mt)
                for row in rows:
                    sub_ctx = {"obj": row.entity}
                    w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                    m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
                    row_formulas.append(z3.Implies(z3.And(row.present, w_z3), m_z3))
            inv_formula = z3.And(*row_formulas) if row_formulas else z3.BoolVal(True)
            inv_trackers.append((inv.name, inv_formula))

        # Background domain axioms
        for bg in comp.background:
            solver.add(bg)

        # Build domain-only baseline checker for joint trigger feasibility
        domain_solver = z3.Solver()
        domain_solver.set("timeout", int(timeout_ms))
        for bg in comp.background:
            domain_solver.add(bg)
        for _, inv_f in inv_trackers:
            domain_solver.add(inv_f)

        # Track invariants and preconditions on main solver
        for inv_name, inv_f in inv_trackers:
            solver.assert_and_track(inv_f, z3.Bool(inv_name))
        for pre, _, _, body_z3 in compiled_pres:
            solver.assert_and_track(body_z3, z3.Bool(pre.name))

        chk = solver.check()
        frag = comp.logic_fragment()
        if chk == z3.unknown:
            elapsed = (time.perf_counter() - t0) * 1000.0
            results.append(
                ActionReachabilityProof(
                    action_name=act.name,
                    implementation_kind="edit_function" if act.edit_function else "rule",
                    status="UNKNOWN",
                    rule_count=len(act.preconditions),
                    logic_fragment="UNKNOWN",
                    elapsed_ms=round(elapsed, 3),
                )
            )
            continue

        if chk == z3.unsat:
            core = _extract_core_rule_names(solver.unsat_core())
            elapsed = (time.perf_counter() - t0) * 1000.0
            results.append(
                ActionReachabilityProof(
                    action_name=act.name,
                    implementation_kind="edit_function" if act.edit_function else "rule",
                    status="TOTAL_DEADLOCK",
                    rule_count=len(act.preconditions),
                    unsat_core=core,
                    logic_fragment=frag,
                    elapsed_ms=round(elapsed, 3),
                )
            )
            continue

        # Global check SAT -> extract witness & evaluate conditional slices
        global_model = solver.model()
        witness = _decode_witness(z3, global_model, comp, actor, params, subjects)

        cond_indices = [i for i, (_, w_z3, _, _) in enumerate(compiled_pres) if w_z3 is not None]
        witnessed_sat_masks: list[set[int]] = []
        if cond_indices:
            sat_in_global = {
                idx
                for idx in cond_indices
                if _eval_model_bool(z3, global_model, compiled_pres[idx][1])
            }
            if sat_in_global:
                witnessed_sat_masks.append(sat_in_global)

        def _is_already_witnessed(subset: tuple[int, ...]) -> bool:
            s_set = set(subset)
            return any(s_set.issubset(mask) for mask in witnessed_sat_masks)

        def _record_model_mask(mdl: Any) -> None:
            if not cond_indices:
                return
            mask = {
                idx
                for idx in cond_indices
                if _eval_model_bool(z3, mdl, compiled_pres[idx][1])
            }
            if mask:
                witnessed_sat_masks.append(mask)

        # Greedy O(r) co-witnessing passes (forward & reverse) collapse compatible trigger subsets cleanly
        if len(cond_indices) >= 2:
            for order in (cond_indices, list(reversed(cond_indices))):
                solver.push()
                pushed_frames = 0
                last_sat_model = None
                for idx in order:
                    solver.push()
                    solver.add(compiled_pres[idx][1])
                    if solver.check() == z3.sat:
                        last_sat_model = solver.model()
                        pushed_frames += 1
                    else:
                        solver.pop()
                if last_sat_model is not None:
                    _record_model_mask(last_sat_model)
                for _ in range(pushed_frames):
                    solver.pop()
                solver.pop()

        slice_results: list[ConditionalSliceResult] = []
        deadlocked_indices_single: set[int] = set()
        deadlocked_pairs: set[tuple[int, int]] = set()

        def _test_slice_subset(
            sl_name: str,
            sl_kind: Literal["rule_when", "joint_when", "custom_slice"],
            cond_items: Sequence[tuple[str | None, Any]],
        ) -> ConditionalSliceResult:
            st0 = time.perf_counter()
            conj_z3 = z3.And(*[c for _, c in cond_items]) if len(cond_items) > 1 else cond_items[0][1]

            # 1) Must be reachable under domain axioms (skip mutually exclusive guards!)
            if sl_kind != "custom_slice":
                domain_solver.push()
                domain_solver.add(conj_z3)
                dom_chk = domain_solver.check()
                domain_solver.pop()
                if dom_chk == z3.unsat:
                    return ConditionalSliceResult(
                        slice_name=sl_name,
                        slice_kind=sl_kind,
                        status="SAT",
                        logic_fragment=frag,
                        elapsed_ms=round((time.perf_counter() - st0) * 1000.0, 3),
                    )
                if dom_chk == z3.unknown:
                    return ConditionalSliceResult(
                        slice_name=sl_name,
                        slice_kind=sl_kind,
                        status="UNKNOWN",
                        logic_fragment="UNKNOWN",
                        elapsed_ms=round((time.perf_counter() - st0) * 1000.0, 3),
                    )

            # 2) Check conjunction with all rules under tracked guards
            solver.push()
            tracker_map: dict[str, str] = {}
            for pos, (rule_label, c_z3) in enumerate(cond_items):
                t_name = f"__slice_trigger__{len(slice_results)}_{pos}"
                if rule_label:
                    tracker_map[t_name] = rule_label
                solver.assert_and_track(c_z3, z3.Bool(t_name))
            sl_chk = solver.check()
            if sl_chk == z3.sat:
                _record_model_mask(solver.model())
                solver.pop()
                return ConditionalSliceResult(
                    slice_name=sl_name,
                    slice_kind=sl_kind,
                    status="SAT",
                    logic_fragment=frag,
                    elapsed_ms=round((time.perf_counter() - st0) * 1000.0, 3),
                )
            if sl_chk == z3.unknown:
                solver.pop()
                return ConditionalSliceResult(
                    slice_name=sl_name,
                    slice_kind=sl_kind,
                    status="UNKNOWN",
                    logic_fragment="UNKNOWN",
                    elapsed_ms=round((time.perf_counter() - st0) * 1000.0, 3),
                )
            raw_core = solver.unsat_core()
            solver.pop()
            core_names = _extract_core_rule_names(raw_core, tracker_map)
            return ConditionalSliceResult(
                slice_name=sl_name,
                slice_kind=sl_kind,
                status="CONDITIONAL_DEADLOCK",
                unsat_core=core_names,
                logic_fragment=frag,
                elapsed_ms=round((time.perf_counter() - st0) * 1000.0, 3),
            )

        # Evaluate explicit custom slices
        for sl_name, sl_z3 in compiled_custom:
            res_sl = _test_slice_subset(sl_name, "custom_slice", [(None, sl_z3)])
            slice_results.append(res_sl)

        # Evaluate every rule.when individually
        for idx in cond_indices:
            pre, w_z3, _, _ = compiled_pres[idx]
            if _is_already_witnessed((idx,)):
                slice_results.append(
                    ConditionalSliceResult(
                        slice_name=pre.name,
                        slice_kind="rule_when",
                        status="SAT",
                        logic_fragment=frag,
                        elapsed_ms=0.0,
                    )
                )
                continue
            res_sl = _test_slice_subset(pre.name, "rule_when", [(pre.name, w_z3)])
            slice_results.append(res_sl)
            if res_sl.status == "CONDITIONAL_DEADLOCK":
                deadlocked_indices_single.add(idx)

        # Evaluate joint conditional intersections (pairwise & 3-way) that can co-fire
        # Filter to indices not already individually deadlocked and build coarse symbol connectivity
        live_cond = [idx for idx in cond_indices if idx not in deadlocked_indices_single]
        uncond_keys: set[str] = set()
        for pre, w_z3, _, _ in compiled_pres:
            if w_z3 is None:
                uncond_keys.update(_action_symbol_keys(pre.must_hold))
        must_keys_by_idx: dict[int, set[str]] = {
            idx: _action_symbol_keys(compiled_pres[idx][0].must_hold)
            for idx in live_cond
        }
        when_keys_by_idx: dict[int, set[str]] = {
            idx: _action_symbol_keys(compiled_pres[idx][0].when)
            for idx in live_cond
        }

        def _rules_interact(indices: Sequence[int]) -> bool:
            """True if rules share constrained symbols or touch quantifiers/unconditional rules."""
            # Count shared keys across must_hold of participating rules or when <-> must overlap
            seen_keys: set[str] = set()
            for idx in indices:
                mk = must_keys_by_idx[idx]
                wk = when_keys_by_idx[idx]
                if any(k.startswith("quant:") for k in mk):
                    return True
                if (mk & seen_keys) or (mk & uncond_keys):
                    return True
                # A rule's must_hold tightening another rule's trigger condition
                for other_idx in indices:
                    if other_idx != idx and (mk & when_keys_by_idx[other_idx]):
                        return True
                seen_keys.update(mk)
                seen_keys.update(wk)
            return False

        # Pairwise joint triggers
        if len(live_cond) >= 2:
            for a_idx, b_idx in itertools.combinations(live_cond, 2):
                if _is_already_witnessed((a_idx, b_idx)):
                    continue
                if not _rules_interact((a_idx, b_idx)):
                    continue
                pre_a, w_a, _, _ = compiled_pres[a_idx]
                pre_b, w_b, _, _ = compiled_pres[b_idx]
                res_pair = _test_slice_subset(
                    f"{pre_a.name} && {pre_b.name}",
                    "joint_when",
                    [(pre_a.name, w_a), (pre_b.name, w_b)],
                )
                if res_pair.status == "CONDITIONAL_DEADLOCK":
                    slice_results.append(res_pair)
                    deadlocked_pairs.add((a_idx, b_idx))

        def _triple_interacts(a: int, b: int, c: int) -> bool:
            if not _rules_interact((a, b, c)):
                return False
            # Ensure third element actually connects into (a, b) rather than an unrelated bystander
            mk_a, wk_a = must_keys_by_idx[a], when_keys_by_idx[a]
            mk_b, wk_b = must_keys_by_idx[b], when_keys_by_idx[b]
            mk_c, wk_c = must_keys_by_idx[c], when_keys_by_idx[c]
            touches_quant = any(
                k.startswith("quant:") for k in (*mk_a, *mk_b, *mk_c)
            )
            if touches_quant:
                return True
            ab_keys = mk_a | mk_b | wk_a | wk_b | uncond_keys
            return bool(mk_c & ab_keys) or bool(wk_c & (mk_a | mk_b))

        # 3-way joint triggers (only among rules with interacting symbols whose pairs aren't already deadlocked)
        if len(live_cond) >= 3:
            triple_candidates = [
                (a_idx, b_idx, c_idx)
                for a_idx, b_idx, c_idx in itertools.combinations(live_cond, 3)
                if not _is_already_witnessed((a_idx, b_idx, c_idx))
                and (a_idx, b_idx) not in deadlocked_pairs
                and (a_idx, c_idx) not in deadlocked_pairs
                and (b_idx, c_idx) not in deadlocked_pairs
                and _triple_interacts(a_idx, b_idx, c_idx)
            ]
            for a_idx, b_idx, c_idx in triple_candidates[:2048]:
                if _is_already_witnessed((a_idx, b_idx, c_idx)):
                    continue
                pre_a, w_a, _, _ = compiled_pres[a_idx]
                pre_b, w_b, _, _ = compiled_pres[b_idx]
                pre_c, w_c, _, _ = compiled_pres[c_idx]
                res_triple = _test_slice_subset(
                    f"{pre_a.name} && {pre_b.name} && {pre_c.name}",
                    "joint_when",
                    [(pre_a.name, w_a), (pre_b.name, w_b), (pre_c.name, w_c)],
                )
                if res_triple.status == "CONDITIONAL_DEADLOCK":
                    slice_results.append(res_triple)

        deadlocked = [s for s in slice_results if s.status == "CONDITIONAL_DEADLOCK"]
        unknowns = [s for s in slice_results if s.status == "UNKNOWN"]
        if deadlocked:
            overall_status: Literal["REACHABLE", "TOTAL_DEADLOCK", "CONDITIONAL_DEADLOCK", "UNKNOWN"] = "CONDITIONAL_DEADLOCK"
            agg_core = deadlocked[0].unsat_core
        elif unknowns:
            overall_status = "UNKNOWN"
            agg_core = []
            frag = "UNKNOWN"
        else:
            overall_status = "REACHABLE"
            agg_core = []

        elapsed = (time.perf_counter() - t0) * 1000.0
        results.append(
            ActionReachabilityProof(
                action_name=act.name,
                implementation_kind="edit_function" if act.edit_function else "rule",
                status=overall_status,
                rule_count=len(act.preconditions),
                unsat_core=agg_core,
                conditional_slices=slice_results,
                deadlocked_slices=deadlocked,
                witness=witness,
                logic_fragment=frag,
                elapsed_ms=round(elapsed, 3),
            )
        )

    return results


# ---------------------------------------------------------------------------
# 2. Redundant / Subsumed Rule Detection
# ---------------------------------------------------------------------------
def prove_redundant_rules(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    action_names: Sequence[str] | None = None,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> list[RedundantRuleFinding]:
    """Identify precondition rules that are vacuous, tautological, or strictly implied."""
    z3 = _require_z3()
    raw_bp = _resolve_effective_blueprint(blueprint_or_spec, overlay=overlay)
    bp, ancestors_map, _ = _prepare_blueprint_for_prover(raw_bp)
    codec = _StringCodec(bp)
    target_filter = set(action_names) if action_names is not None else None
    findings: list[RedundantRuleFinding] = []

    for act in bp.actions:
        if target_filter is not None and act.name not in target_filter:
            continue
        if not act.preconditions:
            continue

        comp = _Z3Compiler(z3, bp, codec, bmc_bound=bmc_bound)
        actor, params, subjects, _ = _build_action_pre_context(comp, act)
        ref_types = _collect_action_referenced_types(act, subjects)
        ref_ancestors: set[str] = set()
        for tname in ref_types:
            ref_ancestors.update(_ancestor_closure(tname, ancestors_map))

        inv_items: list[tuple[str, Any]] = []
        for inv in bp.invariants:
            if inv.target_type not in ref_ancestors:
                continue
            matching_types = sorted(
                tname
                for tname in ref_types
                if inv.target_type in _ancestor_closure(tname, ancestors_map)
            )
            row_formulas = []
            for mt in matching_types:
                rows = comp.get_bmc_table(mt)
                for row in rows:
                    sub_ctx = {"obj": row.entity}
                    w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                    m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
                    row_formulas.append(z3.Implies(z3.And(row.present, w_z3), m_z3))
            inv_items.append(
                (inv.name, z3.And(*row_formulas) if row_formulas else z3.BoolVal(True))
            )

        compiled_pres: list[tuple[InvariantRuleSpec, Any, Any, Any]] = []
        for pre in act.preconditions:
            idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
            subj = subjects[(pre.target_type, idp)]
            p_ctx = {"obj": subj, "param": params, "actor": actor}
            w_z3 = comp.compile_bool(pre.when, p_ctx) if pre.when is not None else z3.BoolVal(True)
            m_z3 = comp.compile_bool(pre.must_hold, p_ctx)
            body_z3 = z3.Implies(w_z3, m_z3)
            compiled_pres.append((pre, w_z3, m_z3, body_z3))

        frag = comp.logic_fragment()

        for target_idx, (pre_i, w_i, m_i, _) in enumerate(compiled_pres):
            t0 = time.perf_counter()

            # Check 1: Is pre_i.when vacuous under domain axioms?
            if pre_i.when is not None:
                vac_solver = z3.Solver()
                vac_solver.set("timeout", int(timeout_ms))
                for bg in comp.background:
                    vac_solver.add(bg)
                for _, inv_f in inv_items:
                    vac_solver.add(inv_f)
                vac_solver.add(w_i)
                if vac_solver.check() == z3.unsat:
                    findings.append(
                        RedundantRuleFinding(
                            action_name=act.name,
                            rule_name=pre_i.name,
                            rule_code=pre_i.code,
                            reason="VACUOUS_WHEN_GUARD",
                            entailed_by=[],
                            logic_fragment=frag,
                            elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                        )
                    )
                    continue

            # Check 2: Ensure other rules + pre_i.when are actually satisfiable
            # (so a deadlocked branch isn't misclassified as redundant via ex falso quodlibet!)
            other_solver = z3.Solver()
            other_solver.set("timeout", int(timeout_ms))
            for bg in comp.background:
                other_solver.add(bg)
            for _, inv_f in inv_items:
                other_solver.add(inv_f)
            for j, (_, _, _, body_j) in enumerate(compiled_pres):
                if j != target_idx:
                    other_solver.add(body_j)
            other_solver.add(w_i)
            if other_solver.check() != z3.sat:
                continue

            # Check 3: Entailment UNSAT?(domain ∧ other_rules ∧ pre_i.when ∧ ¬pre_i.must_hold)
            ent_solver = z3.Solver()
            ent_solver.set("unsat_core", True)
            ent_solver.set("core.minimize", True)
            ent_solver.set("timeout", int(timeout_ms))
            for bg in comp.background:
                ent_solver.add(bg)
            for inv_name, inv_f in inv_items:
                ent_solver.assert_and_track(inv_f, z3.Bool(inv_name))
            for j, (pre_j, _, _, body_j) in enumerate(compiled_pres):
                if j != target_idx:
                    ent_solver.assert_and_track(body_j, z3.Bool(pre_j.name))
            ent_solver.add(w_i)
            ent_solver.add(z3.Not(m_i))

            if ent_solver.check() == z3.unsat:
                core = _extract_core_rule_names(ent_solver.unsat_core())
                reason: Literal["IMPLIED_BY_OTHER_RULES", "INTRINSIC_TAUTOLOGY"] = (
                    "IMPLIED_BY_OTHER_RULES" if core else "INTRINSIC_TAUTOLOGY"
                )
                findings.append(
                    RedundantRuleFinding(
                        action_name=act.name,
                        rule_name=pre_i.name,
                        rule_code=pre_i.code,
                        reason=reason,
                        entailed_by=core,
                        logic_fragment=frag,
                        elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# 3. Hoare-Triple Invariant Preservation {Pre ∧ I} Action {I'}
# ---------------------------------------------------------------------------
def _ite_is_null(z3_mod: Any, cond: Any, t_val: Any, e_val: Any) -> Any:
    if t_val is False and e_val is False:
        return False
    if t_val is True and e_val is True:
        return True
    tb = (
        z3_mod.BoolVal(True)
        if t_val is True
        else (z3_mod.BoolVal(False) if t_val is False else t_val)
    )
    eb = (
        z3_mod.BoolVal(True)
        if e_val is True
        else (z3_mod.BoolVal(False) if e_val is False else e_val)
    )
    return z3_mod.If(cond, tb, eb)


def _apply_mutations_symbolically(
    comp: _Z3Compiler,
    act: ActionBlueprint,
    actor: Mapping[str, _SymVal],
    params: Mapping[str, _SymVal],
    subjects: Mapping[tuple[str, str], _SymEntity],
    anchor: _SymEntity | None,
    *,
    ancestors_map: Mapping[str, Sequence[str]] | None = None,
) -> tuple[
    dict[str, list[_SymRow]],
    list[tuple[str, _SymEntity, Any]],
    list[tuple[str, _SymEntity, _SymEntity]],
    list[Any],
]:
    """Symbolically execute declarative mutations sequentially.

    Returns ``(post_tables, created_entities, modified_pairs, mut_guards)``,
    where each item in ``created_entities`` is ``(ot_name, sym_entity, when_guard)``.
    """
    z3 = comp.z3
    anc_map = ancestors_map or {}
    pre_tables: dict[str, list[_SymRow]] = {}
    post_tables: dict[str, list[_SymRow]] = {}
    mut_guards: list[Any] = []

    touched_types = {m.object_type for m in act.mutations if m.object_type in comp.ot_by_name}
    touched_ancestors: set[str] = set()
    for tt in touched_types:
        touched_ancestors.update(_ancestor_closure(tt, anc_map))

    inv_table_types: set[str] = set()
    for m in act.mutations:
        if m.op == "delete" and m.object_type in comp.ot_by_name:
            inv_table_types.add(m.object_type)
    for inv in comp.bp.invariants:
        inv_qtypes: set[str] = set()
        _collect_expr_refs(inv.when, set(), quant_types=inv_qtypes)
        _collect_expr_refs(inv.must_hold, set(), quant_types=inv_qtypes)
        if not inv_qtypes:
            continue
        if inv.target_type in touched_ancestors or (inv_qtypes & touched_ancestors):
            for qt in inv_qtypes:
                if qt in comp.ot_by_name:
                    inv_table_types.add(qt)
            for tt in touched_types:
                if tt in comp.ot_by_name and (
                    inv.target_type in _ancestor_closure(tt, anc_map) or tt in inv_qtypes
                ):
                    inv_table_types.add(tt)

    # Snapshot pre-state tables if mutations need global updates across instances/BMC
    def _ensure_post_table(ot_name: str) -> list[_SymRow]:
        if ot_name in post_tables:
            return post_tables[ot_name]
        base_rows = comp.get_bmc_table(ot_name)
        pre_tables[ot_name] = base_rows
        copied = [
            _SymRow(
                present=r.present,
                entity=_SymEntity(
                    ot=r.entity.ot,
                    tag=f"{r.entity.tag}_post",
                    fields=dict(r.entity.fields),
                    linked=dict(r.entity.linked),
                ),
            )
            for r in base_rows
        ]
        post_tables[ot_name] = copied
        return copied

    # Track symbolic snapshots of mutated/created entities for non_neg/FSM checks & witness decoding
    created_entities: list[tuple[str, _SymEntity, Any]] = []
    # For each named pre-state subject or created entity, store live symbolic field dicts
    live_entities: dict[int, _SymEntity] = {}
    for ent in subjects.values():
        live_entities[id(ent)] = _SymEntity(
            ot=ent.ot, tag=f"{ent.tag}_live", fields=dict(ent.fields), linked=dict(ent.linked)
        )
    modified_pairs: list[tuple[str, _SymEntity, _SymEntity]] = []

    def _get_live_copy(orig_ent: _SymEntity) -> _SymEntity:
        oid = id(orig_ent)
        if oid not in live_entities:
            live_entities[oid] = _SymEntity(
                ot=orig_ent.ot,
                tag=f"{orig_ent.tag}_live",
                fields=dict(orig_ent.fields),
                linked=dict(orig_ent.linked),
            )
        return live_entities[oid]

    for m_idx, mut in enumerate(act.mutations):
        ot_name = mut.object_type
        if ot_name not in comp.ot_by_name:
            continue
        ot = comp.ot_by_name[ot_name]

        if mut.op == "create":
            eval_ctx = {"param": params, "actor": actor}
            if anchor is not None:
                eval_ctx["source_obj"] = _get_live_copy(anchor)
            w_mut = (
                comp.compile_bool(mut.when, eval_ctx, pre_state=False, state_override=post_tables)
                if mut.when is not None
                else z3.BoolVal(True)
            )
            branch_guards: list[Any] = []
            new_ent = _SymEntity(ot=ot, tag=f"created_{ot_name}_{m_idx}")
            for fld in ot.fields:
                if fld.name in mut.set_fields:
                    val = comp.compile_value(
                        mut.set_fields[fld.name],
                        eval_ctx,
                        pre_state=False,
                        state_override=post_tables,
                        guards=branch_guards,
                    )
                    new_ent.fields[fld.name] = val
                elif fld.primary_key or fld.name == ot.primary_key:
                    if mut.id_param in params:
                        new_ent.fields[fld.name] = params[mut.id_param]
                    else:
                        new_ent.fields[fld.name] = comp.make_scalar_var(
                            f"new_{ot_name}_{m_idx}_{fld.name}", "str"
                        )
                else:
                    default_py = (
                        fld.default
                        if fld.default is not None
                        else (0 if fld.field_type == "int" else (0.0 if fld.field_type == "float" else (False if fld.field_type == "bool" else "")))
                    )
                    new_ent.fields[fld.name] = comp._literal_val(default_py)
            if ot.primary_key not in new_ent.fields:
                new_ent.fields[ot.primary_key] = (
                    params[mut.id_param]
                    if mut.id_param in params
                    else comp.make_scalar_var(f"new_{ot_name}_{m_idx}_{ot.primary_key}", "str")
                )
            for rel in ot.relations:
                if rel.field in new_ent.fields:
                    continue
                if rel.field in mut.set_fields:
                    new_ent.fields[rel.field] = comp.compile_value(
                        mut.set_fields[rel.field],
                        eval_ctx,
                        pre_state=False,
                        state_override=post_tables,
                        guards=branch_guards,
                    )
                else:
                    new_ent.fields[rel.field] = comp._literal_val(None if not rel.required else "")

            for bg in branch_guards:
                mut_guards.append(z3.Implies(w_mut, bg) if mut.when is not None else bg)

            created_entities.append((ot_name, new_ent, w_mut))
            # If post_tables already exists or quantified invariants scan this type, append created row
            if ot_name in inv_table_types or ot_name in post_tables or ot_name in comp.bmc_tables:
                rows = _ensure_post_table(ot_name)
                rows.append(_SymRow(present=w_mut, entity=new_ent))
            continue

        # modify or delete
        if mut.target_ref is not None:
            ref_parts = mut.target_ref.split(".")
            if ref_parts[0] == "obj":
                if anchor is None:
                    continue
                linked_orig, _fk_null = comp.resolve_linked_entity(anchor, ref_parts[1])
                orig_target = linked_orig
                for seg in ref_parts[2:]:
                    orig_target, _ = comp.resolve_linked_entity(orig_target, seg)
                target_pk_sv = orig_target.fields[orig_target.ot.primary_key]
                target_live = _get_live_copy(orig_target)
                anchor_live = _get_live_copy(anchor)
                eval_ctx = {
                    "obj": target_live,
                    "source_obj": anchor_live,
                    "param": params,
                    "actor": actor,
                }
            else:
                # param.<p>
                p_name = ref_parts[1]
                orig_target = subjects.get((ot_name, p_name))
                if orig_target is None:
                    orig_target = comp.build_entity(ot_name, f"mut_{ot_name}_{p_name}")
                    if p_name in params:
                        comp.background.append(
                            orig_target.fields[ot.primary_key].expr == params[p_name].expr
                        )
                    subjects_key_ent = orig_target
                else:
                    subjects_key_ent = orig_target
                target_pk_sv = subjects_key_ent.fields[ot.primary_key]
                target_live = _get_live_copy(subjects_key_ent)
                eval_ctx = {"obj": target_live, "param": params, "actor": actor}
        else:
            key = (ot_name, mut.id_param)
            if key in subjects:
                orig_target = subjects[key]
            else:
                orig_target = comp.build_entity(ot_name, f"mut_{ot_name}_{mut.id_param}")
                if mut.id_param in params:
                    comp.background.append(
                        orig_target.fields[ot.primary_key].expr == params[mut.id_param].expr
                    )
            target_pk_sv = orig_target.fields[ot.primary_key]
            target_live = _get_live_copy(orig_target)
            eval_ctx = {"obj": target_live, "param": params, "actor": actor}

        w_mut = (
            comp.compile_bool(mut.when, eval_ctx, pre_state=False, state_override=post_tables)
            if mut.when is not None
            else z3.BoolVal(True)
        )

        if mut.op == "delete":
            rows = _ensure_post_table(ot_name)
            for r in rows:
                r.present = z3.If(
                    w_mut,
                    z3.And(r.present, r.entity.fields[ot.primary_key].expr != target_pk_sv.expr),
                    r.present,
                )
            continue

        # Compute new field values before McCarthy / guarded update
        branch_guards = []
        computed_updates: dict[str, _SymVal] = {}
        for f_name, f_ast in mut.set_fields.items():
            computed_updates[f_name] = comp.compile_value(
                f_ast,
                eval_ctx,
                pre_state=False,
                state_override=post_tables,
                guards=branch_guards,
            )
        for bg in branch_guards:
            mut_guards.append(z3.Implies(w_mut, bg) if mut.when is not None else bg)

        # Update matching target_live AND any aliased live_entities of same type via McCarthy If
        target_pk_expr = target_pk_sv.expr
        for _, live_ent in list(live_entities.items()):
            if live_ent.ot.name != ot_name:
                continue
            same_pk = live_ent.fields[ot.primary_key].expr == target_pk_expr
            hit_cond = w_mut if live_ent is target_live else z3.And(w_mut, same_pk)
            for f_name, new_sv in computed_updates.items():
                if f_name not in live_ent.fields:
                    continue
                old_sv = live_ent.fields[f_name]
                if live_ent is target_live and mut.when is None:
                    live_ent.fields[f_name] = new_sv
                else:
                    if new_sv.kind in ("int", "float") and old_sv.kind in ("int", "float"):
                        ne_, oe_, rk = comp._coerce_arith_pair(new_sv, old_sv)
                        live_ent.fields[f_name] = _SymVal(
                            z3.If(hit_cond, ne_, oe_),
                            rk,
                            is_null=_ite_is_null(z3, hit_cond, new_sv.is_null, old_sv.is_null),
                        )
                    else:
                        live_ent.fields[f_name] = _SymVal(
                            z3.If(hit_cond, new_sv.expr, old_sv.expr),
                            new_sv.kind if new_sv.kind != "null" else old_sv.kind,
                            is_null=_ite_is_null(z3, hit_cond, new_sv.is_null, old_sv.is_null),
                        )

        # Update post_tables via McCarthy If only when this type participates in global invariants/aggregates
        if ot_name in inv_table_types or ot_name in post_tables or ot_name in comp.bmc_tables:
            rows = _ensure_post_table(ot_name)
            for row in rows:
                same_pk = z3.And(
                    row.present, w_mut, row.entity.fields[ot.primary_key].expr == target_pk_expr
                )
                for f_name, new_sv in computed_updates.items():
                    if f_name not in row.entity.fields:
                        continue
                    old_sv = row.entity.fields[f_name]
                    if new_sv.kind in ("int", "float") and old_sv.kind in ("int", "float"):
                        ne_, oe_, rk = comp._coerce_arith_pair(new_sv, old_sv)
                        row.entity.fields[f_name] = _SymVal(
                            z3.If(same_pk, ne_, oe_),
                            rk,
                            is_null=_ite_is_null(z3, same_pk, new_sv.is_null, old_sv.is_null),
                        )
                    else:
                        row.entity.fields[f_name] = _SymVal(
                            z3.If(same_pk, new_sv.expr, old_sv.expr),
                            new_sv.kind if new_sv.kind != "null" else old_sv.kind,
                            is_null=_ite_is_null(z3, same_pk, new_sv.is_null, old_sv.is_null),
                        )

        # Record pair for FSM / non_negative verification
        if not any(o is orig_target for _, o, _ in modified_pairs):
            modified_pairs.append((ot_name, orig_target, target_live))

    return post_tables, created_entities, modified_pairs, mut_guards


def _make_raw_havoc_val(
    comp: _Z3Compiler,
    ot: ObjectTypeBlueprint,
    fname: str,
    tag: str,
) -> _SymVal:
    """Allocate an unconstrained post-state symbolic field (no non_negative or FSM axioms)."""
    z3 = comp.z3
    for fld in ot.fields:
        if fld.name == fname:
            sv = comp.make_scalar_var(tag, fld.field_type, add_domain_axioms=False)
            if fld.field_type == "str":
                comp.background.append(z3.And(sv.expr >= 1, sv.expr < comp.codec.max_id))
            return sv
    for rel in ot.relations:
        if rel.field == fname:
            nullable = (not rel.required) and (not rel.multiple)
            sv = comp.make_scalar_var(tag, "str", nullable=nullable, add_domain_axioms=False)
            min_code = 0 if nullable else 1
            comp.background.append(z3.And(sv.expr >= min_code, sv.expr < comp.codec.max_id))
            return sv
    sv = comp.make_scalar_var(tag, "str", add_domain_axioms=False)
    comp.background.append(z3.And(sv.expr >= 1, sv.expr < comp.codec.max_id))
    return sv


def _prove_edit_function_contract_action(
    z3: Any,
    bp: DeclarativeBlueprint,
    act: ActionBlueprint,
    codec: _StringCodec,
    ancestors_map: Mapping[str, tuple[str, ...]],
    *,
    bmc_bound: int,
    timeout_ms: int,
) -> InvariantPreservationProof:
    """Modular Assume-Guarantee verification for a contracted ``EditFunction`` action.

    1. **PO-5 Realizability**: ``SAT(Background ∧ Pre(before) ∧ I(before) ∧ Frame ∧ Ensures)``;
       if ``UNSAT``, return ``VACUOUS_CONTRACT`` with minimized ``unsat_core``.
    2. **PO-4b Preservation**: ``UNSAT(Background ∧ Pre(before) ∧ I(before) ∧ Frame ∧ Ensures ∧ ¬I(after))``;
       return ``CONTRACT_PROVED`` or ``VIOLATED`` with counterexample.
    """
    t0 = time.perf_counter()
    comp = _Z3Compiler(z3, bp, codec, bmc_bound=bmc_bound)
    actor = comp.build_actor(act.allowed_roles)
    params = comp.build_params(act)

    def _is_field_modified(ot_name: str, fname: str) -> bool:
        return any(
            f"{anc}.{fname}" in act.modifies or f"{anc}.*" in act.modifies
            for anc in _ancestor_closure(ot_name, ancestors_map)
        )

    modified_ot_names = sorted(
        ot.name
        for ot in bp.object_types
        if any(_is_field_modified(ot.name, f.name) for f in ot.fields)
        or any(_is_field_modified(ot.name, r.field) for r in ot.relations)
    )
    created_types_set = set(act.creates)
    deleted_types_set = set(act.deletes)

    # Materialize keyed subjects from preconditions AND keyed ensures
    pre_subjects: dict[tuple[str, str], _SymEntity] = {}
    anchor: _SymEntity | None = None
    for pre in act.preconditions:
        if pre.target_type not in comp.ot_by_name:
            continue
        idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
        key = (pre.target_type, idp)
        if key not in pre_subjects:
            ent = comp.build_entity(pre.target_type, f"subj_{pre.target_type}_{idp}")
            if idp in params:
                comp.background.append(ent.fields[ent.ot.primary_key].expr == params[idp].expr)
            pre_subjects[key] = ent
            if anchor is None:
                anchor = ent

    for ens in act.ensures:
        if ens.id_param and ens.target_type in comp.ot_by_name:
            key = (ens.target_type, ens.id_param)
            if key not in pre_subjects:
                ent = comp.build_entity(ens.target_type, f"subj_{ens.target_type}_{ens.id_param}")
                if ens.id_param in params:
                    comp.background.append(ent.fields[ent.ot.primary_key].expr == params[ens.id_param].expr)
                pre_subjects[key] = ent
                if anchor is None:
                    anchor = ent

    mutated_or_created_types = (
        set(modified_ot_names)
        | created_types_set
        | deleted_types_set
        | {ens.target_type for ens in act.ensures if ens.target_type in comp.ot_by_name}
        | {ot_n for ot_n, _ in pre_subjects.keys()}
    )
    mutated_ancestors: set[str] = set()
    for mt in mutated_or_created_types:
        if mt in comp.ot_by_name:
            mutated_ancestors.update(_ancestor_closure(mt, ancestors_map))

    # Pre-initialize BMC tables for all touched/created/deleted/invariant-relevant types
    needed_table_types: set[str] = {t for t in mutated_or_created_types if t in comp.ot_by_name}
    applicable_invs: list[InvariantRuleSpec] = []
    for inv in bp.invariants:
        inv_qtypes: set[str] = set()
        _collect_expr_refs(inv.when, set(), quant_types=inv_qtypes)
        _collect_expr_refs(inv.must_hold, set(), quant_types=inv_qtypes)
        if inv.target_type in mutated_ancestors or (inv_qtypes & mutated_ancestors):
            applicable_invs.append(inv)
            if inv.target_type in comp.ot_by_name:
                needed_table_types.add(inv.target_type)
            for ot in bp.object_types:
                if inv.target_type in _ancestor_closure(ot.name, ancestors_map):
                    needed_table_types.add(ot.name)
            for qt in inv_qtypes:
                if qt in comp.ot_by_name:
                    needed_table_types.add(qt)
    for ens in act.ensures:
        qtypes_e: set[str] = set()
        _collect_expr_refs(ens.when, set(), quant_types=qtypes_e)
        _collect_expr_refs(ens.must_hold, set(), quant_types=qtypes_e)
        for qt in qtypes_e:
            if qt in comp.ot_by_name:
                needed_table_types.add(qt)

    for tbl_t in sorted(needed_table_types):
        comp.get_bmc_table(tbl_t)

    # Clone pre BMC tables into post_tables preserving untouched fields via identity (Frame Axiom)
    post_tables: dict[str, list[_SymRow]] = {}
    for tname, rows in comp.bmc_tables.items():
        copied_rows: list[_SymRow] = []
        for r in rows:
            post_present = r.present
            if deleted_types_set & set(_ancestor_closure(tname, ancestors_map)):
                keep_b = z3.Bool(comp.next_name(f"keep_{tname}"))
                post_present = z3.And(r.present, keep_b)
            copied_rows.append(
                _SymRow(
                    present=post_present,
                    entity=_SymEntity(
                        ot=r.entity.ot,
                        tag=f"{r.entity.tag}_post",
                        fields=dict(r.entity.fields),
                        linked=dict(r.entity.linked),
                    ),
                )
            )
        post_tables[tname] = copied_rows

    # Identify which modified types are keyed-only (single instance mutated via id_param)
    unkeyed_before_types: set[str] = set()
    for ens in act.ensures:
        if ens.id_param is None:
            refs_e: set[str] = set()
            _collect_expr_refs(ens.when, refs_e)
            _collect_expr_refs(ens.must_hold, refs_e)
            if any(r.startswith("before.") for r in refs_e):
                unkeyed_before_types.add(ens.target_type)

    keyed_param_for_type: dict[str, tuple[str, str]] = {}
    for ot_name in modified_ot_names:
        ancs = _ancestor_closure(ot_name, ancestors_map)
        if any(ut in ancs for ut in unkeyed_before_types):
            continue
        for (subj_t, idp) in pre_subjects:
            if subj_t in ancs and idp in params:
                keyed_param_for_type[ot_name] = (subj_t, idp)
                break

    # Apply havoc on declared modified fields, keeping exact pre-state on untouched fields
    modified_records: list[tuple[str, _SymEntity, _SymEntity, Any]] = []
    for ot_name in modified_ot_names:
        ot = comp.ot_by_name[ot_name]
        pre_rows = comp.bmc_tables[ot_name]
        post_rows = post_tables[ot_name]
        key_pair = keyed_param_for_type.get(ot_name)
        for r_idx, (pre_r, post_r) in enumerate(zip(pre_rows, post_rows)):
            if key_pair is not None:
                _, idp = key_pair
                hit = z3.And(pre_r.present, pre_r.entity.fields[ot.primary_key].expr == params[idp].expr)
            else:
                hit = pre_r.present
            for fname in list(post_r.entity.fields.keys()):
                if fname == ot.primary_key or not _is_field_modified(ot_name, fname):
                    continue
                havoc_sv = _make_raw_havoc_val(
                    comp, ot, fname, f"havoc_{ot_name}_{r_idx}_{fname}"
                )
                old_sv = pre_r.entity.fields[fname]
                if key_pair is not None:
                    post_r.entity.fields[fname] = _SymVal(
                        z3.If(hit, havoc_sv.expr, old_sv.expr),
                        havoc_sv.kind,
                        is_null=_ite_is_null(z3, hit, havoc_sv.is_null, old_sv.is_null),
                    )
                else:
                    post_r.entity.fields[fname] = havoc_sv
            modified_records.append((ot_name, pre_r.entity, post_r.entity, hit))

    # Build post-state views of keyed subjects so ensures + counterexample decode see exact writes
    post_subjects: dict[tuple[str, str], _SymEntity] = {}
    for (st, idp), pre_s in pre_subjects.items():
        if st in post_tables:
            post_subjects[(st, idp)] = comp._overlay_entity_lookup(
                pre_s.fields[pre_s.ot.primary_key], pre_s, post_tables[st]
            )
        else:
            post_subjects[(st, idp)] = pre_s

    # Materialize symbolic newly created entities for act.creates (without assuming post-invariants)
    created_entities: list[tuple[str, _SymEntity, Any]] = []
    for c_idx, c_type in enumerate(act.creates):
        if c_type not in comp.ot_by_name:
            continue
        c_ot = comp.ot_by_name[c_type]
        c_ent = _SymEntity(ot=c_ot, tag=f"created_{c_idx}_{c_type}")
        seen_c: set[str] = set()
        for fld in c_ot.fields:
            seen_c.add(fld.name)
            c_ent.fields[fld.name] = _make_raw_havoc_val(
                comp, c_ot, fld.name, f"cre_{c_idx}_{c_type}_{fld.name}"
            )
        if c_ot.primary_key not in seen_c:
            c_ent.fields[c_ot.primary_key] = _make_raw_havoc_val(
                comp, c_ot, c_ot.primary_key, f"cre_{c_idx}_{c_type}_{c_ot.primary_key}"
            )
        for rel in c_ot.relations:
            if rel.field not in c_ent.fields:
                c_ent.fields[rel.field] = _make_raw_havoc_val(
                    comp, c_ot, rel.field, f"cre_{c_idx}_{c_type}_{rel.field}"
                )
        # Fresh PK distinct from existing rows of c_type
        pk_name = c_ot.primary_key
        for pre_r in comp.bmc_tables.get(c_type, ()):
            comp.background.append(
                z3.Implies(pre_r.present, c_ent.fields[pk_name].expr != pre_r.entity.fields[pk_name].expr)
            )
        c_guard = z3.BoolVal(True)
        post_tables.setdefault(c_type, []).append(_SymRow(present=c_guard, entity=c_ent))
        created_entities.append((c_type, c_ent, c_guard))

    # Compile tracked assumptions for PO-5 Realizability and PO-4b Preservation
    tracked_items: list[tuple[str, Any]] = []

    # 1) Preconditions Pre(before)
    for pre in act.preconditions:
        idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
        subj = pre_subjects[(pre.target_type, idp)]
        p_ctx = {"obj": subj, "param": params, "actor": actor}
        w_z3 = comp.compile_bool(pre.when, p_ctx) if pre.when is not None else z3.BoolVal(True)
        m_z3 = comp.compile_bool(pre.must_hold, p_ctx)
        tracked_items.append((f"pre:{pre.name}", z3.Implies(w_z3, m_z3)))

    # 2) Pre-state invariants I(before) across pre_subjects and pre BMC tables
    pre_entities_all: list[_SymEntity] = []
    seen_ids: set[int] = set()
    for ent in pre_subjects.values():
        if id(ent) not in seen_ids:
            seen_ids.add(id(ent))
            pre_entities_all.append(ent)

    for inv in applicable_invs:
        rforms: list[Any] = []
        for pre_ent in pre_entities_all:
            if inv.target_type in _ancestor_closure(pre_ent.ot.name, ancestors_map):
                sub_ctx = {"obj": pre_ent}
                w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
                rforms.append(z3.Implies(w_z3, m_z3))
        for bt in sorted(comp.bmc_tables.keys()):
            if inv.target_type in _ancestor_closure(bt, ancestors_map):
                for row in comp.bmc_tables[bt]:
                    sub_ctx = {"obj": row.entity}
                    w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                    m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
                    rforms.append(z3.Implies(z3.And(row.present, w_z3), m_z3))
        if rforms:
            tracked_items.append((f"inv_pre:{inv.name}", z3.And(*rforms)))

    # 3) Postconditions Ensures(before, after)
    for ens in act.ensures:
        if ens.id_param and (ens.target_type, ens.id_param) in pre_subjects:
            pre_s = pre_subjects[(ens.target_type, ens.id_param)]
            post_s = post_subjects[(ens.target_type, ens.id_param)]
            ectx = {
                "before": pre_s,
                "after": post_s,
                "obj": post_s,
                "param": params,
                "actor": actor,
            }
            w_e = (
                comp.compile_bool(ens.when, ectx, pre_state=False, state_override=post_tables)
                if ens.when is not None
                else z3.BoolVal(True)
            )
            m_e = comp.compile_bool(ens.must_hold, ectx, pre_state=False, state_override=post_tables)
            ens_forms = [z3.Implies(w_e, m_e)]
            # Propagate keyed ensures cleanly to any modified concrete subtypes matching params[id_param]
            for sub_t in modified_ot_names:
                if sub_t != ens.target_type and ens.target_type in ancestors_map.get(sub_t, ()):
                    sub_ot = comp.ot_by_name[sub_t]
                    idp = ens.id_param
                    for pre_r, post_r in zip(comp.bmc_tables[sub_t], post_tables[sub_t]):
                        hit_sub = (
                            z3.And(
                                pre_r.present,
                                pre_r.entity.fields[sub_ot.primary_key].expr == params[idp].expr,
                            )
                            if idp in params
                            else pre_r.present
                        )
                        sctx = {
                            "before": pre_r.entity,
                            "after": post_r.entity,
                            "obj": post_r.entity,
                            "param": params,
                            "actor": actor,
                        }
                        sw = (
                            comp.compile_bool(ens.when, sctx, pre_state=False, state_override=post_tables)
                            if ens.when is not None
                            else z3.BoolVal(True)
                        )
                        sm = comp.compile_bool(
                            ens.must_hold, sctx, pre_state=False, state_override=post_tables
                        )
                        ens_forms.append(z3.Implies(z3.And(hit_sub, sw), sm))
            tracked_items.append((f"ensures:{ens.name}", z3.And(*ens_forms)))
        else:
            ens_refs: set[str] = set()
            _collect_expr_refs(ens.when, ens_refs)
            _collect_expr_refs(ens.must_hold, ens_refs)
            uses_before = any(r.startswith("before.") for r in ens_refs)
            matching_created = [
                (c_ot, c_ent, c_guard)
                for c_ot, c_ent, c_guard in created_entities
                if ens.target_type in _ancestor_closure(c_ot, ancestors_map)
            ]
            ens_forms = []
            if matching_created and not uses_before:
                for _c_ot, c_ent, c_guard in matching_created:
                    ectx = {
                        "before": c_ent,
                        "after": c_ent,
                        "obj": c_ent,
                        "param": params,
                        "actor": actor,
                    }
                    w_e = (
                        comp.compile_bool(ens.when, ectx, pre_state=False, state_override=post_tables)
                        if ens.when is not None
                        else z3.BoolVal(True)
                    )
                    m_e = comp.compile_bool(
                        ens.must_hold, ectx, pre_state=False, state_override=post_tables
                    )
                    ens_forms.append(z3.Implies(z3.And(c_guard, w_e), m_e))
            else:
                target_tb_names = sorted(
                    t
                    for t in set(modified_ot_names) | ({ens.target_type} if ens.target_type in comp.bmc_tables else set())
                    if t in comp.bmc_tables and ens.target_type in _ancestor_closure(t, ancestors_map)
                )
                for tb_name in target_tb_names:
                    for pre_r, post_r in zip(comp.bmc_tables[tb_name], post_tables[tb_name]):
                        ectx = {
                            "before": pre_r.entity,
                            "after": post_r.entity,
                            "obj": post_r.entity,
                            "param": params,
                            "actor": actor,
                        }
                        w_e = (
                            comp.compile_bool(ens.when, ectx, pre_state=False, state_override=post_tables)
                            if ens.when is not None
                            else z3.BoolVal(True)
                        )
                        m_e = comp.compile_bool(
                            ens.must_hold, ectx, pre_state=False, state_override=post_tables
                        )
                        ens_forms.append(z3.Implies(z3.And(pre_r.present, w_e), m_e))
            if ens_forms:
                tracked_items.append((f"ensures:{ens.name}", z3.And(*ens_forms)))

    # Compile Obligations A, B, C, D BEFORE running PO-5 so comp.background is 100% materialized
    obligations: list[tuple[str, str, Any]] = []

    # Obligation A: non_negative on created entities and modified fields
    for ot_name, new_ent, c_guard in created_entities:
        for fld in new_ent.ot.fields:
            if fld.non_negative:
                fv = new_ent.fields[fld.name]
                if fv.kind == "float" or z3.is_real(fv.expr):
                    comp.used_real = True
                    cond = fv.expr >= z3.RealVal("0")
                else:
                    cond = fv.expr >= 0
                obligations.append(
                    (f"{ot_name}.{fld.name}_non_negative", "NEGATIVE_VALUE", z3.Implies(c_guard, cond))
                )

    seen_mod_nn: set[tuple[str, str]] = set()
    for ot_name, _pre_ent, post_ent, hit in modified_records:
        for fld in post_ent.ot.fields:
            if fld.non_negative and _is_field_modified(ot_name, fld.name):
                fv = post_ent.fields[fld.name]
                if fv.kind == "float" or z3.is_real(fv.expr):
                    comp.used_real = True
                    cond = fv.expr >= z3.RealVal("0")
                else:
                    cond = fv.expr >= 0
                ob_key = (ot_name, fld.name)
                if ob_key not in seen_mod_nn:
                    seen_mod_nn.add(ob_key)
                obligations.append(
                    (
                        f"{ot_name}.{fld.name}_non_negative",
                        "NEGATIVE_VALUE",
                        z3.Implies(hit, cond),
                    )
                )

    # Collapse per-(ot_name, fld_name) non_negative obligations across BMC rows for concise reporting
    grouped_nn: dict[tuple[str, str, str], list[Any]] = {}
    other_obs: list[tuple[str, str, Any]] = []
    for oname, ocode, oform in obligations:
        if ocode == "NEGATIVE_VALUE":
            grouped_nn.setdefault((oname, ocode, oname), []).append(oform)
        else:
            other_obs.append((oname, ocode, oform))
    obligations = [
        (oname, ocode, z3.And(*forms))
        for (oname, ocode, _), forms in grouped_nn.items()
    ] + other_obs

    # Obligation B: FSM initial statuses & transitions
    for ot_name, new_ent, c_guard in created_entities:
        ot = new_ent.ot
        if ot.initial_statuses is not None:
            sf = new_ent.fields.get(ot.status_field)
            if sf is not None:
                allowed = [sf.expr == codec.encode(s) for s in ot.initial_statuses]
                obligations.append(
                    (
                        f"{ot_name}.initial_{ot.status_field}",
                        "ILLEGAL_INITIAL_STATUS",
                        z3.Implies(c_guard, z3.Or(*allowed) if allowed else z3.BoolVal(False)),
                    )
                )

    fsm_by_type: dict[str, list[Any]] = {}
    fsm_field_by_type: dict[str, str] = {}
    for ot_name, pre_ent, post_ent, hit in modified_records:
        ot = post_ent.ot
        if ot.transitions and _is_field_modified(ot_name, ot.status_field):
            sf_before = pre_ent.fields.get(ot.status_field)
            sf_after = post_ent.fields.get(ot.status_field)
            if sf_before is not None and sf_after is not None:
                edge_disj = [sf_before.expr == sf_after.expr]
                for src_s, dst_list in ot.transitions.items():
                    src_c = codec.encode(src_s)
                    for dst_s in dst_list:
                        edge_disj.append(
                            z3.And(sf_before.expr == src_c, sf_after.expr == codec.encode(dst_s))
                        )
                fsm_field_by_type[ot_name] = ot.status_field
                fsm_by_type.setdefault(ot_name, []).append(z3.Implies(hit, z3.Or(*edge_disj)))
    for ot_name, forms in fsm_by_type.items():
        obligations.append(
            (
                f"{ot_name}.fsm_{fsm_field_by_type[ot_name]}",
                "ILLEGAL_STATE_TRANSITION",
                z3.And(*forms),
            )
        )

    # Obligation C: voucher_delta_rules balance conservation
    for ot in bp.object_types:
        if not ot.voucher_delta_rules:
            continue
        for bal_field, (v_type, v_link, v_delta) in ot.voucher_delta_rules.items():
            if ot.name not in mutated_ancestors and v_type not in mutated_ancestors:
                continue
            for m_ot, pre_ent, post_ent, hit in modified_records:
                if ot.name not in _ancestor_closure(m_ot, ancestors_map):
                    continue
                if not _is_field_modified(m_ot, bal_field):
                    continue
                bf = pre_ent.fields[bal_field]
                af = post_ent.fields[bal_field]
                be, ae, _ = comp._coerce_arith_pair(bf, af)
                ledger_delta = ae - be
                pk_expr = pre_ent.fields[pre_ent.ot.primary_key].expr
                v_terms = []
                for c_ot, c_ent, c_guard in created_entities:
                    if v_type not in _ancestor_closure(c_ot, ancestors_map):
                        continue
                    link_sv = c_ent.fields.get(v_link)
                    delta_sv = c_ent.fields.get(v_delta)
                    if link_sv is not None and delta_sv is not None:
                        v_terms.append(
                            z3.If(
                                z3.And(c_guard, link_sv.expr == pk_expr),
                                z3.ToReal(delta_sv.expr)
                                if not z3.is_real(delta_sv.expr)
                                else delta_sv.expr,
                                z3.RealVal("0"),
                            )
                        )
                v_sum = z3.Sum(*v_terms) if v_terms else z3.RealVal("0")
                ld_real = z3.ToReal(ledger_delta) if not z3.is_real(ledger_delta) else ledger_delta
                diff_abs = ld_real - v_sum
                obligations.append(
                    (
                        f"{ot.name}.{bal_field}_voucher_conservation",
                        "UNBACKED_BALANCE_MUTATION",
                        z3.Implies(
                            hit,
                            z3.And(diff_abs <= z3.RealVal("1e-9"), diff_abs >= z3.RealVal("-1e-9")),
                        ),
                    )
                )

    # Obligation D: Declarative invariants I(after)
    for inv in applicable_invs:
        post_formulas: list[Any] = []
        checked_table_types = sorted(
            t
            for t in (set(post_tables.keys()) | set(comp.bmc_tables.keys()))
            if inv.target_type in _ancestor_closure(t, ancestors_map)
        )
        for tb_type in checked_table_types:
            rows = post_tables.get(tb_type)
            if rows is None:
                rows = comp.bmc_tables.get(tb_type)
            if rows is not None:
                for row in rows:
                    sub_ctx = {"obj": row.entity}
                    w_post = (
                        comp.compile_bool(inv.when, sub_ctx, pre_state=False, state_override=post_tables)
                        if inv.when
                        else z3.BoolVal(True)
                    )
                    m_post = comp.compile_bool(
                        inv.must_hold, sub_ctx, pre_state=False, state_override=post_tables
                    )
                    post_formulas.append(z3.Implies(z3.And(row.present, w_post), m_post))
        if post_formulas:
            obligations.append((inv.name, inv.code, z3.And(*post_formulas)))

    # Step PO-5: Anti-Vacuous Contract Realizability
    po5_solver = z3.Solver()
    po5_solver.set("unsat_core", True)
    po5_solver.set("core.minimize", True)
    po5_solver.set("timeout", int(timeout_ms))
    for bg in comp.background:
        po5_solver.add(bg)
    track_map: dict[str, str] = {}
    for t_name, t_form in tracked_items:
        t_bool = z3.Bool(comp.next_name(f"po5_{t_name}"))
        track_map[str(t_bool)] = t_name
        po5_solver.assert_and_track(t_form, t_bool)

    po5_chk = po5_solver.check()
    frag = comp.logic_fragment()
    if po5_chk == z3.unknown:
        return InvariantPreservationProof(
            action_name=act.name,
            status="UNKNOWN",
            proof_kind="contract_assume_guarantee",
            logic_fragment="UNKNOWN",
            detail="Z3 returned unknown during PO-5 contract realizability check.",
            elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )
    if po5_chk == z3.unsat:
        raw_core = [track_map[str(b)] for b in po5_solver.unsat_core() if str(b) in track_map]
        if not raw_core:
            raw_core = [name for name, _ in tracked_items]
        min_core = _minimize_class_unsat_core(z3, comp.background, tracked_items, raw_core, timeout_ms)
        return InvariantPreservationProof(
            action_name=act.name,
            status="VACUOUS_CONTRACT",
            proof_kind="contract_assume_guarantee",
            unsat_core=min_core,
            logic_fragment=frag,
            detail=f"Contract is unrealizable (PO-5 UNSAT); minimal unsat_core={min_core}",
            elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    # Step PO-4b: Modular Hoare Preservation
    check_solver = z3.Solver()
    check_solver.set("timeout", int(timeout_ms))
    for bg in comp.background:
        check_solver.add(bg)
    for _, t_form in tracked_items:
        check_solver.add(t_form)

    checked_names: list[str] = []
    violated_names: list[str] = []
    counterexamples: list[InvariantCounterexample] = []
    any_unknown = False

    for ob_name, ob_code, post_holds in obligations:
        checked_names.append(ob_name)
        check_solver.push()
        check_solver.add(z3.Not(post_holds))
        c_res = check_solver.check()
        if c_res == z3.sat:
            mdl = check_solver.model()
            check_solver.pop()
            violated_names.append(ob_name)
            before_map: dict[str, dict[str, Any]] = {
                f"{ot_n}:{idp}": _decode_entity(z3, mdl, ent, codec)
                for (ot_n, idp), ent in pre_subjects.items()
            }
            after_map: dict[str, dict[str, Any]] = {
                f"{ot_n}:{idp}(after)": _decode_entity(z3, mdl, ent, codec)
                for (ot_n, idp), ent in post_subjects.items()
            }
            for m_idx, (ot_n, b_ent, a_ent, hit) in enumerate(modified_records):
                if _eval_model_bool(z3, mdl, hit):
                    b_dec = _decode_entity(z3, mdl, b_ent, codec)
                    a_dec = _decode_entity(z3, mdl, a_ent, codec)
                    if f"{ot_n}(before_{m_idx})" not in before_map:
                        before_map.setdefault(f"{ot_n}({m_idx})", b_dec)
                    after_map.setdefault(f"{ot_n}(modified_{m_idx})", a_dec)
            for idx, (ot_n, c_ent, c_guard) in enumerate(created_entities):
                if _eval_model_bool(z3, mdl, c_guard):
                    after_map[f"{ot_n}(created_{idx})"] = _decode_entity(z3, mdl, c_ent, codec)
            counterexamples.append(
                InvariantCounterexample(
                    violated_invariant=ob_name,
                    violated_code=ob_code,
                    actor={k: _decode_z3_scalar(z3, mdl, v, codec) for k, v in actor.items()},
                    params={k: _decode_z3_scalar(z3, mdl, v, codec) for k, v in params.items()},
                    before=before_map,
                    after=after_map,
                )
            )
        elif c_res == z3.unknown:
            check_solver.pop()
            any_unknown = True
        else:
            check_solver.pop()

    if violated_names:
        status: Literal[
            "PROVED",
            "CONTRACT_PROVED",
            "VIOLATED",
            "VACUOUS_CONTRACT",
            "VACUOUS_DEADLOCK",
            "SKIPPED_UNCONTRACTED",
            "UNKNOWN",
        ] = "VIOLATED"
    elif any_unknown:
        status = "UNKNOWN"
        frag = "UNKNOWN"
    else:
        status = "CONTRACT_PROVED"

    elapsed = (time.perf_counter() - t0) * 1000.0
    return InvariantPreservationProof(
        action_name=act.name,
        status=status,
        proof_kind="contract_assume_guarantee",
        checked_invariants=checked_names,
        violated_invariants=violated_names,
        counterexamples=counterexamples,
        logic_fragment=frag,
        elapsed_ms=round(elapsed, 3),
    )


def prove_invariant_preservation(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    action_names: Sequence[str] | None = None,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> list[InvariantPreservationProof]:
    """Prove inductive invariant preservation ``{Pre ∧ I} Action {I'}`` via exact wp or modular contracts."""
    z3 = _require_z3()
    raw_bp = _resolve_effective_blueprint(blueprint_or_spec, overlay=overlay)
    bp, ancestors_map, _ = _prepare_blueprint_for_prover(raw_bp)
    codec = _StringCodec(bp)
    target_filter = set(action_names) if action_names is not None else None
    results: list[InvariantPreservationProof] = []

    for act in bp.actions:
        if target_filter is not None and act.name not in target_filter:
            continue
        if act.edit_function is not None:
            has_contract = bool(act.modifies or act.creates or act.deletes or act.ensures)
            if not has_contract:
                results.append(
                    InvariantPreservationProof(
                        action_name=act.name,
                        status="SKIPPED_UNCONTRACTED",
                        proof_kind="skipped",
                        logic_fragment="EXACT_QF_LIA",
                        detail=(
                            f"EditFunction {act.edit_function!r} has no behavioral contract "
                            "(modifies/creates/deletes/ensures) declared."
                        ),
                    )
                )
            else:
                results.append(
                    _prove_edit_function_contract_action(
                        z3,
                        bp,
                        act,
                        codec,
                        ancestors_map,
                        bmc_bound=bmc_bound,
                        timeout_ms=timeout_ms,
                    )
                )
            continue
        if not act.mutations:
            continue

        t0 = time.perf_counter()
        comp = _Z3Compiler(z3, bp, codec, bmc_bound=bmc_bound)
        actor, params, subjects, anchor = _build_action_pre_context(comp, act)

        pre_formulas: list[Any] = []
        for pre in act.preconditions:
            idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
            subj = subjects[(pre.target_type, idp)]
            p_ctx = {"obj": subj, "param": params, "actor": actor}
            w_z3 = comp.compile_bool(pre.when, p_ctx) if pre.when is not None else z3.BoolVal(True)
            m_z3 = comp.compile_bool(pre.must_hold, p_ctx)
            pre_formulas.append(z3.Implies(w_z3, m_z3))

        post_tables, created_entities, modified_pairs, mut_guards = _apply_mutations_symbolically(
            comp, act, actor, params, subjects, anchor, ancestors_map=ancestors_map
        )

        # Ensure any pre-existing invariants applicable to mutated/created types (or ancestors) hold in PRE-state
        mutated_or_created_types = {ot for ot, _, _ in created_entities} | {
            ot for ot, _, _ in modified_pairs
        }
        mutated_ancestors: set[str] = set()
        for mt in mutated_or_created_types:
            mutated_ancestors.update(_ancestor_closure(mt, ancestors_map))

        pre_entities_all: list[_SymEntity] = []
        seen_pre_ids: set[int] = set()
        for ent in (*subjects.values(), *(b_ent for _, b_ent, _ in modified_pairs)):
            if id(ent) not in seen_pre_ids:
                seen_pre_ids.add(id(ent))
                pre_entities_all.append(ent)

        applicable_invs: list[InvariantRuleSpec] = []
        pre_inv_formulas: list[Any] = []
        for inv in bp.invariants:
            inv_qtypes: set[str] = set()
            _collect_expr_refs(inv.when, set(), quant_types=inv_qtypes)
            _collect_expr_refs(inv.must_hold, set(), quant_types=inv_qtypes)
            if inv.target_type in mutated_ancestors or (inv_qtypes & mutated_ancestors):
                applicable_invs.append(inv)
                rforms: list[Any] = []
                for pre_ent in pre_entities_all:
                    if inv.target_type in _ancestor_closure(pre_ent.ot.name, ancestors_map):
                        sub_ctx = {"obj": pre_ent}
                        w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                        m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
                        rforms.append(z3.Implies(w_z3, m_z3))
                need_bmc_pre = bool(inv_qtypes) or (inv.target_type in comp.bmc_tables) or (inv.target_type in post_tables)
                if need_bmc_pre:
                    bmc_targets = sorted(
                        {inv.target_type}
                        | {t for t in comp.bmc_tables if inv.target_type in _ancestor_closure(t, ancestors_map)}
                    )
                    for bt in bmc_targets:
                        if bt not in comp.ot_by_name:
                            continue
                        rows = comp.get_bmc_table(bt)
                        for row in rows:
                            sub_ctx = {"obj": row.entity}
                            w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                            m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
                            rforms.append(z3.Implies(z3.And(row.present, w_z3), m_z3))
                if rforms:
                    pre_inv_formulas.append(z3.And(*rforms))

        # Check pre-state feasibility first
        pre_solver = z3.Solver()
        pre_solver.set("timeout", int(timeout_ms))
        for bg in comp.background:
            pre_solver.add(bg)
        for pf in pre_formulas:
            pre_solver.add(pf)
        for pif in pre_inv_formulas:
            pre_solver.add(pif)

        pre_chk = pre_solver.check()
        if pre_chk == z3.unknown:
            results.append(
                InvariantPreservationProof(
                    action_name=act.name,
                    status="UNKNOWN",
                    logic_fragment="UNKNOWN",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue
        if pre_chk == z3.unsat:
            results.append(
                InvariantPreservationProof(
                    action_name=act.name,
                    status="VACUOUS_DEADLOCK",
                    logic_fragment=comp.logic_fragment(),
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        # Collect obligations (name, code, post_holds_formula)
        obligations: list[tuple[str, str, Any]] = []
        if mut_guards:
            obligations.append(
                (
                    f"{act.name}.division_safety",
                    "DIVISION_BY_ZERO",
                    z3.And(*mut_guards),
                )
            )

        # Obligation A: Built-in non_negative fields on created or modified entities
        for ot_name, new_ent, c_guard in created_entities:
            for fld in new_ent.ot.fields:
                if fld.non_negative:
                    fv = new_ent.fields[fld.name]
                    if fv.kind == "float" or z3.is_real(fv.expr):
                        comp.used_real = True
                        cond = fv.expr >= z3.RealVal("0")
                    else:
                        cond = fv.expr >= 0
                    obligations.append(
                        (
                            f"{ot_name}.{fld.name}_non_negative",
                            "NEGATIVE_VALUE",
                            z3.Implies(c_guard, cond),
                        )
                    )

        for ot_name, _before_ent, after_ent in modified_pairs:
            for fld in after_ent.ot.fields:
                if fld.non_negative:
                    fv = after_ent.fields[fld.name]
                    if fv.kind == "float" or z3.is_real(fv.expr):
                        comp.used_real = True
                        cond = fv.expr >= z3.RealVal("0")
                    else:
                        cond = fv.expr >= 0
                    obligations.append((f"{ot_name}.{fld.name}_non_negative", "NEGATIVE_VALUE", cond))

        # Obligation B: Built-in FSM transitions
        for ot_name, new_ent, c_guard in created_entities:
            ot = new_ent.ot
            if ot.initial_statuses is not None:
                sf = new_ent.fields.get(ot.status_field)
                if sf is not None:
                    allowed = [sf.expr == codec.encode(s) for s in ot.initial_statuses]
                    obligations.append(
                        (
                            f"{ot_name}.initial_{ot.status_field}",
                            "ILLEGAL_INITIAL_STATUS",
                            z3.Implies(c_guard, z3.Or(*allowed) if allowed else z3.BoolVal(False)),
                        )
                    )

        for ot_name, before_ent, after_ent in modified_pairs:
            ot = after_ent.ot
            if ot.transitions:
                sf_before = before_ent.fields.get(ot.status_field)
                sf_after = after_ent.fields.get(ot.status_field)
                if sf_before is not None and sf_after is not None:
                    edge_disj = [sf_before.expr == sf_after.expr]
                    for src_s, dst_list in ot.transitions.items():
                        src_c = codec.encode(src_s)
                        for dst_s in dst_list:
                            edge_disj.append(
                                z3.And(sf_before.expr == src_c, sf_after.expr == codec.encode(dst_s))
                            )
                    obligations.append(
                        (
                            f"{ot_name}.fsm_{ot.status_field}",
                            "ILLEGAL_STATE_TRANSITION",
                            z3.Or(*edge_disj),
                        )
                    )

        # Obligation C: Built-in voucher_delta_rules balance conservation
        for ot in bp.object_types:
            if not ot.voucher_delta_rules:
                continue
            for bal_field, (v_type, v_link, v_delta) in ot.voucher_delta_rules.items():
                if ot.name not in mutated_ancestors and v_type not in mutated_ancestors:
                    continue
                for _, before_ent, after_ent in modified_pairs:
                    if ot.name not in _ancestor_closure(before_ent.ot.name, ancestors_map):
                        continue
                    bf = before_ent.fields[bal_field]
                    af = after_ent.fields[bal_field]
                    be, ae, _ = comp._coerce_arith_pair(bf, af)
                    ledger_delta = ae - be
                    pk_expr = before_ent.fields[before_ent.ot.primary_key].expr
                    v_terms = []
                    for c_ot, c_ent, c_guard in created_entities:
                        if v_type not in _ancestor_closure(c_ot, ancestors_map):
                            continue
                        link_sv = c_ent.fields.get(v_link)
                        delta_sv = c_ent.fields.get(v_delta)
                        if link_sv is not None and delta_sv is not None:
                            v_terms.append(
                                z3.If(
                                    z3.And(c_guard, link_sv.expr == pk_expr),
                                    z3.ToReal(delta_sv.expr)
                                    if not z3.is_real(delta_sv.expr)
                                    else delta_sv.expr,
                                    z3.RealVal("0"),
                                )
                            )
                    v_sum = z3.Sum(*v_terms) if v_terms else z3.RealVal("0")
                    ld_real = z3.ToReal(ledger_delta) if not z3.is_real(ledger_delta) else ledger_delta
                    diff_abs = ld_real - v_sum
                    obligations.append(
                        (
                            f"{ot.name}.{bal_field}_voucher_conservation",
                            "UNBACKED_BALANCE_MUTATION",
                            z3.And(diff_abs <= z3.RealVal("1e-9"), diff_abs >= z3.RealVal("-1e-9")),
                        )
                    )

        # Obligation D: Declarative invariants (including inherited parent invariants) in post-state
        for inv in applicable_invs:
            post_formulas: list[Any] = []
            checked_table_types = sorted(
                t
                for t in (set(post_tables.keys()) | set(comp.bmc_tables.keys()))
                if inv.target_type in _ancestor_closure(t, ancestors_map)
            )
            for tb_type in checked_table_types:
                rows = post_tables.get(tb_type)
                if rows is None:
                    rows = comp.bmc_tables.get(tb_type)
                if rows is not None:
                    for row in rows:
                        sub_ctx = {"obj": row.entity}
                        w_post = (
                            comp.compile_bool(
                                inv.when, sub_ctx, pre_state=False, state_override=post_tables
                            )
                            if inv.when
                            else z3.BoolVal(True)
                        )
                        m_post = comp.compile_bool(
                            inv.must_hold, sub_ctx, pre_state=False, state_override=post_tables
                        )
                        post_formulas.append(z3.Implies(z3.And(row.present, w_post), m_post))
            for c_ot, c_ent, c_guard in created_entities:
                if inv.target_type in _ancestor_closure(c_ot, ancestors_map) and c_ot not in post_tables:
                    sub_ctx = {"obj": c_ent}
                    w_post = (
                        comp.compile_bool(
                            inv.when, sub_ctx, pre_state=False, state_override=post_tables
                        )
                        if inv.when
                        else z3.BoolVal(True)
                    )
                    m_post = comp.compile_bool(
                        inv.must_hold, sub_ctx, pre_state=False, state_override=post_tables
                    )
                    post_formulas.append(z3.Implies(z3.And(c_guard, w_post), m_post))
            for ot_name, _b_ent, after_ent in modified_pairs:
                if inv.target_type in _ancestor_closure(ot_name, ancestors_map) and ot_name not in post_tables:
                    sub_ctx = {"obj": after_ent}
                    w_post = (
                        comp.compile_bool(
                            inv.when, sub_ctx, pre_state=False, state_override=post_tables
                        )
                        if inv.when
                        else z3.BoolVal(True)
                    )
                    m_post = comp.compile_bool(
                        inv.must_hold, sub_ctx, pre_state=False, state_override=post_tables
                    )
                    post_formulas.append(z3.Implies(w_post, m_post))

            if post_formulas:
                obligations.append((inv.name, inv.code, z3.And(*post_formulas)))

        frag = comp.logic_fragment()
        checked_names: list[str] = []
        violated_names: list[str] = []
        counterexamples: list[InvariantCounterexample] = []
        any_unknown = False

        # Re-add any newly materialized background constraints after obligation compilation
        check_solver = z3.Solver()
        check_solver.set("timeout", int(timeout_ms))
        for bg in comp.background:
            check_solver.add(bg)
        for pf in pre_formulas:
            check_solver.add(pf)
        for pif in pre_inv_formulas:
            check_solver.add(pif)

        for ob_name, ob_code, post_holds in obligations:
            checked_names.append(ob_name)
            check_solver.push()
            check_solver.add(z3.Not(post_holds))
            c_res = check_solver.check()
            if c_res == z3.sat:
                mdl = check_solver.model()
                check_solver.pop()
                violated_names.append(ob_name)
                before_map: dict[str, dict[str, Any]] = {
                    f"{ot_n}:{idp}": _decode_entity(z3, mdl, ent, codec)
                    for (ot_n, idp), ent in subjects.items()
                }
                after_map: dict[str, dict[str, Any]] = {}
                for ot_n, _b_ent, a_ent in modified_pairs:
                    after_map[f"{ot_n}(modified)"] = _decode_entity(z3, mdl, a_ent, codec)
                for idx, (ot_n, c_ent, c_guard) in enumerate(created_entities):
                    if _eval_model_bool(z3, mdl, c_guard):
                        after_map[f"{ot_n}(created_{idx})"] = _decode_entity(z3, mdl, c_ent, codec)
                counterexamples.append(
                    InvariantCounterexample(
                        violated_invariant=ob_name,
                        violated_code=ob_code,
                        actor={
                            k: _decode_z3_scalar(z3, mdl, v, codec) for k, v in actor.items()
                        },
                        params={
                            k: _decode_z3_scalar(z3, mdl, v, codec) for k, v in params.items()
                        },
                        before=before_map,
                        after=after_map,
                    )
                )
            elif c_res == z3.unknown:
                check_solver.pop()
                any_unknown = True
            else:
                check_solver.pop()

        if violated_names:
            status: Literal[
                "PROVED",
                "CONTRACT_PROVED",
                "VIOLATED",
                "VACUOUS_CONTRACT",
                "VACUOUS_DEADLOCK",
                "SKIPPED_UNCONTRACTED",
                "UNKNOWN",
            ] = "VIOLATED"
        elif any_unknown:
            status = "UNKNOWN"
            frag = "UNKNOWN"
        else:
            status = "PROVED"

        elapsed = (time.perf_counter() - t0) * 1000.0
        results.append(
            InvariantPreservationProof(
                action_name=act.name,
                status=status,
                proof_kind="exact_wp",
                checked_invariants=checked_names,
                violated_invariants=violated_names,
                counterexamples=counterexamples,
                logic_fragment=frag,
                elapsed_ms=round(elapsed, 3),
            )
        )

    return results


# ---------------------------------------------------------------------------
# 4. Upgrade Compatibility Checking (base_v1 -> base_v2 under overlay)
# ---------------------------------------------------------------------------
def prove_upgrade_compatibility(
    base_v1: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    base_v2: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    *,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> UpgradeCompatibilityReport:
    """Compare ``base_v1 + overlay`` against ``base_v2 + overlay`` for newly introduced regressions."""
    _require_z3()
    t0 = time.perf_counter()
    bp1_base = _to_blueprint(base_v1, default_model_id="base_v1")
    bp2_base = _to_blueprint(base_v2, default_model_id="base_v2")
    overlays = _normalize_overlays(overlay)

    structural_errors: list[str] = []
    regressions: list[UpgradeRegressionItem] = []

    try:
        eff_v1 = _resolve_effective_blueprint(bp1_base, overlay=overlays)
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        structural_errors.append(f"v1+{code}: {exc}")
        eff_v1 = bp1_base

    try:
        eff_v2 = _resolve_effective_blueprint(bp2_base, overlay=overlays)
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        msg = f"v2+{code}: {exc}"
        structural_errors.append(msg)
        regressions.append(
            UpgradeRegressionItem(
                action_name="*",
                regression_kind="STRUCTURAL_INCOMPATIBILITY",
                v1_status="OK",
                v2_status=str(code),
                detail=msg,
            )
        )
        return UpgradeCompatibilityReport(
            base_v1_id=f"{bp1_base.model_id}@{bp1_base.version}",
            base_v2_id=f"{bp2_base.model_id}@{bp2_base.version}",
            compatible=False,
            structural_errors=structural_errors,
            regressions=regressions,
            elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )

    r1 = prove_action_reachability(eff_v1, bmc_bound=bmc_bound, timeout_ms=timeout_ms)
    r2 = prove_action_reachability(eff_v2, bmc_bound=bmc_bound, timeout_ms=timeout_ms)
    i1 = prove_invariant_preservation(eff_v1, bmc_bound=bmc_bound, timeout_ms=timeout_ms)
    i2 = prove_invariant_preservation(eff_v2, bmc_bound=bmc_bound, timeout_ms=timeout_ms)

    r1_by_name = {item.action_name: item for item in r1}
    r2_by_name = {item.action_name: item for item in r2}
    i1_by_name = {item.action_name: item for item in i1}
    i2_by_name = {item.action_name: item for item in i2}

    for act_name, res2 in r2_by_name.items():
        res1 = r1_by_name.get(act_name)
        prev_status = res1.status if res1 is not None else "ABSENT"
        if res2.status == "TOTAL_DEADLOCK" and prev_status != "TOTAL_DEADLOCK":
            regressions.append(
                UpgradeRegressionItem(
                    action_name=act_name,
                    regression_kind="NEW_TOTAL_DEADLOCK",
                    v1_status=prev_status,
                    v2_status=res2.status,
                    unsat_core=list(res2.unsat_core),
                    detail=f"{act_name} transitioned from {prev_status} in v1 to TOTAL_DEADLOCK in v2",
                )
            )
        elif res2.status == "CONDITIONAL_DEADLOCK":
            prev_dead = {s.slice_name for s in (res1.deadlocked_slices if res1 else ())}
            for ds in res2.deadlocked_slices:
                if ds.slice_name not in prev_dead:
                    regressions.append(
                        UpgradeRegressionItem(
                            action_name=act_name,
                            regression_kind="NEW_CONDITIONAL_DEADLOCK",
                            slice_name=ds.slice_name,
                            v1_status=prev_status,
                            v2_status=res2.status,
                            unsat_core=list(ds.unsat_core),
                            detail=(
                                f"slice {ds.slice_name!r} newly deadlocks in v2 "
                                f"(core={ds.unsat_core})"
                            ),
                        )
                    )

    for act_name, inv2 in i2_by_name.items():
        inv1 = i1_by_name.get(act_name)
        prev_ist = inv1.status if inv1 is not None else "ABSENT"
        if inv2.status == "VIOLATED" and prev_ist != "VIOLATED":
            regressions.append(
                UpgradeRegressionItem(
                    action_name=act_name,
                    regression_kind="NEW_INVARIANT_VIOLATION",
                    v1_status=prev_ist,
                    v2_status=inv2.status,
                    unsat_core=list(inv2.violated_invariants),
                    detail=f"new invariant violation(s): {inv2.violated_invariants}",
                )
            )
        elif inv2.status == "VACUOUS_CONTRACT" and prev_ist != "VACUOUS_CONTRACT":
            regressions.append(
                UpgradeRegressionItem(
                    action_name=act_name,
                    regression_kind="NEW_VACUOUS_CONTRACT",
                    v1_status=prev_ist,
                    v2_status=inv2.status,
                    unsat_core=list(inv2.unsat_core),
                    detail=f"contract became unrealizable (PO-5 UNSAT): {inv2.unsat_core}",
                )
            )

    v1_summary = {
        "total_deadlocks": sum(1 for x in r1 if x.status == "TOTAL_DEADLOCK"),
        "conditional_deadlocks": sum(len(x.deadlocked_slices) for x in r1),
        "invariant_violations": sum(1 for x in i1 if x.status == "VIOLATED"),
    }
    v2_summary = {
        "total_deadlocks": sum(1 for x in r2 if x.status == "TOTAL_DEADLOCK"),
        "conditional_deadlocks": sum(len(x.deadlocked_slices) for x in r2),
        "invariant_violations": sum(1 for x in i2 if x.status == "VIOLATED"),
    }

    elapsed = (time.perf_counter() - t0) * 1000.0
    return UpgradeCompatibilityReport(
        base_v1_id=f"{bp1_base.model_id}@{bp1_base.version}",
        base_v2_id=f"{bp2_base.model_id}@{bp2_base.version}",
        compatible=len(structural_errors) == 0 and len(regressions) == 0,
        structural_errors=structural_errors,
        regressions=regressions,
        v1_summary=v1_summary,
        v2_summary=v2_summary,
        elapsed_ms=round(elapsed, 3),
    )


# ---------------------------------------------------------------------------
# 5. CWA-DL & LSP Class Satisfiability Proofs + Unified Aggregator Entry Point
# ---------------------------------------------------------------------------
def _minimize_class_unsat_core(
    z3_mod: Any,
    background: Sequence[Any],
    rule_items: Sequence[tuple[str, Any]],
    candidate_names: list[str],
    timeout_ms: int,
) -> list[str]:
    """Deterministically minimize class unsat_core by dropping redundant rules."""
    if len(candidate_names) <= 1:
        return candidate_names
    formula_by_name = {name: f for name, f in rule_items}
    active = list(candidate_names)
    for cand in list(candidate_names):
        if len(active) <= 1:
            break
        remaining = [r for r in active if r != cand]
        chk_solver = z3_mod.Solver()
        chk_solver.set("timeout", int(timeout_ms))
        for bg in background:
            chk_solver.add(bg)
        for rname in remaining:
            if rname in formula_by_name:
                chk_solver.add(formula_by_name[rname])
        if chk_solver.check() == z3_mod.unsat:
            active = remaining
    return active


def prove_class_satisfiability(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> list[ClassSatisfiabilityProof]:
    """Prove every ObjectType class is mathematically satisfiable under CWA-DL & LSP."""
    z3 = _require_z3()
    raw_bp = _resolve_effective_blueprint(
        blueprint_or_spec, overlay=overlay, allow_structural_errors=True
    )
    expanded_bp, ancestors_map, structural_issues = _prepare_blueprint_for_prover(raw_bp)
    codec = _StringCodec(expanded_bp)
    expanded_by_name = {ot.name: ot for ot in expanded_bp.object_types}

    proofs: list[ClassSatisfiabilityProof] = []
    for ot in expanded_bp.object_types:
        t0 = time.perf_counter()
        anc_chain = ancestors_map.get(ot.name, ())
        inherited_invs = [
            inv
            for anc in reversed(anc_chain)
            for inv in expanded_bp.invariants
            if inv.target_type == anc
        ]
        own_invs = [inv for inv in expanded_bp.invariants if inv.target_type == ot.name]
        inh_names = [i.name for i in inherited_invs]
        own_names = [i.name for i in own_invs]

        if ot.name in structural_issues:
            st, core, det = structural_issues[ot.name]
            proofs.append(
                ClassSatisfiabilityProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status=st,
                    logic_fragment=LogicFragment.EXACT_QF_LIA
                    if hasattr(LogicFragment, "EXACT_QF_LIA")
                    else "EXACT_QF_LIA",
                    inherited_invariants=inh_names,
                    own_invariants=own_names,
                    unsat_core=list(core),
                    detail=det,
                    witness=None,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        comp = _Z3Compiler(z3, expanded_bp, codec, bmc_bound=bmc_bound)
        ent = comp.build_entity(ot.name, f"cls_{ot.name}", pre_state=True, register_congruence=True)

        compiled_rules: list[tuple[str, Any, str]] = []
        seen_rnames: set[str] = set()
        for idx, inv in enumerate([*inherited_invs, *own_invs]):
            if inv.name in seen_rnames:
                continue
            seen_rnames.add(inv.name)
            sub_ctx = {"obj": ent}
            w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when is not None else z3.BoolVal(True)
            m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
            inv_formula = z3.Implies(w_z3, m_z3)
            trk_name = f"trk__cls_{idx}_{inv.name}"
            compiled_rules.append((inv.name, inv_formula, trk_name))

        solver = z3.Solver()
        solver.set("timeout", int(timeout_ms))
        solver.set("unsat_core", True)
        solver.set("core.minimize", True)
        for bg in comp.background:
            solver.add(bg)

        trk_map: dict[str, str] = {}
        for rname, rformula, trk_name in compiled_rules:
            trk_map[trk_name] = rname
            solver.assert_and_track(rformula, z3.Bool(trk_name))

        chk = solver.check()
        frag = comp.logic_fragment()
        if chk == z3.sat:
            mdl = solver.model()
            witness = _decode_entity(z3, mdl, ent, codec)
            proofs.append(
                ClassSatisfiabilityProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status="SATISFIABLE",
                    logic_fragment=frag,
                    inherited_invariants=inh_names,
                    own_invariants=own_names,
                    unsat_core=[],
                    detail="",
                    witness=witness,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
        elif chk == z3.unsat:
            raw_core = _extract_core_rule_names(solver.unsat_core(), trk_map)
            rule_pairs = [(rname, rformula) for rname, rformula, _ in compiled_rules]
            min_core = _minimize_class_unsat_core(
                z3, comp.background, rule_pairs, raw_core, timeout_ms
            )
            if not min_core:
                min_core = [f"domain:{ot.name}"]
            proofs.append(
                ClassSatisfiabilityProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status="UNSATISFIABLE_CLASS",
                    logic_fragment=frag,
                    inherited_invariants=inh_names,
                    own_invariants=own_names,
                    unsat_core=min_core,
                    detail=f"Class {ot.name!r} has unsatisfiable constraints (core={min_core})",
                    witness=None,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
        else:
            proofs.append(
                ClassSatisfiabilityProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status="UNKNOWN",
                    logic_fragment="UNKNOWN",
                    inherited_invariants=inh_names,
                    own_invariants=own_names,
                    unsat_core=[],
                    detail="Z3 returned unknown while checking class satisfiability",
                    witness=None,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )

    # Pass 2: CWA fixed-point propagation for required / min>=1 relations targeting dead classes
    proof_by_name = {cp.object_type: cp for cp in proofs}
    changed = True
    while changed:
        changed = False
        for idx, cp in enumerate(proofs):
            if cp.status != "SATISFIABLE":
                continue
            ot = expanded_by_name[cp.object_type]
            for rel in ot.relations:
                eff_min, _ = _effective_relation_bounds(rel)
                target_cp = proof_by_name.get(rel.target_type)
                if (
                    eff_min >= 1
                    and target_cp is not None
                    and target_cp.status in ("UNSATISFIABLE_CLASS", "DL_AXIOM_CONFLICT", "LSP_VIOLATION")
                ):
                    updated_cp = cp.model_copy(
                        update={
                            "status": "DL_AXIOM_CONFLICT",
                            "witness": None,
                            "unsat_core": [
                                f"{ot.name}.{rel.field}->unsat:{rel.target_type}",
                                *target_cp.unsat_core,
                            ],
                            "detail": (
                                f"Relation {ot.name}.{rel.field} requires >= {eff_min} instance(s) "
                                f"of unsatisfiable target {rel.target_type!r} ({target_cp.status})"
                            ),
                        }
                    )
                    proofs[idx] = updated_cp
                    proof_by_name[cp.object_type] = updated_cp
                    changed = True
                    break

    return proofs


# ---------------------------------------------------------------------------
# PO-6: Guarded `when` Branch Reachability, Exhaustiveness & Disjointness
# ---------------------------------------------------------------------------
def prove_when_branches(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    action_names: Sequence[str] | None = None,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> list[WhenBranchProof]:
    """PO-6: Verify reachability, exhaustiveness, and pairwise disjointness of `when`-guarded Rule branches."""
    z3 = _require_z3()
    raw_bp = _resolve_effective_blueprint(blueprint_or_spec, overlay=overlay)
    bp, ancestors_map, _ = _prepare_blueprint_for_prover(raw_bp)
    codec = _StringCodec(bp)
    target_filter = set(action_names) if action_names is not None else None
    results: list[WhenBranchProof] = []

    for act in bp.actions:
        if target_filter is not None and act.name not in target_filter:
            continue
        if act.edit_function is not None or not act.mutations:
            continue
        guarded_indices = [idx for idx, m in enumerate(act.mutations) if m.when is not None]
        if not guarded_indices:
            continue

        t0 = time.perf_counter()
        comp = _Z3Compiler(z3, bp, codec, bmc_bound=bmc_bound)
        actor, params, subjects, anchor = _build_action_pre_context(comp, act)

        # Ensure every mutation target has a symbolic entity in `subjects`
        for mut in act.mutations:
            if mut.op in ("modify", "delete") and mut.object_type in comp.ot_by_name:
                if mut.target_ref is None:
                    key = (mut.object_type, mut.id_param)
                    if key not in subjects:
                        ent = comp.build_entity(mut.object_type, f"subj_{mut.object_type}_{mut.id_param}")
                        if mut.id_param in params:
                            comp.background.append(ent.fields[ent.ot.primary_key].expr == params[mut.id_param].expr)
                        subjects[key] = ent
                        if anchor is None:
                            anchor = ent

        # Compile preconditions
        pre_formulas: list[Any] = []
        for pre in act.preconditions:
            idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
            subj = subjects[(pre.target_type, idp)]
            p_ctx = {"obj": subj, "param": params, "actor": actor}
            w_z3 = comp.compile_bool(pre.when, p_ctx) if pre.when is not None else z3.BoolVal(True)
            m_z3 = comp.compile_bool(pre.must_hold, p_ctx)
            pre_formulas.append(z3.Implies(w_z3, m_z3))

        # Compile each guarded mutation's `when` predicate against its target context
        compiled_guards: dict[int, Any] = {}
        for idx in guarded_indices:
            mut = act.mutations[idx]
            eval_ctx: dict[str, Any] = {"param": params, "actor": actor}
            if anchor is not None:
                eval_ctx["source_obj"] = anchor
            if mut.op in ("modify", "delete") and mut.object_type in comp.ot_by_name:
                if mut.target_ref is not None:
                    parts = mut.target_ref.split(".")
                    if parts[0] in ("obj", "source_obj") and anchor is not None and len(parts) >= 2:
                        cur_t = anchor
                        for seg in parts[1:]:
                            cur_t, _ = comp.resolve_linked_entity(cur_t, seg, pre_state=True)
                        eval_ctx["obj"] = cur_t
                    elif parts[0] == "param" and len(parts) >= 2:
                        key_p = (mut.object_type, parts[1])
                        if key_p not in subjects:
                            p_ent = comp.build_entity(mut.object_type, f"subj_{mut.object_type}_{parts[1]}")
                            if parts[1] in params:
                                comp.background.append(
                                    p_ent.fields[p_ent.ot.primary_key].expr == params[parts[1]].expr
                                )
                            subjects[key_p] = p_ent
                        eval_ctx["obj"] = subjects[key_p]
                    else:
                        eval_ctx["obj"] = subjects.get((mut.object_type, mut.id_param), anchor)
                else:
                    eval_ctx["obj"] = subjects[(mut.object_type, mut.id_param)]
            compiled_guards[idx] = comp.compile_bool(mut.when, eval_ctx)

        # Applicable pre-state invariants across all referenced types
        ref_types = _collect_action_referenced_types(act, subjects) | set(comp.named_entities.keys())
        inv_scope_ancestors: set[str] = set()
        for tname in ref_types:
            inv_scope_ancestors.update(_ancestor_closure(tname, ancestors_map))

        inv_formulas: list[Any] = []
        for inv in bp.invariants:
            if inv.target_type not in inv_scope_ancestors:
                continue
            for tname in sorted(ref_types):
                if inv.target_type not in _ancestor_closure(tname, ancestors_map):
                    continue
                for ent in comp.named_entities.get(tname, ()):
                    sub_ctx = {"obj": ent}
                    w_i = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
                    m_i = comp.compile_bool(inv.must_hold, sub_ctx)
                    inv_formulas.append(z3.Implies(w_i, m_i))

        solver = z3.Solver()
        solver.set("timeout", int(timeout_ms))
        for bg in comp.background:
            solver.add(bg)
        for inv_f in inv_formulas:
            solver.add(inv_f)
        for pre_f in pre_formulas:
            solver.add(pre_f)

        frag = comp.logic_fragment()
        saw_unknown = False

        # 1) Dead branch check: every guarded mutation must be satisfiable under Domain ∧ Pre
        dead_indices: list[int] = []
        for idx in guarded_indices:
            solver.push()
            solver.add(compiled_guards[idx])
            chk = solver.check()
            solver.pop()
            if chk == z3.unsat:
                dead_indices.append(idx)
            elif chk == z3.unknown:
                saw_unknown = True

        # Group mutations by target key (op, object_type, id_param or target_ref)
        target_guarded_groups: dict[tuple[str, str, str], list[int]] = {}
        target_has_uncond: set[tuple[str, str, str]] = set()
        for idx, mut in enumerate(act.mutations):
            tkey = (mut.op, mut.object_type, str(mut.target_ref or mut.id_param or ""))
            if mut.when is None:
                target_has_uncond.add(tkey)
            else:
                target_guarded_groups.setdefault(tkey, []).append(idx)

        # 2) Pairwise overlap check on shared modified fields within the same target group
        overlapping_pairs: list[tuple[int, int]] = []
        overlap_witness: dict[str, Any] | None = None
        for tkey, grp in target_guarded_groups.items():
            if tkey[0] != "modify" or len(grp) < 2:
                continue
            for pos_a in range(len(grp)):
                for pos_b in range(pos_a + 1, len(grp)):
                    ia, ib = grp[pos_a], grp[pos_b]
                    shared_fields = set(act.mutations[ia].set_fields.keys()) & set(
                        act.mutations[ib].set_fields.keys()
                    )
                    if not shared_fields:
                        continue
                    solver.push()
                    solver.add(z3.And(compiled_guards[ia], compiled_guards[ib]))
                    chk = solver.check()
                    if chk == z3.sat:
                        overlapping_pairs.append((ia, ib))
                        if overlap_witness is None:
                            overlap_witness = _decode_witness(
                                z3, solver.model(), comp, actor, params, subjects
                            )
                    elif chk == z3.unknown:
                        saw_unknown = True
                    solver.pop()

        # 3) Exhaustiveness check:
        #    (a) action has NO unconditional mutation anywhere (silent NO-OP risk), OR
        #    (b) same-target group has >= 2 guarded branches with no unconditional fallback on that target
        exhaustiveness_groups: list[tuple[int, ...]] = []
        seen_groups: set[tuple[int, ...]] = set()
        has_any_uncond = any(m.when is None for m in act.mutations)
        if not has_any_uncond:
            g_all = tuple(guarded_indices)
            exhaustiveness_groups.append(g_all)
            seen_groups.add(g_all)
        for tkey, grp in target_guarded_groups.items():
            if len(grp) >= 2 and tkey not in target_has_uncond:
                g_tup = tuple(grp)
                if g_tup not in seen_groups:
                    exhaustiveness_groups.append(g_tup)
                    seen_groups.add(g_tup)

        uncovered_witness: dict[str, Any] | None = None
        checked_exhaustiveness = bool(exhaustiveness_groups)
        for grp in exhaustiveness_groups:
            solver.push()
            solver.add(z3.Not(z3.Or(*[compiled_guards[i] for i in grp])))
            chk = solver.check()
            if chk == z3.sat:
                if uncovered_witness is None:
                    uncovered_witness = _decode_witness(
                        z3, solver.model(), comp, actor, params, subjects
                    )
            elif chk == z3.unknown:
                saw_unknown = True
            solver.pop()

        elapsed = round((time.perf_counter() - t0) * 1000.0, 3)
        if dead_indices:
            status: Literal[
                "EXHAUSTIVE",
                "CONDITIONAL_SIDE_EFFECT",
                "INCOMPLETE_WHEN_BRANCHES",
                "DEAD_BRANCH",
                "OVERLAPPING_WHEN_BRANCHES",
                "UNKNOWN",
            ] = "DEAD_BRANCH"
            detail = f"Action {act.name!r} has unreachable guarded branch(es) at index {dead_indices}"
        elif uncovered_witness is not None:
            status = "INCOMPLETE_WHEN_BRANCHES"
            detail = (
                f"Action {act.name!r} has uncovered input space where none of the guarded `when` branches fire"
            )
        elif overlapping_pairs:
            status = "OVERLAPPING_WHEN_BRANCHES"
            detail = (
                f"Action {act.name!r} has overlapping guarded branches writing shared fields: {overlapping_pairs}"
            )
        elif saw_unknown:
            status = "UNKNOWN"
            detail = f"Z3 returned unknown while verifying guarded branches for {act.name!r}"
        elif checked_exhaustiveness:
            status = "EXHAUSTIVE"
            detail = (
                f"All {len(guarded_indices)} guarded `when` branches are reachable, exhaustive, and mutually disjoint"
            )
        else:
            status = "CONDITIONAL_SIDE_EFFECT"
            detail = (
                f"Single conditional side-effect branch alongside unconditional mutation is reachable"
            )

        results.append(
            WhenBranchProof(
                action_name=act.name,
                status=status,
                branch_count=len(guarded_indices),
                dead_branch_indices=dead_indices,
                overlapping_pairs=overlapping_pairs,
                uncovered_witness=uncovered_witness,
                overlap_witness=overlap_witness,
                detail=detail,
                logic_fragment=frag,
                elapsed_ms=elapsed,
            )
        )

    return results


# ---------------------------------------------------------------------------
# PO-7: Workflow FSM Structural Liveness & Z3 Multi-Step Terminal Reachability
# ---------------------------------------------------------------------------
def _extract_literal_strings_from_val(expr: Any) -> set[str] | None:
    """Return all concrete string literals an expression may evaluate to, or None if non-literal."""
    if not isinstance(expr, Mapping):
        return None
    if "lit" in expr:
        val = expr["lit"]
        return {str(val)} if isinstance(val, str) else None
    if expr.get("op") == "if_else":
        t_set = _extract_literal_strings_from_val(expr.get("then"))
        e_set = _extract_literal_strings_from_val(expr.get("else"))
        if t_set is not None and e_set is not None:
            return t_set | e_set
    return None


def _extract_status_constraints_from_pred(
    expr: Any, ref_prefix: str, status_field: str
) -> set[str] | None:
    """Best-effort sound over-approximation of allowed status strings from a conjunction predicate."""
    if not isinstance(expr, Mapping):
        return None
    op = expr.get("op")
    target_ref = f"{ref_prefix}.{status_field}"
    if op == "and":
        allowed: set[str] | None = None
        for arg in expr.get("args") or ():
            sub = _extract_status_constraints_from_pred(arg, ref_prefix, status_field)
            if sub is not None:
                allowed = sub if allowed is None else (allowed & sub)
        return allowed
    if op == "eq":
        left, right = expr.get("left"), expr.get("right")
        if isinstance(left, Mapping) and left.get("ref") == target_ref and isinstance(right, Mapping) and "lit" in right:
            return {str(right["lit"])} if isinstance(right["lit"], str) else None
        if isinstance(right, Mapping) and right.get("ref") == target_ref and isinstance(left, Mapping) and "lit" in left:
            return {str(left["lit"])} if isinstance(left["lit"], str) else None
    if op == "in":
        left, right = expr.get("left"), expr.get("right")
        if (
            isinstance(left, Mapping)
            and left.get("ref") == target_ref
            and isinstance(right, Mapping)
            and isinstance(right.get("lit"), list)
        ):
            return {str(x) for x in right["lit"] if isinstance(x, str)}
    return None


def _verify_workflow_action_chain(
    z3: Any,
    bp: DeclarativeBlueprint,
    ot: ObjectTypeBlueprint,
    anc_set: set[str],
    ancestors_map: Mapping[str, Sequence[str]],
    codec: _StringCodec,
    chain_acts: Sequence[ActionBlueprint],
    terminal_states: Sequence[str],
    *,
    bmc_bound: int,
    timeout_ms: int,
) -> tuple[Literal["SAT", "UNSAT", "UNKNOWN"], str | None, str]:
    """Symbolically compose `chain_acts` on a single tracked entity from initial_statuses to terminal_states."""
    sf = ot.status_field or "status"
    comp = _Z3Compiler(z3, bp, codec, bmc_bound=bmc_bound)
    solver = z3.Solver()
    solver.set("timeout", int(timeout_ms))

    # Track entity across steps without cross-step UNA congruence collapse
    cur_ent = comp.build_entity(ot.name, f"wf_{ot.name}_s0", pre_state=True, register_congruence=False)

    # Initial status constraint
    if ot.initial_statuses:
        solver.add(
            z3.Or(*[cur_ent.fields[sf].expr == codec.encode(s) for s in ot.initial_statuses])
        )

    own_invs = [inv for inv in bp.invariants if inv.target_type in anc_set]

    def _assert_entity_invs(ent: _SymEntity) -> None:
        for inv in own_invs:
            sub_ctx = {"obj": ent}
            w_z3 = comp.compile_bool(inv.when, sub_ctx) if inv.when else z3.BoolVal(True)
            m_z3 = comp.compile_bool(inv.must_hold, sub_ctx)
            solver.add(z3.Implies(w_z3, m_z3))

    _assert_entity_invs(cur_ent)

    def _is_mod_for_ot(act_obj: ActionBlueprint, fname: str) -> bool:
        return any(
            f"{anc}.{fname}" in act_obj.modifies or f"{anc}.*" in act_obj.modifies
            for anc in anc_set
        )

    for step_idx, act in enumerate(chain_acts):
        actor_t = comp.build_actor(act.allowed_roles)
        params_t = comp.build_params(act)

        step_subjects: dict[tuple[str, str], _SymEntity] = {}
        anchor_t: _SymEntity | None = None

        # Determine if any mutation targets `ot` via `target_ref` from a companion anchor (e.g. Delivery.order)
        target_ref_rel: str | None = None
        for mut in act.mutations:
            if (
                mut.op == "modify"
                and mut.object_type in anc_set
                and mut.target_ref
                and mut.target_ref.startswith(("obj.", "source_obj."))
            ):
                parts = mut.target_ref.split(".")
                if len(parts) == 2:
                    target_ref_rel = parts[1]
                    break

        # Materialize subjects for preconditions
        for pre in act.preconditions:
            if pre.target_type not in comp.ot_by_name:
                continue
            idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
            key = (pre.target_type, idp)
            if key in step_subjects:
                continue
            if pre.target_type in anc_set and target_ref_rel is None:
                step_subjects[key] = cur_ent
                if idp in params_t:
                    solver.add(cur_ent.fields[ot.primary_key].expr == params_t[idp].expr)
                if anchor_t is None:
                    anchor_t = cur_ent
            else:
                aux_ent = comp.build_entity(
                    pre.target_type,
                    f"wf_s{step_idx}_{pre.target_type}_{idp}",
                    pre_state=True,
                    register_congruence=False,
                )
                if idp in params_t:
                    solver.add(aux_ent.fields[aux_ent.ot.primary_key].expr == params_t[idp].expr)
                # Bind companion anchor relation to tracked `cur_ent` if mutated via `target_ref`
                if target_ref_rel and target_ref_rel in aux_ent.fields:
                    solver.add(
                        aux_ent.fields[target_ref_rel].expr == cur_ent.fields[ot.primary_key].expr
                    )
                    aux_ent.linked[target_ref_rel] = cur_ent
                # Assert companion class invariants
                aux_ancs = _ancestor_closure(pre.target_type, ancestors_map)
                for inv in bp.invariants:
                    if inv.target_type in aux_ancs:
                        sctx = {"obj": aux_ent}
                        wi = comp.compile_bool(inv.when, sctx) if inv.when else z3.BoolVal(True)
                        mi = comp.compile_bool(inv.must_hold, sctx)
                        solver.add(z3.Implies(wi, mi))
                step_subjects[key] = aux_ent
                if anchor_t is None:
                    anchor_t = aux_ent

        # Assert action preconditions
        for pre in act.preconditions:
            if pre.target_type not in comp.ot_by_name:
                continue
            idp = _resolve_rule_id_param(pre, act, comp.ot_by_name)
            subj = step_subjects[(pre.target_type, idp)]
            p_ctx = {"obj": subj, "param": params_t, "actor": actor_t}
            w_p = comp.compile_bool(pre.when, p_ctx) if pre.when is not None else z3.BoolVal(True)
            m_p = comp.compile_bool(pre.must_hold, p_ctx)
            solver.add(z3.Implies(w_p, m_p))

        # Construct next-step tracked entity `next_ent`
        next_fields = dict(cur_ent.fields)
        next_ent = _SymEntity(
            ot=ot,
            tag=f"wf_{ot.name}_s{step_idx + 1}",
            fields=next_fields,
            linked=dict(cur_ent.linked),
        )

        if act.edit_function is None:
            # Declarative Rule: apply mutations targeting `ot` (or ancestor) via SSA ITE
            for mut in act.mutations:
                if mut.op != "modify" or mut.object_type not in anc_set:
                    continue
                if mut.target_ref is not None and anchor_t is not None:
                    eval_ctx: dict[str, Any] = {
                        "obj": next_ent,
                        "source_obj": anchor_t,
                        "param": params_t,
                        "actor": actor_t,
                    }
                else:
                    if mut.id_param in params_t:
                        solver.add(cur_ent.fields[ot.primary_key].expr == params_t[mut.id_param].expr)
                    eval_ctx = {
                        "obj": next_ent,
                        "source_obj": anchor_t or next_ent,
                        "param": params_t,
                        "actor": actor_t,
                    }
                w_m = comp.compile_bool(mut.when, eval_ctx) if mut.when is not None else z3.BoolVal(True)
                for fname, val_ast in mut.set_fields.items():
                    if fname not in next_ent.fields:
                        continue
                    branch_guards: list[Any] = []
                    new_sv = comp.compile_value(val_ast, eval_ctx, guards=branch_guards)
                    if branch_guards:
                        solver.add(z3.Implies(w_m, z3.And(*branch_guards)))
                    old_sv = next_ent.fields[fname]
                    if mut.when is not None:
                        if new_sv.kind in ("int", "float") and old_sv.kind in ("int", "float"):
                            ne_, oe_, rk = comp._coerce_arith_pair(new_sv, old_sv)
                            next_ent.fields[fname] = _SymVal(
                                z3.If(w_m, ne_, oe_),
                                rk,
                                is_null=_ite_is_null(z3, w_m, new_sv.is_null, old_sv.is_null),
                            )
                        else:
                            next_ent.fields[fname] = _SymVal(
                                z3.If(w_m, new_sv.expr, old_sv.expr),
                                new_sv.kind if new_sv.kind != "null" else old_sv.kind,
                                is_null=_ite_is_null(z3, w_m, new_sv.is_null, old_sv.is_null),
                            )
                    else:
                        if new_sv.kind == "int" and old_sv.kind == "float":
                            ne_, _oe, rk = comp._coerce_arith_pair(new_sv, old_sv)
                            next_ent.fields[fname] = _SymVal(ne_, rk, is_null=new_sv.is_null)
                        else:
                            next_ent.fields[fname] = new_sv
            # Domain axioms (non_negative / valid FSM states) on post-state
            for dom_ax in comp.domain_axioms_for_entity(next_ent):
                solver.add(dom_ax)
        else:
            # Contracted EditFunction: havoc modified fields, Frame-preserve untouched fields, assert ensures
            for fname in list(next_ent.fields.keys()):
                if fname == ot.primary_key or not _is_mod_for_ot(act, fname):
                    continue
                next_ent.fields[fname] = _make_raw_havoc_val(
                    comp, ot, fname, f"wf_s{step_idx}_{ot.name}_{fname}"
                )
            for dom_ax in comp.domain_axioms_for_entity(next_ent):
                solver.add(dom_ax)

            # Assert ensures targeting `ot` / ancestor (skipping pure-created ensures when `id_param is None` & no `before.`)
            creates_anc = bool(set(act.creates) & anc_set)
            for ens in act.ensures:
                if ens.target_type not in anc_set:
                    continue
                ens_refs: set[str] = set()
                _collect_expr_refs(ens.when, ens_refs)
                _collect_expr_refs(ens.must_hold, ens_refs)
                uses_before = any(r.startswith("before.") for r in ens_refs)
                if ens.id_param is None and creates_anc and not uses_before:
                    continue
                if ens.id_param and ens.id_param in params_t:
                    solver.add(cur_ent.fields[ot.primary_key].expr == params_t[ens.id_param].expr)
                ectx = {
                    "before": cur_ent,
                    "after": next_ent,
                    "obj": next_ent,
                    "param": params_t,
                    "actor": actor_t,
                }
                w_e = comp.compile_bool(ens.when, ectx) if ens.when is not None else z3.BoolVal(True)
                m_e = comp.compile_bool(ens.must_hold, ectx)
                solver.add(z3.Implies(w_e, m_e))

        # Enforce `ot`'s own (possibly subtype-narrowed) FSM transition legality
        if ot.transitions and sf in cur_ent.fields:
            pre_s = cur_ent.fields[sf].expr
            post_s = next_ent.fields[sf].expr
            edge_disj = [pre_s == post_s]
            for src_s, dsts in ot.transitions.items():
                src_code = codec.encode(src_s)
                for dst_s in dsts:
                    edge_disj.append(z3.And(pre_s == src_code, post_s == codec.encode(dst_s)))
            solver.add(z3.Or(*edge_disj))

        _assert_entity_invs(next_ent)
        cur_ent = next_ent

    # Terminal goal on final step `E_m`
    solver.add(z3.Or(*[cur_ent.fields[sf].expr == codec.encode(t) for t in terminal_states]))

    for bg in comp.background:
        solver.add(bg)

    chk = solver.check()
    frag = f"BMC_BOUNDED(k={len(chain_acts)})"
    if chk == z3.sat:
        reached = _decode_z3_scalar(z3, solver.model(), cur_ent.fields[sf], codec)
        return "SAT", str(reached), frag
    if chk == z3.unknown:
        return "UNKNOWN", None, "UNKNOWN"
    return "UNSAT", None, frag


def prove_workflow_liveness(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    object_types: Sequence[str] | None = None,
    max_steps: int = 4,
    bmc_bound: int = 4,
    timeout_ms: int = 5000,
) -> list[WorkflowLivenessProof]:
    """PO-7: Prove FSM structural liveness + bounded Z3 multi-step action terminal reachability."""
    z3 = _require_z3()
    raw_bp = _resolve_effective_blueprint(blueprint_or_spec, overlay=overlay)
    bp, ancestors_map, _ = _prepare_blueprint_for_prover(raw_bp)
    codec = _StringCodec(bp)
    ot_by_name = {ot.name: ot for ot in bp.object_types}
    raw_ot_by_name = {ot.name: ot for ot in raw_bp.object_types}
    target_filter = set(object_types) if object_types is not None else None
    results: list[WorkflowLivenessProof] = []

    for ot in bp.object_types:
        if target_filter is not None and ot.name not in target_filter:
            continue
        if not ot.transitions and not ot.initial_statuses:
            continue

        t0 = time.perf_counter()
        sf = ot.status_field or "status"
        inits = list(ot.initial_statuses or ())
        trans = {str(k): [str(v) for v in vs] for k, vs in (ot.transitions or {}).items()}
        declared_states = set(inits) | set(trans.keys())
        for dsts in trans.values():
            declared_states.update(dsts)

        # Missing initial states when transitions exist
        if trans and not inits:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="NO_INITIAL_STATE",
                    action_terminal_status="SKIPPED_STRUCTURAL_FAILURE",
                    initial_statuses=[],
                    reachable_states=[],
                    terminal_states=[],
                    unreachable_states=sorted(declared_states),
                    max_steps=max_steps,
                    detail=f"FSM for {ot.name!r} declares transitions without any initial_statuses",
                    logic_fragment="EXACT_GRAPH",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        # 1) Forward BFS from initial_statuses
        reachable: set[str] = set(inits)
        q = list(inits)
        while q:
            u = q.pop(0)
            for v in trans.get(u, ()):
                if v not in reachable:
                    reachable.add(v)
                    q.append(v)

        # Partition unreachable states into subtype-pruned inherited leaves vs true orphan states
        ancestor_fsm_states: set[str] = set()
        for anc_name in ancestors_map.get(ot.name, ()):
            anc_ot = raw_ot_by_name.get(anc_name) or ot_by_name.get(anc_name)
            if anc_ot is not None:
                ancestor_fsm_states.update(anc_ot.initial_statuses or ())
                ancestor_fsm_states.update((anc_ot.transitions or {}).keys())
                for dsts in (anc_ot.transitions or {}).values():
                    ancestor_fsm_states.update(dsts)

        pruned_inherited: list[str] = []
        unreachable_orphans: list[str] = []
        for s in sorted(declared_states - reachable):
            if ot.extends and s in ancestor_fsm_states and not trans.get(s):
                pruned_inherited.append(s)
            else:
                unreachable_orphans.append(s)

        terminals = sorted(s for s in reachable if not trans.get(s))

        # Reverse BFS from terminals restricted to reachable subgraph
        rev: dict[str, list[str]] = {s: [] for s in reachable}
        for u in reachable:
            for v in trans.get(u, ()):
                if v in reachable:
                    rev.setdefault(v, []).append(u)
        can_term: set[str] = set(terminals)
        rq = list(terminals)
        while rq:
            u = rq.pop(0)
            for prev in rev.get(u, ()):
                if prev not in can_term:
                    can_term.add(prev)
                    rq.append(prev)
        traps = sorted(reachable - can_term)

        if unreachable_orphans:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="UNREACHABLE_STATE",
                    action_terminal_status="SKIPPED_STRUCTURAL_FAILURE",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    unreachable_states=unreachable_orphans,
                    pruned_inherited_states=pruned_inherited,
                    trap_states=traps,
                    max_steps=max_steps,
                    detail=f"FSM for {ot.name!r} has unreachable orphan state(s): {unreachable_orphans}",
                    logic_fragment="EXACT_GRAPH",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        if len(reachable) > 1 and not terminals:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="NO_TERMINAL_STATE",
                    action_terminal_status="SKIPPED_STRUCTURAL_FAILURE",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=[],
                    pruned_inherited_states=pruned_inherited,
                    trap_states=sorted(reachable),
                    max_steps=max_steps,
                    detail=f"FSM for {ot.name!r} has no terminal state (infinite cycle across {sorted(reachable)})",
                    logic_fragment="EXACT_GRAPH",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        if traps:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="TRAP_STATE",
                    action_terminal_status="SKIPPED_STRUCTURAL_FAILURE",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    pruned_inherited_states=pruned_inherited,
                    trap_states=traps,
                    max_steps=max_steps,
                    detail=f"FSM for {ot.name!r} has inescapable trap/livelock state(s): {traps}",
                    logic_fragment="EXACT_GRAPH",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        # Trivial terminal: an initial status is already a terminal state
        init_terms = sorted(set(inits) & set(terminals))
        if init_terms:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="LIVE",
                    action_terminal_status="TRIVIAL_TERMINAL",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    pruned_inherited_states=pruned_inherited,
                    reached_terminal_status=init_terms[0],
                    max_steps=max_steps,
                    detail=f"Initial state {init_terms[0]!r} is already terminal (0 steps required)",
                    logic_fragment="EXACT_GRAPH",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        # 2) Build abstract action-edge graph polymorphically over `anc_set`
        anc_set = _ancestor_closure(ot.name, ancestors_map)
        action_edges: dict[str, list[tuple[str, ActionBlueprint]]] = {}
        covered_edges: set[tuple[str, str]] = set()
        prep_actions: list[ActionBlueprint] = []
        status_act_names: set[str] = set()

        for act in bp.actions:
            touches_sf = False
            possible_dsts: set[str] | None = set()
            possible_srcs: set[str] | None = None

            if act.edit_function is None:
                muts_on_ot = [
                    m for m in act.mutations if m.op == "modify" and m.object_type in anc_set
                ]
                if not muts_on_ot:
                    continue
                sf_muts = [m for m in muts_on_ot if sf in m.set_fields]
                if sf_muts:
                    touches_sf = True
                    for m in sf_muts:
                        lits = _extract_literal_strings_from_val(m.set_fields[sf])
                        if lits is None:
                            possible_dsts = None
                            break
                        assert possible_dsts is not None
                        possible_dsts.update(lits)
                    for pre in act.preconditions:
                        if pre.target_type in anc_set and pre.when is None:
                            cset = _extract_status_constraints_from_pred(pre.must_hold, "obj", sf)
                            if cset is not None:
                                possible_srcs = cset if possible_srcs is None else (possible_srcs & cset)
                else:
                    # Non-status field modifier on `ot` (potential prep action e.g. verify_export_letter_of_credit)
                    prep_actions.append(act)
                    continue
            else:
                if not (act.modifies or act.ensures):
                    continue
                modifies_sf = any(
                    f"{anc}.{sf}" in act.modifies or f"{anc}.*" in act.modifies for anc in anc_set
                )
                if not modifies_sf:
                    if any(
                        m.startswith(f"{anc}.") for anc in anc_set for m in act.modifies
                    ):
                        prep_actions.append(act)
                    continue
                touches_sf = True
                possible_dsts = None
                creates_anc = bool(set(act.creates) & anc_set)
                for ens in act.ensures:
                    if ens.target_type not in anc_set or ens.when is not None:
                        continue
                    ens_refs: set[str] = set()
                    _collect_expr_refs(ens.must_hold, ens_refs)
                    if ens.id_param is None and creates_anc and not any(r.startswith("before.") for r in ens_refs):
                        continue
                    d_cset = _extract_status_constraints_from_pred(ens.must_hold, "after", sf)
                    if d_cset is not None:
                        possible_dsts = d_cset if possible_dsts is None else (possible_dsts & d_cset)
                    s_cset = _extract_status_constraints_from_pred(ens.must_hold, "before", sf)
                    if s_cset is not None:
                        possible_srcs = s_cset if possible_srcs is None else (possible_srcs & s_cset)
                for pre in act.preconditions:
                    if pre.target_type in anc_set and pre.when is None:
                        s_cset = _extract_status_constraints_from_pred(pre.must_hold, "obj", sf)
                        if s_cset is not None:
                            possible_srcs = s_cset if possible_srcs is None else (possible_srcs & s_cset)

            if not touches_sf:
                continue
            status_act_names.add(act.name)
            for src_s in reachable:
                if possible_srcs is not None and src_s not in possible_srcs:
                    continue
                for dst_s in trans.get(src_s, ()):
                    if dst_s == src_s:
                        continue
                    if possible_dsts is not None and dst_s not in possible_dsts:
                        continue
                    action_edges.setdefault(src_s, []).append((dst_s, act))
                    covered_edges.add((src_s, dst_s))

        missing_edges = sorted(
            f"{u}->{v}"
            for u in reachable
            for v in trans.get(u, ())
            if (u, v) not in covered_edges
        )

        # Abstract BFS for candidate action chains from initial_statuses to terminal_states (length <= max_steps)
        candidate_chains: list[list[ActionBlueprint]] = []
        seen_chain_keys: set[tuple[str, ...]] = set()
        bfs_q: list[tuple[str, list[ActionBlueprint], frozenset[str]]] = [
            (s, [], frozenset({s})) for s in sorted(inits)
        ]
        while bfs_q and len(candidate_chains) < 24:
            curr_s, chain, visited = bfs_q.pop(0)
            if len(chain) >= max_steps:
                continue
            for nxt_s, act in action_edges.get(curr_s, ()):
                nxt_chain = [*chain, act]
                ckey = tuple(a.name for a in nxt_chain)
                if nxt_s in terminals:
                    if ckey not in seen_chain_keys:
                        seen_chain_keys.add(ckey)
                        candidate_chains.append(nxt_chain)
                elif nxt_s not in visited and len(nxt_chain) < max_steps:
                    bfs_q.append((nxt_s, nxt_chain, visited | {nxt_s}))

        if not candidate_chains:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="GRAPH_LIVE_PARTIAL_ACTIONS",
                    action_terminal_status="PARTIAL_ACTION_COVERAGE",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    pruned_inherited_states=pruned_inherited,
                    missing_action_edges=missing_edges,
                    max_steps=max_steps,
                    detail=(
                        f"FSM graph for {ot.name!r} is structurally live, but no complete governed action "
                        f"chain covers initial -> terminal within {max_steps} steps"
                    ),
                    logic_fragment="EXACT_GRAPH",
                    elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )
            )
            continue

        # 3) Run Z3 multi-step symbolic composition on candidate chains (direct chains first, then +1 prep action if needed)
        witness_trace: list[str] | None = None
        reached_term: str | None = None
        final_frag = "BMC_BOUNDED(k=1)"
        saw_unknown = False

        for chain in candidate_chains:
            verdict, term_s, frag = _verify_workflow_action_chain(
                z3, bp, ot, anc_set, ancestors_map, codec, chain, terminals,
                bmc_bound=bmc_bound, timeout_ms=timeout_ms,
            )
            final_frag = frag
            if verdict == "SAT":
                witness_trace = [a.name for a in chain]
                reached_term = term_s
                break
            if verdict == "UNKNOWN":
                saw_unknown = True

        # Retry with up to 1 non-status prep action inserted if direct chains were blocked by a field flag
        if witness_trace is None and prep_actions:
            prep_candidates: list[list[ActionBlueprint]] = []
            for base_chain in candidate_chains[:6]:
                if len(base_chain) >= max_steps:
                    continue
                for prep_act in prep_actions[:4]:
                    for ins_pos in range(len(base_chain)):
                        augmented = [*base_chain[:ins_pos], prep_act, *base_chain[ins_pos:]]
                        akey = tuple(a.name for a in augmented)
                        if akey not in seen_chain_keys:
                            seen_chain_keys.add(akey)
                            prep_candidates.append(augmented)
            for chain in prep_candidates[:16]:
                verdict, term_s, frag = _verify_workflow_action_chain(
                    z3, bp, ot, anc_set, ancestors_map, codec, chain, terminals,
                    bmc_bound=bmc_bound, timeout_ms=timeout_ms,
                )
                final_frag = frag
                if verdict == "SAT":
                    witness_trace = [a.name for a in chain]
                    reached_term = term_s
                    break
                if verdict == "UNKNOWN":
                    saw_unknown = True

        elapsed = round((time.perf_counter() - t0) * 1000.0, 3)
        if witness_trace is not None:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="LIVE",
                    action_terminal_status="ACTION_TERMINAL_REACHABLE",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    pruned_inherited_states=pruned_inherited,
                    missing_action_edges=missing_edges,
                    terminal_witness_trace=witness_trace,
                    reached_terminal_status=reached_term,
                    max_steps=max_steps,
                    detail=(
                        f"Z3 verified {len(witness_trace)}-step governed chain to terminal "
                        f"{reached_term!r}: {' -> '.join(witness_trace)}"
                    ),
                    logic_fragment=final_frag,
                    elapsed_ms=elapsed,
                )
            )
        elif saw_unknown:
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="UNKNOWN",
                    action_terminal_status="UNKNOWN",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    pruned_inherited_states=pruned_inherited,
                    missing_action_edges=missing_edges,
                    max_steps=max_steps,
                    detail=f"Z3 returned unknown while verifying multi-step workflow liveness for {ot.name!r}",
                    logic_fragment="UNKNOWN",
                    elapsed_ms=elapsed,
                )
            )
        else:
            tried_preview = [" -> ".join(a.name for a in c) for c in candidate_chains[:3]]
            results.append(
                WorkflowLivenessProof(
                    object_type=ot.name,
                    extends=ot.extends,
                    status_field=sf,
                    status="ACTION_CHAIN_DEADLOCKED",
                    action_terminal_status="ACTION_CHAIN_DEADLOCKED",
                    initial_statuses=sorted(inits),
                    reachable_states=sorted(reachable),
                    terminal_states=terminals,
                    pruned_inherited_states=pruned_inherited,
                    missing_action_edges=missing_edges,
                    max_steps=max_steps,
                    detail=(
                        f"All {len(candidate_chains)} abstract action chain(s) for {ot.name!r} "
                        f"deadlocked in Z3 multi-step composition (e.g. {tried_preview})"
                    ),
                    logic_fragment=final_frag,
                    elapsed_ms=elapsed,
                )
            )

    return results


def prove_blueprint(
    blueprint_or_spec: DeclarativeBlueprint | OntologySpec | Mapping[str, Any],
    *,
    overlay: CustomerOverlaySpec
    | EvolutionPatch
    | Mapping[str, Any]
    | Sequence[CustomerOverlaySpec | EvolutionPatch | Mapping[str, Any]]
    | None = None,
    upgrade_to: DeclarativeBlueprint | OntologySpec | Mapping[str, Any] | None = None,
    slices: Mapping[str, Any] | None = None,
    bmc_bound: int = 4,
    check_redundancy: bool = True,
    timeout_ms: int = 5000,
) -> ProofReport:
    """Run class satisfiability/DL/LSP, reachability, redundancy, Hoare preservation, and upgrade proofs."""
    _require_z3()
    t0 = time.perf_counter()

    base_bp = _to_blueprint(blueprint_or_spec)
    eff_bp = _resolve_effective_blueprint(
        base_bp, overlay=overlay, allow_structural_errors=True
    )

    class_proofs = prove_class_satisfiability(
        eff_bp,
        bmc_bound=bmc_bound,
        timeout_ms=timeout_ms,
    )
    broken_cycle_types = {
        c.object_type
        for c in class_proofs
        if any(tag.startswith(("cycle:", "unknown_parent:", "ancestor_invalid:")) for tag in c.unsat_core)
    }

    safe_action_names: list[str] | None = None
    if broken_cycle_types:
        safe_action_names = [
            a.name
            for a in eff_bp.actions
            if not any(m.object_type in broken_cycle_types for m in a.mutations)
            and not any(p.target_type in broken_cycle_types for p in a.preconditions)
        ]

    reachability = prove_action_reachability(
        eff_bp,
        action_names=safe_action_names,
        slices=slices,
        bmc_bound=bmc_bound,
        timeout_ms=timeout_ms,
    )
    redundant = (
        prove_redundant_rules(
            eff_bp, action_names=safe_action_names, bmc_bound=bmc_bound, timeout_ms=timeout_ms
        )
        if check_redundancy
        else []
    )
    inv_pres = prove_invariant_preservation(
        eff_bp,
        action_names=safe_action_names,
        bmc_bound=bmc_bound,
        timeout_ms=timeout_ms,
    )
    when_proofs = prove_when_branches(
        eff_bp,
        action_names=safe_action_names,
        bmc_bound=bmc_bound,
        timeout_ms=timeout_ms,
    )
    safe_ot_names: list[str] | None = (
        [ot.name for ot in eff_bp.object_types if ot.name not in broken_cycle_types]
        if broken_cycle_types
        else None
    )
    workflow_proofs = prove_workflow_liveness(
        eff_bp,
        object_types=safe_ot_names,
        max_steps=4,
        bmc_bound=bmc_bound,
        timeout_ms=timeout_ms,
    )
    upgrade_report = (
        prove_upgrade_compatibility(
            base_bp,
            upgrade_to,
            overlay=overlay,
            bmc_bound=bmc_bound,
            timeout_ms=timeout_ms,
        )
        if upgrade_to is not None
        else None
    )

    total_deadlocks = sum(1 for r in reachability if r.status == "TOTAL_DEADLOCK")
    cond_deadlocks = sum(len(r.deadlocked_slices) for r in reachability)
    inv_violations = sum(1 for p in inv_pres if p.status == "VIOLATED")
    exact_wp_proved = sum(
        1 for p in inv_pres if p.status == "PROVED" and p.proof_kind == "exact_wp"
    )
    contract_proved = sum(1 for p in inv_pres if p.status == "CONTRACT_PROVED")
    vacuous_contracts = sum(1 for p in inv_pres if p.status == "VACUOUS_CONTRACT")
    uncontracted_efs = sum(1 for p in inv_pres if p.status == "SKIPPED_UNCONTRACTED")
    dead_classes = sum(1 for c in class_proofs if c.status == "UNSATISFIABLE_CLASS")
    dl_conflicts = sum(1 for c in class_proofs if c.status == "DL_AXIOM_CONFLICT")
    lsp_violations = sum(1 for c in class_proofs if c.status == "LSP_VIOLATION")
    when_issues = sum(
        1 for w in when_proofs if w.status not in ("EXHAUSTIVE", "CONDITIONAL_SIDE_EFFECT")
    )
    wf_live = sum(
        1 for wf in workflow_proofs if wf.status in ("LIVE", "GRAPH_LIVE_PARTIAL_ACTIONS")
    )
    wf_action_verified = sum(
        1 for wf in workflow_proofs if wf.action_terminal_status == "ACTION_TERMINAL_REACHABLE"
    )
    wf_issues = sum(
        1 for wf in workflow_proofs if wf.status not in ("LIVE", "GRAPH_LIVE_PARTIAL_ACTIONS")
    )

    frag_counts: dict[str, int] = {}
    for item in [*class_proofs, *reachability, *redundant, *inv_pres, *when_proofs, *workflow_proofs]:
        frag_counts[item.logic_fragment] = frag_counts.get(item.logic_fragment, 0) + 1

    ok = (
        total_deadlocks == 0
        and cond_deadlocks == 0
        and inv_violations == 0
        and vacuous_contracts == 0
        and dead_classes == 0
        and dl_conflicts == 0
        and lsp_violations == 0
        and when_issues == 0
        and wf_issues == 0
        and all(c.status != "UNKNOWN" for c in class_proofs)
        and all(r.status != "UNKNOWN" for r in reachability)
        and all(p.status != "UNKNOWN" for p in inv_pres)
        and all(w.status != "UNKNOWN" for w in when_proofs)
        and all(wf.status != "UNKNOWN" for wf in workflow_proofs)
        and (upgrade_report is None or upgrade_report.compatible)
    )
    total_rules = sum(len(a.preconditions) for a in eff_bp.actions)
    elapsed = (time.perf_counter() - t0) * 1000.0

    return ProofReport(
        ok=ok,
        model_id=eff_bp.model_id,
        version=eff_bp.version,
        blueprint_fingerprint=blueprint_fingerprint(eff_bp),
        bmc_bound=bmc_bound,
        total_actions_checked=len(reachability),
        total_rules_checked=total_rules,
        total_deadlock_count=total_deadlocks,
        conditional_deadlock_count=cond_deadlocks,
        invariant_violation_count=inv_violations,
        redundant_rule_count=len(redundant),
        dead_class_count=dead_classes,
        dl_conflict_count=dl_conflicts,
        lsp_violation_count=lsp_violations,
        exact_wp_proved_count=exact_wp_proved,
        contract_proved_count=contract_proved,
        vacuous_contract_count=vacuous_contracts,
        uncontracted_edit_function_count=uncontracted_efs,
        when_branch_checked_count=len(when_proofs),
        when_branch_issue_count=when_issues,
        workflow_checked_count=len(workflow_proofs),
        workflow_live_count=wf_live,
        workflow_action_verified_count=wf_action_verified,
        workflow_issue_count=wf_issues,
        logic_fragment_counts=frag_counts,
        class_proofs=class_proofs,
        reachability=reachability,
        redundant_rules=redundant,
        invariant_preservation=inv_pres,
        when_branch_proofs=when_proofs,
        workflow_liveness_proofs=workflow_proofs,
        upgrade_compatibility=upgrade_report,
        elapsed_ms=round(elapsed, 3),
    )
