"""端到端合龙（第一步 + 第二步）：无人 FDE 自主侦察 → 零剧本自动合成本体 → 五关证伪结晶 → 双向驱动克隆 Odoo 同态回读核销。

全链路严格零硬编码（不含任何具体业务表名、字段名、方法名或业务阈值）：

1. **探针 1 + 探针 2（纯只读无硬编码侦察）**：
   ``OdooFDEScout`` 扫描目标 Odoo 的 ``ir.model`` / ``ir.model.fields`` / ``mail.tracking.value`` /
   ``ir.model.constraint``，自动压缩高熵活跃子图、挖掘真实状态机、隐藏字符串外键、多态混合表与灰区矛盾。
2. **探针 3（隔离克隆沙箱主动黑盒拷问）**：
   ``EphemeralOdooSandbox`` 克隆一次性隔离库（退出自动销毁，源库零写入），``OdooActiveProber`` 从
   ``ir.ui.view`` 自动发现公开方法并实测正向状态迁移、跨表行数副作用与原生 ``UserError`` 护栏。
3. **第一步 · 零剧本自动合成 + 五关证伪结晶**：
   ``synthesize_from_fde_evidence()`` 纯粹依据探针 1+2+3 证据自动合成 ``DeclarativeBlueprint``、
   双向 ``AdapterMappingSpec``（含 ``ActionExecutionBinding``）与自动推导的 Gate 3 正负考卷；
   交由 ``OntologyBuilderAgent``（可接真实 LLM 润色，默认零剧本确定性合成基线）通过
   ``SymbolicCrucible`` 五关证伪并经 ``Steward`` 审批结晶落盘。
4. **第二步 · 通用双向桥接 + 闭环同态回读核销**：
   ``DeclarativeOdooBridge`` 加载结晶产出的 ``OntologySpec`` 与双向 ``AdapterMappingSpec``，
   在隔离克隆 Odoo 沙箱上通过 ``OntologyRuntime(world_mode="coupled", source_bridge=...)`` 执行：
   ``:memory:`` 反事实预测 → 真实调用目标系统方法 → 独立只读回读并经映射投影 → 同态回读核销，
   实地验证同态交换律 ``φ(T_x(x_t, u_t)) == T_y(φ(x_t), a_t)``（``COMMITTED``），并同时验证外部状态漂移时
   精准触发 ``REJECTED (DEVICE_STATE_MISMATCH)`` 且实然世界零污染。

运行方式::

    python3 -m examples.odoo_fde_autonomous_pipeline --db erp_north
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from open_ontology import (
    Actor,
    DEVICE_STATE_MISMATCH,
    DeviceStateMismatchError,
    OntologyRuntime,
    SourceBridge,
)
from open_ontology.induction import (
    AbductionBackend,
    ActionBlueprint,
    ActionExecutionBinding,
    ActionMutationSpec,
    AdapterMappingSpec,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EntityMappingSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldSpec,
    FieldTransformSpec,
    GeminiAbductionBackend,
    InductionCycleResult,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    OpenAICompatibleAbductionBackend,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    SeedOntology,
    SymbolicCrucible,
    compile_blueprint,
    project_external_phenomenon,
    project_single_record,
)

from .business_ontology import build_business_seed_ontology
from .odoo_dual_erp_demo import DB_NORTH
from .odoo_fde_prober import (
    ActionProbeObservation,
    ActiveProbeReport,
    EphemeralOdooSandbox,
    OdooActiveProber,
    ProbeSandboxInterface,
)
from .odoo_fde_scout import (
    DiscoveredModel,
    FDEScoutReport,
    OdooFDEScout,
    ReadOnlyOdooClient,
    ReadOnlyOdooInterface,
)

AI_OPS_ACTOR = Actor(actor_id="agent:autonomous-ops", role="ai_agent", clearance_tags=frozenset({"*"}))
STEWARD_OPS_ACTOR = Actor(actor_id="human:ops-steward", role="steward", clearance_tags=frozenset({"*"}))


# ---------------------------------------------------------------------------
# Generic Naming & Type Coercion Helpers (Zero Domain Hardcoding)
# ---------------------------------------------------------------------------
def _model_to_pascal(model_name: str) -> str:
    parts = [p for p in re.split(r"[._-]+", model_name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Entity"


def _model_to_snake(model_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", model_name).strip("_").lower()
    return cleaned or "entity"


def _model_id_prefix(model_name: str) -> str:
    return f"{_model_to_snake(model_name).upper()}-"


def _ttype_to_field_type(ttype: str) -> str:
    if ttype in ("integer",):
        return "int"
    if ttype in ("float", "monetary"):
        return "float"
    if ttype == "boolean":
        return "bool"
    return "str"


def _default_for_field_type(ftype: str) -> Any:
    if ftype == "int":
        return 0
    if ftype == "float":
        return 0.0
    if ftype == "bool":
        return False
    return ""


def _parse_ext_id(canonical_id: str, prefix: str) -> int:
    raw = canonical_id[len(prefix) :] if prefix and canonical_id.startswith(prefix) else canonical_id
    return int(raw)


# ---------------------------------------------------------------------------
# 1. Referentially-Closed ExternalPhenomenonProfile Builder (from Scout Output)
# ---------------------------------------------------------------------------
def build_closed_phenomenon_profile(
    scout_report: FDEScoutReport,
    client: ReadOnlyOdooInterface | ProbeSandboxInterface | None = None,
) -> ExternalPhenomenonProfile:
    """Build a referentially-closed ExternalPhenomenonProfile from Scout's sampled rows."""
    dm_by_name = {dm.model: dm for dm in scout_report.discovered_models}
    rows_by_model: dict[str, dict[Any, dict[str, Any]]] = {}
    fields_by_model: dict[str, list[str]] = {}

    for dm in scout_report.discovered_models:
        fnames = list(dict.fromkeys(["id", *[f.name for f in dm.retained_fields]]))
        fields_by_model[dm.model] = fnames
        bucket: dict[Any, dict[str, Any]] = {}
        for row in scout_report.sampled_rows_by_model.get(dm.model, ()):
            if row.get("id") is not None:
                bucket[row["id"]] = dict(row)
        rows_by_model[dm.model] = bucket

    # Close missing many2one parent references within the discovered subgraph using real read-only rows
    if client is not None:
        for _pass in range(2):
            missing_by_target: dict[str, set[int]] = defaultdict(set)
            for rel in scout_report.discovered_relations:
                if rel.kind != "explicit_many2one":
                    continue
                if rel.source_model not in rows_by_model or rel.target_model not in rows_by_model:
                    continue
                for row in rows_by_model[rel.source_model].values():
                    raw = row.get(rel.source_field)
                    pid = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
                    if isinstance(pid, int) and not isinstance(pid, bool):
                        if pid not in rows_by_model[rel.target_model]:
                            missing_by_target[rel.target_model].add(pid)
            if not missing_by_target:
                break
            for tgt_model, pids in missing_by_target.items():
                try:
                    fetched = client.search_read(
                        tgt_model,
                        [("id", "in", sorted(pids)[:100])],
                        fields_by_model[tgt_model],
                        limit=100,
                    )
                    for fr in fetched:
                        if fr.get("id") is not None:
                            rows_by_model[tgt_model][fr["id"]] = dict(fr)
                except Exception:
                    continue

    entities: list[ExternalEntityProfile] = []
    for dm in scout_report.discovered_models:
        entities.append(
            ExternalEntityProfile(
                table_name=dm.model,
                row_count=dm.row_count,
                fields=[
                    ExternalFieldProfile(
                        name=f.name,
                        raw_type=f.ttype,
                        label=f.label,
                        relation_target=f.relation_target,
                        enum_values=list(f.selection_values),
                    )
                    for f in dm.retained_fields
                ],
                sample_rows=list(rows_by_model[dm.model].values()),
            )
        )

    return ExternalPhenomenonProfile(
        source_kind="discovered_erp",
        source_locator=scout_report.source_locator,
        entities=entities,
    )


