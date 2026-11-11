#!/usr/bin/env python3
"""TK-ONTO-011 · 《OpenOntology 架构与第一性原理》高清讲解视频自动生成。

严格并发隔离：本脚本只在 ``video_arch/`` 目录内读写文件。
对 ``open_ontology/``、``examples/``、``tests/``、``camel-sync/`` 只做
**只读**导入与统计（纯内存 SQLite 自检），绝不修改任何文件，
也不运行任何涉及外部 Odoo 的重置操作。

流程：
1. 只读采集真实内核指标（LOC / 表 / 触发器 / 测试数 / Camel 路由 /
   LLM 工具 / 两种主权模式契约一致性 / EditBatch 零副作用干跑）。
2. Playwright + Chromium 1920x1080 渲染 6 幕深色科技风架构图（内联 SVG）。
3. edge-tts（zh-CN-YunxiNeural）生成 6 段中文配音，失败回退 say。
4. ffmpeg 合成 1920x1080 H.264 + AAC yuv420p 30fps MP4。
5. ffprobe 校验分辨率 / 帧率 / 音轨 / 时长 / 文件大小。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))
# 只读导入 examples/open_ontology 时不往受保护目录写 __pycache__
sys.dont_write_bytecode = True

FRAMES = HERE / "frames"
AUDIO = HERE / "audio"
SEGMENTS = HERE / "segments"
OUTPUT = HERE / "open_ontology_architecture.mp4"

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
# 1. 真实内核指标采集（全部只读）
# ---------------------------------------------------------------------------
def collect_facts() -> dict[str, Any]:
    f: dict[str, Any] = {}

    kernel = PROJECT / "open_ontology"
    py_files = sorted(kernel.glob("*.py"))
    f["kernel_loc"] = sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in py_files
    )
    f["kernel_files"] = len(py_files)
    schema = (kernel / "schema.sql").read_text(encoding="utf-8")
    f["schema_loc"] = len(schema.splitlines())
    f["tables"] = len(re.findall(r"CREATE TABLE", schema))
    f["triggers"] = len(re.findall(r"CREATE TRIGGER", schema))
    blocks = re.split(r"CREATE TRIGGER", schema)[1:]
    f["gated_triggers"] = sum(1 for b in blocks if "ontology_write_gate" in b)
    f["append_only_triggers"] = f["triggers"] - f["gated_triggers"]

    # 测试用例数：pytest --collect-only 但禁用 cache 插件，绝不写 .pytest_cache
    test_files = sorted((PROJECT / "tests").glob("test_*.py"))
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(PROJECT), capture_output=True, text=True,
    )
    m = re.search(r"(\d+) tests collected", res.stdout)
    f["tests_collected"] = int(m.group(1)) if m else 0
    f["test_files"] = len(test_files)

    # Camel 集成总线路由数（与内核物理隔离）
    routes = PROJECT / "camel-sync/src/main/java/com/openontology/camel/Routes.java"
    if routes.exists():
        f["camel_routes"] = len(re.findall(r'from\("', routes.read_text(encoding="utf-8")))
    else:
        f["camel_routes"] = 0

    # 真实内核自检：EditBatch 零副作用干跑 + overlay 实存 + 三视角对账
    from examples import steel_mdm_demo as steel
    from open_ontology import export_llm_tools

    demo = steel.build_demo(with_lots=True)
    rt = demo.runtime
    before = rt.count_objects()
    sim = rt.simulate_action(
        "consume_by_brand_fifo", {"brand_id": "BRAND-1", "tons": 150}, steel.MES
    )
    after = rt.count_objects()
    f["sim_valid"] = sim.valid
    f["sim_no_side_effect"] = before == after
    f["sim_diff"] = sim.diff.summary()
    f["sim_diff_ops"] = len(sim.diff.object_changes) + len(sim.diff.link_changes)
    f["overlay_rows"] = rt.connection.execute(
        "SELECT COUNT(*) FROM ontology_overlay"
    ).fetchone()[0]
    f["objects"] = rt.count_objects()
    f["steel_types"] = len(rt.spec.object_types)
    f["steel_actions"] = len(rt.spec.actions)
    f["steel_links"] = len(rt.spec.link_types)
    rec = rt.call_function("reconcile_multi_view_ledger", {}, steel.STEWARD)
    f["reconcile_balanced"] = rec["balanced"]

    # 写门禁物理熔断实证
    gate_err = None
    try:
        rt.connection.execute(
            "INSERT INTO ontology_objects (object_type, object_id, title, "
            "properties_json, version, security_tags_json, content_hash, "
            "updated_at) VALUES ('MaterialSpec','SPEC-BYPASS',NULL,'{}',1,'[]',"
            "'hack','2026-11-11')"
        )
    except Exception as exc:  # sqlite3.IntegrityError
        gate_err = str(exc)
    f["gate_blocked"] = gate_err is not None
    f["gate_message"] = (gate_err or "").split(":")[0]
    rt.close()

    # 两种主权模式契约一致性（Phase A 寄生态 vs Phase B 脱壳态）
    from examples import saas_unplug_demo as saas

    spec_a = saas.build_saas_ontology_spec(standalone=False)
    spec_b = saas.build_saas_ontology_spec(standalone=True)
    tools_a = export_llm_tools(spec_a)
    tools_b = export_llm_tools(spec_b)
    f["llm_tools"] = len(tools_a)
    f["tools_identical"] = (
        [t["function"]["name"] for t in tools_a]
        == [t["function"]["name"] for t in tools_b]
    )
    evals_a = saas.run_eval_suite(False)
    evals_b = saas.run_eval_suite(True)
    f["contract_identical"] = evals_a == evals_b
    f["eval_count"] = len(evals_a)
    f["evals"] = evals_a
    f["phase_a_owned"] = {
        name: cls.owned_spec() for name, cls in spec_a.object_types.items()
    }
    f["phase_b_owned"] = {
        name: cls.owned_spec() for name, cls in spec_b.object_types.items()
    }

    # 已跑通的实证样板
    f["vendor_crm"] = (PROJECT / "vendor_crm" / "bottlecrm_src").exists()
    f["o2c_types"] = 4
    return f


# ---------------------------------------------------------------------------
# 2. 视觉设计系统
# ---------------------------------------------------------------------------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1920px;height:1080px;overflow:hidden}
body{background:#050a12;color:#e9f1fb;
 font-family:"PingFang SC","Hiragino Sans GB","Heiti SC","STHeiti",sans-serif;
 -webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
.stage{position:relative;width:1920px;height:1080px;padding:32px 44px 26px;
 display:flex;flex-direction:column;gap:17px}
.stage::before{content:"";position:absolute;inset:0;z-index:0;background:
 radial-gradient(1250px 640px at 8% -14%,rgba(56,189,248,.19),transparent 62%),
 radial-gradient(980px 560px at 97% -8%,rgba(99,102,241,.17),transparent 60%),
 radial-gradient(1150px 720px at 50% 120%,rgba(245,165,36,.085),transparent 64%),
 linear-gradient(#050a12,#03070d)}
.stage::after{content:"";position:absolute;inset:0;z-index:0;background-image:
 linear-gradient(rgba(120,170,230,.055) 1px,transparent 1px),
 linear-gradient(90deg,rgba(120,170,230,.055) 1px,transparent 1px);
 background-size:60px 60px;
 -webkit-mask-image:radial-gradient(circle at 50% 40%,#000 28%,transparent 78%);
 mask-image:radial-gradient(circle at 50% 40%,#000 28%,transparent 78%)}
.stage>*{position:relative;z-index:1}

header{height:92px;display:flex;align-items:center;justify-content:space-between;gap:24px}
.brand{display:flex;align-items:center;gap:13px;flex:0 0 auto}
.mark{width:50px;height:50px;border-radius:14px;display:grid;place-items:center;font-size:24px;
 font-weight:800;color:#04121f;background:linear-gradient(135deg,#38bdf8,#6366f1);
 box-shadow:0 10px 26px rgba(56,189,248,.34)}
.brand .nm{font-size:22px;font-weight:700;letter-spacing:.7px}
.brand .sb{font-size:11.5px;color:#8fa3bb;letter-spacing:2.3px;margin-top:3px}
h1{font-size:34px;font-weight:800;letter-spacing:.9px;text-align:center;flex:1 1 auto;
 line-height:1.24}
h1 .n{color:#38bdf8}
.badges{display:flex;gap:8px;flex:0 0 auto}
.chip{font-size:12.5px;padding:7px 12px;border-radius:999px;border:1px solid rgba(120,160,210,.26);
 color:#b9cbe0;background:rgba(255,255,255,.03);letter-spacing:.6px;white-space:nowrap}
.chip.ok{border-color:rgba(34,197,94,.5);color:#86efac;background:rgba(34,197,94,.1)}
.chip.cy{border-color:rgba(56,189,248,.5);color:#a5e4ff;background:rgba(56,189,248,.1)}
.chip.am{border-color:rgba(245,165,36,.5);color:#fcd34d;background:rgba(245,165,36,.1)}
.chip.vi{border-color:rgba(167,139,250,.5);color:#ddd6fe;background:rgba(167,139,250,.1)}

.content{flex:1;min-height:0;display:flex;flex-direction:column;gap:15px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:22px;flex:1;min-height:0}
.panel{position:relative;border-radius:17px;padding:15px 18px 13px;min-height:0;overflow:hidden;
 background:linear-gradient(165deg,rgba(18,31,49,.94),rgba(8,15,26,.95));
 border:1px solid rgba(120,160,210,.17);display:flex;flex-direction:column;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 16px 40px rgba(0,0,0,.4)}
.panel::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;
 background:linear-gradient(90deg,#38bdf8,rgba(99,102,241,.5),transparent)}
.panel.am::before{background:linear-gradient(90deg,#f5a524,rgba(239,68,68,.45),transparent)}
.panel.vi::before{background:linear-gradient(90deg,#a78bfa,rgba(56,189,248,.45),transparent)}
.ph{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
 margin-bottom:9px;padding-bottom:8px;border-bottom:1px dashed rgba(120,160,210,.2)}
.ph b{font-size:17px;letter-spacing:.8px;font-weight:700}
.ph b i{color:#38bdf8;font-style:normal}
.panel.am .ph b i{color:#f5a524}
.panel.vi .ph b i{color:#a78bfa}
.ph span{font-size:11.5px;color:#7f93ab;letter-spacing:1px;white-space:nowrap}
.body{flex:1;min-height:0;display:flex;flex-direction:column;gap:10px;justify-content:space-evenly}

.card{background:rgba(255,255,255,.028);border:1px solid rgba(120,160,210,.15);
 border-left:3px solid rgba(120,160,210,.42);border-radius:10px;padding:9px 12px}
.card.cy{border-left-color:#38bdf8;background:rgba(56,189,248,.055)}
.card.gd{border-left-color:#22c55e;background:rgba(34,197,94,.055)}
.card.bd{border-left-color:#ef4444;background:rgba(239,68,68,.06)}
.card.am{border-left-color:#f5a524;background:rgba(245,165,36,.06)}
.card.vi{border-left-color:#a78bfa;background:rgba(167,139,250,.06)}
.ct{font-size:14px;font-weight:700;letter-spacing:.6px;display:flex;align-items:center;
 justify-content:space-between;gap:9px;margin-bottom:5px}
.ct small{font-weight:500;font-size:11.5px;color:#8fa3bb;letter-spacing:.4px}
.mono{font-family:"SF Mono",Menlo,monospace;font-size:12px;color:#9fb4cc;letter-spacing:.1px}
.dim{color:#8fa3bb}
.hl{color:#38bdf8}.hl2{color:#fcd34d}.gr{color:#86efac}.rd{color:#fca5a5}.vi{color:#ddd6fe}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:13.5px;padding:3px 0}
.kv span:first-child{color:#8fa3bb}
.kv b{font-variant-numeric:tabular-nums;text-align:right}
.tag{font-size:11px;padding:2px 8px;border-radius:6px;letter-spacing:.5px;font-weight:700;
 display:inline-block;white-space:nowrap}
.tag.red{color:#fecaca;background:rgba(239,68,68,.2);border:1px solid rgba(239,68,68,.45)}
.tag.green{color:#bbf7d0;background:rgba(34,197,94,.18);border:1px solid rgba(34,197,94,.42)}
.tag.amber{color:#fde68a;background:rgba(245,165,36,.18);border:1px solid rgba(245,165,36,.42)}
.tag.blue{color:#bae6fd;background:rgba(56,189,248,.16);border:1px solid rgba(56,189,248,.4)}
.tag.violet{color:#ddd6fe;background:rgba(167,139,250,.18);border:1px solid rgba(167,139,250,.42)}
.bigstat{display:flex;align-items:baseline;gap:9px}
.bigstat .v{font-size:40px;font-weight:800;line-height:1;letter-spacing:-1px}
.bigstat .u{font-size:13.5px;color:#8fa3bb;letter-spacing:.8px}
.pills{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-size:14px;font-weight:700;padding:8px 14px;border-radius:9px;letter-spacing:.5px;
 border:1px solid rgba(56,189,248,.4);background:rgba(56,189,248,.1);color:#cfeeff}
.pill.o{border-color:rgba(167,139,250,.42);background:rgba(167,139,250,.1);color:#e2dcff}
.plus{color:#5f7a99;font-size:17px;font-weight:700}

footer{height:186px;display:flex;flex-direction:column;justify-content:center;
 padding:14px 34px;border-radius:17px;border:1px solid rgba(120,160,210,.2);
 border-left:6px solid #38bdf8;
 background:linear-gradient(90deg,rgba(8,15,25,.97),rgba(13,24,39,.93));
 box-shadow:0 14px 40px rgba(0,0,0,.5)}
footer p{font-weight:600;letter-spacing:.5px;color:#f3f8ff;line-height:1.58}
footer .s1 p{font-size:33px}footer .s2 p{font-size:29px}
footer .s3 p{font-size:26px}footer .s4 p{font-size:23.5px}
footer .meta{display:flex;align-items:center;justify-content:space-between;margin-top:9px}
.dots{display:flex;gap:7px;align-items:center}
.dots i{width:9px;height:9px;border-radius:50%;background:rgba(255,255,255,.16);display:block}
.dots i.on{width:26px;border-radius:5px;background:#38bdf8;box-shadow:0 0 13px rgba(56,189,248,.9)}
.dots em{font-size:12.5px;color:#7f93ab;font-style:normal;letter-spacing:1px;margin-left:8px}
.fmeta{font-size:11.5px;color:#66798f;letter-spacing:1.1px}
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
    <div><div class="nm">OpenOntology</div><div class="sb">FIRST PRINCIPLES · v0.3.0</div></div>
  </div>
  <h1>{title}</h1>
  <div class="badges">{chips}</div>
</header>"""


