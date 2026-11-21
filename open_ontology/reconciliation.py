"""Pure M2 domain-agnostic observe-before-retry Saga reconciler over ``SourceBridge``.

Wraps any ``SourceBridge`` without altering ``runtime.py`` or ``schema.sql``.
When external actuation raises an ambiguous transport/timeout fault ("write followed by
loss of acknowledgment"), it NEVER retries blindly:

1. Deterministic SHA-256 ``idempotency_key`` is injected into the move envelope.
2. On an ambiguous error, independent read-only ``observe()`` is queried first and
   normalized against both the predicted post-state ``diff`` and the pre-state
   ``diff.inverted()``:
   - ``ALREADY_APPLIED_ON_DEVICE`` -> do NOT re-actuate (zero duplicate external writes);
     return cleanly so ``HomomorphicSettlementAspect`` settles ``COMMITTED``.
   - ``STILL_AT_PRE_STATE`` -> safely retry with the SAME ``idempotency_key`` (up to
     ``max_retries`` attempts).
   - ``DRIFTED_UNKNOWN_STATE`` (or retries exhausted without convergence) -> fail closed,
     trigger optional ``on_drift`` callback, and raise ``ReconciliationAmbiguousError``
     (``InvariantError`` subclass with ``code="RECONCILIATION_STATE_AMBIGUOUS"``) so the
     runtime records ``REJECTED`` with zero ABox mutation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from .ingestion import SnapshotIngestionDriver
from .sandbox import ObjectDiff
from .settlement import (
    SourceBridge,
    call_observe,
    normalize_readback,
    verify_readback_matches_diff,
)
from .spec import InvariantError, OntologySpec

__all__ = [
    "RECONCILIATION_STATE_AMBIGUOUS",
    "ReconciliationAmbiguousError",
    "ReconciliationRecord",
    "SagaReconciler",
    "IdempotentSourceBridge",
    "default_is_ambiguous_error",
    "derive_idempotency_key",
]

RECONCILIATION_STATE_AMBIGUOUS = "RECONCILIATION_STATE_AMBIGUOUS"

ReconciliationOutcome = Literal[
    "APPLIED_DIRECTLY",
    "RECONCILED_REMOTE_COMMITTED",
    "RETRIED_AND_COMMITTED",
    "DRIFTED_UNKNOWN_STATE",
    "NON_AMBIGUOUS_FAILURE",
]


class _SpecOnlyRuntime:
    """Minimal read-only context allowing ``SnapshotIngestionDriver`` normalization with only ``spec``."""

    def __init__(self, spec: OntologySpec) -> None:
        self.spec = spec
        self.ingestion = SnapshotIngestionDriver(self)


@dataclass(frozen=True)
class ReconciliationRecord:
    """Immutable audit entry summarizing idempotent actuation & post-timeout reconciliation."""

    run_id: str
    action_type: str
    idempotency_key: str
    outcome: ReconciliationOutcome
    actuate_calls: int
    observe_calls: int
    attempts: int
    error_messages: tuple[str, ...] = ()
    mismatches_after: tuple[str, ...] = ()
    mismatches_before: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["error_messages"] = list(self.error_messages)
        payload["mismatches_after"] = list(self.mismatches_after)
        payload["mismatches_before"] = list(self.mismatches_before)
        return payload


class ReconciliationAmbiguousError(InvariantError):
    """Fail-closed governance exception raised when device state after fault is ambiguous."""

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: str = "",
        mismatches: Sequence[str] = (),
        record: ReconciliationRecord | None = None,
    ) -> None:
        super().__init__(message, code=RECONCILIATION_STATE_AMBIGUOUS)
        self.idempotency_key = idempotency_key
        self.mismatches = tuple(mismatches)
        self.record = record


def derive_idempotency_key(move: Mapping[str, Any]) -> str:
    """Compute stable SHA-256 idempotency key from move identity, params, and diff."""
    canonical = {
        "run_id": str(move.get("run_id") or ""),
        "action_type": str(move.get("action_type") or ""),
        "is_revert": bool(move.get("is_revert", False)),
        "reverts_run_id": move.get("reverts_run_id"),
        "params": move.get("params") or {},
        "object_diff": move.get("object_diff") or {},
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_is_ambiguous_error(exc: Exception) -> bool:
    """Identify ambiguous timeout/transport/502-504 errors without importing network libraries."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError) and not isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return True
    if getattr(exc, "is_ambiguous", False) is True:
        return True
    for attr in ("status", "status_code", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and val in (502, 503, 504):
            return True
    return False


class SagaReconciler:
    """Observe-before-retry idempotent reconciler wrapping an external ``SourceBridge``."""

    def __init__(
        self,
        bridge: SourceBridge,
        spec: OntologySpec,
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.0,
        is_ambiguous_error: Callable[[Exception], bool] | None = None,
        on_drift: Callable[[ReconciliationRecord], None] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.bridge = bridge
        self.spec = spec
        self.max_retries = int(max_retries)
        self.backoff_seconds = float(backoff_seconds)
        self.is_ambiguous_error = is_ambiguous_error or default_is_ambiguous_error
        self.on_drift = on_drift
        self._lock = threading.RLock()
        self._records: list[ReconciliationRecord] = []
        self._norm_ctx = _SpecOnlyRuntime(spec)

    @property
    def records(self) -> list[ReconciliationRecord]:
        with self._lock:
            return list(self._records)

    def export_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [rec.to_dict() for rec in self._records]

    def _classify_readback(
        self,
        move: dict[str, Any],
        diff: ObjectDiff,
    ) -> tuple[str, list[str], list[str]]:
        readback_objects, readback_links = call_observe(self.bridge, move, spec=self.spec)
        readback_by_type, inline_edges, standalone_edges = normalize_readback(
            self._norm_ctx, readback_objects, readback_links
        )
        mismatches_after = verify_readback_matches_diff(
            self.spec, diff, readback_by_type, inline_edges, standalone_edges
        )
        if not mismatches_after:
            return "ALREADY_APPLIED_ON_DEVICE", [], []

        mismatches_before = verify_readback_matches_diff(
            self.spec, diff.inverted(), readback_by_type, inline_edges, standalone_edges
        )
        if not mismatches_before:
            return "STILL_AT_PRE_STATE", mismatches_after, []
        return "DRIFTED_UNKNOWN_STATE", mismatches_after, mismatches_before

    def _fail_drift(
        self,
        *,
        run_id: str,
        action_type: str,
        idem_key: str,
        actuate_calls: int,
        observe_calls: int,
        attempts: int,
        err_msgs: list[str],
        mismatches_after: Sequence[str],
        mismatches_before: Sequence[str],
    ) -> None:
        rec = ReconciliationRecord(
            run_id=run_id,
            action_type=action_type,
            idempotency_key=idem_key,
            outcome="DRIFTED_UNKNOWN_STATE",
            actuate_calls=actuate_calls,
            observe_calls=observe_calls,
            attempts=attempts,
            error_messages=tuple(err_msgs),
            mismatches_after=tuple(mismatches_after),
            mismatches_before=tuple(mismatches_before),
        )
        self._records.append(rec)
        if self.on_drift is not None:
            self.on_drift(rec)
        detail = "; ".join(mismatches_after or err_msgs) or "device state drifted to unknown state"
        raise ReconciliationAmbiguousError(
            f"reconciliation ambiguous for {action_type!r} ({run_id}): {detail}",
            idempotency_key=idem_key,
            mismatches=mismatches_after,
            record=rec,
        )

    def _actuate_with_reconciliation(self, move_envelope: dict[str, Any]) -> Any:
        with self._lock:
            run_id = str(move_envelope.get("run_id") or "")
            action_type = str(move_envelope.get("action_type") or "")
            diff = ObjectDiff.from_dict(move_envelope.get("object_diff") or {})
            idem_key = derive_idempotency_key(move_envelope)

            actuate_calls = 0
            observe_calls = 0
            err_msgs: list[str] = []
            max_attempts = 1 + self.max_retries

            for attempt in range(1, max_attempts + 1):
                envelope = copy.deepcopy(move_envelope)
                envelope["idempotency_key"] = idem_key
                envelope["attempt"] = attempt
                actuate_calls += 1
                try:
                    result = self.bridge.actuate(envelope)
                    outcome: ReconciliationOutcome = (
                        "APPLIED_DIRECTLY" if attempt == 1 else "RETRIED_AND_COMMITTED"
                    )
                    self._records.append(
                        ReconciliationRecord(
                            run_id=run_id,
                            action_type=action_type,
                            idempotency_key=idem_key,
                            outcome=outcome,
                            actuate_calls=actuate_calls,
                            observe_calls=observe_calls,
                            attempts=attempt,
                            error_messages=tuple(err_msgs),
                        )
                    )
                    return result
                except Exception as exc:
                    err_msgs.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if not self.is_ambiguous_error(exc):
                        self._records.append(
                            ReconciliationRecord(
                                run_id=run_id,
                                action_type=action_type,
                                idempotency_key=idem_key,
                                outcome="NON_AMBIGUOUS_FAILURE",
                                actuate_calls=actuate_calls,
                                observe_calls=observe_calls,
                                attempts=attempt,
                                error_messages=tuple(err_msgs),
                            )
                        )
                        raise

                    observe_calls += 1
                    branch, mismatches_after, mismatches_before = self._classify_readback(envelope, diff)
                    if branch == "ALREADY_APPLIED_ON_DEVICE":
                        self._records.append(
                            ReconciliationRecord(
                                run_id=run_id,
                                action_type=action_type,
                                idempotency_key=idem_key,
                                outcome="RECONCILED_REMOTE_COMMITTED",
                                actuate_calls=actuate_calls,
                                observe_calls=observe_calls,
                                attempts=attempt,
                                error_messages=tuple(err_msgs),
                            )
                        )
                        return {"reconciled": True, "idempotency_key": idem_key}

                    if branch == "DRIFTED_UNKNOWN_STATE":
                        self._fail_drift(
                            run_id=run_id,
                            action_type=action_type,
                            idem_key=idem_key,
                            actuate_calls=actuate_calls,
                            observe_calls=observe_calls,
                            attempts=attempt,
                            err_msgs=err_msgs,
                            mismatches_after=mismatches_after,
                            mismatches_before=mismatches_before,
                        )

                    # STILL_AT_PRE_STATE
                    if attempt >= max_attempts:
                        self._fail_drift(
                            run_id=run_id,
                            action_type=action_type,
                            idem_key=idem_key,
                            actuate_calls=actuate_calls,
                            observe_calls=observe_calls,
                            attempts=attempt,
                            err_msgs=err_msgs,
                            mismatches_after=mismatches_after,
                            mismatches_before=mismatches_before,
                        )

                    if self.backoff_seconds > 0:
                        time.sleep(self.backoff_seconds)

    def as_source_bridge(self) -> SourceBridge:
        """Expose an idempotent, observe-first ``SourceBridge`` drop-in wrapper."""
        return SourceBridge(
            actuate=self._actuate_with_reconciliation,
            observe=self.bridge.observe,
            allow_source_revert=self.bridge.allow_source_revert,
        )


IdempotentSourceBridge = SagaReconciler
