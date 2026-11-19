"""验收测试：无人 FDE 自主侦察器（零硬编码表名/字段名 · 纯只读 · 无人类拷问）。

验证：
1. 源码零硬编码守卫：``examples/odoo_fde_scout.py`` 中不含 ``sale.order``、``stock.picking``、
   ``account.move``、``invoice_origin``、``move_type``、``credit_limit``、``100000``；
2. 跨领域合成 ERP 单测（医疗诊所 ``clinic.patient`` / ``clinic.visit`` / ``clinic.bill``）：
   证明算法完全不依赖 Odoo 业务词汇，通用完成熵裁剪、状态机挖掘、隐藏字符串外键发现、
   多态表判别器发现与跨表额度越界灰区识别；
3. 本机真实 Odoo 19 (``erp_north``) 纯只读无监督侦察验收（``task_intent=""``）：
   确认对 Odoo 零写入，从 340+ 表 / 8,370 字段自动压缩至 <2% 高熵核心子图，并自主发现
   O2C 活跃表、真实状态机、``account.move.move_type`` 混合表、``invoice_origin -> sale.order.name``
   隐藏关联、``stock.picking.sale_id`` 空外键子集与 ``sale.order > credit_limit`` 灰区矛盾。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError
from examples.odoo_fde_scout import OdooFDEScout, ReadOnlyOdooClient, format_scout_summary


def test_fde_scout_source_has_zero_hardcoded_business_tables_or_fields() -> None:
    text = (Path(__file__).resolve().parents[1] / "examples" / "odoo_fde_scout.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in (
        "sale.order",
        "stock.picking",
        "account.move",
        "invoice_origin",
        "move_type",
        "credit_limit",
        "100000",
    ):
        assert forbidden not in text, f"禁止硬编码业务词条 {forbidden!r}"


class _SyntheticOdooMock:
    """完全虚构的诊所 ERP 内存模拟（零 O2C 词汇），验证 FDE 侦察器跨领域通用性。"""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "ir.model": [
                {"id": 1, "model": "clinic.patient", "name": "Patient", "transient": False},
                {"id": 2, "model": "clinic.visit", "name": "Clinic Visit", "transient": False},
                {"id": 3, "model": "clinic.bill", "name": "Clinic Bill", "transient": False},
                {"id": 4, "model": "ir.ui.view", "name": "View", "transient": False},
            ],
            "ir.model.fields": [
                # clinic.patient
                {"id": 101, "model": "clinic.patient", "name": "id", "field_description": "ID", "ttype": "integer", "relation": False, "store": True, "required": True, "readonly": True},
                {"id": 102, "model": "clinic.patient", "name": "name", "field_description": "Name", "ttype": "char", "relation": False, "store": True, "required": True, "readonly": False},
                {"id": 103, "model": "clinic.patient", "name": "coverage_limit", "field_description": "Coverage Limit", "ttype": "float", "relation": False, "store": True, "required": False, "readonly": False},
                {"id": 104, "model": "clinic.patient", "name": "unused_const", "field_description": "Unused", "ttype": "char", "relation": False, "store": True, "required": False, "readonly": False},
                # clinic.visit
                {"id": 201, "model": "clinic.visit", "name": "id", "field_description": "ID", "ttype": "integer", "relation": False, "store": True, "required": True, "readonly": True},
                {"id": 202, "model": "clinic.visit", "name": "name", "field_description": "Visit Code", "ttype": "char", "relation": False, "store": True, "required": True, "readonly": False},
                {"id": 203, "model": "clinic.visit", "name": "patient_id", "field_description": "Patient", "ttype": "many2one", "relation": "clinic.patient", "store": True, "required": True, "readonly": False},
                {"id": 204, "model": "clinic.visit", "name": "state", "field_description": "Status", "ttype": "selection", "relation": False, "store": True, "required": True, "readonly": False},
                {"id": 205, "model": "clinic.visit", "name": "total_amount", "field_description": "Total Amount", "ttype": "monetary", "relation": False, "store": True, "required": False, "readonly": False},
                # clinic.bill
                {"id": 301, "model": "clinic.bill", "name": "id", "field_description": "ID", "ttype": "integer", "relation": False, "store": True, "required": True, "readonly": True},
                {"id": 302, "model": "clinic.bill", "name": "name", "field_description": "Bill Code", "ttype": "char", "relation": False, "store": True, "required": True, "readonly": False},
                {"id": 303, "model": "clinic.bill", "name": "patient_id", "field_description": "Patient", "ttype": "many2one", "relation": "clinic.patient", "store": True, "required": False, "readonly": False},
                {"id": 304, "model": "clinic.bill", "name": "bill_kind", "field_description": "Bill Kind", "ttype": "selection", "relation": False, "store": True, "required": True, "readonly": False},
                {"id": 305, "model": "clinic.bill", "name": "visit_origin", "field_description": "Visit Origin", "ttype": "char", "relation": False, "store": True, "required": False, "readonly": False},
                {"id": 306, "model": "clinic.bill", "name": "state", "field_description": "Status", "ttype": "selection", "relation": False, "store": True, "required": True, "readonly": False},
                {"id": 307, "model": "clinic.bill", "name": "total_amount", "field_description": "Amount", "ttype": "monetary", "relation": False, "store": True, "required": False, "readonly": False},
            ],
            "ir.model.constraint": [
                {"id": 1, "name": "clinic_visit_name_uniq", "type": "u", "definition": "UNIQUE(name)", "message": "Duplicate visit code", "model": [2, "Clinic Visit"]},
            ],
            "ir.rule": [{"id": 1, "model_id": [2, "Clinic Visit"], "active": True}],
            "clinic.patient": [
                {"id": 1, "name": "Alice", "coverage_limit": 500.0, "unused_const": "SAME"},
                {"id": 2, "name": "Bob", "coverage_limit": 500.0, "unused_const": "SAME"},
            ],
            "clinic.visit": [
                {"id": 10, "name": "VST-001", "patient_id": [1, "Alice"], "state": "done", "total_amount": 300.0},
                {"id": 11, "name": "VST-002", "patient_id": [1, "Alice"], "state": "done", "total_amount": 800.0},
                {"id": 12, "name": "VST-003", "patient_id": [2, "Bob"], "state": "draft", "total_amount": 900.0},
                {"id": 13, "name": "VST-004", "patient_id": [2, "Bob"], "state": "done", "total_amount": 200.0},
            ],
            "clinic.bill": [
                {"id": 20, "name": "BIL-001", "patient_id": [1, "Alice"], "bill_kind": "patient_bill", "visit_origin": "VST-001", "state": "paid", "total_amount": 300.0},
                {"id": 21, "name": "BIL-002", "patient_id": [1, "Alice"], "bill_kind": "patient_bill", "visit_origin": "VST-002", "state": "posted", "total_amount": 800.0},
                {"id": 22, "name": "BIL-003", "patient_id": [2, "Bob"], "bill_kind": "patient_bill", "visit_origin": "VST-004", "state": "paid", "total_amount": 200.0},
                {"id": 23, "name": "BIL-004", "patient_id": False, "bill_kind": "internal_adj", "visit_origin": False, "state": "posted", "total_amount": 50.0},
                {"id": 24, "name": "BIL-005", "patient_id": False, "bill_kind": "internal_adj", "visit_origin": False, "state": "posted", "total_amount": 60.0},
                {"id": 25, "name": "BIL-006", "patient_id": False, "bill_kind": "internal_adj", "visit_origin": False, "state": "posted", "total_amount": 70.0},
            ],
        }

    def _matches(self, row: dict[str, Any], domain: list[Any]) -> bool:
        for cond in domain:
            fld, op, val = cond
            cell = row.get(fld)
            if isinstance(cell, list) and cell:
                cell_cmp = cell[0]
            else:
                cell_cmp = cell
            if op == "=" and cell_cmp != val:
                return False
            if op == "!=" and cell_cmp == val:
                return False
            if op == "in" and cell_cmp not in val:
                return False
        return True

    def search_count(self, model: str, domain: list[Any]) -> int:
        if model == "mail.tracking.value":
            return 25
        rows = self.tables.get(model, [])
        return sum(1 for r in rows if self._matches(r, domain))

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 80,
        order: str = "",
    ) -> list[dict[str, Any]]:
        rows = [r for r in self.tables.get(model, []) if self._matches(r, domain)]
        out = [{k: r.get(k) for k in fields if k in r} for r in rows[:limit]]
        return out

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        lazy: bool = True,
    ) -> list[dict[str, Any]]:
        if model == "mail.message":
            return [
                {"model": "clinic.visit", "model_count": 40},
                {"model": "clinic.bill", "model_count": 30},
                {"model": "clinic.patient", "model_count": 5},
            ]
        if model == "mail.tracking.value" and groupby == ["field_id"]:
            return [
                {"field_id": [204, "Status (Visit)"], "field_id_count": 15},
                {"field_id": [306, "Status (Bill)"], "field_id_count": 10},
            ]
        if model == "mail.tracking.value" and len(groupby) == 3:
            return [
                {"field_id": [204, "Status (Visit)"], "old_value_char": "Draft", "new_value_char": "Done", "__count": 12},
                {"field_id": [204, "Status (Visit)"], "old_value_char": "Done", "new_value_char": "Cancelled", "__count": 3},
                {"field_id": [306, "Status (Bill)"], "old_value_char": "Posted", "new_value_char": "Paid", "__count": 10},
            ]
        return []

    def fields_get(
        self,
        model: str,
        fields: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        selections = {
            ("clinic.visit", "state"): [["draft", "Draft"], ["done", "Done"], ["cancel", "Cancelled"]],
            ("clinic.bill", "state"): [["posted", "Posted"], ["paid", "Paid"]],
            ("clinic.bill", "bill_kind"): [["patient_bill", "Patient Bill"], ["internal_adj", "Internal Adj"]],
        }
        out: dict[str, dict[str, Any]] = {}
        for f in fields or []:
            if (model, f) in selections:
                out[f] = {"selection": selections[(model, f)]}
        return out


def test_fde_scout_generalizes_to_arbitrary_synthetic_clinic_erp() -> None:
    scout = OdooFDEScout(_SyntheticOdooMock(), source_locator="mock://clinic", max_models=5)
    report = scout.scout(task_intent="")

    models = {m.model: m for m in report.discovered_models}
    assert set(models) == {"clinic.visit", "clinic.bill", "clinic.patient"}
    # 零熵常量列 unused_const 必须被熵裁剪掉
    patient_fields = {f.name for f in models["clinic.patient"].retained_fields}
    assert "unused_const" not in patient_fields
    assert "coverage_limit" in patient_fields
    assert models["clinic.patient"].suggested_archetype == "MasterEntity"
    assert models["clinic.visit"].suggested_archetype == "ProcessDocument"

    # 隐藏字符串外键 clinic.bill.visit_origin -> clinic.visit.name 被自动发现
    str_joins = {(r.source_model, r.source_field, r.target_model, r.target_field) for r in report.discovered_relations if r.kind == "inferred_string_join"}
    assert ("clinic.bill", "visit_origin", "clinic.visit", "name") in str_joins

    # 状态机从 mail.tracking.value 自动挖掘，且正确反向映射为 key
    sm_map = {(sm.model, sm.field_name): sm for sm in report.mined_state_machines}
    visit_edges = {(e.from_state, e.to_state): e.count for e in sm_map[("clinic.visit", "state")].edges}
    assert visit_edges == {("draft", "done"): 12, ("done", "cancel"): 3}

    # 多态混合表 clinic.bill.bill_kind 与跨表额度越界灰区被自动发现
    poly_fields = {(p.model, p.field_name) for p in report.polymorphic_discriminators}
    assert ("clinic.bill", "bill_kind") in poly_fields
    finding_kinds = {(f.kind, f.model, f.epistemic_tier) for f in report.epistemic_findings}
    assert ("CROSS_ENTITY_LIMIT_EXCEEDANCE_CONTRADICTION", "clinic.visit", "AMBIGUOUS_GRAY_ZONE") in finding_kinds
    assert ("POLYMORPHIC_MIXED_TABLE", "clinic.bill", "INFERRED_TRACE") in finding_kinds
    assert "Autonomous FDE Scout Report" in format_scout_summary(report)


def test_fde_scout_live_odoo19_unsupervised_read_only() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    before_counts = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }
    scout = OdooFDEScout(
        ReadOnlyOdooClient(db=DB_NORTH, client=raw),
        source_locator=f"odoo://{DB_NORTH}",
        max_models=6,
    )
    # 纯无监督（空 task_intent），不给任何表名、字段名或人类问答
    report = scout.scout(task_intent="")

    after_counts = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }
    assert before_counts == after_counts

    # 1. 高压缩比（从 300+ 表 / 8000+ 字段自动压到 < 2%）
    assert report.total_models_scanned >= 300
    assert report.total_fields_scanned >= 8000
    assert report.field_compression_ratio < 0.02

    # 2. 零硬编码自主定位四大 O2C 核心表
    discovered_names = {m.model for m in report.discovered_models}
    assert {"sale.order", "stock.picking", "account.move", "res.partner"}.issubset(discovered_names)

    # 3. 自主发现隐藏字符串关联 account.move.invoice_origin -> sale.order.name
    str_joins = {
        (r.source_model, r.source_field, r.target_model, r.target_field): r.confidence
        for r in report.discovered_relations
        if r.kind == "inferred_string_join"
    }
    assert ("account.move", "invoice_origin", "sale.order", "name") in str_joins
    assert str_joins[("account.move", "invoice_origin", "sale.order", "name")] >= 0.8

    # 4. 自主从 mail.tracking.value 挖出真实状态机
    sm_keys = {(sm.model, sm.field_name) for sm in report.mined_state_machines}
    assert {
        ("sale.order", "state"),
        ("stock.picking", "state"),
        ("account.move", "state"),
        ("account.move", "payment_state"),
    }.issubset(sm_keys)

    # 5. 自主挖出多态表 move_type、空外键 sale_id、及超信用额度灰区矛盾
    poly_keys = {(p.model, p.field_name) for p in report.polymorphic_discriminators}
    assert ("account.move", "move_type") in poly_keys
    finding_sigs = {(f.kind, f.model, f.epistemic_tier) for f in report.epistemic_findings}
    assert ("ORPHAN_OR_UNLINKED_SUBSET", "stock.picking", "INFERRED_TRACE") in finding_sigs
    assert ("CROSS_ENTITY_LIMIT_EXCEEDANCE_CONTRADICTION", "sale.order", "AMBIGUOUS_GRAY_ZONE") in finding_sigs