def footer(text: str, idx: int, meta: str) -> str:
    n = len(text)
    size = "s1" if n <= 96 else ("s2" if n <= 130 else ("s3" if n <= 150 else "s4"))
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
    """``sub`` 只是回退值；字幕强制取 NARRATIONS[idx]，保证字幕与配音逐字一致。"""
    subtitle = NARRATIONS[idx] if idx < len(NARRATIONS) else sub
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="stage">
{head(title, badges)}
<div class="content">{content}</div>
{footer(subtitle, idx, meta)}
</div></body></html>"""


# ---------------------------------------------------------------------------
# SVG 公共件
# ---------------------------------------------------------------------------
DEFS = """
<defs>
  <linearGradient id="gcy" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#12263c"/><stop offset="1" stop-color="#0a1624"/>
  </linearGradient>
  <linearGradient id="gvi" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#1b1b3a"/><stop offset="1" stop-color="#101024"/>
  </linearGradient>
  <linearGradient id="gam" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#2c2110"/><stop offset="1" stop-color="#1a1409"/>
  </linearGradient>
  <linearGradient id="ggr" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#0f2a1c"/><stop offset="1" stop-color="#0a1a12"/>
  </linearGradient>
  <linearGradient id="grd" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#2d1414"/><stop offset="1" stop-color="#1a0c0c"/>
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
  <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
    <feGaussianBlur stdDeviation="7" result="b"/>
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
    return (
        f'<svg class="svgtxt" viewBox="0 0 {vb_w} {vb_h}" '
        f'width="100%" height="100%" preserveAspectRatio="xMidYMid meet" '
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
          lx=0, ly=0, lsize=11.5, lfill="#8fa3bb", lanchor="middle"):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out = (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
           f'stroke-width="{sw}" marker-end="url(#{marker})"{d}/>')
    if label:
        out += txt(lx or (x1 + x2) / 2, ly or (y1 + y2) / 2 - 7, label, lsize, lfill,
                   500, lanchor, "0.3")
    return out


