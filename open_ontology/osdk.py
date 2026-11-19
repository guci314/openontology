"""L5 OSDK: LLM tool export + deterministic AIP evals.

Two pieces:

* :func:`export_llm_tools` turns the declarative spec into OpenAI-style tool
  JSON Schemas (functions + actions).
* :class:`AIPAgentExecutor` is the single dispatcher an LLM agent calls, and
  :func:`run_eval_suite` deterministically grades the ``ObjectDiff`` produced
  by an agent decision -- no LLM judge, no randomness.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Sequence

from pydantic import BaseModel, Field

from .model_views import describe_tbox, resolve_scoped_names
from .query import ObjectSet
from .runtime import OntologyRuntime, SimulationResult
from .spec import ActionDef, Actor, InvariantError, OntologySpec, PermissionDeniedError, ScopedViewSpec

__all__ = [
    "export_llm_tools",
    "discover_skills",
    "UIArtifact",
    "AgentTurnResult",
    "AIPAgentExecutor",
    "run_agent_turn",
    "iter_agent_turn",
    "EvalCaseResult",
    "EvalReport",
    "run_eval_suite",
]


# ---------------------------------------------------------------------------
# UI Artifact protocol (v1 + v2)
# ---------------------------------------------------------------------------
ArtifactKind = Literal[
    "object_table",
    "action_form",
    "diff_preview",
    "approval_card",
    "ui_command",
    "metric_chart",
    "mcts_plan_card",
]


class UIArtifact(BaseModel):
    """Structured UI artifact emitted by an AIP agent for frontend rendering."""

    artifact_id: str = Field(
        default_factory=lambda: f"art-{uuid.uuid4().hex[:8]}"
    )
    kind: ArtifactKind
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass
class AgentTurnResult:
    """Result of a single agent conversation turn."""

    reply_text: str
    artifacts: list[UIArtifact] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply_text": self.reply_text,
            "artifacts": [a.model_dump(mode="json") for a in self.artifacts],
            "tool_calls": list(self.tool_calls),
        }


# ---------------------------------------------------------------------------
# Tool schema export
# ---------------------------------------------------------------------------
def _params_schema(params_cls: type[BaseModel]) -> dict[str, Any]:
    schema = params_cls.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    # Some providers require an explicit object schema even for empty params.
    if schema.get("type") == "object" and "additionalProperties" not in schema:
        schema["additionalProperties"] = False
    return schema


def export_llm_tools(
    spec: OntologySpec,
    *,
    actor: Actor | None = None,
    role: str | None = None,
    view: str | ScopedViewSpec | None = None,
    view_tags: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Export registered function/action tools, optionally narrowed by actor/role/ScopedViewSpec."""
    scoped = resolve_scoped_names(
        spec,
        actor=actor,
        role=role,
        view=view,
        view_tags=view_tags,
        public_only=True,
    )
    tools: list[dict[str, Any]] = []

    for fn in spec.functions.values():
        if fn.name not in scoped["functions"]:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": fn.name,
                    "description": fn.description
                    or f"Read-only ontology function {fn.name}",
                    "parameters": _params_schema(fn.params),
                },
                "x-ontology-kind": "function",
            }
        )

    for action in spec.actions.values():
        if action.name not in scoped["actions"]:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": action.name,
                    "description": action.description
                    or f"Governed ontology action {action.name}",
                    "parameters": _params_schema(action.params),
                },
                "x-ontology-kind": "action",
                "x-allowed-roles": list(action.allowed_roles),
                "x-approver-roles": list(action.approver_roles),
            }
        )

    return tools


