"""企业标准世界模型统一总装入口与 M1→M2 投影桥（WO-24B）。

严守常识细腰架构与 Gate 7 隔离规范：
1. 本模块故意命名为 ``std_enterprise_world_model.py``（而非 ``std_*_ontology.py``），
   作为六大纯 M1 领域本体（L2O/CRM、O2C、P2P、P2M、R2R、H2R）的上层总装入口与桥接器，
   不反向耦合任何单个领域模块；
2. 暴露统一总装接口：
   * ``build_enterprise_standard_blueprint`` —— 幂等菱形合并 + 只增不破 ``CustomerOverlaySpec`` 叠加；
   * ``build_enterprise_standard_spec`` —— 基于 Python ``OntologySpec(extends=...)`` 组合运行期契约；
   * ``build_org_rls_enterprise_spec`` —— 基于标准蓝图叠加 ``org`` 行级安全标签字段（零改写单域类）；
   * ``open_standalone_enterprise_world`` —— 一键打开 ``world_mode="standalone"`` 运行时（零外部 ERP）；
3. 暴露只读 M1(H2R) -> M2(``open_ontology.org_authority``) 投影桥：
   * ``project_h2r_to_org_directory`` —— 将在编在岗 ``Department / JobPosition / Employee / AuthorityMandate``
     只读投影为 ``OrgDirectory``，严格过滤 ``archived`` 部门及祖先与 ``offboarded / suspended / onboarding`` 员工；
   * ``build_h2r_tiered_authority_policy`` / ``evaluate_h2r_employee_authority`` / ``verify_h2r_approver_authority`` ——
     综合基础岗位上限与有效 ``active`` 动态委派（自动剔除离职授权人/被授权人与已吊销委派），无缝对接分级审批与 SoD；
4. 暴露跨闭环事件 -> R2R 总账受控过账辅助：
   * ``project_event_to_draft_journal`` / ``project_and_post_event_journal`` ——
     从已确认 ``FulfillmentEvent``（P2P 收货/付款、P2M 领料/完工、O2C 出库/收款）生成绑定 ``source_event``
     的 ``JournalEntry`` 并严格经由 ``post_journal_entry`` 动作通道过账，绝不旁路写库。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from open_ontology import Actor, InvariantError, OntologyRuntime, OntologySpec
from open_ontology.induction import (
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    FieldSpec,
    apply_overlay,
    apply_overlay_to_spec,
    compile_blueprint,
    compose_blueprints,
)
from open_ontology.spec import (
    EditFunctionDef,
    FunctionDef,
    InvariantDef,
    TransitionInvariantDef,
)
from open_ontology.org_authority import (
    AUTHORITY_TIER_INSUFFICIENT,
    ORG_MEMBER_INACTIVE,
    ORG_MEMBER_NOT_FOUND,
    AuthorityEvaluation,
    AuthorityTier,
    OrgAuthorityError,
    OrgDirectory,
    OrgMember,
    OrgUnit,
    TieredAuthorityPolicy,
    evaluate_action_authority,
    verify_approver_authority,
)

try:
    from .business_ontology import BUSINESS_SEED_ONTOLOGY
    from .std_core_ontology import STD_ROLES, build_std_core_blueprint, spec as std_core_spec
    from .std_crm_ontology import build_std_crm_blueprint, spec as std_crm_spec
    from .std_h2r_ontology import build_std_h2r_blueprint, spec as std_h2r_spec
    from .std_o2c_ontology import build_std_o2c_blueprint, spec as std_o2c_spec
    from .std_p2m_ontology import build_std_p2m_blueprint, spec as std_p2m_spec
    from .std_p2p_ontology import build_std_p2p_blueprint, spec as std_p2p_spec
    from .std_r2r_ontology import build_std_r2r_blueprint, spec as std_r2r_spec
except ImportError:  # pragma: no cover - script execution fallback
    from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
    from examples.std_core_ontology import STD_ROLES, build_std_core_blueprint, spec as std_core_spec
    from examples.std_crm_ontology import build_std_crm_blueprint, spec as std_crm_spec
    from examples.std_h2r_ontology import build_std_h2r_blueprint, spec as std_h2r_spec
    from examples.std_o2c_ontology import build_std_o2c_blueprint, spec as std_o2c_spec
    from examples.std_p2m_ontology import build_std_p2m_blueprint, spec as std_p2m_spec
    from examples.std_p2p_ontology import build_std_p2p_blueprint, spec as std_p2p_spec
    from examples.std_r2r_ontology import build_std_r2r_blueprint, spec as std_r2r_spec

__all__ = [
    "ENTERPRISE_DOMAIN_KEYS",
    "DEFAULT_ENTERPRISE_ROLE_RANK",
    "H2rAuthorityDecision",
    "normalize_domain_keys",
    "normalize_org_scope_tag",
    "build_enterprise_standard_blueprint",
    "build_enterprise_standard_spec",
    "build_org_rls_enterprise_spec",
    "open_standalone_enterprise_world",
    "project_h2r_to_org_directory",
    "build_h2r_tiered_authority_policy",
    "evaluate_h2r_employee_authority",
    "verify_h2r_approver_authority",
    "project_event_to_draft_journal",
    "project_and_post_event_journal",
]

ENTERPRISE_DOMAIN_KEYS: tuple[str, ...] = ("crm", "o2c", "p2p", "p2m", "r2r", "h2r")

_DOMAIN_SPECS: dict[str, OntologySpec] = {
    "crm": std_crm_spec,
    "o2c": std_o2c_spec,
    "p2p": std_p2p_spec,
    "p2m": std_p2m_spec,
    "r2r": std_r2r_spec,
    "h2r": std_h2r_spec,
}

_DOMAIN_BP_BUILDERS: dict[str, Any] = {
    "crm": build_std_crm_blueprint,
    "o2c": build_std_o2c_blueprint,
    "p2p": build_std_p2p_blueprint,
    "p2m": build_std_p2m_blueprint,
    "r2r": build_std_r2r_blueprint,
    "h2r": build_std_h2r_blueprint,
}

DEFAULT_ENTERPRISE_ROLE_RANK: dict[str, int] = {
    "system": 0,
    "ai_agent": 5,
    "shopfloor": 10,
    "buyer": 10,
    "sales_rep": 10,
    "warehouse": 10,
    "finance": 10,
    "support_agent": 10,
    "hr_admin": 10,
    "production_planner": 10,
    "ops": 20,
    "chief_accountant": 40,
    "plant_manager": 40,
    "procurement_manager": 40,
    "sales_manager": 40,
    "steward": 60,
    "finance_director": 80,
    "hr_director": 80,
    "admin": 1000,
}


class H2rAuthorityDecision(BaseModel):
    """Combined H2R + M2 ``OrgDirectory`` authority routing summary for one employee."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    employee_id: str
    user_id: str
    unit_id: str
    target_unit_id: str
    base_role: str
    required_role: str | None = None
    amount_cents: int
    base_ceiling_cents: int
    mandate_ceiling_cents: int
    effective_ceiling_cents: int
    decision: Literal["AUTO", "ESCALATE"]
    tier_evaluation: AuthorityEvaluation


