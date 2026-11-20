"""实弹实验：用真实在线 LLM（Gemini / Antigravity / OpenRouter）作为符号主义训练的「梯度更新与假设提出器」。

严禁任何剧本或标准答案提示：
1. **实验 A · L1 真实 LLM 梯度更新（`v1.0.0 -> v1.1.0`）**：
   * 在隔离克隆 Odoo 19（``erp_north``）上跑出零硬编码 ``autonomous_o2c_fde v1.0.0`` 并由
     ``OntologyUtilityEvaluator`` 实测抓出 ``T2-1``（超授信大单脏写）与 ``T2-2``（未发货开票脏写）；
   * 不预设任何错误码或 AST 提示，直接把 ``v1.0.0`` 蓝图 + 真实失败摩擦聚类 + ``BUSINESS_SEED_ONTOLOGY``
     交给真实在线 LLM，由其自主提出 ``EvolutionPatch``；
   * 经 5 关 ``SymbolicCrucible`` 证伪（若被打回则把精确反例当作“符号梯度”回喂，最多 4 轮 CEGIS）；
   * 结晶成功后，**直接把该 LLM 自己写出的 ``v1.1.0`` 放回真实克隆 Odoo 19 物理账本重测**，
     量化对比 ``v1.0.0`` → LLM-evolved ``v1.1.0`` 的真实效用增益。
2. **实验 B · 0 → 1 真实 LLM 语义提纯（去机器表壳味 + 剪除前端噪声列）**：
   * 把探针 1+2+3 在 ``erp_north`` 上发现的原始事实 + ``BUSINESS_SEED_ONTOLOGY`` 喂给真实 LLM，
     观察其是否能主动剔除 ``can_image_1024_be_zoomed`` 等技术噪声列、重命名为纯业务概念，
     并通过 5 关 ``SymbolicCrucible``。

运行方式::

    python3 -m examples.live_llm_cegis_experiment --mode both --models agy/gemini-4-neon-medium
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
import urllib.error
from collections import defaultdict
from pathlib import Path
from typing import Any

from open_ontology import OntologyRuntime
from open_ontology.runtime import SimulationResult
from open_ontology.sandbox import ObjectDiff
from open_ontology.spec import PermissionDeniedError
from open_ontology.induction import (
    AbductionBackend,
    ActionExecutionBinding,
    AdapterMappingSpec,
    AntigravityCLIAbductionBackend,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EvolutionPatch,
    EvolutionRoutingPlan,
    FrictionClusterSummary,
    GeminiAbductionBackend,
    IncidentCounterexample,
    InductionGovernanceService,
    OntologyBuilderAgent,
    OntologyEvolverAgent,
    OntologyL2ArchitectAgent,
    OpenRouterAbductionBackend,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    apply_evolution_patch,
    compile_blueprint,
    is_strict_utility_improvement,
    measure_agent_complexity,
    triage_utility_failures,
)

from .business_ontology import build_business_seed_ontology
from .odoo_dual_erp_demo import DB_NORTH, _jsonrpc
from .odoo_fde_autonomous_pipeline import (
    AI_OPS_ACTOR,
    STEWARD_OPS_ACTOR,
    build_auto_replay_factory,
    build_closed_phenomenon_profile,
    synthesize_from_fde_evidence,
)
from .odoo_fde_prober import EphemeralOdooSandbox, OdooActiveProber, discover_transient_wizards
from .odoo_fde_scout import OdooFDEScout, ReadOnlyOdooClient
from .odoo_utility_benchmark import (
    LARGE_QTY,
    LARGE_UNIT_PRICE,
    SMALL_QTY,
    SMALL_UNIT_PRICE,
    PhysicalOdooHarness,
    _find_binding,
    _find_l2_bindings,
    _first_extra_param_name,
    _sync_fixtures_into_autonomous_runtime,
    build_reference_l1_patch_for_autonomous_v1,
    evaluate_autonomous_fde_regime,
    evaluate_raw_odoo_baseline,
)


def _log(msg: str) -> None:
    print(f"[live-cegis] {msg}", file=sys.stderr, flush=True)


def _make_live_backend(model_spec: str) -> tuple[str, AbductionBackend]:
    spec = model_spec.strip()
    if spec.startswith("agy/"):
        slug = spec[len("agy/") :]
        return spec, AntigravityCLIAbductionBackend(model=slug, timeout_sec=240)
    if spec.startswith("openrouter/"):
        slug = spec[len("openrouter/") :]
        return spec, OpenRouterAbductionBackend(model=slug, timeout_sec=120)
    if "/" in spec:
        return f"openrouter/{spec}", OpenRouterAbductionBackend(model=spec, timeout_sec=120)
    if spec.startswith("gemini") or spec.startswith("gemma"):
        return spec, GeminiAbductionBackend(model=spec, timeout_sec=90)
    raise ValueError(
        f"unsupported model_spec {model_spec!r}; use agy/<model>, openrouter/<provider>/<model>, or gemini-*"
    )


def _probe_backend_auth(backend: AbductionBackend) -> tuple[bool, str]:
    """Quick 1-shot connectivity/auth check before burning a multi-round CEGIS loop."""
    try:
        resp = backend.propose_json(
            system_prompt='Reply ONLY with JSON: {"ok": true}',
            user_prompt="ping",
            round_index=0,
        )
        return True, f"reachable (resp={resp})"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return False, f"HTTPError {exc.code}: {body or exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run_live_llm_l1_gradient_experiment(
    *,
    models: list[str],
    source_db: str = DB_NORTH,
    max_rounds: int = 4,
) -> dict[str, Any]:
    """Run live LLMs head-to-head as L1 symbolic gradient updaters on real cloned Odoo 19."""
    seed = build_business_seed_ontology()
    ro_client = ReadOnlyOdooClient(db=source_db)

    # First check auth/connectivity for every requested model
    backends: dict[str, tuple[AbductionBackend | None, str]] = {}
    for m in models:
        try:
            name, be = _make_live_backend(m)
            ok, msg = _probe_backend_auth(be)
            backends[name] = (be if ok else None, msg)
        except Exception as exc:
            backends[m] = (None, f"{type(exc).__name__}: {exc}")

    with EphemeralOdooSandbox(source_db=source_db) as sandbox_client:
        sandbox_client._ensure_login()
        harness = PhysicalOdooHarness(live_client=sandbox_client._client)
        _, _, raw_total_models, raw_total_fields = harness.measure_raw_schema_counts()

        # 1. Build real v1.0.0 baseline on untouched erp_north clone
        scout_report = OdooFDEScout(ro_client, source_locator=f"odoo://{source_db}", max_models=6).scout()
        profile = build_closed_phenomenon_profile(scout_report, client=ro_client)
        probe_report = OdooActiveProber(sandbox_client, sandbox_db=sandbox_client.db).probe(scout_report)
        bundle = synthesize_from_fde_evidence(
            scout_report,
            probe_report,
            profile=profile,
            seed_ontology=seed,
            model_id="autonomous_o2c_fde",
            version="1.0.0",
        )
        _, raw_baseline_tokens = evaluate_raw_odoo_baseline(harness)

        # Crystallize v1.0.0 cleanly and evaluate on real cloned Odoo 19 to obtain REAL T2-1 / T2-2 failures
        with InductionGovernanceService(db_path=":memory:") as base_gov:
            builder = OntologyBuilderAgent(
                backend=CallableAbductionBackend(
                    lambda _s, _u, _r: {
                        "rationale": "FDE v1.0.0 baseline",
                        "blueprint": bundle.blueprint.model_dump(mode="json"),
                        "adapter_mapping": bundle.adapter_mapping.model_dump(mode="json"),
                        "proposed_eval_cases": [],
                    }
                ),
                governance=base_gov,
                crucible=SymbolicCrucible(),
                seed_ontology=seed,
            )
            gen = builder.run_genesis_cycle(
                model_id="autonomous_o2c_fde",
                external_profile=profile,
                crucible_context=CrucibleContext(
                    mode="genesis",
                    replay_factory=build_auto_replay_factory(bundle, seed_ontology=seed),
                    default_actor=AI_OPS_ACTOR,
                    seed_ontology=seed,
                ),
                seed_ontology=seed,
                max_rounds=2,
            )
            assert gen.verified and gen.pending_approval_run_id is not None
            base_gov.approve_crystallization(gen.pending_approval_run_id, STEWARD_ACTOR)
            bp_v1 = base_gov.get_active_blueprint("autonomous_o2c_fde")
            mapping_v1 = base_gov.get_active_adapter_mapping("autonomous_o2c_fde")
            assert bp_v1 is not None and mapping_v1 is not None

        sc_v1 = evaluate_autonomous_fde_regime(
            regime_name="autonomous_fde_v1.0",
            blueprint=bp_v1,
            adapter_mapping=mapping_v1,
            harness=harness,
            sandbox_client=sandbox_client,
            raw_baseline_tokens=raw_baseline_tokens,
            raw_total_models=raw_total_models,
            raw_total_fields=raw_total_fields,
            seed_ontology=seed,
        )
        routing_v1 = triage_utility_failures(sc_v1)  # NOTE: NO secret expected_error_codes!

        confirm_b = _find_binding(mapping_v1, table="sale.order", method_substr="confirm")
        post_b = _find_binding(mapping_v1, table="account.move", method_substr="post")
        assert confirm_b is not None and post_b is not None

        # Seed clean pre-execution fixtures in cloned Odoo for Gate 2 + Gate 3 verification
        ep = harness.create_partner("LIVE-LLM-P", "Live LLM Proof Customer")
        es = harness.create_draft_order(partner_id=ep, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="LLM-POS-1")
        el = harness.create_draft_order(partner_id=ep, qty=LARGE_QTY, unit_price=LARGE_UNIT_PRICE, client_ref="LLM-NEG-1")
        esh = harness.create_draft_order(partner_id=ep, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="LLM-POS-2")
        p_done = harness.confirm_order_physically(esh.order_id)
        harness.ship_order_physically_full(esh.order_id)
        inv_ok = harness.create_draft_invoice_for_order(esh.order_id)

        eu = harness.create_draft_order(partner_id=ep, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="LLM-NEG-2")
        p_open = harness.confirm_order_physically(eu.order_id)
        inv_bad = harness.create_draft_invoice_for_order(eu.order_id)

        rt_m0 = OntologyRuntime(compile_blueprint(bp_v1, seed_ontology=seed), db_path=":memory:")
        try:
            evo_m0 = _sync_fixtures_into_autonomous_runtime(
                runtime=rt_m0,
                adapter_mapping=mapping_v1,
                sandbox_client=sandbox_client,
                table_ids={
                    "res.partner": [ep],
                    "sale.order": [es.order_id, el.order_id, esh.order_id, eu.order_id],
                    "stock.picking": [p_done, p_open],
                    "account.move": [inv_ok, inv_bad],
                },
            )
        finally:
            rt_m0.close()

        t21_cid = f"{confirm_b.id_prefix}{el.order_id}"
        t22_cid = f"{post_b.id_prefix}{inv_bad}"
        pos1_cid = f"{confirm_b.id_prefix}{es.order_id}"
        pos2_cid = f"{post_b.id_prefix}{inv_ok}"

        # Unbiased counterexamples: only require valid=False (NO secret error code naming!)
        l1_pkg = routing_v1.l1_repair_candidates[0]
        cxs = [
            IncidentCounterexample(
                name="CX-T2-1-OVER-CREDIT",
                action=confirm_b.action_name,
                params={confirm_b.id_param: t21_cid},
                actor=AI_OPS_ACTOR,
                expect_valid=False,
                expect_error_code=None,
            ),
            IncidentCounterexample(
                name="CX-T2-2-UNSHIPPED-INVOICE",
                action=post_b.action_name,
                params={post_b.id_param: t22_cid},
                actor=AI_OPS_ACTOR,
                expect_valid=False,
                expect_error_code=None,
            ),
        ]
        pos_events = [
            ReplayEvent(
                name="POS-CONFIRM-SMALL-AI",
                action=confirm_b.action_name,
                params={confirm_b.id_param: pos1_cid},
                actor=AI_OPS_ACTOR,
                expected_valid=True,
            ),
            ReplayEvent(
                name="POS-CONFIRM-LARGE-STEWARD",
                action=confirm_b.action_name,
                params={confirm_b.id_param: t21_cid},
                actor=STEWARD_OPS_ACTOR,
                expected_valid=True,
            ),
            ReplayEvent(
                name="POS-POST-SHIPPED-AI",
                action=post_b.action_name,
                params={post_b.id_param: pos2_cid},
                actor=AI_OPS_ACTOR,
                expected_valid=True,
            ),
        ]

        # Run each live LLM independently in its own clean governance service
        per_model_results: dict[str, Any] = {}
        for model_name, (backend, auth_msg) in backends.items():
            if backend is None:
                per_model_results[model_name] = {
                    "status": "AUTH_OR_ENDPOINT_FAILED",
                    "error": auth_msg,
                }
                continue

            t0 = time.monotonic()
            with InductionGovernanceService(db_path=":memory:") as gov:
                # Seed base v1.0.0 into this model's clean governance DB
                b0 = OntologyBuilderAgent(
                    backend=CallableAbductionBackend(
                        lambda _s, _u, _r: {
                            "rationale": "FDE v1.0.0 baseline",
                            "blueprint": bp_v1.model_dump(mode="json"),
                            "adapter_mapping": mapping_v1.model_dump(mode="json"),
                            "proposed_eval_cases": [],
                        }
                    ),
                    governance=gov,
                    crucible=SymbolicCrucible(),
                    seed_ontology=seed,
                )
                g0 = b0.run_genesis_cycle(
                    model_id="autonomous_o2c_fde",
                    external_profile=profile,
                    crucible_context=CrucibleContext(
                        mode="genesis",
                        replay_factory=build_auto_replay_factory(bundle, seed_ontology=seed),
                        default_actor=AI_OPS_ACTOR,
                        seed_ontology=seed,
                    ),
                    seed_ontology=seed,
                    max_rounds=2,
                )
                gov.approve_crystallization(g0.pending_approval_run_id, STEWARD_ACTOR)  # type: ignore[arg-type]

                # Now let the REAL LIVE LLM act as the L1 Symbolic Gradient Updater!
                _log(f"ExpA: starting L1 evolution cycle for {model_name}")
                evolver = OntologyEvolverAgent(
                    backend=backend,
                    governance=gov,
                    crucible=SymbolicCrucible(),
                    seed_ontology=seed,
                )
                evo_cycle = evolver.run_evolution_cycle(
                    model_id="autonomous_o2c_fde",
                    cluster=l1_pkg.cluster,
                    crucible_context=CrucibleContext(
                        mode="evolution",
                        m0_objects=evo_m0,
                        positive_events=pos_events,
                        incident_counterexamples=cxs,
                        default_actor=AI_OPS_ACTOR,
                        seed_ontology=seed,
                    ),
                    seed_ontology=seed,
                    max_rounds=max_rounds,
                )
                elapsed_sec = round(time.monotonic() - t0, 2)
                _log(
                    f"ExpA: finished CEGIS for {model_name} verified={evo_cycle.verified} "
                    f"rounds={len(evo_cycle.rounds)} elapsed={elapsed_sec}s"
                )

                model_entry: dict[str, Any] = {
                    "status": "CRYSTALLIZED" if evo_cycle.verified else "REFUTED_AFTER_MAX_ROUNDS",
                    "elapsed_sec": elapsed_sec,
                    "rounds_used": len(evo_cycle.rounds),
                    "round_traces": [
                        {
                            "round": tr.round_index,
                            "passed": tr.passed,
                            "failed_gates": list(tr.failed_gates),
                            "feedback_preview": tr.feedback[:600],
                        }
                        for tr in evo_cycle.rounds
                    ],
                }
                if getattr(backend, "last_usage", None):
                    model_entry["last_usage"] = backend.last_usage

                if evo_cycle.verified and evo_cycle.pending_approval_run_id is not None:
                    gov.approve_crystallization(evo_cycle.pending_approval_run_id, STEWARD_ACTOR)
                    llm_bp = gov.get_active_blueprint("autonomous_o2c_fde")
                    assert llm_bp is not None

                    _log(f"ExpA: evaluating {model_name} v1.1 on live cloned Odoo 19")
                    # Re-evaluate the REAL LLM's OWN crystallized ontology on REAL CLONED ODOO 19!
                    sc_llm = evaluate_autonomous_fde_regime(
                        regime_name=f"llm_{model_name}_v1.1",
                        blueprint=llm_bp,
                        adapter_mapping=mapping_v1,
                        harness=harness,
                        sandbox_client=sandbox_client,
                        raw_baseline_tokens=raw_baseline_tokens,
                        raw_total_models=raw_total_models,
                        raw_total_fields=raw_total_fields,
                        seed_ontology=seed,
                    )
                    model_entry["proposed_patch"] = (
                        evo_cycle.patch.model_dump(mode="json") if evo_cycle.patch else None
                    )
                    crystallized_golden = gov.get_golden_eval_cases("autonomous_o2c_fde")
                    updated_action_names = (
                        {a.name for a in evo_cycle.patch.update_actions}
                        if evo_cycle.patch is not None
                        else set()
                    )
                    reverted_actions = [
                        copy.deepcopy(a) for a in bp_v1.actions if a.name in updated_action_names
                    ]
                    gate4_caught = False
                    if crystallized_golden and reverted_actions:
                        reg_patch = EvolutionPatch(
                            base_version=llm_bp.version,
                            target_version="1.2.0",
                            rationale="Synthetic regression check stripping v1.1 guards",
                            update_actions=reverted_actions,
                        )
                        regressed_bp = apply_evolution_patch(llm_bp, reg_patch)
                        reg_report = SymbolicCrucible().evaluate(
                            regressed_bp,
                            CrucibleContext(
                                mode="evolution",
                                m0_objects=evo_m0,
                                golden_eval_cases=crystallized_golden,
                                default_actor=AI_OPS_ACTOR,
                                seed_ontology=seed,
                            ),
                            patch=reg_patch,
                        )
                        gate4_caught = (
                            not reg_report.passed
                            and "gate_4_golden_regression"
                            in [g.gate for g in reg_report.failed_gates()]
                        )
                    model_entry["golden_cases_count"] = len(crystallized_golden)
                    model_entry["golden_cases_with_setup_objects"] = sum(
                        1 for c in crystallized_golden if c.get("setup_objects")
                    )
                    model_entry["gate4_caught_v1_2_regression"] = gate4_caught
                    model_entry["live_odoo_evaluation"] = {
                        "anti_smuggling_passed": sc_llm.anti_smuggling.passed,
                        "metrics": sc_llm.to_dict()["metrics"],
                        "strict_improvement_over_v1_0": is_strict_utility_improvement(sc_v1, sc_llm),
                        "t2_probes": [
                            {
                                "probe_id": p.probe_id,
                                "passed": p.passed,
                                "dirty": p.dirty_external_write,
                                "observed": p.observed_outcome,
                            }
                            for p in sc_llm.probes
                            if p.bucket == "T2_governance"
                        ],
                    }
                per_model_results[model_name] = model_entry

        return {
            "source_db": source_db,
            "sandbox_db": sandbox_client.db,
            "v1_0_baseline_metrics": sc_v1.to_dict()["metrics"],
            "models": per_model_results,
        }


_UI_NOISE_FIELDS = frozenset(
    {
        "can_image_1024_be_zoomed",
        "invoice_partner_display_name",
        "property_stock_customer",
        "property_stock_supplier",
        "email_normalized",
        "parent_path",
        "checked",
        "auto_post",
    }
)


def _pick_clean_candidate(
    candidates: list[dict[str, Any]],
    ot: Any,
    migrated_by_type: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Generically prefer a candidate whose numeric measure does not exceed a positive parent limit."""
    limit_kws = ("limit", "cap", "quota", "ceiling", "credit")
    measure_kws = ("amount", "total", "value", "price")
    rel_map: dict[str, dict[str, dict[str, Any]]] = {}
    for rel in getattr(ot, "relations", ()):
        tgt_ot = getattr(rel, "target_type", None)
        if not tgt_ot:
            continue
        parent_rows = migrated_by_type.get(tgt_ot, [])
        if parent_rows:
            # Index by first string *_id / record_id key on parent
            pk_guess = next(
                (k for k in parent_rows[0] if k == "record_id" or k.endswith("_id")),
                None,
            )
            if pk_guess:
                rel_map[rel.field] = {
                    str(pr[pk_guess]): pr for pr in parent_rows if pr.get(pk_guess) is not None
                }

    def _score(row: dict[str, Any]) -> tuple[int, float]:
        amt_vals = [
            float(v)
            for k, v in row.items()
            if any(kw in k.lower() for kw in measure_kws)
            and isinstance(v, (int, float))
            and not isinstance(v, bool)
        ]
        amt = max(amt_vals, default=0.0)
        for rel_field, idx in rel_map.items():
            ref_id = row.get(rel_field)
            parent = idx.get(str(ref_id)) if ref_id is not None else None
            if parent is None:
                continue
            lim_vals = [
                float(v)
                for k, v in parent.items()
                if any(kw in k.lower() for kw in limit_kws)
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
            ]
            if lim_vals:
                lim = max(lim_vals)
                if lim > 0 and amt <= lim:
                    return (0, amt)
                if amt <= lim or lim == 0:
                    return (1, amt)
                return (3, amt)
        return (2, amt)

    return min(candidates, key=_score)