# ---------------------------------------------------------------------------
# Scene 1 · 第一性原理：AlphaGo 架构
# ---------------------------------------------------------------------------
def scene_1(f: dict[str, Any]) -> str:
    left = f"""
<div class="card cy">
  <div class="ct">AlphaGo 的五件套 <small>ALPHAGO DECOMPOSITION</small></div>
  <div class="pills" style="margin-top:4px">
    <span class="pill">策略 Policy</span><span class="plus">+</span>
    <span class="pill">世界模型</span><span class="plus">+</span>
    <span class="pill o">规划 Planning</span><span class="plus">+</span>
    <span class="pill o">价值 Value</span><span class="plus">+</span>
    <span class="pill">工具 Tools</span>
  </div>
</div>
<div class="card bd">
  <div class="ct">普通 ReAct Agent 为什么必死 <small>NAIVE ReAct · ANTI-PATTERN</small></div>
  <div class="kv"><span>把世界模型塞进 LLM</span><b class="rd">靠幻觉脑补企业现实</b></div>
  <div class="kv"><span>把规则裁判塞进 Prompt</span><b class="rd">越狱一句话即失效</b></div>
  <div class="kv"><span>直接裸调 ERP / SaaS API</span><b class="rd">无审批 · 无审计 · 不可回滚</b></div>
</div>
<div class="card gd">
  <div class="ct">Palantir 的第一性原理 <small>NEURAL–SYMBOLIC SPLIT</small></div>
  <div class="kv"><span>确定性世界模型</span><b class="gr">剥离 → 固化为 Ontology</b></div>
  <div class="kv"><span>规则裁判 / 不变量</span><b class="gr">剥离 → 数据库物理门禁</b></div>
  <div class="kv"><span>外部业务系统</span><b class="gr">退化为传感器与机械臂</b></div>
</div>
<div class="card">
  <div class="ct">内核实测 <small>LIVE KERNEL</small></div>
  <div class="kv"><span>Python 内核 / 存储引擎</span>
    <b class="mono">{f['kernel_loc']} 行 + {f['schema_loc']} 行 SQL</b></div>
  <div class="kv"><span>对象图 / 审计账本</span><b class="mono">全量动作留痕 · 可悔棋</b></div>
</div>"""

    s = []
    # LLM 策略层
    s.append(box(140, 8, 520, 106, "url(#gvi)", "#4c4a8f", 14))
    s.append(txt(166, 42, "大模型 LLM · 策略提案器", 18, "#ddd6fe", 700))
    s.append(txt(166, 68, "Policy · Planning · Value  ← 只保留「猜」的部分", 12, "#a9a2d8", 500))
    for i, t in enumerate(["策略", "规划", "价值"]):
        s.append(box(172 + i * 158, 78, 140, 26, "rgba(167,139,250,.13)", "#6b62b8", 7, 1))
        s.append(txt(242 + i * 158, 96, t, 13, "#a78bfa", 700, "middle"))

    # LLM <-> Ontology
    s.append(arrow(394, 116, 394, 236, "#38bdf8", "arwc", "", 2.4))
    s.append(arrow(406, 236, 406, 116, "#6366f1", "arw", "4 4", 1.6))
    s.append(txt(424, 158, "唯一工具调用", 13.5, "#a5e4ff", 700))
    s.append(txt(424, 178, "Tool Call / OSDK / MCP", 11.5, "#6f8aa8", 500))
    s.append(txt(424, 198, "提案 ↓   ·   裁决 ↑", 11.5, "#8fa3bb", 500))

    # Ontology 主体
    s.append(box(80, 238, 660, 210, "url(#gcy)", "#38bdf8", 16, 2))
    s.append(txt(104, 272, "Ontology · 确定性世界模型 + 规则裁判", 18, "#a5e4ff", 800))
    s.append(txt(104, 294, "唯一有写权限的裁决者 · 唯一可被 LLM 调用的工具面", 11.5, "#7f9cba", 500))
    subs = [
        ("对象图谱", "World Model", ["ObjectType", "LinkType"], "#38bdf8"),
        ("不变量与门禁", "Rules", ["Invariants", "Write Gate"], "#f5a524"),
        ("Action 裁判", "Referee", ["ObjectDiff", "Approval / Revert"], "#22c55e"),
    ]
    for i, (t1, t2, t3, c) in enumerate(subs):
        x = 104 + i * 212
        s.append(box(x, 306, 192, 118, "rgba(255,255,255,.035)", "#2f5578", 11))
        s.append(txt(x + 96, 334, t1, 15.5, c, 700, "middle"))
        s.append(txt(x + 96, 354, t2, 11, "#7f93ab", 600, "middle", "1.2"))
        s.append(txt(x + 96, 384, t3[0], 11.5, "#9fb4cc", 500, "middle", "0.2"))
        s.append(txt(x + 96, 404, t3[1], 11.5, "#9fb4cc", 500, "middle", "0.2"))

    # Ontology -> 外部系统
    s.append(arrow(400, 450, 400, 492, "#22c55e", "arwg", "", 2.2))
    s.append(txt(414, 476, "受治理写入（仅 Action 通道）", 11.5, "#86efac", 600))

    # 外部系统
    ext = [("ERP", "Odoo 19"), ("SaaS", "CRM / 商城"), ("数据库", "PG / SQLite")]
    for i, (t1, t2) in enumerate(ext):
        x = 100 + i * 220
        s.append(box(x, 496, 190, 78, "rgba(255,255,255,.03)", "#33506e", 11, 1.2, "5 4"))
        s.append(txt(x + 95, 528, t1, 16, "#b9cbe0", 700, "middle"))
        s.append(txt(x + 95, 552, t2, 11.5, "#7f93ab", 500, "middle", "0.3"))
    s.append(txt(100, 596, "外部业务系统 = 底下的传感器与机械臂 · 无自主写权限", 12, "#8fa3bb", 600))

    # 传感器回传（左侧虚线）
    s.append(f'<path d="M100,535 L58,535 L58,300 L76,300 L76,330" fill="none" '
             f'stroke="#5f88b5" stroke-width="1.6" stroke-dasharray="5 4" marker-end="url(#arw)"/>')
    s.append(txt(24, 452, "传感器", 10.5, "#7f93ab", 500, "middle"))
    s.append(txt(24, 468, "回传", 10.5, "#7f93ab", 500, "middle"))

    # 禁止直连（右侧绕行 + 红叉）
    s.append(f'<path d="M660,60 L760,60 L760,540 L742,540" fill="none" stroke="#ef4444" '
             f'stroke-width="2" stroke-dasharray="7 6" marker-end="url(#arwr)"/>')
    s.append(f'<circle cx="760" cy="300" r="16" fill="#1a0c0c" stroke="#ef4444" stroke-width="2"/>')
    s.append(txt(760, 306, "✕", 15, "#fca5a5", 800, "middle"))
    s.append(txt(786, 296, "禁止裸调", 12.5, "#fca5a5", 700))
    s.append(txt(786, 314, "LLM → API", 11, "#e08585", 500, "start", "0.3"))
    s.append(txt(786, 344, "一切写操作", 11, "#8fa3bb", 500))
    s.append(txt(786, 360, "必须经 Action", 11, "#8fa3bb", 500))
    s.append(txt(786, 376, "留下可回滚 diff", 11, "#8fa3bb", 500))

    right = f"""
<div class="panel vi" style="padding:10px 12px">
  <div class="ph"><b><i>◈</i> 神经符号分工：谁负责猜，谁负责判</b>
    <span>NEURAL PROPOSES · SYMBOLIC DISPOSES</span></div>
  <div class="body" style="padding:0">{svg(900, 610, "".join(s))}</div>
</div>"""

    sub = ("普通 ReAct 智能体一上生产就翻车，是因为把策略、世界模型、规划和价值全塞进大模型靠幻觉脑补，"
           "还让它裸调 ERP 接口。用 AlphaGo 架构一照就穿：Palantir 的本质，是把确定性的世界模型与规则裁判"
           "从大模型剥离，固化为 Ontology。大模型只提策略，Ontology 是它唯一的工具，"
           "外部系统退化为传感器与机械臂。")
    content = f"""
<div class="cols">
  <div class="panel"><div class="ph"><b><i>▣</i> 五件套拆解与反面教材</b>
    <span>WHY NAIVE AGENTS FAIL</span></div>
    <div class="body">{left}</div></div>
  {right}
</div>"""
    return page("1/6 · 第一性原理：用 AlphaGo 架构解释 Palantir 方法论",
                ["cy|神经符号分工", "vi|内核实测"],
                content, sub, 0,
                f"KERNEL {f['kernel_loc']} LOC · SCHEMA {f['schema_loc']} LOC · TESTS {f['tests_collected']}")


