"""L4.5 · 持久化隐空间（Latent Space）与独立回读结算生命周期。

将高频只读内存推演（``runtime.fork()``）与跨重启可恢复的「持久化决策隐空间」分离：

1. **同构双库（零新表）**：``LatentSpace(memory_runtime, db_path)`` 底层复用完全同构的
   6 表 SQLite 内核。``db_path`` 尚不存在时通过 ``Connection.backup()`` 从记忆世界初始化；
   若已是合法 6 表库则原样无损重开，决不覆盖其中已决定但尚未结算的棋步。
2. **隐空间决定（记忆零写入）**：通过 ``stage_move`` / ``approve_move`` / ``reject_move``
   在隐空间内走完 RBAC、Schema、RLS、不变量与审批流程，期间记忆世界零污染；
   ``pending_moves`` 跨重启暴露待外部执行的 ``COMMITTED`` 棋步（含 ``run_id``）。
3. **独立回读核对与提升（``settle_move``）**：
   * ``affects_source=False`` 的自有动作直接校验版本与不变量后提升进记忆世界；
   * ``affects_source=True`` 的外部动作必须传入独立读通道采集的 ``readback_objects``
     （及可选 ``readback_links``），经快照规范化校验后逐项核对预期终态：
     - 一致 → 提升进记忆并写入 ``COMMITTED`` 账本记录（支持后台快照抢跑收敛不误报）；
     - 不一致 → 记忆事实/连边/overlay 零改动，记入 ``status='REJECTED'``
       （``error_code='DEVICE_STATE_MISMATCH'``）并自动 ``rebase()`` 对齐隐空间。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .aspects import (
    AUDIT_STATUS_COMMITTED,
    AUDIT_STATUS_REJECTED,
    AUDIT_STATUS_REVERTED,
    IdempotencyAndOCCAspect,
    SecurityAndAuthorityAspect,
    insert_action_run,
)
from .runtime import OntologyRuntime, _content_hash, _dumps
from .sandbox import EditBatch, ObjectChange, ObjectDiff
from .spec import (
    ActionDef,
    Actor,
    ConcurrencyConflictError,
    GovernanceViolationError,
    InvariantError,
    ObjectNotFoundError,
    OntologySpec,
)
from .workspaces import check_kernel_database

__all__ = [
    "LatentSpace",
    "DEVICE_STATE_MISMATCH",
]

#: 独立回读核对与隐空间预期终态不一致时的标准审计错误码。
DEVICE_STATE_MISMATCH = "DEVICE_STATE_MISMATCH"

_SETTLED_STATUSES = (
    AUDIT_STATUS_COMMITTED,
    AUDIT_STATUS_REJECTED,
    AUDIT_STATUS_REVERTED,
)


class _ConvergenceConflictError(Exception):
    """内部控制流异常：用于触发 SAVEPOINT 回滚并落盘 REJECTED 审计。"""


def _diff_touches_source(spec: OntologySpec, diff: ObjectDiff) -> bool:
    """判断一份 diff 是否实际触及外部源端对象、源端字段或非自有关系边。"""
    for change in diff.object_changes:
        side = SecurityAndAuthorityAspect.object_change_side(spec, change)
        if side in ("source", "mixed"):
            return True
    for link_change in diff.link_changes:
        decl = spec.get_object_property(link_change.link_type)
        if not SecurityAndAuthorityAspect._property_is_owned(spec, decl):
            return True
    return False


def _inline_link_names(cls: type[Any]) -> set[str]:
    prop_map = getattr(cls, "__object_properties__", None) or {}
    return {decl.name for decl in prop_map.values()}


class LatentSpace:
    """基于同构 6 表 SQLite 的持久化隐空间与棋步结算控制器。"""

    def __init__(self, memory_runtime: OntologyRuntime, db_path: str | Path) -> None:
        raw_path = str(db_path).strip()
        if not raw_path or raw_path == ":memory:":
            raise ValueError(
                "LatentSpace requires a persistent filesystem db_path, not ':memory:'"
            )
        self._memory = memory_runtime
        self.db_path = Path(raw_path)

        if self.db_path.exists():
            check_kernel_database(self.db_path)
            self._latent = OntologyRuntime(memory_runtime.spec, db_path=self.db_path)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._latent = OntologyRuntime(memory_runtime.spec, db_path=self.db_path)
            self._memory.connection.backup(self._latent.connection)

    # -- lifecycle & properties --------------------------------------------
    @property
    def spec(self) -> OntologySpec:
        return self._memory.spec

    @property
    def memory_runtime(self) -> OntologyRuntime:
        return self._memory

    @property
    def latent_runtime(self) -> OntologyRuntime:
        return self._latent

    @property
    def is_open(self) -> bool:
        return self._latent.is_open

    def close(self) -> None:
        self._latent.close()

    def __enter__(self) -> "LatentSpace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _require_open(self) -> None:
        if not self._latent.is_open:
            raise GovernanceViolationError("latent space is already closed")

    def rebase(self) -> dict[str, Any]:
        """用最新记忆世界重克隆对齐隐空间（返回隐空间状态指纹）。"""
        self._require_open()
        self._memory.connection.backup(self._latent.connection)
        self._latent._automation_fires.clear()
        return self._latent.state_fingerprint()

    # -- stage / decide moves in latent space ------------------------------
    def stage_move(
        self,
        run_id: str,
        action_name: str,
        params: BaseModel | dict[str, Any] | None,
        actor: Actor,
        *,
        auto_trigger: bool = False,
    ) -> dict[str, Any]:
        """在隐空间内推演并落子；记忆世界零写入。"""
        self._require_open()
        settled = self._memory.get_action_run(run_id)
        if settled is not None and settled["status"] in _SETTLED_STATUSES:
            return settled
        return self._latent.submit_action(
            run_id, action_name, params, actor, auto_trigger=auto_trigger
        )

    def approve_move(self, run_id: str, approver: Actor) -> dict[str, Any]:
        """批准隐空间中处于 ``PENDING_APPROVAL`` 的棋步；记忆世界零写入。"""
        self._require_open()
        return self._latent.approve_action(run_id, approver)

    def reject_move(
        self,
        run_id: str,
        approver: Actor,
        *,
        reason: str = "rejected by approver",
    ) -> dict[str, Any]:
        """驳回隐空间中处于 ``PENDING_APPROVAL`` 的棋步；记忆世界零写入。"""
        self._require_open()
        return self._latent.reject_action(run_id, approver, reason=reason)

    def _list_unsettled_moves(self, latent_status: str) -> list[dict[str, Any]]:
        """列出隐空间处于指定状态、且尚未在记忆世界结算的棋步（跨重启可恢复）。"""
        self._require_open()
        settled_rows = self._memory.connection.execute(
            "SELECT run_id FROM ontology_action_runs "
            "WHERE status IN ('COMMITTED', 'REJECTED', 'REVERTED')"
        ).fetchall()
        settled_ids = {str(r["run_id"]) for r in settled_rows}

        latent_rows = self._latent.connection.execute(
            "SELECT * FROM ontology_action_runs "
            "WHERE status = ? ORDER BY executed_at ASC, rowid ASC",
            (latent_status,),
        ).fetchall()

        moves: list[dict[str, Any]] = []
        for row in latent_rows:
            run_id = str(row["run_id"])
            if run_id in settled_ids:
                continue
            run = OntologyRuntime._row_to_run(row)
            action = self.spec.get_action(run["action_type"])
            diff = ObjectDiff.from_dict(run["object_diff"])
            touches_source = bool(action.affects_source) or _diff_touches_source(
                self.spec, diff
            )
            moves.append(
                {
                    "run_id": run["run_id"],
                    "action_type": run["action_type"],
                    "params": run["params"],
                    "actor": {
                        "actor_id": run["actor_id"],
                        "role": run["actor_role"],
                    },
                    "actor_id": run["actor_id"],
                    "actor_role": run["actor_role"],
                    "affects_source": touches_source,
                    "object_diff": run["object_diff"],
                    "read_versions": run["read_versions"],
                    "approved_by": run["approved_by"],
                    "executed_at": run["executed_at"],
                }
            )
        return moves

    def pending_moves(self) -> list[dict[str, Any]]:
        """枚举隐空间已 ``COMMITTED`` 但尚未在记忆世界结算的棋步（跨重启可恢复）。"""
        return self._list_unsettled_moves("COMMITTED")

    def awaiting_approval_moves(self) -> list[dict[str, Any]]:
        """枚举隐空间正处于 ``PENDING_APPROVAL`` 且尚未结算的棋步。"""
        return self._list_unsettled_moves("PENDING_APPROVAL")

    # -- independent readback verification & settlement --------------------
    def settle_move(
        self,
        run_id: str,
        *,
        readback_objects: dict[str, list[dict[str, Any]]] | None = None,
        readback_links: dict[str, list[tuple[str, str]]] | None = None,
        auto_trigger: bool = False,
    ) -> dict[str, Any]:
        """核对并结算隐空间棋步：一致则提升进记忆，不一致则记 REJECTED 并 rebase。"""
        self._require_open()
        settled = self._memory.get_action_run(run_id)
        if settled is not None and settled["status"] in _SETTLED_STATUSES:
            return settled

        latent_run = self._latent.get_action_run(run_id)
        if latent_run is None:
            raise ObjectNotFoundError(f"latent move {run_id!r} not found")
        if latent_run["status"] != AUDIT_STATUS_COMMITTED:
            raise GovernanceViolationError(
                f"latent move {run_id!r} has status {latent_run['status']!r}; "
                "only COMMITTED latent moves can settle"
            )

        action = self.spec.get_action(latent_run["action_type"])
        actor = Actor(
            actor_id=str(latent_run["actor_id"]),
            role=str(latent_run["actor_role"]),
            clearance_tags={"*"},
        )
        diff = ObjectDiff.from_dict(latent_run["object_diff"])
        read_versions = dict(latent_run["read_versions"])
        params_json = dict(latent_run["params"])
        approved_by = latent_run["approved_by"]
        touches_source = bool(action.affects_source) or _diff_touches_source(
            self.spec, diff
        )

        if not touches_source:
            return self._settle_owned_move(
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                read_versions=read_versions,
                diff=diff,
                approved_by=approved_by,
                auto_trigger=auto_trigger,
            )

        if readback_objects is None:
            raise GovernanceViolationError(
                f"move {run_id!r} ({action.name!r}) affects external source state "
                "and requires independent readback_objects"
            )

        readback_by_type, inline_edges, standalone_edges = self._normalize_readback(
            readback_objects, readback_links
        )
        mismatches = self._verify_readback_matches_diff(
            diff, readback_by_type, inline_edges, standalone_edges
        )
        if mismatches:
            return self._reject_and_rebase(
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                read_versions=read_versions,
                approved_by=approved_by,
                error_code=DEVICE_STATE_MISMATCH,
                error_message="; ".join(mismatches),
            )

        return self._promote_source_move(
            run_id=run_id,
            action=action,
            actor=actor,
            params_json=params_json,
            read_versions=read_versions,
            diff=diff,
            approved_by=approved_by,
            auto_trigger=auto_trigger,
        )

    def _settle_owned_move(
        self,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        diff: ObjectDiff,
        approved_by: str | None,
        auto_trigger: bool,
    ) -> dict[str, Any]:
        with self._memory._write_gate() as conn:
            self._memory._verify_read_versions(conn, read_versions)
            self._memory._apply_diff(conn, diff)
            self._memory.check_invariants()
            self._memory.check_transition_invariants(diff)
            insert_action_run(
                self._memory,
                conn,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                status=AUDIT_STATUS_COMMITTED,
                read_versions=read_versions,
                diff=diff,
                approved_by=approved_by,
            )
        return self._finish_committed(actor, run_id, auto_trigger=auto_trigger)

    def _normalize_readback(
        self,
        readback_objects: dict[str, list[dict[str, Any]]],
        readback_links: dict[str, list[tuple[str, str]]] | None,
    ) -> tuple[
        dict[str, dict[str, dict[str, Any]]],
        dict[str, set[tuple[str, str, str, str]]],
        dict[str, set[tuple[str, str, str, str]]],
    ]:
        """复用摄入驱动的类型与主权规范化器解析独立回读数据。"""
        driver = self._memory.ingestion
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
                    edge_bucket.add(
                        (type_name, object_id, decl.range, target_id)
                    )

        standalone_edges: dict[str, set[tuple[str, str, str, str]]] = {
            self.spec.get_object_property(prop_name).name: set()
            for prop_name in (readback_links or {})
        }
        for link_type, source_type, source_id, target_type, target_id in normalized_links:
            standalone_edges.setdefault(link_type, set()).add(
                (source_type, source_id, target_type, target_id)
            )
        return readback_by_type, inline_edges, standalone_edges

    def _verify_readback_matches_diff(
        self,
        diff: ObjectDiff,
        readback_by_type: dict[str, dict[str, dict[str, Any]]],
        inline_edges: dict[str, set[tuple[str, str, str, str]]],
        standalone_edges: dict[str, set[tuple[str, str, str, str]]],
    ) -> list[str]:
        mismatches: list[str] = []

        for change in diff.object_changes:
            cls = self.spec.get_object_type(change.object_type)
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
            decl = self.spec.get_object_property(lc.link_type)
            if SecurityAndAuthorityAspect._property_is_owned(self.spec, decl):
                continue
            domain_cls = self.spec.object_types.get(decl.domain)
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

    def _promote_source_move(
        self,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        diff: ObjectDiff,
        approved_by: str | None,
        auto_trigger: bool,
    ) -> dict[str, Any]:
        rejection_error: tuple[str, str] | None = None
        savepoint = "latent_settle_promote"

        with self._memory._write_gate() as conn:
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                self._apply_diff_with_convergence(conn, diff, read_versions)
                self._memory.check_invariants()
                self._memory.check_transition_invariants(diff)
                insert_action_run(
                    self._memory,
                    conn,
                    run_id=run_id,
                    action=action,
                    actor=actor,
                    params_json=params_json,
                    status=AUDIT_STATUS_COMMITTED,
                    read_versions=read_versions,
                    diff=diff,
                    approved_by=approved_by,
                )
            except (_ConvergenceConflictError, ConcurrencyConflictError) as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                rejection_error = (DEVICE_STATE_MISMATCH, str(exc))
                insert_action_run(
                    self._memory,
                    conn,
                    run_id=run_id,
                    action=action,
                    actor=actor,
                    params_json=params_json,
                    status=AUDIT_STATUS_REJECTED,
                    read_versions=read_versions,
                    diff=ObjectDiff(),
                    approved_by=approved_by,
                    error_code=rejection_error[0],
                    error_message=rejection_error[1],
                )
            except InvariantError as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                rejection_error = (exc.code or DEVICE_STATE_MISMATCH, str(exc))
                insert_action_run(
                    self._memory,
                    conn,
                    run_id=run_id,
                    action=action,
                    actor=actor,
                    params_json=params_json,
                    status=AUDIT_STATUS_REJECTED,
                    read_versions=read_versions,
                    diff=ObjectDiff(),
                    approved_by=approved_by,
                    error_code=rejection_error[0],
                    error_message=rejection_error[1],
                )
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")

        if rejection_error is not None:
            rejected_run = self._memory.get_action_run(run_id)
            if rejected_run is not None:
                self._memory._notify_run_event(actor, rejected_run)
            self.rebase()
            return rejected_run  # type: ignore[return-value]

        return self._finish_committed(actor, run_id, auto_trigger=auto_trigger)

    def _apply_diff_with_convergence(
        self,
        conn: Any,
        diff: ObjectDiff,
        read_versions: dict[str, int],
    ) -> None:
        """提升变更进记忆，并在后台快照抢跑收敛至一致终态时兼容合并。"""
        now = self._memory._utcnow()
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
            cls = self.spec.get_object_type(change.object_type)
            row = conn.execute(
                "SELECT version, properties_json FROM ontology_objects "
                "WHERE object_type = ? AND object_id = ?",
                (change.object_type, change.object_id),
            ).fetchone()
            if change.kind == "create":
                if self._apply_create_converged(conn, cls, change, row, now):
                    mutated = True
            elif change.kind == "update":
                if self._apply_update_converged(conn, cls, change, row, now):
                    mutated = True
            elif change.kind == "delete":
                if self._apply_delete_converged(conn, cls, change, row):
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
                self._memory, conn, diff, now
            )

    def _apply_create_converged(
        self,
        conn: Any,
        cls: type[Any],
        change: ObjectChange,
        row: Any,
        now: str,
    ) -> bool:
        assert change.after is not None
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
                    _dumps(change.after),
                    version,
                    _dumps(change.after_security_tags),
                    _content_hash(change.after),
                    now,
                ),
            )
            self._memory._sync_overlay(
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
                    _dumps(props_out),
                    int(row["version"]) + 1,
                    _dumps(merged_obj.security_tags()),
                    _content_hash(props_out),
                    now,
                    change.object_type,
                    change.object_id,
                ),
            )
            self._memory._sync_overlay(
                conn, change.object_type, change.object_id, props_out
            )
            return True
        self._memory._sync_overlay(
            conn, change.object_type, change.object_id, existing_props
        )
        return False

    def _apply_update_converged(
        self,
        conn: Any,
        cls: type[Any],
        change: ObjectChange,
        row: Any,
        now: str,
    ) -> bool:
        assert change.after is not None
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
                    _dumps(change.after),
                    cur_ver + 1,
                    _dumps(change.after_security_tags),
                    _content_hash(change.after),
                    now,
                    change.object_type,
                    change.object_id,
                ),
            )
            self._memory._sync_overlay(
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
                    _dumps(props_out),
                    cur_ver + 1,
                    _dumps(merged_obj.security_tags()),
                    _content_hash(props_out),
                    now,
                    change.object_type,
                    change.object_id,
                ),
            )
            self._memory._sync_overlay(
                conn, change.object_type, change.object_id, props_out
            )
            return True
        self._memory._sync_overlay(
            conn, change.object_type, change.object_id, existing_props
        )
        return False

    def _apply_delete_converged(
        self,
        conn: Any,
        cls: type[Any],
        change: ObjectChange,
        row: Any,
    ) -> bool:
        if row is None:
            self._memory._delete_overlay(conn, change.object_type, change.object_id)
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
        self._memory._delete_overlay(conn, change.object_type, change.object_id)
        return True

    def _reject_and_rebase(
        self,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        approved_by: str | None = None,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        with self._memory._write_gate() as conn:
            insert_action_run(
                self._memory,
                conn,
                run_id=run_id,
                action=action,
                actor=actor,
                params_json=params_json,
                status=AUDIT_STATUS_REJECTED,
                read_versions=read_versions,
                diff=ObjectDiff(),
                approved_by=approved_by,
                error_code=error_code,
                error_message=error_message,
            )
        rejected = self._memory.get_action_run(run_id)
        if rejected is not None:
            self._memory._notify_run_event(actor, rejected)
        self.rebase()
        return rejected  # type: ignore[return-value]

    def _finish_committed(
        self, actor: Actor, run_id: str, *, auto_trigger: bool
    ) -> dict[str, Any]:
        if auto_trigger:
            self._memory.evaluate_automations(actor)
        committed = self._memory.get_action_run(run_id)
        if committed is not None:
            self._memory._notify_post_commit(actor, committed)
        if not self.pending_moves():
            self.rebase()
        return committed  # type: ignore[return-value]

    def make_controller(self, actor: Actor, **kwargs: Any) -> Any:
        """快捷构造绑定本持久隐空间的闭环滚动时域控制器 ``ClosedLoopMPCController``。"""
        from .mpc import ClosedLoopMPCController

        return ClosedLoopMPCController(self, actor, **kwargs)
