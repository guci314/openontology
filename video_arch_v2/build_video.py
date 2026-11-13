#!/usr/bin/env python3
"""把 archify 动画图 + 中文配音合成介绍视频。

链路：edge-tts 分段配音 → WAV 时间轴 → CDP 逐帧 seek archify trace 动画
      → ffmpeg 帧序列 + 音轨 → MP4。

用法：
  python build_video.py tts       # 只生成配音与时间轴
  python build_video.py render    # 逐帧渲染（依赖 tts 的 timeline）
  python build_video.py compose   # 合成 MP4（依赖 render 的帧）
  python build_video.py all       # 全流程
"""
from __future__ import annotations

import asyncio
import bisect
import json
import math
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from cdp import CDP

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "html"
AUDIO = ROOT / "audio"
FRAMES = ROOT / "frames"
BUILD = ROOT / "build"
SCRIPT = json.loads((ROOT / "script.json").read_text(encoding="utf-8"))
FPS = SCRIPT["fps"]
SNAP_EVERY = 2  # 每 2 帧截一次，中间帧复用；20fps 视频 / 10fps 画面更新
OUTPUT = ROOT / "open-ontology-architecture-v2.mp4"

# ---------------------------------------------------------------- 页面舞台

SETUP_JS = r"""
window.__setup = function (cfg) {
  const html = document.documentElement;
  html.dataset.vkind = cfg.kind;
  const st = document.createElement('style');
  st.textContent = [
    'html,body{overflow:hidden !important;}',
    'html[data-ambient-motion="running"] svg[data-animation="trace"] [data-animate]{animation-iteration-count:infinite !important;}',
    '#vstage{position:fixed;inset:0;z-index:2147483000;pointer-events:none;',
    '  font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}',
    '#vhead{position:absolute;left:0;right:0;top:0;height:92px;display:flex;align-items:center;',
    '  gap:34px;padding:0 92px;background:rgba(255,255,255,.86);border-bottom:1px solid #e3e9f2;',
    '  box-shadow:0 2px 18px rgba(20,40,80,.05);}',
    '#vbrand{font-size:21px;letter-spacing:.12em;color:#13233b;font-weight:600;white-space:nowrap;}',
    '#vbrand i{display:inline-block;width:11px;height:11px;border-radius:3px;background:#2563eb;',
    '  margin-right:12px;vertical-align:middle;}',
    '#vchapter{font-size:20px;color:#62748e;letter-spacing:.08em;flex:1;}',
    '#vtag{font-size:18px;color:#2563eb;border:1px solid #c7d8f7;background:#eef4ff;',
    '  padding:8px 18px;border-radius:999px;letter-spacing:.06em;white-space:nowrap;}',
    '#vsub{position:absolute;left:0;right:0;bottom:0;height:196px;background:#0e192c;',
    '  display:flex;flex-direction:column;justify-content:center;padding:0 110px;}',
    '#vcaption{color:#f7faff;font-size:40px;line-height:1.45;letter-spacing:.02em;',
    '  text-align:center;font-weight:500;}',
    '#vmeta{margin-top:18px;text-align:center;color:#93a7c4;font-size:18px;letter-spacing:.1em;}',
    '#vbar{position:absolute;left:0;bottom:0;height:5px;width:100%;background:rgba(255,255,255,.16);}',
    '#vbarfill{height:100%;width:0;background:linear-gradient(90deg,#2563eb,#8b5cf6);}',
    'html[data-vkind="title"] #vsub{background:transparent;height:150px;}',
    'html[data-vkind="title"] #vcaption{color:#13233b;font-size:32px;}',
    'html[data-vkind="title"] #vmeta{color:#8496ae;}',
    'html[data-vkind="title"] #vbar{background:#dde5f0;}'
  ].join('');
  document.head.appendChild(st);

  // 图页面：藏掉 viewer 的工具栏 / 图例 / 卡片，只留 SVG 主图
  let dc = null, svg = null, centers = {}, nodes = [], anims = [];
  if (cfg.kind === 'diagram') {
    dc = document.querySelector('.diagram-container');
    svg = dc ? dc.querySelector('svg') : null;
    if (dc) {
      const keep = new Set();
      let e = dc;
      while (e) { keep.add(e); e = e.parentElement; }
      keep.forEach((el) => {
        if (el !== document.body && el !== document.documentElement) {
          el.style.setProperty('border', '0', 'important');
          el.style.setProperty('box-shadow', 'none', 'important');
        }
      });
      document.querySelectorAll('body *').forEach((el) => {
        if (keep.has(el) || dc.contains(el)) return;
        el.style.visibility = 'hidden';
      });
      // 图例、缩放、导航、finder 都挂在 dc 内、svg 外，一并藏掉
      [...dc.children].forEach((el) => { if (el !== svg) el.style.visibility = 'hidden'; });
      html.dataset.ambientMotion = 'running';
      dc.style.cssText = 'position:fixed !important;left:0;top:0;width:1920px;height:1080px;' +
        'margin:0;padding:0 0 110px 0;display:flex;align-items:center;justify-content:center;' +
        'background:transparent;overflow:visible;transform:none;transform-origin:center center;';
      if (svg) {
        const r = svg.getBoundingClientRect();
        const vb = (svg.getAttribute('viewBox') || '0 0 1080 588').split(/\s+/).map(Number);
        const ratio = vb[3] / Math.max(vb[2], 1);
        const maxH = ratio > 0.8 ? 1380 : (ratio > 0.6 ? 920 : 840);
        window.__base = Math.min(1820 / Math.max(r.width, 1), maxH / Math.max(r.height, 1));
        window.__tall = ratio > 0.8;
      } else { window.__base = 1; window.__tall = false; }
      nodes = [...document.querySelectorAll('[data-node-id]')];
      nodes.forEach((n) => {
        const r = n.getBoundingClientRect();
        centers[n.getAttribute('data-node-id')] = { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      });
      anims = document.getAnimations();
      anims.forEach((a) => { try { a.pause(); } catch (_) {} });
    }
  } else {
    html.dataset.card = cfg.card || 'intro';
    const foot = document.getElementById('foot');
    if (foot) foot.style.visibility = 'hidden';
    window.__base = 1;
  }

  const ov = document.createElement('div');
  ov.id = 'vstage';
  ov.innerHTML = '<div id="vhead"><div id="vbrand"><i></i>OPENONTOLOGY · 架构与原理</div>' +
    '<div id="vchapter"></div><div id="vtag"></div></div>' +
    '<div id="vsub"><div id="vcaption"></div><div id="vmeta"></div></div>' +
    '<div id="vbar"><div id="vbarfill"></div></div>';
  document.body.appendChild(ov);
  document.getElementById('vchapter').textContent = cfg.chapter || '';
  document.getElementById('vtag').textContent = cfg.tag || '';
  document.getElementById('vcaption').textContent = cfg.caption || '';
  document.getElementById('vmeta').textContent = '既有实现 / 目标设计 / 未实现 · 逐段标注';
  const tagEl = document.getElementById('vtag');
  const tagText = cfg.tag || '';
  const palette = (tagText.indexOf('未') >= 0)
    ? ['#fff4e6', '#b45309', '#f3d3a6']
    : (tagText.indexOf('设计') >= 0)
      ? ['#eef4ff', '#2563eb', '#c7d8f7']
      : (tagText.indexOf('既有实现') >= 0 || tagText.indexOf('实证') >= 0 || tagText.indexOf('来源') >= 0)
        ? ['#eafaf1', '#047857', '#b7e4cf']
        : ['#f4ecff', '#7c3aed', '#dcc9f8'];
  tagEl.style.background = palette[0];
  tagEl.style.color = palette[1];
  tagEl.style.borderColor = palette[2];
  const cap = document.getElementById('vcaption');

  window.__cfg = cfg;
  window.__dc = dc; window.__svg = svg; window.__centers = centers;
  window.__nodes = nodes; window.__anims = anims;

  window.__seek = function (t, dur, g, total) {
    cap.style.opacity = String(Math.min(1, t / 0.55));
    document.getElementById('vbarfill').style.width = ((g / Math.max(total, .001)) * 100).toFixed(3) + '%';
    if (cfg.kind === 'title') {
      if (typeof window.__titleSeek === 'function') window.__titleSeek(t, dur);
      return;
    }
    const p = Math.min(1, t / Math.max(dur, .001));
    const ease = p < .5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
    const z0 = cfg.shot.zoom[0], z1 = cfg.shot.zoom[1];
    const zoom = (z0 + (z1 - z0) * ease) * (window.__base || 1);
    let tx = 0, ty = 0;
    const focus = cfg.shot.focus || [];
    if (cfg.shot.vertical_pan) {
      ty = 190 - 380 * ease;
    } else if (focus.length) {
      let cx = 0, cy = 0, n = 0;
      focus.forEach((id) => { const c = window.__centers[id]; if (c) { cx += c.x; cy += c.y; n += 1; } });
      if (n) { cx /= n; cy /= n; tx = (960 - cx) * zoom * .30 * ease; ty = (500 - cy) * zoom * .30 * ease; }
    }
    if (window.__dc) {
      window.__dc.style.transform = 'translate(' + tx.toFixed(2) + 'px,' + ty.toFixed(2) + 'px) scale(' + zoom.toFixed(4) + ')';
    }
    const at = t * 1000 * (cfg.shot.anim || 1);
    window.__anims.forEach((a) => { try { a.currentTime = at; } catch (_) {} });
    window.__nodes.forEach((n) => {
      const id = n.getAttribute('data-node-id');
      const dim = focus.length && focus.indexOf(id) < 0;
      n.style.setProperty('opacity', dim ? '0.24' : '1', 'important');
      n.style.setProperty('filter', (!dim && focus.length) ? 'drop-shadow(0 0 9px rgba(37,99,235,.45))' : 'none', 'important');
    });
  };
};
"""


