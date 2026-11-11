#!/usr/bin/env python3
"""TK-ONTO-008 · 钢厂主数据两大死结破局高清演示视频自动生成。

严格并发隔离：本脚本只在 video_mdm/ 目录内读写文件，只真实调用
``examples.steel_mdm_demo``（纯内存 SQLite），不触碰 video/ 目录，
不运行任何涉及 Odoo 的代码或测试。

流程：
1. 真实跑一遍 7 步演示，抓取每一步的运行态、审计记录、ObjectDiff、
   三视角对账数据（不伪造任何数字）。
2. 用 Playwright/Chromium 1920x1080 渲染 6 幕左右分屏 HTML 为 PNG。
3. 用 edge-tts（回退 say）生成 6 段中文配音。
4. 用 ffmpeg 合成 1920x1080 H.264 + AAC yuv420p 的 MP4。
5. 用 ffprobe 校验分辨率 / 音轨 / 时长 / 文件大小。
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

from examples import steel_mdm_demo as steel  # noqa: E402
from open_ontology import InvariantError  # noqa: E402

FRAMES = HERE / "frames"
AUDIO = HERE / "audio"
SEGMENTS = HERE / "segments"
OUTPUT = HERE / "steel_mdm_demo.mp4"

EDGE_TTS = Path("/Users/guci/.local/bin/edge-tts")
FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
FFPROBE = Path("/opt/homebrew/bin/ffprobe")
SAY = Path("/usr/bin/say")
VOICE = "zh-CN-YunxiNeural"
FPS = 30
W, H = 1920, 1080
LEAD_IN = 0.55   # 每幕配音前的静场
TAIL = 0.75      # 每幕收尾留白


# ---------------------------------------------------------------------------
# 1. 真实数据采集（全部来自 examples.steel_mdm_demo 的实跑状态）
# ---------------------------------------------------------------------------
def collect_facts() -> dict[str, Any]:
    demo = steel.build_demo(with_lots=True)
    rt = demo.runtime
    S = steel.STEWARD
    f: dict[str, Any] = {}

    # ---- Scene 1: 存量乱象 -------------------------------------------------
    f["specs"] = [
        {
            "spec_id": s.spec_id,
            "name": s.name,
            "ash": s.ash_max_pct,
            "sulfur": s.sulfur_max_pct,
        }
        for s in rt.query_objects(steel.MaterialSpec, S)
    ]
    f["brands"] = [
        {"brand_id": b.brand_id, "name": b.name}
        for b in rt.query_objects(steel.Manufacturer, S)
    ]
    f["skus"] = [
        {
            "sku_id": s.sku_id,
            "invoice_title": s.invoice_title,
            "brand_id": s.brand_id,
            "price": s.unit_price_ton,
            "ash": s.ash_pct,
            "sulfur": s.sulfur_pct,
            "spec": s.canonical_spec_id,
        }
        for s in rt.query_objects(steel.ContractSKU, S)
    ]

    # ---- Scene 2: overlay 零侵入 + ERP 全量重刷 ---------------------------
    f["map_before"] = {
        s.sku_id: s.canonical_spec_id for s in rt.query_objects(steel.ContractSKU, S)
    }
    snapshot = steel.snapshot_source_rows(rt)
    reindex = rt.load_snapshot(
        {
            "Manufacturer": snapshot["Manufacturer"],
            "ContractSKU": snapshot["ContractSKU"],
        }
    )
    f["reindex"] = {
        k: v for k, v in reindex.items() if isinstance(v, (int, bool, str))
    }
    f["map_after"] = {
        s.sku_id: s.canonical_spec_id for s in rt.query_objects(steel.ContractSKU, S)
    }
    f["overlay_preserved"] = f["map_before"] == f["map_after"]
    f["overlay_rows"] = [
        {"object_id": row["object_id"], "patch": json.loads(row["patch_json"])}
        for row in rt.connection.execute(
            "SELECT object_type, object_id, patch_json FROM ontology_overlay "
            "ORDER BY object_id"
        ).fetchall()
    ]
    f["sku_links"] = {}
    for sku in rt.query_objects(steel.ContractSKU, S):
        f["sku_links"][sku.sku_id] = [
            sp.spec_id
            for sp in rt.get_linked(
                sku, steel.SKU_MAPS_TO_SPEC, steel.MaterialSpec, S
            )
        ]

    # ---- Scene 3: 增量硬护栏 + 新物种审批 ---------------------------------
    rejected: list[dict[str, Any]] = []
    for run_id, params in (
        (
            "REG-UNMAPPED",
            {
                "sku_id": "ERP-SKU-9100",
                "invoice_title": "无归属野码",
                "brand_id": "BRAND-1",
                "unit_price_ton": 1900,
                "ash_pct": 12.0,
                "sulfur_pct": 0.6,
            },
        ),
        (
            "REG-MISMATCH",
            {
                "sku_id": "ERP-SKU-9101",
                "invoice_title": "以次充好挂一级焦",
                "brand_id": "BRAND-1",
                "unit_price_ton": 2000,
                "ash_pct": 13.2,
                "sulfur_pct": 0.8,
                "canonical_spec_id": "SPEC-A",
            },
        ),
    ):
        try:
            rt.submit_action(run_id, "register_contract_sku", params, steel.BUYER)
        except InvariantError as exc:
            rec = rt.get_action_run(run_id) or {}
            rejected.append(
                {
                    "run_id": run_id,
                    "code": exc.code,
                    "message": str(exc),
                    "status": rec.get("status"),
                    "actor": rec.get("actor_id"),
                    "params": rec.get("params"),
                }
            )
    f["rejected"] = rejected

    pending = rt.submit_action(
        "REG-NEW-SPEC",
        "register_contract_sku",
        {
            "sku_id": "ERP-SKU-9102",
            "invoice_title": "河北焦化新规格试采购",
            "brand_id": "BRAND-2",
            "unit_price_ton": 1400,
            "ash_pct": 14.0,
            "sulfur_pct": 1.0,
            "request_new_spec_id": "SPEC-C",
            "request_new_spec_name": "三级冶金焦 (CSR>=45%)",
        },
        steel.BUYER,
    )
    spec_c_before = (
        rt.connection.execute(
            "SELECT 1 FROM ontology_objects "
            "WHERE object_type='MaterialSpec' AND object_id='SPEC-C'"
        ).fetchone()
        is not None
    )
    approved = rt.approve_action("REG-NEW-SPEC", S)
    c_spec = rt.get_object(steel.MaterialSpec, "SPEC-C", S)
    c_sku = rt.get_object(steel.ContractSKU, "ERP-SKU-9102", S)
    f["new_spec"] = {
        "pending_status": pending["status"],
        "spec_c_before": spec_c_before,
        "approved_status": approved["status"],
        "approved_by": approved["approved_by"],
        "spec_name": c_spec.name,
        "ash": c_spec.ash_max_pct,
        "sulfur": c_spec.sulfur_max_pct,
        "sku_id": c_sku.sku_id,
        "canonical": c_sku.canonical_spec_id,
        "link": [
            sp.spec_id
            for sp in rt.get_linked(
                c_sku, steel.SKU_MAPS_TO_SPEC, steel.MaterialSpec, S
            )
        ],
    }
    f["ledger"] = [
        {
            "run_id": r["run_id"],
            "action": r["action_type"],
            "actor": r["actor_id"],
            "status": r["status"],
            "approved_by": r["approved_by"],
            "error_code": r["error_code"],
        }
        for r in rt.list_action_runs()
        if r["run_id"].startswith("REG-")
    ]

    # ---- Scene 4: 高维批次桶 + 黑户散码被卡死 -----------------------------
    f["lots"] = [
        {
            "lot_id": lot.lot_id,
            "sku_id": lot.sku_id,
            "spec_id": lot.spec_id,
            "brand_id": lot.brand_id,
            "initial": lot.initial_tons,
            "remaining": lot.remaining_tons,
            "consumed": lot.consumed_tons,
            "price": lot.unit_price_ton,
        }
        for lot in rt.query_objects(steel.MaterialLot, S)
    ]
    probe = steel.build_demo(with_lots=False)
    try:
        probe.runtime.put_object(
            steel.ContractSKU(
                sku_id="ERP-SKU-9999",
                invoice_title="黑户野码（未归并）",
                brand_id="BRAND-1",
                unit_price_ton=1800,
                ash_pct=12.0,
                sulfur_pct=0.6,
            )
        )
        try:
            probe.runtime.submit_action(
                "RECV-BLACK",
                "receive_material_lot",
                {"lot_id": "LOT-BLACK", "sku_id": "ERP-SKU-9999", "initial_tons": 50},
                steel.BUYER,
            )
            f["black_probe"] = {"blocked": False, "code": None, "message": None}
        except InvariantError as exc:
            f["black_probe"] = {
                "blocked": True,
                "code": exc.code,
                "message": str(exc),
            }
    finally:
        probe.runtime.close()

    # ---- Scene 5: MES 只按厂家报工 -> FIFO 扣桶 -> 三视角对账 -------------
    consume = rt.submit_action(
        "CONSUME-BRAND-1",
        "consume_by_brand_fifo",
        {"brand_id": "BRAND-1", "tons": 150},
        steel.MES,
    )
    f["consume"] = {
        "run_id": consume["run_id"],
        "status": consume["status"],
        "actor": consume["actor_id"],
        "role": consume["actor_role"],
        "diff": consume["object_diff"],
    }
    f["lots_after_consume"] = [
        {
            "lot_id": lot.lot_id,
            "remaining": lot.remaining_tons,
            "consumed": lot.consumed_tons,
        }
        for lot in rt.query_objects(steel.MaterialLot, S)
    ]
    f["reconcile"] = rt.call_function("reconcile_multi_view_ledger", {}, S)
    brand_lots = rt.search_set(steel.MaterialLot, S, {"brand_id": "BRAND-1"})
    f["object_set"] = {
        "brand_lots": list(brand_lots.ids),
        "specs": list(rt.pivot(brand_lots, steel.LOT_HAS_SPEC, S).ids),
        "skus": list(rt.pivot(brand_lots, steel.LOT_BILLED_AS, S).ids),
        "aggregate": [
            {"key": row.key, "sum": row.metrics["sum"]}
            for row in rt.aggregate(
                brand_lots, group_by="spec_id", sum_field="consumed_tons"
            ).rows
        ],
    }

    # ---- Scene 6: 时光机撤回 + 物理写门禁 ---------------------------------
    revert = rt.revert_action("CONSUME-BRAND-1", "CONSUME-BRAND-1-REV", steel.MES)
    f["revert"] = {
        "run_id": revert["run_id"],
        "status": revert["status"],
        "actor": revert["actor_id"],
    }
    f["inventory_after_revert"] = [
        {
            "lot_id": lot.lot_id,
            "remaining": lot.remaining_tons,
            "consumed": lot.consumed_tons,
        }
        for lot in rt.query_objects(steel.MaterialLot, S)
    ]
    gate_error = None
    try:
        rt.connection.execute(
            "INSERT INTO ontology_objects (object_type, object_id, title, "
            "properties_json, version, security_tags_json, content_hash, "
            "updated_at) VALUES ('MaterialSpec','SPEC-HACK',NULL,'{}',1,'[]',"
            "'hack','2026-11-11')"
        )
    except Exception as exc:  # sqlite3.IntegrityError
        gate_error = str(exc)
    f["write_gate"] = {
        "blocked": gate_error is not None,
        "error": gate_error,
        "gate_open": rt.connection.execute(
            "SELECT is_open FROM ontology_write_gate WHERE id = 1"
        ).fetchone()[0],
    }
    f["counts"] = {
        "objects": rt.count_objects(),
        "specs": rt.count_objects("MaterialSpec"),
        "skus": rt.count_objects("ContractSKU"),
        "lots": rt.count_objects("MaterialLot"),
        "runs": len(rt.list_action_runs()),
    }
    rt.close()
    return f


# ---------------------------------------------------------------------------
# 2. HTML / CSS 分镜
# ---------------------------------------------------------------------------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1920px;height:1080px;overflow:hidden}
body{background:#060b12;color:#eaf1fa;font-family:"PingFang SC","Hiragino Sans GB","STHeiti",sans-serif;
 -webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
.stage{position:relative;width:1920px;height:1080px;padding:26px 40px 24px;display:grid;
 grid-template-rows:94px 1fr 168px;gap:16px}
.stage::before{content:"";position:absolute;inset:0;z-index:0;background:
 radial-gradient(1200px 620px at 10% -12%,rgba(56,189,248,.17),transparent 62%),
 radial-gradient(900px 520px at 96% -6%,rgba(99,102,241,.15),transparent 60%),
 radial-gradient(1100px 700px at 52% 118%,rgba(245,165,36,.09),transparent 62%),
 linear-gradient(#060b12,#04080e)}
.stage::after{content:"";position:absolute;inset:0;z-index:0;background-image:
 linear-gradient(rgba(120,170,230,.06) 1px,transparent 1px),
 linear-gradient(90deg,rgba(120,170,230,.06) 1px,transparent 1px);
 background-size:64px 64px;
 -webkit-mask-image:radial-gradient(circle at 50% 42%,#000 26%,transparent 76%);
 mask-image:radial-gradient(circle at 50% 42%,#000 26%,transparent 76%)}
.stage>*{position:relative;z-index:1}

header{display:flex;align-items:center;justify-content:space-between;gap:26px}
.brand{display:flex;align-items:center;gap:14px;flex:0 0 auto}
.mark{width:52px;height:52px;border-radius:14px;display:grid;place-items:center;font-size:25px;
 font-weight:800;color:#04121f;background:linear-gradient(135deg,#38bdf8,#6366f1);
 box-shadow:0 10px 26px rgba(56,189,248,.32)}
.brand .nm{font-size:23px;font-weight:700;letter-spacing:.6px}
.brand .sb{font-size:12.5px;color:#8fa3bb;letter-spacing:2.4px;margin-top:3px}
h1{font-size:38px;font-weight:800;letter-spacing:1.2px;text-align:center;flex:1 1 auto}
h1 .n{color:#38bdf8}
.badges{display:flex;gap:9px;flex:0 0 auto}
.chip{font-size:13px;padding:7px 13px;border-radius:999px;border:1px solid rgba(120,160,210,.24);
 color:#b9cbe0;background:rgba(255,255,255,.03);letter-spacing:.7px;white-space:nowrap}
.chip.ok{border-color:rgba(34,197,94,.5);color:#86efac;background:rgba(34,197,94,.1)}
.chip.warn{border-color:rgba(245,165,36,.5);color:#fcd34d;background:rgba(245,165,36,.1)}
.chip.cy{border-color:rgba(56,189,248,.5);color:#a5e4ff;background:rgba(56,189,248,.1)}

.main{display:grid;grid-template-columns:1fr 1fr;gap:24px;min-height:0}
.panel{position:relative;border-radius:18px;padding:16px 20px 14px;min-height:0;overflow:hidden;
 background:linear-gradient(165deg,rgba(19,32,50,.95),rgba(9,17,28,.95));
 border:1px solid rgba(120,160,210,.18);display:flex;flex-direction:column;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 18px 44px rgba(0,0,0,.38)}
.panel::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;
 background:linear-gradient(90deg,#38bdf8,rgba(99,102,241,.55),transparent)}
.panel.r::before{background:linear-gradient(90deg,#f5a524,rgba(239,68,68,.5),transparent)}
.ph{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
 margin-bottom:11px;padding-bottom:9px;border-bottom:1px dashed rgba(120,160,210,.22)}
.ph b{font-size:18px;letter-spacing:.9px;font-weight:700}
.ph b i{color:#38bdf8;font-style:normal}
.panel.r .ph b i{color:#f5a524}
.ph span{font-size:12.5px;color:#7f93ab;letter-spacing:1.1px;white-space:nowrap}
.body{flex:1;min-height:0;display:flex;flex-direction:column;gap:11px;
 justify-content:space-evenly}

.card{background:rgba(255,255,255,.028);border:1px solid rgba(120,160,210,.16);
 border-left:3px solid rgba(120,160,210,.45);border-radius:11px;padding:10px 13px}
.card.bad{border-left-color:#ef4444;background:rgba(239,68,68,.06)}
.card.good{border-left-color:#22c55e;background:rgba(34,197,94,.06)}
.card.warnc{border-left-color:#f5a524;background:rgba(245,165,36,.06)}
.card.cy{border-left-color:#38bdf8;background:rgba(56,189,248,.06)}
.ct{font-size:14.5px;font-weight:700;letter-spacing:.7px;display:flex;align-items:center;
 justify-content:space-between;gap:10px;margin-bottom:6px}
.ct small{font-weight:500;font-size:12px;color:#8fa3bb;letter-spacing:.5px}
.mono{font-family:"SF Mono",Menlo,monospace;font-size:12.5px;color:#9fb4cc;letter-spacing:.2px}
.dim{color:#8fa3bb}
.hl{color:#38bdf8}
.hl2{color:#fcd34d}
.gr{color:#86efac}
.rd{color:#fca5a5}

.flow{display:flex;align-items:stretch;gap:8px}
.node{flex:1;border-radius:10px;padding:9px 10px;background:rgba(255,255,255,.035);
 border:1px solid rgba(120,160,210,.2);text-align:center}
.node .nt{font-size:12px;color:#8fa3bb;letter-spacing:1px;margin-bottom:4px}
.node .nv{font-size:16px;font-weight:700}
.node.hot{border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.09)}
.node.cool{border-color:rgba(56,189,248,.45);background:rgba(56,189,248,.08)}
.arw{display:grid;place-items:center;color:#5f7a99;font-size:20px;padding:0 2px}

.tbl{width:100%;border-collapse:collapse;font-size:15px}
.tbl th{font-size:12px;font-weight:600;color:#8fa3bb;letter-spacing:.9px;text-align:left;
 padding:5px 7px;border-bottom:1px solid rgba(120,160,210,.25);white-space:nowrap}
.tbl td{padding:6px 7px;border-bottom:1px solid rgba(120,160,210,.11);white-space:nowrap}
.tbl tr:last-child td{border-bottom:none}
.tbl td.num{text-align:right;font-variant-numeric:tabular-nums}
.tbl tr.hotrow td{background:rgba(239,68,68,.08)}
.tbl tr.goodrow td{background:rgba(34,197,94,.08)}

.tag{font-size:11.5px;padding:2px 8px;border-radius:6px;letter-spacing:.6px;font-weight:700;
 display:inline-block;white-space:nowrap}
.tag.red{color:#fecaca;background:rgba(239,68,68,.2);border:1px solid rgba(239,68,68,.45)}
.tag.green{color:#bbf7d0;background:rgba(34,197,94,.18);border:1px solid rgba(34,197,94,.42)}
.tag.amber{color:#fde68a;background:rgba(245,165,36,.18);border:1px solid rgba(245,165,36,.42)}
.tag.blue{color:#bae6fd;background:rgba(56,189,248,.16);border:1px solid rgba(56,189,248,.4)}

.bigstat{display:flex;align-items:baseline;gap:10px}
.bigstat .v{font-size:46px;font-weight:800;line-height:1;letter-spacing:-1px}
.bigstat .u{font-size:15px;color:#8fa3bb;letter-spacing:1px}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:14.5px;padding:3.5px 0}
.kv span:first-child{color:#8fa3bb}
.kv b{font-variant-numeric:tabular-nums}

.sub{display:grid;grid-template-columns:1fr auto;align-items:center;gap:28px;padding:16px 34px;
 border-radius:18px;border:1px solid rgba(120,160,210,.2);border-left:6px solid #38bdf8;
 background:linear-gradient(90deg,rgba(9,16,26,.97),rgba(14,26,42,.92));
 box-shadow:0 14px 44px rgba(0,0,0,.5)}
.sub p{font-weight:600;letter-spacing:.7px;color:#f2f7ff;line-height:1.52}
.s1 p{font-size:38px}.s2 p{font-size:33px}.s3 p{font-size:29px}
.dots{display:flex;gap:7px;align-items:center}
.dots i{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,.16);display:block}
.dots i.on{width:26px;border-radius:5px;background:#38bdf8;box-shadow:0 0 14px rgba(56,189,248,.9)}
.dots em{font-size:13px;color:#7f93ab;font-style:normal;letter-spacing:1px;margin-left:8px}
"""