def normalize_domain_keys(domains: Sequence[str] | None = None) -> tuple[str, ...]:
    """Normalize short keys or module stems (e.g. ``'crm'`` / ``'std_crm_ontology'``)."""
    if domains is None:
        return ENTERPRISE_DOMAIN_KEYS
    selected: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        cleaned = str(raw or "").strip().lower()
        if cleaned in ("std_core", "std_core_ontology", "core"):
            continue
        if cleaned.startswith("std_") and cleaned.endswith("_ontology"):
            cleaned = cleaned[len("std_") : -len("_ontology")]
        if cleaned not in _DOMAIN_SPECS:
            raise InvariantError(
                f"unknown enterprise domain {raw!r}; expected one of {ENTERPRISE_DOMAIN_KEYS}",
                code="UNKNOWN_ENTERPRISE_DOMAIN",
            )
        if cleaned not in seen:
            seen.add(cleaned)
            selected.append(cleaned)
    return tuple(selected)


def normalize_org_scope_tag(raw_tag: str) -> str:
    """Normalize department/mandate ``scope_tag`` into M2 ``namespace:value`` clearance tag."""
    cleaned = str(raw_tag or "").strip()
    if not cleaned:
        return ""
    return cleaned if ":" in cleaned else f"org:{cleaned}"


