"""构建投资人路演 #1（100% 零英文纯中文直观版）高清配音视频：
业务本体沙盘三看板 对比 传统直连模式（接单到回款全流程 1920x1080）。

严格隔离与门禁：
1. 仅使用独立测试隔离客户，绝不触碰 :8789 / :8791，也不外发真实飞书消息；
2. 全部画面、字幕与语音解说（含实时取自外部企业核心账套的凭据大屏）严禁出现任何英文字母 [A-Za-z]；
3. 每次截图前实时校验浏览器可见文字（innerText / title / 表单输入框）英文字母计数为 0；
4. edge-tts (zh-CN-YunxiNeural) 配音 + ffmpeg H.264/AAC 1920x1080 30fps 非黑首帧输出。
"""

from __future__ import annotations

import json
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

from examples.o2c_three_boards_studio import ThreeBoardsStudioApp, create_server  # noqa: E402

VIDEO_DIR = Path(__file__).resolve().parent
FRAME_DIR = VIDEO_DIR / "frames"
AUDIO_DIR = VIDEO_DIR / "audio"
STATE_DIR = VIDEO_DIR / "studio_state"
OUTPUT_MP4 = VIDEO_DIR / "o2c_three_boards_vs_mcp.mp4"

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
        "开场对照 · 演播室全景",
        "传统直连模式开环幻觉 对比 业务本体沙盘三看板闭环体系",
        "为什么大模型接上工具，在真实企业里还是不敢放权？因为传统直连模式属于开环控制，既没有业务规则校验，也没有跨重启记忆对齐。今天我们直接连接真实企业核心账套与飞书，同屏对比传统直连与业务本体三看板。",
    ),
    Scene(
        2,
        "odoo_seed",
        "物理真值 · 企业核心账套实账凭据",
        "实时读取外部核心账套 · 隔离客户双草稿报价单实据",
        "先看物理底盘。这页展示的不是静态草图，而是直接从外部企业核心账套实时读回的真实财务凭据。系统已为演示客户建好两张草稿报价单：两万元常规订单，和十二万元超授信大额单，同时给对照组配了一样的初始单据。",
    ),
    Scene(
        3,
        "act1",
        "第一幕 · 全知对齐与物理防线",
        "外部实账对齐内部确认账 · 推演草稿区当场拦截未发货开票",
        "进入第一幕。左侧传统直连对照组跳过发货校验直接请求开票，先把订单误改为已确认态生成待拣货单，随后开票报错留下半截脏单。而右侧在推演草稿区当场按未发货严禁开票红线拦截，账套零脏写，再合法确认首单。",
    ),
    Scene(
        4,
        "act2",
        "第二幕 · 抓获执行器谎言",
        "丢弃执行器返参 · 只认外部实账核对不符并擦除草稿重对齐",
        "第二幕演练最致命的生产事故：外部执行器撒谎。仓库接口未真正作业却谎称已出库。传统直连模式轻信返回值，助手脑内与真实账套彻底分裂。业务本体沙盘坚决丢弃接口谎言，反查真实账套否决并重对齐，记忆账本零幻觉。",
    ),
    Scene(
        5,
        "act3_hold",
        "第三幕上半场 · 超授信锁入草稿区",
        "十二万元大单冻结在推演草稿区 · 飞书推卡且实账保持草稿",
        "第三幕考验高风险审批隔离。十二万元大额单超出十万元授信总额。变更先被冻结在推演草稿区等待签批，并向飞书业财助手自动推送可点击审批卡片。整个等待签批期间，企业核心账套里的原始单据纹丝不动，继续保持草稿。",
    ),
    Scene(
        6,
        "act3_approve",
        "第三幕下半场 · 飞书签批落盘",
        "财务总监签批回流 · 核心账套确认单据且三看板同步收敛",
        "财务总监在飞书点击批准并写入企业账套后，网关把签批决议直接回调给本演播室，完全不干扰其他演示通道。放行结果随即写入核心账套生成出库单，飞书卡片同步锁定为已签批终态，记忆棋盘瞬间与物理真值对齐。",
    ),
    Scene(
        7,
        "act4_mpc",
        "第四幕 · 边走边核对自动巡航",
        "多步预演试走 · 遇首次发货谎言自动重规划直至应收清零",
        "第四幕启动边走边核对自动巡航。系统向前预演试走多步，每次只落实一步；第一步我们再注入一次发货执行器撒谎。巡航控制器立刻凭实账核对否决谎言并重新规划路径，随后自动完成出库、过账开票与收款核销。",
    ),
    Scene(
        8,
        "odoo_final",
        "终局实账凭据 · 银行收款清零",
        "核心账套增值税发票全额已收款 对比 传统直连依然脱节",
        "来看外部企业核心账套的最终实账凭据：第一单已完成物理出库、发票过账与银行收款核销，当前未收余款为零元；而传统直连对照组依然滞留在账实分裂态。业务本体沙盘用三看板与自动巡航，让智能体真正敢上生产。",
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


def _mock_feishu_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    del headers, body, timeout
    if url.endswith("/tenant_access_token/internal"):
        return 200, json.dumps(
            {"code": 0, "tenant_access_token": "t-video-mock", "expire": 7200}
        ).encode("utf-8")
    if "/open-apis/im/v1/messages/" in url and method == "PATCH":
        return 200, json.dumps({"code": 0, "msg": "ok"}).encode("utf-8")
    return 200, json.dumps({"code": 0, "data": {"message_id": "om_video_feishu_01"}}).encode("utf-8")


def _inject_subtitle_and_check_dom(page: Page, scene: Scene) -> None:
    page.evaluate(
        """([badge, title, narration]) => {
            let old = document.getElementById("video-sub-overlay");
            if (old) old.remove();
            const footer = document.querySelector(".footer-strip");
            if (footer) footer.style.display = "none";
            document.body.style.paddingBottom = "138px";
            const bar = document.createElement("div");
            bar.id = "video-sub-overlay";
            bar.style.cssText = [
                "position:fixed",
                "left:0",
                "right:0",
                "bottom:0",
                "z-index:999999",
                "padding:14px 44px 18px",
                "background:linear-gradient(180deg, rgba(4,8,18,0.90), rgba(2,6,14,0.98))",
                "border-top:2px solid rgba(56,189,248,0.6)",
                "display:flex",
                "flex-direction:column",
                "align-items:center",
                "gap:6px",
                "box-shadow:0 -10px 30px rgba(0,0,0,0.65)"
            ].join(";");
            const meta = document.createElement("div");
            meta.style.cssText = "font-size:15px;font-weight:700;color:#38bdf8;letter-spacing:.6px;";
            meta.textContent = badge + "  ｜  " + title;
            const text = document.createElement("div");
            text.style.cssText = "font-size:25px;font-weight:700;line-height:1.42;color:#f8fafc;text-align:center;max-width:1760px;";
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

    app = ThreeBoardsStudioApp(
        STATE_DIR,
        partner_ref="O2C-INVESTOR-VIDEO",
        partner_name="视频演示三看板客户",
        order_seeds=(("V3B-SO-001", 2, 10_000.0), ("V3B-SO-002", 12, 10_000.0)),
        bare_partner_ref="O2C-VIDEO-BARE-MCP",
        bare_partner_name="视频演示对照组客户",
        bare_order_seeds=(("VMCP-SO-001", 2, 10_000.0),),
        feishu_transport=_mock_feishu_transport,
    )
    app.feishu_client.app_id = "cli_video_showcase"
    app.feishu_client.app_secret = "sec_video_mock_internal_only"
    app.feishu_client.receive_id = "oc_o2c_finance_group"
    app.feishu_client.receive_id_type = "chat_id"

    server = create_server(app, host="127.0.0.1", port=0)
    port = int(server.server_address[1])
    app.port = port
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    studio_url = f"http://127.0.0.1:{port}/"
    voucher_url = f"http://127.0.0.1:{port}/?view=voucher"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=1
            )

            def _shoot(url: str, scene: Scene) -> None:
                page.goto(url, wait_until="networkidle")
                page.wait_for_timeout(320)
                _inject_subtitle_and_check_dom(page, scene)
                page.screenshot(path=str(scene.frame_path))
                print(f"  frame -> {scene.frame_path.name}")

            # Scene 1: 演播室初始全景
            _shoot(studio_url, SCENES[0])

            # Scene 2: 外部核心账套实账凭据核验大屏（实时取自真实 Odoo 19）
            _shoot(voucher_url, SCENES[1])

            # Scene 3: 第一幕
            app.run_act_1()
            _shoot(studio_url, SCENES[2])

            # Scene 4: 第二幕
            app.run_act_2()
            _shoot(studio_url, SCENES[3])

            # Scene 5: 第三幕挂起
            app.run_act_3()
            _shoot(studio_url, SCENES[4])

            # Scene 6: 第三幕签批收敛
            app.decide_act_3(op="approve", operator_open_id="ou_director_roadshow")
            _shoot(studio_url, SCENES[5])

            # Scene 7: 第四幕边走边核对自动巡航收口
            app.run_act_4(inject_first_ship_drift=True)
            _shoot(studio_url, SCENES[6])

            # Scene 8: 外部实账财务凭据最终核销大屏（实时取自真实 Odoo 19 已收款发票）
            _shoot(voucher_url, SCENES[7])

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
