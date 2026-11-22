from __future__ import annotations
import datetime, hashlib, json, math, re, time, urllib.error, urllib.parse, urllib.request
from typing import Any, Mapping
from open_ontology.settlement import SourceBridge

_RM = re.compile(r"OO-RCPT\|([-+0-9.eE]+)\|([A-Za-z_]+)")
_EP = 1e-9


def _a(o: Any, n: str, d: Any = None) -> Any:
    return o.get(n, d) if isinstance(o, dict) else getattr(o, n, d)


def _f(v: Any, d: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return d
    return x if math.isfinite(x) else d


class _C:
    def __init__(self, c: Mapping[str, Any]) -> None:
        self.base = str(c.get("base_url") or "").rstrip("/")
        self.login_url = str(c.get("login_url") or self.base + "/user/login")
        self.user = str(c.get("username") or "")
        s = c.get("password_md5") or hashlib.md5(str(c.get("password") or "").encode()).hexdigest()
        self.secret = str(s)
        self.header = str(c.get("token_header") or "X-Access-Token")
        self.token: str | None = None
        self.last = 0.0

    def _req(self, m: str, url: str, body: Any = None) -> Any:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h[self.header] = self.token
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=m), timeout=90) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}

    def login(self) -> None:
        out = self._req("POST", self.login_url, {"loginName": self.user, "password": self.secret})
        d = out.get("data") if isinstance(out, dict) else None
        tok = d.get("token") if isinstance(d, dict) else None
        if not tok:
            raise RuntimeError("auth")
        self.token = str(tok)

    def call(self, m: str, path: str, params: Mapping[str, Any] | None = None,
             body: Any = None, write: bool = False, retry: bool = True) -> Any:
        if write:
            w = 1.05 - (time.time() - self.last)
            if w > 0:
                time.sleep(w)
            self.last = time.time()
        if not self.token:
            self.login()
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            out = self._req(m, url, body)
        except urllib.error.HTTPError as e:
            if retry and e.code in (401, 403, 500):
                self.login()
                return self.call(m, path, params, body, False, False)
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {}
        except (urllib.error.URLError, TimeoutError):
            if retry:
                self.login()
                return self.call(m, path, params, body, False, False)
            return {}
        if isinstance(out, dict) and out.get("code") == 401 and retry:
            self.login()
            return self.call(m, path, params, body, False, False)
        return out


