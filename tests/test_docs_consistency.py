"""C-WS-06 文档一致性门禁：术语、元层次边界与实现状态不得互相矛盾。

这些断言只锁定**规范文档必须表达的立场**，不锁定具体行文或测试条数：

* 术语表必须存在，并明确区分工作空间身份与业务对象身份；
* ``docs/ARCHITECTURE.md`` 保持 M2 平台宪法，不夹带 M0 具体看板 ID；
* ``docs/TASK_BOARD.md`` 保持 M1/M0 契约，并回链 M2 宪法；
* 实现状态文档必须反映 Model/Workspace 已迁移的事实，不得继续宣称旧标签。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _doc(relpath: str) -> str:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"缺少规范文档：{relpath}"
    return path.read_text(encoding="utf-8")


def test_glossary_defines_core_terms_and_identity_boundary():
    text = _doc("GLOSSARY.md")
    for term in ("Model", "Workspace", "TBox", "ABox", "Behavior", "Runtime", "Device"):
        assert term in text, f"术语表缺少 {term}"
    assert "workspace_id / object_type / object_id" in text
    assert "工作空间身份" in text and "业务对象身份" in text
    assert "聚合根" in text
    assert "workspace_root" in text


def test_architecture_doc_stays_m2_and_rejects_old_framing():
    text = _doc("docs/ARCHITECTURE.md")
    # 旧的「未激活 / 已激活」两阶段说法与「四个完全正交资源」误解必须被明确否定。
    assert "未激活" not in text and "激活的知识" not in text
    assert "不意味着四个完全正交" in text
    # M2 宪法不得夹带 M0 自举看板的实例 ID；M1/M0 契约在 TASK_BOARD.md。
    assert "BD-CC-001" not in text
    assert "docs/TASK_BOARD.md" in text
    # 迁移后的展示词汇与兼容映射必须写进宪法，而不是留在旧标签里。
    assert "Model · 领域模型" in text and "Workspace · 工作空间" in text


def test_task_board_doc_keeps_m1_m0_contract_and_links_m2():
    text = _doc("docs/TASK_BOARD.md")
    assert "M1" in text and "M0" in text
    assert "Board / BD-CC-001" in text
    assert "docs/ARCHITECTURE.md" in text
    assert "task_board" in text


def test_readme_and_state_docs_describe_current_implementation():
    readme = _doc("README.md")
    assert "GLOSSARY.md" in readme
    assert "Model · 领域模型" in readme and "Workspace · 工作空间" in readme
    assert "workspaces.py" in readme and "model_views.py" in readme
    # 旧「施工中 / 尚未验收」的状态声明不得继续冒充当前实现状态。
    assert "尚未验收、未合并 main" not in readme

    state = _doc("_state.md")
    assert "C-WS-06" in state
    assert "GLOSSARY.md" in state
    assert "Batch 2" in state
    # 已迁移的标签不得再被描述成待办或旧语义。
    assert "待 C-WS-05 迁移" not in state
    assert "未激活的知识" not in state

    execution = _doc("_execution.md")
    assert "C-WS-06" in execution
