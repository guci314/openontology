"""固定标准接口契约（Target ABI）的驱动反编译编排器（DriverDecompilerAgent · 纯 M2，领域无关）。

核心机制：
* 锁定目标标准蓝图 ``target_blueprint: DeclarativeBlueprint``（固定业务语义接口与不变量契约）；
* 由可插拔 ``AbductionBackend`` 面对外部系统画像 ``ExternalPhenomenonProfile``，
  反编译生成声明式设备映射 ``AdapterMappingSpec`` 与可选单调差量覆层 ``CustomerOverlaySpec``；
* 任何违反「只增不破」标准契约（``OVERLAY_BREAKS_STANDARD_CONTRACT``）、双向枚举缺口、
  或未能完整投影外部现象与必要接口动作的提案，均在五关 ``SymbolicCrucible`` 捕获为结构化反例并
  反馈进入下一轮 CEGIS 自纠，通过后经 ``InductionGovernanceService`` 同步登记结晶提案。
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..spec import Actor
from .backends import AbductionBackend
from .crucible import CrucibleContext, CrucibleReport, SymbolicCrucible
from .dsl import (
    ActionBlueprint,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    ObjectTypeBlueprint,
    ProposedEvalCase,
    apply_overlay,
    dsl_schema_prompt_guide,
)
from .governance import BUILDER_ACTOR, InductionGovernanceService
from .agents import InductionRoundTrace
from .phenomenon import (
    AdapterMappingSpec,
    EntityMappingSpec,
    ExternalPhenomenonProfile,
    project_external_phenomenon,
)
from .seed import SeedOntology

__all__ = [
    "DecompilationCycleResult",
    "build_decompiler_system_prompt",
    "DriverDecompilerAgent",
]

_DECOMPILER_PERSONA_HEADER = (
    "You are Driver Decompiler (标准接口驱动反编译器). "
    "A canonical target DeclarativeBlueprint (the Target ABI) is LOCKED below. Do NOT invent replacement "
    "domain classes, rename canonical fields/actions, or delete/weaken any standard object, field, primary key, "
    "invariant, action precondition, allowed role, or FSM transition.\n"
    "Given an ExternalPhenomenonProfile discovered from a heterogeneous source system (which may include dozens "
    "of unrelated noise tables, noise columns, alien primary keys, and proprietary source enum tokens), output ONLY "
    "one valid JSON object with exact top-level keys:\n"
    '  {"rationale": str, "adapter_mapping": <AdapterMappingSpec>, '
    '"customer_overlay": <CustomerOverlaySpec | null>, "proposed_eval_cases": [<ProposedEvalCase>, ...]}\n'
    "Strict rules:\n"
    "1. adapter_mapping must align source entities/columns in external_profile onto target_blueprint types/fields "
    "and ignore unrelated noise tables/columns.\n"
    "2. Map relation fields INSIDE the owning EntityMappingSpec.field_transforms (using target_field='<relation_field>', "
    "NEVER create a separate EntityMappingSpec whose target_object_type contains '.' like 'Obj.rel'). Use kind='id_prefix' "
    "with a distinct uppercase prefix ending in '-' on id_transform (so target primary key is string-prefixed), and use "
    "matching kind='foreign_key_id' prefixes on relation fields referencing parent master types. "
    "If two target types derive from the same external table, list the parent entity mapping before the child entity mapping.\n"
    "3. Use kind='enum_map' whenever external status/priority/stage tokens differ from canonical target FSM states or "
    "policy tokens; include BOTH active observed tokens AND target terminal/post-action state tokens in enum_map so "
    "bidirectional actuation and readback round-trip cleanly.\n"
    "4. Provide complete action_bindings in adapter_mapping covering every required action, matching exact "
    "action_name, target_object_type, external_table, id_param, and matching id_prefix; expected_state_field and "
    "expected_state_after MUST use the canonical Target ABI state field & post-action target state declared in "
    "target_blueprint (NEVER the raw external source enum token; store raw<->canonical translation in field_transforms enum_map).\n"
    "5. When overlay_requirement_doc is empty, set customer_overlay to null. When overlay_requirement_doc is provided, "
    "emit an additive/monotonic CustomerOverlaySpec ONLY (new owned fields, new action preconditions, narrowed roles, or "
    "scoped views); never populate remove_*, alter_fields with weaker constraints, update_primary_keys, or break terminal FSM edges.\n"
    "6. Every entry in proposed_eval_cases MUST use a real action declared in target_blueprint.actions (NEVER invent "
    "'noop' or undeclared action names), populate setup_objects using ONLY fresh synthetic IDs (e.g. 'EVAL-MASTER-1', "
    "'EVAL-DOC-1') that never collide with loaded M0 records, include all referenced parent master records, satisfy all "
    "action preconditions when expect.valid=true, and either assert expect={'valid': true/false, 'error_code': ...} "
    "without object_changes OR use exact dict shape {'object_type': '<Type>', 'object_id': '<id>', 'kind': 'update', 'after': {...}} inside expect.object_changes.\n"
)


def _summarize_target_abi(target_blueprint: DeclarativeBlueprint) -> dict[str, Any]:
    return {
        "model_id": target_blueprint.model_id,
        "version": target_blueprint.version,
        "seed_ontology_id": target_blueprint.seed_ontology_id,
        "object_types": [
            {
                "name": ot.name,
                "archetype": ot.archetype,
                "primary_key": ot.primary_key,
                "status_field": ot.status_field,
                "transitions": ot.transitions,
                "fields": [
                    {
                        "name": f.name,
                        "field_type": f.field_type,
                        "required": f.required or f.name == ot.primary_key,
                        "default": f.default,
                        "non_negative": f.non_negative,
                        "owned": f.owned,
                    }
                    for f in ot.fields
                ],
                "relations": [
                    {"field": r.field, "target_type": r.target_type, "required": r.required, "multiple": r.multiple}
                    for r in ot.relations
                ],
            }
            for ot in target_blueprint.object_types
        ],
        "actions": [
            {
                "name": act.name,
                "description": act.description,
                "edit_function": act.edit_function,
                "allowed_roles": act.allowed_roles,
                "params": [{"name": p.name, "field_type": p.field_type, "required": p.required} for p in act.params],
                "preconditions": [pre.model_dump(mode="json") for pre in act.preconditions],
                "mutations": [m.model_dump(mode="json") for m in act.mutations],
            }
            for act in target_blueprint.actions
        ],
        "invariants": [inv.name for inv in target_blueprint.invariants],
    }


def build_decompiler_system_prompt(
    *,
    target_blueprint: DeclarativeBlueprint,
    include_overlay: bool = False,
    seed_ontology: SeedOntology | None = None,
) -> str:
    adapter_schema = json.dumps(
        AdapterMappingSpec.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    eval_schema = json.dumps(
        ProposedEvalCase.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    abi_json = json.dumps(
        _summarize_target_abi(target_blueprint),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sections = [
        _DECOMPILER_PERSONA_HEADER,
        f"LOCKED_TARGET_BLUEPRINT_ABI:\n{abi_json}",
        f"ADAPTER_MAPPING_SCHEMA:\n{adapter_schema}",
        f"PROPOSED_EVAL_CASE_SCHEMA:\n{eval_schema}",
    ]
    if include_overlay:
        overlay_schema = json.dumps(
            CustomerOverlaySpec.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        ast_guide = dsl_schema_prompt_guide(mode="evolution", seed_ontology=seed_ontology)
        sections.append(f"CUSTOMER_OVERLAY_SCHEMA:\n{overlay_schema}")
        sections.append(f"OVERLAY_PREDICATE_AST_GUIDE:\n{ast_guide}")
    return "\n".join(sections)


def _validate_decompiled_alignment(
    *,
    effective_bp: DeclarativeBlueprint,
    external_profile: ExternalPhenomenonProfile,
    mapping: AdapterMappingSpec,
    projected_objects: Sequence[dict[str, Any]],
    required_object_types: Sequence[str],
    required_actions: Sequence[str],
) -> list[str]:
    """Domain-agnostic structural alignment & bidirectional enum coverage checks."""
    errors: list[str] = []
    ot_by_name: dict[str, ObjectTypeBlueprint] = {ot.name: ot for ot in effective_bp.object_types}
    act_by_name: dict[str, ActionBlueprint] = {a.name: a for a in effective_bp.actions}
    source_tables = {e.table_name: e for e in external_profile.entities}

    mapped_types: set[str] = set()
    em_by_type: dict[str, EntityMappingSpec] = {}
    for em in mapping.entity_mappings:
        if em.target_object_type not in ot_by_name:
            errors.append(
                f"UNKNOWN_TARGET_OBJECT_TYPE: entity mapping references unknown target {em.target_object_type!r}"
            )
            continue
        ot = ot_by_name[em.target_object_type]
        mapped_types.add(em.target_object_type)
        em_by_type[em.target_object_type] = em
        if em.id_transform.target_field != ot.primary_key:
            errors.append(
                f"PRIMARY_KEY_MAPPING_MISMATCH: {em.target_object_type} id_transform.target_field="
                f"{em.id_transform.target_field!r} must match primary_key={ot.primary_key!r}"
            )
        if em.source_table in source_tables and not em.id_transform.source_field:
            errors.append(
                f"MISSING_ID_SOURCE_FIELD: {em.target_object_type} id_transform requires source_field"
            )

    for req_ot in required_object_types:
        if req_ot not in mapped_types:
            errors.append(
                f"MISSING_REQUIRED_OBJECT_MAPPING: target object type {req_ot!r} has no EntityMappingSpec"
            )

    # Verify projected rows satisfy required target fields/relations and valid target FSM states
    req_fields_by_ot: dict[str, list[str]] = {}
    for ot in effective_bp.object_types:
        needed: list[str] = [ot.primary_key]
        for f in ot.fields:
            if (f.required or f.primary_key) and f.default is None and not f.owned and f.name not in needed:
                needed.append(f.name)
        for rel in ot.relations:
            if rel.required and not rel.owned and rel.field not in needed:
                needed.append(rel.field)
        req_fields_by_ot[ot.name] = needed

    seen_missing_fields: set[tuple[str, str]] = set()
    seen_invalid_states: set[tuple[str, str, Any]] = set()
    for entry in projected_objects:
        tname = str(entry.get("object_type") or "")
        ot = ot_by_name.get(tname)
        if ot is None:
            continue
        data = entry.get("data") or {}
        for req_f in req_fields_by_ot.get(tname, ()):
            val = data.get(req_f)
            if val is None or (isinstance(val, str) and not val.strip()):
                sig = (tname, req_f)
                if sig not in seen_missing_fields:
                    seen_missing_fields.add(sig)
                    errors.append(
                        f"MISSING_REQUIRED_FIELD_PROJECTION: projected {tname} record is missing required "
                        f"field/relation {req_f!r}"
                    )
        if ot.transitions and ot.status_field in data:
            st_val = data.get(ot.status_field)
            if st_val is not None and str(st_val) not in ot.transitions:
                st_sig = (tname, ot.status_field, str(st_val))
                if st_sig not in seen_invalid_states:
                    seen_invalid_states.add(st_sig)
                    errors.append(
                        f"INVALID_FSM_STATE_PROJECTION: {tname}.{ot.status_field}={st_val!r} is not in "
                        f"target transitions {sorted(ot.transitions)}; use kind='enum_map'"
                    )

    bound_actions: set[str] = set()
    for b in mapping.action_bindings:
        bound_actions.add(b.action_name)
        act = act_by_name.get(b.action_name)
        if act is None:
            errors.append(
                f"UNKNOWN_ACTION_BINDING: action_binding references unknown target action {b.action_name!r}"
            )
            continue
        if b.target_object_type not in ot_by_name:
            errors.append(
                f"UNKNOWN_ACTION_TARGET: action_binding {b.action_name!r} targets unknown {b.target_object_type!r}"
            )
            continue
        if b.external_table not in source_tables:
            errors.append(
                f"UNKNOWN_EXTERNAL_TABLE: action_binding {b.action_name!r} references missing external table {b.external_table!r}"
            )
        param_names = {p.name for p in act.params}
        if b.id_param not in param_names:
            errors.append(
                f"ACTION_ID_PARAM_MISMATCH: action_binding {b.action_name!r} id_param={b.id_param!r} "
                f"not found in action params {sorted(param_names)}"
            )
        ot = ot_by_name[b.target_object_type]
        if ot.transitions and b.expected_state_after:
            if b.expected_state_after not in ot.transitions:
                errors.append(
                    f"INVALID_BINDING_TARGET_STATE: action_binding {b.action_name!r} "
                    f"expected_state_after={b.expected_state_after!r} is not a valid state of {ot.name}"
                )
        em = em_by_type.get(b.target_object_type)
        if em is not None:
            if b.id_prefix != em.id_transform.prefix:
                errors.append(
                    f"ACTION_ID_PREFIX_MISMATCH: action_binding {b.action_name!r} id_prefix={b.id_prefix!r} "
                    f"must match {em.target_object_type} id_transform.prefix={em.id_transform.prefix!r}"
                )
            if b.expected_state_field and b.expected_state_after:
                sf_tf = next((tf for tf in em.field_transforms if tf.target_field == b.expected_state_field), None)
                if sf_tf is not None and sf_tf.kind == "enum_map":
                    if b.expected_state_after not in (sf_tf.enum_map or {}).values():
                        errors.append(
                            f"MISSING_REVERSE_ENUM_TARGET: {em.target_object_type}.{sf_tf.target_field} uses enum_map "
                            f"but lacks a source key mapping to action {b.action_name!r} "
                            f"expected_state_after={b.expected_state_after!r}"
                        )

    for req_act in required_actions:
        if req_act not in bound_actions:
            errors.append(
                f"MISSING_REQUIRED_ACTION_BINDING: required target action {req_act!r} has no ActionExecutionBinding"
            )

    return errors


@dataclass(frozen=True)
class DecompilationCycleResult:
    agent_persona: str
    proposal_id: str
    verified: bool
    pending_approval_run_id: str | None
    rounds: tuple[InductionRoundTrace, ...]
    final_report: CrucibleReport
    target_blueprint: DeclarativeBlueprint
    effective_blueprint: DeclarativeBlueprint | None
    adapter_mapping: AdapterMappingSpec | None
    customer_overlay: CustomerOverlaySpec | None = None
    projected_m0_objects: tuple[dict[str, Any], ...] = ()


class DriverDecompilerAgent:
    """Decompile heterogeneous external phenomena onto a locked standard Target ABI."""

    persona_name: str = "Driver Decompiler (标准接口驱动反编译器)"

    def __init__(
        self,
        *,
        backend: AbductionBackend,
        governance: InductionGovernanceService,
        crucible: SymbolicCrucible | None = None,
        seed_ontology: SeedOntology | None = None,
        actor: Actor = BUILDER_ACTOR,
    ) -> None:
        self.backend = backend
        self.governance = governance
        self.crucible = crucible or SymbolicCrucible()
        self.seed_ontology = seed_ontology
        self.actor = actor

    def run_decompilation_cycle(
        self,
        *,
        model_id: str,
        target_blueprint: DeclarativeBlueprint,
        external_profile: ExternalPhenomenonProfile,
        crucible_context: CrucibleContext | None = None,
        overlay_requirement_doc: str = "",
        phenomenon_brief: Mapping[str, Any] | None = None,
        required_object_types: Sequence[str] = (),
        required_actions: Sequence[str] = (),
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
        initial_feedback: str = "",
    ) -> DecompilationCycleResult:
        active_seed = (
            seed_ontology
            or (crucible_context.seed_ontology if crucible_context is not None else None)
            or self.seed_ontology
        )
        base_ctx = (
            copy.copy(crucible_context)
            if crucible_context is not None
            else CrucibleContext(mode="genesis", seed_ontology=active_seed)
        )
        if base_ctx.mode != "genesis":
            raise ValueError("DriverDecompilerAgent requires crucible_context.mode == 'genesis'")

        include_overlay = bool(overlay_requirement_doc.strip())
        pid = proposal_id or f"PROP-DEC-{model_id}-{uuid.uuid4().hex[:6]}"
        sys_prompt = build_decompiler_system_prompt(
            target_blueprint=target_blueprint,
            include_overlay=include_overlay,
            seed_ontology=active_seed,
        )
        abi_summary = _summarize_target_abi(target_blueprint)
        profile_summary = external_profile.summary_for_builder()

        traces: list[InductionRoundTrace] = []
        last_feedback = initial_feedback
        last_raw_proposal: dict[str, Any] | None = None
        last_report: CrucibleReport | None = None
        last_effective_bp: DeclarativeBlueprint | None = None
        last_mapping: AdapterMappingSpec | None = None
        last_overlay: CustomerOverlaySpec | None = None
        last_projected_m0: list[dict[str, Any]] = []
        pending_run_id: str | None = None
        proposal_created = False

        for r in range(1, max_rounds + 1):
            prompt_payload: dict[str, Any] = {
                "model_id": model_id,
                "round": r,
                "target_blueprint": abi_summary,
                "phenomenon_brief": dict(phenomenon_brief or {}),
                "external_profile": profile_summary,
                "required_object_types": list(required_object_types),
                "required_actions": list(required_actions),
                "overlay_requirement_doc": overlay_requirement_doc,
                "previous_crucible_feedback": last_feedback,
            }
            if last_raw_proposal is not None:
                prompt_payload["previous_proposal"] = last_raw_proposal
            if active_seed is not None:
                prompt_payload["seed_ontology"] = active_seed.prompt_summary()

            rid = f"RPT-{pid}-R{r}"
            try:
                raw_proposal = self.backend.propose_json(
                    system_prompt=sys_prompt,
                    user_prompt=json.dumps(prompt_payload, ensure_ascii=False, indent=2),
                    round_index=r,
                )
                if isinstance(raw_proposal, dict):
                    last_raw_proposal = raw_proposal
                else:
                    raise ValueError(
                        f"expected JSON object from backend, got {type(raw_proposal).__name__}"
                    )
                rationale = str(raw_proposal.get("rationale") or f"decompile round {r}")
                raw_mapping = raw_proposal.get("adapter_mapping")
                if raw_mapping is None:
                    raise ValueError(
                        "MISSING_ADAPTER_MAPPING: output must include 'adapter_mapping'"
                    )
                mapping = AdapterMappingSpec.model_validate(raw_mapping)

                raw_overlay = raw_proposal.get("customer_overlay")
                if raw_overlay is not None:
                    overlay = CustomerOverlaySpec.model_validate(raw_overlay)
                    effective_bp = apply_overlay(target_blueprint, overlay)
                else:
                    overlay = None
                    effective_bp = copy.deepcopy(target_blueprint)

                effective_bp.model_id = model_id
                if active_seed is not None and effective_bp.seed_ontology_id is None:
                    effective_bp.seed_ontology_id = active_seed.seed_id

                eval_cases = [
                    ProposedEvalCase.model_validate(c).model_dump(mode="json")
                    for c in (raw_proposal.get("proposed_eval_cases") or [])
                ]
            except Exception as exc:
                code = getattr(exc, "code", None)
                err_prefix = f"[{code}] " if code else ""
                report = self.crucible.record_parse_failure(
                    self.governance,
                    proposal_id=pid,
                    report_id=rid,
                    mode="genesis",
                    model_id=model_id,
                    version=target_blueprint.version,
                    error_message=f"{err_prefix}{type(exc).__name__}: {exc}",
                )
                last_report = report
                feedback = report.diagnostic_feedback()
                traces.append(
                    InductionRoundTrace(
                        round_index=r,
                        proposal_id=pid,
                        report_id=rid,
                        passed=False,
                        failed_gates=tuple(g.gate for g in report.failed_gates()),
                        feedback=feedback,
                    )
                )
                last_feedback = feedback
                continue

            last_effective_bp = effective_bp
            last_mapping = mapping
            last_overlay = overlay

            projected_objs, proj_errors = project_external_phenomenon(external_profile, mapping)
            align_errors = _validate_decompiled_alignment(
                effective_bp=effective_bp,
                external_profile=external_profile,
                mapping=mapping,
                projected_objects=projected_objs,
                required_object_types=required_object_types,
                required_actions=required_actions,
            )
            last_projected_m0 = projected_objs

            round_ctx = copy.copy(base_ctx)
            round_ctx.seed_ontology = active_seed
            round_ctx.adapter_mapping = mapping
            round_ctx.m0_objects = [*base_ctx.m0_objects, *projected_objs]
            round_ctx.proposed_eval_cases = [*base_ctx.proposed_eval_cases, *eval_cases]
            round_ctx.extra_gate2_errors = [
                *base_ctx.extra_gate2_errors,
                *proj_errors,
                *align_errors,
            ]

            bp_json = effective_bp.model_dump_json()
            mapping_json = mapping.model_dump_json()
            cases_json = json.dumps(eval_cases, ensure_ascii=False)
            if not proposal_created:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "propose_genesis_model",
                    {
                        "proposal_id": pid,
                        "model_id": effective_bp.model_id,
                        "target_version": effective_bp.version,
                        "title": f"Decompile {effective_bp.model_id} v{effective_bp.version}",
                        "rationale": rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )
                proposal_created = True
            else:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "revise_proposal",
                    {
                        "proposal_id": pid,
                        "rationale": rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )

            report = self.crucible.verify_and_record(
                self.governance,
                proposal_id=pid,
                report_id=rid,
                candidate=effective_bp,
                context=round_ctx,
            )
            last_report = report
            feedback = report.diagnostic_feedback()
            traces.append(
                InductionRoundTrace(
                    round_index=r,
                    proposal_id=pid,
                    report_id=rid,
                    passed=report.passed,
                    failed_gates=tuple(g.gate for g in report.failed_gates()),
                    feedback=feedback,
                )
            )
            last_feedback = feedback

            if report.passed:
                pending_run_id = f"CRYSTAL-{pid}"
                self.governance.request_crystallization(
                    pid,
                    actor=self.actor,
                    approval_note=f"decompiled & verified in round {r}",
                    run_id=pending_run_id,
                )
                break

        assert last_report is not None
        return DecompilationCycleResult(
            agent_persona=self.persona_name,
            proposal_id=pid,
            verified=last_report.passed,
            pending_approval_run_id=pending_run_id,
            rounds=tuple(traces),
            final_report=last_report,
            target_blueprint=target_blueprint,
            effective_blueprint=last_effective_bp,
            adapter_mapping=last_mapping,
            customer_overlay=last_overlay,
            projected_m0_objects=tuple(last_projected_m0),
        )
