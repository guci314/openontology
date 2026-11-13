"""工作空间身份清单（C-WS-02）。

本模块只持久化**身份四元组**：``workspace_id`` / ``model_id`` / ``runtime_id`` /
``db_path``。它不序列化 runtime 对象、actor、可调用对象、凭据或任意 import
字符串；模型只能由调用方显式提供的可信注册表解析，清单里的名字绝不会触发
``importlib``。

清单固定落在 ``<data_dir>/workspaces.json``，带显式 ``format_version``；
写入走「同目录临时文件 + ``os.replace``」的原子替换，畸形 / 不支持版本的
清单只报错、不覆盖原文件。读取与解析都是只读的：不会创建缺失目录或数据库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "KERNEL_TABLES",
    "MANIFEST_FILENAME",
    "MANIFEST_FORMAT_VERSION",
    "AmbiguousTakeoverError",
    "IncompleteKernelDatabaseError",
    "ResolvedWorkspace",
    "WorkspaceCatalog",
    "WorkspaceCatalogError",
    "WorkspaceConflictError",
    "WorkspaceDatabaseMissingError",
    "WorkspaceDescriptor",
    "WorkspaceManifestError",
    "WorkspaceModelUnavailableError",
    "WorkspaceNotFoundError",
    "UnsupportedWorkspaceManifestError",
    "check_kernel_database",
    "load_catalog",
    "takeover_workspace",
]

#: 清单文件名（与看板库同目录）。
MANIFEST_FILENAME = "workspaces.json"

#: 清单格式版本。新增不兼容字段必须提升版本，旧读取器应明确拒绝。
MANIFEST_FORMAT_VERSION = 1

#: 内核库必备的六张表；缺任何一张都说明库不完整或不是本项目的库。
KERNEL_TABLES: tuple[str, ...] = (
    "ontology_write_gate",
    "ontology_registry",
    "ontology_objects",
    "ontology_links",
    "ontology_action_runs",
    "ontology_overlay",
)

_DESCRIPTOR_FIELDS = ("workspace_id", "model_id", "runtime_id", "db_path")


class WorkspaceCatalogError(ValueError):
    """工作空间清单相关错误的基类。"""


class WorkspaceManifestError(WorkspaceCatalogError):
    """清单结构畸形（JSON / 字段 / 重复 ID）。"""


class UnsupportedWorkspaceManifestError(WorkspaceManifestError):
    """清单格式版本不受支持。"""


class WorkspaceConflictError(WorkspaceCatalogError):
    """同一 ``workspace_id`` 以不同身份重复注册。"""


class WorkspaceNotFoundError(LookupError):
    """清单里没有这个工作空间。"""


class WorkspaceModelUnavailableError(LookupError):
    """可信模型注册表里没有清单引用的模型。"""


class WorkspaceDatabaseMissingError(FileNotFoundError):
    """工作空间指向的数据库不存在（解析绝不创建它）。"""


class IncompleteKernelDatabaseError(WorkspaceCatalogError):
    """数据库缺少内核表或无法按只读方式解析（绝不补齐、绝不重建）。"""


class AmbiguousTakeoverError(LookupError):
    """多个候选同时命中：结构化列出全部候选，绝不 first-match。

    ``candidates`` 里每项至少含 ``db_path`` / ``board_key`` / ``workspace_id``，
    调用方可以据此要求用户显式指定工作空间或目标对象。
    """

    def __init__(
        self, message: str, candidates: Iterable[Mapping[str, Any]]
    ) -> None:
        super().__init__(message)
        self.candidates: tuple[dict[str, Any], ...] = tuple(
            dict(item) for item in candidates
        )


@dataclass(frozen=True)
class WorkspaceDescriptor:
    """可持久化的工作空间身份；字段之外一律不落盘。"""

    workspace_id: str
    model_id: str
    runtime_id: str
    db_path: str

    def __post_init__(self) -> None:
        for name in ("workspace_id", "model_id", "runtime_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
            if "\x00" in value:
                raise ValueError(f"{name} must not contain NUL")

        db_path = self.db_path
        if isinstance(db_path, os.PathLike):
            db_path = os.fspath(db_path)
        if not isinstance(db_path, str):
            raise ValueError("db_path must be a string or path-like")
        if not db_path.strip():
            raise ValueError("db_path must not be blank")
        if "\x00" in db_path:
            raise ValueError("db_path must not contain NUL")
        if db_path.strip() == ":memory:":
            raise ValueError(
                "a persisted workspace descriptor cannot use an in-memory database"
            )
        object.__setattr__(self, "db_path", db_path)

    # -- projection --------------------------------------------------------
    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _DESCRIPTOR_FIELDS}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceDescriptor":
        if not isinstance(payload, Mapping):
            raise WorkspaceManifestError("workspace descriptor must be an object")
        keys = set(payload)
        expected = set(_DESCRIPTOR_FIELDS)
        if keys != expected:
            missing = ", ".join(sorted(expected - keys)) or "-"
            extra = ", ".join(sorted(keys - expected)) or "-"
            raise WorkspaceManifestError(
                f"workspace descriptor fields mismatch (missing: {missing}; extra: {extra})"
            )
        values = {name: payload[name] for name in _DESCRIPTOR_FIELDS}
        for name, value in values.items():
            if not isinstance(value, str):
                raise WorkspaceManifestError(f"{name} must be a string")
        return cls(**values)

    def resolved_db_path(self, manifest_dir: str | Path) -> Path:
        """把 ``db_path`` 解析成绝对路径；相对路径以清单所在目录为基准。"""
        path = Path(self.db_path)
        if not path.is_absolute():
            path = Path(manifest_dir) / path
        return path.resolve()


@dataclass(frozen=True)
class ResolvedWorkspace:
    """清单条目 + 可信模型 + 已解析的数据库路径（纯读结果）。"""

    descriptor: WorkspaceDescriptor
    model: Any
    db_path: Path


class WorkspaceCatalog:
    """``workspaces.json`` 的读写门面。

    ``load`` / ``get`` / ``descriptors`` / ``resolve`` 都是只读的；只有
    ``register`` / ``save`` 会写文件，且写入是原子的。
    """

    def __init__(
        self,
        manifest_path: str | Path,
        descriptors: Iterable[WorkspaceDescriptor] = (),
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self._descriptors: dict[str, WorkspaceDescriptor] = {}
        for descriptor in descriptors:
            self._add(descriptor)

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, manifest_path: str | Path) -> "WorkspaceCatalog":
        path = Path(manifest_path)
        catalog = cls(path)
        if not path.exists():
            return catalog

        raw = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkspaceManifestError(
                f"{path} is not valid JSON workspace manifest: {exc.msg}"
            ) from exc

        if not isinstance(payload, dict):
            raise WorkspaceManifestError(f"{path} manifest must be a JSON object")
        unknown = set(payload) - {"format_version", "workspaces"}
        if unknown:
            raise WorkspaceManifestError(
                f"{path} manifest has unsupported fields: {', '.join(sorted(unknown))}"
            )
        if "format_version" not in payload:
            raise WorkspaceManifestError(f"{path} manifest is missing format_version")
        version = payload["format_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise WorkspaceManifestError(
                f"{path} manifest format_version must be an integer"
            )
        if version != MANIFEST_FORMAT_VERSION:
            raise UnsupportedWorkspaceManifestError(
                f"{path} manifest format_version {version} is not supported "
                f"(expected {MANIFEST_FORMAT_VERSION})"
            )

        workspaces = payload.get("workspaces")
        if not isinstance(workspaces, list):
            raise WorkspaceManifestError(f"{path} manifest workspaces must be a list")
        for item in workspaces:
            try:
                catalog._add(WorkspaceDescriptor.from_dict(item))
            except ValueError as exc:
                raise WorkspaceManifestError(
                    f"{path} manifest contains an invalid workspace: {exc}"
                ) from exc
        return catalog

    # -- reads -------------------------------------------------------------
    @property
    def manifest_dir(self) -> Path:
        return self.manifest_path.parent

    def descriptors(self) -> tuple[WorkspaceDescriptor, ...]:
        return tuple(
            self._descriptors[key] for key in sorted(self._descriptors)
        )

    def __len__(self) -> int:
        return len(self._descriptors)

    def __contains__(self, workspace_id: object) -> bool:
        return workspace_id in self._descriptors

    def get(self, workspace_id: str) -> WorkspaceDescriptor:
        try:
            return self._descriptors[workspace_id]
        except KeyError as exc:
            raise WorkspaceNotFoundError(
                f"unknown workspace {workspace_id!r}"
            ) from exc

    def resolved_db_path(self, workspace_id: str) -> Path:
        return self.get(workspace_id).resolved_db_path(self.manifest_dir)

    def resolve(
        self,
        workspace_id: str,
        models: Mapping[str, Any],
    ) -> ResolvedWorkspace:
        """用显式可信模型注册表解析工作空间；不 import、不建库、不建目录。"""
        descriptor = self.get(workspace_id)
        if descriptor.model_id not in models:
            raise WorkspaceModelUnavailableError(
                f"workspace {workspace_id!r} references unavailable model "
                f"{descriptor.model_id!r}"
            )
        model = models[descriptor.model_id]
        db_path = descriptor.resolved_db_path(self.manifest_dir)
        if not db_path.is_file():
            raise WorkspaceDatabaseMissingError(
                f"workspace {workspace_id!r} database is missing: {db_path}"
            )
        return ResolvedWorkspace(
            descriptor=descriptor,
            model=getattr(model, "spec", model),
            db_path=db_path,
        )

    # -- writes ------------------------------------------------------------
    def register(
        self, descriptor: WorkspaceDescriptor, *, save: bool = True
    ) -> WorkspaceDescriptor:
        existing = self._descriptors.get(descriptor.workspace_id)
        if existing is not None:
            if self._same_identity(existing, descriptor):
                return existing
            raise WorkspaceConflictError(
                f"workspace {descriptor.workspace_id!r} is already registered "
                "with a different identity"
            )
        self._add(descriptor)
        if save:
            try:
                self.save()
            except Exception:
                del self._descriptors[descriptor.workspace_id]
                raise
        return descriptor

    def save(self) -> None:
        """原子写入清单：同目录临时文件 + ``os.replace``。"""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "workspaces": [descriptor.to_dict() for descriptor in self.descriptors()],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.manifest_path.name}.",
            suffix=".tmp",
            dir=str(self.manifest_path.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.manifest_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    # -- internals ---------------------------------------------------------
    def _add(self, descriptor: WorkspaceDescriptor) -> None:
        if descriptor.workspace_id in self._descriptors:
            raise WorkspaceManifestError(
                f"duplicate workspace_id {descriptor.workspace_id!r} in manifest"
            )
        self._descriptors[descriptor.workspace_id] = descriptor

    def _same_identity(
        self, left: WorkspaceDescriptor, right: WorkspaceDescriptor
    ) -> bool:
        return (
            left.model_id == right.model_id
            and left.runtime_id == right.runtime_id
            and left.resolved_db_path(self.manifest_dir)
            == right.resolved_db_path(self.manifest_dir)
        )


# ---------------------------------------------------------------------------
# 显式接管寻址（C-WS-04）：清单 + 可信模型 + 只读内核预检
# ---------------------------------------------------------------------------
def load_catalog(search_root: str | Path = ".") -> WorkspaceCatalog:
    """从搜索根发现身份清单（纯读，缺失时返回空清单）。

    依次尝试 ``<root>/.instances/workspaces.json`` 与 ``<root>/workspaces.json``；
    都不存在时按 ``.instances`` 位置返回空清单——绝不创建目录或文件。
    """
    root = Path(search_root)
    for candidate in (
        root / ".instances" / MANIFEST_FILENAME,
        root / MANIFEST_FILENAME,
    ):
        if candidate.is_file():
            return WorkspaceCatalog.load(candidate)
    return WorkspaceCatalog.load(root / ".instances" / MANIFEST_FILENAME)


def check_kernel_database(db_path: str | Path) -> Path:
    """只读预检六张内核表，返回解析后的绝对路径。

    必须在实例化 ``OntologyRuntime`` **之前**调用：runtime 的构造会执行
    ``schema.sql``，在残缺 / 非本项目的库上会静默补齐缺失表，把「打开失败」
    变成「悄悄新建」。这里用 ``Path.resolve().as_uri() + '?mode=ro'`` 打开，
    因此不会创建文件、也不会触发任何写入。
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        raise WorkspaceDatabaseMissingError(
            f"workspace database is missing: {path}"
        )
    try:
        conn = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=2.0
        )
    except sqlite3.Error as exc:
        raise IncompleteKernelDatabaseError(
            f"cannot open kernel database {path}: {exc}"
        ) from exc
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise IncompleteKernelDatabaseError(
            f"{path} is not a readable SQLite kernel database: {exc}"
        ) from exc
    finally:
        conn.close()

    present = {str(row[0]) for row in rows}
    missing = [name for name in KERNEL_TABLES if name not in present]
    if missing:
        raise IncompleteKernelDatabaseError(
            f"{path} is not a complete kernel database; missing tables: "
            f"{', '.join(missing)}"
        )
    return path


