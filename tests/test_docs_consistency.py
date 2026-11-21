"""C-WS-06 文档一致性门禁：术语、元层次边界与实现状态不得互相矛盾。

这些断言只锁定**规范文档必须表达的立场**，不锁定具体行文或测试条数：

* 术语表必须存在，并明确区分工作空间身份与业务对象身份；
* ``docs/ARCHITECTURE.md`` 保持 M2 平台宪法，不夹带 M0 具体看板 ID；
* ``docs/TASK_BOARD.md`` 保持 M1/M0 契约，并回链 M2 宪法；
* 实现状态文档必须反映 Model/Workspace 已迁移的事实，不得继续宣称旧标签。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _doc(relpath: str) -> str:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"缺少规范文档：{relpath}"
    return path.read_text(encoding="utf-8")


def test_glossary_defines_core_terms_and_identity_boundary():
    text = _doc("GLOSSARY.md")
    for term in (
        "Symbolic World Model",
        "Model",
        "World",
        "Workspace",
        "TBox",
        "ABox",
        "Behavior",
        "Runtime",
        "Device",
    ):
        assert term in text, f"术语表缺少 {term}"
    assert "World · 世界状态" in text
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
    assert "Model · 领域模型" in text and "World · 世界状态" in text
    assert "Workspace · 工作空间" in text


def test_task_board_doc_keeps_m1_domain_contract_and_links_m2():
    text = _doc("docs/TASK_BOARD.md")
    assert "M1" in text
    assert "examples/kanban.py" in text
    assert "docs/ARCHITECTURE.md" in text
    assert "task_board" in text
    assert "Board" in text and "Card" in text and "Member" in text


def test_readme_and_state_docs_describe_current_implementation():
    readme = _doc("README.md")
    assert "GLOSSARY.md" in readme
    assert "Model · 领域模型" in readme and "World · 世界状态" in readme
    assert "Workspace · 工作空间" in readme
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


def test_canonical_docs_reflect_owl_object_property_and_autoid():
    for relpath in (
        "GLOSSARY.md",
        "docs/ARCHITECTURE.md",
        "docs/TASK_BOARD.md",
        "README.md",
        "_state.md",
    ):
        text = _doc(relpath)
        assert "ObjectProperty" in text, f"{relpath} 缺少 ObjectProperty 说明"
        assert "AutoID" in text, f"{relpath} 缺少 AutoID 说明"
        assert "LinkType" not in text, f"{relpath} 仍残留已移除的 LinkType"
        assert "card_waits_for" not in text, f"{relpath} 仍残留旧连边 card_waits_for"


def test_living_docs_synced_with_runtime_design_split_and_no_retired_symbols():
    """Archify 总体架构图三件套存在且嵌入文档，核心活文档对已退役 M2 符号零残留。"""
    assets_dir = REPO_ROOT / "docs" / "assets"
    arch_json = assets_dir / "openontology-architecture.json"
    arch_html = assets_dir / "openontology-architecture.html"
    arch_png = assets_dir / "openontology-architecture.png"

    for artifact in (arch_json, arch_html, arch_png):
        assert artifact.is_file(), f"缺少系统总体架构图资产：{artifact.relative_to(REPO_ROOT)}"
        assert artifact.stat().st_size > 0, f"系统总体架构图资产为空：{artifact.relative_to(REPO_ROOT)}"

    parsed_json = json.loads(arch_json.read_text(encoding="utf-8"))
    assert parsed_json.get("diagram_type") == "architecture"
    assert parsed_json.get("meta", {}).get("output") == "docs/assets/openontology-architecture.html"
    html_head = arch_html.read_text(encoding="utf-8")[:512].lower()
    assert "<!doctype html" in html_head or "<html" in html_head
    assert arch_png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    arch_doc = _doc("docs/ARCHITECTURE.md")
    assert "assets/openontology-architecture.png" in arch_doc
    assert "assets/openontology-architecture.html" in arch_doc
    assert "assets/openontology-architecture.json" in arch_doc

    readme_doc = _doc("README.md")
    assert "docs/assets/openontology-architecture.png" in readme_doc
    assert "docs/assets/openontology-architecture.html" in readme_doc
    assert "docs/assets/openontology-architecture.json" in readme_doc

    retired_re = re.compile(
        r"(?<![A-Za-z0-9_])("
        r"PENDING_APPROVAL|ApprovalGateAspect|post_approval|settle_approval|"
        r"approve_action|reject_action|approval_rule|approver_roles|plan_mcts"
        r")(?![A-Za-z0-9_])"
    )
    living_paths = (
        "README.md",
        "docs/ARCHITECTURE.md",
        "GLOSSARY.md",
        "docs/PALANTIR_AND_OPEN_ONTOLOGY.md",
        "docs/USE_CASES_IMPLEMENTATION.md",
    )
    corpus_parts: list[str] = []
    for relpath in living_paths:
        text = _doc(relpath)
        hits = sorted(set(retired_re.findall(text)))
        assert hits == [], f"活文档 {relpath} 仍残留已退役架构符号：{hits}"
        corpus_parts.append(text)

    combined = "\n".join(corpus_parts)
    for anchor in (
        "DeviceDriverRegistry",
        "open_ontology.design",
        "WorkCard",
        "mcp_server",
        "AutonomyRampController",
        "SagaReconciler",
        "schema_pg.sql",
        "MDMSidecarPlugin",
        "Gate 7",
    ):
        assert anchor in combined, f"活文档缺少现行关键架构锚点：{anchor}"
    assert "Gate 6" in combined or "Runtime 对 Design-Time" in combined

    for anchor in ("mcp_server", "AutonomyRampController", "SagaReconciler", "schema_pg.sql", "MDMSidecarPlugin", "Gate 7"):
        assert anchor in readme_doc, f"README.md 缺少生产运行壳或门禁锚点：{anchor}"
        assert anchor in arch_doc, f"docs/ARCHITECTURE.md 缺少生产运行壳或门禁锚点：{anchor}"

    for leak in ("jshERP", "Odoo", "BottleCRM", "SteelStorageBin", "螺纹钢"):
        assert leak not in arch_doc, f"M2 平台宪法 docs/ARCHITECTURE.md 夹带了具体厂商或行业实现词：{leak}"
