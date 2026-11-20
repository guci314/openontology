"""非拟人化、100% 确定性五关符号证伪熔炉（SymbolicCrucible · 纯 M2，领域无关）。

修复与保障：
* 关 2 完整导出并校验 ``ontology_objects`` + ``ontology_overlay`` + ``ontology_links``；
* 关 5 扩展至业务编码/实例名防过拟合校验，并加入全蓝图全局复杂度上限（防长期演化补丁膨胀）；
* 熔炉独立持有 ``CRUCIBLE_ACTOR`` 签发绑定 ``blueprint_hash`` 的不可篡改证伪报告。
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from ..osdk import EvalReport, run_eval_suite
from ..runtime import OntologyRuntime, SimulationResult
from ..sandbox import EditBatch, ObjectDiff
from ..spec import Actor, InvariantError, OntologySpec, PermissionDeniedError
from .dsl import (
    COMPARISON_OPS,
    DeclarativeBlueprint,
    EvolutionPatch,
    FieldMigrationSpec,
    ProposedEvalCase,
    blueprint_fingerprint,
    compile_blueprint,
    count_ast_nodes,
    expr_depth,
    migrate_m0_snapshot,
)
from .governance import CRUCIBLE_ACTOR, InductionGovernanceService
from .seed import SeedOntology

__all__ = [
    "ReplayEvent",
    "IncidentCounterexample",
    "CrucibleContext",
    "GateResult",
    "CrucibleReport",
    "SymbolicCrucible",
]

_INSTANCE_ID_LITERAL_RE = re.compile(r"^[A-Z]{2,}(?:-[A-Z0-9]+)+$")
_ZERO_PADDED_SERIAL_RE = re.compile(r"^[A-Z]{2,6}-(?:0\d+|\d{4}-\d{2,})(?:-[A-Z0-9]+)*$")
_UUID_LITERAL_RE = re.compile(
    r"^(?:[A-Z]{2,12}-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    r"|^(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{32}$"
)


@dataclass(frozen=True)
class ReplayEvent:
    name: str
    action: str
    params: dict[str, Any]
    actor: Actor
    expected_valid: bool = True
    expected_error_code: str | None = None
    setup_objects: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class IncidentCounterexample:
    name: str
    action: str
    params: dict[str, Any]
    actor: Actor
    expect_valid: bool | None = None
    expect_error_code: str | None = None
    expect_after: dict[str, Any] | None = None
    setup_objects: tuple[dict[str, Any], ...] = ()


@dataclass
class CrucibleContext:
    mode: Literal["genesis", "evolution"]
    m0_objects: list[dict[str, Any]] = field(default_factory=list)
    m0_links: list[dict[str, Any]] = field(default_factory=list)
    m0_overlays: list[dict[str, Any]] = field(default_factory=list)
    positive_events: list[ReplayEvent] = field(default_factory=list)
    negative_events: list[ReplayEvent] = field(default_factory=list)
    replay_factory: Callable[
        ..., tuple[list[ReplayEvent], list[ReplayEvent]]
    ] | None = None
    extra_gate2_errors: list[str] = field(default_factory=list)
    incident_counterexamples: list[IncidentCounterexample] = field(default_factory=list)
    golden_eval_cases: list[dict[str, Any]] = field(default_factory=list)
    proposed_eval_cases: list[dict[str, Any]] = field(default_factory=list)
    adapter_mapping: Any | None = None
    default_actor: Actor = field(
        default_factory=lambda: Actor(actor_id="eval:actor", role="admin", clearance_tags=frozenset({"*"}))
    )
    seed_ontology: SeedOntology | None = None
    production_runtime: OntologyRuntime | None = None
    known_instance_ids: set[str] = field(default_factory=set)
    min_positive_replay_rate: float = 1.0
    min_negative_rejection_rate: float = 1.0
    max_ast_depth: int = 8
    max_patch_rules: int = 12
    max_total_invariants: int = 30
    max_total_ast_nodes: int = 250


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    summary: str
    counterexamples: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrucibleReport:
    passed: bool
    mode: str
    model_id: str
    version: str
    gates: tuple[GateResult, ...]
    production_untouched: bool = True
    blueprint_hash: str = ""

    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    def diagnostic_feedback(self) -> str:
        lines = [
            f"Crucible verdict: {'PASS' if self.passed else 'REFUTED'} "
            f"({self.model_id} v{self.version}, mode={self.mode})"
        ]
        for g in self.gates:
            tag = "PASS" if g.passed else "FAIL"
            lines.append(f"- [{tag}] {g.gate}: {g.summary}")
            for ce in g.counterexamples:
                lines.append(f"    * counterexample: {ce}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "model_id": self.model_id,
            "version": self.version,
            "blueprint_hash": self.blueprint_hash,
            "production_untouched": self.production_untouched,
            "gates": [
                {
                    "gate": g.gate,
                    "passed": g.passed,
                    "summary": g.summary,
                    "counterexamples": list(g.counterexamples),
                    "metrics": dict(g.metrics),
                }
                for g in self.gates
            ],
        }


def _collect_m0_identifiers(
    m0_objects: Sequence[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Collect known IDs and high-cardinality / name / ref instance codes from M0 objects."""
    known_ids: set[str] = set()
    known_codes: set[str] = set()
    code_col_values: dict[tuple[str, str], list[str]] = {}

    for obj in m0_objects:
        ot_name = str(obj.get("object_type") or "")
        if obj.get("object_id") not in (None, ""):
            known_ids.add(str(obj["object_id"]))
        data = obj.get("data") or {}
        for k, v in data.items():
            if v is None or v == "" or isinstance(v, bool):
                continue
            if k == "id" or k.endswith("_id"):
                if isinstance(v, (str, int)):
                    known_ids.add(str(v))
            elif k == "name" or k.endswith(("_ref", "_name")):
                if isinstance(v, str):
                    known_codes.add(v)
            elif k == "code" or k.endswith("_code"):
                if isinstance(v, str):
                    code_col_values.setdefault((ot_name, k), []).append(v)

    for vals in code_col_values.values():
        if len(vals) >= 3 and len(set(vals)) == len(vals):
            known_codes.update(vals)
        elif any(_ZERO_PADDED_SERIAL_RE.match(v) or _UUID_LITERAL_RE.match(v) for v in vals):
            known_codes.update(
                v for v in vals if _ZERO_PADDED_SERIAL_RE.match(v) or _UUID_LITERAL_RE.match(v)
            )
    return known_ids, known_codes


