"""Instance-level capability binding for the internal execution boundary.

This module is internal infrastructure. It is not an Agent SDK, not a public
HTTP entry point, and a capability name is not by itself an authenticated
permission.

The public value contract of this first stage is limited to plain JSON business
parameters and plain JSON return values. Observers receive immutable summary
records only: they never see raw business parameters, raw results or raw
exception messages.

"Plain JSON" is enforced strictly before digesting: object keys must be strings
and values must be dict/list/str/int/float/bool/None with finite numbers. Tuples,
custom objects and cycles are rejected instead of being silently coerced by
``json.dumps`` (which would encode tuples as lists and integer keys as strings
while the tool still receives the original Python values). Rejection never
rewrites the business values.
"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import math
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallContext:
    instance_id: str
    operation_id: str
    actor_id: str
    mode: str = "live"
    attempt: int = 1


@dataclass(frozen=True)
class Observation:
    context: CallContext
    capability: str
    phase: str
    input_digest: str
    output_digest: str | None = None
    error_type: str | None = None


def _validate_json(value: Any, seen: set[int]) -> None:
    """Reject anything outside the plain JSON value contract.

    Raises TypeError for unsupported types and object keys, ValueError for
    non-finite numbers and cycles. Only type names are reported, never values.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            raise ValueError("JSON value contains a circular reference")
        seen.add(id(value))
        try:
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise TypeError("JSON object keys must be strings")
                    _validate_json(item, seen)
            else:
                for item in value:
                    _validate_json(item, seen)
        finally:
            seen.discard(id(value))
        return
    raise TypeError(f"{type(value).__name__} is not a plain JSON value")


def digest(value: Any) -> str:
    _validate_json(value, set())
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class ExecutionFabric:
    def __init__(self, observers=()):
        self._tools: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._observers = tuple(observers)

    def bind(self, capability, implementation):
        if capability in self._tools:
            raise ValueError("capability already bound")
        self._tools[capability] = implementation

    def _observe(self, observation):
        for observer in self._observers:
            try:
                observer(observation)
            except Exception:
                # Optional observers only. Do not include raw values or exception
                # messages in logs, which may contain sensitive business data.
                LOGGER.warning("optional execution observer failed")

    def invoke(self, capability, params, context):
        implementation = self._tools[capability]
        # Reject non-plain-JSON requests before copying or executing anything,
        # so an invalid call performs zero tool calls and emits no observation.
        _validate_json(params, set())
        request = deepcopy(params)
        input_digest = digest(request)
        self._observe(Observation(context, capability, "started", input_digest))
        try:
            result = implementation(request)
        except Exception as exc:
            self._observe(
                Observation(
                    context,
                    capability,
                    "failed",
                    input_digest,
                    error_type=type(exc).__name__,
                )
            )
            raise
        try:
            output_digest = digest(result)
        except (TypeError, ValueError):
            # Preserve the actual result; no observer may turn an already
            # successful external effect into a retry-inducing failure.
            output_digest = None
            LOGGER.warning("execution result digest unavailable")
        self._observe(
            Observation(context, capability, "succeeded", input_digest, output_digest)
        )
        return result
