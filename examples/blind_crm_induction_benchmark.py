"""Stage 2B: True Blind Cross-Vendor / Cross-Domain Benchmark (Real BottleCRM SQLite).

Runs the **100% FROZEN** ``open_ontology/induction/`` symbolic-training kernel (zero code or
prompt edits to ``open_ontology/induction/``) on an unseen domain and database engine:
``vendor_crm/bottlecrm.sqlite3`` (Django CRM 1.44.0, 118 SQLite tables, 32-char UUID PKs,
CRM pipeline + support escalation domain).

Explicit Rule Provenance Separation:
* **Mechanical DB Probing**: table/column schema, PK/FK topology, observed stage/status strings.
* **NL Policy & Runtime Friction Counterexamples**:
  1. Closing a high-value opportunity (``amount > 100000``) as lost requires ``steward`` authority;
  2. Winning an opportunity is blocked while its parent account has an open ``High``/``Urgent``
     support case (while ``Medium``/``Low`` cases do NOT trigger a false-positive block).

Run::
    python3 -m examples.blind_crm_induction_benchmark --offline
    python3 -m examples.blind_crm_induction_benchmark --model agy/gemini-4-neon-medium
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from open_ontology import (
    Actor,
    GovernanceViolationError,
    InvariantError,
    OntologyRuntime,
    PermissionDeniedError,
    SourceBridge,
)
from open_ontology.induction import (
    ActionBlueprint,
    ActionExecutionBinding,
    ActionMutationSpec,
    AdapterMappingSpec,
    AntigravityCLIAbductionBackend,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EntityMappingSpec,
    EvolutionPatch,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldSpec,
    FieldTransformSpec,
    FrictionClusterSummary,
    FrictionEvidence,
    IncidentCounterexample,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    OntologyEvolverAgent,
    OpenRouterAbductionBackend,
    ProposedEvalCase,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
    project_external_phenomenon,
    project_single_record,
)

from .business_ontology import (
    WorkCard,
    build_business_seed_ontology,
    register_work_card_primitives,
    settle_work_card,
)

ROOT = Path(__file__).resolve().parents[1]
BOTTLECRM_SQLITE_PATH = ROOT / "vendor_crm" / "bottlecrm.sqlite3"
CRM_AI_ACTOR = Actor(actor_id="crm_ai_ops", role="ai_agent")
CRM_STEWARD_ACTOR = Actor(actor_id="crm_steward", role="steward")

CRM_NOISE_COLUMNS = {
    "created_at",
    "updated_at",
    "created_by_id",
    "updated_by_id",
    "org_id",
    "kanban_order",
    "custom_fields",
    "alt_thread_ids",
    "sla_paused_at",
    "sla_paused_seconds",
    "is_sample",
    "merge_record",
    "escalation_count",
    "last_escalation_fired_at",
    "closed_by_id",
    "merged_by_id",
    "merged_into_id",
    "parent_id",
    "stage_id",
    "pipeline_id",
    "source_detail",
    "source_ref",
}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _induction_kernel_sha256() -> dict[str, str]:
    ind_dir = ROOT / "open_ontology" / "induction"
    return {p.name: _file_sha256(p) for p in sorted(ind_dir.glob("*.py"))}


def _log(msg: str) -> None:
    import sys

    print(f"[blind-crm] {msg}", file=sys.stderr, flush=True)


_CATEGORICAL_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,20}$")


def scout_bottlecrm_sqlite(db_path: Path) -> dict[str, Any]:
    """Unsupervised, zero-hardcoding SQLite metadata + relational subgraph scout."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        all_tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        domain_tables = [
            t for t in all_tables if not t.startswith("django_") and not t.startswith("auth_")
        ]

        cols_by_table: dict[str, list[Any]] = {}
        fks_by_table: dict[str, list[dict[str, str]]] = {}
        row_count_by_table: dict[str, int] = {}
        raw_total_cols = 0
        for tbl in domain_tables:
            cols = conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()
            cols_by_table[tbl] = cols
            raw_total_cols += len(cols)
            fks = conn.execute(f'PRAGMA foreign_key_list("{tbl}")').fetchall()
            fks_by_table[tbl] = [
                {"from_table": tbl, "from_col": str(fk["from"]), "to_table": str(fk["table"])}
                for fk in fks
            ]
            row_count_by_table[tbl] = int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0])

        # Unsupervised core entity subgraph: populated multi-row tables connected by non-self FKs
        multi_row_tables = {t for t in domain_tables if row_count_by_table[t] >= 2}
        multi_degree: dict[str, int] = {t: 0 for t in multi_row_tables}
        for tbl in multi_row_tables:
            for fk in fks_by_table[tbl]:
                tgt = fk["to_table"]
                if tgt in multi_row_tables and tgt != tbl:
                    multi_degree[tbl] += 1
                    multi_degree[tgt] += 1
        core_tables = sorted(t for t, deg in multi_degree.items() if deg >= 1)
        core_set = set(core_tables)
        core_fk_targets = {
            fk["to_table"] for tbl in core_tables for fk in fks_by_table[tbl] if fk["to_table"] != tbl
        }

        entities: list[ExternalEntityProfile] = []
        raw_rows_by_table: dict[str, list[dict[str, Any]]] = {}
        fk_edges: list[dict[str, str]] = []

        for tbl in core_tables:
            cols = cols_by_table[tbl]
            fk_by_col = {fk["from_col"]: fk["to_table"] for fk in fks_by_table[tbl]}
            for fk in fks_by_table[tbl]:
                if fk["to_table"] in core_set:
                    fk_edges.append(dict(fk))
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{tbl}" ORDER BY rowid').fetchall()]
            raw_rows_by_table[tbl] = rows

            fields: list[ExternalFieldProfile] = []
            for c in cols:
                cname = str(c["name"])
                ctype = str(c["type"] or "varchar").lower()
                if "int" in ctype or "bool" in ctype:
                    rtype = "integer"
                elif "real" in ctype or "dec" in ctype or "float" in ctype:
                    rtype = "float"
                else:
                    rtype = "string"
                enums: list[str] = []
                if rtype == "string" and int(c["pk"] or 0) == 0 and cname not in fk_by_col:
                    vals = [
                        str(r[cname])
                        for r in rows
                        if r.get(cname) is not None and str(r.get(cname)).strip() != ""
                    ]
                    distinct = sorted(set(vals))
                    if (
                        1 <= len(distinct) <= 10
                        and all(_CATEGORICAL_TOKEN_RE.match(v) for v in distinct)
                        and (len(distinct) < len(rows) or all(" " not in v for v in distinct))
                    ):
                        enums = distinct
                fields.append(
                    ExternalFieldProfile(
                        name=cname,
                        raw_type=rtype,
                        label=cname,
                        relation_target=fk_by_col.get(cname),
                        enum_values=enums,
                    )
                )
            entities.append(
                ExternalEntityProfile(
                    table_name=tbl,
                    row_count=len(rows),
                    fields=fields,
                    sample_rows=rows,
                )
            )

        # Unsupervised discovery of secondary multi-row vocabulary/code lookup tables
        discovered_lookup_codes: dict[str, list[str]] = {}
        for tbl in sorted(multi_row_tables - core_set):
            col_names = {str(c["name"]) for c in cols_by_table[tbl]}
            tbl_fk_targets = {fk["to_table"] for fk in fks_by_table[tbl]}
            if "code" in col_names and (tbl_fk_targets & core_fk_targets):
                order_col = '"order"' if "order" in col_names else "rowid"
                codes = [
                    str(r[0])
                    for r in conn.execute(f'SELECT code FROM "{tbl}" ORDER BY {order_col}').fetchall()
                    if r[0] is not None and str(r[0]).strip() != ""
                ]
                if codes:
                    discovered_lookup_codes[tbl] = codes

        profile = ExternalPhenomenonProfile(
            source_kind="discovered_crm",
            source_locator="discovered://crm",
            entities=entities,
        )
        return {
            "total_sqlite_tables": len(all_tables),
            "domain_table_count": len(domain_tables),
            "raw_domain_columns_total": raw_total_cols,
            "scouted_table_columns": sum(len(e.fields) for e in entities),
            "profile": profile,
            "raw_rows_by_table": raw_rows_by_table,
            "fk_edges": fk_edges,
            "discovered_lookup_codes": discovered_lookup_codes,
        }
    finally:
        conn.close()


