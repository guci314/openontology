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
    InvariantError,
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
    RowFilterSpec,
    STEWARD_ACTOR,
    SeedOntology,
    SymbolicCrucible,
    compile_blueprint,
    project_external_phenomenon,
    project_single_record,
)

from .business_ontology import build_business_seed_ontology, settle_work_card
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
_NON_SINGULAR_WORDS = frozenset({"sales", "status", "ops", "goods", "series", "species"})
_UI_NOISE_SUBSTRINGS = (
    "image",
    "avatar",
    "zoom",
    "kanban",
    "ribbon",
    "tooltip",
    "color_index",
    "website_",
    "portal_",
    "my_activity",
    "activity_",
)
_UI_NOISE_EXACT = frozenset({"color", "access_token", "icon", "sequence", "parent_path"})


def _singularize_word(word: str) -> str:
    lw = word.lower()
    if lw in _NON_SINGULAR_WORDS or len(word) <= 3:
        return word
    if lw.endswith("ies") and len(word) > 4:
        return word[:-3] + ("y" if word[-4].islower() else "Y")
    if lw.endswith(("ses", "xes", "ches", "shes")) and len(word) > 4:
        return word[:-2]
    if lw.endswith("s") and not lw.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _label_to_pascal(label: str, fallback_model: str) -> str:
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", label or "") if t]
    if not tokens:
        tokens = [t for t in re.split(r"[._-]+", fallback_model) if t]
    if not tokens:
        return "Entity"
    tokens[-1] = _singularize_word(tokens[-1])
    out = "".join(tok[:1].upper() + tok[1:] for tok in tokens)
    if out and out[0].isdigit():
        out = "Entity" + out
    return out or "Entity"


def _camel_to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", s2).strip("_").lower()
    return cleaned or "entity"


def _pascal_to_prefix(ot_name: str, used_prefixes: set[str]) -> str:
    base = _camel_to_snake(ot_name).upper()
    cand = f"{base}-"
    idx = 2
    while cand in used_prefixes:
        cand = f"{base}_{idx}-"
        idx += 1
    used_prefixes.add(cand)
    return cand


def _clean_method_verb(method: str) -> str:
    verb = re.sub(r"^(?:action|button|do)_+", "", method.strip())
    return _model_to_snake(verb or method)


def _semantic_action_name(
    method: str,
    target_ot_name: str,
    state_after: str,
    multiple_dests: bool,
    used_names: set[str],
) -> str:
    verb = _clean_method_verb(method)
    ot_snake = _camel_to_snake(target_ot_name)
    if "_" in verb:
        base = verb
    else:
        base = f"{verb}_{ot_snake}"
    if multiple_dests:
        base = f"{base}_to_{_model_to_snake(state_after)}"
    cand = base
    idx = 2
    while cand in used_names:
        cand = f"{base}_{idx}"
        idx += 1
    used_names.add(cand)
    return cand


def _is_ui_presentation_noise_field(fname: str) -> bool:
    fl = fname.lower()
    if (
        fl in _UI_NOISE_EXACT
        or fl.endswith(("_html", "_icon", "_normalized"))
        or fl.startswith("property_")
    ):
        return True
    return any(tok in fl for tok in _UI_NOISE_SUBSTRINGS)


def _non_redundant_description(field_name: str, label: str) -> str:
    if not label:
        return ""
    return "" if _model_to_snake(label) == field_name.lower() else label


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


