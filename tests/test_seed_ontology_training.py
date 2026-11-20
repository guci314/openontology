"""验收测试：可插拔领域种子本体论（SeedOntology）作为符号训练器第一等输入。

覆盖：
1. M2 纯洁性守卫：``open_ontology/induction/seed.py`` 零 Odoo / 零禁用 IT 词汇；
2. 企业种子底座（``BUSINESS_SEED_ONTOLOGY``）全四原型真实继承 + 账证守恒公理：
   - 声明式蓝图真正继承 ``MasterEntity / BalanceLedger / ProcessDocument / EventVoucher``；
   - 第 1 轮修改 ``BalanceLedger`` 却漏写 ``EventVoucher`` → 被继承的 ``business_ontology``
     公理当场在关 3 判 ``UNBACKED_BALANCE_MUTATION`` 驳回；
   - 第 2 轮同事务配平凭证 → 五关全绿结晶，且运行时真继承 ``ILLEGAL_DOCUMENT_TRANSITION`` /
     ``NEGATIVE_BALANCE`` / ``append_only``；
3. 跨领域切换底座（非 ERP 的 ``ClinicalSeedOntology``）：
   - 验证传入 ``ClinicalSeedOntology`` 后，ERP 原型 ``BalanceLedger`` 直接在关 1 报
     ``UNKNOWN_SEED_ARCHETYPE``，非法角色报 ``UnknownRoleError``（严格限定假设空间）；
   - 验证自定义临床剂量安全公理自动注入熔炉并在 CEGIS 第 1→2 轮完成证伪收敛；
4. 自主 FDE 管道（``run_autonomous_fde_end_to_end``）显式接入 ``BUSINESS_SEED_ONTOLOGY``，
   验证产出对象真正继承上位抽象类。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from examples.business_ontology import (
    BUSINESS_SEED_ONTOLOGY,
    BalanceLedger,
    EventVoucher,
    MasterEntity,
    ProcessDocument,
    spec as business_spec,
)
from examples.odoo_fde_autonomous_pipeline import run_autonomous_fde_end_to_end
from open_ontology import Actor, InvariantError, ObjectType, OntologyRuntime, OntologySpec
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CallableAbductionBackend,
    CrucibleContext,
    DeclarativeBlueprint,
    FieldSpec,
    InductionGovernanceService,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    OntologyBuilderAgent,
    RelationSpec,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    compile_blueprint,
    seed_from_spec,
)
from tests.test_odoo_fde_prober import _SyntheticProbeSandbox
from tests.test_odoo_fde_scout import _SyntheticOdooMock


def test_seed_module_m2_purity() -> None:
    path = Path(__file__).resolve().parents[1] / "open_ontology" / "induction" / "seed.py"
    text = path.read_text(encoding="utf-8").lower()
    for forbidden in ("odoo", "res.partner", "sale.order", "stock.picking", "account.move"):
        assert forbidden not in text, f"Odoo 词条 {forbidden!r} 不得泄漏进 M2 seed.py"
    for banned_it in ("outbox", "writeback", "target_system", "retry_count", "last_error", "lineage"):
        assert banned_it not in text, f"禁用 IT 词条 {banned_it!r} 不得泄漏进 M2 seed.py"


def test_business_seed_ontology_full_4_archetypes_and_balance_conservation_in_crucible() -> None:
    """BUSINESS_SEED_ONTOLOGY genuinely enforces all 4 archetypes + BalanceLedger conservation in CEGIS."""
    assert BUSINESS_SEED_ONTOLOGY.seed_id == "business_ontology"
    assert set(BUSINESS_SEED_ONTOLOGY.archetype_names()) == {
        "ObjectType",
        "MasterEntity",
        "BalanceLedger",
        "ProcessDocument",
        "EventVoucher",
    }
    assert "voucher_backed_balance_conservation" in BUSINESS_SEED_ONTOLOGY.inherited_transition_invariant_names()

    def _make_bp(*, emit_voucher: bool) -> dict[str, Any]:
        mutations = [
            ActionMutationSpec(
                op="modify",
                object_type="OrderDoc",
                id_param="order_id",
                set_fields={"status": {"lit": "invoiced"}},
            ),
            ActionMutationSpec(
                op="modify",
                object_type="ArLedger",
                id_param="ledger_id",
                set_fields={
                    "balance_cents": {
                        "op": "add",
                        "left": {"ref": "obj.balance_cents"},
                        "right": {"ref": "param.amount_cents"},
                    }
                },
            ),
        ]
        if emit_voucher:
            mutations.append(
                ActionMutationSpec(
                    op="create",
                    object_type="ArVoucher",
                    id_param="voucher_id",
                    set_fields={
                        "voucher_id": {"ref": "param.voucher_id"},
                        "ledger": {"ref": "param.ledger_id"},
                        "delta_cents": {"ref": "param.amount_cents"},
                    },
                )
            )
        bp = DeclarativeBlueprint(
            model_id="seeded_ar_model",
            version="1.0.0",
            seed_ontology_id="business_ontology",
            object_types=[
                ObjectTypeBlueprint(
                    name="ClientMaster",
                    archetype="MasterEntity",
                    primary_key="client_id",
                    fields=[
                        FieldSpec(name="client_id", field_type="str", primary_key=True),
                        FieldSpec(name="credit_limit_cents", field_type="int", default=0, non_negative=True),
                    ],
                ),
                ObjectTypeBlueprint(
                    name="ArLedger",
                    archetype="BalanceLedger",
                    primary_key="ledger_id",
                    fields=[
                        FieldSpec(name="ledger_id", field_type="str", primary_key=True),
                        FieldSpec(name="balance_cents", field_type="int", default=0, non_negative=True),
                    ],
                    relations=[RelationSpec(field="client", target_type="ClientMaster", required=True)],
                    voucher_delta_rules={"balance_cents": ("ArVoucher", "ledger", "delta_cents")},
                ),
                ObjectTypeBlueprint(
                    name="OrderDoc",
                    archetype="ProcessDocument",
                    primary_key="order_id",
                    fields=[
                        FieldSpec(name="order_id", field_type="str", primary_key=True),
                        FieldSpec(name="status", field_type="str", default="shipped"),
                    ],
                    relations=[RelationSpec(field="client", target_type="ClientMaster", required=True)],
                    status_field="status",
                    initial_statuses=["draft", "shipped"],
                    transitions={"draft": ["shipped"], "shipped": ["invoiced"]},
                ),
                ObjectTypeBlueprint(
                    name="ArVoucher",
                    archetype="EventVoucher",
                    primary_key="voucher_id",
                    fields=[
                        FieldSpec(name="voucher_id", field_type="str", primary_key=True),
                        FieldSpec(name="delta_cents", field_type="int", default=0),
                    ],
                    relations=[RelationSpec(field="ledger", target_type="ArLedger", required=True)],
                ),
            ],
            actions=[
                ActionBlueprint(
                    name="post_invoice",
                    allowed_roles=["finance", "ai_agent"],
                    revert_roles=["finance"],
                    params=[
                        FieldSpec(name="order_id", field_type="str", required=True),
                        FieldSpec(name="ledger_id", field_type="str", required=True),
                        FieldSpec(name="voucher_id", field_type="str", required=True),
                        FieldSpec(name="amount_cents", field_type="int", required=True),
                    ],
                    mutations=mutations,
                )
            ],
        )
        return {"rationale": f"emit_voucher={emit_voucher}", "blueprint": bp.model_dump(mode="json")}

    seen_prompts: list[tuple[str, str]] = []

    def _abduce(sys_p: str, user_p: str, round_idx: int) -> dict[str, Any]:
        seen_prompts.append((sys_p, user_p))
        # Round 1 forgets EventVoucher -> caught by inherited BalanceLedger axiom!
        # Round 2 emits balanced EventVoucher -> passes all 5 gates!
        return _make_bp(emit_voucher=(round_idx >= 2))

    finance = Actor(actor_id="fin-1", role="finance", clearance_tags=frozenset({"*"}))
    m0 = [
        {"object_type": "ClientMaster", "data": {"client_id": "C-1", "credit_limit_cents": 500_000}},
        {"object_type": "ArLedger", "data": {"ledger_id": "L-1", "client": "C-1", "balance_cents": 0}},
        {"object_type": "OrderDoc", "data": {"order_id": "O-1", "client": "C-1", "status": "shipped"}},
    ]

    with InductionGovernanceService(db_path=":memory:") as gov:
        builder = OntologyBuilderAgent(
            backend=CallableAbductionBackend(_abduce),
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        cycle = builder.run_genesis_cycle(
            model_id="seeded_ar_model",
            crucible_context=CrucibleContext(
                mode="genesis",
                m0_objects=m0,
                positive_events=[
                    ReplayEvent(
                        name="post_invoice_balanced",
                        action="post_invoice",
                        params={
                            "order_id": "O-1",
                            "ledger_id": "L-1",
                            "voucher_id": "V-1",
                            "amount_cents": 100_000,
                        },
                        actor=finance,
                        expected_valid=True,
                    )
                ],
                default_actor=finance,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            ),
            max_rounds=3,
        )
        # Round 1 refuted by inherited voucher_backed_balance_conservation (UNBACKED_BALANCE_MUTATION)!
        assert len(cycle.rounds) == 2
        assert cycle.rounds[0].passed is False
        assert "gate_3_counterexample_elimination" in cycle.rounds[0].failed_gates
        assert "UNBACKED_BALANCE_MUTATION" in cycle.rounds[0].feedback
        assert cycle.rounds[1].passed is True
        assert cycle.verified is True
        assert cycle.pending_approval_run_id is not None

        # Verify SeedOntology prompt injection occurred
        assert "business_ontology" in seen_prompts[0][0]
        assert "voucher_backed_balance_conservation" in seen_prompts[0][1]

        gov.approve_crystallization(cycle.pending_approval_run_id, STEWARD_ACTOR)
        active_bp = gov.get_active_blueprint("seeded_ar_model")
        assert active_bp is not None and active_bp.seed_ontology_id == "business_ontology"

        compiled = compile_blueprint(active_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
        assert business_spec in compiled.parents
        assert issubclass(compiled.get_object_type("ClientMaster"), MasterEntity)
        assert issubclass(compiled.get_object_type("ArLedger"), BalanceLedger)
        assert issubclass(compiled.get_object_type("OrderDoc"), ProcessDocument)
        assert issubclass(compiled.get_object_type("ArVoucher"), EventVoucher)
        assert compiled.get_object_type("ArVoucher").is_append_only() is True

        # Verify live runtime executes balanced posting cleanly and blocks negative balance via inherited axiom
        rt = OntologyRuntime(compiled, db_path=":memory:")
        try:
            for row in m0:
                cls = compiled.get_object_type(row["object_type"])
                rt.put_object(cls(**row["data"]))
            run = rt.submit_action(
                "RUN-1",
                "post_invoice",
                {"order_id": "O-1", "ledger_id": "L-1", "voucher_id": "V-1", "amount_cents": 100_000},
                finance,
            )
            assert run["status"] == "COMMITTED"
            assert rt.get_object(compiled.get_object_type("ArLedger"), "L-1", finance).balance_cents == 100_000
            with pytest.raises(InvariantError) as neg_err:
                rt.submit_action(
                    "RUN-NEG",
                    "post_invoice",
                    {"order_id": "O-1", "ledger_id": "L-1", "voucher_id": "V-2", "amount_cents": -200_000},
                    finance,
                )
            assert neg_err.value.code in ("NEGATIVE_BALANCE", "ILLEGAL_DOCUMENT_TRANSITION")
        finally:
            rt.close()


def test_domain_swap_clinical_seed_ontology_enforces_custom_axioms_and_bounds_search_space() -> None:
    """Swapping in a non-ERP ClinicalSeedOntology bounds archetypes, roles, and injects custom clinical axioms."""
    clinical_spec = OntologySpec()
    clinical_spec.role("attending_physician", "Attending physician")
    clinical_spec.role("nurse", "Registered nurse")
    clinical_spec.role("ai_agent", "Clinical copilot")

    class PatientSubject(ObjectType):
        """Clinical patient subject archetype."""

        __abstract__: ClassVar[bool] = True
        __aggregate_root__: ClassVar[bool] = True

    class MedicationOrder(ObjectType):
        """Clinical medication order archetype."""

        __abstract__: ClassVar[bool] = True
        __status_field__: ClassVar[str] = "status"
        __transitions__: ClassVar[dict[str, tuple[str, ...]]] = {}

    clinical_spec.object_type(PatientSubject)
    clinical_spec.object_type(MedicationOrder)

    @clinical_spec.invariant(
        "clinical_safe_dosage_bound",
        code="DOSAGE_EXCEEDS_CLINICAL_LIMIT",
        description="MedicationOrder.dosage_mg must never exceed max_safe_mg.",
    )
    def _check_safe_dosage(view: Any) -> bool:
        for cls in view.spec.subtypes_of(MedicationOrder):
            for order in view.query(cls):
                dosage = getattr(order, "dosage_mg", 0)
                limit = getattr(order, "max_safe_mg", 0)
                if limit > 0 and dosage > limit:
                    raise InvariantError(
                        f"dosage {dosage}mg exceeds safe limit {limit}mg",
                        code="DOSAGE_EXCEEDS_CLINICAL_LIMIT",
                    )
        return True

    clinical_seed = seed_from_spec(
        seed_id="clinical_seed_v1",
        domain_name="Clinical Healthcare",
        base_spec=clinical_spec,
        description="Healthcare seed ontology with PatientSubject, MedicationOrder, and dosage safety axiom.",
    )

    # 1. Search space bounding: ERP archetype 'BalanceLedger' MUST be rejected under ClinicalSeedOntology!
    erp_intruder_bp = DeclarativeBlueprint(
        model_id="wrong_domain",
        seed_ontology_id="clinical_seed_v1",
        object_types=[
            ObjectTypeBlueprint(
                name="FakeLedger",
                archetype="BalanceLedger",
                primary_key="id",
                fields=[FieldSpec(name="id", field_type="str", primary_key=True)],
            )
        ],
    )
    with pytest.raises(InvariantError) as arch_err:
        compile_blueprint(erp_intruder_bp, seed_ontology=clinical_seed)
    assert arch_err.value.code == "UNKNOWN_SEED_ARCHETYPE"

    # 2. CEGIS training under ClinicalSeedOntology:
    #    Round 1 uses undeclared role 'sales_rep' -> rejected in Gate 1!
    #    Round 2 uses valid clinical roles & inherits clinical_safe_dosage_bound -> passes all 5 gates!
    def _clinical_bp(*, use_valid_roles: bool) -> dict[str, Any]:
        bp = DeclarativeBlueprint(
            model_id="ward_prescription_model",
            version="1.0.0",
            seed_ontology_id="clinical_seed_v1",
            object_types=[
                ObjectTypeBlueprint(
                    name="WardPatient",
                    archetype="PatientSubject",
                    primary_key="patient_id",
                    fields=[
                        FieldSpec(name="patient_id", field_type="str", primary_key=True),
                        FieldSpec(name="name", field_type="str", default=""),
                    ],
                ),
                ObjectTypeBlueprint(
                    name="PrescriptionOrder",
                    archetype="MedicationOrder",
                    primary_key="rx_id",
                    fields=[
                        FieldSpec(name="rx_id", field_type="str", primary_key=True),
                        FieldSpec(name="dosage_mg", field_type="int", default=0, non_negative=True),
                        FieldSpec(name="max_safe_mg", field_type="int", default=200, non_negative=True),
                        FieldSpec(name="status", field_type="str", default="draft"),
                    ],
                    relations=[RelationSpec(field="patient", target_type="WardPatient", required=True)],
                    status_field="status",
                    transitions={"draft": ["prescribed"]},
                ),
            ],
            actions=[
                ActionBlueprint(
                    name="prescribe_dose",
                    allowed_roles=["attending_physician", "ai_agent"] if use_valid_roles else ["sales_rep"],
                    revert_roles=["attending_physician"] if use_valid_roles else ["sales_rep"],
                    params=[
                        FieldSpec(name="rx_id", field_type="str", required=True),
                        FieldSpec(name="dosage_mg", field_type="int", required=True),
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="PrescriptionOrder",
                            id_param="rx_id",
                            set_fields={"dosage_mg": {"ref": "param.dosage_mg"}, "status": {"lit": "prescribed"}},
                        )
                    ],
                )
            ],
        )
        return {"rationale": f"valid_roles={use_valid_roles}", "blueprint": bp.model_dump(mode="json")}

    doc_actor = Actor(actor_id="dr-1", role="attending_physician", clearance_tags=frozenset({"*"}))
    m0 = [
        {"object_type": "WardPatient", "data": {"patient_id": "P-1", "name": "Alice"}},
        {
            "object_type": "PrescriptionOrder",
            "data": {"rx_id": "RX-1", "patient": "P-1", "dosage_mg": 0, "max_safe_mg": 200, "status": "draft"},
        },
    ]

    with InductionGovernanceService(db_path=":memory:") as gov:
        builder = OntologyBuilderAgent(
            backend=CallableAbductionBackend(lambda _s, _u, r: _clinical_bp(use_valid_roles=(r >= 2))),
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=clinical_seed,
        )
        cycle = builder.run_genesis_cycle(
            model_id="ward_prescription_model",
            crucible_context=CrucibleContext(
                mode="genesis",
                m0_objects=m0,
                positive_events=[
                    ReplayEvent(
                        name="safe_dosage_100mg",
                        action="prescribe_dose",
                        params={"rx_id": "RX-1", "dosage_mg": 100},
                        actor=doc_actor,
                        expected_valid=True,
                    )
                ],
                negative_events=[
                    ReplayEvent(
                        name="overdose_500mg_blocked_by_seed_axiom",
                        action="prescribe_dose",
                        params={"rx_id": "RX-1", "dosage_mg": 500},
                        actor=doc_actor,
                        expected_valid=False,
                        expected_error_code="DOSAGE_EXCEEDS_CLINICAL_LIMIT",
                    )
                ],
                default_actor=doc_actor,
                seed_ontology=clinical_seed,
            ),
            max_rounds=3,
        )
        assert len(cycle.rounds) == 2
        assert cycle.rounds[0].passed is False
        assert "gate_1_static_safety" in cycle.rounds[0].failed_gates
        assert "UnknownRoleError" in cycle.rounds[0].feedback
        assert cycle.rounds[1].passed is True
        assert cycle.verified is True

        compiled = compile_blueprint(cycle.candidate_blueprint, seed_ontology=clinical_seed)  # type: ignore[arg-type]
        assert clinical_spec in compiled.parents
        assert issubclass(compiled.get_object_type("WardPatient"), PatientSubject)
        assert issubclass(compiled.get_object_type("PrescriptionOrder"), MedicationOrder)


def test_autonomous_fde_pipeline_grounds_on_business_seed_ontology() -> None:
    """Autonomous FDE pipeline explicitly inherits BUSINESS_SEED_ONTOLOGY abstract superclasses."""
    base_ro = _SyntheticOdooMock()
    base_ro.tables["clinic.visit"].append(
        {"id": 16, "name": "VST-005", "patient_id": [2, "Bob"], "state": "draft", "total_amount": 150.0}
    )
    sandbox = _SyntheticProbeSandbox(base_ro)
    out = run_autonomous_fde_end_to_end(
        ro_client=base_ro,
        sandbox_client=sandbox,
        source_locator="mock://clinic_seeded",
        sandbox_db="mock_clone_clinic_seeded",
        seed_ontology=BUSINESS_SEED_ONTOLOGY,
        model_id="seeded_autonomous_clinic",
        max_models=5,
        use_live_llm=False,
    )
    cryst = out["crystallized_ontology"]
    assert cryst["crucible_gates_passed"] is True
    assert cryst["seed_ontology_id"] == "business_ontology"
    assert set(cryst["inherited_abstract_types"]) == {
        "BalanceLedger",
        "EventVoucher",
        "MasterEntity",
        "ProcessDocument",
    }
    assert "ProcessDocument" in cryst["object_superclasses"]["ClinicVisit"]
    assert "ProcessDocument" in cryst["object_superclasses"]["PatientBill"]
    assert "ProcessDocument" in cryst["object_superclasses"]["InternalAdj"]
    assert "MasterEntity" in cryst["object_superclasses"]["Patient"]