def build_blind_crm_genesis_requirement_doc() -> str:
    """Build schema-free natural-language Genesis task brief without leaking column/table/action hints."""
    return (
        "All discovered entity schemas, structural foreign-key edges, lookup vocabularies, and sample rows "
        "are ALREADY provided in phenomenon_brief and external_profile below. Do NOT invoke any shell, file, "
        "search, or external tools; respond with pure JSON in a single turn.\n\n"
        "Synthesize a clean, semantically purified 0->1 DeclarativeBlueprint and complete bidirectional "
        "AdapterMappingSpec grounded on the SeedOntology:\n"
        "1. Map each discovered master and workflow entity in external_profile into clean PascalCase "
        "ObjectTypeBlueprints inheriting MasterEntity or ProcessDocument.\n"
        "2. Actively prune technical audit timestamps, user/tenant routing pointers, UI layout ordering, "
        "custom-field blobs, SLA timers, and merge/sync metadata columns, retaining only business-meaningful "
        "identifiers, foreign keys, monetary measures, priorities, and lifecycle states.\n"
        "3. Provide complete AdapterMappingSpec:\n"
        "   - Use kind='id_prefix' transforms on primary keys with distinct short prefixes per entity type, "
        "and align every foreign_key_id prefix with its target master entity prefix.\n"
        "   - Preserve discovered source enum values directly (kind='direct') without remapping.\n"
        "   - In this initial v1.0.0 Genesis, synthesize mechanical forward lifecycle actions for closing deals "
        "(won or lost) and resolving support tickets directly from any active non-terminal stage/status, allowing "
        "standard operational actor roles 'ai_agent' and 'steward', and reserving enterprise policy guardrails for "
        "Act 2 evolution.\n"
        "4. Emit self-contained ProposedEvalCase checks using synthetic setup_objects (if asserting "
        "expect.object_changes, always include 'object_id' matching the setup_object primary key, or assert "
        "expect={'valid': true/false} directly).\n"
        "5. Return a single root JSON object with exact top-level keys "
        "{'rationale', 'blueprint', 'adapter_mapping', 'proposed_eval_cases'} in one turn without calling any tools."
    )


