"""Thin HTTP JSON-RPC transport driver for the O2C ERP device.

Connection details come from ``endpoint_config`` at runtime and canonical id
prefixes / source model names come from the declarative ``AdapterMappingSpec``.
No tenant, database, record, partner, document or amount literal is embedded:
every value is read from the live system and ids are derived from the mapping.
"""
from __future__ import annotations
import json
import urllib.request
from typing import Any, Mapping

from open_ontology.settlement import SourceBridge

_TIMEOUT = 60
_ENTITY = "AccountingEntity"
_PARTY = "BusinessParty"
_PROFILE = "O2cCustomerProfile"
_ORDER = "O2cOrder"
_DELIVERY = "O2cDelivery"
_INVOICE = "O2cInvoice"
_MODELS = {
    _ENTITY: "res.company", _PARTY: "res.partner", _PROFILE: "res.partner",
    _ORDER: "sale.order", _DELIVERY: "stock.picking", _INVOICE: "account.move",
}
_MOVE_MODEL = "stock.move"
_PAYMENT_WIZARD = "account.payment.register"


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_id(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None or value is False or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
def _as_text(value: Any) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)):
        name = value[1] if len(value) > 1 else ""
        return str(name) if name else ""
    return str(value)

class _JsonRpc:
    """Minimal stateless JSON-RPC 2.0 client (one short session per operation)."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.url = str(_field(config, "url") or "")
        self.db = str(_field(config, "db") or "")
        self.username = str(_field(config, "username") or "")
        self.password = str(_field(config, "password") or "")
        self.uid: Any = None

    def _post(self, service: str, method: str, args: list) -> Any:
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1,
                   "params": {"service": service, "method": method, "args": args}}
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
        error = body.get("error")
        if error:
            detail = error.get("data") if isinstance(error, Mapping) else None
            message = str(detail.get("message") or "") if isinstance(detail, Mapping) else ""
            raise RuntimeError(message or str(error.get("message") or "RPC_ERROR"))
        return body.get("result")

    def login(self) -> Any:
        if self.uid is None:
            self.uid = self._post("common", "login",
                                  [self.db, self.username, self.password])
        return self.uid

    def call(self, model: str, method: str, args: list | None = None,
             kwargs: Mapping[str, Any] | None = None) -> Any:
        uid = self.login()
        return self._post("object", "execute_kw",
                          [self.db, uid, self.password, model, method,
                           list(args or []), dict(kwargs or {})])

class _O2cProjector:
    """Live read-only projection plus governed actuation of the O2C ERP device."""

    def __init__(self, endpoint_config: Mapping[str, Any], mapping: Any) -> None:
        self.config = dict(endpoint_config)
        self.models = dict(_MODELS)
        self.prefixes: dict[str, str] = {}
        for entity_mapping in _field(mapping, "entity_mappings", []) or []:
            target = str(_field(entity_mapping, "target_object_type") or "")
            table = str(_field(entity_mapping, "source_table") or "")
            if target and table:
                self.models[target] = table
            id_transform = _field(entity_mapping, "id_transform", None)
            prefix = "" if id_transform is None else str(_field(id_transform, "prefix", "") or "")
            if target and prefix:
                self.prefixes[target] = prefix

    def canon(self, object_type: str, record_id: Any) -> str:
        return "%s%s" % (self.prefixes.get(object_type, ""), record_id)

    def parse(self, object_type: str, value: Any) -> int:
        text = str(value or "").strip()
        prefix = self.prefixes.get(object_type, "")
        if prefix and text.startswith(prefix):
            text = text[len(prefix):]
        try:
            return int(text)
        except (TypeError, ValueError):
            digits = "".join(ch for ch in text if ch.isdigit())
            return int(digits) if digits else 0

    def _read(self, rpc: _JsonRpc, object_type: str, domain: list,
              fields: list[str]) -> list[dict[str, Any]]:
        rows = rpc.call(self.models[object_type], "search_read", [domain],
                        {"fields": fields})
        return [dict(row) for row in (rows or []) if isinstance(row, Mapping)]

    def observe(self) -> dict[str, list[dict[str, Any]]]:
        rpc = _JsonRpc(self.config)
        companies = self._read(rpc, _ENTITY, [], ["name", "currency_id"])
        orders = self._read(rpc, _ORDER, [], [
            "name", "partner_id", "amount_total", "state", "company_id",
            "picking_ids", "invoice_ids"])

        company_ids = sorted({cid for cid in (_as_id(r.get("id")) for r in companies) if cid})
        default_company = company_ids[0] if company_ids else None
        entities = []
        for row in companies:
            cid = _as_id(row.get("id"))
            if cid is None:
                continue
            entities.append({
                "entity_id": self.canon(_ENTITY, cid),
                "name": _as_text(row.get("name")) or self.canon(_ENTITY, cid),
                "base_currency": _as_text(row.get("currency_id")),
                "status": "active"})

        order_rows: dict[int, dict[str, Any]] = {}
        partner_company: dict[int, Any] = {}
        partner_ids: set[int] = set()
        picking_ids: set[int] = set()
        invoice_ids: set[int] = set()
        for row in orders:
            oid, partner_id = _as_id(row.get("id")), _as_id(row.get("partner_id"))
            company_id = _as_id(row.get("company_id")) or default_company
            if oid is None or partner_id is None or company_id is None:
                continue
            order_rows[oid] = row
            partner_ids.add(partner_id)
            partner_company.setdefault(partner_id, company_id)
            for raw in row.get("picking_ids") or []:
                if _as_id(raw):
                    picking_ids.add(_as_id(raw))
            for raw in row.get("invoice_ids") or []:
                if _as_id(raw):
                    invoice_ids.add(_as_id(raw))

        picking_rows: dict[int, dict[str, Any]] = {}
        if picking_ids:
            for row in self._read(rpc, _DELIVERY, [["id", "in", sorted(picking_ids)]], ["state"]):
                pid = _as_id(row.get("id"))
                if pid is not None:
                    picking_rows[pid] = row

        invoice_rows: dict[int, dict[str, Any]] = {}
        if invoice_ids:
            for row in self._read(rpc, _INVOICE, [["id", "in", sorted(invoice_ids)]],
                                  ["state", "move_type", "amount_total",
                                   "amount_residual", "payment_state"]):
                mid = _as_id(row.get("id"))
                if mid is not None and str(row.get("move_type") or "") == "out_invoice":
                    invoice_rows[mid] = row

        partner_rows: dict[int, dict[str, Any]] = {}
        if partner_ids:
            for row in self._read(rpc, _PARTY, [["id", "in", sorted(partner_ids)]],
                                  ["name", "vat", "active", "credit_limit",
                                   "property_payment_term_id"]):
                pid = _as_id(row.get("id"))
                if pid is not None:
                    partner_rows[pid] = row

        parties, profiles = [], []
        for pid in sorted(partner_rows):
            row = partner_rows[pid]
            parties.append({
                "party_id": self.canon(_PARTY, pid),
                "legal_name": _as_text(row.get("name")) or self.canon(_PARTY, pid),
                "tax_id": _as_text(row.get("vat")),
                "status": "inactive" if row.get("active") is False else "active"})
            company_id = partner_company.get(pid) or default_company
            if company_id is None:
                continue
            profiles.append({
                "profile_id": self.canon(_PROFILE, pid),
                "credit_limit": _as_float(row.get("credit_limit")),
                "payment_terms": _as_text(row.get("property_payment_term_id")) or "NET30",
                "status": "active",
                "party": self.canon(_PARTY, pid),
                "accounting_entity": self.canon(_ENTITY, company_id)})

        deliveries, invoices = [], []
        posted_orders: set[int] = set()
        done_orders: set[int] = set()
        for oid in sorted(order_rows):
            for raw in order_rows[oid].get("picking_ids") or []:
                pid, picking = _as_id(raw), picking_rows.get(_as_id(raw))
                if picking is None:
                    continue
                state = str(picking.get("state") or "")
                status = "completed" if state == "done" else ("cancelled" if state == "cancel" else "pending")
                if status == "completed":
                    done_orders.add(oid)
                deliveries.append({"delivery_id": self.canon(_DELIVERY, pid),
                                   "status": status,
                                   "order": self.canon(_ORDER, oid)})
            for raw in order_rows[oid].get("invoice_ids") or []:
                mid, invoice = _as_id(raw), invoice_rows.get(_as_id(raw))
                if invoice is None:
                    continue
                state = str(invoice.get("state") or "")
                if state == "posted":
                    posted_orders.add(oid)
                    status = "posted"
                elif state == "cancel":
                    status = "cancelled"
                else:
                    status = "draft"
                total = _as_float(invoice.get("amount_total"))
                residual = _as_float(invoice.get("amount_residual"))
                if state != "posted":
                    payment_state = "not_paid"
                elif residual <= 1e-09:
                    payment_state = "paid"
                elif residual < total:
                    payment_state = "partial"
                elif str(invoice.get("payment_state") or "") in ("paid", "in_payment"):
                    payment_state = "paid"
                else:
                    payment_state = "not_paid"
                invoices.append({"invoice_id": self.canon(_INVOICE, mid),
                                 "amount_total": total,
                                 "amount_residual": residual,
                                 "payment_state": payment_state,
                                 "status": status,
                                 "order": self.canon(_ORDER, oid)})

        order_projections = []
        for oid in sorted(order_rows):
            row = order_rows[oid]
            state = str(row.get("state") or "")
            if state == "cancel":
                status = "cancelled"
            elif oid in posted_orders:
                status = "invoiced"
            elif oid in done_orders:
                status = "delivered"
            elif state in ("sale", "done"):
                status = "confirmed"
            else:
                status = "draft"
            company_id = _as_id(row.get("company_id")) or default_company
            order_projections.append({
                "order_id": self.canon(_ORDER, oid),
                "order_number": _as_text(row.get("name")) or self.canon(_ORDER, oid),
                "amount_total": _as_float(row.get("amount_total")),
                "status": status,
                "customer": self.canon(_PROFILE, _as_id(row.get("partner_id"))),
                "accounting_entity": self.canon(_ENTITY, company_id)})

        return {_ENTITY: entities, _PARTY: parties, _PROFILE: profiles,
                _ORDER: order_projections, _DELIVERY: deliveries,
                _INVOICE: invoices}

    def actuate(self, move_envelope: Mapping[str, Any]) -> None:
        action = str(_field(move_envelope, "action_type") or "")
        params = dict(_field(move_envelope, "params", {}) or {})
        try:
            rpc = _JsonRpc(self.config)
            if action == "confirm_o2c_order":
                order_id = self.parse(_ORDER, params.get("order_id"))
                if order_id:
                    rpc.call(self.models[_ORDER], "action_confirm", [[order_id]])
            elif action == "complete_o2c_delivery":
                self._complete_delivery(rpc, self.parse(_DELIVERY, params.get("delivery_id")))
            elif action == "post_o2c_invoice":
                invoice_id = self.parse(_INVOICE, params.get("invoice_id"))
                if invoice_id:
                    rpc.call(self.models[_INVOICE], "action_post", [[invoice_id]])
            elif action == "record_o2c_payment":
                self._record_payment(rpc, self.parse(_INVOICE, params.get("invoice_id")),
                                     _as_float(params.get("payment_amount")))
        except Exception:
            # Out-of-band drift (cancelled / already settled record) surfaces as an
            # expected business fault here; swallow it so the following independent
            # observe() reports physical truth and settlement rejects with zero ABox writes.
            return None
        return None

    def _complete_delivery(self, rpc: _JsonRpc, delivery_id: int) -> None:
        if not delivery_id:
            return
        moves = rpc.call(_MOVE_MODEL, "search_read",
                         [[["picking_id", "=", delivery_id]]],
                         {"fields": ["product_uom_qty", "quantity"]})
        for move in moves or []:
            demanded = _as_float(move.get("product_uom_qty"))
            if _as_float(move.get("quantity")) < demanded:
                rpc.call(_MOVE_MODEL, "write", [[move.get("id")], {"quantity": demanded}])
        rpc.call(self.models[_DELIVERY], "button_validate", [[delivery_id]])

    def _record_payment(self, rpc: _JsonRpc, invoice_id: int, amount: float) -> None:
        if not invoice_id or amount <= 0:
            return
        invoice_model = self.models[_INVOICE]
        context = {"active_model": invoice_model, "active_ids": [invoice_id],
                   "active_id": invoice_id}
        wizard = rpc.call(_PAYMENT_WIZARD, "create", [{"amount": amount}],
                          {"context": context})
        wizard_id = wizard[0] if isinstance(wizard, (list, tuple)) and wizard else wizard
        rpc.call(_PAYMENT_WIZARD, "action_create_payments", [[wizard_id]],
                 {"context": context})


def build_device_bridge(endpoint_config: Mapping[str, Any], mapping: Any) -> SourceBridge:
    projector = _O2cProjector(endpoint_config, mapping)

    def actuate(move_envelope: Mapping[str, Any]) -> Any:
        return projector.actuate(move_envelope)

    def observe(move_envelope: Mapping[str, Any] | None = None) -> Any:
        return projector.observe()

    return SourceBridge(actuate=actuate, observe=observe)