def head(title: str, badges: list[str]) -> str:
    chips = "".join(
        f'<span class="chip {cls}">{txt}</span>' for cls, txt in
        [(b.split("|")[0], b.split("|")[1]) for b in badges]
    )
    return f"""
<header>
  <div class="brand">
    <div class="mark">◈</div>
    <div><div class="nm">OpenOntology</div><div class="sb">STEEL MDM BREAKTHROUGH</div></div>
  </div>
  <h1>{title}</h1>
  <div class="badges">{chips}</div>
</header>"""


def sub_bar(text: str, idx: int) -> str:
    size = "s1" if len(text) <= 62 else ("s2" if len(text) <= 96 else "s3")
    dots = "".join(
        f'<i class="{"on" if i == idx else ""}"></i>' for i in range(6)
    )
    return f"""
<div class="sub {size}">
  <p>{text}</p>
  <div class="dots">{dots}<em>{idx + 1}/6</em></div>
</div>"""


def page(title: str, badges: list[str], left: str, right: str,
         subtitle: str, idx: int) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="stage">
{head(title, badges)}
<div class="main">
  <section class="panel">
    <div class="ph"><b><i>▣</i> 左屏 · OpenOntology 动作与治理沙盘</b><span>GOVERNED ACTION SANDBOX</span></div>
    <div class="body">{left}</div>
  </section>
  <section class="panel r">
    <div class="ph"><b><i>◈</i> 右屏 · 高维拓扑与三视角实测数据</b><span>LIVE ONTOLOGY GRAPH</span></div>
    <div class="body">{right}</div>
  </section>
