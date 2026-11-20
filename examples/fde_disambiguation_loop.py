"""P2-2: AI-FDE 0.7 -> 1.0 Human-in-the-Loop Gray-Zone Disambiguation Gateway.

Connects:
1. ``OdooFDEScout`` ``AMBIGUOUS_GRAY_ZONE`` findings (plus SOP ambiguous clauses)
   -> ``GrayZoneAmbiguity`` candidates grounded to live M0 records;
2. Workshop ``UIArtifact(kind="gray_zone_ruling_card", ...)`` and Feishu interactive
   Steward ruling cards (``feishu_card_bridge.build_feishu_gray_zone_ruling_card``);
3. Steward callback -> ``compile_disambiguation_to_evolution_patch`` (producing
   ``GoldenEvalCase`` + L1 ``EvolutionPatch`` with role-conditional ``APPROVAL_REQUIRED_*``
   or hard ``precondition``) -> 5-gate ``SymbolicCrucible`` -> ``InductionGovernanceService``
   crystallization + Feishu terminal card patch.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from open_ontology import Actor, PermissionDeniedError
from open_ontology.osdk import UIArtifact
from open_ontology.induction import (
    AdapterMappingSpec,
    CrucibleContext,
    DeclarativeBlueprint,
    DisambiguationChoice,
    ExternalPhenomenonProfile,
    GrayZoneAmbiguity,
    InductionGovernanceService,
    STEWARD_ACTOR,
    SeedOntology,
    StewardDisambiguationDecision,
    SymbolicCrucible,
    compile_disambiguation_to_evolution_patch,
    crystallize_disambiguated_patch,
    project_external_phenomenon,
)

from .business_ontology import build_business_seed_ontology
from .feishu_card_bridge import (
    FeishuOpenAPIClient,
    build_feishu_gray_zone_ruling_card,
    parse_feishu_ruling_callback,
)
from .odoo_fde_scout import FDEScoutReport

_SOP_HEDGE_RE = re.compile(
    r"(原则上|视情况|必要时|可特批|subject to review|unless urgent|case-by-case|exceptional)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<!\d)(\d[\d,]*(?:\.\d+)?)")


def _find_action_for_table(
    adapter_mapping: AdapterMappingSpec,
    blueprint: DeclarativeBlueprint,
    *,
    source_table: str,
    prefer_reversal: bool = False,
) -> tuple[str, str, str] | None:
    """Return (target_object_type, action_name, id_param) for an external source table, or None."""
    target_ems = [em for em in adapter_mapping.entity_mappings if em.source_table == source_table]
    target_types = {em.target_object_type for em in target_ems}
    bindings = [
        b
        for b in adapter_mapping.action_bindings
        if b.external_table == source_table and b.target_object_type in target_types
    ]
    if not bindings:
        bindings = [b for b in adapter_mapping.action_bindings if b.target_object_type in target_types]
    if not bindings:
        return None

    def _is_rev(name: str) -> bool:
        low = name.lower()
        return any(tok in low for tok in ("cancel", "draft", "reverse", "refund", "void"))

    if prefer_reversal:
        chosen = next((b for b in bindings if _is_rev(b.action_name)), bindings[0])
    else:
        chosen = next((b for b in bindings if not _is_rev(b.action_name)), bindings[0])

    # Verify id_param exists on blueprint action
    act = next(a for a in blueprint.actions if a.name == chosen.action_name)
    id_param = chosen.id_param or (act.params[0].name if act.params else "record_id")
    return chosen.target_object_type, chosen.action_name, id_param


def _map_source_field_to_m1_rel(
    adapter_mapping: AdapterMappingSpec,
    blueprint: DeclarativeBlueprint,
    *,
    target_object_type: str,
    source_field: str,
) -> tuple[str, str]:
    """Return (rel_field_name, parent_target_type) for a parent M2O relation."""
    em = adapter_mapping.get_entity_mapping(target_object_type)
    ot = next(o for o in blueprint.object_types if o.name == target_object_type)
    rel_map = {r.field: r.target_type for r in ot.relations}
    if em is not None:
        for tf in em.field_transforms:
            if tf.source_field == source_field and tf.target_field in rel_map:
                return tf.target_field, rel_map[tf.target_field]
    if source_field in rel_map:
        return source_field, rel_map[source_field]
    if ot.relations:
        return ot.relations[0].field, ot.relations[0].target_type
    raise ValueError(f"cannot resolve parent relation {source_field!r} on {target_object_type!r}")


def _map_source_field_to_m1_field(
    adapter_mapping: AdapterMappingSpec,
    blueprint: DeclarativeBlueprint,
    *,
    target_object_type: str,
    source_field: str,
) -> str:
    em = adapter_mapping.get_entity_mapping(target_object_type)
    if em is not None:
        for tf in em.field_transforms:
            if tf.source_field == source_field and tf.kind == "direct":
                return tf.target_field
    ot = next(o for o in blueprint.object_types if o.name == target_object_type)
    fnames = {f.name for f in ot.fields}
    if source_field in fnames:
        return source_field
    for f in ot.fields:
        if f.field_type in ("float", "int") and f.name != ot.primary_key:
            return f.name
    return source_field


def extract_scout_gray_zone_ambiguities(
    *,
    scout_report: FDEScoutReport,
    profile: ExternalPhenomenonProfile,
    blueprint: DeclarativeBlueprint,
    adapter_mapping: AdapterMappingSpec,
    sop_text: str = "",
) -> list[GrayZoneAmbiguity]:
    """Convert Probe 1 AMBIGUOUS_GRAY_ZONE findings (+ optional SOP clauses) into GrayZoneAmbiguity items."""
    projected, _ = project_external_phenomenon(profile, adapter_mapping)
    rows_by_type: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in projected:
        ot_name = str(entry["object_type"])
        data = dict(entry["data"])
        ot_bp = next((o for o in blueprint.object_types if o.name == ot_name), None)
        pk = ot_bp.primary_key if ot_bp else "record_id"
        rows_by_type.setdefault(ot_name, {})[str(data[pk])] = data

    out: list[GrayZoneAmbiguity] = []
    for finding in scout_report.epistemic_findings:
        if finding.epistemic_tier != "AMBIGUOUS_GRAY_ZONE":
            continue

        if finding.kind == "CROSS_ENTITY_LIMIT_EXCEEDANCE_CONTRADICTION":
            ev = dict(finding.evidence)
            src_measure = str(ev["measure_field"])
            src_parent_rel = str(ev["parent_relation"])
            parent_table, src_limit_field = str(ev["parent_limit_field"]).rsplit(".", 1)

            found_act = _find_action_for_table(
                adapter_mapping, blueprint, source_table=finding.model, prefer_reversal=False
            )
            if found_act is None:
                continue
            tgt_ot, tgt_act, id_param = found_act
            rel_field, parent_ot = _map_source_field_to_m1_rel(
                adapter_mapping,
                blueprint,
                target_object_type=tgt_ot,
                source_field=src_parent_rel,
            )
            m1_measure = _map_source_field_to_m1_field(
                adapter_mapping,
                blueprint,
                target_object_type=tgt_ot,
                source_field=src_measure,
            )
            m1_limit = _map_source_field_to_m1_field(
                adapter_mapping,
                blueprint,
                target_object_type=parent_ot,
                source_field=src_limit_field,
            )

            ot_bp = next(o for o in blueprint.object_types if o.name == tgt_ot)
            sf = ot_bp.status_field
            act_bp = next(a for a in blueprint.actions if a.name == tgt_act)
            valid_initial_states: set[str] = set()
            for pre in act_bp.preconditions:
                rnode = pre.must_hold.get("right", {})
                if isinstance(rnode, dict) and isinstance(rnode.get("lit"), list):
                    valid_initial_states.update(str(s) for s in rnode["lit"])

            parent_rows = rows_by_type.get(parent_ot, {})
            child_rows = rows_by_type.get(tgt_ot, {})
            sample_exc_id: str | None = None
            sample_ok_id: str | None = None
            for cid, crow in sorted(child_rows.items()):
                if valid_initial_states and str(crow.get(sf)) not in valid_initial_states:
                    continue
                pid = str(crow.get(rel_field) or "")
                prow = parent_rows.get(pid)
                if prow is None:
                    continue
                mv = float(crow.get(m1_measure) or 0.0)
                lv = float(prow.get(m1_limit) or 0.0)
                if lv > 0 and mv > lv and sample_exc_id is None:
                    sample_exc_id = cid
                elif lv > 0 and 0 < mv <= lv and sample_ok_id is None:
                    sample_ok_id = cid

            predicate_ast = {
                "op": "and",
                "args": [
                    {"op": "gt", "left": {"ref": f"obj.{rel_field}.{m1_limit}"}, "right": {"lit": 0}},
                    {"op": "gt", "left": {"ref": f"obj.{m1_measure}"}, "right": {"ref": f"obj.{rel_field}.{m1_limit}"}},
                ],
            }
            sop_clause = ""
            if sop_text:
                for line in sop_text.splitlines():
                    if _SOP_HEDGE_RE.search(line):
                        sop_clause = line.strip()
                        break

            out.append(
                GrayZoneAmbiguity(
                    ambiguity_id=finding.finding_id,
                    kind="limit_exceedance",
                    target_object_type=tgt_ot,
                    target_action=tgt_act,
                    id_param=id_param,
                    summary=finding.summary,
                    sop_excerpt=sop_clause,
                    predicate_ast=predicate_ast,
                    error_code=f"{m1_limit.upper()}_EXCEEDED",
                    approver_roles=["steward", "admin"],
                    counterexample_samples=list(ev.get("sample_records") or []),
                    sample_exceeding_id=sample_exc_id,
                    sample_compliant_id=sample_ok_id,
                    recommended_choice="REQUIRE_APPROVAL",
                )
            )

        elif finding.kind == "STATE_REVERSAL_OR_CANCELLATION_LOOP":
            ev = dict(finding.evidence)
            found_rev = _find_action_for_table(
                adapter_mapping, blueprint, source_table=finding.model, prefer_reversal=True
            )
            if found_rev is None:
                continue
            tgt_ot, tgt_act, id_param = found_rev
            ot_bp = next(o for o in blueprint.object_types if o.name == tgt_ot)
            sf = ot_bp.status_field
            act_bp = next(a for a in blueprint.actions if a.name == tgt_act)
            allowed_from: list[str] = []
            for pre in act_bp.preconditions:
                rnode = pre.must_hold.get("right", {})
                if isinstance(rnode, dict) and isinstance(rnode.get("lit"), list):
                    allowed_from.extend(str(s) for s in rnode["lit"])
            child_rows = rows_by_type.get(tgt_ot, {})
            sample_id = next(
                (
                    cid
                    for cid, crow in sorted(child_rows.items())
                    if not allowed_from or str(crow.get(sf)) in allowed_from
                ),
                None,
            )
            check_states = allowed_from or [str(child_rows[sample_id].get(sf))] if sample_id else ["active"]
            predicate_ast = {"op": "in", "left": {"ref": f"obj.{sf}"}, "right": {"lit": check_states}}
            out.append(
                GrayZoneAmbiguity(
                    ambiguity_id=finding.finding_id,
                    kind="state_reversal",
                    target_object_type=tgt_ot,
                    target_action=tgt_act,
                    id_param=id_param,
                    summary=finding.summary,
                    sop_excerpt="",
                    predicate_ast=predicate_ast,
                    error_code=f"REVERSAL_REQUIRES_AUTHORITY_{tgt_ot.upper()}",
                    approver_roles=["steward", "admin"],
                    counterexample_samples=[{"reversal_edges": "; ".join(ev.get("reversal_edges") or [])}],
                    sample_exceeding_id=sample_id,
                    sample_compliant_id=None,
                    recommended_choice="REQUIRE_APPROVAL",
                )
            )

    if sop_text:
        out.extend(
            extract_sop_gray_zone_ambiguities(
                sop_text=sop_text,
                blueprint=blueprint,
                m0_objects=projected,
                existing_ids={a.ambiguity_id for a in out},
            )
        )
    return out


def extract_sop_gray_zone_ambiguities(
    *,
    sop_text: str,
    blueprint: DeclarativeBlueprint,
    m0_objects: Sequence[dict[str, Any]] = (),
    existing_ids: set[str] | None = None,
) -> list[GrayZoneAmbiguity]:
    """Extract numeric-threshold / modal SOP gray-zone clauses directly bound to blueprint actions."""
    seen = set(existing_ids or set())
    results: list[GrayZoneAmbiguity] = []
    rows_by_type: dict[str, dict[str, dict[str, Any]]] = {}
    for item in m0_objects:
        ot_name = str(item["object_type"])
        data = dict(item["data"])
        ot_bp = next((o for o in blueprint.object_types if o.name == ot_name), None)
        pk = ot_bp.primary_key if ot_bp else "record_id"
        rows_by_type.setdefault(ot_name, {})[str(data[pk])] = data

    for idx, raw_line in enumerate(sop_text.splitlines(), start=1):
        clause = raw_line.strip()
        if not clause or not _SOP_HEDGE_RE.search(clause):
            continue
        num_match = _NUMBER_RE.search(clause)
        if not num_match:
            continue
        threshold = float(num_match.group(1).replace(",", ""))
        # Find forward action with numeric measure field on its primary target type
        chosen_act: ActionBlueprint | None = None
        chosen_ot: Any = None
        measure_field = ""
        for act in blueprint.actions:
            if not act.mutations:
                continue
            tgt_name = act.mutations[0].object_type
            ot_bp = next((o for o in blueprint.object_types if o.name == tgt_name), None)
            if ot_bp is None:
                continue
            num_fields = [f.name for f in ot_bp.fields if f.field_type in ("float", "int") and f.name != ot_bp.primary_key]
            if num_fields:
                chosen_act = act
                chosen_ot = ot_bp
                measure_field = num_fields[0]
                break
        if chosen_act is None or chosen_ot is None:
            continue

        amb_id = f"SOP-AMB-{idx}"
        if amb_id in seen:
            continue
        id_param = chosen_act.params[0].name if chosen_act.params else "record_id"
        pred_ast = {"op": "gt", "left": {"ref": f"obj.{measure_field}"}, "right": {"lit": threshold}}
        child_rows = rows_by_type.get(chosen_ot.name, {})
        exc_id = next((cid for cid, d in sorted(child_rows.items()) if float(d.get(measure_field) or 0.0) > threshold), None)
        ok_id = next((cid for cid, d in sorted(child_rows.items()) if 0 < float(d.get(measure_field) or 0.0) <= threshold), None)
        results.append(
            GrayZoneAmbiguity(
                ambiguity_id=amb_id,
                kind="sop_ambiguity",
                target_object_type=chosen_ot.name,
                target_action=chosen_act.name,
                id_param=id_param,
                summary=f"SOP modal clause requires Steward disambiguation for {chosen_ot.name}.{measure_field} > {threshold}",
                sop_excerpt=clause,
                predicate_ast=pred_ast,
                error_code=f"SOP_THRESHOLD_{int(threshold)}_EXCEEDED",
                approver_roles=["steward", "admin"],
                counterexample_samples=[{"threshold": threshold, "measure_field": measure_field, "sample_exceeding_id": exc_id}],
                sample_exceeding_id=exc_id,
                sample_compliant_id=ok_id,
                recommended_choice="REQUIRE_APPROVAL",
            )
        )
    return results


@dataclass
class RulingCardRecord:
    card_id: str
    ambiguity: GrayZoneAmbiguity
    card_view: dict[str, Any]
    feishu_card: dict[str, Any]
    ui_artifact: UIArtifact
    message_id: str | None = None
    crystallized_version: str | None = None


class GrayZoneDisambiguationGateway:
    """Routes AMBIGUOUS_GRAY_ZONE items through Steward Feishu/Workshop cards into 5-Gate crystallization."""

    def __init__(
        self,
        *,
        governance: InductionGovernanceService,
        model_id: str,
        blueprint: DeclarativeBlueprint,
        crucible_context: CrucibleContext,
        seed_ontology: SeedOntology | None = None,
        feishu_client: FeishuOpenAPIClient | None = None,
        operator_actors: dict[str, Actor] | None = None,
        default_steward: Actor = STEWARD_ACTOR,
        callback_token: str = "",
        ui_sink: list[UIArtifact] | None = None,
    ) -> None:
        self.governance = governance
        self.model_id = model_id
        self.blueprint = blueprint
        self.crucible_context = crucible_context
        self.seed_ontology = seed_ontology or build_business_seed_ontology()
        self.feishu_client = feishu_client or FeishuOpenAPIClient()
        self.operator_actors = dict(operator_actors or {})
        self.default_steward = default_steward
        self.callback_token = callback_token
        self.ui_sink = ui_sink if ui_sink is not None else []
        self.crucible = SymbolicCrucible()
        self._records: dict[str, RulingCardRecord] = {}

    def publish_ruling_card(self, ambiguity: GrayZoneAmbiguity, *, card_id: str | None = None) -> RulingCardRecord:
        cid = card_id or f"RULING-{ambiguity.ambiguity_id}-{uuid.uuid4().hex[:6]}"
        card_view = {
            "card_id": cid,
            "ambiguity_id": ambiguity.ambiguity_id,
            "model_id": self.model_id,
            "title": f"{ambiguity.target_object_type} · {ambiguity.target_action}",
            "kind": ambiguity.kind,
            "summary": ambiguity.summary,
            "sop_excerpt": ambiguity.sop_excerpt,
            "target_object_type": ambiguity.target_object_type,
            "target_action": ambiguity.target_action,
            "samples": list(ambiguity.counterexample_samples),
            "options": [o.model_dump(mode="json") for o in ambiguity.options],
            "recommended_choice": ambiguity.recommended_choice,
            "status": "PENDING_RULING",
        }
        feishu_card = build_feishu_gray_zone_ruling_card(card_view, callback_token=self.callback_token)
        msg_id: str | None = None
        if self.feishu_client.is_configured:
            msg_id = self.feishu_client.send_card(feishu_card)

        ui_art = UIArtifact(
            artifact_id=cid,
            kind="approval_card",
            title=feishu_card["header"]["title"]["content"],
            payload={
                "card_subtype": "gray_zone_ruling",
                "card_view": card_view,
                "feishu_card": feishu_card,
                "message_id": msg_id,
            },
        )
        self.ui_sink.append(ui_art)
        rec = RulingCardRecord(
            card_id=cid,
            ambiguity=ambiguity,
            card_view=card_view,
            feishu_card=feishu_card,
            ui_artifact=ui_art,
            message_id=msg_id,
        )
        self._records[cid] = rec
        return rec

    def _resolve_steward(self, operator_open_id: str) -> Actor:
        if operator_open_id and operator_open_id in self.operator_actors:
            return self.operator_actors[operator_open_id]
        return self.default_steward

    def handle_ruling_callback(
        self,
        payload: dict[str, Any],
        *,
        target_version: str = "1.1.0",
        rationale: str = "",
    ) -> dict[str, Any]:
        parsed = parse_feishu_ruling_callback(payload, callback_token=self.callback_token)
        rec = self._records.get(parsed["card_id"])
        if rec is None:
            raise KeyError(f"unknown ruling card {parsed['card_id']!r}")
        steward = self._resolve_steward(parsed["operator_open_id"])
        if steward.role not in ("steward", "admin"):
            raise PermissionDeniedError(
                f"actor role {steward.role!r} cannot finalize Steward disambiguation ruling"
            )

        choice: DisambiguationChoice = parsed["choice_id"]  # type: ignore[assignment]
        decision = StewardDisambiguationDecision(
            ambiguity_id=rec.ambiguity.ambiguity_id,
            choice_id=choice,
            decided_by=steward,
            rationale=rationale,
        )
        patch = compile_disambiguation_to_evolution_patch(
            base_blueprint=self.blueprint,
            target_version=target_version,
            ambiguities=[rec.ambiguity],
            decisions=[decision],
        )
        crystallized_bp, report, golden_cases = crystallize_disambiguated_patch(
            governance=self.governance,
            crucible=self.crucible,
            model_id=self.model_id,
            base_blueprint=self.blueprint,
            patch=patch,
            crucible_context=self.crucible_context,
            ambiguities=[rec.ambiguity],
            seed_ontology=self.seed_ontology,
            steward=steward,
        )
        assert report.passed, f"Disambiguation 5-gate Crucible failed:\n{report.diagnostic_feedback()}"
        self.blueprint = crystallized_bp

        rec.card_view["status"] = "CRYSTALLIZED"
        rec.card_view["decided_choice"] = choice
        rec.card_view["decided_by"] = steward.actor_id
        rec.card_view["crystallized_version"] = crystallized_bp.version
        rec.crystallized_version = crystallized_bp.version
        rec.feishu_card = build_feishu_gray_zone_ruling_card(
            rec.card_view, callback_token=self.callback_token
        )
        if self.feishu_client.is_configured and rec.message_id:
            self.feishu_client.patch_card(rec.message_id, rec.feishu_card)

        resolved_art = UIArtifact(
            artifact_id=f"{rec.card_id}-RESOLVED",
            kind="approval_card",
            title=rec.feishu_card["header"]["title"]["content"],
            payload={
                "card_subtype": "gray_zone_ruling",
                "card_view": dict(rec.card_view),
                "feishu_card": rec.feishu_card,
                "patch_id": patch.patch_id,
                "golden_eval_case_count": len(golden_cases),
            },
        )
        rec.ui_artifact = resolved_art
        self.ui_sink.append(resolved_art)

        return {
            "card_id": rec.card_id,
            "choice_id": choice,
            "version": crystallized_bp.version,
            "patch": patch,
            "blueprint": crystallized_bp,
            "crucible_passed": report.passed,
            "golden_eval_cases": golden_cases,
            "card": rec.feishu_card,
            "ui_artifact": resolved_art,
        }
