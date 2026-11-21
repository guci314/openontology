"""Enterprise WeChat / Feishu org-tree sync and HMAC-signed WorkCard approval bridge.

Application-layer M1/integration bridge demonstrating how domestic enterprise
collaboration directories map cleanly onto OpenOntology's pure M2 primitives:
- Parses WeCom (企业微信) and Feishu (飞书) department & user payloads into ``OrgDirectory``.
- Builds an org-scoped P2P ontology where ``P2pPurchaseOrder.__security_tag_fields__ = ("org",)``
  natively enforces plant-level RLS via ``OrgDirectory.resolve_actor``.
- Executes dynamic tiered amount routing (AUTO vs M1 ``open_work_card``), renders
  Feishu Interactive Cards & WeCom template cards, verifies HMAC-SHA256 callbacks with
  timestamp + anti-replay ``nonce`` guards, enforces org coverage / SoD / tier rank,
  and re-runs live $t_1$ ``simulate_action`` before atomic settlement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Sequence
import uuid

from open_ontology import Actor, InvariantError, OntologyRuntime, OntologySpec, PermissionDeniedError
from open_ontology.induction import (
    CustomerOverlaySpec,
    FieldSpec,
    apply_overlay,
    compile_blueprint,
    compose_blueprints,
)
from open_ontology.org_authority import (
    AuthorityEvaluation,
    OrgDirectory,
    OrgMember,
    OrgUnit,
    TieredAuthorityPolicy,
    evaluate_action_authority,
    verify_approver_authority,
)

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.std_core_ontology import build_std_core_blueprint
from examples.std_p2p_ontology import build_std_p2p_blueprint

__all__ = [
    "CALLBACK_SIGNATURE_INVALID",
    "CALLBACK_TIMESTAMP_EXPIRED",
    "CALLBACK_REPLAY_DETECTED",
    "T1_PHANTOM_READ_BLOCKED",
    "CallbackSecurityError",
    "scope_value_from_tag",
    "build_org_scoped_p2p_spec",
    "sync_wecom_org_directory",
    "sync_feishu_org_directory",
    "render_feishu_work_card",
    "render_wecom_work_card",
    "sign_callback",
    "CnOrgWorkCardBridge",
]

CALLBACK_SIGNATURE_INVALID = "CALLBACK_SIGNATURE_INVALID"
CALLBACK_TIMESTAMP_EXPIRED = "CALLBACK_TIMESTAMP_EXPIRED"
CALLBACK_REPLAY_DETECTED = "CALLBACK_REPLAY_DETECTED"
T1_PHANTOM_READ_BLOCKED = "T1_PHANTOM_READ_BLOCKED"


class CallbackSecurityError(PermissionDeniedError):
    """Raised when callback signature, timestamp window, or replay checks fail."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = str(code)
        super().__init__(message)


def scope_value_from_tag(scope_tag: str) -> str:
    """Convert an ``org:<value>`` clearance tag into the raw ``org`` field value."""
    tag = str(scope_tag or "").strip()
    return tag[4:] if tag.startswith("org:") else tag


def build_org_scoped_p2p_spec(*, model_id: str = "cn_org_scoped_p2p") -> OntologySpec:
    """Compile ``std_core + std_p2p`` with ``P2pPurchaseOrder.__security_tag_fields__ = ('org',)``."""
    base = compose_blueprints(
        [build_std_core_blueprint(), build_std_p2p_blueprint()],
        model_id=model_id,
        version="1.0.0",
    )
    overlay = CustomerOverlaySpec(
        overlay_id=f"{model_id}_org_rls_overlay",
        base_model_id=base.model_id,
        target_version="1.1.0",
        add_fields={
            "P2pPurchaseOrder": [
                FieldSpec(
                    name="org",
                    field_type="str",
                    default="",
                    description="所属厂区/部门组织安全标签值（与 OrgUnit.scope_tag 对应）",
                )
            ]
        },
    )
    compiled = compile_blueprint(apply_overlay(base, overlay), seed_ontology=BUSINESS_SEED_ONTOLOGY)
    po_cls = compiled.get_object_type("P2pPurchaseOrder")
    po_cls.__security_tag_fields__ = ("org",)
    return compiled


