"""面向 Agent 的客观实用性评测器与双回路演化分诊器（纯 M2，领域无关）。

核心设计原则：
1. **面向 Agent 的复杂度 $C_{\\text{agent}}$**：仅以暴露给 Agent 的本体对象、字段、
   OSDK 工具 JSON Schema Token 估算与动作步长衡量认知负担，不把适配器代码量计入扣分项；
2. **反偷渡硬门禁（Anti-Smuggling Gate）**：为了防止“把业务逻辑/风控偷渡进外部连接器”
   或“一键黑盒万能动作”作弊，评测器强制在断网纯 ``:memory:`` 沙盘内对每条策略/动作做
   离线对照检验，且要求所有提交变更产生非空可观测 ``object_diff``；
3. **四桶正交客观评测（外部物理真值验收）**：
   * ``T1_routine``    —— 常规业务流在外部物理账本的终态达成率与平均步数；
   * ``T2_governance`` —— 对抗越权拦阻率（Recall / 零外部脏写）与合规放行精度（Precision / 低误杀）；
   * ``T3_leakage``    —— 抽象泄漏探针下的同态保真度（$1 - \\text{MismatchRate}$）；
   * ``T4_escape``     —— 复杂真实需求下的业务逃逸率（Escape Rate）与表达覆盖度；
4. **双回路演化分诊（L1 自动修复 vs L2 工程工单）**：
   * L1 可表达的护栏/前置条件缺口自动组装为 ``FrictionClusterSummary`` +
     ``IncidentCounterexample``，直通 ``OntologyEvolverAgent``；
   * 超出 L1 声明式 DSL 表达边界的泄漏/逃逸问题自动沉淀为带外部物理证据与 Red Test 规格的
     ``L2EvolutionTicket``。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from ..model_views import resolve_scoped_names
from ..osdk import export_llm_tools
from ..runtime import OntologyRuntime
from ..spec import Actor, OntologySpec, ScopedViewSpec
from .crucible import IncidentCounterexample
from .friction import FrictionClusterSummary, FrictionEvidence

__all__ = [
    "AgentComplexityMetrics",
    "AntiSmugglingProbeSpec",
    "AntiSmugglingViolation",
    "AntiSmugglingCheckResult",
    "UtilityProbeOutcome",
    "UtilityWeights",
    "UtilityConstraints",
    "UtilityScorecard",
    "L1RepairCandidate",
    "L2EvolutionTicket",
    "EvolutionRoutingPlan",
    "RegimeComparisonSummary",
    "measure_agent_complexity",
    "measure_raw_external_complexity",
    "verify_anti_smuggling_parity",
    "evaluate_utility_scorecard",
    "triage_utility_failures",
    "compare_scorecards",
    "is_strict_utility_improvement",
]

ProbeBucket = Literal["T1_routine", "T2_governance", "T3_leakage", "T4_escape"]
TriageHint = Literal["none", "L1_guardrail_or_precondition", "L2_structural_expressivity"]


# ---------------------------------------------------------------------------
# 1. Agent Complexity Metrics (C_agent)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentComplexityMetrics:
    """Cognitive & tool-schema surface exposed directly to an AI Agent."""

    regime_name: str
    object_type_count: int
    exposed_field_count: int
    exposed_action_count: int
    exposed_function_count: int
    osdk_tool_schema_chars: int
    osdk_tool_schema_tokens_est: int
    raw_external_entity_count: int | None = None
    raw_external_field_count: int | None = None
    field_compression_ratio: float | None = None
    normalized_c_agent: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_name": self.regime_name,
            "object_type_count": self.object_type_count,
            "exposed_field_count": self.exposed_field_count,
            "exposed_action_count": self.exposed_action_count,
            "exposed_function_count": self.exposed_function_count,
            "osdk_tool_schema_chars": self.osdk_tool_schema_chars,
            "osdk_tool_schema_tokens_est": self.osdk_tool_schema_tokens_est,
            "raw_external_entity_count": self.raw_external_entity_count,
            "raw_external_field_count": self.raw_external_field_count,
            "field_compression_ratio": (
                round(self.field_compression_ratio, 4)
                if self.field_compression_ratio is not None
                else None
            ),
            "normalized_c_agent": round(self.normalized_c_agent, 4),
        }


def measure_agent_complexity(
    spec: OntologySpec,
    *,
    regime_name: str,
    raw_external_entity_count: int | None = None,
    raw_external_field_count: int | None = None,
    reference_token_budget: int = 25_000,
    actor: Actor | None = None,
    role: str | None = None,
    view: str | ScopedViewSpec | None = None,
    view_tags: Sequence[str] | None = None,
) -> AgentComplexityMetrics:
    """Measure exact OSDK tool + object schema footprint exposed to an Agent."""
    scoped = resolve_scoped_names(
        spec,
        actor=actor,
        role=role,
        view=view,
        view_tags=view_tags,
        public_only=True,
    )
    tools = export_llm_tools(
        spec,
        actor=actor,
        role=role,
        view=view,
        view_tags=view_tags,
    )
    object_schemas: dict[str, Any] = {}
    exposed_field_count = 0
    scoped_obj_count = 0
    for name, cls in sorted(spec.object_types.items()):
        if name not in scoped["object_types"]:
            continue
        scoped_obj_count += 1
        schema = cls.model_json_schema()
        object_schemas[name] = schema
        exposed_field_count += len(cls.model_fields)

    payload = {
        "object_types": object_schemas,
        "tools": tools,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    chars = len(serialized)
    tokens_est = max(1, math.ceil(chars / 4))

    exposed_actions = len(scoped["actions"])
    exposed_functions = len(scoped["functions"])
    compression_ratio: float | None = None
    if raw_external_field_count and raw_external_field_count > 0:
        compression_ratio = exposed_field_count / raw_external_field_count

    norm_c = min(1.0, tokens_est / max(1, reference_token_budget))
    return AgentComplexityMetrics(
        regime_name=regime_name,
        object_type_count=scoped_obj_count,
        exposed_field_count=exposed_field_count,
        exposed_action_count=exposed_actions,
        exposed_function_count=exposed_functions,
        osdk_tool_schema_chars=chars,
        osdk_tool_schema_tokens_est=tokens_est,
        raw_external_entity_count=raw_external_entity_count,
        raw_external_field_count=raw_external_field_count,
        field_compression_ratio=compression_ratio,
        normalized_c_agent=norm_c,
    )


def measure_raw_external_complexity(
    *,
    regime_name: str,
    entity_count: int,
    field_count: int,
    action_count: int,
    schema_payload: Any | None = None,
    raw_total_entity_count: int | None = None,
    raw_total_field_count: int | None = None,
    reference_token_budget: int | None = None,
) -> AgentComplexityMetrics:
    """Measure cognitive schema footprint of operating directly on a raw external system."""
    if schema_payload is not None:
        serialized = json.dumps(schema_payload, ensure_ascii=False, sort_keys=True)
        chars = len(serialized)
    else:
        # Conservative deterministic lower bound: ~110 JSON schema chars per raw field + ~180 per RPC method
        chars = max(1, field_count * 110 + action_count * 180)
    tokens_est = max(1, math.ceil(chars / 4))
    budget = reference_token_budget or tokens_est
    norm_c = min(1.0, tokens_est / max(1, budget))
    total_fields = raw_total_field_count or field_count
    return AgentComplexityMetrics(
        regime_name=regime_name,
        object_type_count=entity_count,
        exposed_field_count=field_count,
        exposed_action_count=action_count,
        exposed_function_count=0,
        osdk_tool_schema_chars=chars,
        osdk_tool_schema_tokens_est=tokens_est,
        raw_external_entity_count=raw_total_entity_count or entity_count,
        raw_external_field_count=total_fields,
        field_compression_ratio=field_count / max(1, total_fields),
        normalized_c_agent=norm_c,
    )


# ---------------------------------------------------------------------------
# 2. Anti-Smuggling Hard Gate (Offline Pure Sandbox vs Online Execution)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AntiSmugglingProbeSpec:
    """Differential parity test comparing pure offline sandbox against online execution."""

    case_id: str
    description: str
    action_name: str
    params: dict[str, Any]
    actor: Actor
    online_status: Literal["COMMITTED", "REJECTED"]
    online_error_code: str | None = None
    expect_state_mutation: bool = True


@dataclass(frozen=True)
class AntiSmugglingViolation:
    case_id: str
    violation_code: Literal[
        "ADAPTER_LOGIC_SMUGGLING",
        "BLACKBOX_NO_STATE_DIFF",
        "OFFLINE_ONLINE_DIVERGENCE",
    ]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "violation_code": self.violation_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AntiSmugglingCheckResult:
    passed: bool
    checked_cases: int
    violations: tuple[AntiSmugglingViolation, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_cases": self.checked_cases,
            "violations": [v.to_dict() for v in self.violations],
            "note": self.note,
        }


def verify_anti_smuggling_parity(
    spec: OntologySpec,
    m0_objects: Sequence[Mapping[str, Any]],
    probes: Sequence[AntiSmugglingProbeSpec],
    *,
    overlay_records: Sequence[Mapping[str, Any]] = (),
    m0_links: Sequence[Mapping[str, Any]] = (),
) -> AntiSmugglingCheckResult:
    """Enforce Anti-Smuggling & Anti-Blackbox hard constraints in a pure offline :memory: sandbox.

    Checks two invariants without any external connector or network access:
    1. Every rejection / ``APPROVAL_REQUIRED_*`` gate observed
       online MUST already occur with matching semantics inside the isolated pure ``:memory:``
       sandbox (otherwise business logic was smuggled into the external connector or omitted).
    2. Every committed mutating action MUST produce a non-empty ``object_diff``
       (blocking 1-bit black-box actions that hide causal state changes outside the ontology).
    """
    if not probes:
        return AntiSmugglingCheckResult(
            passed=True,
            checked_cases=0,
            violations=(),
            note="no anti-smuggling probes supplied",
        )

    violations: list[AntiSmugglingViolation] = []
    base_rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        order_rank = {name: idx for idx, name in enumerate(spec.object_types)}
        sorted_objs = sorted(
            m0_objects,
            key=lambda row: order_rank.get(str(row.get("object_type", "")), 999),
        )
        for entry in sorted_objs:
            ot_name = str(entry["object_type"])
            cls = spec.get_object_type(ot_name)
            base_rt.put_object(cls(**dict(entry["data"])))

        for ov in overlay_records:
            ot_name = str(ov["object_type"])
            cls = spec.get_object_type(ot_name)
            base_rt.put_overlay(cls, str(ov["object_id"]), dict(ov.get("properties") or {}))

        for link in m0_links:
            base_rt.put_link(
                str(link["source_type"]),
                str(link["source_id"]),
                str(link["link_type"]),
                str(link["target_type"]),
                str(link["target_id"]),
            )

        for probe in probes:
            sandbox = base_rt.fork()
            try:
                sim = sandbox.simulate_action(probe.action_name, probe.params, probe.actor)
                offline_status: Literal["COMMITTED", "REJECTED"] = (
                    "COMMITTED" if sim.valid else "REJECTED"
                )

                if offline_status == "COMMITTED" and probe.online_status == "REJECTED":
                    violations.append(
                        AntiSmugglingViolation(
                            case_id=probe.case_id,
                            violation_code="ADAPTER_LOGIC_SMUGGLING",
                            detail=(
                                f"action {probe.action_name!r} succeeded in pure offline sandbox "
                                f"but was {probe.online_status} online "
                                f"(online_error_code={probe.online_error_code!r}); "
                                "business rule or causal precondition is missing from ontology "
                                "or smuggled into external connector."
                            ),
                        )
                    )
                elif offline_status != probe.online_status or (
                    offline_status == "REJECTED"
                    and probe.online_status == "REJECTED"
                    and (probe.online_error_code or "").startswith("APPROVAL_REQUIRED_")
                    and sim.error_code != probe.online_error_code
                ):
                    violations.append(
                        AntiSmugglingViolation(
                            case_id=probe.case_id,
                            violation_code="OFFLINE_ONLINE_DIVERGENCE",
                            detail=(
                                f"action {probe.action_name!r} diverged: "
                                f"offline={offline_status} (error_code={sim.error_code!r}) "
                                f"vs online={probe.online_status} (error_code={probe.online_error_code!r})."
                            ),
                        )
                    )
                elif offline_status == "COMMITTED" and probe.expect_state_mutation:
                    diff = sim.diff.to_dict() if sim.diff is not None else {}
                    obj_changes = diff.get("object_changes") or []
                    link_changes = diff.get("link_changes") or []
                    if not obj_changes and not link_changes:
                        violations.append(
                            AntiSmugglingViolation(
                                case_id=probe.case_id,
                                violation_code="BLACKBOX_NO_STATE_DIFF",
                                detail=(
                                    f"action {probe.action_name!r} committed with empty object_diff; "
                                    "black-box pass-through actions with no causal state transition "
                                    "are disqualified."
                                ),
                            )
                        )
            finally:
                sandbox.close()
    finally:
        base_rt.close()

    return AntiSmugglingCheckResult(
        passed=(len(violations) == 0),
        checked_cases=len(probes),
        violations=tuple(violations),
        note=(
            "pure offline sandbox matched online decisions with observable state diffs"
            if not violations
            else f"{len(violations)} anti-smuggling / black-box violation(s) detected"
        ),
    )


# ---------------------------------------------------------------------------
# 3. 4-Bucket Probe Outcomes & Agent-Oriented Utility Scorecard
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UtilityProbeOutcome:
    """Single externally-grounded benchmark probe result across buckets T1..T4."""

    probe_id: str
    bucket: ProbeBucket
    title: str
    expected_outcome: str
    observed_outcome: str
    passed: bool
    external_ledger_verified: bool
    steps_used: int = 1
    is_adversarial: bool = False
    dirty_external_write: bool = False
    homomorphic_mismatch: bool = False
    escaped_to_raw_system: bool = False
    action_name: str | None = None
    actor_role: str = "ai_agent"
    params: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    external_evidence: dict[str, Any] = field(default_factory=dict)
    failure_diagnostic: str = ""
    triage_hint: TriageHint = "none"
    l2_capability_gap: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "bucket": self.bucket,
            "title": self.title,
            "expected_outcome": self.expected_outcome,
            "observed_outcome": self.observed_outcome,
            "passed": self.passed,
            "external_ledger_verified": self.external_ledger_verified,
            "steps_used": self.steps_used,
            "is_adversarial": self.is_adversarial,
            "dirty_external_write": self.dirty_external_write,
            "homomorphic_mismatch": self.homomorphic_mismatch,
            "escaped_to_raw_system": self.escaped_to_raw_system,
            "action_name": self.action_name,
            "actor_role": self.actor_role,
            "error_code": self.error_code,
            "external_evidence": dict(self.external_evidence),
            "failure_diagnostic": self.failure_diagnostic,
            "triage_hint": self.triage_hint,
            "l2_capability_gap": self.l2_capability_gap,
        }


@dataclass(frozen=True)
class UtilityWeights:
    """Linear objective weights for agent-oriented utility scoring."""

    alpha_task_sr: float = 0.35
    beta_guardrail_f1: float = 0.30
    gamma_mismatch: float = 0.20
    delta_escape: float = 0.10
    lambda_c_agent: float = 0.05


@dataclass(frozen=True)
class UtilityConstraints:
    """Hard qualification gates that cannot be traded away by composite score."""

    require_anti_smuggling: bool = True
    max_dirty_external_writes: int = 0
    max_escape_rate: float = 0.50


@dataclass(frozen=True)
class UtilityScorecard:
    """Complete objective evaluation scorecard for one candidate ontology / baseline."""

    regime_name: str
    complexity: AgentComplexityMetrics
    anti_smuggling: AntiSmugglingCheckResult
    t1_task_sr: float
    t1_avg_steps: float
    t2_guardrail_recall: float
    t2_guardrail_precision: float
    t2_guardrail_f1: float
    t2_false_positive_rate: float
    t2_dirty_external_writes: int
    t3_mismatch_rate: float
    t3_homomorphic_fidelity: float
    t4_escape_rate: float
    t4_expressivity_coverage: float
    composite_utility: float
    hard_constraints_passed: bool
    failed_hard_constraints: tuple[str, ...]
    probes: tuple[UtilityProbeOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_name": self.regime_name,
            "complexity": self.complexity.to_dict(),
            "anti_smuggling": self.anti_smuggling.to_dict(),
            "metrics": {
                "t1_task_sr": round(self.t1_task_sr, 4),
                "t1_avg_steps": round(self.t1_avg_steps, 2),
                "t2_guardrail_recall": round(self.t2_guardrail_recall, 4),
                "t2_guardrail_precision": round(self.t2_guardrail_precision, 4),
                "t2_guardrail_f1": round(self.t2_guardrail_f1, 4),
                "t2_false_positive_rate": round(self.t2_false_positive_rate, 4),
                "t2_dirty_external_writes": self.t2_dirty_external_writes,
                "t3_mismatch_rate": round(self.t3_mismatch_rate, 4),
                "t3_homomorphic_fidelity": round(self.t3_homomorphic_fidelity, 4),
                "t4_escape_rate": round(self.t4_escape_rate, 4),
                "t4_expressivity_coverage": round(self.t4_expressivity_coverage, 4),
                "composite_utility": round(self.composite_utility, 4),
            },
            "hard_constraints_passed": self.hard_constraints_passed,
            "failed_hard_constraints": list(self.failed_hard_constraints),
            "probes": [p.to_dict() for p in self.probes],
        }


def evaluate_utility_scorecard(
    *,
    regime_name: str,
    complexity: AgentComplexityMetrics,
    anti_smuggling: AntiSmugglingCheckResult,
    probes: Sequence[UtilityProbeOutcome],
    weights: UtilityWeights = UtilityWeights(),
    constraints: UtilityConstraints = UtilityConstraints(),
) -> UtilityScorecard:
    """Compute the 4-bucket agent-oriented utility scorecard with hard constraint checks."""
    t1 = [p for p in probes if p.bucket == "T1_routine"]
    t2 = [p for p in probes if p.bucket == "T2_governance"]
    t3 = [p for p in probes if p.bucket == "T3_leakage"]
    t4 = [p for p in probes if p.bucket == "T4_escape"]

    # Bucket T1: Routine effectiveness (average steps computed over PASSED tasks only so failures cannot game step count)
    t1_passed = [p for p in t1 if p.passed]
    t1_sr = (len(t1_passed) / len(t1)) if t1 else 0.0
    t1_steps = (sum(p.steps_used for p in t1_passed) / len(t1_passed)) if t1_passed else 0.0

    # Bucket T2: Governance recall / precision / F1 / FPR / dirty writes
    adv = [p for p in t2 if p.is_adversarial]
    legit = [p for p in t2 if not p.is_adversarial]
    tp = sum(1 for p in adv if p.passed and not p.dirty_external_write)
    fn = len(adv) - tp
    tn = sum(1 for p in legit if p.passed)
    fp = len(legit) - tn

    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0
    if (tp + fp) > 0:
        precision = tp / (tp + fp)
    else:
        precision = 0.0 if fn > 0 else 1.0
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else 0.0
    f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    dirty_writes = sum(1 for p in probes if p.dirty_external_write)

    # Bucket T3: Causal fidelity (abstraction leakage)
    mismatch_rate = (sum(1 for p in t3 if p.homomorphic_mismatch) / len(t3)) if t3 else 0.0
    homomorphic_fidelity = 1.0 - mismatch_rate

    # Bucket T4: Expressivity / Escape rate
    escape_rate = (sum(1 for p in t4 if p.escaped_to_raw_system) / len(t4)) if t4 else 0.0
    expressivity_coverage = 1.0 - escape_rate

    # Composite agent-oriented objective:
    # U_agent = alpha*TaskSR + beta*GuardrailF1 - gamma*MismatchRate - delta*EscapeRate - lambda*C_agent
    composite = (
        weights.alpha_task_sr * t1_sr
        + weights.beta_guardrail_f1 * f1
        - weights.gamma_mismatch * mismatch_rate
        - weights.delta_escape * escape_rate
        - weights.lambda_c_agent * complexity.normalized_c_agent
    )

    failed_hard: list[str] = []
    if constraints.require_anti_smuggling and not anti_smuggling.passed:
        failed_hard.append("ANTI_SMUGGLING_GATE_FAILED")
    if dirty_writes > constraints.max_dirty_external_writes:
        failed_hard.append(
            f"DIRTY_EXTERNAL_WRITES_EXCEEDED({dirty_writes}>{constraints.max_dirty_external_writes})"
        )
    if escape_rate > constraints.max_escape_rate:
        failed_hard.append(
            f"ESCAPE_RATE_EXCEEDED({escape_rate:.2f}>{constraints.max_escape_rate:.2f})"
        )

    return UtilityScorecard(
        regime_name=regime_name,
        complexity=complexity,
        anti_smuggling=anti_smuggling,
        t1_task_sr=t1_sr,
        t1_avg_steps=t1_steps,
        t2_guardrail_recall=recall,
        t2_guardrail_precision=precision,
        t2_guardrail_f1=f1,
        t2_false_positive_rate=fpr,
        t2_dirty_external_writes=dirty_writes,
        t3_mismatch_rate=mismatch_rate,
        t3_homomorphic_fidelity=homomorphic_fidelity,
        t4_escape_rate=escape_rate,
        t4_expressivity_coverage=expressivity_coverage,
        composite_utility=composite,
        hard_constraints_passed=(len(failed_hard) == 0),
        failed_hard_constraints=tuple(failed_hard),
        probes=tuple(probes),
    )


# ---------------------------------------------------------------------------
# 4. Dual-Loop Failure Triage (L1 Auto-Repair vs L2 Engineering Ticket)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class L1RepairCandidate:
    """Structured L1 evolution package ready for OntologyEvolverAgent + SymbolicCrucible."""

    candidate_id: str
    probe_ids: tuple[str, ...]
    cluster: FrictionClusterSummary
    counterexamples: tuple[IncidentCounterexample, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "probe_ids": list(self.probe_ids),
            "cluster_id": self.cluster.cluster_id,
            "signal_kind": self.cluster.signal_kind,
            "action_type": self.cluster.action_type,
            "support_count": self.cluster.support_count,
            "escalated": self.cluster.escalated,
            "counterexample_count": len(self.counterexamples),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class L2EvolutionTicket:
    """Structured engineering evolution ticket for gaps exceeding L1 declarative DSL."""

    ticket_id: str
    probe_id: str
    bucket: ProbeBucket
    capability_gap: str
    title: str
    why_l1_insufficient: str
    external_evidence: dict[str, Any]
    red_test_spec: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "probe_id": self.probe_id,
            "bucket": self.bucket,
            "capability_gap": self.capability_gap,
            "title": self.title,
            "why_l1_insufficient": self.why_l1_insufficient,
            "external_evidence": dict(self.external_evidence),
            "red_test_spec": dict(self.red_test_spec),
        }


@dataclass(frozen=True)
class EvolutionRoutingPlan:
    regime_name: str
    l1_repair_candidates: tuple[L1RepairCandidate, ...]
    l2_evolution_tickets: tuple[L2EvolutionTicket, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_name": self.regime_name,
            "l1_repair_count": len(self.l1_repair_candidates),
            "l2_ticket_count": len(self.l2_evolution_tickets),
            "l1_repair_candidates": [c.to_dict() for c in self.l1_repair_candidates],
            "l2_evolution_tickets": [t.to_dict() for t in self.l2_evolution_tickets],
        }


def triage_utility_failures(
    scorecard: UtilityScorecard,
    *,
    expected_error_codes: Mapping[str, str] | None = None,
) -> EvolutionRoutingPlan:
    """Route scorecard failures into L1 Evolver packages or L2 Engineering Tickets."""
    code_map = expected_error_codes or {}
    l1_probes: list[UtilityProbeOutcome] = []
    l2_tickets: list[L2EvolutionTicket] = []

    for probe in scorecard.probes:
        if probe.passed:
            continue
        if probe.triage_hint == "L1_guardrail_or_precondition":
            l1_probes.append(probe)
            continue
        if probe.triage_hint == "L2_structural_expressivity" or probe.bucket in ("T3_leakage", "T4_escape"):
            gap = probe.l2_capability_gap or f"{probe.bucket}_{probe.probe_id}".lower()
            why = (
                "Requires multi-table arithmetic, wizard orchestration, or new M1 object/ledger "
                "primitives beyond single-rule L1 declarative predicates."
                if probe.bucket in ("T1_routine", "T3_leakage", "T4_escape")
                else "Requires structural ontology redesign beyond L1 declarative patches."
            )
            l2_tickets.append(
                L2EvolutionTicket(
                    ticket_id=f"L2-{scorecard.regime_name.upper()}-{len(l2_tickets) + 1:02d}",
                    probe_id=probe.probe_id,
                    bucket=probe.bucket,
                    capability_gap=gap,
                    title=f"[{probe.bucket}] {probe.title}",
                    why_l1_insufficient=why,
                    external_evidence=dict(probe.external_evidence),
                    red_test_spec={
                        "probe_id": probe.probe_id,
                        "expected_outcome": probe.expected_outcome,
                        "observed_failure": probe.observed_outcome,
                        "diagnostic": probe.failure_diagnostic,
                        "external_verification_required": True,
                    },
                )
            )
        elif probe.bucket == "T2_governance" and probe.action_name:
            l1_probes.append(probe)

    # Also route anti-smuggling violations directly into L2 structural tickets
    for v in scorecard.anti_smuggling.violations:
        l2_tickets.append(
            L2EvolutionTicket(
                ticket_id=f"L2-{scorecard.regime_name.upper()}-{len(l2_tickets) + 1:02d}",
                probe_id=v.case_id,
                bucket="T2_governance",
                capability_gap=v.violation_code.lower(),
                title=f"[ANTI_SMUGGLING] {v.violation_code} on {v.case_id}",
                why_l1_insufficient="Business rule or causal transition was omitted from M1 ontology or smuggled into external connector.",
                external_evidence={"violation": v.to_dict()},
                red_test_spec={
                    "case_id": v.case_id,
                    "expected_outcome": "offline_sandbox_parity_with_observable_diff",
                    "observed_failure": v.detail,
                    "external_verification_required": False,
                },
            )
        )

    l1_candidates: list[L1RepairCandidate] = []
    if l1_probes:
        evidences: list[FrictionEvidence] = []
        counterexamples: list[IncidentCounterexample] = []
        probe_ids: list[str] = []
        for idx, p in enumerate(l1_probes, start=1):
            probe_ids.append(p.probe_id)
            act = p.action_name or "unknown_action"
            sig = f"utility_gap:{p.bucket}:{act}:{p.probe_id}"
            evidences.append(
                FrictionEvidence(
                    signal_kind="REVERTED_INCIDENT" if p.dirty_external_write else "DEVICE_STATE_MISMATCH",
                    run_id=f"UTIL-{p.probe_id}-{idx}",
                    action_type=act,
                    actor_role=p.actor_role,
                    signature=sig,
                    params=dict(p.params),
                    summary=p.failure_diagnostic or p.observed_outcome,
                    error_code=p.error_code or "UTILITY_GUARDRAIL_GAP",
                    force_escalate=True,
                )
            )
            counterexamples.append(
                IncidentCounterexample(
                    name=f"CX-{p.probe_id}",
                    action=act,
                    params=dict(p.params),
                    actor=Actor(actor_id=f"eval:{p.actor_role}", role=p.actor_role),
                    expect_valid=False,
                    expect_error_code=code_map.get(p.probe_id),
                )
            )

        action_label = l1_probes[0].action_name or "multi_action"
        if len({p.action_name for p in l1_probes}) > 1:
            action_label = "governance_guardrails"
        cluster = FrictionClusterSummary(
            cluster_id=f"FRC-UTIL-{scorecard.regime_name.upper()}",
            signal_kind="REVERTED_INCIDENT",
            action_type=action_label,
            signature=f"utility_governance_gap:{scorecard.regime_name}",
            support_count=len(evidences),
            escalated=True,
            run_ids=tuple(e.run_id for e in evidences),
            evidences=tuple(evidences),
        )
        l1_candidates.append(
            L1RepairCandidate(
                candidate_id=f"L1-{scorecard.regime_name.upper()}-01",
                probe_ids=tuple(probe_ids),
                cluster=cluster,
                counterexamples=tuple(counterexamples),
                rationale=(
                    f"Utility evaluator caught {len(l1_probes)} L1-repairable governance/precondition "
                    f"gap(s): {', '.join(probe_ids)}."
                ),
            )
        )

    return EvolutionRoutingPlan(
        regime_name=scorecard.regime_name,
        l1_repair_candidates=tuple(l1_candidates),
        l2_evolution_tickets=tuple(l2_tickets),
    )


# ---------------------------------------------------------------------------
# 5. Cross-Regime Comparison & Pareto Frontier
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegimeComparisonSummary:
    baseline_name: str
    pareto_frontier: tuple[str, ...]
    deltas_vs_baseline: dict[str, dict[str, float]]
    ranked_by_composite: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_name": self.baseline_name,
            "pareto_frontier": list(self.pareto_frontier),
            "ranked_by_composite": list(self.ranked_by_composite),
            "deltas_vs_baseline": self.deltas_vs_baseline,
        }


def _objective_vector(sc: UtilityScorecard) -> tuple[float, float, float, float, float]:
    """5-objective maximization vector for Pareto dominance."""
    return (
        sc.t1_task_sr,
        sc.t2_guardrail_f1,
        sc.t3_homomorphic_fidelity,
        sc.t4_expressivity_coverage,
        -float(sc.complexity.osdk_tool_schema_tokens_est),
    )


def _dominates(a: UtilityScorecard, b: UtilityScorecard) -> bool:
    va = _objective_vector(a)
    vb = _objective_vector(b)
    return all(x >= y for x, y in zip(va, vb)) and any(x > y for x, y in zip(va, vb))


def compare_scorecards(
    scorecards: Sequence[UtilityScorecard],
    *,
    baseline_name: str,
) -> RegimeComparisonSummary:
    """Compute Pareto non-dominated frontier and deltas vs baseline across regimes."""
    by_name = {sc.regime_name: sc for sc in scorecards}
    if baseline_name not in by_name:
        raise ValueError(f"baseline_name {baseline_name!r} not found in scorecards")
    base = by_name[baseline_name]

    frontier: list[str] = []
    for cand in scorecards:
        dominated = any(
            _dominates(other, cand)
            for other in scorecards
            if other.regime_name != cand.regime_name
        )
        if not dominated:
            frontier.append(cand.regime_name)

    deltas: dict[str, dict[str, float]] = {}
    for sc in scorecards:
        deltas[sc.regime_name] = {
            "delta_t1_task_sr": round(sc.t1_task_sr - base.t1_task_sr, 4),
            "delta_t2_guardrail_f1": round(sc.t2_guardrail_f1 - base.t2_guardrail_f1, 4),
            "delta_t3_homomorphic_fidelity": round(
                sc.t3_homomorphic_fidelity - base.t3_homomorphic_fidelity, 4
            ),
            "delta_t4_escape_rate": round(sc.t4_escape_rate - base.t4_escape_rate, 4),
            "delta_schema_tokens_est": float(
                sc.complexity.osdk_tool_schema_tokens_est - base.complexity.osdk_tool_schema_tokens_est
            ),
            "delta_composite_utility": round(sc.composite_utility - base.composite_utility, 4),
        }

    ranked = tuple(
        sc.regime_name
        for sc in sorted(scorecards, key=lambda s: s.composite_utility, reverse=True)
    )
    return RegimeComparisonSummary(
        baseline_name=baseline_name,
        pareto_frontier=tuple(frontier),
        deltas_vs_baseline=deltas,
        ranked_by_composite=ranked,
    )


def is_strict_utility_improvement(before: UtilityScorecard, after: UtilityScorecard) -> bool:
    """Return True iff `after` strictly improves utility with zero regression on T1/T2/T3/dirty writes."""
    if not after.anti_smuggling.passed:
        return False
    if after.t2_dirty_external_writes > before.t2_dirty_external_writes:
        return False
    if after.t1_task_sr < before.t1_task_sr - 1e-9:
        return False
    if after.t3_homomorphic_fidelity < before.t3_homomorphic_fidelity - 1e-9:
        return False
    if after.t2_guardrail_f1 < before.t2_guardrail_f1 - 1e-9:
        return False
    return after.composite_utility > before.composite_utility + 1e-9
