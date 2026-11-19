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
from open_ontology.induction import (
    AbductionBackend,
    ActionExecutionBinding,
    AntigravityCLIAbductionBackend,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    EvolutionPatch,
    EvolutionRoutingPlan,
    GeminiAbductionBackend,
    IncidentCounterexample,
    InductionGovernanceService,
    OntologyBuilderAgent,
    OntologyEvolverAgent,
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
from .odoo_fde_prober import EphemeralOdooSandbox, OdooActiveProber
from .odoo_fde_scout import OdooFDEScout, ReadOnlyOdooClient
from .odoo_utility_benchmark import (
    LARGE_QTY,
    LARGE_UNIT_PRICE,
    SMALL_QTY,
    SMALL_UNIT_PRICE,
    PhysicalOdooHarness,
    _find_binding,
    _sync_fixtures_into_autonomous_runtime,
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
                    replay_factory=build_auto_replay_factory(bundle),
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
                        replay_factory=build_auto_replay_factory(bundle),
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
            "non-empty prefix (e.g. 'CUST-'), and any many2one_id FieldTransformSpec pointing to that entity MUST "
            "use the exact same prefix (keep optional relations required=False).\n"
            "   - If an ObjectTypeBlueprint uses archetype='ProcessDocument', note status_field defaults to 'status': "
            "either name your status field 'status' (mapped from raw 'state') or set status_field explicitly to match "
            "your field name, and declare initial_statuses + transitions covering all valid edges.\n"
            "   - Bind every discovered forward causal TRANSITION method (and any cancellation/reversal action you "
            "expose) in action_bindings (external_table, external_method, target_object_type, id_param, id_prefix, "
            "expected_state_field, expected_state_after), with id_param matching the action's params[0].name and "
            "id_prefix matching the target entity's id_transform.prefix; keep action mutations, preconditions, "
            "transitions, and expected_state_after in the exact mapped status vocabulary.\n"
            "4. Gate backward/reversal/cancellation transitions behind steward/admin approval (e.g. approval_rule "
            "when actor.role not_in ['steward', 'admin']) AND enforce discovered cross-entity limit and "
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Live LLM (Gemini / Antigravity / OpenRouter) L1 Symbolic Gradient & Genesis Purification on Cloned Odoo 19"
    )
    parser.add_argument(
        "--models",
        default="agy/gemini-4-neon-medium",
        help="Comma-separated model list",
    )
    parser.add_argument(
        "--mode",
        choices=["l1", "genesis", "both"],
        default="l1",
        help="Experiment mode: l1 (Exp A), genesis (Exp B), or both",
    )
    parser.add_argument("--db", default=DB_NORTH)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional path to incrementally checkpoint experiment JSON results",
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