def setup_page(page, seg, cfg):
    if seg["kind"] == "title":
        page.goto(str(HTML / "title.html"))
    else:
        page.goto(str(HTML / seg["html"]))
    page.eval(SETUP_JS + "\nwindow.__setup(" + json.dumps(cfg, ensure_ascii=False) + ");")


# ---------------------------------------------------------------- 配音

async def _synth(text, path, voice, rate):
    import edge_tts
    # edge-tts 的 aiohttp 会读环境代理；本机代理端口常变且可能已失效，直连反而通。
    keys = [k for k in list(os.environ) if k.lower() in
            ("http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy")]
    saved = {k: os.environ.pop(k) for k in keys}
    try:
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
        except Exception as first:
            proxy = saved.get("HTTPS_PROXY") or saved.get("https_proxy") or saved.get("HTTP_PROXY")
            if not proxy:
                raise
            print(f"    TTS 直连失败（{type(first).__name__}），改走代理重试")
            await edge_tts.Communicate(text, voice, rate=rate, proxy=proxy).save(str(path))
    finally:
        os.environ.update(saved)


def build_tts():
    # 沙箱 shim 对已存在目录的 mkdir(exist_ok=True) 会抛 EEXIST，先判存在
    for d in (AUDIO, BUILD):
        if not d.exists():
            d.mkdir(parents=True)
    segs = SCRIPT["segments"]
    voice, rate = SCRIPT["voice"], SCRIPT["rate"]

    async def run():
        for i, s in enumerate(segs):
            mp3 = AUDIO / f"{s['id']}.mp3"
            if mp3.exists() and mp3.stat().st_size > 2000:
                print(f"  [{s['id']}] 已存在，跳过")
                continue
            await _synth(s["narration"], mp3, voice, rate)
            print(f"  [{s['id']}] {mp3.stat().st_size // 1024} KB  <- {s['caption']}")

    asyncio.run(run())

    # 统一转 48k 单声道 WAV，便于用标准库精确拼接
    for s in segs:
        wav_path = AUDIO / f"{s['id']}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(AUDIO / f"{s['id']}.mp3"),
             "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)],
            check=True,
        )

    sr, sw, ch = 48000, 2, 1
    silence_lead = b"\x00" * int(sr * 0.30) * sw * ch
    silence_gap = b"\x00" * int(sr * SCRIPT["gap"]) * sw * ch
    with wave.open(str(BUILD / "narration.wav"), "wb") as out:
        out.setnchannels(ch); out.setsampwidth(sw); out.setframerate(sr)
        out.writeframes(silence_lead)
        pos = 0.30
        timeline = []
        for s in segs:
            with wave.open(str(AUDIO / f"{s['id']}.wav"), "rb") as w:
                data = w.readframes(w.getnframes())
            start = pos
            out.writeframes(data)
            pos += len(data) / (sr * sw * ch)
            out.writeframes(silence_gap)
            end = pos + SCRIPT["gap"]
            timeline.append({"id": s["id"], "start": round(start, 4),
                             "end": round(end, 4), "dur": round(end - start, 4),
                             "voice_dur": round(pos - start, 4)})
            pos = end
    total = (BUILD / "narration.wav").stat().st_size  # placeholder, 下面用 wave 精确算
    with wave.open(str(BUILD / "narration.wav"), "rb") as w:
        total = w.getnframes() / w.getframerate()
    (BUILD / "timeline.json").write_text(
        json.dumps({"total": round(total, 4), "fps": FPS, "timeline": timeline},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  音轨总长 {total:.2f}s，共 {math.ceil(total * FPS)} 帧")
    return timeline, total


# ---------------------------------------------------------------- 逐帧

def build_render():
    tl = json.loads((BUILD / "timeline.json").read_text(encoding="utf-8"))
    total, timeline = tl["total"], tl["timeline"]
    nframes = math.ceil(total * FPS)
    if not FRAMES.exists():
        FRAMES.mkdir(parents=True)
    starts = [x["start"] for x in timeline]
    only = {x for x in os.environ.get("ONLY_SEGMENTS", "").split(",") if x}
    if only:
        print(f"  仅重渲染分镜：{sorted(only)}", flush=True)

    page = CDP.open(port=9363 if only else 9362)
    current = -1
    cfg = {}
    try:
        for i in range(nframes):
            t = i / FPS
            idx = bisect.bisect_right(starts, t) - 1
            idx = max(0, min(idx, len(timeline) - 1))
            if only and SCRIPT["segments"][idx]["id"] not in only:
                continue
            if idx != current:
                current = idx
                seg = SCRIPT["segments"][idx]
                cfg = {
                    "kind": seg["kind"], "card": seg.get("card", "intro"),
                    "chapter": seg["chapter"], "tag": seg["tag"],
                    "caption": seg["caption"], "shot": seg.get("shot", {}),
                }
                setup_page(page, seg, cfg)
                print(f"  [{seg['id']}] 切镜：{seg['html'] if seg['kind']=='diagram' else 'title/'+cfg['card']}", flush=True)
            seg_t = t - timeline[idx]["start"]
            page.eval(f"window.__seek({seg_t:.4f},{timeline[idx]['dur']:.4f},{t:.4f},{total:.4f})")
            out = FRAMES / f"{i:06d}.png"
            if i % SNAP_EVERY == 0 or i == nframes - 1:
                page.screenshot(str(out))
            else:
                shutil.copyfile(FRAMES / f"{i - 1:06d}.png", out)
            if i % 200 == 0:
                print(f"    frame {i}/{nframes}  {i / nframes * 100:.1f}%", flush=True)
    finally:
        page.close()
    print(f"  渲染完成：{nframes} 帧 @ {FPS}fps")


# ---------------------------------------------------------------- 合成

def build_compose():
    tl = json.loads((BUILD / "timeline.json").read_text(encoding="utf-8"))
    nframes = math.ceil(tl["total"] * FPS)
    missing = [i for i in range(nframes) if not (FRAMES / f"{i:06d}.png").exists()]
    if missing:
        raise RuntimeError(f"缺 {len(missing)} 帧，例如 {missing[:5]}")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(FPS), "-i", str(FRAMES / "%06d.png"),
        "-i", str(BUILD / "narration.wav"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-movflags", "+faststart", str(OUTPUT),
    ]
    subprocess.run(cmd, check=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate",
         "-of", "json", str(OUTPUT)], capture_output=True, text=True, check=True)
    print(json.dumps(json.loads(probe.stdout), ensure_ascii=False, indent=2))
    print("  成品:", OUTPUT)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "all"
    if action in ("tts", "all"):
        print("== 1/3 配音与时间轴 =="); build_tts()
    if action in ("render", "all"):
        print("== 2/3 逐帧渲染 =="); build_render()
    if action in ("compose", "all"):
        print("== 3/3 合成 MP4 =="); build_compose()
