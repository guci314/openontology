"""Pure M2 PostgreSQL 16 storage backend adapter (optional ``psycopg`` dependency).

Provides a SQLite-compatible connection wrapper (:class:`PostgresCompatConnection`)
and high-level entrypoint (:func:`open_postgres_runtime`) backed by
``open_ontology/schema_pg.sql`` with real PostgreSQL PL/pgSQL physical write-gate
triggers, append-only ``ontology_action_runs``, MVCC cross-connection OCC locking,
schema-level multi-tenant isolation, and lock-free ``copy_to_sqlite`` support for
``OntologyRuntime.fork()``.

Importing this module never fails when ``psycopg`` is absent; only connecting to
PostgreSQL requires ``psycopg[binary] >= 3.1``.
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence

try:
    import psycopg
    from psycopg import errors as pg_errors
except ImportError as exc:  # pragma: no cover - optional runtime dependency
    psycopg = None  # type: ignore[assignment]
    pg_errors = None  # type: ignore[assignment]
    _PSYCOPG_IMPORT_ERROR: ImportError | None = exc
else:
    _PSYCOPG_IMPORT_ERROR = None

from .runtime import OntologyRuntime, _CORE_SCHEMA_TABLES
from .settlement import SourceBridge, WorldMode
from .spec import (
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    OntologySpec,
)

__all__ = [
    "SCHEMA_PG_PATH",
    "PostgresCompatRow",
    "PostgresCompatCursor",
    "PostgresCompatConnection",
    "PostgresOntologyRuntime",
    "open_postgres_runtime",
    "translate_sqlite_sql",
    "split_sql_script",
    "require_psycopg",
]

SCHEMA_PG_PATH = Path(__file__).with_name("schema_pg.sql")

_SCHEMA_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_TABLE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "ontology_write_gate": ("id",),
    "ontology_registry": ("kind", "name"),
    "ontology_objects": ("object_type", "object_id"),
    "ontology_links": ("link_type", "source_type", "source_id", "target_type", "target_id"),
    "ontology_action_runs": ("run_id",),
    "ontology_overlay": ("object_type", "object_id"),
}

_DOLLAR_TAG_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]+)\)\s*VALUES\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_SQLITE_MASTER_TABLES_RE = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'",
    re.IGNORECASE,
)


def require_psycopg() -> Any:
    """Return imported ``psycopg`` module or raise a clear installation error."""
    if psycopg is None:
        raise ImportError(
            "psycopg (>=3.1) is required for the PostgreSQL backend; "
            'install with `pip install "psycopg[binary]>=3.1"`'
        ) from _PSYCOPG_IMPORT_ERROR
    return psycopg


def _validate_schema_name(schema_name: str) -> str:
    cleaned = str(schema_name).strip()
    if not _SCHEMA_IDENT_RE.fullmatch(cleaned):
        raise ValueError(
            f"invalid PostgreSQL schema_name {schema_name!r}; must match {_SCHEMA_IDENT_RE.pattern}"
        )
    return cleaned


def _replace_qmarks_and_percent(sql: str, *, escape_percent: bool) -> str:
    """Replace SQLite ``?`` placeholders outside strings/comments/dollar-quotes with ``%s``."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # Single-line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            if end == -1:
                seg = sql[i:]
                out.append(seg.replace("%", "%%") if escape_percent else seg)
                break
            seg = sql[i : end + 1]
            out.append(seg.replace("%", "%%") if escape_percent else seg)
            i = end + 1
            continue
        # Block comment
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                seg = sql[i:]
                out.append(seg.replace("%", "%%") if escape_percent else seg)
                break
            seg = sql[i : end + 2]
            out.append(seg.replace("%", "%%") if escape_percent else seg)
            i = end + 2
            continue
        # Single-quoted literal
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            seg = sql[i:j]
            out.append(seg.replace("%", "%%") if escape_percent else seg)
            i = j
            continue
        # Double-quoted identifier
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            seg = sql[i:j]
            out.append(seg.replace("%", "%%") if escape_percent else seg)
            i = j
            continue
        # Dollar-quoted PL/pgSQL string ($tag$...$tag$)
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql[i:])
            if m:
                tag = m.group(0)
                close_idx = sql.find(tag, i + len(tag))
                if close_idx != -1:
                    seg = sql[i : close_idx + len(tag)]
                    out.append(seg.replace("%", "%%") if escape_percent else seg)
                    i = close_idx + len(tag)
                    continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        if ch == "%" and escape_percent:
            out.append("%%")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_sql_script(script: str) -> list[str]:
    """Split a SQL script at top-level semicolons, respecting quotes and PL/pgSQL dollar blocks."""
    statements: list[str] = []
    start = 0
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "-" and i + 1 < n and script[i + 1] == "-":
            end = script.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if ch == "/" and i + 1 < n and script[i + 1] == "*":
            end = script.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch == "'":
            i += 1
            while i < n:
                if script[i] == "'":
                    if i + 1 < n and script[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                if script[i] == '"':
                    if i + 1 < n and script[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(script[i:])
            if m:
                tag = m.group(0)
                close_idx = script.find(tag, i + len(tag))
                if close_idx != -1:
                    i = close_idx + len(tag)
                    continue
        if ch == ";":
            stmt = script[start:i].strip()
            if stmt:
                statements.append(stmt)
            start = i + 1
        i += 1
    tail = script[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def translate_sqlite_sql(sql: str, *, has_params: bool = False) -> str | None:
    """Translate SQLite runtime SQL idioms into standard PostgreSQL 16 SQL.

    Returns ``None`` for SQLite-only no-ops such as ``PRAGMA ...``.
    """
    stripped = sql.strip()
    if not stripped:
        return ""

    core = stripped.rstrip(";").strip()
    upper = core.upper()

    if upper.startswith("PRAGMA ") or upper == "PRAGMA":
        return None
    if upper == "BEGIN IMMEDIATE":
        return "BEGIN"
    if _SQLITE_MASTER_TABLES_RE.search(core):
        translated = _SQLITE_MASTER_TABLES_RE.sub(
            "SELECT tablename AS name FROM pg_tables WHERE schemaname = current_schema()",
            core,
        )
        return _replace_qmarks_and_percent(translated, escape_percent=has_params)

    if _INSERT_OR_IGNORE_RE.match(core):
        rewritten = _INSERT_OR_IGNORE_RE.sub("INSERT INTO ", core, count=1).rstrip()
        if "ON CONFLICT" not in rewritten.upper():
            rewritten = f"{rewritten} ON CONFLICT DO NOTHING"
        return _replace_qmarks_and_percent(rewritten, escape_percent=has_params)

    m_replace = _INSERT_OR_REPLACE_RE.match(core)
    if m_replace:
        table = m_replace.group(1)
        cols_raw = m_replace.group(2)
        values_tail = m_replace.group(3).rstrip()
        cols = [c.strip().strip('"') for c in cols_raw.split(",") if c.strip()]
        pk_cols = _TABLE_PRIMARY_KEYS.get(table)
        if not pk_cols:
            raise ValueError(f"INSERT OR REPLACE unsupported on unknown table {table!r}")
        pk_set = set(pk_cols)
        non_pk = [c for c in cols if c not in pk_set]
        conflict_target = ", ".join(pk_cols)
        if non_pk:
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
            on_conflict = f"ON CONFLICT ({conflict_target}) DO UPDATE SET {updates}"
        else:
            on_conflict = f"ON CONFLICT ({conflict_target}) DO NOTHING"
        rewritten = f"INSERT INTO {table} ({cols_raw}) VALUES {values_tail} {on_conflict}"
        return _replace_qmarks_and_percent(rewritten, escape_percent=has_params)

    return _replace_qmarks_and_percent(core, escape_percent=has_params)


class PostgresCompatRow:
    """``sqlite3.Row``-compatible row object supporting index, slice, and key lookups."""

    __slots__ = ("_columns", "_values", "_index_by_name")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._index_by_name = {col.lower(): idx for idx, col in enumerate(self._columns)}

    def keys(self) -> list[str]:
        return list(self._columns)

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        if isinstance(key, str):
            idx = self._index_by_name.get(key.lower())
            if idx is None:
                raise IndexError(f"No item with that key: {key!r}")
            return self._values[idx]
        raise TypeError(f"indices must be integers, slices, or strings, not {type(key).__name__}")

    def __repr__(self) -> str:
        pairs = ", ".join(f"{c}={v!r}" for c, v in zip(self._columns, self._values))
        return f"PostgresCompatRow({pairs})"


class PostgresCompatCursor:
    """Eagerly materialized ``sqlite3.Cursor``-compatible wrapper over a ``psycopg`` cursor."""

    def __init__(
        self,
        *,
        rows: Sequence[PostgresCompatRow] = (),
        rowcount: int = -1,
    ) -> None:
        self._rows = list(rows)
        self._pos = 0
        self.rowcount = int(rowcount)

    def fetchone(self) -> PostgresCompatRow | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[PostgresCompatRow]:
        rest = self._rows[self._pos :]
        self._pos = len(self._rows)
        return list(rest)

    def fetchmany(self, size: int = 1) -> list[PostgresCompatRow]:
        end = min(len(self._rows), self._pos + max(0, int(size)))
        batch = self._rows[self._pos : end]
        self._pos = end
        return list(batch)

    def __iter__(self) -> Iterator[PostgresCompatRow]:
        return iter(self.fetchall())


def _translate_pg_exception(exc: Exception) -> Exception:
    msg = str(exc)
    if pg_errors is not None:
        if "GOVERNANCE_VIOLATION" in msg:
            return GovernanceViolationError(msg, code="GOVERNANCE_VIOLATION")
        if isinstance(exc, pg_errors.UniqueViolation):
            return ConcurrencyConflictError(msg)
        if isinstance(exc, pg_errors.IntegrityError):
            return GovernanceViolationError(msg)
    return exc


class PostgresCompatConnection:
    """Thread-safe ``sqlite3.Connection``-compatible adapter over PostgreSQL 16 (``psycopg``)."""

    def __init__(
        self,
        dsn: str,
        *,
        schema_name: str | None = None,
        auto_init_schema: bool = True,
    ) -> None:
        pg = require_psycopg()
        self.dsn = str(dsn)
        self.schema_name = _validate_schema_name(schema_name) if schema_name is not None else None
        self._lock = threading.RLock()
        self._pg_conn = pg.connect(self.dsn, autocommit=True)
        self.is_initialized = False
        self._closed = False

        if self.schema_name is not None:
            with self._lock:
                self._raw_exec(f'CREATE SCHEMA IF NOT EXISTS "{self.schema_name}"')
                self._raw_exec(f'SET search_path TO "{self.schema_name}"')

        if auto_init_schema:
            self.ensure_schema()

    @property
    def raw_connection(self) -> Any:
        return self._pg_conn

    def _raw_exec(self, sql: str, params: Sequence[Any] | None = None) -> PostgresCompatCursor:
        try:
            with self._pg_conn.cursor() as cur:
                if params:
                    cur.execute(sql, tuple(params))
                else:
                    cur.execute(sql)
                if cur.description is None:
                    return PostgresCompatCursor(rows=(), rowcount=cur.rowcount)
                cols = [desc.name for desc in cur.description]
                rows = [PostgresCompatRow(cols, vals) for vals in cur.fetchall()]
                return PostgresCompatCursor(rows=rows, rowcount=cur.rowcount)
        except Exception as exc:
            translated = _translate_pg_exception(exc)
            if translated is not exc:
                raise translated from exc
            raise

    def ensure_schema(self) -> None:
        with self._lock:
            rows = self._raw_exec(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
            existing = {str(r["tablename"]) for r in rows}
            if not _CORE_SCHEMA_TABLES <= existing:
                self.executescript(SCHEMA_PG_PATH.read_text(encoding="utf-8"))
            self.is_initialized = True

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> PostgresCompatCursor:
        with self._lock:
            if isinstance(params, Mapping):
                raise TypeError("named dict parameters are not supported; pass sequence parameters")
            param_tuple = tuple(params) if params else ()
            translated = translate_sqlite_sql(sql, has_params=bool(param_tuple))
            if translated is None or translated == "":
                return PostgresCompatCursor(rows=(), rowcount=0)
            return self._raw_exec(translated, param_tuple or None)

    def executescript(self, script: str) -> PostgresCompatCursor:
        with self._lock:
            last = PostgresCompatCursor(rows=(), rowcount=0)
            for stmt in split_sql_script(script):
                translated = translate_sqlite_sql(stmt, has_params=False)
                if translated is None or translated == "":
                    continue
                last = self._raw_exec(translated)
            return last

    def drop_schema(self) -> None:
        """Drop isolated non-public schema created for this connection, if configured."""
        if not self.schema_name or self.schema_name == "public":
            raise ValueError("refusing to drop 'public' or unset schema")
        with self._lock:
            self._raw_exec(f'DROP SCHEMA IF EXISTS "{self.schema_name}" CASCADE')
            self.is_initialized = False

    def copy_to_sqlite(self, target_sqlite_conn: sqlite3.Connection) -> None:
        """Read-only snapshot clone from PostgreSQL 16 into an in-memory SQLite ``fork()``."""
        with self._lock:
            reg_rows = self._raw_exec(
                "SELECT kind, name, spec_json, updated_at FROM ontology_registry ORDER BY kind, name"
            ).fetchall()
            obj_rows = self._raw_exec(
                "SELECT object_type, object_id, title, properties_json, version, "
                "security_tags_json, content_hash, updated_at "
                "FROM ontology_objects ORDER BY object_type, object_id"
            ).fetchall()
            link_rows = self._raw_exec(
                "SELECT link_type, source_type, source_id, target_type, target_id "
                "FROM ontology_links ORDER BY link_type, source_type, source_id, target_type, target_id"
            ).fetchall()
            run_rows = self._raw_exec(
                "SELECT run_id, action_type, actor_id, actor_role, params_json, status, "
                "read_versions_json, object_diff_json, approved_by, reverted_by_run_id, "
                "executed_at, error_code, error_message "
                "FROM ontology_action_runs ORDER BY executed_at, run_id"
            ).fetchall()
            overlay_rows = self._raw_exec(
                "SELECT object_type, object_id, patch_json, updated_at "
                "FROM ontology_overlay ORDER BY object_type, object_id"
            ).fetchall()

        target_sqlite_conn.execute("BEGIN IMMEDIATE")
        try:
            target_sqlite_conn.execute("UPDATE ontology_write_gate SET is_open = 1 WHERE id = 1")
            target_sqlite_conn.execute("DELETE FROM ontology_registry")
            target_sqlite_conn.execute("DELETE FROM ontology_objects")
            target_sqlite_conn.execute("DELETE FROM ontology_links")
            target_sqlite_conn.execute("DELETE FROM ontology_overlay")

            target_sqlite_conn.executemany(
                "INSERT INTO ontology_registry (kind, name, spec_json, updated_at) VALUES (?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3]) for r in reg_rows],
            )
            target_sqlite_conn.executemany(
                "INSERT INTO ontology_objects "
                "(object_type, object_id, title, properties_json, version, "
                "security_tags_json, content_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], int(r[4]), r[5], r[6], r[7]) for r in obj_rows],
            )
            target_sqlite_conn.executemany(
                "INSERT INTO ontology_links "
                "(link_type, source_type, source_id, target_type, target_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], r[4]) for r in link_rows],
            )
            target_sqlite_conn.executemany(
                "INSERT OR IGNORE INTO ontology_action_runs "
                "(run_id, action_type, actor_id, actor_role, params_json, status, "
                "read_versions_json, object_diff_json, approved_by, reverted_by_run_id, "
                "executed_at, error_code, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12]) for r in run_rows],
            )
            target_sqlite_conn.executemany(
                "INSERT INTO ontology_overlay (object_type, object_id, patch_json, updated_at) VALUES (?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3]) for r in overlay_rows],
            )
            target_sqlite_conn.execute("UPDATE ontology_write_gate SET is_open = 0 WHERE id = 1")
            target_sqlite_conn.execute("COMMIT")
        except BaseException:
            target_sqlite_conn.execute("ROLLBACK")
            raise

    @property
    def in_transaction(self) -> bool:
        tx_status = getattr(getattr(self._pg_conn, "info", None), "transaction_status", None)
        if tx_status is None:
            return False
        return int(tx_status) != 0

    def commit(self) -> None:
        with self._lock:
            if self.in_transaction:
                self._raw_exec("COMMIT")

    def rollback(self) -> None:
        with self._lock:
            if self.in_transaction:
                self._raw_exec("ROLLBACK")

    def executemany(
        self,
        sql: str,
        seq_of_params: Sequence[Sequence[Any]],
    ) -> PostgresCompatCursor:
        with self._lock:
            last = PostgresCompatCursor(rows=(), rowcount=0)
            for params in seq_of_params:
                last = self.execute(sql, params)
            return last

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._pg_conn.close()
                self._closed = True


class PostgresOntologyRuntime(OntologyRuntime):
    """PostgreSQL 16-backed ``OntologyRuntime`` with strict ``run_id`` parameter-conflict guards."""

    def submit_action(
        self,
        run_id: str,
        action_name: str,
        params: Any,
        actor: Actor,
        *,
        auto_trigger: bool = False,
    ) -> dict[str, Any]:
        action = self.spec.get_action(action_name)
        norm_params = self._build_params(action.params, params).model_dump(mode="json")
        existing = self.get_action_run(run_id)
        if existing is not None:
            if existing["action_type"] == action.name and existing["params"] == norm_params:
                return existing
            raise ConcurrencyConflictError(
                f"idempotency key {run_id!r} already committed for action "
                f"{existing['action_type']!r} with different parameters"
            )
        return super().submit_action(
            run_id,
            action_name,
            params,
            actor,
            auto_trigger=auto_trigger,
        )


def open_postgres_runtime(
    spec: OntologySpec,
    dsn: str,
    *,
    schema_name: str | None = None,
    world_mode: WorldMode | None = None,
    source_bridge: SourceBridge | None = None,
) -> PostgresOntologyRuntime:
    """Create a ``PostgresOntologyRuntime`` backed by a PostgreSQL 16 ``PostgresCompatConnection``."""
    conn = PostgresCompatConnection(dsn, schema_name=schema_name, auto_init_schema=True)
    return PostgresOntologyRuntime(
        spec,
        db_path=dsn,
        world_mode=world_mode,
        source_bridge=source_bridge,
        connection=conn,
    )