def build_enterprise_standard_blueprint(
    *,
    domains: Sequence[str] | None = None,
    overlays: Sequence[CustomerOverlaySpec | Mapping[str, Any]] = (),
    model_id: str = "std_enterprise_world_model",
    version: str = "1.0.0",
) -> DeclarativeBlueprint:
    """幂等菱形合并标准核心底座与选定领域蓝图，并按序只增不破叠加 ``CustomerOverlay``。"""
    keys = normalize_domain_keys(domains)
    sources: list[DeclarativeBlueprint] = [build_std_core_blueprint()]
    for key in keys:
        sources.append(_DOMAIN_BP_BUILDERS[key]())

    composed = compose_blueprints(
        sources,
        model_id=model_id,
        version=version,
        seed_ontology_id="business_ontology",
        description="企业标准世界模型统一总装蓝图（常识细腰核心 + 选定企业闭环领域）。",
    )
    effective = composed
    for ov in overlays:
        ov_spec = (
            ov
            if isinstance(ov, CustomerOverlaySpec)
            else CustomerOverlaySpec.model_validate(dict(ov))
        )
        effective = apply_overlay(effective, ov_spec)
    return effective


def build_enterprise_standard_spec(
    *,
    domains: Sequence[str] | None = None,
) -> OntologySpec:
    """组合运行期标准世界模型 Python ``OntologySpec``（支持全量六域与任意领域子集）。"""
    keys = normalize_domain_keys(domains)
    parents = (std_core_spec, *(_DOMAIN_SPECS[k] for k in keys))
    unified = OntologySpec(extends=parents)
    for role_name, role_desc in STD_ROLES:
        if role_name not in unified.roles:
            unified.role(role_name, role_desc)
    unified.update_display(
        {
            "ontologies": {
                "std_enterprise_world_model": (
                    f"企业标准世界模型统一总装（核心底座 + {', '.join(k.upper() for k in keys)}）"
                )
            }
        }
    )
    return unified


def build_org_rls_enterprise_spec(
    *,
    domains: Sequence[str] | None = None,
    overlays: Sequence[CustomerOverlaySpec | Mapping[str, Any]] = (),
    rls_object_types: Sequence[str] = ("P2pPurchaseOrder", "WorkOrder", "O2cOrder"),
    model_id: str = "std_enterprise_org_rls_world",
    custom_edit_functions: Sequence[EditFunctionDef] | Mapping[str, EditFunctionDef] = (),
    custom_functions: Sequence[FunctionDef] | Mapping[str, FunctionDef] = (),
    custom_invariants: Sequence[InvariantDef | TransitionInvariantDef]
    | Mapping[str, InvariantDef | TransitionInvariantDef] = (),
) -> OntologySpec:
    """在标准总装 ``OntologySpec`` 上单一源码只增不破叠加插件、Overlay 与 ``org`` 行级安全标签。"""
    keys = normalize_domain_keys(domains)
    spec = build_enterprise_standard_spec(domains=keys)
    spec.update_display(
        {
            "ontologies": {
                model_id: (
                    f"企业标准世界模型统一总装（核心底座 + {', '.join(k.upper() for k in keys)}）"
                )
            }
        }
    )

    normalized_overlays = [
        ov if isinstance(ov, CustomerOverlaySpec) else CustomerOverlaySpec.model_validate(dict(ov))
        for ov in overlays
    ]
    if normalized_overlays:
        for idx, ov_spec in enumerate(normalized_overlays):
            spec = apply_overlay_to_spec(
                spec,
                ov_spec,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
                custom_edit_functions=custom_edit_functions if idx == 0 else (),
                custom_functions=custom_functions if idx == 0 else (),
                custom_invariants=custom_invariants if idx == 0 else (),
            )
    elif custom_edit_functions or custom_functions or custom_invariants:
        spec = apply_overlay_to_spec(
            spec,
            CustomerOverlaySpec(
                overlay_id=f"{model_id}_custom_plugin_overlay",
                base_model_id=model_id,
            ),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            custom_edit_functions=custom_edit_functions,
            custom_functions=custom_functions,
            custom_invariants=custom_invariants,
        )

    eff_rls = [ot_name for ot_name in rls_object_types if ot_name in spec.object_types]
    if eff_rls:
        spec = apply_overlay_to_spec(
            spec,
            CustomerOverlaySpec(
                overlay_id=f"{model_id}_org_rls_overlay",
                base_model_id=model_id,
            ),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            rls_object_types=eff_rls,
            security_tag_field="org",
        )
    return spec