def _extract_m0_from_runtime(
    runtime: OntologyRuntime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], set[str], set[str]]:
    """Read-only export of ontology_objects, ontology_links, ontology_overlay, and known identifiers."""
    objects: list[dict[str, Any]] = []

    rows = runtime._conn.execute(
        "SELECT object_type, object_id, properties_json FROM ontology_objects ORDER BY object_type, object_id"
    ).fetchall()
    for row in rows:
        oid = str(row["object_id"])
        data = json.loads(row["properties_json"])
        objects.append(
            {
                "object_type": str(row["object_type"]),
                "object_id": oid,
                "data": data,
            }
        )
    known_ids, known_codes = _collect_m0_identifiers(objects)

    links: list[dict[str, Any]] = []
    lrows = runtime._conn.execute(
        "SELECT link_type, source_type, source_id, target_type, target_id FROM ontology_links"
    ).fetchall()
    for lrow in lrows:
        links.append(
            {
                "link_type": str(lrow["link_type"]),
                "source_type": str(lrow["source_type"]),
                "source_id": str(lrow["source_id"]),
                "target_type": str(lrow["target_type"]),
                "target_id": str(lrow["target_id"]),
            }
        )

    overlays: list[dict[str, Any]] = []
    orows = runtime._conn.execute(
        "SELECT object_type, object_id, patch_json FROM ontology_overlay ORDER BY object_type, object_id"
    ).fetchall()
    for orow in orows:
        overlays.append(
            {
                "object_type": str(orow["object_type"]),
                "object_id": str(orow["object_id"]),
                "patch": json.loads(orow["patch_json"]),
            }
        )

    return objects, links, overlays, known_ids, known_codes


def _load_objects_into_sandbox(
    spec: OntologySpec,
    migrated_objects: Sequence[dict[str, Any]],
    overlays: Sequence[dict[str, Any]] = (),
    links: Sequence[dict[str, Any]] = (),
) -> tuple[OntologyRuntime, list[str]]:
    rt = OntologyRuntime(spec, db_path=":memory:")
    errors: list[str] = []

    overlay_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (str(ov["object_type"]), str(ov["object_id"])): dict(ov.get("patch") or {})
        for ov in overlays
    }
    loaded_keys: set[tuple[str, str]] = set()

    order = {name: idx for idx, name in enumerate(spec.object_types)}
    ordered = sorted(
        migrated_objects,
        key=lambda e: order.get(str(e.get("object_type") or ""), 9999),
    )
    for entry in ordered:
        ot_name = str(entry.get("object_type") or "")
        oid = str(entry.get("object_id") or "")
        data = dict(entry.get("data") or {})
        ov_patch = overlay_by_key.get((ot_name, oid))
        if ov_patch:
            data.update(ov_patch)
        try:
            cls = spec.get_object_type(ot_name)
            obj = cls(**data)
            rt.put_object(obj)
            loaded_keys.add((ot_name, obj.primary_key_value()))
            if ov_patch:
                # Verify overlay fields remain declared as owned on the candidate M1
                owned_defaults = cls.owned_field_defaults()
                if not cls.is_fully_owned():
                    for k in ov_patch:
                        if k not in owned_defaults:
                            errors.append(
                                f"OVERLAY_OWNERSHIP_LOST: {ot_name}:{oid}.{k} is in production "
                                "ontology_overlay but is no longer declared __owned__ in candidate M1"
                            )
                with rt._write_gate() as conn:
                    rt._sync_overlay(conn, ot_name, obj.primary_key_value(), obj.to_properties())
        except Exception as exc:
            errors.append(f"load {ot_name}:{oid}: {type(exc).__name__}: {exc}")

    # Check for orphaned overlays whose base object disappeared in candidate M1
    for (ot_name, oid) in overlay_by_key:
        if (ot_name, oid) not in loaded_keys:
            errors.append(
                f"ORPHANED_OVERLAY: overlay for {ot_name}:{oid} has no matching object in candidate M1"
            )

    # Restore & validate standalone m0_links declared in candidate spec
    if links:
        with rt._write_gate() as conn:
            for lk in links:
                ltype = str(lk.get("link_type") or "")
                st = str(lk.get("source_type") or "")
                sid = str(lk.get("source_id") or "")
                tt = str(lk.get("target_type") or "")
                tid = str(lk.get("target_id") or "")
                if ltype not in spec.object_properties:
                    continue
                if (st, sid) not in loaded_keys:
                    errors.append(f"DANGLING_LINK_SOURCE: {ltype} source {st}:{sid} missing")
                    continue
                if (tt, tid) not in loaded_keys:
                    errors.append(f"DANGLING_LINK_TARGET: {ltype} target {tt}:{tid} missing")
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ontology_links
                        (link_type, source_type, source_id, target_type, target_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ltype, st, sid, tt, tid),
                )

    return rt, errors


