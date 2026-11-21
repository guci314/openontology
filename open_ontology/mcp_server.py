"""Pure M2 standard MCP (Model Context Protocol) transport wrapper over ``OntologyRuntime``.

Exposes any domain-agnostic ``OntologyRuntime`` instance as a compliant JSON-RPC 2.0
MCP Server over **stdio** (NDJSON) and **Streamable HTTP** (``POST /mcp``,
``GET /health``) using strictly the Python standard library and M2 runtime primitives.

Security & governance invariants:
1. **Server-pinned Actor / Scoped View**: By default, ``(actor, view, view_tags)`` is
   pinned at server construction time; client headers (such as forged role headers)
   or tool parameters cannot escalate privileges unless the host explicitly injects a
   trusted ``actor_resolver(headers)`` callback.
2. **Scoped Tool & Schema Surface**: ``tools/list`` and ``ontology://tbox`` are
   filtered by ``actor`` / ``view`` / ``view_tags``. UI-only tools
   (``emit_ui_artifact``) are omitted by default, and ``plan_mcts`` is omitted unless
   ``planner_factory`` is provided.
3. **Structured Error Envelopes**: ``PermissionDeniedError``, ``InvariantError``,
   ``OpenOntologyError``, and validation errors inside ``tools/call`` return standard
   MCP ``isError=True`` JSON payloads without crashing the transport or leaking traces.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
import uuid
from typing import Any, Callable, Mapping, Sequence, TextIO

from .autonomy import AutonomyRampController
from .drivers import DeviceDriverRegistry
from .osdk import AIPAgentExecutor
from .runtime import OntologyRuntime
from .spec import (
    Actor,
    InvariantError,
    OpenOntologyError,
    PermissionDeniedError,
    ScopedViewSpec,
)

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION: str = "2024-11-05"
SERVER_NAME: str = "open-ontology"
SERVER_VERSION: str = "0.4.0"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_BUILTIN_MCP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "describe_tbox",
        "description": (
            "Read-only schema introspection returning object types, fields, links, "
            "actions, and constraints visible to the active scoped view and actor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "query_objects",
        "description": "Query visible objects of a given object_type with optional equality filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "filters": {"type": "object"},
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "get_object",
        "description": "Fetch a single visible object by object_type and object_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "object_id": {"type": "string"},
            },
            "required": ["object_type", "object_id"],
        },
    },
    {
        "name": "get_linked",
        "description": "Traverse a link relation from a visible source object to linked target objects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string"},
                "source_id": {"type": "string"},
                "link": {"type": "string"},
                "target_type": {"type": "string"},
            },
            "required": ["source_type", "source_id", "link", "target_type"],
        },
    },
    {
        "name": "pivot",
        "description": "Hop from a set of source objects across a link relation into a target ObjectSet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "filters": {"type": "object"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "link": {"type": "string"},
                "direction": {"type": "string"},
            },
            "required": ["object_type", "link"],
        },
    },
    {
        "name": "aggregate",
        "description": "Compute count and optional numeric sum grouped by an optional field over visible objects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "filters": {"type": "object"},
                "group_by": {"type": "string"},
                "sum_field": {"type": "string"},
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "call_function",
        "description": "Invoke a registered read-only ontology function with optional parameters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "simulate_action",
        "description": (
            "Counterfactual zero-side-effect dry-run of a governed ontology action. "
            "Validates preconditions and invariants and returns the predicted diff without writing state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "simulate_sequence",
        "description": (
            "Run a multi-step counterfactual action sequence in an isolated in-memory fork "
            "without mutating primary state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        "required": ["action"],
                    },
                },
                "value_function": {"type": "string"},
                "value_params": {"type": "object"},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "plan_mcts",
        "description": "Run MCTS counterfactual rollout planning across candidate action branches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidates": {"type": "array", "items": {"type": "array"}},
                "value_function": {"type": "string"},
                "simulations": {"type": "integer"},
                "c_puct": {"type": "number"},
                "seed": {"type": "integer"},
            },
            "required": ["candidates", "value_function"],
        },
    },
    {
        "name": "submit_action",
        "description": (
            "Execute and commit a governed ontology action with a unique run_id, "
            "enforcing RBAC, FSM transitions, invariants, and settlement gates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "action": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["run_id", "action"],
        },
    },
    {
        "name": "list_action_runs",
        "description": "List recorded ontology action runs, optionally filtered by run status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "emit_ui_artifact",
        "description": "Emit a structured UI artifact payload for front-end clients.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "title": {"type": "string"},
                "payload": {"type": "object"},
                "artifact_id": {"type": "string"},
            },
            "required": ["kind", "title", "payload"],
        },
    },
)


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


class _JSONRPCException(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _extract_device_registry(runtime: OntologyRuntime) -> DeviceDriverRegistry | None:
    """Locate a mounted ``DeviceDriverRegistry`` attached directly or via bridge closure."""
    direct = getattr(runtime, "device_registry", None)
    if isinstance(direct, DeviceDriverRegistry):
        return direct
    sb = runtime.source_bridge
    if sb is None:
        return None
    sb_reg = getattr(sb, "device_registry", None)
    if isinstance(sb_reg, DeviceDriverRegistry):
        return sb_reg
    for fn in (sb.actuate, sb.observe):
        if fn is None:
            continue
        for cell in getattr(fn, "__closure__", None) or ():
            val = cell.cell_contents
            if isinstance(val, DeviceDriverRegistry):
                return val
    return None


def _summarize_devices(runtime: OntologyRuntime) -> list[dict[str, Any]]:
    registry = _extract_device_registry(runtime)
    if registry is not None:
        items: list[dict[str, Any]] = []
        for dev in registry.list_devices():
            if isinstance(dev, dict):
                items.append({k: v for k, v in dev.items() if k != "bridge"})
            else:
                items.append(
                    {
                        "device_id": dev.device_id,
                        "name": dev.name,
                        "kind": dev.kind,
                        "endpoint": dev.endpoint,
                        "status": dev.status,
                        "owned_object_types": list(dev.owned_object_types),
                        "shared_object_types": list(dev.shared_object_types),
                        "handled_actions": list(dev.handled_actions),
                        "allow_source_revert": bool(dev.allow_source_revert),
                        "metadata": dict(dev.metadata or {}),
                    }
                )
        return items
    sb = runtime.source_bridge
    if sb is not None:
        return [
            {
                "device_id": "primary_source_bridge",
                "protocol": "source_bridge",
                "status": "MOUNTED",
                "allow_source_revert": bool(sb.allow_source_revert),
            }
        ]
    return []


def _format_mcp_text_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }


def _structured_tool_error(exc: Exception) -> dict[str, Any]:
    err_type = type(exc).__name__
    raw_code = getattr(exc, "code", None)
    code = str(raw_code) if raw_code else err_type.upper()
    return {
        "ok": False,
        "error_type": err_type,
        "code": code,
        "message": str(exc),
    }


class OntologyMCPServer:
    """Thread-safe pure M2 MCP protocol adapter over ``OntologyRuntime`` & ``AIPAgentExecutor``."""

    def __init__(
        self,
        runtime: OntologyRuntime,
        actor: Actor,
        *,
        view: str | ScopedViewSpec | None = None,
        view_tags: Sequence[str] | None = None,
        enforce_view_scope: bool = True,
        server_name: str = SERVER_NAME,
        server_version: str = SERVER_VERSION,
        include_ui_tools: bool = False,
        planner_factory: Callable[..., Any] | None = None,
        actor_resolver: Callable[[Mapping[str, str]], Actor] | None = None,
        autonomy_controller: AutonomyRampController | None = None,
    ) -> None:
        self.runtime = runtime
        self.actor = actor
        self.view = view
        self.view_tags = tuple(view_tags) if view_tags is not None else None
        self.enforce_view_scope = bool(enforce_view_scope)
        self.server_name = str(server_name)
        self.server_version = str(server_version)
        self.include_ui_tools = bool(include_ui_tools)
        self.planner_factory = planner_factory
        self.actor_resolver = actor_resolver
        self.autonomy_controller = autonomy_controller
        self._lock = threading.RLock()
        self.call_history: list[dict[str, Any]] = []

    def _resolve_actor(self, headers: Mapping[str, str] | None = None) -> Actor:
        if self.actor_resolver is None:
            return self.actor
        resolved = self.actor_resolver(dict(headers or {}))
        if not isinstance(resolved, Actor):
            raise PermissionDeniedError("actor_resolver must return an Actor instance")
        return resolved

    def _executor_for(self, actor: Actor) -> AIPAgentExecutor:
        return AIPAgentExecutor(
            self.runtime,
            actor,
            view=self.view,
            view_tags=self.view_tags,
            enforce_view_scope=self.enforce_view_scope,
            planner_factory=self.planner_factory,
        )

    def _is_tool_enabled(self, tool_name: str) -> bool:
        if tool_name == "emit_ui_artifact" and not self.include_ui_tools:
            return False
        if tool_name == "plan_mcts" and self.planner_factory is None:
            return False
        return True

    def list_tools(self, headers: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """Build the scoped standard MCP tool list for the resolved actor."""
        with self._lock:
            actor = self._resolve_actor(headers)
            executor = self._executor_for(actor)
            mcp_tools: list[dict[str, Any]] = []
            seen: set[str] = set()

            for builtin in _BUILTIN_MCP_TOOLS:
                name = str(builtin["name"])
                if not self._is_tool_enabled(name):
                    continue
                seen.add(name)
                mcp_tools.append(
                    {
                        "name": name,
                        "description": str(builtin["description"]),
                        "inputSchema": dict(builtin["inputSchema"]),
                    }
                )

            for td in executor.tools():
                fn_meta = td.get("function") or {}
                name = str(fn_meta.get("name") or "")
                if not name or name in seen or not self._is_tool_enabled(name):
                    continue
                seen.add(name)
                desc = str(fn_meta.get("description") or name)
                if td.get("x-ontology-kind") == "action":
                    desc = (
                        f"{desc} [Calling directly runs zero-side-effect simulate_action; "
                        "use submit_action with run_id to commit.]"
                    )
                schema = dict(
                    fn_meta.get("parameters") or {"type": "object", "properties": {}}
                )
                mcp_tools.append(
                    {
                        "name": name,
                        "description": desc,
                        "inputSchema": schema,
                    }
                )
            return mcp_tools

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute an MCP tool call inside ``RLock()`` with structured error handling."""
        with self._lock:
            actor = self._resolve_actor(headers)
            executor = self._executor_for(actor)
            args = dict(arguments or {})
            try:
                if not self._is_tool_enabled(tool_name):
                    raise ValueError(f"tool {tool_name!r} is disabled on this server")
                if tool_name == "describe_tbox":
                    result = executor.describe_tbox()
                elif tool_name == "submit_action" and self.autonomy_controller is not None:
                    executor._require_public_action(args["action"])
                    decision = self.autonomy_controller.execute(
                        self.runtime,
                        actor,
                        args["run_id"],
                        args["action"],
                        args.get("params", {}),
                    )
                    result = {"autonomy": decision.to_dict()}
                    if decision.run is not None:
                        result["run"] = decision.run
                else:
                    result = executor.dispatch(tool_name, args)
                self.call_history.append(
                    {
                        "tool": tool_name,
                        "arguments": args,
                        "actor_id": actor.actor_id,
                        "actor_role": actor.role,
                        "is_error": False,
                    }
                )
                return _format_mcp_text_result(result, is_error=False)
            except (
                PermissionDeniedError,
                InvariantError,
                OpenOntologyError,
                KeyError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                err_payload = _structured_tool_error(exc)
                self.call_history.append(
                    {
                        "tool": tool_name,
                        "arguments": args,
                        "actor_id": actor.actor_id,
                        "actor_role": actor.role,
                        "is_error": True,
                        "error_code": err_payload["code"],
                    }
                )
                return _format_mcp_text_result(err_payload, is_error=True)

    def list_resources(self, headers: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """Expose read-only TBox and mounted device resources."""
        with self._lock:
            self._resolve_actor(headers)
            return [
                {
                    "uri": "ontology://tbox",
                    "name": "Scoped Ontology TBox",
                    "description": (
                        "Read-only ontology declarations (object types, fields, links, "
                        "actions, and constraints) scoped to the active actor and view."
                    ),
                    "mimeType": "application/json",
                },
                {
                    "uri": "ontology://devices",
                    "name": "Mounted Device Drivers",
                    "description": "Read-only manifest of source bridges and mounted device drivers.",
                    "mimeType": "application/json",
                },
            ]

    def read_resource(
        self,
        uri: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actor = self._resolve_actor(headers)
            executor = self._executor_for(actor)
            if uri == "ontology://tbox":
                payload = executor.describe_tbox()
            elif uri == "ontology://devices":
                payload = _summarize_devices(self.runtime)
            else:
                raise _JSONRPCException(_INVALID_PARAMS, f"unknown resource URI: {uri!r}")
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(payload, ensure_ascii=False, default=str),
                    }
                ]
            }

    def _negotiate_protocol(self, requested: Any) -> str:
        if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        return DEFAULT_PROTOCOL_VERSION

    def _handle_single_rpc(
        self,
        msg: Any,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _jsonrpc_error(None, _INVALID_REQUEST, "invalid JSON-RPC 2.0 envelope")

        method = msg.get("method")
        if not isinstance(method, str) or not method:
            return _jsonrpc_error(msg.get("id"), _INVALID_REQUEST, "missing method")

        has_id = "id" in msg and msg["id"] is not None
        req_id = msg.get("id")
        params = msg.get("params")
        if params is None:
            params_dict: dict[str, Any] = {}
        elif isinstance(params, dict):
            params_dict = params
        else:
            return (
                _jsonrpc_error(req_id, _INVALID_PARAMS, "params must be an object")
                if has_id
                else None
            )

        # Notification (no id, or notifications/*) must emit zero response frame
        if not has_id:
            if method == "notifications/initialized":
                return None
            return None

        try:
            if method == "initialize":
                version = self._negotiate_protocol(params_dict.get("protocolVersion"))
                return _jsonrpc_result(
                    req_id,
                    {
                        "protocolVersion": version,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"listChanged": False},
                        },
                        "serverInfo": {
                            "name": self.server_name,
                            "version": self.server_version,
                        },
                    },
                )

            if method == "ping":
                return _jsonrpc_result(req_id, {})

            if method == "tools/list":
                return _jsonrpc_result(req_id, {"tools": self.list_tools(headers=headers)})

            if method == "tools/call":
                tname = params_dict.get("name")
                if not isinstance(tname, str) or not tname:
                    return _jsonrpc_error(req_id, _INVALID_PARAMS, "tools/call requires string 'name'")
                targs = params_dict.get("arguments")
                if targs is not None and not isinstance(targs, dict):
                    return _jsonrpc_error(
                        req_id, _INVALID_PARAMS, "tools/call 'arguments' must be an object"
                    )
                res = self.call_tool(tname, targs or {}, headers=headers)
                return _jsonrpc_result(req_id, res)

            if method == "resources/list":
                return _jsonrpc_result(
                    req_id, {"resources": self.list_resources(headers=headers)}
                )

            if method == "resources/read":
                uri = params_dict.get("uri")
                if not isinstance(uri, str) or not uri:
                    return _jsonrpc_error(
                        req_id, _INVALID_PARAMS, "resources/read requires string 'uri'"
                    )
                return _jsonrpc_result(
                    req_id, self.read_resource(uri, headers=headers)
                )

            if method == "notifications/initialized":
                return _jsonrpc_result(req_id, {})

            return _jsonrpc_error(req_id, _METHOD_NOT_FOUND, f"method not found: {method!r}")
        except PermissionDeniedError as exc:
            return _jsonrpc_error(req_id, _INVALID_REQUEST, str(exc), {"code": exc.code})
        except _JSONRPCException as exc:
            return _jsonrpc_error(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive transport boundary
            return _jsonrpc_error(req_id, _INTERNAL_ERROR, str(exc))

    def handle_jsonrpc_message(
        self,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Process a decoded JSON-RPC 2.0 object or batch and return response or ``None``."""
        if isinstance(payload, list):
            if not payload:
                return _jsonrpc_error(None, _INVALID_REQUEST, "empty JSON-RPC batch")
            replies: list[dict[str, Any]] = []
            for item in payload:
                resp = self._handle_single_rpc(item, headers=headers)
                if resp is not None:
                    replies.append(resp)
            return replies if replies else None
        return self._handle_single_rpc(payload, headers=headers)

    def health_payload(self, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        with self._lock:
            actor = self._resolve_actor(headers)
            tools = self.list_tools(headers=headers)
            return {
                "ok": True,
                "status": "healthy",
                "server_name": self.server_name,
                "server_version": self.server_version,
                "actor_id": actor.actor_id,
                "actor_role": actor.role,
                "view": self.view if isinstance(self.view, str) else getattr(self.view, "name", None),
                "world_mode": self.runtime.world_mode,
                "tool_count": len(tools),
                "autonomy_default_mode": (
                    self.autonomy_controller.policy.default_mode
                    if self.autonomy_controller is not None
                    else None
                ),
            }

    def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        """Serve newline-delimited JSON-RPC 2.0 over stdio until EOF."""
        serve_stdio(self, stdin=stdin, stdout=stdout)


def serve_stdio(
    server: OntologyMCPServer,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Run strict NDJSON MCP JSON-RPC 2.0 loop over ``stdin`` / ``stdout``."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout

    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            err = _jsonrpc_error(None, _PARSE_ERROR, f"parse error: {exc}")
            out_stream.write(json.dumps(err, ensure_ascii=False) + "\n")
            out_stream.flush()
            continue

        reply = server.handle_jsonrpc_message(payload)
        if reply is not None:
            out_stream.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out_stream.flush()


class OntologyMCPHTTPServer(ThreadingHTTPServer):
    """Pure stdlib threaded Streamable HTTP server for ``OntologyMCPServer``."""

    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        mcp_server: OntologyMCPServer,
    ) -> None:
        self.mcp_server = mcp_server
        self.session_ids: set[str] = set()
        self._session_lock = threading.Lock()
        handler_cls = _make_mcp_http_handler(self)
        super().__init__(server_address, handler_cls)

    def ensure_session_id(self, incoming: str | None) -> str:
        with self._session_lock:
            sid = (incoming or "").strip()
            if not sid:
                sid = f"mcp-{uuid.uuid4().hex[:16]}"
            self.session_ids.add(sid)
            return sid


def _make_mcp_http_handler(owner: OntologyMCPHTTPServer) -> type[BaseHTTPRequestHandler]:
    class _MCPHTTPHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
            return

        def _send_json(
            self,
            status: int,
            payload: Any,
            *,
            extra_headers: Mapping[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(body)

        def _send_empty(
            self,
            status: int,
            *,
            extra_headers: Mapping[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.end_headers()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            headers = {k: v for k, v in self.headers.items()}
            if path == "/health":
                self._send_json(HTTPStatus.OK, owner.mcp_server.health_payload(headers=headers))
                return
            if path == "/mcp":
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "GET /mcp SSE stream is not offered; use POST /mcp"},
                    extra_headers={"Allow": "POST, DELETE"},
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

        def do_DELETE(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/mcp":
                self._send_empty(HTTPStatus.NO_CONTENT)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path != "/mcp":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    _jsonrpc_error(None, _INVALID_REQUEST, "invalid Content-Length"),
                )
                return
            raw_body = self.rfile.read(max(0, length))
            try:
                decoded = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    _jsonrpc_error(None, _PARSE_ERROR, f"parse error: {exc}"),
                )
                return

            headers = {k: v for k, v in self.headers.items()}
            session_id = owner.ensure_session_id(self.headers.get("Mcp-Session-Id"))
            resp = owner.mcp_server.handle_jsonrpc_message(decoded, headers=headers)
            if resp is None:
                self._send_empty(
                    HTTPStatus.ACCEPTED,
                    extra_headers={"Mcp-Session-Id": session_id},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                resp,
                extra_headers={"Mcp-Session-Id": session_id},
            )

    return _MCPHTTPHandler


@dataclass
class MCPHTTPHandle:
    server: OntologyMCPHTTPServer
    thread: threading.Thread
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def __enter__(self) -> "MCPHTTPHandle":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def start_mcp_http_server(
    mcp_server: OntologyMCPServer,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> MCPHTTPHandle:
    """Start a background threaded Streamable HTTP MCP server."""
    httpd = OntologyMCPHTTPServer((host, port), mcp_server)
    bound_host, bound_port = httpd.server_address[:2]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return MCPHTTPHandle(
        server=httpd,
        thread=thread,
        host=str(bound_host),
        port=int(bound_port),
    )


def _load_cli_factory(
    factory_arg: str,
    *,
    actor_id: str,
    actor_role: str,
    clearance_tags: Sequence[str],
    view: str | None,
    include_ui_tools: bool,
) -> OntologyMCPServer:
    # Lazy-import from design so dynamic factory resolution stays strictly in design.py.
    from .design import load_symbol_arg

    symbol = load_symbol_arg(factory_arg)
    produced = symbol() if callable(symbol) else symbol
    if isinstance(produced, OntologyMCPServer):
        return produced

    tags = set(clearance_tags) if clearance_tags else {"*"}
    if isinstance(produced, tuple) and len(produced) == 2:
        rt, actor = produced
        if not isinstance(rt, OntologyRuntime) or not isinstance(actor, Actor):
            raise TypeError(
                "--factory tuple must be (OntologyRuntime, Actor)"
            )
    elif isinstance(produced, OntologyRuntime):
        rt = produced
        actor = Actor(actor_id=actor_id, role=actor_role, clearance_tags=tags)
    else:
        raise TypeError(
            "--factory must return OntologyMCPServer, (OntologyRuntime, Actor), or OntologyRuntime"
        )
    return OntologyMCPServer(
        rt,
        actor,
        view=view,
        include_ui_tools=include_ui_tools,
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m open_ontology.mcp_server",
        description="Expose an OpenOntology Runtime as a standard MCP Server (stdio or Streamable HTTP).",
    )
    parser.add_argument(
        "--factory",
        required=True,
        help="Python factory 'module.path:symbol' returning OntologyMCPServer, (OntologyRuntime, Actor), or OntologyRuntime.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport mode (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port (default: 8765).")
    parser.add_argument("--actor-id", default="mcp-agent", help="Pinned Actor ID.")
    parser.add_argument("--actor-role", default="ai_agent", help="Pinned Actor role.")
    parser.add_argument("--view", default=None, help="Optional ScopedViewSpec name.")
    parser.add_argument(
        "--clearance-tag",
        action="append",
        default=[],
        help="Clearance tag for constructed Actor (repeatable; defaults to '*').",
    )
    parser.add_argument(
        "--include-ui-tools",
        action="store_true",
        help="Include UI artifact tools such as emit_ui_artifact.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    server = _load_cli_factory(
        args.factory,
        actor_id=args.actor_id,
        actor_role=args.actor_role,
        clearance_tags=args.clearance_tag,
        view=args.view,
        include_ui_tools=args.include_ui_tools,
    )
    if args.transport == "stdio":
        server.serve_stdio()
        return 0
    handle = start_mcp_http_server(server, host=args.host, port=args.port)
    sys.stderr.write(f"OntologyMCPHTTPServer listening on {handle.mcp_url}\n")
    sys.stderr.flush()
    try:
        handle.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        handle.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "MCPHTTPHandle",
    "OntologyMCPHTTPServer",
    "OntologyMCPServer",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "main",
    "serve_stdio",
    "start_mcp_http_server",
]