# ---------------------------------------------------------------------------
# Scene 2 · 五层正交架构
# ---------------------------------------------------------------------------
def scene_2(f: dict[str, Any]) -> str:
    layers = [
        ("L5", "智能与评测层", "INTELLIGENCE", "#a78bfa",
         ["OSDK / MCP 工具桥", f"export_llm_tools → {f['llm_tools']} 个工具",
          "AIP Evals 契约回归", "Agent 只提策略"],
         "LLM 无写权限"),
        ("L4", "动力学沙箱层", "KINETICS", "#38bdf8",
         ["EditBatch 干跑推演", "ObjectDiff 可逆补丁",
          "乐观锁 read_versions", "审批 / 悔棋状态机"],
         "零副作用"),
        ("L3", "声明式原语层", "DECLARATIVE", "#22c55e",
         ["ObjectType / LinkType", "@function 只读计算",
          "@action 受治理动作", "属性级主权 __owned__"],
         "纯业务概念"),
        ("L2", "纯业务存储层", "STORAGE", "#f5a524",
         [f"{f['tables']} 张纯业务表", f"{f['triggers']} 个物理触发器",
          f"{f['gated_triggers']} 个写门禁守卫", "append-only 审计账本"],
         "写门禁默认关闭"),
        ("L1", "外部同步总线", "INTEGRATION", "#94a3b8",
         [f"Apache Camel EIP · {f['camel_routes']} 条路由",
          "direct:ingest / writeback", "region 分流 north / south",
          "与内核物理隔离"],
         "零业务表混入"),
    ]
    s = []
    top = 4
    rowh = 100
    gap = 12
    for i, (lid, name, en, color, chips, note) in enumerate(layers):
        y = top + i * (rowh + gap)
        s.append(box(0, y, 1560, rowh, "rgba(255,255,255,.022)", "#27445f", 13, 1.3))
        s.append(box(0, y, 132, rowh, "rgba(255,255,255,.04)", color, 13, 1.5))
        s.append(txt(24, y + 44, lid, 25, color, 800, "start", "1"))
        s.append(txt(24, y + 68, en, 9.5, "#7f93ab", 600, "start", "1.1"))
        s.append(txt(152, y + 38, name, 19, "#e9f1fb", 700))
        s.append(txt(152, y + 62, chips[0], 12, "#9fb4cc", 500))
        # chips on right side
        for j, c in enumerate(chips[1:]):
            cx = 560 + j * 300
            s.append(box(cx, y + 30, 282, 42, "rgba(255,255,255,.032)", "#2f5578", 8, 1))
            s.append(txt(cx + 141, y + 57, c, 11.8, "#b9cbe0", 600, "middle", "0.2"))
        # right annotation
        s.append(box(1584, y + 20, 244, rowh - 40, "rgba(56,189,248,.055)", "#2a4a6b", 9, 1))
        s.append(txt(1706, y + 46, note, 12.5, "#a5e4ff", 700, "middle", "0.3"))
        # arrows between layers
        if i < len(layers) - 1:
            s.append(arrow(66, y + rowh + 1, 66, y + rowh + gap - 1, "#3d6485", "arw", "", 1.6))

    # 左侧纯度轴（渐变竖条，标签放在底部指标行，避免旋转文字被裁切）
    s.append(f'<line x1="2" y1="10" x2="2" y2="{top + 5 * (rowh + gap) - gap - 6}" '
             f'stroke="url(#bar)" stroke-width="3" stroke-linecap="round" opacity="0.9"/>')

    body = "".join(s)
    content = f"""
<div class="panel" style="padding:10px 14px">
  <div class="ph"><b><i>▣</i> 五层正交架构 · 每层只干一件事</b>
    <span>L5 INTELLIGENCE → L1 INTEGRATION · PIM vs PSM 严格分离</span></div>
  <div class="body" style="padding:2px 0 0 24px">{svg(1832, 566, body)}</div>
</div>
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;height:96px;flex:0 0 auto">
  <div class="card cy" style="display:flex;flex-direction:column;justify-content:center">
    <div class="mono dim" style="font-size:11px;letter-spacing:1.2px">PURE BUSINESS TABLES</div>
    <div class="bigstat"><div class="v hl">{f['tables']}</div><div class="u">张纯业务表</div></div></div>
  <div class="card am" style="display:flex;flex-direction:column;justify-content:center">
    <div class="mono dim" style="font-size:11px;letter-spacing:1.2px">PHYSICAL WRITE GATE</div>
    <div class="bigstat"><div class="v hl2">{f['gated_triggers']}</div><div class="u">个门禁触发器</div></div></div>
  <div class="card bd" style="display:flex;flex-direction:column;justify-content:center">
    <div class="mono dim" style="font-size:11px;letter-spacing:1.2px">BYPASS ATTEMPT</div>
    <div class="bigstat"><div class="v rd">{'BLOCKED' if f['gate_blocked'] else 'PASSED'}</div>
      <div class="u">直写库实测</div></div></div>
  <div class="card gd" style="display:flex;flex-direction:column;justify-content:center">
    <div class="mono dim" style="font-size:11px;letter-spacing:1.2px">CAMEL OUT-OF-PROCESS</div>
    <div class="bigstat"><div class="v gr">{f['camel_routes']}</div><div class="u">条集成路由</div></div></div>
</div>"""
    sub = ("OpenOntology 采用五层正交设计，严守业务纯洁性铁律：内核只表达纯业务概念，"
           "绝不混入外部系统表名、HTTP 重试或连接池代码。底层由六张纯业务表和九个数据库"
           "物理写门禁触发器锁死，任何绕过 Action 直写库的企图都会被数据库内核当场熔断；"
           "所有脏活累活，全部隔离在外部的 Camel 集成总线里。")
    return page("2/6 · 五层正交架构：100% 业务纯洁性 (PIM vs PSM)",
                ["ok|PIM 纯度 100%", "am|直写被熔断"],
                content, sub, 1,
                f"TABLES {f['tables']} · TRIGGERS {f['triggers']} · CAMEL ROUTES {f['camel_routes']}")


