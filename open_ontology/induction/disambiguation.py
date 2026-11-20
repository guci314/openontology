"""M2 domain-agnostic human-in-the-loop gray-zone disambiguation primitives.

Allows an induction workflow that has reached empirical structural certainty (~0.7)
while still holding unresolved policy gray zones (limit exceedances, reversal loops,
or modal SOP wording) to present structured choices to a human steward and compile
the chosen resolution deterministically into:
1. L1 declarative guardrails (``precondition`` hard rejection, role-conditional
   ``precondition`` approval gates returning ``APPROVAL_REQUIRED_*``, or explicit waiver)
   packaged inside an ``EvolutionPatch``;
2. Regression ``ProposedEvalCase`` / ``GoldenEvalCase`` entries locking the ruling in
   against future M1 evolution cycles through the 5-gate ``SymbolicCrucible``.

This module belongs strictly to M2 and contains zero domain, vendor, or transport terms.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from open_ontology.spec import Actor, PermissionDeniedError

from .agents import CallableAbductionBackend, OntologyEvolverAgent
from .crucible import CrucibleContext, CrucibleReport, SymbolicCrucible
from .dsl import (
    ActionBlueprint,
    DeclarativeBlueprint,
    EvolutionPatch,
    InvariantRuleSpec,
    apply_evolution_patch,
    validate_predicate_ast,
)
from .friction import FrictionClusterSummary, FrictionEvidence
from .governance import STEWARD_ACTOR, InductionGovernanceService
from .seed import SeedOntology

DisambiguationChoice = Literal["HARD_REJECT", "REQUIRE_APPROVAL", "ALLOW_AUTONOMOUS"]

_ALLOWED_AMBIGUITY_KINDS = frozenset({"limit_exceedance", "state_reversal", "orphan_relation", "sop_ambiguity"})


def default_disambiguation_options() -> list["DisambiguationOption"]:
    return [
        DisambiguationOption(
            choice_id="HARD_REJECT",
            label="硬拦截拒止（Precondition 阻断）",
            description="非特权角色触及该灰区条件时直接拦截拒绝（REJECTED），禁止继续推进。",
        ),
        DisambiguationOption(
            choice_id="REQUIRE_APPROVAL",
            label="签批门控拦截（APPROVAL_REQUIRED -> M1 审批流）",
            description="通过角色条件 precondition 拦截非特权角色（返回 APPROVAL_REQUIRED_*），交由上层 M1 审批对象流与授权角色在 t1 重算执行。",
        ),
        DisambiguationOption(
            choice_id="ALLOW_AUTONOMOUS",
            label="允许自主放行（写入豁免回归用例）",
            description="确认历史异常属于合法业务弹性，将该边界固化为放行黄金回归用例。",
        ),
    ]


class DisambiguationOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice_id: DisambiguationChoice
    label: str
    description: str = ""


class GrayZoneAmbiguity(BaseModel):
    """Domain-agnostic representation of a single unresolved behavioral/policy gray zone."""

    model_config = ConfigDict(extra="forbid")

    ambiguity_id: str
    kind: Literal["limit_exceedance", "state_reversal", "orphan_relation", "sop_ambiguity"]
    target_object_type: str
    target_action: str
    id_param: str = "record_id"
    summary: str
    sop_excerpt: str = ""
    predicate_ast: dict[str, Any]
    error_code: str
    approver_roles: list[str] = Field(default_factory=lambda: ["steward", "admin"])
    counterexample_samples: list[dict[str, Any]] = Field(default_factory=list)
    sample_exceeding_id: str | None = None
    sample_compliant_id: str | None = None
    recommended_choice: DisambiguationChoice = "REQUIRE_APPROVAL"
    options: list[DisambiguationOption] = Field(default_factory=default_disambiguation_options)

    @model_validator(mode="after")
    def _validate_ast(self) -> "GrayZoneAmbiguity":
        if self.kind not in _ALLOWED_AMBIGUITY_KINDS:
            raise ValueError(f"unsupported ambiguity kind {self.kind!r}")
        validate_predicate_ast(
            self.predicate_ast,
            allowed_roots=frozenset({"obj", "param", "actor"}),
            allow_view=True,
        )
        return self


@dataclass(frozen=True)
class StewardDisambiguationDecision:
    ambiguity_id: str
    choice_id: DisambiguationChoice
    decided_by: Actor
    rationale: str = ""


def compile_disambiguation_to_evolution_patch(
    *,
    base_blueprint: DeclarativeBlueprint,
    target_version: str,
    ambiguities: Sequence[GrayZoneAmbiguity],
    decisions: Sequence[StewardDisambiguationDecision],
    patch_id: str | None = None,
    ai_actor_dict: dict[str, Any] | None = None,
    steward_actor_dict: dict[str, Any] | None = None,
) -> EvolutionPatch:
    """Compile human Steward gray-zone rulings into a declarative EvolutionPatch + GoldenEvalCases."""
    if not decisions:
        raise ValueError("at least one StewardDisambiguationDecision is required")

    amb_map = {a.ambiguity_id: a for a in ambiguities}
    act_map = {a.name: copy.deepcopy(a) for a in base_blueprint.actions}
    updated_action_names: list[str] = []
    proposed_cases: list[dict[str, Any]] = []
    rationales: list[str] = []

    ai_actor = dict(
        ai_actor_dict or {"actor_id": "eval:ai_agent", "role": "ai_agent", "clearance_tags": ["*"]}
    )
    st_actor = dict(
        steward_actor_dict or {"actor_id": "eval:steward", "role": "steward", "clearance_tags": ["*"]}
    )

    for dec in decisions:
        amb = amb_map.get(dec.ambiguity_id)
        if amb is None:
            raise ValueError(f"unknown ambiguity_id {dec.ambiguity_id!r}")
        if dec.decided_by.role not in amb.approver_roles and dec.decided_by.role != "admin":
            raise PermissionDeniedError(
                f"actor role {dec.decided_by.role!r} is not authorized to resolve gray zone {amb.ambiguity_id!r}"
            )
        if amb.target_action not in act_map:
            raise ValueError(
                f"ambiguity {amb.ambiguity_id!r} references unknown action {amb.target_action!r}"
            )
        act: ActionBlueprint = act_map[amb.target_action]
        roles_lit = list(dict.fromkeys(amb.approver_roles or ["steward", "admin"]))
        slug = amb.ambiguity_id.lower().replace("-", "_")

        if dec.choice_id == "HARD_REJECT":
            act.allowed_roles = list(dict.fromkeys([*act.allowed_roles, *roles_lit]))
            act.preconditions.append(
                InvariantRuleSpec(
                    name=f"disambig_{slug}",
                    code=amb.error_code,
                    target_type=amb.target_object_type,
                    id_param=amb.id_param,
                    when=copy.deepcopy(amb.predicate_ast),
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": roles_lit},
                    },
                    description=(
                        dec.rationale
                        or f"Steward hard-block ruling on gray zone {amb.ambiguity_id}: {amb.summary}"
                    ),
                )
            )
            if amb.target_action not in updated_action_names:
                updated_action_names.append(amb.target_action)
            if amb.sample_exceeding_id:
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_hard_reject_ai",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_exceeding_id},
                        "actor": ai_actor,
                        "expect_valid": False,
                        "expect_error_code": amb.error_code,
                    }
                )
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_steward_override",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_exceeding_id},
                        "actor": st_actor,
                        "expect_valid": True,
                    }
                )
            if amb.sample_compliant_id:
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_compliant_ai",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_compliant_id},
                        "actor": ai_actor,
                        "expect_valid": True,
                    }
                )

        elif dec.choice_id == "REQUIRE_APPROVAL":
            appr_code = (
                amb.error_code
                if amb.error_code.startswith("APPROVAL_REQUIRED_")
                else f"APPROVAL_REQUIRED_{amb.error_code}"
            )
            act.allowed_roles = list(dict.fromkeys([*act.allowed_roles, *roles_lit]))
            act.preconditions.append(
                InvariantRuleSpec(
                    name=f"disambig_approval_{slug}",
                    code=appr_code,
                    target_type=amb.target_object_type,
                    id_param=amb.id_param,
                    when=copy.deepcopy(amb.predicate_ast),
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": roles_lit},
                    },
                    description=(
                        dec.rationale
                        or f"Steward approval-gate ruling on gray zone {amb.ambiguity_id}: {amb.summary}"
                    ),
                )
            )
            if amb.target_action not in updated_action_names:
                updated_action_names.append(amb.target_action)
            if amb.sample_exceeding_id:
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_require_approval_ai",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_exceeding_id},
                        "actor": ai_actor,
                        "expect_valid": False,
                        "expect_error_code": appr_code,
                    }
                )
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_steward_direct",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_exceeding_id},
                        "actor": st_actor,
                        "expect_valid": True,
                    }
                )
            if amb.sample_compliant_id:
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_within_limit_ai",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_compliant_id},
                        "actor": ai_actor,
                        "expect_valid": True,
                    }
                )

        else:  # ALLOW_AUTONOMOUS
            if amb.target_action not in updated_action_names:
                updated_action_names.append(amb.target_action)
            if amb.sample_exceeding_id:
                proposed_cases.append(
                    {
                        "name": f"golden_disambig_{slug}_allow_autonomous",
                        "action": amb.target_action,
                        "params": {amb.id_param: amb.sample_exceeding_id},
                        "actor": ai_actor,
                        "expect_valid": True,
                    }
                )

        rationales.append(f"{amb.ambiguity_id}->{dec.choice_id} by {dec.decided_by.actor_id}")

    return EvolutionPatch(
        patch_id=patch_id or f"PATCH-DISAMBIG-{uuid.uuid4().hex[:6]}",
        base_version=base_blueprint.version,
        target_version=target_version,
        rationale="; ".join(rationales),
        update_actions=[act_map[name] for name in updated_action_names],
        proposed_eval_cases=proposed_cases,
    )


def crystallize_disambiguated_patch(
    *,
    governance: InductionGovernanceService,
    crucible: SymbolicCrucible,
    model_id: str,
    base_blueprint: DeclarativeBlueprint,
    patch: EvolutionPatch,
    crucible_context: CrucibleContext,
    ambiguities: Sequence[GrayZoneAmbiguity] = (),
    seed_ontology: SeedOntology | None = None,
    steward: Actor = STEWARD_ACTOR,
) -> tuple[DeclarativeBlueprint, CrucibleReport, list[dict[str, Any]]]:
    """Run compiled Steward disambiguation EvolutionPatch through 5-gate Crucible and crystallize."""
    evidences = tuple(
        FrictionEvidence(
            run_id=f"GZ-{amb.ambiguity_id}",
            signal_kind="AMBIGUOUS_GRAY_ZONE_RULING",
            action_type=amb.target_action,
            actor_role="steward",
            signature=amb.error_code,
            params=({amb.id_param: amb.sample_exceeding_id} if amb.sample_exceeding_id else {}),
            summary=amb.summary,
            error_code=amb.error_code,
        )
        for amb in ambiguities
    )
    cluster = FrictionClusterSummary(
        cluster_id=f"CL-{patch.patch_id}",
        signal_kind="AMBIGUOUS_GRAY_ZONE_RULING",
        action_type=(patch.update_actions[0].name if patch.update_actions else "unknown"),
        signature=patch.patch_id,
        support_count=max(1, len(evidences)),
        run_ids=tuple(ev.run_id for ev in evidences) or (f"GZ-{patch.patch_id}",),
        escalated=True,
        evidences=evidences,
    )

    evolver = OntologyEvolverAgent(
        backend=CallableAbductionBackend(
            lambda _s, _u, _r: {"patch": patch.model_dump(mode="json")}
        ),
        governance=governance,
        crucible=crucible,
        seed_ontology=seed_ontology,
    )
    evo_ctx = copy.copy(crucible_context)
    evo_ctx.mode = "evolution"
    if seed_ontology is not None and evo_ctx.seed_ontology is None:
        evo_ctx.seed_ontology = seed_ontology

    # Drop stale unguarded baseline positive replays for exact records newly restricted
    # or gated by Steward disambiguation rulings (those records are verified by Gate 4).
    gated_targets: set[tuple[str, str, str]] = set()
    for case in patch.proposed_eval_cases:
        if not case.expect.valid or bool(case.expect.requires_approval):
            for p_key, p_val in case.params.items():
                gated_targets.add((case.action, str(p_key), str(p_val)))
    if gated_targets and evo_ctx.positive_events:
        evo_ctx.positive_events = [
            ev
            for ev in evo_ctx.positive_events
            if not (
                getattr(ev, "expected_valid", True)
                and not getattr(ev, "expected_requires_approval", False)
                and any((ev.action, str(k), str(v)) in gated_targets for k, v in ev.params.items())
            )
        ]

    evo_cycle = evolver.run_evolution_cycle(
        model_id=model_id,
        cluster=cluster,
        crucible_context=evo_ctx,
        base_blueprint=base_blueprint,
        seed_ontology=seed_ontology,
        max_rounds=1,
    )
    report = evo_cycle.final_report
    assert report is not None
    if not evo_cycle.verified or evo_cycle.pending_approval_run_id is None:
        return apply_evolution_patch(base_blueprint, patch), report, governance.get_golden_eval_cases(model_id)

    governance.approve_crystallization(evo_cycle.pending_approval_run_id, steward)
    active_bp = governance.get_active_blueprint(model_id)
    assert active_bp is not None
    return active_bp, report, governance.get_golden_eval_cases(model_id)
