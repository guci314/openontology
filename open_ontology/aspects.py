"""系统调用切面（Syscall Aspects）。

**架构定位**：本模块是「计算机切片」，不是微内核本体。

微内核（``spec.py`` / ``sandbox.py`` / ``runtime.py``）负责状态页表与系统
调用管线；本模块把散落在管线里的**横切关注点**收拢成五个具名切面：

===================================  ========================================
切面                                  负责什么
===================================  ========================================
``ColumnMaskingAspect``               读出口列级脱敏（列级 RLS 投影）
``SecurityAndAuthorityAspect``        RBAC 角色门禁 + 字段主权边界校验
``IdempotencyAndOCCAspect``           幂等短路 + MVCC 乐观锁版本校验
``ApprovalGateAspect``                审批软中断挂起 / 批准恢复 / 时光机回滚
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
from types import UnionType
from typing import Any, Callable, Union, get_args, get_origin

from .sandbox import ObjectDiff
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
    "ApprovalGateAspect",
    "RejectionAuditAspect",
    "AutomationReflexAspect",
    "IdleAwareCallbackAspect",
    "insert_action_run",
    "AUDIT_STATUS_REJECTED",
    "AUDIT_STATUS_PENDING",
    "AUDIT_STATUS_COMMITTED",
    "AUDIT_STATUS_REVERTED",
]

#: 被拒运行的审计状态值。
AUDIT_STATUS_REJECTED = "REJECTED"
AUDIT_STATUS_PENDING = "PENDING_APPROVAL"
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
    def object_change_side(spec: Any, change: Any) -> str | None:
        """判断一次对象变更落在 source 侧、ontology 侧，还是混写。"""
        cls = spec.get_object_type(change.object_type)
        if cls.is_fully_owned():
            return "ontology"

        owned_fields = set(cls.owned_field_defaults())
        if change.kind != "update":
            # 创建或删除底表行属于 source 侧行为
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

    @classmethod
    def check_authority(cls, spec: Any, action: ActionDef, diff: ObjectDiff) -> None:
        """主权边界强校验：混写、未声明源写入、误声明源写入全部拒绝。"""
        sides: set[str] = set()
        for change in diff.object_changes:
            side = cls.object_change_side(spec, change)
            if side == "mixed":
                raise InvariantError(
                    f"action {action.name!r} touches source and ontology-owned "
                    f"fields of {change.object_type}:{change.object_id} in one change",
                    code="MIXED_AUTHORITY",
                )
            if side is not None:
                sides.add(side)

        for link_change in diff.link_changes:
            decl = spec.get_link_type(link_change.link_type)
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


# ---------------------------------------------------------------------------
# 4. ApprovalGateAspect —— 审批软中断挂起与批准恢复
# ---------------------------------------------------------------------------
class ApprovalGateAspect:
    """审批流切面：挂起只落 diff 不动对象；批准时再校验版本并落盘。"""

    @staticmethod
    def stage_pending_approval(
        runtime: Any,
        *,
        run_id: str,
        action: ActionDef,
        actor: Actor,
        params_json: dict[str, Any],
        read_versions: dict[str, int],
        diff: ObjectDiff,
    ) -> None:
        """把需要审批的动作挂起为 ``PENDING_APPROVAL``，对象保持不动。"""
        dumps = runtime._dumps
        with runtime._transaction() as conn:
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
                    dumps(params_json),
                    AUDIT_STATUS_PENDING,
                    dumps(read_versions),
                    dumps(diff.to_dict()),
                    runtime._utcnow(),
                ),
            )

    @staticmethod
    def approve_action(runtime: Any, run_id: str, approver: Actor) -> dict[str, Any]:
        """批准一张挂起单：校验审批角色 + 重新比对版本 + 原子落盘。"""
        run = runtime.get_action_run(run_id)
        if run is None:
            raise ObjectNotFoundError(f"action run {run_id!r} not found")
        if run["status"] != AUDIT_STATUS_PENDING:
            raise GovernanceViolationError(
                f"action run {run_id!r} is {run['status']}, not PENDING_APPROVAL"
            )

        action = runtime.spec.get_action(run["action_type"])
        if approver.role not in action.approver_roles:
            raise PermissionDeniedError(
                f"actor {approver.actor_id!r} role {approver.role!r} may not approve "
                f"{action.name!r}; required one of {list(action.approver_roles)}"
            )

        read_versions = dict(run["read_versions"])
        diff = ObjectDiff.from_dict(run["object_diff"])

        with runtime._write_gate() as conn:
            IdempotencyAndOCCAspect.verify_read_versions(conn, read_versions)
            runtime._apply_diff(conn, diff)
            conn.execute(
                """
                UPDATE ontology_action_runs
                SET status = ?, approved_by = ?, executed_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    AUDIT_STATUS_COMMITTED,
                    approver.actor_id,
                    runtime._utcnow(),
                    run_id,
                    AUDIT_STATUS_PENDING,
                ),
            )
        return runtime.get_action_run(run_id)  # type: ignore[return-value]

    @staticmethod
    def revert_action(
        runtime: Any,
        run_id: str,
        revert_run_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """时光机回滚：取原 diff 的逆向补丁，整体撤回并追加一条回滚记录。"""
        existing = runtime.get_action_run(revert_run_id)
        if existing is not None:
            return existing

        run = runtime.get_action_run(run_id)
        if run is None:
            raise ObjectNotFoundError(f"action run {run_id!r} not found")

        action = runtime.spec.get_action(run["action_type"])
        if actor.role not in set(action.allowed_roles) | set(action.approver_roles):
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} role {actor.role!r} may not revert "
                f"{action.name!r}"
            )

        if run["status"] == AUDIT_STATUS_REVERTED:
            raise GovernanceViolationError(f"action run {run_id!r} was already reverted")
        if run["status"] != AUDIT_STATUS_COMMITTED:
            raise GovernanceViolationError(
                f"action run {run_id!r} is {run['status']}; only COMMITTED runs can revert"
            )

        original_diff = ObjectDiff.from_dict(run["object_diff"])
        inverted = original_diff.inverted()

        # 记录回滚将要触碰的每个对象的当前版本。
        revert_read_versions: dict[str, int] = {}
        for change in inverted.object_changes:
            row = runtime._conn.execute(
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

        with runtime._write_gate() as conn:
            runtime._apply_diff(conn, inverted)
            conn.execute(
                """
                UPDATE ontology_action_runs
                SET status = ?, reverted_by_run_id = ?
                WHERE run_id = ? AND status = ?
                """,
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
        return runtime.get_action_run(revert_run_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 审计账本写入原语（被 RejectionAudit 与 ApprovalGate 共用）
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
        with runtime._transaction() as conn:
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
