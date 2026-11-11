"""L4 in-memory edit sandbox.

Every kinetic action runs against an :class:`EditBatch`.  Actions never touch
SQLite directly: they read a snapshot, stage creates/modifies/deletes and link
changes, and the runtime compiles everything into an :class:`ObjectDiff`
before it is validated, approved and atomically applied.

The sandbox supports read-your-own-writes, row level security, read version
tracking (for optimistic locking) and lossless diff inversion.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

from .spec import (
    Actor,
    InvariantError,
    LinkType,
    ObjectNotFoundError,
    ObjectType,
    OntologySpec,
)

__all__ = [
    "LoadedObject",
    "BatchLoader",
    "ObjectChange",
    "LinkChange",
    "ObjectDiff",
    "EditBatch",
]


# ---------------------------------------------------------------------------
# Loader contract (implemented by OntologyRuntime)
# ---------------------------------------------------------------------------
@dataclass
class LoadedObject:
    object_type: str
    object_id: str
    data: dict[str, Any]
    version: int
    title: str | None
    security_tags: list[str]


class BatchLoader(Protocol):
    def load_object(self, object_type: str, object_id: str) -> LoadedObject | None: ...

    def load_all(self, object_type: str) -> list[LoadedObject]: ...

    def load_link_targets(
        self, link_type: str, source_type: str, source_id: str, target_type: str
    ) -> list[str]: ...

    def load_link_sources(
        self, link_type: str, target_type: str, target_id: str
    ) -> list[tuple[str, str]]: ...

    def link_exists(
        self,
        link_type: str,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Diff primitives
# ---------------------------------------------------------------------------
@dataclass
class ObjectChange:
    object_type: str
    object_id: str
    kind: str  # create | update | delete
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    before_version: int | None = None
    after_version: int | None = None
    before_title: str | None = None
    after_title: str | None = None
    before_security_tags: list[str] = field(default_factory=list)
    after_security_tags: list[str] = field(default_factory=list)

    def inverted(self) -> "ObjectChange":
        flipped = {"create": "delete", "delete": "create", "update": "update"}[self.kind]
        return ObjectChange(
            object_type=self.object_type,
            object_id=self.object_id,
            kind=flipped,
            before=self.after,
            after=self.before,
            before_version=None,
            after_version=self.before_version,
            before_title=self.after_title,
            after_title=self.before_title,
            before_security_tags=list(self.after_security_tags),
            after_security_tags=list(self.before_security_tags),
        )

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ObjectChange":
        return cls(**raw)


@dataclass
class LinkChange:
    link_type: str
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    kind: str  # create | delete

    def inverted(self) -> "LinkChange":
        return LinkChange(
            link_type=self.link_type,
            source_type=self.source_type,
            source_id=self.source_id,
            target_type=self.target_type,
            target_id=self.target_id,
            kind="delete" if self.kind == "create" else "create",
        )

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.__dict__)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LinkChange":
        return cls(**raw)


@dataclass
class ObjectDiff:
    """Structured, serializable, invertible patch."""

    object_changes: list[ObjectChange] = field(default_factory=list)
    link_changes: list[LinkChange] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.object_changes and not self.link_changes

    def inverted(self) -> "ObjectDiff":
        return ObjectDiff(
            object_changes=[c.inverted() for c in self.object_changes],
            link_changes=[c.inverted() for c in self.link_changes],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_changes": [c.to_dict() for c in self.object_changes],
            "link_changes": [c.to_dict() for c in self.link_changes],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ObjectDiff":
        raw = raw or {}
        return cls(
            object_changes=[
                ObjectChange.from_dict(c) for c in raw.get("object_changes", [])
            ],
            link_changes=[
                LinkChange.from_dict(c) for c in raw.get("link_changes", [])
            ],
        )

    def summary(self) -> str:
        parts = [
            f"{c.kind}:{c.object_type}:{c.object_id}" for c in self.object_changes
        ]
        parts += [
            f"{c.kind}:{c.link_type}:{c.source_id}->{c.target_id}"
            for c in self.link_changes
        ]
        return ", ".join(parts) if parts else "<empty diff>"


# ---------------------------------------------------------------------------
# EditBatch
# ---------------------------------------------------------------------------
class EditBatch:
    """Staged, read-your-own-writes view over the object graph.

    Handler contract::

        def handler(ctx: EditBatch, params: BaseModel) -> None:
            obj = ctx.get(Shipment, "SHP-001")
            ctx.modify(obj, status="rerouted")
    """

    def __init__(
        self,
        spec: OntologySpec,
        loader: BatchLoader,
        actor: Actor,
    ) -> None:
        self.spec = spec
        self.loader = loader
        self.actor = actor

        # key "ObjectType:id" -> current staged properties
        self._objects: dict[str, dict[str, Any]] = {}
        # key -> original DB properties (None == created inside this batch)
        self._original: dict[str, dict[str, Any] | None] = {}
        # key -> create | update | delete
        self._kinds: dict[str, str] = {}
        # key -> version observed in DB
        self._versions: dict[str, int] = {}

        # link key -> desired existence (only touched links)
        self._link_state: dict[tuple[str, str, str, str, str], bool] = {}
        # link key -> original existence cache
        self._link_original: dict[tuple[str, str, str, str, str], bool] = {}

        self.read_versions: dict[str, int] = {}

    # -- internal helpers --------------------------------------------------
    @staticmethod
    def _key(object_type: str, object_id: str) -> str:
        return f"{object_type}:{object_id}"

    @staticmethod
    def _split_key(key: str) -> tuple[str, str]:
        object_type, _, object_id = key.partition(":")
        return object_type, object_id

    def _model_for(self, cls: type[ObjectType], data: dict[str, Any]) -> ObjectType:
        return cls(**data)

    def _meta(
        self, object_type: str, data: dict[str, Any] | None
    ) -> tuple[str | None, list[str]]:
        if data is None:
            return None, []
        cls = self.spec.get_object_type(object_type)
        obj = cls(**data)
        return obj.title_value(), obj.security_tags()

    def _load_visible(self, object_type: str, object_id: str) -> LoadedObject | None:
        """Load from DB, returning None when RLS hides the row."""
        row = self.loader.load_object(object_type, object_id)
        if row is None:
            return None
        if not self.actor.has_access(row.security_tags):
            return None
        return row

    def _ensure_loaded(self, object_type: str, object_id: str) -> str | None:
        key = self._key(object_type, object_id)
        if key in self._objects:
            return key
        row = self.loader.load_object(object_type, object_id)
        if row is None:
            return None
        # A write requires full clearance: raise instead of silently hiding.
        self.actor.can_access(row.security_tags)
        self._objects[key] = copy.deepcopy(row.data)
        self._original[key] = copy.deepcopy(row.data)
        self._versions[key] = row.version
        self.read_versions[key] = row.version
        return key

    def _link_key(
        self,
        link: str | LinkType,
        src: ObjectType,
        dst: ObjectType,
    ) -> tuple[str, str, str, str, str]:
        decl = self.spec.get_link_type(link)
        src_type = type(src).__name__
        dst_type = type(dst).__name__
        if src_type != decl.source:
            raise ValueError(
                f"link {decl.name!r} expects source {decl.source!r}, got {src_type!r}"
            )
        if dst_type != decl.target:
            raise ValueError(
                f"link {decl.name!r} expects target {decl.target!r}, got {dst_type!r}"
            )
        return (
            decl.name,
            src_type,
            src.primary_key_value(),
            dst_type,
            dst.primary_key_value(),
        )

    def _original_link_state(self, key: tuple[str, str, str, str, str]) -> bool:
        if key not in self._link_original:
            self._link_original[key] = self.loader.link_exists(*key)
        return self._link_original[key]

    # -- reads -------------------------------------------------------------
    def get(
        self, cls: type[ObjectType], object_id: str
    ) -> ObjectType | None:
        """Read one object with read-your-own-writes and RLS enforcement."""
        object_type = self.spec.get_object_type(cls).__name__
        object_id = str(object_id)
        key = self._key(object_type, object_id)

        if self._kinds.get(key) == "delete":
            return None
        if key in self._objects:
            # already loaded / staged: still validate visibility
            self.actor.can_access(self._meta(object_type, self._objects[key])[1])
            if key in self._versions:
                self.read_versions.setdefault(key, self._versions[key])
            return self._model_for(cls, self._objects[key])

        row = self._load_visible(object_type, object_id)
        if row is None:
            return None
        self._objects[key] = copy.deepcopy(row.data)
        self._original[key] = copy.deepcopy(row.data)
        self._versions[key] = row.version
        self.read_versions[key] = row.version
        return self._model_for(cls, row.data)

    def query(self, cls: type[ObjectType]) -> list[ObjectType]:
        """List every visible instance of ``cls`` (RLS filtered)."""
        object_type = self.spec.get_object_type(cls).__name__
        results: dict[str, ObjectType] = {}
        for row in self.loader.load_all(object_type):
            if not self.actor.has_access(row.security_tags):
                continue
            key = self._key(object_type, row.object_id)
            results[key] = self._model_for(cls, row.data)
            if key not in self._versions:
                self._versions[key] = row.version
                self.read_versions[key] = row.version
        # overlay staged objects
        for key, data in self._objects.items():
            otype, oid = self._split_key(key)
            if otype != object_type or self._kinds.get(key) == "delete":
                continue
            if self.actor.has_access(self._meta(otype, data)[1]):
                results[key] = self._model_for(cls, data)
        return list(results.values())

    def query_linked(
        self,
        source_obj: ObjectType,
        link: str | LinkType,
        target_cls: type[ObjectType],
    ) -> list[ObjectType]:
        """Traverse ``link`` from ``source_obj`` to ``target_cls`` targets."""
        decl = self.spec.get_link_type(link)
        src_type = type(source_obj).__name__
        tgt_type = self.spec.get_object_type(target_cls).__name__
        if src_type != decl.source or tgt_type != decl.target:
            raise ValueError(
                f"link {decl.name!r} spans {decl.source}->{decl.target}, "
                f"got {src_type}->{tgt_type}"
            )

        source_id = source_obj.primary_key_value()
        target_ids: set[str] = set(
            self.loader.load_link_targets(
                decl.name, src_type, source_id, tgt_type
            )
        )
        # overlay staged link changes for this source
        for (lname, stype, sid, ttype, tid), desired in self._link_state.items():
            if (lname, stype, sid, ttype) == (decl.name, src_type, source_id, tgt_type):
                if desired:
                    target_ids.add(tid)
                else:
                    target_ids.discard(tid)

        out: list[ObjectType] = []
        for target_id in sorted(target_ids):
            obj = self.get(target_cls, target_id)
            if obj is not None:
                out.append(obj)
        return out

    # -- writes ------------------------------------------------------------
    def create(self, obj: ObjectType) -> ObjectType:
        object_type = self.spec.get_object_type(type(obj)).__name__
        object_id = obj.primary_key_value()
        key = self._key(object_type, object_id)

        if key in self._objects and self._kinds.get(key) != "delete":
            raise ValueError(f"{key} already exists in this batch")
        if self._kinds.get(key) == "delete":
            # delete + recreate in one batch == a full update of the existing row
            tags = obj.security_tags()
            self.actor.can_access(tags)
            self._objects[key] = copy.deepcopy(obj.to_properties())
            self._kinds[key] = "update"
            return obj
        if self.loader.load_object(object_type, object_id) is not None:
            raise ValueError(f"{key} already exists in the database")

        tags = obj.security_tags()
        self.actor.can_access(tags)

        self._objects[key] = copy.deepcopy(obj.to_properties())
        self._original[key] = None
        self._kinds[key] = "create"
        return obj

    def modify(self, obj: ObjectType, **updates: Any) -> ObjectType:
        object_type = self.spec.get_object_type(type(obj)).__name__
        object_id = obj.primary_key_value()
        key = self._ensure_loaded(object_type, object_id)
        if key is None:
            raise ObjectNotFoundError(f"{object_type}:{object_id} not found")
        if self._kinds.get(key) == "delete":
            raise ObjectNotFoundError(f"{object_type}:{object_id} was deleted")

        merged = {**self._objects[key], **updates}
        validated = type(obj)(**merged)
        self.actor.can_access(validated.security_tags())

        self._objects[key] = validated.to_properties()
        if self._kinds.get(key) is None:
            self._kinds[key] = "update"
        return validated

    def delete(self, obj: ObjectType) -> None:
        object_type = self.spec.get_object_type(type(obj)).__name__
        object_id = obj.primary_key_value()
        key = self._ensure_loaded(object_type, object_id)
        if key is None:
            raise ObjectNotFoundError(f"{object_type}:{object_id} not found")

        if self._kinds.get(key) == "create":
            # created and deleted inside the same batch == no-op
            self._objects.pop(key, None)
            self._original.pop(key, None)
            self._kinds.pop(key, None)
            self._versions.pop(key, None)
            return

        self.actor.can_access(self._meta(object_type, self._objects[key])[1])
        self._kinds[key] = "delete"

    def link(self, src: ObjectType, link: str | LinkType, dst: ObjectType) -> None:
        key = self._link_key(link, src, dst)
        self._link_state[key] = True

    def unlink(self, src: ObjectType, link: str | LinkType, dst: ObjectType) -> None:
        key = self._link_key(link, src, dst)
        self._link_state[key] = False

    def unlink_all_to(
        self, link: str | LinkType, dst: ObjectType
    ) -> int:
        """Remove every incoming ``link`` edge that points at ``dst``."""
        decl = self.spec.get_link_type(link)
        dst_type = type(dst).__name__
        if dst_type != decl.target:
            raise ValueError(
                f"link {decl.name!r} points at {decl.target!r}, got {dst_type!r}"
            )
        dst_id = dst.primary_key_value()

        sources: set[tuple[str, str]] = set(
            self.loader.load_link_sources(decl.name, dst_type, dst_id)
        )
        # overlay staged creates so they can be unlinked too
        for (lname, stype, sid, ttype, tid), desired in self._link_state.items():
            if (lname, ttype, tid) != (decl.name, dst_type, dst_id):
                continue
            if desired:
                sources.add((stype, sid))
            else:
                sources.discard((stype, sid))

        removed = 0
        for source_type, source_id in sorted(sources):
            if source_type != decl.source:
                continue
            key = (decl.name, source_type, source_id, dst_type, dst_id)
            self._link_state[key] = False
            removed += 1
        return removed

    # -- diff compilation --------------------------------------------------
    def build_diff(self) -> ObjectDiff:
        object_changes: list[ObjectChange] = []

        for key in sorted(self._objects):
            kind = self._kinds.get(key)
            if kind is None:
                continue
            object_type, object_id = self._split_key(key)
            before = self._original.get(key)
            after = self._objects[key]
            before_version = self._versions.get(key)

            if kind == "update" and before == after:
                continue

            before_title, before_tags = self._meta(object_type, before)
            after_title, after_tags = self._meta(object_type, after)

            if kind == "create":
                object_changes.append(
                    ObjectChange(
                        object_type=object_type,
                        object_id=object_id,
                        kind="create",
                        before=None,
                        after=copy.deepcopy(after),
                        before_version=None,
                        after_version=None,
                        before_title=None,
                        after_title=after_title,
                        before_security_tags=[],
                        after_security_tags=after_tags,
                    )
                )
            elif kind == "delete":
                object_changes.append(
                    ObjectChange(
                        object_type=object_type,
                        object_id=object_id,
                        kind="delete",
                        before=copy.deepcopy(before),
                        after=None,
                        before_version=before_version,
                        after_version=None,
                        before_title=before_title,
                        after_title=None,
                        before_security_tags=before_tags,
                        after_security_tags=[],
                    )
                )
            else:
                object_changes.append(
                    ObjectChange(
                        object_type=object_type,
                        object_id=object_id,
                        kind="update",
                        before=copy.deepcopy(before),
                        after=copy.deepcopy(after),
                        before_version=before_version,
                        after_version=None,
                        before_title=before_title,
                        after_title=after_title,
                        before_security_tags=before_tags,
                        after_security_tags=after_tags,
                    )
                )

        link_changes: list[LinkChange] = []
        for key in sorted(self._link_state):
            desired = self._link_state[key]
            if self._original_link_state(key) == desired:
                continue
            link_type, source_type, source_id, target_type, target_id = key
            link_changes.append(
                LinkChange(
                    link_type=link_type,
                    source_type=source_type,
                    source_id=source_id,
                    target_type=target_type,
                    target_id=target_id,
                    kind="create" if desired else "delete",
                )
            )

        return ObjectDiff(
            object_changes=object_changes,
            link_changes=link_changes,
        )

    # -- convenience -------------------------------------------------------
    def require(self, cls: type[ObjectType], object_id: str) -> ObjectType:
        """Like :meth:`get` but raises when missing."""
        obj = self.get(cls, object_id)
        if obj is None:
            raise ObjectNotFoundError(f"{cls.__name__}:{object_id} not found")
        return obj

    def invalidate(self, message: str, code: str | None = None) -> None:
        """Small helper for action handlers."""
        raise InvariantError(message, code=code)
