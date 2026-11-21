"""架构红线统一门禁测试（Track A · 5 道结构性自动门禁）。

覆盖两类架构反污染与纯同步架构红线，彻底替代碎片化手工黑名单：
1. 门禁 1（技术不污染业务 · 动态发现 + AST 导入白名单 + 禁动态导入）：动态发现仓库内全部
   纯 M1 本体声明模块，禁止直接/间接/动态 import ``OntologyRuntime``、执行传输、IO、OS 或外部系统库；
2. 门禁 2（M1 不污染 M2 · 递归边界 + 禁动态导入 + 运行时子类白名单）：递归扫描 ``open_ontology/**/*.py``，
   严禁静态或动态反向引用 ``examples``、严禁非豁免模块引入传输 I/O、通过 AST 与运行时递归
   ``ObjectType.__subclasses__()`` 校验唯一允许定义实体子类的模块仅为元自举治理与平台元本体；
3. 门禁 3（动态 M1 结构词表反向扫描 + 供应商词禁令）：动态加载仓库内全部 M1 本体注册词表，
   词边界正则反向扫描 ``open_ontology/**`` 代码、非 docstring 字符串与 ``ui_display.json``，并封杀
   任何供应商耦合标识；
4. 门禁 4（M2 跨领域盲测）：使用完全虚构领域（``Starship / WarpCore / Sector``）
   端到端跑通 ``OntologyRuntime`` / ``OntologyMCTSPlanner`` / ``describe_tbox`` / ``SymbolicCrucible``；
5. 门禁 5（纯同步无挂起 M2 铁笼）：校验 ``open_ontology/schema.sql`` 六表结构及动作账本状态约束仅允许
   ``('COMMITTED', 'REVERTED', 'REJECTED')``，并校验 ``open_ontology/**`` 对退役 M2 挂起/两相审批符号零命中。
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
import re
from pathlib import Path

from pydantic import BaseModel

import open_ontology
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
from open_ontology.mcts import OntologyMCTSPlanner
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
    ("examples.std_core_ontology", "spec"),
    ("examples.std_crm_ontology", "spec"),
    ("examples.std_o2c_ontology", "spec"),
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
        "open_ontology.induction.dsl",
        "examples.business_ontology",
        "examples.std_core_ontology",
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


_VENDOR_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(odoo|bottlecrm|feishu|lark|salesforce|erpnext)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

_RETIRED_M2_STAGING_SYMBOLS: tuple[str, ...] = (
    "PENDING_APPROVAL",
    "AUDIT_STATUS_PENDING",
    "ApprovalGateAspect",
    "settle_approval",
    "approve_action",
    "reject_action",
    "approval_rule",
    "approver_roles",
    "post_approval",
)


_DYNAMIC_IMPORT_EXEMPT_FILES = frozenset(
    {
        (PKG_ROOT / "design.py").resolve(),
    }
)


def _iter_kernel_py_files() -> list[Path]:
    return sorted(
        p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts
    )


def _find_dynamic_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Detect ``importlib.import_module``, ``import_module``, and ``__import__`` calls."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in ("import_module", "__import__"):
                hits.append((node.lineno, f"{fn.id}()"))
            elif isinstance(fn, ast.Attribute) and fn.attr in ("import_module", "__import__"):
                hits.append((node.lineno, f"{ast.unparse(fn)}()"))
            elif (
                isinstance(fn, ast.Name)
                and fn.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in ("import_module", "__import__")
            ):
                hits.append((node.lineno, f"getattr(..., {node.args[1].value!r})()"))
    return hits


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
# Gate 1: M1 纯业务声明层 动态发现 + AST 导入白名单 + 禁动态导入
# ---------------------------------------------------------------------------
def test_gate1_m1_ontologies_strict_ast_import_whitelist() -> None:
    """所有纯 M1 本体声明模块自动被纳管，且只能依赖数据类型库与 open_ontology.spec。"""
    discovered_m1 = {
        f"examples.{p.stem}"
        for p in (REPO_ROOT / "examples").glob("*_ontology.py")
    } | {"examples.kanban"}
    registered_m1 = {mod_name for mod_name, _ in M1_ONTOLOGY_MODULES}
    assert discovered_m1 == registered_m1, (
        "M1 本体自动发现清单与 M1_ONTOLOGY_MODULES 不一致："
        f"missing={sorted(discovered_m1 - registered_m1)}, extra={sorted(registered_m1 - discovered_m1)}"
    )

    for mod_name, _ in M1_ONTOLOGY_MODULES:
        rel_path = Path(*mod_name.split(".")).with_suffix(".py")
        path = REPO_ROOT / rel_path
        assert path.is_file(), f"缺少纯 M1 模块 {rel_path}"

        source = path.read_text(encoding="utf-8")
        assert "OntologyRuntime" not in source, (
            f"M1 本体 {rel_path} 不得引用或构造 OntologyRuntime"
        )

        tree = ast.parse(source, filename=str(rel_path))
        dyn_hits = _find_dynamic_imports(tree)
        assert dyn_hits == [], f"M1 本体 {rel_path} 禁止动态导入绕过：{dyn_hits}"

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in _ALLOWED_M1_STDLIB_AND_SDK, (
                        f"M1 本体 {rel_path} 禁止 import {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level and mod in ("business_ontology", "std_core_ontology"):
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
# Gate 2: M2 递归目录隔离、禁动态导入与 AST+运行时 ObjectType 子类白名单
# ---------------------------------------------------------------------------
def test_gate2_m2_zero_reverse_example_imports_no_business_objects_clean_io() -> None:
    """M2 内核递归门禁：禁 static/dynamic import examples，禁非法 IO，禁夹带业务 ObjectType。"""
    meta_ontology_paths = {
        (PKG_ROOT / "induction" / "governance.py").resolve(),
        (PKG_ROOT / "meta_admin.py").resolve(),
    }
    meta_ontology_modules = frozenset(
        {
            "open_ontology.induction.governance",
            "open_ontology.meta_admin",
        }
    )
    files = _iter_kernel_py_files()
    assert len(files) >= 15

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)

        # 2a. 禁止任何层次的静态与动态反向 import（含 import_module / __import__）
        if path.resolve() not in _DYNAMIC_IMPORT_EXEMPT_FILES:
            dyn_hits = _find_dynamic_imports(tree)
            assert dyn_hits == [], f"M2 内核 {rel} 禁止动态导入逃逸：{dyn_hits}"
        else:
            # design.py 仅允许使用 importlib.import_module 解析命令行显式传入路径，仍禁 __import__ 与硬编码 examples 字符串
            for ln, call_desc in _find_dynamic_imports(tree):
                assert "__import__" not in call_desc, f"{rel}:{ln} 禁止 __import__"
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert "examples" not in node.value.split("."), (
                        f"{rel} 不得硬编码 examples 引用 {node.value!r}"
                    )

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

        # 2c (AST). 内核里唯一允许定义直接 ObjectType 子类的是元自举治理本体与平台自举元本体
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

    # 2c (Runtime). 动态导入全部 open_ontology.* 子模块并递归遍历 ObjectType.__subclasses__()（覆盖间接继承）
    for pkg_info in pkgutil.walk_packages(open_ontology.__path__, open_ontology.__name__ + "."):
        importlib.import_module(pkg_info.name)
    seen: set[type[ObjectType]] = set()
    stack: list[type[ObjectType]] = list(ObjectType.__subclasses__())
    bad_subclasses: list[tuple[str, str]] = []
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        if cls.__module__.startswith("open_ontology") and cls.__module__ not in meta_ontology_modules:
            bad_subclasses.append((cls.__module__, cls.__qualname__))
    assert bad_subclasses == [], (
        f"M2 运行时检测到非法 ObjectType 间接/直接子类：{sorted(bad_subclasses)}"
    )


# ---------------------------------------------------------------------------
# Gate 3: 动态收集 M1 结构词表，词边界正则扫描 open_ontology/** + 供应商词守卫
# ---------------------------------------------------------------------------
def test_gate3_dynamic_m1_vocabulary_reverse_scan() -> None:
    """动态加载全部 M1 本体词表，对内核源码、字符串做词边界反向扫描，并拦截供应商硬编码。"""
    structural_tokens: set[str] = {"task_board"}
    ontology_ids: set[str] = {"task_board"}

    for mod_name, attr in M1_ONTOLOGY_MODULES:
        mod = importlib.import_module(mod_name)
        target = getattr(mod, attr)
        spec: OntologySpec = target() if callable(target) else target
        structural_tokens.update(spec.object_types.keys())
        structural_tokens.update(spec.abstract_types.keys())
        structural_tokens.update(spec.object_properties.keys())
        structural_tokens.update(spec.interfaces.keys())
        structural_tokens.update(spec.actions.keys())
        structural_tokens.update(spec.functions.keys())
        structural_tokens.update(spec.automations.keys())
        structural_tokens.update(spec.invariants.keys())
        structural_tokens.update(spec.transition_invariants.keys())
        ont_keys = set((spec.display.get("ontologies") or {}).keys())
        structural_tokens.update(ont_keys)
        ontology_ids.update(ont_keys)

    assert len(structural_tokens) >= 120
    distinctive_tokens = sorted(
        t for t in structural_tokens if "_" in t or any(c.isupper() for c in t)
    )
    assert len(distinctive_tokens) >= 110
    word_boundary_re = re.compile(
        r"(?<![A-Za-z0-9_-])("
        + "|".join(re.escape(tok) for tok in distinctive_tokens)
        + r")(?![A-Za-z0-9_-])"
    )

    # 3a. ui_display.json 不允许保留任何具体 M1 词条、本体名或供应商词
    ui_display = json.loads((PKG_ROOT / "ui_display.json").read_text(encoding="utf-8"))
    ui_blob = json.dumps(ui_display, ensure_ascii=False)
    leaked_ui = sorted(tok for tok in structural_tokens if tok in ui_blob)
    assert leaked_ui == [], f"ui_display.json 残留 M1 业务标识：{leaked_ui}"
    vendor_ui = sorted({m.group(0).lower() for m in _VENDOR_WORD_RE.finditer(ui_blob)})
    assert vendor_ui == [], f"ui_display.json 残留外部供应商标识：{vendor_ui}"

    # 3b. open_ontology/**/*.py AST 标识符、非 docstring 字符串与供应商词守卫
    leaks: list[tuple[str, list[str]]] = []
    vendor_leaks: list[tuple[str, list[str]]] = []
    for path in _iter_kernel_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc_ids = _docstring_node_ids(tree)
        idents: set[str] = set()
        strings: list[str] = []
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
                strings.append(node.value)

        string_set = set(strings)
        exact_hits = {t for t in structural_tokens if t in idents or t in string_set}
        embedded_hits = {
            m.group(1)
            for s in strings
            for m in word_boundary_re.finditer(s)
        }
        id_sub_hits = {
            oid
            for oid in ontology_ids
            if any(oid in s for s in strings)
        }
        all_hits = sorted(exact_hits | embedded_hits | id_sub_hits)
        if all_hits:
            leaks.append((str(path.relative_to(REPO_ROOT)), all_hits))

        v_hits = sorted(
            {
                m.group(0).lower()
                for text in (*idents, *strings)
                for m in _VENDOR_WORD_RE.finditer(text)
            }
        )
        if v_hits:
            vendor_leaks.append((str(path.relative_to(REPO_ROOT)), v_hits))

    assert leaks == [], f"M1 结构标识反向泄漏进 open_ontology/：{leaks}"
    assert vendor_leaks == [], f"外部供应商标识泄漏进 open_ontology/：{vendor_leaks}"


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

        with OntologyMCTSPlanner(
            rt,
            nav,
            candidate_table=[[("jump_sector", {"ship_id": "SS-01", "target_sector": "omega", "cost_cells": 2})]],
            value_function="fleet_fuel_total",
            simulations=8,
        ) as planner:
            plan = planner.search()
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
                    revert_roles=[],
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


# ---------------------------------------------------------------------------
# Gate 5: 纯同步无挂起 M2 铁笼（schema 约束 + 退役审批符号零命中）
# ---------------------------------------------------------------------------
def test_gate5_pure_synchronous_non_suspending_m2_kernel() -> None:
    """schema.sql 仅保留 6 表且无异步挂起状态；open_ontology/** 对退役 M2 挂起符号零命中。"""
    schema_text = (PKG_ROOT / "schema.sql").read_text(encoding="utf-8")
    tables = set(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)",
            schema_text,
        )
    )
    assert tables == {
        "ontology_write_gate",
        "ontology_registry",
        "ontology_objects",
        "ontology_links",
        "ontology_action_runs",
        "ontology_overlay",
    }

    check_match = re.search(
        r"CREATE TABLE IF NOT EXISTS ontology_action_runs\b.*?CHECK\s*\(\s*status IN\s*\(([^)]+)\)\s*\)",
        schema_text,
        re.DOTALL,
    )
    assert check_match is not None, "schema.sql 缺少 ontology_action_runs.status CHECK 约束"
    statuses = set(re.findall(r"'([^']+)'", check_match.group(1)))
    assert statuses == {"COMMITTED", "REVERTED", "REJECTED"}, (
        f"ontology_action_runs.status 仅允许纯同步状态集，实际：{statuses}"
    )
    for forbidden_state in ("PENDING_APPROVAL", "IN_FLIGHT", "WAITING_CALLBACK"):
        assert forbidden_state not in schema_text

    retired_re = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(re.escape(sym) for sym in _RETIRED_M2_STAGING_SYMBOLS)
        + r")(?![A-Za-z0-9_])"
    )
    raw_hits: list[tuple[str, list[str]]] = []
    ast_hits: list[tuple[str, list[str]]] = []

    for path in sorted(PKG_ROOT.rglob("*")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        if path.suffix not in (".py", ".sql", ".json"):
            continue
        text = path.read_text(encoding="utf-8")
        found_raw = sorted({m.group(1) for m in retired_re.finditer(text)})
        if found_raw:
            raw_hits.append((str(path.relative_to(REPO_ROOT)), found_raw))
        if path.suffix == ".py":
            tree = ast.parse(text, filename=str(path))
            doc_ids = _docstring_node_ids(tree)
            tokens: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    tokens.add(node.id)
                elif isinstance(node, ast.Attribute):
                    tokens.add(node.attr)
                elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    tokens.add(node.name)
                elif isinstance(node, ast.alias):
                    tokens.add(node.name.split(".")[-1])
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in doc_ids
                ):
                    tokens.update(m.group(1) for m in retired_re.finditer(node.value))
            found_ast = sorted(t for t in tokens if retired_re.fullmatch(t))
            if found_ast:
                ast_hits.append((str(path.relative_to(REPO_ROOT)), found_ast))

    assert raw_hits == [], f"open_ontology/** 原文残留退役 M2 挂起符号：{raw_hits}"
    assert ast_hits == [], f"open_ontology/** AST 残留退役 M2 挂起符号：{ast_hits}"


# ---------------------------------------------------------------------------
# Gate 6: Runtime 对 Design-Time 一无所知（AST + 依赖闭包 + 干净子进程 sys.modules）
# ---------------------------------------------------------------------------
_RUNTIME_CORE_FILES: tuple[str, ...] = (
    "spec.py",
    "sandbox.py",
    "runtime.py",
    "aspects.py",
    "settlement.py",
    "drivers.py",
    "ingestion.py",
    "query.py",
    "osdk.py",
)

_FORBIDDEN_IN_RUNTIME_MODULES: frozenset[str] = frozenset(
    {
        "design",
        "induction",
        "crucible",
        "falsifier",
        "decompiler",
        "agents",
        "backends",
        "dsl",
        "phenomenon",
        "resolution",
        "governance",
        "workshop",
        "workshop_ui",
        "meta_admin",
        "workspaces",
        "model_views",
        "http_server",
        "webhooks",
        "mcts",
        "mpc",
    }
)

_RUNTIME_WHITELIST_MODULES: frozenset[str] = frozenset(
    {
        "open_ontology",
        "open_ontology.spec",
        "open_ontology.sandbox",
        "open_ontology.runtime",
        "open_ontology.aspects",
        "open_ontology.settlement",
        "open_ontology.drivers",
        "open_ontology.ingestion",
        "open_ontology.query",
        "open_ontology.osdk",
    }
)


def _internal_import_targets(
    mod_path: Path,
    tree: ast.AST,
) -> tuple[list[tuple[int, str]], list[str]]:
    """Return ``(all_imports, open_ontology_targets)`` respecting relative imports."""
    rel_name = mod_path.relative_to(PKG_ROOT).with_suffix("")
    pkg_parts = ("open_ontology",) + rel_name.parts[:-1]
    all_imports: list[tuple[int, str]] = []
    internal_targets: list[str] = []

    def _visit(node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                all_imports.append((node.lineno, alias.name))
                if alias.name == "open_ontology" or alias.name.startswith("open_ontology."):
                    internal_targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0:
                up = node.level - 1
                base_parts = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else ()
                full_mod = ".".join((*base_parts, mod)) if mod else ".".join(base_parts)
                all_imports.append((node.lineno, full_mod))
                for alias in node.names:
                    cand = f"{full_mod}.{alias.name}" if full_mod else alias.name
                    if full_mod == "open_ontology":
                        all_imports.append((node.lineno, cand))
                        internal_targets.append(cand)
                    elif full_mod.startswith("open_ontology."):
                        internal_targets.append(full_mod)
            else:
                all_imports.append((node.lineno, mod))
                if mod == "open_ontology":
                    for alias in node.names:
                        cand = f"open_ontology.{alias.name}"
                        all_imports.append((node.lineno, cand))
                        internal_targets.append(cand)
                elif mod.startswith("open_ontology."):
                    internal_targets.append(mod)
        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(tree)
    return all_imports, internal_targets


def test_gate6_runtime_oblivious_to_design_time() -> None:
    """零例外铁笼版：核心 Runtime 对 Design-Time/Induction/Workshop/MCTS/MPC/ModelViews 一无所知。"""
    import subprocess
    import sys

    # 6a. 静态 AST 导入封杀 + 禁动态导入（0 例外）
    for fname in _RUNTIME_CORE_FILES:
        path = PKG_ROOT / fname
        assert path.is_file(), f"缺少 Runtime 核心模块 {fname}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dyn_hits = _find_dynamic_imports(tree)
        assert dyn_hits == [], f"Runtime 核心模块 {fname} 禁止动态导入：{dyn_hits}"

        all_imports, _ = _internal_import_targets(path, tree)
        for lineno, target in all_imports:
            parts = target.split(".")
            assert parts[0] != "subprocess", (
                f"Runtime 核心模块 {fname}:{lineno} 禁止引入 subprocess ({target!r})"
            )
            if parts[0] == "open_ontology":
                bad_segs = [p for p in parts[1:] if p in _FORBIDDEN_IN_RUNTIME_MODULES]
                assert bad_segs == [], (
                    f"Runtime 核心模块 {fname}:{lineno} 禁止引用非 Runtime 模块 {target!r}"
                )
            elif parts[0] in _FORBIDDEN_IN_RUNTIME_MODULES - {"agents"}:
                raise AssertionError(
                    f"Runtime 核心模块 {fname}:{lineno} 禁止引入 {target!r}"
                )

    # 6b. 顶层 open_ontology/__init__.py 仅允许静态导入纯 Runtime 核心子模块，严禁 __getattr__ 后门
    init_path = PKG_ROOT / "__init__.py"
    init_tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    assert _find_dynamic_imports(init_tree) == [], "open_ontology/__init__.py 禁止动态导入"
    has_getattr = [
        node.lineno
        for node in ast.walk(init_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
    ]
    assert has_getattr == [], f"open_ontology/__init__.py 禁止 __getattr__ 延迟后门：{has_getattr}"

    _, init_targets = _internal_import_targets(init_path, init_tree)
    for target in init_targets:
        normalized = ".".join(target.split(".")[:2])
        assert normalized in _RUNTIME_WHITELIST_MODULES, (
            f"open_ontology/__init__.py 严禁暴露/加载非 Runtime 模块 {target!r}"
        )

    # 6c. 递归解析顶层入口与 Runtime 核心模块静态依赖闭包（0 例外）
    visited: set[str] = set()
    queue = [
        "open_ontology",
        "open_ontology.runtime",
        "open_ontology.drivers",
        "open_ontology.osdk",
    ]
    while queue:
        mod_name = queue.pop(0)
        if mod_name in visited:
            continue
        visited.add(mod_name)
        mod_file = (
            PKG_ROOT / "__init__.py"
            if mod_name == "open_ontology"
            else REPO_ROOT / Path(*mod_name.split(".")).with_suffix(".py")
        )
        assert mod_file.is_file(), f"缺少闭包模块文件 {mod_file}"
        mtree = ast.parse(mod_file.read_text(encoding="utf-8"), filename=str(mod_file))
        _, internal_deps = _internal_import_targets(mod_file, mtree)
        for dep in internal_deps:
            normalized = ".".join(dep.split(".")[:2])
            assert normalized in _RUNTIME_WHITELIST_MODULES, (
                f"{mod_name} 的依赖链越界引用了 {dep!r}"
            )
            if normalized not in visited:
                queue.append(normalized)

    # 6d. 全新干净子进程 sys.modules 实测：严格落于纯 Runtime 10 模块白名单，零泄漏扩展模块
    allowed_literal = repr(sorted(_RUNTIME_WHITELIST_MODULES))
    banned_segs_literal = repr(
        [
            "design",
            "induction",
            "workshop",
            "workshop_ui",
            "meta_admin",
            "workspaces",
            "model_views",
            "http_server",
            "webhooks",
            "mcts",
            "mpc",
        ]
    )
    probe_code = (
        "import sys; "
        "import open_ontology; "
        "from open_ontology import OntologyRuntime, DeviceDriverRegistry, AIPAgentExecutor; "
        f"allowed = set({allowed_literal}); "
        f"banned = {banned_segs_literal}; "
        "loaded = {m for m in sys.modules if m == 'open_ontology' or m.startswith('open_ontology.')}; "
        "extra = sorted(loaded - allowed); "
        "assert not extra, f'unexpected modules loaded by open_ontology: {extra}'; "
        "leaked = [m for m in loaded if any(m == f'open_ontology.{b}' or m.startswith(f'open_ontology.{b}.') for b in banned)]; "
        "assert not leaked, f'banned modules loaded: {leaked}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"subprocess sys.modules check failed: {proc.stderr or proc.stdout}"

    # 6e. 直接导入 Runtime 核心子模块在干净进程下也严格落于纯 Runtime 白名单
    drv_probe = (
        "import sys; "
        "from open_ontology.drivers import DeviceDriverRegistry; "
        "import open_ontology.runtime, open_ontology.settlement, open_ontology.osdk; "
        f"allowed = set({allowed_literal}); "
        "loaded = {m for m in sys.modules if m == 'open_ontology' or m.startswith('open_ontology.')}; "
        "extra = sorted(loaded - allowed); "
        "assert not extra, f'runtime loaded extra modules: {extra}'"
    )
    drv_proc = subprocess.run(
        [sys.executable, "-c", drv_probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert drv_proc.returncode == 0, (
        f"drivers subprocess sys.modules check failed: {drv_proc.stderr or drv_proc.stdout}"
    )
