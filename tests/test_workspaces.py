"""C-WS-02 验收测试：工作空间身份持久化（不新增业务表）。

覆盖：

1. ``WorkspaceDescriptor`` 只承载身份四元组，校验空值 / 内存库 / 未知字段；
2. 目录清单带显式格式版本，保存走临时文件 + 原子替换；
3. 进程式重建后仍能读回同一工作空间身份；
4. 相对数据库路径相对清单解析，且解析绝不创建缺失目录 / 数据库；
5. 相同注册幂等、冲突重复 ID 明确失败且不污染原文件；
6. 畸形 / 不支持版本的清单明确失败且原文件字节不变；
7. 模型只从显式可信注册表解析，绝不按 JSON 里的名字 import 代码；
8. 文件缺失 / 模型不可用都明确失败；
9. ``CommandCenterRegistry`` 的 workspace / instance 别名共享同一份条目；
10. 仅清单中存在的工作空间在 ``list_workspaces`` 里显式 ``not_loaded``，
    且列表过程不打开 / 不创建数据库；
11. 演示装配把 ``board-prod`` 的真实 ``runtime.db_path`` 写进清单；
12. 内存 runtime 即使展示 URI 像文件也保持临时态，不落清单。
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from open_ontology import Actor, ObjectType, OntologyRuntime
from examples.kanban import BOARD_MANAGER, Board, Card, spec as kanban_spec
from open_ontology.spec import OntologySpec
from open_ontology.workshop import CommandCenterRegistry
from open_ontology.workspaces import (
    IncompleteKernelDatabaseError,
    MANIFEST_FILENAME,
    MANIFEST_FORMAT_VERSION,
    WorkspaceCatalog,
    WorkspaceConflictError,
    WorkspaceDatabaseMissingError,
    WorkspaceDescriptor,
    WorkspaceManifestError,
    WorkspaceModelUnavailableError,
    WorkspaceNotFoundError,
    UnsupportedWorkspaceManifestError,
    find_ontology_sqlite_files,
    locate_object_in_databases,
    takeover_workspace,
)

MANAGER = Actor(actor_id="manager-01", role="board_manager", clearance_tags={"*"})


def _descriptor(**overrides) -> WorkspaceDescriptor:
    values = {
        "workspace_id": "ws-1",
        "model_id": "task_board",
        "runtime_id": "rt-ws-1",
        "db_path": "board.sqlite3",
    }
    values.update(overrides)
    return WorkspaceDescriptor(**values)


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 描述符校验：只有身份四元组
# ---------------------------------------------------------------------------
def test_descriptor_round_trips_only_identity_fields():
    descriptor = _descriptor()
    payload = descriptor.to_dict()
    assert payload == {
        "workspace_id": "ws-1",
        "model_id": "task_board",
        "runtime_id": "rt-ws-1",
        "db_path": "board.sqlite3",
    }
    assert WorkspaceDescriptor.from_dict(payload) == descriptor


@pytest.mark.parametrize(
    "field", ["workspace_id", "model_id", "runtime_id", "db_path"]
)
def test_descriptor_rejects_blank_identity_fields(field):
    with pytest.raises(ValueError):
        _descriptor(**{field: "   "})


def test_descriptor_rejects_in_memory_database():
    with pytest.raises(ValueError, match="memory"):
        _descriptor(db_path=":memory:")


def test_descriptor_rejects_unknown_or_missing_fields():
    payload = _descriptor().to_dict()
    payload["importer"] = "evil.module:factory"
    with pytest.raises(ValueError):
        WorkspaceDescriptor.from_dict(payload)

    incomplete = _descriptor().to_dict()
    del incomplete["runtime_id"]
    with pytest.raises(ValueError):
        WorkspaceDescriptor.from_dict(incomplete)


def test_descriptor_accepts_pathlike_database_path():
    descriptor = _descriptor(db_path=Path("data/board.sqlite3"))
    assert descriptor.db_path == "data/board.sqlite3"


# ---------------------------------------------------------------------------
# 2. 保存 / 进程式重建
# ---------------------------------------------------------------------------
def test_catalog_save_and_reload_across_process_like_reconstruction(tmp_path):
    manifest = tmp_path / "instances" / "workspaces.json"
    catalog = WorkspaceCatalog(manifest)
    catalog.register(_descriptor(db_path=str(tmp_path / "instances" / "board.sqlite3")))

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["format_version"] == MANIFEST_FORMAT_VERSION
    assert payload["workspaces"][0]["workspace_id"] == "ws-1"
    assert "evil.module" not in manifest.read_text(encoding="utf-8")

    rebuilt = WorkspaceCatalog.load(manifest)
    assert rebuilt.get("ws-1") == catalog.get("ws-1")
    assert rebuilt.resolved_db_path("ws-1") == (
        tmp_path / "instances" / "board.sqlite3"
    ).resolve()
    assert rebuilt.manifest_path == manifest


def test_catalog_load_missing_manifest_is_empty_and_read_only(tmp_path):
    manifest = tmp_path / "never" / "workspaces.json"
    catalog = WorkspaceCatalog.load(manifest)
    assert catalog.descriptors() == ()
    assert not manifest.exists()
    assert not manifest.parent.exists()


def test_catalog_resolves_relative_path_against_manifest(tmp_path):
    manifest = tmp_path / "instances" / "workspaces.json"
    db_path = manifest.parent / "data" / "board.sqlite3"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    _write_manifest(
        manifest,
        {
            "format_version": MANIFEST_FORMAT_VERSION,
            "workspaces": [
                {
                    "workspace_id": "board-prod",
                    "model_id": "task_board",
                    "runtime_id": "rt-board-prod",
                    "db_path": "data/board.sqlite3",
                }
            ],
        },
    )

    catalog = WorkspaceCatalog.load(manifest)
    resolved = catalog.resolve("board-prod", {"task_board": kanban_spec})
    assert resolved.db_path == db_path.resolve()
    assert resolved.model is kanban_spec
    assert resolved.descriptor.model_id == "task_board"


# ---------------------------------------------------------------------------
# 3. 幂等 / 冲突 / 畸形清单
# ---------------------------------------------------------------------------
def test_catalog_identical_registration_is_idempotent(tmp_path):
    manifest = tmp_path / "workspaces.json"
    catalog = WorkspaceCatalog(manifest)
    descriptor = _descriptor(db_path=str(tmp_path / "board.sqlite3"))
    catalog.register(descriptor)
    first_bytes = manifest.read_bytes()

    again = catalog.register(descriptor)
    assert again == catalog.get("ws-1")
    assert manifest.read_bytes() == first_bytes
    assert len(catalog.descriptors()) == 1


def test_catalog_conflicting_registration_leaves_file_and_memory_untouched(tmp_path):
    manifest = tmp_path / "workspaces.json"
    catalog = WorkspaceCatalog(manifest)
    catalog.register(_descriptor(db_path=str(tmp_path / "board.sqlite3")))
    before = manifest.read_bytes()

    with pytest.raises(WorkspaceConflictError):
        catalog.register(_descriptor(model_id="other_model"))
    assert catalog.get("ws-1").model_id == "task_board"
    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        "{this is not json",
        json.dumps({"workspaces": []}),
        json.dumps({"format_version": MANIFEST_FORMAT_VERSION, "workspaces": {}}),
        json.dumps(
            {
                "format_version": MANIFEST_FORMAT_VERSION,
                "workspaces": [
                    {
                        "workspace_id": "ws-1",
                        "model_id": "task_board",
                        "runtime_id": "rt-1",
                        "db_path": "a.sqlite3",
                        "actor": {"role": "admin"},
                    }
                ],
            }
        ),
    ],
    ids=["broken-json", "missing-version", "workspaces-not-list", "unknown-field"],
)
def test_malformed_manifest_fails_without_rewriting(tmp_path, raw):
    manifest = tmp_path / "workspaces.json"
    manifest.write_text(raw, encoding="utf-8")
    before = manifest.read_bytes()

    with pytest.raises(WorkspaceManifestError):
        WorkspaceCatalog.load(manifest)
    assert manifest.read_bytes() == before


def test_unsupported_manifest_version_fails_without_rewriting(tmp_path):
    manifest = tmp_path / "workspaces.json"
    _write_manifest(
        manifest,
        {"format_version": MANIFEST_FORMAT_VERSION + 999, "workspaces": []},
    )
    before = manifest.read_bytes()

    with pytest.raises(UnsupportedWorkspaceManifestError):
        WorkspaceCatalog.load(manifest)
    assert manifest.read_bytes() == before


def test_catalog_rejects_duplicate_ids_in_manifest(tmp_path):
    manifest = tmp_path / "workspaces.json"
    item = _descriptor().to_dict()
    _write_manifest(
        manifest,
        {"format_version": MANIFEST_FORMAT_VERSION, "workspaces": [item, item]},
    )
    with pytest.raises(WorkspaceManifestError):
        WorkspaceCatalog.load(manifest)


# ---------------------------------------------------------------------------
# 4. 缺失数据库 / 不可用模型：明确失败且不创建任何东西
# ---------------------------------------------------------------------------
def test_resolving_missing_database_fails_without_creating_it(tmp_path):
    manifest = tmp_path / "instances" / "workspaces.json"
    catalog = WorkspaceCatalog(manifest)
    missing = tmp_path / "instances" / "future.sqlite3"
    catalog.register(_descriptor(db_path=str(missing)))

    with pytest.raises(WorkspaceDatabaseMissingError):
        catalog.resolve("ws-1", {"task_board": kanban_spec})
    assert not missing.exists()


def test_resolving_unavailable_model_never_imports_code(tmp_path):
    manifest = tmp_path / "workspaces.json"
    db_path = tmp_path / "board.sqlite3"
    db_path.write_bytes(b"")
    _write_manifest(
        manifest,
        {
            "format_version": MANIFEST_FORMAT_VERSION,
            "workspaces": [
                {
                    "workspace_id": "ws-1",
                    "model_id": "evil.module:factory",
                    "runtime_id": "rt-1",
                    "db_path": "board.sqlite3",
                }
            ],
        },
    )
    catalog = WorkspaceCatalog.load(manifest)

    with pytest.raises(WorkspaceModelUnavailableError):
        catalog.resolve("ws-1", {"task_board": kanban_spec})


def test_unknown_workspace_lookup_fails_clearly(tmp_path):
    catalog = WorkspaceCatalog(tmp_path / "workspaces.json")
    with pytest.raises(WorkspaceNotFoundError):
        catalog.get("nope")


# ---------------------------------------------------------------------------
# 5. CommandCenterRegistry：workspace / instance 是同一份条目
# ---------------------------------------------------------------------------
def _registry_with_model() -> "CommandCenterRegistry":
    registry = CommandCenterRegistry()
    registry.register_ontology("task_board", "task_board", kanban_spec)
    return registry


def test_workspace_and_instance_aliases_share_one_entry(tmp_path):
    registry = _registry_with_model()
    runtime = OntologyRuntime(kanban_spec, tmp_path / "board.sqlite3")
    try:
        entry = registry.register_workspace(
            "board-prod",
            "task_board",
            "看板",
            runtime,
            db_uri="sqlite://cosmetic.sqlite3",
        )
        assert registry.get_workspace("board-prod") is entry
        assert registry.get_instance("board-prod") is entry
        assert registry.instances["board-prod"] is entry
        assert entry.workspace_id == entry.instance_id == "board-prod"
        assert entry.model_id == entry.ontology_id == "task_board"
        assert entry.runtime_id == "rt-board-prod"
    finally:
        runtime.close()


def test_register_instance_still_uses_the_same_backing_entries(tmp_path):
    registry = _registry_with_model()
    runtime = OntologyRuntime(kanban_spec)
    try:
        entry = registry.register_instance(
            "legacy-ws", "task_board", "旧入口", runtime
        )
        assert registry.get_workspace("legacy-ws") is entry
        assert registry.get_instance("legacy-ws") is entry
        assert entry.workspace_id == "legacy-ws"
        assert entry.model_id == "task_board"
        rows = registry.list_workspaces(MANAGER)
        assert [row["workspace_id"] for row in rows] == ["legacy-ws"]
    finally:
        runtime.close()


def test_identical_workspace_registration_is_idempotent(tmp_path):
    registry = _registry_with_model()
    catalog = WorkspaceCatalog(tmp_path / "workspaces.json")
    registry.catalog = catalog
    runtime = OntologyRuntime(kanban_spec, tmp_path / "board.sqlite3")
    try:
        first = registry.register_workspace(
            "board-prod", "task_board", "看板", runtime
        )
        second = registry.register_workspace(
            "board-prod", "task_board", "看板", runtime
        )
        assert second is first
        assert list(registry.instances) == ["board-prod"]
        assert len(catalog.descriptors()) == 1
    finally:
        runtime.close()


def test_conflicting_workspace_registration_leaves_registry_unchanged(tmp_path):
    registry = _registry_with_model()
    registry.register_ontology("other_model", "other_model", OntologySpec())
    first_runtime = OntologyRuntime(kanban_spec, tmp_path / "board.sqlite3")
    other_runtime = OntologyRuntime(OntologySpec(), tmp_path / "other.sqlite3")
    try:
        entry = registry.register_workspace(
            "board-prod", "task_board", "看板", first_runtime
        )
        runtimes_before = dict(registry.runtimes)
        with pytest.raises(WorkspaceConflictError):
            registry.register_workspace(
                "board-prod", "other_model", "冲突", other_runtime
            )
        assert registry.get_workspace("board-prod") is entry
        assert list(registry.instances) == ["board-prod"]
        assert registry.runtimes == runtimes_before
    finally:
        first_runtime.close()
        other_runtime.close()


def test_in_memory_runtime_stays_ephemeral_even_with_file_like_db_uri(tmp_path):
    registry = _registry_with_model()
    catalog = WorkspaceCatalog(tmp_path / "workspaces.json")
    registry.catalog = catalog
    runtime = OntologyRuntime(kanban_spec)  # db_path == :memory:
    try:
        entry = registry.register_workspace(
            "ws-mem",
            "task_board",
            "内存工作空间",
            runtime,
            db_uri="sqlite://board_prod.sqlite3",
        )
        assert entry.workspace_id == "ws-mem"
        assert catalog.descriptors() == ()
        assert not catalog.manifest_path.exists()
        row = registry.list_workspaces(MANAGER)[0]
        assert row["workspace_id"] == "ws-mem"
        assert row["availability"] == "available"
    finally:
        runtime.close()


def test_file_backed_registration_persists_actual_runtime_db_path(tmp_path):
    registry = _registry_with_model()
    catalog = WorkspaceCatalog(tmp_path / "instances" / "workspaces.json")
    registry.catalog = catalog
    (tmp_path / "instances").mkdir()
    runtime = OntologyRuntime(kanban_spec, tmp_path / "instances" / "board.sqlite3")
    try:
        registry.register_workspace(
            "board-prod",
            "task_board",
            "看板",
            runtime,
            db_uri="sqlite://cosmetic.sqlite3",
        )
        assert catalog.resolved_db_path("board-prod") == Path(runtime.db_path).resolve()
        text = catalog.manifest_path.read_text(encoding="utf-8")
        assert "cosmetic" not in text
        descriptor = catalog.get("board-prod")
        assert descriptor.model_id == "task_board"
        assert descriptor.runtime_id == "rt-board-prod"
    finally:
        runtime.close()


def test_catalog_only_workspace_is_listed_not_loaded_without_opening_db(tmp_path):
    manifest = tmp_path / "instances" / "workspaces.json"
    catalog = WorkspaceCatalog(manifest)
    missing_db = tmp_path / "instances" / "future.sqlite3"
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-future",
            model_id="task_board",
            runtime_id="rt-ws-future",
            db_path=str(missing_db),
        )
    )
    registry = CommandCenterRegistry(catalog=catalog)
    registry.register_ontology("task_board", "task_board", kanban_spec)

    rows = registry.list_workspaces(MANAGER)
    assert [row["workspace_id"] for row in rows] == ["ws-future"]
    row = rows[0]
    assert row["availability"] == "not_loaded"
    assert row["root_status"] == "not_loaded"
    assert row["aggregate_roots"] is None
    assert row["aggregate_root_types"] == ["Board"]
    assert not missing_db.exists()


def test_listing_merges_loaded_and_catalog_only_workspaces(tmp_path):
    catalog = WorkspaceCatalog(tmp_path / "workspaces.json")
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-catalog",
            model_id="task_board",
            runtime_id="rt-ws-catalog",
            db_path=str(tmp_path / "catalog.sqlite3"),
        )
    )
    registry = _registry_with_model()
    registry.catalog = catalog
    runtime = OntologyRuntime(kanban_spec)
    try:
        registry.register_workspace("ws-loaded", "task_board", "已加载", runtime)
        rows = registry.list_workspaces(MANAGER)
        assert [row["workspace_id"] for row in rows] == ["ws-catalog", "ws-loaded"]
        assert rows[0]["availability"] == "not_loaded"
        assert rows[0]["aggregate_roots"] is None
        assert rows[1]["availability"] == "available"
        assert rows[1]["aggregate_roots"] == []
        assert rows[1]["root_status"] == "empty"
    finally:
        runtime.close()


def test_get_workspace_unknown_id_raises_key_error(tmp_path):
    registry = _registry_with_model()
    with pytest.raises(KeyError):
        registry.get_workspace("nope")


# ---------------------------------------------------------------------------
# 6. 演示装配：可选清单 + 真实 runtime.db_path
# ---------------------------------------------------------------------------
def test_demo_wires_catalog_with_real_db_path_for_board_prod(tmp_path):
    from examples.command_center_demo import build_default_command_center

    registry = build_default_command_center(data_dir=tmp_path)
    entry = registry.get_workspace("board-prod")
    try:
        manifest = tmp_path / "workspaces.json"
        assert manifest.is_file()
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert payload["format_version"] == MANIFEST_FORMAT_VERSION
        assert len(payload["workspaces"]) == 1
        stored = payload["workspaces"][0]
        assert stored["workspace_id"] == "board-prod"
        assert stored["model_id"] == "task_board"
        assert stored["runtime_id"] == entry.runtime_id
        # 真实落盘路径，而不是展示用的 sqlite://board_prod.sqlite3
        assert Path(stored["db_path"]) == Path(entry.runtime.db_path)
        assert "sqlite://board_prod.sqlite3" not in manifest.read_text(
            encoding="utf-8"
        )

        # 全新的清单读取器无需构造 / 重置演示即可定位并校验可信模型绑定。
        fresh = WorkspaceCatalog.load(manifest)
        resolved = fresh.resolve("board-prod", {"task_board": kanban_spec})
        assert resolved.db_path == Path(entry.runtime.db_path).resolve()
        assert resolved.model is kanban_spec
        assert resolved.descriptor.runtime_id == entry.runtime_id
    finally:
        entry.runtime.close()


def test_reopening_demo_keeps_catalog_and_does_not_reseed(tmp_path):
    from examples.command_center_demo import build_default_command_center

    first = build_default_command_center(data_dir=tmp_path)
    first_runtime = first.get_workspace("board-prod").runtime
    run_count = len(first_runtime.list_action_runs())
    title = first_runtime.get_object("Board", "BD-CC-001", MANAGER).title
    first_runtime.close()
    manifest = tmp_path / "workspaces.json"
    manifest_before = manifest.read_bytes()
    assert run_count == 15

    second = build_default_command_center(data_dir=tmp_path)
    runtime = second.get_workspace("board-prod").runtime
    try:
        assert len(runtime.list_action_runs()) == run_count
        assert runtime.get_object("Board", "BD-CC-001", MANAGER).title == title
        assert manifest.read_bytes() == manifest_before

        rows = second.list_workspaces(MANAGER)
        board_rows = [row for row in rows if row["workspace_id"] == "board-prod"]
        assert len(board_rows) == 1
        row = board_rows[0]
        assert row["availability"] == "available"
        assert row["aggregate_root_types"] == ["Board"]
        assert [root["object_id"] for root in row["aggregate_roots"]] == [
            "BD-CC-001"
        ]
    finally:
        runtime.close()


def test_two_distinct_in_memory_runtimes_conflict_on_same_workspace_id():
    registry = _registry_with_model()
    rt1 = OntologyRuntime(kanban_spec)
    rt2 = OntologyRuntime(kanban_spec)
    try:
        entry = registry.register_workspace("ws-mem", "task_board", "内存一", rt1)
        with pytest.raises(WorkspaceConflictError):
            registry.register_workspace("ws-mem", "task_board", "内存二", rt2)
        assert registry.get_workspace("ws-mem") is entry
    finally:
        rt1.close()
        rt2.close()


def test_catalog_only_workspace_with_unregistered_model_reports_reason(tmp_path):
    catalog = WorkspaceCatalog(tmp_path / "workspaces.json")
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-unknown-model",
            model_id="unregistered_model",
            runtime_id="rt-ws-unknown-model",
            db_path=str(tmp_path / "future.sqlite3"),
        )
    )
    registry = CommandCenterRegistry(catalog=catalog)
    rows = registry.list_workspaces(MANAGER)
    assert len(rows) == 1
    assert rows[0]["availability"] == "not_loaded"
    assert rows[0]["root_status"] == "not_loaded"
    assert rows[0]["reason"] == "model_not_registered"
    assert rows[0]["aggregate_roots"] is None


class _Ledger(ObjectType):
    __primary_key__ = "ledger_id"
    __title__ = "name"
    __security_tag_fields__ = ("region",)
    __aggregate_root__ = True

    ledger_id: str
    name: str
    region: str


def _ledger_spec() -> OntologySpec:
    s = OntologySpec()
    s.object_type(_Ledger)
    return s


_LEDGER_SPEC = _ledger_spec()
_EAST_READER = Actor(
    actor_id="reader-east", role="reader", clearance_tags={"region:east"}
)


def _seed_ledger_workspace(tmp_path: Path) -> Path:
    data_dir = tmp_path / ".instances"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "ledger.sqlite3"
    runtime = OntologyRuntime(_LEDGER_SPEC, db_path=db_path)
    try:
        runtime.put_object(_Ledger(ledger_id="L-1", name="东区台账", region="east"))
        runtime.put_object(_Ledger(ledger_id="L-2", name="西区台账", region="west"))
    finally:
        runtime.close()
    catalog = WorkspaceCatalog(data_dir / MANIFEST_FILENAME)
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ledger-ws",
            model_id="ledger",
            runtime_id="rt-ledger-ws",
            db_path="ledger.sqlite3",
        )
    )
    return db_path


def _table_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0
    )
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


@pytest.mark.parametrize(
    "dirname",
    ["#fragment", "percent%20dir", "query?part", "literal space"],
    ids=["hash-fragment", "percent-encoded", "question-query", "literal-space"],
)
def test_readonly_scan_and_locate_survive_uri_reserved_directory(tmp_path, dirname):
    data_dir = tmp_path / dirname
    data_dir.mkdir()
    db_path = data_dir / "board_prod.sqlite3"
    runtime = OntologyRuntime(kanban_spec, db_path=db_path)
    try:
        runtime.submit_action(
            "SEED-1",
            "create_board",
            {
                "board_key": "BD-CC-001",
                "title": "看板",
                "tenant": "acme",
                "created_by": "board-manager-01",
            },
            BOARD_MANAGER,
        )
    finally:
        runtime.close()

    assert find_ontology_sqlite_files(data_dir) == [db_path]
    matches = locate_object_in_databases("BD-CC-001", data_dir)
    assert len(matches) == 1
    assert matches[0]["db_path"] == str(db_path.resolve())
    assert matches[0]["object_type"] == "Board"
    assert matches[0]["properties"]["title"] == "看板"

    allowed = {db_path.name, db_path.name + "-wal", db_path.name + "-shm"}
    assert {path.name for path in data_dir.iterdir()} <= allowed
    assert sorted(path.name for path in tmp_path.iterdir()) == [dirname]


def test_takeover_workspace_is_model_agnostic_and_respects_actor_visibility(
    tmp_path, monkeypatch
):
    db_path = _seed_ledger_workspace(tmp_path)
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in {"examples.kanban", "open_ontology.kanban"}:
            raise AssertionError(f"generic workspace takeover must not import {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    taken = takeover_workspace(
        "ledger-ws",
        models={"ledger": _LEDGER_SPEC},
        search_root=tmp_path,
        actor=_EAST_READER,
    )
    try:
        assert taken["workspace_id"] == "ledger-ws"
        assert taken["model_id"] == "ledger"
        assert taken["runtime_id"] == "rt-ledger-ws"
        assert taken["db_path"] == str(db_path.resolve())
        assert taken["model"] is _LEDGER_SPEC
        assert taken["aggregate_root_types"] == ["_Ledger"]
        assert taken["aggregate_roots"] == [
            {
                "workspace_id": "ledger-ws",
                "object_type": "_Ledger",
                "object_id": "L-1",
                "title": "东区台账",
            }
        ]
    finally:
        taken["runtime"].close()

    unscoped = takeover_workspace(
        "ledger-ws", models={"ledger": _LEDGER_SPEC}, search_root=tmp_path
    )
    try:
        assert unscoped["aggregate_root_types"] == ["_Ledger"]
        assert unscoped["aggregate_roots"] is None
    finally:
        unscoped["runtime"].close()


def test_takeover_workspace_missing_workspace_model_and_file_create_nothing(tmp_path):
    data_dir = tmp_path / ".instances"
    data_dir.mkdir()
    catalog = WorkspaceCatalog(data_dir / MANIFEST_FILENAME)
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-model",
            model_id="ledger",
            runtime_id="rt-ws-model",
            db_path="future.sqlite3",
        )
    )
    before = sorted(path.name for path in data_dir.iterdir())

    with pytest.raises(WorkspaceNotFoundError):
        takeover_workspace("ghost", models={"ledger": _LEDGER_SPEC}, catalog=catalog)
    with pytest.raises(WorkspaceModelUnavailableError):
        takeover_workspace(
            "ws-model", models={"other_model": _LEDGER_SPEC}, catalog=catalog
        )
    with pytest.raises(WorkspaceDatabaseMissingError):
        takeover_workspace("ws-model", models={"ledger": _LEDGER_SPEC}, catalog=catalog)

    assert sorted(path.name for path in data_dir.iterdir()) == before
    assert not (data_dir / "future.sqlite3").exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WorkspaceNotFoundError):
        takeover_workspace("ghost", models={"ledger": _LEDGER_SPEC}, search_root=empty)
    assert list(empty.iterdir()) == []


def test_takeover_workspace_rejects_partial_kernel_database_without_creating_tables(
    tmp_path,
):
    data_dir = tmp_path / ".instances"
    data_dir.mkdir()
    db_path = data_dir / "partial.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ontology_objects (object_type TEXT, object_id TEXT)")
    conn.commit()
    conn.close()
    before_tables = _table_names(db_path)
    before_bytes = db_path.read_bytes()

    catalog = WorkspaceCatalog(data_dir / MANIFEST_FILENAME)
    catalog.register(
        WorkspaceDescriptor(
            workspace_id="ws-partial",
            model_id="ledger",
            runtime_id="rt-ws-partial",
            db_path="partial.sqlite3",
        )
    )

    with pytest.raises(IncompleteKernelDatabaseError):
        takeover_workspace(
            "ws-partial", models={"ledger": _LEDGER_SPEC}, catalog=catalog
        )

    assert _table_names(db_path) == before_tables
    assert db_path.read_bytes() == before_bytes
    assert sorted(path.name for path in data_dir.iterdir()) == [
        "partial.sqlite3",
        MANIFEST_FILENAME,
    ]

