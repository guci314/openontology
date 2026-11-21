"""Pure M2 domain-agnostic three-stage autonomy ramp shell over ``OntologyRuntime``.

Supports progressive zero-risk production onboarding via three governance modes:
- ``"SHADOW"``: simulate only (`simulate_action`), never mutate ABox or call external bridges,
  recording counterfactual results to an immutable shadow ledger (optional JSONL file).
- ``"COPILOT_ESCALATE"``: fail-fast on invalid simulation; for valid simulations, escalate
  via an application-configured governed action (plus payload builder) instead of direct
  execution, or return a structured escalation intent when no action is configured.
- ``"AUTO_UNDER_CEILING"``: automatically commit via ``submit_action`` when an extracted
  numerical risk/value metric is at or below ``max_value``; fail-closed by downgrading to
  the escalation path whenever the metric exceeds ``max_value`` or cannot be extracted.
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import threading
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .runtime import OntologyRuntime, SimulationResult
from .spec import Actor

__all__ = [
    "AutonomyMode",
    "ActionAutonomyRule",
    "AutonomyPolicy",
    "ShadowLedgerEntry",
    "AutonomyDecision",
    "AutonomyRampController",
]

AutonomyMode = Literal["SHADOW", "COPILOT_ESCALATE", "AUTO_UNDER_CEILING"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ActionAutonomyRule(BaseModel):
    """Per-action/role governance rule controlling shadow vs copilot vs bounded auto execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str = "*"
    roles: tuple[str, ...] = ()
    mode: AutonomyMode = "SHADOW"
    ceiling_field: str | None = None
    max_value: float | None = None
    escalation_action: str | None = None
    escalation_assignee_role: str | None = None


class AutonomyPolicy(BaseModel):
    """Collection of ordered autonomy rules plus default fallback policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_mode: AutonomyMode = "SHADOW"
    default_escalation_action: str | None = None
    default_escalation_assignee_role: str | None = None
    rules: tuple[ActionAutonomyRule, ...] = ()

    def resolve(self, action: str, role: str) -> ActionAutonomyRule:
        """Select most specific matching rule: exact action+role > exact action > wildcard+role > wildcard > default."""
        candidates: list[tuple[int, int, ActionAutonomyRule]] = []
        for idx, rule in enumerate(self.rules):
            if rule.action not in (action, "*"):
                continue
            if rule.roles and role not in rule.roles:
                continue
            action_prio = 2 if rule.action == action else 1
            role_prio = 1 if rule.roles else 0
            candidates.append((action_prio * 10 + role_prio, -idx, rule))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            chosen = candidates[0][2]
            if chosen.escalation_action is None and self.default_escalation_action is not None:
                return chosen.model_copy(
                    update={
                        "escalation_action": self.default_escalation_action,
                        "escalation_assignee_role": (
                            chosen.escalation_assignee_role or self.default_escalation_assignee_role
                        ),
                    }
                )
            return chosen
        return ActionAutonomyRule(
            action=action,
            mode=self.default_mode,
            escalation_action=self.default_escalation_action,
            escalation_assignee_role=self.default_escalation_assignee_role,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ShadowLedgerEntry(BaseModel):
    """Immutable audit record emitted for every governed autonomy decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: str
    run_id: str
    action: str
    actor_id: str
    actor_role: str
    mode: AutonomyMode
    effective_mode: AutonomyMode
    outcome: str
    valid: bool
    committed: bool
    error_code: str | None = None
    error_message: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    diff: dict[str, Any] = Field(default_factory=dict)
    evaluated_value: float | None = None
    ceiling_max_value: float | None = None
    escalation_run_id: str | None = None