def open_standalone_enterprise_world(
    db_path: str = ":memory:",
    *,
    domains: Sequence[str] | None = None,
) -> OntologyRuntime:
    """开箱打开零外部 ERP 依赖的 ``world_mode='standalone'`` 企业标准世界模型运行时。"""
    spec = build_enterprise_standard_spec(domains=domains)
    return OntologyRuntime(spec, db_path=db_path, world_mode="standalone")


def _department_chain_has_inactive_ancestor(
    dept: Any,
    all_depts: Mapping[str, Any],
) -> bool:
    seen: set[str] = set()
    cur_pid = getattr(dept, "parent_department", None)
    while cur_pid:
        pid = str(cur_pid)
        if pid in seen:
            return False  # cycle is left for OrgDirectory.validate() to catch deterministically
        seen.add(pid)
        parent = all_depts.get(pid)
        if parent is None:
            return False  # missing parent is left for OrgDirectory.validate()
        if parent.status != "active":
            return True
        cur_pid = getattr(parent, "parent_department", None)
    return False


def _load_active_h2r_snapshot(
    runtime: OntologyRuntime,
    actor: Actor,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[Any]]:
    all_depts = {d.department_id: d for d in runtime.query_objects("Department", actor)}
    all_positions = {p.position_id: p for p in runtime.query_objects("JobPosition", actor)}
    all_employees = {e.employee_id: e for e in runtime.query_objects("Employee", actor)}
    all_mandates = list(runtime.query_objects("AuthorityMandate", actor))

    active_depts = {
        did: d
        for did, d in all_depts.items()
        if d.status == "active" and not _department_chain_has_inactive_ancestor(d, all_depts)
    }

    valid_mandates: list[Any] = []
    for m in all_mandates:
        if m.status != "active" or int(m.ceiling_cents) <= 0:
            continue
        grantor = all_employees.get(m.grantor)
        grantee = all_employees.get(m.grantee)
        if grantor is None or grantee is None:
            continue
        if grantor.status != "active" or grantee.status != "active":
            continue
        if grantor.department not in active_depts or grantee.department not in active_depts:
            continue
        grantor_pos = all_positions.get(grantor.position)
        grantee_pos = all_positions.get(grantee.position)
        if grantor_pos is None or grantee_pos is None:
            continue
        if grantor_pos.status != "active" or grantee_pos.status != "active":
            continue
        valid_mandates.append(m)

    return active_depts, all_positions, all_employees, all_depts, valid_mandates


