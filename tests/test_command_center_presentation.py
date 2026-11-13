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
import re
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
def live(tmp_path):
    # 每个测试一个独立数据目录：绝不触碰仓库的 .instances 生产库。
    registry = build_default_command_center(data_dir=tmp_path)
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
    """每个蓝图的对象 / 关系 / 动作 / 函数 / 字段都必须有中文展示名。

    词条来源分两处：内核 ``ui_display.json`` 保存跨蓝图通用词条，
    业务本体把自己的词条声明在 ``spec.display`` 上。展示层把两者
    动态合并，所以本测试验证的是**合并结果**——这也是浏览器真正看到的。
    """
    from open_ontology.workshop import merged_display

    registry, _ = live
    merged = merged_display(registry)

    def show(group: str, key: str) -> str:
        table = merged.get(group) or {}
        return str(table.get(key) or key)

    for entry in registry.ontologies.values():
        spec = entry.spec
        for name in spec.object_types:
            assert show("objects", name) != name, f"missing object label: {name}"
        for name in spec.link_types:
            assert show("links", name) != name, f"missing link label: {name}"
        for name in spec.actions:
            assert show("actions", name) != name, f"missing action label: {name}"
        for name in spec.functions:
            assert show("functions", name) != name, f"missing function label: {name}"
        for name in spec.automations:
            assert show("automations", name) != name, (
                f"missing automation label: {name}"
            )
        for cls in spec.object_types.values():
            for field_name in cls.model_fields:
                assert show("fields", field_name) != field_name or field_name in {
                    "name",
                    "status",
                    "role",
                    "title",
                    "summary",
                    "verified",
                }, f"missing field label: {field_name}"

    assert show("objects", "Engineer") == "研发成员"
    assert show("objects", "EmergencyOrder") == "急救订单"
    assert show("objects", "ColdTruck") == "冷链车辆"
    assert show("actions", "dispatch_order") == "分配配送车辆"
    assert show("roles", "admin") == "管理员"
    assert show("cardinalities", "many-to-many") == "多对多"
    assert show("objects", "UnknownThing") == "UnknownThing"


def test_business_display_terms_live_in_their_own_spec(live):
    """业务词条必须住在业务本体自己的 spec.display 里，不进内核字典。"""
    from open_ontology.workshop_ui import DISPLAY

    registry, _ = live
    board = registry.get_ontology("task_board")
    assert board.spec.display, "task_board 必须在自己的 spec.display 声明词条"
    assert board.spec.display["objects"]["Card"] == "任务卡片"

    # 内核字典里不许出现看板专属词条。
    for group in ("objects", "links", "actions", "functions"):
        table = DISPLAY.get(group) or {}
        assert "Card" not in table, f"看板词条泄漏进内核 {group}"
        assert "claim_card" not in table, f"看板词条泄漏进内核 {group}"

    # 其它蓝图不受影响：内核通用词条仍然生效。
    assert DISPLAY["objects"]["Engineer"] == "研发成员"


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
    # 运行实例列表只在 Instance 页出现，先切页再点实例。
    page.locator("#tab-instance").click()
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


def test_browser_topology_shows_objects_links_actions_and_functions(live, browser):
    """全景拓扑：同一张 #scene 里同时呈现 Object / Link / Action / Function。"""
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    page.goto(
        base_url + "/topology?ontology=task_board", wait_until="networkidle"
    )
    page.wait_for_selector("#scene rect.node")

    # 数据层：6 个对象类型仍走 class="node"，不被新增语义层污染。
    assert page.locator("#scene rect.node").count() == 6
    # 语义层：10 个受控业务动作 + 3 个只读查询函数各自独立成卡。
    assert page.locator("#scene rect.action-node").count() == 10
    assert page.locator("#scene rect.fn-node").count() == 3

    scene_text = page.evaluate(
        "() => document.getElementById('scene').textContent || ''"
    )
    for marker in ("认领任务卡片", "登记执行结果", "创建任务卡片"):
        assert marker in scene_text, marker
    for marker in ("看板总览", "看板流程图", "生成任务简报"):
        assert marker in scene_text, marker

    subtitle = page.inner_text("#subtitle")
    for marker in ("6 个对象类型", "4 条关系", "10 个业务动作", "3 个查询函数"):
        assert marker in subtitle, marker

    page.locator("#fitBtn").click()
    page.wait_for_timeout(150)
    fits = page.evaluate(
        """() => {
            const r = document.getElementById('scene').getBoundingClientRect();
            const c = document.getElementById('canvas').getBoundingClientRect();
            return {left:r.left - c.left, top:r.top - c.top, right:r.right - c.left, bottom:r.bottom - c.top,
                    w:c.width,
                    h:c.height};
        }"""
    )
    assert fits["left"] >= -2 and fits["top"] >= -2
    assert fits["right"] <= fits["w"] + 2 and fits["bottom"] <= fits["h"] + 2
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

    # 左侧实例列表只在 Instance 页展示，切实例前先切页。
    page.locator("#tab-instance").click()
    page.get_by_text("研发工厂 · 生产实例").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('研发成员')"
    )
    assert "研发成员" in page.inner_text(".tabs")
    assert "急救订单" not in page.inner_text(".tabs")

    # 蓝图列表只在 Ontology 页展示，切蓝图前先切回。
    page.locator("#tab-ontology").click()
    page.get_by_text("冷链医药调度与 MCTS 规划").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('急救订单')"
    )
    stage = page.inner_text("#stage")
    assert "急救订单" in stage and "冷链车辆" in stage
    assert "研发成员" not in stage

    page.locator("#tab-instance").click()
    page.get_by_text("冷链第三波 · 急救调度").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('急救订单')"
    )
    tabs = page.inner_text(".tabs")
    assert "急救订单" in tabs and "冷链车辆" in tabs
    assert "研发成员" not in tabs

    page.locator("#tab-ontology").click()
    page.get_by_text("钢厂主数据破局与三视角对账").first.click()
    page.wait_for_function(
        "() => document.getElementById('stage').innerText.includes('物料规格')"
    )
    stage = page.inner_text("#stage")
    assert "物料规格" in stage and "合同物料编码" in stage and "进厂批次" in stage
    assert "急救订单" not in stage and "研发成员" not in stage
    assert errors == []
    context.close()


