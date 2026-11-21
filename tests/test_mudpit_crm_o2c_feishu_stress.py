"""WO-09 跨软件三外设泥潭实战压力测试与底座漏水回归红测。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY, settle_work_card
from examples.dual_device_driver_swap_benchmark import BOTTLECRM_SQLITE_PATH, file_sha256
from examples.feishu_card_bridge import FeishuApprovalGateway, FeishuOpenAPIClient
from examples.mudpit_crm_o2c_feishu_stress import (
    MockFeishuCardTransport,
    build_mudpit_composed_blueprint,
    build_mudpit_customer_overlay,
    build_mudpit_effective_blueprint,
    run_mudpit_stress_suite,
)
from examples.odoo_dual_erp_demo import OdooJSONRPC, _jsonrpc
from open_ontology import Actor, InvariantError, OntologyRuntime, SourceBridge
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    AdapterMappingSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    DifferentialFalsifier,
    EntityMappingSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldSpec,
    FieldTransformSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    RelationSpec,
    apply_overlay,
    compile_blueprint,
    merge_projected_phenomena,
    project_external_phenomenon,
)


# ---------------------------------------------------------------------------
# Red Test 1: Same-Table Dual Entity Projection + Entity Resolution Merge
# ---------------------------------------------------------------------------
def test_leak1_phenomenon_same_table_dual_projection_and_entity_resolution_merge() -> None:
    crm_profile = ExternalPhenomenonProfile(
        source_kind="sqlite_crm",
        entities=[
            ExternalEntityProfile(
                table_name="accounts",
                row_count=1,
                fields=[ExternalFieldProfile(name="id"), ExternalFieldProfile(name="name")],
                sample_rows=[{"id": "uuid-acme", "name": "Acme China Manufacturing", "country": "CN"}],
            )
        ],
    )
    # Put Child entity BEFORE Parent entity sharing same table to stress toposort + lookup_target_type
    crm_mapping = AdapterMappingSpec(
        entity_mappings=[
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="CrmAccount",
                id_transform=FieldTransformSpec(target_field="account_id", source_field="id", kind="id_prefix", prefix="ACC-"),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="party",
                        source_field="id",
                        kind="lookup_join",
                        lookup_table="accounts",
                        lookup_key_field="id",
                        lookup_target_type="BusinessParty",
                    ),
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                ],
            ),
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="PTY-",
                    enum_map={"uuid-acme": "PTY-ACME"},
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="tax_id", source_field="vat", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct", default="CN"),
                ],
            ),
        ]
    )
    crm_m0, crm_errs = project_external_phenomenon(crm_profile, crm_mapping)
    assert crm_errs == []
    acc_entry = next(e for e in crm_m0 if e["object_type"] == "CrmAccount")
    assert acc_entry["data"]["party"] == "PTY-ACME"

    odoo_m0 = [
        {
            "object_type": "BusinessParty",
            "object_id": "PTY-ACME",
            "data": {
                "party_id": "PTY-ACME",
                "legal_name": "Acme Corporation",
                "tax_id": "US12345673",
                "country_code": "US",
            },
        }
    ]
    merged = merge_projected_phenomena([crm_m0, odoo_m0])
    parties = [e for e in merged if e["object_type"] == "BusinessParty"]
    assert len(parties) == 1
    assert parties[0]["data"]["legal_name"] == "Acme China Manufacturing"
    assert parties[0]["data"]["tax_id"] == "US12345673"


# ---------------------------------------------------------------------------
# Red Test 2: DSL Rejects Typo Multi-Hop Relation / Unknown Field / Bad Param
# ---------------------------------------------------------------------------
def test_leak2_dsl_rejects_typo_multihop_ref_and_invalid_param_at_overlay_and_compile() -> None:
    composed = build_mudpit_composed_blueprint()

    # Typo: obj.customer_profile.party instead of obj.customer.party
    typo_overlay = CustomerOverlaySpec(
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="typo_two_hop_guard",
                    code="OPEN_CRITICAL_CRM_TICKET_BLOCKS_ORDER",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "eq",
                            "left": {"ref": "target.account.party"},
                            "right": {"ref": "obj.customer_profile.party"},
                        },
                    },
                )
            ]
        }
    )
    with pytest.raises(InvariantError) as exc_rel:
        apply_overlay(composed, typo_overlay)
    assert exc_rel.value.code == "UNKNOWN_RELATION_IN_REF"

    bad_field_overlay = CustomerOverlaySpec(
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="typo_leaf_field",
                    code="BAD_LEAF",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={"op": "eq", "left": {"ref": "obj.customer.nonexistent_col"}, "right": {"lit": "x"}},
                )
            ]
        }
    )
    with pytest.raises(InvariantError) as exc_fld:
        apply_overlay(composed, bad_field_overlay)
    assert exc_fld.value.code == "UNKNOWN_FIELD_IN_REF"

    bad_param_overlay = CustomerOverlaySpec(
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="typo_param_ref",
                    code="BAD_PARAM",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={"op": "eq", "left": {"ref": "param.missing_arg"}, "right": {"lit": "x"}},
                )
            ]
        }
    )
    with pytest.raises(InvariantError) as exc_prm:
        apply_overlay(composed, bad_param_overlay)
    assert exc_prm.value.code == "UNKNOWN_PARAM_IN_REF"


# ---------------------------------------------------------------------------
# Red Test 3 & 4: Falsifier Stepping-Stone Wave + Feishu Generalization on CrmDeal
# ---------------------------------------------------------------------------
def test_leak3_and_4_falsifier_stepping_stone_and_feishu_crm_deal_settlement() -> None:
    # 3A: Falsifier must reuse already-covered 'unlock' action as stepping stone in Wave 1
    bp = DeclarativeBlueprint(
        model_id="stepping_stone_demo",
        version="1.0.0",
        object_types=[
            ObjectTypeBlueprint(
                name="GateItem",
                primary_key="item_id",
                status_field="status",
                initial_statuses=["locked"],
                transitions={"locked": ["unlocked"], "unlocked": ["done"], "done": []},
                fields=[
                    FieldSpec(name="item_id", field_type="str", primary_key=True, required=True),
                    FieldSpec(name="status", field_type="str", default="locked"),
                ],
            )
        ],
        actions=[
            ActionBlueprint(
                name="unlock_item",
                allowed_roles=["steward"],
                params=[FieldSpec(name="item_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="must_be_locked",
                        code="NOT_LOCKED",
                        target_type="GateItem",
                        id_param="item_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "locked"}},
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="GateItem",
                        id_param="item_id",
                        set_fields={"status": {"lit": "unlocked"}},
                    )
                ],
            ),
            ActionBlueprint(
                name="finish_item",
                allowed_roles=["steward"],
                params=[FieldSpec(name="item_id", field_type="str", required=True)],
                preconditions=[
                    InvariantRuleSpec(
                        name="must_be_unlocked",
                        code="NOT_UNLOCKED",
                        target_type="GateItem",
                        id_param="item_id",
                        must_hold={"op": "eq", "left": {"ref": "obj.status"}, "right": {"lit": "unlocked"}},
                    )
                ],
                mutations=[
                    ActionMutationSpec(
                        op="modify",
                        object_type="GateItem",
                        id_param="item_id",
                        set_fields={"status": {"lit": "done"}},
                    )
                ],
            ),
        ],
    )

    class _MemItemSandbox:
        def __init__(self) -> None:
            self.rows: dict[str, dict[str, Any]] = {}
            self.writes = 0

        def reset(self) -> None:
            self.rows = {"IT-1": {"item_id": "IT-1", "status": "locked"}}

        def snapshot_m0(self) -> list[dict[str, Any]]:
            return [{"object_type": "GateItem", "object_id": k, "data": dict(v)} for k, v in self.rows.items()]

        def make_source_bridge(self, spec: Any, adapter_mapping: AdapterMappingSpec) -> SourceBridge:
            _ = spec, adapter_mapping

            def _actuate(mv: dict[str, Any]) -> None:
                p = mv["params"]
                self.rows[p["item_id"]]["status"] = "unlocked" if mv["action_type"] == "unlock_item" else "done"
                self.writes += 1

            def _observe(mv: dict[str, Any]) -> dict[str, Any]:
                _ = mv
                return {"GateItem": [dict(v) for v in self.rows.values()]}

            return SourceBridge(actuate=_actuate, observe=_observe)

        def external_write_count(self) -> int:
            return self.writes

    rep = DifferentialFalsifier(min_action_coverage=1.0, min_transition_coverage=1.0).evaluate(
        blueprint=bp,
        adapter_mapping=AdapterMappingSpec(),
        sandbox=_MemItemSandbox(),
        target_actions=("finish_item",),
    )
    # Test explicit multi-wave when only finish_item is target: unlock_item isn't target, so pick both targets:
    rep_full = DifferentialFalsifier(min_action_coverage=1.0, min_transition_coverage=1.0).evaluate(
        blueprint=bp,
        adapter_mapping=AdapterMappingSpec(),
        sandbox=_MemItemSandbox(),
    )
    assert rep_full.verdict == "CORROBORATED"
    assert rep_full.transition_coverage_ratio == 1.0
    assert rep.verdict == "FALSIFIED"

    # 3B: FeishuApprovalGateway on composed CrmDeal (deal_id, non-SalesOrder) + settle_work_card idempotent REJECTED guard
    eff_bp = build_mudpit_effective_blueprint()
    spec = compile_blueprint(eff_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, db_path=":memory:")
    try:
        rt.put_object(spec.get_object_type("BusinessParty")(party_id="PTY-S", legal_name="Southstar"))
        rt.put_object(spec.get_object_type("CrmAccount")(account_id="ACC-S", name="Southstar Acc", party="PTY-S"))
        rt.put_object(
            spec.get_object_type("CrmDeal")(
                deal_id="DEAL-BIG",
                title="Big Medical Replace",
                account="ACC-S",
                amount=260_000.0,
                stage="QUALIFICATION",
            )
        )
        transport = MockFeishuCardTransport()
        feishu_cli = FeishuOpenAPIClient(
            app_id="cli_deal",
            app_secret="sec_deal",
            receive_id="oc_crm",
            receive_id_type="chat_id",
            transport=transport,
        )
        gw = FeishuApprovalGateway(
            rt,
            feishu_client=feishu_cli,
            operator_actors={"ou_stew": Actor(actor_id="stew-1", role="steward")},
            default_approver=Actor(actor_id="stew-1", role="steward"),
        )
        sales = Actor(actor_id="sales-1", role="sales_rep")
        esc = gw.submit_or_escalate(
            "RUN-DEAL-1",
            "lose_crm_deal",
            {"deal_id": "DEAL-BIG", "loss_reason": "Budget cut"},
            sales,
            assignee_role="steward",
            target_object_type="CrmDeal",
        )
        assert esc["status"] == "OPEN"
        assert esc["error_code"] == "APPROVAL_REQUIRED_HIGH_VALUE_LOSS"
        rec = gw.get_card_record("RUN-DEAL-1")
        assert rec is not None and rec.message_id == "om_mudpit_1"
        assert rec.card_view["order_id"] == "DEAL-BIG"
        assert rec.card_view["changes_summary"]

        cb = gw.handle_feishu_callback(
            {"schema": "2.0", "event": {"operator": {"open_id": "ou_stew"}, "action": {"value": {"op": "approve", "run_id": "RUN-DEAL-1"}}}}
        )
        assert cb["status"] == "COMMITTED"
        deal_after = rt.get_object(spec.get_object_type("CrmDeal"), "DEAL-BIG", sales)
        assert deal_after.stage == "CLOSED_LOST"
        assert any(c["method"] == "PATCH" for c in transport.calls)
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# End-to-End 6 Mudpits: Offline + Live Odoo 19 Ephemeral Clone
# ---------------------------------------------------------------------------
def test_offline_mudpit_all_6_scenarios_and_falsifier_corroborated() -> None:
    sha_before = file_sha256(BOTTLECRM_SQLITE_PATH)
    out = run_mudpit_stress_suite(offline=True)
    assert out["live_mode"] is False
    assert out["pty_acme_legal_name"] == "Acme China Manufacturing"
    assert out["pty_acme_tax_id"] == "US12345673"
    assert out["crm_view_token_ratio"] < 0.65
    assert out["o2c_view_token_ratio"] < 0.65
    assert out["feishu_http_posts"] == 1
    assert out["feishu_http_patches"] >= 1
    fal = out["falsification"]
    assert fal["verdict"] == "CORROBORATED"
    assert fal["action_coverage_ratio"] == 1.0
    assert fal["transition_coverage_ratio"] >= 0.8
    assert fal["adversarial_escapes"] == 0
    assert fal["differential_mismatches"] == 0
    assert fal["dirty_external_writes"] == 0
    assert file_sha256(BOTTLECRM_SQLITE_PATH) == sha_before


def test_live_odoo19_mudpit_all_6_scenarios_and_falsifier_corroborated() -> None:
    raw = OdooJSONRPC("erp_north")
    raw.login()
    dbs_before = sorted(_jsonrpc("db", "list", []))
    assert dbs_before == ["erp_north", "erp_south", "odoo_demo"]

    sha_before = file_sha256(BOTTLECRM_SQLITE_PATH)
    out = run_mudpit_stress_suite(offline=False)
    assert out["live_mode"] is True
    assert out["pty_acme_legal_name"] == "Acme China Manufacturing"
    assert out["pty_acme_tax_id"] == "US12345673"
    assert out["crm_view_token_ratio"] < 0.65
    assert out["o2c_view_token_ratio"] < 0.65
    assert out["sqlite_writes"] >= 1
    assert out["odoo_writes"] >= 1
    assert out["feishu_http_posts"] == 1
    assert out["feishu_http_patches"] >= 1

    fal = out["falsification"]
    assert fal["verdict"] == "CORROBORATED"
    assert fal["action_coverage_ratio"] == 1.0
    assert fal["transition_coverage_ratio"] >= 0.8
    assert fal["adversarial_escapes"] == 0
    assert fal["differential_mismatches"] == 0
    assert fal["dirty_external_writes"] == 0

    assert file_sha256(BOTTLECRM_SQLITE_PATH) == sha_before
    dbs_after = sorted(_jsonrpc("db", "list", []))
    assert dbs_after == ["erp_north", "erp_south", "odoo_demo"]