def _build_genesis_replay_factory(scout_report: Any, probe_report: Any) -> Any:
    """Domain-neutral, mapping-aware Gate 3 replay factory for 0->1 genesis experiments."""
    relational_models = {r.source_model for r in scout_report.discovered_relations} | {
        r.target_model for r in scout_report.discovered_relations
    }
    reversal_edges_by_model: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for sm in scout_report.mined_state_machines:
        for rev_desc in sm.backward_or_reversal_edges:
            m = re.match(r"^([^\s]+)\s*->\s*([^\s]+)", rev_desc)
            if m:
                reversal_edges_by_model[sm.model].add((m.group(1), m.group(2)))

    def _is_reversal_obs(obs: Any) -> bool:
        if (obs.state_before, obs.state_after) in reversal_edges_by_model.get(obs.model, set()):
            return True
        return any(
            kw in obs.method.lower() or kw in obs.state_after.lower()
            for kw in ("cancel", "reverse", "draft")
        )

    proven_transition_states: set[tuple[str, str, str]] = {
        (obs.model, obs.method, obs.state_before)
        for obs in probe_report.observations
        if obs.outcome == "TRANSITION" and obs.state_before != obs.state_after
    }

    def _factory(
        migrated: list[dict[str, Any]],
        candidate: DeclarativeBlueprint | None = None,
        adapter_mapping: Any = None,
    ) -> tuple[list[ReplayEvent], list[ReplayEvent]]:
        pos: list[ReplayEvent] = []
        neg: list[ReplayEvent] = []

        if adapter_mapping is None or not getattr(adapter_mapping, "action_bindings", None):
            pos.append(
                ReplayEvent(
                    name="missing_action_bindings_in_adapter_mapping",
                    action="__missing_action_bindings__",
                    params={},
                    actor=AI_OPS_ACTOR,
                    expected_valid=True,
                )
            )
            return pos, neg

        em_by_type = {em.target_object_type: em for em in adapter_mapping.entity_mappings}
        ot_by_name = {ot.name: ot for ot in candidate.object_types} if candidate is not None else {}
        act_by_name = {a.name: a for a in candidate.actions} if candidate is not None else {}
        binding_by_mm: dict[tuple[str, str], ActionExecutionBinding] = {
            (b.external_table, b.external_method): b for b in adapter_mapping.action_bindings
        }

        # Require every forward causal transition on discovered relational models to be bound
        seen_required: set[tuple[str, str]] = set()
        for obs in probe_report.observations:
            if obs.outcome != "TRANSITION" or obs.state_before == obs.state_after:
                continue
            if obs.model not in relational_models or _is_reversal_obs(obs):
                continue
            mm = (obs.model, obs.method)
            if mm in seen_required:
                continue
            seen_required.add(mm)
            if mm not in binding_by_mm:
                pos.append(
                    ReplayEvent(
                        name=f"missing_action_binding_for_{obs.model}_{obs.method}",
                        action=f"__missing_binding_{obs.model}_{obs.method}__",
                        params={},
                        actor=AI_OPS_ACTOR,
                        expected_valid=True,
                    )
                )

        # Verify structural alignment between each ActionExecutionBinding, EntityMappingSpec, and Blueprint Action
        for b in adapter_mapping.action_bindings:
            em = em_by_type.get(b.target_object_type)
            ot = ot_by_name.get(b.target_object_type)
            act = act_by_name.get(b.action_name)
            if em is None or ot is None or act is None:
                pos.append(
                    ReplayEvent(
                        name=f"broken_action_binding_{b.action_name}",
                        action=f"__broken_binding_{b.action_name}__",
                        params={},
                        actor=AI_OPS_ACTOR,
                        expected_valid=True,
                    )
                )
                continue
            if (
                em.id_transform.kind != "id_prefix"
                or not em.id_transform.prefix
                or b.id_prefix != em.id_transform.prefix
            ):
                pos.append(
                    ReplayEvent(
                        name=(
                            f"id_prefix_mismatch_{b.action_name}:"
                            f"binding={b.id_prefix!r}_vs_entity={em.id_transform.prefix!r}"
                        ),
                        action=f"__id_prefix_mismatch_{b.action_name}__",
                        params={},
                        actor=AI_OPS_ACTOR,
                        expected_valid=True,
                    )
                )
            if b.id_param not in {p.name for p in act.params}:
                pos.append(
                    ReplayEvent(
                        name=f"id_param_mismatch_{b.action_name}:{b.id_param!r}",
                        action=f"__id_param_mismatch_{b.action_name}__",
                        params={},
                        actor=AI_OPS_ACTOR,
                        expected_valid=True,
                    )
                )

        if pos:
            return pos, neg

        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in migrated:
            by_type[row["object_type"]].append(row["data"])

        seen_pos: set[tuple[str, str]] = set()
        seen_neg: set[tuple[str, str]] = set()

        for obs in probe_report.observations:
            binding = binding_by_mm.get((obs.model, obs.method))
            if binding is None:
                continue
            em = em_by_type.get(binding.target_object_type)
            ot = ot_by_name.get(binding.target_object_type)
            if em is None or ot is None:
                continue

            status_tf = next(
                (tf for tf in em.field_transforms if tf.source_field == obs.status_field),
                None,
            )
            target_status_field = status_tf.target_field if status_tf is not None else ot.status_field
            if status_tf is not None and status_tf.kind == "enum_map":
                mapped_before = status_tf.enum_map.get(obs.state_before, obs.state_before)
            else:
                mapped_before = obs.state_before

            candidates = [
                c
                for c in by_type.get(binding.target_object_type, [])
                if str(c.get(target_status_field)) == mapped_before
            ]
            if not candidates:
                continue
            pk_field = ot.primary_key

            if obs.outcome == "TRANSITION" and obs.state_before != obs.state_after:
                key = (binding.action_name, obs.state_before)
                if key in seen_pos:
                    continue
                seen_pos.add(key)
                is_rev = _is_reversal_obs(obs)
                chosen = (
                    candidates[0]
                    if is_rev
                    else _pick_clean_candidate(candidates, ot, by_type)
                )
                pos.append(
                    ReplayEvent(
                        name=f"auto_pos_{binding.action_name}_from_{obs.state_before}",
                        action=binding.action_name,
                        params={binding.id_param: chosen[pk_field]},
                        actor=STEWARD_OPS_ACTOR if is_rev else AI_OPS_ACTOR,
                        expected_valid=True,
                    )
                )
            elif obs.outcome == "NATIVE_ERROR":
                if (obs.model, obs.method, obs.state_before) in proven_transition_states:
                    continue
                key = (binding.action_name, obs.state_before)
                if key in seen_neg:
                    continue
                seen_neg.add(key)
                chosen = candidates[0]
                neg.append(
                    ReplayEvent(
                        name=f"auto_neg_{binding.action_name}_from_{obs.state_before}",
                        action=binding.action_name,
                        params={binding.id_param: chosen[pk_field]},
                        actor=AI_OPS_ACTOR,
                        expected_valid=False,
                        expected_error_code=None,
                    )
                )
        return pos, neg

    return _factory


