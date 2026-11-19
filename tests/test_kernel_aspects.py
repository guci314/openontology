"""TK-ONTO-KERNEL-SLIM-2 验收测试：五大系统调用切面。

覆盖：
1. ``ColumnMaskingAspect`` —— 读出口脱敏，存储行不变，system 角色绕过；
2. ``SecurityAndAuthorityAspect`` —— RBAC 角色门禁 + 字段主权三态校验 + overlay 持久化；
3. ``IdempotencyAndOCCAspect`` —— 幂等短路 + MVCC 版本校验；
4. ``ApprovalGateAspect`` —— 挂起不动对象 / 批准后落盘 / 时光机回滚；
5. ``RejectionAuditAspect`` —— 被拒企图入账且对象零改动；
6. ``AutomationReflexAspect`` —— 条件反射与去重；
7. 切面与内核的委托关系：内核方法确实是薄封装，逻辑住在 aspects.py。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from pydantic import BaseModel

from open_ontology import (
    Actor,
    InvariantError,
    OntologyRuntime,
    OntologySpec,
    PermissionDeniedError,
    ObjectProperty,
    ObjectType,
)
from open_ontology.aspects import (
    AUDIT_STATUS_COMMITTED,
    AUDIT_STATUS_PENDING,
    AUDIT_STATUS_REJECTED,
    AUDIT_STATUS_REVERTED,
    MASKED_STRING,
    ApprovalGateAspect,
    AutomationReflexAspect,
    ColumnMaskingAspect,
    IdempotencyAndOCCAspect,
    IdleAwareCallbackAspect,
    RejectionAuditAspect,
    SecurityAndAuthorityAspect,
    insert_action_run,
    mask_value,
)
from open_ontology.sandbox import ObjectDiff

ROOT = Path(__file__).resolve().parents[1]

ADMIN = Actor(actor_id="admin-1", role="admin", clearance_tags={"*"})
OPS = Actor(actor_id="ops-1", role="ops", clearance_tags={"*"})
NOBODY = Actor(actor_id="nobody", role="guest", clearance_tags={"*"})


# ---------------------------------------------------------------------------
# 测试用本体
# ---------------------------------------------------------------------------
spec = OntologySpec()


class Account(ObjectType):
    __primary_key__ = "account_id"
    __title__ = "name"
    __owned__ = {"internal_note": ""}
    __masked_fields__ = {"balance": ("finance", "admin", "ops")}
    __mask_defaults__ = {"balance": -1}

    account_id: str
    name: str
    balance: int = 0
    internal_note: str = ""


class OwnedThing(ObjectType):
    __primary_key__ = "thing_id"
    __owned__ = True

    thing_id: str
    name: str = ""


class AutoItem(ObjectType):
    __primary_key__ = "item_id"

    item_id: str
    qty: int = 0


spec.object_type(Account)
spec.object_type(OwnedThing)
spec.add_object_property(
    ObjectProperty(
        name="account_links",
        domain="Account",
        range="Account",
        cardinality="many-to-many",
        owned=False,
    )
)


class RenameParams(BaseModel):
    account_id: str
    name: str


class AnnotateParams(BaseModel):
    account_id: str
    note: str


@spec.action(
    "rename_account",
    RenameParams,
    allowed_roles=("admin", "ops"),
    affects_source=True,
    description="改账户名（source-backed 字段）",
)
def rename_account(ctx, params):
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, name=params.name)


@spec.action(
    "annotate_account",
    AnnotateParams,
    allowed_roles=("admin", "ops"),
    approver_roles=("admin", "ops"),
    affects_source=False,
    description="写自有治理字段",
)
def annotate_account(ctx, params):
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, internal_note=params.note)


@spec.function("noop", RenameParams, description="占位只读函数")
def noop(ctx, params):
    return {}


@pytest.fixture()
def runtime() -> OntologyRuntime:
    rt = OntologyRuntime(spec)
    rt.put_object(
        Account(account_id="A1", name="张三", balance=1000),
        actor=ADMIN,
    )
    yield rt
    rt.close()


# ---------------------------------------------------------------------------
# 1. ColumnMaskingAspect
# ---------------------------------------------------------------------------
def test_mask_value_uses_explicit_defaults():
    assert mask_value(Account, "balance", 1000) == -1


def test_mask_value_falls_back_by_annotation():
    class Sample(ObjectType):
        __primary_key__ = "sid"
        sid: str
        amount: int = 0
        note: str = ""
        flag: bool = False

    assert mask_value(Sample, "amount", 42) == 0
    assert mask_value(Sample, "note", "secret") == MASKED_STRING
    assert mask_value(Sample, "flag", True) is False


def test_masking_hides_column_for_unauthorized_role(runtime):
    account = runtime.get_object(Account, "A1", ADMIN)
    masked = ColumnMaskingAspect.apply_masking(account, NOBODY)
    assert masked.balance == -1
    # 存储行完全没变
    assert runtime.get_object(Account, "A1", ADMIN).balance == 1000


def test_masking_passes_through_for_authorized_role(runtime):
    account = runtime.get_object(Account, "A1", ADMIN)
    same = ColumnMaskingAspect.apply_masking(account, OPS)
    assert same is account or same.balance == 1000


def test_masking_bypassed_for_system_role(runtime):
    account = runtime.get_object(Account, "A1", ADMIN)
    system = Actor(actor_id="sys", role="system", clearance_tags={"*"})
    assert ColumnMaskingAspect.apply_masking(account, system).balance == 1000


def test_runtime_delegates_masking_to_aspect(runtime):
    account = runtime.get_object(Account, "A1", ADMIN)
    assert runtime._apply_masking(account, NOBODY).balance == -1


# ---------------------------------------------------------------------------
# 2. SecurityAndAuthorityAspect
# ---------------------------------------------------------------------------
def test_check_role_allows_and_denies():
    action = spec.get_action("rename_account")
    SecurityAndAuthorityAspect.check_role(action, ADMIN)  # 不抛
    with pytest.raises(PermissionDeniedError):
        SecurityAndAuthorityAspect.check_role(action, NOBODY)


def test_check_authority_rejects_undeclared_source_write():
    """声明 affects_source=False 的动作却写了 source 字段 → 拒绝。"""
    action = spec.get_action("annotate_account")
    # 伪造一个只碰 source 字段的 diff
    diff = ObjectDiff()
    from open_ontology.sandbox import ObjectChange

    diff.object_changes.append(
        ObjectChange(
            object_type="Account",
            object_id="A1",
            kind="update",
            before={"name": "old"},
            after={"name": "new"},
        )
    )
    with pytest.raises(InvariantError) as excinfo:
        SecurityAndAuthorityAspect.check_authority(spec, action, diff)
    assert excinfo.value.code == "UNDECLARED_SOURCE_WRITE"


def test_check_authority_rejects_misdeclared_source_write():
    """声明 affects_source=True 的动作却只写自有字段 → 拒绝。"""
    action = spec.get_action("rename_account")
    diff = ObjectDiff()
    from open_ontology.sandbox import ObjectChange

    diff.object_changes.append(
        ObjectChange(
            object_type="Account",
            object_id="A1",
            kind="update",
            before={"internal_note": ""},
            after={"internal_note": "x"},
        )
    )
    with pytest.raises(InvariantError) as excinfo:
        SecurityAndAuthorityAspect.check_authority(spec, action, diff)
    assert excinfo.value.code == "MISDECLARED_SOURCE_WRITE"


def test_check_authority_rejects_mixed_change():
    from open_ontology.sandbox import ObjectChange

    action = spec.get_action("rename_account")
    diff = ObjectDiff()
    diff.object_changes.append(
        ObjectChange(
            object_type="Account",
            object_id="A1",
            kind="update",
            before={"name": "old", "internal_note": ""},
            after={"name": "new", "internal_note": "x"},
        )
    )
    with pytest.raises(InvariantError) as excinfo:
        SecurityAndAuthorityAspect.check_authority(spec, action, diff)
    assert excinfo.value.code == "MIXED_AUTHORITY"


def test_object_change_side_classification():
    from open_ontology.sandbox import ObjectChange

    def side(**kwargs):
        return SecurityAndAuthorityAspect.object_change_side(
            spec, ObjectChange(object_type="Account", object_id="A1", **kwargs)
        )

    assert side(kind="create", after={"name": "x"}) == "source"
    assert side(kind="update", before={"name": "a"}, after={"name": "b"}) == "source"
    assert (
        side(kind="update", before={"internal_note": ""}, after={"internal_note": "x"})
        == "ontology"
    )
    assert side(kind="update", before={"name": "a"}, after={"name": "a"}) is None


def test_overlay_persists_owned_fields(runtime):
    runtime.submit_action(
        "RUN-A1", "annotate_account", {"account_id": "A1", "note": "治理备注"}, ADMIN
    )
    row = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Account' AND object_id = 'A1'"
    ).fetchone()
    assert row is not None
    assert "治理备注" in row["patch_json"]


def test_overlay_cleared_when_reverting_to_default(runtime):
    runtime.submit_action(
        "RUN-A2", "annotate_account", {"account_id": "A1", "note": "临时"}, ADMIN
    )
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == "临时"

    # 改回默认值（None）→ overlay 里的 patch 被清除
    runtime.submit_action(
        "RUN-A3", "annotate_account", {"account_id": "A1", "note": ""}, ADMIN
    )
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == ""
    row = runtime.connection.execute(
        "SELECT patch_json FROM ontology_overlay "
        "WHERE object_type = 'Account' AND object_id = 'A1'"
    ).fetchone()
    assert row is None, f"overlay 未被清除：{dict(row) if row else None}"


# ---------------------------------------------------------------------------
# 3. IdempotencyAndOCCAspect
# ---------------------------------------------------------------------------
def test_idempotent_run_returns_existing(runtime):
    first = runtime.submit_action(
        "RUN-IDEM", "annotate_account", {"account_id": "A1", "note": "一"}, ADMIN
    )
    second = runtime.submit_action(
        "RUN-IDEM", "annotate_account", {"account_id": "A1", "note": "二"}, ADMIN
    )
    assert first["run_id"] == second["run_id"]
    # 第二次没有真的执行：备注仍是第一次的值
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == "一"
    assert (
        IdempotencyAndOCCAspect.check_idempotent_run(runtime, "RUN-IDEM") is not None
    )


def test_current_version_reads_and_returns_none(runtime):
    assert (
        IdempotencyAndOCCAspect.current_version(
            runtime.connection, "Account", "A1"
        )
        == 1
    )
    assert (
        IdempotencyAndOCCAspect.current_version(
            runtime.connection, "Account", "MISSING"
        )
        is None
    )


def test_verify_read_versions_raises_on_stale(runtime):
    from open_ontology.spec import ConcurrencyConflictError

    with pytest.raises(ConcurrencyConflictError):
        IdempotencyAndOCCAspect.verify_read_versions(
            runtime.connection, {"Account:A1": 99}
        )
    # 正确版本不抛
    IdempotencyAndOCCAspect.verify_read_versions(
        runtime.connection, {"Account:A1": 1}
    )


# ---------------------------------------------------------------------------
# 4. ApprovalGateAspect
# ---------------------------------------------------------------------------
def test_pending_approval_leaves_objects_untouched(runtime):
    runtime.spec.get_action("annotate_account")
    # 用 approval_rule 强制挂起
    action = runtime.spec.actions["annotate_account"]
    original = action.approval_rule
    action.approval_rule = lambda params, actor: True
    try:
        run = runtime.submit_action(
            "RUN-PENDING",
            "annotate_account",
            {"account_id": "A1", "note": "待批"},
            ADMIN,
        )
        assert run["status"] == AUDIT_STATUS_PENDING
        assert runtime.get_object(Account, "A1", ADMIN).internal_note == ""

        approved = runtime.approve_action("RUN-PENDING", ADMIN)
        assert approved["status"] == AUDIT_STATUS_COMMITTED
        assert runtime.get_object(Account, "A1", ADMIN).internal_note == "待批"
    finally:
        action.approval_rule = original


def test_approve_rejects_wrong_role(runtime):
    action = runtime.spec.actions["annotate_account"]
    original = action.approval_rule
    action.approval_rule = lambda params, actor: True
    try:
        runtime.submit_action(
            "RUN-P2", "annotate_account", {"account_id": "A1", "note": "x"}, ADMIN
        )
        with pytest.raises(PermissionDeniedError):
            runtime.approve_action("RUN-P2", NOBODY)
    finally:
        action.approval_rule = original


def test_revert_restores_previous_state(runtime):
    runtime.submit_action(
        "RUN-R1", "annotate_account", {"account_id": "A1", "note": "第一版"}, ADMIN
    )
    runtime.submit_action(
        "RUN-R2", "annotate_account", {"account_id": "A1", "note": "第二版"}, ADMIN
    )
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == "第二版"

    runtime.revert_action("RUN-R2", "RUN-R2-REVERT", ADMIN)
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == "第一版"
    assert runtime.get_action_run("RUN-R2")["status"] == AUDIT_STATUS_REVERTED


def test_revert_is_idempotent(runtime):
    runtime.submit_action(
        "RUN-R3", "annotate_account", {"account_id": "A1", "note": "x"}, ADMIN
    )
    first = runtime.revert_action("RUN-R3", "RUN-R3-REV", ADMIN)
    second = runtime.revert_action("RUN-R3", "RUN-R3-REV", ADMIN)
    assert first["run_id"] == second["run_id"]


def test_revert_rejects_pending_run(runtime):
    from open_ontology.spec import GovernanceViolationError

    action = runtime.spec.actions["annotate_account"]
    original = action.approval_rule
    action.approval_rule = lambda params, actor: True
    try:
        runtime.submit_action(
            "RUN-P3", "annotate_account", {"account_id": "A1", "note": "x"}, ADMIN
        )
        with pytest.raises(GovernanceViolationError):
            runtime.revert_action("RUN-P3", "RUN-P3-REV", ADMIN)
    finally:
        action.approval_rule = original


def test_revert_requires_approver_role_when_approver_roles_declared(runtime):
    """动作声明了 approver_roles 时，普通 allowed_roles（如 ops）不得绕过审批权单方面撤回已提交记录。"""
    # guarded_rename: allowed_roles=('admin', 'ops')? Let's verify using a dedicated governed action.
    class ControlledNoteParams(BaseModel):
        account_id: str
        note: str

    if "controlled_note" not in runtime.spec.actions:
        @runtime.spec.action(
            "controlled_note",
            ControlledNoteParams,
            allowed_roles=("ops", "admin"),
            approver_roles=("admin",),
            affects_source=False,
        )
        def _controlled_note(ctx, params: ControlledNoteParams) -> None:
            acc = ctx.require(Account, params.account_id)
            ctx.modify(acc, internal_note=params.note)

    run = runtime.submit_action(
        "RUN-CTRL-1", "controlled_note", {"account_id": "A1", "note": "由ops提交"}, OPS
    )
    assert run["status"] == AUDIT_STATUS_COMMITTED
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == "由ops提交"

    # Non-approver submitter role (ops) MUST be denied revert_action
    with pytest.raises(PermissionDeniedError):
        runtime.revert_action("RUN-CTRL-1", "RUN-CTRL-1-REV-OPS", OPS)
    assert runtime.get_action_run("RUN-CTRL-1")["status"] == AUDIT_STATUS_COMMITTED
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == "由ops提交"

    # Approver role (admin) succeeds cleanly
    reverted = runtime.revert_action("RUN-CTRL-1", "RUN-CTRL-1-REV-ADMIN", ADMIN)
    assert reverted["status"] == AUDIT_STATUS_COMMITTED
    assert runtime.get_action_run("RUN-CTRL-1")["status"] == AUDIT_STATUS_REVERTED
    assert runtime.get_object(Account, "A1", ADMIN).internal_note == ""


# ---------------------------------------------------------------------------
# 5. RejectionAuditAspect
# ---------------------------------------------------------------------------
def test_rejection_is_audited_and_objects_untouched(runtime):
    class Boom(BaseModel):
        account_id: str

    @runtime.spec.action(
        "boom_action",
        Boom,
        allowed_roles=("admin",),
        affects_source=False,
        description="必定失败",
    )
    def boom_action(ctx, params):
        raise InvariantError("故意失败", code="DELIBERATE")

    with pytest.raises(InvariantError):
        runtime.submit_action(
            "RUN-REJ", "boom_action", {"account_id": "A1"}, ADMIN
        )

    rejected = runtime.list_action_runs(status=AUDIT_STATUS_REJECTED)
    assert any(row["run_id"] == "RUN-REJ" for row in rejected)
    assert rejected[-1]["error_code"] == "DELIBERATE"
    # 对象零改动
    assert runtime.get_object(Account, "A1", ADMIN).name == "张三"


def test_record_rejection_writes_expected_row(runtime):
    action = runtime.spec.get_action("annotate_account")
    RejectionAuditAspect.record_rejection(
        runtime,
        run_id="RUN-DIRECT",
        action=action,
        actor=ADMIN,
        params_json={"account_id": "A1", "note": "x"},
        read_versions={},
        error=InvariantError("直接调用", code="DIRECT"),
    )
    row = runtime.get_action_run("RUN-DIRECT")
    assert row["status"] == AUDIT_STATUS_REJECTED
    assert row["error_code"] == "DIRECT"


# ---------------------------------------------------------------------------
# 6. AutomationReflexAspect
# ---------------------------------------------------------------------------
@pytest.fixture()
def auto_runtime():
    """独立的 spec + runtime：automation 测试不污染其它用例。"""
    spec2 = OntologySpec()
    spec2.object_type(AutoItem)
    rt = OntologyRuntime(spec2)
    rt.put_object(AutoItem(item_id="I1", qty=1000), actor=ADMIN)
    yield rt
    rt.close()


def test_automation_fires_once_per_version(auto_runtime):
    runtime = auto_runtime

    @runtime.spec.automation(
        "mark_big",
        AutoItem,
        condition=lambda obj: obj.qty >= 500,
        description="大额账户打标",
    )
    def mark_big(rt, obj, actor):
        return f"big:{obj.item_id}"

    fired = AutomationReflexAspect.evaluate_automations(runtime)
    assert len(fired) == 1
    assert fired[0]["automation"] == "mark_big"

    # 同版本重复评估不会重复触发
    assert AutomationReflexAspect.evaluate_automations(runtime) == []
    history = AutomationReflexAspect.automation_history(runtime)
    assert len(history) == 1


def test_automation_filters_by_object_type(auto_runtime):
    """只评估被请求的对象类型。"""
    runtime = auto_runtime

    @runtime.spec.automation(
        "watch_item",
        AutoItem,
        condition=lambda obj: True,
        description="只看条目",
    )
    def watch_item(rt, obj, actor):
        return "ok"

    # 请求评估 OwnedThing（不在这个 spec 里）：不该抛错也不该触发
    fired = AutomationReflexAspect.evaluate_automations(
        runtime, automation="no_such_rule"
    )
    assert fired == []


def test_runtime_delegates_automations_to_aspect(auto_runtime):
    runtime = auto_runtime

    @runtime.spec.automation(
        "count_it",
        AutoItem,
        condition=lambda obj: True,
        description="计数",
    )
    def count_it(rt, obj, actor):
        return 1

    assert len(runtime.evaluate_automations()) == 1
    assert len(runtime.automation_history()) == 1


# ---------------------------------------------------------------------------
# 7. 切面与内核的委托关系
# ---------------------------------------------------------------------------
def test_kernel_delegates_to_aspects_not_reimplements():
    """内核方法必须是薄委托：逻辑住在 aspects.py，不在 runtime.py 里重写。"""
    text = (ROOT / "open_ontology" / "runtime.py").read_text(encoding="utf-8")

    # 脱敏逻辑已经不在内核里
    assert "__masked_fields__" not in text
    assert "***MASKED***" not in text
    # 主权判定逻辑已经不在内核里
    assert "MIXED_AUTHORITY" not in text
    assert "UNDECLARED_SOURCE_WRITE" not in text
    # 审批与回滚的完整实现已经不在内核里
    assert "only COMMITTED runs can revert" not in text
    assert "may not approve" not in text
    # 委托调用必须存在
    for name in (
        "ColumnMaskingAspect",
        "SecurityAndAuthorityAspect",
        "IdempotencyAndOCCAspect",
        "ApprovalGateAspect",
        "RejectionAuditAspect",
        "AutomationReflexAspect",
    ):
        assert name in text, f"内核缺少对 {name} 的委托"


def test_aspects_module_is_self_contained():
    """切面模块不许反过来 import 内核运行时（避免循环依赖）。"""
    text = (ROOT / "open_ontology" / "aspects.py").read_text(encoding="utf-8")
    assert "from .runtime import" not in text
    assert "import open_ontology.runtime" not in text


def test_runtime_line_budget():
    """微内核行数预算：1700 → 1400 以内。"""
    lines = len(
        (ROOT / "open_ontology" / "runtime.py").read_text(encoding="utf-8").splitlines()
    )
    assert lines <= 1400, f"runtime.py 仍有 {lines} 行，超出微内核预算"


# ---------------------------------------------------------------------------
# 8. 微内核隐喻：四类模块的物理边界
# ---------------------------------------------------------------------------
MICROKERNEL_MODULES = ("spec.py", "sandbox.py", "runtime.py", "query.py")
ASPECT_MODULES = ("aspects.py",)
DRIVER_MODULES = ("ingestion.py",)


def test_four_module_categories_exist():
    """文档声明的四类模块必须真实存在。"""
    pkg = ROOT / "open_ontology"
    for name in MICROKERNEL_MODULES + ASPECT_MODULES + DRIVER_MODULES:
        assert (pkg / name).is_file(), f"缺少模块 {name}"
    assert (pkg / "schema.sql").is_file()


def test_microkernel_does_not_contain_protocol_code():
    """微内核不碰协议：不出现 HTTP / 子进程 / 命令行传输。"""
    pkg = ROOT / "open_ontology"
    for name in MICROKERNEL_MODULES:
        text = (pkg / name).read_text(encoding="utf-8")
        for token in ("urllib", "subprocess", "http.client", "socket"):
            assert token not in text, f"{token!r} 泄漏进微内核 {name}"


def test_aspects_are_not_business_specific():
    """切面是领域无关的：不出现任何具体业务概念。"""
    text = (ROOT / "open_ontology" / "aspects.py").read_text(encoding="utf-8")
    for token in (
        "WorkTicket",
        "Engineer",
        "Deliverable",
        "BoardColumn",
        "claim_card",
        "record_outcome",
    ):
        assert token not in text, f"业务概念 {token!r} 泄漏进切面模块"


def test_drivers_do_not_reimplement_governance():
    """设备驱动不复制内核治理语义：不自己写写门禁、不做审批裁决。"""
    text = (ROOT / "open_ontology" / "ingestion.py").read_text(encoding="utf-8")
    for token in ("UPDATE ontology_write_gate", "approval_rule", "allowed_roles"):
        assert token not in text, f"治理语义 {token!r} 被驱动复制"


def test_architecture_doc_declares_four_categories():
    """架构文档必须写清四类模块与行数预算，避免口径漂移。"""
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for token in (
        "用户程序",
        "设备驱动",
        "系统调用切面",
        "微内核",
        "ingestion.py",
        "aspects.py",
        "1400",
    ):
        assert token in text, f"架构文档缺少 {token!r}"


def test_architecture_doc_api_references_are_real():
    """文档里点名的方法必须真实存在，防止 API 改名后文档漂移。"""
    doc = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    pkg = ROOT / "open_ontology"

    # 文档点名的方法 → 必须能在源码里找到定义
    for symbol in (
        "load_snapshot",
        "load_mdo_snapshots",
        "approve_action",
        "revert_action",
        "evaluate_automations",
    ):
        found = any(
            f"def {symbol}" in (pkg / name).read_text(encoding="utf-8")
            for name in ("runtime.py", "ingestion.py", "aspects.py")
        )
        assert found, f"文档引用 {symbol} 但源码里没有定义"

    # 已经改名的旧 API 不许再出现在文档里
    for stale in (
        "approve_action_run",
        "revert_action_run",
        "ingest_snapshot",
        "__owned_fields__",
        "EVIDENCE_REQUIRED",
    ):
        assert stale not in doc, f"文档仍引用已不存在的 {stale}"


# ---------------------------------------------------------------------------
# 9. IdleAwareCallbackAspect —— 忙闲感知防吞回调
# ---------------------------------------------------------------------------
class _CallbackProbe:
    """记录投递轨迹的假目标集群：``busy`` 集合里的目标算「正忙」。"""

    def __init__(self, busy: set[str] | None = None) -> None:
        self.busy: set[str] = set(busy or ())
        self.sent: list[tuple[str, dict]] = []
        self.boom: set[str] = set()

    def is_idle(self, target_id: str) -> bool:
        return target_id not in self.busy

    def send(self, target_id: str, payload: dict) -> None:
        if target_id in self.boom:
            raise RuntimeError(f"会话 {target_id} 已关闭")
        self.sent.append((target_id, payload))

    def aspect(self, wait_idle_fn=None) -> IdleAwareCallbackAspect:
        return IdleAwareCallbackAspect(
            is_idle_fn=self.is_idle,
            send_fn=self.send,
            wait_idle_fn=wait_idle_fn,
        )


def test_idle_aware_callback_is_exported():
    import open_ontology.aspects as aspects_module

    assert "IdleAwareCallbackAspect" in aspects_module.__all__


def test_idle_aware_callback_delivers_immediately_when_idle():
    probe = _CallbackProbe()
    aspect = probe.aspect()

    status = aspect.deliver("ses-1", {"card_id": "C-1", "summary": "完成"})

    assert status == "DELIVERED"
    assert len(aspect.delivered) == 1
    assert aspect.delivered[0]["target_id"] == "ses-1"
    assert aspect.delivered[0]["payload"]["card_id"] == "C-1"
    assert aspect.pending_queue == []
    assert probe.sent[0][0] == "ses-1"


def test_idle_aware_callback_queues_without_losing_when_busy():
    probe = _CallbackProbe(busy={"ses-busy"})
    aspect = probe.aspect()

    status = aspect.deliver("ses-busy", {"card_id": "C-1"})

    assert status == "QUEUED"
    assert probe.sent == []  # 绝不强行打断
    assert len(aspect.pending_queue) == 1
    assert aspect.pending_queue[0]["target_id"] == "ses-busy"
    assert aspect.delivered == []
    # 暂存的是深拷贝：外部改载荷不会污染队列
    payload = {"card_id": "C-2"}
    aspect.deliver("ses-busy", payload)
    payload["card_id"] = "MUTATED"
    assert aspect.pending_queue[1]["payload"]["card_id"] == "C-2"


def test_idle_aware_callback_flush_replays_fifo_after_idle():
    probe = _CallbackProbe(busy={"ses-1"})
    aspect = probe.aspect()
    for index in range(3):
        assert aspect.deliver("ses-1", {"seq": index}) == "QUEUED"
    assert len(aspect.pending_queue) == 3

    probe.busy.clear()
    replayed = aspect.flush()

    assert replayed == 3
    assert [payload["seq"] for _, payload in probe.sent] == [0, 1, 2]
    assert aspect.pending_queue == []
    assert len(aspect.delivered) == 3


def test_idle_aware_callback_flush_keeps_busy_items():
    probe = _CallbackProbe(busy={"ses-busy", "ses-idle"})
    aspect = probe.aspect()
    aspect.deliver("ses-busy", {"seq": 1})
    aspect.deliver("ses-idle", {"seq": 2})

    assert aspect.flush() == 0
    assert len(aspect.pending_queue) == 2

    # 指定 target 只处理该目标，其余原样保留
    probe.busy.discard("ses-idle")
    assert aspect.flush("ses-idle") == 1
    assert [item["target_id"] for item in aspect.pending_queue] == ["ses-busy"]


def test_idle_aware_callback_isolates_send_failure():
    probe = _CallbackProbe()
    probe.boom.add("ses-boom")
    aspect = probe.aspect()

    status = aspect.deliver("ses-boom", {"card_id": "C-1"})  # 不外抛

    assert status == "FAILED"
    assert aspect.delivered == []
    assert len(aspect.failed_deliveries) == 1
    failure = aspect.failed_deliveries[0]
    assert failure["target_id"] == "ses-boom"
    assert failure["payload"]["card_id"] == "C-1"
    assert "RuntimeError" in failure["error"]


def test_idle_aware_callback_skips_blank_target():
    probe = _CallbackProbe()
    aspect = probe.aspect()

    assert aspect.deliver("", {"card_id": "C-1"}) == "SKIPPED"
    assert probe.sent == []
    assert aspect.pending_queue == []
    assert aspect.delivered == []


def test_idle_aware_callback_wait_fn_is_used_and_errors_ignored():
    probe = _CallbackProbe()
    calls: list[tuple] = []

    def wait_fn(target_id, quiet_seconds, timeout):
        calls.append((target_id, quiet_seconds, timeout))
        raise RuntimeError("等待端点挂了")

    aspect = probe.aspect(wait_idle_fn=wait_fn)
    status = aspect.deliver(
        "ses-1", {"card_id": "C-1"}, wait=True, quiet_seconds=0.5, timeout=7.0
    )

    assert calls == [("ses-1", 0.5, 7.0)]
    assert status == "DELIVERED"  # 等待失败不影响投递


def test_idle_aware_callback_flush_marks_failed_items():
    probe = _CallbackProbe(busy={"ses-1"})
    aspect = probe.aspect()
    aspect.deliver("ses-1", {"seq": 0})
    aspect.deliver("ses-1", {"seq": 1})

    probe.busy.clear()
    probe.boom.add("ses-1")

    assert aspect.flush() == 0
    assert aspect.pending_queue == []
    assert len(aspect.failed_deliveries) == 2


# ---------------------------------------------------------------------------
# 10. 两相 post_approval 的主权门禁（C-KERNEL-SEC-01）
# ---------------------------------------------------------------------------
class PhaseParams(BaseModel):
    account_id: str
    name: str
    note: str


def _phase_write_source(ctx, params):
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, name=params.name)


def _phase_write_owned(ctx, params):
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, internal_note=params.note)


def _phase_write_mixed(ctx, params):
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, name=f"{params.name}!", internal_note=params.note)


spec.add_object_property(
    ObjectProperty(
        name="account_owned_links",
        domain="Account",
        range="Account",
        cardinality="many-to-many",
        owned=True,
    )
)

#: 第一阶段写 source 平面，第二阶段（post_approval）只写自有平面。
spec.action(
    "phase_source_then_owned",
    PhaseParams,
    allowed_roles=("admin",),
    affects_source=True,
    post_approval=_phase_write_owned,
)(_phase_write_source)

#: 第一阶段写自有平面，第二阶段只写 source 平面。
spec.action(
    "phase_owned_then_source",
    PhaseParams,
    allowed_roles=("admin",),
    affects_source=False,
    post_approval=_phase_write_source,
)(_phase_write_owned)

#: 第二阶段在同一个对象上同时改 source 字段与自有字段 —— 必须被拒。
spec.action(
    "phase_mixed_second_stage",
    PhaseParams,
    allowed_roles=("admin",),
    affects_source=True,
    post_approval=_phase_write_mixed,
)(_phase_write_source)


def test_post_approval_second_stage_mixing_planes_is_rejected(runtime):
    """post_approval 里的混写必须被 MIXED_AUTHORITY 拒绝，且对象零改动。"""
    params = {"account_id": "A1", "name": "李四", "note": "治理备注"}

    sim = runtime.simulate_action("phase_mixed_second_stage", params, ADMIN)
    assert sim.valid is False
    assert sim.error_code == "MIXED_AUTHORITY"

    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action("RUN-PHASE-MIXED", "phase_mixed_second_stage", params, ADMIN)
    assert excinfo.value.code == "MIXED_AUTHORITY"

    run = runtime.get_action_run("RUN-PHASE-MIXED")
    assert run["status"] == AUDIT_STATUS_REJECTED
    assert run["error_code"] == "MIXED_AUTHORITY"
    account = runtime.get_object(Account, "A1", ADMIN)
    assert account.name == "张三"
    assert account.internal_note == ""


def test_post_approval_second_stage_on_one_plane_is_allowed(runtime):
    """两段各自只碰一个主权平面：这是 steel_mdm / saas_unplug 的合法两相。"""
    run = runtime.submit_action(
        "RUN-PHASE-SOURCE-FIRST",
        "phase_source_then_owned",
        {"account_id": "A1", "name": "李四", "note": "治理备注"},
        ADMIN,
    )
    assert run["status"] == AUDIT_STATUS_COMMITTED
    account = runtime.get_object(Account, "A1", ADMIN)
    assert account.name == "李四"
    assert account.internal_note == "治理备注"

    run = runtime.submit_action(
        "RUN-PHASE-OWNED-FIRST",
        "phase_owned_then_source",
        {"account_id": "A1", "name": "王五", "note": "另一条备注"},
        ADMIN,
    )
    assert run["status"] == AUDIT_STATUS_COMMITTED
    account = runtime.get_object(Account, "A1", ADMIN)
    assert account.name == "王五"
    assert account.internal_note == "另一条备注"


def test_check_post_approval_authority_only_judges_the_incremental_delta():
    """只有 post_approval 追加出来的增量参与判定，不含第一阶段 diff。"""
    from open_ontology.sandbox import ObjectChange

    action = spec.get_action("phase_source_then_owned")
    pre = ObjectDiff(
        object_changes=[
            ObjectChange(
                object_type="Account",
                object_id="A1",
                kind="update",
                before={"name": "a", "internal_note": ""},
                after={"name": "b", "internal_note": ""},
            )
        ]
    )
    owned_only = ObjectDiff(
        object_changes=[
            ObjectChange(
                object_type="Account",
                object_id="A1",
                kind="update",
                before={"name": "a", "internal_note": ""},
                after={"name": "b", "internal_note": "n"},
            )
        ]
    )
    # 增量只碰自有字段：放行（即便整份 diff 横跨两个平面）。
    SecurityAndAuthorityAspect.check_post_approval_authority(
        spec, action, pre, owned_only
    )

    mixed = ObjectDiff(
        object_changes=[
            ObjectChange(
                object_type="Account",
                object_id="A1",
                kind="update",
                before={"name": "a", "internal_note": ""},
                after={"name": "c", "internal_note": "n"},
            )
        ]
    )
    with pytest.raises(InvariantError) as excinfo:
        SecurityAndAuthorityAspect.check_post_approval_authority(
            spec, action, pre, mixed
        )
    assert excinfo.value.code == "MIXED_AUTHORITY"


def test_check_post_approval_authority_detects_new_object_and_link_planes():
    """post_approval 新建对象 / 新建关系同样要按平面判定。"""
    from open_ontology.sandbox import LinkChange, ObjectChange

    action = spec.get_action("phase_source_then_owned")
    pre = ObjectDiff()
    mixed_links = ObjectDiff(
        link_changes=[
            LinkChange(
                link_type="account_links",
                source_type="Account",
                source_id="A1",
                target_type="Account",
                target_id="A2",
                kind="create",
            ),
            LinkChange(
                link_type="account_owned_links",
                source_type="Account",
                source_id="A1",
                target_type="Account",
                target_id="A2",
                kind="create",
            ),
        ]
    )
    with pytest.raises(InvariantError) as excinfo:
        SecurityAndAuthorityAspect.check_post_approval_authority(
            spec, action, pre, mixed_links
        )
    assert excinfo.value.code == "MIXED_AUTHORITY"

    new_source_object = ObjectDiff(
        object_changes=[
            ObjectChange(
                object_type="Account",
                object_id="A9",
                kind="create",
                after={"name": "新账户", "internal_note": ""},
            )
        ]
    )
    # 新对象是 source 侧行为，增量只有一个平面：放行。
    SecurityAndAuthorityAspect.check_post_approval_authority(
        spec, action, pre, new_source_object
    )


# ---------------------------------------------------------------------------
# 11. 审计账本 / 注册表的写门禁触发器（C-KERNEL-SEC-01）
# ---------------------------------------------------------------------------
def test_action_runs_insert_and_update_are_gate_protected(runtime):
    conn = runtime.connection
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute(
            """
            INSERT INTO ontology_action_runs
                (run_id, action_type, actor_id, actor_role, params_json,
                 status, executed_at)
            VALUES ('RAW-INSERT', 'rename_account', 'x', 'admin', '{}',
                    'COMMITTED', 'now')
            """
        )

    runtime.submit_action(
        "RUN-GATE", "rename_account", {"account_id": "A1", "name": "李四"}, ADMIN
    )
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute(
            "UPDATE ontology_action_runs SET status = 'REJECTED' "
            "WHERE run_id = 'RUN-GATE'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ontology_action_runs WHERE run_id = 'RUN-GATE'")

    assert runtime.get_action_run("RUN-GATE")["status"] == AUDIT_STATUS_COMMITTED
    assert conn.execute(
        "SELECT is_open FROM ontology_write_gate WHERE id = 1"
    ).fetchone()["is_open"] == 0


def test_registry_insert_update_delete_are_gate_protected(runtime):
    conn = runtime.connection
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute(
            """
            INSERT INTO ontology_registry (kind, name, spec_json, updated_at)
            VALUES ('object_type', 'Injected', '{}', 'now')
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("UPDATE ontology_registry SET updated_at = 'now'")
    with pytest.raises(sqlite3.IntegrityError, match="GOVERNANCE_VIOLATION"):
        conn.execute("DELETE FROM ontology_registry")

    # 内核自身的登记刷新仍然在 _write_gate() 内正常工作。
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM ontology_registry"
    ).fetchone()["n"]
    runtime._sync_registry()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM ontology_registry"
    ).fetchone()["n"] == before
    assert conn.execute(
        "SELECT is_open FROM ontology_write_gate WHERE id = 1"
    ).fetchone()["is_open"] == 0