# ---------------------------------------------------------------------------
# 2. Step 1: Zero-Hardcoding Synthesizer (FDE Scout + Probe -> Blueprint + Bidirectional Mapping + Replay Suite)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SynthesizedBundle:
    blueprint: DeclarativeBlueprint
    adapter_mapping: AdapterMappingSpec
    action_probe_evidence: tuple[ActionProbeObservation, ...]
    reversal_actions: tuple[str, ...]


def synthesize_from_fde_evidence(
    scout_report: FDEScoutReport,
    probe_report: ActiveProbeReport,
    *,
    profile: ExternalPhenomenonProfile,
    seed_ontology: SeedOntology | None = None,
    model_id: str = "discovered_erp_ontology",
    version: str = "1.0.0",
) -> SynthesizedBundle:
    """Autonomously synthesize DeclarativeBlueprint + bidirectional AdapterMappingSpec from FDE evidence."""
    active_seed = seed_ontology or build_business_seed_ontology()
    allowed_archetypes = set(active_seed.archetype_names())
    dm_by_name: dict[str, DiscoveredModel] = {dm.model: dm for dm in scout_report.discovered_models}
    pascal_by_model = {dm.model: _model_to_pascal(dm.model) for dm in scout_report.discovered_models}
    prefix_by_model = {dm.model: _model_id_prefix(dm.model) for dm in scout_report.discovered_models}
    rows_by_table = {e.table_name: e.sample_rows for e in profile.entities}
    present_ids_by_model = {
        e.table_name: {r.get("id") for r in e.sample_rows if r.get("id") is not None}
        for e in profile.entities
    }

    # Index mined state machines & reversal edges per model
    sm_by_model: dict[str, Any] = {}
    reversal_transitions_by_model: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for sm in scout_report.mined_state_machines:
        if sm.model not in sm_by_model or sm.field_name == "state":
            sm_by_model[sm.model] = sm
        for rev_desc in sm.backward_or_reversal_edges:
            m = re.match(r"^([^\s]+)\s*->\s*([^\s]+)", rev_desc)
            if m:
                reversal_transitions_by_model[sm.model].add((m.group(1), m.group(2)))

    # Order discovered models so MasterEntity / parent targets precede ProcessDocuments
    def _model_sort_key(dm: DiscoveredModel) -> tuple[int, str]:
        rank = 0 if dm.suggested_archetype == "MasterEntity" else (1 if dm.suggested_archetype == "ObjectType" else 2)
        return (rank, dm.model)

    ordered_dms = sorted(scout_report.discovered_models, key=_model_sort_key)

    object_types: list[ObjectTypeBlueprint] = []
    entity_mappings: list[EntityMappingSpec] = []

    for dm in ordered_dms:
        ot_name = pascal_by_model[dm.model]
        prefix = prefix_by_model[dm.model]
        sample_rows = rows_by_table.get(dm.model, [])

        fields: list[FieldSpec] = [
            FieldSpec(name="record_id", field_type="str", primary_key=True, required=True)
        ]
        relations: list[RelationSpec] = []
        transforms: list[FieldTransformSpec] = []
        drop_missing_refs: list[str] = []
        used_names: set[str] = {"record_id"}

        for f in dm.retained_fields:
            if f.name == "id" or f.name in used_names:
                continue
            if f.ttype == "many2one":
                if f.relation_target in pascal_by_model and f.relation_target != dm.model:
                    tgt_ids = present_ids_by_model.get(f.relation_target, set())
                    has_unresolved = False
                    for r in sample_rows:
                        raw = r.get(f.name)
                        pid = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
                        if isinstance(pid, int) and not isinstance(pid, bool) and pid not in tgt_ids:
                            has_unresolved = True
                            break
                    if not has_unresolved:
                        relations.append(
                            RelationSpec(
                                field=f.name,
                                target_type=pascal_by_model[f.relation_target],
                                required=False,
                                description=f.label,
                            )
                        )
                        transforms.append(
                            FieldTransformSpec(
                                target_field=f.name,
                                source_field=f.name,
                                kind="foreign_key_id",
                                prefix=prefix_by_model[f.relation_target],
                                default=None,
                            )
                        )
                        used_names.add(f.name)
                        continue
                # Self-referential or out-of-subgraph many2one: extract clean ID string, never raw [id, name] list
                fields.append(
                    FieldSpec(
                        name=f.name,
                        field_type="str",
                        default="",
                        required=False,
                        description=f.label,
                    )
                )
                transforms.append(
                    FieldTransformSpec(
                        target_field=f.name,
                        source_field=f.name,
                        kind="foreign_key_id",
                        prefix=prefix_by_model.get(f.relation_target or "", ""),
                        default="",
                    )
                )
                used_names.add(f.name)
            else:
                ftype = _ttype_to_field_type(f.ttype)
                default_val = _default_for_field_type(ftype)
                is_non_neg = False
                if ftype in ("int", "float") and f.role in ("measure", "limit_or_policy_attr"):
                    observed_nums = [
                        r.get(f.name)
                        for r in sample_rows
                        if isinstance(r.get(f.name), (int, float)) and not isinstance(r.get(f.name), bool)
                    ]
                    if observed_nums and all(v >= 0 for v in observed_nums):
                        is_non_neg = True
                fields.append(
                    FieldSpec(
                        name=f.name,
                        field_type=ftype,  # type: ignore[arg-type]
                        default=default_val,
                        required=False,
                        non_negative=is_non_neg,
                        description=f.label,
                    )
                )
                transforms.append(
                    FieldTransformSpec(
                        target_field=f.name,
                        source_field=f.name,
                        kind="direct",
                        default=default_val,
                    )
                )
                used_names.add(f.name)

        # Ensure Probe 3's probed status_field is always included even if not in top-k
        probed_status_fields = {
            o.status_field for o in probe_report.observations if o.model == dm.model and o.status_field
        }
        for sf_name in sorted(probed_status_fields):
            if sf_name not in used_names:
                fields.append(FieldSpec(name=sf_name, field_type="str", default=""))
                transforms.append(FieldTransformSpec(target_field=sf_name, source_field=sf_name, kind="direct", default=""))
                used_names.add(sf_name)

        # Build FSM transitions strictly from Probe 2 mined traces + Probe 3 proven transitions
        sm = sm_by_model.get(dm.model)
        status_field = sm.field_name if sm is not None else (next(iter(probed_status_fields), "status"))
        transitions_map: dict[str, set[str]] = defaultdict(set)
        if sm is not None:
            for edge in sm.edges:
                transitions_map[edge.from_state].add(edge.to_state)
        for obs in probe_report.causal_transitions():
            if obs.model == dm.model and obs.status_field == status_field and obs.state_before != obs.state_after:
                transitions_map[obs.state_before].add(obs.state_after)

        title_field = "name" if "name" in used_names else "record_id"
        chosen_archetype = (
            dm.suggested_archetype
            if dm.suggested_archetype in allowed_archetypes
            else active_seed.default_archetype
        )
        object_types.append(
            ObjectTypeBlueprint(
                name=ot_name,
                archetype=chosen_archetype,
                primary_key="record_id",
                title_field=title_field,
                fields=fields,
                relations=relations,
                status_field=status_field,
                initial_statuses=None,
                transitions={k: sorted(v) for k, v in sorted(transitions_map.items())},
                display_label=dm.label,
            )
        )
        entity_mappings.append(
            EntityMappingSpec(
                source_table=dm.model,
                target_object_type=ot_name,
                id_transform=FieldTransformSpec(
                    target_field="record_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix=prefix,
                ),
                field_transforms=transforms,
                drop_if_missing_refs=drop_missing_refs,
            )
        )

    # Synthesize governed Actions + ActionExecutionBindings purely from Probe 3 causal observations
    grouped_transitions: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for obs in probe_report.causal_transitions():
        if obs.model not in pascal_by_model:
            continue
        if obs.state_before == obs.state_after:
            continue
        grouped_transitions[(obs.model, obs.method, obs.status_field, obs.state_after)].add(
            obs.state_before
        )

    # Check if a (model, method) maps to multiple distinct state_after values
    after_states_by_mm: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m, meth, _sf, sa in grouped_transitions:
        after_states_by_mm[(m, meth)].add(sa)

    actions: list[ActionBlueprint] = []
    action_bindings: list[ActionExecutionBinding] = []
    reversal_action_names: list[str] = []

    for (m, meth, sf, sa), valid_befores in sorted(grouped_transitions.items()):
        ot_name = pascal_by_model[m]
        snake_m = _model_to_snake(m)
        if len(after_states_by_mm[(m, meth)]) == 1:
            act_name = f"{snake_m}__{meth}"
        else:
            act_name = f"{snake_m}__{meth}_to_{_model_to_snake(sa)}"

        # Check if any edge of this action is flagged as a reversal/cancellation gray zone by Probe 2
        rev_pairs = reversal_transitions_by_model.get(m, set())
        is_reversal = any((sb, sa) in rev_pairs for sb in valid_befores) or any(
            kw in meth.lower() or kw in sa.lower() for kw in ("cancel", "reverse", "draft")
        )
        approval_rule: dict[str, Any] | None = None
        if is_reversal:
            reversal_action_names.append(act_name)
            approval_rule = {
                "op": "ne",
                "left": {"ref": "actor.role"},
                "right": {"lit": "steward"},
            }

        code = f"INVALID_STATE_FOR_{meth.upper()}"
        actions.append(
            ActionBlueprint(
                name=act_name,
                description=(
                    f"Autonomously induced action from Probe 3: {m}.{meth} "
                    f"({sorted(valid_befores)} -> {sa})."
                ),
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                approver_roles=["steward", "admin"],
                approval_rule=approval_rule,
                affects_source=True,
                params=[FieldSpec(name="record_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name=f"{act_name}_valid_state",
                        code=code,
                        target_type=ot_name,
                        id_param="record_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": f"obj.{sf}"},
                            "right": {"lit": sorted(valid_befores)},
                        },
                        description=f"{ot_name}.{sf} must be in {sorted(valid_befores)} for {act_name}",
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type=ot_name,
                        id_param="record_id",
                        set_fields={sf: {"lit": sa}},
                    )
                ],
                display_label=f"{dm_by_name[m].label}: {meth}",
            )
        )
        action_bindings.append(
            ActionExecutionBinding(
                action_name=act_name,
                external_table=m,
                external_method=meth,
                target_object_type=ot_name,
                id_param="record_id",
                id_prefix=prefix_by_model[m],
                expected_state_field=sf,
                expected_state_after=sa,
                readback_tables=[m],
            )
        )

    blueprint = DeclarativeBlueprint(
        model_id=model_id,
        version=version,
        seed_ontology_id=active_seed.seed_id,
        description=(
            f"Autonomously induced M1 ontology from {scout_report.source_locator} "
            f"grounded on seed {active_seed.seed_id!r} (Probe 1+2+3, zero hardcoded tables/methods)."
        ),
        object_types=object_types,
        actions=actions,
        invariants=[],
    )
    adapter_mapping = AdapterMappingSpec(
        mapping_id=f"{model_id}_bidirectional_v1",
        source_kind="discovered_erp",
        treat_false_as_null=True,
        entity_mappings=entity_mappings,
        action_bindings=action_bindings,
    )
    return SynthesizedBundle(
        blueprint=blueprint,
        adapter_mapping=adapter_mapping,
        action_probe_evidence=tuple(probe_report.observations),
        reversal_actions=tuple(reversal_action_names),
    )