def _build_sqlite_genesis_replay_factory(
    profile: ExternalPhenomenonProfile,
):
    """Generic M0 projection + action discriminability check (mirrors Odoo genesis factory)."""

    def _factory(
        _migrated: list[dict[str, Any]],
        candidate: DeclarativeBlueprint,
        adapter_mapping: AdapterMappingSpec | None,
    ) -> tuple[list[ReplayEvent], list[ReplayEvent]]:
        pos: list[ReplayEvent] = []
        neg: list[ReplayEvent] = []
        if adapter_mapping is None:
            return pos, neg

        projected, proj_errors = project_external_phenomenon(profile, adapter_mapping)
        if proj_errors or not projected:
            return pos, neg

        objs_by_type: dict[str, list[dict[str, Any]]] = {}
        for entry in projected:
            objs_by_type.setdefault(entry["object_type"], []).append(entry["data"])

        act_by_name = {a.name: a for a in candidate.actions}
        pk_by_type = {
            ot.name: (ot.primary_key or next((f.name for f in ot.fields if f.primary_key), "id"))
            for ot in candidate.object_types
        }
        for b in adapter_mapping.action_bindings:
            act = act_by_name.get(b.action_name)
            if act is None:
                continue
            pk = pk_by_type.get(b.target_object_type, b.id_param)
            rows = objs_by_type.get(b.target_object_type, [])
            if not rows:
                continue
            actor = CRM_AI_ACTOR
            ot_obj = next((o for o in candidate.object_types if o.name == b.target_object_type), None)
            sf = (b.expected_state_field or (ot_obj.status_field if ot_obj else "")) or ""
            term_states: set[str] = set()
            if ot_obj and ot_obj.transitions:
                term_states.update(st for st, nxts in ot_obj.transitions.items() if not nxts)
            if b.expected_state_after:
                term_states.add(b.expected_state_after)

            seen_states: set[str] = set()
            checked_steward = False
            for r in rows:
                rid = str(r.get(pk) or "")
                if not rid:
                    continue
                cur_st = str(r.get(sf) or "") if sf else ""
                if sf and cur_st in term_states:
                    neg.append(
                        ReplayEvent(
                            name=f"GEN-NEG-M0-{act.name}-{rid[:8]}",
                            action=act.name,
                            params={b.id_param: rid},
                            actor=actor,
                            expected_valid=False,
                        )
                    )
                    continue
                if cur_st in seen_states and seen_states:
                    continue
                seen_states.add(cur_st)
                pos.append(
                    ReplayEvent(
                        name=f"GEN-POS-{act.name}-{cur_st or rid[:6]}",
                        action=act.name,
                        params={b.id_param: rid},
                        actor=actor,
                        expected_valid=True,
                    )
                )
                if not checked_steward:
                    checked_steward = True
                    pos.append(
                        ReplayEvent(
                            name=f"GEN-POS-STEWARD-{act.name}",
                            action=act.name,
                            params={b.id_param: rid},
                            actor=CRM_STEWARD_ACTOR,
                            expected_valid=True,
                        )
                    )

            terminal_val = b.expected_state_after or (next(iter(term_states), "") if term_states else "")
            if sf and terminal_val and rows:
                src_row = rows[0]
                term_data = {**src_row, pk: f"{b.id_prefix}TERM-NEG", sf: terminal_val}
                neg.append(
                    ReplayEvent(
                        name=f"GEN-NEG-TERMINAL-{act.name}",
                        action=act.name,
                        params={b.id_param: f"{b.id_prefix}TERM-NEG"},
                        actor=actor,
                        setup_objects=({"object_type": b.target_object_type, "data": term_data},),
                        expected_valid=False,
                    )
                )
        return pos, neg

    return _factory