def _select_primary_polymorphic_split(
    dm: DiscoveredModel,
    polymorphics: Sequence[Any],
    sample_rows: Sequence[dict[str, Any]],
) -> tuple[Any | None, list[tuple[str, str]]]:
    """Select at most one structural ProcessDocument subtype discriminator + branches from metadata."""
    if dm.suggested_archetype != "ProcessDocument" or not sample_rows:
        return None, []
    min_branch_count = 3 if len(sample_rows) >= 20 else 2
    field_by_name = {f.name: f for f in dm.retained_fields}

    candidates: list[tuple[tuple[int, float], Any, list[tuple[str, str]]]] = []
    for poly in polymorphics:
        if poly.model != dm.model:
            continue
        f_sig = field_by_name.get(poly.field_name)
        label_map = dict(getattr(f_sig, "selection_labels", {}) or {}) if f_sig else {}
        branches: list[tuple[str, str]] = []
        for raw_val, cnt in sorted(poly.value_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if cnt < min_branch_count:
                continue
            human_lbl = label_map.get(raw_val) or raw_val.replace("_", " ").title()
            branches.append((raw_val, human_lbl))
        if len(branches) < 2:
            continue
        candidates.append(((len(poly.divergent_columns), float(poly.nullity_divergence_score)), poly, branches))

    if not candidates:
        return None, []
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, best_poly, best_branches = candidates[0]
    return best_poly, best_branches


def synthesize_from_fde_evidence(
    scout_report: FDEScoutReport,
    probe_report: ActiveProbeReport,
    *,
    profile: ExternalPhenomenonProfile,
    seed_ontology: SeedOntology | None = None,
    model_id: str = "discovered_erp_ontology",
    version: str = "1.0.0",
    apply_gray_zone_approval_rules: bool = False,
) -> SynthesizedBundle:
    """Autonomously synthesize semantically purified Blueprint + AdapterMappingSpec from FDE evidence."""
    active_seed = seed_ontology or build_business_seed_ontology()
    allowed_archetypes = set(active_seed.archetype_names())
    dm_by_name: dict[str, DiscoveredModel] = {dm.model: dm for dm in scout_report.discovered_models}
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

    # Plan semantic entity blueprints (preserving single entities & splitting polymorphic tables)
    @dataclass(frozen=True)
    class _EntityPlan:
        dm: DiscoveredModel
        ot_name: str
        prefix: str
        display_label: str
        row_filters: tuple[RowFilterSpec, ...]
        branch_rows: tuple[dict[str, Any], ...]
        discriminator_field: str | None
        is_split_branch: bool

    def _model_sort_key(dm: DiscoveredModel) -> tuple[int, str]:
        rank = 0 if dm.suggested_archetype == "MasterEntity" else (1 if dm.suggested_archetype == "ObjectType" else 2)
        return (rank, dm.model)

    ordered_dms = sorted(scout_report.discovered_models, key=_model_sort_key)
    used_ot_names: set[str] = set()
    used_prefixes: set[str] = set()
    entity_plans: list[_EntityPlan] = []
    plans_by_model: dict[str, list[_EntityPlan]] = defaultdict(list)

    for dm in ordered_dms:
        raw_rows = list(rows_by_table.get(dm.model, []))
        poly, split_branches = _select_primary_polymorphic_split(
            dm,
            scout_report.polymorphic_discriminators,
            raw_rows,
        )
        if poly is not None and split_branches:
            for disc_val, human_lbl in split_branches:
                base_pascal = _label_to_pascal(human_lbl, dm.model)
                ot_name = base_pascal
                idx = 2
                while ot_name in used_ot_names:
                    ot_name = f"{base_pascal}{idx}"
                    idx += 1
                used_ot_names.add(ot_name)
                prefix = _pascal_to_prefix(ot_name, used_prefixes)
                sub_rows = tuple(r for r in raw_rows if str(r.get(poly.field_name)) == disc_val)
                plan = _EntityPlan(
                    dm=dm,
                    ot_name=ot_name,
                    prefix=prefix,
                    display_label=human_lbl,
                    row_filters=(RowFilterSpec(field=poly.field_name, op="eq", value=disc_val),),
                    branch_rows=sub_rows,
                    discriminator_field=poly.field_name,
                    is_split_branch=True,
                )
                entity_plans.append(plan)
                plans_by_model[dm.model].append(plan)
        else:
            base_pascal = _label_to_pascal(dm.label, dm.model)
            ot_name = base_pascal
            idx = 2
            while ot_name in used_ot_names:
                ot_name = f"{base_pascal}{idx}"
                idx += 1
            used_ot_names.add(ot_name)
            prefix = _pascal_to_prefix(ot_name, used_prefixes)
            plan = _EntityPlan(
                dm=dm,
                ot_name=ot_name,
                prefix=prefix,
                display_label=dm.label or ot_name,
                row_filters=(),
                branch_rows=tuple(raw_rows),
                discriminator_field=None,
                is_split_branch=False,
            )
            entity_plans.append(plan)
            plans_by_model[dm.model].append(plan)

    single_target_plan_by_model: dict[str, _EntityPlan] = {
        mname: plist[0] for mname, plist in plans_by_model.items() if len(plist) == 1
    }

    # Pre-index inferred_string_join candidates per source model
    inferred_joins_by_model: dict[str, list[Any]] = defaultdict(list)
    for rel in scout_report.discovered_relations:
        if rel.kind == "inferred_string_join" and rel.target_model in single_target_plan_by_model:
            inferred_joins_by_model[rel.source_model].append(rel)

    object_types: list[ObjectTypeBlueprint] = []
    entity_mappings: list[EntityMappingSpec] = []
    explicit_rel_field_by_model_target: dict[tuple[str, str], str] = {}
    branch_rel_counts: dict[str, int] = {}

    for ep in entity_plans:
        dm = ep.dm
        ot_name = ep.ot_name
        prefix = ep.prefix
        sample_rows = list(ep.branch_rows)

        probed_status_fields = {
            o.status_field for o in probe_report.observations if o.model == dm.model and o.status_field
        }
        sm = sm_by_model.get(dm.model)
        status_field = sm.field_name if sm is not None else (next(iter(probed_status_fields), "status"))

        fields: list[FieldSpec] = [
            FieldSpec(name="record_id", field_type="str", primary_key=True, required=True)
        ]
        relations: list[RelationSpec] = []
        transforms: list[FieldTransformSpec] = []
        drop_missing_refs: list[str] = []
        used_names: set[str] = {"record_id"}
        explicit_targets_covered: dict[str, float] = {}

        # Detect which explicit many2one fields will be promoted cleanly
        retained_names = {f.name for f in dm.retained_fields}
        for f in dm.retained_fields:
            if f.name == "id" or f.name in used_names:
                continue
            if ep.discriminator_field and f.name == ep.discriminator_field:
                continue  # Constant discriminator value on this split branch
            if f.name not in probed_status_fields and f.name != status_field and _is_ui_presentation_noise_field(f.name):
                continue

            # For split branches, prune columns that are 100% null or 0-bit constant within this branch
            if ep.is_split_branch and f.name not in ("name", status_field) and f.name not in probed_status_fields and sample_rows:
                non_null_in_branch = sum(
                    1 for r in sample_rows if r.get(f.name) is not None and r.get(f.name) is not False and r.get(f.name) != ""
                )
                if non_null_in_branch == 0:
                    continue
                if (
                    len(sample_rows) >= 2
                    and f.ttype != "many2one"
                    and f.role not in ("measure", "limit_or_policy_attr")
                ):
                    distinct_vals = {
                        repr(None if (r.get(f.name) is False and f.ttype != "boolean") else r.get(f.name))
                        for r in sample_rows
                    }
                    if len(distinct_vals) <= 1:
                        continue

            if f.ttype == "many2one":
                if f.relation_target in single_target_plan_by_model and f.relation_target != dm.model:
                    tgt_plan = single_target_plan_by_model[f.relation_target]
                    tgt_ids = present_ids_by_model.get(f.relation_target, set())
                    has_unresolved = False
                    non_null_cnt = 0
                    for r in sample_rows:
                        raw = r.get(f.name)
                        pid = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
                        if isinstance(pid, int) and not isinstance(pid, bool):
                            non_null_cnt += 1
                            if pid not in tgt_ids:
                                has_unresolved = True
                                break
                    if not has_unresolved:
                        fill_rate = non_null_cnt / max(1, len(sample_rows))
                        relations.append(
                            RelationSpec(
                                field=f.name,
                                target_type=tgt_plan.ot_name,
                                required=False,
                                description=_non_redundant_description(f.name, f.label),
                            )
                        )
                        transforms.append(
                            FieldTransformSpec(
                                target_field=f.name,
                                source_field=f.name,
                                kind="foreign_key_id",
                                prefix=tgt_plan.prefix,
                                default=None,
                            )
                        )
                        used_names.add(f.name)
                        explicit_rel_field_by_model_target[(ot_name, f.relation_target)] = f.name
                        explicit_targets_covered[tgt_plan.ot_name] = max(
                            explicit_targets_covered.get(tgt_plan.ot_name, 0.0),
                            fill_rate,
                        )
                        continue
                # Self-referential or unresolved-target many2one: extract clean ID string
                tgt_plan_opt = single_target_plan_by_model.get(f.relation_target or "")
                fields.append(
                    FieldSpec(
                        name=f.name,
                        field_type="str",
                        default="",
                        required=False,
                        description=_non_redundant_description(f.name, f.label),
                    )
                )
                transforms.append(
                    FieldTransformSpec(
                        target_field=f.name,
                        source_field=f.name,
                        kind="foreign_key_id",
                        prefix=tgt_plan_opt.prefix if tgt_plan_opt is not None else "",
                        default="",
                    )
                )
                used_names.add(f.name)
                continue

            # If this char field is a discovered inferred_string_join, evaluate promotion after scalar loop
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
                    description=_non_redundant_description(f.name, f.label),
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

        def _drop_redundant_raw_join_field(src_field: str) -> None:
            nonlocal fields, transforms
            if (
                src_field in (status_field, "name", "record_id")
                or src_field in probed_status_fields
            ):
                return
            fields = [fl for fl in fields if fl.name != src_field]
            transforms = [
                tf
                for tf in transforms
                if not (tf.kind == "direct" and tf.target_field == src_field)
            ]
            used_names.discard(src_field)

        # Promote populated inferred_string_join fields -> formal ObjectProperty (RelationSpec + lookup_join)
        promoted_join_targets: set[str] = set()
        for rel in sorted(inferred_joins_by_model.get(dm.model, []), key=lambda r: (-r.confidence, r.source_field)):
            tgt_plan = single_target_plan_by_model.get(rel.target_model)
            if tgt_plan is None or tgt_plan.ot_name == ot_name:
                continue
            if tgt_plan.ot_name in promoted_join_targets:
                continue
            if sample_rows:
                non_null_join = sum(
                    1
                    for r in sample_rows
                    if r.get(rel.source_field) not in (None, False, "")
                )
                if non_null_join == 0:
                    continue
                # Avoid duplicate ObjectProperty when an explicit FK already covers the same target
                existing_cov = explicit_targets_covered.get(tgt_plan.ot_name, 0.0)
                if existing_cov > 0.0 and rel.target_model != dm.model:
                    _drop_redundant_raw_join_field(rel.source_field)
                    continue
            rel_field = _camel_to_snake(tgt_plan.ot_name)
            src_sig = next((f for f in dm.retained_fields if f.name == rel.source_field), None)
            can_drop_raw = (
                rel.source_field not in (status_field, "name", "record_id")
                and rel.source_field not in probed_status_fields
            )
            candidate_used = used_names - ({rel.source_field} if can_drop_raw else set())
            if rel_field in candidate_used:
                rel_field = f"linked_{rel_field}"
            if rel_field in candidate_used:
                continue
            _drop_redundant_raw_join_field(rel.source_field)
            relations.append(
                RelationSpec(
                    field=rel_field,
                    target_type=tgt_plan.ot_name,
                    required=False,
                    description=_non_redundant_description(
                        rel_field,
                        src_sig.label if src_sig is not None else "",
                    ),
                )
            )
            transforms.append(
                FieldTransformSpec(
                    target_field=rel_field,
                    source_field=rel.source_field,
                    kind="lookup_join",
                    lookup_table=rel.target_model,
                    lookup_key_field=rel.target_field,
                    default=None,
                )
            )
            used_names.add(rel_field)
            promoted_join_targets.add(tgt_plan.ot_name)

        # Ensure Probe 3's probed status_field is always included even if not in top-k
        for sf_name in sorted(probed_status_fields):
            if sf_name not in used_names:
                fields.append(FieldSpec(name=sf_name, field_type="str", default=""))
                transforms.append(FieldTransformSpec(target_field=sf_name, source_field=sf_name, kind="direct", default=""))
                used_names.add(sf_name)

        # Build FSM transitions strictly from Probe 2 mined traces + Probe 3 proven transitions
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
        branch_rel_counts[ot_name] = len(relations)
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
                display_label=ep.display_label,
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
                row_filters=list(ep.row_filters),
                field_transforms=transforms,
                drop_if_missing_refs=drop_missing_refs,
            )
        )

    # Index gray-zone cross-entity limit rules by model (optional at 0->1 or when enabled)
    limit_findings_by_model: dict[str, list[Any]] = defaultdict(list)
    if apply_gray_zone_approval_rules:
        for ef in scout_report.epistemic_findings:
            if ef.kind == "CROSS_ENTITY_LIMIT_EXCEEDANCE_CONTRADICTION":
                limit_findings_by_model[ef.model].append(ef)

    # Synthesize governed verb-noun Actions + ActionExecutionBindings purely from Probe 3 causal observations
    grouped_transitions: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for obs in probe_report.causal_transitions():
        if obs.model not in plans_by_model:
            continue
        if obs.state_before == obs.state_after:
            continue
        grouped_transitions[(obs.model, obs.method, obs.status_field, obs.state_after)].add(
            obs.state_before
        )

    after_states_by_mm: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m, meth, _sf, sa in grouped_transitions:
        after_states_by_mm[(m, meth)].add(sa)

    ot_by_name = {ot.name: ot for ot in object_types}

    def _pick_action_plan(model_name: str, status_fld: str, valid_befores: set[str]) -> _EntityPlan:
        plist = plans_by_model[model_name]
        if len(plist) == 1:
            return plist[0]
        # Pick split branch with probed pre-state rows, richest relations, and highest row support
        def _score(ep: _EntityPlan) -> tuple[int, int, int]:
            has_before = sum(1 for r in ep.branch_rows if str(r.get(status_fld)) in valid_befores)
            return (1 if has_before > 0 else 0, branch_rel_counts.get(ep.ot_name, 0), len(ep.branch_rows))

        return max(plist, key=_score)

    actions: list[ActionBlueprint] = []
    action_bindings: list[ActionExecutionBinding] = []
    reversal_action_names: list[str] = []
    used_action_names: set[str] = set()

    for (m, meth, sf, sa), valid_befores in sorted(grouped_transitions.items()):
        target_plan = _pick_action_plan(m, sf, valid_befores)
        ot_name = target_plan.ot_name
        multiple_dests = len(after_states_by_mm[(m, meth)]) > 1
        act_name = _semantic_action_name(meth, ot_name, sa, multiple_dests, used_action_names)

        code = f"INVALID_STATE_FOR_{meth.upper()}"
        act_preconditions: list[InvariantRuleSpec] = [
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
        ]

        # Check if any edge of this action is flagged as a reversal/cancellation gray zone by Probe 2
        rev_pairs = reversal_transitions_by_model.get(m, set())
        is_reversal = any((sb, sa) in rev_pairs for sb in valid_befores) or any(
            kw in meth.lower() or kw in sa.lower() for kw in ("cancel", "reverse", "draft")
        )
        if is_reversal:
            reversal_action_names.append(act_name)
            act_preconditions.append(
                InvariantRuleSpec(
                    name=f"{act_name}_reversal_requires_steward",
                    code=f"APPROVAL_REQUIRED_REVERSAL_{ot_name.upper()}",
                    target_type=ot_name,
                    id_param="record_id",
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["steward", "admin"]},
                    },
                    description=(
                        f"Reversal/cancellation on {ot_name} requires Steward or Admin approval."
                    ),
                )
            )
        elif apply_gray_zone_approval_rules and limit_findings_by_model.get(m):
            target_ot = ot_by_name[ot_name]
            target_fields = {f.name for f in target_ot.fields}
            target_rels = {r.field: r for r in target_ot.relations}
            for ef in limit_findings_by_model[m]:
                m_fld = str(ef.evidence.get("measure_field") or "")
                p_rel = str(ef.evidence.get("parent_relation") or "")
                p_lim_full = str(ef.evidence.get("parent_limit_field") or "")
                lim_fld = p_lim_full.split(".")[-1] if p_lim_full else ""
                if m_fld in target_fields and p_rel in target_rels and lim_fld:
                    rel_target_ot = ot_by_name.get(target_rels[p_rel].target_type)
                    if rel_target_ot is not None and any(f.name == lim_fld for f in rel_target_ot.fields):
                        act_preconditions.append(
                            InvariantRuleSpec(
                                name=f"{act_name}_limit_requires_steward",
                                code=f"APPROVAL_REQUIRED_{lim_fld.upper()}_EXCEEDED",
                                target_type=ot_name,
                                id_param="record_id",
                                when={
                                    "op": "and",
                                    "args": [
                                        {"op": "gt", "left": {"ref": f"obj.{p_rel}.{lim_fld}"}, "right": {"lit": 0}},
                                        {"op": "gt", "left": {"ref": f"obj.{m_fld}"}, "right": {"ref": f"obj.{p_rel}.{lim_fld}"}},
                                    ],
                                },
                                must_hold={
                                    "op": "in",
                                    "left": {"ref": "actor.role"},
                                    "right": {"lit": ["steward", "admin"]},
                                },
                                description=(
                                    f"{ot_name}.{m_fld} exceeding {p_rel}.{lim_fld} requires Steward/Admin approval."
                                ),
                            )
                        )
                        break

        actions.append(
            ActionBlueprint(
                name=act_name,
                description=(
                    f"Autonomously induced action from Probe 3: {target_plan.display_label} {meth} "
                    f"({sorted(valid_befores)} -> {sa})."
                ),
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                affects_source=True,
                params=[FieldSpec(name="record_id", field_type="str", required=True)],
                preconditions=act_preconditions,
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type=ot_name,
                        id_param="record_id",
                        set_fields={sf: {"lit": sa}},
                    )
                ],
                display_label=f"{target_plan.display_label}: {_clean_method_verb(meth)}",
            )
        )
        action_bindings.append(
            ActionExecutionBinding(
                action_name=act_name,
                external_table=m,
                external_method=meth,
                target_object_type=ot_name,
                id_param="record_id",
                id_prefix=target_plan.prefix,
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
    *,
    seed_ontology: SeedOntology | None = None,
) -> Any:
    """Derive Gate 3 positive & negative ReplayEvents purely from Probe 3 causal + native-error evidence."""
    binding_by_mm: dict[tuple[str, str], ActionExecutionBinding] = {}
    for b in bundle.adapter_mapping.action_bindings:
        binding_by_mm[(b.external_table, b.external_method)] = b
    reversal_set = set(bundle.reversal_actions)
    valid_states_by_action: dict[str, set[str]] = {}
    approval_actions: set[str] = set()
    for act in bundle.blueprint.actions:
        if any(pre.code.startswith("APPROVAL_REQUIRED_") for pre in act.preconditions):
            approval_actions.add(act.name)
        for pre in act.preconditions:
            right = pre.must_hold.get("right", {})
            if isinstance(right, dict) and isinstance(right.get("lit"), list):
                valid_states_by_action[act.name] = set(right["lit"])

    compiled_for_approval = (
        compile_blueprint(
            bundle.blueprint,
            seed_ontology=seed_ontology or build_business_seed_ontology(),
        )
        if approval_actions
        else None
    )

    def _factory(projected_m0: list[dict[str, Any]]) -> tuple[list[ReplayEvent], list[ReplayEvent]]:
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in projected_m0:
            by_type[row["object_type"]].append(row["data"])

        sim_rt: OntologyRuntime | None = None
        if compiled_for_approval is not None:
            sim_rt = OntologyRuntime(compiled_for_approval, db_path=":memory:")
            order_rank = {name: i for i, name in enumerate(compiled_for_approval.object_types)}
            for entry in sorted(projected_m0, key=lambda e: order_rank.get(e["object_type"], 999)):
                cls = compiled_for_approval.get_object_type(entry["object_type"])
                sim_rt.put_object(cls(**entry["data"]))

        pos: list[ReplayEvent] = []
        neg: list[ReplayEvent] = []
        seen_pos: set[tuple[str, str, str]] = set()
        seen_neg: set[tuple[str, str]] = set()

        try:
            for obs in bundle.action_probe_evidence:
                binding = binding_by_mm.get((obs.model, obs.method))
                if binding is None:
                    continue
                candidates = by_type.get(binding.target_object_type, [])
                if obs.outcome == "TRANSITION" and obs.state_before != obs.state_after:
                    state_matches = [
                        c for c in candidates if str(c.get(obs.status_field)) == obs.state_before
                    ]
                    if not state_matches:
                        continue
                    if sim_rt is not None and binding.action_name in approval_actions:
                        for cand in state_matches:
                            rec_id = cand["record_id"]
                            sim_ai = sim_rt.simulate_action(
                                binding.action_name,
                                {binding.id_param: rec_id},
                                actor=AI_OPS_ACTOR,
                            )
                            if sim_ai.valid:
                                key_ai = (binding.action_name, obs.state_before, AI_OPS_ACTOR.role)
                                if key_ai not in seen_pos:
                                    seen_pos.add(key_ai)
                                    pos.append(
                                        ReplayEvent(
                                            name=f"auto_pos_{binding.action_name}_from_{obs.state_before}",
                                            action=binding.action_name,
                                            params={binding.id_param: rec_id},
                                            actor=AI_OPS_ACTOR,
                                            expected_valid=True,
                                        )
                                    )
                            elif (sim_ai.error_code or "").startswith("APPROVAL_REQUIRED_"):
                                neg_key = (binding.action_name, f"{obs.state_before}:{sim_ai.error_code}")
                                if neg_key not in seen_neg:
                                    seen_neg.add(neg_key)
                                    neg.append(
                                        ReplayEvent(
                                            name=f"auto_neg_approval_gate_{binding.action_name}_from_{obs.state_before}",
                                            action=binding.action_name,
                                            params={binding.id_param: rec_id},
                                            actor=AI_OPS_ACTOR,
                                            expected_valid=False,
                                            expected_error_code=sim_ai.error_code,
                                        )
                                    )
                                sim_st = sim_rt.simulate_action(
                                    binding.action_name,
                                    {binding.id_param: rec_id},
                                    actor=STEWARD_OPS_ACTOR,
                                )
                                if sim_st.valid:
                                    key_st = (binding.action_name, obs.state_before, STEWARD_OPS_ACTOR.role)
                                    if key_st not in seen_pos:
                                        seen_pos.add(key_st)
                                        pos.append(
                                            ReplayEvent(
                                                name=f"auto_pos_steward_{binding.action_name}_from_{obs.state_before}",
                                                action=binding.action_name,
                                                params={binding.id_param: rec_id},
                                                actor=STEWARD_OPS_ACTOR,
                                                expected_valid=True,
                                            )
                                        )
                    else:
                        actor = STEWARD_OPS_ACTOR if binding.action_name in reversal_set else AI_OPS_ACTOR
                        key = (binding.action_name, obs.state_before, actor.role)
                        if key in seen_pos:
                            continue
                        seen_pos.add(key)
                        pos.append(
                            ReplayEvent(
                                name=f"auto_pos_{binding.action_name}_from_{obs.state_before}",
                                action=binding.action_name,
                                params={binding.id_param: state_matches[0]["record_id"]},
                                actor=actor,
                                expected_valid=True,
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
                    elif candidates:
                        synth_id = f"{binding.id_prefix}NEG-{binding.action_name}-{obs.state_before}"
                        seen_neg.add(key)
                        neg.append(
                            ReplayEvent(
                                name=f"auto_neg_{binding.action_name}_from_{obs.state_before}",
                                action=binding.action_name,
                                params={binding.id_param: synth_id},
                                actor=AI_OPS_ACTOR,
                                expected_valid=False,
                                expected_error_code=f"INVALID_STATE_FOR_{obs.method.upper()}",
                                setup_objects=(
                                    {
                                        "object_type": binding.target_object_type,
                                        "data": {
                                            **candidates[0],
                                            "record_id": synth_id,
                                            obs.status_field: obs.state_before,
                                        },
                                    },
                                ),
                            )
                        )
        finally:
            if sim_rt is not None:
                sim_rt.close()
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
                settled_status = str(run["status"])
                error_code = run.get("error_code")
                projected_row = self.get_last_observed(run_id) or {}
            except InvariantError as exc:
                err_code = exc.code or type(exc).__name__
                if err_code.startswith("APPROVAL_REQUIRED_") and approver is not None:
                    appr_rid = f"{run_id}-APPR"
                    if "open_work_card" in self.memory_runtime.spec.actions:
                        wc_id = f"WC-{run_id}"
                        self.memory_runtime.submit_action(
                            f"{run_id}-WC-OPEN",
                            "open_work_card",
                            {
                                "card_id": wc_id,
                                "card_kind": "APPROVAL",
                                "target_object_type": binding.target_object_type,
                                "target_object_id": canonical_id,
                                "requested_action": action_name,
                                "requested_params_json": json.dumps(params),
                                "assignee_role": approver.role,
                            },
                            actor,
                        )
                        settled_wc = settle_work_card(
                            self.memory_runtime,
                            wc_id,
                            approver,
                            action_run_id=appr_rid,
                            complete_run_id=f"{run_id}-WC-COMP",
                        )
                        run = settled_wc["settled_action_run"]
                    else:
                        run = self.memory_runtime.submit_action(
                            appr_rid, action_name, params, approver
                        )
                    staged_status = "APPROVAL_REQUIRED"
                    settled_status = str(run["status"])
                    error_code = run.get("error_code")
                    projected_row = self.get_last_observed(str(run.get("run_id") or appr_rid)) or {}
                else:
                    staged_status = "REJECTED"
                    settled_status = "REJECTED"
                    error_code = err_code
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
            replay_factory=build_auto_replay_factory(bundle, seed_ontology=active_seed),
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

                def _has_approval_gate(act_bp: ActionBlueprint) -> bool:
                    return any(
                        pre.code.startswith("APPROVAL_REQUIRED_") for pre in act_bp.preconditions
                    )

                used_records: set[tuple[str, str]] = set()
                # Pass 1: one per distinct clean forward binding (prefers cross-model diversity)
                for binding in active_mapping.action_bindings:
                    if len(homomorphic_results) >= 2:
                        break
                    act_def = next(a for a in active_bp.actions if a.name == binding.action_name)
                    if _has_approval_gate(act_def):
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
                        if _has_approval_gate(act_def):
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
                    if _has_approval_gate(act_def):
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