# ---------------------------------------------------------------------------
# Agent executor
# ---------------------------------------------------------------------------
class AIPAgentExecutor:
    """Single entry point for an LLM agent.

    Supported tools::

        query_objects   {object_type, filters?}
        get_object      {object_type, object_id}
        get_linked      {source_type, source_id, link, target_type}
        pivot           {object_type, filters?/ids?, link, direction?}
        aggregate       {object_type, filters?, group_by?, sum_field?}
        call_function   {name, params?}
        simulate_action {action, params?}
        simulate_sequence {steps:[{action, params?}, ...], value_function?}
        plan_mcts       {candidates:[[{action, params?, prior?}, ...], ...],
                         value_function?, simulations?, c_puct?, seed?}
        submit_action   {run_id, action, params?}
        list_action_runs{status?}
        emit_ui_artifact{kind, title, payload, artifact_id?}
    """

    def __init__(
        self,
        runtime: OntologyRuntime,
        actor: Actor,
        *,
        view: str | ScopedViewSpec | None = None,
        view_tags: Sequence[str] | None = None,
        enforce_view_scope: bool = True,
    ) -> None:
        self.runtime = runtime
        self.actor = actor
        self.view = view
        self.view_tags = tuple(view_tags) if view_tags is not None else None
        self.enforce_view_scope = bool(enforce_view_scope)
        self._artifacts: list[UIArtifact] = []

    # -- artifact helpers --------------------------------------------------
    def clear_artifacts(self) -> None:
        self._artifacts.clear()

    def pop_artifacts(self) -> list[UIArtifact]:
        out = list(self._artifacts)
        self._artifacts.clear()
        return out

    def build_object_table_artifact(
        self,
        object_type: str,
        rows: list[dict[str, Any]],
        title: str | None = None,
        columns: list[str] | None = None,
    ) -> UIArtifact:
        payload: dict[str, Any] = {"object_type": object_type, "rows": rows}
        if columns:
            payload["columns"] = list(columns)
        return UIArtifact(
            kind="object_table",
            title=title or f"{object_type} 列表",
            payload=payload,
        )

    def build_action_form_artifact(
        self,
        action: str,
        prefill_params: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> UIArtifact:
        action_def = self.runtime.spec.get_action(action)
        return UIArtifact(
            kind="action_form",
            title=title or f"执行动作：{action}",
            payload={
                "action": action,
                "prefill_params": dict(prefill_params or {}),
                "schema": _params_schema(action_def.params),
            },
        )

    def build_diff_preview_artifact(
        self,
        action: str,
        params: dict[str, Any],
        simulation: SimulationResult | dict[str, Any],
        title: str | None = None,
    ) -> UIArtifact:
        sim_dict = (
            simulation.to_dict()
            if isinstance(simulation, SimulationResult)
            else dict(simulation)
        )
        return UIArtifact(
            kind="diff_preview",
            title=title or f"沙盘预演：{action}",
            payload={
                "action": action,
                "params": dict(params),
                "simulation": sim_dict,
            },
        )

    def build_approval_card_artifact(
        self,
        run: dict[str, Any],
        title: str | None = None,
    ) -> UIArtifact:
        return UIArtifact(
            kind="approval_card",
            title=title or f"待审批：{run.get('action_type', '')}",
            payload={
                "run_id": run.get("run_id"),
                "action": run.get("action_type"),
                "status": run.get("status"),
                "actor_role": run.get("actor_role"),
                "diff": run.get("object_diff") or {},
            },
        )

    def build_ui_command_artifact(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> UIArtifact:
        return UIArtifact(
            kind="ui_command",
            title=title or f"界面联动：{command}",
            payload={"command": command, **(params or {})},
        )

    def build_metric_chart_artifact(
        self,
        object_type: str,
        agg_result: dict[str, Any],
        title: str | None = None,
    ) -> UIArtifact:
        rows = agg_result.get("rows") or []
        bars = [
            {
                "label": str(row.get("key") if row.get("key") is not None else "总计"),
                "count": int((row.get("metrics") or {}).get("count", len(row.get("object_ids") or []))),
                "sum": (row.get("metrics") or {}).get("sum"),
            }
            for row in rows
        ]
        total = int(agg_result.get("total", sum(b["count"] for b in bars)))
        return UIArtifact(
            kind="metric_chart",
            title=title or f"{object_type} 分布统计",
            payload={
                "object_type": object_type,
                "total": total,
                "bars": bars,
            },
        )

    def build_mcts_plan_card_artifact(
        self,
        mcts_result: dict[str, Any],
        title: str | None = None,
    ) -> UIArtifact:
        return UIArtifact(
            kind="mcts_plan_card",
            title=title or "MCTS 多步推演规划",
            payload={
                "best_value": mcts_result.get("best_value"),
                "best_sequence": mcts_result.get("best_plan_labels")
                or mcts_result.get("best_sequence")
                or [],
                "best_plan": mcts_result.get("best_plan") or [],
                "simulations": mcts_result.get("simulations"),
                "root_actions": mcts_result.get("root_actions") or [],
            },
        )

    # -- introspection -----------------------------------------------------
    def _has_explicit_scope(self) -> bool:
        return (self.view is not None or self.view_tags is not None) and self.enforce_view_scope

    def _scoped_names(self) -> dict[str, set[str]]:
        return resolve_scoped_names(
            self.runtime.spec,
            actor=self.actor,
            view=self.view,
            view_tags=self.view_tags,
            public_only=True,
        )

    def _require_scoped_object_type(self, object_type: str) -> None:
        if not self._has_explicit_scope():
            return
        if object_type not in self._scoped_names()["object_types"]:
            raise PermissionDeniedError(
                f"object type {object_type!r} is outside the active scoped view"
            )

    def _require_scoped_function(self, function_name: str) -> None:
        if not self._has_explicit_scope():
            return
        if function_name not in self._scoped_names()["functions"]:
            raise PermissionDeniedError(
                f"function {function_name!r} is outside the active scoped view"
            )

    def tools(self) -> list[dict[str, Any]]:
        return export_llm_tools(
            self.runtime.spec,
            actor=self.actor,
            view=self.view,
            view_tags=self.view_tags,
        )

    def describe_tbox(self) -> dict[str, Any]:
        return describe_tbox(
            self.runtime.spec,
            actor=self.actor,
            view=self.view,
            view_tags=self.view_tags,
        )

    def _require_public_action(self, action_name: str) -> ActionDef:
        """拒绝 Agent 访问或调用 ``visibility='private'`` 或超出当前 Scoped View 的动作。"""
        action = self.runtime.spec.get_action(action_name)
        if action.visibility == "private":
            raise PermissionDeniedError(
                f"action {action_name!r} is private and cannot be invoked by agents"
            )
        if self._has_explicit_scope() and action_name not in self._scoped_names()["actions"]:
            raise PermissionDeniedError(
                f"action {action_name!r} is outside the active scoped view"
            )
        return action

    # -- dispatch ----------------------------------------------------------
    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | str | None = None,
    ) -> Any:
        args = self._coerce_arguments(arguments)

        if tool_name == "query_objects":
            cls = self.runtime.spec.get_object_type(args["object_type"])
            self._require_scoped_object_type(cls.__name__)
            objects = self.runtime.query_objects(
                cls, self.actor, args.get("filters")
            )
            return {
                "object_type": cls.__name__,
                "count": len(objects),
                "objects": [o.model_dump(mode="json") for o in objects],
            }

        if tool_name == "get_object":
            cls = self.runtime.spec.get_object_type(args["object_type"])
            self._require_scoped_object_type(cls.__name__)
            obj = self.runtime.get_object(cls, args["object_id"], self.actor)
            return {"object": obj.model_dump(mode="json")}

        if tool_name == "get_linked":
            source_cls = self.runtime.spec.get_object_type(args["source_type"])
            target_cls = self.runtime.spec.get_object_type(args["target_type"])
            self._require_scoped_object_type(source_cls.__name__)
            self._require_scoped_object_type(target_cls.__name__)
            source = self.runtime.get_object(
                source_cls, args["source_id"], self.actor
            )
            linked = self.runtime.get_linked(
                source, args["link"], target_cls, self.actor
            )
            return {
                "count": len(linked),
                "objects": [o.model_dump(mode="json") for o in linked],
            }

        if tool_name == "pivot":
            cls = self.runtime.spec.get_object_type(args["object_type"])
            self._require_scoped_object_type(cls.__name__)
            if "ids" in args:
                source_objects = [
                    self.runtime.get_object(cls, object_id, self.actor)
                    for object_id in args["ids"]
                ]
                source_set = ObjectSet(cls.__name__, source_objects)
            else:
                source_set = self.runtime.search_set(
                    cls, self.actor, args.get("filters")
                )
            result = self.runtime.pivot(
                source_set,
                args["link"],
                self.actor,
                args.get("direction"),
            )
            self._require_scoped_object_type(result.object_type)
            return {
                "object_type": result.object_type,
                "count": len(result),
                "ids": result.ids,
                "objects": [o.model_dump(mode="json") for o in result],
            }

        if tool_name == "aggregate":
            cls = self.runtime.spec.get_object_type(args["object_type"])
            self._require_scoped_object_type(cls.__name__)
            source_set = self.runtime.search_set(
                cls, self.actor, args.get("filters")
            )
            agg = self.runtime.aggregate(
                source_set,
                group_by=args.get("group_by"),
                sum_field=args.get("sum_field"),
            )
            return agg.to_dict()

        if tool_name == "call_function":
            self._require_scoped_function(args["name"])
            result = self.runtime.call_function(
                args["name"], args.get("params"), self.actor
            )
            return {"ok": True, "result": result}

        if tool_name == "simulate_action":
            self._require_public_action(args["action"])
            sim = self.runtime.simulate_action(
                args["action"], args.get("params", {}), self.actor
            )
            return sim.to_dict()

        if tool_name == "simulate_sequence":
            return self._simulate_sequence(args)

        if tool_name == "plan_mcts":
            return self._plan_mcts(args)

        if tool_name == "submit_action":
            self._require_public_action(args["action"])
            run = self.runtime.submit_action(
                args["run_id"],
                args["action"],
                args.get("params", {}),
                self.actor,
            )
            return {"run": run}

        if tool_name == "list_action_runs":
            return {
                "runs": self.runtime.list_action_runs(args.get("status")),
            }

        if tool_name == "emit_ui_artifact":
            kind = str(args["kind"])
            title = str(args.get("title") or kind)
            payload = dict(args.get("payload") or {})
            if kind == "action_form" and "action" in payload and "schema" not in payload:
                action_def = self._require_public_action(str(payload["action"]))
                payload["schema"] = _params_schema(action_def.params)
                payload.setdefault("prefill_params", {})
            art_kwargs: dict[str, Any] = {
                "kind": kind,
                "title": title,
                "payload": payload,
            }
            if args.get("artifact_id"):
                art_kwargs["artifact_id"] = str(args["artifact_id"])
            artifact = UIArtifact(**art_kwargs)
            self._artifacts.append(artifact)
            return {"ok": True, "artifact": artifact.model_dump(mode="json")}

        if tool_name in self.runtime.spec.functions:
            self._require_scoped_function(tool_name)
            result = self.runtime.call_function(tool_name, args, self.actor)
            return {"ok": True, "result": result}

        if tool_name in self.runtime.spec.actions:
            self._require_public_action(tool_name)
            sim = self.runtime.simulate_action(tool_name, args, self.actor)
            return {"simulated_only": True, "action": tool_name, **sim.to_dict()}

        raise ValueError(f"unknown tool: {tool_name!r}")

    # -- multi-step sandbox tools -----------------------------------------
    def _simulate_sequence(self, args: dict[str, Any]) -> dict[str, Any]:
        """多步连续沙盘推演：第 k 步跑在前 k-1 步修改后的沙盘状态上。

        全部试错发生在 ``runtime.fork()`` 分支里，主库零修改；任何一步触发
        ``InvariantError`` 立即停下并返回失败步号与错误码。
        """
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("simulate_sequence requires a non-empty 'steps' list")

        before = self.runtime.state_fingerprint()
        fork = self.runtime.fork()
        records: list[dict[str, Any]] = []
        failed: dict[str, Any] | None = None
        value: Any = None
        try:
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict) or "action" not in step:
                    raise ValueError(
                        f"step {index} must be an object with an 'action' field"
                    )
                action_name = str(step["action"])
                self._require_public_action(action_name)
                params = step.get("params") or {}
                try:
                    run = fork.submit_action(
                        f"SIMSEQ-{index:04d}", action_name, params, self.actor
                    )
                except InvariantError as exc:
                    failed = {
                        "step": index,
                        "action": action_name,
                        "params": dict(params),
                        "error_code": exc.code,
                        "error": str(exc),
                    }
                    break
                records.append(
                    {
                        "step": index,
                        "action": action_name,
                        "params": dict(params),
                        "status": run["status"],
                        "requires_approval": run["status"] == "PENDING_APPROVAL",
                        "diff": run["object_diff"],
                    }
                )

            value_function = args.get("value_function")
            if value_function and failed is None:
                value = fork.call_function(
                    value_function, args.get("value_params") or {}, self.actor
                )
        finally:
            fork.close()

        after = self.runtime.state_fingerprint()
        return {
            "ok": failed is None,
            "executed_steps": len(records),
            "total_steps": len(steps),
            "steps": records,
            "failed": failed,
            "value": value,
            "main_db_untouched": before == after,
        }

    def _plan_mcts(self, args: dict[str, Any]) -> dict[str, Any]:
        """静态候选表驱动的 MCTS 沙盘规划（候选按步给，先验可显式指定）。"""
        table = args.get("candidates")
        if not isinstance(table, list) or not table:
            raise ValueError(
                "plan_mcts requires a non-empty 'candidates' table (one row per step)"
            )
        value_function = args.get("value_function")
        if not value_function:
            raise ValueError("plan_mcts requires a 'value_function' name")
        self._require_scoped_function(str(value_function))
        for cand_seq in table:
            if isinstance(cand_seq, list):
                for step in cand_seq:
                    if isinstance(step, dict) and "action" in step:
                        self._require_public_action(str(step["action"]))

        result = self.runtime.plan_mcts(
            self.actor,
            candidate_table=table,
            value_function=str(value_function),
            simulations=int(args.get("simulations", 80)),
            c_puct=float(args.get("c_puct", 1.414)),
            seed=int(args.get("seed", 17)),
        )
        return result.to_dict()

    def dispatch_tool_call(self, payload: dict[str, Any]) -> Any:
        """Accept an OpenAI ``tool_call``/``function_call`` shaped payload."""
        if "function" in payload:
            payload = payload["function"]
        return self.dispatch(payload["name"], payload.get("arguments"))

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _coerce_arguments(
        arguments: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, str):
            text = arguments.strip()
            if text == "":
                return {}
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("tool arguments must decode to a JSON object")
            return parsed
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a dict or JSON string")
        return arguments