def _default_scope_tag(unit_id: str, overrides: Mapping[str, str] | None) -> str:
    if overrides and unit_id in overrides:
        raw = str(overrides[unit_id]).strip()
        return raw if raw.startswith("org:") else f"org:{raw}"
    clean = unit_id[5:] if unit_id.startswith("dept-") else unit_id
    return f"org:dept-{clean}"


def _resolve_mapped_role(
    raw_position: str,
    role_mapping: Mapping[str, str],
    default_role: str,
) -> str:
    pos = str(raw_position or "").strip()
    if pos in role_mapping:
        return str(role_mapping[pos])
    for key, mapped in role_mapping.items():
        if key and key in pos:
            return str(mapped)
    return default_role


def sync_wecom_org_directory(
    departments_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    users_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    role_mapping: Mapping[str, str],
    scope_tag_overrides: Mapping[str, str] | None = None,
    token_hashes: Mapping[str, str] | None = None,
    default_role: str = "buyer",
) -> OrgDirectory:
    """Parse standard Enterprise WeChat (企微) department & user payloads into ``OrgDirectory``."""
    raw_depts = (
        departments_payload.get("department", [])
        if isinstance(departments_payload, Mapping)
        else departments_payload
    )
    raw_users = (
        users_payload.get("userlist", [])
        if isinstance(users_payload, Mapping)
        else users_payload
    )

    dept_ids = {str(d["id"]) for d in raw_depts}
    units: list[OrgUnit] = []
    for dept in raw_depts:
        uid = str(dept["id"])
        raw_parent = dept.get("parentid")
        parent_str = None if raw_parent is None else str(raw_parent).strip()
        if parent_str in ("", "0") and parent_str not in dept_ids:
            parent_str = None
        if uid == parent_str:
            pass  # keep intentional self-cycle so directory.validate() catches ORG_CYCLE_DETECTED
        units.append(
            OrgUnit(
                unit_id=uid,
                name=str(dept.get("name") or uid),
                parent_id=parent_str,
                unit_kind=str(dept.get("unit_kind") or ("group" if parent_str is None else "department")),
                scope_tag=_default_scope_tag(uid, scope_tag_overrides),
                metadata={
                    "platform": "wecom",
                    "order": dept.get("order"),
                },
            )
        )

    members: list[OrgMember] = []
    for user in raw_users:
        user_id = str(user["userid"])
        dept_list = user.get("department") or []
        if not isinstance(dept_list, (list, tuple)):
            dept_list = [dept_list]
        primary_unit = str(dept_list[0]) if dept_list else ""
        position = str(user.get("position") or user.get("title") or "")
        extra_tags = [
            _default_scope_tag(str(extra_u), scope_tag_overrides)
            for extra_u in dept_list[1:]
        ]
        extra_tags.extend(str(t) for t in (user.get("extra_clearance_tags") or []) if t)
        active = int(user.get("status", 1)) == 1
        tok_hash = (
            (token_hashes or {}).get(user_id)
            or str(user.get("auth_token_hash") or "")
        )
        members.append(
            OrgMember(
                user_id=user_id,
                display_name=str(user.get("name") or user_id),
                role=_resolve_mapped_role(position, role_mapping, default_role),
                unit_id=primary_unit,
                title=position,
                extra_clearance_tags=extra_tags,
                active=active,
                auth_token_hash=tok_hash,
            )
        )

    directory = OrgDirectory(units=units, participants=members)
    directory.validate()
    return directory