def _validate_all_post_state_invariants(rt: OntologyRuntime) -> list[str]:
    errors: list[str] = []
    sys_actor = Actor(actor_id="system:crucible", role="system", clearance_tags=frozenset({"*"}))
    batch = EditBatch(rt.spec, rt, sys_actor)
    view = batch.invariant_view()

    for cls in rt.spec.object_types.values():
        prop_map = getattr(cls, "__object_properties__", None) or {}
        if not prop_map:
            continue
        for obj in view.query(cls):
            for fname, decl in prop_map.items():
                raw = getattr(obj, fname, None)
                if raw is None:
                    continue
                targets = list(raw) if isinstance(raw, (list, tuple)) else [raw]
                target_cls = rt.spec.get_object_type(decl.range)
                for tid in targets:
                    if view.get(target_cls, str(tid)) is None:
                        errors.append(
                            f"TARGET_NOT_FOUND: {cls.__name__}:{obj.primary_key_value()}.{fname} "
                            f"references missing {decl.range}:{tid}"
                        )

    for inv in rt.spec.invariants.values():
        try:
            res = inv.handler(view)
            if res is False:
                errors.append(f"invariant {inv.name} returned False")
        except InvariantError as exc:
            errors.append(f"invariant {inv.name} [{exc.code}]: {exc}")
        except Exception as exc:
            errors.append(f"invariant {inv.name} error: {type(exc).__name__}: {exc}")
    return errors


def _walk_overfitting_checks(
    expr: Any,
    *,
    pk_fields: set[str],
    rel_fields: set[str] = frozenset(),
    known_ids: set[str],
    known_codes: set[str],
    rule_label: str,
    findings: list[str],
) -> None:
    if not isinstance(expr, dict):
        return
    op = expr.get("op")
    if op in COMPARISON_OPS:
        left = expr.get("left")
        right = expr.get("right")
        for ref_side, val_side in ((left, right), (right, left)):
            if not (isinstance(ref_side, dict) and "ref" in ref_side):
                continue
            ref_path = str(ref_side["ref"])
            leaf_field = ref_path.split(".")[-1]
            lit_values: list[Any] = []
            if isinstance(val_side, dict) and "lit" in val_side:
                raw_lit = val_side["lit"]
                lit_values = list(raw_lit) if isinstance(raw_lit, list) else [raw_lit]
            elif isinstance(val_side, (str, int, float)):
                lit_values = [val_side]
            elif isinstance(val_side, list):
                lit_values = list(val_side)

            is_id_field = (
                leaf_field in pk_fields
                or leaf_field in rel_fields
                or leaf_field == "id"
                or leaf_field.endswith("_id")
            )
            is_code_or_name = (
                leaf_field in ("name", "code")
                or leaf_field.endswith("_code")
                or leaf_field.endswith("_ref")
                or leaf_field.endswith("_name")
            )
            for lv in lit_values:
                if lv is None or isinstance(lv, bool):
                    continue
                if isinstance(lv, int):
                    if is_id_field and (op in ("eq", "ne", "in", "not_in") or str(lv) in known_ids):
                        findings.append(
                            f"OVERFITTING_INSTANCE_ID in {rule_label}: "
                            f"hardcoding numeric identifier literal {lv!r} via {ref_path}"
                        )
                    continue
                if not isinstance(lv, str) or not lv:
                    continue
                if _UUID_LITERAL_RE.match(lv):
                    findings.append(
                        f"OVERFITTING_INSTANCE_ID in {rule_label}: "
                        f"rule references concrete UUID literal {lv!r} via {ref_path}"
                    )
                elif lv in known_ids:
                    findings.append(
                        f"OVERFITTING_INSTANCE_ID in {rule_label}: "
                        f"rule references concrete instance ID {lv!r} via {ref_path}"
                    )
                elif lv in known_codes and op in ("eq", "ne", "in", "not_in"):
                    findings.append(
                        f"OVERFITTING_INSTANCE_CODE in {rule_label}: "
                        f"rule hardcodes concrete instance code/name {lv!r} via {ref_path}"
                    )
                elif is_id_field and (_INSTANCE_ID_LITERAL_RE.match(lv) or op in ("eq", "ne", "in", "not_in")):
                    findings.append(
                        f"OVERFITTING_INSTANCE_ID in {rule_label}: "
                        f"hardcoding literal {lv!r} on identifier field {ref_path}"
                    )
                elif is_code_or_name and _ZERO_PADDED_SERIAL_RE.match(lv):
                    findings.append(
                        f"OVERFITTING_INSTANCE_CODE in {rule_label}: "
                        f"hardcoding serial code literal {lv!r} on field {ref_path}"
                    )
    for k, v in expr.items():
        if k == "lit":
            continue
        if isinstance(v, dict):
            _walk_overfitting_checks(
                v,
                pk_fields=pk_fields,
                rel_fields=rel_fields,
                known_ids=known_ids,
                known_codes=known_codes,
                rule_label=rule_label,
                findings=findings,
            )
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _walk_overfitting_checks(
                        item,
                        pk_fields=pk_fields,
                        rel_fields=rel_fields,
                        known_ids=known_ids,
                        known_codes=known_codes,
                        rule_label=rule_label,
                        findings=findings,
                    )


