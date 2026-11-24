"""Pure M2 domain-agnostic HTTP/JSON console server over ``OntologyRuntime``.

Provides the L1 control-plane transport surface without binding to any concrete
domain schema or external vendor protocol:
- Actor authentication via SHA-256 Bearer tokens (``OrgDirectory`` or token registry)
  or optional controlled demo persona switching (``demo_actor_switch=True``).
- Read-only TBox, behavior, invariant, and RLS-filtered object + lineage inspection.
- Counterfactual parallel sandboxes backed by ``runtime.fork()`` with live
  primary-vs-sandbox comparison and policy-governed promotion.
- Runtime action simulation and submission governed by ``AutonomyRampController``
  (``SHADOW`` / ``COPILOT_ESCALATE`` / ``AUTO_UNDER_CEILING``).
- Controlled admin diagnostics and autonomy policy tuning (role-gated).
- RLS-scoped natural-language assistant queries and static SPA hosting.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.request
import uuid

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import uvicorn

from .aspects import SecurityAndAuthorityAspect
from .autonomy import ActionAutonomyRule, AutonomyMode, AutonomyPolicy, AutonomyRampController
from .drivers import DeviceDriverRegistry
from .model_views import describe_behavior, describe_tbox
from .org_authority import ORG_MEMBER_INACTIVE, ORG_MEMBER_NOT_FOUND, OrgAuthorityError, OrgDirectory
from .runtime import OntologyRuntime, SimulationResult
from .sandbox import ObjectDiff
from .spec import (
    ActionNotFoundError,
    Actor,
    FunctionNotFoundError,
    InvariantError,
    ObjectNotFoundError,
    OpenOntologyError,
    PermissionDeniedError,
    UnknownObjectTypeError,
)

__all__ = [
    "DemoActorProfile",
    "WorkQueueBinding",
    "ConsoleService",
    "ConsoleServerHandle",
    "create_console_app",
    "start_console_server",
]

_DEFAULT_ADMIN_ROLES: frozenset[str] = frozenset(
    {"admin", "steward", "finance_director", "hr_director"}
)

_MAX_SANDBOXES = 32

_STATUS_ZH: dict[str, str] = {
    "OPEN": "待处理",
    "IN_PROGRESS": "处理中",
    "COMPLETED": "已完成",
    "REJECTED": "已驳回",
    "SLA_BREACHED": "已超时",
    "CANCELLED": "已取消",
    "COMMITTED": "已执行",
    "REVERTED": "已撤回",
    "SHADOW_RECORDED": "影子演练",
    "SHADOW_WOULD_REJECT": "影子拦截",
    "ESCALATED_COMMITTED": "转人工签批",
    "ESCALATION_REQUIRED": "需人工复核",
    "AUTO_COMMITTED": "自动放行",
    "AUTO_REJECTED": "红线拦截",
    "COPILOT_REJECTED": "红线拦截",
    "VALID": "校验通过",
    "MOUNTED": "已连接",
    "CONNECTED": "已连接",
    "DEGRADED": "降级运行",
    "UNPLUGGED": "已断开",
    "draft": "草稿",
    "confirmed": "已确认",
    "released": "已下达",
    "in_progress": "进行中",
    "completed": "已完成",
    "cancelled": "已取消",
    "active": "启用中",
    "archived": "已归档",
    "verified": "已核验",
    "pending": "待处理",
    "recorded": "已记录",
    "agreed": "已生效",
    "passed": "合格",
    "HIGH": "高优",
    "MEDIUM": "中等",
    "LOW": "常规",
}

_ERROR_ZH: dict[str, str] = {
    "PERMISSION_DENIED": "当前角色没有权限执行该操作，已安全拦截。",
    "AUTH_TOKEN_MISSING": "缺少身份认证凭据，请重新登录或选择演示账号。",
    "AUTH_TOKEN_INVALID": "身份凭据无效或已过期。",
    "SOD_SELF_APPROVAL_FORBIDDEN": "触发职责分离红线：发起人不得审批自己提交的事项。",
    "UNBOUND_SOURCE_BRIDGE": "外部系统连接器未挂载或已断开，已阻止对真实系统的写入。",
    "DEVICE_STATE_MISMATCH": "外部系统回读状态与预期不一致，本事务已安全中止并保持零脏写。",
    "ILLEGAL_DOCUMENT_TRANSITION": "当前对象状态不允许直接跃迁到目标状态。",
    "WORK_CARD_ASSIGNEE_MISMATCH": "当前账号角色与该待办事项的指派审批角色不匹配。",
    "WORK_CARD_NOT_ACTIVE": "该待办事项已结束处理，不能重复操作。",
}

_SENSITIVE_FIELDS: frozenset[str] = frozenset({"auth_token_hash", "password", "secret"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_base_display() -> dict[str, Any]:
    display_path = Path(__file__).with_name("ui_display.json")
    if not display_path.is_file():
        return {}
    try:
        raw = json.loads(display_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _merge_display(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for bucket in (
        "ontologies",
        "categories",
        "objects",
        "object_properties",
        "fields",
        "field_overrides",
        "actions",
        "functions",
        "automations",
        "interfaces",
        "roles",
        "statuses",
        "values",
        "cardinalities",
        "kinds",
        "invariants",
        "errors",
    ):
        left = base.get(bucket) or {}
        right = extra.get(bucket) or {}
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            merged[bucket] = {**dict(left), **dict(right)}
        elif isinstance(right, Mapping):
            merged[bucket] = dict(right)
        elif isinstance(left, Mapping):
            merged[bucket] = dict(left)
        else:
            merged[bucket] = {}
    merged["statuses"] = {**_STATUS_ZH, **merged.get("statuses", {})}
    for key, val in extra.items():
        if key not in merged:
            merged[key] = val
    return merged


def _walk_param_enums(node: Any, out: dict[str, list[Any]]) -> None:
    if not isinstance(node, Mapping):
        return
    op = node.get("op")
    if op == "and":
        for sub in node.get("args") or ():
            _walk_param_enums(sub, out)
        return
    if op == "in":
        left = node.get("left")
        right = node.get("right")
        if (
            isinstance(left, Mapping)
            and isinstance(left.get("ref"), str)
            and left["ref"].startswith("param.")
            and isinstance(right, Mapping)
            and isinstance(right.get("lit"), list)
            and len(right["lit"]) > 0
            and all(isinstance(v, (str, int, float, bool)) for v in right["lit"])
        ):
            pname = left["ref"][len("param.") :]
            if pname and "." not in pname:
                out.setdefault(pname, list(right["lit"]))


def _enrich_action_params_schema(
    act_def: Any,
    base_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if base_schema is not None:
        schema = copy.deepcopy(dict(base_schema))
    elif act_def is not None and getattr(act_def, "params", None) is not None:
        schema = act_def.params.model_json_schema()
    else:
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    for pschema in props.values():
        if not isinstance(pschema, dict) or "enum" in pschema:
            continue
        branches = [
            b
            for b in (pschema.get("anyOf") or ())
            if isinstance(b, Mapping) and b.get("type") != "null"
        ]
        if len(branches) == 1 and isinstance(branches[0].get("enum"), list):
            pschema["enum"] = list(branches[0]["enum"])
    if act_def is not None and getattr(act_def, "implementation_kind", None) == "rule":
        param_enums: dict[str, list[Any]] = {}
        for rule in getattr(act_def, "submission_criteria", ()) or ():
            must_hold = (
                rule.get("must_hold")
                if isinstance(rule, Mapping)
                else getattr(rule, "must_hold", None)
            )
            _walk_param_enums(must_hold, param_enums)
        for pname, vals in param_enums.items():
            prop_schema = props.get(pname)
            if isinstance(prop_schema, dict) and "enum" not in prop_schema:
                prop_schema["enum"] = list(vals)
    return schema


def _format_validation_error_zh(
    exc: ValidationError | RequestValidationError,
    display: Mapping[str, Any] | None = None,
) -> str:
    fields_map = (display or {}).get("fields") or {}
    parts: list[str] = []
    for err in exc.errors():
        loc = [str(x) for x in (err.get("loc") or ()) if str(x) not in ("body", "params")]
        fname = loc[-1] if loc else ""
        label = str(fields_map.get(fname) or fname or "输入参数")
        etype = str(err.get("type") or "")
        if "missing" in etype:
            parts.append(f"「{label}」为必填项")
        elif any(k in etype for k in ("int", "float", "decimal")):
            parts.append(f"「{label}」须为有效数值")
        elif "bool" in etype:
            parts.append(f"「{label}」须为布尔值")
        else:
            parts.append(f"「{label}」格式不符合要求")
    uniq = list(dict.fromkeys(parts))
    if not uniq:
        return "参数校验未通过，请核对必填字段与数值格式后重试。"
    return "参数校验未通过：" + "；".join(uniq) + "。"


def _translate_error_hint(
    error_code: str | None,
    error_message: str | None,
    display: Mapping[str, Any] | None = None,
) -> str:
    code = str(error_code or "").strip()
    msg = str(error_message or "").strip()
    if "errors.pydantic.dev" in msg or "validation error" in msg.lower():
        return "提交参数不完整或数值格式有误，请核对必填字段后重试。"
    if code in ("OBJECT_NOT_FOUND", "OBJECT_TYPE_NOT_FOUND") or "not found" in msg.lower():
        m = re.search(
            r"(?:[:\s'\"])([A-Za-z0-9][A-Za-z0-9_.-]{0,63})['\"]?\s+(?:not found|does not exist)",
            msg,
            re.IGNORECASE,
        )
        if m:
            return f"未找到编号为「{m.group(1)}」的目标记录，请核对编号后重试。"
        return "未找到指定的目标记录，请核对编号后重试。"
    custom_errors = (display or {}).get("errors") or {}
    if code and code in custom_errors:
        return str(custom_errors[code])
    if code in _ERROR_ZH:
        return _ERROR_ZH[code]
    if code.startswith("APPROVAL_REQUIRED_"):
        return "触发治理审批阈值：需高一级负责人签批放行。"
    if msg:
        return f"规则校验未通过（{msg}），已阻止不合规写入，真实系统保持零改动。"
    return ""


def _default_numeric_extractor(
    params: Mapping[str, Any],
    sim: SimulationResult,
) -> float:
    for key in (
        "amount_total",
        "amount",
        "payment_amount",
        "settled_amount",
        "amount_cents",
        "total_amount",
        "cost_cells",
        "value",
    ):
        val = params.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val) / 100.0 if key.endswith("_cents") else float(val)
    for change in sim.diff.object_changes:
        for snap in (change.after, change.before):
            if not isinstance(snap, Mapping):
                continue
            for key in (
                "amount_total",
                "amount",
                "amount_cents",
                "total_amount",
                "cost_cells",
            ):
                val = snap.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    return float(val) / 100.0 if key.endswith("_cents") else float(val)
    return 0.0


def _diff_summary(
    diff: Mapping[str, Any],
    *,
    title_resolver: Callable[
        [str, str, Mapping[str, Any] | None, Mapping[str, Any] | None], str
    ]
    | None = None,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for raw in diff.get("object_changes") or ():
        if not isinstance(raw, Mapping):
            continue
        ot = str(raw.get("object_type") or "")
        oid = str(raw.get("object_id") or "")
        before = raw.get("before") if isinstance(raw.get("before"), Mapping) else {}
        after = raw.get("after") if isinstance(raw.get("after"), Mapping) else {}
        field_deltas: list[dict[str, Any]] = []
        keys = sorted(set(before.keys()) | set(after.keys()))
        for key in keys:
            if before.get(key) != after.get(key):
                field_deltas.append(
                    {
                        "field": key,
                        "before": before.get(key),
                        "after": after.get(key),
                    }
                )
        obj_title = title_resolver(ot, oid, before, after) if title_resolver and ot and oid else ""
        changes.append(
            {
                "kind": raw.get("kind"),
                "object_type": ot,
                "object_id": oid,
                "object_title": obj_title,
                "before": dict(before) if before else None,
                "after": dict(after) if after else None,
                "field_deltas": field_deltas,
            }
        )
    return changes


def _extract_device_registry(runtime: OntologyRuntime) -> DeviceDriverRegistry | None:
    direct = getattr(runtime, "device_registry", None)
    if isinstance(direct, DeviceDriverRegistry):
        return direct
    sb = runtime.source_bridge
    if sb is None:
        return None
    sb_reg = getattr(sb, "device_registry", None)
    if isinstance(sb_reg, DeviceDriverRegistry):
        return sb_reg
    for fn in (sb.actuate, sb.observe):
        if fn is None:
            continue
        for cell in getattr(fn, "__closure__", None) or ():
            val = cell.cell_contents
            if isinstance(val, DeviceDriverRegistry):
                return val
    return None


@dataclass(frozen=True)
class DemoActorProfile:
    """Selectable demo identity mapped deterministically onto a scoped ``Actor``."""

    actor_id: str
    display_name: str
    role: str
    unit_id: str = ""
    unit_name: str = ""
    title: str = ""
    badge: str = ""
    clearance_tags: tuple[str, ...] = ()
    token: str = ""
    actor: Actor | None = None

    def to_actor(self, org_directory: OrgDirectory | None = None) -> Actor:
        if self.actor is not None:
            return self.actor
        if org_directory is not None:
            try:
                return org_directory.resolve_actor(self.actor_id)
            except OrgAuthorityError:
                pass
        return Actor(
            actor_id=self.actor_id,
            role=self.role,
            clearance_tags=set(self.clearance_tags),
        )

    def to_public_dict(self, actor: Actor, *, admin_roles: Iterable[str]) -> dict[str, Any]:
        return {
            "actor_id": actor.actor_id,
            "role": actor.role,
            "display_name": self.display_name,
            "unit_id": self.unit_id,
            "unit_name": self.unit_name,
            "title": self.title or self.display_name,
            "badge": self.badge,
            "clearance_tags": sorted(actor.clearance_tags),
            "is_admin_ops": actor.role in set(admin_roles),
        }


class WorkQueueBinding(BaseModel):
    """Pure M2 descriptor mapping any domain task/escalation object to inbox actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_type: str
    id_field: str = "card_id"
    kind_field: str = "card_kind"
    status_field: str = "status"
    active_statuses: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "SLA_BREACHED")
    target_type_field: str = "target_object_type"
    target_id_field: str = "target_object_id"
    intent_action_field: str = "requested_action"
    intent_params_field: str = "requested_params_json"
    requester_field: str = "requester_actor_id"
    assignee_role_field: str = "assignee_role"
    accept_op: str = ""
    decline_op: str = ""
    claim_op: str | None = None
    id_param: str = "card_id"
    note_param: str = "resolution_note"
    settled_run_param: str = "settled_run_id"


@dataclass
class _SandboxSession:
    sandbox_id: str
    fork_runtime: OntologyRuntime
    base_fingerprint: dict[str, Any]
    created_at: str
    created_by: str
    steps: list[dict[str, Any]] = field(default_factory=list)


