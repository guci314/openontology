"""架构红线统一门禁测试（Track A · 4 道结构性自动门禁）。

覆盖两类架构反污染红线，彻底替代碎片化手工黑名单：
1. 门禁 1（技术不污染业务 · AST 导入白名单）：所有纯 M1 本体声明模块禁止直接或间接
   import ``OntologyRuntime``、执行传输、IO、OS 或外部系统库；
2. 门禁 2（M1 不污染 M2 · 递归边界与子类白名单）：递归扫描 ``open_ontology/**/*.py``，
   严禁反向引用 ``examples``、严禁核心/诱导纯符号模块引入网络/子进程 I/O、严禁内核夹带
   除元自举治理本体之外的任何 ``ObjectType`` 子类；
3. 门禁 3（动态 M1 结构词表反向扫描）：动态加载仓库内全部 M1 本体注册的所有对象类型、
   属性、接口、动作、函数、自动化规则与本体 ID，反向扫描整个 ``open_ontology/**`` 代码与
   ``ui_display.json``，确保新增任意业务域都会自动触发隔离校验；
4. 门禁 4（M2 跨领域盲测）：使用完全虚构领域（``Starship / WarpCore / Sector``）
   端到端跑通 ``OntologyRuntime`` / ``OntologyMCTSPlanner`` / ``describe_tbox`` /
   ``SymbolicCrucible``。
"""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

from pydantic import BaseModel

from open_ontology import Actor, OntologyRuntime, OntologySpec
from open_ontology.induction import (
    ActionBlueprint,
    ActionMutationSpec,
    CrucibleContext,
    DeclarativeBlueprint,
    FieldSpec,
    InvariantRuleSpec,
    ObjectTypeBlueprint,
    ReplayEvent,
    SymbolicCrucible,
)
from open_ontology.model_views import describe_tbox
from open_ontology.spec import ObjectProperty, ObjectType

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "open_ontology"

M1_ONTOLOGY_MODULES: tuple[tuple[str, str], ...] = (
    ("examples.business_ontology", "spec"),
    ("examples.kanban", "spec"),
    ("examples.dev_factory_ontology", "spec"),
    ("examples.cold_chain_ontology", "spec"),
    ("examples.steel_mdm_ontology", "spec"),
    ("examples.saas_unplug_ontology", "build_saas_ontology_spec"),
    ("examples.mcts_world_model_ontology", "spec"),
    ("examples.osv2_governance_ontology", "osv2_spec"),
    ("examples.o2c_ontology", "spec"),
    ("examples.odoo_orders_ontology", "spec"),
    ("examples.crm_unplug_ontology", "spec"),
)

_ALLOWED_M1_STDLIB_AND_SDK = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "math",
        "time",
        "typing",
        "uuid",
        "pydantic",
        "open_ontology.spec",
        "open_ontology.induction.seed",
        "examples.business_ontology",
    }
)

_FORBIDDEN_TECH_ROOTS = frozenset(
    {
        "sqlite3",
        "urllib",
        "http",
        "socket",
        "subprocess",
        "requests",
        "httpx",
        "os",
        "pathlib",
        "sys",
        "shutil",
        "open_ontology.runtime",
        "open_ontology.http_server",
        "open_ontology.workshop",
        "open_ontology.execution",
        "open_ontology.webhooks",
        "open_ontology.ingestion",
    }
)

_TRANSPORT_EXEMPT_FILES = frozenset(
    {
        PKG_ROOT / "http_server.py",
        PKG_ROOT / "workshop.py",
        PKG_ROOT / "meta_admin.py",
        PKG_ROOT / "webhooks.py",
        PKG_ROOT / "induction" / "backends.py",
    }
)


def _iter_kernel_py_files() -> list[Path]:
    return sorted(
        p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts
    )


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                ids.add(id(node.body[0].value))
    return ids