</div>
{sub_bar(subtitle, idx)}
</div></body></html>"""


def money(v: float) -> str:
    return f"¥{int(v):,}"


# ------------------------------- Scene 1 -----------------------------------
def scene_1(f: dict[str, Any]) -> str:
    sku_rows = ""
    for s in f["skus"]:
        spec = s["spec"] or "—"
        sku_rows += (
            f"<tr><td class='mono'>{s['sku_id']}</td>"
            f"<td>{s['invoice_title']}</td>"
            f"<td>{s['brand_id']}</td>"
            f"<td class='num'>{s['price']}</td>"
            f"<td class='num'>{s['ash']}</td>"
            f"<td class='num'>{s['sulfur']}</td>"
            f"<td><span class='tag blue'>{spec}</span></td></tr>"
        )
    left = f"""
<div class="card cy">
  <div class="ct">有损降维投影：ERP 按规格 → 老 MES 按厂家 <small>LOSSY PROJECTION</small></div>
  <div class="flow" style="margin-top:8px">
    <div class="node cool"><div class="nt">老 ERP · 采购口径</div><div class="nv">SPEC-A / SPEC-B</div>
      <div class="mono" style="margin-top:5px">4 个合同散码</div></div>
    <div class="arw">→</div>
    <div class="node hot"><div class="nt">投影损失</div><div class="nv rd">厂家维度</div>
      <div class="mono" style="margin-top:5px">规格边被压平</div></div>
    <div class="arw">→</div>
    <div class="node"><div class="nt">老 MES · 生产口径</div><div class="nv">BRAND-1 / BRAND-2</div>
      <div class="mono" style="margin-top:5px">只认厂家</div></div>
  </div>
