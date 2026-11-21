"""Full closed-loop tests for WO-21 on SQLite and live PostgreSQL 16:

1. Pure M2 ``MDMSidecarPlugin`` incremental reconciliation:
   - L1 exact hard-key auto-merge + ``AdapterMappingSpec`` hot refresh (0 ``WorkCard``s);
   - L4 gray-zone candidate & deceptive hard-key conflict failover (0 silent merge,
     idempotent ``WorkCard`` creation on repeated polling);
   - Steward ruling convergence (``CONFIRM_MERGE`` / ``REJECT_SEPARATE``) into L0 links.
2. Hebei steel mill M1 plugin (``examples/steel_mdm_plugin.py``):
   - Deadlock 1 ("loose weighbridge, tight settlement"): unmapped bypass ERP SKU enters
     ``SteelStorageBin`` immediately as ``PROVISIONAL`` (0 truck blockage), blocks month-end
     cost close & lot settlement via ``PROVISIONAL_SKU_BLOCKS_SETTLEMENT``, and closes cleanly
     with three-view conservation after Steward ``WorkCard`` approval & reconciliation;
   - Deadlock 2 (N:M ``Spec A/B x Brand 1`` lossless bin attribution): ``consume_steel_from_bin``
     precisely attributes ``BIN_EXACT`` consumption to ``SPEC-B`` without FIFO misallocation,
     explicitly separated from ``consume_steel_by_brand_fifo`` (``BRAND_FIFO``).
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from typing import Any, Iterator
import uuid

import psycopg
import pytest

from open_ontology import InvariantError, OntologyRuntime
from open_ontology.induction.phenomenon import (
    AdapterMappingSpec,
    EntityMappingSpec,
    FieldTransformSpec,
)
from open_ontology.induction.resolution import (
    EntityResolutionDecision,
    EntityResolutionSpec,
    SourceEntityBatch,
    SourceRef,
)
from open_ontology.mdm_plugin import MDMSidecarPlugin
from open_ontology.postgres_backend import open_postgres_runtime

from examples.std_core_ontology import BusinessParty, TradeResource, WorkCard
from examples.steel_mdm_plugin import (
    AI_AGENT,
    BUYER,
    COST_ACCOUNTANT,
    MES_OPERATOR,
    STEWARD,
    WEIGHBRIDGE,
    SteelConsumptionEntry,
    SteelContractSKU,
    SteelCostPeriod,
    SteelManufacturer,
    SteelMaterialLot,
    spec as steel_plugin_spec,
)

PG_DSN = os.environ.get(
    "OPEN_ONTOLOGY_PG_DSN",
    "postgresql://open_ontology:open_ontology_dev@127.0.0.1:15432/open_ontology",
)


@contextmanager
def _open_backend_runtime(spec_obj: Any, backend_kind: str) -> Iterator[OntologyRuntime]:
    if backend_kind == "sqlite":
        rt = OntologyRuntime(spec_obj, db_path=":memory:")
        try:
            yield rt
        finally:
            rt.close()
        return

    if backend_kind == "postgres":
        schema = f"wo21_t_{uuid.uuid4().hex[:12]}"
        rt = open_postgres_runtime(spec_obj, PG_DSN, schema_name=schema)
        try:
            yield rt
        finally:
            rt.close()
            with psycopg.connect(PG_DSN, autocommit=True) as admin_conn:
                admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        return

    raise ValueError(f"unsupported backend_kind: {backend_kind!r}")


# ---------------------------------------------------------------------------
# 1. Pure M2 MDMSidecarPlugin incremental reconciliation (SQLite + Live PG 16)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend_kind", ["sqlite", "postgres"])
def test_mdm_sidecar_incremental_l1_l4_conflict_and_steward_sync(backend_kind: str) -> None:
    res_spec = EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="BP-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code"],
        strip_tokens=["Co.", "Ltd.", "Group", "有限公司"],
        auto_merge_threshold=0.90,
        review_threshold=0.55,
        quasi_key_weight=0.70,
        neighbor_weight=0.30,
        survivorship={"legal_name": ["erp_north", "erp_south"]},
    )

    def _make_mapping(source_id: str) -> AdapterMappingSpec:
        return AdapterMappingSpec(
            mapping_id=f"map_{source_id}",
            source_kind="erp",
            entity_mappings=[
                EntityMappingSpec(
                    source_table="res_partner",
                    target_object_type="BusinessParty",
                    id_transform=FieldTransformSpec(
                        target_field="party_id",
                        source_field="id",
                        kind="direct",
                    ),
                ),
                EntityMappingSpec(
                    source_table="brand_profile",
                    target_object_type="SteelManufacturer",
                    id_transform=FieldTransformSpec(
                        target_field="brand_id",
                        source_field="id",
                        kind="direct",
                    ),
                    field_transforms=[
                        FieldTransformSpec(
                            target_field="party",
                            source_field="partner_id",
                            kind="foreign_key_id",
                        )
                    ],
                ),
            ],
        )

    callback_events: list[tuple[int, dict[str, dict[str, str]]]] = []

    def _on_cw_update(cw: Any, mappings: dict[str, AdapterMappingSpec]) -> None:
        callback_events.append(
            (
                len(cw.entries),
                {
                    src_id: dict(m.get_entity_mapping("BusinessParty").id_transform.enum_map)  # type: ignore[union-attr]
                    for src_id, m in mappings.items()
                },
            )
        )

    def _review_payload(intent: dict[str, Any]) -> dict[str, Any]:
        return {
            "card_id": f"WC-{intent['target_object_id']}",
            "card_kind": "HUMAN_TASK",
            "target_object_type": intent["target_object_type"],
            "target_object_id": intent["target_object_id"],
            "requested_action": intent["requested_operation"],
            "requested_params_json": intent["params_json"],
            "assignee_role": intent["suggested_assignee_role"],
            "external_ref": intent["intent_code"],
        }

    plugin = MDMSidecarPlugin(
        res_spec,
        adapter_mappings={"erp_north": _make_mapping("erp_north"), "erp_south": _make_mapping("erp_south")},
        foreign_key_fields=[("SteelManufacturer", "party")],
        review_action="open_work_card",
        review_payload_builder=_review_payload,
        on_crosswalk_updated=_on_cw_update,
    )

    with _open_backend_runtime(steel_plugin_spec, backend_kind) as rt:
        # Phase 1: L1 exact tax_id match + singleton -> 0 WorkCards, automatic hot refresh
        b1_north = SourceEntityBatch(
            source_id="erp_north",
            source_table="res_partner",
            pk_field="id",
            records=[
                {"id": "N-1", "legal_name": "Tangshan Coking Coal Co.", "tax_id": "TAX-TS-001", "country_code": "CN"},
                {"id": "N-2", "legal_name": "Alpha Logistics Supply", "tax_id": "TAX-AL-002", "country_code": "CN"},
            ],
        )
        b1_south = SourceEntityBatch(
            source_id="erp_south",
            source_table="res_partner",
            pk_field="id",
            records=[
                {"id": "S-1", "legal_name": "Tangshan Coking Coal Ltd.", "tax_id": "TAX-TS-001", "country_code": "CN"},
            ],
        )

        res1 = plugin.ingest_incremental([b1_north, b1_south], runtime=rt, actor=BUYER, run_id_prefix="mdm-b1")
        cid_n1 = res1.crosswalk.resolve_canonical_id("erp_north", "N-1")
        cid_s1 = res1.crosswalk.resolve_canonical_id("erp_south", "S-1")
        assert cid_n1 is not None and cid_n1 == cid_s1
        assert cid_n1 in res1.auto_merged_canonical_ids
        assert res1.dispatched_review_run_ids == []
        assert res1.pending_candidates == []
        assert rt.query_objects("WorkCard", STEWARD) == []
        assert callback_events

        # Hot-refreshed AdapterMappingSpec maps both entity PK and SteelManufacturer.party FK
        north_map = res1.refreshed_mappings["erp_north"]
        south_map = res1.refreshed_mappings["erp_south"]
        assert north_map.get_entity_mapping("BusinessParty").id_transform.enum_map["N-1"] == cid_n1  # type: ignore[union-attr]
        assert south_map.get_entity_mapping("BusinessParty").id_transform.enum_map["S-1"] == cid_n1  # type: ignore[union-attr]
        north_brand_party_tf = [
            tf for tf in north_map.get_entity_mapping("SteelManufacturer").field_transforms if tf.target_field == "party"  # type: ignore[union-attr]
        ][0]
        assert north_brand_party_tf.enum_map["N-1"] == cid_n1

        # Phase 2: Incremental L4 gray zone (N-3 vs S-3) + deceptive hard-key conflict (N-4 vs S-4)
        b2_north = SourceEntityBatch(
            source_id="erp_north",
            source_table="res_partner",
            pk_field="id",
            records=[
                {"id": "N-3", "legal_name": "Bohai Precision Fuel", "tax_id": "", "country_code": "CN"},
                {"id": "N-4", "legal_name": "Sunrise Steel Group", "tax_id": "TAX-SUN-A", "country_code": "CN"},
            ],
        )
        b2_south = SourceEntityBatch(
            source_id="erp_south",
            source_table="res_partner",
            pk_field="id",
            records=[
                {"id": "S-3", "legal_name": "Bohai Precision Coking", "tax_id": "", "country_code": "CN"},
                {"id": "S-4", "legal_name": "Sunrise Steel Group Ltd.", "tax_id": "TAX-SUN-B", "country_code": "CN"},
            ],
        )

        res2 = plugin.ingest_incremental([b2_north, b2_south], runtime=rt, actor=BUYER, run_id_prefix="mdm-b2")
        assert len(res2.pending_candidates) == 1
        assert {
            (res2.pending_candidates[0].left.key, res2.pending_candidates[0].right.key)
        } == {("erp_north:N-3", "erp_south:S-3")}
        assert res2.crosswalk.resolve_canonical_id("erp_north", "N-3") != res2.crosswalk.resolve_canonical_id("erp_south", "S-3")
        assert res2.crosswalk.resolve_canonical_id("erp_north", "N-4") != res2.crosswalk.resolve_canonical_id("erp_south", "S-4")
        assert len(res2.dispatched_review_run_ids) == 2

        cards = rt.query_objects("WorkCard", STEWARD)
        assert len(cards) == 2

        # Repeated poll over same unresolved records must dispatch ZERO new WorkCards
        res2_repeat = plugin.ingest_incremental([b2_north, b2_south], runtime=rt, actor=BUYER, run_id_prefix="mdm-b2")
        assert res2_repeat.dispatched_review_run_ids == []
        assert len(rt.query_objects("WorkCard", STEWARD)) == 2

        # Phase 3: Steward resolves WorkCards -> CONFIRM_MERGE (L4) & REJECT_SEPARATE (hard-key conflict)
        for card in sorted(cards, key=lambda c: c.card_id):
            if card.external_ref == "RESOLVE_ENTITY_MATCH":
                rt.submit_action(
                    f"STEW-ACT-{card.card_id}",
                    "complete_work_card",
                    {"card_id": card.card_id, "resolution_note": "CONFIRM_MERGE"},
                    STEWARD,
                )
            else:
                rt.submit_action(
                    f"STEW-ACT-{card.card_id}",
                    "reject_work_card",
                    {"card_id": card.card_id, "resolution_note": "REJECT_SEPARATE"},
                    STEWARD,
                )

        def _extract_ruling(raw_card: dict[str, Any]) -> EntityResolutionDecision | None:
            status = raw_card.get("status")
            if status not in ("COMPLETED", "REJECTED"):
                return None
            payload = json.loads(raw_card.get("requested_params_json") or "{}")
            left = SourceRef(**payload["left"])
            right = SourceRef(**payload["right"])
            verdict = "CONFIRM_MERGE" if status == "COMPLETED" else "REJECT_SEPARATE"
            return EntityResolutionDecision(
                candidate_id=str(payload.get("candidate_id") or ""),
                left=left,
                right=right,
                verdict=verdict,  # type: ignore[arg-type]
                decided_by=str(raw_card.get("resolved_by_actor_id") or ""),
                rationale=str(raw_card.get("resolution_note") or ""),
            )

        decisions = plugin.sync_resolved_reviews(
            rt,
            STEWARD,
            review_object_type="WorkCard",
            decision_extractor=_extract_ruling,
        )
        assert len(decisions) == 2
        assert plugin.crosswalk.resolve_canonical_id("erp_north", "N-3") == plugin.crosswalk.resolve_canonical_id("erp_south", "S-3")
        assert plugin.crosswalk.resolve_canonical_id("erp_north", "N-4") != plugin.crosswalk.resolve_canonical_id("erp_south", "S-4")
        assert ("erp_north:N-4", "erp_south:S-4") in plugin.crosswalk.negative_links

        # Hot-refreshed mapping updated with L0 confirmed canonical ID
        latest_north = plugin.adapter_mappings["erp_north"].get_entity_mapping("BusinessParty").id_transform.enum_map  # type: ignore[union-attr]
        latest_south = plugin.adapter_mappings["erp_south"].get_entity_mapping("BusinessParty").id_transform.enum_map  # type: ignore[union-attr]
        assert latest_north["N-3"] == latest_south["S-3"]

        # Re-syncing is strictly idempotent
        assert (
            plugin.sync_resolved_reviews(
                rt, STEWARD, review_object_type="WorkCard", decision_extractor=_extract_ruling
            )
            == []
        )


# ---------------------------------------------------------------------------
# 2. Hebei Steel Deadlock 1: Loose Weighbridge, Tight Month-End Settlement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend_kind", ["sqlite", "postgres"])
def test_hebei_steel_scenario1_loose_weighbridge_tight_settlement(backend_kind: str) -> None:
    with _open_backend_runtime(steel_plugin_spec, backend_kind) as rt:
        # Seed REA TradeResource + BusinessParty + SteelManufacturer + unmapped bypass ERP SKU
        rt.put_object(TradeResource(resource_id="RES-COAL", code="COAL-C", name="炼焦精煤", unit="TON"))
        rt.put_object(BusinessParty(party_id="PTY-BR1", legal_name="冀中能源峰峰煤业", tax_id="91130400001"))
        rt.put_object(SteelManufacturer(brand_id="BR-1", party="PTY-BR1", name="峰峰品牌"))
        rt.load_snapshot(
            {
                "SteelContractSKU": [
                    {
                        "sku_id": "SKU-ROGUE-88",
                        "invoice_title": "旁路应急采购散煤-峰峰",
                        "brand_id": "BR-1",
                        "unit_price_ton": 1500.0,
                        "ash_pct": 9.5,
                        "sulfur_pct": 0.65,
                    }
                ]
            },
            actor=STEWARD,
        )

        rt.submit_action(
            "RUN-SPEC-STD",
            "create_steel_material_spec",
            {
                "spec_id": "SPEC-STD",
                "resource_id": "RES-COAL",
                "name": "低灰低硫主焦煤",
                "grade": "A",
                "ash_max_pct": 10.5,
                "sulfur_max_pct": 0.70,
            },
            STEWARD,
        )
        rt.submit_action(
            "RUN-SPEC-TIGHT",
            "create_steel_material_spec",
            {
                "spec_id": "SPEC-ULTRA-LOW",
                "resource_id": "RES-COAL",
                "name": "特低硫焦煤",
                "grade": "S",
                "ash_max_pct": 9.0,
                "sulfur_max_pct": 0.45,
            },
            STEWARD,
        )
        rt.submit_action(
            "RUN-BIN-1",
            "create_steel_storage_bin",
            {"bin_id": "BIN-1", "yard_name": "一号原料煤场", "material_category": "coking_coal"},
            STEWARD,
        )
        rt.submit_action(
            "RUN-PERIOD-1",
            "open_steel_cost_period",
            {"period_id": "P-2026-11"},
            COST_ACCOUNTANT,
        )

        # Weighbridge receives bypass unmapped SKU WITHOUT blocking trucks -> PROVISIONAL + WorkCard
        rt.submit_action(
            "RUN-WEIGH-1",
            "receive_steel_lot",
            {
                "lot_id": "LOT-P1",
                "sku_id": "SKU-ROGUE-88",
                "bin_id": "BIN-1",
                "initial_tons": 80.0,
                "period_id": "P-2026-11",
            },
            WEIGHBRIDGE,
        )
        lot_p1 = rt.get_object(SteelMaterialLot, "LOT-P1", WEIGHBRIDGE)
        assert lot_p1.governance_state == "PROVISIONAL"
        assert lot_p1.spec_id is None
        assert lot_p1.pending_work_card_id == "WC-PROV-LOT-P1"
        wc = rt.get_object(WorkCard, "WC-PROV-LOT-P1", STEWARD)
        assert wc.status == "OPEN"
        assert wc.target_object_id == "SKU-ROGUE-88"

        # Blast furnace consumes 50t directly from physical bin (no stoppage!)
        rt.submit_action(
            "RUN-CONS-P1",
            "consume_steel_from_bin",
            {"entry_id": "ENT-P1", "bin_id": "BIN-1", "tons": 50.0, "period_id": "P-2026-11"},
            MES_OPERATOR,
        )
        entry_p1 = rt.get_object(SteelConsumptionEntry, "ENT-P1", COST_ACCOUNTANT)
        assert entry_p1.cost_allocation_rule == "BIN_EXACT"
        assert entry_p1.spec_id is None
        assert entry_p1.cost_amount == pytest.approx(75000.0)

        ledger_before = rt.call_function(
            "reconcile_steel_three_view_ledger", {"period_id": "P-2026-11"}, COST_ACCOUNTANT
        )
        assert ledger_before["provisional_lot_ids"] == ["LOT-P1"]
        assert ledger_before["settlement_ready"] is False

        # Hard Gate: payment authorization & month-end closing MUST reject PROVISIONAL lots
        with pytest.raises(InvariantError) as auth_exc:
            rt.submit_action("RUN-AUTH-FAIL", "authorize_steel_lot_settlement", {"lot_id": "LOT-P1"}, COST_ACCOUNTANT)
        assert auth_exc.value.code == "PROVISIONAL_SKU_BLOCKS_SETTLEMENT"

        with pytest.raises(InvariantError) as close_exc:
            rt.submit_action("RUN-CLOSE-FAIL", "close_steel_cost_period", {"period_id": "P-2026-11"}, COST_ACCOUNTANT)
        assert close_exc.value.code == "PROVISIONAL_SKU_BLOCKS_SETTLEMENT"
        assert rt.get_object(SteelCostPeriod, "P-2026-11", COST_ACCOUNTANT).status == "OPEN"

        # WorkCard guard: cannot complete mapping referencing uncompleted WorkCard
        with pytest.raises(InvariantError) as wc_exc:
            rt.submit_action(
                "RUN-MAP-EARLY",
                "map_steel_sku_to_spec",
                {"sku_id": "SKU-ROGUE-88", "canonical_spec_id": "SPEC-STD", "work_card_id": "WC-PROV-LOT-P1"},
                STEWARD,
            )
        assert wc_exc.value.code == "WORK_CARD_NOT_COMPLETED"

        # Steward resolves WorkCard (different actor from WEIGHBRIDGE -> satisfies SoD), then maps SKU
        rt.submit_action(
            "RUN-WC-COMPLETE",
            "complete_work_card",
            {"card_id": "WC-PROV-LOT-P1", "resolution_note": "Confirmed as SPEC-STD after lab check"},
            STEWARD,
        )
        with pytest.raises(InvariantError) as bad_q_exc:
            rt.submit_action(
                "RUN-MAP-BADQ",
                "map_steel_sku_to_spec",
                {
                    "sku_id": "SKU-ROGUE-88",
                    "canonical_spec_id": "SPEC-ULTRA-LOW",
                    "work_card_id": "WC-PROV-LOT-P1",
                },
                STEWARD,
            )
        assert bad_q_exc.value.code == "SPEC_QUALITY_MISMATCH"

        rt.submit_action(
            "RUN-MAP-OK",
            "map_steel_sku_to_spec",
            {
                "sku_id": "SKU-ROGUE-88",
                "canonical_spec_id": "SPEC-STD",
                "work_card_id": "WC-PROV-LOT-P1",
            },
            STEWARD,
        )
        rt.submit_action(
            "RUN-RECON-P1",
            "reconcile_provisional_lot",
            {"lot_id": "LOT-P1", "work_card_id": "WC-PROV-LOT-P1"},
            STEWARD,
        )

        # Lot upgraded to GOVERNED and consumption entry spec_id backfilled!
        lot_governed = rt.get_object(SteelMaterialLot, "LOT-P1", COST_ACCOUNTANT)
        assert lot_governed.governance_state == "GOVERNED"
        assert lot_governed.spec_id == "SPEC-STD"
        assert rt.get_object(SteelConsumptionEntry, "ENT-P1", COST_ACCOUNTANT).spec_id == "SPEC-STD"

        # Now lot settlement authorization and month-end closing succeed cleanly
        rt.submit_action("RUN-AUTH-OK", "authorize_steel_lot_settlement", {"lot_id": "LOT-P1"}, COST_ACCOUNTANT)
        assert rt.get_object(SteelMaterialLot, "LOT-P1", COST_ACCOUNTANT).settlement_authorized is True

        rt.submit_action("RUN-CLOSE-OK", "close_steel_cost_period", {"period_id": "P-2026-11"}, COST_ACCOUNTANT)
        closed_period = rt.get_object(SteelCostPeriod, "P-2026-11", COST_ACCOUNTANT)
        assert closed_period.status == "CLOSED"
        assert closed_period.closed_tons == pytest.approx(50.0)
        assert closed_period.closed_cost == pytest.approx(75000.0)

        ledger_after = rt.call_function(
            "reconcile_steel_three_view_ledger", {"period_id": "P-2026-11"}, COST_ACCOUNTANT
        )
        assert ledger_after["balanced"] is True
        assert ledger_after["settlement_ready"] is True
        assert ledger_after["provisional_lot_ids"] == []
        assert ledger_after["bin_exact_tons"] == pytest.approx(50.0)
        assert ledger_after["brand_fifo_tons"] == pytest.approx(0.0)
        assert ledger_after["by_spec"] == [
            {"spec_id": "SPEC-STD", "tons": 50.0, "cost": 75000.0, "spec_name": "低灰低硫主焦煤", "avg_price_ton": 1500.0}
        ]
        # Verify ontology overlay persisted owned SKU mapping on ERP record without losing source invoice_title
        sku_after = rt.get_object(SteelContractSKU, "SKU-ROGUE-88", STEWARD)
        assert sku_after.canonical_spec_id == "SPEC-STD"
        assert sku_after.invoice_title == "旁路应急采购散煤-峰峰"


# ---------------------------------------------------------------------------
# 3. Hebei Steel Deadlock 2: N:M Spec A/B x Brand 1 Lossless Bin Attribution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend_kind", ["sqlite", "postgres"])
def test_hebei_steel_scenario2_nm_bin_exact_vs_brand_fifo(backend_kind: str) -> None:
    with _open_backend_runtime(steel_plugin_spec, backend_kind) as rt:
        rt.put_object(TradeResource(resource_id="RES-COKE", code="COKE-1", name="冶金焦炭", unit="TON"))
        rt.put_object(BusinessParty(party_id="PTY-B1", legal_name="旭阳化工集团", tax_id="91130100099"))
        rt.put_object(SteelManufacturer(brand_id="BR-1", party="PTY-B1", name="旭阳品牌1"))

        rt.submit_action(
            "RUN-SPEC-A",
            "create_steel_material_spec",
            {
                "spec_id": "SPEC-A",
                "resource_id": "RES-COKE",
                "name": "高硫普通焦-A",
                "grade": "B",
                "ash_max_pct": 12.0,
                "sulfur_max_pct": 1.10,
            },
            STEWARD,
        )
        rt.submit_action(
            "RUN-SPEC-B",
            "create_steel_material_spec",
            {
                "spec_id": "SPEC-B",
                "resource_id": "RES-COKE",
                "name": "低硫优质焦-B",
                "grade": "A+",
                "ash_max_pct": 10.0,
                "sulfur_max_pct": 0.55,
            },
            STEWARD,
        )
        rt.submit_action(
            "RUN-BIN-A",
            "create_steel_storage_bin",
            {"bin_id": "BIN-A", "yard_name": "A号高硫焦仓"},
            STEWARD,
        )
        rt.submit_action(
            "RUN-BIN-B",
            "create_steel_storage_bin",
            {"bin_id": "BIN-B", "yard_name": "B号低硫精品仓"},
            STEWARD,
        )
        rt.submit_action("RUN-PER-11", "open_steel_cost_period", {"period_id": "P-2026-11"}, COST_ACCOUNTANT)

        # Single Brand BR-1 supplies BOTH Spec A (into BIN-A) and Spec B (into BIN-B)
        rt.load_snapshot(
            {
                "SteelContractSKU": [
                    {
                        "sku_id": "SKU-BR1-A",
                        "invoice_title": "旭阳高硫二级焦",
                        "brand_id": "BR-1",
                        "unit_price_ton": 1100.0,
                        "ash_pct": 11.2,
                        "sulfur_pct": 0.95,
                    },
                    {
                        "sku_id": "SKU-BR1-B",
                        "invoice_title": "旭阳低硫一级焦",
                        "brand_id": "BR-1",
                        "unit_price_ton": 1600.0,
                        "ash_pct": 9.4,
                        "sulfur_pct": 0.48,
                    },
                ]
            },
            actor=STEWARD,
        )
        rt.submit_action(
            "RUN-MAP-BR1-A",
            "map_steel_sku_to_spec",
            {"sku_id": "SKU-BR1-A", "canonical_spec_id": "SPEC-A"},
            STEWARD,
        )
        rt.submit_action(
            "RUN-MAP-BR1-B",
            "map_steel_sku_to_spec",
            {"sku_id": "SKU-BR1-B", "canonical_spec_id": "SPEC-B"},
            STEWARD,
        )

        # Notice LOT-A1 sorts earlier than LOT-B1 -> naive brand FIFO would misattribute BIN-B draw to SPEC-A!
        rt.submit_action(
            "RUN-LOT-A1",
            "receive_steel_lot",
            {"lot_id": "LOT-A1", "sku_id": "SKU-BR1-A", "bin_id": "BIN-A", "initial_tons": 100.0, "period_id": "P-2026-11"},
            WEIGHBRIDGE,
        )
        rt.submit_action(
            "RUN-LOT-B1",
            "receive_steel_lot",
            {"lot_id": "LOT-B1", "sku_id": "SKU-BR1-B", "bin_id": "BIN-B", "initial_tons": 100.0, "period_id": "P-2026-11"},
            WEIGHBRIDGE,
        )

        # 1) MES draws 40t from physical BIN-B -> MUST attribute 100% to SPEC-B / LOT-B1 (0 FIFO bleed into LOT-A1)
        rt.submit_action(
            "RUN-MES-BIN-B",
            "consume_steel_from_bin",
            {"entry_id": "ENT-BIN-1", "bin_id": "BIN-B", "tons": 40.0, "period_id": "P-2026-11"},
            MES_OPERATOR,
        )
        e_bin = rt.get_object(SteelConsumptionEntry, "ENT-BIN-1", COST_ACCOUNTANT)
        assert e_bin.cost_allocation_rule == "BIN_EXACT"
        assert e_bin.lot_id == "LOT-B1"
        assert e_bin.spec_id == "SPEC-B"
        assert e_bin.cost_amount == pytest.approx(64000.0)
        assert rt.get_object(SteelMaterialLot, "LOT-B1", COST_ACCOUNTANT).remaining_tons == pytest.approx(60.0)
        assert rt.get_object(SteelMaterialLot, "LOT-A1", COST_ACCOUNTANT).consumed_tons == pytest.approx(0.0)

        # 2) Legacy workstation without bin telemetry draws 25t by brand -> explicitly marked BRAND_FIFO
        rt.submit_action(
            "RUN-MES-FIFO",
            "consume_steel_by_brand_fifo",
            {"entry_id": "ENT-FIFO-1", "brand_id": "BR-1", "tons": 25.0, "period_id": "P-2026-11"},
            MES_OPERATOR,
        )
        e_fifo = rt.get_object(SteelConsumptionEntry, "ENT-FIFO-1", COST_ACCOUNTANT)
        assert e_fifo.cost_allocation_rule == "BRAND_FIFO"
        assert e_fifo.lot_id == "LOT-A1"
        assert e_fifo.spec_id == "SPEC-A"
        assert e_fifo.cost_amount == pytest.approx(27500.0)

        ledger = rt.call_function(
            "reconcile_steel_three_view_ledger", {"period_id": "P-2026-11"}, COST_ACCOUNTANT
        )
        assert ledger["balanced"] is True
        assert ledger["bin_exact_tons"] == pytest.approx(40.0)
        assert ledger["brand_fifo_tons"] == pytest.approx(25.0)
        assert ledger["totals"] == {"tons": 65.0, "cost": 91500.0}
        spec_totals = {r["spec_id"]: (r["tons"], r["cost"]) for r in ledger["by_spec"]}
        assert spec_totals == {
            "SPEC-A": (25.0, 27500.0),
            "SPEC-B": (40.0, 64000.0),
        }
        assert ledger["by_brand"][0]["tons"] == pytest.approx(65.0)
        assert ledger["by_brand"][0]["cost"] == pytest.approx(91500.0)

        rt.submit_action("RUN-CLOSE-NM", "close_steel_cost_period", {"period_id": "P-2026-11"}, COST_ACCOUNTANT)
        assert rt.get_object(SteelCostPeriod, "P-2026-11", COST_ACCOUNTANT).status == "CLOSED"
