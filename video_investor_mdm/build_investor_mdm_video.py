"""构建投资人路演 #2（100% 零英文纯中文直观版）高清配音视频：
钢厂主数据治理两大死结可视化演播台 (1920x1080 H.264/AAC)。

严格隔离与门禁：
1. 在独立 STATE_DIR (video_investor_mdm/studio_state) 与临时端口 0 上运行；
2. 全部画面文字、表格、表单输入、字幕与配音解说严禁出现任何英文字母 [A-Za-z]；
3. edge-tts (zh-CN-YunxiNeural) 配音 + ffmpeg H.264/AAC 1920x1080 30fps 非黑首帧输出。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.steel_mdm_studio import SteelMDMStudioApp, create_server  # noqa: E402

VIDEO_DIR = Path(__file__).resolve().parent
FRAME_DIR = VIDEO_DIR / "frames"
AUDIO_DIR = VIDEO_DIR / "audio"
STATE_DIR = VIDEO_DIR / "studio_state"
OUTPUT_MP4 = VIDEO_DIR / "steel_mdm_investor_demo.mp4"

EDGE_TTS = "/Users/guci/.local/bin/edge-tts"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
VOICE = "zh-CN-YunxiNeural"

VIEW_W = 1920
VIEW_H = 1080
PAD_SECONDS = 0.45


@dataclass
class Scene:
    index: int
    slug: str
    badge: str
    title: str
    narration: str

    @property
    def frame_path(self) -> Path:
        return FRAME_DIR / f"scene_{self.index:02d}_{self.slug}.png"

    @property
    def raw_audio_path(self) -> Path:
        return AUDIO_DIR / f"scene_{self.index:02d}_{self.slug}.mp3"

    @property
    def padded_audio_path(self) -> Path:
        return AUDIO_DIR / "padded" / f"scene_{self.index:02d}_{self.slug}.m4a"


SCENES: list[Scene] = [
    Scene(
        1,
        "overview",
        "困局拆解 · 为何推倒重来必然失败",
        "外部老账套无规格源表 · 业务本体沙盘零改表外挂破局",
        "传统钢厂主数据治理为什么总是推倒重来、耗资千万还屡屡失败？因为老账套里采购按合同开散码，车间只按厂家品牌领料，强行合并老表直接打断生产。业务本体沙盘不改老账套一字一行，用外挂贴片与三看板直接破局。",
    ),
    Scene(
        2,
        "act1_overlay",
        "第一幕 · 死结一外挂贴片归并",
        "四个混乱散码零改表写入外挂贴片账页 · 建立三边高维批次桶",
        "来看第一个死结如何破局。左侧外部老账套源表里，散码九零零一到九零零四没有任何规格列，原表结构保持零改动。中间记忆棋盘通过外挂贴片账页完成标准规格归并，并为入库原料同步挂齐规格、厂家与原始散码三条关系边。",
    ),
    Scene(
        3,
        "act1_reindex",
        "夜间全量数据覆盖演练",
        "老账套连续全量覆盖两次 · 外挂贴片归并与批次关系百分之百存活",
        "很多行业专家担心：老账套每天半夜全量数据覆盖刷新一次，外挂归并关系岂不是全被冲没了？我们现场执行夜间全量覆盖。可以看到连续覆写两次源表后，四条外挂贴片账页与高维批次关系依然百分之百完好存活。",
    ),
    Scene(
        4,
        "act2_guardrails",
        "第二幕 · 源头增量建码双硬护栏",
        "当场拦截未绑定规格野码与以次充好 · 并智能推荐二级冶金焦",
        "再看增量建码的源头治乱。在推演棋盘硬护栏里，采购试图新建未绑定标准规格的野码，当场触发拦截；企图把高灰高硫劣质煤挂靠一级冶金焦，又被抓获煤质指标以次充好，并自动根据化验值智能推荐二级冶金焦。",
    ),
    Scene(
        5,
        "act3_newspec",
        "第三幕 · 新品种草稿区隔离与签批",
        "三级特种焦锁定在推演草稿区 · 主数据管家一键签批生效",
        "遇到真正全新的物理品种怎么办？采购申报三级特种焦后，新申请先锁定在推演草稿区等待签批，老账套与记忆棋盘零污染且系统重启不丢失；主数据管家一键批准后，自动生成三级特种焦标准档案并完成绑定。",
    ),
    Scene(
        6,
        "act4_fifo",
        "第四幕 · 死结二三本账分毫不差",
        "车间仅按山西焦化厂盲投一百五十吨 · 成本采购财税三账零误差",
        "高潮来到第二个死结。车间报工系统只填写按山西焦化厂领料一百五十吨，不填任何规格。沙盘自动按批次先后顺序扣减一百吨一级焦与五十吨二级焦。看最右侧三视角账本：成本、采购、财税全为一百五十吨、二十八万元，分毫不差。",
    ),
    Scene(
        7,
        "act5_revert",
        "第五幕 · 一秒时光机无损撤回",
        "一键无损冲正误报工 · 批次余量还原一百吨与八十吨且账本归零",
        "即便车间把领料吨数报错了，也完全不用手工折腾红字冲销表。点击一秒时光机撤回，刚才的一百五十吨消耗瞬间无损冲正，两桶库存精确还原为一百吨和八十吨，三视角对账账本同步清零，历史审计账目一清二楚。",
    ),
    Scene(
        8,
        "panorama",
        "终局收口 · 非侵入式治理新范式",
        "不改老系统代码 · 不停产割接 · 一次治理三本账永续对齐",
        "最后再次重放按厂家盲投收口全景。不推翻老账套、不搞停产割接，仅凭外挂贴片账页、推演区硬护栏与高维三边批次桶，业务本体沙盘让钢厂主数据一次治本，成本、采购与财务三本账永远分毫不差。",
    ),
]


def _assert_zero_latin(text: str, *, context: str) -> None:
    hits = re.findall(r"[A-Za-z]+", text)
    assert not hits, f"零英文门禁失败 ({context}): 发现残留英文字母 {hits!r}"


def _verify_scenes_zero_latin() -> None:
    for s in SCENES:
        _assert_zero_latin(s.badge, context=f"scene {s.index} badge")
        _assert_zero_latin(s.title, context=f"scene {s.index} title")
        _assert_zero_latin(s.narration, context=f"scene {s.index} narration")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _probe_duration(media_path: Path) -> float:
    out = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ]
    ).stdout.strip()
    return float(out)


def _inject_subtitle_and_check_dom(page: Page, scene: Scene) -> None:
    page.evaluate(
        """([badge, title, narration]) => {
            let old = document.getElementById("mdm-sub-overlay");
            if (old) old.remove();
            const footer = document.querySelector(".footer-strip");
            if (footer) footer.style.display = "none";
            document.body.style.paddingBottom = "138px";
            const bar = document.createElement("div");
            bar.id = "mdm-sub-overlay";
            bar.style.cssText = [
                "position:fixed",
                "left:0",
                "right:0",
                "bottom:0",
                "z-index:999999",
                "padding:14px 48px 18px",
                "background:linear-gradient(180deg, rgba(4,8,18,0.90), rgba(2,6,14,0.98))",
                "border-top:2px solid rgba(167,139,250,0.6)",
                "display:flex",
                "flex-direction:column",
                "align-items:center",
                "gap:6px",
                "box-shadow:0 -12px 30px rgba(0,0,0,0.65)"
            ].join(";");
            const meta = document.createElement("div");
            meta.style.cssText = "font-size:15px;font-weight:700;color:#a78bfa;letter-spacing:.6px;";
            meta.textContent = badge + "  ｜  " + title;
            const text = document.createElement("div");
            text.style.cssText = "font-size:24.5px;font-weight:700;line-height:1.42;color:#f8fafc;text-align:center;max-width:1760px;";
            text.textContent = narration;
            bar.appendChild(meta);
            bar.appendChild(text);
            document.body.appendChild(bar);
        }""",
        [scene.badge, scene.title, scene.narration],
    )
    visible = page.evaluate(
        """() => {
            const texts = [document.title || "", document.body.innerText || ""];
            document.querySelectorAll("input, select, textarea").forEach(el => {
                texts.push(el.value || "");
                texts.push(el.placeholder || "");
            });
            return texts.join("\\n");
        }"""
    )
    _assert_zero_latin(visible, context=f"DOM scene {scene.index} ({scene.slug})")


def capture_scenes() -> None:
    _verify_scenes_zero_latin()
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)

    app = SteelMDMStudioApp(STATE_DIR, port=0)
    server = create_server(app, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    app.port = port
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    studio_url = f"http://127.0.0.1:{port}/"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=1
            )

            def _shoot(scene: Scene) -> None:
                page.goto(studio_url, wait_until="networkidle")
                page.wait_for_timeout(320)
                _inject_subtitle_and_check_dom(page, scene)
                page.screenshot(path=str(scene.frame_path))
                print(f"  frame -> {scene.frame_path.name}")

            # Scene 1: 初始未归并老账套
            _shoot(SCENES[0])

            # Scene 2: 第一幕外挂归并与三边批次桶
            app.run_act_1()
            _shoot(SCENES[1])

            # Scene 3: 第二次夜间全量数据覆盖校验
            app.reindex_legacy_erp()
            _shoot(SCENES[2])

            # Scene 4: 第二幕双硬护栏拦截与智能推荐
            app.run_act_2()
            _shoot(SCENES[3])

            # Scene 5: 第三幕新品种签批入库
            app.run_act_3()
            app.decide_act_3(op="approve")
            _shoot(SCENES[4])

            # Scene 6: 第四幕按厂家盲投 150 吨与三本账恒等
            app.run_act_4()
            _shoot(SCENES[5])

            # Scene 7: 第五幕一秒时光机无损冲正
            app.revert_action_run()
            _shoot(SCENES[6])

            # Scene 8: 再次重放三账对齐终局全景
            app.run_act_4()
            _shoot(SCENES[7])

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def synthesize_audio() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for scene in SCENES:
        out = scene.raw_audio_path
        ok = False
        if Path(EDGE_TTS).exists():
            res = _run(
                [
                    EDGE_TTS,
                    "--voice",
                    VOICE,
                    "--text",
                    scene.narration,
                    "--write-media",
                    str(out),
                ],
                check=False,
            )
            ok = res.returncode == 0 and out.exists() and out.stat().st_size > 1000
        if not ok:
            aiff = AUDIO_DIR / f"scene_{scene.index:02d}_{scene.slug}.aiff"
            _run(["/usr/bin/say", "-v", "Tingting", "-o", str(aiff), scene.narration])
            _run(
                [
                    FFMPEG,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(aiff),
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(out),
                ]
            )
            aiff.unlink(missing_ok=True)
        print(f"  audio -> {out.name} ({_probe_duration(out):.2f}s)")


def assemble_video() -> None:
    pad_dir = AUDIO_DIR / "padded"
    pad_dir.mkdir(parents=True, exist_ok=True)
    segment_durations: list[float] = []
    padded_files: list[Path] = []

    for scene in SCENES:
        p = scene.padded_audio_path
        _run(
            [
                FFMPEG,
                "-y",
                "-v",
                "error",
                "-i",
                str(scene.raw_audio_path),
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
                str(p),
            ]
        )
        padded_files.append(p)
        segment_durations.append(_probe_duration(p))

    audio_list = AUDIO_DIR / "audio_concat.txt"
    audio_list.write_text(
        "".join(f"file '{p}'\n" for p in padded_files), encoding="utf-8"
    )
    narration_m4a = AUDIO_DIR / "narration.m4a"
    _run(
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
            str(narration_m4a),
        ]
    )

    frames_txt = FRAME_DIR / "frames.txt"
    lines: list[str] = []
    for scene, dur in zip(SCENES, segment_durations):
        lines.append(f"file '{scene.frame_path}'")
        lines.append(f"duration {dur:.3f}")
    lines.append(f"file '{SCENES[-1].frame_path}'")
    frames_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _run(
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
            str(frames_txt),
            "-i",
            str(narration_m4a),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-r",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-tag:v",
            "avc1",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(OUTPUT_MP4),
        ]
    )
    print(f"BUILT: {OUTPUT_MP4} ({_probe_duration(OUTPUT_MP4):.2f}s, {OUTPUT_MP4.stat().st_size} bytes)")


def main() -> None:
    capture_scenes()
    synthesize_audio()
    assemble_video()


if __name__ == "__main__":
    main()
