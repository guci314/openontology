"""Pure M2 organizational hierarchy, scoped RLS actor projection, and tiered authority engine.

Domain-agnostic extension shell providing:
- Tree-structured org directories (``OrgUnit``, ``OrgMember``, ``OrgDirectory``) with
  cycle and dangling-parent invariants.
- Deterministic projection of directory positions onto standard M2 ``Actor`` identities
  with hierarchical subtree ``clearance_tags`` for native row-level security (RLS).
- Server-side ``Bearer`` SHA-256 token verification factory for ``OntologyMCPServer``.
- Dynamic multi-tier monetary/risk authority evaluation, strict SoD checks, and
  optional bridge integration with ``AutonomyRampController``.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .autonomy import (
    ActionAutonomyRule,
    AutonomyDecision,
    AutonomyPolicy,
    AutonomyRampController,
)
from .runtime import OntologyRuntime
from .spec import Actor, InvariantError, PermissionDeniedError

__all__ = [
    "ORG_CYCLE_DETECTED",
    "ORG_PARENT_NOT_FOUND",
    "ORG_UNIT_NOT_FOUND",
    "ORG_MEMBER_NOT_FOUND",
    "ORG_MEMBER_INACTIVE",
    "AUTH_TOKEN_MISSING",
    "AUTH_TOKEN_INVALID",
    "AUTHORITY_ORG_SCOPE_DENIED",
    "SOD_SELF_APPROVAL_FORBIDDEN",
    "AUTHORITY_TIER_INSUFFICIENT",
    "AUTHORITY_TIER_UNMATCHED",
    "OrgAuthorityError",
    "hash_auth_token",
    "OrgUnit",
    "OrgMember",
    "OrgDirectory",
    "AuthorityTier",
    "AuthorityEvaluation",
    "TieredAuthorityPolicy",
    "evaluate_action_authority",
    "verify_approver_authority",
    "TieredAutonomyAdapter",
]

ORG_CYCLE_DETECTED = "ORG_CYCLE_DETECTED"
ORG_PARENT_NOT_FOUND = "ORG_PARENT_NOT_FOUND"
ORG_UNIT_NOT_FOUND = "ORG_UNIT_NOT_FOUND"
ORG_MEMBER_NOT_FOUND = "ORG_MEMBER_NOT_FOUND"
ORG_MEMBER_INACTIVE = "ORG_MEMBER_INACTIVE"
AUTH_TOKEN_MISSING = "AUTH_TOKEN_MISSING"
AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
AUTHORITY_ORG_SCOPE_DENIED = "AUTHORITY_ORG_SCOPE_DENIED"
SOD_SELF_APPROVAL_FORBIDDEN = "SOD_SELF_APPROVAL_FORBIDDEN"
AUTHORITY_TIER_INSUFFICIENT = "AUTHORITY_TIER_INSUFFICIENT"
AUTHORITY_TIER_UNMATCHED = "AUTHORITY_TIER_UNMATCHED"


class OrgAuthorityError(PermissionDeniedError):
    """Structured authorization or org-scope failure carrying a deterministic ``code``."""

    def __init__(self, message: str, *, code: str = "PERMISSION_DENIED") -> None:
        self.code = str(code)
        super().__init__(message)


def hash_auth_token(raw_token: str) -> str:
    """Return the lowercase SHA-256 hex digest for a raw bearer credential."""
    cleaned = str(raw_token or "")
    if not cleaned:
        raise ValueError("raw_token must not be empty")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


class OrgUnit(BaseModel):
    """One node inside an enterprise organizational hierarchy tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str
    name: str
    parent_id: str | None = None
    unit_kind: str = "department"
    scope_tag: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrgMember(BaseModel):
    """One human or service participant bound to an ``OrgUnit`` and role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    display_name: str
    role: str
    unit_id: str
    title: str = ""
    extra_clearance_tags: list[str] = Field(default_factory=list)
    active: bool = True
    auth_token_hash: str = ""


class OrgDirectory:
    """In-memory M2 organizational tree supporting RLS projection and token authentication."""

    def __init__(
        self,
        units: Sequence[OrgUnit] = (),
        participants: Sequence[OrgMember] = (),
    ) -> None:
        self._units: dict[str, OrgUnit] = {}
        self._entries: dict[str, OrgMember] = {}
        for unit in units:
            self.register_unit(unit)
        for participant in participants:
            self.register_member(participant)
        if self._units or self._entries:
            self.validate()

    def register_unit(self, unit: OrgUnit) -> None:
        self._units[unit.unit_id] = unit

    def register_member(self, participant: OrgMember) -> None:
        self._entries[participant.user_id] = participant

    def get_unit(self, unit_id: str) -> OrgUnit:
        unit = self._units.get(unit_id)
        if unit is None:
            raise OrgAuthorityError(
                f"unknown org unit {unit_id!r}",
                code=ORG_UNIT_NOT_FOUND,
            )
        return unit

    def get_member(self, user_id: str) -> OrgMember:
        participant = self._entries.get(user_id)
        if participant is None:
            raise OrgAuthorityError(
                f"unknown org participant {user_id!r}",
                code=ORG_MEMBER_NOT_FOUND,
            )
        return participant

    def list_units(self) -> list[OrgUnit]:
        return list(self._units.values())

    def list_members(self) -> list[OrgMember]:
        return list(self._entries.values())

    def validate(self) -> None:
        """Enforce tree acyclicity, valid parent references, and valid unit bindings."""
        for unit in self._units.values():
            if unit.parent_id is not None and unit.parent_id != "":
                if unit.parent_id not in self._units:
                    raise InvariantError(
                        f"org unit {unit.unit_id!r} references missing parent {unit.parent_id!r}",
                        code=ORG_PARENT_NOT_FOUND,
                    )
        for uid in self._units:
            seen: set[str] = set()
            cur: str | None = uid
            while cur is not None and cur != "":
                if cur in seen:
                    raise InvariantError(
                        f"cycle detected in org directory involving unit {cur!r}",
                        code=ORG_CYCLE_DETECTED,
                    )
                seen.add(cur)
                unit = self._units.get(cur)
                if unit is None:
                    raise InvariantError(
                        f"missing ancestor unit {cur!r}",
                        code=ORG_PARENT_NOT_FOUND,
                    )
                cur = unit.parent_id
        for participant in self._entries.values():
            if participant.unit_id not in self._units:
                raise InvariantError(
                    f"org participant {participant.user_id!r} bound to missing unit {participant.unit_id!r}",
                    code=ORG_UNIT_NOT_FOUND,
                )

    def ancestors(self, unit_id: str, *, include_self: bool = False) -> list[str]:
        """Return ordered ancestor IDs from nearest parent (or self) upward to root."""
        self.validate()
        self.get_unit(unit_id)
        chain: list[str] = []
        seen: set[str] = set()
        cur: str | None = unit_id if include_self else self._units[unit_id].parent_id
        while cur is not None and cur != "":
            if cur in seen:
                raise InvariantError(
                    f"cycle detected in org directory at {cur!r}",
                    code=ORG_CYCLE_DETECTED,
                )
            seen.add(cur)
            chain.append(cur)
            cur = self._units[cur].parent_id
        return chain

    def subtree_ids(self, unit_id: str, *, include_self: bool = True) -> set[str]:
        """Return all unit IDs in the hierarchy rooted at ``unit_id``."""
        self.validate()
        self.get_unit(unit_id)
        children_by_parent: dict[str | None, list[str]] = {}
        for unit in self._units.values():
            children_by_parent.setdefault(unit.parent_id, []).append(unit.unit_id)

        visited: set[str] = set()
        stack: list[str] = [unit_id]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            for child_id in children_by_parent.get(cur, ()):
                stack.append(child_id)
        if not include_self:
            visited.discard(unit_id)
        return visited

    def covers(self, supervisor_unit_id: str, target_unit_id: str) -> bool:
        """Return ``True`` iff ``supervisor_unit_id`` equals or is an ancestor of ``target_unit_id``."""
        self.validate()
        self.get_unit(supervisor_unit_id)
        self.get_unit(target_unit_id)
        return target_unit_id in self.subtree_ids(supervisor_unit_id, include_self=True)

    def resolve_actor(
        self,
        user_id: str,
        *,
        include_subtree_tags: bool = True,
    ) -> Actor:
        """Project an active directory participant onto an M2 ``Actor`` with scoped RLS grants."""
        self.validate()
        participant = self.get_member(user_id)
        if not participant.active:
            raise OrgAuthorityError(
                f"org participant {user_id!r} is inactive",
                code=ORG_MEMBER_INACTIVE,
            )
        scoped_units = (
            self.subtree_ids(participant.unit_id, include_self=True)
            if include_subtree_tags
            else {participant.unit_id}
        )
        tags: set[str] = {
            self._units[uid].scope_tag
            for uid in scoped_units
            if self._units[uid].scope_tag
        }
        tags.update(t for t in participant.extra_clearance_tags if t)
        return Actor(
            actor_id=participant.user_id,
            role=participant.role,
            clearance_tags=tags,
        )

    def authenticate_token(
        self,
        raw_token: str,
        *,
        include_subtree_tags: bool = True,
    ) -> Actor:
        """Verify a credential string against stored SHA-256 hashes (fail-closed)."""
        token = str(raw_token or "").strip()
        if not token:
            raise OrgAuthorityError(
                "missing authentication bearer token",
                code=AUTH_TOKEN_MISSING,
            )
        presented_digest = hash_auth_token(token).lower()
        matched: OrgMember | None = None
        for participant in self._entries.values():
            expected_digest = (participant.auth_token_hash or "").strip().lower()
            if expected_digest and hmac.compare_digest(presented_digest, expected_digest):
                matched = participant
                break
        if matched is None:
            raise OrgAuthorityError(
                "invalid authentication bearer token",
                code=AUTH_TOKEN_INVALID,
            )
        return self.resolve_actor(
            matched.user_id,
            include_subtree_tags=include_subtree_tags,
        )

    def build_mcp_actor_resolver(
        self,
        token_header: str = "Authorization",
        *,
        include_subtree_tags: bool = True,
    ) -> Callable[[Mapping[str, str]], Actor]:
        """Build a server-side ``actor_resolver`` ignoring spoofing headers like ``X-Role``."""
        target_header = str(token_header or "Authorization").lower()

        def _resolve(headers: Mapping[str, str]) -> Actor:
            raw_val: str | None = None
            for hk, hv in (headers or {}).items():
                if str(hk).lower() == target_header:
                    raw_val = str(hv)
                    break
            if raw_val is None or not raw_val.strip():
                raise OrgAuthorityError(
                    f"missing required auth header {token_header!r}",
                    code=AUTH_TOKEN_MISSING,
                )
            stripped = raw_val.strip()
            parts = stripped.split(None, 1)
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
                raise OrgAuthorityError(
                    "authorization header must use 'Bearer <token>' format",
                    code=AUTH_TOKEN_INVALID,
                )
            return self.authenticate_token(
                parts[1].strip(),
                include_subtree_tags=include_subtree_tags,
            )

        return _resolve


class AuthorityTier(BaseModel):
    """Single quantitative threshold tier governing AUTO execution vs human signoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_id: str
    min_amount: float = 0.0
    max_amount: float | None = None
    mode: Literal["AUTO", "ESCALATE"]
    required_approver_role: str | None = None
    require_covering_org_scope: bool = True