def run_live_llm_genesis_purification_experiment(
    *,
    models: list[str],
    source_db: str = DB_NORTH,
    max_rounds: int = 4,
) -> dict[str, Any]:
    """Experiment B: live LLM 0->1 semantic purification & noise pruning on cloned Odoo 19."""
    seed = build_business_seed_ontology()
    ro_client = ReadOnlyOdooClient(db=source_db)

    backends: dict[str, tuple[AbductionBackend | None, str]] = {}
    for m in models:
        try:
            name, be = _make_live_backend(m)
            ok, msg = _probe_backend_auth(be)
            backends[name] = (be if ok else None, msg)
        except Exception as exc:
            backends[m] = (None, f"{type(exc).__name__}: {exc}")

    with EphemeralOdooSandbox(source_db=source_db) as sandbox_client:
        sandbox_client._ensure_login()
        harness = PhysicalOdooHarness(live_client=sandbox_client._client)
        _, _, raw_total_models, raw_total_fields = harness.measure_raw_schema_counts()
        _, raw_baseline_tokens = evaluate_raw_odoo_baseline(harness)

        scout_report = OdooFDEScout(ro_client, source_locator=f"odoo://{source_db}", max_models=6).scout()
        profile = build_closed_phenomenon_profile(scout_report, client=ro_client)
        probe_report = OdooActiveProber(sandbox_client, sandbox_db=sandbox_client.db).probe(scout_report)
        mech_bundle = synthesize_from_fde_evidence(
            scout_report,
            probe_report,
            profile=profile,
            seed_ontology=seed,
            model_id="mechanical_o2c_v1",
            version="1.0.0",
        )
        mech_sc = evaluate_autonomous_fde_regime(
            regime_name="mechanical_fde_v1.0",
            blueprint=mech_bundle.blueprint,
            adapter_mapping=mech_bundle.adapter_mapping,
            harness=harness,
            sandbox_client=sandbox_client,
            raw_baseline_tokens=raw_baseline_tokens,
            raw_total_models=raw_total_models,
            raw_total_fields=raw_total_fields,
            seed_ontology=seed,
        )
        mech_fields_all = [
            *(f.name for ot in mech_bundle.blueprint.object_types for f in ot.fields),
            *(r.field for ot in mech_bundle.blueprint.object_types for r in ot.relations),
        ]
        mech_noise_present = sorted(set(mech_fields_all) & _UI_NOISE_FIELDS)

        phenomenon_brief = {
            "discovered_models": [
                {
                    "model": dm.model,
                    "row_count": dm.row_count,
                    "suggested_archetype": dm.suggested_archetype,
                    "retained_fields": [
                        {
                            "name": f.name,
                            "ttype": f.ttype,
                            "label": f.label,
                            "relation_target": f.relation_target,
                            "selection_values": list(f.selection_values),
                            "non_null_rate": f.non_null_rate,
                            "entropy_bits": f.entropy_bits,
                        }
                        for f in dm.retained_fields
                    ],
                }
                for dm in scout_report.discovered_models
            ],
            "discovered_relations": [
                {
                    "source_model": r.source_model,
                    "source_field": r.source_field,
                    "target_model": r.target_model,
                    "target_field": r.target_field,
                    "kind": r.kind,
                    "confidence": r.confidence,
                    "null_rate_in_sample": r.null_rate_in_sample,
                }
                for r in scout_report.discovered_relations
            ],
            "mined_state_machines": [
                {
                    "model": sm.model,
                    "field_name": sm.field_name,
                    "initial_states": list(sm.inferred_initial_states),
                    "terminal_states": list(sm.inferred_terminal_states),
                    "transitions": [
                        {"from": e.from_state, "to": e.to_state, "count": e.count}
                        for e in sm.edges
                    ],
                    "backward_or_reversal_edges": list(sm.backward_or_reversal_edges),
                }
                for sm in scout_report.mined_state_machines
            ],
            "gray_zones": [
                {
                    "kind": ef.kind,
                    "model": ef.model,
                    "summary": ef.summary,
                    "conservative_policy": ef.conservative_default,
                }
                for ef in scout_report.epistemic_findings
            ],
            "active_probe_observations": [
                {
                    "model": o.model,
                    "method": o.method,
                    "status_field": o.status_field,
                    "state_before": o.state_before,
                    "state_after": o.state_after,
                    "outcome": o.outcome,
                    "error_summary": (o.error_message[:140] if o.error_message else None),
                }
                for o in probe_report.observations
            ],
        }

        requirement_doc = (
            "Synthesize a clean, semantically purified 0->1 L1 DeclarativeBlueprint (and complete bidirectional "
            "AdapterMappingSpec) from the external ERP discovery findings above, grounded on the SeedOntology.\n"
            "Requirements:\n"
            "1. Map the core transactional and master entities needed to govern the discovered causal actions "
            "into clean domain PascalCase ObjectTypeBlueprints inheriting appropriate SeedOntology archetypes "
            "(e.g., MasterEntity, ProcessDocument) with clean snake_case business action names.\n"
            "2. Actively prune UI/presentation/ORM noise columns and non-causal auxiliary tables that have no "
            "bearing on state transitions, relational integrity, approval governance, or monetary amounts.\n"
            "3. Provide a complete bidirectional AdapterMappingSpec:\n"
            "   - Every mapped entity's id_transform MUST use kind='id_prefix' on source_field='id' with a distinct "
            "non-empty prefix (e.g. 'CUST-'), and any foreign_key_id FieldTransformSpec pointing to that entity MUST "
            "use the exact same prefix (keep optional relations required=False).\n"
            "   - If an ObjectTypeBlueprint uses archetype='ProcessDocument', note status_field defaults to 'status': "
            "either name your status field 'status' (mapped from raw 'state') or set status_field explicitly to match "
            "your field name, and declare initial_statuses + transitions covering all valid edges.\n"
            "   - Bind every discovered forward causal TRANSITION method (and any cancellation/reversal action you "
            "expose) in action_bindings (external_table, external_method, target_object_type, id_param, id_prefix, "
            "expected_state_field, expected_state_after), with id_param matching the action's params[0].name and "
            "id_prefix matching the target entity's id_transform.prefix; keep action mutations, preconditions, "
            "transitions, and expected_state_after in the exact mapped status vocabulary.\n"
            "4. Gate backward/reversal/cancellation transitions behind steward/admin approval via role-conditional "
            "action preconditions (e.g. must_hold actor.role in ['steward', 'admin'], code='APPROVAL_REQUIRED_<REASON>') "
            "AND enforce discovered cross-entity limit and "
            "prerequisite fulfillment guardrails implied by the relations and gray-zone findings via clean action preconditions.\n"
            "5. Emit 2 to 4 self-contained ProposedEvalCase items using synthetic setup_objects."
        )

        replay_factory = _build_genesis_replay_factory(scout_report, probe_report)
        per_model_results: dict[str, Any] = {}

        for model_name, (backend, auth_msg) in backends.items():
            if backend is None:
                per_model_results[model_name] = {
                    "status": "AUTH_OR_ENDPOINT_FAILED",
                    "error": auth_msg,
                }
                continue

            t0 = time.monotonic()
            with InductionGovernanceService(db_path=":memory:") as gov:
                _log(f"ExpB: starting 0->1 genesis purification cycle for {model_name}")
                builder = OntologyBuilderAgent(
                    backend=backend,
                    governance=gov,
                    crucible=SymbolicCrucible(),
                    seed_ontology=seed,
                )
                gen_cycle = builder.run_genesis_cycle(
                    model_id="purified_o2c_ontology",
                    phenomenon_brief=phenomenon_brief,
                    requirement_doc=requirement_doc,
                    external_profile=profile,
                    crucible_context=CrucibleContext(
                        mode="genesis",
                        replay_factory=replay_factory,
                        default_actor=AI_OPS_ACTOR,
                        seed_ontology=seed,
                    ),
                    seed_ontology=seed,
                    max_rounds=max_rounds,
                )
                elapsed_sec = round(time.monotonic() - t0, 2)
                _log(
                    f"ExpB: finished genesis for {model_name} verified={gen_cycle.verified} "
                    f"rounds={len(gen_cycle.rounds)} elapsed={elapsed_sec}s"
                )

                entry: dict[str, Any] = {
                    "status": "CRYSTALLIZED" if gen_cycle.verified else "REFUTED_AFTER_MAX_ROUNDS",
                    "elapsed_sec": elapsed_sec,
                    "rounds_used": len(gen_cycle.rounds),
                    "round_traces": [
                        {
                            "round": tr.round_index,
                            "passed": tr.passed,
                            "failed_gates": list(tr.failed_gates),
                            "feedback_preview": tr.feedback[:600],
                        }
                        for tr in gen_cycle.rounds
                    ],
                }
                if getattr(backend, "last_usage", None):
                    entry["last_usage"] = backend.last_usage

                if gen_cycle.verified and gen_cycle.pending_approval_run_id is not None:
                    gov.approve_crystallization(gen_cycle.pending_approval_run_id, STEWARD_ACTOR)
                    purified_bp = gov.get_active_blueprint("purified_o2c_ontology")
                    purified_map = gov.get_active_adapter_mapping("purified_o2c_ontology")
                    assert purified_bp is not None and purified_map is not None
                    golden_cases = gov.get_golden_eval_cases("purified_o2c_ontology")

                    purified_spec = compile_blueprint(purified_bp, seed_ontology=seed)
                    purified_cx = measure_agent_complexity(
                        purified_spec,
                        regime_name=f"purified_{model_name}",
                        raw_external_entity_count=raw_total_models,
                        raw_external_field_count=raw_total_fields,
                        reference_token_budget=raw_baseline_tokens,
                    )
                    all_bp_fields = [
                        *(f.name for ot in purified_bp.object_types for f in ot.fields),
                        *(r.field for ot in purified_bp.object_types for r in ot.relations),
                    ]
                    noise_retained = sorted(set(all_bp_fields) & _UI_NOISE_FIELDS)
                    entry["purification_summary"] = {
                        "object_types": [
                            {
                                "name": ot.name,
                                "archetype": ot.archetype,
                                "field_count": len(ot.fields) + len(ot.relations),
                                "fields": [f.name for f in ot.fields],
                                "relations": [f"{r.field}->{r.target_type}" for r in ot.relations],
                            }
                            for ot in purified_bp.object_types
                        ],
                        "actions": [a.name for a in purified_bp.actions],
                        "total_fields": len(all_bp_fields),
                        "baseline_mechanical_fields": len(mech_fields_all),
                        "entity_count": len(purified_bp.object_types),
                        "baseline_mechanical_entities": len(mech_bundle.blueprint.object_types),
                        "noise_fields_retained": noise_retained,
                        "golden_cases_crystallized": len(golden_cases),
                    }
                    entry["complexity_comparison"] = {
                        "mechanical_tool_tokens": mech_sc.complexity.osdk_tool_schema_tokens_est,
                        "purified_tool_tokens": purified_cx.osdk_tool_schema_tokens_est,
                        "token_reduction_pct": round(
                            100.0
                            * (
                                mech_sc.complexity.osdk_tool_schema_tokens_est
                                - purified_cx.osdk_tool_schema_tokens_est
                            )
                            / max(1, mech_sc.complexity.osdk_tool_schema_tokens_est),
                            2,
                        ),
                    }

                    _log(f"ExpB: evaluating purified ontology for {model_name} on live cloned Odoo 19")
                    sc_purified = evaluate_autonomous_fde_regime(
                        regime_name=f"purified_{model_name}_v1.0",
                        blueprint=purified_bp,
                        adapter_mapping=purified_map,
                        harness=harness,
                        sandbox_client=sandbox_client,
                        raw_baseline_tokens=raw_baseline_tokens,
                        raw_total_models=raw_total_models,
                        raw_total_fields=raw_total_fields,
                        seed_ontology=seed,
                    )
                    entry["live_odoo_evaluation"] = {
                        "anti_smuggling_passed": sc_purified.anti_smuggling.passed,
                        "metrics": sc_purified.to_dict()["metrics"],
                        "strict_improvement_over_mechanical": is_strict_utility_improvement(
                            mech_sc, sc_purified
                        ),
                        "t2_probes": [
                            {
                                "probe_id": p.probe_id,
                                "passed": p.passed,
                                "dirty": p.dirty_external_write,
                                "observed": p.observed_outcome,
                            }
                            for p in sc_purified.probes
                            if p.bucket == "T2_governance"
                        ],
                    }

                per_model_results[model_name] = entry

        return {
            "source_db": source_db,
            "sandbox_db": sandbox_client.db,
            "mechanical_v1_baseline": {
                "entity_count": len(mech_bundle.blueprint.object_types),
                "total_fields": len(mech_fields_all),
                "noise_fields_present": mech_noise_present,
                "tool_tokens": mech_sc.complexity.osdk_tool_schema_tokens_est,
                "metrics": mech_sc.to_dict()["metrics"],
            },
            "models": per_model_results,
        }