def _unwrap_feishu_items(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        if "data" in payload and isinstance(payload["data"], Mapping):
            return list(payload["data"].get("items") or ())
        return list(payload.get("items") or ())
    return list(payload)


def _feishu_user_is_active(status_val: Any) -> bool:
    if isinstance(status_val, Mapping):
        if status_val.get("is_resigned") or status_val.get("is_frozen") or status_val.get("is_exited"):
            return False
        if "is_activated" in status_val:
            return bool(status_val["is_activated"])
        return True
    if isinstance(status_val, bool):
        return status_val
    if isinstance(status_val, int):
        return status_val == 1
    if isinstance(status_val, str):
        return status_val.strip().lower() in ("1", "true", "active", "activated")
    return True


def sync_feishu_org_directory(
    departments_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    users_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    role_mapping: Mapping[str, str],
    scope_tag_overrides: Mapping[str, str] | None = None,
    token_hashes: Mapping[str, str] | None = None,
    default_role: str = "buyer",
) -> OrgDirectory:
    """Parse standard Feishu (飞书) Contact v3 department & user payloads into ``OrgDirectory``."""
    raw_depts = _unwrap_feishu_items(departments_payload)
    raw_users = _unwrap_feishu_items(users_payload)

    dept_ids = {str(d.get("open_department_id") or d.get("department_id")) for d in raw_depts}
    units: list[OrgUnit] = []
    for dept in raw_depts:
        uid = str(dept.get("open_department_id") or dept.get("department_id"))
        raw_parent = dept.get("parent_department_id")
        parent_str = None if raw_parent is None else str(raw_parent).strip()
        if parent_str in ("", "0") and parent_str not in dept_ids:
            parent_str = None
        units.append(
            OrgUnit(
                unit_id=uid,
                name=str(dept.get("name") or uid),
                parent_id=parent_str,
                unit_kind=str(dept.get("unit_kind") or ("group" if parent_str is None else "department")),
                scope_tag=_default_scope_tag(uid, scope_tag_overrides),
                metadata={"platform": "feishu"},
            )
        )

    members: list[OrgMember] = []
    for user in raw_users:
        user_id = str(user.get("open_id") or user.get("user_id"))
        dept_list = user.get("department_ids") or user.get("department_id") or []
        if not isinstance(dept_list, (list, tuple)):
            dept_list = [dept_list]
        primary_unit = str(dept_list[0]) if dept_list else ""
        job_title = str(user.get("job_title") or user.get("position") or "")
        extra_tags = [
            _default_scope_tag(str(extra_u), scope_tag_overrides)
            for extra_u in dept_list[1:]
        ]
        extra_tags.extend(str(t) for t in (user.get("extra_clearance_tags") or []) if t)
        tok_hash = (
            (token_hashes or {}).get(user_id)
            or str(user.get("auth_token_hash") or "")
        )
        members.append(
            OrgMember(
                user_id=user_id,
                display_name=str(user.get("name") or user_id),
                role=_resolve_mapped_role(job_title, role_mapping, default_role),
                unit_id=primary_unit,
                title=job_title,
                extra_clearance_tags=extra_tags,
                active=_feishu_user_is_active(user.get("status", True)),
                auth_token_hash=tok_hash,
            )
        )

    directory = OrgDirectory(units=units, participants=members)
    directory.validate()
    return directory


def render_feishu_work_card(
    *,
    card_id: str,
    title: str,
    summary_fields: Sequence[tuple[str, str]],
    target_unit_id: str,
    required_role: str,
    eligible_approver_ids: Sequence[str],
    tier_id: str,
) -> dict[str, Any]:
    """Render a standard Feishu Interactive Card (飞书交互卡片 schema 2.0 JSON)."""
    field_elements = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
        for k, v in summary_fields
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": [
                {"tag": "div", "fields": field_elements},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": (
                                f"工单 {card_id} | 档位 {tier_id} | 目标组织 {target_unit_id} | "
                                f"审批角色 {required_role} | 路由人 {', '.join(eligible_approver_ids)}"
                            ),
                        }
                    ],
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "同意并核验重算提交"},
                            "value": {
                                "card_id": card_id,
                                "decision": "APPROVE",
                                "target_unit_id": target_unit_id,
                                "tier_id": tier_id,
                            },
                        },
                        {
                            "tag": "button",
                            "type": "danger",
                            "text": {"tag": "plain_text", "content": "驳回申请"},
                            "value": {
                                "card_id": card_id,
                                "decision": "REJECT",
                                "target_unit_id": target_unit_id,
                                "tier_id": tier_id,
                            },
                        },
                    ],
                },
            ]
        },
    }


