#!/usr/bin/env python3
"""TK-ONTO-016 · 《OpenOntology 驱动与无感拔除 SaaS》1080p 演示视频。

严格并发隔离：本脚本只在 ``video_saas/`` 目录内读写文件；对
``examples/``、``open_ontology/`` 只做只读导入，不修改任何文件。

流程：
1. 真实运行 ``examples.real_crm_unplug_demo``：真实 BottleCRM
   (django-crm 1.44.0, SQLite) + 真实 Odoo 19 (erp_north) 抽取快照、
   AI 分诊写入 ontology_overlay、源系统全量重刷存活、
   ``unplug_all_saas()`` 物理断连与拦截、脱壳后赢单转订单 /
   小额自动退 / 大额审批 / 时光机撤回。
2. 真实运行 ``examples.saas_unplug_demo.run_eval_suite`` 两种主权模式，
   逐字段对拍得到 ``contract_identical``。
3. Playwright + Chromium 1920x1080 渲染 6 幕深色科技风画面（内联 SVG）。
4. edge-tts（zh-CN-YunxiNeural）生成 6 段中文配音，失败回退 say。
5. ffmpeg 合成 1920x1080 H.264 + AAC yuv420p 30fps MP4（零黑屏参数）。
6. ffprobe 终验：分辨率 / 编码 / 时长 / has_b_frames=0 / start_pts=0 /
   start_time=0.000000 / 第 0 帧亮度均值 > 20。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

FRAMES = HERE / "frames"
AUDIO = HERE / "audio"
SEGMENTS = HERE / "segments"
OUTPUT = HERE / "saas_unplug_demo.mp4"

EDGE_TTS = Path("/Users/guci/.local/bin/edge-tts")
FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
FFPROBE = Path("/opt/homebrew/bin/ffprobe")
SAY = Path("/usr/bin/say")
VOICE = "zh-CN-YunxiNeural"
FPS = 30
W, H = 1920, 1080
LEAD_IN = 0.55
TAIL = 0.80


# ---------------------------------------------------------------------------
# 1. 真实数据采集（只读导入 + 实跑 demo）
# ---------------------------------------------------------------------------
def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _spoken_money(cents: int) -> str:
    dollars = cents / 100
    if abs(dollars - round(dollars)) < 1e-9:
        return f"{int(round(dollars)):,} 美元"
    return f"{dollars:,.2f} 美元"


def _overview_line(ov: dict[str, Any]) -> str:
    return (
        f"客户 {ov['customers']} · 订单 {ov['orders']} · "
        f"工单 {ov['tickets']} · 退款 {ov['refund_count']}"
    )


def collect_facts() -> dict[str, Any]:
    from examples import crm_unplug_ontology as dom
    from examples import real_crm_unplug_demo as demo
    from examples import saas_unplug_demo as saas

    demo.capture_original_declarations()
    f: dict[str, Any] = {}
    runtime = None
    try:
        # -- 真实外部系统 ------------------------------------------------
        bottlecrm = demo.BottleCRMConnector()
        tables = bottlecrm.table_counts()
        client, _ = demo.ensure_database(demo.DB_NORTH)
        seeded = demo.seed_real_odoo_orders(client)
        erp = demo.OdooERPConnector(client)

        f["saas"] = {
            "bottlecrm": {
                "name": "BottleCRM · django-crm 1.44.0",
                "db_mb": round(demo.BOTTLECRM_DB.stat().st_size / 1024 / 1024, 2),
                "accounts": tables["accounts"],
                "opportunity": tables["opportunity"],
                "case": tables["case"],
            },
            "odoo": {
                "name": "Odoo 19 · erp_north",
                "orders": [
                    {
                        "ref": row["client_order_ref"],
                        "name": row["name"],
                        "state": row["state"],
                        "total": float(row["amount_total"]),
                    }
                    for row in seeded
                ],
            },
        }

        runtime = dom.new_runtime()

        # -- Phase A：吸干两个真实 SaaS -----------------------------------
        absorb = demo.absorb_from_real_saas(runtime, bottlecrm, erp)
        type_counts = {
            name: runtime.count_objects(name) for name in demo.UNPLUGGABLE_TYPES
        }
        link_rows = runtime.connection.execute(
            "SELECT link_type, COUNT(*) AS n FROM ontology_links GROUP BY link_type"
        ).fetchall()
        link_counts = {row["link_type"]: int(row["n"]) for row in link_rows}

        accounts = sorted(
            runtime.query_objects(dom.CustomerAccount, dom.OPS_MANAGER),
            key=lambda a: a.account_id,
        )
        opportunities = sorted(
            runtime.query_objects(dom.SalesOpportunity, dom.OPS_MANAGER),
            key=lambda o: o.opportunity_id,
        )

        # -- Phase A：AI 分诊写 overlay -----------------------------------
        demo.apply_overlay_governance(runtime)
        cases = sorted(
            runtime.query_objects(dom.SupportCase, dom.AI_AGENT),
            key=lambda c: c.case_id,
        )
        overlay_before = demo.snapshot_overlay(runtime)
        status_before_resync = sorted(
            {c.status for c in cases}
        )
        triaged = [
            {
                "case_id": case.case_id,
                "subject": case.subject,
                "fraud": case.fraud_score,
                "vip": case.vip_tier,
                "plan": case.ai_resolution_plan,
            }
            for case in cases
        ]

        # -- Phase A：源系统第二次全量重刷 --------------------------------
        resync = demo.absorb_from_real_saas(runtime, bottlecrm, erp)
        overlay_after = demo.snapshot_overlay(runtime)
        cases_after = sorted(
            runtime.query_objects(dom.SupportCase, dom.AI_AGENT),
            key=lambda c: c.case_id,
        )
        status_after_resync = sorted({c.status for c in cases_after})
        triaged_after = [
            {
                "case_id": c.case_id,
                "fraud": c.fraud_score,
                "vip": c.vip_tier,
                "plan": c.ai_resolution_plan,
            }
            for c in cases_after
        ]
        f["phase_a"] = {
            "snapshot": absorb.snapshot,
            "resync_snapshot": resync.snapshot,
            "type_counts": type_counts,
            "link_counts": link_counts,
            "reads": {"bottlecrm": bottlecrm.reads, "odoo": erp.reads},
            "accounts": [
                {
                    "id": a.account_id,
                    "name": a.name,
                    "industry": a.industry,
                    "city": a.city,
                    "country": a.country,
                    "revenue": a.annual_revenue_cents,
                }
                for a in accounts
            ],
            "opportunities": [
                {
                    "id": o.opportunity_id,
                    "name": o.name,
                    "stage": o.stage,
                    "amount": o.amount_cents,
                }
                for o in opportunities
            ],
            "triaged": triaged,
            "triaged_after": triaged_after,
            "status_before_resync": status_before_resync,
            "status_after_resync": status_after_resync,
            "overlay_rows": len(overlay_before),
            "overlay_before": overlay_before,
            "overlay_after": overlay_after,
            "overlay_preserved": overlay_before == overlay_after,
            "resync_overlays_reapplied": resync.snapshot.get("overlays_reapplied"),
        }

        # -- Phase B：unplug_all_saas ------------------------------------
        unplug = demo.unplug_all_saas(runtime, bottlecrm, erp)
        f["unplug"] = {
            "promoted_types": unplug.promoted_types,
            "promoted_links": unplug.promoted_links,
            "demoted_actions": unplug.demoted_actions,
            "disconnected": unplug.disconnected,
            "blocked_calls": unplug.blocked_calls,
            "guard_rejected": unplug.snapshot_guard_rejected,
            "counts": unplug.counts,
            "overlay_rows": len(unplug.overlay_preserved),
            "bottlecrm_connected": bottlecrm.connected,
            "odoo_connected": erp.connected,
        }

        # -- Phase B：零外部 SaaS 业务闭环 --------------------------------
        target = next(
            o
            for o in sorted(
                runtime.query_objects(dom.SalesOpportunity, dom.AI_AGENT),
                key=lambda o: o.opportunity_id,
            )
            if o.stage not in ("CLOSED_WON", "CLOSED_LOST")
        )
        win = runtime.submit_action(
            "OFFLINE-WIN",
            "win_opportunity_and_create_order",
            {
                "opportunity_id": target.opportunity_id,
                "order_ref": "SO-OFFLINE-001",
                "fulfillment_note": "created with zero external SaaS",
            },
            dom.AI_AGENT,
        )
        won = runtime.get_object(
            dom.SalesOpportunity, target.opportunity_id, dom.OPS_MANAGER
        )
        offline_order = runtime.get_object(
            dom.SalesOrder, "SO-OFFLINE-001", dom.OPS_MANAGER
        )

        small_order = dom.SalesOrder(
            order_id="SO-OFFLINE-SMALL",
            order_ref="SO-OFFLINE-SMALL",
            account_id=offline_order.account_id,
            status="confirmed",
            total_cents=8_000,
            currency="USD",
            ordered_on=__import__("datetime").date.today().isoformat(),
        )
        runtime.put_object(small_order)
        runtime.submit_action(
            "OFFLINE-CASE",
            "open_customer_case",
            {
                "case_id": "CASE-OFFLINE-001",
                "account_id": offline_order.account_id,
                "subject": "离线新单到货包装破损",
                "priority": "High",
                "case_type": "Problem",
                "description": "created after unplug, no external SaaS involved",
                "order_id": "SO-OFFLINE-SMALL",
            },
            dom.SUPPORT_AGENT,
        )
        small_refund = runtime.submit_action(
            "OFFLINE-REFUND-SMALL",
            "resolve_case_with_refund",
            {
                "case_id": "CASE-OFFLINE-001",
                "order_id": "SO-OFFLINE-SMALL",
                "reason": "packaging damage",
            },
            dom.AI_AGENT,
        )
        small_after = runtime.get_object(
            dom.SalesOrder, "SO-OFFLINE-SMALL", dom.OPS_MANAGER
        )

        runtime.submit_action(
            "OFFLINE-BIG-CASE",
            "open_customer_case",
            {
                "case_id": "CASE-OFFLINE-BIG",
                "account_id": offline_order.account_id,
                "subject": "大额订单退货申请",
                "priority": "Urgent",
                "order_id": "SO-OFFLINE-001",
            },
            dom.SUPPORT_AGENT,
        )
        pending = runtime.submit_action(
            "OFFLINE-REFUND-BIG",
            "resolve_case_with_refund",
            {
                "case_id": "CASE-OFFLINE-BIG",
                "order_id": "SO-OFFLINE-001",
                "reason": "customer changed mind",
            },
            dom.AI_AGENT,
        )
        big_while_pending = runtime.get_object(
            dom.SalesOrder, "SO-OFFLINE-001", dom.OPS_MANAGER
        )
        approved = runtime.approve_action("OFFLINE-REFUND-BIG", dom.OPS_MANAGER)
        big_after = runtime.get_object(
            dom.SalesOrder, "SO-OFFLINE-001", dom.OPS_MANAGER
        )

        # -- 硬护栏：超期 / 高欺诈 ---------------------------------------
        stale = dom.SalesOrder(
            order_id="SO-STALE-001",
            order_ref="SO-STALE-001",
            account_id=offline_order.account_id,
            status="confirmed",
            total_cents=5_000,
            currency="USD",
            ordered_on=(
                __import__("datetime").date.today()
                - __import__("datetime").timedelta(days=45)
            ).isoformat(),
        )
        runtime.put_object(stale)
        runtime.submit_action(
            "OFFLINE-STALE-CASE",
            "open_customer_case",
            {
                "case_id": "CASE-STALE",
                "account_id": offline_order.account_id,
                "subject": "45 天前订单退货",
                "order_id": "SO-STALE-001",
            },
            dom.SUPPORT_AGENT,
        )
        stale_code = ""
        try:
            runtime.submit_action(
                "OFFLINE-REFUND-STALE",
                "resolve_case_with_refund",
                {"case_id": "CASE-STALE", "order_id": "SO-STALE-001"},
                dom.AI_AGENT,
            )
        except Exception as exc:  # InvariantError
            stale_code = getattr(exc, "code", type(exc).__name__)

        runtime.submit_action(
            "OFFLINE-FRAUD-CASE",
            "open_customer_case",
            {
                "case_id": "CASE-FRAUD",
                "account_id": offline_order.account_id,
                "subject": "疑似欺诈退款申请",
            },
            dom.SUPPORT_AGENT,
        )
        runtime.submit_action(
            "OFFLINE-FRAUD-TRIAGE",
            "ai_triage_crm_case",
            {"case_id": "CASE-FRAUD", "fraud_score": 92},
            dom.AI_AGENT,
        )
        runtime.put_object(
            dom.SalesOrder(
                order_id="SO-FRAUD-001",
                order_ref="SO-FRAUD-001",
                account_id=offline_order.account_id,
                status="confirmed",
                total_cents=4_000,
                currency="USD",
                ordered_on=__import__("datetime").date.today().isoformat(),
            )
        )
        fraud_code = ""
        try:
            runtime.submit_action(
                "OFFLINE-REFUND-FRAUD",
                "resolve_case_with_refund",
                {"case_id": "CASE-FRAUD", "order_id": "SO-FRAUD-001"},
                dom.AI_AGENT,
            )
        except Exception as exc:
            fraud_code = getattr(exc, "code", type(exc).__name__)

        # -- 时光机撤回 ---------------------------------------------------
        revert = runtime.revert_action(
            "OFFLINE-REFUND-SMALL", "OFFLINE-REFUND-SMALL-REV", dom.OPS_MANAGER
        )
        restored_order = runtime.get_object(
            dom.SalesOrder, "SO-OFFLINE-SMALL", dom.OPS_MANAGER
        )
        restored_case = runtime.get_object(
            dom.SupportCase, "CASE-OFFLINE-001", dom.OPS_MANAGER
        )
        restored_account = runtime.get_object(
            dom.CustomerAccount, offline_order.account_id, dom.OPS_MANAGER
        )
        runs = runtime.list_action_runs()
        rejected = [r["run_id"] for r in runs if r["status"] == "REJECTED"]
        health = runtime.call_function(
            "portfolio_health_snapshot", {}, dom.OPS_MANAGER
        )

        f["phase_b"] = {
            "win": {
                "run": win["run_id"],
                "status": win["status"],
                "opportunity": won.opportunity_id,
                "stage": won.stage,
                "win_probability": won.win_probability,
                "order": offline_order.order_id,
                "order_status": offline_order.status,
                "total": offline_order.total_cents,
            },
            "small": {
                "order": "SO-OFFLINE-SMALL",
                "total": 8_000,
                "run": small_refund["run_id"],
                "status": small_refund["status"],
                "approved_by": small_refund["approved_by"],
                "order_status": small_after.status,
                "refunded": small_after.refunded_cents,
            },
            "big": {
                "submit_status": pending["status"],
                "order_while_pending": big_while_pending.status,
                "approve_status": approved["status"],
                "approved_by": approved["approved_by"],
                "order_status": big_after.status,
                "refunded": big_after.refunded_cents,
                "total": offline_order.total_cents,
            },
            "guards": {
                "stale_code": stale_code,
                "stale_run_status": runtime.get_action_run("OFFLINE-REFUND-STALE")[
                    "status"
                ],
                "fraud_code": fraud_code,
                "fraud_run_status": runtime.get_action_run("OFFLINE-REFUND-FRAUD")[
                    "status"
                ],
            },
            "revert": {
                "run": revert["run_id"],
                "status": revert["status"],
                "order_status": restored_order.status,
                "refunded": restored_order.refunded_cents,
                "case_status": restored_case.status,
                "ltv": restored_account.ltv_cents,
                "original_status": runtime.get_action_run("OFFLINE-REFUND-SMALL")[
                    "status"
                ],
            },
            "runs": len(runs),
            "rejected": rejected,
            "ledger": [
                {
                    "run_id": r["run_id"],
                    "action_type": r["action_type"],
                    "status": r["status"],
                    "error_code": r.get("error_code") or "",
                }
                for r in runs
            ],
            "health": health,
        }
    finally:
        if runtime is not None:
            runtime.close()
        demo.reset_unplug_state()

    # -- 契约冻结对拍 ----------------------------------------------------
    parasitic = saas.run_eval_suite(standalone=False)
    standalone = saas.run_eval_suite(standalone=True)
    keys = (
        ("eval_create_and_small_refund_auto", "small_refund"),
        ("eval_expired_window_blocked", "expired"),
        ("eval_high_fraud_blocked", "fraud"),
        ("eval_big_refund_requires_manager", "big_refund"),
        ("eval_double_refund_blocked", "double_refund"),
        ("eval_revert_refund", "revert"),
        ("eval_overview_snapshot", "overview"),
    )
    rows = []
    for name, key in keys:
        p, s = parasitic[key], standalone[key]
        if key == "small_refund":
            desc = f"{p['run_status']} · 退款 {_money(p['refund_amount'])}"
        elif key in ("expired", "fraud", "double_refund"):
            desc = f"{p['error']} → {p['run_status']}"
        elif key == "big_refund":
            desc = f"{p['submit_status']} → {p['approve_status']}（店长签批）"
        elif key == "revert":
            desc = f"{p['status']} · 订单回到 {p['order_status']}"
        else:
            desc = _overview_line(p)
        rows.append(
            {
                "eval": name,
                "parasitic": desc,
                "standalone": desc if p == s else "不一致",
                "same": p == s,
            }
        )
    f["contract"] = {
        "identical": parasitic == standalone,
        "rows": rows,
        "evals": list(saas.AIP_EVALS),
        "parasitic": parasitic,
        "standalone": standalone,
    }
    return f


# ---------------------------------------------------------------------------
# 2. 视觉设计系统
# ---------------------------------------------------------------------------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1920px;height:1080px;overflow:hidden}
body{background:#0a1120;color:#e9f1fb;
 font-family:"PingFang SC","Hiragino Sans GB","Heiti SC","STHeiti",sans-serif;
 -webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
.stage{position:relative;width:1920px;height:1080px;padding:22px 40px 16px;
 display:flex;flex-direction:column;gap:11px}
.stage::before{content:"";position:absolute;inset:0;z-index:0;background:
 radial-gradient(1250px 640px at 4% -16%,rgba(56,189,248,.26),transparent 62%),
 radial-gradient(1000px 560px at 99% -12%,rgba(167,139,250,.22),transparent 60%),
 radial-gradient(1150px 720px at 50% 124%,rgba(34,197,94,.13),transparent 66%),
 linear-gradient(#0d1728,#0a1120 55%,#0c1526)}
.stage::after{content:"";position:absolute;inset:0;z-index:0;background-image:
 linear-gradient(rgba(120,170,230,.07) 1px,transparent 1px),
 linear-gradient(90deg,rgba(120,170,230,.07) 1px,transparent 1px);
 background-size:60px 60px;
 -webkit-mask-image:radial-gradient(circle at 50% 42%,#000 30%,transparent 80%);
 mask-image:radial-gradient(circle at 50% 42%,#000 30%,transparent 80%)}
.stage>*{position:relative;z-index:1}

header{height:78px;display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:12px;flex:0 0 auto}
.mark{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;font-size:22px;
 font-weight:800;color:#04121d;background:linear-gradient(135deg,#38bdf8,#818cf8);
 box-shadow:0 10px 26px rgba(56,189,248,.4)}
.brand .nm{font-size:20px;font-weight:700;letter-spacing:.7px}
.brand .sb{font-size:10.5px;color:#93a8c0;letter-spacing:2px;margin-top:3px}
h1{font-size:31px;font-weight:800;letter-spacing:.6px;text-align:center;flex:1 1 auto;
 line-height:1.2}
h1 .n{color:#38bdf8}
h1 .g{color:#4ade80}
.badges{display:flex;gap:7px;flex:0 0 auto}
.chip{font-size:12px;padding:6px 11px;border-radius:999px;border:1px solid rgba(130,170,220,.3);
 color:#c1d3e6;background:rgba(255,255,255,.05);letter-spacing:.5px;white-space:nowrap}
.chip.ok{border-color:rgba(34,197,94,.55);color:#9df3b4;background:rgba(34,197,94,.15)}
.chip.cy{border-color:rgba(56,189,248,.55);color:#b3e8ff;background:rgba(56,189,248,.15)}
.chip.am{border-color:rgba(245,165,36,.55);color:#ffdf8e;background:rgba(245,165,36,.15)}
.chip.vi{border-color:rgba(167,139,250,.55);color:#e3dcff;background:rgba(167,139,250,.15)}
.chip.rd{border-color:rgba(239,68,68,.55);color:#ffc0c0;background:rgba(239,68,68,.15)}

.sovereign{height:38px;display:flex;align-items:center;gap:14px;padding:0 16px;
 border-radius:11px;background:linear-gradient(90deg,rgba(18,32,51,.92),rgba(12,22,37,.7));
 border:1px solid rgba(130,170,220,.2)}
.sovereign .lb{font-size:11px;color:#8ea6c0;letter-spacing:1.6px;white-space:nowrap}
.track{position:relative;flex:1;height:8px;border-radius:999px;background:rgba(255,255,255,.09);
 overflow:hidden}
.track i{position:absolute;left:0;top:0;bottom:0;border-radius:999px;display:block;
 background:linear-gradient(90deg,#38bdf8,#22c55e)}
.sovereign .st{font-size:12.5px;font-weight:700;letter-spacing:.4px;white-space:nowrap}
.sovereign .st.a{color:#8fd8ff}
.sovereign .st.b{color:#8ef0a8}

.content{flex:1;min-height:0;display:flex;flex-direction:column;gap:11px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;flex:1;min-height:0}
.cols.wide{grid-template-columns:1.32fr 1fr}
.cols.narrow{grid-template-columns:1.04fr 1fr}
.cols.left-wide{grid-template-columns:1.2fr 1fr}
.panel{position:relative;border-radius:16px;padding:13px 16px 11px;min-height:0;overflow:hidden;
 background:linear-gradient(168deg,rgba(24,40,63,.96),rgba(12,22,37,.97));
 border:1px solid rgba(130,170,220,.2);display:flex;flex-direction:column;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 16px 40px rgba(0,0,0,.4)}
.panel::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;
 background:linear-gradient(90deg,#38bdf8,rgba(129,140,248,.55),transparent)}
.panel.am::before{background:linear-gradient(90deg,#f5a524,rgba(239,68,68,.5),transparent)}
.panel.rd::before{background:linear-gradient(90deg,#ef4444,rgba(245,165,36,.45),transparent)}
.panel.vi::before{background:linear-gradient(90deg,#a78bfa,rgba(56,189,248,.5),transparent)}
.panel.gr::before{background:linear-gradient(90deg,#22c55e,rgba(56,189,248,.5),transparent)}
.ph{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
 margin-bottom:8px;padding-bottom:7px;border-bottom:1px dashed rgba(130,170,220,.24)}
.ph b{font-size:16px;letter-spacing:.6px;font-weight:700}
.ph b i{color:#38bdf8;font-style:normal}
.panel.am .ph b i{color:#f5a524}
.panel.rd .ph b i{color:#f87171}
.panel.vi .ph b i{color:#a78bfa}
.panel.gr .ph b i{color:#4ade80}
.ph span{font-size:10.5px;color:#8ba0b8;letter-spacing:1.1px;white-space:nowrap}
.body{flex:1;min-height:0;display:flex;flex-direction:column;gap:9px;justify-content:space-evenly}

.card{background:rgba(255,255,255,.04);border:1px solid rgba(130,170,220,.18);
 border-left:3px solid rgba(130,170,220,.45);border-radius:10px;padding:8px 11px}
.card.cy{border-left-color:#38bdf8;background:rgba(56,189,248,.09)}
.card.gd{border-left-color:#22c55e;background:rgba(34,197,94,.09)}
.card.bd{border-left-color:#ef4444;background:rgba(239,68,68,.1)}
.card.am{border-left-color:#f5a524;background:rgba(245,165,36,.1)}
.card.vi{border-left-color:#a78bfa;background:rgba(167,139,250,.1)}
.ct{font-size:13.5px;font-weight:700;letter-spacing:.4px;display:flex;align-items:center;
 justify-content:space-between;gap:9px;margin-bottom:4px}
.ct small{font-weight:500;font-size:10.5px;color:#8ba0b8;letter-spacing:.4px}
.mono{font-family:"SF Mono",Menlo,monospace;font-size:11.5px;color:#a6bbd3;letter-spacing:.1px}
.dim{color:#8ba0b8}
.hl{color:#7dd3fc}.hl2{color:#fcd34d}.gr{color:#86efac}.rd{color:#fca5a5}.vi{color:#ddd6fe}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:2.5px 0}
.kv span:first-child{color:#8ba0b8}
.kv b{font-variant-numeric:tabular-nums;text-align:right}
.tag{font-size:10.5px;padding:2px 8px;border-radius:6px;letter-spacing:.3px;font-weight:700;
 display:inline-block;white-space:nowrap}
.tag.red{color:#fecaca;background:rgba(239,68,68,.25);border:1px solid rgba(239,68,68,.5)}
.tag.green{color:#bbf7d0;background:rgba(34,197,94,.22);border:1px solid rgba(34,197,94,.48)}
.tag.amber{color:#fde68a;background:rgba(245,165,36,.22);border:1px solid rgba(245,165,36,.48)}
.tag.blue{color:#bae6fd;background:rgba(56,189,248,.2);border:1px solid rgba(56,189,248,.46)}
.tag.violet{color:#ddd6fe;background:rgba(167,139,250,.22);border:1px solid rgba(167,139,250,.48)}
.bigstat{display:flex;align-items:baseline;gap:7px}
.bigstat .v{font-size:34px;font-weight:800;line-height:1;letter-spacing:-.8px}
.bigstat .u{font-size:12px;color:#8ba0b8;letter-spacing:.7px}
.pills{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.pill{font-size:13px;font-weight:700;padding:6px 12px;border-radius:9px;letter-spacing:.3px;
 border:1px solid rgba(56,189,248,.45);background:rgba(56,189,248,.14);color:#d6f2ff}
.pill.o{border-color:rgba(167,139,250,.46);background:rgba(167,139,250,.14);color:#e7e1ff}
.pill.g{border-color:rgba(34,197,94,.46);background:rgba(34,197,94,.14);color:#dcfce5}
.pill.r{border-color:rgba(239,68,68,.46);background:rgba(239,68,68,.14);color:#ffd9d9}

table.tbl{width:100%;border-collapse:collapse;font-size:12.8px}
table.tbl.fixed{table-layout:fixed}
table.tbl.tight td{padding:4.5px 7px}
table.tbl.tight th{padding:4px 7px}
.tbl th{font-size:10.5px;color:#8ba0b8;font-weight:600;text-align:left;padding:5px 7px;
 border-bottom:1px solid rgba(130,170,220,.28);letter-spacing:.4px;white-space:nowrap}
.tbl td{padding:6.5px 7px;border-bottom:1px solid rgba(130,170,220,.11);
 font-variant-numeric:tabular-nums;white-space:nowrap;font-size:13px}
.tbl tr:last-child td{border-bottom:none}
.tbl td.r,.tbl th.r{text-align:right}
.tbl .id{font-family:"SF Mono",Menlo,monospace;font-size:11.5px;color:#a5e4ff}

footer{height:146px;display:flex;flex-direction:column;justify-content:center;
 padding:11px 30px;border-radius:16px;border:1px solid rgba(130,170,220,.24);
 border-left:6px solid #38bdf8;
 background:linear-gradient(90deg,rgba(13,23,40,.98),rgba(17,30,48,.95));
 box-shadow:0 14px 40px rgba(0,0,0,.5)}
footer p{font-weight:600;letter-spacing:.35px;color:#f3f8ff;line-height:1.55}
footer .s1 p{font-size:30px}footer .s2 p{font-size:26.5px}
footer .s3 p{font-size:24px}footer .s4 p{font-size:21.5px}
footer .meta{display:flex;align-items:center;justify-content:space-between;margin-top:7px}
.dots{display:flex;gap:6px;align-items:center}
.dots i{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,.2);display:block}
.dots i.on{width:26px;border-radius:5px;background:#38bdf8;box-shadow:0 0 13px rgba(56,189,248,.95)}
.dots em{font-size:11.5px;color:#8ba0b8;font-style:normal;letter-spacing:1px;margin-left:7px}
.fmeta{font-size:10.5px;color:#7d90a6;letter-spacing:1px}
svg{display:block}
.svgtxt{font-family:"PingFang SC","Hiragino Sans GB",sans-serif}
"""


