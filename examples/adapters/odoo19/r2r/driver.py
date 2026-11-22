from __future__ import annotations

import datetime
import json
import re
import urllib.request
from collections import defaultdict
from typing import Any, Mapping

from open_ontology.settlement import SourceBridge


class _OdooRpcError(RuntimeError):
    pass


class _OdooClient:
    def __init__(self, url: str, db: str, username: str, password: str) -> None:
        self._url, self._db, self._user, self._pwd = url, db, username, password
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._req_id = 0
        self._uid: int | None = None

    def _call(self, service: str, method: str, args: list[Any]) -> Any:
        self._req_id += 1
        data = json.dumps({
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": self._req_id,
        }).encode("utf-8")
        req = urllib.request.Request(self._url, data=data, headers={"Content-Type": "application/json"})
        with self._opener.open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("error"):
            raise _OdooRpcError(str(body["error"]))
        return body.get("result")

    def execute_kw(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        if self._uid is None:
            uid = self._call("common", "authenticate", [self._db, self._user, self._pwd, {}])
            if not uid:
                raise RuntimeError("Odoo authentication failed")
            self._uid = int(uid)
        return self._call("object", "execute_kw", [self._db, self._uid, self._pwd, model, method, args, kwargs or {}])


def _m2o_id(val: Any) -> int | None:
    if isinstance(val, (list, tuple)) and val:
        return int(val[0])
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def _m2o_name(val: Any, default: str = "") -> str:
    if isinstance(val, (list, tuple)) and len(val) >= 2 and isinstance(val[1], str):
        return val[1]
    return val if isinstance(val, str) else default


def _to_cents(val: Any) -> int:
    return 0 if val is None or isinstance(val, bool) else int(round(float(val) * 100))


def _classify_account(account_type: str) -> tuple[str, str]:
    at = (account_type or "").lower()
    if at.startswith("asset"):
        return "ASSET", "DEBIT"
    if at.startswith("liability"):
        return "LIABILITY", "CREDIT"
    if at.startswith("equity"):
        return "EQUITY", "CREDIT"
    if at.startswith("income"):
        return "REVENUE", "CREDIT"
    if at.startswith("expense"):
        return "EXPENSE", "DEBIT"
    return "ASSET", "DEBIT"


def _month_bounds(ym: str) -> tuple[str, str]:
    year, month = (int(x) for x in ym.split("-", 1))
    start = datetime.date(year, month, 1)
    next_m = datetime.date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    return start.isoformat(), (next_m - datetime.timedelta(days=1)).isoformat()


def _extract_prefixes(mapping: Any) -> dict[str, str]:
    prefixes = {"AccountingEntity": "ENT-", "FiscalPeriod": "FP-", "GlAccount": "ACC-", "JournalEntry": "JE-"}
    for em in getattr(mapping, "entity_mappings", []) or []:
        t = getattr(em, "target_object_type", None)
        it = getattr(em, "id_transform", None)
        pref = getattr(it, "prefix", None) if it is not None else None
        if t and isinstance(pref, str):
            prefixes[t] = pref
    return prefixes


def _parse_int_id(canonical_id: str, prefix: str) -> int | None:
    if not isinstance(canonical_id, str):
        return None
    raw = canonical_id[len(prefix):] if prefix and canonical_id.startswith(prefix) else canonical_id
    return int(raw) if raw.isdigit() else None


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: Any) -> SourceBridge:
    client = _OdooClient(
        url=str(endpoint_config["url"]),
        db=str(endpoint_config["db"]),
        username=str(endpoint_config["username"]),
        password=str(endpoint_config["password"]),
    )
    pref = _extract_prefixes(mapping)
    ent_p, fp_p, acc_p, je_p = pref["AccountingEntity"], pref["FiscalPeriod"], pref["GlAccount"], pref["JournalEntry"]

    def observe(move_envelope: Mapping[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        del move_envelope
        companies = client.execute_kw(
            "res.company", "search_read",
            [[["active", "=", True]], ["id", "name", "currency_id", "fiscalyear_lock_date", "hard_lock_date"]],
        )
        lock_dates: dict[int, str] = {}
        entities: list[dict[str, Any]] = []
        valid_cids: set[int] = set()
        default_cid: int | None = None
        for comp in sorted(companies, key=lambda r: int(r["id"])):
            cid = int(comp["id"])
            valid_cids.add(cid)
            default_cid = cid if default_cid is None else default_cid
            locks = [d for d in (comp.get("fiscalyear_lock_date"), comp.get("hard_lock_date")) if isinstance(d, str) and d]
            if locks:
                lock_dates[cid] = max(locks)
            entities.append({
                "entity_id": f"{ent_p}{cid}",
                "name": str(comp.get("name") or ""),
                "base_currency": _m2o_name(comp.get("currency_id"), "USD"),
                "status": "active",
            })

        accounts_raw = client.execute_kw(
            "account.account", "search_read",
            [[["active", "=", True]], ["id", "code", "name", "account_type", "active", "company_ids"]],
        )
        active_acc_ids = {int(a["id"]) for a in accounts_raw}
        posted_lines = client.execute_kw(
            "account.move.line", "search_read",
            [[["parent_state", "=", "posted"]], ["account_id", "debit", "credit"]],
        )
        debit_totals: dict[int, int] = defaultdict(int)
        credit_totals: dict[int, int] = defaultdict(int)
        for ln in posted_lines:
            aid = _m2o_id(ln.get("account_id"))
            if aid in active_acc_ids:
                debit_totals[aid] += _to_cents(ln.get("debit"))
                credit_totals[aid] += _to_cents(ln.get("credit"))

        gl_accounts: list[dict[str, Any]] = []
        for acc in sorted(accounts_raw, key=lambda r: int(r["id"])):
            aid = int(acc["id"])
            cids = acc.get("company_ids") or []
            cid = int(cids[0]) if cids else default_cid
            if cid not in valid_cids:
                continue
            category, normal_side = _classify_account(str(acc.get("account_type") or ""))
            gl_accounts.append({
                "account_code": f"{acc_p}{aid}",
                "accounting_entity": f"{ent_p}{cid}",
                "name": str(acc.get("name") or ""),
                "category": category,
                "normal_side": normal_side,
                "debit_total_cents": debit_totals.get(aid, 0),
                "credit_total_cents": credit_totals.get(aid, 0),
                "status": "active",
            })

        moves_raw = client.execute_kw(
            "account.move", "search_read",
            [[["move_type", "=", "entry"], ["state", "in", ["draft", "posted"]]],
             ["id", "name", "state", "date", "ref", "company_id", "reversed_entry_id"]],
        )
        move_ids = [int(m["id"]) for m in moves_raw]
        lines_by_move: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if move_ids:
            move_lines = client.execute_kw(
                "account.move.line", "search_read",
                [[["move_id", "in", move_ids]], ["id", "move_id", "account_id", "debit", "credit", "name", "display_type"]],
            )
            for ln in move_lines:
                if ln.get("display_type") in ("line_section", "line_note"):
                    continue
                mid = _m2o_id(ln.get("move_id"))
                if mid is not None:
                    lines_by_move[mid].append(ln)

        parsed_moves: list[dict[str, Any]] = []
        reversed_originals: set[int] = set()
        period_max_dates: dict[tuple[int, str], str] = {}
        for m in sorted(moves_raw, key=lambda r: int(r["id"])):
            mid, cid, mdate = int(m["id"]), _m2o_id(m.get("company_id")), str(m.get("date") or "")
            if cid not in valid_cids or len(mdate) < 7:
                continue
            lines = lines_by_move.get(mid, [])
            d_lines = [ln for ln in lines if _to_cents(ln.get("debit")) > 0]
            c_lines = [ln for ln in lines if _to_cents(ln.get("credit")) > 0]
            if len(d_lines) != 1 or len(c_lines) != 1:
                continue
            d_acc, c_acc = _m2o_id(d_lines[0].get("account_id")), _m2o_id(c_lines[0].get("account_id"))
            d_cents, c_cents = _to_cents(d_lines[0].get("debit")), _to_cents(c_lines[0].get("credit"))
            if d_acc not in active_acc_ids or c_acc not in active_acc_ids or d_acc == c_acc or d_cents <= 0 or d_cents != c_cents:
                continue
            orig_id = _m2o_id(m.get("reversed_entry_id"))
            if m.get("state") == "posted" and orig_id is not None:
                reversed_originals.add(orig_id)
            ym = mdate[:7]
            if (cid, ym) not in period_max_dates or mdate > period_max_dates[(cid, ym)]:
                period_max_dates[(cid, ym)] = mdate
            memo = m.get("ref") if isinstance(m.get("ref"), str) and m.get("ref") else str(d_lines[0].get("name") or m.get("name") or "")
            parsed_moves.append({
                "raw_id": mid, "cid": cid, "ym": ym, "d_acc": d_acc, "c_acc": c_acc,
                "amount_cents": d_cents, "memo": memo, "orig_id": orig_id, "raw_state": str(m.get("state") or "draft"),
            })

        fiscal_periods: list[dict[str, Any]] = []
        for (cid, ym), max_date in sorted(period_max_dates.items()):
            start_date, end_date = _month_bounds(ym)
            lock = lock_dates.get(cid)
            is_closed = bool(lock and (lock >= end_date or lock >= max_date))
            fiscal_periods.append({
                "period_id": f"{fp_p}{cid}-{ym}",
                "accounting_entity": f"{ent_p}{cid}",
                "start_date": start_date,
                "end_date": end_date,
                "status": "closed" if is_closed else "open",
            })

        journal_entries = [
            {
                "entry_id": f"{je_p}{pm['raw_id']}",
                "accounting_entity": f"{ent_p}{pm['cid']}",
                "fiscal_period": f"{fp_p}{pm['cid']}-{pm['ym']}",
                "debit_account": f"{acc_p}{pm['d_acc']}",
                "credit_account": f"{acc_p}{pm['c_acc']}",
                "amount_cents": pm["amount_cents"],
                "memo": pm["memo"],
                "reversal_of": f"{je_p}{pm['orig_id']}" if pm["orig_id"] is not None else "",
                "status": "reversed" if pm["raw_id"] in reversed_originals else pm["raw_state"],
            }
            for pm in parsed_moves
        ]
        return {
            "AccountingEntity": entities,
            "FiscalPeriod": fiscal_periods,
            "GlAccount": gl_accounts,
            "JournalEntry": journal_entries,
        }

    def actuate(move_envelope: Mapping[str, Any]) -> None:
        action = str(move_envelope.get("action_type") or "")
        params = move_envelope.get("params") or {}
        if action == "post_journal_entry":
            move_id = _parse_int_id(str(params.get("entry_id") or ""), je_p)
            if move_id is None:
                return
            rows = client.execute_kw("account.move", "search_read", [[["id", "=", move_id]], ["id", "state"]])
            if not rows or rows[0].get("state") != "draft":
                return
            try:
                client.execute_kw("account.move", "action_post", [[move_id]])
            except _OdooRpcError:
                return
            return

        if action == "reverse_journal_entry":
            move_id = _parse_int_id(str(params.get("entry_id") or ""), je_p)
            if move_id is None:
                return
            rows = client.execute_kw(
                "account.move", "search_read",
                [[["id", "=", move_id]], ["id", "state", "date", "journal_id", "company_id", "reversal_move_ids"]],
            )
            if not rows or rows[0].get("state") != "posted":
                return
            existing_revs = rows[0].get("reversal_move_ids") or []
            if existing_revs:
                posted_revs = client.execute_kw(
                    "account.move", "search_read", [[["id", "in", existing_revs], ["state", "=", "posted"]], ["id"]],
                )
                if posted_revs:
                    return
            journal_id, company_id = _m2o_id(rows[0].get("journal_id")), _m2o_id(rows[0].get("company_id"))
            rev_date = str(rows[0].get("date") or "")
            ym_match = re.search(r"(\d{4}-\d{2})$", str(params.get("target_period_id") or ""))
            if ym_match and rev_date[:7] != ym_match.group(1):
                rev_date = f"{ym_match.group(1)}-01"
            ctx = {"active_model": "account.move", "active_ids": [move_id]}
            vals: dict[str, Any] = {
                "move_ids": [(6, 0, [move_id])],
                "date": rev_date,
                "reason": str(params.get("memo") or "").strip() or "Reversal",
            }
            if journal_id is not None:
                vals["journal_id"] = journal_id
            if company_id is not None:
                vals["company_id"] = company_id
            try:
                wiz_id = client.execute_kw("account.move.reversal", "create", [vals], {"context": ctx})
                client.execute_kw("account.move.reversal", "reverse_moves", [[wiz_id]], {"context": ctx})
                wiz_rows = client.execute_kw("account.move.reversal", "read", [[wiz_id], ["new_move_ids"]])
                new_ids = (wiz_rows[0].get("new_move_ids") or []) if wiz_rows else []
                if new_ids:
                    draft_revs = client.execute_kw(
                        "account.move", "search_read", [[["id", "in", new_ids], ["state", "=", "draft"]], ["id"]],
                    )
                    draft_ids = [int(r["id"]) for r in draft_revs]
                    if draft_ids:
                        client.execute_kw("account.move", "action_post", [draft_ids])
            except _OdooRpcError:
                return

    return SourceBridge(actuate=actuate, observe=observe)
