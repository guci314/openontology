"""系统调用切面（Syscall Aspects）。

**架构定位**：本模块是「计算机切片」，不是微内核本体。

微内核（``spec.py`` / ``sandbox.py`` / ``runtime.py``）负责状态页表与系统
调用管线；本模块把散落在管线里的**横切关注点**收拢成具名切面：

===================================  ========================================
切面                                  负责什么
===================================  ========================================
``ColumnMaskingAspect``               读出口列级脱敏（列级 RLS 投影）
``SecurityAndAuthorityAspect``        RBAC 角色门禁 + 字段主权边界校验
``IdempotencyAndOCCAspect``           幂等短路 + MVCC 乐观锁版本校验
``RejectionAuditAspect``              业务不变量被拒时的入账审计
``AutomationReflexAspect``            提交后自动化条件反射与去重
``IdleAwareCallbackAspect``           忙闲感知的回调投递（忙时暂存、闲时重放）
===================================  ========================================

每个切面都只通过内核**公开或受保护的既定入口**工作（``runtime.spec``、
``runtime._write_gate()``、``runtime._apply_diff()``、``runtime.get_action_run()``
等），因此切面可以独立测试、独立替换，而内核不需要知道切面内部细节。
"""

from __future__ import annotations

import copy
import hashlib
import json
from types import UnionType
from typing import Any, Callable, Union, get_args, get_origin

from .sandbox import EditBatch, ObjectDiff
from .spec import (
    Actor,
    ActionDef,
    ConcurrencyConflictError,
    GovernanceViolationError,
    InvariantError,
    ObjectNotFoundError,
    ObjectType,
    PermissionDeniedError,
)

__all__ = [
    "MASKED_STRING",
    "mask_value",
    "ColumnMaskingAspect",
    "SecurityAndAuthorityAspect",
    "IdempotencyAndOCCAspect",
    "RejectionAuditAspect",
    "AutomationReflexAspect",
    "IdleAwareCallbackAspect",
    "insert_action_run",
    "AUDIT_STATUS_REJECTED",
    "AUDIT_STATUS_COMMITTED",
    "AUDIT_STATUS_REVERTED",
]

#: 被拒运行的审计状态值。
AUDIT_STATUS_REJECTED = "REJECTED"
AUDIT_STATUS_COMMITTED = "COMMITTED"
AUDIT_STATUS_REVERTED = "REVERTED"

MASKED_STRING = "***MASKED***"


# ---------------------------------------------------------------------------
# 1. ColumnMaskingAspect —— 读出口列级脱敏
# ---------------------------------------------------------------------------
def mask_value(cls: type[Any], field_name: str, current: Any) -> Any:
    """为某个敏感列挑一个掩码值。

    优先用类型上显式声明的 ``__mask_defaults__``；否则按字段注解推断
    （``Optional`` → ``None``、``bool`` → ``False``、数值 → ``0``、
    字符串 → ``***MASKED***``），注解不可用时退回按现值推断。
    """
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
        return MASKED_STRING

    # Fallback: infer from the live value.
    if isinstance(current, bool):
        return False
    if isinstance(current, int):
        return 0
    if isinstance(current, float):
        return 0.0
    if isinstance(current, str):
        return MASKED_STRING
    return None