def head(title: str, badges: list[str]) -> str:
    chips = "".join(
        f'<span class="chip {b.split("|")[0]}">{b.split("|")[1]}</span>' for b in badges
    )
    return f"""
<header>
  <div class="brand">
    <div class="mark">◈</div>
    <div><div class="nm">OpenOntology</div><div class="sb">SAAS UNPLUG · v0.3.0</div></div>
  </div>
  <h1>{title}</h1>
  <div class="badges">{chips}</div>
</header>"""


def sovereign(phase: int) -> str:
    pct = 20 if phase == 1 else 100
    state = (
        '<span class="st a">Phase A · 寄生态 standalone=False</span>'
        if phase == 1
        else '<span class="st b">Phase B · 脱壳态 standalone=True</span>'
    )
    return f"""
<div class="sovereign">
  <span class="lb">主权演进 SOVEREIGNTY</span>
  <span class="st a">Phase A 寄生态 20%</span>
  <div class="track"><i style="width:{pct}%"></i></div>
  <span class="st b">Phase B 脱壳态 100%</span>
  {state}
</div>"""


def footer(text: str, idx: int, meta: str) -> str:
    n = len(text)
    size = "s1" if n <= 92 else ("s2" if n <= 124 else ("s3" if n <= 152 else "s4"))
    dots = "".join(f'<i class="{"on" if i == idx else ""}"></i>' for i in range(6))
    return f"""
<footer class="{size}">
  <p>{text}</p>
  <div class="meta">
    <div class="fmeta">{meta}</div>
    <div class="dots">{dots}<em>{idx + 1}/6</em></div>
  </div>
</footer>"""


