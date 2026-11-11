"""Build the O2C left/right split-screen demo video.

Left pane  : OpenOntology AI command deck (pure business action / diff / audit)
Right pane : REAL Odoo 19 web pages captured with Playwright from
             http://localhost:8069 (database erp_north, admin/admin)

Output     : video/o2c_ontology_demo.mp4  (1920x1080, 30fps, h264 + aac)

Run:  python3 video/build_o2c_video.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_ontology import InvariantError, OntologyRuntime  # noqa: E402
from examples import o2c_ontology as o2c  # noqa: E402
from examples import odoo_o2c_adapter as adapter_mod  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

VIDEO_DIR = ROOT / "video"
ASSET_DIR = VIDEO_DIR / "assets"
FRAME_DIR = VIDEO_DIR / "frames"
AUDIO_DIR = VIDEO_DIR / "audio"
OUTPUT = VIDEO_DIR / "o2c_ontology_demo.mp4"

ODOO_BASE = "http://localhost:8069"
ODOO_DB = "erp_north"
ODOO_USER = "admin"
ODOO_PASS = "admin"

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
EDGE_TTS = "/Users/guci/.local/bin/edge-tts"
VOICE = "zh-CN-YunxiNeural"

VIEW_W, VIEW_H = 1920, 1080
FPS = 30
PAD_SECONDS = 0.6


# ---------------------------------------------------------------------------
# Scene script
# ---------------------------------------------------------------------------
@dataclass
class Scene:
    index: int
    title: str
    narration: str
    action_label: str
    params: dict[str, Any]
    status: str
    diff_lines: list[str]
    audit_lines: list[str]
    odoo_url: str = ""
    odoo_caption: str = ""
    odoo_highlight: str = ""
    raw_shot: str = ""
    extra_notes: list[str] = field(default_factory=list)

    @property
    def frame_path(self) -> Path:
        return FRAME_DIR / f"scene_{self.index:02d}.png"

    @property
    def audio_path(self) -> Path:
        return AUDIO_DIR / f"scene_{self.index:02d}.mp3"


SCENES: list[Scene] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def probe_duration(path: Path) -> float:
    result = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(result.stdout.strip())


def fmt_cents(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def diff_to_lines(run_record: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    diff = run_record.get("object_diff") or {}
    for change in diff.get("object_changes", []):
        kind = change["kind"]
        otype = change["object_type"]
        oid = change["object_id"]
        if kind == "update":
            before = change.get("before") or {}
            after = change.get("after") or {}
            fields = [
                key
                for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            ]
            for key in sorted(fields):
                lines.append(
                    f"update {otype}:{oid}  {key}: "
                    f"{before.get(key)!r} -> {after.get(key)!r}"
                )
        else:
            lines.append(f"{kind} {otype}:{oid}")
    for change in diff.get("link_changes", []):
        lines.append(
            f"{change['kind']} link {change['link_type']}: "
            f"{change['source_id']} -> {change['target_id']}"
        )
    return lines or ["<no object changes>"]


def audit_lines(runtime: OntologyRuntime, limit: int = 6) -> list[str]:
    rows = runtime.list_action_runs()
    lines: list[str] = []
    for row in rows[-limit:]:
        flag = {
            "COMMITTED": "OK",
            "PENDING_APPROVAL": "HOLD",
            "REJECTED": "BLOCK",
            "REVERTED": "REV",
        }.get(row["status"], row["status"])
        error = row.get("error_code") or ""
        lines.append(
            f"[{flag:<5}] {row['run_id']:<24} {row['action_type']:<24}"
            f" {row['status']:<17} {error}"
        )
    return lines or ["<ledger empty>"]


# ---------------------------------------------------------------------------
# Stage 1: drive the real O2C flow and capture the ontology-side evidence
# ---------------------------------------------------------------------------
@dataclass
class FlowEvidence:
    runtime: OntologyRuntime
    adapter: adapter_mod.OdooO2CAdapter
    data: adapter_mod.O2CData
    small_order: str = "O2C-SO-001"
    large_order: str = "O2C-SO-002"


def drive_flow() -> FlowEvidence:
    runtime = OntologyRuntime(o2c.spec)
    adapter = adapter_mod.OdooO2CAdapter(runtime)
    data = adapter.seed_odoo()
    adapter.sync_from_odoo()
    return FlowEvidence(runtime=runtime, adapter=adapter, data=data)


# ---------------------------------------------------------------------------
# Stage 2: real Odoo 19 screenshots via Playwright
# ---------------------------------------------------------------------------
class OdooShooter:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self) -> "OdooShooter":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        context = self._browser.new_context(
            # 1280x1000 keeps the Odoo page aspect close to the right pane
            # (1058x826 ~ 1.28) so nothing meaningful is cropped.
            viewport={"width": 1280, "height": 1000},
            device_scale_factor=2,
        )
        page = context.new_page()
        page.goto(
            f"{ODOO_BASE}/web/login?db={ODOO_DB}",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        page.fill("input[name=login]", ODOO_USER)
        page.fill("input[name=password]", ODOO_PASS)
        page.click("button:has-text('Log in')")
        page.wait_for_timeout(7000)
        session = page.evaluate(
            """async () => {
                const r = await fetch('/web/session/get_session_info', {
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({jsonrpc:'2.0', method:'call', params:{}})
                });
                const j = await r.json();
                return {db: j.result && j.result.db, uid: j.result && j.result.uid};
            }"""
        )
        if session.get("db") != ODOO_DB:
            raise RuntimeError(
                f"Odoo session bound to {session.get('db')!r}, expected {ODOO_DB!r}"
            )
        self._page = page
        return self

    def shot(self, url: str, out_path: Path, wait_ms: int = 6000) -> None:
        assert self._page is not None
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(wait_ms)
        final_url = page.url
        if "/web/login" in final_url:
            raise RuntimeError(f"Odoo redirected to login for {url}")
        page.screenshot(path=str(out_path), full_page=False)

    def __exit__(self, *exc: Any) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()


# ---------------------------------------------------------------------------
# Stage 3: HTML composite frames
# ---------------------------------------------------------------------------
FRAME_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  width: 1920px; height: 1080px; overflow: hidden;
  font-family: -apple-system, "SF Pro Text", "Helvetica Neue", Arial,
               "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  background: #05070d; color: #e6edf7;
  -webkit-font-smoothing: antialiased;
}
.stage {
  width: 1920px; height: 1080px;
  display: grid;
  grid-template-rows: 92px 1fr 132px;
  background:
    radial-gradient(1200px 600px at 12% -10%, rgba(56,189,248,.16), transparent 60%),
    radial-gradient(900px 520px at 92% 0%, rgba(139,92,246,.14), transparent 62%),
    #05070d;
}
/* ---------- top title bar ---------- */
.titlebar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 36px;
  border-bottom: 1px solid rgba(148,163,184,.16);
  background: linear-gradient(180deg, rgba(15,23,42,.92), rgba(9,13,24,.72));
}
.brand { display: flex; align-items: center; gap: 16px; }
.brand-mark {
  width: 44px; height: 44px; border-radius: 12px;
  background: linear-gradient(135deg, #38bdf8, #8b5cf6);
  display: grid; place-items: center;
  font-weight: 800; font-size: 19px; color: #05070d;
  box-shadow: 0 8px 24px rgba(56,189,248,.35);
}
.brand-text { display: flex; flex-direction: column; gap: 3px; }
.brand-name { font-size: 17px; font-weight: 700; letter-spacing: .3px; }
.brand-sub { font-size: 11.5px; color: #7c8ba1; letter-spacing: 1.6px; text-transform: uppercase; }
.scene-title { font-size: 25px; font-weight: 700; letter-spacing: .4px; }
.scene-title .idx { color: #38bdf8; }
.pill {
  font-size: 12px; font-weight: 600; letter-spacing: 1.2px;
  padding: 7px 15px; border-radius: 999px;
  border: 1px solid rgba(148,163,184,.28); color: #93a4bd;
}
/* ---------- main split ---------- */
.split {
  display: grid;
  grid-template-columns: 780px 1fr;
  gap: 22px;
  padding: 22px 30px 8px 30px;
  min-height: 0;
}
.pane {
  border-radius: 18px;
  border: 1px solid rgba(148,163,184,.18);
  background: linear-gradient(180deg, rgba(13,20,34,.96), rgba(8,12,22,.96));
  overflow: hidden;
  display: flex; flex-direction: column;
  min-height: 0;
}
.pane-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid rgba(148,163,184,.14);
  background: rgba(15,23,42,.6);
  flex: 0 0 auto;
}
.pane-head .name { font-size: 13.5px; font-weight: 700; letter-spacing: .8px; color: #cbd5e1; }
.pane-head .meta { font-size: 11px; color: #64748b; letter-spacing: 1px; }
.pane-body { padding: 18px 20px; overflow: hidden; flex: 1 1 auto; }

/* ---------- command deck ---------- */
.cmd-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.cmd-name {
  font-family: "SF Mono", Menlo, monospace; font-size: 19px; font-weight: 700;
  color: #7dd3fc;
}
.badge {
  font-size: 11.5px; font-weight: 800; letter-spacing: 1.3px;
  padding: 5px 12px; border-radius: 7px;
}
.badge.ok { background: rgba(34,197,94,.16); color: #4ade80; border: 1px solid rgba(34,197,94,.45); }
.badge.hold { background: rgba(234,179,8,.16); color: #facc15; border: 1px solid rgba(234,179,8,.45); }
.badge.block { background: rgba(239,68,68,.16); color: #f87171; border: 1px solid rgba(239,68,68,.5); }
.block-label {
  font-size: 10.5px; letter-spacing: 1.4px; color: #64748b;
  text-transform: uppercase; margin: 14px 0 7px;
}
.code {
  font-family: "SF Mono", Menlo, monospace; font-size: 12.5px; line-height: 1.62;
  background: rgba(2,6,15,.85);
  border: 1px solid rgba(148,163,184,.14);
  border-radius: 10px; padding: 12px 14px;
  color: #b9c7dd; white-space: pre-wrap; word-break: break-word;
}
.code .k { color: #7dd3fc; }
.diff-line { color: #a5b4c8; }
.diff-line.add { color: #4ade80; }
.diff-line.del { color: #f87171; }
.diff-line.warn { color: #fbbf24; }
.audit { font-family: "SF Mono", Menlo, monospace; font-size: 11.5px; line-height: 1.7; color: #8fa0b8; }
.audit .ok { color: #4ade80; }
.audit .hold { color: #facc15; }
.audit .block { color: #f87171; }
.note {
  margin-top: 12px; font-size: 12.5px; line-height: 1.6; color: #94a3b8;
  border-left: 3px solid rgba(56,189,248,.5); padding-left: 12px;
}

/* ---------- odoo pane ---------- */
.browser-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 11px 16px;
  background: linear-gradient(180deg, #1e293b, #16202f);
  border-bottom: 1px solid rgba(148,163,184,.2);
  flex: 0 0 auto;
}
.dots { display: flex; gap: 7px; }
.dot { width: 11px; height: 11px; border-radius: 50%; }
.dot.r { background: #ff5f57; } .dot.y { background: #febc2e; } .dot.g { background: #28c840; }
.url {
  flex: 1; font-family: "SF Mono", Menlo, monospace; font-size: 12px;
  color: #94a3b8; background: rgba(2,6,15,.7);
  border-radius: 7px; padding: 7px 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.url .lock { color: #4ade80; margin-right: 7px; }
.browser-shot { position: relative; flex: 1 1 auto; overflow: hidden; background: #0b1220; }
.browser-shot img {
  width: 100%; height: 100%;
  object-fit: contain; object-position: top center; display: block;
}
.highlight {
  position: absolute; right: 22px; top: 22px;
  background: rgba(5,10,20,.9);
  border: 1.5px solid rgba(56,189,248,.7);
  border-radius: 11px; padding: 10px 16px;
  font-size: 13px; font-weight: 700; color: #7dd3fc;
  box-shadow: 0 10px 30px rgba(2,6,15,.7);
}
.highlight.green { border-color: rgba(34,197,94,.8); color: #4ade80; }
.caption-strip {
  flex: 0 0 auto; padding: 10px 18px;
  background: rgba(9,13,24,.95);
  border-top: 1px solid rgba(148,163,184,.16);
  font-size: 12.5px; color: #94a3b8; letter-spacing: .2px;
}

/* ---------- subtitle ---------- */
.subtitle {
  display: flex; align-items: center;
  padding: 0 56px;
  background: linear-gradient(0deg, rgba(2,6,15,.98), rgba(2,6,15,.55));
  border-top: 1px solid rgba(148,163,184,.14);
}
.subtitle-inner {
  width: 100%;
  font-size: 31px; line-height: 1.42; font-weight: 600;
  color: #f1f5f9; letter-spacing: .4px;
  text-align: center;
  text-shadow: 0 2px 18px rgba(2,6,15,.95);
}
.subtitle-inner .accent { color: #38bdf8; }
"""


def escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def scene_html(scene: Scene) -> str:
    badge_class = {
        "COMMITTED": "ok",
        "PENDING_APPROVAL": "hold",
        "REJECTED": "block",
        "REVERTED": "hold",
    }.get(scene.status, "ok")

    diff_html = []
    for line in scene.diff_lines:
        cls = "diff-line"
        if line.startswith("create"):
            cls += " add"
        elif line.startswith("delete"):
            cls += " del"
        elif "REJECTED" in line or "cannot" in line.lower():
            cls += " warn"
        diff_html.append(f'<div class="{cls}">{escape(line)}</div>')

    audit_html = []
    for line in scene.audit_lines:
        cls = ""
        if "[OK" in line:
            cls = "ok"
        elif "[BLOCK" in line:
            cls = "block"
        elif "[HOLD" in line:
            cls = "hold"
        audit_html.append(f'<div class="{cls}">{escape(line)}</div>')

    notes_html = "".join(
        f'<div class="note">{escape(note)}</div>' for note in scene.extra_notes
    )

    params_json = escape(json.dumps(scene.params, ensure_ascii=False, indent=2))

    odoo_pane = ""
    if scene.raw_shot:
        shot_path = ASSET_DIR / scene.raw_shot
        b64 = base64.b64encode(shot_path.read_bytes()).decode("ascii")
        highlight_cls = "highlight green" if scene.status == "COMMITTED" else "highlight"
        odoo_pane = f"""
      <div class="pane">
        <div class="browser-bar">
          <div class="dots"><span class="dot r"></span><span class="dot y"></span><span class="dot g"></span></div>
          <div class="url"><span class="lock">&#128274;</span>{escape(scene.odoo_url)}</div>
        </div>
        <div class="browser-shot">
          <img src="data:image/png;base64,{b64}" alt="real Odoo 19 page" />
          <div class="{highlight_cls}">{escape(scene.odoo_highlight)}</div>
        </div>
        <div class="caption-strip">{escape(scene.odoo_caption)}</div>
      </div>"""
    else:
        odoo_pane = """
      <div class="pane">
        <div class="pane-head"><span class="name">ODOO 19</span><span class="meta">no capture</span></div>
        <div class="pane-body"><div class="code">no screenshot</div></div>
      </div>"""

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{FRAME_CSS}</style></head>
<body>
<div class="stage">
  <div class="titlebar">
    <div class="brand">
      <div class="brand-mark">OO</div>
      <div class="brand-text">
        <div class="brand-name">OpenOntology</div>
        <div class="brand-sub">business ontology runtime</div>
      </div>
    </div>
    <div class="scene-title"><span class="idx">{scene.index}/6</span> &middot; {escape(scene.title)}</div>
    <div class="pill">ODOO 19 &middot; ERP_NORTH</div>
  </div>

  <div class="split">
    <div class="pane">
      <div class="pane-head">
        <span class="name">AI 作战指挥台</span>
        <span class="meta">ont&#8203;ology_action_runs</span>
      </div>
      <div class="pane-body">
        <div class="cmd-row">
          <span class="cmd-name">{escape(scene.action_label)}</span>
          <span class="badge {badge_class}">{escape(scene.status)}</span>
        </div>
        <div class="block-label">action params</div>
        <div class="code">{params_json}</div>
        <div class="block-label">object diff</div>
        <div class="code">{''.join(diff_html)}</div>
        <div class="block-label">audit ledger</div>
        <div class="audit">{''.join(audit_html)}</div>
        {notes_html}
      </div>
    </div>
    {odoo_pane}
  </div>

  <div class="subtitle">
    <div class="subtitle-inner">{escape(scene.narration)}</div>
  </div>
