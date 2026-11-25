"""M2.5 External Brain & Contract-Gated Commit ABI (Pure Planner + Thin IO Gate).

Implements the architectural separation specified in ``docs/DOAL_SPEC.md`` Chapter 9:

* **External Turing-Complete Brain (Zero Write Handle)**: reads versioned snapshots
  through ``ScopedView`` / read-only ``@spec.function`` planners and emits a flat,
  JSON-serializable :class:`WriteBatchProposal`.
* **Thin IO Atomic Contract Gate** (:func:`apply_contract_proposal`): accepts a
  :class:`WriteBatchProposal` and executes a strictly loop-free atomic transaction:
  1. Idempotency (``proposal_id``) & RBAC & parameter validation
  2. Declared contract check & non-empty ``ops`` check
  3. Trust-mode enforcement (Class A ``untrusted_verifiable`` vs Class B ``attested_provider``)
  4. Read-Set CAS optimistic concurrency check (anti-TOCTOU, ``ConcurrencyConflictError``)
  5. Action declarative ``preconditions``
  6. Flat primitive ``ops`` (``create`` / ``modify`` / ``delete``) staged in :class:`EditBatch`
  7. Contract Frame (``CONTRACT_FRAME_VIOLATION``) + ``ensures`` (``CONTRACT_ENSURES_VIOLATION``)
     + authority + global state/transition invariants + FSM -> atomic settlement via
     :class:`HomomorphicSettlementAspect`.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import uuid
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .aspects import IdempotencyAndOCCAspect
from .runtime import OntologyRuntime, SimulationResult
from .sandbox import EditBatch, ObjectDiff
from .settlement import HomomorphicSettlementAspect
from .spec import (
    ActionDef,
    Actor,
    ConcurrencyConflictError,
    InvariantError,
    ObjectNotFoundError,
)

__all__ = [
    "ReadSetVersion",
    "WriteOp",
    "ProposalAttestation",
    "WriteBatchProposal",
    "canonical_proposal_sha256",
    "sign_proposal_attestation",
    "verify_proposal_attestation",
    "build_read_snapshot",
    "plan_contract_proposal",
    "apply_ops_to_batch",
    "apply_contract_proposal",
]


class ReadSetVersion(BaseModel):
    """Optimistic-lock read version pin for a single snapshot object (anti-TOCTOU CAS)."""

    model_config = ConfigDict(extra="forbid")

    object_type: str
    object_id: str
    expected_version: int = Field(..., ge=1)


class WriteOp(BaseModel):
    """Flat, loop-free primitive state mutation (strictly locked to Gate 8 ops)."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["create", "modify", "delete"]
    object_type: str
    object_id: str | None = None
    set_fields: dict[str, Any] = Field(default_factory=dict)


class ProposalAttestation(BaseModel):
    """Cryptographic or kernel-reverified trust anchor for Class-B attested brains."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    payload_sha256: str
    signature: str = ""


class WriteBatchProposal(BaseModel):
    """Structured mutation proposal emitted by an external zero-write-handle brain."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(default_factory=lambda: f"PROP-{uuid.uuid4().hex[:12].upper()}")
    contract_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    read_set: list[ReadSetVersion] = Field(default_factory=list)
    ops: list[WriteOp] = Field(default_factory=list)
    attestation: ProposalAttestation | None = None
    brain_source: str = "external_brain"


