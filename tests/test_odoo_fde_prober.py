"""验收测试：无人 FDE 探针 3（克隆隔离沙箱主动黑盒拷问 · 零硬编码 · 零污染生产）。

覆盖：
1. 源码零硬编码守卫：``examples/odoo_fde_prober.py`` 不含 ``sale.order``、``stock.picking``、
   ``account.move``、``action_confirm``、``button_validate``、``action_post``；
2. 跨领域离线模拟单测（基于合成诊所 ERP）：验证 ``ir.ui.view`` 按钮解析、正向状态迁移、
   跨表行数增量副作用、向导弹窗识别（``WIZARD_REQUIRED``）与原生异常护栏捕获（``NATIVE_ERROR``）；
3. 本机真实 Odoo 19 (``erp_north``) 克隆沙箱实测：验证生产库四表行数零变更、临时克隆库在退出后
   自动销毁零残留，并自主测出 ``sale.order.action_confirm`` 导致 ``stock.picking +1`` 跨表副作用与原生错误护栏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError, _jsonrpc
from examples.odoo_fde_prober import (
    EphemeralOdooSandbox,
    OdooActiveProber,
    format_probe_summary,
)
from examples.odoo_fde_scout import OdooFDEScout, ReadOnlyOdooClient
from tests.test_odoo_fde_scout import _SyntheticOdooMock


def test_prober_source_has_zero_hardcoded_business_tables_or_methods() -> None:
    text = (Path(__file__).resolve().parents[1] / "examples" / "odoo_fde_prober.py").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in (
        "sale.order",
        "stock.picking",
        "account.move",
        "action_confirm",
        "button_validate",
        "action_post",
    ):
        assert forbidden not in text, f"探针 3 不得硬编码具体业务表或方法 {forbidden!r}"


class _SyntheticProbeSandbox:
    """Offline mock sandbox for the synthetic clinic ERP (tests transitions, cross-table deltas, wizards, errors)."""

    def __init__(self, base_mock: _SyntheticOdooMock) -> None:
        self.tables = {k: [dict(r) for r in v] for k, v in base_mock.tables.items()}
        self.tables["ir.ui.view"] = [
            {
                "model": "clinic.visit",
                "type": "form",
                "arch_db": (
                    '<form><header>'
                    '<button name="do_confirm_visit" type="object" string="Confirm"/>'
                    '<button name="do_cancel_visit" type="object" string="Cancel"/>'
                    '<button name="action_view_bills" type="object" string="View"/>'
                    '</header></form>'
                ),
            },
            {
                "model": "clinic.bill",
                "type": "form",
                "arch_db": (
                    '<form><header>'
                    '<button name="do_register_payment" type="object" string="Pay"/>'
                    '<button name="do_post_bill" type="object" string="Post"/>'
                    '</header></form>'
                ),
            },
        ]

    def _matches(self, row: dict[str, Any], domain: list[Any]) -> bool:
        for fld, op, val in domain:
            cell = row.get(fld)
            if op == "=" and cell != val:
                return False
            if op == "in" and cell not in val:
                return False
        return True

    def search_count(self, model: str, domain: list[Any]) -> int:
        return sum(1 for r in self.tables.get(model, []) if self._matches(r, domain))

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        order: str = "",
    ) -> list[dict[str, Any]]:
        rows = [r for r in self.tables.get(model, []) if self._matches(r, domain)]
        return [{k: r.get(k) for k in fields if k in r} for r in rows[:limit]]

    def call_method(self, model: str, method: str, record_ids: list[int]) -> Any:
        rid = record_ids[0]
        rec = next(r for r in self.tables[model] if r["id"] == rid)
        if model == "clinic.visit" and method == "do_confirm_visit":
            if rec["state"] != "draft":
                raise RuntimeError("Visit must be in draft state to confirm.")
            rec["state"] = "done"
            # Cross-model side effect: confirming visit spawns a new clinic.bill
            self.tables["clinic.bill"].append(
                {
                    "id": 999,
                    "name": "BIL-AUTO",
                    "patient_id": rec["patient_id"],
                    "bill_kind": "patient_bill",
                    "visit_origin": rec["name"],
                    "state": "posted",
                    "total_amount": rec["total_amount"],
                }
            )
            return True
        if model == "clinic.visit" and method == "do_cancel_visit":
            rec["state"] = "cancel"
            return True
        if model == "clinic.bill" and method == "do_register_payment":
            return {"type": "ir.actions.act_window", "res_model": "clinic.payment.wizard"}
        if model == "clinic.bill" and method == "do_post_bill":
            raise RuntimeError("Bill is already posted or paid.")
        return True


def test_prober_discovers_views_transitions_cross_deltas_wizards_and_errors_offline() -> None:
    base = _SyntheticOdooMock()
    scout_report = OdooFDEScout(base, source_locator="mock://clinic").scout(task_intent="")
    sandbox = _SyntheticProbeSandbox(base)

    prober = OdooActiveProber(sandbox, sandbox_db="mock_clone_01")
    report = prober.probe(scout_report)

    # 导航按钮 action_view_bills 必须被自动过滤
    assert "do_confirm_visit" in report.discovered_action_methods["clinic.visit"]
    assert "action_view_bills" not in report.discovered_action_methods["clinic.visit"]

    # 正向因果 + 跨表副作用：confirm clinic.visit -> clinic.bill +1
    cross = report.cross_model_side_effects()
    assert any(
        o.model == "clinic.visit"
        and o.method == "do_confirm_visit"
        and o.state_before == "draft"
        and o.state_after == "done"
        and o.cross_model_row_deltas.get("clinic.bill") == 1
        for o in cross
    )

    # 向导弹窗识别
    wizards = report.wizard_interactions()
    assert any(o.wizard_model == "clinic.payment.wizard" for o in wizards)

    # 原生错误护栏捕获
    errs = report.native_error_guards()
    assert any("already posted" in (o.error_message or "") for o in errs)
    assert "Probe 3" in format_probe_summary(report)


def test_prober_live_odoo19_ephemeral_clone_is_dropped_and_source_untouched() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    dbs_before = set(_jsonrpc("db", "list", []))
    counts_before = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    scout_report = OdooFDEScout(
        ReadOnlyOdooClient(db=DB_NORTH, client=raw),
        source_locator=f"odoo://{DB_NORTH}",
        max_models=6,
    ).scout(task_intent="")

    with EphemeralOdooSandbox(source_db=DB_NORTH) as sandbox_client:
        clone_name = sandbox_client.db
        probe_report = OdooActiveProber(sandbox_client, sandbox_db=clone_name).probe(scout_report)

    dbs_after = set(_jsonrpc("db", "list", []))
    counts_after = {
        m: int(raw.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    # 铁律 1：临时克隆库必须被彻底销毁，零残留
    assert clone_name not in dbs_after
    assert dbs_before == dbs_after
    # 铁律 2：源生产库 erp_north 零写入
    assert counts_before == counts_after

    # 实证发现：跨表副作用（confirm sale.order -> stock.picking +1）与系统原生报错护栏
    assert any(
        o.model == "sale.order"
        and o.method == "action_confirm"
        and o.state_before == "draft"
        and o.state_after == "sale"
        and o.cross_model_row_deltas.get("stock.picking", 0) >= 1
        for o in probe_report.cross_model_side_effects()
    )
    assert len(probe_report.native_error_guards()) >= 3