class AuthorityEvaluation(BaseModel):
    """Result returned by ``evaluate_action_authority``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requester_user_id: str
    requester_unit_id: str
    target_unit_id: str
    amount: float
    tier_id: str
    decision: Literal["AUTO", "ESCALATE"]
    required_approver_role: str | None = None
    required_covering_unit_id: str
    eligible_approver_ids: list[str] = Field(default_factory=list)


class TieredAuthorityPolicy(BaseModel):
    """Ordered authority ladder mapping monetary/risk bands to org-scoped roles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tiers: tuple[AuthorityTier, ...]
    role_rank: dict[str, int] = Field(default_factory=dict)

    def resolve_tier(self, amount: float) -> AuthorityTier:
        """Select the unique matching tier for ``amount`` or fail-closed."""
        val = float(amount)
        if val < 0:
            raise OrgAuthorityError(
                f"authority amount must be non-negative, got {val!r}",
                code=AUTHORITY_TIER_UNMATCHED,
            )
        for tier in self.tiers:
            if val < float(tier.min_amount):
                continue
            if tier.max_amount is not None and val > float(tier.max_amount):
                continue
            return tier
        raise OrgAuthorityError(
            f"no authority tier matched amount={val}",
            code=AUTHORITY_TIER_UNMATCHED,
        )

    def _effective_ranks(self) -> dict[str, int]:
        if self.role_rank:
            ranks = dict(self.role_rank)
            ranks.setdefault("admin", max(ranks.values(), default=0) + 1000)
            return ranks
        ranks: dict[str, int] = {}
        next_rank = 1
        for tier in self.tiers:
            req_role = (tier.required_approver_role or "").strip()
            if req_role and req_role not in ranks:
                ranks[req_role] = next_rank
                next_rank += 1
        ranks.setdefault("admin", 10_000)
        return ranks

    def role_satisfies(self, candidate_role: str, required_role: str | None) -> bool:
        """Return ``True`` if ``candidate_role`` meets or outranks ``required_role``."""
        if not required_role:
            return True
        if candidate_role == required_role or candidate_role == "admin":
            return True
        ranks = self._effective_ranks()
        if candidate_role in ranks and required_role in ranks:
            return ranks[candidate_role] >= ranks[required_role]
        return False

    def evaluate(
        self,
        directory: OrgDirectory,
        requester_user_id: str,
        target_unit_id: str,
        amount: float,
    ) -> AuthorityEvaluation:
        return evaluate_action_authority(
            directory,
            requester_user_id,
            target_unit_id,
            amount,
            policy=self,
        )

    def verify_approver(
        self,
        directory: OrgDirectory,
        approver_user_id: str,
        requester_user_id: str,
        target_unit_id: str,
        amount: float,
    ) -> AuthorityTier:
        return verify_approver_authority(
            directory,
            approver_user_id,
            requester_user_id,
            target_unit_id,
            amount,
            policy=self,
        )


