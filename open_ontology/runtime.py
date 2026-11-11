"""L4 kinetic runtime.

``OntologyRuntime`` is the only component allowed to mutate the object graph.
It owns four responsibilities:

1. **Object state placement** with content-hash idempotency.
2. **Governed queries** with strict RBAC + row level security.
3. **Simulation** (zero writes) and **atomic submission** with optimistic
   locking and approval interception.
4. **Append-only business audit / time-machine revert.**

The physical write gate lives in SQLite itself: every object or link mutation
is rejected by a trigger unless the runtime opened the gate inside the same
transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import UnionType
from typing import Any, Callable, Iterator, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from .query import AggregationResult, AggregationRow, ObjectSet
from .sandbox import EditBatch, LoadedObject, ObjectDiff
from .spec import (
    ActionDef,
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    InvariantError,
    LinkType,
    ObjectNotFoundError,
    ObjectType,
    OntologySpec,
    PermissionDeniedError,
    ReindexConflictError,
)

__all__ = ["OntologyRuntime", "SimulationResult", "SCHEMA_PATH"]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_PENDING = "PENDING_APPROVAL"
_COMMITTED = "COMMITTED"
_REVERTED = "REVERTED"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    text = str(value).strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


_MASKED_STRING = "***MASKED***"


def _mask_value(cls: type[Any], field_name: str, current: Any) -> Any:
    """Pick the mask value for one sensitive column."""
    defaults = getattr(cls, "__mask_defaults__", None) or {}
    if field_name in defaults:
        return defaults[field_name]

    annotation = cls.model_fields[field_name].annotation
    origin = get_origin(annotation)
    if origin in (Union, UnionType) and type(None) in get_args(annotation):
        return None
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is str:
        return _MASKED_STRING

    # Fallback: infer from the live value.
    if isinstance(current, bool):
        return False
    if isinstance(current, int):
        return 0
    if isinstance(current, float):
        return 0.0
    if isinstance(current, str):
        return _MASKED_STRING
    return None


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: str | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    return json.loads(raw)


def _content_hash(properties: dict[str, Any]) -> str:
    return hashlib.sha256(_dumps(properties).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Simulation result
# ---------------------------------------------------------------------------
@dataclass
class SimulationResult:
    valid: bool
    diff: ObjectDiff
    requires_approval: bool
    read_versions: dict[str, int]
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "requires_approval": self.requires_approval,
            "read_versions": dict(self.read_versions),
            "diff": self.diff.to_dict(),
            "error": self.error,
            "error_code": self.error_code,
        }


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
class OntologyRuntime:
    """SQLite-backed ontology runtime (single connection, explicit tx)."""

    def __init__(
        self,
        spec: OntologySpec,
        db_path: str | Path = ":memory:",
    ) -> None:
        self.spec = spec
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # explicit BEGIN/COMMIT
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._closed = False
        # Automation de-duplication ledger: (name, object_id, version) -> record.
        self._automation_fires: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._sync_registry()

    # -- lifecycle ---------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        """Raw handle. Writes are still rejected by the physical gate."""
        return self._conn

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "OntologyRuntime":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- parallel-universe sandbox ----------------------------------------
    def fork(self) -> "OntologyRuntime":
        """把整个 runtime 克隆成一个完全独立的内存沙盘。

        SQLite 原生 ``Connection.backup()`` 一次性复制
        ``ontology_registry`` / ``ontology_objects`` / ``ontology_links`` /
        ``ontology_overlay`` / ``ontology_action_runs`` 以及写门禁状态，
        耗时是毫秒级。分叉支持嵌套（``fork1.fork()``），任何在 fork 上的
        写入对母体 100% 零污染。
        """
        fork = OntologyRuntime(self.spec, db_path=":memory:")
        self._conn.backup(fork._conn)
        return fork

    def state_fingerprint(self) -> dict[str, Any]:
        """结构化世界状态指纹，用于验证沙盘推演前后主库零污染。"""
        rows = self._conn.execute(
            "SELECT object_type, object_id, version FROM ontology_objects"
        ).fetchall()
        versions = {
            f"{row['object_type']}:{row['object_id']}": int(row["version"])
            for row in rows
        }
        counts = {
            "objects": len(versions),
            "links": self._scalar_count("ontology_links"),
            "overlay": self._scalar_count("ontology_overlay"),
            "runs": self._scalar_count("ontology_action_runs"),
        }
        return {"versions": versions, **counts}

    def _scalar_count(self, table: str) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def plan_mcts(self, actor: Actor, **kwargs: Any) -> Any:
        """快捷入口：``OntologyMCTSPlanner(self, actor, **kwargs).search()``。"""
        from .mcts import OntologyMCTSPlanner

        with OntologyMCTSPlanner(self, actor, **kwargs) as planner:
            return planner.search()

    # -- transactions ------------------------------------------------------
    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @contextmanager
    def _write_gate(self) -> Iterator[sqlite3.Connection]:
        """Open the physical gate for exactly one committed transaction.

        If anything raises, the transaction (including the gate flip) rolls
        back, so the gate can never be left open.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE ontology_write_gate SET is_open = 1 WHERE id = 1"
            )
            yield self._conn
            self._conn.execute(
                "UPDATE ontology_write_gate SET is_open = 0 WHERE id = 1"
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # -- registry bootstrapping -------------------------------------------
    def _sync_registry(self) -> None:
        rows: list[tuple[str, str, dict[str, Any]]] = []
        for name, cls in self.spec.object_types.items():
            rows.append(
                (
                    "object_type",
                    name,
                    {
                        "name": name,
                        "primary_key": cls.primary_key_name(),
                        "title": getattr(cls, "__title__", None),
                        "security_tag_fields": list(
                            getattr(cls, "__security_tag_fields__", ()) or ()
                        ),
                        "schema": cls.model_json_schema(),
                    },
                )
            )
        for name, decl in self.spec.link_types.items():
            rows.append(("link_type", name, decl.model_dump(mode="json")))
        for name, fn in self.spec.functions.items():
            rows.append(
                (
                    "function",
                    name,
                    {
                        "name": name,
                        "description": fn.description,
                        "params_schema": fn.params.model_json_schema(),
                    },
                )
            )
        for name, action in self.spec.actions.items():
            rows.append(
                (
                    "action",
                    name,
                    {
                        "name": name,
                        "description": action.description,
                        "allowed_roles": list(action.allowed_roles),
                        "approver_roles": list(action.approver_roles),
                        "affects_source": action.affects_source,
                        "params_schema": action.params.model_json_schema(),
                    },
                )
            )

        now = _utcnow()
        with self._transaction() as conn:
            for kind, name, spec in rows:
                conn.execute(
                    """
                    INSERT INTO ontology_registry (kind, name, spec_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(kind, name) DO UPDATE SET
                        spec_json = excluded.spec_json,
                        updated_at = excluded.updated_at
                    """,
                    (kind, name, _dumps(spec), now),
                )

    # -- batch loader contract (used by EditBatch) -------------------------
    def load_object(self, object_type: str, object_id: str) -> LoadedObject | None:
        row = self._conn.execute(
            """
            SELECT object_type, object_id, title, properties_json, version,
                   security_tags_json
            FROM ontology_objects
            WHERE object_type = ? AND object_id = ?
            """,
            (object_type, str(object_id)),
        ).fetchone()
        return self._row_to_loaded(row) if row else None

    def load_all(self, object_type: str) -> list[LoadedObject]:
        rows = self._conn.execute(
            """
            SELECT object_type, object_id, title, properties_json, version,
                   security_tags_json
            FROM ontology_objects
            WHERE object_type = ?
            ORDER BY object_id
            """,
            (object_type,),
        ).fetchall()
        return [self._row_to_loaded(row) for row in rows]

    def load_link_targets(
        self, link_type: str, source_type: str, source_id: str, target_type: str
    ) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT target_id FROM ontology_links
            WHERE link_type = ? AND source_type = ? AND source_id = ?
              AND target_type = ?
            ORDER BY target_id
            """,
            (link_type, source_type, str(source_id), target_type),
        ).fetchall()
        return [str(row["target_id"]) for row in rows]

    def load_link_sources(
        self, link_type: str, target_type: str, target_id: str
    ) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            """
            SELECT source_type, source_id FROM ontology_links
            WHERE link_type = ? AND target_type = ? AND target_id = ?
            ORDER BY source_type, source_id
            """,
            (link_type, target_type, str(target_id)),
        ).fetchall()
        return [(str(r["source_type"]), str(r["source_id"])) for r in rows]

    def link_exists(
        self,
        link_type: str,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
    ) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM ontology_links
            WHERE link_type = ? AND source_type = ? AND source_id = ?
              AND target_type = ? AND target_id = ?
            """,
            (link_type, source_type, str(source_id), target_type, str(target_id)),
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_loaded(row: sqlite3.Row) -> LoadedObject:
        return LoadedObject(
            object_type=row["object_type"],
            object_id=row["object_id"],
            data=json.loads(row["properties_json"]),
            version=int(row["version"]),
            title=row["title"],
            security_tags=list(json.loads(row["security_tags_json"])),
        )

    # -- object state placement -------------------------------------------
    def put_object(
        self,
        obj: ObjectType,
        *,
        auto_trigger: bool = False,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        """Place one object state into the ontology, keyed by content hash.

        Unchanged state is silently skipped and **does not bump version**;
        changed state is updated and increments ``version``.  With
        ``auto_trigger=True`` every armed automation is evaluated afterwards.
        """
        cls = self.spec.get_object_type(type(obj))
        object_type = cls.__name__
        object_id = obj.primary_key_value()
        properties = obj.to_properties()
        content_hash = _content_hash(properties)
        security_tags = obj.security_tags()
        title = obj.title_value()
        now = _utcnow()

        existing = self._conn.execute(
            """
            SELECT version, content_hash FROM ontology_objects
            WHERE object_type = ? AND object_id = ?
            """,
            (object_type, object_id),
        ).fetchone()

        if existing is not None and existing["content_hash"] == content_hash:
            if auto_trigger:
                self.evaluate_automations(actor)
            return {
                "status": "SKIPPED",
                "object_type": object_type,
                "object_id": object_id,
                "version": int(existing["version"]),
                "content_hash": content_hash,
            }

        with self._write_gate() as conn:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO ontology_objects (
                        object_type, object_id, title, properties_json, version,
                        security_tags_json, content_hash, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        object_type,
                        object_id,
                        title,
                        _dumps(properties),
                        _dumps(security_tags),
                        content_hash,
                        now,
                    ),
                )
                status = "CREATED"
                version = 1
            else:
                old_version = int(existing["version"])
                cursor = conn.execute(
                    """
                    UPDATE ontology_objects
                    SET title = ?, properties_json = ?, version = version + 1,
                        security_tags_json = ?, content_hash = ?, updated_at = ?
                    WHERE object_type = ? AND object_id = ? AND version = ?
                    """,
                    (
                        title,
                        _dumps(properties),
                        _dumps(security_tags),
                        content_hash,
                        now,
                        object_type,
                        object_id,
                        old_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrencyConflictError(
                        f"concurrent update while putting {object_type}:{object_id}"
                    )
                status = "UPDATED"
                version = old_version + 1

            self._sync_overlay(conn, object_type, object_id, properties)

        if auto_trigger:
            self.evaluate_automations(actor)
        return {
            "status": status,
            "object_type": object_type,
            "object_id": object_id,
            "version": version,
            "content_hash": content_hash,
        }

    def ingest_object(self, obj: ObjectType) -> dict[str, Any]:
        """Backward-compatible alias for :meth:`put_object`."""
        return self.put_object(obj)

    # -- snapshot reindex with field-level ownership ----------------------
    def load_snapshot(
        self,
        objects: dict[str, list[dict[str, Any]]],
        links: dict[str, list[tuple[str, str]]] | None = None,
        *,
        conflict_strategy: str = "user_edits_win",
        source_timestamps: dict[tuple[str, str], str] | None = None,
        auto_trigger: bool = False,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        """Replace source-backed base rows, then re-apply owned overlays.

        Fully ontology-owned types may not be supplied at all.  Source-backed
        rows may not carry any property declared in ``__owned__``.  If a
        snapshot would delete an object that still has an ontology-owned
        patch, the whole operation is rejected and rolled back.

        Conflict strategies (Palantir OSv2):

        * ``"user_edits_win"`` (default, Strategy 1): the ``ontology_overlay``
          patch always wins over the incoming source row.
        * ``"most_recent_value"`` (Strategy 2): when ``source_timestamps``
          carries a UTC timestamp for ``(object_type, object_id)`` that is
          strictly newer than ``ontology_overlay.updated_at``, the stale patch
          is dropped so the source value wins; otherwise the patch is kept.
        """
        if conflict_strategy not in ("user_edits_win", "most_recent_value"):
            raise ValueError(
                "conflict_strategy must be 'user_edits_win' or 'most_recent_value'"
            )
        normalized_objects: list[
            tuple[str, type[ObjectType], ObjectType, dict[str, Any]]
        ] = []
        supplied_types_set: set[str] = set()
        seen: set[tuple[str, str]] = set()

        for type_name, rows in objects.items():
            cls = self.spec.get_object_type(type_name)
            if cls.is_fully_owned():
                raise GovernanceViolationError(
                    f"object type {type_name!r} is fully ontology-owned; "
                    "a source snapshot may not supply it"
                )
            supplied_types_set.add(type_name)
            owned_fields = set(cls.owned_field_defaults())

            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise GovernanceViolationError(
                        f"snapshot row {type_name}[{index}] must be a dict"
                    )
                forbidden = sorted(owned_fields & set(row))
                if forbidden:
                    raise GovernanceViolationError(
                        f"snapshot row {type_name}[{index}] supplies "
                        f"ontology-owned fields {forbidden}"
                    )
                try:
                    obj = cls(**row)
                except ValidationError as exc:
                    raise GovernanceViolationError(
                        f"snapshot row {type_name}[{index}] does not match "
                        f"the object type: {exc}"
                    ) from exc
                object_id = obj.primary_key_value()
                key = (type_name, object_id)
                if key in seen:
                    raise GovernanceViolationError(
                        f"duplicate snapshot object {type_name}:{object_id}"
                    )
                seen.add(key)
                normalized_objects.append(
                    (type_name, cls, obj, obj.to_properties())
                )

        normalized_links: list[tuple[str, str, str, str, str]] = []
        supplied_link_types_set: set[str] = set()
        for link_name, pairs in (links or {}).items():
            decl = self.spec.get_link_type(link_name)
            if decl.owned:
                raise GovernanceViolationError(
                    f"link type {link_name!r} is ontology-owned; "
                    "a source snapshot may not supply it"
                )
            supplied_link_types_set.add(link_name)
            for pair in pairs:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise GovernanceViolationError(
                        f"snapshot link {link_name!r} expects (source_id, target_id) pairs"
                    )
                normalized_links.append(
                    (
                        decl.name,
                        decl.source,
                        str(pair[0]),
                        decl.target,
                        str(pair[1]),
                    )
                )

        supplied_types = sorted(supplied_types_set)
        supplied_link_types = sorted(supplied_link_types_set)

        with self._write_gate() as conn:
            existing: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
            for type_name in supplied_types:
                rows = conn.execute(
                    "SELECT object_id, version, properties_json "
                    "FROM ontology_objects WHERE object_type = ?",
                    (type_name,),
                ).fetchall()
                for row in rows:
                    existing[(type_name, row["object_id"])] = (
                        int(row["version"]),
                        json.loads(row["properties_json"]),
                    )

            overlay_rows = conn.execute(
                "SELECT object_type, object_id, patch_json, updated_at "
                "FROM ontology_overlay"
            ).fetchall()
            overlays = {
                (row["object_type"], row["object_id"]): json.loads(row["patch_json"])
                for row in overlay_rows
            }
            overlay_updated = {
                (row["object_type"], row["object_id"]): row["updated_at"]
                for row in overlay_rows
            }
            overlays_dropped: list[str] = []
            overlays_kept: list[str] = []

            if conflict_strategy == "most_recent_value":
                stamps = source_timestamps or {}
                for key in list(overlays):
                    source_ts = stamps.get(key)
                    if source_ts is None:
                        source_ts = stamps.get(f"{key[0]}:{key[1]}")  # type: ignore[arg-type]
                    if source_ts is None:
                        continue
                    label = f"{key[0]}:{key[1]}"
                    if _parse_utc(source_ts) > _parse_utc(overlay_updated[key]):
                        overlays.pop(key)
                        conn.execute(
                            "DELETE FROM ontology_overlay "
                            "WHERE object_type = ? AND object_id = ?",
                            key,
                        )
                        overlays_dropped.append(label)
                    else:
                        overlays_kept.append(label)

            for link_name in supplied_link_types:
                conn.execute(
                    "DELETE FROM ontology_links WHERE link_type = ?",
                    (link_name,),
                )
            for type_name in supplied_types:
                conn.execute(
                    "DELETE FROM ontology_objects WHERE object_type = ?",
                    (type_name,),
                )

            now = _utcnow()
            for type_name, cls, obj, base_properties in normalized_objects:
                object_id = obj.primary_key_value()
                patch = overlays.get((type_name, object_id), {})
                merged = {**base_properties, **patch}
                try:
                    merged_obj = cls(**merged)
                except ValidationError as exc:
                    raise ReindexConflictError(
                        f"owned patch for {type_name}:{object_id} no longer "
                        f"fits the object type: {exc}"
                    ) from exc
                properties = merged_obj.to_properties()
                old = existing.get((type_name, object_id))
                if old is None:
                    version = 1
                elif old[1] == properties:
                    version = old[0]
                else:
                    version = old[0] + 1

                conn.execute(
                    """
                    INSERT INTO ontology_objects (
                        object_type, object_id, title, properties_json, version,
                        security_tags_json, content_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        type_name,
                        object_id,
                        merged_obj.title_value(),
                        _dumps(properties),
                        version,
                        _dumps(merged_obj.security_tags()),
                        _content_hash(properties),
                        now,
                    ),
                )

            for link in normalized_links:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ontology_links (
                        link_type, source_type, source_id, target_type, target_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    link,
                )

            # Orphaned-overlay refusal: every owned patch must still have a
            # base object after the snapshot has been applied.
            for object_type, object_id in overlays:
                row = conn.execute(
                    "SELECT 1 FROM ontology_objects "
                    "WHERE object_type = ? AND object_id = ?",
                    (object_type, object_id),
                ).fetchone()
                if row is None:
                    raise ReindexConflictError(
                        "snapshot would orphan the ontology-owned patch for "
                        f"{object_type}:{object_id}"
                    )

        if auto_trigger:
            self.evaluate_automations(actor)
        return {
            "object_types": supplied_types,
            "objects": len(normalized_objects),
            "link_types": supplied_link_types,
            "links": len(normalized_links),
            "overlays_reapplied": sum(
                1 for object_type, _ in overlays if object_type in supplied_types
            ),
            "conflict_strategy": conflict_strategy,
            "overlays_dropped": overlays_dropped,
            "overlays_kept": overlays_kept,
        }

    # -- column level security / masking ----------------------------------
    def _apply_masking(self, obj: ObjectType, actor: Actor) -> ObjectType:
        """Return a masked copy of ``obj`` when ``actor`` may not see a column.

        The stored row is never touched: masking is a read-time projection.
        ``role == "system"`` bypasses masking (kernel-internal reads).
        """
        masked = getattr(type(obj), "__masked_fields__", None) or {}
        if not masked or actor.role == "system":
            return obj
        updates: dict[str, Any] = {}
        for field_name, allowed_roles in masked.items():
            if field_name not in type(obj).model_fields:
                continue
            if actor.role in allowed_roles:
                continue
            updates[field_name] = _mask_value(
                type(obj), field_name, getattr(obj, field_name, None)
            )
        if not updates:
            return obj
        return obj.model_copy(update=updates)

    # -- automations -------------------------------------------------------
    def evaluate_automations(
        self,
        actor: Actor | None = None,
        *,
        object_type: type[ObjectType] | str | None = None,
        automation: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run every armed automation over the watched objects.

        A trigger fires at most once per ``(automation, object_id, version)``;
        the version guard is what prevents condition/action feedback loops.
        """
        actor = actor or Actor(actor_id="system", role="system")
        wanted_type = (
            self.spec.get_object_type(object_type).__name__
            if object_type is not None
            else None
        )
        fired: list[dict[str, Any]] = []

        for name, decl in sorted(self.spec.automations.items()):
            if automation is not None and name != automation:
                continue
            cls = self.spec.get_object_type(decl.watch_object_type)
            if wanted_type is not None and cls.__name__ != wanted_type:
                continue
            for row in self.load_all(cls.__name__):
                obj = cls(**row.data)
                if not decl.condition(obj):
                    continue
                key = (name, row.object_id, row.version)
                if key in self._automation_fires:
                    continue
                result = decl.handler(self, obj, actor)
                record = {
                    "automation": name,
                    "object_type": cls.__name__,
                    "object_id": row.object_id,
                    "version": row.version,
                    "actor": actor.actor_id,
                    "result": result,
                }
                self._automation_fires[key] = record
                fired.append(record)
        return fired

    def automation_history(self) -> list[dict[str, Any]]:
        """Every automation trigger recorded so far, in fire order."""
        return list(self._automation_fires.values())

    # -- column-wise MDO ---------------------------------------------------
    @staticmethod
    def _mdo_rows(
        source: Any, primary_key: str, index: int
    ) -> list[tuple[str, dict[str, Any]]]:
        if isinstance(source, dict):
            if source and all(isinstance(v, dict) for v in source.values()):
                return [(str(key), dict(cols)) for key, cols in source.items()]
            if primary_key not in source:
                raise GovernanceViolationError(
                    f"MDO source {index} is a flat row without primary key "
                    f"{primary_key!r}"
                )
            return [(str(source[primary_key]), dict(source))]
        if isinstance(source, (list, tuple)):
            rows: list[tuple[str, dict[str, Any]]] = []
            for row_index, row in enumerate(source):
                if not isinstance(row, dict) or primary_key not in row:
                    raise GovernanceViolationError(
                        f"MDO source {index} row {row_index} must be a dict "
                        f"carrying {primary_key!r}"
                    )
                rows.append((str(row[primary_key]), dict(row)))
            return rows
        raise GovernanceViolationError(
            f"MDO source {index} must be a dict or a list of dicts"
        )

    def load_mdo_snapshots(
        self,
        object_type: type[ObjectType] | str,
        sources: list[Any],
        *,
        links: dict[str, list[tuple[str, str]]] | None = None,
        conflict_strategy: str = "user_edits_win",
        source_timestamps: dict[tuple[str, str], str] | None = None,
        auto_trigger: bool = False,
        actor: Actor | None = None,
    ) -> dict[str, Any]:
        """Column-wise MDO: stitch partial column sets from many sources.

        Each source contributes a subset of columns for the same primary keys;
        they are merged per column (later sources win on overlap) and then
        written through :meth:`load_snapshot`, so ontology-owned overlays are
        re-applied exactly as in a normal reindex.
        """
        cls = self.spec.get_object_type(object_type)
        primary_key = cls.primary_key_name()
        merged: dict[str, dict[str, Any]] = {}
        stats: list[dict[str, Any]] = []

        for index, source in enumerate(sources):
            columns_seen: set[str] = set()
            rows = self._mdo_rows(source, primary_key, index)
            for object_id, columns in rows:
                bucket = merged.setdefault(object_id, {})
                for column, value in columns.items():
                    if column == primary_key:
                        continue
                    bucket[column] = value
                    columns_seen.add(column)
            stats.append(
                {
                    "source": index,
                    "rows": len(rows),
                    "columns": sorted(columns_seen),
                }
            )

        rows_out = [
            {primary_key: object_id, **columns}
            for object_id, columns in merged.items()
        ]
        summary = self.load_snapshot(
            {cls.__name__: rows_out},
            links=links,
            conflict_strategy=conflict_strategy,
            source_timestamps=source_timestamps,
            auto_trigger=auto_trigger,
            actor=actor,
        )
        return {
            **summary,
            "mdo_sources": stats,
            "merged_objects": len(rows_out),
            "merged_columns": sorted(
                {column for columns in merged.values() for column in columns}
            ),
        }

    # -- governed queries --------------------------------------------------
    def get_object(
        self,
        cls: type[ObjectType] | str,
        object_id: str,
        actor: Actor,
    ) -> ObjectType:
        object_type = self.spec.get_object_type(cls).__name__
        row = self.load_object(object_type, str(object_id))
        if row is None:
            raise ObjectNotFoundError(f"{object_type}:{object_id} not found")
        actor.can_access(row.security_tags)
        obj = self.spec.get_object_type(object_type)(**row.data)
        return self._apply_masking(obj, actor)

    def query_interface(
        self,
        interface: Any,
        actor: Actor,
        filters: dict[str, Any] | Callable[[ObjectType], bool] | None = None,
    ) -> list[ObjectType]:
        """Polymorphic union query across every type implementing ``interface``."""
        decl = self.spec.get_interface(interface)
        results: list[ObjectType] = []
        for cls in self.spec.object_types_for_interface(decl):
            results.extend(self.query_objects(cls, actor, filters))
        return results

    def query_objects(
        self,
        cls: type[ObjectType] | str,
        actor: Actor,
        filters: dict[str, Any] | Callable[[ObjectType], bool] | None = None,
    ) -> list[ObjectType]:
        object_type = self.spec.get_object_type(cls).__name__
        model_cls = self.spec.get_object_type(object_type)
        results: list[ObjectType] = []
        for row in self.load_all(object_type):
            if not actor.has_access(row.security_tags):
                continue
            obj = model_cls(**row.data)
            if callable(filters):
                if not filters(obj):
                    continue
            elif filters:
                if any(getattr(obj, key, object()) != value for key, value in filters.items()):
                    continue
            results.append(self._apply_masking(obj, actor))
        return results

    def get_linked(
        self,
        source_obj: ObjectType,
        link: str | LinkType,
        target_cls: type[ObjectType] | str,
        actor: Actor,
    ) -> list[ObjectType]:
        decl = self.spec.get_link_type(link)
        source_type = type(source_obj).__name__
        target_type = self.spec.get_object_type(target_cls).__name__
        if source_type != decl.source or target_type != decl.target:
            raise ValueError(
                f"link {decl.name!r} spans {decl.source}->{decl.target}, "
                f"got {source_type}->{target_type}"
            )

        # the caller must be allowed to see the source object first
        source_row = self.load_object(source_type, source_obj.primary_key_value())
        if source_row is None:
            raise ObjectNotFoundError(
                f"{source_type}:{source_obj.primary_key_value()} not found"
            )
        actor.can_access(source_row.security_tags)

        targets: list[ObjectType] = []
        for target_id in self.load_link_targets(
            decl.name,
            source_type,
            source_obj.primary_key_value(),
            target_type,
        ):
            row = self.load_object(target_type, target_id)
            if row is None or not actor.has_access(row.security_tags):
                continue
            target = self.spec.get_object_type(target_type)(**row.data)
            targets.append(self._apply_masking(target, actor))
        return targets

    def get_linked_to(
        self,
        target_obj: ObjectType,
        link: str | LinkType,
        source_cls: type[ObjectType] | str,
        actor: Actor,
    ) -> list[ObjectType]:
        """Reverse traversal: every ``source`` whose ``link`` points at target."""
        decl = self.spec.get_link_type(link)
        source_type = self.spec.get_object_type(source_cls).__name__
        target_type = type(target_obj).__name__
        if source_type != decl.source or target_type != decl.target:
            raise ValueError(
                f"link {decl.name!r} spans {decl.source}->{decl.target}, "
                f"got {source_type}->{target_type}"
            )

        target_row = self.load_object(target_type, target_obj.primary_key_value())
        if target_row is None:
            raise ObjectNotFoundError(
                f"{target_type}:{target_obj.primary_key_value()} not found"
            )
        actor.can_access(target_row.security_tags)

        sources: list[ObjectType] = []
        for linked_source_type, source_id in self.load_link_sources(
            decl.name, target_type, target_obj.primary_key_value()
        ):
            if linked_source_type != source_type:
                continue
            row = self.load_object(source_type, source_id)
            if row is None or not actor.has_access(row.security_tags):
                continue
            source = self.spec.get_object_type(source_type)(**row.data)
            sources.append(self._apply_masking(source, actor))
        return sources

    # -- object-set algebra ------------------------------------------------
    def search_set(
        self,
        cls: type[ObjectType] | str,
        actor: Actor,
        filters: dict[str, Any] | Callable[[ObjectType], bool] | None = None,
    ) -> ObjectSet:
        object_type = self.spec.get_object_type(cls).__name__
        return ObjectSet(object_type, self.query_objects(cls, actor, filters))

    def pivot(
        self,
        source_set: ObjectSet,
        link: str | LinkType,
        actor: Actor,
        direction: str | None = None,
    ) -> ObjectSet:
        decl = self.spec.get_link_type(link)
        set_type = source_set.object_type

        if direction is None:
            if set_type == decl.source:
                direction = "forward"
            elif set_type == decl.target:
                direction = "reverse"
            else:
                raise ValueError(
                    f"object set type {set_type!r} is not an endpoint of link "
                    f"{decl.name!r} ({decl.source}->{decl.target})"
                )

        if direction == "forward":
            if set_type != decl.source:
                raise ValueError(
                    f"forward pivot of {decl.name!r} requires {decl.source!r}, "
                    f"got {set_type!r}"
                )
            target_type = decl.target
        elif direction == "reverse":
            if set_type != decl.target:
                raise ValueError(
                    f"reverse pivot of {decl.name!r} requires {decl.target!r}, "
                    f"got {set_type!r}"
                )
            target_type = decl.source
        else:
            raise ValueError("direction must be 'forward', 'reverse' or None")

        seen: set[str] = set()
        collected: list[ObjectType] = []

        for source_obj in source_set:
            source_id = source_obj.primary_key_value()
            source_row = self.load_object(set_type, source_id)
            if source_row is None or not actor.has_access(source_row.security_tags):
                continue

            if direction == "forward":
                target_ids = self.load_link_targets(
                    decl.name, set_type, source_id, target_type
                )
            else:
                target_ids = [
                    linked_id
                    for linked_type, linked_id in self.load_link_sources(
                        decl.name, set_type, source_id
                    )
                    if linked_type == target_type
                ]

            for target_id in target_ids:
                if target_id in seen:
                    continue
                target_row = self.load_object(target_type, target_id)
                if target_row is None or not actor.has_access(
                    target_row.security_tags
                ):
                    continue
                seen.add(target_id)
                collected.append(
                    self._apply_masking(
                        self.spec.get_object_type(target_type)(**target_row.data),
                        actor,
                    )
                )

        return ObjectSet(target_type, collected)

    @staticmethod
    def _require_same_type(a: ObjectSet, b: ObjectSet) -> None:
        if a.object_type != b.object_type:
            raise ValueError(
                f"object sets must share a type: {a.object_type!r} != {b.object_type!r}"
            )

    def union(self, a: ObjectSet, b: ObjectSet) -> ObjectSet:
        self._require_same_type(a, b)
        return ObjectSet(a.object_type, [*a, *b])

    def intersect(self, a: ObjectSet, b: ObjectSet) -> ObjectSet:
        self._require_same_type(a, b)
        b_ids = set(b.ids)
        return ObjectSet(
            a.object_type,
            [obj for obj in a if obj.primary_key_value() in b_ids],
        )

    def subtract(self, a: ObjectSet, b: ObjectSet) -> ObjectSet:
        self._require_same_type(a, b)
        b_ids = set(b.ids)
        return ObjectSet(
            a.object_type,
            [obj for obj in a if obj.primary_key_value() not in b_ids],
        )

    def aggregate(
        self,
        source_set: ObjectSet,
        group_by: str | None = None,
        sum_field: str | None = None,
    ) -> AggregationResult:
        model_cls = self.spec.get_object_type(source_set.object_type)
        if group_by is not None and group_by not in model_cls.model_fields:
            raise ValueError(
                f"unknown group_by field {group_by!r} on {model_cls.__name__}"
            )
        if sum_field is not None and sum_field not in model_cls.model_fields:
            raise ValueError(
                f"unknown sum_field {sum_field!r} on {model_cls.__name__}"
            )

        groups: dict[Any, list[ObjectType]] = {}
        for obj in source_set:
            key = None if group_by is None else getattr(obj, group_by)
            groups.setdefault(key, []).append(obj)

        if group_by is None and not groups:
            groups[None] = []

        rows: list[AggregationRow] = []
        for key, objects in groups.items():
            metrics: dict[str, float | int] = {"count": len(objects)}
            if sum_field is not None:
                total: float | int = 0
                for obj in objects:
                    value = getattr(obj, sum_field)
                    if value is None:
                        continue
                    if isinstance(value, bool) or not isinstance(
                        value, (int, float)
                    ):
                        raise TypeError(
                            f"sum_field {sum_field!r} is not numeric on "
                            f"{source_set.object_type}:{obj.primary_key_value()}"
                        )
                    total += value
                metrics["sum"] = total
            rows.append(
                AggregationRow(
                    key=key,
                    object_ids=[o.primary_key_value() for o in objects],
                    metrics=metrics,
                )
            )

        return AggregationResult(object_set=source_set, rows=rows)

    def filter_aggregation(
        self,
        agg: AggregationResult,
        predicate: Callable[[AggregationRow], bool],
    ) -> AggregationResult:
        rows = [row for row in agg.rows if predicate(row)]
        selected_ids: set[str] = set()
        for row in rows:
            selected_ids.update(row.object_ids)
        subset = [
            obj
            for obj in agg.object_set
            if obj.primary_key_value() in selected_ids
        ]
        return AggregationResult(
            object_set=ObjectSet(agg.object_set.object_type, subset),
            rows=rows,
        )

    # -- explicit link bootstrap ------------------------------------------
    def link_objects(
        self,
        source_obj: ObjectType,
        link: str | LinkType,
        target_obj: ObjectType,
    ) -> None:
        """Create one directed edge (governed, gate-protected).

        Useful for initial seeding; runtime actions normally manage links
        through the sandbox diff.
        """
        decl = self.spec.get_link_type(link)
        source_type = type(source_obj).__name__
        target_type = type(target_obj).__name__
        if source_type != decl.source or target_type != decl.target:
            raise ValueError(
                f"link {decl.name!r} spans {decl.source}->{decl.target}, "
                f"got {source_type}->{target_type}"
            )
        with self._write_gate() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO ontology_links (
                    link_type, source_type, source_id, target_type, target_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decl.name,
                    source_type,
                    source_obj.primary_key_value(),
                    target_type,
                    target_obj.primary_key_value(),
                ),
            )

    def unlink_objects(
        self,
        source_obj: ObjectType,
        link: str | LinkType,
        target_obj: ObjectType,
    ) -> None:
        """Delete one directed edge (governed, gate-protected)."""
        decl = self.spec.get_link_type(link)
        with self._write_gate() as conn:
            conn.execute(
                """
                DELETE FROM ontology_links
                WHERE link_type = ? AND source_type = ? AND source_id = ?
                  AND target_type = ? AND target_id = ?
                """,
                (
                    decl.name,
                    type(source_obj).__name__,
                    source_obj.primary_key_value(),
                    type(target_obj).__name__,
                    target_obj.primary_key_value(),
                ),
            )

    # -- functions ---------------------------------------------------------
    def call_function(
        self,
        name: str,
        params: BaseModel | dict[str, Any] | None = None,
        actor: Actor | None = None,
    ) -> Any:
        fn = self.spec.get_function(name)
        model = self._build_params(fn.params, params)
        batch = EditBatch(self.spec, self, actor or Actor(actor_id="system", role="system"))
        result = fn.handler(batch, model)
        # functions are read-only: any staged diff is intentionally discarded
        return result

    # -- simulate ----------------------------------------------------------
    def simulate_action(
        self,
        action_name: str,
        params: BaseModel | dict[str, Any] | None,
        actor: Actor,
    ) -> SimulationResult:
        action = self.spec.get_action(action_name)
        self._check_role(action, actor)
        model = self._build_params(action.params, params)

        batch = EditBatch(self.spec, self, actor)
        try:
            action.handler(batch, model)
            main_diff = batch.build_diff()
            self._check_authority(action, main_diff)
            if action.post_approval is not None:
                action.post_approval(batch, model)
            diff = batch.build_diff()
        except InvariantError as exc:
            return SimulationResult(
                valid=False,
                diff=ObjectDiff(),
                requires_approval=False,
                read_versions=dict(batch.read_versions),
                error=str(exc),
                error_code=exc.code,
            )

        return SimulationResult(
            valid=True,
            diff=diff,
            requires_approval=action.requires_approval(model, actor),
            read_versions=dict(batch.read_versions),
        )

    # -- submit ------------------------------------------------------------
    def submit_action(
        self,
        run_id: str,
        action_name: str,
        params: BaseModel | dict[str, Any] | None,
        actor: Actor,
        *,
        auto_trigger: bool = False,
    ) -> dict[str, Any]:
        """Idempotent governed submission.

        * no approval required -> single atomic transaction, status COMMITTED
        * approval required    -> diff stored, status PENDING_APPROVAL,
          objects untouched until :meth:`approve_action`
        """
        existing = self.get_action_run(run_id)
        if existing is not None:
            return existing

        action = self.spec.get_action(action_name)
        self._check_role(action, actor)
        model = self._build_params(action.params, params)
        params_json = model.model_dump(mode="json")

        batch = EditBatch(self.spec, self, actor)
        try:
            action.handler(batch, model)
            main_diff = batch.build_diff()
            self._check_authority(action, main_diff)
            if action.post_approval is not None:
                action.post_approval(batch, model)
            diff = batch.build_diff()
        except InvariantError as exc:
            # Refusal auditing: persist the failed attempt, then re-raise.
            self._record_rejection(
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                read_versions=dict(batch.read_versions),
                error=exc,
            )
            raise

        read_versions = dict(batch.read_versions)
        requires_approval = action.requires_approval(model, actor)
        diff_json = diff.to_dict()

        if requires_approval:
            with self._transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO ontology_action_runs (
                        run_id, action_type, actor_id, actor_role, params_json,
                        status, read_versions_json, object_diff_json,
                        approved_by, reverted_by_run_id, executed_at,
                        error_code, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL)
                    """,
                    (
                        run_id,
                        action.name,
                        actor.actor_id,
                        actor.role,
                        _dumps(params_json),
                        _PENDING,
                        _dumps(read_versions),
                        _dumps(diff_json),
                        _utcnow(),
                    ),
                )
            if auto_trigger:
                self.evaluate_automations(actor)
            return self.get_action_run(run_id)  # type: ignore[return-value]

        with self._write_gate() as conn:
            self._verify_read_versions(conn, read_versions)
            self._apply_diff(conn, diff)
            self._insert_run(
                conn,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                status=_COMMITTED,
                read_versions=read_versions,
                diff=diff,
                approved_by=None,
            )
        if auto_trigger:
            self.evaluate_automations(actor)
        return self.get_action_run(run_id)  # type: ignore[return-value]

    # -- approve -----------------------------------------------------------
    def approve_action(self, run_id: str, approver: Actor) -> dict[str, Any]:
        run = self.get_action_run(run_id)
        if run is None:
            raise ObjectNotFoundError(f"action run {run_id!r} not found")
        if run["status"] != _PENDING:
            raise GovernanceViolationError(
                f"action run {run_id!r} is {run['status']}, not PENDING_APPROVAL"
            )

        action = self.spec.get_action(run["action_type"])
        if approver.role not in action.approver_roles:
            raise PermissionDeniedError(
                f"actor {approver.actor_id!r} role {approver.role!r} may not approve "
                f"{action.name!r}; required one of {list(action.approver_roles)}"
            )

        read_versions = dict(run["read_versions"])
        diff = ObjectDiff.from_dict(run["object_diff"])

        with self._write_gate() as conn:
            self._verify_read_versions(conn, read_versions)
            self._apply_diff(conn, diff)
            conn.execute(
                """
                UPDATE ontology_action_runs
                SET status = ?, approved_by = ?, executed_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (_COMMITTED, approver.actor_id, _utcnow(), run_id, _PENDING),
            )
        return self.get_action_run(run_id)  # type: ignore[return-value]

    # -- revert (time machine) --------------------------------------------
    def revert_action(
        self,
        run_id: str,
        revert_run_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        existing = self.get_action_run(revert_run_id)
        if existing is not None:
            return existing

        run = self.get_action_run(run_id)
        if run is None:
            raise ObjectNotFoundError(f"action run {run_id!r} not found")

        action = self.spec.get_action(run["action_type"])
        if actor.role not in set(action.allowed_roles) | set(action.approver_roles):
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} role {actor.role!r} may not revert "
                f"{action.name!r}"
            )

        if run["status"] == _REVERTED:
            raise GovernanceViolationError(f"action run {run_id!r} was already reverted")
        if run["status"] != _COMMITTED:
            raise GovernanceViolationError(
                f"action run {run_id!r} is {run['status']}; only COMMITTED runs can revert"
            )

        original_diff = ObjectDiff.from_dict(run["object_diff"])
        inverted = original_diff.inverted()

        # Capture current versions of every object the revert will touch.
        revert_read_versions: dict[str, int] = {}
        for change in inverted.object_changes:
            row = self._conn.execute(
                """
                SELECT version FROM ontology_objects
                WHERE object_type = ? AND object_id = ?
                """,
                (change.object_type, change.object_id),
            ).fetchone()
            if row is not None:
                revert_read_versions[
                    f"{change.object_type}:{change.object_id}"
                ] = int(row["version"])

        with self._write_gate() as conn:
            self._apply_diff(conn, inverted)
            conn.execute(
                """
                UPDATE ontology_action_runs
                SET status = ?, reverted_by_run_id = ?
                WHERE run_id = ? AND status = ?
                """,
                (_REVERTED, revert_run_id, run_id, _COMMITTED),
            )
            self._insert_run(
                conn,
                run_id=revert_run_id,
                action=action,
                actor=actor,
                params_json=run["params"],
                status=_COMMITTED,
                read_versions=revert_read_versions,
                diff=inverted,
                approved_by=None,
            )
        return self.get_action_run(revert_run_id)  # type: ignore[return-value]

    # -- apply diff --------------------------------------------------------
    def _apply_diff(self, conn: sqlite3.Connection, diff: ObjectDiff) -> None:
        """Apply a diff. Must run inside ``_write_gate``."""
        now = _utcnow()

        for change in diff.object_changes:
            if change.kind == "create":
                assert change.after is not None
                exists = conn.execute(
                    """
                    SELECT 1 FROM ontology_objects
                    WHERE object_type = ? AND object_id = ?
                    """,
                    (change.object_type, change.object_id),
                ).fetchone()
                if exists is not None:
                    raise ConcurrencyConflictError(
                        f"cannot create {change.object_type}:{change.object_id}: "
                        "object already exists"
                    )
                version = change.after_version or 1
                conn.execute(
                    """
                    INSERT INTO ontology_objects (
                        object_type, object_id, title, properties_json, version,
                        security_tags_json, content_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        change.object_type,
                        change.object_id,
                        change.after_title,
                        _dumps(change.after),
                        version,
                        _dumps(change.after_security_tags),
                        _content_hash(change.after),
                        now,
                    ),
                )
                self._sync_overlay(
                    conn,
                    change.object_type,
                    change.object_id,
                    change.after,
                )

            elif change.kind == "update":
                assert change.after is not None
                row = conn.execute(
                    """
                    SELECT version FROM ontology_objects
                    WHERE object_type = ? AND object_id = ?
                    """,
                    (change.object_type, change.object_id),
                ).fetchone()
                if row is None:
                    raise ConcurrencyConflictError(
                        f"cannot update {change.object_type}:{change.object_id}: "
                        "object vanished"
                    )
                current_version = int(row["version"])
                if (
                    change.before_version is not None
                    and current_version != change.before_version
                ):
                    raise ConcurrencyConflictError(
                        f"stale update on {change.object_type}:{change.object_id}: "
                        f"expected version {change.before_version}, "
                        f"found {current_version}"
                    )
                conn.execute(
                    """
                    UPDATE ontology_objects
                    SET title = ?, properties_json = ?, version = ?,
                        security_tags_json = ?, content_hash = ?, updated_at = ?
                    WHERE object_type = ? AND object_id = ?
                    """,
                    (
                        change.after_title,
                        _dumps(change.after),
                        current_version + 1,
                        _dumps(change.after_security_tags),
                        _content_hash(change.after),
                        now,
                        change.object_type,
                        change.object_id,
                    ),
                )
                self._sync_overlay(
                    conn,
                    change.object_type,
                    change.object_id,
                    change.after,
                )

            elif change.kind == "delete":
                row = conn.execute(
                    """
                    SELECT version FROM ontology_objects
                    WHERE object_type = ? AND object_id = ?
                    """,
                    (change.object_type, change.object_id),
                ).fetchone()
                if row is None:
                    continue
                current_version = int(row["version"])
                if (
                    change.before_version is not None
                    and current_version != change.before_version
                ):
                    raise ConcurrencyConflictError(
                        f"stale delete on {change.object_type}:{change.object_id}: "
                        f"expected version {change.before_version}, "
                        f"found {current_version}"
                    )
                conn.execute(
                    """
                    DELETE FROM ontology_objects
                    WHERE object_type = ? AND object_id = ?
                    """,
                    (change.object_type, change.object_id),
                )
                self._delete_overlay(conn, change.object_type, change.object_id)
            else:  # pragma: no cover - guarded by ObjectChange
                raise GovernanceViolationError(f"unknown object change kind {change.kind!r}")

        for link_change in diff.link_changes:
            if link_change.kind == "create":
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ontology_links (
                        link_type, source_type, source_id, target_type, target_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        link_change.link_type,
                        link_change.source_type,
                        link_change.source_id,
                        link_change.target_type,
                        link_change.target_id,
                    ),
                )
            elif link_change.kind == "delete":
                conn.execute(
                    """
                    DELETE FROM ontology_links
                    WHERE link_type = ? AND source_type = ? AND source_id = ?
                      AND target_type = ? AND target_id = ?
                    """,
                    (
                        link_change.link_type,
                        link_change.source_type,
                        link_change.source_id,
                        link_change.target_type,
                        link_change.target_id,
                    ),
                )
            else:  # pragma: no cover
                raise GovernanceViolationError(
                    f"unknown link change kind {link_change.kind!r}"
                )

    # -- internal helpers --------------------------------------------------
    @staticmethod
    def _build_params(
        params_cls: type[BaseModel],
        params: BaseModel | dict[str, Any] | None,
    ) -> BaseModel:
        if isinstance(params, params_cls):
            return params
        if isinstance(params, BaseModel):
            return params_cls(**params.model_dump(mode="json"))
        if params is None:
            return params_cls()
        return params_cls(**params)

    @staticmethod
    def _check_role(action: ActionDef, actor: Actor) -> None:
        if actor.role not in action.allowed_roles:
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} role {actor.role!r} may not execute "
                f"{action.name!r}; allowed roles: {list(action.allowed_roles)}"
            )

    # -- field-level ownership / authority --------------------------------
    def _owned_defaults(self, object_type: str) -> dict[str, Any]:
        cls = self.spec.get_object_type(object_type)
        return cls.owned_field_defaults()

    def _sync_overlay(
        self,
        conn: sqlite3.Connection,
        object_type: str,
        object_id: str,
        properties: dict[str, Any],
    ) -> None:
        defaults = self._owned_defaults(object_type)
        if not defaults:
            return
        patch = {
            key: properties.get(key, default)
            for key, default in defaults.items()
            if properties.get(key, default) != default
        }
        now = _utcnow()
        if patch:
            conn.execute(
                """
                INSERT INTO ontology_overlay (
                    object_type, object_id, patch_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(object_type, object_id) DO UPDATE SET
                    patch_json = excluded.patch_json,
                    updated_at = excluded.updated_at
                """,
                (object_type, object_id, _dumps(patch), now),
            )
        else:
            self._delete_overlay(conn, object_type, object_id)

    def _delete_overlay(
        self,
        conn: sqlite3.Connection,
        object_type: str,
        object_id: str,
    ) -> None:
        conn.execute(
            "DELETE FROM ontology_overlay "
            "WHERE object_type = ? AND object_id = ?",
            (object_type, object_id),
        )

    def _object_change_side(self, change: Any) -> str | None:
        cls = self.spec.get_object_type(change.object_type)
        if cls.is_fully_owned():
            return "ontology"

        owned_fields = set(cls.owned_field_defaults())
        if change.kind != "update":
            # creating or deleting the base row is a source-owned act
            return "source"

        before = change.before or {}
        after = change.after or {}
        changed = {
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        }
        touches_source = any(key not in owned_fields for key in changed)
        touches_owned = any(key in owned_fields for key in changed)
        if touches_source and touches_owned:
            return "mixed"
        if touches_owned:
            return "ontology"
        if touches_source:
            return "source"
        return None

    def _check_authority(self, action: ActionDef, diff: ObjectDiff) -> None:
        sides: set[str] = set()
        for change in diff.object_changes:
            side = self._object_change_side(change)
            if side == "mixed":
                raise InvariantError(
                    f"action {action.name!r} touches source and ontology-owned "
                    f"fields of {change.object_type}:{change.object_id} in one change",
                    code="MIXED_AUTHORITY",
                )
            if side is not None:
                sides.add(side)

        for link_change in diff.link_changes:
            decl = self.spec.get_link_type(link_change.link_type)
            sides.add("ontology" if decl.owned else "source")

        if len(sides) > 1:
            raise InvariantError(
                f"action {action.name!r} touches both source and ontology-owned "
                f"state: {sorted(sides)}",
                code="MIXED_AUTHORITY",
            )
        if sides == {"source"} and not action.affects_source:
            raise InvariantError(
                f"action {action.name!r} is declared affects_source=False but "
                "its edit plan writes source-backed state",
                code="UNDECLARED_SOURCE_WRITE",
            )
        if sides == {"ontology"} and action.affects_source:
            raise InvariantError(
                f"action {action.name!r} is declared affects_source=True but "
                "its edit plan only writes ontology-owned state",
                code="MISDECLARED_SOURCE_WRITE",
            )

    def _verify_read_versions(
        self, conn: sqlite3.Connection, read_versions: dict[str, int]
    ) -> None:
        for key, expected_version in read_versions.items():
            object_type, _, object_id = key.partition(":")
            row = conn.execute(
                """
                SELECT version FROM ontology_objects
                WHERE object_type = ? AND object_id = ?
                """,
                (object_type, object_id),
            ).fetchone()
            if row is None or int(row["version"]) != int(expected_version):
                actual = None if row is None else int(row["version"])
                raise ConcurrencyConflictError(
                    f"stale read on {key}: proposal read version "
                    f"{expected_version}, current version {actual}"
                )

    def _insert_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        status: str,
        read_versions: dict[str, int],
        diff: ObjectDiff,
        approved_by: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO ontology_action_runs (
                run_id, action_type, actor_id, actor_role, params_json, status,
                read_versions_json, object_diff_json, approved_by,
                reverted_by_run_id, executed_at, error_code, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                action.name,
                actor.actor_id,
                actor.role,
                _dumps(params_json),
                status,
                _dumps(read_versions),
                _dumps(diff.to_dict()),
                approved_by,
                _utcnow(),
                error_code,
                error_message,
            ),
        )

    def _record_rejection(
        self,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        error: InvariantError,
    ) -> None:
        with self._transaction() as conn:
            self._insert_run(
                conn,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                status="REJECTED",
                read_versions=read_versions,
                diff=ObjectDiff(),
                approved_by=None,
                error_code=error.code or "INVARIANT_ERROR",
                error_message=str(error),
            )

    # -- pure-business audit readers --------------------------------------
    def get_action_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM ontology_action_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_run(row) if row else None

    def list_action_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM ontology_action_runs ORDER BY executed_at, run_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ontology_action_runs WHERE status = ? "
                "ORDER BY executed_at, run_id",
                (status,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "action_type": row["action_type"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "params": json.loads(row["params_json"]),
            "status": row["status"],
            "read_versions": json.loads(row["read_versions_json"]),
            "object_diff": json.loads(row["object_diff_json"]),
            "approved_by": row["approved_by"],
            "reverted_by_run_id": row["reverted_by_run_id"],
            "executed_at": row["executed_at"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }

    # -- observability -----------------------------------------------------
    def count_objects(self, object_type: str | None = None) -> int:
        if object_type is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM ontology_objects").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM ontology_objects WHERE object_type = ?",
                (object_type,),
            ).fetchone()
        return int(row["n"])

    def list_objects(self, object_type: str | None = None) -> list[dict[str, Any]]:
        if object_type is None:
            rows = self._conn.execute(
                "SELECT * FROM ontology_objects ORDER BY object_type, object_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ontology_objects WHERE object_type = ? "
                "ORDER BY object_id",
                (object_type,),
            ).fetchall()
        return [
            {
                "object_type": row["object_type"],
                "object_id": row["object_id"],
                "title": row["title"],
                "properties": json.loads(row["properties_json"]),
                "version": int(row["version"]),
                "security_tags": json.loads(row["security_tags_json"]),
                "content_hash": row["content_hash"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