def project_h2r_to_org_directory(
    runtime: OntologyRuntime,
    actor: Actor,
    *,
    token_hashes: Mapping[str, str] | None = None,
) -> OrgDirectory:
    """只读读取 H2R ABox，构造剔除了已撤销/已离职/已停职幽灵成员与归档部门的 M2 ``OrgDirectory``。"""
    active_depts, all_positions, all_employees, _, valid_mandates = _load_active_h2r_snapshot(
        runtime, actor
    )

    units: list[OrgUnit] = []
    for dept in active_depts.values():
        parent_id = dept.parent_department.strip() if dept.parent_department else None
        units.append(
            OrgUnit(
                unit_id=dept.department_id,
                name=dept.name,
                parent_id=parent_id or None,
                unit_kind="group" if not parent_id else "department",
                scope_tag=normalize_org_scope_tag(dept.scope_tag),
                metadata={"accounting_entity": dept.accounting_entity},
            )
        )

    unit_dir = OrgDirectory(units=units)
    unit_dir.validate()

    subtree_tags_by_dept: dict[str, set[str]] = {}
    for did in active_depts:
        subtree_tags_by_dept[did] = {
            unit_dir.get_unit(uid).scope_tag
            for uid in unit_dir.subtree_ids(did, include_self=True)
            if unit_dir.get_unit(uid).scope_tag and "*" not in unit_dir.get_unit(uid).scope_tag
        }

    mandate_tags_by_emp: dict[str, list[str]] = {}
    for m in valid_mandates:
        raw_tag = str(m.scope_tag or "").strip()
        if not raw_tag or "*" in raw_tag:
            continue
        norm_tag = normalize_org_scope_tag(raw_tag)
        if not norm_tag or "*" in norm_tag:
            continue
        grantor = all_employees.get(m.grantor)
        if grantor is None or grantor.department not in subtree_tags_by_dept:
            continue
        # P0-1: Only project scope_tag strictly covered by grantor's active org subtree
        if norm_tag not in subtree_tags_by_dept[grantor.department]:
            continue
        bucket = mandate_tags_by_emp.setdefault(m.grantee, [])
        if norm_tag not in bucket:
            bucket.append(norm_tag)

    members: list[OrgMember] = []
    for emp in all_employees.values():
        if emp.status != "active":
            continue
        if emp.department not in active_depts:
            continue
        pos = all_positions.get(emp.position)
        if pos is None or pos.status != "active":
            continue
        user_id = emp.actor_id.strip() or emp.employee_id
        tok_hash = (
            (token_hashes or {}).get(user_id)
            or (token_hashes or {}).get(emp.employee_id)
            or ""
        )
        members.append(
            OrgMember(
                user_id=user_id,
                display_name=emp.full_name,
                role=pos.role_name,
                unit_id=emp.department,
                title=pos.title,
                extra_clearance_tags=list(mandate_tags_by_emp.get(emp.employee_id, ())),
                active=True,
                auth_token_hash=str(tok_hash),
            )
        )

    directory = OrgDirectory(units=units, participants=members)
    directory.validate()
    return directory


def _resolve_employee_record(
    employees: Mapping[str, Any],
    employee_or_actor_id: str,
) -> Any:
    key = str(employee_or_actor_id or "").strip()
    if key in employees:
        return employees[key]
    by_actor = [emp for emp in employees.values() if emp.actor_id and emp.actor_id == key]
    if by_actor:
        active_matches = [emp for emp in by_actor if emp.status == "active"]
        return active_matches[0] if active_matches else by_actor[0]
    raise OrgAuthorityError(
        f"unknown H2R employee or actor {employee_or_actor_id!r}",
        code=ORG_MEMBER_NOT_FOUND,
    )


def _compute_employee_effective_ceilings(
    emp: Any,
    *,
    all_positions: Mapping[str, Any],
    all_employees: Mapping[str, Any],
    active_depts: Mapping[str, Any],
    valid_mandates: Sequence[Any],
    directory: OrgDirectory,
    target_unit_id: str,
    required_role: str | None,
) -> tuple[int, int, str]:
    if emp.status != "active":
        raise OrgAuthorityError(
            f"Employee:{emp.employee_id} status is {emp.status!r}; inactive employee holds zero authority",
            code=ORG_MEMBER_INACTIVE,
        )
    if emp.department not in active_depts:
        raise OrgAuthorityError(
            f"Employee:{emp.employee_id} department {emp.department!r} is inactive or missing",
            code=ORG_MEMBER_INACTIVE,
        )
    pos = all_positions.get(emp.position)
    if pos is None or pos.status != "active":
        raise OrgAuthorityError(
            f"Employee:{emp.employee_id} position {emp.position!r} is inactive or missing",
            code=ORG_MEMBER_INACTIVE,
        )

    base_ceiling = (
        int(pos.approval_ceiling_cents)
        if (required_role is None or pos.role_name == required_role or pos.role_name == "admin")
        else 0
    )

    target_chain_tags = {
        directory.get_unit(uid).scope_tag
        for uid in directory.ancestors(target_unit_id, include_self=True)
        if directory.get_unit(uid).scope_tag and "*" not in directory.get_unit(uid).scope_tag
    }
    mandate_ceiling = 0
    for m in valid_mandates:
        if m.grantee != emp.employee_id:
            continue
        if required_role is not None and m.role_name != required_role:
            continue
        raw_m_tag = str(m.scope_tag or "").strip()
        if "*" in raw_m_tag:
            continue
        m_tag = normalize_org_scope_tag(raw_m_tag)
        if "*" in m_tag:
            continue
        grantor = all_employees.get(m.grantor)
        # P0-2: Strict conjunction — grantor subtree must cover target AND explicit scope_tag must match target ancestor chain
        grantor_covers_target = (
            grantor is not None
            and grantor.department in active_depts
            and directory.covers(grantor.department, target_unit_id)
        )
        tag_matches_target = (not m_tag) or (m_tag in target_chain_tags)
        if grantor_covers_target and tag_matches_target and int(m.ceiling_cents) > mandate_ceiling:
            mandate_ceiling = int(m.ceiling_cents)

    return base_ceiling, mandate_ceiling, str(pos.role_name)