class _SandboxSubmitStep(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    actor_id: str | None = None


class _SandboxSubmitRequest(BaseModel):
    action: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    actor_id: str | None = None
    steps: list[_SandboxSubmitStep] | None = None


class _ActionPayload(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class _WorkQueueResolvePayload(BaseModel):
    decision: str
    note: str = ""
    item_type: str | None = None


class _AutonomyRulePayload(BaseModel):
    action: str = "*"
    roles: list[str] = Field(default_factory=list)
    mode: AutonomyMode = "SHADOW"
    ceiling_field: str | None = None
    max_value: float | None = None
    escalation_action: str | None = None
    escalation_assignee_role: str | None = None


class _AutonomyUpdatePayload(BaseModel):
    default_mode: AutonomyMode | None = None
    max_value: float | None = None
    ceiling_field: str | None = None
    escalation_action: str | None = None
    escalation_assignee_role: str | None = None
    rules: list[_AutonomyRulePayload] | None = None


@dataclass(frozen=True)
class _LlmEndpointConfig:
    """Resolved OpenAI-compatible LLM endpoint configuration."""

    provider: str
    api_key: str
    base_url: str
    model: str


def _normalize_llm_model_name(base_url: str, raw_model: str) -> str:
    """Normalize provider model aliases so official DeepSeek API accepts flash names."""
    model_str = str(raw_model or "").strip()
    if "api.deepseek.com" in str(base_url or "").lower() and model_str.lower() in (
        "deepseek-v4.1-flash",
        "deepseek/deepseek-v4.1-flash",
    ):
        return "deepseek-flash"
    return model_str


def _resolve_llm_assistant_config(
    explicit_model: str | None = None,
    explicit_base_url: str | None = None,
) -> _LlmEndpointConfig | None:
    """Resolve OpenAI-compatible assistant credentials in priority order."""
    model_override = (
        str(explicit_model).strip()
        if explicit_model is not None and str(explicit_model).strip()
        else None
    )
    base_override = (
        str(explicit_base_url).strip()
        if explicit_base_url is not None and str(explicit_base_url).strip()
        else None
    )

    ds_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if ds_key:
        base_url = (
            base_override
            or os.environ.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com/v1"
        ).strip().rstrip("/")
        raw_model = (
            model_override
            or os.environ.get("OPEN_ONTOLOGY_ASSISTANT_MODEL")
            or os.environ.get("DEEPSEEK_MODEL")
            or "deepseek-flash"
        )
        return _LlmEndpointConfig(
            provider="deepseek",
            api_key=ds_key,
            base_url=base_url,
            model=_normalize_llm_model_name(base_url, raw_model),
        )

    or_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if or_key:
        base_url = (
            base_override
            or os.environ.get("OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1"
        ).strip().rstrip("/")
        raw_model = (
            model_override
            or os.environ.get("OPEN_ONTOLOGY_ASSISTANT_MODEL")
            or "deepseek/deepseek-v4.1-flash"
        )
        return _LlmEndpointConfig(
            provider="openrouter",
            api_key=or_key,
            base_url=base_url,
            model=_normalize_llm_model_name(base_url, raw_model),
        )

    oa_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if oa_key:
        base_url = (
            base_override
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip().rstrip("/")
        raw_model = (
            model_override
            or os.environ.get("OPEN_ONTOLOGY_ASSISTANT_MODEL")
            or "gpt-4o-mini"
        )
        return _LlmEndpointConfig(
            provider="openai",
            api_key=oa_key,
            base_url=base_url,
            model=_normalize_llm_model_name(base_url, raw_model),
        )

    return None


def _default_openai_compatible_chat(
    cfg: _LlmEndpointConfig,
    request_body: Mapping[str, Any],
    *,
    timeout_s: float = 25.0,
) -> dict[str, Any]:
    """Perform a single stdlib HTTP POST to an OpenAI-compatible chat/completions endpoint."""
    endpoint = f"{cfg.base_url.rstrip('/')}/chat/completions"
    payload_bytes = json.dumps(dict(request_body), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = str(exc.reason or "")
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM network error: {exc.reason}") from None
    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response payload is not a JSON object")
    return parsed


_MAX_M1_DRAFTS = 32


class _AssistantPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = ""
    question: str = ""
    sandbox_id: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


class _M1ExpertChatPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = ""
    draft_id: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class _M1DraftSession:
    """Isolated actor-owned M1 DOAL draft overlay session."""

    draft_id: str
    owner_actor_id: str
    created_at: str
    last_used_at: str
    overlay: Any
    escalations: list[dict[str, Any]] = field(default_factory=list)
    last_proof: dict[str, Any] | None = None


class _BlueprintProvePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    force_refresh: bool = False


class ConsoleService:
    """Thread-safe pure M2 backend service driving the console REST & SPA surface."""

    def __init__(
        self,
        runtime: OntologyRuntime,
        *,
        default_actor: Actor | None = None,
        org_directory: OrgDirectory | None = None,
        autonomy_controller: AutonomyRampController | None = None,
        actor_tokens: Mapping[str, Actor] | None = None,
        demo_actors: Sequence[DemoActorProfile] = (),
        demo_actor_switch: bool = False,
        app_title: str = "OpenOntology 数字孪生控制台",
        app_subtitle: str = "受治理本体运营工作台",
        pack_hint: str = "",
        work_queue_binding: WorkQueueBinding | None = None,
        sandbox_presets: Sequence[Mapping[str, Any]] = (),
        quick_prompts: Sequence[str] = (),
        assistant_welcome: str = "",
        value_unit_label: str = "",
        assistant_responder: Callable[
            [OntologyRuntime, Actor, str, Mapping[str, Any]], dict[str, Any]
        ]
        | None = None,
        assistant_llm_client: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        enable_llm_assistant: bool | None = None,
        assistant_model: str | None = None,
        assistant_base_url: str | None = None,
        admin_roles: Iterable[str] = _DEFAULT_ADMIN_ROLES,
        escalation_action: str | None = None,
        escalation_assignee_role: str | None = None,
        escalation_payload_builder: Callable[..., dict[str, Any]] | None = None,
        value_extractor: Callable[[Mapping[str, Any], SimulationResult], float | int | None]
        | None = None,
    ) -> None:
        self.runtime = runtime
        self.org_directory = org_directory
        self.demo_actor_switch = bool(demo_actor_switch)
        self.app_title = str(app_title)
        self.app_subtitle = str(app_subtitle)
        self.pack_hint = str(pack_hint)
        self.sandbox_presets = [dict(item) for item in sandbox_presets]
        self.quick_prompts = [str(item) for item in quick_prompts]
        self.assistant_welcome = str(assistant_welcome)
        self.value_unit_label = str(value_unit_label)
        self.assistant_responder = assistant_responder
        self.assistant_llm_client = assistant_llm_client
        self.enable_llm_assistant = enable_llm_assistant
        self.assistant_model = assistant_model
        self.assistant_base_url = assistant_base_url
        self.admin_roles = frozenset(admin_roles or _DEFAULT_ADMIN_ROLES)
        self._lock = threading.RLock()
        self._sandboxes: dict[str, _SandboxSession] = {}
        self._m1_drafts: dict[str, _M1DraftSession] = {}
        self._proof_cache: dict[tuple[str, bool], dict[str, Any]] = {}

        self._demo_profiles: dict[str, DemoActorProfile] = {}
        self._demo_order: list[str] = []
        self._token_map: dict[str, Actor] = dict(actor_tokens or {})
        for prof in demo_actors:
            resolved = prof.to_actor(org_directory)
            self._demo_profiles[prof.actor_id] = prof
            self._demo_order.append(prof.actor_id)
            if prof.token:
                self._token_map[prof.token] = resolved

        if default_actor is not None:
            self.default_actor = default_actor
        elif self._demo_order:
            first_prof = self._demo_profiles[self._demo_order[0]]
            self.default_actor = first_prof.to_actor(org_directory)
        else:
            self.default_actor = Actor(actor_id="console-viewer", role="admin", clearance_tags={"*"})

        self.work_queue_binding = work_queue_binding or self._infer_work_queue_binding()

        if autonomy_controller is not None:
            self.autonomy_controller = autonomy_controller
            if self.autonomy_controller.value_extractor is None:
                self.autonomy_controller.value_extractor = (
                    value_extractor or _default_numeric_extractor
                )
        else:
            policy = AutonomyPolicy(
                default_mode="AUTO_UNDER_CEILING",
                default_escalation_action=escalation_action,
                default_escalation_assignee_role=escalation_assignee_role,
                rules=(
                    ActionAutonomyRule(
                        action="*",
                        mode="AUTO_UNDER_CEILING",
                        max_value=500_000.0,
                        escalation_action=escalation_action,
                        escalation_assignee_role=escalation_assignee_role,
                    ),
                ),
            )
            self.autonomy_controller = AutonomyRampController(
                policy,
                escalation_payload_builder=escalation_payload_builder,
                value_extractor=value_extractor or _default_numeric_extractor,
            )

    def close(self) -> None:
        with self._lock:
            for sbx in list(self._sandboxes.values()):
                try:
                    sbx.fork_runtime.close()
                except Exception:
                    pass
            self._sandboxes.clear()
            self._m1_drafts.clear()

    def _infer_work_queue_binding(self) -> WorkQueueBinding | None:
        """Purely structural M2 detection of an active queue object schema."""
        required_cols = {
            "target_object_type",
            "target_object_id",
            "requested_action",
            "requested_params_json",
            "assignee_role",
            "status",
        }
        for ot_name, cls in self.runtime.spec.object_types.items():
            fnames = set(cls.model_fields.keys())
            if required_cols <= fnames:
                pk_name = cls.primary_key_name()
                accept_op = ""
                decline_op = ""
                claim_op = None
                for act_name, act_def in self.runtime.spec.actions.items():
                    if ot_name not in act_def.touched_object_types:
                        continue
                    param_keys = set(act_def.params.model_fields.keys())
                    if "settled_run_id" in param_keys and pk_name in param_keys:
                        accept_op = act_name
                    elif pk_name in param_keys and "resolution_note" in param_keys and "settled_run_id" not in param_keys:
                        if "sla" not in act_name and "cancel" not in act_name:
                            decline_op = act_name
                    elif "external_ref" in param_keys and "resolution_note" not in param_keys and pk_name in param_keys:
                        claim_op = act_name
                if accept_op and decline_op:
                    return WorkQueueBinding(
                        object_type=ot_name,
                        id_field=pk_name,
                        accept_op=accept_op,
                        decline_op=decline_op,
                        claim_op=claim_op,
                        id_param=pk_name,
                    )
        return None

    def _actor_profile(self, actor: Actor) -> dict[str, Any]:
        prof = self._demo_profiles.get(actor.actor_id)
        if prof is not None:
            return prof.to_public_dict(actor, admin_roles=self.admin_roles)
        unit_id = ""
        unit_name = ""
        display_name = actor.actor_id
        title = ""
        if self.org_directory is not None:
            try:
                member = self.org_directory.get_member(actor.actor_id)
                display_name = member.display_name
                unit_id = member.unit_id
                title = member.title
                unit = self.org_directory.get_unit(unit_id)
                unit_name = unit.name
            except OrgAuthorityError:
                pass
        return {
            "actor_id": actor.actor_id,
            "role": actor.role,
            "display_name": display_name,
            "unit_id": unit_id,
            "unit_name": unit_name,
            "title": title,
            "badge": "",
            "clearance_tags": sorted(actor.clearance_tags),
            "is_admin_ops": actor.role in self.admin_roles,
        }

    def resolve_request_actor(
        self,
        headers: Mapping[str, str],
        *,
        query_actor_id: str | None = None,
    ) -> Actor:
        """Authenticate bearer token or controlled demo persona switch (fail-closed)."""
        norm_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        auth_header = norm_headers.get("authorization", "").strip()
        if auth_header:
            parts = auth_header.split(None, 1)
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
                raise PermissionDeniedError("authorization header must use 'Bearer <token>' format")
            raw_token = parts[1].strip()
            if raw_token in self._token_map:
                return self._token_map[raw_token]
            if self.org_directory is not None:
                return self.org_directory.authenticate_token(raw_token)
            raise PermissionDeniedError("invalid or expired bearer token")

        requested_id = (
            (query_actor_id or "").strip()
            or norm_headers.get("x-actor-id", "").strip()
        )
        if requested_id:
            if not self.demo_actor_switch:
                raise PermissionDeniedError(
                    "demo_actor_switch is disabled; provide a valid Bearer token"
                )
            prof = self._demo_profiles.get(requested_id)
            if prof is not None:
                return prof.to_actor(self.org_directory)
            if self.org_directory is not None:
                return self.org_directory.resolve_actor(requested_id)
            if requested_id == self.default_actor.actor_id:
                return self.default_actor
            raise PermissionDeniedError(f"unknown demo actor_id {requested_id!r}")

        if not self.demo_actor_switch and (self._token_map or self.org_directory is not None):
            raise PermissionDeniedError("authentication required")
        return self.default_actor

    def _require_admin_ops(self, actor: Actor) -> None:
        if actor.role not in self.admin_roles:
            raise PermissionDeniedError(
                f"role {actor.role!r} is not permitted for admin operations endpoints"
            )

    def _merged_display(self) -> dict[str, Any]:
        return _merge_display(_load_base_display(), self.runtime.spec.display or {})

    @staticmethod
    def _sanitize_properties(props: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(k): v
            for k, v in props.items()
            if str(k) not in _SENSITIVE_FIELDS
        }

    def _serialize_instance(
        self,
        rt: OntologyRuntime,
        cls: type[Any],
        obj: Any,
    ) -> dict[str, Any]:
        oid = obj.primary_key_value()
        loaded = rt.load_object(cls.__name__, oid)
        props = self._sanitize_properties(obj.model_dump(mode="json"))
        return {
            "object_type": cls.__name__,
            "object_id": oid,
            "primary_key": cls.primary_key_name(),
            "title": obj.title_value() or oid,
            "version": int(loaded.version) if loaded is not None else 1,
            "security_tags": list(loaded.security_tags) if loaded is not None else [],
            "owned": bool(cls.owned_spec()),
            "read_only_source": bool(cls.is_read_only_source()),
            "properties": props,
        }

    def _visible_sandbox_presets(self, actor: Actor) -> list[dict[str, Any]]:
        is_privileged = actor.role in self.admin_roles or "*" in actor.clearance_tags
        out: list[dict[str, Any]] = []
        for raw in self.sandbox_presets:
            allowed_roles = tuple(raw.get("allowed_roles") or ())
            req_clear = tuple(raw.get("required_clearance") or ())
            if not is_privileged:
                if allowed_roles and actor.role not in allowed_roles:
                    continue
                if req_clear and not actor.has_access(list(req_clear)):
                    continue
            out.append(
                {
                    k: v
                    for k, v in raw.items()
                    if k not in ("allowed_roles", "required_clearance")
                }
            )
        return out

    def _actor_display_name(self, actor_id: str) -> str:
        Aid = str(actor_id or "").strip()
        if not Aid:
            return ""
        prof = self._demo_profiles.get(Aid)
        if prof is not None:
            return prof.display_name
        if self.org_directory is not None:
            try:
                return self.org_directory.get_member(Aid).display_name
            except OrgAuthorityError:
                pass
        return Aid

    def _resolve_object_title(
        self,
        ot: str,
        oid: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> str:
        loaded = self.runtime.load_object(ot, oid)
        if loaded is not None and loaded.title:
            return str(loaded.title)
        cls = self.runtime.spec.object_types.get(ot)
        title_field = getattr(cls, "__title__", None) if cls is not None else None
        for snap in (after, before):
            if isinstance(snap, Mapping):
                if title_field and snap.get(title_field):
                    return str(snap[title_field])
                for fallback_k in ("name", "title", "memo", "subject"):
                    if snap.get(fallback_k):
                        return str(snap[fallback_k])
        return ""

    def _resolve_llm_mode_and_cfg(
        self,
    ) -> tuple[bool, _LlmEndpointConfig | None, str | None]:
        if self.enable_llm_assistant is False:
            return False, None, None
        if self.assistant_llm_client is not None:
            model_name = _normalize_llm_model_name(
                self.assistant_base_url or "https://api.deepseek.com/v1",
                self.assistant_model or "deepseek-flash",
            )
            return True, None, model_name
        if os.environ.get("OPEN_ONTOLOGY_ASSISTANT_LLM", "").strip() == "0":
            return False, None, None
        if self.enable_llm_assistant is None and os.environ.get("PYTEST_CURRENT_TEST"):
            return False, None, None
        cfg = _resolve_llm_assistant_config(self.assistant_model, self.assistant_base_url)
        if cfg is None:
            return False, None, None
        return True, cfg, cfg.model

    def _llm_assistant_ready(self) -> bool:
        return self._resolve_llm_mode_and_cfg()[0]

    def _active_assistant_model_name(self) -> str | None:
        return self._resolve_llm_mode_and_cfg()[2]

    # -- endpoints ---------------------------------------------------------
    def get_meta(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            demo_list: list[dict[str, Any]] = []
            if self.demo_actor_switch:
                for aid in self._demo_order:
                    prof = self._demo_profiles[aid]
                    resolved = prof.to_actor(self.org_directory)
                    demo_list.append(
                        prof.to_public_dict(resolved, admin_roles=self.admin_roles)
                    )
            reg = _extract_device_registry(self.runtime)
            device_count = len(reg.list_devices()) if reg is not None else (1 if self.runtime.source_bridge else 0)
            actor_prof = self._actor_profile(actor)
            prompts = self.quick_prompts or [
                "当前有哪些待处理的协同事项？",
                "当前可见记录与状态分布如何？",
                "最近有哪些规则拦截或审计记录？",
                "我当前的权限可见范围是什么？",
            ]
            welcome_tpl = self.assistant_welcome or (
                "您好 {display_name}！我是运行在「{title}」权限视角内的智能助手，可为您检索可见记录、待办审批与平行沙盘推演。"
            )
            try:
                welcome_text = welcome_tpl.format(
                    display_name=actor_prof.get("display_name") or actor.actor_id,
                    title=actor_prof.get("title") or actor.role,
                )
            except Exception:
                welcome_text = welcome_tpl
            llm_ready, _, llm_model = self._resolve_llm_mode_and_cfg()
            return {
                "ok": True,
                "world_mode": self.runtime.world_mode,
                "app_title": self.app_title,
                "app_subtitle": self.app_subtitle,
                "pack_hint": self.pack_hint,
                "actor": actor_prof,
                "demo_actor_switch": self.demo_actor_switch,
                "demo_actors": demo_list,
                "display": display,
                "work_queue": (
                    self.work_queue_binding.model_dump(mode="json")
                    if self.work_queue_binding is not None
                    else None
                ),
                "sandbox_presets": self._visible_sandbox_presets(actor),
                "quick_prompts": prompts,
                "assistant_welcome": welcome_text,
                "assistant_mode": "llm_sandbox" if llm_ready else "offline_fallback",
                "assistant_model": llm_model,
                "value_unit_label": self.value_unit_label,
                "autonomy_mode": self.autonomy_controller.policy.default_mode,
                "state_fingerprint": self.runtime.state_fingerprint(),
                "capabilities": {
                    "is_admin_ops": actor.role in self.admin_roles,
                    "has_org_directory": self.org_directory is not None,
                    "device_count": device_count,
                },
            }

    def _collect_invariants(
        self,
        display: Mapping[str, Any],
        *,
        spec_override: Any | None = None,
    ) -> list[dict[str, Any]]:
        target_spec = spec_override or self.runtime.spec
        inv_labels = display.get("invariants") or {}

        def _inv_desc(name: str, code: str, raw_desc: str) -> str:
            if inv_labels.get(name):
                return str(inv_labels[name])
            if inv_labels.get(code):
                return str(inv_labels[code])
            if raw_desc == "Status transitions must follow declared FSM edges.":
                return "对象状态流转必须严格遵循声明的状态机跃迁路径。"
            return raw_desc or name

        dedup_invs: dict[tuple[str, str], dict[str, Any]] = {}
        raw_invs = [
            ("state", inv.name, inv.code, _inv_desc(inv.name, inv.code, inv.description))
            for inv in sorted(target_spec.invariants.values(), key=lambda i: i.name)
        ] + [
            ("transition", tinv.name, tinv.code, _inv_desc(tinv.name, tinv.code, tinv.description))
            for tinv in sorted(
                target_spec.transition_invariants.values(), key=lambda i: i.name
            )
        ]
        for kind_v, name_v, code_v, desc_v in raw_invs:
            k_pair = (kind_v, code_v or name_v)
            if k_pair not in dedup_invs:
                dedup_invs[k_pair] = {
                    "kind": kind_v,
                    "name": name_v,
                    "code": code_v,
                    "description": desc_v,
                }
            else:
                existing_d = str(dedup_invs[k_pair]["description"])
                if desc_v and desc_v not in existing_d:
                    dedup_invs[k_pair]["description"] = f"{existing_d}；{desc_v}"

        return list(dedup_invs.values())

    def _export_runtime_blueprint(
        self,
        display: Mapping[str, Any],
        *,
        spec_override: Any | None = None,
    ) -> tuple[Any, str, str, str]:
        from .induction.dsl import blueprint_fingerprint, export_spec_blueprint

        target_spec = spec_override or self.runtime.spec
        ont_raw = target_spec.display.get("ontologies") or {}
        merged_ont = display.get("ontologies") or {}
        raw_id = getattr(target_spec, "name", None) or (
            str(list(ont_raw.keys())[-1]) if ont_raw else "model_under_proof"
        )
        safe_id = re.sub(r"[^A-Za-z0-9_]", "_", str(raw_id)).strip("_") or "model_under_proof"
        if safe_id[0].isdigit():
            safe_id = f"m_{safe_id}"
        spec_name = str(
            merged_ont.get(raw_id) or ont_raw.get(raw_id) or self.app_title or raw_id
        )
        spec_version = str(getattr(target_spec, "version", None) or "1.0")
        bp = export_spec_blueprint(target_spec, model_id=safe_id)
        return bp, spec_name, spec_version, blueprint_fingerprint(bp)

    def _describe_devices_by_object(
        self,
    ) -> tuple[dict[str, list[dict[str, str]]], set[str]]:
        reg = _extract_device_registry(self.runtime)
        by_type: dict[str, list[dict[str, str]]] = {}
        synced: set[str] = set()
        if reg is not None:
            for dev in reg.list_devices():
                if isinstance(dev, dict):
                    did = str(dev.get("device_id", ""))
                    name = str(dev.get("name", did))
                    kind = str(dev.get("kind", ""))
                    status = str(dev.get("status", ""))
                    owned_t = [str(x) for x in (dev.get("owned_object_types") or ())]
                    shared_t = [str(x) for x in (dev.get("shared_object_types") or ())]
                else:
                    did = dev.device_id
                    name = dev.name
                    kind = dev.kind
                    status = dev.status
                    owned_t = list(dev.owned_object_types)
                    shared_t = list(dev.shared_object_types)
                for ot_name in owned_t:
                    synced.add(ot_name)
                    by_type.setdefault(ot_name, []).append(
                        {
                            "device_id": did,
                            "name": name,
                            "kind": kind,
                            "status": status,
                            "binding": "owned",
                        }
                    )
                for ot_name in shared_t:
                    synced.add(ot_name)
                    by_type.setdefault(ot_name, []).append(
                        {
                            "device_id": did,
                            "name": name,
                            "kind": kind,
                            "status": status,
                            "binding": "shared",
                        }
                    )
        return by_type, synced

    def _resolve_action_autonomy_label(self, action_name: str, actor: Actor) -> str:
        rule = self.autonomy_controller.policy.resolve(action_name, actor.role)
        mode = str(rule.mode or "")
        if mode == "SHADOW":
            return "shadow"
        if mode == "COPILOT_ESCALATE":
            return "confirm"
        if mode in ("AUTO_UNDER_CEILING", "FULL_AUTO"):
            return "auto"
        return mode.lower() or "auto"

    def get_tbox(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            obj_labels = display.get("objects") or {}
            field_labels = display.get("fields") or {}
            field_overrides = display.get("field_overrides") or {}
            act_labels = display.get("actions") or {}
            fn_labels = display.get("functions") or {}

            scoped_tbox = describe_tbox(self.runtime.spec, actor=actor)
            scoped_type_names = {ot["name"] for ot in scoped_tbox["object_types"]}
            tbox = describe_tbox(self.runtime.spec)
            behavior = describe_behavior(self.runtime.spec, actor=actor)

            enriched_types: list[dict[str, Any]] = []
            for ot in tbox["object_types"]:
                ot_name = ot["name"]
                cls = self.runtime.spec.get_object_type(ot_name)
                visible_count = len(self.runtime.query_objects(cls, actor))
                fields_out = []
                for fld in ot["fields"]:
                    fname = fld["name"]
                    flabel = field_overrides.get(f"{ot_name}.{fname}") or field_labels.get(fname) or fname
                    fields_out.append({**fld, "display_label": flabel})
                enriched_types.append(
                    {
                        **ot,
                        "display_label": obj_labels.get(ot_name) or ot_name,
                        "fields": fields_out,
                        "status_field": getattr(cls, "__status_field__", None),
                        "initial_statuses": list(getattr(cls, "__initial_statuses__", ()) or ()),
                        "transitions": {
                            str(k): list(v)
                            for k, v in (getattr(cls, "__transitions__", {}) or {}).items()
                        },
                        "in_role_scope": ot_name in scoped_type_names,
                        "visible_count": visible_count,
                    }
                )

            enriched_actions = []
            for act in behavior["actions"]:
                act_def = self.runtime.spec.actions.get(act["name"])
                enriched_actions.append(
                    {
                        **act,
                        "params": _enrich_action_params_schema(act_def, act.get("params")),
                        "display_label": act_labels.get(act["name"]) or act["name"],
                        "touched_object_types": (
                            list(act_def.touched_object_types) if act_def is not None else []
                        ),
                    }
                )

            enriched_functions = []
            for fn_meta in behavior["functions"]:
                fn_obj = self.runtime.spec.functions.get(fn_meta["name"])
                enriched_functions.append(
                    {
                        **fn_meta,
                        "display_label": fn_labels.get(fn_meta["name"]) or fn_meta["name"],
                        "params_schema": (
                            fn_obj.params.model_json_schema() if fn_obj is not None else {}
                        ),
                    }
                )

            invariants = self._collect_invariants(display)

            return {
                "ok": True,
                "object_types": enriched_types,
                "interfaces": tbox["interfaces"],
                "object_properties": tbox["object_properties"],
                "actions": enriched_actions,
                "functions": enriched_functions,
                "automations": behavior["automations"],
                "roles": behavior["roles"],
                "invariants": invariants,
            }

    def _build_blueprint_from_spec(
        self,
        target_spec: Any,
        actor: Actor,
        *,
        draft_overlay: Any | None = None,
    ) -> dict[str, Any]:
        display = _merge_display(_load_base_display(), target_spec.display or {})
        obj_labels = display.get("objects") or {}
        field_labels = display.get("fields") or {}
        field_overrides = display.get("field_overrides") or {}
        act_labels = display.get("actions") or {}
        ef_labels = display.get("edit_functions") or {}
        fn_labels = display.get("functions") or {}
        link_labels = display.get("links") or display.get("object_properties") or {}
        card_labels = display.get("cardinalities") or {
            "many-to-one": "N:1",
            "one-to-many": "1:N",
            "many-to-many": "M:N",
            "one-to-one": "1:1",
        }

        bp, spec_name, spec_version, bp_fp = self._export_runtime_blueprint(
            display, spec_override=target_spec
        )
        bp_actions = {act_bp.name: act_bp for act_bp in bp.actions}
        bp_ot_map = {ot_bp.name: ot_bp for ot_bp in bp.object_types}
        bp_rel_map = {
            (ot_bp.name, rel.field): rel
            for ot_bp in bp.object_types
            for rel in ot_bp.relations
        }

        draft_added_types: set[str] = set()
        draft_modified_types: set[str] = set()
        draft_added_links: set[tuple[str, str]] = set()
        draft_added_actions: set[str] = set()
        draft_modified_actions: set[str] = set()
        draft_added_invs: set[str] = set()
        if draft_overlay is not None:
            for ot_bp in (*draft_overlay.add_object_types, *draft_overlay.derive_object_types):
                draft_added_types.add(ot_bp.name)
                for rel in ot_bp.relations:
                    draft_added_links.add((ot_bp.name, rel.field))
            draft_modified_types = (
                set(draft_overlay.add_fields.keys())
                | set(draft_overlay.add_relations.keys())
                | set(draft_overlay.update_transitions.keys())
            )
            for ot_n, rels in draft_overlay.add_relations.items():
                for rel in rels:
                    draft_added_links.add((ot_n, rel.field))
            draft_added_actions = {a.name for a in draft_overlay.add_actions}
            draft_modified_actions = (
                {a.name for a in draft_overlay.update_actions}
                | set(draft_overlay.add_action_preconditions.keys())
                | set(draft_overlay.restrict_allowed_roles.keys())
                | set(draft_overlay.update_revert_roles.keys())
            )
            draft_added_invs = {inv.name for inv in draft_overlay.add_invariants}

        scoped_tbox = describe_tbox(target_spec, actor=actor)
        scoped_type_names = {ot["name"] for ot in scoped_tbox["object_types"]}
        tbox = describe_tbox(target_spec)
        scoped_behavior = describe_behavior(target_spec, actor=actor)
        scoped_action_names = {a["name"] for a in scoped_behavior["actions"]}

        devices_by_type, device_synced_types = self._describe_devices_by_object()
        pk_to_type = {
            str(cls.primary_key_name()): name
            for name, cls in target_spec.object_types.items()
        }

        actions_out: list[dict[str, Any]] = []
        for act_def in sorted(target_spec.actions.values(), key=lambda a: a.name):
            aname = act_def.name
            act_bp = bp_actions.get(aname)
            touched = list(act_def.touched_object_types or ())
            if not touched and act_bp is not None:
                seen_t: list[str] = []
                for mut in act_bp.mutations:
                    if mut.object_type not in seen_t:
                        seen_t.append(mut.object_type)
                if seen_t:
                    touched = seen_t
            if not touched and act_def.params is not None:
                inferred: list[str] = []
                for pname in getattr(act_def.params, "model_fields", {}):
                    mapped = pk_to_type.get(pname)
                    if mapped and mapped not in inferred:
                        inferred.append(mapped)
                touched = inferred

            rule_summary: dict[str, Any] | None = None
            if act_def.implementation_kind == "rule":
                pres = list(act_bp.preconditions) if act_bp is not None else []
                muts = list(act_bp.mutations) if act_bp is not None else []
                rule_summary = {
                    "precondition_count": len(pres),
                    "preconditions": [
                        {
                            "name": p.name,
                            "code": p.code,
                            "description": p.description,
                        }
                        for p in pres
                    ],
                    "mutation_count": len(muts),
                    "mutations": [
                        {
                            "op": m.op,
                            "object_type": m.object_type,
                            "set_fields": sorted(str(k) for k in m.set_fields.keys()),
                        }
                        for m in muts
                    ],
                }

            act_status = (
                "draft_added"
                if aname in draft_added_actions
                else "draft_modified"
                if aname in draft_modified_actions
                else "base"
            )
            actions_out.append(
                {
                    "name": aname,
                    "display_label": str(act_labels.get(aname) or aname),
                    "description": str(act_def.description or ""),
                    "implementation_kind": str(act_def.implementation_kind),
                    "edit_function_name": act_def.edit_function,
                    "allowed_roles": list(act_def.allowed_roles),
                    "in_role_scope": aname in scoped_action_names,
                    "touched_object_types": touched,
                    "params_schema": _enrich_action_params_schema(act_def),
                    "rule_summary": rule_summary,
                    "autonomy_mode": self._resolve_action_autonomy_label(aname, actor),
                    "draft_status": act_status,
                }
            )

        edit_functions_out: list[dict[str, Any]] = []
        for ef in sorted(target_spec.edit_functions.values(), key=lambda e: e.name):
            bound_actions = [
                a["name"]
                for a in actions_out
                if a["edit_function_name"] == ef.name
            ]
            ef_touched = list(ef.touched_object_types or ())
            if not ef_touched:
                seen_ef: list[str] = []
                for bname in bound_actions:
                    bact = next((x for x in actions_out if x["name"] == bname), None)
                    if bact:
                        for ot_t in bact["touched_object_types"]:
                            if ot_t not in seen_ef:
                                seen_ef.append(ot_t)
                ef_touched = seen_ef
            edit_functions_out.append(
                {
                    "name": ef.name,
                    "display_label": str(
                        ef_labels.get(ef.name) or act_labels.get(ef.name) or ef.name
                    ),
                    "description": str(ef.description or ""),
                    "touched_object_types": ef_touched,
                    "bound_actions": bound_actions,
                    "params_schema": (
                        ef.params.model_json_schema() if ef.params is not None else {}
                    ),
                }
            )

        functions_out: list[dict[str, Any]] = []
        for fn_def in sorted(target_spec.functions.values(), key=lambda f: f.name):
            functions_out.append(
                {
                    "name": fn_def.name,
                    "display_label": str(fn_labels.get(fn_def.name) or fn_def.name),
                    "description": str(fn_def.description or ""),
                    "read_only": bool(fn_def.read_only),
                    "params_schema": (
                        fn_def.params.model_json_schema()
                        if fn_def.params is not None
                        else {}
                    ),
                }
            )

        object_types_out: list[dict[str, Any]] = []
        base_doc = str(
            getattr(
                next(iter(target_spec.object_types.values())).__mro__[-3],
                "__doc__",
                "",
            )
            or ""
        ) if target_spec.object_types else ""

        for ot in sorted(tbox["object_types"], key=lambda o: str(o["name"])):
            ot_name = str(ot["name"])
            cls = target_spec.get_object_type(ot_name)
            if ot_name in self.runtime.spec.object_types:
                live_cls = self.runtime.spec.get_object_type(ot_name)
                visible_count = len(self.runtime.query_objects(live_cls, actor))
            else:
                visible_count = 0
            status_field = getattr(cls, "__status_field__", None)
            initial_statuses = [
                str(s) for s in (getattr(cls, "__initial_statuses__", ()) or ())
            ]
            raw_transitions = getattr(cls, "__transitions__", {}) or {}
            transitions: dict[str, list[str]] = {
                str(k): [str(dst) for dst in v]
                for k, v in raw_transitions.items()
            }
            terminal_statuses: list[str] = []
            if transitions:
                ordered_states: list[str] = []
                for st in initial_statuses:
                    if st not in ordered_states:
                        ordered_states.append(st)
                for src_s, dst_list in transitions.items():
                    if src_s not in ordered_states:
                        ordered_states.append(src_s)
                    for dst_s in dst_list:
                        if dst_s not in ordered_states:
                            ordered_states.append(dst_s)
                terminal_statuses = [
                    st
                    for st in ordered_states
                    if len(transitions.get(st, ())) == 0
                ]

            cls_doc = str(getattr(cls, "__doc__", "") or "").strip()
            clean_doc = ""
            if cls_doc and cls_doc != base_doc.strip():
                clean_doc = cls_doc.splitlines()[0].strip()

            owned_set = set(ot.get("owned_fields") or ())
            fully_owned = cls.is_fully_owned()
            type_is_synced = (
                ot_name in device_synced_types or cls.is_read_only_source()
            ) and not fully_owned

            fields_out: list[dict[str, Any]] = []
            for fld in ot["fields"]:
                fname = str(fld["name"])
                flabel = str(
                    field_overrides.get(f"{ot_name}.{fname}")
                    or field_labels.get(fname)
                    or fname
                )
                is_overlay_ext = (
                    (not fully_owned and fname in owned_set)
                    or fname.startswith("x_")
                )
                if is_overlay_ext:
                    ownership = "overlay_extension"
                elif type_is_synced:
                    ownership = "external_synced"
                else:
                    ownership = "ontology_owned"

                fields_out.append(
                    {
                        "name": fname,
                        "display_label": flabel,
                        "type": str(fld.get("type", "Any")),
                        "required": bool(fld.get("required", False)),
                        "is_primary_key": fname == str(ot["primary_key"]),
                        "is_status_field": bool(status_field and fname == status_field),
                        "ownership": ownership,
                    }
                )

            ot_status = (
                "draft_added"
                if ot_name in draft_added_types
                else "draft_modified"
                if ot_name in draft_modified_types
                else "base"
            )
            object_types_out.append(
                {
                    "name": ot_name,
                    "display_label": str(obj_labels.get(ot_name) or ot_name),
                    "description": clean_doc,
                    "extends": bp_ot_map[ot_name].extends if ot_name in bp_ot_map else None,
                    "primary_key": str(ot["primary_key"]),
                    "status_field": status_field,
                    "initial_statuses": initial_statuses,
                    "transitions": transitions,
                    "terminal_statuses": terminal_statuses,
                    "fields": fields_out,
                    "attached_devices": list(devices_by_type.get(ot_name, [])),
                    "touched_by_actions": [
                        a["name"]
                        for a in actions_out
                        if ot_name in a["touched_object_types"]
                    ],
                    "in_role_scope": ot_name in scoped_type_names,
                    "visible_count": visible_count,
                    "draft_status": ot_status,
                }
            )

        links_out: list[dict[str, Any]] = []
        for prop in sorted(
            tbox["object_properties"],
            key=lambda p: (str(p.get("domain", "")), str(p.get("field", ""))),
        ):
            pname = str(prop.get("name", ""))
            fname = str(prop.get("field", ""))
            card = str(prop.get("cardinality", "many-to-one"))
            src_name = str(prop.get("domain", ""))
            raw_label = (
                link_labels.get(pname)
                or field_overrides.get(pname)
                or field_overrides.get(f"{src_name}.{fname}")
                or field_labels.get(fname)
                or fname
                or pname
            )
            rel_meta = bp_rel_map.get((src_name, fname))
            lnk_status = (
                "draft_added"
                if (src_name, fname) in draft_added_links
                else "base"
            )
            links_out.append(
                {
                    "name": pname,
                    "field": fname,
                    "display_label": str(raw_label),
                    "source": src_name,
                    "target": str(prop.get("range", "")),
                    "cardinality": card,
                    "cardinality_label": str(card_labels.get(card, card)),
                    "min_cardinality": int(rel_meta.min_cardinality) if rel_meta is not None else 0,
                    "max_cardinality": rel_meta.max_cardinality if rel_meta is not None else None,
                    "inverse_name": prop.get("inverse_name"),
                    "description": str(prop.get("description") or ""),
                    "draft_status": lnk_status,
                }
            )

        invariants_out = [
            {
                **inv,
                "draft_status": (
                    "draft_added"
                    if inv["name"] in draft_added_invs
                    else "base"
                ),
            }
            for inv in self._collect_invariants(display, spec_override=target_spec)
        ]
        rule_count = sum(
            1 for a in actions_out if a["implementation_kind"] == "rule"
        )
        ef_action_count = sum(
            1 for a in actions_out if a["implementation_kind"] == "edit_function"
        )
        fsm_object_count = sum(
            1 for o in object_types_out if o["status_field"] or o["transitions"]
        )

        return {
            "ok": True,
            "spec_name": spec_name,
            "spec_version": spec_version,
            "blueprint_fingerprint": bp_fp,
            "summary": {
                "object_type_count": len(object_types_out),
                "link_count": len(links_out),
                "action_count": len(actions_out),
                "rule_action_count": rule_count,
                "edit_function_action_count": ef_action_count,
                "edit_function_count": len(edit_functions_out),
                "function_count": len(functions_out),
                "invariant_count": len(invariants_out),
                "fsm_object_count": fsm_object_count,
            },
            "object_types": object_types_out,
            "disjoint_types": [[a, b] for a, b in bp.disjoint_types],
            "links": links_out,
            "actions": actions_out,
            "edit_functions": edit_functions_out,
            "functions": functions_out,
            "invariants": invariants_out,
        }

    def get_blueprint(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            return self._build_blueprint_from_spec(self.runtime.spec, actor)

    def _format_proof_report_dict(
        self,
        report: Any,
        display: Mapping[str, Any],
        declared_invs: Sequence[Mapping[str, Any]],
        *,
        cached: bool = False,
    ) -> dict[str, Any]:
        obj_labels = display.get("objects") or {}
        act_labels = display.get("actions") or {}
        inv_labels = display.get("invariants") or {}

        deadlocked_actions = (
            report.total_deadlock_count + report.conditional_deadlock_count
        )
        reachable_actions = sum(
            1 for r in report.reachability if r.status == "REACHABLE"
        )
        total_invariants = len(report.invariant_preservation)
        preserved_invariants = sum(
            1 for p in report.invariant_preservation if p.status == "PROVED"
        )
        violated_invariants = report.invariant_violation_count
        total_classes = len(report.class_proofs)
        satisfiable_classes = sum(
            1 for c in report.class_proofs if c.status == "SATISFIABLE"
        )

        frag_counts = dict(report.logic_fragment_counts)
        total_frags = sum(frag_counts.values())
        exact_frags = sum(
            cnt
            for frag, cnt in frag_counts.items()
            if frag.startswith("EXACT_QF_LIA") or frag.startswith("EXACT_QF_LRA")
        )
        exact_linear_ratio = (
            round(exact_frags / total_frags, 4) if total_frags > 0 else 1.0
        )

        class_proofs = [
            {
                **item.model_dump(mode="json"),
                "display_label": str(obj_labels.get(item.object_type) or item.object_type),
                "extends_display_label": (
                    str(obj_labels.get(item.extends) or item.extends)
                    if item.extends
                    else None
                ),
                "unsat_core_labels": [
                    str(inv_labels.get(n) or n) for n in item.unsat_core
                ],
            }
            for item in report.class_proofs
        ]
        action_proofs = [
            {
                **item.model_dump(mode="json"),
                "display_label": str(
                    act_labels.get(item.action_name) or item.action_name
                ),
            }
            for item in report.reachability
        ]
        invariant_proofs = [
            {
                **item.model_dump(mode="json"),
                "display_label": str(
                    act_labels.get(item.action_name) or item.action_name
                ),
                "checked_invariant_labels": [
                    str(inv_labels.get(n) or n) for n in item.checked_invariants
                ],
                "violated_invariant_labels": [
                    str(inv_labels.get(n) or n) for n in item.violated_invariants
                ],
            }
            for item in report.invariant_preservation
        ]
        redundant_rules = [
            {
                **item.model_dump(mode="json"),
                "action_display_label": str(
                    act_labels.get(item.action_name) or item.action_name
                ),
                "rule_display_label": str(
                    inv_labels.get(item.rule_name) or item.rule_name
                ),
            }
            for item in report.redundant_rules
        ]

        verdict = (
            "PROVEN_CLEAN"
            if (
                report.ok
                and deadlocked_actions == 0
                and violated_invariants == 0
                and report.dead_class_count == 0
                and report.dl_conflict_count == 0
                and report.lsp_violation_count == 0
            )
            else "ISSUES_FOUND"
        )

        return {
            "ok": True,
            "prover_available": True,
            "cached": cached,
            "verdict": verdict,
            "elapsed_ms": report.elapsed_ms,
            "blueprint_fingerprint": report.blueprint_fingerprint,
            "summary": {
                "total_classes": total_classes,
                "satisfiable_classes": satisfiable_classes,
                "dead_class_count": report.dead_class_count,
                "dl_conflict_count": report.dl_conflict_count,
                "lsp_violation_count": report.lsp_violation_count,
                "total_actions": report.total_actions_checked,
                "reachable_actions": reachable_actions,
                "deadlocked_actions": deadlocked_actions,
                "total_invariants": total_invariants,
                "preserved_invariants": preserved_invariants,
                "violated_invariants": violated_invariants,
                "declared_invariant_count": len(declared_invs),
                "redundant_rule_count": report.redundant_rule_count,
                "exact_linear_ratio": exact_linear_ratio,
                "fragment_counts": frag_counts,
            },
            "class_proofs": class_proofs,
            "action_proofs": action_proofs,
            "invariant_proofs": invariant_proofs,
            "redundant_rules": redundant_rules,
        }

    def _prove_spec_unlocked(self, target_spec: Any) -> dict[str, Any]:
        from .induction.prover import ProverUnavailableError, prove_blueprint

        try:
            report = prove_blueprint(target_spec, check_redundancy=True)
        except ProverUnavailableError:
            return {
                "ok": True,
                "prover_available": False,
                "reason": "PROVER_UNAVAILABLE",
                "message": "未安装可选形式化证明器依赖（核心运行态零影响）。",
                "install_hint": "pip install 'open-ontology[prove]'",
            }
        with self._lock:
            display = _merge_display(_load_base_display(), target_spec.display or {})
            declared_invs = self._collect_invariants(display, spec_override=target_spec)
            return self._format_proof_report_dict(report, display, declared_invs)

    def _prove_draft_overlay(self, overlay: Any) -> dict[str, Any]:
        from .induction.dsl import apply_overlay_to_spec

        with self._lock:
            preview_spec = apply_overlay_to_spec(self.runtime.spec, overlay)
        return self._prove_spec_unlocked(preview_spec)

    def prove_current_blueprint(
        self,
        actor: Actor,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        del actor  # Authenticated by _resolve_actor before entry
        with self._lock:
            display = self._merged_display()
            _, _, _, bp_fp = self._export_runtime_blueprint(display)
            cache_key = (bp_fp, True)
            if not force_refresh and cache_key in self._proof_cache:
                cached_payload = copy.deepcopy(self._proof_cache[cache_key])
                cached_payload["cached"] = True
                return cached_payload
            live_spec = self.runtime.spec

        result = self._prove_spec_unlocked(live_spec)
        if result.get("prover_available"):
            with self._lock:
                self._proof_cache[cache_key] = copy.deepcopy(result)
        return result

    def list_objects(
        self,
        object_type: str,
        actor: Actor,
        *,
        status: str | None = None,
        q: str | None = None,
        runtime_override: OntologyRuntime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            rt = runtime_override or self.runtime
            cls = rt.spec.get_object_type(object_type)
            objs = rt.query_objects(cls, actor)
            items = [self._serialize_instance(rt, cls, obj) for obj in objs]
            if status:
                s_val = status.strip()
                items = [
                    it
                    for it in items
                    if str(it["properties"].get(getattr(cls, "__status_field__", "status") or "status", ""))
                    == s_val
                ]
            if q:
                needle = q.strip().lower()
                items = [
                    it
                    for it in items
                    if needle in it["object_id"].lower()
                    or needle in str(it["title"]).lower()
                    or needle in json.dumps(it["properties"], ensure_ascii=False).lower()
                ]
            display = self._merged_display()
            return {
                "ok": True,
                "object_type": cls.__name__,
                "display_label": (display.get("objects") or {}).get(cls.__name__) or cls.__name__,
                "primary_key": cls.primary_key_name(),
                "count": len(items),
                "items": items,
                "objects": items,
            }

    def get_single_object(
        self,
        object_type: str,
        object_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            cls = self.runtime.spec.get_object_type(object_type)
            obj = self.runtime.get_object(cls, object_id, actor)
            return {
                "ok": True,
                "item": self._serialize_instance(self.runtime, cls, obj),
            }

    def _total_degree(self, ot: str, oid: str) -> int:
        row = self.runtime.connection.execute(
            "SELECT COUNT(*) AS n FROM ontology_links "
            "WHERE (source_type = ? AND source_id = ?) OR (target_type = ? AND target_id = ?)",
            (ot, oid, ot, oid),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _neighbor_edges(
        self,
        ot: str,
        oid: str,
        actor: Actor,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Collect 1-hop prerequisite (upstream) & follow-up (downstream) links strictly via RLS."""
        upstream: list[dict[str, Any]] = []
        downstream: list[dict[str, Any]] = []
        out_rows = self.runtime.connection.execute(
            "SELECT link_type, target_type, target_id FROM ontology_links "
            "WHERE source_type = ? AND source_id = ? ORDER BY link_type, target_type, target_id",
            (ot, oid),
        ).fetchall()
        for r in out_rows:
            t_type, t_id, l_type = str(r["target_type"]), str(r["target_id"]), str(r["link_type"])
            loaded = self.runtime.load_object(t_type, t_id)
            if loaded is None or not actor.has_access(loaded.security_tags):
                continue
            upstream.append(
                {
                    "direction": "upstream",
                    "link_type": l_type,
                    "source_type": ot,
                    "source_id": oid,
                    "target_type": t_type,
                    "target_id": t_id,
                    "peer_type": t_type,
                    "peer_id": t_id,
                }
            )

        in_rows = self.runtime.connection.execute(
            "SELECT link_type, source_type, source_id FROM ontology_links "
            "WHERE target_type = ? AND target_id = ? ORDER BY link_type, source_type, source_id",
            (ot, oid),
        ).fetchall()
        for r in in_rows:
            s_type, s_id, l_type = str(r["source_type"]), str(r["source_id"]), str(r["link_type"])
            loaded = self.runtime.load_object(s_type, s_id)
            if loaded is None or not actor.has_access(loaded.security_tags):
                continue
            downstream.append(
                {
                    "direction": "downstream",
                    "link_type": l_type,
                    "source_type": s_type,
                    "source_id": s_id,
                    "target_type": ot,
                    "target_id": oid,
                    "peer_type": s_type,
                    "peer_id": s_id,
                }
            )
        return upstream, downstream

    def get_object_lineage(
        self,
        object_type: str,
        object_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            root_cls = self.runtime.spec.get_object_type(object_type)
            root_obj = self.runtime.get_object(root_cls, object_id, actor)
            display = self._merged_display()
            obj_labels = display.get("objects") or {}

            nodes: dict[tuple[str, str], dict[str, Any]] = {}
            edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[str, str, str, str, str]] = set()

            def _ensure_node(t_name: str, o_id: str, hop: int, rel_dir: str) -> None:
                key = (t_name, o_id)
                if key in nodes:
                    return
                cls = self.runtime.spec.get_object_type(t_name)
                obj = self.runtime.get_object(cls, o_id, actor)
                serialized = self._serialize_instance(self.runtime, cls, obj)
                nodes[key] = {
                    **serialized,
                    "display_label": obj_labels.get(t_name) or t_name,
                    "hop": hop,
                    "relation": rel_dir,
                }

            def _add_edge(edge: Mapping[str, Any]) -> None:
                sig = (
                    str(edge["link_type"]),
                    str(edge["source_type"]),
                    str(edge["source_id"]),
                    str(edge["target_type"]),
                    str(edge["target_id"]),
                )
                if sig in seen_edges:
                    return
                seen_edges.add(sig)
                edges.append(dict(edge))

            root_pk = root_obj.primary_key_value()
            _ensure_node(root_cls.__name__, root_pk, 0, "root")

            # Expand 2 hops upstream & downstream within RLS, skipping fan-out across high-degree hubs
            _HUB_DEGREE_LIMIT = 6
            frontier: list[tuple[str, str, int, str]] = [
                (root_cls.__name__, root_pk, 0, "root")
            ]
            visited_expand: set[tuple[str, str]] = set()
            while frontier:
                cur_type, cur_id, cur_hop, cur_dir = frontier.pop(0)
                if (cur_type, cur_id) in visited_expand or cur_hop >= 2:
                    continue
                visited_expand.add((cur_type, cur_id))
                up_list, down_list = self._neighbor_edges(cur_type, cur_id, actor)
                if cur_hop == 0:
                    candidate_edges = [*up_list, *down_list]
                elif cur_dir == "upstream":
                    candidate_edges = up_list
                else:
                    candidate_edges = down_list

                for edge in candidate_edges:
                    p_type = str(edge["peer_type"])
                    p_id = str(edge["peer_id"])
                    if (p_type, p_id) == (root_cls.__name__, root_pk):
                        continue
                    node_dir = str(edge["direction"]) if cur_hop == 0 else cur_dir
                    try:
                        _ensure_node(p_type, p_id, cur_hop + 1, node_dir)
                    except (ObjectNotFoundError, PermissionDeniedError, UnknownObjectTypeError):
                        continue
                    _add_edge(edge)
                    if cur_hop + 1 < 2 and self._total_degree(p_type, p_id) <= _HUB_DEGREE_LIMIT:
                        frontier.append((p_type, p_id, cur_hop + 1, node_dir))

            # Attach generic target-referencing items (if binding present)
            if self.work_queue_binding is not None:
                wb = self.work_queue_binding
                q_cls = self.runtime.spec.object_types.get(wb.object_type)
                if q_cls is not None:
                    for item_obj in self.runtime.query_objects(q_cls, actor):
                        props = item_obj.model_dump(mode="json")
                        if (
                            str(props.get(wb.target_type_field, "")) == root_cls.__name__
                            and str(props.get(wb.target_id_field, "")) == root_pk
                        ):
                            iid = item_obj.primary_key_value()
                            _ensure_node(wb.object_type, iid, 1, "downstream")
                            _add_edge(
                                {
                                    "direction": "downstream",
                                    "link_type": "governs_target",
                                    "source_type": wb.object_type,
                                    "source_id": iid,
                                    "target_type": root_cls.__name__,
                                    "target_id": root_pk,
                                    "peer_type": wb.object_type,
                                    "peer_id": iid,
                                }
                            )

            all_nodes = list(nodes.values())
            return {
                "ok": True,
                "root": nodes[(root_cls.__name__, root_obj.primary_key_value())],
                "nodes": all_nodes,
                "edges": edges,
                "hops": {
                    "upstream": [n for n in all_nodes if n["relation"] == "upstream"],
                    "downstream": [n for n in all_nodes if n["relation"] == "downstream"],
                },
            }

    def simulate_action(
        self,
        payload: _ActionPayload,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            try:
                sim = self.runtime.simulate_action(payload.action, payload.params, actor)
                diff_dict = sim.diff.to_dict()
                return {
                    "ok": bool(sim.valid),
                    "valid": bool(sim.valid),
                    "status": "VALID" if sim.valid else "REJECTED",
                    "action": payload.action,
                    "read_versions": dict(sim.read_versions),
                    "diff": diff_dict,
                    "changes": _diff_summary(diff_dict, title_resolver=self._resolve_object_title),
                    "error_code": sim.error_code,
                    "error": sim.error,
                    "hint_zh": _translate_error_hint(sim.error_code, sim.error, display=display),
                    "zero_external_dirty_write": True,
                }
            except ValidationError as exc:
                hint = _format_validation_error_zh(exc, display)
                return {
                    "ok": False,
                    "valid": False,
                    "status": "REJECTED",
                    "action": payload.action,
                    "diff": {"object_changes": [], "link_changes": []},
                    "changes": [],
                    "error_code": "INVALID_PARAMETER",
                    "error": hint,
                    "hint_zh": hint,
                    "zero_external_dirty_write": True,
                }
            except InvariantError as exc:
                return {
                    "ok": False,
                    "valid": False,
                    "status": "REJECTED",
                    "action": payload.action,
                    "diff": {"object_changes": [], "link_changes": []},
                    "changes": [],
                    "error_code": exc.code,
                    "error": str(exc),
                    "hint_zh": _translate_error_hint(exc.code, str(exc), display=display),
                    "zero_external_dirty_write": True,
                }

    def submit_action(
        self,
        payload: _ActionPayload,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            run_id = (payload.run_id or "").strip() or f"RUN-{uuid.uuid4().hex[:12]}"
            try:
                decision = self.autonomy_controller.execute(
                    self.runtime,
                    actor,
                    run_id,
                    payload.action,
                    payload.params,
                )
            except ValidationError as exc:
                hint = _format_validation_error_zh(exc, display)
                return {
                    "ok": False,
                    "valid": False,
                    "committed": False,
                    "status": "REJECTED",
                    "outcome": "REJECTED",
                    "action": payload.action,
                    "run_id": run_id,
                    "diff": {"object_changes": [], "link_changes": []},
                    "changes": [],
                    "error_code": "INVALID_PARAMETER",
                    "error": hint,
                    "hint_zh": hint,
                    "zero_external_dirty_write": True,
                    "state_fingerprint": self.runtime.state_fingerprint(),
                }
            except InvariantError as exc:
                return {
                    "ok": False,
                    "valid": False,
                    "committed": False,
                    "status": "REJECTED",
                    "outcome": "REJECTED",
                    "action": payload.action,
                    "run_id": run_id,
                    "diff": {"object_changes": [], "link_changes": []},
                    "changes": [],
                    "error_code": exc.code,
                    "error": str(exc),
                    "hint_zh": _translate_error_hint(exc.code, str(exc), display=display),
                    "zero_external_dirty_write": True,
                    "state_fingerprint": self.runtime.state_fingerprint(),
                }

            sim_diff = decision.simulated.get("diff") or {"object_changes": [], "link_changes": []}
            run_obj = decision.run
            diff_dict = (run_obj.get("object_diff") if run_obj else None) or sim_diff
            ok = bool(decision.valid)
            return {
                "ok": ok,
                "valid": bool(decision.valid),
                "committed": bool(decision.committed),
                "mode": decision.mode,
                "effective_mode": decision.effective_mode,
                "status": (
                    "COMMITTED"
                    if decision.committed
                    else (decision.outcome if ok else "REJECTED")
                ),
                "outcome": decision.outcome,
                "action": payload.action,
                "run_id": decision.run_id,
                "run": run_obj,
                "escalation": decision.escalation,
                "diff": diff_dict,
                "changes": _diff_summary(diff_dict, title_resolver=self._resolve_object_title),
                "evaluated_value": decision.evaluated_value,
                "max_value": decision.max_value,
                "error_code": decision.error_code,
                "error": decision.error_message,
                "hint_zh": (
                    _translate_error_hint(
                        decision.error_code,
                        decision.error_message,
                        display=display,
                    )
                    if not ok
                    else ""
                ),
                "zero_external_dirty_write": True,
                "state_fingerprint": self.runtime.state_fingerprint(),
            }

    def resolve_work_queue_item(
        self,
        item_id: str,
        payload: _WorkQueueResolvePayload,
        actor: Actor,
    ) -> dict[str, Any]:
        """Human work-queue signoff executing RBAC, SoD, and $t_1$ invariants."""
        with self._lock:
            wb = self.work_queue_binding
            if wb is None:
                raise ActionNotFoundError("no active work_queue_binding registered on this runtime")
            type_name = (payload.item_type or wb.object_type).strip()
            cls = self.runtime.spec.get_object_type(type_name)
            item_obj = self.runtime.get_object(cls, item_id, actor)
            props = item_obj.model_dump(mode="json")

            decision_kind = payload.decision.strip().lower()
            if decision_kind not in ("approve", "accept", "reject", "decline"):
                raise ValueError("decision must be 'approve' or 'reject'")
            is_accept = decision_kind in ("approve", "accept")
            op_name = wb.accept_op if is_accept else wb.decline_op

            base_params: dict[str, Any] = {
                wb.id_param: item_id,
                wb.note_param: payload.note or ("核准通过" if is_accept else "审核驳回"),
            }
            item_kind = str(props.get(wb.kind_field, "")).strip()
            intent_action = str(props.get(wb.intent_action_field, "")).strip()
            raw_intent_json = str(props.get(wb.intent_params_field, "") or "{}").strip()
            try:
                intent_params = json.loads(raw_intent_json) if raw_intent_json else {}
            except json.JSONDecodeError:
                intent_params = {}

            run_id = f"WQ-{uuid.uuid4().hex[:12]}"
            target_act = self.runtime.spec.actions.get(intent_action) if intent_action else None
            needs_followup = False
            if is_accept and item_kind == "APPROVAL" and target_act is not None and isinstance(intent_params, dict):
                preflight_params = (
                    {**base_params, wb.settled_run_param: "PREFLIGHT"}
                    if (target_act.affects_source and wb.settled_run_param)
                    else base_params
                )
                sim_accept = self.runtime.simulate_action(op_name, preflight_params, actor)
                if not sim_accept.valid:
                    return self._rejected_resolution(op_name, run_id, sim_accept.error_code, sim_accept.error)
                if target_act.affects_source:
                    # Source-coupled approvals settle the intent first, then finalize queue item with settled_run_id
                    act_rid = f"WQ-ACT-{uuid.uuid4().hex[:12]}"
                    try:
                        settled_run = self.runtime.submit_action(act_rid, intent_action, intent_params, actor)
                        comp_params = {
                            **base_params,
                            wb.settled_run_param: str(settled_run.get("run_id", act_rid)),
                        }
                        comp_run = self.runtime.submit_action(run_id, op_name, comp_params, actor)
                    except InvariantError as exc:
                        return self._rejected_resolution(intent_action, run_id, exc.code, str(exc))
                    return self._committed_resolution(op_name, comp_run, follow_up_run=settled_run)

                touched_non_queue = any(
                    ch.object_type != wb.object_type for ch in sim_accept.diff.object_changes
                )
                if not touched_non_queue and intent_params:
                    needs_followup = True

            if needs_followup and target_act is not None:
                # Dry-run both steps in isolated fork so partial failure never leaves half-committed state
                fork_rt = self.runtime.fork()
                try:
                    fork_rt.submit_action(f"{run_id}-CHK-1", op_name, base_params, actor)
                    fork_rt.submit_action(f"{run_id}-CHK-2", intent_action, intent_params, actor)
                except InvariantError as exc:
                    return self._rejected_resolution(intent_action, run_id, exc.code, str(exc))
                finally:
                    fork_rt.close()

                comp_run = self.runtime.submit_action(run_id, op_name, base_params, actor)
                follow_run = self.runtime.submit_action(
                    f"WQ-INT-{uuid.uuid4().hex[:12]}",
                    intent_action,
                    intent_params,
                    actor,
                )
                return self._committed_resolution(op_name, comp_run, follow_up_run=follow_run)

            try:
                comp_run = self.runtime.submit_action(run_id, op_name, base_params, actor)
            except InvariantError as exc:
                return self._rejected_resolution(op_name, run_id, exc.code, str(exc))
            return self._committed_resolution(op_name, comp_run)

    def _rejected_resolution(
        self,
        action_name: str,
        run_id: str,
        error_code: str | None,
        error_message: str | None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "committed": False,
            "status": "REJECTED",
            "action": action_name,
            "run_id": run_id,
            "error_code": error_code,
            "error": error_message,
            "hint_zh": _translate_error_hint(
                error_code,
                error_message,
                display=self._merged_display(),
            ),
            "zero_external_dirty_write": True,
            "state_fingerprint": self.runtime.state_fingerprint(),
        }

    def _committed_resolution(
        self,
        action_name: str,
        run: dict[str, Any],
        *,
        follow_up_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diff_dict = run.get("object_diff") or {"object_changes": [], "link_changes": []}
        return {
            "ok": True,
            "committed": True,
            "status": str(run.get("status", "COMMITTED")),
            "action": action_name,
            "run_id": run.get("run_id"),
            "run": run,
            "follow_up_run": follow_up_run,
            "diff": diff_dict,
            "changes": _diff_summary(diff_dict),
            "zero_external_dirty_write": True,
            "state_fingerprint": self.runtime.state_fingerprint(),
        }

    def call_function(
        self,
        fn_name: str,
        params: Mapping[str, Any] | None,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            result = self.runtime.call_function(fn_name, dict(params or {}), actor)
            return {
                "ok": True,
                "function": fn_name,
                "result": result,
            }

    # -- parallel sandboxes (runtime.fork) ---------------------------------
    def create_sandbox(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            while len(self._sandboxes) >= _MAX_SANDBOXES:
                oldest_sid = next(iter(self._sandboxes))
                oldest = self._sandboxes.pop(oldest_sid)
                try:
                    oldest.fork_runtime.close()
                except Exception:
                    pass
            sid = f"SBX-{uuid.uuid4().hex[:8].upper()}"
            fork_rt = self.runtime.fork()
            base_fp = self.runtime.state_fingerprint()
            self._sandboxes[sid] = _SandboxSession(
                sandbox_id=sid,
                fork_runtime=fork_rt,
                base_fingerprint=base_fp,
                created_at=_utc_now_iso(),
                created_by=actor.actor_id,
            )
            return {
                "ok": True,
                "sandbox_id": sid,
                "world_mode": fork_rt.world_mode,
                "base_fingerprint": base_fp,
                "steps": [],
            }

    def _require_sandbox(self, sid: str, actor: Actor) -> _SandboxSession:
        session = self._sandboxes.get(sid)
        if session is None:
            raise ObjectNotFoundError(f"sandbox {sid!r} not found")
        if session.created_by != actor.actor_id and actor.role not in self.admin_roles:
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} cannot access sandbox {sid!r}"
            )
        return session

    def _resolve_sandbox_step_actor(self, requested_actor_id: str | None, fallback: Actor) -> Actor:
        if not requested_actor_id:
            return fallback
        is_privileged = fallback.role in self.admin_roles or "*" in fallback.clearance_tags
        if is_privileged and self.demo_actor_switch and requested_actor_id in self._demo_profiles:
            return self._demo_profiles[requested_actor_id].to_actor(self.org_directory)
        return fallback

    def submit_sandbox_action(
        self,
        sid: str,
        payload: _SandboxSubmitRequest,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            session = self._require_sandbox(sid, actor)
            steps = list(payload.steps or ())
            if not steps and payload.action:
                steps = [
                    _SandboxSubmitStep(
                        action=payload.action,
                        params=payload.params,
                        actor_id=payload.actor_id,
                    )
                ]
            if not steps:
                raise ValueError("sandbox submit requires 'action' or non-empty 'steps'")

            step_results: list[dict[str, Any]] = []
            final_error_code: str | None = None
            final_error_msg: str | None = None
            last_hint_zh = ""
            all_ok = True

            for idx, step in enumerate(steps, start=1):
                step_actor = self._resolve_sandbox_step_actor(step.actor_id, actor)
                step_run_id = f"{sid}-S{len(session.steps) + 1}-{idx}"
                try:
                    run = session.fork_runtime.submit_action(
                        step_run_id,
                        step.action,
                        step.params,
                        step_actor,
                    )
                    diff_dict = run.get("object_diff") or {"object_changes": [], "link_changes": []}
                    step_rec = {
                        "step_index": len(session.steps) + 1,
                        "action": step.action,
                        "params": dict(step.params),
                        "actor_id": step_actor.actor_id,
                        "run_id": step_run_id,
                        "status": str(run.get("status", "COMMITTED")),
                        "diff": diff_dict,
                        "changes": _diff_summary(
                            diff_dict,
                            title_resolver=self._resolve_object_title,
                        ),
                    }
                    session.steps.append(step_rec)
                    step_results.append({"ok": True, **step_rec})
                except ValidationError as exc:
                    all_ok = False
                    final_error_code = "INVALID_PARAMETER"
                    last_hint_zh = _format_validation_error_zh(exc, display)
                    final_error_msg = last_hint_zh
                    step_results.append(
                        {
                            "ok": False,
                            "action": step.action,
                            "params": dict(step.params),
                            "actor_id": step_actor.actor_id,
                            "status": "REJECTED",
                            "error_code": final_error_code,
                            "error": final_error_msg,
                            "hint_zh": last_hint_zh,
                        }
                    )
                    break
                except OpenOntologyError as exc:
                    all_ok = False
                    final_error_code = (
                        getattr(exc, "code", None)
                        or ("PERMISSION_DENIED" if isinstance(exc, PermissionDeniedError) else None)
                        or ("OBJECT_NOT_FOUND" if isinstance(exc, ObjectNotFoundError) else None)
                        or "SANDBOX_STEP_REJECTED"
                    )
                    final_error_msg = str(exc)
                    last_hint_zh = _translate_error_hint(
                        final_error_code,
                        final_error_msg,
                        display=display,
                    )
                    step_results.append(
                        {
                            "ok": False,
                            "action": step.action,
                            "params": dict(step.params),
                            "actor_id": step_actor.actor_id,
                            "status": "REJECTED",
                            "error_code": final_error_code,
                            "error": final_error_msg,
                            "hint_zh": last_hint_zh,
                        }
                    )
                    break

            current_primary_fp = self.runtime.state_fingerprint()
            return {
                "ok": all_ok,
                "sandbox_id": sid,
                "step_results": step_results,
                "steps": list(session.steps),
                "primary_fingerprint_unchanged": current_primary_fp == session.base_fingerprint,
                "base_fingerprint": session.base_fingerprint,
                "current_primary_fingerprint": current_primary_fp,
                "sandbox_fingerprint": session.fork_runtime.state_fingerprint(),
                "error_code": final_error_code,
                "error": final_error_msg,
                "hint_zh": last_hint_zh,
                "zero_external_dirty_write": True,
            }

    def compare_sandbox_function(
        self,
        sid: str,
        fn_name: str,
        params: Mapping[str, Any] | None,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._require_sandbox(sid, actor)
            p_dict = dict(params or {})

            def _invoke(rt: OntologyRuntime) -> Any:
                try:
                    return rt.call_function(fn_name, p_dict, actor)
                except OpenOntologyError as exc:
                    return {"error": str(exc), "error_code": getattr(exc, "code", None)}

            primary_res = _invoke(self.runtime)
            sandbox_res = _invoke(session.fork_runtime)
            return {
                "ok": True,
                "sandbox_id": sid,
                "function": fn_name,
                "primary": primary_res,
                "sandbox": sandbox_res,
            }

    def diff_sandbox(self, sid: str, actor: Actor) -> dict[str, Any]:
        with self._lock:
            session = self._require_sandbox(sid, actor)
            created: list[dict[str, Any]] = []
            updated: list[dict[str, Any]] = []
            deleted: list[dict[str, Any]] = []

            for ot_name, cls in self.runtime.spec.object_types.items():
                prim_map = {
                    obj.primary_key_value(): self._sanitize_properties(obj.model_dump(mode="json"))
                    for obj in self.runtime.query_objects(cls, actor)
                }
                sbx_map = {
                    obj.primary_key_value(): self._sanitize_properties(obj.model_dump(mode="json"))
                    for obj in session.fork_runtime.query_objects(cls, actor)
                }
                all_ids = sorted(set(prim_map.keys()) | set(sbx_map.keys()))
                for oid in all_ids:
                    p_props = prim_map.get(oid)
                    s_props = sbx_map.get(oid)
                    if p_props is None and s_props is not None:
                        created.append(
                            {
                                "object_type": ot_name,
                                "object_id": oid,
                                "object_title": self._resolve_object_title(ot_name, oid, None, s_props),
                                "after": s_props,
                            }
                        )
                    elif p_props is not None and s_props is None:
                        deleted.append(
                            {
                                "object_type": ot_name,
                                "object_id": oid,
                                "object_title": self._resolve_object_title(ot_name, oid, p_props, None),
                                "before": p_props,
                            }
                        )
                    elif p_props != s_props and p_props is not None and s_props is not None:
                        deltas = [
                            {
                                "field": k,
                                "primary": p_props.get(k),
                                "sandbox": s_props.get(k),
                            }
                            for k in sorted(set(p_props) | set(s_props))
                            if p_props.get(k) != s_props.get(k)
                        ]
                        updated.append(
                            {
                                "object_type": ot_name,
                                "object_id": oid,
                                "object_title": self._resolve_object_title(ot_name, oid, p_props, s_props),
                                "before": p_props,
                                "after": s_props,
                                "field_deltas": deltas,
                            }
                        )

            current_primary_fp = self.runtime.state_fingerprint()
            return {
                "ok": True,
                "sandbox_id": sid,
                "created": created,
                "updated": updated,
                "deleted": deleted,
                "total_changes": len(created) + len(updated) + len(deleted),
                "step_count": len(session.steps),
                "steps": list(session.steps),
                "primary_fingerprint_unchanged": current_primary_fp == session.base_fingerprint,
                "zero_external_dirty_write": True,
            }

    def promote_sandbox(self, sid: str, actor: Actor) -> dict[str, Any]:
        with self._lock:
            session = self._require_sandbox(sid, actor)
            if not session.steps:
                return {
                    "ok": True,
                    "sandbox_id": sid,
                    "promoted_count": 0,
                    "results": [],
                    "state_fingerprint": self.runtime.state_fingerprint(),
                }

            results: list[dict[str, Any]] = []
            all_ok = True
            for step in session.steps:
                res = self.submit_action(
                    _ActionPayload(
                        action=str(step["action"]),
                        params=dict(step.get("params") or {}),
                        run_id=f"PROMOTE-{sid}-{step['step_index']}-{uuid.uuid4().hex[:6]}",
                    ),
                    actor,
                )
                results.append(res)
                if not res.get("ok") or not res.get("committed"):
                    all_ok = False
                    break

            return {
                "ok": all_ok,
                "sandbox_id": sid,
                "promoted_count": sum(1 for r in results if r.get("committed")),
                "results": results,
                "zero_external_dirty_write": True,
                "state_fingerprint": self.runtime.state_fingerprint(),
            }

    def destroy_sandbox(self, sid: str, actor: Actor) -> dict[str, Any]:
        with self._lock:
            session = self._require_sandbox(sid, actor)
            session.fork_runtime.close()
            self._sandboxes.pop(sid, None)
            current_primary_fp = self.runtime.state_fingerprint()
            return {
                "ok": True,
                "sandbox_id": sid,
                "destroyed": True,
                "primary_fingerprint_unchanged": current_primary_fp == session.base_fingerprint,
                "state_fingerprint": current_primary_fp,
            }

    # -- pure-M2 audit / activity RLS filtering -----------------------------
    @staticmethod
    def _tags_from_snapshot(cls: type[Any] | None, snap: Mapping[str, Any] | None) -> list[str]:
        if cls is None or not isinstance(snap, Mapping):
            return []
        tags: list[str] = []
        for fld in getattr(cls, "__security_tag_fields__", ()) or ():
            val = snap.get(fld)
            if isinstance(val, str):
                s = val.strip()
                if s:
                    tags.append(f"{fld}:{s}")
            elif isinstance(val, (list, tuple, set)):
                for elem in val:
                    s = str(elem).strip()
                    if s:
                        tags.append(f"{fld}:{s}")
        return tags

    def _is_activity_visible_to_actor(
        self,
        *,
        action_name: str,
        run_actor_id: str,
        params: Mapping[str, Any] | None,
        diff: Mapping[str, Any] | None,
        read_versions: Mapping[str, Any] | None,
        actor: Actor,
    ) -> bool:
        """Pure-M2 fail-closed RLS check for action runs and shadow ledger entries."""
        if actor.role in self.admin_roles or "*" in actor.clearance_tags:
            return True

        items_by_key: dict[tuple[str, str], tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]] = {}
        queue: list[tuple[str, str, int]] = []
        enqueued_depth: dict[tuple[str, str], int] = {}

        def _add_item(
            ot: str,
            oid: str,
            before_m: Mapping[str, Any] | None,
            after_m: Mapping[str, Any] | None,
            depth: int,
        ) -> None:
            if not ot or not oid:
                return
            key = (ot, oid)
            prev_b, prev_a = items_by_key.get(key, (None, None))
            items_by_key[key] = (before_m or prev_b, after_m or prev_a)
            if key not in enqueued_depth or depth < enqueued_depth[key]:
                enqueued_depth[key] = depth
                queue.append((ot, oid, depth))

        for raw in (diff or {}).get("object_changes") or ():
            if not isinstance(raw, Mapping):
                continue
            ot = str(raw.get("object_type") or "")
            oid = str(raw.get("object_id") or "")
            b_m = raw.get("before") if isinstance(raw.get("before"), Mapping) else None
            a_m = raw.get("after") if isinstance(raw.get("after"), Mapping) else None
            _add_item(ot, oid, b_m, a_m, 0)

        for rk in (read_versions or {}).keys():
            if isinstance(rk, str) and ":" in rk:
                ot, _, oid = rk.partition(":")
                if ot in self.runtime.spec.object_types and oid:
                    _add_item(ot, oid, None, None, 0)

        if isinstance(params, Mapping):
            if self.work_queue_binding is not None:
                wb = self.work_queue_binding
                p_ot = params.get(wb.target_type_field)
                p_id = params.get(wb.target_id_field)
                if isinstance(p_ot, str) and p_ot and isinstance(p_id, str) and p_id:
                    _add_item(p_ot, p_id, None, None, 0)
            for val in params.values():
                if isinstance(val, str) and 0 < len(val) <= 128:
                    s_val = val.strip()
                    for ot_name in self.runtime.spec.object_types:
                        if self.runtime.load_object(ot_name, s_val) is not None:
                            _add_item(ot_name, s_val, None, None, 0)

        idx = 0
        while idx < len(queue):
            ot, oid, depth = queue[idx]
            idx += 1
            if depth >= 2:
                continue
            cls = self.runtime.spec.object_types.get(ot)
            loaded = self.runtime.load_object(ot, oid)
            before_m, after_m = items_by_key[(ot, oid)]
            cand_maps = [
                m
                for m in (before_m, after_m, loaded.data if loaded is not None else None)
                if isinstance(m, Mapping)
            ]
            if cls is not None:
                prop_map = getattr(cls, "__object_properties__", None) or {}
                for fname, decl in prop_map.items():
                    tgt_type = getattr(decl, "range", "")
                    if not tgt_type:
                        continue
                    for pmap in cand_maps:
                        raw_ref = pmap.get(fname)
                        if isinstance(raw_ref, str) and raw_ref:
                            _add_item(tgt_type, raw_ref, None, None, depth + 1)
                        elif isinstance(raw_ref, (list, tuple, set)):
                            for elem in raw_ref:
                                if isinstance(elem, str) and elem:
                                    _add_item(tgt_type, elem, None, None, depth + 1)
            if self.work_queue_binding is not None and ot == self.work_queue_binding.object_type:
                wb = self.work_queue_binding
                for pmap in cand_maps:
                    t_ot = pmap.get(wb.target_type_field)
                    t_id = pmap.get(wb.target_id_field)
                    if isinstance(t_ot, str) and t_ot and isinstance(t_id, str) and t_id:
                        _add_item(t_ot, t_id, None, None, depth + 1)

        has_accessible_tagged = False
        for (ot, oid), (before_m, after_m) in items_by_key.items():
            cls = self.runtime.spec.object_types.get(ot)
            loaded = self.runtime.load_object(ot, oid)
            tag_sets: list[list[str]] = []
            b_tags = self._tags_from_snapshot(cls, before_m)
            if b_tags:
                tag_sets.append(b_tags)
            a_tags = self._tags_from_snapshot(cls, after_m)
            if a_tags:
                tag_sets.append(a_tags)
            if loaded is not None and loaded.security_tags:
                tag_sets.append(list(loaded.security_tags))
            for tag_list in tag_sets:
                if not actor.has_access(tag_list):
                    return False
                has_accessible_tagged = True

        if has_accessible_tagged:
            return True

        if run_actor_id and run_actor_id == actor.actor_id:
            return True

        if self.work_queue_binding is not None:
            wb = self.work_queue_binding
            if action_name in (wb.accept_op, wb.decline_op, wb.claim_op or "") or any(
                ot == wb.object_type for ot, _ in items_by_key
            ):
                assignee_roles: set[str] = set()
                requesters: set[str] = set()
                if isinstance(params, Mapping):
                    rv = params.get(wb.assignee_role_field)
                    if isinstance(rv, str) and rv:
                        assignee_roles.add(rv)
                    rq = params.get(wb.requester_field)
                    if isinstance(rq, str) and rq:
                        requesters.add(rq)
                for (ot, oid), (before_m, after_m) in items_by_key.items():
                    if ot != wb.object_type:
                        continue
                    loaded = self.runtime.load_object(ot, oid)
                    for pmap in (
                        before_m,
                        after_m,
                        loaded.data if loaded is not None else None,
                    ):
                        if not isinstance(pmap, Mapping):
                            continue
                        rv = pmap.get(wb.assignee_role_field)
                        if isinstance(rv, str) and rv:
                            assignee_roles.add(rv)
                        rq = pmap.get(wb.requester_field)
                        if isinstance(rq, str) and rq:
                            requesters.add(rq)
                return actor.actor_id in requesters or actor.role in assignee_roles

        act_def = self.runtime.spec.actions.get(action_name)
        if act_def is None:
            return False
        if act_def.allowed_roles:
            return actor.role in act_def.allowed_roles
        return not actor.clearance_tags or "*" in actor.clearance_tags

    def _filter_runs_for_actor(
        self,
        runs: Sequence[dict[str, Any]],
        actor: Actor,
    ) -> list[dict[str, Any]]:
        return [
            r
            for r in runs
            if self._is_activity_visible_to_actor(
                action_name=str(r.get("action_type", "")),
                run_actor_id=str(r.get("actor_id", "")),
                params=r.get("params") if isinstance(r.get("params"), Mapping) else None,
                diff=r.get("object_diff") if isinstance(r.get("object_diff"), Mapping) else None,
                read_versions=(
                    r.get("read_versions") if isinstance(r.get("read_versions"), Mapping) else None
                ),
                actor=actor,
            )
        ]

    def _filter_shadow_for_actor(
        self,
        entries: Sequence[Any],
        actor: Actor,
    ) -> list[Any]:
        return [
            entry
            for entry in entries
            if self._is_activity_visible_to_actor(
                action_name=str(entry.action),
                run_actor_id=str(entry.actor_id),
                params=entry.params if isinstance(entry.params, Mapping) else None,
                diff=entry.diff if isinstance(entry.diff, Mapping) else None,
                read_versions=None,
                actor=actor,
            )
        ]

    @staticmethod
    def _build_shadow_report(entries: Sequence[Any]) -> dict[str, Any]:
        shadow_entries = [e for e in entries if e.mode == "SHADOW"]
        shadow_ok = sum(1 for e in shadow_entries if e.outcome == "SHADOW_RECORDED")
        shadow_rej = sum(1 for e in shadow_entries if e.outcome == "SHADOW_WOULD_REJECT")
        error_dist: dict[str, int] = {}
        outcome_dist: dict[str, int] = {}
        for entry in entries:
            outcome_dist[entry.outcome] = outcome_dist.get(entry.outcome, 0) + 1
            if entry.error_code:
                error_dist[entry.error_code] = error_dist.get(entry.error_code, 0) + 1
        return {
            "total": len(entries),
            "shadow_total": len(shadow_entries),
            "shadow_would_approve": shadow_ok,
            "shadow_would_reject": shadow_rej,
            "shadow_would_approve_rate": (
                round(shadow_ok / len(shadow_entries), 4) if shadow_entries else 0.0
            ),
            "auto_committed": sum(1 for e in entries if e.outcome == "AUTO_COMMITTED"),
            "escalated": sum(
                1 for e in entries if e.outcome in ("ESCALATED_COMMITTED", "ESCALATION_REQUIRED")
            ),
            "error_code_distribution": error_dist,
            "outcome_distribution": outcome_dist,
        }

    # -- audit ledger ------------------------------------------------------
    def get_audit(
        self,
        actor: Actor,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            act_labels = display.get("actions") or {}
            status_labels = display.get("statuses") or _STATUS_ZH
            status_arg = status.strip().upper() if status and status.strip().upper() in ("COMMITTED", "REJECTED", "REVERTED") else None
            runs = self._filter_runs_for_actor(
                self.runtime.list_action_runs(status_arg),
                actor,
            )
            visible_ledger = self._filter_shadow_for_actor(
                self.autonomy_controller.ledger,
                actor,
            )

            ledger_by_run = {entry.run_id: entry for entry in visible_ledger}
            enriched_runs: list[dict[str, Any]] = []
            for r in reversed(runs):
                rid = str(r["run_id"])
                led = ledger_by_run.get(rid)
                diff_obj = r.get("object_diff") or {}
                act_name = str(r.get("action_type", ""))
                err_code = r.get("error_code")
                err_msg = r.get("error_message")
                mode_tag = "auto"
                if led is not None:
                    if led.effective_mode == "SHADOW":
                        mode_tag = "shadow"
                    elif led.outcome in ("ESCALATED_COMMITTED", "ESCALATION_REQUIRED"):
                        mode_tag = "escalated"
                    elif led.outcome == "AUTO_COMMITTED":
                        mode_tag = "auto"
                elif r.get("approved_by") or act_name in (
                    self.work_queue_binding.accept_op if self.work_queue_binding else "",
                    self.work_queue_binding.decline_op if self.work_queue_binding else "",
                ):
                    mode_tag = "human"
                enriched_runs.append(
                    {
                        **r,
                        "action_label": act_labels.get(act_name) or act_name,
                        "actor_display_name": self._actor_display_name(str(r.get("actor_id", ""))),
                        "status_label": status_labels.get(str(r.get("status", "")), str(r.get("status", ""))),
                        "governance_channel": mode_tag,
                        "changes": _diff_summary(
                            diff_obj,
                            title_resolver=self._resolve_object_title,
                        ),
                        "hint_zh": (
                            _translate_error_hint(err_code, err_msg, display=display)
                            if err_code
                            else ""
                        ),
                        "external_dirty_writes": 0,
                    }
                )

            shadow_entries = [
                {
                    **entry.model_dump(mode="json"),
                    "action_label": act_labels.get(entry.action) or entry.action,
                    "actor_display_name": self._actor_display_name(entry.actor_id),
                    "status_label": status_labels.get(entry.outcome, entry.outcome),
                    "hint_zh": (
                        _translate_error_hint(
                            entry.error_code,
                            entry.error_message,
                            display=display,
                        )
                        if entry.error_code
                        else ""
                    ),
                    "external_dirty_writes": 0,
                }
                for entry in reversed(visible_ledger)
            ]

            return {
                "ok": True,
                "runs": enriched_runs,
                "shadow_entries": shadow_entries,
                "shadow_report": self._build_shadow_report(visible_ledger),
                "summary": {
                    "total_runs": len(runs),
                    "committed": sum(1 for r in runs if r["status"] == "COMMITTED"),
                    "rejected": sum(1 for r in runs if r["status"] == "REJECTED"),
                    "reverted": sum(1 for r in runs if r["status"] == "REVERTED"),
                    "external_dirty_writes": 0,
                },
            }

    # -- admin endpoints (strictly role-gated) -----------------------------
    def admin_devices(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            self._require_admin_ops(actor)
            display = self._merged_display()
            obj_labels = display.get("objects") or {}
            status_labels = display.get("statuses") or _STATUS_ZH
            reg = _extract_device_registry(self.runtime)

            devices: list[dict[str, Any]] = []
            coupled_types: set[str] = set()
            if reg is not None:
                for dev in reg.list_devices():
                    if isinstance(dev, dict):
                        item = {k: v for k, v in dev.items() if k != "bridge"}
                        owned_t = list(item.get("owned_object_types") or ())
                        shared_t = list(item.get("shared_object_types") or ())
                        coupled_types.update(owned_t)
                        devices.append({**item, "domain_mode": "coupled"})
                    else:
                        coupled_types.update(dev.owned_object_types)
                        devices.append(
                            {
                                "device_id": dev.device_id,
                                "name": dev.name,
                                "kind": dev.kind,
                                "endpoint": dev.endpoint,
                                "status": dev.status,
                                "status_label": status_labels.get(dev.status, dev.status),
                                "domain_mode": "coupled",
                                "owned_object_types": list(dev.owned_object_types),
                                "shared_object_types": list(dev.shared_object_types),
                                "handled_actions": list(dev.handled_actions),
                                "allow_source_revert": bool(dev.allow_source_revert),
                                "metadata": dict(dev.metadata or {}),
                            }
                        )
            elif self.runtime.source_bridge is not None:
                devices.append(
                    {
                        "device_id": "primary_source_bridge",
                        "name": "主外部系统连接桥",
                        "kind": "source_bridge",
                        "endpoint": "bound",
                        "status": "MOUNTED",
                        "status_label": "已连接",
                        "domain_mode": "coupled",
                        "owned_object_types": [],
                        "shared_object_types": [],
                        "handled_actions": [],
                        "allow_source_revert": bool(self.runtime.source_bridge.allow_source_revert),
                    }
                )

            standalone_domains = [
                {
                    "object_type": ot_name,
                    "display_label": obj_labels.get(ot_name) or ot_name,
                    "domain_mode": "standalone",
                    "owned": bool(cls.owned_spec()),
                }
                for ot_name, cls in self.runtime.spec.object_types.items()
                if ot_name not in coupled_types
            ]

            return {
                "ok": True,
                "world_mode": self.runtime.world_mode,
                "devices": devices,
                "standalone_object_types": standalone_domains,
                "coupled_object_types": sorted(coupled_types),
            }

    def admin_get_autonomy(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            self._require_admin_ops(actor)
            return {
                "ok": True,
                "policy": self.autonomy_controller.policy.to_dict(),
                "shadow_report": self.autonomy_controller.shadow_report(),
                "recent_ledger": [
                    entry.model_dump(mode="json")
                    for entry in reversed(self.autonomy_controller.ledger[-50:])
                ],
                "external_dirty_writes": 0,
            }

    def admin_update_autonomy(
        self,
        payload: _AutonomyUpdatePayload,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_admin_ops(actor)
            cur = self.autonomy_controller.policy
            new_default_mode = payload.default_mode or cur.default_mode
            new_esc_action = (
                payload.escalation_action
                if payload.escalation_action is not None
                else cur.default_escalation_action
            )
            new_esc_role = (
                payload.escalation_assignee_role
                if payload.escalation_assignee_role is not None
                else cur.default_escalation_assignee_role
            )

            if payload.rules is not None:
                new_rules = tuple(
                    ActionAutonomyRule(
                        action=r.action,
                        roles=tuple(r.roles),
                        mode=r.mode,
                        ceiling_field=r.ceiling_field,
                        max_value=r.max_value,
                        escalation_action=r.escalation_action or new_esc_action,
                        escalation_assignee_role=r.escalation_assignee_role or new_esc_role,
                    )
                    for r in payload.rules
                )
            else:
                cur_wildcard = next((r for r in cur.rules if r.action == "*"), None)
                other_rules = [r for r in cur.rules if r.action != "*"]
                eff_max = (
                    payload.max_value
                    if payload.max_value is not None
                    else (
                        cur_wildcard.max_value
                        if cur_wildcard and cur_wildcard.max_value is not None
                        else 500_000.0
                    )
                )
                eff_field = (
                    payload.ceiling_field
                    if payload.ceiling_field is not None
                    else (cur_wildcard.ceiling_field if cur_wildcard else None)
                )
                new_wildcard = ActionAutonomyRule(
                    action="*",
                    mode=new_default_mode,
                    ceiling_field=eff_field,
                    max_value=eff_max,
                    escalation_action=new_esc_action,
                    escalation_assignee_role=new_esc_role,
                )
                new_rules = (new_wildcard, *other_rules)

            updated_policy = AutonomyPolicy(
                default_mode=new_default_mode,
                default_escalation_action=new_esc_action,
                default_escalation_assignee_role=new_esc_role,
                rules=new_rules,
            )
            self.autonomy_controller.policy = updated_policy
            return {
                "ok": True,
                "policy": updated_policy.to_dict(),
                "shadow_report": self.autonomy_controller.shadow_report(),
                "external_dirty_writes": 0,
            }

    def admin_org(self, actor: Actor) -> dict[str, Any]:
        with self._lock:
            self._require_admin_ops(actor)
            units_out: list[dict[str, Any]] = []
            members_out: list[dict[str, Any]] = []
            if self.org_directory is not None:
                units_out = [
                    {
                        "unit_id": u.unit_id,
                        "name": u.name,
                        "parent_id": u.parent_id,
                        "unit_kind": u.unit_kind,
                        "scope_tag": u.scope_tag,
                    }
                    for u in self.org_directory.list_units()
                ]
                for m in self.org_directory.list_members():
                    try:
                        resolved_actor = self.org_directory.resolve_actor(m.user_id)
                        eff_tags = sorted(resolved_actor.clearance_tags)
                    except OrgAuthorityError:
                        eff_tags = []
                    members_out.append(
                        {
                            "user_id": m.user_id,
                            "display_name": m.display_name,
                            "role": m.role,
                            "unit_id": m.unit_id,
                            "title": m.title,
                            "active": m.active,
                            "clearance_tags": eff_tags,
                        }
                    )

            behavior = describe_behavior(self.runtime.spec, actor=actor)
            return {
                "ok": True,
                "units": units_out,
                "members": members_out,
                "roles": behavior["roles"],
            }

    # -- NL assistant (Pure M2 LLM sandbox agent + offline fallback) -------
    @staticmethod
    def _build_m2_llm_tools() -> list[dict[str, Any]]:
        """Declare strictly read + isolated-sandbox tools (never primary write/promote)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_objects",
                    "description": "查询当前用户权限可见范围内的本体对象列表（可查主世界或当前平行沙盘）。执行任何修改动作前应先调用本工具匹配对象主键与当前字段。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "object_type": {
                                "type": "string",
                                "description": "本体对象类型名称",
                            },
                            "in_sandbox": {
                                "type": "boolean",
                                "description": "是否读取当前平行沙盘分支状态；默认 false 读主世界",
                            },
                        },
                        "required": ["object_type"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_sandbox_action",
                    "description": "在当前用户专属的隔离平行沙盘（runtime.fork）中试演一次受治理动作，绝不修改真实主世界。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action_name": {
                                "type": "string",
                                "description": "要执行的动作内部名称",
                            },
                            "params": {
                                "type": "object",
                                "description": "传递给该动作的参数字典",
                            },
                        },
                        "required": ["action_name", "params"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_sandbox_diff",
                    "description": "获取当前平行沙盘相对真实主世界的全部对象新建/属性变更/删除差异。",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_function",
                    "description": "调用只读本体聚合或衍生查询函数（支持在主世界或当前沙盘运行）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "fn_name": {
                                "type": "string",
                                "description": "只读函数内部名称",
                            },
                            "params": {
                                "type": "object",
                                "description": "函数参数字典",
                            },
                            "in_sandbox": {
                                "type": "boolean",
                                "description": "是否在当前平行沙盘中求值，默认 false",
                            },
                        },
                        "required": ["fn_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_audit_summary",
                    "description": "查询当前用户权限可见范围内的审计运行记录摘要与最近合规拦截情况。",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
        ]

    @staticmethod
    def _sanitize_llm_history(
        raw_history: Sequence[Mapping[str, Any]] | None,
    ) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for entry in raw_history or ():
            if not isinstance(entry, Mapping):
                continue
            role = str(entry.get("role") or "").strip().lower()
            content = entry.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                text = content.strip()
                if text:
                    cleaned.append({"role": role, "content": text[:1200]})
        return cleaned[-8:]

    def _build_m2_llm_system_prompt(
        self,
        actor: Actor,
        active_sid: str | None,
    ) -> str:
        """Build a 100% domain-agnostic system prompt purely from spec + merged display."""
        display = self._merged_display()
        obj_labels = display.get("objects") or {}
        act_labels = display.get("actions") or {}
        fn_labels = display.get("functions") or {}
        field_labels = display.get("fields") or {}
        status_labels = display.get("statuses") or {}
        value_labels = display.get("values") or {}
        role_labels = display.get("roles") or {}

        scoped_tbox = describe_tbox(self.runtime.spec, actor=actor)
        scoped_behavior = describe_behavior(self.runtime.spec, actor=actor)
        scoped_action_names = [a["name"] for a in scoped_behavior["actions"]]
        prof = self._actor_profile(actor)

        entities_info: list[dict[str, Any]] = []
        for ot_desc in scoped_tbox["object_types"]:
            ot_name = ot_desc["name"]
            cls = self.runtime.spec.object_types.get(ot_name)
            fields_brief = []
            for f in ot_desc.get("fields", ()):
                fname = f["name"]
                f_info: dict[str, Any] = {
                    "name": fname,
                    "label": field_labels.get(fname) or fname,
                    "type": f.get("type"),
                }
                if f.get("masked"):
                    f_info["masked"] = True
                if f.get("choices"):
                    f_info["choices"] = list(f["choices"])
                fields_brief.append(f_info)
            entities_info.append(
                {
                    "name": ot_name,
                    "display_label": obj_labels.get(ot_name) or ot_name,
                    "primary_key": ot_desc.get("primary_key"),
                    "status_field": ot_desc.get("status_field"),
                    "transitions": (
                        getattr(cls, "__transitions__", {}) if cls is not None else {}
                    ),
                    "fields": fields_brief,
                }
            )

        actions_info: list[dict[str, Any]] = []
        for aname in scoped_action_names:
            act_def = self.runtime.spec.actions.get(aname)
            if act_def is None:
                continue
            pschema = _enrich_action_params_schema(act_def)
            actions_info.append(
                {
                    "name": aname,
                    "display_label": act_labels.get(aname) or aname,
                    "description": str(act_def.description or ""),
                    "touched_object_types": list(act_def.touched_object_types or ()),
                    "params_schema": {
                        "properties": pschema.get("properties", {}),
                        "required": pschema.get("required", []),
                    },
                }
            )

        functions_info = [
            {
                "name": fn_name,
                "display_label": fn_labels.get(fn_name) or fn_name,
                "description": str(fn_def.description or ""),
            }
            for fn_name, fn_def in sorted(self.runtime.spec.functions.items())
        ]

        vocab_hints: dict[str, str] = {}
        for bucket in (value_labels, status_labels):
            for k, v in bucket.items():
                ks = str(k).strip()
                vs = str(v).strip()
                if ks and vs and ks not in vocab_hints:
                    vocab_hints[ks] = vs

        presets_hint = [
            {
                "title": p.get("title"),
                "steps": p.get("steps", []),
            }
            for p in self._visible_sandbox_presets(actor)[:6]
        ]

        catalog = {
            "actor": {
                "actor_id": actor.actor_id,
                "display_name": prof.get("display_name"),
                "role": actor.role,
                "role_label": role_labels.get(actor.role) or prof.get("title") or actor.role,
                "clearance_tags": sorted(actor.clearance_tags),
            },
            "active_sandbox_id": active_sid,
            "object_types": entities_info,
            "allowed_actions": actions_info,
            "read_only_functions": functions_info,
            "value_dictionary": dict(list(vocab_hints.items())[:40]),
            "sandbox_preset_examples": presets_hint,
        }
        catalog_json = json.dumps(catalog, ensure_ascii=False)
        return (
            "你是运行在受治理本体控制台上的 AI 数字智能管家（Pure M2 沙盘代理）。\n"
            "严格遵守以下运行规范：\n"
            "1. 当用户请求查询记录、执行操作、改变状态或进行假设推演时，先调用 `list_objects` 查看可见对象，依据标题、呼号或属性匹配真实主键编号（严禁臆测不存在的主键或字段值）。\n"
            "2. 所有状态变更只能通过 `run_sandbox_action` 在隔离平行沙盘中执行，绝对不会污染真实主世界；若用户未明确某个有参考示例或最小正值的参数，优先使用参数 schema 默认值或预设示例中的典型值（切忌传入越界或负值导致不必要的参数错误）。\n"
            "3. 将自然语言描述映射为对应类型和动作 schema 中声明的精确代码值（参考 `value_dictionary` 与字段 `enum`）。\n"
            "4. 若权限校验（`PERMISSION_DENIED`）、行级隔离（`OBJECT_NOT_FOUND`）或本体不变式/状态机护栏拒绝执行，请如实说明拦截原因，绝不编造成功结果。\n"
            "5. 成功在沙盘推演后，用简洁清晰的中文汇报动作结果与字段前后对比，明确指出真实主世界保持零脏写，并提示用户可点击下方卡片进入平行沙盘页面核对或晋升落盘。\n\n"
            f"当前权限作用域内的本体目录：\n{catalog_json}"
        )

    def _execute_m2_assistant_tool(
        self,
        tool_name: str,
        raw_args: Mapping[str, Any],
        actor: Actor,
        sandbox_holder: dict[str, Any],
        tool_trace: list[dict[str, Any]],
        discovered_objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            display = self._merged_display()
            obj_labels = display.get("objects") or {}
            act_labels = display.get("actions") or {}
            fn_labels = display.get("functions") or {}
            active_sid: str | None = sandbox_holder.get("sid")

            if tool_name == "list_objects":
                ot_name = str(raw_args.get("object_type") or "").strip()
                in_sbx = bool(raw_args.get("in_sandbox", False))
                rt_override = None
                if in_sbx and active_sid:
                    rt_override = self._require_sandbox(active_sid, actor).fork_runtime
                ot_label = obj_labels.get(ot_name) or ot_name or "对象"
                try:
                    listed = self.list_objects(ot_name, actor, runtime_override=rt_override)
                except (OpenOntologyError, ValueError) as exc:
                    err_code = getattr(exc, "code", None) or "UNKNOWN_OBJECT_TYPE"
                    tool_trace.append(
                        {"tool": "list_objects", "summary": f"查询 {ot_label} 失败", "ok": False}
                    )
                    return {"ok": False, "error_code": err_code, "error": str(exc)}
                compact_items = [
                    {
                        "object_id": it["object_id"],
                        "title": it["title"],
                        "properties": it["properties"],
                    }
                    for it in listed["items"][:25]
                ]
                for it in compact_items[:6]:
                    discovered_objects.append(
                        {
                            "object_type": listed["object_type"],
                            "object_id": it["object_id"],
                            "title": f"{listed['display_label']} · {it['title']}",
                            "subtitle": f"编号 {it['object_id']}",
                        }
                    )
                tool_trace.append(
                    {
                        "tool": "list_objects",
                        "summary": f"查询 {listed['display_label']}（{listed['count']} 条）",
                        "ok": True,
                    }
                )
                return {
                    "ok": True,
                    "object_type": listed["object_type"],
                    "display_label": listed["display_label"],
                    "primary_key": listed["primary_key"],
                    "count": listed["count"],
                    "in_sandbox": bool(in_sbx and active_sid),
                    "items": compact_items,
                }

            if tool_name == "run_sandbox_action":
                action_name = str(raw_args.get("action_name") or "").strip()
                raw_params = raw_args.get("params")
                params_dict = dict(raw_params) if isinstance(raw_params, Mapping) else {}
                act_label = act_labels.get(action_name) or action_name or "未知动作"

                if not active_sid:
                    created = self.create_sandbox(actor)
                    active_sid = str(created["sandbox_id"])
                    sandbox_holder["sid"] = active_sid
                else:
                    self._require_sandbox(active_sid, actor)

                try:
                    sub = self.submit_sandbox_action(
                        active_sid,
                        _SandboxSubmitRequest(
                            steps=[_SandboxSubmitStep(action=action_name, params=params_dict)]
                        ),
                        actor,
                    )
                except (OpenOntologyError, ValueError) as exc:
                    err_code = getattr(exc, "code", None) or "SANDBOX_STEP_REJECTED"
                    hint = _translate_error_hint(err_code, str(exc), display=display)
                    tool_trace.append(
                        {
                            "tool": "run_sandbox_action",
                            "summary": f"沙盘执行 {act_label}（拦截：{err_code}）",
                            "ok": False,
                        }
                    )
                    return {
                        "ok": False,
                        "sandbox_id": active_sid,
                        "error_code": err_code,
                        "error": str(exc),
                        "hint_zh": hint,
                    }

                diff = self.diff_sandbox(active_sid, actor)
                sandbox_holder["last_diff"] = diff
                if sub.get("ok"):
                    tool_trace.append(
                        {
                            "tool": "run_sandbox_action",
                            "summary": f"沙盘执行 {act_label}（{diff['total_changes']} 项变更）",
                            "ok": True,
                        }
                    )
                else:
                    err_code = str(sub.get("error_code") or "REJECTED")
                    tool_trace.append(
                        {
                            "tool": "run_sandbox_action",
                            "summary": f"沙盘执行 {act_label}（拦截：{err_code}）",
                            "ok": False,
                        }
                    )
                return {
                    "ok": bool(sub.get("ok")),
                    "sandbox_id": active_sid,
                    "step_results": sub.get("step_results", []),
                    "diff_summary": {
                        "step_count": diff["step_count"],
                        "total_changes": diff["total_changes"],
                        "updated": diff["updated"],
                        "created": diff["created"],
                        "deleted": diff["deleted"],
                    },
                    "error_code": sub.get("error_code"),
                    "error": sub.get("error"),
                    "hint_zh": sub.get("hint_zh"),
                    "primary_fingerprint_unchanged": sub.get("primary_fingerprint_unchanged", True),
                }

            if tool_name == "get_sandbox_diff":
                if not active_sid:
                    tool_trace.append(
                        {"tool": "get_sandbox_diff", "summary": "当前尚无沙盘改动", "ok": True}
                    )
                    return {
                        "ok": True,
                        "sandbox_id": None,
                        "step_count": 0,
                        "total_changes": 0,
                        "updated": [],
                        "created": [],
                        "deleted": [],
                    }
                diff = self.diff_sandbox(active_sid, actor)
                sandbox_holder["last_diff"] = diff
                tool_trace.append(
                    {
                        "tool": "get_sandbox_diff",
                        "summary": f"核对沙盘差异（{diff['step_count']} 步 · {diff['total_changes']} 项）",
                        "ok": True,
                    }
                )
                return diff

            if tool_name == "run_function":
                fn_name = str(raw_args.get("fn_name") or "").strip()
                raw_params = raw_args.get("params")
                params_dict = dict(raw_params) if isinstance(raw_params, Mapping) else {}
                in_sbx = bool(raw_args.get("in_sandbox", False))
                fn_label = fn_labels.get(fn_name) or fn_name or "函数"
                rt_target = self.runtime
                if in_sbx and active_sid:
                    rt_target = self._require_sandbox(active_sid, actor).fork_runtime
                try:
                    result = rt_target.call_function(fn_name, params_dict, actor)
                    tool_trace.append(
                        {"tool": "run_function", "summary": f"计算指标 {fn_label}", "ok": True}
                    )
                    return {
                        "ok": True,
                        "function": fn_name,
                        "in_sandbox": bool(in_sbx and active_sid),
                        "result": result,
                    }
                except (OpenOntologyError, ValidationError, ValueError) as exc:
                    err_code = getattr(exc, "code", None) or "FUNCTION_ERROR"
                    tool_trace.append(
                        {"tool": "run_function", "summary": f"计算 {fn_label} 失败", "ok": False}
                    )
                    return {"ok": False, "error_code": err_code, "error": str(exc)}

            if tool_name == "get_audit_summary":
                aud = self.get_audit(actor)
                tool_trace.append(
                    {
                        "tool": "get_audit_summary",
                        "summary": f"检索审计记录（{aud['summary']['total_runs']} 条）",
                        "ok": True,
                    }
                )
                compact_runs = [
                    {
                        "run_id": r["run_id"],
                        "action_type": r["action_type"],
                        "action_label": r["action_label"],
                        "status": r["status"],
                        "error_code": r.get("error_code"),
                        "hint_zh": r.get("hint_zh"),
                    }
                    for r in aud["runs"][:8]
                ]
                return {"ok": True, "summary": aud["summary"], "recent_runs": compact_runs}

            tool_trace.append(
                {"tool": tool_name, "summary": f"未知工具 {tool_name}", "ok": False}
            )
            return {
                "ok": False,
                "error_code": "UNKNOWN_TOOL",
                "error": f"unsupported tool {tool_name!r}",
            }

    def _run_llm_sandbox_assistant(
        self,
        question: str,
        payload: _AssistantPayload,
        actor: Actor,
        *,
        initial_sid: str | None,
        cfg: _LlmEndpointConfig | None,
        model_label: str,
    ) -> dict[str, Any]:
        """Run multi-round OpenAI tool-calling loop with lock released during HTTP I/O."""
        with self._lock:
            if initial_sid:
                self._require_sandbox(initial_sid, actor)
            sys_prompt = self._build_m2_llm_system_prompt(actor, initial_sid)
        tools = self._build_m2_llm_tools()
        history = self._sanitize_llm_history(payload.history)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            *history,
            {"role": "user", "content": question},
        ]

        def _invoke(body: Mapping[str, Any]) -> Mapping[str, Any]:
            if self.assistant_llm_client is not None:
                return self.assistant_llm_client(dict(body))
            assert cfg is not None
            return _default_openai_compatible_chat(cfg, body, timeout_s=25.0)

        sandbox_holder: dict[str, Any] = {"sid": initial_sid, "last_diff": None}
        tool_trace: list[dict[str, Any]] = []
        discovered_objects: list[dict[str, Any]] = []
        final_reply = ""
        llm_error_note = ""

        try:
            for _ in range(5):
                resp = _invoke(
                    {
                        "model": model_label,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "temperature": 0.1,
                    }
                )
                choices = resp.get("choices") if isinstance(resp, Mapping) else None
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError("LLM returned empty choices")
                first = choices[0]
                msg = first.get("message") if isinstance(first, Mapping) else None
                if not isinstance(msg, Mapping):
                    raise RuntimeError("LLM returned invalid choice message")
                raw_tool_calls = msg.get("tool_calls") or []
                if not raw_tool_calls:
                    final_reply = str(msg.get("content") or "").strip()
                    break

                assistant_turn: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": [dict(tc) for tc in raw_tool_calls],
                }
                if msg.get("reasoning_content") is not None:
                    assistant_turn["reasoning_content"] = msg.get("reasoning_content")
                messages.append(assistant_turn)

                for tc in raw_tool_calls:
                    tc_id = str(tc.get("id") or f"tc-{uuid.uuid4().hex[:8]}")
                    fn_obj = tc.get("function") if isinstance(tc, Mapping) else None
                    t_name = str(fn_obj.get("name") or "") if isinstance(fn_obj, Mapping) else ""
                    raw_args_val = (
                        fn_obj.get("arguments") if isinstance(fn_obj, Mapping) else {}
                    )
                    parsed_args: dict[str, Any] = {}
                    if isinstance(raw_args_val, Mapping):
                        parsed_args = dict(raw_args_val)
                    elif isinstance(raw_args_val, str) and raw_args_val.strip():
                        try:
                            loaded = json.loads(raw_args_val)
                            if isinstance(loaded, Mapping):
                                parsed_args = dict(loaded)
                        except json.JSONDecodeError:
                            parsed_args = {}

                    t_res = self._execute_m2_assistant_tool(
                        t_name,
                        parsed_args,
                        actor,
                        sandbox_holder,
                        tool_trace,
                        discovered_objects,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(t_res, ensure_ascii=False),
                        }
                    )

            if not final_reply and tool_trace:
                try:
                    synth = _invoke(
                        {
                            "model": model_label,
                            "messages": messages,
                            "temperature": 0.1,
                        }
                    )
                    s_choices = synth.get("choices") if isinstance(synth, Mapping) else None
                    if isinstance(s_choices, list) and s_choices:
                        s_msg = s_choices[0].get("message") or {}
                        final_reply = str(s_msg.get("content") or "").strip()
                except Exception:
                    final_reply = ""
        except Exception as exc:
            llm_error_note = f"大模型推演调用暂时不可用（{str(exc)[:160]}）。"

        final_sid: str | None = sandbox_holder.get("sid")
        final_diff: dict[str, Any] | None = None
        if final_sid:
            with self._lock:
                try:
                    final_diff = self.diff_sandbox(final_sid, actor)
                except OpenOntologyError:
                    final_diff = None

        cards: list[dict[str, Any]] = []
        highlights: list[dict[str, str]] = []
        if final_sid and final_diff and int(final_diff.get("step_count", 0)) > 0:
            step_count = int(final_diff["step_count"])
            total_changes = int(final_diff["total_changes"])
            cards.append(
                {
                    "kind": "sandbox_session",
                    "title": "平行沙盘推演已就绪",
                    "subtitle": f"共 {step_count} 步推演 · {total_changes} 项变更（点击进入沙盘核对或晋升落盘）",
                    "sandbox_id": final_sid,
                    "route": "/sandbox",
                }
            )
            highlights = [
                {"label": "沙盘推演步数", "value": str(step_count)},
                {"label": "变更记录数", "value": str(total_changes)},
                {"label": "主世界脏写", "value": "0"},
            ]
        elif discovered_objects:
            highlights = [
                {"label": "已检索对象", "value": str(len(discovered_objects))},
                {"label": "主世界脏写", "value": "0"},
            ]

        seen_obj_keys: set[tuple[str, str]] = set()
        for item in discovered_objects:
            k = (str(item["object_type"]), str(item["object_id"]))
            if k in seen_obj_keys:
                continue
            seen_obj_keys.add(k)
            cards.append(
                {
                    "kind": "object",
                    "title": item["title"],
                    "subtitle": item["subtitle"],
                    "object_type": item["object_type"],
                    "object_id": item["object_id"],
                }
            )
            if len(cards) >= 6:
                break

        if llm_error_note:
            reply_text = llm_error_note
        elif final_reply:
            reply_text = final_reply
        elif final_diff and int(final_diff.get("step_count", 0)) > 0:
            reply_text = (
                f"已在隔离平行沙盘 {final_sid} 完成 {final_diff['step_count']} 步推演"
                f"（涉及 {final_diff['total_changes']} 项变更），主世界保持零脏写。"
            )
        else:
            reply_text = "已完成当前权限作用域内的检索分析。"

        return {
            "ok": True,
            "reply": reply_text,
            "highlights": highlights,
            "cards": cards,
            "assistant_mode": "llm_sandbox",
            "assistant_model": model_label,
            "sandbox_id": final_sid,
            "tool_trace": tool_trace,
            "rls_scope": sorted(actor.clearance_tags),
        }

    def assistant_chat(
        self,
        payload: _AssistantPayload,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            question = (payload.message or payload.question or "").strip()
            raw_sid = (payload.sandbox_id or "").strip() if payload.sandbox_id else ""
            if raw_sid:
                self._require_sandbox(raw_sid, actor)

            llm_ready, cfg, model_label = self._resolve_llm_mode_and_cfg()
            if not question:
                return {
                    "ok": True,
                    "reply": "请输入您想查询的运行指标、记录异常、待办审批或假设推演问题。",
                    "highlights": [],
                    "cards": [],
                    "assistant_mode": "llm_sandbox" if llm_ready else "offline_fallback",
                    "assistant_model": model_label,
                    "sandbox_id": raw_sid or None,
                    "tool_trace": [],
                    "rls_scope": sorted(actor.clearance_tags),
                }

            if not llm_ready:
                if self.assistant_responder is not None:
                    visible_runs = self._filter_runs_for_actor(
                        self.runtime.list_action_runs(),
                        actor,
                    )
                    custom = self.assistant_responder(
                        self.runtime,
                        actor,
                        question,
                        {
                            "display": self._merged_display(),
                            "work_queue": (
                                self.work_queue_binding.model_dump(mode="json")
                                if self.work_queue_binding
                                else None
                            ),
                            "visible_presets": self._visible_sandbox_presets(actor),
                            "visible_runs": visible_runs,
                            "visible_rejected_runs": [
                                r for r in visible_runs if r.get("status") == "REJECTED"
                            ],
                        },
                    )
                    return {
                        "ok": True,
                        **custom,
                        "assistant_mode": "offline_fallback",
                        "assistant_model": None,
                        "sandbox_id": raw_sid or custom.get("sandbox_id"),
                        "tool_trace": custom.get("tool_trace", []),
                        "rls_scope": sorted(actor.clearance_tags),
                    }

                offline = self._generic_m2_assistant(question, actor)
                if raw_sid and not offline.get("sandbox_id"):
                    offline["sandbox_id"] = raw_sid
                return offline

        # Release self._lock before issuing external LLM network I/O
        return self._run_llm_sandbox_assistant(
            question,
            payload,
            actor,
            initial_sid=raw_sid or None,
            cfg=cfg,
            model_label=model_label or "deepseek-flash",
        )

    def _generic_m2_assistant(self, question: str, actor: Actor) -> dict[str, Any]:
        display = self._merged_display()
        obj_labels = display.get("objects") or {}
        role_labels = display.get("roles") or {}
        act_labels = display.get("actions") or {}
        type_summaries: list[str] = []
        matched_cards: list[dict[str, Any]] = []
        lower_q = question.lower()
        total_visible = 0

        for ot_name, cls in self.runtime.spec.object_types.items():
            visible = self.runtime.query_objects(cls, actor)
            total_visible += len(visible)
            ot_label = obj_labels.get(ot_name) or ot_name
            if visible:
                type_summaries.append(f"{ot_label} {len(visible)} 条")
            for obj in visible:
                oid = obj.primary_key_value()
                title = obj.title_value() or oid
                blob = f"{ot_name} {ot_label} {oid} {title} {obj.to_properties()}".lower()
                if any(tok in blob for tok in lower_q.split() if len(tok) >= 2):
                    matched_cards.append(
                        {
                            "kind": "object",
                            "title": f"{ot_label} · {title}",
                            "subtitle": f"编号 {oid}",
                            "object_type": ot_name,
                            "object_id": oid,
                        }
                    )
                elif ot_name.lower() in lower_q or ot_label.lower() in lower_q:
                    matched_cards.append(
                        {
                            "kind": "object",
                            "title": f"{ot_label} · {title}",
                            "subtitle": f"编号 {oid}",
                            "object_type": ot_name,
                            "object_id": oid,
                        }
                    )

        runs = self._filter_runs_for_actor(
            self.runtime.list_action_runs(),
            actor,
        )
        rejected = [r for r in runs if r["status"] == "REJECTED"]
        prof = self._actor_profile(actor)
        role_zh = role_labels.get(actor.role) or prof.get("title") or actor.role
        reply = (
            f"当前以「{prof['display_name']}（{role_zh}）」权限视角共检索到 "
            f"{total_visible} 条可见记录：" + ("、".join(type_summaries[:6]) or "暂无数据") + "。"
        )
        if rejected:
            last_rej = rejected[-1]
            act_zh = act_labels.get(last_rej["action_type"]) or last_rej["action_type"]
            err_zh = _translate_error_hint(
                last_rej.get("error_code"),
                last_rej.get("error_message"),
                display=display,
            )
            reply += (
                f" 最近一次红线拦截为「{act_zh}」"
                + (f"（{err_zh}）" if err_zh else "。")
            )

        action_intent_tokens = (
            "沙盘",
            "推演",
            "模拟",
            "试演",
            "执行",
            "修改",
            "更新",
            "变更",
            "跃迁",
            "创建",
            "删除",
            "sandbox",
            "execute",
        )
        mentions_action = any(tok in lower_q for tok in action_intent_tokens) or any(
            aname.lower() in lower_q or str(act_labels.get(aname) or "").lower() in lower_q
            for aname in self.runtime.spec.actions
            if len(aname) >= 2
        )
        if mentions_action:
            reply += (
                "（当前处于离线检索模式，未连接大模型推理引擎来自动执行动作；"
                "请在环境变量配置 DEEPSEEK_API_KEY 后重启控制台，或前往「平行沙盘推演」页面手动试演。）"
            )

        return {
            "ok": True,
            "reply": reply,
            "highlights": [
                {"label": "可见对象总数", "value": str(total_visible)},
                {"label": "累计审计记录", "value": str(len(runs))},
                {"label": "红线拦截次数", "value": str(len(rejected))},
            ],
            "cards": matched_cards[:6],
            "assistant_mode": "offline_fallback",
            "assistant_model": None,
            "sandbox_id": None,
            "tool_trace": [],
            "rls_scope": sorted(actor.clearance_tags),
        }

    # -- M1 Isolated DOAL Drafts & Ontology Expert Agent -------------------
    def _create_m1_draft(self, actor: Actor) -> _M1DraftSession:
        from .induction.dsl import CustomerOverlaySpec

        while len(self._m1_drafts) >= _MAX_M1_DRAFTS:
            oldest_id = min(
                self._m1_drafts,
                key=lambda k: self._m1_drafts[k].last_used_at,
            )
            self._m1_drafts.pop(oldest_id, None)
        now = _utc_now_iso()
        did = f"M1D-{uuid.uuid4().hex[:8].upper()}"
        display = self._merged_display()
        _, _, _, _ = self._export_runtime_blueprint(display)
        ov = CustomerOverlaySpec(
            overlay_id=f"overlay_{did.lower()}",
            description="交互式 M1 DOAL 扩展草稿",
        )
        sess = _M1DraftSession(
            draft_id=did,
            owner_actor_id=actor.actor_id,
            created_at=now,
            last_used_at=now,
            overlay=ov,
        )
        self._m1_drafts[did] = sess
        return sess

    def _require_m1_draft(self, draft_id: str, actor: Actor) -> _M1DraftSession:
        sess = self._m1_drafts.get(draft_id)
        if sess is None:
            raise ObjectNotFoundError(f"M1 draft {draft_id!r} not found")
        if sess.owner_actor_id != actor.actor_id and actor.role not in self.admin_roles:
            raise PermissionDeniedError(
                f"actor {actor.actor_id!r} cannot access M1 draft {draft_id!r}"
            )
        sess.last_used_at = _utc_now_iso()
        return sess

    @staticmethod
    def _overlay_has_changes(overlay: Any) -> bool:
        return any(
            bool(getattr(overlay, attr, None))
            for attr in (
                "add_roles",
                "add_object_types",
                "derive_object_types",
                "add_disjoint_types",
                "add_fields",
                "add_relations",
                "update_transitions",
                "add_actions",
                "update_actions",
                "add_action_preconditions",
                "restrict_allowed_roles",
                "update_revert_roles",
                "add_invariants",
                "add_views",
                "update_views",
                "migrations",
            )
        )

    @staticmethod
    def _clean_overlay_export_dict(overlay: Any) -> dict[str, Any]:
        dumped = overlay.model_dump(mode="json", exclude_defaults=True)
        dumped.setdefault("overlay_id", str(getattr(overlay, "overlay_id", "")))
        return dumped

    @staticmethod
    def _expr_to_zh(node: Any, display: Mapping[str, Any]) -> str:
        field_labels = display.get("fields") or {}
        if isinstance(node, Mapping):
            if set(node.keys()) == {"lit"}:
                return json.dumps(node["lit"], ensure_ascii=False)
            if set(node.keys()) == {"ref"}:
                ref_s = str(node["ref"])
                parts = ref_s.split(".")
                if len(parts) >= 2 and parts[0] in ("obj", "param"):
                    prefix = "对象" if parts[0] == "obj" else "参数"
                    fname = ".".join(parts[1:])
                    flbl = field_labels.get(parts[1]) or parts[1]
                    if len(parts) == 2 and flbl != parts[1]:
                        return f"{prefix}.{flbl}({fname})"
                return ref_s
            op = str(node.get("op") or "")
            if op == "and":
                return " 且 ".join(
                    ConsoleService._expr_to_zh(a, display) for a in (node.get("args") or ())
                )
            if op == "or":
                return "(" + " 或 ".join(
                    ConsoleService._expr_to_zh(a, display) for a in (node.get("args") or ())
                ) + ")"
            if op == "not":
                return f"非({ConsoleService._expr_to_zh(node.get('arg'), display)})"
            op_map = {
                "eq": "=",
                "ne": "!=",
                "lt": "<",
                "le": "<=",
                "gt": ">",
                "ge": ">=",
                "add": "+",
                "sub": "-",
                "mul": "*",
                "div": "/",
                "in": "属于",
                "not_in": "不属于",
            }
            if op in op_map:
                l_s = ConsoleService._expr_to_zh(node.get("left"), display)
                r_s = ConsoleService._expr_to_zh(node.get("right"), display)
                return f"({l_s} {op_map[op]} {r_s})"
            if op in ("exists", "not_exists", "count_eq", "count_ge", "count_le"):
                ot = node.get("object_type", "")
                where_s = (
                    ConsoleService._expr_to_zh(node.get("where"), display)
                    if node.get("where")
                    else "任意"
                )
                return f"{op}({ot}: {where_s})"
            return json.dumps(node, ensure_ascii=False)
        return json.dumps(node, ensure_ascii=False)

    @classmethod
    def _render_doal_clauses_zh(
        cls,
        overlay: Any,
        display: Mapping[str, Any],
    ) -> list[str]:
        obj_labels = {**(display.get("objects") or {}), **((overlay.display or {}).get("objects") or {})}
        field_labels = {**(display.get("fields") or {}), **((overlay.display or {}).get("fields") or {})}
        act_labels = {**(display.get("actions") or {}), **((overlay.display or {}).get("actions") or {})}
        clauses: list[str] = []

        for ot in (*overlay.add_object_types, *overlay.derive_object_types):
            lbl = obj_labels.get(ot.name) or ot.display_label or ot.name
            ext = f"继承自 {obj_labels.get(ot.extends) or ot.extends}" if ot.extends else "独立根类型"
            f_parts = []
            for fld in ot.fields:
                flags = []
                if fld.non_negative:
                    flags.append("非负")
                if fld.required:
                    flags.append("必填")
                flbl = field_labels.get(fld.name) or fld.name
                f_parts.append(
                    f"{flbl}({fld.name}:{fld.field_type}" + (f",{','.join(flags)}" if flags else "") + ")"
                )
            rel_parts = []
            for rel in ot.relations:
                bound = f"[{rel.min_cardinality},{rel.max_cardinality if rel.max_cardinality is not None else '∞'}]"
                rel_parts.append(f"{rel.field}->{rel.target_type} {bound}")
            detail = "；".join(
                p
                for p in (
                    ext,
                    f"字段: {', '.join(f_parts)}" if f_parts else "",
                    f"关系: {', '.join(rel_parts)}" if rel_parts else "",
                    f"状态机分支: {len(ot.transitions)} 态" if ot.transitions else "",
                )
                if p
            )
            clauses.append(f"新增实体「{lbl}({ot.name})」：{detail}")

        for ot_name, flds in overlay.add_fields.items():
            olbl = obj_labels.get(ot_name) or ot_name
            f_desc = ", ".join(
                f"{f.name}:{f.field_type}" + ("[非负]" if f.non_negative else "")
                for f in flds
            )
            clauses.append(f"单调扩展「{olbl}({ot_name})」新增字段：{f_desc}")

        for ot_name, rels in overlay.add_relations.items():
            olbl = obj_labels.get(ot_name) or ot_name
            r_desc = ", ".join(
                f"{r.field}->{r.target_type}[{r.min_cardinality},{r.max_cardinality if r.max_cardinality is not None else '∞'}]"
                for r in rels
            )
            clauses.append(f"单调扩展「{olbl}({ot_name})」新增关系：{r_desc}")

        for ot_name, trans in overlay.update_transitions.items():
            olbl = obj_labels.get(ot_name) or ot_name
            clauses.append(f"收窄实体「{olbl}({ot_name})」状态机跃迁为：{json.dumps(trans, ensure_ascii=False)}")

        for a, b in overlay.add_disjoint_types:
            clauses.append(f"声明本体互斥公理：{a} ⊓ {b} = ⊥（实例不可交叉重叠）")

        for act in overlay.add_actions:
            albl = act_labels.get(act.name) or act.display_label or act.name
            pres = [
                f"{p.code}({cls._expr_to_zh(p.must_hold, display)})"
                for p in act.preconditions
            ]
            muts = []
            for m in act.mutations:
                assigns = ", ".join(
                    f"{k} := {cls._expr_to_zh(v, display)}"
                    for k, v in m.set_fields.items()
                )
                guard = f"当({cls._expr_to_zh(m.when, display)}) " if m.when is not None else ""
                muts.append(f"{guard}{m.op} {m.object_type} [{assigns}]")
            clauses.append(
                f"新增 Rule 动作「{albl}({act.name})」[角色:{','.join(act.allowed_roles)}]："
                + (f"前置=[{'; '.join(pres)}]，" if pres else "")
                + f"变更=[{'; '.join(muts)}]"
            )

        for inv in overlay.add_invariants:
            guard = f"当({cls._expr_to_zh(inv.when, display)}) " if inv.when is not None else ""
            clauses.append(
                f"新增红线不变量「{inv.name}[{inv.code}]」作用于 {inv.target_type}："
                f"{guard}必须满足 {cls._expr_to_zh(inv.must_hold, display)}"
            )

        return clauses

    @staticmethod
    def _coerce_field_spec_dict(raw: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        d = dict(raw)
        disp_lbl = str(d.pop("display_label", "") or "").strip() or None
        if "field_type" not in d and "type" in d:
            d["field_type"] = d.pop("type")
        ft_map = {"integer": "int", "number": "float", "string": "str", "boolean": "bool"}
        raw_ft = str(d.get("field_type") or "str").strip().lower()
        d["field_type"] = ft_map.get(raw_ft, raw_ft)
        allowed_keys = {
            "name",
            "field_type",
            "default",
            "required",
            "primary_key",
            "auto_id",
            "owned",
            "masked_roles",
            "non_negative",
            "description",
        }
        return {k: v for k, v in d.items() if k in allowed_keys}, disp_lbl

    @staticmethod
    def _coerce_relation_spec_dict(raw: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        d = dict(raw)
        disp_lbl = str(d.pop("display_label", "") or "").strip() or None
        if "target_type" not in d and "target" in d:
            d["target_type"] = d.pop("target")
        if "field" not in d and "name" in d:
            d["field"] = d.pop("name")
        allowed_keys = {
            "field",
            "target_type",
            "multiple",
            "owned",
            "required",
            "min_cardinality",
            "max_cardinality",
            "description",
        }
        return {k: v for k, v in d.items() if k in allowed_keys}, disp_lbl

    @staticmethod
    def _coerce_invariant_rule_dict(
        raw: Mapping[str, Any],
        *,
        default_id_param: str | None = None,
    ) -> dict[str, Any]:
        d = dict(raw)
        name_s = str(d.get("name") or "rule").strip()
        if not d.get("code"):
            d["code"] = re.sub(r"[^A-Z0-9_]", "_", name_s.upper()).strip("_") or "RULE_VIOLATION"
        if default_id_param and not d.get("id_param"):
            d["id_param"] = default_id_param
        allowed_keys = {"name", "code", "target_type", "id_param", "when", "must_hold", "description"}
        return {k: v for k, v in d.items() if k in allowed_keys}

    @staticmethod
    def _coerce_mutation_spec_dict(
        raw: Mapping[str, Any],
        *,
        pk_lookup: Mapping[str, str],
    ) -> dict[str, Any]:
        d = dict(raw)
        ot_n = str(d.get("object_type") or "").strip()
        if not d.get("id_param") and ot_n in pk_lookup:
            d["id_param"] = pk_lookup[ot_n]
        allowed_keys = {"op", "object_type", "id_param", "target_ref", "when", "set_fields"}
        return {k: v for k, v in d.items() if k in allowed_keys}

    def _merged_draft_display(self, overlay: Any) -> dict[str, Any]:
        base_disp = self._merged_display()
        extra = getattr(overlay, "display", None) or {}
        return _merge_display(base_disp, extra)

    def _validate_candidate_overlay(self, candidate: Any) -> Any:
        from .induction.dsl import apply_overlay_to_spec

        return apply_overlay_to_spec(self.runtime.spec, candidate)

    def _build_draft_bundle(
        self,
        draft: _M1DraftSession,
        actor: Actor,
        *,
        proof_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .induction.dsl import apply_overlay_to_spec

        has_changes = self._overlay_has_changes(draft.overlay)
        preview_spec = apply_overlay_to_spec(self.runtime.spec, draft.overlay)
        preview_bp = self._build_blueprint_from_spec(
            preview_spec,
            actor,
            draft_overlay=draft.overlay if has_changes else None,
        )
        disp = self._merged_draft_display(draft.overlay)
        clauses_zh = self._render_doal_clauses_zh(draft.overlay, disp)
        resolved_proof = proof_report if proof_report is not None else draft.last_proof
        return {
            "draft_id": draft.draft_id,
            "draft_overlay": self._clean_overlay_export_dict(draft.overlay),
            "draft_has_changes": has_changes,
            "draft_clauses_zh": clauses_zh,
            "preview_blueprint": preview_bp,
            "proof_report": resolved_proof,
            "escalations": list(draft.escalations),
        }

    @staticmethod
    def _build_m1_expert_tools() -> list[dict[str, Any]]:
        """Declare strictly declarative DOAL M1 modeling + Z3 + escalation tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "inspect_m1_blueprint",
                    "description": "查看当前基座模型及草稿中所有的实体、子类继承、字段、关系、动作、不变量与互斥公理。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_draft": {"type": "boolean", "default": True},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_draft_object",
                    "description": "在当前 DOAL 草稿层新增或更新实体类型（设置 extends 可创建单调继承子类；严禁覆盖基座标准实体）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "PascalCase 实体类名"},
                            "display_label": {"type": "string", "description": "实体中文名称"},
                            "extends": {"type": "string", "description": "可选父类名（创建子类时填基座或草稿父类名）"},
                            "primary_key": {"type": "string", "description": "主键字段名（若 extends 非空则继承父类主键，无需改写）"},
                            "fields": {
                                "type": "array",
                                "description": "新增或细化字段列表，每项含 name, field_type(str|int|float|bool), default, required, non_negative, display_label",
                                "items": {"type": "object"},
                            },
                            "relations": {
                                "type": "array",
                                "description": "语义关系列表，每项含 field, target_type, multiple, required, min_cardinality, max_cardinality, display_label",
                                "items": {"type": "object"},
                            },
                            "status_field": {"type": "string"},
                            "initial_statuses": {"type": "array", "items": {"type": "string"}},
                            "transitions": {"type": "object"},
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "extend_base_object",
                    "description": "向已有基座实体单调追加新字段、新关系或收窄状态机（严格遵守 LSP，禁止删改基座契约）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "object_type": {"type": "string"},
                            "add_fields": {"type": "array", "items": {"type": "object"}},
                            "add_relations": {"type": "array", "items": {"type": "object"}},
                            "narrow_transitions": {"type": "object"},
                        },
                        "required": ["object_type"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_draft_object",
                    "description": "删除当前草稿中新增的实体或子类（系统将物理阻断任何删除基座实体的请求）。",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_draft_link",
                    "description": "为草稿实体新增/更新语义关系，或为基座实体追加扩展语义关系（含最小/最大基数约束）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_object": {"type": "string"},
                            "field": {"type": "string"},
                            "target_type": {"type": "string"},
                            "multiple": {"type": "boolean"},
                            "required": {"type": "boolean"},
                            "min_cardinality": {"type": "integer"},
                            "max_cardinality": {"type": ["integer", "null"]},
                            "display_label": {"type": "string"},
                        },
                        "required": ["source_object", "field", "target_type"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_draft_link",
                    "description": "删除草稿中新增的语义关系（禁止删除基座关系）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_object": {"type": "string"},
                            "field": {"type": "string"},
                        },
                        "required": ["source_object", "field"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_draft_rule_action",
                    "description": "在草稿层新增或更新纯声明式 Rule 动作（支持 JSON AST 前置条件 preconditions 与有限分支守卫 when 的 mutations）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "display_label": {"type": "string"},
                            "description": {"type": "string"},
                            "allowed_roles": {"type": "array", "items": {"type": "string"}},
                            "params": {"type": "array", "items": {"type": "object"}},
                            "preconditions": {"type": "array", "items": {"type": "object"}},
                            "mutations": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["name", "mutations"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_draft_rule_action",
                    "description": "删除草稿中新增的 Rule 动作（禁止删除或改写基座动作及原语）。",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_draft_invariant",
                    "description": "在草稿层新增或更新一条作用于目标实体全实例的声明式红线不变量。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "code": {"type": "string"},
                            "target_type": {"type": "string"},
                            "must_hold": {"type": "object"},
                            "when": {"type": ["object", "null"]},
                            "description": {"type": "string"},
                        },
                        "required": ["name", "code", "target_type", "must_hold"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_draft_invariant",
                    "description": "删除草稿中新增的红线不变量（禁止删除基座红线）。",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_draft_disjoint",
                    "description": "在草稿层声明两个本体类型互斥（DisjointWith）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type_a": {"type": "string"},
                            "type_b": {"type": "string"},
                        },
                        "required": ["type_a", "type_b"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_draft_disjoint",
                    "description": "删除草稿中新增的互斥公理（禁止删除基座互斥公理）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type_a": {"type": "string"},
                            "type_b": {"type": "string"},
                        },
                        "required": ["type_a", "type_b"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "prove_draft_mathematics",
                    "description": "调用 Z3 SMT 形式化定理证明器对「基座 + 当前 DOAL 草稿」执行 PO-1~PO-4（类可满足性/死类检测、LSP、死锁、Hoare 不变量保持、冗余规则）全面数学证明。",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "escalate_to_edit_function",
                    "description": "当用户提出超出可判定一阶/QF_LIA DOAL 边界的图灵完备需求（如任意递归图遍历、循环批处理、非线性物理算法、密码/外部 IO）时，提交工程升级提案并拒绝伪造近似 DOAL。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "reason": {"type": "string"},
                            "affected_objects": {"type": "array", "items": {"type": "string"}},
                            "specification_notes": {"type": "string"},
                        },
                        "required": ["title", "reason"],
                    },
                },
            },
        ]

    def _build_m1_expert_system_prompt(
        self,
        actor: Actor,
        draft: _M1DraftSession,
    ) -> str:
        from .induction.dsl import apply_overlay_to_spec

        preview_spec = apply_overlay_to_spec(self.runtime.spec, draft.overlay)
        disp = self._merged_draft_display(draft.overlay)
        obj_labels = disp.get("objects") or {}
        act_labels = disp.get("actions") or {}
        tbox = describe_tbox(preview_spec)
        behavior = describe_behavior(preview_spec)
        base_type_names = sorted(self.runtime.spec.object_types.keys())
        base_action_names = sorted(self.runtime.spec.actions.keys())
        base_inv_names = sorted(
            set(self.runtime.spec.invariants.keys())
            | set(self.runtime.spec.transition_invariants.keys())
        )
        valid_roles = sorted(preview_spec.roles.keys())

        entities_summary = [
            {
                "name": ot["name"],
                "display_label": obj_labels.get(ot["name"]) or ot["name"],
                "primary_key": ot["primary_key"],
                "extends": getattr(preview_spec.get_object_type(ot["name"]), "__extends_type__", None),
                "is_base": ot["name"] in self.runtime.spec.object_types,
                "fields": [f["name"] for f in ot.get("fields", ())],
            }
            for ot in tbox["object_types"]
        ]
        summary_json = json.dumps(
            {
                "draft_id": draft.draft_id,
                "actor_role": actor.role,
                "declared_roles": valid_roles,
                "base_immutable_types": base_type_names,
                "base_immutable_actions": base_action_names,
                "base_immutable_invariants": base_inv_names,
                "current_entities": entities_summary,
                "current_actions": [
                    {
                        "name": a["name"],
                        "display_label": act_labels.get(a["name"]) or a["name"],
                        "implementation_kind": preview_spec.actions[a["name"]].implementation_kind,
                    }
                    for a in behavior["actions"]
                ],
                "current_clauses_zh": self._render_doal_clauses_zh(draft.overlay, disp),
            },
            ensure_ascii=False,
        )
        return (
            "你是 OpenOntology M1 本体架构专家（DOAL 形式化建模引擎 + Z3 定理证明顾问）。\n"
            "严格遵循以下核心原则：\n"
            "1. **两层严格边界**：\n"
            "   - `base_immutable_*` 为基座标准契约，只读 + 仅支持单调扩展（`extend_base_object` 或通过 `upsert_draft_object(..., extends=...)` 派生严格子类）。禁止删除、覆盖或弱化基座类型/字段/关系/动作/状态机/不变量。\n"
            "   - 草稿层 (`draft_*`) 中的新增实体、子类、关系、Rule 动作、不变量和互斥公理支持完整增删改查 (CRUD)。\n"
            "2. **纯声明式 DOAL JSON AST 语法**：\n"
            "   - 比较/逻辑表达式：`{\"op\": \"eq|ne|lt|le|gt|ge|add|sub|mul|div|in|not_in\", \"left\": <expr>, \"right\": <expr>}`，`{\"op\": \"and|or\", \"args\": [...]}`，`{\"op\": \"not\", \"arg\": <expr>}`；\n"
            "   - 引用 `{\"ref\": \"obj.<字段>\"}`、`{\"ref\": \"param.<参数>\"}`、`{\"ref\": \"actor.role\"}`；字面量 `{\"lit\": 值}`；\n"
            "   - 创建子类时若沿用父类主键，无需重传 `primary_key`；给 `extend_base_object` 加扩展字段建议优先 `x_` 前缀；新增 Rule 动作若希望立即通过 Hoare `wp` 证明且无违例，请注意为减值/上限等操作配齐声明式前置条件 `preconditions` 或分支守卫 `when`！\n"
            "3. **图灵完备边界升级 (AG-3)**：遇到任意深度图递归、动态循环遍历、非线性数值迭代或需自定义外部代码原语的需求，必须调用 `escalate_to_edit_function`，绝不伪造近似规则。\n"
            "4. **修改即证明**：每次修改草稿后，务必调用 `prove_draft_mathematics` 获取 Z3 PO-1~PO-4 数学证明结果（关注死类 `UNSATISFIABLE_CLASS` 极小冲突核、Hoare 反例），并用精准中文汇报变更条款与数学核验结论。\n\n"
            f"当前 M1 建模上下文：\n{summary_json}"
        )

    def _execute_m1_expert_tool(
        self,
        tool_name: str,
        raw_args: Mapping[str, Any],
        actor: Actor,
        draft_id: str,
        tool_trace: list[dict[str, Any]],
        mutated_flag: list[bool],
    ) -> dict[str, Any]:
        from .induction.dsl import (
            ActionBlueprint,
            ActionMutationSpec,
            FieldSpec,
            InvariantRuleSpec,
            ObjectTypeBlueprint,
            RelationSpec,
            apply_overlay_to_spec,
        )

        # Tool 12 runs Z3 without holding self._lock for proof execution
        if tool_name == "prove_draft_mathematics":
            with self._lock:
                draft = self._require_m1_draft(draft_id, actor)
                ov_copy = copy.deepcopy(draft.overlay)
            try:
                proof = self._prove_draft_overlay(ov_copy)
            except (OpenOntologyError, ValueError, ValidationError) as exc:
                err_code = getattr(exc, "code", None) or "INVALID_DOAL_DRAFT"
                tool_trace.append(
                    {"tool": "prove_draft_mathematics", "summary": f"Z3 建模核验失败（{err_code}）", "ok": False}
                )
                return {"ok": False, "error_code": err_code, "error": str(exc)}
            with self._lock:
                draft = self._require_m1_draft(draft_id, actor)
                draft.last_proof = proof
                disp = self._merged_draft_display(draft.overlay)
                clauses_zh = self._render_doal_clauses_zh(draft.overlay, disp)
            if not proof.get("prover_available"):
                tool_trace.append(
                    {"tool": "prove_draft_mathematics", "summary": "Z3 未安装（降级跳过）", "ok": True}
                )
                return {**proof, "draft_clauses_zh": clauses_zh}
            verdict = str(proof.get("verdict") or "ISSUES_FOUND")
            ok_flag = verdict == "PROVEN_CLEAN"
            tool_trace.append(
                {
                    "tool": "prove_draft_mathematics",
                    "summary": "Z3 全路径证明通过" if ok_flag else f"Z3 发现冲突（{verdict}）",
                    "ok": True,
                }
            )
            return {
                "ok": True,
                "verdict": verdict,
                "summary": proof.get("summary"),
                "class_proofs": [
                    cp for cp in (proof.get("class_proofs") or ()) if cp.get("status") != "SATISFIABLE"
                ],
                "action_proofs": [
                    ap for ap in (proof.get("action_proofs") or ()) if ap.get("status") != "REACHABLE"
                ],
                "invariant_proofs": [
                    ip for ip in (proof.get("invariant_proofs") or ()) if ip.get("status") != "PROVED"
                ],
                "redundant_rules": proof.get("redundant_rules") or [],
                "draft_clauses_zh": clauses_zh,
            }

        with self._lock:
            draft = self._require_m1_draft(draft_id, actor)
            base_spec = self.runtime.spec
            base_ot_names = set(base_spec.object_types.keys())
            base_act_names = set(base_spec.actions.keys())
            base_inv_names = set(base_spec.invariants.keys()) | set(base_spec.transition_invariants.keys())
            base_disjoints = {
                tuple(sorted((str(a), str(b))))
                for a, b in (getattr(base_spec, "__disjoint_types__", ()) or ())
            }
            valid_roles = sorted(base_spec.roles.keys())

            def _pk_lookup(spec_obj: Any, candidate_ov: Any) -> dict[str, str]:
                m = {k: cls.primary_key_name() for k, cls in spec_obj.object_types.items()}
                for ot_item in (*candidate_ov.add_object_types, *candidate_ov.derive_object_types):
                    if ot_item.extends and ot_item.extends in m:
                        m[ot_item.name] = m[ot_item.extends]
                    else:
                        m[ot_item.name] = ot_item.primary_key
                return m

            def _fail(tool_lbl: str, code: str, msg: str) -> dict[str, Any]:
                tool_trace.append({"tool": tool_name, "summary": f"{tool_lbl} 拒绝（{code}）", "ok": False})
                return {"ok": False, "error_code": code, "error": msg}

            def _commit_candidate(candidate_ov: Any, trace_msg: str) -> dict[str, Any]:
                preview_spec = apply_overlay_to_spec(base_spec, candidate_ov)
                draft.overlay = candidate_ov
                draft.last_proof = None
                mutated_flag[0] = True
                disp = self._merged_draft_display(draft.overlay)
                clauses_zh = self._render_doal_clauses_zh(draft.overlay, disp)
                tool_trace.append({"tool": tool_name, "summary": trace_msg, "ok": True})
                return {
                    "ok": True,
                    "draft_id": draft.draft_id,
                    "object_count": len(preview_spec.object_types),
                    "draft_clauses_zh": clauses_zh,
                }

            if tool_name == "inspect_m1_blueprint":
                inc = bool(raw_args.get("include_draft", True))
                target_sp = apply_overlay_to_spec(base_spec, draft.overlay) if inc else base_spec
                bp_dict = self._build_blueprint_from_spec(
                    target_sp,
                    actor,
                    draft_overlay=draft.overlay if (inc and self._overlay_has_changes(draft.overlay)) else None,
                )
                tool_trace.append(
                    {
                        "tool": "inspect_m1_blueprint",
                        "summary": f"检查模型契约（{bp_dict['summary']['object_type_count']} 类 · {bp_dict['summary']['action_count']} 动作）",
                        "ok": True,
                    }
                )
                return {
                    "ok": True,
                    "summary": bp_dict["summary"],
                    "object_types": [
                        {
                            "name": o["name"],
                            "display_label": o["display_label"],
                            "extends": o.get("extends"),
                            "primary_key": o["primary_key"],
                            "draft_status": o.get("draft_status", "base"),
                            "fields": [f["name"] for f in o["fields"]],
                            "transitions": o["transitions"],
                        }
                        for o in bp_dict["object_types"]
                    ],
                    "links": [
                        {
                            "source": lk["source"],
                            "field": lk["field"],
                            "target": lk["target"],
                            "min_cardinality": lk.get("min_cardinality", 0),
                            "max_cardinality": lk.get("max_cardinality"),
                            "draft_status": lk.get("draft_status", "base"),
                        }
                        for lk in bp_dict["links"]
                    ],
                    "actions": [
                        {
                            "name": a["name"],
                            "implementation_kind": a["implementation_kind"],
                            "draft_status": a.get("draft_status", "base"),
                        }
                        for a in bp_dict["actions"]
                    ],
                    "invariants": bp_dict["invariants"],
                    "disjoint_types": bp_dict.get("disjoint_types", []),
                }

            if tool_name == "upsert_draft_object":
                name = str(raw_args.get("name") or "").strip()
                if not name:
                    return _fail("定义实体", "INVALID_IDENTIFIER", "object name is required")
                if name in base_ot_names:
                    return _fail(
                        f"覆盖实体 {name}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"不能覆盖或重定义基座标准实体 {name!r}，请使用 extend_base_object 扩展或设置 extends 派生子类。",
                    )
                extends = str(raw_args.get("extends") or "").strip() or None
                disp_lbl = str(raw_args.get("display_label") or "").strip()
                candidate = copy.deepcopy(draft.overlay)

                try:
                    raw_fields = list(raw_args.get("fields") or ())
                    coerced_fields: list[FieldSpec] = []
                    for rf in raw_fields:
                        f_dict, f_lbl = self._coerce_field_spec_dict(rf)
                        f_obj = FieldSpec(**f_dict)
                        coerced_fields.append(f_obj)
                        if f_lbl:
                            candidate.display.setdefault("fields", {})[f_obj.name] = f_lbl

                    raw_rels = list(raw_args.get("relations") or ())
                    coerced_rels: list[RelationSpec] = []
                    for rr in raw_rels:
                        r_dict, r_lbl = self._coerce_relation_spec_dict(rr)
                        r_obj = RelationSpec(**r_dict)
                        coerced_rels.append(r_obj)
                        if r_lbl:
                            candidate.display.setdefault("fields", {})[r_obj.field] = r_lbl

                    ot_kwargs: dict[str, Any] = {
                        "name": name,
                        "fields": coerced_fields,
                        "relations": coerced_rels,
                    }
                    if extends is not None:
                        ot_kwargs["extends"] = extends
                    raw_pk = raw_args.get("primary_key")
                    sub_field_names = {f.name for f in coerced_fields}
                    if raw_pk and (not extends or (raw_pk != "id" or "id" in sub_field_names)):
                        ot_kwargs["primary_key"] = str(raw_pk)
                    if raw_args.get("status_field") and (not extends or raw_args.get("status_field") != "status"):
                        ot_kwargs["status_field"] = str(raw_args["status_field"])
                    if raw_args.get("initial_statuses") is not None:
                        ot_kwargs["initial_statuses"] = list(raw_args["initial_statuses"])
                    if raw_args.get("transitions") is not None:
                        ot_kwargs["transitions"] = {
                            str(k): list(v) for k, v in dict(raw_args["transitions"]).items()
                        }
                    if disp_lbl:
                        ot_kwargs["display_label"] = disp_lbl
                        candidate.display.setdefault("objects", {})[name] = disp_lbl

                    ot_bp = ObjectTypeBlueprint(**ot_kwargs)
                    candidate.add_object_types = [o for o in candidate.add_object_types if o.name != name]
                    candidate.derive_object_types = [o for o in candidate.derive_object_types if o.name != name]
                    if extends is not None:
                        candidate.derive_object_types.append(ot_bp)
                    else:
                        candidate.add_object_types.append(ot_bp)
                    return _commit_candidate(candidate, f"草稿写入实体 {disp_lbl or name}({name})")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(
                        f"定义实体 {name}",
                        getattr(exc, "code", None) or "LSP_OR_SCHEMA_ERROR",
                        str(exc),
                    )

            if tool_name == "extend_base_object":
                ot_name = str(raw_args.get("object_type") or "").strip()
                if ot_name not in base_ot_names:
                    return _fail(
                        f"扩展实体 {ot_name}",
                        "UNKNOWN_OBJECT_TYPE",
                        f"{ot_name!r} 不是当前基座实体；若为草稿实体请使用 upsert_draft_object。",
                    )
                base_cls = base_spec.object_types[ot_name]
                candidate = copy.deepcopy(draft.overlay)
                try:
                    for rf in raw_args.get("add_fields") or ():
                        f_dict, f_lbl = self._coerce_field_spec_dict(rf)
                        f_obj = FieldSpec(**f_dict)
                        if f_obj.name in base_cls.model_fields:
                            return _fail(
                                f"修改基座字段 {ot_name}.{f_obj.name}",
                                "OVERLAY_BREAKS_STANDARD_CONTRACT",
                                f"禁止修改基座已有字段 {ot_name}.{f_obj.name}；请使用新增扩展字段（如 x_ 前缀）或 derive subtype。",
                            )
                        cur_list = [f for f in candidate.add_fields.get(ot_name, []) if f.name != f_obj.name]
                        cur_list.append(f_obj)
                        candidate.add_fields[ot_name] = cur_list
                        if f_lbl:
                            candidate.display.setdefault("fields", {})[f_obj.name] = f_lbl

                    for rr in raw_args.get("add_relations") or ():
                        r_dict, r_lbl = self._coerce_relation_spec_dict(rr)
                        r_obj = RelationSpec(**r_dict)
                        if r_obj.field in base_cls.model_fields:
                            return _fail(
                                f"修改基座关系 {ot_name}.{r_obj.field}",
                                "OVERLAY_BREAKS_STANDARD_CONTRACT",
                                f"禁止覆盖基座已有关系字段 {ot_name}.{r_obj.field}。",
                            )
                        cur_rels = [r for r in candidate.add_relations.get(ot_name, []) if r.field != r_obj.field]
                        cur_rels.append(r_obj)
                        candidate.add_relations[ot_name] = cur_rels
                        if r_lbl:
                            candidate.display.setdefault("fields", {})[r_obj.field] = r_lbl

                    if raw_args.get("narrow_transitions") is not None:
                        candidate.update_transitions[ot_name] = {
                            str(k): list(v) for k, v in dict(raw_args["narrow_transitions"]).items()
                        }

                    return _commit_candidate(candidate, f"单调扩展基座实体 {ot_name}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(
                        f"扩展实体 {ot_name}",
                        getattr(exc, "code", None) or "LSP_VIOLATION",
                        str(exc),
                    )

            if tool_name == "delete_draft_object":
                name = str(raw_args.get("name") or "").strip()
                if name in base_ot_names:
                    return _fail(
                        f"删除基座实体 {name}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"基座标准实体 {name!r} 受契约保护，严禁删除。",
                    )
                in_add = any(o.name == name for o in draft.overlay.add_object_types)
                in_der = any(o.name == name for o in draft.overlay.derive_object_types)
                if not in_add and not in_der:
                    return _fail(f"删除草稿实体 {name}", "OBJECT_NOT_FOUND", f"草稿中不存在实体 {name!r}")
                candidate = copy.deepcopy(draft.overlay)
                candidate.add_object_types = [o for o in candidate.add_object_types if o.name != name]
                candidate.derive_object_types = [o for o in candidate.derive_object_types if o.name != name]
                candidate.add_fields.pop(name, None)
                candidate.add_relations.pop(name, None)
                candidate.update_transitions.pop(name, None)
                try:
                    return _commit_candidate(candidate, f"删除草稿实体 {name}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(
                        f"删除草稿实体 {name}",
                        getattr(exc, "code", None) or "DEPENDENCY_CONFLICT",
                        str(exc),
                    )

            if tool_name == "upsert_draft_link":
                src = str(raw_args.get("source_object") or "").strip()
                fld = str(raw_args.get("field") or "").strip()
                tgt = str(raw_args.get("target_type") or "").strip()
                disp_lbl = str(raw_args.get("display_label") or "").strip()
                candidate = copy.deepcopy(draft.overlay)
                try:
                    r_dict, _ = self._coerce_relation_spec_dict(
                        {
                            "field": fld,
                            "target_type": tgt,
                            "multiple": bool(raw_args.get("multiple", False)),
                            "required": bool(raw_args.get("required", False)),
                            "min_cardinality": int(raw_args.get("min_cardinality", 0) or 0),
                            "max_cardinality": raw_args.get("max_cardinality"),
                        }
                    )
                    rel_obj = RelationSpec(**r_dict)
                    if src in base_ot_names:
                        base_cls = base_spec.object_types[src]
                        if fld in base_cls.model_fields:
                            return _fail(
                                f"覆盖基座关系 {src}.{fld}",
                                "OVERLAY_BREAKS_STANDARD_CONTRACT",
                                f"禁止覆盖基座已有关系或字段 {src}.{fld}。",
                            )
                        cur = [r for r in candidate.add_relations.get(src, []) if r.field != fld]
                        cur.append(rel_obj)
                        candidate.add_relations[src] = cur
                    else:
                        found = False
                        for lst in (candidate.add_object_types, candidate.derive_object_types):
                            for ot in lst:
                                if ot.name == src:
                                    ot.relations = [r for r in ot.relations if r.field != fld] + [rel_obj]
                                    found = True
                        if not found:
                            return _fail(f"添加关系 {src}.{fld}", "UNKNOWN_OBJECT_TYPE", f"未找到源实体 {src!r}")
                    if disp_lbl:
                        candidate.display.setdefault("fields", {})[fld] = disp_lbl
                        candidate.display.setdefault("field_overrides", {})[f"{src}.{fld}"] = disp_lbl
                    return _commit_candidate(candidate, f"草稿写入关系 {src}.{fld} -> {tgt}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(
                        f"添加关系 {src}.{fld}",
                        getattr(exc, "code", None) or "INVALID_RELATION",
                        str(exc),
                    )

            if tool_name == "delete_draft_link":
                src = str(raw_args.get("source_object") or "").strip()
                fld = str(raw_args.get("field") or "").strip()
                if src in base_ot_names:
                    base_props = getattr(base_spec.object_types[src], "__object_properties__", {}) or {}
                    if fld in base_props or fld in base_spec.object_types[src].model_fields:
                        return _fail(
                            f"删除基座关系 {src}.{fld}",
                            "OVERLAY_BREAKS_STANDARD_CONTRACT",
                            f"基座标准关系 {src}.{fld} 严禁删除。",
                        )
                candidate = copy.deepcopy(draft.overlay)
                removed = False
                if src in candidate.add_relations:
                    before_len = len(candidate.add_relations[src])
                    candidate.add_relations[src] = [r for r in candidate.add_relations[src] if r.field != fld]
                    removed = len(candidate.add_relations[src]) < before_len
                    if not candidate.add_relations[src]:
                        candidate.add_relations.pop(src, None)
                for lst in (candidate.add_object_types, candidate.derive_object_types):
                    for ot in lst:
                        if ot.name == src:
                            before_len = len(ot.relations)
                            ot.relations = [r for r in ot.relations if r.field != fld]
                            removed = removed or (len(ot.relations) < before_len)
                if not removed:
                    return _fail(f"删除关系 {src}.{fld}", "OBJECT_NOT_FOUND", f"草稿中不存在关系 {src}.{fld}")
                try:
                    return _commit_candidate(candidate, f"删除草稿关系 {src}.{fld}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"删除关系 {src}.{fld}", getattr(exc, "code", None) or "INVALID_DOAL_DRAFT", str(exc))

            if tool_name == "upsert_draft_rule_action":
                name = str(raw_args.get("name") or "").strip()
                if not name:
                    return _fail("定义动作", "INVALID_IDENTIFIER", "action name is required")
                if name in base_act_names:
                    return _fail(
                        f"覆盖基座动作 {name}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"基座标准动作或原语 {name!r} 受契约保护，禁止覆盖；请新建派生动作或仅追加前置条件。",
                    )
                candidate = copy.deepcopy(draft.overlay)
                try:
                    pk_map = _pk_lookup(base_spec, candidate)
                    disp_lbl = str(raw_args.get("display_label") or "").strip()
                    raw_roles = raw_args.get("allowed_roles")
                    if raw_roles:
                        roles_list = [str(r).strip() for r in raw_roles if str(r).strip()]
                    else:
                        defaults = [r for r in dict.fromkeys([actor.role, "admin"]) if not valid_roles or r in valid_roles]
                        roles_list = defaults or (valid_roles[:1] if valid_roles else ["admin"])

                    coerced_params: list[FieldSpec] = []
                    for rp in raw_args.get("params") or ():
                        p_dict, p_lbl = self._coerce_field_spec_dict(rp)
                        p_obj = FieldSpec(**p_dict)
                        coerced_params.append(p_obj)
                        if p_lbl:
                            candidate.display.setdefault("fields", {})[p_obj.name] = p_lbl

                    coerced_muts = [
                        ActionMutationSpec(**self._coerce_mutation_spec_dict(rm, pk_lookup=pk_map))
                        for rm in (raw_args.get("mutations") or ())
                    ]
                    first_mut_ot = coerced_muts[0].object_type if coerced_muts else ""
                    first_mut_idp = coerced_muts[0].id_param if coerced_muts else None
                    coerced_pres = [
                        InvariantRuleSpec(
                            **self._coerce_invariant_rule_dict(
                                {"target_type": first_mut_ot, **dict(rp)},
                                default_id_param=first_mut_idp,
                            )
                        )
                        for rp in (raw_args.get("preconditions") or ())
                    ]
                    act_bp = ActionBlueprint(
                        name=name,
                        display_label=disp_lbl,
                        description=str(raw_args.get("description") or ""),
                        allowed_roles=roles_list,
                        params=coerced_params,
                        preconditions=coerced_pres,
                        mutations=coerced_muts,
                    )
                    if disp_lbl:
                        candidate.display.setdefault("actions", {})[name] = disp_lbl
                    candidate.add_actions = [a for a in candidate.add_actions if a.name != name] + [act_bp]
                    return _commit_candidate(candidate, f"草稿写入 Rule 动作 {disp_lbl or name}({name})")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"定义动作 {name}", getattr(exc, "code", None) or "INVALID_ACTION", str(exc))

            if tool_name == "delete_draft_rule_action":
                name = str(raw_args.get("name") or "").strip()
                if name in base_act_names:
                    return _fail(
                        f"删除基座动作 {name}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"基座标准动作 {name!r} 受契约保护，严禁删除。",
                    )
                if not any(a.name == name for a in draft.overlay.add_actions):
                    return _fail(f"删除动作 {name}", "ACTION_NOT_FOUND", f"草稿中不存在动作 {name!r}")
                candidate = copy.deepcopy(draft.overlay)
                candidate.add_actions = [a for a in candidate.add_actions if a.name != name]
                try:
                    return _commit_candidate(candidate, f"删除草稿动作 {name}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"删除动作 {name}", getattr(exc, "code", None) or "INVALID_DOAL_DRAFT", str(exc))

            if tool_name == "upsert_draft_invariant":
                name = str(raw_args.get("name") or "").strip()
                if name in base_inv_names:
                    return _fail(
                        f"覆盖基座不变量 {name}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"基座标准不变量 {name!r} 受契约保护，禁止覆盖。",
                    )
                candidate = copy.deepcopy(draft.overlay)
                try:
                    inv_obj = InvariantRuleSpec(**self._coerce_invariant_rule_dict(raw_args))
                    candidate.add_invariants = [i for i in candidate.add_invariants if i.name != inv_obj.name] + [inv_obj]
                    return _commit_candidate(candidate, f"草稿写入红线不变量 {inv_obj.name}[{inv_obj.code}]")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"定义不变量 {name}", getattr(exc, "code", None) or "INVALID_PREDICATE_AST", str(exc))

            if tool_name == "delete_draft_invariant":
                name = str(raw_args.get("name") or "").strip()
                if name in base_inv_names:
                    return _fail(
                        f"删除基座不变量 {name}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"基座标准不变量 {name!r} 受契约保护，严禁删除。",
                    )
                if not any(i.name == name for i in draft.overlay.add_invariants):
                    return _fail(f"删除不变量 {name}", "OBJECT_NOT_FOUND", f"草稿中不存在不变量 {name!r}")
                candidate = copy.deepcopy(draft.overlay)
                candidate.add_invariants = [i for i in candidate.add_invariants if i.name != name]
                try:
                    return _commit_candidate(candidate, f"删除草稿不变量 {name}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"删除不变量 {name}", getattr(exc, "code", None) or "INVALID_DOAL_DRAFT", str(exc))

            if tool_name == "upsert_draft_disjoint":
                ta = str(raw_args.get("type_a") or "").strip()
                tb = str(raw_args.get("type_b") or "").strip()
                candidate = copy.deepcopy(draft.overlay)
                pair = tuple(sorted((ta, tb)))
                existing = {tuple(sorted((a, b))) for a, b in candidate.add_disjoint_types}
                if pair not in existing:
                    candidate.add_disjoint_types.append((ta, tb))
                try:
                    return _commit_candidate(candidate, f"草稿声明互斥 {ta} ⊓ {tb} = ⊥")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"互斥公理 {ta}/{tb}", getattr(exc, "code", None) or "DL_DISJOINT_VIOLATION", str(exc))

            if tool_name == "delete_draft_disjoint":
                ta = str(raw_args.get("type_a") or "").strip()
                tb = str(raw_args.get("type_b") or "").strip()
                pair = tuple(sorted((ta, tb)))
                if pair in base_disjoints:
                    return _fail(
                        f"删除基座互斥公理 {ta}/{tb}",
                        "OVERLAY_BREAKS_STANDARD_CONTRACT",
                        f"基座互斥公理 ({ta}, {tb}) 受契约保护，严禁删除。",
                    )
                candidate = copy.deepcopy(draft.overlay)
                before_len = len(candidate.add_disjoint_types)
                candidate.add_disjoint_types = [
                    (a, b) for a, b in candidate.add_disjoint_types if tuple(sorted((a, b))) != pair
                ]
                if len(candidate.add_disjoint_types) == before_len:
                    return _fail(f"删除互斥公理 {ta}/{tb}", "OBJECT_NOT_FOUND", "草稿中不存在该互斥公理")
                try:
                    return _commit_candidate(candidate, f"删除草稿互斥 {ta}/{tb}")
                except (OpenOntologyError, ValueError, ValidationError) as exc:
                    return _fail(f"删除互斥 {ta}/{tb}", getattr(exc, "code", None) or "INVALID_DOAL_DRAFT", str(exc))

            if tool_name == "escalate_to_edit_function":
                ticket = {
                    "kind": "edit_function_escalation",
                    "ticket_id": f"ESC-{len(draft.escalations) + 1:03d}",
                    "title": str(raw_args.get("title") or "图灵完备原语工程升级").strip(),
                    "reason": str(raw_args.get("reason") or "超出可判定一阶/QF_LIA DOAL 边界").strip(),
                    "affected_objects": [str(x) for x in (raw_args.get("affected_objects") or ())],
                    "specification_notes": str(raw_args.get("specification_notes") or "").strip(),
                    "created_at": _utc_now_iso(),
                }
                draft.escalations.append(ticket)
                tool_trace.append(
                    {
                        "tool": "escalate_to_edit_function",
                        "summary": f"已生成图灵完备原语升级单 {ticket['ticket_id']}",
                        "ok": True,
                    }
                )
                return {"ok": True, "escalated": True, "ticket": ticket}

            tool_trace.append({"tool": tool_name, "summary": f"未知建模工具 {tool_name}", "ok": False})
            return {"ok": False, "error_code": "UNKNOWN_TOOL", "error": f"unsupported tool {tool_name!r}"}

    def _run_m1_ontology_expert(
        self,
        question: str,
        payload: _M1ExpertChatPayload,
        actor: Actor,
        *,
        draft_id: str,
        cfg: _LlmEndpointConfig | None,
        model_label: str,
    ) -> dict[str, Any]:
        """Run bounded OpenAI tool-calling loop for M1 DOAL authoring with lock released on HTTP I/O."""
        with self._lock:
            draft = self._require_m1_draft(draft_id, actor)
            sys_prompt = self._build_m1_expert_system_prompt(actor, draft)

        tools = self._build_m1_expert_tools()
        history = self._sanitize_llm_history(payload.history)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            *history,
            {"role": "user", "content": question},
        ]

        def _invoke(body: Mapping[str, Any]) -> Mapping[str, Any]:
            if self.assistant_llm_client is not None:
                return self.assistant_llm_client(dict(body))
            assert cfg is not None
            return _default_openai_compatible_chat(cfg, body, timeout_s=25.0)

        tool_trace: list[dict[str, Any]] = []
        mutated_flag = [False]
        proved_after_last_mut = False
        final_reply = ""
        llm_error_note = ""

        try:
            for _ in range(6):
                resp = _invoke(
                    {
                        "model": model_label,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "temperature": 0.1,
                    }
                )
                choices = resp.get("choices") if isinstance(resp, Mapping) else None
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError("LLM returned empty choices")
                msg = choices[0].get("message") if isinstance(choices[0], Mapping) else None
                if not isinstance(msg, Mapping):
                    raise RuntimeError("LLM returned invalid message")
                raw_tool_calls = msg.get("tool_calls") or []
                if not raw_tool_calls:
                    final_reply = str(msg.get("content") or "").strip()
                    break

                assistant_turn: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": [dict(tc) for tc in raw_tool_calls],
                }
                if msg.get("reasoning_content") is not None:
                    assistant_turn["reasoning_content"] = msg.get("reasoning_content")
                messages.append(assistant_turn)

                for tc in raw_tool_calls:
                    tc_id = str(tc.get("id") or f"m1tc-{uuid.uuid4().hex[:8]}")
                    fn_obj = tc.get("function") if isinstance(tc, Mapping) else None
                    t_name = str(fn_obj.get("name") or "") if isinstance(fn_obj, Mapping) else ""
                    raw_args_val = fn_obj.get("arguments") if isinstance(fn_obj, Mapping) else {}
                    parsed_args: dict[str, Any] = {}
                    if isinstance(raw_args_val, Mapping):
                        parsed_args = dict(raw_args_val)
                    elif isinstance(raw_args_val, str) and raw_args_val.strip():
                        try:
                            loaded = json.loads(raw_args_val)
                            if isinstance(loaded, Mapping):
                                parsed_args = dict(loaded)
                        except json.JSONDecodeError:
                            parsed_args = {}

                    before_mut = mutated_flag[0]
                    mutated_flag[0] = False
                    t_res = self._execute_m1_expert_tool(
                        t_name,
                        parsed_args,
                        actor,
                        draft_id,
                        tool_trace,
                        mutated_flag,
                    )
                    if mutated_flag[0]:
                        proved_after_last_mut = False
                    elif before_mut:
                        mutated_flag[0] = True
                    if t_name == "prove_draft_mathematics" and t_res.get("ok"):
                        proved_after_last_mut = True

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(t_res, ensure_ascii=False),
                        }
                    )

            if not final_reply and tool_trace:
                try:
                    synth = _invoke(
                        {
                            "model": model_label,
                            "messages": messages,
                            "temperature": 0.1,
                        }
                    )
                    s_choices = synth.get("choices") if isinstance(synth, Mapping) else None
                    if isinstance(s_choices, list) and s_choices:
                        s_msg = s_choices[0].get("message") or {}
                        final_reply = str(s_msg.get("content") or "").strip()
                except Exception:
                    final_reply = ""
        except Exception as exc:
            llm_error_note = f"本体专家大模型调用暂时不可用（{str(exc)[:160]}）。"

        # Guarantee Z3 proof is fresh whenever the draft overlay changed during this turn
        if mutated_flag[0] and not proved_after_last_mut:
            try:
                with self._lock:
                    cur_draft = self._require_m1_draft(draft_id, actor)
                    ov_copy = copy.deepcopy(cur_draft.overlay)
                fresh_proof = self._prove_draft_overlay(ov_copy)
                with self._lock:
                    cur_draft = self._require_m1_draft(draft_id, actor)
                    cur_draft.last_proof = fresh_proof
                    tool_trace.append(
                        {
                            "tool": "prove_draft_mathematics",
                            "summary": (
                                "Z3 未安装（降级跳过）"
                                if not fresh_proof.get("prover_available")
                                else (
                                    "Z3 自动证明通过"
                                    if fresh_proof.get("verdict") == "PROVEN_CLEAN"
                                    else f"Z3 自动核验发现问题（{fresh_proof.get('verdict')}）"
                                )
                            ),
                            "ok": True,
                        }
                    )
            except Exception:
                pass

        with self._lock:
            cur_draft = self._require_m1_draft(draft_id, actor)
            bundle = self._build_draft_bundle(cur_draft, actor)

        cards: list[dict[str, Any]] = []
        for esc in bundle["escalations"][-3:]:
            cards.append(
                {
                    "kind": "edit_function_escalation",
                    "title": f"图灵完备原语升级建议 · {esc['ticket_id']}",
                    "subtitle": f"{esc['title']}（需工程化 Python 原语）",
                    "ticket": esc,
                }
            )

        if llm_error_note:
            reply_text = llm_error_note
        elif final_reply:
            reply_text = final_reply
        elif bundle["draft_has_changes"]:
            reply_text = (
                f"已更新 M1 DOAL 草稿 {bundle['draft_id']}，共 {len(bundle['draft_clauses_zh'])} 条扩展定义。"
            )
        else:
            reply_text = "已完成当前 M1 本体蓝图检查。"

        return {
            "ok": True,
            "reply": reply_text,
            "assistant_mode": "llm_sandbox",
            "assistant_model": model_label,
            "tool_trace": tool_trace,
            "cards": cards,
            **bundle,
        }

    def m1_expert_chat(
        self,
        payload: _M1ExpertChatPayload,
        actor: Actor,
    ) -> dict[str, Any]:
        with self._lock:
            question = (payload.message or "").strip()
            raw_did = (payload.draft_id or "").strip() if payload.draft_id else ""
            if raw_did:
                draft = self._require_m1_draft(raw_did, actor)
            else:
                draft = self._create_m1_draft(actor)
            draft_id = draft.draft_id
            llm_ready, cfg, model_label = self._resolve_llm_mode_and_cfg()

            if not question:
                bundle = self._build_draft_bundle(draft, actor)
                return {
                    "ok": True,
                    "reply": "请描述您希望新增或扩展的模型实体、关系、声明式 Rule 动作或红线不变量。",
                    "assistant_mode": "llm_sandbox" if llm_ready else "offline_fallback",
                    "assistant_model": model_label,
                    "tool_trace": [],
                    "cards": [],
                    **bundle,
                }

            if not llm_ready:
                bundle = self._build_draft_bundle(draft, actor)
                return {
                    "ok": True,
                    "reply": (
                        "当前处于离线模式，未配置大模型推理密钥（DEEPSEEK_API_KEY），"
                        "无法将自然语言自动编译为 DOAL 增量；请在配置密钥后使用 AI 本体论专家，或通过工具直接提交 DOAL overlay.json。"
                    ),
                    "assistant_mode": "offline_fallback",
                    "assistant_model": None,
                    "tool_trace": [],
                    "cards": [],
                    **bundle,
                }

        return self._run_m1_ontology_expert(
            question,
            payload,
            actor,
            draft_id=draft_id,
            cfg=cfg,
            model_label=model_label or "deepseek-flash",
        )

    def get_m1_draft(self, draft_id: str, actor: Actor) -> dict[str, Any]:
        with self._lock:
            draft = self._require_m1_draft(draft_id, actor)
            return {"ok": True, **self._build_draft_bundle(draft, actor)}

    def delete_m1_draft(self, draft_id: str, actor: Actor) -> dict[str, Any]:
        with self._lock:
            self._require_m1_draft(draft_id, actor)
            self._m1_drafts.pop(draft_id, None)
            return {"ok": True, "draft_id": draft_id, "deleted": True}

    def apply_m1_draft(self, draft_id: str, actor: Actor) -> dict[str, Any]:
        from .induction.dsl import apply_overlay_to_spec

        with self._lock:
            draft = self._require_m1_draft(draft_id, actor)
            if not self.demo_actor_switch:
                err = PermissionDeniedError(
                    "Production M1 changes must be exported as DOAL overlay.json and merged via Git PR/CI"
                )
                err.code = "PRODUCTION_APPLY_REQUIRES_GIT_PR"  # type: ignore[attr-defined]
                raise err
            ov_copy = copy.deepcopy(draft.overlay)

        proof = self._prove_draft_overlay(ov_copy)
        if proof.get("prover_available") and proof.get("verdict") != "PROVEN_CLEAN":
            raise InvariantError(
                f"M1 draft {draft_id!r} failed Z3 formal verification (verdict={proof.get('verdict')})",
                code="DRAFT_PROOF_FAILED",
            )

        with self._lock:
            self._require_m1_draft(draft_id, actor)
            derived_spec = apply_overlay_to_spec(self.runtime.spec, ov_copy)
            self.runtime.spec = derived_spec
            self.runtime._sync_registry()
            self.work_queue_binding = self.work_queue_binding or self._infer_work_queue_binding()
            self._proof_cache.clear()
            self._m1_drafts.pop(draft_id, None)
            new_bp = self._build_blueprint_from_spec(self.runtime.spec, actor)
            if proof.get("prover_available"):
                fp = new_bp["blueprint_fingerprint"]
                self._proof_cache[(fp, True)] = copy.deepcopy(proof)
            return {
                "ok": True,
                "applied_draft_id": draft_id,
                "blueprint": new_bp,
                "proof_report": proof,
            }


def _status_for_error(exc: Exception) -> int:
    if isinstance(exc, PermissionDeniedError):
        return HTTPStatus.FORBIDDEN
    if isinstance(
        exc,
        (
            KeyError,
            ObjectNotFoundError,
            UnknownObjectTypeError,
            ActionNotFoundError,
            FunctionNotFoundError,
        ),
    ):
        return HTTPStatus.NOT_FOUND
    if isinstance(exc, (OpenOntologyError, ValueError)):
        return HTTPStatus.BAD_REQUEST
    return HTTPStatus.INTERNAL_SERVER_ERROR


def create_console_app(
    service: ConsoleService,
    *,
    static_dir: Path | str | None = None,
) -> FastAPI:
    """Create a FastAPI ASGI application wrapping ``ConsoleService`` + optional SPA bundle."""
    app = FastAPI(title=service.app_title, docs_url=None, redoc_url=None)
    resolved_static = (
        Path(static_dir)
        if static_dir is not None
        else (Path(__file__).resolve().parent.parent / "web" / "dist")
    )

    @app.exception_handler(PermissionDeniedError)
    def _handle_perm(_req: Request, exc: PermissionDeniedError) -> JSONResponse:
        code = getattr(exc, "code", None) or "PERMISSION_DENIED"
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "status": "REJECTED",
                "error_code": code,
                "error": str(exc),
                "hint_zh": _translate_error_hint(
                    code,
                    str(exc),
                    display=service._merged_display(),
                ),
                "zero_external_dirty_write": True,
            },
        )

    @app.exception_handler(OpenOntologyError)
    def _handle_ontology_err(_req: Request, exc: OpenOntologyError) -> JSONResponse:
        code = getattr(exc, "code", None) or "ONTOLOGY_ERROR"
        return JSONResponse(
            status_code=_status_for_error(exc),
            content={
                "ok": False,
                "status": "REJECTED",
                "error_code": code,
                "error": str(exc),
                "hint_zh": _translate_error_hint(
                    code,
                    str(exc),
                    display=service._merged_display(),
                ),
                "zero_external_dirty_write": True,
            },
        )

    @app.exception_handler(RequestValidationError)
    def _handle_req_validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        hint = _format_validation_error_zh(exc, service._merged_display())
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "status": "REJECTED",
                "error_code": "INVALID_PARAMETER",
                "error": hint,
                "hint_zh": hint,
                "zero_external_dirty_write": True,
            },
        )

    @app.exception_handler(ValidationError)
    def _handle_pydantic_validation(_req: Request, exc: ValidationError) -> JSONResponse:
        hint = _format_validation_error_zh(exc, service._merged_display())
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "status": "REJECTED",
                "error_code": "INVALID_PARAMETER",
                "error": hint,
                "hint_zh": hint,
                "zero_external_dirty_write": True,
            },
        )

    @app.exception_handler(ValueError)
    def _handle_val_err(_req: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "status": "REJECTED",
                "error_code": "INVALID_PARAMETER",
                "error": str(exc),
                "hint_zh": str(exc),
                "zero_external_dirty_write": True,
            },
        )

    def _actor_from_request(req: Request) -> Actor:
        return service.resolve_request_actor(
            req.headers,
            query_actor_id=req.query_params.get("actor_id"),
        )

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        return {"ok": True, "world_mode": service.runtime.world_mode}

    @app.get("/api/meta")
    def api_meta(req: Request) -> dict[str, Any]:
        return service.get_meta(_actor_from_request(req))

    @app.get("/api/tbox")
    def api_tbox(req: Request) -> dict[str, Any]:
        return service.get_tbox(_actor_from_request(req))

    @app.get("/api/blueprint")
    def api_blueprint(req: Request) -> dict[str, Any]:
        return service.get_blueprint(_actor_from_request(req))

    @app.post("/api/blueprint/prove")
    def api_blueprint_prove(
        req: Request,
        payload: _BlueprintProvePayload | None = Body(default=None),
    ) -> dict[str, Any]:
        return service.prove_current_blueprint(
            _actor_from_request(req),
            force_refresh=bool(payload.force_refresh if payload is not None else False),
        )

    @app.get("/api/objects/{object_type}")
    def api_list_objects(
        object_type: str,
        req: Request,
        status: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        return service.list_objects(
            object_type,
            _actor_from_request(req),
            status=status,
            q=q,
        )

    @app.get("/api/objects/{object_type}/{object_id}/lineage")
    def api_object_lineage(
        object_type: str,
        object_id: str,
        req: Request,
    ) -> dict[str, Any]:
        return service.get_object_lineage(
            object_type,
            object_id,
            _actor_from_request(req),
        )

    @app.get("/api/objects/{object_type}/{object_id}")
    def api_get_object(
        object_type: str,
        object_id: str,
        req: Request,
    ) -> dict[str, Any]:
        return service.get_single_object(
            object_type,
            object_id,
            _actor_from_request(req),
        )

    @app.post("/api/actions/simulate")
    def api_simulate_action(payload: _ActionPayload, req: Request) -> dict[str, Any]:
        return service.simulate_action(payload, _actor_from_request(req))

    @app.post("/api/actions/submit")
    def api_submit_action(payload: _ActionPayload, req: Request) -> dict[str, Any]:
        return service.submit_action(payload, _actor_from_request(req))

    @app.post("/api/work-queue/{item_id}/resolve")
    def api_resolve_work_queue(
        item_id: str,
        payload: _WorkQueueResolvePayload,
        req: Request,
    ) -> dict[str, Any]:
        return service.resolve_work_queue_item(
            item_id,
            payload,
            _actor_from_request(req),
        )

    def _unwrap_fn_params(body: dict[str, Any] | None) -> dict[str, Any]:
        if not body:
            return {}
        if set(body.keys()) == {"params"} and isinstance(body.get("params"), dict):
            return dict(body["params"])
        return dict(body)

    @app.post("/api/functions/{fn_name}")
    def api_call_function(
        fn_name: str,
        req: Request,
        body: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        return service.call_function(
            fn_name,
            _unwrap_fn_params(body),
            _actor_from_request(req),
        )

    @app.post("/api/sandboxes")
    def api_create_sandbox(req: Request) -> dict[str, Any]:
        return service.create_sandbox(_actor_from_request(req))

    @app.post("/api/sandboxes/{sid}/submit")
    def api_sandbox_submit(
        sid: str,
        payload: _SandboxSubmitRequest,
        req: Request,
    ) -> dict[str, Any]:
        return service.submit_sandbox_action(sid, payload, _actor_from_request(req))

    @app.post("/api/sandboxes/{sid}/function/{fn_name}")
    def api_sandbox_function(
        sid: str,
        fn_name: str,
        req: Request,
        body: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        return service.compare_sandbox_function(
            sid,
            fn_name,
            _unwrap_fn_params(body),
            _actor_from_request(req),
        )

    @app.get("/api/sandboxes/{sid}/objects/{object_type}")
    def api_sandbox_objects(
        sid: str,
        object_type: str,
        req: Request,
        status: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        actor = _actor_from_request(req)
        with service._lock:
            sbx = service._require_sandbox(sid, actor)
            return service.list_objects(
                object_type,
                actor,
                status=status,
                q=q,
                runtime_override=sbx.fork_runtime,
            )

    @app.get("/api/sandboxes/{sid}/diff")
    def api_sandbox_diff(sid: str, req: Request) -> dict[str, Any]:
        return service.diff_sandbox(sid, _actor_from_request(req))

    @app.post("/api/sandboxes/{sid}/promote")
    def api_sandbox_promote(sid: str, req: Request) -> dict[str, Any]:
        return service.promote_sandbox(sid, _actor_from_request(req))

    @app.delete("/api/sandboxes/{sid}")
    def api_sandbox_destroy(sid: str, req: Request) -> dict[str, Any]:
        return service.destroy_sandbox(sid, _actor_from_request(req))

    @app.get("/api/audit")
    def api_audit(req: Request, status: str | None = None) -> dict[str, Any]:
        return service.get_audit(_actor_from_request(req), status=status)

    @app.get("/api/admin/devices")
    def api_admin_devices(req: Request) -> dict[str, Any]:
        return service.admin_devices(_actor_from_request(req))

    @app.get("/api/admin/autonomy")
    def api_admin_get_autonomy(req: Request) -> dict[str, Any]:
        return service.admin_get_autonomy(_actor_from_request(req))

    @app.put("/api/admin/autonomy")
    def api_admin_put_autonomy(
        payload: _AutonomyUpdatePayload,
        req: Request,
    ) -> dict[str, Any]:
        return service.admin_update_autonomy(payload, _actor_from_request(req))

    @app.get("/api/admin/org")
    def api_admin_org(req: Request) -> dict[str, Any]:
        return service.admin_org(_actor_from_request(req))

    @app.post("/api/blueprint/expert/chat")
    def api_blueprint_expert_chat(
        payload: _M1ExpertChatPayload,
        req: Request,
    ) -> dict[str, Any]:
        return service.m1_expert_chat(payload, _actor_from_request(req))

    @app.get("/api/blueprint/drafts/{draft_id}")
    def api_blueprint_draft_get(draft_id: str, req: Request) -> dict[str, Any]:
        return service.get_m1_draft(draft_id, _actor_from_request(req))

    @app.delete("/api/blueprint/drafts/{draft_id}")
    def api_blueprint_draft_delete(draft_id: str, req: Request) -> dict[str, Any]:
        return service.delete_m1_draft(draft_id, _actor_from_request(req))

    @app.post("/api/blueprint/drafts/{draft_id}/apply")
    def api_blueprint_draft_apply(draft_id: str, req: Request) -> dict[str, Any]:
        return service.apply_m1_draft(draft_id, _actor_from_request(req))

    @app.post("/api/assistant/chat")
    def api_assistant_chat(payload: _AssistantPayload, req: Request) -> dict[str, Any]:
        return service.assistant_chat(payload, _actor_from_request(req))

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> Any:
        cleaned = (full_path or "").lstrip("/")
        if cleaned == "api" or cleaned.startswith("api/"):
            return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
        if resolved_static.is_dir():
            if cleaned:
                candidate = (resolved_static / cleaned).resolve()
                try:
                    candidate.relative_to(resolved_static.resolve())
                except ValueError:
                    return JSONResponse(status_code=404, content={"ok": False, "error": "forbidden"})
                if candidate.is_file():
                    media_type, _ = mimetypes.guess_type(str(candidate))
                    return FileResponse(candidate, media_type=media_type or "application/octet-stream")
            index_file = resolved_static / "index.html"
            if index_file.is_file():
                return FileResponse(index_file, media_type="text/html; charset=utf-8")
        return HTMLResponse(
            "<html><body><h3>OpenOntology Console API Running</h3>"
            "<p>Static frontend bundle not built at web/dist.</p></body></html>"
        )

    return app


@dataclass
class ConsoleServerHandle:
    """Running Uvicorn thread handle for programmatic & E2E verification."""

    service: ConsoleService
    app: FastAPI
    server: uvicorn.Server
    thread: threading.Thread
    sock: socket.socket
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)
        try:
            self.sock.close()
        except OSError:
            pass
        self.service.close()


def start_console_server(
    service: ConsoleService,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    static_dir: Path | str | None = None,
) -> ConsoleServerHandle:
    """Start the pure M2 console ASGI server on a background thread and wait until ready."""
    app = create_console_app(service, static_dir=static_dir)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    actual_host, actual_port = sock.getsockname()[:2]

    config = uvicorn.Config(
        app,
        host=str(actual_host),
        port=int(actual_port),
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name=f"open-ontology-console-{actual_port}",
        daemon=True,
    )
    thread.start()

    deadline = threading.Event()
    for _ in range(100):
        if server.started or not thread.is_alive():
            break
        deadline.wait(0.02)

    return ConsoleServerHandle(
        service=service,
        app=app,
        server=server,
        thread=thread,
        sock=sock,
        host=str(actual_host),
        port=int(actual_port),
    )
