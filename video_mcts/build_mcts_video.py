#!/usr/bin/env python3
"""TK-ONTO-013 · 《AlphaGo MCTS 在符号世界模型中的多步沙盘推演》高清演示视频。

严格并发隔离：本脚本只在 ``video_mcts/`` 目录内读写文件；对
``examples/``、``open_ontology/`` 只做只读导入，不修改任何文件。

流程：
1. 真实运行 ``examples.mcts_world_model_demo``：单步贪心翻车、80 次 MCTS
   树搜索、根节点 P/N/Q 分布、符号裁判剪枝计数、主库零污染指纹、
   最优三步序列正式提交与时光机悔棋。
2. Playwright + Chromium 1920x1080 渲染 6 幕深色科技风画面（内联 SVG
   搜索树拓扑图）。
3. edge-tts（zh-CN-YunxiNeural）生成 6 段中文配音，失败回退 say。
4. ffmpeg 合成 1920x1080 H.264 + AAC yuv420p 30fps MP4。
5. ffprobe 校验分辨率 / 帧率 / 音轨 / 时长 / 文件大小。
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
OUTPUT = HERE / "mcts_world_model_demo.mp4"

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
# 1. 真实数据采集（只读导入 + 内存沙盘）
# ---------------------------------------------------------------------------
def _money(value: float) -> str:
    """带符号的千分位金额。"""
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{abs(value):,.0f}"


def _walk(node: Any, depth: int = 0) -> dict[str, Any]:
    return {
        "label": node.label,
        "depth": depth,
        "visits": node.visits,
        "q": round(node.mean_value, 2),
        "prior": round(node.prior, 4),
        "illegal": [item["action"] for item in node.illegal_actions],
        "dead": node.dead_end,
        "terminal": node.terminal,
        "children": [
            _walk(child, depth + 1)
            for child in sorted(
                node.children.values(), key=lambda c: (-c.visits, c.label)
            )
        ],
    }


def collect_facts() -> dict[str, Any]:
    from examples import mcts_world_model_demo as m

    demo = m.build_demo()
    runtime = demo.runtime
    f: dict[str, Any] = {}

    f["orders"] = [
        {
            "id": o.order_id,
            "cargo": o.cargo,
            "value": o.base_value,
            "eta": o.max_eta_hours,
            "cold": o.min_cold_level,
            "penalty": o.penalty,
        }
        for o in sorted(
            runtime.query_objects(m.EmergencyOrder, m.DISPATCHER),
            key=lambda o: o.order_id,
        )
    ]
    f["trucks"] = [
        {
            "id": t.truck_id,
            "name": t.name,
            "eta": t.eta_hours,
            "cold": t.cold_level,
            "cost": t.freight_cost,
        }
        for t in sorted(
            runtime.query_objects(m.ColdTruck, m.DISPATCHER),
            key=lambda t: t.freight_cost,
        )
    ]

    fingerprint_before = m.state_fingerprint(runtime)
    f["runs_before"] = fingerprint_before["runs"]
    f["links_before"] = fingerprint_before["links"]

    # -- 方法 A：单步贪心 --------------------------------------------------
    greedy_fork = m.fork_runtime(runtime)
    try:
        steps, failed_at = m.run_greedy_policy(
            greedy_fork, m.DISPATCHER, demo.order_sequence
        )
        greedy_value = greedy_fork.call_function(
            "evaluate_world_value", {"label": "greedy_dead_end"}, m.DISPATCHER
        )
    finally:
        greedy_fork.close()

    f["greedy"] = {
        "steps": [
            {
                "order": s.order_id,
                "truck": s.truck_id,
                "ok": s.ok,
                "code": s.error_code,
                "message": s.error_message,
            }
            for s in steps
        ],
        "failed_at": failed_at,
        "value": greedy_value["total_value"],
        "fulfilled": greedy_value["fulfilled"],
        "revenue": greedy_value["revenue"],
        "freight": greedy_value["freight_cost"],
        "penalty": greedy_value["penalty"],
        "error_code": steps[-1].error_code,
        "error_message": steps[-1].error_message,
    }

    # -- 方法 B：AlphaGo MCTS（80 次模拟） --------------------------------
    with m.OntologyMCTSPlanner(
        runtime,
        m.DISPATCHER,
        order_sequence=demo.order_sequence,
        simulations=80,
        c_puct=1.414,
        seed=17,
    ) as planner:
        report = planner.search()

    fingerprint_after = m.state_fingerprint(runtime)
    f["main_writes_during_search"] = (
        0
        if (
            fingerprint_after["runs"] == fingerprint_before["runs"]
            and fingerprint_after["links"] == fingerprint_before["links"]
            and fingerprint_after["versions"] == fingerprint_before["versions"]
        )
        else -1
    )

    root_actions = {
        stat.label: {
            "label": stat.label,
            "prior": round(stat.prior, 4),
            "visits": stat.visits,
            "q": stat.mean_value,
        }
        for stat in report.root_actions
    }
    tree = _walk(report.root)
    prune_by_depth: dict[int, int] = {}

    def _agg(node: dict[str, Any]) -> None:
        if node["illegal"]:
            level = node["depth"] + 1
            prune_by_depth[level] = prune_by_depth.get(level, 0) + len(node["illegal"])
        for child in node["children"]:
            _agg(child)

    _agg(tree)

    f["mcts"] = {
        "simulations": report.simulations,
        "root_visits": report.root_visits,
        "pruned": report.pruned_illegal_nodes,
        "forks": report.forks_created,
        "sandbox_writes": report.sandbox_writes,
        "explored": report.explored_nodes,
        "dead_ends": report.dead_end_nodes,
        "root_actions": root_actions,
        "best_plan": report.best_plan_labels,
        "best_value": report.best_value,
        "tree": tree,
        "prune_by_depth": dict(sorted(prune_by_depth.items())),
        "c_puct": 1.414,
    }
    f["mcts"]["best_steps"] = [
        {
            "order": cand.order_id,
            "truck": cand.truck_id,
            "prior": round(cand.prior, 4),
        }
        for cand in report.best_plan
    ]

    # -- 正式提交 + 时光机悔棋 --------------------------------------------
    committed: list[dict[str, Any]] = []
    for index, cand in enumerate(report.best_plan, start=1):
        run_id = f"MCTS-PLAN-{index}"
        run = runtime.submit_action(
            run_id, "dispatch_order", cand.params, m.DISPATCHER
        )
        committed.append(
            {"run_id": run["run_id"], "status": run["status"]}
        )
    final_value = runtime.call_function(
        "evaluate_world_value", {"label": "committed"}, m.DISPATCHER
    )

    revert = runtime.revert_action("MCTS-PLAN-3", "MCTS-PLAN-3-REV", m.DISPATCHER)
    order3_after_revert = runtime.get_object(m.EmergencyOrder, "ORD-3", m.DISPATCHER)
    redo = runtime.submit_action(
        "MCTS-PLAN-3-REDO",
        "dispatch_order",
        {"order_id": "ORD-3", "truck_id": "TRK-MID"},
        m.DISPATCHER,
    )
    order3_after_redo = runtime.get_object(m.EmergencyOrder, "ORD-3", m.DISPATCHER)

    f["commit"] = {
        "runs": committed,
        "final_value": final_value["total_value"],
        "fulfilled": final_value["fulfilled"],
        "freight": final_value["freight_cost"],
        "penalty": final_value["penalty"],
        "revert_run": revert["run_id"],
        "revert_status": revert["status"],
        "order3_after_revert": order3_after_revert.status,
        "redo_status": redo["status"],
        "order3_after_redo": order3_after_redo.status,
        "total_runs": len(runtime.list_action_runs()),
    }
    f["delta"] = round(
        final_value["total_value"] - f["greedy"]["value"], 2
    )
    f["expected_value"] = m.EXPECTED_OPTIMAL_VALUE
    runtime.close()
    return f


# ---------------------------------------------------------------------------
# 2. 视觉设计系统
# ---------------------------------------------------------------------------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1920px;height:1080px;overflow:hidden}
body{background:#04080f;color:#e9f1fb;
 font-family:"PingFang SC","Hiragino Sans GB","Heiti SC","STHeiti",sans-serif;
 -webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
.stage{position:relative;width:1920px;height:1080px;padding:30px 44px 24px;
 display:flex;flex-direction:column;gap:16px}
.stage::before{content:"";position:absolute;inset:0;z-index:0;background:
 radial-gradient(1250px 640px at 6% -14%,rgba(56,189,248,.2),transparent 62%),
 radial-gradient(1000px 560px at 98% -10%,rgba(129,140,248,.18),transparent 60%),
 radial-gradient(1150px 720px at 52% 122%,rgba(34,197,94,.075),transparent 64%),
 linear-gradient(#04080f,#02050a)}
.stage::after{content:"";position:absolute;inset:0;z-index:0;background-image:
 linear-gradient(rgba(120,170,230,.05) 1px,transparent 1px),
 linear-gradient(90deg,rgba(120,170,230,.05) 1px,transparent 1px);
 background-size:60px 60px;
 -webkit-mask-image:radial-gradient(circle at 50% 42%,#000 26%,transparent 78%);
 mask-image:radial-gradient(circle at 50% 42%,#000 26%,transparent 78%)}
.stage>*{position:relative;z-index:1}

header{height:88px;display:flex;align-items:center;justify-content:space-between;gap:22px}
.brand{display:flex;align-items:center;gap:12px;flex:0 0 auto}
.mark{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;font-size:23px;
 font-weight:800;color:#03121d;background:linear-gradient(135deg,#38bdf8,#6366f1);
 box-shadow:0 10px 26px rgba(56,189,248,.34)}
.brand .nm{font-size:21px;font-weight:700;letter-spacing:.7px}
.brand .sb{font-size:11px;color:#8fa3bb;letter-spacing:2.1px;margin-top:3px}
h1{font-size:33px;font-weight:800;letter-spacing:.8px;text-align:center;flex:1 1 auto;
 line-height:1.22}
h1 .n{color:#38bdf8}
.badges{display:flex;gap:8px;flex:0 0 auto}
.chip{font-size:12.5px;padding:7px 12px;border-radius:999px;border:1px solid rgba(120,160,210,.26);
 color:#b9cbe0;background:rgba(255,255,255,.03);letter-spacing:.6px;white-space:nowrap}
.chip.ok{border-color:rgba(34,197,94,.5);color:#86efac;background:rgba(34,197,94,.1)}
.chip.cy{border-color:rgba(56,189,248,.5);color:#a5e4ff;background:rgba(56,189,248,.1)}
.chip.am{border-color:rgba(245,165,36,.5);color:#fcd34d;background:rgba(245,165,36,.1)}
.chip.vi{border-color:rgba(167,139,250,.5);color:#ddd6fe;background:rgba(167,139,250,.1)}

.content{flex:1;min-height:0;display:flex;flex-direction:column;gap:14px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px;flex:1;min-height:0}
.cols.wide{grid-template-columns:1.42fr 1fr}
.cols.narrow{grid-template-columns:1.06fr 1fr}
.panel{position:relative;border-radius:17px;padding:14px 17px 12px;min-height:0;overflow:hidden;
 background:linear-gradient(165deg,rgba(18,31,49,.94),rgba(7,14,24,.96));
 border:1px solid rgba(120,160,210,.17);display:flex;flex-direction:column;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 16px 40px rgba(0,0,0,.42)}
.panel::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;
 background:linear-gradient(90deg,#38bdf8,rgba(99,102,241,.5),transparent)}
.panel.am::before{background:linear-gradient(90deg,#f5a524,rgba(239,68,68,.45),transparent)}
.panel.rd::before{background:linear-gradient(90deg,#ef4444,rgba(245,165,36,.4),transparent)}
.panel.vi::before{background:linear-gradient(90deg,#a78bfa,rgba(56,189,248,.45),transparent)}
.panel.gr::before{background:linear-gradient(90deg,#22c55e,rgba(56,189,248,.45),transparent)}
.ph{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
 margin-bottom:8px;padding-bottom:7px;border-bottom:1px dashed rgba(120,160,210,.2)}
.ph b{font-size:16.5px;letter-spacing:.7px;font-weight:700}
.ph b i{color:#38bdf8;font-style:normal}
.panel.am .ph b i{color:#f5a524}
.panel.rd .ph b i{color:#f87171}
.panel.vi .ph b i{color:#a78bfa}
.panel.gr .ph b i{color:#4ade80}
.ph span{font-size:11px;color:#7f93ab;letter-spacing:1px;white-space:nowrap}
.body{flex:1;min-height:0;display:flex;flex-direction:column;gap:9px;justify-content:space-evenly}

.card{background:rgba(255,255,255,.028);border:1px solid rgba(120,160,210,.15);
 border-left:3px solid rgba(120,160,210,.42);border-radius:10px;padding:8px 12px}
.card.cy{border-left-color:#38bdf8;background:rgba(56,189,248,.055)}
.card.gd{border-left-color:#22c55e;background:rgba(34,197,94,.055)}
.card.bd{border-left-color:#ef4444;background:rgba(239,68,68,.065)}
.card.am{border-left-color:#f5a524;background:rgba(245,165,36,.06)}
.card.vi{border-left-color:#a78bfa;background:rgba(167,139,250,.06)}
.ct{font-size:13.5px;font-weight:700;letter-spacing:.5px;display:flex;align-items:center;
 justify-content:space-between;gap:9px;margin-bottom:4px}
.ct small{font-weight:500;font-size:11px;color:#8fa3bb;letter-spacing:.4px}
.mono{font-family:"SF Mono",Menlo,monospace;font-size:11.8px;color:#9fb4cc;letter-spacing:.1px}
.dim{color:#8fa3bb}
.hl{color:#38bdf8}.hl2{color:#fcd34d}.gr{color:#86efac}.rd{color:#fca5a5}.vi{color:#ddd6fe}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13.5px;padding:2.5px 0}
.kv span:first-child{color:#8fa3bb}
.kv b{font-variant-numeric:tabular-nums;text-align:right}
.tag{font-size:10.8px;padding:2px 8px;border-radius:6px;letter-spacing:.4px;font-weight:700;
 display:inline-block;white-space:nowrap}
.tag.red{color:#fecaca;background:rgba(239,68,68,.2);border:1px solid rgba(239,68,68,.45)}
.tag.green{color:#bbf7d0;background:rgba(34,197,94,.18);border:1px solid rgba(34,197,94,.42)}
.tag.amber{color:#fde68a;background:rgba(245,165,36,.18);border:1px solid rgba(245,165,36,.42)}
.tag.blue{color:#bae6fd;background:rgba(56,189,248,.16);border:1px solid rgba(56,189,248,.4)}
.tag.violet{color:#ddd6fe;background:rgba(167,139,250,.18);border:1px solid rgba(167,139,250,.42)}
.bigstat{display:flex;align-items:baseline;gap:8px}
.bigstat .v{font-size:38px;font-weight:800;line-height:1;letter-spacing:-1px}
.bigstat .u{font-size:13px;color:#8fa3bb;letter-spacing:.8px}
.pills{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-size:13.5px;font-weight:700;padding:7px 13px;border-radius:9px;letter-spacing:.4px;
 border:1px solid rgba(56,189,248,.4);background:rgba(56,189,248,.1);color:#cfeeff}
.pill.o{border-color:rgba(167,139,250,.42);background:rgba(167,139,250,.1);color:#e2dcff}
.pill.g{border-color:rgba(34,197,94,.42);background:rgba(34,197,94,.1);color:#d3f9dd}
.plus{color:#5f7a99;font-size:16px;font-weight:700}

table.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{font-size:10.8px;color:#7f93ab;font-weight:600;text-align:left;padding:5px 7px;
 border-bottom:1px solid rgba(120,160,210,.24);letter-spacing:.5px;white-space:nowrap}
.tbl td{padding:7.5px 7px;border-bottom:1px solid rgba(120,160,210,.09);
 font-variant-numeric:tabular-nums;white-space:nowrap;font-size:13.5px}
.tbl tr:last-child td{border-bottom:none}
.tbl td.r,.tbl th.r{text-align:right}
.tbl .id{font-family:"SF Mono",Menlo,monospace;font-size:12px;color:#a5e4ff}

footer{height:180px;display:flex;flex-direction:column;justify-content:center;
 padding:12px 32px;border-radius:17px;border:1px solid rgba(120,160,210,.2);
 border-left:6px solid #38bdf8;
 background:linear-gradient(90deg,rgba(7,14,24,.97),rgba(12,23,37,.93));
 box-shadow:0 14px 40px rgba(0,0,0,.5)}
footer p{font-weight:600;letter-spacing:.4px;color:#f3f8ff;line-height:1.56}
footer .s1 p{font-size:31px}footer .s2 p{font-size:28px}
footer .s3 p{font-size:25px}footer .s4 p{font-size:22.5px}
footer .meta{display:flex;align-items:center;justify-content:space-between;margin-top:8px}
.dots{display:flex;gap:7px;align-items:center}
.dots i{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,.16);display:block}
.dots i.on{width:26px;border-radius:5px;background:#38bdf8;box-shadow:0 0 13px rgba(56,189,248,.9)}
.dots em{font-size:12px;color:#7f93ab;font-style:normal;letter-spacing:1px;margin-left:8px}
.fmeta{font-size:11px;color:#66798f;letter-spacing:1px}
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
    <div><div class="nm">OpenOntology</div><div class="sb">MCTS SANDBOX · v0.3.0</div></div>
  </div>
  <h1>{title}</h1>
  <div class="badges">{chips}</div>
</header>"""


def footer(text: str, idx: int, meta: str) -> str:
    n = len(text)
    size = "s1" if n <= 92 else ("s2" if n <= 124 else ("s3" if n <= 150 else "s4"))
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
         idx: int, meta: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="stage">
{head(title, badges)}
<div class="content">{content}</div>
{footer(sub, idx, meta)}
</div></body></html>"""


# ---------------------------------------------------------------------------
# SVG 公共件
# ---------------------------------------------------------------------------
DEFS = """
<defs>
  <linearGradient id="gcy" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#12263c"/><stop offset="1" stop-color="#081422"/>
  </linearGradient>
  <linearGradient id="ggr" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#0e2f1f"/><stop offset="1" stop-color="#08190f"/>
  </linearGradient>
  <linearGradient id="grd" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#331414"/><stop offset="1" stop-color="#1a0b0b"/>
  </linearGradient>
  <linearGradient id="gam" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#2e2211"/><stop offset="1" stop-color="#191206"/>
  </linearGradient>
  <linearGradient id="gvi" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#1e1c3d"/><stop offset="1" stop-color="#0f0e22"/>
  </linearGradient>
  <linearGradient id="gsl" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#141a24"/><stop offset="1" stop-color="#0a0e15"/>
  </linearGradient>
  <linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#38bdf8"/><stop offset="1" stop-color="#6366f1"/>
  </linearGradient>
  <linearGradient id="bargr" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#22c55e"/><stop offset="1" stop-color="#38bdf8"/>
  </linearGradient>
  <linearGradient id="baram" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#f5a524"/><stop offset="1" stop-color="#ef4444"/>
  </linearGradient>
  <filter id="glow" x="-45%" y="-45%" width="190%" height="190%">
    <feGaussianBlur stdDeviation="8" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
          orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#5f88b5"/>
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


def box(x, y, w, h, fill="url(#gcy)", stroke="#2a4a6b", rx=12, sw=1.4, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def txt(x, y, s, size=14, fill="#dce8f6", weight=600, anchor="start", ls="0.4"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}" letter-spacing="{ls}">{s}</text>')


def arrow(x1, y1, x2, y2, color="#5f88b5", marker="arw", dash="", sw=1.8, label="",
          lx=0, ly=0, lsize=11, lfill="#8fa3bb", lanchor="middle"):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out = (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
           f'stroke-width="{sw}" marker-end="url(#{marker})"{d}/>')
    if label:
        out += txt(lx or (x1 + x2) / 2, ly or (y1 + y2) / 2 - 7, label, lsize, lfill,
                   500, lanchor, "0.3")
    return out


def node(x, y, w, h, title, lines, fill="url(#gcy)", stroke="#2a4a6b",
         sw=1.6, dash="", glow=False):
    """搜索树节点：标题 + 若干行副文本。"""
    body = box(x, y, w, h, fill, stroke, 12, sw, dash)
    if glow:
        body = box(x, y, w, h, "none", stroke, 12, sw + 3.2, dash).replace(
            'stroke-width="4.8"', 'stroke-width="4.8" opacity="0.22"'
        ) + body
    body += txt(x + 14, y + 26, title, 14.5, "#eef6ff", 700)
    for i, (s, size, color, weight) in enumerate(lines):
        body += txt(x + 14, y + 26 + 20 * (i + 1), s, size, color, weight)
    return body


# ---------------------------------------------------------------------------
# Scene 1 · 为什么单步推演不够
# ---------------------------------------------------------------------------
def scene_1(f: dict[str, Any]) -> str:
    orders = f["orders"]
    trucks = f["trucks"]

    order_rows = "".join(
        f'<tr><td class="id">{o["id"]}</td><td>{o["cargo"]}</td>'
        f'<td class="r">{o["eta"]:g}h</td><td class="r">{o["cold"]} 级</td>'
        f'<td class="r">¥{o["value"]:,.0f}</td>'
        f'<td class="r rd">−{o["penalty"]:,.0f}</td></tr>'
        for o in orders
    )
    truck_rows = "".join(
        f'<tr><td class="id">{t["id"]}</td><td>{t["name"]}</td>'
        f'<td class="r">{t["eta"]:g}h</td><td class="r">{t["cold"]} 级</td>'
        f'<td class="r">¥{t["cost"]:,.0f}</td>'
        f'<td class="r">{"<span class=\'tag green\'>✓ 可承运</span>" if t["cold"] >= 2 and t["eta"] <= 1.5 else "<span class=\'tag red\'>✕ 冷链不足</span>"}</td></tr>'
        for t in trucks
    )

    left = f"""
<div class="panel cy" style="flex:1">
  <div class="ph"><b><i>▍</i>初始局势 s0 · 三张连环急救运单</b><span>EMERGENCY ORDERS</span></div>
  <div class="body" style="justify-content:flex-start;gap:9px">
    <div class="mono" style="color:#5f88b5;letter-spacing:1.5px">① 急救运单 · ORDERS</div>
    <table class="tbl">
      <tr><th>运单</th><th>货物</th><th class="r">ETA 上限</th><th class="r">冷链</th>
          <th class="r">基础价值</th><th class="r">断供罚金</th></tr>
      {order_rows}
    </table>
    <div class="card bd" style="padding:7px 12px">
      <div class="mono" style="color:#fca5a5">⚠ ORD-2 是特级移植器官 / 血浆：
      ≤ 1.5h 且必须 2 级超低温 · 违约重罚 ¥50,000</div>
    </div>
    <div class="mono" style="color:#5f88b5;letter-spacing:1.5px;margin-top:2px">② 冷链车队 · TRUCKS</div>
    <table class="tbl">
      <tr><th>冷链车</th><th>车型</th><th class="r">ETA</th><th class="r">冷链</th>
          <th class="r">运费</th><th class="r">承运血浆单</th></tr>
      {truck_rows}
    </table>
    <div class="card gd" style="padding:7px 12px">
      <div class="mono" style="color:#86efac">硬约束：全场只有 TRK-FAST 同时满足
      ORD-2 的 ≤1.5h 时效与 2 级超低温冷链</div>
    </div>
    <div class="card am">
      <div class="ct">隐藏的稀薄解空间 <small>SOLUTION SPACE</small></div>
      <div class="kv"><span>3 张运单 × 3 辆车的全部排列</span><b>3! = 6 种</b></div>
      <div class="kv"><span>满足全部硬约束的三单全通方案</span><b class="gr">仅 1 种</b></div>
      <div class="mono" style="margin-top:3px;color:#fcd34d">
        只有把 TRK-FAST 留给 ORD-2 的那一种 —— 靠单步贪心几乎撞不上
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:2px">
      <div class="card cy" style="text-align:center;padding:7px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v hl" style="font-size:26px">¥47,000</span></div>
        <div class="mono" style="margin-top:3px;font-size:10.5px">三单基础价值合计</div>
      </div>
      <div class="card bd" style="text-align:center;padding:7px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v rd" style="font-size:26px">¥85,000</span></div>
        <div class="mono" style="margin-top:3px;font-size:10.5px">全部断供罚金</div>
      </div>
      <div class="card am" style="text-align:center;padding:7px 6px">
        <div class="bigstat" style="justify-content:center"><span class="v hl2" style="font-size:26px">1 辆</span></div>
        <div class="mono" style="margin-top:3px;font-size:10.5px">唯一 2 级超低温车</div>
      </div>
    </div>
  </div>
</div>"""

    right = f"""
<div class="panel rd" style="flex:1">
  <div class="ph"><b><i>▍</i>单步 Agent 的局部视野：只看一步的代价</b><span>GREEDY MYOPIA</span></div>
  <div class="body" style="justify-content:flex-start;gap:11px">
    <div class="card cy">
      <div class="ct">它的评估函数 <small>ONE-STEP SCORE</small></div>
      <div class="mono">score(s, a) = 基础价值 − 运费 &nbsp;&nbsp;<span class="dim">// 不含未来状态</span></div>
    </div>
    <div class="card am">
      <div class="ct">于是它看到的最优解 <small>LOCAL OPTIMUM</small></div>
      <div class="kv"><span>ORD-1 → TRK-FAST</span><b class="gr">+¥9,600</b></div>
      <div class="kv"><span>ORD-1 → TRK-MID</span><b>+¥9,100</b></div>
      <div class="kv"><span>ORD-1 → TRK-HEAVY</span><b>+¥8,900</b></div>
      <div class="mono" style="margin-top:4px;color:#fcd34d">第一步必然抢走运费最低的王牌快车</div>
    </div>
    <div class="card bd">
      <div class="ct">它看不见的第二步 <small>HIDDEN DEAD END</small></div>
      <div class="mono" style="line-height:1.72">
        TRK-FAST 一旦被占用，ORD-2 只剩<br>
        TRK-MID / TRK-HEAVY：冷链 1 级 &lt; 2 级<br>
        → 符号裁判当场拒绝，三单连环崩盘
      </div>
    </div>
    <div class="card vi">
      <div class="ct">同样三张运单，两种世界观 <small>ONE STEP VS SEQUENCE</small></div>
      <div class="kv"><span>单步贪心 · 只算眼前一步</span><b class="rd">第 2 步撞死局</b></div>
      <div class="kv"><span>多步推演 · 沙盘里试错到底</span><b class="gr">全局最优序列</b></div>
    </div>
    <div class="card gd">
      <div class="ct">关键洞察 <small>WHY LOOKAHEAD</small></div>
      <div style="font-size:13.5px;line-height:1.6;color:#d3f9dd">
        企业调度不是单步打分题，而是<b class="gr">序列决策</b>：
        每一步都在改写下一步的可行域。
      </div>
    </div>
    <div class="card cy">
      <div class="ct">怎么才能看到第二步 <small>LOOKAHEAD</small></div>
      <div style="font-size:13.2px;line-height:1.6;color:#cfeeff">
        把“如果我这样派，后面几步会怎样”搬进沙盘推演一遍，
        用终局价值反过来决定这一步 —— 这正是下一幕 MCTS 要做的事。
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols">{left}{right}</div>'
    return page(
        '1/6 · 为什么单步推演不够？企业决策中的<span class="n">贪心陷阱</span>',
        ["cy|3 张急救运单", "vi|3 辆冷链车", "am|硬约束 · 2 级冷链"],
        content,
        "在复杂企业调度中，只看眼前一步的贪心决策往往会酿成大祸。比如医院同时下达三张连环急救运单，"
        "其中第二单是特级器官血浆，硬性要求一点五小时内送达且具备二级超低温冷链，一旦断供将面临五万元重罚。",
        0,
        "REAL SEED · examples.mcts_world_model_demo.build_demo()",
    )


# ---------------------------------------------------------------------------
# Scene 2 · 单步贪心翻车实录
# ---------------------------------------------------------------------------
def scene_2(f: dict[str, Any]) -> str:
    g = f["greedy"]
    steps = g["steps"]
    s1 = steps[0]
    err = steps[1]

    flow = svg(1000, 610, f"""
{box(26, 4, 948, 120, "url(#ggr)", "#1f6b45", 13, 1.5)}
{txt(46, 38, "① ORD-1 普通胰岛素补货", 17.5, "#d3f9dd", 700)}
{txt(46, 66, "→ TRK-FAST 王牌超低温快车", 15, "#a7f3c6", 600)}
{txt(46, 94, "运费 ¥400 全场最低 · 单步合法 ✅ · 立即净赚 ¥9,600", 13.5, "#7fbf98", 500)}
{txt(952, 38, "PRIOR P=0.553", 13, "#86efac", 700, "end")}
{txt(952, 66, "先验最高", 12.2, "#6da887", 500, "end")}

{arrow(500, 126, 500, 158, "#ef4444", "arwr", "", 2.6)}

{box(26, 162, 948, 176, "url(#grd)", "#ef4444", 13, 2.2)}
{txt(46, 198, "② ORD-2 特级移植器官 / 血浆急单 → 无车可用", 17.5, "#fecaca", 700)}
{txt(46, 228, "TRK-MID 冷链 1 级 &lt; 2 级 · TRK-HEAVY 冷链 1 级 &lt; 2 级", 13.5, "#f0a8a8", 500)}
{txt(46, 258, "InvariantError: ETA_OR_COLD_LEVEL_VIOLATION", 15, "#fca5a5", 700)}
{txt(46, 286, "符号裁判硬护栏当场拒绝 · 剩余 2 张运单全部违约", 13.5, "#c98f8f", 500)}
{txt(46, 314, "罚金 50,000 + 15,000 = 65,000 元，远超省下的 700 元运费", 13, "#e39a9a", 600)}
{txt(952, 198, "DEAD END", 15.5, "#f87171", 800, "end")}
{txt(952, 228, "第 2 步即崩", 12.5, "#c98f8f", 500, "end")}

{arrow(500, 342, 500, 374, "#ef4444", "arwr", "4 5", 2.2)}

{box(26, 378, 948, 100, "url(#gsl)", "#3d4a5c", 13, 1.4, "7 5")}
{txt(46, 412, "③ ORD-3 郊区疫苗配送 → 流程中断，未处理", 16.5, "#9aa9ba", 700)}
{txt(46, 440, "贪心策略在死局处停下，终局只剩 1/3 履约", 13.5, "#7c8b9d", 500)}
{txt(952, 412, "ABORTED", 14, "#7c8b9d", 700, "end")}

{txt(26, 514, "① 单步合法  ≠  ② 全局可行", 17.5, "#fcd34d", 800)}
{txt(26, 544, "局部最优的每一步都合法，但序列整体撞上不变量铁律 —— 这正是需要多步推演的根因。",
    13.5, "#8fa3bb", 500)}

{box(26, 560, 296, 44, "url(#gsl)", "#2a4a6b", 10, 1.2)}
{txt(40, 588, "单步得分 +¥9,600", 14, "#86efac", 700)}
{box(352, 560, 296, 44, "url(#gsl)", "#7a3535", 10, 1.2)}
{txt(366, 588, "终局价值 −¥55,400", 14, "#f87171", 700)}
{box(678, 560, 296, 44, "url(#gsl)", "#7a5a1e", 10, 1.2)}
{txt(692, 588, "一步之差 −¥65,000", 14, "#fcd34d", 700)}
""")

    left = f"""
<div class="panel rd" style="flex:1">
  <div class="ph"><b><i>▍</i>方法 A · 单步贪心策略的翻车路径</b><span>GREEDY POLICY TRACE</span></div>
  <div class="body" style="justify-content:center">{flow}</div>
</div>"""

    right = f"""
<div class="panel rd" style="flex:1">
  <div class="ph"><b><i>▍</i>贪心终局账单</b><span>TERMINAL BILL</span></div>
  <div class="body" style="justify-content:flex-start;gap:11px">
    <div class="card bd">
      <div class="kv"><span>履约运单</span><b class="rd">{g["fulfilled"]} / 3</b></div>
      <div class="kv"><span>已收基础价值</span><b>¥{g["revenue"]:,.0f}</b></div>
      <div class="kv"><span>已付运费</span><b>−¥{g["freight"]:,.0f}</b></div>
      <div class="kv"><span>未履约重罚</span><b class="rd">−¥{g["penalty"]:,.0f}</b></div>
      <div style="border-top:1px dashed rgba(120,160,210,.22);margin:5px 0 4px"></div>
      <div class="kv" style="font-size:15px"><span>终局净价值 V</span>
        <b class="rd" style="font-size:19px">−¥{abs(g["value"]):,.0f}</b></div>
    </div>
    <div class="card am">
      <div class="ct">罚金是怎么滚出来的 <small>PENALTY BREAKDOWN</small></div>
      <div class="kv"><span>ORD-2 特级血浆断供</span><b class="rd">−¥50,000</b></div>
      <div class="kv"><span>ORD-3 疫苗未处理</span><b class="rd">−¥15,000</b></div>
      <div class="mono" style="margin-top:4px;color:#fcd34d">
        为省 ¥700 运费，赔掉 ¥65,000 违约金
      </div>
    </div>
    <div class="card vi">
      <div class="ct">错误码 <small>INVARIANT ERROR</small></div>
      <div class="mono" style="color:#fca5a5">{g["error_code"]}</div>
      <div class="mono" style="margin-top:4px;color:#8fa3bb;white-space:normal">
        {err["message"]}
      </div>
    </div>
    <div class="card gd">
      <div class="ct">失败根因 <small>ROOT CAUSE</small></div>
      <div style="font-size:13.5px;line-height:1.62;color:#d3f9dd">
        贪心只优化 <b class="hl2">单步运费</b>，把全场唯一的
        <b class="hl">2 级超低温救命车</b> 提前消耗在低价值运单上。
      </div>
    </div>
    <div class="card cy">
      <div class="ct">一步之差 <small>COUNTERFACTUAL</small></div>
      <div class="kv"><span>贪心首派 TRK-FAST</span><b class="rd">−¥55,400</b></div>
      <div class="kv"><span>若首派 TRK-HEAVY，救命车留给血浆单</span>
        <b class="gr">+¥{f["commit"]["final_value"]:,.0f}</b></div>
      <div class="mono" style="margin-top:3px;color:#a5e4ff">
        同一批运单、同一批车，只差第一步的先后顺序
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols narrow">{left}{right}</div>'
    return page(
        '2/6 · 方法 A 翻车实录：单步贪心策略撞上<span class="n">死胡同</span>',
        ["am|Greedy ReAct", "red|InvariantError", "rd|V = −55,400"],
        content,
        "看普通单步贪心智能体的翻车现场：第一步处理普通胰岛素订单时，它贪图王牌快车运费最便宜，"
        "直接把唯一一辆超低温快车派了出去；结果到了第二步处理特级血浆急单时，剩下的车全部违反时效与冷链铁律，"
        "当场触发死局，造成负五万五千四百元的惨重亏损。",
        1,
        f"REAL RUN · failed_at={g['failed_at']} · fulfilled={g['fulfilled']}/3",
    )


# ---------------------------------------------------------------------------
# Scene 3 · MCTS 原理与平行宇宙沙盘
# ---------------------------------------------------------------------------
def scene_3(f: dict[str, Any]) -> str:
    m = f["mcts"]
    phases = [
        ("①", "Selection · PUCT", "#38bdf8", "gcy", "#2a4a6b",
         "Q + c·P·√N(s) / (1 + N(s,a))",
         "在已展开的树上按“价值 + 探索”平衡下钻"),
        ("②", "Expansion · 符号裁判", "#f5a524", "gam", "#7a5a1e",
         "沙盘真跑 submit_action",
         "抛 InvariantError 的候选动作当场剪枝"),
        ("③", "Simulation · 终局价值", "#a78bfa", "gvi", "#4a3f7a",
         "V(s) = 收益 − 运费 − 重罚",
         "在另一个 fork 上把剩余步数推演到底"),
        ("④", "Backpropagation · 回传", "#22c55e", "ggr", "#1f6b45",
         "N ← N+1 · W ← W+V · Q = W/N",
         "沿路径回传，更新每个动作的访问数与价值"),
    ]
    y = 8
    flow = ""
    for idx, (num, title, color, fill, stroke, formula, desc) in enumerate(phases):
        flow += box(10, y, 940, 114, f"url(#{fill})", stroke, 12, 1.5)
        flow += txt(28, y + 36, num, 25, color, 800)
        flow += txt(66, y + 36, title, 17, "#eef6ff", 700)
        flow += txt(66, y + 64, formula, 13.2, color, 600)
        flow += txt(66, y + 90, desc, 12.8, "#93a7bd", 500)
        if idx < 3:
            flow += arrow(480, y + 114, 480, y + 136, "#5f88b5", "arw", "", 1.9)
        y += 136

    flow += txt(10, 566, "确定性世界模型 = 同一个动作在沙盘里重放多少次，结果都 100% 可复现 —— 这是 MCTS 敢于信任模拟的前提。",
                12.8, "#8fa3bb", 500)

    left = f"""
<div class="panel cy" style="flex:1">
  <div class="ph"><b><i>▍</i>MCTS 四阶段循环 · 在符号世界模型上推演</b><span>ALPHAGO LOOP</span></div>
  <div class="body" style="justify-content:center">
    {svg(960, 590, flow)}
  </div>
</div>"""

    right = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>fork_runtime：毫秒级平行宇宙沙盘</b><span>SANDBOX ISOLATION</span></div>
  <div class="body" style="justify-content:flex-start;gap:11px">
    <div class="card cy">
      <div class="ct">克隆机制 <small>SQLITE NATIVE BACKUP</small></div>
      <div class="mono" style="line-height:1.7">
        runtime.connection.backup(fork.connection)<br>
        <span class="dim">// objects / links / overlay / 审计账本 一次性复制</span>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div class="card gd" style="text-align:center;padding:10px 8px">
        <div class="bigstat" style="justify-content:center">
          <span class="v gr">{f["main_writes_during_search"]}</span>
        </div>
        <div class="mono" style="margin-top:5px">主库写入次数</div>
      </div>
      <div class="card cy" style="text-align:center;padding:10px 8px">
        <div class="bigstat" style="justify-content:center">
          <span class="v hl">{m["forks"]}</span>
        </div>
        <div class="mono" style="margin-top:5px">平行沙盘分叉</div>
      </div>
    </div>
    <div class="card vi">
      <div class="ct">沙盘内真实写入 <small>SANDBOX WRITES</small></div>
      <div class="kv"><span>沙盘 submit_action 次数</span><b class="vi">{m["sandbox_writes"]}</b></div>
      <div class="kv"><span>探索树节点数</span><b>{m["explored"]}</b></div>
      <div class="kv"><span>符号裁判剪枝非法节点</span><b class="hl2">{m["pruned"]}</b></div>
      <div class="kv"><span>死局节点 dead_end</span><b class="rd">{m["dead_ends"]}</b></div>
    </div>
    <div class="card am">
      <div class="ct">沙盘生命周期 <small>FORK LIFECYCLE</small></div>
      <div style="font-size:13.2px;line-height:1.6;color:#fde68a">
        分叉 → 试错 → 读取终局价值 → 立即关闭释放；
        主库句柄全程只读，审计账本里不会留下任何一次失败的试探。
      </div>
    </div>
    <div class="card gd">
      <div class="ct">为什么安全 <small>WHY SAFE</small></div>
      <div style="font-size:13.2px;line-height:1.6;color:#d3f9dd">
        全部试错都发生在 fork 里，主库 <b class="gr">0 写入、0 污染</b>；
        只有搜索收敛后，才通过受治理 Action 正式落子。
      </div>
    </div>
    <div class="card vi">
      <div class="ct">确定性 rollout 的红利 <small>DETERMINISTIC ROLLOUT</small></div>
      <div class="kv"><span>同一动作重复推演</span><b class="vi">结果 100% 一致</b></div>
      <div class="kv"><span>非法动作判定</span><b>护栏当场返回</b></div>
      <div class="kv"><span>终局价值来源</span><b>业务 Function 精算</b></div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols narrow">{left}{right}</div>'
    return page(
        '3/6 · 破局之道：AlphaGo MCTS 在<span class="n">符号世界模型</span>中分叉推演',
        ["vi|fork_runtime", "gr|主库 0 写入", "cy|符号裁判剪枝"],
        content,
        "AlphaGo 的蒙特卡洛树搜索，本质上就是在确定性的符号世界模型中做多步沙盘推演。"
        "OpenOntology 通过毫秒级内存克隆开辟平行宇宙沙盘，由 Action 前置护栏充当符号规则裁判自动剪枝非法死局，"
        "由纯业务 Function 精算终局价值，并沿搜索树反向传播更新动作价值 Q。",
        2,
        f"REAL RUN · forks={m['forks']} · sandbox_writes={m['sandbox_writes']} · main_writes=0",
    )


# ---------------------------------------------------------------------------
# Scene 4 · 80 次树搜索可视化
# ---------------------------------------------------------------------------
def scene_4(f: dict[str, Any]) -> str:
    m = f["mcts"]
    ra = m["root_actions"]
    fast = ra["ORD-1->TRK-FAST"]
    mid = ra["ORD-1->TRK-MID"]
    heavy = ra["ORD-1->TRK-HEAVY"]

    tree = svg(1010, 600, f"""
{arrow(200, 300, 246, 106, "#7a4a4a", "arwr", "5 5", 2.0)}
{arrow(200, 300, 246, 300, "#5f88b5", "arw", "", 2.0)}
{arrow(200, 300, 246, 494, "#22c55e", "arwg", "", 4.2)}

{node(20, 262, 180, 76, "s0 · 根节点", [
    (f"N = {m['root_visits']}", 12.6, "#9fb4cc", 600),
    ("3 条候选边", 12.2, "#7f93ab", 500),
], "url(#gvi)", "#6d5fc4", 2.0)}

{node(248, 62, 212, 88, "ORD-1 → TRK-FAST", [
    (f"P={fast['prior']:.3f} · N={fast['visits']}", 12.4, "#9fb4cc", 600),
    (f"Q = {_money(fast['q'])}", 15.5, "#f87171", 800),
], "url(#grd)", "#ef4444", 2.0, "6 4")}

{node(248, 256, 212, 88, "ORD-1 → TRK-MID", [
    (f"P={mid['prior']:.3f} · N={mid['visits']}", 12.4, "#9fb4cc", 600),
    (f"Q = {_money(mid['q'])}", 15.5, "#fcd34d", 800),
], "url(#gam)", "#a8792a", 1.8)}

{node(248, 450, 212, 88, "ORD-1 → TRK-HEAVY", [
    (f"P={heavy['prior']:.3f} · N={heavy['visits']}", 12.4, "#d3f9dd", 600),
    (f"Q = {_money(heavy['q'])}", 15.5, "#4ade80", 800),
], "url(#ggr)", "#22c55e", 2.2)}

{arrow(460, 106, 530, 106, "#ef4444", "arwr", "4 4", 1.7)}
{box(532, 76, 190, 60, "url(#gsl)", "#7a3535", 10, 1.4, "5 4")}
{txt(546, 100, "✕ 3 条候选全剪枝", 12.6, "#f87171", 700)}
{txt(546, 120, "第 2 步即死局", 11.8, "#b08a8a", 500)}

{arrow(460, 300, 530, 300, "#5f88b5", "arw", "", 1.9)}
{node(532, 256, 200, 88, "ORD-2 → TRK-FAST", [
    ("N=10 · Q=+18,700", 12.4, "#9fb4cc", 600),
    ("仅剩 ORD-3 可救", 12.0, "#8fa3bb", 500),
], "url(#gam)", "#a8792a", 1.6)}
{arrow(732, 300, 800, 300, "#f5a524", "arwa", "4 4", 1.7)}
{box(802, 272, 190, 58, "url(#gsl)", "#7a5a1e", 10, 1.4, "5 4")}
{txt(816, 296, "✕ 3 条候选全剪枝", 12.6, "#fcd34d", 700)}
{txt(816, 316, "Q 止步 +18,700", 11.8, "#b0956a", 500)}

{arrow(460, 494, 530, 494, "#22c55e", "arwg", "", 3.4)}
{node(532, 450, 200, 88, "ORD-2 → TRK-FAST", [
    ("N=64 · Q=+44,600", 12.4, "#d3f9dd", 600),
    ("救命车精准留给血浆单", 11.8, "#7fbf98", 500),
], "url(#ggr)", "#22c55e", 2.4, glow=True)}
{arrow(732, 494, 800, 494, "#22c55e", "arwg", "", 3.4)}
{node(802, 450, 190, 88, "ORD-3 → TRK-MID", [
    ("N=63 · Q=+44,600", 12.4, "#d3f9dd", 600),
    ("三单全通 · 终局锁定", 11.8, "#7fbf98", 500),
], "url(#ggr)", "#22c55e", 2.4, glow=True)}

{txt(20, 580, "● 粗绿边 = 胜出主路径", 12.2, "#4ade80", 600)}
{txt(210, 580, "● 红虚线 = 符号裁判剪枝的死局分支", 12.2, "#f87171", 600)}
{txt(510, 580, "● 节点内：先验 P / 访问次数 N / 期望价值 Q", 12.2, "#8fa3bb", 600)}
""")

    rows = ""
    for stat in (fast, mid, heavy):
        tone = "rd" if stat["q"] < 0 else ("gr" if stat["q"] > 40000 else "hl2")
        tag = ("被抛弃", "tag red") if stat["visits"] < 8 else (
            ("胜出", "tag green") if stat["visits"] >= 60 else ("次优", "tag amber"))
        rows += (
            f'<tr><td class="id">{stat["label"]}</td>'
            f'<td class="r">{stat["prior"]:.3f}</td>'
            f'<td class="r">{stat["visits"]}</td>'
            f'<td class="r {tone}">{_money(stat["q"])}</td>'
            f'<td class="r"><span class="{tag[1]}">{tag[0]}</span></td></tr>'
        )

    left = f"""
<div class="panel cy" style="flex:1">
  <div class="ph"><b><i>▍</i>真实 MCTS 搜索树拓扑 · {m["simulations"]} 次沙盘推演</b>
    <span>SEARCH TREE TOPOLOGY</span></div>
  <div class="body" style="justify-content:center">{tree}</div>
</div>"""

    right = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>根节点统计 N / Q / P</b><span>ROOT STATS</span></div>
  <div class="body" style="justify-content:flex-start;gap:11px">
    <table class="tbl" style="font-size:12.6px">
      <tr><th>动作</th><th class="r">先验 P</th><th class="r">访问 N</th>
          <th class="r">期望 Q</th><th class="r">判决</th></tr>
      {rows}
    </table>
    <div class="card cy" style="padding:8px 12px">
      <div class="ct">80 次模拟的访问分配 <small>VISIT ALLOCATION</small></div>
      <div style="display:flex;height:26px;border-radius:6px;overflow:hidden;margin:2px 0 6px">
        <div style="width:7.5%;background:#ef4444;opacity:.8;display:grid;place-items:center;
                    font-size:11px;font-weight:700;color:#2a0b0b">6</div>
        <div style="width:12.5%;background:#f5a524;opacity:.85;display:grid;place-items:center;
                    font-size:11px;font-weight:700;color:#2a1c05">10</div>
        <div style="width:80%;background:linear-gradient(90deg,#22c55e,#38bdf8);display:grid;
                    place-items:center;font-size:11.5px;font-weight:700;color:#04200f">64 · 80%</div>
      </div>
      <div class="mono">访问预算向高价值分支集中：先验只决定先看哪儿，价值决定看多久</div>
    </div>
    <div class="card bd">
      <div class="ct">先验最高 ≠ 价值最高 <small>PRIOR TRAP</small></div>
      <div style="font-size:13.2px;line-height:1.6;color:#fecaca">
        先验 <b class="hl2">0.553</b> 的 TRK-FAST 只被试探
        <b class="rd">{fast["visits"]}</b> 次就抛弃：
        往下推演一步，<b class="rd">{_money(fast["q"])}</b> 的终局价值暴露无遗。
      </div>
    </div>
    <div class="card gd">
      <div class="ct">先验最低反而胜出 <small>SEARCH OVERRIDE</small></div>
      <div style="font-size:13.2px;line-height:1.6;color:#d3f9dd">
        先验仅 <b class="hl2">0.201</b> 的长途车，因解锁三单全通拿到
        <b class="gr">{_money(heavy["q"])}</b>，吸引
        <b class="gr">{heavy["visits"]}</b> 次访问强势胜出。
      </div>
    </div>
    <div class="card am">
      <div class="ct">剪枝都发生在哪一步 <small>PRUNE BY DEPTH</small></div>
      {''.join(
          f'<div class="kv"><span>派第 {depth} 单时，候选车被符号裁判拒绝</span>'
          f'<b class="hl2">{count} 次</b></div>'
          for depth, count in m["prune_by_depth"].items()
      )}
      <div class="mono" style="margin-top:3px">死局越早暴露，越省下无谓的沙盘推演预算</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div class="card am" style="text-align:center;padding:9px 8px">
        <div class="bigstat" style="justify-content:center"><span class="v hl2">{m["pruned"]}</span></div>
        <div class="mono" style="margin-top:4px">符号裁判剪枝节点</div>
      </div>
      <div class="card cy" style="text-align:center;padding:9px 8px">
        <div class="bigstat" style="justify-content:center"><span class="v hl">{m["forks"]}</span></div>
        <div class="mono" style="margin-top:4px">平行沙盘分叉</div>
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols wide">{left}{right}</div>'
    return page(
        '4/6 · 80 次 MCTS 树搜索可视化：<span class="n">PUCT 如何逃离贪心陷阱</span>',
        ["cy|simulations = 80", "am|剪枝 12 节点", "gr|N=64 胜出"],
        content,
        "在八十次蒙特卡洛沙盘推演中，尽管第一步派王牌快车的先验概率高达零点五五三，"
        "但树搜索往下推演一步就撞上符号裁判剪枝，期望价值暴跌至负五万五千四百元，仅试探六次就被果断放弃；"
        "而先验最低的长途车路线因为把王牌救命车留给了第二步的特级血浆单，吸引了六十四次访问脱颖而出！",
        3,
        "REAL RUN · c_puct=1.414 · seed=17 · pruned=12 · dead_ends=2",
    )


# ---------------------------------------------------------------------------
# Scene 5 · 最优三步落子与时光机
# ---------------------------------------------------------------------------
def scene_5(f: dict[str, Any]) -> str:
    m = f["mcts"]
    c = f["commit"]
    g = f["greedy"]
    steps = m["best_steps"]
    costs = {"TRK-HEAVY": 1100, "TRK-FAST": 400, "TRK-MID": 900}
    trucks = {t["id"]: t for t in f["trucks"]}
    orders = {o["id"]: o for o in f["orders"]}

    plan_rows = ""
    for i, st in enumerate(steps, start=1):
        cost = costs[st["truck"]]
        o = orders[st["order"]]
        plan_rows += (
            f'<div class="card gd" style="padding:8px 12px">'
            f'<div class="ct"><span>Step {i} · {st["order"]} → {st["truck"]} '
            f'<span class="mono" style="color:#7fbf98">{trucks[st["truck"]]["name"]}</span></span>'
            f'<b class="gr">¥{cost:,.0f}</b></div>'
            f'<div class="mono">ETA {trucks[st["truck"]]["eta"]:g}h ≤ {o["eta"]:g}h · '
            f'冷链 {trucks[st["truck"]]["cold"]} ≥ {o["cold"]} 级 · '
            f'价值 ¥{o["value"]:,.0f}</div></div>'
        )

    left = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>MCTS 锁定的全局最优三步序列</b><span>OPTIMAL PLAN</span></div>
  <div class="body" style="justify-content:flex-start;gap:11px">
    {plan_rows}
    <div class="card cy">
      <div class="kv"><span>总基础价值</span><b>¥{sum(o["value"] for o in f["orders"]):,.0f}</b></div>
      <div class="kv"><span>总运费</span><b>−¥{sum(costs.values()):,.0f}</b></div>
      <div class="kv" style="font-size:15px"><span>终局净价值 V</span>
        <b class="gr" style="font-size:19px">+¥{m["best_value"]:,.0f}</b></div>
    </div>
    <div class="card vi">
      <div class="ct">与贪心策略的关键差异 <small>DECISION DIVERGENCE</small></div>
      <div class="mono" style="line-height:1.75">
        贪心：ORD-1 → TRK-FAST（省 700 元运费，赔 65,000 罚金）<br>
        MCTS：ORD-1 → TRK-HEAVY（多花 700，把救命车留给血浆单）
      </div>
    </div>
    <div class="card gd">
      <div class="ct">为什么这样排 <small>LOOKAHEAD REASONING</small></div>
      <div style="font-size:13.2px;line-height:1.6;color:#d3f9dd">
        TRK-FAST 的稀缺性只在 ORD-2 上体现：把它留给最难满足的运单，
        普通单交给“够用就好”的长途车，整体价值最大。
      </div>
    </div>
    <div class="card cy">
      <div class="ct">正式落子后的世界状态 <small>POST-COMMIT STATE</small></div>
      <div class="kv"><span>ORD-1 普通胰岛素</span><b>TRK-HEAVY · assigned</b></div>
      <div class="kv"><span>ORD-2 特级血浆急单</span><b>TRK-FAST · assigned</b></div>
      <div class="kv"><span>ORD-3 郊区疫苗</span><b>TRK-MID · assigned</b></div>
      <div class="kv"><span>履约率</span><b class="gr">3 / 3 · 零违约</b></div>
    </div>
  </div>
</div>"""

    # 对比条形图：负值向左，正值向右
    zero_x = 470
    scale = 380 / 60000

    def bar(value: float, y: int, color: str, label: str):
        w = abs(value) * scale
        if value >= 0:
            rect = (f'<rect x="{zero_x}" y="{y}" width="{w:.1f}" height="44" rx="6" '
                    f'fill="{color}" opacity="0.85"/>')
            tx = zero_x + w + 14
            anchor = "start"
        else:
            rect = (f'<rect x="{zero_x - w:.1f}" y="{y}" width="{w:.1f}" height="44" rx="6" '
                    f'fill="{color}" opacity="0.85"/>')
            tx = zero_x - w - 14
            anchor = "end"
        return (txt(20, y - 10, label, 13, "#8fa3bb", 600)
                + rect
                + txt(tx, y + 29, f"{_money(value)} 元", 16.5, color, 800, anchor))

    chart = svg(940, 330, f"""
<line x1="{zero_x}" y1="46" x2="{zero_x}" y2="286" stroke="#3d4a5c" stroke-width="1.4"
      stroke-dasharray="4 4"/>
{txt(zero_x, 36, "0", 12, "#6b7c91", 600, "middle")}
{bar(g["value"], 78, "#ef4444", "单步贪心 · 终局净价值 V")}
{bar(m["best_value"], 188, "#22c55e", "MCTS 多步推演 · 终局净价值 V")}
{txt(20, 300, f"逆转幅度 = {_money(f['delta'])} 元", 19, "#fcd34d", 800)}
{txt(400, 300, "同一局势、同一套符号规则，仅因是否多步推演，终局相差整整十万元。", 12.6, "#8fa3bb", 500)}
""")

    right = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>正式落子与主库零污染指纹</b><span>COMMIT &amp; TIME MACHINE</span></div>
  <div class="body" style="justify-content:flex-start;gap:11px">
    {chart}
    <div class="card cy">
      <div class="ct">搜索期间主库指纹 <small>ZERO POLLUTION</small></div>
      <div class="kv"><span>主库写入次数</span><b class="gr">{f["main_writes_during_search"]}</b></div>
      <div class="kv"><span>搜索前 runs / links</span>
        <b>{f["runs_before"]} / {f["links_before"]}</b></div>
      <div class="kv"><span>提交后审计账本 runs</span><b>{c["total_runs"]}</b></div>
    </div>
    <div class="card gd">
      <div class="ct">时光机一键悔棋 <small>REVERT / REDO</small></div>
      <div class="mono" style="line-height:1.75">
        revert {c["revert_run"]} → {c["revert_status"]} · ORD-3 = {c["order3_after_revert"]}<br>
        redo MCTS-PLAN-3-REDO → {c["redo_status"]} · ORD-3 = {c["order3_after_redo"]}
      </div>
    </div>
    <div class="card vi">
      <div class="ct">零污染是怎么做到的 <small>ISOLATION</small></div>
      <div class="mono" style="line-height:1.75">
        {m["forks"]} 次分叉 × {m["sandbox_writes"]} 次沙盘写入 → 全部落在 fork<br>
        主库只接受 3 次正式 run + 1 次 revert + 1 次 redo
      </div>
    </div>
  </div>
</div>"""

    content = f'<div class="cols narrow">{left}{right}</div>'
    return page(
        '5/6 · 全局最优三步序列落子与<span class="n">时光机一键悔棋</span>',
        ["gr|+44,600 元", "am|逆转 +100,000", "vi|revert / redo"],
        content,
        "最终，MCTS 锁定全局最优三步序列：第一步派长途车送普通单，第二步把王牌超低温快车精准留给特级血浆急单，"
        "第三步派中型车送疫苗单。三单全部合法履约，净收益正四万四千六百元，比单步贪心策略逆转整整十万元！"
        "整个搜索期间主库零写入零污染，落子后仍支持时光机一键悔棋。",
        4,
        f"REAL RUN · committed={len(c['runs'])} · final_V={c['final_value']:,.0f} · delta={f['delta']:,.0f}",
    )


# ---------------------------------------------------------------------------
# Scene 6 · 终极范式
# ---------------------------------------------------------------------------
def scene_6(f: dict[str, Any]) -> str:
    m = f["mcts"]
    panel = svg(1840, 580, f"""
{box(20, 6, 430, 196, "url(#gvi)", "#6d5fc4", 14, 1.8)}
{txt(44, 46, "LLM 大模型", 21, "#ddd6fe", 800)}
{txt(44, 78, "策略先验网络 P(s,a)", 14.5, "#b9aef2", 600)}
{txt(44, 112, "提供直觉与候选排序", 13, "#8e86bd", 500)}
{txt(44, 140, "不懂业务 · 会幻觉", 13, "#8e86bd", 500)}
{txt(44, 168, "绝不允许直接改库", 13, "#8e86bd", 500)}
{txt(424, 186, "PRIOR", 12, "#6d5fc4", 700, "end")}

{txt(468, 112, "+", 32, "#5f7a99", 800, "middle")}

{box(496, 6, 458, 196, "url(#gcy)", "#38bdf8", 14, 2.0)}
{txt(520, 46, "OpenOntology", 21, "#cfeeff", 800)}
{txt(520, 78, "Objects / Links / Actions", 14.5, "#9ad8f7", 600)}
{txt(520, 112, "确定性符号世界模型 + 规则裁判", 13, "#7fa8c4", 500)}
{txt(520, 140, "不变量硬护栏 · 非法动作当场剪枝", 13, "#7fa8c4", 500)}
{txt(520, 168, "零幻觉 · 可审计 · 可回滚", 13, "#7fa8c4", 500)}
{txt(936, 186, "WORLD MODEL", 12, "#38bdf8", 700, "end")}

{txt(980, 112, "+", 32, "#5f7a99", 800, "middle")}

{box(1008, 6, 396, 196, "url(#gam)", "#a8792a", 14, 1.8)}
{txt(1032, 46, "@spec.function", 21, "#fde68a", 800)}
{txt(1032, 78, "精确价值网络 V(s)", 14.5, "#e8c48a", 600)}
{txt(1032, 112, "收益 − 运费 − 违约重罚", 13, "#b79a6f", 500)}
{txt(1032, 140, "纯业务精算，只读不改状态", 13, "#b79a6f", 500)}
{txt(1032, 168, "不看一步，看终局", 13, "#b79a6f", 500)}
{txt(1388, 186, "VALUE", 12, "#a8792a", 700, "end")}

{txt(1430, 112, "=", 30, "#5f7a99", 800, "middle")}

{box(1458, 6, 360, 196, "url(#ggr)", "#22c55e", 14, 2.4)}
{txt(1482, 46, "OntologyMCTSPlanner", 19, "#d3f9dd", 800)}
{txt(1482, 78, "多步沙盘推演规划器", 14.5, "#a7f3c6", 600)}
{txt(1482, 112, f"{m['simulations']} 次模拟 · {m['forks']} 个平行宇宙", 13, "#7fbf98", 500)}
{txt(1482, 140, f"剪枝 {m['pruned']} 个非法节点", 13, "#7fbf98", 500)}
{txt(1482, 168, "主库 0 写入 · 0 污染", 13, "#7fbf98", 500)}
{txt(1802, 186, "DECISION", 12, "#22c55e", 700, "end")}

{box(20, 222, 1798, 92, "url(#gsl)", "#2a4a6b", 12, 1.4)}
{txt(46, 260, "a* = argmax  Q(s, a)", 23, "#f3f8ff", 800)}
{txt(390, 260, "Q = W / N    ·    PUCT = Q + c·P·√N(s) / (1 + N(s,a))", 14.5, "#8fa3bb", 500)}
{txt(46, 294, "先验只负责“先看哪儿”，最终由符号裁判与终局价值决定“落在哪儿” —— 这就是企业级 AlphaGo 的决策闭环。",
    13.2, "#7f93ab", 500)}

{box(20, 334, 884, 112, "url(#grd)", "#7a3535", 12, 1.4, "6 5")}
{txt(44, 372, "传统单步 Agent", 17, "#fecaca", 700)}
{txt(44, 402, "看一步走一步 · 幻觉驱动 · 撞死局才发现", 13.2, "#c98f8f", 500)}
{txt(44, 430, "终局 V = −55,400 元", 15.5, "#f87171", 800)}

{arrow(916, 390, 956, 390, "#22c55e", "arwg", "", 3.2)}

{box(962, 334, 856, 112, "url(#ggr)", "#22c55e", 12, 1.8)}
{txt(986, 372, "企业级 AlphaGo 决策系统", 17, "#d3f9dd", 700)}
{txt(986, 402, "多步沙盘推演 · 符号裁判兜底 · 算清终局价值", 13.2, "#8fcca6", 500)}
{txt(986, 430, "终局 V = +44,600 元（逆转 +100,000）", 15.5, "#4ade80", 800)}

{txt(20, 492, "决策闭环", 13, "#8fa3bb", 700)}
{box(100, 470, 330, 62, "url(#gvi)", "#6d5fc4", 10, 1.4)}
{txt(118, 500, "① LLM 提先验", 14, "#ddd6fe", 700)}
{txt(118, 522, "P(s,a) 候选排序", 11.5, "#8e86bd", 500)}
{box(450, 470, 330, 62, "url(#gcy)", "#38bdf8", 10, 1.4)}
{txt(468, 500, "② 沙盘分叉推演", 14, "#cfeeff", 700)}
{txt(468, 522, "fork + rollout", 11.5, "#7fa8c4", 500)}
{box(800, 470, 330, 62, "url(#gam)", "#a8792a", 10, 1.4)}
{txt(818, 500, "③ 符号裁判剪枝", 14, "#fde68a", 700)}
{txt(818, 522, "InvariantError 死局", 11.5, "#b79a6f", 500)}
{box(1150, 470, 330, 62, "url(#ggr)", "#22c55e", 10, 1.4)}
{txt(1168, 500, "④ 价值回传更新 Q", 14, "#d3f9dd", 700)}
{txt(1168, 522, "N / W / Q backprop", 11.5, "#7fbf98", 500)}
{box(1500, 470, 318, 62, "url(#gsl)", "#3d4a5c", 10, 1.4)}
{txt(1518, 500, "⑤ 受治理落子", 14, "#cfe0f2", 700)}
{txt(1518, 522, "submit_action + 悔棋", 11.5, "#8fa3bb", 500)}
""")

    content = f"""
<div class="panel gr" style="flex:1">
  <div class="ph"><b><i>▍</i>企业级 AlphaGo 决策架构</b><span>LLM PRIOR + SYMBOLIC WORLD MODEL</span></div>
  <div class="body" style="justify-content:center">{panel}</div>
</div>"""

    return page(
        '6/6 · 终极范式：<span class="n">LLM 提直觉先验 + Ontology 做符号树搜索</span>',
        ["vi|P(s,a) 先验", "cy|符号世界模型", "gr|多步沙盘推演"],
        content,
        "大模型提供直觉先验，OpenOntology 提供零幻觉的符号世界模型与规则裁判，配合蒙特卡洛多步沙盘推演，"
        "让企业 AI 从看一步走一步的盲目试错，跃升为算清全局终局价值的 AlphaGo 级决策系统。",
        5,
        "TK-ONTO-013 · MCTS ON SYMBOLIC WORLD MODEL · v0.3.0",
    )


SCENES = [scene_1, scene_2, scene_3, scene_4, scene_5, scene_6]

NARRATIONS = [
    "在复杂企业调度中，只看眼前一步的贪心决策往往会酿成大祸。比如医院同时下达三张连环急救运单，"
    "其中第二单是特级器官血浆，硬性要求一点五小时内送达且具备二级超低温冷链，一旦断供将面临五万元重罚。",
    "看普通单步贪心智能体的翻车现场：第一步处理普通胰岛素订单时，它贪图王牌快车运费最便宜，"
    "直接把唯一一辆超低温快车派了出去；结果到了第二步处理特级血浆急单时，剩下的车全部违反时效与冷链铁律，"
    "当场触发死局，造成负五万五千四百元的惨重亏损。",
    "AlphaGo 的蒙特卡洛树搜索，本质上就是在确定性的符号世界模型中做多步沙盘推演。"
    "OpenOntology 通过毫秒级内存克隆开辟平行宇宙沙盘，由 Action 前置护栏充当符号规则裁判自动剪枝非法死局，"
    "由纯业务 Function 精算终局价值，并沿搜索树反向传播更新动作价值 Q。",
    "在八十次蒙特卡洛沙盘推演中，尽管第一步派王牌快车的先验概率高达零点五五三，"
    "但树搜索往下推演一步就撞上符号裁判剪枝，期望价值暴跌至负五万五千四百元，仅试探六次就被果断放弃；"
    "而先验最低的长途车路线因为把王牌救命车留给了第二步的特级血浆单，吸引了六十四次访问脱颖而出！",
    "最终，MCTS 锁定全局最优三步序列：第一步派长途车送普通单，第二步把王牌超低温快车精准留给特级血浆急单，"
    "第三步派中型车送疫苗单。三单全部合法履约，净收益正四万四千六百元，比单步贪心策略逆转整整十万元！"
    "整个搜索期间主库零写入零污染，落子后仍支持时光机一键悔棋。",
    "大模型提供直觉先验，OpenOntology 提供零幻觉的符号世界模型与规则裁判，配合蒙特卡洛多步沙盘推演，"
    "让企业 AI 从看一步走一步的盲目试错，跃升为算清全局终局价值的 AlphaGo 级决策系统。",
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
        for i, builder in enumerate(SCENES, start=1):
            html = builder(facts)
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


def synth_audio() -> list[Path]:
    AUDIO.mkdir(parents=True, exist_ok=True)
    outs: list[Path] = []
    for i, text in enumerate(NARRATIONS, start=1):
        out = AUDIO / f"scene_{i:02d}.mp3"
        txt = AUDIO / f"scene_{i:02d}.txt"
        txt.write_text(text, encoding="utf-8")
        ok = False
        # 先写临时文件再原子替换：edge-tts 失败会留下损坏的空 mp3
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
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x04080f,"
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
        "duration_70_160": 70 <= info["duration"] <= 160,
        "size_gt_1mb": info["size_bytes"] > 1024 * 1024,
        "no_b_frames": info["has_b_frames"] == 0,
        "start_pts_zero": info["start_pts"] == 0,
        "start_time_zero": abs(float(info["start_time"])) < 1e-6,
        "frame0_not_black": info["frame0_yavg"] > 10,
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
    reuse = "--reuse" in sys.argv[1:]
    print("=" * 78)
    print("TK-ONTO-013 · AlphaGo MCTS 符号世界模型多步沙盘推演 · 高清视频生成")
    if reuse:
        print("MODE: --reuse（复用 frames/ + audio/，跳过 Playwright 与 TTS）")
    print("=" * 78)
    for tool in (FFMPEG, FFPROBE):
        if not tool.exists():
            raise SystemExit(f"missing tool: {tool}")

    if reuse:
        frames, audios = existing_assets()
    else:
        facts = collect_facts()
        m = facts["mcts"]
        print(f"[data]  simulations={m['simulations']} pruned={m['pruned']} "
              f"forks={m['forks']} sandbox_writes={m['sandbox_writes']} "
              f"greedy_V={facts['greedy']['value']:.0f} mcts_V={m['best_value']:.0f}")
        for label, stat in m["root_actions"].items():
            print(f"[root]  {label:<20} P={stat['prior']:.3f} N={stat['visits']:<3} "
                  f"Q={stat['q']:.0f}")
        frames = render_frames(facts)
        audios = synth_audio()

    segs = build_segments(frames, audios)
    concat_segments(segs)

    info = verify(OUTPUT)
    print("-" * 78)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("-" * 78)
    if not info["all_ok"]:
        print("FAIL: 视频验收未通过")
        return 1
    print(f"PASS: {OUTPUT}")
    print(f"      分辨率 {info['width']}x{info['height']} · {info['vcodec']}/{info['acodec']}"
          f" · {info['fps']}fps · 时长 {info['duration']}s · {info['size_mb']} MB")
    print(f"      has_b_frames={info['has_b_frames']} · start_pts={info['start_pts']}"
          f" · start_time={info['start_time']} · frame0_YAVG={info['frame0_yavg']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
