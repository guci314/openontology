"""数据摄入设备驱动（Ingestion Driver）。

**架构定位**：本模块是「设备驱动程序」，不是微内核。

微内核（``spec.py`` / ``sandbox.py`` / ``runtime.py``）只负责状态页表与
系统调用管线。外部业务系统的快照格式解析、多数据源按列
拼接（Column-wise MDO Stitching）、时间戳冲突裁决、overlay 重放这些
**协议相关**的活，全部属于设备驱动层，放在这里。

驱动以「借用内核能力」的方式工作：它接收一个 ``OntologyRuntime``，
通过内核公开的写门禁与 spec 注册表完成写入，**不复制内核的治理语义**。

对外公开：

* :class:`SnapshotIngestionDriver` —— 驱动类，内核通过它执行摄入；
* :func:`parse_mdo_rows` —— 把单个 MDO 源归一化成 ``[(主键, 列字典)]``；
* :func:`ingest_object` / :func:`load_snapshot` / :func:`load_mdo_snapshots`
  —— 与内核同名方法一一对应的纯函数式入口。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .spec import (
    Actor,
    GovernanceViolationError,
    ObjectType,
    ReindexConflictError,
)

__all__ = [
    "SnapshotIngestionDriver",
    "parse_mdo_rows",
    "ingest_object",
    "load_snapshot",
    "load_mdo_snapshots",
    "CONFLICT_STRATEGIES",
]

#: 支持的冲突裁决策略。
CONFLICT_STRATEGIES = ("user_edits_win", "most_recent_value")


# ---------------------------------------------------------------------------
# 驱动内部用到的纯工具（与内核保持同一份语义）
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    """把 ISO-8601 时间戳解析成带时区的 UTC datetime。"""
    text = str(value).strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(properties: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(_dumps(properties).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# MDO 源归一化（纯函数）
# ---------------------------------------------------------------------------
def parse_mdo_rows(
    source: Any, primary_key: str, index: int
) -> list[tuple[str, dict[str, Any]]]:
    """把一个 MDO 源归一化成 ``[(object_id, columns)]``。

    支持三种形状：

    1. ``{object_id: {列: 值}}`` —— 已按主键索引的列字典；
    2. ``{列: 值, ...}`` —— 单行扁平字典（必须自带主键列）；
    3. ``[{列: 值}, ...]`` —— 多行列表（每行必须自带主键列）。
    """
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


# ---------------------------------------------------------------------------
# 驱动类
# ---------------------------------------------------------------------------
class SnapshotIngestionDriver:
    """外部快照摄入驱动。

    用法::

        driver = SnapshotIngestionDriver(runtime)
        driver.load_snapshot(objects, links, conflict_strategy="most_recent_value")

    驱动只通过 ``runtime.spec``、``runtime._write_gate()`` 与
    ``runtime.evaluate_automations()`` 三个内核入口工作，不直接碰内核
    的内部状态，因此内核可以随时替换实现而不影响本驱动。
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # -- 内核能力的只读转发 ------------------------------------------------
    @property
    def spec(self) -> Any:
        return self.runtime.spec

    def _write_gate(self) -> Any:
        return self.runtime._write_gate()

    def _evaluate_automations(self, actor: Actor | None) -> None:
        self.runtime.evaluate_automations(actor)

    # -- 对象摄入 ----------------------------------------------------------
    def ingest_object(self, obj: ObjectType) -> dict[str, Any]:
        """对象摄入的兼容别名，转发到内核 ``put_object``。"""
        return self.runtime.put_object(obj)

    # -- 快照重索引 --------------------------------------------------------
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
        """替换 source-backed 底表，然后重放 ontology-owned overlay。

        完全本体自有的类型不允许出现在快照里；source-backed 行不得携带任何
        ``__owned__`` 声明的字段；若快照会让某个自有 patch 失去基础对象，
        整个操作被拒绝并回滚。

        冲突策略：

        * ``"user_edits_win"``（默认）：overlay 永远压过外来行；
        * ``"most_recent_value"``：当 ``source_timestamps`` 给出的 UTC 时间戳
          严格新于 ``ontology_overlay.updated_at`` 时，丢弃陈旧 patch 让源值胜出。
        """
        if conflict_strategy not in CONFLICT_STRATEGIES:
            raise ValueError(
                "conflict_strategy must be 'user_edits_win' or 'most_recent_value'"
            )

        normalized_objects = self._normalize_objects(objects)
        normalized_links = self._normalize_links(links)

        # 「出现在快照里的类型」必须按**输入字典的键**统计，而不是按归一化后的行。
        # 否则传入 ``{"Shipment": []}``（清空该类型）时类型名会丢失，
        # 既不删旧行，也让孤儿 overlay 保护失效。
        supplied_types = sorted(objects)
        supplied_link_types = sorted(links or {})

        with self._write_gate() as conn:
            existing = self._load_existing(conn, supplied_types)
            overlays, overlay_updated = self._load_overlays(conn)

            overlays_dropped: list[str] = []
            overlays_kept: list[str] = []
            if conflict_strategy == "most_recent_value":
                overlays_dropped, overlays_kept = self._drop_stale_overlays(
                    conn, overlays, overlay_updated, source_timestamps
                )

            self._wipe_supplied(conn, supplied_types, supplied_link_types)
            now = _utcnow()
            for type_name, cls, obj, base_properties in normalized_objects:
                self._write_object_row(
                    conn, type_name, cls, obj, base_properties, overlays, existing, now
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
            self._assert_no_orphan_overlay(conn, overlays)

        if auto_trigger:
            self._evaluate_automations(actor)

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

    # -- 多源列拼接 --------------------------------------------------------
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
        """按列拼接多个 MDO 源，然后走一次普通快照重索引。

        每个源只贡献同一批主键的部分列；后到的源在重叠列上胜出。
        overlay 重放与普通重索引完全一致。
        """
        cls = self.spec.get_object_type(object_type)
        primary_key = cls.primary_key_name()
        merged: dict[str, dict[str, Any]] = {}
        stats: list[dict[str, Any]] = []

        for index, source in enumerate(sources):
            columns_seen: set[str] = set()
            rows = parse_mdo_rows(source, primary_key, index)
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

    # -- 内部实现 ----------------------------------------------------------
    def _normalize_objects(
        self, objects: dict[str, list[dict[str, Any]]]
    ) -> list[tuple[str, type[ObjectType], ObjectType, dict[str, Any]]]:
        """校验并归一化快照对象行。"""
        normalized: list[
            tuple[str, type[ObjectType], ObjectType, dict[str, Any]]
        ] = []
        seen: set[tuple[str, str]] = set()

        for type_name, rows in objects.items():
            cls = self.spec.get_object_type(type_name)
            if cls.is_fully_owned():
                raise GovernanceViolationError(
                    f"object type {type_name!r} is fully ontology-owned; "
                    "a source snapshot may not supply it"
                )
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
                normalized.append((type_name, cls, obj, obj.to_properties()))
        return normalized

    def _normalize_links(
        self, links: dict[str, list[tuple[str, str]]] | None
    ) -> list[tuple[str, str, str, str, str]]:
        """校验并归一化快照关系。"""
        normalized: list[tuple[str, str, str, str, str]] = []
        for link_name, pairs in (links or {}).items():
            decl = self.spec.get_link_type(link_name)
            if decl.owned:
                raise GovernanceViolationError(
                    f"link type {link_name!r} is ontology-owned; "
                    "a source snapshot may not supply it"
                )
            for pair in pairs:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise GovernanceViolationError(
                        f"snapshot link {link_name!r} expects "
                        "(source_id, target_id) pairs"
                    )
                normalized.append(
                    (
                        decl.name,
                        decl.source,
                        str(pair[0]),
                        decl.target,
                        str(pair[1]),
                    )
                )
        return normalized

    @staticmethod
    def _load_existing(
        conn: Any, supplied_types: list[str]
    ) -> dict[tuple[str, str], tuple[int, dict[str, Any]]]:
        """读取待替换类型的现存版本与属性。"""
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
        return existing

    @staticmethod
    def _load_overlays(
        conn: Any,
    ) -> tuple[
        dict[tuple[str, str], dict[str, Any]],
        dict[tuple[str, str], str],
    ]:
        """读取全部自有字段 patch 及其更新时间。"""
        rows = conn.execute(
            "SELECT object_type, object_id, patch_json, updated_at "
            "FROM ontology_overlay"
        ).fetchall()
        overlays = {
            (row["object_type"], row["object_id"]): json.loads(row["patch_json"])
            for row in rows
        }
        updated = {
            (row["object_type"], row["object_id"]): row["updated_at"]
            for row in rows
        }
        return overlays, updated

    def _drop_stale_overlays(
        self,
        conn: Any,
        overlays: dict[tuple[str, str], dict[str, Any]],
        overlay_updated: dict[tuple[str, str], str],
        source_timestamps: dict[tuple[str, str], str] | None,
    ) -> tuple[list[str], list[str]]:
        """``most_recent_value`` 策略：源时间戳更新时丢弃陈旧 patch。"""
        stamps = source_timestamps or {}
        dropped: list[str] = []
        kept: list[str] = []
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
                dropped.append(label)
            else:
                kept.append(label)
        return dropped, kept

    @staticmethod
    def _wipe_supplied(
        conn: Any, supplied_types: list[str], supplied_link_types: list[str]
    ) -> None:
        """清掉即将被快照替换的底表行。"""
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

    def _write_object_row(
        self,
        conn: Any,
        type_name: str,
        cls: type[ObjectType],
        obj: ObjectType,
        base_properties: dict[str, Any],
        overlays: dict[tuple[str, str], dict[str, Any]],
        existing: dict[tuple[str, str], tuple[int, dict[str, Any]]],
        now: str,
    ) -> None:
        """写入一行「底表 + overlay」合并后的对象。"""
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

    @staticmethod
    def _assert_no_orphan_overlay(
        conn: Any, overlays: dict[tuple[str, str], dict[str, Any]]
    ) -> None:
        """孤儿保护：每个自有 patch 在快照应用后都必须还有基础对象。"""
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


# ---------------------------------------------------------------------------
# 纯函数式入口（与驱动方法一一对应，便于直接调用与测试）
# ---------------------------------------------------------------------------
def ingest_object(runtime: Any, obj: ObjectType) -> dict[str, Any]:
    return SnapshotIngestionDriver(runtime).ingest_object(obj)


def load_snapshot(runtime: Any, objects: Any, links: Any = None, **kwargs: Any) -> dict[str, Any]:
    return SnapshotIngestionDriver(runtime).load_snapshot(objects, links, **kwargs)


def load_mdo_snapshots(
    runtime: Any, object_type: Any, sources: Any, **kwargs: Any
) -> dict[str, Any]:
    return SnapshotIngestionDriver(runtime).load_mdo_snapshots(
        object_type, sources, **kwargs
    )
