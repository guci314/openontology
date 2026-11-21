"""同态闭环结算切面（Homomorphic Settlement Aspect · 纯 M2，领域无关）。

将「反事实预测 → 无锁外部执行 → 独立传感器只读回读核验 → 同构收敛提交」收束为
``OntologyRuntime`` 唯一的默认提交/冲正管道：

1. **三元运行态隔离（``WorldMode``）**：
   * ``"standalone"``：纯本体世界（无外部耦合系统），受治理变更直接原子提交；
   * ``"coupled"``：强耦合外部物理/软件系统，凡触及外部源权威字段或声明受外部影响的
     动作必须绑定 :class:`SourceBridge`（未绑定则 fail-closed 抛 ``UNBOUND_SOURCE_BRIDGE``）；
   * ``"counterfactual"``：只允许 ``:memory:`` 沙盘且强制禁止绑定 ``SourceBridge``，
     确保 ``runtime.fork()`` 与 MCTS 多步推演对物理世界 100% 绝缘。
2. **三阶段无锁外部结算**：
   * Phase A（写门禁前预检）：先校乐观锁版本号（``verify_read_versions``）并加 per-``run_id``
     在途并发互斥锁，避免过期读取或重复提交误触外部执行器；
   * Phase B（完全脱离 SQLite 写锁）：调用 ``bridge.actuate(move)`` 后**无条件丢弃返回值**，
     再调用独立只读通道 ``bridge.observe(...)`` 采样真实状态；
   * Phase C（进 ``_write_gate()`` 核验提升）：回读与预测补丁一致则经收敛合并校验
     后原子写入 ``COMMITTED``；一旦不一致（或并发漂移冲突）立即保持内存对象零改动，
     落盘 ``REJECTED``（``DEVICE_STATE_MISMATCH``）并抛出 :class:`DeviceStateMismatchError`。
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

from .aspects import (
    AUDIT_STATUS_COMMITTED,
    AUDIT_STATUS_REJECTED,
    AUDIT_STATUS_REVERTED,
    IdempotencyAndOCCAspect,
    SecurityAndAuthorityAspect,
    insert_action_run,
)
from .sandbox import EditBatch, ObjectChange, ObjectDiff
from .spec import (
    ActionDef,
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    InvariantError,
    ObjectNotFoundError,
    OntologySpec,
    PermissionDeniedError,
)

__all__ = [
    "WorldMode",
    "VALID_WORLD_MODES",
    "DEVICE_STATE_MISMATCH",
    "UNBOUND_SOURCE_BRIDGE",
    "SOURCE_REVERT_FORBIDDEN",
    "DeviceStateMismatchError",
    "SourceBridge",
    "HomomorphicSettlementAspect",
    "call_observe",
    "diff_touches_source",
    "normalize_readback",
    "verify_readback_matches_diff",
    "apply_diff_with_convergence",
]

WorldMode = Literal["standalone", "coupled", "counterfactual"]
VALID_WORLD_MODES: tuple[WorldMode, ...] = ("standalone", "coupled", "counterfactual")

#: 独立只读回读与本体预测终态不一致时的标准错误码。
DEVICE_STATE_MISMATCH = "DEVICE_STATE_MISMATCH"
#: 强耦合模式缺少外部 SourceBridge 时的 fail-closed 错误码。
UNBOUND_SOURCE_BRIDGE = "UNBOUND_SOURCE_BRIDGE"
#: 强耦合模式默认禁止对已生效外部变更执行自动冲正时的错误码。
SOURCE_REVERT_FORBIDDEN = "SOURCE_REVERT_FORBIDDEN"


class _ConvergenceConflictError(Exception):
    """内部控制流异常：用于触发 SAVEPOINT 回滚并落盘 REJECTED 审计。"""


class DeviceStateMismatchError(InvariantError):
    """外部独立回读与本体预测补丁不一致时抛出的受治理异常。"""

    def __init__(
        self,
        message: str,
        *,
        mismatches: Sequence[str] = (),
        readback_objects: Mapping[str, Sequence[Any]] | None = None,
        readback_links: Mapping[str, Sequence[Any]] | None = None,
        run: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=DEVICE_STATE_MISMATCH)
        self.mismatches = tuple(mismatches)
        self.readback_objects = readback_objects
        self.readback_links = readback_links
        self.run = run


@dataclass(frozen=True)
class SourceBridge:
    """强耦合世界的外部执行与独立只读观测通道。"""

    actuate: Callable[[dict[str, Any]], Any]
    observe: Callable[..., Any]
    allow_source_revert: bool = False


def _content_hash(dumps: Callable[[Any], str], properties: dict[str, Any]) -> str:
    return hashlib.sha256(dumps(properties).encode("utf-8")).hexdigest()


def _inline_link_names(cls: type[Any]) -> set[str]:
    prop_map = getattr(cls, "__object_properties__", None) or {}
    return {decl.name for decl in prop_map.values()}


def _diff_has_source_side(spec: OntologySpec, diff: ObjectDiff) -> bool:
    for change in diff.object_changes:
        side = SecurityAndAuthorityAspect.object_change_side(spec, change)
        if side in ("source", "mixed"):
            return True
    for link_change in diff.link_changes:
        decl = spec.get_object_property(link_change.link_type)
        if not SecurityAndAuthorityAspect._property_is_owned(spec, decl):
            return True
    return False


def diff_touches_source(spec: OntologySpec, action: ActionDef, diff: ObjectDiff) -> bool:
    """判断一次动作执行及其差集是否会触及外部权威状态。"""
    return bool(action.affects_source) or _diff_has_source_side(spec, diff)


def _unpack_observation(
    raw: Any,
    *,
    spec: OntologySpec | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[tuple[str, str]]] | None]:
    if isinstance(raw, tuple) and len(raw) == 2:
        objs, links = raw
        if not isinstance(objs, Mapping):
            raise GovernanceViolationError(
                "observe() tuple must contain (objects_mapping, links_mapping_or_none)",
                code=DEVICE_STATE_MISMATCH,
            )
        if links is not None and not isinstance(links, Mapping):
            raise GovernanceViolationError(
                "observe() second tuple element must be a links mapping or None",
                code=DEVICE_STATE_MISMATCH,
            )
        return copy.deepcopy(dict(objs)), (
            copy.deepcopy(dict(links)) if links is not None else None
        )
    if isinstance(raw, Mapping):
        keys = set(raw.keys())
        registered = set(spec.object_types.keys()) if spec is not None else set()
        if (
            keys
            and keys <= {"objects", "links"}
            and "objects" in keys
            and "objects" not in registered
            and isinstance(raw["objects"], Mapping)
        ):
            links = raw.get("links")
            return copy.deepcopy(dict(raw["objects"])), (
                copy.deepcopy(dict(links)) if isinstance(links, Mapping) else None
            )
        return copy.deepcopy(dict(raw)), None
    raise GovernanceViolationError(
        "SourceBridge.observe() must return objects mapping or (objects, links) tuple",
        code=DEVICE_STATE_MISMATCH,
    )


def call_observe(
    bridge: SourceBridge,
    move: dict[str, Any] | None = None,
    *,
    spec: OntologySpec | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[tuple[str, str]]] | None]:
    """统一兼容 0 参 ``observe()`` 与接收移动信封的 ``observe(move)``。"""
    sig = inspect.signature(bridge.observe)
    accepts_arg = any(
        p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for p in sig.parameters.values()
    )
    if accepts_arg and move is not None:
        raw = bridge.observe(copy.deepcopy(move))
    else:
        raw = bridge.observe()
    return _unpack_observation(raw, spec=spec)


def normalize_readback(
    runtime: Any,
    readback_objects: Mapping[str, Sequence[Any]],
    readback_links: Mapping[str, Sequence[Any]] | None,
) -> tuple[
    dict[str, dict[str, dict[str, Any]]],
    dict[str, set[tuple[str, str, str, str]]],
    dict[str, set[tuple[str, str, str, str]]],
]:
    """复用摄入驱动的类型与主权规范化器解析独立回读数据。"""
    driver = runtime.ingestion
    normalized_objs = driver._normalize_objects(readback_objects)
    normalized_links = driver._normalize_links(readback_links)

    readback_by_type: dict[str, dict[str, dict[str, Any]]] = {
        type_name: {} for type_name in readback_objects
    }
    inline_edges: dict[str, set[tuple[str, str, str, str]]] = {}

    for type_name, cls, obj, props in normalized_objs:
        object_id = obj.primary_key_value()
        readback_by_type.setdefault(type_name, {})[object_id] = props
        prop_map = getattr(cls, "__object_properties__", None) or {}
        for field_name, decl in prop_map.items():
            edge_bucket = inline_edges.setdefault(decl.name, set())
            for target_id in EditBatch._extract_field_targets(props.get(field_name)):
                edge_bucket.add((type_name, object_id, decl.range, target_id))

    standalone_edges: dict[str, set[tuple[str, str, str, str]]] = {
        runtime.spec.get_object_property(prop_name).name: set()
        for prop_name in (readback_links or {})
    }
    for link_type, source_type, source_id, target_type, target_id in normalized_links:
        standalone_edges.setdefault(link_type, set()).add(
            (source_type, source_id, target_type, target_id)
        )
    return readback_by_type, inline_edges, standalone_edges


def verify_readback_matches_diff(
    spec: OntologySpec,
    diff: ObjectDiff,
    readback_by_type: dict[str, dict[str, dict[str, Any]]],
    inline_edges: dict[str, set[tuple[str, str, str, str]]],
    standalone_edges: dict[str, set[tuple[str, str, str, str]]],
) -> list[str]:
    """逐项核对外部只读回读与动作预测 diff 的非自有终态。"""
    mismatches: list[str] = []

    for change in diff.object_changes:
        cls = spec.get_object_type(change.object_type)
        if cls.is_fully_owned():
            continue
        owned_fields = set(cls.owned_field_defaults())
        pk = cls.primary_key_name()

        if change.kind == "create":
            expected = change.after or {}
            type_bucket = readback_by_type.get(change.object_type)
            if type_bucket is None or change.object_id not in type_bucket:
                mismatches.append(
                    f"{change.object_type}:{change.object_id} created object missing in readback"
                )
                continue
            actual = type_bucket[change.object_id]
            for field_name in sorted(set(expected) - owned_fields):
                if actual.get(field_name) != expected.get(field_name):
                    mismatches.append(
                        f"{change.object_type}:{change.object_id}.{field_name} "
                        f"expected {expected.get(field_name)!r}, got {actual.get(field_name)!r}"
                    )
        elif change.kind == "delete":
            type_bucket = readback_by_type.get(change.object_type)
            if type_bucket is None:
                mismatches.append(
                    f"readback missing object type {change.object_type!r} "
                    f"to verify deletion of {change.object_id}"
                )
            elif change.object_id in type_bucket:
                mismatches.append(
                    f"{change.object_type}:{change.object_id} expected deleted "
                    "but still present in readback"
                )
        elif change.kind == "update":
            before = change.before or {}
            after = change.after or {}
            changed_src_fields = sorted(
                field_name
                for field_name in (set(before) | set(after)) - owned_fields - {pk}
                if before.get(field_name) != after.get(field_name)
            )
            if not changed_src_fields:
                continue
            type_bucket = readback_by_type.get(change.object_type)
            if type_bucket is None or change.object_id not in type_bucket:
                mismatches.append(
                    f"{change.object_type}:{change.object_id} missing in readback for update check"
                )
                continue
            actual = type_bucket[change.object_id]
            for field_name in changed_src_fields:
                if actual.get(field_name) != after.get(field_name):
                    mismatches.append(
                        f"{change.object_type}:{change.object_id}.{field_name} "
                        f"expected {after.get(field_name)!r}, got {actual.get(field_name)!r}"
                    )

    for lc in diff.link_changes:
        decl = spec.get_object_property(lc.link_type)
        if SecurityAndAuthorityAspect._property_is_owned(spec, decl):
            continue
        domain_cls = spec.object_types.get(decl.domain)
        is_inline = domain_cls is not None and decl.name in _inline_link_names(
            domain_cls
        )
        if is_inline and decl.name not in standalone_edges:
            if decl.domain not in readback_by_type:
                mismatches.append(
                    f"readback missing domain {decl.domain!r} for link {decl.name!r}"
                )
                continue
            edges = inline_edges.get(decl.name, set())
        else:
            if decl.name not in standalone_edges:
                mismatches.append(
                    f"readback_links missing object property {decl.name!r}"
                )
                continue
            edges = standalone_edges[decl.name]

        edge_tuple = (lc.source_type, lc.source_id, lc.target_type, lc.target_id)
        if lc.kind == "create" and edge_tuple not in edges:
            mismatches.append(
                f"link {decl.name}({lc.source_id}->{lc.target_id}) "
                "expected present, missing in readback"
            )
        elif lc.kind == "delete" and edge_tuple in edges:
            mismatches.append(
                f"link {decl.name}({lc.source_id}->{lc.target_id}) "
                "expected deleted, still present in readback"
            )

    return mismatches


def apply_diff_with_convergence(
    runtime: Any,
    conn: Any,
    diff: ObjectDiff,
    read_versions: dict[str, int],
) -> None:
    """在写门禁内应用变更集，若期间快照抢跑收敛至一致终态则兼容合并。"""
    now = runtime._utcnow()
    mutated_keys = {
        (change.object_type, change.object_id) for change in diff.object_changes
    }
    for key in read_versions:
        obj_type, _, obj_id = key.partition(":")
        if (obj_type, obj_id) in mutated_keys:
            continue
        row = conn.execute(
            "SELECT 1 FROM ontology_objects "
            "WHERE object_type = ? AND object_id = ?",
            (obj_type, obj_id),
        ).fetchone()
        if row is None:
            raise _ConvergenceConflictError(
                f"referenced object {key} vanished concurrently"
            )

    mutated = False
    for change in diff.object_changes:
        cls = runtime.spec.get_object_type(change.object_type)
        row = conn.execute(
            "SELECT version, properties_json FROM ontology_objects "
            "WHERE object_type = ? AND object_id = ?",
            (change.object_type, change.object_id),
        ).fetchone()
        if change.kind == "create":
            if _apply_create_converged(runtime, conn, cls, change, row, now):
                mutated = True
        elif change.kind == "update":
            if _apply_update_converged(runtime, conn, cls, change, row, now):
                mutated = True
        elif change.kind == "delete":
            if _apply_delete_converged(runtime, conn, cls, change, row):
                mutated = True
        else:  # pragma: no cover
            raise GovernanceViolationError(
                f"unknown object change kind {change.kind!r}"
            )

    for lc in diff.link_changes:
        if lc.kind == "create":
            conn.execute(
                "INSERT OR IGNORE INTO ontology_links "
                "(link_type, source_type, source_id, target_type, target_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    lc.link_type,
                    lc.source_type,
                    lc.source_id,
                    lc.target_type,
                    lc.target_id,
                ),
            )
        elif lc.kind == "delete":
            conn.execute(
                "DELETE FROM ontology_links "
                "WHERE link_type = ? AND source_type = ? AND source_id = ? "
                "AND target_type = ? AND target_id = ?",
                (
                    lc.link_type,
                    lc.source_type,
                    lc.source_id,
                    lc.target_type,
                    lc.target_id,
                ),
            )
        else:  # pragma: no cover
            raise GovernanceViolationError(f"unknown link change kind {lc.kind!r}")

    if mutated or diff.link_changes:
        IdempotencyAndOCCAspect.propagate_aggregate_root_versions(
            runtime, conn, diff, now
        )


def _apply_create_converged(
    runtime: Any,
    conn: Any,
    cls: type[Any],
    change: ObjectChange,
    row: Any,
    now: str,
) -> bool:
    assert change.after is not None
    dumps = runtime._dumps
    if row is None:
        version = change.after_version or 1
        conn.execute(
            "INSERT INTO ontology_objects "
            "(object_type, object_id, title, properties_json, version, "
            "security_tags_json, content_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                change.object_type,
                change.object_id,
                change.after_title,
                dumps(change.after),
                version,
                dumps(change.after_security_tags),
                _content_hash(dumps, change.after),
                now,
            ),
        )
        runtime._sync_overlay(
            conn, change.object_type, change.object_id, change.after
        )
        return True

    if cls.is_fully_owned():
        raise _ConvergenceConflictError(
            f"owned object {change.object_type}:{change.object_id} already exists"
        )

    existing_props = json.loads(row["properties_json"])
    owned_defaults = cls.owned_field_defaults()
    owned_fields = set(owned_defaults)
    for field_name in set(change.after) - owned_fields:
        if existing_props.get(field_name) != change.after.get(field_name):
            raise _ConvergenceConflictError(
                f"concurrent state conflict on {change.object_type}:{change.object_id}.{field_name}: "
                f"memory has {existing_props.get(field_name)!r}, expected {change.after.get(field_name)!r}"
            )

    merged = {
        **existing_props,
        **{
            k: change.after[k]
            for k in owned_fields
            if k in change.after and change.after[k] != owned_defaults.get(k)
        },
    }
    merged_obj = cls(**merged)
    props_out = merged_obj.to_properties()
    if props_out != existing_props:
        conn.execute(
            "UPDATE ontology_objects SET title = ?, properties_json = ?, version = ?, "
            "security_tags_json = ?, content_hash = ?, updated_at = ? "
            "WHERE object_type = ? AND object_id = ?",
            (
                merged_obj.title_value(),
                dumps(props_out),
                int(row["version"]) + 1,
                dumps(merged_obj.security_tags()),
                _content_hash(dumps, props_out),
                now,
                change.object_type,
                change.object_id,
            ),
        )
        runtime._sync_overlay(
            conn, change.object_type, change.object_id, props_out
        )
        return True
    runtime._sync_overlay(
        conn, change.object_type, change.object_id, existing_props
    )
    return False


def _apply_update_converged(
    runtime: Any,
    conn: Any,
    cls: type[Any],
    change: ObjectChange,
    row: Any,
    now: str,
) -> bool:
    assert change.after is not None
    dumps = runtime._dumps
    if row is None:
        raise _ConvergenceConflictError(
            f"cannot update {change.object_type}:{change.object_id}: object vanished concurrently"
        )

    cur_ver = int(row["version"])
    existing_props = json.loads(row["properties_json"])

    if change.before_version is None or cur_ver == change.before_version:
        conn.execute(
            "UPDATE ontology_objects SET title = ?, properties_json = ?, version = ?, "
            "security_tags_json = ?, content_hash = ?, updated_at = ? "
            "WHERE object_type = ? AND object_id = ?",
            (
                change.after_title,
                dumps(change.after),
                cur_ver + 1,
                dumps(change.after_security_tags),
                _content_hash(dumps, change.after),
                now,
                change.object_type,
                change.object_id,
            ),
        )
        runtime._sync_overlay(
            conn, change.object_type, change.object_id, change.after
        )
        return True

    owned_fields = set(cls.owned_field_defaults())
    pk = cls.primary_key_name()
    before = change.before or {}
    after = change.after

    changed_src = [
        f
        for f in (set(before) | set(after)) - owned_fields - {pk}
        if before.get(f) != after.get(f)
    ]
    changed_owned = {
        f: after[f] for f in owned_fields if before.get(f) != after.get(f)
    }

    for field_name in changed_src:
        mem_val = existing_props.get(field_name)
        exp_val = after.get(field_name)
        if mem_val != exp_val and mem_val != before.get(field_name):
            raise _ConvergenceConflictError(
                f"concurrent conflict on {change.object_type}:{change.object_id}.{field_name}: "
                f"memory drifted to {mem_val!r}, expected {exp_val!r}"
            )

    for field_name, exp_val in changed_owned.items():
        mem_val = existing_props.get(field_name)
        if mem_val not in (before.get(field_name), exp_val):
            raise _ConvergenceConflictError(
                f"concurrent owned-field conflict on "
                f"{change.object_type}:{change.object_id}.{field_name}: "
                f"memory drifted to {mem_val!r}, expected {exp_val!r}"
            )

    merged = {
        **existing_props,
        **{f: after[f] for f in changed_src},
        **changed_owned,
    }
    merged_obj = cls(**merged)
    props_out = merged_obj.to_properties()
    if props_out != existing_props:
        conn.execute(
            "UPDATE ontology_objects SET title = ?, properties_json = ?, version = ?, "
            "security_tags_json = ?, content_hash = ?, updated_at = ? "
            "WHERE object_type = ? AND object_id = ?",
            (
                merged_obj.title_value(),
                dumps(props_out),
                cur_ver + 1,
                dumps(merged_obj.security_tags()),
                _content_hash(dumps, props_out),
                now,
                change.object_type,
                change.object_id,
            ),
        )
        runtime._sync_overlay(
            conn, change.object_type, change.object_id, props_out
        )
        return True
    runtime._sync_overlay(
        conn, change.object_type, change.object_id, existing_props
    )
    return False


def _apply_delete_converged(
    runtime: Any,
    conn: Any,
    cls: type[Any],
    change: ObjectChange,
    row: Any,
) -> bool:
    if row is None:
        runtime._delete_overlay(conn, change.object_type, change.object_id)
        return False

    cur_ver = int(row["version"])
    if change.before_version is not None and cur_ver != change.before_version:
        existing_props = json.loads(row["properties_json"])
        owned_fields = set(cls.owned_field_defaults())
        before = change.before or {}
        for field_name in set(before) - owned_fields:
            if existing_props.get(field_name) != before.get(field_name):
                raise _ConvergenceConflictError(
                    f"concurrent conflict before delete on "
                    f"{change.object_type}:{change.object_id}.{field_name}: "
                    f"memory has {existing_props.get(field_name)!r}, expected {before.get(field_name)!r}"
                )

    conn.execute(
        "DELETE FROM ontology_objects WHERE object_type = ? AND object_id = ?",
        (change.object_type, change.object_id),
    )
    runtime._delete_overlay(conn, change.object_type, change.object_id)
    return True


def _build_move_envelope(
    *,
    run_id: str,
    action_name: str,
    actor: Actor,
    params_json: dict[str, Any],
    read_versions: dict[str, int],
    diff: ObjectDiff,
    approved_by: str | None = None,
    is_revert: bool = False,
    reverts_run_id: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "run_id": run_id,
        "action_type": action_name,
        "actor": {"actor_id": actor.actor_id, "role": actor.role},
        "actor_id": actor.actor_id,
        "actor_role": actor.role,
        "params": copy.deepcopy(params_json),
        "read_versions": dict(read_versions),
        "object_diff": diff.to_dict(),
        "affects_source": True,
        "approved_by": approved_by,
        "is_revert": is_revert,
    }
    if reverts_run_id is not None:
        envelope["reverts_run_id"] = reverts_run_id
    return envelope


@contextmanager
def _inflight_guard(runtime: Any, run_id: str) -> Iterator[None]:
    with runtime._inflight_lock:
        if run_id in runtime._inflight_runs:
            raise ConcurrencyConflictError(f"action run {run_id!r} is already in flight")
        runtime._inflight_runs.add(run_id)
    try:
        yield
    finally:
        with runtime._inflight_lock:
            runtime._inflight_runs.discard(run_id)


def _notify_committed(runtime: Any, actor: Actor, run_id: str, *, auto_trigger: bool = False) -> dict[str, Any]:
    if auto_trigger:
        runtime.evaluate_automations(actor)
    committed = runtime.get_action_run(run_id)
    notify = getattr(runtime, "_notify_post_commit", None)
    if committed is not None and callable(notify):
        notify(actor, committed)
    return committed  # type: ignore[return-value]


def _notify_rejected(runtime: Any, actor: Actor, run_id: str) -> dict[str, Any] | None:
    rejected = runtime.get_action_run(run_id)
    notify = getattr(runtime, "_notify_run_event", None)
    if rejected is not None and callable(notify):
        notify(actor, rejected)
    return rejected


# ---------------------------------------------------------------------------
# HomomorphicSettlementAspect —— 统一闭环结算切面
# ---------------------------------------------------------------------------
class HomomorphicSettlementAspect:
    """统一执行「预测 -> 外部驱动 -> 独立回读核验 -> 原子提交/否决」三阶段闭环。"""

    @staticmethod
    def _require_coupled_bridge(
        runtime: Any, action_name: str, *, for_revert: bool = False
    ) -> SourceBridge:
        bridge = runtime.source_bridge
        if bridge is None:
            raise GovernanceViolationError(
                f"action {action_name!r} touches external source in coupled world "
                "without a bound SourceBridge",
                code=UNBOUND_SOURCE_BRIDGE,
            )
        if for_revert and not bridge.allow_source_revert:
            raise GovernanceViolationError(
                f"action {action_name!r} touches external source and coupled SourceBridge "
                "forbids automatic source revert",
                code=SOURCE_REVERT_FORBIDDEN,
            )
        return bridge

    @staticmethod
    def _phase_b_actuate_and_observe(
        runtime: Any,
        bridge: SourceBridge,
        move_envelope: dict[str, Any],
        diff: ObjectDiff,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, list[tuple[str, str]]] | None,
        list[str],
    ]:
        # 法则 2：物理与内存分离——actuate 返回值一律丢弃，只认独立 observe 采样。
        _ = bridge.actuate(copy.deepcopy(move_envelope))
        readback_objects, readback_links = call_observe(
            bridge, move_envelope, spec=runtime.spec
        )
        readback_by_type, inline_edges, standalone_edges = normalize_readback(
            runtime, readback_objects, readback_links
        )
        mismatches = verify_readback_matches_diff(
            runtime.spec, diff, readback_by_type, inline_edges, standalone_edges
        )
        return readback_objects, readback_links, mismatches

    @classmethod
    def _phase_b_with_rejection_audit(
        cls,
        runtime: Any,
        bridge: SourceBridge,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        move: dict[str, Any],
        diff: ObjectDiff,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[str, list[tuple[str, str]]] | None,
        list[str],
    ]:
        try:
            return cls._phase_b_actuate_and_observe(runtime, bridge, move, diff)
        except InvariantError as exc:
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
                    error_code=exc.code or DEVICE_STATE_MISMATCH,
                    error_message=str(exc),
                )
            _notify_rejected(runtime, actor, run_id)
            raise

    @classmethod
    def _reject_on_mismatch(
        cls,
        runtime: Any,
        *,
        run_id: str,
        action: ActionDef,
        commit_actor: Actor,
        audit_actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        mismatches: list[str],
        readback_objects: dict[str, list[dict[str, Any]]],
        readback_links: dict[str, list[tuple[str, str]]] | None,
        approved_by: str | None = None,
    ) -> None:
        msg = "; ".join(mismatches)
        with runtime._write_gate() as conn:
            insert_action_run(
                runtime,
                conn,
                run_id=run_id,
                action=action,
                actor=audit_actor,
                params_json=params_json,
                status=AUDIT_STATUS_REJECTED,
                read_versions=read_versions,
                diff=ObjectDiff(),
                approved_by=approved_by,
                error_code=DEVICE_STATE_MISMATCH,
                error_message=msg,
            )
        rejected_run = _notify_rejected(runtime, commit_actor, run_id)
        raise DeviceStateMismatchError(
            msg,
            mismatches=mismatches,
            readback_objects=readback_objects,
            readback_links=readback_links,
            run=rejected_run,
        )

    @classmethod
    def _commit_or_reject_phase_c(
        cls,
        runtime: Any,
        *,
        run_id: str,
        action: ActionDef,
        commit_actor: Actor,
        audit_actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        diff: ObjectDiff,
        readback_objects: dict[str, list[dict[str, Any]]],
        readback_links: dict[str, list[tuple[str, str]]] | None,
        approved_by: str | None = None,
        reverted_target_run_id: str | None = None,
    ) -> None:
        savepoint = "homomorphic_settle_phase_c"
        conv_err: str | None = None
        inv_err: InvariantError | None = None

        def _write_rejection(conn: Any, code: str, message: str) -> None:
            insert_action_run(
                runtime,
                conn,
                run_id=run_id,
                action=action,
                actor=audit_actor,
                params_json=params_json,
                status=AUDIT_STATUS_REJECTED,
                read_versions=read_versions,
                diff=ObjectDiff(),
                approved_by=approved_by,
                error_code=code,
                error_message=message,
            )

        with runtime._write_gate() as conn:
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                apply_diff_with_convergence(runtime, conn, diff, read_versions)
                runtime.check_invariants()
                check_trans = getattr(runtime, "check_transition_invariants", None)
                if reverted_target_run_id is None and callable(check_trans):
                    check_trans(diff)
                if reverted_target_run_id is not None:
                    conn.execute(
                        "UPDATE ontology_action_runs SET status = ?, reverted_by_run_id = ? "
                        "WHERE run_id = ? AND status = ?",
                        (
                            AUDIT_STATUS_REVERTED,
                            run_id,
                            reverted_target_run_id,
                            AUDIT_STATUS_COMMITTED,
                        ),
                    )
                insert_action_run(
                    runtime,
                    conn,
                    run_id=run_id,
                    action=action,
                    actor=audit_actor,
                    params_json=params_json,
                    status=AUDIT_STATUS_COMMITTED,
                    read_versions=read_versions,
                    diff=diff,
                    approved_by=approved_by,
                )
            except _ConvergenceConflictError as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                conv_err = str(exc)
                _write_rejection(conn, DEVICE_STATE_MISMATCH, conv_err)
            except InvariantError as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                inv_err = exc
                _write_rejection(conn, exc.code or DEVICE_STATE_MISMATCH, str(exc))
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")

        if conv_err is not None:
            rejected_run = _notify_rejected(runtime, commit_actor, run_id)
            raise DeviceStateMismatchError(
                conv_err,
                mismatches=(conv_err,),
                readback_objects=readback_objects,
                readback_links=readback_links,
                run=rejected_run,
            )
        if inv_err is not None:
            _notify_rejected(runtime, commit_actor, run_id)
            raise inv_err

    @classmethod
    def settle_submission(
        cls,
        runtime: Any,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        diff: ObjectDiff,
        auto_trigger: bool = False,
    ) -> dict[str, Any]:
        """同步动作提交的统一同态结算入口。"""
        if (
            runtime.world_mode != "coupled"
            or not diff_touches_source(runtime.spec, action, diff)
        ):
            with runtime._write_gate() as conn:
                IdempotencyAndOCCAspect.verify_read_versions(conn, read_versions)
                IdempotencyAndOCCAspect.apply_diff(runtime, conn, diff)
                runtime.check_invariants()
                runtime.check_transition_invariants(diff)
                insert_action_run(
                    runtime,
                    conn,
                    run_id=run_id,
                    action=action,
                    actor=actor,
                    params_json=params_json,
                    status=AUDIT_STATUS_COMMITTED,
                    read_versions=read_versions,
                    diff=diff,
                    approved_by=None,
                )
            return _notify_committed(runtime, actor, run_id, auto_trigger=auto_trigger)

        bridge = cls._require_coupled_bridge(runtime, action.name)
        with _inflight_guard(runtime, run_id):
            existing = IdempotencyAndOCCAspect.check_idempotent_run(runtime, run_id)
            if existing is not None:
                return existing

            # Phase A：触碰外部前先行验证读取版本（禁止陈旧读污染外部系统）
            IdempotencyAndOCCAspect.verify_read_versions(runtime._conn, read_versions)

            # Phase B：完全脱离 SQLite 写门禁执行外部驱动并独立采样回读
            move = _build_move_envelope(
                run_id=run_id,
                action_name=action.name,
                actor=actor,
                params_json=params_json,
                read_versions=read_versions,
                diff=diff,
            )
            readback_objects, readback_links, mismatches = cls._phase_b_with_rejection_audit(
                runtime,
                bridge,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                read_versions=read_versions,
                move=move,
                diff=diff,
            )

            # Phase C：回读核验与原子提升（不一致则保留物理观测，内存事实零改动）
            if mismatches:
                cls._reject_on_mismatch(
                    runtime,
                    run_id=run_id,
                    action=action,
                    commit_actor=actor,
                    audit_actor=actor,
                    params_json=params_json,
                    read_versions=read_versions,
                    mismatches=mismatches,
                    readback_objects=readback_objects,
                    readback_links=readback_links,
                )

            cls._commit_or_reject_phase_c(
                runtime,
                run_id=run_id,
                action=action,
                commit_actor=actor,
                audit_actor=actor,
                params_json=params_json,
                read_versions=read_versions,
                diff=diff,
                readback_objects=readback_objects,
                readback_links=readback_links,
            )
        return _notify_committed(runtime, actor, run_id, auto_trigger=auto_trigger)

    @classmethod
    def settle_revert(
        cls,
        runtime: Any,
        run_id: str,
        revert_run_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """时光机冲正回滚的同态结算入口。"""
        existing = IdempotencyAndOCCAspect.check_idempotent_run(runtime, revert_run_id)
        if existing is not None:
            return existing

        run = runtime.get_action_run(run_id)
        if run is None:
            raise ObjectNotFoundError(f"action run {run_id!r} not found")

        action = runtime.spec.get_action(run["action_type"])
        revert_roles = (
            set(action.revert_roles)
            if action.revert_roles
            else set(action.allowed_roles)
        )
        if actor.role not in revert_roles:
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} role {actor.role!r} may not revert "
                f"{action.name!r} (required roles: {sorted(revert_roles)})"
            )

        if run["status"] == AUDIT_STATUS_REVERTED:
            raise GovernanceViolationError(f"action run {run_id!r} was already reverted")
        if run["status"] != AUDIT_STATUS_COMMITTED:
            raise GovernanceViolationError(
                f"action run {run_id!r} is {run['status']}; only COMMITTED runs can revert"
            )

        original_diff = ObjectDiff.from_dict(run["object_diff"])
        inverted = original_diff.inverted()

        revert_read_versions: dict[str, int] = {}
        for change in inverted.object_changes:
            row = runtime._conn.execute(
                "SELECT version FROM ontology_objects "
                "WHERE object_type = ? AND object_id = ?",
                (change.object_type, change.object_id),
            ).fetchone()
            if row is not None:
                revert_read_versions[f"{change.object_type}:{change.object_id}"] = int(
                    row["version"]
                )

        if (
            runtime.world_mode != "coupled"
            or not diff_touches_source(runtime.spec, action, inverted)
        ):
            with runtime._write_gate() as conn:
                IdempotencyAndOCCAspect.apply_diff(runtime, conn, inverted)
                runtime.check_invariants()
                conn.execute(
                    "UPDATE ontology_action_runs SET status = ?, reverted_by_run_id = ? "
                    "WHERE run_id = ? AND status = ?",
                    (
                        AUDIT_STATUS_REVERTED,
                        revert_run_id,
                        run_id,
                        AUDIT_STATUS_COMMITTED,
                    ),
                )
                insert_action_run(
                    runtime,
                    conn,
                    run_id=revert_run_id,
                    action=action,
                    actor=actor,
                    params_json=run["params"],
                    status=AUDIT_STATUS_COMMITTED,
                    read_versions=revert_read_versions,
                    diff=inverted,
                    approved_by=None,
                )
            return _notify_committed(runtime, actor, revert_run_id)

        bridge = cls._require_coupled_bridge(runtime, action.name, for_revert=True)
        with _inflight_guard(runtime, revert_run_id):
            existing_rev = IdempotencyAndOCCAspect.check_idempotent_run(
                runtime, revert_run_id
            )
            if existing_rev is not None:
                return existing_rev

            IdempotencyAndOCCAspect.verify_read_versions(
                runtime._conn, revert_read_versions
            )

            move = _build_move_envelope(
                run_id=revert_run_id,
                action_name=action.name,
                actor=actor,
                params_json=dict(run["params"]),
                read_versions=revert_read_versions,
                diff=inverted,
                is_revert=True,
                reverts_run_id=run_id,
            )
            readback_objects, readback_links, mismatches = cls._phase_b_with_rejection_audit(
                runtime,
                bridge,
                run_id=revert_run_id,
                action=action,
                actor=actor,
                params_json=dict(run["params"]),
                read_versions=revert_read_versions,
                move=move,
                diff=inverted,
            )
            if mismatches:
                cls._reject_on_mismatch(
                    runtime,
                    run_id=revert_run_id,
                    action=action,
                    commit_actor=actor,
                    audit_actor=actor,
                    params_json=run["params"],
                    read_versions=revert_read_versions,
                    mismatches=mismatches,
                    readback_objects=readback_objects,
                    readback_links=readback_links,
                )

            cls._commit_or_reject_phase_c(
                runtime,
                run_id=revert_run_id,
                action=action,
                commit_actor=actor,
                audit_actor=actor,
                params_json=run["params"],
                read_versions=revert_read_versions,
                diff=inverted,
                readback_objects=readback_objects,
                readback_links=readback_links,
                reverted_target_run_id=run_id,
            )
        return _notify_committed(runtime, actor, revert_run_id)