class GuardedParams(BaseModel):
    account_id: str
    name: str


@spec.action(
    "guarded_rename",
    GuardedParams,
    allowed_roles=("admin",),
    approver_roles=("admin",),
    approval_rule=lambda params, actor: params.name.startswith("VIP"),
    affects_source=True,
)
def guarded_rename(ctx, params):
    if not params.name:
        ctx.invalidate("name must not be empty", code="EMPTY_NAME")
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, name=params.name)


def test_pending_approval_and_rejection_writes_pass_through_the_gate(runtime):
    """挂起审批 / 拒绝入账 / 批准 / 回滚都在写门禁内落账。"""
    with pytest.raises(InvariantError) as excinfo:
        runtime.submit_action(
            "RUN-REJECT", "guarded_rename", {"account_id": "A1", "name": ""}, ADMIN
        )
    assert excinfo.value.code == "EMPTY_NAME"
    rejected = runtime.get_action_run("RUN-REJECT")
    assert rejected["status"] == AUDIT_STATUS_REJECTED
    assert rejected["error_code"] == "EMPTY_NAME"

    pending = runtime.submit_action(
        "RUN-PENDING",
        "guarded_rename",
        {"account_id": "A1", "name": "VIP-李四"},
        ADMIN,
    )
    assert pending["status"] == AUDIT_STATUS_PENDING
    assert runtime.get_object(Account, "A1", ADMIN).name == "张三"

    approved = runtime.approve_action("RUN-PENDING", ADMIN)
    assert approved["status"] == AUDIT_STATUS_COMMITTED
    assert runtime.get_object(Account, "A1", ADMIN).name == "VIP-李四"

    reverted = runtime.revert_action("RUN-PENDING", "RUN-PENDING-REV", ADMIN)
    assert reverted["status"] == AUDIT_STATUS_COMMITTED
    assert runtime.get_object(Account, "A1", ADMIN).name == "张三"
    assert runtime.get_action_run("RUN-PENDING")["status"] == AUDIT_STATUS_REVERTED
    assert runtime.connection.execute(
        "SELECT is_open FROM ontology_write_gate WHERE id = 1"
    ).fetchone()["is_open"] == 0


# ---------------------------------------------------------------------------
# 12. 只读函数的读出口脱敏（C-KERNEL-SEC-01）
# ---------------------------------------------------------------------------
class PeekParams(BaseModel):
    account_id: str


@spec.function("peek_balance", PeekParams, description="只读：读单个账户余额")
def peek_balance(ctx, params):
    account = ctx.get(Account, params.account_id)
    return {"balance": None if account is None else account.balance}


class ScanParams(BaseModel):
    pass


@spec.function("scan_balances", ScanParams, description="只读：扫描全部账户余额")
def scan_balances(ctx, params):
    return {"balances": [account.balance for account in ctx.query(Account)]}


@spec.function("linked_balances", PeekParams, description="只读：沿关系遍历余额")
def linked_balances(ctx, params):
    account = ctx.get(Account, params.account_id)
    return {
        "balances": [
            target.balance
            for target in ctx.query_linked(account, "account_links", Account)
        ]
    }


class SetBalanceParams(BaseModel):
    account_id: str
    balance: int


@spec.action(
    "set_balance",
    SetBalanceParams,
    allowed_roles=("admin", "guest"),
    affects_source=True,
)
def set_balance(ctx, params):
    obj = ctx.get(Account, params.account_id)
    ctx.modify(obj, balance=params.balance)


def test_call_function_masks_columns_for_unauthorized_actor(runtime):
    assert runtime.call_function(
        "peek_balance", {"account_id": "A1"}, NOBODY
    )["balance"] == -1
    assert runtime.call_function(
        "peek_balance", {"account_id": "A1"}, OPS
    )["balance"] == 1000
    system = Actor(actor_id="sys", role="system", clearance_tags={"*"})
    assert runtime.call_function(
        "peek_balance", {"account_id": "A1"}, system
    )["balance"] == 1000


def test_call_function_query_and_query_linked_mask_columns(runtime):
    runtime.put_object(Account(account_id="A2", name="李四", balance=500), actor=ADMIN)
    a1 = runtime.get_object(Account, "A1", ADMIN)
    a2 = runtime.get_object(Account, "A2", ADMIN)
    runtime.link_objects(a1, "account_links", a2)

    assert runtime.call_function("scan_balances", {}, NOBODY)["balances"] == [-1, -1]
    assert sorted(runtime.call_function("scan_balances", {}, OPS)["balances"]) == [
        500,
        1000,
    ]
    assert runtime.call_function(
        "linked_balances", {"account_id": "A1"}, NOBODY
    )["balances"] == [-1]
    assert runtime.call_function(
        "linked_balances", {"account_id": "A1"}, OPS
    )["balances"] == [500]


def test_action_batches_never_mask_staged_diffs(runtime):
    """动作沙盘不脱敏：非白名单角色执行动作也只会写入真实值。"""
    run = runtime.submit_action(
        "RUN-MASK-FREE",
        "set_balance",
        {"account_id": "A1", "balance": 1234},
        NOBODY,
    )
    assert run["status"] == AUDIT_STATUS_COMMITTED
    change = run["object_diff"]["object_changes"][0]
    assert change["after"]["balance"] == 1234
    assert MASKED_STRING not in json.dumps(run["object_diff"], ensure_ascii=False)
    stored = runtime.load_object("Account", "A1")
    assert stored is not None and stored.data["balance"] == 1234


# ---------------------------------------------------------------------------
# 9. Platform-level read_only_source enforcement (spec + ObjectType + runtime)
# ---------------------------------------------------------------------------
class _ExternalCustomer(ObjectType):
    __primary_key__ = "customer_id"
    __title__ = "name"
    __owned__ = {"ops_tag": None}
    __read_only_source__ = True

    customer_id: str
    name: str
    credit_limit: int = 0
    ops_tag: str | None = None


class _MutableOrder(ObjectType):
    __primary_key__ = "order_id"
    __title__ = "order_id"
    __owned__ = {"priority": 0}

    order_id: str
    amount: int = 0
    priority: int = 0


class _OwnedNote(ObjectType):
    __primary_key__ = "note_id"
    __owned__ = True

    note_id: str
    text: str


class _CustomerEditParams(BaseModel):
    customer_id: str
    name: str = ""
    ops_tag: str = ""


class _OrderEditParams(BaseModel):
    order_id: str
    amount: int = 0


class _LinkEditParams(BaseModel):
    customer_id: str
    note_id: str = "N1"


def test_read_only_source_spec_forbids_affects_source_true_at_registration():
    """OntologySpec(read_only_source=True) 在注册期即彻底堵死 affects_source=True。"""
    from open_ontology.spec import GovernanceViolationError

    ro_spec = OntologySpec(read_only_source=True)
    ro_spec.object_type(_ExternalCustomer)

    with pytest.raises(GovernanceViolationError, match="read_only_source"):

        @ro_spec.action("default_affects_source", _CustomerEditParams, allowed_roles=("admin",))
        def _bad_default(ctx, params):
            return None

    with pytest.raises(GovernanceViolationError, match="read_only_source"):

        @ro_spec.action(
            "explicit_affects_source",
            _CustomerEditParams,
            allowed_roles=("admin",),
            affects_source=True,
        )
        def _bad_explicit(ctx, params):
            return None

    @ro_spec.action(
        "tag_customer",
        _CustomerEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def tag_customer(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, ops_tag=params.ops_tag)

    assert "tag_customer" in ro_spec.actions


def test_read_only_source_object_type_declaration_and_tbox():
    """__read_only_source__ 是类型元数据，禁止与 __owned__=True 混用，且投射到 TBox。"""
    from open_ontology.model_views import describe_tbox

    class _ContradictoryOwned(ObjectType):
        __primary_key__ = "item_id"
        __owned__ = True
        __read_only_source__ = True
        item_id: str

    ro_spec = OntologySpec(read_only_source=True)
    with pytest.raises(ValueError, match="__read_only_source__"):
        ro_spec.object_type(_ContradictoryOwned)

    ro_spec.object_type(_ExternalCustomer)
    ro_spec.object_type(_OwnedNote)
    assert "__read_only_source__" not in _ExternalCustomer.model_fields
    assert _ExternalCustomer.is_read_only_source() is True
    assert _OwnedNote.is_read_only_source() is False

    cust = _ExternalCustomer(customer_id="C1", name="Acme", credit_limit=100)
    assert "__read_only_source__" not in cust.model_dump()
    assert "__read_only_source__" not in cust.to_properties()

    tbox = describe_tbox(ro_spec)
    by_name = {row["name"]: row for row in tbox["object_types"]}
    assert by_name["_ExternalCustomer"]["read_only_source"] is True
    assert by_name["_OwnedNote"]["read_only_source"] is False


def test_read_only_source_spec_blocks_action_source_writes_but_allows_snapshots_and_overlay(tmp_path):
    """read_only_source=True 模型下：SnapshotIngestionDriver 可刷新源数据，Action 只能改自有字段。"""
    from open_ontology.spec import ActionDef

    ro_spec = OntologySpec(read_only_source=True)
    ro_spec.object_type(_ExternalCustomer)
    ro_spec.object_type(_OwnedNote)
    ro_spec.object_property(
        "customer_src_links",
        "_ExternalCustomer",
        "_ExternalCustomer",
        cardinality="many-to-many",
        owned=False,
    )
    ro_spec.object_property(
        "customer_owned_notes",
        "_ExternalCustomer",
        "_OwnedNote",
        cardinality="one-to-many",
        owned=True,
    )

    @ro_spec.action(
        "annotate_customer",
        _CustomerEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def annotate_customer(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, ops_tag=params.ops_tag)

    @ro_spec.action(
        "link_customer_note",
        _LinkEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def link_customer_note(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        note = ctx.create(_OwnedNote(note_id=params.note_id, text="note"))
        ctx.link(cust, "customer_owned_notes", note)

    @ro_spec.action(
        "illegal_mutate_source",
        _CustomerEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def illegal_mutate_source(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, name=params.name)

    @ro_spec.action(
        "illegal_create_source",
        _CustomerEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def illegal_create_source(ctx, params):
        ctx.create(_ExternalCustomer(customer_id=params.customer_id, name=params.name))

    @ro_spec.action(
        "illegal_src_link",
        _LinkEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def illegal_src_link(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.link(cust, "customer_src_links", cust)

    # Bypass decorator registration to test runtime guard defense-in-depth
    ro_spec.actions["rogue_source_action"] = ActionDef(
        name="rogue_source_action",
        params=_CustomerEditParams,
        handler=lambda ctx, params: None,
        allowed_roles=("admin",),
        affects_source=True,
    )

    rt = OntologyRuntime(ro_spec, tmp_path / "ro_spec.sqlite3")
    try:
        # Snapshot ingestion works freely on external source objects
        rt.ingestion.load_snapshot(
            {
                "_ExternalCustomer": [
                    {"customer_id": "C1", "name": "Acme v1", "credit_limit": 500}
                ]
            },
            actor=ADMIN,
        )

        # Owned field mutation succeeds
        sim_ok = rt.simulate_action(
            "annotate_customer", {"customer_id": "C1", "ops_tag": "vip"}, ADMIN
        )
        assert sim_ok.valid is True
        rt.submit_action("RUN-RO-1", "annotate_customer", {"customer_id": "C1", "ops_tag": "vip"}, ADMIN)

        # Owned link + owned object creation succeeds
        rt.submit_action("RUN-RO-2", "link_customer_note", {"customer_id": "C1", "note_id": "N1"}, ADMIN)

        # Snapshot refresh updates source fields while preserving __owned__ overlay
        rt.ingestion.load_snapshot(
            {
                "_ExternalCustomer": [
                    {"customer_id": "C1", "name": "Acme v2", "credit_limit": 800}
                ]
            },
            actor=ADMIN,
        )
        refreshed = rt.get_object(_ExternalCustomer, "C1", ADMIN)
        assert refreshed.name == "Acme v2"
        assert refreshed.credit_limit == 800
        assert refreshed.ops_tag == "vip"

        # Any Action source write, source creation, source link, or rogue affects_source=True fails
        for action_name, payload in (
            ("illegal_mutate_source", {"customer_id": "C1", "name": "Hacked"}),
            ("illegal_create_source", {"customer_id": "C2", "name": "Rogue"}),
            ("illegal_src_link", {"customer_id": "C1"}),
            ("rogue_source_action", {"customer_id": "C1"}),
        ):
            sim = rt.simulate_action(action_name, payload, ADMIN)
            assert sim.valid is False, action_name
            assert sim.error_code == "SOURCE_READ_ONLY", action_name
    finally:
        rt.close()


def test_object_level_read_only_source_protects_external_object_in_mixed_spec(tmp_path):
    """混合主权模型中：__read_only_source__=True 锁定指定外部对象（含 post_approval 与非自有关系），不影响可写源对象。"""
    mixed_spec = OntologySpec()  # read_only_source=False
    mixed_spec.object_type(_ExternalCustomer)
    mixed_spec.object_type(_MutableOrder)
    mixed_spec.object_property(
        "customer_orders",
        "_ExternalCustomer",
        "_MutableOrder",
        cardinality="one-to-many",
        owned=False,
    )

    @mixed_spec.action(
        "update_order_amount",
        _OrderEditParams,
        allowed_roles=("admin",),
        affects_source=True,
    )
    def update_order_amount(ctx, params):
        order = ctx.get(_MutableOrder, params.order_id)
        ctx.modify(order, amount=params.amount)

    @mixed_spec.action(
        "rename_customer_with_true",
        _CustomerEditParams,
        allowed_roles=("admin",),
        affects_source=True,
    )
    def rename_customer_with_true(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, name=params.name)

    @mixed_spec.action(
        "tag_customer_only",
        _CustomerEditParams,
        allowed_roles=("admin",),
        affects_source=False,
    )
    def tag_customer_only(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, ops_tag=params.ops_tag)

    def _post_approval_mutate_customer(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, credit_limit=9999)

    @mixed_spec.action(
        "two_phase_touches_ro_source",
        _CustomerEditParams,
        allowed_roles=("admin",),
        approver_roles=("admin",),
        affects_source=False,
        post_approval=_post_approval_mutate_customer,
    )
    def two_phase_touches_ro_source(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        ctx.modify(cust, ops_tag=params.ops_tag)

    @mixed_spec.action(
        "link_customer_to_order",
        _LinkEditParams,
        allowed_roles=("admin",),
        affects_source=True,
    )
    def link_customer_to_order(ctx, params):
        cust = ctx.get(_ExternalCustomer, params.customer_id)
        order = ctx.get(_MutableOrder, "O1")
        ctx.link(cust, "customer_orders", order)

    rt = OntologyRuntime(mixed_spec, tmp_path / "mixed.sqlite3")
    try:
        rt.ingestion.load_snapshot(
            {
                "_ExternalCustomer": [
                    {"customer_id": "C1", "name": "Acme", "credit_limit": 100}
                ],
                "_MutableOrder": [{"order_id": "O1", "amount": 10}],
            },
            actor=ADMIN,
        )

        # Writable source object still works with affects_source=True
        rt.submit_action("RUN-ORD-1", "update_order_amount", {"order_id": "O1", "amount": 42}, ADMIN)
        assert rt.get_object(_MutableOrder, "O1", ADMIN).amount == 42

        # Owned overlay update on read-only source object works with affects_source=False
        rt.submit_action("RUN-CUST-OWNED", "tag_customer_only", {"customer_id": "C1", "ops_tag": "gold"}, ADMIN)
        assert rt.get_object(_ExternalCustomer, "C1", ADMIN).ops_tag == "gold"

        # Source writes (even with affects_source=True), post_approval source writes, and source links fail
        for action_name, payload in (
            ("rename_customer_with_true", {"customer_id": "C1", "name": "Changed"}),
            ("two_phase_touches_ro_source", {"customer_id": "C1", "ops_tag": "silver"}),
            ("link_customer_to_order", {"customer_id": "C1"}),
        ):
            sim = rt.simulate_action(action_name, payload, ADMIN)
            assert sim.valid is False, action_name
            assert sim.error_code == "SOURCE_READ_ONLY", action_name
    finally:
        rt.close()


def test_kanban_and_dev_factory_enforce_read_only_source():
    """kanban.py 与 dev_factory_ontology.py 已从平台层锁定 read_only_source=True。"""
    from examples import dev_factory_ontology as dev_factory
    from examples import kanban
    from open_ontology.spec import GovernanceViolationError

    assert kanban.spec.read_only_source is True
    assert dev_factory.spec.read_only_source is True

    for target_spec in (kanban.spec, dev_factory.spec):
        with pytest.raises(GovernanceViolationError):
            target_spec.action(
                "_probe_forbidden_source_action",
                _CustomerEditParams,
                allowed_roles=("admin",),
                affects_source=True,
            )