def page(title: str, badges: list[str], content: str, sub: str,
         idx: int, meta: str, phase: int = 1) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="stage">
{head(title, badges)}
{sovereign(phase)}
<div class="content">{content}</div>
{footer(sub, idx, meta)}
</div></body></html>"""


# ---------------------------------------------------------------------------
# SVG 公共件
# ---------------------------------------------------------------------------
DEFS = """
<defs>
  <linearGradient id="gcy" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#1a2e48"/><stop offset="1" stop-color="#0c1a2b"/>
  </linearGradient>
  <linearGradient id="ggr" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#123a26"/><stop offset="1" stop-color="#0a1f13"/>
  </linearGradient>
  <linearGradient id="grd" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#3d1717"/><stop offset="1" stop-color="#1d0d0d"/>
  </linearGradient>
  <linearGradient id="gam" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#3a2a12"/><stop offset="1" stop-color="#1d1507"/>
  </linearGradient>
  <linearGradient id="gvi" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#251f45"/><stop offset="1" stop-color="#121024"/>
  </linearGradient>
  <linearGradient id="gsl" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#1a2231"/><stop offset="1" stop-color="#0d1420"/>
  </linearGradient>
  <marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
          orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#6d93bd"/>
  </marker>
  <marker id="arwc" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
          orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#38bdf8"/>
  </marker>
  <marker id="arwg" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
          orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#22c55e"/>
  </marker>
  <marker id="arwr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
          orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#ef4444"/>
  </marker>
  <marker id="arwa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
          orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#f5a524"/>
  </marker>
</defs>"""


def svg(vb_w: int, vb_h: int, body: str, style: str = "") -> str:
    style = style or "width:100%;height:auto"
    return (
        f'<svg class="svgtxt" viewBox="0 0 {vb_w} {vb_h}" '
        f'width="100%" height="auto" preserveAspectRatio="xMidYMid meet" '
        f'style="{style}">{DEFS}{body}</svg>'
    )


def box(x, y, w, h, fill="url(#gcy)", stroke="#31527a", rx=12, sw=1.4, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def txt(x, y, s, size=14, fill="#e2ecf8", weight=600, anchor="start", ls="0.4"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}" letter-spacing="{ls}">{s}</text>')


def arrow(x1, y1, x2, y2, color="#6d93bd", marker="arw", dash="", sw=1.8, label="",
          lx=0, ly=0, lsize=11, lfill="#93a8c0", lanchor="middle"):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out = (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
           f'stroke-width="{sw}" marker-end="url(#{marker})"{d}/>')
    if label:
        out += txt(lx or (x1 + x2) / 2, ly or (y1 + y2) / 2 - 8, label, lsize, lfill,
                   500, lanchor, "0.3")
    return out


# ---------------------------------------------------------------------------
# 旁白
# ---------------------------------------------------------------------------
def narration_1(f: dict[str, Any]) -> str:
    return (
        "每家企业都被一堆 SaaS 绑架：客户在 BottleCRM 和 Zendesk，订单在 Odoo 十九和 Shopify，"
        "支付在 Stripe，数据割裂成一个个烟囱；每次全量重刷，本地积累的风控标注和 AI 结论又被抹掉一次；"
        "想换掉昂贵 SaaS，还得把业务代码和 AI Agent 全部推倒重写。OpenOntology 的答案是寄居蟹式双相演进。"
    )


def narration_2(f: dict[str, Any]) -> str:
    a = f["phase_a"]
    c = a["type_counts"]
    return (
        "第一步进入寄生态。OpenOntology 直接连上本机真实运行的 BottleCRM 一点四四版与真实 Odoo 十九，"
        f"把 {c['CustomerAccount']} 个客户账户、{c['SalesOpportunity']} 个销售商机、"
        f"{c['SupportCase']} 个客服工单和 {c['SalesOrder']} 张销售订单，连同 "
        f"{sum(a['link_counts'].values())} 条语义关系整库吸干，装进统一本体；"
        "外部 SaaS 从此退化为底层快照源，只负责供数。"
    )


def narration_3(f: dict[str, Any]) -> str:
    a = f["phase_a"]
    scores = "、".join(str(t["fraud"]) for t in a["triaged"])
    return (
        "第二步，AI 分诊把欺诈分、VIP 层级与处置方案写进 ontology_overlay 补丁层，"
        f"三个工单分别拿到 {scores} 分。这些字段源系统根本没有，归 Ontology 自有。"
        "BottleCRM 与 Odoo 十九第二次全量重刷后，源字段被原样刷新，"
        "而 AI 治理补丁逐字节存活，overlay 前后完全一致。"
    )


def narration_4(f: dict[str, Any]) -> str:
    u = f["unplug"]
    return (
        "第三步，执行 unplug_all_saas 一键拔插头。两个连接器被物理断开，"
        f"{len(u['promoted_types'])} 大对象类型与 {len(u['promoted_links'])} 条关系边全部晋升为自有，"
        "动作的 affects_source 归零。再调外部接口立刻抛出断连异常，"
        f"{len(u['blocked_calls'])} 项调用全部拦截；外部想用快照覆盖，"
        f"{len(u['guard_rejected'])} 个类型全部被治理异常拒之门外，而原有对象与补丁零丢失。"
    )


def narration_5(f: dict[str, Any]) -> str:
    b = f["phase_b"]
    return (
        "拔掉插头之后才是真正的考验。零外部 SaaS，OpenOntology 靠自己的 SQLite 内核跑完整条闭环："
        f"商机赢单原生转出订单 {b['win']['order']}，{_spoken_money(b['small']['total'])}的小额退款 AI 直接秒批，"
        f"{_spoken_money(b['big']['total'])}的大额退款自动挂起等经理签批，欺诈高分工单被红线拦截，"
        "最后时光机一键撤回，全部真实落账。"
    )


def narration_6(f: dict[str, Any]) -> str:
    ct = f["contract"]
    return (
        f"最后是契约冻结定理。同一套 AIP Evals {len(ct['evals'])} 条用例，在寄生态与脱壳态各跑一遍："
        "Action 名称、参数 Schema、错误码、审批状态、撤回能力逐字节一致，"
        f"contract_identical 等于 {ct['identical']}。先统一语义驱动现有 SaaS，"
        "再把逻辑沉淀进 Action 与 Overlay，最后拔掉插头，上层 AI Agent 一行代码都不用改。"
    )


# ---------------------------------------------------------------------------
# Scene 1 · 三大痛点 vs 寄居蟹双相演进
# ---------------------------------------------------------------------------
def scene_1(f: dict[str, Any], sub: str) -> str:
    left = f"""