def build_h2r_tiered_authority_policy(
    runtime: OntologyRuntime,
    actor: Actor,
    *,
    auto_ceiling_cents: int = 0,
    escalation_ladder_cents: Sequence[tuple[int | None, str]] | None = None,
    role_rank: Mapping[str, int] | None = None,
) -> TieredAuthorityPolicy:
    """结合 H2R 在岗定编上限与等级天梯构造 M2 ``TieredAuthorityPolicy``（金额单位：元）。"""
    ranks = dict(role_rank or DEFAULT_ENTERPRISE_ROLE_RANK)
    tiers: list[AuthorityTier] = []

    next_min_yuan = 0.0
    if auto_ceiling_cents > 0:
        max_auto_yuan = round(int(auto_ceiling_cents) / 100.0, 2)
        tiers.append(
            AuthorityTier(
                tier_id="H2R_AUTO_WITHIN_EFFECTIVE_CEILING",
                min_amount=0.0,
                max_amount=max_auto_yuan,
                mode="AUTO",
                required_approver_role=None,
                require_covering_org_scope=True,
            )
        )
        next_min_yuan = round((int(auto_ceiling_cents) + 1) / 100.0, 2)

    resolved_ladder: list[tuple[int | None, str]]
    if escalation_ladder_cents is not None:
        resolved_ladder = [(int(mc) if mc is not None else None, str(r)) for mc, r in escalation_ladder_cents]
    else:
        max_by_role: dict[str, int] = {}
        for pos in runtime.query_objects("JobPosition", actor):
            if pos.status != "active":
                continue
            rname = str(pos.role_name)
            if ranks.get(rname, 0) < 20 or rname == "admin":
                continue
            ceil = int(pos.approval_ceiling_cents)
            if rname not in max_by_role or ceil > max_by_role[rname]:
                max_by_role[rname] = ceil

        if max_by_role:
            sorted_roles = sorted(
                max_by_role.items(),
                key=lambda item: (item[1], ranks.get(item[0], 0), item[0]),
            )
            deduped: list[tuple[int | None, str]] = []
            last_ceil = -1
            for rname, ceil in sorted_roles:
                if ceil > last_ceil:
                    deduped.append((ceil, rname))
                    last_ceil = ceil
            top_role = max(
                sorted_roles,
                key=lambda item: (ranks.get(item[0], 0), item[1], item[0]),
            )[0]
            if deduped and deduped[-1][1] == top_role:
                deduped[-1] = (None, top_role)
            else:
                deduped.append((None, top_role))
            resolved_ladder = deduped
        else:
            resolved_ladder = [
                (20_000_000, "procurement_manager"),
                (None, "finance_director"),
            ]

    for idx, (max_cents, approver_role) in enumerate(resolved_ladder, start=1):
        is_last = idx == len(resolved_ladder)
        if max_cents is not None and auto_ceiling_cents >= max_cents and not is_last:
            continue
        band_max = None if (max_cents is None or is_last) else round(max_cents / 100.0, 2)
        if band_max is not None and band_max < next_min_yuan:
            continue
        tiers.append(
            AuthorityTier(
                tier_id=f"H2R_ESCALATE_{idx}_{approver_role.upper()}",
                min_amount=next_min_yuan,
                max_amount=band_max,
                mode="ESCALATE",
                required_approver_role=approver_role,
                require_covering_org_scope=True,
            )
        )
        if band_max is None:
            break
        next_min_yuan = round(band_max + 0.01, 2)

    return TieredAuthorityPolicy(tiers=tuple(tiers), role_rank=ranks)


