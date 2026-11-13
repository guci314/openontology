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
from typing import Any, Callable, Iterator

from pydantic import BaseModel

from .aspects import (
    ApprovalGateAspect,
    AutomationReflexAspect,
    ColumnMaskingAspect,
    MASKED_STRING as _MASKED_STRING,
    mask_value as _mask_value,
    IdempotencyAndOCCAspect,
    RejectionAuditAspect,
    SecurityAndAuthorityAspect,
    insert_action_run,
)
from .ingestion import SnapshotIngestionDriver, parse_mdo_rows
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
)

__all__ = ["OntologyRuntime", "SimulationResult", "SCHEMA_PATH"]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_COMMITTED = "COMMITTED"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        # 数据摄入设备驱动：外部快照解析 / 多源列拼接 / overlay 重放。
        self.ingestion = SnapshotIngestionDriver(self)
        self._sync_registry()

    # -- lifecycle ---------------------------------------------------------
    # -- kernel primitives exposed to syscall aspects ----------------------
    @staticmethod
    def _dumps(value: Any) -> str:
        return _dumps(value)

    @staticmethod
    def _utcnow() -> str:
        return _utcnow()

    @property
    def connection(self) -> sqlite3.Connection:
        """Raw handle. Writes are still rejected by the physical gate."""
        return self._conn

    @property
    def is_open(self) -> bool:
        """连接是否仍然可用（关闭后的 runtime 不能再被查询）。"""
        return not self._closed

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
            meta = cls.type_metadata()
            meta["schema"] = cls.model_json_schema()
            rows.append(("object_type", name, meta))
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
        with self._write_gate() as conn:
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
        """Backward-compatible alias for :meth:`put_object`（由摄入驱动转发）。"""
        return self.ingestion.ingest_object(obj)

    # -- snapshot reindex (delegated to the ingestion device driver) -------
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

        快照解析、多源列拼接与时间戳裁决属于**设备驱动层**，
        实现见 :mod:`open_ontology.ingestion`；内核只做委托。
        """
        return self.ingestion.load_snapshot(
            objects,
            links,
            conflict_strategy=conflict_strategy,
            source_timestamps=source_timestamps,
            auto_trigger=auto_trigger,
            actor=actor,
        )

    # -- column level security / masking (ColumnMaskingAspect) -------------
    def _apply_masking(self, obj: ObjectType, actor: Actor) -> ObjectType:
        """读出口脱敏：委托给 :class:`ColumnMaskingAspect`。"""
        return ColumnMaskingAspect.apply_masking(obj, actor)

    # -- automations (AutomationReflexAspect) ------------------------------
    def evaluate_automations(
        self,
        actor: Actor | None = None,
        *,
        object_type: type[ObjectType] | str | None = None,
        automation: str | None = None,
    ) -> list[dict[str, Any]]:
        """跑一遍已武装的自动化规则：委托给 :class:`AutomationReflexAspect`。"""
        return AutomationReflexAspect.evaluate_automations(
            self, actor, object_type=object_type, automation=automation
        )

    def automation_history(self) -> list[dict[str, Any]]:
        """按触发顺序返回全部自动化记录。"""
        return AutomationReflexAspect.automation_history(self)

    # -- column-wise MDO (delegated to the ingestion device driver) --------
    @staticmethod
    def _mdo_rows(
        source: Any, primary_key: str, index: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """兼容入口：真正的实现在 :mod:`open_ontology.ingestion`。"""
        return parse_mdo_rows(source, primary_key, index)

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

        实现见 :mod:`open_ontology.ingestion`；内核只做委托。
        """
        return self.ingestion.load_mdo_snapshots(
            object_type,
            sources,
            links=links,
            conflict_strategy=conflict_strategy,
            source_timestamps=source_timestamps,
            auto_trigger=auto_trigger,
            actor=actor,
        )

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
        actor = actor or Actor(actor_id="system", role="system")
        batch = EditBatch(self.spec, self, actor, mask_fn=self._apply_masking)
        # functions are read-only: any staged diff is intentionally discarded
        return fn.handler(batch, model)

    # -- simulate ----------------------------------------------------------
    def _run_handler(
        self,
        action: ActionDef,
        model: BaseModel,
        actor: Actor,
        *,
        batch: EditBatch | None = None,
    ) -> ObjectDiff:
        """跑一次动作 handler 并产出已通过主权校验的 diff。"""
        batch = batch or EditBatch(self.spec, self, actor)
        action.handler(batch, model)
        pre_diff = batch.build_diff()
        self._check_authority(action, pre_diff)
        if action.post_approval is not None:
            action.post_approval(batch, model)
            SecurityAndAuthorityAspect.check_post_approval_authority(
                self.spec, action, pre_diff, batch.build_diff()
            )
        return batch.build_diff()

    def simulate_action(
        self,
        action_name: str,
        params: BaseModel | dict[str, Any] | None,
        actor: Actor,
    ) -> SimulationResult:
        action = self.spec.get_action(action_name)
        self._check_role(action, actor)
        model = self._build_params(action.params, params)

        try:
            batch = EditBatch(self.spec, self, actor)
            diff = self._run_handler(action, model, actor, batch=batch)
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
        existing = IdempotencyAndOCCAspect.check_idempotent_run(self, run_id)
        if existing is not None:
            return existing

        action = self.spec.get_action(action_name)
        self._check_role(action, actor)
        model = self._build_params(action.params, params)
        params_json = model.model_dump(mode="json")

        batch = EditBatch(self.spec, self, actor)
        try:
            diff = self._run_handler(action, model, actor, batch=batch)
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
            ApprovalGateAspect.stage_pending_approval(
                self,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                read_versions=read_versions,
                diff=diff,
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
        """批准挂起单：委托给 :class:`ApprovalGateAspect`。"""
        return ApprovalGateAspect.approve_action(self, run_id, approver)

    # -- revert (time machine) --------------------------------------------
    def revert_action(
        self,
        run_id: str,
        revert_run_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """时光机回滚：委托给 :class:`ApprovalGateAspect`。"""
        return ApprovalGateAspect.revert_action(self, run_id, revert_run_id, actor)

    # -- apply diff --------------------------------------------------------
    def _apply_diff(self, conn: sqlite3.Connection, diff: ObjectDiff) -> None:
        """Apply a diff. Must run inside ``_write_gate``."""
        now = _utcnow()

        for change in diff.object_changes:
            if change.kind == "create":
                assert change.after is not None
                if IdempotencyAndOCCAspect.current_version(
                    conn, change.object_type, change.object_id
                ) is not None:
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
                self._sync_overlay(conn, change.object_type, change.object_id, change.after)

            elif change.kind == "update":
                assert change.after is not None
                current_version = IdempotencyAndOCCAspect.current_version(
                    conn, change.object_type, change.object_id
                )
                if current_version is None:
                    raise ConcurrencyConflictError(
                        f"cannot update {change.object_type}:{change.object_id}: "
                        "object vanished"
                    )
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
                self._sync_overlay(conn, change.object_type, change.object_id, change.after)

            elif change.kind == "delete":
                current_version = IdempotencyAndOCCAspect.current_version(
                    conn, change.object_type, change.object_id
                )
                if current_version is None:
                    continue
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
        """动作级 RBAC：委托给 :class:`SecurityAndAuthorityAspect`。"""
        SecurityAndAuthorityAspect.check_role(action, actor)

    # -- field-level ownership / authority --------------------------------
    def _sync_overlay(
        self,
        conn: sqlite3.Connection,
        object_type: str,
        object_id: str,
        properties: dict[str, Any],
    ) -> None:
        """自有字段持久化：委托给 :class:`SecurityAndAuthorityAspect`。"""
        SecurityAndAuthorityAspect.sync_overlay(
            self, conn, object_type, object_id, properties
        )

    def _delete_overlay(
        self,
        conn: sqlite3.Connection,
        object_type: str,
        object_id: str,
    ) -> None:
        SecurityAndAuthorityAspect.delete_overlay(conn, object_type, object_id)

    def _object_change_side(self, change: Any) -> str | None:
        """判断变更落在 source / ontology / mixed 侧：委托给切面。"""
        return SecurityAndAuthorityAspect.object_change_side(self.spec, change)

    def _check_authority(self, action: ActionDef, diff: ObjectDiff) -> None:
        """主权边界强校验：委托给 :class:`SecurityAndAuthorityAspect`。"""
        SecurityAndAuthorityAspect.check_authority(self.spec, action, diff)

    def _verify_read_versions(
        self, conn: sqlite3.Connection, read_versions: dict[str, int]
    ) -> None:
        """MVCC 乐观锁：委托给 :class:`IdempotencyAndOCCAspect`。"""
        IdempotencyAndOCCAspect.verify_read_versions(conn, read_versions)

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
        """审计账本写入：委托给 :func:`open_ontology.aspects.insert_action_run`。"""
        insert_action_run(
            self,
            conn,
            run_id=run_id,
            action=action,
            actor=actor,
            params_json=params_json,
            status=status,
            read_versions=read_versions,
            diff=diff,
            approved_by=approved_by,
            error_code=error_code,
            error_message=error_message,
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
        """被拒企图入账：委托给 :class:`RejectionAuditAspect`。"""
        RejectionAuditAspect.record_rejection(
            self,
            run_id=run_id,
            action=action,
            actor=actor,
            params_json=params_json,
            read_versions=read_versions,
            error=error,
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