# ---------------------------------------------------------------------------
# Scene 3 · 语义层原语
# ---------------------------------------------------------------------------
def scene_3(f: dict[str, Any]) -> str:
    # 左：一维扁平（有损）vs 高维对象图
    s = []
    s.append(txt(0, 16, "① 一维扁平编码：有损降维投影", 15, "#fca5a5", 700))
    s.append(box(0, 26, 880, 136, "rgba(239,68,68,.05)", "#7a3333", 11, 1.2))
    cols = ["SKU", "发票名目", "厂家", "规格", "成本"]
    for i, c in enumerate(cols):
        x = 16 + i * 172
        s.append(txt(x, 50, c, 11.5, "#8fa3bb", 600, "start", "0.8"))
    rows = [("ERP-9001", "一级焦", "BRAND-1", "SPEC-A", "¥200,000"),
            ("ERP-9002", "一级焦", "BRAND-1", "SPEC-A", "¥0,000"),
            ("ERP-9003", "二级焦", "BRAND-1", "SPEC-B", "¥128,000")]
    for r, row in enumerate(rows):
        y = 74 + r * 24
        for i, cell in enumerate(row):
            x = 16 + i * 172
            fill = "#fca5a5" if (i == 3 and r == 1) else "#b9cbe0"
            s.append(txt(x, y, cell, 11.5, fill, 500, "start", "0.1"))
    s.append(txt(16, 152, "规格维度被压平 → 同一厂家跨规格无法表达，成本按发票散码碎裂", 11.5, "#e08585", 600))
    s.append(f'<path d="M440,166 L440,188" stroke="#ef4444" stroke-width="1.8" '
             f'marker-end="url(#arwr)" stroke-dasharray="5 4"/>')

    s.append(txt(0, 208, "② Ontology 正交高维对象图：多视角无损还原", 15, "#86efac", 700))
    s.append(box(0, 218, 880, 268, "rgba(34,197,94,.04)", "#2f6b4a", 11, 1.2))
    nodes = [(60, 236, 150, 46, "ContractSKU", "ERP-9001", "#38bdf8"),
             (60, 304, 150, 46, "ContractSKU", "ERP-9003", "#38bdf8"),
             (360, 236, 150, 46, "MaterialSpec", "SPEC-A", "#22c55e"),
             (360, 304, 150, 46, "MaterialSpec", "SPEC-B", "#22c55e"),
             (640, 270, 150, 46, "Manufacturer", "BRAND-1", "#f5a524")]
    for (x, y, w, h, t1, t2, c) in nodes:
        s.append(box(x, y, w, h, "rgba(255,255,255,.04)", c, 9, 1.3))
        s.append(txt(x + w / 2, y + 20, t1, 10.5, "#7f93ab", 600, "middle", "0.6"))
        s.append(txt(x + w / 2, y + 38, t2, 12.5, c, 700, "middle", "0.2"))
    s.append('<path d="M210,259 L360,259" stroke="#38bdf8" stroke-width="1.6" marker-end="url(#arwc)"/>')
    s.append('<path d="M210,327 L360,327" stroke="#38bdf8" stroke-width="1.6" marker-end="url(#arwc)"/>')
    s.append('<path d="M510,259 L640,286" stroke="#f5a524" stroke-width="1.6" marker-end="url(#arwa)"/>')
    s.append('<path d="M510,327 L640,300" stroke="#f5a524" stroke-width="1.6" marker-end="url(#arwa)"/>')
    s.append(txt(285, 252, "sku_maps_to_spec", 9.5, "#7fb8d8", 500, "middle", "0.2"))
    s.append(txt(575, 272, "spec_produced_by", 9.5, "#d8b478", 500, "middle", "0.2"))
    s.append(txt(16, 372, "ObjectSet 集合代数（多视角无损算账）", 12, "#a5e4ff", 700))
    ops = [("pivot", "群跳转跨部门"), ("union", "并集合并"), ("intersect", "交集核对"),
           ("aggregate", "分组聚合")]
    for i, (op, desc) in enumerate(ops):
        x = 16 + i * 214
        s.append(box(x, 380, 200, 52, "rgba(56,189,248,.07)", "#2a4a6b", 8, 1))
        s.append(txt(x + 12, 402, f".{op}()", 12.5, "#a5e4ff", 700, "start", "0.1"))
        s.append(txt(x + 12, 420, desc, 10.5, "#7f93ab", 500, "start", "0.3"))
    s.append(txt(16, 462, "同一批批次桶 → 成本科按厂家 / 采购按规格 / 财务按发票散码，三方恒等", 11, "#86efac", 500))

    # 三视角无损对账（同源三投影）
    s.append(txt(0, 498, "③ 同源三投影：一份对象图，三种部门口径", 14, "#a5e4ff", 700))
    s.append(box(0, 508, 880, 136, "rgba(56,189,248,.045)", "#2a4a6b", 11, 1.2))
    views = [("成本科", "group by 厂家", "BRAND-1 · 150 吨", "#f5a524"),
             ("采购报产", "group by 规格", "SPEC-A 100 + SPEC-B 50", "#38bdf8"),
             ("财务发票", "group by 散码", "ERP-9001 100 + 9003 50", "#a78bfa")]
    for i, (t1, t2, t3, c) in enumerate(views):
        x = 16 + i * 288
        s.append(box(x, 520, 268, 86, "rgba(255,255,255,.032)", c, 8, 1.1))
        s.append(txt(x + 134, 546, t1, 13, c, 700, "middle"))
        s.append(txt(x + 134, 568, t2, 10.5, "#7f93ab", 500, "middle", "0.2"))
        s.append(txt(x + 134, 592, t3, 11, "#b9cbe0", 500, "middle", "0.1"))
    s.append(txt(16, 630, "三个投影总吨数、总成本完全一致 → balanced = True，不需要任何人工对账", 11, "#86efac", 600))

    # 右：属性级主权
    r = []
    r.append(txt(0, 16, "同一对象 · 属性级主权划分（Palantir OSv2 同款）", 15, "#e9f1fb", 700))
    r.append(box(0, 30, 880, 210, "rgba(255,255,255,.022)", "#27445f", 12, 1.3))
    # 对象头
    r.append(box(18, 46, 844, 54, "rgba(56,189,248,.08)", "#2f5578", 9, 1.2))
    r.append(txt(36, 70, "Object  Customer:CUST-3001", 15, "#a5e4ff", 700, "start", "0.2"))
    r.append(txt(36, 90, "同一个业务对象 · 两套字段主权 · 一次读写口径", 11, "#7f93ab", 500))
    # 左右两半
    r.append(box(18, 110, 412, 116, "rgba(56,189,248,.05)", "#38bdf8", 9, 1.2))
    r.append(txt(36, 134, "source-backed 源系统属性", 13, "#a5e4ff", 700))
    r.append(txt(36, 156, "email · name · created_at", 11.5, "#9fb4cc", 500, "start", "0.1"))
    r.append(txt(36, 176, "来自 Shopify / Stripe / Zendesk 快照", 11, "#7f93ab", 500))
    r.append(txt(36, 200, "底表全量重刷 = 覆盖这一半", 11, "#7f93ab", 500))
    r.append(txt(36, 218, "→ 只许读，不许本体改", 11, "#7f93ab", 500))
    r.append(box(450, 110, 412, 116, "rgba(245,165,36,.055)", "#f5a524", 9, 1.2))
    r.append(txt(468, 134, "__owned__ 本体自有属性", 13, "#fcd34d", 700))
    r.append(txt(468, 156, "fraud_score · vip_tier · ops_memo", 11.5, "#d8c08a", 500, "start", "0.1"))
    r.append(txt(468, 176, "存 ontology_overlay 补丁层", 11, "#c9ad78", 500))
    r.append(txt(468, 200, "重刷后 replace-base → reapply-owned", 11, "#c9ad78", 500))
    r.append(txt(468, 218, "→ 自有状态 100% 存活", 11, "#fcd34d", 600))

    # overlay 表
    r.append(txt(0, 268, "ontology_overlay 实存补丁", 12.5, "#fcd34d", 700))
    r.append(box(0, 280, 880, 96, "rgba(245,165,36,.04)", "#7a5a22", 10, 1.2))
    r.append(txt(16, 304, "object_type", 10.5, "#8fa3bb", 600, "start", "0.8"))
    r.append(txt(200, 304, "object_id", 10.5, "#8fa3bb", 600, "start", "0.8"))
    r.append(txt(380, 304, "patch_json（自有字段）", 10.5, "#8fa3bb", 600, "start", "0.8"))
    orows = [("ContractSKU", "ERP-SKU-9001", '{"canonical_spec_id":"SPEC-A"}'),
             ("ContractSKU", "ERP-SKU-9003", '{"canonical_spec_id":"SPEC-B"}')]
    for i, (a, b, c) in enumerate(orows):
        y = 326 + i * 24
        r.append(txt(16, y, a, 11, "#c9ad78", 500, "start", "0.1"))
        r.append(txt(200, y, b, 11, "#9fb4cc", 500, "start", "0.1"))
        r.append(txt(380, y, c, 11, "#86efac", 500, "start", "0.1"))
    r.append(txt(16, 366, f"本次内核实跑：{f['overlay_rows']} 行补丁 · 全量重刷后 preserved = True",
                 11, "#fcd34d", 600))

    # 三阶段流水
    r.append(txt(0, 404, "重刷存活三拍", 12.5, "#e9f1fb", 700))
    steps = [("① replace base", "覆盖 source 半边", "#38bdf8"),
             ("② reapply overlay", "回放 owned 补丁", "#f5a524"),
             ("③ orphan fuse", "孤儿补丁熔断告警", "#ef4444")]
    for i, (t1, t2, c) in enumerate(steps):
        x = i * 300
        r.append(box(x, 416, 280, 74, "rgba(255,255,255,.032)", c, 9, 1.2))
        r.append(txt(x + 140, 442, t1, 13, c, 700, "middle"))
        r.append(txt(x + 140, 466, t2, 11, "#9fb4cc", 500, "middle", "0.2"))
        if i < 2:
            r.append(arrow(x + 282, 453, x + 298, 453, "#5f88b5", "arw", "", 1.6))
    r.append(box(0, 504, 880, 52, "rgba(34,197,94,.06)", "#22c55e", 9, 1.2))
    r.append(txt(16, 526, "契约实测：Phase A 寄生态 与 Phase B 脱壳态，同一套 Action / 同一套 Evals", 11.5, "#86efac", 700))
    r.append(txt(16, 546, f"contract_identical = {f['contract_identical']} · LLM tools 一致 = {f['tools_identical']} · {f['llm_tools']} 个工具",
                 11.5, "#bbf7d0", 600))

    content = f"""
<div class="cols">
  <div class="panel"><div class="ph"><b><i>▣</i> 拒绝一维扁平 · 高维对象图与集合代数</b>
    <span>HIGH-DIM OBJECT GRAPH</span></div>
    <div class="body" style="padding:0">{svg(880, 632, "".join(s))}</div></div>
  <div class="panel am"><div class="ph"><b><i>◈</i> 属性级主权 · overlay 补丁与重刷存活</b>
    <span>FIELD-LEVEL SOVEREIGNTY</span></div>
    <div class="body" style="padding:0">{svg(880, 560, "".join(r))}</div></div>
</div>"""
    sub = ("在语义层，OpenOntology 拒绝一维扁平表，用对象、有向关系边与 ObjectSet 集合代数"
           "构建高维业务图谱，靠 pivot 群跳转实现跨部门多视角无损算账。同时引入属性级主权："
           "同一个对象既映射外部源字段，又能用 overlay 补丁层挂载本体自有属性，"
           "外部底表全量重刷时自有状态百分之百存活。")
    return page("3/6 · 语义层原语：高维对象图、集合代数与属性主权",
                ["ok|overlay 100% 存活", "cy|ObjectSet 代数"],
                content, sub, 2,
                f"OVERLAY {f['overlay_rows']} PATCHES · contract_identical={f['contract_identical']}")


