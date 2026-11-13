"""Pure-business HTTP boundary for an :class:`OntologyRuntime`.

This module is deliberately transport-only: it exposes the runtime's business
API over JSON and never learns anything about external business systems or
vendor protocols.  The ontology kernel itself remains transport-agnostic.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .runtime import OntologyRuntime
from .spec import Actor, OpenOntologyError

__all__ = ["OntologyHTTPServer", "HTTPHandle", "start_http_server"]


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenOntology/0.4"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------
    @property
    def _runtime(self) -> OntologyRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    @property
    def _lock(self) -> threading.RLock:
        return self.server.runtime_lock  # type: ignore[attr-defined]

    @property
    def _actor(self) -> Actor | None:
        return self.server.actor  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        with self._lock:
            try:
                if path == "/api/health":
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/action-runs":
                    status = parse_qs(parsed.query).get("status", [None])[0]
                    self._send_json(
                        200, {"runs": self._runtime.list_action_runs(status)}
                    )
                    return

                prefix = "/api/objects/"
                if path.startswith(prefix):
                    object_type = path[len(prefix):]
                    if not object_type or "/" in object_type:
                        self._error(404, "object type required")
                        return
                    self._send_json(200, self._objects(object_type))
                    return

                self._error(404, "not found")
            except OpenOntologyError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    def _objects(self, object_type: str) -> list[dict[str, Any]]:
        """枚举投影：未注入 Actor 时保持裸行兼容，注入后走 RLS + 列脱敏。"""
        actor = self._actor
        if actor is None:
            return self._runtime.list_objects(object_type)
        cls = self._runtime.spec.get_object_type(object_type)
        visible = {
            obj.primary_key_value(): obj.to_properties()
            for obj in self._runtime.query_objects(cls, actor)
        }
        return [
            {**row, "properties": visible[row["object_id"]]}
            for row in self._runtime.list_objects(object_type)
            if row["object_id"] in visible
        ]

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path != "/api/snapshot":
            self._error(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._error(400, "invalid Content-Length")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid JSON body: {exc}")
            return
        if not isinstance(body, dict):
            self._error(400, "snapshot body must be a JSON object")
            return

        objects = body.get("objects")
        if not isinstance(objects, dict):
            self._error(400, "snapshot.objects must be a JSON object")
            return
        links = body.get("links")
        if links is not None and not isinstance(links, dict):
            self._error(400, "snapshot.links must be a JSON object or null")
            return

        with self._lock:
            try:
                summary = self._runtime.load_snapshot(objects=objects, links=links)
                self._send_json(200, {"ok": True, **summary})
            except OpenOntologyError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    # -- noise control -----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        # Tests and demos should not spam stderr for every health probe.
        return


class OntologyHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying one runtime plus a request lock."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        runtime: OntologyRuntime,
        actor: Actor | None = None,
    ) -> None:
        self.runtime = runtime
        self.actor = actor
        self.runtime_lock = threading.RLock()
        super().__init__(server_address, _Handler)


@dataclass
class HTTPHandle:
    server: OntologyHTTPServer
    thread: threading.Thread
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)


def start_http_server(
    runtime: OntologyRuntime,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    actor: Actor | None = None,
) -> HTTPHandle:
    """Start a background HTTP server and return its handle.

    传入 ``actor`` 时枚举接口按该调用方做 RLS + 列脱敏投影；不传则保持
    历史裸行行为。
    """
    server = OntologyHTTPServer((host, port), runtime, actor)
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"open-ontology-http-{actual_port}",
        daemon=True,
    )
    thread.start()
    return HTTPHandle(
        server=server,
        thread=thread,
        host=str(actual_host),
        port=int(actual_port),
    )