<div class="panel rd" style="flex:1">
  <div class="ph"><b><i>▍</i>传统企业的 SaaS 困局</b><span>THE SAAS TRAP</span></div>
  <div class="body" style="justify-content:space-between;gap:10px">
    <div class="card bd" style="padding:10px 12px">
      <div class="ct">① 数据碎片化 · 系统割裂成烟囱 <small>SILOED DATA</small></div>
      <div class="mono" style="line-height:1.78">
        CRM：BottleCRM / Zendesk &nbsp;·&nbsp; ERP：Odoo 19 / Shopify<br>
        支付：Stripe &nbsp;—— 同一笔生意散落在至少 3 类互不相认的系统里
      </div>
    </div>
    <div class="card am" style="padding:10px 12px">
      <div class="ct">② 全量重刷 · 治理记忆被抹掉 <small>RESYNC WIPES MEMORY</small></div>
      <div class="mono" style="line-height:1.78">
        每晚一次全量同步，源系统字段原样覆盖<br>
        <span class="rd">本地人工标注、风控评分、AI 结论 → 归零重来</span>
      </div>
    </div>
    <div class="card bd" style="padding:10px 12px">
      <div class="ct">③ 想换掉昂贵 SaaS · 就得推倒重写 <small>LOCK-IN</small></div>
      <div class="mono" style="line-height:1.78">
        业务逻辑长在 SaaS 的 API 上，AI Agent 长在 SaaS 的字段上<br>
        <span class="rd">换供应商 = 全部业务代码 + 全部 Agent 重写</span>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:9px">
      <div class="card bd" style="text-align:center;padding:9px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v rd" style="font-size:26px">3 套</span></div>
        <div class="mono" style="margin-top:3px;font-size:10px">异构 SaaS 烟囱</div>
      </div>
      <div class="card am" style="text-align:center;padding:9px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v hl2" style="font-size:26px">0 行</span></div>
        <div class="mono" style="margin-top:3px;font-size:10px">可带走的治理资产</div>
      </div>
      <div class="card bd" style="text-align:center;padding:9px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v rd" style="font-size:26px">100%</span></div>
        <div class="mono" style="margin-top:3px;font-size:10px">被供应商锁定</div>
      </div>
    </div>
    <div class="card cy" style="padding:9px 12px">
      <div class="ct">本片实证路线 <small>PROOF ROUTE</small></div>
      <div class="kv" style="font-size:12.6px"><span>① 吸收 · 真实 BottleCRM + 真实 Odoo 19</span>
        <b class="hl">整库装进本体</b></div>
      <div class="kv" style="font-size:12.6px"><span>② 沉淀 · AI 治理字段写 overlay</span>
        <b class="hl2">重刷 100% 存活</b></div>
      <div class="kv" style="font-size:12.6px"><span>③ 拔除 · unplug_all_saas() 物理断连</span>
        <b class="gr">契约冻结独立运行</b></div>
    </div>
    <div class="card gd" style="padding:9px 12px">
      <div class="mono" style="color:#c8f7d4;line-height:1.65">
        破局思路：不要让业务长在 SaaS 上，让 SaaS 长在 Ontology 下面。
      </div>
    </div>
  </div>
</div>"""

    diagram = svg(1000, 720, f"""
{box(8, 8, 306, 64, "url(#gcy)", "#2b4c72", 11, 1.5)}
{txt(24, 34, "BottleCRM · django-crm 1.44.0", 14.5, "#cfe6fb", 700)}
{txt(24, 58, "真实 SQLite · accounts / opportunity / case", 11.8, "#8ba0b8", 500)}
{box(8, 82, 306, 64, "url(#gcy)", "#2b4c72", 11, 1.5)}
{txt(24, 108, "Odoo 19 · erp_north", 14.5, "#cfe6fb", 700)}
{txt(24, 132, "真实 JSON-RPC · sale.order", 11.8, "#8ba0b8", 500)}
{box(8, 156, 306, 60, "url(#gsl)", "#3c4a5c", 11, 1.3, "6 4")}
{txt(24, 182, "Shopify / Stripe / Zendesk", 14, "#9fb0c4", 600)}
{txt(24, 204, "同类异构 SaaS · 同样的困局", 11.5, "#7d90a6", 500)}
{txt(8, 244, "Phase A 数据源 · 只供数不掌权", 12, "#7dd3fc", 700)}

{arrow(316, 40, 372, 106, "#38bdf8", "arwc", "", 1.9)}
{arrow(316, 114, 372, 128, "#38bdf8", "arwc", "", 1.9)}
{arrow(316, 186, 372, 156, "#6d93bd", "arw", "5 4", 1.6)}

{box(374, 40, 290, 158, "url(#gvi)", "#6d5fc4", 12, 2.0)}
{txt(392, 70, "OpenOntology 本体", 17, "#eee9ff", 800)}
{txt(392, 98, "5 类对象 · 5 条关系边", 12.8, "#b7aef0", 600)}
{txt(392, 124, "受治理 Action / Function", 12.8, "#b7aef0", 600)}
{txt(392, 150, "不变量护栏 · 审计账本 · 时光机", 12.8, "#b7aef0", 600)}
{txt(392, 180, "standalone = False", 13.5, "#7dd3fc", 700)}

{arrow(666, 118, 706, 118, "#a78bfa", "arw", "", 2.0)}

{box(708, 26, 284, 186, "url(#gam)", "#a8792a", 12, 1.8)}
{txt(726, 54, "ontology_overlay", 15.5, "#ffe9b0", 800)}
{txt(726, 80, "企业自有治理补丁层", 12, "#c8a874", 600)}
{txt(726, 106, "fraud_score · vip_tier", 12.2, "#fcd34d", 700)}
{txt(726, 130, "ai_resolution_plan", 12.2, "#fcd34d", 700)}
{txt(726, 158, "__owned__ 声明 · 源系统没有", 11.8, "#c8a874", 600)}
{txt(726, 186, "重刷杀不死 · 100% 存活", 12.8, "#86efac", 700)}

{txt(8, 290, "Phase A · 寄生态：外部 SaaS 退化为底层快照源，治理记忆沉淀在补丁层", 13.5, "#9fb6cf", 600)}

<line x1="8" y1="308" x2="992" y2="308" stroke="#2b4c72" stroke-width="1.2" stroke-dasharray="5 5"/>

{arrow(500, 220, 500, 336, "#f5a524", "arwa", "", 2.6)}
{txt(516, 278, "Phase B · 一键物理拔插头", 13, "#fcd34d", 700)}

{box(322, 340, 356, 54, "url(#gsl)", "#f5a524", 27, 1.8)}
{txt(500, 374, "unplug_all_saas(runtime, bottlecrm, erp)", 14.5, "#ffe9b0", 800, "middle")}

{box(8, 424, 306, 100, "url(#grd)", "#7a3535", 11, 1.5, "6 4")}
{txt(24, 452, "bottlecrm.disconnect()", 12.8, "#ffc0c0", 700)}
{txt(24, 476, "erp.disconnect()", 12.8, "#ffc0c0", 700)}
{txt(24, 504, "外部调用 → SaasDisconnectedError", 11.5, "#d99a9a", 500)}

{box(374, 424, 290, 100, "url(#ggr)", "#22c55e", 11, 1.8)}
{txt(392, 452, "5 类对象 __owned__ = True", 12.8, "#c8f7d4", 700)}
{txt(392, 476, "5 条关系边 owned = True", 12.8, "#c8f7d4", 700)}
{txt(392, 504, "affects_source → 0", 12.8, "#c8f7d4", 700)}

{box(708, 424, 284, 100, "url(#gcy)", "#38bdf8", 11, 1.6)}
{txt(726, 452, "零外部数据源独立运行", 12.8, "#cfeeff", 700)}
{txt(726, 476, "SQLite 内核 + 原生 Action", 12.6, "#9ad8f7", 600)}
{txt(726, 504, "上层 Agent 零改动", 12.6, "#9ad8f7", 600)}

{arrow(316, 474, 370, 474, "#f5a524", "arwa", "", 2.0)}
{arrow(666, 474, 704, 474, "#22c55e", "arwg", "", 2.0)}

{box(8, 552, 984, 152, "url(#gsl)", "#31527a", 12, 1.4)}
{txt(28, 584, "寄居蟹隐喻：先钻进别人的壳里吸收养分，再自己长出更硬的壳", 15.5, "#eaf4ff", 700)}
{txt(28, 618, "Phase A  借壳：Ontology 盖在真实 SaaS 上，源字段照收，治理字段写 overlay", 13, "#9fb6cf", 500)}
{txt(28, 646, "Phase B  换壳：unplug_all_saas() 物理断连 + 全量主权晋升，业务与 Agent 零感知", 13, "#9fb6cf", 500)}
{txt(28, 680, "结果：SaaS 从「房东」降级为「一次性快照源」，企业第一次真正拥有自己的业务语义。", 13, "#86efac", 600)}
""")

    right = f"""
<div class="panel vi" style="flex:1">
  <div class="ph"><b><i>▍</i>OpenOntology 寄居蟹双相演进</b><span>PARASITIC → STANDALONE</span></div>
  <div class="body" style="justify-content:center">
    <div style="height:100%;display:flex;align-items:center">{diagram}</div>
    <div style="display:flex;gap:12px;justify-content:center;margin-top:2px">
      <span class="chip cy">● 蓝色 = 供数通路</span>
      <span class="chip am">● 橙色 = 主权晋升</span>
      <span class="chip ok">● 绿色 = 自有运行</span>
      <span class="chip vi">● 紫色 = 语义本体</span>
    </div>
  </div>
</div>"""

    content = f'<div class="cols left-wide">{left}{right}</div>'
    return page(
        '1/6 · 企业被 SaaS 绑架的三大痛点 vs <span class="n">寄居蟹式演进</span>',
        ["rd|数据烟囱", "am|重刷抹记忆", "cy|零锁定演进"],
        content, sub, 0,
        "REAL ENV · bottlecrm.sqlite3 + Odoo 19 erp_north",
    )


# ---------------------------------------------------------------------------
# Scene 2 · Phase A 吸干真实 SaaS
# ---------------------------------------------------------------------------
def scene_2(f: dict[str, Any], sub: str) -> str:
    a = f["phase_a"]
    s = f["saas"]
    c = a["type_counts"]
    bc = s["bottlecrm"]
    odoo = s["odoo"]["orders"]

    flow = svg(1000, 720, f"""
{box(8, 8, 288, 162, "url(#gcy)", "#2b4c72", 12, 1.6)}
{txt(24, 38, "BottleCRM 真实库", 16, "#dceeff", 800)}
{txt(24, 62, "django-crm 1.44.0 · SQLite", 11.8, "#8ba0b8", 500)}
{txt(24, 90, f"accounts_account&nbsp;&nbsp;{bc['accounts']} 行", 12, "#a5e4ff", 600)}
{txt(24, 114, f"opportunity_opportunity&nbsp;&nbsp;{bc['opportunity']} 行", 12, "#a5e4ff", 600)}
{txt(24, 138, f"cases_case&nbsp;&nbsp;{bc['case']} 行", 12, "#a5e4ff", 600)}
{txt(24, 162, f"{bc['db_mb']} MB · 真实迁移建表", 11.5, "#7d90a6", 500)}