def _role_option_pairs(page) -> list[list[str]]:
    return page.eval_on_selector_all(
        "#role option", "els => els.map(el => [el.value, el.textContent.trim()])"
    )


def test_browser_all_ontologies_instance_and_role_isolation_and_demo_flows(live, browser):
    """6 蓝图 × 8 实例全量界面巡检。

    断言：
    (a) 点击每个蓝图后 ``#instances .card`` 仅含该蓝图实例；
    (b) ``#role option`` 仅含该蓝图专属角色，且文案 100% 为中文；
    (c) ``osv2-coil-prod`` 列级脱敏随角色动态生效，接口 Tab 高亮与汉化正常；
    (d) 非 MCTS 实例不渲染「多步智能推演」按钮；
    (e) 全程 0 console error / 0 pageerror。
    """
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = _collect_errors(page)
    try:
        page.goto(base_url, wait_until="networkidle")
        catalog = _get_json(base_url, "/api/catalog")
        ontologies = catalog["ontologies"]
        instances = catalog["instances"]
        assert len(ontologies) == 6, len(ontologies)
        assert len(instances) == 8, len(instances)

        labels = {row["instance_id"]: row["label"] for row in instances}
        mcts = {row["instance_id"]: row["supports_mcts"] for row in instances}
        by_ontology = {row["ontology_id"]: row for row in ontologies}

        def open_ontology(ontology: dict) -> None:
            # Ontology 页才显示本体蓝图列表，跨标签跳转必须先切回该页。
            page.locator("#tab-ontology").click()
            page.locator("#ontologies .card", has_text=ontology["name"]).first.click()
            page.wait_for_function(
                "(name) => {const h = document.querySelector('#stage .hero h2');"
                " return h && h.innerText.includes(name);}",
                arg=ontology["name"],
            )

        def open_instance(instance_id: str) -> None:
            # Instance 页才显示运行实例列表，跨标签跳转必须先切到该页。
            page.locator("#tab-instance").click()
            label = labels[instance_id]
            page.locator("#instances .card", has_text=label).first.click()
            page.wait_for_function(
                "(label) => {const h = document.querySelector('#stage .hero h2');"
                " return h && h.innerText.includes(label);}",
                arg=label,
            )
            page.locator("#action").wait_for(state="visible")
            page.wait_for_selector("#objects .obj")

        for ontology in ontologies:
            oid = ontology["ontology_id"]
            open_ontology(ontology)

            # 侧栏隔离：Ontology 页只显示本体蓝图列表，实例列表必须隐藏。
            assert page.locator("#ontologiesPanel").is_visible(), oid
            assert not page.locator("#instancesPanel").is_visible(), oid
            # 实例过滤默认关闭，直接展示全部实例。
            assert not page.locator("#filterToggle").is_checked(), oid

            # (b) 角色下拉隔离 + 全中文。
            options = _role_option_pairs(page)
            assert [value for value, _ in options] == ontology["roles"], oid
            for value, text in options:
                assert text and text != value, (oid, value, text)
                assert not re.search(r"[A-Za-z_]", text), (oid, value, text)

            expected = [labels[iid] for iid in ontology["instances"]]
            if expected:
                # Instance 页：本体蓝图列表消失，实例列表出现。
                page.locator("#tab-instance").click()
                page.locator("#instancesPanel").wait_for(state="visible")
                assert not page.locator("#ontologiesPanel").is_visible(), oid
                assert not page.locator("#filterToggle").is_checked(), oid
                # 默认不勾选：全部 8 个实例都在。
                assert page.locator("#instances .card").count() == len(instances), oid

                # 勾选过滤：仅展示当前蓝图的实例，杜绝跨蓝图串台。
                page.locator("#filterToggle").check()
                page.wait_for_function(
                    "(n) => document.querySelectorAll('#instances .card').length === n",
                    arg=len(expected),
                )
                instance_text = page.inner_text("#instances")
                for label in expected:
                    assert label in instance_text, (oid, label)
                for row in instances:
                    if row["ontology_id"] != oid:
                        assert row["label"] not in instance_text, (oid, row["label"])

                # 取消勾选：恢复全部 8 个实例。
                page.locator("#filterToggle").uncheck()
                page.wait_for_function(
                    "(n) => document.querySelectorAll('#instances .card').length === n",
                    arg=len(instances),
                )

            # (d) 遍历该蓝图下每个实例：能正常进入，按钮按能力显隐。
            for iid in ontology["instances"]:
                open_instance(iid)
                stage = page.inner_text("#stage")
                if mcts[iid]:
                    assert "多步智能推演" in stage, iid
                else:
                    assert "多步智能推演" not in stage, iid
                # 实例视图下实例列表可见、蓝图列表隐藏，角色列表仍属于当前蓝图。
                assert page.locator("#instancesPanel").is_visible(), iid
                assert not page.locator("#ontologiesPanel").is_visible(), iid
                assert [
                    value for value, _ in _role_option_pairs(page)
                ] == ontology["roles"], iid

        # (c) 列级脱敏：ops 看不到吨价 / 成本备注 / 内部编码，finance 全部可见。
        open_ontology(by_ontology["osv2_governance"])
        open_instance("osv2-coil-prod")
        assert page.locator("#role").input_value() == "ops"

        objects = page.inner_text("#objects")
        assert "不可见" in objects
        for hidden in ("18000", "主力现货", "NC-304"):
            assert hidden not in objects, hidden

        page.locator(".tab", has_text="可计价资产").click()
        page.wait_for_function(
            "() => {const el = document.querySelector('.tab[data-name=\"PricedAsset\"]');"
            " return el && el.classList.contains('on');}"
        )
        assert page.locator(".tab.on").count() == 1
        iface = page.inner_text("#objects")
        assert "可计价资产" in iface and "PricedAsset" not in iface
        for field_label in ("吨价", "成本备注", "内部编码"):
            assert field_label in iface, field_label
        assert "不可见" in iface

        page.select_option("#role", "finance")
        sync_api.expect(page.locator("#objects")).to_contain_text(
            "18000", timeout=10_000
        )
        sync_api.expect(page.locator("#objects")).to_contain_text(
            "主力现货", timeout=10_000
        )
        objects = page.inner_text("#objects")
        assert "不可见" not in objects

        page.locator(".tab", has_text="可计价资产").click()
        sync_api.expect(page.locator("#objects")).to_contain_text("吨价", timeout=10_000)
        sync_api.expect(page.locator("#objects")).to_contain_text(
            "18000", timeout=10_000
        )
        iface = page.inner_text("#objects")
        assert "不可见" not in iface
        assert "可计价资产" in iface
        assert page.locator(".tab.on").count() == 1

        # (e) 全程零报错。
        assert errors == [], errors
    finally:
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

        page.locator("#tab-instance").click()
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