# ---------------------------------------------------------------------------
# Agent turn orchestration (v1: OpenAI-compatible or deterministic fallback)
# ---------------------------------------------------------------------------
_BUILTIN_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_objects",
            "description": "Query visible objects of a given object_type with optional filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string"},
                    "filters": {"type": "object"},
                },
                "required": ["object_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_action",
            "description": "Simulate an ontology action in a zero-side-effect sandbox and return its ObjectDiff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_action_runs",
            "description": "List recent ontology action runs, optionally filtered by status (e.g. PENDING_APPROVAL).",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "emit_ui_artifact",
            "description": (
                "Emit a structured UI artifact card for the user interface. "
                "Supported kinds: object_table, action_form, diff_preview, approval_card."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "object_table",
                            "action_form",
                            "diff_preview",
                            "approval_card",
                            "ui_command",
                            "metric_chart",
                            "mcts_plan_card",
                        ],
                    },
                    "title": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["kind", "title", "payload"],
            },
        },
    },
]


def _format_ui_context(
    ui_context: dict[str, Any] | None,
    ghost_events: Sequence[dict[str, Any]] | None = None,
) -> str:
    parts: list[str] = []
    if ui_context:
        if ui_context.get("ontology_id"):
            parts.append(f"ontology_id={ui_context['ontology_id']}")
        if ui_context.get("instance_id"):
            parts.append(f"instance_id={ui_context['instance_id']}")
        if ui_context.get("active_object_type"):
            parts.append(f"active_object_type={ui_context['active_object_type']}")
        if ui_context.get("selected_object_ids"):
            parts.append(f"selected_object_ids={ui_context['selected_object_ids']}")
    if ghost_events:
        recent = list(ghost_events)[-5:]
        summary = "; ".join(
            f"{e.get('event_type', 'ui')}:{json.dumps(e.get('detail') or {}, ensure_ascii=False)}"
            for e in recent
        )
        parts.append(f"recent_ghost_events=[{summary}]")
    return "[UI_CONTEXT] " + ", ".join(parts) if parts else ""


