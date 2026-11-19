"""OpenOntology 统一作战指挥中心（Command Center）。

同一个深色科技风单页应用里，同时管理：

* **Ontology 蓝图（PIM）** —— 纯静态元模型：ObjectTypes / ObjectProperties /
  Interfaces / Functions / Actions / Automations，以及自动排布的内联 SVG 拓扑；
* **Instance 实例（Runtime + PSM）** —— 某个蓝图挂上数据库与外部系统连接器
  后的活体运行环境：实时世界状态、单步干跑、MCTS 多步沙盘、正式执行、
  审计账本、Approve / Revert、Fork 平行沙盘、一键拔除 SaaS。

向后兼容：``WorkshopServer(addr, runtime)`` 与 ``start_workshop_server(runtime)``
仍然原样可用 —— 内部会把单个 runtime 包装成「1 个默认蓝图 + 1 个默认实例」的
:class:`CommandCenterRegistry`。多实例场景传入 ``registry=`` 或使用
:func:`start_command_center_server`。

本模块是纯传输边界：不懂任何具体业务，只把 runtime / registry 的能力暴露成 JSON。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlparse

from .meta_admin import MetaAdminService
from .model_views import describe_behavior, describe_openapi, describe_tbox
from .osdk import AIPAgentExecutor, iter_agent_turn, run_agent_turn
from .runtime import OntologyRuntime
from .spec import (
    ActionNotFoundError,
    Actor,
    FunctionNotFoundError,
    ObjectNotFoundError,
    ObjectType,
    OntologySpec,
    OpenOntologyError,
    PermissionDeniedError,
    UnknownObjectTypeError,
)
from .webhooks import OutboundWebhookDispatcher, WebhookFilter
from .workshop_ui import (
    DISPLAY,
    DISPLAY_JS,
    DOCS_PAGE_HTML,
    TOPOLOGY_JS,
    TOPOLOGY_PAGE_HTML,
    display_json,
)
from .workspaces import (
    WorkspaceCatalog,
    WorkspaceConflictError,
    WorkspaceDescriptor,
)

__all__ = [
    "ConnectorMount",
    "OntologyCatalogEntry",
    "OntologyInstanceEntry",
    "SymbolicDeviceEntry",
    "RuntimeCatalogEntry",
    "ActorTokenEntry",
    "CommandCenterRegistry",
    "WorkshopServer",
    "WorkshopHandle",
    "start_workshop_server",
    "start_command_center_server",
]

#: 默认纯内存宿体编号：无外设的沙盘实例共用同一台宿主。
DEFAULT_RUNTIME_ID = "rt-default-memory"

def _compute_live_revision(runtime: Any) -> tuple[dict[str, Any], str]:
    """Build deterministic state_fingerprint + lightweight content revision digest."""
    fp = runtime.state_fingerprint()
    conn = runtime.connection
    obj_rows = conn.execute(
        "SELECT object_type, object_id, version, properties_json "
        "FROM ontology_objects ORDER BY object_type, object_id"
    ).fetchall()
    ov_rows = conn.execute(
        "SELECT object_type, object_id, patch_json "
        "FROM ontology_overlay ORDER BY object_type, object_id"
    ).fetchall()
    link_rows = conn.execute(
        "SELECT link_type, source_type, source_id, target_type, target_id "
        "FROM ontology_links ORDER BY link_type, source_type, source_id, target_type, target_id"
    ).fetchall()
    run_rows = conn.execute(
        "SELECT run_id, status, approved_by, reverted_by_run_id "
        "FROM ontology_action_runs ORDER BY rowid"
    ).fetchall()
    db_ino = 0
    db_p = getattr(runtime, "db_path", None)
    if db_p and str(db_p) != ":memory:":
        try:
            db_ino = Path(db_p).stat().st_ino
        except OSError:
            db_ino = 0
    digest_payload = json.dumps(
        {
            "ino": db_ino,
            "obj": [tuple(r) for r in obj_rows],
            "ov": [tuple(r) for r in ov_rows],
            "links": [tuple(r) for r in link_rows],
            "runs": [tuple(r) for r in run_rows],
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    revision = hashlib.sha1(digest_payload).hexdigest()[:16]
    return fp, revision


_REPO_ROOT = Path(__file__).resolve().parent.parent

#: 内核默认只暴露 M2 平台宪法与词典；具体 M1/M0 文档（如演进真值表）由宿主通过
#: ``registry.register_doc(...)`` 注入。
_DEFAULT_DOC_FILES: dict[str, tuple[str, str, str]] = {
    "architecture": (
        "OpenOntology 架构设计规范",
        "docs/ARCHITECTURE.md",
        "🏛 架构规范",
    ),
    "palantir": (
        "Palantir Foundry / AIP vs OpenOntology 对标与协同指南",
        "docs/PALANTIR_AND_OPEN_ONTOLOGY.md",
        "📖 Palantir 对比",
    ),
    "glossary": (
        "OpenOntology 概念词典",
        "GLOSSARY.md",
        "📚 概念字典",
    ),
}

#: 八大全景治理切面（Core 之上的系统调用切面层）。
RUNTIME_ASPECTS = [
    "ColumnMaskingAspect",
    "SecurityAndAuthorityAspect",
    "IdempotencyAndOCCAspect",
    "ApprovalGateAspect",
    "RejectionAuditAspect",
    "AutomationReflexAspect",
    "IdleAwareCallbackAspect",
    "HomomorphicSettlementAspect",
]


# ---------------------------------------------------------------------------
# 注册中心数据结构
# ---------------------------------------------------------------------------
@dataclass
class ConnectorMount:
    """后台数据库 / 企业资源计划 / 客户关系 / 软件服务 / 仓储 / 机械臂挂载状态。"""

    name: str
    kind: str
    endpoint: str
    status: str = "CONNECTED"  # CONNECTED | UNPLUGGED | STANDBY


@dataclass
class OntologyCatalogEntry:
    """Ontology 蓝图条目。"""

    ontology_id: str
    name: str
    category: str = "Enterprise"
    description: str = ""
    spec: OntologySpec = field(default_factory=OntologySpec)


@dataclass
class OntologyInstanceEntry:
    """Instance 实例化条目。"""

    instance_id: str
    ontology_id: str
    label: str
    description: str
    db_uri: str
    runtime: OntologyRuntime
    runtime_id: str = DEFAULT_RUNTIME_ID
    connectors: list[ConnectorMount] = field(default_factory=list)
    default_actor: Actor | None = None
    sample_action_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcts_config: dict[str, Any] | None = None
    unplug_fn: Callable[[], dict[str, Any]] | None = None

    @property
    def workspace_id(self) -> str:
        """工作空间身份；与历史 ``instance_id`` 是同一个值、同一份条目。"""
        return self.instance_id

    @property
    def model_id(self) -> str:
        """领域模型身份；与历史 ``ontology_id`` 是同一个值。"""
        return self.ontology_id


@dataclass
class SymbolicDeviceEntry:
    """外部工具/设备：带 OpenAPI 契约的外部系统（仓储 / 物流 / 软件服务 / 数据库…）。"""

    device_id: str
    name: str
    kind: str
    endpoint: str
    status: str = "CONNECTED"
    openapi_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeCatalogEntry:
    """Runtime 宿体：Core 微内核 + Aspect 切面 + DeviceAdapter 设备驱动。"""

    runtime_id: str
    label: str
    storage_kind: str
    db_uri: str
    description: str = ""
    aspects: list[str] = field(default_factory=list)
    adapters: list[str] = field(default_factory=list)
    ontology_ids: list[str] = field(default_factory=list)
    instance_ids: list[str] = field(default_factory=list)


def _openapi_schema(connector: ConnectorMount) -> dict[str, Any]:
    """把外部连接器投影成最小可用的 OpenAPI 3.1 契约。"""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{connector.name} ({connector.kind})",
            "version": "1.0.0",
            "description": f"{connector.kind} 外部工具/系统的 OpenAPI 契约。",
        },
        "servers": [{"url": connector.endpoint}],
        "paths": {
            "/": {
                "get": {
                    "operationId": "describe",
                    "summary": f"读取 {connector.name} 的可用资源",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


class _WorkspaceUnavailableError(Exception):
    """工作空间不可读的内部信号。

    消息只能是可信固定字面量：公开清单绝不回传可能夹带明文取值的异常消息。
    """


def _workspace_runtime_id(
    workspace_id: str,
    db_uri: str,
    connectors: Iterable[ConnectorMount],
) -> str:
    """按存储类型与外设决定宿体编号（注册与清单持久化共用同一规则）。"""
    storage_kind = "memory" if ":memory:" in db_uri else "sqlite"
    has_peripherals = any(True for _ in connectors)
    if storage_kind == "memory" and not has_peripherals:
        return DEFAULT_RUNTIME_ID
    return f"rt-{workspace_id}"


class _UnauthorizedError(OpenOntologyError):
    """Raised when a Bearer token is missing or invalid."""


@dataclass(frozen=True)
class ActorTokenEntry:
    """绑定凭证与真实 Actor 身份（actor_id / role / clearance_tags / 可选工作空间限制）。"""

    token: str
    actor: Actor
    workspace_id: str | None = None


def _http_webhook_sender(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, str]:
    """HTTP transport sender injected into :class:`OutboundWebhookDispatcher`."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return int(exc.code), detail