def _canonical_ops_digest_payload(
    contract_name: str,
    params: Mapping[str, Any],
    read_set: Sequence[ReadSetVersion | Mapping[str, Any]],
    ops: Sequence[WriteOp | Mapping[str, Any]],
) -> bytes:
    norm_rs = sorted(
        (
            r.model_dump(mode="json") if isinstance(r, ReadSetVersion) else dict(r)
            for r in read_set
        ),
        key=lambda item: (str(item.get("object_type", "")), str(item.get("object_id", ""))),
    )
    norm_ops = [
        o.model_dump(mode="json") if isinstance(o, WriteOp) else dict(o)
        for o in ops
    ]
    envelope = {
        "contract_name": str(contract_name),
        "params": dict(params),
        "read_set": norm_rs,
        "ops": norm_ops,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def canonical_proposal_sha256(proposal: WriteBatchProposal) -> str:
    """Compute canonical SHA-256 digest of a proposal's semantic payload."""
    return hashlib.sha256(
        _canonical_ops_digest_payload(
            proposal.contract_name,
            proposal.params,
            proposal.read_set,
            proposal.ops,
        )
    ).hexdigest()


def sign_proposal_attestation(
    proposal: WriteBatchProposal,
    provider_id: str,
    *,
    secret_key: str | None = None,
) -> ProposalAttestation:
    """Create a canonical SHA-256 (and optional HMAC-SHA256) attestation for a proposal."""
    digest = canonical_proposal_sha256(proposal)
    sig = ""
    if secret_key:
        sig = hmac.new(
            secret_key.encode("utf-8"),
            f"{provider_id}:{digest}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    return ProposalAttestation(
        provider_id=provider_id,
        payload_sha256=digest,
        signature=sig,
    )


def _normalize_ops_for_comparison(ops: Sequence[WriteOp]) -> list[dict[str, Any]]:
    return [op.model_dump(mode="json") for op in ops]


def verify_proposal_attestation(
    runtime: OntologyRuntime,
    action: ActionDef,
    proposal: WriteBatchProposal,
    actor: Actor,
    *,
    attestation_keys: Mapping[str, str] | None = None,
) -> None:
    """Enforce Class-B ``attested_provider`` trust boundary; no-op for Class-A contracts."""
    if action.trust_mode != "attested_provider":
        return

    att = proposal.attestation
    if att is None or att.provider_id not in action.attested_providers:
        raise InvariantError(
            f"Contract {action.name!r} requires trust_mode='attested_provider' from "
            f"{list(action.attested_providers)}; got {att.provider_id if att else None!r}",
            code="UNTRUSTED_PROPOSAL_PROVIDER",
        )

    expected_sha = canonical_proposal_sha256(proposal)
    if not hmac.compare_digest(att.payload_sha256, expected_sha):
        raise InvariantError(
            f"Proposal {proposal.proposal_id!r} payload_sha256 mismatch for provider {att.provider_id!r}",
            code="ATTESTATION_MISMATCH",
        )

    key_map = attestation_keys or {}
    if att.provider_id in key_map:
        expected_sig = hmac.new(
            key_map[att.provider_id].encode("utf-8"),
            f"{att.provider_id}:{expected_sha}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not att.signature or not hmac.compare_digest(att.signature, expected_sig):
            raise InvariantError(
                f"Proposal {proposal.proposal_id!r} HMAC attestation signature invalid for "
                f"provider {att.provider_id!r}",
                code="ATTESTATION_MISMATCH",
            )
        return

    # Zero-trust read-only kernel cross-verification via declared audited planner_function
    if action.planner_function and action.planner_function in runtime.spec.functions:
        fn_def = runtime.spec.get_function(action.planner_function)
        if not fn_def.read_only:
            raise InvariantError(
                f"Planner function {action.planner_function!r} must be read_only=True",
                code="ATTESTATION_UNVERIFIABLE",
            )
        recomputed_raw = runtime.call_function(action.planner_function, proposal.params, actor)
        recomputed = (
            recomputed_raw
            if isinstance(recomputed_raw, WriteBatchProposal)
            else WriteBatchProposal.model_validate(recomputed_raw)
        )
        if _normalize_ops_for_comparison(recomputed.ops) != _normalize_ops_for_comparison(
            proposal.ops
        ):
            raise InvariantError(
                f"Proposal {proposal.proposal_id!r} ops diverged from audited pure planner "
                f"{action.planner_function!r}",
                code="ATTESTATION_MISMATCH",
            )
        return

    raise InvariantError(
        f"Contract {action.name!r} has no verifiable attestation key or read-only planner_function "
        f"for provider {att.provider_id!r}",
        code="ATTESTATION_UNVERIFIABLE",
    )


def build_read_snapshot(
    runtime: OntologyRuntime,
    actor: Actor,
    *,
    object_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Export a strictly read-only versioned snapshot for an external zero-write-handle brain."""
    target_types = (
        list(object_types)
        if object_types is not None
        else sorted(runtime.spec.object_types.keys())
    )
    objects_by_type: dict[str, list[dict[str, Any]]] = {}
    read_set: list[dict[str, Any]] = []

    for ot_name in target_types:
        cls = runtime.spec.get_object_type(ot_name)
        loaded_by_id = {row.object_id: row for row in runtime.load_all(ot_name)}
        visible_objs = runtime.query_objects(cls, actor)
        entries: list[dict[str, Any]] = []
        for obj in visible_objs:
            oid = str(obj.primary_key_value())
            loaded = loaded_by_id.get(oid)
            ver = int(loaded.version) if loaded is not None else 1
            entries.append(
                {
                    "object_type": ot_name,
                    "object_id": oid,
                    "version": ver,
                    "properties": obj.model_dump(mode="json"),
                }
            )
            read_set.append(
                {
                    "object_type": ot_name,
                    "object_id": oid,
                    "expected_version": ver,
                }
            )
        objects_by_type[ot_name] = entries

    return {
        "actor_id": actor.actor_id,
        "actor_role": actor.role,
        "objects": objects_by_type,
        "read_set": read_set,
    }


def plan_contract_proposal(
    runtime: OntologyRuntime,
    contract_name: str,
    params: BaseModel | Mapping[str, Any] | None,
    actor: Actor,
    *,
    proposal_id: str | None = None,
    secret_key: str | None = None,
) -> WriteBatchProposal:
    """Invoke a contract's strictly read-only ``planner_function`` to emit a ``WriteBatchProposal``."""
    action = runtime.spec.get_action(contract_name)
    runtime._check_role(action, actor)
    if not action.planner_function:
        raise InvariantError(
            f"Contract {contract_name!r} has no declared read-only planner_function",
            code="NO_PLANNER_FUNCTION",
        )
    fn_def = runtime.spec.get_function(action.planner_function)
    if not fn_def.read_only:
        raise InvariantError(
            f"Planner function {action.planner_function!r} must be read_only=True",
            code="UNSAFE_PLANNER_FUNCTION",
        )
    model = runtime._build_params(action.params, params)
    params_dict = model.model_dump(mode="json")
    raw = runtime.call_function(action.planner_function, params_dict, actor)
    proposal = (
        raw
        if isinstance(raw, WriteBatchProposal)
        else WriteBatchProposal.model_validate(raw)
    )
    updates: dict[str, Any] = {
        "contract_name": contract_name,
        "params": params_dict,
        "brain_source": f"pure_function:{action.planner_function}",
    }
    if proposal_id is not None:
        updates["proposal_id"] = proposal_id
    proposal = proposal.model_copy(update=updates)
    if action.trust_mode == "attested_provider" or secret_key is not None:
        provider = (
            action.attested_providers[0]
            if action.attested_providers
            else action.planner_function
        )
        att = sign_proposal_attestation(proposal, provider, secret_key=secret_key)
        proposal = proposal.model_copy(update={"attestation": att})
    return proposal


def apply_ops_to_batch(ctx: EditBatch, ops: Sequence[WriteOp]) -> None:
    """Apply flat primitive ``WriteOp`` sequence into an in-memory ``EditBatch`` (zero loops/logic)."""
    for op in ops:
        cls = ctx.spec.get_object_type(op.object_type)
        if op.op == "create":
            payload = dict(op.set_fields)
            pk_name = cls.primary_key_name()
            if op.object_id is not None and pk_name not in payload:
                payload[pk_name] = op.object_id
            ctx.create(cls(**payload))
        elif op.op == "modify":
            if not op.object_id:
                raise InvariantError(
                    f"modify WriteOp on {op.object_type!r} requires non-empty object_id",
                    code="INVALID_WRITE_OP",
                )
            obj = ctx.require(cls, op.object_id)
            ctx.modify(obj, **dict(op.set_fields))
        elif op.op == "delete":
            if not op.object_id:
                raise InvariantError(
                    f"delete WriteOp on {op.object_type!r} requires non-empty object_id",
                    code="INVALID_WRITE_OP",
                )
            obj = ctx.require(cls, op.object_id)
            ctx.delete(obj)
        else:
            raise InvariantError(f"unsupported WriteOp op={op.op!r}", code="INVALID_WRITE_OP")


def _run_declared_preconditions(
    ctx: EditBatch,
    action: ActionDef,
    params_model: BaseModel,
) -> None:
    if not action.submission_criteria:
        return
    from .induction.dsl import InvariantRuleSpec, _run_action_preconditions

    pre_specs: list[InvariantRuleSpec] = []
    for item in action.submission_criteria:
        if isinstance(item, InvariantRuleSpec):
            pre_specs.append(item)
        elif isinstance(item, Mapping):
            pre_specs.append(InvariantRuleSpec.model_validate(item))
    if pre_specs:
        _run_action_preconditions(ctx, params_model, tuple(pre_specs))


def apply_contract_proposal(
    runtime: OntologyRuntime,
    proposal: WriteBatchProposal | Mapping[str, Any],
    actor: Actor,
    *,
    simulate: bool = False,
    attestation_keys: Mapping[str, str] | None = None,
    auto_trigger: bool = False,
) -> dict[str, Any] | SimulationResult:
    """M2.5 Thin IO Contract Gate: validate & atomically commit (or simulate) a ``WriteBatchProposal``."""
    prop = (
        proposal
        if isinstance(proposal, WriteBatchProposal)
        else WriteBatchProposal.model_validate(proposal)
    )

    if not simulate:
        existing = IdempotencyAndOCCAspect.check_idempotent_run(runtime, prop.proposal_id)
        if existing is not None:
            return existing

    action = runtime.spec.get_action(prop.contract_name)
    runtime._check_role(action, actor)
    params_model = runtime._build_params(action.params, prop.params)
    params_json = params_model.model_dump(mode="json")

    pinned_read_versions: dict[str, int] = {
        f"{r.object_type}:{r.object_id}": int(r.expected_version) for r in prop.read_set
    }

    def _handle_failure(exc: InvariantError) -> SimulationResult:
        if simulate:
            return SimulationResult(
                valid=False,
                diff=ObjectDiff(),
                read_versions=dict(pinned_read_versions),
                error=str(exc),
                error_code=exc.code,
            )
        runtime._record_rejection(
            run_id=prop.proposal_id,
            action=action,
            actor=actor,
            params_json=params_json,
            read_versions=dict(pinned_read_versions),
            error=exc,
        )
        rejected_run = runtime.get_action_run(prop.proposal_id)
        if rejected_run is not None:
            runtime._notify_run_event(actor, rejected_run)
        raise exc

    # 1) Must target a declared behavioral contract (never naked uncontracted writes)
    has_contract = bool(action.modifies or action.creates or action.deletes or action.ensures)
    if not has_contract:
        return _handle_failure(
            InvariantError(
                f"Action {action.name!r} has no declared behavioral contract for WriteBatchProposal",
                code="UNCONTRACTED_PROPOSAL_TARGET",
            )
        )

    if not prop.ops:
        return _handle_failure(
            InvariantError(
                f"Proposal {prop.proposal_id!r} for {action.name!r} must contain at least one WriteOp",
                code="EMPTY_PROPOSAL_OPS",
            )
        )

    # 2) Trust Mode / Attestation Gate (Class A vs Class B)
    try:
        verify_proposal_attestation(
            runtime, action, prop, actor, attestation_keys=attestation_keys
        )
    except InvariantError as exc:
        return _handle_failure(exc)

    # 3) Read-Set CAS Pin Verification (anti-TOCTOU)
    if action.require_read_set_cas:
        for op in prop.ops:
            if op.op in ("modify", "delete"):
                if not op.object_id:
                    return _handle_failure(
                        InvariantError(
                            f"{op.op} op on {op.object_type!r} missing object_id",
                            code="INVALID_WRITE_OP",
                        )
                    )
                key = f"{op.object_type}:{op.object_id}"
                if key not in pinned_read_versions:
                    return _handle_failure(
                        InvariantError(
                            f"Contract {action.name!r} requires Read-Set CAS version pin for {key!r}",
                            code="MISSING_READ_SET_VERSION",
                        )
                    )

    for key, expected_ver in pinned_read_versions.items():
        ot_name, _, oid = key.partition(":")
        loaded = runtime.load_object(ot_name, oid)
        if loaded is None:
            err = ConcurrencyConflictError(
                f"Read-Set CAS failed: {key!r} expected version {expected_ver}, but object no longer exists"
            )
            if simulate:
                return SimulationResult(
                    valid=False,
                    diff=ObjectDiff(),
                    read_versions=dict(pinned_read_versions),
                    error=str(err),
                    error_code="CONCURRENCY_CONFLICT",
                )
            raise err
        if int(loaded.version) != expected_ver:
            err = ConcurrencyConflictError(
                f"Read-Set CAS failed for {key!r}: expected version {expected_ver}, "
                f"live version is {loaded.version}"
            )
            if simulate:
                return SimulationResult(
                    valid=False,
                    diff=ObjectDiff(),
                    read_versions=dict(pinned_read_versions),
                    error=str(err),
                    error_code="CONCURRENCY_CONFLICT",
                )
            raise err

    # 4) Stage flat ops inside EditBatch and run full contract + invariant verification
    batch = EditBatch(runtime.spec, runtime, actor)
    pre_objs: dict[str, dict[str, Any]] = {}
    pre_kinds: dict[str, str] = {}
    try:
        _run_declared_preconditions(batch, action, params_model)
        pre_objs = {k: copy.deepcopy(v) for k, v in batch._objects.items()}
        pre_kinds = dict(batch._kinds)

        apply_ops_to_batch(batch, prop.ops)

        from .induction.dsl import verify_action_contract_delta

        verify_action_contract_delta(batch, action, params_model, pre_objs, pre_kinds)
        pre_diff = batch.build_diff()
        runtime._check_authority(action, pre_diff)
        batch.validate_object_properties()
        batch.validate_invariants()
        diff = batch.build_diff()
        batch.validate_transition_invariants(diff)
    except (InvariantError, ObjectNotFoundError) as exc:
        inv_exc = (
            exc
            if isinstance(exc, InvariantError)
            else InvariantError(str(exc), code=getattr(exc, "code", "OBJECT_NOT_FOUND"))
        )
        return _handle_failure(inv_exc)

    # Pin caller's exact Read-Set CAS versions over any newly read versions so SQLite OCC enforces them atomically!
    final_read_versions = dict(batch.read_versions)
    final_read_versions.update(pinned_read_versions)

    if simulate:
        return SimulationResult(
            valid=True,
            diff=diff,
            read_versions=final_read_versions,
        )

    audit_params = dict(params_json)
    audit_params["_proposal_meta"] = {
        "proposal_id": prop.proposal_id,
        "brain_source": prop.brain_source,
        "trust_mode": action.trust_mode,
        "attested_provider": prop.attestation.provider_id if prop.attestation else None,
        "read_set_count": len(prop.read_set),
        "op_count": len(prop.ops),
    }

    return HomomorphicSettlementAspect.settle_submission(
        runtime,
        run_id=prop.proposal_id,
        action=action,
        actor=actor,
        params_json=audit_params,
        read_versions=final_read_versions,
        diff=diff,
        auto_trigger=auto_trigger,
    )
