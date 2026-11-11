"""TK-ONTO-015 验收测试：Palantir OSv2 四大原语 + Workshop Lite 作战台。

覆盖：
1. ``Automations``：入库触发条件反射、按 ``(name, object_id, version)`` 去重、
   新版本可再次触发、处理器可开出 ``PENDING_APPROVAL`` 审批单；
2. ``Interfaces``：必填字段校验、``query_interface`` 跨类型多态、RLS 生效；
3. ``Column-Level Masking``：非授权角色读不到明文、授权角色读到明文、
   底层存储不受影响、链接与接口查询同样脱敏；
4. ``Column-wise MDO``：多数据源按主键按列拼装并重盖 overlay；
5. ``Conflict Strategy 2``：源时间戳更新则丢弃旧 overlay，否则保留；
6. ``WorkshopServer``：``GET /`` 返回交互式 HTML，JSON API 全链路可用。
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import BaseModel

from open_ontology import (
    Actor,
    InterfaceType,
    InvariantError,
    LinkType,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    WorkshopServer,
    start_workshop_server,
)

# ---------------------------------------------------------------------------
# 领域模型：钢材卷（带列级脱敏 + 本体自有字段）与废钢批（同接口、带 RLS）
# ---------------------------------------------------------------------------
spec = OntologySpec()
spec.interface("PricedAsset", ("asset_id", "unit_price_ton"), "可计价资产")


class SteelCoil(ObjectType):
    __primary_key__ = "asset_id"
    __title__ = "grade"
    __security_tag_fields__ = ("region",)
    __interfaces__ = ("PricedAsset",)
    __owned__ = {"quality_grade": "UNSET"}
    __masked_fields__ = {
        "unit_price_ton": ("finance",),
        "cost_note": ("finance", "auditor"),
        "internal_code": ("finance",),
    }
    __mask_defaults__ = {"internal_code": "REDACTED"}

    asset_id: str
    grade: str
    region: str = "north"
    unit_price_ton: float
    stock_tons: float = 0.0
    cost_note: str | None = None
    internal_code: str = "SECRET"
    quality_grade: str = "UNSET"


class ScrapLot(ObjectType):
    __primary_key__ = "asset_id"
    __title__ = "grade"
    __security_tag_fields__ = ("region",)
    __interfaces__ = ("PricedAsset",)
    __masked_fields__ = {"unit_price_ton": ("finance",)}

    asset_id: str
    grade: str
    region: str = "north"
    unit_price_ton: float
    stock_tons: float = 0.0


for _cls in (SteelCoil, ScrapLot):
    spec.object_type(_cls)
spec.add_link_type(
    LinkType(name="feeds", source="ScrapLot", target="SteelCoil", cardinality="many-to-many")
)


class EmptyParams(BaseModel):
    pass


class SetGradeParams(BaseModel):
    asset_id: str
    quality_grade: str


class AdjustPriceParams(BaseModel):
    asset_id: str
    unit_price_ton: float


@spec.function("portfolio_value", EmptyParams, description="库存货值合计（只读）")
def portfolio_value(ctx: Any, params: EmptyParams) -> dict[str, Any]:
    total = 0.0
    for coil in ctx.query(SteelCoil):
        total += coil.unit_price_ton * coil.stock_tons
    return {"total_value": round(total, 2)}


@spec.action(
    "set_quality_grade",
    SetGradeParams,
    allowed_roles=("ops", "finance", "admin"),
    affects_source=False,
)
def set_quality_grade(ctx: Any, params: SetGradeParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    ctx.modify(coil, quality_grade=params.quality_grade)


@spec.action("adjust_price", AdjustPriceParams, allowed_roles=("ops", "finance", "admin"))
def adjust_price(ctx: Any, params: AdjustPriceParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    if params.unit_price_ton <= 0:
        raise InvariantError("price must be positive", code="INVALID_PRICE")
    ctx.modify(coil, unit_price_ton=params.unit_price_ton)


@spec.action(
    "request_price_change",
    AdjustPriceParams,
    allowed_roles=("ops", "finance", "admin"),
    approver_roles=("finance_manager",),
    approval_rule=lambda params, actor: params.unit_price_ton > 20000,
    description="超过 2 万元/吨的改价需要财务经理批准",
)
def request_price_change(ctx: Any, params: AdjustPriceParams) -> None:
    coil = ctx.require(SteelCoil, params.asset_id)
    ctx.modify(coil, unit_price_ton=params.unit_price_ton)


AUTO_RUN_IDS = itertools.count(1)


@spec.automation(
    "fill_missing_grade",
    SteelCoil,
    lambda coil: coil.quality_grade == "UNSET",
    description="发现未定级的钢卷时自动提交定级动作",
)
def fill_missing_grade(runtime: OntologyRuntime, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    run = runtime.submit_action(
        f"AUTO-GRADE-{next(AUTO_RUN_IDS):04d}",
        "set_quality_grade",
        {"asset_id": coil.asset_id, "quality_grade": "PENDING_REVIEW"},
        actor,
    )
    return {"run_id": run["run_id"], "status": run["status"]}


@spec.automation(
    "log_low_stock",
    "SteelCoil",
    lambda coil: coil.stock_tons < 100,
    description="低库存只记录不改状态（用于验证版本去重）",
)
def log_low_stock(runtime: OntologyRuntime, coil: SteelCoil, actor: Actor) -> dict[str, Any]:
    return {"flagged": coil.asset_id, "stock_tons": coil.stock_tons}


@spec.automation(
    "escalate_premium_price",
    SteelCoil,
    lambda coil: coil.unit_price_ton > 20000,
    description="高价钢卷改价走审批流",
)
def escalate_premium_price(
    runtime: OntologyRuntime, coil: SteelCoil, actor: Actor
) -> dict[str, Any]:
    run = runtime.submit_action(
        f"AUTO-ESCALATE-{next(AUTO_RUN_IDS):04d}",
        "request_price_change",
        {"asset_id": coil.asset_id, "unit_price_ton": coil.unit_price_ton + 100.0},
        actor,
    )
    return {"run_id": run["run_id"], "status": run["status"]}


OPS = Actor(actor_id="ops-01", role="ops", clearance_tags={"*"})
FINANCE = Actor(actor_id="fin-01", role="finance", clearance_tags={"*"})
AUDITOR = Actor(actor_id="aud-01", role="auditor", clearance_tags={"*"})
NORTH_ONLY = Actor(actor_id="north-01", role="ops", clearance_tags={"region:north"})
SYSTEM = Actor(actor_id="system", role="system", clearance_tags={"*"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _seed(rt: OntologyRuntime) -> None:
    rt.put_object(
        SteelCoil(
            asset_id="C-1",
            grade="SUS304",
            unit_price_ton=18000.0,
            stock_tons=120.0,
            cost_note="主力现货",
        )
    )
    rt.put_object(
        SteelCoil(
            asset_id="C-2",
            grade="SUS316L",
            unit_price_ton=24000.0,
            stock_tons=80.0,
            cost_note="进口批次",
        )
    )
    rt.put_object(
        ScrapLot(
            asset_id="LOT-1",
            grade="废钢重料",
            region="south",
            unit_price_ton=300.0,
            stock_tons=50.0,
        )
    )
    rt.put_object(
        ScrapLot(
            asset_id="LOT-2",
            grade="废钢轻料",
            region="north",
            unit_price_ton=250.0,
            stock_tons=40.0,
        )
    )


@pytest.fixture()
def rt():
    runtime = OntologyRuntime(spec)
    _seed(runtime)
    yield runtime
    runtime.close()


# ---------------------------------------------------------------------------
# 1. Interfaces
# ---------------------------------------------------------------------------
def test_interface_requires_declared_fields():
    local = OntologySpec()
    local.interface("Named", ("asset_id", "display_name"))

    class Missing(ObjectType):
        __interfaces__ = ("Named",)
        asset_id: str

    with pytest.raises(TypeError) as excinfo:
        local.object_type(Missing)
    assert "display_name" in str(excinfo.value)

    class Unknown(ObjectType):
        __interfaces__ = ("NotRegistered",)
        asset_id: str

    with pytest.raises(Exception):
        local.object_type(Unknown)

    # 接口声明本身可被检索
    assert local.get_interface("Named").required_fields == ("asset_id", "display_name")


def test_query_interface_is_polymorphic(rt):
    rows = rt.query_interface("PricedAsset", SYSTEM)
    assert {type(row).__name__ for row in rows} == {"SteelCoil", "ScrapLot"}
    assert {row.asset_id for row in rows} == {"C-1", "C-2", "LOT-1", "LOT-2"}

    cheap = rt.query_interface("PricedAsset", SYSTEM, {"unit_price_ton": 300.0})
    assert [row.asset_id for row in cheap] == ["LOT-1"]


def test_query_interface_respects_row_level_security(rt):
    visible = rt.query_interface("PricedAsset", NORTH_ONLY)
    assert {row.asset_id for row in visible} == {"C-1", "C-2", "LOT-2"}
    assert "LOT-1" not in {row.asset_id for row in visible}


# ---------------------------------------------------------------------------
# 2. Column-Level Masking
# ---------------------------------------------------------------------------
def test_masking_hides_sensitive_columns_from_unauthorized_roles(rt):
    coil = rt.get_object(SteelCoil, "C-1", OPS)
    assert coil.unit_price_ton == 0.0
    assert coil.cost_note is None
    assert coil.internal_code == "REDACTED"
    assert coil.grade == "SUS304"  # 非敏感列照常返回

    finance_view = rt.get_object(SteelCoil, "C-1", FINANCE)
    assert finance_view.unit_price_ton == 18000.0
    assert finance_view.cost_note == "主力现货"

    auditor_view = rt.get_object(SteelCoil, "C-1", AUDITOR)
    assert auditor_view.unit_price_ton == 0.0
    assert auditor_view.cost_note == "主力现货"

    system_view = rt.get_object(SteelCoil, "C-1", SYSTEM)
    assert system_view.unit_price_ton == 18000.0
    assert system_view.internal_code == "SECRET"


def test_masking_does_not_touch_stored_state(rt):
    rt.get_object(SteelCoil, "C-1", OPS)
    raw = rt.list_objects("SteelCoil")
    stored = {row["object_id"]: row for row in raw}["C-1"]
    assert stored["properties"]["unit_price_ton"] == 18000.0
    assert stored["properties"]["cost_note"] == "主力现货"
    assert stored["properties"]["internal_code"] == "SECRET"


def test_masking_applies_to_queries_links_and_interfaces(rt):
    for coil in rt.query_objects(SteelCoil, OPS):
        assert coil.unit_price_ton == 0.0
    assert rt.search_set(SteelCoil, OPS).objects[0].unit_price_ton == 0.0

    lot = rt.get_object(ScrapLot, "LOT-1", SYSTEM)
    rt.link_objects(lot, "feeds", rt.get_object(SteelCoil, "C-1", SYSTEM))
    linked = rt.get_linked(lot, "feeds", SteelCoil, OPS)
    assert [c.unit_price_ton for c in linked] == [0.0]

    for row in rt.query_interface("PricedAsset", OPS):
        assert row.unit_price_ton == 0.0


def test_mask_defaults_fall_back_by_type():
    from open_ontology.runtime import _mask_value

    assert _mask_value(SteelCoil, "unit_price_ton", 18000.0) == 0.0
    assert _mask_value(SteelCoil, "grade", "SUS304") == "***MASKED***"
    assert _mask_value(SteelCoil, "cost_note", "x") is None
    assert _mask_value(SteelCoil, "internal_code", "SECRET") == "REDACTED"


# ---------------------------------------------------------------------------
# 3. Automations
# ---------------------------------------------------------------------------
def test_automation_fires_on_ingest_and_dedupes_per_version(rt):
    rt2 = OntologyRuntime(spec)
    try:
        rt2.put_object(
            SteelCoil(asset_id="A-1", grade="SUS304", unit_price_ton=15000.0, stock_tons=10.0),
            auto_trigger=True,
            actor=OPS,
        )
        history = rt2.automation_history()
        fired = [h for h in history if h["automation"] == "fill_missing_grade"]
        assert len(fired) == 1
        assert fired[0]["version"] == 1
        # 处理器真的执行了动作：质量等级已被写成本体自有字段
        coil = rt2.get_object(SteelCoil, "A-1", SYSTEM)
        assert coil.quality_grade == "PENDING_REVIEW"

        # 同一版本再次评估不会重复触发
        assert rt2.evaluate_automations(OPS) == []
        assert len(
            [h for h in rt2.automation_history() if h["automation"] == "fill_missing_grade"]
        ) == 1
    finally:
        rt2.close()


def test_automation_refires_only_after_new_version(rt):
    rt2 = OntologyRuntime(spec)
    try:
        rt2.put_object(
            SteelCoil(asset_id="A-2", grade="SUS304", unit_price_ton=15000.0, stock_tons=10.0)
        )
        assert len([h for h in rt2.evaluate_automations(OPS) if h["automation"] == "fill_missing_grade"]) == 1

        # 回退到未定级 → 版本 +1 → 允许再次触发
        rt2.submit_action(
            "MANUAL-1",
            "set_quality_grade",
            {"asset_id": "A-2", "quality_grade": "UNSET"},
            OPS,
        )
        again = [h for h in rt2.evaluate_automations(OPS) if h["automation"] == "fill_missing_grade"]
        assert len(again) == 1
        assert again[0]["version"] == 3
    finally:
        rt2.close()


def test_automation_can_open_pending_approval(rt):
    rt2 = OntologyRuntime(spec)
    try:
        rt2.put_object(
            SteelCoil(asset_id="A-3", grade="SUS316L", unit_price_ton=30000.0, stock_tons=5.0)
        )
        fired = rt2.evaluate_automations(OPS, automation="escalate_premium_price")
        assert len(fired) == 1
        assert fired[0]["result"]["status"] == "PENDING_APPROVAL"

        run_id = fired[0]["result"]["run_id"]
        approved = rt2.approve_action(
            run_id, Actor(actor_id="cfo", role="finance_manager", clearance_tags={"*"})
        )
        assert approved["status"] == "COMMITTED"
        assert rt2.get_object(SteelCoil, "A-3", SYSTEM).unit_price_ton == 30100.0
    finally:
        rt2.close()


def test_automation_history_is_recorded(rt):
    rt2 = OntologyRuntime(spec)
    try:
        rt2.put_object(
            SteelCoil(asset_id="A-4", grade="SUS304", unit_price_ton=10000.0, stock_tons=1.0)
        )
        rt2.evaluate_automations(OPS)
        names = {h["automation"] for h in rt2.automation_history()}
        assert {"fill_missing_grade", "log_low_stock"} <= names
        assert all("object_id" in h and "version" in h for h in rt2.automation_history())
    finally:
        rt2.close()


# ---------------------------------------------------------------------------
# 4. Column-wise MDO
# ---------------------------------------------------------------------------
def test_mdo_merges_columns_from_two_sources(rt):
    rt2 = OntologyRuntime(spec)
    try:
        summary = rt2.load_mdo_snapshots(
            SteelCoil,
            [
                {
                    "M-1": {"grade": "SUS304", "unit_price_ton": 17500.0},
                    "M-2": {"grade": "SUS316L", "unit_price_ton": 23500.0},
                },
                [
                    {"asset_id": "M-1", "stock_tons": 33.0},
                    {"asset_id": "M-2", "stock_tons": 44.0},
                ],
            ],
        )
        assert summary["merged_objects"] == 2
        assert summary["mdo_sources"][0]["columns"] == ["grade", "unit_price_ton"]
        assert summary["mdo_sources"][1]["columns"] == ["stock_tons"]

        coil = rt2.get_object(SteelCoil, "M-1", SYSTEM)
        assert (coil.grade, coil.unit_price_ton, coil.stock_tons) == ("SUS304", 17500.0, 33.0)
        assert coil.quality_grade == "UNSET"
    finally:
        rt2.close()


def test_mdo_reapplies_owned_overlay(rt):
    rt2 = OntologyRuntime(spec)
    try:
        rt2.put_object(
            SteelCoil(asset_id="M-9", grade="SUS304", unit_price_ton=10000.0, stock_tons=1.0)
        )
        rt2.submit_action(
            "GRADE-1",
            "set_quality_grade",
            {"asset_id": "M-9", "quality_grade": "A"},
            OPS,
        )
        rt2.load_mdo_snapshots(
            SteelCoil,
            [{"M-9": {"grade": "SUS304", "unit_price_ton": 11111.0, "stock_tons": 9.0}}],
        )
        coil = rt2.get_object(SteelCoil, "M-9", SYSTEM)
        assert coil.unit_price_ton == 11111.0
        assert coil.quality_grade == "A"  # 本体自有补丁必须存活
    finally:
        rt2.close()


# ---------------------------------------------------------------------------
# 5. Conflict Strategy 2 (most_recent_value)
# ---------------------------------------------------------------------------
def _seed_overlay(rt2: OntologyRuntime) -> None:
    rt2.put_object(
        SteelCoil(asset_id="W-1", grade="SUS304", unit_price_ton=10000.0, stock_tons=1.0)
    )
    rt2.submit_action(
        "GRADE-W1",
        "set_quality_grade",
        {"asset_id": "W-1", "quality_grade": "A"},
        OPS,
    )


def test_lww_source_newer_drops_stale_overlay(rt):
    rt2 = OntologyRuntime(spec)
    try:
        _seed_overlay(rt2)
        summary = rt2.load_snapshot(
            {
                "SteelCoil": [
                    {"asset_id": "W-1", "grade": "SUS304", "unit_price_ton": 12000.0, "stock_tons": 2.0}
                ]
            },
            conflict_strategy="most_recent_value",
            source_timestamps={("SteelCoil", "W-1"): "2099-01-01T00:00:00+00:00"},
        )
        assert summary["overlays_dropped"] == ["SteelCoil:W-1"]
        coil = rt2.get_object(SteelCoil, "W-1", SYSTEM)
        assert coil.quality_grade == "UNSET"  # 源系统最新值胜出
        assert coil.unit_price_ton == 12000.0
    finally:
        rt2.close()


def test_lww_overlay_newer_keeps_patch(rt):
    rt2 = OntologyRuntime(spec)
    try:
        _seed_overlay(rt2)
        summary = rt2.load_snapshot(
            {
                "SteelCoil": [
                    {"asset_id": "W-1", "grade": "SUS304", "unit_price_ton": 12000.0, "stock_tons": 2.0}
                ]
            },
            conflict_strategy="most_recent_value",
            source_timestamps={("SteelCoil", "W-1"): "2000-01-01T00:00:00+00:00"},
        )
        assert summary["overlays_kept"] == ["SteelCoil:W-1"]
        coil = rt2.get_object(SteelCoil, "W-1", SYSTEM)
        assert coil.quality_grade == "A"  # 用户补丁胜出
        assert coil.unit_price_ton == 12000.0
    finally:
        rt2.close()


def test_lww_default_strategy_is_user_edits_win(rt):
    rt2 = OntologyRuntime(spec)
    try:
        _seed_overlay(rt2)
        summary = rt2.load_snapshot(
            {
                "SteelCoil": [
                    {"asset_id": "W-1", "grade": "SUS304", "unit_price_ton": 12000.0, "stock_tons": 2.0}
                ]
            },
            source_timestamps={("SteelCoil", "W-1"): "2099-01-01T00:00:00+00:00"},
        )
        assert summary["conflict_strategy"] == "user_edits_win"
        assert summary["overlays_dropped"] == []
        assert rt2.get_object(SteelCoil, "W-1", SYSTEM).quality_grade == "A"
        with pytest.raises(ValueError):
            rt2.load_snapshot({}, conflict_strategy="nope")
    finally:
        rt2.close()


# ---------------------------------------------------------------------------
# 6. WorkshopServer
# ---------------------------------------------------------------------------
def _http(url: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.fixture()
def workshop(rt):
    handle = start_workshop_server(rt, port=0)
    yield handle
    handle.stop()


def test_workshop_index_returns_interactive_html(workshop):
    with urllib.request.urlopen(workshop.base_url + "/", timeout=10) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        html = response.read().decode("utf-8")
    for marker in (
        "<!doctype html>",
        "统一作战指挥中心",
        "OBJECT EXPLORER",
        "GOVERNED ACTION",
        "ACTION RUNS",
        "当前状态指纹",
        "/api/simulate",
        "/api/submit",
        "/api/automations/run",
        "自动化规则",
    ):
        assert marker.lower() in html.lower(), marker
    assert WorkshopServer is not None


def test_workshop_meta_and_fingerprint(workshop):
    meta = _http(workshop.base_url + "/api/meta")
    assert meta["ok"] is True
    assert {t["name"] for t in meta["object_types"]} == {"SteelCoil", "ScrapLot"}
    assert [i["name"] for i in meta["interfaces"]] == ["PricedAsset"]
    assert {a["name"] for a in meta["automations"]} == {
        "fill_missing_grade",
        "log_low_stock",
        "escalate_premium_price",
    }
    assert {a["name"] for a in meta["actions"]} >= {"adjust_price", "set_quality_grade"}

    fingerprint = _http(workshop.base_url + "/api/fingerprint")
    assert fingerprint["objects"] == 4
    assert fingerprint["runs"] == 0


def test_workshop_simulate_and_submit(workshop):
    sim = _http(
        workshop.base_url + "/api/simulate",
        {"action": "adjust_price", "params": {"asset_id": "C-1", "unit_price_ton": 19000.0}},
    )
    assert sim["valid"] is True
    assert sim["diff"]["object_changes"][0]["after"]["unit_price_ton"] == 19000.0
    # 干跑不落库
    assert _http(workshop.base_url + "/api/fingerprint")["runs"] == 0

    submitted = _http(
        workshop.base_url + "/api/submit",
        {
            "run_id": "WS-1",
            "action": "adjust_price",
            "params": {"asset_id": "C-1", "unit_price_ton": 19000.0},
        },
    )
    assert submitted["run"]["status"] == "COMMITTED"
    runs = _http(workshop.base_url + "/api/action-runs")["runs"]
    assert [r["run_id"] for r in runs] == ["WS-1"]

    bad = _http(
        workshop.base_url + "/api/simulate",
        {"action": "adjust_price", "params": {"asset_id": "C-1", "unit_price_ton": -1}},
    )
    assert bad["valid"] is False
    assert bad["error_code"] == "INVALID_PRICE"


def test_workshop_automations_approve_and_revert(workshop):
    # 1) 提交一笔超限改价 → 进入 PENDING_APPROVAL
    pending = _http(
        workshop.base_url + "/api/submit",
        {
            "run_id": "WS-PENDING-1",
            "action": "request_price_change",
            "params": {"asset_id": "C-1", "unit_price_ton": 25000.0},
        },
    )
    assert pending["run"]["status"] == "PENDING_APPROVAL"

    # 2) 财务经理批准
    approved = _http(
        workshop.base_url + "/api/approve?role=finance_manager",
        {"run_id": "WS-PENDING-1"},
    )
    assert approved["run"]["status"] == "COMMITTED"
    assert approved["run"]["approved_by"] == "workshop:finance_manager"

    # 3) 时光机撤回
    reverted = _http(
        workshop.base_url + "/api/revert",
        {"run_id": "WS-PENDING-1", "revert_run_id": "WS-REV-1"},
    )
    assert reverted["run"]["status"] == "COMMITTED"
    statuses = {
        run["run_id"]: run["status"]
        for run in _http(workshop.base_url + "/api/action-runs")["runs"]
    }
    assert statuses["WS-PENDING-1"] == "REVERTED"

    # 4) 一键触发 evaluate_automations
    fired = _http(workshop.base_url + "/api/automations/run", {})
    assert fired["ok"] is True
    assert fired["count"] >= 1
    assert all("automation" in item for item in fired["fired"])
    history = _http(workshop.base_url + "/api/automations/history")["history"]
    assert {h["automation"] for h in history} >= {"fill_missing_grade", "log_low_stock"}


def test_workshop_masks_by_role(workshop):
    ops_view = _http(workshop.base_url + "/api/objects/SteelCoil?role=ops")
    assert ops_view["objects"][0]["unit_price_ton"] == 0.0
    finance_view = _http(workshop.base_url + "/api/objects/SteelCoil?role=finance")
    assert finance_view["objects"][0]["unit_price_ton"] in (18000.0, 24000.0)