</div>
<div class="card bad">
  <div class="ct">死结一 · 散码通胀 <small>INVOICE-DRIVEN SKU SPRAWL</small></div>
  <div class="kv"><span>存量混乱散码</span><b class="rd">4 个（ERP-SKU-9001 ~ 9004）</b></div>
  <div class="kv"><span>真实物理规格</span><b class="gr">仅 2 个（SPEC-A / SPEC-B）</b></div>
  <div class="kv"><span>财务历史账</span><b class="rd">挂着散码，不敢合并</b></div>
</div>
<div class="card bad">
  <div class="ct">死结二 · 多对多断链 <small>MANY-TO-MANY</small></div>
  <div class="kv"><span>山西焦化 BRAND-1</span><b class="hl2">同时供 SPEC-A 一级焦 与 SPEC-B 二级焦</b></div>
  <div class="kv"><span>MES 回传 ERP</span><b class="rd">按厂家报工 → 规格不可还原</b></div>
</div>
<div class="card">
  <div class="ct">本次实跑基座 <small>REAL RUNTIME</small></div>
  <div class="kv"><span>OntologyRuntime</span><b class="mono">SQLite 内存库 · write_gate 默认关闭</b></div>
  <div class="kv"><span>对象 / 审计</span><b>{f['counts']['objects']} 个对象 · 全量动作留痕</b></div>
</div>"""
    right = f"""
<table class="tbl">
  <tr><th>ERP 散码</th><th>发票名目（采购原文）</th><th>厂家</th>
      <th class="num">单价</th><th class="num">灰分%</th><th class="num">硫%</th><th>归并</th></tr>
  {sku_rows}
</table>
<div class="card warnc" style="margin-top:2px">
  <div class="ct">同一厂家跨两个规格：规格 ←→ 厂家 多对多 <small>NOT A TREE</small></div>
  <div class="flow" style="margin-top:6px">
    <div class="node"><div class="nt">SPEC-A 一级焦</div><div class="nv">CSR≥65%</div></div>
    <div class="node cool"><div class="nt">BRAND-1</div><div class="nv">山西焦化</div></div>
    <div class="node"><div class="nt">SPEC-B 二级焦</div><div class="nv">CSR≥55%</div></div>
  </div>
  <div class="mono" style="margin-top:7px">BRAND-1 → {{SPEC-A, SPEC-B}} ：一维编码无法同时表达</div>
</div>
<div class="card">
  <div class="ct">规格主数据（Ontology 完全自有） <small>OWNED</small></div>
  <div class="kv"><span>SPEC-A</span><b>{f['specs'][0]['name']} · 灰≤{f['specs'][0]['ash']} 硫≤{f['specs'][0]['sulfur']}</b></div>
  <div class="kv"><span>SPEC-B</span><b>{f['specs'][1]['name']} · 灰≤{f['specs'][1]['ash']} 硫≤{f['specs'][1]['sulfur']}</b></div>
  <div class="kv"><span>厂家主数据</span><b>{' / '.join(b['name'] + ' ' + b['brand_id'] for b in f['brands'])}</b></div>
</div>"""
    narration = (
        "钢铁企业主数据治理有两个公认死结：一是采购为签合同开票随意建散码，"
        "挂着历史财务账不敢合并；二是采购按规格管、生产按厂家管，"
        "山西焦化既供一级焦又供二级焦，MES 消耗后回传 ERP 时多对多断链，成本全部算歪。"
    )
    return page("1/6 · 钢厂主数据两大绝望死结", ["warn|存量 4 散码", "warn|多对多断链"],
                left, right, narration, 0)


# ------------------------------- Scene 2 -----------------------------------
def scene_2(f: dict[str, Any]) -> str:
    rows = ""
    for s in f["skus"]:
        before = f["map_before"].get(s["sku_id"])
        after = f["map_after"].get(s["sku_id"])
        same = before == after
        rows += (
            f"<tr><td class='mono'>{s['sku_id']}</td>"
            f"<td>{s['invoice_title']}</td>"
            f"<td><span class='tag blue'>{before}</span></td>"
            f"<td class='dim'>─ ─ ─▶</td>"
            f"<td><span class='tag {'green' if same else 'red'}'>{after}</span></td>"
            f"<td>{'<span class=tag green>保留</span>' if same else '<span class=tag red>丢失</span>'}</td></tr>"
        )
    overlays = "".join(
        f"<div class='kv'><span class='mono'>{r['object_id']}</span>"
        f"<b class='mono gr'>canonical_spec_id={r['patch'].get('canonical_spec_id')}</b></div>"
        for r in f["overlay_rows"]
    )
    left = f"""