# ---------------------------------------------------------------------------
# Gate 1: M1 纯业务声明层 AST 导入白名单
# ---------------------------------------------------------------------------
def test_gate1_m1_ontologies_strict_ast_import_whitelist() -> None:
    """所有纯 M1 本体声明模块只能依赖数据类型库与 open_ontology.spec。"""
    for mod_name, _ in M1_ONTOLOGY_MODULES:
        rel_path = Path(*mod_name.split(".")).with_suffix(".py")
        path = REPO_ROOT / rel_path
        assert path.is_file(), f"缺少纯 M1 模块 {rel_path}"

        source = path.read_text(encoding="utf-8")
        assert "OntologyRuntime" not in source, (
            f"M1 本体 {rel_path} 不得引用或构造 OntologyRuntime"
        )

        tree = ast.parse(source, filename=str(rel_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in _ALLOWED_M1_STDLIB_AND_SDK, (
                        f"M1 本体 {rel_path} 禁止 import {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level and mod == "business_ontology":
                    continue
                if mod in ("open_ontology",):
                    raise AssertionError(
                        f"M1 本体 {rel_path} 禁止从包顶层 `from open_ontology import ...`，"
                        "必须精确引用 `open_ontology.spec`（或 `open_ontology.induction.seed`）"
                    )
                root = mod.split(".")[0]
                allowed = (
                    mod in _ALLOWED_M1_STDLIB_AND_SDK
                    or root in _ALLOWED_M1_STDLIB_AND_SDK
                )
                assert allowed, f"M1 本体 {rel_path} 存在白名单外导入: {mod!r}"
                assert root not in _FORBIDDEN_TECH_ROOTS and mod not in _FORBIDDEN_TECH_ROOTS, (
                    f"M1 本体 {rel_path} 泄漏技术依赖: {mod!r}"
                )


# ---------------------------------------------------------------------------
# Gate 2: M2 递归目录隔离、禁反向导入与 ObjectType 子类白名单
# ---------------------------------------------------------------------------
def test_gate2_m2_zero_reverse_example_imports_no_business_objects_clean_io() -> None:
    """M2 内核递归门禁：禁 import examples，禁非法 IO，禁夹带业务 ObjectType。"""
    meta_ontology_paths = {
        (PKG_ROOT / "induction" / "governance.py").resolve(),
        (PKG_ROOT / "meta_admin.py").resolve(),
    }
    files = _iter_kernel_py_files()
    assert len(files) >= 15

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)

        # 2a. 禁止任何层次的反向 import examples（含函数内延迟导入）
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] != "examples", (
                        f"M2 内核 {rel} 禁止反向导入 {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod.split(".")[0] != "examples", (
                    f"M2 内核 {rel} 禁止反向导入 {mod!r}"
                )

        # 2b. 非显式传输适配器禁止网络 / 子进程 I/O
        if PKG_ROOT / "execution" not in path.parents and path.resolve() not in _TRANSPORT_EXEMPT_FILES:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                else:
                    continue
                for mod in mods:
                    root = mod.split(".")[0]
                    assert root not in ("urllib", "subprocess", "socket", "requests", "httpx"), (
                        f"纯 M2 模块 {rel} 禁止引入传输库 {mod!r}"
                    )
                    assert not mod.startswith("http."), (
                        f"纯 M2 模块 {rel} 禁止引入 {mod!r}"
                    )

        # 2c. 内核里唯一允许定义具体 ObjectType 子类的是元自举治理本体与平台自举元本体
        if path.resolve() not in meta_ontology_paths:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_name = (
                            base.id
                            if isinstance(base, ast.Name)
                            else base.attr
                            if isinstance(base, ast.Attribute)
                            else ""
                        )
                        assert base_name != "ObjectType", (
                            f"M2 内核 {rel} 禁止夹带具体 ObjectType 子类 {node.name!r}"
                        )


# ---------------------------------------------------------------------------
# Gate 3: 动态收集全部 M1 结构词表，反向扫描整个 open_ontology/** 与 ui_display.json
# ---------------------------------------------------------------------------
def test_gate3_dynamic_m1_vocabulary_reverse_scan() -> None:
    """动态加载全部 M1 本体词表并反向扫描内核源码与内核展示字典。"""
    structural_tokens: set[str] = {"task_board"}
    ontology_ids: set[str] = {"task_board"}

    for mod_name, attr in M1_ONTOLOGY_MODULES:
        mod = importlib.import_module(mod_name)
        target = getattr(mod, attr)
        spec: OntologySpec = target() if callable(target) else target
        structural_tokens.update(spec.object_types.keys())
        structural_tokens.update(spec.object_properties.keys())
        structural_tokens.update(spec.interfaces.keys())
        structural_tokens.update(spec.actions.keys())
        structural_tokens.update(spec.functions.keys())
        structural_tokens.update(spec.automations.keys())
        ont_keys = set((spec.display.get("ontologies") or {}).keys())
        structural_tokens.update(ont_keys)
        ontology_ids.update(ont_keys)

    assert len(structural_tokens) >= 120

    # 3a. ui_display.json 不允许保留任何具体 M1 词条或本体名
    ui_display = json.loads((PKG_ROOT / "ui_display.json").read_text(encoding="utf-8"))
    ui_blob = json.dumps(ui_display, ensure_ascii=False)
    leaked_ui = sorted(tok for tok in structural_tokens if tok in ui_blob)
    assert leaked_ui == [], f"ui_display.json 残留 M1 业务标识：{leaked_ui}"

    # 3b. open_ontology/**/*.py AST 标识符与非 docstring 字符串不得出现 M1 标识
    leaks: list[tuple[str, list[str]]] = []
    for path in _iter_kernel_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc_ids = _docstring_node_ids(tree)
        idents: set[str] = set()
        strings: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                idents.add(node.id)
            elif isinstance(node, ast.Attribute):
                idents.add(node.attr)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                idents.add(node.name)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in doc_ids
            ):
                strings.add(node.value)

        hits = sorted(t for t in structural_tokens if t in idents or t in strings)
        # 对多段本体 ID 也做非 docstring 字符串子串检查
        id_sub_hits = sorted(
            oid
            for oid in ontology_ids
            if any(oid in s for s in strings) and oid not in hits
        )
        all_hits = sorted(set(hits + id_sub_hits))
        if all_hits:
            leaks.append((str(path.relative_to(REPO_ROOT)), all_hits))

    assert leaks == [], f"M1 结构标识反向泄漏进 open_ontology/：{leaks}"


