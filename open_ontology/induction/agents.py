"""拟人化溯因编排器（OntologyBuilderAgent / OntologyEvolverAgent）与可插拔 LLM 后端（纯 M2）。

修复与保障：
* 系统提示词注入精确 JSON Schema（``DeclarativeBlueprint`` / ``AdapterMappingSpec`` / ``EvolutionPatch``
  与安全谓词 AST 操作符表），适配真实 LLM 调用；
* ``propose_json`` / ``model_validate`` / ``apply_evolution_patch`` 全程受 ``try/except`` 保护，
  格式或 Schema 偏差自动转成关 1 反例报告回喂给下一轮 CEGIS 自我修正，不再中途崩溃；
* 编排器物理上不接触 ``CRUCIBLE_ACTOR``，完全委托 ``SymbolicCrucible.verify_and_record()`` 独立签发报告。
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..spec import Actor, InvariantError
from .backends import (
    AbductionBackend,
    AntigravityCLIAbductionBackend,
    CallableAbductionBackend,
    GeminiAbductionBackend,
    OpenAICompatibleAbductionBackend,
    OpenRouterAbductionBackend,
)
from .crucible import CrucibleContext, CrucibleReport, SymbolicCrucible
from .dsl import (
    DeclarativeBlueprint,
    EvolutionPatch,
    ProposedEvalCase,
    apply_adapter_mapping_patch,
    apply_evolution_patch,
    dsl_schema_prompt_guide,
)
from .friction import FrictionClusterSummary
from .governance import BUILDER_ACTOR, EVOLVER_ACTOR, InductionGovernanceService
from .phenomenon import (
    AdapterMappingSpec,
    ExternalPhenomenonProfile,
    project_external_phenomenon,
)
from .seed import SeedOntology
from .utility import L2EvolutionTicket

__all__ = [
    "BUILDER_SYSTEM_PROMPT",
    "EVOLVER_SYSTEM_PROMPT",
    "L2_ARCHITECT_SYSTEM_PROMPT",
    "build_builder_system_prompt",
    "build_evolver_system_prompt",
    "build_l2_architect_system_prompt",
    "AbductionBackend",
    "CallableAbductionBackend",
    "GeminiAbductionBackend",
    "OpenAICompatibleAbductionBackend",
    "OpenRouterAbductionBackend",
    "AntigravityCLIAbductionBackend",
    "InductionRoundTrace",
    "InductionCycleResult",
    "OntologyBuilderAgent",
    "OntologyEvolverAgent",
    "OntologyL2ArchitectAgent",
]


_BUILDER_PERSONA_HEADER = (
    "You are Ontology Builder (本体建造师 · 驻场测绘官). "
    "Given external domain phenomena, scout reports, and requirement documents, "
    "propose a strict L1 zero-exec DeclarativeBlueprint JSON (and AdapterMappingSpec JSON when external_profile is provided). "
    "Output ONLY valid JSON matching the exact schema below (extra keys are forbidden). "
    "Never hardcode concrete instance IDs or business codes. If Symbolic Crucible refutes your proposal, "
    "fix every reported counterexample.\n"
    "SCHEMA_GUIDE:\n"
)

_EVOLVER_PERSONA_HEADER = (
    "You are Ontology Evolver (本体进化师 · 业务法医). "
    "Given a base DeclarativeBlueprint and an escalated M0 FrictionClusterSummary, "
    "propose a minimal, general EvolutionPatch JSON with any required FieldMigrationSpec and new GoldenEvalCase assertions. "
    "Output ONLY valid JSON matching the exact schema below (extra keys are forbidden). "
    "Never overfit to concrete instance IDs/codes and never break historical golden eval cases.\n"
    "SCHEMA_GUIDE:\n"
)

_L2_ARCHITECT_PERSONA_HEADER = (
    "You are Ontology L2 Architect (本体结构与业务规则架构师). "
    "Given L2EvolutionTickets (structural expressivity/capability/settlement gaps), the current "
    "DeclarativeBlueprint + AdapterMappingSpec, and external domain phenomenon context, propose a pure M1 "
    "EvolutionPatch JSON adding new M1 fields/relations/actions (using safe 'if_else'/arithmetic expressions "
    "and preconditions), FieldMigrations, update_entity_mappings, declarative 1-to-1 "
    "add_action_bindings/update_action_bindings (declaring extra_params when actions take extra arguments), "
    "and self-contained ProposedEvalCase assertions. Output ONLY valid JSON matching the exact schema below.\n"
    "Never embed external wire/ORM protocols, concrete instance IDs, or break historical eval cases.\n"
    "SCHEMA_GUIDE:\n"
)


def build_builder_system_prompt(
    *,
    include_adapter_schema: bool = True,
    seed_ontology: SeedOntology | None = None,
) -> str:
    guide = dsl_schema_prompt_guide(mode="genesis", seed_ontology=seed_ontology)
    if include_adapter_schema:
        adapter_schema = json.dumps(
            AdapterMappingSpec.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"{_BUILDER_PERSONA_HEADER}{guide}\nADAPTER_MAPPING_SCHEMA:\n{adapter_schema}"
    return f"{_BUILDER_PERSONA_HEADER}{guide}"


def build_evolver_system_prompt(*, seed_ontology: SeedOntology | None = None) -> str:
    return f"{_EVOLVER_PERSONA_HEADER}{dsl_schema_prompt_guide(mode='evolution', seed_ontology=seed_ontology)}"


def build_l2_architect_system_prompt(*, seed_ontology: SeedOntology | None = None) -> str:
    guide = dsl_schema_prompt_guide(mode="evolution", seed_ontology=seed_ontology)
    adapter_schema = json.dumps(
        AdapterMappingSpec.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{_L2_ARCHITECT_PERSONA_HEADER}{guide}\nADAPTER_MAPPING_SCHEMA:\n{adapter_schema}"


BUILDER_SYSTEM_PROMPT = build_builder_system_prompt(include_adapter_schema=True)
EVOLVER_SYSTEM_PROMPT = build_evolver_system_prompt()
L2_ARCHITECT_SYSTEM_PROMPT = build_l2_architect_system_prompt()


@dataclass(frozen=True)
class InductionRoundTrace:
    round_index: int
    proposal_id: str
    report_id: str
    passed: bool
    failed_gates: tuple[str, ...]
    feedback: str


@dataclass(frozen=True)
class InductionCycleResult:
    agent_persona: str
    proposal_id: str
    verified: bool
    pending_approval_run_id: str | None
    rounds: tuple[InductionRoundTrace, ...]
    final_report: CrucibleReport
    candidate_blueprint: DeclarativeBlueprint | None
    patch: EvolutionPatch | None = None
    adapter_mapping: AdapterMappingSpec | None = None
    projected_m0_objects: tuple[dict[str, Any], ...] = ()


class OntologyBuilderAgent:
    """Anthropomorphized 0->1 Ontology Builder running fault-tolerant CEGIS against SymbolicCrucible."""

    persona_name: str = "Ontology Builder (本体建造师)"

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

    def run_genesis_cycle(
        self,
        *,
        model_id: str,
        phenomenon_brief: dict[str, Any] | None = None,
        crucible_context: CrucibleContext,
        requirement_doc: str = "",
        external_profile: ExternalPhenomenonProfile | None = None,
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
    ) -> InductionCycleResult:
        if crucible_context.mode != "genesis":
            raise ValueError("OntologyBuilderAgent requires crucible_context.mode == 'genesis'")

        active_seed = seed_ontology or crucible_context.seed_ontology or self.seed_ontology
        pid = proposal_id or f"PROP-GEN-{model_id}-{uuid.uuid4().hex[:6]}"
        sys_prompt = build_builder_system_prompt(
            include_adapter_schema=external_profile is not None,
            seed_ontology=active_seed,
        )
        traces: list[InductionRoundTrace] = []
        last_feedback = ""
        last_report: CrucibleReport | None = None
        last_blueprint: DeclarativeBlueprint | None = None
        last_mapping: AdapterMappingSpec | None = None
        last_projected_m0: list[dict[str, Any]] = []
        pending_run_id: str | None = None
        proposal_created = False

        for r in range(1, max_rounds + 1):
            prompt_payload: dict[str, Any] = {
                "model_id": model_id,
                "round": r,
                "requirement_doc": requirement_doc,
                "phenomenon": phenomenon_brief or {},
                "previous_crucible_feedback": last_feedback,
            }
            if active_seed is not None:
                prompt_payload["seed_ontology"] = active_seed.prompt_summary()
            if external_profile is not None:
                prompt_payload["external_profile"] = external_profile.summary_for_builder()

            rid = f"RPT-{pid}-R{r}"
            try:
                raw_proposal = self.backend.propose_json(
                    system_prompt=sys_prompt,
                    user_prompt=json.dumps(prompt_payload, ensure_ascii=False, indent=2),
                    round_index=r,
                )
                if not isinstance(raw_proposal, dict):
                    raise ValueError(f"expected JSON object from backend, got {type(raw_proposal).__name__}")
                bp_dict = raw_proposal.get("blueprint", raw_proposal)
                eval_cases = [
                    ProposedEvalCase.model_validate(c).model_dump(mode="json")
                    for c in (raw_proposal.get("proposed_eval_cases") or [])
                ]
                rationale = str(raw_proposal.get("rationale") or f"genesis round {r}")
                blueprint = DeclarativeBlueprint.model_validate(bp_dict)
                raw_mapping = raw_proposal.get("adapter_mapping")
                mapping = (
                    AdapterMappingSpec.model_validate(raw_mapping)
                    if raw_mapping is not None
                    else None
                )
            except Exception as exc:
                # P0-1 fix: capture parse/schema errors and feed back into next CEGIS round
                report = self.crucible.record_parse_failure(
                    self.governance,
                    proposal_id=pid,
                    report_id=rid,
                    mode="genesis",
                    model_id=model_id,
                    version="0.0.0",
                    error_message=f"{type(exc).__name__}: {exc}",
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

            last_blueprint = blueprint
            if active_seed is not None and blueprint.seed_ontology_id is None:
                blueprint.seed_ontology_id = active_seed.seed_id
            mapping_json = "{}"
            round_ctx = copy.copy(crucible_context)
            round_ctx.seed_ontology = active_seed
            round_ctx.extra_gate2_errors = list(crucible_context.extra_gate2_errors)
            round_ctx.proposed_eval_cases = [*crucible_context.proposed_eval_cases, *eval_cases]
            round_ctx.adapter_mapping = mapping

            if external_profile is not None:
                if mapping is None:
                    round_ctx.extra_gate2_errors.append(
                        "MISSING_ADAPTER_MAPPING: Builder must emit adapter_mapping when external_profile is supplied"
                    )
                    last_projected_m0 = []
                else:
                    last_mapping = mapping
                    mapping_json = mapping.model_dump_json()
                    projected_objs, proj_errors = project_external_phenomenon(
                        external_profile, mapping
                    )
                    last_projected_m0 = projected_objs
                    round_ctx.m0_objects = [*crucible_context.m0_objects, *projected_objs]
                    round_ctx.extra_gate2_errors.extend(proj_errors)
            elif mapping is not None:
                last_mapping = mapping
                mapping_json = mapping.model_dump_json()

            bp_json = blueprint.model_dump_json()
            cases_json = json.dumps(eval_cases, ensure_ascii=False)
            if not proposal_created:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "propose_genesis_model",
                    {
                        "proposal_id": pid,
                        "model_id": blueprint.model_id,
                        "target_version": blueprint.version,
                        "title": f"Genesis {blueprint.model_id} v{blueprint.version}",
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

            # P2-3 fix: Crucible independently evaluates & signs FalsificationReport under its own actor
            report = self.crucible.verify_and_record(
                self.governance,
                proposal_id=pid,
                report_id=rid,
                candidate=blueprint,
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
                self.governance.runtime.submit_action(
                    pending_run_id,
                    "crystallize_model",
                    {"proposal_id": pid, "approval_note": f"verified in round {r}"},
                    self.actor,
                )
                break

        assert last_report is not None
        return InductionCycleResult(
            agent_persona=self.persona_name,
            proposal_id=pid,
            verified=last_report.passed,
            pending_approval_run_id=pending_run_id,
            rounds=tuple(traces),
            final_report=last_report,
            candidate_blueprint=last_blueprint,
            adapter_mapping=last_mapping,
            projected_m0_objects=tuple(last_projected_m0),
        )


class OntologyEvolverAgent:
    """Anthropomorphized 1->N Ontology Evolver running fault-tolerant CEGIS against SymbolicCrucible."""

    persona_name: str = "Ontology Evolver (本体进化师)"

    def __init__(
        self,
        *,
        backend: AbductionBackend,
        governance: InductionGovernanceService,
        crucible: SymbolicCrucible | None = None,
        seed_ontology: SeedOntology | None = None,
        actor: Actor = EVOLVER_ACTOR,
    ) -> None:
        self.backend = backend
        self.governance = governance
        self.crucible = crucible or SymbolicCrucible()
        self.seed_ontology = seed_ontology
        self.actor = actor

    def run_evolution_cycle(
        self,
        *,
        model_id: str,
        cluster: FrictionClusterSummary,
        crucible_context: CrucibleContext,
        base_blueprint: DeclarativeBlueprint | None = None,
        base_mapping: AdapterMappingSpec | None = None,
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
        system_prompt_override: str | None = None,
        extra_prompt_context: dict[str, Any] | None = None,
        l2_validation_hook: Callable[
            [DeclarativeBlueprint, AdapterMappingSpec | None], list[str]
        ] | None = None,
    ) -> InductionCycleResult:
        if crucible_context.mode != "evolution":
            raise ValueError("OntologyEvolverAgent requires crucible_context.mode == 'evolution'")
        if not cluster.escalated:
            raise InvariantError(
                f"friction cluster {cluster.cluster_id!r} (support={cluster.support_count}) "
                "has not met escalation threshold; refusing to trigger evolution on noise",
                code="BELOW_ESCALATION_THRESHOLD",
            )

        base = base_blueprint or self.governance.get_active_blueprint(model_id)
        if base is None:
            raise InvariantError(
                f"no active crystallized blueprint found for model {model_id!r}",
                code="NO_ACTIVE_MODEL",
            )
        active_base_mapping = (
            base_mapping
            or crucible_context.adapter_mapping
            or self.governance.get_active_adapter_mapping(model_id)
        )

        # Record friction cluster under crucible's own actor
        self.governance.runtime.submit_action(
            f"FRC-REC-{cluster.cluster_id}-{uuid.uuid4().hex[:6]}",
            "record_friction_cluster",
            {
                "cluster_id": cluster.cluster_id,
                "model_id": model_id,
                "signal_kind": cluster.signal_kind,
                "action_type": cluster.action_type,
                "signature": cluster.signature,
                "support_count": cluster.support_count,
                "run_ids_json": json.dumps(list(cluster.run_ids), ensure_ascii=False),
            },
            self.crucible._actor,
        )

        active_seed = seed_ontology or crucible_context.seed_ontology or self.seed_ontology
        merged_ctx = copy.copy(crucible_context)
        merged_ctx.seed_ontology = active_seed
        existing_golden = self.governance.get_golden_eval_cases(model_id)
        seen_names = {str(c.get("name")) for c in merged_ctx.golden_eval_cases}
        combined_golden = list(merged_ctx.golden_eval_cases)
        for gc in existing_golden:
            if str(gc.get("name")) not in seen_names:
                combined_golden.append(gc)
                seen_names.add(str(gc.get("name")))
        merged_ctx.golden_eval_cases = combined_golden

        pid = proposal_id or f"PROP-EVO-{model_id}-{uuid.uuid4().hex[:6]}"
        sys_prompt = system_prompt_override or build_evolver_system_prompt(seed_ontology=active_seed)
        traces: list[InductionRoundTrace] = []
        last_feedback = ""
        last_report: CrucibleReport | None = None
        last_blueprint: DeclarativeBlueprint = base
        last_patch: EvolutionPatch | None = None
        last_mapping: AdapterMappingSpec | None = active_base_mapping
        pending_run_id: str | None = None
        proposal_created = False

        for r in range(1, max_rounds + 1):
            prompt_payload: dict[str, Any] = {
                "model_id": model_id,
                "round": r,
                "base_version": base.version,
                "base_blueprint": base.model_dump(mode="json"),
                "friction_cluster": {
                    "cluster_id": cluster.cluster_id,
                    "signal_kind": cluster.signal_kind,
                    "action_type": cluster.action_type,
                    "signature": cluster.signature,
                    "support_count": cluster.support_count,
                    "evidences": [
                        {
                            "run_id": e.run_id,
                            "actor_role": e.actor_role,
                            "params": e.params,
                            "summary": e.summary,
                            "error_code": e.error_code,
                        }
                        for e in cluster.evidences
                    ],
                },
                "previous_crucible_feedback": last_feedback,
            }
            if active_base_mapping is not None:
                prompt_payload["base_adapter_mapping"] = active_base_mapping.model_dump(mode="json")
            if active_seed is not None:
                prompt_payload["seed_ontology"] = active_seed.prompt_summary()
            if extra_prompt_context:
                prompt_payload.update(extra_prompt_context)
            rid = f"RPT-{pid}-R{r}"
            try:
                raw_patch = self.backend.propose_json(
                    system_prompt=sys_prompt,
                    user_prompt=json.dumps(prompt_payload, ensure_ascii=False, indent=2),
                    round_index=r,
                )
                if not isinstance(raw_patch, dict):
                    raise ValueError(f"expected JSON object from backend, got {type(raw_patch).__name__}")
                patch = EvolutionPatch.model_validate(raw_patch.get("patch", raw_patch))
                candidate = apply_evolution_patch(base, patch)
                updated_mapping = apply_adapter_mapping_patch(active_base_mapping, patch)
            except Exception as exc:
                # P0-1 fix: catch schema/patch errors and feed back into next CEGIS round
                report = self.crucible.record_parse_failure(
                    self.governance,
                    proposal_id=pid,
                    report_id=rid,
                    mode="evolution",
                    model_id=model_id,
                    version=base.version,
                    error_message=f"{type(exc).__name__}: {exc}",
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

            last_patch = patch
            last_blueprint = candidate
            last_mapping = updated_mapping
            bp_json = candidate.model_dump_json()
            patch_json = patch.model_dump_json()
            mapping_json = updated_mapping.model_dump_json() if updated_mapping is not None else "{}"
            cases_json = json.dumps(
                [c.model_dump(mode="json") for c in patch.proposed_eval_cases],
                ensure_ascii=False,
            )
            if not proposal_created:
                self.governance.runtime.submit_action(
                    f"ACT-{pid}-R{r}",
                    "propose_evolution_patch",
                    {
                        "proposal_id": pid,
                        "model_id": model_id,
                        "cluster_id": cluster.cluster_id,
                        "base_version": base.version,
                        "target_version": patch.target_version,
                        "title": f"Evolve {model_id} {base.version}->{patch.target_version}",
                        "rationale": patch.rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "patch_json": patch_json,
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
                        "rationale": patch.rationale,
                        "blueprint_json": bp_json,
                        "adapter_mapping_json": mapping_json,
                        "patch_json": patch_json,
                        "proposed_eval_cases_json": cases_json,
                    },
                    self.actor,
                )

            round_ctx = copy.copy(merged_ctx)
            round_ctx.adapter_mapping = updated_mapping
            round_ctx.extra_gate2_errors = list(merged_ctx.extra_gate2_errors)
            if l2_validation_hook is not None:
                round_ctx.extra_gate2_errors.extend(
                    l2_validation_hook(candidate, updated_mapping)
                )

            report = self.crucible.verify_and_record(
                self.governance,
                proposal_id=pid,
                report_id=rid,
                candidate=candidate,
                context=round_ctx,
                patch=patch,
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
                self.governance.runtime.submit_action(
                    pending_run_id,
                    "crystallize_model",
                    {"proposal_id": pid, "approval_note": f"verified in round {r}"},
                    self.actor,
                )
                break

        assert last_report is not None
        return InductionCycleResult(
            agent_persona=self.persona_name,
            proposal_id=pid,
            verified=last_report.passed,
            pending_approval_run_id=pending_run_id,
            rounds=tuple(traces),
            final_report=last_report,
            candidate_blueprint=last_blueprint,
            patch=last_patch,
            adapter_mapping=last_mapping,
        )


class OntologyL2ArchitectAgent:
    """Anthropomorphized 1->N L2 M1 structural & business-rule architect over SymbolicCrucible."""

    persona_name: str = "Ontology L2 Architect (本体结构与业务规则架构师)"

    def __init__(
        self,
        *,
        backend: AbductionBackend,
        governance: InductionGovernanceService,
        crucible: SymbolicCrucible | None = None,
        seed_ontology: SeedOntology | None = None,
        actor: Actor = EVOLVER_ACTOR,
    ) -> None:
        self._evolver = OntologyEvolverAgent(
            backend=backend,
            governance=governance,
            crucible=crucible,
            seed_ontology=seed_ontology,
            actor=actor,
        )
        self._evolver.persona_name = self.persona_name

    def run_l2_evolution_cycle(
        self,
        *,
        model_id: str,
        l2_tickets: Sequence[L2EvolutionTicket],
        crucible_context: CrucibleContext,
        external_phenomenon_context: Mapping[str, Any] | None = None,
        base_blueprint: DeclarativeBlueprint | None = None,
        base_mapping: AdapterMappingSpec | None = None,
        seed_ontology: SeedOntology | None = None,
        max_rounds: int = 4,
        proposal_id: str | None = None,
        l2_validation_hook: Callable[
            [DeclarativeBlueprint, AdapterMappingSpec | None], list[str]
        ] | None = None,
    ) -> InductionCycleResult:
        if not l2_tickets:
            raise ValueError("OntologyL2ArchitectAgent requires at least one L2EvolutionTicket")

        cluster = FrictionClusterSummary(
            cluster_id=f"L2-STRUCT-{model_id}-{uuid.uuid4().hex[:6]}",
            signal_kind="L2_CAPABILITY_GAP",
            action_type=l2_tickets[0].capability_gap,
            signature="+".join(sorted({t.probe_id for t in l2_tickets})),
            support_count=max(2, len(l2_tickets)),
            run_ids=tuple(t.ticket_id for t in l2_tickets),
            escalated=True,
            evidences=(),
        )
        active_seed = (
            seed_ontology or crucible_context.seed_ontology or self._evolver.seed_ontology
        )
        sys_prompt = build_l2_architect_system_prompt(seed_ontology=active_seed)
        extra_ctx: dict[str, Any] = {
            "l2_tickets": [t.to_dict() for t in l2_tickets],
            "external_phenomenon_context": dict(external_phenomenon_context or {}),
        }
        return self._evolver.run_evolution_cycle(
            model_id=model_id,
            cluster=cluster,
            crucible_context=crucible_context,
            base_blueprint=base_blueprint,
            base_mapping=base_mapping,
            seed_ontology=active_seed,
            max_rounds=max_rounds,
            proposal_id=proposal_id or f"PROP-L2-{model_id}-{uuid.uuid4().hex[:6]}",
            system_prompt_override=sys_prompt,
            extra_prompt_context=extra_ctx,
            l2_validation_hook=l2_validation_hook,
        )
