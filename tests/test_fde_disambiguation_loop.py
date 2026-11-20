"""Acceptance tests for P2-2: AI-FDE 0.7 -> 1.0 Human-in-the-Loop Gray-Zone Disambiguation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from open_ontology import Actor, InvariantError, OntologyRuntime, PermissionDeniedError
from open_ontology.induction import (
    CallableAbductionBackend,
    CrucibleContext,
    InductionGovernanceService,
    OntologyBuilderAgent,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
    project_external_phenomenon,
)

from examples.business_ontology import (
    WorkCard,
    build_business_seed_ontology,
    register_work_card_primitives,
    settle_work_card,
)
from examples.fde_disambiguation_loop import (
    GrayZoneDisambiguationGateway,
    extract_scout_gray_zone_ambiguities,
)
from examples.feishu_card_bridge import FeishuOpenAPIClient, parse_feishu_ruling_callback
from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError
from examples.odoo_fde_autonomous_pipeline import (
    AI_OPS_ACTOR,
    STEWARD_OPS_ACTOR,
    build_auto_replay_factory,
    build_closed_phenomenon_profile,
    synthesize_from_fde_evidence,
)
from examples.odoo_fde_prober import OdooActiveProber
from examples.odoo_fde_scout import OdooFDEScout, ReadOnlyOdooClient
from tests.test_odoo_fde_prober import _SyntheticProbeSandbox
from tests.test_odoo_fde_scout import _SyntheticOdooMock


def _setup_clinic_v1_governance() -> tuple[
    InductionGovernanceService,
    Any,
    Any,
    Any,
    Any,
    list[dict[str, Any]],
    list[Any],
]:
    base_ro = _SyntheticOdooMock()
    base_ro.tables["clinic.visit"].append(
        {"id": 14, "name": "VST-005", "patient_id": [2, "Bob"], "state": "draft", "total_amount": 150.0}
    )
    sandbox = _SyntheticProbeSandbox(base_ro)
    scout_report = OdooFDEScout(base_ro, source_locator="mock://clinic_gz", max_models=5).scout()
    profile = build_closed_phenomenon_profile(scout_report, client=base_ro)
    probe_report = OdooActiveProber(sandbox, sandbox_db="mock_clone_gz").probe(scout_report)
    seed = build_business_seed_ontology()
    bundle = synthesize_from_fde_evidence(
        scout_report,
        probe_report,
        profile=profile,
        seed_ontology=seed,
        model_id="clinic_gz_model",
        apply_gray_zone_approval_rules=False,
    )
    projected, errs = project_external_phenomenon(profile, bundle.adapter_mapping)
    assert errs == []
    pos_events, neg_events = build_auto_replay_factory(bundle, seed_ontology=seed)(projected)

    gov = InductionGovernanceService(db_path=":memory:")
    builder = OntologyBuilderAgent(
        backend=CallableAbductionBackend(
            lambda _s, _u, _r: {
                "rationale": "Clinic v1.0.0",
                "blueprint": bundle.blueprint.model_dump(mode="json"),
                "adapter_mapping": bundle.adapter_mapping.model_dump(mode="json"),
                "proposed_eval_cases": [],
            }
        ),
        governance=gov,
        crucible=SymbolicCrucible(),
        seed_ontology=seed,
    )
    ctx = CrucibleContext(
        mode="genesis",
        replay_factory=build_auto_replay_factory(bundle, seed_ontology=seed),
        default_actor=AI_OPS_ACTOR,
        seed_ontology=seed,
    )
    cycle = builder.run_genesis_cycle(
        model_id="clinic_gz_model",
        phenomenon_brief={"source": "clinic_mock"},
        external_profile=profile,
        crucible_context=ctx,
        seed_ontology=seed,
        max_rounds=2,
    )
    assert cycle.verified and cycle.pending_approval_run_id is not None
    gov.approve_crystallization(cycle.pending_approval_run_id, STEWARD_ACTOR)
    bp_v1 = gov.get_active_blueprint("clinic_gz_model")
    assert bp_v1 is not None

    sop_doc = "原则上单笔诊单金额超过 700 元需视情况复核（subject to review）。"
    ambiguities = extract_scout_gray_zone_ambiguities(
        scout_report=scout_report,
        profile=profile,
        blueprint=bp_v1,
        adapter_mapping=bundle.adapter_mapping,
        sop_text=sop_doc,
    )
    return gov, bp_v1, bundle, scout_report, (pos_events, neg_events), projected, ambiguities


def _mock_feishu_transport(sent_calls: list[tuple[str, str, dict[str, Any]]]) -> tuple[FeishuOpenAPIClient, Any]:
    def _transport(method: str, url: str, headers: dict[str, str], body: bytes, _timeout: float) -> tuple[int, bytes]:
        del headers
        if url.endswith("/tenant_access_token/internal"):
            return 200, json.dumps({"code": 0, "tenant_access_token": "tok-mock", "expire": 7200}).encode()
        payload = json.loads(body.decode("utf-8")) if body else {}
        sent_calls.append((method, url, payload))
        return 200, json.dumps({"code": 0, "data": {"message_id": "om_gz_card_001"}}).encode()

    cli = FeishuOpenAPIClient(
        app_id="cli_mock",
        app_secret="sec_mock",
        receive_id="ou_steward",
        transport=_transport,
    )
    return cli, _transport


@pytest.mark.parametrize(
    "choice_id,expected_exceed_status,expected_within_status",
    [
        ("HARD_REJECT", "REJECTED", "COMMITTED"),
        ("REQUIRE_APPROVAL", "REJECTED", "COMMITTED"),
        ("ALLOW_AUTONOMOUS", "COMMITTED", "COMMITTED"),
    ],
)
def test_gray_zone_disambiguation_e2e_all_three_steward_rulings(
    choice_id: str,
    expected_exceed_status: str,
    expected_within_status: str,
) -> None:
    gov, bp_v1, bundle, _sr, (pos_evs, neg_evs), projected, ambiguities = _setup_clinic_v1_governance()
    try:
        assert any(a.kind == "limit_exceedance" for a in ambiguities)
        assert any(a.kind == "sop_ambiguity" for a in ambiguities)
        limit_amb = next(a for a in ambiguities if a.kind == "limit_exceedance")
        assert limit_amb.sample_exceeding_id is not None and limit_amb.sample_compliant_id is not None
        assert len(limit_amb.counterexample_samples) >= 1

        feishu_calls: list[tuple[str, str, dict[str, Any]]] = []
        feishu_cli, _ = _mock_feishu_transport(feishu_calls)
        ui_sink: list[Any] = []
        seed = build_business_seed_ontology()
        evo_pos_evs = [
            *pos_evs,
            ReplayEvent(
                name="disambig_compliant_sample_positive",
                action=limit_amb.target_action,
                params={limit_amb.id_param: limit_amb.sample_compliant_id},
                actor=AI_OPS_ACTOR,
                expected_valid=True,
                expected_requires_approval=False,
            ),
        ]
        evo_ctx = CrucibleContext(
            mode="evolution",
            m0_objects=projected,
            positive_events=evo_pos_evs,
            negative_events=neg_evs,
            default_actor=AI_OPS_ACTOR,
            seed_ontology=seed,
        )
        gw = GrayZoneDisambiguationGateway(
            governance=gov,
            model_id="clinic_gz_model",
            blueprint=bp_v1,
            crucible_context=evo_ctx,
            seed_ontology=seed,
            feishu_client=feishu_cli,
            operator_actors={"ou_steward_1": STEWARD_OPS_ACTOR, "ou_ai": AI_OPS_ACTOR},
            callback_token="cb-secret-gz",
            ui_sink=ui_sink,
        )
        rec = gw.publish_ruling_card(limit_amb)
        assert rec.message_id == "om_gz_card_001"
        assert rec.ui_artifact.kind == "approval_card"
        assert rec.ui_artifact.payload["card_subtype"] == "gray_zone_ruling"
        assert len(feishu_calls) == 1 and feishu_calls[0][0] == "POST"

        # Verify card has interactive buttons for all 3 branches
        action_blocks = [el for el in rec.feishu_card["elements"] if el.get("tag") == "action"]
        assert len(action_blocks) == 1
        btn_choices = [b["value"]["choice_id"] for b in action_blocks[0]["actions"]]
        assert btn_choices == ["HARD_REJECT", "REQUIRE_APPROVAL", "ALLOW_AUTONOMOUS"]

        # Reject invalid callback token & non-steward actor
        bad_payload = {
            "open_id": "ou_steward_1",
            "action": {"value": {"kind": "gray_zone_ruling", "card_id": rec.card_id, "ambiguity_id": limit_amb.ambiguity_id, "choice_id": choice_id, "token": "wrong"}},
        }
        with pytest.raises(PermissionDeniedError):
            parse_feishu_ruling_callback(bad_payload, callback_token="cb-secret-gz")

        non_steward_payload = {
            "open_id": "ou_ai",
            "action": {"value": {"kind": "gray_zone_ruling", "card_id": rec.card_id, "ambiguity_id": limit_amb.ambiguity_id, "choice_id": choice_id, "token": "cb-secret-gz"}},
        }
        with pytest.raises(PermissionDeniedError):
            gw.handle_ruling_callback(non_steward_payload, target_version="1.1.0")

        # Steward executes callback -> compiles GoldenEvalCase + EvolutionPatch -> 5-Gate Crucible -> crystallizes
        steward_payload = {
            "schema": "2.0",
            "event": {
                "operator": {"open_id": "ou_steward_1"},
                "action": {
                    "value": {
                        "kind": "gray_zone_ruling",
                        "card_id": rec.card_id,
                        "ambiguity_id": limit_amb.ambiguity_id,
                        "choice_id": choice_id,
                        "token": "cb-secret-gz",
                    }
                },
            },
        }
        res = gw.handle_ruling_callback(steward_payload, target_version="1.1.0")
        assert res["crucible_passed"] is True
        assert res["version"] == "1.1.0"
        assert len(res["golden_eval_cases"]) >= 1
        # Feishu card was patched to green CRYSTALLIZED terminal state
        assert len(feishu_calls) == 2 and feishu_calls[1][0] == "PATCH"
        assert res["card"]["header"]["template"] == "green"
        assert ui_sink[-1].kind == "approval_card"
        assert ui_sink[-1].payload["card_subtype"] == "gray_zone_ruling"

        # Runtime behavioral check on crystallized v1.1.0 M1
        spec_v11 = compile_blueprint(res["blueprint"], seed_ontology=seed)
        register_work_card_primitives(spec_v11)
        rt = OntologyRuntime(spec_v11, db_path=":memory:")
        try:
            order_rank = {name: i for i, name in enumerate(spec_v11.object_types)}
            for entry in sorted(projected, key=lambda e: order_rank.get(e["object_type"], 999)):
                cls = spec_v11.get_object_type(entry["object_type"])
                rt.put_object(cls(**entry["data"]))

            exc_code: str | None = None
            try:
                out_exc = rt.submit_action(
                    "GZ-RT-EXC",
                    limit_amb.target_action,
                    {limit_amb.id_param: limit_amb.sample_exceeding_id},
                    AI_OPS_ACTOR,
                )
                exc_status = out_exc["status"]
            except InvariantError as exc:
                exc_status = "REJECTED"
                exc_code = exc.code
            assert exc_status == expected_exceed_status
            assert rt.list_action_runs(status="PENDING_APPROVAL") == []

            if choice_id == "HARD_REJECT":
                assert exc_code == limit_amb.error_code
            elif choice_id == "REQUIRE_APPROVAL":
                assert exc_code == f"APPROVAL_REQUIRED_{limit_amb.error_code}"
                wc_open = rt.submit_action(
                    "GZ-WC-OPEN",
                    "open_work_card",
                    {
                        "card_id": "WC-GZ-01",
                        "card_kind": "APPROVAL",
                        "target_object_type": limit_amb.target_object_type,
                        "target_object_id": limit_amb.sample_exceeding_id,
                        "requested_action": limit_amb.target_action,
                        "requested_params_json": json.dumps(
                            {limit_amb.id_param: limit_amb.sample_exceeding_id}
                        ),
                        "assignee_role": "steward",
                    },
                    AI_OPS_ACTOR,
                )
                assert wc_open["status"] == "COMMITTED"
                settled = settle_work_card(
                    rt,
                    "WC-GZ-01",
                    STEWARD_OPS_ACTOR,
                    resolution_note="steward approved gray-zone exceedance",
                )
                assert settled["status"] == "COMMITTED"
                assert settled["settled_action_run"]["status"] == "COMMITTED"
                assert rt.get_object(WorkCard, "WC-GZ-01", STEWARD_OPS_ACTOR).status == "COMPLETED"
                assert rt.list_action_runs(status="PENDING_APPROVAL") == []

            out_ok = rt.submit_action(
                "GZ-RT-OK",
                limit_amb.target_action,
                {limit_amb.id_param: limit_amb.sample_compliant_id},
                AI_OPS_ACTOR,
            )
            assert out_ok["status"] == expected_within_status
        finally:
            rt.close()
    finally:
        gov.close()


def test_live_odoo19_gray_zone_card_extraction_read_only() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    ro = ReadOnlyOdooClient(db=DB_NORTH, client=raw)
    scout_report = OdooFDEScout(ro, source_locator=f"odoo://{DB_NORTH}", max_models=6).scout()
    profile = build_closed_phenomenon_profile(scout_report, client=ro)
    from examples.odoo_fde_prober import EphemeralOdooSandbox

    with EphemeralOdooSandbox(source_db=DB_NORTH) as sb:
        probe_report = OdooActiveProber(sb, sandbox_db=sb.db).probe(scout_report)
    bundle = synthesize_from_fde_evidence(
        scout_report, probe_report, profile=profile, seed_ontology=build_business_seed_ontology()
    )
    ambiguities = extract_scout_gray_zone_ambiguities(
        scout_report=scout_report,
        profile=profile,
        blueprint=bundle.blueprint,
        adapter_mapping=bundle.adapter_mapping,
    )
    assert len(ambiguities) >= 1
    assert any(a.counterexample_samples for a in ambiguities)
