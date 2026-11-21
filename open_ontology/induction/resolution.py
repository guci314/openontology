"""Pure M2 4-tier entity resolution funnel and crosswalk engine (domain-agnostic).

Resolves duplicate or ambiguous source records across heterogeneous batches
using a safety-first 4-tier funnel without mutating M1 domain schemas:

* **L0** — Persistent confirmed links & negative links (human-decided locks).
* **L1** — Normalized hard-key exact match & conflicting hard-key hard block.
* **L2 + L3** — Caller-injected suffix normalization, token/sequence similarity,
  and weighted auxiliary attribute matching.
* **L4** — Gray-zone split (never silently auto-merged) producing
  ``EntityMatchCandidate`` records and domain-agnostic review task intents.

Maintains an all-pairs transitive clustering invariant: two clusters are never
merged if any cross-cluster pair hits a negative link or conflicting hard key.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .phenomenon import AdapterMappingSpec

__all__ = [
    "EntityResolutionSpec",
    "SourceRef",
    "SourceEntityBatch",
    "CrosswalkEntry",
    "EntityMatchCandidate",
    "BlockedPairRecord",
    "EntityResolutionDecision",
    "CrosswalkTable",
    "EntityResolutionReport",
    "resolve_entities",
    "apply_resolution_decisions",
    "build_resolution_task_intent",
    "inject_crosswalk_into_mapping",
]

_TIER_RANK: dict[str, int] = {
    "L0_CONFIRMED": 0,
    "L1_HARD_KEY": 1,
    "L2_L3_SCORE": 2,
    "SINGLETON": 3,
}

_PUNCT_SPACE_RE = re.compile(r"[\s,.;:()\[\]\"'\-/]+")
_ASCII_TOKEN_RE = re.compile(r"^[a-z0-9.\s,&\-+/]+$")
_HARD_KEY_STRIP_RE = re.compile(r"[\s\-_]+")
_CODE_NORM_RE = re.compile(r"[\s\-]+")
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]+")


class EntityResolutionSpec(BaseModel):
    """Declarative configuration for pure M2 multi-source entity resolution."""

    model_config = ConfigDict(extra="forbid")

    target_object_type: str
    canonical_id_prefix: str = "ENT-"
    hard_key_fields: list[str] = Field(default_factory=list)
    quasi_key_fields: list[str] = Field(default_factory=list)
    neighbor_match_fields: list[str] = Field(default_factory=list)
    strip_tokens: list[str] = Field(default_factory=list)
    auto_merge_threshold: float = 0.90
    review_threshold: float = 0.55
    quasi_key_weight: float = 0.70
    neighbor_weight: float = 0.30
    survivorship: dict[str, list[str]] = Field(default_factory=dict)


class SourceRef(BaseModel):
    """Reference to one source record by origin system, table, and primary key."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_table: str
    raw_pk: str

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.raw_pk}"


class SourceEntityBatch(BaseModel):
    """Batch of raw records from a single source table for entity resolution."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_table: str
    pk_field: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    field_map: dict[str, str] = Field(default_factory=dict)


class CrosswalkEntry(BaseModel):
    """Resolved canonical cluster, member refs, and survived golden attributes."""

    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    members: list[SourceRef] = Field(default_factory=list)
    golden_record: dict[str, Any] = Field(default_factory=dict)
    match_tier: Literal["SINGLETON", "L0_CONFIRMED", "L1_HARD_KEY", "L2_L3_SCORE"] = "SINGLETON"
    confidence: float = 1.0
    evidence: list[str] = Field(default_factory=list)


class EntityMatchCandidate(BaseModel):
    """Ambiguous gray-zone pair requiring human steward adjudication."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    target_object_type: str
    left: SourceRef
    right: SourceRef
    left_snapshot: dict[str, Any] = Field(default_factory=dict)
    right_snapshot: dict[str, Any] = Field(default_factory=dict)
    score: float
    match_tier: Literal["L4_AMBIGUOUS"] = "L4_AMBIGUOUS"
    reasons: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)


class BlockedPairRecord(BaseModel):
    """Record pair hard-blocked from merging and suppressed from L4 prompts."""

    model_config = ConfigDict(extra="forbid")

    left: SourceRef
    right: SourceRef
    reason_code: Literal["L0_NEGATIVE_LINK", "CONFLICTING_HARD_KEY"]
    detail: str = ""