def _participant_covers_target(
    directory: OrgDirectory,
    participant: OrgMember,
    target_unit_id: str,
) -> bool:
    if directory.covers(participant.unit_id, target_unit_id):
        return True
    target_tag = directory.get_unit(target_unit_id).scope_tag
    return bool(target_tag and target_tag in set(participant.extra_clearance_tags))


def evaluate_action_authority(
    directory: OrgDirectory,
    requester_user_id: str,
    target_unit_id: str,
    amount: float,
    *,
    policy: TieredAuthorityPolicy,
) -> AuthorityEvaluation:
    """Validate requester org scope and route the request per ``policy``."""
    directory.validate()
    requester = directory.get_member(requester_user_id)
    if not requester.active:
        raise OrgAuthorityError(
            f"requester {requester_user_id!r} is inactive",
            code=ORG_MEMBER_INACTIVE,
        )
    directory.get_unit(target_unit_id)

    if not _participant_covers_target(directory, requester, target_unit_id):
        raise OrgAuthorityError(
            f"requester {requester_user_id!r} (unit={requester.unit_id!r}) does not cover target unit {target_unit_id!r}",
            code=AUTHORITY_ORG_SCOPE_DENIED,
        )

    tier = policy.resolve_tier(amount)
    if tier.mode == "AUTO":
        return AuthorityEvaluation(
            requester_user_id=requester_user_id,
            requester_unit_id=requester.unit_id,
            target_unit_id=target_unit_id,
            amount=float(amount),
            tier_id=tier.tier_id,
            decision="AUTO",
            required_approver_role=None,
            required_covering_unit_id=target_unit_id,
            eligible_approver_ids=[],
        )

    chain = directory.ancestors(target_unit_id, include_self=True)
    distance_by_unit = {uid: idx for idx, uid in enumerate(chain)}

    exact_candidates: list[tuple[int, str, OrgMember]] = []
    higher_candidates: list[tuple[int, str, OrgMember]] = []
    for participant in directory.list_members():
        if not participant.active or participant.user_id == requester_user_id:
            continue
        if tier.require_covering_org_scope and not directory.covers(
            participant.unit_id, target_unit_id
        ):
            continue
        dist = distance_by_unit.get(participant.unit_id, 9999)
        if tier.required_approver_role is None or participant.role == tier.required_approver_role:
            exact_candidates.append((dist, participant.user_id, participant))
        elif policy.role_satisfies(participant.role, tier.required_approver_role):
            higher_candidates.append((dist, participant.user_id, participant))

    exact_candidates.sort(key=lambda item: (item[0], item[1]))
    higher_candidates.sort(key=lambda item: (item[0], item[1]))
    selected = exact_candidates or higher_candidates
    eligible_ids = [item[1] for item in selected]
    covering_unit_id = selected[0][2].unit_id if selected else target_unit_id

    return AuthorityEvaluation(
        requester_user_id=requester_user_id,
        requester_unit_id=requester.unit_id,
        target_unit_id=target_unit_id,
        amount=float(amount),
        tier_id=tier.tier_id,
        decision="ESCALATE",
        required_approver_role=tier.required_approver_role,
        required_covering_unit_id=covering_unit_id,
        eligible_approver_ids=eligible_ids,
    )


