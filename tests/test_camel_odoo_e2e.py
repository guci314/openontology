"""End-to-end tests: real Quarkus/Camel JVM + OpenOntology HTTP + dual Odoo 19.

Every test starts a real ``quarkus-run.jar`` process and a real Python HTTP
server, then exercises Camel's REST routes, split/choice content-based router
and Odoo JSON-RPC writeback against ``erp_north`` / ``erp_south``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from examples import odoo_dual_erp_demo as demo
from examples import odoo_orders_ontology as dom
from open_ontology import InvariantError, OntologyRuntime
from open_ontology.http_server import HTTPHandle, start_http_server

ROOT = Path(__file__).resolve().parents[1]
CAMEL_DIR = ROOT / "camel-sync"
CAMEL_JAR = CAMEL_DIR / "target" / "quarkus-app" / "quarkus-run.jar"

JAVA_HOME = Path(
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
)
JAVA = JAVA_HOME / "bin" / "java"
MVN = Path("/opt/homebrew/opt/maven/bin/mvn")


# ---------------------------------------------------------------------------
# Infrastructure fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def camel_jar() -> Path:
    """Build the real Quarkus/Camel project offline once per test session."""
    java = str(JAVA) if JAVA.exists() else (shutil.which("java") or "java")
    mvn = str(MVN) if MVN.exists() else (shutil.which("mvn") or "mvn")

    env = os.environ.copy()
    env["JAVA_HOME"] = str(JAVA_HOME)
    env["PATH"] = (
        f"{JAVA_HOME / 'bin'}:{MVN.parent}:{env.get('PATH', '')}"
    )
    result = subprocess.run(
        [mvn, "-o", "package", "-DskipTests"],
        cwd=CAMEL_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise AssertionError(
            "Camel offline build failed.\n"
            f"STDOUT:\n{result.stdout[-4000:]}\n"
            f"STDERR:\n{result.stderr[-4000:]}"
        )
    if not CAMEL_JAR.exists():
        raise AssertionError(f"expected Camel jar not found: {CAMEL_JAR}")
    return CAMEL_JAR


@pytest.fixture(scope="session")
def odoo_data():
    return demo.setup_dual_odoo_erps(reset=True)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@dataclass
class CamelStack:
    runtime: OntologyRuntime
    data: demo.DualERPData
    http: HTTPHandle
    process: subprocess.Popen
    base_url: str
    log_path: str

    def camel(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def ontology(self, path: str) -> str:
        return f"{self.http.base_url}{path}"

    def stats(self) -> dict:
        return http_json(self.camel("/camel/stats"))

    def ingest(self) -> dict:
        return http_json(self.camel("/camel/ingest"), method="POST", payload={})

    def writeback(self) -> dict:
        return http_json(
            self.camel("/camel/writeback"), method="POST", payload={}
        )


def http_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 60,
):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else None


def _wait_for_camel(
    process: subprocess.Popen,
    base_url: str,
    log_path: str,
    timeout: float = 90.0,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "Camel JVM exited during startup.\n"
                + Path(log_path).read_text(encoding="utf-8", errors="replace")[-5000:]
            )
        try:
            http_json(f"{base_url}/camel/stats", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    process.terminate()
    raise AssertionError(
        "Camel JVM did not become ready.\n"
        + Path(log_path).read_text(encoding="utf-8", errors="replace")[-5000:]
    )


@pytest.fixture()
def stack(camel_jar: Path, odoo_data: demo.DualERPData):
    """Fresh Odoo state + fresh ontology + fresh real Camel JVM."""
    demo.reset_dual_odoo_orders(odoo_data)

    runtime = OntologyRuntime(dom.spec)
    http_handle = start_http_server(runtime)

    camel_port = _free_port()
    base_url = f"http://127.0.0.1:{camel_port}"

    fd, log_path = tempfile.mkstemp(prefix="camel-sync-", suffix=".log")
    log_file = os.fdopen(fd, "w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "QUARKUS_HTTP_HOST": "127.0.0.1",
            "QUARKUS_HTTP_PORT": str(camel_port),
            "ONTOLOGY_URL": http_handle.base_url,
            "ODOO_URL": os.environ.get(
                "ODOO_URL", "http://localhost:8069/jsonrpc"
            ),
            "ODOO_USER": "admin",
            "ODOO_PASSWORD": "admin",
            "ODOO_DB_NORTH": "erp_north",
            "ODOO_DB_SOUTH": "erp_south",
        }
    )

    java = str(JAVA) if JAVA.exists() else (shutil.which("java") or "java")
    process = subprocess.Popen(
        [java, "-jar", str(camel_jar)],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_camel(process, base_url, log_path)
        yield CamelStack(
            runtime=runtime,
            data=odoo_data,
            http=http_handle,
            process=process,
            base_url=base_url,
            log_path=log_path,
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log_file.close()
        http_handle.stop()
        runtime.close()


# ---------------------------------------------------------------------------
# 1. Ingest route: both real Odoos -> Camel -> ontology snapshot
# ---------------------------------------------------------------------------
def test_camel_ingest_route_loads_both_erps(stack: CamelStack):
    assert stack.process.poll() is None

    result = stack.ingest()
    assert result["ok"] is True
    assert result["customers"] == 2
    assert result["orders"] == 5
    assert result["links"] == 5
    assert result["snapshot"]["ok"] is True

    orders = http_json(stack.ontology("/api/objects/Order"))
    assert {row["object_id"] for row in orders} == {
        "N-SO001",
        "N-SO002",
        "S-SO001",
        "S-SO002",
        "S-SO003",
    }

    stats = stack.stats()
    assert stats["ingest_count"] == 1
    assert stats["north_writebacks"] == 0
    assert stats["south_writebacks"] == 0


# ---------------------------------------------------------------------------
# 2. Camel CBR: north cancel and time-machine revert
# ---------------------------------------------------------------------------
def test_camel_cbr_north_cancel_and_revert(stack: CamelStack):
    stack.ingest()

    stack.runtime.submit_action(
        "CAMEL-N-CANCEL",
        "cancel_order",
        {"order_id": "N-SO001", "reason": "customer requested"},
        demo.NORTH_DISPATCHER,
    )
    first = stack.writeback()
    assert first["ok"] is True
    assert first["count"] == 1
    assert first["applied"][0]["region"] == "north"
    assert first["applied"][0]["odoo_state_before"] == "sale"
    assert first["applied"][0]["odoo_state_after"] == "cancel"
    assert stack.data.client("erp_north").order_state("N-SO001") == "cancel"
    assert stack.stats()["north_writebacks"] == 1

    stack.runtime.revert_action(
        "CAMEL-N-CANCEL", "CAMEL-N-CANCEL-REV", demo.OPS_MANAGER
    )
    second = stack.writeback()
    assert second["ok"] is True
    assert second["count"] == 1
    assert second["applied"][0]["run_id"] == "CAMEL-N-CANCEL-REV"
    assert second["applied"][0]["odoo_state_before"] == "cancel"
    assert second["applied"][0]["odoo_state_after"] == "sale"
    assert stack.data.client("erp_north").order_state("N-SO001") == "sale"
    assert stack.stats()["north_writebacks"] == 2


# ---------------------------------------------------------------------------
# 3. Camel CBR: south large AI approval, no write before approval
# ---------------------------------------------------------------------------
def test_camel_cbr_south_large_ai_approval(stack: CamelStack):
    stack.ingest()
    south = stack.data.client("erp_south")

    pending = stack.runtime.submit_action(
        "CAMEL-AI-LARGE",
        "cancel_order",
        {
            "order_id": "S-SO002",
            "reason": "budget freeze",
            "large_order": True,
            "order_total_cents": 6_000_000,
        },
        demo.AI_AGENT,
    )
    assert pending["status"] == "PENDING_APPROVAL"

    before = stack.writeback()
    assert before["count"] == 0
    assert south.order_state("S-SO002") == "sale"
    stats = stack.stats()
    assert stats["south_writebacks"] == 0
    assert stats["north_writebacks"] == 0

    approved = stack.runtime.approve_action(
        "CAMEL-AI-LARGE", demo.OPS_MANAGER
    )
    assert approved["status"] == "COMMITTED"

    after = stack.writeback()
    assert after["ok"] is True
    assert after["count"] == 1
    assert after["applied"][0]["region"] == "south"
    assert after["applied"][0]["odoo_state_before"] == "sale"
    assert after["applied"][0]["odoo_state_after"] == "cancel"
    assert south.order_state("S-SO002") == "cancel"

    stats = stack.stats()
    assert stats["south_writebacks"] == 1
    assert stats["north_writebacks"] == 0


# ---------------------------------------------------------------------------
# 4. Camel ingest/reindex keeps overlay and owned OrderNote alive
# ---------------------------------------------------------------------------
def test_camel_ingest_preserves_overlay_and_owned_notes(stack: CamelStack):
    stack.ingest()

    stack.runtime.submit_action(
        "CAMEL-ASSIGN",
        "assign_order",
        {
            "order_id": "N-SO001",
            "assignee": "north-ops",
            "ops_note": "priority customer",
        },
        demo.NORTH_DISPATCHER,
    )
    stack.runtime.submit_action(
        "CAMEL-NOTE",
        "add_order_note",
        {
            "note_id": "CAMEL-NOTE-001",
            "order_id": "N-SO001",
            "body": "keep the original packaging",
        },
        demo.NORTH_DISPATCHER,
    )

    north = stack.data.client("erp_north")
    old_price = north.get_order_line_price("N-SO002")
    try:
        north.set_order_line_price("N-SO002", old_price + 50.0)
        second = stack.ingest()
        assert second["ok"] is True

        orders = {
            row["object_id"]: row
            for row in http_json(stack.ontology("/api/objects/Order"))
        }
        assert orders["N-SO001"]["properties"]["assignee"] == "north-ops"
        assert (
            orders["N-SO001"]["properties"]["ops_note"]
            == "priority customer"
        )
        assert orders["N-SO002"]["properties"]["total_cents"] == 75000

        notes = http_json(stack.ontology("/api/objects/OrderNote"))
        assert len(notes) == 1
        assert notes[0]["object_id"] == "CAMEL-NOTE-001"
        assert notes[0]["properties"]["body"] == "keep the original packaging"

        order = stack.runtime.get_object(
            dom.Order, "N-SO001", demo.OPS_MANAGER
        )
        linked = stack.runtime.get_linked(
            order, dom.ORDER_NOTES, dom.OrderNote, demo.OPS_MANAGER
        )
        assert [note.note_id for note in linked] == ["CAMEL-NOTE-001"]
    finally:
        north.set_order_line_price("N-SO002", old_price)


# ---------------------------------------------------------------------------
# 5. Camel writeback idempotency
# ---------------------------------------------------------------------------
def test_camel_writeback_is_idempotent(stack: CamelStack):
    stack.ingest()
    south = stack.data.client("erp_south")

    stack.runtime.submit_action(
        "CAMEL-IDEMPOTENT",
        "cancel_order",
        {"order_id": "S-SO001", "reason": "once"},
        demo.SOUTH_DISPATCHER,
    )

    first = stack.writeback()
    assert first["count"] == 1
    assert first["applied"][0]["order_id"] == "S-SO001"
    assert south.order_state("S-SO001") == "cancel"

    second = stack.writeback()
    assert second["count"] == 0
    assert second["applied"] == []
    assert south.order_state("S-SO001") == "cancel"

    stats = stack.stats()
    assert stats["south_writebacks"] == 1
    assert stats["processed_run_ids"] == ["CAMEL-IDEMPOTENT"]


# ---------------------------------------------------------------------------
# 6. Preconditions still reject before any Camel writeback
# ---------------------------------------------------------------------------
def test_camel_shipped_order_refusal_has_no_writeback(stack: CamelStack):
    stack.ingest()

    with pytest.raises(InvariantError) as excinfo:
        stack.runtime.submit_action(
            "CAMEL-REJECTED-SHIPPED",
            "cancel_order",
            {"order_id": "N-SO002", "reason": "too late"},
            demo.NORTH_DISPATCHER,
        )
    assert excinfo.value.code == "SHIPPED_ORDER_CANNOT_BE_CANCELLED"

    result = stack.writeback()
    assert result["count"] == 0
    assert stack.data.client("erp_north").order_state("N-SO002") == "sale"
    stats = stack.stats()
    assert stats["north_writebacks"] == 0
    assert stats["south_writebacks"] == 0
