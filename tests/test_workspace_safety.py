"""C-WS-01 验收测试：看板无损打开与隔离测试数据库。

覆盖：
1. 首次构建：在**指定目录**里创建演示库，不碰仓库 ``.instances``；
2. 重复打开：对象值 / 版本 / 关系 / 动作记录全部原样保留，种子动作不重放；
3. 显式 ``reset``：只重建指定数据库，绝不删目录里的其它文件；
4. 不兼容 / 半成品数据库：明确报错，不覆盖、不静默补种；
5. 缺任意一张核心表的半成品库：重新打开必须**先失败**，缺表不得被
   ``OntologyRuntime`` 静默补建，文件字节与既有数据不得改变（C-WS-01-R1）；
6. 目录名含 URI 保留字符（``#`` / ``%20`` / ``?`` / 空格）：预检必须打开
   **目标库本身**，绝不能被片段 / 查询串截断去创建或读取旁路文件
   （C-WS-01-R2）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from examples.command_center_demo import (
    IncompatibleBoardDatabaseError,
    build_default_command_center,
)
from open_ontology.kanban import (
    BOARD_MANAGER,
    CARD_WAITS_FOR,
    Card,
    spec as board_spec,
)
from open_ontology.runtime import OntologyRuntime

BOARD_KEY = "BD-CC-001"
INSTANCE_ID = "board-prod"

#: 可复用看板库必须完整的当前内核表；缺任何一张都是半成品库。
KERNEL_TABLES = (
    "ontology_registry",
    "ontology_objects",
    "ontology_links",
    "ontology_overlay",
    "ontology_action_runs",
    "ontology_write_gate",
)

#: C-WS-01 预检漏检、会被 ``OntologyRuntime`` 静默补建的四张核心表。
PREVIOUSLY_UNCHECKED_TABLES = (
    "ontology_links",
    "ontology_overlay",
    "ontology_registry",
    "ontology_write_gate",
)


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _logical_state(db_path: Path) -> dict:
    """逻辑状态指纹：对象数、看板行、动作账本条数。"""
    conn = sqlite3.connect(db_path)
    try:
        objects = conn.execute("SELECT COUNT(*) FROM ontology_objects").fetchone()[0]
        board = conn.execute(
            "SELECT object_id, version, properties_json FROM ontology_objects "
            "WHERE object_type = 'Board' AND object_id = ?",
            (BOARD_KEY,),
        ).fetchone()
        runs = conn.execute("SELECT COUNT(*) FROM ontology_action_runs").fetchone()[0]
    finally:
        conn.close()
    return {"objects": objects, "board": board, "runs": runs}


def _drop_table(db_path: Path, table: str) -> None:
    """删掉一张核心表并落盘，确保之后只剩单文件、无 WAL 残留。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()



def _runtime(registry):
    return registry.get_instance(INSTANCE_ID).runtime


def _board(runtime):
    return runtime.get_object("Board", BOARD_KEY, BOARD_MANAGER)


def _cards(runtime) -> dict[str, tuple[str, str, str | None]]:
    return {
        card.card_id: (card.title, card.column_key, card.assignee_id)
        for card in runtime.query_objects(Card, BOARD_MANAGER)
    }


def _object_rows(runtime) -> list[dict]:
    """完整业务对象行（值 / 版本 / 标题 / 安全标签 / 内容哈希）。

    ``ontology_registry.updated_at`` 在每次打开时会被重新登记刷新，不属于
    业务事实，故不纳入比较；业务对象自身的 ``updated_at`` 只在真实写入时
    变化，必须原样保留。
    """
    return [
        {
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "title": row["title"],
            "properties": row["properties"],
            "version": row["version"],
            "security_tags": row["security_tags"],
            "content_hash": row["content_hash"],
            "updated_at": row["updated_at"],
        }
        for row in runtime.list_objects()
    ]


def _link_rows(runtime) -> list[tuple[str, str, str, str, str]]:
    """全部关系边（稳定排序：按五元组字典序）。"""
    conn = sqlite3.connect(runtime.db_path)
    try:
        return [
            tuple(str(value) for value in row)
            for row in conn.execute(
                "SELECT link_type, source_type, source_id, target_type, target_id "
                "FROM ontology_links "
                "ORDER BY link_type, source_type, source_id, target_type, target_id"
            )
        ]
    finally:
        conn.close()


def _snapshot(runtime) -> dict:
    """完整世界状态：指纹 + 业务对象全字段 + 关系边 + 完整动作账本。"""
    return {
        "fingerprint": runtime.state_fingerprint(),
        "objects": _object_rows(runtime),
        "links": _link_rows(runtime),
        "runs": [dict(row) for row in runtime.list_action_runs()],
    }


def _seed_runs(runtime) -> int:
    return sum(
        1 for row in runtime.list_action_runs() if row["action_type"] == "create_board"
    )


