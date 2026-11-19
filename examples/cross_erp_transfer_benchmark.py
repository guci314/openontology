"""Stage 2A: Zero-Shot Cross-Instance Generalization Benchmark (erp_north -> erp_south).

Freezes an ontology (DeclarativeBlueprint + AdapterMappingSpec) induced exclusively on
``erp_north`` (with zero prior exposure to ``erp_south``), verifies SHA-256 immutability,
and evaluates zero-shot transfer onto the independent ``erp_south`` ERP instance across:
1. Read-only M0 historical record grounding on ``erp_south`` sample records;
2. Full 12-probe physical ledger execution on cloned ``erp_south`` via
   ``EphemeralOdooSandbox(source_db="erp_south")`` + ``evaluate_autonomous_fde_regime``.

Notice: ``open_ontology/induction/`` remains 100% frozen (verified by SHA-256 checksums).

Run::
    python3 -m examples.cross_erp_transfer_benchmark --offline
    python3 -m examples.cross_erp_transfer_benchmark --v2-bundle /path/to/north_v2_bundle.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from open_ontology import OntologyRuntime
from open_ontology.induction import (
    AdapterMappingSpec,
    DeclarativeBlueprint,
    apply_adapter_mapping_patch,
    apply_evolution_patch,
    compile_blueprint,
    toposort_entity_mappings,
)
from open_ontology.induction.phenomenon import project_single_record

from .business_ontology import build_business_seed_ontology
from .live_llm_cegis_experiment import run_live_llm_l2_architect_experiment
from .odoo_dual_erp_demo import DB_NORTH, DB_SOUTH, _jsonrpc
from .odoo_fde_autonomous_pipeline import (
    build_closed_phenomenon_profile,
    synthesize_from_fde_evidence,
)
from .odoo_fde_prober import EphemeralOdooSandbox, OdooActiveProber
from .odoo_fde_scout import OdooFDEScout, ReadOnlyOdooClient
from .odoo_utility_benchmark import (
    OfflineOdooPhysicalWorld,
    PhysicalOdooHarness,
    _row_matches_filter,
    build_reference_l1_patch_for_autonomous_v1,
    build_reference_l2_patch_for_autonomous_v11,
    evaluate_autonomous_fde_regime,
    evaluate_raw_odoo_baseline,
)


def _induction_kernel_sha256() -> dict[str, str]:
    ind_dir = Path(__file__).resolve().parents[1] / "open_ontology" / "induction"
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(ind_dir.glob("*.py"))
    }


def _freeze_pair_sha256(
    blueprint: DeclarativeBlueprint,
    adapter_mapping: AdapterMappingSpec,
) -> str:
    payload = json.dumps(
        {
            "blueprint": blueprint.model_dump(mode="json"),
            "adapter_mapping": adapter_mapping.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_disjoint_south_offline_world() -> OfflineOdooPhysicalWorld:
    """Create an offline mirror with disjoint South ID space (9000+) & naming conventions."""
    w = OfflineOdooPhysicalWorld()
    id_map = {1: 9001, 10: 9010, 11: 9011, 12: 9012, 20: 9020, 30: 9030, 31: 9031, 32: 9032}
    order_name_map = {"S00010": "SO-SOUTH-9010", "S00011": "SO-SOUTH-9011", "S00012": "SO-SOUTH-9012"}

    def _remap_m2o(val: Any) -> Any:
        if isinstance(val, list) and val and isinstance(val[0], int) and val[0] in id_map:
            new_id = id_map[val[0]]
            label = order_name_map.get(str(val[1]), f"South {val[1]}")
            return [new_id, label]
        return val

    remapped_tables: dict[str, dict[int, dict[str, Any]]] = {}
    for tbl, rows in w.tables.items():
        remapped_tables[tbl] = {}
        for old_id, row in rows.items():
            new_id = id_map.get(old_id, old_id + 9000)
            nr = copy.deepcopy(row)
            nr["id"] = new_id
            if tbl == "res.partner":
                nr["name"] = f"South {nr['name']}"
            elif tbl == "sale.order":
                nr["name"] = order_name_map.get(str(nr["name"]), f"SO-SOUTH-{new_id}")
                nr["partner_id"] = _remap_m2o(nr.get("partner_id"))
            elif tbl == "stock.picking":
                nr["name"] = f"SOUTH/WH/{new_id:05d}"
                nr["origin"] = order_name_map.get(str(nr.get("origin")), nr.get("origin"))
                nr["sale_id"] = _remap_m2o(nr.get("sale_id"))
                nr["partner_id"] = _remap_m2o(nr.get("partner_id"))
            elif tbl == "account.move":
                nr["name"] = f"SOUTH-INV/{new_id:05d}"
                nr["invoice_origin"] = order_name_map.get(str(nr.get("invoice_origin")), nr.get("invoice_origin"))
                nr["partner_id"] = _remap_m2o(nr.get("partner_id"))
            remapped_tables[tbl][new_id] = nr

    w.tables = remapped_tables
    w._next_id = 9100
    w._tracking_msgs = [
        {"id": 91, "model": "sale.order", "res_id": 9012},
        {"id": 92, "model": "account.move", "res_id": 9031},
        {"id": 93, "model": "stock.picking", "res_id": 9020},
    ]
    return w


def _evaluate_south_m0_grounding(
    *,
    client: Any,
    source_locator: str,
    blueprint: DeclarativeBlueprint,
    adapter_mapping: AdapterMappingSpec,
    seed: Any,
) -> dict[str, Any]:
    """Read-only M0 historical grounding check of frozen North ontology on South ERP rows."""
    scout_south = OdooFDEScout(client, source_locator=source_locator, max_models=6).scout()
    south_profile = build_closed_phenomenon_profile(scout_south, client=client)
    spec = compile_blueprint(blueprint, seed_ontology=seed)
    rt = OntologyRuntime(spec, db_path=":memory:")
    grounding_errors: list[str] = []
    counts_by_type: dict[str, int] = {}

    try:
        rows_by_table: dict[str, dict[int, dict[str, Any]]] = {}
        ids_by_table: dict[str, set[int]] = {}
        for ent in south_profile.entities:
            for sr in ent.sample_rows:
                rid = sr.get("id")
                if isinstance(rid, int) and not isinstance(rid, bool):
                    ids_by_table.setdefault(ent.table_name, set()).add(rid)

        prefix_to_table = {
            em.id_transform.prefix: em.source_table
            for em in adapter_mapping.entity_mappings
            if em.id_transform.prefix
        }

        def _fetch(tbl: str) -> None:
            em = next((e for e in adapter_mapping.entity_mappings if e.source_table == tbl), None)
            if em is None:
                return
            ids = sorted(ids_by_table.get(tbl, set()))
            if not ids:
                return
            fields = list(
                dict.fromkeys(
                    ["id", em.id_transform.source_field]
                    + [tf.source_field for tf in em.field_transforms if tf.source_field]
                    + [fb for tf in em.field_transforms for fb in tf.fallback_fields if fb]
                    + [flt.field for flt in em.row_filters if flt.field]
                )
            )
            missing_ids = [i for i in ids if i not in rows_by_table.get(tbl, {})]
            if missing_ids:
                fetched = client.search_read(tbl, [("id", "in", missing_ids)], fields, limit=len(missing_ids))
                tbl_map = rows_by_table.setdefault(tbl, {})
                for r in fetched:
                    tbl_map[int(r["id"])] = r

        for em in adapter_mapping.entity_mappings:
            _fetch(em.source_table)
        # Referentially close parent FK IDs cleanly across South tables
        for _ in range(2):
            added = False
            for em in adapter_mapping.entity_mappings:
                for row in list(rows_by_table.get(em.source_table, {}).values()):
                    for tf in em.field_transforms:
                        if tf.kind != "foreign_key_id" or not tf.prefix or not tf.source_field:
                            continue
                        tgt = prefix_to_table.get(tf.prefix)
                        if not tgt or tgt == em.source_table:
                            continue
                        raw = row.get(tf.source_field)
                        pid = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
                        if isinstance(pid, int) and not isinstance(pid, bool):
                            if pid not in ids_by_table.setdefault(tgt, set()):
                                ids_by_table[tgt].add(pid)
                                added = True
            if not added:
                break
            for em in adapter_mapping.entity_mappings:
                _fetch(em.source_table)

        ordered_ems, topo_errs = toposort_entity_mappings(adapter_mapping.entity_mappings)
        grounding_errors.extend(topo_errs)
        lookup_index: dict[tuple[str, str, str], str] = {}
        for em in ordered_ems:
            cls = spec.get_object_type(em.target_object_type)
            for rid in sorted(rows_by_table.get(em.source_table, {})):
                raw_row = rows_by_table[em.source_table][rid]
                if any(not _row_matches_filter(raw_row, flt) for flt in em.row_filters):
                    continue
                proj = project_single_record(
                    em,
                    raw_row,
                    lookup_index=lookup_index,
                    treat_false_as_null=True,
                )
                if any(proj.get(rf) is None for rf in em.drop_if_missing_refs):
                    continue
                cid = str(proj[em.id_transform.target_field])
                for rk, rv in raw_row.items():
                    if rv is not None and rv is not False and not isinstance(rv, (list, dict)):
                        lookup_index[(em.source_table, str(rk), str(rv))] = cid
                try:
                    rt.put_object(cls(**proj))
                    counts_by_type[em.target_object_type] = counts_by_type.get(em.target_object_type, 0) + 1
                except Exception as exc:
                    grounding_errors.append(
                        f"{em.source_table}:{rid} -> {em.target_object_type}: {type(exc).__name__}: {exc}"
                    )
    finally:
        rt.close()

    return {
        "source_locator": source_locator,
        "scout_discovered_models": [m.model for m in scout_south.discovered_models],
        "projected_counts_by_type": counts_by_type,
        "total_projected_objects": sum(counts_by_type.values()),
        "grounding_errors": grounding_errors,
    }


def _induce_north_v2_offline() -> tuple[DeclarativeBlueprint, AdapterMappingSpec]:
    world = OfflineOdooPhysicalWorld()
    seed = build_business_seed_ontology()
    sr = OdooFDEScout(world, source_locator="offline://erp_north", max_models=6).scout()
    profile = build_closed_phenomenon_profile(sr, client=world)
    pr = OdooActiveProber(world, sandbox_db="offline_north").probe(sr)
    bundle = synthesize_from_fde_evidence(
        sr, pr, profile=profile, seed_ontology=seed, model_id="autonomous_o2c_fde", version="1.0.0"
    )
    l1_patch = build_reference_l1_patch_for_autonomous_v1(bundle.blueprint, bundle.adapter_mapping, target_version="1.1.0")
    bp_v11 = apply_evolution_patch(bundle.blueprint, l1_patch)
    l2_patch = build_reference_l2_patch_for_autonomous_v11(bp_v11, bundle.adapter_mapping, target_version="2.0.0")
    bp_v2 = apply_evolution_patch(bp_v11, l2_patch)
    map_v2 = apply_adapter_mapping_patch(bundle.adapter_mapping, l2_patch)
    assert map_v2 is not None
    return bp_v2, map_v2


def run_cross_erp_transfer_benchmark(
    *,
    north_db: str = DB_NORTH,
    south_db: str = DB_SOUTH,
    offline: bool = False,
    model: str = "agy/gemini-4-neon-medium",
    v2_bundle_path: str | Path | None = None,
    max_rounds: int = 4,
) -> dict[str, Any]:
    """Freeze North-induced v2.0.0 ontology and test zero-shot transfer on South ERP."""
    seed = build_business_seed_ontology()
    kernel_sha_before = _induction_kernel_sha256()

    provenance = "offline-reference-north" if offline else "live-north-bundle"
    if offline:
        north_bp, north_map = _induce_north_v2_offline()
    elif v2_bundle_path is not None:
        raw_bundle = json.loads(Path(v2_bundle_path).read_text(encoding="utf-8"))
        bundle_src = str(raw_bundle.get("source_db") or "")
        if bundle_src and bundle_src != north_db:
            raise ValueError(f"v2 bundle must originate strictly from {north_db!r}, got {bundle_src!r}")
        north_bp = DeclarativeBlueprint.model_validate(raw_bundle["blueprint"])
        north_map = AdapterMappingSpec.model_validate(raw_bundle["adapter_mapping"])
        provenance = f"frozen-bundle:{Path(v2_bundle_path).name} (induced_by={raw_bundle.get('induced_by_model')})"
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            bpath = Path(tmpdir) / "north_v2_bundle.json"
            run_live_llm_l2_architect_experiment(
                models=[model],
                source_db=north_db,
                max_rounds=max_rounds,
                save_v2_bundle_path=str(bpath),
            )
            raw_bundle = json.loads(bpath.read_text(encoding="utf-8"))
            north_bp = DeclarativeBlueprint.model_validate(raw_bundle["blueprint"])
            north_map = AdapterMappingSpec.model_validate(raw_bundle["adapter_mapping"])
            provenance = f"fresh-live-induction:{model}@{north_db}"

    frozen_sha_before = _freeze_pair_sha256(north_bp, north_map)

    if offline:
        north_world = OfflineOdooPhysicalWorld()
        north_harness = PhysicalOdooHarness(offline_world=north_world)
        _, _, n_models, n_fields = north_harness.measure_raw_schema_counts()
        _, n_raw_tokens = evaluate_raw_odoo_baseline(north_harness)
        sc_north = evaluate_autonomous_fde_regime(
            regime_name="north_v2.0_frozen",
            blueprint=north_bp,
            adapter_mapping=north_map,
            harness=north_harness,
            sandbox_client=north_world,
            raw_baseline_tokens=n_raw_tokens,
            raw_total_models=n_models,
            raw_total_fields=n_fields,
            seed_ontology=seed,
        )

        south_world = _make_disjoint_south_offline_world()
        south_m0_grounding = _evaluate_south_m0_grounding(
            client=south_world,
            source_locator="offline://erp_south_disjoint",
            blueprint=north_bp,
            adapter_mapping=north_map,
            seed=seed,
        )
        south_harness = PhysicalOdooHarness(offline_world=south_world)
        _, _, s_models, s_fields = south_harness.measure_raw_schema_counts()
        _, s_raw_tokens = evaluate_raw_odoo_baseline(south_harness)
        sc_south = evaluate_autonomous_fde_regime(
            regime_name="south_v2.0_zero_shot_transfer",
            blueprint=north_bp,
            adapter_mapping=north_map,
            harness=south_harness,
            sandbox_client=south_world,
            raw_baseline_tokens=s_raw_tokens,
            raw_total_models=s_models,
            raw_total_fields=s_fields,
            seed_ontology=seed,
        )
    else:
        with EphemeralOdooSandbox(source_db=north_db) as n_sb:
            n_sb._ensure_login()
            n_harness = PhysicalOdooHarness(live_client=n_sb._client)
            _, _, n_models, n_fields = n_harness.measure_raw_schema_counts()
            _, n_raw_tokens = evaluate_raw_odoo_baseline(n_harness)
            sc_north = evaluate_autonomous_fde_regime(
                regime_name=f"{north_db}_v2.0_frozen",
                blueprint=north_bp,
                adapter_mapping=north_map,
                harness=n_harness,
                sandbox_client=n_sb,
                raw_baseline_tokens=n_raw_tokens,
                raw_total_models=n_models,
                raw_total_fields=n_fields,
                seed_ontology=seed,
            )

        ro_south = ReadOnlyOdooClient(db=south_db)
        south_m0_grounding = _evaluate_south_m0_grounding(
            client=ro_south,
            source_locator=f"odoo://{south_db}",
            blueprint=north_bp,
            adapter_mapping=north_map,
            seed=seed,
        )
        with EphemeralOdooSandbox(source_db=south_db) as s_sb:
            s_sb._ensure_login()
            s_harness = PhysicalOdooHarness(live_client=s_sb._client)
            _, _, s_models, s_fields = s_harness.measure_raw_schema_counts()
            _, s_raw_tokens = evaluate_raw_odoo_baseline(s_harness)
            sc_south = evaluate_autonomous_fde_regime(
                regime_name=f"{south_db}_v2.0_zero_shot_transfer",
                blueprint=north_bp,
                adapter_mapping=north_map,
                harness=s_harness,
                sandbox_client=s_sb,
                raw_baseline_tokens=s_raw_tokens,
                raw_total_models=s_models,
                raw_total_fields=s_fields,
                seed_ontology=seed,
            )

    frozen_sha_after = _freeze_pair_sha256(north_bp, north_map)
    kernel_sha_after = _induction_kernel_sha256()

    north_probes = {p.probe_id: p.passed for p in sc_north.probes}
    south_probes = {p.probe_id: p.passed for p in sc_south.probes}
    probe_parity = {
        pid: {
            "north_passed": north_probes.get(pid),
            "south_passed": south_probes.get(pid),
            "parity": north_probes.get(pid) == south_probes.get(pid),
        }
        for pid in sorted(set(north_probes) | set(south_probes))
    }

    return {
        "mode": "offline-disjoint-mirror" if offline else "live-cloned-erp-south",
        "north_db": north_db,
        "south_db": south_db,
        "provenance": provenance,
        "frozen_ontology_sha256_before": frozen_sha_before,
        "frozen_ontology_sha256_after": frozen_sha_after,
        "zero_shot_immutable": frozen_sha_before == frozen_sha_after,
        "induction_kernel_frozen": kernel_sha_before == kernel_sha_after,
        "induction_kernel_sha256": kernel_sha_after,
        "south_m0_grounding": south_m0_grounding,
        "north": sc_north.to_dict(),
        "south": sc_south.to_dict(),
        "probe_parity": probe_parity,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Zero-shot cross-ERP transfer benchmark (erp_north -> erp_south)"
    )
    parser.add_argument("--north-db", default=DB_NORTH)
    parser.add_argument("--south-db", default=DB_SOUTH)
    parser.add_argument("--model", default="agy/gemini-4-neon-medium")
    parser.add_argument("--v2-bundle", default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    out = run_cross_erp_transfer_benchmark(
        north_db=args.north_db,
        south_db=args.south_db,
        offline=args.offline,
        model=args.model,
        v2_bundle_path=args.v2_bundle,
        max_rounds=args.max_rounds,
    )
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not args.offline:
        print("ODOO_DBS_AFTER:", _jsonrpc("db", "list", []))


if __name__ == "__main__":
    main()