# ---------------------------------------------------------------------------
# 3. 架构分层红线（M2 内核不得承载 M1 业务词汇）
# ---------------------------------------------------------------------------
KERNEL_FILES = ("workshop.py", "workshop_ui.py")

#: 看板领域的私有标识与中文词，一个都不许出现在通用 UI 内核里。
BOARD_LEAKS = (
    "board_key",
    "claimable",
    "is_terminal",
    "可认领",
    "终态",
    ".cards",
    ".purpose",
)


def test_ui_kernel_has_no_board_domain_fields():
    """通用 UI 内核只认领域无关的视觉协议，不许出现任何看板私有字段。"""
    root = Path(__file__).resolve().parents[1] / "open_ontology"
    for name in KERNEL_FILES:
        text = (root / name).read_text(encoding="utf-8")
        for token in BOARD_LEAKS:
            assert token not in text, f"{token!r} 泄漏进内核 {name}"


def test_spec_carries_its_own_display_terms():
    """OntologySpec 自带 display，新增业务本体不必改内核字典。"""
    from open_ontology.kanban import spec as board_spec
    from open_ontology.workshop_ui import DISPLAY

    assert hasattr(board_spec, "display")
    assert board_spec.display["objects"]["BoardColumn"] == "看板列"
    # 内核字典保持 0 污染。
    assert "BoardColumn" not in DISPLAY.get("objects", {})


# ---------------------------------------------------------------------------
# 3b. 看板实例承载完整架构宪法与基线真值表（TK-ONTO-BOARD-FULL-DOCS）
# ---------------------------------------------------------------------------
def test_task_board_instance_carries_full_architecture_and_state_docs(base):
    payload = _get_json(base, "/api/objects/Board", instance="board-prod")
    boards = {item["board_key"]: item for item in payload["objects"]}
    board = boards["BD-CC-001"]

    assert "# OpenOntology 架构设计规范" in board["goal"]
    assert len(board["goal"]) > 1000
    assert "# OpenOntology 当前状态真值表" in board["state"]
    assert len(board["state"]) > 1000


