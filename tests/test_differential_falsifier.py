"""Phase C (WO-08) 最终验收测试：覆盖率驱动波普尔差分证伪器 + 四大埋雷自愈红测 + 双实库验真。

覆盖：
1. 红测 1（埋雷 A · 枚举双向映射错位自愈）：错误 ``enum_map`` 逃过静态 5 关，被 ``DifferentialFalsifier``
   物理差分 100% 捕获 ``DIFFERENTIAL_STATE_MISMATCH`` 判 ``FALSIFIED``，回流 ``decompile_and_falsify_driver``
   自愈后达 ``CORROBORATED``；
2. 红测 2（埋雷 B · 外键/动作前缀错位自愈）：错位前缀导致静默 0 行写入或回读不匹配，100% 判 ``FALSIFIED``，
   自愈修复后达 ``CORROBORATED``；
3. 红测 3（埋雷 C · 漏掉客户风控 Overlay 自愈）：漏掉高优工单阻断 / 超 100k ``APPROVAL_REQUIRED_HIGH_VALUE_LOSS``
   覆层，红队对抗轨 100% 捕获 ``ADVERSARIAL_ESCAPE`` 判 ``FALSIFIED``，补全 ``CustomerOverlaySpec`` 后 ``CORROBORATED``；
4. 红测 4（反伪安全红测 · 覆盖率不足硬拒）：只跑 1 条简单正常路径重复 10 次（0 报错、0 差分），仍因
   ``INSUFFICIENT_COVERAGE`` 硬判 ``FALSIFIED``；
5. 双实库端到端验收：
   - **SQLite 双外设**：真实 ``BottleCRM`` 临时副本与 Device B 高噪异构 SQLite 均达 ``CORROBORATED``
     （源库 ``vendor_crm/bottlecrm.sqlite3`` SHA-256 零修改）；
   - **Live Odoo 19 克隆库**：基于 ``EphemeralOdooSandbox(source_db='erp_north')`` 挂载最小标准 ``std_o2c`` 驱动，
     在真实 Odoo 19 克隆沙箱上跑通 ``DifferentialFalsifier`` 达 ``CORROBORATED``，且结束时 Odoo DB
     严格清零为 ``['erp_north', 'erp_south', 'odoo_demo']``。
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.dual_device_driver_swap_benchmark import (
    BOTTLECRM_SQLITE_PATH,
    CRM_SALES_ACTOR,
    GenericSQLiteDriverBridge,
    build_crm_decompiler_replay_factory,
    build_device_b_heterogeneous_sqlite,
    build_locked_crm_target_blueprint,
    build_offline_device_a_mapping,
    build_offline_device_b_mapping,
    build_standard_crm_customer_overlay,
    file_sha256,
    scout_sqlite_device,
)
from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError, _jsonrpc
from examples.odoo_fde_prober import EphemeralOdooSandbox
from examples.odoo_utility_benchmark import PhysicalOdooHarness
from examples.std_crm_ontology import build_std_crm_blueprint
from examples.std_o2c_ontology import build_std_o2c_blueprint
from open_ontology import Actor, OntologySpec, SourceBridge
from open_ontology.induction import (
    ActionExecutionBinding,
    AdapterMappingSpec,
    AdversarialProbe,
    CallableAbductionBackend,
    CrucibleContext,
    DifferentialFalsifier,
    DriverDecompilerAgent,
    EntityMappingSpec,
    FalsificationCase,
    FieldTransformSpec,
    InductionGovernanceService,
    ProposedEvalCase,
    SymbolicCrucible,
    decompile_and_falsify_driver,
    project_external_phenomenon,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Generic SQLite Sandbox Adapter implementing M2 ExternalSandboxAdapter
# ---------------------------------------------------------------------------
class SQLiteSandboxAdapter:
    """Isolated ephemeral SQLite sandbox supporting clean resets & write counting."""

    def __init__(
        self,
        *,
        baseline_db: Path,
        work_db: Path,
        adapter_mapping: AdapterMappingSpec | None = None,
        source_kind: str = "sqlite_sandbox",
    ) -> None:
        self.baseline_db = baseline_db
        self.work_db = work_db
        self._mapping = adapter_mapping
        self.source_kind = source_kind
        self._write_count = 0
        self._open_bridges: list[GenericSQLiteDriverBridge] = []

    def _close_bridges(self) -> None:
        while self._open_bridges:
            bridge = self._open_bridges.pop()
            try:
                if bridge.conn.in_transaction:
                    bridge.conn.rollback()
            except Exception:
                pass
            try:
                bridge.close()
            except Exception:
                pass

    def set_adapter_mapping(self, adapter_mapping: AdapterMappingSpec) -> None:
        self._mapping = adapter_mapping

    def reset(self) -> None:
        self._close_bridges()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{self.work_db}{suffix}").unlink(missing_ok=True)
        shutil.copy2(self.baseline_db, self.work_db)
        self._profile = scout_sqlite_device(self.work_db, source_kind=self.source_kind)["profile"]

    def snapshot_m0(self) -> list[dict[str, Any]]:
        assert self._mapping is not None
        profile = scout_sqlite_device(self.work_db, source_kind=self.source_kind)["profile"]
        projected, _ = project_external_phenomenon(
            profile,
            self._mapping,
            require_non_empty_per_entity=False,
        )
        return projected

    def make_source_bridge(
        self,
        spec: OntologySpec,
        adapter_mapping: AdapterMappingSpec,
    ) -> SourceBridge:
        self._mapping = adapter_mapping
        self._close_bridges()
        profile = scout_sqlite_device(self.work_db, source_kind=self.source_kind)["profile"]
        inner = GenericSQLiteDriverBridge(
            self.work_db,
            spec=spec,
            profile=profile,
            adapter_mapping=adapter_mapping,
        )
        self._open_bridges.append(inner)

        def _actuate(mv: dict[str, Any]) -> None:
            before_changes = inner.conn.total_changes
            try:
                inner.actuate(mv)
            except Exception:
                try:
                    if inner.conn.in_transaction:
                        inner.conn.rollback()
                except Exception:
                    pass
                raise
            delta = inner.conn.total_changes - before_changes
            if delta > 0:
                self._write_count += delta

        def _observe(mv: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
            return inner.observe(mv)

        return SourceBridge(actuate=_actuate, observe=_observe)

    def external_write_count(self) -> int:
        return self._write_count


# ---------------------------------------------------------------------------
# Live Odoo 19 Ephemeral Sandbox Adapter for std_o2c_ontology
# ---------------------------------------------------------------------------
class OdooO2cSandboxAdapter:
    """ExternalSandboxAdapter backed by a disposable EphemeralOdooSandbox clone."""

    def __init__(self, client: OdooJSONRPC, *, source_db_label: str = "erp_north_clone") -> None:
        self.client = client
        self.source_db_label = source_db_label
        self.harness = PhysicalOdooHarness(live_client=client)
        self._write_count = 0
        self._entity_id = f"ENT-{source_db_label.upper()}"
        self._partner_ref = "O2C-FALS-CUST"
        self._partner_id: int = 0
        self._flow_a_order_id: int = 0
        self._flow_b_order_id: int = 0
        self._flow_b_inv_id: int = 0
        self._flow_c_zero_order_id: int = 0
        self._order_tags: dict[str, int] = {}
        self._invoice_tags: dict[str, int] = {}

    def reset(self) -> None:
        run_tag = f"FALS-{uuid.uuid4().hex[:8]}"
        partners = self.client.search_read(
            "res.partner",
            [("ref", "=", self._partner_ref)],
            ["id"],
            limit=1,
        )
        if partners:
            self._partner_id = int(partners[0]["id"])
            self.client.write("res.partner", [self._partner_id], {"credit_limit": 200_000.0})
        else:
            self._partner_id = self.harness.create_partner(
                self._partner_ref,
                "Falsifier O2C Demo Partner",
                credit_limit=200_000.0,
            )

        # Archive any prior active orders for this synthetic partner inside clone DB
        old_orders = self.client.search_read(
            "sale.order",
            [("partner_id", "=", self._partner_id), ("client_order_ref", "not like", "__archived__%")],
            ["id", "client_order_ref"],
        )
        for o in old_orders:
            self.client.write(
                "sale.order",
                [int(o["id"])],
                {"client_order_ref": f"__archived__{o['id']}_{o.get('client_order_ref') or ''}"},
            )

        # Flow A: draft sale order -> tests confirm_o2c_order -> spawns picking -> tests complete_o2c_delivery
        ord_a = self.harness.create_draft_order(
            partner_id=self._partner_id,
            qty=2,
            unit_price=10_000.0,
            client_ref=f"{run_tag}-ORD-A",
        )
        self._flow_a_order_id = ord_a.order_id

        # Flow B: already confirmed & shipped order with draft invoice -> tests post_o2c_invoice & record_o2c_payment
        ord_b = self.harness.create_draft_order(
            partner_id=self._partner_id,
            qty=1,
            unit_price=15_000.0,
            client_ref=f"{run_tag}-ORD-B",
        )
        self.harness.confirm_order_physically(ord_b.order_id)
        self.harness.ship_order_physically_full(ord_b.order_id)
        inv_b_id = self.harness.create_draft_invoice_for_order(ord_b.order_id, taxed=False)
        self._flow_b_order_id = ord_b.order_id
        self._flow_b_inv_id = inv_b_id

        # Flow C: zero-amount draft order -> tests adversarial guardrail rejection & 0 dirty Odoo writes
        ord_zero = self.harness.create_draft_order(
            partner_id=self._partner_id,
            qty=1,
            unit_price=0.0,
            client_ref=f"{run_tag}-ORD-ZERO",
        )
        self._flow_c_zero_order_id = ord_zero.order_id

        self._order_tags = {
            "ORD-A": self._flow_a_order_id,
            "ORD-B": self._flow_b_order_id,
            "ORD-ZERO": self._flow_c_zero_order_id,
        }
        self._invoice_tags = {
            f"INV-{self._flow_b_inv_id}": self._flow_b_inv_id,
        }

    def _canonical_order_status(self, order_row: dict[str, Any], pickings: list[dict[str, Any]], invoices: list[dict[str, Any]]) -> str:
        st = str(order_row.get("state") or "")
        if st == "draft":
            return "draft"
        if st == "cancel":
            return "cancelled"
        if any(str(inv.get("state")) == "posted" for inv in invoices):
            return "invoiced"
        if any(str(pk.get("state")) == "done" for pk in pickings):
            return "delivered"
        return "confirmed"

    def _invoices_for_order(self, order_name: str) -> list[dict[str, Any]]:
        return self.client.search_read(
            "account.move",
            [("move_type", "=", "out_invoice"), ("invoice_origin", "=", order_name), ("state", "!=", "cancel")],
            fields=["id", "name", "state", "payment_state", "amount_total", "amount_residual", "invoice_origin"],
        )

    def snapshot_m0(self) -> list[dict[str, Any]]:
        party_id = f"PTY-{self._partner_id}"
        prof_id = f"PROF-{self._partner_id}"
        objects: list[dict[str, Any]] = [
            {
                "object_type": "AccountingEntity",
                "object_id": self._entity_id,
                "data": {"entity_id": self._entity_id, "name": "ERP North Legal Entity", "base_currency": "CNY", "status": "active"},
            },
            {
                "object_type": "BusinessParty",
                "object_id": party_id,
                "data": {"party_id": party_id, "legal_name": "Falsifier O2C Demo Partner", "status": "active"},
            },
            {
                "object_type": "O2cCustomerProfile",
                "object_id": prof_id,
                "data": {
                    "profile_id": prof_id,
                    "party": party_id,
                    "accounting_entity": self._entity_id,
                    "credit_limit": 200_000.0,
                    "payment_terms": "NET30",
                    "status": "active",
                },
            },
        ]

        for cid, oid in sorted(self._order_tags.items()):
            orow = self.harness.read_order(oid)
            pickings = self.harness.list_pickings_for_order(oid)
            invoices = self._invoices_for_order(str(orow["name"]))
            ostatus = self._canonical_order_status(orow, pickings, invoices)
            objects.append(
                {
                    "object_type": "O2cOrder",
                    "object_id": cid,
                    "data": {
                        "order_id": cid,
                        "order_number": str(orow["name"]),
                        "customer": prof_id,
                        "accounting_entity": self._entity_id,
                        "amount_total": round(float(orow.get("amount_total") or 0.0), 2),
                        "status": ostatus,
                    },
                }
            )
            for pk in pickings:
                did = f"DEL-{pk['id']}"
                pk_st = "completed" if pk["state"] == "done" else ("cancelled" if pk["state"] == "cancel" else "pending")
                objects.append(
                    {
                        "object_type": "O2cDelivery",
                        "object_id": did,
                        "data": {"delivery_id": did, "order": cid, "status": pk_st},
                    }
                )
            for inv in invoices:
                iid = f"INV-{inv['id']}"
                self._invoice_tags[iid] = int(inv["id"])
                inv_st = "posted" if inv["state"] == "posted" else ("cancelled" if inv["state"] == "cancel" else "draft")
                raw_pay = str(inv.get("payment_state") or "not_paid")
                pay_st = "paid" if raw_pay in ("paid", "in_payment") else ("partial" if raw_pay == "partial" else "not_paid")
                objects.append(
                    {
                        "object_type": "O2cInvoice",
                        "object_id": iid,
                        "data": {
                            "invoice_id": iid,
                            "order": cid,
                            "amount_total": round(float(inv.get("amount_total") or 0.0), 2),
                            "amount_residual": round(float(inv.get("amount_residual") or 0.0), 2),
                            "payment_state": pay_st,
                            "status": inv_st,
                        },
                    }
                )
        return objects

    def make_source_bridge(
        self,
        spec: OntologySpec,
        adapter_mapping: AdapterMappingSpec,
    ) -> SourceBridge:
        _ = spec, adapter_mapping

        def _actuate(mv: dict[str, Any]) -> None:
            act_name = str(mv.get("action_type") or "")
            params = dict(mv.get("params") or {})
            if act_name == "confirm_o2c_order":
                oid = self._order_tags[str(params["order_id"])]
                self.harness.confirm_order_physically(oid)
                self._write_count += 1
            elif act_name == "complete_o2c_delivery":
                did = str(params["delivery_id"])
                pick_id = int(did.split("-", 1)[1])
                pk = self.client.read("stock.picking", [pick_id], ["sale_id"])[0]
                sale_id = pk["sale_id"][0] if isinstance(pk["sale_id"], list) else pk["sale_id"]
                self.harness.ship_order_physically_full(int(sale_id))
                self._write_count += 1
            elif act_name == "post_o2c_invoice":
                iid = int(str(params["invoice_id"]).split("-", 1)[1])
                self.harness.post_invoice_physically(iid)
                self._write_count += 1
            elif act_name == "record_o2c_payment":
                iid = int(str(params["invoice_id"]).split("-", 1)[1])
                amt = float(params["payment_amount"])
                self.harness.register_payment_physically(iid, amount=amt)
                self._write_count += 1

        def _observe(mv: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
            changes = (mv.get("object_diff") or {}).get("object_changes") or []
            snap_by_key = {
                (str(e["object_type"]), str(e["object_id"])): dict(e["data"])
                for e in self.snapshot_m0()
            }
            observed: dict[str, list[dict[str, Any]]] = {}
            for ch in changes:
                tname = str(ch.get("object_type") or "")
                oid = str(ch.get("object_id") or "")
                if (tname, oid) in snap_by_key:
                    observed.setdefault(tname, []).append(snap_by_key[(tname, oid)])
            return observed

        return SourceBridge(actuate=_actuate, observe=_observe)

    def external_write_count(self) -> int:
        return self._write_count


# ---------------------------------------------------------------------------
# Red Test 1: Buggy Enum Map -> FALSIFIED (DIFFERENTIAL_STATE_MISMATCH) -> Self-Healed
# ---------------------------------------------------------------------------
def test_1_red_team_sabotaged_enum_map_caught_and_healed() -> None:
    locked_bp = build_locked_crm_target_blueprint()
    bad_mapping = build_offline_device_b_mapping()
    deal_em = next(em for em in bad_mapping.entity_mappings if em.target_object_type == "CrmDeal")
    for ft in deal_em.field_transforms:
        if ft.target_field == "stage":
            # Sabotage: include ambiguous duplicate target mapping so static check passes,
            # while reverse encoding emits an invalid physical literal rejected by Device B SQLite.
            ft.enum_map = {
                "phase_qual": "QUALIFICATION",
                "phase_proposal": "PROPOSAL",
                "phase_negotiate": "NEGOTIATION",
                "phase_won": "CLOSED_WON",
                "phase_won_invalid_alias": "CLOSED_WON",
                "phase_lost": "CLOSED_LOST",
            }
    good_mapping = build_offline_device_b_mapping()
    seen_feedbacks: list[str] = []

    eval_cases = [
        ProposedEvalCase(
            name="FALS-EVAL-WIN",
            actor={"actor_id": "eval:sales", "role": "sales_rep"},
            action="win_crm_deal",
            params={"deal_id": "DEAL-E1"},
            setup_objects=[
                {"object_type": "BusinessParty", "data": {"party_id": "PTY-E1", "legal_name": "EvalCo"}},
                {"object_type": "CrmAccount", "data": {"account_id": "ACC-E1", "name": "EvalAcc", "party": "PTY-E1"}},
                {"object_type": "CrmDeal", "data": {"deal_id": "DEAL-E1", "title": "EvalDeal", "account": "ACC-E1", "amount": 5000.0, "stage": "NEGOTIATION"}},
            ],
            expect={"valid": True},
        ).model_dump(mode="json")
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        baseline_b = tmp / "baseline_b.sqlite3"
        work_b = tmp / "work_b.sqlite3"
        build_device_b_heterogeneous_sqlite(baseline_b)
        profile = scout_sqlite_device(baseline_b, source_kind="sqlite_dev_b")["profile"]
        sandbox = SQLiteSandboxAdapter(baseline_db=baseline_b, work_db=work_b, source_kind="sqlite_dev_b")

        # 1A. Direct falsifier rejects buggy enum_map as FALSIFIED
        direct_report = DifferentialFalsifier().evaluate(
            blueprint=locked_bp,
            adapter_mapping=bad_mapping,
            sandbox=sandbox,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            target_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
        )
        assert direct_report.verdict == "FALSIFIED"
        assert direct_report.differential_mismatches >= 1
        assert any(ce.kind == "DIFFERENTIAL_STATE_MISMATCH" for ce in direct_report.counterexamples)

        # 1B. Closed healing loop automatically receives DIFFERENTIAL_STATE_MISMATCH and repairs it
        call_count = 0

        def _healing_backend(_sys: str, user_prompt: str, _r: int) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            payload = json.loads(user_prompt)
            seen_feedbacks.append(str(payload.get("previous_crucible_feedback") or ""))
            chosen = bad_mapping if call_count == 1 else good_mapping
            return {
                "rationale": f"call {call_count}",
                "adapter_mapping": chosen.model_dump(mode="json"),
                "customer_overlay": None,
                "proposed_eval_cases": eval_cases,
            }

        with InductionGovernanceService(db_path=":memory:") as gov:
            dec_agent = DriverDecompilerAgent(
                backend=CallableAbductionBackend(_healing_backend),
                governance=gov,
                crucible=SymbolicCrucible(),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            heal_res = decompile_and_falsify_driver(
                model_id="heal_enum_dev_b",
                target_blueprint=locked_bp,
                external_profile=profile,
                sandbox=sandbox,
                decompiler=dec_agent,
                governance=gov,
                required_object_types=("BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket"),
                required_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
                crucible_context=CrucibleContext(
                    mode="genesis",
                    replay_factory=build_crm_decompiler_replay_factory(),
                    default_actor=CRM_SALES_ACTOR,
                    seed_ontology=BUSINESS_SEED_ONTOLOGY,
                ),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
                max_healing_rounds=3,
            )
            assert heal_res.verified is True
            assert heal_res.corroborated is True
            assert heal_res.healing_rounds_used == 2
            assert heal_res.falsification is not None
            assert heal_res.falsification.verdict == "CORROBORATED"
            assert "DIFFERENTIAL_STATE_MISMATCH" in seen_feedbacks[1]


# ---------------------------------------------------------------------------
# Red Test 2: Misaligned FK / Action Binding Prefix -> FALSIFIED -> Self-Healed
# ---------------------------------------------------------------------------
def test_2_red_team_misaligned_fk_or_action_prefix_caught_and_healed() -> None:
    locked_bp = build_locked_crm_target_blueprint()
    bad_mapping = build_offline_device_a_mapping()
    # Sabotage action binding prefix ('OPP-' vs entity prefix 'DEAL-') -> 0 SQLite matches & Gate 2 / Falsifier catch
    for b in bad_mapping.action_bindings:
        if b.action_name == "win_crm_deal":
            b.id_prefix = "OPP-"
    # Also test FK prefix misalignment separately on CrmDeal.account
    bad_fk_mapping = build_offline_device_b_mapping()
    deal_em = next(em for em in bad_fk_mapping.entity_mappings if em.target_object_type == "CrmDeal")
    for ft in deal_em.field_transforms:
        if ft.target_field == "account":
            ft.prefix = "WRONGFK-"
    good_mapping = build_offline_device_a_mapping()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        base_a = tmp / "base_a.sqlite3"
        work_a = tmp / "work_a.sqlite3"
        shutil.copy2(BOTTLECRM_SQLITE_PATH, base_a)
        profile_a = scout_sqlite_device(base_a, source_kind="sqlite_bottlecrm")["profile"]
        sandbox_a = SQLiteSandboxAdapter(baseline_db=base_a, work_db=work_a, source_kind="sqlite_bottlecrm")

        base_b = tmp / "base_b.sqlite3"
        work_b = tmp / "work_b.sqlite3"
        build_device_b_heterogeneous_sqlite(base_b)
        sandbox_b = SQLiteSandboxAdapter(baseline_db=base_b, work_db=work_b, source_kind="sqlite_dev_b")

        # Misaligned FK prefix causes TARGET_NOT_FOUND / DIFFERENTIAL_STATE_MISMATCH -> FALSIFIED
        fk_report = DifferentialFalsifier().evaluate(
            blueprint=locked_bp,
            adapter_mapping=bad_fk_mapping,
            sandbox=sandbox_b,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            target_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
        )
        assert fk_report.verdict == "FALSIFIED"

        # Misaligned action binding prefix caught & repaired in decompile_and_falsify_driver
        attempt = 0

        def _prefix_repair_backend(_s: str, user_prompt: str, _r: int) -> dict[str, Any]:
            nonlocal attempt
            attempt += 1
            chosen = bad_mapping if attempt == 1 else good_mapping
            return {
                "rationale": f"prefix repair attempt {attempt}",
                "adapter_mapping": chosen.model_dump(mode="json"),
                "customer_overlay": None,
                "proposed_eval_cases": [],
            }

        with InductionGovernanceService(db_path=":memory:") as gov:
            dec_agent = DriverDecompilerAgent(
                backend=CallableAbductionBackend(_prefix_repair_backend),
                governance=gov,
                crucible=SymbolicCrucible(),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            heal_res = decompile_and_falsify_driver(
                model_id="heal_prefix_dev_a",
                target_blueprint=locked_bp,
                external_profile=profile_a,
                sandbox=sandbox_a,
                decompiler=dec_agent,
                governance=gov,
                required_object_types=("BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket"),
                required_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
                crucible_context=CrucibleContext(
                    mode="genesis",
                    replay_factory=build_crm_decompiler_replay_factory(),
                    default_actor=CRM_SALES_ACTOR,
                    seed_ontology=BUSINESS_SEED_ONTOLOGY,
                ),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
                max_decompile_rounds=3,
                max_healing_rounds=2,
            )
            assert heal_res.corroborated is True
            assert len(heal_res.decompilation.rounds) >= 2
            assert "ACTION_ID_PREFIX_MISMATCH" in heal_res.decompilation.rounds[0].feedback


# ---------------------------------------------------------------------------
# Red Test 3: Missing Customer Risk Overlay -> ADVERSARIAL_ESCAPE -> Self-Healed
# ---------------------------------------------------------------------------
def test_3_red_team_missing_customer_overlay_caught_and_healed() -> None:
    std_base = build_std_crm_blueprint()
    valid_overlay = build_standard_crm_customer_overlay()
    mapping_b = build_offline_device_b_mapping()
    seen_feedbacks: list[str] = []

    business_probes = [
        AdversarialProbe(
            name="PROBE-CRIT-TICKET-MUST-BLOCK-WIN",
            action="win_crm_deal",
            params={"deal_id": "DEAL-201"},
            actor=CRM_SALES_ACTOR,
            expect_valid=False,
            description="Open HIGH priority CrmTicket on client 101 must block win_crm_deal.",
        ),
        AdversarialProbe(
            name="PROBE-HIGH-VALUE-LOSS-MUST-REQUIRE-STEWARD",
            action="lose_crm_deal",
            params={"deal_id": "DEAL-203"},
            actor=CRM_SALES_ACTOR,
            expect_valid=False,
            expect_error_code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
            description="Losing >100k deal as sales_rep must trigger APPROVAL_REQUIRED_HIGH_VALUE_LOSS.",
        ),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        base_b = tmp / "base_b.sqlite3"
        work_b = tmp / "work_b.sqlite3"
        build_device_b_heterogeneous_sqlite(base_b)
        profile_b = scout_sqlite_device(base_b, source_kind="sqlite_dev_b")["profile"]
        sandbox_b = SQLiteSandboxAdapter(baseline_db=base_b, work_db=work_b, source_kind="sqlite_dev_b")

        attempt = 0

        def _overlay_healing_backend(_s: str, user_prompt: str, _r: int) -> dict[str, Any]:
            nonlocal attempt
            attempt += 1
            payload = json.loads(user_prompt)
            seen_feedbacks.append(str(payload.get("previous_crucible_feedback") or ""))
            # Round 1 omits CustomerOverlaySpec completely! Round 2 adds CustomerOverlaySpec.
            ov = None if attempt == 1 else valid_overlay.model_dump(mode="json")
            return {
                "rationale": f"overlay attempt {attempt}",
                "adapter_mapping": mapping_b.model_dump(mode="json"),
                "customer_overlay": ov,
                "proposed_eval_cases": [],
            }

        with InductionGovernanceService(db_path=":memory:") as gov:
            dec_agent = DriverDecompilerAgent(
                backend=CallableAbductionBackend(_overlay_healing_backend),
                governance=gov,
                crucible=SymbolicCrucible(),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            )
            heal_res = decompile_and_falsify_driver(
                model_id="heal_overlay_dev_b",
                target_blueprint=std_base,
                external_profile=profile_b,
                sandbox=sandbox_b,
                decompiler=dec_agent,
                governance=gov,
                falsifier=DifferentialFalsifier(adversarial_probes=business_probes),
                overlay_requirement_doc="Add CrmAccount.vip_tier, block win_crm_deal while HIGH/URGENT ticket open, require steward above 100k on loss.",
                required_object_types=("BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket"),
                required_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
                max_decompile_rounds=1,
                max_healing_rounds=3,
            )

            assert attempt == 2
            assert "ADVERSARIAL_ESCAPE" in seen_feedbacks[1]
            assert heal_res.verified is True
            assert heal_res.corroborated is True
            assert heal_res.healing_rounds_used == 2
            assert heal_res.falsification is not None
            assert heal_res.falsification.verdict == "CORROBORATED"
            assert heal_res.falsification.adversarial_escapes == 0
            assert heal_res.falsification.dirty_external_writes == 0


# ---------------------------------------------------------------------------
# Red Test 4: Anti-Fake-Safety (10x identical happy path -> FALSIFIED by INSUFFICIENT_COVERAGE)
# ---------------------------------------------------------------------------
def test_4_anti_fake_safety_rejects_10x_repeated_happy_path_as_insufficient_coverage() -> None:
    locked_bp = build_locked_crm_target_blueprint()
    mapping_b = build_offline_device_b_mapping()
    steward = Actor(actor_id="stew-1", role="steward")

    repeated_10_cases = [
        FalsificationCase(
            name=f"HAPPY-WIN-REPEAT-{i}",
            track="coverage",
            action="win_crm_deal",
            params={"deal_id": "DEAL-202"},
            actor=steward,
            expected_valid=True,
        )
        for i in range(10)
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        base_b = tmp / "base_b.sqlite3"
        work_b = tmp / "work_b.sqlite3"
        build_device_b_heterogeneous_sqlite(base_b)
        sandbox_b = SQLiteSandboxAdapter(
            baseline_db=base_b,
            work_db=work_b,
            adapter_mapping=mapping_b,
            source_kind="sqlite_dev_b",
        )

        report = DifferentialFalsifier().evaluate(
            blueprint=locked_bp,
            adapter_mapping=mapping_b,
            sandbox=sandbox_b,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            target_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
            custom_cases=repeated_10_cases,
            auto_generate=False,
        )

        assert report.total_cases_run == 10
        assert report.differential_mismatches == 0
        assert report.adversarial_escapes == 0
        assert report.dirty_external_writes == 0
        assert report.action_coverage_ratio == pytest.approx(1.0 / 3.0)
        assert report.verdict == "FALSIFIED"
        assert any(ce.kind == "INSUFFICIENT_COVERAGE" for ce in report.counterexamples)


# ---------------------------------------------------------------------------
# Test 5: Dual Real-Database Differential Acceptance (SQLite A/B + Live Odoo 19)
# ---------------------------------------------------------------------------
def test_5a_dual_real_sqlite_devices_corroborated_and_source_unmodified() -> None:
    src_sha_before = file_sha256(BOTTLECRM_SQLITE_PATH)
    locked_bp = build_locked_crm_target_blueprint()
    business_probes = [
        AdversarialProbe(
            name="PROBE-NORTH-HIGH-LOSS-STEWARD",
            action="lose_crm_deal",
            params={"deal_id": "DEAL-67268d54876f4f8fb51d06977011f823"},
            actor=CRM_SALES_ACTOR,
            expect_valid=False,
            expect_error_code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
        ),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Device A (real BottleCRM temp copy)
        base_a = tmp / "base_a.sqlite3"
        work_a = tmp / "work_a.sqlite3"
        shutil.copy2(BOTTLECRM_SQLITE_PATH, base_a)
        map_a = build_offline_device_a_mapping()
        sb_a = SQLiteSandboxAdapter(baseline_db=base_a, work_db=work_a, adapter_mapping=map_a, source_kind="sqlite_bottlecrm")

        rep_a = DifferentialFalsifier(adversarial_probes=business_probes).evaluate(
            blueprint=locked_bp,
            adapter_mapping=map_a,
            sandbox=sb_a,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            target_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
        )
        assert rep_a.verdict == "CORROBORATED", rep_a.diagnostic_feedback()
        assert rep_a.action_coverage_ratio == 1.0
        assert rep_a.transition_coverage_ratio >= 0.8
        assert rep_a.adversarial_escapes == 0
        assert rep_a.differential_mismatches == 0
        assert rep_a.dirty_external_writes == 0

        # Device B (29-table heterogeneous SQLite)
        base_b = tmp / "base_b.sqlite3"
        work_b = tmp / "work_b.sqlite3"
        build_device_b_heterogeneous_sqlite(base_b)
        map_b = build_offline_device_b_mapping()
        sb_b = SQLiteSandboxAdapter(baseline_db=base_b, work_db=work_b, adapter_mapping=map_b, source_kind="sqlite_dev_b")

        rep_b = DifferentialFalsifier().evaluate(
            blueprint=locked_bp,
            adapter_mapping=map_b,
            sandbox=sb_b,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            target_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
        )
        assert rep_b.verdict == "CORROBORATED", rep_b.diagnostic_feedback()
        assert rep_b.action_coverage_ratio == 1.0
        assert rep_b.transition_coverage_ratio >= 0.8
        assert rep_b.adversarial_escapes == 0
        assert rep_b.differential_mismatches == 0
        assert rep_b.dirty_external_writes == 0

    assert file_sha256(BOTTLECRM_SQLITE_PATH) == src_sha_before


def test_5b_live_odoo19_ephemeral_clone_o2c_corroborated_and_zero_db_residue() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    dbs_before = sorted(_jsonrpc("db", "list", []))
    assert dbs_before == ["erp_north", "erp_south", "odoo_demo"]

    std_o2c_bp = build_std_o2c_blueprint()
    o2c_dummy_mapping = AdapterMappingSpec(
        mapping_id="odoo19_std_o2c_live_driver",
        source_kind="odoo19_jsonrpc",
        entity_mappings=[
            EntityMappingSpec(
                source_table="res.partner",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="id", kind="id_prefix", prefix="PTY-"),
            ),
            EntityMappingSpec(
                source_table="sale.order",
                target_object_type="O2cOrder",
                id_transform=FieldTransformSpec(target_field="order_id", source_field="id", kind="direct"),
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="confirm_o2c_order",
                external_table="sale.order",
                external_method="action_confirm",
                target_object_type="O2cOrder",
                id_param="order_id",
            ),
            ActionExecutionBinding(
                action_name="complete_o2c_delivery",
                external_table="stock.picking",
                external_method="button_validate",
                target_object_type="O2cDelivery",
                id_param="delivery_id",
            ),
            ActionExecutionBinding(
                action_name="post_o2c_invoice",
                external_table="account.move",
                external_method="action_post",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
            ),
            ActionExecutionBinding(
                action_name="record_o2c_payment",
                external_table="account.payment.register",
                external_method="action_create_payments",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
            ),
        ],
    )

    zero_order_probe = AdversarialProbe(
        name="ODOO-ADV-ZERO-ORDER-CONFIRM",
        action="confirm_o2c_order",
        params={"order_id": "ORD-ZERO"},
        actor=Actor(actor_id="adv:sales", role="sales_rep"),
        expect_valid=False,
        expect_error_code="NON_POSITIVE_ORDER_AMOUNT",
    )

    with EphemeralOdooSandbox(source_db=DB_NORTH) as sb_client:
        sb_client._ensure_login()
        odoo_adapter = OdooO2cSandboxAdapter(sb_client._client, source_db_label=DB_NORTH)
        falsifier = DifferentialFalsifier(
            min_action_coverage=1.0,
            min_transition_coverage=0.8,
            adversarial_probes=[zero_order_probe],
        )
        report = falsifier.evaluate(
            blueprint=std_o2c_bp,
            adapter_mapping=o2c_dummy_mapping,
            sandbox=odoo_adapter,
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            target_actions=(
                "confirm_o2c_order",
                "complete_o2c_delivery",
                "post_o2c_invoice",
                "record_o2c_payment",
            ),
        )
        assert report.verdict == "CORROBORATED", report.diagnostic_feedback()
        assert report.action_coverage_ratio == 1.0
        assert report.transition_coverage_ratio >= 0.8
        assert report.adversarial_escapes == 0
        assert report.differential_mismatches == 0
        assert report.dirty_external_writes == 0

    dbs_after = sorted(_jsonrpc("db", "list", []))
    assert dbs_after == ["erp_north", "erp_south", "odoo_demo"]