def evaluate_h2r_employee_authority(
    runtime: OntologyRuntime,
    actor: Actor,
    *,
    employee_or_actor_id: str,
    target_unit_id: str,
    amount_cents: int,
    required_role: str | None = None,
    escalation_ladder_cents: Sequence[tuple[int | None, str]] | None = None,
    directory: OrgDirectory | None = None,
) -> H2rAuthorityDecision:
    """联合 H2R 基础岗位上限、有效 ``active`` 授权委派与 M2 ``OrgDirectory`` 评估是否可自动执行或需升级审批。"""
    active_depts, all_positions, all_employees, _, valid_mandates = _load_active_h2r_snapshot(
        runtime, actor
    )
    emp = _resolve_employee_record(all_employees, employee_or_actor_id)
    eff_dir = directory or project_h2r_to_org_directory(runtime, actor)

    base_ceiling, mandate_ceiling, base_role = _compute_employee_effective_ceilings(
        emp,
        all_positions=all_positions,
        all_employees=all_employees,
        active_depts=active_depts,
        valid_mandates=valid_mandates,
        directory=eff_dir,
        target_unit_id=target_unit_id,
        required_role=required_role,
    )
    effective_ceiling = max(base_ceiling, mandate_ceiling)
    user_id = emp.actor_id.strip() or emp.employee_id

    policy = build_h2r_tiered_authority_policy(
        runtime,
        actor,
        auto_ceiling_cents=effective_ceiling,
        escalation_ladder_cents=escalation_ladder_cents,
    )
    tier_eval = evaluate_action_authority(
        eff_dir,
        user_id,
        target_unit_id,
        round(int(amount_cents) / 100.0, 2),
        policy=policy,
    )
    return H2rAuthorityDecision(
        employee_id=emp.employee_id,
        user_id=user_id,
        unit_id=emp.department,
        target_unit_id=target_unit_id,
        base_role=base_role,
        required_role=required_role,
        amount_cents=int(amount_cents),
        base_ceiling_cents=base_ceiling,
        mandate_ceiling_cents=mandate_ceiling,
        effective_ceiling_cents=effective_ceiling,
        decision=tier_eval.decision,
        tier_evaluation=tier_eval,
    )


def verify_h2r_approver_authority(
    runtime: OntologyRuntime,
    actor: Actor,
    *,
    approver_employee_or_actor_id: str,
    requester_employee_or_actor_id: str,
    target_unit_id: str,
    amount_cents: int,
    policy: TieredAuthorityPolicy,
    required_role: str | None = None,
    directory: OrgDirectory | None = None,
) -> AuthorityTier:
    """在签批放行前联合校验审批人的组织范围覆盖、SoD 反自批、角色职级及实际 H2R 核准额度上限。"""
    active_depts, all_positions, all_employees, _, valid_mandates = _load_active_h2r_snapshot(
        runtime, actor
    )
    eff_dir = directory or project_h2r_to_org_directory(runtime, actor)

    approver_emp = _resolve_employee_record(all_employees, approver_employee_or_actor_id)
    if approver_emp.status != "active":
        raise OrgAuthorityError(
            f"approver Employee:{approver_emp.employee_id} is inactive ({approver_emp.status!r})",
            code=ORG_MEMBER_INACTIVE,
        )
    requester_emp = _resolve_employee_record(all_employees, requester_employee_or_actor_id)
    approver_uid = approver_emp.actor_id.strip() or approver_emp.employee_id
    requester_uid = requester_emp.actor_id.strip() or requester_emp.employee_id

    tier = verify_approver_authority(
        eff_dir,
        approver_uid,
        requester_uid,
        target_unit_id,
        round(int(amount_cents) / 100.0, 2),
        policy=policy,
    )

    eff_req_role = required_role if required_role is not None else tier.required_approver_role
    base_ceil, mnd_ceil, base_role = _compute_employee_effective_ceilings(
        approver_emp,
        all_positions=all_positions,
        all_employees=all_employees,
        active_depts=active_depts,
        valid_mandates=valid_mandates,
        directory=eff_dir,
        target_unit_id=target_unit_id,
        required_role=eff_req_role,
    )
    # A director/higher-rank base position also satisfies a lower tier role rank -> fall back to full base ceiling if role rank satisfies.
    if base_ceil == 0 and eff_req_role and policy.role_satisfies(base_role, eff_req_role):
        pos = all_positions[approver_emp.position]
        base_ceil = int(pos.approval_ceiling_cents)

    approver_effective_ceiling = max(base_ceil, mnd_ceil)
    if base_role != "admin" and int(amount_cents) > approver_effective_ceiling:
        raise OrgAuthorityError(
            f"approver {approver_uid!r} effective ceiling {approver_effective_ceiling} cents < requested {amount_cents} cents",
            code=AUTHORITY_TIER_INSUFFICIENT,
        )
    return tier


