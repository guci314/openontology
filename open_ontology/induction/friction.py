"""M0 生产摩擦信号只读采集器与防噪聚类门禁（纯 M2，领域无关）。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ..runtime import OntologyRuntime
from ..settlement import DEVICE_STATE_MISMATCH

__all__ = [
    "FrictionEvidence",
    "FrictionClusterSummary",
    "FrictionCollector",
]


@dataclass(frozen=True)
class FrictionEvidence:
    signal_kind: Literal[
        "DEVICE_STATE_MISMATCH",
        "REVERTED_INCIDENT",
        "APPROVAL_PRECEDENT",
        "REPEATED_REJECTION",
        "PLANNER_DEAD_END",
    ]
    run_id: str
    action_type: str
    actor_role: str
    signature: str
    params: dict[str, Any]
    summary: str
    error_code: str | None = None
    object_diff: dict[str, Any] = field(default_factory=dict)
    force_escalate: bool = False


@dataclass(frozen=True)
class FrictionClusterSummary:
    cluster_id: str
    signal_kind: str
    action_type: str
    signature: str
    support_count: int
    escalated: bool
    run_ids: tuple[str, ...]
    evidences: tuple[FrictionEvidence, ...]


class FrictionCollector:
    """Read-only M0 audit scanner that clusters friction signals and filters single-event noise."""

    def __init__(self, *, min_support: int = 2) -> None:
        if min_support < 1:
            raise ValueError("min_support must be >= 1")
        self.min_support = min_support

    def collect(
        self,
        runtime: OntologyRuntime,
        *,
        revert_reasons: dict[str, str] | None = None,
        planner_dead_ends: Sequence[dict[str, Any]] = (),
        force_escalate_runs: set[str] | None = None,
    ) -> list[FrictionClusterSummary]:
        reasons = revert_reasons or {}
        forced = force_escalate_runs or set()
        runs = runtime.list_action_runs()
        evidences: list[FrictionEvidence] = []

        for run in runs:
            run_id = str(run["run_id"])
            status = str(run["status"])
            action_type = str(run["action_type"])
            actor_role = str(run["actor_role"])
            error_code = run.get("error_code")
            params = dict(run.get("params") or {})
            obj_diff = dict(run.get("object_diff") or {})

            if status == "REJECTED" and error_code == DEVICE_STATE_MISMATCH:
                sig = f"mismatch:{action_type}:{error_code}"
                evidences.append(
                    FrictionEvidence(
                        signal_kind="DEVICE_STATE_MISMATCH",
                        run_id=run_id,
                        action_type=action_type,
                        actor_role=actor_role,
                        signature=sig,
                        params=params,
                        summary=str(run.get("error_message") or "external readback mismatch"),
                        error_code=str(error_code),
                        object_diff=obj_diff,
                        force_escalate=run_id in forced,
                    )
                )
            elif status == "REVERTED":
                reason = (
                    reasons.get(run_id)
                    or reasons.get(str(run.get("reverted_by_run_id") or ""))
                    or "reverted_post_commit"
                )
                sig = f"reverted:{action_type}:{reason}"
                evidences.append(
                    FrictionEvidence(
                        signal_kind="REVERTED_INCIDENT",
                        run_id=run_id,
                        action_type=action_type,
                        actor_role=actor_role,
                        signature=sig,
                        params=params,
                        summary=reason,
                        error_code="REVERTED_AFTER_COMMIT",
                        object_diff=obj_diff,
                        force_escalate=run_id in forced,
                    )
                )
            elif status == "REJECTED" and error_code == "APPROVAL_REJECTED":
                sig = f"approval_rejected:{action_type}:{actor_role}"
                evidences.append(
                    FrictionEvidence(
                        signal_kind="APPROVAL_PRECEDENT",
                        run_id=run_id,
                        action_type=action_type,
                        actor_role=actor_role,
                        signature=sig,
                        params=params,
                        summary=str(run.get("error_message") or "rejected by approver"),
                        error_code=str(error_code),
                        object_diff=obj_diff,
                        force_escalate=run_id in forced,
                    )
                )
            elif status == "REJECTED" and error_code:
                sig = f"rejected:{action_type}:{error_code}"
                evidences.append(
                    FrictionEvidence(
                        signal_kind="REPEATED_REJECTION",
                        run_id=run_id,
                        action_type=action_type,
                        actor_role=actor_role,
                        signature=sig,
                        params=params,
                        summary=str(run.get("error_message") or str(error_code)),
                        error_code=str(error_code),
                        object_diff=obj_diff,
                        force_escalate=run_id in forced,
                    )
                )

        for idx, de in enumerate(planner_dead_ends):
            action_type = str(de.get("action_type") or "planner")
            reason = str(de.get("reason") or "MPC_UNREACHABLE")
            sig = f"dead_end:{action_type}:{reason}"
            evidences.append(
                FrictionEvidence(
                    signal_kind="PLANNER_DEAD_END",
                    run_id=str(de.get("run_id") or f"dead-end-{idx + 1}"),
                    action_type=action_type,
                    actor_role=str(de.get("actor_role") or "ai_planner"),
                    signature=sig,
                    params=dict(de.get("params") or {}),
                    summary=reason,
                    error_code=reason,
                    force_escalate=bool(de.get("force_escalate", False)),
                )
            )

        grouped: dict[tuple[str, str, str], list[FrictionEvidence]] = defaultdict(list)
        for ev in evidences:
            grouped[(ev.signal_kind, ev.action_type, ev.signature)].append(ev)

        clusters: list[FrictionClusterSummary] = []
        for idx, ((kind, act, sig), items) in enumerate(sorted(grouped.items()), start=1):
            support = len(items)
            escalated = support >= self.min_support or any(i.force_escalate for i in items)
            clusters.append(
                FrictionClusterSummary(
                    cluster_id=f"FRC-{idx:03d}",
                    signal_kind=kind,
                    action_type=act,
                    signature=sig,
                    support_count=support,
                    escalated=escalated,
                    run_ids=tuple(i.run_id for i in items),
                    evidences=tuple(items),
                )
            )
        return clusters