def _simulate_action_with_rbac(
    rt: OntologyRuntime, action: str, params: dict[str, Any], actor: Actor
) -> SimulationResult:
    """Wrap simulate_action so PermissionDeniedError becomes a structured invalid SimulationResult."""
    try:
        return rt.simulate_action(action, params, actor)
    except PermissionDeniedError as exc:
        return SimulationResult(
            valid=False,
            diff=ObjectDiff(),
            read_versions={},
            error=str(exc),
            error_code="PermissionDeniedError",
        )


def _run_single_eval_case(
    sandbox_rt: OntologyRuntime,
    compiled_spec: OntologySpec,
    migrations: Sequence[FieldMigrationSpec],
    default_actor: Actor,
    raw_case: dict[str, Any],
) -> tuple[str, bool, list[str]]:
    """Run one golden or proposed eval case inside an isolated forked runtime."""
    case_name = str(raw_case.get("name") or "case")
    try:
        case_dict = (
            ProposedEvalCase.model_validate(raw_case).model_dump(mode="json")
            if ("expect" not in raw_case or "setup_objects" in raw_case)
            else dict(raw_case)
        )
        case_name = str(case_dict.get("name") or case_name)
    except Exception as exc:
        return case_name, False, [f"invalid eval case schema: {type(exc).__name__}: {exc}"]

    case_rt = sandbox_rt.fork()
    try:
        for so in case_dict.get("setup_objects") or []:
            migrated_so = migrate_m0_snapshot(
                [{"object_type": so["object_type"], "data": so["data"]}],
                migrations,
            )[0]
            cls = compiled_spec.get_object_type(migrated_so["object_type"])
            case_rt.put_object(cls(**migrated_so["data"]))

        raw_expect = dict(case_dict.get("expect") or {})
        osdk_expect: dict[str, Any] = {}
        for k, v in raw_expect.items():
            if k == "error_code" or v is None:
                continue
            if k in ("object_changes", "link_changes") and not v:
                continue
            osdk_expect[k] = v

        osdk_case: dict[str, Any] = {
            "name": case_name,
            "action": case_dict["action"],
            "params": dict(case_dict.get("params") or {}),
            "expect": osdk_expect,
        }
        if "actor" in case_dict and case_dict["actor"] is not None:
            osdk_case["actor"] = case_dict["actor"]

        rep: EvalReport = run_eval_suite(case_rt, default_actor, [osdk_case])
        res = rep.results[0]
        failures = list(res.failures)
        if raw_expect.get("valid") is True and res.simulation and not res.simulation.get("valid"):
            sim = res.simulation
            failures.append(
                f"actual_error_code={sim.get('error_code')!r}, actual_error={sim.get('error')!r}"
            )
        if (
            raw_expect.get("valid") is False
            and len(failures) == 1
            and failures[0].startswith("simulate raised PermissionDeniedError:")
        ):
            failures = []
            if res.simulation is not None:
                res.simulation["error_code"] = "PermissionDeniedError"
        exp_code = raw_expect.get("error_code")
        if exp_code:
            sim = res.simulation or {}
            if not (
                sim.get("error_code") == exp_code
                or str(exp_code) in str(sim.get("error") or "")
            ):
                failures.append(
                    f"error_code: expected {exp_code!r}, got code={sim.get('error_code')!r} "
                    f"error={sim.get('error')!r}"
                )
        return case_name, (not failures), failures
    except Exception as exc:
        return case_name, False, [f"eval execution raised {type(exc).__name__}: {exc}"]
    finally:
        case_rt.close()


