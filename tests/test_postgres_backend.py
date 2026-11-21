"""Offline contract & live PostgreSQL 16 verification suite for WO-20."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Iterator
import uuid

import psycopg
import pytest
from pydantic import BaseModel

from open_ontology import (
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    ObjectType,
    OntologySpec,
    SourceBridge,
)
from open_ontology.autonomy import (
    ActionAutonomyRule,
    AutonomyPolicy,
    AutonomyRampController,
)
from open_ontology.mcp_server import OntologyMCPServer
from open_ontology import postgres_backend
from open_ontology.postgres_backend import (
    PostgresCompatConnection,
    PostgresOntologyRuntime,
    SCHEMA_PG_PATH,
    open_postgres_runtime,
    require_psycopg,
    split_sql_script,
    translate_sqlite_sql,
)

from examples.std_core_ontology import AccountingEntity, BusinessParty
from examples.std_p2p_ontology import (
    P2pPurchaseOrder,
    P2pSupplierProfile,
    spec as std_p2p_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_DSN = os.environ.get(
    "OPEN_ONTOLOGY_PG_DSN",
    "postgresql://open_ontology:open_ontology_dev@127.0.0.1:15432/open_ontology",
)
ADMIN = Actor(actor_id="admin-pg", role="admin", clearance_tags={"*"})
AI_AGENT = Actor(actor_id="agent-pg", role="ai_agent", clearance_tags={"*"})


# ---------------------------------------------------------------------------
# Isolated schema management (guaranteed DROP SCHEMA ... CASCADE cleanup)
# ---------------------------------------------------------------------------
@contextmanager
def _isolated_pg_schema() -> Iterator[str]:
    schema = f"wo20_t_{uuid.uuid4().hex[:12]}"
    try:
        yield schema
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as admin_conn:
            admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _build_ledger_item_spec(
    *,
    barrier: threading.Barrier | None = None,
    sync_dir: Path | None = None,
) -> tuple[OntologySpec, type[ObjectType]]:
    spec = OntologySpec()

    @spec.object_type
    class LedgerItem(ObjectType):
        __primary_key__ = "item_id"
        __read_only_source__ = True
        __owned__ = {"ops_tag": "", "note": ""}

        item_id: str
        title: str
        balance: int = 0
        credit_limit: int = 500
        ops_tag: str = ""
        note: str = ""

    @spec.object_type
    class WorkDoc(ObjectType):
        __primary_key__ = "doc_id"

        doc_id: str
        title: str
        counter: int = 0
        status: str = "draft"

    class AnnotateItemParams(BaseModel):
        item_id: str
        ops_tag: str
        note: str = ""

    class BumpDocParams(BaseModel):
        doc_id: str
        delta: int = 1

    class UpdateTitleParams(BaseModel):
        doc_id: str
        title: str

    @spec.action(
        "annotate_item",
        AnnotateItemParams,
        allowed_roles=("admin", "ai_agent"),
        revert_roles=("admin",),
        affects_source=False,
    )
    def annotate_item(ctx: Any, params: AnnotateItemParams) -> None:
        item = ctx.require(LedgerItem, params.item_id)
        ctx.modify(item, ops_tag=params.ops_tag, note=params.note)

    @spec.action(
        "bump_doc",
        BumpDocParams,
        allowed_roles=("admin", "ai_agent"),
        revert_roles=("admin",),
    )
    def bump_doc(ctx: Any, params: BumpDocParams) -> None:
        doc = ctx.require(WorkDoc, params.doc_id)
        if barrier is not None:
            barrier.wait(timeout=5.0)
        if sync_dir is not None:
            (sync_dir / "dry_run_ready").write_text("ready", encoding="utf-8")
            deadline = time.monotonic() + 5.0
            go_flag = sync_dir / "proceed"
            while not go_flag.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("timed out waiting for parent commit sync signal")
                time.sleep(0.02)
        ctx.modify(doc, counter=doc.counter + params.delta)

    @spec.action(
        "rename_doc",
        UpdateTitleParams,
        allowed_roles=("admin", "ai_agent"),
        revert_roles=("admin",),
    )
    def rename_doc(ctx: Any, params: UpdateTitleParams) -> None:
        doc = ctx.require(WorkDoc, params.doc_id)
        ctx.modify(doc, title=params.title)

    spec._test_work_doc = WorkDoc  # type: ignore[attr-defined]
    return spec, LedgerItem


# ---------------------------------------------------------------------------
# 1. Offline SQL translator, script splitter, DDL, & optional-psycopg contract
# ---------------------------------------------------------------------------
def test_translate_sqlite_sql_and_script_splitting() -> None:
    assert translate_sqlite_sql("PRAGMA journal_mode=WAL;") is None
    assert translate_sqlite_sql("  pragma foreign_keys = ON ") is None
    assert translate_sqlite_sql("BEGIN IMMEDIATE;") == "BEGIN"

    sm_sql = translate_sqlite_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        has_params=True,
    )
    assert sm_sql is not None
    assert "pg_tables" in sm_sql
    assert "current_schema()" in sm_sql
    assert "%s" in sm_sql
    assert "?" not in sm_sql

    like_sql = translate_sqlite_sql(
        "SELECT object_id FROM ontology_objects WHERE title LIKE '100%' AND object_type = ?",
        has_params=True,
    )
    assert like_sql == "SELECT object_id FROM ontology_objects WHERE title LIKE '100%%' AND object_type = %s"

    ign_sql = translate_sqlite_sql(
        "INSERT OR IGNORE INTO ontology_links (link_type, source_type, source_id, target_type, target_id) "
        "VALUES (?, ?, ?, ?, ?)",
        has_params=True,
    )
    assert ign_sql is not None
    assert ign_sql.startswith("INSERT INTO ontology_links")
    assert ign_sql.endswith("ON CONFLICT DO NOTHING")
    assert ign_sql.count("%s") == 5

    rep_sql = translate_sqlite_sql(
        "INSERT OR REPLACE INTO ontology_overlay (object_type, object_id, patch_json, updated_at) "
        "VALUES (?, ?, ?, ?)",
        has_params=True,
    )
    assert rep_sql is not None
    assert "ON CONFLICT (object_type, object_id) DO UPDATE SET" in rep_sql
    assert "patch_json = EXCLUDED.patch_json" in rep_sql
    assert "updated_at = EXCLUDED.updated_at" in rep_sql

    ddl_text = SCHEMA_PG_PATH.read_text(encoding="utf-8")
    stmts = split_sql_script(ddl_text)
    assert any("CREATE OR REPLACE FUNCTION _oo_enforce_write_gate()" in s for s in stmts)
    assert any("TG_TABLE_SCHEMA" in s for s in stmts)
    assert any("CREATE OR REPLACE FUNCTION _oo_forbid_action_runs_delete()" in s for s in stmts)


def test_require_psycopg_error_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_backend, "psycopg", None)
    monkeypatch.setattr(
        postgres_backend,
        "_PSYCOPG_IMPORT_ERROR",
        ModuleNotFoundError("No module named 'psycopg'"),
    )
    with pytest.raises(ImportError, match="psycopg"):
        require_psycopg()


def test_import_open_ontology_and_postgres_backend_without_psycopg() -> None:
    probe = (
        "import sys; "
        "sys.modules['psycopg'] = None; "
        "sys.modules['psycopg.errors'] = None; "
        "import open_ontology; "
        "import open_ontology.postgres_backend as pb; "
        "assert pb.psycopg is None; "
        "assert 'psycopg' not in [k for k, v in sys.modules.items() if v is not None]"
    )
    res = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stdout={res.stdout!r}\nstderr={res.stderr!r}"


# ---------------------------------------------------------------------------
# 2. Live PostgreSQL 16 PL/pgSQL physical write-gate & append-only triggers
# ---------------------------------------------------------------------------
def test_pg_physical_write_gate_and_append_only_triggers() -> None:
    spec, LedgerItem = _build_ledger_item_spec()
    with _isolated_pg_schema() as schema:
        rt = open_postgres_runtime(spec, PG_DSN, schema_name=schema)
        try:
            rt.load_snapshot(
                {
                    "LedgerItem": [
                        {"item_id": "IT-1", "title": "Plate", "balance": 10, "credit_limit": 500}
                    ]
                },
                actor=ADMIN,
            )
            run = rt.submit_action(
                "RUN-GATE-1",
                "annotate_item",
                {"item_id": "IT-1", "ops_tag": "urgent", "note": "check"},
                ADMIN,
            )
            assert run["status"] == "COMMITTED"

            # Direct adapter bypass must fail via GovernanceViolationError
            with pytest.raises(GovernanceViolationError, match="GOVERNANCE_VIOLATION"):
                rt.connection.execute(
                    "INSERT INTO ontology_objects "
                    "(object_type, object_id, title, properties_json, version, security_tags_json, content_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("LedgerItem", "ROGUE", "Hack", "{}", 1, "[]", "abc", "2026-01-01T00:00:00Z"),
                )

            # Raw external psycopg connection with default public search_path (tests TG_TABLE_SCHEMA)
            with psycopg.connect(PG_DSN, autocommit=True) as raw_conn:
                with pytest.raises(psycopg.errors.RaiseException, match="GOVERNANCE_VIOLATION"):
                    raw_conn.execute(
                        f'INSERT INTO "{schema}".ontology_objects '
                        "(object_type, object_id, title, properties_json, version, security_tags_json, content_hash, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        ("LedgerItem", "BYPASS", "Hack", "{}", 1, "[]", "abc", "2026-01-01T00:00:00Z"),
                    )
                with pytest.raises(psycopg.errors.RaiseException, match="GOVERNANCE_VIOLATION"):
                    raw_conn.execute(
                        f'UPDATE "{schema}".ontology_objects SET title = %s WHERE object_id = %s',
                        ("Tampered", "IT-1"),
                    )
                with pytest.raises(psycopg.errors.RaiseException, match="GOVERNANCE_VIOLATION"):
                    raw_conn.execute(
                        f'DELETE FROM "{schema}".ontology_objects WHERE object_id = %s',
                        ("IT-1",),
                    )
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    raw_conn.execute(
                        f'DELETE FROM "{schema}".ontology_action_runs WHERE run_id = %s',
                        ("RUN-GATE-1",),
                    )
                gate_row = raw_conn.execute(
                    f'SELECT is_open FROM "{schema}".ontology_write_gate WHERE id = 1'
                ).fetchone()
                assert gate_row is not None and gate_row[0] == 0

            # Legitimate state preserved
            item = rt.get_object(LedgerItem, "IT-1", ADMIN)
            assert item.ops_tag == "urgent"
        finally:
            rt.close()


# ---------------------------------------------------------------------------
# 3. Cross-connection OCC, barrier contention, OS cross-process OCC & idempotency
# ---------------------------------------------------------------------------
def test_pg_dual_connection_occ_barrier_and_cross_process_idempotency(tmp_path: Path) -> None:
    barrier = threading.Barrier(2, timeout=5.0)
    spec_race, _ = _build_ledger_item_spec(barrier=barrier)
    WorkDoc = spec_race._test_work_doc  # type: ignore[attr-defined]
    spec_plain, _ = _build_ledger_item_spec()

    with _isolated_pg_schema() as schema:
        rt_a = open_postgres_runtime(spec_race, PG_DSN, schema_name=schema)
        rt_b = open_postgres_runtime(spec_race, PG_DSN, schema_name=schema)
        rt_plain_b = open_postgres_runtime(spec_plain, PG_DSN, schema_name=schema)
        try:
            assert isinstance(rt_a, PostgresOntologyRuntime)
            rt_a.put_object(WorkDoc(doc_id="D-1", title="Init", counter=10, status="draft"))

            # (a) Simultaneous multi-threaded dual-connection barrier race on D-1
            outcomes: list[tuple[str, Any]] = []
            outcomes_lock = threading.Lock()

            def _worker(rt: PostgresOntologyRuntime, run_id: str, delta: int) -> None:
                try:
                    res = rt.submit_action(run_id, "bump_doc", {"doc_id": "D-1", "delta": delta}, ADMIN)
                    with outcomes_lock:
                        outcomes.append(("OK", res))
                except ConcurrencyConflictError as exc:
                    with outcomes_lock:
                        outcomes.append(("CONFLICT", str(exc)))

            t1 = threading.Thread(target=_worker, args=(rt_a, "RUN-RACE-A", 5))
            t2 = threading.Thread(target=_worker, args=(rt_b, "RUN-RACE-B", 7))
            t1.start()
            t2.start()
            t1.join(timeout=8.0)
            t2.join(timeout=8.0)

            assert len(outcomes) == 2
            oks = [o for o in outcomes if o[0] == "OK"]
            conflicts = [o for o in outcomes if o[0] == "CONFLICT"]
            assert len(oks) == 1, f"expected 1 winner, got {outcomes}"
            assert len(conflicts) == 1, f"expected 1 OCC loser, got {outcomes}"

            winner_run = oks[0][1]
            expected_counter = 15 if winner_run["run_id"] == "RUN-RACE-A" else 17
            doc_after_race = rt_plain_b.get_object(WorkDoc, "D-1", ADMIN)
            assert doc_after_race.counter == expected_counter

            with psycopg.connect(PG_DSN, autocommit=True) as raw_conn:
                ver_row = raw_conn.execute(
                    f'SELECT version FROM "{schema}".ontology_objects '
                    "WHERE object_type = 'WorkDoc' AND object_id = 'D-1'"
                ).fetchone()
                assert ver_row is not None and ver_row[0] == 2

            # (b) Cross-connection run_id idempotent replay vs parameter-mismatch conflict
            run_id_shared = "RUN-SHARED-IDEM"
            committed = rt_plain_b.submit_action(
                run_id_shared, "rename_doc", {"doc_id": "D-1", "title": "AgreedTitle"}, ADMIN
            )
            assert committed["status"] == "COMMITTED"

            rt_plain_a = open_postgres_runtime(spec_plain, PG_DSN, schema_name=schema)
            try:
                replay = rt_plain_a.submit_action(
                    run_id_shared, "rename_doc", {"doc_id": "D-1", "title": "AgreedTitle"}, ADMIN
                )
                assert replay["run_id"] == committed["run_id"]
                assert replay["status"] == "COMMITTED"
                assert rt_plain_a.get_object(WorkDoc, "D-1", ADMIN).title == "AgreedTitle"

                # Different params on same run_id -> strict ConcurrencyConflictError
                with pytest.raises(ConcurrencyConflictError, match="different parameters"):
                    rt_plain_a.submit_action(
                        run_id_shared, "rename_doc", {"doc_id": "D-1", "title": "HijackedTitle"}, ADMIN
                    )
                # Different action on same run_id -> strict ConcurrencyConflictError
                with pytest.raises(ConcurrencyConflictError, match="different parameters"):
                    rt_plain_a.submit_action(
                        run_id_shared, "bump_doc", {"doc_id": "D-1", "delta": 99}, ADMIN
                    )
                assert rt_plain_a.get_object(WorkDoc, "D-1", ADMIN).title == "AgreedTitle"
            finally:
                rt_plain_a.close()

            # (c) Real OS cross-process OCC race + cross-process run_id replay
            sync_dir = tmp_path / "proc_sync"
            sync_dir.mkdir(parents=True, exist_ok=True)
            child_script = (
                "import json, sys\n"
                "from pathlib import Path\n"
                "from open_ontology import Actor, ConcurrencyConflictError\n"
                "from open_ontology.postgres_backend import open_postgres_runtime\n"
                "from tests.test_postgres_backend import _build_ledger_item_spec\n"
                "dsn, schema, sync_path, replay_id = sys.argv[1:5]\n"
                "admin = Actor(actor_id='proc-2', role='admin', clearance_tags={'*'})\n"
                "spec_c, _ = _build_ledger_item_spec(sync_dir=Path(sync_path))\n"
                "rt = open_postgres_runtime(spec_c, dsn, schema_name=schema)\n"
                "try:\n"
                "    replay = rt.submit_action(replay_id, 'rename_doc', {'doc_id': 'D-1', 'title': 'AgreedTitle'}, admin)\n"
                "    occ_hit = False\n"
                "    try:\n"
                "        rt.submit_action('RUN-PROC-STALE', 'bump_doc', {'doc_id': 'D-1', 'delta': 100}, admin)\n"
                "    except ConcurrencyConflictError:\n"
                "        occ_hit = True\n"
                "    mismatch_hit = False\n"
                "    try:\n"
                "        rt.submit_action(replay_id, 'rename_doc', {'doc_id': 'D-1', 'title': 'ConflictProc'}, admin)\n"
                "    except ConcurrencyConflictError:\n"
                "        mismatch_hit = True\n"
                "    print(json.dumps({'replay_id': replay['run_id'], 'occ_hit': occ_hit, 'mismatch_hit': mismatch_hit}))\n"
                "finally:\n"
                "    rt.close()\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", child_script, PG_DSN, schema, str(sync_dir), run_id_shared],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                ready_file = sync_dir / "dry_run_ready"
                deadline = time.monotonic() + 8.0
                while not ready_file.exists():
                    if proc.poll() is not None:
                        out, err = proc.communicate()
                        raise AssertionError(f"child exited early rc={proc.returncode}: out={out!r} err={err!r}")
                    if time.monotonic() > deadline:
                        raise TimeoutError("child process never signaled dry-run snapshot read")
                    time.sleep(0.02)

                # Parent mutates D-1 in PG before child acquires _write_gate()
                parent_win = rt_plain_b.submit_action(
                    "RUN-PARENT-WIN", "bump_doc", {"doc_id": "D-1", "delta": 1}, ADMIN
                )
                assert parent_win["status"] == "COMMITTED"
                (sync_dir / "proceed").write_text("go", encoding="utf-8")

                out, err = proc.communicate(timeout=10.0)
                assert proc.returncode == 0, f"child failed: out={out!r}\nerr={err!r}"
                payload = json.loads(out.strip().splitlines()[-1])
                assert payload["replay_id"] == run_id_shared
                assert payload["occ_hit"] is True
                assert payload["mismatch_hit"] is True
            finally:
                if proc.poll() is None:
                    proc.kill()

            # Child stale action did zero dirty writes
            assert rt_plain_b.get_object(WorkDoc, "D-1", ADMIN).counter == expected_counter + 1
        finally:
            rt_a.close()
            rt_b.close()
            rt_plain_b.close()


# ---------------------------------------------------------------------------
# 4. Counterfactual fork(), simulate, revert_action, & __owned__ overlay survival
# ---------------------------------------------------------------------------
def test_pg_fork_sandbox_revert_and_owned_overlay_reconnect() -> None:
    spec, LedgerItem = _build_ledger_item_spec()
    WorkDoc = spec._test_work_doc  # type: ignore[attr-defined]

    with _isolated_pg_schema() as schema:
        rt1 = open_postgres_runtime(spec, PG_DSN, schema_name=schema)
        try:
            rt1.load_snapshot(
                {
                    "LedgerItem": [
                        {"item_id": "IT-10", "title": "Coil", "balance": 20, "credit_limit": 500}
                    ]
                },
                actor=ADMIN,
            )
            rt1.put_object(WorkDoc(doc_id="W-1", title="Original", counter=1, status="draft"))
            rt1.submit_action(
                "RUN-OWN-1",
                "annotate_item",
                {"item_id": "IT-10", "ops_tag": "vip", "note": "keep-across-refresh"},
                ADMIN,
            )
            rt1.submit_action("RUN-DOC-BUMP", "bump_doc", {"doc_id": "W-1", "delta": 4}, ADMIN)
        finally:
            rt1.close()

        # Reconnect fresh PG runtime on same schema
        rt2 = open_postgres_runtime(spec, PG_DSN, schema_name=schema)
        fork_rt = None
        try:
            # Refresh snapshot: source fields update, __owned__ overlay survives in PG
            rt2.load_snapshot(
                {
                    "LedgerItem": [
                        {"item_id": "IT-10", "title": "Coil v2", "balance": 20, "credit_limit": 900}
                    ]
                },
                actor=ADMIN,
            )
            item_after = rt2.get_object(LedgerItem, "IT-10", ADMIN)
            assert item_after.title == "Coil v2"
            assert item_after.credit_limit == 900
            assert item_after.ops_tag == "vip"
            assert item_after.note == "keep-across-refresh"

            # simulate_action reads PG without mutating
            sim = rt2.simulate_action("bump_doc", {"doc_id": "W-1", "delta": 10}, ADMIN)
            assert sim.valid is True
            assert rt2.get_object(WorkDoc, "W-1", ADMIN).counter == 5

            # fork() builds counterfactual :memory: SQLite sandbox via lock-free copy_to_sqlite
            fork_rt = rt2.fork()
            assert fork_rt.world_mode == "counterfactual"
            assert isinstance(fork_rt.connection, sqlite3.Connection)
            fork_rt.submit_action("FORK-RUN-1", "bump_doc", {"doc_id": "W-1", "delta": 100}, ADMIN)
            assert fork_rt.get_object(WorkDoc, "W-1", ADMIN).counter == 105
            # PG runtime untouched by counterfactual fork
            assert rt2.get_object(WorkDoc, "W-1", ADMIN).counter == 5

            # revert_action restores PG object state while keeping append-only run log
            rev = rt2.revert_action("RUN-DOC-BUMP", "RUN-DOC-REVERT", ADMIN)
            assert rev["status"] == "COMMITTED"
            assert rt2.get_object(WorkDoc, "W-1", ADMIN).counter == 1
            orig_run = rt2.get_action_run("RUN-DOC-BUMP")
            assert orig_run is not None and orig_run["status"] == "REVERTED"
            assert rt2.get_action_run("RUN-DOC-REVERT") is not None
        finally:
            if fork_rt is not None:
                fork_rt.close()
            rt2.close()


# ---------------------------------------------------------------------------
# 5. P2P + OntologyMCPServer + AutonomyRampController end-to-end on PG 16
# ---------------------------------------------------------------------------
def test_pg_p2p_mcp_and_autonomy_ramp_end_to_end() -> None:
    remote_state: dict[str, dict[str, dict[str, Any]]] = {
        "AccountingEntity": {
            "ENT-1": {"entity_id": "ENT-1", "name": "Buyer Corp", "base_currency": "CNY", "status": "active"}
        },
        "BusinessParty": {
            "PTY-1": {"party_id": "PTY-1", "legal_name": "Alloy Works", "tax_id": "TAX-99", "country_code": "CN", "status": "active"}
        },
        "P2pSupplierProfile": {
            "SUP-1": {
                "profile_id": "SUP-1",
                "party": "PTY-1",
                "accounting_entity": "ENT-1",
                "payment_terms": "NET30",
                "status": "active",
            }
        },
        "P2pPurchaseOrder": {
            "PO-SMALL": {
                "po_id": "PO-SMALL",
                "po_number": "PO-2026-001",
                "supplier": "SUP-1",
                "accounting_entity": "ENT-1",
                "ordered_qty": 10.0,
                "unit_price": 4000.0,
                "amount_total": 40000.0,
                "status": "draft",
            },
            "PO-LARGE": {
                "po_id": "PO-LARGE",
                "po_number": "PO-2026-002",
                "supplier": "SUP-1",
                "accounting_entity": "ENT-1",
                "ordered_qty": 10.0,
                "unit_price": 15000.0,
                "amount_total": 150000.0,
                "status": "draft",
            },
        },
    }
    actuate_calls: list[dict[str, Any]] = []

    def _actuate(move: dict[str, Any]) -> dict[str, Any]:
        actuate_calls.append(copy.deepcopy(move))
        po_id = str((move.get("params") or {}).get("po_id") or "")
        if move.get("action_type") == "confirm_p2p_order" and po_id in remote_state["P2pPurchaseOrder"]:
            remote_state["P2pPurchaseOrder"][po_id]["status"] = "confirmed"
        return {"ok": True}

    def _observe() -> dict[str, list[dict[str, Any]]]:
        return {
            t: [copy.deepcopy(row) for row in bucket.values()]
            for t, bucket in remote_state.items()
        }

    def _escalation_builder(ctx: dict[str, Any]) -> dict[str, Any]:
        params = dict(ctx["params"])
        return {
            "card_id": f"WC-{ctx['run_id']}",
            "card_kind": "APPROVAL",
            "target_object_type": "P2pPurchaseOrder",
            "target_object_id": str(params.get("po_id") or ""),
            "requested_action": str(ctx["action"]),
            "requested_params_json": json.dumps(params, sort_keys=True, ensure_ascii=False),
            "assignee_role": ctx["rule"].escalation_assignee_role or "procurement_manager",
        }

    bridge = SourceBridge(actuate=_actuate, observe=_observe)

    with _isolated_pg_schema() as schema:
        rt_seed = open_postgres_runtime(
            std_p2p_spec,
            PG_DSN,
            schema_name=schema,
            world_mode="coupled",
            source_bridge=bridge,
        )
        try:
            rt_seed.put_object(AccountingEntity(**remote_state["AccountingEntity"]["ENT-1"]))
            rt_seed.put_object(BusinessParty(**remote_state["BusinessParty"]["PTY-1"]))
            rt_seed.put_object(P2pSupplierProfile(**remote_state["P2pSupplierProfile"]["SUP-1"]))
            for row in remote_state["P2pPurchaseOrder"].values():
                rt_seed.put_object(P2pPurchaseOrder(**row))
        finally:
            rt_seed.close()

        policy = AutonomyPolicy(
            default_mode="SHADOW",
            default_escalation_action="open_work_card",
            default_escalation_assignee_role="procurement_manager",
            rules=(
                ActionAutonomyRule(
                    action="confirm_p2p_order",
                    roles=("ai_agent",),
                    mode="AUTO_UNDER_CEILING",
                    ceiling_field="amount_total",
                    max_value=100000.0,
                ),
            ),
        )
        controller = AutonomyRampController(
            policy,
            escalation_payload_builder=_escalation_builder,
        )

        rt_mcp = open_postgres_runtime(
            std_p2p_spec,
            PG_DSN,
            schema_name=schema,
            world_mode="coupled",
            source_bridge=bridge,
        )
        try:
            mcp = OntologyMCPServer(rt_mcp, AI_AGENT, autonomy_controller=controller)
            res_small = mcp.call_tool(
                "submit_action",
                {"run_id": "PG-P2P-1", "action": "confirm_p2p_order", "params": {"po_id": "PO-SMALL"}},
            )
            assert res_small["isError"] is False
            body_small = json.loads(res_small["content"][0]["text"])
            assert body_small["autonomy"]["outcome"] == "AUTO_COMMITTED"
            assert remote_state["P2pPurchaseOrder"]["PO-SMALL"]["status"] == "confirmed"

            res_large = mcp.call_tool(
                "submit_action",
                {"run_id": "PG-P2P-2", "action": "confirm_p2p_order", "params": {"po_id": "PO-LARGE"}},
            )
            assert res_large["isError"] is False
            body_large = json.loads(res_large["content"][0]["text"])
            assert body_large["autonomy"]["outcome"] == "ESCALATED_COMMITTED"
            assert body_large["autonomy"]["escalation"]["reason"] == "ceiling_exceeded"
            assert remote_state["P2pPurchaseOrder"]["PO-LARGE"]["status"] == "draft"
            assert len(actuate_calls) == 1
        finally:
            rt_mcp.close()

        # Verify persisted PG state from a brand new independent runtime connection
        rt_verify = open_postgres_runtime(std_p2p_spec, PG_DSN, schema_name=schema)
        try:
            assert rt_verify.get_object("P2pPurchaseOrder", "PO-SMALL", ADMIN).status == "confirmed"
            assert rt_verify.get_object("P2pPurchaseOrder", "PO-LARGE", ADMIN).status == "draft"
            cards = rt_verify.query_objects("WorkCard", ADMIN)
            assert len(cards) == 1
            assert cards[0].target_object_id == "PO-LARGE"
        finally:
            rt_verify.close()
