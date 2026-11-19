"""验收测试：Ontology Builder 输入「外部世界 Odoo 19 + 销售 Agent 自然语言需求」→ 输出「O2C Ontology」。

覆盖：
1. M2 纯洁性：``open_ontology/induction.py`` 零 Odoo 词汇泄漏；
2. 零 exec 投影器 ``project_external_phenomenon``：验证过滤非客户、内部调拨与红字退款单，
   并校验 many2one / lookup_join / enum_map / scale_int 变换；
3. 离线镜像端到端测试：第 1 轮脏数据破损与缺护栏被关 2/3 打回 → 第 2 轮五关全绿结晶
   → 销售 Agent 经 OSDK 实弹验证脱敏、未发货拦截、大单挂起审批与自有覆盖层写入；
4. 真实本机 Odoo 19（``erp_north``）端到端只读勘察验收：确认对 Odoo 零写入、噪声过滤准确、
   产出的 O2C 本体在真实 Odoo 采样记录上全链路验收通过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.odoo_builder_o2c_demo import (
    REQUIREMENT_DOC_PATH,
    OdooPhenomenonProfiler,
    _synthesize_odoo_adapter_mapping,
    build_offline_odoo_mirror_profile,
    run_odoo_to_o2c_builder_demo,
)
from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError
from open_ontology.induction import AdapterMappingSpec, project_external_phenomenon


def test_m2_induction_module_has_zero_odoo_leakage() -> None:
    pkg_dir = Path(__file__).resolve().parents[1] / "open_ontology" / "induction"
    py_files = sorted(pkg_dir.glob("*.py"))
    assert py_files, "expected open_ontology/induction/*.py files"
    for path in py_files:
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("odoo", "res.partner", "sale.order", "stock.picking", "account.move"):
            assert forbidden not in text, f"Odoo PSM 词条 {forbidden!r} 不得泄漏进 M2 {path.name}"
        for banned_it in ("outbox", "writeback", "target_system", "retry_count", "last_error", "lineage"):
            assert banned_it not in text, f"禁用 IT 词条 {banned_it!r} 不得泄漏进 M2 {path.name}"


def test_requirement_doc_exists_and_specifies_sales_agent_o2c_rules() -> None:
    assert REQUIREMENT_DOC_PATH.is_file()
    doc = REQUIREMENT_DOC_PATH.read_text(encoding="utf-8")
    for keyword in (
        "sales_agent",
        "Customer",
        "SalesOrder",
        "Delivery",
        "Invoice",
        "CANNOT_INVOICE_UNSHIPPED_ORDER",
        "PENDING_APPROVAL",
        "ontology_overlay",
    ):
        assert keyword in doc


def test_project_external_phenomenon_filters_odoo_noise_and_joins_origin() -> None:
    profile = build_offline_odoo_mirror_profile()
    mapping = AdapterMappingSpec.model_validate(_synthesize_odoo_adapter_mapping(clean_filters=True))
    projected, errors = project_external_phenomenon(profile, mapping)

    assert errors == []
    by_type: dict[str, list[dict]] = {}
    for row in projected:
        by_type.setdefault(row["object_type"], []).append(row["data"])

    # 非客户 (id=36, customer_rank=0) 已过滤
    assert len(by_type["Customer"]) == 2
    assert {c["customer_id"] for c in by_type["Customer"]} == {"CUST-77", "CUST-71"}
    assert all(c["credit_limit_cents"] == 10_000_000 for c in by_type["Customer"])

    # 3 张销售单状态枚举已归一化 (sale -> confirmed, draft -> draft)
    assert len(by_type["SalesOrder"]) == 3
    statuses = {o["order_id"]: o["status"] for o in by_type["SalesOrder"]}
    assert statuses == {"SO-7289": "confirmed", "SO-7286": "draft", "SO-7287": "draft"}

    # 无销售归属内部调拨单 (id=31, sale_id=False) 已过滤
    assert len(by_type["Delivery"]) == 1
    assert by_type["Delivery"][0]["order"] == "SO-7289"
    assert by_type["Delivery"][0]["status"] == "done"

    # 红字退款单 (id=1063, out_refund) 已过滤，且 invoice_origin='S07289' 成功 lookup_join 为 SO-7289
    assert len(by_type["Invoice"]) == 1
    assert by_type["Invoice"][0]["order"] == "SO-7289"
    assert by_type["Invoice"][0]["customer"] == "CUST-77"
    assert by_type["Invoice"][0]["status"] == "paid"


def test_odoo_builder_to_o2c_ontology_offline_mirror_end_to_end() -> None:
    out = run_odoo_to_o2c_builder_demo(offline_odoo=True, use_live_llm=False)

    assert out["input"]["odoo_mode"] == "offline-mirror"
    # 第 1 轮脏数据与缺护栏被关 2 & 关 3 打回，第 2 轮五关全绿
    assert out["builder_cegis"]["rounds"] == [
        {
            "round": 1,
            "passed": False,
            "failed_gates": ["gate_2_m0_grounding", "gate_3_counterexample_elimination"],
        },
        {
            "round": 2,
            "passed": True,
            "failed_gates": [],
        },
    ]
    assert out["builder_cegis"]["final_status"] == "CRYSTALLIZED"
    assert out["output_o2c_ontology"]["object_types"] == [
        "Customer",
        "SalesOrder",
        "Delivery",
        "Invoice",
    ]
    assert out["output_o2c_ontology"]["grounded_m0_counts_from_odoo"] == {
        "Customer": 2,
        "SalesOrder": 3,
        "Delivery": 1,
        "Invoice": 1,
    }
    sv = out["sales_agent_verification"]
    assert sv["customer_credit_masked_for_sales_agent"] is True
    assert sv["credit_limit_seen_by_sales_agent"] == 0
    assert sv["credit_limit_seen_by_finance_director"] == 10_000_000
    assert sv["unshipped_invoice_blocked"] is True
    assert sv["large_order_status"] == "PENDING_APPROVAL"
    assert sv["overlay_note_status"] == "COMMITTED"


def test_odoo_builder_to_o2c_ontology_live_odoo19_read_only() -> None:
    client = OdooJSONRPC(DB_NORTH)
    try:
        client.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    before_counts = {
        m: int(client.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }
    out = run_odoo_to_o2c_builder_demo(offline_odoo=False, use_live_llm=False, db=DB_NORTH)
    after_counts = {
        m: int(client.execute_kw(m, "search_count", [[]]))
        for m in ("res.partner", "sale.order", "stock.picking", "account.move")
    }

    # 铁律：只读勘察不得改变真实 Odoo 19 任意表行数
    assert before_counts == after_counts
    assert out["input"]["odoo_mode"] == f"live-odoo19:{DB_NORTH}"
    assert out["builder_cegis"]["final_status"] == "CRYSTALLIZED"
    assert out["builder_cegis"]["rounds"][0]["passed"] is False
    assert out["builder_cegis"]["rounds"][1]["passed"] is True

    grounded = out["output_o2c_ontology"]["grounded_m0_counts_from_odoo"]
    assert grounded["Customer"] >= 1
    assert grounded["SalesOrder"] >= 1
    assert grounded["Delivery"] >= 1
    assert grounded["Invoice"] >= 1
    # 采样中的脏记录（非客户/无销售调拨/红字单）被成功过滤，落库数严格小于原始采样数
    sampled = out["input"]["odoo_sampled_rows"]
    assert grounded["Customer"] < sampled["res.partner"]
    assert grounded["Delivery"] < sampled["stock.picking"]
    assert grounded["Invoice"] < sampled["account.move"]

    sv = out["sales_agent_verification"]
    assert sv["customer_credit_masked_for_sales_agent"] is True
    assert sv["unshipped_invoice_blocked"] is True
    assert sv["large_order_status"] == "PENDING_APPROVAL"
    assert sv["overlay_note_status"] == "COMMITTED"
