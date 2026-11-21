"""Pure M2 sidecar plugin coordinating incremental multi-source entity resolution.

Wraps ``open_ontology.induction.resolution`` outside the microkernel:

* Cumulative incremental source record merging without mutating M1 schemas;
* Automatic L1/L0 crosswalk persistence and hot refresh of ``AdapterMappingSpec``;
* Fail-closed L4 gray-zone and deceptive hard-key conflict queuing via generic
  injected ``OntologyRuntime`` actions with strict idempotent deduplication;
* Polling convergence from resolved M1 review objects into permanent
  ``confirmed_links`` and ``negative_links``.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import threading
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .induction.phenomenon import AdapterMappingSpec
from .induction.resolution import (
    BlockedPairRecord,
    CrosswalkTable,
    EntityMatchCandidate,
    EntityResolutionDecision,
    EntityResolutionReport,
    EntityResolutionSpec,
    SourceEntityBatch,
    _build_snapshot,
    _pair_key,
    _quasi_similarity,
    apply_resolution_decisions,
    build_resolution_task_intent,
    inject_crosswalk_into_mapping,
    resolve_entities,
)
from .runtime import OntologyRuntime
from .spec import Actor

__all__ = [
    "MDMReconciliationResult",
    "MDMSidecarPlugin",
]


class MDMReconciliationResult(BaseModel):
    """Outcome summary returned by ``MDMSidecarPlugin.ingest_incremental``."""

    model_config = ConfigDict(extra="forbid")

    crosswalk: CrosswalkTable
    auto_merged_canonical_ids: list[str] = Field(default_factory=list)
    dispatched_review_run_ids: list[str] = Field(default_factory=list)
    pending_candidates: list[EntityMatchCandidate] = Field(default_factory=list)
    blocked_pairs: list[BlockedPairRecord] = Field(default_factory=list)
    refreshed_mappings: dict[str, AdapterMappingSpec] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)


class MDMSidecarPlugin:
    """Domain-agnostic external MDM coordinator attached alongside ``OntologyRuntime``."""

    def __init__(
        self,
        resolution_spec: EntityResolutionSpec,
        *,
        existing_crosswalk: CrosswalkTable | None = None,
        adapter_mappings: Mapping[str, AdapterMappingSpec] | None = None,
        foreign_key_fields: Sequence[tuple[str, str]] = (),
        review_action: str | None = None,
        review_payload_builder: Callable[..., dict[str, Any]] | None = None,
        on_crosswalk_updated: Callable[[CrosswalkTable, dict[str, AdapterMappingSpec]], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.resolution_spec = copy.deepcopy(resolution_spec)
        self.crosswalk = (
            copy.deepcopy(existing_crosswalk)
            if existing_crosswalk is not None
            else CrosswalkTable(
                target_object_type=resolution_spec.target_object_type,
                canonical_id_prefix=resolution_spec.canonical_id_prefix,
            )
        )
        self._base_mappings: dict[str, AdapterMappingSpec] = {
            src_id: copy.deepcopy(spec_obj)
            for src_id, spec_obj in (adapter_mappings or {}).items()
        }
        self._refreshed_mappings: dict[str, AdapterMappingSpec] = {}
        self._foreign_key_fields = tuple(foreign_key_fields)
        self.review_action = review_action
        self.review_payload_builder = review_payload_builder
        self.on_crosswalk_updated = on_crosswalk_updated
        self.dispatched_review_keys: set[str] = set()
        self._applied_rulings: set[tuple[str, str, str, str]] = set()

        # Cumulative key -> (source_id, source_table, pk_field, field_map, row_copy)
        self._records_by_key: dict[
            str, tuple[str, str, str, dict[str, str], dict[str, Any]]
        ] = {}
        self._refresh_mappings(notify=False)

    @property
    def adapter_mappings(self) -> dict[str, AdapterMappingSpec]:
        """Return a deep copy of all hot-refreshed ``AdapterMappingSpec`` objects."""
        with self._lock:
            return copy.deepcopy(self._refreshed_mappings)

    def _refresh_mappings(self, *, notify: bool = True) -> dict[str, AdapterMappingSpec]:
        refreshed: dict[str, AdapterMappingSpec] = {}
        for src_id, base_map in self._base_mappings.items():
            refreshed[src_id] = inject_crosswalk_into_mapping(
                base_map,
                source_id=src_id,
                crosswalk=self.crosswalk,
                target_object_type=self.resolution_spec.target_object_type,
                foreign_key_fields=self._foreign_key_fields,
            )
        self._refreshed_mappings = refreshed
        copied = copy.deepcopy(refreshed)
        if notify and self.on_crosswalk_updated is not None:
            self.on_crosswalk_updated(copy.deepcopy(self.crosswalk), copied)
        return copied

    def _materialize_batches(self) -> list[SourceEntityBatch]:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        field_maps: dict[tuple[str, str, str], dict[str, str]] = {}
        for source_id, source_table, pk_field, fmap, row in self._records_by_key.values():
            grp_key = (source_id, source_table, pk_field)
            grouped.setdefault(grp_key, []).append(copy.deepcopy(row))
            if grp_key not in field_maps:
                field_maps[grp_key] = dict(fmap)
            else:
                field_maps[grp_key] = {**field_maps[grp_key], **fmap}
        return [
            SourceEntityBatch(
                source_id=grp_key[0],
                source_table=grp_key[1],
                pk_field=grp_key[2],
                records=rows,
                field_map=field_maps[grp_key],
            )
            for grp_key, rows in grouped.items()
        ]

    def _merge_batches(self, batches: Sequence[SourceEntityBatch]) -> None:
        for batch in batches:
            fmap = dict(batch.field_map)
            for row in batch.records:
                raw_pk_val = row.get(batch.pk_field)
                if raw_pk_val is None or str(raw_pk_val).strip() == "":
                    raise ValueError(
                        f"missing primary key {batch.pk_field!r} in batch "
                        f"{batch.source_id}:{batch.source_table}"
                    )
                rec_key = f"{batch.source_id}:{raw_pk_val}"
                self._records_by_key[rec_key] = (
                    batch.source_id,
                    batch.source_table,
                    batch.pk_field,
                    fmap,
                    copy.deepcopy(row),
                )

    def _build_snapshot_lookup(self, batches: Sequence[SourceEntityBatch]) -> dict[str, dict[str, Any]]:
        key_fields = (
            set(self.resolution_spec.hard_key_fields)
            | set(self.resolution_spec.quasi_key_fields)
            | set(self.resolution_spec.neighbor_match_fields)
            | set(self.resolution_spec.survivorship.keys())
        )
        snaps: dict[str, dict[str, Any]] = {}
        for batch in batches:
            for row in batch.records:
                rec_key = f"{batch.source_id}:{row[batch.pk_field]}"
                snaps[rec_key] = _build_snapshot(row, batch, key_fields)
        return snaps

    def _build_review_payload(self, intent: dict[str, Any], item: Any) -> dict[str, Any]:
        builder = self.review_payload_builder
        assert builder is not None
        sig = inspect.signature(builder)
        positional = [
            p
            for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if has_varargs or len(positional) >= 2:
            return builder(intent, item)
        return builder(intent)

    def _collect_review_items(
        self,
        report: EntityResolutionReport,
        snaps: Mapping[str, dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any], EntityMatchCandidate]]:
        items: list[tuple[str, dict[str, Any], EntityMatchCandidate]] = []
        for cand in report.ambiguous_candidates:
            pk = _pair_key(cand.left.key, cand.right.key)
            review_key = f"L4:{pk[0]}|{pk[1]}"
            intent = build_resolution_task_intent(cand)
            intent["item_kind"] = "AMBIGUOUS_CANDIDATE"
            items.append((review_key, intent, cand))

        for bp in report.blocked_pairs:
            if bp.reason_code != "CONFLICTING_HARD_KEY":
                continue
            snap_a = snaps.get(bp.left.key, {})
            snap_b = snaps.get(bp.right.key, {})
            best_quasi = max(
                (
                    _quasi_similarity(
                        snap_a.get(qf),
                        snap_b.get(qf),
                        self.resolution_spec.strip_tokens,
                    )
                    for qf in self.resolution_spec.quasi_key_fields
                ),
                default=0.0,
            )
            if best_quasi < self.resolution_spec.review_threshold:
                continue
            pk = _pair_key(bp.left.key, bp.right.key)
            review_key = f"CONFLICT:{pk[0]}|{pk[1]}"
            conf_digest = hashlib.sha256(f"{pk[0]}|{pk[1]}".encode("utf-8")).hexdigest()[:10].upper()
            conflict_cand = EntityMatchCandidate(
                candidate_id=f"CONF-{conf_digest}",
                target_object_type=self.resolution_spec.target_object_type,
                left=copy.deepcopy(bp.left),
                right=copy.deepcopy(bp.right),
                left_snapshot=copy.deepcopy(snap_a),
                right_snapshot=copy.deepcopy(snap_b),
                score=round(best_quasi, 6),
                reasons=["CONFLICTING_HARD_KEY", bp.detail],
                conflicting_fields=list(self.resolution_spec.hard_key_fields),
            )
            intent = build_resolution_task_intent(conflict_cand)
            intent["intent_code"] = "RESOLVE_HARD_KEY_CONFLICT"
            intent["item_kind"] = "HARD_KEY_CONFLICT"
            items.append((review_key, intent, conflict_cand))

        return items

    def ingest_incremental(
        self,
        batches: Sequence[SourceEntityBatch],
        *,
        runtime: OntologyRuntime | None = None,
        actor: Actor | None = None,
        run_id_prefix: str = "mdm",
    ) -> MDMReconciliationResult:
        """Merge incremental source batches, update crosswalks, and dispatch idempotent reviews."""
        with self._lock:
            self._merge_batches(batches)
            materialized = self._materialize_batches()
            snaps = self._build_snapshot_lookup(materialized)
            report = resolve_entities(
                self.resolution_spec,
                materialized,
                existing_crosswalk=self.crosswalk,
            )
            self.crosswalk = report.crosswalk
            refreshed = self._refresh_mappings(notify=True)

            auto_merged = [
                entry.canonical_id
                for entry in report.crosswalk.entries
                if len(entry.members) > 1
                or entry.match_tier in ("L0_CONFIRMED", "L1_HARD_KEY", "L2_L3_SCORE")
            ]

            review_items = self._collect_review_items(report, snaps)
            dispatched_run_ids: list[str] = []
            if (
                runtime is not None
                and actor is not None
                and self.review_action
                and self.review_payload_builder is not None
            ):
                for review_key, intent, item_obj in review_items:
                    if review_key in self.dispatched_review_keys:
                        continue
                    digest = hashlib.sha256(review_key.encode("utf-8")).hexdigest()[:12]
                    run_id = f"{run_id_prefix}-rev-{digest}"
                    intent_meta = {**intent, "run_id": run_id, "review_key": review_key}
                    payload = self._build_review_payload(intent_meta, item_obj)
                    runtime.submit_action(run_id, self.review_action, payload, actor)
                    self.dispatched_review_keys.add(review_key)
                    dispatched_run_ids.append(run_id)

            return MDMReconciliationResult(
                crosswalk=copy.deepcopy(self.crosswalk),
                auto_merged_canonical_ids=auto_merged,
                dispatched_review_run_ids=dispatched_run_ids,
                pending_candidates=copy.deepcopy(report.ambiguous_candidates),
                blocked_pairs=copy.deepcopy(report.blocked_pairs),
                refreshed_mappings=refreshed,
                stats=dict(report.stats),
            )

    def apply_steward_ruling(self, decision: EntityResolutionDecision) -> CrosswalkTable:
        """Lock a human ruling into L0 links, recompute crosswalk, and refresh mappings."""
        with self._lock:
            updated = apply_resolution_decisions(self.crosswalk, [decision])
            if self._records_by_key:
                materialized = self._materialize_batches()
                report = resolve_entities(
                    self.resolution_spec,
                    materialized,
                    existing_crosswalk=updated,
                )
                self.crosswalk = report.crosswalk
            else:
                self.crosswalk = updated
            sig = (
                *_pair_key(decision.left.key, decision.right.key),
                decision.verdict,
                decision.preferred_canonical_id or "",
            )
            self._applied_rulings.add(sig)
            self._refresh_mappings(notify=True)
            return copy.deepcopy(self.crosswalk)

    def sync_resolved_reviews(
        self,
        runtime: OntologyRuntime,
        actor: Actor,
        *,
        review_object_type: str,
        decision_extractor: Callable[[dict[str, Any]], EntityResolutionDecision | None],
    ) -> list[EntityResolutionDecision]:
        """Poll resolved review objects from ``runtime`` and apply unapplied rulings idempotently."""
        with self._lock:
            rows = runtime.query_objects(review_object_type, actor)
            applied: list[EntityResolutionDecision] = []
            for obj in rows:
                raw = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else dict(obj)
                dec = decision_extractor(raw)
                if dec is None:
                    continue
                sig = (
                    *_pair_key(dec.left.key, dec.right.key),
                    dec.verdict,
                    dec.preferred_canonical_id or "",
                )
                if sig in self._applied_rulings:
                    continue
                self.apply_steward_ruling(dec)
                applied.append(dec)
            return applied