class AutonomyDecision(BaseModel):
    """Structured result returned by ``AutonomyRampController.execute``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: AutonomyMode
    effective_mode: AutonomyMode
    outcome: str
    action: str
    run_id: str
    committed: bool = False
    valid: bool
    simulated: dict[str, Any]
    run: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None
    evaluated_value: float | None = None
    max_value: float | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AutonomyRampController:
    """Thread-safe M2 controller enforcing SHADOW / COPILOT_ESCALATE / AUTO_UNDER_CEILING."""

    def __init__(
        self,
        policy: AutonomyPolicy,
        *,
        ledger_path: Path | str | None = None,
        escalation_payload_builder: Callable[..., dict[str, Any]] | None = None,
        value_extractor: Callable[[Mapping[str, Any], SimulationResult], float | int | None] | None = None,
    ) -> None:
        self.policy = policy
        self.ledger_path = Path(ledger_path) if ledger_path is not None else None
        self.escalation_payload_builder = escalation_payload_builder
        self.value_extractor = value_extractor
        self._lock = threading.RLock()
        self._ledger: list[ShadowLedgerEntry] = []

    @property
    def ledger(self) -> list[ShadowLedgerEntry]:
        with self._lock:
            return list(self._ledger)

    def _append_ledger(self, entry: ShadowLedgerEntry) -> None:
        self._ledger.append(entry)
        if self.ledger_path is not None:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def _extract_metric_value(
        self,
        rule: ActionAutonomyRule,
        params: Mapping[str, Any],
        sim: SimulationResult,
    ) -> float | None:
        if self.value_extractor is not None:
            return _to_number(self.value_extractor(dict(params), sim))
        field_name = rule.ceiling_field
        if not field_name:
            return None
        direct = _to_number(params.get(field_name))
        if direct is not None:
            return direct
        found: list[float] = []
        for change in sim.diff.object_changes:
            for snapshot in (change.after, change.before):
                if isinstance(snapshot, Mapping) and field_name in snapshot:
                    num = _to_number(snapshot.get(field_name))
                    if num is not None:
                        found.append(num)
        return max(found) if found else None

    def _build_escalation_payload(
        self,
        *,
        run_id: str,
        action: str,
        params: Mapping[str, Any],
        actor: Actor,
        sim: SimulationResult,
        rule: ActionAutonomyRule,
        reason: str,
        evaluated_value: float | None,
    ) -> dict[str, Any]:
        ctx = {
            "run_id": run_id,
            "action": action,
            "params": dict(params),
            "actor": actor,
            "simulated": sim,
            "rule": rule,
            "reason": reason,
            "evaluated_value": evaluated_value,
        }
        builder = self.escalation_payload_builder
        if builder is None:
            return {
                "requested_action": action,
                "requested_params": dict(params),
                "requester_actor_id": actor.actor_id,
                "assignee_role": rule.escalation_assignee_role or "",
                "reason": reason,
            }
        sig = inspect.signature(builder)
        positional = [
            p
            for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if has_kwargs:
            return dict(builder(**ctx))
        if len(positional) == 1 and not any(name in ctx for name in sig.parameters):
            return dict(builder(ctx))
        accepted = {k: v for k, v in ctx.items() if k in sig.parameters}
        if accepted:
            return dict(builder(**accepted))
        return dict(builder(ctx))

    def _escalate(
        self,
        runtime: OntologyRuntime,
        actor: Actor,
        run_id: str,
        action: str,
        params: dict[str, Any],
        sim: SimulationResult,
        rule: ActionAutonomyRule,
        *,
        reason: str,
        evaluated_value: float | None = None,
    ) -> AutonomyDecision:
        payload = self._build_escalation_payload(
            run_id=run_id,
            action=action,
            params=params,
            actor=actor,
            sim=sim,
            rule=rule,
            reason=reason,
            evaluated_value=evaluated_value,
        )
        esc_action = rule.escalation_action
        if esc_action:
            esc_run_id = f"{run_id}:escalate"
            esc_run = runtime.submit_action(esc_run_id, esc_action, payload, actor)
            decision = AutonomyDecision(
                mode=rule.mode,
                effective_mode="COPILOT_ESCALATE",
                outcome="ESCALATED_COMMITTED",
                action=action,
                run_id=run_id,
                committed=True,
                valid=True,
                simulated=sim.to_dict(),
                run=esc_run,
                escalation={
                    "reason": reason,
                    "escalation_action": esc_action,
                    "escalation_run_id": esc_run_id,
                    "payload": payload,
                },
                evaluated_value=evaluated_value,
                max_value=rule.max_value,
            )
            self._append_ledger(
                ShadowLedgerEntry(
                    timestamp=_utc_now_iso(),
                    run_id=run_id,
                    action=action,
                    actor_id=actor.actor_id,
                    actor_role=actor.role,
                    mode=rule.mode,
                    effective_mode="COPILOT_ESCALATE",
                    outcome="ESCALATED_COMMITTED",
                    valid=True,
                    committed=False,
                    params=params,
                    diff=sim.diff.to_dict(),
                    evaluated_value=evaluated_value,
                    ceiling_max_value=rule.max_value,
                    escalation_run_id=esc_run_id,
                )
            )
            return decision

        decision = AutonomyDecision(
            mode=rule.mode,
            effective_mode="COPILOT_ESCALATE",
            outcome="ESCALATION_REQUIRED",
            action=action,
            run_id=run_id,
            committed=False,
            valid=True,
            simulated=sim.to_dict(),
            run=None,
            escalation={
                "reason": reason,
                "escalation_action": None,
                "payload": payload,
            },
            evaluated_value=evaluated_value,
            max_value=rule.max_value,
        )
        self._append_ledger(
            ShadowLedgerEntry(
                timestamp=_utc_now_iso(),
                run_id=run_id,
                action=action,
                actor_id=actor.actor_id,
                actor_role=actor.role,
                mode=rule.mode,
                effective_mode="COPILOT_ESCALATE",
                outcome="ESCALATION_REQUIRED",
                valid=True,
                committed=False,
                params=params,
                diff=sim.diff.to_dict(),
                evaluated_value=evaluated_value,
                ceiling_max_value=rule.max_value,
            )
        )
        return decision

    def execute(
        self,
        runtime: OntologyRuntime,
        actor: Actor,
        run_id: str,
        action: str,
        params: Mapping[str, Any] | None = None,
    ) -> AutonomyDecision:
        """Evaluate ``action`` under the resolved autonomy rule without leaking unsafe writes."""
        with self._lock:
            params_dict = dict(params or {})
            rule = self.policy.resolve(action, actor.role)
            sim = runtime.simulate_action(action, params_dict, actor)

            if rule.mode == "SHADOW":
                outcome = "SHADOW_RECORDED" if sim.valid else "SHADOW_WOULD_REJECT"
                self._append_ledger(
                    ShadowLedgerEntry(
                        timestamp=_utc_now_iso(),
                        run_id=run_id,
                        action=action,
                        actor_id=actor.actor_id,
                        actor_role=actor.role,
                        mode=rule.mode,
                        effective_mode="SHADOW",
                        outcome=outcome,
                        valid=bool(sim.valid),
                        committed=False,
                        error_code=sim.error_code,
                        error_message=sim.error,
                        params=params_dict,
                        diff=sim.diff.to_dict(),
                    )
                )
                return AutonomyDecision(
                    mode=rule.mode,
                    effective_mode="SHADOW",
                    outcome=outcome,
                    action=action,
                    run_id=run_id,
                    committed=False,
                    valid=bool(sim.valid),
                    simulated=sim.to_dict(),
                    run=None,
                    error_code=sim.error_code,
                    error_message=sim.error,
                )

            if not sim.valid:
                self._append_ledger(
                    ShadowLedgerEntry(
                        timestamp=_utc_now_iso(),
                        run_id=run_id,
                        action=action,
                        actor_id=actor.actor_id,
                        actor_role=actor.role,
                        mode=rule.mode,
                        effective_mode=rule.mode,
                        outcome="COPILOT_REJECTED" if rule.mode == "COPILOT_ESCALATE" else "AUTO_REJECTED",
                        valid=False,
                        committed=False,
                        error_code=sim.error_code,
                        error_message=sim.error,
                        params=params_dict,
                        diff=sim.diff.to_dict(),
                    )
                )
                return AutonomyDecision(
                    mode=rule.mode,
                    effective_mode=rule.mode,
                    outcome="COPILOT_REJECTED" if rule.mode == "COPILOT_ESCALATE" else "AUTO_REJECTED",
                    action=action,
                    run_id=run_id,
                    committed=False,
                    valid=False,
                    simulated=sim.to_dict(),
                    run=None,
                    error_code=sim.error_code,
                    error_message=sim.error,
                )

            if rule.mode == "COPILOT_ESCALATE":
                return self._escalate(
                    runtime,
                    actor,
                    run_id,
                    action,
                    params_dict,
                    sim,
                    rule,
                    reason="policy_requires_escalation",
                )

            # AUTO_UNDER_CEILING: fail-closed whenever metric missing or exceeds max_value
            evaluated = self._extract_metric_value(rule, params_dict, sim)
            if rule.max_value is None or evaluated is None:
                return self._escalate(
                    runtime,
                    actor,
                    run_id,
                    action,
                    params_dict,
                    sim,
                    rule,
                    reason="missing_ceiling_metric",
                    evaluated_value=evaluated,
                )
            if float(evaluated) > float(rule.max_value):
                return self._escalate(
                    runtime,
                    actor,
                    run_id,
                    action,
                    params_dict,
                    sim,
                    rule,
                    reason="ceiling_exceeded",
                    evaluated_value=evaluated,
                )

            run = runtime.submit_action(run_id, action, params_dict, actor)
            self._append_ledger(
                ShadowLedgerEntry(
                    timestamp=_utc_now_iso(),
                    run_id=run_id,
                    action=action,
                    actor_id=actor.actor_id,
                    actor_role=actor.role,
                    mode=rule.mode,
                    effective_mode="AUTO_UNDER_CEILING",
                    outcome="AUTO_COMMITTED",
                    valid=True,
                    committed=True,
                    params=params_dict,
                    diff=sim.diff.to_dict(),
                    evaluated_value=evaluated,
                    ceiling_max_value=rule.max_value,
                )
            )
            return AutonomyDecision(
                mode=rule.mode,
                effective_mode="AUTO_UNDER_CEILING",
                outcome="AUTO_COMMITTED",
                action=action,
                run_id=run_id,
                committed=True,
                valid=True,
                simulated=sim.to_dict(),
                run=run,
                evaluated_value=evaluated,
                max_value=rule.max_value,
            )

    def shadow_report(self) -> dict[str, Any]:
        """Return aggregate telemetry for shadow ramp-up and escalation analysis."""
        with self._lock:
            shadow_entries = [e for e in self._ledger if e.mode == "SHADOW"]
            shadow_ok = sum(1 for e in shadow_entries if e.outcome == "SHADOW_RECORDED")
            shadow_rej = sum(1 for e in shadow_entries if e.outcome == "SHADOW_WOULD_REJECT")
            error_dist: dict[str, int] = {}
            outcome_dist: dict[str, int] = {}
            for entry in self._ledger:
                outcome_dist[entry.outcome] = outcome_dist.get(entry.outcome, 0) + 1
                if entry.error_code:
                    error_dist[entry.error_code] = error_dist.get(entry.error_code, 0) + 1
            return {
                "total": len(self._ledger),
                "shadow_total": len(shadow_entries),
                "shadow_would_approve": shadow_ok,
                "shadow_would_reject": shadow_rej,
                "shadow_would_approve_rate": (
                    round(shadow_ok / len(shadow_entries), 4) if shadow_entries else 0.0
                ),
                "auto_committed": sum(1 for e in self._ledger if e.outcome == "AUTO_COMMITTED"),
                "escalated": sum(
                    1 for e in self._ledger if e.outcome in ("ESCALATED_COMMITTED", "ESCALATION_REQUIRED")
                ),
                "error_code_distribution": error_dist,
                "outcome_distribution": outcome_dist,
            }