class _SimulationLedger:
    """沙盘推演持久化账本（独立于主世界业务库，专用于留痕与本体 Bug 诊断）。

    只记录平行宇宙的推演步骤、校验结果、Diff 与异常原因，绝不触碰主库的
    ``ontology_objects`` / ``ontology_action_runs`` / ``schema.sql``。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conns: dict[str, sqlite3.Connection] = {}

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _ledger_target(entry: OntologyInstanceEntry | None) -> str:
        if entry is not None and entry.runtime is not None:
            db_path = getattr(entry.runtime, "db_path", None)
            if db_path is not None and str(db_path) != ":memory:":
                return str(Path(db_path).with_suffix(".sim_ledger.sqlite3"))
        return ":memory:"

    def _conn_for(
        self, workspace_id: str, entry: OntologyInstanceEntry | None = None
    ) -> sqlite3.Connection:
        target = self._ledger_target(entry)
        conn = self._conns.get(workspace_id)
        if conn is not None:
            return conn
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sandbox_simulations (
                sim_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                params_json TEXT NOT NULL,
                prior_steps_json TEXT NOT NULL,
                valid INTEGER NOT NULL,
                requires_approval INTEGER NOT NULL,
                error_code TEXT,
                error_message TEXT,
                diff_json TEXT NOT NULL,
                base_fingerprint TEXT NOT NULL,
                committed_run_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        self._conns[workspace_id] = conn
        return conn

    def record(
        self,
        entry: OntologyInstanceEntry,
        *,
        session_id: str,
        step_index: int,
        action_name: str,
        actor: Actor,
        params: dict[str, Any],
        prior_steps: list[dict[str, Any]],
        valid: bool,
        requires_approval: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        diff: dict[str, Any] | None = None,
        base_fingerprint: str = "",
    ) -> dict[str, Any]:
        sim_id = f"SIM-{uuid.uuid4().hex[:12]}"
        created_at = self._utcnow()
        with self._lock:
            conn = self._conn_for(entry.instance_id, entry)
            conn.execute(
                """
                INSERT INTO sandbox_simulations (
                    sim_id, workspace_id, session_id, step_index, action_type,
                    actor_id, actor_role, params_json, prior_steps_json,
                    valid, requires_approval, error_code, error_message,
                    diff_json, base_fingerprint, committed_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    sim_id,
                    entry.instance_id,
                    session_id,
                    int(step_index),
                    action_name,
                    actor.actor_id,
                    actor.role,
                    json.dumps(params, ensure_ascii=False, default=str),
                    json.dumps(prior_steps, ensure_ascii=False, default=str),
                    1 if valid else 0,
                    1 if requires_approval else 0,
                    error_code,
                    error_message,
                    json.dumps(diff or {"object_changes": [], "link_changes": []}, ensure_ascii=False, default=str),
                    base_fingerprint,
                    created_at,
                ),
            )
            conn.commit()
        return {
            "sim_id": sim_id,
            "session_id": session_id,
            "step_index": int(step_index),
            "created_at": created_at,
        }

    def mark_first_step_committed(
        self,
        entry: OntologyInstanceEntry,
        *,
        session_id: str,
        run_id: str,
    ) -> None:
        if not session_id or not run_id:
            return
        with self._lock:
            conn = self._conn_for(entry.instance_id, entry)
            conn.execute(
                """
                UPDATE sandbox_simulations
                SET committed_run_id = ?
                WHERE workspace_id = ? AND session_id = ? AND step_index = 1
                """,
                (run_id, entry.instance_id, session_id),
            )
            conn.commit()

    def list(
        self,
        entry: OntologyInstanceEntry,
        *,
        valid: bool | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._conn_for(entry.instance_id, entry)
            clauses = ["workspace_id = ?"]
            args: list[Any] = [entry.instance_id]
            if valid is not None:
                clauses.append("valid = ?")
                args.append(1 if valid else 0)
            if session_id:
                clauses.append("session_id = ?")
                args.append(session_id)
            args.append(max(1, min(int(limit), 500)))
            rows = conn.execute(
                f"""
                SELECT * FROM sandbox_simulations
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                args,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "sim_id": row["sim_id"],
                    "workspace_id": row["workspace_id"],
                    "session_id": row["session_id"],
                    "step_index": int(row["step_index"]),
                    "action_type": row["action_type"],
                    "actor_id": row["actor_id"],
                    "actor_role": row["actor_role"],
                    "params": json.loads(row["params_json"]),
                    "prior_steps": json.loads(row["prior_steps_json"]),
                    "valid": bool(row["valid"]),
                    "requires_approval": bool(row["requires_approval"]),
                    "error_code": row["error_code"],
                    "error_message": row["error_message"],
                    "diff": json.loads(row["diff_json"]),
                    "base_fingerprint": row["base_fingerprint"],
                    "committed_run_id": row["committed_run_id"],
                    "created_at": row["created_at"],
                }
            )
        return out

    def clear(self, entry: OntologyInstanceEntry) -> int:
        with self._lock:
            conn = self._conn_for(entry.instance_id, entry)
            cur = conn.execute(
                "DELETE FROM sandbox_simulations WHERE workspace_id = ?",
                (entry.instance_id,),
            )
            conn.commit()
            return int(cur.rowcount or 0)


class CommandCenterRegistry:
    """多蓝图 / 多实例注册中心。"""

    def __init__(self, catalog: WorkspaceCatalog | None = None) -> None:
        self.ontologies: dict[str, OntologyCatalogEntry] = {}
        self.instances: dict[str, OntologyInstanceEntry] = {}
        self.runtimes: dict[str, RuntimeCatalogEntry] = {}
        self.symbolic_devices: dict[str, SymbolicDeviceEntry] = {}
        self.actor_tokens: dict[str, ActorTokenEntry] = {}
        self.extra_docs: dict[str, tuple[str, str, str]] = {}
        self.webhooks: OutboundWebhookDispatcher = OutboundWebhookDispatcher(
            sender=_http_webhook_sender
        )
        self.simulation_ledger: _SimulationLedger = _SimulationLedger()
        #: 可选的工作空间身份清单；只有文件落盘的 runtime 会被写入。
        self.catalog = catalog
        self.lock = threading.RLock()
        self.register_runtime(
            DEFAULT_RUNTIME_ID,
            "默认内存 Runtime (In-Memory)",
            "memory",
            "sqlite://:memory:",
            description="纯内存零外设沙盘宿体，用于验证 Ontology 与虚拟现实推演",
            aspects=list(RUNTIME_ASPECTS),
        )

    def register_doc(
        self,
        key: str,
        title: str,
        rel_path: str,
        *,
        tab_label: str | None = None,
    ) -> None:
        """为文档中心注入宿主扩展文档（不修改 M2 内核默认文档列表）。"""
        with self.lock:
            self.extra_docs[str(key)] = (
                str(title),
                str(rel_path),
                str(tab_label or title),
            )

    def doc_entries(self) -> dict[str, tuple[str, str, str]]:
        """返回完整文档目录（M2 默认文档 + 宿主注册文档）。"""
        entries = dict(_DEFAULT_DOC_FILES)
        with self.lock:
            entries.update(self.extra_docs)
        return entries

    # -- actor token binding -----------------------------------------------
    def register_actor_token(
        self,
        token: str,
        actor: Actor,
        *,
        workspace_id: str | None = None,
    ) -> ActorTokenEntry:
        """Bind a bearer token to an explicit Actor (and optional workspace)."""
        clean = token.strip()
        if not clean:
            raise ValueError("actor token must not be empty")
        entry = ActorTokenEntry(token=clean, actor=actor, workspace_id=workspace_id)
        with self.lock:
            self.actor_tokens[clean] = entry
        return entry

    def revoke_actor_token(self, token: str) -> None:
        with self.lock:
            self.actor_tokens.pop(token, None)

    def resolve_actor_token(
        self, token: str, *, workspace_id: str | None = None
    ) -> Actor:
        with self.lock:
            binding = self.actor_tokens.get(token)
        if binding is None:
            raise _UnauthorizedError("invalid or expired actor bearer token")
        if (
            workspace_id is not None
            and binding.workspace_id is not None
            and binding.workspace_id != workspace_id
        ):
            raise PermissionDeniedError(
                f"actor token is scoped to workspace {binding.workspace_id!r}, "
                f"not {workspace_id!r}"
            )
        return binding.actor

    # -- runtime / device registration -------------------------------------
    def register_runtime(
        self,
        runtime_id: str,
        label: str,
        storage_kind: str,
        db_uri: str,
        description: str = "",
        aspects: Iterable[str] | None = None,
        adapters: Iterable[str] | None = None,
        ontology_ids: Iterable[str] | None = None,
        instance_ids: Iterable[str] | None = None,
    ) -> RuntimeCatalogEntry:
        entry = RuntimeCatalogEntry(
            runtime_id=runtime_id,
            label=label,
            storage_kind=storage_kind,
            db_uri=db_uri,
            description=description,
            aspects=list(aspects or []),
            adapters=list(adapters or []),
            ontology_ids=list(ontology_ids or []),
            instance_ids=list(instance_ids or []),
        )
        self.runtimes[runtime_id] = entry
        return entry

    def register_symbolic_device(
        self,
        device_id: str,
        name: str,
        kind: str,
        endpoint: str,
        status: str = "CONNECTED",
        openapi_schema: dict[str, Any] | None = None,
    ) -> SymbolicDeviceEntry:
        entry = SymbolicDeviceEntry(
            device_id=device_id,
            name=name,
            kind=kind,
            endpoint=endpoint,
            status=status,
            openapi_schema=dict(openapi_schema or {}),
        )
        self.symbolic_devices[device_id] = entry
        return entry

    def _sync_devices(self, entry: OntologyInstanceEntry) -> None:
        """将外部连接器统一登记为 OpenAPI 工具/设备契约。"""
        for idx, connector in enumerate(entry.connectors, start=1):
            self.register_symbolic_device(
                f"dev-{entry.instance_id}-openapi-{idx}",
                connector.name,
                connector.kind,
                connector.endpoint,
                connector.status,
                _openapi_schema(connector),
            )
        entry.runtime_id = self._bind_runtime(entry)

    def _bind_runtime(self, entry: OntologyInstanceEntry) -> str:
        """为实例绑定宿体：零外设内存沙盘共用默认宿体，其余各建一台。"""
        storage_kind = "memory" if ":memory:" in entry.db_uri else "sqlite"
        adapters = [connector.name for connector in entry.connectors]
        runtime_id = _workspace_runtime_id(
            entry.instance_id, entry.db_uri, entry.connectors
        )
        if runtime_id == DEFAULT_RUNTIME_ID:
            runtime = self.runtimes.get(runtime_id)
            if runtime is None:
                runtime = self.register_runtime(
                    runtime_id,
                    "默认内存 Runtime (In-Memory)",
                    "memory",
                    "sqlite://:memory:",
                    description="纯内存零外设沙盘宿体，用于验证 Ontology 与虚拟现实推演",
                    aspects=list(RUNTIME_ASPECTS),
                )
        else:
            runtime_id = f"rt-{entry.instance_id}"
            runtime = self.register_runtime(
                runtime_id,
                f"{entry.label} · {storage_kind.upper()} Runtime",
                storage_kind,
                entry.db_uri,
                description=(
                    f"承载 {entry.label} 的治理宿体：{storage_kind} 存储 + "
                    f"{len(RUNTIME_ASPECTS)} 大切面 + {len(adapters)} 个设备驱动。"
                ),
                aspects=list(RUNTIME_ASPECTS),
                adapters=adapters,
                ontology_ids=[entry.ontology_id],
                instance_ids=[entry.instance_id],
            )
        if entry.ontology_id not in runtime.ontology_ids:
            runtime.ontology_ids.append(entry.ontology_id)
        if entry.instance_id not in runtime.instance_ids:
            runtime.instance_ids.append(entry.instance_id)
        for name in adapters:
            if name not in runtime.adapters:
                runtime.adapters.append(name)
        return runtime_id

    # -- registration ------------------------------------------------------
    def register_ontology(
        self,
        ontology_id: str,
        name: str,
        spec: OntologySpec,
        category: str = "Enterprise",
        description: str = "",
    ) -> OntologyCatalogEntry:
        entry = OntologyCatalogEntry(
            ontology_id=ontology_id,
            name=name,
            category=category,
            description=description,
            spec=spec,
        )
        self.ontologies[ontology_id] = entry
        return entry

    def register_workspace(
        self,
        workspace_id: str,
        model_id: str,
        label: str,
        runtime: OntologyRuntime,
        db_uri: str = "sqlite://:memory:",
        description: str = "",
        connectors: Iterable[ConnectorMount] | None = None,
        default_actor: Actor | None = None,
        sample_action_params: dict[str, dict[str, Any]] | None = None,
        mcts_config: dict[str, Any] | None = None,
        unplug_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> OntologyInstanceEntry:
        """注册工作空间；``instance_id`` 是其历史别名，指向同一份条目。

        只有 ``runtime.db_path`` 真正落盘的工作空间才会写入可选清单；展示用
        的 ``db_uri`` 绝不参与持久化判断。冲突注册在改动任何内存状态之前失败。
        """
        if model_id not in self.ontologies:
            raise KeyError(f"unknown ontology {model_id!r}")
        connector_list = list(connectors or [])
        with self.lock:
            existing = self.instances.get(workspace_id)
            if existing is not None:
                if self._same_workspace(existing, model_id, runtime, db_uri, connector_list):
                    return existing
                raise WorkspaceConflictError(
                    f"workspace {workspace_id!r} is already registered "
                    "with a different identity"
                )

            entry = OntologyInstanceEntry(
                instance_id=workspace_id,
                ontology_id=model_id,
                label=label,
                description=description,
                db_uri=db_uri,
                runtime=runtime,
                connectors=connector_list,
                default_actor=default_actor,
                sample_action_params=dict(sample_action_params or {}),
                mcts_config=dict(mcts_config) if mcts_config else None,
                unplug_fn=unplug_fn,
            )
            entry.runtime_id = _workspace_runtime_id(
                workspace_id, db_uri, connector_list
            )
            descriptor = self._workspace_descriptor(entry)
            if descriptor is not None:
                # 先写清单：清单冲突 / 写入失败时内存注册中心保持原样。
                self.catalog.register(descriptor)  # type: ignore[union-attr]

            self.instances[workspace_id] = entry
            self._sync_devices(entry)
            self._attach_webhook_hook(workspace_id, runtime)
            return entry

    def _attach_webhook_hook(self, world_id: str, runtime: OntologyRuntime | None) -> None:
        if runtime is None:
            return
        for existing in getattr(runtime, "_post_commit_hooks", ()):
            if getattr(existing, "_webhook_world_id", None) == world_id:
                return
        runtime.add_post_commit_hook(self.webhooks.make_post_commit_hook(world_id))

    def register_instance(
        self,
        instance_id: str,
        ontology_id: str,
        label: str,
        runtime: OntologyRuntime,
        db_uri: str = "sqlite://:memory:",
        description: str = "",
        connectors: Iterable[ConnectorMount] | None = None,
        default_actor: Actor | None = None,
        sample_action_params: dict[str, dict[str, Any]] | None = None,
        mcts_config: dict[str, Any] | None = None,
        unplug_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> OntologyInstanceEntry:
        """历史入口：与 :meth:`register_workspace` 共用同一份底层条目。"""
        return self.register_workspace(
            instance_id,
            ontology_id,
            label,
            runtime,
            db_uri=db_uri,
            description=description,
            connectors=connectors,
            default_actor=default_actor,
            sample_action_params=sample_action_params,
            mcts_config=mcts_config,
            unplug_fn=unplug_fn,
        )

    def _workspace_descriptor(
        self, entry: OntologyInstanceEntry
    ) -> WorkspaceDescriptor | None:
        """只为真正文件落盘的 runtime 生成清单条目；内存 runtime 永远临时。"""
        if self.catalog is None:
            return None
        db_path = str(entry.runtime.db_path)
        if db_path == ":memory:":
            return None
        return WorkspaceDescriptor(
            workspace_id=entry.instance_id,
            model_id=entry.ontology_id,
            runtime_id=entry.runtime_id,
            db_path=db_path,
        )

    def _same_workspace(
        self,
        existing: OntologyInstanceEntry,
        model_id: str,
        runtime: OntologyRuntime,
        db_uri: str,
        connectors: Iterable[ConnectorMount],
    ) -> bool:
        if existing.ontology_id != model_id:
            return False
        existing_db = str(existing.runtime.db_path)
        incoming_db = str(runtime.db_path)
        if existing_db == ":memory:" or incoming_db == ":memory:":
            if existing.runtime is not runtime:
                return False
        elif Path(existing_db).resolve() != Path(incoming_db).resolve():
            return False
        expected_runtime_id = _workspace_runtime_id(
            existing.instance_id, db_uri, connectors
        )
        return existing.runtime_id == expected_runtime_id

    # -- lookup ------------------------------------------------------------
    def get_ontology(self, ontology_id: str) -> OntologyCatalogEntry:
        if ontology_id not in self.ontologies:
            raise KeyError(f"unknown ontology {ontology_id!r}")
        return self.ontologies[ontology_id]

    def get_instance(self, instance_id: str) -> OntologyInstanceEntry:
        if instance_id not in self.instances:
            raise KeyError(f"unknown instance {instance_id!r}")
        return self.instances[instance_id]

    def get_workspace(self, workspace_id: str) -> OntologyInstanceEntry:
        """按工作空间身份取已加载条目；仅清单中存在但未加载的请用清单解析。"""
        return self.get_instance(workspace_id)

    def instances_for(self, ontology_id: str) -> list[OntologyInstanceEntry]:
        return [
            entry
            for entry in self.instances.values()
            if entry.ontology_id == ontology_id
        ]

    def default_instance(self) -> OntologyInstanceEntry:
        if not self.instances:
            raise KeyError("registry has no instances")
        return next(iter(self.instances.values()))

    def default_instance_id(self) -> str:
        return self.default_instance().instance_id

    # -- workspace listing (read-only) -------------------------------------
    def list_workspaces(self, actor: Actor) -> list[dict[str, Any]]:
        """列出全部工作空间及其聚合根对象（纯读投影）。

        每个工作空间一行，身份为 ``workspace_id``（当前与 ``instance_id``
        同源），根对象的完整身份是 工作空间 + 类型 + 对象 ID。根对象一律经
        ``runtime.query_objects`` 读取，因此行级安全与列级脱敏照常生效。
        不可读工作空间的 ``aggregate_roots`` 是 ``None``（不是空列表），并给出
        只含可信固定字面量或异常类型的 ``reason``。仅在身份清单中存在、尚未
        打开的工作空间也会显式出现（``availability='not_loaded'``），列表过程
        绝不打开或创建数据库。
        """
        with self.lock:
            entries = [self.instances[key] for key in sorted(self.instances)]
            loaded = set(self.instances)
            catalog = self.catalog
            descriptors = catalog.descriptors() if catalog is not None else ()
        rows = [self._workspace_record(entry, actor) for entry in entries]
        rows.extend(
            self._catalog_workspace_record(descriptor)
            for descriptor in descriptors
            if descriptor.workspace_id not in loaded
        )
        rows.sort(key=lambda row: row["workspace_id"])
        return rows

    def _catalog_workspace_record(
        self, descriptor: WorkspaceDescriptor
    ) -> dict[str, Any]:
        """清单里存在但未加载的工作空间：只读声明，不碰数据库。"""
        model = self.ontologies.get(descriptor.model_id)
        root_types = (
            sorted(
                name
                for name, cls in model.spec.object_types.items()
                if cls.is_aggregate_root()
            )
            if model is not None
            else []
        )
        return {
            "workspace_id": descriptor.workspace_id,
            "model_id": descriptor.model_id,
            "runtime_id": descriptor.runtime_id,
            "availability": "not_loaded",
            "root_status": "not_loaded",
            "reason": None if model is not None else "model_not_registered",
            "aggregate_root_types": root_types,
            "aggregate_roots": None,
        }

    def _workspace_record(
        self, entry: OntologyInstanceEntry, actor: Actor
    ) -> dict[str, Any]:
        runtime = entry.runtime
        spec = (
            runtime.spec
            if runtime is not None
            else self.ontologies[entry.ontology_id].spec
        )
        root_types = sorted(
            name for name, cls in spec.object_types.items() if cls.is_aggregate_root()
        )
        roots: list[dict[str, Any]] | None = []
        availability = "available"
        reason: str | None = None
        try:
            if runtime is None:
                raise _WorkspaceUnavailableError("runtime_missing")
            if not runtime.is_open:
                raise _WorkspaceUnavailableError("runtime_closed")
            for type_name in root_types:
                cls = spec.object_types[type_name]
                for obj in runtime.query_objects(cls, actor):
                    roots.append(
                        {
                            "workspace_id": entry.instance_id,
                            "object_type": type_name,
                            "object_id": obj.primary_key_value(),
                            "title": obj.title_value(),
                        }
                    )
            roots.sort(key=lambda root: (root["object_type"], root["object_id"]))
        except _WorkspaceUnavailableError as exc:
            availability, reason, roots = "unavailable", str(exc), None
        except Exception as exc:  # 单个工作空间不可读，不得拖垮整张清单
            # 只暴露异常类型：校验/查询消息里可能带未脱敏的原始取值。
            availability, reason, roots = "unavailable", type(exc).__name__, None

        if availability == "unavailable":
            root_status = "unavailable"
        elif not root_types:
            root_status = "no_declared_roots"
        elif roots:
            root_status = "roots"
        else:
            root_status = "empty"
        return {
            "workspace_id": entry.instance_id,
            "model_id": entry.ontology_id,
            "runtime_id": entry.runtime_id,
            "availability": availability,
            "root_status": root_status,
            "reason": reason,
            "aggregate_root_types": root_types,
            "aggregate_roots": roots,
        }

    # -- fork --------------------------------------------------------------
    def fork_instance(
        self,
        source_instance_id: str,
        new_instance_id: str | None = None,
        label: str | None = None,
    ) -> OntologyInstanceEntry:
        source = self.get_instance(source_instance_id)
        candidate = new_instance_id
        if not candidate:
            index = 1
            while f"{source.instance_id}-fork-{index}" in self.instances:
                index += 1
            candidate = f"{source.instance_id}-fork-{index}"
        if candidate in self.instances:
            raise KeyError(f"instance {candidate!r} already exists")

        forked = source.runtime.fork()
        entry = OntologyInstanceEntry(
            instance_id=candidate,
            ontology_id=source.ontology_id,
            label=label or f"{source.label} · 平行沙盘",
            description=f"由 {source.instance_id} 毫秒级克隆的独立世界状态",
            db_uri="sqlite://:memory: (fork)",
            runtime=forked,
            connectors=[
                ConnectorMount(
                    name=item.name,
                    kind=item.kind,
                    endpoint=item.endpoint,
                    status=item.status,
                )
                for item in source.connectors
            ],
            default_actor=source.default_actor,
            sample_action_params=copy.deepcopy(source.sample_action_params),
            mcts_config=dict(source.mcts_config) if source.mcts_config else None,
            unplug_fn=None,
        )
        self.instances[candidate] = entry
        self._sync_devices(entry)
        return entry

    # -- compatibility -----------------------------------------------------
    @classmethod
    def from_runtime(
        cls,
        runtime: OntologyRuntime,
        *,
        actor: Actor | None = None,
        title: str | None = None,
    ) -> "CommandCenterRegistry":
        registry = cls()
        label = title or "Default Instance"
        registry.register_ontology(
            "default",
            title or "Ontology",
            runtime.spec,
            category="Enterprise",
            description="由单个 runtime 自动包装的兼容模式蓝图。",
        )
        registry.register_instance(
            "default",
            "default",
            label=label,
            runtime=runtime,
            db_uri=str(runtime.db_path),
            description="单 runtime 向后兼容世界状态。",
            default_actor=actor,
        )
        return registry


# ---------------------------------------------------------------------------
# 元模型投影
# ---------------------------------------------------------------------------
def _ontology_roles(
    spec: OntologySpec,
    instances: Iterable[OntologyInstanceEntry] = (),
) -> list[str]:
    """按首次出现顺序去重提取某个 Ontology 专属的角色体系。

    角色来源（全部来自该蓝图自己的声明，不读任何全局角色表）：

    1. ``spec.roles`` 中显式声明的模型角色（``RoleDef``）；
    2. 该蓝图下各实例的 ``default_actor.role``；
    3. ``spec.actions`` 中每个动作的 ``allowed_roles`` 与 ``approver_roles``；
    4. ``spec.object_types`` 中各对象类型的 ``__masked_fields__`` 白名单角色。

    均为空时回退 ``["admin"]``，保证前端下拉框永远有可选项。
    """
    roles: list[str] = []

    def add(role: str | None) -> None:
        if role and role not in roles:
            roles.append(role)

    for role_name in spec.roles:
        add(role_name)
    for entry in instances:
        actor = getattr(entry, "default_actor", None)
        if actor is not None:
            add(actor.role)
    for action in spec.actions.values():
        for role in action.allowed_roles:
            add(role)
        for role in action.approver_roles:
            add(role)
    for cls in spec.object_types.values():
        masked = getattr(cls, "__masked_fields__", {}) or {}
        for allowed_roles in masked.values():
            for role in allowed_roles or ():
                add(role)
    return roles or ["admin"]


def _spec_meta(
    spec: OntologySpec,
    instances: Iterable[OntologyInstanceEntry] = (),
) -> dict[str, Any]:
    """把一个 OntologySpec 投影成前端可渲染的完整元模型。

    结构（TBox）与行为（Behavior）直接复用 :mod:`open_ontology.model_views`
    的只读投影；这里只补充工作空间派生的角色体系与 ``spec`` 摘要。
    """
    tbox = describe_tbox(spec)
    behavior = describe_behavior(spec)
    return {
        "spec": spec.summary(),
        "roles": _ontology_roles(spec, instances),
        "object_types": tbox["object_types"],
        "interfaces": tbox["interfaces"],
        "object_properties": tbox["object_properties"],
        "functions": behavior["functions"],
        "actions": behavior["actions"],
        "automations": behavior["automations"],
        "role_defs": behavior["roles"],
    }


def _instance_summary(entry: OntologyInstanceEntry, registry: CommandCenterRegistry) -> dict[str, Any]:
    ontology = registry.ontologies.get(entry.ontology_id)
    runtime = entry.runtime
    return {
        "instance_id": entry.instance_id,
        "ontology_id": entry.ontology_id,
        "ontology_name": ontology.name if ontology else entry.ontology_id,
        "runtime_id": entry.runtime_id,
        "formula": f"{entry.instance_id} = {entry.ontology_id} + {entry.runtime_id}",
        "label": entry.label,
        "description": entry.description,
        "db_uri": entry.db_uri,
        "connectors": [asdict(item) for item in entry.connectors],
        "counts": {
            "objects": runtime.count_objects(),
            "links": _count_links(runtime),
            "runs": len(runtime.list_action_runs()),
        },
        "supports_mcts": bool(entry.mcts_config),
        "supports_unplug": entry.unplug_fn is not None,
        "default_actor_role": entry.default_actor.role if entry.default_actor else None,
        "sample_action_params": {
            key: value for key, value in entry.sample_action_params.items()
        },
    }


def _count_links(runtime: OntologyRuntime) -> int:
    row = runtime.connection.execute(
        "SELECT COUNT(*) AS n FROM ontology_links"
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# 前端单页应用
# ---------------------------------------------------------------------------
_WORKSHOP_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#05080f;--panel:#0c1420;--panel2:#101a28;--line:rgba(120,160,210,.16);
  --txt:#e6eefb;--dim:#8ea3bd;--cy:#38bdf8;--vi:#a78bfa;--gr:#22c55e;--am:#f5a524;--rd:#ef4444;
}
html,body{height:100%}
body{background:
  radial-gradient(1100px 560px at 6% -12%,rgba(56,189,248,.16),transparent 62%),
  radial-gradient(900px 520px at 98% -8%,rgba(129,140,248,.14),transparent 60%),
  linear-gradient(#05080f,#02040a);
  color:var(--txt);font-family:"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif;
  font-feature-settings:"tnum";overflow:hidden}
.shell{display:grid;grid-template-rows:auto auto 1fr;height:100vh}
header{display:flex;align-items:center;gap:16px;padding:11px 18px;
  border-bottom:1px solid var(--line);background:rgba(8,14,22,.86)}
.mark{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-size:19px;
  font-weight:800;color:#03121d;background:linear-gradient(135deg,#38bdf8,#6366f1);
  box-shadow:0 8px 22px rgba(56,189,248,.35)}
.brand b{font-size:15.5px;letter-spacing:.5px}
.brand span{display:block;font-size:9.5px;color:var(--dim);letter-spacing:2px;margin-top:2px}
.chips{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.chip{font-size:11px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);
  color:#b9cbe0;background:rgba(255,255,255,.03);white-space:nowrap}
.chip b{color:var(--cy)}
.role{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim)}
.role select{background:#0d1725;color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:6px 9px;font-size:12px;outline:none}
main{display:grid;grid-template-columns:330px 1fr;gap:10px;padding:10px;min-height:0}
main.full{grid-template-columns:1fr}
[hidden]{display:none !important}
.side{display:flex;flex-direction:column;gap:10px;min-height:0}
.stage{min-height:0;display:flex;flex-direction:column;gap:10px;overflow:auto}
.stage > *{flex-shrink:0}
.panel{background:linear-gradient(165deg,rgba(18,31,49,.9),rgba(8,14,24,.94));
  border:1px solid var(--line);border-radius:14px;display:flex;flex-direction:column;
  min-height:0;overflow:hidden;box-shadow:0 14px 36px rgba(0,0,0,.42)}
.ph{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:8px 12px;border-bottom:1px dashed var(--line);font-size:12.5px;font-weight:700;letter-spacing:.4px}
.ph small{font-weight:500;font-size:10px;color:var(--dim);letter-spacing:1px}
.ph i{color:var(--cy);font-style:normal;margin-right:6px}
.body{overflow:auto;padding:9px 11px;min-height:0}
.list{overflow:auto;padding:8px;display:flex;flex-direction:column;gap:7px;min-height:0}
.card{border:1px solid var(--line);border-left:3px solid rgba(120,160,210,.35);
  border-radius:10px;padding:8px 10px;background:rgba(255,255,255,.025);cursor:pointer;transition:.15s}
.card:hover{border-left-color:var(--cy);background:rgba(56,189,248,.06)}
.card.on{border-color:rgba(56,189,248,.6);border-left-color:var(--cy);background:rgba(56,189,248,.13);
  box-shadow:0 0 0 1px rgba(56,189,248,.2) inset}
.card h4{font-size:12.5px;font-weight:700;display:flex;justify-content:space-between;gap:8px}
.card h4 em{font-style:normal;font-family:"SF Mono",Menlo,monospace;font-size:10px;color:#8fb7d6}
.card .sub{font-size:10.5px;color:var(--dim);margin-top:3px;line-height:1.5}
.badges{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.badge{font-size:9.5px;padding:2px 6px;border-radius:5px;border:1px solid var(--line);color:#a9bdd4;background:rgba(255,255,255,.03)}
.badge.c{color:#a5e4ff;border-color:rgba(56,189,248,.35);background:rgba(56,189,248,.1)}
.badge.g{color:#bbf7d0;border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.1)}
.badge.v{color:#ddd6fe;border-color:rgba(167,139,250,.35);background:rgba(167,139,250,.1)}
.badge.a{color:#fde68a;border-color:rgba(245,165,36,.35);background:rgba(245,165,36,.1)}
.badge.r{color:#fecaca;border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.1)}
button{font-size:11px;padding:5px 10px;border-radius:7px;cursor:pointer;
  border:1px solid rgba(56,189,248,.42);background:rgba(56,189,248,.12);color:#cfeeff}
button:hover{background:rgba(56,189,248,.24)}
button.g{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.12);color:#c9f7d7}
button.g:hover{background:rgba(34,197,94,.24)}
button.a{border-color:rgba(245,165,36,.45);background:rgba(245,165,36,.12);color:#fde4b0}
button.r{border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.12);color:#ffd0d0}
button.v{border-color:rgba(167,139,250,.45);background:rgba(167,139,250,.12);color:#e3dbff}
button.w{width:100%;margin-top:8px}
button:disabled{opacity:.4;cursor:not-allowed}
.hero{padding:14px 16px;border:1px solid var(--line);border-radius:14px;
  background:linear-gradient(150deg,rgba(20,36,58,.92),rgba(8,14,24,.96));box-shadow:0 14px 36px rgba(0,0,0,.42)}
.hero h2{font-size:17px;letter-spacing:.4px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.hero p{font-size:11.5px;color:var(--dim);margin-top:6px;line-height:1.65;max-width:980px}
.hero .acts{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.grid2{display:grid;grid-template-columns:1.15fr 1fr;gap:10px;min-height:0;align-items:start}
.grid2b{display:grid;grid-template-columns:1.05fr 1fr;gap:10px;min-height:0;align-items:start}
.panel.flat .body{overflow:auto;max-height:520px}
.mount{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 12px;border-bottom:1px solid var(--line);
  background:rgba(12,22,36,.7);font-size:11px;color:var(--dim)}
.mount b{color:var(--txt);font-size:12.5px}
.mount .sp{flex:1}
.fp{display:flex;gap:9px;flex-wrap:wrap}
.fp span{font-family:"SF Mono",Menlo,monospace;font-size:10.5px;color:#a5e4ff}
.tabs,.state-tabs{display:flex;gap:5px;flex-wrap:wrap;padding:8px 11px;border-bottom:1px solid var(--line)}
.tab,.state-tab{font-size:11px;padding:4px 9px;border-radius:7px;border:1px solid var(--line);
  background:rgba(255,255,255,.02);color:#b9cbe0;cursor:pointer;white-space:nowrap}
.tab.on,.state-tab.on{background:rgba(56,189,248,.16);border-color:rgba(56,189,248,.55);color:#d6f2ff}
.pillarnav{display:flex;gap:6px;flex-wrap:wrap;padding:0 18px 9px;
  border-bottom:1px solid var(--line);background:rgba(8,14,22,.86)}
.pillar-tab{font-size:12px;font-weight:600;padding:7px 14px;border-radius:9px;letter-spacing:.2px;
  border:1px solid var(--line);background:rgba(255,255,255,.03);color:#b9cbe0;cursor:pointer;
  white-space:nowrap;transition:.15s}
.pillar-tab:hover{background:rgba(56,189,248,.12);color:#d6f2ff}
.pillar-tab.on{color:#eaf8ff;border-color:rgba(56,189,248,.62);
  background:linear-gradient(135deg,rgba(56,189,248,.28),rgba(129,140,248,.22));
  box-shadow:0 0 0 1px rgba(56,189,248,.25) inset}
.devcols{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:start}
.obj{border:1px solid var(--line);border-left:3px solid rgba(120,160,210,.4);
  border-radius:10px;padding:7px 10px;margin-bottom:7px;background:rgba(255,255,255,.025)}
.obj:hover{border-left-color:var(--cy);background:rgba(56,189,248,.05)}
.obj.sim-create{border-left-color:var(--ok);border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.08)}
.obj.sim-update{border-left-color:var(--vi);border-color:rgba(167,139,250,.4);background:rgba(167,139,250,.08)}
.obj.sim-delete{border-left-color:var(--rd);border-color:rgba(239,68,68,.4);background:rgba(239,68,68,.08);opacity:.78}
.sim-banner{border:1px solid rgba(167,139,250,.45);background:rgba(167,139,250,.12);border-radius:9px;
  padding:6px 10px;margin-bottom:8px;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:11px;color:#e9d5ff}
.sim-old{text-decoration:line-through;color:#fca5a5;font-weight:400;margin-right:4px}
.sim-new{color:#86efac}
.sim-link-create{color:#86efac;background:rgba(34,197,94,.08);padding:3px 7px;border-radius:6px;border:1px solid rgba(34,197,94,.28)}
.sim-link-delete{color:#fca5a5;text-decoration:line-through;background:rgba(239,68,68,.08);padding:3px 7px;border-radius:6px;border:1px solid rgba(239,68,68,.28)}
.obj h4{font-size:12px;font-weight:700;display:flex;justify-content:space-between;gap:8px}
.obj h4 span{font-family:"SF Mono",Menlo,monospace;font-size:10.5px;color:#a5e4ff;font-weight:500}
.kv{display:flex;gap:7px;font-size:10.8px;color:var(--dim);margin-top:3px;flex-wrap:wrap}
.kv b{color:#dbe7f5;font-weight:600;font-family:"SF Mono",Menlo,monospace;font-size:10.6px}
.kv .masked{color:var(--am)}
.kv .owned{color:#67e8f9}
.kv .ov{color:#fbbf24}
.ops{display:flex;gap:5px;margin-top:6px;flex-wrap:wrap}
.row{display:flex;gap:7px;align-items:center;margin-bottom:7px;flex-wrap:wrap}
label.f,div.f{display:flex;flex-direction:column;gap:3px;font-size:10px;color:var(--dim);flex:1;min-width:120px}
label.f input,label.f select,div.f input,div.f select{background:#0b1522;color:var(--txt);border:1px solid var(--line);
  border-radius:7px;padding:5px 8px;font-size:11px;font-family:"SF Mono",Menlo,monospace;outline:none}
input.auto-id{opacity:.78;cursor:default;color:#a5e4ff;border-style:dashed}
.msel{position:relative}
.msel-btn{width:100%;display:flex;justify-content:space-between;align-items:center;gap:6px;text-align:left;
  background:#0b1522;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:5px 8px;
  font-size:11px;font-family:"SF Mono",Menlo,monospace;cursor:pointer}
.msel-btn:hover{background:#0f1d30;border-color:rgba(56,189,248,.45)}
.msel-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msel-menu{display:none;position:fixed;z-index:40;max-height:220px;overflow:auto;
  background:#0b1522;border:1px solid rgba(56,189,248,.45);border-radius:8px;padding:4px;
  box-shadow:0 12px 28px rgba(0,0,0,.6)}
.msel.open .msel-menu{display:block}
.msel-opt{display:flex;align-items:flex-start;gap:7px;padding:5px 7px;border-radius:6px;cursor:pointer;
  font-size:11px;line-height:1.35;color:var(--txt);font-family:"SF Mono",Menlo,monospace}
.msel-opt:hover{background:rgba(56,189,248,.14)}
.msel-opt input[type=checkbox]{margin:2px 0 0;flex-shrink:0;width:13px;height:13px;accent-color:var(--cy)}
.msel-opt span{min-width:0;word-break:break-word}
select,input[type=text],textarea{background:#0b1522;color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:6px 9px;font-size:11px;font-family:"SF Mono",Menlo,monospace;outline:none}
pre{margin:0;font-family:"SF Mono",Menlo,monospace;font-size:10.6px;line-height:1.5;
  color:#9fb4cc;white-space:pre-wrap;word-break:break-all;max-height:240px;overflow:auto}
.run{border-left:3px solid var(--line);border-radius:8px;padding:6px 9px;margin-bottom:6px;
  background:rgba(255,255,255,.025);font-size:11px}
.run .id{font-family:"SF Mono",Menlo,monospace;color:#a5e4ff;font-size:10.5px}
.tag{font-size:9.5px;font-weight:700;padding:2px 6px;border-radius:5px;letter-spacing:.3px}
.t-COMMITTED{color:#bbf7d0;background:rgba(34,197,94,.2)}
.t-PENDING_APPROVAL{color:#fde68a;background:rgba(245,165,36,.2)}
.t-REJECTED{color:#fecaca;background:rgba(239,68,68,.2)}
.t-REVERTED{color:#ddd6fe;background:rgba(167,139,250,.2)}
.stat{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);padding:2px 0}
.stat b{color:#dbe7f5;font-family:"SF Mono",Menlo,monospace}
.hint{font-size:10.8px;color:var(--dim);line-height:1.6}
.err{color:#fca5a5}
.ok{color:#86efac}
.toast{position:fixed;right:18px;bottom:18px;max-width:520px;padding:10px 14px;border-radius:10px;
  font-size:12px;background:rgba(12,22,34,.97);border:1px solid var(--line);color:#dbe7f5;
  box-shadow:0 16px 40px rgba(0,0,0,.55);opacity:0;transform:translateY(8px);
  transition:all .18s ease;pointer-events:none;z-index:9}
.toast.show{opacity:1;transform:none}
.toast.bad{border-color:rgba(239,68,68,.55);color:#fecaca}
table{width:100%;border-collapse:collapse;font-size:10.8px}
th,td{text-align:left;padding:3px 6px;border-bottom:1px solid rgba(120,160,210,.1)}
th{color:#8ea3bd;font-weight:600;font-size:10px;letter-spacing:.4px}
td b{color:#dbe7f5;font-family:"SF Mono",Menlo,monospace;font-size:10.4px}
.sect{font-size:11.5px;font-weight:700;color:#cfe0f3;margin:9px 0 6px;letter-spacing:.3px}
.sect:first-child{margin-top:0}
.svgbox{background:rgba(4,9,16,.55);border:1px solid var(--line);border-radius:12px;padding:4px;overflow:auto}
.svgbox svg{display:block;width:100%;height:auto}
.en{font-family:"SF Mono",Menlo,monospace;font-size:9.5px;color:#6f839b;font-weight:500;letter-spacing:.2px}
.ph-link{display:flex;align-items:center;gap:8px;color:inherit;text-decoration:none;border-radius:6px;outline-offset:2px}
.ph-link:hover{color:#d6f2ff}
.ph-link small{font-weight:500;font-size:10px;color:var(--dim);letter-spacing:1px}
.ph-more{font-size:10.5px;font-weight:600;color:#a5e4ff;text-decoration:none;white-space:nowrap;
  border:1px solid rgba(56,189,248,.35);border-radius:6px;padding:3px 7px;background:rgba(56,189,248,.08)}
.ph-more:hover{background:rgba(56,189,248,.2)}
.topo-link{display:block;background:rgba(4,9,16,.55);border:1px solid var(--line);border-radius:12px;
  padding:4px;overflow:auto;cursor:zoom-in;text-decoration:none;outline-offset:3px}
.topo-link:hover{border-color:rgba(56,189,248,.45)}
.topo-link svg{display:block;width:100%;height:auto}
.fl{display:flex;align-items:baseline;gap:5px;font-size:10px;color:var(--dim)}
.fl em{font-style:normal;color:#6f839b;font-size:9.5px;font-family:"SF Mono",Menlo,monospace}
.diff{width:100%;border-collapse:collapse}
.diff td{vertical-align:top;font-family:"SF Mono",Menlo,monospace;font-size:10px}
.diff .b{color:#fca5a5}.diff .a{color:#86efac}
.agent-drawer{position:fixed;top:64px;right:12px;bottom:12px;width:420px;max-width:92vw;
  background:linear-gradient(170deg,rgba(14,24,40,.98),rgba(6,11,20,.98));
  border:1px solid rgba(56,189,248,.38);border-radius:14px;display:flex;flex-direction:column;
  box-shadow:0 20px 56px rgba(0,0,0,.68);z-index:20;overflow:hidden}
.agent-ctx{padding:6px 11px;font-size:10.5px;color:#8ea3bd;border-bottom:1px dashed var(--line);
  background:rgba(56,189,248,.05);display:flex;gap:8px;flex-wrap:wrap}
.agent-msgs{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:9px;min-height:0}
.amsg{border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:11.5px;line-height:1.55}
.amsg.user{align-self:flex-end;max-width:86%;background:rgba(56,189,248,.12);border-color:rgba(56,189,248,.35)}
.amsg.bot{align-self:stretch;background:rgba(255,255,255,.03)}
.art-card{margin-top:8px;border:1px solid rgba(56,189,248,.32);border-radius:10px;
  padding:8px 10px;background:rgba(8,15,26,.85)}
.art-card h5{font-size:11.5px;color:#a5e4ff;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.agent-foot{padding:9px 10px;border-top:1px solid var(--line);display:flex;gap:7px;background:rgba(8,14,24,.9)}
.agent-foot input{flex:1;background:#0b1522;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-size:11.5px;outline:none}
</style></head>
<body><div class="shell">
<header>
  <div class="mark">◈</div>
  <div class="brand"><b>__TITLE__</b><span>统一作战指挥中心 · 领域模型 × 世界状态</span></div>
  <div class="chips" id="chips"></div>
  <div class="role">角色
    <select id="role" onchange="loadAll()">
      <option value="admin">管理员</option>
      <option value="finance">财务人员</option>
      <option value="ops">业务操作员</option>
      <option value="auditor">审计员</option>
      <option value="dispatcher">调度员</option>
      <option value="factory_manager">研发负责人</option>
      <option value="system">系统</option>
      <option value="finance_manager">财务经理</option>
      <option value="ai_agent">智能助手</option>
      <option value="buyer">采购员</option>
      <option value="mdm_steward">主数据管理员</option>
    </select>
    <button class="v" onclick="runAutomations()">⚡ 运行自动化规则</button>
    <button class="c" id="btnAgent" onclick="toggleAgentDrawer()">🤖 智能助手</button>
    <a class="ph-more" id="btnDocs" href="/docs" target="_self" rel="noopener">文档</a>
  </div>
</header>
<nav class="pillarnav" id="pillarTabs">
  <button class="pillar-tab on" id="tab-ontology" onclick="switchPillar('ontology')">📐 Model · 领域模型</button>
  <button class="pillar-tab" id="tab-runtime" onclick="switchPillar('runtime')">⚙️ Runtime · 算力与治理宿体</button>
  <button class="pillar-tab" id="tab-instance" onclick="switchPillar('instance')">🌍 World · 世界状态</button>
  <button class="pillar-tab" id="tab-device" onclick="switchPillar('device')">🔌 Device · 外部工具与设备</button>
</nav>
<main>
  <aside class="side" id="side">
    <section class="panel" id="ontologiesPanel" style="flex:1">
      <div class="ph"><span><i>▍</i>📐 本体蓝图列表</span><small>ONTOLOGY BLUEPRINTS</small></div>
      <div class="list" id="ontologies"></div>
    </section>
    <section class="panel" id="instancesPanel" style="flex:1" hidden>
      <div class="ph"><span><i>▍</i>🌍 世界状态列表</span><small>WORLD STATES</small></div>
      <div class="list" id="instances"></div>
      <div style="padding:8px 10px;border-top:1px dashed var(--line)">
        <button class="v w" onclick="forkInstance()">＋ 克隆沙盘（Fork 当前世界状态）</button>
        <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:7px">
          <input type="checkbox" id="filterToggle" onchange="renderLists()"> 仅显示当前蓝图的世界状态
        </label>
      </div>
    </section>
  </aside>
  <section class="stage" id="stage"></section>
</main>
</div>
<aside class="agent-drawer" id="agentDrawer" hidden>
  <div class="ph">
    <span><i>▍</i>🤖 AIP 智能助手</span>
    <span style="display:flex;gap:6px;align-items:center">
      <button id="agentScopeBtn" class="v" onclick="toggleAgentScope()">🛠 平台管理员</button>
      <button onclick="clearAgentChat()">清空</button>
      <button onclick="toggleAgentDrawer()">✕</button>
    </span>
  </div>
  <div class="agent-ctx" id="agentCtx">等待选择世界状态…</div>
  <div class="agent-msgs" id="agentMessages">
    <div class="amsg bot">你好！我是 AIP 智能助手。可以让我查询对象列表、打开动作表单、查看待审批记录，或基于当前视图协同推演。</div>
  </div>
  <div class="agent-foot">
    <input type="text" id="agentInput" placeholder="输入指令，例如：查询对象 / 打开表单 / 待审批…" onkeydown="if(event.key==='Enter')sendAgentMessage()">
    <button class="g" onclick="sendAgentMessage()">发送</button>
  </div>
</aside>
<div class="toast" id="toast"></div>
<script>
__UI_BUNDLE__
__TOPOLOGY_JS__
const S = {catalog:null, ont:null, inst:null, meta:null, objects:{}, links:[], fp:null, health:null, healthOpen:false, type:null,
           ontoDetail:{}, mcts:null, result:null, simPreview:null, simSteps:[], simSessionId:null,
           simulations:[], simOnlyBugs:false, stateTab:'real', pillar:'ontology'};
const $ = (id) => document.getElementById(id);
const role = () => $('role').value;
function setRole(value) {
  if (!value) return;
  const sel = $('role');
  if (!Array.from(sel.options).some(o => o.value === value)) {
    const opt = document.createElement('option');
    opt.value = value; opt.textContent = zhRole(value);
    sel.appendChild(opt);
  }
  sel.value = value;
}

/* 把 #role 下拉选项收敛为当前 Ontology 的专属角色（文案一律中文）。 */
function syncRoleOptions(roles, preferredRole) {
  const sel = $('role');
  if (!sel || !roles || !roles.length) return;
  const current = sel.value;
  sel.innerHTML = roles.map(r =>
    '<option value="' + esc(r) + '">' + esc(zhRole(r)) + '</option>').join('');
  let wanted = null;
  if (preferredRole && roles.indexOf(preferredRole) >= 0) wanted = preferredRole;
  else if (roles.indexOf(current) >= 0) wanted = current;
  if (!wanted) {
    const first = (S.catalog && S.ont)
      ? S.catalog.instances.find(i => i.ontology_id === S.ont.ontology_id && i.default_actor_role)
      : null;
    const fallback = (first && first.default_actor_role) || roles[0];
    wanted = roles.indexOf(fallback) >= 0 ? fallback : roles[0];
  }
  setRole(wanted);
}

function toast(msg, bad) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 3600);
}

async function api(path, body) {
  let url = path + (path.includes('?') ? '&' : '?') + 'role=' + encodeURIComponent(role());
  if (S.inst && !path.includes('instance=') && !path.includes('workspace=')) {
    url += '&workspace=' + encodeURIComponent(S.inst.instance_id);
  }
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({workspace: S.inst && S.inst.instance_id}, body))
  };
  const res = await fetch(url, opt);
  const data = await res.json().catch(() => ({ok: false, error: 'invalid JSON'}));
  if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function chips() {
  if ($('chips')) $('chips').innerHTML = '';
}

/* ------------------------- 四标签页（四大原语） ------------------------- */
function setPillar(name) {
  S.pillar = name;
  document.querySelectorAll('.pillar-tab').forEach(el => el.classList.remove('on'));
  const tab = $('tab-' + name);
  if (tab) tab.classList.add('on');
  // 侧栏与主舞台严格隔离：Runtime / Device 独占整幅舞台，无侧栏。
  const full = (name === 'runtime' || name === 'device');
  const main = document.querySelector('main');
  if (main) main.classList.toggle('full', full);
  const side = $('side');
  if (side) side.hidden = full;
  const ontoPanel = $('ontologiesPanel');
  if (ontoPanel) ontoPanel.hidden = (name !== 'ontology');
  const instPanel = $('instancesPanel');
  if (instPanel) instPanel.hidden = (name !== 'instance');
}

async function switchPillar(name) {
  if (!S.catalog) return;
  setPillar(name);
  try {
    if (name === 'runtime') { renderRuntimeView(); return; }
    if (name === 'device') { renderDeviceView(); return; }
    if (name === 'ontology') {
      if (!S.ont && S.catalog.ontologies.length) {
        await selectOntology(S.catalog.ontologies[0].ontology_id);
        return;
      }
      if (!S.ont) { $('stage').innerHTML = '<div class="hint">暂无本体蓝图。</div>'; return; }
      if (!S.ontoDetail[S.ont.ontology_id]) {
        S.ontoDetail[S.ont.ontology_id] = await api('/api/ontology/' + encodeURIComponent(S.ont.ontology_id));
      }
      renderOntologyView(S.ontoDetail[S.ont.ontology_id]);
      return;
    }
    if (name === 'instance') {
      if (S.inst) { await loadInstanceData(); return; }
      const first = S.catalog.instances.find(i => !S.ont || i.ontology_id === S.ont.ontology_id) ||
        S.catalog.instances[0];
      if (first) await selectInstance(first.instance_id);
      else $('stage').innerHTML = '<div class="hint">暂无世界状态。</div>';
    }
  } catch (e) { toast('切换失败：' + e.message, true); }
}

function renderLists() {
  if (!S.catalog) return;
  const only = $('filterToggle').checked && S.ont;
  const instances = S.catalog.instances.filter(i => !only || i.ontology_id === S.ont.ontology_id);
  $('ontologies').innerHTML = S.catalog.ontologies.map(o =>
    '<div class="card' + (S.ont && S.ont.ontology_id === o.ontology_id ? ' on' : '') +
    '" onclick="selectOntology(\'' + o.ontology_id + '\')">' +
    '<h4>' + esc(zh('ontologies', o.ontology_id, o.name)) + '<em>' + esc(zh('categories', o.category, o.category)) + '</em></h4>' +
    '<div class="sub">' + esc(o.description || '') + '</div>' +
    '<div class="badges"><span class="badge c">' + o.counts.object_types + ' 对象类型</span>' +
    '<span class="badge">' + o.counts.object_properties + ' 对象属性</span>' +
    '<span class="badge a">' + o.counts.actions + ' 业务动作</span>' +
    '<span class="badge v">' + o.counts.instances + ' 世界状态</span></div></div>').join('');

  $('instances').innerHTML = instances.map(i => {
    const conns = (i.connectors || []).map(c =>
      '<span class="badge ' + (c.status === 'CONNECTED' ? 'g' : 'v') + '" title="' + esc(c.endpoint) + '">' +
      (c.status === 'CONNECTED' ? '🟢' : '🟣') + ' ' + esc(zhKind(c.kind)) + '：' + esc(c.name) +
      '（' + esc(zhStatus(c.status)) + '）</span>').join('');
    return '<div class="card' + (S.inst && S.inst.instance_id === i.instance_id ? ' on' : '') +
      '" onclick="selectInstance(\'' + i.instance_id + '\')">' +
      '<h4>' + esc(i.label) + '<em>' + esc(zh('ontologies', i.ontology_id, i.ontology_name)) + '</em></h4>' +
      '<div class="sub">数据源 ' + esc(i.db_uri) + '</div>' +
      '<div class="badges"><span class="badge">' + i.counts.objects + ' 对象</span>' +
      '<span class="badge">' + i.counts.links + ' 关系</span>' +
      '<span class="badge">' + i.counts.runs + ' 执行记录</span>' +
      (i.supports_mcts ? '<span class="badge c">多步推演</span>' : '') +
      (i.supports_unplug ? '<span class="badge a">可断开外部系统</span>' : '') + conns + '</div></div>';
  }).join('') || '<div class="hint">该蓝图暂无世界状态。</div>';
}

async function selectOntology(id) {
  S.ont = S.catalog.ontologies.find(o => o.ontology_id === id) || null;
  S.inst = null;
  S.meta = null;
  setPillar('ontology');
  // 默认展示全部实例；勾选「仅显示当前蓝图的世界状态」时才按 S.ont 过滤。
  renderLists();
  if (!S.ont) return;
  syncRoleOptions(S.ont.roles || []);
  if (!S.ontoDetail[id]) {
    S.ontoDetail[id] = await api('/api/ontology/' + encodeURIComponent(id));
  }
  renderOntologyView(S.ontoDetail[id]);
}

async function selectInstance(id) {
  const summary = S.catalog.instances.find(i => i.instance_id === id);
  if (!summary) return;
  S.inst = summary;
  S.ont = S.catalog.ontologies.find(o => o.ontology_id === summary.ontology_id) || null;
  S.meta = null;
  S.objects = {};
  S.links = [];
  S.fp = null;
  S.type = null;
  S.mcts = null;
  S.simPreview = null;
  S.simSteps = [];
  S.stateTab = 'real';
  setPillar('instance');
  syncRoleOptions((S.ont && S.ont.roles) || [], summary.default_actor_role);
  renderLists();
  await loadInstanceData();
}

async function loadAll() {
  try {
    const [cat, adp] = await Promise.all([
      api('/api/catalog'),
      api('/api/adapters').catch(() => ({ adapters: [] })),
    ]);
    S.catalog = cat;
    S.adapters = (adp && adp.adapters) || [];
    chips();
    if (S.inst) {
      const fresh = S.catalog.instances.find(i => i.instance_id === S.inst.instance_id);
      if (fresh) S.inst = fresh;
    }
    renderLists();
    setPillar(S.pillar);
    if (S.pillar === 'runtime') { renderRuntimeView(); return; }
    if (S.pillar === 'device') { renderDeviceView(); return; }
    if (S.pillar === 'instance') {
      if (S.inst) { await loadInstanceData(); return; }
      const first = S.catalog.instances.find(i => !S.ont || i.ontology_id === S.ont.ontology_id) ||
        S.catalog.instances[0];
      if (first) await selectInstance(first.instance_id);
      else $('stage').innerHTML = '<div class="hint">暂无世界状态。</div>';
      return;
    }
    if (S.ont) await selectOntology(S.ont.ontology_id);
    else if (S.catalog.ontologies.length) await selectOntology(S.catalog.ontologies[0].ontology_id);
    else $('stage').innerHTML = '<div class="hint">暂无本体蓝图。</div>';
  } catch (e) { toast('加载失败：' + e.message, true); }
}

/* ------------------------- 蓝图定义视图 ------------------------- */
function renderOntologyView(detail) {
  const o = detail.ontology;
  const m = detail;
  const first = S.catalog.instances.find(i => i.ontology_id === o.ontology_id);
  const svg = topology(m);
  const topoHref = '/topology?ontology=' + encodeURIComponent(o.ontology_id);
  const objects = m.object_types.map(t => {
    const rows = t.fields.map(f => {
      const tags = [];
      if (f.name === t.primary_key) tags.push('<span class="badge c">主键</span>');
      if (f.auto_generated) tags.push('<span class="badge">系统生成</span>');
      if (f.property_kind === 'object' && f.range) {
        tags.push('<span class="badge c">对象属性 → ' + esc(zhObject(f.range)) +
          (f.multiple ? '[]' : '') + '</span>');
      }
      if (f.owned) tags.push('<span class="badge a">本体自有</span>');
      if (f.masked) tags.push('<span class="badge r">🔒 仅可见：' + esc((f.masked_roles||[]).map(zhRole).join(' / ')) + '</span>');
      return '<tr><td><b>' + esc(zhField(t.name, f.name)) + '</b> <span class="en">' + esc(f.name) +
        '</span></td><td>' + esc(f.type) + '</td><td>' + tags.join(' ') + '</td></tr>';
    }).join('');
    return '<div class="sect">📦 ' + esc(zhObject(t.name)) + ' <span class="badge ' + (t.owned ? 'a' : '') + '">' +
      (t.owned ? '本体自有' : '来自外部数据源') + '</span> <span class="en">' + esc(t.name) + '</span></div>' +
      '<table><tr><th>字段</th><th>类型</th><th>属性语义 / 主键 / 主权 / 可见范围</th></tr>' + rows + '</table>';
  }).join('');

  const links = (m.object_properties || []).map(l =>
    '<div class="kv" style="margin:5px 0"><b>' + esc(zh('object_properties', l.name, '') || zh('links', l.name, l.name)) + '</b><span>' +
    esc(zhObject(l.domain)) + ' → ' + esc(zhObject(l.range)) + '</span><span class="badge">' +
    esc(zhCardinality(l.cardinality)) + '</span><span class="badge ' + (l.owned ? 'a' : '') + '">' +
    (l.owned ? '本体自有' : '来自外部数据源') + '</span>' +
    (l.field ? '<span class="badge c">字段：' + esc(l.field) + '</span>' : '') +
    '<span class="en">' + esc(l.name) + '</span></div>').join('') ||
    '<div class="hint">无。</div>';

  const funcs = m.functions.map(f => '<div class="kv" style="margin:5px 0"><b>' + esc(zh('functions', f.name, f.name)) +
    '</b><span>' + esc(f.description) + '</span><span class="en">' + esc(f.name) + '</span></div>').join('') ||
    '<div class="hint">无。</div>';
  const autos = m.automations.map(a => '<div class="kv" style="margin:5px 0"><b>' + esc(zh('automations', a.name, a.name)) +
    '</b><span>监听对象：' + esc(zhObject(a.watch_object_type)) + '</span><span>' + esc(a.description) + '</span>' +
    '<span class="en">' + esc(a.name) + '</span></div>').join('') || '<div class="hint">无。</div>';

  const actions = m.actions.map(a => {
    const props = (a.params && a.params.properties) || {};
    const fields = Object.keys(props).map(k => '<span class="badge">' + esc(zhField(null, k)) + '：' +
      esc((props[k]||{}).type || 'any') + '</span>').join('');
    return '<div class="obj"><h4>' + esc(zhAction(a.name)) + ' <span class="en">' + esc(a.name) + '</span></h4>' +
      '<div class="sub hint">' + esc(a.description) + '</div>' +
      '<div class="badges">' + fields + '</div>' +
      '<div class="badges"><span class="badge c">可执行角色 ' + esc((a.allowed_roles||[]).map(zhRole).join(' / ')) + '</span>' +
      (a.visibility === 'private' ? '<span class="badge a">🔒 私有 · 仅内部触发</span>' : '') +
      '<span class="badge a">审批角色 ' + esc((a.approver_roles||[]).map(zhRole).join(' / ') || '无需审批') + '</span>' +
      '<span class="badge">' + (a.affects_source ? '来自外部数据源' : '本体自有') + '</span></div></div>';
  }).join('') || '<div class="hint">无。</div>';

  const roleCards = (m.role_defs || []).map(r => {
    const allowed = (r.allowed_actions || []).map(a => '<span class="badge c">' + esc(zhAction(a)) + '</span>').join(' ');
    const approvers = (r.approver_actions || []).map(a => '<span class="badge a">审批：' + esc(zhAction(a)) + '</span>').join(' ');
    const masked = (r.masked_fields || []).map(f => '<span class="badge r">🔒 ' + esc(f) + '</span>').join(' ');
    return '<div class="obj"><h4>🎭 ' + esc(zhRole(r.name)) + ' <span class="en">' + esc(r.name) + '</span>' +
      (r.declared ? ' <span class="badge v">显式 RoleDef</span>' : ' <span class="badge">动作推断</span>') + '</h4>' +
      (r.description ? '<div class="sub hint">' + esc(r.description) + '</div>' : '') +
      '<div class="badges">' + (allowed || '<span class="hint">无可执行动作</span>') +
      (approvers ? ' ' + approvers : '') + (masked ? ' ' + masked : '') + '</div></div>';
  }).join('') || '<div class="hint">无。</div>';

  $('stage').innerHTML =
    '<div class="hero"><h2>📐 ' + esc(zh('ontologies', o.ontology_id, o.name)) +
    ' <span class="badge c">Model · 领域模型</span> <span class="badge">' +
    esc(zh('categories', o.category, o.category)) + '</span></h2>' +
    '<p>' + esc(o.description) + '</p><div class="acts">' +
    '<span class="badge v">TBox · 结构声明</span>' +
    '<span class="badge a">Behavior · 受控行为</span>' +
    (first ? '<button class="g" onclick="selectInstance(\'' + first.instance_id + '\')">🚀 进入该模型的首个世界状态</button>' : '') +
    '<span class="badge">' + m.counts.object_types + ' 对象类型</span>' +
    '<span class="badge">' + m.counts.interfaces + ' 共享接口</span>' +
    '<span class="badge">' + m.counts.object_properties + ' 对象属性</span>' +
    '<span class="badge">' + m.counts.functions + ' 查询与计算</span>' +
    '<span class="badge">' + m.counts.actions + ' 业务动作</span>' +
    '<span class="badge">' + m.counts.automations + ' 自动化规则</span>' +
    '<span class="badge">' + (m.counts.roles || (m.role_defs || []).length) + ' 角色</span></div></div>' +
    '<div class="grid2"><section class="panel"><div class="ph">' +
    '<a class="ph-link" href="' + topoHref + '" target="_blank" rel="noopener" tabindex="0" ' +
    'title="在新标签页打开可缩放的本体关系图"><i>▍</i>本体关系图<small>TBOX · TOPOLOGY</small></a>' +
    '<a class="ph-more" href="' + topoHref + '" target="_blank" rel="noopener" tabindex="0">查看大图 ↗</a></div>' +
    '<div class="body"><a class="topo-link" href="' + topoHref + '" target="_blank" rel="noopener" tabindex="0" ' +
    'title="在新标签页打开可缩放的本体关系图">' + svg + '</a></div></section>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>📦 对象类型与共享接口</span><small>TBOX · SCHEMA</small></div>' +
    '<div class="body"><div class="sect">TBox · 结构声明</div>' + objects + '</div></section></div>' +
    '<div class="grid2b"><section class="panel flat"><div class="ph"><span><i>▍</i>🔗 对象属性关系</span><small>TBOX · OBJECT PROPERTIES</small></div>' +
    '<div class="body">' + links + '</div></section>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>🧮 查询与计算 / ⚡ 自动化规则</span><small>BEHAVIOR · FUNCTIONS &amp; AUTOMATIONS</small></div>' +
    '<div class="body"><div class="sect">Behavior · 查询与计算</div>' + funcs + '<div class="sect">Behavior · 自动化规则</div>' + autos + '</div></section></div>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>🎭 模型角色与授权</span><small>BEHAVIOR · ROLES &amp; AUTHORIZATION</small></div>' +
    '<div class="body">' + roleCards + '</div></section>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>🎯 业务动作</span><small>BEHAVIOR · GOVERNED ACTIONS</small></div>' +
    '<div class="body"><div class="sect">Behavior · 受控行为</div>' + actions + '</div></section>';
}

function topology(m) {
  if (!(m.object_types || []).length) return '<div class="hint">该本体暂无对象类型。</div>';
  return topologySvg(m);
}

/* ------------------------- 实例作战室视图 ------------------------- */
async function loadInstanceData() {
  if (!S.inst) return;
  try {
    const meta = await api('/api/meta');
    S.meta = meta;
    if (!S.type || !meta.object_types.some(t => t.name === S.type)) {
      S.type = meta.object_types[0] && meta.object_types[0].name;
    }
    S.objects = {};
    for (const t of meta.object_types) {
      const res = await api('/api/objects/' + encodeURIComponent(t.name));
      S.objects[t.name] = res.objects;
    }
    const fp = await api('/api/fingerprint');
    const runs = (await api('/api/action-runs')).runs;
    const links = (await api('/api/links')).links;
    const simsRes = await api('/api/simulations').catch(() => ({simulations: []}));
    const health = await api('/api/world-health').catch(() => null);
    S.fp = fp;
    S.links = links || [];
    S.simulations = simsRes.simulations || [];
    S.health = health;
    S.healthOpen = false;
    S.simPreview = null;
    S.simSteps = [];
    S.simSessionId = null;
    renderInstanceView(meta, fp, runs, links);
  } catch (e) { toast('世界状态加载失败：' + e.message, true); }
}

function connectorChips(conns) {
  return (conns || []).map(c => '<span class="badge ' + (c.status === 'CONNECTED' ? 'g' : 'v') + '" title="' +
    esc(c.endpoint) + '">' + (c.status === 'CONNECTED' ? '🟢' : '🟣') + ' ' + esc(zhKind(c.kind)) + '：' + esc(c.name) +
    '（' + esc(zhStatus(c.status)) + '）</span>').join('') || '<span class="badge">无外部连接器</span>';
}

function renderStateTabs() {
  const box = $('stateTabs');
  if (!box) return;
  const objChanges = mergeObjectChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.object_changes) || []);
  const linkChanges = mergeLinkChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.link_changes) || []);
  const total = objChanges.length + linkChanges.length;
  const simBadge = S.simPreview
    ? ' <span class="badge v" style="padding:0 5px;font-size:9px">' + (total ? total + ' 项变更' : '无变更') + '</span>'
    : '';
  box.innerHTML =
    '<span class="state-tab' + (S.stateTab === 'real' ? ' on' : '') + '" data-state-tab="real" onclick="pickStateTab(\'real\')">🌍 当前业务状态</span>' +
    '<span class="state-tab' + (S.stateTab === 'sim' ? ' on' : '') + '" data-state-tab="sim" onclick="pickStateTab(\'sim\')">🧪 沙盘推演状态' + simBadge + '</span>';
}

function pickStateTab(tab) {
  S.stateTab = (tab === 'sim') ? 'sim' : 'real';
  renderStateTabs();
  renderObjectTabs();
  renderObjects();
  renderLinks(S.links);
  refreshParamOptions();
}

function renderObjectTabs() {
  const box = $('objectTabs');
  if (!box || !S.meta) return;
  const changes = mergeObjectChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.object_changes) || []);
  const countByType = {};
  changes.forEach(c => { countByType[c.object_type] = (countByType[c.object_type] || 0) + 1; });
  const objectTabs = S.meta.object_types.map(t => {
    const cnt = countByType[t.name] || 0;
    const simBadge = (S.stateTab === 'sim' && cnt) ? ' <span class="badge v" style="padding:0 5px;font-size:9px">沙盘 ' + cnt + '</span>' : '';
    return '<span class="tab' + (t.name === S.type ? ' on' : '') + '" data-name="' + esc(t.name) +
      '" onclick="pickType(\'' + t.name + '\')">' +
      esc(zhObject(t.name)) + ' <b style="color:#7f93ab">' + ((S.objects[t.name] || []).length) + '</b>' + simBadge + '</span>';
  }).join('');
  const ifaceTabs = S.meta.interfaces.map(f =>
    '<span class="tab" data-name="' + esc(f.name) + '" onclick="pickInterface(\'' + f.name + '\')">◇ ' +
    esc(zh('interfaces', f.name, f.name)) + '</span>').join('');
  box.innerHTML = objectTabs + ifaceTabs;
}

function renderInstanceView(meta, fp, runs, links) {
  const i = S.inst;

  $('stage').innerHTML =
    '<div class="hero"><h2>🌍 ' + esc(i.label) + ' <span class="badge c">World · 世界状态</span>' +
    ' <span class="badge">' + esc(zh('ontologies', i.ontology_id, i.ontology_name)) + '</span></h2>' +
    '<p>' + esc(i.description || '') + ' · <b style="color:#a5e4ff">数据源 ' + esc(i.db_uri) + '</b></p>' +
    '<div class="acts"><span class="badge v" id="formula" title="World = Model + Runtime">🧬 认知降维：' +
    esc(i.formula || '') + '</span><span class="badge">ABox · 事实状态</span>' +
    '<a class="badge c" style="text-decoration:none" href="/api/worlds/' + encodeURIComponent(i.instance_id) + '/openapi.json" target="_blank" rel="noopener">📡 OpenAPI 3.1</a>' +
    connectorChips(i.connectors) + '</div></div>' +
    '<section class="panel"><div class="mount"><b>世界状态环境与后台挂载栏</b>' +
    '<span class="badge">' + esc(i.instance_id) + '</span>' +
    '<span id="worldHealthBadge"></span><span class="sp"></span>' +
    '<small style="color:#8ea3bd;font-size:9.5px;letter-spacing:1px">当前状态指纹</small>' +
    '<div class="fp" id="fp"></div>' +
    '<button class="v" onclick="runAutomations()">🔄 触发自动化规则</button>' +
    '<button class="v" onclick="forkInstance()">🍴 克隆沙盘</button>' +
    (i.supports_unplug ? '<button class="r" onclick="unplug()">🔌 断开外部系统</button>' : '') +
    '</div>' +
    '<div id="worldHealthPanel" hidden style="padding:9px 12px;border-top:1px dashed var(--line);background:rgba(8,15,26,.88);font-size:11px"></div>' +
    '</section>' +
    '<div class="grid2"><section class="panel"><div class="ph"><span><i>▍</i>🌍 世界状态 · ABox 事实</span><small>ABOX · WORLD STATE</small></div>' +
    '<div class="state-tabs" id="stateTabs" style="background:rgba(167,139,250,.06);border-bottom:1px solid var(--line)"></div>' +
    '<div class="tabs" id="objectTabs"></div><div class="body" id="objects"></div></section>' +
    '<section class="panel"><div class="ph"><span><i>▍</i>🎯 业务动作与沙盘推演台</span><small>GOVERNED ACTION</small></div>' +
    '<div class="body"><div class="row"><select id="action" onchange="fillParams()"></select>' +
    '<span class="badge" id="actmeta"></span></div><div id="form" class="row"></div>' +
    '<div class="row"><button id="simBtn" onclick="simulate()">🔍 沙盘预演</button>' +
    (i.supports_mcts ? '<button class="v" onclick="runMcts()">🌳 多步智能推演</button>' : '') +
    '</div>' +
    '<pre id="result" class="hint">沙盘预演或正式执行后，这里显示对象变更 / 错误码。</pre>' +
    '<div id="mcts" class="hint" style="margin-top:8px"></div>' +
    '<div class="hint" style="margin-top:8px">⚡ 自动化规则按（规则、对象、版本）去重执行。</div>' +
    '</div></section></div>' +
    '<div class="grid2b"><section class="panel"><div class="ph"><span><i>▍</i>🔗 语义关系与 🧮 查询计算</span><small>SEMANTIC EDGES</small></div>' +
    '<div class="body" id="links"></div></section>' +
    '<section class="panel"><div class="ph"><span><i>▍</i>📋 动作执行记录</span><small>ACTION RUNS</small></div>' +
    '<div class="body" id="runs"></div></section></div>' +
    '<section class="panel"><div class="ph"><span><i>▍</i>🧪 沙盘推演日志（本体 Bug 诊断库）</span><small>SIMULATION LEDGER · DB PERSISTED</small></div>' +
    '<div class="body" id="simulations"></div></section>';

  const sel = $('action');
  const publicActions = (meta.actions || []).filter(a => a.visibility !== 'private');
  sel.innerHTML = publicActions.map(a => '<option value="' + a.name + '">' + esc(zhAction(a.name)) + '</option>').join('');
  if (S.lastAction && publicActions.some(a => a.name === S.lastAction)) sel.value = S.lastAction;
  renderStateTabs();
  renderObjectTabs();
  renderFingerprint(fp);
  renderWorldHealth();
  renderObjects();
  renderLinks(links);
  renderRuns(runs);
  renderSimulations();
  fillParams();
}

/* ------------------------- Runtime 宿体视图 ------------------------- */
function renderRuntimeView() {
  const list = (S.catalog && S.catalog.runtimes) || [];
  const cards = list.map(r => {
    const aspects = (r.aspects || []).map(a =>
      '<span class="badge v">' + esc(a) + '</span>').join('') ||
      '<span class="badge">未挂载切面</span>';
    const adapters = (r.adapters || []).map(a =>
      '<span class="badge c">' + esc(a) + '</span>').join('') ||
      '<span class="badge">零外设驱动</span>';
    const onto = (r.ontology_ids || []).map(o =>
      '<span class="badge a">' + esc(zh('ontologies', o, o)) + '</span>').join('') ||
      '<span class="badge">—</span>';
    const inst = (r.instance_ids || []).map(i =>
      '<button onclick="selectInstance(\'' + esc(i) + '\')">' + esc(i) + '</button>').join('') ||
      '<span class="badge">—</span>';
    return '<section class="panel flat"><div class="ph"><span><i>▍</i>⚙️ ' + esc(r.label) +
      '</span><small>' + esc(r.runtime_id) + '</small></div><div class="body">' +
      '<div class="kv"><span>Core</span><b>' + esc(r.storage_kind) + '</b>' +
      '<b>' + esc(r.db_uri) + '</b></div>' +
      '<div class="hint" style="margin:5px 0 8px">' + esc(r.description || '') + '</div>' +
      '<div class="sect">Aspect · 治理切面</div><div class="badges">' + aspects + '</div>' +
      '<div class="sect">DeviceAdapter · 设备驱动</div><div class="badges">' + adapters + '</div>' +
      '<div class="sect">关联 Ontology</div><div class="badges">' + onto + '</div>' +
      '<div class="sect">关联 Instance</div><div class="badges">' + inst + '</div>' +
      '</div></section>';
  }).join('') || '<div class="hint">暂无 Runtime 宿体。</div>';

  const adaptersList = S.adapters || [];
  const adapterCards = adaptersList.map(a => {
    const running = a.status === 'RUNNING';
    const nextStatus = running ? 'PAUSED' : 'RUNNING';
    const mappingEntries = Object.entries(a.field_mapping || {});
    const mappingHtml = mappingEntries.length
      ? mappingEntries.map(([k, v]) => '<span class="badge c">' + esc(k) + ' → ' + esc(v) + '</span>').join('')
      : '<span class="badge">同名透传</span>';
    return '<section class="card"><div class="card-h"><h3>' + esc(a.id) + '</h3>' +
      '<span class="badge ' + (running ? 'g' : 'a') + '">' + esc(a.status) + '</span></div>' +
      '<div class="card-b">' +
      '<div class="kv"><span>源设备 <b>' + esc(a.device_id) + '</b></span>' +
      '<span>目标世界 <b>' + esc(a.world_id) + '</b></span>' +
      '<span>对象类型 <b>' + esc(a.object_type) + '</b></span>' +
      '<span>接口路径 <b>' + esc(a.data_path) + '</b></span>' +
      '<span>轮询周期 <b>' + (a.interval_sec > 0 ? esc(a.interval_sec + 's') : '仅手动') + '</b></span>' +
      '<span>累计同步 <b>' + esc(String(a.sync_count || 0)) + ' 次</b></span>' +
      '<span>最近同步 <b>' + esc(a.last_sync_at || '尚未同步') + '</b></span></div>' +
      (a.sync_error ? '<div style="margin-top:8px;color:var(--red);font-size:12px">最近错误：' + esc(a.sync_error) + '</div>' : '') +
      '<div class="sect">Webhook 接收地址</div><div style="font-size:11px;color:var(--muted);word-break:break-all"><code>' + esc(location.origin + '/api/adapter/webhook?adapter_id=' + encodeURIComponent(a.id)) + '</code></div>' +
      '<div class="sect">字段映射 (field_mapping)</div><div class="badges">' + mappingHtml + '</div>' +
      '<div style="display:flex;gap:8px;margin-top:12px">' +
      '<button class="btn primary" onclick="syncAdapterOnce(\'' + esc(a.id) + '\')">▶ 立即同步</button>' +
      '<button class="btn" onclick="toggleAdapterState(\'' + esc(a.id) + '\',\'' + nextStatus + '\')">' + (running ? '⏸ 暂停' : '▶ 恢复') + '</button>' +
      '</div></div></section>';
  }).join('');

  $('stage').innerHTML = '<div class="hero"><h2>⚙️ Runtime · 算力与治理宿体</h2>' +
    '<p>Runtime = Core 微内核 + Aspect 系统调用切面 + DeviceAdapter 设备驱动。' +
    '同一套微内核在不同宿体上执行领域模型定义，隔离承载各世界状态的 ABox 事实与审计历史。</p>' +
    '<div class="acts"><span class="badge c">' + list.length + ' 台宿体</span>' +
    '<span class="badge v">' + list.reduce((n, r) => n + (r.aspects || []).length, 0) +
    ' 个切面挂载</span><span class="badge a">' +
    adaptersList.length + ' 个活动适配器</span></div></div>' +
    '<div class="grid2">' + cards + '</div>' +
    '<div class="hero" style="margin-top:18px"><h2>🔌 DeviceAdapter · 轻量级设备适配器</h2>' +
    '<p>替代重型 ESB 的轻量级拉取-映射-写入管道，支持异构字段映射（field_mapping）与周期轮询。</p></div>' +
    (adapterCards ? '<div class="grid2">' + adapterCards + '</div>' : '<div class="hint">暂无已注册设备适配器，可让平台管理员智能体创建（create_adapter）。</div>');
}

async function syncAdapterOnce(adapterId) {
  try {
    const res = await api('/api/adapter/sync', { method: 'POST', body: JSON.stringify({ adapter_id: adapterId }) });
    toast('适配器 ' + adapterId + ' 已同步 ' + res.row_count + ' 条记录');
    await loadAll();
  } catch (e) { toast('同步失败：' + e.message, true); }
}

async function toggleAdapterState(adapterId, status) {
  try {
    await api('/api/adapter/toggle', { method: 'POST', body: JSON.stringify({ adapter_id: adapterId, status }) });
    toast('适配器 ' + adapterId + ' 状态已切换为 ' + status);
    await loadAll();
  } catch (e) { toast('切换失败：' + e.message, true); }
}

/* ------------------------- Device 外部世界视图 ------------------------- */
function deviceCard(d, schema) {
  return '<div class="obj"><h4>' + esc(d.name) + ' <span>' + esc(d.kind) + '</span></h4>' +
    '<div class="kv"><span>设备编号 <b>' + esc(d.device_id) + '</b></span>' +
    '<span>端点 <b>' + esc(d.endpoint) + '</b></span>' +
    '<span class="badge ' + (d.status === 'CONNECTED' ? 'g' : 'v') + '">' +
    esc(zhStatus(d.status)) + '</span></div>' +
    '<pre style="margin-top:6px">' + esc(JSON.stringify(schema || {}, null, 2)) + '</pre></div>';
}

function renderDeviceView() {
  const devices = (S.catalog && S.catalog.devices) || {};
  const symbolic = (devices.symbolic || []).map(d => deviceCard(d, d.openapi_schema));
  $('stage').innerHTML = '<div class="hero"><h2>🔌 Device · 外部工具与设备</h2>' +
    '<p>外部工具与数据源通过 OpenAPI 契约登记，World 通过 Runtime 的设备驱动将其同步为受约束的对象图；' +
    '外部 Agent 则直接持有 Actor 身份通过 OSDK / ActionDef 与世界模型交互。</p>' +
    '<div class="acts"><span class="badge c">OpenAPI 设备 ' + symbolic.length + ' 台</span></div></div>' +
    '<section class="panel flat"><div class="ph"><span><i>▍</i>🔗 外部工具与设备 · OpenAPI Schema</span>' +
    '<small>EXTERNAL DEVICES</small></div><div class="body" id="symbolicDevices">' +
    (symbolic.join('') || '<div class="hint">暂无外部设备。</div>') + '</div></section>';
}

function renderFingerprint(fp) {
  const box = $('fp');
  if (!box || !fp) return;
  const objChanges = mergeObjectChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.object_changes) || []);
  const linkChanges = mergeLinkChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.link_changes) || []);
  let objDelta = 0, objUpdates = 0, linkDelta = 0, linkCreates = 0;
  objChanges.forEach(c => {
    if (c.kind === 'create') objDelta += 1;
    else if (c.kind === 'delete') objDelta -= 1;
    else objUpdates += 1;
  });
  linkChanges.forEach(c => {
    if (c.kind === 'create') { linkDelta += 1; linkCreates += 1; }
    else if (c.kind === 'delete') linkDelta -= 1;
  });
  const objParts = [];
  if (objDelta !== 0) objParts.push(objDelta > 0 ? '+' + objDelta : String(objDelta));
  if (objUpdates > 0) objParts.push('~' + objUpdates);
  const objHint = objParts.length
    ? ' <small style="color:#c4b5fd">(沙盘 ' + objParts.join(', ') + ')</small>'
    : '';
  const linkHint = linkDelta !== 0
    ? ' <small style="color:#c4b5fd">(沙盘 ' + (linkDelta > 0 ? '+' + linkDelta : linkDelta) + ')</small>'
    : (linkChanges.length ? ' <small style="color:#c4b5fd">(沙盘 ~' + linkCreates + ')</small>' : '');
  box.innerHTML = '<span>对象 <b style="color:#dbe7f5">' + fp.objects + '</b>' + objHint + '</span>' +
    '<span>关系 <b style="color:#dbe7f5">' + fp.links + '</b>' + linkHint + '</span>' +
    '<span>叠加层 <b style="color:#dbe7f5">' + fp.overlay + '</b></span>' +
    '<span>执行记录 <b style="color:#dbe7f5">' + fp.runs + '</b></span>';
}

function renderWorldHealth() {
  const badgeBox = $('worldHealthBadge');
  const panel = $('worldHealthPanel');
  const h = S.health;
  if (!badgeBox || !panel) return;
  if (!h) {
    badgeBox.innerHTML = '';
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }

  const violations = h.violations || [];
  const invariants = h.invariants || [];
  if (h.healthy) {
    const label = h.invariants_total > 0
      ? ('🟢 自洽 (' + h.invariants_passed + '/' + h.invariants_total + ' 不变量)')
      : '🟢 自洽 (Schema·引用完整)';
    badgeBox.innerHTML =
      '<span class="badge g" id="healthChip" role="button" tabindex="0" style="cursor:pointer" ' +
      'onclick="toggleWorldHealth()" onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();toggleWorldHealth();}" ' +
      'title="点击查看世界状态自洽详情">' + esc(label) + '</span>';
  } else {
    badgeBox.innerHTML =
      '<span class="badge r" id="healthChip" role="button" tabindex="0" style="cursor:pointer;font-weight:700" ' +
      'onclick="toggleWorldHealth()" onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();toggleWorldHealth();}" ' +
      'title="点击展开状态违背诊断明细">' +
      esc('🔴 ' + violations.length + ' 项状态违背 ' + (S.healthOpen ? '▴' : '▾')) + '</span>';
  }

  panel.hidden = !S.healthOpen;
  if (!S.healthOpen) return;

  const kindLabel = (k) => k === 'schema' ? 'Schema 漂移' : (k === 'reference' ? '悬空引用' : '不变量违背');
  const summaryHtml =
    '<div class="row" style="justify-content:space-between;margin-bottom:6px">' +
    '<div class="kv"><span>已校验对象 <b>' + esc(String(h.objects_checked || 0)) + '</b></span>' +
    '<span>语义关系 <b>' + esc(String(h.links_checked || 0)) + '</b></span>' +
    '<span>不变量通过 <b>' + esc(String(h.invariants_passed || 0) + '/' + String(h.invariants_total || 0)) + '</b></span></div>' +
    '<button id="recheckHealthBtn" onclick="recheckWorldHealth()">↻ 重新校验</button></div>';

  const violationsHtml = violations.length
    ? ('<div class="sect" style="color:#fecaca">违背明细（共 ' + violations.length + ' 项）</div>' +
       violations.map(v =>
         '<div class="obj" style="border-left-color:var(--rd);background:rgba(239,68,68,.07);margin-bottom:5px">' +
         '<h4><span>' + esc(kindLabel(v.kind) + ' · ' + (v.subject || '')) + '</span>' +
         '<span class="badge r">' + esc(v.code || '') + '</span></h4>' +
         '<div class="hint" style="margin-top:3px;color:#fecaca">' + esc(v.message || '') + '</div></div>'
       ).join(''))
    : '';

  const invariantsHtml = invariants.length
    ? ('<div class="sect">全局状态不变量 (@spec.invariant)</div>' +
       invariants.map(inv =>
         '<div class="kv" style="margin:4px 0;align-items:center">' +
         '<span class="badge ' + (inv.passed ? 'g' : 'r') + '">' + (inv.passed ? '🟢 通过' : '🔴 违背') + '</span>' +
         '<b>' + esc(inv.name) + '</b>' +
         '<span class="badge">' + esc(inv.code || '') + '</span>' +
         '<span class="hint">' + esc(inv.error || inv.description || '') + '</span></div>'
       ).join(''))
    : (h.healthy
        ? '<div class="hint">所有存量对象均满足当前 TBox Schema 与引用完整性约束（该模型未声明额外 @spec.invariant）。</div>'
        : '');

  panel.innerHTML = summaryHtml + violationsHtml + invariantsHtml;
}

function toggleWorldHealth() {
  S.healthOpen = !S.healthOpen;
  renderWorldHealth();
}

async function recheckWorldHealth() {
  if (!S.inst) return;
  try {
    S.health = await api('/api/world-health');
    S.healthOpen = true;
    renderWorldHealth();
    const n = (S.health.violations || []).length;
    toast(
      S.health.healthy ? '状态自洽巡检通过' : ('发现 ' + n + ' 项状态违背'),
      !S.health.healthy
    );
  } catch (e) { toast('巡检失败：' + e.message, true); }
}

function clearSimPreview() {
  S.simPreview = null;
  S.simSteps = [];
  S.simSessionId = null;
  S.stateTab = 'real';
  renderStateTabs();
  renderFingerprint(S.fp);
  renderObjectTabs();
  renderObjects();
  renderLinks(S.links);
  refreshParamOptions();
}

function pickType(name) {
  S.type = name;
  document.querySelectorAll('#objectTabs .tab').forEach(el => el.classList.remove('on'));
  renderObjects();
  const target = document.querySelector('#objectTabs .tab[data-name="' + name + '"]');
  if (target) target.classList.add('on');
  fillParams();
}

async function pickInterface(name) {
  try {
    const data = await api('/api/interface?interface=' + encodeURIComponent(name));
    const title = zh('interfaces', name, name);
    const decl = ((S.meta && S.meta.interfaces) || []).find(i => i.name === name);
    const types = ((S.meta && S.meta.object_types) || []).filter(t =>
      !decl || (decl.implementations || []).indexOf(t.name) >= 0);
    const fieldMeta = {};
    types.forEach(t => (t.fields || []).forEach(f => {
      if (!fieldMeta[f.name]) fieldMeta[f.name] = f;
    }));
    const rows = (data.objects || []).map(o => {
      const keys = Object.keys(o);
      const pk = keys.length ? o[keys[0]] : '';
      return '<div class="obj"><h4>◇ ' + esc(title) + '<span>' + esc(pk) + '</span></h4><div class="kv">' +
        keys.map(k => {
          const meta = fieldMeta[k];
          const isRoleMasked = !!(meta && meta.masked) && role() !== 'system' &&
            !((meta.masked_roles || []).indexOf(role()) >= 0);
          const v = o[k];
          const masked = isRoleMasked || v === '***MASKED***' || v === 'REDACTED';
          const text = masked ? '不可见'
            : ((v === null || v === undefined) ? '—'
              : (typeof v === 'object' ? JSON.stringify(v) : String(v)));
          return '<span title="' + esc(k) + '">' + esc(zhField(null, k)) +
            ' <b class="' + (masked ? 'masked' : '') + '">' + esc(text) + '</b></span>';
        }).join('') + '</div></div>';
    }).join('');
    $('objects').innerHTML = '<div class="hint" style="margin-bottom:7px">Interface 多态视图：' + esc(title) + '</div>' +
      (rows || '<div class="hint">该接口暂无对象。</div>');
    document.querySelectorAll('#objectTabs .tab').forEach(el => el.classList.remove('on'));
    const target = document.querySelector('#objectTabs .tab[data-name="' + name + '"]');
    if (target) target.classList.add('on');
  } catch (e) { toast(e.message, true); }
}

function formatFieldVal(f, v) {
  const isRoleMasked = f.masked && role() !== 'system' &&
    !((f.masked_roles || []).indexOf(role()) >= 0);
  const masked = isRoleMasked || v === '***MASKED***' || v === 'REDACTED';
  const cls = masked ? 'masked' : (f.owned ? 'owned' : '');
  let text = (v === null || v === undefined) ? '—'
    : (typeof v === 'object' ? JSON.stringify(v) : String(v));
  if (masked) text = '不可见';
  else text = zhValue(f.name, text);
  return { masked, cls, text };
}

function canonicalJson(v) {
  if (v === null || v === undefined || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonicalJson).join(',') + ']';
  const keys = Object.keys(v).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + canonicalJson(v[k])).join(',') + '}';
}

function sameJson(a, b) {
  return canonicalJson(a) === canonicalJson(b);
}

function mergeObjectChanges(changes) {
  const map = new Map();
  (changes || []).forEach(c => {
    const key = c.object_type + '|' + c.object_id;
    const prev = map.get(key);
    if (!prev) {
      map.set(key, Object.assign({}, c));
      return;
    }
    if (prev.kind === 'create' && c.kind === 'update') {
      prev.after = Object.assign({}, prev.after || {}, c.after || {});
    } else if (prev.kind === 'create' && c.kind === 'delete') {
      map.delete(key);
    } else if (prev.kind === 'update' && c.kind === 'update') {
      prev.after = Object.assign({}, prev.after || {}, c.after || {});
      if (prev.before && sameJson(prev.before, prev.after)) map.delete(key);
    } else if (prev.kind === 'update' && c.kind === 'delete') {
      prev.kind = 'delete';
      prev.after = null;
    } else if (prev.kind === 'delete' && c.kind === 'create') {
      const nextAfter = Object.assign({}, prev.before || {}, c.after || {});
      if (prev.before && sameJson(prev.before, nextAfter)) {
        map.delete(key);
      } else {
        prev.kind = 'update';
        prev.after = nextAfter;
      }
    } else {
      map.set(key, Object.assign({}, c));
    }
  });
  return Array.from(map.values());
}

function linkChangeKey(l) {
  return [l.link_type, l.source_type, l.source_id, l.target_type, l.target_id].join('|');
}

function mergeLinkChanges(changes) {
  const map = new Map();
  (changes || []).forEach(l => {
    const key = linkChangeKey(l);
    const prev = map.get(key);
    if (!prev) {
      map.set(key, Object.assign({}, l));
      return;
    }
    if ((prev.kind === 'create' && l.kind === 'delete') ||
        (prev.kind === 'delete' && l.kind === 'create')) {
      map.delete(key);
    } else {
      map.set(key, Object.assign({}, l));
    }
  });
  return Array.from(map.values());
}

function effectiveObjects(typeName) {
  const base = (S.objects && S.objects[typeName]) || [];
  if (S.stateTab !== 'sim' || !S.simPreview) return base;
  const types = (S.meta && S.meta.object_types) || [];
  const typeMeta = types.find(t => t.name === typeName);
  const pk = (typeMeta && typeMeta.primary_key) || 'id';
  const changes = mergeObjectChanges(
    (S.simPreview.diff && S.simPreview.diff.object_changes) || []
  ).filter(c => c.object_type === typeName);
  if (!changes.length) return base;
  const byId = new Map();
  changes.forEach(c => byId.set(String(c.object_id), c));
  const out = [];
  base.forEach(row => {
    const id = String(row[pk]);
    const ch = byId.get(id);
    if (!ch) { out.push(row); return; }
    if (ch.kind === 'delete') { byId.delete(id); return; }
    out.push(ch.after ? Object.assign({}, row, ch.after, {_simKind: ch.kind}) : row);
    byId.delete(id);
  });
  byId.forEach(ch => {
    if (ch.kind !== 'delete' && ch.after) {
      out.push(Object.assign({}, ch.after, {_simKind: ch.kind}));
    }
  });
  return out;
}

function renderObjects() {
  const box = $('objects');
  if (!box) return;
  const isSim = S.stateTab === 'sim';
  if (isSim && !S.simPreview) {
    box.innerHTML = '<div class="hint" style="padding:12px 4px">暂无沙盘推演结果。请在右侧点击「<b>🔍 沙盘预演</b>」或「<b>🌳 多步智能推演</b>」，推演后的预期状态将展示在此标签页中（主库零写入）。</div>';
    return;
  }
  const list = S.objects[S.type] || [];
  const typeMeta = S.meta.object_types.find(t => t.name === S.type) || {fields: []};
  const allObjChanges = isSim ? mergeObjectChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.object_changes) || []) : [];
  const allLinkChanges = isSim ? mergeLinkChanges((S.simPreview && S.simPreview.diff && S.simPreview.diff.link_changes) || []) : [];
  const changeById = {};
  const creates = [];
  const otherTypes = {};
  allObjChanges.forEach(c => {
    if (c.object_type === S.type) {
      if (c.kind === 'create') creates.push(c);
      else changeById[String(c.object_id)] = c;
    } else {
      otherTypes[c.object_type] = (otherTypes[c.object_type] || 0) + 1;
    }
  });

  let banner = '';
  if (isSim && S.simPreview) {
    const jumpBtns = Object.keys(otherTypes).map(t =>
      '<button class="v" style="padding:2px 7px;font-size:10px" onclick="pickType(\'' + esc(t) + '\')">查看 ' +
      esc(zhObject(t)) + ' (+' + otherTypes[t] + ')</button>').join('');
    const stepInfo = S.simSteps.length > 1
      ? '（已推演 ' + S.simSteps.length + ' 步 · 正式执行仅提交第 1 步，后续需基于新现实重演）'
      : '';
    const summaryText = (allObjChanges.length || allLinkChanges.length)
      ? '「' + esc(zhAction(S.simPreview.action || '')) + '」推演后状态' + stepInfo
      : '「' + esc(zhAction(S.simPreview.action || '')) + '」推演完成（无对象或关系变更）' + stepInfo;
    const firstStep = S.simSteps[0];
    const commitLabel = firstStep
      ? '⚡ 正式执行第 1 步（' + esc(zhAction(firstStep.action)) + '）'
      : '⚡ 正式执行第 1 步';
    banner = '<div class="sim-banner"><span>🧪 <b>沙盘推演状态</b>（主库未写入 · ' + summaryText + '）</span>' +
      '<div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap">' + jumpBtns +
      '<button class="g" id="submitBtn" style="padding:3px 9px;font-size:10.5px" onclick="commitSimPreview()">' + commitLabel + '</button>' +
      '<button style="padding:2px 8px;font-size:10px" onclick="pickStateTab(\'real\')">查看当前业务状态</button>' +
      '<button style="padding:2px 8px;font-size:10px" onclick="clearSimPreview()">清除沙盘</button></div></div>';
  }

  if (!list.length && !creates.length) {
    box.innerHTML = banner + '<div class="hint">该类型暂无可见对象。</div>';
    return;
  }

  const pkKey = typeMeta.primary_key;
  const existingCards = list.map((o, idx) => {
    const pk = o[pkKey || Object.keys(o)[0]];
    const ch = changeById[String(pk)];
    const simCls = ch ? (ch.kind === 'delete' ? ' sim-delete' : ' sim-update') : '';
    const simBadge = ch
      ? (ch.kind === 'delete' ? ' <span class="badge r">沙盘删除</span>' : ' <span class="badge v">沙盘变更</span>')
      : '';
    const afterObj = (ch && ch.after) || {};
    const fields = typeMeta.fields.map(f => {
      const cur = formatFieldVal(f, o[f.name]);
      if (ch && ch.kind === 'update' && !cur.masked && Object.prototype.hasOwnProperty.call(afterObj, f.name)) {
        const next = formatFieldVal(f, afterObj[f.name]);
        if (cur.text !== next.text) {
          return '<span title="' + esc(f.name) + '">' + esc(zhField(S.type, f.name)) +
            ' <s class="sim-old">' + esc(cur.text) + '</s><b class="sim-new">' + esc(next.text) + '</b></span>';
        }
      }
      return '<span title="' + esc(f.name) + '">' + esc(zhField(S.type, f.name)) +
        ' <b class="' + cur.cls + '">' + esc(cur.text) + '</b></span>';
    }).join('');
    return '<div class="obj' + simCls + '"><h4><span>' + esc(zhObject(S.type)) + simBadge + '</span><span>' + esc(pk) + '</span></h4>' +
      '<div class="kv">' + fields + '</div>' +
      '<div class="ops"><button onclick="pickObject(' + idx + ')">载入参数</button>' +
      '<button class="v" onclick="quickSim(' + idx + ')">沙盘预演</button></div></div>';
  }).join('');

  const createCards = creates.map(c => {
    const afterObj = c.after || {};
    const fields = typeMeta.fields.map(f => {
      const val = formatFieldVal(f, afterObj[f.name]);
      return '<span title="' + esc(f.name) + '">' + esc(zhField(S.type, f.name)) +
        ' <b class="' + (val.masked ? 'masked' : 'sim-new') + '">' + esc(val.text) + '</b></span>';
    }).join('');
    return '<div class="obj sim-create"><h4><span>' + esc(zhObject(S.type)) + ' <span class="badge g">沙盘新增</span></span><span>' +
      esc(c.object_id) + '</span></h4><div class="kv">' + fields + '</div></div>';
  }).join('');

  box.innerHTML = banner + createCards + existingCards;
}

function renderLinks(links) {
  const box = $('links');
  if (!box) return;
  const baseLinks = links || S.links || [];
  const linkChanges = mergeLinkChanges(
    (S.stateTab === 'sim' && S.simPreview && S.simPreview.diff && S.simPreview.diff.link_changes) || []
  );
  const baseSet = new Set(baseLinks.map(linkChangeKey));
  const delSet = new Set(linkChanges.filter(c => c.kind === 'delete').map(linkChangeKey));
  const createList = linkChanges.filter(c => c.kind === 'create' && !baseSet.has(linkChangeKey(c)));
  const formatLink = (l, extraCls, badge) =>
    '<div class="kv' + (extraCls ? ' ' + extraCls : '') + '" style="margin:4px 0">' + badge +
    '<b>' + esc(zh('object_properties', l.link_type, '') || zh('links', l.link_type, l.link_type)) + '</b><span>' +
    esc(zhObject(l.source_type)) + '：' + esc(l.source_id) + ' → ' + esc(zhObject(l.target_type)) + '：' +
    esc(l.target_id) + '</span></div>';

  const createRows = createList.map(l => formatLink(l, 'sim-link-create', '<span class="badge g">沙盘新增</span> ')).join('');
  const rows = baseLinks.slice(0, 120).map(l => {
    const isDel = delSet.has(linkChangeKey(l));
    return formatLink(l, isDel ? 'sim-link-delete' : '', isDel ? '<span class="badge r">沙盘删除</span> ' : '');
  }).join('');
  const funcs = (S.meta.functions || []).map(f =>
    '<div class="ops" style="margin:4px 0"><span class="badge">' + esc(zh('functions', f.name, f.name)) + '</span>' +
    '<span class="en">' + esc(f.name) + '</span>' +
    '<button onclick="callFunction(\'' + f.name + '\')">调用</button></div>').join('');
  box.innerHTML = '<div class="sect">🔗 语义关系</div>' + (createRows + rows || '<div class="hint">暂无关系边。</div>') +
    '<div class="sect">🧮 查询与计算（点击实时调用）</div>' + (funcs || '<div class="hint">无。</div>') +
    '<pre id="fnout" class="hint" style="margin-top:6px"></pre>';
}

async function callFunction(name) {
  try {
    const data = await api('/api/function', {function: name, params: {}});
    const el = $('fnout');
    if (el) el.textContent = name + ' →\n' + JSON.stringify(data.result, null, 2);
    toast('函数 ' + name + ' 返回');
  } catch (e) { toast(e.message, true); }
}

function currentAction() {
  return (S.meta.actions || []).find(a => a.name === $('action').value);
}

function typeOf(prop) {
  if (!prop) return 'string';
  if (prop.type) return prop.type;
  if (prop.anyOf) {
    const t = prop.anyOf.find(x => x.type && x.type !== 'null');
    if (t) return t.type;
  }
  return 'string';
}

function defaultFor(prop) {
  const enums = enumValuesFor(prop);
  if (enums && enums.length && !isNullable(prop)) return enums[0];
  const t = typeOf(prop);
  if (t === 'integer' || t === 'number') return 0;
  if (t === 'boolean') return false;
  return '';
}

function enumValuesFor(prop) {
  if (!prop) return null;
  if (Array.isArray(prop.enum)) return prop.enum;
  if (Array.isArray(prop.anyOf)) {
    const hit = prop.anyOf.find(x => x && Array.isArray(x.enum));
    if (hit) return hit.enum;
  }
  return null;
}

function enumOptionsFor(k, prop) {
  const enums = enumValuesFor(prop);
  if (!enums) return null;
  return enums.map(ev => {
    const raw = String(ev);
    const zhLabel = (k === 'role' ? zh('roles', raw, '') : '') ||
      zh('statuses', raw, '') || zh('roles', raw, '');
    return {
      value: raw,
      label: zhLabel && zhLabel !== raw ? (zhLabel + ' (' + raw + ')') : raw,
    };
  });
}

function isNullable(prop) {
  if (!prop) return false;
  if (prop.nullable) return true;
  if (Array.isArray(prop.anyOf)) return prop.anyOf.some(x => x && x.type === 'null');
  return false;
}

function isAutoGenerated(prop) {
  if (!prop) return false;
  if (prop.auto_generated || prop['x-auto-generated']) return true;
  if (Array.isArray(prop.anyOf)) return prop.anyOf.some(x => x && (x.auto_generated || x['x-auto-generated']));
  return false;
}

function isMultiple(prop) {
  return !!(prop && (typeOf(prop) === 'array' || prop.multiple));
}

function refOptionsFor(prop) {
  const refType = prop && (
    prop.range ||
    (Array.isArray(prop.anyOf) && (prop.anyOf.find(x => x && x.range) || {}).range)
  );
  if (!refType) return null;
  const types = (S.meta && S.meta.object_types) || [];
  const typeMeta = types.find(t => t.name === refType);
  const pk = (typeMeta && typeMeta.primary_key) || 'id';
  const tf = typeMeta && typeMeta.title;
  const rows = effectiveObjects(refType);
  return rows
    .filter(r => r && r[pk] !== undefined && r[pk] !== null && r[pk] !== '***MASKED***')
    .map(r => {
      const val = String(r[pk]);
      const title = tf && r[tf] && r[tf] !== '***MASKED***' ? String(r[tf]) : '';
      const simTag = r._simKind === 'create' ? ' [沙盘]' : '';
      return {value: val, label: (title ? (val + ' · ' + title) : val) + simTag};
    });
}

function mselLabel(selected) {
  return selected.length ? ('已选 ' + selected.length + ' 项：' + selected.join(', ')) : '未选择（可多选）';
}

function closeAllMsels(exceptEl) {
  document.querySelectorAll('.msel.open').forEach(el => {
    if (el !== exceptEl) el.classList.remove('open');
  });
}

function toggleMsel(btn, ev) {
  if (ev) ev.stopPropagation();
  const msel = btn && btn.closest('.msel');
  if (!msel) return;
  const willOpen = !msel.classList.contains('open');
  closeAllMsels(msel);
  if (!willOpen) { msel.classList.remove('open'); return; }
  msel.classList.add('open');
  const menu = msel.querySelector('.msel-menu');
  const r = btn.getBoundingClientRect();
  if (menu && r.width) {
    const w = Math.min(Math.max(r.width, 260), window.innerWidth - 24);
    const left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12));
    menu.style.left = left + 'px';
    menu.style.width = w + 'px';
    const below = window.innerHeight - r.bottom - 12;
    if (below >= 120 || below >= r.top) {
      menu.style.top = (r.bottom + 4) + 'px';
      menu.style.bottom = 'auto';
      menu.style.maxHeight = Math.max(120, Math.min(240, below)) + 'px';
    } else {
      menu.style.top = 'auto';
      menu.style.bottom = (window.innerHeight - r.top + 4) + 'px';
      menu.style.maxHeight = Math.max(120, Math.min(240, r.top - 12)) + 'px';
    }
  }
}

function syncMsel(cb) {
  const msel = cb && cb.closest('.msel');
  if (!msel) return;
  const vals = Array.from(msel.querySelectorAll('input[type="checkbox"]:checked')).map(x => x.value);
  const txt = msel.querySelector('.msel-txt');
  if (txt) txt.textContent = mselLabel(vals);
}

function mselMenuHtml(opts, selected, extraCbAttrs) {
  if (!opts.length) return '<div class="hint" style="padding:6px 8px">暂无可选对象</div>';
  return opts.map(o =>
    '<label class="msel-opt"><input type="checkbox" value="' + esc(o.value) + '"' +
    (selected.indexOf(o.value) >= 0 ? ' checked' : '') +
    ' onchange="syncMsel(this)' + (extraCbAttrs || '') + '"><span>' + esc(o.label) + '</span></label>'
  ).join('');
}

function renderFieldHtml(k, prop, v, headHtml, attrs, isAgent) {
  const t = typeOf(prop);
  if (isAutoGenerated(prop)) {
    return '<label class="f">' + headHtml + '<input ' + attrs +
      ' type="text" value="" placeholder="系统自动生成" readonly class="auto-id"></label>';
  }
  if (t === 'boolean') {
    return '<label class="f">' + headHtml + '<select ' + attrs + '><option value="true"' +
      (v ? ' selected' : '') + '>是</option><option value="false"' + (v ? '' : ' selected') + '>否</option></select></label>';
  }
  const refOpts = refOptionsFor(prop);
  const opts = refOpts !== null ? refOpts : enumOptionsFor(k, prop);
  if (isMultiple(prop)) {
    const selected = Array.isArray(v) ? v.map(String) : [];
    if (opts !== null) {
      const cbExtra = isAgent ? ';pushGhostEvent(\'form_interaction\',{action:this.closest(\'[data-action]\').getAttribute(\'data-action\'),field:\'' + esc(k) + '\'})' : '';
      return '<div class="f">' + headHtml + '<div class="msel" ' + attrs + ' data-multi="1">' +
        '<button type="button" class="msel-btn" onclick="toggleMsel(this, event)">' +
        '<span class="msel-txt">' + esc(mselLabel(selected)) + '</span><span>▾</span></button>' +
        '<div class="msel-menu">' + mselMenuHtml(opts, selected, cbExtra) + '</div></div></div>';
    }
    return '<label class="f">' + headHtml + '<input ' + attrs + ' type="text" value="' +
      esc(selected.join(', ')) + '" placeholder="逗号分隔"></label>';
  }
  if (opts !== null) {
    const cur = (v === null || v === undefined) ? '' : String(v);
    const nullable = isNullable(prop);
    let optionsHtml = nullable ? '<option value=""' + (cur === '' ? ' selected' : '') + '>（不指定 / 无）</option>' : '';
    optionsHtml += opts.map(o =>
      '<option value="' + esc(o.value) + '"' + (cur === o.value ? ' selected' : '') + '>' + esc(o.label) + '</option>'
    ).join('');
    if (cur !== '' && !opts.some(o => o.value === cur)) {
      optionsHtml += '<option value="' + esc(cur) + '" selected>' + esc(cur) + '</option>';
    }
    return '<label class="f">' + headHtml + '<select ' + attrs + '>' + optionsHtml + '</select></label>';
  }
  const strVal = (v === null || v === undefined) ? '' : String(v);
  return '<label class="f">' + headHtml + '<input ' + attrs + ' type="' +
    (t === 'integer' || t === 'number' ? 'number' : 'text') + '" value="' + esc(strVal) + '"></label>';
}

function smartPrefill(actionName) {
  const act = (S.meta.actions || []).find(a => a.name === actionName);
  const props = (act && act.params && act.params.properties) || {};
  const sample = ((S.inst && S.inst.sample_action_params) || {})[actionName] || {};
  const out = {};
  Object.keys(props).forEach(k => {
    const prop = props[k] || {};
    if (isAutoGenerated(prop)) { out[k] = ''; return; }
    if (isMultiple(prop)) {
      out[k] = Array.isArray(sample[k]) ? sample[k] : (Array.isArray(prop.default) ? prop.default : []);
      return;
    }
    if (sample[k] !== undefined) { out[k] = sample[k]; return; }
    if (isNullable(prop) && prop.default === null) { out[k] = ''; return; }
    let found = null;
    for (const type of Object.keys(S.objects)) {
      const row = (S.objects[type] || []).find(o => o[k] !== undefined && o[k] !== null && o[k] !== '***MASKED***');
      if (row) { found = row[k]; break; }
    }
    out[k] = found !== null ? found : defaultFor(prop);
  });
  return out;
}

function fillParams() {
  const box = $('result');
  if (box) box.textContent = '沙盘预演或正式执行后，这里显示对象变更 / 错误码。';
  const act = currentAction();
  if (!act) { $('form').innerHTML = ''; $('actmeta').textContent = ''; return; }
  S.lastAction = act.name;
  const props = (act.params && act.params.properties) || {};
  const values = smartPrefill(act.name);
  $('actmeta').textContent = '可执行角色：' + ((act.allowed_roles || []).map(zhRole).join(' / ') || '—');
  $('form').innerHTML = Object.keys(props).map(k => {
    const head = '<span class="fl">' + esc(zhField(null, k)) + '<em>' + esc(k) + '</em></span>';
    return renderFieldHtml(k, props[k], values[k], head, 'id="p_' + esc(k) + '"', false);
  }).join('');
}

function refreshParamOptions() {
  const act = currentAction();
  if (!act) return;
  const props = (act.params && act.params.properties) || {};
  Object.keys(props).forEach(k => {
    const prop = props[k] || {};
    const el = $('p_' + k);
    if (!el) return;
    if (isAutoGenerated(prop)) { el.value = ''; return; }
    const refOpts = refOptionsFor(prop);
  const opts = refOpts !== null ? refOpts : enumOptionsFor(k, prop);
    if (opts === null) return;
    if (el.getAttribute('data-multi') === '1') {
      const checked = new Set(
        Array.from(el.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value)
      );
      const menu = el.querySelector('.msel-menu');
      if (menu) menu.innerHTML = mselMenuHtml(opts, Array.from(checked), '');
      const kept = opts.filter(o => checked.has(o.value)).map(o => o.value);
      const txt = el.querySelector('.msel-txt');
      if (txt) txt.textContent = mselLabel(kept);
      return;
    }
    if (el.tagName === 'SELECT') {
      const prev = el.value;
      const nullable = isNullable(prop);
      let html = nullable ? '<option value="">（不指定 / 无）</option>' : '';
      html += opts.map(o => '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>').join('');
      el.innerHTML = html;
      if (prev && opts.some(o => o.value === prev)) el.value = prev;
      else if (nullable) el.value = '';
    }
  });
}

function collectParams() {
  const act = currentAction();
  const props = (act && act.params && act.params.properties) || {};
  const out = {};
  Object.keys(props).forEach(k => {
    const prop = props[k] || {};
    const el = $('p_' + k);
    if (!el) return;
    if (isAutoGenerated(prop)) {
      const raw = (el.value || '').trim();
      if (raw) out[k] = raw;
      return;
    }
    const t = typeOf(prop);
    if (isMultiple(prop)) {
      if (el.getAttribute('data-multi') === '1') {
        out[k] = Array.from(el.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
      } else {
        out[k] = (el.value || '').split(',').map(s => s.trim()).filter(Boolean);
      }
      return;
    }
    if (t === 'integer') out[k] = parseInt(el.value || '0', 10);
    else if (t === 'number') out[k] = parseFloat(el.value || '0');
    else if (t === 'boolean') out[k] = el.value === 'true';
    else if (isNullable(prop) && el.value === '') out[k] = null;
    else out[k] = el.value;
  });
  return out;
}

function pickObject(idx) {
  const o = (S.objects[S.type] || [])[idx];
  if (!o) return;
  const act = currentAction();
  const props = (act && act.params && act.params.properties) || {};
  Object.keys(props).forEach(k => {
    const prop = props[k] || {};
    if (isAutoGenerated(prop)) return;
    const el = $('p_' + k);
    if (!el) return;
    if (o[k] === undefined || o[k] === '***MASKED***') return;
    if (el.getAttribute('data-multi') === '1') {
      const vals = Array.isArray(o[k]) ? o[k].map(String) : [];
      el.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.checked = vals.indexOf(cb.value) >= 0;
      });
      const txt = el.querySelector('.msel-txt');
      if (txt) txt.textContent = mselLabel(vals);
      return;
    }
    el.value = (o[k] === null) ? '' : o[k];
  });
  toast('已载入对象参数');
}

async function quickSim(idx) { pickObject(idx); await simulate(); }

function show(title, payload) {
  const el = $('result');
  if (el) el.textContent = title + '\n' + JSON.stringify(payload, null, 2);
}

function diffTable(diff) {
  const changes = (diff && diff.object_changes) || [];
  const linkChanges = (diff && diff.link_changes) || [];
  if (!changes.length && !linkChanges.length) return '<div class="hint">无对象或关系变更。</div>';
  let html = '';
  if (changes.length) {
    html += '<table class="diff"><tr><th>变更</th><th>对象</th><th>变更前</th><th>变更后</th></tr>' +
      changes.map(c => '<tr><td>' + esc(c.kind) + '</td><td>' + esc(zhObject(c.object_type)) + '：' +
        esc(c.object_id) + '</td><td class="b">' + esc(JSON.stringify(c.before || {})) +
        '</td><td class="a">' + esc(JSON.stringify(c.after || {})) + '</td></tr>').join('') + '</table>';
  }
  if (linkChanges.length) {
    html += '<table class="diff" style="margin-top:6px"><tr><th>关系变更</th><th>关系类型</th><th>源对象</th><th>目标对象</th></tr>' +
      linkChanges.map(l => '<tr><td>' + esc(l.kind) + '</td><td>' +
        esc(zh('object_properties', l.link_type, '') || zh('links', l.link_type, l.link_type)) + '</td><td>' +
        esc(zhObject(l.source_type)) + '：' + esc(l.source_id) + '</td><td>' +
        esc(zhObject(l.target_type)) + '：' + esc(l.target_id) + '</td></tr>').join('') + '</table>';
  }
  return html;
}

function applySimPreview(actionName, simData, params) {
  const diff = (simData && simData.diff) || { object_changes: [], link_changes: [] };
  if (simData && simData.valid) {
    if (params !== undefined) {
      S.simSteps.push({ action: actionName, params: params || {} });
      const prevObj = (S.simPreview && S.simPreview.diff && S.simPreview.diff.object_changes) || [];
      const prevLink = (S.simPreview && S.simPreview.diff && S.simPreview.diff.link_changes) || [];
      S.simPreview = {
        action: actionName,
        diff: {
          object_changes: mergeObjectChanges(prevObj.concat(diff.object_changes || [])),
          link_changes: mergeLinkChanges(prevLink.concat(diff.link_changes || [])),
        },
      };
    } else {
      S.simSteps = [];
      S.simPreview = {
        action: actionName,
        diff: {
          object_changes: mergeObjectChanges(diff.object_changes || []),
          link_changes: mergeLinkChanges(diff.link_changes || []),
        },
      };
    }
    S.stateTab = 'sim';
    const touchedTypes = (diff.object_changes || []).map(c => c.object_type);
    if (touchedTypes.length && touchedTypes.indexOf(S.type) < 0) {
      S.type = touchedTypes[0];
    }
  }
  renderStateTabs();
  renderFingerprint(S.fp);
  renderObjectTabs();
  renderObjects();
  renderLinks(S.links);
  refreshParamOptions();
}

async function simulate() {
  try {
    const actionName = $('action').value;
    const params = collectParams();
    if (!S.simSessionId) S.simSessionId = 'SIM-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7);
    const data = await api('/api/simulate', {
      action: actionName,
      params,
      prior_steps: S.simSteps,
      session_id: S.simSessionId,
    });
    const el = $('result');
    if (el) el.innerHTML = '<b class="' + (data.valid ? 'ok' : 'err') + '">沙盘预演 → ' +
      (data.valid ? '通过' : '被拒绝 ' + esc(data.error_code || '')) + '</b>' +
      (data.error ? '<div class="err">' + esc(data.error) + '</div>' : '') +
      '<div class="hint">需要审批：' + (data.requires_approval ? '是' : '否') +
      (data.sim_id ? (' · 已存库 ' + esc(data.sim_id)) : '') + '</div>' + diffTable(data.diff);
    applySimPreview(actionName, data, params);
    const simsRes = await api('/api/simulations').catch(() => null);
    if (simsRes) { S.simulations = simsRes.simulations || []; renderSimulations(); }
    toast(data.valid ? '沙盘预演通过（已存入沙盘诊断库，主库零写入）' : '沙盘预演被拒绝（已存入 Bug 诊断库）', !data.valid);
  } catch (e) {
    const simsRes = await api('/api/simulations').catch(() => null);
    if (simsRes) { S.simulations = simsRes.simulations || []; renderSimulations(); }
    show('沙盘预演失败', {error: e.message}); toast(e.message, true);
  }
}

async function commitSimPreview() {
  if (!S.simSteps.length) {
    if (S.mcts) { await commitMcts(); return; }
    toast('暂无可提交的沙盘推演步骤', true);
    return;
  }
  const totalPlanned = S.simSteps.length;
  const firstStep = S.simSteps[0];
  try {
    const data = await api('/api/submit', {
      run_id: 'CC-' + Date.now(),
      action: firstStep.action,
      params: firstStep.params || {},
      simulation_session_id: S.simSessionId || null,
    });
    const run = data.run || {};
    const el = $('result');
    if (el) el.innerHTML = '<b class="ok">正式执行第 1 步 → ' + esc(zhStatus(run.status || 'COMMITTED')) +
      (totalPlanned > 1 ? '（原推演共 ' + totalPlanned + ' 步，已按滚动视野只执行第 1 步）' : '') + '</b>' +
      '<div class="hint">执行编号：' + esc(run.run_id || '') + '</div>' + diffTable(run.object_diff);
    toast('已正式执行第 1 步：' + zhAction(firstStep.action));
    await refreshInstance();
  } catch (e) { show('正式执行失败', {error: e.message}); toast(e.message, true); }
}

async function submitAction() {
  try {
    const runId = 'CC-' + Date.now();
    const data = await api('/api/submit', {run_id: runId, action: $('action').value, params: collectParams()});
    const el = $('result');
    if (el) el.innerHTML = '<b class="ok">正式执行 → ' + esc(zhStatus(data.run.status)) + '</b>' +
      '<div class="hint">执行编号：' + esc(data.run.run_id) + '</div>' + diffTable(data.run.object_diff);
    toast('已正式执行：' + zhStatus(data.run.status));
    await refreshInstance();
  } catch (e) { show('正式执行失败', {error: e.message}); toast(e.message, true); }
}

let _refreshInFlight = false;

async function refreshInstance(opts) {
  opts = opts || {};
  if (!S.inst || _refreshInFlight) return;
  _refreshInFlight = true;
  try {
    S.catalog = await api('/api/catalog');
    const fresh = S.catalog.instances.find(i => i.instance_id === S.inst.instance_id);
    if (fresh) S.inst = fresh;
    chips();
    renderLists();
    const fp = await api('/api/fingerprint');
    const runs = (await api('/api/action-runs')).runs;
    const links = (await api('/api/links')).links;
    const simsRes = await api('/api/simulations').catch(() => ({simulations: []}));
    const health = await api('/api/world-health').catch(() => null);
    S.fp = fp;
    S.links = links || [];
    S.simulations = simsRes.simulations || [];
    S.health = health;
    if (!opts.preserveSim) {
      S.simPreview = null;
      S.simSteps = [];
      S.simSessionId = null;
      S.stateTab = 'real';
    }
    for (const t of S.meta.object_types) {
      const res = await api('/api/objects/' + encodeURIComponent(t.name));
      S.objects[t.name] = res.objects;
    }
    renderStateTabs();
    renderObjectTabs();
    renderFingerprint(fp);
    renderWorldHealth();
    renderObjects();
    renderLinks(links);
    renderRuns(runs);
    renderSimulations();
    const ae = document.activeElement;
    const inParamField = !!(ae && (
      (ae.id && (ae.id.startsWith('p_') || ae.id.startsWith('fp_'))) ||
      (ae.closest && ae.closest('.msel'))
    ));
    if (!opts.preserveSim || !inParamField) refreshParamOptions();
  } catch (e) {
    if (!opts.silent) toast(e.message, true);
  } finally {
    _refreshInFlight = false;
  }
}

async function runMcts() {
  try {
    const box = $('mcts');
    if (box) box.innerHTML = '多步智能推演中……（平行沙盘，主库零污染）';
    const data = await api('/api/instance/mcts', {num_simulations: 80});
    S.mcts = data;
    const rows = (data.root_stats || []).map(r => '<tr><td><b>' + esc(r.label) + '</b></td><td>' +
      Number(r.prior).toFixed(3) + '</td><td>' + r.visits + '</td><td>' + Number(r.mean_value).toFixed(0) + '</td></tr>').join('');
    const pruned = (data.pruned_branches || []).map(p => '<span class="badge r">' + esc(p.action || JSON.stringify(p)) + '</span>').join('') ||
      '<span class="hint">无</span>';
    if (box) box.innerHTML = '<div class="sect">🌳 多步智能推演 · 根节点统计 P / N / Q</div>' +
      '<table><tr><th>业务动作</th><th>P</th><th>N</th><th>Q</th></tr>' + rows + '</table>' +
      '<div class="sect">符号裁判剪枝</div><div class="badges">' + pruned + '</div>' +
      '<div class="sect">最优序列 · 终局价值 ' + Number(data.best_value).toFixed(0) + '</div>' +
      '<div class="badges">' + (data.best_sequence || []).map((s, i) =>
        '<span class="badge c">' + (i + 1) + '. ' + esc(s) + '</span>').join('') + '</div>' +
      '<button class="g" style="margin-top:8px" onclick="commitMcts()">⚡ 一键提交最优序列</button>';
    if (data.diff) {
      applySimPreview('MCTS 多步推演 (' + (data.best_sequence || []).length + '步)', { valid: true, diff: data.diff });
    }
    toast('多步推演完成：终局价值 ' + data.best_value);
  } catch (e) {
    const box = $('mcts');
    if (box) box.innerHTML = '<span class="err">多步推演失败：' + esc(e.message) + '</span>';
    toast(e.message, true);
  }
}

async function commitMcts() {
  try {
    const data = await api('/api/instance/mcts', {num_simulations: 80, commit: true});
    toast('最优序列已正式执行（' + (data.committed_runs || []).length + ' 步）');
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function approveRun(runId) {
  try {
    const data = await api('/api/approve', {run_id: runId});
    toast('已批准 ' + runId + ' → ' + zhStatus(data.run.status));
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function revertRun(runId) {
  try {
    const data = await api('/api/revert', {run_id: runId});
    toast('已撤回 ' + runId + ' → ' + zhStatus(data.run.status));
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function runAutomations() {
  if (!S.inst) { toast('请先选择一个世界状态', true); return; }
  try {
    const data = await api('/api/automations/run', {});
    toast(data.fired.length ? ('自动化规则触发 ' + data.fired.length + ' 次') : '没有新的可触发对象');
    if (S.inst) await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function forkInstance() {
  if (!S.inst) { toast('请先选择一个世界状态', true); return; }
  try {
    const data = await api('/api/instance/fork', {instance: S.inst.instance_id, label: S.inst.label + ' · 克隆沙盘'});
    S.catalog = await api('/api/catalog');
    chips();
    renderLists();
    toast('已克隆沙盘：' + data.instance.instance_id);
    await selectInstance(data.instance.instance_id);
  } catch (e) { toast(e.message, true); }
}

async function unplug() {
  if (!S.inst) return;
  try {
    const data = await api('/api/instance/unplug', {instance: S.inst.instance_id});
    toast('已断开外部系统：' + (data.unplugged || []).join(', '));
    await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

function renderRuns(runs) {
  const box = $('runs');
  if (!box) return;
  if (!runs.length) { box.innerHTML = '<div class="hint">动作执行记录为空。</div>'; return; }
  box.innerHTML = runs.slice().reverse().map(r => {
    const ops = [];
    if (r.status === 'PENDING_APPROVAL') ops.push('<button class="g" onclick="approveRun(\'' + esc(r.run_id) + '\')">✅ 批准</button>');
    if (r.status === 'COMMITTED') ops.push('<button class="r" onclick="revertRun(\'' + esc(r.run_id) + '\')">↩️ 撤回</button>');
    return '<div class="run"><div class="id">' + esc(r.run_id) + ' <span class="tag t-' + esc(r.status) + '">' +
      esc(zhStatus(r.status)) + '</span></div><div class="kv"><span>' + esc(zhAction(r.action_type)) + '</span><span>' +
      esc(zhRole(r.actor_role)) + '</span><span class="err">' + esc(r.error_code || '') + '</span></div>' +
      (ops.length ? '<div class="ops">' + ops.join('') + '</div>' : '') + '</div>';
  }).join('');
}

function toggleSimBugFilter(checked) {
  S.simOnlyBugs = !!checked;
  renderSimulations();
}

async function clearSimulationLedger() {
  try {
    const res = await fetch('/api/simulations?workspace=' + encodeURIComponent(S.inst.instance_id) +
      '&role=' + encodeURIComponent(role()), {method: 'DELETE'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    S.simulations = [];
    renderSimulations();
    toast('已清空当前实例的沙盘推演日志');
  } catch (e) { toast('清空失败：' + e.message, true); }
}

function renderSimulations() {
  const box = $('simulations');
  if (!box) return;
  const all = S.simulations || [];
  const rows = S.simOnlyBugs ? all.filter(s => !s.valid) : all;
  const bugCount = all.filter(s => !s.valid).length;
  const toolbar = '<div class="row" style="justify-content:space-between;margin-bottom:8px">' +
    '<label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:6px">' +
    '<input type="checkbox" ' + (S.simOnlyBugs ? 'checked ' : '') +
    'onchange="toggleSimBugFilter(this.checked)">仅看失败/被拒 Bug（' + bugCount + '）</label>' +
    (all.length ? '<button style="padding:2px 8px;font-size:10px" onclick="clearSimulationLedger()">清空诊断日志</button>' : '') +
    '</div>';
  if (!rows.length) {
    box.innerHTML = toolbar + '<div class="hint">暂无符合条件的沙盘推演记录。</div>';
    return;
  }
  box.innerHTML = toolbar + rows.slice(0, 40).map(s => {
    const statusBadge = s.valid
      ? '<span class="tag t-COMMITTED">推演通过</span>'
      : '<span class="tag t-REJECTED">被拒 ' + esc(s.error_code || 'ERR') + '</span>';
    const commitTag = s.committed_run_id
      ? ' <span class="badge g">第1步已提交 ' + esc(s.committed_run_id) + '</span>'
      : '';
    return '<div class="run"><div class="id">' + esc(s.sim_id) + ' · 第' + esc(s.step_index) + '步 ' +
      statusBadge + commitTag + '</div>' +
      '<div class="kv"><span>' + esc(zhAction(s.action_type)) + '</span><span>' +
      esc(zhRole(s.actor_role)) + '</span><span>' + esc(s.created_at) + '</span></div>' +
      '<div class="hint" style="margin-top:3px">参数：' + esc(JSON.stringify(s.params || {})) + '</div>' +
      (s.error_message ? '<div class="err" style="margin-top:3px">' + esc(s.error_message) + '</div>' : '') +
      '</div>';
  }).join('');
}

S.agent = {open: false, history: [], selectedIds: [], ghostEvents: [], scope: null};

function currentAgentScope() {
  if (S.agent.scope) return S.agent.scope;
  return S.pillar === 'instance' ? 'world' : 'platform';
}

function toggleAgentScope() {
  S.agent.scope = currentAgentScope() === 'platform' ? 'world' : 'platform';
  updateAgentContext();
}

function agentSessionKey() {
  return 'oo_agent_' + ((S.inst && S.inst.instance_id) || 'default') + '_' + role();
}

function saveAgentSession() {
  try {
    sessionStorage.setItem(agentSessionKey(), JSON.stringify({
      history: S.agent.history.slice(-20),
      selectedIds: S.agent.selectedIds.slice(-5),
    }));
  } catch (_) {}
}

function pushGhostEvent(eventType, detail) {
  const ev = {event_type: eventType, detail: detail || {}};
  S.agent.ghostEvents.push(ev);
  if (S.agent.ghostEvents.length > 20) S.agent.ghostEvents.shift();
  saveAgentSession();
  if (S.inst) {
    fetch('/api/agent/ghost?instance=' + encodeURIComponent(S.inst.instance_id) + '&role=' + encodeURIComponent(role()), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(ev),
    }).catch(() => {});
  }
}

if (typeof switchPillar === 'function') {
  const _origSwitchPillar = switchPillar;
  switchPillar = function(name) {
    _origSwitchPillar(name);
    pushGhostEvent('view_changed', {pillar: name});
  };
}

function agentUiContext() {
  return {
    ontology_id: S.ont ? S.ont.ontology_id : null,
    instance_id: S.inst ? S.inst.instance_id : null,
    active_object_type: S.type || null,
    selected_object_ids: S.agent.selectedIds.slice(),
  };
}

function updateAgentContext() {
  const scopeBtn = $('agentScopeBtn');
  const scope = currentAgentScope();
  if (scopeBtn) {
    scopeBtn.textContent = scope === 'platform' ? '🛠 平台管理员' : '🌍 业务世界';
  }
  const el = $('agentCtx');
  if (!el) return;
  if (scope === 'platform') {
    el.innerHTML = '当前模式：<b>平台管理员（M2 元本体）</b> · 可创建模型 / 宿体 / 世界状态 / 外部设备';
    return;
  }
  const ctx = agentUiContext();
  const parts = [];
  if (ctx.instance_id) parts.push('世界状态：' + esc(ctx.instance_id));
  if (ctx.active_object_type) parts.push('对象：' + esc(zhObject(ctx.active_object_type)));
  if (ctx.selected_object_ids.length) parts.push('已选：' + esc(ctx.selected_object_ids.join(', ')));
  if (S.agent.ghostEvents.length) parts.push('静默感知 ' + S.agent.ghostEvents.length + ' 条');
  el.innerHTML = parts.length ? parts.join(' · ') : '当前未绑定世界状态（将使用默认世界状态）';
}

function toggleAgentDrawer() {
  const drawer = $('agentDrawer');
  if (!drawer) return;
  S.agent.open = !S.agent.open;
  drawer.hidden = !S.agent.open;
  if (S.agent.open) updateAgentContext();
}

function clearAgentChat() {
  S.agent.history = [];
  S.agent.ghostEvents = [];
  saveAgentSession();
  const box = $('agentMessages');
  if (box) box.innerHTML = '<div class="amsg bot">对话已清空，请继续输入指令。</div>';
  updateAgentContext();
}

function applyAgentUiCommand(p) {
  if (!p || !p.command) return;
  if (p.command === 'open_topology') {
    if (typeof switchPillar === 'function') switchPillar('ontology');
    toast('智能助手已联动切换至领域模型拓扑视图');
    return;
  }
  if (p.command === 'focus_object') {
    if (p.object_type) S.type = p.object_type;
    if (p.object_id) S.agent.selectedIds = [p.object_id];
    if (typeof switchPillar === 'function') switchPillar('instance');
    updateAgentContext();
    toast('智能助手已聚焦对象：' + (p.object_id || ''));
    return;
  }
  if (p.command === 'apply_filter' && p.object_type) {
    S.type = p.object_type;
    if (typeof renderObjects === 'function') renderObjects();
    toast('已按智能助手指令过滤对象：' + zhObject(p.object_type));
  }
}

function appendAgentBubble(roleCls, htmlText, artifacts) {
  const box = $('agentMessages');
  if (!box) return null;
  const div = document.createElement('div');
  div.className = 'amsg ' + roleCls;
  const textEl = document.createElement('div');
  textEl.className = 'amsg-text';
  textEl.innerHTML = htmlText;
  div.appendChild(textEl);
  const artBoxEl = document.createElement('div');
  artBoxEl.className = 'amsg-cards';
  div.appendChild(artBoxEl);
  (artifacts || []).forEach(art => {
    mountArtifactCard(artBoxEl, art);
  });
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return {div, textEl, artBoxEl};
}

function mountArtifactCard(container, art) {
  const card = document.createElement('div');
  card.className = 'art-card';
  card.innerHTML = renderArtifactHtml(art);
  container.appendChild(card);
  if (art.kind === 'ui_command') applyAgentUiCommand(art.payload);
}

function selectAgentObject(objId) {
  if (!objId) return;
  S.agent.selectedIds = [objId];
  pushGhostEvent('selection_changed', {object_id: objId, selected_object_ids: [objId]});
  updateAgentContext();
  toast('已选中对象：' + objId + '（已静默同步至智能助手上下文）');
}

function renderArtifactHtml(art) {
  const p = art.payload || {};
  const aid = esc(art.artifact_id || ('art-' + Math.random().toString(36).slice(2, 8)));
  const head = '<h5><span>' + esc(art.title || art.kind) + '</span><span class="badge c">' + esc(art.kind) + '</span></h5>';

  if (art.kind === 'object_table') {
    const rows = p.rows || [];
    if (!rows.length) return head + '<div class="hint">暂无可见记录。</div>';
    const cols = (p.columns && p.columns.length) ? p.columns : Object.keys(rows[0]).slice(0, 4);
    const ths = cols.map(c => '<th>' + esc(zhField(p.object_type || '', c)) + '</th>').join('');
    const trs = rows.slice(0, 12).map(r => {
      const firstVal = r[cols[0]];
      const tds = cols.map(c => '<td>' + esc(zhValue(c, typeof r[c] === 'object' ? JSON.stringify(r[c]) : r[c])) + '</td>').join('');
      return '<tr style="cursor:pointer" onclick="selectAgentObject(\'' + esc(firstVal) + '\')">' + tds + '</tr>';
    }).join('');
    return head + '<div style="overflow:auto;max-height:220px"><table><tr>' + ths + '</tr>' + trs + '</table></div>' +
      '<div class="hint" style="margin-top:4px">点击行可将该对象同步至智能助手上下文</div>';
  }

  if (art.kind === 'action_form') {
    const props = (p.schema && p.schema.properties) || {};
    const prefill = p.prefill_params || {};
    const fields = Object.keys(props).map(k => {
      const def = props[k] || {};
      const val = isAutoGenerated(def)
        ? ''
        : (prefill[k] !== undefined ? prefill[k] : (def.default !== undefined ? def.default : ''));
      const ftype = typeOf(def);
      const attrs = 'data-art-field="' + esc(k) + '" data-ftype="' + esc(ftype) + '"' +
        (isNullable(def) ? ' data-nullable="1"' : '') +
        (isAutoGenerated(def) ? ' data-auto="1"' : '') +
        ' onchange="pushGhostEvent(\'form_interaction\',{action:\'' + esc(p.action || '') + '\',field:\'' + esc(k) + '\',value:this.value})"';
      const head = '<span>' + esc(zhField(p.action || '', k)) + '</span>';
      return renderFieldHtml(k, def, val, head, attrs, true);
    }).join('');
    return head + '<div id="' + aid + '" data-action="' + esc(p.action || '') + '">' +
      '<div class="row">' + (fields || '<div class="hint">此动作无需参数</div>') + '</div>' +
      '<button class="c" onclick="agentSimulateCard(\'' + aid + '\')">🧪 沙盘模拟</button>' +
      '</div>';
  }

  if (art.kind === 'diff_preview') {
    const sim = p.simulation || {};
    const okBadge = sim.valid ? '<span class="badge g">校验通过</span>' : '<span class="badge r">被拒绝</span>';
    const paramsJson = esc(JSON.stringify(p.params || {}));
    const btn = sim.valid
      ? '<button class="g" style="margin-top:6px" onclick="agentSubmitCard(\'' + esc(p.action || '') + '\', \'' + paramsJson + '\')">✅ 确认提交</button>'
      : '';
    return head + '<div class="row">' + okBadge +
      (sim.requires_approval ? '<span class="badge a">需审批</span>' : '') + '</div>' +
      (sim.error ? '<div class="err">' + esc(sim.error) + '</div>' : '') +
      diffTable(sim.diff) + btn;
  }

  if (art.kind === 'approval_card') {
    return head + '<div class="hint">单号：' + esc(p.run_id || '') + ' · 动作：' + esc(zhAction(p.action || '')) + '</div>' +
      diffTable(p.diff) +
      '<button class="g" style="margin-top:6px" onclick="agentApproveCard(\'' + esc(p.run_id || '') + '\')">✅ 批准执行</button>';
  }

  if (art.kind === 'metric_chart') {
    const bars = p.bars || [];
    if (!bars.length) return head + '<div class="hint">暂无统计数据。</div>';
    const maxVal = Math.max(1, ...bars.map(b => Number(b.count || 0)));
    const rowH = 24;
    const svgH = bars.length * rowH + 8;
    const rects = bars.map((b, i) => {
      const w = Math.max(4, Math.round((Number(b.count || 0) / maxVal) * 190));
      const y = i * rowH + 4;
      return '<text x="2" y="' + (y + 13) + '" fill="#b9cbe0" font-size="10.5">' + esc(zhStatus(b.label)) + '</text>' +
        '<rect x="92" y="' + (y + 2) + '" width="' + w + '" height="14" rx="4" fill="#38bdf8" fill-opacity="0.75"></rect>' +
        '<text x="' + (98 + w) + '" y="' + (y + 13) + '" fill="#e6eefb" font-size="10.5">' + esc(b.count) + '</text>';
    }).join('');
    return head + '<div class="hint">总计：' + esc(p.total || 0) + '</div>' +
      '<svg width="100%" viewBox="0 0 330 ' + svgH + '" style="margin-top:4px">' + rects + '</svg>';
  }

  if (art.kind === 'mcts_plan_card') {
    const seq = (p.best_sequence || []).map((s, i) =>
      '<span class="badge c">' + (i + 1) + '. ' + esc(s) + '</span>'
    ).join('') || '<span class="hint">无推荐序列</span>';
    return head + '<div class="hint">终局价值：<b>' + esc(p.best_value ?? '-') + '</b> · 模拟次数：' + esc(p.simulations ?? '-') + '</div>' +
      '<div class="badges" style="margin-top:6px">' + seq + '</div>' +
      '<button class="g" style="margin-top:8px" onclick="commitMcts()">⚡ 一键执行推荐序列</button>';
  }

  if (art.kind === 'ui_command') {
    return head + '<div class="hint">已执行界面联动指令：<b>' + esc(p.command || '') + '</b></div>';
  }

  return head + '<pre>' + esc(JSON.stringify(p, null, 2)) + '</pre>';
}

function collectCardParams(cardId) {
  const box = $(cardId);
  if (!box) return {};
  const out = {};
  box.querySelectorAll('[data-art-field]').forEach(inp => {
    const k = inp.getAttribute('data-art-field');
    const ft = inp.getAttribute('data-ftype');
    if (inp.getAttribute('data-multi') === '1') {
      out[k] = Array.from(inp.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
      return;
    }
    const raw = inp.value;
    if (inp.getAttribute('data-auto') === '1' && !(raw || '').trim()) return;
    if (raw === '') {
      if (inp.getAttribute('data-nullable') === '1') out[k] = null;
      return;
    }
    if (ft === 'integer') out[k] = parseInt(raw, 10);
    else if (ft === 'number') out[k] = parseFloat(raw);
    else if (ft === 'boolean') out[k] = raw === 'true';
    else if (ft === 'array') out[k] = raw.split(',').map(s => s.trim()).filter(Boolean);
    else out[k] = raw;
  });
  return out;
}

async function agentSimulateCard(cardId) {
  const box = $(cardId);
  if (!box) return;
  const action = box.getAttribute('data-action');
  const params = collectCardParams(cardId);
  try {
    const sim = await api('/api/simulate', {action, params, prior_steps: S.simSteps, scope: currentAgentScope()});
    applySimPreview(action, sim, params);
    appendAgentBubble('bot', '已完成沙盘预演：', [{
      artifact_id: 'sim-' + Date.now(),
      kind: 'diff_preview',
      title: '沙盘预演：' + zhAction(action),
      payload: {action, params, simulation: sim},
    }]);
  } catch (e) { toast(e.message, true); }
}

async function agentSubmitCard(action, paramsJson) {
  const scope = currentAgentScope();
  try {
    const params = JSON.parse(paramsJson || '{}');
    const data = await api('/api/submit', {run_id: 'AGENT-' + Date.now(), action, params, scope});
    const run = data.run || {};
    if (run.status === 'PENDING_APPROVAL') {
      appendAgentBubble('bot', '动作已提交，命中审批门禁，请审批：', [{
        artifact_id: 'apr-' + Date.now(),
        kind: 'approval_card',
        title: '待审批：' + zhAction(action),
        payload: {run_id: run.run_id, action: run.action_type, status: run.status, diff: run.object_diff},
      }]);
    } else {
      appendAgentBubble('bot', '正式执行成功：<b>' + esc(zhStatus(run.status)) + '</b>（' + esc(run.run_id) + '）');
    }
    if (scope === 'platform') await loadAll();
    else await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function agentApproveCard(runId) {
  const scope = currentAgentScope();
  try {
    const data = await api('/api/approve', {run_id: runId, scope});
    appendAgentBubble('bot', '审批完成：' + esc(runId) + ' → ' + esc(zhStatus((data.run || {}).status || '')));
    if (scope === 'platform') await loadAll();
    else await refreshInstance();
  } catch (e) { toast(e.message, true); }
}

async function sendAgentMessage() {
  const inp = $('agentInput');
  if (!inp) return;
  const msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  appendAgentBubble('user', esc(msg));
  const bubble = appendAgentBubble('bot', '<span class="hint">思考中…</span>');
  const qs = [];
  if (S.inst) qs.push('instance=' + encodeURIComponent(S.inst.instance_id));
  qs.push('role=' + encodeURIComponent(role()));
  const url = '/api/agent/chat' + (qs.length ? '?' + qs.join('&') : '');

  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      body: JSON.stringify({
        message: msg,
        history: S.agent.history,
        ui_context: agentUiContext(),
        ghost_events: S.agent.ghostEvents,
        scope: currentAgentScope(),
        stream: true,
      }),
    });
    const ctype = resp.headers.get('content-type') || '';
    if (!ctype.includes('text/event-stream') || !resp.body) {
      const res = await resp.json();
      if (!resp.ok || res.ok === false) throw new Error(res.error || ('HTTP ' + resp.status));
      bubble.textEl.innerHTML = esc(res.reply_text || '').replace(/\n/g, '<br>');
      (res.artifacts || []).forEach(art => mountArtifactCard(bubble.artBoxEl, art));
      S.agent.history.push({role: 'user', content: msg});
      S.agent.history.push({role: 'assistant', content: res.reply_text || ''});
      saveAgentSession();
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    let accText = '';
    const box = $('agentMessages');
    bubble.textEl.innerHTML = '';

    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      const frames = buf.split('\n\n');
      buf = frames.pop() || '';
      for (const frame of frames) {
        let evName = 'message';
        let dataLine = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) evName = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLine += line.slice(5).trim();
        }
        if (!dataLine) continue;
        const payload = JSON.parse(dataLine);
        if (evName === 'tool_call') {
          bubble.textEl.innerHTML = '<span class="hint">⚙ 正在调用 ' + esc(payload.name || '') + '…</span>';
        } else if (evName === 'token') {
          accText += (payload.text || '');
          bubble.textEl.innerHTML = esc(accText).replace(/\n/g, '<br>');
        } else if (evName === 'artifact') {
          mountArtifactCard(bubble.artBoxEl, payload);
        } else if (evName === 'done') {
          const finalText = payload.reply_text || accText;
          bubble.textEl.innerHTML = esc(finalText).replace(/\n/g, '<br>');
          S.agent.history.push({role: 'user', content: msg});
          S.agent.history.push({role: 'assistant', content: finalText});
          saveAgentSession();
        }
        if (box) box.scrollTop = box.scrollHeight;
      }
    }
  } catch (e) {
    if (bubble) bubble.textEl.innerHTML = '<span class="err">调用失败：' + esc(e.message) + '</span>';
  }
}

document.addEventListener('click', ev => {
  if (!ev.target || !ev.target.closest || !ev.target.closest('.msel')) closeAllMsels();
});
window.addEventListener('scroll', ev => {
  if (!ev.target || !ev.target.closest || !ev.target.closest('.msel-menu')) closeAllMsels();
}, true);

async function pollLiveFingerprint() {
  if (document.hidden || S.pillar !== 'instance' || !S.inst || _refreshInFlight) return;
  try {
    const fp = await api('/api/fingerprint');
    const prevRev = S.fp && S.fp.revision;
    if (prevRev && fp && fp.revision && fp.revision !== prevRev) {
      await refreshInstance({ preserveSim: true, silent: true });
    } else if (!prevRev && fp) {
      S.fp = fp;
    }
  } catch (_) {}
}
setInterval(pollLiveFingerprint, 1500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollLiveFingerprint(); });

loadAll();
</script></body></html>
"""


def merged_display(registry: CommandCenterRegistry | None = None) -> dict[str, Any]:
    """基础展示词条 + 每个蓝图自己声明的 ``spec.display``。

    内核的 ``ui_display.json`` 只保存跨蓝图通用的词条；
    任何业务本体自己的中文名都写在它自己的 ``spec.display`` 里，
    因此新增业务本体**不需要修改内核文件**。
    """
    merged = copy.deepcopy(DISPLAY)
    if registry is None:
        return merged
    for entry in registry.ontologies.values():
        overlay = getattr(entry.spec, "display", None) or {}
        for group, table in overlay.items():
            if not isinstance(table, dict):
                continue
            target = merged.setdefault(group, {})
            if not isinstance(target, dict):
                continue
            for key, value in table.items():
                # 内核通用词条优先，蓝图只补充缺口，避免互相覆盖。
                target.setdefault(key, value)
    return merged


def _docs_catalog_payload(
    registry: CommandCenterRegistry | None = None,
) -> list[dict[str, str]]:
    entries = (
        registry.doc_entries()
        if registry is not None and hasattr(registry, "doc_entries")
        else dict(_DEFAULT_DOC_FILES)
    )
    items: list[dict[str, str]] = []
    for k, (title, rel_path, tab_label) in entries.items():
        items.append(
            {
                "key": k,
                "title": title,
                "path": rel_path,
                "basename": Path(rel_path).name,
                "tab_label": tab_label,
            }
        )
    return items


def _render_ui_page(
    template: str,
    title: str,
    registry: CommandCenterRegistry | None = None,
) -> str:
    """注入共享中文展示助手、拓扑渲染器与页面标题。

    展示映射与渲染脚本都是**纯展示层**资源，不会改写任何 API 标识、
    角色 value 或动作参数。展示词条来自「内核通用词条 + 各蓝图
    ``spec.display``」的动态合并结果。
    """
    bundle = DISPLAY_JS.replace(
        "__DISPLAY_JSON__",
        json.dumps(merged_display(registry), ensure_ascii=False),
    )
    return (
        template.replace("__UI_BUNDLE__", bundle)
        .replace("__TOPOLOGY_JS__", TOPOLOGY_JS)
        .replace(
            "__DOCS_CATALOG_JSON__",
            json.dumps(_docs_catalog_payload(registry), ensure_ascii=False),
        )
        .replace("__TITLE__", title)
    )


def _routing_instance_id(
    body: dict[str, Any] | None,
    query: dict[str, list[str]] | None,
) -> str | None:
    """解析工作空间身份，兼容新旧别名 ``workspace`` / ``instance``。

    两个名字指向同一份注册条目。若调用方同时给出**不同**的值，说明意图
    不明确：这里直接以 :class:`OpenOntologyError`（HTTP 400）拒绝，绝不
    静默挑一个工作空间。相同值重复给出属于合法迁移期写法。
    """
    candidates: list[str] = []
    if body:
        for key in ("instance", "workspace"):
            value = body.get(key)
            if value:
                candidates.append(str(value))
    if query:
        for key in ("instance", "workspace"):
            for value in query.get(key, ()):
                if value:
                    candidates.append(str(value))
    distinct = sorted(set(candidates))
    if len(distinct) > 1:
        raise OpenOntologyError(
            "conflicting 'instance' and 'workspace' parameters: "
            + ", ".join(distinct)
        )
    return distinct[0] if distinct else None


def _topology_model_id(query: dict[str, list[str]]) -> str | None:
    """解析拓扑页的模型参数；``model`` 是 ``ontology`` 的等价别名。"""
    candidates: list[str] = []
    for key in ("ontology", "model"):
        for value in query.get(key, ()):
            if value:
                candidates.append(str(value))
    distinct = sorted(set(candidates))
    if len(distinct) > 1:
        raise OpenOntologyError(
            "conflicting 'ontology' and 'model' parameters: "
            + ", ".join(distinct)
        )
    return distinct[0] if distinct else None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _WorkshopHandler(BaseHTTPRequestHandler):
    server_version = "OpenOntologyCommandCenter/0.5"
    protocol_version = "HTTP/1.1"

    # -- server accessors --------------------------------------------------
    @property
    def _registry(self) -> CommandCenterRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    @property
    def _lock(self) -> threading.RLock:
        return self.server.runtime_lock  # type: ignore[attr-defined]

    @property
    def _title(self) -> str:
        return self.server.title  # type: ignore[attr-defined]

    def _instance_entry(self, body: dict[str, Any] | None, query: dict[str, list[str]] | None) -> OntologyInstanceEntry:
        instance_id = _routing_instance_id(body, query)
        if not instance_id:
            return self._registry.default_instance()
        return self._registry.get_instance(instance_id)

    def _runtime(self, body: dict[str, Any] | None = None, query: dict[str, list[str]] | None = None) -> OntologyRuntime:
        return self._instance_entry(body, query).runtime

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(
        self, status: int, message: str, *, error_code: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"ok": False, "error": message}
        if error_code:
            payload["error_code"] = error_code
        self._send_json(status, payload)

    def _resolve_request_actor(
        self,
        entry: OntologyInstanceEntry | None = None,
        *,
        role: str | None = None,
        body: dict[str, Any] | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> tuple[Actor, bool]:
        """Resolve caller Actor from Bearer token or explicit actor headers/params.

        Returns ``(actor, from_bearer_token)``. When an ``Authorization: Bearer``
        header is present, the token's bound Actor cannot be overridden by query
        parameters or request headers.
        """
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if not token:
                raise _UnauthorizedError("empty bearer token")
            ws_id = entry.instance_id if entry is not None else None
            return self._registry.resolve_actor_token(token, workspace_id=ws_id), True

        q = query or {}
        b = body or {}
        hdr_id = (self.headers.get("X-Actor-Id") or "").strip() or None
        q_id = (q.get("actor_id", [None])[0] or "").strip() or None
        b_id = (str(b.get("actor_id")).strip() if b.get("actor_id") else None)
        explicit_id = hdr_id or b_id or q_id

        hdr_role = (self.headers.get("X-Actor-Role") or "").strip() or None
        q_role = (q.get("role", [None])[0] or "").strip() or None
        b_role = (str(b.get("role")).strip() if b.get("role") else None)
        eff_role = role or b_role or hdr_role or q_role

        hdr_tags = (self.headers.get("X-Clearance-Tags") or "").strip()
        tags = (
            {t.strip() for t in hdr_tags.split(",") if t.strip()}
            if hdr_tags
            else {"*"}
        )

        if eff_role or explicit_id:
            if not eff_role:
                if entry is not None and entry.default_actor is not None:
                    eff_role = entry.default_actor.role
                else:
                    eff_role = self.server.default_actor.role  # type: ignore[attr-defined]
            actor_id = explicit_id or f"workshop:{eff_role}"
            return Actor(actor_id=actor_id, role=eff_role, clearance_tags=tags), False

        if entry is not None and entry.default_actor is not None:
            return entry.default_actor, False
        default_role = self.server.default_actor.role  # type: ignore[attr-defined]
        return Actor(actor_id=f"workshop:{default_role}", role=default_role, clearance_tags=tags), False

    def _actor(
        self, role: str | None = None, entry: OntologyInstanceEntry | None = None
    ) -> Actor:
        actor, _ = self._resolve_request_actor(entry, role=role)
        return actor

    def _base_url(self) -> str:
        host = (self.headers.get("Host") or "").strip()
        return f"http://{host}" if host else ""

    @staticmethod
    def _visible_links(runtime: OntologyRuntime, actor: Actor) -> list[dict[str, Any]]:
        rows = runtime.connection.execute(
            """
            SELECT link_type, source_type, source_id, target_type, target_id
            FROM ontology_links ORDER BY link_type, source_id, target_id
            """
        ).fetchall()
        visible = {
            (row["object_type"], row["object_id"])
            for row in runtime.connection.execute(
                "SELECT object_type, object_id, security_tags_json "
                "FROM ontology_objects"
            ).fetchall()
            if actor.has_access(json.loads(row["security_tags_json"]))
        }
        # 任一端的对象缺失（悬空边）或被 RLS 隐藏，整条边都不出口。
        return [
            dict(row)
            for row in rows
            if (row["source_type"], row["source_id"]) in visible
            and (row["target_type"], row["target_id"]) in visible
        ]

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    # -- meta / catalog ----------------------------------------------------
    def _meta(self, actor: Actor, runtime: OntologyRuntime, entry: OntologyInstanceEntry) -> dict[str, Any]:
        payload = _spec_meta(runtime.spec, [entry])
        payload.update(
            {
                "ok": True,
                "actor": {"actor_id": actor.actor_id, "role": actor.role},
                "instance": _instance_summary(entry, self._registry),
                "counts": {
                    "objects": runtime.count_objects(),
                    "runs": len(runtime.list_action_runs()),
                },
            }
        )
        return payload

    def _catalog(self) -> dict[str, Any]:
        ontologies = []
        for entry in self._registry.ontologies.values():
            instances = self._registry.instances_for(entry.ontology_id)
            meta = _spec_meta(entry.spec, instances)
            instance_ids = [item.instance_id for item in instances]
            ontologies.append(
                {
                    "ontology_id": entry.ontology_id,
                    "name": entry.name,
                    "category": entry.category,
                    "description": entry.description,
                    "roles": meta["roles"],
                    "counts": {
                        "object_types": len(meta["object_types"]),
                        "interfaces": len(meta["interfaces"]),
                        "object_properties": len(meta["object_properties"]),
                        "functions": len(meta["functions"]),
                        "actions": len(meta["actions"]),
                        "automations": len(meta["automations"]),
                        "instances": len(instance_ids),
                    },
                    "instances": instance_ids,
                }
            )
        instances = [
            _instance_summary(entry, self._registry)
            for entry in self._registry.instances.values()
        ]
        return {
            "ok": True,
            # ``models`` / ``workspaces`` 是 ``ontologies`` / ``instances`` 的
            # 新名字；两套键同时返回，旧客户端无需改动。
            "ontologies": ontologies,
            "models": ontologies,
            "instances": instances,
            "workspaces": instances,
            "runtimes": [asdict(item) for item in self._registry.runtimes.values()],
            "devices": {
                "symbolic": [
                    asdict(item) for item in self._registry.symbolic_devices.values()
                ],
            },
        }

    def _ontology_detail(self, ontology_id: str) -> dict[str, Any]:
        entry = self._registry.get_ontology(ontology_id)
        payload = _spec_meta(
            entry.spec, self._registry.instances_for(entry.ontology_id)
        )
        model_info = {
            "ontology_id": entry.ontology_id,
            "model_id": entry.ontology_id,
            "name": entry.name,
            "category": entry.category,
            "description": entry.description,
        }
        workspaces = [
            _instance_summary(item, self._registry)
            for item in self._registry.instances_for(entry.ontology_id)
        ]
        payload.update(
            {
                "ok": True,
                "ontology": model_info,
                "model": model_info,
                "counts": {
                    "object_types": len(payload["object_types"]),
                    "interfaces": len(payload["interfaces"]),
                    "object_properties": len(payload["object_properties"]),
                    "functions": len(payload["functions"]),
                    "actions": len(payload["actions"]),
                    "automations": len(payload["automations"]),
                    "roles": len(payload["role_defs"]),
                    "instances": len(workspaces),
                    "workspaces": len(workspaces),
                },
                "instances": workspaces,
                "workspaces": workspaces,
            }
        )
        return payload

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        role = query.get("role", [None])[0]

        if path == "/":
            self._send(
                200,
                _render_ui_page(
                    _WORKSHOP_HTML, self._title, self._registry
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        if path == "/topology":
            # 独立、可刷新的矢量关系图页面：不读取也不回显模型查询参数，
            # 前端再按 ?ontology=<编号>（或新别名 ?model=<编号>）请求
            # /api/ontology/<编号>。冲突的新旧参数在这里直接拒绝。
            try:
                _topology_model_id(query)
            except OpenOntologyError as exc:
                self._error(400, str(exc), error_code=getattr(exc, "code", None))
                return
            self._send(
                200,
                _render_ui_page(
                    TOPOLOGY_PAGE_HTML, self._title, self._registry
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        if path == "/docs":
            self._send(
                200,
                _render_ui_page(
                    DOCS_PAGE_HTML, self._title, self._registry
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return

        with self._lock:
            try:
                if path == "/api/health":
                    self._send_json(200, {"ok": True})
                    return
                if path == "/api/docs":
                    doc_key = (
                        query.get("doc", ["architecture"])[0] or "architecture"
                    ).strip()
                    entries = self._registry.doc_entries()
                    doc_entry = entries.get(doc_key)
                    if doc_entry is None:
                        self._error(404, f"unknown doc '{doc_key}'")
                        return
                    title, rel_path, _tab = doc_entry
                    doc_path = _REPO_ROOT / rel_path
                    if not doc_path.is_file():
                        self._error(404, f"doc file missing: {rel_path}")
                        return
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "doc": doc_key,
                            "title": title,
                            "path": rel_path,
                            "content": doc_path.read_text(encoding="utf-8"),
                            "docs": [
                                {"key": k, "title": t, "path": p}
                                for k, (t, p, _lbl) in entries.items()
                            ],
                        },
                    )
                    return
                if path == "/api/catalog":
                    self._send_json(200, self._catalog())
                    return
                if path == "/api/runtimes":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "runtimes": [
                                asdict(item)
                                for item in self._registry.runtimes.values()
                            ],
                        },
                    )
                    return

                if path == "/api/adapters":
                    meta_admin = getattr(self.server, "meta_admin", None)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "adapters": meta_admin.list_adapters() if meta_admin else [],
                        },
                    )
                    return
                if path == "/api/devices":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "symbolic": [
                                asdict(item)
                                for item in self._registry.symbolic_devices.values()
                            ],
                        },
                    )
                    return

                if path == "/api/workspaces":
                    actor = self._actor(role, None)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "workspaces": self._registry.list_workspaces(actor),
                        },
                    )
                    return

                if path.startswith("/api/ontology/"):
                    ontology_id = path[len("/api/ontology/"):]
                    self._send_json(200, self._ontology_detail(ontology_id))
                    return

                if path.startswith("/api/model/"):
                    model_id = path[len("/api/model/"):]
                    self._send_json(200, self._ontology_detail(model_id))
                    return

                if path.startswith("/api/worlds/"):
                    self._handle_world_get(path, query)
                    return

                entry = self._instance_entry(None, query)
                runtime = entry.runtime
                actor, _ = self._resolve_request_actor(entry, role=role, query=query)

                if path == "/api/meta":
                    self._send_json(200, self._meta(actor, runtime, entry))
                    return
                if path == "/api/fingerprint":
                    fp, revision = _compute_live_revision(runtime)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            **fp,
                            "revision": revision,
                        },
                    )
                    return
                if path == "/api/world-health":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            "workspace": entry.instance_id,
                            **runtime.inspect_health(),
                        },
                    )
                    return
                if path in ("/api/action-runs", "/api/runs"):
                    status = query.get("status", [None])[0]
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            "runs": runtime.list_action_runs(status),
                        },
                    )
                    return
                if path == "/api/simulations":
                    self._send_json(200, self._list_simulations_payload(entry, query))
                    return
                if path == "/api/automations/history":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "history": runtime.automation_history(),
                        },
                    )
                    return
                if path == "/api/links":
                    links = self._visible_links(runtime, actor)
                    self._send_json(
                        200,
                        {"ok": True, "count": len(links), "links": links},
                    )
                    return
                if path == "/api/interface":
                    name = query.get("interface", [None])[0]
                    if not name:
                        raise ValueError("interface query parameter is required")
                    objects = runtime.query_interface(name, actor)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "interface": name,
                            "count": len(objects),
                            "objects": [o.model_dump(mode="json") for o in objects],
                        },
                    )
                    return

                if path == "/api/storage-planes":
                    object_type = query.get("object_type", [None])[0]
                    planes = runtime.inspect_storage_planes(
                        actor, object_type=object_type
                    )
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            "workspace": entry.instance_id,
                            "actor": {"actor_id": actor.actor_id, "role": actor.role},
                            "count": len(planes),
                            "planes": planes,
                        },
                    )
                    return

                prefix = "/api/objects/"
                if path.startswith(prefix):
                    object_type = path[len(prefix):]
                    if not object_type or "/" in object_type:
                        self._error(404, "object type required")
                        return
                    cls = runtime.spec.get_object_type(object_type)
                    objects = runtime.query_objects(cls, actor)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "object_type": cls.__name__,
                            "count": len(objects),
                            "objects": [o.model_dump(mode="json") for o in objects],
                        },
                    )
                    return

                self._error(404, "not found")
            except _UnauthorizedError as exc:
                self._error(401, str(exc))
            except PermissionDeniedError as exc:
                self._error(403, str(exc))
            except (
                KeyError,
                ObjectNotFoundError,
                UnknownObjectTypeError,
                ActionNotFoundError,
                FunctionNotFoundError,
            ) as exc:
                self._error(404, str(exc))
            except OpenOntologyError as exc:
                self._error(400, str(exc), error_code=getattr(exc, "code", None))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        role = query.get("role", [None])[0]

        try:
            body = self._read_json()
        except ValueError as exc:
            self._error(400, str(exc))
            return

        with self._lock:
            try:
                if path.startswith("/api/worlds/"):
                    self._handle_world_post(path, query, body)
                    return
                entry = self._instance_entry(body, query)
                actor, _ = self._resolve_request_actor(
                    entry, role=body.get("role") or role, body=body, query=query
                )
                self._dispatch_post(path, body, actor, entry)
            except _UnauthorizedError as exc:
                self._error(401, str(exc))
            except PermissionDeniedError as exc:
                self._error(403, str(exc))
            except (
                KeyError,
                ObjectNotFoundError,
                UnknownObjectTypeError,
                ActionNotFoundError,
                FunctionNotFoundError,
            ) as exc:
                self._error(404, str(exc))
            except OpenOntologyError as exc:
                self._error(400, str(exc), error_code=getattr(exc, "code", None))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    # -- /api/worlds/{world_id}/... (REST + OpenAPI 3.1) -------------------
    def _handle_world_get(
        self, path: str, query: dict[str, list[str]]
    ) -> None:
        parts = [unquote(p) for p in path[len("/api/worlds/"):].split("/") if p]
        if not parts:
            self._error(404, "world id required")
            return
        entry = self._registry.get_instance(parts[0])
        runtime = entry.runtime
        actor, from_token = self._resolve_request_actor(entry, query=query)

        if len(parts) == 1:
            self._send_json(
                200,
                {
                    "ok": True,
                    "world": _instance_summary(entry, self._registry),
                    "actor": {"actor_id": actor.actor_id, "role": actor.role},
                    "openapi_url": f"/api/worlds/{entry.instance_id}/openapi.json",
                    "fingerprint": runtime.state_fingerprint(),
                },
            )
            return

        sub = parts[1:]
        if sub == ["openapi.json"]:
            role_filter = (
                actor.role
                if from_token
                else (
                    (query.get("role", [None])[0] or "").strip()
                    or (self.headers.get("X-Actor-Role") or "").strip()
                    or None
                )
            )
            spec_doc = describe_openapi(
                runtime.spec,
                world_id=entry.instance_id,
                base_url=self._base_url(),
                role=role_filter,
                title=entry.label,
                description=entry.description,
            )
            self._send_json(200, spec_doc)
            return

        if sub == ["links"]:
            links = self._visible_links(runtime, actor)
            self._send_json(200, {"ok": True, "count": len(links), "links": links})
            return

        if sub == ["health"]:
            self._send_json(
                200,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    **runtime.inspect_health(),
                },
            )
            return

        if sub == ["action-runs"]:
            status = query.get("status", [None])[0]
            self._send_json(
                200,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    "runs": runtime.list_action_runs(status),
                },
            )
            return

        if len(sub) == 2 and sub[0] == "objects":
            cls = runtime.spec.get_object_type(sub[1])
            objects = runtime.query_objects(cls, actor)
            self._send_json(
                200,
                {
                    "ok": True,
                    "object_type": cls.__name__,
                    "count": len(objects),
                    "objects": [o.model_dump(mode="json") for o in objects],
                },
            )
            return

        if len(sub) == 3 and sub[0] == "objects":
            cls = runtime.spec.get_object_type(sub[1])
            obj = runtime.get_object(cls, sub[2], actor)
            self._send_json(
                200,
                {
                    "ok": True,
                    "object_type": cls.__name__,
                    "object": obj.model_dump(mode="json"),
                },
            )
            return

        if sub == ["simulations"]:
            self._send_json(200, self._list_simulations_payload(entry, query))
            return

        if sub == ["webhooks"]:
            self._require_webhook_admin(actor, entry)
            subs = [
                s.to_public_dict()
                for s in self._registry.webhooks.list_subscriptions(entry.instance_id)
            ]
            self._send_json(
                200,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    "count": len(subs),
                    "subscriptions": subs,
                },
            )
            return

        if len(sub) == 3 and sub[0] == "webhooks" and sub[2] == "deliveries":
            self._require_webhook_admin(actor, entry)
            rows = self._registry.webhooks.list_deliveries(
                sub[1], world_id=entry.instance_id
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    "sub_id": sub[1],
                    "count": len(rows),
                    "deliveries": rows,
                },
            )
            return

        self._error(404, "not found")

    @staticmethod
    def _require_webhook_admin(actor: Actor, entry: OntologyInstanceEntry) -> None:
        allowed = {"admin", "system"}
        if entry.default_actor is not None and entry.default_actor.role:
            allowed.add(entry.default_actor.role)
        if actor.role not in allowed:
            raise PermissionDeniedError(
                f"role {actor.role!r} is not allowed to manage webhooks "
                f"(allowed: {sorted(allowed)})"
            )

    def _handle_world_post(
        self, path: str, query: dict[str, list[str]], body: dict[str, Any]
    ) -> None:
        parts = [unquote(p) for p in path[len("/api/worlds/"):].split("/") if p]
        if len(parts) == 2 and parts[1] == "webhooks":
            entry = self._registry.get_instance(parts[0])
            actor, _ = self._resolve_request_actor(entry, query=query)
            self._require_webhook_admin(actor, entry)
            target_url = str(body.get("target_url") or "")
            raw_filter = body.get("filter")
            if raw_filter is not None and not isinstance(raw_filter, dict):
                raise ValueError("filter must be a JSON object")
            sub = self._registry.webhooks.register(
                world_id=entry.instance_id,
                target_url=target_url,
                filter_spec=WebhookFilter.from_mapping(raw_filter),
                secret=str(body.get("secret") or ""),
                description=str(body.get("description") or ""),
                sub_id=str(body["sub_id"]) if body.get("sub_id") else None,
                enabled=bool(body.get("enabled", True)),
                created_by=actor.actor_id,
            )
            self._send_json(
                201,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    "subscription": sub.to_public_dict(),
                },
            )
            return

        if len(parts) != 3 or parts[1] not in ("functions", "actions"):
            self._error(404, "not found")
            return

        world_id, kind, name = parts
        entry = self._registry.get_instance(world_id)
        runtime = entry.runtime
        actor, _ = self._resolve_request_actor(entry, query=query)

        if kind == "functions":
            runtime.spec.get_function(name)
            result = runtime.call_function(name, body, actor)
            self._send_json(
                200,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    "actor": {"actor_id": actor.actor_id, "role": actor.role},
                    "function": name,
                    "result": result,
                },
            )
            return

        action_def = runtime.spec.get_action(name)
        if action_def.visibility == "private":
            raise PermissionDeniedError(f"action {name!r} is private")
        dry_run = (
            str((query.get("dry_run") or [""])[0]).lower() in ("1", "true", "yes")
            or str(self.headers.get("X-Dry-Run") or "").lower() in ("1", "true", "yes")
        )
        if dry_run:
            res = self._simulate_and_record(
                entry,
                actor=actor,
                action_name=name,
                params=body,
                prior_steps=[],
                session_id=(query.get("session_id") or [None])[0],
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "world_id": entry.instance_id,
                    "dry_run": True,
                    **res,
                },
            )
            return

        run_id = (
            (query.get("run_id") or [None])[0]
            or (self.headers.get("X-Run-Id") or "").strip()
            or f"API-{len(runtime.list_action_runs()) + 1:04d}-{uuid.uuid4().hex[:6]}"
        )
        run = runtime.submit_action(run_id, name, body, actor)
        self._send_json(
            200,
            {
                "ok": True,
                "world_id": entry.instance_id,
                "actor": {"actor_id": actor.actor_id, "role": actor.role},
                "run": run,
            },
        )

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        with self._lock:
            try:
                if path.startswith("/api/worlds/"):
                    parts = [
                        unquote(p) for p in path[len("/api/worlds/"):].split("/") if p
                    ]
                    if len(parts) == 3 and parts[1] == "webhooks":
                        entry = self._registry.get_instance(parts[0])
                        actor, _ = self._resolve_request_actor(entry, query=query)
                        self._require_webhook_admin(actor, entry)
                        removed = self._registry.webhooks.delete_subscription(
                            parts[2], world_id=entry.instance_id
                        )
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "world_id": entry.instance_id,
                                "deleted": removed.to_public_dict(),
                            },
                        )
                        return
                    if parts[1:] == ["simulations"]:
                        entry = self._registry.get_instance(parts[0])
                        self._resolve_request_actor(entry, query=query)
                        deleted_count = self._registry.simulation_ledger.clear(entry)
                        self._send_json(
                            200,
                            {
                                "ok": True,
                                "world_id": entry.instance_id,
                                "deleted": deleted_count,
                            },
                        )
                        return
                if path == "/api/simulations":
                    entry = self._instance_entry(None, query)
                    self._resolve_request_actor(entry, query=query)
                    deleted_count = self._registry.simulation_ledger.clear(entry)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "instance": entry.instance_id,
                            "deleted": deleted_count,
                        },
                    )
                    return
                self._error(404, "not found")
            except _UnauthorizedError as exc:
                self._error(401, str(exc))
            except PermissionDeniedError as exc:
                self._error(403, str(exc))
            except (KeyError, ObjectNotFoundError) as exc:
                self._error(404, str(exc))
            except OpenOntologyError as exc:
                self._error(400, str(exc), error_code=getattr(exc, "code", None))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                self._error(500, str(exc))

    def _list_simulations_payload(
        self, entry: OntologyInstanceEntry, query: dict[str, list[str]]
    ) -> dict[str, Any]:
        valid_raw = (query.get("valid") or [None])[0]
        valid_filter: bool | None = None
        if valid_raw is not None:
            low = str(valid_raw).strip().lower()
            if low in ("1", "true", "yes"):
                valid_filter = True
            elif low in ("0", "false", "no"):
                valid_filter = False
        session_id = (query.get("session_id") or [None])[0]
        limit_raw = (query.get("limit") or ["100"])[0]
        rows = self._registry.simulation_ledger.list(
            entry,
            valid=valid_filter,
            session_id=session_id,
            limit=int(limit_raw or "100"),
        )
        return {
            "ok": True,
            "instance": entry.instance_id,
            "world_id": entry.instance_id,
            "count": len(rows),
            "simulations": rows,
        }

    def _simulate_and_record(
        self,
        entry: OntologyInstanceEntry,
        *,
        actor: Actor,
        action_name: str,
        params: dict[str, Any],
        prior_steps: list[dict[str, Any]],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = entry.runtime
        eff_session = (str(session_id or "").strip()) or f"SIM-{uuid.uuid4().hex[:10]}"
        step_index = len(prior_steps) + 1
        fp_hash = str(runtime.state_fingerprint().get("fingerprint", ""))

        try:
            if prior_steps:
                sim_fork = runtime.fork()
                try:
                    for idx, step in enumerate(prior_steps, start=1):
                        step_run_id = f"SIM-PRIOR-{idx}-{uuid.uuid4().hex[:6]}"
                        run = sim_fork.submit_action(
                            step_run_id,
                            step["action"],
                            step.get("params") or {},
                            actor,
                        )
                        if (run or {}).get("status") == "PENDING_APPROVAL":
                            sim_fork.approve_action(
                                step_run_id,
                                Actor(actor_id="sim-approver", role="system"),
                            )
                    sim = sim_fork.simulate_action(action_name, params, actor)
                finally:
                    sim_fork.close()
            else:
                sim = runtime.simulate_action(action_name, params, actor)
        except Exception as exc:
            self._registry.simulation_ledger.record(
                entry,
                session_id=eff_session,
                step_index=step_index,
                action_name=action_name,
                actor=actor,
                params=params,
                prior_steps=prior_steps,
                valid=False,
                requires_approval=False,
                error_code=getattr(exc, "code", None) or type(exc).__name__,
                error_message=str(exc),
                base_fingerprint=fp_hash,
            )
            raise

        sim_dict = sim.to_dict()
        meta = self._registry.simulation_ledger.record(
            entry,
            session_id=eff_session,
            step_index=step_index,
            action_name=action_name,
            actor=actor,
            params=params,
            prior_steps=prior_steps,
            valid=sim.valid,
            requires_approval=sim.requires_approval,
            error_code=sim.error_code,
            error_message=sim.error,
            diff=sim_dict.get("diff"),
            base_fingerprint=fp_hash,
        )
        return {**sim_dict, **meta}

    def _dispatch_post(
        self,
        path: str,
        body: dict[str, Any],
        actor: Actor,
        entry: OntologyInstanceEntry,
    ) -> None:
        runtime = entry.runtime
        is_platform = body.get("scope") == "platform"
        meta_admin = getattr(self.server, "meta_admin", None)
        if is_platform and meta_admin is not None:
            runtime = meta_admin.runtime

        if path == "/api/adapter/sync":
            if meta_admin is None:
                raise KeyError("meta_admin not configured")
            adapter_id = str(body.get("adapter_id") or "")
            if not adapter_id:
                raise ValueError("adapter_id is required")
            res = meta_admin.run_adapter_once(adapter_id)
            self._send_json(200, {"ok": True, **res})
            return

        if path == "/api/adapter/toggle":
            if meta_admin is None:
                raise KeyError("meta_admin not configured")
            adapter_id = str(body.get("adapter_id") or "")
            status = str(body.get("status") or "")
            if not adapter_id or not status:
                raise ValueError("adapter_id and status are required")
            res = meta_admin.toggle_adapter(adapter_id, status)
            self._send_json(200, {"ok": True, **res})
            return

        if path == "/api/adapter/webhook":
            if meta_admin is None:
                raise KeyError("meta_admin not configured")
            parsed_q = parse_qs(urlparse(self.path).query)
            adapter_id = str(
                (parsed_q.get("adapter_id") or [None])[0]
                or body.get("adapter_id")
                or ""
            )
            if not adapter_id:
                raise ValueError("adapter_id is required")
            res = meta_admin.ingest_adapter_webhook(adapter_id, body)
            self._send_json(200, {"ok": True, **res})
            return

        if path == "/api/simulate":
            prior_steps = body.get("prior_steps") or []
            res = self._simulate_and_record(
                entry,
                actor=actor,
                action_name=body["action"],
                params=body.get("params") or {},
                prior_steps=prior_steps,
                session_id=body.get("session_id"),
            )
            self._send_json(200, {"ok": True, **res})
            return

        if path == "/api/submit":
            steps = body.get("steps")
            if isinstance(steps, list) and steps:
                runs = []
                base_idx = len(runtime.list_action_runs()) + 1
                for offset, step in enumerate(steps):
                    run_id = step.get("run_id") or f"CC-{base_idx + offset:04d}-{uuid.uuid4().hex[:6]}"
                    run = runtime.submit_action(
                        run_id,
                        step["action"],
                        step.get("params") or {},
                        actor,
                    )
                    if is_platform and meta_admin is not None and run.get("status") == "COMMITTED":
                        meta_admin.sync_to_registry()
                    runs.append(run)
                self._send_json(200, {"ok": True, "run": runs[-1], "runs": runs})
                return
            run = runtime.submit_action(
                body.get("run_id") or f"UI-{len(runtime.list_action_runs()) + 1:04d}",
                body["action"],
                body.get("params") or {},
                actor,
            )
            sim_sess = str(body.get("simulation_session_id") or "").strip()
            if sim_sess and run.get("run_id"):
                self._registry.simulation_ledger.mark_first_step_committed(
                    entry, session_id=sim_sess, run_id=str(run["run_id"])
                )
            if is_platform and meta_admin is not None and run.get("status") == "COMMITTED":
                meta_admin.sync_to_registry()
            self._send_json(200, {"ok": True, "run": run})
            return

        if path == "/api/approve":
            run = runtime.approve_action(body["run_id"], actor)
            if is_platform and meta_admin is not None and run.get("status") == "COMMITTED":
                meta_admin.sync_to_registry()
            self._send_json(200, {"ok": True, "run": run})
            return

        if path == "/api/revert":
            revert_run_id = body.get("revert_run_id") or f"{body['run_id']}-REV"
            run = runtime.revert_action(body["run_id"], revert_run_id, actor)
            self._send_json(200, {"ok": True, "run": run})
            return

        if path == "/api/function":
            result = runtime.call_function(
                body["function"], body.get("params") or {}, actor
            )
            self._send_json(200, {"ok": True, "result": result})
            return

        if path == "/api/automations/run":
            fired = runtime.evaluate_automations(actor)
            self._send_json(
                200,
                {
                    "ok": True,
                    "fired": fired,
                    "count": len(fired),
                    "history": runtime.automation_history(),
                },
            )
            return

        if path == "/api/agent/ghost":
            event_type = str(body.get("event_type") or "").strip()
            if not event_type:
                raise ValueError("ghost event_type is required")
            ghost_key = f"{entry.instance_id}:{actor.actor_id}"
            store = getattr(self.server, "agent_ghost_events", None)
            if store is None:
                store = {}
                self.server.agent_ghost_events = store
            buf = store.setdefault(ghost_key, [])
            buf.append(
                {
                    "event_type": event_type,
                    "detail": dict(body.get("detail") or {}),
                }
            )
            if len(buf) > 20:
                del buf[:-20]
            self._send_json(200, {"ok": True, "count": len(buf)})
            return

        if path == "/api/agent/chat":
            msg = str(body.get("message") or "").strip()
            if not msg:
                raise ValueError("agent message must be a non-empty string")
            executor = AIPAgentExecutor(runtime, actor)
            llm_fn = getattr(self.server, "agent_llm_fn", None)
            scope_tag = "platform" if is_platform else entry.instance_id
            ghost_key = f"{scope_tag}:{actor.actor_id}"
            stored_ghosts = list(
                (getattr(self.server, "agent_ghost_events", None) or {}).get(ghost_key) or []
            )
            req_ghosts = list(body.get("ghost_events") or [])
            merged_ghosts = (stored_ghosts + req_ghosts)[-20:]

            accept = str(self.headers.get("Accept") or "")
            want_stream = bool(body.get("stream")) or ("text/event-stream" in accept)
            if want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for ev in iter_agent_turn(
                    executor,
                    msg,
                    history=body.get("history"),
                    ui_context=body.get("ui_context"),
                    ghost_events=merged_ghosts,
                    llm_fn=llm_fn,
                ):
                    frame = (
                        f"event: {ev['event']}\n"
                        f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
                    ).encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()
                self.close_connection = True
                return

            turn = run_agent_turn(
                executor,
                msg,
                history=body.get("history"),
                ui_context=body.get("ui_context"),
                ghost_events=merged_ghosts,
                llm_fn=llm_fn,
            )
            self._send_json(200, {"ok": True, **turn.to_dict()})
            return

        if path == "/api/snapshot":
            objects = body.get("objects")
            if not isinstance(objects, dict):
                raise ValueError("snapshot.objects must be a JSON object")
            summary = runtime.load_snapshot(
                objects=objects,
                links=body.get("links"),
                conflict_strategy=str(
                    body.get("conflict_strategy", "user_edits_win")
                ),
                source_timestamps={
                    tuple(key.split(":", 1)): value
                    for key, value in (body.get("source_timestamps") or {}).items()
                }
                or None,
                actor=actor,
            )
            self._send_json(200, {"ok": True, **summary})
            return

        if path in ("/api/instance/fork", "/api/workspace/fork"):
            forked = self._registry.fork_instance(
                body.get("workspace") or body.get("instance") or entry.instance_id,
                body.get("new_workspace_id") or body.get("new_instance_id"),
                body.get("label"),
            )
            summary = _instance_summary(forked, self._registry)
            self._send_json(
                200,
                {
                    "ok": True,
                    "instance": summary,
                    "workspace": summary,
                },
            )
            return

        if path in ("/api/instance/mcts", "/api/workspace/mcts"):
            self._run_mcts(body, actor, entry)
            return

        if path in ("/api/instance/unplug", "/api/workspace/unplug"):
            self._unplug(body, entry)
            return

        self._error(404, "not found")

    # -- advanced instance operations --------------------------------------
    def _run_mcts(
        self, body: dict[str, Any], actor: Actor, entry: OntologyInstanceEntry
    ) -> None:
        config = dict(entry.mcts_config or {})
        if not config:
            raise ValueError(f"instance {entry.instance_id!r} has no MCTS config")
        if body.get("num_simulations") is not None:
            config["simulations"] = int(body["num_simulations"])
        if body.get("simulations") is not None:
            config["simulations"] = int(body["simulations"])
        for key in ("c_puct", "seed", "value_scale", "max_steps"):
            if body.get(key) is not None:
                config[key] = body[key]

        # 规划与提交都使用实例预设的默认角色（各蓝图角色体系不同）。
        planner_actor = entry.default_actor or actor
        report = entry.runtime.plan_mcts(planner_actor, **config)
        payload = {
            "ok": True,
            "instance": entry.instance_id,
            "root_stats": [
                {
                    "label": stat.label,
                    "action": stat.action,
                    "prior": stat.prior,
                    "visits": stat.visits,
                    "mean_value": stat.mean_value,
                }
                for stat in report.root_actions
            ],
            "best_sequence": report.best_plan_labels,
            "best_value": report.best_value,
            "pruned_branches": list(report.pruned_errors),
            "pruned_illegal_nodes": report.pruned_illegal_nodes,
            "simulations": report.simulations,
            "forks_created": report.forks_created,
            "sandbox_writes": report.sandbox_writes,
            "report": report.to_dict(),
        }
        if body.get("commit"):
            committed: list[str] = []
            for index, candidate in enumerate(report.best_plan, start=1):
                run_id = f"CC-MCTS-{entry.instance_id}-{index}-{uuid.uuid4().hex[:8]}"
                entry.runtime.submit_action(
                    run_id, candidate.action, candidate.params, planner_actor
                )
                committed.append(run_id)
            payload["committed_runs"] = committed
        elif report.best_plan:
            sim_fork = entry.runtime.fork()
            try:
                obj_changes: list[dict[str, Any]] = []
                link_changes: list[dict[str, Any]] = []
                for index, candidate in enumerate(report.best_plan, start=1):
                    run = sim_fork.submit_action(
                        f"MCTS-SIM-{index}",
                        candidate.action,
                        candidate.params,
                        planner_actor,
                    )
                    diff_dict = (run or {}).get("object_diff") or {}
                    obj_changes.extend(diff_dict.get("object_changes") or [])
                    link_changes.extend(diff_dict.get("link_changes") or [])
                payload["diff"] = {
                    "object_changes": obj_changes,
                    "link_changes": link_changes,
                }
            finally:
                sim_fork.close()
        self._send_json(200, payload)

    def _unplug(self, body: dict[str, Any], entry: OntologyInstanceEntry) -> None:
        if entry.unplug_fn is None:
            raise ValueError(f"instance {entry.instance_id!r} does not support unplug")
        result = entry.unplug_fn()
        if entry.runtime.world_mode == "coupled":
            entry.runtime.bind_source_bridge(None, world_mode="standalone")
        for connector in entry.connectors:
            connector.status = "UNPLUGGED"
        self._send_json(
            200,
            {
                "ok": True,
                "instance": entry.instance_id,
                "unplugged": [connector.name for connector in entry.connectors],
                "connectors": [asdict(connector) for connector in entry.connectors],
                "result": result,
            },
        )

    # -- noise control -----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        return


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------
class WorkshopServer(ThreadingHTTPServer):
    """线程化指挥中心服务器：一个注册表 + 一把请求锁。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        runtime: OntologyRuntime | None = None,
        *,
        registry: CommandCenterRegistry | None = None,
        actor: Actor | None = None,
        title: str = "OpenOntology Workshop",
        agent_llm_fn: Callable[..., Any] | None = None,
    ) -> None:
        if registry is None:
            if runtime is None:
                raise TypeError(
                    "WorkshopServer requires either a runtime or a registry"
                )
            registry = CommandCenterRegistry.from_runtime(
                runtime, actor=actor, title=title
            )
        self.registry = registry
        # 向后兼容属性：始终指向默认实例的 runtime。
        self.runtime = registry.default_instance().runtime
        self.runtime_lock = threading.RLock()
        self.title = title
        self.agent_llm_fn = agent_llm_fn
        self.agent_ghost_events: dict[str, list[dict[str, Any]]] = {}
        self.meta_admin = MetaAdminService(self.registry)
        self.default_actor = actor or Actor(
            actor_id="workshop:admin", role="admin", clearance_tags={"*"}
        )
        self.meta_admin.start_adapter_loop()
        super().__init__(server_address, _WorkshopHandler)

    def server_close(self) -> None:
        self.meta_admin.stop()
        super().server_close()


@dataclass
class WorkshopHandle:
    server: WorkshopServer
    thread: threading.Thread
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)


def _serve(server: WorkshopServer, host: str, port: int) -> WorkshopHandle:
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"open-ontology-workshop-{actual_port}",
        daemon=True,
    )
    thread.start()
    return WorkshopHandle(
        server=server,
        thread=thread,
        host=str(actual_host),
        port=int(actual_port),
    )


def start_workshop_server(
    runtime: OntologyRuntime | CommandCenterRegistry,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    actor: Actor | None = None,
    title: str = "OpenOntology Workshop",
    registry: CommandCenterRegistry | None = None,
) -> WorkshopHandle:
    """启动作战台。

    ``runtime`` 可以传单个 :class:`OntologyRuntime`（旧签名），也可以直接传
    :class:`CommandCenterRegistry`（新用法），或使用 ``registry=`` 关键字。
    """
    if isinstance(runtime, CommandCenterRegistry):
        registry = runtime
        runtime = None
    server = WorkshopServer(
        (host, port), runtime, registry=registry, actor=actor, title=title
    )
    return _serve(server, host, port)


def start_command_center_server(
    registry: CommandCenterRegistry,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    actor: Actor | None = None,
    title: str = "OpenOntology Command Center",
) -> WorkshopHandle:
    """多蓝图 / 多实例指挥中心的启动入口。"""
    server = WorkshopServer(
        (host, port), registry=registry, actor=actor, title=title
    )
    return _serve(server, host, port)