def _resolve_selected_ids(
    ui_context: dict[str, Any] | None,
    ghost_events: Sequence[dict[str, Any]] | None,
) -> list[str]:
    selected = list((ui_context or {}).get("selected_object_ids") or [])
    if selected:
        return selected
    for ev in reversed(list(ghost_events or [])):
        if ev.get("event_type") == "selection_changed":
            detail = ev.get("detail") or {}
            ids = detail.get("selected_object_ids") or (
                [detail["object_id"]] if detail.get("object_id") else []
            )
            if ids:
                return list(ids)
    return []


def _deterministic_local_turn(
    executor: AIPAgentExecutor,
    message: str,
    ui_context: dict[str, Any] | None = None,
    ghost_events: Sequence[dict[str, Any]] | None = None,
) -> AgentTurnResult:
    """Zero-dependency deterministic fallback when no external LLM key is set."""
    spec = executor.runtime.spec
    lower = message.lower()
    tool_calls: list[dict[str, Any]] = []
    artifacts: list[UIArtifact] = []
    selected_ids = _resolve_selected_ids(ui_context, ghost_events)

    # 1. Pending approvals
    if any(k in lower for k in ("审批", "approve", "approval", "待审")):
        runs_res = executor.dispatch("list_action_runs", {"status": "PENDING_APPROVAL"})
        tool_calls.append({"name": "list_action_runs", "arguments": {"status": "PENDING_APPROVAL"}})
        pending = runs_res.get("runs") or []
        if not pending:
            return AgentTurnResult(
                reply_text="当前没有待审批的动作记录。",
                artifacts=[],
                tool_calls=tool_calls,
            )
        for run in pending[:5]:
            artifacts.append(executor.build_approval_card_artifact(run))
        return AgentTurnResult(
            reply_text=f"找到 {len(pending)} 条待审批记录，已生成审批卡片供您直接处理：",
            artifacts=artifacts,
            tool_calls=tool_calls,
        )

    # 2. UI focus / navigation command
    if any(k in lower for k in ("聚焦", "跳转", "打开拓扑", "focus", "topology")):
        if "拓扑" in lower or "topology" in lower:
            artifacts.append(executor.build_ui_command_artifact("open_topology"))
            return AgentTurnResult(
                reply_text="已为您切换至领域模型拓扑视图。",
                artifacts=artifacts,
                tool_calls=tool_calls,
            )
        if selected_ids:
            obj_type = (ui_context or {}).get("active_object_type") or next(iter(spec.object_types), "")
            artifacts.append(
                executor.build_ui_command_artifact(
                    "focus_object",
                    {"object_type": obj_type, "object_id": selected_ids[0]},
                )
            )
            return AgentTurnResult(
                reply_text=f"已定位并聚焦到对象 {selected_ids[0]}。",
                artifacts=artifacts,
                tool_calls=tool_calls,
            )

    # 3. Metric chart / aggregation
    if any(k in lower for k in ("统计", "汇总", "图表", "分布", "aggregate", "chart")):
        matched_type = next(
            (name for name in spec.object_types if name.lower() in lower),
            None,
        ) or (ui_context or {}).get("active_object_type") or next(iter(spec.object_types), None)
        if matched_type and matched_type in spec.object_types:
            cls = spec.get_object_type(matched_type)
            group_field = "status" if "status" in cls.model_fields else None
            agg_args: dict[str, Any] = {"object_type": matched_type}
            if group_field:
                agg_args["group_by"] = group_field
            agg_res = executor.dispatch("aggregate", agg_args)
            tool_calls.append({"name": "aggregate", "arguments": agg_args})
            artifacts.append(executor.build_metric_chart_artifact(matched_type, agg_res))
            return AgentTurnResult(
                reply_text=f"已完成 {matched_type} 聚合统计（共 {agg_res.get('total', 0)} 条）：",
                artifacts=artifacts,
                tool_calls=tool_calls,
            )

    # 4. Match explicit object type query before generic action keywords
    matched_type = next(
        (name for name in spec.object_types if name.lower() in lower),
        None,
    )
    has_query_intent = any(k in lower for k in ("查", "列", "看", "对象", "数据", "list", "query", "show"))
    if matched_type and has_query_intent:
        res = executor.dispatch("query_objects", {"object_type": matched_type})
        tool_calls.append({"name": "query_objects", "arguments": {"object_type": matched_type}})
        rows = res.get("objects") or []
        artifacts.append(executor.build_object_table_artifact(matched_type, rows))
        return AgentTurnResult(
            reply_text=f"已查询 {matched_type}，共返回 {len(rows)} 条可见记录：",
            artifacts=artifacts,
            tool_calls=tool_calls,
        )

    # 5. Match explicit action name or action intent
    public_actions = [
        name for name, act in spec.actions.items() if act.visibility != "private"
    ]
    matched_action = next(
        (name for name in public_actions if name.lower() in lower),
        None,
    )
    if matched_action or any(k in lower for k in ("表单", "执行", "动作", "模拟", "处理", "action", "simulate")):
        action_name = matched_action or next(iter(public_actions), None)
        if action_name:
            prefill: dict[str, Any] = {}
            if selected_ids:
                action_def = spec.get_action(action_name)
                prop_names = list(action_def.params.model_fields.keys())
                id_candidates = [p for p in prop_names if p == "object_id" or p.endswith("_id")]
                if len(id_candidates) == 1:
                    prefill[id_candidates[0]] = selected_ids[0]
            artifacts.append(
                executor.build_action_form_artifact(action_name, prefill_params=prefill)
            )
            return AgentTurnResult(
                reply_text=f"已为您准备好动作「{action_name}」的交互表单，可先沙盘模拟再确认提交：",
                artifacts=artifacts,
                tool_calls=tool_calls,
            )

    # 5. Match object type query or active_object_type
    matched_type = next(
        (name for name in spec.object_types if name.lower() in lower),
        None,
    )
    if matched_type is None and any(k in lower for k in ("查", "列", "看", "对象", "数据", "list", "query", "show")):
        ctx_type = (ui_context or {}).get("active_object_type")
        if ctx_type and ctx_type in spec.object_types:
            matched_type = ctx_type
        elif spec.object_types:
            matched_type = next(iter(spec.object_types))

    if matched_type:
        res = executor.dispatch("query_objects", {"object_type": matched_type})
        tool_calls.append({"name": "query_objects", "arguments": {"object_type": matched_type}})
        rows = res.get("objects") or []
        artifacts.append(executor.build_object_table_artifact(matched_type, rows))
        return AgentTurnResult(
            reply_text=f"已查询 {matched_type}，共返回 {len(rows)} 条可见记录：",
            artifacts=artifacts,
            tool_calls=tool_calls,
        )

    # 6. General help / ontology summary
    obj_list = "、".join(spec.object_types.keys()) or "无"
    act_list = "、".join(
        name for name, act in spec.actions.items() if act.visibility != "private"
    ) or "无"
    ctx_note = _format_ui_context(ui_context, ghost_events)
    reply = (
        f"当前本体包含对象类型：{obj_list}；可执行动作：{act_list}。"
        "您可以让我「查询某个对象列表」、「统计对象分布」、「打开某个动作表单」或「查看待审批记录」。"
    )
    if ctx_note:
        reply += f"\n（已感知上下文：{ctx_note}）"
    return AgentTurnResult(reply_text=reply, artifacts=[], tool_calls=tool_calls)