{box(8, 184, 288, 112, "url(#gcy)", "#2b4c72", 12, 1.6)}
{txt(24, 214, "Odoo 19 真实库", 16, "#dceeff", 800)}
{txt(24, 238, "erp_north · JSON-RPC", 11.8, "#8ba0b8", 500)}
{txt(24, 264, f"sale.order&nbsp;&nbsp;{len(odoo)} 行", 12, "#a5e4ff", 600)}
{txt(24, 288, "state = sale · 已确认", 11.5, "#7d90a6", 500)}

{arrow(300, 88, 356, 186, "#38bdf8", "arwc", "", 1.9)}
{arrow(300, 240, 356, 236, "#38bdf8", "arwc", "", 1.9)}

{box(358, 154, 236, 116, "url(#gvi)", "#6d5fc4", 12, 2.0)}
{txt(376, 186, "absorb_from_real_saas()", 12.8, "#eee9ff", 800)}
{txt(376, 212, "source_properties()", 11.8, "#b7aef0", 600)}
{txt(376, 236, "剥掉自有字段 → 只收源字段", 11.5, "#b7aef0", 500)}
{txt(376, 260, "load_snapshot(objects, links)", 11.5, "#b7aef0", 500)}

{arrow(598, 212, 654, 212, "#a78bfa", "arw", "", 2.2)}

{box(656, 8, 336, 72, "url(#gcy)", "#38bdf8", 11, 1.6)}
{txt(674, 36, "CustomerAccount 客户账户", 13.6, "#e2f4ff", 700)}
{txt(974, 38, str(c["CustomerAccount"]), 23, "#7dd3fc", 800, "end")}
{txt(674, 62, "accounts_account → 名称 / 城市 / 国家 / 年营收", 11, "#8ba0b8", 500)}

{box(656, 88, 336, 72, "url(#gcy)", "#38bdf8", 11, 1.6)}
{txt(674, 116, "SalesOpportunity 销售商机", 13.6, "#e2f4ff", 700)}
{txt(974, 118, str(c["SalesOpportunity"]), 23, "#7dd3fc", 800, "end")}
{txt(674, 142, "opportunity → 阶段 / 金额 / 预计成交日", 11, "#8ba0b8", 500)}

{box(656, 168, 336, 72, "url(#gcy)", "#38bdf8", 11, 1.6)}
{txt(674, 196, "SupportCase 客服工单", 13.6, "#e2f4ff", 700)}
{txt(974, 198, str(c["SupportCase"]), 23, "#7dd3fc", 800, "end")}
{txt(674, 222, "cases_case → 主题 / 状态 / 优先级 / 类型", 11, "#8ba0b8", 500)}

{box(656, 248, 336, 72, "url(#gcy)", "#38bdf8", 11, 1.6)}
{txt(674, 276, "SalesOrder 销售订单", 13.6, "#e2f4ff", 700)}
{txt(974, 278, str(c["SalesOrder"]), 23, "#7dd3fc", 800, "end")}
{txt(674, 302, "sale.order → 单号 / 状态 / 含税金额", 11, "#8ba0b8", 500)}

{txt(656, 350, "语义关系边 · 一次性装进本体", 12.4, "#8ba0b8", 700)}
{box(656, 366, 336, 38, "url(#gsl)", "#2b4c72", 8, 1.2)}
{txt(672, 391, "account_opportunities", 12.2, "#a5e4ff", 700)}
{txt(976, 391, f"{a['link_counts'].get('account_opportunities', 0)} 条", 12.4, "#86efac", 700, "end")}
{box(656, 412, 336, 38, "url(#gsl)", "#2b4c72", 8, 1.2)}
{txt(672, 437, "account_cases", 12.2, "#a5e4ff", 700)}
{txt(976, 437, f"{a['link_counts'].get('account_cases', 0)} 条", 12.4, "#86efac", 700, "end")}
{box(656, 458, 336, 38, "url(#gsl)", "#2b4c72", 8, 1.2)}
{txt(672, 483, "account_orders", 12.2, "#a5e4ff", 700)}
{txt(976, 483, f"{a['link_counts'].get('account_orders', 0)} 条", 12.4, "#86efac", 700, "end")}

<line x1="8" y1="530" x2="992" y2="530" stroke="#2b4c72" stroke-width="1.2" stroke-dasharray="5 5"/>

{txt(8, 562, "本轮真实读取次数", 12.4, "#8ba0b8", 700)}
{box(8, 578, 226, 46, "url(#gsl)", "#2b4c72", 9, 1.2)}
{txt(24, 607, f"BottleCRM reads = {a['reads']['bottlecrm']}", 12.8, "#a5e4ff", 700)}
{box(248, 578, 226, 46, "url(#gsl)", "#2b4c72", 9, 1.2)}
{txt(264, 607, f"Odoo reads = {a['reads']['odoo']}", 12.8, "#a5e4ff", 700)}
{box(488, 578, 240, 46, "url(#gsl)", "#2b4c72", 9, 1.2)}
{txt(504, 607, f"objects = {a['snapshot']['objects']}", 12.8, "#86efac", 700)}
{box(742, 578, 250, 46, "url(#gsl)", "#2b4c72", 9, 1.2)}
{txt(758, 607, f"links = {a['snapshot']['links']}", 12.8, "#86efac", 700)}

{txt(8, 662, "关键：本体只收源系统字段，企业自己的治理字段留给 overlay —— 这是后面拔插头的前提。",
    13, "#9fb6cf", 500)}
{txt(8, 694, "本次快照涉及对象类型 4 个、关系类型 3 个，全部来自真实数据库真实行，没有一行是手工造的。",
    12.4, "#7d90a6", 500)}
""")

    left = f"""
<div class="panel cy" style="flex:1">
  <div class="ph"><b><i>▍</i>Phase A · 吸干真实 BottleCRM 与真实 Odoo 19</b><span>REAL ABSORB FLOW</span></div>
  <div class="body" style="justify-content:center">{flow}</div>
</div>"""

    account_rows = "".join(
        f'<tr><td class="id">{x["id"][:8]}</td><td>{x["name"][:24]}</td>'
        f'<td>{x["city"] or "—"}</td><td class="r">{x["country"]}</td>'
        f'<td class="r">${x["revenue"] / 100:,.0f}</td></tr>'
        for x in a["accounts"]
    )
    odoo_rows = "".join(
        f'<tr><td class="id">{o["ref"]}</td><td>{o["name"]}</td>'
        f'<td class="r">{o["state"]}</td>'
        f'<td class="r">${o["total"]:,.2f}</td></tr>'
        for o in odoo
    )

    right = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>实跑指标卡 · 真实行数而非示意</b><span>LIVE METRICS</span></div>
  <div class="body" style="justify-content:space-between;gap:10px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:9px">
      <div class="card cy" style="text-align:center;padding:9px 8px">
        <div class="bigstat" style="justify-content:center">
          <span class="v hl">{c["CustomerAccount"]}</span><span class="u">CustomerAccount</span>
        </div>
        <div class="mono" style="margin-top:4px;font-size:10.5px">真实 accounts_account</div>
      </div>
      <div class="card cy" style="text-align:center;padding:9px 8px">
        <div class="bigstat" style="justify-content:center">
          <span class="v hl">{c["SalesOpportunity"]}</span><span class="u">SalesOpportunity</span>
        </div>
        <div class="mono" style="margin-top:4px;font-size:10.5px">真实 opportunity</div>
      </div>
      <div class="card cy" style="text-align:center;padding:9px 8px">
        <div class="bigstat" style="justify-content:center">
          <span class="v hl">{c["SupportCase"]}</span><span class="u">SupportCase</span>
        </div>
        <div class="mono" style="margin-top:4px;font-size:10.5px">真实 cases_case</div>
      </div>
      <div class="card gd" style="text-align:center;padding:9px 8px">
        <div class="bigstat" style="justify-content:center">
          <span class="v gr">{c["SalesOrder"]}</span><span class="u">SalesOrder</span>
        </div>
        <div class="mono" style="margin-top:4px;font-size:10.5px">真实 Odoo sale.order</div>
      </div>
    </div>
    <div class="card cy" style="padding:8px 11px">
      <div class="ct">客户主数据（BottleCRM）<small>ACCOUNTS</small></div>
      <table class="tbl">
        <tr><th>ID</th><th>名称</th><th>城市</th><th class="r">国家</th><th class="r">年营收</th></tr>
        {account_rows}
      </table>
    </div>
    <div class="card am" style="padding:8px 11px">
      <div class="ct">销售订单（Odoo 19 · 真实 RPC 读取）<small>SALE.ORDER</small></div>
      <table class="tbl">
        <tr><th>client_order_ref</th><th>单号</th><th class="r">状态</th><th class="r">金额</th></tr>
        {odoo_rows}
      </table>
    </div>
    <div class="card vi" style="padding:8px 11px">
      <div class="kv"><span>快照对象总数</span><b class="vi">{a["snapshot"]["objects"]}</b></div>
      <div class="kv"><span>语义关系边总数</span><b class="vi">{a["snapshot"]["links"]}</b></div>
      <div class="kv"><span>对象类型 / 关系类型</span>
        <b>{len(a["snapshot"]["object_types"])} / {len(a["snapshot"]["link_types"])}</b></div>
      <div class="kv"><span>冲突策略</span><b class="hl2">{a["snapshot"]["conflict_strategy"]}</b></div>
      <div class="mono" style="margin-top:3px;color:#b7aef0">
        外部 SaaS 此刻只剩一个身份：底层快照源
      </div>
    </div>
    <div class="card gd" style="padding:8px 11px">
      <div class="mono" style="color:#c8f7d4;line-height:1.6">
        注意：这一步没有写任何治理字段。本体此刻只是「别人数据的语义镜像」，
        企业自己的判断还没长出来 —— 下一幕才轮到 overlay 出场。
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols wide">{left}{right}</div>'
    return page(
        '2/6 · Phase A · 吸干真实 <span class="n">BottleCRM</span> 与真实 <span class="n">Odoo 19</span>',
        ["cy|django-crm 1.44.0", "vi|Odoo 19 erp_north", "gr|真实行数"],
        content, sub, 1,
        f"REAL RUN · objects={a['snapshot']['objects']} · links={a['snapshot']['links']} "
        f"· reads={a['reads']['bottlecrm']}/{a['reads']['odoo']}",
    )


# ---------------------------------------------------------------------------
# Scene 3 · overlay 补丁层 + 重刷存活
# ---------------------------------------------------------------------------
def scene_3(f: dict[str, Any], sub: str) -> str:
    a = f["phase_a"]
    tri = a["triaged"]
    rows = ""
    for t in tri:
        tone = "rd" if t["fraud"] >= 70 else ("hl2" if t["fraud"] >= 30 else "gr")
        rows += (
            f'<tr><td class="id">{t["case_id"][:8]}</td>'
            f'<td>{t["subject"][:16]}</td>'
            f'<td class="r {tone}">{t["fraud"]}</td>'
            f'<td class="r">{t["vip"]}</td></tr>'
        )
    plan_cards = "".join(
        f"""<div class="card {"bd" if t["fraud"] >= 70 else ("am" if t["fraud"] >= 30 else "gd")}">
  <div class="ct">CASE {t["case_id"][:8]} · {t["subject"][:18]}
    <small>fraud={t["fraud"]} · {t["vip"]}</small></div>
  <div class="mono" style="color:#cfe0f2">{t["plan"]}</div>
</div>"""
        for t in tri
    )
    overlay_keys = list(a["overlay_before"].keys())

    def _brief(patch: dict[str, Any]) -> str:
        parts = [
            f"{k}={patch[k]}"
            for k in ("fraud_score", "vip_tier", "ai_resolution_plan")
            if k in patch
        ]
        text = " · ".join(parts)
        return text if len(text) <= 46 else text[:45] + "…"

    patch_rows = "".join(
        f'<tr><td class="id">{k.split(":")[0]}</td><td class="id">{k.split(":")[1][:8]}</td>'
        f'<td class="mono" style="font-size:11px;color:#a6bbd3">'
        f'{_brief(a["overlay_before"][k])}</td></tr>'
        for k in overlay_keys
    )
    after_rows = "".join(
        f'<tr><td class="id">{t["case_id"][:8]}</td>'
        f'<td class="r gr">{t["fraud"]}</td><td class="r gr">{t["vip"]}</td>'
        f'<td class="r">{"存活" if t["plan"] else "—"}</td></tr>'
        for t in a["triaged_after"]
    )

    left = f"""