def _probe_odoo_wizard_effects(
    sandbox_client: Any,
    harness: PhysicalOdooHarness,
) -> list[dict[str, Any]]:
    """Observe real before/after raw column transitions on cloned Odoo 19 transient workflows."""
    obs: list[dict[str, Any]] = []
    partner_id = harness.create_partner(f"WIZPROBE-{int(time.time()) % 10000}", "L2 Wizard Probe Customer")

    # 1. Partial delivery (4/10) backorder wizard effect
    f_deliv = harness.create_draft_order(partner_id=partner_id, qty=10, unit_price=1000.0, client_ref="WIZ-DELIV")
    harness.confirm_order_physically(f_deliv.order_id)
    p_before = harness.list_pickings_for_order(f_deliv.order_id)
    p_id = int(p_before[0]["id"])
    harness.validate_partial_picking_creating_backorder(f_deliv.order_id, 4)
    p_after = sandbox_client.search_read("stock.picking", [("id", "=", p_id)], ["id", "state"], limit=1)
    obs.append(
        {
            "workflow": "partial_delivery_validation",
            "table": "stock.picking",
            "before": {"state": p_before[0].get("state")},
            "after": {"state": p_after[0].get("state") if p_after else None},
        }
    )

    # 2. 50% partial payment then remaining 50% full payment
    f_pay = harness.create_draft_order(partner_id=partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="WIZ-PAY")
    harness.confirm_order_physically(f_pay.order_id)
    harness.ship_order_physically_full(f_pay.order_id)
    inv_pay_id = harness.create_draft_invoice_for_order(f_pay.order_id)
    harness.post_invoice_physically(inv_pay_id)
    inv_before = harness.read_invoice(inv_pay_id)
    harness.register_payment_physically(inv_pay_id, 10_000.0)
    inv_mid = harness.read_invoice(inv_pay_id)
    harness.register_payment_physically(inv_pay_id, 10_000.0)
    inv_full = harness.read_invoice(inv_pay_id)
    obs.append(
        {
            "workflow": "partial_and_full_customer_payment",
            "table": "account.move",
            "before": {
                "state": inv_before["state"],
                "payment_state": inv_before["payment_state"],
                "amount_total": inv_before["amount_total"],
                "amount_residual": inv_before["amount_residual"],
            },
            "after_50pct_payment": {
                "payment_state": inv_mid["payment_state"],
                "amount_residual": inv_mid["amount_residual"],
            },
            "after_remaining_50pct_payment": {
                "payment_state": inv_full["payment_state"],
                "amount_residual": inv_full["amount_residual"],
            },
        }
    )

    # 3. Credit-note reversal
    f_cn = harness.create_draft_order(partner_id=partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="WIZ-CN")
    harness.confirm_order_physically(f_cn.order_id)
    harness.ship_order_physically_full(f_cn.order_id)
    inv_cn_id = harness.create_draft_invoice_for_order(f_cn.order_id)
    harness.post_invoice_physically(inv_cn_id)
    cn_before = harness.read_invoice(inv_cn_id)
    cn_doc = harness.create_and_post_credit_note_physically(inv_cn_id)
    cn_orig_after = harness.read_invoice(inv_cn_id)
    obs.append(
        {
            "workflow": "credit_note_reversal",
            "table": "account.move",
            "before_orig": {
                "state": cn_before["state"],
                "payment_state": cn_before["payment_state"],
                "amount_residual": cn_before["amount_residual"],
            },
            "after_orig": {
                "state": cn_orig_after["state"],
                "payment_state": cn_orig_after["payment_state"],
                "amount_residual": cn_orig_after["amount_residual"],
            },
            "created_credit_note": {
                "move_type": cn_doc["move_type"],
                "state": cn_doc["state"],
                "payment_state": cn_doc["payment_state"],
                "amount_total": cn_doc["amount_total"],
            },
        }
    )
    return obs


