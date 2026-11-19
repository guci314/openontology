"""钢厂主数据（MDM）两大死结演播台验收测试（I-008）。

覆盖：
1. 第一幕：老 ERP 4 散码零改表寄生归并 + 多次夜间 load_snapshot 全量重刷后 overlay 100% 存活；
2. 第二幕：增量建码双硬护栏（UNMAPPED_PHYSICAL_SPEC / SPEC_QUALITY_MISMATCH + 智能推荐）与自定义破坏性试探；
3. 第三幕：新品种 SPEC-C 在持久 W_lat 挂起（W_mem 零污染）-> 进程重启保留 -> MDM 管家签批原子建标与驳回分支；
4. 第四幕：高维批次桶三正交边 -> MES 按厂家盲投 150 吨 FIFO -> 三视角账本 150t / ¥280,000 数学恒等；
5. 第五幕：1 秒时光机无损冲正库存与三本账，以及 HTTP 服务路由往返验证。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.request
from pathlib import Path

from examples.steel_mdm_studio import SteelMDMStudioApp, create_server


def test_steel_mdm_five_acts_restart_and_http_roundtrip(tmp_path: Path):
    state_dir = tmp_path / "steel_state"
    app = SteelMDMStudioApp(state_dir, port=0)

    try:
        # 初始重置后：老 ERP 源表绝无 canonical_spec_id 列，4 散码均未归并
        init_state = app.state_snapshot()
        assert not (state_dir / "w_lat.sqlite3").exists()
        assert len(init_state["w_real"]["skus"]) == 4
        assert all("canonical_spec_id" not in row for row in init_state["w_real"]["skus"])
        assert all(s["canonical_spec_id"] is None for s in init_state["w_mem"]["skus"])

        # 第一幕：寄生归并 + 批次入库 + 老 ERP 全量重刷存活
        r1 = app.run_act_1()["act_result"]
        assert r1["reindex"]["overlay_preserved"] is True
        assert r1["reindex"]["overlay_rows_count"] == 4

        # 额外再模拟一次夜间重刷，验证依然 100% 存活
        r_re = app.reindex_legacy_erp()["reindex_result"]
        assert r_re["reindex_count"] == 2
        assert r_re["overlay_preserved"] is True
        snap1 = app.state_snapshot()
        assert {s["sku_id"]: s["canonical_spec_id"] for s in snap1["w_mem"]["skus"]} == {
            "ERP-SKU-9001": "SPEC-A",
            "ERP-SKU-9002": "SPEC-A",
            "ERP-SKU-9003": "SPEC-B",
            "ERP-SKU-9004": "SPEC-A",
        }
        assert len(snap1["w_mem"]["lots"]) == 3
        lot1 = next(x for x in snap1["w_mem"]["lots"] if x["lot_id"] == "LOT-01")
        assert lot1["edges"] == {
            "lot_has_spec": ["SPEC-A"],
            "lot_produced_by": ["BRAND-1"],
            "lot_billed_as": ["ERP-SKU-9001"],
        }

        # 第二幕：双硬护栏试探
        r2 = app.run_act_2()["act_result"]
        assert r2["wild_sku_probe"]["blocked"] is True
        assert r2["wild_sku_probe"]["error_code"] == "UNMAPPED_PHYSICAL_SPEC"
        assert r2["quality_mismatch_probe"]["blocked"] is True
        assert r2["quality_mismatch_probe"]["error_code"] == "SPEC_QUALITY_MISMATCH"
        assert any(
            rec["spec_id"] == "SPEC-B"
            for rec in r2["quality_mismatch_probe"]["recommendations"]
        )

        # 自定义边界破坏性试探：负单价、不存在厂家、合规新散码
        bad_price = app.try_register_sku(
            {
                "sku_id": "ERP-SKU-9198",
                "invoice_title": "零元错码",
                "brand_id": "BRAND-1",
                "unit_price_ton": 0,
                "ash_pct": 12.0,
                "sulfur_pct": 0.6,
                "canonical_spec_id": "SPEC-A",
            }
        )["probe_result"]
        assert bad_price["blocked"] is True
        assert bad_price["error_code"] == "INVALID_UNIT_PRICE"

        bad_brand = app.try_register_sku(
            {
                "sku_id": "ERP-SKU-9199",
                "invoice_title": "未知厂家测试",
                "brand_id": "BRAND-NOPE",
                "unit_price_ton": 1800,
                "ash_pct": 12.0,
                "sulfur_pct": 0.6,
                "canonical_spec_id": "SPEC-A",
            }
        )["probe_result"]
        assert bad_brand["blocked"] is True
        assert bad_brand["error_code"] == "BRAND_NOT_FOUND"

        valid_sku = app.try_register_sku(
            {
                "sku_id": "ERP-SKU-9105",
                "invoice_title": "合规绑定二级焦测试码",
                "brand_id": "BRAND-1",
                "unit_price_ton": 1620,
                "ash_pct": 13.1,
                "sulfur_pct": 0.85,
                "canonical_spec_id": "SPEC-B",
            }
        )["probe_result"]
        assert valid_sku["blocked"] is False
        assert valid_sku["status"] == "COMMITTED"

        # 第三幕：申报新品种 SPEC-C 锁入持久 W_lat（W_mem 零污染）
        r3 = app.run_act_3()["act_result"]
        assert r3["status"] == "PENDING_APPROVAL"
        assert r3["spec_c_exists_in_mem"] is False
        assert r3["awaiting_count"] == 1
        pending_rid = r3["run_id"]
    finally:
        app.close()

    # 重启恢复验证：W_lat 待签批 SPEC-C 完好留存
    app2 = SteelMDMStudioApp(state_dir, port=0)
    server = create_server(app2, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    app2.port = port
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    try:
        resumed = app2.state_snapshot()
        assert resumed["w_lat"]["awaiting_approval_count"] == 1
        assert resumed["w_lat"]["awaiting_approval_moves"][0]["run_id"] == pending_rid
        assert all(sp["spec_id"] != "SPEC-C" for sp in resumed["w_mem"]["specs"])

        # MDM 管家签批 -> post_approval 原子创建 SPEC-C + ERP-SKU-9102
        dec = app2.decide_act_3(op="approve", run_id=pending_rid)["decision_result"]
        assert dec["status"] == "COMMITTED"
        assert dec["spec_c_exists_in_mem"] is True
        after_appr = app2.state_snapshot()
        assert not (state_dir / "w_lat.sqlite3").exists()
        assert after_appr["w_lat"]["awaiting_approval_count"] == 0
        assert any(sp["spec_id"] == "SPEC-C" for sp in after_appr["w_mem"]["specs"])
        assert any(sk["sku_id"] == "ERP-SKU-9102" for sk in after_appr["w_real"]["skus"])

        # 第四幕：MES 按厂家 BRAND-1 盲投 150 吨 FIFO -> 三账数学恒等 150t / ¥280,000
        r4 = app2.run_act_4()["act_result"]
        ledger = r4["ledger"]
        assert ledger["balanced"] is True
        assert ledger["totals"] == {"tons": 150, "cost": 280_000}
        assert ledger["views"] == {
            "brand_tons": 150,
            "spec_tons": 150,
            "sku_tons": 150,
            "brand_cost": 280_000,
            "spec_cost": 280_000,
            "sku_cost": 280_000,
        }

        # 超库存盲投拦截测试
        over_cons = app2.consume_brand(brand_id="BRAND-1", tons=999)["consume_result"]
        assert over_cons["blocked"] is True
        assert over_cons["error_code"] == "INSUFFICIENT_BRAND_STOCK"

        # 第五幕：1 秒时光机冲正 -> LOT-01/LOT-02 恢复 100/80，三本账归零
        rev = app2.revert_action_run()["revert_result"]
        assert rev["revert_status"] == "COMMITTED"
        assert rev["ledger_after_revert"]["totals"] == {"tons": 0, "cost": 0}
        lots_after_rev = {
            lot["lot_id"]: lot for lot in app2.state_snapshot()["w_mem"]["lots"]
        }
        assert lots_after_rev["LOT-01"]["remaining_tons"] == 100
        assert lots_after_rev["LOT-02"]["remaining_tons"] == 80

        # HTTP GET / + GET /api/state + POST /api/walkthrough 往返
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as r_html:
            html_text = r_html.read().decode("utf-8")
            assert "钢厂主数据" in html_text
        req_walk = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/walkthrough",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req_walk, timeout=10) as r_walk:
            walk_data = json.loads(r_walk.read().decode("utf-8"))
            assert walk_data["ok"] is True
            assert walk_data["state"]["ledger"]["balanced"] is True
            assert walk_data["state"]["ledger"]["totals"] == {"tons": 150, "cost": 280_000}
    finally:
        server.shutdown()
        server.server_close()
        app2.close()


def test_steel_mdm_act3_reject_leaves_erp_and_mem_clean(tmp_path: Path):
    app = SteelMDMStudioApp(tmp_path / "steel_reject", port=0)
    try:
        r3 = app.run_act_3()["act_result"]
        dec = app.decide_act_3(op="reject", run_id=r3["run_id"])["decision_result"]
        assert dec["status"] == "REJECTED"
        snap = app.state_snapshot()
        assert snap["w_lat"]["awaiting_approval_count"] == 0
        assert all(sp["spec_id"] != "SPEC-C" for sp in snap["w_mem"]["specs"])
        assert all(sk["sku_id"] != "ERP-SKU-9102" for sk in snap["w_real"]["skus"])
    finally:
        app.close()


def test_steel_mdm_investor_video_artifact_quality():
    root = Path(__file__).resolve().parents[1]
    mp4 = root / "video_investor_mdm" / "steel_mdm_investor_demo.mp4"
    builder = root / "video_investor_mdm" / "build_investor_mdm_video.py"
    assert builder.is_file()
    assert mp4.is_file(), f"missing built video: {mp4}"
    assert mp4.stat().st_size > 1_000_000

    probe = subprocess.run(
        [
            "/opt/homebrew/bin/ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(mp4),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    streams = {s["codec_type"]: s for s in info.get("streams", [])}
    v = streams.get("video", {})
    a = streams.get("audio", {})
    assert v.get("width") == 1920
    assert v.get("height") == 1080
    assert v.get("codec_name") == "h264"
    assert v.get("pix_fmt") == "yuv420p"
    assert v.get("r_frame_rate") == "30/1"
    assert int(v.get("start_pts", -1)) == 0
    assert a.get("codec_name") == "aac"
    assert int(a.get("channels", 0)) == 2
    duration = float(info["format"]["duration"])
    assert 120.0 <= duration <= 250.0

    sig = subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-v",
            "info",
            "-i",
            str(mp4),
            "-vf",
            "select=eq(n\\,0),signalstats,metadata=mode=print",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", sig.stderr)
    assert match is not None, sig.stderr[-400:]
    assert float(match.group(1)) > 10.0


def test_steel_mdm_studio_and_video_zero_english_gate(tmp_path: Path):
    from playwright.sync_api import sync_playwright
    from video_investor_mdm import build_investor_mdm_video as v_mdm

    # 1) 视频全部解说与字幕零英文字母
    for scene in v_mdm.SCENES:
        assert re.findall(r"[A-Za-z]+", scene.badge) == []
        assert re.findall(r"[A-Za-z]+", scene.title) == []
        assert re.findall(r"[A-Za-z]+", scene.narration) == []

    # 2) 支持纯中文输入框参数提交 + 全流程所有页面状态 DOM 零英文字母
    app = SteelMDMStudioApp(tmp_path / "steel_zero_en", port=0)
    server = create_server(app, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    app.port = port
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    try:
        cn_probe = app.try_register_sku(
            {
                "sku_id": "散码 9108",
                "invoice_title": "纯中文建码测试散码",
                "brand_id": "山西焦化厂",
                "unit_price_ton": 1620,
                "ash_pct": 13.0,
                "sulfur_pct": 0.85,
                "canonical_spec_id": "二级冶金焦",
            }
        )["probe_result"]
        assert cn_probe["blocked"] is False
        assert cn_probe["sku_id"] == "ERP-SKU-9108"

        app.reset()

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})

            def _assert_page_no_en(stage: str) -> None:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                text = page.evaluate(
                    """() => {
                        const arr = [document.title || "", document.body.innerText || ""];
                        document.querySelectorAll("input, select, textarea").forEach(el => {
                            arr.push(el.value || "", el.placeholder || "");
                        });
                        return arr.join("\\n");
                    }"""
                )
                hits = re.findall(r"[A-Za-z]+", text)
                assert hits == [], f"{stage} 发现英文字母: {hits!r}"

            _assert_page_no_en("init")
            app.run_act_1()
            _assert_page_no_en("act1")
            app.reindex_legacy_erp()
            _assert_page_no_en("reindex")
            app.run_act_2()
            _assert_page_no_en("act2")
            app.run_act_3()
            _assert_page_no_en("act3-hold")
            app.decide_act_3(op="approve")
            _assert_page_no_en("act3-approved")
            app.run_act_4()
            _assert_page_no_en("act4")
            app.revert_action_run()
            _assert_page_no_en("act5-revert")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()


