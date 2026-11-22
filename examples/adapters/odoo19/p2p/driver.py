from __future__ import annotations

import base64
import datetime
import json
import re
import urllib.request
from typing import Any, Mapping

from open_ontology.induction.dsl import AdapterMappingSpec
from open_ontology.settlement import SourceBridge

_QC_TAG_RE = re.compile(r"O2O_QC:([A-Za-z0-9+/=]+)")
_CONFLICT_TOKENS = ("usererror", "validationerror", "accesserror", "missingerror")


class _OdooRpcFault(RuntimeError):
    def __init__(self, err: Mapping[str, Any]) -> None:
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        self.fault_name = str(data.get("name") or "")
        super().__init__(f"{self.fault_name}: {data.get('message') or err.get('message') or ''}")


def _m2o_id(val: Any) -> int | None:
    if isinstance(val, (list, tuple)) and val:
        return int(val[0])
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def _m2o_name(val: Any, default: str = "") -> str:
    return str(val[1]) if isinstance(val, (list, tuple)) and len(val) > 1 and val[1] else default


def _parse_qc_note(note: Any) -> dict[str, Any] | None:
    if not isinstance(note, str):
        return None
    m = _QC_TAG_RE.search(note)
    if not m:
        return None
    try:
        obj = json.loads(base64.b64decode(m.group(1).encode("ascii")).decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _encode_qc_note(received_qty: float, inspection_status: str) -> str:
    raw = json.dumps({"received_qty": float(received_qty), "inspection_status": str(inspection_status)}, separators=(",", ":")).encode("utf-8")
    return "O2O_QC:" + base64.b64encode(raw).decode("ascii")


def _strip_prefix(raw_id: Any, prefix: str) -> int:
    text = str(raw_id)
    return int(text[len(prefix):] if prefix and text.startswith(prefix) else text)


class _OdooClient:
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self._url, self._db = str(cfg["url"]), str(cfg["db"])
        self._user, self._pwd = str(cfg["username"]), str(cfg["password"])
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._req_id = 0
        self._uid: int | None = None

    def _rpc(self, service: str, method: str, args: list[Any]) -> Any:
        self._req_id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "params": {"service": service, "method": method, "args": args}, "id": self._req_id}).encode("utf-8")
        req = urllib.request.Request(self._url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with self._opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("error"):
            raise _OdooRpcFault(data["error"])
        return data.get("result")

    def call(self, model: str, method: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None) -> Any:
        if self._uid is None:
            uid = self._rpc("common", "authenticate", [self._db, self._user, self._pwd, {}])
            if not uid:
                raise RuntimeError("Authentication failed")
            self._uid = int(uid)
        return self._rpc("object", "execute_kw", [self._db, self._uid, self._pwd, model, method, args or [], kwargs or {}])


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: AdapterMappingSpec) -> SourceBridge:
    client = _OdooClient(endpoint_config)
    pfx = {em.target_object_type: str(em.id_transform.prefix or "") for em in mapping.entity_mappings}
    p_ent, p_pty = pfx.get("AccountingEntity", "ENT-"), pfx.get("BusinessParty", "PTY-")
    p_sup, p_po = pfx.get("P2pSupplierProfile", "SUP-"), pfx.get("P2pPurchaseOrder", "PO-")
    p_gr, p_bill = pfx.get("P2pGoodsReceipt", "GR-"), pfx.get("P2pVendorBill", "BILL-")

    def observe(move_envelope: Mapping[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        companies = client.call("res.company", "search_read", [[]], {"fields": ["id", "name", "currency_id", "active"], "context": {"active_test": False}})
        def_comp = int(companies[0]["id"]) if companies else 1
        entities = [
            {"entity_id": f"{p_ent}{c['id']}", "name": str(c.get("name") or ""), "base_currency": _m2o_name(c.get("currency_id"), "USD"), "status": "active" if c.get("active", True) else "inactive"}
            for c in companies
        ]

        pos = client.call("purchase.order", "search_read", [[]], {"fields": ["id", "name", "partner_id", "company_id", "state", "amount_total", "order_line", "picking_ids", "invoice_ids", "receipt_status"]})
        po_ids = [int(p["id"]) for p in pos]
        po_name_map = {str(p["name"]): int(p["id"]) for p in pos if p.get("name")}
        po_partners: set[int] = set()
        partner_comp: dict[int, int] = {}
        pick_to_po: dict[int, int] = {}
        bill_to_po: dict[int, int] = {}
        for po in pos:
            pid = int(po["id"])
            part_id = _m2o_id(po.get("partner_id"))
            comp_id = _m2o_id(po.get("company_id")) or def_comp
            if part_id is not None:
                po_partners.add(part_id)
                partner_comp.setdefault(part_id, comp_id)
            for pk in po.get("picking_ids") or []:
                pick_to_po[int(pk)] = pid
            for inv in po.get("invoice_ids") or []:
                bill_to_po[int(inv)] = pid

        pol_by_po: dict[int, list[dict[str, Any]]] = {}
        if po_ids:
            for line in client.call("purchase.order.line", "search_read", [[["order_id", "in", po_ids]]], {"fields": ["order_id", "product_qty", "price_unit"]}):
                if (oid := _m2o_id(line.get("order_id"))) is not None:
                    pol_by_po.setdefault(oid, []).append(line)

        p_dom: list[Any] = ["|", ["supplier_rank", ">", 0], ["id", "in", sorted(po_partners)]] if po_partners else [["supplier_rank", ">", 0]]
        partners = client.call("res.partner", "search_read", [p_dom], {"fields": ["id", "name", "vat", "country_id", "active", "company_id", "property_supplier_payment_term_id"], "context": {"active_test": False}})
        c_ids = sorted({cid for p in partners if (cid := _m2o_id(p.get("country_id"))) is not None})
        c_codes = {int(c["id"]): str(c.get("code") or "CN") for c in client.call("res.country", "search_read", [[["id", "in", c_ids]]], {"fields": ["id", "code"]})} if c_ids else {}

        parties, suppliers = [], []
        for p in partners:
            pid = int(p["id"])
            st = "active" if p.get("active", True) else "inactive"
            cid = _m2o_id(p.get("country_id"))
            comp_id = _m2o_id(p.get("company_id")) or partner_comp.get(pid) or def_comp
            parties.append({"party_id": f"{p_pty}{pid}", "legal_name": str(p.get("name") or ""), "tax_id": str(p.get("vat") or ""), "country_code": c_codes.get(cid, "CN") if cid is not None else "CN", "status": st})
            suppliers.append({"profile_id": f"{p_sup}{pid}", "party": f"{p_pty}{pid}", "accounting_entity": f"{p_ent}{comp_id}", "payment_terms": _m2o_name(p.get("property_supplier_payment_term_id"), "NET30"), "status": st})

        picks: list[dict[str, Any]] = []
        pick_ids = sorted(pick_to_po.keys())
        if pick_ids or po_ids:
            pk_dom: list[Any] = [["picking_type_code", "=", "incoming"]]
            if pick_ids and po_ids:
                pk_dom.extend(["|", ["id", "in", pick_ids], ["purchase_id", "in", po_ids]])
            else:
                pk_dom.append(["id", "in", pick_ids] if pick_ids else ["purchase_id", "in", po_ids])
            picks = client.call("stock.picking", "search_read", [pk_dom], {"fields": ["id", "origin", "state", "purchase_id", "note"]})

        moves_by_pick: dict[int, list[dict[str, Any]]] = {}
        if picks:
            for mv in client.call("stock.move", "search_read", [[["picking_id", "in", [int(pk["id"]) for pk in picks]]]], {"fields": ["picking_id", "product_uom_qty", "quantity", "state"]}):
                if (pkid := _m2o_id(mv.get("picking_id"))) is not None:
                    moves_by_pick.setdefault(pkid, []).append(mv)

        receipts, rec_states_by_po = [], {}
        for pk in picks:
            pkid = int(pk["id"])
            linked_po = _m2o_id(pk.get("purchase_id")) or pick_to_po.get(pkid) or po_name_map.get(str(pk.get("origin") or ""))
            if linked_po is None:
                continue
            pmoves = moves_by_pick.get(pkid, [])
            exp_qty = round(sum(float(m.get("product_uom_qty") or 0.0) for m in pmoves), 6)
            st, qc = str(pk.get("state") or ""), _parse_qc_note(pk.get("note"))
            if st == "done":
                rec_qty = round(sum(float(m.get("quantity") or 0.0) for m in pmoves), 6)
                insp, r_st = (str(qc.get("inspection_status", "passed")) if qc else "passed"), "completed"
            elif st == "cancel":
                rec_qty, insp, r_st = 0.0, "pending", "cancelled"
            elif qc is not None:
                rec_qty = round(float(qc.get("received_qty", 0.0)), 6)
                insp = str(qc.get("inspection_status", "pending"))
                r_st = "disputed" if (rec_qty < exp_qty or insp == "failed") else "pending"
            else:
                rec_qty, insp, r_st = 0.0, "pending", "pending"
            rec_states_by_po.setdefault(linked_po, []).append(r_st)
            receipts.append({"receipt_id": f"{p_gr}{pkid}", "purchase_order": f"{p_po}{linked_po}", "expected_qty": exp_qty, "received_qty": rec_qty, "inspection_status": insp, "status": r_st})

        raw_bills = client.call("account.move", "search_read", [[["move_type", "=", "in_invoice"]]], {"fields": ["id", "state", "payment_state", "invoice_origin", "amount_total", "amount_residual"]})
        line_bill_to_po: dict[int, int] = {}
        if raw_bills:
            for bl in client.call("account.move.line", "search_read", [[["move_id", "in", [int(b["id"]) for b in raw_bills]], ["purchase_order_id", "!=", False]]], {"fields": ["move_id", "purchase_order_id"]}):
                mid, oid = _m2o_id(bl.get("move_id")), _m2o_id(bl.get("purchase_order_id"))
                if mid is not None and oid is not None:
                    line_bill_to_po[mid] = oid

        bills, bill_states_by_po = [], {}
        for b in raw_bills:
            bid = int(b["id"])
            linked_po = bill_to_po.get(bid) or line_bill_to_po.get(bid) or po_name_map.get(str(b.get("invoice_origin") or ""))
            if linked_po is None:
                continue
            rst = str(b.get("state") or "draft")
            b_st = "posted" if rst == "posted" else ("cancelled" if rst == "cancel" else "draft")
            amt_tot = round(abs(float(b.get("amount_total") or 0.0)), 6)
            amt_res = round(abs(float(b.get("amount_residual") or 0.0)), 6)
            rps = str(b.get("payment_state") or "not_paid")
            if rps in ("paid", "in_payment") or (b_st == "posted" and amt_tot > 0 and amt_res <= 1e-6):
                pay_st, amt_res = "paid", 0.0
            elif rps == "partial" or (b_st == "posted" and 1e-6 < amt_res < amt_tot - 1e-6):
                pay_st = "partial"
            else:
                pay_st = "not_paid"
            bill_states_by_po.setdefault(linked_po, []).append(b_st)
            bills.append({"bill_id": f"{p_bill}{bid}", "purchase_order": f"{p_po}{linked_po}", "amount_total": amt_tot, "amount_residual": amt_res, "payment_state": pay_st, "status": b_st})

        orders = []
        for po in pos:
            pid = int(po["id"])
            part_id = _m2o_id(po.get("partner_id"))
            if part_id is None:
                continue
            comp_id = _m2o_id(po.get("company_id")) or def_comp
            lines = pol_by_po.get(pid, [])
            ord_qty = round(sum(float(l.get("product_qty") or 0.0) for l in lines), 6)
            amt_tot = round(float(po.get("amount_total") or 0.0), 6)
            u_price = round(float(lines[0].get("price_unit") or 0.0), 6) if len(lines) == 1 else (round(amt_tot / ord_qty, 6) if ord_qty > 0 else 0.0)
            rst = str(po.get("state") or "draft")
            if rst == "cancel":
                po_st = "cancelled"
            elif rst in ("draft", "sent", "to approve"):
                po_st = "draft"
            else:
                bs, rs = bill_states_by_po.get(pid, []), rec_states_by_po.get(pid, [])
                po_st = "billed" if "posted" in bs else ("disputed" if "disputed" in rs else ("received" if ("completed" in rs or str(po.get("receipt_status") or "") == "full") else "confirmed"))
            orders.append({"po_id": f"{p_po}{pid}", "po_number": str(po.get("name") or ""), "supplier": f"{p_sup}{part_id}", "accounting_entity": f"{p_ent}{comp_id}", "ordered_qty": ord_qty, "unit_price": u_price, "amount_total": amt_tot, "status": po_st})

        return {"AccountingEntity": entities, "BusinessParty": parties, "P2pSupplierProfile": suppliers, "P2pPurchaseOrder": orders, "P2pGoodsReceipt": receipts, "P2pVendorBill": bills}

    def actuate(move_envelope: Mapping[str, Any]) -> None:
        action = str(move_envelope.get("action_type") or "")
        params = move_envelope.get("params") or {}
        try:
            if action == "confirm_p2p_order":
                po_id = _strip_prefix(params["po_id"], p_po)
                recs = client.call("purchase.order", "read", [[po_id], ["state"]])
                if recs and recs[0].get("state") in ("draft", "sent", "to approve"):
                    client.call("purchase.order", "button_confirm", [[po_id]])
            elif action == "record_p2p_receipt":
                pick_id = _strip_prefix(params["receipt_id"], p_gr)
                rec_qty, insp = float(params["received_qty"]), str(params.get("inspection_status") or "passed")
                recs = client.call("stock.picking", "read", [[pick_id], ["state", "move_ids"]])
                if not recs or recs[0].get("state") in ("done", "cancel"):
                    return
                mids = [int(m) for m in (recs[0].get("move_ids") or [])]
                moves = client.call("stock.move", "read", [mids, ["id", "product_uom_qty"]]) if mids else []
                exp_qty = sum(float(m.get("product_uom_qty") or 0.0) for m in moves)
                if rec_qty >= exp_qty and insp == "passed":
                    rem = rec_qty
                    for i, mv in enumerate(moves):
                        qty = rem if i == len(moves) - 1 else min(rem, float(mv.get("product_uom_qty") or 0.0))
                        rem = max(0.0, rem - qty)
                        client.call("stock.move", "write", [[int(mv["id"])], {"quantity": qty, "picked": True}])
                    client.call("stock.picking", "write", [[pick_id], {"note": False}])
                    res = client.call("stock.picking", "button_validate", [[pick_id]], {"context": {"skip_backorder": True, "skip_immediate": True, "skip_sms": True}})
                    if isinstance(res, dict) and res.get("res_model") and res.get("res_id"):
                        wm, wid = str(res["res_model"]), int(res["res_id"])
                        if "backorder" in wm:
                            client.call(wm, "process_cancel_backorder", [[wid]])
                        elif "immediate" in wm:
                            client.call(wm, "process", [[wid]])
                else:
                    if moves:
                        client.call("stock.move", "write", [[int(moves[0]["id"])], {"quantity": rec_qty, "picked": False}])
                    client.call("stock.picking", "write", [[pick_id], {"note": _encode_qc_note(rec_qty, insp)}])
            elif action == "post_p2p_bill":
                bill_id = _strip_prefix(params["bill_id"], p_bill)
                recs = client.call("account.move", "read", [[bill_id], ["state", "invoice_date"]])
                if not recs or recs[0].get("state") != "draft":
                    return
                if not recs[0].get("invoice_date"):
                    client.call("account.move", "write", [[bill_id], {"invoice_date": datetime.date.today().isoformat()}])
                client.call("account.move", "action_post", [[bill_id]])
            elif action == "record_p2p_payment":
                bill_id = _strip_prefix(params["bill_id"], p_bill)
                pay_amt = float(params["payment_amount"])
                recs = client.call("account.move", "read", [[bill_id], ["state", "company_id", "amount_residual"]])
                if not recs or recs[0].get("state") != "posted" or abs(float(recs[0].get("amount_residual") or 0.0)) <= 1e-6:
                    return
                cid = _m2o_id(recs[0].get("company_id"))
                j_dom: list[Any] = [["type", "in", ["bank", "cash"]]] + ([["company_id", "=", cid]] if cid is not None else [])
                journals = client.call("account.journal", "search_read", [j_dom], {"fields": ["id"], "limit": 1})
                ctx = {"active_model": "account.move", "active_ids": [bill_id]}
                vals: dict[str, Any] = {"amount": pay_amt, "payment_difference_handling": "open"}
                if journals:
                    vals["journal_id"] = int(journals[0]["id"])
                wid = client.call("account.payment.register", "create", [[vals]], {"context": ctx})
                wid = int(wid[0]) if isinstance(wid, list) else int(wid)
                client.call("account.payment.register", "action_create_payments", [[wid]], {"context": ctx})
        except _OdooRpcFault as exc:
            if any(tok in exc.fault_name.lower() for tok in _CONFLICT_TOKENS):
                return
            raise

    return SourceBridge(actuate=actuate, observe=observe)