# ---------------------------------------------------------------------------
# Gate 4: 完全虚构领域的 M2 领域无关契约盲测
# ---------------------------------------------------------------------------
def test_gate4_fictional_domain_blind_m2_contract() -> None:
    """用虚构星舰领域（Starship / WarpCore）盲测 Runtime、MCTS、TBox 与 Crucible。"""

    class WarpCore(ObjectType):
        __primary_key__ = "core_id"
        __title__ = "label"
        core_id: str
        label: str
        output_gw: int = 100

    class Starship(ObjectType):
        __primary_key__ = "ship_id"
        __title__ = "callsign"
        ship_id: str
        callsign: str
        sector: str = "alpha"
        fuel_cells: int = 5
        core: ObjectProperty[WarpCore]

    class JumpParams(BaseModel):
        ship_id: str
        target_sector: str
        cost_cells: int = 2

    class ScoreParams(BaseModel):
        label: str = "score"

    blind_spec = OntologySpec()
    blind_spec.object_type(WarpCore)
    blind_spec.object_type(Starship)

    @blind_spec.action("jump_sector", JumpParams, allowed_roles=("navigator",))
    def jump_sector(ctx, params: JumpParams) -> None:
        ship = ctx.require(Starship, params.ship_id)
        if ship.fuel_cells < params.cost_cells:
            raise ValueError("insufficient fuel cells")
        ctx.modify(
            ship,
            sector=params.target_sector,
            fuel_cells=ship.fuel_cells - params.cost_cells,
        )

    @blind_spec.function("fleet_fuel_total", ScoreParams)
    def fleet_fuel_total(ctx, params: ScoreParams) -> dict:
        ships = ctx.query(Starship)
        return {
            "total_value": sum(s.fuel_cells * 10 + (50 if s.sector == "omega" else 0) for s in ships),
        }

    nav = Actor(actor_id="nav-01", role="navigator", clearance_tags={"*"})
    rt = OntologyRuntime(blind_spec)
    try:
        rt.put_object(WarpCore(core_id="WC-1", label="Mark-IV"))
        rt.put_object(Starship(ship_id="SS-01", callsign="Enterprise", core="WC-1"))

        tbox = describe_tbox(blind_spec)
        assert {ot["name"] for ot in tbox["object_types"]} == {"WarpCore", "Starship"}

        plan = rt.plan_mcts(
            nav,
            candidate_table=[[("jump_sector", {"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 2})]],
            value_function="fleet_fuel_total",
            simulations=8,
        )
        assert plan.best_plan
        rt.submit_action("JUMP-1", "jump_sector", {"ship_id": "SS-01", "target_sector": "omega"}, nav)
        assert rt.get_object(Starship, "SS-01", nav).sector == "omega"

        blind_bp = DeclarativeBlueprint(
            model_id="starship_blind",
            version="1.0.0",
            object_types=[
                ObjectTypeBlueprint(
                    name="WarpCore",
                    primary_key="core_id",
                    title_field="label",
                    fields=[
                        FieldSpec(name="core_id", field_type="str", primary_key=True, required=True),
                        FieldSpec(name="label", field_type="str", required=True),
                        FieldSpec(name="output_gw", field_type="int", default=100, non_negative=True),
                    ],
                ),
                ObjectTypeBlueprint(
                    name="Starship",
                    primary_key="ship_id",
                    title_field="callsign",
                    fields=[
                        FieldSpec(name="ship_id", field_type="str", primary_key=True, required=True),
                        FieldSpec(name="callsign", field_type="str", required=True),
                        FieldSpec(name="sector", field_type="str", default="alpha"),
                        FieldSpec(name="fuel_cells", field_type="int", default=5, non_negative=True),
                    ],
                ),
            ],
            actions=[
                ActionBlueprint(
                    name="jump_sector",
                    allowed_roles=["navigator"],
                    approver_roles=[],
                    params=[
                        FieldSpec(name="ship_id", field_type="str", required=True),
                        FieldSpec(name="target_sector", field_type="str", required=True),
                        FieldSpec(name="cost_cells", field_type="int", default=2),
                    ],
                    preconditions=[
                        InvariantRuleSpec(
                            name="enough_fuel",
                            code="INSUFFICIENT_FUEL",
                            target_type="Starship",
                            id_param="ship_id",
                            must_hold={
                                "op": "ge",
                                "left": {"ref": "obj.fuel_cells"},
                                "right": {"ref": "param.cost_cells"},
                            },
                        )
                    ],
                    mutations=[
                        ActionMutationSpec(
                            op="modify",
                            object_type="Starship",
                            id_param="ship_id",
                            set_fields={
                                "sector": {"ref": "param.target_sector"},
                                "fuel_cells": {
                                    "op": "sub",
                                    "left": {"ref": "obj.fuel_cells"},
                                    "right": {"ref": "param.cost_cells"},
                                },
                            },
                        )
                    ],
                )
            ],
        )
        ctx = CrucibleContext(
            mode="genesis",
            m0_objects=[
                {"object_type": "WarpCore", "object_id": "WC-1", "data": {"core_id": "WC-1", "label": "Mark-IV", "output_gw": 100}},
                {"object_type": "Starship", "object_id": "SS-01", "data": {"ship_id": "SS-01", "callsign": "Enterprise", "sector": "alpha", "fuel_cells": 5}},
            ],
            positive_events=[
                ReplayEvent(
                    name="valid_jump",
                    action="jump_sector",
                    params={"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 2},
                    actor=nav,
                    expected_valid=True,
                )
            ],
            negative_events=[
                ReplayEvent(
                    name="empty_tank_jump",
                    action="jump_sector",
                    params={"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 99},
                    actor=nav,
                    expected_valid=False,
                    expected_error_code="INSUFFICIENT_FUEL",
                )
            ],
            default_actor=nav,
        )
        crucible_report = SymbolicCrucible().evaluate(blind_bp, ctx)
        assert crucible_report.passed is True, crucible_report.diagnostic_feedback()
    finally:
        rt.close()