# ---------------------------------------------------------------------------
# 4. M2 零流程图污染护栏（静态源码 + HTTP 404）
# ---------------------------------------------------------------------------
#: M1「业务流程图」遗留物，一个都不许再出现在 M2 指挥台里。
FLOW_POLLUTION_TOKENS = (
    "flow_function",
    "FLOW_JS",
    "FLOW_PAGE_HTML",
    "业务流程图",
    "PROCESS FLOW",
    "/flow",
)


def test_m2_workshop_has_no_process_flow_pollution(base):
    """M2 指挥台只认四大通用原语，不得承载任何流程图专属协议。"""
    root = Path(__file__).resolve().parents[1] / "open_ontology"
    for name in KERNEL_FILES:
        text = (root / name).read_text(encoding="utf-8")
        for token in FLOW_POLLUTION_TOKENS:
            assert token not in text, f"{token!r} 泄漏进内核 {name}"

    payload = _get_json(base, "/api/meta", instance="board-prod")
    assert "flow_function" not in payload["instance"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(base + "/flow?instance=board-prod")
    assert exc.value.code == 404


def test_meta_unknown_instance_has_chinese_error(base):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get_json(base, "/api/meta", instance="no-such-instance")
    assert exc.value.code == 404


# ---------------------------------------------------------------------------
# 5. 真实浏览器：任务看板实例无流程图面板，四大通用 M2 面板完整
# ---------------------------------------------------------------------------
def test_browser_task_board_instance_has_no_flow_panel(live, browser):
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    try:
        page.goto(base_url, wait_until="networkidle")
        page.get_by_text("多智能体任务看板").first.click()
        page.locator("#tab-instance").click()
        page.get_by_text("任务看板 · 生产实例").first.click()
        page.locator("#action").wait_for(state="visible")
        page.wait_for_selector("#objects .obj")

        # 流程图面板与 #flow 容器彻底消失（正文里的架构文档引述属于数据，不算 UI 面板）
        assert page.locator("#flow").count() == 0
        assert (
            page.locator(
                "button:has-text('业务流程图'), a:has-text('业务流程图'), "
                "button:has-text('PROCESS FLOW'), a:has-text('PROCESS FLOW')"
            ).count()
            == 0
        )
        body = page.inner_text("body")

        # 四大通用 M2 面板完整渲染
        for marker in (
            "当前业务状态",
            "业务动作与沙盘推演台",
            "语义关系与 🧮 查询计算",
            "动作执行记录",
        ):
            assert marker in body, marker
        assert page.locator("#objects").count() == 1
        assert page.locator("#links").count() == 1
        assert page.locator("#runs").count() == 1
        assert page.locator("#form").count() == 1

        assert errors == [], errors
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 6. 微内核顶层命名空间纯洁性（M2 内核不得导出用户态程序与设备驱动）
# ---------------------------------------------------------------------------
#: 具体业务本体（用户态程序）与具体 CLI 宿主驱动（设备驱动）都不许出现在内核顶层。
NON_KERNEL_TOP_LEVEL = (
    # 业务本体对象
    "Engineer",
    "WorkTicket",
    "Deliverable",
    "Board",
    "BoardColumn",
    "Member",
    "Card",
    "CardComment",
    "CardRun",
    # 业务查询函数
    "factory_board_summary",
    "build_engineer_briefing",
    "render_briefing",
    "board_overview",
    "board_flow",
    "card_briefing",
    # CLI / 宿主驱动
    "CodeBuddyCLIDriver",
    "HermesCLIDriver",
    "OpenCodeHTTPDriver",
    "GenericCLIDriver",
    "CallableHarnessDriver",
    "FactoryOrchestrator",
    "HarnessDriver",
    "HarnessError",
    "HarnessRateLimitError",
    "HarnessUnavailableError",
    "CodeBuddyHost",
    "OpenCodeHost",
    "RoleRoutedHost",
    "BoardRunner",
)


def test_kernel_top_level_exports_only_kernel_primitives():
    """``open_ontology.__all__`` 只包含微内核与通用引擎原语。"""
    import open_ontology

    leaked = [name for name in NON_KERNEL_TOP_LEVEL if name in open_ontology.__all__]
    assert leaked == [], f"内核顶层泄漏了用户态程序 / 设备驱动：{leaked}"


def test_kernel_top_level_has_no_business_or_driver_attributes():
    """不只是 ``__all__``：属性也不能挂在内核顶层命名空间上。"""
    import open_ontology

    leaked = [name for name in NON_KERNEL_TOP_LEVEL if hasattr(open_ontology, name)]
    assert leaked == [], f"内核顶层仍可访问到：{leaked}"


def test_business_programs_remain_importable_from_their_own_modules():
    """清理顶层导出不等于删除能力：业务本体与驱动仍可从各自模块导入。"""
    from open_ontology.agent_hosts import BoardRunner, CodeBuddyHost, OpenCodeHost
    from open_ontology.dev_factory import Engineer, WorkTicket
    from open_ontology.harness_drivers import CodeBuddyCLIDriver
    from open_ontology.kanban import Board, Card

    assert Engineer.__name__ == "Engineer"
    assert WorkTicket.__name__ == "WorkTicket"
    assert Board.__name__ == "Board"
    assert Card.__name__ == "Card"
    assert CodeBuddyCLIDriver.__name__ == "CodeBuddyCLIDriver"
    assert BoardRunner.__name__ == "BoardRunner"
    assert CodeBuddyHost.__name__ == "CodeBuddyHost"
    assert OpenCodeHost.__name__ == "OpenCodeHost"


def test_ingestion_driver_is_a_device_driver_not_kernel():
    """数据摄入是设备驱动：内核通过 ``runtime.ingestion`` 委托，自己不实现。"""
    from open_ontology import Actor, OntologyRuntime, OntologySpec, ObjectType
    from open_ontology.ingestion import SnapshotIngestionDriver, parse_mdo_rows

    spec = OntologySpec()

    class Item(ObjectType):
        __primary_key__ = "item_id"
        __owned__ = {"note": None}
        item_id: str
        qty: int = 0
        label: str = ""
        note: str = ""

    spec.object_type(Item)
    runtime = OntologyRuntime(spec)
    try:
        assert isinstance(runtime.ingestion, SnapshotIngestionDriver)
        admin = Actor(actor_id="a", role="admin", clearance_tags={"*"})
        runtime.load_snapshot({"Item": [{"item_id": "I1", "qty": 5}]}, actor=admin)
        assert runtime.get_object(Item, "I1", admin).qty == 5

        # 多源列拼接：两个源各贡献一部分列
        summary = runtime.load_mdo_snapshots(
            Item,
            [
                [{"item_id": "I1", "qty": 9}],
                [{"item_id": "I1", "label": "from source"}],
            ],
            actor=admin,
        )
        assert summary["merged_objects"] == 1
        assert set(summary["merged_columns"]) == {"qty", "label"}
        item = runtime.get_object(Item, "I1", admin)
        assert item.qty == 9 and item.label == "from source"

        # 纯函数入口与内核静态方法行为一致
        assert parse_mdo_rows([{"item_id": "X", "qty": 1}], "item_id", 0) == [
            ("X", {"item_id": "X", "qty": 1})
        ]
        assert OntologyRuntime._mdo_rows({"X": {"qty": 2}}, "item_id", 0) == [
            ("X", {"qty": 2})
        ]
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 7. 指挥台四个一级标签页（Model / Runtime / Workspace / Device）
# ---------------------------------------------------------------------------
PILLAR_TABS = (
    ("tab-ontology", "Model · 领域模型"),
    ("tab-runtime", "Runtime · 算力与治理宿体"),
    ("tab-instance", "Workspace · 工作空间"),
    ("tab-device", "Device · 外部世界(符号+联结)"),
)


def test_pillar_tabs_markup_and_class_isolation(base):
    """四个一级标签页必须是 pillar-tab，绝不占用 Instance 页的 .tab 小标签。"""
    html = _html(base + "/")

    assert 'id="pillarTabs"' in html
    for tab_id, label in PILLAR_TABS:
        assert f'id="{tab_id}"' in html, tab_id
        assert label in html, label
        assert f"switchPillar('{tab_id[len('tab-'):]}')" in html, tab_id
    assert 'class="pillar-tab' in html
    assert 'class="pillar-tab on"' in html  # 默认高亮 Ontology
    # 侧栏两块面板必须可独立显隐：实例列表默认隐藏，仅在 Instance 页出现。
    assert 'id="side"' in html
    assert 'id="ontologiesPanel"' in html
    assert 'id="instancesPanel"' in html
    assert 'id="instancesPanel" style="flex:1" hidden' in html
    assert 'main.full' in html
    assert '[hidden]{display:none !important}' in html
    # 一级标签页不得复用 .tab：.tab.on 仍专属于实例页的对象 / 接口小标签。
    assert '<button class="tab' not in html
    assert 'class="tab on" id="tab-' not in html


def test_browser_pillar_tabs_switch_four_views(live, browser):
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = _collect_errors(page)
    try:
        page.goto(base_url, wait_until="networkidle")
        page.wait_for_selector("#pillarTabs .pillar-tab")
        assert page.locator("#pillarTabs .pillar-tab").count() == 4
        assert page.locator(".pillar-tab.on").count() == 1
        assert page.locator("#tab-ontology.on").count() == 1

        # Ontology 页：侧栏可见，只挂本体蓝图列表。
        assert page.locator("#side").is_visible()
        assert page.locator("#ontologiesPanel").is_visible()
        assert not page.locator("#instancesPanel").is_visible()

        # --- Runtime：默认内存宿体 + 各实例专属宿体 -------------------
        page.locator("#tab-runtime").click()
        page.wait_for_function(
            "() => document.getElementById('stage').innerText"
            ".includes('Runtime · 算力与治理宿体')"
        )
        stage = page.inner_text("#stage")
        assert "默认内存 Runtime (In-Memory)" in stage
        assert "rt-default-memory" in stage
        assert "rt-coldchain-wave3" in stage
        assert "IdleAwareCallbackAspect" in stage
        assert "DeviceAdapter" in stage
        assert "WMS 仓储" in stage
        assert page.locator("#tab-runtime.on").count() == 1
        assert page.locator("#tab-ontology.on").count() == 0
        # Runtime 页：侧栏整体隐藏，主舞台独占整幅。
        assert not page.locator("#side").is_visible()
        assert not page.locator("#ontologiesPanel").is_visible()
        assert not page.locator("#instancesPanel").is_visible()
        assert page.locator("main.full").count() == 1

        # --- Device：符号 / 联结双栏 ---------------------------------
        page.locator("#tab-device").click()
        page.wait_for_function(
            "() => document.getElementById('stage').innerText"
            ".includes('外部世界(符号+联结)')"
        )
        assert page.locator("#symbolicDevices").count() == 1
        assert page.locator("#connectionistDevices").count() == 1
        symbolic = page.inner_text("#symbolicDevices")
        connectionist = page.inner_text("#connectionistDevices")
        assert '"openapi": "3.1.0"' in symbolic
        assert "WMS 仓储" in symbolic
        assert '"protocol": "A2A/1.0"' in connectionist
        assert "CodeBuddy CLI" in connectionist
        assert page.locator("#tab-device.on").count() == 1
        # Device 页：侧栏整体隐藏，主舞台独占整幅。
        assert not page.locator("#side").is_visible()
        assert not page.locator("#ontologiesPanel").is_visible()
        assert not page.locator("#instancesPanel").is_visible()
        assert page.locator("main.full").count() == 1

        # --- Instance：认知降维公式 + 原有作战室面板 -------------------
        page.locator("#tab-instance").click()
        page.locator("#action").wait_for(state="visible")
        page.wait_for_selector("#objects .obj")
        assert page.locator("#formula").count() == 1
        formula = page.inner_text("#formula")
        assert " + " in formula and "=" in formula
        assert page.locator("#tab-instance.on").count() == 1
        assert page.locator(".tab.on").count() == 1  # 小标签语义未被污染
        for panel in ("当前业务状态", "动作执行记录"):
            assert panel in page.inner_text("#stage"), panel
        # Instance 页：侧栏可见，只挂运行实例列表，本体蓝图列表隐藏。
        assert page.locator("#side").is_visible()
        assert not page.locator("#ontologiesPanel").is_visible()
        assert page.locator("#instancesPanel").is_visible()
        assert page.locator("main.full").count() == 0

        # --- Ontology：拓扑预览与元模型 -------------------------------
        page.locator("#tab-ontology").click()
        page.wait_for_selector("#stage .topo-link")
        assert "本体关系图" in page.inner_text("#stage")
        assert page.locator("#tab-ontology.on").count() == 1
        assert page.locator("#side").is_visible()
        assert page.locator("#ontologiesPanel").is_visible()
        assert not page.locator("#instancesPanel").is_visible()

        # --- 左侧栏点击同样驱动一级标签页高亮 -------------------------
        # 实例列表只在 Instance 页出现，点实例前先切到该页。
        page.locator("#tab-instance").click()
        page.locator("#instancesPanel").wait_for(state="visible")
        page.locator("#instances .card").first.click()
        page.wait_for_function(
            "() => document.getElementById('tab-instance').classList.contains('on')"
        )
        # 本体蓝图列表只在 Ontology 页出现，点蓝图前先切回该页。
        page.locator("#tab-ontology").click()
        page.locator("#ontologiesPanel").wait_for(state="visible")
        page.locator("#ontologies .card").first.click()
        page.wait_for_function(
            "() => document.getElementById('tab-ontology').classList.contains('on')"
        )

        assert errors == [], errors
    finally:
        context.close()


# ---------------------------------------------------------------------------
# 8. C-WS-05：Model / Workspace 展示与新旧 API 兼容
# ---------------------------------------------------------------------------
MODEL_WORKSPACE_PILLARS = (
    ("tab-ontology", "Model · 领域模型"),
    ("tab-runtime", "Runtime · 算力与治理宿体"),
    ("tab-instance", "Workspace · 工作空间"),
    ("tab-device", "Device · 外部世界(符号+联结)"),
)


def test_pillar_tabs_use_model_and_workspace_labels(base):
    """一级标签页文案迁移为 Model / Workspace，且不新增 TBox / ABox 一级页。"""
    html = _html(base + "/")

    assert 'id="pillarTabs"' in html
    for tab_id, label in MODEL_WORKSPACE_PILLARS:
        assert f'id="{tab_id}"' in html, tab_id
        assert label in html, label
        assert f"switchPillar('{tab_id[len('tab-'):]}')" in html, tab_id
    # 旧的 Ontology / Instance 一级标签文案彻底退役。
    assert "Ontology · 未激活的知识" not in html
    assert "Instance · 激活的知识与沙盘" not in html
    # 不新增 TBox / ABox 一级标签，也不新增一级 DOM 主键。
    for forbidden in (
        'id="tab-tbox"',
        'id="tab-abox"',
        "switchPillar('tbox')",
        "switchPillar('abox')",
    ):
        assert forbidden not in html, forbidden
    # 仍然恰好四个一级标签，且默认高亮 Model 页。
    assert html.count('class="pillar-tab') == 4
    assert 'class="pillar-tab on" id="tab-ontology"' in html
    # 既有 DOM 主键全部保留。
    for dom_id in ("tab-ontology", "tab-runtime", "tab-instance", "tab-device"):
        assert f'id="{dom_id}"' in html, dom_id


def test_catalog_exposes_model_and_workspace_aliases(base):
    """catalog 同时提供 models / workspaces 别名，旧键原样保留。"""
    catalog = _get_json(base, "/api/catalog")

    assert catalog["models"] == catalog["ontologies"]
    assert catalog["workspaces"] == catalog["instances"]
    assert len(catalog["models"]) == 6
    assert len(catalog["workspaces"]) == 8
    assert [row["ontology_id"] for row in catalog["ontologies"]] == [
        row["ontology_id"] for row in catalog["models"]
    ]
    assert [row["instance_id"] for row in catalog["instances"]] == [
        row["instance_id"] for row in catalog["workspaces"]
    ]


def test_workspace_query_alias_matches_legacy_instance_query(base):
    """?workspace= 与 ?instance= 指向同一个工作空间，响应逐字节一致。"""
    legacy = _get_json(base, "/api/meta", instance="coldchain-wave3")
    alias = _get_json(base, "/api/meta", workspace="coldchain-wave3")

    assert alias == legacy
    assert alias["instance"]["instance_id"] == "coldchain-wave3"
    assert alias["instance"]["ontology_id"] == "coldchain_mcts"


def test_workspace_alias_covers_every_model_and_workspace(base):
    """6 个模型 + 8 个工作空间在新旧参数下都可寻址且身份一致。"""
    catalog = _get_json(base, "/api/catalog")

    for model in catalog["models"]:
        detail = _get_json(base, f"/api/ontology/{model['ontology_id']}")
        assert detail["ok"] is True
        assert detail["ontology"]["ontology_id"] == model["ontology_id"]
        assert len(detail["object_types"]) == model["counts"]["object_types"]

    for workspace in catalog["workspaces"]:
        workspace_id = workspace["instance_id"]
        meta = _get_json(base, "/api/meta", workspace=workspace_id)
        assert meta["instance"]["instance_id"] == workspace_id
        assert meta["instance"]["ontology_id"] == workspace["ontology_id"]
        assert meta["instance"]["runtime_id"] == workspace["runtime_id"]
        assert meta["instance"] == _get_json(
            base, "/api/meta", instance=workspace_id
        )["instance"]


def test_conflicting_workspace_and_instance_parameters_are_rejected(base):
    """新旧参数冲突必须 400，绝不静默选中其中一个工作空间。"""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get_json(
            base, "/api/meta", instance="coldchain-wave3", workspace="factory-prod"
        )
    assert exc.value.code == 400
    error = json.loads(exc.value.read().decode("utf-8"))
    assert error["ok"] is False
    assert "workspace" in error["error"] and "instance" in error["error"]

    params = {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"}

    # POST 查询参数冲突
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(
            base,
            "/api/simulate?instance=coldchain-wave3&workspace=factory-prod",
            {"action": "dispatch_order", "params": params},
        )
    assert exc.value.code == 400

    # POST 请求体冲突
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(
            base,
            "/api/simulate",
            {
                "instance": "coldchain-wave3",
                "workspace": "factory-prod",
                "action": "dispatch_order",
                "params": params,
            },
        )
    assert exc.value.code == 400

    # 新旧别名给出相同值不算冲突（迁移期可共存）。
    same = _post_json(
        base,
        "/api/simulate?instance=coldchain-wave3&workspace=coldchain-wave3",
        {"action": "dispatch_order", "params": params},
    )
    assert same["valid"] is True


def test_workspace_alias_drives_actions_approval_audit_and_simulation(base):
    """新路由参数贯通沙盘预演、正式执行、审批与审计账本。"""
    params = {"order_id": "ORD-2", "truck_id": "TRK-FAST"}
    simulated = _post_json(
        base,
        "/api/simulate?workspace=coldchain-wave3",
        {"workspace": "coldchain-wave3", "action": "dispatch_order", "params": params},
    )
    assert simulated["valid"] is True
    assert simulated["diff"]["object_changes"]

    submitted = _post_json(
        base,
        "/api/submit?workspace=coldchain-wave3",
        {
            "workspace": "coldchain-wave3",
            "run_id": "CWS05-RUN-1",
            "action": "dispatch_order",
            "params": params,
        },
    )
    assert submitted["run"]["status"] == "COMMITTED"

    runs = _get_json(base, "/api/action-runs", workspace="coldchain-wave3")
    assert runs["instance"] == "coldchain-wave3"
    assert any(run["run_id"] == "CWS05-RUN-1" for run in runs["runs"])

    pending = _post_json(
        base,
        "/api/submit?workspace=osv2-coil-prod",
        {
            "workspace": "osv2-coil-prod",
            "run_id": "CWS05-APPROVE-1",
            "action": "request_price_change",
            "params": {"asset_id": "C-2", "unit_price_ton": 24500.0},
        },
    )
    assert pending["run"]["status"] == "PENDING_APPROVAL"

    approved = _post_json(
        base,
        "/api/approve?workspace=osv2-coil-prod&role=finance_manager",
        {"workspace": "osv2-coil-prod", "run_id": "CWS05-APPROVE-1"},
    )
    assert approved["run"]["status"] == "COMMITTED"


def test_workspace_alias_preserves_role_masking(base):
    """新路由参数不绕过列级脱敏。"""
    ops_view = _get_json(
        base, "/api/objects/SteelCoil", workspace="osv2-coil-prod", role="ops"
    )
    coil = next(row for row in ops_view["objects"] if row["asset_id"] == "C-1")
    assert coil["unit_price_ton"] == 0.0
    assert coil["cost_note"] is None
    assert coil["internal_code"] == "REDACTED"

    finance_view = _get_json(
        base, "/api/objects/SteelCoil", workspace="osv2-coil-prod", role="finance"
    )
    coil = next(row for row in finance_view["objects"] if row["asset_id"] == "C-1")
    assert coil["unit_price_ton"] == 18000.0
    assert coil["cost_note"] == "主力现货"
    assert coil["internal_code"] == "NC-304"


def test_workspace_alias_fork_isolation(base):
    """经新路由参数克隆沙盘后，母体与子体事实严格隔离。"""
    forked = _post_json(
        base,
        "/api/instance/fork",
        {"workspace": "coldchain-wave3", "label": "别名沙盘"},
    )
    new_id = forked["instance"]["instance_id"]
    assert new_id.startswith("coldchain-wave3-fork-")

    _post_json(
        base,
        f"/api/submit?workspace={new_id}",
        {
            "workspace": new_id,
            "run_id": "CWS05-FORK-1",
            "action": "dispatch_order",
            "params": {"order_id": "ORD-1", "truck_id": "TRK-HEAVY"},
        },
    )

    mother = _get_json(base, "/api/objects/EmergencyOrder", workspace="coldchain-wave3")
    assert next(o for o in mother["objects"] if o["order_id"] == "ORD-1")[
        "status"
    ] == "pending"
    child = _get_json(base, "/api/objects/EmergencyOrder", workspace=new_id)
    assert next(o for o in child["objects"] if o["order_id"] == "ORD-1")[
        "status"
    ] == "assigned"


def test_topology_page_accepts_model_alias(base):
    """?model= 是 ?ontology= 的等价别名，渲染同一页面。"""
    legacy = _html(base + "/topology?ontology=task_board")
    alias = _html(base + "/topology?model=task_board")

    assert alias == legacy
    assert "本体关系图" in alias


def test_topology_conflicting_ontology_and_model_rejected(base):
    """?ontology= 与 ?model= 冲突必须 400；同值可共存。"""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(base + "/topology?ontology=task_board&model=coldchain_mcts")
    assert exc.value.code == 400

    same = _html(base + "/topology?ontology=task_board&model=task_board")
    assert same == _html(base + "/topology?ontology=task_board")


def test_browser_model_and_workspace_views_label_tbox_behavior_abox(live, browser):
    """模型视图区分 TBox 结构与 Behavior 行为；工作空间事实标注 ABox。"""
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = _collect_errors(page)
    try:
        page.goto(base_url, wait_until="networkidle")
        page.wait_for_selector("#pillarTabs .pillar-tab")
        pillars = page.inner_text("#pillarTabs")
        assert "Model · 领域模型" in pillars
        assert "Workspace · 工作空间" in pillars

        page.locator("#ontologies .card").first.click()
        page.wait_for_selector("#stage .topo-link")
        model_stage = page.inner_text("#stage")
        assert "TBox" in model_stage, model_stage
        assert "Behavior" in model_stage, model_stage

        page.locator("#tab-instance").click()
        page.locator("#instancesPanel").wait_for(state="visible")
        page.locator("#instances .card").first.click()
        page.locator("#action").wait_for(state="visible")
        page.wait_for_selector("#objects .obj")
        workspace_stage = page.inner_text("#stage")
        assert "ABox" in workspace_stage, workspace_stage
        assert "Workspace · 工作空间" in workspace_stage

        assert errors == [], errors
    finally:
        context.close()


def test_browser_topology_model_alias_renders_same_scene(live, browser):
    """独立拓扑页经 ?model= 也能加载同一张场景图。"""
    base_url = live[1].base_url
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = _collect_errors(page)
    try:
        page.goto(base_url + "/topology?model=task_board", wait_until="networkidle")
        page.wait_for_selector("#scene rect.node")
        assert page.locator("#scene rect.node").count() == 6
        assert "多智能体任务看板" in page.inner_text("#title")
        assert errors == [], errors
    finally:
        context.close()