class EntityResolutionDecision(BaseModel):
    """Human steward ruling on an ambiguous entity resolution candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = ""
    left: SourceRef
    right: SourceRef
    verdict: Literal["CONFIRM_MERGE", "REJECT_SEPARATE"]
    decided_by: str = ""
    rationale: str = ""
    preferred_canonical_id: str | None = None


class CrosswalkTable(BaseModel):
    """Canonical entity ID crosswalk with persistent positive and negative links."""

    model_config = ConfigDict(extra="forbid")

    target_object_type: str
    canonical_id_prefix: str = "ENT-"
    entries: list[CrosswalkEntry] = Field(default_factory=list)
    confirmed_links: list[tuple[str, str, str]] = Field(default_factory=list)
    negative_links: list[tuple[str, str]] = Field(default_factory=list)

    def alias_map_for(self, source_id: str) -> dict[str, str]:
        """Return ``raw_pk -> canonical_id`` mapping for ``source_id``."""
        aliases: dict[str, str] = {}
        for entry in self.entries:
            for member in entry.members:
                if member.source_id == source_id:
                    aliases[member.raw_pk] = entry.canonical_id
        return aliases

    def resolve_canonical_id(self, source_id: str, raw_pk: str) -> str | None:
        """Look up the canonical ID assigned to ``(source_id, raw_pk)`` if present."""
        target_pk = str(raw_pk)
        for entry in self.entries:
            for member in entry.members:
                if member.source_id == source_id and member.raw_pk == target_pk:
                    return entry.canonical_id
        return None

    def members_of(self, canonical_id: str) -> list[SourceRef]:
        """Return all member ``SourceRef`` objects belonging to ``canonical_id``."""
        for entry in self.entries:
            if entry.canonical_id == canonical_id:
                return [copy.deepcopy(m) for m in entry.members]
        return []

    def golden_records(self) -> list[dict[str, Any]]:
        """Return copied golden attribute dicts for all crosswalk entries."""
        return [copy.deepcopy(entry.golden_record) for entry in self.entries]


class EntityResolutionReport(BaseModel):
    """Output bundle from running the 4-tier entity resolution funnel."""

    model_config = ConfigDict(extra="forbid")

    crosswalk: CrosswalkTable
    ambiguous_candidates: list[EntityMatchCandidate] = Field(default_factory=list)
    blocked_pairs: list[BlockedPairRecord] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


def _pair_key(k1: str, k2: str) -> tuple[str, str]:
    return (k1, k2) if k1 <= k2 else (k2, k1)


def _is_non_empty(val: Any) -> bool:
    if val is None or val is False:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    return True


def _normalize_hard_key(val: Any) -> str:
    if val is None or isinstance(val, bool):
        return ""
    norm = unicodedata.normalize("NFKC", str(val)).strip().upper()
    return _HARD_KEY_STRIP_RE.sub("", norm)


def _normalize_neighbor_value(val: Any) -> str:
    if val is None or isinstance(val, bool):
        return ""
    if isinstance(val, (int, float)):
        if float(val).is_integer():
            return str(int(val))
        return str(val)
    norm = unicodedata.normalize("NFKC", str(val)).strip().casefold()
    return _CODE_NORM_RE.sub("", norm)


def _normalize_quasi_value(val: Any, strip_tokens: Sequence[str]) -> str:
    if val is None or isinstance(val, bool):
        return ""
    base = unicodedata.normalize("NFKC", str(val)).strip().casefold()
    if not base:
        return ""
    cleaned = base
    norm_tokens = [
        unicodedata.normalize("NFKC", tok).strip().casefold()
        for tok in strip_tokens
        if tok and unicodedata.normalize("NFKC", tok).strip()
    ]
    # Remove longest suffixes/stopwords first so compound tokens win over sub-tokens.
    for tok in sorted(set(norm_tokens), key=len, reverse=True):
        escaped = re.escape(tok)
        if _ASCII_TOKEN_RE.match(tok):
            cleaned = re.sub(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", " ", cleaned)
        else:
            cleaned = cleaned.replace(tok, " ")
    collapsed = _PUNCT_SPACE_RE.sub(" ", cleaned).strip()
    if collapsed:
        return collapsed
    return _PUNCT_SPACE_RE.sub(" ", base).strip()


def _quasi_tokens(text: str) -> set[str]:
    if not text:
        return set()
    tokens: set[str] = set()
    for word in text.split():
        if not word:
            continue
        tokens.add(word)
        for span in _CJK_SPAN_RE.findall(word):
            if len(span) == 1:
                tokens.add(span)
            else:
                for idx in range(len(span) - 1):
                    tokens.add(span[idx : idx + 2])
    return tokens


def _quasi_similarity(val_a: Any, val_b: Any, strip_tokens: Sequence[str]) -> float:
    na = _normalize_quasi_value(val_a, strip_tokens)
    nb = _normalize_quasi_value(val_b, strip_tokens)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta = _quasi_tokens(na)
    tb = _quasi_tokens(nb)
    union = ta | tb
    jaccard = (len(ta & tb) / len(union)) if union else 0.0
    seq_ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return 0.5 * jaccard + 0.5 * seq_ratio


def _deterministic_canonical_id(prefix: str, keys: Sequence[str]) -> str:
    joined = "|".join(sorted(keys))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}{digest}"


def _build_snapshot(
    raw_row: dict[str, Any],
    batch: SourceEntityBatch,
    key_and_survivorship_fields: set[str],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    mapped_src_cols = {
        src_col
        for canonical_col, src_col in batch.field_map.items()
        if canonical_col != src_col
    }
    for canonical_col, src_col in batch.field_map.items():
        if src_col in raw_row:
            val = raw_row[src_col]
            if val is False and canonical_col in key_and_survivorship_fields:
                val = ""
            snapshot[canonical_col] = copy.deepcopy(val)
    for raw_col, raw_val in raw_row.items():
        if raw_col in mapped_src_cols or raw_col in snapshot:
            continue
        if raw_col == batch.pk_field and raw_col not in key_and_survivorship_fields:
            continue
        val = "" if (raw_val is False and raw_col in key_and_survivorship_fields) else raw_val
        snapshot[raw_col] = copy.deepcopy(val)
    return snapshot


def _resolve_golden_record(
    member_indices: Sequence[int],
    refs: Sequence[SourceRef],
    snapshots: Sequence[dict[str, Any]],
    spec: EntityResolutionSpec,
) -> dict[str, Any]:
    sorted_indices = sorted(member_indices, key=lambda idx: refs[idx].key)
    all_fields: list[str] = []
    seen_fields: set[str] = set()
    for fname in spec.survivorship:
        if fname not in seen_fields:
            seen_fields.add(fname)
            all_fields.append(fname)
    for idx in sorted_indices:
        for fname in snapshots[idx]:
            if fname not in seen_fields:
                seen_fields.add(fname)
                all_fields.append(fname)

    golden: dict[str, Any] = {}
    for fname in all_fields:
        priority_sources = spec.survivorship.get(fname, ())
        chosen_found = False
        chosen_val: Any = None
        for src_id in priority_sources:
            for idx in sorted_indices:
                if refs[idx].source_id == src_id and fname in snapshots[idx]:
                    cand = snapshots[idx][fname]
                    if _is_non_empty(cand):
                        chosen_val = copy.deepcopy(cand)
                        chosen_found = True
                        break
            if chosen_found:
                break
        if not chosen_found:
            for idx in sorted_indices:
                if fname in snapshots[idx] and _is_non_empty(snapshots[idx][fname]):
                    chosen_val = copy.deepcopy(snapshots[idx][fname])
                    chosen_found = True
                    break
        if not chosen_found:
            for idx in sorted_indices:
                if fname in snapshots[idx]:
                    chosen_val = copy.deepcopy(snapshots[idx][fname])
                    chosen_found = True
                    break
        if chosen_found:
            golden[fname] = chosen_val
    return golden


def resolve_entities(
    spec: EntityResolutionSpec,
    batches: Sequence[SourceEntityBatch],
    *,
    existing_crosswalk: CrosswalkTable | None = None,
) -> EntityResolutionReport:
    """Resolve records across source batches through the L0-L4 funnel."""
    key_and_surv = (
        set(spec.hard_key_fields)
        | set(spec.quasi_key_fields)
        | set(spec.neighbor_match_fields)
        | set(spec.survivorship.keys())
    )

    refs: list[SourceRef] = []
    snapshots: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}

    for batch in batches:
        for row in batch.records:
            raw_pk_val = row.get(batch.pk_field)
            if raw_pk_val is None or str(raw_pk_val).strip() == "":
                raise ValueError(
                    f"missing primary key {batch.pk_field!r} in batch {batch.source_id}:{batch.source_table}"
                )
            ref = SourceRef(
                source_id=batch.source_id,
                source_table=batch.source_table,
                raw_pk=str(raw_pk_val),
            )
            if ref.key in index_by_key:
                raise ValueError(f"duplicate source record key {ref.key!r}")
            index_by_key[ref.key] = len(refs)
            refs.append(ref)
            snapshots.append(_build_snapshot(row, batch, key_and_surv))

    neg_pair_set: set[tuple[str, str]] = set()
    persisted_negative_links: list[tuple[str, str]] = []
    seen_neg: set[tuple[str, str]] = set()
    persisted_confirmed_links: list[tuple[str, str, str]] = []
    confirmed_cid_by_pair: dict[tuple[str, str], str] = {}
    prior_cid_by_member: dict[str, str] = {}

    if existing_crosswalk is not None:
        for a_key, b_key in existing_crosswalk.negative_links:
            pk = _pair_key(a_key, b_key)
            if pk not in seen_neg:
                seen_neg.add(pk)
                neg_pair_set.add(pk)
                persisted_negative_links.append(pk)
        seen_conf: dict[tuple[str, str], str] = {}
        for a_key, b_key, cid in existing_crosswalk.confirmed_links:
            pk = _pair_key(a_key, b_key)
            if pk in neg_pair_set:
                continue
            seen_conf[pk] = cid
        for pk, cid in seen_conf.items():
            confirmed_cid_by_pair[pk] = cid
            persisted_confirmed_links.append((pk[0], pk[1], cid))
        for entry in existing_crosswalk.entries:
            for member in entry.members:
                prior_cid_by_member[member.key] = entry.canonical_id

    num_records = len(refs)
    norm_hard_keys: list[dict[str, str]] = [
        {hk: _normalize_hard_key(snap.get(hk)) for hk in spec.hard_key_fields}
        for snap in snapshots
    ]
    norm_neighbors: list[dict[str, str]] = [
        {nf: _normalize_neighbor_value(snap.get(nf)) for nf in spec.neighbor_match_fields}
        for snap in snapshots
    ]

    hard_blocked_pairs: set[tuple[str, str]] = set(neg_pair_set)
    blocked_records: list[BlockedPairRecord] = []

    # Candidate merge edges: (tier_rank, neg_score, key_a, key_b, idx_a, idx_b, tier_name, score, evidence)
    merge_edges: list[tuple[int, float, str, str, int, int, str, float, str]] = []
    gray_pairs: dict[tuple[int, int], tuple[float, list[str], list[str]]] = {}

    def _order_refs(idx_a: int, idx_b: int) -> tuple[int, int]:
        return (idx_a, idx_b) if refs[idx_a].key <= refs[idx_b].key else (idx_b, idx_a)

    for i in range(num_records):
        for j in range(i + 1, num_records):
            lo, hi = _order_refs(i, j)
            ref_lo, ref_hi = refs[lo], refs[hi]
            pk = (ref_lo.key, ref_hi.key)

            # L0 Negative link guard
            if pk in neg_pair_set:
                blocked_records.append(
                    BlockedPairRecord(
                        left=ref_lo,
                        right=ref_hi,
                        reason_code="L0_NEGATIVE_LINK",
                        detail=f"Negative link persisted for {ref_lo.key} and {ref_hi.key}.",
                    )
                )
                continue

            # Compute hard-key comparison (used both for L1 and for conflict block)
            conflicting_hk: list[str] = []
            matched_hk: list[str] = []
            for hk in spec.hard_key_fields:
                va = norm_hard_keys[lo][hk]
                vb = norm_hard_keys[hi][hk]
                if va and vb:
                    if va == vb:
                        matched_hk.append(hk)
                    else:
                        conflicting_hk.append(hk)

            if conflicting_hk:
                hard_blocked_pairs.add(pk)
                detail_parts = [
                    f"{hk}: {norm_hard_keys[lo][hk]!r} != {norm_hard_keys[hi][hk]!r}"
                    for hk in conflicting_hk
                ]
                blocked_records.append(
                    BlockedPairRecord(
                        left=ref_lo,
                        right=ref_hi,
                        reason_code="CONFLICTING_HARD_KEY",
                        detail="; ".join(detail_parts),
                    )
                )
                continue

            # L0 Confirmed link
            if pk in confirmed_cid_by_pair:
                merge_edges.append(
                    (
                        _TIER_RANK["L0_CONFIRMED"],
                        -1.0,
                        ref_lo.key,
                        ref_hi.key,
                        lo,
                        hi,
                        "L0_CONFIRMED",
                        1.0,
                        f"L0 confirmed link ({confirmed_cid_by_pair[pk]})",
                    )
                )
                continue

            # L1 Hard-key exact match
            if matched_hk:
                merge_edges.append(
                    (
                        _TIER_RANK["L1_HARD_KEY"],
                        -1.0,
                        ref_lo.key,
                        ref_hi.key,
                        lo,
                        hi,
                        "L1_HARD_KEY",
                        1.0,
                        f"L1 hard key match on {', '.join(matched_hk)}",
                    )
                )
                continue

            # L2 Normalized quasi-key + L3 Neighbor weighted similarity
            best_quasi = 0.0
            best_quasi_field = ""
            for qf in spec.quasi_key_fields:
                sim = _quasi_similarity(
                    snapshots[lo].get(qf),
                    snapshots[hi].get(qf),
                    spec.strip_tokens,
                )
                if sim > best_quasi:
                    best_quasi = sim
                    best_quasi_field = qf

            comparable_neighbors = 0
            matched_neighbors = 0
            mismatched_neighbors: list[str] = []
            matched_neighbor_fields: list[str] = []
            for nf in spec.neighbor_match_fields:
                na = norm_neighbors[lo][nf]
                nb = norm_neighbors[hi][nf]
                if na and nb:
                    comparable_neighbors += 1
                    if na == nb:
                        matched_neighbors += 1
                        matched_neighbor_fields.append(nf)
                    else:
                        mismatched_neighbors.append(nf)

            has_neighbor_channel = bool(spec.neighbor_match_fields) and spec.neighbor_weight > 0
            if not has_neighbor_channel:
                combined_score = best_quasi
            else:
                neighbor_ratio = (
                    matched_neighbors / comparable_neighbors if comparable_neighbors > 0 else 0.0
                )
                total_w = spec.quasi_key_weight + spec.neighbor_weight
                if total_w <= 0:
                    combined_score = best_quasi
                else:
                    combined_score = (
                        spec.quasi_key_weight * best_quasi
                        + spec.neighbor_weight * neighbor_ratio
                    ) / total_w

            combined_score = round(min(max(combined_score, 0.0), 1.0), 6)
            if combined_score >= spec.auto_merge_threshold:
                ev_parts = [f"L2 quasi({best_quasi_field})={best_quasi:.3f}"]
                if matched_neighbor_fields:
                    ev_parts.append(f"L3 neighbors({','.join(matched_neighbor_fields)}) matched")
                merge_edges.append(
                    (
                        _TIER_RANK["L2_L3_SCORE"],
                        -combined_score,
                        ref_lo.key,
                        ref_hi.key,
                        lo,
                        hi,
                        "L2_L3_SCORE",
                        combined_score,
                        "; ".join(ev_parts),
                    )
                )
            elif combined_score >= spec.review_threshold:
                reasons = [f"quasi({best_quasi_field})={best_quasi:.3f}"]
                if comparable_neighbors > 0:
                    reasons.append(f"neighbor_match={matched_neighbors}/{comparable_neighbors}")
                elif spec.neighbor_match_fields:
                    reasons.append("neighbor_match=0/0 (missing confirming attributes)")
                gray_pairs[(lo, hi)] = (combined_score, reasons, mismatched_neighbors)

    # Disjoint clusters with all-pairs anti-transitive conflict invariant
    cluster_of = list(range(num_records))
    clusters: dict[int, list[int]] = {idx: [idx] for idx in range(num_records)}
    cluster_tier: dict[int, str] = {idx: "SINGLETON" for idx in range(num_records)}
    cluster_confidence: dict[int, float] = {idx: 1.0 for idx in range(num_records)}
    cluster_evidence: dict[int, list[str]] = {idx: [] for idx in range(num_records)}

    merge_edges.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    for _rank, _neg_score, _ka, _kb, lo, hi, tier_name, edge_score, evidence in merge_edges:
        c1 = cluster_of[lo]
        c2 = cluster_of[hi]
        if c1 == c2:
            if evidence not in cluster_evidence[c1]:
                cluster_evidence[c1].append(evidence)
            continue

        members1 = clusters[c1]
        members2 = clusters[c2]
        cross_blocked = False
        for u in members1:
            for v in members2:
                pk = _pair_key(refs[u].key, refs[v].key)
                if pk in hard_blocked_pairs:
                    cross_blocked = True
                    break
            if cross_blocked:
                break
        if cross_blocked:
            continue

        # Merge c2 into min(c1, c2) deterministically
        keep_c, drop_c = (c1, c2) if c1 < c2 else (c2, c1)
        for m in clusters[drop_c]:
            cluster_of[m] = keep_c
            clusters[keep_c].append(m)
        prev_tier = cluster_tier[keep_c]
        other_tier = cluster_tier[drop_c]
        winning_tier = min(
            (prev_tier, other_tier, tier_name),
            key=lambda t: _TIER_RANK[t],
        )
        cluster_tier[keep_c] = winning_tier
        if winning_tier in ("L0_CONFIRMED", "L1_HARD_KEY"):
            cluster_confidence[keep_c] = 1.0
        else:
            cluster_confidence[keep_c] = min(
                cluster_confidence[keep_c],
                cluster_confidence[drop_c],
                edge_score,
            )
        merged_ev = list(cluster_evidence[keep_c])
        for ev in (*cluster_evidence[drop_c], evidence):
            if ev and ev not in merged_ev:
                merged_ev.append(ev)
        cluster_evidence[keep_c] = merged_ev
        del clusters[drop_c]
        del cluster_tier[drop_c]
        del cluster_confidence[drop_c]
        del cluster_evidence[drop_c]

    # Resolve canonical IDs and build CrosswalkEntries deterministically
    ordered_cluster_items: list[tuple[str, int, list[int]]] = []
    for cid_root, member_idxs in clusters.items():
        sorted_idxs = sorted(member_idxs, key=lambda idx: refs[idx].key)
        ordered_cluster_items.append((refs[sorted_idxs[0]].key, cid_root, sorted_idxs))
    ordered_cluster_items.sort(key=lambda item: item[0])

    claimed_cids: set[str] = set()
    entries: list[CrosswalkEntry] = []
    cluster_canonical_id: dict[int, str] = {}

    for _first_key, cid_root, sorted_idxs in ordered_cluster_items:
        member_keys = [refs[idx].key for idx in sorted_idxs]
        chosen_cid: str | None = None

        # 1. Explicit L0 confirmed_links ID takes top priority
        for a_i in range(len(member_keys)):
            for b_i in range(a_i + 1, len(member_keys)):
                pk = _pair_key(member_keys[a_i], member_keys[b_i])
                cand_cid = confirmed_cid_by_pair.get(pk)
                if cand_cid and cand_cid not in claimed_cids:
                    chosen_cid = cand_cid
                    break
            if chosen_cid is not None:
                break

        # 2. Otherwise reuse unclaimed prior canonical_id from existing_crosswalk
        if chosen_cid is None:
            for mkey in member_keys:
                prior_cid = prior_cid_by_member.get(mkey)
                if prior_cid and prior_cid not in claimed_cids:
                    chosen_cid = prior_cid
                    break

        # 3. Otherwise mint deterministic canonical_id
        if chosen_cid is None:
            chosen_cid = _deterministic_canonical_id(spec.canonical_id_prefix, member_keys)
            while chosen_cid in claimed_cids:
                chosen_cid = _deterministic_canonical_id(
                    spec.canonical_id_prefix, [*member_keys, chosen_cid]
                )

        claimed_cids.add(chosen_cid)
        cluster_canonical_id[cid_root] = chosen_cid

        entry_members = [copy.deepcopy(refs[idx]) for idx in sorted_idxs]
        golden = _resolve_golden_record(sorted_idxs, refs, snapshots, spec)
        entries.append(
            CrosswalkEntry(
                canonical_id=chosen_cid,
                members=entry_members,
                golden_record=golden,
                match_tier=cluster_tier[cid_root],  # type: ignore[arg-type]
                confidence=round(float(cluster_confidence[cid_root]), 6),
                evidence=list(cluster_evidence[cid_root]),
            )
        )

    # Post-clustering L4 candidate emission between unblocked distinct clusters
    cluster_pair_best: dict[
        tuple[int, int], tuple[float, int, int, list[str], list[str]]
    ] = {}
    blocked_cluster_pairs: set[tuple[int, int]] = set()
    for (lo, hi), (score, reasons, conflicts) in gray_pairs.items():
        ca = cluster_of[lo]
        cb = cluster_of[hi]
        if ca == cb:
            continue
        c_pair = (ca, cb) if ca < cb else (cb, ca)
        if c_pair in blocked_cluster_pairs:
            continue
        has_conflict = False
        for u in clusters[ca]:
            for v in clusters[cb]:
                if _pair_key(refs[u].key, refs[v].key) in hard_blocked_pairs:
                    has_conflict = True
                    break
            if has_conflict:
                break
        if has_conflict:
            blocked_cluster_pairs.add(c_pair)
            cluster_pair_best.pop(c_pair, None)
            continue

        prev = cluster_pair_best.get(c_pair)
        if prev is None or score > prev[0]:
            cluster_pair_best[c_pair] = (score, lo, hi, reasons, conflicts)

    ambiguous_candidates: list[EntityMatchCandidate] = []
    for _c_pair, (score, lo, hi, reasons, conflicts) in sorted(
        cluster_pair_best.items(),
        key=lambda item: (-item[1][0], refs[item[1][1]].key, refs[item[1][2]].key),
    ):
        ref_lo, ref_hi = refs[lo], refs[hi]
        cand_id = _deterministic_canonical_id("CAND-", [ref_lo.key, ref_hi.key])
        ambiguous_candidates.append(
            EntityMatchCandidate(
                candidate_id=cand_id,
                target_object_type=spec.target_object_type,
                left=copy.deepcopy(ref_lo),
                right=copy.deepcopy(ref_hi),
                left_snapshot=copy.deepcopy(snapshots[lo]),
                right_snapshot=copy.deepcopy(snapshots[hi]),
                score=score,
                match_tier="L4_AMBIGUOUS",
                reasons=list(reasons),
                conflicting_fields=list(conflicts),
            )
        )

    blocked_records.sort(key=lambda item: (item.left.key, item.right.key, item.reason_code))

    stats = {
        "total_input_records": num_records,
        "canonical_entities": len(entries),
        "merged_entities": sum(1 for e in entries if len(e.members) > 1),
        "singleton_entities": sum(1 for e in entries if len(e.members) == 1),
        "l0_confirmed_entities": sum(1 for e in entries if e.match_tier == "L0_CONFIRMED"),
        "l1_hard_key_entities": sum(1 for e in entries if e.match_tier == "L1_HARD_KEY"),
        "l2_l3_merged_entities": sum(1 for e in entries if e.match_tier == "L2_L3_SCORE"),
        "ambiguous_candidates": len(ambiguous_candidates),
        "blocked_pairs": len(blocked_records),
    }

    crosswalk = CrosswalkTable(
        target_object_type=spec.target_object_type,
        canonical_id_prefix=spec.canonical_id_prefix,
        entries=entries,
        confirmed_links=persisted_confirmed_links,
        negative_links=persisted_negative_links,
    )
    return EntityResolutionReport(
        crosswalk=crosswalk,
        ambiguous_candidates=ambiguous_candidates,
        blocked_pairs=blocked_records,
        stats=stats,
    )


def apply_resolution_decisions(
    crosswalk: CrosswalkTable,
    decisions: Sequence[EntityResolutionDecision],
) -> CrosswalkTable:
    """Immutably persist Steward CONFIRM_MERGE / REJECT_SEPARATE rulings onto a CrosswalkTable."""
    updated = copy.deepcopy(crosswalk)
    neg_set: set[tuple[str, str]] = {_pair_key(a, b) for a, b in updated.negative_links}
    conf_map: dict[tuple[str, str], str] = {
        _pair_key(a, b): cid for a, b, cid in updated.confirmed_links
    }

    for dec in decisions:
        pk = _pair_key(dec.left.key, dec.right.key)
        if dec.verdict == "CONFIRM_MERGE":
            neg_set.discard(pk)
            chosen_cid = (dec.preferred_canonical_id or "").strip()
            if not chosen_cid:
                chosen_cid = (
                    updated.resolve_canonical_id(dec.left.source_id, dec.left.raw_pk)
                    or updated.resolve_canonical_id(dec.right.source_id, dec.right.raw_pk)
                    or _deterministic_canonical_id(updated.canonical_id_prefix, list(pk))
                )
            conf_map[pk] = chosen_cid
        elif dec.verdict == "REJECT_SEPARATE":
            conf_map.pop(pk, None)
            neg_set.add(pk)
        else:
            raise ValueError(f"unsupported verdict {dec.verdict!r}")

    updated.confirmed_links = [(k1, k2, conf_map[(k1, k2)]) for k1, k2 in sorted(conf_map)]
    updated.negative_links = sorted(neg_set)
    return updated


def build_resolution_task_intent(candidate: EntityMatchCandidate) -> dict[str, Any]:
    """Build a pure domain-agnostic human-review task intent dict from an L4 candidate."""
    params = {
        "candidate_id": candidate.candidate_id,
        "target_object_type": candidate.target_object_type,
        "left": candidate.left.model_dump(mode="json"),
        "right": candidate.right.model_dump(mode="json"),
        "left_snapshot": copy.deepcopy(candidate.left_snapshot),
        "right_snapshot": copy.deepcopy(candidate.right_snapshot),
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "conflicting_fields": list(candidate.conflicting_fields),
    }
    return {
        "task_kind": "HUMAN_TASK",
        "intent_code": "RESOLVE_ENTITY_MATCH",
        "target_object_type": candidate.target_object_type,
        "target_object_id": candidate.candidate_id,
        "requested_operation": "resolve_entity_match",
        "suggested_assignee_role": "steward",
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "conflicting_fields": list(candidate.conflicting_fields),
        "left": candidate.left.model_dump(mode="json"),
        "right": candidate.right.model_dump(mode="json"),
        "params": params,
        "params_json": json.dumps(params, ensure_ascii=False, sort_keys=True),
    }


def inject_crosswalk_into_mapping(
    mapping: AdapterMappingSpec,
    *,
    source_id: str,
    crosswalk: CrosswalkTable,
    target_object_type: str | None = None,
    foreign_key_fields: Sequence[tuple[str, str]] = (),
) -> AdapterMappingSpec:
    """Immutably inject source alias mappings from a CrosswalkTable into an AdapterMappingSpec."""
    updated = copy.deepcopy(mapping)
    aliases = crosswalk.alias_map_for(source_id)
    if not aliases:
        return updated

    resolved_target = target_object_type or crosswalk.target_object_type
    fk_targets = set(foreign_key_fields)

    for em in updated.entity_mappings:
        if em.target_object_type == resolved_target:
            if em.id_transform.kind == "direct":
                em.id_transform.kind = "id_prefix"
                em.id_transform.prefix = ""
            if em.id_transform.kind in ("id_prefix", "enum_map"):
                em.id_transform.enum_map = {**em.id_transform.enum_map, **aliases}

        for tf in em.field_transforms:
            if (em.target_object_type, tf.target_field) in fk_targets:
                if tf.kind == "direct":
                    tf.kind = "foreign_key_id"
                    tf.prefix = ""
                if tf.kind in ("foreign_key_id", "id_prefix", "enum_map"):
                    tf.enum_map = {**tf.enum_map, **aliases}

    return updated