_SKILL_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def discover_skills(
    skills_dirs: Sequence[str | Path] | None = None,
) -> list[dict[str, str]]:
    """Discover standard ``SKILL.md`` skills (name, description, path).

    Compatible with OpenAI Agents SDK ``ShellToolLocalSkill`` metadata format.
    """
    if skills_dirs is not None:
        roots = [Path(d) for d in skills_dirs]
    else:
        env_dirs = os.environ.get("OPEN_ONTOLOGY_SKILLS_DIR", "").strip()
        if env_dirs:
            roots = [Path(p) for p in env_dirs.split(os.pathsep) if p.strip()]
        else:
            roots = [Path(__file__).resolve().parents[1] / "skills"]

    discovered: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            name = skill_md.parent.name
            description = ""
            m = _SKILL_FRONTMATTER_RE.match(text)
            if m:
                for line in m.group(1).splitlines():
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().strip("\"'")
                    if k == "name" and v:
                        name = v
                    elif k == "description" and v:
                        description = v
            if name in seen_names:
                continue
            seen_names.add(name)
            discovered.append(
                {
                    "name": name,
                    "description": description or f"Skill {name}",
                    "path": str(skill_md.parent),
                }
            )
    return discovered


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine safely whether or not a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _build_sdk_session(
    executor: AIPAgentExecutor,
    message: str,
    history: Sequence[dict[str, Any]] | None,
    ui_context: dict[str, Any] | None,
    api_key: str,
    ghost_events: Sequence[dict[str, Any]] | None = None,
    on_tool_call: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]], list[UIArtifact]]:
    """Build an OpenAI Agents SDK ``Agent`` and input payload for one turn."""
    from agents import (
        Agent,
        FunctionTool,
        OpenAIChatCompletionsModel,
        set_tracing_disabled,
    )
    from openai import AsyncOpenAI

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/") or None
    model_name = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    spec = executor.runtime.spec

    skills = discover_skills()
    skills_by_name = {s["name"]: s for s in skills}

    public_action_names = [
        name for name, act in spec.actions.items() if act.visibility != "private"
    ]
    sys_prompt = (
        "You are an enterprise Palantir-style AIP Agent for OpenOntology.\n"
        f"Available object types: {list(spec.object_types.keys())}.\n"
        f"Available actions: {public_action_names}.\n"
        "IMPORTANT UI ARTIFACT RULES:\n"
        "1. Whenever the user asks to execute/perform an action (or open a form), ALWAYS call "
        "emit_ui_artifact with kind='action_form', payload={'action': '<action_name>', 'prefill_params': {...}} "
        "(schema is auto-filled by the backend; do NOT invent schema).\n"
        "2. Whenever you simulate an action (via simulate_action or calling an action tool), ALWAYS call "
        "emit_ui_artifact with kind='diff_preview', payload={'action': '<name>', 'params': {...}, 'simulation': <result>} "
        "so the user can click '确认提交'.\n"
        "3. Whenever you query objects, emit kind='object_table', payload={'object_type': '<type>', 'rows': <objects>}.\n"
        "4. Always reply concisely in Chinese."
    )
    if skills:
        skill_lines = "\n".join(
            f"- {s['name']}: {s['description']}" for s in skills
        )
        sys_prompt += (
            "\n\nAvailable standard skills (call `load_skill` with the skill name "
            "before following a skill's workflow):\n" + skill_lines
        )
    ctx_line = _format_ui_context(ui_context, ghost_events)
    if ctx_line:
        sys_prompt += "\n" + ctx_line

    recorded_calls: list[dict[str, Any]] = []
    auto_artifacts: list[UIArtifact] = []
    executor.clear_artifacts()

    def _record_call(call_info: dict[str, Any]) -> None:
        recorded_calls.append(call_info)
        if on_tool_call is not None:
            on_tool_call(call_info)

    def _make_sdk_tool(tool_def: dict[str, Any]) -> FunctionTool:
        fn_meta = tool_def["function"]
        tool_name = str(fn_meta["name"])
        tool_desc = str(fn_meta.get("description") or tool_name)
        params_schema = dict(
            fn_meta.get("parameters") or {"type": "object", "properties": {}}
        )

        async def _on_invoke(_ctx: Any, raw_args: str) -> str:
            try:
                result = executor.dispatch(tool_name, raw_args)
                parsed_args = executor._coerce_arguments(raw_args)
                if tool_name == "query_objects" and isinstance(result, dict):
                    auto_artifacts.append(
                        executor.build_object_table_artifact(
                            str(
                                result.get("object_type")
                                or parsed_args.get("object_type", "")
                            ),
                            list(result.get("objects") or []),
                        )
                    )
                elif tool_name == "simulate_action" and isinstance(result, dict):
                    auto_artifacts.append(
                        executor.build_diff_preview_artifact(
                            str(parsed_args.get("action", "")),
                            dict(parsed_args.get("params") or {}),
                            result,
                        )
                    )
                elif tool_name in spec.actions and isinstance(result, dict):
                    auto_artifacts.append(
                        executor.build_diff_preview_artifact(
                            tool_name, parsed_args, result
                        )
                    )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
                err_code = getattr(exc, "code", None)
                if err_code:
                    result["error_code"] = err_code
            _record_call({"name": tool_name, "arguments": raw_args})
            return json.dumps(result, ensure_ascii=False)

        return FunctionTool(
            name=tool_name,
            description=tool_desc,
            params_json_schema=params_schema,
            on_invoke_tool=_on_invoke,
            strict_json_schema=False,
        )

    sdk_tools = [
        _make_sdk_tool(td) for td in (_BUILTIN_AGENT_TOOLS + export_llm_tools(spec))
    ]

    if skills_by_name:
        async def _load_skill_invoke(_ctx: Any, raw_args: str) -> str:
            parsed = executor._coerce_arguments(raw_args)
            name = str(parsed.get("name") or "")
            _record_call({"name": "load_skill", "arguments": raw_args})
            entry = skills_by_name.get(name)
            if entry is None:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"unknown skill {name!r}",
                        "available": list(skills_by_name.keys()),
                    },
                    ensure_ascii=False,
                )
            content = (Path(entry["path"]) / "SKILL.md").read_text(encoding="utf-8")
            return json.dumps(
                {"ok": True, "name": name, "path": entry["path"], "content": content},
                ensure_ascii=False,
            )

        sdk_tools.append(
            FunctionTool(
                name="load_skill",
                description="Load the full SKILL.md instructions for a named skill.",
                params_json_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Skill name from the available skills list",
                        }
                    },
                    "required": ["name"],
                },
                on_invoke_tool=_load_skill_invoke,
                strict_json_schema=False,
            )
        )

    set_tracing_disabled(True)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    model_obj = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
    agent = Agent(
        name="OpenOntologyAgent",
        instructions=sys_prompt,
        model=model_obj,
        tools=sdk_tools,
    )

    input_items: list[dict[str, Any]] = []
    for item in history or []:
        if item.get("role") in ("user", "assistant") and item.get("content"):
            input_items.append(
                {"role": item["role"], "content": str(item["content"])}
            )
    input_items.append({"role": "user", "content": message})
    return agent, input_items, recorded_calls, auto_artifacts