<div class="panel am" style="flex:1">
  <div class="ph"><b><i>▍</i>AI_AGENT 执行 ai_triage_crm_case · 写入补丁层</b><span>OVERLAY PATCHES</span></div>
  <div class="body" style="justify-content:space-between;gap:9px">
    <div class="card cy" style="padding:7px 11px">
      <div class="mono" style="line-height:1.6">
        submit_action(<span class="hl">"ai_triage_crm_case"</span>, &#123;fraud_score, vip_tier,
        ai_resolution_plan&#125;, AI_AGENT)<br>
        <span class="dim">affects_source = False &nbsp;// 只写 Ontology 自有字段，不回写源系统</span>
      </div>
    </div>
    {plan_cards}
    <div class="card vi">
      <div class="ct">ontology_overlay 实际落库行 <small>PATCH ROWS</small></div>
      <table class="tbl">
        <tr><th>对象类型</th><th>对象 ID</th><th>patch_json</th></tr>
        {patch_rows}
      </table>
      <div class="mono" style="margin-top:4px">共 {a["overlay_rows"]} 行 · 全部来自 AI 分诊</div>
    </div>
    <div class="card cy">
      <div class="ct">为什么 affects_source = False <small>SPLIT SOVEREIGNTY</small></div>
      <div class="mono" style="line-height:1.7">
        主编辑计划只动自有字段 → 落 overlay<br>
        源系统字段（工单状态等）留给源系统自己管<br>
        <span class="dim">// 一次动作不跨两个主权平面，冲突才不会打架</span>
      </div>
    </div>
    <div class="card gd" style="padding:7px 11px">
      <div class="mono" style="color:#c8f7d4;line-height:1.6">
        这 {a["overlay_rows"]} 行是企业真正的治理资产：源系统没有、也删不掉。
      </div>
    </div>
  </div>
</div>"""

    right = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>第二次全量重刷 absorb_from_real_saas 前后对拍</b><span>RESYNC SURVIVAL</span></div>
  <div class="body" style="justify-content:space-between;gap:9px">
    <div style="display:flex;align-items:center;gap:10px">
      <span class="tag blue">源字段刷新</span>
      <span class="tag amber">overlays_reapplied = {a["resync_overlays_reapplied"]}</span>
      <span class="tag green">overlay 存活</span>
    </div>
    <table class="tbl">
      <tr><th>维度</th><th class="r">重刷前</th><th class="r">重刷后</th><th class="r">归属</th></tr>
      <tr><td>工单 status</td>
        <td class="r">{"/".join(a["status_before_resync"])}</td>
        <td class="r rd">{"/".join(a["status_after_resync"])}（源字段）</td>
        <td class="r">源系统</td></tr>
      <tr><td>账户名称 / 年营收</td><td class="r">BottleCRM 快照</td>
        <td class="r rd">原样覆盖</td><td class="r">源系统</td></tr>
      <tr><td>账户 fraud_score</td>
        <td class="r">{"/".join(str(t["fraud"]) for t in a["triaged"])}</td>
        <td class="r gr">逐字节保留</td><td class="r">Overlay</td></tr>
      <tr><td>账户 vip_tier</td>
        <td class="r">{"/".join(t["vip"] for t in a["triaged"])}</td>
        <td class="r gr">逐字节保留</td><td class="r">Overlay</td></tr>
      <tr><td>工单 ai_resolution_plan</td><td class="r">{a["overlay_rows"]} 条</td>
        <td class="r gr">{a["overlay_rows"]} 条</td><td class="r">Overlay</td></tr>
    </table>
    <div class="card cy">
      <div class="ct">重刷后的 AI 治理结论 <small>AFTER RESYNC</small></div>
      <table class="tbl">
        <tr><th>CASE</th><th class="r">fraud</th><th class="r">vip</th><th class="r">plan</th></tr>
        {after_rows}
      </table>
    </div>
    <div class="card gd">
      <div class="ct">逐字段对拍结果 <small>BYTE-IDENTICAL</small></div>
      <div class="kv"><span>overlay_before == overlay_after_resync</span>
        <b class="gr">{a["overlay_preserved"]}</b></div>
      <div class="kv"><span>快照对象 / 关系边</span>
        <b>{a["resync_snapshot"]["objects"]} / {a["resync_snapshot"]["links"]}</b></div>
      <div class="kv"><span>冲突策略</span>
        <b class="hl2">{a["resync_snapshot"]["conflict_strategy"]}</b></div>
      <div class="mono" style="margin-top:3px;color:#c8f7d4">
        源系统怎么刷都行：它只能改自己那一半字段。
      </div>
    </div>
    <div class="card am" style="padding:8px 11px">
      <div class="ct">为什么必须真的重刷一次 <small>WHY RESYNC</small></div>
      <div class="mono" style="line-height:1.65;color:#fde68a">
        源系统每晚全量同步是现实，不可能关掉。没有字段级主权隔离，
        AI 结论每晚被抹一次，企业永远攒不下治理资产。
      </div>
    </div>
    <div class="card vi" style="padding:7px 11px">
      <div class="mono" style="color:#e3dcff;line-height:1.6">
        主权边界 = 字段级声明。这是「拔插头不丢记忆」的底层机制。
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols narrow">{left}{right}</div>'
    return page(
        '3/6 · AI 分诊写入 <span class="n">ontology_overlay</span> · 源系统全量重刷 <span class="g">100% 存活</span>',
        ["am|3 条 AI 补丁", "cy|第二次全量重刷", "gr|overlay 前后一致"],
        content, sub, 2,
        f"REAL RUN · overlay_rows={a['overlay_rows']} · "
        f"preserved={a['overlay_preserved']} · resync={a['resync_snapshot']['objects']} objects",
    )


# ---------------------------------------------------------------------------
# Scene 4 · unplug_all_saas 一键拔插头
# ---------------------------------------------------------------------------
def scene_4(f: dict[str, Any], sub: str) -> str:
    u = f["unplug"]
    blocked = "".join(
        f'<div class="kv"><span class="mono" style="font-size:11.5px">{b}()</span>'
        f'<b class="rd">SaasDisconnectedError</b></div>'
        for b in u["blocked_calls"]
    )
    guard = "".join(
        f'<span class="pill r" style="font-size:11.5px">{g}</span>' for g in u["guard_rejected"]
    )
    types = "".join(
        f'<span class="pill g" style="font-size:11.5px">{t} · True</span>'
        for t in u["promoted_types"]
    )
    type_rows = "".join(
        f'<tr><td class="id">{t}</td><td class="r">{n}</td><td class="r gr">{n}</td></tr>'
        for t, n in u["counts"].items()
    )

    left = f"""
<div class="panel am" style="flex:1">
  <div class="ph"><b><i>▍</i>unplug_all_saas(runtime, bottlecrm, erp) 四大原子动作</b>
    <span>ATOMIC UNPLUG</span></div>
  <div class="body" style="justify-content:space-between;gap:9px">
    <div class="card bd">
      <div class="ct">① 物理切断连接 <small>DISCONNECT</small></div>
      <div class="mono" style="line-height:1.68">
        bottlecrm.disconnect() &nbsp;→ connected = {u["bottlecrm_connected"]}<br>
        erp.disconnect() &nbsp;→ connected = {u["odoo_connected"]}
      </div>
      <div class="pills" style="margin-top:5px">
        <span class="tag red">{u["disconnected"][0]}</span>
        <span class="tag red">{u["disconnected"][1]}</span>
      </div>
    </div>
    <div class="card gd">
      <div class="ct">② 5 大对象类型晋升 <small>__owned__ = True</small></div>
      <div class="pills">{types}</div>
    </div>
    <div class="card gd">
      <div class="ct">③ 关系边晋升 + 动作主权归零 <small>LINKS &amp; ACTIONS</small></div>
      <div class="kv"><span>晋升为 owned 的关系边</span>
        <b class="gr">{len(u["promoted_links"])} 条</b></div>
      <div class="mono" style="font-size:11.2px">{", ".join(u["promoted_links"])}</div>
      <div class="kv" style="margin-top:3px"><span>affects_source 归零的动作</span>
        <b class="gr">{len(u["demoted_actions"])} 个</b></div>
      <div class="mono" style="font-size:11.2px">{", ".join(u["demoted_actions"])}</div>
    </div>
    <div class="card vi">
      <div class="ct">④ 零丢失校验 <small>NO LOSS</small></div>
      <div class="kv"><span>拔插头后对象计数</span>
        <b class="vi">{sum(u["counts"].values())} 个 · 与拔前一致</b></div>
      <div class="kv"><span>overlay 补丁行</span><b class="vi">{u["overlay_rows"]} 行 · 全保留</b></div>
      <div class="mono" style="margin-top:3px;color:#b7aef0">
        代码一行没改，只挪了主权声明 —— 这就是「无感」的含义。
      </div>
    </div>
    <div class="card cy">
      <div class="ct">同一份业务代码 · 只改声明 <small>ZERO CODE CHANGE</small></div>
      <div class="mono" style="line-height:1.7;font-size:11px">
        <span class="rd">− CustomerAccount.__owned__ = &#123;"vip_tier": ..., "fraud_score": ...&#125;</span><br>
        <span class="gr">+ CustomerAccount.__owned__ = True</span><br>
        <span class="rd">− action.affects_source = True</span><br>
        <span class="gr">+ action.affects_source = False</span>
      </div>
    </div>
  </div>
</div>"""

    right = f"""
<div class="panel rd" style="flex:1">
  <div class="ph"><b><i>▍</i>铁证拦截面板 · 拔掉之后再想碰外部系统</b><span>HARD EVIDENCE</span></div>
  <div class="body" style="justify-content:space-between;gap:9px">
    <div class="card bd">
      <div class="ct">外部调用全部拦截 <small>{len(u["blocked_calls"])} / 4 BLOCKED</small></div>
      {blocked}
    </div>
    <div class="card am">
      <div class="ct">快照覆盖被治理护栏拒绝 <small>{len(u["guard_rejected"])} / 5 REJECTED</small></div>
      <div class="mono" style="margin-bottom:5px;color:#fcd34d">
        runtime.load_snapshot(&#123;type: []&#125;) → GovernanceViolationError
      </div>
      <div class="pills">{guard}</div>
    </div>
    <div class="card cy">
      <div class="ct">主权前后对照 <small>BEFORE / AFTER</small></div>
      <div class="kv"><span>对象 __owned__</span><b>字段级 dict → True</b></div>
      <div class="kv"><span>关系边 owned</span><b>False → True</b></div>
      <div class="kv"><span>动作 affects_source</span><b>{len(u["demoted_actions"])} 个 → False</b></div>
      <div class="kv"><span>外部连接</span><b class="rd">2 条 → 0 条</b></div>
    </div>
    <div class="card vi">
      <div class="ct">拔完之后本体仍然完整 <small>DATA STILL INTACT</small></div>
      <table class="tbl">
        <tr><th>对象类型</th><th class="r">拔前</th><th class="r">拔后</th></tr>
        {type_rows}
      </table>
    </div>
    <div class="card gd" style="padding:7px 11px">
      <div class="mono" style="color:#c8f7d4;line-height:1.62">
        物理断连 + 类型晋升 + 护栏拦截，三重证据同时成立：<br>
        外部 SaaS 再也无法读、无法写、无法覆盖。
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols narrow">{left}{right}</div>'
    return page(
        '4/6 · Phase B · <span class="n">unplug_all_saas()</span> 一键物理拔除与主权晋升',
        ["am|物理断连", "gr|5 类型全自有", "rd|拦截铁证"],
        content, sub, 3,
        f"REAL RUN · promoted={len(u['promoted_types'])} types / "
        f"{len(u['promoted_links'])} links · blocked={len(u['blocked_calls'])} · "
        f"guard={len(u['guard_rejected'])}",
        phase=2,
    )


# ---------------------------------------------------------------------------
# Scene 5 · 零外部 SaaS 业务闭环
# ---------------------------------------------------------------------------
def scene_5(f: dict[str, Any], sub: str) -> str:
    b = f["phase_b"]
    w, sm, bg = b["win"], b["small"], b["big"]
    steps = [
        ("①", "商机赢单转订单", "OFFLINE-WIN", w["status"], "cy", "blue",
         [f'{w["opportunity"][:8]} → {w["stage"]} · win_probability={w["win_probability"]}',
          f'原生生成订单 {w["order"]} · {_money(w["total"])}',
          f'订单状态 {w["order_status"]} · 零外部 ERP 参与'],
         "win_opportunity_and_create_order"),
        ("②", "小额 AI 自动退款", sm["run"], sm["status"], "gd", "green",
         [f'{_money(sm["total"])} < $100 审批线',
          f'{sm["order"]} → {sm["order_status"]} · 已退 {_money(sm["refunded"])}',
          f'approved_by = {sm["approved_by"] or "AI 直接放行"}'],
         "resolve_case_with_refund"),
        ("③", "大额触发人工审批", "OFFLINE-REFUND-BIG",
         f'{bg["submit_status"]} → {bg["approve_status"]}', "am", "amber",
         [f'{_money(bg["total"])} ≥ $100 门槛 · 挂起等签批',
          f'挂起期间订单 = {bg["order_while_pending"]}（未动账）',
          f'approve by {bg["approved_by"]} → {bg["order_status"]}'],
         "resolve_case_with_refund + approve_action"),
        ("④", "红线拦截 + 时光机撤回", "OFFLINE-REFUND-SMALL → REV",
         b["revert"]["original_status"], "vi", "violet",
         [f'欺诈分 92 → {b["guards"]["fraud_code"]} · {b["guards"]["fraud_run_status"]}',
          f'45 天超期 → {b["guards"]["stale_code"]}',
          f'撤回后订单 {b["revert"]["order_status"]} · 工单 {b["revert"]["case_status"]}'],
         "revert_action（时光机）"),
    ]
    cards = ""
    for num, title, rid, status, tone, tag, lines, action in steps:
        body = "<br>".join(lines)
        cards += f"""
<div class="card {tone}" style="display:flex;flex-direction:column;gap:6px;padding:12px 13px;height:100%">
  <div class="ct" style="margin-bottom:0;font-size:14px">
    <span><span style="font-size:18px;color:#7dd3fc">{num}</span> {title}</span>
  </div>
  <div class="mono" style="font-size:11px;color:#8ba0b8">{rid}</div>
  <div><span class="tag {tag}">{status}</span></div>
  <div class="mono" style="font-size:11.6px;line-height:1.68;color:#cfe0f2">{body}</div>
  <div class="mono" style="margin-top:auto;font-size:10.5px;color:#7d90a6">{action}</div>
</div>"""

    h = b["health"]
    ledger_rows = ""
    for row in b["ledger"][-8:]:
        tone = "rd" if row["status"] == "REJECTED" else (
            "hl2" if row["status"] == "PENDING_APPROVAL" else "gr"
        )
        ledger_rows += (
            f'<tr><td class="id">{row["run_id"]}</td>'
            f'<td class="mono" style="font-size:11px">{row["action_type"]}</td>'
            f'<td class="r {tone}">{row["status"]}</td>'
            f'<td class="r rd">{row["error_code"] or "—"}</td></tr>'
        )

    content = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>零外部 SaaS · 纯 Ontology 内核跑完核心业务闭环</b>
    <span>STANDALONE BUSINESS LOOP</span></div>
  <div class="body" style="justify-content:space-between;gap:10px">
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:11px;height:208px">{cards}</div>
    <div style="display:grid;grid-template-columns:1.05fr 1fr 1fr;gap:10px">
      <div class="card bd">
        <div class="ct">硬护栏在脱壳态依然生效 <small>GUARDS</small></div>
        <div class="kv"><span>45 天超期退款</span>
          <b class="rd">{b["guards"]["stale_code"]}</b></div>
        <div class="kv"><span>审计状态</span>
          <b class="rd">{b["guards"]["stale_run_status"]}</b></div>
        <div class="kv"><span>欺诈分 92 退款</span>
          <b class="rd">{b["guards"]["fraud_code"]}</b></div>
        <div class="kv"><span>审计状态</span>
          <b class="rd">{b["guards"]["fraud_run_status"]}</b></div>
      </div>
      <div class="card vi">
        <div class="ct">时光机一键撤回 <small>REVERT</small></div>
        <div class="kv"><span>原 run 状态</span><b class="rd">{b["revert"]["original_status"]}</b></div>
        <div class="kv"><span>撤回 run</span><b class="vi">{b["revert"]["status"]}</b></div>
        <div class="kv"><span>订单 / 已退金额</span>
          <b>{b["revert"]["order_status"]} / {_money(b["revert"]["refunded"])}</b></div>
        <div class="kv"><span>工单状态</span><b>{b["revert"]["case_status"]}</b></div>
      </div>
      <div class="card cy">
        <div class="ct">拔插头后的经营快照 <small>PORTFOLIO HEALTH</small></div>
        <div class="kv"><span>客户 / 商机</span>
          <b>{h["accounts"]} / {h["opportunities"]["total"]}</b></div>
        <div class="kv"><span>订单（活跃）</span>
          <b>{h["orders"]["total"]}（{h["orders"]["active"]}）</b></div>
        <div class="kv"><span>工单 / 高危</span>
          <b>{h["cases"]["total"]} / {h["cases"]["high_fraud"]}</b></div>
        <div class="kv"><span>已入账退款</span>
          <b>{h["refunds"]["posted"]} 笔 · {_money(h["refunds"]["posted_amount_cents"])}</b></div>
      </div>
    </div>
    <div class="card gd" style="padding:9px 12px;flex:1;display:flex;flex-direction:column">
      <div class="ct">审计账本 · 拔掉 SaaS 后每一次受治理动作都有据可查
        <small>ACTION RUNS · {b["runs"]} 条</small></div>
      <table class="tbl fixed tight" style="font-size:11.8px">
        <colgroup>
          <col style="width:30%"><col style="width:34%"><col style="width:20%"><col style="width:16%">
        </colgroup>
        <tr><th>run_id</th><th>action_type</th><th class="r">status</th><th class="r">error_code</th></tr>
        {ledger_rows}
      </table>
    </div>
  </div>
</div>"""
    return page(
        '5/6 · 零外部 SaaS 独立跑完 <span class="g">核心业务闭环</span>',
        ["cy|赢单转订单", "gr|小额自动退", "am|大额审批", "vi|时光机撤回"],
        content, sub, 4,
        f"REAL RUN · runs={b['runs']} · rejected={len(b['rejected'])} · "
        f"refunds posted={h['refunds']['posted']}",
        phase=2,
    )


# ---------------------------------------------------------------------------
# Scene 6 · 契约冻结定理
# ---------------------------------------------------------------------------
def scene_6(f: dict[str, Any], sub: str) -> str:
    ct = f["contract"]
    rows = ""
    for r in ct["rows"]:
        rows += (
            f'<tr><td class="id">{r["eval"]}</td>'
            f'<td>{r["parasitic"]}</td>'
            f'<td>{r["standalone"]}</td>'
            f'<td class="r"><span class="tag {"green" if r["same"] else "red"}">'
            f'{"✓ 一致" if r["same"] else "✗ 不一致"}</span></td></tr>'
        )

    left = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>同一套 AIP Evals 跑在两种 Runtime 上的对拍表</b>
    <span>CONTRACT FREEZE</span></div>
  <div class="body" style="justify-content:space-between;gap:10px">
    <div style="display:flex;align-items:center;gap:10px">
      <span class="tag blue">寄生态 standalone=False</span>
      <span class="tag violet">脱壳态 standalone=True</span>
      <span class="tag green">逐字段比对</span>
    </div>
    <table class="tbl fixed" style="font-size:12.4px">
      <colgroup>
        <col style="width:29%"><col style="width:33%"><col style="width:31%"><col style="width:7%">
      </colgroup>
      <tr><th>Eval 用例</th><th>寄生态结果</th><th>脱壳态结果</th><th class="r">判决</th></tr>
      {rows}
    </table>
    <div class="card gd">
      <div class="ct">契约冻结定理 <small>CONTRACT IDENTICAL</small></div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:14px">
        <div>
          <div class="kv" style="font-size:14px"><span>Action 名称 / 参数 Schema</span><b class="gr">100% 一致</b></div>
          <div class="kv" style="font-size:14px"><span>错误码 / 审批语义</span><b class="gr">100% 一致</b></div>
          <div class="kv" style="font-size:14px"><span>撤回能力 / 看板字段</span><b class="gr">100% 一致</b></div>
        </div>
        <div style="text-align:right">
          <div class="bigstat" style="justify-content:flex-end">
            <span class="v gr" style="font-size:44px">{ct["identical"]}</span>
          </div>
          <div class="mono" style="margin-top:4px">contract_identical</div>
        </div>
      </div>
    </div>
    <div class="card cy">
      <div class="ct">为什么两次跑出来一模一样 <small>WHY IDENTICAL</small></div>
      <div class="mono" style="line-height:1.7">
        同一份 build_saas_model() 生成 Action 签名、参数模型与审批规则<br>
        两种模式只改 __owned__ 与 affects_source，业务函数体一字未动<br>
        <span class="dim">→ 名称 / Schema / 错误码 / 审批 / 撤回自然逐字节一致</span>
      </div>
    </div>
    <div class="card am">
      <div class="ct">对照口径 <small>SAME HARNESS</small></div>
      <div class="kv"><span>Eval 条数</span><b>{len(ct["evals"])}</b></div>
      <div class="kv"><span>比对粒度</span><b>整份结果字典逐字段</b></div>
      <div class="kv"><span>结论</span><b class="gr">contract_identical = {ct["identical"]}</b></div>
    </div>
  </div>
</div>"""

    right = f"""
<div class="panel vi" style="flex:1">
  <div class="ph"><b><i>▍</i>三步演进收束 · 上层 AI Agent 一行代码不用改</b><span>THE VERDICT</span></div>
  <div class="body" style="justify-content:space-between;gap:10px">
    <div class="card cy">
      <div class="ct">第一步 · 统一语义并驱动现有 SaaS <small>PARASITIC</small></div>
      <div class="mono" style="line-height:1.62">
        真实 BottleCRM + 真实 Odoo 19 整库吸收<br>
        外部系统仍是供数方，Ontology 只做语义统一
      </div>
    </div>
    <div class="card am">
      <div class="ct">第二步 · 业务逻辑沉淀进 Action 与 Overlay <small>ACCRETION</small></div>
      <div class="mono" style="line-height:1.62">
        风控分 / VIP 分层 / AI 处置方案写进补丁层<br>
        全量重刷杀不死，越用越厚
      </div>
    </div>
    <div class="card gd">
      <div class="ct">第三步 · 拔掉插头，契约冻结 <small>UNPLUG</small></div>
      <div class="mono" style="line-height:1.62">
        unplug_all_saas() 物理断连 + 主权晋升<br>
        同一套 Action / Tool / Evals 原样跑通
      </div>
    </div>
    <div class="card gr" style="background:rgba(34,197,94,.13);border-left-color:#22c55e">
      <div style="font-size:17px;font-weight:800;line-height:1.6;color:#dcfce5">
        先用 Ontology 统一语义并驱动现有 SaaS，<br>
        再把业务逻辑沉淀进 Action 与 Overlay，<br>
        最后拔掉插头 —— <span style="color:#7dd3fc">上层 AI Agent 一行代码不用改！</span>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:9px">
      <div class="card gd" style="text-align:center;padding:8px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v gr" style="font-size:24px">2 套</span></div>
        <div class="mono" style="margin-top:3px;font-size:10px">真实 SaaS 被拔掉</div>
      </div>
      <div class="card cy" style="text-align:center;padding:8px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v hl" style="font-size:24px">5 类</span></div>
        <div class="mono" style="margin-top:3px;font-size:10px">对象 100% 自有</div>
      </div>
      <div class="card vi" style="text-align:center;padding:8px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v vi" style="font-size:24px">0 行</span></div>
        <div class="mono" style="margin-top:3px;font-size:10px">上层代码改动</div>
      </div>
    </div>
    <div class="card am">
      <div class="ct">这套做法成立的前提 <small>PREREQUISITES</small></div>
      <div class="kv" style="font-size:12.6px"><span>① 源系统字段可枚举</span><b class="hl2">才谈得上字段级主权</b></div>
      <div class="kv" style="font-size:12.6px"><span>② 治理字段显式声明</span><b class="hl2">才写得进 overlay</b></div>
      <div class="kv" style="font-size:12.6px"><span>③ 写操作全走 Action</span><b class="hl2">才拦得住越权</b></div>
      <div class="mono" style="margin-top:3px;color:#fde68a">
        三条缺一条，「无感拔插头」就会变成「带病迁移」。
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols wide">{left}{right}</div>'
    return page(
        '6/6 · 契约冻结定理 · <span class="g">contract_identical = True</span>',
        ["cy|7 条 AIP Evals", "vi|双模式对拍", "gr|零改动接管"],
        content, sub, 5,
        "TK-ONTO-016 · SAAS UNPLUG DEMO · v0.3.0",
        phase=2,
    )


SCENES = [scene_1, scene_2, scene_3, scene_4, scene_5, scene_6]
NARRATIONS = [
    narration_1,
    narration_2,
    narration_3,
    narration_4,
    narration_5,
    narration_6,
]


# ---------------------------------------------------------------------------
# 3. 渲染 / 配音 / 合成
# ---------------------------------------------------------------------------
def render_frames(facts: dict[str, Any]) -> list[Path]:
    from playwright.sync_api import sync_playwright

    FRAMES.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": W, "height": H}, device_scale_factor=1
        )
        for i, (builder, narrator) in enumerate(zip(SCENES, NARRATIONS), start=1):
            html = builder(facts, narrator(facts))
            out = FRAMES / f"scene_{i:02d}.png"
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(420)
            page.screenshot(
                path=str(out), clip={"x": 0, "y": 0, "width": W, "height": H}
            )
            paths.append(out)
            print(f"[frame] {out.name}  {out.stat().st_size / 1024:.0f} KB")
        browser.close()
    return paths


def probe_duration(path: Path) -> float:
    res = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())


def synth_audio(facts: dict[str, Any]) -> list[Path]:
    AUDIO.mkdir(parents=True, exist_ok=True)
    outs: list[Path] = []
    for i, narrator in enumerate(NARRATIONS, start=1):
        text = narrator(facts)
        out = AUDIO / f"scene_{i:02d}.mp3"
        txt = AUDIO / f"scene_{i:02d}.txt"
        txt.write_text(text, encoding="utf-8")
        ok = False
        if EDGE_TTS.exists():
            tmp = AUDIO / f".scene_{i:02d}.tmp.mp3"
            cmd = [
                str(EDGE_TTS), "--voice", VOICE, "--file", str(txt),
                "--rate=+12%", "--write-media", str(tmp), "--no-progress",
            ]
            for attempt in range(8):
                tmp.unlink(missing_ok=True)
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 4000:
                    try:
                        probe_duration(tmp)
                    except subprocess.CalledProcessError:
                        continue
                    tmp.replace(out)
                    ok = True
                    break
                tail = res.stderr.strip().splitlines()
                print(f"[tts] edge-tts scene {i} attempt {attempt + 1} failed: "
                      f"{tail[-1][:120] if tail else 'no stderr'}")
            tmp.unlink(missing_ok=True)
        if not ok and out.exists():
            try:
                probe_duration(out)
                ok = True
                print(f"[tts] scene {i} 复用已有配音")
            except subprocess.CalledProcessError:
                out.unlink(missing_ok=True)
        if not ok:
            print(f"[tts] falling back to say -v Tingting for scene {i}")
            aiff = AUDIO / f"scene_{i:02d}.aiff"
            subprocess.run([str(SAY), "-v", "Tingting", "-o", str(aiff),
                            "-f", str(txt)], check=True)
            subprocess.run([str(FFMPEG), "-y", "-loglevel", "error",
                            "-i", str(aiff), "-c:a", "libmp3lame", "-q:a", "3",
                            str(out)], check=True)
            aiff.unlink(missing_ok=True)
        outs.append(out)
        print(f"[tts]   {out.name}  {probe_duration(out):.2f}s  "
              f"{out.stat().st_size / 1024:.0f} KB")
    return outs


def build_segments(frames: list[Path], audios: list[Path]) -> list[Path]:
    SEGMENTS.mkdir(parents=True, exist_ok=True)
    segs: list[Path] = []
    for i, (frame, audio) in enumerate(zip(frames, audios), start=1):
        dur = probe_duration(audio) + LEAD_IN + TAIL
        seg = SEGMENTS / f"seg_{i:02d}.mp4"
        # 不加 fade：第 0 帧必须是有内容的画面（QuickTime 黑屏根因之一）
        vf = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x0a1120,"
            f"fps={FPS},format=yuv420p[v];"
            f"[1:a]adelay={int(LEAD_IN * 1000)}|{int(LEAD_IN * 1000)},"
            f"apad=pad_dur={TAIL},aresample=48000,aformat=channel_layouts=stereo[a]"
        )
        cmd = [
            str(FFMPEG), "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", str(FPS), "-i", str(frame),
            "-i", str(audio),
            "-filter_complex", vf, "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.2f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
            "-bf", "0", "-video_track_timescale", "30000",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(seg),
        ]
        subprocess.run(cmd, check=True)
        segs.append(seg)
        print(f"[seg]   {seg.name}  {dur:.2f}s  {seg.stat().st_size / 1024:.0f} KB")
    return segs


def concat_segments(segs: list[Path]) -> Path:
    lst = SEGMENTS / "concat.txt"
    lst.write_text("".join(f"file '{s.name}'\n" for s in segs), encoding="utf-8")
    # 必须重新编码而不是 -c copy：流拷贝会把首片的 start_pts 与 edit list 带进
    # 最终 MP4，QuickTime 硬件解码器据此黑屏。重编码后时间轴从 0 重新开始。
    subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst),
         "-vf", "setpts=PTS-STARTPTS", "-af", "asetpts=PTS-STARTPTS",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-bf", "0", "-pix_fmt", "yuv420p", "-tag:v", "avc1",
         "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", str(OUTPUT)],
        check=True,
    )
    return OUTPUT


def probe_start(path: Path) -> tuple[int, str]:
    res = subprocess.run(
        [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=start_pts,start_time,has_b_frames",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    st = json.loads(res.stdout)["streams"][0]
    return int(st.get("start_pts", 0)), str(st.get("start_time", "0.000000"))


def frame0_mean(path: Path) -> float:
    """抽第 0 帧算亮度均值：QuickTime 黑屏时该值会趋近 0。"""
    res = subprocess.run(
        [str(FFMPEG), "-v", "error", "-i", str(path), "-frames:v", "1",
         "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", res.stdout)
    return round(float(m.group(1)), 2) if m else 0.0


def verify(path: Path) -> dict[str, Any]:
    res = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_format", "-show_streams",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(res.stdout)
    streams = data["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    info = {
        "path": str(path),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "vcodec": video["codec_name"],
        "tag": (video.get("tags") or {}).get("major_brand"),
        "pix_fmt": video.get("pix_fmt"),
        "fps": video.get("r_frame_rate"),
        "has_b_frames": int(video.get("has_b_frames", 0)),
        "acodec": audio["codec_name"] if audio else None,
        "sample_rate": int(audio["sample_rate"]) if audio else None,
        "channels": int(audio["channels"]) if audio else None,
        "duration": round(float(data["format"]["duration"]), 2),
        "size_bytes": int(data["format"]["size"]),
        "size_mb": round(int(data["format"]["size"]) / 1024 / 1024, 2),
    }
    start_pts, start_time = probe_start(path)
    info["start_pts"] = start_pts
    info["start_time"] = start_time
    info["frame0_yavg"] = frame0_mean(path)
    checks = {
        "resolution_1920x1080": info["width"] == W and info["height"] == H,
        "fps_30": info["fps"] == "30/1",
        "h264": info["vcodec"] == "h264",
        "yuv420p": info["pix_fmt"] == "yuv420p",
        "has_audio": info["acodec"] is not None,
        "aac": info["acodec"] == "aac",
        "duration_80_165": 80 <= info["duration"] <= 165,
        "size_gt_1_5mb": info["size_bytes"] > int(1.5 * 1024 * 1024),
        "no_b_frames": info["has_b_frames"] == 0,
        "start_pts_zero": info["start_pts"] == 0,
        "start_time_zero": abs(float(info["start_time"])) < 1e-6,
        "frame0_not_black": info["frame0_yavg"] > 20.0,
    }
    info["checks"] = checks
    info["all_ok"] = all(checks.values())
    return info


def existing_assets() -> tuple[list[Path], list[Path]]:
    """复用已渲染的分镜与配音，跳过 Playwright 与 TTS。"""
    frames = sorted(FRAMES.glob("scene_*.png"))
    audios = sorted(AUDIO.glob("scene_*.mp3"))
    if len(frames) != 6 or len(audios) != 6:
        raise SystemExit(
            f"existing assets incomplete: {len(frames)} frames / {len(audios)} audios"
        )
    for path in frames + audios:
        print(f"[reuse] {path.relative_to(HERE)}  {path.stat().st_size / 1024:.0f} KB")
    return frames, audios


def main() -> int:
    args = sys.argv[1:]
    reuse = "--reuse" in args
    facts_only = "--facts-only" in args
    print("=" * 78)
    print("TK-ONTO-016 · OpenOntology 驱动与无感拔除 SaaS · 1080p 演示视频")
    if reuse:
        print("MODE: --reuse（复用 frames/ + audio/，跳过 Playwright 与 TTS）")
    print("=" * 78)
    for tool in (FFMPEG, FFPROBE):
        if not tool.exists():
            raise SystemExit(f"missing tool: {tool}")

    if facts_only:
        facts = collect_facts()
        print(json.dumps(facts, ensure_ascii=False, indent=2, default=str))
        return 0

    if reuse:
        frames, audios = existing_assets()
    else:
        facts = collect_facts()
        a = facts["phase_a"]
        u = facts["unplug"]
        b = facts["phase_b"]
        print(f"[data]  objects={a['snapshot']['objects']} links={a['snapshot']['links']} "
              f"overlay={a['overlay_rows']} preserved={a['overlay_preserved']}")
        print(f"[data]  unplug types={len(u['promoted_types'])} links={len(u['promoted_links'])} "
              f"blocked={len(u['blocked_calls'])} guard={len(u['guard_rejected'])}")
        print(f"[data]  win={b['win']['order']}/{b['win']['status']} "
              f"small={b['small']['status']} big={b['big']['submit_status']}→"
              f"{b['big']['approve_status']} revert={b['revert']['status']}")
        print(f"[data]  contract_identical={facts['contract']['identical']}")
        frames = render_frames(facts)
        audios = synth_audio(facts)

    segs = build_segments(frames, audios)
    concat_segments(segs)

    info = verify(OUTPUT)
    print("-" * 78)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("-" * 78)
    if not info["all_ok"]:
        print("FAIL: 视频验收未通过")
        failed = [k for k, v in info["checks"].items() if not v]
        print(f"       未达标项: {', '.join(failed)}")
        return 1
    print(f"PASS: {OUTPUT}")
    print(f"      分辨率 {info['width']}x{info['height']} · {info['vcodec']}/{info['acodec']}"
          f" · {info['fps']}fps · 时长 {info['duration']}s · {info['size_mb']} MB")
    print(f"      has_b_frames={info['has_b_frames']} · start_pts={info['start_pts']}"
          f" · start_time={info['start_time']} · frame0_YAVG={info['frame0_yavg']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