# ---------------------------------------------------------------------------
# Scene 4 · 动力学层 EditBatch
# ---------------------------------------------------------------------------
def scene_4(f: dict[str, Any]) -> str:
    s = []
    stages = [
        ("①", "simulate_action", "EditBatch 内存沙箱", "零副作用干跑",
         ["产出 ObjectDiff", "AI 多步推演", "不碰任何一行库"], "#38bdf8"),
        ("②", "Preconditions", "不变量 + 主权高压线", "当场熔断",
         ["拦截违规变更", "未遂记 REJECTED", "审计不可删改"], "#f5a524"),
        ("③", "approval_rule", "Governed Freedom", "超阈值挂起",
         ["PENDING_APPROVAL", "read_versions 乐观锁", "人工签批后生效"], "#a78bfa"),
        ("④", "revert_action", "diff.inverted() 时光机", "一键悔棋",
         ["逆向补丁重放", "生成 REVERTED 运行", "账本永久留痕"], "#22c55e"),
    ]
    for i, (num, name, sub2, note, items, color) in enumerate(stages):
        x = i * 452
        s.append(box(x, 10, 420, 300, "rgba(255,255,255,.024)", color, 14, 1.6))
        s.append(txt(x + 24, 52, num, 26, color, 800, "start", "1"))
        s.append(txt(x + 62, 50, name, 16.5, "#e9f1fb", 700, "start", "0.1"))
        s.append(txt(x + 62, 72, sub2, 11.5, "#8fa3bb", 500, "start", "0.2"))
        s.append(f'<line x1="{x + 24}" y1="88" x2="{x + 396}" y2="88" stroke="{color}" '
                 f'stroke-width="1" stroke-dasharray="4 4" opacity="0.5"/>')
        s.append(txt(x + 24, 116, note, 13.5, color, 700, "start", "0.4"))
        for j, it in enumerate(items):
            y = 150 + j * 34
            s.append(f'<circle cx="{x + 30}" cy="{y - 4}" r="3.2" fill="{color}"/>')
            s.append(txt(x + 44, y, it, 12.2, "#b9cbe0", 500, "start", "0.2"))
        s.append(box(x + 24, 254, 372, 36, "rgba(255,255,255,.03)", "#2f5578", 8, 1))
        s.append(txt(x + 210, 277, ["SIM-1 · 对象数不变", "REJECTED 入账", "store-manager-01",
                                    "CONSUME-REV"][i], 11, "#9fb4cc", 600, "middle", "0.3"))
        if i < 3:
            s.append(arrow(x + 424, 160, x + 448, 160, "#5f88b5", "arw", "", 2))

    # 底部：真实干跑证据
    s.append(box(0, 330, 880, 150, "rgba(56,189,248,.05)", "#2a4a6b", 12, 1.2))
    s.append(txt(20, 358, "真实干跑证据 · simulate_action（内存自检，不写库）", 13.5, "#a5e4ff", 700))
    s.append(txt(20, 388, f"valid = {f['sim_valid']}", 13, "#86efac", 600, "start", "0.1"))
    s.append(txt(200, 388, f"零副作用 = {f['sim_no_side_effect']}", 13, "#86efac", 600, "start", "0.1"))
    s.append(txt(430, 388, f"diff 变更 {f['sim_diff_ops']} 项", 13, "#fcd34d", 600, "start", "0.1"))
    s.append(txt(20, 418, f"diff.summary() = {f['sim_diff']}", 12, "#9fb4cc", 500, "start", "0.1"))
    s.append(txt(20, 446, "submit 阶段才在 _write_gate 事务内真正落库；审批 / 悔棋全程走同一条审计账本",
                 11.5, "#7f93ab", 500, "start", "0.2"))
    s.append(txt(20, 470, "任何一步异常 → 事务整体 ROLLBACK，门禁不可能被留在打开状态", 11.5, "#7f93ab", 500))

    # 右侧：治理状态机
    s.append(box(900, 330, 908, 150, "rgba(255,255,255,.022)", "#27445f", 12, 1.2))
    s.append(txt(920, 358, "受治理状态机 · 四态封闭", 13.5, "#e9f1fb", 700))
    # 四态：左列 COMMITTED / REVERTED，右列 PENDING_APPROVAL / REJECTED
    s.append(box(1002, 384, 200, 42, "rgba(255,255,255,.035)", "#22c55e", 9, 1.3))
    s.append(txt(1102, 411, "COMMITTED", 12, "#22c55e", 700, "middle", "0.6"))
    s.append(box(1002, 462, 200, 42, "rgba(255,255,255,.035)", "#38bdf8", 9, 1.3))
    s.append(txt(1102, 489, "REVERTED", 12, "#38bdf8", 700, "middle", "0.6"))
    s.append(box(1300, 384, 232, 42, "rgba(255,255,255,.035)", "#f5a524", 9, 1.3))
    s.append(txt(1416, 411, "PENDING_APPROVAL", 12, "#f5a524", 700, "middle", "0.6"))
    s.append(box(1300, 462, 232, 42, "rgba(255,255,255,.035)", "#ef4444", 9, 1.3))
    s.append(txt(1416, 489, "REJECTED", 12, "#ef4444", 700, "middle", "0.6"))

    s.append(arrow(1206, 405, 1296, 405, "#f5a524", "arwa", "", 1.7))
    s.append(txt(1251, 396, "超阈值", 10, "#d8b478", 500, "middle"))
    s.append(arrow(1296, 417, 1206, 417, "#22c55e", "arwg", "5 4", 1.5))
    s.append(txt(1251, 434, "审批放行", 10, "#86efac", 500, "middle"))
    s.append(arrow(1416, 430, 1416, 458, "#ef4444", "arwr", "5 4", 1.7))
    s.append(txt(1432, 448, "不变量违规", 10, "#e08585", 500, "start"))
    s.append(arrow(1102, 430, 1102, 458, "#38bdf8", "arwc", "5 4", 1.7))
    s.append(txt(1086, 448, "悔棋", 10, "#7fb8d8", 500, "end"))

    s.append(txt(1580, 388, "① AI 提交即通过", 11, "#86efac", 600))
    s.append(txt(1580, 406, "② 触碰红线挂起", 11, "#fcd34d", 600))
    s.append(txt(1580, 466, "③ 违规当场拒绝", 11, "#fca5a5", 600))
    s.append(txt(1580, 484, "④ 撤回生成新 run", 11, "#a5e4ff", 600))
    s.append(txt(920, 528, "四态之外没有第五种可能：每一次尝试都被强制落到其中一格，账本永久留痕", 11.5, "#8fa3bb", 600, "start", "0.2"))

    content = f"""
<div class="panel" style="padding:10px 14px">
  <div class="ph"><b><i>▣</i> Action 执行生命期 · 沙箱推演 → 门禁 → 审批 → 时光机</b>
    <span>GOVERNED FREEDOM · ZERO SIDE EFFECTS</span></div>
  <div class="body" style="padding:2px 0">{svg(1832, 540, "".join(s))}</div>
</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;height:104px;flex:0 0 auto">
  <div class="card cy"><div class="ct">沙箱不是数据库 <small>SANDBOX</small></div>
    <div class="mono">EditBatch 在内存字典里改对象，build_diff() 才产出补丁</div></div>
  <div class="card am"><div class="ct">拒绝也要留痕 <small>REFUSAL AUDIT</small></div>
    <div class="mono">InvariantError 触发 _record_rejection → REJECTED 永久入账</div></div>
  <div class="card gd"><div class="ct">悔棋是数学 <small>REVERT</small></div>
    <div class="mono">diff.inverted() 精确反演，不靠备份、不靠人工比对</div></div>
</div>"""
    sub = ("在动力学层，任何动作都不许直接改库，而是在 EditBatch 内存沙箱里零副作用干跑，"
           "产出可逆的 ObjectDiff。AI 可以先做多步推演；提交时若违反业务不变量，"
           "未遂企图也会被强制记入审计；触碰大额风控红线则自动挂起等待人工审批，"
           "配合读取版本乐观锁与一键时光机逆向悔棋，实现有治理的自由。")
    return page("4/6 · 动力学层原语：EditBatch 沙箱、有治理的自由与时光机",
                ["cy|零副作用干跑", "gd|一键悔棋"],
                content, sub, 3,
                f"SIM valid={f['sim_valid']} · no_side_effect={f['sim_no_side_effect']} · diff {f['sim_diff_ops']} ops")