def render_wecom_work_card(
    *,
    card_id: str,
    title: str,
    summary_fields: Sequence[tuple[str, str]],
    target_unit_id: str,
    required_role: str,
    eligible_approver_ids: Sequence[str],
    tier_id: str,
) -> dict[str, Any]:
    """Render a standard Enterprise WeChat template card (企微按钮交互模版卡片 JSON)."""
    return {
        "msgtype": "template_card",
        "template_card": {
            "card_type": "button_interaction",
            "main_title": {
                "title": title,
                "desc": f"工单 {card_id} · 档位 {tier_id} · 组织 {target_unit_id}",
            },
            "horizontal_content_list": [
                {"keyname": k, "value": v} for k, v in summary_fields
            ]
            + [
                {"keyname": "审批角色", "value": required_role},
                {"keyname": "候选审批人", "value": ", ".join(eligible_approver_ids)},
            ],
            "task_id": card_id,
            "button_list": [
                {
                    "text": "同意审批",
                    "style": 1,
                    "key": f"APPROVE:{card_id}:{tier_id}",
                },
                {
                    "text": "驳回",
                    "style": 3,
                    "key": f"REJECT:{card_id}:{tier_id}",
                },
            ],
        },
    }


def sign_callback(
    payload_bytes: bytes,
    timestamp: int | str,
    nonce: str,
    secret: str,
) -> str:
    """Compute canonical HMAC-SHA256 hex signature over ``timestamp + '\\n' + nonce + '\\n' + payload``."""
    canonical = f"{timestamp}\n{nonce}\n".encode("utf-8") + bytes(payload_bytes)
    return hmac.new(str(secret).encode("utf-8"), canonical, hashlib.sha256).hexdigest()


