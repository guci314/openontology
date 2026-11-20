"""投资人路演演播室 #1（裸 MCP vs OpenOntology 三看板）真实 Odoo 19 集成测试。

覆盖：
1. Odoo 隔离（不触碰 :8789 的 O2C-CUST-001 / O2C-SO-001 / O2C-SO-002）与密钥脱敏；
2. 第一幕：全知对齐 + 未出库强开票拦截 + 裸 MCP 失控对照；
3. 第二幕：撒谎执行器穿帮 -> DEVICE_STATE_MISMATCH 读回否决 + rebase() 零幻觉；
4. 第三幕：超授信挂起在持久 W_lat -> 跨重启保留待批动作 -> 通过 execute_feishu_card_slash_command
   不触碰 :8789 状态目录直接回调 HTTP /api/feishu/callback 完成真实 Odoo 写入与卡片刷新；
5. 第三幕驳回分支：保持 Odoo 原单草稿零改动；
6. 第四幕：ClosedLoopMPCController 首次执行器漂移自动重规划并闭环收款清零。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any

from open_ontology import DEVICE_STATE_MISMATCH, MPC_GOAL_REACHED, OntologyRuntime

from examples.o2c_hermes_mcp_server import execute_feishu_card_slash_command
from examples.o2c_three_boards_studio import (
    ThreeBoardsStudioApp,
    create_server,
    render_studio_html,
)
from examples.odoo_o2c_adapter import PARTNER_REF, OdooO2CAdapter
from examples import o2c_ontology as o2c


def _make_fake_feishu_profile(tmp_path: Path, secret: str = "sec_test_never_leak_987654") -> Path:
    prof = tmp_path / "hermes_profile"
    pairing = prof / "platforms" / "pairing"
    pairing.mkdir(parents=True, exist_ok=True)
    (prof / ".env").write_text(
        "\n".join(
            [
                "FEISHU_DOMAIN=feishu",
                "FEISHU_APP_ID=cli_test_three_boards",
                f"FEISHU_APP_SECRET={secret}",
                "FEISHU_HOME_CHANNEL=oc_test_studio_room",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (pairing / "feishu-approved.json").write_text(
        json.dumps(["ou_director_approved_01"]), encoding="utf-8"
    )
    return prof


def _mock_feishu_transport_factory(recorded: list[dict[str, Any]]):
    def _transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        del headers, timeout
        parsed = json.loads(body.decode("utf-8") or "{}") if body else None
        recorded.append(
            {
                "method": method,
                "url": url,
                "body": parsed,
            }
        )
        if url.endswith("/tenant_access_token/internal"):
            return 200, json.dumps(
                {"code": 0, "tenant_access_token": "t-mock-token", "expire": 7200}
            ).encode("utf-8")
        if "/open-apis/im/v1/messages/" in url and method == "PATCH":
            return 200, json.dumps({"code": 0, "msg": "ok"}).encode("utf-8")
        if url.endswith("/open-apis/im/v1/messages") or "receive_id_type=" in url:
            return 200, json.dumps(
                {"code": 0, "data": {"message_id": "om_mock_studio_001"}}
            ).encode("utf-8")
        return 200, json.dumps({"code": 0}).encode("utf-8")

    return _transport


def _snapshot_default_partner_refs() -> dict[str, str]:
    probe_rt = OntologyRuntime(o2c.spec)
    try:
        probe_adapter = OdooO2CAdapter(probe_rt)
        rows = probe_adapter.client.search_read(
            "sale.order",
            [("partner_id.ref", "=", PARTNER_REF), ("client_order_ref", "in", ["O2C-SO-001", "O2C-SO-002"])],
            fields=["id", "client_order_ref", "state"],
        )
        return {str(r["client_order_ref"]): f"{r['id']}:{r['state']}" for r in rows}
    finally:
        probe_rt.close()


def test_studio_acts_1_to_4_restart_and_feishu_forward_real_odoo(tmp_path: Path):
    secret_val = "sec_super_secret_should_never_leak_987654"
    profile_dir = _make_fake_feishu_profile(tmp_path, secret=secret_val)
    state_dir = tmp_path / "studio_state"
    feishu_calls: list[dict[str, Any]] = []
    transport = _mock_feishu_transport_factory(feishu_calls)

    baseline_default_orders = _snapshot_default_partner_refs()

    app = ThreeBoardsStudioApp(
        state_dir,
        profile_dir=profile_dir,
        partner_ref="TEST-3B-ISO-001",
        partner_name="测试隔离三看板客户",
        order_seeds=(("T3B-SO-001", 2, 10_000.0), ("T3B-SO-002", 12, 10_000.0)),
        bare_partner_ref="TEST-3B-BARE-001",
        bare_partner_name="测试隔离裸MCP客户",
        bare_order_seeds=(("TBARE-SO-001", 2, 10_000.0),),
        feishu_transport=transport,
    )

    try:
        init_snap = app.state_snapshot()
        assert not (state_dir / "w_lat.sqlite3").exists()
        assert secret_val not in json.dumps(init_snap, ensure_ascii=False)
        assert secret_val not in render_studio_html(init_snap)
        assert secret_val not in (state_dir / "studio_meta.json").read_text(encoding="utf-8")
        assert init_snap["feishu"]["configured"] is True

        # 第一幕：未出库强行开票拦截 + 合法确认 T3B-SO-001
        r1 = app.run_act_1()
        a1 = r1["act_result"]
        assert a1["blocked_illegal_invoice"]["blocked"] is True
        assert a1["blocked_illegal_invoice"]["odoo_untouched_order_state"] == "draft"
        assert a1["confirmed_order_1"]["settled_status"] == "COMMITTED"
        assert a1["confirmed_order_1"]["mem_status"] == "confirmed"
        assert a1["confirmed_order_1"]["odoo_docs"]["order_state"] == "sale"
        assert r1["state"]["bare_mcp"]["brain_world_split"] is True

        # 第二幕：撒谎执行器穿帮 -> DEVICE_STATE_MISMATCH + rebase
        r2 = app.run_act_2()
        a2 = r2["act_result"]
        assert a2["settled_status"] == "REJECTED"
        assert a2["error_code"] == DEVICE_STATE_MISMATCH
        assert a2["mem_status_after"] == "confirmed"
        assert a2["rebased_pending_count"] == 0
        assert a2["odoo_docs"]["delivery_state"] != "done"
        assert r2["state"]["bare_mcp"]["belief_status"] == "shipped"

        # 第三幕：超授信挂起在 W_lat，真实 Odoo T3B-SO-002 保持 draft，并发送飞书卡片
        r3 = app.run_act_3()
        a3 = r3["act_result"]
        assert a3["latent_status"] == "OPEN"
        assert a3["odoo_order_state_before_approval"] == "draft"
        assert a3["mem_status_before_approval"] == "draft"
        assert a3["feishu_push_status"] == "sent"
        assert a3["feishu_message_id"] == "om_mock_studio_001"
        run_id_act3 = a3["run_id"]
        callback_token = str(
            (state_dir / "studio_meta.json").read_text(encoding="utf-8")
        )
        assert secret_val not in callback_token
        meta_obj = json.loads(callback_token)
        tok = meta_obj["feishu_card"]["callback_token"]
    finally:
        app.close()

    # 进程重启恢复测试：原状态与待签批 move 必须完好保留，不重播/不洗库
    app2 = ThreeBoardsStudioApp(
        state_dir,
        profile_dir=profile_dir,
        partner_ref="TEST-3B-ISO-001",
        partner_name="测试隔离三看板客户",
        order_seeds=(("T3B-SO-001", 2, 10_000.0), ("T3B-SO-002", 12, 10_000.0)),
        bare_partner_ref="TEST-3B-BARE-001",
        bare_partner_name="测试隔离裸MCP客户",
        bare_order_seeds=(("TBARE-SO-001", 2, 10_000.0),),
        feishu_transport=transport,
    )
    server = create_server(app2, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    app2.port = port
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    try:
        resumed = app2.state_snapshot()
        assert resumed["w_lat"]["awaiting_approval_count"] == 1
        assert resumed["w_lat"]["awaiting_approval_moves"][0]["run_id"] == run_id_act3
        assert resumed["w_real"]["orders"][1]["odoo_order_state"] == "draft"

        # GET / & GET /api/state 健康且不泄露密钥
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as resp:
            api_body = resp.read().decode("utf-8")
            assert secret_val not in api_body

        # 错误令牌必须拒绝
        bad_cb = app2.handle_feishu_callback(
            {"op": "approve", "run_id": run_id_act3, "token": "wrong-token"}
        )
        assert bad_cb["ok"] is False

        # 通过 Hermes /card 转发至演播室 HTTP 回调：必须不触碰 unused_8789 状态目录
        unused_8789 = tmp_path / "unused_8789_state"
        raw_cmd = "/card " + json.dumps(
            {
                "op": "approve",
                "run_id": run_id_act3,
                "token": tok,
                "studio_callback_url": f"http://127.0.0.1:{port}/api/feishu/callback",
            },
            ensure_ascii=False,
        )
        fwd = execute_feishu_card_slash_command(
            raw_cmd,
            state_dir=unused_8789,
            profile_dir=profile_dir,
            operator_open_id="ou_director_approved_01",
            chat_id="oc_test_studio_room",
            feishu_transport=transport,
        )
        assert fwd["ok"] is True
        assert not (unused_8789 / "o2c_erp.sqlite3").exists()

        after_cb = app2.state_snapshot()
        assert not (state_dir / "w_lat.sqlite3").exists()
        assert after_cb["w_lat"]["awaiting_approval_count"] == 0
        assert after_cb["w_real"]["orders"][1]["odoo_order_state"] == "sale"
        assert after_cb["w_mem"]["orders"][1]["mem_status"] == "confirmed"
        assert any(call["method"] == "PATCH" for call in feishu_calls)

        # 第四幕：ClosedLoopMPCController + 首次执行器漂移故障自愈 + 全额核销清零
        r4 = app2.run_act_4(inject_first_ship_drift=True)
        a4 = r4["act_result"]
        mpc = a4["mpc"]
        assert mpc["status"] == MPC_GOAL_REACHED
        assert mpc["goal_reached"] is True
        assert mpc["replans_used"] >= 1
        assert mpc["snapshots_refreshed"] >= 1
        assert mpc["steps_committed"] == 3
        assert mpc["trajectory"][0]["status"] == "REJECTED"
        assert mpc["trajectory"][0]["error_code"] == DEVICE_STATE_MISMATCH
        assert a4["mem_status_after"] == "paid"
        assert a4["ar_balance_cents"] == 0
        assert a4["odoo_docs"]["invoice_state"] == "posted"
        assert a4["odoo_docs"]["payment_state"] in ("paid", "in_payment")
        assert float(a4["odoo_docs"]["amount_residual"]) == 0.0
    finally:
        server.shutdown()
        server.server_close()
        app2.close()

    # 确认没有动摇 :8789 使用的默认演示客户 O2C-CUST-001 订单引用
    assert _snapshot_default_partner_refs() == baseline_default_orders


def test_studio_act3_reject_keeps_odoo_draft(tmp_path: Path):
    profile_dir = _make_fake_feishu_profile(tmp_path)
    feishu_calls: list[dict[str, Any]] = []
    app = ThreeBoardsStudioApp(
        tmp_path / "studio_reject",
        profile_dir=profile_dir,
        partner_ref="TEST-3B-REJ-001",
        partner_name="测试驳回分支客户",
        order_seeds=(("T3BR-SO-001", 2, 10_000.0), ("T3BR-SO-002", 12, 10_000.0)),
        bare_partner_ref="TEST-3B-REJ-BARE",
        bare_partner_name="测试驳回裸MCP客户",
        bare_order_seeds=(("T3BRB-SO-001", 2, 10_000.0),),
        feishu_transport=_mock_feishu_transport_factory(feishu_calls),
    )
    try:
        r3 = app.run_act_3()
        run_id = r3["act_result"]["run_id"]
        res = app.decide_act_3(op="reject", run_id=run_id, operator_open_id="ou_director_01")
        assert res["ok"] is True
        assert res["final_status"] == "REJECTED"
        assert res["state"]["w_real"]["orders"][1]["odoo_order_state"] == "draft"
        assert res["state"]["w_mem"]["orders"][1]["mem_status"] == "draft"
        assert res["state"]["w_lat"]["awaiting_approval_count"] == 0
    finally:
        app.close()


def test_investor_o2c_video_artifact_quality():
    root = Path(__file__).resolve().parents[1]
    mp4 = root / "video_investor_o2c" / "o2c_three_boards_vs_mcp.mp4"
    builder = root / "video_investor_o2c" / "build_investor_o2c_video.py"
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
    assert 120.0 <= duration <= 260.0

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


def test_o2c_studio_and_video_zero_english_gate(tmp_path: Path):
    from playwright.sync_api import sync_playwright
    from video_investor_o2c import build_investor_o2c_video as v_o2c

    # 1) 视频全部解说与字幕零英文字母
    for scene in v_o2c.SCENES:
        assert re.findall(r"[A-Za-z]+", scene.badge) == []
        assert re.findall(r"[A-Za-z]+", scene.title) == []
        assert re.findall(r"[A-Za-z]+", scene.narration) == []

    # 2) 全流程所有状态下，浏览器 DOM 可见文本（含主页面与实账凭据大屏）零英文字母
    profile_dir = _make_fake_feishu_profile(tmp_path)
    feishu_calls: list[dict[str, Any]] = []
    app = ThreeBoardsStudioApp(
        tmp_path / "studio_zero_en",
        profile_dir=profile_dir,
        partner_ref="TEST-3B-ZEN-001",
        partner_name="零英文门禁演示客户",
        order_seeds=(("T3B-ZEN-001", 2, 10_000.0), ("T3B-ZEN-002", 12, 10_000.0)),
        bare_partner_ref="TEST-3B-ZEN-BARE",
        bare_partner_name="零英文门禁对照客户",
        bare_order_seeds=(("T3BZ-RAW-001", 2, 10_000.0),),
        feishu_transport=_mock_feishu_transport_factory(feishu_calls),
    )
    server = create_server(app, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    app.port = port
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})

            def _assert_page_no_en(url: str, stage: str) -> None:
                page.goto(url, wait_until="networkidle")
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
                assert hits == [], f"{stage} ({url}) 发现英文字母: {hits!r}"

            _assert_page_no_en(f"http://127.0.0.1:{port}/", "init")
            _assert_page_no_en(f"http://127.0.0.1:{port}/?view=voucher", "init-voucher")
            app.run_act_1()
            _assert_page_no_en(f"http://127.0.0.1:{port}/", "act1")
            app.run_act_2()
            _assert_page_no_en(f"http://127.0.0.1:{port}/", "act2")
            app.run_act_3()
            _assert_page_no_en(f"http://127.0.0.1:{port}/", "act3-hold")
            app.decide_act_3(op="approve", operator_open_id="ou_director_approved_01")
            _assert_page_no_en(f"http://127.0.0.1:{port}/", "act3-approved")
            app.run_act_4(inject_first_ship_drift=True)
            _assert_page_no_en(f"http://127.0.0.1:{port}/", "act4")
            _assert_page_no_en(f"http://127.0.0.1:{port}/?view=voucher", "act4-voucher")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()