def verify_approver_authority(
    directory: OrgDirectory,
    approver_user_id: str,
    requester_user_id: str,
    target_unit_id: str,
    amount: float,
    *,
    policy: TieredAuthorityPolicy,
) -> AuthorityTier:
    """Enforce active status, SoD, org hierarchy coverage, and tier rank for a decision maker."""
    directory.validate()
    approver = directory.get_member(approver_user_id)
    if not approver.active:
        raise OrgAuthorityError(
            f"approver {approver_user_id!r} is inactive",
            code=ORG_MEMBER_INACTIVE,
        )
    if approver_user_id == requester_user_id:
        raise OrgAuthorityError(
            f"SoD violation: requester {requester_user_id!r} cannot self-approve",
            code=SOD_SELF_APPROVAL_FORBIDDEN,
        )

    directory.get_unit(target_unit_id)
    tier = policy.resolve_tier(amount)

    if tier.require_covering_org_scope and not directory.covers(
        approver.unit_id, target_unit_id
    ):
        raise OrgAuthorityError(
            f"approver {approver_user_id!r} (unit={approver.unit_id!r}) does not cover target unit {target_unit_id!r}",
            code=AUTHORITY_ORG_SCOPE_DENIED,
        )

    if tier.required_approver_role and not policy.role_satisfies(
        approver.role, tier.required_approver_role
    ):
        raise OrgAuthorityError(
            f"approver role {approver.role!r} is insufficient for tier {tier.tier_id!r} "
            f"(requires {tier.required_approver_role!r})",
            code=AUTHORITY_TIER_INSUFFICIENT,
        )
    return tier


