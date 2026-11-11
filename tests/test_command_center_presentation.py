"""TK-ONTO-019 验收测试：指挥台中文展示层 + 拓扑独立大图。

先行的失败测试覆盖：
1. ``/topology?ontology=...`` 可直接 GET、刷新、独立访问，未知 ID 中文报错；
2. 中文展示映射与真实 API 标识严格分离，未知标识安全回退；
3. 主界面中文主文案、拓扑预览链接、角色 value 不变；
4. 真实浏览器：新标签页、按钮缩放、鼠标拖拽、滚轮缩放及上下界、复位、
   适应窗口、独立刷新、空图、控制台无错误；
5. 研发工厂 / 冷链 / 钢厂三种蓝图 + 两个实例的中文名称不串台。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from open_ontology import (
    OntologyRuntime,
    OntologySpec,
    start_command_center_server,
)
from open_ontology.workshop import CommandCenterRegistry
from examples.command_center_demo import build_default_command_center

EVIDENCE_DIR = Path(
    "/private/var/folders/nj/9yhn6dcd1xl3c7v6zs7fdqn40000gn/T/opencode/tk019_evidence"
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _request(url: str, body: dict | None = None) -> tuple[int, dict | str, str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            return response.status, json.loads(raw), content_type
        return response.status, raw, content_type


def _get_json(base: str, path: str, **params) -> dict:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    _, payload, _ = _request(f"{base}{path}" + (f"?{query}" if query else ""))
    return payload  # type: ignore[return-value]


def _post_json(base: str, path: str, body: dict) -> dict:
    _, payload, _ = _request(f"{base}{path}", body)
    return payload  # type: ignore[return-value]


def _html(url: str) -> str:
    status, payload, content_type = _request(url)
    assert status == 200
    assert content_type.startswith("text/html")
    return payload  # type: ignore[return-value]


@pytest.fixture()
def live():
    registry = build_default_command_center()
    handle = start_command_center_server(
        registry, port=0, title="OpenOntology 统一作战指挥中心"
    )
    yield registry, handle
    handle.stop()


@pytest.fixture()
def base(live):
    return live[1].base_url


# ---------------------------------------------------------------------------
# 1. 拓扑独立页（无浏览器）
# ---------------------------------------------------------------------------
def test_topology_page_served_and_refreshable(base):
    url = base + "/topology?ontology=software_factory"
    first = _html(url)
    second = _html(url)  # 独立刷新 / 直接访问
    assert first == second
    for marker in (
        "本体关系图",
        "放大",
        "缩小",
        "适应窗口",
        "复位",
        "返回指挥台",
        'id="canvas"',
        'id="scene"',
        "缩放比例",
        "target=",
    ):
        assert marker in first, marker
    assert "<script src" not in first
    assert "cdn." not in first.lower()


def test_topology_page_unknown_ontology_has_chinese_error(base):
    html = _html(base + "/topology?ontology=no-such-blueprint")
    assert "未找到该本体蓝图" in html
    assert "返回指挥台" in html
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get_json(base, "/api/ontology/no-such-blueprint")
    assert exc.value.code == 404


def test_topology_page_does_not_echo_query(base):
    payload = quote("<script>alert(1)</script>", safe="")
    html = _html(base + "/topology?ontology=" + payload)
    assert "<script>alert(1)</script>" not in html
    assert "alert(1)" not in html


def test_empty_ontology_topology_page():
    registry = CommandCenterRegistry()
    empty_spec = OntologySpec()
    runtime = OntologyRuntime(empty_spec)
    registry.register_ontology(
        "empty", "空蓝图", empty_spec, category="Enterprise", description="无对象"
    )
    registry.register_instance(
        "empty-instance", "empty", "空实例", runtime, db_uri="sqlite://:memory:"
    )
    handle = start_command_center_server(registry, port=0)
    try:
        html = _html(handle.base_url + "/topology?ontology=empty")
        assert "该本体暂无对象类型" in html
        detail = _get_json(handle.base_url, "/api/ontology/empty")
        assert detail["object_types"] == []
        assert detail["link_types"] == []
    finally:
        handle.stop()
        runtime.close()


# ---------------------------------------------------------------------------
# 2. 展示映射与 API 标识分离
# ---------------------------------------------------------------------------
def test_display_mapping_covers_default_blueprints(live):
    from open_ontology.workshop_ui import DISPLAY, label

    registry, _ = live
    for entry in registry.ontologies.values():
        spec = entry.spec
        for name in spec.object_types:
            assert DISPLAY["objects"].get(name), f"missing object label: {name}"
        for name in spec.link_types:
            assert DISPLAY["links"].get(name), f"missing link label: {name}"
        for name in spec.actions:
            assert DISPLAY["actions"].get(name), f"missing action label: {name}"
        for name in spec.functions:
            assert DISPLAY["functions"].get(name), f"missing function label: {name}"
        for name in spec.automations:
            assert DISPLAY["automations"].get(name), f"missing automation label: {name}"
        for cls in spec.object_types.values():
            for field_name in cls.model_fields:
                assert label("fields", field_name) != field_name or field_name in {
                    "name",
                    "status",
                    "role",
                    "title",
                    "summary",
                    "verified",
                }, f"missing field label: {field_name}"

    assert label("objects", "Engineer") == "研发成员"
    assert label("objects", "EmergencyOrder") == "急救订单"
    assert label("objects", "ColdTruck") == "冷链车辆"
    assert label("actions", "dispatch_order") == "分配配送车辆"
    assert label("roles", "admin") == "管理员"
    assert label("cardinalities", "many-to-many") == "多对多"
    assert label("objects", "UnknownThing") == "UnknownThing"


def test_main_page_chinese_primary_copy(base):
    html = _html(base + "/")
    for marker in (
        "本体蓝图列表",
        "运行实例列表",
        "本体关系图",
        "当前业务状态",
        "动作执行记录",
        "业务动作",
        "沙盘预演",
        "正式执行",
        "批准",
        "撤回",
        "克隆沙盘",
        "断开外部系统",
        "多步智能推演",
        "查看大图",
        "管理员",
        "财务人员",
        "调度员",
        "研发负责人",
    ):
        assert marker in html, marker
    assert "__DISPLAY_JSON__" not in html
    assert "__UI_BUNDLE__" not in html


def test_topology_preview_link_attributes(base):
    html = _html(base + "/")
    assert "/topology?ontology=" in html
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html
    assert 'tabindex="0"' in html
    assert "查看大图" in html


def test_role_values_and_api_ids_unchanged(base):
    html = _html(base + "/")
    for value in (
        "admin",
        "finance",
        "ops",
        "auditor",
        "dispatcher",
        "factory_manager",
        "system",
        "finance_manager",
        "ai_agent",
        "buyer",
        "mdm_steward",
    ):
        assert f'value="{value}"' in html

    meta = _get_json(base, "/api/meta", instance="coldchain-wave3")
    assert [a["name"] for a in meta["actions"]] == ["dispatch_order"]
    assert {t["name"] for t in meta["object_types"]} == {
        "EmergencyOrder",
        "ColdTruck",
    }

    sim = _post_json(
        base,
        "/api/simulate?instance=coldchain-wave3",
        {
            "instance": "coldchain-wave3",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )
    assert sim["valid"] is True
    assert sim["diff"]["object_changes"]

    submitted = _post_json(
        base,
        "/api/submit?instance=coldchain-wave3",
        {
            "instance": "coldchain-wave3",
            "run_id": "TK019-API-1",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )
    assert submitted["run"]["status"] == "COMMITTED"


# ---------------------------------------------------------------------------
# 3. 真实浏览器交互验收
# ---------------------------------------------------------------------------
sync_api = pytest.importorskip("playwright.sync_api")

# 本机 webapp-testing 以 Firefox 为验收基线；需要额外兼容验证时显式设置
# TK019_BROWSER=chromium，不改变默认回归口径。
BROWSER_ENGINE = os.environ.get("TK019_BROWSER", "firefox").strip().lower()
if BROWSER_ENGINE not in {"firefox", "chromium"}:
    BROWSER_ENGINE = "firefox"


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        launched = getattr(pw, BROWSER_ENGINE).launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()


def _collect_errors(page) -> list[str]:
    errors: list[str] = []

    def on_console(msg):
        if msg.type != "error":
            return
        text = msg.text
        if "favicon" in text.lower():
            return
        # 未知蓝图由页面 #error 中文兜底，404 探测属预期网络状态，不算未捕获异常
        if "failed to load resource" in text.lower() and "404" in text:
            return
        errors.append(f"console:{text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    return errors


def _open_coldchain(page, base_url: str) -> None:
    page.goto(base_url, wait_until="networkidle")
    page.get_by_text("冷链医药调度与 MCTS 规划").first.click()
    # 蓝图视图渲染完成（拓扑预览可点）后再点实例，避免点空。
    page.locator(".topo-link").first.wait_for(state="visible")
    page.get_by_text("冷链第三波 · 急救调度").first.click()
    # 实例作战室真正挂载完成：动作下拉可见 + 世界状态出现对象卡片。
    page.locator("#action").wait_for(state="visible")
    page.wait_for_selector("#objects .obj")


def _pct(page) -> int:
    return int(page.inner_text("#zoomPct").replace("%", ""))


def _transform(page) -> str:
    return page.get_attribute("#scene", "transform") or ""


def test_browser_topology_new_tab_zoom_drag_reset(live, browser):
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    page.goto(base_url, wait_until="networkidle")
    page.get_by_text("冷链医药调度与 MCTS 规划").first.click()
    page.locator(".topo-link").first.wait_for(state="visible")

    # 1) 点击拓扑预览 → 新标签页
    with page.expect_popup() as popup_info:
        page.locator(".topo-link").first.click()
    topo = popup_info.value
    topo.wait_for_load_state("networkidle")
    topo_errors = _collect_errors(topo)
    topo.wait_for_selector("#scene rect.node")
    assert "本体关系图" in topo.title()
    assert "冷链医药调度" in topo.inner_text("#title")
    assert topo.locator("#scene rect.node").count() == 2

    initial = _transform(topo)
    initial_pct = _pct(topo)

    # 2) 按钮放大 / 缩小，共享同一变换状态
    topo.locator("#zoomIn").click()
    topo.wait_for_timeout(120)
    assert _transform(topo) != initial
    assert _pct(topo) > initial_pct
    zoomed_pct = _pct(topo)
    topo.locator("#zoomOut").click()
    topo.wait_for_timeout(120)
    assert _pct(topo) < zoomed_pct

    # 3) 真实鼠标拖拽平移
    before_drag = _transform(topo)
    box = topo.locator("#canvas").bounding_box()
    assert box is not None
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    topo.mouse.move(cx, cy)
    topo.mouse.down()
    topo.mouse.move(cx + 160, cy + 90, steps=10)
    topo.mouse.up()
    topo.wait_for_timeout(120)
    assert _transform(topo) != before_drag

    # 4) 复位恢复初始视图
    topo.locator("#resetBtn").click()
    topo.wait_for_timeout(150)
    assert _transform(topo) == initial
    assert _pct(topo) == initial_pct

    # 5) 窗口变化后适应窗口仍正确
    topo.set_viewport_size({"width": 1000, "height": 700})
    topo.wait_for_timeout(150)
    topo.locator("#fitBtn").click()
    topo.wait_for_timeout(150)
    fits = topo.evaluate(
        """() => {
            const r = document.getElementById('scene').getBoundingClientRect();
            return {left:r.left, top:r.top, right:r.right, bottom:r.bottom,
                    w:document.getElementById('canvas').clientWidth,
                    h:document.getElementById('canvas').clientHeight};
        }"""
    )
    assert fits["left"] >= -2 and fits["top"] >= -2
    assert fits["right"] <= fits["w"] + 2 and fits["bottom"] <= fits["h"] + 2

    # 6) 滚轮以指针为中心缩放，且有上下限
    topo.mouse.move(fits["w"] * 0.5, fits["h"] * 0.5)
    before_wheel = _pct(topo)
    topo.mouse.wheel(0, -500)
    topo.wait_for_timeout(120)
    assert _pct(topo) > before_wheel
    for _ in range(40):
        topo.mouse.wheel(0, 1200)
    topo.wait_for_timeout(150)
    assert 10 <= _pct(topo) <= 800
    for _ in range(40):
        topo.mouse.wheel(0, -1200)
    topo.wait_for_timeout(150)
    assert 10 <= _pct(topo) <= 800

    # 7) 独立刷新仍可用
    topo.reload(wait_until="networkidle")
    topo.wait_for_selector("#scene rect.node")
    assert topo.locator("#scene rect.node").count() == 2
    assert "本体关系图" in topo.title()

    assert errors == []
    assert topo_errors == []
    context.close()


def test_browser_topology_link_keyboard(live, browser):
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    page.goto(base_url, wait_until="networkidle")
    page.get_by_text("AI 软件研发工厂").first.click()
    page.locator(".ph-link").first.wait_for(state="visible")

    page.locator(".ph-link").first.focus()
    with page.expect_popup() as popup_info:
        page.keyboard.press("Enter")
    topo = popup_info.value
    topo.wait_for_load_state("networkidle")
    topo.wait_for_selector("#scene rect.node")
    assert "AI 软件研发工厂" in topo.inner_text("#title")
    assert topo.locator("#scene rect.node").count() == 3
    assert errors == []
    context.close()


def test_browser_chinese_action_form_and_submit(live, browser):
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    _open_coldchain(page, base_url)

    page.wait_for_selector("#action option", state="attached")
    assert page.locator("#action").input_value() == "dispatch_order"
    selected = page.locator("#action option:checked").inner_text()
    assert selected == "分配配送车辆"

    form_text = page.inner_text("#form")
    assert "订单编号" in form_text
    assert "车辆编号" in form_text
    assert "order_id" in form_text  # 英文 key 作为次要技术说明保留

    page.locator("#submitBtn").click()
    page.wait_for_function(
        "() => document.getElementById('result').innerText.includes('已执行')"
    )
    # 正式执行成功后 refreshInstance() 会异步重绘动作账本与世界状态，
    # 等这两个面板真实刷新出结果，再保留原有断言，不以固定 sleep 掩盖竞态。
    sync_api.expect(page.locator("#runs")).to_contain_text("已执行", timeout=10_000)
    sync_api.expect(page.locator("#objects")).to_contain_text("已分配", timeout=10_000)
    assert "已执行" in page.inner_text("#runs")
    assert "已分配" in page.inner_text("#objects")
    assert errors == []
    context.close()


def test_browser_three_blueprints_and_two_instances_no_mixed_names(live, browser):
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    page.goto(base_url, wait_until="networkidle")

    page.get_by_text("AI 软件研发工厂").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('研发成员')"
    )
    stage = page.inner_text("#stage")
    assert "研发成员" in stage and "研发工单" in stage and "交付物" in stage
    assert "急救订单" not in stage

    page.get_by_text("冷链医药调度与 MCTS 规划").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('急救订单')"
    )
    stage = page.inner_text("#stage")
    assert "急救订单" in stage and "冷链车辆" in stage
    assert "研发成员" not in stage

    page.get_by_text("钢厂主数据破局与三视角对账").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('物料规格')"
    )
    stage = page.inner_text("#stage")
    assert "物料规格" in stage and "合同物料编码" in stage and "进厂批次" in stage
    assert "急救订单" not in stage and "研发成员" not in stage

    page.get_by_text("研发工厂 · 生产实例").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('研发成员')"
    )
    assert "研发成员" in page.inner_text(".tabs")
    assert "急救订单" not in page.inner_text(".tabs")

    page.get_by_text("冷链第三波 · 急救调度").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('急救订单')"
    )
    tabs = page.inner_text(".tabs")
    assert "急救订单" in tabs and "冷链车辆" in tabs
    assert "研发成员" not in tabs
    assert errors == []
    context.close()


def test_browser_empty_graph_and_unknown_ontology(browser):
    registry = CommandCenterRegistry()
    empty_spec = OntologySpec()
    runtime = OntologyRuntime(empty_spec)
    registry.register_ontology(
        "empty", "空蓝图", empty_spec, category="Enterprise", description="无对象"
    )
    registry.register_instance(
        "empty-instance", "empty", "空实例", runtime, db_uri="sqlite://:memory:"
    )
    handle = start_command_center_server(registry, port=0)
    try:
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        errors = _collect_errors(page)
        page.goto(
            handle.base_url + "/topology?ontology=empty", wait_until="networkidle"
        )
        page.wait_for_selector("#empty:not([hidden])")
        assert "该本体暂无对象类型" in page.inner_text("#empty")
        before = _pct(page)
        page.locator("#zoomIn").click()
        page.wait_for_timeout(100)
        assert _pct(page) >= before

        page.goto(
            handle.base_url + "/topology?ontology=no-such", wait_until="networkidle"
        )
        page.wait_for_selector("#error:not([hidden])")
        assert "未找到该本体蓝图" in page.inner_text("#error")
        assert page.locator("#back").get_attribute("href") == "/"
        assert errors == []
        context.close()
    finally:
        handle.stop()
        runtime.close()


def test_browser_screenshots_two_viewports(live, browser):
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    base_url = live[1].base_url
    for width, height in ((1440, 900), (1920, 1080)):
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        page.goto(base_url, wait_until="networkidle")
        page.get_by_text("冷链医药调度与 MCTS 规划").first.click()
        page.locator(".topo-link").first.wait_for(state="visible")
        page.screenshot(path=str(EVIDENCE_DIR / f"main_{width}x{height}.png"))

        with page.expect_popup() as popup_info:
            page.locator(".topo-link").first.click()
        topo = popup_info.value
        topo.wait_for_load_state("networkidle")
        topo.wait_for_selector("#scene rect.node")
        topo.screenshot(path=str(EVIDENCE_DIR / f"topology_{width}x{height}.png"))
        topo.locator("#zoomIn").click()
        topo.locator("#zoomIn").click()
        topo.wait_for_timeout(200)
        topo.screenshot(
            path=str(EVIDENCE_DIR / f"topology_zoom_{width}x{height}.png")
        )
        topo.close()

        page.get_by_text("冷链第三波 · 急救调度").first.click()
        page.wait_for_selector("#objects .obj")
        page.screenshot(path=str(EVIDENCE_DIR / f"instance_{width}x{height}.png"))
        context.close()

    for name in (
        "main_1440x900.png",
        "topology_1440x900.png",
        "topology_zoom_1440x900.png",
        "instance_1440x900.png",
        "main_1920x1080.png",
        "topology_1920x1080.png",
        "topology_zoom_1920x1080.png",
        "instance_1920x1080.png",
    ):
        path = EVIDENCE_DIR / name
        assert path.exists() and path.stat().st_size > 10_000, name