class ColumnMaskingAspect:
    """读出口脱敏切面：存储行永不改动，脱敏是读时投影。"""

    @staticmethod
    def apply_masking(obj: ObjectType, actor: Actor) -> ObjectType:
        """当 ``actor`` 无权看某列时返回一个脱敏副本。

        ``role == "system"`` 绕过脱敏（内核内部读取）。
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
            updates[field_name] = mask_value(
                type(obj), field_name, getattr(obj, field_name, None)
            )
        if not updates:
            return obj
        return obj.model_copy(update=updates)


# ---------------------------------------------------------------------------
# 2. SecurityAndAuthorityAspect —— RBAC / RLS 与字段主权门禁
# ---------------------------------------------------------------------------
class SecurityAndAuthorityAspect:
    """角色门禁与主权边界切面。"""

    @staticmethod
    def check_role(action: ActionDef, actor: Actor) -> None:
        """动作级 RBAC：``actor.role`` 必须在 ``action.allowed_roles`` 里。"""
        if actor.role not in action.allowed_roles:
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} role {actor.role!r} may not execute "
                f"{action.name!r}; allowed roles: {list(action.allowed_roles)}"
            )

    @staticmethod
    def _field_side(obj_cls: Any, before: dict[str, Any], after: dict[str, Any]) -> str | None:
        """同一对象上字段级增量的主权平面：source / ontology / mixed / None。"""
        owned_fields = set(obj_cls.owned_field_defaults())
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

    @classmethod
    def object_change_side(cls, spec: Any, change: Any) -> str | None:
        """判断一次对象变更落在 source 侧、ontology 侧，还是混写。"""
        cls_ = spec.get_object_type(change.object_type)
        if cls_.is_fully_owned():
            return "ontology"

        if change.kind != "update":
            # 创建或删除底表行属于 source 侧行为
            return "source"

        return cls._field_side(cls_, change.before or {}, change.after or {})

    @staticmethod
    def _assert_source_object_writable(
        spec: Any, action: ActionDef, object_type: str, object_id: str
    ) -> None:
        """禁止任何动作写入声明为外部只读源的对象或源平面。"""
        obj_cls = spec.get_object_type(object_type)
        if getattr(spec, "read_only_source", False) or obj_cls.is_read_only_source():
            raise InvariantError(
                f"action {action.name!r} cannot write read-only external source "
                f"state of {object_type}:{object_id}",
                code="SOURCE_READ_ONLY",
            )

    @staticmethod
    def _assert_append_only_respected(
        spec: Any, action: ActionDef, change: Any
    ) -> None:
        """禁止任何动作修改或删除声明为 ``__append_only__=True`` 的不可变对象。"""
        obj_cls = spec.get_object_type(change.object_type)
        if obj_cls.is_append_only() and change.kind in ("update", "delete"):
            raise InvariantError(
                f"action {action.name!r} cannot {change.kind} append-only object "
                f"{change.object_type}:{change.object_id}",
                code="APPEND_ONLY_VIOLATION",
            )

    @staticmethod
    def _assert_source_link_writable(
        spec: Any,
        action: ActionDef,
        decl: Any,
        source_type: str,
        target_type: str,
    ) -> None:
        """禁止任何动作修改只读外部源关系。"""
        if getattr(spec, "read_only_source", False):
            raise InvariantError(
                f"action {action.name!r} cannot modify read-only external source "
                f"link {decl.name!r}",
                code="SOURCE_READ_ONLY",
            )
        for type_name in (decl.domain, decl.range, source_type, target_type):
            if type_name and type_name in spec.object_types:
                if spec.object_types[type_name].is_read_only_source():
                    raise InvariantError(
                        f"action {action.name!r} cannot modify external source "
                        f"link {decl.name!r} involving read-only source type "
                        f"{type_name!r}",
                        code="SOURCE_READ_ONLY",
                    )

    @classmethod
    def check_authority(cls, spec: Any, action: ActionDef, diff: ObjectDiff) -> None:
        """主权边界强校验：只读源保护、混写、未声明源写入、误声明源写入全部拒绝。"""
        if getattr(spec, "read_only_source", False) and action.affects_source:
            raise InvariantError(
                f"action {action.name!r} is declared affects_source=True on a "
                "read_only_source OntologySpec",
                code="SOURCE_READ_ONLY",
            )

        sides: set[str] = set()
        for change in diff.object_changes:
            cls._assert_append_only_respected(spec, action, change)
            side = cls.object_change_side(spec, change)
            if side == "mixed":
                raise InvariantError(
                    f"action {action.name!r} touches source and ontology-owned "
                    f"fields of {change.object_type}:{change.object_id} in one change",
                    code="MIXED_AUTHORITY",
                )
            if side is not None:
                if side == "source":
                    cls._assert_source_object_writable(
                        spec, action, change.object_type, change.object_id
                    )
                sides.add(side)

        for link_change in diff.link_changes:
            decl = spec.get_object_property(link_change.link_type)
            link_side = "ontology" if cls._property_is_owned(spec, decl) else "source"
            if link_side == "source":
                cls._assert_source_link_writable(
                    spec,
                    action,
                    decl,
                    link_change.source_type,
                    link_change.target_type,
                )
            sides.add(link_side)

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

    @staticmethod
    def _property_is_owned(spec: Any, decl: Any) -> bool:
        if decl.owned:
            return True
        if decl.field and decl.domain in spec.object_types:
            dom_cls = spec.object_types[decl.domain]
            dom_owned = getattr(dom_cls, "__owned__", False)
            if dom_owned is True:
                return True
            if isinstance(dom_owned, dict):
                return bool(dom_owned.get(decl.field, False))
        return False

    # -- field-level ownership persistence --------------------------------
    @staticmethod
    def sync_overlay(
        runtime: Any,
        conn: Any,
        object_type: str,
        object_id: str,
        properties: dict[str, Any],
    ) -> None:
        """把自有字段的偏离写进 ``ontology_overlay``；回归默认值时清除。"""
        cls = runtime.spec.get_object_type(object_type)
        defaults = cls.owned_field_defaults()
        if not defaults:
            return
        patch = {
            key: properties.get(key, default)
            for key, default in defaults.items()
            if properties.get(key, default) != default
        }
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
                (object_type, object_id, runtime._dumps(patch), runtime._utcnow()),
            )
        else:
            SecurityAndAuthorityAspect.delete_overlay(conn, object_type, object_id)

    @staticmethod
    def delete_overlay(conn: Any, object_type: str, object_id: str) -> None:
        conn.execute(
            "DELETE FROM ontology_overlay "
            "WHERE object_type = ? AND object_id = ?",
            (object_type, object_id),
        )

    @staticmethod
    def inspect_storage_planes(
        runtime: Any,
        actor: Actor,
        object_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """按行级 RLS 与列级脱敏投影 Backing Dataset 与 Overlay 双平面。"""
        if object_type is not None:
            resolved_type = runtime.spec.get_object_type(object_type).__name__
            obj_rows = runtime._conn.execute(
                "SELECT * FROM ontology_objects WHERE object_type = ? ORDER BY object_id",
                (resolved_type,),
            ).fetchall()
            ov_rows = runtime._conn.execute(
                "SELECT object_type, object_id, patch_json FROM ontology_overlay "
                "WHERE object_type = ?",
                (resolved_type,),
            ).fetchall()
        else:
            obj_rows = runtime._conn.execute(
                "SELECT * FROM ontology_objects ORDER BY object_type, object_id"
            ).fetchall()
            ov_rows = runtime._conn.execute(
                "SELECT object_type, object_id, patch_json FROM ontology_overlay"
            ).fetchall()

        overlays: dict[tuple[str, str], dict[str, Any]] = {
            (str(r["object_type"]), str(r["object_id"])): json.loads(r["patch_json"])
            for r in ov_rows
        }
        entries: list[dict[str, Any]] = []
        for row in obj_rows:
            type_name = str(row["object_type"])
            obj_id = str(row["object_id"])
            security_tags = list(json.loads(row["security_tags_json"]))
            if not actor.has_access(security_tags):
                continue
            cls = runtime.spec.get_object_type(type_name)
            raw_props = json.loads(row["properties_json"])
            masked_obj = runtime._apply_masking(cls(**raw_props), actor)
            merged_props = masked_obj.to_properties()
            patch_raw = overlays.get((type_name, obj_id), {})
            if cls.is_fully_owned():
                source_plane: dict[str, Any] = {}
                overlay_plane: dict[str, Any] = dict(merged_props)
                has_overlay = True
                owned_mode = "fully_owned"
            else:
                source_plane = masked_obj.source_properties()
                overlay_plane = {
                    k: merged_props.get(k, v) for k, v in patch_raw.items()
                }
                has_overlay = bool(overlay_plane)
                owned_mode = "hybrid" if cls.owned_field_defaults() else "source_only"
            entries.append(
                {
                    "object_type": type_name,
                    "object_id": obj_id,
                    "version": int(row["version"]),
                    "owned_mode": owned_mode,
                    "has_overlay": has_overlay,
                    "source": source_plane,
                    "overlay": overlay_plane,
                    "merged": merged_props,
                }
            )
        return entries

    @staticmethod
    def inspect_health(runtime: Any) -> dict[str, Any]:
        """对当前世界状态执行只读全量健康巡检（Schema / 引用完整性 / @spec.invariant）。"""
        rows = runtime._conn.execute(
            """
            SELECT object_type, object_id, properties_json
            FROM ontology_objects
            ORDER BY object_type, object_id
            """
        ).fetchall()
        objects_checked = len(rows)
        existing_keys = {
            (str(r["object_type"]), str(r["object_id"])) for r in rows
        }

        violations: list[dict[str, str]] = []
        valid_rows: list[tuple[type[ObjectType], str, dict[str, Any]]] = []

        for r in rows:
            otype = str(r["object_type"])
            oid = str(r["object_id"])
            if otype not in runtime.spec.object_types:
                violations.append(
                    {
                        "kind": "schema",
                        "code": "UNKNOWN_OBJECT_TYPE",
                        "subject": f"{otype}:{oid}",
                        "message": f"未知对象类型 {otype!r}",
                    }
                )
                continue
            cls = runtime.spec.object_types[otype]
            try:
                raw = json.loads(r["properties_json"])
                cls(**raw)
            except Exception as exc:
                violations.append(
                    {
                        "kind": "schema",
                        "code": "SCHEMA_VALIDATION_ERROR",
                        "subject": f"{otype}:{oid}",
                        "message": str(exc),
                    }
                )
            else:
                valid_rows.append((cls, oid, raw))

        reported_dangling_edges: set[tuple[str, str, str, str, str]] = set()
        for cls, oid, raw in valid_rows:
            prop_map = getattr(cls, "__object_properties__", None) or {}
            for field_name, decl in prop_map.items():
                if decl.range not in runtime.spec.object_types:
                    continue
                for tid in EditBatch._extract_field_targets(raw.get(field_name)):
                    if (decl.range, tid) not in existing_keys:
                        reported_dangling_edges.add(
                            (decl.name, cls.__name__, oid, decl.range, tid)
                        )
                        violations.append(
                            {
                                "kind": "reference",
                                "code": "DANGLING_OBJECT_PROPERTY",
                                "subject": f"{cls.__name__}:{oid}.{field_name}",
                                "message": (
                                    f"{cls.__name__}:{oid}.{field_name} "
                                    f"指向不存在的 {decl.range}:{tid}"
                                ),
                            }
                        )

        link_rows = runtime._conn.execute(
            """
            SELECT link_type, source_type, source_id, target_type, target_id
            FROM ontology_links
            ORDER BY link_type, source_type, source_id, target_type, target_id
            """
        ).fetchall()
        links_checked = len(link_rows)
        for lr in link_rows:
            edge = (
                str(lr["link_type"]),
                str(lr["source_type"]),
                str(lr["source_id"]),
                str(lr["target_type"]),
                str(lr["target_id"]),
            )
            if edge in reported_dangling_edges:
                continue
            if (edge[1], edge[2]) not in existing_keys or (
                edge[3],
                edge[4],
            ) not in existing_keys:
                violations.append(
                    {
                        "kind": "reference",
                        "code": "DANGLING_LINK",
                        "subject": f"{edge[0]}:{edge[1]}:{edge[2]}->{edge[3]}:{edge[4]}",
                        "message": (
                            f"关系边 {edge[0]} 引用了缺失端点 "
                            f"{edge[1]}:{edge[2]} -> {edge[3]}:{edge[4]}"
                        ),
                    }
                )

        view = EditBatch(
            runtime.spec,
            runtime,
            Actor(actor_id="__invariant__", role="system", clearance_tags={"*"}),
        ).invariant_view()
        invariant_results: list[dict[str, Any]] = []
        for inv in runtime.spec.invariants.values():
            default_code = inv.code or f"INVARIANT_{inv.name.upper()}"
            err_code: str | None = None
            err_msg: str | None = None
            try:
                outcome = inv.handler(view)
            except InvariantError as exc:
                err_code = exc.code or default_code
                err_msg = str(exc)
            except AssertionError as exc:
                err_code = default_code
                err_msg = (
                    str(exc).strip()
                    or inv.description
                    or f"post-state invariant {inv.name!r} failed"
                )
            except Exception as exc:
                err_code = "INVARIANT_EXECUTION_ERROR"
                err_msg = str(exc)
            else:
                if outcome is False:
                    err_code = default_code
                    err_msg = (
                        inv.description
                        or f"post-state invariant {inv.name!r} returned False"
                    )

            if err_msg is None:
                invariant_results.append(
                    {
                        "name": inv.name,
                        "code": default_code,
                        "description": inv.description,
                        "passed": True,
                        "error": None,
                    }
                )
            else:
                assert err_code is not None
                invariant_results.append(
                    {
                        "name": inv.name,
                        "code": err_code,
                        "description": inv.description,
                        "passed": False,
                        "error": err_msg,
                    }
                )
                violations.append(
                    {
                        "kind": "invariant",
                        "code": err_code,
                        "subject": inv.name,
                        "message": err_msg,
                    }
                )

        return {
            "healthy": len(violations) == 0,
            "objects_checked": objects_checked,
            "links_checked": links_checked,
            "invariants_total": len(invariant_results),
            "invariants_passed": sum(1 for item in invariant_results if item["passed"]),
            "invariants": invariant_results,
            "violations": violations,
        }

# ---------------------------------------------------------------------------
# 3. IdempotencyAndOCCAspect —— 幂等短路与 MVCC 乐观锁
# ---------------------------------------------------------------------------
class IdempotencyAndOCCAspect:
    """幂等与并发控制切面。"""

    @staticmethod
    def check_idempotent_run(runtime: Any, run_id: str) -> dict[str, Any] | None:
        """同一 ``run_id`` 只执行一次；已存在则直接返回既有记录。"""
        return runtime.get_action_run(run_id)

    @staticmethod
    def current_version(conn: Any, object_type: str, object_id: str) -> int | None:
        """读一个对象当前的版本号；不存在返回 ``None``。"""
        row = conn.execute(
            """
            SELECT version FROM ontology_objects
            WHERE object_type = ? AND object_id = ?
            """,
            (object_type, object_id),
        ).fetchone()
        return None if row is None else int(row["version"])

    @staticmethod
    def verify_read_versions(
        conn: Any, read_versions: dict[str, int]
    ) -> None:
        """MVCC 乐观锁：提案读到的版本必须仍是当前版本。"""
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

    @staticmethod
    def propagate_aggregate_root_versions(
        runtime: Any, conn: Any, diff: ObjectDiff, now: str
    ) -> None:
        """聚合根版本号传播：子实体或归属连边变更时，同步递增所属聚合根的 version。"""
        if not diff.object_changes and not diff.link_changes:
            return
        direct_changed = {
            (change.object_type, change.object_id) for change in diff.object_changes
        }
        frontier: list[tuple[str, str]] = list(direct_changed)
        for lc in diff.link_changes:
            frontier.append((lc.source_type, lc.source_id))
            frontier.append((lc.target_type, lc.target_id))

        visited: set[tuple[str, str]] = set()
        touched_roots: set[tuple[str, str]] = set()
        while frontier:
            obj_type, obj_id = frontier.pop()
            if (obj_type, obj_id) in visited:
                continue
            visited.add((obj_type, obj_id))
            cls = runtime.spec.object_types.get(obj_type)
            if (
                cls is not None
                and cls.is_aggregate_root()
                and (obj_type, obj_id) not in direct_changed
            ):
                touched_roots.add((obj_type, obj_id))
            rows = conn.execute(
                "SELECT target_type, target_id FROM ontology_links "
                "WHERE source_type = ? AND source_id = ?",
                (obj_type, obj_id),
            ).fetchall()
            for row in rows:
                frontier.append((str(row["target_type"]), str(row["target_id"])))

        for root_type, root_id in sorted(touched_roots):
            conn.execute(
                "UPDATE ontology_objects SET version = version + 1, updated_at = ? "
                "WHERE object_type = ? AND object_id = ?",
                (now, root_type, root_id),
            )

    @classmethod
    def apply_diff(cls, runtime: Any, conn: Any, diff: ObjectDiff) -> None:
        """在写门禁事务内校验并发版本并原子应用变更集。"""
        now = runtime._utcnow()
        dumps = runtime._dumps

        def _hash(props: dict[str, Any]) -> str:
            return hashlib.sha256(dumps(props).encode("utf-8")).hexdigest()

        for change in diff.object_changes:
            if change.kind == "create":
                assert change.after is not None
                if cls.current_version(conn, change.object_type, change.object_id) is not None:
                    raise ConcurrencyConflictError(
                        f"cannot create {change.object_type}:{change.object_id}: "
                        "object already exists"
                    )
                version = change.after_version or 1
                conn.execute(
                    "INSERT INTO ontology_objects "
                    "(object_type, object_id, title, properties_json, version, "
                    "security_tags_json, content_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        change.object_type,
                        change.object_id,
                        change.after_title,
                        dumps(change.after),
                        version,
                        dumps(change.after_security_tags),
                        _hash(change.after),
                        now,
                    ),
                )
                runtime._sync_overlay(conn, change.object_type, change.object_id, change.after)

            elif change.kind == "update":
                assert change.after is not None
                current_version = cls.current_version(
                    conn, change.object_type, change.object_id
                )
                if current_version is None:
                    raise ConcurrencyConflictError(
                        f"cannot update {change.object_type}:{change.object_id}: object vanished"
                    )
                if change.before_version is not None and current_version != change.before_version:
                    raise ConcurrencyConflictError(
                        f"stale update on {change.object_type}:{change.object_id}: "
                        f"expected version {change.before_version}, found {current_version}"
                    )
                conn.execute(
                    "UPDATE ontology_objects SET title = ?, properties_json = ?, version = ?, "
                    "security_tags_json = ?, content_hash = ?, updated_at = ? "
                    "WHERE object_type = ? AND object_id = ?",
                    (
                        change.after_title,
                        dumps(change.after),
                        current_version + 1,
                        dumps(change.after_security_tags),
                        _hash(change.after),
                        now,
                        change.object_type,
                        change.object_id,
                    ),
                )
                runtime._sync_overlay(conn, change.object_type, change.object_id, change.after)

            elif change.kind == "delete":
                current_version = cls.current_version(
                    conn, change.object_type, change.object_id
                )
                if current_version is None:
                    continue
                if change.before_version is not None and current_version != change.before_version:
                    raise ConcurrencyConflictError(
                        f"stale delete on {change.object_type}:{change.object_id}: "
                        f"expected version {change.before_version}, found {current_version}"
                    )
                conn.execute(
                    "DELETE FROM ontology_objects WHERE object_type = ? AND object_id = ?",
                    (change.object_type, change.object_id),
                )
                runtime._delete_overlay(conn, change.object_type, change.object_id)
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

        cls.propagate_aggregate_root_versions(runtime, conn, diff, now)


# ---------------------------------------------------------------------------
# 审计账本写入原语（被 RejectionAudit 与 HomomorphicSettlement 共用）
# ---------------------------------------------------------------------------
def insert_action_run(
    runtime: Any,
    conn: Any,
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
    """往追加式业务审计账本写一条运行记录。"""
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
            runtime._dumps(params_json),
            status,
            runtime._dumps(read_versions),
            runtime._dumps(diff.to_dict()),
            approved_by,
            runtime._utcnow(),
            error_code,
            error_message,
        ),
    )


# ---------------------------------------------------------------------------
# 5. RejectionAuditAspect —— 被拒企图入账
# ---------------------------------------------------------------------------
class RejectionAuditAspect:
    """业务不变量失败时，对象零改动但必须留下一条 REJECTED 审计。"""

    @staticmethod
    def record_rejection(
        runtime: Any,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        error: InvariantError,
    ) -> None:
        with runtime._write_gate() as conn:
            insert_action_run(
                runtime,
                conn,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                status=AUDIT_STATUS_REJECTED,
                read_versions=read_versions,
                diff=ObjectDiff(),
                approved_by=None,
                error_code=error.code or "INVARIANT_ERROR",
                error_message=str(error),
            )


# ---------------------------------------------------------------------------
# 6. AutomationReflexAspect —— 提交后自动化反射
# ---------------------------------------------------------------------------
class AutomationReflexAspect:
    """提交后自动化条件反射：按 ``(规则, 对象, 版本)`` 去重。"""

    @staticmethod
    def evaluate_automations(
        runtime: Any,
        actor: Actor | None = None,
        *,
        object_type: type[ObjectType] | str | None = None,
        automation: str | None = None,
    ) -> list[dict[str, Any]]:
        """对所有被监视对象跑一遍已武装的自动化规则。"""
        actor = actor or Actor(actor_id="system", role="system")
        wanted_type = (
            runtime.spec.get_object_type(object_type).__name__
            if object_type is not None
            else None
        )
        fired: list[dict[str, Any]] = []

        for name, decl in sorted(runtime.spec.automations.items()):
            if automation is not None and name != automation:
                continue
            cls = runtime.spec.get_object_type(decl.watch_object_type)
            if wanted_type is not None and cls.__name__ != wanted_type:
                continue
            for row in runtime.load_all(cls.__name__):
                obj = cls(**row.data)
                if not decl.condition(obj):
                    continue
                key = (name, row.object_id, row.version)
                if key in runtime._automation_fires:
                    continue
                result = decl.handler(runtime, obj, actor)
                record = {
                    "automation": name,
                    "object_type": cls.__name__,
                    "object_id": row.object_id,
                    "version": row.version,
                    "actor": actor.actor_id,
                    "result": result,
                }
                runtime._automation_fires[key] = record
                fired.append(record)
        return fired

    @staticmethod
    def automation_history(runtime: Any) -> list[dict[str, Any]]:
        """按触发顺序返回全部自动化记录。"""
        return list(runtime._automation_fires.values())


# ---------------------------------------------------------------------------
# 7. IdleAwareCallbackAspect —— 忙闲感知的回调投递
# ---------------------------------------------------------------------------
class IdleAwareCallbackAspect:
    """忙闲感知防吞回调切面：目标忙时暂存，闲时按 FIFO 重放。

    回调是**事后通知**，绝不能反过来影响已提交的业务事务。因此本切面
    只有一条铁律：**永不打断、永不丢包、永不外抛异常**。

    * 目标空闲 → 立即投递，记入 ``delivered``；
    * 目标正忙 → 追加进 ``pending_queue``，等 :meth:`flush` 时重放；
    * 投递抛异常 → 捕获后记入 ``failed_deliveries``，返回 ``"FAILED"``。

    三个注入点把「忙闲判定」与「怎么发」都留给调用方，切面本身不认识
    任何具体协议或业务概念。
    """

    def __init__(
        self,
        is_idle_fn: Callable[[str], bool],
        send_fn: Callable[[str, dict[str, Any]], Any],
        wait_idle_fn: Callable[[str, float, float], bool] | None = None,
    ) -> None:
        self.is_idle_fn = is_idle_fn
        self.send_fn = send_fn
        self.wait_idle_fn = wait_idle_fn
        #: 目标正忙时暂存的待投递项，FIFO。
        self.pending_queue: list[dict[str, Any]] = []
        #: 已成功投递的账本。
        self.delivered: list[dict[str, Any]] = []
        #: 投递失败的账本（含 ``target_id`` / ``payload`` / ``error``）。
        self.failed_deliveries: list[dict[str, Any]] = []

    # -- internals ---------------------------------------------------------
    def _idle(self, target_id: str) -> bool:
        """判定目标是否空闲；判定本身出错时保守地当作「忙」。"""
        try:
            return bool(self.is_idle_fn(target_id))
        except Exception:
            return False

    def _send(self, target_id: str, payload: dict[str, Any]) -> str:
        """投递一条消息，异常一律隔离进 ``failed_deliveries``。"""
        try:
            self.send_fn(target_id, copy.deepcopy(payload))
        except Exception as exc:
            self.failed_deliveries.append(
                {
                    "target_id": target_id,
                    "payload": copy.deepcopy(payload),
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            return "FAILED"
        self.delivered.append(
            {"target_id": target_id, "payload": copy.deepcopy(payload)}
        )
        return "DELIVERED"

    # -- public API --------------------------------------------------------
    def deliver(
        self,
        target_id: str,
        payload: dict[str, Any],
        *,
        wait: bool = False,
        quiet_seconds: float = 1.0,
        timeout: float = 30.0,
    ) -> str:
        """把一条回调投递给 ``target_id``。

        返回 ``"DELIVERED"`` / ``"QUEUED"`` / ``"FAILED"`` / ``"SKIPPED"``。
        任何情况下都不向外抛异常。
        """
        if not target_id:
            return "SKIPPED"

        if wait and self.wait_idle_fn is not None:
            try:
                self.wait_idle_fn(target_id, quiet_seconds, timeout)
            except Exception:
                pass  # 等不到也照常走下面的忙闲判定，绝不因此丢包

        if not self._idle(target_id):
            self.pending_queue.append(
                {"target_id": target_id, "payload": copy.deepcopy(payload)}
            )
            return "QUEUED"

        # 目标已空闲：先把积压的旧消息按 FIFO 补投，再投当前这条。
        self.flush(target_id)
        return self._send(target_id, payload)

    def flush(self, target_id: str | None = None) -> int:
        """重放暂存队列中目标已空闲的条目，返回本轮成功投递条数。"""
        remaining: list[dict[str, Any]] = []
        delivered_count = 0
        for item in self.pending_queue:
            item_target = str(item.get("target_id") or "")
            if target_id is not None and item_target != target_id:
                remaining.append(item)
                continue
            if not self._idle(item_target):
                remaining.append(item)  # 仍然忙：原样保留，绝不丢
                continue
            status = self._send(item_target, item.get("payload") or {})
            if status == "DELIVERED":
                delivered_count += 1
        self.pending_queue[:] = remaining
        return delivered_count
