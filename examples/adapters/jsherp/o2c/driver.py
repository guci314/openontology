from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from open_ontology.settlement import SourceBridge


def _build_prefix_map(mapping: Any) -> dict[str, str]:
    prefixes = {
        "AccountingEntity": "ENT-",
        "BusinessParty": "PTY-",
        "O2cCustomerProfile": "CUST-",
        "O2cOrder": "ORD-",
        "O2cDelivery": "DEL-",
        "O2cInvoice": "INV-",
    }
    for em in getattr(mapping, "entity_mappings", []) or []:
        obj_type = getattr(em, "target_object_type", None)
        id_tf = getattr(em, "id_transform", None)
        pref = getattr(id_tf, "prefix", None) if id_tf else None
        if obj_type and pref:
            prefixes[str(obj_type)] = str(pref)
    return prefixes


def _strip_prefix(val: Any, prefix: str) -> int:
    s = str(val or "").strip()
    if prefix and s.startswith(prefix):
        s = s[len(prefix) :]
    elif "-" in s:
        s = s.split("-", 1)[1]
    return int(s)


class _JshRestClient:
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.login_url = str(cfg.get("login_url") or f"{self.base_url}/user/login")
        self.username = str(cfg["username"])
        pwd_md5 = cfg.get("password_md5") or hashlib.md5(
            str(cfg.get("password") or "").encode("utf-8")
        ).hexdigest()
        self.password_md5 = str(pwd_md5)
        self.token_header = str(cfg.get("token_header") or "X-Access-Token")
        self._token: str | None = None
        self._last_write_ts: float = 0.0

    def _login(self) -> str:
        payload = json.dumps({"loginName": self.username, "password": self.password_md5}).encode("utf-8")
        req = urllib.request.Request(
            self.login_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
        token = (body.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"Login failed: {body}")
        self._token = str(token)
        return self._token

    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        is_write: bool = False,
    ) -> dict[str, Any]:
        if is_write:
            elapsed = time.monotonic() - self._last_write_ts
            if elapsed < 1.05:
                time.sleep(1.05 - elapsed)
        if not self._token:
            self._login()
        for attempt in range(2):
            url = f"{self.base_url}{path}"
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
            headers = {"Content-Type": "application/json", self.token_header: str(self._token)}
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8", "ignore")
                    res = json.loads(raw) if raw else {}
                if is_write:
                    self._last_write_ts = time.monotonic()
                if isinstance(res, dict) and res.get("code") in (301, 401) and attempt == 0:
                    self._login()
                    continue
                return res if isinstance(res, dict) else {"code": 200, "data": res}
            except urllib.error.HTTPError as exc:
                if is_write:
                    self._last_write_ts = time.monotonic()
                if exc.code in (401, 403, 500) and attempt == 0:
                    self._login()
                    if is_write:
                        time.sleep(1.05)
                    continue
                return {"code": exc.code, "error": exc.read().decode("utf-8", "ignore")}
        return {"code": 500, "error": "max_retries_exceeded"}


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: Any) -> SourceBridge:
    client = _JshRestClient(endpoint_config)
    prefixes = _build_prefix_map(mapping)
    ent_pref, pty_pref = prefixes["AccountingEntity"], prefixes["BusinessParty"]
    cust_pref, ord_pref = prefixes["O2cCustomerProfile"], prefixes["O2cOrder"]
    del_pref, inv_pref = prefixes["O2cDelivery"], prefixes["O2cInvoice"]

    def _rows(path: str, search: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        s = json.dumps(search or {}, ensure_ascii=False)
        res = client.request("GET", path, {"currentPage": 1, "pageSize": 200, "search": s})
        return ((res.get("data") or {}) if isinstance(res, dict) else {}).get("rows") or []

    def observe(move_envelope: Mapping[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        _ = move_envelope
        sys_res = client.request("GET", "/systemConfig/getCurrentInfo")
        sys_info = (sys_res.get("data") or {}) if isinstance(sys_res, dict) else {}
        if not sys_info or not sys_info.get("id"):
            rows = _rows("/systemConfig/list")
            sys_info = rows[0] if rows else {"id": 1, "companyName": "Default"}

        comp_id = f"{ent_pref}{sys_info['id']}"
        comp_st = "inactive" if str(sys_info.get("deleteFlag", "0")) == "1" else "active"
        accounting_entities = [
            {"entity_id": comp_id, "name": str(sys_info.get("companyName") or comp_id), "base_currency": "CNY", "status": comp_st}
        ]

        parties, profiles, valid_cust_ids = [], [], set()
        for row in sorted(_rows("/supplier/list"), key=lambda r: int(r.get("id", 0))):
            if str(row.get("deleteFlag", "0")) == "1" or str(row.get("type") or "") != "客户":
                continue
            raw_id = int(row["id"])
            valid_cust_ids.add(raw_id)
            p_id, c_id = f"{pty_pref}{raw_id}", f"{cust_pref}{raw_id}"
            active = "active" if bool(row.get("enabled", True)) else "inactive"
            parties.append({"party_id": p_id, "legal_name": str(row.get("supplier") or p_id), "tax_id": str(row.get("taxNum") or ""), "country_code": "CN", "status": active})
            profiles.append({"profile_id": c_id, "credit_limit": round(max(0.0, float(row.get("advanceIn") or 0.0)), 2), "payment_terms": str(row.get("description") or "NET30"), "status": active, "party": p_id, "accounting_entity": comp_id})

        so_rows = _rows("/depotHead/list", {"type": "其它", "subType": "销售订单"})
        ck_rows = _rows("/depotHead/list", {"type": "出库", "subType": "销售"})
        ak_rows = _rows("/accountHead/list")

        so_by_num: dict[str, dict[str, Any]] = {}
        for row in sorted(so_rows, key=lambda r: int(r.get("id", 0))):
            if str(row.get("deleteFlag", "0")) == "1" or row.get("organId") is None or int(row["organId"]) not in valid_cust_ids:
                continue
            num = str(row.get("number") or row.get("defaultNumber") or "").strip()
            if num:
                so_by_num[num] = row

        deliveries, ck_by_num, completed_so_nums = [], {}, set()
        for row in sorted(ck_rows, key=lambda r: int(r.get("id", 0))):
            if str(row.get("deleteFlag", "0")) == "1":
                continue
            link_num = str(row.get("linkNumber") or "").strip()
            if not link_num or link_num not in so_by_num:
                continue
            parent_so = so_by_num[link_num]
            raw_id = int(row["id"])
            ck_num = str(row.get("number") or row.get("defaultNumber") or "").strip()
            if ck_num:
                ck_by_num[ck_num] = row
            raw_st = str(row.get("status") or "0")
            if raw_st == "1":
                del_status = "completed"
                completed_so_nums.add(link_num)
            elif raw_st == "9":
                del_status = "cancelled"
            else:
                del_status = "pending"
            deliveries.append({"delivery_id": f"{del_pref}{raw_id}", "status": del_status, "order": f"{ord_pref}{int(parent_so['id'])}"})

        invoices, invoiced_so_nums = [], set()
        for row in sorted(ak_rows, key=lambda r: int(r.get("id", 0))):
            if str(row.get("deleteFlag", "0")) == "1" or str(row.get("type") or "") != "收款":
                continue
            raw_id = int(row["id"])
            item_rows = ((client.request("GET", "/accountItem/getDetailList", {"headerId": raw_id}).get("data") or {}).get("rows") or [])
            linked_so, item_each_sum, item_need_sum = None, 0.0, 0.0
            for it in item_rows:
                b_num = str(it.get("billNumber") or "").strip()
                item_each_sum += float(it.get("eachAmount") or 0.0)
                item_need_sum += float(it.get("needDebt") or 0.0)
                if b_num in ck_by_num:
                    so_num = str(ck_by_num[b_num].get("linkNumber") or "").strip()
                    if so_num in so_by_num:
                        linked_so = so_by_num[so_num]
                elif b_num in so_by_num:
                    linked_so = so_by_num[b_num]
            if linked_so is None:
                continue

            so_num = str(linked_so.get("number") or linked_so.get("defaultNumber") or "").strip()
            raw_total = float(row.get("totalPrice") or 0.0) or item_need_sum or float(linked_so.get("totalPrice") or 0.0)
            amount_total = round(max(0.0, raw_total), 2)
            collected = max(abs(float(row.get("changeAmount") or 0.0)), abs(item_each_sum))
            amount_residual = round(max(0.0, amount_total - collected), 2)
            if amount_residual <= 1e-9 and amount_total > 0.0:
                amount_residual, payment_state = 0.0, "paid"
            elif collected > 1e-9:
                payment_state = "partial"
            else:
                payment_state = "not_paid"

            raw_st = str(row.get("status") or "0")
            if raw_st == "1":
                inv_status = "posted"
                if so_num:
                    invoiced_so_nums.add(so_num)
            elif raw_st == "9":
                inv_status = "cancelled"
            else:
                inv_status = "draft"
            invoices.append({"invoice_id": f"{inv_pref}{raw_id}", "amount_total": amount_total, "amount_residual": amount_residual, "payment_state": payment_state, "status": inv_status, "order": f"{ord_pref}{int(linked_so['id'])}"})

        orders = []
        for so_num, row in sorted(so_by_num.items(), key=lambda kv: int(kv[1].get("id", 0))):
            raw_id, organ_id = int(row["id"]), int(row["organId"])
            raw_st = str(row.get("status") or "0")
            if raw_st == "9":
                ord_status = "cancelled"
            elif raw_st == "0":
                ord_status = "draft"
            elif so_num in invoiced_so_nums:
                ord_status = "invoiced"
            elif so_num in completed_so_nums:
                ord_status = "delivered"
            else:
                ord_status = "confirmed"
            orders.append({"order_id": f"{ord_pref}{raw_id}", "order_number": so_num, "amount_total": round(max(0.0, float(row.get("totalPrice") or 0.0)), 2), "status": ord_status, "customer": f"{cust_pref}{organ_id}", "accounting_entity": comp_id})

        return {
            "AccountingEntity": accounting_entities,
            "BusinessParty": parties,
            "O2cCustomerProfile": profiles,
            "O2cOrder": orders,
            "O2cDelivery": deliveries,
            "O2cInvoice": invoices,
        }

    def actuate(move_envelope: Mapping[str, Any]) -> None:
        action = str(move_envelope.get("action_type") or "")
        params = move_envelope.get("params") or {}
        if action in ("confirm_o2c_order", "complete_o2c_delivery"):
            pref = ord_pref if action == "confirm_o2c_order" else del_pref
            key = "order_id" if action == "confirm_o2c_order" else "delivery_id"
            raw_id = _strip_prefix(params.get(key), pref)
            info = ((client.request("GET", "/depotHead/info", {"id": raw_id}).get("data") or {}).get("info") or {})
            if not info or str(info.get("deleteFlag", "0")) == "1" or str(info.get("status")) != "0":
                return
            client.request("POST", "/depotHead/batchSetStatus", json_body={"status": "1", "ids": str(raw_id)}, is_write=True)
        elif action == "post_o2c_invoice":
            raw_id = _strip_prefix(params.get("invoice_id"), inv_pref)
            info = ((client.request("GET", "/accountHead/info", {"id": raw_id}).get("data") or {}).get("info") or {})
            if not info or str(info.get("deleteFlag", "0")) == "1" or str(info.get("status")) != "0":
                return
            client.request("POST", "/accountHead/batchSetStatus", json_body={"status": "1", "ids": str(raw_id)}, is_write=True)
        elif action == "record_o2c_payment":
            raw_id = _strip_prefix(params.get("invoice_id"), inv_pref)
            pay_amt = float(params.get("payment_amount") or 0.0)
            info = ((client.request("GET", "/accountHead/info", {"id": raw_id}).get("data") or {}).get("info") or {})
            if not info or str(info.get("deleteFlag", "0")) == "1" or str(info.get("status")) != "1":
                return
            item_rows = ((client.request("GET", "/accountItem/getDetailList", {"headerId": raw_id}).get("data") or {}).get("rows") or [])
            cur_hdr = abs(float(info.get("changeAmount") or 0.0))
            cur_line = abs(sum(float(r.get("eachAmount") or 0.0) for r in item_rows))
            cur_collected = max(cur_hdr, cur_line)
            residual = max(0.0, float(info.get("totalPrice") or 0.0) - cur_collected)
            if pay_amt <= 0.0 or pay_amt > residual + 1e-6:
                return
            new_collected = round(cur_collected + pay_amt, 2)
            info["changeAmount"] = new_collected
            if item_rows:
                item_rows[0]["eachAmount"] = new_collected
            client.request("PUT", "/accountHead/updateAccountHeadAndDetail", json_body={"info": json.dumps(info, ensure_ascii=False), "rows": json.dumps(item_rows, ensure_ascii=False)}, is_write=True)

    return SourceBridge(actuate=actuate, observe=observe)