def build_auto_replay_factory(
    bundle: SynthesizedBundle,
) -> Any:
    """Derive Gate 3 positive & negative ReplayEvents purely from Probe 3 causal + native-error evidence."""
    binding_by_mm: dict[tuple[str, str], ActionExecutionBinding] = {}
    for b in bundle.adapter_mapping.action_bindings:
        binding_by_mm[(b.external_table, b.external_method)] = b
    reversal_set = set(bundle.reversal_actions)
    valid_states_by_action: dict[str, set[str]] = {}
    for act in bundle.blueprint.actions:
        for pre in act.preconditions:
            right = pre.must_hold.get("right", {})
            if isinstance(right, dict) and isinstance(right.get("lit"), list):
                valid_states_by_action[act.name] = set(right["lit"])

    def _factory(projected_m0: list[dict[str, Any]]) -> tuple[list[ReplayEvent], list[ReplayEvent]]:
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in projected_m0:
            by_type[row["object_type"]].append(row["data"])

        pos: list[ReplayEvent] = []
        neg: list[ReplayEvent] = []
        seen_pos: set[tuple[str, str]] = set()
        seen_neg: set[tuple[str, str]] = set()

        for obs in bundle.action_probe_evidence:
            binding = binding_by_mm.get((obs.model, obs.method))
            if binding is None:
                continue
            candidates = by_type.get(binding.target_object_type, [])
            if obs.outcome == "TRANSITION" and obs.state_before != obs.state_after:
                key = (binding.action_name, obs.state_before)
                if key in seen_pos:
                    continue
                matched = next(
                    (c for c in candidates if str(c.get(obs.status_field)) == obs.state_before),
                    None,
                )
                if matched is not None:
                    seen_pos.add(key)
                    pos.append(
                        ReplayEvent(
                            name=f"auto_pos_{binding.action_name}_from_{obs.state_before}",
                            action=binding.action_name,
                            params={binding.id_param: matched["record_id"]},
                            actor=AI_OPS_ACTOR,
                            expected_valid=True,
                            expected_requires_approval=(binding.action_name in reversal_set),
                        )
                    )
            elif obs.outcome == "NATIVE_ERROR":
                allowed = valid_states_by_action.get(binding.action_name, set())
                if obs.state_before in allowed:
                    continue
                key = (binding.action_name, obs.state_before)
                if key in seen_neg:
                    continue
                matched = next(
                    (c for c in candidates if str(c.get(obs.status_field)) == obs.state_before),
                    None,
                )
                if matched is not None:
                    seen_neg.add(key)
                    neg.append(
                        ReplayEvent(
                            name=f"auto_neg_{binding.action_name}_from_{obs.state_before}",
                            action=binding.action_name,
                            params={binding.id_param: matched["record_id"]},
                            actor=AI_OPS_ACTOR,
                            expected_valid=False,
                            expected_error_code=f"INVALID_STATE_FOR_{obs.method.upper()}",
                        )
                    )
        return pos, neg

    return _factory