def _aggregate_roots(runtime: Any, actor: Any) -> list[dict[str, Any]]:
    """经 ``runtime.query_objects`` 枚举聚合根（RLS / 脱敏照常生效）。"""
    spec = runtime.spec
    roots: list[dict[str, Any]] = []
    for type_name in sorted(spec.object_types):
        cls = spec.object_types[type_name]
        if not cls.is_aggregate_root():
            continue
        for obj in runtime.query_objects(cls, actor):
            roots.append(
                {
                    "object_type": type_name,
                    "object_id": obj.primary_key_value(),
                    "title": obj.title_value(),
                }
            )
    roots.sort(key=lambda item: (item["object_type"], item["object_id"]))
    return roots


def takeover_workspace(
    workspace_id: str,
    *,
    models: Mapping[str, Any],
    catalog: WorkspaceCatalog | None = None,
    search_root: str | Path = ".",
    actor: Any = None,
) -> dict[str, Any]:
    """按工作空间身份显式接管：清单解析 → 只读预检 → 打开 runtime。

    * ``models`` 是调用方显式提供的**可信**模型注册表；清单里的模型名只做
      键查找，绝不 ``importlib``。
    * 通用打开路径完全不感知任何具体领域模型（不 import ``kanban.spec``）。
    * 传入 ``actor`` 时附带经 ``runtime.query_objects`` 过滤的聚合根摘要；
      不传时不臆造 system 角色，``aggregate_roots`` 为 ``None``。
    * 缺失的工作空间 / 模型 / 数据库都在创建任何数据之前失败。
    """
    resolved = (catalog or load_catalog(search_root)).resolve(workspace_id, models)
    db_path = check_kernel_database(resolved.db_path)

    from .runtime import OntologyRuntime

    runtime = OntologyRuntime(resolved.model, db_path=db_path)
    try:
        root_types = sorted(
            name
            for name, cls in resolved.model.object_types.items()
            if cls.is_aggregate_root()
        )
        roots: list[dict[str, Any]] | None = None
        if actor is not None:
            roots = [
                {"workspace_id": resolved.descriptor.workspace_id, **root}
                for root in _aggregate_roots(runtime, actor)
            ]
    except Exception:
        runtime.close()
        raise

    return {
        "workspace_id": resolved.descriptor.workspace_id,
        "model_id": resolved.descriptor.model_id,
        "runtime_id": resolved.descriptor.runtime_id,
        "db_path": str(db_path),
        "model": resolved.model,
        "runtime": runtime,
        "aggregate_root_types": root_types,
        "aggregate_roots": roots,
    }