# ---------------------------------------------------------------------------
# 1. 首次构建：库落在指定目录，不碰仓库 .instances
# ---------------------------------------------------------------------------
def test_first_build_creates_database_under_given_directory(tmp_path):
    data_dir = tmp_path / "instances"
    assert not data_dir.exists()

    registry = build_default_command_center(data_dir=data_dir)
    runtime = _runtime(registry)
    try:
        db_path = data_dir / "board_prod.sqlite3"
        assert db_path.is_file()
        assert Path(runtime.db_path) == db_path
        # 没有在临时目录里偷偷造 .instances
        assert not (tmp_path / ".instances").exists()
        assert _board(runtime).board_key == BOARD_KEY
        assert len(_cards(runtime)) == 4
        assert _seed_runs(runtime) == 1
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 2. 重复打开：一切原样保留，种子动作不重放
# ---------------------------------------------------------------------------
def test_reopening_demo_preserves_objects_versions_links_and_runs(tmp_path):
    first = build_default_command_center(data_dir=tmp_path)
    runtime = _runtime(first)
    original_goal = _board(runtime).goal
    original_cards = _cards(runtime)
    assert len(original_cards) == 4

    # 关系：C-103 waits_for C-101
    waits = runtime.get_linked(
        runtime.get_object("Card", "C-103", BOARD_MANAGER),
        CARD_WAITS_FOR,
        Card,
        BOARD_MANAGER,
    )
    assert [card.card_id for card in waits] == ["C-101"]

    # 通过动作改状态：更新基线 + 新增一张卡
    runtime.submit_action(
        "T-EDIT-STATE",
        "update_board_state",
        {"board_key": BOARD_KEY, "state": "人工编辑后的基线：C-WS-01 进行中"},
        BOARD_MANAGER,
    )
    runtime.submit_action(
        "T-EDIT-CARD",
        "create_card",
        {"card_id": "C-900", "board_key": BOARD_KEY, "title": "人工新增卡"},
        BOARD_MANAGER,
    )
    before = _snapshot(runtime)
    assert _board(runtime).state == "人工编辑后的基线：C-WS-01 进行中"
    runtime.close()

    # 重新打开演示：默认非破坏性
    second = build_default_command_center(data_dir=tmp_path)
    reopened = _runtime(second)
    try:
        board = _board(reopened)
        assert board.state == "人工编辑后的基线：C-WS-01 进行中"
        assert board.goal == original_goal

        # 对象值、版本、关系、动作记录全部原样
        assert _snapshot(reopened) == before
        cards = _cards(reopened)
        assert cards["C-900"] == ("人工新增卡", "ready", None)
        for card_id, expected in original_cards.items():
            assert cards[card_id] == expected
        assert len(cards) == 5

        waits_after = reopened.get_linked(
            reopened.get_object("Card", "C-103", BOARD_MANAGER),
            CARD_WAITS_FOR,
            Card,
            BOARD_MANAGER,
        )
        assert [card.card_id for card in waits_after] == ["C-101"]

        # 种子动作不重放：create_board 仍只有一次，账本没有新增
        assert _seed_runs(reopened) == 1
        assert len(reopened.list_action_runs()) == len(before["runs"])
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 3. 显式 reset：只重建指定数据库，不动同目录其它文件
# ---------------------------------------------------------------------------
def test_explicit_reset_reseeds_only_the_specified_database(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sentinel = data_dir / "keep_me.txt"
    sentinel.write_text("用户数据", encoding="utf-8")

    first = build_default_command_center(data_dir=data_dir)
    runtime = _runtime(first)
    runtime.submit_action(
        "T-EDIT-STATE",
        "update_board_state",
        {"board_key": BOARD_KEY, "state": "改过的基线"},
        BOARD_MANAGER,
    )
    runtime.close()

    reset_registry = build_default_command_center(data_dir=data_dir, reset=True)
    reopened = _runtime(reset_registry)
    try:
        board = _board(reopened)
        assert board.state != "改过的基线"
        assert board.state
        assert len(_cards(reopened)) == 4
        assert _seed_runs(reopened) == 1

        # 目录没有被整体删除：哨兵文件还在，库也重建了
        assert sentinel.read_text(encoding="utf-8") == "用户数据"
        assert (data_dir / "board_prod.sqlite3").is_file()
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 4. 不兼容 / 半成品数据库：明确报错，不覆盖、不静默补种
# ---------------------------------------------------------------------------
def test_foreign_sqlite_file_fails_clearly_and_is_not_overwritten(tmp_path):
    db_path = tmp_path / "board_prod.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    before = db_path.read_bytes()

    with pytest.raises(ValueError, match="不是可复用的看板库") as excinfo:
        build_default_command_center(data_dir=tmp_path)

    assert str(db_path) in str(excinfo.value)
    assert db_path.read_bytes() == before


def test_partial_board_database_without_board_fails_clearly(tmp_path):
    db_path = tmp_path / "board_prod.sqlite3"
    # 只有 task_board 的注册表，没有任何看板对象 —— 半成品库
    OntologyRuntime(board_spec, db_path=db_path).close()

    with pytest.raises(ValueError, match="BD-CC-001"):
        build_default_command_center(data_dir=tmp_path)

    # 失败后仍然没有看板，也没有被种子动作污染
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM ontology_objects").fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


def test_corrupt_database_file_fails_clearly(tmp_path):
    db_path = tmp_path / "board_prod.sqlite3"
    db_path.write_bytes(b"this is not a sqlite database at all")
    before = db_path.read_bytes()

    with pytest.raises(ValueError, match="board_prod.sqlite3"):
        build_default_command_center(data_dir=tmp_path)

    assert db_path.read_bytes() == before


# ---------------------------------------------------------------------------
# 5. 半成品库：缺任意一张核心表都必须先失败，绝不静默补建
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dropped_table", PREVIOUSLY_UNCHECKED_TABLES)
def test_partial_database_missing_core_table_fails_before_schema_recreation(
    tmp_path, dropped_table
):
    """缺 ``ontology_links`` / ``ontology_overlay`` / ``ontology_registry`` /
    ``ontology_write_gate`` 任意一张：保留目标看板，重新打开必须抛
    ``IncompatibleBoardDatabaseError``，且缺表不得被补建、数据与文件不得改变。
    """
    db_path = tmp_path / "board_prod.sqlite3"

    # 先造一份合法的演示库，再关掉所有 runtime 连接。
    first = build_default_command_center(data_dir=tmp_path)
    _runtime(first).close()
    assert set(KERNEL_TABLES) <= _table_names(db_path)
    state_before = _logical_state(db_path)
    assert state_before["board"] is not None
    assert state_before["objects"] == 19
    assert state_before["runs"] == 15

    # 只删这一张表：目标看板与其余数据保持原样。
    _drop_table(db_path, dropped_table)
    assert dropped_table not in _table_names(db_path)
    assert _logical_state(db_path) == state_before

    partial_bytes = db_path.read_bytes()
    partial_state = _logical_state(db_path)

    with pytest.raises(IncompatibleBoardDatabaseError, match=dropped_table):
        build_default_command_center(data_dir=tmp_path)

    # 失败发生在任何建表之前：缺表仍是缺的，对象 / 看板 / 账本 / 文件字节全未变。
    assert dropped_table not in _table_names(db_path)
    assert set(KERNEL_TABLES) - _table_names(db_path) == {dropped_table}
    assert _logical_state(db_path) == partial_state
    assert db_path.read_bytes() == partial_bytes


# ---------------------------------------------------------------------------
# 6. URI 保留字符目录：只读预检必须落在目标库上，不得产生旁路文件
# ---------------------------------------------------------------------------
#: 会破坏 ``file:<path>?mode=ro`` 拼接的目录名：``#`` 起片段、``?`` 起查询串、
#: ``%20`` 被百分号解码成空格。裸空格用于确认正常路径不被误伤。
RESERVED_DIRNAMES = (
    "board#1",
    "literal%20space",
    "query?dir",
    "space dir",
)


def _entries(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


@pytest.mark.parametrize(
    "dirname",
    RESERVED_DIRNAMES,
    ids=["hash-fragment", "percent-encoded", "question-query", "literal-space"],
)
def test_reopening_under_uri_reserved_directory_stays_on_target(tmp_path, dirname):
    """目录名含 ``#`` / ``%20`` / ``?`` / 空格时，默认重开必须：

    1. 打开**目标库本身**（``<dir>/board_prod.sqlite3``），而不是被片段或查询
       串截断出来的兄弟文件；
    2. 完整保留上一次写入的业务状态；
    3. 目录条目一字不差——绝不在库目录或上级目录里创建旁路文件。
    """
    data_dir = tmp_path / dirname
    data_dir.mkdir()
    db_path = data_dir / "board_prod.sqlite3"

    first = build_default_command_center(data_dir=data_dir)
    runtime = _runtime(first)
    assert Path(runtime.db_path) == db_path
    runtime.submit_action(
        "T-EDIT-STATE",
        "update_board_state",
        {"board_key": BOARD_KEY, "state": f"保留字符目录基线：{dirname}"},
        BOARD_MANAGER,
    )
    before = _snapshot(runtime)
    runtime.close()

    entries_after_first = _entries(data_dir)
    assert entries_after_first == ["board_prod.sqlite3"]

    second = build_default_command_center(data_dir=data_dir)
    reopened = _runtime(second)
    try:
        # 1. 命中的就是目标库，且数据是同一份
        assert Path(reopened.db_path) == db_path
        assert _board(reopened).state == f"保留字符目录基线：{dirname}"
        # 2. 完整状态（对象 / 关系 / 账本）原样保留，种子动作不重放
        assert _snapshot(reopened) == before
        assert _seed_runs(reopened) == 1
        assert len(_cards(reopened)) == 4
    finally:
        reopened.close()

    # 3. 目录条目完全一致：没有 ``board`` / ``query`` / ``literal space`` 之类旁路
    assert _entries(data_dir) == entries_after_first
    assert _entries(tmp_path) == [dirname]