# ---------------------------------------------------------------------------
# 3. Step 2: Generic Bidirectional Odoo Bridge + Homomorphic Settlement
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HomomorphicExecutionResult:
    run_id: str
    action_name: str
    record_id: str
    external_table: str
    external_method: str
    external_id: int
    staged_status: str
    settled_status: str
    homomorphic_match: bool
    error_code: str | None
    expected_state_after: str
    observed_state_after: str | None


class OdooProtocolStep(BaseModel):
    """Odoo-specific PSM multi-step execution / transient wizard protocol step."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[
        "call_method",
        "prepare_child_quantities",
        "call_and_follow_transient",
        "create_and_call_transient",
        "post_created_document",
    ]
    external_table: str = ""
    method: str = ""
    transient_method: str = ""
    fallback_method: str = ""
    parent_key_field: str = ""
    source_qty_field: str = ""
    target_qty_field: str = ""
    flag_field: str = ""
    context_active_table: str = ""
    payload_template: dict[str, Any] = Field(default_factory=dict)


class DeclarativeOdooBridge:
    """Generic bidirectional runtime bridge driven purely by crystallized AdapterMappingSpec."""

    def __init__(
        self,
        *,
        memory_runtime: OntologyRuntime,
        adapter_mapping: AdapterMappingSpec,
        sandbox: ProbeSandboxInterface,
        protocol_overrides: Mapping[str, Sequence[OdooProtocolStep]] | None = None,
        lookup_index: Mapping[tuple[str, str, str], str] | None = None,
    ) -> None:
        self.memory_runtime = memory_runtime
        self.adapter_mapping = adapter_mapping
        self.sandbox = sandbox
        self.protocol_overrides: dict[str, list[OdooProtocolStep]] = {
            k: list(v) for k, v in (protocol_overrides or {}).items()
        }
        self.lookup_index: Mapping[tuple[str, str, str], str] | None = lookup_index
        self._last_observed_by_run: dict[str, dict[str, Any]] = {}
        self._prev_world_mode = memory_runtime.world_mode
        self._prev_source_bridge = memory_runtime.source_bridge
        self._default_bridge = self.make_source_bridge()
        self.memory_runtime.bind_source_bridge(self._default_bridge, world_mode="coupled")

    def close(self) -> None:
        self.memory_runtime.bind_source_bridge(
            self._prev_source_bridge,
            world_mode=self._prev_world_mode,
        )

    def __enter__(self) -> "DeclarativeOdooBridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def make_source_bridge(
        self,
        *,
        skip_external_call_to_simulate_drift: bool = False,
    ) -> SourceBridge:
        def _resolve_target(move: dict[str, Any]) -> tuple[ActionExecutionBinding, int, dict[str, Any]]:
            action_name = str(move["action_type"])
            params = dict(move.get("params") or {})
            binding = self.adapter_mapping.get_action_binding(action_name)
            if binding is None:
                raise ValueError(f"no ActionExecutionBinding registered for {action_name!r}")
            canonical_id = str(params[binding.id_param])
            ext_id = _parse_ext_id(canonical_id, binding.id_prefix)
            return binding, ext_id, params

        def _actuate(move: dict[str, Any]) -> Any:
            if skip_external_call_to_simulate_drift:
                return None
            binding, ext_id, params = _resolve_target(move)
            return self.execute_external_binding(binding, ext_id, params)

        def _observe(move: dict[str, Any]) -> dict[str, Any]:
            binding, ext_id, _params = _resolve_target(move)
            em = self.adapter_mapping.get_entity_mapping(binding.target_object_type)
            if em is None:
                raise ValueError(f"no EntityMappingSpec registered for {binding.target_object_type!r}")
            read_fields = list(
                dict.fromkeys(
                    ["id", em.id_transform.source_field]
                    + [tf.source_field for tf in em.field_transforms if tf.source_field]
                    + [fb for tf in em.field_transforms for fb in tf.fallback_fields if fb]
                    + [flt.field for flt in em.row_filters if flt.field]
                )
            )
            raw_rows = self.sandbox.search_read(
                binding.external_table,
                [("id", "=", ext_id)],
                read_fields,
                limit=1,
            )
            if not raw_rows:
                raise RuntimeError(
                    f"independent readback could not find {binding.external_table}:{ext_id}"
                )
            projected_row = project_single_record(
                em,
                raw_rows[0],
                lookup_index=self.lookup_index,
                treat_false_as_null=True,
            )
            run_id = str(move.get("run_id") or "")
            if run_id:
                self._last_observed_by_run[run_id] = dict(projected_row)
            return {binding.target_object_type: [projected_row]}

        return SourceBridge(actuate=_actuate, observe=_observe)

    def get_last_observed(self, run_id: str) -> dict[str, Any] | None:
        row = self._last_observed_by_run.get(run_id)
        return dict(row) if row is not None else None

    def execute_external_binding(
        self,
        binding: ActionExecutionBinding,
        ext_id: int,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute either simple external method or Odoo PSM OdooProtocolStep sequence."""
        steps = self.protocol_overrides.get(binding.action_name, [])
        if not steps:
            return self.sandbox.call_method(binding.external_table, binding.external_method, [ext_id])

        rpc_client = getattr(self.sandbox, "_client", None)
        if rpc_client is None and hasattr(self.sandbox, "execute_kw"):
            rpc_client = self.sandbox
        if rpc_client is None:
            raise RuntimeError(
                f"action {binding.action_name!r} uses OdooProtocolStep overrides requiring RPC client"
            )

        action_params = dict(params or {})
        m2o_cache: dict[str, Any] | None = None

        def _read_target_record() -> dict[str, Any]:
            nonlocal m2o_cache
            if m2o_cache is None:
                rows = self.sandbox.search_read(
                    binding.external_table,
                    [("id", "=", ext_id)],
                    [],
                    limit=1,
                )
                m2o_cache = dict(rows[0]) if rows else {}
            return m2o_cache

        def _resolve_val(val: Any) -> Any:
            if isinstance(val, str):
                if val == "$ext_id":
                    return ext_id
                if val == "$m2m_ext_id":
                    return [(6, 0, [ext_id])]
                if val == "$today":
                    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()
                if val.startswith("$param."):
                    pname = val[len("$param.") :]
                    return action_params.get(pname)
                if val.startswith("$record_m2o."):
                    fname = val[len("$record_m2o.") :]
                    raw = _read_target_record().get(fname)
                    if isinstance(raw, (list, tuple)) and raw:
                        return int(raw[0])
                    if isinstance(raw, int) and not isinstance(raw, bool):
                        return int(raw)
                    return None
                return val
            if isinstance(val, list):
                return [_resolve_val(x) for x in val]
            if isinstance(val, dict):
                return {k: _resolve_val(v) for k, v in val.items()}
            return val

        def _resolve_payload(tmpl: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for k, v in tmpl.items():
                resolved = _resolve_val(v)
                if resolved is not None:
                    out[k] = resolved
            return out

        last_result: Any = None
        for step in steps:
            if step.op == "call_method":
                tbl = step.external_table or binding.external_table
                last_result = self.sandbox.call_method(tbl, step.method, [ext_id])
            elif step.op == "prepare_child_quantities":
                read_cols = ["id", step.source_qty_field, step.target_qty_field]
                if step.flag_field and step.flag_field not in read_cols:
                    read_cols.append(step.flag_field)
                child_rows = self.sandbox.search_read(
                    step.external_table,
                    [(step.parent_key_field, "=", ext_id)],
                    read_cols,
                    limit=100,
                )
                for crow in child_rows:
                    cur_qty = crow.get(step.target_qty_field) or 0
                    vals: dict[str, Any] = {}
                    if not cur_qty:
                        vals[step.target_qty_field] = crow.get(step.source_qty_field)
                    if step.flag_field and (not cur_qty or not crow.get(step.flag_field)):
                        vals[step.flag_field] = True
                    if vals:
                        rpc_client.execute_kw(
                            step.external_table,
                            "write",
                            [[int(crow["id"])], vals],
                        )
            elif step.op == "call_and_follow_transient":
                tbl = step.external_table or binding.external_table
                res = self.sandbox.call_method(tbl, step.method, [ext_id])
                last_result = res
                if isinstance(res, dict) and res.get("res_model"):
                    wiz_model = str(res["res_model"])
                    ctx = dict(res.get("context") or {})
                    payload = _resolve_payload(step.payload_template)
                    wiz_id = int(rpc_client.execute_kw(wiz_model, "create", [payload], {"context": ctx}))
                    wiz_method = step.transient_method or step.fallback_method or step.method
                    last_result = rpc_client.execute_kw(
                        wiz_model, wiz_method, [[wiz_id]], {"context": ctx}
                    )
            elif step.op == "create_and_call_transient":
                active_tbl = step.context_active_table or binding.external_table
                ctx = {"active_model": active_tbl, "active_ids": [ext_id]}
                payload = _resolve_payload(step.payload_template)
                wiz_id = int(
                    rpc_client.execute_kw(step.external_table, "create", [payload], {"context": ctx})
                )
                primary_method = step.transient_method or step.method
                try:
                    last_result = rpc_client.execute_kw(
                        step.external_table, primary_method, [[wiz_id]], {"context": ctx}
                    )
                except Exception:
                    if step.fallback_method:
                        last_result = rpc_client.execute_kw(
                            step.external_table, step.fallback_method, [[wiz_id]], {"context": ctx}
                        )
                    else:
                        raise
            elif step.op == "post_created_document":
                doc_tbl = step.external_table or binding.external_table
                doc_ids: list[int] = []
                if isinstance(last_result, dict):
                    if last_result.get("res_id"):
                        doc_ids.append(int(last_result["res_id"]))
                    elif isinstance(last_result.get("domain"), list):
                        for cond in last_result["domain"]:
                            if isinstance(cond, (list, tuple)) and len(cond) == 3 and cond[0] == "id":
                                if cond[1] == "=" and isinstance(cond[2], int):
                                    doc_ids.append(int(cond[2]))
                                elif cond[1] == "in" and isinstance(cond[2], (list, tuple)):
                                    doc_ids.extend(int(x) for x in cond[2] if isinstance(x, int))
                if doc_ids:
                    if not step.method:
                        raise ValueError(
                            "OdooProtocolStep(op='post_created_document') requires explicit step.method"
                        )
                    drows = self.sandbox.search_read(
                        doc_tbl,
                        [("id", "in", doc_ids), ("state", "=", "draft")],
                        ["id", "state"],
                        limit=20,
                    )
                    for dr in drows:
                        self.sandbox.call_method(doc_tbl, step.method, [int(dr["id"])])
            else:
                raise ValueError(f"unsupported OdooProtocolStep op {step.op!r}")
        return last_result

    def execute_and_settle(
        self,
        *,
        run_id: str,
        action_name: str,
        params: dict[str, Any],
        actor: Actor = AI_OPS_ACTOR,
        approver: Actor | None = STEWARD_OPS_ACTOR,
        skip_external_call_to_simulate_drift: bool = False,
    ) -> HomomorphicExecutionResult:
        binding = self.adapter_mapping.get_action_binding(action_name)
        if binding is None:
            raise ValueError(f"no ActionExecutionBinding registered for {action_name!r}")
        em = self.adapter_mapping.get_entity_mapping(binding.target_object_type)
        if em is None:
            raise ValueError(f"no EntityMappingSpec registered for {binding.target_object_type!r}")

        canonical_id = str(params[binding.id_param])
        ext_id = _parse_ext_id(canonical_id, binding.id_prefix)
        staged_status = "PENDING_EXECUTION"

        if skip_external_call_to_simulate_drift:
            self.memory_runtime.bind_source_bridge(
                self.make_source_bridge(skip_external_call_to_simulate_drift=True),
                world_mode="coupled",
            )
        try:
            try:
                run = self.memory_runtime.submit_action(run_id, action_name, params, actor)
                staged_status = str(run["status"])
                if staged_status == "PENDING_APPROVAL":
                    if approver is None:
                        raise RuntimeError(
                            f"move {run_id!r} requires approval and no approver was supplied"
                        )
                    run = self.memory_runtime.approve_action(run_id, approver)
                settled_status = str(run["status"])
                error_code = run.get("error_code")
                projected_row = self.get_last_observed(run_id) or {}
            except DeviceStateMismatchError as exc:
                settled_status = "REJECTED"
                error_code = exc.code or DEVICE_STATE_MISMATCH
                obs_rows = (exc.readback_objects or {}).get(binding.target_object_type) or []
                projected_row = dict(obs_rows[0]) if obs_rows else (self.get_last_observed(run_id) or {})
        finally:
            if skip_external_call_to_simulate_drift:
                self.memory_runtime.bind_source_bridge(
                    self._default_bridge,
                    world_mode="coupled",
                )

        observed_after = (
            str(projected_row.get(binding.expected_state_field))
            if binding.expected_state_field and binding.expected_state_field in projected_row
            else None
        )
        return HomomorphicExecutionResult(
            run_id=run_id,
            action_name=action_name,
            record_id=canonical_id,
            external_table=binding.external_table,
            external_method=binding.external_method,
            external_id=ext_id,
            staged_status=staged_status,
            settled_status=settled_status,
            homomorphic_match=(settled_status == "COMMITTED"),
            error_code=error_code,
            expected_state_after=binding.expected_state_after,
            observed_state_after=observed_after,
        )


# ---------------------------------------------------------------------------
# 4. Full Unified Pipeline (Probe 1+2 -> Probe 3 -> Builder + 5-Gate Crucible -> Bidirectional Coupled Settlement)
# ---------------------------------------------------------------------------
def _select_synthesizer_backend(bundle: SynthesizedBundle, *, use_live_llm: bool) -> tuple[AbductionBackend, str]:
    if use_live_llm:
        if os.environ.get("GEMINI_API_KEY"):
            return GeminiAbductionBackend(), "gemini"
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAICompatibleAbductionBackend(), "openai-compatible"

    def _deterministic_fde_synthesis(_sys: str, _user: str, _round: int) -> dict[str, Any]:
        return {
            "rationale": "Autonomously synthesized from Probe 1+2+3 FDE evidence (zero hardcoded scripts).",
            "blueprint": bundle.blueprint.model_dump(mode="json"),
            "adapter_mapping": bundle.adapter_mapping.model_dump(mode="json"),
            "proposed_eval_cases": [],
        }

    return CallableAbductionBackend(_deterministic_fde_synthesis), "fde-evidence-synthesizer"


def _refresh_profile_from_sandbox(
    base_profile: ExternalPhenomenonProfile,
    sandbox_client: ProbeSandboxInterface,
) -> ExternalPhenomenonProfile:
    """Re-read current field values of sampled IDs from sandbox so memory_rt matches post-Probe-3 state."""
    refreshed: list[ExternalEntityProfile] = []
    for ent in base_profile.entities:
        ids = [r["id"] for r in ent.sample_rows if isinstance(r.get("id"), int)]
        fnames = list(dict.fromkeys(["id", *[f.name for f in ent.fields]]))
        if ids:
            try:
                live_rows = sandbox_client.search_read(
                    ent.table_name,
                    [("id", "in", ids)],
                    fnames,
                    limit=max(len(ids), 200),
                )
            except Exception:
                live_rows = list(ent.sample_rows)
        else:
            live_rows = list(ent.sample_rows)
        refreshed.append(
            ExternalEntityProfile(
                table_name=ent.table_name,
                row_count=ent.row_count,
                fields=list(ent.fields),
                sample_rows=live_rows,
            )
        )
    return ExternalPhenomenonProfile(
        source_kind=base_profile.source_kind,
        source_locator=base_profile.source_locator,
        entities=refreshed,
    )


def run_autonomous_fde_end_to_end(
    *,
    ro_client: ReadOnlyOdooInterface,
    sandbox_client: ProbeSandboxInterface,
    source_locator: str,
    sandbox_db: str,
    seed_ontology: SeedOntology | None = None,
    model_id: str = "autonomous_erp_ontology",
    task_intent: str = "",
    max_models: int = 6,
    use_live_llm: bool = False,
) -> dict[str, Any]:
    """Run the complete Step 1 + Step 2 pipeline grounded on an explicit pluggable SeedOntology."""
    active_seed = seed_ontology or build_business_seed_ontology()
    # --- Probe 1 + Probe 2 (Read-only discovery) ---
    scout_report = OdooFDEScout(
        ro_client,
        source_locator=source_locator,
        max_models=max_models,
    ).scout(task_intent=task_intent)
    profile = build_closed_phenomenon_profile(scout_report, client=ro_client)

    # --- Probe 3 (Active black-box interrogation on isolated sandbox) ---
    probe_report = OdooActiveProber(sandbox_client, sandbox_db=sandbox_db).probe(scout_report)

    # --- Step 1: Autonomous Synthesis -> OntologyBuilderAgent -> 5-Gate Crucible -> Crystallize ---
    bundle = synthesize_from_fde_evidence(
        scout_report,
        probe_report,
        profile=profile,
        seed_ontology=active_seed,
        model_id=model_id,
    )
    backend, synthesis_mode = _select_synthesizer_backend(bundle, use_live_llm=use_live_llm)

    with InductionGovernanceService(db_path=":memory:") as governance:
        builder = OntologyBuilderAgent(
            backend=backend,
            governance=governance,
            crucible=SymbolicCrucible(),
            seed_ontology=active_seed,
        )
        crucible_ctx = CrucibleContext(
            mode="genesis",
            replay_factory=build_auto_replay_factory(bundle),
            default_actor=AI_OPS_ACTOR,
            seed_ontology=active_seed,
        )
        cycle: InductionCycleResult = builder.run_genesis_cycle(
            model_id=model_id,
            phenomenon_brief={
                "scout_summary": scout_report.to_dict()["compression_summary"],
                "mined_state_machines": scout_report.to_dict()["mined_state_machines"],
                "probe_summary": probe_report.to_dict()["summary"],
            },
            external_profile=profile,
            crucible_context=crucible_ctx,
            seed_ontology=active_seed,
            max_rounds=3,
        )
        assert cycle.verified and cycle.pending_approval_run_id is not None, (
            f"Builder failed Crucible:\n{cycle.final_report.diagnostic_feedback()}"
        )
        governance.approve_crystallization(cycle.pending_approval_run_id, STEWARD_ACTOR)

        active_bp = governance.get_active_blueprint(model_id)
        active_mapping = governance.get_active_adapter_mapping(model_id)
        assert active_bp is not None and active_mapping is not None

        # --- Step 2: Compile crystallized M1 (extending active_seed) + Bidirectional Execution ---
        compiled_spec = compile_blueprint(active_bp, seed_ontology=active_seed)
        memory_rt = OntologyRuntime(compiled_spec, db_path=":memory:")
        try:
            # Refresh current state of sandbox records after Probe 3 into memory_rt
            fresh_profile = _refresh_profile_from_sandbox(profile, sandbox_client)
            projected_m0, proj_errors = project_external_phenomenon(fresh_profile, active_mapping)
            assert proj_errors == [], f"unexpected projection errors: {proj_errors}"
            order_rank = {name: i for i, name in enumerate(compiled_spec.object_types)}
            for entry in sorted(projected_m0, key=lambda e: order_rank.get(e["object_type"], 999)):
                cls = compiled_spec.get_object_type(entry["object_type"])
                memory_rt.put_object(cls(**entry["data"]))

            homomorphic_results: list[HomomorphicExecutionResult] = []
            drift_result: HomomorphicExecutionResult | None = None

            with DeclarativeOdooBridge(
                memory_runtime=memory_rt,
                adapter_mapping=active_mapping,
                sandbox=sandbox_client,
            ) as bridge:
                # Pick up to 2 forward actions with available live records on the sandbox
                def _valid_befores_for(act_def: ActionBlueprint) -> set[str]:
                    valid: set[str] = set()
                    for pre in act_def.preconditions:
                        r_lit = pre.must_hold.get("right", {}).get("lit")
                        if isinstance(r_lit, list):
                            valid.update(str(x) for x in r_lit)
                    return valid

                used_records: set[tuple[str, str]] = set()
                # Pass 1: one per distinct clean forward binding (prefers cross-model diversity)
                for binding in active_mapping.action_bindings:
                    if len(homomorphic_results) >= 2:
                        break
                    act_def = next(a for a in active_bp.actions if a.name == binding.action_name)
                    if act_def.approval_rule is not None:
                        continue  # prefer clean forward actions first
                    valid_befores = _valid_befores_for(act_def)
                    cls = compiled_spec.get_object_type(binding.target_object_type)
                    target_obj = next(
                        (
                            o
                            for o in memory_rt.query_objects(cls, AI_OPS_ACTOR)
                            if str(getattr(o, binding.expected_state_field, "")) in valid_befores
                            and (binding.target_object_type, o.primary_key_value()) not in used_records
                        ),
                        None,
                    )
                    if target_obj is None:
                        continue
                    used_records.add((binding.target_object_type, target_obj.primary_key_value()))
                    res = bridge.execute_and_settle(
                        run_id=f"HOMO-{len(homomorphic_results) + 1}",
                        action_name=binding.action_name,
                        params={binding.id_param: target_obj.primary_key_value()},
                        actor=AI_OPS_ACTOR,
                    )
                    homomorphic_results.append(res)

                # Pass 2: if fewer than 2 ran, top up from remaining unused clean candidates
                # while keeping >= 1 unused candidate reserved for the drift check below.
                while len(homomorphic_results) < 2:
                    unused_keys: set[tuple[str, str]] = set()
                    next_pick: tuple[ActionExecutionBinding, Any] | None = None
                    for binding in active_mapping.action_bindings:
                        act_def = next(a for a in active_bp.actions if a.name == binding.action_name)
                        if act_def.approval_rule is not None:
                            continue
                        valid_befores = _valid_befores_for(act_def)
                        cls = compiled_spec.get_object_type(binding.target_object_type)
                        for o in memory_rt.query_objects(cls, AI_OPS_ACTOR):
                            key = (binding.target_object_type, o.primary_key_value())
                            if (
                                str(getattr(o, binding.expected_state_field, "")) in valid_befores
                                and key not in used_records
                            ):
                                unused_keys.add(key)
                                if next_pick is None:
                                    next_pick = (binding, o)
                    if len(unused_keys) < 2 or next_pick is None:
                        break
                    binding, target_obj = next_pick
                    used_records.add((binding.target_object_type, target_obj.primary_key_value()))
                    res = bridge.execute_and_settle(
                        run_id=f"HOMO-{len(homomorphic_results) + 1}",
                        action_name=binding.action_name,
                        params={binding.id_param: target_obj.primary_key_value()},
                        actor=AI_OPS_ACTOR,
                    )
                    homomorphic_results.append(res)

                # Also prove DEVICE_STATE_MISMATCH protection on a live record when external drifts
                for binding in active_mapping.action_bindings:
                    act_def = next(a for a in active_bp.actions if a.name == binding.action_name)
                    if act_def.approval_rule is not None:
                        continue
                    valid_befores = _valid_befores_for(act_def)
                    cls = compiled_spec.get_object_type(binding.target_object_type)
                    objs = memory_rt.query_objects(cls, AI_OPS_ACTOR)
                    drift_obj = next(
                        (
                            o
                            for o in objs
                            if str(getattr(o, binding.expected_state_field, "")) in valid_befores
                            and (binding.target_object_type, o.primary_key_value()) not in used_records
                        ),
                        None,
                    )
                    if drift_obj is not None:
                        fp_before_drift = memory_rt.state_fingerprint()
                        drift_result = bridge.execute_and_settle(
                            run_id="HOMO-DRIFT-CHECK",
                            action_name=binding.action_name,
                            params={binding.id_param: drift_obj.primary_key_value()},
                            actor=AI_OPS_ACTOR,
                            skip_external_call_to_simulate_drift=True,
                        )
                        fp_after_drift = memory_rt.state_fingerprint()
                        assert fp_after_drift["versions"] == fp_before_drift["versions"]
                        assert fp_after_drift["objects"] == fp_before_drift["objects"]
                        assert fp_after_drift["links"] == fp_before_drift["links"]
                        assert fp_after_drift["overlay"] == fp_before_drift["overlay"]
                        assert fp_after_drift["runs"] == fp_before_drift["runs"] + 1
                        break

            return {
                "source_locator": source_locator,
                "sandbox_db": sandbox_db,
                "synthesis_mode": synthesis_mode,
                "probe_1_2_compression": scout_report.to_dict()["compression_summary"],
                "probe_3_summary": probe_report.to_dict()["summary"],
                "crystallized_ontology": {
                    "model_id": active_bp.model_id,
                    "version": active_bp.version,
                    "seed_ontology_id": active_bp.seed_ontology_id,
                    "inherited_abstract_types": sorted(compiled_spec.abstract_types.keys()),
                    "object_types": [ot.name for ot in active_bp.object_types],
                    "object_superclasses": {
                        name: cls.ontology_superclasses()
                        for name, cls in compiled_spec.object_types.items()
                    },
                    "actions": [a.name for a in active_bp.actions],
                    "action_bindings": [
                        f"{b.action_name} -> {b.external_table}.{b.external_method}"
                        for b in active_mapping.action_bindings
                    ],
                    "crucible_gates_passed": all(g.passed for g in cycle.final_report.gates),
                    "gate_3_metrics": next(
                        g.metrics
                        for g in cycle.final_report.gates
                        if g.gate == "gate_3_counterexample_elimination"
                    ),
                },
                "homomorphic_settlement_runs": [
                    {
                        "run_id": r.run_id,
                        "action_name": r.action_name,
                        "record_id": r.record_id,
                        "external_call": f"{r.external_table}.{r.external_method}([{r.external_id}])",
                        "settled_status": r.settled_status,
                        "homomorphic_match": r.homomorphic_match,
                        "expected_state_after": r.expected_state_after,
                        "observed_state_after": r.observed_state_after,
                    }
                    for r in homomorphic_results
                ],
                "drift_protection_run": (
                    {
                        "run_id": drift_result.run_id,
                        "action_name": drift_result.action_name,
                        "settled_status": drift_result.settled_status,
                        "error_code": drift_result.error_code,
                        "mismatch_caught": (
                            drift_result.settled_status == "REJECTED"
                            and drift_result.error_code == DEVICE_STATE_MISMATCH
                        ),
                    }
                    if drift_result is not None
                    else None
                ),
            }
        finally:
            memory_rt.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Unified Autonomous FDE (Probe 1+2+3 -> Zero-Script Builder -> Bidirectional Homomorphic Execution)"
    )
    parser.add_argument("--db", default=DB_NORTH, help=f"Source Odoo DB (default: {DB_NORTH})")
    parser.add_argument("--intent", default="", help="Optional task intent")
    parser.add_argument("--live-llm", action="store_true", help="Use live Gemini/OpenAI backend if key set")
    args = parser.parse_args(argv)

    ro_client = ReadOnlyOdooClient(db=args.db)
    with EphemeralOdooSandbox(source_db=args.db) as sandbox_client:
        out = run_autonomous_fde_end_to_end(
            ro_client=ro_client,
            sandbox_client=sandbox_client,
            source_locator=f"odoo://{args.db}",
            sandbox_db=sandbox_client.db,
            task_intent=args.intent,
            use_live_llm=args.live_llm,
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