def project_event_to_draft_journal(
    runtime: OntologyRuntime,
    actor: Actor,
    *,
    entry_id: str,
    accounting_entity: str,
    fiscal_period: str,
    source_event_id: str,
    debit_account: str,
    credit_account: str,
    amount_cents: int | None = None,
    memo: str = "",
) -> Any:
    """从已确认 ``FulfillmentEvent`` 确定性生成绑定 ``source_event`` 的草稿 ``JournalEntry``（零旁路写入）。"""
    ev = runtime.get_object("FulfillmentEvent", source_event_id, actor)
    if ev.status != "confirmed":
        raise InvariantError(
            f"FulfillmentEvent:{ev.event_id} status is {ev.status!r}; expected 'confirmed' to project journal",
            code="SOURCE_EVENT_NOT_CONFIRMED",
        )
    event_cents = int(round(float(ev.amount) * 100))
    eff_cents = event_cents if amount_cents is None else int(amount_cents)
    if eff_cents <= 0:
        raise InvariantError(
            f"JournalEntry:{entry_id} projected amount_cents={eff_cents} must be > 0",
            code="NON_POSITIVE_JOURNAL_AMOUNT",
        )
    if eff_cents > event_cents:
        raise InvariantError(
            f"JournalEntry:{entry_id} amount_cents={eff_cents} exceeds event {ev.event_id} amount_cents={event_cents}",
            code="JOURNAL_EXCEEDS_SOURCE_EVENT",
        )

    JeCls = runtime.spec.get_object_type("JournalEntry")
    draft = JeCls(
        entry_id=entry_id,
        accounting_entity=accounting_entity,
        fiscal_period=fiscal_period,
        source_event=ev.event_id,
        debit_account=debit_account,
        credit_account=credit_account,
        amount_cents=eff_cents,
        memo=memo or f"{ev.event_kind}:{ev.event_id}",
        status="draft",
    )
    runtime.put_object(draft, actor=actor)
    return draft


def project_and_post_event_journal(
    runtime: OntologyRuntime,
    *,
    seed_actor: Actor,
    post_actor: Actor,
    run_id: str,
    entry_id: str,
    accounting_entity: str,
    fiscal_period: str,
    source_event_id: str,
    debit_account: str,
    credit_account: str,
    amount_cents: int | None = None,
    memo: str = "",
) -> dict[str, Any]:
    """将跨闭环已确认履约事实投影为总账凭证，并严格经由 ``post_journal_entry`` 受控过账。"""
    project_event_to_draft_journal(
        runtime,
        seed_actor,
        entry_id=entry_id,
        accounting_entity=accounting_entity,
        fiscal_period=fiscal_period,
        source_event_id=source_event_id,
        debit_account=debit_account,
        credit_account=credit_account,
        amount_cents=amount_cents,
        memo=memo,
    )
    return runtime.submit_action(
        run_id,
        "post_journal_entry",
        {"entry_id": entry_id},
        post_actor,
    )