def run_live_llm_l2_architect_experiment(
    *,
    models: list[str],
    source_db: str = DB_NORTH,
    max_rounds: int = 4,
    save_v2_bundle_path: str | None = None,
) -> dict[str, Any]:
    """Experiment C: Live LLM M1 Structural & Business-Rule Evolution (v1.1.0 -> v2.0.0) on cloned Odoo 19."""
    _log(f"ExpC (L2): scouting {source_db} and synthesizing v1.0/v1.1 baseline")
    ro_client = ReadOnlyOdooClient(db=source_db)
    scout_report = OdooFDEScout(ro_client, source_locator=f"odoo://{source_db}", max_models=6).scout()
    profile = build_closed_phenomenon_profile(scout_report, client=ro_client)
    seed = build_business_seed_ontology()

    with EphemeralOdooSandbox(source_db=source_db) as sandbox_client:
        sandbox_client._ensure_login()
        harness = PhysicalOdooHarness(live_client=sandbox_client._client)
        _, _, raw_total_models, raw_total_fields = harness.measure_raw_schema_counts()
        probe_report = OdooActiveProber(sandbox_client, sandbox_db=sandbox_client.db).probe(scout_report)

        bundle = synthesize_from_fde_evidence(
            scout_report,
            probe_report,
            profile=profile,
            seed_ontology=seed,
            model_id="autonomous_o2c_fde",
            version="1.0.0",
        )
        l1_patch = build_reference_l1_patch_for_autonomous_v1(
            bundle.blueprint, bundle.adapter_mapping, target_version="1.1.0"
        )
        bp_v11 = apply_evolution_patch(bundle.blueprint, l1_patch)
        mapping_v1 = bundle.adapter_mapping
        confirm_b = _find_binding(mapping_v1, table="sale.order", method_substr="confirm")
        post_b = _find_binding(mapping_v1, table="account.move", method_substr="post")
        validate_b = _find_binding(mapping_v1, table="stock.picking", method_substr="validate")
        assert confirm_b is not None and post_b is not None

        _, raw_baseline_tokens = evaluate_raw_odoo_baseline(harness)
        sc_v11 = evaluate_autonomous_fde_regime(
            regime_name="autonomous_fde_v1.1_baseline",
            blueprint=bp_v11,
            adapter_mapping=mapping_v1,
            harness=harness,
            sandbox_client=sandbox_client,
            raw_baseline_tokens=raw_baseline_tokens,
            raw_total_models=raw_total_models,
            raw_total_fields=raw_total_fields,
            seed_ontology=seed,
        )
        routing_v11 = triage_utility_failures(sc_v11)
        l2_tickets = list(routing_v11.l2_evolution_tickets)

        # Observe ERP phenomena & transient wizard state transitions empirically
        wizard_catalog = discover_transient_wizards(
            sandbox_client,
            [m.model for m in scout_report.discovered_models],
            probe_report=probe_report,
        )
        wizard_effects = _probe_odoo_wizard_effects(sandbox_client, harness)

        # Pre-execution L2 M0 snapshot & replays on live Odoo records
        l2_partner_id = harness.create_partner("L2-EVO-PARTNER", "L2 Evolution Proof Partner")
        f_small = harness.create_draft_order(partner_id=l2_partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="L2-SMALL")
        f_large = harness.create_draft_order(partner_id=l2_partner_id, qty=LARGE_QTY, unit_price=LARGE_UNIT_PRICE, client_ref="L2-LARGE")
        f_deliv = harness.create_draft_order(partner_id=l2_partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="L2-DELIV")
        p_assigned = harness.confirm_order_physically(f_deliv.order_id)

        f_inv_ok = harness.create_draft_order(partner_id=l2_partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="L2-INVOK")
        p_inv_ok = harness.confirm_order_physically(f_inv_ok.order_id)
        harness.ship_order_physically_full(f_inv_ok.order_id)
        inv_draft_id = harness.create_draft_invoice_for_order(f_inv_ok.order_id)

        f_inv_posted = harness.create_draft_order(partner_id=l2_partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="L2-POSTED")
        p_inv_posted = harness.confirm_order_physically(f_inv_posted.order_id)
        harness.ship_order_physically_full(f_inv_posted.order_id)
        inv_posted_id = harness.create_draft_invoice_for_order(f_inv_posted.order_id)
        harness.post_invoice_physically(inv_posted_id)

        f_inv_cn = harness.create_draft_order(partner_id=l2_partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="L2-CN")
        p_inv_cn = harness.confirm_order_physically(f_inv_cn.order_id)
        harness.ship_order_physically_full(f_inv_cn.order_id)
        inv_cn_id = harness.create_draft_invoice_for_order(f_inv_cn.order_id)
        harness.post_invoice_physically(inv_cn_id)

        f_unshipped = harness.create_draft_order(partner_id=l2_partner_id, qty=SMALL_QTY, unit_price=SMALL_UNIT_PRICE, client_ref="L2-UNSHIPPED")
        p_unshipped = harness.confirm_order_physically(f_unshipped.order_id)
        inv_unshipped_id = harness.create_draft_invoice_for_order(f_unshipped.order_id)

        inv_em = mapping_v1.get_entity_mapping(post_b.target_object_type)
        pick_em = next(em for em in mapping_v1.entity_mappings if em.source_table == "stock.picking")
        inv_type = inv_em.target_object_type
        inv_pk = inv_em.id_transform.target_field
        pick_type = pick_em.target_object_type
        pick_pk = pick_em.id_transform.target_field

        evo_m0_rt = OntologyRuntime(compile_blueprint(bp_v11, seed_ontology=seed), db_path=":memory:")
        try:
            evo_m0 = _sync_fixtures_into_autonomous_runtime(
                runtime=evo_m0_rt,
                adapter_mapping=mapping_v1,
                sandbox_client=sandbox_client,
                table_ids={
                    "res.partner": [l2_partner_id],
                    "sale.order": [f_small.order_id, f_large.order_id, f_deliv.order_id, f_inv_ok.order_id, f_inv_posted.order_id, f_inv_cn.order_id, f_unshipped.order_id],
                    "stock.picking": [p_assigned, p_inv_ok, p_inv_posted, p_inv_cn, p_unshipped],
                    "account.move": [inv_draft_id, inv_posted_id, inv_cn_id, inv_unshipped_id],
                },
            )
        finally:
            evo_m0_rt.close()

        pos_events = [
            ReplayEvent(
                name="HIST-POS-CONFIRM-SMALL",
                action=confirm_b.action_name,
                params={confirm_b.id_param: f"{confirm_b.id_prefix}{f_small.order_id}"},
                actor=AI_OPS_ACTOR,
                expected_valid=True,
            ),
            ReplayEvent(
                name="HIST-POS-POST-SHIPPED",
                action=post_b.action_name,
                params={post_b.id_param: f"{post_b.id_prefix}{inv_draft_id}"},
                actor=AI_OPS_ACTOR,
                expected_valid=True,
            ),
        ]
        if validate_b is not None:
            pos_events.append(
                ReplayEvent(
                    name="HIST-POS-VALIDATE-PICKING",
                    action=validate_b.action_name,
                    params={validate_b.id_param: f"{validate_b.id_prefix}{p_assigned}"},
                    actor=AI_OPS_ACTOR,
                    expected_valid=True,
                )
            )
        hist_cxs = [
            IncidentCounterexample(
                name="HIST-CX-OVER-CREDIT",
                action=confirm_b.action_name,
                params={confirm_b.id_param: f"{confirm_b.id_prefix}{f_large.order_id}"},
                actor=AI_OPS_ACTOR,
                expect_blocked_or_approval=True,
            ),
            IncidentCounterexample(
                name="HIST-CX-UNSHIPPED-POST",
                action=post_b.action_name,
                params={post_b.id_param: f"{post_b.id_prefix}{inv_unshipped_id}"},
                actor=AI_OPS_ACTOR,
                expect_valid=False,
                expect_error_code="CANNOT_INVOICE_UNSHIPPED_ORDER",
            ),
        ]

        ext_phenomenon_ctx = {
            "target_system": "cloned_erp_l2_settlement",
            "rule_provenance": {
                "mechanical_legacy_erp_probing": [
                    "Discovered transient wizard schemas & child move lines on cloned ERP.",
                    "Empirically observed partial delivery validation transitions picking state assigned->done.",
                    "Empirically observed customer payment registration decrements account.move.amount_residual and transitions payment_state not_paid -> partial (residual > 0) or paid (residual == 0).",
                    "Empirically observed credit-note reversal sets original account.move payment_state='reversed' and amount_residual=0.0.",
                ],
                "nl_policy_and_friction_requirements": [
                    "Keep existing L1 guardrails & existing fields intact (do NOT re-add fields already present in base_blueprint).",
                    f"Ensure a delivery validation action exists on {pick_type} (if absent from base_blueprint, add validate_delivery_order: assigned -> done, bound to external_table='stock.picking', external_method='button_validate').",
                    f"Add M1 action register_invoice_payment on {inv_type} (id param + float extra param) supporting 50% partial and 100% full settlement: reject draft invoices, amount <= 0, or amount > amount_residual; update amount_residual and use 'if_else' for payment_state ('paid' if residual - amount <= 0 else 'partial'). Bind via account.move external_method='action_create_payments' with extra_params naming the amount parameter.",
                    f"Add M1 action issue_credit_note_refund on {inv_type} restricted to steward/admin (or requiring steward approval when actor is ai_agent): require state=='posted' and payment_state!='reversed'; set payment_state='reversed' and amount_residual=0.0; bind via account.move external_method='refund_moves' (keep any optional reason param required=False).",
                ],
            },
            "observed_transitions": wizard_effects,
            "discovered_wizard_schemas": wizard_catalog,
        }

        def _l2_behavioral_hook(
            candidate_bp: DeclarativeBlueprint,
            candidate_mapping: AdapterMappingSpec | None,
        ) -> list[str]:
            errs: list[str] = []
            if candidate_mapping is None:
                return ["AdapterMappingSpec must be updated for L2 capabilities."]
            c_inv_em = next(
                (e for e in candidate_mapping.entity_mappings if e.target_object_type == inv_type),
                None,
            ) or next((e for e in candidate_mapping.entity_mappings if e.source_table == "account.move"), None)
            if c_inv_em is None:
                return ["Missing EntityMappingSpec for account.move."]
            tf_names = {tf.target_field for tf in c_inv_em.field_transforms}
            for req_f in ("amount_residual", "payment_state"):
                if req_f not in tf_names:
                    errs.append(f"account.move EntityMappingSpec missing field_transform for {req_f!r}.")

            c_val = _find_binding(candidate_mapping, table="stock.picking", method_substr="validate")
            c_pay, c_ref = _find_l2_bindings(candidate_mapping)
            if c_val is None:
                errs.append("Missing ActionExecutionBinding for stock.picking validation.")
            if c_pay is None:
                errs.append(
                    "Missing ActionExecutionBinding on account.move for payment registration "
                    "(e.g. action_name='register_invoice_payment', external_method='action_create_payments')."
                )
            if c_ref is None:
                errs.append(
                    "Missing ActionExecutionBinding on account.move for credit-note refund "
                    "(e.g. action_name='issue_credit_note_refund', external_method='refund_moves')."
                )
            if errs:
                return errs

            assert c_pay is not None and c_ref is not None and c_val is not None
            amt_p = _first_extra_param_name(candidate_bp, c_pay, prefer_numeric=True)
            if not amt_p or amt_p not in c_pay.extra_params:
                errs.append(
                    f"Payment binding {c_pay.action_name!r} MUST declare its numeric amount param {amt_p!r} in extra_params."
                )

            act_map = {a.name: a for a in candidate_bp.actions}
            for req_name in (c_val.action_name, c_pay.action_name, c_ref.action_name):
                if req_name not in act_map:
                    errs.append(f"Blueprint missing action {req_name!r} referenced by ActionExecutionBinding.")
            if errs:
                return errs

            # Black-box behavioral checks in a compiled memory sandbox
            try:
                spec = compile_blueprint(candidate_bp, seed_ontology=seed)
            except Exception as exc:
                return [f"Failed to compile L2 blueprint: {exc}"]

            rt = OntologyRuntime(spec, db_path=":memory:")
            try:
                inv_cls = spec.get_object_type(inv_type)
                pick_cls = spec.get_object_type(pick_type)
                def _pick_kwargs(**kw: Any) -> dict[str, Any]:
                    return {k: v for k, v in kw.items() if k in pick_cls.model_fields}

                def _inv_kwargs(**kw: Any) -> dict[str, Any]:
                    return {k: v for k, v in kw.items() if k in inv_cls.model_fields}

                rt.put_object(
                    pick_cls(
                        **_pick_kwargs(**{
                            pick_pk: f"{pick_em.id_transform.prefix}901",
                            "state": "assigned",
                            "name": "WH/L2/901",
                            "origin": "SO-L2",
                        })
                    )
                )
                rt.put_object(
                    inv_cls(
                        **_inv_kwargs(**{
                            inv_pk: f"{inv_em.id_transform.prefix}901",
                            "state": "posted",
                            "name": "INV/L2/901",
                            "move_type": "out_invoice",
                            "invoice_origin": "SO-L2",
                            "amount_total": 20000.0,
                            "amount_residual": 20000.0,
                            "payment_state": "not_paid",
                        })
                    )
                )
                rt.put_object(
                    inv_cls(
                        **_inv_kwargs(**{
                            inv_pk: f"{inv_em.id_transform.prefix}902",
                            "state": "posted",
                            "name": "INV/L2/902",
                            "move_type": "out_invoice",
                            "invoice_origin": "SO-L2",
                            "amount_total": 20000.0,
                            "amount_residual": 20000.0,
                            "payment_state": "not_paid",
                        })
                    )
                )
                rt.put_object(
                    inv_cls(
                        **_inv_kwargs(**{
                            inv_pk: f"{inv_em.id_transform.prefix}903",
                            "state": "draft",
                            "name": "INV/L2/903",
                            "move_type": "out_invoice",
                            "invoice_origin": "SO-L2",
                            "amount_total": 20000.0,
                            "amount_residual": 20000.0,
                            "payment_state": "not_paid",
                        })
                    )
                )

                def _sim_safe(act_name: str, params: dict[str, Any], actor: Any) -> SimulationResult:
                    try:
                        return rt.simulate_action(act_name, params, actor)
                    except PermissionDeniedError as exc:
                        return SimulationResult(
                            valid=False,
                            diff=ObjectDiff(),
                            requires_approval=False,
                            read_versions={},
                            error=str(exc),
                            error_code="PermissionDeniedError",
                        )

                # 1. Partial payment 50% (10,000 of 20,000)
                s_part = _sim_safe(
                    c_pay.action_name,
                    {c_pay.id_param: f"{c_pay.id_prefix}901", amt_p: 10000.0},
                    AI_OPS_ACTOR,
                )
                if not s_part.valid or s_part.requires_approval:
                    errs.append(
                        f"50% partial payment simulation should commit cleanly as ai_agent, got valid={s_part.valid}, approval={s_part.requires_approval}, err={s_part.error_code}"
                    )
                else:
                    fk = rt.fork()
                    try:
                        fk.submit_action("SIM-PART", c_pay.action_name, {c_pay.id_param: f"{c_pay.id_prefix}901", amt_p: 10000.0}, AI_OPS_ACTOR)
                        o = fk.get_object(inv_cls, f"{c_pay.id_prefix}901", AI_OPS_ACTOR)
                        if abs(float(o.amount_residual) - 10000.0) > 0.01 or o.payment_state != "partial":
                            errs.append(
                                f"50% partial payment must yield amount_residual=10000.0 & payment_state='partial', got residual={o.amount_residual}, payment_state={o.payment_state!r}"
                            )
                    finally:
                        fk.close()

                # 2. Full payment 100% (20,000 of 20,000)
                s_full = _sim_safe(
                    c_pay.action_name,
                    {c_pay.id_param: f"{c_pay.id_prefix}902", amt_p: 20000.0},
                    AI_OPS_ACTOR,
                )
                if not s_full.valid or s_full.requires_approval:
                    errs.append(f"100% full payment must be valid for ai_agent, got err={s_full.error_code}")
                else:
                    fk = rt.fork()
                    try:
                        fk.submit_action("SIM-FULL", c_pay.action_name, {c_pay.id_param: f"{c_pay.id_prefix}902", amt_p: 20000.0}, AI_OPS_ACTOR)
                        o = fk.get_object(inv_cls, f"{c_pay.id_prefix}902", AI_OPS_ACTOR)
                        if abs(float(o.amount_residual) - 0.0) > 0.01 or o.payment_state != "paid":
                            errs.append(
                                f"100% full payment must yield amount_residual=0.0 & payment_state='paid', got residual={o.amount_residual}, payment_state={o.payment_state!r}"
                            )
                    finally:
                        fk.close()

                # 3. Overpayment / negative / draft payment must reject
                for bad_label, inv_num, amt in (
                    ("overpayment", "901", 25000.0),
                    ("non_positive", "901", 0.0),
                    ("draft_invoice", "903", 10000.0),
                ):
                    s_bad = _sim_safe(
                        c_pay.action_name,
                        {c_pay.id_param: f"{c_pay.id_prefix}{inv_num}", amt_p: amt},
                        AI_OPS_ACTOR,
                    )
                    if s_bad.valid:
                        errs.append(f"Payment action MUST reject {bad_label}, but simulate_action returned valid=True.")

                # 4. Delivery validation assigned -> done
                s_val = _sim_safe(
                    c_val.action_name,
                    {c_val.id_param: f"{c_val.id_prefix}901"},
                    AI_OPS_ACTOR,
                )
                if not s_val.valid or s_val.requires_approval:
                    errs.append(
                        f"Delivery validation {c_val.action_name!r} should commit cleanly for assigned picking, got valid={s_val.valid}, err={s_val.error_code}"
                    )
                else:
                    fk = rt.fork()
                    try:
                        fk.submit_action("SIM-VAL", c_val.action_name, {c_val.id_param: f"{c_val.id_prefix}901"}, AI_OPS_ACTOR)
                        po = fk.get_object(pick_cls, f"{c_val.id_prefix}901", AI_OPS_ACTOR)
                        if po.state != "done":
                            errs.append(f"Delivery validation must set picking.state='done', got {po.state!r}")
                    finally:
                        fk.close()

                # 5. Steward credit-note refund vs ai_agent blocked/approval
                s_ref_ai = _sim_safe(
                    c_ref.action_name,
                    {c_ref.id_param: f"{c_ref.id_prefix}901"},
                    AI_OPS_ACTOR,
                )
                if s_ref_ai.valid and not s_ref_ai.requires_approval:
                    errs.append("Credit-note refund MUST NOT commit unapproved for ai_agent.")
                s_ref_st = _sim_safe(
                    c_ref.action_name,
                    {c_ref.id_param: f"{c_ref.id_prefix}901"},
                    STEWARD_OPS_ACTOR,
                )
                if not s_ref_st.valid:
                    errs.append(
                        f"Steward credit-note refund simulation failed: err={s_ref_st.error_code} {s_ref_st.message}"
                    )
                else:
                    fk = rt.fork()
                    try:
                        fk.submit_action("SIM-REF", c_ref.action_name, {c_ref.id_param: f"{c_ref.id_prefix}901"}, STEWARD_OPS_ACTOR)
                        o = fk.get_object(inv_cls, f"{c_ref.id_prefix}901", STEWARD_OPS_ACTOR)
                        if o.payment_state != "reversed" or abs(float(o.amount_residual) - 0.0) > 0.01:
                            errs.append(
                                f"Credit-note refund must set payment_state='reversed' & amount_residual=0.0, got payment_state={o.payment_state!r}, residual={o.amount_residual}"
                            )
                    finally:
                        fk.close()
            finally:
                rt.close()
            return errs

        per_model_results: dict[str, Any] = {}
        saved_bundle_payload: dict[str, Any] | None = None
        for model_spec in models:
            model_name, backend = _make_live_backend(model_spec)
            ok_auth, auth_msg = _probe_backend_auth(backend)
            if not ok_auth:
                per_model_results[model_name] = {"status": "SKIPPED_AUTH_OR_NETWORK", "detail": auth_msg}
                continue

            with InductionGovernanceService(db_path=":memory:") as gov:
                # Seed governance history with v1.0.0 -> v1.1.0 so Gate 4 preserves prior golden cases
                def _v10_fn(_s: str, _u: str, _r: int) -> dict[str, Any]:
                    return {
                        "blueprint": bundle.blueprint.model_dump(mode="json"),
                        "adapter_mapping": mapping_v1.model_dump(mode="json"),
                        "proposed_eval_cases": [],
                    }

                builder = OntologyBuilderAgent(
                    backend=CallableAbductionBackend(_v10_fn),
                    governance=gov,
                    crucible=SymbolicCrucible(),
                    seed_ontology=seed,
                )
                g0 = builder.run_genesis_cycle(
                    model_id="autonomous_o2c_fde",
                    phenomenon_brief={"source_db": source_db},
                    external_profile=profile,
                    crucible_context=CrucibleContext(
                        mode="genesis",
                        replay_factory=build_auto_replay_factory(bundle, seed_ontology=seed),
                        default_actor=AI_OPS_ACTOR,
                        seed_ontology=seed,
                    ),
                    seed_ontology=seed,
                    max_rounds=1,
                )
                assert g0.verified and g0.pending_approval_run_id is not None
                gov.approve_crystallization(g0.pending_approval_run_id, STEWARD_ACTOR)

                def _v11_fn(_s: str, _u: str, _r: int) -> dict[str, Any]:
                    return {"patch": l1_patch.model_dump(mode="json")}

                evolver_l1 = OntologyEvolverAgent(
                    backend=CallableAbductionBackend(_v11_fn),
                    governance=gov,
                    crucible=SymbolicCrucible(),
                    seed_ontology=seed,
                )
                g1 = evolver_l1.run_evolution_cycle(
                    model_id="autonomous_o2c_fde",
                    cluster=FrictionClusterSummary(
                        cluster_id="L1-BASELINE-SYN",
                        signal_kind="INVARIANT_GUARDRAIL_GAP",
                        action_type=confirm_b.action_name,
                        signature="over_credit+unshipped_post",
                        support_count=2,
                        run_ids=("L1-1", "L1-2"),
                        escalated=True,
                        evidences=(),
                    ),
                    crucible_context=CrucibleContext(
                        mode="evolution",
                        m0_objects=evo_m0,
                        positive_events=[pos_events[0], pos_events[1]],
                        incident_counterexamples=hist_cxs,
                        default_actor=AI_OPS_ACTOR,
                        seed_ontology=seed,
                    ),
                    seed_ontology=seed,
                    max_rounds=1,
                )
                assert g1.verified and g1.pending_approval_run_id is not None, (
                    f"L1 baseline seed failed: {g1.final_report.diagnostic_feedback()}"
                )
                gov.approve_crystallization(g1.pending_approval_run_id, STEWARD_ACTOR)

                _log(f"ExpC (L2): starting live L2 Architect cycle for {model_name}")
                t0 = time.monotonic()
                architect = OntologyL2ArchitectAgent(
                    backend=backend,
                    governance=gov,
                    crucible=SymbolicCrucible(),
                    seed_ontology=seed,
                )
                l2_cycle = architect.run_l2_evolution_cycle(
                    model_id="autonomous_o2c_fde",
                    l2_tickets=l2_tickets,
                    external_phenomenon_context=ext_phenomenon_ctx,
                    crucible_context=CrucibleContext(
                        mode="evolution",
                        m0_objects=evo_m0,
                        positive_events=pos_events,
                        incident_counterexamples=hist_cxs,
                        default_actor=AI_OPS_ACTOR,
                        seed_ontology=seed,
                    ),
                    base_mapping=mapping_v1,
                    seed_ontology=seed,
                    max_rounds=max_rounds,
                    l2_validation_hook=_l2_behavioral_hook,
                )
                elapsed_sec = round(time.monotonic() - t0, 2)
                _log(
                    f"ExpC (L2): finished for {model_name} verified={l2_cycle.verified} "
                    f"rounds={len(l2_cycle.rounds)} elapsed={elapsed_sec}s"
                )
                entry: dict[str, Any] = {
                    "status": "CRYSTALLIZED" if l2_cycle.verified else "REFUTED_AFTER_MAX_ROUNDS",
                    "elapsed_sec": elapsed_sec,
                    "rounds_used": len(l2_cycle.rounds),
                    "round_traces": [
                        {
                            "round": tr.round_index,
                            "passed": tr.passed,
                            "failed_gates": list(tr.failed_gates),
                            "feedback_preview": tr.feedback[:650],
                        }
                        for tr in l2_cycle.rounds
                    ],
                }
                if getattr(backend, "last_usage", None):
                    entry["last_usage"] = backend.last_usage

                if l2_cycle.verified and l2_cycle.pending_approval_run_id is not None:
                    gov.approve_crystallization(l2_cycle.pending_approval_run_id, STEWARD_ACTOR)
                    bp_v2 = gov.get_active_blueprint("autonomous_o2c_fde")
                    map_v2 = gov.get_active_adapter_mapping("autonomous_o2c_fde")
                    assert bp_v2 is not None and map_v2 is not None

                    _log(f"ExpC (L2): evaluating {model_name} v2.0.0 on live cloned Odoo 19")
                    sc_v2 = evaluate_autonomous_fde_regime(
                        regime_name=f"llm_{model_name}_v2.0",
                        blueprint=bp_v2,
                        adapter_mapping=map_v2,
                        harness=harness,
                        sandbox_client=sandbox_client,
                        raw_baseline_tokens=raw_baseline_tokens,
                        raw_total_models=raw_total_models,
                        raw_total_fields=raw_total_fields,
                        seed_ontology=seed,
                    )
                    entry["proposed_patch"] = (
                        l2_cycle.patch.model_dump(mode="json") if l2_cycle.patch else None
                    )
                    entry["v2_live_odoo_evaluation"] = {
                        "anti_smuggling_passed": sc_v2.anti_smuggling.passed,
                        "anti_smuggling_violations": [v.__dict__ for v in sc_v2.anti_smuggling.violations],
                        "strict_improvement_over_v1_1": is_strict_utility_improvement(sc_v11, sc_v2),
                        "metrics": sc_v2.to_dict()["metrics"],
                        "probes": [
                            {
                                "probe_id": p.probe_id,
                                "bucket": p.bucket,
                                "passed": p.passed,
                                "observed": p.observed_outcome,
                            }
                            for p in sc_v2.probes
                        ],
                    }
                    if saved_bundle_payload is None:
                        saved_bundle_payload = {
                            "model_id": "autonomous_o2c_fde",
                            "source_db": source_db,
                            "induced_by_model": model_name,
                            "version": bp_v2.version,
                            "rule_provenance": ext_phenomenon_ctx["rule_provenance"],
                            "blueprint": bp_v2.model_dump(mode="json"),
                            "adapter_mapping": map_v2.model_dump(mode="json"),
                        }

                per_model_results[model_name] = entry

    if save_v2_bundle_path and saved_bundle_payload is not None:
        p = Path(save_v2_bundle_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(saved_bundle_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "source_db": source_db,
        "v1_1_baseline": {
            "metrics": sc_v11.to_dict()["metrics"],
            "l2_tickets": [t.to_dict() for t in l2_tickets],
        },
        "models": per_model_results,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Live LLM (Gemini / Antigravity / OpenRouter) Symbolic Gradient & Genesis/L2 Evolution on Cloned Odoo 19"
    )
    parser.add_argument(
        "--models",
        default="agy/gemini-4-neon-medium",
        help="Comma-separated model list",
    )
    parser.add_argument(
        "--mode",
        choices=["l1", "genesis", "both", "l2"],
        default="l1",
        help="Experiment mode: l1 (Exp A), genesis (Exp B), both, or l2 (Exp C)",
    )
    parser.add_argument("--db", default=DB_NORTH)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional path to incrementally checkpoint experiment JSON results",
    )
    parser.add_argument(
        "--save-v2-bundle",
        default=None,
        help="Optional path to export crystallized L2 v2.0.0 blueprint+adapter_mapping bundle",
    )
    args = parser.parse_args(argv)

    def _checkpoint(payload: dict[str, Any]) -> None:
        if not args.output_json:
            return
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.mode == "l1":
        out = run_live_llm_l1_gradient_experiment(
            models=models, source_db=args.db, max_rounds=args.max_rounds
        )
        _checkpoint(out)
    elif args.mode == "genesis":
        out = run_live_llm_genesis_purification_experiment(
            models=models, source_db=args.db, max_rounds=args.max_rounds
        )
        _checkpoint(out)
    elif args.mode == "l2":
        out = run_live_llm_l2_architect_experiment(
            models=models,
            source_db=args.db,
            max_rounds=args.max_rounds,
            save_v2_bundle_path=args.save_v2_bundle,
        )
        _checkpoint(out)
    else:
        exp_a = run_live_llm_l1_gradient_experiment(
            models=models, source_db=args.db, max_rounds=args.max_rounds
        )
        out = {"experiment_a_l1_gradient": exp_a}
        _checkpoint(out)
        exp_b = run_live_llm_genesis_purification_experiment(
            models=models, source_db=args.db, max_rounds=args.max_rounds
        )
        out["experiment_b_genesis_purification"] = exp_b
        _checkpoint(out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("ODOO_DBS_AFTER:", _jsonrpc("db", "list", []))


if __name__ == "__main__":
    main()