<div class="card cy">
  <div class="ct">治理动作 <small>map_legacy_sku_to_spec · affects_source=False</small></div>
  <div class="flow" style="margin-top:7px">
    <div class="node"><div class="nt">① 提交动作</div><div class="nv">4 个散码</div></div>
    <div class="arw">→</div>
    <div class="node cool"><div class="nt">② 只写自有字段</div><div class="nv">overlay 补丁</div></div>
    <div class="arw">→</div>
    <div class="node"><div class="nt">③ ERP 全量重刷</div><div class="nv">load_snapshot</div></div>
    <div class="arw">→</div>
    <div class="node cool"><div class="nt">④ 校验存活</div><div class="nv gr">preserved</div></div>
  </div>
</div>
<div class="card good">
  <div class="ct">零侵入结论 <small>SOURCE UNTOUCHED</small></div>
  <div class="bigstat"><div class="v gr">True</div><div class="u">overlay preserved after full reindex</div></div>
  <div class="kv" style="margin-top:6px"><span>重刷结果</span>
    <b class="mono">objects={f['reindex'].get('objects')} · overlays_reapplied={f['reindex'].get('overlays_reapplied')}</b></div>
  <div class="kv"><span>owned 归并边</span><b class="mono">由 overlay 重建（快照不供给 links）</b></div>
  <div class="kv"><span>老 ERP 底表</span><b class="gr">未改一颗螺丝钉（affects_source=False）</b></div>
</div>
<div class="card">
  <div class="ct">ontology_overlay 实存行 <small>{len(f['overlay_rows'])} PATCHES</small></div>
  {overlays}
</div>"""
    right = f"""
<div class="ct" style="font-size:15px;font-weight:700;letter-spacing:.8px">
  ERP 重刷前后归并关系对照 <small class="dim" style="font-weight:500">
  before → after（load_snapshot 真实执行）</small></div>
<table class="tbl">
  <tr><th>散码</th><th>发票名目</th><th>重刷前</th><th></th><th>重刷后</th><th>结果</th></tr>
  {rows}
</table>
<div class="card good" style="margin-top:2px">
  <div class="ct">归并边（owned link） <small>sku_maps_to_spec</small></div>
  <div class="mono" style="line-height:1.9">
    {('<br>'.join(f"{k} ──▶ {v[0] if v else '—'}" for k, v in f['sku_links'].items()))}
  </div>
</div>
<div class="card warnc">
  <div class="ct">为什么财务敢用 <small>WHY IT SURVIVES</small></div>
  <div class="kv"><span>源系统字段</span><b>invoice_title / price / ash / sulfur 原样保留</b></div>
  <div class="kv"><span>本体自有字段</span><b class="hl">canonical_spec_id / mapped_by 独立存储</b></div>
</div>"""
    narration = (
        "面对存量散码，OpenOntology 不改老 ERP 底表一颗螺丝钉，"
        "用 overlay 补丁层将四个散码寄生归并到标准物理规格。"
        "哪怕老 ERP 每天全量重刷底表，归并关系依然百分之百完好存活。"
    )
    return page("2/6 · 治存量：overlay 零侵入寄生归并",
                ["ok|overlay preserved", "cy|affects_source=False"],
                left, right, narration, 1)


# ------------------------------- Scene 3 -----------------------------------
def scene_3(f: dict[str, Any]) -> str:
    r1, r2 = f["rejected"][0], f["rejected"][1]
    ns = f["new_spec"]
    ledger_rows = ""
    for r in f["ledger"]:
        st = r["status"]
        cls = {"REJECTED": "red", "COMMITTED": "green", "PENDING_APPROVAL": "amber"}.get(st, "blue")
        ledger_rows += (
            f"<tr><td class='mono'>{r['run_id']}</td>"
            f"<td class='mono'>{r['action']}</td>"
            f"<td>{r['actor']}</td>"
            f"<td><span class='tag {cls}'>{st}</span></td>"
            f"<td class='mono rd'>{r['error_code'] or '—'}</td>"
            f"<td class='mono'>{r['approved_by'] or '—'}</td></tr>"
        )
    left = f"""
<div class="card bad">
  <div class="ct">① 无归属野码 <small>REG-UNMAPPED · REJECTED</small></div>
  <div class="kv"><span>采购提交</span><b class="mono">ERP-SKU-9100「{r1['params']['invoice_title']}」</b></div>
  <div class="kv"><span>硬护栏拒绝码</span><b class="tag red">{r1['code']}</b></div>
  <div class="mono" style="margin-top:3px">{r1['message'][:58]}…</div>
</div>
<div class="card bad">
  <div class="ct">② 以次充好挂一级焦 <small>REG-MISMATCH · REJECTED</small></div>
  <div class="kv"><span>灰分 / 硫分</span><b>13.2% / 0.8% → 挂 SPEC-A（限 12.5% / 0.7%）</b></div>
  <div class="kv"><span>硬护栏拒绝码</span><b class="tag red">{r2['code']}</b></div>
</div>
<div class="card good">
  <div class="ct">③ 新物理品种 <small>REG-NEW-SPEC · 审批流</small></div>
  <div class="kv"><span>提交后状态</span><b class="tag amber">{ns['pending_status']}</b></div>
  <div class="kv"><span>审批前 SPEC-C 存在</span><b class="rd">{ns['spec_c_before']}</b></div>
  <div class="kv"><span>管理员签批</span><b class="gr">{ns['approved_by']} → {ns['approved_status']}</b></div>
  <div class="kv"><span>落地结果</span><b>{ns['spec_name']} · 灰≤{ns['ash']} 硫≤{ns['sulfur']}</b></div>
</div>
<div class="card cy">
  <div class="ct">三道门禁语义 <small>GATE SEMANTICS</small></div>
  <div class="mono" style="line-height:1.85">必须绑规格 → 必须过化验 → 新物种必须人签批</div>
</div>"""
    right = f"""
<div class="ct" style="font-size:15px;font-weight:700;letter-spacing:.8px">
  ontology_action_runs 审计账本 <small class="dim" style="font-weight:500">真实落库记录</small></div>
<table class="tbl">
  <tr><th>run_id</th><th>action_type</th><th>actor</th><th>status</th><th>error_code</th><th>approved_by</th></tr>
  {ledger_rows}
</table>
<div class="card good" style="margin-top:2px">
  <div class="ct">审批通过后自动补建 <small>post_approval HOOK</small></div>
  <div class="flow" style="margin-top:6px">
    <div class="node"><div class="nt">MaterialSpec</div><div class="nv">SPEC-C</div></div>
    <div class="arw">＋</div>
    <div class="node"><div class="nt">ContractSKU</div><div class="nv">{ns['sku_id']}</div></div>
    <div class="arw">→</div>
    <div class="node cool"><div class="nt">link</div><div class="nv">{ns['link'][0]}</div></div>
  </div>
  <div class="mono" style="margin-top:7px">canonical={ns['canonical']} · 主数据管理员签批后生效</div>
</div>
<div class="card">
  <div class="ct">拒绝也留痕 <small>REFUSAL AUDIT</small></div>
  <div class="kv"><span>被拒 run</span><b class="mono rd">{', '.join(r['run_id'] for r in f['rejected'])}</b></div>
  <div class="kv"><span>写入的对象变更</span><b class="gr">0 条（事务整体回滚）</b></div>