def _openai_chat_turn(
    executor: AIPAgentExecutor,
    message: str,
    history: Sequence[dict[str, Any]] | None,
    ui_context: dict[str, Any] | None,
    api_key: str,
    ghost_events: Sequence[dict[str, Any]] | None = None,
) -> AgentTurnResult:
    """Run one conversational turn via OpenAI Agents SDK (``agents``)."""
    from agents import Runner

    agent, input_items, recorded_calls, auto_artifacts = _build_sdk_session(
        executor, message, history, ui_context, api_key, ghost_events
    )
    result = _run_coro_sync(Runner.run(agent, input_items, max_turns=8))
    emitted = executor.pop_artifacts()
    return AgentTurnResult(
        reply_text=str(result.final_output or ""),
        artifacts=emitted if emitted else auto_artifacts,
        tool_calls=recorded_calls,
    )


def _iter_openai_chat_turn(
    executor: AIPAgentExecutor,
    message: str,
    history: Sequence[dict[str, Any]] | None,
    ui_context: dict[str, Any] | None,
    api_key: str,
    ghost_events: Sequence[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """True token-by-token streaming via ``Runner.run_streamed``."""
    from agents import Runner

    ev_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    err_box: list[BaseException] = []

    agent, input_items, recorded_calls, auto_artifacts = _build_sdk_session(
        executor,
        message,
        history,
        ui_context,
        api_key,
        ghost_events,
        on_tool_call=lambda tc: ev_queue.put({"event": "tool_call", "data": tc}),
    )

    async def _consume() -> None:
        result = Runner.run_streamed(agent, input_items, max_turns=8)
        tokens_buf: list[str] = []
        async for event in result.stream_events():
            if getattr(event, "type", None) == "raw_response_event":
                data = getattr(event, "data", None)
                if getattr(data, "type", None) == "response.output_text.delta":
                    delta = getattr(data, "delta", "")
                    if delta:
                        tokens_buf.append(delta)
                        ev_queue.put({"event": "token", "data": {"text": delta}})
        final_text = str(result.final_output or "".join(tokens_buf))
        emitted = executor.pop_artifacts()
        artifacts = emitted if emitted else auto_artifacts
        for art in artifacts:
            ev_queue.put({"event": "artifact", "data": art.model_dump(mode="json")})
        turn = AgentTurnResult(
            reply_text=final_text,
            artifacts=artifacts,
            tool_calls=recorded_calls,
        )
        ev_queue.put({"event": "done", "data": {"ok": True, **turn.to_dict()}})

    def _worker() -> None:
        try:
            asyncio.run(_consume())
        except BaseException as exc:  # pragma: no cover - propagated below
            err_box.append(exc)
        finally:
            ev_queue.put(None)

    t = threading.Thread(target=_worker, name="open-ontology-agent-stream", daemon=True)
    t.start()
    while True:
        item = ev_queue.get()
        if item is None:
            break
        yield item
    t.join(timeout=2.0)
    if err_box:
        raise err_box[0]


def run_agent_turn(
    executor: AIPAgentExecutor,
    message: str,
    *,
    history: Sequence[dict[str, Any]] | None = None,
    ui_context: dict[str, Any] | None = None,
    ghost_events: Sequence[dict[str, Any]] | None = None,
    llm_fn: Callable[..., dict[str, Any] | AgentTurnResult | str] | None = None,
) -> AgentTurnResult:
    """Run one conversational turn with tool execution and UIArtifact collection."""
    executor.clear_artifacts()

    if llm_fn is not None:
        raw = llm_fn(
            executor=executor,
            message=message,
            history=list(history or []),
            ui_context=dict(ui_context or {}),
            ghost_events=list(ghost_events or []),
            tools=_BUILTIN_AGENT_TOOLS + export_llm_tools(executor.runtime.spec),
        )
        if isinstance(raw, AgentTurnResult):
            return raw
        if isinstance(raw, str):
            return AgentTurnResult(
                reply_text=raw,
                artifacts=executor.pop_artifacts(),
            )
        if isinstance(raw, dict):
            arts = [
                a if isinstance(a, UIArtifact) else UIArtifact(**a)
                for a in (raw.get("artifacts") or [])
            ]
            arts.extend(executor.pop_artifacts())
            return AgentTurnResult(
                reply_text=str(raw.get("reply_text") or ""),
                artifacts=arts,
                tool_calls=list(raw.get("tool_calls") or []),
            )
        raise TypeError("llm_fn must return AgentTurnResult, dict, or str")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        return _openai_chat_turn(
            executor, message, history, ui_context, api_key, ghost_events
        )

    return _deterministic_local_turn(executor, message, ui_context, ghost_events)


def iter_agent_turn(
    executor: AIPAgentExecutor,
    message: str,
    *,
    history: Sequence[dict[str, Any]] | None = None,
    ui_context: dict[str, Any] | None = None,
    ghost_events: Sequence[dict[str, Any]] | None = None,
    llm_fn: Callable[..., dict[str, Any] | AgentTurnResult | str] | None = None,
    chunk_size: int = 24,
) -> Iterator[dict[str, Any]]:
    """Stream an agent turn as structured SSE-ready events.

    Yields ``tool_call``, ``token``, ``artifact``, and ``done`` events.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if llm_fn is None and api_key:
        yield from _iter_openai_chat_turn(
            executor,
            message,
            history,
            ui_context,
            api_key,
            ghost_events,
        )
        return

    turn = run_agent_turn(
        executor,
        message,
        history=history,
        ui_context=ui_context,
        ghost_events=ghost_events,
        llm_fn=llm_fn,
    )
    for tc in turn.tool_calls:
        yield {"event": "tool_call", "data": tc}

    text = turn.reply_text or ""
    step = max(1, int(chunk_size))
    for i in range(0, len(text), step):
        yield {"event": "token", "data": {"text": text[i : i + step]}}

    for art in turn.artifacts:
        yield {"event": "artifact", "data": art.model_dump(mode="json")}

    yield {"event": "done", "data": {"ok": True, **turn.to_dict()}}


# ---------------------------------------------------------------------------
# Deterministic evals
# ---------------------------------------------------------------------------
@dataclass
class EvalCaseResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    simulation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "failures": list(self.failures),
            "simulation": self.simulation,
        }


@dataclass
class EvalReport:
    total: int
    passed: int
    failed: int
    results: list[EvalCaseResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "all_passed": self.all_passed,
            "results": [r.to_dict() for r in self.results],
        }


def _subset(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> list[str]:
    if not expected:
        return []
    if actual is None:
        return [f"expected properties {expected}, got None"]
    missing = []
    for key, value in expected.items():
        if key not in actual:
            missing.append(f"missing key {key!r}")
        elif actual[key] != value:
            missing.append(f"{key!r}: expected {value!r}, got {actual[key]!r}")
    return missing


def _find_object_change(
    diff: Any, expected: dict[str, Any]
) -> Any | None:
    for change in diff.object_changes:
        if change.object_type != expected.get("object_type"):
            continue
        if change.object_id != expected.get("object_id"):
            continue
        if "kind" in expected and change.kind != expected["kind"]:
            continue
        return change
    return None


def _find_link_change(diff: Any, expected: dict[str, Any]) -> Any | None:
    for change in diff.link_changes:
        if change.link_type != expected.get("link_type"):
            continue
        if change.source_id != expected.get("source_id"):
            continue
        if change.target_id != expected.get("target_id"):
            continue
        if "kind" in expected and change.kind != expected["kind"]:
            continue
        return change
    return None


def run_eval_suite(
    runtime: OntologyRuntime,
    actor: Actor,
    cases: Sequence[dict[str, Any]],
) -> EvalReport:
    """Deterministically simulate each case and grade the resulting diff.

    Case shape::

        {
          "name": "ai_agent_high_cost_needs_approval",
          "action": "reroute_shipment",
          "params": {...},
          "expect": {
              "valid": True,
              "requires_approval": True,
              "object_changes": [
                  {"object_type": "Shipment", "object_id": "SHP-001",
                   "kind": "update", "after": {"status": "rerouted"}}
              ],
              "link_changes": [
                  {"link_type": "carries", "source_id": "TRK-002",
                   "target_id": "SHP-001", "kind": "create"}
              ],
              "error_contains": "...",
              "no_changes": False,
          }
        }
    """
    results: list[EvalCaseResult] = []

    for index, case in enumerate(cases):
        name = str(case.get("name") or f"case-{index + 1}")
        failures: list[str] = []
        before_count = runtime.count_objects()
        expect = case.get("expect", {})

        try:
            case_actor = case.get("actor", actor)
            if isinstance(case_actor, dict):
                case_actor = Actor(**case_actor)
            sim: SimulationResult = runtime.simulate_action(
                case["action"], case.get("params", {}), case_actor
            )
        except Exception as exc:  # executor-level failures are eval failures
            after_count = runtime.count_objects()
            if after_count != before_count:
                failures.append("database changed despite simulation failure")
            results.append(
                EvalCaseResult(
                    name=name,
                    passed=False,
                    failures=[f"simulate raised {type(exc).__name__}: {exc}"],
                    simulation={"valid": False, "error": str(exc)},
                )
            )
            continue

        if "valid" in expect and sim.valid != bool(expect["valid"]):
            failures.append(
                f"valid: expected {expect['valid']}, got {sim.valid}"
            )
        if "requires_approval" in expect and (
            sim.requires_approval != bool(expect["requires_approval"])
        ):
            failures.append(
                "requires_approval: expected "
                f"{expect['requires_approval']}, got {sim.requires_approval}"
            )
        if "error_contains" in expect:
            needle = str(expect["error_contains"])
            if needle not in (sim.error or ""):
                failures.append(
                    f"error_contains: {needle!r} not in {sim.error!r}"
                )
        if expect.get("no_changes") and not sim.diff.is_empty():
            failures.append(f"expected empty diff, got {sim.diff.summary()}")

        if sim.valid:
            for expected_change in expect.get("object_changes", []):
                change = _find_object_change(sim.diff, expected_change)
                if change is None:
                    failures.append(
                        "missing object change "
                        f"{expected_change.get('kind', '*')}:"
                        f"{expected_change.get('object_type')}:"
                        f"{expected_change.get('object_id')}"
                    )
                    continue
                if "before" in expected_change:
                    failures.extend(
                        _subset(expected_change["before"], change.before)
                    )
                if "after" in expected_change:
                    failures.extend(
                        _subset(expected_change["after"], change.after)
                    )

            for expected_change in expect.get("link_changes", []):
                change = _find_link_change(sim.diff, expected_change)
                if change is None:
                    failures.append(
                        "missing link change "
                        f"{expected_change.get('kind', '*')}:"
                        f"{expected_change.get('link_type')}:"
                        f"{expected_change.get('source_id')}->"
                        f"{expected_change.get('target_id')}"
                    )

        after_count = runtime.count_objects()
        if after_count != before_count:
            failures.append(
                f"simulation mutated the database: {before_count} -> {after_count}"
            )

        results.append(
            EvalCaseResult(
                name=name,
                passed=not failures,
                failures=failures,
                simulation=sim.to_dict(),
            )
        )

    passed = sum(1 for r in results if r.passed)
    return EvalReport(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )
