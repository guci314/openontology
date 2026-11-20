"""元自举 6 表治理本体（InductionGovernanceService · 纯 M2，领域无关）。

修复与保障：
* 结晶同步落盘 ``adapter_mapping_json``（支持 ``get_active_adapter_mapping``）；
* 演化结晶强制乐观锁校验 ``prop.base_version == head.active_version``（防并发覆盖）；
* ``FalsificationReport`` 绑定 ``blueprint_hash``，防止验证后篡改提案；
* ``ActiveModelHead`` 维护完整版本栈支持多步连续回滚，并自动停用被回滚版本的 ``GoldenEvalCase``。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from ..runtime import OntologyRuntime
from ..sandbox import EditBatch
from ..spec import (
    Actor,
    AutoID,
    InvariantError,
    ObjectNotFoundError,
    ObjectProperty,
    ObjectType,
    OntologySpec,
)
from .dsl import DeclarativeBlueprint, blueprint_fingerprint
from .phenomenon import AdapterMappingSpec

__all__ = [
    "BUILDER_ACTOR",
    "EVOLVER_ACTOR",
    "CRUCIBLE_ACTOR",
    "STEWARD_ACTOR",
    "ActiveModelHead",
    "FrictionCluster",
    "ModelProposal",
    "FalsificationReport",
    "GoldenEvalCase",
    "CrystallizedModelVersion",
    "build_induction_governance_spec",
    "InductionGovernanceService",
]

BUILDER_ACTOR = Actor(
    actor_id="agent:ontology-builder",
    role="ontology_builder",
    clearance_tags=frozenset({"*"}),
)
EVOLVER_ACTOR = Actor(
    actor_id="agent:ontology-evolver",
    role="ontology_evolver",
    clearance_tags=frozenset({"*"}),
)
CRUCIBLE_ACTOR = Actor(
    actor_id="system:symbolic-crucible",
    role="falsifier",
    clearance_tags=frozenset({"*"}),
)
STEWARD_ACTOR = Actor(
    actor_id="human:ontology-steward",
    role="steward",
    clearance_tags=frozenset({"*"}),
)


class ActiveModelHead(ObjectType):
    __primary_key__: ClassVar[str] = "model_id"
    __title__: ClassVar[str] = "model_id"
    __aggregate_root__: ClassVar[bool] = True
    __owned__: ClassVar[bool] = True

    model_id: str
    active_version: str = ""
    active_blueprint_json: str = "{}"
    active_adapter_mapping_json: str = "{}"
    #: Multi-step rollback stack: list of {version, proposal_id, blueprint_json, adapter_mapping_json}
    version_stack_json: str = "[]"
    #: Rolled-back versions whose GoldenEvalCases are deactivated
    inactive_versions_json: str = "[]"


class FrictionCluster(ObjectType):
    __primary_key__: ClassVar[str] = "cluster_id"
    __title__: ClassVar[str] = "signature"
    __aggregate_root__: ClassVar[bool] = True
    __owned__: ClassVar[bool] = True

    cluster_id: str
    model_id: str
    signal_kind: str
    action_type: str
    signature: str
    support_count: int = 1
    run_ids_json: str = "[]"
    status: str = "OPEN"  # OPEN | ASSIGNED | RESOLVED | DISMISSED


class ModelProposal(ObjectType):
    __primary_key__: ClassVar[str] = "proposal_id"
    __title__: ClassVar[str] = "title"
    __aggregate_root__: ClassVar[bool] = True
    __owned__: ClassVar[bool] = True

    proposal_id: AutoID
    model_id: str
    kind: Literal["genesis", "evolution"] = "genesis"
    base_version: str = ""
    target_version: str = "1.0.0"
    title: str = ""
    rationale: str = ""
    proposer_id: str = ""
    cluster_id: str = ""
    blueprint_json: str = "{}"
    adapter_mapping_json: str = "{}"
    patch_json: str = "{}"
    proposed_eval_cases_json: str = "[]"
    revision: int = 1
    status: str = "DRAFT"  # DRAFT | REFUTED | VERIFIED | AWAITING_APPROVAL | CRYSTALLIZED | REJECTED | ROLLED_BACK
    requested_by: str = ""
    approved_by: str = ""
    approval_note: str = ""
    rejection_reason: str = ""
    last_report_id: str = ""
    last_report_passed: bool = False
    last_report_blueprint_hash: str = ""


class FalsificationReport(ObjectType):
    __primary_key__: ClassVar[str] = "report_id"
    __title__: ClassVar[str] = "verdict"
    __owned__: ClassVar[bool] = True
    __append_only__: ClassVar[bool] = True

    report_id: AutoID
    proposal: ObjectProperty[ModelProposal]
    revision: int = 1
    passed: bool = False
    verdict: str = "REFUTED"
    blueprint_hash: str = ""
    report_json: str = "{}"


class GoldenEvalCase(ObjectType):
    __primary_key__: ClassVar[str] = "case_id"
    __title__: ClassVar[str] = "name"
    __owned__: ClassVar[bool] = True
    __append_only__: ClassVar[bool] = True

    case_id: AutoID
    model_id: str
    introduced_in_version: str
    name: str
    case_json: str


class CrystallizedModelVersion(ObjectType):
    __primary_key__: ClassVar[str] = "version_key"
    __title__: ClassVar[str] = "version_key"
    __owned__: ClassVar[bool] = True
    __append_only__: ClassVar[bool] = True

    version_key: str
    model_id: str
    version: str
    parent_version: str = ""
    proposal: ObjectProperty[ModelProposal]
    blueprint_json: str
    adapter_mapping_json: str = "{}"
    requested_by: str = ""


class _RecordFrictionParams(BaseModel):
    cluster_id: str
    model_id: str
    signal_kind: str
    action_type: str
    signature: str
    support_count: int
    run_ids_json: str = "[]"


class _ProposeGenesisParams(BaseModel):
    proposal_id: str
    model_id: str
    target_version: str = "1.0.0"
    title: str = ""
    rationale: str = ""
    blueprint_json: str
    adapter_mapping_json: str = "{}"
    proposed_eval_cases_json: str = "[]"


class _ProposeEvolutionParams(BaseModel):
    proposal_id: str
    model_id: str
    cluster_id: str = ""
    base_version: str
    target_version: str
    title: str = ""
    rationale: str = ""
    blueprint_json: str
    adapter_mapping_json: str = "{}"
    patch_json: str
    proposed_eval_cases_json: str = "[]"


class _ReviseProposalParams(BaseModel):
    proposal_id: str
    rationale: str = ""
    blueprint_json: str
    adapter_mapping_json: str = "{}"
    patch_json: str = "{}"
    proposed_eval_cases_json: str = "[]"


class _RecordFalsificationParams(BaseModel):
    report_id: str
    proposal_id: str
    passed: bool
    blueprint_hash: str = ""
    report_json: str


class _RequestCrystallizationParams(BaseModel):
    proposal_id: str
    approval_note: str = ""


class _ApproveCrystallizationParams(BaseModel):
    proposal_id: str
    approval_note: str = ""


class _RejectCrystallizationParams(BaseModel):
    proposal_id: str
    reason: str = "rejected by steward"


class _RollbackCrystallizationParams(BaseModel):
    proposal_id: str
    reason: str = "steward rollback"


def build_induction_governance_spec(*, allow_self_approval: bool = False) -> OntologySpec:
    """Build the pure synchronous M2 meta-governance OntologySpec for Builder/Evolver/Crucible/Steward."""
    spec = OntologySpec()
    for cls in (
        ActiveModelHead,
        FrictionCluster,
        ModelProposal,
        FalsificationReport,
        GoldenEvalCase,
        CrystallizedModelVersion,
    ):
        spec.object_type(cls)

    spec.display = {
        "objects": {
            "ActiveModelHead": "模型当前生效头指针",
            "FrictionCluster": "M0 摩擦证据簇",
            "ModelProposal": "M1 演进提案",
            "FalsificationReport": "符号证伪报告",
            "GoldenEvalCase": "黄金回归用例",
            "CrystallizedModelVersion": "结晶模型版本",
        },
        "actions": {
            "record_friction_cluster": "登记摩擦证据簇",
            "propose_genesis_model": "提议创世模型 (Builder)",
            "propose_evolution_patch": "提议演化补丁 (Evolver)",
            "revise_proposal": "依据反例修订提案",
            "record_falsification_report": "签发符号证伪报告 (Crucible)",
            "request_crystallization": "申请结晶待批 (Builder/Evolver)",
            "approve_crystallization": "批准并同步结晶新版本 (Steward)",
            "reject_crystallization": "驳回结晶提案 (Steward)",
            "rollback_crystallization": "回退结晶版本 (Steward)",
        },
    }

    @spec.action(
        "record_friction_cluster",
        _RecordFrictionParams,
        allowed_roles=("falsifier", "steward"),
        affects_source=False,
        description="登记经聚类去噪后的 M0 生产摩擦证据簇",
    )
    def _record_friction(ctx: EditBatch, params: _RecordFrictionParams) -> None:
        existing = ctx.get(FrictionCluster, params.cluster_id)
        if existing is not None:
            ctx.modify(
                existing,
                support_count=params.support_count,
                run_ids_json=params.run_ids_json,
                status="OPEN",
            )
            return
        ctx.create(
            FrictionCluster(
                cluster_id=params.cluster_id,
                model_id=params.model_id,
                signal_kind=params.signal_kind,
                action_type=params.action_type,
                signature=params.signature,
                support_count=params.support_count,
                run_ids_json=params.run_ids_json,
                status="OPEN",
            )
        )

    @spec.action(
        "propose_genesis_model",
        _ProposeGenesisParams,
        allowed_roles=("ontology_builder",),
        affects_source=False,
        description="由 Ontology Builder 提交 0->1 创世候选蓝图",
    )
    def _propose_genesis(ctx: EditBatch, params: _ProposeGenesisParams) -> None:
        if ctx.get(ModelProposal, params.proposal_id) is not None:
            raise InvariantError(
                f"proposal {params.proposal_id!r} already exists",
                code="PROPOSAL_EXISTS",
            )
        ctx.create(
            ModelProposal(
                proposal_id=params.proposal_id,
                model_id=params.model_id,
                kind="genesis",
                base_version="",
                target_version=params.target_version,
                title=params.title or f"Genesis {params.model_id} v{params.target_version}",
                rationale=params.rationale,
                proposer_id=ctx.actor.actor_id,
                blueprint_json=params.blueprint_json,
                adapter_mapping_json=params.adapter_mapping_json,
                proposed_eval_cases_json=params.proposed_eval_cases_json,
                revision=1,
                status="DRAFT",
            )
        )

    @spec.action(
        "propose_evolution_patch",
        _ProposeEvolutionParams,
        allowed_roles=("ontology_evolver",),
        affects_source=False,
        description="由 Ontology Evolver 提交 1->N 演化差异补丁提案",
    )
    def _propose_evolution(ctx: EditBatch, params: _ProposeEvolutionParams) -> None:
        if ctx.get(ModelProposal, params.proposal_id) is not None:
            raise InvariantError(
                f"proposal {params.proposal_id!r} already exists",
                code="PROPOSAL_EXISTS",
            )
        if params.cluster_id:
            cluster = ctx.get(FrictionCluster, params.cluster_id)
            if cluster is not None:
                ctx.modify(cluster, status="ASSIGNED")
        ctx.create(
            ModelProposal(
                proposal_id=params.proposal_id,
                model_id=params.model_id,
                kind="evolution",
                base_version=params.base_version,
                target_version=params.target_version,
                title=params.title or f"Evolve {params.model_id} {params.base_version}->{params.target_version}",
                rationale=params.rationale,
                proposer_id=ctx.actor.actor_id,
                cluster_id=params.cluster_id,
                blueprint_json=params.blueprint_json,
                adapter_mapping_json=params.adapter_mapping_json,
                patch_json=params.patch_json,
                proposed_eval_cases_json=params.proposed_eval_cases_json,
                revision=1,
                status="DRAFT",
            )
        )

    @spec.action(
        "revise_proposal",
        _ReviseProposalParams,
        allowed_roles=("ontology_builder", "ontology_evolver"),
        affects_source=False,
        description="根据 Symbolic Crucible 反例修订被拒提案",
    )
    def _revise_proposal(ctx: EditBatch, params: _ReviseProposalParams) -> None:
        prop = ctx.require(ModelProposal, params.proposal_id)
        if prop.status not in ("DRAFT", "REFUTED"):
            raise InvariantError(
                f"proposal {prop.proposal_id!r} in status {prop.status!r} cannot be revised",
                code="INVALID_PROPOSAL_STATE",
            )
        ctx.modify(
            prop,
            rationale=params.rationale or prop.rationale,
            blueprint_json=params.blueprint_json,
            adapter_mapping_json=params.adapter_mapping_json or prop.adapter_mapping_json,
            patch_json=params.patch_json,
            proposed_eval_cases_json=params.proposed_eval_cases_json,
            revision=prop.revision + 1,
            status="DRAFT",
            last_report_passed=False,
            last_report_blueprint_hash="",
        )

    @spec.action(
        "record_falsification_report",
        _RecordFalsificationParams,
        allowed_roles=("falsifier",),
        affects_source=False,
        description="由非拟人化 Symbolic Crucible 签发绑定 blueprint_hash 的不可篡改证伪报告",
    )
    def _record_report(ctx: EditBatch, params: _RecordFalsificationParams) -> None:
        prop = ctx.require(ModelProposal, params.proposal_id)
        if prop.status not in ("DRAFT", "REFUTED"):
            raise InvariantError(
                f"proposal {prop.proposal_id!r} status {prop.status!r} is not eligible for falsification",
                code="INVALID_PROPOSAL_STATE",
            )
        actual_hash = params.blueprint_hash
        if not actual_hash and prop.blueprint_json and prop.blueprint_json != "{}":
            try:
                actual_hash = blueprint_fingerprint(prop.blueprint_json)
            except Exception:
                actual_hash = ""
        verdict = "VERIFIED" if params.passed else "REFUTED"
        ctx.create(
            FalsificationReport(
                report_id=params.report_id,
                proposal=prop.proposal_id,
                revision=prop.revision,
                passed=params.passed,
                verdict=verdict,
                blueprint_hash=actual_hash,
                report_json=params.report_json,
            )
        )
        ctx.modify(
            prop,
            status=verdict,
            last_report_id=params.report_id,
            last_report_passed=params.passed,
            last_report_blueprint_hash=actual_hash,
        )

    def _assert_optimistic_base(ctx: EditBatch, prop: ModelProposal) -> ActiveModelHead | None:
        head = ctx.get(ActiveModelHead, prop.model_id)
        if prop.kind == "evolution":
            if head is None or not head.active_version:
                raise InvariantError(
                    f"evolution proposal {prop.proposal_id!r} requires an active model head",
                    code="NO_ACTIVE_MODEL",
                )
            if prop.base_version != head.active_version:
                raise InvariantError(
                    f"stale evolution base_version {prop.base_version!r}; "
                    f"current active_version is {head.active_version!r}",
                    code="STALE_BASE_VERSION",
                )
        return head

    def _assert_version_available(ctx: EditBatch, prop: ModelProposal) -> str:
        version_key = f"{prop.model_id}@{prop.target_version}"
        if ctx.get(CrystallizedModelVersion, version_key) is not None:
            raise InvariantError(
                f"version {version_key!r} is already crystallized",
                code="VERSION_ALREADY_CRYSTALLIZED",
            )
        return version_key

    def _assert_verified_integrity(prop: ModelProposal) -> None:
        if prop.status in ("DRAFT", "REFUTED") or not prop.last_report_passed:
            raise InvariantError(
                f"proposal {prop.proposal_id!r} has not passed Symbolic Crucible verification",
                code="UNVERIFIED_PROPOSAL",
            )
        expected_hash = blueprint_fingerprint(prop.blueprint_json)
        if prop.last_report_blueprint_hash and prop.last_report_blueprint_hash != expected_hash:
            raise InvariantError(
                f"proposal {prop.proposal_id!r} blueprint hash changed after falsification",
                code="BLUEPRINT_HASH_MISMATCH",
            )

    @spec.action(
        "request_crystallization",
        _RequestCrystallizationParams,
        allowed_roles=("ontology_builder", "ontology_evolver", "steward"),
        affects_source=False,
        description="将五关全绿的提案同步推进为 AWAITING_APPROVAL 等待 Steward t1 签批",
    )
    def _request_crystallization(ctx: EditBatch, params: _RequestCrystallizationParams) -> None:
        prop = ctx.require(ModelProposal, params.proposal_id)
        if prop.status != "VERIFIED" or not prop.last_report_passed:
            raise InvariantError(
                f"proposal {prop.proposal_id!r} has not passed Symbolic Crucible verification",
                code="UNVERIFIED_PROPOSAL",
            )
        _assert_verified_integrity(prop)
        _assert_optimistic_base(ctx, prop)
        _assert_version_available(ctx, prop)
        ctx.modify(
            prop,
            status="AWAITING_APPROVAL",
            requested_by=ctx.actor.actor_id,
            approval_note=params.approval_note,
        )

    @spec.action(
        "approve_crystallization",
        _ApproveCrystallizationParams,
        allowed_roles=("steward",),
        affects_source=False,
        description="Steward 在 t1 现场重算基线与哈希并同步结晶新模型版本（无跨期 diff 队列）",
    )
    def _approve_crystallization(ctx: EditBatch, params: _ApproveCrystallizationParams) -> None:
        prop = ctx.require(ModelProposal, params.proposal_id)
        _assert_verified_integrity(prop)
        if prop.status != "AWAITING_APPROVAL":
            raise InvariantError(
                f"proposal {prop.proposal_id!r} status {prop.status!r} is not AWAITING_APPROVAL",
                code="PROPOSAL_NOT_AWAITING_APPROVAL",
            )
        if not allow_self_approval and prop.requested_by and ctx.actor.actor_id == prop.requested_by:
            raise InvariantError(
                f"SoD violation: requester {prop.requested_by!r} cannot self-approve proposal {prop.proposal_id!r}",
                code="SOD_SELF_APPROVAL_FORBIDDEN",
            )

        head = _assert_optimistic_base(ctx, prop)
        version_key = _assert_version_available(ctx, prop)
        parent_ver = head.active_version if head is not None else ""
        # Inherit previous adapter_mapping_json if evolution patch did not replace it
        effective_mapping = prop.adapter_mapping_json
        if (not effective_mapping or effective_mapping == "{}") and head is not None:
            effective_mapping = head.active_adapter_mapping_json

        ctx.create(
            CrystallizedModelVersion(
                version_key=version_key,
                model_id=prop.model_id,
                version=prop.target_version,
                parent_version=parent_ver,
                proposal=prop.proposal_id,
                blueprint_json=prop.blueprint_json,
                adapter_mapping_json=effective_mapping,
                requested_by=prop.requested_by or prop.proposer_id or ctx.actor.actor_id,
            )
        )
        new_entry = {
            "version": prop.target_version,
            "proposal_id": prop.proposal_id,
            "blueprint_json": prop.blueprint_json,
            "adapter_mapping_json": effective_mapping,
        }
        if head is None:
            ctx.create(
                ActiveModelHead(
                    model_id=prop.model_id,
                    active_version=prop.target_version,
                    active_blueprint_json=prop.blueprint_json,
                    active_adapter_mapping_json=effective_mapping,
                    version_stack_json=json.dumps([new_entry], ensure_ascii=False),
                    inactive_versions_json="[]",
                )
            )
        else:
            stack = json.loads(head.version_stack_json or "[]")
            stack.append(new_entry)
            inactive = [v for v in json.loads(head.inactive_versions_json or "[]") if v != prop.target_version]
            ctx.modify(
                head,
                active_version=prop.target_version,
                active_blueprint_json=prop.blueprint_json,
                active_adapter_mapping_json=effective_mapping,
                version_stack_json=json.dumps(stack, ensure_ascii=False),
                inactive_versions_json=json.dumps(inactive, ensure_ascii=False),
            )

        if prop.cluster_id:
            cluster = ctx.get(FrictionCluster, prop.cluster_id)
            if cluster is not None:
                ctx.modify(cluster, status="RESOLVED")

        eval_cases = json.loads(prop.proposed_eval_cases_json or "[]")
        for idx, case in enumerate(eval_cases, start=1):
            cname = str(case.get("name") or f"case-{idx}")
            cid = f"GEC-{prop.model_id}-{prop.target_version}-{idx:03d}"
            if ctx.get(GoldenEvalCase, cid) is None:
                ctx.create(
                    GoldenEvalCase(
                        case_id=cid,
                        model_id=prop.model_id,
                        introduced_in_version=prop.target_version,
                        name=cname,
                        case_json=json.dumps(case, ensure_ascii=False, sort_keys=True),
                    )
                )

        ctx.modify(
            prop,
            status="CRYSTALLIZED",
            approved_by=ctx.actor.actor_id,
            approval_note=params.approval_note or prop.approval_note,
        )

    @spec.action(
        "reject_crystallization",
        _RejectCrystallizationParams,
        allowed_roles=("steward",),
        affects_source=False,
        description="Steward 同步驳回处于 AWAITING_APPROVAL 状态的演进提案",
    )
    def _reject_crystallization(ctx: EditBatch, params: _RejectCrystallizationParams) -> None:
        prop = ctx.require(ModelProposal, params.proposal_id)
        if prop.status != "AWAITING_APPROVAL":
            raise InvariantError(
                f"proposal {prop.proposal_id!r} status {prop.status!r} is not AWAITING_APPROVAL",
                code="PROPOSAL_NOT_AWAITING_APPROVAL",
            )
        if not allow_self_approval and prop.requested_by and ctx.actor.actor_id == prop.requested_by:
            raise InvariantError(
                f"SoD violation: requester {prop.requested_by!r} cannot self-reject proposal {prop.proposal_id!r}",
                code="SOD_SELF_APPROVAL_FORBIDDEN",
            )
        ctx.modify(
            prop,
            status="REJECTED",
            approved_by=ctx.actor.actor_id,
            rejection_reason=params.reason,
        )

    @spec.action(
        "rollback_crystallization",
        _RollbackCrystallizationParams,
        allowed_roles=("steward",),
        affects_source=False,
        description="Steward 将已结晶版本回退至栈中上一稳定版本，并停用该版本新增的黄金回归用例",
    )
    def _rollback(ctx: EditBatch, params: _RollbackCrystallizationParams) -> None:
        prop = ctx.require(ModelProposal, params.proposal_id)
        if prop.status != "CRYSTALLIZED":
            raise InvariantError(
                f"proposal {prop.proposal_id!r} is not CRYSTALLIZED",
                code="NOT_CRYSTALLIZED",
            )
        head = ctx.require(ActiveModelHead, prop.model_id)
        stack = json.loads(head.version_stack_json or "[]")
        if len(stack) < 2:
            raise InvariantError(
                f"model {prop.model_id!r} has no previous version to roll back to",
                code="NO_PREVIOUS_VERSION",
            )
        if stack[-1].get("version") != prop.target_version:
            raise InvariantError(
                f"can only roll back active top version {stack[-1].get('version')!r}, not {prop.target_version!r}",
                code="NOT_TOP_VERSION",
            )
        popped = stack.pop()
        prev = stack[-1]
        inactive = list(json.loads(head.inactive_versions_json or "[]"))
        if popped["version"] not in inactive:
            inactive.append(popped["version"])

        ctx.modify(prop, status="ROLLED_BACK")
        ctx.modify(
            head,
            active_version=prev["version"],
            active_blueprint_json=prev["blueprint_json"],
            active_adapter_mapping_json=prev.get("adapter_mapping_json", "{}"),
            version_stack_json=json.dumps(stack, ensure_ascii=False),
            inactive_versions_json=json.dumps(inactive, ensure_ascii=False),
        )

    return spec


class InductionGovernanceService:
    """Meta-bootstrapped 6-table pure-sync governance runtime for Builder/Evolver/Crucible/Steward."""

    def __init__(self, db_path: str = ":memory:", *, allow_self_approval: bool = False) -> None:
        self.spec = build_induction_governance_spec(allow_self_approval=allow_self_approval)
        self.runtime = OntologyRuntime(self.spec, db_path=db_path)
        self._run_to_proposal: dict[str, str] = {}

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> "InductionGovernanceService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def get_active_head(self, model_id: str) -> ActiveModelHead | None:
        try:
            return self.runtime.get_object(ActiveModelHead, model_id, STEWARD_ACTOR)
        except ObjectNotFoundError:
            return None

    def get_active_blueprint(self, model_id: str) -> DeclarativeBlueprint | None:
        head = self.get_active_head(model_id)
        if head is None or not head.active_version:
            return None
        return DeclarativeBlueprint.model_validate_json(head.active_blueprint_json)

    def get_active_adapter_mapping(self, model_id: str) -> AdapterMappingSpec | None:
        head = self.get_active_head(model_id)
        if head is None or not head.active_adapter_mapping_json or head.active_adapter_mapping_json == "{}":
            return None
        return AdapterMappingSpec.model_validate_json(head.active_adapter_mapping_json)

    def get_golden_eval_cases(self, model_id: str) -> list[dict[str, Any]]:
        head = self.get_active_head(model_id)
        inactive = set(json.loads(head.inactive_versions_json or "[]")) if head is not None else set()
        rows = self.runtime.query_objects(GoldenEvalCase, STEWARD_ACTOR, filters={"model_id": model_id})
        return [
            json.loads(r.case_json)
            for r in rows
            if r.introduced_in_version not in inactive
        ]

    def list_proposals(self, model_id: str | None = None) -> list[ModelProposal]:
        filters = {"model_id": model_id} if model_id else None
        return self.runtime.query_objects(ModelProposal, STEWARD_ACTOR, filters=filters)

    def list_pending_crystallizations(self, model_id: str | None = None) -> list[ModelProposal]:
        filters: dict[str, Any] = {"status": "AWAITING_APPROVAL"}
        if model_id:
            filters["model_id"] = model_id
        return self.runtime.query_objects(ModelProposal, STEWARD_ACTOR, filters=filters)

    def list_reports(self, proposal_id: str | None = None) -> list[FalsificationReport]:
        filters = {"proposal": proposal_id} if proposal_id else None
        return self.runtime.query_objects(FalsificationReport, STEWARD_ACTOR, filters=filters)

    def _resolve_proposal_id(self, proposal_or_run_id: str) -> str:
        if proposal_or_run_id in self._run_to_proposal:
            return self._run_to_proposal[proposal_or_run_id]
        try:
            self.runtime.get_object(ModelProposal, proposal_or_run_id, STEWARD_ACTOR)
            return proposal_or_run_id
        except ObjectNotFoundError:
            pass
        run = self.runtime.get_action_run(proposal_or_run_id)
        if run is not None:
            pid = (run.get("params") or {}).get("proposal_id")
            if isinstance(pid, str) and pid:
                return pid
        if proposal_or_run_id.startswith("CRYSTAL-"):
            cand = proposal_or_run_id[len("CRYSTAL-") :]
            try:
                self.runtime.get_object(ModelProposal, cand, STEWARD_ACTOR)
                return cand
            except ObjectNotFoundError:
                pass
        return proposal_or_run_id

    def request_crystallization(
        self,
        proposal_id: str,
        actor: Actor = BUILDER_ACTOR,
        *,
        approval_note: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        rid = run_id or f"REQ-CRYSTAL-{proposal_id}-{uuid.uuid4().hex[:6]}"
        res = self.runtime.submit_action(
            rid,
            "request_crystallization",
            {"proposal_id": proposal_id, "approval_note": approval_note},
            actor,
        )
        self._run_to_proposal[rid] = proposal_id
        out = dict(res)
        out["proposal_id"] = proposal_id
        return out

    def approve_crystallization(
        self,
        approval_run_id: str,
        approver: Actor = STEWARD_ACTOR,
        *,
        approval_note: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        proposal_id = self._resolve_proposal_id(approval_run_id)
        rid = run_id or f"APPROVE-{proposal_id}-{uuid.uuid4().hex[:6]}"
        res = self.runtime.submit_action(
            rid,
            "approve_crystallization",
            {"proposal_id": proposal_id, "approval_note": approval_note},
            approver,
        )
        out = dict(res)
        out["approved_by"] = approver.actor_id
        out["proposal_id"] = proposal_id
        return out

    def reject_crystallization(
        self,
        proposal_or_run_id: str,
        steward: Actor = STEWARD_ACTOR,
        *,
        reason: str = "rejected by steward",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        proposal_id = self._resolve_proposal_id(proposal_or_run_id)
        rid = run_id or f"REJECT-{proposal_id}-{uuid.uuid4().hex[:6]}"
        res = self.runtime.submit_action(
            rid,
            "reject_crystallization",
            {"proposal_id": proposal_id, "reason": reason},
            steward,
        )
        out = dict(res)
        out["approved_by"] = steward.actor_id
        out["proposal_id"] = proposal_id
        return out

    def rollback_crystallization(
        self,
        proposal_id: str,
        *,
        run_id: str | None = None,
        steward: Actor = STEWARD_ACTOR,
        reason: str = "steward rollback",
    ) -> dict[str, Any]:
        return self.runtime.submit_action(
            run_id or f"ROLLBACK-{proposal_id}-{uuid.uuid4().hex[:6]}",
            "rollback_crystallization",
            {"proposal_id": proposal_id, "reason": reason},
            steward,
        )