def _make_offline_crm_backends(scout_info: dict[str, Any]):
    """Deterministic reference backends ONLY for offline unit-test verification (--offline)."""
    v1_bp = DeclarativeBlueprint(
        model_id="bottlecrm_ontology",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="Purified BottleCRM Account -> Opportunity & SupportCase governance ontology.",
        object_types=[
            ObjectTypeBlueprint(
                name="Account",
                archetype="MasterEntity",
                primary_key="account_id",
                fields=[
                    FieldSpec(name="account_id", field_type="str", primary_key=True),
                    FieldSpec(name="name", field_type="str", default=""),
                    FieldSpec(name="industry", field_type="str", default=""),
                    FieldSpec(name="annual_revenue", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="status", field_type="str", default="ACTIVE"),
                ],
            ),
            ObjectTypeBlueprint(
                name="Opportunity",
                archetype="ProcessDocument",
                primary_key="opportunity_id",
                status_field="stage",
                initial_statuses=["PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION"],
                transitions={
                    "PROSPECTING": ["QUALIFICATION", "PROPOSAL", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"],
                    "QUALIFICATION": ["PROPOSAL", "NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"],
                    "PROPOSAL": ["NEGOTIATION", "CLOSED_WON", "CLOSED_LOST"],
                    "NEGOTIATION": ["CLOSED_WON", "CLOSED_LOST"],
                    "CLOSED_WON": [],
                    "CLOSED_LOST": [],
                },
                fields=[
                    FieldSpec(name="opportunity_id", field_type="str", primary_key=True),
                    FieldSpec(name="name", field_type="str", default=""),
                    FieldSpec(name="stage", field_type="str", default="PROSPECTING"),
                    FieldSpec(name="amount", field_type="float", default=0.0, non_negative=True),
                    FieldSpec(name="probability", field_type="int", default=0),
                ],
                relations=[
                    RelationSpec(field="account_id", target_type="Account", required=True),
                ],
            ),
            ObjectTypeBlueprint(
                name="SupportCase",
                archetype="ProcessDocument",
                primary_key="case_id",
                status_field="status",
                initial_statuses=["New", "In Progress"],
                transitions={
                    "New": ["In Progress", "Resolved", "Closed"],
                    "In Progress": ["Resolved", "Closed"],
                    "Resolved": ["Closed"],
                    "Closed": [],
                },
                fields=[
                    FieldSpec(name="case_id", field_type="str", primary_key=True),
                    FieldSpec(name="name", field_type="str", default=""),
                    FieldSpec(name="status", field_type="str", default="New"),
                    FieldSpec(name="priority", field_type="str", default="Medium"),
                    FieldSpec(name="case_type", field_type="str", default="Problem"),
                ],
                relations=[
                    RelationSpec(field="account_id", target_type="Account", required=True),
                ],
            ),
        ],
        actions=[
            ActionBlueprint(
                name="win_opportunity",
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                params=[FieldSpec(name="opportunity_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="opp_open_to_win",
                        code="OPPORTUNITY_ALREADY_CLOSED",
                        target_type="Opportunity",
                        id_param="opportunity_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.stage"},
                            "right": {"lit": ["PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION"]},
                        },
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Opportunity",
                        id_param="opportunity_id",
                        set_fields={"stage": {"lit": "CLOSED_WON"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="close_lost_opportunity",
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                params=[FieldSpec(name="opportunity_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="opp_open_to_lose",
                        code="OPPORTUNITY_ALREADY_CLOSED",
                        target_type="Opportunity",
                        id_param="opportunity_id",
                        must_hold={
                            "op": "in",
                            "left": {"ref": "obj.stage"},
                            "right": {"lit": ["PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION"]},
                        },
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="Opportunity",
                        id_param="opportunity_id",
                        set_fields={"stage": {"lit": "CLOSED_LOST"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="resolve_support_case",
                allowed_roles=["ai_agent", "ops", "steward", "admin"],
                params=[FieldSpec(name="case_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="case_must_be_open",
                        code="CASE_ALREADY_RESOLVED",
                        target_type="SupportCase",
                        id_param="case_id",
                        must_hold={"op": "in", "left": {"ref": "obj.status"}, "right": {"lit": ["New", "In Progress"]}},
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="SupportCase",
                        id_param="case_id",
                        set_fields={"status": {"lit": "Resolved"}},
                    )
                ],
            ),
        ],
    )
    v1_map = AdapterMappingSpec(
        mapping_id="bottlecrm_sqlite_v1",
        source_kind="sqlite_django_crm",
        entity_mappings=[
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="Account",
                id_transform=FieldTransformSpec(target_field="account_id", source_field="id", kind="id_prefix", prefix="ACCT-"),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="industry", source_field="industry", kind="direct", default=""),
                    FieldTransformSpec(target_field="annual_revenue", source_field="annual_revenue", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="ACTIVE"),
                ],
            ),
            EntityMappingSpec(
                source_table="opportunity",
                target_object_type="Opportunity",
                id_transform=FieldTransformSpec(target_field="opportunity_id", source_field="id", kind="id_prefix", prefix="OPP-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account_id", source_field="account_id", kind="foreign_key_id", prefix="ACCT-"),
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="stage", source_field="stage", kind="direct", default="PROSPECTING"),
                    FieldTransformSpec(target_field="amount", source_field="amount", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="probability", source_field="probability", kind="direct", default=0),
                ],
            ),
            EntityMappingSpec(
                source_table="case",
                target_object_type="SupportCase",
                id_transform=FieldTransformSpec(target_field="case_id", source_field="id", kind="id_prefix", prefix="CASE-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account_id", source_field="account_id", kind="foreign_key_id", prefix="ACCT-"),
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="status", source_field="status", kind="direct", default="New"),
                    FieldTransformSpec(target_field="priority", source_field="priority", kind="direct", default="Medium"),
                    FieldTransformSpec(target_field="case_type", source_field="case_type", kind="direct", default="Problem"),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="win_opportunity",
                external_table="opportunity",
                external_method="stage=CLOSED_WON",
                target_object_type="Opportunity",
                id_param="opportunity_id",
                id_prefix="OPP-",
                expected_state_field="stage",
                expected_state_after="CLOSED_WON",
            ),
            ActionExecutionBinding(
                action_name="close_lost_opportunity",
                external_table="opportunity",
                external_method="stage=CLOSED_LOST",
                target_object_type="Opportunity",
                id_param="opportunity_id",
                id_prefix="OPP-",
                expected_state_field="stage",
                expected_state_after="CLOSED_LOST",
            ),
            ActionExecutionBinding(
                action_name="resolve_support_case",
                external_table="case",
                external_method="status=Resolved",
                target_object_type="SupportCase",
                id_param="case_id",
                id_prefix="CASE-",
                expected_state_field="status",
                expected_state_after="Resolved",
            ),
        ],
    )

    def _gen_fn(_s: str, _u: str, _r: int) -> dict[str, Any]:
        return {
            "blueprint": v1_bp.model_dump(mode="json"),
            "adapter_mapping": v1_map.model_dump(mode="json"),
            "proposed_eval_cases": [
                {
                    "name": "CRM-GEN-WIN-EVAL",
                    "action": "win_opportunity",
                    "params": {"opportunity_id": "OPP-EVAL-1"},
                    "actor": {"actor_id": "eval:ai", "role": "ai_agent"},
                    "setup_objects": [
                        {"object_type": "Account", "data": {"account_id": "ACCT-EVAL-1", "name": "Eval Co", "annual_revenue": 1000.0, "status": "ACTIVE"}},
                        {"object_type": "Opportunity", "data": {"opportunity_id": "OPP-EVAL-1", "account_id": "ACCT-EVAL-1", "name": "Deal", "stage": "NEGOTIATION", "amount": 10000.0, "probability": 70}},
                    ],
                    "expect": {"valid": True},
                }
            ],
        }

    win_act = next(copy.deepcopy(a) for a in v1_bp.actions if a.name == "win_opportunity")
    win_act.preconditions.append(
        InvariantRuleSpec(
            name="no_open_high_or_urgent_support_cases",
            code="ACCOUNT_HAS_OPEN_CRITICAL_CASE",
            target_type="Opportunity",
            id_param="opportunity_id",
            must_hold={
                "op": "not_exists",
                "object_type": "SupportCase",
                "where": {
                    "op": "and",
                    "args": [
                        {"op": "eq", "left": {"ref": "target.account_id"}, "right": {"ref": "obj.account_id"}},
                        {"op": "in", "left": {"ref": "target.priority"}, "right": {"lit": ["High", "Urgent"]}},
                        {"op": "not_in", "left": {"ref": "target.status"}, "right": {"lit": ["Resolved", "Closed"]}},
                    ],
                },
            },
            description="Cannot close-won opportunity while customer account has an open High/Urgent support case.",
        )
    )
    lost_act = next(copy.deepcopy(a) for a in v1_bp.actions if a.name == "close_lost_opportunity")
    lost_act.preconditions.append(
        InvariantRuleSpec(
            name="high_value_loss_requires_steward",
            code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
            target_type="Opportunity",
            id_param="opportunity_id",
            when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 100000.0}},
            must_hold={"op": "in", "left": {"ref": "actor.role"}, "right": {"lit": ["steward", "admin"]}},
            description="Closing opportunities above 100,000 as lost requires steward or admin authority.",
        )
    )
    patch = EvolutionPatch(
        patch_id="PATCH-CRM-L1-POLICIES",
        base_version="1.0.0",
        target_version="1.1.0",
        rationale="Add CRM critical-case blocker check on win and steward guard on >100k close-lost.",
        update_actions=[win_act, lost_act],
        proposed_eval_cases=[
            ProposedEvalCase(
                name="CRM-EVAL-ALLOW-MEDIUM-CASE-WIN",
                actor={"actor_id": "eval:ai", "role": "ai_agent"},
                action="win_opportunity",
                params={"opportunity_id": "OPP-EV-MED"},
                setup_objects=[
                    {"object_type": "Account", "data": {"account_id": "ACCT-EV-MED", "name": "MedCo", "annual_revenue": 1000.0, "status": "ACTIVE"}},
                    {"object_type": "Opportunity", "data": {"opportunity_id": "OPP-EV-MED", "account_id": "ACCT-EV-MED", "name": "DealMed", "stage": "PROPOSAL", "amount": 5000.0, "probability": 60}},
                    {"object_type": "SupportCase", "data": {"case_id": "CASE-EV-MED", "account_id": "ACCT-EV-MED", "name": "Minor question", "status": "In Progress", "priority": "Medium", "case_type": "Question"}},
                ],
                expect={"valid": True},
            )
        ],
    )

    def _evo_fn(_s: str, _u: str, _r: int) -> dict[str, Any]:
        return {"patch": patch.model_dump(mode="json")}

    _ = scout_info
    return CallableAbductionBackend(_gen_fn), CallableAbductionBackend(_evo_fn)