class SymbolicCrucible:
    """Non-anthropomorphized 100% deterministic 5-gate falsification engine."""

    def __init__(self, *, actor: Actor = CRUCIBLE_ACTOR) -> None:
        self._actor = actor

    def verify_and_record(
        self,
        governance: InductionGovernanceService,
        *,
        proposal_id: str,
        report_id: str,
        candidate: DeclarativeBlueprint,
        context: CrucibleContext,
        patch: EvolutionPatch | None = None,
    ) -> CrucibleReport:
        """Independently run all 5 gates and sign the append-only FalsificationReport under CRUCIBLE_ACTOR."""
        report = self.evaluate(candidate, context, patch=patch)
        governance.runtime.submit_action(
            f"VERIFY-{report_id}",
            "record_falsification_report",
            {
                "report_id": report_id,
                "proposal_id": proposal_id,
                "passed": report.passed,
                "blueprint_hash": report.blueprint_hash,
                "report_json": json.dumps(report.to_dict(), ensure_ascii=False),
            },
            self._actor,
        )
        return report

    def record_friction_cluster(
        self,
        governance: InductionGovernanceService,
        *,
        model_id: str,
        cluster: Any,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an escalated production friction cluster into governance under CRUCIBLE_ACTOR."""
        return governance.runtime.submit_action(
            run_id or f"FRC-REC-{cluster.cluster_id}-{uuid.uuid4().hex[:6]}",
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
            self._actor,
        )

    def record_parse_failure(
        self,
        governance: InductionGovernanceService,
        *,
        proposal_id: str,
        report_id: str,
        mode: str,
        model_id: str,
        version: str,
        error_message: str,
    ) -> CrucibleReport:
        """Record a Gate-1 schema/parse failure without crashing the CEGIS loop."""
        g1 = GateResult(
            gate="gate_1_static_safety",
            passed=False,
            summary="candidate JSON failed strict schema validation",
            counterexamples=(error_message,),
        )
        skipped = tuple(
            GateResult(gate=g, passed=False, summary="skipped because gate_1_static_safety failed")
            for g in (
                "gate_2_m0_grounding",
                "gate_3_counterexample_elimination",
                "gate_4_golden_regression",
                "gate_5_parsimony_anti_overfitting",
            )
        )
        report = CrucibleReport(
            passed=False,
            mode=mode,
            model_id=model_id,
            version=version,
            gates=(g1, *skipped),
            production_untouched=True,
            blueprint_hash="",
        )
        try:
            governance.runtime.submit_action(
                f"VERIFY-{report_id}",
                "record_falsification_report",
                {
                    "report_id": report_id,
                    "proposal_id": proposal_id,
                    "passed": False,
                    "blueprint_hash": "",
                    "report_json": json.dumps(report.to_dict(), ensure_ascii=False),
                },
                self._actor,
            )
        except Exception:
            pass
        return report

    def evaluate(
        self,
        candidate: DeclarativeBlueprint,
        context: CrucibleContext,
        *,
        patch: EvolutionPatch | None = None,
    ) -> CrucibleReport:
        bp_hash = blueprint_fingerprint(candidate)
        prod_before = (
            context.production_runtime.state_fingerprint()
            if context.production_runtime is not None
            else None
        )

        m0_objects = list(context.m0_objects)
        m0_links = list(context.m0_links)
        m0_overlays = list(context.m0_overlays)
        known_ids = set(context.known_instance_ids)
        known_codes: set[str] = set()

        if context.production_runtime is not None and not m0_objects:
            exp_objs, exp_links, exp_ovs, exp_ids, exp_codes = _extract_m0_from_runtime(
                context.production_runtime
            )
            m0_objects = exp_objs
            if not m0_links:
                m0_links = exp_links
            if not m0_overlays:
                m0_overlays = exp_ovs
            known_ids |= exp_ids
            known_codes |= exp_codes
        else:
            col_ids, col_codes = _collect_m0_identifiers(m0_objects)
            known_ids |= col_ids
            known_codes |= col_codes

        for inc in context.incident_counterexamples:
            for k, v in inc.params.items():
                if v not in (None, "") and not isinstance(v, bool):
                    if k == "id" or k.endswith("_id"):
                        known_ids.add(str(v))
                    elif isinstance(v, str) and (k == "name" or k.endswith(("_ref", "_name"))):
                        known_codes.add(v)
            for so in inc.setup_objects:
                if so.get("object_id") not in (None, ""):
                    known_ids.add(str(so["object_id"]))

        gates: list[GateResult] = []

        # --- Gate 1: Static & M2 Constitutional Safety (zero eval/exec + SeedOntology contract) ---
        compiled_spec: OntologySpec | None = None
        g1_errors: list[str] = []
        try:
            compiled_spec = compile_blueprint(candidate, seed_ontology=context.seed_ontology)
        except Exception as exc:
            g1_errors.append(f"{type(exc).__name__}: {exc}")

        gates.append(
            GateResult(
                gate="gate_1_static_safety",
                passed=not g1_errors and compiled_spec is not None,
                summary="zero-exec declarative compilation and M2/SeedOntology contract check passed"
                if not g1_errors
                else "static safety or M2/SeedOntology schema compilation failed",
                counterexamples=tuple(g1_errors),
                metrics={
                    "object_types": len(candidate.object_types),
                    "actions": len(candidate.actions),
                    "invariants": len(candidate.invariants),
                    "seed_ontology_id": (
                        context.seed_ontology.seed_id
                        if context.seed_ontology is not None
                        else candidate.seed_ontology_id
                    ),
                },
            )
        )

        if compiled_spec is None:
            for gname in (
                "gate_2_m0_grounding",
                "gate_3_counterexample_elimination",
                "gate_4_golden_regression",
                "gate_5_parsimony_anti_overfitting",
            ):
                gates.append(
                    GateResult(
                        gate=gname,
                        passed=False,
                        summary="skipped because gate_1_static_safety failed",
                    )
                )
            prod_untouched = (
                prod_before == context.production_runtime.state_fingerprint()
                if context.production_runtime is not None
                else True
            )
            return CrucibleReport(
                passed=False,
                mode=context.mode,
                model_id=candidate.model_id,
                version=candidate.version,
                gates=tuple(gates),
                production_untouched=prod_untouched,
                blueprint_hash=bp_hash,
            )

        # --- Gate 2: Legacy M0 Grounding, Overlay & Link Fit ---
        migrated = migrate_m0_snapshot(m0_objects, candidate.migrations)
        sandbox_rt, load_errors = _load_objects_into_sandbox(
            compiled_spec,
            migrated,
            overlays=m0_overlays,
            links=m0_links,
        )
        try:
            inv_errors = _validate_all_post_state_invariants(sandbox_rt) if not load_errors else []
            g2_errors = [*context.extra_gate2_errors, *load_errors, *inv_errors]
            gates.append(
                GateResult(
                    gate="gate_2_m0_grounding",
                    passed=not g2_errors,
                    summary=f"all {len(migrated)} M0 objects, {len(m0_overlays)} overlays, and {len(m0_links)} links satisfied candidate M1"
                    if not g2_errors
                    else f"{len(g2_errors)} M0 grounding/overlay/migration violations detected",
                    counterexamples=tuple(g2_errors[:10]),
                    metrics={
                        "m0_objects_checked": len(migrated),
                        "m0_overlays_checked": len(m0_overlays),
                        "m0_links_checked": len(m0_links),
                        "migrations_applied": len(candidate.migrations),
                        "violations": len(g2_errors),
                    },
                )
            )

            # --- Gate 3: Historical Replay & Counterexample Elimination ---
            g3_errors: list[str] = []
            g3_metrics: dict[str, Any] = {}
            if context.mode == "genesis":
                pos_events = list(context.positive_events)
                neg_events = list(context.negative_events)
                if context.replay_factory is not None:
                    try:
                        sig_params = len(inspect.signature(context.replay_factory).parameters)
                        if sig_params >= 3:
                            dyn_pos, dyn_neg = context.replay_factory(
                                migrated, candidate, context.adapter_mapping
                            )
                        else:
                            dyn_pos, dyn_neg = context.replay_factory(migrated)
                        pos_events.extend(dyn_pos)
                        neg_events.extend(dyn_neg)
                    except Exception as exc:
                        g3_errors.append(f"replay_factory failed: {type(exc).__name__}: {exc}")
                pos_total = len(pos_events)
                pos_ok = 0
                for ev in pos_events:
                    ev_rt = sandbox_rt.fork()
                    try:
                        for so in ev.setup_objects:
                            migrated_so = migrate_m0_snapshot([so], candidate.migrations)[0]
                            cls = compiled_spec.get_object_type(migrated_so["object_type"])
                            ev_rt.put_object(cls(**migrated_so["data"]))
                        sim = _simulate_action_with_rbac(ev_rt, ev.action, ev.params, ev.actor)
                        if sim.valid == ev.expected_valid:
                            pos_ok += 1
                        else:
                            g3_errors.append(
                                f"positive replay {ev.name!r} failed: valid={sim.valid}, "
                                f"error_code={sim.error_code}, error={sim.error}"
                            )
                    except Exception as exc:
                        g3_errors.append(f"positive replay {ev.name!r} raised {exc}")
                    finally:
                        ev_rt.close()

                neg_total = len(neg_events)
                neg_blocked = 0
                for nev in neg_events:
                    nev_rt = sandbox_rt.fork()
                    try:
                        for so in nev.setup_objects:
                            migrated_so = migrate_m0_snapshot([so], candidate.migrations)[0]
                            cls = compiled_spec.get_object_type(migrated_so["object_type"])
                            nev_rt.put_object(cls(**migrated_so["data"]))
                        sim = _simulate_action_with_rbac(nev_rt, nev.action, nev.params, nev.actor)
                        blocked = not sim.valid
                        code_match = (
                            nev.expected_error_code is None
                            or sim.error_code == nev.expected_error_code
                            or (nev.expected_error_code in (sim.error or ""))
                        )
                        if blocked and code_match:
                            neg_blocked += 1
                        else:
                            g3_errors.append(
                                f"negative adversarial case {nev.name!r} was NOT blocked "
                                f"(vacuous rule risk): valid={sim.valid}, "
                                f"error_code={sim.error_code}, error={sim.error}"
                            )
                    except Exception as exc:
                        g3_errors.append(f"negative case {nev.name!r} raised {exc}")
                    finally:
                        nev_rt.close()

                pos_rate = (pos_ok / pos_total) if pos_total else 1.0
                neg_rate = (neg_blocked / neg_total) if neg_total else 1.0
                g3_metrics = {
                    "positive_passed": pos_ok,
                    "positive_total": pos_total,
                    "positive_rate": round(pos_rate, 4),
                    "negative_blocked": neg_blocked,
                    "negative_total": neg_total,
                    "negative_rate": round(neg_rate, 4),
                }
                g3_passed = (
                    pos_rate >= context.min_positive_replay_rate
                    and neg_rate >= context.min_negative_rejection_rate
                    and not g3_errors
                )
            else:
                inc_total = len(context.incident_counterexamples)
                inc_eliminated = 0
                for inc in context.incident_counterexamples:
                    inc_rt = sandbox_rt.fork()
                    try:
                        for so in inc.setup_objects:
                            migrated_so = migrate_m0_snapshot([so], candidate.migrations)[0]
                            cls = compiled_spec.get_object_type(migrated_so["object_type"])
                            inc_rt.put_object(cls(**migrated_so["data"]))
                        sim = _simulate_action_with_rbac(inc_rt, inc.action, inc.params, inc.actor)
                        ok = True
                        if inc.expect_valid is not None and sim.valid != inc.expect_valid:
                            ok = False
                            g3_errors.append(
                                f"incident {inc.name!r}: expected valid={inc.expect_valid}, got {sim.valid}"
                            )
                        if inc.expect_error_code is not None and not (
                            sim.error_code == inc.expect_error_code
                            or (inc.expect_error_code in (sim.error or ""))
                        ):
                            ok = False
                            g3_errors.append(
                                f"incident {inc.name!r}: expected error_code {inc.expect_error_code!r}, "
                                f"got code={sim.error_code!r} error={sim.error!r}"
                            )
                        if inc.expect_after is not None and sim.valid:
                            matched_after = False
                            for ch in sim.diff.object_changes:
                                if ch.after and all(
                                    ch.after.get(k) == v for k, v in inc.expect_after.items()
                                ):
                                    matched_after = True
                                    break
                            if not matched_after:
                                ok = False
                                g3_errors.append(
                                    f"incident {inc.name!r}: predicted diff did not match expected_after "
                                    f"{inc.expect_after}"
                                )
                        if ok:
                            inc_eliminated += 1
                    except Exception as exc:
                        g3_errors.append(f"incident {inc.name!r} raised {exc}")
                    finally:
                        inc_rt.close()
                # Also enforce positive_events & negative_events in evolution mode so
                # overly broad / shortcut rules that break legitimate operations are caught
                # immediately in Gate 3 and fed back as a symbolic gradient.
                evo_pos_total = len(context.positive_events)
                evo_pos_ok = 0
                for ev in context.positive_events:
                    ev_rt = sandbox_rt.fork()
                    try:
                        for so in ev.setup_objects:
                            migrated_so = migrate_m0_snapshot([so], candidate.migrations)[0]
                            cls = compiled_spec.get_object_type(migrated_so["object_type"])
                            ev_rt.put_object(cls(**migrated_so["data"]))
                        sim = _simulate_action_with_rbac(ev_rt, ev.action, ev.params, ev.actor)
                        valid_ok = sim.valid == ev.expected_valid
                        if valid_ok:
                            evo_pos_ok += 1
                        else:
                            g3_errors.append(
                                f"positive replay {ev.name!r} (action={ev.action!r}, "
                                f"actor_role={ev.actor.role!r}) regressed: "
                                f"expected valid={ev.expected_valid}, got valid={sim.valid}, "
                                f"error_code={sim.error_code!r}, error={sim.error!r}"
                            )
                    except Exception as exc:
                        g3_errors.append(f"positive replay {ev.name!r} raised {exc}")
                    finally:
                        ev_rt.close()

                g3_metrics = {
                    "incidents_eliminated": inc_eliminated,
                    "incidents_total": inc_total,
                    "positive_passed": evo_pos_ok,
                    "positive_total": evo_pos_total,
                }
                g3_passed = (
                    inc_eliminated == inc_total
                    and evo_pos_ok == evo_pos_total
                    and not g3_errors
                )

            gates.append(
                GateResult(
                    gate="gate_3_counterexample_elimination",
                    passed=g3_passed,
                    summary="replay fitness and counterexample elimination satisfied"
                    if g3_passed
                    else f"{len(g3_errors)} replay/counterexample failures",
                    counterexamples=tuple(g3_errors[:10]),
                    metrics=g3_metrics,
                )
            )

            # --- Gate 4: Anti-Catastrophic-Forgetting Golden Regression Lock & Proposed Eval Self-Check ---
            g4_errors: list[str] = []
            proposed_cases: list[dict[str, Any]] = [
                *(c.model_dump(mode="json") for c in (patch.proposed_eval_cases if patch is not None else ())),
                *context.proposed_eval_cases,
            ]
            for raw_case in context.golden_eval_cases:
                cname, ok, fails = _run_single_eval_case(
                    sandbox_rt,
                    compiled_spec,
                    candidate.migrations,
                    context.default_actor,
                    raw_case,
                )
                if not ok:
                    g4_errors.append(f"golden case {cname!r} regressed: " + "; ".join(fails))

            for raw_case in proposed_cases:
                cname, ok, fails = _run_single_eval_case(
                    sandbox_rt,
                    compiled_spec,
                    candidate.migrations,
                    context.default_actor,
                    raw_case,
                )
                if not ok:
                    g4_errors.append(
                        f"proposed_eval_case {cname!r} failed self-check on candidate: "
                        + "; ".join(fails)
                    )

            g4_total = len(context.golden_eval_cases) + len(proposed_cases)
            g4_failed = len(g4_errors)
            g4_passed_cnt = max(0, g4_total - g4_failed)
            g4_metrics = {
                "total": g4_total,
                "passed": g4_passed_cnt,
                "failed": g4_failed,
                "golden_total": len(context.golden_eval_cases),
                "proposed_checked": len(proposed_cases),
            }
            g4_passed = g4_failed == 0

            gates.append(
                GateResult(
                    gate="gate_4_golden_regression",
                    passed=g4_passed,
                    summary=f"all {g4_metrics['total']} golden/proposed eval cases passed (0 regressions)"
                    if g4_passed
                    else f"{g4_metrics['failed']}/{g4_metrics['total']} golden/proposed eval cases failed",
                    counterexamples=tuple(g4_errors[:10]),
                    metrics=g4_metrics,
                )
            )

            # --- Gate 5: Occam's Razor / Anti-Overfitting + Global Complexity Budget ---
            pk_fields = {ot.primary_key for ot in candidate.object_types}
            rel_fields = {rel.field for ot in candidate.object_types for rel in ot.relations}
            g5_errors: list[str] = []
            max_seen_depth = 0
            total_ast_nodes = 0

            def _inspect_ast(expr: dict[str, Any] | None, label: str) -> None:
                nonlocal max_seen_depth, total_ast_nodes
                if expr is None:
                    return
                d = expr_depth(expr)
                if d > max_seen_depth:
                    max_seen_depth = d
                total_ast_nodes += count_ast_nodes(expr)
                if d > context.max_ast_depth:
                    g5_errors.append(
                        f"PARSIMONY_VIOLATION in {label}: AST depth {d} > max {context.max_ast_depth}"
                    )
                _walk_overfitting_checks(
                    expr,
                    pk_fields=pk_fields,
                    rel_fields=rel_fields,
                    known_ids=known_ids,
                    known_codes=known_codes,
                    rule_label=label,
                    findings=g5_errors,
                )

            for inv in candidate.invariants:
                _inspect_ast(inv.when, f"invariant:{inv.name}.when")
                _inspect_ast(inv.must_hold, f"invariant:{inv.name}.must_hold")
            for act in candidate.actions:
                for pre in act.preconditions:
                    _inspect_ast(pre.when, f"action:{act.name}.pre:{pre.name}.when")
                    _inspect_ast(pre.must_hold, f"action:{act.name}.pre:{pre.name}.must_hold")

            if len(candidate.invariants) > context.max_total_invariants:
                g5_errors.append(
                    f"GLOBAL_COMPLEXITY_EXCEEDED: blueprint has {len(candidate.invariants)} "
                    f"invariants > max_total_invariants={context.max_total_invariants}"
                )
            if total_ast_nodes > context.max_total_ast_nodes:
                g5_errors.append(
                    f"GLOBAL_COMPLEXITY_EXCEEDED: blueprint has {total_ast_nodes} AST nodes "
                    f"> max_total_ast_nodes={context.max_total_ast_nodes}"
                )

            patch_rule_count = 0
            if patch is not None:
                patch_rule_count = (
                    len(patch.add_invariants)
                    + len(patch.add_actions)
                    + len(patch.update_actions)
                )
                if patch_rule_count > context.max_patch_rules:
                    g5_errors.append(
                        f"PARSIMONY_VIOLATION: patch modifies {patch_rule_count} rules > "
                        f"max_patch_rules={context.max_patch_rules}"
                    )

            gates.append(
                GateResult(
                    gate="gate_5_parsimony_anti_overfitting",
                    passed=not g5_errors,
                    summary="no instance overfitting detected and global rule complexity within budget"
                    if not g5_errors
                    else f"{len(g5_errors)} overfitting/parsimony violations detected",
                    counterexamples=tuple(g5_errors[:10]),
                    metrics={
                        "max_ast_depth_seen": max_seen_depth,
                        "max_ast_depth_limit": context.max_ast_depth,
                        "total_ast_nodes": total_ast_nodes,
                        "patch_rule_count": patch_rule_count,
                    },
                )
            )
        finally:
            sandbox_rt.close()

        prod_untouched = (
            prod_before == context.production_runtime.state_fingerprint()
            if context.production_runtime is not None
            else True
        )
        all_passed = all(g.passed for g in gates) and prod_untouched
        return CrucibleReport(
            passed=all_passed,
            mode=context.mode,
            model_id=candidate.model_id,
            version=candidate.version,
            gates=tuple(gates),
            production_untouched=prod_untouched,
            blueprint_hash=bp_hash,
        )