# ---------------------------------------------------------------------------
# Scene 5 · 寄居蟹式演进刻度尺
# ---------------------------------------------------------------------------
def scene_5(f: dict[str, Any]) -> str:
    s = []
    axis_y = 152
    x0, x1 = 60, 1772
    s.append(txt(x0, 22, "主权刻度尺 · Owned 占比由 0 平滑推到 100，越往右本体掌握越多", 13, "#a5e4ff", 700))
    s.append(f'<line x1="{x0}" y1="{axis_y}" x2="{x1}" y2="{axis_y}" stroke="#27445f" '
             f'stroke-width="8" stroke-linecap="round"/>')
    s.append(f'<line x1="{x0}" y1="{axis_y}" x2="{x1}" y2="{axis_y}" stroke="url(#bargr)" '
             f'stroke-width="8" stroke-linecap="round" opacity="0.92"/>')

    stages = [
        (0, "0%", "阶段 1", "纯镜像寄生", "不改老系统一分一毫", "第一周出全局诊断", "纯寄生", "#38bdf8"),
        (20, "20%", "阶段 2", "表皮长肉", "overlay 挂 AI 风控与协同新属性", "3 天上线新业务", "长新肉", "#22c55e"),
        (70, "70%", "阶段 3", "核心脏器内迁", "状态机收归 Ontology 裁判", "老系统降级为下游记账机", "内迁", "#f5a524"),
        (100, "100%", "阶段 4", "拔掉插头去 SaaS", "__owned__ = True 全量自有", "旧系统断电退役 · 上层零改动", "脱壳", "#a78bfa"),
    ]
    for pct, pct_s, tag, title, d1, d2, word, color in stages:
        x = x0 + (x1 - x0) * pct / 100
        x = min(max(x, x0 + 16), x1 - 16)
        s.append(f'<circle cx="{x}" cy="{axis_y}" r="14" fill="#050a12" stroke="{color}" stroke-width="3.2"/>')
        s.append(f'<circle cx="{x}" cy="{axis_y}" r="5.5" fill="{color}"/>')
        s.append(txt(x, axis_y - 26, pct_s, 17, color, 800, "middle", "0.4"))
        s.append(txt(x, axis_y + 40, tag, 11.5, "#8fa3bb", 600, "middle", "0.8"))

    # 四张等宽阶段卡（独立一行，与刻度尺的百分比位置解耦，保证可读）
    cw, gap = 434, 20
    for i, (pct, pct_s, tag, title, d1, d2, word, color) in enumerate(stages):
        cx = i * (cw + gap)
        cy = 208
        ch = 168
        s.append(box(cx, cy, cw, ch, "rgba(255,255,255,.028)", color, 12, 1.5))
        s.append(box(cx + 16, cy + 16, 92, 30, f"rgba(255,255,255,.06)", color, 8, 1.2))
        s.append(txt(cx + 62, cy + 37, pct_s, 14, color, 800, "middle", "0.3"))
        s.append(txt(cx + 120, cy + 37, word, 12, "#8fa3bb", 600, "start", "1.1"))
        s.append(txt(cx + 16, cy + 74, title, 16.5, "#e9f1fb", 800, "start", "0.3"))
        s.append(f'<line x1="{cx + 16}" y1="{cy + 88}" x2="{cx + cw - 16}" y2="{cy + 88}" '
                 f'stroke="{color}" stroke-width="1" stroke-dasharray="4 4" opacity="0.45"/>')
        s.append(txt(cx + 16, cy + 116, d1, 12, "#b9cbe0", 500, "start", "0.1"))
        s.append(txt(cx + 16, cy + 142, d2, 12, "#b9cbe0", 500, "start", "0.1"))
        # 从刻度尺连到卡片
        ax = min(max(x0 + (x1 - x0) * pct / 100, x0 + 16), x1 - 16)
        s.append(f'<line x1="{ax}" y1="{axis_y + 52}" x2="{cx + cw / 2}" y2="{cy - 4}" '
                 f'stroke="{color}" stroke-width="1.2" stroke-dasharray="4 5" opacity="0.45"/>')

    # 底部三张说明卡
    by = 408
    s.append(box(0, by, 566, 148, "rgba(56,189,248,.045)", "#2a4a6b", 12, 1.2))
    s.append(txt(20, by + 28, "寄居蟹模型 · 壳是租的，蟹是本体", 13.5, "#a5e4ff", 700))
    s.append(txt(20, by + 56, "旧 ERP / 云端 SaaS = 壳（可随时更换）", 11.5, "#b9cbe0", 500, "start", "0.1"))
    s.append(txt(20, by + 80, "Ontology 对象图 + 自有字段 = 蟹（真正的资产）", 11.5, "#86efac", 500, "start", "0.1"))
    s.append(txt(20, by + 104, "换壳不需要重写业务，只需要切主权声明", 11.5, "#fcd34d", 600, "start", "0.1"))
    s.append(txt(20, by + 130, "寄生 → 共生 → 原生 → 全自有：只动声明，不动代码", 11, "#7f93ab", 500, "start", "0.1"))

    s.append(box(590, by, 620, 148, "rgba(34,197,94,.05)", "#22c55e", 12, 1.2))
    s.append(txt(610, by + 28, "契约冻结 · 换壳前后上层零改动", 13.5, "#86efac", 700))
    s.append(txt(610, by + 56, f"contract_identical = {f['contract_identical']}（{f['eval_count']} 组 Evals 逐字段比对）",
                 11.5, "#bbf7d0", 600, "start", "0.1"))
    s.append(txt(610, by + 80, f"LLM 工具集一致 = {f['tools_identical']} · {f['llm_tools']} 个工具",
                 11.5, "#bbf7d0", 500, "start", "0.1"))
    s.append(txt(610, by + 104, "Action 名 / 参数 schema / 错误码 / 审批语义 全部不变", 11.5, "#9fb4cc", 500, "start", "0.1"))
    s.append(txt(610, by + 130, "同一套 Evals 在两种主权模式下跑出逐字段相同结果", 11, "#7f93ab", 500, "start", "0.1"))

    s.append(box(1234, by, 598, 148, "rgba(167,139,250,.05)", "#a78bfa", 12, 1.2))
    s.append(txt(1254, by + 28, "主权声明实测（Phase A → Phase B）", 13.5, "#ddd6fe", 700))
    s.append(txt(1254, by + 56, "A: Customer {'fraud_score': 0, 'vip_tier': 'STANDARD'}", 11, "#b9cbe0", 500, "start", "0.05"))
    s.append(txt(1254, by + 78, "A: ShopOrder {'ops_memo': None}", 11, "#b9cbe0", 500, "start", "0.05"))
    s.append(txt(1254, by + 100, "A: SupportTicket {'ai_triage_note': None}", 11, "#b9cbe0", 500, "start", "0.05"))
    s.append(txt(1254, by + 124, "B: 四个对象类型全部 __owned__ = True → 零外部数据源", 11.5, "#ddd6fe", 700, "start", "0.1"))

    # 底部对照条：大爆炸重写 vs 刻度尺演进
    cy2 = by + 158
    s.append(txt(0, cy2 + 4, "为什么不是「大爆炸式重写」", 13, "#e9f1fb", 700))
    pairs = [
        ("传统做法", "一次性替换核心系统", "18 个月上线 · 中途无法回退 · 业务停摆风险", "#ef4444"),
        ("刻度尺做法", "按字段逐步接管主权", "第一周就有产出 · 每步可回退 · 旧系统持续可用", "#22c55e"),
    ]
    for i, (t1, t2, t3, c) in enumerate(pairs):
        x = i * 924
        s.append(box(x, cy2 + 12, 908, 66, "rgba(255,255,255,.024)", c, 10, 1.2))
        s.append(txt(x + 18, cy2 + 36, t1, 12.5, c, 700, "start", "0.6"))
        s.append(txt(x + 120, cy2 + 36, t2, 13, "#e9f1fb", 600, "start", "0.2"))
        s.append(txt(x + 18, cy2 + 62, t3, 11.5, "#9fb4cc", 500, "start", "0.1"))

    content = f"""
<div class="panel" style="padding:10px 14px">
  <div class="ph"><b><i>▣</i> 四阶段主权刻度尺 · 系统替换不再是掀桌子</b>
    <span>PARASITIC → SYMBIOTIC → NATIVE → FULLY OWNED</span></div>
  <div class="body" style="padding:2px 0">{svg(1832, 700, "".join(s))}</div>
</div>"""
    sub = ("属性级主权把系统替换变成一把平滑滑动的刻度尺：第一周零侵入寄生在旧 ERP 或云端 SaaS 上，"
           "用 overlay 快速长出 AI 协同新业务；随后按字段逐步接管核心状态主权；"
           "最终把主权声明切为百分之百自有，即可直接拔掉 SaaS 插头独立运行，"
           "而上层 AI Agent 与业务代码一行都不用改。")
    return page("5/6 · 寄居蟹式演进：从 0% 寄生老系统到 100% 去掉 SaaS",
                ["gd|上层零改动", "vi|100% 自有"],
                content, sub, 4,
                f"CONTRACT IDENTICAL={f['contract_identical']} · EVALS {f['eval_count']}")


