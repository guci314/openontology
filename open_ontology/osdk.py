"""L5 OSDK: LLM tool export + deterministic AIP evals.

Two pieces:

* :func:`export_llm_tools` turns the declarative spec into OpenAI-style tool
  JSON Schemas (functions + actions).
* :class:`AIPAgentExecutor` is the single dispatcher an LLM agent calls, and
  :func:`run_eval_suite` deterministically grades the ``ObjectDiff`` produced
  by an agent decision -- no LLM judge, no randomness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import BaseModel

from .query import ObjectSet
from .runtime import OntologyRuntime, SimulationResult
from .spec import Actor, InvariantError, OntologySpec

__all__ = [
    "export_llm_tools",
    "AIPAgentExecutor",
    "EvalCaseResult",
    "EvalReport",
    "run_eval_suite",
]


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


def export_llm_tools(spec: OntologySpec) -> list[dict[str, Any]]:
    """Export every registered function/action as an LLM tool declaration."""
    tools: list[dict[str, Any]] = []

    for fn in spec.functions.values():
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
    """

    def __init__(self, runtime: OntologyRuntime, actor: Actor) -> None:
        self.runtime = runtime
        self.actor = actor

    # -- introspection -----------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        return export_llm_tools(self.runtime.spec)

    # -- dispatch ----------------------------------------------------------
    def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any] | str | None = None,
    ) -> Any:
        args = self._coerce_arguments(arguments)

        if tool_name == "query_objects":
            cls = self.runtime.spec.get_object_type(args["object_type"])
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
            obj = self.runtime.get_object(cls, args["object_id"], self.actor)
            return {"object": obj.model_dump(mode="json")}

        if tool_name == "get_linked":
            source_cls = self.runtime.spec.get_object_type(args["source_type"])
            target_cls = self.runtime.spec.get_object_type(args["target_type"])
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
            return {
                "object_type": result.object_type,
                "count": len(result),
                "ids": result.ids,
                "objects": [o.model_dump(mode="json") for o in result],
            }

        if tool_name == "aggregate":
            cls = self.runtime.spec.get_object_type(args["object_type"])
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
            result = self.runtime.call_function(
                args["name"], args.get("params"), self.actor
            )
            return {"ok": True, "result": result}

        if tool_name == "simulate_action":
            sim = self.runtime.simulate_action(
                args["action"], args.get("params", {}), self.actor
            )
            return sim.to_dict()

        if tool_name == "simulate_sequence":
            return self._simulate_sequence(args)

        if tool_name == "plan_mcts":
            return self._plan_mcts(args)

        if tool_name == "submit_action":
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
