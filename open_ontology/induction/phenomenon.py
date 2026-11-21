"""外部现象画像与零 exec 声明式映射投影器（纯 M2，领域无关）。

修复与保障：
* 自动按 ``lookup_join.lookup_table`` 做拓扑排序，消除映射声明顺序依赖；
* 校验单实体非空投影覆盖，防止把整张外部表过滤成 0 行来空心通过门禁。
"""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..spec import InvariantError

__all__ = [
    "ExternalFieldProfile",
    "ExternalEntityProfile",
    "ExternalPhenomenonProfile",
    "RowFilterSpec",
    "FieldTransformSpec",
    "EntityMappingSpec",
    "ActionExecutionBinding",
    "AdapterMappingSpec",
    "toposort_entity_mappings",
    "project_external_phenomenon",
    "project_single_record",
    "merge_projected_phenomena",
]


class ExternalFieldProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    raw_type: str = "string"
    label: str = ""
    relation_target: str | None = None
    enum_values: list[str] = Field(default_factory=list)


class ExternalEntityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    row_count: int = 0
    fields: list[ExternalFieldProfile] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


class ExternalPhenomenonProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: str = "erp"
    source_locator: str = ""
    entities: list[ExternalEntityProfile] = Field(default_factory=list)

    def summary_for_builder(self, *, max_samples_per_table: int = 4) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_locator": self.source_locator,
            "entities": [
                {
                    "table_name": e.table_name,
                    "row_count": e.row_count,
                    "fields": [f.model_dump(mode="json") for f in e.fields],
                    "sample_rows": e.sample_rows[:max_samples_per_table],
                }
                for e in self.entities
            ],
        }


class RowFilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: Literal["eq", "ne", "in", "truthy", "gt"] = "eq"
    value: Any = None


class FieldTransformSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_field: str
    source_field: str = ""
    kind: Literal[
        "direct",
        "id_prefix",
        "foreign_key_id",
        "lookup_join",
        "enum_map",
        "scale_int",
        "const",
        "coalesce",
    ] = "direct"
    prefix: str = ""
    default: Any = None
    scale: int = 1
    enum_map: dict[str, str] = Field(default_factory=dict)
    fallback_fields: list[str] = Field(default_factory=list)
    lookup_table: str = ""
    lookup_key_field: str = ""
    lookup_target_type: str = ""
    treat_false_as_null: bool | None = None


class EntityMappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_table: str
    target_object_type: str
    id_transform: FieldTransformSpec
    row_filters: list[RowFilterSpec] = Field(default_factory=list)
    field_transforms: list[FieldTransformSpec] = Field(default_factory=list)
    drop_if_missing_refs: list[str] = Field(default_factory=list)
    treat_false_as_null: bool | None = None


class ActionExecutionBinding(BaseModel):
    """Declarative binding from a governed M1 action to an external system method."""

    model_config = ConfigDict(extra="forbid")

    action_name: str
    external_table: str
    external_method: str
    target_object_type: str
    id_param: str = "record_id"
    id_prefix: str = ""
    expected_state_field: str = ""
    expected_state_after: str = ""
    readback_tables: list[str] = Field(default_factory=list)
    extra_params: list[str] = Field(default_factory=list)


class AdapterMappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping_id: str = ""
    source_kind: str = "erp"
    treat_false_as_null: bool = False
    entity_mappings: list[EntityMappingSpec] = Field(default_factory=list)
    action_bindings: list[ActionExecutionBinding] = Field(default_factory=list)

    def get_action_binding(self, action_name: str) -> ActionExecutionBinding | None:
        for b in self.action_bindings:
            if b.action_name == action_name:
                return b
        return None

    def get_entity_mapping(self, target_object_type: str) -> EntityMappingSpec | None:
        for em in self.entity_mappings:
            if em.target_object_type == target_object_type:
                return em
        return None


def _row_matches_filter(row: dict[str, Any], flt: RowFilterSpec) -> bool:
    val = row.get(flt.field)
    if flt.op == "eq":
        return val == flt.value
    if flt.op == "ne":
        return val != flt.value
    if flt.op == "in":
        return val in (flt.value or ())
    if flt.op == "truthy":
        return bool(val)
    if flt.op == "gt":
        if isinstance(val, bool) or val is None:
            return False
        return val > flt.value
    return False


def _resolve_false_as_null(
    tf: FieldTransformSpec,
    em: EntityMappingSpec | None,
    default_false_as_null: bool,
) -> bool:
    if tf.treat_false_as_null is not None:
        return bool(tf.treat_false_as_null)
    if em is not None and em.treat_false_as_null is not None:
        return bool(em.treat_false_as_null)
    return bool(default_false_as_null)


def _extract_raw_value(
    row: dict[str, Any],
    field_name: str,
    *,
    treat_false_as_null: bool = False,
) -> Any:
    val = row.get(field_name)
    if treat_false_as_null and val is False:
        return None
    return val


def _apply_field_transform(
    row: dict[str, Any],
    tf: FieldTransformSpec,
    lookup_index: dict[tuple[str, str, str], str],
    *,
    treat_false_as_null: bool = False,
) -> Any:
    if tf.kind == "const":
        return copy.deepcopy(tf.default)
    if tf.kind == "direct":
        raw = row.get(tf.source_field)
        if raw is None or (treat_false_as_null and raw is False and not isinstance(tf.default, bool)):
            return copy.deepcopy(tf.default)
        return raw
    if tf.kind == "coalesce":
        for fname in [tf.source_field, *tf.fallback_fields]:
            if not fname:
                continue
            cand = _extract_raw_value(row, fname, treat_false_as_null=treat_false_as_null)
            if cand is not None and str(cand).strip() != "":
                return f"{tf.prefix}{cand}" if tf.prefix else cand
        return copy.deepcopy(tf.default)
    if tf.kind == "id_prefix":
        raw = _extract_raw_value(row, tf.source_field, treat_false_as_null=treat_false_as_null)
        if raw is None:
            return copy.deepcopy(tf.default)
        if tf.enum_map and str(raw) in tf.enum_map:
            return tf.enum_map[str(raw)]
        return f"{tf.prefix}{raw}"
    if tf.kind == "foreign_key_id":
        raw = row.get(tf.source_field)
        if raw is None or (treat_false_as_null and raw is False):
            return copy.deepcopy(tf.default)
        raw_id = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
        if raw_id is None or (treat_false_as_null and raw_id is False):
            return copy.deepcopy(tf.default)
        if tf.enum_map and str(raw_id) in tf.enum_map:
            return tf.enum_map[str(raw_id)]
        return f"{tf.prefix}{raw_id}"
    if tf.kind == "lookup_join":
        raw = _extract_raw_value(row, tf.source_field, treat_false_as_null=treat_false_as_null)
        if raw is None:
            return copy.deepcopy(tf.default)
        if tf.lookup_target_type:
            typed_key = (f"{tf.lookup_target_type}@{tf.lookup_table}", tf.lookup_key_field, str(raw))
            if typed_key in lookup_index:
                return lookup_index[typed_key]
        return lookup_index.get(
            (tf.lookup_table, tf.lookup_key_field, str(raw)),
            copy.deepcopy(tf.default),
        )
    if tf.kind == "enum_map":
        raw = _extract_raw_value(row, tf.source_field, treat_false_as_null=treat_false_as_null)
        if raw is None:
            return copy.deepcopy(tf.default)
        return tf.enum_map.get(
            str(raw),
            copy.deepcopy(tf.default) if tf.default is not None else str(raw),
        )
    if tf.kind == "scale_int":
        raw = _extract_raw_value(row, tf.source_field, treat_false_as_null=treat_false_as_null)
        if raw is None or isinstance(raw, bool):
            return int(tf.default or 0)
        return int(round(float(raw) * tf.scale))
    raise InvariantError(f"unsupported transform kind {tf.kind!r}", code="INVALID_TRANSFORM")


def toposort_entity_mappings(
    mappings: list[EntityMappingSpec],
) -> tuple[list[EntityMappingSpec], list[str]]:
    """Order entity_mappings so lookup_join and foreign_key_id parent tables precede dependent tables."""
    idx_by_table: dict[str, list[int]] = defaultdict(list)
    idx_by_target_and_table: dict[tuple[str, str], list[int]] = defaultdict(list)
    idx_by_prefix: dict[str, list[int]] = defaultdict(list)
    for idx, em in enumerate(mappings):
        idx_by_table[em.source_table].append(idx)
        idx_by_target_and_table[(em.target_object_type, em.source_table)].append(idx)
        if em.id_transform.prefix:
            idx_by_prefix[em.id_transform.prefix].append(idx)

    deps: dict[int, set[int]] = {i: set() for i in range(len(mappings))}
    rev: dict[int, set[int]] = {i: set() for i in range(len(mappings))}
    for idx, em in enumerate(mappings):
        all_tfs = [em.id_transform, *em.field_transforms]
        for tf in all_tfs:
            if tf.kind == "lookup_join" and tf.lookup_table:
                dep_candidates = (
                    idx_by_target_and_table.get((tf.lookup_target_type, tf.lookup_table), [])
                    if tf.lookup_target_type
                    else idx_by_table.get(tf.lookup_table, [])
                )
                for dep_idx in dep_candidates:
                    if dep_idx != idx:
                        deps[idx].add(dep_idx)
                        rev[dep_idx].add(idx)

    def _reachable(start: int, target: int) -> bool:
        stack = [start]
        seen_nodes: set[int] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen_nodes:
                continue
            seen_nodes.add(node)
            stack.extend(rev[node])
        return False

    # Soft-order foreign_key_id parent entities before child entities whenever acyclic
    for idx, em in enumerate(mappings):
        for tf in em.field_transforms:
            if tf.kind == "foreign_key_id" and tf.prefix:
                for dep_idx in idx_by_prefix.get(tf.prefix, []):
                    if dep_idx != idx and dep_idx not in deps[idx] and not _reachable(idx, dep_idx):
                        deps[idx].add(dep_idx)
                        rev[dep_idx].add(idx)

    in_deg = {i: len(d) for i, d in deps.items()}
    q: deque[int] = deque(i for i in range(len(mappings)) if in_deg[i] == 0)
    ordered_indices: list[int] = []
    while q:
        cur = q.popleft()
        ordered_indices.append(cur)
        for nxt in sorted(rev[cur]):
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                q.append(nxt)

    errors: list[str] = []
    if len(ordered_indices) != len(mappings):
        errors.append("CYCLIC_LOOKUP_JOIN: cyclic lookup_join dependencies across entity_mappings")
        # Append remaining deterministically so diagnostics can proceed
        seen = set(ordered_indices)
        ordered_indices.extend(i for i in range(len(mappings)) if i not in seen)

    return [mappings[i] for i in ordered_indices], errors


_toposort_entity_mappings = toposort_entity_mappings


def project_external_phenomenon(
    profile: ExternalPhenomenonProfile,
    mapping: AdapterMappingSpec,
    *,
    require_non_empty_per_entity: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministically project external sampled rows into M0 objects via zero-exec AdapterMappingSpec."""
    entities_by_table = {e.table_name: e for e in profile.entities}
    lookup_index: dict[tuple[str, str, str], str] = {}
    projected: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    errors: list[str] = []

    if not mapping.entity_mappings:
        return [], ["adapter_mapping contains zero entity_mappings"]

    ordered_mappings, sort_errors = toposort_entity_mappings(mapping.entity_mappings)
    errors.extend(sort_errors)

    for em in ordered_mappings:
        src_entity = entities_by_table.get(em.source_table)
        if src_entity is None:
            errors.append(f"mapping references unknown external table {em.source_table!r}")
            continue
        em_count = 0
        for row in src_entity.sample_rows:
            if any(not _row_matches_filter(row, flt) for flt in em.row_filters):
                continue
            obj_id = _apply_field_transform(
                row,
                em.id_transform,
                lookup_index,
                treat_false_as_null=_resolve_false_as_null(
                    em.id_transform, em, mapping.treat_false_as_null
                ),
            )
            if obj_id is None or str(obj_id).strip() == "":
                errors.append(
                    f"failed to compute canonical ID for {em.source_table} -> {em.target_object_type}"
                )
                continue
            canonical_id = str(obj_id)
            data: dict[str, Any] = {em.id_transform.target_field: canonical_id}
            dropped = False
            for tf in em.field_transforms:
                val = _apply_field_transform(
                    row,
                    tf,
                    lookup_index,
                    treat_false_as_null=_resolve_false_as_null(
                        tf, em, mapping.treat_false_as_null
                    ),
                )
                if val is None and tf.target_field in em.drop_if_missing_refs:
                    dropped = True
                    break
                data[tf.target_field] = val
            if dropped:
                continue

            # Register lookup keys for downstream child tables (e.g. foreign_ref -> parent.code)
            em_false_as_null = (
                bool(em.treat_false_as_null)
                if em.treat_false_as_null is not None
                else bool(mapping.treat_false_as_null)
            )
            for raw_k, raw_v in row.items():
                if (
                    raw_v is not None
                    and not (em_false_as_null and raw_v is False)
                    and not isinstance(raw_v, (list, dict))
                ):
                    lookup_index[
                        (f"{em.target_object_type}@{em.source_table}", str(raw_k), str(raw_v))
                    ] = canonical_id
                    lookup_index.setdefault(
                        (em.source_table, str(raw_k), str(raw_v)), canonical_id
                    )

            dedup_key = (em.target_object_type, canonical_id)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            em_count += 1
            projected.append(
                {
                    "object_type": em.target_object_type,
                    "object_id": canonical_id,
                    "data": data,
                }
            )

        # P1-2 fix: reject vacuous 0-row filtering on non-empty source tables
        if require_non_empty_per_entity and src_entity.sample_rows and em_count == 0:
            errors.append(
                f"EMPTY_ENTITY_PROJECTION: mapping {em.source_table!r} -> {em.target_object_type!r} "
                f"projected 0 objects from {len(src_entity.sample_rows)} sampled rows"
            )

    if not projected:
        errors.append("adapter_mapping projected 0 M0 objects from external phenomenon")
    return projected, errors


def project_single_record(
    em: EntityMappingSpec,
    raw_row: dict[str, Any],
    *,
    lookup_index: dict[tuple[str, str, str], str] | None = None,
    treat_false_as_null: bool = False,
) -> dict[str, Any]:
    """Project a single raw external record using an EntityMappingSpec (e.g. for independent readback)."""
    idx = lookup_index or {}
    obj_id = _apply_field_transform(
        raw_row,
        em.id_transform,
        idx,
        treat_false_as_null=_resolve_false_as_null(em.id_transform, em, treat_false_as_null),
    )
    if obj_id is None or str(obj_id).strip() == "":
        raise InvariantError(
            f"failed to compute canonical ID for {em.source_table} -> {em.target_object_type}",
            code="INVALID_READBACK_ID",
        )
    data: dict[str, Any] = {em.id_transform.target_field: str(obj_id)}
    for tf in em.field_transforms:
        data[tf.target_field] = _apply_field_transform(
            raw_row,
            tf,
            idx,
            treat_false_as_null=_resolve_false_as_null(tf, em, treat_false_as_null),
        )
    return data


def merge_projected_phenomena(
    batches: Sequence[Sequence[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Coalesce projected M0 object dicts across multiple sources by ``(object_type, object_id)``."""
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in batches:
        for raw_entry in batch:
            tname = str(raw_entry.get("object_type") or "")
            oid = str(raw_entry.get("object_id") or "")
            key = (tname, oid)
            incoming_data = dict(raw_entry.get("data") or {})
            if key not in by_key:
                copied = {
                    "object_type": tname,
                    "object_id": oid,
                    "data": copy.deepcopy(incoming_data),
                }
                merged.append(copied)
                by_key[key] = copied
                continue
            cur_data = by_key[key]["data"]
            for fname, val in incoming_data.items():
                if val is None or val == "":
                    continue
                cur_val = cur_data.get(fname)
                if cur_val is None or cur_val == "":
                    cur_data[fname] = copy.deepcopy(val)
    return merged