class TieredAutonomyAdapter:
    """Bridge combining ``OrgDirectory`` + ``TieredAuthorityPolicy`` with ``AutonomyRampController``."""

    def __init__(
        self,
        directory: OrgDirectory,
        policy: TieredAuthorityPolicy,
        *,
        escalation_action: str | None = None,
        escalation_payload_builder: Callable[..., dict[str, Any]] | None = None,
        shadow_only: bool = False,
        ledger_path: Any = None,
    ) -> None:
        self.directory = directory
        self.policy = policy
        self.escalation_action = escalation_action
        self.escalation_payload_builder = escalation_payload_builder
        self.shadow_only = bool(shadow_only)
        self.ledger_path = ledger_path

    def build_autonomy_controller(
        self,
        *,
        action: str,
        evaluation: AuthorityEvaluation,
        shadow_only: bool | None = None,
    ) -> AutonomyRampController:
        """Instantiate an ``AutonomyRampController`` aligned with ``evaluation``."""
        is_shadow = self.shadow_only if shadow_only is None else bool(shadow_only)
        auto_tiers = [t for t in self.policy.tiers if t.mode == "AUTO" and t.max_amount is not None]
        max_auto = max((float(t.max_amount) for t in auto_tiers), default=0.0)
        if is_shadow:
            rule = ActionAutonomyRule(
                action=action,
                mode="SHADOW",
                escalation_action=self.escalation_action,
                escalation_assignee_role=evaluation.required_approver_role,
            )
        elif evaluation.decision == "AUTO":
            rule = ActionAutonomyRule(
                action=action,
                mode="AUTO_UNDER_CEILING",
                max_value=max_auto,
                escalation_action=self.escalation_action,
                escalation_assignee_role=evaluation.required_approver_role,
            )
        else:
            rule = ActionAutonomyRule(
                action=action,
                mode="COPILOT_ESCALATE",
                escalation_action=self.escalation_action,
                escalation_assignee_role=evaluation.required_approver_role,
            )
        policy = AutonomyPolicy(
            default_mode="SHADOW" if is_shadow else "COPILOT_ESCALATE",
            default_escalation_action=self.escalation_action,
            default_escalation_assignee_role=evaluation.required_approver_role,
            rules=(rule,),
        )
        return AutonomyRampController(
            policy,
            ledger_path=self.ledger_path,
            escalation_payload_builder=self.escalation_payload_builder,
            value_extractor=lambda _p, _sim: evaluation.amount,
        )

    def execute_with_authority(
        self,
        runtime: OntologyRuntime,
        *,
        requester_user_id: str,
        target_unit_id: str,
        run_id: str,
        action: str,
        params: Mapping[str, Any],
        amount: float,
        shadow_only: bool | None = None,
    ) -> tuple[AuthorityEvaluation, AutonomyDecision]:
        """Evaluate org authority first, then execute through the tiered autonomy controller."""
        evaluation = evaluate_action_authority(
            self.directory,
            requester_user_id,
            target_unit_id,
            amount,
            policy=self.policy,
        )
        actor = self.directory.resolve_actor(requester_user_id)
        controller = self.build_autonomy_controller(
            action=action,
            evaluation=evaluation,
            shadow_only=shadow_only,
        )
        decision = controller.execute(runtime, actor, run_id, action, params)
        return evaluation, decision
