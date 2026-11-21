"""Cross-Enterprise 5-Primitive REA Commonsense Exchange Bridge.

Two independent enterprises — a buyer running ``std_p2p_ontology`` and a seller
running ``std_o2c_ontology`` (optionally overlaid with ``std_crm_ontology`` and
enterprise rules) — exchange business state **exclusively** through pure JSON
envelopes containing the 5 ``std_core_ontology`` commonsense primitives:

1. ``BusinessParty``
2. ``TradeResource``
3. ``TradeCommitment``
4. ``FulfillmentEvent``
5. ``WorkCard``

Architectural isolation guarantees:
- ``P2pCommonsensePort`` has zero knowledge of seller-side ``O2c*`` / ``Crm*``
  types, actions, or error codes.
- ``O2cCommonsensePort`` has zero knowledge of buyer-side ``P2p*`` / ``Crm*``
  types, actions, or internal rules.
- ``CommonsenseEnvelope`` rejects any foreign fields or non-commonsense types
  on the wire.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any
import uuid

from open_ontology.runtime import OntologyRuntime
from open_ontology.spec import Actor, InvariantError

ALLOWED_COMMONSENSE_TYPES: tuple[str, ...] = (
    "BusinessParty",
    "TradeResource",
    "TradeCommitment",
    "FulfillmentEvent",
    "WorkCard",
)

PROTOCOL_ID: str = "open-ontology-rea-waist/v1"

_PARTY_FIELDS: frozenset[str] = frozenset(
    {"party_id", "legal_name", "tax_id", "country_code", "status"}
)
_RESOURCE_FIELDS: frozenset[str] = frozenset(
    {"resource_id", "code", "name", "unit", "spec_summary", "status"}
)
_COMMITMENT_FIELDS: frozenset[str] = frozenset(
    {
        "commitment_id",
        "buyer_party",
        "seller_party",
        "resource",
        "quantity",
        "unit_price",
        "total_amount",
        "status",
    }
)
_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        "event_id",
        "commitment",
        "event_kind",
        "quantity",
        "amount",
        "evidence_summary",
        "status",
    }
)
_WORK_CARD_FIELDS: frozenset[str] = frozenset(
    {
        "card_id",
        "card_kind",
        "target_object_type",
        "target_object_id",
        "requested_action",
        "requested_params_json",
        "requester_actor_id",
        "assignee_role",
        "assignee_dept",
        "status",
        "sla_due_at",
        "external_ref",
        "resolution_note",
        "claimed_by_actor_id",
        "resolved_by_actor_id",
    }
)

_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "protocol",
        "envelope_id",
        "sender_party_id",
        "receiver_party_id",
        "correlation_id",
        "message_kind",
        "parties",
        "resources",
        "commitments",
        "events",
        "work_cards",
        "summary",
    }
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _filter_fields(raw: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k in allowed}


def validate_commonsense_envelope(payload: dict[str, Any]) -> None:
    """Reject any non-whitelist keys, domain-private types, or non-commonsense payloads."""
    if not isinstance(payload, dict):
        raise ValueError("commonsense envelope must be a JSON object")
    extra_top = set(payload.keys()) - _ENVELOPE_KEYS
    if extra_top:
        raise ValueError(f"forbidden envelope keys outside 5-primitive waist: {sorted(extra_top)}")
    if payload.get("protocol") != PROTOCOL_ID:
        raise ValueError(f"unexpected protocol: {payload.get('protocol')!r}")

    buckets: list[tuple[str, list[Any], frozenset[str]]] = [
        ("parties", payload.get("parties") or [], _PARTY_FIELDS),
        ("resources", payload.get("resources") or [], _RESOURCE_FIELDS),
        ("commitments", payload.get("commitments") or [], _COMMITMENT_FIELDS),
        ("events", payload.get("events") or [], _EVENT_FIELDS),
        ("work_cards", payload.get("work_cards") or [], _WORK_CARD_FIELDS),
    ]
    for bname, rows, allowed in buckets:
        if not isinstance(rows, list):
            raise ValueError(f"envelope.{bname} must be a list")
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"envelope.{bname}[{idx}] must be an object")
            bad = set(row.keys()) - allowed
            if bad:
                raise ValueError(
                    f"envelope.{bname}[{idx}] leaks forbidden fields: {sorted(bad)}"
                )
            if bname == "work_cards":
                target_type = str(row.get("target_object_type") or "")
                if target_type and target_type not in ALLOWED_COMMONSENSE_TYPES:
                    raise ValueError(
                        f"WorkCard.target_object_type on wire must belong to {ALLOWED_COMMONSENSE_TYPES}, "
                        f"got {target_type!r}"
                    )


@dataclass
class CommonsenseEnvelope:
    """Strict JSON message carrier restricted to the 5 ``std_core`` commonsense primitives."""

    sender_party_id: str
    receiver_party_id: str
    correlation_id: str
    message_kind: str
    parties: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    commitments: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    work_cards: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    envelope_id: str = field(default_factory=lambda: _new_id("ENV"))
    protocol: str = PROTOCOL_ID

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        validate_commonsense_envelope(data)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CommonsenseEnvelope":
        validate_commonsense_envelope(payload)
        return cls(
            protocol=str(payload["protocol"]),
            envelope_id=str(payload["envelope_id"]),
            sender_party_id=str(payload["sender_party_id"]),
            receiver_party_id=str(payload["receiver_party_id"]),
            correlation_id=str(payload["correlation_id"]),
            message_kind=str(payload["message_kind"]),
            parties=[dict(x) for x in payload.get("parties") or []],
            resources=[dict(x) for x in payload.get("resources") or []],
            commitments=[dict(x) for x in payload.get("commitments") or []],
            events=[dict(x) for x in payload.get("events") or []],
            work_cards=[dict(x) for x in payload.get("work_cards") or []],
            summary=str(payload.get("summary") or ""),
        )

    @classmethod
    def from_json(cls, raw: str) -> "CommonsenseEnvelope":
        decoded = json.loads(raw)
        return cls.from_dict(decoded)


def _upsert_commonsense_records(
    runtime: OntologyRuntime,
    envelope: CommonsenseEnvelope,
    actor: Actor,
    *,
    preserve_work_cards: bool = False,
) -> dict[str, int]:
    """Ingest incoming commonsense primitives from a validated envelope into ``runtime``."""
    spec = runtime.spec
    counts: dict[str, int] = {
        "parties": 0,
        "resources": 0,
        "commitments": 0,
        "events": 0,
        "work_cards": 0,
    }
    type_pairs = [
        ("parties", "BusinessParty", envelope.parties, _PARTY_FIELDS),
        ("resources", "TradeResource", envelope.resources, _RESOURCE_FIELDS),
        ("commitments", "TradeCommitment", envelope.commitments, _COMMITMENT_FIELDS),
        ("events", "FulfillmentEvent", envelope.events, _EVENT_FIELDS),
    ]
    if not preserve_work_cards:
        type_pairs.append(("work_cards", "WorkCard", envelope.work_cards, _WORK_CARD_FIELDS))

    for key, tname, rows, allowed in type_pairs:
        if not rows or tname not in spec.object_types:
            continue
        cls = spec.get_object_type(tname)
        for row in rows:
            clean = _filter_fields(row, allowed)
            runtime.put_object(cls(**clean), actor=actor)
            counts[key] += 1
    return counts


class P2pCommonsensePort:
    """Buyer-side commonsense projection bridge over a ``std_p2p_ontology`` runtime.

    Strictly restricted to buyer ``std_p2p_ontology`` + ``std_core_ontology``
    primitives; holds zero references to any seller ontology concepts.
    """

    def __init__(
        self,
        runtime: OntologyRuntime,
        *,
        buyer_actor: Actor,
        warehouse_actor: Actor,
        procurement_manager_actor: Actor,
        finance_actor: Actor,
        sync_actor: Actor | None = None,
    ) -> None:
        self.runtime = runtime
        self.buyer_actor = buyer_actor
        self.warehouse_actor = warehouse_actor
        self.procurement_manager_actor = procurement_manager_actor
        self.finance_actor = finance_actor
        self.sync_actor = sync_actor or Actor(actor_id="p2p-bridge", role="admin", clearance_tags={"*"})

    def emit_purchase_commitment(
        self,
        *,
        po_id: str,
        resource_id: str,
        commitment_id: str,
        buyer_party_id: str,
        seller_party_id: str,
    ) -> CommonsenseEnvelope:
        """Project a buyer PO into a 5-primitive ``TradeCommitment(proposed)`` envelope."""
        po = self.runtime.get_object("P2pPurchaseOrder", po_id, self.buyer_actor)
        buyer_party = self.runtime.get_object("BusinessParty", buyer_party_id, self.buyer_actor)
        seller_party = self.runtime.get_object("BusinessParty", seller_party_id, self.buyer_actor)
        resource = self.runtime.get_object("TradeResource", resource_id, self.buyer_actor)

        cm_cls = self.runtime.spec.get_object_type("TradeCommitment")
        commitment = cm_cls(
            commitment_id=commitment_id,
            buyer_party=buyer_party_id,
            seller_party=seller_party_id,
            resource=resource_id,
            quantity=float(po.ordered_qty),
            unit_price=float(po.unit_price),
            total_amount=float(po.amount_total),
            status="proposed",
        )
        self.runtime.put_object(commitment, actor=self.sync_actor)

        return CommonsenseEnvelope(
            sender_party_id=buyer_party_id,
            receiver_party_id=seller_party_id,
            correlation_id=commitment_id,
            message_kind="COMMITMENT_PROPOSED",
            parties=[
                _filter_fields(buyer_party.model_dump(mode="json"), _PARTY_FIELDS),
                _filter_fields(seller_party.model_dump(mode="json"), _PARTY_FIELDS),
            ],
            resources=[_filter_fields(resource.model_dump(mode="json"), _RESOURCE_FIELDS)],
            commitments=[_filter_fields(commitment.model_dump(mode="json"), _COMMITMENT_FIELDS)],
            summary=f"Buyer proposes commitment {commitment_id} for PO {po.po_number}",
        )

    def ingest_seller_envelope(
        self,
        raw_or_env: CommonsenseEnvelope | str,
        *,
        preserve_work_cards: bool = False,
    ) -> CommonsenseEnvelope:
        """Validate and ingest seller commonsense response into buyer ABox."""
        env = CommonsenseEnvelope.from_json(raw_or_env) if isinstance(raw_or_env, str) else CommonsenseEnvelope.from_dict(raw_or_env.to_dict())
        _upsert_commonsense_records(
            self.runtime, env, self.sync_actor, preserve_work_cards=preserve_work_cards
        )
        return env

    def emit_receipt_and_dispute_envelope(
        self,
        *,
        receipt_id: str,
        commitment_id: str,
        buyer_party_id: str,
        seller_party_id: str,
        event_id: str,
        work_card_id: str | None = None,
        run_prefix: str = "P2P-EV",
    ) -> CommonsenseEnvelope:
        """Project buyer ``P2pGoodsReceipt`` into ``FulfillmentEvent(RECEIPT)`` (+ ``WorkCard`` if disputed)."""
        receipt = self.runtime.get_object("P2pGoodsReceipt", receipt_id, self.buyer_actor)
        po = self.runtime.get_object("P2pPurchaseOrder", receipt.purchase_order, self.buyer_actor)
        is_disputed = receipt.status == "disputed" or po.status == "disputed"

        ev_cls = self.runtime.spec.get_object_type("FulfillmentEvent")
        ev_amount = round(float(receipt.received_qty) * float(po.unit_price), 2)
        ev_status = "disputed" if is_disputed else "confirmed"
        ev = ev_cls(
            event_id=event_id,
            commitment=commitment_id,
            event_kind="RECEIPT",
            quantity=float(receipt.received_qty),
            amount=ev_amount,
            evidence_summary=(
                f"Weighbridge receipt expected={receipt.expected_qty}, "
                f"received={receipt.received_qty}, inspection={receipt.inspection_status}"
            ),
            status=ev_status,
        )
        self.runtime.put_object(ev, actor=self.sync_actor)

        work_cards_wire: list[dict[str, Any]] = []
        commitments_wire: list[dict[str, Any]] = []
        if is_disputed:
            cm = self.runtime.get_object("TradeCommitment", commitment_id, self.sync_actor)
            cm_cls = self.runtime.spec.get_object_type("TradeCommitment")
            cm_disputed = cm_cls(
                commitment_id=cm.commitment_id,
                buyer_party=cm.buyer_party,
                seller_party=cm.seller_party,
                resource=cm.resource,
                quantity=cm.quantity,
                unit_price=cm.unit_price,
                total_amount=cm.total_amount,
                status="disputed",
            )
            self.runtime.put_object(cm_disputed, actor=self.sync_actor)
            commitments_wire.append(
                _filter_fields(cm_disputed.model_dump(mode="json"), _COMMITMENT_FIELDS)
            )

            card_id = work_card_id or _new_id("WC-P2P")
            shortage = round(float(receipt.expected_qty) - float(receipt.received_qty), 4)
            self.runtime.submit_action(
                f"{run_prefix}-{card_id}",
                "open_work_card",
                {
                    "card_id": card_id,
                    "card_kind": "HUMAN_TASK",
                    "target_object_type": "P2pPurchaseOrder",
                    "target_object_id": po.po_id,
                    "requested_action": "resolve_p2p_dispute",
                    "requested_params_json": json.dumps(
                        {
                            "commitment_id": commitment_id,
                            "received_qty": float(receipt.received_qty),
                            "shortage_qty": shortage,
                            "proposed_settled_amount": ev_amount,
                        },
                        ensure_ascii=False,
                    ),
                    "assignee_role": "procurement_manager",
                    "external_ref": f"short_weight:{shortage}",
                },
                self.buyer_actor,
            )
            card = self.runtime.get_object("WorkCard", card_id, self.buyer_actor)
            wire_card = _filter_fields(card.model_dump(mode="json"), _WORK_CARD_FIELDS)
            # Redact internal P2pPurchaseOrder target to commonsense TradeCommitment on wire
            wire_card["requested_action"] = "reconcile_commitment_dispute"
            wire_card["target_object_type"] = "TradeCommitment"
            wire_card["target_object_id"] = commitment_id
            wire_card["external_ref"] = card.card_id
            work_cards_wire.append(wire_card)

        return CommonsenseEnvelope(
            sender_party_id=buyer_party_id,
            receiver_party_id=seller_party_id,
            correlation_id=commitment_id,
            message_kind="DISPUTE_RAISED" if is_disputed else "RECEIPT_EVENT",
            commitments=commitments_wire,
            events=[_filter_fields(ev.model_dump(mode="json"), _EVENT_FIELDS)],
            work_cards=work_cards_wire,
            summary="Buyer raised receipt quantity dispute" if is_disputed else "Buyer confirmed full receipt",
        )

    def apply_settled_dispute_from_seller(
        self,
        raw_or_env: CommonsenseEnvelope | str,
        *,
        po_id: str,
        receipt_id: str,
        local_work_card_id: str,
        run_prefix: str = "P2P-RES",
    ) -> dict[str, Any]:
        """Complete buyer ``WorkCard`` and invoke ``resolve_p2p_dispute`` once seller consensus arrives."""
        env = self.ingest_seller_envelope(raw_or_env, preserve_work_cards=True)
        if not env.commitments:
            raise ValueError("seller settlement envelope must carry updated TradeCommitment")
        settled_cm = env.commitments[0]
        settled_amount = float(settled_cm["total_amount"])

        card = self.runtime.get_object("WorkCard", local_work_card_id, self.procurement_manager_actor)
        if card.status == "OPEN":
            self.runtime.submit_action(
                f"{run_prefix}-CLAIM",
                "claim_work_card",
                {"card_id": local_work_card_id, "external_ref": env.envelope_id},
                self.procurement_manager_actor,
            )
        if self.runtime.get_object("WorkCard", local_work_card_id, self.procurement_manager_actor).status != "COMPLETED":
            self.runtime.submit_action(
                f"{run_prefix}-COMP",
                "complete_work_card",
                {
                    "card_id": local_work_card_id,
                    "resolution_note": f"Seller accepted deduction settlement {settled_amount}",
                    "external_ref": env.envelope_id,
                },
                self.procurement_manager_actor,
            )
        self.runtime.submit_action(
            f"{run_prefix}-PO",
            "resolve_p2p_dispute",
            {
                "po_id": po_id,
                "receipt_id": receipt_id,
                "work_card_id": local_work_card_id,
                "settled_amount": settled_amount,
            },
            self.procurement_manager_actor,
        )
        return {"settled_amount": settled_amount, "po_status": "received"}

    def emit_payment_event(
        self,
        *,
        bill_id: str,
        commitment_id: str,
        buyer_party_id: str,
        seller_party_id: str,
        event_id: str,
    ) -> CommonsenseEnvelope:
        """Project settled ``P2pVendorBill`` into ``FulfillmentEvent(PAYMENT)`` and ``TradeCommitment(fulfilled)``."""
        bill = self.runtime.get_object("P2pVendorBill", bill_id, self.finance_actor)
        if bill.payment_state != "paid":
            raise InvariantError(
                f"P2pVendorBill:{bill_id} is {bill.payment_state!r}; expected 'paid'",
                code="BILL_NOT_FULLY_PAID",
            )
        cm = self.runtime.get_object("TradeCommitment", commitment_id, self.sync_actor)
        cm_cls = self.runtime.spec.get_object_type("TradeCommitment")
        cm_done = cm_cls(
            commitment_id=cm.commitment_id,
            buyer_party=cm.buyer_party,
            seller_party=cm.seller_party,
            resource=cm.resource,
            quantity=cm.quantity,
            unit_price=cm.unit_price,
            total_amount=float(bill.amount_total),
            status="fulfilled",
        )
        self.runtime.put_object(cm_done, actor=self.sync_actor)

        ev_cls = self.runtime.spec.get_object_type("FulfillmentEvent")
        ev = ev_cls(
            event_id=event_id,
            commitment=commitment_id,
            event_kind="PAYMENT",
            quantity=float(cm.quantity),
            amount=float(bill.amount_total),
            evidence_summary=f"Settled vendor bill {bill.bill_id}",
            status="confirmed",
        )
        self.runtime.put_object(ev, actor=self.sync_actor)

        return CommonsenseEnvelope(
            sender_party_id=buyer_party_id,
            receiver_party_id=seller_party_id,
            correlation_id=commitment_id,
            message_kind="PAYMENT_EVENT",
            commitments=[_filter_fields(cm_done.model_dump(mode="json"), _COMMITMENT_FIELDS)],
            events=[_filter_fields(ev.model_dump(mode="json"), _EVENT_FIELDS)],
            summary=f"Buyer settled commitment {commitment_id} ({bill.amount_total})",
        )


class O2cCommonsensePort:
    """Seller-side commonsense projection bridge over an ``O2c*`` runtime.

    Strictly interacts through ``std_o2c_ontology`` + ``std_core_ontology``
    primitives and runtime error codes; holds zero knowledge of buyer internals
    or unrelated support/CRM schemas.
    """

    def __init__(
        self,
        runtime: OntologyRuntime,
        *,
        sales_actor: Actor,
        warehouse_actor: Actor,
        finance_actor: Actor,
        accounting_entity_id: str,
        seller_party_id: str,
        default_credit_limit: float = 1_000_000.0,
        sync_actor: Actor | None = None,
    ) -> None:
        self.runtime = runtime
        self.sales_actor = sales_actor
        self.warehouse_actor = warehouse_actor
        self.finance_actor = finance_actor
        self.accounting_entity_id = accounting_entity_id
        self.seller_party_id = seller_party_id
        self.default_credit_limit = float(default_credit_limit)
        self.sync_actor = sync_actor or Actor(actor_id="o2c-bridge", role="admin", clearance_tags={"*"})

    def _ensure_customer_profile(self, buyer_party_id: str, profile_id: str) -> str:
        prof_cls = self.runtime.spec.get_object_type("O2cCustomerProfile")
        existing = self.runtime.query_objects(
            "O2cCustomerProfile", self.sales_actor, {"party": buyer_party_id}
        )
        if existing:
            return existing[0].profile_id
        prof = prof_cls(
            profile_id=profile_id,
            party=buyer_party_id,
            accounting_entity=self.accounting_entity_id,
            credit_limit=self.default_credit_limit,
            payment_terms="NET30",
            status="active",
        )
        self.runtime.put_object(prof, actor=self.sync_actor)
        return profile_id

    def ingest_buyer_commitment(
        self,
        raw_or_env: CommonsenseEnvelope | str,
        *,
        order_id: str,
        delivery_id: str,
        profile_id: str | None = None,
        run_prefix: str = "O2C-IN",
    ) -> CommonsenseEnvelope:
        """Accept buyer ``TradeCommitment(proposed)``, materialize ``O2cOrder``, and attempt confirmation.

        If seller's domain runtime/overlay raises ``InvariantError``, holds order in draft
        and returns a commonsense ``TradeCommitment(disputed)`` + ``WorkCard`` without
        exposing any internal seller schema details on the wire.
        """
        env = (
            CommonsenseEnvelope.from_json(raw_or_env)
            if isinstance(raw_or_env, str)
            else CommonsenseEnvelope.from_dict(raw_or_env.to_dict())
        )
        _upsert_commonsense_records(self.runtime, env, self.sync_actor)
        if not env.commitments:
            raise ValueError("incoming buyer envelope missing TradeCommitment")

        cm_data = env.commitments[0]
        commitment_id = str(cm_data["commitment_id"])
        buyer_party_id = str(cm_data["buyer_party"])
        resolved_profile_id = self._ensure_customer_profile(
            buyer_party_id, profile_id or f"PROF-{buyer_party_id}"
        )

        ord_cls = self.runtime.spec.get_object_type("O2cOrder")
        ord_obj = ord_cls(
            order_id=order_id,
            order_number=f"SO-{commitment_id}",
            customer=resolved_profile_id,
            accounting_entity=self.accounting_entity_id,
            amount_total=float(cm_data["total_amount"]),
            status="draft",
        )
        self.runtime.put_object(ord_obj, actor=self.sync_actor)

        cm_cls = self.runtime.spec.get_object_type("TradeCommitment")
        try:
            self.runtime.submit_action(
                f"{run_prefix}-CONF-{order_id}",
                "confirm_o2c_order",
                {"order_id": order_id},
                self.sales_actor,
            )
            cm_agreed = cm_cls(
                commitment_id=commitment_id,
                buyer_party=buyer_party_id,
                seller_party=self.seller_party_id,
                resource=str(cm_data["resource"]),
                quantity=float(cm_data["quantity"]),
                unit_price=float(cm_data["unit_price"]),
                total_amount=float(cm_data["total_amount"]),
                status="agreed",
            )
            self.runtime.put_object(cm_agreed, actor=self.sync_actor)

            del_cls = self.runtime.spec.get_object_type("O2cDelivery")
            self.runtime.put_object(
                del_cls(delivery_id=delivery_id, order=order_id, status="pending"),
                actor=self.sync_actor,
            )
            return CommonsenseEnvelope(
                sender_party_id=self.seller_party_id,
                receiver_party_id=buyer_party_id,
                correlation_id=commitment_id,
                message_kind="COMMITMENT_AGREED",
                commitments=[_filter_fields(cm_agreed.model_dump(mode="json"), _COMMITMENT_FIELDS)],
                summary=f"Seller agreed commitment {commitment_id} (order {order_id} confirmed)",
            )
        except InvariantError as exc:
            cm_held = cm_cls(
                commitment_id=commitment_id,
                buyer_party=buyer_party_id,
                seller_party=self.seller_party_id,
                resource=str(cm_data["resource"]),
                quantity=float(cm_data["quantity"]),
                unit_price=float(cm_data["unit_price"]),
                total_amount=float(cm_data["total_amount"]),
                status="disputed",
            )
            self.runtime.put_object(cm_held, actor=self.sync_actor)

            hold_card_id = f"WC-HOLD-{commitment_id}"
            self.runtime.submit_action(
                f"{run_prefix}-HOLD-{order_id}",
                "open_work_card",
                {
                    "card_id": hold_card_id,
                    "card_kind": "HUMAN_TASK",
                    "target_object_type": "TradeCommitment",
                    "target_object_id": commitment_id,
                    "requested_action": "review_commitment_hold",
                    "requested_params_json": json.dumps(
                        {"commitment_id": commitment_id, "hold_code": exc.code},
                        ensure_ascii=False,
                    ),
                    "assignee_role": "sales_manager",
                    "external_ref": f"hold:{exc.code}",
                },
                self.sales_actor,
            )
            card = self.runtime.get_object("WorkCard", hold_card_id, self.sales_actor)
            return CommonsenseEnvelope(
                sender_party_id=self.seller_party_id,
                receiver_party_id=buyer_party_id,
                correlation_id=commitment_id,
                message_kind="COMMITMENT_HELD",
                commitments=[_filter_fields(cm_held.model_dump(mode="json"), _COMMITMENT_FIELDS)],
                work_cards=[_filter_fields(card.model_dump(mode="json"), _WORK_CARD_FIELDS)],
                summary=f"Seller placed commitment {commitment_id} on hold [{exc.code}]",
            )

    def retry_held_commitment(
        self,
        *,
        commitment_id: str,
        order_id: str,
        delivery_id: str,
        run_prefix: str = "O2C-RETRY",
    ) -> CommonsenseEnvelope:
        """Re-attempt confirmation after seller-internal hold condition is cleared."""
        cm = self.runtime.get_object("TradeCommitment", commitment_id, self.sync_actor)
        self.runtime.submit_action(
            f"{run_prefix}-CONF-{order_id}",
            "confirm_o2c_order",
            {"order_id": order_id},
            self.sales_actor,
        )
        cm_cls = self.runtime.spec.get_object_type("TradeCommitment")
        cm_agreed = cm_cls(
            commitment_id=cm.commitment_id,
            buyer_party=cm.buyer_party,
            seller_party=cm.seller_party,
            resource=cm.resource,
            quantity=cm.quantity,
            unit_price=cm.unit_price,
            total_amount=cm.total_amount,
            status="agreed",
        )
        self.runtime.put_object(cm_agreed, actor=self.sync_actor)
        del_cls = self.runtime.spec.get_object_type("O2cDelivery")
        self.runtime.put_object(
            del_cls(delivery_id=delivery_id, order=order_id, status="pending"),
            actor=self.sync_actor,
        )
        return CommonsenseEnvelope(
            sender_party_id=self.seller_party_id,
            receiver_party_id=cm.buyer_party,
            correlation_id=commitment_id,
            message_kind="COMMITMENT_AGREED",
            commitments=[_filter_fields(cm_agreed.model_dump(mode="json"), _COMMITMENT_FIELDS)],
            summary=f"Seller released hold and agreed commitment {commitment_id}",
        )

    def emit_shipment_event(
        self,
        *,
        delivery_id: str,
        order_id: str,
        commitment_id: str,
        event_id: str,
        shipped_qty: float,
        run_prefix: str = "O2C-SHIP",
    ) -> CommonsenseEnvelope:
        """Complete seller ``O2cDelivery`` and project ``FulfillmentEvent(SHIPMENT)``."""
        self.runtime.submit_action(
            f"{run_prefix}-{delivery_id}",
            "complete_o2c_delivery",
            {"delivery_id": delivery_id},
            self.warehouse_actor,
        )
        ord_obj = self.runtime.get_object("O2cOrder", order_id, self.warehouse_actor)
        cm = self.runtime.get_object("TradeCommitment", commitment_id, self.sync_actor)
        ev_cls = self.runtime.spec.get_object_type("FulfillmentEvent")
        ev = ev_cls(
            event_id=event_id,
            commitment=commitment_id,
            event_kind="SHIPMENT",
            quantity=float(shipped_qty),
            amount=float(ord_obj.amount_total),
            evidence_summary=f"Seller delivery {delivery_id} dispatched qty={shipped_qty}",
            status="confirmed",
        )
        self.runtime.put_object(ev, actor=self.sync_actor)
        return CommonsenseEnvelope(
            sender_party_id=self.seller_party_id,
            receiver_party_id=cm.buyer_party,
            correlation_id=commitment_id,
            message_kind="SHIPMENT_EVENT",
            events=[_filter_fields(ev.model_dump(mode="json"), _EVENT_FIELDS)],
            summary=f"Seller dispatched {shipped_qty} under {commitment_id}",
        )

    def emit_invoice_event(
        self,
        *,
        invoice_id: str,
        order_id: str,
        commitment_id: str,
        event_id: str,
        qty: float,
        amount: float,
        run_prefix: str = "O2C-INV",
    ) -> CommonsenseEnvelope:
        """Create and post seller ``O2cInvoice``, emitting ``FulfillmentEvent(INVOICE)``."""
        inv_cls = self.runtime.spec.get_object_type("O2cInvoice")
        self.runtime.put_object(
            inv_cls(
                invoice_id=invoice_id,
                order=order_id,
                amount_total=float(amount),
                amount_residual=float(amount),
                payment_state="not_paid",
                status="draft",
            ),
            actor=self.sync_actor,
        )
        self.runtime.submit_action(
            f"{run_prefix}-{invoice_id}",
            "post_o2c_invoice",
            {"invoice_id": invoice_id},
            self.finance_actor,
        )
        cm = self.runtime.get_object("TradeCommitment", commitment_id, self.sync_actor)
        ev_cls = self.runtime.spec.get_object_type("FulfillmentEvent")
        ev = ev_cls(
            event_id=event_id,
            commitment=commitment_id,
            event_kind="INVOICE",
            quantity=float(qty),
            amount=float(amount),
            evidence_summary=f"Seller posted invoice {invoice_id}",
            status="confirmed",
        )
        self.runtime.put_object(ev, actor=self.sync_actor)
        return CommonsenseEnvelope(
            sender_party_id=self.seller_party_id,
            receiver_party_id=cm.buyer_party,
            correlation_id=commitment_id,
            message_kind="INVOICE_EVENT",
            events=[_filter_fields(ev.model_dump(mode="json"), _EVENT_FIELDS)],
            summary=f"Seller posted invoice {invoice_id} for {amount}",
        )

    def settle_dispute_and_issue_invoice(
        self,
        raw_or_env: CommonsenseEnvelope | str,
        *,
        order_id: str,
        invoice_id: str,
        event_id: str,
        consensus_qty: float,
        unit_price: float,
        run_prefix: str = "O2C-SET",
    ) -> CommonsenseEnvelope:
        """Ingest buyer dispute envelope, accept weight/qty deduction, and issue adjusted invoice."""
        env = (
            CommonsenseEnvelope.from_json(raw_or_env)
            if isinstance(raw_or_env, str)
            else CommonsenseEnvelope.from_dict(raw_or_env.to_dict())
        )
        _upsert_commonsense_records(self.runtime, env, self.sync_actor, preserve_work_cards=True)
        cm = self.runtime.get_object("TradeCommitment", env.correlation_id, self.sync_actor)
        settled_amount = round(float(consensus_qty) * float(unit_price), 2)

        cm_cls = self.runtime.spec.get_object_type("TradeCommitment")
        cm_agreed = cm_cls(
            commitment_id=cm.commitment_id,
            buyer_party=cm.buyer_party,
            seller_party=cm.seller_party,
            resource=cm.resource,
            quantity=float(consensus_qty),
            unit_price=float(unit_price),
            total_amount=settled_amount,
            status="agreed",
        )
        self.runtime.put_object(cm_agreed, actor=self.sync_actor)

        inv_cls = self.runtime.spec.get_object_type("O2cInvoice")
        self.runtime.put_object(
            inv_cls(
                invoice_id=invoice_id,
                order=order_id,
                amount_total=settled_amount,
                amount_residual=settled_amount,
                payment_state="not_paid",
                status="draft",
            ),
            actor=self.sync_actor,
        )
        self.runtime.submit_action(
            f"{run_prefix}-INV-{invoice_id}",
            "post_o2c_invoice",
            {"invoice_id": invoice_id},
            self.finance_actor,
        )

        ev_cls = self.runtime.spec.get_object_type("FulfillmentEvent")
        ev = ev_cls(
            event_id=event_id,
            commitment=cm.commitment_id,
            event_kind="INVOICE",
            quantity=float(consensus_qty),
            amount=settled_amount,
            evidence_summary=f"Adjusted invoice {invoice_id} after deduction consensus",
            status="confirmed",
        )
        self.runtime.put_object(ev, actor=self.sync_actor)

        settled_cards: list[dict[str, Any]] = []
        for wc in env.work_cards:
            upd = dict(wc)
            upd["status"] = "COMPLETED"
            settled_cards.append(upd)

        return CommonsenseEnvelope(
            sender_party_id=self.seller_party_id,
            receiver_party_id=cm.buyer_party,
            correlation_id=cm.commitment_id,
            message_kind="DISPUTE_SETTLED",
            commitments=[_filter_fields(cm_agreed.model_dump(mode="json"), _COMMITMENT_FIELDS)],
            events=[_filter_fields(ev.model_dump(mode="json"), _EVENT_FIELDS)],
            work_cards=settled_cards,
            summary=f"Seller agreed deduction: qty={consensus_qty}, settled_amount={settled_amount}",
        )

    def ingest_payment_event(
        self,
        raw_or_env: CommonsenseEnvelope | str,
        *,
        invoice_id: str,
        run_prefix: str = "O2C-PAY",
    ) -> dict[str, Any]:
        """Ingest buyer ``FulfillmentEvent(PAYMENT)`` and post matching ``record_o2c_payment``."""
        env = (
            CommonsenseEnvelope.from_json(raw_or_env)
            if isinstance(raw_or_env, str)
            else CommonsenseEnvelope.from_dict(raw_or_env.to_dict())
        )
        _upsert_commonsense_records(self.runtime, env, self.sync_actor)
        pay_events = [e for e in env.events if e.get("event_kind") == "PAYMENT"]
        if not pay_events:
            raise ValueError("payment envelope must include a PAYMENT FulfillmentEvent")
        paid_amount = float(pay_events[0]["amount"])
        self.runtime.submit_action(
            f"{run_prefix}-{invoice_id}",
            "record_o2c_payment",
            {"invoice_id": invoice_id, "payment_amount": paid_amount},
            self.finance_actor,
        )
        inv = self.runtime.get_object("O2cInvoice", invoice_id, self.finance_actor)
        return {
            "invoice_id": inv.invoice_id,
            "payment_state": inv.payment_state,
            "amount_residual": inv.amount_residual,
        }


__all__ = [
    "ALLOWED_COMMONSENSE_TYPES",
    "CommonsenseEnvelope",
    "O2cCommonsensePort",
    "P2pCommonsensePort",
    "PROTOCOL_ID",
    "validate_commonsense_envelope",
]