def _rows(out: Any) -> list[dict[str, Any]]:
    d = out.get("data") if isinstance(out, dict) else None
    r = d.get("rows") if isinstance(d, dict) else None
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: Any) -> SourceBridge:
    c = _C(endpoint_config)
    pfx = {str(_a(e, "target_object_type")): str(_a(_a(e, "id_transform"), "prefix", "") or "")
           for e in (_a(mapping, "entity_mappings", []) or [])}
    binds = {str(_a(b, "action_name")): b for b in (_a(mapping, "action_bindings", []) or [])}

    def P(t: str, fb: str) -> str:
        return pfx.get(t) or fb

    def BP(a: str, t: str, fb: str) -> str:
        return str(_a(binds.get(a), "id_prefix", "") or "") or P(t, fb)

    def rid(v: Any, p: str) -> str:
        s = str(v)
        return s[len(p):] if p and s.startswith(p) else s

    def sr(**kw: Any) -> str:
        d = {"number": "", "beginTime": "", "endTime": "", "type": "", "subType": "",
             "hasDebt": "", "remark": "", "currentPage": 1, "pageSize": 1000}
        d.update(kw)
        return json.dumps(d, ensure_ascii=False)

    def heads(st: str) -> list[dict[str, Any]]:
        return _rows(c.call("GET", "/depotHead/list", {"search": sr(subType=st)}))

    def items(hid: Any) -> list[dict[str, Any]]:
        return _rows(c.call("GET", "/depotItem/getDetailList", {"headerId": str(hid)}))

    def dinfo(hid: Any) -> dict[str, Any]:
        o = c.call("GET", "/depotHead/info", {"id": str(hid)})
        d = o.get("data") if isinstance(o, dict) else None
        i = d.get("info") if isinstance(d, dict) else None
        return i if isinstance(i, dict) else {}

    def mark(v: Any) -> tuple[float, str] | None:
        m = _RM.search(str(v or ""))
        return (_f(m.group(1)), m.group(2).lower()) if m else None

    def observe(move_envelope: Any = None) -> dict[str, list[dict[str, Any]]]:
        cur = c.call("GET", "/systemConfig/getCurrentInfo")
        info = cur.get("data") if isinstance(cur, dict) else None
        info = info if isinstance(info, dict) else {}
        tid = info.get("tenantId")
        eid = P("AccountingEntity", "ENT-") + str(tid)
        ents = [{"entity_id": eid, "name": str(info.get("companyName") or ""),
                 "base_currency": "CNY", "status": "active"}] if tid is not None else []
        parties, profiles = [], []
        for r in _rows(c.call("GET", "/supplier/list", {"search": "{}"})):
            if str(r.get("type") or "") != "供应商" or str(r.get("deleteFlag", "0")) != "0" or r.get("id") is None:
                continue
            pid = P("BusinessParty", "PTY-") + str(r.get("id"))
            st = "active" if bool(r.get("enabled")) else "inactive"
            parties.append({"party_id": pid, "legal_name": str(r.get("supplier") or ""),
                            "tax_id": str(r.get("taxNum") or ""), "country_code": "CN", "status": st})
            profiles.append({"profile_id": P("P2pSupplierProfile", "SUP-") + str(r.get("id")),
                             "payment_terms": str(r.get("description") or "NET30"),
                             "status": st, "party": pid, "accounting_entity": eid})
        pos = [r for r in heads("采购订单") if str(r.get("type")) == "其它"
               and str(r.get("subType")) == "采购订单" and str(r.get("deleteFlag", "0")) == "0"]
        rhs = [r for r in heads("采购") if str(r.get("type")) == "入库"
               and str(r.get("subType")) == "采购" and str(r.get("deleteFlag", "0")) == "0"]
        pnum: dict[str, dict[str, Any]] = {}
        pitems: dict[Any, list[dict[str, Any]]] = {}
        for r in pos:
            pnum[str(r.get("number") or "")] = r
            pitems[r.get("id")] = items(r.get("id"))
        receipts, bills, bnum = [], [], {}
        for r in rhs:
            po = pnum.get(str(r.get("linkNumber") or ""))
            if po is None:
                continue
            it = items(r.get("id"))
            exp = sum(_f(i.get("preNumber")) for i in it)
            raw = str(r.get("status") or "0")
            mk = mark(r.get("remark"))
            if raw == "1":
                st, got, insp = "completed", sum(_f(i.get("operNumber")) for i in it), (mk[1] if mk else "passed")
            elif mk is not None:
                got, insp = mk
                st = "completed" if (got + _EP >= exp and insp == "passed") else "disputed"
            else:
                st, got, insp = "pending", 0.0, "pending"
            poid = P("P2pPurchaseOrder", "PO-") + str(po.get("id"))
            num = str(r.get("number") or "")
            bnum[str(r.get("id"))] = num
            receipts.append({"receipt_id": P("P2pGoodsReceipt", "GR-") + str(r.get("id")),
                             "expected_qty": exp, "received_qty": got, "inspection_status": insp,
                             "status": st, "purchase_order": poid})
            bst = "posted" if raw == "1" else ("cancelled" if raw == "2" else "draft")
            bills.append({"bill_id": P("P2pVendorBill", "BILL-") + str(r.get("id")),
                          "amount_total": _f(r.get("totalPrice")),
                          "amount_residual": _f(r.get("totalPrice")),
                          "payment_state": "not_paid", "status": bst, "purchase_order": poid})
        paid: dict[str, float] = {}
        pays = _rows(c.call("GET", "/accountHead/list", {"search": json.dumps(
            {"type": "付款", "number": "", "beginTime": "", "endTime": "",
             "currentPage": 1, "pageSize": 1000}, ensure_ascii=False)}))
        for pay in pays:
            if str(pay.get("type")) != "付款" or str(pay.get("status")) != "1":
                continue
            it = _rows(c.call("GET", "/accountItem/getDetailList", {"headerId": str(pay.get("id"))}))
            tot = abs(_f(pay.get("totalPrice")))
            sh = [max(0.0, _f(i.get("eachAmount"))) for i in it]
            nd = [max(0.0, _f(i.get("needDebt"))) for i in it]
            if sum(sh) <= _EP and tot > _EP:
                base = sum(nd) if sum(nd) > _EP else float(len(it) or 1)
                sh = [tot * (n / base) if sum(nd) > _EP else tot / float(len(it) or 1) for n in nd]
            for i, s in zip(it, sh):
                ref = str(i.get("billNumber") or "")
                if ref:
                    paid[ref] = paid.get(ref, 0.0) + s
        for b in bills:
            num = bnum.get(b["bill_id"].split("-", 1)[-1], "")
            tot = b["amount_total"]
            setl = min(tot, paid.get(num, 0.0))
            res = max(0.0, tot - setl)
            b["amount_residual"] = res
            b["payment_state"] = "not_paid" if setl <= _EP else ("paid" if res <= max(_EP, 1e-6 * max(tot, 1.0)) else "partial")
        orders = []
        for r in pos:
            poid = P("P2pPurchaseOrder", "PO-") + str(r.get("id"))
            rs = [x["status"] for x in receipts if x["purchase_order"] == poid]
            bs = [x["status"] for x in bills if x["purchase_order"] == poid]
            raw = str(r.get("status") or "0")
            if "disputed" in rs and "posted" not in bs:
                st = "disputed"
            elif "posted" in bs:
                st = "billed"
            elif "completed" in rs:
                st = "received"
            elif raw in ("1", "2"):
                st = "confirmed"
            elif raw in ("5", "6"):
                st = "disputed" if raw == "5" else "cancelled"
            else:
                st = "draft"
            q = sum(_f(i.get("operNumber")) for i in pitems.get(r.get("id"), []))
            tot = _f(r.get("totalPrice"))
            orders.append({"po_id": poid, "po_number": str(r.get("number") or ""),
                           "ordered_qty": q, "unit_price": (tot / q if q > _EP else 0.0),
                           "amount_total": tot, "status": st,
                           "supplier": P("P2pSupplierProfile", "SUP-") + str(r.get("organId")),
                           "accounting_entity": eid})
        return {"AccountingEntity": ents, "BusinessParty": parties, "P2pSupplierProfile": profiles,
                "P2pPurchaseOrder": orders, "P2pGoodsReceipt": receipts, "P2pVendorBill": bills}

    def po_num(number: str) -> dict[str, Any] | None:
        return next((r for r in heads("采购订单") if str(r.get("number") or "") == number), None) if number else None

    def put_receipt(raw: str, got: float, insp: str) -> None:
        doc, it = dinfo(raw), items(raw)
        po = po_num(str(doc.get("linkNumber") or ""))
        link = {}
        for i in (items(po.get("id")) if po else []):
            link[str(i.get("materialExtendId"))] = i.get("id")
            link[str(i.get("barCode"))] = i.get("id")
        exp = sum(_f(i.get("preNumber")) for i in it)
        left, out = max(0.0, got), []
        for k, i in enumerate(it):
            z = dict(i)
            m = link.get(str(i.get("materialExtendId"))) or link.get(str(i.get("barCode")))
            if m is not None:
                z["linkId"] = m
            q = left if k == len(it) - 1 else (max(0.0, got) * _f(i.get("preNumber")) / exp if exp > _EP else 0.0)
            if k != len(it) - 1:
                left -= q
            z["operNumber"] = max(0.0, q)
            out.append(z)
        head = dict(doc)
        head["remark"] = "OO-RCPT|%s|%s" % (repr(float(got)), insp)
        c.call("PUT", "/depotHead/updateDepotHeadAndDetail", body={
            "id": int(raw) if str(raw).isdigit() else raw,
            "info": json.dumps(head, ensure_ascii=False),
            "rows": json.dumps(out, ensure_ascii=False),
            "preTotalPrice": _f(doc.get("totalPrice"))}, write=True)

    def pay_list() -> list[dict[str, Any]]:
        return _rows(c.call("GET", "/accountHead/list", {"search": json.dumps(
            {"type": "付款", "number": "", "beginTime": "", "endTime": "",
             "currentPage": 1, "pageSize": 1000}, ensure_ascii=False)}))

    def actuate(move_envelope: Mapping[str, Any]) -> dict[str, Any]:
        act = str(move_envelope.get("action_type") or "")
        p = move_envelope.get("params") or {}
        try:
            if act == "confirm_p2p_order":
                r = rid(p.get("po_id"), BP(act, "P2pPurchaseOrder", "PO-"))
                c.call("POST", "/depotHead/batchSetStatus", body={"ids": str(r), "status": "1"}, write=True)
            elif act == "record_p2p_receipt":
                r = rid(p.get("receipt_id"), BP(act, "P2pGoodsReceipt", "GR-"))
                insp = str(p.get("inspection_status") or "passed").lower()
                put_receipt(str(r), max(0.0, _f(p.get("received_qty"))), insp if insp in ("passed", "failed") else "passed")
            elif act == "post_p2p_bill":
                r = rid(p.get("bill_id"), BP(act, "P2pVendorBill", "BILL-"))
                c.call("POST", "/depotHead/batchSetStatus", body={"ids": str(r), "status": "1"}, write=True)
            elif act == "record_p2p_payment":
                r = rid(p.get("bill_id"), BP(act, "P2pVendorBill", "BILL-"))
                amt = max(0.0, _f(p.get("payment_amount")))
                doc = dinfo(r)
                aid = doc.get("accountId")
                if aid is None:
                    ac = _rows(c.call("GET", "/account/list", {"search": "{}"}))
                    aid = ac[0].get("id") if ac else None
                sq = c.call("GET", "/sequence/buildNumber", write=True)
                sd = sq.get("data") if isinstance(sq, dict) else None
                serial = str((sd or {}).get("defaultNumber") or int(time.time()))
                no = "FK" + serial
                head = {"type": "付款", "organId": doc.get("organId"), "accountId": aid, "billNo": no,
                        "billTime": datetime.date.today().isoformat(), "changeAmount": 0.0,
                        "totalPrice": -amt, "discountMoney": 0.0, "status": "0", "source": "0",
                        "remark": "OO-PAY|" + serial}
                it = {"accountId": aid, "billNumber": str(doc.get("number") or ""),
                      "needDebt": _f(doc.get("totalPrice")), "eachAmount": 0.0, "remark": "OO-PAY|" + serial}
                c.call("POST", "/accountHead/addAccountHeadAndDetail", body={
                    "info": json.dumps(head, ensure_ascii=False),
                    "rows": json.dumps([it], ensure_ascii=False)}, write=True)
                fid = next((x.get("id") for x in pay_list() if str(x.get("billNo") or "") == no), None)
                if fid is not None:
                    c.call("POST", "/accountHead/batchSetStatus", body={"ids": str(fid), "status": "1"}, write=True)
            return {"ok": True, "action": act}
        except Exception as e:
            return {"ok": False, "action": act, "error": type(e).__name__}

    return SourceBridge(actuate=actuate, observe=observe)