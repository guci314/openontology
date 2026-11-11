"""ObjectSet algebra and aggregation primitives (v3).

A read-only, order-preserving, primary-key-deduplicated collection of ontology
objects.  All methods that produce a new set preserve the source ordering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .spec import ObjectType

__all__ = [
    "ObjectSet",
    "AggregationRow",
    "AggregationResult",
]


def _dedupe(objects: Iterable[ObjectType]) -> list[ObjectType]:
    seen: set[str] = set()
    unique: list[ObjectType] = []
    for obj in objects:
        object_id = obj.primary_key_value()
        if object_id in seen:
            continue
        seen.add(object_id)
        unique.append(obj)
    return unique


class ObjectSet:
    """Immutable-ish, deduplicated, order-preserving object collection."""

    __slots__ = ("_object_type", "_objects", "_ids")

    def __init__(self, object_type: str, objects: Iterable[ObjectType]) -> None:
        self._object_type = object_type
        self._objects = _dedupe(objects)
        self._ids = [obj.primary_key_value() for obj in self._objects]

    @property
    def object_type(self) -> str:
        return self._object_type

    @property
    def objects(self) -> list[ObjectType]:
        return list(self._objects)

    @property
    def ids(self) -> list[str]:
        return list(self._ids)

    def __iter__(self) -> Iterator[ObjectType]:
        return iter(self._objects)

    def __len__(self) -> int:
        return len(self._objects)

    def __bool__(self) -> bool:
        return bool(self._objects)

    def __contains__(self, obj: object) -> bool:
        if not isinstance(obj, ObjectType):
            return False
        if type(obj).__name__ != self._object_type:
            return False
        return obj.primary_key_value() in set(self._ids)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ObjectSet):
            return NotImplemented
        return self.object_type == other.object_type and self.ids == other.ids

    def __repr__(self) -> str:
        return f"ObjectSet({self.object_type!r}, ids={self.ids!r})"


@dataclass(frozen=True)
class AggregationRow:
    key: Any
    object_ids: list[str]
    metrics: dict[str, float | int]


@dataclass(frozen=True)
class AggregationResult:
    object_set: ObjectSet
    rows: list[AggregationRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.object_set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_set.object_type,
            "total": self.total,
            "rows": [
                {
                    "key": row.key,
                    "object_ids": list(row.object_ids),
                    "metrics": dict(row.metrics),
                }
                for row in self.rows
            ],
        }