class CnOrgWorkCardBridge:
    """Coordinates org-scoped tiered evaluation, card rendering, and signed $t_1$ settlement."""

    def __init__(
        self,
        *,
        runtime: OntologyRuntime,
        directory: OrgDirectory,
        policy: TieredAuthorityPolicy,
        max_clock_skew_sec: int = 300,
    ) -> None:
        self.runtime = runtime
        self.directory = directory
        self.policy = policy
        self.max_clock_skew_sec = int(max_clock_skew_sec)
        self._seen_nonces: set[tuple[str, str]] = set()
        self._card_contexts: dict[str, dict[str, Any]] = {}

    def submit_or_escalate_with_org_authority(
        self,
        *,
        requester_user_id: str,
        target_unit_id: str,
        run_id: str,
        action: str,
        params: Mapping[str, Any],
        target_object_type: str,
        target_object_id: str,
        amount: float,
        card_id: str | None = None,
        shadow_mode: bool = False,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate org hierarchy + tier threshold; either AUTO-commit or issue WorkCard + IM cards."""
        evaluation: AuthorityEvaluation = evaluate_action_authority(
            self.directory,
            requester_user_id,
            target_unit_id,
            amount,
            policy=self.policy,
        )
        requester_actor: Actor = self.directory.resolve_actor(requester_user_id)
        params_dict = dict(params)

        sim = self.runtime.simulate_action(action, params_dict, requester_actor)
        if shadow_mode:
            return {
                "outcome": "SHADOW_RECORDED" if sim.valid else "SHADOW_WOULD_REJECT",
                "committed": False,
                "evaluation": evaluation.model_dump(mode="json"),
                "valid": bool(sim.valid),
                "error_code": sim.error_code,
            }
        if not sim.valid:
            raise InvariantError(
                f"simulation failed before authority routing for {action!r}: {sim.error}",
                code=str(sim.error_code or "ACTION_SIMULATION_INVALID"),
            )

        if evaluation.decision == "AUTO":
            run = self.runtime.submit_action(run_id, action, params_dict, requester_actor)
            return {
                "outcome": "AUTO_COMMITTED",
                "committed": True,
                "evaluation": evaluation.model_dump(mode="json"),
                "run": run,
            }

        cid = (card_id or f"WC-{target_object_id}-{uuid.uuid4().hex[:6].upper()}").strip()
        required_role = evaluation.required_approver_role or "steward"
        context_meta = {
            "target_unit_id": target_unit_id,
            "amount": float(amount),
            "tier_id": evaluation.tier_id,
            "requester_user_id": requester_user_id,
            "eligible_approver_ids": list(evaluation.eligible_approver_ids),
        }
        self._card_contexts[cid] = context_meta
        card_run = self.runtime.submit_action(
            f"{run_id}:OPEN:{cid}",
            "open_work_card",
            {
                "card_id": cid,
                "card_kind": "APPROVAL",
                "target_object_type": target_object_type,
                "target_object_id": target_object_id,
                "requested_action": action,
                "requested_params_json": json.dumps(params_dict, ensure_ascii=False),
                "assignee_role": required_role,
                "assignee_dept": target_unit_id,
                "external_ref": json.dumps(context_meta, ensure_ascii=False),
            },
            requester_actor,
        )

        unit_name = self.directory.get_unit(target_unit_id).name
        member_name = self.directory.get_member(requester_user_id).display_name
        card_title = title or f"[{unit_name}] {action} 分级审批 ({amount:,.0f} 元)"
        summary_fields = [
            ("申请单号", f"{target_object_type}:{target_object_id}"),
            ("目标动作", action),
            ("所属组织", f"{unit_name} ({target_unit_id})"),
            ("申请人", f"{member_name} ({requester_user_id})"),
            ("涉事金额", f"{amount:,.2f} CNY"),
        ]
        feishu_card = render_feishu_work_card(
            card_id=cid,
            title=card_title,
            summary_fields=summary_fields,
            target_unit_id=target_unit_id,
            required_role=required_role,
            eligible_approver_ids=evaluation.eligible_approver_ids,
            tier_id=evaluation.tier_id,
        )
        wecom_card = render_wecom_work_card(
            card_id=cid,
            title=card_title,
            summary_fields=summary_fields,
            target_unit_id=target_unit_id,
            required_role=required_role,
            eligible_approver_ids=evaluation.eligible_approver_ids,
            tier_id=evaluation.tier_id,
        )
        return {
            "outcome": "ESCALATED_WORK_CARD",
            "committed": False,
            "card_id": cid,
            "evaluation": evaluation.model_dump(mode="json"),
            "work_card_run": card_run,
            "feishu_card": feishu_card,
            "wecom_card": wecom_card,
        }

    def _parse_platform_payload(
        self,
        platform: str,
        payload_bytes: bytes,
    ) -> tuple[str, str, str, str]:
        data = json.loads(payload_bytes.decode("utf-8"))
        plat = platform.strip().lower()
        if plat == "feishu":
            approver_id = str(data.get("open_id") or data.get("user_id") or data["approver_user_id"])
            action_block = data.get("action") or {}
            val = action_block.get("value") if isinstance(action_block, Mapping) else {}
            if not isinstance(val, Mapping):
                val = {}
            card_id = str(val.get("card_id") or data["card_id"])
            decision = str(val.get("decision") or data.get("decision") or "APPROVE").upper()
            note = str(data.get("note") or "")
            return approver_id, card_id, decision, note
        if plat == "wecom":
            approver_id = str(data.get("userid") or data.get("FromUserName") or data["approver_user_id"])
            event_key = str(data.get("EventKey") or "")
            if ":" in event_key and len(event_key.split(":")) >= 2:
                parts = event_key.split(":")
                decision = parts[0].upper()
                card_id = parts[1]
            else:
                card_id = str(data.get("task_id") or data["card_id"])
                decision = str(data.get("decision") or "APPROVE").upper()
            note = str(data.get("note") or "")
            return approver_id, card_id, decision, note
        raise ValueError(f"unsupported callback platform {platform!r}")

    def handle_card_callback(
        self,
        platform: str,
        payload_bytes: bytes,
        *,
        signature: str,
        timestamp: int | str,
        nonce: str,
        secret: str,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """Verify HMAC, timestamp, replay nonce, org scope, SoD, tier rank, and run $t_1$ checks."""
        cur_ts = time.time() if now_ts is None else float(now_ts)
        try:
            req_ts = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise CallbackSecurityError(
                f"invalid callback timestamp {timestamp!r}",
                code=CALLBACK_TIMESTAMP_EXPIRED,
            ) from exc
        if abs(cur_ts - req_ts) > self.max_clock_skew_sec:
            raise CallbackSecurityError(
                f"callback timestamp {timestamp!r} outside allowed window ({self.max_clock_skew_sec}s)",
                code=CALLBACK_TIMESTAMP_EXPIRED,
            )

        expected_sig = sign_callback(payload_bytes, timestamp, nonce, secret)
        if not hmac.compare_digest(str(signature or "").strip().lower(), expected_sig.lower()):
            raise CallbackSecurityError(
                "invalid callback HMAC-SHA256 signature",
                code=CALLBACK_SIGNATURE_INVALID,
            )

        plat_key = platform.strip().lower()
        nonce_key = (plat_key, str(nonce))
        if not str(nonce or "").strip():
            raise CallbackSecurityError("missing callback nonce", code=CALLBACK_REPLAY_DETECTED)
        if nonce_key in self._seen_nonces:
            raise CallbackSecurityError(
                f"duplicate callback nonce replayed: {nonce!r}",
                code=CALLBACK_REPLAY_DETECTED,
            )
        self._seen_nonces.add(nonce_key)

        approver_user_id, card_id, decision, note = self._parse_platform_payload(platform, payload_bytes)
        reader_admin = Actor(actor_id="bridge-meta-reader", role="admin", clearance_tags={"*"})
        card = self.runtime.get_object("WorkCard", card_id, reader_admin)
        if card.status not in ("OPEN", "IN_PROGRESS", "SLA_BREACHED"):
            raise InvariantError(
                f"WorkCard:{card_id} in status {card.status!r} is no longer actionable",
                code="WORK_CARD_NOT_ACTIVE",
            )
        meta = dict(self._card_contexts.get(card_id) or {})
        if (not meta) and card.external_ref:
            try:
                parsed_ref = json.loads(card.external_ref)
                if isinstance(parsed_ref, dict):
                    meta = parsed_ref
            except Exception:
                meta = {}
        target_unit_id = str(meta.get("target_unit_id") or card.assignee_dept)
        amount = float(meta["amount"])

        verify_approver_authority(
            self.directory,
            approver_user_id,
            card.requester_actor_id,
            target_unit_id,
            amount,
            policy=self.policy,
        )
        approver_actor = self.directory.resolve_actor(approver_user_id)

        if decision == "REJECT":
            rej_run = self.runtime.submit_action(
                f"CB-{plat_key}-{nonce}-REJ",
                "reject_work_card",
                {
                    "card_id": card_id,
                    "resolution_note": note or f"rejected via {plat_key} by {approver_user_id}",
                },
                approver_actor,
            )
            return {
                "outcome": "REJECTED_WORK_CARD",
                "card_id": card_id,
                "platform": plat_key,
                "approver_user_id": approver_user_id,
                "work_card_run": rej_run,
            }

        target_params = json.loads(card.requested_params_json or "{}")
        # Synchronous t1 live re-verification against current world state before any mutation
        sim_t1 = self.runtime.simulate_action(card.requested_action, target_params, approver_actor)
        if not sim_t1.valid:
            raise InvariantError(
                f"t1 re-verification failed for WorkCard:{card_id} action {card.requested_action!r}: {sim_t1.error}",
                code=str(sim_t1.error_code or T1_PHANTOM_READ_BLOCKED),
            )

        action_run = self.runtime.submit_action(
            f"CB-{plat_key}-{nonce}-ACT",
            card.requested_action,
            target_params,
            approver_actor,
        )
        comp_run = self.runtime.submit_action(
            f"CB-{plat_key}-{nonce}-COMP",
            "complete_work_card",
            {
                "card_id": card_id,
                "resolution_note": note or f"approved via {plat_key} by {approver_user_id}",
                "external_ref": json.dumps(
                    {"platform": plat_key, "nonce": nonce, "approver": approver_user_id},
                    ensure_ascii=False,
                ),
                "settled_run_id": action_run["run_id"],
            },
            approver_actor,
        )
        return {
            "outcome": "APPROVED_AND_SETTLED",
            "card_id": card_id,
            "platform": plat_key,
            "approver_user_id": approver_user_id,
            "action_run": action_run,
            "work_card_run": comp_run,
        }