def _find_crm_action_bindings(
    adapter_mapping: AdapterMappingSpec,
) -> tuple[ActionExecutionBinding, ActionExecutionBinding, ActionExecutionBinding]:
    """Tolerantly resolve opportunity win/lost and support-case resolution bindings."""
    opp_bindings = [b for b in adapter_mapping.action_bindings if b.external_table == "opportunity"]
    case_bindings = [b for b in adapter_mapping.action_bindings if b.external_table == "case"]
    assert len(opp_bindings) >= 2 and case_bindings, (
        f"missing expected opportunity/case bindings: {adapter_mapping.action_bindings}"
    )

    def _sig(b: ActionExecutionBinding) -> str:
        return f"{b.action_name} {b.external_method} {b.expected_state_after or ''}".lower()

    win_b = next(b for b in opp_bindings if any(k in _sig(b) for k in ("win", "won", "closed_won")))
    lost_b = next(b for b in opp_bindings if b is not win_b and any(k in _sig(b) for k in ("lost", "closed_lost", "lose_")))
    res_b = case_bindings[0]
    return win_b, lost_b, res_b


def _sqlite_homomorphic_settlement_check(
    *,
    source_sqlite: Path,
    profile: ExternalPhenomenonProfile,
    blueprint: DeclarativeBlueprint,
    adapter_mapping: AdapterMappingSpec,
    raw_rows_by_table: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Execute crystallized CRM v1.1.0 against an ephemeral temp copy of bottlecrm.sqlite3."""
    seed = build_business_seed_ontology()
    spec = compile_blueprint(blueprint, seed_ontology=seed)
    register_work_card_primitives(
        spec,
        opener_roles=("ai_agent", "ops", "steward", "admin"),
        resolver_roles=("steward", "admin"),
    )
    projected, _ = project_external_phenomenon(profile, adapter_mapping)

    opp_em = next(em for em in adapter_mapping.entity_mappings if em.source_table == "opportunity")
    case_em = next(em for em in adapter_mapping.entity_mappings if em.source_table == "case")
    win_b, lost_b, res_b = _find_crm_action_bindings(adapter_mapping)
    case_act = next((a for a in blueprint.actions if a.name == res_b.action_name), None)
    case_mut_val = next(
        (
            m.set_fields["status"].get("lit")
            for m in (case_act.mutations if case_act else [])
            if isinstance(m.set_fields.get("status"), dict) and isinstance(m.set_fields["status"].get("lit"), str)
        ),
        None,
    )
    case_done_val = str(res_b.expected_state_after or case_mut_val or "Resolved")

    opp_rows = {r["name"]: r for r in raw_rows_by_table["opportunity"]}
    case_rows = {r["name"]: r for r in raw_rows_by_table["case"]}
    acme_opp_id = str(opp_rows["Acme 产线升级一期"]["id"])
    nw_opp_id = str(opp_rows["Northwind 年度框架采购"]["id"])
    south_opp_id = str(opp_rows["Southstar 影像设备替换"]["id"])
    acme_case_id = str(case_rows["Acme 到货外包装破损"]["id"])

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = Path(tmpdir) / "bottlecrm_sandbox.sqlite3"
        shutil.copy2(source_sqlite, tmp_db)
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        rt = OntologyRuntime(spec, db_path=":memory:", world_mode="coupled")
        try:
            for item in projected:
                cls = spec.get_object_type(item["object_type"])
                rt.put_object(cls(**item["data"]))

            def _make_crm_bridge(
                binding: ActionExecutionBinding,
                em: EntityMappingSpec,
                raw_uuid: str,
                col_name: str,
                new_val: str,
            ) -> SourceBridge:
                cid = f"{binding.id_prefix}{raw_uuid}"

                def _actuate(mv: dict[str, Any]) -> None:
                    valid_cols = {
                        str(r[1])
                        for r in conn.execute(f'PRAGMA table_info("{binding.external_table}")').fetchall()
                    }
                    updates: dict[str, Any] = {}
                    for ch in (mv.get("object_diff") or {}).get("object_changes") or []:
                        if ch.get("object_type") != binding.target_object_type or ch.get("object_id") != cid:
                            continue
                        before = dict(ch.get("before") or {})
                        after = dict(ch.get("after") or {})
                        for ft in em.field_transforms:
                            tf = ft.target_field
                            sf = ft.source_field
                            if tf not in after or after[tf] == before.get(tf) or sf not in valid_cols:
                                continue
                            val = after[tf]
                            if ft.kind == "direct":
                                updates[sf] = int(val) if isinstance(val, bool) else val
                            elif ft.kind == "enum_map":
                                inv = {v: k for k, v in (ft.enum_map or {}).items()}
                                updates[sf] = inv.get(str(val), val)
                            elif ft.kind in ("id_prefix", "foreign_key_id") and ft.prefix:
                                sval = str(val)
                                updates[sf] = sval[len(ft.prefix) :] if sval.startswith(ft.prefix) else sval
                    if not updates and col_name in valid_cols:
                        updates[col_name] = new_val
                    if updates:
                        set_sql = ", ".join(f'"{c}"=?' for c in updates)
                        conn.execute(
                            f'UPDATE "{binding.external_table}" SET {set_sql} WHERE id=?',
                            (*updates.values(), raw_uuid),
                        )
                        conn.commit()

                def _observe(_mv: dict[str, Any]) -> dict[str, Any]:
                    raw_after = dict(
                        conn.execute(
                            f'SELECT * FROM "{binding.external_table}" WHERE id=?',
                            (raw_uuid,),
                        ).fetchone()
                    )
                    proj_after = project_single_record(em, raw_after)
                    return {binding.target_object_type: [proj_after]}

                return SourceBridge(actuate=_actuate, observe=_observe)

            def _try_crm_action(
                run_id: str,
                binding: ActionExecutionBinding,
                em: EntityMappingSpec,
                raw_uuid: str,
                actor: Actor,
                col_name: str,
                new_val: str,
            ) -> tuple[str, str | None, str]:
                cid = f"{binding.id_prefix}{raw_uuid}"
                rt.bind_source_bridge(
                    _make_crm_bridge(binding, em, raw_uuid, col_name, new_val),
                    world_mode="coupled",
                )
                try:
                    run = rt.submit_action(run_id, binding.action_name, {binding.id_param: cid}, actor)
                    status = str(run["status"])
                    err_code = run.get("error_code")
                except (InvariantError, PermissionDeniedError, GovernanceViolationError) as exc:
                    status = "REJECTED"
                    err_code = getattr(exc, "code", None) or type(exc).__name__

                db_val = str(
                    conn.execute(
                        f'SELECT "{col_name}" FROM "{binding.external_table}" WHERE id=?',
                        (raw_uuid,),
                    ).fetchone()[0]
                )
                return status, err_code, db_val

            # 1. Adversarial: ai_agent tries to win Acme deal while Acme High support case is open -> MUST BLOCK & 0 SQLite write
            st_acme_blk, err_acme_blk, db_acme_blk = _try_crm_action(
                "CRM-1-ACME-BLOCKED", win_b, opp_em, acme_opp_id, CRM_AI_ACTOR, "stage", "CLOSED_WON"
            )
            # 2. Adversarial: ai_agent tries to close-lost Southstar 260,000 deal -> MUST BLOCK & 0 SQLite write
            st_south_blk, err_south_blk, db_south_blk = _try_crm_action(
                "CRM-2-SOUTH-BLOCKED", lost_b, opp_em, south_opp_id, CRM_AI_ACTOR, "stage", "CLOSED_LOST"
            )
            # 3. Control: ai_agent wins Northwind (account has only Medium open case) -> MUST COMMIT & write SQLite
            st_nw_ok, err_nw_ok, db_nw_ok = _try_crm_action(
                "CRM-3-NW-OK", win_b, opp_em, nw_opp_id, CRM_AI_ACTOR, "stage", "CLOSED_WON"
            )
            # 4. Sequential: resolve Acme High case -> now Acme opportunity wins & settles homomorphically
            st_case_res, err_case_res, db_case_res = _try_crm_action(
                "CRM-4-RESOLVE-CASE", res_b, case_em, acme_case_id, CRM_AI_ACTOR, "status", case_done_val
            )
            st_acme_win, err_acme_win, db_acme_win = _try_crm_action(
                "CRM-5-ACME-WIN-AFTER", win_b, opp_em, acme_opp_id, CRM_AI_ACTOR, "stage", "CLOSED_WON"
            )
            # 5. Steward approval flow via M1 WorkCard for blocked Southstar 260k loss
            assert all(r["status"] in {"COMMITTED", "REJECTED", "REVERTED"} for r in rt.list_action_runs())
            south_cid = f"{lost_b.id_prefix}{south_opp_id}"
            rt.bind_source_bridge(
                _make_crm_bridge(lost_b, opp_em, south_opp_id, "stage", "CLOSED_LOST"),
                world_mode="coupled",
            )
            rt.submit_action(
                "CRM-6-SOUTH-WC-OPEN",
                "open_work_card",
                {
                    "card_id": "WC-CRM-SOUTH-LOSS",
                    "card_kind": "APPROVAL",
                    "target_object_type": opp_em.target_object_type,
                    "target_object_id": south_cid,
                    "requested_action": lost_b.action_name,
                    "requested_params_json": json.dumps({lost_b.id_param: south_cid}),
                    "assignee_role": "steward",
                },
                CRM_AI_ACTOR,
            )
            south_settled = settle_work_card(
                rt,
                "WC-CRM-SOUTH-LOSS",
                CRM_STEWARD_ACTOR,
                resolution_note="steward approved 260k close-lost in SQLite sandbox",
            )
            db_south_after = str(
                conn.execute(
                    f'SELECT "stage" FROM "{lost_b.external_table}" WHERE id=?',
                    (south_opp_id,),
                ).fetchone()[0]
            )
            wc_south_done = rt.get_object(WorkCard, "WC-CRM-SOUTH-LOSS", CRM_STEWARD_ACTOR)
            assert all(r["status"] in {"COMMITTED", "REJECTED", "REVERTED"} for r in rt.list_action_runs())

            return {
                "acme_blocked_while_high_case_open": {
                    "passed": st_acme_blk == "REJECTED" and db_acme_blk == "NEGOTIATION",
                    "settled": st_acme_blk,
                    "error_code": err_acme_blk,
                    "sqlite_stage_after": db_acme_blk,
                },
                "southstar_260k_loss_blocked_for_ai": {
                    "passed": st_south_blk == "REJECTED" and db_south_blk == "QUALIFICATION",
                    "settled": st_south_blk,
                    "error_code": err_south_blk,
                    "sqlite_stage_after": db_south_blk,
                },
                "northwind_medium_case_allows_win": {
                    "passed": st_nw_ok == "COMMITTED" and db_nw_ok == "CLOSED_WON",
                    "settled": st_nw_ok,
                    "error_code": err_nw_ok,
                    "sqlite_stage_after": db_nw_ok,
                },
                "acme_resolve_case_then_win": {
                    "passed": (
                        st_case_res == "COMMITTED"
                        and db_case_res == case_done_val
                        and st_acme_win == "COMMITTED"
                        and db_acme_win == "CLOSED_WON"
                    ),
                    "case_settled": st_case_res,
                    "case_sqlite_status": db_case_res,
                    "opp_settled": st_acme_win,
                    "opp_sqlite_stage": db_acme_win,
                },
                "southstar_work_card_steward_settlement": {
                    "passed": (
                        south_settled["status"] == "COMMITTED"
                        and south_settled["settled_action_run"]["status"] == "COMMITTED"
                        and wc_south_done.status == "COMPLETED"
                        and db_south_after == "CLOSED_LOST"
                    ),
                    "work_card_status": wc_south_done.status,
                    "sqlite_stage_after": db_south_after,
                    "pending_approval_runs": sum(1 for r in rt.list_action_runs() if r["status"] not in {"COMMITTED", "REJECTED", "REVERTED"}),
                },
            }
        finally:
            rt.close()
            conn.close()


def run_blind_crm_induction_benchmark(
    *,
    sqlite_path: Path = BOTTLECRM_SQLITE_PATH,
    offline: bool = False,
    model: str = "agy/gemini-4-neon-medium",
    max_rounds: int = 4,
) -> dict[str, Any]:
    """Blind cross-vendor / cross-domain induction benchmark on BottleCRM SQLite."""
    kernel_sha_before = _induction_kernel_sha256()
    db_sha_before = _file_sha256(sqlite_path)

    scout_info = scout_bottlecrm_sqlite(sqlite_path)
    profile: ExternalPhenomenonProfile = scout_info["profile"]
    raw_rows_by_table: dict[str, list[dict[str, Any]]] = scout_info["raw_rows_by_table"]
    seed = build_business_seed_ontology()

    if offline:
        gen_backend, evo_backend = _make_offline_crm_backends(scout_info)
        model_label = "offline-mock-crm"
    elif model.startswith("openrouter/"):
        or_slug = model[len("openrouter/") :]
        gen_backend = OpenRouterAbductionBackend(model=or_slug, timeout_sec=240)
        evo_backend = OpenRouterAbductionBackend(model=or_slug, timeout_sec=240)
        model_label = model
    else:
        slug = model[len("agy/") :] if model.startswith("agy/") else model
        gen_backend = AntigravityCLIAbductionBackend(model=slug, timeout_sec=240)
        evo_backend = AntigravityCLIAbductionBackend(model=slug, timeout_sec=240)
        model_label = model

    opp_rows = {r["name"]: r for r in raw_rows_by_table["opportunity"]}
    case_rows = {r["name"]: r for r in raw_rows_by_table["case"]}

    req_doc = build_blind_crm_genesis_requirement_doc()

    with InductionGovernanceService(db_path=":memory:") as gov:
        _log(f"Act 1 (Genesis): inducing BottleCRM ontology v1.0.0 with {model_label}")
        t0 = time.monotonic()
        builder = OntologyBuilderAgent(
            backend=gen_backend,
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=seed,
        )
        gen_res = builder.run_genesis_cycle(
            model_id="bottlecrm_ontology",
            phenomenon_brief={
                "discovered_entities": [e.table_name for e in profile.entities],
                "discovered_relations": scout_info["fk_edges"],
                "discovered_lookup_codes": scout_info["discovered_lookup_codes"],
            },
            requirement_doc=req_doc,
            external_profile=profile,
            crucible_context=CrucibleContext(
                mode="genesis",
                replay_factory=_build_sqlite_genesis_replay_factory(profile),
                default_actor=CRM_AI_ACTOR,
                seed_ontology=seed,
            ),
            seed_ontology=seed,
            max_rounds=max_rounds,
        )
        gen_elapsed = round(time.monotonic() - t0, 2)
        if not gen_res.verified or gen_res.pending_approval_run_id is None:
            raise RuntimeError(f"BottleCRM Genesis failed:\n{gen_res.final_report.diagnostic_feedback()}")
        gov.approve_crystallization(gen_res.pending_approval_run_id, STEWARD_ACTOR)
        bp_v1 = gov.get_active_blueprint("bottlecrm_ontology")
        map_v1 = gov.get_active_adapter_mapping("bottlecrm_ontology")
        assert bp_v1 is not None and map_v1 is not None

        # Measure purified schema compression vs scouted BottleCRM columns
        bp_fields_all = [f.name for ot in bp_v1.object_types for f in ot.fields]
        noise_retained = [f for f in bp_fields_all if f in CRM_NOISE_COLUMNS]

        # Project real BottleCRM SQLite rows through crystallized v1 adapter mapping
        projected_m0, proj_errs = project_external_phenomenon(profile, map_v1)
        assert not proj_errs, proj_errs

        opp_em = next(em for em in map_v1.entity_mappings if em.source_table == "opportunity")
        case_em = next(em for em in map_v1.entity_mappings if em.source_table == "case")
        win_b, lost_b, res_b = _find_crm_action_bindings(map_v1)

        acme_opp_cid = f"{opp_em.id_transform.prefix}{opp_rows['Acme 产线升级一期']['id']}"
        nw_opp_cid = f"{opp_em.id_transform.prefix}{opp_rows['Northwind 年度框架采购']['id']}"
        south_opp_cid = f"{opp_em.id_transform.prefix}{opp_rows['Southstar 影像设备替换']['id']}"
        acme_case_cid = f"{case_em.id_transform.prefix}{case_rows['Acme 到货外包装破损']['id']}"

        cluster = FrictionClusterSummary(
            cluster_id="CRM-GOV-L1-POLICIES",
            signal_kind="INVARIANT_GUARDRAIL_GAP",
            action_type=win_b.action_name,
            signature="critical_open_case_blocker+high_value_loss_authority",
            support_count=2,
            run_ids=("CRM-FRIC-1", "CRM-FRIC-2"),
            escalated=True,
            evidences=(
                FrictionEvidence(
                    run_id="CRM-FRIC-1",
                    signal_kind="NL_GOVERNANCE_POLICY",
                    action_type=win_b.action_name,
                    actor_role="ai_agent",
                    signature="critical_open_case_blocker",
                    params={win_b.id_param: acme_opp_cid},
                    summary=(
                        f"Enterprise Sales Governance Policy: automated agents must not mark a deal closed-won "
                        f"({win_b.action_name}) while the linked customer still has an active unresolved urgent or "
                        f"high-priority support ticket open (blocked on {acme_opp_cid}, while allowing {nw_opp_cid} "
                        f"whose open ticket is only medium priority)."
                    ),
                    error_code="ACCOUNT_HAS_OPEN_CRITICAL_CASE",
                ),
                FrictionEvidence(
                    run_id="CRM-FRIC-2",
                    signal_kind="NL_GOVERNANCE_POLICY",
                    action_type=lost_b.action_name,
                    actor_role="ai_agent",
                    signature="high_value_loss_authority",
                    params={lost_b.id_param: south_opp_cid},
                    summary=(
                        f"Enterprise Revenue Loss Control Policy: automated agents must not mark a deal closed-lost "
                        f"({lost_b.action_name}) when commercial deal value exceeds 100,000; high-value losses require "
                        f"human steward or admin authority (blocked {south_opp_cid} valued at 260,000 for ai_agent, allowed for steward)."
                    ),
                    error_code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
                ),
            ),
        )

        _log(f"Act 2 (L1 Evolution): evolving BottleCRM policies v1.0.0 -> v1.1.0 with {model_label}")
        t1 = time.monotonic()
        evolver = OntologyEvolverAgent(
            backend=evo_backend,
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=seed,
        )
        evo_res = evolver.run_evolution_cycle(
            model_id="bottlecrm_ontology",
            cluster=cluster,
            crucible_context=CrucibleContext(
                mode="evolution",
                m0_objects=projected_m0,
                positive_events=[
                    ReplayEvent(
                        name="CRM-POS-WIN-MEDIUM-CASE-ALLOWED",
                        action=win_b.action_name,
                        params={win_b.id_param: nw_opp_cid},
                        actor=CRM_AI_ACTOR,
                        expected_valid=True,
                    ),
                    ReplayEvent(
                        name="CRM-POS-RESOLVE-CASE",
                        action=res_b.action_name,
                        params={res_b.id_param: acme_case_cid},
                        actor=CRM_AI_ACTOR,
                        expected_valid=True,
                    ),
                    ReplayEvent(
                        name="CRM-POS-STEWARD-HIGH-LOSS",
                        action=lost_b.action_name,
                        params={lost_b.id_param: south_opp_cid},
                        actor=CRM_STEWARD_ACTOR,
                        expected_valid=True,
                    ),
                ],
                incident_counterexamples=[
                    IncidentCounterexample(
                        name="CRM-CX-BLOCKER-CASE",
                        action=win_b.action_name,
                        params={win_b.id_param: acme_opp_cid},
                        actor=CRM_AI_ACTOR,
                        expect_valid=False,
                    ),
                    IncidentCounterexample(
                        name="CRM-CX-HIGH-VALUE-LOSS",
                        action=lost_b.action_name,
                        params={lost_b.id_param: south_opp_cid},
                        actor=CRM_AI_ACTOR,
                        expect_valid=False,
                    ),
                ],
                default_actor=CRM_AI_ACTOR,
                seed_ontology=seed,
            ),
            seed_ontology=seed,
            max_rounds=max_rounds,
        )
        evo_elapsed = round(time.monotonic() - t1, 2)
        if not evo_res.verified or evo_res.pending_approval_run_id is None:
            raise RuntimeError(f"BottleCRM Evolution failed:\n{evo_res.final_report.diagnostic_feedback()}")
        gov.approve_crystallization(evo_res.pending_approval_run_id, STEWARD_ACTOR)
        bp_v11 = gov.get_active_blueprint("bottlecrm_ontology")
        map_v11 = gov.get_active_adapter_mapping("bottlecrm_ontology")
        assert bp_v11 is not None and map_v11 is not None

    settlement = _sqlite_homomorphic_settlement_check(
        source_sqlite=sqlite_path,
        profile=profile,
        blueprint=bp_v11,
        adapter_mapping=map_v11,
        raw_rows_by_table=raw_rows_by_table,
    )
    kernel_sha_after = _induction_kernel_sha256()
    db_sha_after = _file_sha256(sqlite_path)

    return {
        "mode": "offline-mock-crm" if offline else f"live-blind-crm:{model_label}",
        "source_sqlite": str(sqlite_path.relative_to(ROOT)),
        "source_sqlite_unmodified": db_sha_before == db_sha_after,
        "induction_kernel_frozen": kernel_sha_before == kernel_sha_after,
        "induction_kernel_sha256": kernel_sha_after,
        "rule_provenance": {
            "mechanical_db_probing": [
                f"{scout_info['total_sqlite_tables']} SQLite tables scanned ({scout_info['domain_table_count']} domain tables)",
                "PK/FK edges & observed pipeline stage strings extracted read-only",
            ],
            "nl_policy_and_friction_counterexamples": [
                "Critical-priority (High/Urgent) open SupportCase blocks win_opportunity on the same Account (Medium allowed)",
                "High-value (>100,000) close_lost_opportunity requires steward authority",
            ],
        },
        "raw_crm_schema": {
            "total_sqlite_tables": scout_info["total_sqlite_tables"],
            "raw_domain_columns_total": scout_info["raw_domain_columns_total"],
            "scouted_3table_columns": scout_info["scouted_table_columns"],
            "real_m0_rows_grounded": len(projected_m0),
        },
        "act1_genesis": {
            "verified": gen_res.verified,
            "rounds_used": len(gen_res.rounds),
            "elapsed_sec": gen_elapsed,
            "version": bp_v1.version,
            "purified_entities": [ot.name for ot in bp_v1.object_types],
            "purified_field_count": len(bp_fields_all),
            "column_compression_pct": round(
                100.0 * (scout_info["scouted_table_columns"] - len(bp_fields_all)) / max(1, scout_info["scouted_table_columns"]),
                2,
            ),
            "noise_columns_retained": noise_retained,
            "actions": [a.name for a in bp_v1.actions],
        },
        "act2_l1_evolution": {
            "verified": evo_res.verified,
            "rounds_used": len(evo_res.rounds),
            "elapsed_sec": evo_elapsed,
            "version": bp_v11.version,
        },
        "act3_sqlite_homomorphic_settlement": settlement,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Stage 2B: True Blind Cross-Domain BottleCRM Induction Benchmark"
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--model", default="agy/gemini-4-neon-medium")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    out = run_blind_crm_induction_benchmark(
        offline=args.offline,
        model=args.model,
        max_rounds=args.max_rounds,
    )
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