</div>
</body></html>"""


def render_frames(scenes: list[Scene]) -> None:
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": VIEW_W, "height": VIEW_H},
            device_scale_factor=1,
        )
        for scene in scenes:
            html = scene_html(scene)
            tmp = FRAME_DIR / f".scene_{scene.index:02d}.html"
            tmp.write_text(html, encoding="utf-8")
            page.goto(tmp.as_uri(), wait_until="load")
            page.wait_for_timeout(400)
            page.screenshot(path=str(scene.frame_path))
            tmp.unlink(missing_ok=True)
            print(f"  frame -> {scene.frame_path.name}")
        browser.close()


# ---------------------------------------------------------------------------
# Stage 4: TTS
# ---------------------------------------------------------------------------
def synth_audio(scenes: list[Scene]) -> dict[int, float]:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    durations: dict[int, float] = {}
    for scene in scenes:
        out = scene.audio_path
        result = run(
            [
                EDGE_TTS,
                "--voice",
                VOICE,
                "--text",
                scene.narration,
                "--write-media",
                str(out),
            ]
        )
        if result.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"edge-tts failed for scene {scene.index}: {result.stderr[-500:]}"
            )
        durations[scene.index] = probe_duration(out)
        print(f"  audio -> {out.name} ({durations[scene.index]:.2f}s)")
    return durations


# ---------------------------------------------------------------------------
# Stage 5: ffmpeg assembly
# ---------------------------------------------------------------------------
def build_video(scenes: list[Scene], durations: dict[int, float]) -> None:
    pad_dir = AUDIO_DIR / "padded"
    pad_dir.mkdir(parents=True, exist_ok=True)

    padded: list[Path] = []
    segment_durations: list[float] = []

    for scene in scenes:
        padded_path = pad_dir / f"scene_{scene.index:02d}.m4a"
        result = run(
            [
                FFMPEG,
                "-y",
                "-v",
                "error",
                "-i",
                str(scene.audio_path),
                "-af",
                f"apad=pad_dur={PAD_SECONDS}",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(padded_path),
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(f"apad failed: {result.stderr[-800:]}")
        padded.append(padded_path)
        segment_durations.append(probe_duration(padded_path))

    # concat audio
    audio_list = AUDIO_DIR / "audio_concat.txt"
    audio_list.write_text(
        "".join(f"file '{p}'\n" for p in padded), encoding="utf-8"
    )
    audio_concat = AUDIO_DIR / "narration.m4a"
    result = run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(audio_list),
            "-c",
            "copy",
            str(audio_concat),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"audio concat failed: {result.stderr[-800:]}")

    # concat images with per-scene duration
    image_list = FRAME_DIR / "frames.txt"
    lines = []
    for scene, duration in zip(scenes, segment_durations):
        lines.append(f"file '{scene.frame_path}'")
        lines.append(f"duration {duration:.3f}")
    # ffmpeg concat quirk: repeat the last frame
    lines.append(f"file '{scenes[-1].frame_path}'")
    image_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

    total = sum(segment_durations)
    result = run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(image_list),
            "-i",
            str(audio_concat),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            "-vf",
            f"scale={VIEW_W}:{VIEW_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIEW_W}:{VIEW_H}:(ow-iw)/2:(oh-ih)/2:color=0x05070d",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-shortest",
            "-t",
            f"{total:.3f}",
            "-movflags",
            "+faststart",
            str(OUTPUT),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mux failed: {result.stderr[-1500:]}")
    print(f"  video -> {OUTPUT} ({total:.2f}s target)")


# ---------------------------------------------------------------------------
# Stage 6: verification
# ---------------------------------------------------------------------------
def verify() -> dict[str, Any]:
    size = OUTPUT.stat().st_size
    probe = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(OUTPUT),
        ]
    )
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video"), {})
    audio = next((s for s in streams if s["codec_type"] == "audio"), {})
    result = {
        "path": str(OUTPUT),
        "size_bytes": size,
        "size_mb": round(size / (1024 * 1024), 2),
        "duration_sec": round(float(info["format"]["duration"]), 2),
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": video.get("r_frame_rate"),
        "pix_fmt": video.get("pix_fmt"),
        "audio_codec": audio.get("codec_name"),
        "audio_channels": audio.get("channels"),
        "audio_sample_rate": audio.get("sample_rate"),
    }
    problems = []
    if size <= 1024 * 1024:
        problems.append(f"file size {result['size_mb']} MB <= 1 MB")
    if (result["width"], result["height"]) != (1920, 1080):
        problems.append(f"resolution {result['width']}x{result['height']} != 1920x1080")
    if result["video_codec"] != "h264":
        problems.append(f"video codec {result['video_codec']} != h264")
    if result["audio_codec"] != "aac":
        problems.append(f"audio codec {result['audio_codec']} != aac")
    if not (60.0 <= result["duration_sec"] <= 140.0):
        problems.append(f"duration {result['duration_sec']}s outside 60-140s")
    result["problems"] = problems
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> dict[str, Any]:
    for directory in (VIDEO_DIR, ASSET_DIR, FRAME_DIR, AUDIO_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("1. driving the real O2C flow against Odoo 19 / erp_north")
    print("=" * 78)
    flow = drive_flow()
    runtime = flow.runtime
    adapter = flow.adapter
    data = flow.data
    small = flow.small_order
    large = flow.large_order

    # --- scene 1: initial state + bottleneck radar
    initial_view = runtime.call_function(
        "diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR
    )
    small_ref = data.order(small)
    large_ref = data.order(large)
    print(
        f"  seeded {small} (odoo {small_ref.odoo_name}) "
        f"and {large} (odoo {large_ref.odoo_name})"
    )

    shooter = OdooShooter().__enter__()
    audit_snapshots: dict[int, list[str]] = {}
    try:
        raw_01 = ASSET_DIR / "odoo_raw_01.png"
        shooter.shot(
            f"{ODOO_BASE}/odoo/orders/{small_ref.odoo_id}", raw_01
        )
        audit_snapshots[1] = audit_lines(runtime)

        # --- scene 2: confirm then rejected early invoice
        confirm_run = runtime.submit_action(
            "VIDEO-CONFIRM-001",
            "confirm_sales_order",
            {"order_id": small},
            o2c.SALES_REP,
        )
        adapter.process_new_runs()
        raw_02 = ASSET_DIR / "odoo_raw_02.png"
        shooter.shot(
            f"{ODOO_BASE}/odoo/orders/{small_ref.odoo_id}", raw_02
        )

        try:
            runtime.submit_action(
                "VIDEO-INVOICE-EARLY",
                "issue_and_post_invoice",
                {"order_id": small},
                o2c.FINANCE_DIRECTOR,
            )
        except InvariantError as exc:
            print(f"  early invoice blocked: {exc.code}")
        rejected_run = runtime.get_action_run("VIDEO-INVOICE-EARLY")
        audit_snapshots[2] = audit_lines(runtime)

        # --- scene 3: ship
        ship_run = runtime.submit_action(
            "VIDEO-SHIP-001",
            "ship_order_delivery",
            {"order_id": small},
            o2c.WAREHOUSE,
        )
        adapter.process_new_runs()
        delivery = adapter.delivery_row(small)
        raw_03 = ASSET_DIR / "odoo_raw_03.png"
        shooter.shot(
            f"{ODOO_BASE}/odoo/deliveries/{delivery['id']}", raw_03
        )
        audit_snapshots[3] = audit_lines(runtime)

        # --- scene 4: invoice + payment
        invoice_run = runtime.submit_action(
            "VIDEO-INVOICE-001",
            "issue_and_post_invoice",
            {"order_id": small},
            o2c.FINANCE_DIRECTOR,
        )
        adapter.process_new_runs()
        payment_run = runtime.submit_action(
            "VIDEO-PAY-001",
            "record_customer_payment",
            {"order_id": small, "payment_cents": small_ref.total_cents},
            o2c.FINANCE_DIRECTOR,
        )
        adapter.process_new_runs()
        invoice = adapter.invoice_row(small)
        raw_04 = ASSET_DIR / "odoo_raw_04.png"
        shooter.shot(
            f"{ODOO_BASE}/odoo/customer-invoices/{invoice['id']}", raw_04
        )
        audit_snapshots[4] = audit_lines(runtime)

        # --- scene 5: AI large order -> approval
        pending_run = runtime.submit_action(
            "VIDEO-AI-LARGE",
            "confirm_sales_order",
            {"order_id": large},
            o2c.AI_AGENT,
        )
        approved_run = runtime.approve_action(
            "VIDEO-AI-LARGE", o2c.FINANCE_DIRECTOR
        )
        adapter.process_new_runs()
        raw_05 = ASSET_DIR / "odoo_raw_05.png"
        shooter.shot(
            f"{ODOO_BASE}/odoo/orders/{large_ref.odoo_id}", raw_05
        )
        audit_snapshots[5] = audit_lines(runtime)

        # --- scene 6: governance overlay survives resync
        runtime.submit_action(
            "VIDEO-GOV-001",
            "annotate_o2c_governance",
            {
                "customer_id": "O2C-CUST-001",
                "risk_tier": "WATCHLIST",
                "credit_hold_note": "Q4 信用观察：单笔上限收紧至 10 万",
                "order_id": small,
                "account_rep": "sales-rep-01",
                "fulfillment_note": "已全额收款，保留原包装单据",
            },
            o2c.SALES_REP,
        )
        adapter.sync_from_odoo()
        raw_06 = ASSET_DIR / "odoo_raw_06.png"
        shooter.shot(
            f"{ODOO_BASE}/odoo/orders/{small_ref.odoo_id}", raw_06
        )
        audit_snapshots[6] = audit_lines(runtime)
    finally:
        shooter.__exit__(None, None, None)

    customer = runtime.get_object(
        o2c.Customer, "O2C-CUST-001", o2c.FINANCE_DIRECTOR
    )
    small_order = runtime.get_object(o2c.SalesOrder, small, o2c.FINANCE_DIRECTOR)
    large_order = runtime.get_object(o2c.SalesOrder, large, o2c.FINANCE_DIRECTOR)
    invoice_obj = runtime.get_object(
        o2c.Invoice, f"INV-{small}", o2c.FINANCE_DIRECTOR
    )
    final_view = runtime.call_function(
        "diagnose_o2c_bottlenecks", {}, o2c.FINANCE_DIRECTOR
    )

    # --- build the six scenes
    scenes: list[Scene] = []

    scenes.append(
        Scene(
            index=1,
            title="痛点与 O2C 经营体检",
            narration=(
                "为什么企业不敢让大模型直接操作 ERP？因为大模型一旦产生幻觉改错单据、"
                "提前开出假发票，财务底账就会崩盘。来看看我们如何用 Palantir 同款 Ontology，"
                "给真实 Odoo 19 装上带保险栓的 AI 自动驾驶仪。"
            ),
            action_label="diagnose_o2c_bottlenecks()",
            params={},
            status="COMMITTED",
            diff_lines=[
                f"credit_exceeded_drafts: {len(initial_view['credit_exceeded_drafts'])} 张",
                f"  -> {large}  {fmt_cents(large_ref.total_cents)}"
                f" > 限额 {fmt_cents(data.credit_limit_cents)}",
                f"  -> 超限 {fmt_cents(initial_view['totals']['credit_exceeded_cents'])}",
                f"unshipped_confirmed_orders: 0",
                f"unbilled_shipped_orders: 0",
                f"unpaid_invoices: 0",
            ],
            audit_lines=audit_snapshots[1],
            odoo_url=f"{ODOO_BASE}/odoo/orders/{small_ref.odoo_id}",
            odoo_caption=(
                f"真实 Odoo 19 报价单 {small_ref.odoo_name} · 草稿状态 · "
                f"金额 {fmt_cents(small_ref.total_cents)}"
            ),
            odoo_highlight="Quotation · 草稿",
            raw_shot="odoo_raw_01.png",
            extra_notes=[
                f"正常草稿单 {small} {fmt_cents(small_ref.total_cents)}",
                f"超信用风险单 {large} {fmt_cents(large_ref.total_cents)}",
            ],
        )
    )

    scenes.append(
        Scene(
            index=2,
            title="物理护栏：拦截未发货提前开票",
            narration=(
                "先让 AI 确认两万元的正常订单，右侧 Odoo 实时生成销售单与待发货单。"
                "如果这时 AI 试图在仓库发货前违规提前开票，Ontology 的前置硬护栏会当场红牌拦截，"
                "并将未遂企图记入审计账本，确保 Odoo 里绝不产生一张假发票。"
            ),
            action_label="issue_and_post_invoice()",
            params={"order_id": small},
            status="REJECTED",
            diff_lines=[
                "REJECTED  CANNOT_INVOICE_UNSHIPPED_ORDER",
                "  cannot invoice an unshipped order",
                "  object_changes: []  link_changes: []",
                f"  confirmed order: {small} -> confirmed (by VIDEO-CONFIRM-001)",
                "  Odoo side: invoice_count = 0  (no fake invoice)",
            ],
            audit_lines=audit_snapshots[2],
            odoo_url=f"{ODOO_BASE}/odoo/orders/{small_ref.odoo_id}",
            odoo_caption=(
                f"真实 Odoo 19 销售单 {small_ref.odoo_name} · 状态 Sales Order · "
                f"待交货 {adapter.delivery_row(small)['name']} · 发票 0 张"
            ),
            odoo_highlight="Sales Order · 0 Invoices",
            raw_shot="odoo_raw_02.png",
            extra_notes=[
                f"拒绝码 {rejected_run['error_code']}",
                f"审计状态 {rejected_run['status']}",
            ],
        )
    )

    delivery_row = adapter.delivery_row(small)
    scenes.append(
        Scene(
            index=3,
            title="仓库出库：纯业务动词驱动拣货过账",
            narration=(
                "在合法路径下，AI 根本不需要懂 Odoo 底层复杂的瞬态向导。"
                "AI 只需调用 ship_order_delivery 一个纯业务动词，"
                "底层适配器自动完成拣货数量填充与出库校验，右侧真实 Odoo 交货单瞬间变为已完成。"
            ),
            action_label="ship_order_delivery()",
            params={"order_id": small},
            status="COMMITTED",
            diff_lines=diff_to_lines(ship_run),
            audit_lines=audit_snapshots[3],
            odoo_url=f"{ODOO_BASE}/odoo/deliveries/{delivery_row['id']}",
            odoo_caption=(
                f"真实 Odoo 19 交货单 {delivery_row['name']} · "
                f"状态 Done（已完成）"
            ),
            odoo_highlight="Delivery · Done",
            raw_shot="odoo_raw_03.png",
            extra_notes=[
                f"适配器调用 stock.picking.button_validate({delivery_row['id']})",
                "AI 只看到一个纯业务动词，不知道底层向导",
            ],
        )
    )

    scenes.append(
        Scene(
            index=4,
            title="开票与收款核销：应收余额精准归零",
            narration=(
                "货物出库后，AI 连续调用开票与收款核销两个纯业务动作。"
                "底层适配器自动在 Odoo 中创建应收发票、正式过账，并驱动付款核销向导。"
                "右侧真实 Odoo 发票直接盖上 Paid 已付款绿章，待核销余额精准归零。"
            ),
            action_label="record_customer_payment()",
            params={
                "order_id": small,
                "payment_cents": small_ref.total_cents,
            },
            status="COMMITTED",
            diff_lines=[
                f"create Invoice:INV-{small}  amount={fmt_cents(small_ref.total_cents)}",
                f"update SalesOrder:{small}  status: 'shipped' -> 'invoiced' -> 'paid'",
                f"update Customer:O2C-CUST-001  ar_balance_cents:"
                f" 0 -> {small_ref.total_cents} -> 0",
                "create link order_invoices: "
                f"{small} -> INV-{small}",
            ],
            audit_lines=audit_snapshots[4],
            odoo_url=f"{ODOO_BASE}/odoo/customer-invoices/{invoice['id']}",
            odoo_caption=(
                f"真实 Odoo 19 发票 {invoice['name']} · 已过账 · 已付款 · "
                f"待核销 {invoice['amount_residual']:.2f}"
            ),
            odoo_highlight="Invoice · PAID · Residual 0.00",
            raw_shot="odoo_raw_04.png",
            extra_notes=[
                f"发票 {invoice_obj.invoice_id} 状态 {invoice_obj.status}",
                f"客户应收余额 {fmt_cents(customer.ar_balance_cents)}",
            ],
        )
    )

    scenes.append(
        Scene(
            index=5,
            title="人机共治：12 万超信用大单挂起与总监审批",
            narration=(
                "面对那张十二万元的超信用额度大单，AI 申请确认时被治理规则自动挂起为待审批，"
                "此时 Odoo 单据纹丝不动。只有当财务总监在指挥台核对沙箱预演并点击批准后，"
                "右侧真实 Odoo 才正式放行确认。"
            ),
            action_label="confirm_sales_order() -> approve_action()",
            params={
                "order_id": large,
                "actor": "ai-agent-01",
                "approver": "finance-director-01",
            },
            status="PENDING_APPROVAL",
            diff_lines=[
                f"AI submit  {large}  -> PENDING_APPROVAL  (超信用额度)",
                f"  ar_balance {fmt_cents(0)} + order {fmt_cents(large_ref.total_cents)}"
                f" > limit {fmt_cents(data.credit_limit_cents)}",
                "  Odoo state while pending: draft  (纹丝不动)",
                "finance-director-01 approve_action() -> COMMITTED",
                f"update SalesOrder:{large}  status: 'draft' -> 'confirmed'",
                f"create Delivery:DEL-{large}",
            ],
            audit_lines=audit_snapshots[5],
            odoo_url=f"{ODOO_BASE}/odoo/orders/{large_ref.odoo_id}",
            odoo_caption=(
                f"真实 Odoo 19 销售单 {large_ref.odoo_name} · 审批通过后转为 Sales Order · "
                f"金额 {fmt_cents(large_ref.total_cents)}"
            ),
            odoo_highlight="Approved · Sales Order",
            raw_shot="odoo_raw_05.png",
            extra_notes=[
                f"pending 记录 {pending_run['run_id']} -> {pending_run['status']}",
                f"审批后 {approved_run['status']} by {approved_run['approved_by']}",
            ],
        )
    )

    scenes.append(
        Scene(
            index=6,
            title="架构总结：薄纯业务本体 + 厚 ERP 适配器",
            narration=(
                "上层是百分之百纯业务的 Ontology 世界模型，下层是可插拔的 ERP 适配器。"
                "不改旧系统一行代码，让 AI Agent 在铁律护栏下安全替你运行企业。"
            ),
            action_label="annotate_o2c_governance() + sync_from_odoo()",
            params={
                "risk_tier": customer.risk_tier,
                "credit_hold_note": customer.credit_hold_note,
                "account_rep": small_order.account_rep,
                "fulfillment_note": small_order.fulfillment_note,
            },
            status="COMMITTED",
            diff_lines=[
                "overlay survived full Odoo re-sync:",
                f"  Customer.risk_tier        = {customer.risk_tier!r}",
                f"  Customer.credit_hold_note = {customer.credit_hold_note!r}",
                f"  SalesOrder.account_rep    = {small_order.account_rep!r}",
                f"  SalesOrder.fulfillment_note = {small_order.fulfillment_note!r}",
                f"final small order status = {small_order.status!r}",
                f"final large order status = {large_order.status!r}",
            ],
            audit_lines=audit_snapshots[6],
            odoo_url=f"{ODOO_BASE}/odoo/orders/{small_ref.odoo_id}",
            odoo_caption=(
                f"真实 Odoo 19 销售单 {small_ref.odoo_name} · "
                f"全量重刷后 Ontology 自有治理字段 100% 存活"
            ),
            odoo_highlight="Overlay survived re-sync",
            raw_shot="odoo_raw_06.png",
            extra_notes=[
                "薄纯业务本体：Customer / SalesOrder / Delivery / Invoice",
                "厚 ERP 适配器：确认 / 出库 / 开票 / 收款全部可插拔",
                f"最终经营体检 unpaid_invoices="
                f"{len(final_view['unpaid_invoices'])}",
            ],
        )
    )

    print()
    print("=" * 78)
    print("2. rendering 1920x1080 composite frames")
    print("=" * 78)
    render_frames(scenes)

    print()
    print("=" * 78)
    print("3. synthesizing Chinese narration")
    print("=" * 78)
    durations = synth_audio(scenes)

    print()
    print("=" * 78)
    print("4. muxing video with ffmpeg")
    print("=" * 78)
    build_video(scenes, durations)

    print()
    print("=" * 78)
    print("5. verifying with ffprobe")
    print("=" * 78)
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["problems"]:
        raise SystemExit(
            "video verification failed: " + "; ".join(result["problems"])
        )

    print()
    print("scenes:")
    for scene in scenes:
        print(
            f"  {scene.index}/6  {scene.frame_path.name:<16}"
            f" {scene.audio_path.name:<16}"
            f" {durations[scene.index]:.2f}s  {scene.title}"
        )
    print()
    print(f"OK -> {OUTPUT}")
    return result


if __name__ == "__main__":
    main()