</div>"""
    narration = (
        "面对增量建码，采购新建开票名目必须通过 register_contract_sku 门禁。"
        "不绑规格的野码和灰分超标的错配被当场红牌拦截并记入审计；"
        "申请全厂首次采购的新物理品种则自动挂起，由主数据管理员签批后生效。"
    )
    return page("3/6 · 治增量：Action 硬护栏与新物种审批",
                ["warn|2 红牌", "ok|1 审批生效"],
                left, right, narration, 2)


# ------------------------------- Scene 4 -----------------------------------
def scene_4(f: dict[str, Any]) -> str:
    lot_rows = ""
    for lot in f["lots"]:
        lot_rows += (
            f"<tr><td class='mono'>{lot['lot_id']}</td>"
            f"<td class='num'>{lot['initial']}</td>"
            f"<td><span class='tag blue'>{lot['spec_id']}</span></td>"
            f"<td><span class='tag amber'>{lot['brand_id']}</span></td>"
            f"<td class='mono'>{lot['sku_id']}</td>"
            f"<td class='num'>{lot['price']}</td>"
            f"<td class='num'>{lot['remaining']}</td></tr>"
        )
    total_tons = sum(lot["initial"] for lot in f["lots"])
    bp = f["black_probe"]
    left = f"""
<div class="card cy">
  <div class="ct">入库截留点 <small>receive_material_lot · T1</small></div>
  <div class="flow" style="margin-top:7px">
    <div class="node"><div class="nt">老 ERP 散码</div><div class="nv">发票口径</div></div>
    <div class="arw">→</div>
    <div class="node cool"><div class="nt">入库口截留</div><div class="nv hl">MaterialLot</div></div>
    <div class="arw">→</div>
    <div class="node"><div class="nt">老 MES</div><div class="nv">厂家口径</div></div>
  </div>
  <div class="mono" style="margin-top:8px">三条正交边同时落库：规格 / 厂家 / 原始发票散码</div>
</div>
<div class="card bad">
  <div class="ct">黑户散码卡位 <small>SKU_NOT_GOVERNED · 实跑验证</small></div>
  <div class="kv"><span>未归并散码</span><b class="mono">ERP-SKU-9999「黑户野码」</b></div>
  <div class="kv"><span>尝试入库 50 吨</span><b class="tag red">{'BLOCKED' if bp['blocked'] else 'PASSED'}</b></div>
  <div class="kv"><span>拒绝码</span><b class="mono rd">{bp['code'] or '—'}</b></div>
  <div class="mono" style="margin-top:3px">{(bp['message'] or '')[:56]}…</div>
</div>
<div class="card good">
  <div class="ct">批次桶总量 <small>INVENTORY BUCKETS</small></div>
  <div class="bigstat"><div class="v">{total_tons}</div><div class="u">吨 · 3 个高维桶 · 3 条正交边/桶</div></div>
</div>
<div class="card">
  <div class="ct">为什么 MES 丢信息还能找回 <small>WHY IT WORKS</small></div>
  <div class="kv"><span>MES 只看见</span><b>brand_id（山西焦化）</b></div>
  <div class="kv"><span>本体偷偷记住</span><b class="hl">spec_id + sku_id + unit_price</b></div>
</div>"""
    right = f"""
<div class="ct" style="font-size:15px;font-weight:700;letter-spacing:.8px">
  MaterialLot 高维批次桶 <small class="dim" style="font-weight:500">lot_has_spec / lot_produced_by / lot_billed_as</small></div>
<table class="tbl">
  <tr><th>批次</th><th class="num">入库吨</th><th>规格边</th><th>厂家边</th>
      <th>发票边</th><th class="num">单价</th><th class="num">剩余</th></tr>
  {lot_rows}
</table>
<div class="card cy" style="margin-top:2px">
  <div class="ct">LOT-01 的三条正交边 <small>ORTHOGONAL EDGES</small></div>
  <div class="flow" style="margin-top:6px">
    <div class="node"><div class="nt">lot_has_spec</div><div class="nv">SPEC-A</div></div>
    <div class="node"><div class="nt">lot_produced_by</div><div class="nv">BRAND-1</div></div>
    <div class="node"><div class="nt">lot_billed_as</div><div class="nv">SKU-9001</div></div>
  </div>
</div>
<div class="card">
  <div class="ct">同一厂家的两个批次落在不同规格 <small>MULTI-TO-MANY RESOLVED</small></div>
  <div class="kv"><span>LOT-01</span><b>100 吨 · SPEC-A · BRAND-1 · ¥2,000</b></div>
  <div class="kv"><span>LOT-02</span><b>80 吨 · SPEC-B · BRAND-1 · ¥1,600</b></div>
  <div class="kv"><span>LOT-03</span><b>120 吨 · SPEC-A · BRAND-2 · ¥1,950</b></div>
</div>"""
    narration = (
        "为什么老 MES 把规格弄丢了还能找回来？秘密在入库口。"
        "当原料入库时，Ontology 在中间截留完整信息，构建高维批次桶 MaterialLot，"
        "同时锁死物理规格、生产厂家和原始发票散码三条正交关系。"
    )
    return page("4/6 · T1 入库截留：构建高维批次桶 MaterialLot",
                ["cy|3 个批次桶", "warn|黑户卡位锁"],
                left, right, narration, 3)


# ------------------------------- Scene 5 -----------------------------------
def scene_5(f: dict[str, Any]) -> str:
    rec = f["reconcile"]
    brand = rec["by_brand"][0]
    spec_rows = "".join(
        f"<tr><td><span class='tag blue'>{r['spec_id']}</span></td>"
        f"<td class='num'>{r['tons']} 吨</td>"
        f"<td class='num'>{money(r['cost'])}</td></tr>"
        for r in rec["by_spec"]
    )
    sku_rows = "".join(
        f"<tr><td class='mono'>{r['sku_id']}</td>"
        f"<td class='num'>{r['tons']} 吨</td>"
        f"<td class='num'>{money(r['cost'])}</td></tr>"
        for r in rec["by_sku"]
    )
    fifo_rows = ""
    for lot in f["lots_after_consume"]:
        init = next(l["initial"] for l in f["lots"] if l["lot_id"] == lot["lot_id"])
        pct = int(lot["consumed"] / init * 100) if init else 0
        color = "#ef4444" if pct == 100 else ("#f5a524" if pct else "rgba(120,160,210,.3)")
        fifo_rows += f"""
<div style="margin-bottom:9px">
  <div class="kv" style="padding:0 0 4px"><span class="mono">{lot['lot_id']}</span>
    <b>扣减 {lot['consumed']} 吨 · 剩余 {lot['remaining']} 吨 / 入库 {init} 吨</b></div>
  <div style="height:9px;border-radius:5px;background:rgba(255,255,255,.07);overflow:hidden">
    <div style="height:100%;width:{pct}%;background:{color}"></div>
  </div>
</div>"""
    left = f"""
<div class="card cy">
  <div class="ct">MES 报工（老习惯） <small>consume_by_brand_fifo</small></div>
  <div class="mono" style="font-size:15px;line-height:1.9">
    brand_id = <span class="hl2">"BRAND-1"</span> ，tons = <span class="hl2">150</span><br>
    actor = {f['consume']['actor']} · role = {f['consume']['role']}<br>
    run = {f['consume']['run_id']} · status = <span class="gr">{f['consume']['status']}</span>
  </div>
</div>
<div class="card">
  <div class="ct">本体自动 FIFO 扣桶 <small>AUTO DEDUCTION</small></div>
  {fifo_rows}
  <div class="mono">100 吨 LOT-01 + 50 吨 LOT-02，LOT-03 未动</div>
</div>
<div class="card good">
  <div class="ct">ObjectDiff 真实变更 <small>OBJECT DIFF</small></div>
  <div class="mono" style="line-height:1.8">{json.dumps(f['consume']['diff'], ensure_ascii=False)[:230]}…</div>
</div>"""
    right = f"""
<div class="card" style="border-left-color:#f5a524">
  <div class="ct">① 成本科 · 按厂家品牌 <small>COST</small></div>
  <div class="kv"><span>{brand['brand_name']} {brand['brand_id']}</span>
    <b class="hl2">{brand['tons']} 吨 · {money(brand['cost'])}</b></div>
</div>
<div class="card" style="border-left-color:#38bdf8">
  <div class="ct">② 采购 / ERP 报产 · 按物理规格 <small>PROCUREMENT</small></div>
  <table class="tbl"><tr><th>规格</th><th class="num">吨</th><th class="num">成本</th></tr>{spec_rows}</table>
</div>
<div class="card" style="border-left-color:#a78bfa">
  <div class="ct">③ 财务税务 · 按原始发票散码 <small>FINANCE</small></div>
  <table class="tbl"><tr><th>散码</th><th class="num">吨</th><th class="num">成本</th></tr>{sku_rows}</table>
</div>
<div class="card good">
  <div class="ct">三视角恒等 <small>RECONCILIATION</small></div>
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div class="bigstat"><div class="v gr">balanced</div><div class="u">= True</div></div>
    <div style="text-align:right">
      <div class="kv" style="justify-content:flex-end;gap:8px"><span>总吨数</span><b class="hl">{rec['totals']['tons']} 吨</b></div>
      <div class="kv" style="justify-content:flex-end;gap:8px"><span>总成本</span><b class="hl">{money(rec['totals']['cost'])}</b></div>
    </div>
  </div>
  <div class="mono" style="margin-top:6px">brand={rec['views']['brand_tons']} · spec={rec['views']['spec_tons']} · sku={rec['views']['sku_tons']} 吨　
  cost {rec['views']['brand_cost']} / {rec['views']['spec_cost']} / {rec['views']['sku_cost']}</div>
</div>"""
    narration = (
        "高炉车间照常只按厂家报工：今日消耗山西焦化一百五十吨。"
        "Ontology 按先进先出自动扣减高维批次桶，顺着三条关系边一键生成成本科、采购报产、财务发票三张对账单，"
        "一百吨一级焦加五十吨二级焦无损还原，三方总吨数与二十八万元总成本分毫不差！"
    )
    return page("5/6 · T2 报工还原：三视角无损对账恒等",
                ["ok|balanced=True", "cy|150 吨 / ¥280,000"],
                left, right, narration, 4)


# ------------------------------- Scene 6 -----------------------------------
def scene_6(f: dict[str, Any]) -> str:
    inv_rows = "".join(
        f"<tr><td class='mono'>{r['lot_id']}</td><td class='num'>{r['remaining']} 吨</td>"
        f"<td class='num'>{r['consumed']} 吨</td><td><span class='tag green'>已恢复</span></td></tr>"
        for r in f["inventory_after_revert"]
    )
    left = f"""
<div class="card good">
  <div class="ct">时光机撤回投料 <small>revert_action · {f['revert']['run_id']}</small></div>
  <div class="kv"><span>撤回动作</span><b class="mono">CONSUME-BRAND-1 → {f['revert']['status']}</b></div>
  <div class="kv"><span>操作者</span><b class="mono">{f['revert']['actor']}</b></div>
  <table class="tbl" style="margin-top:6px">
    <tr><th>批次</th><th class="num">库存剩余</th><th class="num">已消耗</th><th>状态</th></tr>
    {inv_rows}
  </table>
</div>
<div class="card bad">
  <div class="ct">第一把锁 · AI 断网与触发器锁 <small>ontology_write_gate</small></div>
  <div class="kv"><span>绕过 Runtime 直写 ontology_objects</span>
    <b class="tag red">{'BLOCKED' if f['write_gate']['blocked'] else 'PASSED'}</b></div>
  <div class="mono" style="margin-top:4px;line-height:1.7">{(f['write_gate']['error'] or '')[:96]}…</div>
  <div class="kv" style="margin-top:5px"><span>门禁当前状态</span>
    <b class="mono gr">is_open = {f['write_gate']['gate_open']}（默认关闭）</b></div>
</div>
<div class="card">
  <div class="ct">最终对象盘点 <small>FINAL COUNTS</small></div>
  <div class="kv"><span>对象总数 / 审计 run 数</span><b>{f['counts']['objects']} / {f['counts']['runs']}</b></div>
  <div class="kv"><span>规格 · 散码 · 批次桶</span>
    <b>{f['counts']['specs']} · {f['counts']['skus']} · {f['counts']['lots']}</b></div>
</div>"""
    right = f"""
<div class="card bad">
  <div class="ct">① AI 断网与触发器锁 <small>NO BYPASS WRITE</small></div>
  <div class="kv"><span>物理触发器</span><b>ontology_objects / links / overlay 全表守卫</b></div>
  <div class="kv"><span>结果</span><b class="rd">GOVERNANCE_VIOLATION，任何直写被 ABORT</b></div>
</div>
<div class="card bad">
  <div class="ct">② 下游黑户卡位锁 <small>SKU_NOT_GOVERNED</small></div>
  <div class="kv"><span>老系统偷建野码</span><b>可以建，但进不了入库口</b></div>
  <div class="kv"><span>实跑拒绝码</span><b class="mono rd">{f['black_probe']['code']}</b></div>
</div>
<div class="card bad">
  <div class="ct">③ 老系统收缴人类 Create 权限 <small>PERMISSION RECLAIM</small></div>
  <div class="kv"><span>建码入口</span><b>只保留 register_contract_sku 一个受治理动作</b></div>
  <div class="kv"><span>新物种</span><b>必须 mdm_steward 签批（已实测）</b></div>
</div>
<div class="card good">
  <div class="ct">结论 <small>REPLACE TRADITIONAL MDM</small></div>
  <div class="bigstat"><div class="v gr">3 天</div><div class="u">算清每一分真实成本 · 零传统 MDM 采购</div></div>
  <div class="mono" style="margin-top:6px">overlay 治存量 · 护栏治增量 · 批次桶治断链 · 三锁防旁路</div>
