"""覆盖率驱动的波普尔差分证伪器（DifferentialFalsifier · 纯 M2，领域无关）。

通过「结构覆盖岗位测试 + 红队对抗探索 + 双世界同态差分（内存 M1 沙盘预测 vs 物理外设独立回读）」
取代“重复固定样本 N 次未报错”的伪安全标准：

1. **``ExternalSandboxAdapter`` 协议**：纯 M2 外设沙盒抽象，彻底解耦底层传输引擎；
2. **双轨自动用例生成**：
   - 轨道 1（``coverage``）：沿锁定标准蓝图状态机与动作签名，执行多步波前探索以追求高动作/跃迁覆盖率；
   - 轨道 2（``adversarial``）：自动构造越权角色调用、终态/非法态跃迁、以及前置护栏触发用例（含自定义对抗探针），
     验证对抗拒止时外部沙盒 0 非法物理脏写；
3. **波普尔裁决（``CORROBORATED`` / ``FALSIFIED``）**：任一决策分叉、同态字段差分、对抗逃逸、外部脏写
   或覆盖率不足（``INSUFFICIENT_COVERAGE``）均硬判 ``FALSIFIED`` 并输出结构化反例梯度；
4. **``decompile_and_falsify_driver``**：串联 ``DriverDecompilerAgent`` 与 ``DifferentialFalsifier``，
   实现从反编译到物理沙盒实证失败的自动自愈闭环。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from ..runtime import OntologyRuntime, SimulationResult
from ..sandbox import ObjectDiff
from ..settlement import SourceBridge
from ..spec import (
    Actor,
    GovernanceViolationError,
    InvariantError,
    ObjectType,
    OntologySpec,
    PermissionDeniedError,
)
from .crucible import CrucibleContext, SymbolicCrucible
from .decompiler import DecompilationCycleResult, DriverDecompilerAgent
from .dsl import (
    ActionBlueprint,
    DeclarativeBlueprint,
    ObjectTypeBlueprint,
    compile_blueprint,
)
from .governance import BUILDER_ACTOR, InductionGovernanceService
from .phenomenon import AdapterMappingSpec, ExternalPhenomenonProfile
from .seed import SeedOntology

__all__ = [
    "VerdictKind",
    "ExternalSandboxAdapter",
    "AdversarialProbe",
    "FalsificationCase",
    "FalsificationCounterexample",
    "DifferentialFalsificationReport",
    "DecompileAndFalsifyResult",
    "DifferentialFalsifier",
    "decompile_and_falsify_driver",
]

VerdictKind = Literal["CORROBORATED", "FALSIFIED"]


@runtime_checkable
class ExternalSandboxAdapter(Protocol):
    """Domain-agnostic external physical sandbox contract for differential falsification."""

    def reset(self) -> None:
        """Reset external sandbox state to a clean baseline snapshot."""
        ...

    def snapshot_m0(self) -> Sequence[dict[str, Any]]:
        """Read current external records projected into canonical M0 dicts."""
        ...

    def make_source_bridge(
        self,
        spec: OntologySpec,
        adapter_mapping: AdapterMappingSpec,
    ) -> SourceBridge:
        """Construct a coupled SourceBridge for dual-world actuation and readback."""
        ...

    def external_write_count(self) -> int:
        """Return cumulative committed mutation count observed on external sandbox."""
        ...


@dataclass(frozen=True)
class AdversarialProbe:
    """Explicit red-team probe injected alongside automatically generated adversarial cases."""

    name: str
    action: str
    params: dict[str, Any]
    actor: Actor
    expect_valid: bool = False
    expect_error_code: str | None = None
    setup_objects: tuple[dict[str, Any], ...] = ()
    description: str = ""


@dataclass(frozen=True)
class FalsificationCase:
    """One concrete step or probe evaluated by DifferentialFalsifier."""

    name: str
    track: Literal["coverage", "adversarial"]
    action: str
    params: dict[str, Any]
    actor: Actor
    expected_valid: bool
    expected_error_code: str | None = None
    setup_objects: tuple[dict[str, Any], ...] = ()
    covered_transition: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class FalsificationCounterexample:
    kind: str
    case_name: str
    action: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DifferentialFalsificationReport:
    verdict: VerdictKind
    action_coverage_ratio: float
    transition_coverage_ratio: float
    covered_actions: tuple[str, ...]
    target_actions: tuple[str, ...]
    covered_transitions: tuple[str, ...]
    reachable_transitions: tuple[str, ...]
    total_cases_run: int
    coverage_cases_run: int
    adversarial_cases_run: int
    adversarial_escapes: int
    differential_mismatches: int
    dirty_external_writes: int
    counterexamples: tuple[FalsificationCounterexample, ...] = ()

    def diagnostic_feedback(self) -> str:
        lines = [
            f"DifferentialFalsifier verdict: {self.verdict} "
            f"(action_cov={self.action_coverage_ratio:.2%}, "
            f"transition_cov={self.transition_coverage_ratio:.2%}, "
            f"escapes={self.adversarial_escapes}, mismatches={self.differential_mismatches}, "
            f"dirty_writes={self.dirty_external_writes})"
        ]
        for ce in self.counterexamples:
            lines.append(f"- [{ce.kind}] ({ce.case_name} / {ce.action}): {ce.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "action_coverage_ratio": round(self.action_coverage_ratio, 4),
            "transition_coverage_ratio": round(self.transition_coverage_ratio, 4),
            "covered_actions": list(self.covered_actions),
            "target_actions": list(self.target_actions),
            "covered_transitions": list(self.covered_transitions),
            "reachable_transitions": list(self.reachable_transitions),
            "total_cases_run": self.total_cases_run,
            "coverage_cases_run": self.coverage_cases_run,
            "adversarial_cases_run": self.adversarial_cases_run,
            "adversarial_escapes": self.adversarial_escapes,
            "differential_mismatches": self.differential_mismatches,
            "dirty_external_writes": self.dirty_external_writes,
            "counterexamples": [
                {
                    "kind": c.kind,
                    "case_name": c.case_name,
                    "action": c.action,
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.counterexamples
            ],
        }


@dataclass(frozen=True)
class DecompileAndFalsifyResult:
    verified: bool
    corroborated: bool
    healing_rounds_used: int
    decompilation: DecompilationCycleResult
    falsification: DifferentialFalsificationReport | None


def _snapshot_digest(m0_objects: Sequence[dict[str, Any]]) -> str:
    canonical = sorted(
        (
            {
                "object_type": str(e.get("object_type") or ""),
                "object_id": str(e.get("object_id") or ""),
                "data": e.get("data") or {},
            }
            for e in m0_objects
        ),
        key=lambda item: (item["object_type"], item["object_id"]),
    )
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _resolve_target_actions(
    blueprint: DeclarativeBlueprint,
    target_actions: Sequence[str] | None,
) -> list[ActionBlueprint]:
    if target_actions:
        wanted = set(target_actions)
        return [a for a in blueprint.actions if a.name in wanted]
    source_actions = [a for a in blueprint.actions if a.affects_source]
    return source_actions if source_actions else list(blueprint.actions)


def _infer_reachable_transitions(
    blueprint: DeclarativeBlueprint,
    target_acts: Sequence[ActionBlueprint],
    initial_m0: Sequence[dict[str, Any]],
) -> set[tuple[str, str, str]]:
    """Infer reachable FSM edges produced by ``target_acts`` from active M0/initial states."""
    ot_by_name = {ot.name: ot for ot in blueprint.object_types}
    observed_states_by_type: dict[str, set[str]] = {}
    for entry in initial_m0:
        tname = str(entry.get("object_type") or "")
        ot = ot_by_name.get(tname)
        if ot is None or not ot.transitions:
            continue
        st = (entry.get("data") or {}).get(ot.status_field)
        if st is not None:
            observed_states_by_type.setdefault(tname, set()).add(str(st))

    # Also include any non-terminal states produced by earlier actions in a chain
    produced_states_by_type: dict[str, set[str]] = {
        k: set(v) for k, v in observed_states_by_type.items()
    }
    changed = True
    while changed:
        changed = False
        for act in target_acts:
            for mut in act.mutations:
                ot = ot_by_name.get(mut.object_type)
                if ot is None or not ot.transitions:
                    continue
                sf_expr = mut.set_fields.get(ot.status_field)
                if isinstance(sf_expr, dict) and isinstance(sf_expr.get("lit"), str):
                    target_s = sf_expr["lit"]
                    if target_s not in produced_states_by_type.setdefault(ot.name, set()):
                        produced_states_by_type[ot.name].add(target_s)
                        changed = True

    edges: set[tuple[str, str, str]] = set()
    for act in target_acts:
        for mut in act.mutations:
            ot = ot_by_name.get(mut.object_type)
            if ot is None or not ot.transitions:
                continue
            sf_expr = mut.set_fields.get(ot.status_field)
            if not (isinstance(sf_expr, dict) and isinstance(sf_expr.get("lit"), str)):
                continue
            to_state = sf_expr["lit"]
            # Check explicit precondition allowed states if present
            allowed_from: set[str] | None = None
            for pre in act.preconditions:
                if pre.target_type != ot.name or not isinstance(pre.must_hold, dict):
                    continue
                mh = pre.must_hold
                op = mh.get("op")
                left = mh.get("left")
                right = mh.get("right")
                if isinstance(left, dict) and left.get("ref") == f"obj.{ot.status_field}" and isinstance(right, dict):
                    lit_v = right.get("lit")
                    if op == "eq" and isinstance(lit_v, str):
                        allowed_from = {lit_v}
                    elif op == "in" and isinstance(lit_v, list):
                        allowed_from = {str(x) for x in lit_v}
            cand_from = produced_states_by_type.get(ot.name, set(ot.transitions.keys()))
            for from_state in cand_from:
                if allowed_from is not None and from_state not in allowed_from:
                    continue
                if to_state in (ot.transitions.get(from_state) or ()):
                    edges.add((ot.name, from_state, to_state))
    return edges


def _sync_rt_with_m0(
    rt: OntologyRuntime,
    spec: OntologySpec,
    m0_objects: Sequence[dict[str, Any]],
) -> None:
    """Synchronize in-memory sandbox with newly materialized objects from external snapshot."""
    order = {name: idx for idx, name in enumerate(spec.object_types)}
    ordered = sorted(m0_objects, key=lambda e: order.get(str(e.get("object_type") or ""), 9999))
    for entry in ordered:
        tname = str(entry.get("object_type") or "")
        cls = spec.get_object_type(tname)
        data = dict(entry.get("data") or {})
        obj = cls(**data)
        oid = obj.primary_key_value()
        try:
            existing = rt.load_object(tname, oid)
        except Exception:
            existing = None
        if existing is None:
            rt.put_object(obj)


def _index_m0_by_type(
    m0_objects: Sequence[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_type: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in m0_objects:
        tname = str(entry.get("object_type") or "")
        oid = str(entry.get("object_id") or "")
        data = dict(entry.get("data") or {})
        if not oid and data:
            continue
        by_type.setdefault(tname, {})[oid] = data
    return by_type


def _primary_subject_for_action(
    act: ActionBlueprint,
    ot_by_name: dict[str, ObjectTypeBlueprint],
) -> tuple[str | None, str]:
    param_names = {p.name for p in act.params}
    for mut in act.mutations:
        if mut.op != "create" and mut.target_ref is None and mut.id_param in param_names:
            return mut.object_type, mut.id_param
    for pre in act.preconditions:
        ot = ot_by_name.get(pre.target_type)
        idp = pre.id_param or (ot.primary_key if ot else "id")
        if idp in param_names:
            return pre.target_type, idp
    return None, (act.params[0].name if act.params else "id")


def _candidate_actors_for_action(act: ActionBlueprint) -> list[Actor]:
    preferred = ("steward", "admin", "finance_director", "sales_manager", "ops")
    ordered_roles: list[str] = [r for r in preferred if r in act.allowed_roles]
    for r in act.allowed_roles:
        if r not in ordered_roles:
            ordered_roles.append(r)
    if not ordered_roles:
        ordered_roles = ["admin"]
    return [Actor(actor_id=f"falsifier:{role}", role=role) for role in ordered_roles]


def _candidate_param_dicts(
    act: ActionBlueprint,
    obj_id: str,
    id_param: str,
    obj_data: dict[str, Any],
) -> list[dict[str, Any]]:
    extra_choices: list[dict[str, Any]] = [{}]
    for p in act.params:
        if p.name == id_param:
            continue
        if not p.required:
            continue
        if p.field_type in ("float", "int"):
            nums: list[Any] = []
            for v in obj_data.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    val = int(v) if p.field_type == "int" else float(v)
                    if val not in nums:
                        nums.append(val)
            if not nums:
                nums = [1 if p.field_type == "int" else 1.0]
            expanded: list[dict[str, Any]] = []
            for base in extra_choices:
                for num in nums[:3]:
                    nxt = dict(base)
                    nxt[p.name] = num
                    expanded.append(nxt)
            extra_choices = expanded
        elif p.field_type == "bool":
            extra_choices = [{**base, p.name: True} for base in extra_choices]
        else:
            extra_choices = [{**base, p.name: p.default or "falsifier_val"} for base in extra_choices]
    return [{id_param: obj_id, **extra} for extra in extra_choices]


class DifferentialFalsifier:
    """Coverage-guided + red-team dual-world differential falsifier (pure M2)."""

    def __init__(
        self,
        *,
        min_action_coverage: float = 1.0,
        min_transition_coverage: float = 0.8,
        adversarial_probes: Sequence[AdversarialProbe] = (),
        max_wave_steps: int = 24,
        max_waves: int = 3,
    ) -> None:
        self.min_action_coverage = float(min_action_coverage)
        self.min_transition_coverage = float(min_transition_coverage)
        self.adversarial_probes = tuple(adversarial_probes)
        self.max_wave_steps = max(4, int(max_wave_steps))
        self.max_waves = max(1, int(max_waves))

    def evaluate(
        self,
        *,
        blueprint: DeclarativeBlueprint,
        adapter_mapping: AdapterMappingSpec,
        sandbox: ExternalSandboxAdapter,
        seed_ontology: SeedOntology | None = None,
        target_actions: Sequence[str] | None = None,
        custom_cases: Sequence[FalsificationCase] | None = None,
        auto_generate: bool = True,
    ) -> DifferentialFalsificationReport:
        spec = compile_blueprint(blueprint, seed_ontology=seed_ontology)
        ot_by_name = {ot.name: ot for ot in blueprint.object_types}
        target_act_defs = _resolve_target_actions(blueprint, target_actions)
        target_act_names = [a.name for a in target_act_defs]
        target_act_map = {a.name: a for a in target_act_defs}

        set_map = getattr(sandbox, "set_adapter_mapping", None)
        if callable(set_map):
            set_map(adapter_mapping)
        sandbox.reset()
        initial_m0 = list(sandbox.snapshot_m0())
        reachable_edges = _infer_reachable_transitions(blueprint, target_act_defs, initial_m0)

        covered_actions: set[str] = set()
        covered_edges: set[tuple[str, str, str]] = set()
        counterexamples: list[FalsificationCounterexample] = []
        escapes = 0
        mismatches = 0
        dirty_writes = 0
        cov_count = 0
        adv_count = 0

        if custom_cases is not None and not auto_generate:
            c_cases = [c for c in custom_cases if c.track == "coverage"]
            a_cases = [c for c in custom_cases if c.track == "adversarial"]
            cov_res = self._run_case_batch(
                cases=c_cases,
                spec=spec,
                blueprint=blueprint,
                ot_by_name=ot_by_name,
                target_act_map=target_act_map,
                adapter_mapping=adapter_mapping,
                sandbox=sandbox,
                covered_actions=covered_actions,
                covered_edges=covered_edges,
                counterexamples=counterexamples,
            )
            adv_res = self._run_case_batch(
                cases=a_cases,
                spec=spec,
                blueprint=blueprint,
                ot_by_name=ot_by_name,
                target_act_map=target_act_map,
                adapter_mapping=adapter_mapping,
                sandbox=sandbox,
                covered_actions=covered_actions,
                covered_edges=covered_edges,
                counterexamples=counterexamples,
            )
            cov_count = len(c_cases)
            adv_count = len(a_cases)
            escapes += cov_res["escapes"] + adv_res["escapes"]
            mismatches += cov_res["mismatches"] + adv_res["mismatches"]
            dirty_writes += cov_res["dirty_writes"] + adv_res["dirty_writes"]
        else:
            # --- Track 1: Stateful Coverage-Guided Dual-World Wave Exploration ---
            cov_stats = self._run_track1_coverage_waves(
                spec=spec,
                blueprint=blueprint,
                ot_by_name=ot_by_name,
                target_act_defs=target_act_defs,
                adapter_mapping=adapter_mapping,
                sandbox=sandbox,
                covered_actions=covered_actions,
                covered_edges=covered_edges,
                counterexamples=counterexamples,
            )
            cov_count += cov_stats["cases_run"]
            mismatches += cov_stats["mismatches"]
            dirty_writes += cov_stats["dirty_writes"]

            # Also run any explicit coverage custom cases
            if custom_cases:
                extra_cov = [c for c in custom_cases if c.track == "coverage"]
                if extra_cov:
                    res_ec = self._run_case_batch(
                        cases=extra_cov,
                        spec=spec,
                        blueprint=blueprint,
                        ot_by_name=ot_by_name,
                        target_act_map=target_act_map,
                        adapter_mapping=adapter_mapping,
                        sandbox=sandbox,
                        covered_actions=covered_actions,
                        covered_edges=covered_edges,
                        counterexamples=counterexamples,
                    )
                    cov_count += len(extra_cov)
                    escapes += res_ec["escapes"]
                    mismatches += res_ec["mismatches"]
                    dirty_writes += res_ec["dirty_writes"]

            # --- Track 2: Adversarial Red-Team Exploration + User Probes ---
            adv_cases = self._build_adversarial_cases(
                spec=spec,
                blueprint=blueprint,
                ot_by_name=ot_by_name,
                target_act_defs=target_act_defs,
                initial_m0=initial_m0,
                custom_adversarial=[
                    *(c for c in (custom_cases or ()) if c.track == "adversarial"),
                ],
            )
            res_adv = self._run_case_batch(
                cases=adv_cases,
                spec=spec,
                blueprint=blueprint,
                ot_by_name=ot_by_name,
                target_act_map=target_act_map,
                adapter_mapping=adapter_mapping,
                sandbox=sandbox,
                covered_actions=covered_actions,
                covered_edges=covered_edges,
                counterexamples=counterexamples,
            )
            adv_count = len(adv_cases)
            escapes += res_adv["escapes"]
            mismatches += res_adv["mismatches"]
            dirty_writes += res_adv["dirty_writes"]

        action_cov = (len(covered_actions) / len(target_act_names)) if target_act_names else 1.0
        trans_cov = (len(covered_edges) / len(reachable_edges)) if reachable_edges else 1.0

        if action_cov < self.min_action_coverage:
            uncovered = sorted(set(target_act_names) - covered_actions)
            counterexamples.append(
                FalsificationCounterexample(
                    kind="INSUFFICIENT_COVERAGE",
                    case_name="track_1_action_coverage",
                    action=",".join(uncovered) if uncovered else "*",
                    message=(
                        f"INSUFFICIENT_COVERAGE: action_coverage_ratio={action_cov:.2%} "
                        f"< min_action_coverage={self.min_action_coverage:.2%}; uncovered={uncovered}"
                    ),
                    details={"uncovered_actions": uncovered, "ratio": action_cov},
                )
            )
        if trans_cov < self.min_transition_coverage:
            uncovered_e = sorted(f"{t}:{f}->{to}" for (t, f, to) in (reachable_edges - covered_edges))
            counterexamples.append(
                FalsificationCounterexample(
                    kind="INSUFFICIENT_COVERAGE",
                    case_name="track_1_transition_coverage",
                    action="*",
                    message=(
                        f"INSUFFICIENT_COVERAGE: transition_coverage_ratio={trans_cov:.2%} "
                        f"< min_transition_coverage={self.min_transition_coverage:.2%}; "
                        f"uncovered={uncovered_e}"
                    ),
                    details={"uncovered_transitions": uncovered_e, "ratio": trans_cov},
                )
            )

        verdict: VerdictKind = "CORROBORATED" if not counterexamples else "FALSIFIED"
        return DifferentialFalsificationReport(
            verdict=verdict,
            action_coverage_ratio=action_cov,
            transition_coverage_ratio=trans_cov,
            covered_actions=tuple(sorted(covered_actions)),
            target_actions=tuple(target_act_names),
            covered_transitions=tuple(sorted(f"{t}:{f}->{to}" for (t, f, to) in covered_edges)),
            reachable_transitions=tuple(sorted(f"{t}:{f}->{to}" for (t, f, to) in reachable_edges)),
            total_cases_run=cov_count + adv_count,
            coverage_cases_run=cov_count,
            adversarial_cases_run=adv_count,
            adversarial_escapes=escapes,
            differential_mismatches=mismatches,
            dirty_external_writes=dirty_writes,
            counterexamples=tuple(counterexamples),
        )

    def _run_track1_coverage_waves(
        self,
        *,
        spec: OntologySpec,
        blueprint: DeclarativeBlueprint,
        ot_by_name: dict[str, ObjectTypeBlueprint],
        target_act_defs: Sequence[ActionBlueprint],
        adapter_mapping: AdapterMappingSpec,
        sandbox: ExternalSandboxAdapter,
        covered_actions: set[str],
        covered_edges: set[tuple[str, str, str]],
        counterexamples: list[FalsificationCounterexample],
    ) -> dict[str, int]:
        """Execute up to two stateful waves (primary workflow chain + alternate branch) in dual worlds."""
        mismatches = 0
        dirty_writes = 0
        cases_run = 0
        tried_pairs: set[tuple[str, str, str]] = set()
        reachable_edges = _infer_reachable_transitions(blueprint, target_act_defs, list(sandbox.snapshot_m0()))

        for wave_idx in range(self.max_waves):
            if len(covered_actions) == len(target_act_defs) and covered_edges >= reachable_edges:
                break
            wave_failed_sigs: set[tuple[str, str, str]] = set()
            wave_executed_sigs: set[tuple[str, str, str]] = set()
            sandbox.reset()
            m0_now = list(sandbox.snapshot_m0())
            bridge = sandbox.make_source_bridge(spec, adapter_mapping)
            mem_rt = OntologyRuntime(spec, db_path=":memory:")
            coupled_rt = OntologyRuntime(spec, db_path=":memory:", world_mode="coupled", source_bridge=bridge)
            try:
                _sync_rt_with_m0(mem_rt, spec, m0_now)
                _sync_rt_with_m0(coupled_rt, spec, m0_now)

                for step_idx in range(self.max_wave_steps):
                    if len(covered_actions) == len(target_act_defs) and covered_edges >= reachable_edges:
                        break
                    m0_by_type = _index_m0_by_type(m0_now)
                    chosen: (
                        tuple[
                            ActionBlueprint,
                            dict[str, Any],
                            Actor,
                            SimulationResult,
                            list[tuple[str, str, str]],
                            tuple[str, str, str],
                        ]
                        | None
                    ) = None

                    # Prioritize: (1) uncovered action, (2) uncovered edge, (3) state-advancing stepping stone
                    for pass_kind in ("uncovered_action", "uncovered_edge", "stepping_stone"):
                        if chosen is not None:
                            break
                        for act in target_act_defs:
                            if pass_kind == "uncovered_action" and act.name in covered_actions:
                                continue
                            subj_type, id_param = _primary_subject_for_action(act, ot_by_name)
                            if subj_type is None:
                                continue
                            actors = _candidate_actors_for_action(act)
                            subj_rows = m0_by_type.get(subj_type, {})
                            for oid, odata in sorted(subj_rows.items()):
                                sig = (act.name, oid, str(odata.get(ot_by_name[subj_type].status_field, "")))
                                if sig in wave_failed_sigs or sig in wave_executed_sigs:
                                    continue
                                if wave_idx >= 1 and pass_kind != "stepping_stone" and sig in tried_pairs:
                                    continue
                                for params in _candidate_param_dicts(act, oid, id_param, odata):
                                    for actor in actors:
                                        try:
                                            sim = mem_rt.simulate_action(act.name, params, actor)
                                        except Exception:
                                            continue
                                        if not sim.valid:
                                            continue
                                        edges = self._extract_transition_edges(sim, ot_by_name)
                                        gains_cov = (
                                            act.name not in covered_actions
                                            or any(e not in covered_edges for e in edges)
                                        )
                                        if pass_kind in ("uncovered_action", "uncovered_edge") and not gains_cov:
                                            continue
                                        if pass_kind == "stepping_stone" and not edges:
                                            continue
                                        chosen = (act, params, actor, sim, edges, sig)
                                        tried_pairs.add(sig)
                                        break
                                    if chosen is not None:
                                        break
                                if chosen is not None:
                                    break
                            if chosen is not None:
                                break

                    if chosen is None:
                        break

                    act, params, actor, mem_sim, edges, chosen_sig = chosen
                    wave_executed_sigs.add(chosen_sig)
                    cases_run += 1
                    run_id = f"FALS-W{wave_idx}-S{step_idx}-{act.name}"
                    try:
                        run_res = coupled_rt.submit_action(run_id, act.name, params, actor)
                    except (InvariantError, GovernanceViolationError, PermissionDeniedError) as exc:
                        mismatches += 1
                        wave_failed_sigs.add(chosen_sig)
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DIFFERENTIAL_STATE_MISMATCH",
                                case_name=run_id,
                                action=act.name,
                                message=(
                                    f"M1 memory simulation succeeded, but coupled sandbox rejected with "
                                    f"{getattr(exc, 'code', None) or type(exc).__name__}: {exc}"
                                ),
                                details={"params": params, "actor_role": actor.role},
                            )
                        )
                        continue
                    except Exception as exc:
                        mismatches += 1
                        wave_failed_sigs.add(chosen_sig)
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DIFFERENTIAL_STATE_MISMATCH",
                                case_name=run_id,
                                action=act.name,
                                message=f"Coupled execution failed unexpectedly: {type(exc).__name__}: {exc}",
                                details={"params": params, "actor_role": actor.role},
                            )
                        )
                        continue

                    if run_res.get("status") != "COMMITTED":
                        mismatches += 1
                        wave_failed_sigs.add(chosen_sig)
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DECISION_DIVERGENCE",
                                case_name=run_id,
                                action=act.name,
                                message=f"Expected COMMITTED, got {run_res.get('status')!r}",
                            )
                        )
                        continue

                    # Advance mem_rt and compare against independent snapshot_m0()
                    mem_rt.submit_action(f"MEM-{run_id}", act.name, params, actor)
                    m0_after = list(sandbox.snapshot_m0())
                    diff_errs = self._compare_diff_against_m0(mem_sim, m0_after, spec)
                    if diff_errs:
                        mismatches += 1
                        wave_failed_sigs.add(chosen_sig)
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DIFFERENTIAL_STATE_MISMATCH",
                                case_name=run_id,
                                action=act.name,
                                message="; ".join(diff_errs),
                                details={"params": params},
                            )
                        )
                    else:
                        covered_actions.add(act.name)
                        for edge in edges:
                            covered_edges.add(edge)

                    # Sync any downstream objects materialized in external sandbox into both runtimes
                    _sync_rt_with_m0(mem_rt, spec, m0_after)
                    _sync_rt_with_m0(coupled_rt, spec, m0_after)
                    m0_now = m0_after
            finally:
                mem_rt.close()
                coupled_rt.close()

        return {"cases_run": cases_run, "mismatches": mismatches, "dirty_writes": dirty_writes}

    def _build_adversarial_cases(
        self,
        *,
        spec: OntologySpec,
        blueprint: DeclarativeBlueprint,
        ot_by_name: dict[str, ObjectTypeBlueprint],
        target_act_defs: Sequence[ActionBlueprint],
        initial_m0: Sequence[dict[str, Any]],
        custom_adversarial: Sequence[FalsificationCase],
    ) -> list[FalsificationCase]:
        cases: list[FalsificationCase] = list(custom_adversarial)
        m0_by_type = _index_m0_by_type(initial_m0)
        all_roles = {r for a in blueprint.actions for r in a.allowed_roles} | {"ai_agent", "guest_intruder"}

        eval_rt = OntologyRuntime(spec, db_path=":memory:")
        try:
            _sync_rt_with_m0(eval_rt, spec, initial_m0)

            # Track 2.1: Explicit user-supplied AdversarialProbe instances
            for probe in self.adversarial_probes:
                cases.append(
                    FalsificationCase(
                        name=probe.name,
                        track="adversarial",
                        action=probe.action,
                        params=dict(probe.params),
                        actor=probe.actor,
                        expected_valid=probe.expect_valid,
                        expected_error_code=probe.expect_error_code,
                        setup_objects=probe.setup_objects,
                    )
                )

            # Track 2.2: Unauthorized role RBAC probes
            for act in target_act_defs:
                subj_type, id_param = _primary_subject_for_action(act, ot_by_name)
                if subj_type is None or not m0_by_type.get(subj_type):
                    continue
                oid, odata = next(iter(sorted(m0_by_type[subj_type].items())))
                params = _candidate_param_dicts(act, oid, id_param, odata)[0]
                bad_roles = sorted(all_roles - set(act.allowed_roles))
                bad_role = bad_roles[0] if bad_roles else "guest_intruder"
                cases.append(
                    FalsificationCase(
                        name=f"ADV-RBAC-{act.name}-{bad_role}",
                        track="adversarial",
                        action=act.name,
                        params=params,
                        actor=Actor(actor_id=f"adv:{bad_role}", role=bad_role),
                        expected_valid=False,
                        expected_error_code="PermissionDeniedError",
                    )
                )

            # Track 2.3: Terminal & incompatible FSM state probes
            for act in target_act_defs:
                subj_type, id_param = _primary_subject_for_action(act, ot_by_name)
                if subj_type is None or subj_type not in ot_by_name:
                    continue
                ot = ot_by_name[subj_type]
                if not ot.transitions or not m0_by_type.get(subj_type):
                    continue
                oid, odata = next(iter(sorted(m0_by_type[subj_type].items())))
                params = _candidate_param_dicts(act, oid, id_param, odata)[0]
                terminal_states = sorted(st for st, nxts in ot.transitions.items() if not nxts)
                term_state = terminal_states[0] if terminal_states else None
                real_term = next(
                    (
                        (r_id, r_data)
                        for r_id, r_data in sorted(m0_by_type[subj_type].items())
                        if term_state is not None and r_data.get(ot.status_field) == term_state
                    ),
                    None,
                )
                actor = _candidate_actors_for_action(act)[0]
                if real_term is not None:
                    term_id, term_data = real_term
                    cases.append(
                        FalsificationCase(
                            name=f"ADV-TERM-{act.name}",
                            track="adversarial",
                            action=act.name,
                            params=_candidate_param_dicts(act, term_id, id_param, term_data)[0],
                            actor=actor,
                            expected_valid=False,
                        )
                    )
                elif term_state is not None:
                    term_id = f"ADV-TERM-{subj_type}"
                    term_data = {**odata, ot.primary_key: term_id, ot.status_field: term_state}
                    cases.append(
                        FalsificationCase(
                            name=f"ADV-SYN-TERM-{act.name}",
                            track="adversarial",
                            action=act.name,
                            params={**params, id_param: term_id},
                            actor=actor,
                            expected_valid=False,
                            setup_objects=({"object_type": subj_type, "data": term_data},),
                        )
                    )

            # Track 2.4: Role-conditional precondition gates (APPROVAL_REQUIRED_*)
            for act in target_act_defs:
                subj_type, id_param = _primary_subject_for_action(act, ot_by_name)
                if subj_type is None or not m0_by_type.get(subj_type):
                    continue
                for pre in act.preconditions:
                    if not pre.code.startswith("APPROVAL_REQUIRED_"):
                        continue
                    priv_roles: list[str] = []
                    if (
                        isinstance(pre.must_hold, dict)
                        and pre.must_hold.get("op") in ("in", "eq")
                        and isinstance(pre.must_hold.get("left"), dict)
                        and pre.must_hold["left"].get("ref") == "actor.role"
                    ):
                        rv = (pre.must_hold.get("right") or {}).get("lit")
                        priv_roles = [str(x) for x in rv] if isinstance(rv, list) else ([str(rv)] if rv else [])
                    non_priv = [r for r in act.allowed_roles if r not in priv_roles]
                    if not non_priv:
                        continue
                    unpriv_actor = Actor(actor_id=f"adv:{non_priv[0]}", role=non_priv[0])
                    matched_gate_case: FalsificationCase | None = None
                    for oid, odata in sorted(m0_by_type[subj_type].items()):
                        for pdict in _candidate_param_dicts(act, oid, id_param, odata):
                            try:
                                sim = eval_rt.simulate_action(act.name, pdict, unpriv_actor)
                            except Exception:
                                continue
                            if not sim.valid and sim.error_code == pre.code:
                                matched_gate_case = FalsificationCase(
                                    name=f"ADV-GATE-{act.name}-{pre.code}-{oid[:8]}",
                                    track="adversarial",
                                    action=act.name,
                                    params=pdict,
                                    actor=unpriv_actor,
                                    expected_valid=False,
                                    expected_error_code=pre.code,
                                )
                                break
                        if matched_gate_case is not None:
                            break
                    if matched_gate_case is not None:
                        cases.append(matched_gate_case)
        finally:
            eval_rt.close()

        return cases

    def _run_case_batch(
        self,
        *,
        cases: Sequence[FalsificationCase],
        spec: OntologySpec,
        blueprint: DeclarativeBlueprint,
        ot_by_name: dict[str, ObjectTypeBlueprint],
        target_act_map: Mapping[str, ActionBlueprint],
        adapter_mapping: AdapterMappingSpec,
        sandbox: ExternalSandboxAdapter,
        covered_actions: set[str],
        covered_edges: set[tuple[str, str, str]],
        counterexamples: list[FalsificationCounterexample],
    ) -> dict[str, int]:
        escapes = 0
        mismatches = 0
        dirty_writes = 0
        for idx, case in enumerate(cases):
            sandbox.reset()
            m0_base = list(sandbox.snapshot_m0())
            bridge = sandbox.make_source_bridge(spec, adapter_mapping)
            mem_rt = OntologyRuntime(spec, db_path=":memory:")
            coupled_rt = OntologyRuntime(
                spec, db_path=":memory:", world_mode="coupled", source_bridge=bridge
            )
            try:
                _sync_rt_with_m0(mem_rt, spec, m0_base)
                _sync_rt_with_m0(coupled_rt, spec, m0_base)
                for so in case.setup_objects:
                    cls = spec.get_object_type(so["object_type"])
                    obj = cls(**so["data"])
                    mem_rt.put_object(obj)
                    coupled_rt.put_object(obj)

                try:
                    mem_sim = mem_rt.simulate_action(case.action, case.params, case.actor)
                except PermissionDeniedError as exc:
                    mem_sim = SimulationResult(
                        valid=False,
                        diff=ObjectDiff(),
                        read_versions={},
                        error=str(exc),
                        error_code="PermissionDeniedError",
                    )
                except GovernanceViolationError as exc:
                    mem_sim = SimulationResult(
                        valid=False,
                        diff=ObjectDiff(),
                        read_versions={},
                        error=str(exc),
                        error_code=getattr(exc, "code", None) or "GovernanceViolationError",
                    )

                writes_before = sandbox.external_write_count()
                snap_before_digest = _snapshot_digest(sandbox.snapshot_m0())

                coupled_valid = False
                coupled_err_code: str | None = None
                coupled_err_msg: str | None = None
                try:
                    run_res = coupled_rt.submit_action(
                        f"FALS-CASE-{idx}-{case.action}",
                        case.action,
                        case.params,
                        case.actor,
                    )
                    coupled_valid = run_res.get("status") == "COMMITTED"
                    coupled_err_code = run_res.get("error_code")
                except (InvariantError, GovernanceViolationError) as exc:
                    coupled_valid = False
                    coupled_err_code = getattr(exc, "code", None) or type(exc).__name__
                    coupled_err_msg = str(exc)
                except PermissionDeniedError as exc:
                    coupled_valid = False
                    coupled_err_code = "PermissionDeniedError"
                    coupled_err_msg = str(exc)
                except Exception as exc:
                    coupled_valid = False
                    coupled_err_code = type(exc).__name__
                    coupled_err_msg = str(exc)

                if case.expected_valid:
                    # Both worlds must allow and produce identical post-state in external snapshot
                    if not mem_sim.valid or not coupled_valid or mem_sim.valid != coupled_valid:
                        mismatches += 1
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DIFFERENTIAL_STATE_MISMATCH",
                                case_name=case.name,
                                action=case.action,
                                message=(
                                    f"expected valid=True: mem_valid={mem_sim.valid}({mem_sim.error_code}), "
                                    f"coupled_valid={coupled_valid}({coupled_err_code}: {coupled_err_msg})"
                                ),
                            )
                        )
                        continue
                    diff_errs = self._compare_diff_against_m0(
                        mem_sim, list(sandbox.snapshot_m0()), spec
                    )
                    if diff_errs:
                        mismatches += 1
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DIFFERENTIAL_STATE_MISMATCH",
                                case_name=case.name,
                                action=case.action,
                                message="; ".join(diff_errs),
                            )
                        )
                    else:
                        if case.action in target_act_map:
                            covered_actions.add(case.action)
                        for edge in self._extract_transition_edges(mem_sim, ot_by_name):
                            covered_edges.add(edge)
                else:
                    # Adversarial / expected invalid: MUST reject and leave physical sandbox untouched
                    # Skip dynamic APPROVAL_REQUIRED_* auto-generated candidate if M0 row didn't hit pre.when
                    if case.name.startswith("ADV-GATE-") and not mem_sim.valid and case.expected_error_code and mem_sim.error_code != case.expected_error_code:
                        continue
                    if mem_sim.valid != coupled_valid:
                        mismatches += 1
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DECISION_DIVERGENCE",
                                case_name=case.name,
                                action=case.action,
                                message=f"mem_valid={mem_sim.valid} vs coupled_valid={coupled_valid}",
                            )
                        )
                    if coupled_valid:
                        escapes += 1
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="ADVERSARIAL_ESCAPE",
                                case_name=case.name,
                                action=case.action,
                                message=(
                                    f"ADVERSARIAL_ESCAPE: expected rejection ({case.expected_error_code or 'any'}), "
                                    f"but coupled sandbox COMMITTED for role={case.actor.role!r}"
                                ),
                            )
                        )
                    elif case.expected_error_code and not (
                        coupled_err_code == case.expected_error_code
                        or (
                            case.expected_error_code == "PermissionDeniedError"
                            and coupled_err_code == "PermissionDeniedError"
                        )
                        or (case.expected_error_code in (coupled_err_msg or ""))
                    ):
                        escapes += 1
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="ADVERSARIAL_ESCAPE",
                                case_name=case.name,
                                action=case.action,
                                message=(
                                    f"ADVERSARIAL_ESCAPE: expected error_code {case.expected_error_code!r}, "
                                    f"got {coupled_err_code!r} ({coupled_err_msg})"
                                ),
                            )
                        )

                    writes_after = sandbox.external_write_count()
                    snap_after_digest = _snapshot_digest(sandbox.snapshot_m0())
                    if writes_after > writes_before or snap_after_digest != snap_before_digest:
                        dirty_writes += 1
                        counterexamples.append(
                            FalsificationCounterexample(
                                kind="DIRTY_EXTERNAL_WRITE",
                                case_name=case.name,
                                action=case.action,
                                message=(
                                    f"DIRTY_EXTERNAL_WRITE: rejected adversarial case mutated external sandbox "
                                    f"(writes {writes_before} -> {writes_after})"
                                ),
                            )
                        )
            finally:
                mem_rt.close()
                coupled_rt.close()

        return {"escapes": escapes, "mismatches": mismatches, "dirty_writes": dirty_writes}

    @staticmethod
    def _extract_transition_edges(
        sim: SimulationResult,
        ot_by_name: dict[str, ObjectTypeBlueprint],
    ) -> list[tuple[str, str, str]]:
        edges: list[tuple[str, str, str]] = []
        for ch in sim.diff.object_changes:
            if ch.kind != "update":
                continue
            ot = ot_by_name.get(ch.object_type)
            if ot is None or not ot.transitions:
                continue
            sf = ot.status_field
            b = (ch.before or {}).get(sf)
            a = (ch.after or {}).get(sf)
            if b is not None and a is not None and b != a:
                edges.append((ch.object_type, str(b), str(a)))
        return edges

    @staticmethod
    def _compare_diff_against_m0(
        sim: SimulationResult,
        post_m0: Sequence[dict[str, Any]],
        spec: OntologySpec,
    ) -> list[str]:
        """Compare every non-owned field mutated in M1 simulation against external post-action snapshot."""
        by_key = {
            (str(e.get("object_type") or ""), str(e.get("object_id") or "")): dict(e.get("data") or {})
            for e in post_m0
        }
        errors: list[str] = []
        for ch in sim.diff.object_changes:
            cls = spec.get_object_type(ch.object_type)
            if cls.is_fully_owned():
                continue
            owned_fields = set(cls.owned_field_defaults())
            pk_field = cls.primary_key_name()
            ext_data = by_key.get((ch.object_type, ch.object_id))
            if ext_data is None:
                errors.append(
                    f"{ch.object_type}:{ch.object_id} missing from post-action external snapshot"
                )
                continue
            after = ch.after or {}
            before = ch.before or {}
            for fname, exp_val in after.items():
                if fname == pk_field or fname in owned_fields:
                    continue
                if ch.kind == "update" and before.get(fname) == exp_val:
                    continue
                act_val = ext_data.get(fname)
                if (
                    isinstance(exp_val, (int, float))
                    and not isinstance(exp_val, bool)
                    and isinstance(act_val, (int, float))
                    and not isinstance(act_val, bool)
                ):
                    if abs(float(exp_val) - float(act_val)) > 1e-6:
                        errors.append(
                            f"DIFFERENTIAL_STATE_MISMATCH on {ch.object_type}:{ch.object_id}.{fname}: "
                            f"M1 predicted {exp_val!r}, external snapshot has {act_val!r}"
                        )
                elif act_val != exp_val:
                    errors.append(
                        f"DIFFERENTIAL_STATE_MISMATCH on {ch.object_type}:{ch.object_id}.{fname}: "
                        f"M1 predicted {exp_val!r}, external snapshot has {act_val!r}"
                    )
        return errors


def decompile_and_falsify_driver(
    *,
    model_id: str,
    target_blueprint: DeclarativeBlueprint,
    external_profile: ExternalPhenomenonProfile,
    sandbox: ExternalSandboxAdapter,
    decompiler: DriverDecompilerAgent,
    governance: InductionGovernanceService,
    falsifier: DifferentialFalsifier | None = None,
    crucible_context: CrucibleContext | None = None,
    overlay_requirement_doc: str = "",
    phenomenon_brief: Mapping[str, Any] | None = None,
    required_object_types: Sequence[str] = (),
    required_actions: Sequence[str] = (),
    seed_ontology: SeedOntology | None = None,
    approver_actor: Actor | None = None,
    max_decompile_rounds: int = 3,
    max_healing_rounds: int = 3,
) -> DecompileAndFalsifyResult:
    """Closed self-healing loop: DriverDecompilerAgent -> SymbolicCrucible -> DifferentialFalsifier."""
    from .governance import STEWARD_ACTOR

    active_falsifier = falsifier or DifferentialFalsifier()
    active_approver = approver_actor or STEWARD_ACTOR
    last_dec: DecompilationCycleResult | None = None
    last_report: DifferentialFalsificationReport | None = None
    extra_feedback = ""

    for heal_round in range(1, max_healing_rounds + 1):
        round_model_id = model_id if heal_round == 1 else f"{model_id}_heal_{heal_round}"
        dec = decompiler.run_decompilation_cycle(
            model_id=round_model_id,
            target_blueprint=target_blueprint,
            external_profile=external_profile,
            crucible_context=crucible_context,
            overlay_requirement_doc=overlay_requirement_doc,
            phenomenon_brief=phenomenon_brief,
            required_object_types=required_object_types,
            required_actions=required_actions,
            seed_ontology=seed_ontology,
            max_rounds=max_decompile_rounds,
            initial_feedback=extra_feedback,
        )
        last_dec = dec
        if not dec.verified or dec.effective_blueprint is None or dec.adapter_mapping is None:
            extra_feedback = dec.final_report.diagnostic_feedback()
            continue

        report = active_falsifier.evaluate(
            blueprint=dec.effective_blueprint,
            adapter_mapping=dec.adapter_mapping,
            sandbox=sandbox,
            seed_ontology=seed_ontology,
            target_actions=required_actions or None,
        )
        last_report = report
        if report.verdict == "CORROBORATED":
            if dec.pending_approval_run_id is not None:
                governance.approve_crystallization(dec.pending_approval_run_id, active_approver)
            return DecompileAndFalsifyResult(
                verified=True,
                corroborated=True,
                healing_rounds_used=heal_round,
                decompilation=dec,
                falsification=report,
            )

        extra_feedback = (
            f"DIFFERENTIAL_FALSIFICATION_FAILED (healing round {heal_round}):\n"
            f"{report.diagnostic_feedback()}\n"
            "Repair the faulty AdapterMappingSpec (enum_map / id_prefix / foreign_key_id / action_bindings) "
            "or missing CustomerOverlaySpec policy guardrails and resubmit."
        )

    assert last_dec is not None
    return DecompileAndFalsifyResult(
        verified=bool(last_dec.verified),
        corroborated=False,
        healing_rounds_used=max_healing_rounds,
        decompilation=last_dec,
        falsification=last_report,
    )
