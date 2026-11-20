"""Acceptance tests for Step 3: Scoped OSDK Views + O2C x P2P Incremental Grafting."""

from __future__ import annotations

import pytest

from open_ontology import OntologyRuntime, PermissionDeniedError
from open_ontology.induction import compile_blueprint, measure_agent_complexity
from open_ontology.model_views import describe_tbox
from open_ontology.osdk import AIPAgentExecutor, export_llm_tools

from examples.business_ontology import build_business_seed_ontology
from examples.o2c_p2p_incremental_grafting import (
    O2C_OPS_ACTOR,
    P2P_OPS_ACTOR,
    crystallize_and_graft_o2c_p2p,
    run_o2c_p2p_grafting_demo,
)
from examples.odoo_dual_erp_demo import DB_NORTH, OdooJSONRPC, OdooRPCError, _jsonrpc


def test_scoped_views_filter_tbox_tools_and_enforce_runtime_isolation() -> None:
    _v1, _v2, spec_v2 = crystallize_and_graft_o2c_p2p()

    o2c_tools = export_llm_tools(spec_v2, actor=O2C_OPS_ACTOR, view="o2c_sales_ops")
    p2p_tools = export_llm_tools(spec_v2, actor=P2P_OPS_ACTOR, view="p2p_buyer_ops")
    o2c_tbox = {ot["name"] for ot in describe_tbox(spec_v2, actor=O2C_OPS_ACTOR, view="o2c_sales_ops")["object_types"]}
    p2p_tbox = {ot["name"] for ot in describe_tbox(spec_v2, actor=P2P_OPS_ACTOR, view="p2p_buyer_ops")["object_types"]}

    assert o2c_tbox == {
        "BusinessParty",
        "CustomerCommercialProfile",
        "WarehouseStockBalance",
        "CustomerSalesOrder",
        "InventoryMovementVoucher",
    }
    assert p2p_tbox == {
        "BusinessParty",
        "SupplierProcurementProfile",
        "PurchaseOrder",
        "InboundGoodsReceipt",
        "SupplierBill",
    }
    assert len(o2c_tools) > 0 and len(p2p_tools) > 0

    full_cx = measure_agent_complexity(spec_v2, regime_name="full")
    o2c_cx = measure_agent_complexity(spec_v2, regime_name="o2c", actor=O2C_OPS_ACTOR, view="o2c_sales_ops")
    p2p_cx = measure_agent_complexity(spec_v2, regime_name="p2p", actor=P2P_OPS_ACTOR, view="p2p_buyer_ops")
    assert o2c_cx.osdk_tool_schema_tokens_est < 0.60 * full_cx.osdk_tool_schema_tokens_est
    assert p2p_cx.osdk_tool_schema_tokens_est < 0.60 * full_cx.osdk_tool_schema_tokens_est

    # Wrong-role view access must fail fast with PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        describe_tbox(spec_v2, actor=O2C_OPS_ACTOR, view="p2p_buyer_ops")

    rt = OntologyRuntime(spec_v2, db_path=":memory:")
    try:
        o2c_exec = AIPAgentExecutor(rt, O2C_OPS_ACTOR, view="o2c_sales_ops", enforce_view_scope=True)
        with pytest.raises(PermissionDeniedError):
            o2c_exec.dispatch("query_objects", {"object_type": "PurchaseOrder"})
        with pytest.raises(PermissionDeniedError):
            o2c_exec.dispatch("submit_action", {"run_id": "X-1", "action": "post_supplier_bill", "params": {"bill_id": "B-1"}})
    finally:
        rt.close()


def test_o2c_p2p_incremental_grafting_offline_end_to_end() -> None:
    out = run_o2c_p2p_grafting_demo(offline=True)

    assert out["v1_version"] == "1.0.0"
    assert out["v2_version"] == "2.0.0"
    assert out["business_party_field_count"] <= 6
    assert out["o2c_token_ratio"] < 0.60
    assert out["p2p_token_ratio"] < 0.60
    assert out["cross_view_obj_blocked"] is True
    assert out["cross_view_action_blocked"] is True
    assert out["fork_replenishment"] == {
        "parent_on_hand_qty": 2.0,
        "fork_ending_on_hand_qty": 7.0,
        "voucher_net_delta": 5.0,
        "premature_bill_error_code": "PO_GOODS_NOT_RECEIVED_FOR_BILL",
    }
    assert out["work_card_approval"] == {
        "error_code": "APPROVAL_REQUIRED_SPEND_LIMIT_EXCEEDED",
        "work_card_status": "COMPLETED",
        "settled_status": "COMMITTED",
        "pending_approval_runs": 0,
    }


def test_o2c_p2p_incremental_grafting_live_erp_north_po_settlement() -> None:
    raw = OdooJSONRPC(DB_NORTH)
    try:
        raw.login()
    except OdooRPCError as exc:
        pytest.skip(f"live Odoo 19 unavailable: {exc}")

    dbs_before = set(_jsonrpc("db", "list", []))
    pos_before = int(raw.execute_kw("purchase.order", "search_count", [[("state", "=", "draft")]]))

    out = run_o2c_p2p_grafting_demo(offline=False, source_db=DB_NORTH)

    dbs_after = set(_jsonrpc("db", "list", []))
    pos_after = int(raw.execute_kw("purchase.order", "search_count", [[("state", "=", "draft")]]))

    assert dbs_before == dbs_after
    assert pos_before == pos_after
    po_res = out["live_odoo_p2p_settlement"]
    assert po_res is not None
    assert po_res["sandbox_db"] not in dbs_after
    assert po_res["settled_status"] == "COMMITTED"
    assert po_res["odoo_state_after"] in ("purchase", "done")