</div>"""
    narration = (
        "配合 AI 触发器门禁、下游黑户入库卡位锁与老系统建码权限收缴，"
        "任何人和 AI 都无法绕过 Ontology 制造脏数据。"
        "不买笨重的传统 MDM 软件，不逼业务部门打架，三天算清钢厂每一分真实成本。"
    )
    return page("6/6 · 防旁路三把物理锁：彻底淘汰传统 MDM",
                ["ok|三锁齐备", "cy|revert 已验证"],
                left, right, narration, 5)


SCENES = [scene_1, scene_2, scene_3, scene_4, scene_5, scene_6]

NARRATIONS = [
    "钢铁企业主数据治理有两个公认死结：一是采购为签合同开票随意建散码，"
    "挂着历史财务账不敢合并；二是采购按规格管、生产按厂家管，"
    "山西焦化既供一级焦又供二级焦，MES 消耗后回传 ERP 时多对多断链，成本全部算歪。",
    "面对存量散码，OpenOntology 不改老 ERP 底表一颗螺丝钉，"
    "用 overlay 补丁层将四个散码寄生归并到标准物理规格。"
    "哪怕老 ERP 每天全量重刷底表，归并关系依然百分之百完好存活。",
    "面对增量建码，采购新建开票名目必须通过 register_contract_sku 门禁。"
    "不绑规格的野码和灰分超标的错配被当场红牌拦截并记入审计；"
    "申请全厂首次采购的新物理品种则自动挂起，由主数据管理员签批后生效。",
    "为什么老 MES 把规格弄丢了还能找回来？秘密在入库口。"
    "当原料入库时，Ontology 在中间截留完整信息，构建高维批次桶 MaterialLot，"
    "同时锁死物理规格、生产厂家和原始发票散码三条正交关系。",
    "高炉车间照常只按厂家报工：今日消耗山西焦化一百五十吨。"
    "Ontology 按先进先出自动扣减高维批次桶，顺着三条关系边一键生成成本科、采购报产、财务发票三张对账单，"
    "一百吨一级焦加五十吨二级焦无损还原，三方总吨数与二十八万元总成本分毫不差！",
    "配合 AI 触发器门禁、下游黑户入库卡位锁与老系统建码权限收缴，"
    "任何人和 AI 都无法绕过 Ontology 制造脏数据。"
    "不买笨重的传统 MDM 软件，不逼业务部门打架，三天算清钢厂每一分真实成本。",
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
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        for i, builder in enumerate(SCENES, start=1):
            html = builder(facts)
            out = FRAMES / f"scene_{i:02d}.png"
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(320)
            page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": W, "height": H})
            paths.append(out)
            print(f"[frame] {out.name}  {out.stat().st_size / 1024:.0f} KB")
        browser.close()
    return paths


def synth_audio() -> list[Path]:
    AUDIO.mkdir(parents=True, exist_ok=True)
    outs: list[Path] = []
    for i, text in enumerate(NARRATIONS, start=1):
        out = AUDIO / f"scene_{i:02d}.mp3"
        txt = AUDIO / f"scene_{i:02d}.txt"
        txt.write_text(text, encoding="utf-8")
        ok = False
        if EDGE_TTS.exists():
            cmd = [
                str(EDGE_TTS), "--voice", VOICE, "--file", str(txt),
                "--rate=+6%", "--write-media", str(out), "--no-progress",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            ok = res.returncode == 0 and out.exists() and out.stat().st_size > 4000
            if not ok:
                print(f"[tts] edge-tts failed scene {i}: {res.stderr[-300:]}")
        if not ok:
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


def probe_duration(path: Path) -> float:
    res = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())


def build_segments(frames: list[Path], audios: list[Path]) -> list[Path]:
    SEGMENTS.mkdir(parents=True, exist_ok=True)
    segs: list[Path] = []
    for i, (frame, audio) in enumerate(zip(frames, audios), start=1):
        dur = probe_duration(audio) + LEAD_IN + TAIL
        seg = SEGMENTS / f"seg_{i:02d}.mp4"
        # 不加 fade：第 0 帧必须是有内容的画面（QuickTime 黑屏根因之一）
        vf = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x060b12,"
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
         "-show_entries", "stream=start_pts,start_time", "-of", "json", str(path)],
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
    start_pts, start_time = probe_start(path)
    info = {
        "path": str(path),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "vcodec": video["codec_name"],
        "pix_fmt": video.get("pix_fmt"),
        "fps": video.get("r_frame_rate"),
        "acodec": audio["codec_name"] if audio else None,
        "sample_rate": int(audio["sample_rate"]) if audio else None,
        "channels": int(audio["channels"]) if audio else None,
        "duration": round(float(data["format"]["duration"]), 2),
        "size_bytes": int(data["format"]["size"]),
        "size_mb": round(int(data["format"]["size"]) / 1024 / 1024, 2),
        "start_pts": start_pts,
        "start_time": start_time,
        "frame0_mean": frame0_mean(path),
    }
    checks = {
        "resolution_1920x1080": info["width"] == W and info["height"] == H,
        "h264": info["vcodec"] == "h264",
        "yuv420p": info["pix_fmt"] == "yuv420p",
        "has_audio": info["acodec"] is not None,
        "aac": info["acodec"] == "aac",
        "duration_60_140": 60 <= info["duration"] <= 140,
        "start_pts_zero": info["start_pts"] == 0,
        "start_time_zero": abs(float(info["start_time"])) < 1e-6,
        "frame0_not_black": info["frame0_mean"] > 20,
    }
    info["checks"] = checks
    info["all_ok"] = all(checks.values())
    return info


def main() -> int:
    print("=" * 74)
    print("TK-ONTO-008 · 钢厂主数据破局演示视频 · 自动生成")
    print("=" * 74)
    for tool in (FFMPEG, FFPROBE):
        if not tool.exists():
            raise SystemExit(f"missing tool: {tool}")

    reuse = "--reuse" in sys.argv[1:]
    if reuse:
        frames = sorted(FRAMES.glob("scene_*.png"))
        audios = sorted(AUDIO.glob("scene_*.mp3"))
        if len(frames) != len(audios) or not frames:
            raise SystemExit(
                f"--reuse 需要成对的已有素材，实际 frames={len(frames)} audio={len(audios)}"
            )
        print(f"[reuse] 复用已有 {len(frames)} 帧 PNG + {len(audios)} 段 MP3，跳过渲染与 TTS")
    else:
        facts = collect_facts()
        print(f"[data]  对象 {facts['counts']['objects']} · 审计 run {facts['counts']['runs']}"
              f" · 三视角 balanced={facts['reconcile']['balanced']}"
              f" · overlay preserved={facts['overlay_preserved']}")
        frames = render_frames(facts)
        audios = synth_audio()

    segs = build_segments(frames, audios)
    concat_segments(segs)

    info = verify(OUTPUT)
    print("-" * 74)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("-" * 74)
    if not info["all_ok"]:
        print("FAIL: 视频验收未通过")
        return 1
    print(f"PASS: {OUTPUT}")
    print(f"      分辨率 {info['width']}x{info['height']} · {info['vcodec']}/{info['acodec']}"
          f" · 时长 {info['duration']}s · {info['size_mb']} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