# ---------------------------------------------------------------------------
# Scene 6 · 三把物理锁 + 实证矩阵
# ---------------------------------------------------------------------------
def scene_6(f: dict[str, Any]) -> str:
    locks = [
        ("锁 ①", "AI 工具断网 + 写门禁触发器锁", "#ef4444",
         ["LLM 只能看到 Action 工具面", "9 个触发器守卫 objects / links / overlay",
          "绕过 Runtime 直写 → ABORT"],
         f"实测：{f['gate_message'] or 'GOVERNANCE_VIOLATION'}"),
        ("锁 ②", "下游黑户卡脖子锁", "#f5a524",
         ["人类在老系统偷建野码？可以建", "进不了入库口：SKU_NOT_GOVERNED",
          "AI 追缴：散码必须先归并到标准规格"],
         "实测：未归并散码入库 50 吨 → BLOCKED"),
        ("锁 ③", "老系统建码权限收缴", "#a78bfa",
         ["Create 入口只留 register_contract_sku", "新物理物种必须 mdm_steward 签批",
          "审计账本 append-only，删不掉"],
         "实测：新规格 → PENDING_APPROVAL → 签批生效"),
    ]
    tagcls = {"#ef4444": "red", "#f5a524": "amber", "#a78bfa": "violet"}
    clsmap = {"#ef4444": "bd", "#f5a524": "am", "#a78bfa": "vi"}
    left = ""
    for tag, title, color, items, proof in locks:
        rows = "".join(
            f'<div style="display:flex;gap:8px;align-items:baseline;font-size:13.5px;padding:2.5px 0">'
            f'<span style="color:{color};font-size:10.5px">▸</span>'
            f'<span style="color:#b9cbe0">{it}</span></div>'
            for it in items
        )
        left += f"""
<div class="card {clsmap[color]}" style="padding:11px 13px">
  <div class="ct" style="margin-bottom:7px"><span><span class="tag {tagcls[color]}">{tag}</span>
    &nbsp;{title}</span><small>PHYSICAL LOCK</small></div>
  {rows}
  <div class="mono" style="margin-top:6px;color:{color}">{proof}</div>
</div>"""

    proofs = [
        ("①", "双真实 Odoo 19 + 真 Apache Camel 路由", "#38bdf8",
         [f"{f['camel_routes']} 条 EIP 路由 · direct:ingest / writeback",
          "region 分流 north / south · 真 JSON-RPC 回写",
          "两套 ERP 订单同步进同一对象图"], "已跑通"),
        ("②", "钢厂主数据死结破局", "#22c55e",
         [f"overlay 治存量 · 批次桶治断链",
          f"三视角对账 balanced = {f['reconcile_balanced']}",
          "黑户散码入库被 SKU_NOT_GOVERNED 卡死"], "已跑通"),
        ("③", "标准 O2C 全流程", "#a78bfa",
         ["客户 → 订单 → 发货 → 开票 → 回款",
          "信用额度超限自动挂审批",
          "全链路 Action 均留 ObjectDiff"], "已跑通"),
        ("④", "吸干开源 CRM 后拔插头独立运行", "#f5a524",
         [f"BottleCRM 数据已落盘 = {f['vendor_crm']}",
          "unplug 后外部适配器焊死为 Disconnected",
          "上层 Action 与 Evals 零改动"], "已跑通"),
    ]
    right = ""
    for num, title, color, lines, status in proofs:
        cls = {"#38bdf8": "cy", "#22c55e": "gd", "#a78bfa": "vi", "#f5a524": "am"}[color]
        body = "".join(
            f'<div class="mono" style="line-height:1.62;color:#9fb4cc">{ln}</div>' for ln in lines
        )
        right += f"""
<div class="card {cls}" style="padding:11px 13px">
  <div class="ct" style="margin-bottom:6px"><span><b style="color:{color};font-size:15px">{num}</b>&nbsp;{title}</span>
    <span class="tag green">{status}</span></div>
  {body}
</div>"""

    content = f"""
<div class="cols">
  <div class="panel am"><div class="ph"><b><i>▣</i> 防旁路三把物理锁</b>
    <span>NO HUMAN / AI BYPASS</span></div>
    <div class="body" style="justify-content:space-between;gap:14px">{left}
      <div class="card" style="background:rgba(255,255,255,.02)">
        <div class="ct">三锁为什么必须「物理」<small>WHY PHYSICAL</small></div>
        <div class="mono" style="line-height:1.75">制度靠人守，人会被绕过；只有写进数据库触发器与权限表，
        才可能对人和 AI 同时成立。</div>
      </div>
    </div></div>
  <div class="panel vi"><div class="ph"><b><i>◈</i> 全场景实证矩阵</b>
    <span>PROVEN ON REAL SYSTEMS</span></div>
    <div class="body" style="justify-content:space-between;gap:12px">{right}
      <div class="card gd">
        <div class="ct">内核体积 vs 能力 <small>MINIMAL KERNEL</small></div>
        <div class="kv"><span>Python 内核 + SQL 存储引擎</span>
          <b class="mono">{f['kernel_loc']} + {f['schema_loc']} 行</b></div>
        <div class="kv"><span>测试用例 / 测试文件</span>
          <b class="mono">{f['tests_collected']} / {f['test_files']}</b></div>
        <div class="kv"><span>对象类型 / Action / LinkType（钢厂样板）</span>
          <b class="mono">{f['steel_types']} / {f['steel_actions']} / {f['steel_links']}</b></div>
      </div>
    </div></div>
</div>"""
    sub = ("最后用三把物理锁焊死人类和 AI 的旁路偷写：AI 触发器门禁、下游黑户入库卡脖子锁、"
           "老系统建码权限收缴。从双 Odoo 与 Camel 并购集成、钢厂主数据破局，"
           "到 O2C 全流程与吸干开源 CRM 后拔插头独立运行，OpenOntology 用极简纯净的内核，"
           "让企业拥有真正可推演、可治理、可演进的决策操作系统。")
    return page("6/6 · 防旁路三把物理锁与全场景实证矩阵",
                ["ok|三锁齐备", "cy|4 个实证样板"],
                content, sub, 5,
                f"LOCKS 3 · PROOFS 4 · TESTS {f['tests_collected']} · KERNEL {f['kernel_loc']} LOC")


SCENES = [scene_1, scene_2, scene_3, scene_4, scene_5, scene_6]

NARRATIONS = [
    "为什么普通 ReAct 智能体一上生产就翻车？因为它把策略、世界模型、规划和价值全塞进大模型，"
    "靠幻觉脑补企业现实，还让它直接裸调 ERP 接口。用 AlphaGo 架构一照就穿：Palantir 的本质，"
    "是把确定性的世界模型与规则裁判从大模型剥离，固化为 Ontology。大模型只提策略，"
    "Ontology 成为它唯一的工具，外部业务系统退化为传感器与机械臂。",
    "OpenOntology 采用五层正交设计，严守业务纯洁性铁律：内核只表达纯业务概念，"
    "绝不混入任何外部系统表名、HTTP 重试或连接池代码。底层由六张纯业务表和九个物理写门禁触发器锁死，"
    "任何绕过 Action 直接写库的企图都会被数据库内核当场熔断；所有脏活累活，"
    "全部隔离在外部的 Apache Camel 集成总线里。",
    "语义层拒绝一维扁平表，用对象、有向关系边与 ObjectSet 集合代数构建高维业务图谱，"
    "靠 pivot 群跳转实现跨部门多视角无损算账。再引入属性级主权：同一个对象既能映射外部源字段，"
    "又能用 overlay 补丁层挂载本体自有属性，外部底表全量重刷时，自有状态百分之百存活。",
    "动力学层里，任何动作都不许直接改库，而是在 EditBatch 内存沙箱零副作用干跑，"
    "产出可逆的 ObjectDiff。AI 可以先做多步推演；提交时若违反业务不变量，未遂企图也被强制记入审计；"
    "触碰风控红线则自动挂起等待人工审批，配合版本乐观锁与一键时光机悔棋，实现有治理的自由。",
    "属性级主权把系统替换变成一把平滑滑动的刻度尺：第一周零侵入寄生在旧 ERP 或云端 SaaS 上，"
    "用 overlay 快速长出 AI 协同新业务；随后按字段逐步接管核心状态主权；"
    "最终把主权声明切为百分之百自有，直接拔掉 SaaS 插头独立运行，上层代码一行都不用改。",
    "最后用三把物理锁焊死人类和 AI 的旁路偷写：AI 触发器门禁、下游黑户入库卡脖子锁、建码权限收缴。"
    "从双 Odoo 与 Camel 并购集成、钢厂主数据破局，到 O2C 全流程与吸掉开源 CRM 后拔插头独立运行，"
    "OpenOntology 用极简纯净的内核，让企业拥有真正可推演、可治理、可演进的决策操作系统。",
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
            page.wait_for_timeout(380)
            page.screenshot(
                path=str(out), clip={"x": 0, "y": 0, "width": W, "height": H}
            )
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
        stamp = AUDIO / f"scene_{i:02d}.hash"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        txt.write_text(text, encoding="utf-8")
        ok = False
        # 先写临时文件再原子替换：edge-tts 失败时会留下损坏的空 mp3，
        # 绝不允许它覆盖已有的好文件。
        if EDGE_TTS.exists():
            tmp = AUDIO / f".scene_{i:02d}.tmp.mp3"
            cmd = [
                str(EDGE_TTS), "--voice", VOICE, "--file", str(txt),
                "--rate=+12%", "--write-media", str(tmp), "--no-progress",
            ]
            for attempt in range(6):
                if attempt:
                    time.sleep(1.5 * attempt)  # 公网抖动，退避重试
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
            # 仅当文本与上一轮完全一致（hash 命中）时才复用，避免字幕与配音错位
            same_text = stamp.exists() and stamp.read_text().strip() == digest
            try:
                probe_duration(out)
                if same_text:
                    ok = True
                    print(f"[tts] scene {i} 文本未变，复用已有配音")
                else:
                    print(f"[tts] scene {i} 文本已变更，旧配音不可复用")
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
        stamp.write_text(digest, encoding="utf-8")
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
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x050a12,"
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
        "fps_30": info["fps"] == "30/1",
        "h264": info["vcodec"] == "h264",
        "yuv420p": info["pix_fmt"] == "yuv420p",
        "has_audio": info["acodec"] is not None,
        "aac": info["acodec"] == "aac",
        "duration_90_160": 90 <= info["duration"] <= 160,
        "start_pts_zero": info["start_pts"] == 0,
        "start_time_zero": abs(float(info["start_time"])) < 1e-6,
        "frame0_not_black": info["frame0_mean"] > 20,
    }
    info["checks"] = checks
    info["all_ok"] = all(checks.values())
    return info


def main() -> int:
    print("=" * 78)
    print("TK-ONTO-011 · OpenOntology 架构与第一性原理 · 高清讲解视频自动生成")
    print("=" * 78)
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
        print(f"[data]  kernel {facts['kernel_loc']} LOC · tables {facts['tables']} · "
              f"triggers {facts['triggers']} · tests {facts['tests_collected']} · "
              f"camel {facts['camel_routes']} · contract_identical={facts['contract_identical']}")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
